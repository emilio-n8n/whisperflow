"""Page servie au téléphone par remote.py (aucune ressource externe)."""

PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0b0b0f">
<title>WhisperFlow</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; margin: 0; }
  body {
    background: #0b0b0f; color: #f4f4f5;
    font: 16px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    display: flex; flex-direction: column;
    padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
    overflow: hidden;
  }
  header {
    display: flex; align-items: center; gap: .55rem;
    padding: .8rem 1rem .5rem; font-weight: 600; letter-spacing: .01em;
  }
  .dot { width: .6rem; height: .6rem; border-radius: 50%; background: #52525b; transition: background .2s; }
  .dot.on { background: #34d399; box-shadow: 0 0 0 .25rem rgba(52,211,153,.15); }
  .dot.busy { background: #fbbf24; }
  .dot.err { background: #f87171; }
  #state { margin-left: auto; font-size: .8rem; font-weight: 400; color: #a1a1aa; }
  #state.err { color: #fca5a5; }
  main { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: .6rem; padding: 0 .8rem .4rem; }
  #screenBox {
    flex: 1; min-height: 0; display: flex; align-items: center; justify-content: center;
    background: #111118; border-radius: 1rem; overflow: hidden; border: 1px solid #27272a;
  }
  #screen { max-width: 100%; max-height: 100%; object-fit: contain; display: none; }
  #empty { color: #52525b; font-size: .85rem; padding: 2rem; text-align: center; }
  #log {
    flex: 0 0 22vh; overflow-y: auto; font-size: .85rem; line-height: 1.45;
    background: #0e0e14; border: 1px solid #27272a; border-radius: .9rem; padding: .6rem .75rem;
  }
  #log:empty::before { content: "La conversation apparaîtra ici."; color: #52525b; }
  #log p { margin: 0 0 .35rem; color: #d4d4d8; }
  #level { height: 3px; margin: 0 1rem .5rem; background: #18181b; border-radius: 2px; overflow: hidden; }
  #level i { display: block; height: 100%; width: 0; background: #34d399; transition: width .08s linear; }
  #log b { color: #a1a1aa; font-weight: 600; }
  footer { display: flex; gap: .6rem; padding: .55rem .8rem 1rem; }
  button {
    flex: 1; border: 0; border-radius: 1rem; padding: .95rem 1rem;
    font-size: 1rem; font-weight: 600; color: #0b0b0f; background: #f4f4f5;
    transition: transform .08s, opacity .15s;
  }
  button:active { transform: scale(.97); }
  button:disabled { opacity: .35; }
  #talk { background: #34d399; }
  #stop { background: #27272a; color: #f4f4f5; }
  #shot { flex: 0 0 auto; padding: .95rem 1.1rem; background: #27272a; color: #f4f4f5; }
  .note { padding: 0 1rem .7rem; font-size: .72rem; color: #71717a; }
</style>
</head>
<body>
<header><span class="dot" id="dot"></span><span>WhisperFlow</span><span id="state">prêt</span></header>
<div id="level"><i></i></div>
<main>
  <div id="screenBox"><span id="empty">L'écran du PC apparaîtra ici.</span><img id="screen" alt="écran du PC"></div>
  <div id="log"></div>
</main>
<footer>
  <button id="talk">Parler</button>
  <button id="stop" disabled>Arrêter</button>
  <button id="shot" title="Capture fraîche">📷</button>
</footer>
<div class="note">Lien privé et temporaire : ne le partage pas. Les actions s'exécutent sur le PC.</div>
<script>
const $ = (s) => document.querySelector(s);
const dot = $('#dot'), stateEl = $('#state'), screenEl = $('#screen'), emptyEl = $('#empty'),
      logEl = $('#log'), talkBtn = $('#talk'), stopBtn = $('#stop'), shotBtn = $('#shot'),
      levelBar = $('#level i'), screenBox = $('#screenBox');
const token = new URLSearchParams(location.search).get('t') || '';
if (token) history.replaceState(null, '', location.pathname);

let ws = null, ctxIn = null, ctxOut = null, micStream = null, proc = null, zeroGain = null,
    nextAt = 0, sources = [], wakeLock = null, currentWho = '', lastP = null, streaming = false;

function setState(text, kind) {
  stateEl.textContent = text;
  stateEl.className = kind === 'err' ? 'err' : '';
  dot.className = 'dot' + (kind === 'err' ? ' err' : kind === 'busy' ? ' busy' : streaming ? ' on' : '');
}
function logLine(who, text) {
  const label = who === 'gemini' ? 'Gemini' : 'Moi';
  if (lastP && currentWho === who) { lastP.textContent += text; return; }
  const p = document.createElement('p');
  const b = document.createElement('b');
  b.textContent = label + ' · ';
  p.appendChild(b);
  p.appendChild(document.createTextNode(text));
  logEl.appendChild(p);
  logEl.scrollTop = logEl.scrollHeight;
  currentWho = who; lastP = p;
}
function clamp16(v) { return Math.max(-1, Math.min(1, v)) * 32767; }
function to16k(input, rate) {
  if (rate === 16000) {
    const same = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) same[i] = clamp16(input[i]);
    return same;
  }
  const ratio = rate / 16000, outLen = Math.floor(input.length / ratio);
  const out = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const a = Math.floor(i * ratio), b = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0; const n = Math.max(1, b - a);
    for (let j = a; j < b; j++) sum += input[j];
    out[i] = clamp16(sum / n);
  }
  return out;
}
async function openOut() {
  if (!ctxOut) ctxOut = new (window.AudioContext || window.webkitAudioContext)();
  if (ctxOut.state === 'suspended') await ctxOut.resume();
  const unlock = ctxOut.createBufferSource();
  unlock.buffer = ctxOut.createBuffer(1, 1, ctxOut.sampleRate);
  unlock.connect(ctxOut.destination);
  unlock.start(0);
  nextAt = ctxOut.currentTime;
}
async function openMic() {
  if (micStream) return;
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
  });
  ctxIn = new (window.AudioContext || window.webkitAudioContext)();
  if (ctxIn.state === 'suspended') await ctxIn.resume();
  const src = ctxIn.createMediaStreamSource(micStream);
  proc = ctxIn.createScriptProcessor(4096, 1, 1);
  zeroGain = ctxIn.createGain();
  zeroGain.gain.value = 0;
  proc.onaudioprocess = (ev) => {
    const input = ev.inputBuffer.getChannelData(0);
    let sum = 0;
    for (let i = 0; i < input.length; i += 8) sum += input[i] * input[i];
    levelBar.style.width = Math.min(100, Math.sqrt(sum / (input.length / 8)) * 320) + '%';
    if (!ws || ws.readyState !== 1) return;
    const pcm = to16k(input, ctxIn.sampleRate);
    if (pcm.length) ws.send(pcm.buffer);
  };
  src.connect(proc); proc.connect(zeroGain); zeroGain.connect(ctxIn.destination);
}
function closeMic() {
  if (proc) { proc.onaudioprocess = null; try { proc.disconnect(); } catch (e) {} proc = null; }
  if (zeroGain) { try { zeroGain.disconnect(); } catch (e) {} zeroGain = null; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  if (ctxIn) { try { ctxIn.close(); } catch (e) {} ctxIn = null; }
  levelBar.style.width = '0%';
}
function stopPlayback() {
  for (const s of sources) { try { s.stop(); } catch (e) {} }
  sources = [];
  nextAt = ctxOut ? ctxOut.currentTime : 0;
}
function play(buf) {
  if (!ctxOut) return;
  const pcm = new Int16Array(buf);
  if (!pcm.length) return;
  const ab = ctxOut.createBuffer(1, pcm.length, 24000);
  const ch = ab.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
  const src = ctxOut.createBufferSource();
  src.buffer = ab; src.connect(ctxOut.destination);
  const at = Math.max(ctxOut.currentTime + 0.03, nextAt);
  src.start(at);
  nextAt = at + ab.duration;
  sources.push(src);
  src.onended = () => { sources = sources.filter((x) => x !== src); };
}
function onJson(m) {
  if (m.type === 'ready') { streaming = true; setState('Gemini écoute'); talkBtn.disabled = true; stopBtn.disabled = false; }
  else if (m.type === 'frame') { screenEl.src = 'data:image/jpeg;base64,' + m.data; screenEl.style.display = 'block'; emptyEl.style.display = 'none'; }
  else if (m.type === 'text') logLine(m.who, m.text);
  else if (m.type === 'interrupted') { currentWho = ''; lastP = null; stopPlayback(); }
  else if (m.type === 'end') { logLine('gemini', ' — session terminée : ' + (m.reason || '')); shutdown(false); }
  else if (m.type === 'error') { setState(m.message || 'erreur', 'err'); }
  else if (m.type === 'pong') { /* keepalive */ }
}
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws?t=' + encodeURIComponent(token));
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { setState('connexion à Gemini…', 'busy'); };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') { try { onJson(JSON.parse(ev.data)); } catch (e) {} }
    else play(ev.data);
  };
  ws.onerror = () => setState('erreur réseau', 'err');
  ws.onclose = () => { shutdown(false); };
}
async function shutdown(notifyServer) {
  if (notifyServer && ws && ws.readyState === 1) { try { ws.send(JSON.stringify({ type: 'stop' })); } catch (e) {} }
  if (ws) { try { ws.close(); } catch (e) {} ws = null; }
  streaming = false;
  closeMic(); stopPlayback();
  talkBtn.disabled = false; stopBtn.disabled = true;
  setState('arrêté');
  if (wakeLock) { try { await wakeLock.release(); } catch (e) {} wakeLock = null; }
}
async function start() {
  talkBtn.disabled = true;
  setState('ouverture du micro…', 'busy');
  try {
    if (!token) throw new Error('Jeton absent : rouvre le lien complet.');
    await openOut();
    await openMic();
    connect();
    if ('wakeLock' in navigator) { try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {} }
  } catch (e) {
    setState(e.message || String(e), 'err');
    talkBtn.disabled = false;
    closeMic();
  }
}
talkBtn.addEventListener('click', start);
stopBtn.addEventListener('click', () => shutdown(true));
shotBtn.addEventListener('click', () => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'frame' })); });
screenBox.addEventListener('click', () => {
  if (screenEl.style.display !== 'block') return;
  if (document.fullscreenElement) { document.exitFullscreen(); return; }
  screenBox.requestFullscreen && screenBox.requestFullscreen().catch(() => {});
});
document.addEventListener('visibilitychange', async () => {
  if (document.visibilityState === 'visible' && streaming && 'wakeLock' in navigator && !wakeLock) {
    try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
  }
});
window.addEventListener('beforeunload', () => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'stop' })); });
setState('prêt');
</script>
</body>
</html>
"""
