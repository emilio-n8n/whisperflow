"""Assemble les niveaux 2 et 3 : routeur + catalogue, agents OpenCode, Gemini Live, annotations."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from .actions import Confirmer, Executor, Result
from .agents import AgentManager
from .config import Config, SecretError, load_secret
from .router import JevClient, Router, Shortcuts
from .screen import Annotator, Screen

log = logging.getLogger("whisperflow.brain")


class Brain:
    def __init__(self, cfg: Config, injector, notify, state_dir: Path, history=None, confirmer=None,
                 screen=None, agent_factory=None, live_factory=None, secret=load_secret):
        self.cfg, self.notify, self.history = cfg, notify, history
        self.confirmer = confirmer or Confirmer()
        self.screen = screen or Screen()
        self.annotator = Annotator(self.screen)
        self.shortcuts = Shortcuts(state_dir / "shortcuts.json")
        agent_confirm = confirmer or Confirmer(cfg.agent.permission_timeout_s)
        self.agents = (agent_factory or AgentManager)(cfg.agent, state_dir / "acp_sessions.json", notify,
                                                      agent_confirm)
        self.injector = injector
        self.executor = Executor(cfg.actions, notify, self.confirmer, injector=injector, agents=self.agents,
                                 live=self.live_ctl, annotator=self.annotator, shortcuts=self.shortcuts)
        jev = None
        try:
            jev = JevClient(cfg.jev, secret("OPENCODE_API_KEY"))
        except SecretError:
            log.info("pas de clé OpenCode : règles locales seulement")
        self.router = Router(self.executor.describe(), list(cfg.actions.apps), jev, cfg.jev, self.shortcuts)
        self._secret = secret
        self._live_factory = live_factory
        self.live = None
        self._live_lock = threading.Lock()
        self.remote = None
        self._remote_lock = threading.Lock()
        self.commands = {
            "run": lambda r: self.handle_text(r.get("text", "")),
            "route": lambda r: self._route_only(r.get("text", "")),
            "live": lambda r: self._res(self.live_ctl(r.get("op", "toggle"))),
            "remote": lambda r: self._res(self.remote_ctl(r.get("op", "toggle"))),
            "agent": self._agent_cmd,
            "shortcut": self._shortcut_cmd,
            "show": lambda r: self._res(self.annotator.show(r.get("text", ""))),
        }

    @staticmethod
    def _res(r: Result) -> dict:
        return {"ok": r.ok, "msg": r.message, **r.data}

    # ---- commandes vocales ----------------------------------------------
    def _route_only(self, text: str) -> dict:
        r = self.router.route(text)
        return {"ok": r.action is not None, "action": r.action, "params": r.params, "source": r.source,
                "confidence": round(r.confidence, 2), "alternatives": r.alternatives, "msg": r.message}

    def run_command(self, text: str) -> Result:
        route = self.router.route(text)
        if not route.action:
            return Result(False, route.message or "Commande non reconnue", {"alternatives": route.alternatives})
        res = self.executor.execute(route.action, route.params)
        if self.history:
            self.history.add(kind="command", text=text, action=route.action, params=route.params,
                             source=route.source, confidence=round(route.confidence, 2), ok=res.ok, result=res.message)
        return res

    def handle_text(self, text: str) -> dict:
        res = self.run_command(text)
        self.notify("WhisperFlow", res.message)
        return self._res(res) | {"command": text}

    # ---- Gemini Live -----------------------------------------------------
    def live_ctl(self, op: str = "toggle") -> Result:
        with self._live_lock:
            active = self.live is not None and self.live.active
            if op == "status":
                return Result(True, "Gemini Live actif" if active else "Gemini Live arrêté", {"active": active})
            if op == "stop" or (op == "toggle" and active):
                if active:
                    self.live.stop("arrêt demandé")
                return Result(True, "Gemini Live arrêté")
            if active:
                return Result(True, "Gemini Live déjà actif")
            if self.remote and self.remote.active:
                if self.remote.info().get("connected"):
                    return Result(False, "Le téléphone est connecté : coupe la session distante avant Gemini Live.")
                # Personne au bout du lien : on le ferme pour laisser la place au Live local.
                self.remote.stop("Gemini Live local")
            try:
                key = self._secret("GEMINI_API_KEY")
            except SecretError as e:
                return Result(False, str(e))
            factory = self._live_factory or self._default_live
            try:
                self.live = factory(key)
                self.live.start()
            except Exception as e:  # noqa: BLE001
                self.live = None
                return Result(False, str(e))
            self.notify("🎙 Gemini Live", "Je t'écoute. SUPER+CTRL+G pour arrêter.")
            return Result(True, "Gemini Live démarré")

    def _default_live(self, key: str):
        from .live import LiveSession, LiveTools

        def tools_factory(session):
            return LiveTools(self.cfg.live, self.screen, self.run_command, self.annotator, self.agents,
                             self.injector, self.confirmer, session.send_frame)

        def on_text(who, text):
            if self.history:
                self.history.add(kind="live", who=who, text=text)

        return LiveSession(self.cfg.live, key, tools_factory, self.screen, on_text,
                           lambda reason: self.notify("🎙 Gemini Live terminé", reason))

    # ---- accès distant (iPhone via tunnel Cloudflare) --------------------
    def remote_ctl(self, op: str = "toggle") -> Result:
        with self._remote_lock:
            active = self.remote is not None and self.remote.active
            if op == "status":
                info = self.remote.info() if self.remote else {"active": False, "url": ""}
                return Result(True, "Accès distant actif" if active else "Accès distant arrêté", info)
            if op == "stop" or (op == "toggle" and active):
                if self.remote:
                    self.remote.stop("arrêt demandé")
                return Result(True, "Accès distant arrêté", {"active": False})
            if active:
                return Result(True, "Accès distant déjà actif", self.remote.info())
            if self.live and self.live.active:
                return Result(False, "Gemini Live est actif en local : arrête-le d'abord (SUPER+CTRL+G)")
            try:
                key = self._secret("GEMINI_API_KEY")
            except SecretError as e:
                return Result(False, str(e))
            from .remote import RemoteServer

            try:
                remote = RemoteServer(self.cfg.remote, lambda hooks: self._remote_live(key, hooks), self.notify)
                info = remote.start()
            except Exception as e:  # noqa: BLE001
                return Result(False, f"Accès distant impossible : {e}")
            self.remote = remote
            minutes = max(1, info.get("expires_in", 0) // 60)
            self.notify("🌐 WhisperFlow distant", f"Lien actif {minutes} min : {info['url']}")
            return Result(True, "Lien distant prêt", info)

    def _remote_live(self, key: str, hooks):
        """Session Gemini dont le micro/haut-parleur sont ceux du téléphone."""
        from .live import LiveSession, LiveTools

        if self.live and self.live.active:
            raise RuntimeError("Gemini Live est déjà actif sur le PC : arrête-le d'abord.")

        def tools_factory(session):
            return LiveTools(self.cfg.live, self.screen, self.run_command, self.annotator, self.agents,
                             self.injector, self.confirmer, session.send_frame)

        def on_text(who, text):
            if self.history:
                self.history.add(kind="live", who=who, text=text)
            hooks.text(who, text)

        def on_end(reason):
            self.notify("🎙 Gemini Live terminé", reason)
            hooks.end(reason)

        return LiveSession(self.cfg.live, key, tools_factory, self.screen, on_text, on_end,
                           local_audio=False, on_audio=hooks.audio, on_frame=hooks.frame,
                           on_interrupt=hooks.interrupt)

    # ---- agents / raccourcis --------------------------------------------
    def _agent_cmd(self, req: dict) -> dict:
        op = req.get("op", "list")
        if op == "submit":
            return {"ok": True, "task_id": self.agents.submit(req.get("text", ""), req.get("project", ""))}
        if op == "cancel":
            return {"ok": True, "cancelled": self.agents.cancel(req.get("id"))}
        return {"ok": True, "tasks": self.agents.list()}

    def _shortcut_cmd(self, req: dict) -> dict:
        op = req.get("op", "list")
        if op == "learn":
            steps = []
            for phrase in req.get("steps", []):
                r = self.router.route(phrase)
                if not r.action or r.action == "run_shortcut":
                    return {"ok": False, "msg": f"Étape non comprise : « {phrase} » ({r.message})"}
                steps.append({"action": r.action, "params": r.params, "said": phrase})
            self.shortcuts.save(req["name"], steps, req.get("phrases", []))
            return {"ok": True, "msg": f"Raccourci « {req['name']} » appris", "steps": steps}
        if op == "delete":
            return {"ok": self.shortcuts.delete(req.get("name", ""))}
        return {"ok": True, "shortcuts": self.shortcuts.all()}

    # ---- état / arrêt d'urgence -----------------------------------------
    def status(self) -> dict:
        return {"live": bool(self.live and self.live.active),
                "remote": self.remote.info() if self.remote else {"active": False},
                "agents": [t for t in self.agents.list() if t["status"] == "running"]}

    def panic(self) -> dict:
        """Arrêt d'urgence : coupe Gemini, l'accès distant, les agents et les minuteurs."""
        stopped = {"live": False, "remote": False, "agents": []}
        if self.remote and self.remote.active:
            self.remote.stop("arrêt d'urgence")
            stopped["remote"] = True
        if self.live and self.live.active:
            self.live.stop("arrêt d'urgence")
            stopped["live"] = True
        stopped["agents"] = self.agents.cancel()
        for t in self.executor.timers:
            t.cancel()
        self.notify("⛔ WhisperFlow", "Arrêt d'urgence : tout est stoppé")
        return stopped
