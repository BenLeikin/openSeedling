// openSeedling dashboard: The view crop and alignment guides, the Settings form's fill and save (driven by config.FORM), the timelapse player, the enlarged view, and sign-in.
// An ES module: what it uses from the others is imported at the top, and what
// it offers is exported. Another module cannot assign one of its variables
// directly; it calls the set_<name>() exported at the bottom. Code that runs
// at page load is in start(), which main.js calls in a fixed order.
// ---- view crop: drag a rectangle on the full photo ----
// The crop is a view setting: stored photos stay full, and the snapshot,
// scrubber, video and AI report are cut to it. Only used when photos are
// not flattened (a flattened photo is already just the tray).
import { sensorData, sensorMeta, set_units, tDisp, tFromF, tToF, tUnit } from './charts.js';
import { esc } from './trays.js';
import { applyTheme } from './devices.js';
import { gridEditable, refresh, resetTimelapse } from './grid.js';
import { restartStream } from './live.js';
import { set_buddyOn, set_buddyPick } from './buddy.js';
import { showScheduleMode, syncCaptureLight, syncUsbAuto } from './cards.js';

export function parseRoi(s) {
  const m = String(s || '')
    .trim()
    .split(',')
    .map(Number);
  return m.length === 4 &&
    m.every(n => isFinite(n) && n >= 0 && n <= 1) &&
    m[2] >= 0.05 &&
    m[3] >= 0.05
    ? m
    : null;
}
export var cropping = false,
  cropSel = null,
  cropDrag = null; // var: renderPhoto reads it
export function drawCrop() {
  const svg = document.getElementById('guidesvg');
  if (!svg) return;
  const r = cropSel;
  svg.innerHTML = r
    ? `<path d="M0 0H1000V1000H0Z M${r[0] * 1000} ${r[1] * 1000}v${r[3] * 1000}h${r[2] * 1000}v${-r[3] * 1000}Z"` +
      ` fill="rgba(0,0,0,.45)" fill-rule="evenodd"/>` +
      `<rect x="${r[0] * 1000}" y="${r[1] * 1000}" width="${r[2] * 1000}" height="${r[3] * 1000}" fill="none"` +
      ` stroke="#ffd54a" stroke-width="3" stroke-dasharray="12 9" vector-effect="non-scaling-stroke"/>`
    : '';
  const info = document.getElementById('photoinfo');
  if (info)
    info.textContent = r
      ? `Crop ${Math.round(r[2] * 100)}% \u00d7 ${Math.round(r[3] * 100)}% of the frame \u00b7 drag to redraw`
      : 'Drag a rectangle over the area to keep';
  if (info && window._flatOn)
    info.textContent +=
      ' \u00b7 note: Show photos flattened is on, so the crop applies once it is off';
}
export function startCrop() {
  if (cropping) return;
  if (aligning) stopAlign();
  cropping = true;
  const img = document.getElementById('photo');
  img.dataset.flat = '';
  img.dataset.crop = '';
  img.onerror = null;
  img.src = '/photo/latest?' + Date.now(); // shown until the live frame lands
  // then a fresh full frame from the camera, uncropped: choose from what the
  // camera sees now, in the same mode the photos are taken in
  fetch('/api/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}'
  })
    .then(r => r.json())
    .then(j => {
      if (cropping && j && j.ok) img.src = '/preview.jpg?' + j.ts;
    })
    .catch(() => {});
  document.getElementById('gridsvg').style.display = 'none';
  const svg = document.getElementById('guidesvg');
  svg.style.display = '';
  svg.style.pointerEvents = 'auto';
  svg.style.cursor = 'crosshair';
  cropSel = parseRoi(
    document.querySelector('[name=roi]') && document.querySelector('[name=roi]').value
  );
  document.getElementById('cropctl').style.display = '';
  // only the crop controls while choosing: fewer buttons, no wrapping
  for (const id of ['cropbtn', 'capturebtn', 'alignbtn', 'cropreset']) {
    const b = document.getElementById(id);
    if (b) b.style.display = 'none';
  }
  drawCrop();
}
export function stopCrop() {
  cropping = false;
  cropDrag = null;
  const svg = document.getElementById('guidesvg');
  svg.style.display = 'none';
  svg.style.pointerEvents = '';
  svg.style.cursor = '';
  svg.innerHTML = '';
  document.getElementById('cropctl').style.display = 'none';
  for (const id of ['cropbtn', 'capturebtn', 'alignbtn']) {
    const b = document.getElementById(id);
    if (b) b.style.display = '';
  }
  refresh();
}
export async function saveCrop(roiStr) {
  const info = document.getElementById('photoinfo');
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ roi: roiStr })
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) {
      if (info)
        info.textContent =
          'Crop not saved: ' + ((j.errors && j.errors.roi) || j.error || 'HTTP ' + r.status);
      return;
    }
    const el = document.querySelector('[name=roi]');
    if (el) el.value = roiStr;
  } catch (e) {
    if (info) info.textContent = 'Crop not saved: request failed';
    return;
  }
  stopCrop();
}
export function cropPoint(ev) {
  const b = document.getElementById('guidesvg').getBoundingClientRect();
  return [
    Math.min(1, Math.max(0, (ev.clientX - b.left) / b.width)),
    Math.min(1, Math.max(0, (ev.clientY - b.top) / b.height))
  ];
}

export let aligning = false,
  alignTimer = null;
export function drawGuides() {
  const svg = document.getElementById('guidesvg');
  if (!svg) return;
  const el = document.querySelector('[name=roi]');
  const m = ((el && el.value) || '')
    .trim()
    .split(',')
    .map(s => parseFloat(s));
  let roi = '';
  if (m.length === 4 && m.every(n => !isNaN(n) && n >= 0 && n <= 1)) {
    roi =
      `<rect x="${m[0] * 1000}" y="${m[1] * 1000}" width="${m[2] * 1000}" height="${m[3] * 1000}" ` +
      `fill="none" stroke="#ffd54a" stroke-width="3" stroke-dasharray="12 9" vector-effect="non-scaling-stroke"/>`;
  }
  svg.innerHTML =
    '<line x1="333" y1="0" x2="333" y2="1000"/><line x1="667" y1="0" x2="667" y2="1000"/>' +
    '<line x1="0" y1="333" x2="1000" y2="333"/><line x1="0" y1="667" x2="1000" y2="667"/>' +
    '<line x1="500" y1="468" x2="500" y2="532"/><line x1="468" y1="500" x2="532" y2="500"/>' +
    roi;
}
export async function alignTick() {
  if (!aligning) return;
  try {
    const r = await fetch('/api/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    });
    const j = await r.json();
    if (j.ok) {
      const img = document.getElementById('photo');
      img.style.display = '';
      img.src = '/preview.jpg?' + j.ts; // busy/error: keep the last frame
      const info = document.getElementById('photoinfo');
      // sharpness score: bigger is sharper for this scene; walk the focus
      // setting and keep whatever maximizes it
      if (info && j.sharpness != null)
        info.textContent =
          'Live preview \u00b7 sharpness ' + Math.round(j.sharpness) + ' (higher = sharper)';
    }
  } catch (e) {}
  if (aligning) alignTimer = setTimeout(alignTick, 1200);
}
export function startAlign() {
  if (aligning) return;
  if (cropping) stopCrop();
  aligning = true;
  const btn = document.getElementById('alignbtn');
  if (btn) {
    btn.textContent = '\u23F9 Stop align';
    btn.classList.add('on');
  }
  document.getElementById('gridsvg').style.display = 'none'; // hide grid while aiming
  const g = document.getElementById('guidesvg');
  if (g) g.style.display = '';
  drawGuides();
  document.getElementById('photocard').style.display = '';
  document.getElementById('photoinfo').textContent =
    'Live preview \u00b7 move the camera; the frame updates';
  alignTick();
}
export function stopAlign() {
  aligning = false;
  if (alignTimer) {
    clearTimeout(alignTimer);
    alignTimer = null;
  }
  const btn = document.getElementById('alignbtn');
  if (btn) {
    btn.textContent = '\uD83C\uDFAF Align';
    btn.classList.remove('on');
  }
  const g = document.getElementById('guidesvg');
  if (g) g.style.display = 'none';
  refresh(); // restore the normal snapshot and grid overlay
}

// Values the user just saved, held until a status arrives that reflects them.
// Without this, any poll landing between the POST and the server's next status
// repaints the form with the OLD value, so a changed dropdown visibly snaps
// back before snapping forward again.
export let pendingSave = {};
// Fields changed on the page but not saved yet. A status arrives every few
// seconds (the live readings), and each one repaints the form; guarding only
// the focused field meant a choice reverted as soon as focus moved on, which
// on a phone is right after picking from a dropdown. A changed field now keeps
// the user's value until Save sends it (or the page is reloaded).
export const formDirty = new Set();
export function markDirty(ev) {
  const el = ev.target;
  if (!el || !el.name || !el.form || el.form.id !== 'cfgform') return;
  formDirty.add(el.name);
  const msg = document.getElementById('msg');
  if (msg && !/Planting/.test(msg.textContent)) {
    msg.textContent = 'Unsaved changes';
    msg.className = '';
  }
}

export function formHolds(key, cfg) {
  const f = document.getElementById('cfgform');
  if (formDirty.has(key)) return true;
  if (f && f.elements[key] && document.activeElement === f.elements[key]) return true;
  if (!(key in pendingSave)) return false;
  if (cfg && String(cfg[key]) === String(pendingSave[key])) {
    delete pendingSave[key]; // server agrees; stop holding
    return false;
  }
  return true; // still stale, keep what the user chose
}

export function fillForm(cfg) {
  if (!cfg) return;
  const f = document.getElementById('cfgform');
  // the display units first: temperature fields are converted with them
  if (f.elements['units'] && !formHolds('units', cfg)) set_units(cfg.units || 'imperial');
  {
    // the probe list depends on what is plugged in; build it before its value is set
    const hs = f.elements['heat_sensor'];
    if (hs) {
      const keys = new Set([
        'temp:soil',
        cfg.heat_sensor || 'temp:soil',
        ...Object.keys(sensorData || {}).filter(k => /^temp:soil(_\w+)?$/.test(k))
      ]);
      const have = [...hs.options].map(o => o.value).join('|');
      const want = [...keys].sort().join('|');
      if (have !== want && !formHolds('heat_sensor', cfg))
        hs.innerHTML = [...keys]
          .sort()
          .map(k => `<option value="${esc(k)}">${esc(sensorMeta(k, 0).label || k)}</option>`)
          .join('');
    }
  }
  // every Settings field, filled the way config.FORM says it is stored
  for (const k of Object.keys(FORM)) fillField(f, k, FORM[k], cfg);
  // what goes with some of them on screen
  {
    const sm = f.elements['schedule_mode'];
    showScheduleMode(sm ? sm.value : 'solar');
  }
  document.querySelectorAll('.tunit').forEach(e => (e.innerHTML = tUnit()));
  {
    const ll = document.getElementById('threshlolbl');
    if (ll) ll.innerHTML = tUnit();
    const lbl = document.getElementById('threshlbl');
    if (lbl) lbl.innerHTML = tUnit();
  }
  {
    const cb = f.elements['camera_backend'];
    // only show the UVC controls when a USB camera is selected
    document
      .querySelectorAll('.usbonly')
      .forEach(el => (el.style.display = cb && cb.value === 'usb' ? '' : 'none'));
    syncUsbAuto();
  }
  applyTheme(cfg['theme'] || 'auto');
  syncCaptureLight();
  set_buddyOn(cfg['little_buddy'] !== false);
  set_buddyPick(cfg['buddy_model'] || 'sprout');
}

// config.FORM, embedded in the page: each Settings field's kind and default
export var FORM = (() => {
  try {
    return JSON.parse(document.getElementById('formspec').textContent);
  } catch (e) {
    return {};
  }
})();

// One field from the form, as the server stores it; undefined leaves it out
export function readField(f, k, spec) {
  const el = f.elements[k];
  if (!el) return undefined;
  const raw = (el.value == null ? '' : String(el.value)).trim();
  switch (spec.kind) {
    case 'bool':
      return el.checked;
    case 'int':
      return raw === '' ? spec.default : parseInt(raw, 10);
    case 'float':
      return raw === '' ? spec.default : parseFloat(raw);
    case 'choice':
      return spec.ints ? parseInt(raw, 10) : raw;
    case 'time':
      return raw;
    case 'text':
      return raw;
    case 'secret':
      return el.value ? el.value : undefined; // blank keeps the stored one
    case 'tempF': {
      // typed in the units the field shows; stored in F
      if (raw === '') return spec.zero_off ? 0 : undefined;
      const v = parseFloat(raw);
      if (spec.zero_off && !v) return 0;
      const fv = tToF(v);
      return spec.round === 1 ? Math.round(fv) : Math.round(fv * 10) / 10;
    }
  }
  return undefined;
}

// One field filled from the server's settings, unless it is being edited
export function fillField(f, k, spec, cfg) {
  const el = f.elements[k];
  if (!el || spec.kind === 'secret' || formHolds(k, cfg)) return; // a password is never sent back
  const v = k in cfg ? cfg[k] : spec.default;
  if (spec.kind === 'bool') {
    el.checked = !!v;
    return;
  }
  if (spec.kind === 'tempF') {
    const fv = +v || 0;
    el.value =
      spec.zero_off && !fv
        ? 0
        : spec.round === 1
          ? Math.round(tFromF(fv))
          : Math.round(tFromF(fv) * 10) / 10;
    return;
  }
  el.value = v == null ? '' : v;
}

export let frames = [],
  fidx = 0,
  ptimer = null;
export function frameLabel(n) {
  const m = n.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})/);
  return m ? `${m[2]}/${m[3]} ${m[4]}:${m[5]}` : n;
}
export function frameTs(n) {
  // filename encodes local capture time; _m suffix (manual) parses the same
  const m = n.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
  if (!m) return null;
  return Math.floor(new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime() / 1000);
}
export let ctxTimer = null;
export function loadFrameContext(name) {
  // debounced: fires only when scrubbing pauses, never per-frame in playback
  const el = document.getElementById('pctx');
  if (!el) return;
  clearTimeout(ctxTimer);
  ctxTimer = setTimeout(async () => {
    const ts = frameTs(name);
    if (ts == null) {
      el.textContent = '';
      return;
    }
    try {
      const r = await fetch('/api/frame_context?ts=' + ts);
      const j = await r.json();
      const d = j.readings || {};
      const bits = [];
      if (d.soil_c != null) bits.push(`soil ${tDisp(d.soil_c).toFixed(1)}${tUnit()}`);
      if (d.air_c != null) bits.push(`air ${tDisp(d.air_c).toFixed(1)}${tUnit()}`);
      if (d.humidity != null) bits.push(`${d.humidity.toFixed(0)}% RH`);
      if (d.lux != null) bits.push(`${Math.round(d.lux).toLocaleString()} lx`);
      el.textContent = bits.length ? ' \u00b7 ' + bits.join(' \u00b7 ') : '';
    } catch (e) {
      el.textContent = '';
    }
  }, 350);
}
export function showFrame() {
  if (!frames.length) return;
  document.getElementById('vframe').src = '/thumb/' + frames[fidx] + '?v=' + thumbsV;
  document.getElementById('scrub').value = fidx;
  document.getElementById('pframe').textContent =
    `${frameLabel(frames[fidx])} \u00b7 ${fidx + 1}/${frames.length}`;
  loadFrameContext(frames[fidx]);
  new Image().src = '/thumb/' + frames[(fidx + 1) % frames.length] + '?v=' + thumbsV;
  loadSharpFrame();
}
// Thumbnails are 640px: right for playing, soft once stretched across the
// card. When the player stops on a frame, swap in the full-resolution one
// (framed the same way), after a short pause so scrubbing stays light.
export var sharpTimer = null;
export function loadSharpFrame() {
  clearTimeout(sharpTimer);
  if (ptimer || !frames.length) return;
  const name = frames[fidx];
  sharpTimer = setTimeout(() => {
    if (ptimer || frames[fidx] !== name) return;
    const im = new Image();
    im.onload = () => {
      if (!ptimer && frames[fidx] === name) document.getElementById('vframe').src = im.src;
    };
    im.src = '/frame/' + encodeURIComponent(name);
  }, 350);
}
export function stopPlay() {
  if (ptimer) {
    clearInterval(ptimer);
    ptimer = null;
  }
  document.getElementById('playbtn').innerHTML = '&#9654; Grow';
  loadSharpFrame();
}
export function togglePlay() {
  if (ptimer) {
    stopPlay();
    return;
  }
  if (fidx >= frames.length - 1) fidx = 0;
  document.getElementById('playbtn').innerHTML = '&#9208; Pause';
  ptimer = setInterval(
    () => {
      if (fidx >= frames.length - 1) {
        stopPlay();
        return;
      }
      fidx++;
      showFrame();
    },
    Math.round(1000 / Math.max(0.5, playerFps))
  );
}
export var thumbsV = 0; // bumped by the server when thumbnails are rebuilt
export var playerFps = 4; // player_fps: frames a second in the dashboard player
// Opened from the touchscreen's summary (/?kiosk=1): a button back, and back on
// its own after three minutes untouched, so the screen never stays scrolled
// halfway down the dashboard.

export async function loadFrames() {
  if (window._camOn === false) return;
  try {
    const r = await fetch('/api/photos');
    const j = await r.json();
    const had = frames.length;
    frames = j.names || [];
    if (j.v != null && j.v !== thumbsV) {
      thumbsV = j.v;
      if (had) showFrame();
    } // rebuilt: refetch
    const card = document.getElementById('videocard');
    if (frames.length < 2) {
      card.style.display = 'none';
      return;
    }
    card.style.display = '';
    document.getElementById('scrub').max = frames.length - 1;
    if (!had) {
      fidx = frames.length - 1;
      showFrame();
    }
  } catch (e) {}
}

export let renderTimer = null,
  renderStart = null;
export function clearRenderTimer() {
  if (renderTimer) {
    clearInterval(renderTimer);
    renderTimer = null;
  }
}
export function tickRender() {
  if (renderStart === null) return;
  const s = Math.floor((Date.now() - renderStart) / 1000);
  const mm = String(Math.floor(s / 60)).padStart(2, '0'),
    ss = String(s % 60).padStart(2, '0');
  const f = window._renderFrames ? ` of ${window._renderFrames} frames` : '';
  document.getElementById('renderinfo').textContent = `Rendering${f}... ${mm}:${ss} elapsed`;
}
export function renderVideoState(j) {
  const info = document.getElementById('renderinfo');
  const dl = document.getElementById('dlbtn');
  const btn = document.getElementById('renderbtn');
  const st = j.render || {};
  const running = st.state === 'running';
  btn.disabled = running;
  btn.textContent = running ? '\u23F3 Rendering...' : '\uD83C\uDFA5 Render video';
  if (running) {
    window._renderFrames = st.frames || 0;
    if (renderStart === null) {
      renderStart = st.started ? new Date(st.started).getTime() : Date.now();
      clearRenderTimer();
      renderTimer = setInterval(tickRender, 1000);
    }
    tickRender();
  } else {
    clearRenderTimer();
    renderStart = null;
    if (!canEdit && st.state !== 'running') {
      // read-only viewer: hide render chatter, keep the video timestamp
      if (j.video_time) {
        const w = new Date(j.video_time);
        info.textContent = 'Video from ' + w.toLocaleString();
      } else info.textContent = '';
    } else if (st.state === 'error') {
      info.textContent =
        'Render error' + (st.elapsed ? ` after ${Math.round(st.elapsed)}s` : '') + ': ' + st.msg;
    } else if (j.video_time) {
      const when = new Date(j.video_time);
      info.textContent =
        'Video from ' +
        when.toLocaleString() +
        (j.video_fps ? ` \u00b7 ${j.video_fps} fps` : '') +
        (st.state === 'done' ? ' \u00b7 ' + st.msg : '');
    } else {
      info.textContent = 'No video rendered yet';
    }
  }
  dl.style.display = !running && j.video_time ? '' : 'none';
  if (!running && j.video_time) dl.href = '/video?t=' + encodeURIComponent(j.video_time);
}

// ---------------- enlarged view ----------------
// Click (tap on a phone) the snapshot or the timelapse frame to see it full
// screen; X, Esc or a click outside the picture closes it. Not while the photo
// is being edited (grid corners or a crop), and not on a cell of the shown grid
// when signed in, where a click names the cell.
export var lbReturn = null,
  lbSeq = 0,
  lbOpened = 0;
export function openLightbox(src, cap, full) {
  const lb = document.getElementById('lightbox'),
    im = document.getElementById('lbimg');
  if (!lb || !src) return;
  const seq = ++lbSeq;
  lbReturn = document.activeElement;
  im.src = src;
  im.alt = cap || 'Enlarged photo';
  document.getElementById('lbcap').textContent = cap || '';
  if (full && full !== src) {
    // show what is on screen now, then sharpen
    const hi = new Image();
    hi.onload = () => {
      if (!lb.hidden && seq === lbSeq) im.src = hi.src;
    };
    hi.src = full;
  }
  lb.hidden = false;
  lbOpened = Date.now();
  document.documentElement.classList.add('lbopen');
  document.getElementById('lbclose').focus();
}
export function closeLightbox() {
  const lb = document.getElementById('lightbox');
  if (!lb || lb.hidden) return;
  lb.hidden = true;
  lbSeq++; // a late full-size load must not land
  document.documentElement.classList.remove('lbopen');
  document.getElementById('lbimg').removeAttribute('src');
  if (lbReturn && lbReturn.focus) lbReturn.focus();
}
export function enlargePhoto(e) {
  if (cropping || gridEditable()) return;
  if (canEdit && e && e.target.classList && e.target.classList.contains('gc')) return;
  const img = document.getElementById('photo');
  if (img && img.naturalWidth)
    openLightbox(img.src, document.getElementById('photoinfo').textContent);
}
export function enlargeFrame() {
  if (!frames.length) return;
  stopPlay(); // it would keep moving underneath
  const v = document.getElementById('vframe');
  openLightbox(
    v.src,
    document.getElementById('pframe').textContent,
    '/frame/' + encodeURIComponent(frames[fidx])
  );
}

// ---------------- auth / read-only ----------------
export let canEdit = true,
  authEnabled = false;
export function applyAuth(j) {
  authEnabled = !!j.auth_enabled;
  canEdit = !authEnabled || !!j.authed;
  document.body.classList.toggle('readonly', authEnabled && !canEdit);
  const box = document.getElementById('authbox');
  box.style.display = authEnabled ? 'flex' : 'none';
  document.getElementById('pw').style.display = authEnabled && !canEdit ? '' : 'none';
  document.getElementById('loginbtn').style.display = authEnabled && !canEdit ? '' : 'none';
  document.getElementById('rolabel').style.display = authEnabled && !canEdit ? '' : 'none';
  document.getElementById('logoutbtn').style.display = authEnabled && canEdit ? '' : 'none';
}
export async function doLogin() {
  const pw = document.getElementById('pw');
  const err = document.getElementById('loginerr');
  err.textContent = '';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw.value })
    });
    if (r.ok) {
      pw.value = '';
      restartStream();
      refresh();
    } else err.textContent = 'wrong password';
  } catch (e) {
    err.textContent = 'login failed';
  }
}
export async function doLogout() {
  try {
    await fetch('/api/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    });
  } catch (e) {}
  restartStream();
  refresh();
}
export function initAuth() {
  document.getElementById('loginbtn').addEventListener('click', doLogin);
  document.getElementById('logoutbtn').addEventListener('click', doLogout);
  document.getElementById('pw').addEventListener('keydown', e => {
    if (e.key === 'Enter') doLogin();
  });
}

// setters: other modules cannot assign an imported binding
export function set_playerFps(v) {
  playerFps = v;
  return v;
}

// what ran at load time as a plain script; main.js calls it in the old order
export function start() {
  {
    const svg = document.getElementById('guidesvg');
    if (svg) {
      svg.addEventListener('pointerdown', ev => {
        if (!cropping) return;
        ev.preventDefault();
        svg.setPointerCapture(ev.pointerId);
        cropDrag = cropPoint(ev);
        cropSel = null;
        drawCrop();
      });
      svg.addEventListener('pointermove', ev => {
        if (!cropping || !cropDrag) return;
        const p = cropPoint(ev);
        const x = Math.min(p[0], cropDrag[0]),
          y = Math.min(p[1], cropDrag[1]);
        cropSel = [x, y, Math.abs(p[0] - cropDrag[0]), Math.abs(p[1] - cropDrag[1])];
        drawCrop();
      });
      svg.addEventListener('pointerup', () => {
        if (!cropping) return;
        cropDrag = null;
        if (cropSel && (cropSel[2] < 0.05 || cropSel[3] < 0.05)) cropSel = null; // a click, not a box
        drawCrop();
      });
    }
    const cb = document.getElementById('cropbtn');
    if (cb) cb.addEventListener('click', startCrop);
    const cs = document.getElementById('cropsave');
    if (cs)
      cs.addEventListener('click', () => {
        if (!cropSel) {
          const i = document.getElementById('photoinfo');
          if (i) i.textContent = 'Drag a rectangle first, or choose Full frame';
          return;
        }
        saveCrop(cropSel.map(v => v.toFixed(3)).join(','));
      });
    const cf = document.getElementById('cropfull');
    if (cf) cf.addEventListener('click', () => saveCrop(''));
    const cr = document.getElementById('cropreset');
    if (cr) cr.addEventListener('click', () => saveCrop(''));
    const cc = document.getElementById('cropcancel');
    if (cc) cc.addEventListener('click', stopCrop);
  }
  {
    const f = document.getElementById('cfgform');
    if (f) {
      f.addEventListener('input', markDirty);
      f.addEventListener('change', markDirty);
    }
  }
  (function kioskReturn() {
    if (!/[?&]kiosk=1\b/.test(location.search)) return;
    const go = () => {
      location.href = '/screen';
    };
    const b = document.createElement('a');
    b.href = '/screen';
    b.className = 'kioskback';
    b.textContent = '\u2190 Summary';
    document.addEventListener('DOMContentLoaded', () => document.body.appendChild(b));
    let t = setTimeout(go, 180000);
    ['pointerdown', 'touchstart', 'keydown', 'scroll'].forEach(ev =>
      addEventListener(
        ev,
        () => {
          clearTimeout(t);
          t = setTimeout(go, 180000);
        },
        { passive: true }
      )
    );
  })();
  document.getElementById('playbtn').addEventListener('click', togglePlay);
  {
    const rt = document.getElementById('resettlbtn');
    if (rt) rt.addEventListener('click', () => resetTimelapse(false));
  }
  document.getElementById('renderbtn').addEventListener('click', async () => {
    const info = document.getElementById('renderinfo');
    const dl = document.getElementById('dlbtn');
    const btn = document.getElementById('renderbtn');
    dl.style.display = 'none'; // hide download instantly, no race
    btn.disabled = true;
    btn.textContent = '\u23F3 Rendering...';
    renderStart = Date.now();
    clearRenderTimer();
    renderTimer = setInterval(tickRender, 1000);
    info.textContent = 'Starting render...';
    try {
      const r = await fetch('/api/render', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      });
      const j = await r.json();
      if (!r.ok) {
        info.textContent = j.error || 'Render failed to start';
        btn.disabled = false;
        btn.textContent = '\uD83C\uDFA5 Render video';
      }
    } catch (e) {
      info.textContent = 'Render failed to start';
      btn.disabled = false;
      btn.textContent = '\uD83C\uDFA5 Render video';
    }
  });
  document.getElementById('scrub').addEventListener('input', ev => {
    stopPlay();
    fidx = +ev.target.value;
    showFrame();
  });
  document.querySelector('#photocard .imgwrap').addEventListener('click', enlargePhoto);
  document.getElementById('vframe').addEventListener('click', enlargeFrame);
  document.getElementById('lbclose').addEventListener('click', closeLightbox);
  document.getElementById('lightbox').addEventListener('click', e => {
    // the backdrop, not the picture; and not in the first half second, so a
    // habitual double click opens it rather than opening and closing it
    if (e.target.id === 'lightbox' && Date.now() - lbOpened > 500) closeLightbox();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeLightbox();
  });
}
