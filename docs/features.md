# Features

What each part of the dashboard does and how it decides. Settings are listed in
[configuration.md](configuration.md); the hardware is in [hardware.md](hardware.md).

## Light

- Three schedule modes: **solar** (local sunrise/sunset with offsets), **fixed**
  (clock times; an off before on runs overnight), and **duration** (constant day
  length anchored to lights-off). Smooth fade ramps via 1 kHz hardware PWM in
  every mode.
- Draggable lights-on/lights-off handles directly on the day-curve chart
  (fixed and duration modes), snapped to 5 minutes.
- Auto / On / Off override with a live brightness slider.
- **Light response sweep**: steps 0-100% while reading the lux sensor, producing
  the fixture's measured brightness-to-lux curve. PWM dimming is not linear;
  this measures how not-linear yours is.
- **PPFD and DLI** derived from lux through a configurable spectrum factor (K)
  and a canopy factor (the sensor sits at soil level; the canopy is brighter).
- **Schedule-aware DLI forecast**: integrates the remaining scheduled brightness
  through the measured light curve, and a light-plan panel gives a verdict (on
  track / short / too much) with advice computed from the fixture's real output.
- A day-progress bar and an animated header that tracks the current stage:
  a moon drawn to the real lunar phase at night, sunrise, full sun, sunset.

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

## Sensors and charts

- Logs air temp / humidity / pressure, soil temp, lux, and per-tray soil
  moisture to SQLite (WAL), with raw samples rolled up to hourly past 30 days.
- A chart grid with every sensor at once, grouped Soil / Environment / Canopy;
  24 h default with 6 h, 3 d, 7 d and 30 d ranges, per-chart expand, value
  labels at round numbers, time labels at round hours (or dates), lights-off
  hours shaded, and one hover/tap crosshair that shows the same moment on every
  chart. Heat mat power sits with Soil, 0-100%, drawn as one step per window.
- Target bands on soil temperature and humidity; barometric tendency with a
  plain-language reading and trend line.
- Staleness marking: chips dim and charts get a badge when a reading is older
  than 3x its expected cadence.
- Probe calibration from the dashboard (wet/dry anchors per tray), optional
  temperature compensation, and a red **recal** badge when a live reading sits
  outside its own anchors, because a mis-captured wet anchor silently pegs the
  tray at 100% and blinds the dry alert otherwise.

## Water

- Per-tray pump, float switch, and moisture probe. Timed doses and
  fill-to-float with a time cap, per-dose / cooldown / daily limits, and
  one-pump-at-a-time enforcement.
- **Reservoir level** from two non-contact sensors on the outside of the source
  bucket: full / ok / empty / fault. Empty hard-refuses every pump run so the
  pumps can never run dry; fault (high sensor wet, low dry) flags a dead or
  slipped sensor.
- A fill that hits its cap without the float tripping alerts to Discord and
  switches automatic watering off.
- `auto_water` is off by default and should stay off until the probes are
  calibrated.

`run_pump(seconds)` does a timed dose; `run_pump_until_full()` fills until the
float trips, debounced against slosh, with `fill_max_seconds` as a backstop.
Both honour the per-dose, cooldown, and daily caps, run one pump at a time,
refuse outright when the reservoir reads empty, and always force the pump off in
a `finally`. A fill also stops if the reservoir runs empty partway through. A
fill that hits the cap, loses its float, or empties the reservoir raises a
Discord alert and switches `auto_water` off.

On shutdown (SIGTERM from `systemctl stop` or a reboot) every output is turned
off, running pumps stop within a tenth of a second, and nothing may turn back
on: pump starts are refused and light and fan writes can only go dark. The
outputs are switched off a second time after running pumps finish, to catch
any write that was already in flight. A fill cut short this way is not treated
as a failure, so `auto_water` comes back up the way it went down.

Keep `auto_water` off until the probes are calibrated and the seedlings are
established; overwatering (damping-off) is the number-one seedling killer.

### Reservoir level

The two XKC-Y23A sensors combine into one state: **full** (water at both),
**ok** (low only), **empty** (neither; pump runs refused), and **fault** (high
wet but low dry, which is physically impossible: a sensor died, slipped off the
wall, or needs its sensitivity pot adjusted). Empty and fault each alert with
recovery notices. Mount the flat faces tight against the bucket wall; rated
through ~13 mm of non-metal. If the bench test reads backwards, set
`GROWLIGHT_RESERVOIR_INVERT=1` instead of rewiring.

## Fan

- Auto / On / Off with PWM speed control, a separate auto-mode speed, and a
  minimum-speed floor (cheap fans stall below ~25%). Auto mode runs during the
  photoperiod and/or above a humidity threshold, and the dashboard says which
  rule is driving it.

## Camera and timelapse

- USB (v4l2) and rpicam backends, selectable. Manual exposure, white balance,
  and focus by default: auto anything makes the timelapse flicker, and auto
  white balance drifts badly under magenta grow light.
- **Focus sweep**: walks `focus_absolute` coarse then fine, scores each stop by
  Laplacian sharpness on a live capture, and pins the sharpest. The align
  preview shows a live sharpness number for manual tweaking.
- Captures at a fixed interval during the photoperiod with the light held at a
  constant brightness, so every frame is exposed identically. Each capture
  grabs a burst and keeps the last frame (the first UVC frame is routinely dark
  or torn).
- Perspective rectification from four draggable grid corners (with optional
  auto-detect). Photos are stored rotated but **not** flattened; flattening is
  applied when an image is served, so the corners stay re-draggable against the
  real scene forever. The rectified view is cached per photo and geometry.
- Frame-by-frame scrubber with a sensor overlay: scrub to any frame and see the
  soil temp, air temp, humidity, and lux at the moment it was taken. On-device
  MP4 render with explicit pixel-format and color tags so hardware decoders
  don't draw black.
- Camera health tracking: failure counts, last-good time, real error text,
  surfaced as a banner and a grayscale snapshot while failing. A master switch
  hides all camera UI and pauses the AI report.
- Manual captures are filename-tagged; the daily report always prefers the
  latest scheduled frame so an off-schedule dark shot never becomes its input.

### USB (UVC) cameras

Set **Camera type** to "USB webcam" in Settings. The capture path uses
`v4l2-ctl` rather than `rpicam-still`, and pins exposure, gain, focus and white
balance so every frame is taken under identical conditions; auto white balance
in particular drifts badly under magenta grow light and ruins both the video and
the per-cell colour analysis.

Details worth knowing:

* Controls are applied in two passes. A manual control stays flagged `inactive`
  and rejects writes until its automatic counterpart is switched off, so
  `focus_automatic_continuous=0` must land before `focus_absolute` can be set.
* Don't guess the focus value: the **Focus sweep** button walks `focus_absolute`
  coarse then fine, scores each stop by Laplacian sharpness on a live capture,
  and pins the sharpest (~90 s; the timelapse pauses). The Align preview also
  shows a live sharpness number for manual tweaking.
* Each capture grabs a short burst and keeps the last frame. The first frame
  after opening a UVC device is routinely dark or torn.

Find your camera and its supported sizes with:

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext
v4l2-ctl -d /dev/video0 --list-ctrls
```

#### Two cameras side by side

When one camera cannot see all the trays, a second USB camera beside it widens
the view. Set **Camera 2** in Settings, Camera (pick both by USB port: two
cameras of the same model share one `/dev/v4l/by-id` name and can swap
`/dev/videoN` numbers at boot, so the port path is the stable name), save, then
press **Align cameras** with the light on. It takes a frame from each camera,
matches features in the overlap (ORB, RANSAC homography, `imgtool.py align`)
and saves the fit as `cam_stitch`. From then on every photo and preview reads
camera 1, then camera 2 (never both at once, so they do not share the hub's
bandwidth), and `imgtool.py stitch` warps camera 2 onto camera 1's plane,
matches its brightness over the overlap and joins them along the seam of least
difference (OpenCV's seam finder, routed around leaves), softened slightly. Everything
downstream (grid corners, crop, canopy, timelapse, AI report) uses the stitched
photo as if it came from one wide camera.

* Aligning moves the grid corners onto the wide view and resets the crop.
  Start a new timelapse so the video does not mix one-camera and two-camera
  frames (a mixed run renders letterboxed rather than failing).
* Until the pair is aligned, photos stay camera 1's alone. Changing either
  camera or the capture size drops the alignment and moves the grid back.
* Redo Align cameras whenever a camera moves. The fit assumes the trays are
  roughly flat; tall plants in the overlap can show a slight double edge.
* Each camera has its own rotation (Rotate photo for camera 1, Rotate camera
  2), applied to its frames before matching and stitching, so the two can be
  mounted different ways round. Changing either calls for a new alignment.
* The settings (exposure, focus, white balance) apply to both cameras; the
  focus sweep scores camera 1.
* `bash scripts/usbcheck.sh` checks the link speed and capture size of both.

## Canopy tracking

- Canopy coverage per tray (share of plant pixels via the excess-green index,
  2G - R - B, which survives the magenta light where hue-based detection reads
  leaves as magenta). Logged and charted as a growth curve. Runs as a
  subprocess so OpenCV's memory is released between captures.
- Per-cell measurement and camera-based soil-dryness estimation existed and
  were deliberately retired: seedlings spill across cell lines, and a closed
  canopy hides the soil. Trays are physical boundaries; cells are not.
- Canopy readings come from photos, so they are only due while photos are
  taken: timelapse capture on, grid corners set, and the camera's light on. At
  night, or with capture off, a canopy chip or chart is never marked stale; in
  the morning its age counts from lights-on. Past three photo intervals of that
  it is stale.

`growth.py` runs as a subprocess against each scheduled frame and logs canopy
coverage per tray (`canopy:1`, `canopy:2`): the share of plant pixels in that
tray's region, via the excess-green index (2G - R - B), which survives the
magenta grow light where hue-based detection fails. It is a relative tracker,
understated under coloured light; treat it as a growth curve, not absolute
coverage.

Per-cell measurement and camera-based soil-dryness estimation existed in earlier
versions and were retired: seedlings spill across cell lines (making per-cell
numbers fiction) and a closed canopy hides the soil. Soil moisture comes from
the probes.

Define the grid by dragging its corners on the photo (or the Detect button),
then lock it.

Two switches in Settings, Camera control flattening (warping the photo to a
top-down view of the tray grid). **Show photos flattened** covers the snapshot,
the scrubber thumbnails and the rendered video; turning it off shows raw frames,
rebuilds the thumbnails automatically, and takes effect in the video on the next
Render. **Flatten for canopy analysis** only changes what `growth.py` measures.

**Player sharpness.** The Grow player plays 640px thumbnails (light enough for
8 frames a second over Wi-Fi). When it stops on a frame, paused or scrubbed to,
it swaps in that photo at full resolution from `/frame/<name>`, framed the same
way, so a still frame is as sharp as the snapshot (signed-out viewers keep the
thumbnail: a full-size frame needs a login). The rendered video is
1280 wide, lanczos-scaled, x264 ultrafast at CRF 20.

**Enlarged view.** Click the snapshot or the timelapse frame (tap on a phone)
to open it full screen; the timelapse pauses and shows that frame at full
resolution. Close with the X, Esc, or a click outside the picture. It does not
open while the grid is unlocked or a crop is being drawn, and with the cell grid
showing and signed in, a click on a cell still names the cell.

**Full frame and crop.** A USB (UVC) camera frames a different part of its
sensor at each capture size, so the size decides how much the camera sees, not
just the resolution. Settings, Camera, **Camera modes** lists the sizes the
camera offers, largest first; the largest is normally the whole sensor, the
widest view. Photos, Align and Crop all use the chosen size, so Align frames
exactly what gets captured. Changing the size resets the crop.

**Memory and large capture sizes.** The camera driver allocates its frame
buffers (about 16 MB each at 3264x2448) from memory the kernel cannot take out
of its contiguous (CMA) pool. With the stock 256 MB CMA reservation that left
too little, and the kernel's out-of-memory killer took the whole controller.
`deploy/boot-config.txt` sets CMA to 128 MB (enough for a USB camera and the
touchscreen; a headless Pi can use 64), captures request two buffers instead of four, and every camera,
thumbnail, render and analysis helper runs as the first thing the kernel kills
if memory still runs out, so a failed capture is logged ("killed by the kernel
for running out of memory") and the controller keeps running. Check the CMA
size with `grep Cma /proc/meminfo` (want `CmaTotal: 131072 kB`).

With flattening off, **Crop** on the snapshot takes a fresh full frame from the
camera; drag a rectangle over the area to keep and press **Save crop**. **Reset
crop** (in crop mode, and beside Crop whenever a crop is set) goes back to the
full frame. The crop is applied when photos are shown, not when they are taken,
so it can be changed or reset later without losing anything: the snapshot,
scrubber thumbnails (rebuilt automatically), the next Render and the AI report
all use it. The cell grid is drawn to match the cropped view. Manual captures are tagged `_m` in the filename; they are analyzed
only inside the photoperiod, and the daily AI report always prefers the latest
scheduled frame so an off-schedule dark shot never becomes its input.

## Heat mat

- The smart plug can run a heat mat instead of the light: Settings, System,
  Smart plug, "Use the plug for", then Settings, Climate, Heat mat for the
  target and cut-off, and
  Auto, On and Off on the Light card. With the plug given to the heat mat the
  light code never touches it.
- Auto is time-proportional control on a soil probe: every 15 minutes it sets
  a power share (PI on the soil error plus a feed-forward from the air
  temperature) and runs the mat for that share of the window, one pulse per
  window, pulses under a minute skipped. It holds about 0.4F peak to peak in
  simulation, where on/off control swung 2-4F. Your own On or Off acts at once.
  Pulses are logged at debug level, not as events.
- The tuning, and the model of the rig it was fitted to, are in
  [heat-tuning.md](heat-tuning.md). Each window's power level is logged as the
  reading `heat:duty` ("Heat mat power" in the Soil charts), and the
  controller's integral is kept across a restart.
- Belongs to one setup, like the fan: tick Heat mat under that setup's
  "Here" in Settings, Setups, and its controls show on that setup's tab and in
  that setup's block of the AI report.
- Safety: at the cut-off the mat goes off in any mode. In Auto, no soil reading
  for three sample intervals holds it off. Both raise an alert, and so does a
  plug that stops answering. Stopping the service turns it off. A Pi that loses
  power leaves the plug as it was, so a mat left on stays on: a plug-in
  thermostat between the plug and the mat is a sensible backstop.

## Planting map

- Two trays (count and size adjustable) with per-cell records: seed, equipment,
  sown / sprouted / archived dates, seed count, seed source, notes.
  One-tap "Sprouted" and "Archive" buttons stamp today's date.
- **Per-variety germination stats** (rate and average days to sprout) computed
  from the map and fed to the AI report.

## Alerts (Discord and ntfy)

- Each rule is a state machine: it fires once after a sustain window, reminds
  once a day while the problem persists, and posts a recovery notice when it
  clears. Analog rules clear with hysteresis margins and a clear-side
  sustain, so a value hovering at a threshold can neither spam nor flap.
- Rules: soil too warm / too cold, tray dry (calibrated probes only), humidity
  high, daily light integral under the floor (judged once, just after
  lights-off), reservoir empty, reservoir sensor fault, fill failure, camera
  failure, and sensors that stopped reporting (aged out as removed after three
  silent days).

`notify.py` (ntfy) and `discord_alert.py` (Discord webhook) are independent
transports; either fires if configured. Test them:

```bash
./venv/bin/python notify.py "test"
./venv/bin/python discord_alert.py "test"
```

Set `discord_webhook` in the secrets file (same place as the API key), then
enable **Threshold alerts** in Settings. Rules cover soil temperature (high and
low), tray dryness on calibrated probes, humidity, daily light integral that
finishes under the floor (judged once, just after lights-off), reservoir empty,
reservoir sensor fault, watering that fails to reach the float, camera failures,
and sensors that stop reporting (a sensor silent three days is treated as
removed and stops reminding).

Each rule fires once when a condition has held for the sustain window, reminds
once a day while it persists (the reminder interval is a setting), and posts a
recovery notice when it clears. Hysteresis and the sustain window mean a sensor
hovering at a threshold cannot spam the channel. Rule states are saved, so a
restart does not announce a standing problem again.

## Grow setups

For separate grow areas, each under its own light (for example seedlings under
the main fixture and transplants under the second light), define setups in
Settings, Setups. Each setup has:

- a **light**, listed by fixture: the AC fixture (dim line or smart plug), the
  5V LED panel, or none. Which fixture is which output follows the light
  backend setting, so the names stay right if that changes;
- a **light sensor**: the BH1750 under that light. A second BH1750 on the same
  I2C bus with its ADDR pin tied high answers at 0x5C and logs as `lux:2`;
  the first (ADDR low, 0x23) stays `lux`. A light and a sensor can each belong
  to only one setup;
- an optional **lux per umol** factor for that light's spectrum (blank uses
  `lux_to_ppfd_k`);
- a **DLI target band**;
- whether the **fan**, the **camera** and the **reservoir** sensors are here
  (one setup each; the reservoir row shows on that tab only). The fan's
  auto mode then runs on this setup's light hours; the camera captures during
  them, its capture brightness is applied only when this setup is on the main
  light, and the snapshot, timelapse and AI report show on this tab only (the
  report judges this setup's light). Canopy is measured only for this setup's
  trays, left to right across the grid, so set the grid's rows and columns to
  those trays; other trays' old canopy readings drop out of the chips, charts
  and report. The fan controls show on this tab only;
- the **trays** that belong to it. None ticked shows every tray. A tray's own
  probe, float and canopy readings come with it;
- the **sensors** that belong to it. None ticked shows every sensor.

The editor's choices update as things change: a tray added under Trays or a
sensor that starts reporting appears in the list without losing unsaved edits.

With two or more setups, tabs appear at the top of the dashboard. The chosen
tab decides whose light the Day card's DLI bar, pace and forecast, and the Plan
card describe, which sensor chips and charts are shown, and which trays the
planting map and watering controls show. The short-day and
too-much-light alerts are judged per setup and name it, and the AI report is
told each setup's light. The Light card, the day phase and brightness, the
photoperiod bar and the schedule chart follow the selected setup's light too:
on a tab whose light is the second fixture, Auto/On/Off and the slider set that
light (`light2_override`, `light2_bright`) and dragging the chart's edges sets
its hours (`light2_start`, `light2_end`); windows that cross midnight are not
draggable. The one-line second-light status is hidden when there are tabs.

On each tab, **Calibrate** sweeps that tab's light against that setup's light
sensor (the other light goes dark for the few minutes it runs), and each
fixture keeps its own curve and calibration (`light_curve` / `light_linear` for
the main light, `light2_curve` / `light2_linear` for the second). **Arm** under
Auto-water arms or disarms that setup's pump trays only
(`auto_water_trays`); a tray with a blocker on another tab does not stop it,
and a failed fill disarms just the tray that failed.

Each setup's Plan verdict waits for its own light: the day is judged once that
light's hours are over and its sensor has read dark for a quarter hour (a setup
with no light waits for both lights).

The Day card also charts daily light through today (solid) and yesterday
(dashed) against the target: the shaded band is where the day should end, and
the wedge is where the total should be at each hour of the photoperiod.

## Sensors per tray

Settings, Setups lists each tray under its setup with a **Moisture** probe, a
**Float**, and checkboxes for any other sensor placed there (a soil
temperature probe, the air sensor). Stored as `tray_sensors` in
`config.json`: `{tray id: [sensor keys]}`.

- Watering for a tray reads the moisture probe placed in it and stops a fill
  on its float; the pump stays the tray's own (pump 1 fills tray 1). A tray
  takes one probe and one float, and no probe or float can be in two trays:
  picking one for a tray takes it from the tray that had it.
- A tray never given a list keeps the probe and float of its own number when
  that hardware is wired, so an existing install works unchanged.
- Readings follow their tray: the tray's setup tab shows them, probes are
  named with their tray ("Soil moisture 1 · Seedling 1"), dry alerts say which
  tray, and the AI report lists each tray's sensors with the tray.
- Probe calibration stays with the probe, so moving a probe to another tray
  keeps its wet and dry anchors.

## Daily AI report

- Sends the latest scheduled photo plus the environment, canopy trend,
  germination stats, planting map, pump runtime, fan state, barometric trend,
  and light metrics to the Claude API and renders a structured report:
  summary, overall health, growth stage, notable-cell notes, a **variety
  check** (flags a cell whose seedling looks inconsistent with what the map
  says was sown there; it never guesses species), light/water assessments,
  concerns, and recommendations. Runs daily at a set time or on demand; waits
  for NTP sync at boot so the clockless Pi never fires a spurious report.
- With two or more grow setups, the data is grouped by setup: each block has
  that setup's light (fixture, schedule, level), its light sensor, PPFD and DLI
  against its own target, its trays with probe moisture, canopy, float and
  planting, its other sensors, and the fan or reservoir if ticked there. The
  setup holding the camera is marked as the one in the photo. A reading no
  setup claims is listed as shared. The grouping is worked out from Settings,
  Setups each time a report runs, so a change applies to the next report.

When enabled, the controller sends the latest photo (downscaled) plus the current
data to the Claude API once a day at the configured time, and shows the result on
the dashboard while pushing the summary to ntfy/Discord. A restart does **not**
regenerate the report; it keeps the last one. New reports come only from crossing
the scheduled time or pressing "Generate now". Current models think before
answering and thinking is billed as output, so a report costs a few cents; the
8,000-token output cap (`ai_report.MAX_TOKENS`) bounds it at roughly 13 cents at
Sonnet 5's list price. If the model runs out of room, the report shows that as an
error instead of an empty "(no summary)".

The prompt tells the model that grow lights tint the photo without assuming a
color. To be specific, describe your light in `ai_notes`. There is no dashboard
field for it; from the Pi (the first line is only needed with a password set):

```bash
curl -s -c /tmp/gl -H 'Content-Type: application/json' -d '{"password": "YOUR-PASSWORD"}' http://127.0.0.1:5000/api/login
curl -s -b /tmp/gl -H 'Content-Type: application/json' \
  -d '{"ai_notes": "Full-spectrum white LED fixture. Peat/perlite seed starter, bottom-watered."}' \
  http://127.0.0.1:5000/api/ai_settings
```

## Settings layout

Eight collapsible sections: **Light** (fixture, schedule, second light, light
metrics), **Climate** (heat mat, fan, target bands), **Watering** (auto-watering
and sensor smoothing), **Camera**, **Setups**, **Trays**, **Alerts**, and
**System** (location, smart plug, display, backup).

## Authentication

With `password_hash` blank the dashboard is fully open. Set a password to make it
read-only until login (viewing stays open; editing, rendering, watering, and
settings require a session):

```bash
./venv/bin/python scripts/set_password.py
```

Every request goes through one check (`auth.check_request`) before its route
runs. A route needs a login unless it is marked public in the code, and a
request that changes anything must carry a JSON object, which is what stops
another site's page from posting to the dashboard through your browser.

While no password is set, the dashboard answers only when it is addressed by
its IP address, `localhost` or the Pi's hostname (plain or `.local`). That
blocks DNS rebinding, where a page on another site points its own name at
your Pi. If you reach a password-less dashboard through another name (a DNS
entry on your router, a reverse proxy), list it in `.env`:

```
GROWLIGHT_ALLOWED_HOSTS=grow.lan,seedlings.example.org
```

With a password set there is nothing to list: any name works, because the
session cookie belongs to the name you signed in under.

A session lasts 30 days and belongs to the password it signed in with: setting a
new password signs every browser out. To sign everyone out without changing the
password, delete `.secret` in the app folder and restart the service.

A wrong password makes that client wait before its next try: 1 s, doubling with
each failure in a row up to 60 s. Other clients are not held up. Behind a
reverse proxy every client has the proxy's address, so they share one wait
unless the proxy is named in `GROWLIGHT_TRUSTED_PROXY`
([install.md](install.md#reverse-proxy)).

`cookie_secure` defaults to `"auto"`: the session cookie is marked Secure when the
request arrived over HTTPS, which the dashboard knows from the reverse proxy
named in `GROWLIGHT_TRUSTED_PROXY` (`X-Forwarded-Proto: https`). Set `true` to
force it (only if every request is HTTPS, or login silently fails over http) or
`false` to never set it.

## Safeguards against a bad day

- `config.json` is written atomically (temp file, fsync, rename) and the
  previous good copy is kept as `config.json.bak`. If `config.json` cannot be
  read at start the backup is used; if neither can, the app runs on defaults
  but saves nothing and refuses every change until the file is restored (the
  defaults have no password).
- Pump time caps run on the monotonic clock, so an NTP step at boot cannot
  stretch or cut a fill.
- Below 3 GB free on the SD card an alert goes out; below 1 GB photos are
  skipped so the database and settings keep working.
- A capture keeps the last complete frame of the burst and writes it whole;
  one failed capture does not stop the timelapse.
