"""Routeur de commandes vocales : raccourcis appris → règles locales → Jev 1.13.

Jev (OpenCode Zen, endpoint System One, modèle jev-1.13 / jev-1.13-free) ne produit jamais de
texte ni de commande : il choisit une action dans le catalogue (question `choice`) avec des probabilités.
Les paramètres libres (requête, nombre…) sont extraits localement. Confiance trop basse → rien.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .actions import norm
from .config import JevConfig, redact

NUMBERS = {"un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8,
           "neuf": 9, "dix": 10, "quinze": 15, "vingt": 20, "trente": 30, "quarante": 40, "cinquante": 50,
           "soixante": 60, "quatre-vingt": 80, "cent": 100}


def to_int(s: str) -> int | None:
    s = s.strip().rstrip("%").strip()
    if s.isdigit():
        return int(s)
    return NUMBERS.get(norm(s).replace(" ", "-"))


@dataclass
class Route:
    action: str | None
    params: dict = field(default_factory=dict)
    source: str = "rule"           # shortcut | rule | jev | none
    confidence: float = 1.0
    alternatives: dict = field(default_factory=dict)
    message: str = ""


NUM = r"(\d+|un|une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|quinze|vingt|trente|quarante|cinquante|soixante|cent)"

# (regex sur le texte normalisé, action, fonction -> params). L'ordre compte.
RULES: list[tuple[str, str, callable]] = [
    (r"^(?:demande a |dis a )?(?:open ?code|l agent)[ ,:]+(?:dans (?:le projet )?(\S+) )?(?:de |d |pour )?(.+)$",
     "delegate_agent", lambda m: {"project": m.group(1) or "", "task": m.group(2)}),
    (r"^(?:montre moi|montre|ou est|ou se trouve)(?: ou (?:est|se trouve))? (?:le |la |les |l )?(?:bouton |lien |menu )?(.+)$",
     "show_me", lambda m: {"target": m.group(1)}),
    (r"^(?:cherche|trouve|retrouve) (?:le |la |les |mon |ma |mes )?(?:fichier|dossier|document)s? (.+)$",
     "find_file", lambda m: {"query": m.group(1)}),
    (r"^ouvre (?:le |la |mon |ma )?(?:fichier|document) (.+)$", "open_file", lambda m: {"query": m.group(1)}),
    (r"^(?:cherche|recherche)(?: sur (?:internet|le web|google))? (.+)$", "web_search", lambda m: {"query": m.group(1)}),
    (r"^(?:va sur|ouvre le site|ouvre) ((?:https?://)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?)$", "open_url",
     lambda m: {"url": m.group(1)}),
    (r"^(?:ouvre|lance|demarre) (?:le |la |l |un |une |mon |ma )?(.+)$", "open_app", lambda m: {"app": m.group(1)}),
    (r"^(?:coupe|remets?|active|desactive) le son$|^(?:silence|mute)$", "volume", lambda m: {"op": "mute"}),
    (r"^(?:monte|augmente) (?:le son|le volume)(?: de " + NUM + r")?", "volume",
     lambda m: {"op": "up", "value": to_int(m.group(1)) if m.group(1) else 10}),
    (r"^(?:baisse|diminue) (?:le son|le volume)(?: de " + NUM + r")?", "volume",
     lambda m: {"op": "down", "value": to_int(m.group(1)) if m.group(1) else 10}),
    (r"^(?:mets |regle )?(?:le )?(?:son|volume) (?:a )?" + NUM + r"(?: pour ?cent)?$", "volume",
     lambda m: {"op": "set", "value": to_int(m.group(1))}),
    (r"^(?:pause|mets en pause|stop la musique)$", "media", lambda m: {"op": "pause"}),
    (r"^(?:lecture|reprends|play|relance la musique)$", "media", lambda m: {"op": "play"}),
    (r"^(?:morceau |chanson |piste )?suivante?$", "media", lambda m: {"op": "next"}),
    (r"^(?:morceau |chanson |piste )?precedente?$", "media", lambda m: {"op": "previous"}),
    (r"^(?:va sur |passe sur |aller sur )?(?:le |l )?(?:bureau|espace(?: de travail)?) (?:numero )?" + NUM + r"$",
     "workspace", lambda m: {"n": to_int(m.group(1))}),
    (r"^ferme (?:la |cette )?fenetre$", "close_window", lambda m: {}),
    (r"^verrouille(?: l ecran| la session| l ordi(?:nateur)?)?$", "lock_screen", lambda m: {}),
    (r"^(?:fais |prends )?(?:une )?capture(?: d ecran)?$", "screenshot", lambda m: {}),
    (r"^(?:mets |lance )?(?:un )?(?:minuteur|timer|rappel) (?:de |dans )?" + NUM + r" minutes?(?: pour (.+))?$",
     "timer", lambda m: {"minutes": to_int(m.group(1)), "label": m.group(2) or ""}),
    (r"^(?:parle a |lance |demarre )?gemini(?: live)?$|^(?:mode )?conversation$", "live", lambda m: {"op": "start"}),
    (r"^(?:arrete|stoppe|coupe) gemini$", "live", lambda m: {"op": "stop"}),
    (r"^(?:raccourci|lance le raccourci) (.+)$", "run_shortcut", lambda m: {"name": m.group(1)}),
]

# Paramètres qu'on sait extraire quand Jev choisit l'action sans règle locale.
EXTRACT = {
    "web_search": lambda t: {"query": re.sub(r"^\W*(?:\w+ ){0,3}?(?:sur|pour|de) ", "", t) or t},
    "find_file": lambda t: {"query": t.split()[-1] if t.split() else t},
    "volume": lambda t: {"op": "down" if re.search(r"baiss|fort|moins", norm(t)) else "up", "value": 10},
    "media": lambda t: {"op": "play-pause"},
}


class Shortcuts:
    """Raccourcis appris : nom → suite d'actions du catalogue (rejouées sans IA)."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def get(self, name: str):
        d = self._load()
        return next((v["steps"] for k, v in d.items() if norm(k) == norm(name)), None)

    def match_phrase(self, text: str):
        t = norm(text)
        for k, v in self._load().items():
            if t == norm(k) or t in (norm(p) for p in v.get("phrases", [])):
                return k
        return None

    def save(self, name: str, steps: list[dict], phrases: list[str] | None = None) -> None:
        d = self._load()
        d[name] = {"steps": steps, "phrases": phrases or [], "created": time.strftime("%Y-%m-%d %H:%M")}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def delete(self, name: str) -> bool:
        d = self._load()
        k = next((k for k in d if norm(k) == norm(name)), None)
        if not k:
            return False
        del d[k]
        self.path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    def all(self) -> dict:
        return self._load()


class JevClient:
    def __init__(self, cfg: JevConfig, api_key: str):
        self.cfg, self.api_key = cfg, api_key

    def decide(self, state, questions: dict) -> dict:
        body = json.dumps({"model": self.cfg.model, "state": state, "questions": questions}).encode()
        req = urllib.request.Request(self.cfg.url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
            "X-Title": "WhisperFlow", "User-Agent": "whisperflow"})
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError("Clé OpenCode refusée (401) : vérifie OPENCODE_API_KEY") from None
            if e.code in (402, 403):
                raise RuntimeError(
                    f"Modèle Jev non autorisé pour cette clé OpenCode ({e.code}) : "
                    "active-le dans la console, ou utilise jev-1.13-free") from None
            raise RuntimeError(f"Jev HTTP {e.code} : {redact(e.read().decode('utf-8', 'replace')[:200], self.api_key)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"Jev injoignable : {type(e).__name__}") from None


class Router:
    def __init__(self, catalog: dict[str, str], apps: list[str], jev: JevClient | None, cfg: JevConfig,
                 shortcuts: Shortcuts | None = None):
        self.catalog, self.apps, self.jev, self.cfg, self.shortcuts = catalog, apps, jev, cfg, shortcuts

    def route(self, text: str) -> Route:
        t = norm(text).rstrip(".")
        if not t:
            return Route(None, source="none", message="Commande vide")
        if self.shortcuts and (name := self.shortcuts.match_phrase(t)):
            return Route("run_shortcut", {"name": name}, "shortcut")
        words = t.replace("-", " ")  # « montre-moi » = « montre moi » (sauf pour les adresses web)
        for pattern, action, fn in RULES:
            m = re.search(pattern, t if action == "open_url" else words)
            if m:
                params = fn(m)
                if action == "open_app":
                    app = self._match_app(params["app"])
                    if not app:
                        continue  # « ouvre fichier.pdf » etc. : on laisse les autres règles / Jev
                    params = {"app": app}
                if any(v is None for v in params.values()):
                    continue
                return Route(action, params, "rule")
        return self._jev(text)

    def _match_app(self, said: str) -> str | None:
        s = norm(said)
        return next((a for a in self.apps if norm(a) == s or norm(a) in s.split()), None)

    def _jev(self, text: str) -> Route:
        if not (self.jev and self.cfg.enabled):
            return Route(None, source="none", message=f"Commande non reconnue : « {text} »")
        criteria = dict(self.catalog)
        criteria["none"] = "Rien de tout cela, ou ce n'est pas une commande pour l'ordinateur."
        questions = {"action": {"type": "choice", "instructions":
                                "Quelle action de l'ordinateur l'utilisateur demande-t-il ? "
                                "Le texte vient d'une dictée vocale en français et peut contenir des erreurs.",
                                "criteria": criteria}}
        if self.apps:
            questions["app"] = {"type": "choice", "instructions": "Si l'utilisateur veut ouvrir une application, laquelle ?",
                                "criteria": {**{a: f"L'application « {a} »" for a in self.apps},
                                             "none": "Aucune application de cette liste."}}
        try:
            res = self.jev.decide({"commande_vocale": text}, questions)
        except RuntimeError as e:
            return Route(None, source="none", message=str(e))
        ans = res.get("answers", {}).get("action", {})
        if ans.get("type") != "choice":
            return Route(None, source="none", message="Réponse Jev inattendue")
        action = ans.get("choice")
        probs = ans.get("probabilities") or {}
        conf = float(ans.get("confidence", probs.get(action, 0)))
        alts = dict(sorted(probs.items(), key=lambda kv: -kv[1])[:3])
        if action in (None, "none") or action not in self.catalog:
            return Route(None, source="jev", confidence=conf, alternatives=alts,
                         message=f"Je n'ai pas reconnu de commande dans « {text} »")
        if conf < self.cfg.min_confidence:
            return Route(None, source="jev", confidence=conf, alternatives=alts,
                         message=f"Pas sûr ({int(conf * 100)} %) : {action} ? Reformule ou utilise la commande exacte.")
        params = {}
        if action == "open_app":
            app_ans = res["answers"].get("app", {})
            if app_ans.get("choice") in (None, "none"):
                return Route(None, source="jev", confidence=conf, message="Quelle application ?")
            params = {"app": app_ans["choice"]}
        elif action in EXTRACT:
            params = EXTRACT[action](text)
        elif self._needs_free_params(action):
            return Route(None, source="jev", confidence=conf, alternatives=alts,
                         message=f"J'ai compris « {action} » mais il me manque des détails. Dis la commande complète.")
        return Route(action, params, "jev", conf, alts)

    @staticmethod
    def _needs_free_params(action: str) -> bool:
        return action in {"open_url", "open_file", "workspace", "timer", "type_text", "delegate_agent",
                          "show_me", "run_shortcut"}
