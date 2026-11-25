# API

Read endpoints are open, except `/api/backup`. Every mutating endpoint requires
a session when a password is set, and always requires a JSON body
(`Content-Type: application/json`, `{}` when there is nothing to send); other
content types get 415. That is the defense against another site posting a form
at the dashboard. From a shell:
`curl -X POST -H 'Content-Type: application/json' -d '{}' http://<pi>:5000/api/capture`.

Signed-out viewers see neither the location nor any credential in
`/api/status`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/status` | Full state: light, sensors, water, settings, auth |
| GET | `/api/series` | Sensor history for charts |
| GET | `/api/photos`, `/photo/latest`, `/thumb/<name>`, `/frame/<name>` | Timelapse frames (thumbnail, and full size framed like it; `/frame/` needs a login) |
| GET | `/video` | Rendered MP4 |
| GET | `/api/float` | Fast float read |
| GET | `/api/report` | Latest AI report + generating flag |
| GET | `/api/series_all` | Every sensor's history in one call (feeds the chart grid) |
| GET | `/api/frame_context?ts=` | Sensor readings nearest a timelapse frame (feeds the scrubber overlay) |
| GET | `/api/stream` | Server-sent events: the status pushed as it changes (6 connections max, 2 of them for signed-out viewers) |
| GET | `/api/host` | Pi health: CPU temperature, load, memory, throttling, Wi-Fi |
| GET | `/api/plantings` | Finished plantings (germination history) |
| GET | `/preview.jpg`, `/rectified.jpg` | Latest alignment preview; latest frame flattened to the tray grid |
| GET | `/api/backup` | Database snapshot, `config.json` and restore steps; `?secrets=1` adds `.env`. Requires login |
| POST | `/api/settings` | Update settings. Accepts partial bodies; valid fields are saved and invalid ones come back by name in `errors`, so one bad field never silently discards the rest |
| POST | `/api/grid`, `/api/detect_grid` | Cell grid |
| POST | `/api/focus_sweep` | Run (or cancel) the USB camera focus sweep |
| POST | `/api/probe_cal` | Capture a tray probe's wet/dry anchor |
| POST | `/api/purge_series` | Delete a sensor prefix's logged history (explicit, destructive) |
| POST | `/api/probe_tempcomp` | Estimate/apply a probe's temperature-drift coefficient |
| POST | `/api/fan` | Fan mode: `auto`, `on`, or `off` |
| POST | `/api/schedule` | Adjust the light window (used by the chart drag handles) |
| POST | `/api/light` | Manual light hold (auto/on/off) + manual brightness |
| POST | `/api/tray_layout` | Add, remove, rename or resize a tray (confirm required if cells would be lost) |
| POST | `/api/reset_timelapse` | Archive the current run and start fresh (confirm required) |
| POST | `/api/rebuild_thumbs` | Regenerate thumbnails after the corners move |
| POST | `/api/trays` | Save the planting map (what's sown in each cell) |
| POST | `/api/render` | Render the timelapse MP4 |
| POST | `/api/capture` | Take a photo now (tagged manual) |
| POST | `/api/pump` | Timed dose or fill-to-float, per tray (`tray: "1"` or `"2"`) |
| POST | `/api/ai_settings`, `/api/report` | AI report config / generate now |
| POST | `/api/preview` | Take an alignment preview frame |
| POST | `/api/light_sweep` | Run (or cancel) the light response sweep / Calibrate |
| POST | `/api/lightning` | Thunderstorm effect (dim backend only) |
| POST | `/api/auto_water` | Arm or disarm auto-watering |
| POST | `/api/planting_end`, `/api/planting_restore` | Move a cell to the planting history, or bring it back |
| POST | `/api/plug_discover`, `/api/plug_test` | Find and test a Kasa smart plug |
| POST | `/api/login`, `/api/logout` | Auth. A wrong password makes that client address wait (1 s, doubling to 60 s); a login sent during the wait gets 429 with `Retry-After` |

Secrets (`password_hash`, webhook URLs, ntfy topic, Kasa credentials) are
never in `/api/status`. With a password set, a signed-out viewer gets a
shorter status still: only the settings the read-only page draws with
(`status.VISITOR_SETTINGS` and the `screen_*` ones), sunrise and sunset to the
nearest quarter hour (and the light window too, when it follows the sun), and
no address for the smart plug. `/api/host` leaves out the hostname, the IP
address and the Wi-Fi link for them.
