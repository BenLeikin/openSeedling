# Configuration

Settings live in `config.json` (created on first run, gitignored). Most are
editable from the dashboard Settings panel; the rest are edited in the file.

| Key | Default | Meaning |
| --- | --- | --- |
| `latitude` / `longitude` / `timezone` | Thousand Oaks, CA | Location for sun times |
| `max_bright` | 100 | Peak brightness (%) |
| `ramp_min` | 30 | Fade-in/out length (minutes) |
| `sunrise_offset_min` / `sunset_offset_min` | 0 | Shift the on/off times (solar mode) |
| `schedule_mode` | `solar` | `solar`, `fixed`, or `duration` |
| `fixed_on` / `fixed_off` | 06:00 / 20:00 | Clock times for fixed mode |
| `duration_hours` / `duration_end` | 15.5 / 21:30 | Day length + lights-off anchor for duration mode |
| `lux_to_ppfd_k` / `canopy_factor` | 60 / 1.0 | Lux-to-PPFD spectrum factor and soil-to-canopy brightness ratio; both feed the DLI numbers |
| `units` | `imperial` | Display only; storage is always Celsius / hPa and switching never converts data |
| `capture_enabled` | false | Timelapse on/off |
| `capture_interval_min` | 30 | Minutes between frames |
| `player_fps` / `video_fps` | 4 / 8 | Timelapse speed in frames per second: the dashboard player, and the rendered video (changing it re-renders; a `timelapse_speed_pct` from an older config is migrated to `video_fps`) |
| `plug_use` | `light` | What the smart plug switches: `light` (the smart plug light backend) or `heat` (the heat mat thermostat) |
| `heat_mode` | `off` | Heat mat: `off`, `auto` (thermostat) or `on`; set with the Heat mat buttons on the Light card |
| `heat_target_f` / `heat_max_f` | 75 / 95 | Soil temperature the mat holds / cut-off that turns it off in any mode; stored in F, shown in the display units |
| `heat_sensor` | `temp:soil` | Soil temperature probe the thermostat reads |
| `capture_set_light` | false | Set the main light to `capture_brightness` for each photo and the focus sweep; off leaves the light as it is |
| `capture_brightness` | 100 | Brightness held during each photo when `capture_set_light` is on |
| `camera_backend` | `usb` | `usb` (v4l2) or `rpicam` |
| `timelapse_flatten` / `cam_rectify` | true / true | Show the snapshot, thumbnails and video flattened / flatten before canopy analysis |
| `cam_rotate` | 0 | Rotation baked in at capture (0/90/180/270) for an upside-down mount |
| `usb_*` | | USB camera controls: device, size, warmup frames, exposure, gain, white balance, focus, and the auto/manual toggle for each |
| `camera_enabled` | true | Master switch: hides all camera UI and pauses the AI report |
| `roi` | "" | View crop as `x,y,w,h` fractions of the stored frame, blank = full frame. Set it with the Crop button on the snapshot. Stored photos stay full; the snapshot, scrubber, video and AI report are cut to it when photos are not flattened |
| `cam_width` / `cam_height` | 2304 / 1296 | Capture resolution at full field of view. The Module 3 sensor is 4608x2592, but a full 12MP capture runs the Pi Zero 2 W out of memory, so the default is the 2304x1296 binned mode (same view, ~3MP). Keep the sensor's 16:9 aspect or the frame gets cropped. Raise to 4608x2592 only on a Pi with more RAM |
| `sample_interval_min` | 5 | Sensor logging interval |
| `auto_water` / `auto_water_trays` | false / [] | Trays armed for automatic watering (`auto_water` is true while any is armed); keep off until each probe is calibrated. An older config with `auto_water` on arms every pump tray once |
| `pump_max_seconds` | 20 | Cap on a single dose |
| `pump_cooldown_min` | 30 | Minimum wait between auto doses |
| `pump_daily_max_seconds` | 180 | Daily runaway backstop |
| `fill_max_seconds` | 60 | Cap on a fill-to-float run if the float never trips |
| `ntfy_topic` | "" | Set to enable ntfy push |
| `discord_webhook` | "" | Set to enable Discord alerts |
| `ai_enabled` | false | Daily AI report on/off |
| `ai_model` | `claude-sonnet-5` | Claude model for the report; any model with vision. A stored `claude-opus-4-8` (the former default) is moved to this on startup |
| `ai_report_hour` / `ai_report_minute` | 8:00 | When the daily report runs |
| `ai_notify` | true | Push the report summary |
| `ai_notes` | (grow description) | Context handed to the AI; list what you planted here to sharpen species guesses |
| `setups` | [] | Grow setups, edited in Settings, Setups: each has a name, a light (`main`, `second` or none), a light sensor (`lux` or `lux:2`), an optional lux-to-PPFD factor for that light, a DLI band, and the sensors that belong to it. Empty means one setup, Main, with everything |
| `dli_target_low` / `dli_target_high` | 10 / 15 | DLI band for the single default setup when `setups` is empty |
| `alert_dli_low` | 4 | Daily light integral floor (mol/m2/day), judged just after lights-off; 0 disables |
| `alerts_enabled` | false | Master switch for threshold alerts |
| `alert_sustain_min` / `alert_cooldown_hours` | 10 / 24 | Sustain window before a rule fires; reminder interval while it persists |
| `soil_temp_low_f` / `soil_temp_high_f` | 80 / 85 | Soil temp band: chart target band and the alert thresholds. Shipped defaults are the chile **germination** band; drop to ~70-80 once seedlings are up, or the low alert nags all day |
| `alert_dry_pct` / `alert_humidity_high` | 15 / 80 | Dry-tray and high-humidity alert thresholds |
| `fan_mode` / `fan_speed` / `fan_auto_speed` / `fan_min_speed` / `fan_humidity_on` | auto / 100 / 70 / 25 / 65 | Fan behaviour: mode, manual and auto speeds, stall floor, humidity trigger |
| `probe_cal` | {} | Per-tray ADC wet/dry anchors for the soil probes, set from the dashboard. A live reading outside its anchors shows a red "recal" badge: the percentage is pegged and the dry alert is blind until the anchor is recaptured |
| `probe_names` | {} | Custom labels for the two soil moisture probes; default "Soil moisture 1" and "Soil moisture 2" (A0 = probe 1 in tray 1, A1 = probe 2 in tray 2). Canopy readings and the watering rows are named after the tray instead. Stored old defaults ("Tray 1", "Tray 2") are cleared on startup |

## Secrets (all gitignored)

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
