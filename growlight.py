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

import json
import os
import re
import secrets
import subprocess
import sys
import signal
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from rpi_hardware_pwm import HardwarePWM
from astral import LocationInfo
from astral.sun import sun
from flask import (Flask, jsonify, render_template, request, session,
                   send_file, send_from_directory)
from werkzeug.security import check_password_hash

import db
import sensors
import notify
import ai_report
import discord_alert

# ------------------- defaults (overridden by config.json) -------------------
DEFAULTS = {
    "latitude": 34.17,
    "longitude": -118.84,
    "timezone": "America/Los_Angeles",
    "max_bright": 100,         # percent
    "ramp_min": 30,            # minutes
    "light_override": "auto",  # auto = follow the sun schedule; on|off = manual hold
    "manual_bright": 100,      # brightness held when light_override is "on"
    "units": "imperial",       # "imperial" (F, inHg) or "metric" (C, hPa);
                               #   storage stays Celsius/hPa either way
    "lux_to_ppfd_k": 60,       # lux -> PPFD divisor; set for your fixture's
                               #   spectrum (0 = hide PPFD/DLI). ~60 suits a
                               #   white-dominant mixed red/blue/white panel.
    "soil_temp_high_f": 90,    # warning line on the soil temp chart; 0 disables.
                               #   Chiles germinate best ~85F and drop off above
                               #   ~90-95F; seedlings prefer 70-80F once up.
    "sunrise_offset_min": 0,   # negative starts before sunrise
    "sunset_offset_min": 0,    # positive runs past sunset
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
    "auto_water": False,        # master switch; keep OFF until moisture calibrated
    "moisture_threshold_pct": 30,  # CALIBRATION TODO: "dry" trigger, per-probe
    "pump_max_seconds": 20,     # hard cap on a single dose (anti-flood/dry-run)
    "pump_cooldown_min": 30,    # min wait between auto doses (soil wicks slowly)
    "pump_daily_max_seconds": 180,  # runaway backstop
    "fill_max_seconds": 60,     # hard cap on a fill-to-float run (if float never trips)
    "dryness_cal": {},          # per-cell {wet,dry} brightness anchors -> camera moisture %
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

GPIO_PIN      = 18
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
PUMP_PINS = {"1": 24, "2": 26}   # tray -> BCM (physical 18, 37)
_pp = os.environ.get("GROWLIGHT_PUMP_PINS", "").strip()
if _pp:
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

settings = dict(DEFAULTS)
if CONFIG_PATH.exists():
    try:
        settings.update(json.loads(CONFIG_PATH.read_text()))
    except Exception as e:
        print(f"config.json unreadable ({e}), using defaults")

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
            CONFIG_PATH.write_text(json.dumps(settings, indent=2))
        except Exception as e:
            print(f"tray migration not persisted ({e})")
_migrate_trays()

settings_lock = threading.Lock()
wake = threading.Event()

state = {"brightness": 0.0, "on": None, "off": None,
         "sunrise": None, "sunset": None}
state_lock = threading.Lock()
# camera health: every capture/preview outcome lands here so the dashboard
# can tell "old photo because night" from "old photo because the camera died"
camera = {"last_ok": None, "fails": 0, "last_err": "", "last_err_ts": None}


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
GROWTH_SCRIPT = Path(__file__).with_name("growth.py")

try:
    pwm = HardwarePWM(pwm_channel=0, hz=PWM_FREQ, chip=0)
    pwm.start(0)
except Exception as e:
    sys.exit(f"Hardware PWM unavailable ({e}). Check that "
             f"'dtoverlay=pwm,pin=18,func=2' is in /boot/firmware/config.txt "
             f"and reboot after adding it.")


def set_brightness(percent):
    percent = max(0.0, min(100.0, percent))
    pwm.change_duty_cycle(percent)


def sun_window(cfg, day, tz):
    loc = LocationInfo(latitude=cfg["latitude"], longitude=cfg["longitude"])
    s = sun(loc.observer, date=day, tzinfo=tz)
    on_time  = s["sunrise"] + timedelta(minutes=cfg["sunrise_offset_min"])
    off_time = s["sunset"]  + timedelta(minutes=cfg["sunset_offset_min"])
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


def make_thumb(photo_path):
    """640px thumbnail for the browser player. Cheap, one-time per photo."""
    dst = THUMB_DIR / photo_path.name
    if dst.exists():
        return
    try:
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(photo_path),
             "-vf", "scale=640:-2", "-q:v", "7", str(dst)],
            capture_output=True, timeout=120)
    except Exception as e:
        print(f"thumbnail error for {photo_path.name}: {e}")


def photo_inventory():
    photos = sorted(TIMELAPSE_DIR.glob("*.jpg"))
    if not photos:
        return 0, None, None
    latest = photos[-1]
    return len(photos), latest, datetime.fromtimestamp(latest.stat().st_mtime)


# --------------------------- control loop ---------------------------

def _today_str():
    return datetime.now(ZoneInfo(settings["timezone"])).date().isoformat()


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
        return True, f"filled in {elapsed:.1f}s"
    return False, f"ran to {elapsed:.1f}s cap without float trip (source empty?)"


def _cam_moisture(cell, b, cal):
    c = cal.get(cell) or {}
    wet = c.get("wet")
    if wet is None:
        return None
    dry = c.get("dry", wet + 15)
    if dry <= wet:
        return None
    return round(max(0.0, min(100.0, 100.0 * (dry - b) / (dry - wet))))


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
    cal = cfg.get("dryness_cal") or {}
    pcal = cfg.get("probe_cal") or {}
    pnames = cfg.get("probe_names") or {}
    cam, raw, growth, probes, soiltemp, env = {}, {}, {}, {}, {}, {}
    snap = db.latest()
    stf = latest_soil_temp_f(snap)
    for k, (ts, v) in snap.items():
        if k.startswith("dry:"):
            cell = k[4:]
            m = _cam_moisture(cell, v, cal)
            (cam if m is not None else raw)[cell] = m if m is not None else round(v, 1)
        elif k.startswith("growth:"):
            growth[k[7:]] = round(v, 1)
        elif k.startswith("probe:"):
            t = k[6:]
            pm, approx = probe_moisture_any(v, pcal.get(t) or {}, stf)
            nm = pnames.get(t, f"Tray {t}")
            if pm is not None:
                probes[nm + (" (approx)" if approx else "")] = pm
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
            if v.get("planted"):
                try:
                    d = datetime.strptime(v["planted"], "%Y-%m-%d").date()
                    bits.append(f"sown {v['planted']} ({(now.date() - d).days}d ago)")
                except ValueError:
                    pass
            if bits:
                rows.append(f"{cid}: {' '.join(bits)}")
        if rows:
            planting[t.get("label", f"Tray {tid}")] = rows
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
        "camera_moisture": cam, "dryness_raw": raw, "growth": growth,
        "probe_moisture": probes,
        "soil_temp_f": soiltemp,
        "environment": env,
        "pressure_trend": pressure_tendency(),
        "light_metrics": {"ppfd": ppfd_from_lux((snap.get("lux") or (None, None))[1]),
                          "dli": dli_today()},
        "planting": planting,
        "units": {"temp": temp_unit(), "press": press_unit()},
        "float": flabel,
        "pump_today_s": round(pump_state.get("today_seconds", 0.0), 1),
        "pump_last": pump_state.get("last_detail") or "none",
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
        photos = sorted(TIMELAPSE_DIR.glob("*.jpg"))
        photo = photos[-1] if photos else None
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


def watering_loop():
    """Autonomous watering. DISABLED until auto_water is on AND moisture is
    calibrated. Scaffold only -- the dry-trigger and fill logic go here once
    real moisture data tells us what 'dry' means.
    Planned: if a cell reads below moisture_threshold_pct and cooldown has
    elapsed and the tray float says 'not full', dose via run_pump(reason="auto"),
    stopping early when the float trips. If a dose runs to the cap without the
    float tripping, treat it as source-empty/leak/stuck-float: log it, fire a
    notify alert, and flip auto_water off."""
    while True:
        time.sleep(60)
        with settings_lock:
            on = bool(settings.get("auto_water", False))
        if not on:
            continue
        # TODO (post-calibration): real dry detection + float-gated dosing.


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
            readings = sensors.read_all()
            if readings:
                db.log_many(list(readings.items()))
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


def control_loop():
    seen = None
    sunrise = sunset = on_time = off_time = None
    while True:
        with settings_lock:
            cfg = dict(settings)
        tz = ZoneInfo(cfg["timezone"])
        now = datetime.now(tz)
        key = (now.date(), json.dumps(cfg, sort_keys=True))
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
        if not capturing:
            set_brightness(b)
        with state_lock:
            state.update(brightness=b, on=on_time, off=off_time,
                         sunrise=sunrise, sunset=sunset, override=ov)
        wake.wait(timeout=LOOP_SECONDS)
        wake.clear()


# --------------------------- capture loop ---------------------------

def take_photo(cfg, now):
    global capturing
    capturing = True
    saved = None
    try:
        set_brightness(cfg["capture_brightness"])
        time.sleep(2)  # let light and auto-exposure settle
        fname = TIMELAPSE_DIR / f"{now:%Y%m%d_%H%M%S}.jpg"
        cw = int(cfg.get("cam_width", 4608))
        ch = int(cfg.get("cam_height", 2592))
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
            make_thumb(fname)
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
    """Measure per-cell canopy coverage from a just-captured photo and log it.
    Runs growth.py as a subprocess so OpenCV memory is freed afterwards."""
    grid = cfg.get("grid") or {}
    if not grid.get("corners"):
        return
    payload = {"corners": grid["corners"],
               "rows": grid.get("rows", 4), "cols": grid.get("cols", 4)}
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
    readings = out.get("readings") or {}
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

def render_worker():
    import time as _t
    t0 = _t.monotonic()
    frames = sorted(TIMELAPSE_DIR.glob("*.jpg"))
    with render_lock:
        render.update(state="running", frames=len(frames),
                      started=datetime.now(ZoneInfo(settings["timezone"])).isoformat(),
                      elapsed=None, msg=f"Rendering {len(frames)} frames...")
    try:
        tmp = TIMELAPSE_DIR / "_render_tmp.mp4"
        # Encode pass: small footprint so the 512MB Zero never OOMs.
        # 1280-wide, ultrafast, single thread, no faststart here (the
        # +faststart second pass rewrites the whole file in memory and is
        # what tips the box over). We add faststart as a cheap remux after.
        r = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y",
             "-framerate", "24", "-pattern_type", "glob",
             "-i", str(TIMELAPSE_DIR / "*.jpg"),
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


@app.route("/api/dryness_cal", methods=["POST"])
@require_auth
def dryness_cal_set():
    """Capture the current per-cell camera brightness as the 'wet' (100%) or
    'dry' (0%) anchor, so the dashboard can show a camera-moisture percentage."""
    data = request.get_json(silent=True) or {}
    point = data.get("point")
    if point not in ("wet", "dry"):
        return jsonify(ok=False, error="point must be 'wet' or 'dry'"), 200
    cells = {k[4:]: v for k, (ts, v) in db.latest().items() if k.startswith("dry:")}
    if not cells:
        return jsonify(ok=False, error="no camera readings yet; wait for a capture"), 200
    with settings_lock:
        cal = settings.setdefault("dryness_cal", {})
        for cell, v in cells.items():
            cal.setdefault(cell, {})[point] = v
        CONFIG_PATH.write_text(json.dumps(settings, indent=2))
    return jsonify(ok=True, point=point, cells=len(cells))


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
        CONFIG_PATH.write_text(json.dumps(settings, indent=2))
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
        CONFIG_PATH.write_text(json.dumps(settings, indent=2))
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
        CONFIG_PATH.write_text(json.dumps(settings, indent=2))
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
        CONFIG_PATH.write_text(json.dumps(settings, indent=2))
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
    if tray not in ("1", "2"):
        return jsonify(ok=False, error="tray must be 1 or 2"), 200
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
            CONFIG_PATH.write_text(json.dumps(settings, indent=2))
        res["applied"] = True
    return jsonify(res)


@app.route("/api/trays", methods=["POST"])
@require_auth
def api_trays():
    """Save what's planted in each cell. Body: {"tray": "1"|"2", "cells":
    {"A1": {"seed": str, "equipment": str, "planted": "YYYY-MM-DD"}, ...}}.
    Empty cells are dropped so the map only holds what's actually there."""
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", ""))
    cells = data.get("cells")
    label = data.get("label")
    if tray not in ("1", "2"):
        return jsonify(ok=False, error="tray must be 1 or 2"), 200
    if not isinstance(cells, dict):
        return jsonify(ok=False, error="cells must be an object"), 200
    clean = {}
    for cid, v in cells.items():
        if not isinstance(v, dict) or not re.fullmatch(r"[A-Z]\d{1,2}", str(cid)):
            continue
        seed = str(v.get("seed", "")).strip()[:60]
        equip = str(v.get("equipment", "")).strip()[:60]
        planted = str(v.get("planted", "")).strip()[:10]
        if planted and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", planted):
            planted = ""
        if seed or equip or planted:
            clean[cid] = {"seed": seed, "equipment": equip, "planted": planted}
    with settings_lock:
        trays = settings.setdefault("trays", {})
        t = trays.setdefault(tray, {"label": f"Tray {tray}", "rows": 3,
                                    "cols": 4, "cells": {}})
        t["cells"] = clean
        if label is not None:
            t["label"] = str(label).strip()[:40] or f"Tray {tray}"
        CONFIG_PATH.write_text(json.dumps(settings, indent=2))
    return jsonify(ok=True, tray=tray, count=len(clean))


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
        path = take_photo(cfg, now)
    finally:
        capture_lock.release()
    if not path:
        return jsonify(ok=False, error="Capture failed; check the camera and the log."), 200
    # analyze growth in the background so the response returns as soon as the
    # photo is on disk; the dashboard can refresh the snapshot immediately
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
    return jsonify(ok=True, ts=int(time.time()))


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
                   if cam_on or not (k.startswith("dry:") or k.startswith("growth:"))}
    return jsonify(
        now=datetime.now(tz).isoformat(),
        brightness=s["brightness"],
        light_override=s.get("override", cfg.get("light_override", "auto")),
        manual_bright=cfg.get("manual_bright", 100),
        on=s["on"].isoformat(), off=s["off"].isoformat(),
        sunrise=s["sunrise"].isoformat(), sunset=s["sunset"].isoformat(),
        ramp=cfg["ramp_min"], max=cfg["max_bright"],
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
        settings=cfg,
        probe_default_cal=PROBE_DEFAULT_CAL,
        sensors=sensors_out,
        pressure_tendency=pressure_tendency(),
        light_metrics=(lambda lx: {
            "k": lux_k(),
            "ppfd": ppfd_from_lux(lx),
            "dli": dli_today(),
        } if lx is not None and lux_k() else None)(
            (snap.get("lux") or (None, None))[1]),
        authed=is_authed(),
        auth_enabled=auth_enabled(),
        water={
            "pump_hw": PUMP_HW,
            "auto_water": cfg.get("auto_water", False),
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
        return jsonify(ok=False, error="tray must be 1 or 2"), 200
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


def ppfd_from_lux(lux):
    k = lux_k()
    if not k or lux is None:
        return None
    return round(lux / k, 1)


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
    return round(total / 1_000_000, 2)      # micromol -> mol


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
        if k.startswith("growth_px:") or k.startswith("float:"):
            continue                      # pixel counts and the float aren't charted
        if not camera_on and (k.startswith("dry:") or k.startswith("growth:")):
            continue                      # camera vision paused; hide its series
        pts = db.series(k, hours)
        if k.startswith("probe:"):
            cal = pcal.get(k[6:]) or {}
            pts = [[ts, compensated_volts(v, cal, stf)] for ts, v in pts]
        out[k] = pts
    k = lux_k()
    if k and "lux" in out:
        out["ppfd"] = [[ts, round(v / k, 1)] for ts, v in out["lux"]]
    return jsonify(hours=hours, series=out)


@app.route("/api/settings", methods=["POST"])
@require_auth
def update_settings():
    data = request.get_json(silent=True) or {}
    new = {}
    try:
        new["latitude"]  = float(data["latitude"])
        new["longitude"] = float(data["longitude"])
        new["timezone"]  = str(data["timezone"]).strip()
        new["max_bright"] = int(data["max_bright"])
        new["ramp_min"]   = int(data["ramp_min"])
        new["sunrise_offset_min"] = int(data["sunrise_offset_min"])
        new["sunset_offset_min"]  = int(data["sunset_offset_min"])
        new["capture_enabled"]      = bool(data["capture_enabled"])
        if "camera_enabled" in data:
            new["camera_enabled"] = bool(data["camera_enabled"])
        new["capture_interval_min"] = int(data["capture_interval_min"])
        new["capture_brightness"]   = int(data["capture_brightness"])
        new["roi"] = str(data.get("roi", "")).strip()
        if data.get("units") in ("imperial", "metric"):
            new["units"] = data["units"]
        if "lux_to_ppfd_k" in data:
            try:
                new["lux_to_ppfd_k"] = max(0.0, min(200.0, float(data["lux_to_ppfd_k"] or 0)))
            except (TypeError, ValueError):
                pass
        if "soil_temp_high_f" in data:
            v = data.get("soil_temp_high_f")
            new["soil_temp_high_f"] = max(0, min(150, int(v or 0)))
    except (KeyError, TypeError, ValueError):
        return jsonify(error="All fields are required and must be numbers "
                             "(timezone is text)."), 400
    if not -90 <= new["latitude"] <= 90:
        return jsonify(error="Latitude must be between -90 and 90."), 400
    if not -180 <= new["longitude"] <= 180:
        return jsonify(error="Longitude must be between -180 and 180."), 400
    if not 1 <= new["max_bright"] <= 100:
        return jsonify(error="Max brightness must be 1 to 100."), 400
    if not 0 <= new["ramp_min"] <= 240:
        return jsonify(error="Ramp must be 0 to 240 minutes."), 400
    if not 5 <= new["capture_interval_min"] <= 720:
        return jsonify(error="Photo interval must be 5 to 720 minutes."), 400
    if not 1 <= new["capture_brightness"] <= 100:
        return jsonify(error="Photo brightness must be 1 to 100."), 400
    try:
        parse_roi(new["roi"])
    except ValueError:
        return jsonify(error="Crop must be 'x,y,w,h' as fractions 0-1 "
                             "(e.g. 0.22,0.08,0.57,0.82), or blank for "
                             "full frame."), 400
    try:
        ZoneInfo(new["timezone"])
    except Exception:
        return jsonify(error=f"Unknown timezone '{new['timezone']}'. "
                             "Use an IANA name like America/Los_Angeles."), 400
    with settings_lock:
        settings.update(new)
        CONFIG_PATH.write_text(json.dumps(settings, indent=2))
    wake.set()
    return jsonify(ok=True)


def cleanup(*_):
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
