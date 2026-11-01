"""Insertion du texte dans le champ actif sous Wayland/Hyprland.

1. wtype (protocole virtual-keyboard, géré par Hyprland) : tape le texte, accents compris.
2. Repli presse-papiers : wl-copy + raccourci coller, puis restauration du presse-papiers
   d'origine (idée reprise d'Atticus : on emprunte le presse-papiers, on le rend).
En cas d'échec, l'appelant garde le texte dans l'historique : jamais de double frappe.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from typing import Protocol

from .config import InjectConfig


class InjectError(RuntimeError):
    pass


class Injector(Protocol):
    def insert(self, text: str) -> str: ...


def _run(cmd: list[str], input_: bytes | None = None, timeout: float = 5) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input_, capture_output=True, timeout=timeout)


def _shortcut_args(shortcut: str) -> list[str]:
    parts = shortcut.lower().split("+")
    key = parts[-1]
    args: list[str] = []
    for m in parts[:-1]:
        args += ["-M", m]
    args += ["-P", key, "-p", key]
    for m in reversed(parts[:-1]):
        args += ["-m", m]
    return args


class WaylandInjector:
    def __init__(self, cfg: InjectConfig, run=_run, which=shutil.which):
        self.cfg, self._run, self._which = cfg, run, which

    def _wtype(self, text: str) -> None:
        # "wtype -" lit le texte sur l'entrée standard : pas de souci avec un texte
        # commençant par "-", ni de limite de longueur d'argument.
        r = self._run(["wtype", "-"], input_=text.encode("utf-8"), timeout=max(5, len(text) / 50))
        if r.returncode != 0:
            raise InjectError(f"wtype : {r.stderr.decode(errors='replace').strip()}")

    def _clipboard(self, text: str) -> None:
        if not (self._which("wl-copy") and self._which("wl-paste") and self._which("wtype")):
            raise InjectError("wl-clipboard ou wtype manquant")
        old = None
        if self.cfg.restore_clipboard:
            r = self._run(["wl-paste", "--no-newline"])
            old = r.stdout if r.returncode == 0 else None
        if self._run(["wl-copy"], input_=text.encode()).returncode != 0:
            raise InjectError("wl-copy a échoué")
        time.sleep(0.05)
        r = self._run(["wtype", *_shortcut_args(self.cfg.paste_shortcut)])
        if r.returncode != 0:
            raise InjectError("raccourci coller refusé")
        if old is not None:
            time.sleep(0.3)  # laisser l'appli lire le presse-papiers avant de le rendre
            self._run(["wl-copy"], input_=old)

    def insert(self, text: str) -> str:
        m = self.cfg.method
        if m == "none":
            return "none"
        if m in ("auto", "wtype") and self._which("wtype"):
            try:
                self._wtype(text)
                return "wtype"
            except (InjectError, subprocess.TimeoutExpired):
                if m == "wtype":
                    raise
        if m in ("auto", "clipboard"):
            try:
                self._clipboard(text)
                return "clipboard"
            except subprocess.TimeoutExpired as e:
                raise InjectError("presse-papiers bloqué") from e
        raise InjectError("Aucune méthode d'insertion disponible (installe wtype)")
