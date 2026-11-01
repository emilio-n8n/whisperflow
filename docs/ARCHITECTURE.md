# Architecture WhisperFlow 0.3

```
Hyprland binds ─► whisperflow <cmd> ─► socket Unix 0600 ─► daemon
                                                          ├─ Dictation (machine d'états, jeton d'annulation)
                                                          │    mode dictée   : Groq → nettoyage → wtype/presse-papiers
                                                          │    mode commande : Groq → Brain.handle_text
                                                          └─ Brain
                                                               ├─ Router : raccourcis → règles FR → Jev (Decisions API)
                                                               ├─ Executor : catalogue fermé, argv fixes, confirmations
                                                               ├─ AgentManager : OpenCode via ACP v1 (ndjson stdio)
                                                               ├─ Annotator : grim + tesseract + overlay GTK4 layer-shell
                                                               └─ LiveSession : Gemini Live (WebSocket stdlib)
                                                                    └─ RemoteServer : iPhone ⇄ WebSocket local ⇄ cloudflared
```

## Fichiers
| Module | Rôle |
|---|---|
| config.py | TOML + secrets (fichier 0600 exigé), sections stt/audio/inject/ui/history/jev/actions/agent/live |
| audio.py, stt.py, textproc.py, inject.py, ui.py, history.py | Niveau 1 (dictée) |
| daemon.py, cli.py | Service, socket, commandes |
| actions.py | Catalogue (17 actions), Confirmer (notification Oui/Non) |
| router.py | Règles locales, client Jev, raccourcis appris |
| agents.py | Client ACP, sessions persistantes, permissions, annulation |
| ws.py | Client WebSocket RFC 6455 (TLS, ping/pong, fragments, close) |
| screen.py | Capture, OCR, correspondance de cible, géométrie Hyprland, pointeur, touches |
| overlay.py | Cadre d'annotation transparent aux clics |
| live.py | Session Gemini Live et outils locaux |
| remote.py, remote_page.py | Accès iPhone : HTTP + WebSocket local, tunnel cloudflared, page audio/écran |
| brain.py | Assemblage, arrêt d'urgence |

## Protocoles vérifiés
- **Jev (OpenCode Zen)** : `POST https://opencode.ai/inference/systemone/v1/systemone`, Bearer `OPENCODE_API_KEY`, `{model: "jev-1.13-free"|"jev-1.13", state, questions:{action:{type:"choice", instructions, criteria}}}`. Réponse : `answers.action.{choice, confidence, probabilities}`. Zen sert Jev 1.13 (pas 1.14).
- **ACP v1** :
  - `initialize` (protocolVersion 1, capacités fs/terminal à false), puis `session/new {cwd, mcpServers:[]}` et `session/load` si l'agent le permet ;
  - `session/prompt` jusqu'au `stopReason` ;
  - `session/update` (agent_message_chunk, tool_call, tool_call_update) ;
  - `session/request_permission` : réponse `{outcome:{outcome:"selected", optionId}}`, jamais allow_always ;
  - `session/cancel`.
- **Gemini Live** : wss `…BidiGenerateContent?key=`.
  - Le setup `{model, generationConfig:{responseModalities, speechConfig}, systemInstruction, tools, input/outputAudioTranscription}` a été **accepté par le vrai serveur** le 2026-10-26 : refus seulement sur la clé de test. `responseModalities` placé à la racine est refusé.
  - Entrées : audio PCM 16 kHz (blocs de 100 ms), JPEG ≤ 1 image/s.
  - Sorties : PCM 24 kHz, `interrupted`, `toolCall`, `toolCallCancellation`, `goAway`.
  - Réponses d'outil : `{status, retryable, message}`, comme le recommande le guide 3.8 Live.

## Sécurité
- Aucune exécution de code arbitraire. Les modèles choisissent dans un catalogue ; chaque action valide ses paramètres.
- Le texte de l'écran est une donnée non fiable : consigne système à Gemini, aucune exécution directe.
- Les clés ne passent ni par les journaux ni par les erreurs (masquées).
- Le socket et l'historique sont privés (0600). L'audio est supprimé après la transcription.
