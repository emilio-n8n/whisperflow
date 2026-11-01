"""Nettoyage déterministe de la transcription (aucun appel à un modèle)."""
from __future__ import annotations

import re

# Phrases que Whisper "hallucine" sur du silence ou du bruit (connues en FR/EN).
HALLUCINATIONS = [
    r"sous-titres? (réalisés? )?(par|pour) la communauté d'amara\.org",
    r"sous-titrage (st'?\s*501|société radio-canada|fr ?\?)",
    r"merci d'avoir regardé( cette vidéo)?",
    r"abonnez-vous( à la chaîne)?",
    r"thanks? (you )?for watching",
    r"please subscribe",
]
_HALL_RE = re.compile(r"^\W*(" + "|".join(HALLUCINATIONS) + r")\W*$", re.IGNORECASE)

# Commandes de mise en forme dictées (option simple, prévisible).
SPOKEN = [
    (re.compile(r"\s*\b(à la ligne|nouvelle ligne|retour à la ligne)\b[.,]?\s*", re.I), "\n"),
    (re.compile(r"\s*\bnouveau paragraphe\b[.,]?\s*", re.I), "\n\n"),
]


def is_hallucination(text: str) -> bool:
    return bool(_HALL_RE.match(text.strip()))


def clean(text: str, trailing_space: bool = True) -> str:
    t = text.strip()
    if not t or is_hallucination(t):
        return ""
    for rx, rep in SPOKEN:
        t = rx.sub(rep, t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = t.strip(" ")
    if trailing_space and t and not t.endswith("\n"):
        t += " "
    return t
