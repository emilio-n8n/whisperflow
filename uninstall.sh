#!/usr/bin/env bash
set -u
CFG="${XDG_CONFIG_HOME:-$HOME/.config}"
systemctl --user disable --now whisperflow 2>/dev/null
rm -f "$CFG/systemd/user/whisperflow.service" "$CFG/hypr/whisperflow.conf"
sed -i '\|source = ~/.config/hypr/whisperflow.conf|d' "$CFG/hypr/hyprland.conf" 2>/dev/null
pipx uninstall whisperflow 2>/dev/null
echo "Désinstallé. Config et clé conservées dans $CFG/whisperflow (supprime-les à la main si besoin)."
