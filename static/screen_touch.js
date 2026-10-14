// openSeedling: taps on the Pi's own touchscreen.
// Loaded only on pages served to that screen (routes.on_screen). The panel
// delivers clean touches (libinput shows them), but a tap is only useful if the
// browser turns it into a click, and WPE does not always. Each tap is shown as
// a brief ring where it landed, so a tap that lands in the wrong place is
// visible; when no click follows the tap within a moment, the element under
// the finger is clicked here. The first taps of each page are also reported to
// the controller's journal (where they landed, and whether the browser made a
// click itself), which is how a tap problem is told apart from a screen one.
(function () {
  'use strict';
  const MOVE_PX = 14; // a finger that travels further is a drag, not a tap
  const WAIT_MS = 350; // how long a native click may take to follow the tap
  const REPORT_TAPS = 5;
  let start = null,
    moved = false,
    clickedAt = 0,
    reported = 0;

  addEventListener('click', () => (clickedAt = Date.now()), true);

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
        target: target
      })
    }).catch(() => {});
  }

  addEventListener(
    'touchstart',
    e => {
      const t = e.changedTouches[0];
      start = { x: t.clientX, y: t.clientY, at: Date.now() };
      moved = false;
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
      if (!start || moved) return (start = null);
      const t = e.changedTouches[0];
      const x = t.clientX,
        y = t.clientY,
        began = start.at;
      start = null;
      ring(x, y);
      const el = document.elementFromPoint(x, y);
      const name = el
        ? el.tagName.toLowerCase() +
          (el.id ? '#' + el.id : '') +
          (el.dataset.v ? '[' + el.dataset.v + ']' : '')
        : '';
      setTimeout(() => {
        const native = clickedAt >= began;
        report(x, y, native, name);
        if (native || !el) return;
        // no click from the browser: give the tap one
        if (el.matches('input, select, textarea')) el.focus();
        el.click();
      }, WAIT_MS);
    },
    { passive: true }
  );
})();
