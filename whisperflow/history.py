"""Historique local (JSONL, permissions 600) : texte dicté et résultat de l'insertion.

Sert de filet de sécurité : si l'insertion échoue, le texte n'est pas perdu.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class History:
    def __init__(self, path: Path, enabled: bool = True, max_entries: int = 500):
        self.path, self.enabled, self.max_entries = path, enabled, max_entries

    def add(self, **entry) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._trim()

    def _trim(self) -> None:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if len(lines) > self.max_entries * 1.2:
            self.path.write_text("\n".join(lines[-self.max_entries:]) + "\n", encoding="utf-8")

    def last(self, n: int = 10) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines()[-n:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out
