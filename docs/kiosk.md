# Touchscreen kiosk

An optional touchscreen on the Pi's own HDMI port.

A small HDMI touchscreen on the stand can show the full dashboard without a
second computer:

    bash scripts/kiosk.sh install --rotate 90     # portrait; 0, 90, 180 or 270
    bash scripts/kiosk.sh touch-rotate 90         # only if taps land in the wrong place
                                                  # (0/90/180/270, or 90-flip etc. if mirrored)
    bash scripts/kiosk.sh status                  # running? memory used?
    bash scripts/kiosk.sh remove                  # back to headless

It opens `/screen`, a one-page summary built for the 600 x 1024 portrait panel
with no scrolling: an attention line, soil and air, the heat mat (Auto / On /
Off, power), each setup's light (Auto / On / Off, today's light against its
target) with its trays' moisture, the fan, the reservoir, and the soil
temperature over the heat mat's power for the last 24 hours. "Full dashboard"
opens the normal page with a button back, and it returns to the summary by
itself after three minutes untouched. The page fits whatever the panel's
fonts, the number of setups and trays, or the length of the attention line:
the soil chart takes the leftover height, the page tightens in steps when
that is too little, and the chart is dropped only as a last resort.

It runs cog (a WebKit browser) inside cage (a one-window Wayland compositor),
no desktop, first in line for the out-of-memory killer, so the screen may
blank and restart under pressure but cannot take the controller down. The
unit's `MemoryMax=210M` does not currently reach the browser: the PAM login
session moves cage out of the service's cgroup.

What the Zero 2 W needs, all set by the unit and `deploy/boot-config.txt`:

- `cma-128`: the graphics driver allocates screen and render buffers from
  contiguous memory; at 64 MB it ran out ("Failed to allocate from GEM DMA
  helper" in `journalctl -k`) and the page showed cog's crash screen.
- `WEBKIT_SKIA_ENABLE_CPU_RENDERING=1`: pages are drawn on the CPU, leaving the
  GPU only the screen buffers.
- `WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1`: WebKit's bubblewrap sandbox
  refuses to start in this session ("bwrap: Unexpected capabilities but not
  setuid") and the browser aborts with status 133. The screen only loads this
  Pi's own dashboard from 127.0.0.1.
- An invisible cursor theme (`scripts/blank_cursor.py`, written to
  `~/.local/share/openseedling-cursor` and named by `XCURSOR_PATH`), plus
  `cursor: none` on pages served to the screen, because the touch panel also
  reports itself as a mouse.
- The browser's output goes to the journal: `journalctl -u growlight-kiosk`. The screen counts as signed in
(`GROWLIGHT_TRUST_LOCALHOST=1` in `.env`, set by the script). With that on,
the controller opens a second port for the screen, 5001 on 127.0.0.1 only
(`GROWLIGHT_SCREEN_PORT`), and a request is signed in because it arrived
there. The main port, 5000, always needs the password, so a reverse proxy
running on the Pi is safe as long as it points at 5000. Never proxy to 5001. On a Zero the touch panel's USB needs a hub on the single port the
camera uses. Check any hub before relying on it:

    bash scripts/usbcheck.sh

It passes only if the camera links at 480 Mbit/s, still offers the size the
app captures at, a touchscreen is seen, and there was no undervoltage. A
camera on a slower link also raises the "Camera on a slow USB link" alert and
shows on the touchscreen's attention line.

## External display settings

Settings, External display sets up the touchscreen from the dashboard:

* **Rotation** and **Display mode**: `scripts/display_watch.py` runs inside the
  kiosk session (started by `kiosk-session.sh`, it ends with the browser) and
  applies them with `wlr-randr` within a few seconds. It also writes what the
  display reports (its modes, the one in use, the rotation) to
  `display_state.json`, which the section shows. "As installed" keeps the
  `kiosk.sh install --rotate` angle; a blank mode leaves the display's own
  choice. A panel that is fed a larger mode than its own scales it down, which
  costs the Pi memory and drawing time for nothing.
* **Touch calibration**: hold a finger on the touchscreen for 3 seconds and
  tap the four targets. `static/screen_touch.js` fits the straight-line
  mapping from where the taps arrived to the targets, saves it
  (`screen_touch_cal`) and corrects every later tap with it, blocking the
  browser's own misplaced click. It fixes any turn, mirror or squeeze between
  the panel and the page; `kiosk.sh touch-rotate` is then not needed.
* **Summary screen**: how often the charts turn, which charts are shown, how
  long the full dashboard stays up untouched before returning to the summary,
  while every light is off either a dark page or the display switched off
  (the HDMI output off, so the panel's backlight goes dark; a touch, read
  from the panel directly, wakes it for a minute), and
  whether Little Buddy (on under System) also walks across the summary's tiles
  (`static/screen_buddy.js` loads the dashboard's `buddy.js`; styles in
  `buddy.css`).
