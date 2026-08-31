#!/usr/bin/env python3
"""
Grow light controller + dashboard + timelapse for Raspberry Pi.

Drives a logic-level MOSFET on GPIO18 via the kernel's hardware PWM,
following local sunrise and sunset with smooth fade-in / fade-out ramps.
Serves a dashboard on http://<pi-ip>:5000 with live-editable settings.
Optionally captures timelapse photos at a fixed interval during the
photoperiod, holding the light at a fixed brightness for each shot so
every frame is identically exposed. Photos land in ./timelapse/.

Requires (handled by setup.sh):
  - 'dtoverlay=pwm,pin=18,func=2' in /boot/firmware/config.txt, then reboot
  - rpicam-apps (apt) for the camera
  - pip install astral rpi-hardware-pwm flask
"""

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import signal
import statistics
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from rpi_hardware_pwm import HardwarePWM
from astral import LocationInfo
from astral.sun import sun
from flask import (Flask, Response, jsonify, render_template, request, session,
                   send_file, send_from_directory)
from werkzeug.security import check_password_hash

import db
import sensors
import growth as growth_mod
import notify
import ai_report
import discord_alert
import alerts
import hoststats
import quality

# ------------------- defaults (overridden by config.json) -------------------
DEFAULTS = {
    "latitude": 34.17,
    "longitude": -118.84,
    "timezone": "America/Los_Angeles",
    "max_bright": 100,         # percent
    "ramp_min": 30,            # minutes
    "light_override": "auto",  # auto = follow the sun schedule; on|off = manual hold
    "manual_bright": 100,      # brightness held when light_override is "on"
    "fan_mode": "auto",        # auto | on | off
    "fan_speed": 100,          # manual speed held when fan_mode is "on"
    "fan_auto_speed": 70,      # speed used by auto mode
    "fan_min_speed": 25,       # below this a fan often stalls; 0 disables the floor
    "fan_with_light": True,    # auto: run during the photoperiod
    "fan_humidity_on": 65,     # auto: also run above this RH (0 disables)
    "alerts_enabled": True,     # Discord threshold alerts (needs discord_webhook)
    "alert_sustain_min": 10,    # a condition must hold this long before firing
    "alert_cooldown_hours": 6,  # reminder interval while a problem persists
    "alert_dry_pct": 15,        # calibrated trays only
    "alert_humidity_high": 80,  # 0 disables
    "alert_dli_high": 0,        # ceiling counterpart to alert_dli_low: a day
                                #   finishing above this many mol means the
                                #   fixture is turned up too far or hung too
                                #   close. 0 disables (default, since it only
                                #   makes sense once a target is chosen).
    "alert_dli_low": 4,         # checked once daily just after lights-off:
                                #   a day that finishes under this many mol/m2
                                #   means the light was off, dimmed or blocked.
                                #   0 disables.
    "units": "imperial",       # "imperial" (F, inHg) or "metric" (C, hPa);
                               #   storage stays Celsius/hPa either way
    "humidity_low": 40,        # comfort band drawn on the humidity chart;
    "humidity_high": 60,       #   display only, the alert uses its own setting
    "canopy_factor": 1.0,      # sensor plane -> canopy multiplier. The sensor
                               #   sits at soil level; measure lux at canopy and
                               #   divide by the soil reading to get this. 1.0 =
                               #   sensor is already at canopy height.
    "lux_to_ppfd_k": 60,       # lux -> PPFD divisor; set for your fixture's
                               #   spectrum (0 = hide PPFD/DLI). ~60 suits a
                               #   white-dominant mixed red/blue/white panel.
    "soil_temp_high_f": 85,    # target band for the soil temp chart and alerts.
    "soil_temp_low_f": 80,     #   Chile germination is best 80-85F; drop the
                               #   band to about 70-80F once seedlings are up.
                               #   Either bound at 0 disables that side.
    "schedule_mode": "solar",  # solar | fixed | duration
    "fixed_on": "06:00",       # fixed mode: lights on
    "fixed_off": "20:00",      # fixed mode: lights off
    "duration_hours": 14,      # duration mode: day length...
    "duration_end": "20:00",   #   ...anchored to this off time
    "sunrise_offset_min": 0,   # negative starts before sunrise
    "sunset_offset_min": 0,    # positive runs past sunset
    "camera_backend": "rpicam",   # rpicam (CSI ribbon) or usb (UVC webcam)
    "usb_device": "/dev/video0",
    "usb_width": 2048,
    "usb_height": 1536,
    "usb_warmup_frames": 4,       # frames discarded so exposure settles
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
    "cam_rotate": 0,           # 0/90/180/270, applied to captures and analysis
    "timelapse_flatten": True, # render the video and thumbnails flattened
    "cam_rectify": True,       # flatten the tray plane before per-cell analysis
    "camera_enabled": False,   # master switch for all camera features (photos,
                               #   timelapse, camera vision, AI report). Off until
                               #   a working camera is connected.
    "capture_enabled": False,
    "capture_interval_min": 30,
    "capture_brightness": 100,  # light level held during each photo
    "roi": "",                  # crop as "x,y,w,h" fractions, blank = full frame
    "cam_width": 2304,          # capture resolution at full field of view. The
    "cam_height": 1296,         #   Module 3 sensor is 4608x2592, but a full 12MP
                                #   capture exhausts the Pi Zero 2 W's 512MB RAM, so
                                #   default to the 2304x1296 binned mode (same view).
                                #   Raise to 4608x2592 only on a Pi with more memory
    "sample_interval_min": 5,   # how often to read + log sensors
    "ntfy_topic": "",           # set to enable push notifications (see notify.py)
    "discord_webhook": "",      # set to enable Discord alerts (see discord_alert.py)
    "password_hash": "",        # set to enable login (see README); blank = open
    "cookie_secure": True,      # True for HTTPS; set False only for local http testing
    "probe_median_depth": 5,    # rolling median over the last N logged probe
                                # readings, used for decisions only (charts
                                # stay raw); 1 disables the filter
    "light_backend": "pwm",     # "pwm" = dimmable panel on GPIO; "kasa" = smart
                                # plug (on/off only, fixture knob sets intensity)
    "auto_water": False,        # master switch; keep OFF until moisture calibrated
    "moisture_threshold_pct": 30,  # CALIBRATION TODO: "dry" trigger, per-probe
    "pump_max_seconds": 20,     # hard cap on a single dose (anti-flood/dry-run)
    "pump_cooldown_min": 30,    # min wait between auto doses (soil wicks slowly)
    "pump_daily_max_seconds": 180,  # runaway backstop
    "fill_max_seconds": 60,     # hard cap on a fill-to-float run (if float never trips)
    "probe_cal": {},            # per-tray {wet,dry} raw ADC anchors -> probe moisture %
    "probe_names": {"1": "Tray 1", "2": "Tray 2"},  # ADS1115 A0 = tray 1, A1 = tray 2
    "ai_enabled": False,        # daily Claude vision report (needs an API key, see ai_report.py)
    "ai_model": "claude-opus-4-8",
    "ai_report_hour": 8,        # local hour (0-23) to run the daily report
    "ai_report_minute": 0,      # minute (0-59) within that hour
    "ai_notify": True,          # push the report summary via ntfy
    "ai_notes": "Peat/vermiculite/perlite seed starter in a cell tray, bottom-watered. "
                "Mixed germination: some cells sprouted, some still germinating.",
    "grid": {                   # cell-mapping overlay
        "corners": [[0.12, 0.10], [0.88, 0.10], [0.88, 0.92], [0.12, 0.92]],
        "rows": 4, "cols": 4, "names": {}, "show": True, "locked": False,
    },
    # What is actually planted where. Two trays, 3 wide x 4 deep, cells A1..C4.
    # Each cell: {"seed": name, "equipment": what's occupying it, "planted": ISO date}
    "trays": {
        "1": {"label": "Tray 1", "rows": 4, "cols": 3, "cells": {}},
        "2": {"label": "Tray 2", "rows": 4, "cols": 3, "cells": {}},
    },
}

GPIO_PIN      = 18   # hardware PWM channel 0. Only 18 or 19 can do hardware
                     # PWM; GROWLIGHT_LIGHT_PIN picks between them.
try:
    GPIO_PIN = int(os.environ.get("GROWLIGHT_LIGHT_PIN", GPIO_PIN))
except ValueError:
    pass
PWM_FREQ      = 1000
LOOP_SECONDS  = 30
HTTP_PORT     = 5000
CONFIG_PATH   = Path(__file__).with_name("config.json")
TIMELAPSE_DIR = Path(__file__).with_name("timelapse")
AI_REPORT_PATH = Path(__file__).with_name("ai_report.json")
PREVIEW_PATH = Path(__file__).with_name("preview.jpg")  # alignment viewfinder; not a timelapse frame
report_lock = threading.Lock()
report_state = {"generating": False}
TIMEZONES     = sorted(available_timezones())
THUMB_DIR     = TIMELAPSE_DIR / "thumbs"
# ----------------------------------------------------------------------------

TIMELAPSE_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)

# --- pump actuators (gpiozero, guarded so off-Pi / unwired stays safe) ---
# Pump GPIOs (BCM). Override without editing code by setting GROWLIGHT_PUMP_PINS,
# e.g. GROWLIGHT_PUMP_PINS="1:24,2:26" in the systemd unit, then restart.
FAN_PIN = 20                     # BCM; physical 38. Low-side switched via a
                                 #   D4184 with a flyback across the fan.
_fanenv = os.environ.get("GROWLIGHT_FAN_PIN")
if _fanenv is not None and not _fanenv.strip():
    FAN_PIN = None                      # explicitly configured as "no fan"
else:
    try:
        FAN_PIN = int(_fanenv) if _fanenv else FAN_PIN
    except ValueError:
        print(f"GROWLIGHT_FAN_PIN unreadable; using {FAN_PIN}")
# Speed control is software PWM: GPIO20 has no hardware PWM channel (those are
# 18 and 19, and 18 drives the light). Software PWM is fine for a fan at these
# duty cycles, but cheap fans can whine audibly or stall below ~30%, which is
# why fan_min_speed exists.
_fan = None
FAN_PWM_HZ = 100
if FAN_PIN is None:
    print("fan: none configured")
else:
    try:
        from gpiozero import PWMOutputDevice as _PWMOut
        _fan = _PWMOut(FAN_PIN, frequency=FAN_PWM_HZ, initial_value=0)
    except Exception as _e:
        print(f"fan GPIO{FAN_PIN} unavailable ({_e}); fan control disabled")
FAN_HW = _fan is not None
fan_state = {"on": False, "reason": "off", "speed": 0}

PUMP_PINS = {"1": 24, "2": 26}   # tray -> BCM (physical 18, 37)
_pp = os.environ.get("GROWLIGHT_PUMP_PINS")
if _pp is not None and not _pp.strip():
    PUMP_PINS = {}                      # explicitly configured as "no pumps"
    print("pump pins: none configured")
elif (_pp or "").strip():
    _pp = _pp.strip()
    try:
        PUMP_PINS = {t.strip(): int(v) for t, v in
                     (part.split(":") for part in _pp.split(","))}
        print(f"pump pins from environment: {PUMP_PINS}")
    except Exception as _e:
        print(f"GROWLIGHT_PUMP_PINS unreadable ({_e}); using {PUMP_PINS}")
_pumps = {}
for _t, _pin in PUMP_PINS.items():
    try:
        from gpiozero import OutputDevice
        _pumps[_t] = OutputDevice(_pin, active_high=True, initial_value=False)
    except Exception as _e:
        print(f"pump {_t} GPIO{_pin} unavailable ({_e}); disabled")
PUMP_HW = bool(_pumps)

def _blank_pump():
    return {"running": False, "last_run": 0.0,
            "today_seconds": 0.0, "day": "", "last_detail": ""}
pump_state = {t: _blank_pump() for t in PUMP_PINS}
pump_lock = threading.Lock()   # also serializes the two pumps: one at a time
# a fill that ran to its cap without the float tripping: the message feeds the
# alert state machine (fires on the next sample tick, reminds while unresolved)
# and is cleared by the next successful fill
fill_failure = {"msg": ""}

settings = dict(DEFAULTS)
_file_keys = set()
if CONFIG_PATH.exists():
    try:
        _saved = json.loads(CONFIG_PATH.read_text())
        # keys starting with "_" are runtime state that older builds persisted
        # by accident (e.g. _seen_sensors); loading them back made a removed
        # sensor alert forever
        _saved = {k: v for k, v in _saved.items() if not k.startswith("_")}
        _file_keys = set(_saved)
        settings.update(_saved)
    except Exception as e:
        print(f"config.json unreadable ({e}), using defaults")


def save_config():
    """Persist settings to config.json. Caller must hold settings_lock.

    The single place config is written, so the no-runtime-keys rule cannot be
    forgotten at one of a dozen call sites: anything starting with "_" is
    in-memory state and never lands on disk."""
    data = {k: v for k, v in settings.items() if not k.startswith("_")}
    CONFIG_PATH.write_text(json.dumps(data, indent=2))

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
                    col = ord(m.group(1)) - 65        # old column 0-3
                    row = int(m.group(2)) - 1          # old row 0-2
                    newcells[f"{chr(65 + row)}{col + 1}"] = v   # transpose
                else:
                    newcells[cid] = v
            t.update(rows=4, cols=3, cells=newcells)
            changed = True
            if newcells:
                print(f"tray {tid}: migrated {len(newcells)} cells to 3x4 layout")
    if changed:
        try:
            save_config()
        except Exception as e:
            print(f"tray migration not persisted ({e})")
_migrate_trays()

# the soil low bound used to live under an alerts-only key; adopt it once so
# the chart and the alerts can never disagree. Checked against the file's own
# keys, since DEFAULTS always supplies soil_temp_low_f after the merge.
if "alert_soil_low_f" in settings:
    _old = settings.pop("alert_soil_low_f")
    if _file_keys and "soil_temp_low_f" not in _file_keys:
        settings["soil_temp_low_f"] = _old
    try:
        save_config()
    except Exception:
        pass


settings_lock = threading.Lock()
wake = threading.Event()

state = {"brightness": 0.0, "on": None, "off": None,
         "sunrise": None, "sunset": None}
state_lock = threading.Lock()
# camera health: every capture/preview outcome lands here so the dashboard
# can tell "old photo because night" from "old photo because the camera died"
camera = {"last_ok": None, "fails": 0, "last_err": "", "last_err_ts": None}

# light response sweep: brightness % -> measured lux, so the dashboard can show
# what the driver actually delivers (PWM dimming is rarely linear)
sweep_state = {"running": False, "pct": 0, "error": "", "started": 0.0,
               "cancel": False}   # cancel is a request; running means the
                                  # worker thread is still holding the light
sweep_lock = threading.Lock()

# focus sweep: walk focus_absolute, score each frame's sharpness, pin the best.
# Replaces guessing focus values over SSH (a guessed 68 produced a week of
# blurry photos). step counts down as coarse then fine passes run.
focus_state = {"running": False, "step": 0, "total": 0, "best": None,
               "error": "", "cancel": False}
focus_lock = threading.Lock()


def _camera_ok():
    with state_lock:
        camera.update(last_ok=time.time(), fails=0, last_err="", last_err_ts=None)


def _camera_fail(err):
    with state_lock:
        camera["fails"] += 1
        camera["last_err"] = str(err)[:200]
        camera["last_err_ts"] = time.time()
capturing = False   # capture thread holds the light; control loop defers
capture_lock = threading.Lock()  # serialize camera access (manual vs scheduled)
render = {"state": "idle", "msg": "", "frames": 0,
          "started": None, "elapsed": None}   # idle|running|done|error
render_lock = threading.Lock()
VIDEO_PATH = TIMELAPSE_DIR / "timelapse.mp4"
ARCHIVE_DIR = Path(__file__).with_name("timelapse_archive")
GROWTH_SCRIPT = Path(__file__).with_name("growth.py")

try:
    pwm = HardwarePWM(pwm_channel=(1 if GPIO_PIN == 19 else 0),
                      hz=PWM_FREQ, chip=0)
    pwm.start(0)
except Exception as e:
    sys.exit(f"Hardware PWM unavailable ({e}). Check that "
             f"'dtoverlay=pwm,pin=18,func=2' is in /boot/firmware/config.txt "
             f"and reboot after adding it.")


# ---- light output backends ----
# "pwm" is the original path: a dimmable 5V panel on hardware PWM.
# "kasa" drives a TP-Link smart plug for an AC fixture that has no controllable
# dimming, so output is on/off and the fixture's own knob sets intensity.
# Both are kept: switching backends is a settings change, not a redeploy.
KASA_HOST = (os.environ.get("GROWLIGHT_KASA_HOST") or "").strip()
KASA_USER = (os.environ.get("GROWLIGHT_KASA_USER") or "").strip()
KASA_PASS = os.environ.get("GROWLIGHT_KASA_PASS") or ""
KASA_ON_AT = 1.0        # brightness above this percent means "on"

kasa_state = {"on": None,        # last state we believe the plug is in
              "ok": None,        # last command succeeded?
              "error": "",       # human-readable last failure
              "fails": 0,        # consecutive failures
              "last_ok": 0.0}
_kasa_lock = threading.Lock()
_kasa_loop = None
_kasa_dev = None


def _kasa_run(coro, timeout=12):
    """Run a python-kasa coroutine from this threaded app.

    python-kasa is async and the app is threads, so all plug I/O happens on one
    dedicated event loop in its own thread. One loop (not asyncio.run per call)
    because the library caches a session per device and reconnecting on every
    poll is both slow and hard on the plug.
    """
    global _kasa_loop
    if _kasa_loop is None:
        _kasa_loop = asyncio.new_event_loop()
        threading.Thread(target=_kasa_loop.run_forever, daemon=True,
                         name="kasa").start()
    fut = asyncio.run_coroutine_threadsafe(coro, _kasa_loop)
    return fut.result(timeout=timeout)


async def _kasa_device():
    global _kasa_dev
    if _kasa_dev is not None:
        return _kasa_dev
    from kasa import Device, DeviceConfig, Credentials
    if KASA_USER or KASA_PASS:
        cfg = DeviceConfig(host=KASA_HOST,
                           credentials=Credentials(KASA_USER, KASA_PASS))
        _kasa_dev = await Device.connect(config=cfg)
    else:
        # legacy devices (HS103 and friends) need no credentials at all
        _kasa_dev = await Device.connect(host=KASA_HOST)
    return _kasa_dev


async def _kasa_set(on):
    dev = await _kasa_device()
    await (dev.turn_on() if on else dev.turn_off())
    await dev.update()
    return bool(dev.is_on)


def kasa_apply(on, retries=1):
    """Drive the plug to `on`. Returns True on success.

    Retries once because a single dropped packet over wifi is routine and not
    worth an alert; a genuine failure (plug unplugged, wrong IP, auth broken)
    fails both attempts and is recorded for the alert rules.
    """
    global _kasa_dev
    if not KASA_HOST:
        with _kasa_lock:
            kasa_state.update(ok=False, error="no GROWLIGHT_KASA_HOST configured")
        return False
    last = ""
    for attempt in range(retries + 1):
        try:
            actual = _kasa_run(_kasa_set(on))
            with _kasa_lock:
                kasa_state.update(on=actual, ok=True, error="", fails=0,
                                  last_ok=time.time())
            return True
        except Exception as e:
            last = f"{type(e).__name__}: {e}"[:200]
            _kasa_dev = None          # force a reconnect on the next attempt
            if attempt < retries:
                time.sleep(1.5)
    with _kasa_lock:
        kasa_state["fails"] += 1
        kasa_state.update(ok=False, error=last)
    print(f"kasa plug command failed ({last})")
    return False


def release_backend(old):
    """Turn off whichever output we are switching away from.

    Without this the abandoned backend holds its last state forever: switch
    from PWM to the plug while the panel is lit and the panel stays lit, drawing
    power with nothing in the app able to reach it any more.
    """
    try:
        if old == "kasa":
            kasa_apply(False)
        else:
            pwm.change_duty_cycle(0)
        print(f"light backend released: {old} set to off")
    except Exception as e:
        print(f"could not release the {old} light backend: {e}")


def light_backend(cfg=None):
    if cfg is None:
        with settings_lock:
            cfg = dict(settings)
    return "kasa" if cfg.get("light_backend") == "kasa" else "pwm"


def set_brightness(percent):
    """Apply a brightness percent through whichever backend is configured.

    On the kasa backend there is no dimming: anything above KASA_ON_AT turns the
    plug on, at or below turns it off. The percent is still carried through the
    rest of the app unchanged so schedules, ramps and the DLI code need no
    special cases; the plug simply cannot express the middle of a ramp.
    """
    percent = max(0.0, min(100.0, percent))
    if light_backend() == "kasa":
        want = percent > KASA_ON_AT
        with _kasa_lock:
            believed = kasa_state["on"]
        if believed is want and kasa_state["ok"]:
            return                      # already there; don't poll the plug
        kasa_apply(want)
        return
    pwm.change_duty_cycle(percent)


def _clock(day, tz, hhmm, fallback="06:00"):
    """'HH:MM' on `day` as an aware datetime."""
    try:
        h, m = str(hhmm or fallback).split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        h, m = int(fallback[:2]), int(fallback[3:])
    return datetime(day.year, day.month, day.day,
                    max(0, min(23, h)), max(0, min(59, m)), tzinfo=tz)


def sun_window(cfg, day, tz):
    """The light window for `day`, in whichever scheduling mode is configured.

    solar     - follow local sunrise/sunset with offsets (the original behaviour)
    fixed     - explicit on and off clock times
    duration  - a day length anchored to the off time, so lights-off stays put
                and lights-on moves to give the requested hours

    Always returns the real sunrise/sunset too, so the dashboard can show them
    regardless of mode. A window that ends before it starts is treated as
    crossing midnight.
    """
    loc = LocationInfo(latitude=cfg["latitude"], longitude=cfg["longitude"])
    s = sun(loc.observer, date=day, tzinfo=tz)
    mode = cfg.get("schedule_mode", "solar")

    if mode == "fixed":
        on_time = _clock(day, tz, cfg.get("fixed_on"), "06:00")
        off_time = _clock(day, tz, cfg.get("fixed_off"), "20:00")
        if off_time <= on_time:
            off_time += timedelta(days=1)      # window crosses midnight
    elif mode == "duration":
        try:
            hours = float(cfg.get("duration_hours", 14))
        except (TypeError, ValueError):
            hours = 14.0
        hours = max(0.0, min(24.0, hours))
        off_time = _clock(day, tz, cfg.get("duration_end"), "20:00")
        on_time = off_time - timedelta(hours=hours)
    else:
        on_time = s["sunrise"] + timedelta(minutes=cfg["sunrise_offset_min"])
        off_time = s["sunset"] + timedelta(minutes=cfg["sunset_offset_min"])

    return s["sunrise"], s["sunset"], on_time, off_time


def brightness_for(cfg, now, on_time, off_time):
    if now <= on_time or now >= off_time:
        return 0.0
    mx = cfg["max_bright"]
    ramp = timedelta(minutes=cfg["ramp_min"])
    full_start, full_end = on_time + ramp, off_time - ramp
    if full_start >= full_end:  # very short window: triangular peak
        mid = on_time + (off_time - on_time) / 2
        if now <= mid:
            return mx * (now - on_time) / (mid - on_time)
        return mx * (off_time - now) / (off_time - mid)
    if now < full_start:
        return mx * (now - on_time) / ramp
    if now > full_end:
        return mx * (off_time - now) / ramp
    return float(mx)


def parse_roi(s):
    """Validate 'x,y,w,h' fraction string. Returns tuple or None for blank."""
    s = (s or "").strip()
    if not s:
        return None
    parts = [float(p) for p in s.split(",")]
    if len(parts) != 4:
        raise ValueError("need four numbers")
    x, y, w, h = parts
    if not (0 <= x < 1 and 0 <= y < 1 and 0.05 <= w <= 1 and 0.05 <= h <= 1
            and x + w <= 1.001 and y + h <= 1.001):
        raise ValueError("out of range")
    return x, y, w, h


def make_thumb(photo_path, cfg=None):
    """640px thumbnail for the browser player. Cheap, one-time per photo.

    Rectified to match the snapshot and the rendered video, so scrubbing the
    timelapse shows the same corrected view as everything else. Falls back to a
    plain scale if the grid corners are not set or OpenCV is unavailable.
    """
    dst = THUMB_DIR / photo_path.name
    if dst.exists():
        return
    if cfg is None:
        with settings_lock:
            cfg = dict(settings)
    grid = (cfg.get("grid") or {})
    corners = grid.get("corners")
    if cfg.get("timelapse_flatten", True) and corners and len(corners) == 4:
        try:
            import cv2
            img = cv2.imread(str(photo_path))
            if img is not None:
                warped = growth_mod.rectify(img, corners,
                                            cols=int(grid.get("cols", 4)),
                                            rows=int(grid.get("rows", 4)))
                h, w = warped.shape[:2]
                if w > 640:
                    warped = cv2.resize(warped, (640, max(1, int(h * 640 / w))))
                cv2.imwrite(str(dst), warped, [cv2.IMWRITE_JPEG_QUALITY, 82])
                return
        except Exception as e:
            print(f"thumb rectify failed for {photo_path.name} ({e}); plain scale")
    try:
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(photo_path),
             "-vf", "scale=640:-2", "-q:v", "7", str(dst)],
            capture_output=True, timeout=120)
    except Exception as e:
        print(f"thumbnail error for {photo_path.name}: {e}")


def photo_inventory():
    # leading underscore marks render scratch, which is not a captured photo
    photos = sorted(p for p in TIMELAPSE_DIR.glob("*.jpg")
                    if not p.name.startswith("_"))
    if not photos:
        return 0, None, None
    latest = photos[-1]
    return len(photos), latest, datetime.fromtimestamp(latest.stat().st_mtime)


# --------------------------- control loop ---------------------------

def _today_str():
    return datetime.now(ZoneInfo(settings["timezone"])).date().isoformat()


def reservoir_state():
    """Combined reservoir level from the low and high sensors.

    full  - water at both sensors
    ok    - water at the low sensor only
    empty - water at neither
    fault - water at the high sensor but not the low one, which is physically
            impossible: a sensor died, fell off the wall, or the sensitivity
            pot needs adjusting
    None  - no reservoir sensors wired

    "empty" hard-refuses pump runs (checked directly at run time, no sustain)
    because pumping from an empty source runs the pumps dry.
    """
    lo = sensors.read_reservoir_level("low")
    hi = sensors.read_reservoir_level("high")
    if lo is None and hi is None:
        return None
    if hi is not None and hi >= 1:
        return "full" if (lo is None or lo >= 1) else "fault"
    if lo is not None and lo >= 1:
        return "ok"
    return "empty"


def run_pump(tray, seconds, reason="manual", force=False):
    """Run one tray's pump for `seconds`, clamped to the hard cap. Safety:
    refuses if that pump is absent, if any pump is running (one at a time, the
    two share a supply), or (unless forced) if the tray's daily cap would be
    exceeded. Blocks for the duration, so call it in a thread.
    Returns (ok, message). Logs every run as a pump event."""
    tray = str(tray)
    with settings_lock:
        cap = float(settings.get("pump_max_seconds", 20))
        daily_cap = float(settings.get("pump_daily_max_seconds", 180))
    secs = max(0.0, min(float(seconds), cap))
    with pump_lock:
        if tray not in _pumps:
            return False, f"pump {tray} hardware not available"
        if any(st["running"] for st in pump_state.values()):
            return False, "a pump is already running"
        if not force and reservoir_state() == "empty":
            return False, "reservoir is empty; refusing to run the pump dry"
        st = pump_state[tray]
        if st["day"] != _today_str():
            st["day"] = _today_str()
            st["today_seconds"] = 0.0
        if not force and st["today_seconds"] + secs > daily_cap:
            return False, f"tray {tray} daily pump limit reached"
        st["running"] = True
    elapsed = 0.0
    try:
        _pumps[tray].on()
        t0 = time.time()
        time.sleep(secs)
        elapsed = time.time() - t0
    finally:
        _pumps[tray].off()
        with pump_lock:
            st["running"] = False
            st["last_run"] = time.time()
            st["today_seconds"] += elapsed
            st["last_detail"] = f"{reason} {elapsed:.1f}s"
    try:
        db.log_event("pump", f"tray {tray}: {reason} {elapsed:.1f}s")
    except Exception:
        pass
    return True, f"ran {elapsed:.1f}s"


def run_pump_until_full(tray, reason="fill", force=False):
    """Run the pump until the float reads full, then stop. A hard time cap is
    the backstop: if the float never trips within fill_max_seconds the pump
    stops anyway and the run is flagged, because that means the source is empty,
    the tube is off, or the float failed. Float reading None (sensor lost) is
    treated as 'stop' (fail-safe). Blocks; call in a thread. Logs the run."""
    tray = str(tray)
    with settings_lock:
        cap = float(settings.get("fill_max_seconds", 60))
        daily_cap = float(settings.get("pump_daily_max_seconds", 180))
    with pump_lock:
        if tray not in _pumps:
            return False, f"pump {tray} hardware not available"
        if any(st["running"] for st in pump_state.values()):
            return False, "a pump is already running"
        if not force and reservoir_state() == "empty":
            return False, "reservoir is empty; refusing to run the pump dry"
        st = pump_state[tray]
        if st["day"] != _today_str():
            st["day"] = _today_str()
            st["today_seconds"] = 0.0
        f0 = sensors.read_float(tray)
        if f0 is None and not force:
            return False, f"no float sensor on tray {tray}; refusing to fill blind"
        if f0 is not None and f0 < 1:
            return False, f"tray {tray} already full"
        remaining = daily_cap - st["today_seconds"]
        if not force and remaining <= 0:
            return False, f"tray {tray} daily pump limit reached"
        run_cap = cap if force else min(cap, remaining)
        st["running"] = True
    elapsed = 0.0
    tripped = False
    confirm = 0                              # consecutive "full" reads needed
    CONFIRM_NEEDED = 4                        # ~0.4s steady, rejects slosh/bobble
    try:
        _pumps[tray].on()
        t0 = time.time()
        while True:
            elapsed = time.time() - t0
            if elapsed >= run_cap:
                break                        # cap hit, float never stayed full
            fv = sensors.read_float(tray)
            if fv is None or fv < 1:         # full (open) or sensor lost
                confirm += 1
                if confirm >= CONFIRM_NEEDED:
                    tripped = (fv is not None and fv < 1)
                    break                    # full held steady -> stop
            else:
                confirm = 0                  # a not-full read resets the count
            time.sleep(0.1)                  # poll the float ~10x/sec
    finally:
        _pumps[tray].off()
        with pump_lock:
            st["running"] = False
            st["last_run"] = time.time()
            st["today_seconds"] += elapsed
            detail = (f"tray {tray} {reason}: full at {elapsed:.1f}s" if tripped
                      else f"tray {tray} {reason}: STOPPED at {elapsed:.1f}s cap, no float trip")
            st["last_detail"] = detail
    try:
        db.log_event("pump", detail)
    except Exception:
        pass
    if tripped:
        fill_failure["msg"] = ""             # a good fill resolves the alert
        schedule_postfill(tray)              # judge the probe once water wicks
        return True, f"filled in {elapsed:.1f}s"
    fill_failure["msg"] = (f"Tray {tray} fill ran to the {elapsed:.1f}s cap "
                           "without the float tripping. Likely causes: source "
                           "empty, tube off, or float stuck.")
    # a fill that can't complete means auto-watering must not keep trying
    with settings_lock:
        if settings.get("auto_water"):
            settings["auto_water"] = False
            fill_failure["msg"] += " Auto-watering has been switched off."
            try:
                save_config()
            except Exception as e:
                print(f"auto_water disable not persisted ({e})")
    return False, f"ran to {elapsed:.1f}s cap without float trip (source empty?)"


PROBE_DEFAULT_CAL = {"wet": 1.25, "dry": 2.95}   # typical HW-390 on 3.3V; used
                                                 # until a tray is calibrated


TEMP_COMP_REF_F = 70.0   # compensation is zero at this soil temp


def compensated_volts(volts, cal, soil_temp_f):
    """Capacitive probes read wetter (lower voltage) as the soil warms. Undo
    that drift so moisture is comparable across a temperature swing, which
    matters most on a heat mat. No coefficient configured = unchanged."""
    tc = (cal or {}).get("temp_comp") or {}
    coeff = tc.get("coeff")
    if not coeff or soil_temp_f is None:
        return volts
    ref = tc.get("ref_f", TEMP_COMP_REF_F)
    return volts - coeff * (soil_temp_f - ref)


PROBE_JUMP_V = 0.08     # volts; a departure larger than this is judged
PROBE_CONFIRM = 3       # consecutive agreeing readings that make it a step
_probe_verdict = {}     # tray -> "steady" | "spike" | "step", for display


def probe_volts_filtered(tray, snap=None):
    """A probe's voltage with single-sample transients rejected.

    The probes sit steady to a couple of millivolts and then take occasional
    excursions of 100-350mV, which is interference rather than sensor noise
    (a switching load near unshielded leads into a high-impedance ADC input).
    The per-read median in sensors.py samples milliseconds apart, so it cannot
    see a transient that lasts longer than that; this takes a median across the
    last few LOGGED readings instead, which rejects one bad sample outright
    while still tracking soil that dries over hours.

    Charts keep showing raw values on purpose: the excursions are diagnostic
    and hiding them would have hidden the problem. Only the decisions
    (moisture %, dry alert, auto-watering) read the filtered value.

    Returns (volts, ts) or (None, None).
    """
    key = f"probe:{tray}"
    if snap is None:
        snap = db.latest()
    rec = snap.get(key)
    if rec is None:
        return None, None
    ts, raw = rec
    with settings_lock:
        depth = int(settings.get("probe_median_depth", 5))
    depth = max(depth, PROBE_CONFIRM + 6) if depth > 1 else depth
    if depth <= 1:
        return raw, ts
    try:
        vals = db.recent_values(key, n=depth)
    except Exception as e:
        print(f"probe filter fell back to raw ({e})")
        return raw, ts
    if len(vals) < 3:
        return raw, ts          # not enough history to filter meaningfully
    val, verdict = quality.spike_or_step(vals, jump=PROBE_JUMP_V,
                                         confirm=PROBE_CONFIRM)
    _probe_verdict[str(tray)] = verdict
    return (val if val is not None else raw), ts


def probe_moisture(volts, cal, soil_temp_f=None):
    """Map a tray probe's voltage to 0-100% from its wet/dry anchors.
    Capacitive probes read high when dry, low when wet."""
    if not cal:
        return None
    wet, dry = cal.get("wet"), cal.get("dry")
    if wet is None or dry is None:
        return None
    if dry - wet < 0.05:      # anchors too close to mean anything
        return None
    v = compensated_volts(volts, cal, soil_temp_f)
    return round(max(0.0, min(100.0, 100.0 * (dry - v) / (dry - wet))))


def probe_moisture_any(volts, cal, soil_temp_f=None):
    """(percent, approximate) - falls back to typical probe endpoints when the
    tray hasn't been calibrated, so there's always a number to look at."""
    pct = probe_moisture(volts, cal, soil_temp_f)
    if pct is not None:
        return pct, False
    return probe_moisture(volts, PROBE_DEFAULT_CAL, soil_temp_f), True


PROBE_CAL_SLOP = 0.02   # volts past an anchor before the calibration is flagged


def probe_cal_flag(volts, cal):
    """'below_wet' / 'above_dry' when a live reading sits outside the tray's
    calibration anchors, else None. A reading below the wet anchor clamps to
    100% and silently kills the dry alert (this exact failure hid two pegged
    probes for a week), so it is surfaced instead of absorbed."""
    if not cal:
        return None
    wet, dry = cal.get("wet"), cal.get("dry")
    if wet is None or dry is None or dry - wet < 0.05:
        return None
    if volts < wet - PROBE_CAL_SLOP:
        return "below_wet"
    if volts > dry + PROBE_CAL_SLOP:
        return "above_dry"
    return None


def latest_soil_temp_f(snapshot=None):
    """Current soil temperature in F, or None. Uses the first DS18B20 found.
    Pass an existing db.latest() snapshot to avoid a redundant query."""
    for k, (ts, v) in (snapshot if snapshot is not None else db.latest()).items():
        if k.startswith("temp:soil"):
            return v * 9 / 5 + 32
    return None


def estimate_temp_comp(tray, hours=48):
    """Regress a tray's probe voltage against soil temperature over the last
    `hours` to find the thermal drift. Best run over a warm, no-watering
    window, so the voltage change is dominated by the temperature artifact
    rather than real drying. Returns a dict with the slope in V/F."""
    volts = db.series(f"probe:{tray}", hours=hours)
    if len(volts) < 10:
        return {"ok": False, "error": f"not enough probe data ({len(volts)} points)"}
    xs, ys = [], []
    for ts, v in volts:
        t = db.reading_near("temp:soil", ts, window=1800)
        if t is None:
            continue
        xs.append(t * 9 / 5 + 32)   # soil temp F
        ys.append(v)                # probe volts
    n = len(xs)
    if n < 10:
        return {"ok": False, "error": f"not enough paired temp data ({n} points)"}
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom < 1e-9:
        return {"ok": False, "error": "soil temp did not vary enough to estimate"}
    slope = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / denom
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    r = (sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / (denom ** 0.5 * sy)
         if sy > 0 else 0.0)
    return {"ok": True, "tray": tray, "coeff": round(slope, 5), "r": round(r, 3),
            "n": n, "temp_min": round(min(xs), 1), "temp_max": round(max(xs), 1),
            "span": round(max(xs) - min(xs), 1)}


def _units():
    with settings_lock:
        return settings.get("units", "imperial")


def temp_out(c):
    """Celsius storage -> the configured display unit."""
    return round(c if _units() == "metric" else c * 9 / 5 + 32, 1)


def temp_unit():
    return "C" if _units() == "metric" else "F"


def press_out(hpa):
    return round(hpa if _units() == "metric" else hpa * 0.0295299830714,
                 0 if _units() == "metric" else 2)


def press_unit():
    return "hPa" if _units() == "metric" else "inHg"


def gather_report_data():
    """Assemble the controller snapshot the AI report is built from."""
    with settings_lock:
        cfg = dict(settings)
    with state_lock:
        st = dict(state)
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    pcal = cfg.get("probe_cal") or {}
    pnames = cfg.get("probe_names") or {}
    canopy, probes, soiltemp, env = {}, {}, {}, {}
    trays_cfg = cfg.get("trays") or {}
    snap = db.latest()
    stf = latest_soil_temp_f(snap)
    for k, (ts, v) in snap.items():
        if k.startswith("canopy:"):
            tid = k[7:]
            label = ((trays_cfg.get(tid) or {}).get("label")
                     or pnames.get(tid) or f"Tray {tid}")
            canopy[label] = round(v, 1)
        elif k.startswith("probe:"):
            t = k[6:]
            tcal = pcal.get(t) or {}
            fv, _ = probe_volts_filtered(t, snap)
            if fv is None:
                fv = v
            pm, approx = probe_moisture_any(fv, tcal, stf)
            nm = pnames.get(t, f"Tray {t}")
            flag = probe_cal_flag(fv, tcal)
            if flag:
                nm += (" (reading beyond the wet anchor - recalibrate wet)"
                       if flag == "below_wet"
                       else " (reading beyond the dry anchor - recalibrate dry)")
            elif approx:
                nm += " (approx)"
            if pm is not None:
                probes[nm] = pm
            else:
                probes[nm] = round(v, 3)
        elif k.startswith("temp:soil"):
            label = "Soil" if k == "temp:soil" else f"Soil {k.split('_')[-1]}"
            soiltemp[label] = temp_out(v)
        elif k == "temp:air":
            env["air_f"] = temp_out(v)
        elif k == "pressure":
            env["pressure"] = press_out(v)
        elif k in ("humidity", "lux"):
            env[k] = round(v, 1)
    fstates = {}
    for _t in sensors.FLOAT_PINS:
        _fv = sensors.read_float(_t)
        fstates[_t] = "no sensor" if _fv is None else ("not full" if _fv >= 1 else "full")
    flabel = ", ".join(f"tray {t}: {v}" for t, v in sorted(fstates.items()))
    grid = cfg.get("grid") or {}
    planting = {}
    for tid, t in sorted((cfg.get("trays") or {}).items()):
        rows = []
        for cid, v in sorted((t.get("cells") or {}).items()):
            bits = []
            if v.get("seed"):
                bits.append(v["seed"])
            if v.get("equipment"):
                bits.append(f"[{v['equipment']}]")
            sown = spr = None
            if v.get("planted"):
                try:
                    sown = datetime.strptime(v["planted"], "%Y-%m-%d").date()
                except ValueError:
                    sown = None
            if v.get("sprouted"):
                try:
                    spr = datetime.strptime(v["sprouted"], "%Y-%m-%d").date()
                except ValueError:
                    spr = None
            if v.get("archived"):
                bits.append(f"TRANSPLANTED {v['archived']}")
            elif spr and sown:
                bits.append(f"sprouted in {(spr - sown).days}d, "
                            f"{(now.date() - spr).days}d old")
            elif spr:
                bits.append(f"sprouted {v['sprouted']}")
            elif sown:
                bits.append(f"sown {v['planted']} ({(now.date() - sown).days}d ago, "
                            "not yet sprouted)")
            if v.get("count"):
                bits.append(f"({v['count']} seeds)")
            if v.get("notes"):
                bits.append(f"- {v['notes']}")
            if bits:
                rows.append(f"{cid}: {' '.join(bits)}")
        if rows:
            planting[t.get("label", f"Tray {tid}")] = rows

    # per-variety germination: rate and average days, the numbers worth keeping
    germ = {}
    for tid, t in sorted((cfg.get("trays") or {}).items()):
        for cid, v in (t.get("cells") or {}).items():
            seed = (v.get("seed") or "").strip()
            if not seed or not v.get("planted"):
                continue
            g = germ.setdefault(seed, {"sown": 0, "up": 0, "days": [],
                                       "seeds": 0, "sources": set()})
            g["sown"] += 1
            g["seeds"] += int(v.get("count") or 0)
            if v.get("source"):
                g["sources"].add(v["source"])
            if v.get("sprouted"):
                g["up"] += 1
                try:
                    d0 = datetime.strptime(v["planted"], "%Y-%m-%d").date()
                    d1 = datetime.strptime(v["sprouted"], "%Y-%m-%d").date()
                    g["days"].append((d1 - d0).days)
                except ValueError:
                    pass
    germ_out = {}
    for seed, g in sorted(germ.items()):
        line = f"{g['up']}/{g['sown']} cells up"
        if g["seeds"]:
            line += f" ({g['seeds']} seeds sown)"
        if g["days"]:
            line += f", avg {sum(g['days']) / len(g['days']):.1f}d to sprout"
        if g["sources"]:
            line += f", seed from {', '.join(sorted(g['sources']))}"
        germ_out[seed] = line
    bright = round(st.get("brightness") or 0)
    return {
        "date": now.strftime("%Y-%m-%d %H:%M"),
        "days_running": None,
        "location": f"lat {cfg['latitude']}, lon {cfg['longitude']} ({cfg['timezone']})",
        "light": {"phase": "day" if bright > 0 else "night", "brightness": bright,
                  "on": st["on"].strftime("%H:%M") if st.get("on") else "?",
                  "off": st["off"].strftime("%H:%M") if st.get("off") else "?",
                  "capture_brightness": cfg.get("capture_brightness")},
        "grid": {"rows": grid.get("rows"), "cols": grid.get("cols"),
                 "names": grid.get("names") or {}},
        "canopy": canopy,
        "probe_moisture": probes,
        "soil_temp_f": soiltemp,
        "environment": env,
        "fan": (f"{'on' if fan_state['on'] else 'off'}"
                + (f" at {fan_state['speed']}% ({fan_state['reason']})"
                   if fan_state["on"] else f" ({fan_state['reason']})")
                + f", mode {cfg.get('fan_mode', 'auto')}") if FAN_HW else None,
        "pressure_trend": pressure_tendency(),
        "light_metrics": {"ppfd": ppfd_from_lux((snap.get("lux") or (None, None))[1]),
                          "dli": dli_today()},
        "planting": planting,
        "germination": germ_out,
        "units": {"temp": temp_unit(), "press": press_unit()},
        "float": flabel,
        "reservoir": reservoir_state(),
        # pump_state is keyed by tray; sum across trays, latest detail wins
        "pump_today_s": round(sum(s["today_seconds"]
                                  for s in pump_state.values()), 1),
        "pump_last": next((s["last_detail"] for s in
                           sorted(pump_state.values(),
                                  key=lambda s: s["last_run"], reverse=True)
                           if s["last_detail"]), "none"),
        "notes": cfg.get("ai_notes", ""),
    }


def run_report(reason="daily"):
    """Generate one AI report: gather data + latest photo, call the API, store
    the result, and push the summary. Serialized via report_lock."""
    with settings_lock:
        if not settings.get("camera_enabled"):
            return {"ok": False, "error": "camera features are disabled in settings"}
    with report_lock:
        if report_state["generating"]:
            return {"ok": False, "error": "a report is already being generated"}
        report_state["generating"] = True
    try:
        with settings_lock:
            cfg = dict(settings)
        if not ai_report.have_key():
            return {"ok": False, "error": "no API key on the controller"}
        photos = sorted(p for p in TIMELAPSE_DIR.glob("*.jpg")
                        if not p.name.startswith("_"))
        # prefer scheduled frames: a manual (_m) capture can be off-schedule
        # and dark; fall back to manual only when nothing else exists
        sched = [p for p in photos if not p.stem.endswith("_m")]
        photo = (sched or photos)[-1] if photos else None
        result = ai_report.generate(photo, gather_report_data(),
                                    model=cfg.get("ai_model"))
        result["reason"] = reason
        try:
            AI_REPORT_PATH.write_text(json.dumps(result, indent=2))
        except Exception as e:
            print(f"report save error: {e}")
        if result.get("ok") and cfg.get("ai_notify", True):
            rep = result.get("report") or {}
            summary = rep.get("summary") or "Daily report ready."
            health = rep.get("overall_health")
            tag = {"good": "seedling", "watch": "eyes",
                   "problem": "warning"}.get(health, "seedling")
            notify.send("Garden report", summary, tags=tag)
            # Discord: same summary as a colour-coded embed with key details
            fields = []
            g = rep.get("germination") or {}
            if g.get("sprouted") is not None and g.get("total_cells") is not None:
                fields.append({"name": "Germination",
                               "value": f"{g['sprouted']}/{g['total_cells']}",
                               "inline": True})
            if rep.get("growth_stage"):
                fields.append({"name": "Stage", "value": rep["growth_stage"],
                               "inline": True})
            if (rep.get("light") or {}).get("assessment"):
                fields.append({"name": "Light", "value": rep["light"]["assessment"],
                               "inline": True})
            if (rep.get("water") or {}).get("assessment"):
                fields.append({"name": "Water", "value": rep["water"]["assessment"],
                               "inline": True})
            recs = rep.get("recommendations") or []
            if recs:
                fields.append({"name": "Recommendations",
                               "value": "\n".join("\u2022 " + str(r) for r in recs[:4]),
                               "inline": False})
            sp = rep.get("species") or []
            named = [f"{s.get('cell', '?')}: {s.get('guess')}"
                     + (f" ({s.get('confidence')})" if s.get('confidence') else "")
                     for s in sp if s.get('guess') and s.get('guess') != 'unsure']
            if named:
                fields.append({"name": "Species (guesses)",
                               "value": "\n".join(named[:10]), "inline": False})
            discord_alert.send("\U0001F331 Garden report", summary,
                               level=(health or "info"), fields=fields)
        try:
            db.log_event("ai_report",
                         reason + (": ok" if result.get("ok")
                                   else ": " + str(result.get("error"))[:80]))
        except Exception:
            pass
        return result
    finally:
        with report_lock:
            report_state["generating"] = False


def clock_synced():
    """True once the system clock is trustworthy. The Pi Zero 2 W has no RTC, so
    at boot the time is wrong until NTP corrects it. Priming the report schedule
    on that wrong time, then having the clock jump forward past the target, is
    what fires a report on every reboot. systemd-timesyncd (the Pi OS default)
    creates this file once the clock is synced."""
    if os.path.exists("/run/systemd/timesync/synchronized"):
        return True
    return datetime.now().year >= 2025  # fallback for non-timesyncd setups


def report_loop():
    """Run the AI report once a day when the clock crosses ai_report_hour:minute.

    A restart does NOT trigger a report: if the service starts up already past
    today's scheduled time, today is marked done and the last stored report is
    kept as-is. New reports come only from crossing the time while running, or
    from the manual button."""
    last_day = None
    primed = False
    while True:
        try:
            with settings_lock:
                cfg = dict(settings)
            if (cfg.get("ai_enabled") and cfg.get("camera_enabled")
                    and ai_report.have_key() and clock_synced()):
                tz = ZoneInfo(cfg["timezone"])
                now = datetime.now(tz)
                target = (int(cfg.get("ai_report_hour", 8)) * 60
                          + int(cfg.get("ai_report_minute", 0)))
                nowmin = now.hour * 60 + now.minute
                if not primed:
                    # first pass (on a synced clock): if we're already past
                    # today's time, treat today as handled so a restart or
                    # reboot doesn't fire a fresh report
                    if nowmin >= target:
                        last_day = now.date()
                    primed = True
                if nowmin >= target and last_day != now.date():
                    print("running daily AI report")
                    run_report("daily")
                    last_day = now.date()
        except Exception as e:
            print(f"report_loop error: {e}")
        time.sleep(60)


def auto_water_blockers(cfg=None):
    """Reasons auto-watering must not run, per tray -> [reasons].

    Auto-watering decides to pump based on a probe reading, so an untrustworthy
    probe is not a degraded feature, it is a flood or a drought. A tray with any
    blocker is skipped by the loop and cannot be armed from the UI.
    """
    if cfg is None:
        with settings_lock:
            cfg = dict(settings)
    cal = cfg.get("probe_cal") or {}
    snap = db.latest()
    out = {}
    for tray in sorted(set(PUMP_PINS) | set(sensors.FLOAT_PINS)):
        why = []
        if tray not in _pumps:
            why.append("no pump hardware")
        if tray not in sensors.FLOAT_PINS:
            why.append("no float switch (fill would run blind)")
        tcal = cal.get(tray) or {}
        if not tcal or tcal.get("wet") is None or tcal.get("dry") is None:
            why.append("probe not calibrated")
        else:
            fv, _ = probe_volts_filtered(tray, snap)
            if fv is None:
                why.append("no probe reading yet")
            elif probe_cal_flag(fv, tcal):
                why.append("probe reading is outside its calibration range")
        if why:
            out[tray] = why
    return out


POSTFILL_DELAY_S = 20 * 60     # let water wick to the probe before judging


def schedule_postfill(tray):
    """After a successful fill, judge the probe against its wet anchor later.

    Water takes time to wick from the tray to the probe, so the check is
    deferred rather than taken immediately. Drift here is the early warning
    for a degrading probe or a stale calibration.
    """
    with settings_lock:
        cal = ((settings.get("probe_cal") or {}).get(str(tray)) or {}).copy()
    if cal.get("wet") is None:
        return                      # nothing to compare against
    _postfill_due[str(tray)] = {"due": time.time() + POSTFILL_DELAY_S,
                                "cal": cal}


def check_postfill(readings=None):
    """Evaluate any post-fill probe checks that have come due."""
    if not _postfill_due:
        return
    for tray, pending in list(_postfill_due.items()):
        if time.time() < pending["due"]:
            continue
        _postfill_due.pop(tray, None)
        volts, _ = probe_volts_filtered(tray)
        ok, msg = quality.postfill_verdict(volts, pending["cal"])
        if ok is None:
            continue
        postfill_result[tray] = {"ok": bool(ok), "msg": msg, "ts": time.time()}
        print(f"post-fill check tray {tray}: {msg}")
        try:
            db.log_event("probe", f"tray {tray} post-fill: {msg}")
        except Exception:
            pass


def watering_loop():
    """Autonomous watering: when a calibrated probe reads dry and the tray's
    float says it is not full, fill to the float.

    Every guard is deliberate. The trigger is the probe, so a tray whose probe
    is uncalibrated or reading outside its anchors is skipped entirely rather
    than watered on a guess. The fill itself is float-gated with a time cap,
    refuses on an empty reservoir, honours the daily cap, and a fill that caps
    out without the float tripping switches auto_water off (in
    run_pump_until_full). Cooldown exists because soil wicks slowly: probes
    read dry for minutes after a fill has already reached the roots.
    """
    while True:
        time.sleep(60)
        try:
            with settings_lock:
                cfg = dict(settings)
            if not cfg.get("auto_water"):
                continue
            blockers = auto_water_blockers(cfg)
            threshold = float(cfg.get("moisture_threshold_pct", 30))
            cooldown = float(cfg.get("pump_cooldown_min", 30)) * 60
            cal = cfg.get("probe_cal") or {}
            snap = db.latest()
            stf = latest_soil_temp_f(snap)
            for tray in sorted(PUMP_PINS):
                if tray in blockers:
                    continue
                volts, ts = probe_volts_filtered(tray, snap)
                if volts is None:
                    continue
                if time.time() - ts > 3600:
                    continue          # stale reading: do not water on old data
                pct = probe_moisture(volts, cal.get(tray) or {}, stf)
                if pct is None or pct > threshold:
                    continue
                st = pump_state.get(tray) or {}
                if time.time() - st.get("last_run", 0.0) < cooldown:
                    continue
                f = sensors.read_float(tray)
                if f is None or f < 1:
                    continue          # no float, or already full
                ok, msg = run_pump_until_full(tray, reason="auto")
                print(f"auto-water tray {tray} at {pct:.0f}%: {msg}")
                if ok:
                    try:
                        db.log_event("auto_water",
                                     f"tray {tray} at {pct:.0f}% moisture: {msg}")
                    except Exception:
                        pass
                break                 # one pump per pass; re-evaluate next minute
        except Exception as e:
            print(f"watering_loop error: {e}")


def sample_loop():
    """Read all sensors on an interval, log them in one transaction, and run
    daily downsampling. Tolerant: a read failure logs nothing and tries again
    next tick rather than killing the thread."""
    db.init()
    last_prune = 0.0
    while True:
        with settings_lock:
            interval = max(1, int(settings.get("sample_interval_min", 5)))
        try:
            readings = validate_readings(sensors.read_all())
            if readings:
                db.log_many(list(readings.items()))
                run_alerts(readings)
                check_postfill(readings)
        except Exception as e:
            print(f"sample_loop error: {e}")
        # housekeeping once a day: roll raw -> hourly, prune old raw
        now = time.time()
        if now - last_prune > 86400:
            try:
                db.downsample_and_prune()
            except Exception as e:
                print(f"prune error: {e}")
            last_prune = now
        time.sleep(interval * 60)


def set_fan(speed, reason):
    """Drive the fan at `speed` percent (0 = off) and remember why. Airflow
    does two jobs for seedlings: it dries the surface between waterings
    (damping-off is the main killer after germination) and the movement
    thickens stems. A non-zero speed is raised to fan_min_speed, since a fan
    commanded below its stall point hums without actually turning."""
    if not FAN_HW:
        return
    speed = max(0.0, min(100.0, float(speed or 0)))
    if speed > 0:
        with settings_lock:
            floor = float(settings.get("fan_min_speed", 0) or 0)
        speed = max(speed, floor)
    try:
        _fan.value = speed / 100.0
    except Exception as e:
        print(f"fan control error: {e}")
        return
    with state_lock:
        changed = round(fan_state.get("speed", 0)) != round(speed)
        fan_state.update(on=speed > 0, reason=reason, speed=round(speed))
    if changed:
        print(f"fan {round(speed)}% ({reason})")


def fan_should_run(cfg, now, on_time, off_time):
    """Decide the fan's state in auto mode. Returns (on, reason)."""
    rh = None
    try:
        rh = (db.latest().get("humidity") or (None, None))[1]
    except Exception:
        pass
    hum_on = cfg.get("fan_humidity_on", 0)
    if hum_on and rh is not None and rh >= hum_on:
        return True, f"humidity {rh:.0f}%"
    if cfg.get("fan_with_light", True) and on_time and off_time:
        if on_time <= now <= off_time:
            return True, "photoperiod"
    return False, "idle"


# --- sensor data quality state (in memory; nothing here is worth persisting) ---
# sensor -> count of implausible readings rejected in the current session, and
# the reason for the most recent one. Feeds the health score and the dashboard.
_reject_counts = {}
_reject_last = {}
# tray -> pending post-fill probe check: {"due": ts, "cal": {...}}
_postfill_due = {}
# tray -> last post-fill verdict for display
postfill_result = {}
# cached health payload; recomputing per status poll would hammer sqlite
_health_cache = {"ts": 0.0, "data": {}}


def validate_readings(readings):
    """Drop implausible values before they ever reach the database.

    A probe reading the 3.3V rail means the signal is gone, not that the soil
    is bone dry. Letting that into history corrupts the charts, the alert
    thresholds and any calibration done afterwards, and it is indistinguishable
    from real data later. Rejections are counted and logged as events so a
    wiring fault is visible rather than silent.
    """
    clean, dropped = {}, []
    for k, v in readings.items():
        why = quality.check_bounds(k, v)
        if why is None:
            clean[k] = v
            continue
        dropped.append((k, v, why))
        _reject_counts[k] = _reject_counts.get(k, 0) + 1
        _reject_last[k] = why
    for k, v, why in dropped:
        print(f"rejected {k}={v}: {why}")
        try:
            db.log_event("sensor", f"rejected {k}={v}: {why}")
        except Exception:
            pass
    return clean


# sensor -> last-seen unix ts; touched only by the sample thread via run_alerts
_seen_sensors = {}
SEEN_TTL = 3 * 86400    # a sensor silent this long is treated as removed


# binary sensors are legitimately constant for days; a flatline there means
# nothing and accusing them would train you to ignore the rule
NON_STUCK_PREFIXES = ("float:", "reservoir:", "canopy:")


def stuck_sensors(cadence_s):
    """Sensors reporting on time but reporting the same number forever."""
    out = {}
    for key in sorted(_seen_sensors):
        if key.startswith(NON_STUCK_PREFIXES):
            continue
        try:
            vals = db.recent_values(key, n=24, max_age=48 * 3600)
        except Exception:
            continue
        why = quality.check_stuck(vals, cadence_s)
        if why:
            out[key] = why
    return out


def sensor_health(cfg, snap=None, max_age=60):
    """Per-sensor health for the dashboard, cached: recomputing this on every
    15-second status poll would mean a dozen queries per client per poll."""
    now = time.time()
    if now - _health_cache["ts"] < max_age and _health_cache["data"]:
        return _health_cache["data"]
    snap = snap if snap is not None else db.latest()
    cadence = max(1, int(cfg.get("sample_interval_min", 5))) * 60
    cap = max(1, int(cfg.get("capture_interval_min", 10))) * 60
    out = {}
    for key, (ts, _v) in snap.items():
        if key.startswith(("dry:", "growth", "moisture:")):
            continue
        try:
            vals = db.recent_values(key, n=24, max_age=48 * 3600)
        except Exception:
            vals = []
        verdict = (_probe_verdict.get(key[6:], "steady")
                   if key.startswith("probe:") else "steady")
        if key.startswith(NON_STUCK_PREFIXES):
            vals = vals[:1]      # binary sensors are meant to sit still
        score, grade, why = quality.health(
            vals, age_s=now - ts,
            cadence_s=cap if key.startswith("canopy:") else cadence,
            rejects=_reject_counts.get(key, 0), verdict=verdict,
            noise_ref=0.01 if key.startswith("probe:") else None)
        if _reject_last.get(key) and not any("implausible" in w for w in why):
            why.append(_reject_last[key])
        out[key] = {"score": score, "grade": grade, "why": why}
    _health_cache.update(ts=now, data=out)
    return out


def run_alerts(readings):
    """Evaluate the alert rules against this tick's readings and push anything
    that changed state. Never raises: a failure here must not stop sampling."""
    try:
        with settings_lock:
            cfg = dict(settings)
        if not cfg.get("alerts_enabled", True):
            return
        acfg = {
            "sustain_seconds": cfg.get("alert_sustain_min", 10) * 60,
            "cooldown_seconds": cfg.get("alert_cooldown_hours", 6) * 3600,
            "soil_temp_high_f": cfg.get("soil_temp_high_f", 0),
            "soil_temp_low_f": cfg.get("soil_temp_low_f", 0),
            "probe_dry_pct": cfg.get("alert_dry_pct", 15),
            "humidity_high": cfg.get("alert_humidity_high", 80),
            "dli_low": cfg.get("alert_dli_low", 4),
            "dli_high": cfg.get("alert_dli_high", 0),
        }
        snap = dict(readings)

        # the day's light total, judged once the day is over: only within a
        # window after lights-off is _dli present, so the rule fires at most
        # once per evening and a mid-morning low total can't false-alarm
        with state_lock:
            off_t = state.get("off")
        if off_t is not None and (cfg.get("alert_dli_low")
                                  or cfg.get("alert_dli_high")):
            now_dt = datetime.now(off_t.tzinfo)
            if off_t <= now_dt <= off_t + timedelta(minutes=45):
                d = dli_today()
                if d is not None:
                    snap["_dli"] = d

        # tray moisture as percentages, using each tray's calibration
        pcal = cfg.get("probe_cal") or {}
        names = cfg.get("probe_names") or {}
        stf = latest_soil_temp_f()
        moist = {}
        for k, v in readings.items():
            if not k.startswith("probe:"):
                continue
            t = k[6:]
            fv, _ = probe_volts_filtered(t)      # transient-rejected, not raw
            pct, approx = probe_moisture_any(fv if fv is not None else v,
                                             pcal.get(t) or {}, stf)
            if pct is not None and not approx:      # only alert on real calibration
                moist[names.get(t, f"Tray {t}")] = pct
        snap["_moisture"] = moist

        with state_lock:
            snap["_camera_fails"] = camera["fails"] if cfg.get("camera_enabled") else 0

        snap["_fill_failed"] = fill_failure["msg"]
        snap["_reservoir"] = reservoir_state()
        # a plug that stops responding means the light is stuck wherever it
        # was; the sustain window absorbs a wifi blip, repeated failures do not
        snap["_plug_failed"] = (kasa_state["error"]
                                if (light_backend(cfg) == "kasa"
                                    and kasa_state["fails"] >= 2) else "")

        # sensors that have gone quiet: keys seen recently but missing from
        # this read. Tracked in memory only (persisting it once made a removed
        # sensor alert forever), and aged out after SEEN_TTL so unwiring a
        # device stops the reminders after a few days instead of never.
        now_ts = time.time()
        for k in readings:
            _seen_sensors[k] = now_ts
        for k in [k for k, t in _seen_sensors.items()
                  if now_ts - t > SEEN_TTL]:
            _seen_sensors.pop(k, None)
        snap["_stale"] = sorted(set(_seen_sensors) - set(readings))
        # reporting on time but not measuring: invisible to the stale check
        snap["_stuck"] = stuck_sensors(
            max(1, int(cfg.get("sample_interval_min", 5))) * 60)

        for action, key, title, message, level in alerts.check_all(
                snap, acfg, "C" if cfg.get("units") == "metric" else "F"):
            prefix = {"fire": "", "remind": "Still: ", "clear": "Resolved: "}[action]
            discord_alert.send(prefix + title, message,
                               level="good" if action == "clear" else level)
            print(f"alert {action}: {key}")
    except Exception as e:
        print(f"alert check error: {e}")


# the only settings that move the light window; anything else (a planting-map
# edit, a saved light curve) must not trigger a recompute or a log line
SCHED_KEYS = ("latitude", "longitude", "timezone", "schedule_mode",
              "fixed_on", "fixed_off", "duration_hours", "duration_end",
              "sunrise_offset_min", "sunset_offset_min")


def control_loop():
    seen = None
    sunrise = sunset = on_time = off_time = None
    while True:
        with settings_lock:
            cfg = dict(settings)
        tz = ZoneInfo(cfg["timezone"])
        now = datetime.now(tz)
        key = (now.date(), tuple(cfg.get(k) for k in SCHED_KEYS))
        if key != seen:
            seen = key
            sunrise, sunset, on_time, off_time = sun_window(cfg, now.date(), tz)
            print(f"{now.date()}: on {on_time:%H:%M}, off {off_time:%H:%M} "
                  f"({cfg['latitude']}, {cfg['longitude']}, {cfg['timezone']})")
        b = brightness_for(cfg, now, on_time, off_time)
        ov = cfg.get("light_override", "auto")
        if ov == "on":
            b = max(0, min(100, int(cfg.get("manual_bright", cfg["max_bright"]))))
        elif ov == "off":
            b = 0
        if not capturing and not sweep_state["running"]:
            set_brightness(b)      # a sweep owns the light while it runs

        mode = cfg.get("fan_mode", "auto")
        if mode == "on":
            set_fan(cfg.get("fan_speed", 100), "manual")
        elif mode == "off":
            set_fan(0, "manual")
        else:
            want, why = fan_should_run(cfg, now, on_time, off_time)
            set_fan(cfg.get("fan_auto_speed", 70) if want else 0, why)
        with state_lock:
            state.update(brightness=b, on=on_time, off=off_time,
                         sunrise=sunrise, sunset=sunset, override=ov)
        wake.wait(timeout=LOOP_SECONDS)
        wake.clear()


# --------------------------- capture loop ---------------------------

# Each entry: config flag -> (v4l2 auto control, value for auto, value for
# manual, the manual controls it unlocks). Auto is offered because it is what
# people expect, but note that for a timelapse it produces visible flicker:
# the camera re-decides exposure and white balance every frame, so brightness
# and colour shift between shots and the per-cell analysis moves with them.
USB_AUTO_GROUPS = {
    "usb_auto_focus": ("focus_automatic_continuous", 1, 0, ("focus_absolute",)),
    "usb_auto_exposure_on": ("auto_exposure", 3, 1,
                             ("exposure_time_absolute", "gain")),
    "usb_auto_white_balance": ("white_balance_automatic", 1, 0,
                               ("white_balance_temperature",)),
}


def _usb_apply_controls(cfg, dev):
    """Push camera settings before a capture.

    Applied in two passes: a manual control stays flagged `inactive` and
    rejects writes until its automatic counterpart has been switched off, so
    every auto flag must land before any manual value.
    """
    autos, manuals = [], []
    for flag, (ctrl, on_val, off_val, dependents) in USB_AUTO_GROUPS.items():
        auto = bool(cfg.get(flag, False))
        autos.append(f"{ctrl}={on_val if auto else off_val}")
        if auto:
            continue                      # let the camera decide these
        for dep in dependents:
            val = cfg.get("usb_" + dep)
            if val not in (None, ""):
                manuals.append(f"{dep}={int(val)}")
    for extra in ("brightness", "contrast", "saturation"):
        val = cfg.get("usb_" + extra)
        if val not in (None, ""):
            manuals.append(f"{extra}={int(val)}")
    for group in (autos, manuals):
        if not group:
            continue
        args = []
        for c in group:
            args += ["-c", c]
        subprocess.run(["v4l2-ctl", "-d", dev] + args,
                       capture_output=True, timeout=10)


def _usb_capture(cfg, out_path, width, height, warmup=None):
    """Grab one MJPEG frame from a UVC camera via v4l2-ctl.

    UVC sensors need a few frames before auto-gain settles, and the very first
    frame after opening the device is frequently dark or torn. Capturing a
    short burst and keeping the last frame costs a second and removes that
    whole class of bad photo.
    """
    dev = cfg.get("usb_device", "/dev/video0")
    if not Path(dev).exists():
        return False, f"{dev} not present"
    _usb_apply_controls(cfg, dev)
    n = int(cfg.get("usb_warmup_frames", 4) if warmup is None else warmup)
    n = max(1, min(20, n))
    tmp = Path(str(out_path) + ".raw")
    cmd = ["v4l2-ctl", "-d", dev,
           "--set-fmt-video=width=%d,height=%d,pixelformat=MJPG" % (width, height),
           "--stream-mmap", "--stream-count=%d" % n, "--stream-to=%s" % tmp]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        return False, "capture timed out"
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return False, (r.stderr.decode(errors="replace")[-200:].strip()
                       or "v4l2-ctl returned no frames")
    # The stream is n JPEGs back to back; keep the last, which is the settled one.
    try:
        data = tmp.read_bytes()
        starts = []
        i = data.find(b"\xff\xd8")
        while i != -1:
            starts.append(i)
            i = data.find(b"\xff\xd8", i + 2)
        if not starts:
            tmp.unlink(missing_ok=True)
            return False, "no JPEG frame in the stream"
        Path(out_path).write_bytes(data[starts[-1]:])
    finally:
        tmp.unlink(missing_ok=True)
    return True, None


def _postprocess_file(path, cfg):
    """Rotate a just-captured JPEG in place.

    Rotation is baked in because it is not a matter of interpretation: the
    camera is mounted upside down and every consumer wants it the right way up.
    Flattening deliberately is NOT baked in -- it is applied when the image is
    served, so the stored frame stays raw and the grid corners can always be
    re-dragged against the real scene.

    Failure is non-fatal: the original frame is kept and the reason is logged.
    """
    degrees = int(cfg.get("cam_rotate", 0) or 0)
    if degrees not in (90, 180, 270):
        return
    try:
        import cv2
        img = cv2.imread(str(path))
        if img is None:
            return
        if degrees in (90, 180, 270):
            img = cv2.rotate(img, {90: cv2.ROTATE_90_CLOCKWISE,
                                   180: cv2.ROTATE_180,
                                   270: cv2.ROTATE_90_COUNTERCLOCKWISE}[degrees])
        cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    except Exception as e:
        print(f"post-process failed ({e}); keeping the frame as captured")


# kept for callers that only need the rotation
def _rotate_file(path, degrees):
    _postprocess_file(path, {"cam_rotate": degrees})


def take_photo(cfg, now, manual=False):
    """Capture one frame. `manual` tags the filename with an _m suffix so the
    daily AI report can prefer scheduled frames: a manual shot at midnight is
    a dark off-schedule photo that would otherwise become the report's input."""
    global capturing
    capturing = True
    saved = None
    try:
        set_brightness(cfg["capture_brightness"])
        time.sleep(2)  # let light and auto-exposure settle
        suffix = "_m" if manual else ""
        fname = TIMELAPSE_DIR / f"{now:%Y%m%d_%H%M%S}{suffix}.jpg"
        cw = int(cfg.get("cam_width", 4608))
        ch = int(cfg.get("cam_height", 2592))
        if cfg.get("camera_backend", "rpicam") == "usb":
            ok, err = _usb_capture(cfg, fname,
                                   int(cfg.get("usb_width", 2048)),
                                   int(cfg.get("usb_height", 1536)))
            if ok:
                _postprocess_file(fname, cfg)
                make_thumb(fname, cfg)
                saved = fname
                _camera_ok()
            else:
                print(f"capture failed: {err}")
                _camera_fail(err)
            return saved

        cmd = ["rpicam-still", "-n", "-o", str(fname), "-t", "2000"]
        try:
            roi = parse_roi(cfg.get("roi", ""))
        except ValueError:
            roi = None
        if roi:
            x, y, w, h = roi
            cmd += ["--roi", f"{x},{y},{w},{h}",
                    "--width", str(int(cw * w) // 2 * 2),
                    "--height", str(int(ch * h) // 2 * 2)]
        else:
            cmd += ["--width", str(cw), "--height", str(ch)]
        r = subprocess.run(cmd, capture_output=True, timeout=90)
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace")[-300:]
            print(f"capture failed: {err}")
            _camera_fail(err.strip().splitlines()[-1] if err.strip() else "capture failed")
        else:
            _postprocess_file(fname, cfg)
            make_thumb(fname, cfg)
            saved = fname
            _camera_ok()
    except Exception as e:
        print(f"capture error: {e}")
        _camera_fail(str(e))
    finally:
        capturing = False
        wake.set()  # control loop restores scheduled brightness now
    return saved


def record_growth(path, cfg, now):
    """Measure per-tray canopy coverage from a just-captured photo and log it.
    Per-cell measurement was retired: seedlings spill across cell lines and the
    attribution becomes fiction, while tray boundaries are physical. Runs
    growth.py as a subprocess so OpenCV memory is freed afterwards."""
    grid = cfg.get("grid") or {}
    if not grid.get("corners"):
        return
    # tray column spans, left to right, mirroring how the trays sit under the
    # camera (tray 1 leftmost)
    trays = [{"id": tid, "cols": int((t or {}).get("cols", 3))}
             for tid, t in sorted((cfg.get("trays") or {}).items())]
    payload = {"corners": grid["corners"],
               "rows": grid.get("rows", 4), "cols": grid.get("cols", 4),
               "trays": trays,
               "rectify": bool(cfg.get("cam_rectify", True)),
               # the file is rotated at capture time, so analysis must not
               # rotate it a second time
               "rotate": 0}
    try:
        r = subprocess.run([sys.executable, str(GROWTH_SCRIPT), str(path),
                            json.dumps(payload)],
                           capture_output=True, timeout=120)
        out = json.loads((r.stdout or b"{}").decode(errors="replace") or "{}")
    except Exception as e:
        print(f"growth analyze error: {e}")
        return
    if not out.get("ok"):
        if out.get("error"):
            print(f"growth: {out['error']}")
        return
    readings = validate_readings(out.get("readings") or {})
    if readings:
        db.log_many(list(readings.items()), ts=int(now.timestamp()))


def capture_loop():
    last_shot = None
    while True:
        # opportunistic thumbnail backfill, at most one per tick
        missing = next((p for p in sorted(TIMELAPSE_DIR.glob("*.jpg"))
                        if not (THUMB_DIR / p.name).exists()), None)
        if missing:
            make_thumb(missing)
        with settings_lock:
            cfg = dict(settings)
        if cfg.get("camera_enabled") and cfg["capture_enabled"]:
            tz = ZoneInfo(cfg["timezone"])
            now = datetime.now(tz)
            with state_lock:
                on_time, off_time = state["on"], state["off"]
            in_day = on_time is not None and on_time <= now <= off_time
            due = (last_shot is None or
                   now - last_shot >= timedelta(minutes=cfg["capture_interval_min"]))
            if in_day and due:
                last_shot = now
                with capture_lock:
                    path = take_photo(cfg, now)
                if path:
                    record_growth(path, cfg, now)
        time.sleep(15)


# ----------------------------- video render -----------------------------

def _flatten_frames_to(dest, frames, cfg):
    """Write rectified copies of `frames` into `dest`, numbered in order.

    The timelapse is rendered from flattened frames so the video matches what
    the dashboard shows, but the originals on disk stay raw: that is what keeps
    the grid corners re-draggable against the real scene. Frames that cannot be
    rectified are copied through unchanged rather than dropped, so a bad frame
    leaves a blip instead of a gap in the timeline.
    """
    import shutil
    grid = cfg.get("grid") or {}
    corners = grid.get("corners")
    if not corners or len(corners) != 4:
        return None
    try:
        import cv2
    except Exception:
        return None
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.jpg"):
        old.unlink()
    cols, rows = int(grid.get("cols", 4)), int(grid.get("rows", 4))
    for i, src in enumerate(frames):
        out = dest / f"{i:06d}.jpg"
        try:
            img = cv2.imread(str(src))
            if img is None:
                raise ValueError("unreadable")
            warped = growth_mod.rectify(img, corners, cols=cols, rows=rows)
            cv2.imwrite(str(out), warped, [cv2.IMWRITE_JPEG_QUALITY, 88])
        except Exception:
            shutil.copyfile(src, out)
    return dest


def render_worker():
    import time as _t
    t0 = _t.monotonic()
    frames = sorted(p for p in TIMELAPSE_DIR.glob("*.jpg")
                    if not p.name.startswith("_"))
    with render_lock:
        render.update(state="running", frames=len(frames),
                      started=datetime.now(ZoneInfo(settings["timezone"])).isoformat(),
                      elapsed=None, msg=f"Rendering {len(frames)} frames...")
    flat_dir = None
    try:
        with settings_lock:
            cfg_r = dict(settings)
        if cfg_r.get("timelapse_flatten", True):
            with render_lock:
                render["msg"] = f"Flattening {len(frames)} frames..."
            flat_dir = _flatten_frames_to(TIMELAPSE_DIR / "_flat", frames, cfg_r)
        src_glob = str((flat_dir or TIMELAPSE_DIR) / "*.jpg")
        tmp = TIMELAPSE_DIR / "_render_tmp.mp4"
        # Encode pass: small footprint so the 512MB Zero never OOMs.
        # 1280-wide, ultrafast, single thread, no faststart here (the
        # +faststart second pass rewrites the whole file in memory and is
        # what tips the box over). We add faststart as a cheap remux after.
        r = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y",
             "-framerate", "24", "-pattern_type", "glob",
             "-i", src_glob,
             # JPEG stills are full-range (yuvj420p/pc); browsers render that as
             # black. Remap to limited-range yuv420p and tag it. Height is forced
             # to a multiple of 16 (-16, not -2): a non-mod16 height makes the
             # encoder signal a crop that some hardware decoders render as black.
             "-vf", "scale=1280:-16:in_range=full:out_range=tv",
             "-c:v", "libx264", "-preset", "ultrafast",
             "-crf", "24", "-threads", "1",
             "-pix_fmt", "yuv420p", "-color_range", "tv",
             # Fully specify the colour metadata (BT.601, matching the JPEG
             # source). An unspecified matrix makes some hardware decoders
             # (VLC's, browsers') render the video as black.
             "-colorspace", "smpte170m", "-color_primaries", "smpte170m",
             "-color_trc", "smpte170m",
             str(tmp)],
            capture_output=True, timeout=3600)
        # Faststart as a stream-copy remux: no re-encode, trivial memory.
        if r.returncode == 0 and tmp.exists():
            r2 = subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-i", str(tmp),
                 "-c", "copy", "-movflags", "+faststart", str(VIDEO_PATH)],
                capture_output=True, timeout=600)
            tmp.unlink(missing_ok=True)
            if r2.returncode != 0:
                r = r2  # surface the remux error below
        dt = _t.monotonic() - t0
        if r.returncode == 0 and VIDEO_PATH.exists():
            mb = VIDEO_PATH.stat().st_size / 1e6
            with render_lock:
                render.update(state="done", elapsed=round(dt, 1),
                              msg=f"{len(frames)} frames, {mb:.1f} MB, {dt:.0f}s")
        else:
            err = r.stderr.decode(errors="replace")[-200:]
            with render_lock:
                render.update(state="error", elapsed=round(dt, 1),
                              msg=err or "ffmpeg failed")
    except Exception as e:
        with render_lock:
            render.update(state="error", elapsed=round(_t.monotonic() - t0, 1),
                          msg=str(e))
    finally:
        if flat_dir and flat_dir.exists():
            # scratch only: the SD card cannot afford a second copy of the set
            import shutil
            shutil.rmtree(flat_dir, ignore_errors=True)


def start_render():
    with render_lock:
        if render["state"] == "running":
            return False
        render.update(state="running", msg="Starting...")
    threading.Thread(target=render_worker, daemon=True).start()
    return True


# ----------------------------- dashboard -----------------------------

app = Flask(__name__)

SECRET_PATH = Path(__file__).with_name(".secret")
def _load_secret():
    try:
        s = SECRET_PATH.read_text().strip()
        if s:
            return s
    except Exception:
        pass
    s = secrets.token_hex(32)
    try:
        SECRET_PATH.write_text(s)
        SECRET_PATH.chmod(0o600)
    except Exception:
        pass
    return s

app.secret_key = _load_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(settings.get("cookie_secure", True)),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


def auth_enabled():
    with settings_lock:
        return bool(settings.get("password_hash"))


def is_authed():
    # If no password is configured, the dashboard is open (legacy behaviour).
    return (not auth_enabled()) or bool(session.get("authed"))


def require_auth(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not is_authed():
            return jsonify(error="login required"), 401
        return fn(*a, **k)
    return wrapper


@app.route("/api/probe_cal", methods=["POST"])
@require_auth
def probe_cal_set():
    """Capture a tray probe's current raw reading as its 'wet' (100%) or 'dry'
    (0%) anchor. Reads the probe live so the anchor reflects the soil right now."""
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", ""))
    point = data.get("point")
    if tray not in ("1", "2") or point not in ("wet", "dry"):
        return jsonify(ok=False, error="tray must be 1|2 and point wet|dry"), 200
    live, spread = sensors.probe_spread(tray)
    if live is None:
        return jsonify(ok=False, error="no reading from that probe"), 200
    with settings_lock:
        cal = settings.setdefault("probe_cal", {})
        cal.setdefault(tray, {})[point] = live
        save_config()
    # a wide spread means noise on the analog run; the anchor is unreliable
    return jsonify(ok=True, tray=tray, point=point, volts=live,
                   spread=spread, noisy=bool(spread and spread > 0.05))


@app.route("/api/report")
def api_report_get():
    """Latest stored AI report (or nulls if none yet), plus generating flag."""
    try:
        data = json.loads(AI_REPORT_PATH.read_text())
    except Exception:
        data = {"ok": None, "report": None, "ts": None}
    data["generating"] = report_state["generating"]
    data["have_key"] = ai_report.have_key()
    return jsonify(data)


@app.route("/api/ai_settings", methods=["POST"])
@require_auth
def update_ai_settings():
    data = request.get_json(silent=True) or {}
    with settings_lock:
        if "ai_enabled" in data:
            settings["ai_enabled"] = bool(data["ai_enabled"])
        if "ai_notify" in data:
            settings["ai_notify"] = bool(data["ai_notify"])
        if "ai_report_hour" in data:
            try:
                settings["ai_report_hour"] = max(0, min(23, int(data["ai_report_hour"])))
            except (TypeError, ValueError):
                pass
        if "ai_report_minute" in data:
            try:
                settings["ai_report_minute"] = max(0, min(59, int(data["ai_report_minute"])))
            except (TypeError, ValueError):
                pass
        if "ai_notes" in data:
            settings["ai_notes"] = str(data["ai_notes"])[:1000]
        save_config()
        out = {k: settings[k] for k in
               ("ai_enabled", "ai_notify", "ai_report_hour", "ai_report_minute", "ai_notes")}
    return jsonify(ok=True, **out)


@app.route("/api/report", methods=["POST"])
@require_auth
def api_report_run():
    if not ai_report.have_key():
        return jsonify(ok=False, error="no API key set on the controller"), 200
    return jsonify(run_report("manual"))


@app.route("/api/float")
def api_float():
    return jsonify(floats={t: sensors.read_float(t) for t in sensors.FLOAT_PINS})


@app.route("/api/login", methods=["POST"])
def login():
    with settings_lock:
        h = settings.get("password_hash", "")
    if not h:
        return jsonify(ok=True, authed=True)  # no password set -> open
    pw = (request.get_json(silent=True) or {}).get("password", "")
    time.sleep(0.5)  # crude throttle against rapid guessing
    if check_password_hash(h, pw):
        session["authed"] = True
        session.permanent = True
        return jsonify(ok=True, authed=True)
    return jsonify(ok=False, error="wrong password"), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify(ok=True, authed=False)


def _asset_ver():
    """Static asset version from file mtimes: browsers refetch app.js/style.css
    the moment either changes, so a deploy can never leave a stale script
    running against new markup."""
    try:
        st = Path(app.static_folder)
        return int(max((st / f).stat().st_mtime for f in ("app.js", "style.css")))
    except Exception:
        return 0


@app.route("/")
def index():
    return render_template("index.html", tzs=TIMEZONES, v=_asset_ver())


@app.route("/favicon.ico")
def favicon():
    # browsers request this at the site root regardless of the <link> tags
    return send_from_directory(app.static_folder, "favicon.ico",
                               mimetype="image/x-icon")


@app.route("/site.webmanifest")
def webmanifest():
    """Home-screen metadata. Served from a route rather than /static because
    Flask's mimetype guess for .webmanifest is application/octet-stream, which
    some browsers refuse."""
    return Response(json.dumps({
        "name": "OpenSeedling",
        "short_name": "Seedling",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f0f6ea",
        "theme_color": "#f0f6ea",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"},
        ],
    }), mimetype="application/manifest+json")


@app.route("/photo/latest")
def latest_photo():
    count, latest, _ = photo_inventory()
    if not count:
        return "no photos yet", 404
    resp = send_file(latest, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/photos")
def photo_list():
    return jsonify(names=[p.name for p in sorted(THUMB_DIR.glob("*.jpg"))])


@app.route("/thumb/<name>")
def thumb(name):
    resp = send_from_directory(THUMB_DIR, name, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/api/grid", methods=["POST"])
@require_auth
def update_grid():
    data = request.get_json(silent=True) or {}
    try:
        corners = data["corners"]
        if len(corners) != 4:
            raise ValueError
        corners = [[float(x), float(y)] for x, y in corners]
        for x, y in corners:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError
        rows = int(data.get("rows", 4))
        cols = int(data.get("cols", 4))
        if not (1 <= rows <= 12 and 1 <= cols <= 12):
            raise ValueError
        names = {str(k): str(v)[:40] for k, v in (data.get("names") or {}).items()}
        show = bool(data.get("show", True))
        locked = bool(data.get("locked", False))
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Invalid grid data."), 400
    with settings_lock:
        cur = settings.get("grid") or {}
        cur_locked = bool(cur.get("locked", False))
        # Server-side lock: while locked, the geometry cannot be changed. A
        # stale tab or stray save can't move a locked grid; you must unlock
        # first (which leaves the geometry untouched).
        if cur_locked:
            def _same(a, b):
                try:
                    return all(abs(p[i] - q[i]) < 1e-9
                               for p, q in zip(a, b) for i in (0, 1))
                except Exception:
                    return False
            geom_changed = (not _same(corners, cur.get("corners", [])) or
                            rows != cur.get("rows") or cols != cur.get("cols"))
            if geom_changed:
                return jsonify(error="grid is locked; unlock before editing"), 409
        settings["grid"] = {"corners": corners, "rows": rows, "cols": cols,
                            "names": names, "show": show, "locked": locked}
        save_config()
    # audit trail so a future revert can be traced to who/when/what
    try:
        db.log_event("grid", f"saved corners[0]={corners[0]} "
                             f"rows={rows} cols={cols} locked={locked}")
    except Exception:
        pass
    print(f"grid saved: corners[0]={corners[0]} rows={rows} cols={cols} locked={locked}")
    return jsonify(ok=True)


@app.route("/api/detect_grid", methods=["POST"])
@require_auth
def detect_grid():
    count, latest, _ = photo_inventory()
    if not count or latest is None:
        return jsonify(ok=False, error="No photo to detect from yet."), 200
    helper = Path(__file__).with_name("detect_corners.py")
    if not helper.exists():
        return jsonify(ok=False, error="Detector not installed."), 200
    try:
        r = subprocess.run([sys.executable, str(helper), str(latest)],
                           capture_output=True, timeout=60)
        out = r.stdout.decode(errors="replace").strip()
        return jsonify(json.loads(out) if out else
                       {"ok": False, "error": "Detector returned nothing."}), 200
    except Exception as e:
        return jsonify(ok=False, error=f"Detector error: {e}"), 200


@app.route("/api/light", methods=["POST"])
@require_auth
def api_light():
    """Manual light hold. 'on' forces manual_bright, 'off' forces dark, 'auto'
    hands control back to the sun schedule. An optional 'brightness' (0-100)
    sets the level the 'on' hold uses."""
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    bright = data.get("brightness")
    if mode is not None and mode not in ("auto", "on", "off"):
        return jsonify(ok=False, error="mode must be auto|on|off"), 200
    if bright is not None:
        try:
            bright = max(0, min(100, int(bright)))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="brightness must be 0-100"), 200
    if mode is None and bright is None:
        return jsonify(ok=False, error="nothing to set"), 200
    with settings_lock:
        if mode is not None:
            settings["light_override"] = mode
        if bright is not None:
            settings["manual_bright"] = bright
        save_config()
        cur_mode = settings["light_override"]
        cur_bright = settings["manual_bright"]
    wake.set()          # apply now instead of waiting for the next loop pass
    if mode is not None:
        db.log_event("light", f"override set to {mode}")
    return jsonify(ok=True, mode=cur_mode, brightness=cur_bright)


@app.route("/api/probe_tempcomp", methods=["POST"])
@require_auth
def probe_tempcomp():
    """Estimate (and optionally apply) a tray probe's temperature-drift
    coefficient from logged data. Run it over a warm, no-watering window."""
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", ""))
    if tray not in sensors.PROBE_CHANNELS:
        return jsonify(ok=False,
                       error=f"no probe wired for tray {tray} "
                             f"(probes: {', '.join(sorted(sensors.PROBE_CHANNELS))})"), 200
    try:
        hours = max(6, min(720, int(data.get("hours", 48))))
    except (TypeError, ValueError):
        hours = 48
    res = estimate_temp_comp(tray, hours)
    if not res.get("ok"):
        return jsonify(res), 200
    if res["span"] < 5:
        res["warning"] = (f"soil temp only varied {res['span']}F; "
                          "the estimate is weak until it swings more")
    if data.get("apply"):
        with settings_lock:
            cal = settings.setdefault("probe_cal", {}).setdefault(tray, {})
            cal["temp_comp"] = {"coeff": res["coeff"], "ref_f": TEMP_COMP_REF_F}
            save_config()
        res["applied"] = True
    return jsonify(res)


@app.route("/api/trays", methods=["POST"])
@require_auth
def api_trays():
    """Save what's planted in each cell. Body: {"tray": "1"|"2", "cells":
    {"A1": {"seed": str, "equipment": str, "planted": "YYYY-MM-DD",
            "sprouted": "YYYY-MM-DD", "archived": "YYYY-MM-DD"}, ...}}.
    `sprouted` records emergence (so days-to-germinate is measurable) and
    `archived` marks a cell transplanted out. Empty cells are dropped."""
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", ""))
    cells = data.get("cells")
    label = data.get("label")
    with settings_lock:
        known = set(settings.get("trays") or {})
    if tray not in known:
        return jsonify(ok=False, error=f"no such tray: {tray}"), 200
    if not isinstance(cells, dict):
        return jsonify(ok=False, error="cells must be an object"), 200
    clean = {}
    for cid, v in cells.items():
        if not isinstance(v, dict) or not re.fullmatch(r"[A-Z]\d{1,2}", str(cid)):
            continue
        seed = str(v.get("seed", "")).strip()[:60]
        equip = str(v.get("equipment", "")).strip()[:60]

        def _date(field):
            d = str(v.get(field, "") or "").strip()[:10]
            return d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else ""

        planted, sprouted, archived = _date("planted"), _date("sprouted"), _date("archived")
        source = str(v.get("source", "")).strip()[:60]
        notes = str(v.get("notes", "")).strip()[:200]
        try:
            count = max(0, min(99, int(v.get("count") or 0)))
        except (TypeError, ValueError):
            count = 0
        # which fields this cell shows; display-only, so an empty equipment
        # row can be hidden on the cells that will never have one
        _fields = ("seed", "equipment", "planted", "sprouted",
                   "source", "count", "notes")
        hide = [f for f in (v.get("hide") or [])
                if f in _fields or (f.startswith("!") and f[1:] in _fields)][:14]
        if (seed or equip or planted or sprouted or archived or hide
                or source or notes or count):
            clean[cid] = {"seed": seed, "equipment": equip, "planted": planted,
                          "sprouted": sprouted, "archived": archived,
                          "source": source, "count": count, "notes": notes,
                          "hide": hide}
    with settings_lock:
        trays = settings.setdefault("trays", {})
        t = trays.setdefault(tray, {"label": f"Tray {tray}", "rows": 4,
                                    "cols": 3, "cells": {}})
        t["cells"] = clean
        if label is not None:
            t["label"] = str(label).strip()[:40] or f"Tray {tray}"
        save_config()
    return jsonify(ok=True, tray=tray, count=len(clean))


MAX_TRAYS, MAX_DIM = 8, 12


def _cell_ids(rows, cols):
    return {f"{chr(65 + c)}{r}" for r in range(1, rows + 1) for c in range(cols)}


@app.route("/api/reset_timelapse", methods=["POST"])
@require_auth
def api_reset_timelapse():
    """Archive the current timelapse and start a fresh one.

    Photos are MOVED, not deleted: a grow run is not reproducible, so the old
    frames go to timelapse_archive/<timestamp>/ and can be restored or removed
    by hand once you are sure. Thumbnails and the rendered video are rebuilt
    from scratch, and the camera-derived per-cell history is optionally cleared
    since it was measured against the old geometry.
    """
    data = request.get_json(silent=True) or {}
    if not data.get("confirm"):
        count, _, _ = photo_inventory()
        return jsonify(ok=False, needs_confirm=True, photos=count,
                       error=f"{count} photos would be archived"), 200
    stamp = datetime.now(ZoneInfo(settings["timezone"])).strftime("%Y%m%d_%H%M%S")
    dest = ARCHIVE_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for ph in sorted(TIMELAPSE_DIR.glob("*.jpg")):
        if ph.name.startswith("_"):
            continue
        try:
            ph.rename(dest / ph.name)
            moved += 1
        except Exception as e:
            print(f"archive {ph.name} failed: {e}")
    for t in THUMB_DIR.glob("*.jpg"):
        t.unlink(missing_ok=True)
    if VIDEO_PATH.exists():
        try:
            VIDEO_PATH.rename(dest / VIDEO_PATH.name)
        except Exception:
            VIDEO_PATH.unlink(missing_ok=True)
    cleared = 0
    if data.get("clear_readings"):
        # camera-derived series only: probes, temperature and light stay
        for prefix in ("canopy:", "growth:", "growth_px:", "dry:", "moisture:"):
            try:
                cleared += db.delete_series_prefix(prefix)
            except Exception as e:
                print(f"clear {prefix} failed: {e}")
    with render_lock:
        render.update(state="idle", frames=0, msg="", started=None, elapsed=None)
    print(f"timelapse reset: {moved} photos archived to {dest}, "
          f"{cleared} readings cleared")
    return jsonify(ok=True, archived=moved, cleared=cleared, path=str(dest))


@app.route("/api/rebuild_thumbs", methods=["POST"])
@require_auth
def api_rebuild_thumbs():
    """Regenerate every thumbnail from its source photo.

    Needed after the grid corners move or flattening is toggled: thumbnails are
    built once at capture time, so existing ones keep whatever geometry was
    current then and the scrubber would show a mix.
    """
    def work():
        with settings_lock:
            cfg = dict(settings)
        photos = [p for p in sorted(TIMELAPSE_DIR.glob("*.jpg"))
                  if not p.name.startswith("_")]
        for old in THUMB_DIR.glob("*.jpg"):
            old.unlink(missing_ok=True)
        for i, ph in enumerate(photos):
            make_thumb(ph, cfg)
        print(f"rebuilt {len(photos)} thumbnails")
    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True, started=True)


@app.route("/api/purge_series", methods=["POST"])
@require_auth
def api_purge_series():
    """Delete a sensor's logged history.

    Needed when a schema changes: the old `moisture:` per-cell keys are no
    longer written by anything, so they sit on the dashboard forever showing
    whatever they last read. Deliberately explicit rather than automatic --
    this destroys data.
    """
    data = request.get_json(silent=True) or {}
    prefix = str(data.get("prefix", "")).strip()
    if not prefix or len(prefix) < 3:
        return jsonify(ok=False, error="prefix required (3+ chars)"), 200
    try:
        n = db.delete_series_prefix(prefix)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 200
    print(f"purged {n} readings with prefix {prefix!r}")
    return jsonify(ok=True, deleted=n, prefix=prefix)


@app.route("/api/tray_layout", methods=["POST"])
@require_auth
def api_tray_layout():
    """Add, remove, rename or resize a tray.

    Body: {"action": "add"} | {"action": "remove", "tray": id}
        | {"action": "resize", "tray": id, "rows": n, "cols": n, "label": str}

    Resizing keeps every cell that still fits the new grid. Cells that fall
    outside it are reported as `dropped`, and are only discarded when the
    caller passes confirm=true, so a mis-tap cannot silently delete planting
    records.
    """
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "resize"))
    with settings_lock:
        trays = settings.setdefault("trays", {})

        if action == "add":
            if len(trays) >= MAX_TRAYS:
                return jsonify(ok=False, error=f"at most {MAX_TRAYS} trays"), 200
            nid = next(str(i) for i in range(1, MAX_TRAYS + 2) if str(i) not in trays)
            trays[nid] = {"label": f"Tray {nid}", "rows": 4, "cols": 3, "cells": {}}
            save_config()
            return jsonify(ok=True, tray=nid, added=True)

        tray = str(data.get("tray", ""))
        if tray not in trays:
            return jsonify(ok=False, error="no such tray"), 200

        if action == "remove":
            if len(trays) <= 1:
                return jsonify(ok=False, error="keep at least one tray"), 200
            filled = len(trays[tray].get("cells") or {})
            if filled and not data.get("confirm"):
                return jsonify(ok=False, needs_confirm=True, filled=filled,
                               error=f"tray {tray} has {filled} filled cells"), 200
            trays.pop(tray)
            save_config()
            return jsonify(ok=True, removed=tray)

        # resize / rename
        t = trays[tray]
        rows = t.get("rows", 4)
        cols = t.get("cols", 3)
        try:
            if "rows" in data:
                rows = max(1, min(MAX_DIM, int(data["rows"])))
            if "cols" in data:
                cols = max(1, min(MAX_DIM, int(data["cols"])))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="rows and cols must be numbers"), 200

        keep = _cell_ids(rows, cols)
        cells = t.get("cells") or {}
        dropped = sorted(c for c in cells if c not in keep)
        if dropped and not data.get("confirm"):
            return jsonify(ok=False, needs_confirm=True, dropped=dropped,
                           error=f"{len(dropped)} filled cells fall outside "
                                 f"a {cols}x{rows} grid"), 200
        t["rows"], t["cols"] = rows, cols
        t["cells"] = {k: v for k, v in cells.items() if k in keep}
        if "label" in data:
            t["label"] = str(data["label"]).strip()[:40] or f"Tray {tray}"
        save_config()
    return jsonify(ok=True, tray=tray, rows=rows, cols=cols, dropped=dropped)


@app.route("/api/render", methods=["POST"])
@require_auth
def api_render():
    count, _, _ = photo_inventory()
    if count < 2:
        return jsonify(error="Need at least 2 photos to render."), 400
    start_render()
    return jsonify(ok=True)


@app.route("/api/capture", methods=["POST"])
@require_auth
def api_capture():
    """Take a photo right now, using the same light-hold and exposure as the
    timelapse so it lines up with the grid and growth analysis."""
    with settings_lock:
        if not settings.get("camera_enabled"):
            return jsonify(ok=False, error="camera features are disabled in settings"), 200
    if not capture_lock.acquire(blocking=False):
        return jsonify(ok=False, error="A capture is already in progress."), 200
    try:
        with settings_lock:
            cfg = dict(settings)
        now = datetime.now(ZoneInfo(cfg["timezone"]))
        path = take_photo(cfg, now, manual=True)
    finally:
        capture_lock.release()
    if not path:
        return jsonify(ok=False, error="Capture failed; check the camera and the log."), 200
    # analyze growth in the background so the response returns as soon as the
    # photo is on disk -- but only inside the photoperiod: a night capture is
    # lit differently and would pollute the growth/dryness series
    with state_lock:
        on_t, off_t = state["on"], state["off"]
    if on_t is not None and off_t is not None and on_t <= now <= off_t:
        threading.Thread(target=record_growth, args=(path, cfg, now),
                         daemon=True).start()
    return jsonify(ok=True, photo=path.name)


@app.route("/api/preview", methods=["POST"])
@require_auth
def api_preview():
    """Grab a quick full-frame still for camera alignment. Not saved to the
    timelapse, not logged, and the light is left as-is, so the dashboard can
    poll it as a live-ish viewfinder while positioning the camera."""
    with settings_lock:
        if not settings.get("camera_enabled"):
            return jsonify(ok=False, error="camera features are disabled in settings"), 200
    if not capture_lock.acquire(blocking=False):
        return jsonify(ok=False, busy=True), 200
    try:
        with settings_lock:
            cw = int(settings.get("cam_width", 4608))
            ch = int(settings.get("cam_height", 2592))
        # half resolution keeps the full field of view but reads out faster
        pw = max(2, (cw // 2) // 2 * 2)
        ph = max(2, (ch // 2) // 2 * 2)
        tmp = PREVIEW_PATH.with_suffix(".tmp.jpg")
        with settings_lock:
            backend = settings.get("camera_backend", "rpicam")
            cfg_snapshot = dict(settings)
        if backend == "usb":
            # smaller and fewer warmup frames: alignment wants speed, not quality
            ok, err = _usb_capture(cfg_snapshot, tmp, 1280, 720, warmup=2)
            if not ok:
                _camera_fail(err)
                return jsonify(ok=False, error=err), 200
            # rotation only: the corners are dragged on the unflattened scene
            _rotate_file(tmp, int(cfg_snapshot.get("cam_rotate", 0)))
        else:
            cmd = ["rpicam-still", "-n", "-o", str(tmp), "-t", "500",
                   "--width", str(pw), "--height", str(ph)]
            r = subprocess.run(cmd, capture_output=True, timeout=20)
            if r.returncode != 0:
                _camera_fail(r.stderr.decode(errors="replace")[-150:] or "preview failed")
                return jsonify(ok=False,
                               error="camera error; check the log"), 200
        tmp.replace(PREVIEW_PATH)  # atomic, so a half-written frame is never served
        _camera_ok()
    except Exception as e:
        _camera_fail(str(e))
        return jsonify(ok=False, error=str(e)), 200
    finally:
        capture_lock.release()
    # sharpness rides along so the align view doubles as a focus aid; the
    # number is only comparable between frames of the same scene and light
    return jsonify(ok=True, ts=int(time.time()),
                   sharpness=sharpness_score(PREVIEW_PATH))


@app.route("/preview.jpg")
def preview_img():
    if not PREVIEW_PATH.exists():
        return "no preview yet", 404
    return send_file(PREVIEW_PATH, mimetype="image/jpeg")


@app.route("/video")
def video():
    if not VIDEO_PATH.exists():
        return "no video rendered yet", 404
    return send_file(VIDEO_PATH, mimetype="video/mp4", as_attachment=True,
                     download_name="grow_timelapse.mp4")


# never sent to the browser: /api/status is readable without login, and the
# frontend has no use for any of these
SECRET_SETTINGS = ("password_hash", "discord_webhook", "ntfy_topic")


def public_settings(cfg):
    return {k: v for k, v in cfg.items() if k not in SECRET_SETTINGS}


@app.route("/api/status")
def status():
    with state_lock:
        s = dict(state)
        cam = dict(camera)
    with settings_lock:
        cfg = dict(settings)
    if s["on"] is None:
        return jsonify(error="warming up"), 503
    tz = ZoneInfo(cfg["timezone"])
    count, _, latest_time = photo_inventory()
    snap = db.latest()                        # one query serves the whole response
    stf = latest_soil_temp_f(snap)
    pcal = cfg.get("probe_cal") or {}
    cam_on = bool(cfg.get("camera_enabled"))
    sensors_out = {k: {"ts": ts,
                       "value": (compensated_volts(v, pcal.get(k[6:]) or {}, stf)
                                 if k.startswith("probe:") else v)}
                   for k, (ts, v) in snap.items()
                   # legacy per-cell camera series (dry:/growth:/moisture:) are
                   # no longer written or shown; canopy hides with the camera
                   if not (k.startswith("dry:") or k.startswith("growth")
                           or k.startswith("moisture:"))
                   and (cam_on or not k.startswith("canopy:"))}
    day = day_light_summary()
    if day:
        # what the rest of today's schedule will deliver, from the measured curve
        day["forecast_remaining"] = dli_forecast(cfg, datetime.now(tz),
                                                 s["on"], s["off"])
    return jsonify(
        now=datetime.now(tz).isoformat(),
        brightness=s["brightness"],
        light_override=s.get("override", cfg.get("light_override", "auto")),
        manual_bright=cfg.get("manual_bright", 100),
        on=s["on"].isoformat(), off=s["off"].isoformat(),
        sunrise=s["sunrise"].isoformat(), sunset=s["sunset"].isoformat(),
        ramp=cfg["ramp_min"], max=cfg["max_bright"],
        schedule_mode=cfg.get("schedule_mode", "solar"),
        fan={"hw": FAN_HW, "on": fan_state["on"], "reason": fan_state["reason"],
             "mode": cfg.get("fan_mode", "auto"),
             "speed": fan_state.get("speed", 0),
             "manual_speed": cfg.get("fan_speed", 100),
             "auto_speed": cfg.get("fan_auto_speed", 70)},
        gpio=GPIO_PIN, freq=PWM_FREQ, loop=LOOP_SECONDS,
        photo_count=count,
        latest_photo_time=latest_time.isoformat() if latest_time else None,
        camera={"fails": cam["fails"], "last_err": cam["last_err"],
                "last_ok": (datetime.fromtimestamp(cam["last_ok"], tz).isoformat()
                            if cam["last_ok"] else None)},
        capturing=capturing,
        render=dict(render),
        video_time=(datetime.fromtimestamp(VIDEO_PATH.stat().st_mtime)
                    .isoformat() if VIDEO_PATH.exists() else None),
        light_backend=light_backend(cfg),
        kasa={"host": KASA_HOST,
              "on": kasa_state["on"], "ok": kasa_state["ok"],
              "error": kasa_state["error"], "fails": kasa_state["fails"]}
             if light_backend(cfg) == "kasa" else None,
        settings=public_settings(cfg),
        probe_default_cal=PROBE_DEFAULT_CAL,
        probe_cal_flags={t: f for t in
                         [k[6:] for k in snap if k.startswith("probe:")]
                         if (f := probe_cal_flag(
                             (probe_volts_filtered(t, snap)[0] or 0),
                             (pcal.get(t) or {})))},
        # filtered volts per tray, so the readout matches what decisions use
        probe_filtered={t: probe_volts_filtered(t, snap)[0]
                        for t in [k[6:] for k in snap if k.startswith("probe:")]},
        quality={
            "health": sensor_health(cfg, snap),
            # advisory only: cross-sensor checks can accuse the wrong sensor,
            # so they are shown but never wired to alerts or watering
            "contradictions": [
                {"subject": subj, "message": msg} for subj, msg in
                quality.contradictions(
                    {k: v for k, (ts, v) in snap.items()},
                    {**cfg, "_light_on": bool(s["brightness"] > 1)},
                    pumped_recently=any(
                        time.time() - (st.get("last_run") or 0) < 3600
                        for st in pump_state.values()))],
            "postfill": postfill_result,
            "rejects": {k: {"count": n, "last": _reject_last.get(k, "")}
                        for k, n in _reject_counts.items()},
            "probe_verdict": dict(_probe_verdict),
        },
        focus={"running": focus_state["running"], "step": focus_state["step"],
               "best": focus_state["best"], "error": focus_state["error"]},
        sensors=sensors_out,
        pressure_tendency=pressure_tendency(),
        sweep={"running": sweep_state["running"], "pct": sweep_state["pct"],
               "error": sweep_state["error"]},
        light_curve=cfg.get("light_curve"),
        day_light=day,
        light_plan=light_plan(cfg, s["on"], s["off"]),
        light_metrics=(lambda lx: {
            "k": lux_k(),
            "canopy": canopy_factor(),
            "ppfd": ppfd_from_lux(lx),
            "dli": dli_today(),
        } if lx is not None and lux_k() else None)(
            (snap.get("lux") or (None, None))[1]),
        authed=is_authed(),
        auth_enabled=auth_enabled(),
        water={
            "pump_hw": PUMP_HW,
            "auto_water": cfg.get("auto_water", False),
            "reservoir": {"state": reservoir_state(),
                          "wired": bool(sensors.RESERVOIR_PINS)},
            "auto_blockers": auto_water_blockers(cfg),
            "moisture_threshold_pct": cfg.get("moisture_threshold_pct", 30),
            "pump_cooldown_min": cfg.get("pump_cooldown_min", 30),
            "trays": {t: {
                "float": sensors.read_float(t),
                "pump_hw": t in _pumps,
                "running": pump_state[t]["running"],
                "last": pump_state[t]["last_detail"],
                "today_seconds": round(pump_state[t]["today_seconds"], 1),
            } for t in PUMP_PINS},
        },
    )


@app.route("/api/pump", methods=["POST"])
@require_auth
def pump_test():
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", "1"))
    if tray not in PUMP_PINS:
        return jsonify(ok=False,
                       error=f"no pump wired for tray {tray} "
                             f"(pumps: {', '.join(sorted(PUMP_PINS))})"), 200
    if tray not in _pumps:
        return jsonify(ok=False, error=f"pump {tray} hardware not available"), 200
    try:
        secs = float(data.get("seconds", 3))
    except (TypeError, ValueError):
        secs = 3.0
    force = bool(data.get("force", False))
    if data.get("until_full"):
        threading.Thread(target=lambda: run_pump_until_full(tray, "fill", force),
                         daemon=True).start()
        return jsonify(ok=True, started=True, mode="fill", tray=tray)
    threading.Thread(target=lambda: run_pump(tray, secs, "manual", force),
                     daemon=True).start()
    return jsonify(ok=True, started=True, mode="timed", tray=tray)


@app.route("/api/series")
def series():
    sensor = request.args.get("sensor", "")
    try:
        hours = max(1, min(24 * 90, int(request.args.get("hours", 168))))
    except ValueError:
        hours = 168
    if not sensor:
        return jsonify(error="sensor required"), 400
    return jsonify(sensor=sensor, hours=hours, points=db.series(sensor, hours))


def pressure_tendency():
    """Barometric trend, the way a barometer's 3-hour tendency works. Returns
    the change over 3h and 24h in hPa plus a plain-language reading. Falling
    pressure precedes unsettled weather; rising precedes clearing. Thresholds
    follow the conventional 3-hour bands used in surface observation."""
    pts = db.series("pressure", hours=26)
    if len(pts) < 4:
        return None
    now_ts, now_v = pts[-1]

    def value_at(hours_ago):
        target = now_ts - hours_ago * 3600
        best, bestd = None, 3600      # accept within an hour of the target
        for ts, v in pts:
            d = abs(ts - target)
            if d < bestd:
                best, bestd = v, d
        return best

    p3, p24 = value_at(3), value_at(24)
    if p3 is None:
        return None
    d3 = round(now_v - p3, 1)
    out = {"now": round(now_v, 1), "change_3h": d3,
           "change_24h": round(now_v - p24, 1) if p24 is not None else None}
    # conventional 3-hour tendency bands
    if d3 <= -6:
        words, arrow = "falling rapidly - expect a change", "down"
    elif d3 <= -2:
        words, arrow = "falling - unsettled ahead", "down"
    elif d3 < -0.5:
        words, arrow = "slowly falling", "down"
    elif d3 < 0.5:
        words, arrow = "steady", "flat"
    elif d3 < 2:
        words, arrow = "slowly rising", "up"
    elif d3 < 6:
        words, arrow = "rising - clearing", "up"
    else:
        words, arrow = "rising rapidly", "up"
    out["words"], out["arrow"] = words, arrow
    return out


def canopy_factor():
    """Multiplier from the sensor plane to canopy height. Light falls off with
    distance, so a sensor at soil level under-reads what the leaves receive."""
    with settings_lock:
        try:
            return max(0.1, min(10.0, float(settings.get("canopy_factor", 1.0))))
        except (TypeError, ValueError):
            return 1.0


def lux_k():
    """Lux -> PPFD divisor for this fixture's spectrum. Lux is weighted for
    human vision and undercounts the deep red and royal blue a grow panel
    emits, so the divisor is fixture-specific: ~54 sunlight, ~72 white LED,
    ~60 for a white-dominant mixed panel. 0 disables the derived metrics."""
    with settings_lock:
        try:
            return max(0.0, float(settings.get("lux_to_ppfd_k", 60)))
        except (TypeError, ValueError):
            return 60.0


def ppfd_from_lux(lux, at_canopy=True):
    """PPFD in umol/m2/s. By default reported at canopy height, since that is
    what the plants actually experience; pass at_canopy=False for the raw
    sensor plane."""
    k = lux_k()
    if not k or lux is None:
        return None
    return round(lux * (canopy_factor() if at_canopy else 1.0) / k, 1)


def dli_today():
    """Daily light integral so far today, in mol/m2/day: PPFD integrated over
    time since local midnight. This is the number that actually tracks growth,
    since it folds intensity and duration (ramps included) into one figure.
    Trapezoidal over logged lux; gaps longer than 30 min are skipped rather
    than interpolated, so downtime doesn't invent light that never fell."""
    k = lux_k()
    if not k:
        return None
    with settings_lock:
        tz = ZoneInfo(settings["timezone"])
    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = int(midnight.timestamp())
    pts = [(ts, v) for ts, v in db.series("lux", hours=25) if ts >= since]
    if len(pts) < 2:
        return None
    total = 0.0                       # micromol/m2 accumulated
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        dt = t1 - t0
        if dt <= 0 or dt > 1800:      # gap: don't fill it in
            continue
        total += ((v0 + v1) / 2 / k) * dt
    return round(total * canopy_factor() / 1_000_000, 2)   # micromol -> mol


def run_light_sweep(step=5, settle=2.0):
    """Step the light 0..100% and record lux at each stop, so we can chart the
    fixture's real response curve. Runs in a thread; the control loop leaves the
    light alone while sweep_state["running"] is set, and the previous brightness
    is restored at the end whatever happens."""
    points = []
    before = 0
    try:
        with state_lock:
            before = state.get("brightness") or 0
        levels = list(range(0, 101, step))
        if levels[-1] != 100:
            levels.append(100)
        for i, pct in enumerate(levels):
            if sweep_state["cancel"]:
                break
            set_brightness(pct)
            time.sleep(settle)                    # let the sensor integrate
            if sweep_state["cancel"]:             # cancelled while settling
                break
            lux = (sensors.read_all() or {}).get("lux")
            if lux is None:
                sweep_state["error"] = "no lux sensor reading; aborted"
                break
            points.append([pct, round(lux, 1)])
            sweep_state["pct"] = pct
    except Exception as e:
        sweep_state["error"] = str(e)
    finally:
        try:
            set_brightness(before)                # always hand the light back
        except Exception:
            pass
        complete = points and points[-1][0] == 100 and not sweep_state["error"]
        if complete:
            with settings_lock:
                settings["light_curve"] = {
                    "ts": int(time.time()), "step": step,
                    "settle": settle, "points": points,
                }
                save_config()
            print(f"light sweep: {len(points)} points, "
                  f"peak {max(p[1] for p in points):.0f} lx")
        elif points:
            print(f"light sweep stopped at {points[-1][0]}%; keeping previous curve")
        with sweep_lock:
            sweep_state["running"] = False
            sweep_state["cancel"] = False
        wake.set()                                # control loop resumes at once


def sharpness_score(path):
    """Variance of the Laplacian over the center half of the frame: higher is
    sharper. Center crop because the trays are centered and the frame edges
    are bench clutter that would reward focusing on the wrong thing.
    Returns None when the frame can't be read."""
    try:
        import cv2
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        h, w = img.shape[:2]
        img = img[h // 4: 3 * h // 4, w // 4: 3 * w // 4]
        return float(cv2.Laplacian(img, cv2.CV_64F).var())
    except Exception as e:
        print(f"sharpness score failed: {e}")
        return None


def run_focus_sweep():
    """Coarse pass across the full focus range, fine pass around the winner,
    then pin usb_focus_absolute to the sharpest value. Holds capture_lock for
    the duration (~60-90s) so the timelapse can't interleave, and holds the
    light at capture_brightness so every frame is scored under the same light."""
    with settings_lock:
        cfg = dict(settings)
    dev = cfg.get("usb_device", "/dev/video0")
    tmp = Path(__file__).with_name("_focus_probe.jpg")
    results = []          # (focus, score)
    global capturing
    try:
        with capture_lock:
            capturing = True                  # control loop leaves the light alone
            set_brightness(cfg.get("capture_brightness", 100))
            # manual focus must be active or focus_absolute writes are rejected
            subprocess.run(["v4l2-ctl", "-d", dev,
                            "-c", "focus_automatic_continuous=0"],
                           capture_output=True, timeout=10)

            def score_at(fv):
                subprocess.run(["v4l2-ctl", "-d", dev,
                                "-c", f"focus_absolute={int(fv)}"],
                               capture_output=True, timeout=10)
                time.sleep(0.6)               # lens travel time
                ok, err = _usb_capture(cfg, tmp, 1280, 720, warmup=3)
                if not ok:
                    raise RuntimeError(err or "capture failed")
                s = sharpness_score(tmp)
                if s is None:
                    raise RuntimeError("could not score the frame")
                return s

            coarse = list(range(0, 1024, 96)) + [1023]
            fine_span, fine_step = 96, 24
            with focus_lock:
                focus_state["total"] = len(coarse) + 2 * (fine_span // fine_step)
            done = 0
            for fv in coarse:
                if focus_state["cancel"]:
                    return
                results.append((fv, score_at(fv)))
                done += 1
                with focus_lock:
                    focus_state["step"] = done
            best = max(results, key=lambda r: r[1])[0]
            for fv in range(max(0, best - fine_span + fine_step),
                            min(1023, best + fine_span), fine_step):
                if focus_state["cancel"]:
                    return
                if any(r[0] == fv for r in results):
                    continue
                results.append((fv, score_at(fv)))
                done += 1
                with focus_lock:
                    focus_state["step"] = done
            best, best_score = max(results, key=lambda r: r[1])
            with settings_lock:
                settings["usb_focus_absolute"] = int(best)
                settings["usb_auto_focus"] = False
                save_config()
            with focus_lock:
                focus_state["best"] = {"focus": int(best),
                                       "score": round(best_score, 1),
                                       "tested": len(results)}
            print(f"focus sweep: best {best} "
                  f"(score {best_score:.0f}, {len(results)} points)")
            db.log_event("camera", f"focus sweep pinned focus_absolute={best}")
    except Exception as e:
        with focus_lock:
            focus_state["error"] = str(e)[:200]
        print(f"focus sweep failed: {e}")
    finally:
        tmp.unlink(missing_ok=True)
        capturing = False
        with focus_lock:
            focus_state["running"] = False
            focus_state["cancel"] = False
        wake.set()                            # control loop restores the light


@app.route("/api/focus_sweep", methods=["POST"])
@require_auth
def api_focus_sweep():
    """Start (or cancel) a focus sweep on the USB camera."""
    data = request.get_json(silent=True) or {}
    if data.get("cancel"):
        with focus_lock:
            focus_state["cancel"] = True
        return jsonify(ok=True, cancelled=True)
    with settings_lock:
        cfg = dict(settings)
    if not cfg.get("camera_enabled"):
        return jsonify(ok=False, error="camera features are disabled in settings"), 200
    if cfg.get("camera_backend") != "usb":
        return jsonify(ok=False, error="focus sweep needs the USB camera backend"), 200
    if not Path(cfg.get("usb_device", "/dev/video0")).exists():
        return jsonify(ok=False, error=f"{cfg.get('usb_device')} not present"), 200
    with focus_lock:
        if focus_state["running"]:
            return jsonify(ok=False, error="a focus sweep is already running"), 200
        focus_state.update(running=True, step=0, total=0, best=None,
                           error="", cancel=False)
    threading.Thread(target=run_focus_sweep, daemon=True).start()
    return jsonify(ok=True, started=True, estimate_seconds=90)


@app.route("/api/light_sweep", methods=["POST"])
@require_auth
def api_light_sweep():
    """Start (or cancel) a light response sweep."""
    data = request.get_json(silent=True) or {}
    if data.get("cancel"):
        with sweep_lock:
            sweep_state["cancel"] = True     # the worker restores the light
        return jsonify(ok=True, cancelled=True)
    if sensors.read_all().get("lux") is None:
        return jsonify(ok=False, error="no lux sensor detected"), 200
    try:
        step = max(1, min(25, int(data.get("step", 5))))
        settle = max(0.5, min(10.0, float(data.get("settle", 2.0))))
    except (TypeError, ValueError):
        step, settle = 5, 2.0
    with sweep_lock:
        if sweep_state["running"]:
            return jsonify(ok=False, error="a sweep is already running"), 200
        sweep_state.update(running=True, pct=0, error="", cancel=False,
                           started=time.time())
    threading.Thread(target=run_light_sweep, args=(step, settle),
                     daemon=True).start()
    est = int((101 / step + 1) * (settle + 0.3))
    return jsonify(ok=True, started=True, estimate_seconds=est)


def day_light_summary():
    """Today's light in one shot: DLI so far, the peak intensity reached, and
    how long the light has actually been delivering. Reads the same lux history
    the DLI integration uses, so the numbers always agree."""
    k = lux_k()
    with settings_lock:
        tz = ZoneInfo(settings["timezone"])
    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = int(midnight.timestamp())
    pts = [(ts, v) for ts, v in db.series("lux", hours=25) if ts >= since]
    if not pts:
        return None
    peak = max(v for _, v in pts)
    lit_s = 0
    for (t0, v0), (t1, _) in zip(pts, pts[1:]):
        dt = t1 - t0
        if 0 < dt <= 1800 and v0 >= 100:      # 100 lx: light is genuinely on
            lit_s += dt
    return {
        "dli": dli_today(),
        "peak_lux": round(peak, 1),                       # sensor plane
        "peak_ppfd": ppfd_from_lux(peak),                  # canopy
        "canopy_factor": canopy_factor(),
        "lit_minutes": round(lit_s / 60),
    }


def dli_forecast(cfg, now, on_time, off_time):
    """Project today's final DLI by integrating the *scheduled* brightness over
    the rest of the photoperiod and converting through the measured light
    response curve. Far better than extrapolating the average so far, which
    misreads the morning ramp as a dim day and midday as a bright one.

    Returns the forecast remaining mol/m2, or None when it can't be computed
    (no sweep on file, no lux factor, or the light is already done for today).
    """
    k = lux_k()
    curve = (cfg.get("light_curve") or {}).get("points")
    if not k or not curve or now >= off_time:
        return None

    pts = sorted(curve)
    cf = canopy_factor()

    def lux_at(pct):
        """Interpolate the measured curve at a brightness percentage."""
        if pct <= pts[0][0]:
            return pts[0][1]
        if pct >= pts[-1][0]:
            return pts[-1][1]
        for i in range(1, len(pts)):
            if pts[i][0] >= pct:
                x0, y0 = pts[i - 1]
                x1, y1 = pts[i]
                if x1 == x0:
                    return y1
                return y0 + (y1 - y0) * (pct - x0) / (x1 - x0)
        return pts[-1][1]

    # walk the remaining schedule in one-minute steps; the ramps are linear so
    # this is exact to well under the sensor's own noise
    start = max(now, on_time)
    total = 0.0
    step = timedelta(minutes=1)
    t = start
    while t < off_time:
        pct = brightness_for(cfg, t, on_time, off_time)
        total += (lux_at(pct) * cf / k) * 60.0   # umol/m2 for this minute
        t += step
    return round(total / 1_000_000, 2)


DLI_TARGET_LOW, DLI_TARGET_HIGH = 6.0, 12.0


def light_plan(cfg, on_time, off_time):
    """What the current schedule delivers on a full day, and what to change if
    that misses the seedling DLI window. Uses the measured light curve, so the
    advice is grounded in this fixture rather than a rule of thumb."""
    full = dli_forecast(cfg, on_time, on_time, off_time)
    if full is None:
        return None
    k = lux_k()
    pts = sorted((cfg.get("light_curve") or {}).get("points") or [])
    peak_lux = pts[-1][1] if pts else 0
    mx = float(cfg.get("max_bright", 100))
    # what one more hour at the current peak brightness is worth
    per_hour = (peak_lux * canopy_factor() / k) * 3600 / 1_000_000 if k else 0
    hours = (off_time - on_time).total_seconds() / 3600

    plan = {"full_day": full, "per_hour": round(per_hour, 2),
            "hours": round(hours, 1), "status": "ok", "advice": []}

    if full < DLI_TARGET_LOW:
        plan["status"] = "low"
        deficit = DLI_TARGET_LOW - full
        if per_hour > 0:
            add_h = deficit / per_hour
            if hours + add_h <= 18:
                plan["advice"].append(
                    f"Extend the photoperiod about {add_h:.1f}h "
                    f"(to ~{hours + add_h:.0f}h) to reach {DLI_TARGET_LOW:.0f} mol.")
            else:
                plan["advice"].append(
                    "Even an 18h day would not close the gap at this intensity.")
        if mx < 100:
            plan["advice"].append(
                f"Max brightness is {mx:.0f}%; raising it to 100% would add "
                f"roughly {(100 / mx - 1) * full:.1f} mol.")
        else:
            plan["advice"].append(
                "Brightness is already maxed, so the other lever is lowering the "
                "fixture: halving the distance roughly quadruples intensity.")
    elif full > DLI_TARGET_HIGH:
        plan["status"] = "high"
        excess = full - DLI_TARGET_HIGH
        if per_hour > 0:
            plan["advice"].append(
                f"About {excess / per_hour:.1f}h more light than seedlings need; "
                "shorten the photoperiod or dim slightly.")
    else:
        plan["advice"].append(
            f"This schedule delivers {full:.1f} mol/day, inside the "
            f"{DLI_TARGET_LOW:.0f}-{DLI_TARGET_HIGH:.0f} seedling window.")
    return plan


@app.route("/api/frame_context")
def frame_context():
    """Sensor readings nearest a timelapse frame's timestamp, so scrubbing the
    player shows what conditions were when the frame was taken. Storage units
    (Celsius); the frontend converts for display."""
    try:
        ts = int(request.args.get("ts", ""))
    except (TypeError, ValueError):
        return jsonify(error="ts required (unix seconds)"), 400
    out = {}
    snap_keys = db.latest()
    # first DS18B20 key, whatever its serial suffix is
    soil_key = next((k for k in snap_keys if k.startswith("temp:soil")), None)
    for key, label in ((soil_key, "soil_c"), ("temp:air", "air_c"),
                       ("humidity", "humidity"), ("lux", "lux")):
        if not key:
            continue
        v = db.reading_near(key, ts, window=1800)
        if v is not None:
            out[label] = round(v, 1)
    return jsonify(ts=ts, readings=out)


@app.route("/api/auto_water", methods=["POST"])
@require_auth
def api_auto_water():
    """Arm or disarm autonomous watering.

    Arming is refused while any tray has a blocker, because the trigger is a
    probe reading and an untrustworthy probe means a flood or a drought.
    Disarming is always allowed and never questioned.
    """
    data = request.get_json(silent=True) or {}
    want = bool(data.get("enabled"))
    if not want:
        with settings_lock:
            settings["auto_water"] = False
            save_config()
        db.log_event("auto_water", "disarmed")
        return jsonify(ok=True, enabled=False)
    blockers = auto_water_blockers()
    if blockers:
        return jsonify(ok=False, enabled=False, blockers=blockers,
                       error="Auto-watering needs a calibrated probe and a "
                             "float switch on every tray with a pump."), 200
    with settings_lock:
        settings["auto_water"] = True
        save_config()
    db.log_event("auto_water", "armed")
    return jsonify(ok=True, enabled=True)


@app.route("/api/fan", methods=["POST"])
@require_auth
def api_fan():
    """Set the fan mode: auto (schedule + humidity), on, or off."""
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).strip()
    if mode and mode not in ("auto", "on", "off"):
        return jsonify(ok=False, error="mode must be auto, on or off"), 200
    with settings_lock:
        if mode:
            settings["fan_mode"] = mode
        if "speed" in data:
            try:
                settings["fan_speed"] = max(0, min(100, int(float(data["speed"]))))
            except (TypeError, ValueError):
                pass
        if "auto_speed" in data:
            try:
                settings["fan_auto_speed"] = max(0, min(100, int(float(data["auto_speed"]))))
            except (TypeError, ValueError):
                pass
        mode = settings.get("fan_mode", "auto")
        save_config()
    wake.set()                      # apply on the next loop pass immediately
    return jsonify(ok=True, mode=mode)


_rect_cache = {"key": None, "bytes": None}
_rect_lock = threading.Lock()


@app.route("/rectified.jpg")
def rectified_image():
    """The latest photo flattened through the grid corners. This is what the
    per-cell analysis actually sees, so it is the honest way to check corner
    placement: if the tray edges are not straight here, the corners are wrong.

    Cached per (photo, geometry): the warp is recomputed only when a new photo
    lands or the corners move, not once per open browser tab."""
    _, latest, _ = photo_inventory()
    if not latest:
        return ("no photo yet", 404)
    with settings_lock:
        grid = dict(settings.get("grid") or {})
    corners = grid.get("corners")
    if not corners or len(corners) != 4:
        # nothing to rectify against; the page falls back to the raw frame
        return ("no grid corners set", 404)
    key = (str(latest), latest.stat().st_mtime,
           json.dumps([corners, grid.get("rows"), grid.get("cols")]))
    with _rect_lock:
        if _rect_cache["key"] == key:
            return Response(_rect_cache["bytes"], mimetype="image/jpeg",
                            headers={"Cache-Control": "no-store"})
    try:
        import cv2
        img = cv2.imread(str(latest))   # photos are stored already rotated
        if img is None:
            return ("could not read the photo", 500)
        out = growth_mod.rectify(img, corners,
                                 cols=int(grid.get("cols", 4)),
                                 rows=int(grid.get("rows", 4)))
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return ("encode failed", 500)
        data = buf.tobytes()
        with _rect_lock:
            _rect_cache.update(key=key, bytes=data)
        return Response(data, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        return (f"rectify failed: {e}", 500)


@app.route("/api/schedule", methods=["POST"])
@require_auth
def api_schedule():
    """Update just the schedule window. Separate from /api/settings so the
    chart's drag handles can commit a change without resubmitting every
    unrelated setting. Only meaningful in fixed and duration modes."""
    data = request.get_json(silent=True) or {}
    with settings_lock:
        mode = settings.get("schedule_mode", "solar")
        if mode == "solar":
            return jsonify(ok=False, error="switch to fixed or duration mode "
                                           "to drag the schedule"), 200
        changed = {}
        for k in ("fixed_on", "fixed_off", "duration_end"):
            if k in data:
                v = str(data[k] or "").strip()
                if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", v):
                    settings[k] = v
                    changed[k] = v
        if "duration_hours" in data:
            try:
                h = max(0.0, min(24.0, float(data["duration_hours"])))
                settings["duration_hours"] = round(h, 2)
                changed["duration_hours"] = settings["duration_hours"]
            except (TypeError, ValueError):
                pass
        if changed:
            save_config()
    if changed:
        wake.set()                      # recompute the window immediately
        print(f"schedule updated from chart: {changed}")
    return jsonify(ok=True, changed=changed, mode=mode)


@app.route("/api/host")
def api_host():
    """Pi health. Separate from /api/status so the 15s poll stays cheap:
    vcgencmd shells out, and none of this changes fast."""
    return jsonify(hoststats.all_stats())


@app.route("/api/series_all")
def series_all():
    """Every logged sensor's history in one request, so the chart grid doesn't
    fire a request per card. Probe voltages are temperature-compensated here,
    same as the live readings."""
    try:
        hours = max(1, min(24 * 90, int(request.args.get("hours", 168))))
    except ValueError:
        hours = 168
    with settings_lock:
        pcal = dict(settings.get("probe_cal") or {})
    snap = db.latest()
    stf = latest_soil_temp_f(snap)
    out = {}
    with settings_lock:
        camera_on = bool(settings.get("camera_enabled"))
    for k in snap.keys():
        if (k.startswith("float:") or k.startswith("reservoir:")):
            continue                      # binary states aren't charted
        if (k.startswith("dry:") or k.startswith("growth")
                or k.startswith("moisture:")):
            continue                      # retired per-cell camera series
        if not camera_on and k.startswith("canopy:"):
            continue                      # camera vision paused; hide its series
        pts = db.series(k, hours)
        if k.startswith("probe:"):
            cal = pcal.get(k[6:]) or {}
            pts = [[ts, compensated_volts(v, cal, stf)] for ts, v in pts]
        out[k] = pts
    k = lux_k()
    if k and "lux" in out:
        cf = canopy_factor()      # charted at canopy, matching the chips
        out["ppfd"] = [[ts, round(v * cf / k, 1)] for ts, v in out["lux"]]
    return jsonify(hours=hours, series=out)


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
        raise ValueError("unknown timezone; use an IANA name like "
                         "America/Los_Angeles")
    return v


def _v_roi(v):
    v = str(v or "").strip()
    try:
        parse_roi(v)
    except ValueError:
        raise ValueError("must be 'x,y,w,h' as fractions 0-1, or blank")
    return v


def _v_usb_device(v):
    v = str(v or "").strip()
    if not re.fullmatch(r"/dev/video\d+", v):
        raise ValueError("must look like /dev/video0")
    return v


SETTINGS_VALIDATORS = {
    "latitude": _v_float(-90, 90, clamp=False),
    "longitude": _v_float(-180, 180, clamp=False),
    "timezone": _v_timezone,
    "max_bright": _v_int(1, 100, clamp=False),
    "ramp_min": _v_int(0, 240, clamp=False),
    "sunrise_offset_min": _v_int(-720, 720),
    "sunset_offset_min": _v_int(-720, 720),
    "capture_enabled": _v_bool,
    "camera_enabled": _v_bool,
    "usb_auto_focus": _v_bool,
    "usb_auto_exposure_on": _v_bool,
    "usb_auto_white_balance": _v_bool,
    "timelapse_flatten": _v_bool,
    "cam_rectify": _v_bool,
    "alerts_enabled": _v_bool,
    "fan_with_light": _v_bool,
    "camera_backend": _v_choice("rpicam", "usb"),
    "usb_device": _v_usb_device,
    "usb_width": _v_int(160, 4096),
    "usb_height": _v_int(120, 4096),
    "usb_warmup_frames": _v_int(1, 20),
    "usb_exposure_time_absolute": _v_int(1, 100000),
    "usb_gain": _v_int(0, 255),
    "usb_white_balance_temperature": _v_int(1000, 10000),
    "usb_focus_absolute": _v_int(0, 1023),
    "cam_rotate": _v_choice(0, 90, 180, 270),
    "capture_interval_min": _v_int(5, 720, clamp=False),
    "capture_brightness": _v_int(1, 100, clamp=False),
    "roi": _v_roi,
    "alert_sustain_min": _v_int(1, 120),
    "alert_cooldown_hours": _v_int(1, 72),
    "soil_temp_low_f": _v_int(0, 150),
    "soil_temp_high_f": _v_int(0, 150),
    "alert_dry_pct": _v_int(0, 90),
    "alert_humidity_high": _v_int(0, 100),
    "alert_dli_low": _v_float(0, 30),
    "alert_dli_high": _v_float(0, 80),
    "fan_mode": _v_choice("auto", "on", "off"),
    "fan_speed": _v_int(0, 100),
    "fan_auto_speed": _v_int(0, 100),
    "fan_min_speed": _v_int(0, 100),
    "fan_humidity_on": _v_int(0, 100),
    "moisture_threshold_pct": _v_int(1, 90),
    "pump_max_seconds": _v_int(1, 120),
    "pump_cooldown_min": _v_int(1, 1440),
    "pump_daily_max_seconds": _v_int(1, 3600),
    "fill_max_seconds": _v_int(1, 600),
    "units": _v_choice("imperial", "metric"),
    "light_backend": _v_choice("pwm", "kasa"),
    "probe_median_depth": _v_int(1, 15),
    "schedule_mode": _v_choice("solar", "fixed", "duration"),
    "fixed_on": _v_hhmm,
    "fixed_off": _v_hhmm,
    "duration_end": _v_hhmm,
    "duration_hours": _v_float(0.0, 24.0),
    "humidity_low": _v_int(0, 100),
    "humidity_high": _v_int(0, 100),
    "canopy_factor": _v_float(0.1, 10.0),
    "lux_to_ppfd_k": _v_float(0.0, 200.0),
}


@app.route("/api/settings", methods=["POST"])
@require_auth
def update_settings():
    """Validate and apply every recognized field in the body.

    Partial bodies are fine, and one bad field no longer discards the rest:
    valid fields are saved, invalid ones come back in `errors` by name so the
    form can say exactly what was rejected. Unknown keys are ignored."""
    data = request.get_json(silent=True) or {}
    new, errors = {}, {}
    for k, v in data.items():
        fn = SETTINGS_VALIDATORS.get(k)
        if fn is None:
            continue
        try:
            new[k] = fn(v)
        except ValueError as e:
            errors[k] = str(e) or "invalid"
    if new:
        with settings_lock:
            was = light_backend(settings)
            settings.update(new)
            now_backend = light_backend(settings)
            save_config()
        if now_backend != was:
            # dark the abandoned output before the loop starts driving the new one
            release_backend(was)
            db.log_event("light", f"backend {was} -> {now_backend}")
        wake.set()
    return jsonify(ok=not errors, saved=sorted(new), errors=errors)


def cleanup(*_):
    # explicit off for every actuator: a SIGTERM mid-fill must not leave a
    # pump relying on gpiozero's atexit teardown and a gate pulldown
    for _p in _pumps.values():
        try:
            _p.off()
        except Exception:
            pass
    if _fan is not None:
        try:
            _fan.value = 0
        except Exception:
            pass
    set_brightness(0)
    pwm.stop()
    sys.exit(0)


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

if __name__ == "__main__":
    threading.Thread(target=control_loop, daemon=True).start()
    threading.Thread(target=capture_loop, daemon=True).start()
    threading.Thread(target=sample_loop, daemon=True).start()
    threading.Thread(target=watering_loop, daemon=True).start()
    threading.Thread(target=report_loop, daemon=True).start()
    print(f"Dashboard at http://0.0.0.0:{HTTP_PORT}")
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)
