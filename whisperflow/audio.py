"""Capture micro via PipeWire (pw-record) ou ALSA (arecord), sans dépendance Python.

Le fichier WAV est écrit par l'outil externe. À l'arrêt on envoie SIGINT pour
qu'il ferme proprement ; si l'en-tête est quand même incomplet (arrêt brutal),
`repair_wav` recalcule les tailles.
"""
from __future__ import annotations

import array
import os
import shutil
import signal
import struct
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from .config import AudioConfig


class AudioError(RuntimeError):
    pass


def pick_backend(cfg: AudioConfig) -> str:
    if cfg.backend != "auto":
        if not shutil.which(cfg.backend):
            raise AudioError(f"{cfg.backend} introuvable")
        return cfg.backend
    for b in ("pw-record", "arecord"):
        if shutil.which(b):
            return b
    raise AudioError("Ni pw-record (pipewire) ni arecord (alsa-utils) n'est installé")


def build_command(backend: str, cfg: AudioConfig, out: Path) -> list[str]:
    if backend == "pw-record":
        cmd = ["pw-record", "--rate", str(cfg.sample_rate), "--channels", "1", "--format", "s16"]
        if cfg.device:
            cmd += ["--target", cfg.device]
        return cmd + [str(out)]
    if backend == "arecord":
        cmd = ["arecord", "-q", "-f", "S16_LE", "-r", str(cfg.sample_rate), "-c", "1", "-t", "wav"]
        if cfg.device:
            cmd += ["-D", cfg.device]
        return cmd + [str(out)]
    raise AudioError(f"backend inconnu : {backend}")


@dataclass
class Recording:
    path: Path
    duration_s: float
    rms: int


class Recorder:
    def __init__(self, cfg: AudioConfig, workdir: Path):
        self.cfg = cfg
        self.workdir = workdir
        self.proc: subprocess.Popen | None = None
        self.path: Path | None = None
        self.started = 0.0

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.active:
            return
        backend = pick_backend(self.cfg)
        self.workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.workdir / f"rec-{int(time.time() * 1000)}.wav"
        self.proc = subprocess.Popen(
            build_command(backend, self.cfg, self.path),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.started = time.monotonic()
        time.sleep(0.05)
        if self.proc.poll() is not None:
            err = (self.proc.stderr.read() or b"").decode(errors="replace").strip()
            self.proc = None
            raise AudioError(f"Le micro n'a pas démarré : {err or 'erreur inconnue'}")

    def _halt(self) -> None:
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    def stop(self) -> Recording:
        if not self.path:
            raise AudioError("Aucun enregistrement en cours")
        self._halt()
        path, self.path = self.path, None
        if not path.exists():
            raise AudioError("Aucun son capturé (fichier absent)")
        repair_wav(path)
        dur, rms = wav_stats(path)
        return Recording(path, dur, rms)

    def cancel(self) -> None:
        self._halt()
        if self.path and self.path.exists():
            self.path.unlink()
        self.path = None

    def elapsed(self) -> float:
        return time.monotonic() - self.started if self.active else 0.0


def repair_wav(path: Path) -> None:
    """Corrige les tailles RIFF/data d'un WAV PCM dont l'écriture a été interrompue."""
    data = bytearray(path.read_bytes())
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return
    # chercher le chunk "data"
    pos = 12
    while pos + 8 <= len(data):
        cid = bytes(data[pos:pos + 4])
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"data":
            real = len(data) - (pos + 8)
            if size != real:
                struct.pack_into("<I", data, pos + 4, real)
                struct.pack_into("<I", data, 4, len(data) - 8)
                path.write_bytes(bytes(data))
            return
        pos += 8 + size + (size & 1)


def wav_stats(path: Path) -> tuple[float, int]:
    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
        rate, width = w.getframerate(), w.getsampwidth()
        n = w.getnframes()
    if n == 0 or width != 2:
        return (n / rate if rate else 0.0, 0)
    samples = array.array("h", frames)
    step = max(1, len(samples) // 20000)  # échantillonnage : rapide sur un vieux CPU
    sub = samples[::step]
    rms = int((sum(s * s for s in sub) / len(sub)) ** 0.5)
    return n / rate, rms


def cleanup_old(workdir: Path, keep_seconds: int = 3600) -> None:
    if not workdir.exists():
        return
    now = time.time()
    for f in workdir.glob("rec-*.wav"):
        try:
            if now - f.stat().st_mtime > keep_seconds:
                os.unlink(f)
        except OSError:
            pass
