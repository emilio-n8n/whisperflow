"""Client WebSocket minimal (RFC 6455), stdlib uniquement, pour Gemini Live."""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WSError(RuntimeError):
    pass


class WSClosed(WSError):
    def __init__(self, code: int | None, reason: str):
        super().__init__(f"connexion fermée ({code}) {reason}".strip())
        self.code, self.reason = code, reason


def xor_mask(data: bytes, key: bytes) -> bytes:
    n = len(data)
    if not n:
        return b""
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def encode_frame(opcode: int, payload: bytes, mask: bool = True, fin: bool = True) -> bytes:
    head = bytearray([(0x80 if fin else 0) | opcode])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head.append(mbit | n)
    elif n < 65536:
        head.append(mbit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(mbit | 127)
        head += struct.pack("!Q", n)
    if not mask:
        return bytes(head) + payload
    key = os.urandom(4)
    return bytes(head) + key + xor_mask(payload, key)


class WebSocket:
    def __init__(self, url: str, timeout: float = 15, headers: dict | None = None, ssl_context=None):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "wss"):
            raise WSError("URL ws:// ou wss:// attendue")
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == "wss":
            raw = (ssl_context or ssl.create_default_context()).wrap_socket(raw, server_hostname=u.hostname)
        self.sock = raw
        self._wlock = threading.Lock()
        self._buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        lines = [f"GET {path} HTTP/1.1", f"Host: {u.hostname}:{port}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        resp = self._read_until(b"\r\n\r\n").decode("latin-1")
        status = resp.split("\r\n", 1)[0]
        if " 101 " not in status + " ":
            raise WSError(f"Handshake refusé : {status}")
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        hdrs = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in resp.split("\r\n")[1:] if ":" in l}
        if hdrs.get("sec-websocket-accept") != expected:
            raise WSError("Handshake : Sec-WebSocket-Accept invalide")
        self.sock.settimeout(None)
        self.closed = False

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("connexion fermée pendant le handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(marker, 1)
        return head

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise WSClosed(None, "socket fermée")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send(self, opcode: int, payload: bytes) -> None:
        with self._wlock:
            if self.closed:
                raise WSClosed(None, "déjà fermée")
            self.sock.sendall(encode_frame(opcode, payload))

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def send_bytes(self, data: bytes) -> None:
        self._send(OP_BIN, data)

    def recv(self) -> str | bytes:
        """Renvoie le prochain message complet (texte ou binaire). Gère ping/pong/close."""
        parts, first_op = [], None
        while True:
            b1, b2 = self._read_exact(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if b2 & 0x80 else None
            data = self._read_exact(n)
            if mask:
                data = xor_mask(data, mask)
            if op == OP_PING:
                self._send(OP_PONG, data)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else None
                reason = data[2:].decode("utf-8", "replace")
                try:
                    self._send(OP_CLOSE, data[:2])
                except (OSError, WSError):
                    pass
                self.closed = True
                raise WSClosed(code, reason)
            if op != OP_CONT:
                first_op = op
            parts.append(data)
            if fin:
                msg = b"".join(parts)
                return msg.decode("utf-8") if first_op == OP_TEXT else msg

    def close(self, code: int = 1000) -> None:
        if not self.closed:
            try:
                self._send(OP_CLOSE, struct.pack("!H", code))
            except (OSError, WSError):
                pass
            self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
