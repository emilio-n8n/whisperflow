"""Service WhisperFlow : machine d'états de la dictée, piloté par un socket Unix.

Protocole : une ligne JSON par requête, une ligne JSON par réponse.
  {"cmd": "toggle"|"start"|"stop"|"cancel"|"status"|"ping"|"quit"}

États : idle -> recording -> transcribing -> inserting -> idle
        (error -> idle après affichage). Annuler fonctionne à toute étape :
        un jeton de session invalide le résultat d'une transcription en vol.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path

from .audio import AudioError, Recorder, cleanup_old
from .config import Config
from .history import History
from .inject import InjectError, Injector
from .stt import STTError, Transcriber
from .textproc import clean
from .ui import Indicator

log = logging.getLogger("whisperflow")


class Dictation:
    def __init__(self, cfg: Config, recorder: Recorder, transcriber: Transcriber,
                 injector: Injector, indicator: Indicator, history: History, commander=None):
        self.commander = commander  # callable(texte) -> dict, pour le mode commande
        self.mode = "dictate"
        self.cfg, self.rec, self.stt = cfg, recorder, transcriber
        self.inj, self.ui, self.hist = injector, indicator, history
        self.state = "idle"
        self.session = 0
        self.lock = threading.RLock()
        self.last: dict = {}
        self._watchdog: threading.Timer | None = None
        self.ui.set("idle")

    # ---- commandes -------------------------------------------------------
    def toggle(self, mode: str = "dictate") -> dict:
        with self.lock:
            if self.state == "recording":
                return self.stop()
            if self.state == "idle":
                return self.start(mode)
            return {"ok": False, "state": self.state, "msg": "occupé"}

    def start(self, mode: str = "dictate") -> dict:
        with self.lock:
            if self.state != "idle":
                return {"ok": False, "state": self.state, "msg": "occupé"}
            if mode == "command" and not self.commander:
                return {"ok": False, "state": self.state, "msg": "mode commande indisponible"}
            self.mode = mode
            try:
                self.rec.start()
            except AudioError as e:
                return self._fail(str(e))
            self.session += 1
            self.state = "recording"
            self._arm_watchdog(self.session)
            self.ui.set("recording", "Commande ?" if mode == "command" else "")
            log.info("enregistrement démarré (session %d)", self.session)
            return {"ok": True, "state": self.state}

    def stop(self) -> dict:
        with self.lock:
            if self.state != "recording":
                return {"ok": False, "state": self.state, "msg": "pas d'enregistrement"}
            self._disarm_watchdog()
            t_stop = time.monotonic()
            try:
                recording = self.rec.stop()
            except AudioError as e:
                return self._fail(str(e))
            if recording.duration_s < self.cfg.audio.min_seconds or recording.rms < self.cfg.audio.silence_rms:
                recording.path.unlink(missing_ok=True)
                self.state = "idle"
                self.ui.set("idle", "Rien entendu")
                return {"ok": True, "state": "idle", "msg": "silence"}
            self.state = "transcribing"
            self.ui.set("transcribing")
            sid = self.session
        threading.Thread(target=self._process, args=(sid, recording, t_stop, self.mode), daemon=True).start()
        return {"ok": True, "state": "transcribing"}

    def cancel(self) -> dict:
        with self.lock:
            self._disarm_watchdog()
            was = self.state
            if was == "recording":
                self.rec.cancel()
            self.session += 1  # invalide un traitement en vol
            self.state = "idle"
            self.ui.set("idle", "Annulé" if was != "idle" else "")
            return {"ok": True, "state": "idle", "cancelled": was}

    def status(self) -> dict:
        with self.lock:
            return {"ok": True, "state": self.state, "elapsed": round(self.rec.elapsed(), 1), "last": self.last}

    # ---- traitement ------------------------------------------------------
    def _process(self, sid: int, recording, t_stop: float, mode: str = "dictate") -> None:
        text, err = "", ""
        try:
            raw = self.stt.transcribe(recording.path)
            text = clean(raw, self.cfg.inject.trailing_space, self.cfg.inject.newlines)
        except STTError as e:
            err = str(e)
        finally:
            recording.path.unlink(missing_ok=True)
        t_stt = time.monotonic()
        with self.lock:
            if sid != self.session:
                log.info("résultat ignoré : session annulée")
                return
            if err:
                self._fail(err)
                return
            if not text:
                self.state = "idle"
                self.ui.set("idle", "Rien de compris")
                return
            log.info("texte prêt : %d caractères, %d retour(s) à la ligne",
                     len(text), text.count("\n"))
            if mode == "command":
                self.state = "acting"
                self.ui.set("inserting", text.strip())
        if mode == "command":
            res = self.commander(text.strip())
            with self.lock:
                if sid == self.session:
                    self.last = {"command": text.strip(), **res}
                    self.state = "idle"
                    self.ui.set("idle" if res.get("ok") else "error", res.get("msg", ""))
            return
        with self.lock:
            self.state = "inserting"
            self.ui.set("inserting")
        method, ok, ierr = "", False, ""
        try:
            method = self.inj.insert(text)
            ok = method != "none"
        except InjectError as e:
            ierr = str(e)
        t_done = time.monotonic()
        self.hist.add(text=text, inserted=ok, method=method, error=ierr,
                      audio_s=round(recording.duration_s, 2),
                      stt_ms=int((t_stt - t_stop) * 1000), total_ms=int((t_done - t_stop) * 1000))
        with self.lock:
            if sid != self.session:
                return
            self.last = {"chars": len(text), "inserted": ok, "total_ms": int((t_done - t_stop) * 1000)}
            self.state = "idle"
            if ok:
                self.ui.set("idle")
            else:
                self.ui.set("error", "Texte non inséré, gardé dans l'historique (whisperflow last)")
                self.state = "idle"

    def _fail(self, msg: str) -> dict:
        log.error(msg)
        self.state = "idle"
        self.ui.set("error", msg)
        return {"ok": False, "state": "idle", "msg": msg}

    def _arm_watchdog(self, sid: int) -> None:
        def fire():
            with self.lock:
                if self.session == sid and self.state == "recording":
                    log.warning("durée max atteinte, arrêt automatique")
                    self.stop()
        self._watchdog = threading.Timer(self.cfg.audio.max_seconds, fire)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _disarm_watchdog(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None


class Server:
    def __init__(self, dictation: Dictation, sock_path: Path, brain=None):
        self.d, self.sock_path, self.brain = dictation, sock_path, brain
        self._stop = threading.Event()

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        mode = req.get("mode", "dictate")
        fn = {"toggle": lambda: self.d.toggle(mode), "start": lambda: self.d.start(mode), "stop": self.d.stop,
              "cancel": self.d.cancel, "status": self._status}.get(cmd)
        if self.brain and cmd in self.brain.commands:
            return self.brain.commands[cmd](req)
        if cmd == "panic":
            out = self.d.cancel()
            if self.brain:
                out["brain"] = self.brain.panic()
            return out
        if cmd == "ping":
            return {"ok": True, "pong": True}
        if cmd == "quit":
            self._stop.set()
            return {"ok": True}
        if not fn:
            return {"ok": False, "msg": f"commande inconnue : {cmd}"}
        return fn()

    def _status(self) -> dict:
        st = self.d.status()
        if self.brain:
            st.update(self.brain.status())
        return st

    def serve(self) -> None:
        if self.sock_path.exists():
            # un autre daemon vivant ?
            try:
                send(self.sock_path, {"cmd": "ping"}, timeout=0.5)
                raise RuntimeError("WhisperFlow tourne déjà")
            except (ConnectionRefusedError, FileNotFoundError, socket.timeout, OSError) as e:
                if isinstance(e, RuntimeError):
                    raise
                self.sock_path.unlink()
        self.sock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.sock_path))
        os.chmod(self.sock_path, 0o600)
        srv.listen(8)
        srv.settimeout(0.5)
        log.info("à l'écoute sur %s", self.sock_path)
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                with conn:
                    conn.settimeout(2)
                    try:
                        line = conn.makefile("r", encoding="utf-8").readline()
                        resp = self.handle(json.loads(line or "{}"))
                    except (ValueError, OSError) as e:
                        resp = {"ok": False, "msg": f"requête invalide : {e}"}
                    try:
                        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                    except OSError:
                        pass
        finally:
            srv.close()
            self.sock_path.unlink(missing_ok=True)
            self.d.cancel()
            if self.brain:
                self.brain.panic()
            self.d.ui.set("off")


def send(sock_path: Path, req: dict, timeout: float = 3.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sock_path))
        s.sendall((json.dumps(req) + "\n").encode())
        data = s.makefile("r", encoding="utf-8").readline()
    return json.loads(data)


def run(cfg: Config, transcriber: Transcriber, injector: Injector, indicator: Indicator,
        history: History, rt: Path, brain=None) -> None:
    workdir = rt / "audio"
    cleanup_old(workdir, 0)
    d = Dictation(cfg, Recorder(cfg.audio, workdir), transcriber, injector, indicator, history,
                  commander=brain.handle_text if brain else None)
    Server(d, rt / "whisperflow.sock", brain).serve()
