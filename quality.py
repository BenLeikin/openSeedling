#!/usr/bin/env python3
"""Sensor data quality: validation, filtering, and health scoring.

Everything here is a pure function over readings. No I/O, no globals that
outlive a call, so each rule can be tested against real logged data rather
than reasoned about.

The guiding principle is that no algorithm recovers information a bad sensor
never provided. These rules exist to say WHEN a reading should be distrusted,
not to invent a better number. A rule that silently "corrects" a value is
worse than one that flags it, because it hides the fault that needs fixing.
"""
import statistics
import time

# ---------------------------------------------------------------- bounds ----
# Physically possible ranges. A value outside these is a wiring or driver
# fault, not a measurement: a probe reading the ADC supply rail means the
# signal is gone, not that the soil is dry. Keyed by sensor prefix, checked
# longest-prefix first so "temp:soil" can differ from "temp:air".
BOUNDS = {
    "probe:":      (0.05, 3.25),   # volts on a 3.3V rail; rail or 0 = fault
    "temp:soil":   (-5.0, 70.0),   # Celsius
    "temp:air":    (-20.0, 60.0),
    "humidity":    (0.0, 100.0),
    "pressure":    (800.0, 1100.0),  # hPa at any habitable altitude
    "lux":         (0.0, 200000.0),
    "canopy:":     (0.0, 100.0),
    "float:":      (0.0, 1.0),
    "reservoir:":  (0.0, 1.0),
}


def bounds_for(sensor):
    match = None
    for prefix, rng in BOUNDS.items():
        if sensor.startswith(prefix):
            if match is None or len(prefix) > len(match[0]):
                match = (prefix, rng)
    return match[1] if match else None


def check_bounds(sensor, value):
    """None if plausible, else a short reason string."""
    rng = bounds_for(sensor)
    if rng is None:
        return None
    lo, hi = rng
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "not a number"
    if v != v:                      # NaN
        return "not a number"
    if v < lo:
        return f"below the possible minimum ({v:g} < {lo:g})"
    if v > hi:
        return f"above the possible maximum ({v:g} > {hi:g})"
    return None


# ------------------------------------------------------ spike vs step ----
def spike_or_step(values, jump=0.08, confirm=3, window=9):
    """Discriminate a transient from a genuine level change.

    `values` newest-first. Returns (accepted_value, verdict) where verdict is
    "steady", "spike" (rejected, baseline returned), "step" (a confirmed new
    level, accepted) or "trend" (steadily moving in one direction, accepted).

    A plain rolling median rejects a spike but also smothers a real step for
    half its window, and if the disturbance outlasts the window it passes
    through anyway. This instead compares the newest reading to a baseline of
    older samples, and accepts a departure only once `confirm` consecutive
    readings agree on the new level. So a one-sample excursion is rejected
    outright, while a real change is adopted after `confirm` samples rather
    than being averaged away.
    """
    if not values:
        return None, "no data"
    newest = values[0]
    if len(values) < confirm + 2:
        return newest, "steady"      # not enough history to judge
    baseline_pool = values[confirm:confirm + window]
    if len(baseline_pool) < 2:
        return newest, "steady"
    baseline = statistics.median(baseline_pool)
    recent = values[:confirm]
    if abs(newest - baseline) <= jump:
        return newest, "steady"
    # departed from the baseline: do the last `confirm` samples agree?
    same_side = all((v - baseline) * (newest - baseline) > 0 for v in recent)
    tight = (max(recent) - min(recent)) <= jump
    if same_side and all(abs(v - baseline) > jump for v in recent):
        if tight:
            return statistics.median(recent), "step"
        # not tight, but if each reading is further from the baseline than the
        # one before it, this is a sensor steadily moving (a tray drying over
        # hours), not noise. Rejecting it would under-report dryness and delay
        # watering, so accept the newest value and say so.
        up = newest > baseline
        ordered = all((recent[i] - recent[i + 1] > 0) == up
                      for i in range(len(recent) - 1))
        if ordered:
            return newest, "trend"
    return baseline, "spike"


# ---------------------------------------------------------- stuck value ----
def stuck_run(values, epsilon=1e-4):
    """How many of the newest readings are identical (within epsilon).

    A sensor reporting the same number forever is broken in a way the
    stale-reading check cannot see: it is reporting on time, just not
    measuring. A frozen I2C device and a disconnected probe pinned to a rail
    both look like this.
    """
    if not values:
        return 0
    n = 1
    for v in values[1:]:
        if abs(v - values[0]) <= epsilon:
            n += 1
        else:
            break
    return n


def check_stuck(values, cadence_s, min_run=12, min_hours=2.0):
    """None, or a reason string when a sensor has flatlined.

    Requires BOTH a run of identical values and enough elapsed time, so a
    slow-changing sensor sampled quickly is not accused.
    """
    run = stuck_run(values)
    if run < min_run:
        return None
    if run * max(1.0, cadence_s) < min_hours * 3600:
        return None
    return (f"identical value for {run} readings "
            f"(~{run * cadence_s / 3600:.1f}h); sensor may be frozen")


# --------------------------------------------------- cross-sensor checks ----
def contradictions(snap, cfg=None, pumped_recently=False):
    """Advisory cross-sensor sanity checks -> list of (subject, message).

    These catch faults no single-sensor test can, but they can also accuse the
    wrong sensor, so they are advisory: surfaced on the dashboard, never wired
    to an alert or to a watering decision.
    """
    cfg = cfg or {}
    out = []

    # two probes in the same room under the same schedule should track each
    # other; one moving alone (with no pump run) suggests that one is at fault
    probes = {k[6:]: v for k, v in snap.items() if k.startswith("probe:")}
    if len(probes) == 2 and not pumped_recently:
        a, b = sorted(probes)
        if abs(probes[a] - probes[b]) > 0.60:
            out.append(("probe", f"trays {a} and {b} disagree by "
                                 f"{abs(probes[a]-probes[b]):.2f}V with no recent "
                                 "watering; one probe may be faulty or misplaced"))

    # a float reporting full while its probe reads bone dry is a contradiction
    # that one of the two is wrong about
    for t, v in probes.items():
        f = snap.get(f"float:{t}")
        cal = ((cfg.get("probe_cal") or {}).get(t) or {})
        dry = cal.get("dry")
        if f is not None and dry is not None and f < 1 and v >= dry - 0.05:
            out.append(("probe", f"tray {t} float says full but its probe reads "
                                 "dry; check the probe placement or the float"))

    # soil far from air with no heat source explains itself only if a mat is on
    st, at = snap.get("temp:soil"), snap.get("temp:air")
    if st is not None and at is not None and abs(st - at) > 15:
        out.append(("temp:soil", f"soil is {abs(st-at):.0f}C from air temp; "
                                 "expected with a heat mat, otherwise suspect "
                                 "the probe"))

    # lux during the photoperiod should not be zero
    if cfg.get("_light_on") and snap.get("lux") is not None and snap["lux"] < 1:
        out.append(("lux", "the light is on but the sensor reads darkness; "
                           "check the sensor or whether the fixture is lit"))
    return out


# ------------------------------------------------ post-fill recovery ----
def postfill_verdict(volts, cal, tolerance=0.15):
    """After a fill the probe should return near its wet anchor.

    Drift here is the early warning for a probe degrading or a calibration
    going stale, which is exactly the failure that hid two pegged probes for a
    week. Returns (ok, message).
    """
    wet = (cal or {}).get("wet")
    if wet is None or volts is None:
        return None, ""
    delta = volts - wet
    if abs(delta) <= tolerance:
        return True, f"post-fill {volts:.3f}V, wet anchor {wet:.3f}V: healthy"
    if delta > 0:
        return False, (f"post-fill {volts:.3f}V is {delta:.3f}V drier than the "
                       f"wet anchor ({wet:.3f}V): the tray may not be filling, "
                       "or the probe is drifting")
    return False, (f"post-fill {volts:.3f}V is {abs(delta):.3f}V wetter than the "
                   f"wet anchor ({wet:.3f}V): the wet anchor was captured too "
                   "dry, so readings will peg at 100%")


# --------------------------------------------------------- health score ----
def health(values, *, age_s=None, cadence_s=300, rejects=0, verdict="steady",
           noise_ref=None):
    """Per-sensor health -> (score 0-100, grade, reasons).

    Deliberately simple and explainable: each fault subtracts a fixed amount
    and names itself. A score nobody can explain is a score nobody trusts.
    """
    score, why = 100, []
    if not values:
        return 0, "unknown", ["no readings"]

    if age_s is not None and cadence_s and age_s > 3 * cadence_s:
        score -= 40
        why.append(f"last reading {age_s/60:.0f} min old")

    stuck = check_stuck(values, cadence_s)
    if stuck:
        score -= 40
        why.append(stuck)

    if rejects:
        score -= min(30, 6 * rejects)
        why.append(f"{rejects} implausible reading(s) rejected recently")

    if verdict == "spike":
        score -= 10
        why.append("a transient was rejected on the latest reading")

    if noise_ref and len(values) >= 6:
        try:
            spread = statistics.pstdev(values[:12])
            if spread > noise_ref * 3:
                score -= 20
                why.append(f"noisy: spread {spread:.3f} vs {noise_ref:.3f} typical")
        except statistics.StatisticsError:
            pass

    score = max(0, min(100, score))
    grade = ("good" if score >= 80 else
             "fair" if score >= 55 else
             "poor" if score >= 30 else "bad")
    return score, grade, why
