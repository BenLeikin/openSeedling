"""Sensor readings on their way in and out of the database: implausible values
are dropped before they are stored, and what decisions read back has single
bad samples filtered out.

Sits below the modules that act on readings (heat mat, fan, watering, alerts)
so each can use it without importing the others.
"""

from applog import log
import db
import quality

import config

PROBE_CONFIRM = 3  # consecutive agreeing readings that make a change real

# How large a departure from the recent baseline counts as suspicious, per
# sensor, in that sensor's own units. A value under the threshold is passed
# through untouched, so ordinary drift is never filtered.
SENSOR_JUMP = {
    "probe:": 0.08,  # volts
    "temp:soil": 2.0,  # Celsius; a real soil change is slower than this
    "temp:air": 2.5,  # Celsius
    "humidity": 6.0,  # percent
    "pressure": 2.0,  # hPa
    "canopy:": 5.0,  # percent of tray area
}
# Lux has no entry: it steps by thousands when the light switches or a capture
# raises brightness, and the DLI integration needs the real values.


def sensor_jump(key):
    best = None
    for prefix, jump in SENSOR_JUMP.items():
        if key.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, jump)
    return best[1] if best else None


def reading_filtered(key, snap=None):
    """A sensor's latest value with single-sample transients rejected.

    Same machinery as the probes: compare the newest reading against a baseline
    of older ones, reject a lone excursion, accept a change that persists or
    that moves steadily. Sensors without a jump threshold (lux) are returned
    raw. Charts always show raw values; only decisions read this.

    Returns (value, ts) or (None, None).
    """
    if snap is None:
        snap = db.latest()
    rec = snap.get(key)
    if rec is None:
        return None, None
    ts, raw = rec
    jump = sensor_jump(key)
    if jump is None:
        return raw, ts
    with config.settings_lock:
        depth = int(config.settings.get("probe_median_depth", 5))
    if depth <= 1:
        return raw, ts
    try:
        vals = db.recent_values(key, n=max(depth, PROBE_CONFIRM + 6))
    except Exception as e:
        log.info(f"filter fell back to raw for {key} ({e})")
        return raw, ts
    if len(vals) < 3:
        return raw, ts
    val, _ = quality.spike_or_step(vals, jump=jump, confirm=PROBE_CONFIRM)
    return (val if val is not None else raw), ts


# sensor -> count of implausible readings rejected in the current session, and
# the reason for the most recent one. Feeds the health score and the dashboard.
reject_counts = {}
reject_last = {}


def validate_readings(readings):
    """Drop implausible values before they reach the database; returns the rest.

    A probe reading the 3.3V rail has lost its signal, and stored, that value
    would skew the charts, the alert thresholds and any later calibration.
    Each rejection is counted and logged as an event.
    """
    clean, dropped = {}, []
    for k, v in readings.items():
        why = quality.check_bounds(k, v)
        if why is None:
            clean[k] = v
            continue
        dropped.append((k, v, why))
        reject_counts[k] = reject_counts.get(k, 0) + 1
        reject_last[k] = why
    for k, v, why in dropped:
        log.info(f"rejected {k}={v}: {why}")
        try:
            db.log_event("sensor", f"rejected {k}={v}: {why}")
        except Exception:
            pass
    return clean
