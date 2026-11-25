"""Threshold alerting for the grow controller.

* Every rule is a state machine, not a level check. It fires once when its
  condition has held for the sustain period, reminds once a cooldown (a day by
  default) while the problem lasts, and says so when it clears.
* Clearing uses hysteresis: a rule that fires at 90F does not clear until 88F,
  so sensor noise at the threshold does not toggle it.
* The sustain period means one bad reading, or the light going off for a
  moment during a capture, is ignored.

Rule states are plain data: snapshot() and restore() let the caller keep them
across a restart, so a restart does not announce every standing problem again.
"""

import time
from dataclasses import dataclass

WARNING, ERROR, GOOD = "warning", "error", "good"  # the levels a notice can have

# rule key -> {"active": bool, "since": ts, "last_sent": ts}
_state = {}

DEFAULTS = {
    "sustain_seconds": 600,  # how long a condition must hold before firing
    "cooldown_seconds": 86400,  # a day between reminders while still active
    "soil_temp_high_f": 0,  # 0 disables
    "soil_temp_low_f": 60,
    "probe_dry_pct": 15,
    "humidity_high": 80,
    "dli_low": 4.0,  # checked once daily, near lights-off
    "dli_high": 0.0,  # ceiling counterpart; 0 disables
}


def _rule(key):
    return _state.setdefault(
        key, {"active": False, "since": 0.0, "last_sent": 0.0, "clear_since": 0.0}
    )


def evaluate(key, condition, now=None, release=None, hold=None, clear_hold=0.0):
    """Feed a rule its current truth value; get back what to do about it.

    Returns "fire" the first time a condition has held for long enough,
    "remind" when it is still true after the cooldown, "clear" on the
    transition back to normal, or None when there is nothing to say.

    `release`: the condition for clearing while active. Defaults to
    `not condition`, but analog rules should pass a hysteresis margin
    (clear a 90F alarm at 88F, not 89.9F) so noise at the threshold cannot
    flap the rule.
    `hold`: seconds the condition must persist before firing; defaults to the
    configured sustain. Pass 0 for discrete events (a fill failure is a fact
    the moment it happens, not a level that needs to settle).
    `clear_hold`: seconds the release condition must persist before clearing.
    0 keeps the old immediate-clear behavior for event rules; analog rules
    should pass the sustain so one spurious in-range reading cannot emit a
    Resolved notice and re-arm the alarm.
    """
    now = now if now is not None else time.time()
    st = _rule(key)
    cfg = _state.get("_cfg", DEFAULTS)
    sustain = cfg.get("sustain_seconds", 600) if hold is None else hold
    cooldown = cfg.get("cooldown_seconds", 21600)

    if not st["active"]:
        if condition:
            if not st["since"]:
                st["since"] = now  # start the sustain clock
            if now - st["since"] >= sustain:
                st.update(active=True, last_sent=now, clear_since=0.0)
                return "fire"
            return None  # still settling
        st["since"] = 0.0
        return None

    # active: decide between remind and clear
    rel = (not condition) if release is None else bool(release)
    if rel:
        if not st["clear_since"]:
            st["clear_since"] = now
        if now - st["clear_since"] >= clear_hold:
            st.update(active=False, since=0.0, clear_since=0.0)
            return "clear"
        return None  # recovering, not confirmed yet
    st["clear_since"] = 0.0
    if now - st["last_sent"] >= cooldown:
        st["last_sent"] = now
        return "remind"
    return None


def snapshot():
    """The rule states worth keeping across a restart: rules that are active,
    and rules part way through their sustain period."""
    return {
        key: dict(st)
        for key, st in _state.items()
        if key != "_cfg" and isinstance(st, dict) and (st.get("active") or st.get("since"))
    }


def restore(saved):
    """Take up rule states from an earlier snapshot(). Anything malformed is
    skipped: that rule starts fresh, which at worst repeats one notice."""
    if not isinstance(saved, dict):
        return
    for key, st in saved.items():
        if not isinstance(st, dict):
            continue
        try:
            _state[str(key)] = {
                "active": bool(st.get("active")),
                "since": float(st.get("since") or 0),
                "last_sent": float(st.get("last_sent") or 0),
                "clear_since": float(st.get("clear_since") or 0),
            }
        except (TypeError, ValueError):
            continue


def configure(cfg):
    _state["_cfg"] = {**DEFAULTS, **(cfg or {})}


def reset():
    _state.clear()


@dataclass
class Rule:
    """One thing to judge on this pass: is it wrong now, and what to say.

    `release` is the condition for clearing while active; None means "no
    longer wrong". Analog rules pass a margin past the threshold instead, so
    noise at the threshold cannot flap them. `hold` is the seconds the
    condition must last before firing (None: the configured sustain; 0 for a
    fact rather than a reading), `clear_hold` the same for clearing.
    """

    key: str
    wrong: bool
    title: str
    message: str
    level: str = WARNING
    clear_title: str = ""
    clear_message: str = ""
    release: bool | None = None
    hold: float | None = None
    clear_hold: float = 0.0


TEMP_MARGIN_F = 2.0  # clear a temperature rule this far past its threshold
PCT_MARGIN = 3  # and a percentage rule this many points past


def check_all(snapshot, cfg, unit_temp="F"):
    """Run every rule against a reading snapshot.

    `snapshot` is {sensor_key: value} in storage units (Celsius, hPa, volts as
    logged) plus the derived keys the rules below name. Returns a list of
    (action, key, title, message, level) to send.
    """
    configure(cfg)
    now = time.time()
    out = []
    for rule in _rules(snapshot, cfg, unit_temp):
        act = evaluate(
            rule.key, rule.wrong, now, rule.release, rule.hold, clear_hold=rule.clear_hold
        )
        if act in ("fire", "remind"):
            out.append((act, rule.key, rule.title, rule.message, rule.level))
        elif act == "clear" and rule.clear_title:
            out.append((act, rule.key, rule.clear_title, rule.clear_message, GOOD))
    return out


def _rules(snapshot, cfg, unit_temp):
    """Every rule that applies to this snapshot, in the order they are sent."""
    sustain = cfg.get("sustain_seconds", DEFAULTS["sustain_seconds"])
    yield from _soil_temperature(snapshot, cfg, unit_temp, sustain)
    yield from _moisture(snapshot, cfg, sustain)
    yield from _humidity(snapshot, cfg, sustain)
    yield from _daily_light(snapshot, cfg)
    yield from _reservoir(snapshot, sustain)
    yield from _faults(snapshot)
    yield from _per_subject(
        "stuck",
        snapshot.get("_stuck") or {},
        lambda key, why: (f"{key} is not changing", why, WARNING),
        lambda key: (f"{key} is changing again", "The sensor is reporting varying values."),
    )
    _state.pop("fill_failed", None)  # the single rule from before failures were per tray
    yield from _per_subject(
        "fill_failed",
        snapshot.get("_fill_failed") or {},
        lambda tray, why: ("Watering did not complete", str(why), ERROR),
        lambda tray: (
            "Watering completed normally",
            f"Tray {tray}'s fill reached the float again.",
        ),
    )
    cam_fails = snapshot.get("_camera_fails") or 0
    yield Rule(
        "camera",
        cam_fails >= 3,
        "Camera not responding",
        f"{cam_fails} consecutive capture failures. Timelapse and the daily report are affected.",
        clear_title="Camera recovered",
        clear_message="Captures are succeeding again.",
    )
    stale = snapshot.get("_stale") or []
    yield Rule(
        "stale",
        bool(stale),
        "Sensors not reporting",
        "No recent readings from: "
        + ", ".join(sorted(stale))
        + ". Check wiring and the service log.",
        clear_title="Sensors reporting again",
        clear_message="All sensors are logging.",
    )


def _soil_temperature(snapshot, cfg, unit_temp, sustain):
    """Too warm and too cold, for each soil probe."""

    def shown(c):
        return c if unit_temp == "C" else c * 9 / 5 + 32

    tu = "\u00b0C" if unit_temp == "C" else "\u00b0F"
    hi = cfg.get("soil_temp_high_f") or 0
    lo = cfg.get("soil_temp_low_f", DEFAULTS["soil_temp_low_f"])
    for key, val in snapshot.items():
        if not key.startswith("temp:soil"):
            continue
        f = val * 9 / 5 + 32
        label = "Soil" if key == "temp:soil" else key.split(":", 1)[1]
        reads = f"{label} is {shown(val):.1f}{tu}"
        if hi:
            yield Rule(
                f"soil_hot:{key}",
                f > hi,
                "Soil too warm",
                f"{reads}, above the {shown((hi - 32) * 5 / 9):.0f}{tu} warning line. "
                "Sustained heat past this stalls germination and stretches seedlings.",
                clear_title="Soil temperature back to normal",
                clear_message=f"{reads}.",
                release=f < hi - TEMP_MARGIN_F,
                clear_hold=sustain,
            )
        if lo:
            yield Rule(
                f"soil_cold:{key}",
                f < lo,
                "Soil too cold",
                f"{reads}, below {shown((lo - 32) * 5 / 9):.0f}{tu}. Germination "
                "slows sharply and the risk of seed rot rises.",
                clear_title="Soil temperature recovered",
                clear_message=f"{reads}.",
                release=f > lo + TEMP_MARGIN_F,
                clear_hold=sustain,
            )


def _moisture(snapshot, cfg, sustain):
    """A tray drying out. `_moisture` is {tray name: percent}, calibrated."""
    dry_at = cfg.get("probe_dry_pct", DEFAULTS["probe_dry_pct"])
    for tray, pct in (snapshot.get("_moisture") or {}).items():
        if pct is None:
            continue
        yield Rule(
            f"dry:{tray}",
            pct <= dry_at,
            "Tray drying out",
            f"{tray} moisture is {pct}%, at or below the {dry_at}% alert level.",
            clear_title="Tray moisture recovered",
            clear_message=f"{tray} is back to {pct}%.",
            release=pct > dry_at + PCT_MARGIN,
            clear_hold=sustain,
        )


def _humidity(snapshot, cfg, sustain):
    rh_hi = cfg.get("humidity_high", DEFAULTS["humidity_high"])
    rh = snapshot.get("humidity")
    if rh is None or not rh_hi:
        return
    yield Rule(
        "humidity_high",
        rh >= rh_hi,
        "Humidity high",
        f"Air is {rh:.0f}% RH, at or above {rh_hi}%. Combined with warm soil this is "
        "damping-off weather; increase airflow.",
        clear_title="Humidity back down",
        clear_message=f"Air is {rh:.0f}% RH.",
        release=rh < rh_hi - PCT_MARGIN,
        clear_hold=sustain,
    )


def _daily_light(snapshot, cfg):
    """The day's light total, judged once, just after lights-off.

    The caller supplies `_dli` (one setup) or `_dli_setups` (several, each on
    its own sensor) only inside that window, so nothing is evaluated, or
    cleared, outside it: a short day fires once and a good day clears it.
    """
    low = cfg.get("dli_low", DEFAULTS["dli_low"])
    high = cfg.get("dli_high", DEFAULTS["dli_high"])
    per_setup = snapshot.get("_dli_setups") or {}
    for st in cfg.get("setups") or []:
        d = per_setup.get(st.get("id"))
        if d is None:
            continue
        sid, name = st.get("id"), st.get("name") or st.get("id")
        band_lo, band_hi = st.get("band") or (10, 15)
        done = f"{name} finished at {d:.1f} mol/m2"
        if low:
            yield Rule(
                f"dli_low:{sid}",
                d < low,
                f"{name}: short light day",
                f"{done}, below the {low:g} mol alert level; its target is "
                f"{band_lo:g}-{band_hi:g} mol/day.",
                clear_title=f"{name}: light back on target",
                clear_message=f"{done}.",
                hold=0,
            )
        if high:
            yield Rule(
                f"dli_high:{sid}",
                d > high,
                f"{name}: too much light today",
                f"{done}, above the {high:g} mol ceiling.",
                clear_title=f"{name}: light back under the ceiling",
                clear_message=f"{done}.",
                hold=0,
            )
    d = snapshot.get("_dli")
    if d is None:
        return
    done = f"Today finished at {d:.1f} mol/m2"
    if low:
        target = "{:g}-{:g}".format(*cfg.get("dli_target", (15, 20)))
        yield Rule(
            "dli_low",
            d < low,
            "Short light day",
            f"{done}, below the {low:g} mol target. The light was off, dimmed, or blocked "
            f"for part of the photoperiod; the seedling target is {target} mol/day.",
            clear_title="Light back on target",
            clear_message=f"{done}.",
            hold=0,
        )
    if high:
        yield Rule(
            "dli_high",
            d > high,
            "Too much light today",
            f"{done}, above the {high:g} mol ceiling. Turn the fixture down or raise it; "
            "too much light bleaches seedlings and wastes power.",
            clear_title="Light back under the ceiling",
            clear_message=f"{done}.",
            hold=0,
        )


def _reservoir(snapshot, sustain):
    """Empty stops watering; a fault means a sensor is lying."""
    res = snapshot.get("_reservoir")
    if not res:
        return
    # the clear is sustained so slosh from a pump, or a wave during a refill,
    # cannot flap it: only water solidly back at the low sensor clears
    yield Rule(
        "res_empty",
        res == "empty",
        "Reservoir empty",
        "No water at either reservoir sensor. Pump runs are refused until it is refilled.",
        ERROR,
        clear_title="Reservoir refilled",
        clear_message=f"Water level is back ({res}).",
        release=res in ("ok", "full"),
        clear_hold=sustain,
    )
    yield Rule(
        "res_fault",
        res == "fault",
        "Reservoir sensor fault",
        "The high sensor reads water but the low one does not, which is physically "
        "impossible. A sensor died, slipped off the wall, or needs its sensitivity pot "
        "adjusted.",
        clear_title="Reservoir sensors agree again",
        clear_message=f"Level reads {res}.",
    )


def _faults(snapshot):
    """Faults the caller has already worked out: the snapshot holds a
    description while one stands, and nothing when it does not."""
    slow = snapshot.get("_camera_slow_link")
    yield Rule(
        "camera_slow_link",
        bool(slow),
        "Camera on a slow USB link",
        f"The camera is connected at {slow} instead of 480 Mbit/s, so large photo sizes "
        "are missing or fail. A USB hub or cable is holding it back: plug the camera in "
        "directly, or use a USB 2.0 high-speed hub (scripts/usbcheck.sh tests one).",
        ERROR,
        clear_title="Camera USB link back to full speed",
        clear_message="The camera is connected at 480 Mbit/s again.",
    )
    disk = snapshot.get("_disk_low")
    yield Rule(
        "disk_low",
        bool(disk),
        "SD card nearly full",
        f"{disk}. Archived timelapse runs are being removed, oldest first (the current "
        "run is never touched); if none are left, start a new timelapse or free space by "
        "hand. A full card stops the database and settings writes.",
        ERROR,
        clear_title="SD card space recovered",
        clear_message="Free space is back above the alert level.",
    )
    heat = snapshot.get("_heat_fault")
    yield Rule(
        "heat_fault",
        bool(heat),
        "Heat mat held off",
        f"The heat mat is off for safety: {heat}.",
        ERROR,
        clear_title="Heat mat back under control",
        clear_message="The soil temperature is reading and the plug is responding.",
    )
    plug = snapshot.get("_plug_failed")
    yield Rule(
        "plug_failed",
        bool(plug),
        "Light plug not responding",
        "The smart plug is not accepting commands, so the light is stuck wherever it "
        f"last was. {plug}",
        ERROR,
        clear_title="Light plug responding again",
        clear_message="Plug commands are succeeding.",
    )


def _per_subject(prefix, wrong_now, fire_text, clear_text):
    """One rule per subject (a sensor, a tray), named `prefix:subject`.

    `wrong_now` is {subject: why}. A subject that had a rule and is no longer
    listed is judged too, as no longer wrong, so its rule clears.
    """
    for subject, why in sorted(wrong_now.items()):
        title, message, level = fire_text(subject, why)
        yield Rule(f"{prefix}:{subject}", True, title, message, level, hold=0)
    cut = len(prefix) + 1
    for subject in [k[cut:] for k in list(_state) if k.startswith(prefix + ":")]:
        if subject not in wrong_now:
            title, message = clear_text(subject)
            yield Rule(
                f"{prefix}:{subject}",
                False,
                "",
                "",
                clear_title=title,
                clear_message=message,
                hold=0,
            )
