"""Point d'entrée : `whisperflow <commande>`."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import __version__
from .config import SecretError, config_dir, load_config, load_secret, runtime_dir, state_dir


def _paths():
    rt = runtime_dir() / "whisperflow"
    rt.mkdir(parents=True, exist_ok=True, mode=0o700)
    return rt, rt / "whisperflow.sock", rt / "state.json"


def cmd_daemon(args) -> int:
    from .daemon import run
    from .history import History
    from .inject import WaylandInjector
    from .stt import GroqTranscriber
    from .ui import Indicator

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    try:
        key = load_secret("GROQ_API_KEY")
    except SecretError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 2
    from .brain import Brain
    from .ui import notify
    rt, _, state = _paths()
    injector = WaylandInjector(cfg.inject)
    history = History(state_dir() / "history.jsonl", cfg.history.enabled, cfg.history.max_entries)
    brain = Brain(cfg, injector, notify, state_dir(), history)
    run(cfg, GroqTranscriber(cfg.stt, key), injector, Indicator(cfg.ui, state), history, rt, brain)
    return 0


def cmd_client(args) -> int:
    from .daemon import send
    _, sock, _ = _paths()
    req = {"cmd": args.cmd}
    for k in ("mode", "op", "text", "project", "id", "name", "steps", "phrases"):
        v = getattr(args, k, None)
        if v is not None:
            req[k] = " ".join(v) if k == "text" and isinstance(v, list) else v
    try:
        resp = send(sock, req, timeout=getattr(args, "timeout", 3.0))
    except (FileNotFoundError, ConnectionRefusedError):
        print("Le service WhisperFlow ne tourne pas : systemctl --user start whisperflow", file=sys.stderr)
        subprocess.run(["notify-send", "-u", "critical", "WhisperFlow", "Service arrêté"], capture_output=True) \
            if shutil.which("notify-send") else None
        return 3
    print(json.dumps(resp, ensure_ascii=False, indent=2 if len(json.dumps(resp)) > 120 else None))
    return 0 if resp.get("ok") else 1


def cmd_waybar(args) -> int:
    from .ui import waybar_json
    _, _, state = _paths()
    print(waybar_json(state))
    return 0


def cmd_last(args) -> int:
    from .history import History
    cfg = load_config()
    items = History(state_dir() / "history.jsonl", cfg.history.enabled).last(args.n)
    if args.copy and items:
        subprocess.run(["wl-copy"], input=items[-1]["text"].encode())
        print("Dernier texte copié dans le presse-papiers.")
        return 0
    for it in items:
        flag = "✓" if it.get("inserted") else "✗"
        print(f"{it.get('ts','')} {flag} {it.get('total_ms','?')} ms  {it.get('text','').strip()}")
    return 0


def cmd_transcribe(args) -> int:
    """Teste Groq sur un fichier, sans micro ni insertion. Affiche la latence."""
    from .stt import GroqTranscriber
    from .textproc import clean
    cfg = load_config()
    if args.model:
        cfg.stt.model = args.model
    t = time.monotonic()
    text = GroqTranscriber(cfg.stt, load_secret("GROQ_API_KEY")).transcribe(Path(args.file))
    print(clean(text, False))
    print(f"[{cfg.stt.model} : {int((time.monotonic() - t) * 1000)} ms]", file=sys.stderr)
    return 0


def cmd_doctor(args) -> int:
    ok = True

    def line(good: bool, label: str, hint: str = ""):
        nonlocal ok
        ok = ok and good
        print(f"{'✓' if good else '✗'} {label}" + (f"  → {hint}" if hint and not good else ""))

    import os
    line(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]} (≥ 3.11)", "pacman -S python")
    line(os.environ.get("XDG_SESSION_TYPE") == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY")),
         "Session Wayland")
    line(bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")), "Hyprland détecté")
    rec = shutil.which("pw-record") or shutil.which("arecord")
    line(bool(rec), f"Enregistreur audio ({rec or 'aucun'})", "pacman -S pipewire-audio")
    line(bool(shutil.which("wtype")), "wtype (insertion du texte)", "pacman -S wtype")
    line(bool(shutil.which("wl-copy")), "wl-clipboard (repli presse-papiers)", "pacman -S wl-clipboard")
    line(bool(shutil.which("notify-send")), "notify-send (notifications)", "pacman -S libnotify")
    if shutil.which("wpctl"):
        r = subprocess.run(["wpctl", "inspect", "@DEFAULT_AUDIO_SOURCE@"], capture_output=True, text=True)
        name = next((l.split("=", 1)[1].strip().strip('"') for l in r.stdout.splitlines()
                     if "node.description" in l), "")
        line(r.returncode == 0 and bool(name), f"Micro par défaut : {name or 'aucun'}",
             "branche un micro USB/casque puis choisis-le (wpctl status / pavucontrol)")
    for tool, label, pkg in [("grim", "grim (captures)", "grim"), ("tesseract", "tesseract (OCR)",
                             "tesseract tesseract-data-fra tesseract-data-eng"),
                             ("ydotool", "ydotool (clics, computer use)", "ydotool"),
                             ("parec", "parec/pacat (audio Gemini Live)", "libpulse"),
                             ("playerctl", "playerctl (musique)", "playerctl"),
                             ("cloudflared", "cloudflared (accès iPhone)", "cloudflared"),
                             ("opencode", "OpenCode (agent)", "opencode")]:
        line(bool(shutil.which(tool)), label, f"sudo pacman -S {pkg}")
    from .screen import overlay_supported
    line(overlay_supported(), "Overlay d'annotation (GTK4 layer-shell)", "sudo pacman -S python-gobject gtk4-layer-shell")
    for name, what in [("OPENCODE_API_KEY", "Jev / OpenCode Zen (commandes floues)"),
                       ("GEMINI_API_KEY", "Gemini Live")]:
        try:
            load_secret(name)
            line(True, f"Clé {name} ({what})")
        except SecretError:
            line(False, f"Clé {name} absente ({what})", f"ajoute {name}=... dans {config_dir()}/secrets.env")
    try:
        key = load_secret("GROQ_API_KEY")
        line(True, "Clé Groq trouvée")
        req = urllib.request.Request("https://api.groq.com/openai/v1/models",
                                     headers={"Authorization": f"Bearer {key}", "User-Agent": "whisperflow"})
        try:
            t = time.monotonic()
            with urllib.request.urlopen(req, timeout=8) as r:
                ids = {m["id"] for m in json.loads(r.read())["data"]}
            ms = int((time.monotonic() - t) * 1000)
            cfg = load_config()
            line(cfg.stt.model in ids, f"Groq joignable ({ms} ms), modèle {cfg.stt.model} disponible")
        except Exception as e:  # noqa: BLE001
            line(False, f"Groq injoignable : {type(e).__name__}", "vérifie la clé et le réseau")
    except SecretError as e:
        line(False, str(e), f"echo 'GROQ_API_KEY=...' > {config_dir()}/secrets.env && chmod 600 {config_dir()}/secrets.env")
    _, sock, _ = _paths()
    line(sock.exists(), "Service WhisperFlow lancé", "systemctl --user enable --now whisperflow")
    print("\nTout est prêt." if ok else "\nCorrige les ✗ ci-dessus puis relance `whisperflow doctor`.")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="whisperflow", description="Dictée vocale pour Hyprland")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("daemon", help="lance le service (systemd s'en charge)")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(fn=cmd_daemon)
    for c, h in [("toggle", "démarrer/arrêter la dictée"), ("start", "démarrer (mode maintien)"),
                 ("stop", "arrêter (mode maintien)"), ("cancel", "annuler"), ("status", "état")]:
        sub.add_parser(c, help=h).set_defaults(fn=cmd_client, cmd=c)
    sub.add_parser("command", help="démarrer/arrêter une commande vocale").set_defaults(
        fn=cmd_client, cmd="toggle", mode="command")
    sub.add_parser("panic", help="arrêt d'urgence (Gemini, agents, enregistrement)").set_defaults(fn=cmd_client, cmd="panic")
    r = sub.add_parser("run", help="exécuter une commande écrite (comme si tu l'avais dite)")
    r.add_argument("text", nargs="+")
    r.set_defaults(fn=cmd_client, cmd="run", timeout=60)
    r = sub.add_parser("route", help="voir quelle action serait choisie, sans l'exécuter")
    r.add_argument("text", nargs="+")
    r.set_defaults(fn=cmd_client, cmd="route", timeout=15)
    lv = sub.add_parser("live", help="Gemini Live : start | stop | toggle | status")
    lv.add_argument("op", nargs="?", default="toggle", choices=["start", "stop", "toggle", "status"])
    lv.set_defaults(fn=cmd_client, cmd="live", timeout=20)
    rm = sub.add_parser("remote", help="lien iPhone temporaire via Cloudflare : start | stop | status")
    rm.add_argument("op", nargs="?", default="toggle", choices=["start", "stop", "toggle", "status"])
    rm.set_defaults(fn=cmd_client, cmd="remote", timeout=120)
    ag = sub.add_parser("agent", help="confier une tâche à OpenCode")
    ag.add_argument("text", nargs="+")
    ag.add_argument("-p", "--project", default="")
    ag.set_defaults(fn=cmd_client, cmd="agent", op="submit")
    sub.add_parser("agents", help="tâches OpenCode").set_defaults(fn=cmd_client, cmd="agent", op="list")
    ac = sub.add_parser("agent-cancel", help="annuler une tâche OpenCode (toutes si pas d'id)")
    ac.add_argument("id", nargs="?")
    ac.set_defaults(fn=cmd_client, cmd="agent", op="cancel")
    le = sub.add_parser("learn", help='apprendre un raccourci : learn travail "ouvre firefox" "bureau 2"')
    le.add_argument("name")
    le.add_argument("steps", nargs="+")
    le.add_argument("--phrase", dest="phrases", action="append", help="phrase déclencheuse en plus du nom")
    le.set_defaults(fn=cmd_client, cmd="shortcut", op="learn", timeout=30)
    sub.add_parser("shortcuts", help="raccourcis appris").set_defaults(fn=cmd_client, cmd="shortcut", op="list")
    fo = sub.add_parser("forget", help="supprimer un raccourci")
    fo.add_argument("name")
    fo.set_defaults(fn=cmd_client, cmd="shortcut", op="delete")
    sh = sub.add_parser("show", help="montrer où est un texte à l'écran")
    sh.add_argument("text", nargs="+")
    sh.set_defaults(fn=cmd_client, cmd="show", timeout=40)
    sub.add_parser("waybar", help="sortie JSON pour le module Waybar").set_defaults(fn=cmd_waybar)
    l = sub.add_parser("last", help="derniers textes dictés")
    l.add_argument("-n", type=int, default=5)
    l.add_argument("--copy", action="store_true", help="copier le dernier texte")
    l.set_defaults(fn=cmd_last)
    t = sub.add_parser("transcribe", help="tester Groq sur un fichier audio")
    t.add_argument("file")
    t.add_argument("--model")
    t.set_defaults(fn=cmd_transcribe)
    sub.add_parser("doctor", help="vérifier l'installation").set_defaults(fn=cmd_doctor)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
