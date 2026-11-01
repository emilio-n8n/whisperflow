"""Catalogue fermé des actions que WhisperFlow sait faire.

Règle d'or : ni Jev, ni Gemini, ni le texte à l'écran ne fournissent de commande
système. Ils choisissent une action de ce catalogue ; chaque action valide ses
paramètres puis lance un argv fixe (jamais de shell). Les actions marquées
`confirm` demandent ton accord par une notification avant de s'exécuter.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import ActionsConfig


class ActionError(RuntimeError):
    pass


def norm(s: str) -> str:
    """minuscules, sans accents, espaces simples : pour comparer ce qui est dit."""
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s.-]", " ", s)).strip()


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=kw.pop("timeout", 10), **kw)


def _spawn(cmd: list[str]) -> None:
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


class Confirmer:
    """Demande oui/non via une notification à boutons (mako gère les actions)."""

    def __init__(self, timeout_s: int = 30, run=subprocess.run, which=shutil.which):
        self.timeout_s, self._run, self._which = timeout_s, run, which

    def ask(self, question: str, detail: str = "") -> bool:
        if not self._which("notify-send"):
            return False  # pas de moyen de demander : on refuse par sécurité
        try:
            r = self._run(["notify-send", "--wait", "-u", "critical", "-t", str(self.timeout_s * 1000),
                           "-A", "yes=Oui", "-A", "no=Non", "WhisperFlow : " + question, detail],
                          capture_output=True, text=True, timeout=self.timeout_s + 5)
        except subprocess.TimeoutExpired:
            return False
        return (r.stdout or "").strip() == "yes"


@dataclass
class Action:
    name: str
    description: str                  # sert aussi de critère pour Jev
    params: dict = field(default_factory=dict)   # nom -> "str" | "int" | "app"
    risk: str = "safe"                # safe | confirm
    handler: Callable | None = None


@dataclass
class Result:
    ok: bool
    message: str
    data: dict = field(default_factory=dict)

    def for_model(self) -> dict:
        """Format de réponse d'outil conseillé pour Gemini 3.8 Live."""
        return {"status": "ok" if self.ok else "error", "retryable": False, "message": self.message, **self.data}


class Executor:
    def __init__(self, cfg: ActionsConfig, notify: Callable[[str, str], None], confirmer: Confirmer,
                 run=_run, spawn=_spawn, which=shutil.which, injector=None, agents=None, live=None,
                 annotator=None, shortcuts=None):
        self.cfg, self.notify, self.confirmer = cfg, notify, confirmer
        self._run, self._spawn, self._which = run, spawn, which
        self.injector, self.agents, self.live, self.annotator, self.shortcuts = (
            injector, agents, live, annotator, shortcuts)
        self.timers: list[threading.Timer] = []
        self.catalog = self._build()

    # ---- catalogue -------------------------------------------------------
    def _build(self) -> dict[str, Action]:
        A = Action
        acts = [
            A("open_app", "Ouvrir une application (navigateur, terminal, fichiers, éditeur…).", {"app": "app"},
              handler=self.open_app),
            A("open_url", "Ouvrir un site web précis dont l'adresse est donnée.", {"url": "str"}, handler=self.open_url),
            A("web_search", "Faire une recherche sur internet.", {"query": "str"}, handler=self.web_search),
            A("find_file", "Chercher un fichier ou un dossier sur l'ordinateur par son nom.", {"query": "str"},
              handler=self.find_file),
            A("open_file", "Ouvrir un fichier de l'ordinateur par son nom.", {"query": "str"}, handler=self.open_file),
            A("volume", "Changer le volume du son : monter, baisser, couper, régler à un niveau.",
              {"op": "str", "value": "int"}, handler=self.volume),
            A("media", "Contrôler la musique ou la vidéo : pause, lecture, suivant, précédent.", {"op": "str"},
              handler=self.media),
            A("workspace", "Aller sur un bureau / espace de travail numéroté.", {"n": "int"}, handler=self.workspace),
            A("close_window", "Fermer la fenêtre active.", risk="confirm", handler=self.close_window),
            A("lock_screen", "Verrouiller l'écran.", handler=self.lock_screen),
            A("screenshot", "Prendre une capture d'écran et l'enregistrer.", handler=self.screenshot),
            A("timer", "Lancer un minuteur ou un rappel dans quelques minutes.", {"minutes": "int", "label": "str"},
              handler=self.timer),
            A("type_text", "Écrire un texte donné dans la fenêtre active.", {"text": "str"}, handler=self.type_text),
            A("delegate_agent", "Confier une tâche de programmation longue à l'agent OpenCode.",
              {"task": "str", "project": "str"}, handler=self.delegate_agent),
            A("show_me", "Montrer où se trouve un élément à l'écran, sans cliquer.", {"target": "str"},
              handler=self.show_me),
            A("live", "Démarrer ou arrêter une conversation vocale avec Gemini.", {"op": "str"}, handler=self.live_ctl),
            A("run_shortcut", "Lancer un raccourci appris (suite d'actions enregistrée).", {"name": "str"},
              handler=self.run_shortcut),
        ]
        return {a.name: a for a in acts}

    def describe(self) -> dict[str, str]:
        return {a.name: a.description for a in self.catalog.values()}

    def execute(self, name: str, params: dict | None = None, confirmed: bool = False) -> Result:
        act = self.catalog.get(name)
        if not act:
            return Result(False, f"Action inconnue : {name}")
        params = {k: v for k, v in (params or {}).items() if k in act.params}
        if act.risk == "confirm" and not confirmed:
            if not self.confirmer.ask(act.description, str(params or "")):
                return Result(False, "Annulé : pas de confirmation")
        try:
            return act.handler(**params)
        except ActionError as e:
            return Result(False, str(e))
        except TypeError as e:
            return Result(False, f"Paramètres invalides pour {name} : {e}")

    # ---- handlers --------------------------------------------------------
    def open_app(self, app: str = "") -> Result:
        key = next((k for k in self.cfg.apps if norm(k) == norm(app)), None)
        if not key:
            raise ActionError(f"Application non autorisée : « {app} ». Ajoute-la dans [actions.apps].")
        argv = list(self.cfg.apps[key])
        if not self._which(argv[0]):
            raise ActionError(f"{argv[0]} n'est pas installé")
        self._spawn(argv)
        return Result(True, f"J'ouvre {key}")

    def open_url(self, url: str = "") -> Result:
        url = url.strip()
        if not re.match(r"^https?://", url):
            if re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", url):
                url = "https://" + url
            else:
                raise ActionError(f"Adresse invalide : {url}")
        self._spawn(["xdg-open", url])
        return Result(True, f"J'ouvre {url}")

    def web_search(self, query: str = "") -> Result:
        if not query.strip():
            raise ActionError("Recherche vide")
        self._spawn(["xdg-open", self.cfg.search_engine + urllib.parse.quote_plus(query.strip())])
        return Result(True, f"Je cherche « {query} »")

    def _search(self, query: str, limit: int = 5, max_files: int = 50000) -> list[Path]:
        def words(x: str) -> str:
            return re.sub(r"[-_.]+", " ", norm(x)).strip()
        q = words(query)
        if not q:
            return []
        scored, seen = [], 0
        for d in self.cfg.search_dirs:
            root = Path(os.path.expanduser(d))
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [x for x in dirnames if not x.startswith(".") and x not in ("node_modules", "__pycache__")]
                for name in dirnames + filenames:
                    seen += 1
                    n = words(name)
                    if q in n:
                        scored.append((0 if words(Path(name).stem) == q else 1, len(n), Path(dirpath) / name))
                if seen > max_files:
                    break
        scored.sort(key=lambda t: (t[0], t[1]))
        return [p for *_, p in scored[:limit]]

    def find_file(self, query: str = "") -> Result:
        hits = self._search(query)
        if not hits:
            return Result(False, f"Aucun fichier ne contient « {query} »")
        home = str(Path.home())
        lines = [str(p).replace(home, "~") for p in hits]
        self.notify("Fichiers trouvés", "\n".join(lines))
        return Result(True, f"{len(hits)} résultat(s), le premier : {lines[0]}", {"files": lines})

    def open_file(self, query: str = "") -> Result:
        hits = self._search(query, limit=1)
        if not hits:
            return Result(False, f"Aucun fichier ne contient « {query} »")
        self._spawn(["xdg-open", str(hits[0])])
        return Result(True, f"J'ouvre {hits[0].name}")

    def volume(self, op: str = "set", value: int = 0) -> Result:
        sink = "@DEFAULT_AUDIO_SINK@"
        if op == "mute":
            argv = ["wpctl", "set-mute", sink, "toggle"]
        elif op == "up":
            argv = ["wpctl", "set-volume", "-l", "1.0", sink, f"{max(1, min(int(value or 10), 50))}%+"]
        elif op == "down":
            argv = ["wpctl", "set-volume", sink, f"{max(1, min(int(value or 10), 50))}%-"]
        elif op == "set":
            argv = ["wpctl", "set-volume", "-l", "1.0", sink, f"{max(0, min(int(value), 100))}%"]
        else:
            raise ActionError(f"Opération volume inconnue : {op}")
        r = self._run(argv)
        return Result(r.returncode == 0, "Volume réglé" if r.returncode == 0 else "wpctl a échoué")

    def media(self, op: str = "play-pause") -> Result:
        if op not in ("play-pause", "play", "pause", "next", "previous"):
            raise ActionError(f"Commande média inconnue : {op}")
        r = self._run(["playerctl", op])
        return Result(r.returncode == 0, "OK" if r.returncode == 0 else "Aucun lecteur actif")

    def workspace(self, n: int = 1) -> Result:
        n = int(n)
        if not 1 <= n <= 10:
            raise ActionError("Bureau entre 1 et 10")
        r = self._run(["hyprctl", "dispatch", "workspace", str(n)])
        return Result(r.returncode == 0, f"Bureau {n}")

    def close_window(self) -> Result:
        r = self._run(["hyprctl", "dispatch", "killactive"])
        return Result(r.returncode == 0, "Fenêtre fermée")

    def lock_screen(self) -> Result:
        r = self._run(["loginctl", "lock-session"])
        return Result(r.returncode == 0, "Écran verrouillé")

    def screenshot(self) -> Result:
        d = Path(os.path.expanduser(os.environ.get("XDG_PICTURES_DIR", "~/Images")))
        d.mkdir(parents=True, exist_ok=True)
        out = d / time.strftime("capture-%Y%m%d-%H%M%S.png")
        r = self._run(["grim", str(out)])
        return Result(r.returncode == 0, f"Capture enregistrée : {out.name}", {"path": str(out)})

    def timer(self, minutes: int = 5, label: str = "") -> Result:
        minutes = int(minutes)
        if not 1 <= minutes <= 24 * 60:
            raise ActionError("Minuteur entre 1 minute et 24 heures")
        t = threading.Timer(minutes * 60, self.notify, args=("⏰ Minuteur", label or f"{minutes} min écoulées"))
        t.daemon = True
        t.start()
        self.timers.append(t)
        return Result(True, f"Minuteur de {minutes} min lancé")

    def type_text(self, text: str = "") -> Result:
        if not self.injector or not text:
            raise ActionError("Rien à écrire")
        self.injector.insert(text)
        return Result(True, "Texte écrit")

    def delegate_agent(self, task: str = "", project: str = "") -> Result:
        if not self.agents:
            raise ActionError("Agent OpenCode indisponible")
        tid = self.agents.submit(task, project)
        return Result(True, f"Tâche {tid} confiée à OpenCode", {"task_id": tid})

    def show_me(self, target: str = "") -> Result:
        if not self.annotator:
            raise ActionError("Annotations indisponibles (installe tesseract)")
        return self.annotator.show(target)

    def live_ctl(self, op: str = "toggle") -> Result:
        if not self.live:
            raise ActionError("Gemini Live indisponible")
        return self.live(op)

    def run_shortcut(self, name: str = "") -> Result:
        if not self.shortcuts:
            raise ActionError("Aucun raccourci")
        steps = self.shortcuts.get(name)
        if steps is None:
            raise ActionError(f"Raccourci inconnu : {name}")
        msgs = []
        for st in steps:
            if st["action"] == "run_shortcut":
                continue  # pas de récursion
            r = self.execute(st["action"], st.get("params"))
            msgs.append(r.message)
            if not r.ok:
                return Result(False, f"Raccourci {name} arrêté : {r.message}")
        return Result(True, f"Raccourci {name} : " + " ; ".join(msgs))
