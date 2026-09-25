"""Grow setups, their light windows, DLI integration and curves, and the
Plan card's verdicts."""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import db

import config
import hardware
import light as light_mod

def canopy_factor():
    """Multiplier from the sensor plane to canopy height. Light falls off with
    distance, so a sensor at soil level under-reads what the leaves receive."""
    with config.settings_lock:
        try:
            return max(0.1, min(10.0, float(config.settings.get("canopy_factor", 1.0))))
        except (TypeError, ValueError):
            return 1.0


def lux_k():
    """Lux -> PPFD divisor for this fixture's spectrum. Lux is weighted for
    human vision and undercounts the deep red and royal blue a grow panel
    emits, so the divisor is fixture-specific: ~54 sunlight, ~72 white LED,
    ~60 for a white-dominant mixed panel. 0 disables the derived metrics."""
    with config.settings_lock:
        try:
            return max(0.0, float(config.settings.get("lux_to_ppfd_k", 60)))
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


def dli_between(start, end, key="lux", k=None):
    """Measured light between two unix times: (mol/m2, covered, lit_seconds).

    The same integration as dli_today, over any window. `covered` is the
    fraction of the window the sensor record actually spans; gaps over 30
    minutes are left out rather than invented, so a restart or an outage
    shows up as low coverage instead of as a dim day. lit_seconds is time the
    sensor saw real light, whatever produced it.
    """
    k = k or lux_k()
    if not k or end <= start:
        return None
    hours = (time.time() - start) / 3600 + 1
    pts = [(ts, v) for ts, v in db.series(key, hours=max(2, hours))
           if start <= ts <= end]
    if len(pts) < 2:
        return None
    peak = max(v for _, v in pts)
    dark = max(50.0, peak * 0.02)
    total = covered = lit = 0.0
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        dt = t1 - t0
        if dt <= 0 or dt > 1800:
            continue
        total += ((v0 + v1) / 2 / k) * dt
        covered += dt
        if (v0 + v1) / 2 > dark:
            lit += dt
    return (round(total * canopy_factor() / 1_000_000, 2),
            covered / (end - start), lit)


def measured_day(cfg, now, off_time, key="lux", k=None):
    """The most recent complete day as the light sensor recorded it.

    Today once every light is out (the photoperiod is over and the sensor has
    read dark for a quarter hour), otherwise yesterday. The source is named so
    the card can say which day it is judging.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    t_mid, t_now = midnight.timestamp(), now.timestamp()
    if now >= off_time:
        day_pts = [v for ts, v in db.series(key, hours=25) if ts >= t_mid]
        recent = [v for ts, v in db.series(key, hours=1) if ts >= t_now - 900]
        today = dli_between(t_mid, t_now, key, k)
        # "every light is out" is judged by the sensor too: the last quarter
        # hour reads under 2% of today's peak (room light is well below that)
        dark = max(50.0, 0.02 * max(day_pts)) if day_pts else 50.0
        if recent and today and max(recent) < dark:
            mol, cov, lit = today
            if cov >= 0.9:
                return {"mol": mol, "lit_hours": lit / 3600, "day": "today"}
    y = dli_between(t_mid - 86400, t_mid, key, k)
    if y and y[1] >= 0.9:
        return {"mol": y[0], "lit_hours": y[2] / 3600, "day": "yesterday"}
    return None


def dli_today(key="lux", k=None):
    """Daily light integral so far today, in mol/m2/day: PPFD integrated over
    time since local midnight. This is the number that actually tracks growth,
    since it folds intensity and duration (ramps included) into one figure.
    Trapezoidal over logged lux; gaps longer than 30 min are skipped rather
    than interpolated, so downtime doesn't invent light that never fell."""
    k = k or lux_k()
    if not k:
        return None
    with config.settings_lock:
        tz = ZoneInfo(config.settings["timezone"])
    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = int(midnight.timestamp())
    pts = [(ts, v) for ts, v in db.series(key, hours=25) if ts >= since]
    if len(pts) < 2:
        return None
    total = 0.0                       # micromol/m2 accumulated
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        dt = t1 - t0
        if dt <= 0 or dt > 1800:      # gap: don't fill it in
            continue
        total += ((v0 + v1) / 2 / k) * dt
    return round(total * canopy_factor() / 1_000_000, 2)   # micromol -> mol


def day_light_summary(key="lux", k=None):
    """Today's light in one shot: DLI so far, the peak intensity reached, and
    how long the light has actually been delivering. Reads the same lux history
    the DLI integration uses, so the numbers always agree."""
    with config.settings_lock:
        tz = ZoneInfo(config.settings["timezone"])
    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = int(midnight.timestamp())
    pts = [(ts, v) for ts, v in db.series(key, hours=25) if ts >= since]
    if not pts:
        return None
    peak = max(v for _, v in pts)
    kk = k or lux_k()
    lit_s = 0
    for (t0, v0), (t1, _) in zip(pts, pts[1:]):
        dt = t1 - t0
        if 0 < dt <= 1800 and v0 >= 100:      # 100 lx: light is genuinely on
            lit_s += dt
    return {
        "dli": dli_today(key, k),
        "peak_lux": round(peak, 1),                       # sensor plane
        "peak_ppfd": (round(peak / kk * canopy_factor(), 1) if kk else None),  # canopy
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
        """Interpolate the measured curve at a dashboard brightness.

        The stored curve is RAW hardware response, so the dashboard percent
        goes through the same calibration the light itself uses first.
        """
        return light_mod.dashboard_lux(pct, cfg, pts)
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
        pct = light_mod.brightness_for(cfg, t, on_time, off_time)
        total += (lux_at(pct) * cf / k) * 60.0   # umol/m2 for this minute
        t += step
    return round(total / 1_000_000, 2)


MAX_SETUPS = 8


def setups(cfg=None):
    """The configured grow setups, or one default setup covering everything.

    Each: id, name, light ("main", "second" or "" for none), lux (the light
    sensor key or ""), k (lux-to-PPFD factor for that light's spectrum, None
    for the global one), sensors (keys shown for it; empty = all), trays (tray
    ids that belong to it; empty = all), and a DLI band dli_low / dli_high."""
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    got = [x for x in (cfg.get("setups") or []) if isinstance(x, dict)]
    if got:
        return got
    return [{"id": "main", "name": "Main", "light": "main", "lux": "lux",
             "k": None, "sensors": [], "trays": [],
             "dli_low": float(cfg.get("dli_target_low") or config.DEFAULTS["dli_target_low"]),
             "dli_high": float(cfg.get("dli_target_high") or config.DEFAULTS["dli_target_high"])}]


def setup_band(setup):
    lo, hi = float(setup.get("dli_low") or 0), float(setup.get("dli_high") or 0)
    if not 0 < lo < hi:                   # a hand-edited config.json; keep it usable
        lo, hi = config.DEFAULTS["dli_target_low"], config.DEFAULTS["dli_target_high"]
    return lo, hi


def setup_k(setup):
    try:
        k = float(setup.get("k") or 0)
    except (TypeError, ValueError):
        k = 0
    return k if k > 0 else lux_k()


FIXTURE_LABELS = {"pwm": "5V LED panel", "dim": "AC fixture (dim line)",
                  "kasa": "AC fixture (smart plug)"}


def light_options(cfg=None):
    """The lights a setup can be assigned, named by what they physically are.

    Internally a setup stores "main" or "second", because which fixture is
    main is a setting (light_backend) and the second light always takes the
    PWM output main is not using. People think in fixtures, so the names
    come from the wiring, and they follow a backend change on their own."""
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    main = light_mod.light_backend(cfg)
    opts = [{"value": "main", "label": FIXTURE_LABELS[main]}]
    if hardware.pwm2 is not None:              # the second light needs its own PWM channel
        sec = "pwm" if main != "pwm" else "dim"
        opts.append({"value": "second", "label": FIXTURE_LABELS[sec]
                     + ("" if cfg.get("light2_on") else " (turned off in Settings)")})
    return opts


def light_label(value, cfg=None):
    for o in light_options(cfg):
        if o["value"] == value:
            return o["label"]
    return {"main": "main light", "second": "second light"}.get(value, "no light")


def setup_window(cfg, setup, main_on, main_off):
    """(on, off) for a setup's own light today. The main light's window for
    the main light; the second light's clock window for the second (an end
    at or before the start runs past midnight); for no light, the span of
    both, so a verdict never judges a day before every light is out."""
    light = (setup or {}).get("light", "main")
    if light == "main" or main_on is None:
        return main_on, main_off
    tz = main_on.tzinfo
    day = datetime.now(tz).date()
    on2 = light_mod._clock(day, tz, cfg.get("light2_start"), "08:00")
    off2 = light_mod._clock(day, tz, cfg.get("light2_end"), "20:00")
    if off2 <= on2:
        off2 += timedelta(days=1)
    if light == "second":
        return on2, off2
    return min(main_on, on2), max(main_off, off2)


def camera_trays(cfg):
    """Tray ids the camera measures: its setup's trays, or every tray when the
    camera is not assigned to a setup with trays. Canopy is only computed,
    shown and reported for these; another tray's old canopy readings are the
    past, not the present."""
    cam = setup_with(cfg, "camera")
    got = [str(t) for t in ((cam or {}).get("trays") or [])]
    return got or sorted(str(t) for t in (cfg.get("trays") or {}))


def setup_with(cfg, flag):
    """The setup that has the fan or the camera ("fan" / "camera"), or None."""
    return next((st for st in setups(cfg) if st.get(flag)), None)


def main_lux_key(cfg=None):
    """The light sensor under the main light: what a calibration sweep reads."""
    for st in setups(cfg):
        if st.get("light") == "main" and st.get("lux"):
            return st["lux"]
    return "lux"


def dli_target(cfg=None):
    """The first setup's DLI band (low, high) in mol/m2/day."""
    return setup_band(setups(cfg)[0])


_dli_curve_cache = {}


def dli_curves(key, k, tz):
    """Cumulative DLI through today and through yesterday, as [[minute of
    the day, mol], ...] every 15 minutes, for the Day card's chart. Same
    integration as dli_between (gaps over 30 min are skipped). Cached for a
    minute: it reads two days of samples and a push can ask several times."""
    now = datetime.now(tz)
    ck = (key, k, int(time.time() // 60))
    if ck in _dli_curve_cache:
        return _dli_curve_cache[ck]
    k = k or lux_k()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    pts = db.series(key, hours=50) if k else []
    cf = canopy_factor()

    def curve(t0, t1):
        out, total, nxt = [[0, 0.0]], 0.0, t0 + 900
        seg = [(ts, v) for ts, v in pts if t0 <= ts <= t1]
        for (a, va), (b, vb) in zip(seg, seg[1:]):
            while b > nxt and nxt <= t1:
                out.append([round((nxt - t0) / 60), round(total * cf / 1e6, 2)])
                nxt += 900
            dt = b - a
            if 0 < dt <= 1800:
                total += ((va + vb) / 2 / k) * dt
        out.append([round((min(t1, seg[-1][0] if seg else t0) - t0) / 60),
                    round(total * cf / 1e6, 2)])
        return out if seg else []
    res = {"today": curve(midnight, now.timestamp()),
           "yesterday": curve(midnight - 86400, midnight)}
    if len(_dli_curve_cache) > 32:
        _dli_curve_cache.clear()
    _dli_curve_cache[ck] = res
    return res


def setup_status(cfg, setup, on_time, off_time, tz):
    """One setup as the dashboard shows it: its measured day and verdict."""
    key, k = setup.get("lux") or "", setup_k(setup)
    on_time, off_time = setup_window(cfg, setup, on_time, off_time)
    day = day_light_summary(key, k) if key else None
    if day:
        # What the rest of today should add, as the SENSOR recorded it over the
        # same hours yesterday: every light and every ramp included, with no
        # model of any fixture. Nothing is forecast without a clean record.
        now_ = datetime.now(tz)
        midnight = now_.replace(hour=0, minute=0, second=0, microsecond=0)
        rest = dli_between(now_.timestamp() - 86400, midnight.timestamp(), key, k)
        day["forecast_remaining"] = rest[0] if rest and rest[1] >= 0.9 else None
        day["curve"] = dli_curves(key, k, tz)
    lo, hi = setup_band(setup)
    return {"id": setup.get("id"), "name": setup.get("name"),
            "light": setup.get("light", ""), "lux": key, "k": k,
            "light_label": light_label(setup.get("light", ""), cfg) if setup.get("light") else "",
            "sensors": list(setup.get("sensors") or []),
            "trays": [str(t) for t in (setup.get("trays") or [])],
            "fan": bool(setup.get("fan")), "camera": bool(setup.get("camera")),
            "reservoir": bool(setup.get("reservoir")),
            "on": on_time.isoformat() if on_time else None,
            "off": off_time.isoformat() if off_time else None,
            "band": [lo, hi], "day": day,
            "plan": light_plan(cfg, on_time, off_time, setup)}


def light_plan(cfg, on_time, off_time, setup=None):
    """Judge a whole day's light from what the sensor measured.

    The last complete day as recorded, whatever lit it: one fixture, two, a
    window, a storm. Earlier this modelled the fixtures instead, which called
    a day "on track" at 10.8 mol while the sensor had measured 14.4.
    """
    with config.settings_lock:
        tz = ZoneInfo(config.settings["timezone"])
    now = datetime.now(tz)
    setup = setup or setups(cfg)[0]
    key, k = setup.get("lux") or "", setup_k(setup)
    if not key:
        return {"status": "no_sensor", "full_day": None, "day": None,
                "advice": [f"{setup.get('name', 'This setup')} has no light sensor "
                           "assigned, so its daily light is not measured."]}
    m = measured_day(cfg, now, off_time, key, k)
    if m is None:
        so_far = dli_today(key, k)
        return {"status": "pending", "full_day": so_far or 0.0, "day": None,
                "advice": ["Waiting for a full day measured by the light "
                           "sensor; the first one completes tonight."]}
    full, lit_h = m["mol"], m["lit_hours"]
    DLI_TARGET_LOW, DLI_TARGET_HIGH = setup_band(setup)
    # average light per lit hour, measured: what an hour more or less is worth
    per_hour = full / lit_h if lit_h > 0 else 0.0
    mx = float(cfg.get("light2_bright", 100) if setup.get("light") == "second"
               else cfg.get("max_bright", 100))
    plan = {"full_day": full, "per_hour": round(per_hour, 2),
            "hours": round(lit_h, 1), "status": "ok", "day": m["day"],
            "advice": [f"Measured by the light sensor {m['day']}: {full:.1f} mol "
                       f"over {lit_h:.1f}h of light."]}
    if full < DLI_TARGET_LOW:
        plan["status"] = "low"
        deficit = DLI_TARGET_LOW - full
        if per_hour > 0:
            add_h = deficit / per_hour
            plan["advice"].append(
                f"About {add_h:.1f}h more light, or brighter, to reach "
                f"{DLI_TARGET_LOW:g} mol." if lit_h + add_h <= 18 else
                "Even an 18h day would not close the gap at this intensity.")
    elif full > DLI_TARGET_HIGH:
        plan["status"] = "high"
        excess = full - DLI_TARGET_HIGH
        if per_hour > 0:
            plan["advice"].append(
                f"About {excess / per_hour:.1f}h more light than seedlings need; "
                f"shorten the photoperiod or dim to roughly "
                f"{mx * DLI_TARGET_HIGH / full:.0f}% max.")
    else:
        plan["advice"].append(
            f"Inside the {DLI_TARGET_LOW:g}-{DLI_TARGET_HIGH:g} mol seedling target.")
    return plan
