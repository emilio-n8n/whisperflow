"""Tests hors ligne de l'accès distant : page, jeton, WebSocket, relais audio/captures.

Aucun cloudflared, aucun Gemini, aucune capture réelle : le serveur est monté sur
127.0.0.1 avec une fausse session.
"""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from whisperflow import config, remote, ws
from whisperflow.live import LiveSession


class FakeSession:
    def __init__(self, hooks):
        self.hooks = hooks
        self.audio = []
        self.frames = 0
        self.active = False
        self.stop_reason = None

    def start(self):
        self.active = True

    def stop(self, reason="arrêt"):
        if not self.active:
            return
        self.active = False
        self.stop_reason = reason
        self.hooks.end(reason)

    def send_audio(self, pcm):
        self.audio.append(pcm)

    def send_frame(self):
        self.frames += 1
        self.hooks.frame(b"\xff\xd8jpeg-factice")


def wait_for(check, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def server():
    made = {}

    def factory(hooks):
        session = FakeSession(hooks)
        made["session"] = session
        return session

    srv = remote.RemoteServer(config.RemoteConfig(port=0, token_ttl_min=1), factory, tunnel=False)
    info = srv.start()
    made["srv"] = srv
    made["info"] = info
    try:
        yield made
    finally:
        srv.stop("fin de test")


def ws_url(info: dict) -> str:
    u = urllib.parse.urlsplit(info["url"])
    return f"ws://{u.netloc}/ws?t={urllib.parse.parse_qs(u.query)['t'][0]}"


def test_page_requires_token(server):
    info = server["info"]
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(info["url"].replace("?t=", "?x="), timeout=5)
    assert err.value.code == 403
    with urllib.request.urlopen(info["url"], timeout=5) as r:
        body = r.read().decode()
        assert r.status == 200 and "WhisperFlow" in body and "/ws?t=" in body


def test_health_needs_token(server):
    info = server["info"]
    base = info["url"].split("/?")[0]
    token = urllib.parse.parse_qs(urllib.parse.urlsplit(info["url"]).query)["t"][0]
    with urllib.request.urlopen(f"{base}/health?t={token}", timeout=5) as r:
        assert r.read() == b"ok"
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{base}/health", timeout=5)


def test_websocket_relays_audio_and_frames(server):
    client = ws.WebSocket(ws_url(server["info"]), timeout=5)
    try:
        hello = json.loads(client.recv())
        assert hello["type"] == "ready" and server["session"].active

        client.send_bytes(b"\x01\x00" * 1600)
        assert wait_for(lambda: server["session"].audio)
        assert server["session"].audio[0] == b"\x01\x00" * 1600

        session = server["session"]
        session.hooks.audio(b"\x02\x00" * 480)
        assert client.recv() == b"\x02\x00" * 480

        session.hooks.frame(b"\xff\xd8photo")
        frame = json.loads(client.recv())
        assert frame["type"] == "frame" and frame["mime"] == "image/jpeg"
        assert base64.b64decode(frame["data"]) == b"\xff\xd8photo"

        client.send_text(json.dumps({"type": "frame"}))
        assert wait_for(lambda: session.frames == 1)
        assert json.loads(client.recv())["type"] == "frame"

        client.send_text(json.dumps({"type": "stop"}))
        end = json.loads(client.recv())
        assert end["type"] == "end" and "téléphone" in end["reason"]
        assert session.stop_reason == "téléphone déconnecté"
    finally:
        client.close()


def test_websocket_rejects_bad_token(server):
    bad = ws_url(server["info"]).replace(urllib.parse.parse_qs(urllib.parse.urlsplit(server["info"]["url"]).query)["t"][0],
                                         "mauvais")
    with pytest.raises(ws.WSError):
        ws.WebSocket(bad, timeout=5)


def test_websocket_reports_factory_error():
    def factory(hooks):
        raise RuntimeError("Gemini indisponible")

    srv = remote.RemoteServer(config.RemoteConfig(port=0, token_ttl_min=1), factory, tunnel=False)
    info = srv.start()
    client = ws.WebSocket(ws_url(info), timeout=5)
    try:
        msg = json.loads(client.recv())
        assert msg["type"] == "error" and "Gemini indisponible" in msg["message"]
        with pytest.raises(ws.WSClosed):
            client.recv()
    finally:
        client.close()
        srv.stop("fin de test")


def test_local_and_remote_sessions_are_exclusive(tmp_path):
    from types import SimpleNamespace

    from whisperflow import brain

    cfg = config.Config()
    b = brain.Brain(cfg, SimpleNamespace(insert=lambda t: "none"), lambda t, m: None, tmp_path,
                    confirmer=SimpleNamespace(ask=lambda q, d="": False),
                    secret=lambda n: (_ for _ in ()).throw(config.SecretError(n)))
    b.live = SimpleNamespace(active=True)
    refused = b.remote_ctl("start")
    assert not refused.ok and "local" in refused.message

    b.live = None
    b.remote = SimpleNamespace(active=True, info=lambda: {"active": True})
    refused = b.live_ctl("start")
    assert not refused.ok and "distant" in refused.message


def test_tunnel_url_parsing(tmp_path, monkeypatch):
    line = "2026-11-01T10:00:00Z INF |  https://bytes-possession-receive-arrival.trycloudflare.com  |"
    assert remote.quick_tunnel_url(line) == "https://bytes-possession-receive-arrival.trycloudflare.com"
    # l'API de Cloudflare apparaît dans les logs : ce n'est pas un tunnel
    assert remote.quick_tunnel_url('ERR Post "https://api.trycloudflare.com/tunnel": timeout') is None
    assert remote.quick_tunnel_url("INF Requesting new quick Tunnel on trycloudflare.com...") is None
    monkeypatch.setattr(remote.shutil, "which", lambda _: None)
    monkeypatch.setattr(remote.Path, "home", lambda: tmp_path)
    srv = remote.RemoteServer(config.RemoteConfig(), factory=lambda h: None)
    with pytest.raises(RuntimeError, match="cloudflared absent"):
        srv._tunnel_binary()


def test_live_session_remote_audio_hooks():
    got = {"audio": [], "frames": 0, "interrupt": 0, "ended": []}
    session = LiveSession(
        config.LiveConfig(), "cle", lambda s: None, screen=None,
        on_text=lambda w, t: None, on_end=got["ended"].append, local_audio=False,
        on_audio=got["audio"].append,
        on_frame=lambda jpg: got.__setitem__("frames", got["frames"] + 1),
        on_interrupt=lambda: got.__setitem__("interrupt", got["interrupt"] + 1),
    )
    assert session.mic is None and session.speaker is None
    session.handle({"serverContent": {
        "interrupted": True,
        "modelTurn": {"parts": [{"inlineData": {"data": base64.b64encode(b"\x01\x02").decode()}}]},
        "inputTranscription": {"text": "bonjour"},
    }})
    assert got["audio"] == [b"\x01\x02"] and got["interrupt"] == 1


def test_live_session_local_speaker_still_used():
    class Spk:
        def __init__(self):
            self.written, self.flushed = [], 0

        def write(self, pcm):
            self.written.append(pcm)

        def flush(self):
            self.flushed += 1

    session = LiveSession(config.LiveConfig(), "cle", lambda s: None, screen=None,
                          on_text=lambda w, t: None, on_end=lambda r: None)
    session.speaker = Spk()
    session.handle({"serverContent": {"interrupted": True,
                                      "modelTurn": {"parts": [{"inlineData": {"data": "AQI="}}]}}})
    assert session.speaker.written == [b"\x01\x02"] and session.speaker.flushed == 1
