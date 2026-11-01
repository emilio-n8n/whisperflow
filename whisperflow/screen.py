"""Écran : capture (grim), OCR (tesseract), géométrie Hyprland, pointeur (hyprctl + ydotool), overlay.

Principe repris de Bluey : on ne vise jamais une coordonnée inventée par un modèle.
Une cible = une ligne OCR citée, retrouvée puis REVÉRIFIÉE sur une nouvelle capture
juste avant d'agir. Le texte lu à l'écran est une donnée, jamais une instruction.
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .actions import Result, norm

RISKY_WORDS = {"supprimer", "effacer", "delete", "remove", "envoyer", "send", "payer", "pay", "acheter", "buy",
               "commander", "order", "publier", "publish", "post", "valider", "confirmer", "confirm", "submit",
               "soumettre", "virement", "transferer", "formater", "desinstaller", "uninstall", "vider"}

ALLOWED_KEYS = {"enter", "return", "tab", "escape", "esc", "backspace", "delete", "up", "down", "left", "right",
                "home", "end", "page_up", "page_down", "space", "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z",
                "ctrl+shift+z", "ctrl+f", "ctrl+l", "ctrl+t", "ctrl+w", "ctrl+s", "ctrl+tab", "ctrl+shift+tab",
                "alt+left", "alt+right", "f5", "ctrl+r", "ctrl+plus", "ctrl+minus", "ctrl+0"}
KEYSYM = {"enter": "Return", "return": "Return", "tab": "Tab", "escape": "Escape", "esc": "Escape",
          "backspace": "BackSpace", "delete": "Delete", "up": "Up", "down": "Down", "left": "Left",
          "right": "Right", "home": "Home", "end": "End", "page_up": "Prior", "page_down": "Next",
          "space": "space", "plus": "plus", "minus": "minus", "f5": "F5"}


@dataclass
class Monitor:
    name: str
    x: int
    y: int
    width: int      # pixels physiques
    height: int
    scale: float

    def to_logical(self, px: float, py: float) -> tuple[int, int]:
        """Pixel de la capture (physique) -> coordonnée globale Hyprland (logique)."""
        return int(round(self.x + px / self.scale)), int(round(self.y + py / self.scale))


@dataclass
class Box:
    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float

    @property
    def center(self) -> tuple[float, float]:
        return self.left + self.width / 2, self.top + self.height / 2

    def as_dict(self, i: int) -> dict:
        return {"id": i, "text": self.text, "x": self.left, "y": self.top, "w": self.width, "h": self.height}


def parse_tesseract_tsv(tsv: str, min_conf: float = 40) -> list[Box]:
    """Regroupe les mots TSV de tesseract en lignes (bloc, paragraphe, ligne)."""
    lines: dict[tuple, list] = {}
    rows = tsv.strip().splitlines()
    if not rows:
        return []
    header = rows[0].split("\t")
    idx = {h: i for i, h in enumerate(header)}
    for r in rows[1:]:
        c = r.split("\t")
        if len(c) < len(header) or c[idx["level"]] != "5":
            continue
        word, conf = c[idx["text"]].strip(), float(c[idx["conf"]])
        if not word or conf < min_conf:
            continue
        key = (c[idx["page_num"]], c[idx["block_num"]], c[idx["par_num"]], c[idx["line_num"]])
        lines.setdefault(key, []).append((int(c[idx["left"]]), int(c[idx["top"]]), int(c[idx["width"]]),
                                          int(c[idx["height"]]), word, conf))
    out = []
    for words in lines.values():
        words.sort()
        l = min(w[0] for w in words)
        t = min(w[1] for w in words)
        r = max(w[0] + w[2] for w in words)
        b = max(w[1] + w[3] for w in words)
        out.append(Box(" ".join(w[4] for w in words), l, t, r - l, b - t, sum(w[5] for w in words) / len(words)))
    out.sort(key=lambda bx: (bx.top, bx.left))
    return out


def match_target(boxes: list[Box], target: str, threshold: float = 0.8) -> tuple[Box | None, list[Box]]:
    """Ligne OCR la plus proche de la cible. Renvoie (meilleure, candidates ambiguës)."""
    t = norm(target)
    if not t:
        return None, []
    scored = []
    for b in boxes:
        n = norm(b.text)
        if not n:
            continue
        if n == t:
            s = 1.0
        elif t in n.split() or (len(t) > 3 and t in n):
            s = 0.9 - min(0.2, (len(n) - len(t)) / 200)
        else:
            s = difflib.SequenceMatcher(None, t, n).ratio()
        if s >= threshold:
            scored.append((s, b))
    scored.sort(key=lambda sb: -sb[0])
    if not scored:
        return None, []
    best = scored[0]
    ties = [b for s, b in scored if best[0] - s < 0.03]
    return (best[1] if len(ties) == 1 else None), ties


class Screen:
    def __init__(self, run=subprocess.run, which=shutil.which, lang: str = "fra+eng"):
        self._run, self._which, self.lang = run, which, lang

    def monitor(self) -> Monitor:
        r = self._run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=5)
        mons = json.loads(r.stdout or "[]")
        m = next((m for m in mons if m.get("focused")), mons[0] if mons else None)
        if not m:
            raise RuntimeError("Aucun écran trouvé (hyprctl)")
        return Monitor(m["name"], m["x"], m["y"], m["width"], m["height"], float(m.get("scale", 1)))

    def capture(self, mon: Monitor, fmt: str = "png", scale: float = 1.0, quality: int = 70) -> bytes:
        cmd = ["grim", "-o", mon.name, "-t", fmt]
        if fmt == "jpeg":
            cmd += ["-q", str(quality)]
        if scale != 1.0:
            cmd += ["-s", f"{scale:.3f}"]
        r = self._run(cmd + ["-"], capture_output=True, timeout=10)
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError("Capture impossible (grim)")
        return r.stdout

    def frame_jpeg(self, max_width: int = 1280) -> bytes:
        mon = self.monitor()
        return self.capture(mon, "jpeg", min(1.0, max_width / mon.width))

    def ocr(self, png: bytes) -> list[Box]:
        if not self._which("tesseract"):
            raise RuntimeError("tesseract absent : sudo pacman -S tesseract tesseract-data-fra tesseract-data-eng")
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            f.write(png)
            f.flush()
            r = self._run(["tesseract", f.name, "stdout", "-l", self.lang, "--psm", "11", "tsv"],
                          capture_output=True, text=True, timeout=30)
        return parse_tesseract_tsv(r.stdout or "")

    def read(self) -> tuple[Monitor, list[Box]]:
        mon = self.monitor()
        return mon, self.ocr(self.capture(mon))

    # ---- pointeur / clavier ---------------------------------------------
    def move(self, x: int, y: int) -> None:
        self._run(["hyprctl", "dispatch", "movecursor", str(x), str(y)], capture_output=True, timeout=5)

    def click(self, x: int, y: int, double: bool = False) -> None:
        if not self._which("ydotool"):
            raise RuntimeError("ydotool absent : sudo pacman -S ydotool && systemctl --user enable --now ydotool")
        self.move(x, y)
        time.sleep(0.05)
        args = ["ydotool", "click"] + (["--repeat", "2"] if double else []) + ["0xC0"]
        r = self._run(args, capture_output=True, timeout=5)
        if r.returncode != 0:
            raise RuntimeError("ydotool click a échoué (ydotoold lancé ?)")

    def scroll(self, direction: str, amount: int = 3) -> None:
        if not self._which("ydotool"):
            raise RuntimeError("ydotool absent")
        dy = {"down": amount, "up": -amount}.get(direction)
        dx = {"right": amount, "left": -amount}.get(direction, 0)
        if dy is None and not dx:
            raise RuntimeError("direction : up/down/left/right")
        self._run(["ydotool", "mousemove", "-w", "-x", str(dx), "-y", str(dy or 0)], capture_output=True, timeout=5)

    def keys(self, combo: str) -> None:
        c = combo.strip().lower().replace(" ", "")
        if c not in ALLOWED_KEYS:
            raise RuntimeError(f"Raccourci clavier non autorisé : {combo}")
        parts = c.split("+")
        mods, key = parts[:-1], parts[-1]
        args = ["wtype"]
        for m in mods:
            args += ["-M", m]
        args += ["-P", KEYSYM.get(key, key), "-p", KEYSYM.get(key, key)]
        for m in reversed(mods):
            args += ["-m", m]
        self._run(args, capture_output=True, timeout=5)


class Annotator:
    """« Montre-moi où » : surligne sans cliquer. Overlay GTK4 layer-shell, sinon le pointeur se déplace."""

    def __init__(self, screen: Screen, spawn=None, overlay_available=None):
        self.screen = screen
        self._spawn = spawn or (lambda argv, env: subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL,
                                                                   stderr=subprocess.DEVNULL, start_new_session=True))
        self._overlay = overlay_available if overlay_available is not None else overlay_supported()

    def show(self, target: str, seconds: float = 4.0) -> Result:
        mon, boxes = self.screen.read()
        best, ties = match_target(boxes, target)
        if not best:
            if ties:
                return Result(False, f"Plusieurs « {target} » à l'écran, précise lequel",
                              {"candidates": [b.as_dict(i) for i, b in enumerate(ties[:5])]})
            return Result(False, f"Je ne vois pas « {target} » à l'écran")
        x, y = mon.to_logical(best.left, best.top)
        w, h = int(best.width / mon.scale), int(best.height / mon.scale)
        if self._overlay:
            env = dict(os.environ)
            lib = overlay_lib()
            if lib:
                env["LD_PRELOAD"] = lib
            self._spawn([sys.executable, "-m", "whisperflow.overlay", str(x - mon.x), str(y - mon.y), str(w), str(h),
                         best.text[:60], str(seconds), mon.name], env)
        else:
            self.screen.move(x + w // 2, y + h // 2)
        return Result(True, f"C'est ici : « {best.text} »", {"x": x, "y": y, "w": w, "h": h})


def overlay_lib() -> str | None:
    for p in ("/usr/lib/libgtk4-layer-shell.so", "/usr/lib64/libgtk4-layer-shell.so"):
        if Path(p).exists():
            return p
    return None


def overlay_supported() -> bool:
    if not overlay_lib():
        return False
    try:
        import gi  # noqa: F401
        return True
    except ImportError:
        return False
