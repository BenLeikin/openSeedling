'use strict';
// openSeedling touchscreen summary. Polls /api/status and draws everything on
// one screen; the buttons post to the same endpoints as the dashboard.

let S = null,
  lastOk = 0,
  failing = false;
const $ = id => document.getElementById(id);
const esc = s =>
  String(s == null ? '' : s).replace(
    /[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
  );

const metric = () => !!(S && S.settings && S.settings.units === 'metric');
const tDisp = c => (metric() ? c : (c * 9) / 5 + 32);
const tUnit = () => (metric() ? '\u00b0C' : '\u00b0F');
const fToDisp = f => (metric() ? ((f - 32) * 5) / 9 : f);
const hhmm = iso =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '';

function probePct(cal, v) {
  if (!cal || cal.wet == null || cal.dry == null || cal.dry - cal.wet < 0.05 || v == null)
    return null;
  return Math.max(0, Math.min(100, (100 * (cal.dry - v)) / (cal.dry - cal.wet)));
}
// the probe and float that sit in a tray (Settings, Setups); by number on an
// older controller that does not send the map
function trayProbe(t) {
  return S.tray_map ? (S.tray_map[t] || {}).probe : t;
}
function trayFloat(t) {
  const wt = ((S.water || {}).trays || {})[t];
  if (wt) return wt.float;
  const ch = S.tray_map ? (S.tray_map[t] || {}).float : t;
  return ch == null ? null : ((S.sensors || {})['float:' + ch] || {}).value;
}
function trayMoisture(t) {
  const ch = trayProbe(t);
  if (ch == null) return null;
  const v = (S.probe_filtered || {})[ch];
  const own = probePct(((S.settings || {}).probe_cal || {})[ch], v);
  if (own != null) return { pct: own, approx: false };
  const d = probePct(S.probe_default_cal, v);
  return d == null ? null : { pct: d, approx: true };
}
function canopyOf(t) {
  const c = ((S.sensors || {})['canopy:' + t] || {}).value;
  return c == null ? 'no moisture probe' : `canopy ${Math.round(c)}%`;
}
function trayLabel(t) {
  const tr = ((S.settings || {}).trays || {})[t] || {};
  return tr.label || 'Tray ' + t;
}

function setSeg(ctl, mode) {
  document
    .querySelectorAll(`.seg[data-ctl="${ctl}"] button`)
    .forEach(b => b.classList.toggle('on', b.dataset.v === mode));
}

// ---- drawing ---------------------------------------------------------------

function drawEnv() {
  const f = S.filtered || {};
  const soil = f['temp:soil'],
    air = f['temp:air'];
  $('soil').innerHTML = soil == null ? '--' : `${tDisp(soil).toFixed(1)}<small>${tUnit()}</small>`;
  const h = S.heat || {};
  $('soilsub').textContent = h.use
    ? `heat mat target ${fToDisp(h.target_f).toFixed(0)}${tUnit()}`
    : '';
  $('air').innerHTML = air == null ? '--' : `${tDisp(air).toFixed(1)}<small>${tUnit()}</small>`;
  const rh = f.humidity,
    pt = S.pressure_tendency || {};
  $('airsub').textContent = [
    rh != null ? `${Math.round(rh)}% humidity` : '',
    pt.words ? `pressure ${pt.words}` : ''
  ]
    .filter(Boolean)
    .join(' \u00b7 ');
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
  $('heatbar').style.width = (h.mode === 'off' ? 0 : duty == null ? (h.on ? 100 : 0) : duty) + '%';
  setSeg('heat', h.mode);
}

function lightOf(su) {
  if (su.light === 'second') {
    const l = S.light2 || {};
    return {
      level: l.level || 0,
      mode: l.override || 'auto',
      on: l.start,
      off: l.end,
      ctl: 'light2'
    };
  }
  return {
    level: S.brightness || 0,
    mode: S.light_override || 'auto',
    on: hhmm(su.on || S.on),
    off: hhmm(su.off || S.off),
    ctl: 'light'
  };
}

function drawSetups() {
  const sets =
    S.setups && S.setups.length
      ? S.setups
      : [
          {
            id: 'main',
            name: 'Grow',
            light: 'main',
            band: [0, 0],
            day: S.day_light || {},
            trays: []
          }
        ];
  const allTrays = Object.keys((S.settings || {}).trays || {}).sort();
  const thr = (S.water || {}).moisture_threshold_pct || 30;
  const armed = new Set((S.water || {}).armed || []);
  let h = '';
  sets.forEach((su, i) => {
    const L = lightOf(su);
    const lit = L.level > 0;
    const lstate = lit
      ? `On ${Math.round(L.level)}%`
      : `Off <span class="dim">\u00b7 on at ${esc(L.on)}</span>`;
    const dli = (su.day || {}).dli,
      band = su.band || [0, 0];
    const top = Math.max(band[1] * 1.5, (dli || 0) * 1.1, 1);
    const trays = su.trays && su.trays.length ? su.trays : sets.length === 1 ? allTrays : [];
    const th = trays
      .map(t => {
        const m = trayMoisture(t),
          fv = trayFloat(t);
        const fl = fv == null ? '' : fv >= 1 ? 'float low' : 'float full';
        const dry = m && m.pct < thr;
        return (
          `<div class="tray${dry ? ' dry' : ''}"><div class="tn">${esc(trayLabel(t))}</div>` +
          (m
            ? `<div class="tm">${(m.approx ? '~' : '') + Math.round(m.pct)}<small>%</small></div>`
            : `<div class="tnp">${canopyOf(t)}</div>`) +
          `<div class="ts">${[fl, armed.has(t) ? 'auto-water on' : ''].filter(Boolean).join(' \u00b7 ') || '&nbsp;'}</div></div>`
        );
      })
      .join('');
    h += `<section class="tile setup">
      <div class="tline"><div style="min-width:0">
        <div class="head"><span class="name">${esc(su.name || 'Grow')}</span>
          <span class="light">${esc(su.light_label || '')}</span></div>
        <div class="state">${lstate}</div></div>
        <div class="seg" data-ctl="${L.ctl}" data-i="${i}"><button data-v="auto">Auto</button><button data-v="on">On</button><button data-v="off">Off</button></div>
      </div>
      <div class="dliline"><span>Light today <b>${dli == null ? '--' : dli.toFixed(1)}</b> mol</span>
        <span>${band[1] ? `target ${band[0]}\u2013${band[1]}` : ''}</span></div>
      <div class="bar">${band[1] ? `<span class="band" style="left:${(100 * band[0]) / top}%;width:${(100 * (band[1] - band[0])) / top}%"></span>` : ''}
        <i style="width:${Math.min(100, (100 * (dli || 0)) / top)}%"></i></div>
      ${th ? `<div class="trays">${th}</div>` : ''}
    </section>`;
  });
  const wrap = $('setups');
  if (wrap.dataset.html !== h) {
    wrap.innerHTML = h;
    wrap.dataset.html = h;
  }
  sets.forEach((su, i) => {
    const L = lightOf(su);
    wrap
      .querySelectorAll(`.seg[data-i="${i}"] button`)
      .forEach(b => b.classList.toggle('on', b.dataset.v === L.mode));
  });
}

function drawFanRes() {
  const f = S.fan || {};
  $('fan').innerHTML = f.on
    ? `Running <span class="dim">${Math.round(f.speed || 0)}%</span>`
    : 'Off';
  setSeg('fan', f.mode || 'auto');
  const r = (S.water || {}).reservoir || {};
  const words = { full: 'Full', ok: 'OK', empty: 'Empty', fault: 'Sensor fault' };
  $('res').innerHTML =
    r.wired === false
      ? '<span class="dim">not wired</span>'
      : `<span class="${r.state === 'empty' || r.state === 'fault' ? 'badc' : 'ok'}">${words[r.state] || '--'}</span>`;
  const secs = Object.values((S.water || {}).trays || {}).reduce(
    (a, t) => a + (t.today_seconds || 0),
    0
  );
  $('ressub').textContent = `pumped ${Math.round(secs)}s today`;
}

function drawAttention() {
  const out = [],
    h = S.heat || {},
    w = S.water || {},
    cam = S.camera || {};
  let level = 'ok';
  const bad = s => {
    out.push(s);
    level = 'bad';
  };
  const warn = s => {
    out.push(s);
    if (level !== 'bad') level = 'warn';
  };
  if (h.use && h.fault) bad('Heat mat: ' + h.fault);
  else if (h.use && h.plug_ok === false) warn('Heat mat plug not responding');
  const r = w.reservoir || {};
  if (r.state === 'empty') bad('Reservoir empty');
  if (r.state === 'fault') warn('Reservoir sensor fault');
  if (cam.fails > 0 && cam.last_err) warn('Camera: ' + cam.last_err);
  if (cam.usb_speed != null && cam.usb_speed < 480)
    bad(`Camera on a slow USB link (${cam.usb_speed} Mbit/s)`);
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
  drawEnv();
  drawHeat();
  drawSetups();
  drawFanRes();
  drawAttention();
  fit();
  drawTrend();
}

// Everything must fit the screen with nothing cut off, whatever the fonts,
// the screen's resolution, the number of setups and trays, or the length of
// the attention line. The page is laid out for 600 px wide and sized in rem:
// on a bigger screen the text grows (no more than the width allows) until the
// chart is down to about a quarter of the height, so spare room goes to the
// numbers rather than to an empty chart. When even the base size is too much,
// the page tightens step by step, and as a last resort the chart is dropped.
const FIT_STEPS = ['tight', 'tighter', 'notrend'];
const BASE_PX = 16;
const DESIGN_W = 600;
function overflowing(chartMin = 110) {
  const b = document.body,
    t = $('t-trend');
  if (b.scrollHeight > b.clientHeight + 1 || b.scrollWidth > b.clientWidth + 1) return true;
  return t.offsetParent !== null && t.getBoundingClientRect().height < chartMin;
}
function setSize(px) {
  document.documentElement.style.fontSize = px + 'px';
}
function fit() {
  const b = document.body;
  FIT_STEPS.forEach(c => b.classList.remove(c));
  setSize(BASE_PX);
  const want = Math.max(150, 0.28 * b.clientHeight);
  const most = BASE_PX * Math.max(1, b.clientWidth / DESIGN_W);
  if (!overflowing(want) && most > BASE_PX) {
    let lo = BASE_PX,
      hi = most;
    setSize(hi);
    if (!overflowing(want)) lo = hi;
    while (hi - lo > 0.25) {
      const m = (lo + hi) / 2;
      setSize(m);
      if (overflowing(want)) hi = m;
      else lo = m;
    }
    setSize(lo);
    return;
  }
  for (const c of FIT_STEPS) {
    if (!overflowing()) break;
    b.classList.add(c);
  }
}
addEventListener('resize', () => {
  fit();
  drawTrend();
});

// Soil temperature (line) over the heat mat's power (shaded), last 24 hours,
// drawn at the box's real pixel size.
let trend = { soil: [], duty: [], at: 0 };
async function loadTrend() {
  try {
    const [a, b] = await Promise.all(
      ['temp:soil', 'heat:duty'].map(k =>
        fetch(`/api/series?sensor=${encodeURIComponent(k)}&hours=24`, { cache: 'no-store' }).then(
          r => r.json()
        )
      )
    );
    trend = { soil: a.points || [], duty: b.points || [], at: Date.now() };
  } catch (e) {
    /* keep the last one */
  }
  drawTrend();
}
function niceStep(span, n) {
  const raw = span / n,
    mag = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 5, 10].map(m => m * mag).find(st => st >= raw) || 10 * mag;
}
function hourLabel(t) {
  return new Date(t * 1000).toLocaleTimeString([], { hour: 'numeric' }).replace(':00', '');
}
// Soil temperature with the heat mat's target, and below it the heat mat's
// power, over the last 24 hours, drawn at the box's real pixel size: degree
// labels on the left, clock times along the bottom, the target line labelled.
function drawTrend() {
  const svg = $('trend'),
    box = svg.getBoundingClientRect();
  const W = Math.round(box.width),
    H = Math.round(box.height);
  if (W < 10 || H < 10 || !S) return;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const pts = trend.soil.map(p => [p[0], tDisp(p[1])]);
  if (pts.length < 2) {
    svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">not enough data yet</text>`;
    return;
  }
  const fs = parseFloat(getComputedStyle(svg).fontSize) || 12;
  const now = Date.now() / 1000,
    x0 = now - 86400;
  const h = S.heat || {};
  const tgt = h.use ? fToDisp(h.target_f) : null;
  const shown = pts.filter(p => p[0] >= x0);
  const vals = (shown.length ? shown : pts).map(p => p[1]);
  let lo = Math.min(...vals),
    hi = Math.max(...vals);
  if (tgt != null) {
    lo = Math.min(lo, tgt);
    hi = Math.max(hi, tgt);
  }
  const pad = Math.max(0.5, (hi - lo) * 0.1);
  lo -= pad;
  hi += pad;
  // layout: degree labels in a left gutter; the plot; the heat mat strip; times
  const L = Math.round(fs * 2.9),
    R = W - Math.round(fs * 0.4),
    top = Math.round(fs * 0.7),
    timeH = Math.round(fs * 1.5),
    small = H < fs * 11,
    stripH = !trend.duty.length
      ? 0
      : small
        ? Math.round(fs * 0.9)
        : Math.max(Math.round(fs * 1.8), Math.round(H * 0.12)),
    gap = stripH ? Math.round(fs * 0.5) : 0,
    plotB = H - timeH - stripH - gap,
    stripT = plotB + gap,
    stripB = stripT + stripH;
  const sx = t => L + ((t - x0) / 86400) * (R - L),
    sy = v => plotB - ((v - lo) / (hi - lo)) * (plotB - top);
  let s = '';
  // degree gridlines and labels
  const st = niceStep(hi - lo, Math.max(2, Math.min(6, Math.floor((plotB - top) / (fs * 2.5)))));
  for (let v = Math.ceil(lo / st) * st; v <= hi; v += st) {
    const y = sy(v).toFixed(1);
    s += `<line class="grid" x1="${L}" x2="${R}" y1="${y}" y2="${y}"/>`;
    s += `<text class="tick" x="${L - fs * 0.4}" y="${y}" dy="0.35em" text-anchor="end">${+v.toFixed(1)}\u00b0</text>`;
  }
  // clock times every 6 hours, with "now" at the right edge
  const first = Math.ceil(x0 / 3600) * 3600;
  for (let t = first; t < now; t += 3600) {
    if (new Date(t * 1000).getHours() % 6) continue;
    const x = sx(t);
    if (x - L < fs * 1.5 || R - x < fs * 3.5) continue;
    s += `<line class="grid" x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${top}" y2="${stripB || plotB}"/>`;
    s += `<text class="tick" x="${x.toFixed(1)}" y="${H - fs * 0.35}" text-anchor="middle">${hourLabel(t)}</text>`;
  }
  s += `<text class="tick" x="${R}" y="${H - fs * 0.35}" text-anchor="end">now</text>`;
  // heat mat power: its own strip, full height = 100%
  if (stripH) {
    s += `<rect class="strip" x="${L}" y="${stripT}" width="${R - L}" height="${stripH}"/>`;
    const d = trend.duty.filter(p => p[0] >= x0);
    let path = '';
    d.forEach((p, i) => {
      const x = Math.max(L, sx(p[0])),
        nx = i + 1 < d.length ? sx(d[i + 1][0]) : Math.min(R, x + ((R - L) * 900) / 86400),
        y = stripB - (Math.max(0, Math.min(100, p[1])) / 100) * stripH;
      if (p[1] > 0)
        path += `M${x.toFixed(1)},${stripB} V${y.toFixed(1)} H${nx.toFixed(1)} V${stripB} Z`;
    });
    if (path) s += `<path class="duty" d="${path}"/>`;
    const mid = stripT + stripH / 2,
      gx = L - fs * 0.4;
    s += small
      ? `<text class="tick" x="${gx}" y="${mid}" dy="0.35em" text-anchor="end">mat</text>`
      : `<text class="tick" x="${gx}" y="${mid}" dy="-0.15em" text-anchor="end">heat</text>` +
        `<text class="tick" x="${gx}" y="${mid}" dy="0.85em" text-anchor="end">mat</text>`;
  }
  if (tgt != null) {
    const y = sy(tgt).toFixed(1);
    s += `<line class="tgt" x1="${L}" x2="${R}" y1="${y}" y2="${y}"/>`;
    const above = sy(tgt) - fs * 1.1 >= 0;
    if (plotB - top >= fs * 4)
      s += `<text class="tgtl" x="${L + fs * 0.4}" y="${y}" dy="${above ? '-0.4em' : '1.1em'}">target ${tgt.toFixed(0)}\u00b0</text>`;
  }
  s += `<polyline class="soil" points="${shown
    .map(p => `${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`)
    .join(' ')}"/>`;
  const last = shown[shown.length - 1];
  if (last)
    s += `<circle class="dot" cx="${sx(last[0]).toFixed(1)}" cy="${sy(last[1]).toFixed(1)}" r="${(fs * 0.3).toFixed(1)}"/>`;
  svg.innerHTML = s;
}
addEventListener('resize', drawTrend);

// ---- data ------------------------------------------------------------------

async function refresh() {
  try {
    const r = await fetch('/api/status?lite=1', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    S = await r.json();
    lastOk = Date.now();
    failing = false;
    draw();
  } catch (e) {
    failing = true;
  }
  const p = $('live');
  if (!failing) {
    p.className = 'pill live';
    p.textContent = 'live';
  } else {
    p.className = 'pill off';
    p.textContent = lastOk
      ? 'no contact since ' +
        new Date(lastOk).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
      : 'controller not answering';
  }
}

async function post(url, body) {
  $('msg').textContent = '';
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false)
      $('msg').textContent =
        r.status === 401
          ? 'not allowed: sign in on the full dashboard'
          : (j.errors && Object.values(j.errors)[0]) || j.error || 'failed: HTTP ' + r.status;
  } catch (e) {
    $('msg').textContent = 'request failed';
  }
  refresh();
}

document.addEventListener('click', ev => {
  const b = ev.target.closest('.seg button');
  if (!b) return;
  const seg = b.closest('.seg'),
    v = b.dataset.v,
    ctl = seg.dataset.ctl;
  seg.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); // instant feedback
  if (ctl === 'heat') post('/api/heat', { mode: v });
  else if (ctl === 'fan') post('/api/fan', { mode: v });
  else if (ctl === 'light') post('/api/light', { mode: v });
  else if (ctl === 'light2') post('/api/settings', { light2_override: v });
});

function tick() {
  $('clock').textContent = new Date().toLocaleTimeString([], {
    hour: 'numeric',
    minute: '2-digit'
  });
}
tick();
setInterval(tick, 1000);
refresh().then(loadTrend);
setInterval(refresh, 10000);
setInterval(loadTrend, 300000);
