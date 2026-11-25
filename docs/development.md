# Development

## Code checks

- **Settings fields are defined once**, in `config.FORM`: kind, accepted
  range, step. The server's validators for those keys, the form's
  min/max/step (`routes.form_attrs`) and the page's save and fill code
  (`readField`/`fillField` in `static/js/photos.js`, from a copy of the table
  embedded in the page) are generated from it. To add a setting: a default in
  `DEFAULTS`, one line in `FORM`, and the field in `index.html`
  (`<input {{ fa('key') }}>` for numbers). The suite checks they agree.
- `python -m pytest` runs the server tests (`tests/test_*.py`, one file per
  area) anywhere, the Pi included: the server's behavior through Flask's test
  client, plus some checks on the page's code. `tests/harness.py` imports the
  app from a temp copy with GPIO, PWM and I2C faked, and a fixture in
  `tests/conftest.py` gives each test the same settings and an empty readings
  history, so a test passes on its own (`pytest tests/test_heat.py`,
  `pytest -k overnight`).
- `tests/test_ui.py` runs on a desktop with Playwright
  (`pip install playwright && playwright install chromium`): the app from a
  throwaway copy with fake GPIO, driven in a headless browser. Every Settings
  field is changed, saved and checked after a reload; refused values,
  units, Setups, charts, the phone layout and the touchscreen summary are
  checked by what happens on screen, not by how the code is written. Without
  Playwright it skips and exits 0.
- `ruff check .` lints the Python with the rules in `ruff.toml`; `ruff format`
  formats it. `mypy` type-checks the modules listed in `pyproject.toml`: the
  ones with no hardware or web framework in them, which are fully annotated. Prettier formats the JavaScript and CSS (`.prettierrc.json`).
  `pip install -r requirements-dev.txt` and `npm install` bring the tools;
  `pre-commit install` runs them on every commit
  (`.pre-commit-config.yaml`), and `.gitlab-ci.yml` runs them with the test
  suite on every push.
- The version is in `pyproject.toml` and heads `CHANGELOG.md`; the installer
  commits each update as "openSeedling <version>: <changelog heading>". A
  `REMOVED` file in an update zip lists files the installer deletes.
- The dashboard's scripts are ES modules (`static/js/`, entered through
  `main.js`): each imports what it uses and exports what it offers, so a
  missing or renamed function is an error at load, not a silent global. A
  module cannot assign another module's variable; it calls that module's
  `set_<name>()`. Code that runs at page load sits in each module's
  `start()`, which `main.js` calls in a fixed order. Scripts are served with
  `Cache-Control: no-cache`, so a browser revalidates each one after an
  update. Lint them with `npm install && npm run lint` (ESLint, rules in
  `eslint.config.mjs`; development only, nothing installs on the Pi).

## Project layout

```
growlight.py        entry point: registers the routes, starts the loops and waitress
config.py           settings: DEFAULTS, config.json load/save, migrations, validators, shared state
events.py           publish/subscribe for "the status changed"
hardware.py         pins and actuators: PWM channels, pumps, fan, shutdown steps, cleanup()
readings.py         readings in and out of the database: validation and transient filtering
units.py            display units (Celsius/Fahrenheit, hPa/inHg)
light.py            both lights: backends, schedules, ramps, calibration, dithering, storm, who holds the light
control.py          the control loop: lights from the schedule, the fan
setups.py           grow setups, light windows, DLI integration and curves, Plan verdicts
water.py            fills, reservoir, probe filtering and moisture, auto-water, post-fill checks
monitor.py          sampling and live readings, validation, sensor health, alerts, AI report loop
camera.py           capture, crop and flatten, thumbnails, focus sweep, render, canopy
heat.py             heat mat thermostat on a smart plug
status.py           status payload, display units, redaction, live event stream
db.py               SQLite logging (WAL) with downsampling
sensors.py          sensor I/O: probes, floats, reservoir, air, lux, soil temp
alerts.py           threshold alert state machine (sustain, hysteresis, reminders)
quality.py          reading checks: implausible, stuck and contradictory sensors
applog.py           logging setup
growth.py           per-tray canopy vision (subprocess)
detect_corners.py   grid corner auto-detect (subprocess)
hoststats.py        Pi health: CPU temp, load, memory, throttling, wifi
notify.py           ntfy push transport
discord_alert.py    Discord webhook transport
ai_report.py        Claude API daily report
imgtool.py          every OpenCV job, run as a short-lived process (never in the controller)
auth.py             the dashboard password, the touchscreen's local trust, require_auth
routes.py           Flask app, sign-in, pages, live status, settings
routes_camera.py    photos, thumbnails, frames, grid, capture, timelapse
routes_garden.py    probes, floats, plantings, tray map, pumps, auto-water
routes_climate.py   light, schedule, sweeps, smart plug, fan, heat mat
routes_data.py      AI report, backups, readings history, host stats
templates/index.html, templates/screen.html
static/js/*.js      the dashboard: ES modules, entered through main.js
static/{style.css,screen.js,screen.css}
scripts/            setup.sh (install), deploy.sh and update.sh (updates), kiosk.sh
                    (touchscreen), set_password.py, and bench tools for the light
tests/              pytest files for the server (no hardware), test_ui.py (browser)
deploy/             box config applied by setup.sh (boot, unit, packages, env reference)
```

The modules are layered, and each imports only from layers below it:
`config`, `db` and `sensors`; then `hardware`, `readings` and `auth`; `light`;
`setups` and `heat`; `water`, `camera` and `control`; `monitor`; `status`;
`routes`; and `growlight` on top, with the four route blueprints. A lower
layer that has news for a higher one announces it through `events` or
registers a callback (`hardware.at_shutdown`, `config.SETTINGS_VALIDATORS`).
They refer to each other as `module.name`, never `from module import name`,
because several module globals are rebound at runtime.

Runtime files (`config.json`, `growlight.db`, `timelapse/`, `.secret`,
`.anthropic_key`, `.discord_webhook`, `ai_report.json`) are gitignored.

## Memory and storage

- The controller never imports OpenCV (about 40 MB that is never given back).
  Every warp, crop, rotation, resize and sharpness score runs in `imgtool.py`,
  a short-lived helper launched like the other image jobs, first in line for
  the kernel's out-of-memory killer and freed when it exits. Measured on a
  test box, the controller stays at about 40 MB through a round of image work;
  with OpenCV imported it grew to 119 MB.
- Memory is logged every sample and charted under Device: free memory, swap
  in use, the controller, and the touchscreen session.
- The polled status is built once per change and shared; every API change
  announces a new status, so open tabs and the touchscreen update at once.
  The touchscreen asks for a lite status (`/api/status?lite=1`).
- `setup.sh` caps the system journal (`deploy/journald.conf`: 50 MB, never
  within 1 GB of full, one month).
- Below 3 GB free, archived timelapse runs (`timelapse_archive/`) are removed
  oldest first until 4 GB is free; the newest archive is kept unless free
  space drops under 1 GB, and the current run is never touched. Each removal
  is logged as an event.
