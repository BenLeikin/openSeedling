# Growlight

A self-contained seedling station for the Raspberry Pi: a sun-synced grow light,
a timelapse camera, camera-based moisture and growth tracking, optional automatic
watering, and a daily AI plant-health report, all driven from a single Flask
dashboard.

It runs on a Pi Zero 2 W controlling a cheap USB LED grow light through a MOSFET,
and grew from "dim a light on a schedule" into a small greenhouse controller.

![The Growlight dashboard](docs/dashboard.png)

*Light status and schedule, the latest snapshot with the cell grid, sensor charts,
the watering controls, and the daily AI plant-health report.*

---

## What it does

- **Sun-synced lighting.** Tracks local sunrise/sunset and drives the light with
  smooth fade-in/fade-out ramps via 1 kHz hardware PWM. Offsets, max brightness,
  and ramp length are all adjustable; a header graphic shows the current stage
  (moon at night drawn to the real lunar phase, sunrise, full sun, sunset).
- **Timelapse.** Captures a frame at a fixed interval during the photoperiod,
  holding the light at a constant brightness so every frame is exposed the same.
  Plays back in the browser frame-by-frame and renders an MP4 on-device.
- **Camera vision.** Per-cell soil moisture (from surface brightness) and a canopy
  growth index, overlaid on the latest photo as a grid, logged and charted.
- **Watering.** A submersible pump with a float switch can fill the tray to a set
  level. Heavily guarded with per-dose, cooldown, and daily caps. Fully automatic
  watering is off by default and should stay off until moisture is calibrated.
- **Daily AI report.** Sends the latest photo plus the sensor data to the Claude
  API and gets back a structured plant-health report: germination, growth stage,
  per-cell notes, best-effort species guesses, light/water assessment, concerns,
  and recommendations.
- **Alerts.** Pushes report summaries (and anything else you wire up) to your
  phone via ntfy and/or a Discord channel.
- **Dashboard.** Live-editable settings, sensor charts, the timelapse player, and
  the report card. Read-only until you log in.

---

## Hardware

| Part | Detail |
| --- | --- |
| Computer | Raspberry Pi Zero 2 W (64-bit Raspberry Pi OS) |
| Light | 5 V USB LED grow light, ground switched low-side through an XY-MOS D4184 MOSFET |
| Camera | Raspberry Pi camera |
| Pump | 5 V USB submersible pump through a second D4184 + 1N5819 flyback diode, on its own 5 V supply with a common ground |
| Float | Normally-open float switch in the destination tray |

### Pinout (BCM)

| Signal | GPIO | Physical pin | Notes |
| --- | --- | --- | --- |
| Light PWM | 18 | 12 | Hardware PWM, 1 kHz, to the light MOSFET gate |
| Pump tray 1 | 24 | 18 | To that pump's MOSFET gate (separate 5 V brick + common ground) |
| Pump tray 2 | 26 | 37 | Second pump MOSFET gate, same supply rules |
| Fan | 20 | 38 | Fan MOSFET gate; 5 V fan on the pump supply, flyback across the fan |
| Float tray 1 | 23 | 16 | Internal pull-up; other leg to GND (`FLOAT_ENABLED` in `sensors.py`) |
| Float tray 2 | 22 | 15 | Internal pull-up; other leg to GND |

Pin overrides live in `.env` next to the app, written by `scripts/setup.sh` and
read by the systemd unit:

| Variable | Example | Notes |
| --- | --- | --- |
| `GROWLIGHT_LIGHT_PIN` | `18` | Hardware PWM: **only 18 or 19** |
| `GROWLIGHT_PUMP_PINS` | `1:24,2:26` | One entry per tray with a pump |
| `GROWLIGHT_FLOAT_PINS` | `1:23,2:22` | One entry per tray with a float |
| `GROWLIGHT_FAN_PIN` | `20` | Any free GPIO (software PWM) |
| `ANTHROPIC_API_KEY` | | Enables the daily AI report |
| `DISCORD_WEBHOOK` | | Enables threshold alerts |

Answer `none` to any hardware prompt you do not have; that device is then never
claimed, its dashboard controls stay hidden, and re-running setup offers `none`
again rather than reverting to the suggested pin. Wire it up later and re-run
setup to enable it.

Edit `.env` and restart to apply; the journal prints the pins in use at startup.
`.env` is gitignored and written mode 600 because it holds secrets.
| I2C SDA | 2 | 3 | To ADS1115 SDA (soil moisture ADC) |
| I2C SCL | 3 | 5 | To ADS1115 SCL |

The ADS1115 runs off 3.3 V (pin 1) and GND (pin 9), with ADDR to GND for
address 0x48. Two capacitive soil probes go on A0 (tray 1) and A1 (tray 2),
powered from the same 3.3 V rail so their output can't exceed the ADC supply.
Enable I2C with `raspi-config` (or `dtparam=i2c_arm=on`) and confirm the board
shows up: `i2cdetect -y 1` should list `48`.

Enable hardware PWM on GPIO18 by adding to `/boot/firmware/config.txt`:

```
dtoverlay=pwm,pin=18,func=2
```

**Float fail-safe.** Mount the float so that rising water *opens* the switch. Open
(reads "full") means stop, which is also the broken-wire state, so a disconnected
float fails safe by refusing to pump.

**Pump siphoning.** A float only cuts the pump; it can't stop a siphon. Either keep
the discharge above the water line or drill a ~1.5 mm vent hole at the top of the
tubing run.

Wiring diagrams are in `growlight_wiring.svg`, `pump_float_wiring.svg`, and
`sensor_wiring.svg`.

---

## Install

On the Pi:

```bash
git clone <your-repo-url> ~/growlight
cd ~/growlight
bash scripts/setup.sh
```

It asks which GPIO each device is on (suggesting the defaults below) and
optionally takes your Anthropic API key and Discord webhook. Answers go to
`.env`, which the systemd unit reads, so nothing in the Python needs editing.
Re-running offers your previous answers as the defaults; `bash scripts/setup.sh
--defaults` skips every prompt.

The script is idempotent and preserves `config.json` and `growlight.db`, so it is
safe to re-run after an update. It handles:

1. System packages (`rpicam-apps`, `ffmpeg`, `i2c-tools`)
2. Boot config: the PWM overlay for light dimming, plus I2C and 1-Wire for the
   sensors. **Adding any of these requires a reboot**, and the script says so.
3. Swap, so a timelapse render does not OOM a 512 MB board
4. A venv with the core and sensor libraries
5. The systemd unit, enabled at boot
6. Shell convenience (venv auto-activate)
7. A hardware check: which I2C addresses and DS18B20 sensors are actually visible

That last step is the useful one when something is not reading. It distinguishes
"the sensor is not wired" from "the software is not seeing it": if `i2cdetect`
does not list the address, no amount of restarting the service will help.

After the first run, reboot if asked, then set the location and schedule in
Settings on the dashboard.

### Run as a service

`scripts/setup.sh` writes and enables `/etc/systemd/system/growlight.service`
for you. Useful commands:

```bash
sudo systemctl status growlight
sudo systemctl restart growlight
journalctl -u growlight -f
```

### Deploy updates

```bash
cd ~/growlight && git pull && sudo systemctl restart growlight
```

Static assets are versioned by file mtime, so the browser picks up new JS and CSS
on its own; no hard refresh needed.

If a pull leaves one file behind, the service can end up running a mix of old and
new code. `git status` should be clean before a pull, and the journal is the
place to confirm the restart came up without a traceback.

---

## Configuration

Settings live in `config.json` (created on first run, gitignored). Most are
editable from the dashboard Settings panel; the rest are edited in the file.

| Key | Default | Meaning |
| --- | --- | --- |
| `latitude` / `longitude` / `timezone` | Thousand Oaks, CA | Location for sun times |
| `max_bright` | 100 | Peak brightness (%) |
| `ramp_min` | 30 | Fade-in/out length (minutes) |
| `sunrise_offset_min` / `sunset_offset_min` | 0 | Shift the on/off times |
| `capture_enabled` | false | Timelapse on/off |
| `capture_interval_min` | 30 | Minutes between frames |
| `capture_brightness` | 100 | Brightness held during each photo |
| `roi` | "" | Crop as `x,y,w,h` fractions, blank = full frame |
| `cam_width` / `cam_height` | 2304 / 1296 | Capture resolution at full field of view. The Module 3 sensor is 4608x2592, but a full 12MP capture runs the Pi Zero 2 W out of memory, so the default is the 2304x1296 binned mode (same view, ~3MP). Keep the sensor's 16:9 aspect or the frame gets cropped. Raise to 4608x2592 only on a Pi with more RAM |
| `sample_interval_min` | 5 | Sensor logging interval |
| `auto_water` | false | Master switch for automatic watering (keep off until calibrated) |
| `pump_max_seconds` | 20 | Cap on a single dose |
| `pump_cooldown_min` | 30 | Minimum wait between auto doses |
| `pump_daily_max_seconds` | 180 | Daily runaway backstop |
| `fill_max_seconds` | 60 | Cap on a fill-to-float run if the float never trips |
| `ntfy_topic` | "" | Set to enable ntfy push |
| `discord_webhook` | "" | Set to enable Discord alerts |
| `ai_enabled` | false | Daily AI report on/off |
| `ai_model` | `claude-opus-4-8` | Claude model for the report |
| `ai_report_hour` / `ai_report_minute` | 8:00 | When the daily report runs |
| `ai_notify` | true | Push the report summary |
| `ai_notes` | (grow description) | Context handed to the AI; list what you planted here to sharpen species guesses |
| `probe_cal` | {} | Per-tray ADC wet/dry anchors for the soil probes, set from the dashboard |
| `probe_names` | Tray 1 / Tray 2 | Labels for the two probes (A0 = tray 1, A1 = tray 2) |

### Secrets (all gitignored)

| File | Purpose |
| --- | --- |
| `.secret` | Flask session key (auto-generated) |
| `.anthropic_key` | Claude API key for the AI report (or set `ANTHROPIC_API_KEY`) |
| `.discord_webhook` | Discord webhook URL (or put it in `config.json`) |

```bash
echo 'sk-ant-...'                              > ~/growlight/.anthropic_key
echo 'https://discord.com/api/webhooks/...'    > ~/growlight/.discord_webhook
chmod 600 ~/growlight/.anthropic_key ~/growlight/.discord_webhook
```

---

## Features in detail

### Authentication

With `password_hash` blank the dashboard is fully open. Set a password to make it
read-only until login (viewing stays open; editing, rendering, watering, and
settings require a session):

```bash
./venv/bin/python scripts/set_password.py
```

Behind HTTPS keep `cookie_secure: true`; for plain-http local testing set it
false.

### Camera moisture & growth

`growth.py` runs as a subprocess against the latest frame. Moisture is the median
surface brightness of the soil per cell (darker = wetter), which is robust under
the magenta grow light where colour-based methods fail. Growth is an excess-green
canopy index, a relative tracker, understated under coloured light.

Calibrate moisture per cell with the "Set wet 100%" / "Set dry 0%" buttons after a
capture lands. Define the cell grid by dragging its corners on the photo (or the
Detect button), then lock it.

### Watering

`run_pump(seconds)` does a timed dose; `run_pump_until_full()` fills until the
float trips, debounced against slosh, with `fill_max_seconds` as a backstop that
flags a dry reservoir. Both honour the per-dose, cooldown, and daily caps and
always force the pump off in a `finally`. Keep `auto_water` off until moisture is
calibrated and the seedlings are established; overwatering (damping-off) is the
number-one seedling killer.

### Daily AI report

When enabled, the controller sends the latest photo (downscaled) plus the current
data to the Claude API once a day at the configured time, and shows the result on
the dashboard while pushing the summary to ntfy/Discord. A restart does **not**
regenerate the report; it keeps the last one. New reports come only from crossing
the scheduled time or pressing "Generate now". Cost is roughly a cent or two per
report.

### Alerts

`notify.py` (ntfy) and `discord_alert.py` (Discord webhook) are independent
transports; either fires if configured. Test them:

```bash
./venv/bin/python notify.py "test"
./venv/bin/python discord_alert.py "test"
```

---

## Reverse proxy

A separate box runs nginx and proxies the dashboard to the public site over HTTPS:

```nginx
location / {
    proxy_pass http://grow:5000;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

---

## API

Read endpoints are open; mutating ones require a session when a password is set.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/status` | Full state: light, sensors, water, settings, auth |
| GET | `/api/series` | Sensor history for charts |
| GET | `/api/photos`, `/photo/latest`, `/thumb/<name>` | Timelapse frames |
| GET | `/video` | Rendered MP4 |
| GET | `/api/float` | Fast float read |
| GET | `/api/report` | Latest AI report + generating flag |
| GET | `/api/series_all` | Every sensor's history in one call (feeds the chart grid) |
| POST | `/api/settings` | Update light/capture settings |
| POST | `/api/grid`, `/api/detect_grid` | Cell grid |
| POST | `/api/dryness_cal` | Capture a wet/dry moisture anchor |
| POST | `/api/probe_cal` | Capture a tray probe's wet/dry anchor |
| POST | `/api/probe_tempcomp` | Estimate/apply a probe's temperature-drift coefficient |
| POST | `/api/fan` | Fan mode: `auto`, `on`, or `off` |
| POST | `/api/schedule` | Adjust the light window (used by the chart drag handles) |
| POST | `/api/light` | Manual light hold (auto/on/off) + manual brightness |
| POST | `/api/tray_layout` | Add, remove, rename or resize a tray (confirm required if cells would be lost) |
| POST | `/api/trays` | Save the planting map (what's sown in each cell) |

### Schedule modes

| Mode | Behaviour |
| --- | --- |
| `solar` | Follows local sunrise/sunset with offsets (tracks the season) |
| `fixed` | The same clock times daily; an off time before the on time runs overnight |
| `duration` | A constant day length anchored to the off time: lights-off stays put, lights-on moves |

Set it under Settings > Schedule. In `fixed` and `duration` modes the day-curve
chart gets draggable handles on the lights-on and lights-off edges (logged-in
only): drag to adjust, snapped to five minutes. In `fixed` mode each edge sets
its own clock time; in `duration` mode the right edge moves the anchor and the
left edge changes the day length. Dragging commits via `POST /api/schedule`. Sunrise and sunset are still computed and shown
on the dashboard in every mode. Ramps apply the same way in all three.

### Discord alerts

Set `discord_webhook` in the secrets file (same place as the API key), then
enable **Threshold alerts** in Settings. Rules cover soil temperature (high and
low), tray dryness on calibrated probes, humidity, watering that fails to reach
the float, camera failures, and sensors that stop reporting.

Each rule fires once when a condition has held for the sustain window, reminds
on the cooldown interval while it persists, and posts a recovery notice when it
clears. Hysteresis and the sustain window mean a sensor hovering at a threshold
cannot spam the channel. Alert state is in memory, so a restart re-arms
everything.
| POST | `/api/render` | Render the timelapse MP4 |
| POST | `/api/capture` | Take a photo now |
| POST | `/api/pump` | Timed dose or fill-to-float, per tray (`tray: "1"|"2"`) |
| POST | `/api/ai_settings`, `/api/report` | AI report config / generate now |
| POST | `/api/login`, `/api/logout` | Auth |

---

## Project layout

```
growlight.py        main app: light/capture/sample/watering/report loops + Flask
db.py               SQLite logging (WAL) with downsampling
sensors.py          sensor I/O, float switch
growth.py           per-cell moisture + canopy vision (subprocess)
detect_corners.py   grid corner auto-detect (subprocess)
notify.py           ntfy push transport
discord_alert.py    Discord webhook transport
ai_report.py        Claude API daily report
templates/index.html, static/{app.js,style.css}
scripts/set_password.py
setup.sh, test_ramp.py
```

Runtime files (`config.json`, `growlight.db`, `timelapse/`, `.secret`,
`.anthropic_key`, `.discord_webhook`, `ai_report.json`) are gitignored.

---

## Troubleshooting

- **Timelapse MP4 plays but is black.** A pixel-format/colour issue, not a corrupt
  file. The render forces limited-range `yuv420p`, a mod-16 height, and explicit
  BT.601 colour tags precisely so hardware decoders (VLC's especially) don't draw
  black. If an old file is black, just re-render. To check a file:
  `ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt,color_range,color_space ...`.
- **Discord returns HTTP 403.** Discord's Cloudflare blocks the default
  `Python-urllib` user agent; `discord_alert.py` sends a real `User-Agent`, which
  fixes it.
- **`lgpio` won't pip-install.** Use the apt package `python3-lgpio` and enable
  system site packages in the venv (see Install).
- **A new AI report fires on every reboot.** The Pi Zero 2 W has no RTC, so at
  boot the clock is wrong until NTP corrects it. The report scheduler waits for
  the clock to sync (via `/run/systemd/timesync/synchronized`) before deciding
  anything, so it won't mistake the post-NTP time jump for the scheduled time. If
  you don't use systemd-timesyncd, add `After=time-sync.target` to the service
  unit and enable `systemd-time-wait-sync` so the service starts only once the
  clock is set.
- **Pump does nothing.** Check the separate 5 V supply and common ground, and that
  `gpiozero` loaded (the log prints if the GPIO backend is unavailable).
- **Photo looks zoomed in / cropped after a camera swap.** The capture resolution
  must match the sensor's native aspect ratio, or libcamera crops into the middle
  of the sensor. Set `cam_width`/`cam_height` to a 16:9 size for Module 3 (default
  2304x1296) or 2592x1944 for Module 1. After changing the frame, re-do the cell
  grid, since the old corners no longer line up.
- **"Capture failed" while the live preview works.** The preview runs at a lower
  resolution than a full capture. If Take photo fails but Align previews fine, the
  capture resolution is too large for available memory, which happens on the Pi
  Zero 2 W (512MB) at the sensor's full 4608x2592. Lower `cam_width`/`cam_height`
  to 2304x1296; the preview proves that size works on the hardware.
