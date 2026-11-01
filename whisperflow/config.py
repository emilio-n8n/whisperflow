"""Configuration centralisée de WhisperFlow.

Ordre de priorité : valeurs par défaut < config.toml < variables d'environnement.
Les clés API ne sont JAMAIS lues depuis config.toml : uniquement depuis
l'environnement ou ~/.config/whisperflow/secrets.env (permissions 600 exigées).
"""
from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "whisperflow"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "whisperflow"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/whisperflow-{os.getuid()}"
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


@dataclass
class STTConfig:
    provider: str = "groq"
    model: str = "whisper-large-v3-turbo"  # ou "whisper-large-v3" (plus précis, plus cher)
    language: str = "fr"                    # "" = détection automatique
    prompt: str = ""                        # vocabulaire : noms propres, jargon
    timeout_s: float = 20.0
    retries: int = 2


@dataclass
class AudioConfig:
    backend: str = "auto"       # auto | pw-record | arecord
    device: str = ""            # cible PipeWire (vide = défaut)
    sample_rate: int = 16000
    max_seconds: int = 300      # arrêt automatique de sécurité
    min_seconds: float = 0.4    # en dessous : ignoré
    silence_rms: int = 120      # en dessous : considéré comme silence


@dataclass
class InjectConfig:
    method: str = "auto"        # auto | wtype | clipboard | none
    restore_clipboard: bool = True
    paste_shortcut: str = "ctrl+v"   # terminaux : "ctrl+shift+v"
    trailing_space: bool = True
    # False (défaut) : les retours à la ligne sont aplatis en espaces. Une dictée
    # ne doit jamais presser Entrée, sinon un champ de chat envoie le message.
    newlines: bool = False


@dataclass
class UIConfig:
    notifications: bool = True
    waybar_signal: int = 8      # pkill -RTMIN+8 waybar


@dataclass
class HistoryConfig:
    enabled: bool = True        # garde le texte dicté localement (secours si l'insertion échoue)
    max_entries: int = 500


@dataclass
class JevConfig:
    enabled: bool = True
    # OpenCode Zen (System One) : même corps de requête que TypeSafe.
    model: str = "jev-1.13-free"     # "jev-1.13" si le modèle payant est activé sur la clé
    url: str = "https://opencode.ai/inference/systemone/v1/systemone"
    min_confidence: float = 0.6      # en dessous : on ne fait rien et on demande
    timeout_s: float = 8.0


@dataclass
class ActionsConfig:
    # Applications que les commandes vocales ont le droit d'ouvrir : nom dit -> commande (argv).
    apps: dict = field(default_factory=lambda: {
        "navigateur": ["chromium"], "chromium": ["chromium"], "firefox": ["firefox"],
        "terminal": ["xdg-terminal-exec"], "fichiers": ["nautilus"], "éditeur": ["gnome-text-editor"],
    })
    search_dirs: list = field(default_factory=lambda: ["~/Documents", "~/Bureau", "~/Desktop",
                                                       "~/Téléchargements", "~/Downloads", "~/Projets"])
    search_engine: str = "https://duckduckgo.com/?q="


@dataclass
class AgentConfig:
    command: list = field(default_factory=lambda: ["opencode", "acp"])
    default_cwd: str = "~/Projets"
    permission_timeout_s: int = 120   # sans réponse : refus
    auto_allow_kinds: list = field(default_factory=lambda: ["read", "search", "think", "fetch"])


@dataclass
class LiveConfig:
    model: str = "gemini-3.8-live"
    url: str = ("wss://generativelanguage.googleapis.com/ws/"
                "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
    voice: str = "Aoede"
    send_screen: bool = True          # une capture par seconde max (limite du modèle : 1 FPS)
    screen_interval_s: float = 1.5
    screen_max_width: int = 1280
    computer_use: bool = False        # clics/saisie par Gemini : désactivé tant que tu ne l'actives pas
    max_session_min: int = 15


@dataclass
class RemoteConfig:
    """Accès iPhone : page HTTPS + WebSocket derrière un tunnel Cloudflare temporaire."""
    port: int = 8765                  # écoute locale uniquement (127.0.0.1)
    token_ttl_min: int = 120          # le lien et le serveur s'arrêtent seuls après ce délai
    allow_audio: bool = True          # micro du téléphone → Gemini
    allow_controls: bool = True       # bouton capture / arrêt depuis la page


@dataclass
class Config:
    stt: STTConfig = field(default_factory=STTConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    inject: InjectConfig = field(default_factory=InjectConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    jev: JevConfig = field(default_factory=JevConfig)
    actions: ActionsConfig = field(default_factory=ActionsConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    remote: RemoteConfig = field(default_factory=RemoteConfig)


def _apply(section, data: dict) -> None:
    for k, v in data.items():
        if hasattr(section, k):
            cur = getattr(section, k)
            setattr(section, k, v if isinstance(cur, (dict, list)) else type(cur)(v))


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    path = path or (config_dir() / "config.toml")
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for name in ("stt", "audio", "inject", "ui", "history", "jev", "actions", "agent", "live", "remote"):
            if isinstance(data.get(name), dict):
                _apply(getattr(cfg, name), data[name])
    if os.environ.get("WHISPERFLOW_LANGUAGE") is not None:
        cfg.stt.language = os.environ["WHISPERFLOW_LANGUAGE"]
    return cfg


class SecretError(RuntimeError):
    pass


def load_secret(name: str, secrets_file: Path | None = None) -> str:
    """Lit une clé (ex. GROQ_API_KEY). Ne la journalise jamais."""
    if os.environ.get(name):
        return os.environ[name]
    secrets_file = secrets_file or (config_dir() / "secrets.env")
    if secrets_file.exists():
        mode = stat.S_IMODE(secrets_file.stat().st_mode)
        if mode & 0o077:
            raise SecretError(
                f"{secrets_file} est lisible par d'autres utilisateurs : lance `chmod 600 {secrets_file}`"
            )
        for line in secrets_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    raise SecretError(f"Clé {name} introuvable (environnement ou {secrets_file})")


def redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text
