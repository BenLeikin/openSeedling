// openSeedling dashboard: The smart plug, backup, theme, thunderstorm, light calibration and the second light.
// An ES module: what it uses from the others is imported at the top, and what
// it offers is exported. Another module cannot assign one of its variables
// directly; it calls the set_<name>() exported at the bottom. Code that runs
// at page load is in start(), which main.js calls in a fixed order.
// ---- smart plug setup ----
// Scan, pick, test. The password field is never populated from the server (it
// is redacted like the dashboard hash), so a blank one means "leave it alone"
// rather than "clear it".
import { ctlTarget } from './setups.js';
import { canEdit, formDirty, pendingSave } from './photos.js';
import {
  calibrateProbe,
  chartLeave,
  chartMove,
  chartPlots,
  checkTempComp,
  cssId,
  drawMini,
  expandedCharts,
  layoutChartRows,
  loadChart,
  seriesData,
  set_chartHours
} from './charts.js';
import { esc, loadPlantings } from './trays.js';
import { refresh } from './grid.js';

export async function plugScan() {
  const info = document.getElementById('pluginfo');
  const list = document.getElementById('pluglist');
  const f = document.getElementById('cfgform');
  if (info) info.textContent = 'scanning the local network\u2026';
  if (list) {
    list.hidden = true;
    list.innerHTML = '';
  }
  try {
    const r = await fetch('/api/plug_discover', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user: f.elements['kasa_user'].value,
        pass: f.elements['kasa_pass'].value
      })
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      info.textContent = 'log in first';
      return;
    }
    if (!j.ok) {
      info.textContent = j.error || 'scan failed';
      return;
    }
    const devs = j.devices || [];
    if (!devs.length) {
      info.textContent = 'no plugs found on this subnet';
      return;
    }
    info.textContent = `${devs.length} found \u00b7 pick one to use it`;
    list.innerHTML = devs
      .map(d => {
        const why = d.needs_auth
          ? '<span class="plugauth">needs your TP-Link login</span>'
          : d.error
            ? `<span class="plugauth">${esc(d.error)}</span>`
            : `<span class="plugstate">${d.on ? 'on' : 'off'}</span>`;
        return `<button type="button" class="plugpick" data-host="${esc(d.host)}">
                <b>${esc(d.alias || d.model || 'plug')}</b>
                <span class="plughost">${esc(d.host)}</span>${why}</button>`;
      })
      .join('');
    list.hidden = false;
  } catch (e) {
    if (info) info.textContent = 'scan failed';
  }
}

export async function plugTest() {
  const info = document.getElementById('pluginfo');
  const f = document.getElementById('cfgform');
  if (info) info.textContent = 'connecting\u2026';
  try {
    const r = await fetch('/api/plug_test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        host: f.elements['kasa_host'].value,
        user: f.elements['kasa_user'].value,
        pass: f.elements['kasa_pass'].value
      })
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      info.textContent = 'log in first';
      return;
    }
    if (!j.ok) {
      info.textContent = (j.error || 'could not connect') + (j.hint ? ' \u2014 ' + j.hint : '');
      info.className = 'fhint plugbad';
      return;
    }
    info.className = 'fhint plugok';
    info.textContent =
      `${j.alias || j.model || 'plug'} responded \u00b7 currently ` +
      `${j.on ? 'on' : 'off'} \u00b7 remember to save`;
  } catch (e) {
    if (info) info.textContent = 'request failed';
  }
}

export function initPlug() {
  const scan = document.getElementById('plugscan');
  const test = document.getElementById('plugtest');
  const list = document.getElementById('pluglist');
  if (scan) scan.addEventListener('click', plugScan);
  if (test) test.addEventListener('click', plugTest);
  if (list)
    list.addEventListener('click', ev => {
      const b = ev.target.closest('.plugpick');
      if (!b) return;
      const f = document.getElementById('cfgform');
      f.elements['kasa_host'].value = b.dataset.host;
      formDirty.add('kasa_host'); // set by code: no input event
      list.hidden = true;
      const info = document.getElementById('pluginfo');
      if (info) info.textContent = `${b.dataset.host} selected \u00b7 test it, then save`;
    });
}

export function initBackup() {
  const box = document.getElementById('backupsecrets');
  const btn = document.getElementById('backupbtn');
  const info = document.getElementById('backupinfo');
  if (!btn) return;
  const sync = () => {
    btn.href = '/api/backup' + (box && box.checked ? '?secrets=1' : '');
    if (info)
      info.textContent =
        box && box.checked
          ? 'database, settings and .env \u2014 keep this file private'
          : 'database, settings and planting history';
  };
  if (box) box.addEventListener('change', sync);
  sync();
}

export function initPlantings() {
  const t = document.getElementById('histtoggle'),
    b = document.getElementById('histbody');
  if (t && b)
    t.addEventListener('click', () => {
      const open = b.hasAttribute('hidden');
      if (open) b.removeAttribute('hidden');
      else b.setAttribute('hidden', '');
      t.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  if (b)
    b.addEventListener('click', async ev => {
      const btn = ev.target.closest('.hrestore');
      if (!btn) return;
      btn.blur();
      try {
        const r = await fetch('/api/planting_restore', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: Number(btn.dataset.id) })
        });
        const j = await r.json().catch(() => ({}));
        if (!j.ok) {
          alert(j.error || 'Could not restore that.');
          return;
        }
      } catch (e) {
        alert('Request failed.');
        return;
      }
      const wrapEl = document.getElementById('trayswrap');
      if (wrapEl) wrapEl.dataset.sig = ''; // the restored cell must reappear now
      refresh();
      loadPlantings();
    });
  loadPlantings();
}

// Empty a cell in place. Used for the instant feedback after Transplanted or
// Died: the authoritative redraw follows from the next status, but the click
// should not look ignored while that round trip happens.
export function clearCellUI(cell) {
  cell.querySelectorAll('input,textarea').forEach(el => {
    el.value = '';
    el.setAttribute('value', ''); // keep the attribute in step with the
  }); // property, so the markup stays honest
  cell.dataset.sprouted = '';
  cell.dataset.archived = '';
  cell.dataset.seed = '';
  cell.classList.remove('sprouted', 'archived', 'filled');
  const age = cell.querySelector('.tage');
  if (age) age.textContent = '';
  const st = cell.querySelector('.tstat');
  if (st) st.textContent = '';
}

// Theme: "auto" leaves it to the device's own preference, which the stylesheet
// handles through a media query; light and dark force it with an attribute.
// The browser chrome colour is kept in step so the phone address bar matches.
// The header button names the mode it will switch you to, which is how a
// two-state toggle stays unambiguous: "Dark Mode" means pressing it gives you
// dark. "auto" resolves to whatever the device is currently doing, so the
// first press always lands on the opposite of what you can see.
export let themeMode = 'auto';
export function isDarkNow(mode) {
  return (
    mode === 'dark' ||
    (mode !== 'light' &&
      window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: dark)').matches)
  );
}
export function labelTheme() {
  const btn = document.getElementById('themebtn');
  if (btn) btn.textContent = isDarkNow(themeMode) ? 'Light Mode' : 'Dark Mode';
}
export async function toggleTheme() {
  const next = isDarkNow(themeMode) ? 'light' : 'dark';
  themeMode = next;
  applyTheme(next); // instant: never wait on the round trip
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: next })
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401 || !j.ok) return; // read-only: it still applies for this visit
    pendingSave.theme = next; // hold it until a status echoes it back
  } catch (e) {}
}
// ---- thunderstorm ----
// A button in the Light settings, shown only on the wiring that can do it:
// an AC fixture on a 0-10V dim line. The 5V panel has too little range and
// the smart plug cannot dim at all, so on those the server refuses and the
// button stays hidden rather than offering something that will not work.
export let stormBusy = false;
export async function summonStorm() {
  if (stormBusy) return;
  const btn = document.getElementById('stormbtn');
  const info = document.getElementById('storminfo');
  stormBusy = true;
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/lightning', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ seconds: 20, style: 'storm' })
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      if (info) info.textContent = 'log in first';
      stormBusy = false;
      if (btn) btn.disabled = false;
      return;
    }
    if (!j.ok) {
      if (info) info.textContent = j.error || 'could not start';
      stormBusy = false;
      if (btn) btn.disabled = false;
      return;
    }
    const secs = j.seconds || 20;
    let left = secs;
    if (info) info.textContent = `storm running \u2014 ${left}s`;
    const tick = setInterval(() => {
      left -= 1;
      if (info) info.textContent = `storm running \u2014 ${left}s`;
      if (left <= 0) {
        clearInterval(tick);
        stormBusy = false;
        if (btn) btn.disabled = false;
        if (info) info.textContent = 'done, back to the schedule';
      }
    }, 1000);
  } catch (e) {
    if (info) info.textContent = 'request failed';
    stormBusy = false;
    if (btn) btn.disabled = false;
  }
}
export function renderStorm(j) {
  const row = document.getElementById('stormrow');
  if (!row) return;
  // the server decides: it knows the wiring and whether the channel opened
  row.style.display = j.lightning && j.lightning.available && canEdit ? '' : 'none';
}
// ---- light calibration ----
// A fine raw sweep (every percent) and an inverse lookup, so the dashboard's
// percent means a fraction of the fixture's real output. The sweep's own
// progress is shown by the existing sweep card; this just starts it and
// reports what the calibration found.
export async function startCalibration() {
  const info = document.getElementById('lininfo');
  const btn = document.getElementById('linbtn');
  try {
    const r = await fetch('/api/light_sweep', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ linearize: true, light: ctlTarget })
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      if (info) info.textContent = 'log in first';
      return;
    }
    if (!j.ok) {
      if (info) info.textContent = j.error || 'could not start';
      return;
    }
    if (btn) btn.disabled = true;
    if (info)
      info.textContent =
        `calibrating, about ${Math.round((j.estimate_seconds || 180) / 60)} minutes. ` +
        'The light will step through its whole range.';
  } catch (e) {
    if (info) info.textContent = 'request failed';
  }
}
export function renderCalibration(j) {
  const info = document.getElementById('lininfo');
  const btn = document.getElementById('linbtn');
  const running = !!(j.sweep && j.sweep.running);
  if (btn) btn.disabled = running;
  const lin = j.light_linear;
  if (!info || running) return;
  if (j.light_linear_stale) {
    info.textContent =
      'The stored calibration was built by an older version and ' +
      'is not being used. Press Calibrate to rebuild it.';
    return;
  }
  if (lin && lin.table === undefined && lin.cutoff_raw !== undefined) {
    const when = lin.ts ? new Date(lin.ts * 1000).toLocaleDateString() : '';
    const floorNote =
      lin.min_output_pct > 2
        ? ` The driver cannot hold less than about ${Math.round(lin.min_output_pct)}% ` +
          `of full, so settings below that hold that lowest level, or cycle on ` +
          `and off to average down if you switch that on.`
        : '';
    info.textContent =
      `Calibrated ${when}: light appears at ${lin.cutoff_raw}% ` +
      `and reaches full output by ${lin.saturation_raw}% raw, ` +
      `${Math.round(lin.peak_lux).toLocaleString()} lx peak.${floorNote} ` +
      (j.light_linear_on ? 'In use.' : 'Not in use; tick the box to apply it.');
  }
}

// ---- second light ----
export function renderLight2(j) {
  const el = document.getElementById('l2line');
  if (!el) return;
  const l = j.light2;
  if (!l || !l.enabled || el.dataset.hide) {
    el.style.display = 'none';
    return;
  }
  el.style.display = '';
  const name =
    l.fixture === 'pwm' ? '5V panel' : l.fixture === 'dim' ? 'dim fixture' : 'second light';
  if (!l.fixture) {
    el.textContent = `Second light: ${l.why}`;
    el.className = 'l2line l2bad';
    return;
  }
  el.className = 'l2line';
  const sched = l.override === 'auto' ? ` \u00b7 ${l.start}\u2013${l.end}` : '';
  el.textContent = `Second light (${name}): ${Math.round(l.level)}% \u00b7 ${l.why}${sched}`;
}

export function initStorm() {
  const btn = document.getElementById('stormbtn');
  if (btn) btn.addEventListener('click', summonStorm);
  const lb = document.getElementById('linbtn');
  if (lb) lb.addEventListener('click', startCalibration);
}

export function initTheme() {
  const btn = document.getElementById('themebtn');
  if (btn) btn.addEventListener('click', toggleTheme);
  labelTheme();
}

export function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === 'light' || mode === 'dark') root.setAttribute('data-theme', mode);
  else root.removeAttribute('data-theme');
  const dark =
    mode === 'dark' ||
    (mode !== 'light' &&
      window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: dark)').matches);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', dark ? '#141a15' : '#f0f6ea');
  themeMode = mode;
  labelTheme();
}

export function initSensors() {
  const pmap = {
    probewet1: ['1', 'wet'],
    probedry1: ['1', 'dry'],
    probewet2: ['2', 'wet'],
    probedry2: ['2', 'dry']
  };
  for (const id in pmap) {
    const b = document.getElementById(id);
    if (b) b.addEventListener('click', () => calibrateProbe(pmap[id][0], pmap[id][1]));
  }
  {
    const info = document.getElementById('probecalinfo');
    if (info)
      info.addEventListener('click', ev => {
        const b = ev.target.closest('.calforce');
        if (b)
          calibrateProbe(
            b.dataset.tray,
            b.dataset.point,
            true,
            b.dataset.volts != null ? parseFloat(b.dataset.volts) : null
          );
      });
  }
  {
    const t1 = document.getElementById('tc1');
    if (t1) t1.addEventListener('click', () => checkTempComp('1'));
    const t2 = document.getElementById('tc2');
    if (t2) t2.addEventListener('click', () => checkTempComp('2'));
  }
  const hc = document.getElementById('chartgrid');
  if (hc) {
    hc.addEventListener('mousemove', chartMove);
    hc.addEventListener('mouseleave', chartLeave);
    hc.addEventListener('pointerdown', chartMove); // tap/click reads a point
    {
      let rt = null;
      window.addEventListener('resize', () => {
        // viewBox follows the box
        clearTimeout(rt);
        rt = setTimeout(() => {
          for (const k in chartPlots) drawMini(k);
        }, 150);
      });
    }
    hc.addEventListener('click', ev => {
      const b = ev.target.closest('.cexpand');
      if (!b) return;
      const card = document.getElementById('cc-' + b.dataset.key);
      if (!card) return;
      const nowBig = card.classList.toggle('expanded');
      layoutChartRows(); // an expanded chart takes its own row
      if (nowBig) expandedCharts.add(card.id);
      else expandedCharts.delete(card.id);
      b.textContent = nowBig ? '\u2921' : '\u2922';
      b.title = nowBig ? 'Shrink this chart' : 'Expand this chart';
      const key = Object.keys(seriesData).find(k => cssId(k) === b.dataset.key);
      if (key) drawMini(key); // redraw at the new size
    });
  }
  loadChart(); // initial draw; range buttons reload
  document.querySelectorAll('#ranges button').forEach(b => {
    b.addEventListener('click', () => {
      set_chartHours(+b.dataset.h);
      document.querySelectorAll('#ranges button').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      loadChart();
    });
  });
}
