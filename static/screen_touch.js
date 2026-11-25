// openSeedling: taps on the Pi's own touchscreen.
// Loaded only on pages served to that screen (routes.on_screen).
//
// The panel delivers clean touches (libinput shows them), but between the
// panel, the compositor and the browser a tap can land somewhere else on the
// page: turned, mirrored, even squeezed into a corner. The page corrects taps
// itself:
//
// - Hold a finger anywhere on the screen for 3 seconds to calibrate: tap the
//   four targets that appear. The page fits the straight-line mapping from
//   where those taps arrived to where the targets are and saves it
//   (screen_touch_cal), then reloads with it.
// - With a calibration saved, each tap is moved by it, the browser's own click
//   (which lands in the wrong place) is blocked, and the element under the
//   corrected point is clicked.
// - Without one, a tap the browser does not turn into a click is clicked here.
//
// Each tap shows a brief ring where it is taken to be, and the first taps of a
// page are reported to the controller's journal.
(function () {
  'use strict';
  const MOVE_PX = 14; // a finger that travels further is a drag, not a tap
  const WAIT_MS = 350; // how long a native click may take to follow the tap
  const HOLD_MS = 3000; // a finger held this long starts calibration
  const REPORT_TAPS = 5;
  // The saved calibration is in the page as data: the content policy allows
  // no inline script to set it.
  const saved = (() => {
    if (window.TOUCH_CAL) return window.TOUCH_CAL;
    const el = document.getElementById('touchcal');
    try {
      return el ? JSON.parse(el.textContent) : null;
    } catch (e) {
      return null;
    }
  })();
  const CAL = (saved && saved.m) || null;
  let start = null,
    moved = false,
    clickedAt = 0,
    lastTouch = 0,
    reported = 0,
    holdTimer = null,
    calib = null; // {points: [], targets: [], el}

  const fix = (x, y) =>
    CAL ? [CAL[0] * x + CAL[1] * y + CAL[2], CAL[3] * x + CAL[4] * y + CAL[5]] : [x, y];

  addEventListener(
    'click',
    e => {
      // with a calibration, the browser's own click from a tap is in the wrong
      // place: only the corrected one (made below, not trusted) gets through
      if (CAL && e.isTrusted && Date.now() - lastTouch < 800) {
        e.preventDefault();
        e.stopImmediatePropagation();
        return;
      }
      clickedAt = Date.now();
    },
    true
  );

  function ring(x, y) {
    const r = document.createElement('div');
    r.className = 'tapring';
    r.style.left = x + 'px';
    r.style.top = y + 'px';
    document.body.appendChild(r);
    setTimeout(() => r.remove(), 450);
  }

  function report(x, y, native, target) {
    if (reported >= REPORT_TAPS) return;
    reported++;
    fetch('/api/screen_event', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kind: 'tap',
        x: Math.round(x),
        y: Math.round(y),
        w: innerWidth,
        h: innerHeight,
        native: native,
        target: (CAL ? 'corrected ' : '') + target
      })
    }).catch(() => {});
  }

  function describe(el) {
    if (!el) return '';
    return (
      el.tagName.toLowerCase() +
      (el.id ? '#' + el.id : '') +
      (el.dataset && el.dataset.v ? '[' + el.dataset.v + ']' : '')
    );
  }

  // ---- calibration: four targets, a least-squares fit, saved and reloaded ----
  const TARGETS = [
    [0.12, 0.12],
    [0.88, 0.12],
    [0.88, 0.88],
    [0.12, 0.88]
  ];

  function solve3(m, v) {
    // m: 3x3, v: 3 -> x with m x = v (Gaussian elimination, partial pivoting)
    const a = m.map((row, i) => row.concat([v[i]]));
    for (let c = 0; c < 3; c++) {
      let p = c;
      for (let r = c + 1; r < 3; r++) if (Math.abs(a[r][c]) > Math.abs(a[p][c])) p = r;
      if (Math.abs(a[p][c]) < 1e-9) return null;
      [a[c], a[p]] = [a[p], a[c]];
      for (let r = 0; r < 3; r++) {
        if (r === c) continue;
        const f = a[r][c] / a[c][c];
        for (let k = c; k < 4; k++) a[r][k] -= f * a[c][k];
      }
    }
    return [a[0][3] / a[0][0], a[1][3] / a[1][1], a[2][3] / a[2][2]];
  }

  // the a..f that best carry the raw taps onto the targets
  function fit(raw, want) {
    const n = [
      [0, 0, 0],
      [0, 0, 0],
      [0, 0, 0]
    ];
    const bx = [0, 0, 0],
      by = [0, 0, 0];
    raw.forEach(([x, y], i) => {
      const row = [x, y, 1];
      for (let r = 0; r < 3; r++) {
        for (let c = 0; c < 3; c++) n[r][c] += row[r] * row[c];
        bx[r] += row[r] * want[i][0];
        by[r] += row[r] * want[i][1];
      }
    });
    const px = solve3(n, bx),
      py = solve3(n, by);
    return px && py ? px.concat(py) : null;
  }

  function showTarget() {
    const [fx, fy] = TARGETS[calib.points.length];
    const x = fx * innerWidth,
      y = fy * innerHeight;
    calib.el.innerHTML =
      `<div class="calmsg">Touch calibration: tap the center of the target ` +
      `(${calib.points.length + 1} of 4)</div>` +
      `<div class="caltarget" style="left:${x}px;top:${y}px"></div>`;
  }

  function startCalibration() {
    if (calib) return;
    const el = document.createElement('div');
    el.className = 'calib';
    document.body.appendChild(el);
    calib = { points: [], el, idle: setTimeout(endCalibration, 60000) };
    showTarget();
  }

  function endCalibration(msg) {
    if (!calib) return;
    clearTimeout(calib.idle);
    if (msg) {
      calib.el.innerHTML = `<div class="calmsg">${msg}</div>`;
      const el = calib.el;
      setTimeout(() => el.remove(), 2500);
    } else calib.el.remove();
    calib = null;
  }

  function calibrationTap(x, y) {
    calib.points.push([x, y]);
    if (calib.points.length < TARGETS.length) return showTarget();
    const want = TARGETS.map(([fx, fy]) => [fx * innerWidth, fy * innerHeight]);
    const m = fit(calib.points, want);
    const err = m
      ? Math.max(
          ...calib.points.map(([rx, ry], i) =>
            Math.hypot(
              m[0] * rx + m[1] * ry + m[2] - want[i][0],
              m[3] * rx + m[4] * ry + m[5] - want[i][1]
            )
          )
        )
      : Infinity;
    // four taps that no single straight-line mapping fits are not saved:
    // start again
    if (!(err < 0.06 * Math.min(innerWidth, innerHeight))) {
      calib.points = [];
      calib.el.innerHTML =
        '<div class="calmsg">Those taps did not line up. Again, slowly, the center of each target.</div>';
      setTimeout(() => calib && showTarget(), 1800);
      return;
    }
    calib.el.innerHTML = '<div class="calmsg">Saving&hellip;</div>';
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ screen_touch_cal: { m, at: Math.floor(Date.now() / 1000) } })
    })
      .then(r => r.json())
      .then(j => {
        if (j && j.ok === false)
          throw new Error((j.errors && JSON.stringify(j.errors)) || 'refused');
        calib.el.innerHTML = '<div class="calmsg">Touch calibrated.</div>';
        setTimeout(() => location.reload(), 900);
      })
      .catch(e => endCalibration('Could not save the calibration (' + e.message + ').'));
  }

  // ---- taps ----
  addEventListener(
    'touchstart',
    e => {
      const t = e.changedTouches[0];
      start = { x: t.clientX, y: t.clientY, at: Date.now() };
      moved = false;
      lastTouch = Date.now();
      clearTimeout(holdTimer);
      holdTimer = setTimeout(() => {
        if (start && !moved) startCalibration();
      }, HOLD_MS);
    },
    { passive: true }
  );
  addEventListener(
    'touchmove',
    e => {
      const t = e.changedTouches[0];
      if (start && Math.hypot(t.clientX - start.x, t.clientY - start.y) > MOVE_PX) moved = true;
    },
    { passive: true }
  );
  addEventListener(
    'touchend',
    e => {
      clearTimeout(holdTimer);
      lastTouch = Date.now();
      if (!start) return;
      const t = e.changedTouches[0];
      const rx = t.clientX,
        ry = t.clientY,
        began = start.at,
        held = Date.now() - began >= HOLD_MS;
      const wasMove = moved;
      start = null;
      if (calib) {
        e.preventDefault();
        // the hold that opened it is not a target tap
        if (!held && !wasMove) calibrationTap(rx, ry);
        return;
      }
      if (wasMove || held) return;
      const [x, y] = fix(rx, ry);
      ring(x, y);
      const el = document.elementFromPoint(x, y);
      if (CAL) {
        e.preventDefault();
        report(x, y, false, describe(el));
        if (!el) return;
        if (el.matches('input, select, textarea')) el.focus();
        el.click();
        return;
      }
      setTimeout(() => {
        const native = clickedAt >= began;
        report(x, y, native, describe(el));
        if (native || !el) return;
        // no click from the browser: give the tap one
        if (el.matches('input, select, textarea')) el.focus();
        el.click();
      }, WAIT_MS);
    },
    { passive: false }
  );
})();
