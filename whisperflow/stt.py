"""Transcription via l'API Groq (compatible OpenAI), bibliothèque standard uniquement.

Doc vérifiée le 2026-10-26 : POST https://api.groq.com/openai/v1/audio/transcriptions,
multipart (file, model, language, prompt, response_format, temperature),
modèles whisper-large-v3-turbo et whisper-large-v3, 25 Mo max en offre gratuite.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Protocol

from .config import STTConfig, redact

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MAX_BYTES_FREE = 25 * 1024 * 1024


class STTError(RuntimeError):
    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


class Transcriber(Protocol):
    def transcribe(self, wav: Path) -> str: ...


def build_multipart(fields: dict[str, str], file_field: str, filename: str, payload: bytes,
                    content_type: str = "audio/wav") -> tuple[bytes, str]:
    boundary = "wf-" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n").encode()
    out += payload + f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class GroqTranscriber:
    def __init__(self, cfg: STTConfig, api_key: str, url: str = GROQ_URL):
        self.cfg, self.api_key, self.url = cfg, api_key, url

    def _once(self, body: bytes, ctype: str) -> str:
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": ctype,
            "User-Agent": "whisperflow/0.1",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = redact(e.read().decode("utf-8", "replace")[:300], self.api_key)
            retry = e.code == 429 or e.code >= 500
            msg = {401: "Clé Groq refusée (401)", 413: "Audio trop gros (413)",
                   429: "Limite Groq atteinte (429)"}.get(e.code, f"Erreur Groq {e.code}")
            raise STTError(f"{msg} : {detail}", retryable=retry) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise STTError(f"Réseau indisponible : {e}", retryable=True) from None
        text = data.get("text")
        if not isinstance(text, str):
            raise STTError("Réponse Groq inattendue")
        return text

    def transcribe(self, wav: Path) -> str:
        payload = wav.read_bytes()
        if len(payload) > MAX_BYTES_FREE:
            raise STTError("Enregistrement > 25 Mo : trop long pour l'offre gratuite Groq")
        fields = {"model": self.cfg.model, "response_format": "json", "temperature": "0"}
        if self.cfg.language:
            fields["language"] = self.cfg.language
        if self.cfg.prompt:
            fields["prompt"] = self.cfg.prompt
        body, ctype = build_multipart(fields, "file", wav.name, payload)
        delay = 0.5
        for attempt in range(self.cfg.retries + 1):
            try:
                return self._once(body, ctype)
            except STTError as e:
                if not e.retryable or attempt == self.cfg.retries:
                    raise
                time.sleep(delay)
                delay *= 2
        raise STTError("inaccessible")
