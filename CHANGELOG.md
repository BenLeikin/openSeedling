# Changelog

One entry per update zip, newest first. Install any update with
`bash scripts/update.sh openSeedling-update-N.zip`; each is cumulative from
the 4-17 baseline. The installer commits each one with the heading below.

## Unreleased

### Fixed
- A light schedule that crosses midnight (fixed 20:00 to 06:00, or a duration ending in the small hours) now runs through the night; it used to stop at 00:00.
- A fill cut short by the daily pump limit is no longer reported as a failed fill, and no longer disarms auto-watering for that tray.
- A tray that cannot be watered (daily limit, reservoir empty) no longer keeps auto-watering from considering the next tray.
- The pump buttons report a refused run instead of "started".
- The focus sweep photographs each focus value it tests; before, every frame was taken at the configured focus.
- An image job killed for memory reports that, instead of failing with a TypeError.
- Flattened frames from a camera wider than 4000 px keep their proportions.
- A status request during a two-camera capture no longer fails, and half frames from an interrupted capture are no longer listed as photos. Leftover capture scratch files are cleared at start.
- Warnings reach Discord in the warning colour.
- With the light off, the control loop no longer pushes a status to every open dashboard on each pass.
- A dashboard polling during a restart shows "Controller starting" instead of empty readings.
- `update.sh` and `deploy.sh` find tracebacks in a long log and check the port set in `.env`.
- A pump run is put on record before the pump starts, so a restart part way through still counts it against the daily limit and the cooldown.
- Failed fills are kept per tray: a good fill on one tray no longer clears another tray's alert.
- A fill on a tray with no float is refused even when forced; it used to run for a moment and report a float fault.
- A light calibration sweep whose result cannot be saved hands the light back to the schedule instead of holding it until a restart.
- A daily report that fails is not run again every minute for the rest of the day.
- Alerts go to ntfy as well as Discord, and one that could not be sent is sent on the next pass.
- A light sensor reading zero in the dark is no longer reported as stuck.
- Settings in a hand-edited `config.json` are checked at start-up; an invalid one falls back to its default with an error in the log. NaN and infinity are refused wherever a number is expected.
- `setup.sh` runs without a terminal where sudo needs no password, and a change to `requirements-dev.txt` no longer triggers it on the Pi.

### Changed
- One thing at a time has the light: the schedule, a photo, a focus sweep, the camera alignment, a calibration sweep or the lightning effect. A scheduled photo now waits for a sweep or the lightning effect to finish instead of changing the light under it, a sweep or effect will not start during a photo, and a holder that hangs loses the light when its time limit runs out. A photo gives the light back as soon as the frame is on disk, before it is rotated and thumbnailed.
- The modules no longer import each other in loops. The control loop has its own module (`control.py`), with `events.py`, `readings.py`, `units.py` and `auth.py` split out of the modules that shared them, and the four route modules are Flask blueprints. The lint rule for import order is back on.

- Alerts remind once a day while a problem lasts, instead of every six hours (Settings, Alerts; an install keeps the interval it has saved). Alert state survives a restart, so restarting the service no longer announces every standing problem again.
- The touchscreen is signed in because it connects to its own port, 5001 on 127.0.0.1 (`GROWLIGHT_SCREEN_PORT`), not because its requests come from the Pi. After updating, restart the kiosk: `sudo systemctl restart growlight-kiosk`.

### Security
- Request bodies over 1 MB are refused.
- Light-plan advice is escaped before it is shown (it can contain a setup's name).
- The Discord webhook URL and ntfy topic are kept out of the log when a send fails.
- With the touchscreen kiosk on, every request from the Pi itself was signed in, including ones forwarded by a reverse proxy running on the Pi: the web server drops the header the check relied on. Only the screen's own port is trusted now; the main port always needs the password.

### Added
- `scripts/deploy.sh`: push `main` and update the Pi from a development checkout, with a health check and rollback.
- `.gitlab-ci.yml`: lint, format checks and the test suite on every push. `requirements-dev.txt` and a committed `package-lock.json` for the development tools.

## 63 (2026-10-14): Display off at night
- Settings, External display, When the lights are off: a third choice, Turn the display off. The HDMI output goes off while every light is off (the panel's backlight goes dark with it) and comes back when a light comes on; a touch wakes it for a minute (the watcher reads the touch panel itself, since the compositor has nowhere to send a tap on a dark screen).

## 62 (2026-10-14): Touch calibration on the screen
- Hold a finger anywhere on the touchscreen for 3 seconds, then tap the four targets: the page works out how taps arrive (turned, mirrored, even squeezed into a corner) and corrects every tap from then on, on the summary and the full dashboard. Settings, External display shows when it was done and can reset it.
- The status-sharing check in the test suite allows for a background status announcement landing mid-run.

## 61 (2026-10-14): Mirrored touch
- `kiosk.sh touch-rotate` also takes 0-flip, 90-flip, 180-flip and 270-flip, for a touch layer that is mirrored relative to its picture (taps on the right landing on the left). It restarts the kiosk without waiting on its slow stop, and prints the calibration libinput now reports.

## 60 (2026-10-14): Taps on the touchscreen
- The panel delivers clean touches (libinput shows them), so taps are now handled on the page itself: each tap shows a brief ring where it landed, and when the browser does not turn a tap into a click, the button under the finger is clicked anyway (screen_touch.js, only on pages served to the Pi's own screen).
- The first taps of each page go to the controller's journal: where they landed, on what, and whether the browser made a click of it.

## 59 (2026-10-14): Little Buddy on the touchscreen
- Little Buddy crosses the touchscreen summary's tiles too, the same characters and walk as the dashboard, sized with the summary's text. It follows the Little Buddy setting under System, can be kept off the screen in Settings, External display, and stays away while the night screen is up.
- Its styles moved from style.css to buddy.css, shared by both pages.

## 58 (2026-10-13): External display settings
- Settings, External display: the touchscreen's rotation and display mode, applied on the screen within a few seconds (scripts/display_watch.py, started with the kiosk); what the display reports, its modes included, is shown there.
- Also there: how often the summary's charts turn and which are shown, how long the full dashboard stays up before returning to the summary, and a dark screen while the lights are off (tap to wake).

## 57 (2026-10-12): Light sensor reads past 27,000 lx
- The BH1750 is set to its widest range (1 lx mode, shortest measurement time): up to 121,557 lx, from 27,306. The fixture passed the old maximum at a fifth of its power, so the light response sweep took that for full output and the calibration drove the fixture at 19% when the dashboard said 100%.
- A sweep whose readings stop at the sensor's maximum is refused instead of saved.
- A calibration on file that the sensor capped is flagged in Settings with what to do; it stays in use until rebuilt, since dropping it would take the light straight to full power.

## 56 (2026-10-12): Camera stitching: cleaner seam, much less memory
- The seam between the cameras follows the path of least difference through the overlap (around leaves rather than through them), so a leaf standing at a different place in each view is not shown twice; it is softened over about 20 px.
- Stitching scales each frame as it is decoded (decoding at half size when the output is half size or less), places camera 1 before reading camera 2, and blends a few rows at a time: about 108 MB at 3264x2448 per camera, from 168 (Python and OpenCV alone are about 50).
- Align cameras matches on frames decoded straight to 640 px: about 130 MB, from about 340, with as many agreeing points on the test scenes.
- The stitched photo is at most 3072 px wide.

## 55 (2026-10-12): Camera alignment tolerates plant height
- Align cameras fits a shift, turn and scale with a tolerance of 2% of the frame, instead of a tight flat-plane fit that rejected most true matches on leaves standing at different heights (15 of 85 agreed on the rig). With 40 or more in agreement it is refined to a perspective fit.
- The fit must keep camera 2 within 15 degrees and 0.8 to 1.25x of camera 1 (each is already turned the right way up), which is what lets it accept 12 agreeing points instead of 25.

## 54 (2026-10-11): Rotation per camera
- Settings, Camera: Rotate camera 2, beside Rotate photo (now camera 1's). Each camera's frames are turned the right way up before they are matched and stitched, so the two can be mounted different ways round. Changing either rotation calls for a new alignment.

## 53 (2026-10-11): See what the camera alignment matched
- Every Align cameras draws the two frames side by side with what matched: green lines agree on one fit, red ones were rejected, yellow dots are the detail found in each frame. It shows in Settings, Camera, under the button, whether the alignment worked or not; tap it for full size.

## 52 (2026-10-11): Camera alignment finds more matches, shows its frames
- Align cameras matches with SIFT (ORB only where OpenCV lacks it) and a looser ratio test: about twice the matches in a thin overlap of soil and leaves.
- The two frames it used stay viewable from links beside the button, so a failed match can be judged by eye.

## 51 (2026-10-11): Align cameras button fixed
- Settings' Align cameras button shared its id with the camera card's Align button, so it did nothing and the camera card's button ran the camera alignment instead. Both work now; a check fails on any repeated id on the dashboard.
- Aligning runs in the background and the page polls for the outcome, so a reverse proxy's request timeout cannot cut it off. The outcome (or the error) stays shown beside the button.

## 50 (2026-10-11): Two cameras stitched into one view
- Settings, Camera: a second camera (`usb_device2`), a picker listing USB cameras by port, and Align cameras.
- Align cameras matches the two views once and saves the fit (`cam_stitch`); every photo and preview is then both cameras stitched into one wide photo, used for the grid, crop, canopy, timelapse and AI report.
- Cameras can be set by USB port (`/dev/v4l/by-path/...`). Status, the slow-link alert, the touchscreen and `usbcheck.sh` cover camera 2.
- The timelapse video keeps one frame size when a run changes shape (letterboxed).

## 49 (2026-10-11): Touchscreen charts rotate
- The chart slot turns every 15 s through soil temperature with the heat mat, soil moisture per tray (with the dry line), light (PPFD when the light is calibrated, otherwise thousand lux) and air temperature with humidity. A tap turns it and holds that chart for a minute; dots beside the title show which one is up.
- Charts with no data in the last day are skipped.
- `/api/series_all` takes `only=` (key prefixes), so the touchscreen fetches just what it charts.

## 48 (2026-10-11): Touchscreen soil chart labelled
- The soil chart has degree labels on the left, clock times along the bottom, the heat mat target labelled on its line, and a dot at the latest reading.
- The heat mat's power moves out from under the temperature line into its own strip below it (full strip = 100%).
- A key beside the title: soil, heat mat target, heat mat power.

## 47 (2026-10-11): Touchscreen summary fills the screen
- The summary is sized in rem; on a screen bigger than its 600 px layout, the text grows until the soil chart is down to about a quarter of the height, instead of the chart taking all the spare room.
- The kiosk session logs the display's modes, with the one in use marked, to the journal.

## 46 (2026-10-11): Touchscreen summary always fits
- The soil chart takes the height left over instead of a fixed minimum that pushed it off the bottom.
- When that is too little to read, the page tightens in steps (smaller spacing and numbers, then fewer detail lines and a two-line attention line), and only as a last resort drops the chart.

## 45 (2026-10-11): Sensors per tray
- Settings, Setups: each tray picks its moisture probe and float, and any other sensor placed in it (`tray_sensors`).
- Auto-watering, fills, post-fill checks and wet recalibration read the tray's assigned probe and float; the pump stays the tray's own.
- Readings, probe names, dry alerts and the AI report follow the tray a sensor sits in. Unassigned trays keep their number's probe and float.

## 44 (2026-10-11): Touchscreen working, no cursor
- The kiosk unit carries what bring-up on the real screen needed: browser output to the journal, WebKit's sandbox off, CPU rendering; `kiosk.sh install` removes the hand-made drop-ins.
- CMA raised to 128 MB in `deploy/boot-config.txt` (the GPU ran out at 64 MB).
- No mouse pointer on the touchscreen: an invisible cursor theme for cage and cog, and `cursor: none` on pages served to the Pi's own screen.

## 43 (2026-10-06): Memory charts in the Device card
- The Pi's memory charts (free memory, swap, controller, touchscreen) moved from the chart grid into the Device card, under the health tiles; same range buttons, crosshair and expand.

## 42 (2026-10-06): Little Buddy redrawn
- Colors: each sprite's own leg color now wins over the shared leaf green (Avey's legs, the snail's eye stalks, the raindrop's and the gnome's legs had been green); faces keep dark ink on the dark theme.
- Anatomy: a tapered chile pod with a calyx cap and curved stem; Avey on four legs with a rounded body and an S tail; a snail with its foot, feelers and a spiral inside the shell; a seven-spot ladybug with a pronotum; a bee striped within its body, with a stinger; a gnome with a tall pointed hat and a full beard.
- Cuteness: eye highlights, cheeks and small smiles on the cat, bee and snail.

## 41 (2026-10-06): Confirm before removing
- Removing a setup or a tray asks first. A tray with plants says how many planted cells it would discard, in one question.
- The Trays list's "filled" count ignores cleared cells.
- The smart plug's uses read "Light" and "Heat Mat".

## 40 (2026-10-05): Comments explain why
- History in code comments (names, dates, which update changed what) rewritten as the reason the code is that way; migrations are labeled as such.
- The heat mat's tuning story and current constants moved to `docs/heat-tuning.md`.
- A suite check keeps names and dates out of code comments.

## 39 (2026-10-05): Formatting and project hygiene
- Python formatted with `ruff format`, JavaScript and CSS with Prettier (`.prettierrc.json`); no behavior change.
- `pyproject.toml` with the version, this changelog, pre-commit hooks (`.pre-commit-config.yaml`).
- The installer's git commit names the version and this heading.
- The retired `static/app.js` is removed on install.
- Suite checks on the code's text ignore whitespace and optional semicolons, and find functions by brace matching.

## 38 (2026-10-05): Dashboard as ES modules
- `static/js/` modules with explicit imports and exports, entered through `main.js`; cross-module writes through `set_<name>()`; load-time code in each module's `start()`.
- Scripts served with `Cache-Control: no-cache`. ESLint configuration (`eslint.config.mjs`, `package.json`, development only).

## 37 (2026-10-04): Code structure
- `config.FORM` defines each Settings field once; validators, form attributes and the page's save and fill code come from it.
- Browser tests (`tests/test_ui.py`, Playwright). `routes.py` split into area modules; `app.js` split into ten scripts. `ruff.toml`.
- Fixed: the smart plug account name was erased by every Save.

## 36 (2026-10-04): Memory and storage
- All OpenCV work in `imgtool.py`, out of the controller. Memory charted under Device. Polled status built once per change; lite status for the touchscreen. Journal size capped. Oldest archived timelapse runs removed below 3 GB free.

## 35 (2026-10-03): Settings in eight sections
## 34 (2026-10-03): USB link speed guard and `scripts/usbcheck.sh`
## 33 (2026-10-03): One-page touchscreen summary (`/screen`)
## 32 (2026-10-03): Touchscreen kiosk (`scripts/kiosk.sh`, cage and cog under a memory cap)
## 31 (2026-09-29): Phone layout (tray scrolls in its card); planting map count
## 30 (2026-09-29): Fixed: Save did nothing after update 29 (form validation moved to the server)
## 29 (2026-09-28): Separate player and video speeds; a new video speed re-renders
## 28 (2026-09-27): Code review fixes
- Atomic settings writes with a backup, no fallback to passwordless defaults; pump time caps on the monotonic clock; disk-space guard and alert; capture loop survives errors; last complete frame; reduced-scale decoding.

## 27 (2026-09-27): Balanced full-width chart rows
## 26 (2026-09-27): Charts drawn at their real size (no stretched text)
## 25 (2026-09-27): Chart axes, lights-off shading, one crosshair across all charts, 6 h and 3 d ranges
## 24 (2026-09-26): Timelapse speed setting
## 23 (2026-09-26): The main Save also saves Setups edits
## 22 (2026-09-26): Heat mat controller retuned from the rig's measured response; power level logged
## 21 (2026-09-26): Heat mat Auto as time-proportional PI control
## 20 (2026-09-25): Shared sensors across setups; heat mat as a setup item
## 19 (2026-09-25): Light card device rows
## 18 (2026-09-25): Readable settings error messages
## 17 (2026-09-25): Heat mat thermostat on the smart plug (`heat.py`)
## 16 (2026-09-24): Canopy readings not stale at night
## 15 (2026-09-24): Photos leave the light alone by default
## 14 (2026-09-24): Out-of-memory fixes (CMA 64 MB, two capture buffers, OOM-first helpers)
## 11 (2026-09-23): Unsaved settings no longer reverted by incoming statuses
## 10 (2026-09-23): Startup log noise; thumbnail race
## 9 (2026-09-23): AI report grouped by setup
## 8 (2026-09-22): Enlarged view opens on a single click
## 7 (2026-09-22): Enlarged view of photos and timelapse frames
## 6 (2026-09-22): Sharper paused timelapse frames; render quality
## 5 (2026-09-21): `growlight.py` split into modules
