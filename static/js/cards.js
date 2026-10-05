// openSeedling dashboard: Pi health tiles, the light plan and day progress, the fan, focus and USB camera controls, and start-up.
// An ES module: what it uses from the others is imported at the top, and what
// it offers is exported. Another module cannot assign one of its variables
// directly; it calls the set_<name>() exported at the bottom. Code that runs
// at page load is in start(), which main.js calls in a fixed order.
// ---- Pi health tiles ----
import {
  S,
  dragTimer,
  dragging,
  mins,
  pushBrightness,
  setLight,
  set_dragPending,
  set_dragTimer,
  set_dragging
} from './light.js';
import { initAuth } from './photos.js';
import { isMetric, lightMetrics, tDisp, tFromF, tUnit } from './charts.js';
import { esc, initTrayConfig, initTrays } from './trays.js';
import {
  initBackup,
  initPlantings,
  initPlug,
  initSensors,
  initStorm,
  initTheme
} from './devices.js';
import { dliBand, initGridSvg, initReport, refresh } from './grid.js';
import {
  drawLightCurve,
  lastCurve,
  set_lastCurve,
  set_sweepRunning,
  startSweep,
  sweepRunning
} from './live.js';
import { startWalker } from './buddy.js';

export function hostTile(label, value, sub, cls) {
  // numeric tiles are short and keep the large size; text values (hostname, IP)
  // can be long, so step the size down by length rather than breaking mid-word
  const plain = String(value).replace(/<[^>]*>/g, '');
  if (!/^[\d.,:%\s-]+$/.test(plain)) {
    cls = (cls ? cls + ' ' : '') + (plain.length > 10 ? 'long' : 'text');
  }
  return (
    `<div class="htile"><div class="hlabel">${label}</div>` +
    `<div class="hvalue${cls ? ' ' + cls : ''}" title="${esc(plain)}">${value}</div>` +
    `<div class="hsub">${sub || '&nbsp;'}</div></div>`
  );
}
export function upStr(sec) {
  if (sec == null) return null;
  const d = Math.floor(sec / 86400),
    h = Math.floor((sec % 86400) / 3600),
    m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
  return `${m}m`;
}
export async function loadHost() {
  const grid = document.getElementById('hgrid');
  if (!grid) return;
  try {
    const r = await fetch('/api/host');
    const h = await r.json();
    let out = '';
    if (h.cpu_temp_c != null) {
      const f = (h.cpu_temp_c * 9) / 5 + 32;
      // Pi soft-throttles at 80C, hard at 85C
      const cls = h.cpu_temp_c >= 80 ? 'bad' : h.cpu_temp_c >= 70 ? 'warn' : 'good';
      out += hostTile(
        'CPU temp',
        isMetric() ? `${h.cpu_temp_c.toFixed(1)}\u00b0C` : `${f.toFixed(1)}\u00b0F`,
        isMetric() ? `${f.toFixed(0)}\u00b0F` : `${h.cpu_temp_c.toFixed(0)}\u00b0C`,
        cls
      );
    }
    if (h.load) {
      const per = h.load['1m'] / (h.load.cores || 1);
      out += hostTile(
        'Load',
        h.load['1m'].toFixed(2),
        `${h.load.cores} core${h.load.cores > 1 ? 's' : ''}`,
        per > 1 ? 'warn' : ''
      );
    }
    if (h.memory)
      out += hostTile(
        'Memory',
        h.memory.percent + '%',
        `${h.memory.used_mb}/${h.memory.total_mb} MB`,
        h.memory.percent >= 90 ? 'bad' : ''
      );
    if (h.disk)
      out += hostTile(
        'Disk',
        h.disk.percent + '%',
        `${h.disk.used_gb}/${h.disk.total_gb} GB`,
        h.disk.percent >= 90 ? 'bad' : h.disk.percent >= 75 ? 'warn' : ''
      );
    if (h.uptime_seconds != null) out += hostTile('Uptime', upStr(h.uptime_seconds), '');
    if (h.throttled) {
      const t = h.throttled;
      const val = t.now.length ? t.now[0] : t.since_boot.length ? 'recovered' : 'healthy';
      const sub = t.now.length
        ? 'happening now'
        : t.since_boot.length
          ? `since boot: ${t.since_boot.join(', ')}`
          : 'no issues';
      out += hostTile(
        'Power',
        val,
        sub,
        t.now.length ? 'bad' : t.since_boot.length ? 'warn' : 'good'
      );
    }
    if (h.core_voltage != null)
      out += hostTile(
        'Core V',
        h.core_voltage.toFixed(4) + 'V',
        h.cpu_mhz != null ? `${h.cpu_mhz} MHz` : ''
      );
    else if (h.cpu_mhz != null) out += hostTile('CPU clock', h.cpu_mhz + ' MHz', '');
    if (h.host) out += hostTile('Host', h.host, h.ip || '');
    if (h.wifi)
      out += hostTile(
        'WiFi',
        h.wifi.percent + '%',
        `${h.wifi.iface} ${h.wifi.dbm} dBm`,
        h.wifi.percent < 35 ? 'warn' : ''
      );
    grid.innerHTML = out || '<p class="rmuted">No device stats available.</p>';
  } catch (e) {
    grid.innerHTML = '<p class="rmuted">Device stats unavailable.</p>';
  }
}
export function renderLightPlan(j) {
  const box = document.getElementById('lplan');
  if (!box) return;
  const p = j.light_plan;
  if (!p) {
    box.style.display = 'none';
    return;
  }
  box.style.display = '';
  box.className = 'lplan ' + p.status;
  const dot = document.getElementById('lpdot');
  if (dot) dot.className = 'lpdot ' + p.status;
  const title = document.getElementById('lptitle');
  if (title) {
    const when = p.day ? ` (${p.day})` : '';
    title.textContent =
      p.status === 'no_sensor'
        ? 'No light sensor'
        : p.status === 'pending'
          ? `Measuring \u00b7 ${(p.full_day || 0).toFixed(1)} mol so far`
          : p.status === 'ok'
            ? `On track \u00b7 ${p.full_day.toFixed(1)} mol${when}`
            : p.status === 'low'
              ? `Short on light \u00b7 ${p.full_day.toFixed(1)} mol${when}`
              : `More light than needed \u00b7 ${p.full_day.toFixed(1)} mol${when}`;
  }
  const ul = document.getElementById('lpadvice');
  if (ul) ul.innerHTML = (p.advice || []).map(a => `<li>${a}</li>`).join('');
}
// Daily light through today (solid) and yesterday (dashed) against the target:
// the band the day should end in, and the wedge where the total should be by
// each hour of the photoperiod.
export function drawDliChart(day) {
  const svg = document.getElementById('dlichart');
  if (!svg) return;
  const cv = day && day.curve;
  if (!cv || (!(cv.today || []).length && !(cv.yesterday || []).length)) {
    svg.style.display = 'none';
    return;
  }
  svg.style.display = '';
  const W = 320,
    H = 130,
    L = 28,
    R = 8,
    T = 8,
    B = 18;
  const { lo, hi, max } = dliBand;
  const top = Math.max(max, ...[...(cv.today || []), ...(cv.yesterday || [])].map(p => p[1] * 1.1));
  const x = m => L + ((W - L - R) * m) / 1440,
    y = v => H - B - ((H - T - B) * Math.min(v, top)) / top;
  const path = pts =>
    pts.map((p, i) => (i ? 'L' : 'M') + x(p[0]).toFixed(1) + ' ' + y(p[1]).toFixed(1)).join('');
  const on = S ? mins(S.on) : 420,
    off = S ? mins(S.off) : 1140;
  let h = '';
  // target band for the whole day, across the chart
  h += `<rect x="${L}" y="${y(hi).toFixed(1)}" width="${W - L - R}" height="${(y(lo) - y(hi)).toFixed(1)}" class="dcband"/>`;
  // pace wedge: 0 at lights on, the band at lights off
  if (off > on)
    h +=
      `<path d="M${x(on).toFixed(1)} ${y(0)} L${x(off).toFixed(1)} ${y(hi).toFixed(1)} ` +
      `L${x(1440).toFixed(1)} ${y(hi).toFixed(1)} L${x(1440).toFixed(1)} ${y(lo).toFixed(1)} ` +
      `L${x(off).toFixed(1)} ${y(lo).toFixed(1)} Z" class="dcpace"/>`;
  for (const hr of [0, 6, 12, 18, 24])
    h +=
      `<line x1="${x(hr * 60)}" y1="${T}" x2="${x(hr * 60)}" y2="${H - B}" class="dcgrid"/>` +
      `<text x="${x(hr * 60)}" y="${H - 5}" class="dcax" text-anchor="middle">${String(hr).padStart(2, '0')}</text>`;
  for (const v of [lo, hi])
    h += `<text x="${L - 4}" y="${(y(v) + 3).toFixed(1)}" class="dcax" text-anchor="end">${v}</text>`;
  if ((cv.yesterday || []).length) h += `<path d="${path(cv.yesterday)}" class="dcyest"/>`;
  if ((cv.today || []).length) {
    const last = cv.today[cv.today.length - 1];
    h +=
      `<path d="${path(cv.today)}" class="dctoday"/>` +
      `<circle cx="${x(last[0]).toFixed(1)}" cy="${y(last[1]).toFixed(1)}" r="3" class="dcnow"/>`;
  }
  svg.innerHTML = h;
}
export function renderDayProgress(j) {
  const wrap = document.getElementById('dayprog');
  if (!wrap || !S.on || !S.off) return;
  const on = +S.on,
    off = +S.off,
    now = Date.now();
  const span = off - on;
  if (!(span > 0)) {
    wrap.style.display = 'none';
    return;
  }
  wrap.style.display = '';
  const frac = Math.max(0, Math.min(1, (now - on) / span));
  const fill = document.getElementById('dpfill');
  const marker = document.getElementById('dpnow');
  if (fill) fill.style.width = (frac * 100).toFixed(1) + '%';
  if (marker) marker.style.left = (frac * 100).toFixed(1) + '%';
  const left = document.getElementById('dpleft');
  if (left) {
    if (now < on) left.textContent = 'lights on in ' + durStr(on - now);
    else if (now > off) left.textContent = 'lights off \u00b7 on again tomorrow';
    else left.textContent = durStr(off - now) + ' of light left';
  }
  const day = j.day_light || null;
  drawDliChart(day);
  // how long the light has actually delivered today
  const lit = document.getElementById('dplit');
  if (lit) lit.textContent = day && day.lit_minutes ? durStr(day.lit_minutes * 60000) + ' lit' : '';

  // DLI against the setup's target band (Settings, Setups)
  const { lo: BLO, hi: BHI, max: BMAX } = dliBand;
  const d = day && day.dli != null ? day.dli : lightMetrics && lightMetrics.dli;
  const dfill = document.getElementById('dlifill');
  const dval = document.getElementById('dlival');
  const pace = document.getElementById('dlipace');
  if (dfill)
    dfill.style.width = Math.max(0, Math.min(100, ((d || 0) / BMAX) * 100)).toFixed(1) + '%';

  // Follow the schedule. During the photoperiod the fair comparison is not the
  // whole day's target band but where the total should be by now: that band
  // scaled by how far through the lit day we are (a noon total exactly on pace
  // is not "below target"). After lights out the full band applies again.
  const lo = BLO * frac,
    hi = BHI * frac;
  const during = frac > 0 && frac < 1;
  const pm = document.getElementById('dlipacemark');
  if (pm) {
    pm.style.left = Math.max(0, Math.min(100, (lo / BMAX) * 100)).toFixed(1) + '%';
    pm.style.width = Math.max(0.6, Math.min(100, ((hi - lo) / BMAX) * 100)).toFixed(1) + '%';
    pm.style.display = during ? '' : 'none';
    pm.title = `where today\u2019s total should be by now: ${lo.toFixed(1)}\u2013${hi.toFixed(1)} mol`;
  }
  // one verdict drives both the words and the fill color, so they can never
  // disagree: pace while the lights are on, the full-day target after
  let state = '';
  if (d != null) {
    state = during
      ? d < lo
        ? 'low'
        : d <= hi
          ? 'ok'
          : 'high'
      : d < BLO
        ? 'low'
        : d <= BHI
          ? 'ok'
          : 'high';
  }
  if (dfill) dfill.className = 'dlifill' + (state ? ' ' + state : '');
  if (dval) {
    if (d == null) {
      dval.textContent = 'building today\u2019s total';
    } else if (during) {
      const where = { low: 'behind pace', ok: 'on pace', high: 'ahead of pace' }[state];
      dval.innerHTML = `<b>${d.toFixed(1)}</b> mol \u00b7 ${where}`;
      dval.className = 'dli' + state;
    } else {
      const band = { low: 'below target', ok: 'in target', high: 'above target' }[state];
      dval.innerHTML = `<b>${d.toFixed(1)}</b> mol \u00b7 ${band}`;
      dval.className = 'dli' + state;
    }
  }
  if (pace) {
    const fc = day && day.forecast_remaining;
    if (d == null || frac <= 0) {
      pace.textContent = '';
    } else if (frac >= 1) {
      pace.textContent = 'day complete';
      pace.className = 'dlipace';
    } else if (fc != null) {
      // schedule-aware: today's total = banked + what the remaining ramp and
      // full-brightness hours will deliver, from the measured light curve
      const proj = d + fc;
      const verdict = proj < BLO ? 'behind' : proj <= BHI ? 'on track' : 'ahead';
      pace.innerHTML = `forecast <b>${proj.toFixed(1)}</b> \u00b7 ${verdict}`;
      pace.className = 'dlipace ' + (proj < BLO ? 'low' : proj <= BHI ? 'ok' : 'high');
      pace.title =
        `${d.toFixed(1)} banked + ${fc.toFixed(1)} from the rest of ` +
        'today\u2019s schedule (measured light curve)';
    } else if (frac < 0.08) {
      pace.textContent = 'too early to project';
      pace.className = 'dlipace';
    } else {
      const proj = d / frac; // fallback: no sweep on file yet
      const verdict = proj < BLO ? 'behind' : proj <= BHI ? 'on track' : 'ahead';
      pace.innerHTML = `projected <b>${proj.toFixed(1)}</b> \u00b7 ${verdict}`;
      pace.className = 'dlipace ' + (proj < BLO ? 'low' : proj <= BHI ? 'ok' : 'high');
      pace.title =
        'rough estimate from today\u2019s average so far; ' +
        'measure the light response curve for a schedule-aware forecast';
    }
  }
  // peak intensity reached today
  const stats = document.getElementById('daystats');
  if (stats) {
    // each pair wrapped so the label and value stay on one line together;
    // a bare dt/dd sequence in a flex row separates them
    let h2 = '';
    if (day && day.peak_ppfd != null)
      h2 += `<div><dt>Peak today</dt><dd>${Math.round(day.peak_ppfd)} <small>\u00b5mol</small></dd></div>`;
    if (day && day.peak_lux != null)
      h2 += `<div><dt>Peak light</dt><dd>${Math.round(day.peak_lux).toLocaleString()} <small>lx</small></dd></div>`;
    stats.innerHTML = h2;
    stats.style.display = h2 ? '' : 'none';
  }
}
export function durStr(ms) {
  const m = Math.max(0, Math.round(ms / 60000));
  if (m < 60) return m + ' min';
  return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm';
}
export let fanDragging = false,
  fanDragTimer = null,
  fanDragPending = null;
export function renderFan(j) {
  const row = document.getElementById('fanrow');
  const sl = document.getElementById('fanslider');
  if (!row) return;
  const f = j.fan;
  if (!f || !f.hw) {
    row.style.display = 'none';
    if (sl) sl.style.display = 'none';
    return;
  }
  row.style.display = '';
  document
    .querySelectorAll('.fanbtn')
    .forEach(b => b.classList.toggle('on', b.dataset.mode === f.mode));
  const info = document.getElementById('faninfo'),
    dot = document.getElementById('fandot');
  if (dot) dot.className = 'devdot' + (f.on ? ' on' : '');
  if (info) {
    let t,
      why = f.reason && f.reason !== 'idle' ? f.reason : '';
    if (f.on) t = `<b>Running</b> at ${f.speed}%` + (why ? ` \u00b7 ${esc(why)}` : '');
    else if (f.mode === 'auto') t = `<b>Off</b> \u00b7 runs in the photoperiod or on high humidity`;
    else t = '<b>Off</b>';
    info.innerHTML = t;
  }
  // the slider edits manual speed in "on", auto speed in "auto"; hidden in "off"
  if (sl) {
    sl.style.display = f.mode === 'off' ? 'none' : '';
    sl.classList.toggle('dim', f.mode === 'off');
    const rng = document.getElementById('fanrange'),
      val = document.getElementById('fanval');
    const target = f.mode === 'on' ? f.manual_speed : f.auto_speed;
    if (rng && !fanDragging && document.activeElement !== rng) rng.value = target;
    if (val && !fanDragging) val.textContent = (rng ? rng.value : target) + '%';
    const lbl = sl.querySelector('.fanslabel');
    if (lbl) lbl.textContent = f.mode === 'on' ? 'speed' : 'auto speed';
  }
}
export function renderHeat(j) {
  const row = document.getElementById('heatrow');
  if (!row) return;
  const h = j.heat;
  if (!h || !h.use) {
    row.style.display = 'none';
    return;
  }
  row.style.display = '';
  document
    .querySelectorAll('.heatbtn')
    .forEach(b => b.classList.toggle('on', b.dataset.mode === h.mode));
  const info = document.getElementById('heatinfo'),
    dot = document.getElementById('heatdot');
  if (!info) return;
  const plugBad = h.plug_ok === false;
  const warn = !!h.fault || plugBad;
  if (dot) dot.className = 'devdot' + (warn ? ' warn' : h.on ? ' on' : '');
  const state = h.on == null ? 'Unknown' : h.on ? 'On' : 'Off';
  const soil =
    h.temp_c != null ? `soil ${tDisp(h.temp_c).toFixed(1)}${tUnit()}` : 'no soil reading';
  const tgt = `${Math.round(tFromF(+h.target_f) * 10) / 10}${tUnit()}`;
  let t = `<b>${state}</b> \u00b7 ${soil}`;
  if (h.fault) t += ` \u00b7 held off: ${esc(h.fault)}`;
  else if (h.mode === 'auto')
    t +=
      ` \u00b7 target ${tgt}` +
      (h.duty != null ? ` \u00b7 ${Math.round(h.duty * 100)}% power` : '');
  else if (h.mode === 'on') t += ' \u00b7 held on';
  else t += ' \u00b7 switched off';
  if (plugBad) t += ' \u00b7 plug not responding';
  info.innerHTML = t;
  info.title = plugBad && h.plug_error ? h.plug_error : '';
}
export async function setHeat(mode) {
  const info = document.getElementById('heatinfo');
  if (info) info.textContent = '\u2026';
  try {
    const r = await fetch('/api/heat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode })
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401 && info) {
      info.textContent = 'log in first';
      return;
    }
    if (!j.ok && info) info.textContent = j.error || 'failed';
  } catch (e) {
    if (info) info.textContent = 'request failed';
  }
  setTimeout(refresh, 1500); // the plug takes a moment to answer
}
export function pushFanSpeed(v) {
  fanDragPending = v;
  if (fanDragTimer) return;
  fanDragTimer = setTimeout(() => {
    fanDragTimer = null;
    const val = fanDragPending;
    fanDragPending = null;
    if (val != null) sendFanSpeed(val);
  }, 150);
}
export async function sendFanSpeed(v) {
  const mode = (document.querySelector('.fanbtn.on') || { dataset: {} }).dataset.mode || 'auto';
  const body = mode === 'on' ? { speed: v } : { auto_speed: v };
  try {
    await fetch('/api/fan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
  } catch (e) {}
}
export async function setFan(mode) {
  const info = document.getElementById('faninfo');
  if (info) info.textContent = '\u2026';
  try {
    const r = await fetch('/api/fan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode })
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401 && info) {
      info.textContent = 'log in first';
      return;
    }
    if (!j.ok && info) info.textContent = j.error || 'failed';
  } catch (e) {
    if (info) info.textContent = 'request failed';
  }
  refresh();
}
export let focusRunning = false;
export async function startFocusSweep() {
  const info = document.getElementById('focusinfo');
  if (focusRunning) {
    await fetch('/api/focus_sweep', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cancel: true })
    });
    return;
  }
  if (info) info.textContent = 'starting\u2026';
  try {
    const r = await fetch('/api/focus_sweep', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      if (info) info.textContent = 'log in first';
      return;
    }
    if (!j.ok) {
      if (info) info.textContent = j.error || 'failed';
      return;
    }
    if (info) info.textContent = `sweeping\u2026 (~${j.estimate_seconds}s; timelapse pauses)`;
  } catch (e) {
    if (info) info.textContent = 'request failed';
  }
}
export function renderFocus(j) {
  const btn = document.getElementById('focusbtn');
  const info = document.getElementById('focusinfo');
  const f = j.focus || {};
  const was = focusRunning;
  focusRunning = !!f.running;
  if (btn) btn.textContent = focusRunning ? 'Cancel' : 'Focus sweep';
  if (focusRunning && info)
    info.textContent = `sweeping\u2026 ${f.step}${f.total ? '/' + f.total : ''} points`;
  if (was && !focusRunning && info) {
    if (f.error) info.textContent = f.error;
    else if (f.best)
      info.textContent = `pinned focus ${f.best.focus} (score ${f.best.score}, ${f.best.tested} points)`;
    else info.textContent = 'cancelled';
  }
}
export function renderSweep(j) {
  const btn = document.getElementById('sweepbtn');
  const info = document.getElementById('sweepinfo');
  const sw = j.sweep || {};
  const was = sweepRunning;
  set_sweepRunning(!!sw.running);
  if (btn) btn.textContent = sweepRunning ? 'Cancel' : 'Calibrate';
  if (sweepRunning && info) info.textContent = `measuring\u2026 ${sw.pct}%`;
  if (was && !sweepRunning && info && sw.error) info.textContent = sw.error;
  // with a calibration in use, chart what the dashboard DELIVERS (a straight
  // line if it worked) rather than the raw fixture, which never changes shape
  set_lastCurve(
    j.light_curve_effective
      ? {
          points: j.light_curve_effective,
          ts: (j.light_curve || {}).ts,
          calibrated: true,
          rawPoints: (j.light_curve || {}).points || []
        }
      : j.light_curve
        ? { ...j.light_curve, stale: !!j.light_linear_stale }
        : null
  );
  if (!dragging)
    // a drag owns the marker
    drawLightCurve(
      lastCurve,
      sweepRunning,
      sweepRunning ? null : j.brightness != null ? j.brightness : null
    );
}
export function showScheduleMode(mode) {
  document.querySelectorAll('.modeblock').forEach(b => {
    b.style.display = b.dataset.mode === mode ? '' : 'none';
  });
}
// show a manual block only when its auto checkbox is clear
export const USB_AUTO = [
  ['usb_auto_focus', 'manual-focus'],
  ['usb_auto_exposure_on', 'manual-exposure'],
  ['usb_auto_white_balance', 'manual-wb']
];
export function syncUsbAuto() {
  const f = document.getElementById('cfgform');
  if (!f) return;
  const usb = (f.elements['camera_backend'] || {}).value === 'usb';
  for (const [name, cls] of USB_AUTO) {
    const on = f.elements[name] && f.elements[name].checked;
    document
      .querySelectorAll('.' + cls)
      .forEach(el => (el.style.display = usb && !on ? '' : 'none'));
  }
}
export function initCameraBackend() {
  for (const [name] of USB_AUTO) {
    const cb = document.querySelector(`[name=${name}]`);
    if (cb && !cb.dataset.bound) {
      cb.dataset.bound = '1';
      cb.addEventListener('change', syncUsbAuto);
    }
  }
  const cb = document.querySelector('[name=camera_backend]');
  if (!cb || cb.dataset.bound) return;
  cb.dataset.bound = '1';
  cb.addEventListener('change', () => {
    document
      .querySelectorAll('.usbonly')
      .forEach(el => (el.style.display = cb.value === 'usb' ? '' : 'none'));
    syncUsbAuto();
  });
}
// A field's label as the page shows it ("Cut-off (\u00b0F)"), for messages that
// would otherwise show the setting's internal name
export function fieldLabel(f, k) {
  if (k === 'setups') return 'Setups:';
  const el = f.elements[k];
  const lab = el && el.closest && el.closest('label');
  if (!lab) return k;
  let t = '';
  for (const n of lab.childNodes)
    if (n.nodeType === 3 || (n.nodeType === 1 && !/^(INPUT|SELECT|TEXTAREA)$/.test(n.tagName)))
      t += n.textContent;
  t = t.replace(/\s+/g, ' ').trim();
  return t ? t + ':' : k;
}

// Photo brightness only matters when photos set the light
export function syncCaptureLight() {
  const cb = document.querySelector('#cfgform [name=capture_set_light]');
  document
    .querySelectorAll('.capbright')
    .forEach(el => (el.style.display = cb && cb.checked ? '' : 'none'));
}

export function initSchedule() {
  const sm = document.querySelector('[name=schedule_mode]');
  if (!sm) return;
  sm.addEventListener('change', () => showScheduleMode(sm.value));
}
export function initLight() {
  {
    const row = document.getElementById('lightctl') || document.body;
    if (row.dataset.lightBound) return; // double-binding would fire
    row.dataset.lightBound = '1';
  } // every click twice
  {
    const b = document.getElementById('sweepbtn');
    if (b) b.addEventListener('click', startSweep);
  }
  {
    const qt = document.getElementById('qtoggle'),
      qb = document.getElementById('qbody');
    if (qt && qb)
      qt.addEventListener('click', () => {
        const open = qb.hasAttribute('hidden');
        if (open) qb.removeAttribute('hidden');
        else qb.setAttribute('hidden', '');
        qt.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
  }
  {
    const fb = document.getElementById('focusbtn');
    if (fb) fb.addEventListener('click', startFocusSweep);
  }
  document
    .querySelectorAll('.fanbtn')
    .forEach(b => b.addEventListener('click', () => setFan(b.dataset.mode)));
  document
    .querySelectorAll('.heatbtn')
    .forEach(b => b.addEventListener('click', () => setHeat(b.dataset.mode)));
  {
    const fr = document.getElementById('fanrange'),
      fv = document.getElementById('fanval');
    if (fr) {
      const start = () => {
        fanDragging = true;
      };
      const end = () => {
        if (!fanDragging) return;
        fanDragging = false;
        clearTimeout(fanDragTimer);
        fanDragTimer = null;
        sendFanSpeed(+fr.value).then(() => refresh());
      };
      fr.addEventListener('pointerdown', start);
      fr.addEventListener('keydown', start);
      fr.addEventListener('input', () => {
        if (fv) fv.textContent = fr.value + '%';
        if (fanDragging) pushFanSpeed(+fr.value);
      });
      fr.addEventListener('pointerup', end);
      fr.addEventListener('pointercancel', end);
      fr.addEventListener('change', end);
      fr.addEventListener('blur', end);
    }
  }
  document
    .querySelectorAll('.lcbtn:not(.fanbtn):not(.heatbtn)')
    .forEach(b => b.addEventListener('click', () => setLight(b.dataset.mode)));
  const rng = document.getElementById('lcrange');
  const val = document.getElementById('lcval');
  if (!rng) return;
  const startDrag = () => {
    set_dragging(true);
  };
  const endDrag = () => {
    if (!dragging) return;
    set_dragging(false);
    if (dragTimer) {
      clearTimeout(dragTimer);
      set_dragTimer(null);
    }
    set_dragPending(null);
    setLight(null, +rng.value); // final value, with UI sync
  };
  rng.addEventListener('pointerdown', startDrag);
  rng.addEventListener('keydown', startDrag);
  rng.addEventListener('input', () => {
    const v = +rng.value;
    if (val) val.textContent = v + '%'; // slider label tracks instantly
    if (dragging) {
      pushBrightness(v); // light tracks, throttled
      // the readouts follow the drag rather than the 15s status poll
      const pctEl = document.getElementById('pct');
      if (pctEl) pctEl.textContent = v + '%';
      const ph = document.querySelector('.aphase');
      if (ph) ph.style.setProperty('--lum', (v / 100).toFixed(2));
      drawLightCurve(lastCurve, false, v);
    }
  });
  rng.addEventListener('pointerup', endDrag);
  rng.addEventListener('pointercancel', endDrag);
  rng.addEventListener('keyup', endDrag);
  rng.addEventListener('blur', endDrag);
  rng.addEventListener('change', () => {
    // click-to-jump, no drag involved
    if (!dragging) setLight(null, +rng.value);
  });
}

// Runs once at page load; main.js calls each module's start() in a fixed order.
export function start() {
  {
    const f = document.getElementById('cfgform');
    if (f)
      f.addEventListener('input', e => {
        if (e.target.removeAttribute) e.target.removeAttribute('aria-invalid');
      });
  }
  {
    const cb = document.querySelector('#cfgform [name=capture_set_light]');
    if (cb) cb.addEventListener('change', syncCaptureLight);
  }
  [
    initAuth,
    initSensors,
    initGridSvg,
    initReport,
    initLight,
    initTrays,
    initSchedule,
    initTrayConfig,
    initCameraBackend,
    initPlantings,
    initBackup,
    initPlug,
    initTheme,
    initStorm,
    startWalker
  ].forEach(fn => {
    try {
      fn();
    } catch (e) {
      console.error(fn.name + ' init failed:', e);
    }
  });
  refresh();
}
