"""Settings: DEFAULTS, config.json load and save, startup migrations,
validators, and the shared runtime state and locks."""

import copy
import json
import math
import os
import re
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from applog import log

# ------------------- defaults (overridden by config.json) -------------------
DEFAULTS = {
    "latitude": 34.17,
    "longitude": -118.84,
    "timezone": "America/Los_Angeles",
    "max_bright": 100,  # percent
    "ramp_min": 30,  # minutes
    "light_override": "auto",  # auto = follow the schedule; on|off = manual hold
    "manual_bright": 100,  # brightness held when light_override is "on"
    "fan_mode": "auto",  # auto | on | off
    "fan_speed": 100,  # manual speed held when fan_mode is "on"
    "fan_auto_speed": 70,  # speed used by auto mode
    "fan_min_speed": 25,  # below this a fan often stalls; 0 disables the floor
    "fan_with_light": True,  # auto: run during the photoperiod
    "fan_humidity_on": 65,  # auto: also run above this RH (0 disables)
    "alerts_enabled": True,  # Discord threshold alerts (needs discord_webhook)
    "alert_sustain_min": 10,  # a condition must hold this long before firing
    "alert_cooldown_hours": 24,  # reminder interval while a problem persists
    "alert_dry_pct": 15,  # calibrated trays only
    "alert_humidity_high": 80,  # 0 disables
    # Ceiling counterpart to alert_dli_low: a day finishing above this many
    # mol/m2 means the fixture is turned up too far or hung too close. 0 disables.
    "alert_dli_high": 0,
    # Seedling DLI target band (mol/m2/day): the Day card's DLI bar, the Plan
    # verdict and advice, and the AI report all judge against it. 10-15 is the
    # extension range for vegetable transplants in general; 15-20 is peppers
    # close to transplant (docs/features.md). This is the band for the default
    # setup when no setups are defined; each setup carries its own.
    "dli_target_low": 10.0,
    "dli_target_high": 15.0,
    # Grow setups: separate areas, each with its own light, light sensor, DLI
    # band and a chosen subset of the sensors. Empty = one setup, "Main", with
    # everything. See setups.setups().
    "setups": [],
    # sensors per tray, set in Settings, Setups: {tray id: [sensor keys]}. A
    # tray not listed has the probe and float of its own number (setups.tray_sensors)
    "tray_sensors": {},
    # Checked once daily just after lights-off: a day that finishes under this
    # many mol/m2 means the light was off, dimmed or blocked. 0 disables.
    "alert_dli_low": 4,
    # "imperial" (F, inHg) or "metric" (C, hPa); storage stays Celsius/hPa either way
    "units": "imperial",
    # comfort band drawn on the humidity chart; display only, the alert uses its own setting
    "humidity_low": 40,
    "humidity_high": 60,
    # Sensor plane -> canopy multiplier. The sensor sits at soil level; measure
    # lux at canopy and divide by the soil reading to get this. 1.0 = the sensor
    # is already at canopy height.
    "canopy_factor": 1.0,
    # lux -> PPFD divisor; set for the fixture's spectrum (0 = hide PPFD/DLI).
    # About 60 suits a white-dominant mixed red/blue/white panel.
    "lux_to_ppfd_k": 60,
    # Target band for the soil temp chart and alerts. Chile germination is best
    # at 80-85F; drop the band to about 70-80F once seedlings are up. Either
    # bound at 0 disables that side.
    "soil_temp_high_f": 85,
    "soil_temp_low_f": 80,
    "schedule_mode": "solar",  # solar | fixed | duration
    "fixed_on": "06:00",  # fixed mode: lights on
    "fixed_off": "20:00",  # fixed mode: lights off
    "duration_hours": 14,  # duration mode: day length...
    "duration_end": "20:00",  #   ...anchored to this off time
    "sunrise_offset_min": 0,  # negative starts before sunrise
    "sunset_offset_min": 0,  # positive runs past sunset
    "camera_backend": "rpicam",  # rpicam (CSI ribbon) or usb (UVC webcam)
    # the touchscreen kiosk (Settings, External display; scripts/display_watch.py
    # applies rotation and mode while the kiosk runs)
    "screen_rotate": "",  # "" = as kiosk.sh was installed, else 0/90/180/270
    "screen_mode": "",  # "" = the display's own choice, else WxH or WxH@Hz
    "screen_chart_s": 15,  # summary charts turn this often
    "screen_chart_soil": True,
    "screen_chart_moisture": True,
    "screen_chart_light": True,
    "screen_chart_air": True,
    "screen_back_min": 3,  # the full dashboard returns to the summary after this
    "screen_night": "off",  # while every light is off: "dim" a dark page, "sleep" display off
    "screen_buddy": True,  # Little Buddy on the summary too (when it is on at all)
    # taps on the touchscreen corrected in the page: {"m": [a, b, c, d, e, f],
    # "at": epoch}, x' = a x + b y + c, y' = d x + e y + f (screen_touch.js)
    "screen_touch_cal": {},
    "usb_device": "/dev/video0",
    # a second camera beside the first ("" for none); photos are the two
    # stitched once aligned (Settings, Camera, Align cameras -> cam_stitch)
    "usb_device2": "",
    "cam_stitch": {},
    "usb_width": 2048,
    "usb_height": 1536,
    "usb_warmup_frames": 4,  # frames discarded so exposure settles
    # Auto vs manual per group. Manual is the default because a timelapse
    # wants identical conditions in every frame; auto re-decides each shot and
    # the video flickers.
    "usb_auto_focus": False,
    "usb_focus_absolute": 68,
    "usb_auto_exposure_on": False,
    "usb_exposure_time_absolute": 200,
    "usb_gain": 32,
    "usb_auto_white_balance": False,
    "usb_white_balance_temperature": 4600,
    "cam_rotate": 0,  # 0/90/180/270, applied to captures and analysis
    "cam2_rotate": 0,  # the same for camera 2, which may be mounted differently
    "timelapse_flatten": True,  # show the snapshot, thumbnails and video flattened
    "cam_rectify": True,  # flatten the tray plane before canopy analysis
    # Master switch for all camera features (photos, timelapse, camera vision,
    # AI report). Off until a working camera is connected.
    "camera_enabled": False,
    "capture_enabled": False,
    "capture_interval_min": 30,
    # Timelapse speeds, set separately: the dashboard player steps through
    # thumbnails, the downloaded video is rendered. Frames per second.
    "player_fps": 4,
    "video_fps": 8,
    # Photos are taken under whatever the light is doing. With this on, the
    # main light is set to capture_brightness for the few seconds of each
    # photo (and the focus sweep), so every frame is lit the same.
    "capture_set_light": False,
    # The smart plug does one job: "light" (the smart plug light backend) or
    # "heat" (a heat mat thermostat on the soil temperature, see heat.py).
    "plug_use": "light",
    "heat_mode": "off",  # "off", "auto" (thermostat) or "on"
    "heat_target_f": 75,  # soil temperature the mat holds
    "heat_max_f": 95,  # cut-off: the mat goes off at this, any mode
    "heat_sensor": "temp:soil",  # which soil temperature probe it reads
    "capture_brightness": 100,  # light level held during each photo
    # View crop as "x,y,w,h" fractions of the stored frame, blank = full frame.
    # Applied when photos are shown, thumbnailed, rendered or sent to the AI
    # report; stored frames stay full.
    "roi": "",
    # Capture resolution at full field of view. The Module 3 sensor is
    # 4608x2592, but a full 12MP capture exhausts the Pi Zero 2 W's 512MB RAM,
    # so the default is the 2304x1296 binned mode (same view). Raise to
    # 4608x2592 only on a Pi with more memory.
    "cam_width": 2304,
    "cam_height": 1296,
    # Seconds between re-reads of the quick sensors, for the dashboard only
    # (nothing is written); 0 = off.
    "live_interval_s": 10,
    "sample_interval_min": 5,  # how often to read + log sensors
    "ntfy_topic": "",  # set to enable push notifications (see notify.py)
    "discord_webhook": "",  # set to enable Discord alerts (see discord_alert.py)
    "password_hash": "",  # set to enable login (docs/features.md); blank = open
    "cookie_secure": "auto",  # auto: Secure only when the request came over HTTPS; or true/false
    # Re-capture the wet anchor after a fill that tripped the float, if the
    # reading passes every check a manual capture must.
    "auto_wet_cal": False,
    "auto_wet_cal_max_move": 0.10,  # volts one auto-capture may move it
    # Smoothing depth: how many recent readings the spike/step filter looks at.
    # Applies to every smoothed sensor, not just probes; 1 disables.
    "probe_median_depth": 5,
    "theme": "auto",  # "auto" follows the device, or force light/dark
    "little_buddy": True,  # a decorative character that crosses a card every half minute
    "buddy_model": "sprout",  # which character walks; "random" picks per outing
    "kasa_host": "",  # smart plug IP; set from Settings > Smart plug
    "kasa_user": "",  # TP-Link account, only for KLAP firmware
    "kasa_pass": "",  # redacted from every response, like the dashboard password hash
    # A second light, independently scheduled, on whichever PWM fixture the
    # main light is not.
    "light2_on": False,
    "light2_start": "08:00",
    "light2_end": "20:00",
    "light2_bright": 50,
    "light2_ramp_min": 5,
    "light2_override": "auto",  # "auto" follows its schedule; "on" / "off"
    # What to do when asked for less light than the driver can hold: "hold" its
    # lowest level, or "cycle" on and off to average down to the setting.
    # Cycling is exact for a daily total but visibly blinks.
    "dim_below_min": "hold",
    "light_linear_on": False,  # map dashboard % onto measured light output
    "light_linear": {},  # the table built by a calibration sweep
    # The brightness at which the fixture first lights. 1-100% is mapped onto
    # floor..100 so no part of the scale is dead; 0 is still hard off. Measure
    # it with the light response sweep.
    "light_floor_pct": 0,
    # "pwm" a 5V panel on a MOSFET, "dim" an AC fixture on a 0-10V line through
    # an optocoupler, "kasa" a smart plug (on/off only, the fixture's knob sets
    # intensity).
    "light_backend": "pwm",
    "auto_water": False,  # true while any tray is armed (see auto_water_trays)
    # trays armed for auto-watering; keep them off until each tray's probe is calibrated
    "auto_water_trays": [],
    "moisture_threshold_pct": 30,  # auto-watering fills a tray at or below this probe moisture %
    "pump_max_seconds": 20,  # hard cap on a single dose (anti-flood/dry-run)
    "pump_cooldown_min": 30,  # min wait between auto doses (soil wicks slowly)
    "pump_daily_max_seconds": 180,  # runaway backstop
    "fill_max_seconds": 60,  # hard cap on a fill-to-float run (if float never trips)
    "probe_cal": {},  # per-probe {wet, dry} anchor voltages -> probe moisture %
    # custom probe labels; default "Soil moisture 1/2" (ADS1115 A0 = probe 1 in
    # tray 1, A1 = probe 2)
    "probe_names": {},
    "ai_enabled": False,  # daily Claude vision report (needs an API key, see ai_report.py)
    "ai_model": "claude-sonnet-5",  # any Claude model with vision; config.json only
    "ai_report_hour": 8,  # local hour (0-23) to run the daily report
    "ai_report_minute": 0,  # minute (0-59) within that hour
    "ai_notify": True,  # push the report summary to Discord and ntfy
    "ai_notes": "Peat/vermiculite/perlite seed starter in a cell tray, bottom-watered. "
    "Mixed germination: some cells sprouted, some still germinating.",
    "grid": {  # cell-mapping overlay
        "corners": [[0.12, 0.10], [0.88, 0.10], [0.88, 0.92], [0.12, 0.92]],
        "rows": 4,
        "cols": 4,
        "names": {},
        "show": True,
        "locked": False,
    },
    # What is planted where. Two trays, 3 wide x 4 deep, cells A1..C4.
    # Each cell: {"seed": name, "equipment": what's occupying it, "planted": ISO date}
    "trays": {
        "1": {"label": "Tray 1", "rows": 4, "cols": 3, "cells": {}},
        "2": {"label": "Tray 2", "rows": 4, "cols": 3, "cells": {}},
    },
}
# GROWLIGHT_HTTP_PORT moves the dashboard to another port; tests/test_ui.py
# uses it to run on a spare one.
HTTP_PORT = int(os.environ.get("GROWLIGHT_HTTP_PORT", "5000"))

# The touchscreen on this Pi's own HDMI port (scripts/kiosk.sh) can change
# settings without a login. It has its own listener for that: SCREEN_PORT,
# bound to 127.0.0.1 and opened only while GROWLIGHT_TRUST_LOCALHOST is 1. A
# request is the screen's because of the port it arrived on, so a reverse
# proxy pointed at the main port is not trusted, whatever headers it sends.
TRUST_LOCALHOST = os.environ.get("GROWLIGHT_TRUST_LOCALHOST", "") == "1"
SCREEN_PORT = int(os.environ.get("GROWLIGHT_SCREEN_PORT", "5001"))


# Extra names the dashboard answers to while it has no password, besides its
# IP address, localhost and this machine's hostname: comma-separated, in
# GROWLIGHT_ALLOWED_HOSTS. See auth.check_request.
ALLOWED_HOSTS = {"localhost", socket.gethostname().lower(), socket.gethostname().lower() + ".local"}
ALLOWED_HOSTS |= {
    h.strip().lower() for h in os.environ.get("GROWLIGHT_ALLOWED_HOSTS", "").split(",") if h.strip()
}


def listen_on():
    """The addresses the web server listens on, for waitress's `listen`."""
    listen = [f"0.0.0.0:{HTTP_PORT}"]
    if TRUST_LOCALHOST:
        listen.append(f"127.0.0.1:{SCREEN_PORT}")
    return " ".join(listen)


CONFIG_PATH = Path(__file__).with_name("config.json")
TIMEZONES = sorted(available_timezones())

settings = copy.deepcopy(DEFAULTS)
_file_keys = set()
CONFIG_BAK = CONFIG_PATH.with_name("config.json.bak")
# Set when config.json exists but neither it nor its backup could be read.
# The app then runs on defaults to keep the lights and loops going, but it
# must not save (that would overwrite the real settings with defaults) and
# must not accept changes (the defaults have no password, so the dashboard
# would be open to anyone who can reach it). auth.check_request refuses changes while it is set.
config_broken = ""


def atomic_write_text(path, text):
    """Write a file so that it holds either the old contents or the new, never
    half of either, through a power cut or a full disk mid-write."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_json(path):
    saved = json.loads(Path(path).read_text())
    if not isinstance(saved, dict):
        raise ValueError("not a JSON object")
    return saved


if CONFIG_PATH.exists():
    _saved, _why = None, ""
    for _p in (CONFIG_PATH, CONFIG_BAK):
        try:
            _saved = _load_json(_p)
            if _p is CONFIG_BAK:
                log.error(f"config.json unreadable ({_why}); loaded config.json.bak instead")
            break
        except FileNotFoundError:
            continue
        except Exception as e:
            _why = _why or str(e)
    if _saved is None:
        config_broken = (
            f"config.json could not be read ({_why}) and there is no readable "
            "backup; running on defaults with changes refused. Restore it "
            "(~/growlight-backups/ keeps copies) and restart."
        )
        log.error(config_broken)
    else:
        # keys starting with "_" are runtime state (e.g. _seen_sensors); loaded
        # back from a file, a removed sensor would alert forever
        _saved = {k: v for k, v in _saved.items() if not k.startswith("_")}
        _file_keys = set(_saved)
        settings.update(_saved)


# Migration: timelapse_speed_pct is one speed for the player and the video, as
# a percentage (100 = player 8 frames/s, video 24). The video keeps its speed;
# the player takes its own default.
if "timelapse_speed_pct" in settings:
    _pct = settings.pop("timelapse_speed_pct")
    if "video_fps" not in _file_keys:
        try:
            settings["video_fps"] = round(24 * float(_pct) / 100, 2)
        except (TypeError, ValueError):
            pass


def save_config():
    """Persist settings to config.json. Caller must hold settings_lock.

    Keys starting with "_" are in-memory state and are not written. The write
    is atomic, and the previous good file is kept as config.json.bak. Nothing
    is written when config.json was unreadable at start."""
    if config_broken:
        log.error("not saving settings: config.json was unreadable at start")
        return
    data = {k: v for k, v in settings.items() if not k.startswith("_")}
    if CONFIG_PATH.exists():
        try:
            _load_json(CONFIG_PATH)  # only a good file becomes the backup
            atomic_write_text(CONFIG_BAK, CONFIG_PATH.read_text())
        except Exception:
            pass
    atomic_write_text(CONFIG_PATH, json.dumps(data, indent=2))


# Migration: a saved tray laid out 4 wide x 3 deep (A1..D3) becomes 3 wide x
# 4 deep (A1..C4), the shape of the physical trays. Cells are transposed so
# each entry stays on the same physical spot.
def _migrate_trays():
    changed = False
    for tid, t in (settings.get("trays") or {}).items():
        if t.get("rows") == 3 and t.get("cols") == 4:
            newcells = {}
            for cid, v in (t.get("cells") or {}).items():
                m = re.fullmatch(r"([A-D])([1-3])", str(cid))
                if m:
                    col = ord(m.group(1)) - 65  # old column 0-3
                    row = int(m.group(2)) - 1  # old row 0-2
                    newcells[f"{chr(65 + row)}{col + 1}"] = v  # transpose
                else:
                    newcells[cid] = v
            t.update(rows=4, cols=3, cells=newcells)
            changed = True
            if newcells:
                log.info(f"tray {tid}: migrated {len(newcells)} cells to 3x4 layout")
    if changed:
        try:
            save_config()
        except Exception as e:
            log.warning(f"tray migration not persisted ({e})")


_migrate_trays()

# Migration: a stored probe name of "Tray N" for probe N is an old default.
# Drop it so the current default name applies; a name someone chose stays.
_pn = settings.get("probe_names") or {}
_old = {t: n for t, n in _pn.items() if n == f"Tray {t}"}
if _old:
    settings["probe_names"] = {t: n for t, n in _pn.items() if t not in _old}
    try:
        save_config()
    except Exception as e:
        log.warning(f"probe name update not persisted ({e})")


# Migration: config.json stores every key, including defaults nobody chose. A
# stored ai_model that is a retired default moves to the current default; any
# other model stays.
_RETIRED_AI_DEFAULTS = ("claude-opus-4-8",)
if settings.get("ai_model") in _RETIRED_AI_DEFAULTS:
    log.info(
        f"AI report model: {settings['ai_model']} -> {DEFAULTS['ai_model']} "
        "(former default; set ai_model in config.json to choose another)"
    )
    settings["ai_model"] = DEFAULTS["ai_model"]
    try:
        save_config()
    except Exception as e:
        log.warning(f"AI model change not persisted ({e})")

# Migration: alert_soil_low_f is an older, alerts-only key for the soil low
# bound. Its value becomes soil_temp_low_f, which the chart and the alerts
# share, unless the file sets that itself. Checked against the file's own
# keys, since DEFAULTS always supplies soil_temp_low_f.
if "alert_soil_low_f" in settings:
    _old = settings.pop("alert_soil_low_f")
    if _file_keys and "soil_temp_low_f" not in _file_keys:
        settings["soil_temp_low_f"] = _old
    try:
        save_config()
    except Exception as e:
        # logged as an error because the setting looks saved and is not
        log.error(f"settings not written to config.json: {e}")


settings_lock = threading.Lock()
wake = threading.Event()

state = {"brightness": 0.0, "on": None, "off": None, "sunrise": None, "sunset": None}
state_lock = threading.Lock()


def _today_str():
    return datetime.now(ZoneInfo(settings["timezone"])).date().isoformat()


# ----------------------- per-field settings validation -----------------------
# Each validator returns the cleaned value or raises ValueError with a short,
# user-facing reason. routes.update_settings applies every valid field and
# reports the invalid ones by name, so one bad field does not discard the
# other changes in a save.


def _v_bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _number(v):
    """v as a finite float; blank counts as 0. NaN and infinity are refused:
    they pass every range comparison and cannot be written as JSON."""
    try:
        n = float(v if v not in (None, "") else 0)
    except (TypeError, ValueError):
        raise ValueError("must be a number") from None
    if not math.isfinite(n):
        raise ValueError("must be a finite number")
    return n


def _v_int(lo, hi, clamp=True):
    def f(v):
        n = int(_number(v))
        if clamp:
            return max(lo, min(hi, n))
        if not lo <= n <= hi:
            raise ValueError(f"must be {lo} to {hi}")
        return n

    return f


def _v_float(lo, hi, clamp=True):
    def f(v):
        n = _number(v)
        if clamp:
            return max(lo, min(hi, n))
        if not lo <= n <= hi:
            raise ValueError(f"must be {lo} to {hi}")
        return n

    return f


def _v_choice(*opts):
    def f(v):
        if v not in opts:
            raise ValueError("must be one of " + ", ".join(map(str, opts)))
        return v

    return f


def _v_hhmm(v):
    v = str(v or "").strip()
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", v):
        raise ValueError("must be HH:MM")
    return v


def _v_timezone(v):
    v = str(v or "").strip()
    try:
        ZoneInfo(v)
    except Exception:
        raise ValueError("unknown timezone; use an IANA name like America/Los_Angeles") from None
    return v


def parse_roi(s):
    """A crop as (x, y, w, h) fractions of the frame, from "x,y,w,h"; None for
    blank. Raises ValueError when it is not four numbers inside the frame."""
    s = (s or "").strip()
    if not s:
        return None
    parts = [float(p) for p in s.split(",")]
    if len(parts) != 4:
        raise ValueError("need four numbers")
    x, y, w, h = parts
    if not (
        0 <= x < 1
        and 0 <= y < 1
        and 0.05 <= w <= 1
        and 0.05 <= h <= 1
        and x + w <= 1.001
        and y + h <= 1.001
    ):
        raise ValueError("out of range")
    return x, y, w, h


def _v_roi(v):
    v = str(v or "").strip()
    try:
        parse_roi(v)
    except ValueError:
        raise ValueError("must be 'x,y,w,h' as fractions 0-1, or blank") from None
    return v


def _v_host(v):
    """An IP address or hostname, or blank. Strict, because this string is
    handed to the plug library, where a stray scheme or port produces a
    confusing connection error."""
    v = str(v or "").strip()
    if not v:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", v):
        raise ValueError("must be an IP address or hostname, with no scheme or port")
    return v


# A camera by number (/dev/video0) or by the USB port it is plugged into
# (/dev/v4l/by-path/...). Two cameras of the same model can swap numbers at
# boot and share one by-id name, so a pair is best set by port.
_DEV_RE = r"/dev/video\d+|/dev/v4l/by-(path|id)/[\w.:+-]+"


def _v_usb_device(v):
    v = str(v or "").strip()
    if not re.fullmatch(_DEV_RE, v):
        raise ValueError("must look like /dev/video0 or /dev/v4l/by-path/...")
    return v


def _v_screen_mode(v):
    v = str(v or "").strip()
    if v and not re.fullmatch(r"\d{3,5}x\d{3,5}(@\d{2,3}(\.\d+)?)?", v):
        raise ValueError("must look like 1024x600 or 1024x600@60, or blank")
    return v


def _v_usb_device2(v):
    v = str(v or "").strip()
    return _v_usb_device(v) if v else ""


def _v_heat_sensor(v):
    v = str(v or "").strip()
    if not re.fullmatch(r"temp:soil(_[0-9A-Za-z]+)?", v):
        raise ValueError("pick a soil temperature probe")
    return v


def _v_tray_sensors(v):
    """{tray id: [sensor keys]}: which sensors sit in which tray. Watering reads
    a tray's one moisture probe and stops on its one float, so each tray has at
    most one of each and no probe or float is in two trays. Anything else (a
    soil temperature probe, the air sensor) may be shared."""
    if not isinstance(v, dict) or len(v) > 32:
        raise ValueError("must map tray ids to lists of sensors")
    out, owner = {}, {}
    for tid, keys in v.items():
        tid = str(tid)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", tid):
            raise ValueError(f"{tid!r} is not a tray id")
        if not isinstance(keys, list) or len(keys) > 32:
            raise ValueError(f"tray {tid}: sensors must be a list")
        clean = []
        for k in keys:
            if not (isinstance(k, str) and re.fullmatch(r"[a-z_]+(:[A-Za-z0-9_]+)?", k)):
                raise ValueError(f"tray {tid}: {k!r} is not a sensor")
            # canopy follows the camera's grid, the reservoir and light sensor
            # their setup: not a tray's to claim
            if re.match(r"(canopy|reservoir):|lux(:|$)|sys:|heat:", k) or k in clean:
                continue
            clean.append(k)
        for kind, word in (("probe:", "moisture probe"), ("float:", "float")):
            mine = [k for k in clean if k.startswith(kind)]
            if len(mine) > 1:
                raise ValueError(f"tray {tid}: one {word} per tray (watering reads one)")
            for k in mine:
                if k in owner:
                    raise ValueError(f"{k} is in tray {owner[k]} and tray {tid}; pick one")
                owner[k] = tid
        out[tid] = sorted(clean)
    return out


def _v_trimmed(v):
    return str(v or "").strip()[:200]


def _v_secret(v):
    return str(v or "")[:200]


# One definition per field on the Settings form: its kind, the range the
# server accepts, and the step the form offers. The server's validators for
# these keys, the form's min/max/step attributes (routes.form_attrs) and the
# page's save and fill code (static/js/photos.js, from the copy of this table
# embedded in the page) are all generated from it, so they cannot disagree
# about a field. Kinds: int, float, bool, choice, time, text (with a check
# function), secret (blank keeps the stored value), tempF (stored in F, shown
# in the display units; zero_off: 0 means off and is never converted).
FORM = {
    "light_backend": {"kind": "choice", "choices": ["pwm", "dim", "kasa"]},
    "max_bright": {"kind": "int", "min": 1, "max": 100, "clamp": False},
    "light_floor_pct": {"kind": "float", "min": 0, "max": 50, "step": "0.1"},
    "dim_below_min": {"kind": "choice", "choices": ["hold", "cycle"]},
    "light_linear_on": {"kind": "bool"},
    "ramp_min": {"kind": "int", "min": 0, "max": 240, "clamp": False},
    "schedule_mode": {"kind": "choice", "choices": ["solar", "fixed", "duration"]},
    "sunrise_offset_min": {"kind": "int", "min": -720, "max": 720},
    "sunset_offset_min": {"kind": "int", "min": -720, "max": 720},
    "fixed_on": {"kind": "time"},
    "fixed_off": {"kind": "time"},
    "duration_hours": {"kind": "float", "min": 0.0, "max": 24.0, "step": "0.25"},
    "duration_end": {"kind": "time"},
    "light2_on": {"kind": "bool"},
    "light2_override": {"kind": "choice", "choices": ["auto", "on", "off"]},
    "light2_start": {"kind": "time"},
    "light2_end": {"kind": "time"},
    "light2_bright": {"kind": "int", "min": 0, "max": 100},
    "light2_ramp_min": {"kind": "int", "min": 0, "max": 120},
    "lux_to_ppfd_k": {"kind": "float", "min": 0.0, "max": 200.0, "step": "1"},
    "canopy_factor": {"kind": "float", "min": 0.1, "max": 10.0, "step": "0.05"},
    "heat_target_f": {
        "kind": "tempF",
        "min": 40,
        "max": 105,
        "clamp": False,
        "round": 0.1,
        "step": "0.5",
    },
    "heat_max_f": {
        "kind": "tempF",
        "min": 50,
        "max": 120,
        "clamp": False,
        "round": 0.1,
        "step": "0.5",
    },
    "heat_sensor": {"kind": "text", "check": "_v_heat_sensor"},
    "fan_humidity_on": {"kind": "int", "min": 0, "max": 100},
    "fan_min_speed": {"kind": "int", "min": 0, "max": 100},
    "fan_with_light": {"kind": "bool"},
    "soil_temp_low_f": {"kind": "tempF", "min": 0, "max": 150, "round": 1, "zero_off": True},
    "soil_temp_high_f": {"kind": "tempF", "min": 0, "max": 150, "round": 1, "zero_off": True},
    "humidity_low": {"kind": "int", "min": 0, "max": 100},
    "humidity_high": {"kind": "int", "min": 0, "max": 100},
    "moisture_threshold_pct": {"kind": "int", "min": 1, "max": 90},
    "pump_cooldown_min": {"kind": "int", "min": 1, "max": 1440},
    "fill_max_seconds": {"kind": "int", "min": 1, "max": 600},
    "pump_daily_max_seconds": {"kind": "int", "min": 1, "max": 3600},
    "pump_max_seconds": {"kind": "int", "min": 1, "max": 120},
    "probe_median_depth": {"kind": "int", "min": 1, "max": 15},
    "auto_wet_cal": {"kind": "bool"},
    "auto_wet_cal_max_move": {"kind": "float", "min": 0.01, "max": 1.0, "step": "0.01"},
    "camera_backend": {"kind": "choice", "choices": ["rpicam", "usb"]},
    "screen_rotate": {"kind": "choice", "choices": ["", "0", "90", "180", "270"]},
    "screen_mode": {"kind": "text", "check": "_v_screen_mode"},
    "screen_chart_s": {"kind": "int", "min": 5, "max": 600},
    "screen_chart_soil": {"kind": "bool"},
    "screen_chart_moisture": {"kind": "bool"},
    "screen_chart_light": {"kind": "bool"},
    "screen_chart_air": {"kind": "bool"},
    "screen_back_min": {"kind": "int", "min": 1, "max": 60},
    "screen_night": {"kind": "choice", "choices": ["off", "dim", "sleep"]},
    "screen_buddy": {"kind": "bool"},
    "usb_device": {"kind": "text", "check": "_v_usb_device"},
    "usb_device2": {"kind": "text", "check": "_v_usb_device2"},
    "usb_width": {"kind": "int", "min": 160, "max": 4096},
    "usb_height": {"kind": "int", "min": 120, "max": 4096},
    "usb_auto_focus": {"kind": "bool"},
    "usb_focus_absolute": {"kind": "int", "min": 0, "max": 1023},
    "usb_auto_exposure_on": {"kind": "bool"},
    "usb_exposure_time_absolute": {"kind": "int", "min": 1, "max": 100000},
    "usb_gain": {"kind": "int", "min": 0, "max": 255},
    "usb_auto_white_balance": {"kind": "bool"},
    "usb_white_balance_temperature": {"kind": "int", "min": 1000, "max": 10000},
    "camera_enabled": {"kind": "bool"},
    "capture_enabled": {"kind": "bool"},
    "capture_interval_min": {"kind": "int", "min": 5, "max": 720, "clamp": False},
    "player_fps": {"kind": "float", "min": 0.5, "max": 30, "clamp": False, "step": "any"},
    "video_fps": {"kind": "float", "min": 1, "max": 60, "clamp": False, "step": "any"},
    "capture_set_light": {"kind": "bool"},
    "capture_brightness": {"kind": "int", "min": 1, "max": 100, "clamp": False},
    "roi": {"kind": "text", "check": "_v_roi"},
    "cam_rotate": {"kind": "choice", "choices": [0, 90, 180, 270], "ints": True},
    "cam2_rotate": {"kind": "choice", "choices": [0, 90, 180, 270], "ints": True},
    "timelapse_flatten": {"kind": "bool"},
    "cam_rectify": {"kind": "bool"},
    "alerts_enabled": {"kind": "bool"},
    "alert_sustain_min": {"kind": "int", "min": 1, "max": 120},
    "alert_cooldown_hours": {"kind": "int", "min": 1, "max": 72},
    "alert_dry_pct": {"kind": "int", "min": 0, "max": 90},
    "alert_humidity_high": {"kind": "int", "min": 0, "max": 100},
    "alert_dli_low": {"kind": "float", "min": 0, "max": 30, "step": "0.5"},
    "alert_dli_high": {"kind": "float", "min": 0, "max": 80, "step": "0.5"},
    "latitude": {"kind": "float", "min": -90, "max": 90, "clamp": False, "step": "0.0001"},
    "longitude": {"kind": "float", "min": -180, "max": 180, "clamp": False, "step": "0.0001"},
    "timezone": {"kind": "text", "check": "_v_timezone"},
    "plug_use": {"kind": "choice", "choices": ["light", "heat"]},
    "kasa_host": {"kind": "text", "check": "_v_host"},
    # never sent to the page (status.SECRET_SETTINGS), so the form shows it
    # blank; a blank field must keep the stored value, not overwrite it
    "kasa_user": {"kind": "secret", "check": "_v_trimmed"},
    "kasa_pass": {"kind": "secret", "check": "_v_secret"},
    "little_buddy": {"kind": "bool"},
    "buddy_model": {
        "kind": "choice",
        "choices": [
            "sprout",
            "pepper",
            "cat",
            "snail",
            "ladybug",
            "drop",
            "bee",
            "gnome",
            "random",
        ],
    },
    "units": {"kind": "choice", "choices": ["imperial", "metric"]},
}


def _form_validator(spec):
    kind = spec["kind"]
    if kind in ("int", "float", "tempF"):
        whole = kind == "int" or (kind == "tempF" and spec.get("round") == 1)
        lo, hi = spec["min"], spec["max"]
        if whole:
            return _v_int(int(lo), int(hi), clamp=spec.get("clamp", True))
        return _v_float(lo, hi, clamp=spec.get("clamp", True))
    if kind == "bool":
        return _v_bool
    if kind == "choice":
        return _v_choice(*spec["choices"])
    if kind == "time":
        return _v_hhmm
    return globals()[spec["check"]]


def _v_touch_cal(v):
    """{} or {"m": six numbers, "at": time}: the touch correction worked out by
    the touchscreen's own calibration (hold a finger on it for 3 s)."""
    if not v:
        return {}
    if not isinstance(v, dict) or not isinstance(v.get("m"), list) or len(v["m"]) != 6:
        raise ValueError("must be {} or {m: [a, b, c, d, e, f]}")
    try:
        m = [float(x) for x in v["m"]]
    except (TypeError, ValueError):
        raise ValueError("the six values must be numbers") from None
    if not all(math.isfinite(x) for x in m):
        raise ValueError("the six values must be finite numbers")
    # a squeezed mapping needs a large factor to undo it (taps on the rig
    # arrived within about a twentieth of the screen's height)
    if any(abs(x) > 200 for x in (m[0], m[1], m[3], m[4])) or any(
        abs(x) > 200000 for x in (m[2], m[5])
    ):
        raise ValueError("those values would throw taps far off the screen")
    try:
        at = int(v.get("at") or time.time())
    except (TypeError, ValueError):
        at = int(time.time())
    return {"m": [round(x, 6) for x in m], "at": at}


SETTINGS_VALIDATORS = {
    "screen_touch_cal": _v_touch_cal,
    "usb_warmup_frames": _v_int(1, 20),
    "live_interval_s": _v_int(0, 120),
    "heat_mode": _v_choice("off", "auto", "on"),
    "tray_sensors": _v_tray_sensors,
    "dli_target_low": _v_float(0.5, 60, clamp=False),
    "dli_target_high": _v_float(1, 65, clamp=False),
    "fan_mode": _v_choice("auto", "on", "off"),
    "fan_speed": _v_int(0, 100),
    "fan_auto_speed": _v_int(0, 100),
    "theme": _v_choice("auto", "light", "dark"),
}
SETTINGS_VALIDATORS.update({k: _form_validator(s) for k, s in FORM.items()})

LOAD_CHECKED_KINDS = ("int", "float", "tempF", "bool", "choice", "time")


def check_loaded(loaded, keys):
    """Put saved form settings through the validators a POST would meet.

    config.json can be edited by hand, and a wrong type there (a number in
    quotes, a mode that does not exist) would otherwise surface later as an
    exception inside a control loop. A value that fails is replaced in
    `loaded` by its default and reported; the file itself is left alone.
    Blank values are skipped: for several fields "not set" is a choice.
    """
    bad = []
    for key in sorted(keys):
        if FORM.get(key, {}).get("kind") not in LOAD_CHECKED_KINDS:
            continue
        if loaded.get(key) in (None, ""):
            continue
        try:
            loaded[key] = SETTINGS_VALIDATORS[key](loaded[key])
        except (ValueError, TypeError) as e:
            log.error(
                f"config.json: {key} = {loaded[key]!r} is not valid ({e}); "
                f"using the default, {DEFAULTS.get(key)!r}"
            )
            loaded[key] = copy.deepcopy(DEFAULTS.get(key))
            bad.append(key)
    return bad


check_loaded(settings, _file_keys)


def form_spec():
    """FORM for the page, each field with its default (the page fills a
    missing value from it, and saves it for a field left blank)."""
    return {k: dict(s, default=DEFAULTS.get(k)) for k, s in FORM.items()}
