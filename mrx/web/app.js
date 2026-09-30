'use strict';
/* M.R.X. UI — vanilla JS, no build step. All remote text is inserted with textContent or escaped. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const safeUrl = u => (/^https?:\/\//i.test(u || '') ? u : '#');
function h(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') e.className = v; else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
}
function md(s) {
  s = esc(s);
  s = s.replace(/```[\w-]*\n?([\s\S]*?)```/g, (_, c) => `<pre><code>${c}</code></pre>`);
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
  return s.replace(/\bhttps?:\/\/[^\s<&]+(?:&amp;[^\s<&]+)*/g, u => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);
}
const ago = t => { const m = Math.max(0, Math.round((Date.now() / 1000 - t) / 60)); return m < 1 ? 'now' : m < 60 ? m + 'm ago' : m < 1440 ? Math.round(m / 60) + 'h ago' : Math.round(m / 1440) + 'd ago'; };

/* ---------- connection ---------- */
let ws, wsRetry = 0;
function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); else toast('Not connected.'); }
function connect() {
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  ws.onopen = () => { wsRetry = 0; $('#conn').classList.add('on'); };
  ws.onclose = () => { $('#conn').classList.remove('on'); setBusy(false); setTimeout(connect, Math.min(1000 * 2 ** wsRetry++, 10000)); };
  ws.onmessage = ev => { const e = JSON.parse(ev.data); (H[e.type] || (() => {}))(e); };
}

/* ---------- chat ---------- */
const chat = $('#chat');
let cur = null, curText = '', tools = {}, busy = false;
const scroll = () => { chat.scrollTop = chat.scrollHeight; };
function addMsg(cls, text) { const m = h('div', { class: 'msg ' + cls }); m.innerHTML = md(text); chat.append(m); scroll(); return m; }
function setBusy(b) { busy = b; $('#btn-stop').hidden = !b; $('#btn-send').hidden = b; if (!b) status(''); }
function status(t) { $('#status').textContent = t || ''; }
function toast(text, cls = '') { const t = h('div', { class: 'toast ' + cls }, text); $('#toasts').append(t); setTimeout(() => t.remove(), 9000); }

const H = {
  hello(e) {
    $('#model').textContent = e.model; $('#autonomy').value = e.autonomy; setBusy(e.busy);
    if (!chat.children.length) for (const r of e.history || []) addMsg(r.role === 'user' ? 'user' : 'bot', r.text);
    if (!chat.children.length) addMsg('sys', 'M.R.X. online. Ask for anything — in any language.');
    refreshMemory(); fetchNews();
  },
  user_echo(e) { addMsg('user', e.text); cur = null; },
  turn_start() { setBusy(true); cur = null; curText = ''; },
  assistant_delta(e) { if (!cur) { cur = addMsg('bot', ''); curText = ''; } curText += e.text; cur.innerHTML = md(curText); scroll(); },
  assistant_break() { cur = null; curText = ''; },
  assistant_done(e) { cur = null; curText = ''; if (e.text) speak(e.text); },
  turn_end() { setBusy(false); },
  status(e) { status(e.text); },
  tool_call(e) {
    cur = null;
    const det = h('div', { class: 'det' }, JSON.stringify(e.input, null, 1));
    const chip = h('div', { class: 'tool', onclick: () => chip.classList.toggle('open') }, `⚙ ${e.name}`, det);
    tools[e.id] = { chip, det }; chat.append(chip); scroll(); status(`Running ${e.name}…`);
  },
  tool_result(e) {
    const t = tools[e.id]; if (!t) return;
    t.chip.classList.add(e.ok ? 'ok' : 'bad'); t.chip.firstChild.textContent = `${e.ok ? '✓' : '✗'} ${e.name}`;
    t.det.textContent += '\n→ ' + e.preview;
  },
  notice(e) { addMsg('sys', e.text); },
  error(e) { addMsg('err', e.text); },
  alert(e) { toast('⚠ ' + e.text, 'alert'); addMsg('err', e.text); },
  notification(e) { toast(e.title + ': ' + e.text.slice(0, 200)); addMsg('sys', e.title + '\n' + e.text); },
  confirm_request(e) { confirmQueue.push(e); showConfirm(); },
  confirm_resolved(e) { confirmQueue = confirmQueue.filter(c => c.id !== e.id); if (confirmCur && confirmCur.id === e.id) hideConfirm(); showConfirm(); },
  map: e => mapEvent(e), news: e => renderNews(e.items, e.title, true), youtube: e => ytEvent(e), radar: e => radarUpdate(e),
  radar_progress(e) { $('#radar-info').textContent = `${e.stage}…`; },
  radar_device_update(e) { const d = radarDevices.find(x => x.mac === e.mac); if (d) { if (e.label != null) d.label = e.label; if (e.trusted != null) d.trusted = e.trusted; renderRadarList(); } },
  browser_frame(e) { const f = $('#br-frame'); f.src = 'data:image/jpeg;base64,' + e.data; f.hidden = false; $('#br-url').textContent = `${e.title || ''} — ${e.url}`; },
};

$('#composer').addEventListener('submit', ev => { ev.preventDefault(); sendUser($('#input').value); });
$('#input').addEventListener('keydown', ev => { if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); sendUser($('#input').value); } });
$('#input').addEventListener('input', ev => { ev.target.style.height = 'auto'; ev.target.style.height = Math.min(ev.target.scrollHeight, 140) + 'px'; });
function sendUser(text) { text = text.trim(); if (!text) return; send({ type: 'user', text }); $('#input').value = ''; $('#input').style.height = 'auto'; }
$('#btn-stop').onclick = () => send({ type: 'stop' });
$('#btn-reset').onclick = () => { chat.textContent = ''; send({ type: 'reset' }); };
$('#autonomy').onchange = e => fetch('/api/autonomy/' + e.target.value, { method: 'POST' }).then(r => r.ok ? toast('Autonomy: ' + e.target.value) : toast('Failed to change autonomy'));

/* ---------- approval prompts ---------- */
let confirmQueue = [], confirmCur = null, confirmTimer = null;
function showConfirm() {
  if (confirmCur || !confirmQueue.length) return;
  confirmCur = confirmQueue[0]; const box = $('#confirm');
  box.className = confirmCur.risk; $('#cf-risk').textContent = `${confirmCur.risk} action`;
  $('#cf-title').textContent = `M.R.X. wants to use “${confirmCur.tool}”`; $('#cf-desc').textContent = confirmCur.description;
  let left = confirmCur.timeout; box.hidden = false;
  const tick = () => { $('#cf-timer').textContent = `Auto-denies in ${left}s`; if (left-- <= 0) answer(false); };
  tick(); confirmTimer = setInterval(tick, 1000); $('#cf-ok').focus();
}
function hideConfirm() { clearInterval(confirmTimer); $('#confirm').hidden = true; confirmCur = null; }
function answer(ok) { if (!confirmCur) return; send({ type: 'confirm', id: confirmCur.id, approved: ok }); confirmQueue = confirmQueue.filter(c => c.id !== confirmCur.id); hideConfirm(); showConfirm(); }
$('#cf-ok').onclick = () => answer(true); $('#cf-deny').onclick = () => answer(false);
document.addEventListener('keydown', e => { if (confirmCur && e.key === 'Escape') answer(false); });

/* ---------- tabs ---------- */
function showTab(name) {
  $$('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  $$('.tab').forEach(t => t.classList.toggle('on', t.id === 'tab-' + name));
  if (name === 'map') setTimeout(() => map.invalidateSize(), 50);
  if (name === 'radar') radarDraw();
  if (name === 'memory') refreshMemory();
}
$$('#tabs button').forEach(b => b.onclick = () => showTab(b.dataset.tab));

/* ---------- map ---------- */
const map = L.map('map', { worldCopyJump: true, zoomControl: true }).setView([22, 10], 2);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap contributors' }).addTo(map);
const custom = L.layerGroup().addTo(map), routeLayer = L.layerGroup().addTo(map);
const layerState = {};
function popup(title, ...lines) { return h('div', {}, h('b', {}, title || ''), ...lines.filter(Boolean).flatMap(l => [h('br'), l])); }
const LAYERS = {
  async quakes(g) {
    const d = await geo('quakes');
    (d.features || []).forEach(f => { const [lo, la] = f.geometry.coordinates, m = f.properties.mag || 0;
      L.circleMarker([la, lo], { radius: 3 + m * 2.2, color: '#ff5d6c', fillColor: '#ff5d6c', fillOpacity: .35, weight: 1 })
        .bindPopup(popup(`M ${m}`, f.properties.place, new Date(f.properties.time).toLocaleString())).addTo(g); });
  },
  async events(g) {
    const d = await geo('events');
    (d.features || []).forEach(f => { const [lo, la] = f.geometry.coordinates;
      L.circleMarker([la, lo], { radius: 7, color: '#ffb84d', fillColor: '#ffb84d', fillOpacity: .5, weight: 1 })
        .bindPopup(popup(f.properties.title, f.properties.category, f.properties.date)).addTo(g); });
  },
  async news(g, o) {
    const d = await geo('news', '?query=' + encodeURIComponent(o.query || ''));
    (d.features || []).forEach(f => { const [lo, la] = f.geometry.coordinates;
      L.circleMarker([la, lo], { radius: 4 + Math.min(10, Math.log2((f.properties.count || 1) + 1)), color: '#37e6ff', fillColor: '#37e6ff', fillOpacity: .35, weight: 1 })
        .bindPopup(popup(f.properties.name, `${f.properties.count || '?'} mentions`)).addTo(g); });
  },
  async flights(g) {
    const b = map.getBounds(), q = `?bbox=${[b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map(x => x.toFixed(2)).join(',')}`;
    const d = await geo('flights', q);
    (d.features || []).forEach(f => { const [lo, la] = f.geometry.coordinates, p = f.properties;
      L.circleMarker([la, lo], { radius: 3, color: '#7c5cff', fillOpacity: .9, weight: 1 })
        .bindPopup(popup(p.callsign || 'flight', p.country, p.alt_m != null ? Math.round(p.alt_m * 3.28) + ' ft' : '', p.speed_ms != null ? Math.round(p.speed_ms * 1.94) + ' kt' : '')).addTo(g); });
  },
  async rain(g) {
    const d = await geo('rain'); if (!d.host || !d.path) throw new Error('no radar frame');
    L.tileLayer(`${d.host}${d.path}/256/{z}/{x}/{y}/2/1_1.png`, { opacity: .6 }).addTo(g);
  },
};
async function geo(layer, q = '') { const r = await fetch('/api/geo/' + layer + q); const j = await r.json(); if (!r.ok || j.error) throw new Error(j.error || r.statusText); return j; }
async function loadLayer(name) {
  const st = layerState[name]; if (!st || !st.on) return;
  const g = L.layerGroup();
  try { await LAYERS[name](g, st); $('#map-msg').textContent = ''; } catch (e) { $('#map-msg').textContent = `${name}: ${e.message}`; }
  if (st.on) { if (st.group) map.removeLayer(st.group); st.group = g.addTo(map); }
}
function setLayer(name, on, query = '') {
  const st = layerState[name] || (layerState[name] = { on: false });
  st.on = on; st.query = query; clearInterval(st.timer);
  $(`#map-tools [data-layer=${name}]`).classList.toggle('on', on);
  if (on) { loadLayer(name); st.timer = setInterval(() => loadLayer(name), name === 'flights' ? 20000 : 120000); }
  else if (st.group) { map.removeLayer(st.group); st.group = null; }
}
$$('#map-tools [data-layer]').forEach(b => b.onclick = () => setLayer(b.dataset.layer, !(layerState[b.dataset.layer] || {}).on));
let flightDebounce; map.on('moveend', () => { if ((layerState.flights || {}).on) { clearTimeout(flightDebounce); flightDebounce = setTimeout(() => loadLayer('flights'), 800); } });
$('#btn-locate').onclick = () => navigator.geolocation ? navigator.geolocation.getCurrentPosition(p => { map.flyTo([p.coords.latitude, p.coords.longitude], 12); L.marker([p.coords.latitude, p.coords.longitude]).bindPopup('You are here').addTo(custom); }, () => toast('Location permission denied')) : toast('Geolocation unavailable');
function mapEvent(e) {
  showTab('map');
  if (e.action === 'fly') { map.flyTo([e.lat, e.lon], e.zoom || 10); custom.clearLayers(); L.marker([e.lat, e.lon]).bindPopup(popup(e.label || 'Here')).addTo(custom).openPopup(); }
  else if (e.action === 'layer') setLayer(e.layer, e.enabled, e.query);
  else if (e.action === 'markers') { custom.clearLayers(); const pts = (e.markers || []).map(m => { L.marker([m.lat, m.lon]).bindPopup(popup(m.label || '')).addTo(custom); return [m.lat, m.lon]; }); if (pts.length) map.fitBounds(pts, { maxZoom: 12, padding: [30, 30] }); }
  else if (e.action === 'route') { routeLayer.clearLayers(); const l = L.geoJSON(e.geometry, { style: { color: '#37e6ff', weight: 5 } }).addTo(routeLayer); map.fitBounds(l.getBounds(), { padding: [30, 30] }); custom.clearLayers(); [e.from, e.to].forEach(p => L.marker([p.lat, p.lon]).bindPopup(popup(p.name)).addTo(custom)); }
}

/* ---------- news ---------- */
function renderNews(items, title, focus) {
  const ul = $('#news-list'); ul.textContent = ''; $('#news-title').textContent = title || '';
  (items || []).forEach(it => ul.append(h('li', {}, h('a', { href: safeUrl(it.url), target: '_blank', rel: 'noopener noreferrer' }, it.title || '(untitled)'),
    h('div', { class: 'meta' }, [it.source, it.time ? ago(it.time) : ''].filter(Boolean).join(' · ')), it.summary ? h('div', { class: 'meta' }, it.summary) : null)));
  if (focus) showTab('news');
}
async function fetchNews() {
  $('#news-title').textContent = 'Loading…';
  try { const r = await (await fetch(`/api/news?topic=${$('#news-topic').value}&lang=${$('#news-lang').value}`)).json(); renderNews(r.items, r.failed_sources.length ? `${r.failed_sources.length} source(s) unreachable` : 'Live', false); }
  catch { $('#news-title').textContent = 'Could not load news'; }
}
$('#news-refresh').onclick = fetchNews; $('#news-topic').onchange = fetchNews; $('#news-lang').onchange = fetchNews;
setInterval(() => { if ($('#tab-news').classList.contains('on') && !document.hidden) fetchNews(); }, 180000);

/* ---------- YouTube ---------- */
let ytPlayer = null, ytReady = false, ytPending = null, ytQueue = [], ytHistory = [], ytNow = null;
window.onYouTubeIframeAPIReady = () => { ytReady = true; if (ytPending) { ytLoad(ytPending); ytPending = null; } };
function ytLoad(id) {
  showTab('yt');
  if (!ytReady) { ytPending = id; $('#yt-msg').textContent = 'Loading player… (needs access to youtube.com)'; return; }
  if (ytNow) ytHistory.push(ytNow); ytNow = id; $('#yt-msg').textContent = '';
  $('#yt-wrap').classList.add('playing');
  if (!ytPlayer) ytPlayer = new YT.Player('yt-player', { videoId: id, playerVars: { autoplay: 1, rel: 0, playsinline: 1 }, events: {
    onStateChange: e => { if (e.data === 0) ytNext(); },
    onError: () => { $('#yt-msg').textContent = ''; $('#yt-msg').append('This video can’t be played in an embedded player. ', h('a', { href: 'https://www.youtube.com/watch?v=' + ytNow, target: '_blank', rel: 'noopener noreferrer' }, 'Open on YouTube')); } } });
  else ytPlayer.loadVideoById(id);
}
function ytNext() { const n = ytQueue.shift(); $('#yt-queue-n').textContent = ytQueue.length; if (n) ytLoad(n); }
function ytEvent(e) {
  if (e.action === 'load') ytLoad(e.videoId);
  else if (e.action === 'queue') { if (!ytNow) ytLoad(e.videoId); else { ytQueue.push(e.videoId); $('#yt-queue-n').textContent = ytQueue.length; toast('Queued: ' + (e.title || e.videoId)); } }
  else if (e.action === 'results') { showTab('yt'); const ul = $('#yt-results'); ul.textContent = ''; e.results.forEach(r => ul.append(h('li', { class: 'click', onclick: () => ytLoad(r.id) }, h('b', {}, r.title), h('div', { class: 'meta' }, [r.channel, r.duration].filter(Boolean).join(' · '))))); }
  else if (e.action === 'control' && ytPlayer) {
    const p = ytPlayer, v = e.value;
    ({ pause: () => p.pauseVideo(), resume: () => p.playVideo(), stop: () => p.stopVideo(), mute: () => p.mute(), unmute: () => p.unMute(), volume: () => p.setVolume(v),
       seek: () => p.seekTo(p.getCurrentTime() + v, true), next: ytNext, previous: () => { const b = ytHistory.pop(); if (b) { ytNow = null; ytLoad(b); } },
       fullscreen: () => { const f = $('#yt-wrap iframe'); f && f.requestFullscreen && f.requestFullscreen(); } }[e.command] || (() => {}))();
  }
}
$('#yt-go').onclick = () => { const q = $('#yt-q').value.trim(); if (!q) return; const m = q.match(/(?:v=|youtu\.be\/|shorts\/|embed\/)([\w-]{11})/) || q.match(/^([\w-]{11})$/); if (m) ytLoad(m[1]); else sendUser('Play on YouTube: ' + q); };
$('#yt-q').addEventListener('keydown', e => { if (e.key === 'Enter') $('#yt-go').click(); });

/* ---------- radar ---------- */
let radarDevices = [], radarSel = null, radarStart = performance.now();
const hash = s => { let x = 2166136261; for (const c of s) { x ^= c.charCodeAt(0); x = Math.imul(x, 16777619); } return (x >>> 0); };
function blipPos(d, W) {
  const a = (hash(d.mac) % 3600) / 3600 * Math.PI * 2, r = d.is_self ? 0 : d.is_gateway ? .16 : .32 + ((hash(d.ip || d.mac) % 1000) / 1000) * .6;
  return { x: W / 2 + Math.cos(a) * r * W / 2, y: W / 2 + Math.sin(a) * r * W / 2, a };
}
const blipColor = d => d.is_self ? '#37e6ff' : d.trusted ? '#3ddc97' : d.new ? '#ff5d6c' : '#ffb84d';
function radarUpdate(e) {
  radarDevices = e.devices || []; radarSel = null;
  $('#radar-info').textContent = `${e.network} · ${radarDevices.length} devices · ${e.took_s}s`; $('#radar-scan').disabled = false;
  renderRadarList(); if (!$('#tab-radar').classList.contains('on')) showTab('radar');
  const bad = radarDevices.filter(d => d.new && !d.trusted && !d.is_self); if (bad.length) toast(`${bad.length} new device(s) on your network`, 'alert');
}
function renderRadarList() {
  const ul = $('#radar-list'); ul.textContent = '';
  radarDevices.forEach(d => {
    const name = d.label || d.hostname || d.vendor || (d.kind !== 'unknown' ? d.kind : d.mac);
    ul.append(h('li', { class: 'click', onclick: () => { radarSel = d.mac; renderRadarDetail(); } }, h('b', {}, name),
      d.is_self ? h('span', { class: 'badge me' }, 'this PC') : null, d.is_gateway ? h('span', { class: 'badge me' }, 'router') : null,
      d.new && !d.trusted ? h('span', { class: 'badge new' }, 'new') : null, d.trusted && !d.is_self ? h('span', { class: 'badge ok' }, 'trusted') : null,
      h('div', { class: 'meta' }, `${d.ip} · ${d.kind}${d.vendor ? ' · ' + d.vendor : d.randomized_mac ? ' · private MAC' : ''}`)));
  });
  renderRadarDetail();
}
function renderRadarDetail() {
  const box = $('#radar-detail'); box.textContent = ''; const d = radarDevices.find(x => x.mac === radarSel);
  if (!d) { box.append(h('span', { class: 'muted' }, 'Click a device for details.')); return; }
  const label = h('input', { value: d.label || '', placeholder: 'Name this device (e.g. Dad’s phone)' }), trust = h('input', { type: 'checkbox' }); trust.checked = !!d.trusted;
  box.append(h('div', {}, h('b', {}, d.kind), ` (${d.confidence} confidence)`), h('div', { class: 'meta' }, `MAC ${d.mac}`), h('div', { class: 'meta' }, `IP ${d.ip}${d.hostname ? ' · ' + d.hostname : ''}`),
    h('div', { class: 'meta' }, `Vendor: ${d.vendor || (d.randomized_mac ? 'randomized (phone/laptop privacy address)' : 'unknown')}`),
    d.open_services && d.open_services.length ? h('div', { class: 'meta' }, 'Open: ' + d.open_services.join(', ')) : null,
    d.ssdp && d.ssdp.server ? h('div', { class: 'meta' }, 'UPnP: ' + d.ssdp.server) : null, label,
    h('label', {}, trust, ' Trusted device '), ' ', h('button', { onclick: () => { send({ type: 'radar_label', mac: d.mac, label: label.value, trusted: trust.checked }); d.label = label.value; d.trusted = trust.checked; renderRadarList(); } }, 'Save'));
}
function radarDraw() {
  const c = $('#radar-canvas'); if (!$('#tab-radar').classList.contains('on')) return; const ctx = c.getContext('2d'), W = c.width, t = (performance.now() - radarStart) / 1000, sweep = (t * .9) % (Math.PI * 2);
  ctx.clearRect(0, 0, W, W); ctx.strokeStyle = 'rgba(61,220,151,.25)'; ctx.lineWidth = 1;
  for (let i = 1; i <= 4; i++) { ctx.beginPath(); ctx.arc(W / 2, W / 2, i * W / 8, 0, 7); ctx.stroke(); }
  ctx.beginPath(); ctx.moveTo(W / 2, 0); ctx.lineTo(W / 2, W); ctx.moveTo(0, W / 2); ctx.lineTo(W, W / 2); ctx.stroke();
  const g = ctx.createConicGradient ? ctx.createConicGradient(sweep - 1.2, W / 2, W / 2) : null;
  if (g) { g.addColorStop(0, 'rgba(61,220,151,0)'); g.addColorStop(.19, 'rgba(61,220,151,.35)'); g.addColorStop(.2, 'rgba(61,220,151,0)'); ctx.fillStyle = g; ctx.beginPath(); ctx.arc(W / 2, W / 2, W / 2, 0, 7); ctx.fill(); }
  ctx.strokeStyle = 'rgba(61,220,151,.9)'; ctx.beginPath(); ctx.moveTo(W / 2, W / 2); ctx.lineTo(W / 2 + Math.cos(sweep) * W / 2, W / 2 + Math.sin(sweep) * W / 2); ctx.stroke();
  radarDevices.forEach(d => {
    const p = blipPos(d, W), col = blipColor(d); let diff = (sweep - p.a) % (Math.PI * 2); if (diff < 0) diff += Math.PI * 2;
    const glow = Math.max(0, 1 - diff / 3.2), sel = d.mac === radarSel;
    ctx.globalAlpha = .35 + .65 * glow; ctx.fillStyle = col; ctx.shadowColor = col; ctx.shadowBlur = 10 * glow + 3;
    ctx.beginPath(); ctx.arc(p.x, p.y, sel ? 8 : 5.5, 0, 7); ctx.fill(); ctx.shadowBlur = 0;
    if (sel || glow > .7) { ctx.globalAlpha = .9; ctx.fillStyle = '#dbe7ff'; ctx.font = '12px system-ui'; ctx.fillText(d.label || d.hostname || d.vendor || (d.kind !== 'unknown' ? d.kind : d.ip), p.x + 9, p.y - 7); }
    ctx.globalAlpha = 1;
  });
  requestAnimationFrame(radarDraw);
}
$('#radar-canvas').addEventListener('click', ev => {
  const c = ev.target, r = c.getBoundingClientRect(), x = (ev.clientX - r.left) * c.width / r.width, y = (ev.clientY - r.top) * c.height / r.height;
  let best = null, bd = 18; radarDevices.forEach(d => { const p = blipPos(d, c.width), dist = Math.hypot(p.x - x, p.y - y); if (dist < bd) { bd = dist; best = d; } });
  radarSel = best ? best.mac : null; renderRadarDetail();
});
$('#radar-scan').onclick = () => { send({ type: 'radar_scan' }); $('#radar-info').textContent = 'Scanning…'; $('#radar-scan').disabled = true; setTimeout(() => $('#radar-scan').disabled = false, 60000); };
$('#radar-live').onchange = e => send({ type: 'radar_live', enabled: e.target.checked });

/* ---------- memory ---------- */
async function refreshMemory() {
  try {
    const facts = await (await fetch('/api/memory')).json(), ul = $('#mem-list'); ul.textContent = '';
    if (!facts.length) ul.append(h('li', { class: 'muted' }, 'Nothing yet. Tell M.R.X. things worth remembering.'));
    facts.forEach(f => ul.append(h('li', {}, f.text, h('div', { class: 'meta' }, `#${f.id}${f.tags ? ' · ' + f.tags : ''} `, h('a', { href: '#', onclick: async ev => { ev.preventDefault(); await fetch('/api/memory/' + f.id, { method: 'DELETE' }); refreshMemory(); } }, 'forget')))));
  } catch { /* ignore */ }
}
$('#mem-refresh').onclick = refreshMemory;

/* ---------- voice: input (Web Speech API) and output (speechSynthesis) ---------- */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition; let rec = null;
$('#btn-mic').onclick = () => {
  if (!SR) return toast('Voice input needs a Chromium-based browser (Chrome, Edge, Brave).');
  if (rec) { rec.stop(); return; }
  rec = new SR(); rec.lang = $('#lang').value || navigator.language || 'en-US'; rec.interimResults = true; rec.continuous = false;
  rec.onstart = () => $('#btn-mic').classList.add('rec');
  rec.onresult = ev => { const r = ev.results[ev.results.length - 1]; $('#input').value = r[0].transcript; if (r.isFinal) sendUser(r[0].transcript); };
  rec.onerror = ev => toast('Voice: ' + ev.error);
  rec.onend = () => { $('#btn-mic').classList.remove('rec'); rec = null; };
  rec.start();
};
const SCRIPTS = [[/[؀-ۿ]/, 'ar'], [/[ऀ-ॿ]/, 'hi'], [/[ঀ-৿]/, 'bn'], [/[஀-௿]/, 'ta'], [/[ఀ-౿]/, 'te'], [/[぀-ヿ]/, 'ja'], [/[가-힯]/, 'ko'],
  [/[一-鿿]/, 'zh'], [/[Ѐ-ӿ]/, 'ru'], [/[฀-๿]/, 'th'], [/[֐-׿]/, 'he'], [/[Ͱ-Ͽ]/, 'el']];
function speak(text) {
  if (!$('#tts').checked || !window.speechSynthesis) return;
  const clean = text.replace(/```[\s\S]*?```/g, ' code block ').replace(/https?:\/\/\S+/g, ' link ').replace(/[*_`#>]/g, '');
  const u = new SpeechSynthesisUtterance(clean); const s = SCRIPTS.find(([re]) => re.test(clean));
  u.lang = s ? s[1] : ($('#lang').value || navigator.language || 'en-US');
  const v = speechSynthesis.getVoices().find(v => v.lang.toLowerCase().startsWith(u.lang.slice(0, 2).toLowerCase())); if (v) u.voice = v;
  speechSynthesis.cancel(); speechSynthesis.speak(u);
}
try { $('#tts').checked = localStorage.getItem('mrx-tts') === '1'; } catch { /* storage blocked */ }
$('#tts').onchange = e => { try { localStorage.setItem('mrx-tts', e.target.checked ? '1' : '0'); } catch { /* ignore */ } if (!e.target.checked) speechSynthesis.cancel(); };

connect();
