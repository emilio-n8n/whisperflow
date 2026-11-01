# Installation et dépannage

## Clés (`~/.config/whisperflow/secrets.env`, chmod 600)
```
GROQ_API_KEY=gsk_...         # obligatoire (dictée et commandes vocales)
OPENCODE_API_KEY=...          # facultative : Jev via OpenCode Zen (jev-1.13 ou jev-1.13-free)
GEMINI_API_KEY=AIza...       # facultative : Gemini Live
```
Après une modification : `systemctl --user restart whisperflow`.

## Problèmes fréquents
- **Le texte ne s'insère pas dans un terminal** : dans `config.toml`, mets `[inject] method = "clipboard"` et `paste_shortcut = "ctrl+shift+v"`.
- **Raccourci sans effet** : `hyprctl binds | grep whisperflow`. Si SUPER+CTRL+D/E/G est déjà pris par Omarchy, change la touche dans `~/.config/hypr/whisperflow.conf`.
- **« Rien entendu »** : choisis le bon micro (`wpctl status`, puis `wpctl set-default <id>`) ou baisse `silence_rms`.
- **Notifications Oui/Non absentes** : il faut mako (Omarchy l'a par défaut). Sans réponse, c'est toujours « non ».
- **Clics (computer use)** : ydotool a besoin de ydotoold et de /dev/uinput.
  ```
  sudo usermod -aG input $USER     # puis se reconnecter
  systemctl --user enable --now ydotool
  ```
- **Overlay absent** : `sudo pacman -S python-gobject gtk4-layer-shell`. Sans overlay, WhisperFlow déplace le pointeur sur la cible.
- **Gemini Live se coupe tout de suite** : le journal donne la raison exacte renvoyée par Google (clé, quota, modèle).
- **Accès iPhone sans URL** : `cloudflared` doit être installé (`sudo pacman -S cloudflared`) ; le journal affiche la sortie du tunnel. Une URL `trycloudflare.com` change à chaque démarrage.
- **Le téléphone n'entend rien** : Safari exige HTTPS (c'est le cas du tunnel) et un appui sur **Parler** pour débloquer l'audio ; vérifie aussi que le son du téléphone n'est pas en silencieux.
- **Jev « Pas sûr »** : reformule plus directement, ou crée un raccourci appris.

## Désinstaller
`./uninstall.sh`. La configuration et les clés restent dans `~/.config/whisperflow`.
