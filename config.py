"""Settings: DEFAULTS, config.json load and save, startup migrations,
validators, and the shared runtime state and locks."""

import json
import os
import re
import threading
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
    "light_override": "auto",  # auto = follow the sun schedule; on|off = manual hold
    "manual_bright": 100,  # brightness held when light_override is "on"
    "fan_mode": "auto",  # auto | on | off
    "fan_speed": 100,  # manual speed held when fan_mode is "on"
    "fan_auto_speed": 70,  # speed used by auto mode
    "fan_min_speed": 25,  # below this a fan often stalls; 0 disables the floor
    "fan_with_light": True,  # auto: run during the photoperiod
    "fan_humidity_on": 65,  # auto: also run above this RH (0 disables)
    "alerts_enabled": True,  # Discord threshold alerts (needs discord_webhook)
    "alert_sustain_min": 10,  # a condition must hold this long before firing
    "alert_cooldown_hours": 6,  # reminder interval while a problem persists
    "alert_dry_pct": 15,  # calibrated trays only
    "alert_humidity_high": 80,  # 0 disables
    "alert_dli_high": 0,  # ceiling counterpart to alert_dli_low: a day
    #   finishing above this many mol means the
    #   fixture is turned up too far or hung too
    #   close. 0 disables (default, since it only
    #   makes sense once a target is chosen).
    # Seedling DLI target band (mol/m2/day): the Day card's DLI bar, the Plan
    # verdict and advice, and the AI report all judge against it. 10-15 is the
    # extension range for vegetable transplants in general; 15-20 is peppers
    # close to transplant. See the README.
    "dli_target_low": 10.0,  # band for the default setup when no setups are
    "dli_target_high": 15.0,  # defined; each setup carries its own band
    # Grow setups: separate areas, each with its own light, light sensor, DLI
    # band and a chosen subset of the sensors. Empty = one setup, "Main", with
    # everything. See setups().
    "setups": [],
    "alert_dli_low": 4,  # checked once daily just after lights-off:
    #   a day that finishes under this many mol/m2
    #   means the light was off, dimmed or blocked.
    #   0 disables.
    "units": "imperial",  # "imperial" (F, inHg) or "metric" (C, hPa);
    #   storage stays Celsius/hPa either way
    "humidity_low": 40,  # comfort band drawn on the humidity chart;
    "humidity_high": 60,  #   display only, the alert uses its own setting
    "canopy_factor": 1.0,  # sensor plane -> canopy multiplier. The sensor
    #   sits at soil level; measure lux at canopy and
    #   divide by the soil reading to get this. 1.0 =
    #   sensor is already at canopy height.
    "lux_to_ppfd_k": 60,  # lux -> PPFD divisor; set for your fixture's
    #   spectrum (0 = hide PPFD/DLI). ~60 suits a
    #   white-dominant mixed red/blue/white panel.
    "soil_temp_high_f": 85,  # target band for the soil temp chart and alerts.
    "soil_temp_low_f": 80,  #   Chile germination is best 80-85F; drop the
    #   band to about 70-80F once seedlings are up.
    #   Either bound at 0 disables that side.
    "schedule_mode": "solar",  # solar | fixed | duration
    "fixed_on": "06:00",  # fixed mode: lights on
    "fixed_off": "20:00",  # fixed mode: lights off
    "duration_hours": 14,  # duration mode: day length...
    "duration_end": "20:00",  #   ...anchored to this off time
    "sunrise_offset_min": 0,  # negative starts before sunrise
    "sunset_offset_min": 0,  # positive runs past sunset
    "camera_backend": "rpicam",  # rpicam (CSI ribbon) or usb (UVC webcam)
    "usb_device": "/dev/video0",
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
    "timelapse_flatten": True,  # show the snapshot, thumbnails and video flattened
    "cam_rectify": True,  # flatten the tray plane before canopy analysis
    "camera_enabled": False,  # master switch for all camera features (photos,
    #   timelapse, camera vision, AI report). Off until
    #   a working camera is connected.
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
    "heat_target_f": 75,  # soil temperature the mat holds (on 1F below)
    "heat_max_f": 95,  # cut-off: the mat goes off at this, any mode
    "heat_sensor": "temp:soil",  # which soil temperature probe it reads
    "capture_brightness": 100,  # light level held during each photo
    "roi": "",  # view crop as "x,y,w,h" fractions of the stored
    # frame, blank = full frame. Applied when photos
    # are shown, thumbnailed, rendered or sent to the
    # AI report; stored frames stay full.
    "cam_width": 2304,  # capture resolution at full field of view. The
    "cam_height": 1296,  #   Module 3 sensor is 4608x2592, but a full 12MP
    #   capture exhausts the Pi Zero 2 W's 512MB RAM, so
    #   default to the 2304x1296 binned mode (same view).
    #   Raise to 4608x2592 only on a Pi with more memory
    "live_interval_s": 10,  # how often the quick sensors are re-read for
    # the dashboard only, nothing written; 0 off
    "sample_interval_min": 5,  # how often to read + log sensors
    "ntfy_topic": "",  # set to enable push notifications (see notify.py)
    "discord_webhook": "",  # set to enable Discord alerts (see discord_alert.py)
    "password_hash": "",  # set to enable login (see README); blank = open
    "cookie_secure": "auto",  # auto: Secure only when the request came over HTTPS; or true/false
    "auto_wet_cal": False,  # re-capture the wet anchor after a fill that
    # actually tripped the float, if the reading
    # passes every check a manual capture must
    "auto_wet_cal_max_move": 0.10,  # volts one auto-capture may move it
    "probe_median_depth": 5,  # smoothing depth: how many recent readings the
    # spike/step filter looks at. Applies to every
    # smoothed sensor, not just probes; 1 disables
    "theme": "auto",  # "auto" follows the device, or force light/dark
    "little_buddy": True,  # the character that wanders across a card every
    # half minute. Purely decorative; off by choice.
    "buddy_model": "sprout",  # which character walks; "random" picks per outing
    "kasa_host": "",  # smart plug IP; set from Settings > Smart plug
    "kasa_user": "",  # TP-Link account, only for KLAP firmware
    "kasa_pass": "",  # ...redacted from every response, like the
    #    dashboard password hash
    "light2_on": False,  # a second light, independently scheduled, on
    # whichever PWM fixture the main light is not
    "light2_start": "08:00",
    "light2_end": "20:00",
    "light2_bright": 50,
    "light2_ramp_min": 5,
    "light2_override": "auto",  # "auto" follows its schedule; "on" / "off"
    "dim_below_min": "hold",  # what to do when asked for less light than
    # the driver can hold: "hold" its lowest
    # level, or "cycle" on and off to average
    # down to the setting. Cycling is exact for
    # a daily total but visibly blinks.
    "light_linear_on": False,  # map dashboard % onto measured light output
    "light_linear": {},  # the table built by a calibration sweep
    "light_floor_pct": 0,  # the brightness at which the fixture first
    # lights. 1-100% is mapped onto floor..100
    # so no part of the scale is dead; 0 is
    # still hard off. Measure it with the
    # light response sweep.
    "light_backend": "pwm",  # "pwm" 5V panel on a MOSFET, "dim" an AC
    # fixture on a 0-10V line through an
    # optocoupler, "kasa" a smart
    # plug (on/off only, fixture knob sets intensity)
    "auto_water": False,  # true while any tray is armed (see auto_water_trays)
    "auto_water_trays": [],  # trays armed for auto-watering; keep them off
    # until each tray's probe is calibrated
    "moisture_threshold_pct": 30,  # CALIBRATION TODO: "dry" trigger, per-probe
    "pump_max_seconds": 20,  # hard cap on a single dose (anti-flood/dry-run)
    "pump_cooldown_min": 30,  # min wait between auto doses (soil wicks slowly)
    "pump_daily_max_seconds": 180,  # runaway backstop
    "fill_max_seconds": 60,  # hard cap on a fill-to-float run (if float never trips)
    "probe_cal": {},  # per-tray {wet,dry} raw ADC anchors -> probe moisture %
    "probe_names": {},  # custom probe labels; default "Soil moisture 1/2"
    # (ADS1115 A0 = probe 1 in tray 1, A1 = probe 2)
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
    # What is actually planted where. Two trays, 3 wide x 4 deep, cells A1..C4.
    # Each cell: {"seed": name, "equipment": what's occupying it, "planted": ISO date}
    "trays": {
        "1": {"label": "Tray 1", "rows": 4, "cols": 3, "cells": {}},
        "2": {"label": "Tray 2", "rows": 4, "cols": 3, "cells": {}},
    },
}
HTTP_PORT = int(
    os.environ.get("GROWLIGHT_HTTP_PORT", "5000")
)  # tests/test_ui.py runs on a spare port

# Set by scripts/kiosk.sh for the touchscreen on this Pi's own HDMI port:
# requests from 127.0.0.1 count as signed in, so the screen can change
# settings without a login. Only a process on the Pi itself can connect from
# 127.0.0.1 (the app reads the TCP peer, never X-Forwarded-For), so the public
# site is unaffected. Do not set it if a reverse proxy runs on this Pi.
TRUST_LOCALHOST = os.environ.get("GROWLIGHT_TRUST_LOCALHOST", "") == "1"
CONFIG_PATH = Path(__file__).with_name("config.json")
TIMEZONES = sorted(available_timezones())

settings = dict(DEFAULTS)
_file_keys = set()
CONFIG_BAK = CONFIG_PATH.with_name("config.json.bak")
# Set when config.json exists but neither it nor its backup could be read.
# The app then runs on defaults to keep the lights and loops going, but it
# must not save (that would overwrite the real settings with defaults) and
# must not accept changes (the defaults have no password, so the dashboard
# would be open to anyone who can reach it). routes.require_auth checks it.
config_broken = ""


def atomic_write_text(path, text):
    """Write a file so it is either the old contents or the new, never half:
    a power cut or a full disk mid-write used to be able to leave config.json
    truncated, and a truncated config.json meant defaults on the next start."""
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
        # keys starting with "_" are runtime state that older builds persisted
        # by accident (e.g. _seen_sensors); loading them back made a removed
        # sensor alert forever
        _saved = {k: v for k, v in _saved.items() if not k.startswith("_")}
        _file_keys = set(_saved)
        settings.update(_saved)


# Update 24 had one speed for both, as a percentage (100 = player 8 frames/s,
# video 24). Carry the video's over; the player gets its own default (Ben
# found the shared 33% too slow on the page).
if "timelapse_speed_pct" in settings:
    _pct = settings.pop("timelapse_speed_pct")
    if "video_fps" not in _file_keys:
        try:
            settings["video_fps"] = round(24 * float(_pct) / 100, 2)
        except (TypeError, ValueError):
            pass


def save_config():
    """Persist settings to config.json. Caller must hold settings_lock.

    The single place config is written, so the no-runtime-keys rule cannot be
    forgotten at one of a dozen call sites: anything starting with "_" is
    in-memory state and never lands on disk. Written atomically, and the
    previous good file is kept as config.json.bak."""
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


# One-time migration: trays were first laid out 4 wide x 3 deep (A1..D3); the
# physical trays are 3 wide x 4 deep (A1..C4). Transpose saved cells so each
# entry stays on the same physical spot, then persist the new shape.
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

# The probes were once labeled by tray ("Tray 1"), which read as the tray
# itself and went stale when a tray was renamed. Drop those stored old
# defaults so the new default name applies; a name someone chose stays.
_pn = settings.get("probe_names") or {}
_old = {t: n for t, n in _pn.items() if n == f"Tray {t}"}
if _old:
    settings["probe_names"] = {t: n for t, n in _pn.items() if t not in _old}
    try:
        save_config()
    except Exception as e:
        log.warning(f"probe name update not persisted ({e})")


# config.json stores every key, so the old default model was written into it
# and would stay forever. Move that one value (and only that value) to the
# current default; a model chosen deliberately is left alone.
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

# the soil low bound used to live under an alerts-only key; adopt it once so
# the chart and the alerts can never disagree. Checked against the file's own
# keys, since DEFAULTS always supplies soil_temp_low_f after the merge.
if "alert_soil_low_f" in settings:
    _old = settings.pop("alert_soil_low_f")
    if _file_keys and "soil_temp_low_f" not in _file_keys:
        settings["soil_temp_low_f"] = _old
    try:
        save_config()
    except Exception as e:
        # a setting that looks saved and is not is worth saying out loud
        log.error(f"settings not written to config.json: {e}")


settings_lock = threading.Lock()
wake = threading.Event()

state = {"brightness": 0.0, "on": None, "off": None, "sunrise": None, "sunset": None}
state_lock = threading.Lock()


# --------------------------- control loop ---------------------------


def _today_str():
    return datetime.now(ZoneInfo(settings["timezone"])).date().isoformat()


# ---- per-field settings validation ----
# Each validator returns the cleaned value or raises ValueError with a short,
# user-facing reason. update_settings applies every valid field and reports
# the invalid ones by name: the old all-or-nothing form silently discarded a
# whole save when one field was bad, which twice shipped stale settings
# (cam_rotate stuck at 0, soil band stuck at 80-85).


def _v_bool(v):
    return bool(v)


def _v_int(lo, hi, clamp=True):
    def f(v):
        try:
            n = int(float(v if v not in (None, "") else 0))
        except (TypeError, ValueError):
            raise ValueError("must be a number")
        if clamp:
            return max(lo, min(hi, n))
        if not lo <= n <= hi:
            raise ValueError(f"must be {lo} to {hi}")
        return n

    return f


def _v_float(lo, hi, clamp=True):
    def f(v):
        try:
            n = float(v if v not in (None, "") else 0)
        except (TypeError, ValueError):
            raise ValueError("must be a number")
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
        raise ValueError("unknown timezone; use an IANA name like America/Los_Angeles")
    return v


def _v_roi(v):
    v = str(v or "").strip()
    try:
        camera_mod.parse_roi(v)
    except ValueError:
        raise ValueError("must be 'x,y,w,h' as fractions 0-1, or blank")
    return v


def _v_host(v):
    """An IP address or hostname, or blank. Deliberately strict: this string is
    handed to the plug library, and a stray scheme or port produces a confusing
    connection error rather than an obvious validation one."""
    v = str(v or "").strip()
    if not v:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", v):
        raise ValueError("must be an IP address or hostname, with no scheme or port")
    return v


def _v_usb_device(v):
    v = str(v or "").strip()
    if not re.fullmatch(r"/dev/video\d+", v):
        raise ValueError("must look like /dev/video0")
    return v


def _v_heat_sensor(v):
    v = str(v or "").strip()
    if not re.fullmatch(r"temp:soil(_[0-9A-Za-z]+)?", v):
        raise ValueError("pick a soil temperature probe")
    return v


def _v_setups(v):
    """A list of grow setups; see setups(). An empty list means one default."""
    if not isinstance(v, list):
        raise ValueError("must be a list")
    if len(v) > setups_mod.MAX_SETUPS:
        raise ValueError(f"at most {setups_mod.MAX_SETUPS} setups")
    out, ids, lights, luxes = [], set(), set(), set()
    for i, x in enumerate(v, 1):
        if not isinstance(x, dict):
            raise ValueError(f"setup {i} is not an object")
        name = str(x.get("name") or "").strip()[:40]
        if not name:
            raise ValueError(f"setup {i} needs a name")
        sid = (
            re.sub(r"[^a-z0-9]+", "-", str(x.get("id") or name).lower()).strip("-")[:24]
            or f"setup{i}"
        )
        while sid in ids:
            sid += "x"
        light = x.get("light") or ""
        if light not in ("main", "second", ""):
            raise ValueError(f"{name}: unknown light")
        if light and light in lights:
            raise ValueError(
                f"{name}: the {setups_mod.light_label(light)} is already assigned to another setup"
            )
        lux = str(x.get("lux") or "")
        if lux and not re.fullmatch(r"lux(:\d)?", lux):
            raise ValueError(f"{name}: light sensor must be lux or lux:2")
        if lux and lux in luxes:
            raise ValueError(f"{name}: {lux} is already assigned to another setup")
        k = x.get("k")
        if k in (None, "", 0):
            k = None
        else:
            try:
                k = float(k)
            except (TypeError, ValueError):
                raise ValueError(f"{name}: the lux-to-PPFD factor must be a number")
            if not 10 <= k <= 200:
                raise ValueError(f"{name}: the lux-to-PPFD factor must be 10 to 200")
        sens = x.get("sensors") or []
        if (
            not isinstance(sens, list)
            or len(sens) > 64
            or not all(
                isinstance(q, str) and re.fullmatch(r"[a-z_]+(:[A-Za-z0-9_]+)?", q) for q in sens
            )
        ):
            raise ValueError(f"{name}: sensors must be a list of sensor keys")
        trays_ = x.get("trays") or []
        if (
            not isinstance(trays_, list)
            or len(trays_) > 32
            or not all(
                isinstance(q, (str, int)) and re.fullmatch(r"[A-Za-z0-9_-]{1,24}", str(q))
                for q in trays_
            )
        ):
            raise ValueError(f"{name}: trays must be a list of tray ids")
        with settings_lock:
            known = set((settings.get("trays") or {}).keys())
        trays_ = sorted({str(q) for q in trays_} & known)  # a deleted tray drops out
        try:
            lo, hi = float(x.get("dli_low")), float(x.get("dli_high"))
        except (TypeError, ValueError):
            raise ValueError(f"{name}: the DLI band needs a low and a high")
        if not (0.5 <= lo < hi <= 65):
            raise ValueError(f"{name}: the DLI band low must be below the high (0.5 to 65)")
        # A tray's probe, canopy and float come with the tray, the light sensor
        # is chosen on its own, and the reservoir follows its flag: none of
        # them belong in the sensor list. A sensor may be listed in more than
        # one setup: one air sensor between two close areas counts for both.
        sens = [q for q in sens if not re.match(r"(probe|canopy|float|reservoir):|lux(:|$)", q)]
        for flag in ("fan", "camera", "reservoir", "heat"):
            if x.get(flag):
                if any(o.get(flag) for o in out):
                    raise ValueError(f"{name}: the {flag} is already assigned to another setup")
        ids.add(sid)
        if light:
            lights.add(light)
        if lux:
            luxes.add(lux)
        out.append(
            {
                "id": sid,
                "name": name,
                "light": light,
                "lux": lux,
                "k": k,
                "sensors": sorted(set(sens)),
                "trays": trays_,
                "fan": bool(x.get("fan")),
                "camera": bool(x.get("camera")),
                "reservoir": bool(x.get("reservoir")),
                "heat": bool(x.get("heat")),
                "dli_low": lo,
                "dli_high": hi,
            }
        )
    return out


def _v_trimmed(v):
    return str(v or "").strip()[:200]


def _v_secret(v):
    return str(v or "")[:200]


# One definition per field on the Settings form: its kind, the range the
# server accepts, and the step the form offers. The server's validators for
# these keys, the form's min/max/step attributes (routes.form_attrs) and the
# page's save and fill code (static/js/photos.js, from the embedded copy of this table) are
# all generated from it, so a field cannot be accepted by one and rejected or
# dropped by another. Kinds: int, float, bool, choice, time, text (with a
# check function), secret (blank keeps the stored value), tempF (stored in F,
# shown in the display units; zero_off: 0 means off and is never converted).
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
    "usb_device": {"kind": "text", "check": "_v_usb_device"},
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
    # never sent to the page (status.SECRET_SETTINGS), so a blank field keeps
    # the stored one: as plain text it was blanked by every save
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


SETTINGS_VALIDATORS = {
    "usb_warmup_frames": _v_int(1, 20),
    "live_interval_s": _v_int(0, 120),
    "heat_mode": _v_choice("off", "auto", "on"),
    "setups": _v_setups,
    "dli_target_low": _v_float(0.5, 60, clamp=False),
    "dli_target_high": _v_float(1, 65, clamp=False),
    "fan_mode": _v_choice("auto", "on", "off"),
    "fan_speed": _v_int(0, 100),
    "fan_auto_speed": _v_int(0, 100),
    "theme": _v_choice("auto", "light", "dark"),
}
SETTINGS_VALIDATORS.update({k: _form_validator(s) for k, s in FORM.items()})


def form_spec():
    """FORM for the page, each field with its default (the page fills a
    missing value from it, and saves it for a field left blank)."""
    return {k: dict(s, default=DEFAULTS.get(k)) for k, s in FORM.items()}


# Imported last: these modules import this one, and their import-time
# code runs only after everything above is defined. Their names are
# used inside functions, at call time, always as module.name.
import setups as setups_mod
import camera as camera_mod
