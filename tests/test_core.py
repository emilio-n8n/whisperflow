"""Tests hors-ligne : aucun micro, aucun appel réseau réel, aucune touche tapée."""
import json
import os
import struct
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from whisperflow import audio, config, daemon, history, inject, stt, textproc, ui


def make_wav(path: Path, seconds=1.0, amp=3000, rate=16000):
    import math
    n = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(amp * math.sin(i / 8))) for i in range(n)))
    return path


# ---------- config / secrets ----------
def test_secret_requires_600(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    f = tmp_path / "secrets.env"
    f.write_text("GROQ_API_KEY='gsk_abc'\n")
    os.chmod(f, 0o644)
    with pytest.raises(config.SecretError):
        config.load_secret("GROQ_API_KEY", f)
    os.chmod(f, 0o600)
    assert config.load_secret("GROQ_API_KEY", f) == "gsk_abc"


def test_config_toml(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[stt]\nmodel="whisper-large-v3"\nlanguage=""\n[audio]\nmax_seconds=10\n')
    c = config.load_config(p)
    assert c.stt.model == "whisper-large-v3" and c.stt.language == "" and c.audio.max_seconds == 10


def test_redact():
    assert config.redact("bad key gsk_123", "gsk_123") == "bad key ***"


# ---------- audio ----------
def test_repair_truncated_wav(tmp_path):
    p = make_wav(tmp_path / "a.wav", 0.5)
    data = bytearray(p.read_bytes())
    struct.pack_into("<I", data, 4, 0); struct.pack_into("<I", data, 40, 0)  # en-tête "en cours"
    p.write_bytes(bytes(data))
    audio.repair_wav(p)
    dur, rms = audio.wav_stats(p)
    assert abs(dur - 0.5) < 0.01 and rms > 1000


def test_silence_rms(tmp_path):
    _, rms = audio.wav_stats(make_wav(tmp_path / "s.wav", 0.5, amp=0))
    assert rms == 0


def test_build_commands(tmp_path):
    cfg = config.AudioConfig(device="mic1")
    pw = audio.build_command("pw-record", cfg, tmp_path / "o.wav")
    assert pw[:2] == ["pw-record", "--rate"] and "--target" in pw and pw[-1].endswith("o.wav")
    ar = audio.build_command("arecord", cfg, tmp_path / "o.wav")
    assert "-D" in ar and "S16_LE" in ar


# ---------- texte ----------
@pytest.mark.parametrize("raw,expected", [
    ("  Bonjour à tous.  ", "Bonjour à tous. "),
    ("Sous-titres réalisés par la communauté d'Amara.org", ""),
    ("Merci d'avoir regardé !", ""),
    ("Première ligne. À la ligne deuxième ligne.", "Première ligne.\ndeuxième ligne. "),
    ("", ""),
])
def test_clean(raw, expected):
    assert textproc.clean(raw) == expected


def test_clean_keeps_real_merci():
    assert textproc.clean("Merci pour ton aide, à demain.") == "Merci pour ton aide, à demain. "


# ---------- STT avec faux serveur Groq ----------
class FakeGroq(BaseHTTPRequestHandler):
    calls = []
    script = []  # liste de (code, body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeGroq.calls.append((self.headers.get("Authorization"), self.headers.get("Content-Type"), body))
        code, payload = FakeGroq.script.pop(0)
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def groq():
    srv = HTTPServer(("127.0.0.1", 0), FakeGroq)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeGroq.calls.clear(); FakeGroq.script.clear()
    yield f"http://127.0.0.1:{srv.server_port}/openai/v1/audio/transcriptions"
    srv.shutdown()


def test_groq_request_shape(groq, tmp_path):
    FakeGroq.script = [(200, {"text": "Salut Émilio"})]
    t = stt.GroqTranscriber(config.STTConfig(language="fr", prompt="Omarchy"), "gsk_secret", groq)
    assert t.transcribe(make_wav(tmp_path / "a.wav", 0.2)) == "Salut Émilio"
    auth, ctype, body = FakeGroq.calls[0]
    assert auth == "Bearer gsk_secret" and ctype.startswith("multipart/form-data; boundary=")
    for part in (b'name="model"\r\n\r\nwhisper-large-v3-turbo', b'name="language"\r\n\r\nfr',
                 b'name="prompt"\r\n\r\nOmarchy', b'filename="a.wav"', b"RIFF"):
        assert part in body


def test_groq_retry_then_ok(groq, tmp_path):
    FakeGroq.script = [(503, {"error": "busy"}), (200, {"text": "ok"})]
    t = stt.GroqTranscriber(config.STTConfig(retries=2), "k", groq)
    assert t.transcribe(make_wav(tmp_path / "a.wav", 0.1)) == "ok" and len(FakeGroq.calls) == 2


def test_groq_401_no_retry_and_redacted(groq, tmp_path):
    FakeGroq.script = [(401, {"error": {"message": "invalid key gsk_leak"}})]
    t = stt.GroqTranscriber(config.STTConfig(retries=2), "gsk_leak", groq)
    with pytest.raises(stt.STTError) as e:
        t.transcribe(make_wav(tmp_path / "a.wav", 0.1))
    assert "401" in str(e.value) and "gsk_leak" not in str(e.value) and len(FakeGroq.calls) == 1


def test_groq_size_limit(tmp_path):
    big = tmp_path / "big.wav"
    big.write_bytes(b"0" * (stt.MAX_BYTES_FREE + 1))
    with pytest.raises(stt.STTError):
        stt.GroqTranscriber(config.STTConfig(), "k", "http://127.0.0.1:9").transcribe(big)


# ---------- insertion ----------
class FakeRun:
    def __init__(self, fail=()):
        self.calls, self.fail, self.clip, self.inputs = [], set(fail), b"ancien", []

    def __call__(self, cmd, input_=None, timeout=5):
        self.calls.append(cmd)
        if cmd[0] == "wl-copy":
            self.clip = input_
        self.inputs.append(input_)
        code = 1 if (cmd[0] in self.fail or (cmd == ["wtype", "-"] and "wtype-text" in self.fail)) else 0
        return SimpleNamespace(returncode=code, stdout=self.clip if cmd[0] == "wl-paste" else b"", stderr=b"x")


def which_all(_):
    return "/usr/bin/x"


def test_inject_wtype_handles_dash_and_accents():
    r = FakeRun()
    m = inject.WaylandInjector(config.InjectConfig(), r, which_all).insert("-é à ç")
    assert m == "wtype" and r.calls == [["wtype", "-"]] and r.inputs == ["-é à ç".encode()]


def test_inject_fallback_restores_clipboard():
    r = FakeRun(fail={"wtype-text"})
    m = inject.WaylandInjector(config.InjectConfig(), r, which_all).insert("texte")
    assert m == "clipboard"
    assert ["wtype", "-M", "ctrl", "-P", "v", "-p", "v", "-m", "ctrl"] in r.calls
    assert r.clip == b"ancien"  # presse-papiers rendu


def test_terminal_paste_shortcut():
    assert inject._shortcut_args("ctrl+shift+v") == ["-M", "ctrl", "-M", "shift", "-P", "v", "-p", "v", "-m", "shift", "-m", "ctrl"]


def test_inject_nothing_available():
    with pytest.raises(inject.InjectError):
        inject.WaylandInjector(config.InjectConfig(), FakeRun(), lambda _: None).insert("x")


# ---------- UI ----------
def test_waybar_json(tmp_path):
    f = tmp_path / "state.json"
    assert json.loads(ui.waybar_json(f))["class"] == "off"
    ind = ui.Indicator(config.UIConfig(notifications=False), f, run=lambda *a, **k: None, which=lambda _: None)
    ind.set("recording")
    assert json.loads(ui.waybar_json(f))["class"] == "recording"


# ---------- machine d'états du daemon ----------
class FakeRecorder:
    def __init__(self, tmp, seconds=1.0, amp=3000):
        self.tmp, self.seconds, self.amp, self._active, self.n = tmp, seconds, amp, False, 0

    @property
    def active(self):
        return self._active

    def start(self):
        self._active = True

    def stop(self):
        self._active = False
        self.n += 1
        p = make_wav(self.tmp / f"r{self.n}.wav", self.seconds, self.amp)
        d, r = audio.wav_stats(p)
        return audio.Recording(p, d, r)

    def cancel(self):
        self._active = False

    def elapsed(self):
        return 0.0


class FakeSTT:
    def __init__(self, text="Bonjour.", delay=0.0, error=None):
        self.text, self.delay, self.error = text, delay, error

    def transcribe(self, wav):
        time.sleep(self.delay)
        if self.error:
            raise stt.STTError(self.error)
        return self.text


class FakeInj:
    def __init__(self, fail=False):
        self.texts, self.fail = [], fail

    def insert(self, text):
        if self.fail:
            raise inject.InjectError("non")
        self.texts.append(text)
        return "wtype"


class FakeUI:
    def __init__(self):
        self.states = []

    def set(self, s, d=""):
        self.states.append((s, d))


def make_dictation(tmp, **kw):
    cfg = config.Config()
    h = history.History(tmp / "h.jsonl")
    d = daemon.Dictation(cfg, kw.get("rec") or FakeRecorder(tmp), kw.get("stt") or FakeSTT(),
                         kw.get("inj") or FakeInj(), FakeUI(), h)
    return d, h


def wait_idle(d, timeout=3):
    end = time.time() + timeout
    while time.time() < end and d.state != "idle":
        time.sleep(0.01)
    assert d.state == "idle"


def test_toggle_full_cycle(tmp_path):
    inj = FakeInj()
    d, h = make_dictation(tmp_path, inj=inj)
    assert d.toggle()["state"] == "recording"
    assert d.toggle()["state"] == "transcribing"
    wait_idle(d)
    assert inj.texts == ["Bonjour. "]
    last = h.last(1)[0]
    assert last["inserted"] is True and "total_ms" in last
    assert not list(tmp_path.glob("r*.wav"))  # audio supprimé


def test_cancel_during_transcription_never_types(tmp_path):
    inj = FakeInj()
    d, _ = make_dictation(tmp_path, inj=inj, stt=FakeSTT(delay=0.3))
    d.toggle(); d.toggle()
    d.cancel()
    time.sleep(0.5)
    assert inj.texts == [] and d.state == "idle"


def test_silence_is_ignored(tmp_path):
    inj = FakeInj()
    d, _ = make_dictation(tmp_path, inj=inj, rec=FakeRecorder(tmp_path, amp=0))
    d.toggle()
    assert d.toggle()["msg"] == "silence" and inj.texts == []


def test_insert_failure_kept_in_history(tmp_path):
    d, h = make_dictation(tmp_path, inj=FakeInj(fail=True))
    d.toggle(); d.toggle(); wait_idle(d)
    e = h.last(1)[0]
    assert e["inserted"] is False and e["text"] == "Bonjour. "
    assert d.ui.states[-1][0] == "error"


def test_stt_error_reported(tmp_path):
    d, _ = make_dictation(tmp_path, stt=FakeSTT(error="Réseau indisponible"))
    d.toggle(); d.toggle(); wait_idle(d)
    assert d.ui.states[-1] == ("error", "Réseau indisponible")


def test_busy_while_transcribing(tmp_path):
    d, _ = make_dictation(tmp_path, stt=FakeSTT(delay=0.3))
    d.toggle(); d.toggle()
    assert d.toggle()["ok"] is False  # pas de 2e enregistrement pendant la transcription
    wait_idle(d)


def test_hold_mode_start_stop(tmp_path):
    inj = FakeInj()
    d, _ = make_dictation(tmp_path, inj=inj)
    assert d.start()["ok"] and d.start()["ok"] is False  # répétition de touche ignorée
    d.stop(); wait_idle(d)
    assert inj.texts == ["Bonjour. "]


def test_history_permissions(tmp_path):
    h = history.History(tmp_path / "x" / "h.jsonl")
    h.add(text="secret")
    assert oct(os.stat(h.path).st_mode & 0o777) == "0o600"


# ---------- socket Unix bout en bout ----------
def test_socket_server(tmp_path):
    inj = FakeInj()
    d, _ = make_dictation(tmp_path, inj=inj)
    sock = tmp_path / "wf.sock"
    srv = daemon.Server(d, sock)
    th = threading.Thread(target=srv.serve, daemon=True); th.start()
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.01)
    assert daemon.send(sock, {"cmd": "ping"})["pong"]
    assert daemon.send(sock, {"cmd": "toggle"})["state"] == "recording"
    daemon.send(sock, {"cmd": "toggle"}); wait_idle(d)
    assert inj.texts == ["Bonjour. "]
    assert daemon.send(sock, {"cmd": "nope"})["ok"] is False
    assert oct(os.stat(sock).st_mode & 0o777) == "0o600"
    daemon.send(sock, {"cmd": "quit"}); th.join(2)
    assert not sock.exists()
