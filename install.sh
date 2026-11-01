#!/usr/bin/env bash
# Installation WhisperFlow 0.3 sur Omarchy (Arch + Hyprland). Relançable sans risque.
set -euo pipefail
cd "$(dirname "$0")"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}"
SECRETS="$CFG/whisperflow/secrets.env"

echo "==> Paquets système (sudo)"
sudo pacman -S --needed --noconfirm python python-pipx pipewire-audio libpulse wtype wl-clipboard libnotify \
  grim tesseract tesseract-data-fra tesseract-data-eng playerctl python-gobject gtk4-layer-shell ydotool cloudflared

echo "==> WhisperFlow (pipx, avec accès à python-gobject pour l'overlay)"
pipx install --force --system-site-packages .
pipx ensurepath >/dev/null || true

echo "==> Configuration"
mkdir -p "$CFG/whisperflow" "$CFG/systemd/user"
[ -f "$CFG/whisperflow/config.toml" ] || cp packaging/config.toml "$CFG/whisperflow/config.toml"
umask 077; touch "$SECRETS"; chmod 600 "$SECRETS"
ask_key() {  # $1 = nom, $2 = description, $3 = obligatoire ?
  grep -q "^$1=" "$SECRETS" && return 0
  read -rsp "$2 (saisie masquée${3:+, obligatoire}${3:-, Entrée pour passer}) : " KEY; echo
  if [ -n "$KEY" ]; then printf '%s=%s\n' "$1" "$KEY" >> "$SECRETS"
  elif [ -n "${3:-}" ]; then echo "Clé obligatoire." >&2; exit 1; fi
}
ask_key GROQ_API_KEY "Clé Groq (dictée, gsk_...)" oui
ask_key OPENCODE_API_KEY "Clé OpenCode Zen (Jev, commandes floues)"
ask_key GEMINI_API_KEY "Clé Gemini (Gemini Live)"

cp packaging/hyprland-whisperflow.conf "$CFG/hypr/whisperflow.conf"
grep -q "whisperflow.conf" "$CFG/hypr/hyprland.conf" 2>/dev/null || \
  echo 'source = ~/.config/hypr/whisperflow.conf' >> "$CFG/hypr/hyprland.conf"

cp packaging/whisperflow.service "$CFG/systemd/user/whisperflow.service"
systemctl --user daemon-reload
systemctl --user enable --now whisperflow
systemctl --user restart whisperflow
systemctl --user enable --now ydotool 2>/dev/null || echo "(ydotool : voir docs/INSTALL.md, utile seulement pour computer use)"
hyprctl reload >/dev/null 2>&1 || true

echo
"$HOME/.local/bin/whisperflow" doctor || true
cat <<'TXT'

Raccourcis :
  SUPER+CTRL+D  dictée            SUPER+CTRL+E  commande vocale
  SUPER+CTRL+G  Gemini Live       SUPER+CTRL+X  annuler
  SUPER+CTRL+Échap  arrêt d'urgence
TXT
