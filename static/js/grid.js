// openSeedling dashboard: The cell grid overlay, the Settings form's submit handler, and the AI garden report.
// An ES module: what it uses from the others is imported at the top, and what
// it offers is exported. Another module cannot assign one of its variables
// directly; it calls the set_<name>() exported at the bottom. Code that runs
// at page load is in start(), which main.js calls in a fixed order.
// ---------------- cell grid overlay ----------------
import { S, capturePhoto, render, renderPhoto, set_S } from './light.js';
import { applySetup, set_setupDirty, setupDirty, setupDraft } from './setups.js';
import {
  FORM,
  aligning,
  applyAuth,
  canEdit,
  cropping,
  fillForm,
  formDirty,
  loadFrames,
  pendingSave,
  readField,
  renderVideoState,
  startAlign,
  stopAlign
} from './photos.js';
import {
  lastChartLoad,
  lastHostLoad,
  loadChart,
  renderQuality,
  renderSensors,
  renderWater,
  set_lastHostLoad,
  set_lightBackend
} from './charts.js';
import { esc, renderTrayConfig, renderTrays } from './trays.js';
import { renderCalibration, renderLight2, renderStorm } from './devices.js';
import {
  fieldLabel,
  loadHost,
  renderDayProgress,
  renderFan,
  renderFocus,
  renderHeat,
  renderLightPlan,
  renderSweep
} from './cards.js';

export let grid = null,
  gdrag = -1,
  gridDirty = false;
export function colL(c) {
  return String.fromCharCode(65 + c);
}
export function cellKey(r, c) {
  return colL(c) + (r + 1);
}
export function bil(C, u, v) {
  const t = [(1 - u) * C[0][0] + u * C[1][0], (1 - u) * C[0][1] + u * C[1][1]];
  const b = [(1 - u) * C[3][0] + u * C[2][0], (1 - u) * C[3][1] + u * C[2][1]];
  return [(1 - v) * t[0] + v * b[0], (1 - v) * t[1] + v * b[1]];
}
export function gridEditable() {
  return canEdit && grid && !grid.locked;
}
export function drawGrid() {
  const svg = document.getElementById('gridsvg');
  if (!grid || !svg) return;
  if (cropping) {
    svg.style.display = 'none';
    return;
  } // the crop box owns the photo
  svg.style.display = grid.show ? '' : 'none';
  if (!grid.show) {
    svg.innerHTML = '';
    return;
  }
  // On the flattened view the image IS the tray rectangle, so the cells are
  // even splits of the frame; the saved corners describe the raw frame and
  // would land in the wrong places here.
  const photo = document.getElementById('photo');
  const flat = !!(photo && photo.dataset.flat);
  const cr =
    !flat && photo && photo.dataset.crop ? photo.dataset.crop.split(',').map(Number) : null;
  // corners are saved against the full frame; on a cropped view, re-express
  // them relative to the crop so the cells stay on the trays
  const C = flat
    ? [
        [0, 0],
        [1, 0],
        [1, 1],
        [0, 1]
      ]
    : cr
      ? grid.corners.map(p => [(p[0] - cr[0]) / cr[2], (p[1] - cr[1]) / cr[3]])
      : grid.corners;
  const R = grid.rows,
    K = grid.cols,
    S = 1000;
  let h = '';
  for (let r = 0; r < R; r++)
    for (let c = 0; c < K; c++) {
      const p = [
        bil(C, c / K, r / R),
        bil(C, (c + 1) / K, r / R),
        bil(C, (c + 1) / K, (r + 1) / R),
        bil(C, c / K, (r + 1) / R)
      ];
      const pts = p.map(q => (q[0] * S).toFixed(1) + ',' + (q[1] * S).toFixed(1)).join(' ');
      const k = cellKey(r, c);
      const fill = 'rgba(127,176,105,0.12)';
      h += `<polygon class="gc" data-k="${k}" points="${pts}" fill="${fill}" stroke="#eafff0" stroke-width="2"/>`;
      const ctr = bil(C, (c + 0.5) / K, (r + 0.5) / R);
      const cx = (ctr[0] * S).toFixed(1);
      let yy = ctr[1] * S - 3;
      h += `<text x="${cx}" y="${yy.toFixed(1)}" class="glbl" text-anchor="middle">${k}</text>`;
      const nm = grid.names[k];
      if (nm) {
        yy += 22;
        h += `<text x="${cx}" y="${yy.toFixed(1)}" class="gnm" text-anchor="middle">${esc(nm)}</text>`;
      }
    }
  if (gridEditable())
    for (let i = 0; i < 4; i++)
      h += `<circle class="gh" data-i="${i}" cx="${(C[i][0] * S).toFixed(1)}" cy="${(C[i][1] * S).toFixed(1)}" r="16"/>`;
  svg.innerHTML = h;
}
export function ptFrac(svg, e) {
  const r = svg.getBoundingClientRect();
  return [
    Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)),
    Math.max(0, Math.min(1, (e.clientY - r.top) / r.height))
  ];
}
export async function saveGrid() {
  gridDirty = true; // pending local edit; block poll-sync until saved
  const info = document.getElementById('gridinfo');
  try {
    const r = await fetch('/api/grid', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(grid)
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      if (info)
        info.textContent =
          r.status === 401
            ? 'not saved \u2014 log in to edit the grid'
            : 'grid not saved: ' + (j.error || 'HTTP ' + r.status);
      return; // stay dirty so a poll won't revert unsaved edits
    }
    gridDirty = false; // saved; tabs may sync again
    if (info && /not saved|HTTP|log in/.test(info.textContent)) info.textContent = '';
  } catch (e) {
    if (info) info.textContent = 'grid not saved (request failed)';
  }
}
export async function detectGrid() {
  if (!gridEditable()) return;
  const info = document.getElementById('gridinfo');
  info.textContent = 'Detecting...';
  try {
    const r = await fetch('/api/detect_grid', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    });
    const j = await r.json();
    if (j.ok && j.corners) {
      grid.corners = j.corners;
      if (!grid.show) {
        grid.show = true;
        document.getElementById('gridshow').checked = true;
      }
      drawGrid();
      saveGrid();
      info.textContent = 'Detected \u2014 drag corners to fine-tune.';
    } else info.textContent = j.error || 'Detection failed; place corners by hand.';
  } catch (e) {
    info.textContent = 'Detection unavailable; place corners by hand.';
  }
}
export function syncGridControls() {
  if (!grid) return;
  document.getElementById('gridshow').checked = !!grid.show;
  document.getElementById('gridrows').value = grid.rows;
  document.getElementById('gridcols').value = grid.cols;
  applyGridLock();
}
export function initGridSvg() {
  const svg = document.getElementById('gridsvg');
  svg.addEventListener('pointerdown', e => {
    if (!gridEditable()) return;
    if (e.target.classList.contains('gh')) {
      gdrag = +e.target.dataset.i;
      svg.setPointerCapture(e.pointerId);
      e.preventDefault();
    }
  });
  svg.addEventListener('pointermove', e => {
    if (gdrag < 0 || !grid) return;
    grid.corners[gdrag] = ptFrac(svg, e);
    drawGrid();
  });
  svg.addEventListener('pointerup', () => {
    if (gdrag >= 0) {
      gdrag = -1;
      saveGrid();
    }
  });
  svg.addEventListener('click', e => {
    if (!canEdit) return;
    if (!e.target.classList.contains('gc')) return;
    const k = e.target.dataset.k,
      cur = grid.names[k] || '';
    const v = prompt('Name for cell ' + k + ':', cur);
    if (v !== null) {
      if (v.trim()) grid.names[k] = v.trim();
      else delete grid.names[k];
      drawGrid();
      saveGrid();
    }
  });
  document.getElementById('gridshow').addEventListener('change', e => {
    grid.show = e.target.checked;
    drawGrid();
    saveGrid();
  });
  document.getElementById('detectbtn').addEventListener('click', detectGrid);
  {
    const cb = document.getElementById('capturebtn');
    if (cb) cb.addEventListener('click', capturePhoto);
  }
  {
    const ab = document.getElementById('alignbtn');
    if (ab) ab.addEventListener('click', () => (aligning ? stopAlign() : startAlign()));
  }
  const upd = () => {
    if (!gridEditable()) {
      syncGridControls();
      return;
    }
    grid.rows = Math.max(1, Math.min(12, +document.getElementById('gridrows').value || 4));
    grid.cols = Math.max(1, Math.min(12, +document.getElementById('gridcols').value || 4));
    drawGrid();
    saveGrid();
  };
  document.getElementById('gridrows').addEventListener('change', upd);
  document.getElementById('gridcols').addEventListener('change', upd);
  const lockBtn = document.getElementById('gridlock');
  if (lockBtn)
    lockBtn.addEventListener('click', () => {
      if (!grid || !canEdit) return;
      grid.locked = !grid.locked;
      applyGridLock();
      drawGrid();
      saveGrid();
    });
}
export function applyGridLock() {
  if (!grid) return;
  const locked = !!grid.locked;
  document.body.classList.toggle('gridlocked', locked);
  const btn = document.getElementById('gridlock');
  if (btn) btn.textContent = locked ? '\uD83D\uDD13 Unlock grid' : '\uD83D\uDD12 Lock grid';
}
export function adoptGrid(g) {
  grid = g;
  if (!grid.names) grid.names = {};
  if (grid.locked === undefined) grid.locked = false;
  syncGridControls();
}
export function handleGrid(j) {
  if (j.settings && j.settings.grid) {
    const srv = j.settings.grid;
    if (grid === null) {
      adoptGrid(srv);
    } else if (gdrag < 0 && !gridDirty && JSON.stringify(srv) !== JSON.stringify(grid)) {
      // another tab/device saved a newer grid; sync to it instead of holding
      // a stale copy that could later overwrite the saved one
      adoptGrid(srv);
    }
    if (gdrag < 0) drawGrid();
  }
}

export async function refresh() {
  let j = null;
  try {
    const r = await fetch('/api/status');
    if (!r.ok && r.status !== 503) throw 0;
    j = await r.json();
  } catch (e) {
    // the server genuinely didn't answer
    document.getElementById('phase').textContent = 'Controller unreachable';
    return;
  }
  applyStatus(j);
}

// The whole render, split out so a pushed status and a polled one go through
// exactly the same path. Anything that renders differently depending on how
// the data arrived is a bug waiting to happen.
export function applyStatus(j) {
  try {
    set_S({
      ...j,
      now: new Date(j.now),
      on: new Date(j.on),
      off: new Date(j.off),
      sunrise: new Date(j.sunrise),
      sunset: new Date(j.sunset)
    });
    // Same hold as the form fields: until the server echoes a saved backend,
    // keep the chosen one. Otherwise a stale poll flips the slider and sweep
    // card back into view for one cycle and the whole card jumps.
    set_lightBackend(
      'light_backend' in pendingSave ? pendingSave.light_backend : j.light_backend || 'pwm'
    ); // set before any renderer reads it
    fillForm(j.settings);
    {
      const camOn = !!(j.settings && j.settings.camera_enabled);
      for (const id of ['photocard', 'videocard', 'reportcard']) {
        const el = document.getElementById(id);
        if (el) el.dataset.camoff = camOn ? '' : '1';
        if (el && !camOn) el.style.display = 'none';
      }
      window._camOn = camOn;
      // collapse the media column entirely, or its grid track sits empty
      document.body.classList.toggle('nocam', !camOn);
    }
    if (window._camOn) renderPhoto(j);
    renderVideoState(j);
    loadFrames();
    applyAuth(j);
    setAiControls(j.settings);
    applySetup(j);
    {
      const rc = document.getElementById('reportctl');
      if (rc) rc.style.display = canEdit ? '' : 'none';
    }
    fetchReport();
    requestAnimationFrame(fitReportHeight);
    handleGrid(j);
    renderSensors(j);
    renderQuality(j);
    renderStorm(j);
    renderCalibration(j);
    renderLight2(j);
    renderTrays(j);
    renderTrayConfig(j.settings);
    renderSweep(j);
    renderFocus(j);
    renderFan(j);
    renderHeat(j);
    renderDayProgress(j);
    renderLightPlan(j);
    if (Date.now() - lastHostLoad > 60000) {
      set_lastHostLoad(Date.now());
      loadHost();
    }
    if (Date.now() - lastChartLoad > 120000) loadChart(); // history every ~2 min
    renderWater(j);
    render();
  } catch (e) {
    // the server answered but our page code failed: usually a stale cached
    // script after a deploy. Say so instead of blaming the controller.
    console.error('render error:', e);
    document.getElementById('phase').textContent = 'Page error \u2013 hard-refresh (Ctrl-Shift-R)';
  }
}

// ---------------- AI garden report ----------------
export function rBadge(h) {
  const m = {
    good: ['Healthy', 'rbg-good'],
    watch: ['Watch', 'rbg-watch'],
    problem: ['Problem', 'rbg-problem']
  };
  const v = m[h] || ['\u2014', 'rbg-watch'];
  return `<span class="rbadge ${v[1]}">${v[0]}</span>`;
}
export function rList(title, arr) {
  if (!arr || !arr.length) return '';
  return `<div class="rsec"><h4>${title}</h4><ul>${arr.map(x => `<li>${esc(String(x))}</li>`).join('')}</ul></div>`;
}
export function rAgo(ts) {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleString([], {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  });
}
export function fitReportHeight() {
  const card = document.getElementById('reportcard');
  const media = document.querySelector('.amedia');
  if (!card || !media) return;
  if (window.innerWidth < 1100) {
    card.style.maxHeight = '';
    return;
  } // single column: let it flow
  const top = card.getBoundingClientRect().top;
  const mediaBottom = media.getBoundingClientRect().bottom;
  card.style.maxHeight = Math.max(220, Math.round(mediaBottom - top)) + 'px';
}
export function renderReport(j) {
  const body = document.getElementById('reportbody');
  if (!body) return;
  if (j.generating) {
    body.innerHTML = '<p class="rmuted">Generating report\u2026</p>';
    return;
  }
  if (j.have_key === false) {
    body.innerHTML =
      '<p class="rmuted">No API key on the controller. Add a <code>.anthropic_key</code> file (or set ANTHROPIC_API_KEY) to enable AI reports.</p>';
    return;
  }
  if (j.ok === null || j.ok === undefined) {
    body.innerHTML =
      '<p class="rmuted">No report yet. Generate one, or enable the daily report.</p>';
    return;
  }
  if (j.ok === false) {
    body.innerHTML = `<p class="rmuted">Last attempt failed: ${esc(j.error || 'unknown error')}</p>`;
    return;
  }
  const r = j.report || {};
  let h = `<div class="rhead">${rBadge(r.overall_health)}<span class="rtime">${rAgo(j.ts)}${j.model ? ' \u00b7 ' + esc(j.model) : ''}</span></div>`;
  if (r.summary) h += `<p class="rsummary">${esc(r.summary)}</p>`;
  const f = [];
  if (r.germination && r.germination.sprouted != null && r.germination.total_cells != null)
    f.push(`Germinated ${r.germination.sprouted}/${r.germination.total_cells}`);
  if (r.growth_stage) f.push('Stage: ' + esc(r.growth_stage));
  if (r.light && r.light.assessment) f.push('Light: ' + esc(r.light.assessment));
  if (r.water && r.water.assessment) f.push('Water: ' + esc(r.water.assessment));
  if (f.length) h += `<p class="rfacts">${f.join(' \u00b7 ')}</p>`;
  if (r.light && r.light.reason) h += `<p class="rreason"><b>Light:</b> ${esc(r.light.reason)}</p>`;
  if (r.water && r.water.reason) h += `<p class="rreason"><b>Water:</b> ${esc(r.water.reason)}</p>`;
  h += rList('Concerns', r.concerns);
  h += rList('Recommendations', r.recommendations);
  if (r.per_cell && r.per_cell.length)
    h += `<div class="rsec"><h4>Cell notes</h4><ul>${r.per_cell.map(c => `<li><b>${esc(c.cell || '')}</b> ${esc(c.note || '')}</li>`).join('')}</ul></div>`;
  // Only cells that look wrong are worth showing: the planting map already
  // records what is in every cell, so a list of confirmations is noise.
  {
    const vc = (r.variety_check || []).filter(v => v && v.looks_consistent === false);
    if (vc.length)
      h += `<div class="rsec"><h4>Possible mix-ups</h4><ul>${vc
        .map(
          v =>
            `<li><b>${esc(v.cell || '')}</b> recorded as ${esc(v.expected || '?')}` +
            `${v.why ? ` &mdash; ${esc(v.why)}` : ''}</li>`
        )
        .join('')}</ul></div>`;
    // reports saved by older versions lack this field
    if (!r.variety_check && r.species && r.species.length)
      h += `<div class="rsec"><h4>Species guesses</h4><ul>${r.species
        .map(sp => `<li><b>${esc(sp.cell || '')}</b> ${esc(sp.guess || 'unsure')}</li>`)
        .join('')}</ul></div>`;
  }
  if (r.confidence)
    h += `<p class="rconf">Confidence: ${esc(r.confidence)}${j.parse_error ? ' \u00b7 (reply was not structured JSON)' : ''}</p>`;
  body.innerHTML = h;
}
export let lastReportSig = null;
export async function fetchReport() {
  try {
    const r = await fetch('/api/report');
    const j = await r.json();
    const sig = `${j.ts}|${j.generating}|${j.ok}|${j.have_key}`;
    if (sig !== lastReportSig) {
      // only re-render when something changed
      lastReportSig = sig;
      renderReport(j);
      requestAnimationFrame(fitReportHeight);
    }
    if (j.generating) setTimeout(fetchReport, 4000); // poll faster until it lands
  } catch (e) {}
}
export async function genReport() {
  const info = document.getElementById('reportinfo');
  if (info) info.textContent = 'working\u2026';
  document.getElementById('reportbody').innerHTML =
    '<p class="rmuted">Generating report\u2026 this takes ~20s.</p>';
  try {
    const r = await fetch('/api/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    });
    const j = await r.json();
    if (info) info.textContent = '';
    if (j.ok) {
      renderReport({ ...j, have_key: true });
      lastReportSig = `${j.ts}|${j.generating}|${j.ok}|true`;
    } else renderReport({ ok: false, error: j.error || 'HTTP ' + r.status });
    requestAnimationFrame(fitReportHeight);
  } catch (e) {
    if (info) info.textContent = '';
    document.getElementById('reportbody').innerHTML =
      '<p class="rmuted">Request timed out, but it may still be generating. Reload in a moment to see it.</p>';
  }
}
// The seedling DLI target band, from the setup (Settings, Setups). The bar's scale grows
// to fit a high band: 16 mol for a 6 to 12 band, 24 for 15 to 20, and so on.
export var dliBand = { lo: 15, hi: 20, max: 24 }; // var: read by renders that can run before this line
export function setDliBand(s) {
  if (!s) return;
  const lo = parseFloat(s.dli_target_low),
    hi = parseFloat(s.dli_target_high);
  if (!(lo > 0 && hi > lo)) return;
  const max = [16, 24, 32, 40, 48, 60, 80].find(m => m >= hi * 1.15) || Math.ceil(hi * 1.15);
  if (lo === dliBand.lo && hi === dliBand.hi && max === dliBand.max && dliBand.drawn) return;
  dliBand = { lo, hi, max, drawn: true };
  const pct = v => ((v / max) * 100).toFixed(2) + '%';
  const band = document.getElementById('dliband');
  if (band) {
    band.style.left = pct(lo);
    band.style.width = pct(hi - lo);
  }
  const sc = document.getElementById('dliscale');
  if (sc)
    sc.innerHTML =
      `<i style="left:${pct(lo)}"></i><i style="left:${pct(hi)}"></i>` +
      `<span class="s0" style="left:0">0</span>` +
      `<span style="left:${pct(lo)}">${lo}</span>` +
      // the word "target" only fits between the two numbers on a wide band
      ((hi - lo) / max >= 0.3
        ? `<span class="sband" style="left:${pct((lo + hi) / 2)}">target</span>`
        : '') +
      `<span style="left:${pct(hi)}">${hi}</span>` +
      `<span class="s16" style="left:100%">${max} mol</span>`;
}
export function setAiControls(s) {
  if (!s) return;
  const en = document.getElementById('aienabled'),
    t = document.getElementById('aitime');
  if (en && document.activeElement !== en) en.checked = !!s.ai_enabled;
  if (t && document.activeElement !== t && s.ai_report_hour != null) {
    const h = String(s.ai_report_hour).padStart(2, '0');
    const m = String(s.ai_report_minute || 0).padStart(2, '0');
    t.value = `${h}:${m}`;
  }
}
export async function saveAi() {
  const en = document.getElementById('aienabled'),
    t = document.getElementById('aitime');
  const parts = (t.value || '08:00').split(':');
  const h = Math.min(23, Math.max(0, parseInt(parts[0], 10) || 0));
  const m = Math.min(59, Math.max(0, parseInt(parts[1], 10) || 0));
  try {
    await fetch('/api/ai_settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ai_enabled: en.checked, ai_report_hour: h, ai_report_minute: m })
    });
  } catch (e) {}
}
export async function resetTimelapse(confirmed) {
  const info = document.getElementById('renderinfo');
  try {
    const r = await fetch('/api/reset_timelapse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(confirmed ? { confirm: true, clear_readings: true } : {})
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      if (info) info.textContent = 'log in first';
      return;
    }
    if (!j.ok && j.needs_confirm && !confirmed) {
      // ask once, never loop
      if (
        window.confirm(
          `Archive ${j.photos} photos and start a new timelapse?\n\n` +
            `They are moved to timelapse_archive/, not deleted. Per-cell camera ` +
            `readings are cleared too, since they were measured against the old ` +
            `camera position. Probe, temperature and light history is kept.`
        )
      )
        return resetTimelapse(true);
      return;
    }
    if (!j.ok) {
      if (info) info.textContent = j.error || 'failed';
      return;
    }
    if (info) info.textContent = `archived ${j.archived} photos`;
    refresh();
  } catch (e) {
    if (info) info.textContent = 'request failed';
  }
}
export function initReport() {
  const gb = document.getElementById('genreport');
  if (gb) gb.addEventListener('click', genReport);
  const en = document.getElementById('aienabled');
  if (en) en.addEventListener('change', saveAi);
  const t = document.getElementById('aitime');
  if (t) t.addEventListener('change', saveAi);
  window.addEventListener('resize', fitReportHeight);
  if ('ResizeObserver' in window) {
    const m = document.querySelector('.amedia');
    if (m) new ResizeObserver(() => fitReportHeight()).observe(m);
  }
  fetchReport();
}

// Runs once at page load; main.js calls each module's start() in a fixed order.
export function start() {
  document.getElementById('cfgform').addEventListener('submit', async ev => {
    ev.preventDefault();
    const f = ev.target,
      msg = document.getElementById('msg');
    const body = {};
    // Setups have their own Save button, but a change there must not be lost
    // when the main Save is the one pressed
    if (setupDirty && setupDraft) body.setups = setupDraft;
    // every Settings field, read the way config.FORM says it is stored
    for (const k of Object.keys(FORM)) {
      const v = readField(f, k, FORM[k]);
      if (v !== undefined) body[k] = v;
    }
    msg.textContent = 'Planting...';
    msg.className = '';
    try {
      const r = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const j = await r.json();
      const errs = j.errors && Object.keys(j.errors);
      // a rejected field inside a collapsed section would be invisible: open the
      // sections holding any errors so the message points at something on screen
      f.querySelectorAll('[aria-invalid="true"]').forEach(el => el.removeAttribute('aria-invalid'));
      for (const k of errs || []) {
        const el = k === 'setups' ? document.getElementById('setupcfg') : f.elements[k];
        const grp = el && el.closest && el.closest('details.fgroup');
        if (grp) grp.open = true;
        if (el && el.setAttribute) el.setAttribute('aria-invalid', 'true'); // outlined until fixed
      }
      // hold every accepted field until the server echoes it back; it is no
      // longer an unsaved edit. A rejected one stays as typed, to be fixed.
      for (const k of j.saved || []) if (k in body) pendingSave[k] = body[k];
      for (const k of Object.keys(body)) if (!(j.errors && k in j.errors)) formDirty.delete(k);
      formDirty.delete('kasa_pass');
      if ('setups' in body && (j.saved || []).includes('setups')) {
        set_setupDirty(false);
        const sm = document.getElementById('setupmsg');
        if (sm) sm.textContent = 'Saved.';
      }
      if (r.ok && j.ok) {
        msg.textContent = 'Saved \u{1F331}';
        msg.className = 'ok';
        refresh();
      } else if (errs && errs.length) {
        // everything valid was saved; say exactly which fields were rejected
        msg.textContent =
          'Saved, except: ' + errs.map(k => fieldLabel(f, k) + ' ' + j.errors[k]).join('; ');
        msg.className = 'err';
        refresh();
      } else {
        msg.textContent = j.error || 'Save failed';
        msg.className = 'err';
      }
    } catch (e) {
      msg.textContent = 'Save failed';
      msg.className = 'err';
    }
  });
  setInterval(() => {
    const d = new Date();
    document.getElementById('clock').textContent = d.toLocaleTimeString();
    if (S) {
      S.now = d;
    }
  }, 1000);
}
