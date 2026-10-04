'use strict';
// openSeedling touchscreen summary. Polls /api/status and draws everything on
// one screen; the buttons post to the same endpoints as the dashboard.

let S = null, lastOk = 0, failing = false;
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

const metric = () => !!(S && S.settings && S.settings.units === 'metric');
const tDisp = c => metric() ? c : c * 9 / 5 + 32;
const tUnit = () => metric() ? '\u00b0C' : '\u00b0F';
const fToDisp = f => metric() ? (f - 32) * 5 / 9 : f;
const hhmm = iso => iso ? new Date(iso).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}) : '';

function probePct(cal, v) {
  if (!cal || cal.wet == null || cal.dry == null || (cal.dry - cal.wet) < 0.05 || v == null) return null;
  return Math.max(0, Math.min(100, 100 * (cal.dry - v) / (cal.dry - cal.wet)));
}
function trayMoisture(t) {
  const v = (S.probe_filtered || {})[t];
  const own = probePct(((S.settings || {}).probe_cal || {})[t], v);
  if (own != null) return {pct: own, approx: false};
  const d = probePct(S.probe_default_cal, v);
  return d == null ? null : {pct: d, approx: true};
}
function canopyOf(t) {
  const c = ((S.sensors || {})['canopy:' + t] || {}).value;
  return c == null ? 'no moisture probe' : `canopy ${Math.round(c)}%`;
}
function trayLabel(t) {
  const tr = ((S.settings || {}).trays || {})[t] || {};
  return tr.label || ('Tray ' + t);
}

function setSeg(ctl, mode) {
  document.querySelectorAll(`.seg[data-ctl="${ctl}"] button`).forEach(b =>
    b.classList.toggle('on', b.dataset.v === mode));
}

// ---- drawing ---------------------------------------------------------------

function drawEnv() {
  const f = S.filtered || {};
  const soil = f['temp:soil'], air = f['temp:air'];
  $('soil').innerHTML = soil == null ? '--' : `${tDisp(soil).toFixed(1)}<small>${tUnit()}</small>`;
  const h = S.heat || {};
  $('soilsub').textContent = h.use ? `heat mat target ${fToDisp(h.target_f).toFixed(0)}${tUnit()}` : '';
  $('air').innerHTML = air == null ? '--' : `${tDisp(air).toFixed(1)}<small>${tUnit()}</small>`;
  const rh = f.humidity, pt = S.pressure_tendency || {};
  $('airsub').textContent = [rh != null ? `${Math.round(rh)}% humidity` : '',
                             pt.words ? `pressure ${pt.words}` : ''].filter(Boolean).join(' \u00b7 ');
}

function drawHeat() {
  const h = S.heat || {};
  $('t-heat').style.display = h.use ? '' : 'none';
  if (!h.use) return;
  const duty = h.duty == null ? null : Math.round(h.duty * 100);
  const st = h.on === true ? 'On' : h.on === false ? 'Off' : 'Unknown';
  let txt = `${st}`;
  if (h.mode === 'auto' && duty != null) txt += ` <span class="dim">\u00b7 ${duty}% power</span>`;
  else if (h.mode !== 'auto') txt += ` <span class="dim">\u00b7 held ${h.mode}</span>`;
  $('heat').innerHTML = txt;
  $('heatbar').style.width = (h.mode === 'off' ? 0 : (duty == null ? (h.on ? 100 : 0) : duty)) + '%';
  setSeg('heat', h.mode);
}

function lightOf(su) {
  if (su.light === 'second') {
    const l = S.light2 || {};
    return {level: l.level || 0, mode: l.override || 'auto', on: l.start, off: l.end, ctl: 'light2'};
  }
  return {level: S.brightness || 0, mode: S.light_override || 'auto',
          on: hhmm(su.on || S.on), off: hhmm(su.off || S.off), ctl: 'light'};
}

function drawSetups() {
  const sets = (S.setups && S.setups.length) ? S.setups
    : [{id: 'main', name: 'Grow', light: 'main', band: [0, 0], day: S.day_light || {}, trays: []}];
  const allTrays = Object.keys((S.settings || {}).trays || {}).sort();
  const thr = ((S.water || {}).moisture_threshold_pct) || 30;
  const armed = new Set(((S.water || {}).armed) || []);
  let h = '';
  sets.forEach((su, i) => {
    const L = lightOf(su);
    const lit = L.level > 0;
    const lstate = lit ? `On ${Math.round(L.level)}%`
      : `Off <span class="dim">\u00b7 on at ${esc(L.on)}</span>`;
    const dli = (su.day || {}).dli, band = su.band || [0, 0];
    const top = Math.max(band[1] * 1.5, (dli || 0) * 1.1, 1);
    const trays = (su.trays && su.trays.length) ? su.trays : (sets.length === 1 ? allTrays : []);
    const th = trays.map(t => {
      const m = trayMoisture(t), wt = ((S.water || {}).trays || {})[t] || {};
      const fl = wt.float == null ? '' : (wt.float >= 1 ? 'float low' : 'float full');
      const dry = m && m.pct < thr;
      return `<div class="tray${dry ? ' dry' : ''}"><div class="tn">${esc(trayLabel(t))}</div>`
        + (m ? `<div class="tm">${(m.approx ? '~' : '') + Math.round(m.pct)}<small>%</small></div>`
             : `<div class="tnp">${canopyOf(t)}</div>`)
        + `<div class="ts">${[fl, armed.has(t) ? 'auto-water on' : ''].filter(Boolean).join(' \u00b7 ') || '&nbsp;'}</div></div>`;
    }).join('');
    h += `<section class="tile setup">
      <div class="tline"><div style="min-width:0">
        <div class="head"><span class="name">${esc(su.name || 'Grow')}</span>
          <span class="light">${esc(su.light_label || '')}</span></div>
        <div class="state">${lstate}</div></div>
        <div class="seg" data-ctl="${L.ctl}" data-i="${i}"><button data-v="auto">Auto</button><button data-v="on">On</button><button data-v="off">Off</button></div>
      </div>
      <div class="dliline"><span>Light today <b>${dli == null ? '--' : dli.toFixed(1)}</b> mol</span>
        <span>${band[1] ? `target ${band[0]}\u2013${band[1]}` : ''}</span></div>
      <div class="bar">${band[1] ? `<span class="band" style="left:${100 * band[0] / top}%;width:${100 * (band[1] - band[0]) / top}%"></span>` : ''}
        <i style="width:${Math.min(100, 100 * (dli || 0) / top)}%"></i></div>
      ${th ? `<div class="trays">${th}</div>` : ''}
    </section>`;
  });
  const wrap = $('setups');
  if (wrap.dataset.html !== h) { wrap.innerHTML = h; wrap.dataset.html = h; }
  sets.forEach((su, i) => {
    const L = lightOf(su);
    wrap.querySelectorAll(`.seg[data-i="${i}"] button`).forEach(b =>
      b.classList.toggle('on', b.dataset.v === L.mode));
  });
}

function drawFanRes() {
  const f = S.fan || {};
  $('fan').innerHTML = f.on ? `Running <span class="dim">${Math.round(f.speed || 0)}%</span>` : 'Off';
  setSeg('fan', f.mode || 'auto');
  const r = ((S.water || {}).reservoir) || {};
  const words = {full: 'Full', ok: 'OK', empty: 'Empty', fault: 'Sensor fault'};
  $('res').innerHTML = r.wired === false ? '<span class="dim">not wired</span>'
    : `<span class="${r.state === 'empty' || r.state === 'fault' ? 'badc' : 'ok'}">${words[r.state] || '--'}</span>`;
  const secs = Object.values(((S.water || {}).trays) || {}).reduce((a, t) => a + (t.today_seconds || 0), 0);
  $('ressub').textContent = `pumped ${Math.round(secs)}s today`;
}

function drawAttention() {
  const out = [], h = S.heat || {}, w = S.water || {}, cam = S.camera || {};
  let level = 'ok';
  const bad = s => { out.push(s); level = 'bad'; };
  const warn = s => { out.push(s); if (level !== 'bad') level = 'warn'; };
  if (h.use && h.fault) bad('Heat mat: ' + h.fault);
  else if (h.use && h.plug_ok === false) warn('Heat mat plug not responding');
  const r = w.reservoir || {};
  if (r.state === 'empty') bad('Reservoir empty');
  if (r.state === 'fault') warn('Reservoir sensor fault');
  if (cam.fails > 0 && cam.last_err) warn('Camera: ' + cam.last_err);
  if (cam.usb_speed != null && cam.usb_speed < 480) bad(`Camera on a slow USB link (${cam.usb_speed} Mbit/s)`);
  const thr = w.moisture_threshold_pct || 30;
  Object.keys((S.settings || {}).trays || {}).forEach(t => {
    const m = trayMoisture(t);
    if (m && m.pct < thr) warn(`${trayLabel(t)} dry (${Math.round(m.pct)}%)`);
  });
  const el = $('attn');
  el.className = 'attn' + (level === 'bad' ? ' bad' : level === 'warn' ? ' warn' : '');
  el.textContent = out.length ? out.join(' \u00b7 ') : 'Everything normal';
}

function draw() {
  if (!S) return;
  drawEnv(); drawHeat(); drawSetups(); drawFanRes(); drawAttention();
}

// Soil temperature (line) over the heat mat's power (shaded), last 24 hours,
// drawn at the box's real pixel size.
let trend = {soil: [], duty: [], at: 0};
async function loadTrend() {
  try {
    const [a, b] = await Promise.all(['temp:soil', 'heat:duty'].map(k =>
      fetch(`/api/series?sensor=${encodeURIComponent(k)}&hours=24`, {cache: 'no-store'}).then(r => r.json())));
    trend = {soil: a.points || [], duty: b.points || [], at: Date.now()};
  } catch (e) { /* keep the last one */ }
  drawTrend();
}
function drawTrend() {
  const svg = $('trend'), box = svg.getBoundingClientRect();
  const W = Math.round(box.width), H = Math.round(box.height);
  if (W < 10 || H < 10 || !S) return;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const pts = trend.soil.map(p => [p[0], tDisp(p[1])]);
  if (pts.length < 2) { svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">not enough data yet</text>`; $('trendsub').textContent = ''; return; }
  const now = Date.now() / 1000, x0 = now - 86400;
  const h = S.heat || {};
  const tgt = h.use ? fToDisp(h.target_f) : null;
  let lo = Math.min(...pts.map(p => p[1])), hi = Math.max(...pts.map(p => p[1]));
  if (tgt != null) { lo = Math.min(lo, tgt); hi = Math.max(hi, tgt); }
  const pad = Math.max(0.5, (hi - lo) * 0.15); lo -= pad; hi += pad;
  const sx = t => (t - x0) / 86400 * W, sy = v => H - 2 - (v - lo) / (hi - lo) * (H - 4);
  let s = '';
  if (trend.duty.length) {
    const d = trend.duty.filter(p => p[0] >= x0);
    let path = `M0,${H}`;
    d.forEach((p, i) => {
      const x = sx(p[0]), y = H - (p[1] / 100) * H * 0.45, nx = i + 1 < d.length ? sx(d[i + 1][0]) : Math.min(W, x + W * 900 / 86400);
      path += ` L${x.toFixed(1)},${H} L${x.toFixed(1)},${y.toFixed(1)} L${nx.toFixed(1)},${y.toFixed(1)} L${nx.toFixed(1)},${H}`;
    });
    s += `<path class="duty" d="${path} Z"/>`;
  }
  if (tgt != null) s += `<line class="tgt" x1="0" x2="${W}" y1="${sy(tgt).toFixed(1)}" y2="${sy(tgt).toFixed(1)}"/>`;
  s += `<polyline class="soil" points="${pts.filter(p => p[0] >= x0).map(p => `${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(' ')}"/>`;
  svg.innerHTML = s;
  const vals = pts.filter(p => p[0] >= x0).map(p => p[1]);
  $('trendsub').textContent = `${Math.min(...vals).toFixed(1)}–${Math.max(...vals).toFixed(1)}${tUnit()}`
    + (tgt != null ? ` · target ${tgt.toFixed(0)}${tUnit()} · shaded: heat mat power` : '');
}
addEventListener('resize', drawTrend);

// ---- data ------------------------------------------------------------------

async function refresh() {
  try {
    const r = await fetch('/api/status?lite=1', {cache: 'no-store'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    S = await r.json();
    lastOk = Date.now(); failing = false;
    draw();
  } catch (e) {
    failing = true;
  }
  const p = $('live');
  if (!failing) { p.className = 'pill live'; p.textContent = 'live'; }
  else {
    p.className = 'pill off';
    p.textContent = lastOk ? 'no contact since ' + new Date(lastOk).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})
                           : 'controller not answering';
  }
}

async function post(url, body) {
  $('msg').textContent = '';
  try {
    const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(body)});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false)
      $('msg').textContent = r.status === 401 ? 'not allowed: sign in on the full dashboard'
        : ((j.errors && Object.values(j.errors)[0]) || j.error || ('failed: HTTP ' + r.status));
  } catch (e) { $('msg').textContent = 'request failed'; }
  refresh();
}

document.addEventListener('click', ev => {
  const b = ev.target.closest('.seg button');
  if (!b) return;
  const seg = b.closest('.seg'), v = b.dataset.v, ctl = seg.dataset.ctl;
  seg.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));   // instant feedback
  if (ctl === 'heat') post('/api/heat', {mode: v});
  else if (ctl === 'fan') post('/api/fan', {mode: v});
  else if (ctl === 'light') post('/api/light', {mode: v});
  else if (ctl === 'light2') post('/api/settings', {light2_override: v});
});

function tick() {
  $('clock').textContent = new Date().toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
}
tick(); setInterval(tick, 1000);
refresh().then(loadTrend); setInterval(refresh, 10000); setInterval(loadTrend, 300000);
