"""Indicateurs visuels : module Waybar (Omarchy) + notifications (mako via notify-send).

Le daemon écrit l'état dans $XDG_RUNTIME_DIR/whisperflow/state.json puis signale Waybar.
`whisperflow waybar` lit ce fichier et renvoie le JSON attendu par un module custom.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .config import UIConfig

ICONS = {"idle": "󰍬", "recording": "󰍬", "transcribing": "󰔟", "inserting": "󰌌", "error": "󰍭"}
LABELS = {"idle": "Prêt", "recording": "J'écoute…", "transcribing": "Transcription…",
          "inserting": "Insertion…", "error": "Erreur"}


class Indicator:
    def __init__(self, cfg: UIConfig, state_file: Path, run=subprocess.run, which=shutil.which):
        self.cfg, self.state_file, self._run, self._which = cfg, state_file, run, which
        self._notif_id: str | None = None

    def set(self, state: str, detail: str = "") -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"state": state, "detail": detail, "ts": time.time()}))
        os.replace(tmp, self.state_file)
        if self._which("pkill"):
            self._run(["pkill", f"-RTMIN+{self.cfg.waybar_signal}", "waybar"], capture_output=True)
        if self.cfg.notifications and self._which("notify-send"):
            self._notify(state, detail)

    def _notify(self, state: str, detail: str) -> None:
        if state == "idle" and not detail:
            if self._notif_id:  # fermer la bulle "J'écoute"
                self._run(["notify-send", "-r", self._notif_id, "-t", "1", "WhisperFlow", ""],
                          capture_output=True)
                self._notif_id = None
            return
        urgency = "critical" if state == "error" else "low"
        cmd = ["notify-send", "-a", "WhisperFlow", "-u", urgency, "-p",
               "-t", "4000" if state in ("error", "idle") else "0",
               "-h", "string:x-canonical-private-synchronous:whisperflow"]
        if self._notif_id:
            cmd += ["-r", self._notif_id]
        cmd += [f"{ICONS.get(state, '')} {LABELS.get(state, state)}", detail]
        r = self._run(cmd, capture_output=True, text=True)
        out = (r.stdout or "").strip() if hasattr(r, "stdout") else ""
        if out.isdigit():
            self._notif_id = out


def waybar_json(state_file: Path) -> str:
    try:
        st = json.loads(state_file.read_text())
    except (OSError, ValueError):
        st = {"state": "off", "detail": ""}
    s = st.get("state", "off")
    if s == "off":
        return json.dumps({"text": "", "tooltip": "WhisperFlow arrêté", "class": "off"})
    text = ICONS.get(s, "") if s != "idle" else ""
    tip = LABELS.get(s, s) + (f" — {st['detail']}" if st.get("detail") else "")
    return json.dumps({"text": text, "tooltip": tip, "class": s, "alt": s})


def notify(title: str, body: str = "", run=subprocess.run, which=shutil.which) -> None:
    """Notification simple (mako). Le corps est tronqué : jamais de pavé à l'écran."""
    if which("notify-send"):
        run(["notify-send", "-a", "WhisperFlow", title, body[:400]], capture_output=True)
