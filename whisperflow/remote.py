"""Accès distant temporaire : iPhone → WhisperFlow → Gemini Live.

Le téléphone ouvre une page servie par ce module, capture son micro et parle à
Gemini Live par WebSocket. Le serveur relaie l'audio au PC, renvoie la voix de
Gemini et pousse les captures d'écran ; les outils (actions, clics, agents)
s'exécutent sur le PC, exactement comme en local.

Exposition : `cloudflared` en tunnel rapide (URL trycloudflare.com, HTTPS/WSS).
Sécurité : écoute 127.0.0.1 seulement, jeton aléatoire exigé sur la page et le
WebSocket, un seul client, expiration automatique. L'URL reste publique :
quiconque l'a avec le jeton peut piloter le PC via Gemini, donc ne la partage pas.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import http.server
from pathlib import Path
from typing import Callable

from .config import RemoteConfig
from .remote_page import PAGE
from .ws import GUID, OP_BIN, OP_CLOSE, OP_CONT, OP_PING, OP_PONG, OP_TEXT, encode_frame, xor_mask

log = logging.getLogger("whisperflow.remote")

TUNNEL_RE = re.compile(r"https://([a-z0-9-]+)\.trycloudflare\.com")
# cloudflared parle aussi de son API dans ses logs : ce ne sont pas des tunnels.
RESERVED_TUNNEL_HOSTS = {"api", "www", "cdn", "dash", "trycloudflare"}
OP_MASK = 0x80

ERROR_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0b0f"><title>WhisperFlow</title>
<style>
  html,body{height:100%;margin:0}
  body{background:#0b0b0f;color:#f4f4f5;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       display:grid;place-items:center;padding:2rem}
  main{max-width:22rem;text-align:center}
  .dot{width:.7rem;height:.7rem;border-radius:50%;background:#f87171;display:inline-block;margin-right:.5rem}
  h1{font-size:1.1rem;font-weight:600;margin:0 0 1rem;letter-spacing:.01em}
  p{color:#a1a1aa;margin:.4rem 0}
  code{background:#18181b;border:1px solid #27272a;border-radius:.4rem;padding:.1rem .35rem;font-size:.85em}
</style></head>
<body><main>
  <h1><span class="dot"></span>WhisperFlow</h1>
  <p>__MSG__</p>
  <p>Sur le PC, lance <code>whisperflow remote</code> pour obtenir un lien neuf.</p>
</main></body></html>
"""


def quick_tunnel_url(line: str) -> str | None:
    """URL d'un tunnel rapide dans une ligne de log (ignore api.trycloudflare.com)."""
    for m in TUNNEL_RE.finditer(line):
        host = m.group(1)
        if host in RESERVED_TUNNEL_HOSTS or len(host) < 8:
            continue
        return m.group(0)
    return None


class _WS:
    """Extrémité serveur d'un WebSocket (une seule session, lecture bloquante)."""

    def __init__(self, sock, reader=None):
        self.sock = sock
        self.reader = reader or sock  # rfile du handler : rien ne se perd entre en-têtes et trames
        self._lock = threading.Lock()
        self.closed = False

    def _send(self, opcode: int, payload: bytes) -> None:
        with self._lock:
            if self.closed:
                raise OSError("WebSocket fermé")
            self.sock.sendall(encode_frame(opcode, payload, mask=False))

    def send_json(self, obj: dict) -> None:
        self._send(OP_TEXT, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def send_bytes(self, data: bytes) -> None:
        if data:
            self._send(OP_BIN, data)

    def _exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.reader.read(n - len(buf))
            if not chunk:
                raise ConnectionError("socket fermée")
            buf += chunk
        return buf

    def recv_message(self):
        """(opcode, data) du prochain message complet, ou None à la fermeture."""
        first, parts = None, []
        while True:
            b1, b2 = self._exact(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._exact(8))[0]
            mask = self._exact(4) if b2 & OP_MASK else None
            data = self._exact(n) if n else b""
            if mask:
                data = xor_mask(data, mask)
            if op == OP_PING:
                self._send(OP_PONG, data)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                try:
                    self._send(OP_CLOSE, data[:2])
                except OSError:
                    pass
                return None
            if op != OP_CONT:
                first = op
            parts.append(data)
            if fin:
                return first, b"".join(parts)

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.shutdown(2)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class _Hooks:
    """Ce que la session Gemini appelle pour parler au téléphone."""

    def __init__(self, conn: _WS):
        self.conn = conn

    def audio(self, pcm: bytes) -> None:
        self.conn.send_bytes(pcm)  # binaire = PCM 16 bits / 24 kHz

    def frame(self, jpg: bytes) -> None:
        self.conn.send_json({"type": "frame", "mime": "image/jpeg",
                             "data": base64.b64encode(jpg).decode("ascii")})

    def text(self, who: str, text: str) -> None:
        self.conn.send_json({"type": "text", "who": who, "text": text})

    def interrupt(self) -> None:
        self.conn.send_json({"type": "interrupted"})

    def end(self, reason: str) -> None:
        try:
            self.conn.send_json({"type": "end", "reason": reason})
        except OSError:
            pass
        self.conn.close()


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, cfg: RemoteConfig, factory: Callable[[_Hooks], object]):
        self.cfg = cfg
        self.factory = factory
        self.token = secrets.token_urlsafe(24)
        self.client_lock = threading.Lock()
        self.session = None
        self.started = time.time()
        super().__init__(addr, _Handler)


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "WhisperFlow"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # pas de logs de requêtes (le jeton est dans l'URL)
        pass

    def _auth(self, query: dict) -> bool:
        given = (query.get("t") or [""])[0]
        return bool(given) and hmac.compare_digest(given, self.server.token)

    def _reply(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
               headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/ws":
                self._ws(q)
            elif u.path in ("/", "/index.html"):
                if not self._auth(q):
                    self._error(403, "Ce lien est invalide ou a expiré.")
                    return
                self._reply(200, PAGE.encode("utf-8"), "text/html; charset=utf-8", {
                    "Content-Security-Policy": "default-src 'self' 'unsafe-inline' data: blob:; "
                                               "connect-src 'self' wss: ws:; media-src 'self' blob: data:"})
            elif u.path == "/health":
                if not self._auth(q):
                    self._reply(403, b"refuse")
                    return
                self._reply(200, b"ok")
            else:
                self._error(404, "Cette page n'existe pas.")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, code: int, message: str) -> None:
        body = ERROR_HTML.replace("__MSG__", message).encode("utf-8")
        self._reply(code, body, "text/html; charset=utf-8")

    def do_POST(self) -> None:
        self._error(405, "Cette adresse ne reçoit que des visites (GET).")

    def _ws(self, query: dict) -> None:
        if not self._auth(query):
            self._error(403, "Ce lien est invalide ou a expiré.")
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        if "websocket" not in (self.headers.get("Upgrade") or "").lower() or not key:
            self._reply(400, b"WebSocket attendu")
            return
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.flush()
        except OSError:
            return

        conn = _WS(self.connection, self.rfile)
        srv = self.server
        if not srv.client_lock.acquire(blocking=False):
            try:
                conn.send_json({"type": "error", "message": "Une autre session est déjà connectée."})
            except OSError:
                pass
            conn.close()
            return
        session = None
        try:
            hooks = _Hooks(conn)
            session = srv.factory(hooks)
            srv.session = session
            session.start()
            conn.send_json({"type": "ready"})
            while True:
                msg = conn.recv_message()
                if msg is None:
                    break
                op, data = msg
                if op == OP_BIN:
                    session.send_audio(data)
                elif op == OP_TEXT:
                    try:
                        cmd = json.loads(data.decode("utf-8"))
                    except ValueError:
                        continue
                    kind = cmd.get("type")
                    if kind == "stop":
                        break
                    if kind == "frame":
                        session.send_frame()
                    elif kind == "ping":
                        conn.send_json({"type": "pong"})
        except Exception as e:  # noqa: BLE001
            log.warning("session distante interrompue : %s", e)
            try:
                conn.send_json({"type": "error", "message": str(e)})
            except OSError:
                pass
        finally:
            if session is not None:
                try:
                    session.stop("téléphone déconnecté")
                except Exception:  # noqa: BLE001
                    log.exception("arrêt de la session distante")
            srv.session = None
            conn.close()
            srv.client_lock.release()


class RemoteServer:
    """Serveur local + tunnel cloudflared, piloté par le daemon."""

    def __init__(self, cfg: RemoteConfig, factory: Callable[[_Hooks], object],
                 notify: Callable[[str, str], None] | None = None, tunnel: bool = True):
        self.cfg, self.factory, self._notify, self.use_tunnel = cfg, factory, notify, tunnel
        self.http: _Server | None = None
        self.thread: threading.Thread | None = None
        self.tunnel_proc: subprocess.Popen | None = None
        self.tunnel_lines: list[str] = []
        self.url = ""
        self.started = 0.0
        self.expires_at = 0.0
        self._timer: threading.Timer | None = None
        self._stopped = True
        self._lock = threading.Lock()

    # ---- cycle de vie ----------------------------------------------------
    @property
    def active(self) -> bool:
        return self.http is not None and not self._stopped

    def info(self) -> dict:
        now = time.time()
        srv = self.http
        return {
            "active": self.active,
            "url": self.url,
            "expires_in": max(0, int(self.expires_at - now)) if self.expires_at else 0,
            "connected": bool(srv and srv.session is not None),
        }

    def start(self) -> dict:
        with self._lock:
            if self.active:
                return self.info()
            try:
                httpd = _Server(("127.0.0.1", self.cfg.port), self.cfg, self.factory)
            except OSError:
                httpd = _Server(("127.0.0.1", 0), self.cfg, self.factory)  # port pris : on en choisit un libre
            self.http = httpd
            self._stopped = False
            port = httpd.server_address[1]
            self.thread = threading.Thread(target=httpd.serve_forever,
                                           kwargs={"poll_interval": 0.2}, daemon=True)
            self.thread.start()
            self.started = time.time()
            self.expires_at = self.started + self.cfg.token_ttl_min * 60
            try:
                base = self._start_tunnel(port) if self.use_tunnel else f"http://127.0.0.1:{port}"
                self.url = f"{base}/?t={httpd.token}"
                if self.use_tunnel and not self._verify_tunnel(base, httpd.token):
                    # DNS du tunnel rapide parfois en retard : l'URL reste la bonne.
                    log.warning("tunnel pas encore joignable depuis cette machine : %s", base)
            except Exception:
                self._kill_tunnel()
                self._shutdown_http()
                self._stopped = True
                raise
            self._timer = threading.Timer(self.cfg.token_ttl_min * 60, self.stop, args=("lien expiré",))
            self._timer.daemon = True
            self._timer.start()
            log.info("accès distant prêt sur le port %d", port)
            return self.info()

    def stop(self, reason: str = "arrêt demandé") -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            if self._timer:
                self._timer.cancel()
                self._timer = None
            srv = self.http
            if srv is not None and srv.session is not None:
                try:
                    srv.session.stop(reason)
                except Exception:  # noqa: BLE001
                    log.exception("arrêt de la session distante")
            self._shutdown_http()
            self._kill_tunnel()
            self.url = ""
            self.expires_at = 0.0
            log.info("accès distant arrêté (%s)", reason)
        if self._notify:
            self._notify("🌐 WhisperFlow distant", f"Accès arrêté : {reason}")

    def _shutdown_http(self) -> None:
        srv, self.http = self.http, None
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                srv.server_close()
            except Exception:  # noqa: BLE001
                pass

    # ---- tunnel ----------------------------------------------------------
    def _tunnel_binary(self) -> str:
        found = shutil.which("cloudflared")
        if found:
            return found
        local = Path.home() / ".local" / "bin" / "cloudflared"
        if local.exists() and local.stat().st_mode & 0o111:
            return str(local)
        raise RuntimeError("cloudflared absent : sudo pacman -S cloudflared "
                           "(ou place le binaire dans ~/.local/bin)")

    def _start_tunnel(self, port: int) -> str:
        exe = self._tunnel_binary()
        self.tunnel_proc = subprocess.Popen(
            [exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate", "--loglevel", "info"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1,
            start_new_session=True,
        )
        found: dict[str, str] = {}
        ready = threading.Event()

        def read() -> None:
            for line in self.tunnel_proc.stderr or []:
                line = line.strip()
                if not line:
                    continue
                self.tunnel_lines.append(line)
                del self.tunnel_lines[:-40]
                url = quick_tunnel_url(line)
                if url and not found:
                    found["url"] = url
                    ready.set()
            ready.set()

        threading.Thread(target=read, daemon=True).start()
        if not ready.wait(30) or not found:
            tail = " | ".join(self.tunnel_lines[-4:])
            self._kill_tunnel()
            raise RuntimeError(f"cloudflared n'a pas fourni d'URL publique. {tail}".strip())
        return found["url"]

    def _verify_tunnel(self, base: str, token: str) -> bool:
        """Le tunnel joint-il ce serveur ? Un échec DNS transitoire ne doit pas bloquer."""
        last = "aucune réponse"
        time.sleep(2)  # le DNS du tunnel rapide peut mettre quelques secondes
        for _ in range(5):
            try:
                with urllib.request.urlopen(f"{base}/health?t={token}", timeout=4) as r:
                    if r.status == 200 and r.read().strip() == b"ok":
                        return True
                    last = f"HTTP {r.status}"
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
            time.sleep(2)
        log.info("vérification du tunnel impossible (%s)", last)
        return False

    def _kill_tunnel(self) -> None:
        proc, self.tunnel_proc = self.tunnel_proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        except OSError:
            pass
