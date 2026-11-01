"""Tests hors-ligne des niveaux 2 et 3 : routeur, catalogue, Jev (faux serveur), ACP (faux agent),
WebSocket (faux serveur), Gemini Live (faux WS), OCR, garde-fous du computer use."""
import base64
import hashlib
import json
import socket
import struct
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from whisperflow import actions, agents, config, live, router, screen, ws
from whisperflow.actions import Executor, Result


# ---------------------------------------------------------------- utilitaires
class Rec:
    """Faux subprocess.run / spawn : enregistre les argv, ne lance rien."""

    def __init__(self, rc=0, stdout=b""):
        self.calls, self.spawned, self.rc, self.stdout = [], [], rc, stdout

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        return SimpleNamespace(returncode=self.rc, stdout=self.stdout, stderr=b"")

    def spawn(self, cmd):
        self.spawned.append(cmd)


class FakeConfirm:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def ask(self, q, d=""):
        self.asked.append(q)
        return self.answer


def make_exec(confirm=True, **kw):
    r = Rec()
    notes = []
    ex = Executor(config.ActionsConfig(), lambda t, b: notes.append((t, b)), FakeConfirm(confirm),
                  run=r.run, spawn=r.spawn, which=lambda _: "/usr/bin/x", **kw)
    return ex, r, notes


def make_router(jev=None, shortcuts=None, min_conf=0.6):
    ex, _, _ = make_exec()
    return router.Router(ex.describe(), list(config.ActionsConfig().apps), jev,
                         config.JevConfig(min_confidence=min_conf), shortcuts)


# ---------------------------------------------------------------- règles locales
@pytest.mark.parametrize("said,action,params", [
    ("Ouvre Firefox.", "open_app", {"app": "firefox"}),
    ("lance le terminal", "open_app", {"app": "terminal"}),
    ("Monte le son de vingt", "volume", {"op": "up", "value": 20}),
    ("baisse le volume", "volume", {"op": "down", "value": 10}),
    ("Mets le son à 40 %", "volume", {"op": "set", "value": 40}),
    ("coupe le son", "volume", {"op": "mute"}),
    ("Bureau trois", "workspace", {"n": 3}),
    ("va sur l'espace de travail 2", "workspace", {"n": 2}),
    ("Cherche le fichier facture", "find_file", {"query": "facture"}),
    ("Cherche sur internet météo Praia", "web_search", {"query": "meteo praia"}),
    ("va sur github.com", "open_url", {"url": "github.com"}),
    ("Mets un minuteur de 10 minutes pour le thé", "timer", {"minutes": 10, "label": "le the"}),
    ("Ferme la fenêtre", "close_window", {}),
    ("verrouille l'écran", "lock_screen", {}),
    ("capture d'écran", "screenshot", {}),
    ("Pause", "media", {"op": "pause"}),
    ("suivant", "media", {"op": "next"}),
    ("Montre-moi où est le bouton Enregistrer", "show_me", {"target": "enregistrer"}),
    ("Demande à OpenCode dans whisperflow d'ajouter des tests", "delegate_agent",
     {"project": "whisperflow", "task": "ajouter des tests"}),
    ("parle à Gemini", "live", {"op": "start"}),
])
def test_local_rules(said, action, params):
    r = make_router().route(said)
    assert (r.action, r.params, r.source) == (action, params, "rule")


def test_unknown_app_not_opened_by_rule():
    r = make_router().route("ouvre photoshop")
    assert r.action is None  # pas de Jev, pas d'appli autorisée


# ---------------------------------------------------------------- Jev (faux serveur OpenCode Zen)
class FakeJev(BaseHTTPRequestHandler):
    reply, seen = {}, []

    def do_POST(self):
        FakeJev.seen.append((self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        code = FakeJev.reply.get("_code", 200)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(FakeJev.reply).encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def jev_url():
    srv = HTTPServer(("127.0.0.1", 0), FakeJev)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeJev.seen.clear()
    yield f"http://127.0.0.1:{srv.server_port}/inference/systemone/v1/systemone"
    srv.shutdown()


def jev(url, key="oc-secret"):
    return router.JevClient(config.JevConfig(url=url), key)


def test_jev_request_shape_and_choice(jev_url):
    FakeJev.reply = {"answers": {"action": {"type": "choice", "choice": "screenshot", "confidence": 0.88,
                                            "probabilities": {"screenshot": 0.9, "none": 0.1}},
                                 "app": {"type": "choice", "choice": "none"}}}
    r = make_router(jev(jev_url)).route("immortalise ce que je vois là")
    assert (r.action, r.source) == ("screenshot", "jev")
    auth, body = FakeJev.seen[0]
    assert auth == "Bearer oc-secret" and body["model"] == "jev-1.13-free"
    q = body["questions"]["action"]
    assert q["type"] == "choice" and "none" in q["criteria"] and "screenshot" in q["criteria"]
    assert body["state"] == {"commande_vocale": "immortalise ce que je vois là"}


def test_jev_low_confidence_does_nothing(jev_url):
    FakeJev.reply = {"answers": {"action": {"type": "choice", "choice": "close_window", "confidence": 0.41,
                                            "probabilities": {"close_window": 0.45, "lock_screen": 0.4}}}}
    r = make_router(jev(jev_url)).route("fais disparaître ça")
    assert r.action is None and "Pas sûr" in r.message and r.alternatives


def test_jev_none_and_missing_params(jev_url):
    FakeJev.reply = {"answers": {"action": {"type": "choice", "choice": "none", "confidence": 0.9}}}
    assert make_router(jev(jev_url)).route("il fait beau").action is None
    FakeJev.reply = {"answers": {"action": {"type": "choice", "choice": "timer", "confidence": 0.9}}}
    r = make_router(jev(jev_url)).route("rappelle moi un truc")
    assert r.action is None and "détails" in r.message


def test_jev_app_choice(jev_url):
    FakeJev.reply = {"answers": {"action": {"type": "choice", "choice": "open_app", "confidence": 0.8},
                                 "app": {"type": "choice", "choice": "fichiers", "confidence": 0.9}}}
    r = make_router(jev(jev_url)).route("je veux voir mes dossiers")
    assert (r.action, r.params) == ("open_app", {"app": "fichiers"})


def test_jev_error_redacts_key(jev_url):
    FakeJev.reply = {"_code": 401, "error": "bad key oc-secret"}
    r = make_router(jev(jev_url)).route("truc flou")
    assert r.action is None and "401" in r.message and "oc-secret" not in r.message


def test_jev_model_access_disabled(jev_url):
    FakeJev.reply = {"_code": 403, "error": "Model access is disabled"}
    r = make_router(jev(jev_url)).route("truc flou")
    assert r.action is None and "non autorisé" in r.message and "jev-1.13-free" in r.message


def test_jev_wrong_type_is_error(jev_url):
    FakeJev.reply = {"answers": {"action": {"type": "noul", "noul": 0.9}}}
    assert make_router(jev(jev_url)).route("bidule").action is None


# ---------------------------------------------------------------- catalogue / exécution
def test_actions_use_fixed_argv_never_shell():
    ex, r, _ = make_exec()
    ex.execute("volume", {"op": "set", "value": 250})
    assert r.calls[-1] == ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "100%"]
    ex.execute("workspace", {"n": 4})
    assert r.calls[-1] == ["hyprctl", "dispatch", "workspace", "4"]
    ex.execute("web_search", {"query": "a; rm -rf ~"})
    assert r.spawned[-1][0] == "xdg-open" and "rm+-rf" in r.spawned[-1][1]


def test_disallowed_app_and_bad_params():
    ex, r, _ = make_exec()
    assert not ex.execute("open_app", {"app": "rm"}).ok
    assert not ex.execute("workspace", {"n": 42}).ok
    assert not ex.execute("open_url", {"url": "javascript:alert(1)"}).ok
    assert not ex.execute("inexistante").ok
    assert r.spawned == []


def test_confirm_required_for_close_window():
    ex, r, _ = make_exec(confirm=False)
    res = ex.execute("close_window")
    assert not res.ok and r.calls == []
    ex2, r2, _ = make_exec(confirm=True)
    assert ex2.execute("close_window").ok and r2.calls == [["hyprctl", "dispatch", "killactive"]]


def test_find_file(tmp_path):
    (tmp_path / "Docs").mkdir()
    (tmp_path / "Docs" / "Facture-Électricité.pdf").write_text("x")
    (tmp_path / "Docs" / "notes.txt").write_text("x")
    ex, r, notes = make_exec()
    ex.cfg.search_dirs = [str(tmp_path / "Docs")]
    res = ex.execute("find_file", {"query": "facture electricite"})
    assert res.ok and "Facture-Électricité.pdf" in res.data["files"][0]
    assert ex.execute("open_file", {"query": "notes"}).ok and r.spawned[-1][0] == "xdg-open"


def test_shortcuts_learn_and_replay(tmp_path):
    sc = router.Shortcuts(tmp_path / "s.json")
    ex, r, _ = make_exec(shortcuts=sc)
    rt = router.Router(ex.describe(), list(ex.cfg.apps), None, config.JevConfig(), sc)
    steps = [{"action": rt.route(p).action, "params": rt.route(p).params} for p in ("ouvre firefox", "bureau 2")]
    sc.save("travail", steps, ["mode boulot"])
    assert rt.route("Mode boulot").params == {"name": "travail"}
    res = ex.execute("run_shortcut", {"name": "Travail"})
    assert res.ok and r.spawned == [["firefox"]] and r.calls[-1] == ["hyprctl", "dispatch", "workspace", "2"]
    assert oct((tmp_path / "s.json").stat().st_mode & 0o777) == "0o600"


def test_confirmer_parses_notify_action():
    out = []
    c = actions.Confirmer(run=lambda cmd, **k: (out.append(cmd), SimpleNamespace(stdout="yes\n"))[1],
                          which=lambda _: "/usr/bin/notify-send")
    assert c.ask("Fermer ?") is True and "--wait" in out[0] and "yes=Oui" in out[0]
    assert actions.Confirmer(which=lambda _: None).ask("x") is False


# ---------------------------------------------------------------- ACP avec un faux agent OpenCode
FAKE_AGENT = textwrap.dedent(r'''
    import json, sys
    def out(m): sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
    pending = {}
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("method") == "initialize":
            out({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}})
        elif m.get("method") == "session/new":
            out({"jsonrpc": "2.0", "id": m["id"], "result": {"sessionId": "sess_1"}})
        elif m.get("method") == "session/load":
            out({"jsonrpc": "2.0", "id": m["id"], "result": {}})
        elif m.get("method") == "session/prompt":
            sid = m["params"]["sessionId"]; pending["prompt"] = m["id"]
            text = m["params"]["prompt"][0]["text"]
            out({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Je regarde. "}}}})
            out({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Lire main.py", "kind": "read", "status": "pending"}}})
            out({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {"sessionId": sid, "toolCall": {"toolCallId": "c1", "title": "Lire main.py", "kind": "read"}, "options": [{"optionId": "a1", "name": "Autoriser", "kind": "allow_once"}, {"optionId": "aa", "name": "Toujours", "kind": "allow_always"}, {"optionId": "r1", "name": "Refuser", "kind": "reject_once"}]}})
            if "slow" not in text:
                out({"jsonrpc": "2.0", "id": 901, "method": "session/request_permission", "params": {"sessionId": sid, "toolCall": {"toolCallId": "c2", "title": "rm -rf build", "kind": "execute"}, "options": [{"optionId": "a2", "name": "Autoriser", "kind": "allow_once"}, {"optionId": "r2", "name": "Refuser", "kind": "reject_once"}]}})
        elif m.get("id") in (900, 901):
            out({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "sess_1", "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "perm%d=%s " % (m["id"], json.dumps(m["result"]["outcome"]))}}}})
            if m["id"] == 901:
                out({"jsonrpc": "2.0", "id": pending.pop("prompt"), "result": {"stopReason": "end_turn"}})
        elif m.get("method") == "session/cancel":
            out({"jsonrpc": "2.0", "id": pending.pop("prompt"), "result": {"stopReason": "cancelled"}})
''')


@pytest.fixture
def fake_agent(tmp_path):
    p = tmp_path / "fake_agent.py"
    p.write_text(FAKE_AGENT)
    return [sys.executable, str(p)]


def wait_for(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def manager(tmp_path, argv, confirm):
    notes = []
    cfg = config.AgentConfig(command=argv, default_cwd=str(tmp_path))
    m = agents.AgentManager(cfg, tmp_path / "sess.json", lambda t, b: notes.append((t, b)), FakeConfirm(confirm))
    return m, notes


def test_acp_full_turn_with_permissions(tmp_path, fake_agent):
    m, notes = manager(tmp_path, fake_agent, confirm=False)
    tid = m.submit("ajoute un test")
    assert wait_for(lambda: m.tasks[tid].status == "done")
    t = m.tasks[tid]
    assert t.stop_reason == "end_turn" and "Je regarde." in t.output
    assert 'perm900={"outcome": "selected", "optionId": "a1"}' in t.output      # lecture : auto, jamais "always"
    assert 'perm901={"outcome": "selected", "optionId": "r2"}' in t.output      # exécution refusée
    assert json.loads((tmp_path / "sess.json").read_text())[str(tmp_path.resolve())] == "sess_1"
    m.shutdown()


def test_acp_cancel(tmp_path, fake_agent):
    m, _ = manager(tmp_path, fake_agent, confirm=True)
    tid = m.submit("slow task")
    assert wait_for(lambda: m.tasks[tid].session_id and m.tasks[tid].output)
    assert m.cancel() == [tid]
    assert wait_for(lambda: m.tasks[tid].status == "cancelled")
    m.shutdown()


def test_acp_agent_missing(tmp_path):
    m, notes = manager(tmp_path, [sys.executable, "-c", "import sys; sys.exit(1)"], True)
    tid = m.submit("x")
    assert wait_for(lambda: m.tasks[tid].status == "error")


def test_choose_option_never_always():
    opts = [{"optionId": "aa", "kind": "allow_always"}]
    assert agents.choose_option(opts, True) == {"outcome": "cancelled"}


# ---------------------------------------------------------------- WebSocket (faux serveur)
def ws_server(handler):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        key = [l.split(b": ")[1] for l in req.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + ws.GUID.encode()).digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        handler(conn)

    threading.Thread(target=serve, daemon=True).start()
    return f"ws://127.0.0.1:{srv.getsockname()[1]}/ws?key=abc"


def read_frame(conn):
    h = conn.recv(2)
    n = h[1] & 0x7F
    if n == 126:
        n = struct.unpack("!H", conn.recv(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", conn.recv(8))[0]
    mask = conn.recv(4)
    data = b""
    while len(data) < n:
        data += conn.recv(n - len(data))
    return h[0] & 0x0F, ws.xor_mask(data, mask)


def test_websocket_roundtrip_ping_fragments_close():
    got = []

    def handler(conn):
        op, data = read_frame(conn)
        got.append(data)
        conn.sendall(ws.encode_frame(ws.OP_PING, b"hi", mask=False))
        assert read_frame(conn) == (ws.OP_PONG, b"hi")
        conn.sendall(ws.encode_frame(ws.OP_TEXT, b'{"a":', mask=False, fin=False))
        conn.sendall(ws.encode_frame(ws.OP_CONT, b"1}", mask=False))
        big = b"x" * 70000
        conn.sendall(ws.encode_frame(ws.OP_BIN, big, mask=False))
        conn.sendall(ws.encode_frame(ws.OP_CLOSE, struct.pack("!H", 1008) + b"policy", mask=False))

    c = ws.WebSocket(ws_server(handler))
    c.send_text("héllo")
    assert c.recv() == '{"a":1}'
    assert c.recv() == b"x" * 70000
    with pytest.raises(ws.WSClosed) as e:
        c.recv()
    assert e.value.code == 1008 and got == ["héllo".encode()]


def test_xor_mask_roundtrip():
    k = b"\x01\x02\x03\x04"
    assert ws.xor_mask(ws.xor_mask(b"bonjour \xc3\xa9", k), k) == b"bonjour \xc3\xa9"


# ---------------------------------------------------------------- Gemini Live (faux WS)
class FakeWS:
    def __init__(self, script):
        self.sent, self.script, self.closed = [], list(script), False
        self.ev = threading.Event()

    def send_text(self, t):
        self.sent.append(json.loads(t))
        self.ev.set()

    def recv(self):
        while not self.script:
            if self.closed:
                raise ws.WSClosed(1000, "fin")
            time.sleep(0.01)
        item = self.script.pop(0)
        return json.dumps(item).encode() if isinstance(item, dict) else item

    def close(self):
        self.closed = True


class FakeMic:
    def __init__(self):
        self.n = 0

    def read(self, n):
        time.sleep(0.02)
        self.n += 1
        return b"\x00" * n if self.n < 3 else b""  # puis micro coupé

    def close(self):
        pass


class FakeSpeaker:
    def __init__(self):
        self.data, self.flushes = b"", 0

    def write(self, pcm):
        self.data += pcm

    def flush(self):
        self.flushes += 1

    def close(self):
        pass


class FakeScreen:
    def __init__(self, boxes_seq=None):
        self.boxes_seq = boxes_seq or []
        self.clicks, self.keys_pressed = [], []

    def frame_jpeg(self, w):
        return b"\xff\xd8JPEG"

    def read(self):
        boxes = self.boxes_seq.pop(0) if len(self.boxes_seq) > 1 else self.boxes_seq[0]
        return screen.Monitor("eDP-1", 0, 0, 2560, 1440, 2.0), boxes

    def click(self, x, y, double=False):
        self.clicks.append((x, y, double))

    def keys(self, c):
        screen.Screen(run=lambda *a, **k: None).keys(c)  # validation réelle
        self.keys_pressed.append(c)

    def scroll(self, d, a):
        pass


def make_live(script, computer_use=False, boxes=None, confirm=True):
    cfg = config.LiveConfig(computer_use=computer_use, send_screen=False)
    fws, spk, scr = FakeWS(script), FakeSpeaker(), FakeScreen(boxes or [[]])
    texts, ends = [], []

    class Agents:
        def submit(self, t, p=""):
            return "t1"

        def list(self):
            return []

    def tools_factory(session):
        ann = SimpleNamespace(show=lambda t: Result(True, f"montré {t}"))
        return live.LiveTools(cfg, scr, lambda p: Result(True, f"fait: {p}"), ann, Agents(),
                              SimpleNamespace(insert=lambda t: None), FakeConfirm(confirm), session.send_frame)

    s = live.LiveSession(cfg, "AIza-secret", tools_factory, scr, lambda w, t: texts.append((w, t)),
                         ends.append, ws_factory=lambda url: fws, mic_factory=FakeMic, speaker_factory=lambda: spk)
    return s, fws, spk, scr, texts, ends


def test_live_setup_audio_tools_and_transcripts():
    pcm = base64.b64encode(b"\x01\x02" * 10).decode()
    script = [{"setupComplete": {}},
              {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": pcm}}]},
                                 "outputTranscription": {"text": "Bonjour"}}},
              {"serverContent": {"interrupted": True}},
              {"toolCall": {"functionCalls": [{"id": "f1", "name": "run_command", "args": {"phrase": "monte le son"}}]}}]
    s, fws, spk, scr, texts, ends = make_live(script)
    s.start()
    setup = fws.sent[0]["setup"]
    assert setup["model"] == "models/gemini-3.8-live" and setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    names = {f["name"] for f in setup["tools"][0]["functionDeclarations"]}
    assert "run_command" in names and "click_text" not in names      # computer use désactivé par défaut
    assert wait_for(lambda: any("toolResponse" in m for m in fws.sent))
    resp = next(m for m in fws.sent if "toolResponse" in m)["toolResponse"]["functionResponses"][0]
    assert resp["id"] == "f1" and resp["response"]["status"] == "ok" and "retryable" in resp["response"]
    assert spk.data == b"\x01\x02" * 10 and spk.flushes == 1 and ("gemini", "Bonjour") in texts
    assert wait_for(lambda: any("audio" in m.get("realtimeInput", {}) for m in fws.sent))
    a = next(m for m in fws.sent if "audio" in m.get("realtimeInput", {}))["realtimeInput"]["audio"]
    assert a["mimeType"] == "audio/pcm;rate=16000"
    assert wait_for(lambda: ends)  # micro coupé -> fin propre
    assert "AIza-secret" not in json.dumps(fws.sent)


def test_live_setup_refused():
    s, *_ = make_live([{"error": "bad model"}])
    with pytest.raises(RuntimeError, match="refusé"):
        s.start()


def test_computer_use_disabled_by_default():
    s, *_ = make_live([{"setupComplete": {}}])
    r = s.tools.call("click_text", {"text": "OK"})
    assert r["status"] == "error" and "désactivé" in r["message"]


def box(t, x=100, y=100):
    return screen.Box(t, x, y, 80, 20, 95)


def test_click_text_verified_and_mapped_to_logical_coords():
    s, fws, spk, scr, *_ = make_live([{"setupComplete": {}}], computer_use=True,
                                     boxes=[[box("Fichier"), box("Enregistrer", 400, 200)]])
    s.ws = fws  # session ouverte
    r = s.tools.call("click_text", {"text": "enregistrer"})
    assert r["status"] == "ok" and scr.clicks == [(220, 105, False)]   # (400+40)/2, (200+10)/2
    assert any("video" in m.get("realtimeInput", {}) for m in fws.sent)  # nouvelle capture après l'action


def test_click_refused_if_screen_moved():
    s, _, _, scr, *_ = make_live([{"setupComplete": {}}], computer_use=True,
                                 boxes=[[box("Enregistrer", 400, 200)], [box("Enregistrer", 600, 500)]])
    r = s.tools.call("click_text", {"text": "Enregistrer"})
    assert r["status"] == "error" and "changé" in r["message"] and scr.clicks == []


def test_risky_click_needs_confirmation():
    s, _, _, scr, *_ = make_live([{"setupComplete": {}}], computer_use=True, boxes=[[box("Supprimer le compte")]],
                                 confirm=False)
    r = s.tools.call("click_text", {"text": "Supprimer le compte"})
    assert r["status"] == "error" and scr.clicks == []


def test_ambiguous_target_not_clicked():
    s, _, _, scr, *_ = make_live([{"setupComplete": {}}], computer_use=True,
                                 boxes=[[box("Envoyer", 10, 10), box("Envoyer", 300, 300)]])
    assert s.tools.call("click_text", {"text": "Envoyer"})["status"] == "error" and scr.clicks == []


def test_press_keys_whitelist():
    s, *_ = make_live([{"setupComplete": {}}], computer_use=True)
    assert s.tools.call("press_keys", {"combo": "ctrl+l"})["status"] == "ok"
    assert s.tools.call("press_keys", {"combo": "super+q"})["status"] == "error"


def test_unknown_tool():
    s, *_ = make_live([{"setupComplete": {}}])
    assert s.tools.call("rm_rf", {})["status"] == "error"


# ---------------------------------------------------------------- OCR / géométrie
TSV = "\n".join([
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
    "5\t1\t1\t1\t1\t1\t10\t20\t50\t15\t96\tEnregistrer",
    "5\t1\t1\t1\t1\t2\t65\t20\t30\t15\t95\tsous",
    "5\t1\t2\t1\t1\t1\t300\t400\t60\t15\t91\tAnnuler",
    "5\t1\t3\t1\t1\t1\t5\t5\t9\t9\t12\t~~",
])


def test_parse_tsv_groups_lines_and_drops_noise():
    boxes = screen.parse_tesseract_tsv(TSV)
    assert [b.text for b in boxes] == ["Enregistrer sous", "Annuler"]
    assert (boxes[0].left, boxes[0].width) == (10, 85)


def test_match_target_accents_and_fuzzy():
    boxes = [screen.Box("Paramètres", 0, 0, 1, 1, 90), screen.Box("Aide", 0, 0, 1, 1, 90)]
    assert screen.match_target(boxes, "parametres")[0].text == "Paramètres"
    assert screen.match_target(boxes, "paramtres")[0].text == "Paramètres"
    assert screen.match_target(boxes, "fenêtre")[0] is None


def test_monitor_mapping_with_offset():
    m = screen.Monitor("HDMI-A-1", 1920, 0, 2560, 1440, 1.25)
    assert m.to_logical(1280, 720) == (2944, 576)


def test_keys_build_wtype_args():
    calls = []
    screen.Screen(run=lambda cmd, **k: calls.append(cmd)).keys("ctrl+shift+tab")
    assert calls[0] == ["wtype", "-M", "ctrl", "-M", "shift", "-P", "Tab", "-p", "Tab", "-m", "shift", "-m", "ctrl"]


# ---------------------------------------------------------------- intégration : mode commande + Brain
def test_command_mode_routes_instead_of_typing(tmp_path, monkeypatch):
    from whisperflow import brain, daemon, history
    from tests.test_core import FakeInj, FakeRecorder, FakeSTT, FakeUI
    notes = []
    cfg = config.Config()
    inj = FakeInj()
    b = brain.Brain(cfg, inj, lambda t, m: notes.append(m), tmp_path, history.History(tmp_path / "h.jsonl"),
                    confirmer=FakeConfirm(True), secret=lambda n: (_ for _ in ()).throw(config.SecretError(n)))
    calls = []
    b.executor._run = lambda cmd, **k: (calls.append(cmd), SimpleNamespace(returncode=0, stdout=b""))[1]
    d = daemon.Dictation(cfg, FakeRecorder(tmp_path), FakeSTT("Bureau deux."), inj, FakeUI(),
                         history.History(tmp_path / "h2.jsonl"), commander=b.handle_text)
    d.toggle("command"); d.toggle()
    assert wait_for(lambda: d.state == "idle" and d.last)
    assert calls == [["hyprctl", "dispatch", "workspace", "2"]] and inj.texts == []
    assert d.last["ok"] and "Bureau 2" in notes[-1]
    # Gemini sans clé : message clair, rien ne plante
    assert not b.live_ctl("start").ok
    assert b.panic()["live"] is False
