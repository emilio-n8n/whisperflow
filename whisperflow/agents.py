"""Délégation à OpenCode via l'Agent Client Protocol (ACP v1, JSON-RPC 2.0 ndjson sur stdio).

`opencode acp` est lancé en sous-processus. WhisperFlow est le *client* : il crée des
sessions (persistées par dossier), envoie les tâches, suit les `session/update`,
répond aux `session/request_permission` (lecture/recherche auto, le reste sur
confirmation, refus si pas de réponse) et peut annuler (`session/cancel`).
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import AgentConfig

log = logging.getLogger("whisperflow.agents")


class ACPError(RuntimeError):
    pass


class ACPClient:
    def __init__(self, argv: list[str], cwd: str, on_update: Callable[[dict], None],
                 on_permission: Callable[[dict], dict], popen=subprocess.Popen):
        self.proc = popen(argv + ["--cwd", cwd], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
        self.on_update, self.on_permission = on_update, on_permission
        self._ids = itertools.count(1)
        self._pending: dict[int, dict] = {}
        self._wlock = threading.Lock()
        self.agent_caps: dict = {}
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    # ---- transport -------------------------------------------------------
    def _send(self, msg: dict) -> None:
        with self._wlock:
            if self.proc.poll() is not None:
                raise ACPError("OpenCode s'est arrêté")
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float | None = 60) -> dict:
        rid = next(self._ids)
        slot = {"ev": threading.Event()}
        self._pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not slot["ev"].wait(timeout):
            self._pending.pop(rid, None)
            raise ACPError(f"{method} : pas de réponse")
        if "error" in slot:
            raise ACPError(f"{method} : {slot['error'].get('message', slot['error'])}")
        return slot.get("result") or {}

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log.debug("ligne non JSON ignorée")
                continue
            if "method" in msg and "id" in msg:          # requête de l'agent vers nous
                threading.Thread(target=self._answer, args=(msg,), daemon=True).start()
            elif "method" in msg:                        # notification
                if msg["method"] == "session/update":
                    try:
                        self.on_update(msg.get("params", {}))
                    except Exception:  # noqa: BLE001
                        log.exception("on_update")
            elif "id" in msg:                            # réponse à une de nos requêtes
                slot = self._pending.pop(msg["id"], None)
                if slot is not None:
                    slot.update({k: msg[k] for k in ("result", "error") if k in msg})
                    slot["ev"].set()
        for slot in list(self._pending.values()):        # processus mort : on débloque tout
            slot["error"] = {"message": "OpenCode s'est arrêté"}
            slot["ev"].set()

    def _answer(self, msg: dict) -> None:
        method, params = msg["method"], msg.get("params", {})
        if method == "session/request_permission":
            result, error = {"outcome": self.on_permission(params)}, None
        else:  # fs/*, terminal/* : capacités non annoncées → refus propre
            result, error = None, {"code": -32601, "message": f"Méthode non prise en charge : {method}"}
        reply = {"jsonrpc": "2.0", "id": msg["id"]}
        reply.update({"error": error} if error else {"result": result})
        try:
            self._send(reply)
        except ACPError:
            pass

    # ---- protocole -------------------------------------------------------
    def initialize(self) -> dict:
        res = self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "whisperflow", "version": "0.3.0"}}, timeout=30)
        self.agent_caps = res.get("agentCapabilities", {})
        return res

    def new_session(self, cwd: str) -> str:
        return self.request("session/new", {"cwd": cwd, "mcpServers": []}, timeout=60)["sessionId"]

    def load_session(self, sid: str, cwd: str) -> bool:
        if not self.agent_caps.get("loadSession"):
            return False
        try:
            self.request("session/load", {"sessionId": sid, "cwd": cwd, "mcpServers": []}, timeout=60)
            return True
        except ACPError:
            return False

    def prompt(self, sid: str, text: str, timeout: float | None = None) -> str:
        res = self.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
                           timeout=timeout)
        return res.get("stopReason", "unknown")

    def cancel(self, sid: str) -> None:
        self.notify("session/cancel", {"sessionId": sid})

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def choose_option(options: list[dict], allow: bool) -> dict:
    """Ne choisit jamais « toujours autoriser » : seulement une fois, ou refuser."""
    wanted = ("allow_once",) if allow else ("reject_once", "reject_always")
    for kind in wanted:
        for o in options:
            if o.get("kind") == kind:
                return {"outcome": "selected", "optionId": o["optionId"]}
    return {"outcome": "cancelled"}


@dataclass
class Task:
    id: str
    text: str
    cwd: str
    status: str = "queued"        # queued | running | done | cancelled | error
    started: float = field(default_factory=time.time)
    output: str = ""
    tools: list = field(default_factory=list)
    session_id: str = ""
    stop_reason: str = ""
    cancel_ev: threading.Event = field(default_factory=threading.Event)

    def summary(self) -> dict:
        return {"id": self.id, "status": self.status, "cwd": self.cwd, "task": self.text[:80],
                "minutes": round((time.time() - self.started) / 60, 1), "tools": len(self.tools),
                "last": self.output[-300:].strip()}


class AgentManager:
    """Tâches OpenCode en arrière-plan, une session ACP persistante par dossier."""

    def __init__(self, cfg: AgentConfig, state_file: Path, notify: Callable[[str, str], None],
                 confirmer, client_factory=ACPClient):
        self.cfg, self.state_file, self.notify, self.confirmer = cfg, state_file, notify, confirmer
        self.client_factory = client_factory
        self.tasks: dict[str, Task] = {}
        self._clients: dict[str, ACPClient] = {}
        self._lock = threading.Lock()
        self._n = itertools.count(1)

    def _sessions(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _save_session(self, cwd: str, sid: str) -> None:
        d = self._sessions()
        d[cwd] = sid
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_file.write_text(json.dumps(d))
        os.chmod(self.state_file, 0o600)

    def resolve_cwd(self, project: str) -> str:
        base = Path(os.path.expanduser(self.cfg.default_cwd))
        if project:
            for cand in (base / project, Path(os.path.expanduser(project))):
                if cand.is_dir():
                    return str(cand.resolve())
            low = project.casefold()
            if base.is_dir():
                for d in base.iterdir():
                    if d.is_dir() and d.name.casefold() == low:
                        return str(d.resolve())
        return str(base.resolve() if base.is_dir() else Path.home())

    def submit(self, text: str, project: str = "") -> str:
        tid = f"t{next(self._n)}"
        task = Task(tid, text, self.resolve_cwd(project))
        self.tasks[tid] = task
        threading.Thread(target=self._run, args=(task,), daemon=True).start()
        return tid

    def _client_for(self, task: Task) -> tuple[ACPClient, str]:
        with self._lock:
            client = self._clients.get(task.cwd)
            if client and client.proc.poll() is not None:
                client = None
            fresh = client is None
            if fresh:
                client = self.client_factory(self.cfg.command, task.cwd, self._on_update, self._on_permission)
                client.initialize()
                self._clients[task.cwd] = client
            sid = self._sessions().get(task.cwd)
            if sid and not fresh:
                return client, sid
            if sid and client.load_session(sid, task.cwd):
                return client, sid
            sid = client.new_session(task.cwd)
            self._save_session(task.cwd, sid)
            return client, sid

    def _run(self, task: Task) -> None:
        task.status = "running"
        self.notify("🤖 OpenCode", f"Tâche {task.id} démarrée dans {Path(task.cwd).name}")
        try:
            client, sid = self._client_for(task)
            task.session_id = sid
            task.stop_reason = client.prompt(sid, task.text)
            task.status = "cancelled" if task.stop_reason == "cancelled" else "done"
        except Exception as e:  # noqa: BLE001
            task.status, task.output = "error", task.output + f"\n[erreur] {e}"
        tail = task.output.strip()[-200:] or task.stop_reason
        self.notify(f"🤖 OpenCode : tâche {task.id} {task.status}", tail)

    def _task_by_session(self, sid: str) -> Task | None:
        return next((t for t in reversed(list(self.tasks.values()))
                     if t.session_id == sid and t.status == "running"), None)

    def _on_update(self, params: dict) -> None:
        task = self._task_by_session(params.get("sessionId", ""))
        if not task:
            return
        up = params.get("update", {})
        kind = up.get("sessionUpdate")
        if kind == "agent_message_chunk":
            c = up.get("content", {})
            if c.get("type") == "text":
                task.output += c.get("text", "")
        elif kind == "tool_call":
            task.tools.append({"id": up.get("toolCallId"), "title": up.get("title", ""), "status": up.get("status")})
        elif kind == "tool_call_update":
            for t in task.tools:
                if t["id"] == up.get("toolCallId") and up.get("status"):
                    t["status"] = up["status"]

    def _on_permission(self, params: dict) -> dict:
        task = self._task_by_session(params.get("sessionId", ""))
        if task and task.cancel_ev.is_set():
            return {"outcome": "cancelled"}
        tc = params.get("toolCall", {})
        options = params.get("options", [])
        if tc.get("kind") in self.cfg.auto_allow_kinds:
            return choose_option(options, True)
        title = tc.get("title") or tc.get("kind") or "action"
        ok = self.confirmer.ask(f"OpenCode veut : {title}", f"Dossier : {task.cwd if task else '?'}")
        return choose_option(options, ok)

    def cancel(self, tid: str | None = None) -> list[str]:
        done = []
        for t in self.tasks.values():
            if t.status == "running" and (tid is None or t.id == tid):
                t.cancel_ev.set()
                client = self._clients.get(t.cwd)
                if client:
                    try:
                        client.cancel(t.session_id)
                    except ACPError:
                        pass
                done.append(t.id)
        return done

    def list(self) -> list[dict]:
        return [t.summary() for t in self.tasks.values()]

    def shutdown(self) -> None:
        self.cancel()
        for c in self._clients.values():
            c.close()
