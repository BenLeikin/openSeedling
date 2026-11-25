#!/usr/bin/env python3
"""Sensor data quality: validation, filtering, and health scoring.

Everything here is a pure function over readings, with no I/O and no state
kept between calls, so each rule can be tested against logged data.

The rules say when a reading should be distrusted. Each rejection comes with
a verdict or a reason, so the fault behind it stays visible.
"""

import statistics
from collections.abc import Mapping, Sequence
from typing import Any

# ---------------------------------------------------------------- bounds ----
# Physically possible ranges. A value outside them is a wiring or driver
# fault: a probe reading the ADC supply rail means the signal is gone. Keyed
# by sensor prefix; the longest matching prefix wins, so "temp:soil" can
# differ from "temp:air".
BOUNDS: dict[str, tuple[float, float]] = {
    "probe:": (0.05, 3.25),  # volts on a 3.3V rail; rail or 0 = fault
    "temp:soil": (-5.0, 70.0),  # Celsius
    "temp:air": (-20.0, 60.0),
    "humidity": (0.0, 100.0),
    "pressure": (800.0, 1100.0),  # hPa at any habitable altitude
    "lux": (0.0, 200000.0),
    "canopy:": (0.0, 100.0),
    "float:": (0.0, 1.0),
    "reservoir:": (0.0, 1.0),
}


def bounds_for(sensor: str) -> tuple[float, float] | None:
    match = None
    for prefix, rng in BOUNDS.items():
        if sensor.startswith(prefix):
            if match is None or len(prefix) > len(match[0]):
                match = (prefix, rng)
    return match[1] if match else None


def check_bounds(sensor: str, value: Any) -> str | None:
    """None if plausible, else a short reason string."""
    rng = bounds_for(sensor)
    if rng is None:
        return None
    lo, hi = rng
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "not a number"
    if v != v:  # NaN
        return "not a number"
    if v < lo:
        return f"below the possible minimum ({v:g} < {lo:g})"
    if v > hi:
        return f"above the possible maximum ({v:g} > {hi:g})"
    return None


# ------------------------------------------------------ spike vs step ----
def spike_or_step(
    values: Sequence[float], jump: float = 0.08, confirm: int = 3, window: int = 9
) -> tuple[float | None, str]:
    """Tell a transient from a real level change.

    `values` is newest-first. Returns (accepted_value, verdict), where verdict
    is "steady", "spike" (rejected, the baseline is returned), "step" (a
    confirmed new level) or "trend" (moving steadily in one direction).
    Returns (None, "no data") when no value is usable.

    The newest reading is compared to a baseline: the median of up to
    `window` samples older than the newest `confirm`. A departure of more
    than `jump` is accepted only once `confirm` consecutive readings agree on
    the new level. A rolling median would hold a real step back for half its
    window.
    """
    # A reading can arrive as None (a failed read logged as a gap) or as
    # something non-numeric. Comparing those against a float raises, so they
    # are dropped here.
    clean = []
    for v in values or []:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f:  # drop NaN, which compares false against all
            clean.append(f)
    values = clean
    if not values:
        return None, "no data"
    newest = values[0]
    if len(values) < confirm + 2:
        return newest, "steady"  # not enough history to judge
    baseline_pool = values[confirm : confirm + window]
    if len(baseline_pool) < 2:
        return newest, "steady"
    baseline = statistics.median(baseline_pool)
    recent = values[:confirm]
    if abs(newest - baseline) <= jump:
        return newest, "steady"
    # departed from the baseline: do the last `confirm` samples agree?
    same_side = all((v - baseline) * (newest - baseline) > 0 for v in recent)
    tight = (max(recent) - min(recent)) <= jump
    if same_side:
        if tight and all(abs(v - baseline) > jump for v in recent):
            return statistics.median(recent), "step"
        # A steadily moving sensor (a tray drying over hours, humidity climbing)
        # must not be held at a lagging baseline: that delays an alert while
        # the reading climbs toward it. Monotonic movement, with every recent
        # sample more than half the jump from the baseline, separates it from
        # a spike decaying back.
        up = newest > baseline
        ordered = all((recent[i] - recent[i + 1] > 0) == up for i in range(len(recent) - 1))
        if ordered and all(abs(v - baseline) > jump / 2 for v in recent):
            return newest, "trend"
    return baseline, "spike"


# ---------------------------------------------------------- stuck value ----
def stuck_run(values: Sequence[float], epsilon: float = 1e-4) -> int:
    """How many of the newest readings are identical (within epsilon).

    A frozen I2C device and a disconnected probe pinned to a rail both report
    on time without measuring, which the stale-reading check cannot see.
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


def check_stuck(
    values: Sequence[float], cadence_s: float, min_run: int = 12, min_hours: float = 2.0
) -> str | None:
    """None, or a reason string when a sensor has flatlined.

    Needs both a run of identical values and enough elapsed time, so a
    slow-changing sensor sampled quickly is not flagged.
    """
    run = stuck_run(values)
    if run < min_run:
        return None
    if run * max(1.0, cadence_s) < min_hours * 3600:
        return None
    return (
        f"identical value for {run} readings (~{run * cadence_s / 3600:.1f}h); sensor may be frozen"
    )


# --------------------------------------------------- cross-sensor checks ----
def contradictions(
    snap: Mapping[str, Any], cfg: Mapping[str, Any] | None = None, pumped_recently: bool = False
) -> list[tuple[str, str]]:
    """Cross-sensor sanity checks; returns a list of (subject, message).

    These catch faults no single-sensor test can, but they can blame the
    wrong sensor, so they are advisory: shown on the dashboard, and not used
    by any alert or watering decision.
    """
    cfg = cfg or {}
    out = []

    # two probes in the same room under the same schedule should track each
    # other; one moving alone (with no pump run) suggests that one is at fault
    probes = {k[6:]: v for k, v in snap.items() if k.startswith("probe:")}
    if len(probes) == 2 and not pumped_recently:
        a, b = sorted(probes)
        if abs(probes[a] - probes[b]) > 0.60:
            out.append(
                (
                    "probe",
                    f"trays {a} and {b} disagree by "
                    f"{abs(probes[a] - probes[b]):.2f}V with no recent "
                    "watering; one probe may be faulty or misplaced",
                )
            )

    # a float reporting full while its probe reads dry: one of the two is wrong
    for t, v in probes.items():
        f = snap.get(f"float:{t}")
        cal = (cfg.get("probe_cal") or {}).get(t) or {}
        dry = cal.get("dry")
        if f is not None and dry is not None and f < 1 and v >= dry - 0.05:
            out.append(
                (
                    "probe",
                    f"tray {t} float says full but its probe reads "
                    "dry; check the probe placement or the float",
                )
            )

    # soil more than 15 C from air is expected only with a heat mat on
    st, at = snap.get("temp:soil"), snap.get("temp:air")
    if st is not None and at is not None and abs(st - at) > 15:
        out.append(
            (
                "temp:soil",
                f"soil is {abs(st - at):.0f}C from air temp; "
                "expected with a heat mat, otherwise suspect "
                "the probe",
            )
        )

    # lux during the photoperiod should not be zero
    if cfg.get("_light_on") and snap.get("lux") is not None and snap["lux"] < 1:
        out.append(
            (
                "lux",
                "the light is on but the sensor reads darkness; "
                "check the sensor or whether the fixture is lit",
            )
        )
    return out


# ------------------------------------------------ post-fill recovery ----
def postfill_verdict(
    volts: float | None, cal: Mapping[str, Any], tolerance: float = 0.15
) -> tuple[bool | None, str]:
    """Judge a post-fill probe reading against its wet anchor; (ok, message).

    Within `tolerance` volts of the anchor is healthy. Drift beyond it is the
    early warning for a degrading probe or a stale calibration. ok is None
    when there is no anchor or no reading.
    """
    wet = (cal or {}).get("wet")
    if wet is None or volts is None:
        return None, ""
    delta = volts - wet
    if abs(delta) <= tolerance:
        return True, f"post-fill {volts:.3f}V, wet anchor {wet:.3f}V: healthy"
    if delta > 0:
        return False, (
            f"post-fill {volts:.3f}V is {delta:.3f}V drier than the "
            f"wet anchor ({wet:.3f}V): the tray may not be filling, "
            "or the probe is drifting"
        )
    return False, (
        f"post-fill {volts:.3f}V is {abs(delta):.3f}V wetter than the "
        f"wet anchor ({wet:.3f}V): the wet anchor was captured too "
        "dry, so readings will peg at 100%"
    )


# --------------------------------------------------------- health score ----
def health(
    values: Sequence[float],
    *,
    age_s: float | None = None,
    cadence_s: float = 300,
    rejects: int = 0,
    verdict: str = "steady",
    noise_ref: float | None = None,
) -> tuple[int, str, list[str]]:
    """Per-sensor health; returns (score 0-100, grade, reasons).

    Each fault subtracts points and adds its reason.
    """
    score, why = 100, []
    if not values:
        return 0, "unknown", ["no readings"]

    if age_s is not None and cadence_s and age_s > 3 * cadence_s:
        score -= 40
        why.append(f"last reading {age_s / 60:.0f} min old")

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
    grade = "good" if score >= 80 else "fair" if score >= 55 else "poor" if score >= 30 else "bad"
    return score, grade, why
