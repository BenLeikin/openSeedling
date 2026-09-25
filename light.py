"""Both lights: backends (PWM, dim line, Kasa), schedules and ramps,
calibration tables and sweeps, below-minimum dithering, the storm, and
the control loop."""

import asyncio
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from astral import LocationInfo
from astral.sun import sun

from applog import log
import db
import sensors

import config
import hardware

LOOP_SECONDS  = 30

# light response sweep: brightness % -> measured lux, so the dashboard can show
# what the driver actually delivers (PWM dimming is rarely linear)
sweep_state = {"running": False, "pct": 0, "error": "", "started": 0.0,
               "cancel": False}   # cancel is a request; running means the
                                  # worker thread is still holding the light
sweep_lock = threading.Lock()


# ---- light output backends ----
# "pwm" is the original path: a dimmable 5V panel on hardware PWM.
# "kasa" drives a TP-Link smart plug for an AC fixture that has no controllable
# dimming, so output is on/off and the fixture's own knob sets intensity.
# Both are kept: switching backends is a settings change, not a redeploy.
# Plug connection details. The environment still wins, so an existing .env
# keeps working untouched; otherwise these come from settings, which is what
# lets a plug be set up from the dashboard instead of over SSH.
KASA_HOST_ENV = (os.environ.get("GROWLIGHT_KASA_HOST") or "").strip()
KASA_USER_ENV = (os.environ.get("GROWLIGHT_KASA_USER") or "").strip()
KASA_PASS_ENV = os.environ.get("GROWLIGHT_KASA_PASS") or ""


def kasa_conf(cfg=None):
    """(host, username, password) for the plug, environment taking precedence."""
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    return (KASA_HOST_ENV or str(cfg.get("kasa_host") or "").strip(),
            KASA_USER_ENV or str(cfg.get("kasa_user") or "").strip(),
            KASA_PASS_ENV or str(cfg.get("kasa_pass") or ""))
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


async def _kasa_connect(host, user, password):
    """Connect to one plug. Credentials only when the device needs them:
    legacy models (HS103 and friends) reject the authenticated path."""
    from kasa import Device, DeviceConfig, Credentials
    if user or password:
        cfg = DeviceConfig(host=host, credentials=Credentials(user, password))
        return await Device.connect(config=cfg)
    return await Device.connect(host=host)


async def _kasa_device():
    global _kasa_dev
    if _kasa_dev is not None:
        return _kasa_dev
    host, user, password = kasa_conf()
    _kasa_dev = await _kasa_connect(host, user, password)
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
    if not kasa_conf()[0]:
        with _kasa_lock:
            kasa_state.update(ok=False,
                              error="no smart plug configured (Settings > Smart plug)")
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
    log.warning(f"kasa plug command failed ({last})")
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
            # The duty that means dark depends on the backend being ABANDONED,
            # not the new one: settings have already been updated by the time
            # this runs, so asking pwm_duty_for would use the wrong wiring and
            # leave the old fixture at full brightness.
            hardware.pwm.change_duty_cycle(100.0 if old == "dim" else 0.0)
        log.info(f"light backend released: {old} set to off")
    except Exception as e:
        log.error(f"could not release the {old} light backend: {e}")


def light_backend(cfg=None):
    """Which wiring drives the light: "pwm", "dim" or "kasa".

    "pwm"  a MOSFET driving a 5V panel: duty goes straight through
    "dim"  an optocoupler sinking an AC driver's 0-10V dim line: inverted,
           and it dims all the way to dark, so no separate switch is needed
    "kasa" a smart plug: on/off only

    These were once "pwm" plus a light_invert checkbox, which allowed the
    nonsense combination of invert on a backend that cannot invert. A config
    still carrying light_invert is read as "dim" so nothing breaks on upgrade.
    """
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    val = cfg.get("light_backend")
    if val == "kasa":
        return "kasa"
    if val == "dim" or cfg.get("light_invert"):
        return "dim"
    return "pwm"


def set_brightness(percent):
    """Apply a dashboard brightness through the fixture's calibration.

    The dashboard speaks in intended light output. The hardware speaks in
    duty cycle, and the two are not the same shape: a fixture has a cutoff at
    the bottom, usually saturates well before the top, and bends in between.
    hw_percent() maps one onto the other; this just applies it.
    """
    percent = max(0.0, min(100.0, percent))
    frac = _below_minimum(percent)
    if frac is None:
        _dither_stop()
        set_brightness_raw(hw_percent(percent))
    else:
        _dither_set(frac, hw_percent(percent))


# ---- reaching below the driver's minimum ------------------------------------
# The dim-line driver holds about 20% of full and then cuts out, so nothing
# between off and that minimum exists as a steady level. To follow the straight
# response line down to zero anyway, the light cycles between off and the
# minimum with the on-time set so the AVERAGE lands on the line. Plants
# integrate light over far longer than this period, so for growth it is the
# same as a steady dim level; the daily light integral is identical.
DITHER_PERIOD_S = 20.0
_dither = {"frac": 0.0, "raw": 0.0, "on": False}
_dither_lock = threading.Lock()
_dither_wake = threading.Event()


def _below_minimum(percent, cfg=None):
    """On-fraction for a target below the fixture's minimum, else None.

    None also when cycling is switched off, which is the default: the light
    then simply holds the lowest level it can, and the bottom of a ramp is a
    short steady dim instead of a fixture flicking on and off.
    """
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    if percent <= 0 or not linear_table(cfg):
        return None
    if cfg.get("dim_below_min", "hold") != "cycle":
        return None
    lin = cfg.get("light_linear") or {}
    min_f = float(lin.get("min_output_pct") or 0) / 100.0
    if min_f <= 0 or percent / 100.0 >= min_f:
        return None
    return (percent / 100.0) / min_f


def _dither_set(frac, raw):
    with _dither_lock:
        _dither.update(frac=max(0.0, min(1.0, frac)), raw=raw, on=True)
    _dither_wake.set()


def _dither_stop():
    with _dither_lock:
        was = _dither["on"]
        _dither.update(on=False, frac=0.0)
    if was:
        _dither_wake.set()


def _dither_loop():
    """Cycle between off and the minimum while a sub-minimum target is set."""
    while True:
        with _dither_lock:
            on, frac, raw = _dither["on"], _dither["frac"], _dither["raw"]
        if not on:
            _dither_wake.wait()
            _dither_wake.clear()
            continue
        lit = DITHER_PERIOD_S * frac
        try:
            if lit > 0:
                set_brightness_raw(raw)
                if _dither_wake.wait(lit):
                    _dither_wake.clear(); continue   # target changed; restart
            set_brightness_raw(0)
            if _dither_wake.wait(DITHER_PERIOD_S - lit):
                _dither_wake.clear()
        except Exception as e:
            log.error(f"dimming below the minimum failed: {e}")
            time.sleep(1)


threading.Thread(target=_dither_loop, daemon=True, name="dither").start()


def set_brightness_raw(percent):
    """Drive the hardware directly, with no calibration applied.

    Used by the response sweep, which must measure the fixture as it really
    is. Building a calibration from a sweep that was itself calibrated would
    correct the correction.
    """
    percent = max(0.0, min(100.0, percent))
    if hardware.SHUTTING_DOWN.is_set() and percent > 0:
        return                        # shutting down: dark writes only
    with config.settings_lock:
        cfg = dict(config.settings)
    mode = light_backend(cfg)

    # The second light, if one is running, keeps its own level on its own
    # fixture. Every OTHER output is driven OFF, not merely skipped: an output
    # left alone holds whatever it had when the selection changed, which on a
    # grow light means a fixture quietly running with nothing pointing at it.
    # A sweep measures the main fixture alone, so the second one goes dark
    # while it runs rather than adding its light to the calibration.
    l2 = light2_fixture(cfg)
    if sweep_state["running"] and sweep_state.get("target") == "second":
        l2_level = float(sweep_state.get("l2_raw") or 0.0)   # raw: under test
    elif l2 is None or sweep_state["running"]:
        l2_level = 0.0
    else:
        # the second light's own calibration, when it has one
        l2_level = hw_percent(float(light2_state["level"]), light2_cfg(cfg))
    if hardware.SHUTTING_DOWN.is_set():
        l2_level = 0.0

    if mode == "dim" and hardware.pwm2 is None:
        # Only one channel configured: the dim fixture is on the main pin.
        hardware.pwm.change_duty_cycle(100.0 - percent)
    else:
        panel = percent if mode == "pwm" else (l2_level if l2 == "pwm" else 0.0)
        hardware.pwm.change_duty_cycle(panel)                              # MOSFET panel
        if hardware.pwm2 is not None:
            dim = percent if mode == "dim" else (l2_level if l2 == "dim" else 0.0)
            # the dim line: 0% duty is full brightness, so dark is 100
            hardware.pwm2.change_duty_cycle(100.0 - dim)
    if KASA_HOST_ENV or cfg.get("kasa_host"):
        set_plug(percent if mode == "kasa" else 0)


# ---- the second light -------------------------------------------------------
# Independently scheduled, on whichever PWM fixture the main light is not
# using. It deliberately has fewer features than the main light: no
# calibration, no DLI plan, no lightning. It runs a simple window with a ramp
# at each end, or a manual on/off.
light2_state = {"level": 0.0, "why": "off"}


def light2_fixture(cfg=None):
    """"pwm" or "dim" for the second light, or None when it cannot run.

    It takes the fixture the main light is NOT on, and needs both hardware
    PWM channels: with one channel the pin is already the main light's.
    """
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    if not cfg.get("light2_on") or hardware.pwm2 is None:
        return None
    main = light_backend(cfg)
    return "pwm" if main != "pwm" else "dim"


def light2_level(cfg, now):
    """Brightness for the second light at `now`, and a short reason."""
    ov = cfg.get("light2_override", "auto")
    top = max(0.0, min(100.0, float(cfg.get("light2_bright", 50))))
    if ov == "on":
        return top, "manual on"
    if ov == "off":
        return 0.0, "manual off"
    tz = now.tzinfo
    start = _clock(now.date(), tz, cfg.get("light2_start"), "08:00")
    end = _clock(now.date(), tz, cfg.get("light2_end"), "20:00")
    if end <= start:
        # a window across midnight: yesterday's start or today's end
        if now < end:
            start -= timedelta(days=1)
        else:
            end += timedelta(days=1)
    if not (start <= now < end):
        return 0.0, "outside its schedule"
    ramp = max(0.0, float(cfg.get("light2_ramp_min", 5))) * 60
    span = (end - start).total_seconds()
    ramp = min(ramp, span / 2)
    since = (now - start).total_seconds()
    until = (end - now).total_seconds()
    if ramp > 0 and since < ramp:
        return top * since / ramp, "ramping up"
    if ramp > 0 and until < ramp:
        return top * until / ramp, "ramping down"
    return top, "on schedule"


def hw_percent(percent, cfg=None):
    """Dashboard brightness -> the raw hardware percent that produces it.

    With a linear calibration on file, 50% means half of the fixture's real
    maximum output, the cutoff and the saturation both disappear from the
    scale, and 0% is still hard off. Without one, the simpler floor applies.
    """
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    percent = max(0.0, min(100.0, percent))
    if percent <= 0:
        return 0.0
    table = linear_table(cfg)
    if table:
        i = int(percent)
        if i >= 100:
            return float(table[100])
        frac = percent - i
        return float(table[i] + (table[i + 1] - table[i]) * frac)
    return apply_floor(percent, cfg)


def dashboard_lux(pct, cfg, pts):
    """Average lux a dashboard brightness delivers, through the calibration.

    Above the driver's minimum it is the raw curve at the calibrated duty.
    Below it the light is time-averaged, so the delivered light is the on
    fraction of the minimum level.
    """
    if pct <= 0:
        return _curve_lux(pts, 0)
    frac = _below_minimum(pct, cfg)
    raw = hw_percent(pct, cfg)
    if frac is None:
        # holding: below the minimum the fixture cannot go lower, so the
        # response flattens there rather than following the line down
        return _curve_lux(pts, raw)
    dark = _curve_lux(pts, 0)
    return dark + frac * (_curve_lux(pts, raw) - dark)


def effective_curve(cfg):
    """Dashboard brightness -> the lux it actually produces, 0..100.

    The stored sweep is the fixture's RAW response and will always be the
    shape the hardware makes. What the dashboard delivers is that response
    seen through the calibration, and with one in use it is a straight line:
    this is the curve that shows whether the calibration worked. Computed here
    so the chart uses exactly the mapping the light uses, rather than a copy
    of it in JavaScript that could drift.
    """
    pts = sorted((cfg.get("light_curve") or {}).get("points") or [])
    if not pts or not linear_table(cfg):
        return None
    return [[p, round(dashboard_lux(p, cfg, pts), 1)] for p in range(0, 101)]


# Which mapping a stored calibration table was built for. A table is just 101
# raw percentages; nothing in the numbers says what they mean. When the
# mapping changed from "spread over the usable range" to "fraction of full
# output", an old table read under the new meaning put 30% at 45% of full,
# silently. Every table is now stamped, and one built for a different
# mapping is refused rather than misread.
LINEAR_MAPPING = 2


def light2_cfg(cfg):
    """cfg seen as the second light: its own curve and calibration table in
    the places the main light's calibration code reads, so the same mapping
    code serves both fixtures."""
    return dict(cfg, light_curve=cfg.get("light2_curve"),
                light_linear=cfg.get("light2_linear"),
                light_linear_on=cfg.get("light2_linear_on", False),
                light_floor_pct=0, dim_below_min="hold")


def lux_key_for_light(light, cfg=None):
    """The light sensor of the setup on that light ("main" or "second")."""
    for st in setups_mod.setups(cfg):
        if st.get("light") == light and st.get("lux"):
            return st["lux"]
    return "lux" if light == "main" else None


def linear_table(cfg):
    """The calibration table if it is on, current and well formed, else None."""
    if not cfg.get("light_linear_on"):
        return None
    lin = cfg.get("light_linear") or {}
    table = lin.get("table")
    if not table or len(table) != 101:
        return None
    if lin.get("mapping") != LINEAR_MAPPING:
        return None
    return table


def build_linear_table(points):
    """From a raw sweep, the raw percent needed for each 0-100% of output.

    points: [[raw_pct, lux], ...]. Output is normalised to 0..1 between the
    darkest and brightest readings, forced monotonic (sensor noise can make a
    brighter step read a little lower, which would make the inverse jump
    backwards), then inverted: for each target fraction, the smallest raw
    level that reaches it, interpolated between measured steps.

    Returns (table, info) where table[i] is the raw percent for i% of full
    output, pinned to the lowest lit level below the driver's minimum.
    """
    pts = sorted((float(p), float(l)) for p, l in points)
    if len(pts) < 5:
        raise ValueError("need at least 5 sweep points")
    lo = pts[0][1]
    hi = max(l for _, l in pts)
    if hi - lo < 50:
        raise ValueError("the light barely changed across the sweep; is the "
                         "sensor under the fixture and the right backend set?")
    # normalise and force monotonic
    norm, run = [], 0.0
    for p, l in pts:
        f = max(0.0, (l - lo) / (hi - lo))
        run = max(run, f)
        norm.append((p, run))

    # Many LED drivers cannot dim to zero: they hold a minimum level and then
    # cut out entirely below it. Measured here the light sits near 20% of full
    # and drops straight to off. Interpolating across that cliff would place
    # small targets INSIDE the dark zone, so 1-2% would be off and the light
    # would then snap on. Anything below the lowest lit output is pinned to
    # the first lit sweep point instead: on always means on.
    first_lit = next((i for i, (_, f) in enumerate(norm) if f > 0.01), None)
    min_f = norm[first_lit][1] if first_lit is not None else 0.0
    min_raw = norm[first_lit][0] if first_lit is not None else 0.0

    def raw_for(target):
        if target <= 0:
            return 0.0
        if target <= min_f:
            return min_raw          # the lowest level that is actually lit
        for k in range(max(1, first_lit or 1), len(norm)):
            p1, f1 = norm[k]
            if f1 >= target:
                p0, f0 = norm[k - 1]
                if f1 == f0:
                    return p1
                return p0 + (p1 - p0) * (target - f0) / (f1 - f0)
        return norm[-1][0]

    # The dashboard spans the fixture's USABLE range: 1% is the dimmest level
    # it can hold and 100% is full. Mapping to a fraction of maximum instead
    # left 1-20% all producing the same minimum, because the driver cannot go
    # lower than that without cutting out. Every step now changes the light.
    # The DLI forecast converts through this same table, so it stays correct
    # even though 50% on the dashboard is no longer half the photons.
    # Dashboard percent is a fraction of full output, so the response follows
    # the straight line from zero to peak. Below the driver's minimum the table
    # holds the lowest lit level, and set_brightness reaches the line by
    # cycling between off and that level so the average lands on it.
    table = [0.0] + [round(raw_for(i / 100.0), 3) for i in range(1, 101)]
    # where light first appears, and where it stops increasing
    cutoff = next((p for p, f in norm if f > 0.01), pts[-1][0])
    sat = next((p for p, f in norm if f >= 0.99), pts[-1][0])
    info = {"mapping": LINEAR_MAPPING,
            "cutoff_raw": round(cutoff, 1), "saturation_raw": round(sat, 1),
            "peak_lux": round(hi, 0), "dark_lux": round(lo, 1),
            # the dimmest the fixture goes before cutting out, as a share of
            # full: below this the dashboard cannot ask for less light
            "min_output_pct": round(min_f * 100, 1),
            "points": len(pts)}
    return table, info


def set_plug(percent):
    """Switch the smart plug on or off to match a brightness."""
    want = percent > KASA_ON_AT
    with _kasa_lock:
        believed = kasa_state["on"]
    if believed is want and kasa_state["ok"]:
        return                          # already there; don't poll the plug
    kasa_apply(want)


def _stop_pwm(channel, mode):
    """Release a PWM channel, unless releasing it would turn the light ON.

    A MOSFET gate has a pulldown, so a released pin means off and stopping is
    right. An optocoupler sinking a dim line is the opposite: released means
    not conducting, which means full brightness.
    """
    try:
        if mode == "dim":
            channel.change_duty_cycle(100.0)    # keep it pulled down
        else:
            channel.stop()
    except Exception as e:
        log.error(f"could not release a PWM channel cleanly: {e}")


def apply_floor(percent, cfg=None):
    """Map the dashboard's 1-100% onto the range the fixture actually uses.

    A fixture has a cutoff: below some level the driver produces no light at
    all. Measured on the 0-10V fixture here, nothing happens until about 4%.
    Left alone, the bottom of every ramp is dead time and the DLI code plans
    light that never arrives.

    With a floor set, 0% is still hard off (below the cutoff on purpose) and
    1-100% is compressed into floor..100, so the first percent above zero is
    the first percent that lights.
    """
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    try:
        floor = float(cfg.get("light_floor_pct") or 0)
    except (TypeError, ValueError):
        floor = 0.0
    floor = max(0.0, min(50.0, floor))
    if floor <= 0 or percent <= 0:
        return percent
    return floor + percent * (100.0 - floor) / 100.0


def pwm_duty_for(percent, cfg=None):
    """Convert a brightness percent into the duty cycle the wiring needs.

    A MOSFET driving a 5V panel takes duty straight through: more duty, more
    light. An optocoupler on an AC driver's 0-10V dim input works the other
    way round. The driver supplies its own ~10.8V and the optocoupler SINKS
    it, so the light is at full when the optocoupler is off and dark when it
    conducts hardest: 0% duty is full brightness, 100% duty is dark.

    The "dim" backend means the dashboard's percentages match reality.
    Without the flip, every percentage in the app -- schedules, ramps, the
    response sweep, the DLI forecast -- would be backwards.
    """
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    if light_backend(cfg) != "dim":
        return percent
    return 100.0 - percent


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
    try:
        s = sun(loc.observer, date=day, tzinfo=tz)
    except ValueError:
        # Polar day or night: the sun never crosses the horizon, so there is
        # no sunrise to report. Fixed and duration schedules do not need one;
        # stand in noon +/- 6 h so they still run and the chart still draws.
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=tz)
        s = {"sunrise": noon - timedelta(hours=6),
             "sunset": noon + timedelta(hours=6)}
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


def ramp_floor(cfg=None):
    """The dimmest setting that actually changes the light.

    A driver that cannot hold less than a fifth of full output makes the
    bottom of a ramp pointless: the fixture sits at that minimum while the
    schedule counts down through settings it cannot produce, then cuts out.
    Ramping between this floor and max instead spends the whole ramp where
    the light really moves, and ends with one step to off.
    """
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    if linear_table(cfg) and cfg.get("dim_below_min", "hold") != "cycle":
        try:
            return max(0.0, float((cfg.get("light_linear") or {})
                                  .get("min_output_pct") or 0))
        except (TypeError, ValueError):
            return 0.0
    try:
        return max(0.0, float(cfg.get("light_floor_pct") or 0))
    except (TypeError, ValueError):
        return 0.0


def brightness_for(cfg, now, on_time, off_time):
    if now <= on_time or now >= off_time:
        return 0.0
    mx = float(cfg["max_bright"])
    lo = min(ramp_floor(cfg), mx)          # never above the ceiling itself
    ramp = timedelta(minutes=cfg["ramp_min"])
    full_start, full_end = on_time + ramp, off_time - ramp

    def at(frac):
        """frac 0..1 through a ramp, as brightness from the floor to max."""
        return lo + (mx - lo) * max(0.0, min(1.0, frac))

    if full_start >= full_end:  # very short window: triangular peak
        mid = on_time + (off_time - on_time) / 2
        if now <= mid:
            return at((now - on_time) / (mid - on_time))
        return at((off_time - now) / (off_time - mid))
    if now < full_start:
        return at((now - on_time) / ramp)
    if now > full_end:
        return at((off_time - now) / ramp)
    return mx


# the only settings that move the light window; anything else (a planting-map
# edit, a saved light curve) must not trigger a recompute or a log line
SCHED_KEYS = ("latitude", "longitude", "timezone", "schedule_mode",
              "fixed_on", "fixed_off", "duration_hours", "duration_end",
              "sunrise_offset_min", "sunset_offset_min")


# ---- lightning (an easter egg) -------------------------------------------
# Only offered on the optocoupler wiring, where the light is an AC fixture on
# a 0-10V dim line: that is the setup with enough range and speed to look like
# weather. The 5V panel cannot do it convincingly and the smart plug has no
# dimming at all.
LIGHTNING_MAX_S = 90
lightning_state = {"running": False, "until": 0.0, "style": ""}
_lightning_lock = threading.Lock()
# Set to ask the storm to end. An Event rather than a flag so the gap between
# strikes can be interrupted: those gaps run to several seconds, and a stop
# that waits them out does not feel like a stop.
_lightning_stop = threading.Event()


def lightning_available(cfg=None):
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    return light_backend(cfg) == "dim"


def _flash_prep():
    _dither_stop()


def _flash(level_pct):
    """Set a raw brightness without touching state: the storm is transient and
    must not be mistaken for a schedule change by anything watching.

    Goes to whichever channel carries the dim fixture, which is its own pin
    when one is configured and the main pin otherwise.
    """
    if hardware.SHUTTING_DOWN.is_set() and level_pct > 0:
        return                        # shutting down: dark writes only
    ch = hardware.pwm2 if hardware.pwm2 is not None else hardware.pwm
    ch.change_duty_cycle(100.0 - max(0.0, min(100.0, level_pct)))


def _storm(seconds, style):
    """Run the effect, then put the light back exactly where it was.

    The control loop is told to stand off while this runs; without that it
    would overwrite each flash on its next pass and the effect would stutter
    to a halt.
    """
    import random
    end = time.time() + seconds
    with config.state_lock:
        restore = float(config.state.get("brightness") or 0)
    _dither_stop()           # the storm owns the light for now
    try:
        while time.time() < end and not _lightning_stop.is_set():
            kind = style
            if style == "storm":
                r = random.random()
                kind = "sheet" if r < .5 else "strike" if r < .9 else "flicker"
            if kind == "sheet":              # cloud to cloud: no sharp edges
                top = random.uniform(45, 80)
                for i in range(14):
                    _flash(top * (i + 1) / 14); time.sleep(0.012)
                time.sleep(random.uniform(.05, .12))
                for i in range(28):
                    _flash(top * (1 - (i + 1) / 28)); time.sleep(0.018)
            elif kind == "flicker":          # storm overhead
                for _ in range(random.randint(6, 16)):
                    _flash(random.uniform(40, 100))
                    time.sleep(random.uniform(.05, .11))
                    _flash(0); time.sleep(random.uniform(.03, .12))
            else:                            # a stroke: leader, return, restrikes
                if random.random() < .6:
                    _flash(random.uniform(10, 25)); time.sleep(.06)
                    _flash(0); time.sleep(random.uniform(.02, .06))
                _flash(100); time.sleep(random.uniform(.06, .13))
                for _ in range(random.randint(1, 4)):
                    _flash(0); time.sleep(random.uniform(.03, .09))
                    _flash(random.uniform(55, 95))
                    time.sleep(random.uniform(.05, .10))
                _flash(15); time.sleep(random.uniform(.05, .15))
            _flash(0)
            # never sleep past the end: a long gap would otherwise hold the
            # light for seconds after the storm was meant to stop
            gap = random.expovariate(1 / 3.0)
            if _lightning_stop.wait(max(0.0, min(gap, end - time.time()))):
                break
    except Exception as e:
        log.warning(f"lightning stopped: {e}")
    finally:
        with _lightning_lock:
            lightning_state.update(running=False, until=0.0, style="")
        try:
            set_brightness(restore)  # back where the schedule had it, dithering
                                     # included if that is what it needs
        except Exception:
            pass
        config.wake.set()                  # and let the loop take over again
        db.log_event("light", "lightning finished")


def control_loop():
    seen = None
    last_err = None
    sunrise = sunset = on_time = off_time = None
    while True:
        try:
            with config.settings_lock:
                cfg = dict(config.settings)
            tz = ZoneInfo(cfg["timezone"])
            now = datetime.now(tz)
            key = (now.date(), tuple(cfg.get(k) for k in SCHED_KEYS))
            if key != seen:
                sunrise, sunset, on_time, off_time = sun_window(cfg, now.date(), tz)
                seen = key          # only once it worked, so a failure retries
                log.info(f"{now.date()}: on {on_time:%H:%M}, off {off_time:%H:%M} "
                      f"({cfg['latitude']}, {cfg['longitude']}, {cfg['timezone']})")
            # the second light first, so the write below carries its new level
            if light2_fixture(cfg):
                lv, why = light2_level(cfg, now)
                light2_state.update(level=round(lv, 2), why=why)
            else:
                light2_state.update(level=0.0,
                                    why="off" if not cfg.get("light2_on")
                                    else "needs both PWM channels")
            b = brightness_for(cfg, now, on_time, off_time)
            ov = cfg.get("light_override", "auto")
            if ov == "on":
                b = max(0, min(100, int(cfg.get("manual_bright", cfg["max_bright"]))))
            elif ov == "off":
                b = 0
            if (not camera_mod.capturing and not sweep_state["running"]
                    and not lightning_state["running"]):
                # a capture, a sweep or a storm each own the light while running;
                # writing the scheduled level here would fight them
                set_brightness(b)

            mode = cfg.get("fan_mode", "auto")
            if mode == "on":
                hardware.set_fan(cfg.get("fan_speed", 100), "manual")
            elif mode == "off":
                hardware.set_fan(0, "manual")
            else:
                fon, foff = setups_mod.setup_window(cfg, setups_mod.setup_with(cfg, "fan") or {"light": "main"},
                                         on_time, off_time)
                want, why = hardware.fan_should_run(cfg, now, fon, foff)
                hardware.set_fan(cfg.get("fan_auto_speed", 70) if want else 0, why)
            with config.state_lock:
                l2_now = light2_state["level"]
                changed = (round(config.state.get("light2_level", -1), 1) != round(l2_now, 1)
                           or round(config.state.get("brightness") or -1, 1) != round(b, 1)
                           or config.state.get("override") != ov
                           or config.state.get("on") != on_time)
                config.state.update(brightness=b, on=on_time, off=off_time,
                             sunrise=sunrise, sunset=sunset, override=ov,
                             light2_level=l2_now)
            # only when something moved: this loop runs every 30 seconds and during
            # a ramp every pass changes brightness, but a steady day should not
            # push an identical status to every open browser twice a minute
            if changed:
                status_mod.publish("light")
        except Exception as e:
            # One bad pass must not end light control for good: the thread
            # dying leaves the dashboard up while the fixture stays wherever
            # it was last set. Log it (once per distinct error, with the
            # traceback) and try again next pass.
            if str(e) != last_err:
                log.exception(f"control_loop error: {e}")
                last_err = str(e)
        else:
            last_err = None
        config.wake.wait(timeout=LOOP_SECONDS)
        config.wake.clear()


def _curve_lux(pts, raw_pct):
    """Interpolate a raw [[pct, lux]] curve at a raw percent."""
    if not pts:
        return 0.0
    if raw_pct <= pts[0][0]:
        return pts[0][1]
    if raw_pct >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        if pts[i][0] >= raw_pct:
            x0, y0 = pts[i - 1]; x1, y1 = pts[i]
            return y1 if x1 == x0 else y0 + (y1 - y0) * (raw_pct - x0) / (x1 - x0)
    return pts[-1][1]


def run_light_sweep(step=5, settle=2.0, linearize=False, target="main"):
    """Step the light 0..100% and record lux at each stop, so we can chart the
    fixture's real response curve. Runs in a thread; the control loop leaves the
    light alone while sweep_state["running"] is set, and the previous brightness
    is restored at the end whatever happens."""
    points = []
    before = 0
    _dither_stop()           # nothing else may move the light mid-measurement
    try:
        with config.state_lock:
            before = config.state.get("brightness") or 0
        levels = list(range(0, 101, step))
        if levels[-1] != 100:
            levels.append(100)
        key = lux_key_for_light(target) or "lux"
        for i, pct in enumerate(levels):
            if sweep_state["cancel"]:
                break
            if target == "second":
                # the second fixture alone: the main light is dark meanwhile
                sweep_state["l2_raw"] = pct
                set_brightness_raw(0)
            else:
                set_brightness_raw(pct)           # raw: measure the real fixture
            time.sleep(settle)                    # let the sensor integrate
            if sweep_state["cancel"]:             # cancelled while settling
                break
            lux = (sensors.read_all() or {}).get(key)
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
        except Exception as e:
            log.error(f"light not restored after the sweep ({e}); it is left "
                      f"at the sweep's last level")
        complete = points and points[-1][0] == 100 and not sweep_state["error"]
        if complete:
            lin = None
            if linearize:
                try:
                    table, info = build_linear_table(points)
                    lin = {"ts": int(time.time()), "table": table, **info}
                except ValueError as e:
                    sweep_state["error"] = f"calibration not built: {e}"
            pre = "light2" if target == "second" else "light"
            with config.settings_lock:
                config.settings[f"{pre}_curve"] = {
                    "ts": int(time.time()), "step": step,
                    "settle": settle, "points": points, "sensor": key,
                }
                if lin:
                    config.settings[f"{pre}_linear"] = lin
                    config.settings[f"{pre}_linear_on"] = True
                config.save_config()
            if lin:
                db.log_event("light", f"linear calibration built: light from "
                             f"{lin['cutoff_raw']}%, full by "
                             f"{lin['saturation_raw']}% raw")
            log.info(f"light sweep: {len(points)} points, "
                  f"peak {max(p[1] for p in points):.0f} lx")
        elif points:
            log.warning(f"light sweep stopped at {points[-1][0]}%; keeping previous curve")
        with sweep_lock:
            sweep_state["running"] = False
            sweep_state["cancel"] = False
            sweep_state["l2_raw"] = 0.0
        config.wake.set()                                # control loop resumes at once

# Imported last: these modules import this one, and their import-time
# code runs only after everything above is defined. Their names are
# used inside functions, at call time, always as module.name.
import setups as setups_mod
import camera as camera_mod
import status as status_mod
