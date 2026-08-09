"""Threshold alerting for the grow controller.

Design notes, because the failure mode of a naive alerter is worse than no
alerter at all:

* Every rule is a state machine, not a level check. A rule fires once when it
  becomes true and stays quiet until it clears, so a soil probe hovering on the
  threshold cannot spam the channel.
* Clearing uses hysteresis: a rule that fires at 90F does not clear until 88F.
  Without that margin, sensor noise alone toggles the state.
* Rules must be true for a sustained period before firing, so one bad reading
  from a probe or a momentary light-off during a capture is ignored.
* Everything is re-armed on a cooldown, so a genuinely persistent problem
  reminds you occasionally rather than either screaming or going silent.

State lives in memory only: a restart re-arms every rule, which is the safe
direction (you get told again) rather than silently suppressing.
"""

import time

# rule key -> {"active": bool, "since": ts, "last_sent": ts}
_state = {}

DEFAULTS = {
    "enabled": True,
    "sustain_seconds": 600,      # how long a condition must hold before firing
    "cooldown_seconds": 21600,   # 6h between reminders while still active
    "soil_temp_high_f": 0,       # 0 = follow the chart threshold setting
    "soil_temp_low_f": 60,
    "probe_dry_pct": 15,
    "humidity_high": 80,
    "dli_low": 4.0,              # checked once daily, near lights-off
}


def _rule(key):
    return _state.setdefault(key, {"active": False, "since": 0.0,
                                   "last_sent": 0.0})


def evaluate(key, condition, now=None):
    """Feed a rule its current truth value; get back what to do about it.

    Returns "fire" the first time a condition has held for long enough,
    "remind" when it is still true after the cooldown, "clear" on the
    transition back to normal, or None when there is nothing to say.
    """
    now = now if now is not None else time.time()
    st = _rule(key)
    cfg = _state.get("_cfg", DEFAULTS)
    sustain = cfg.get("sustain_seconds", 600)
    cooldown = cfg.get("cooldown_seconds", 21600)

    if condition:
        if not st["since"]:
            st["since"] = now                 # start the sustain clock
        held = now - st["since"]
        if not st["active"]:
            if held >= sustain:
                st.update(active=True, last_sent=now)
                return "fire"
            return None                        # still settling
        if now - st["last_sent"] >= cooldown:
            st["last_sent"] = now
            return "remind"
        return None

    # condition false
    st["since"] = 0.0
    if st["active"]:
        st["active"] = False
        return "clear"
    return None


def configure(cfg):
    _state["_cfg"] = {**DEFAULTS, **(cfg or {})}


def active_rules():
    return sorted(k for k, v in _state.items()
                  if k != "_cfg" and isinstance(v, dict) and v.get("active"))


def reset():
    _state.clear()


def check_all(snapshot, cfg, unit_temp="F"):
    """Run every rule against a reading snapshot.

    `snapshot` is {sensor_key: value} in storage units (Celsius, hPa, volts as
    logged) plus the derived keys this module needs. Returns a list of
    (action, key, title, message, level) to send.
    """
    configure(cfg)
    out = []
    now = time.time()

    def temp_disp(c):
        return c if unit_temp == "C" else c * 9 / 5 + 32

    tu = "\u00b0C" if unit_temp == "C" else "\u00b0F"

    # --- soil temperature, the one that ruins a germination run ---
    hi = cfg.get("soil_temp_high_f") or 0
    lo = cfg.get("soil_temp_low_f", DEFAULTS["soil_temp_low_f"])
    for key, val in snapshot.items():
        if not key.startswith("temp:soil"):
            continue
        f = val * 9 / 5 + 32
        label = "Soil" if key == "temp:soil" else key.split(":", 1)[1]
        if hi:
            act = evaluate(f"soil_hot:{key}", f > hi, now)
            if act in ("fire", "remind"):
                out.append((act, f"soil_hot:{key}", "Soil too warm",
                            f"{label} is {temp_disp(val):.1f}{tu}, above the "
                            f"{temp_disp((hi - 32) * 5 / 9):.0f}{tu} warning line. "
                            "Sustained heat past this stalls germination and "
                            "stretches seedlings.", "warn"))
            elif act == "clear":
                out.append((act, f"soil_hot:{key}", "Soil temperature back to normal",
                            f"{label} is {temp_disp(val):.1f}{tu}.", "good"))
        if lo:
            act = evaluate(f"soil_cold:{key}", f < lo, now)
            if act in ("fire", "remind"):
                out.append((act, f"soil_cold:{key}", "Soil too cold",
                            f"{label} is {temp_disp(val):.1f}{tu}, below "
                            f"{temp_disp((lo - 32) * 5 / 9):.0f}{tu}. Chile seed "
                            "germination slows sharply and seed rot risk rises.",
                            "warn"))
            elif act == "clear":
                out.append((act, f"soil_cold:{key}", "Soil temperature recovered",
                            f"{label} is {temp_disp(val):.1f}{tu}.", "good"))

    # --- tray moisture, as a percentage the caller has already converted ---
    dry_at = cfg.get("probe_dry_pct", DEFAULTS["probe_dry_pct"])
    for tray, pct in (snapshot.get("_moisture") or {}).items():
        if pct is None:
            continue
        act = evaluate(f"dry:{tray}", pct <= dry_at, now)
        if act in ("fire", "remind"):
            out.append((act, f"dry:{tray}", "Tray drying out",
                        f"{tray} moisture is {pct}%, at or below the {dry_at}% "
                        "alert level.", "warn"))
        elif act == "clear":
            out.append((act, f"dry:{tray}", "Tray moisture recovered",
                        f"{tray} is back to {pct}%.", "good"))

    # --- humidity: damping-off weather ---
    rh_hi = cfg.get("humidity_high", DEFAULTS["humidity_high"])
    rh = snapshot.get("humidity")
    if rh is not None and rh_hi:
        act = evaluate("humidity_high", rh >= rh_hi, now)
        if act in ("fire", "remind"):
            out.append((act, "humidity_high", "Humidity high",
                        f"Air is {rh:.0f}% RH, at or above {rh_hi}%. Combined "
                        "with warm soil this is damping-off weather; increase "
                        "airflow.", "warn"))
        elif act == "clear":
            out.append((act, "humidity_high", "Humidity back down",
                        f"Air is {rh:.0f}% RH.", "good"))

    # --- reservoir / fill failure, surfaced by the caller ---
    if snapshot.get("_fill_failed"):
        act = evaluate("fill_failed", True, now)
        if act in ("fire", "remind"):
            out.append((act, "fill_failed", "Watering did not complete",
                        str(snapshot["_fill_failed"]), "error"))
    else:
        act = evaluate("fill_failed", False, now)
        if act == "clear":
            out.append((act, "fill_failed", "Watering completed normally",
                        "A fill reached the float again.", "good"))

    # --- camera health, when the camera is in use ---
    cam_fails = snapshot.get("_camera_fails") or 0
    act = evaluate("camera", cam_fails >= 3, now)
    if act in ("fire", "remind"):
        out.append((act, "camera", "Camera not responding",
                    f"{cam_fails} consecutive capture failures. "
                    "Timelapse and the daily report are affected.", "warn"))
    elif act == "clear":
        out.append((act, "camera", "Camera recovered",
                    "Captures are succeeding again.", "good"))

    # --- sensors that have stopped reporting entirely ---
    stale = snapshot.get("_stale") or []
    act = evaluate("stale", bool(stale), now)
    if act in ("fire", "remind"):
        out.append((act, "stale", "Sensors not reporting",
                    "No recent readings from: " + ", ".join(sorted(stale))
                    + ". Check wiring and the service log.", "warn"))
    elif act == "clear":
        out.append((act, "stale", "Sensors reporting again",
                    "All sensors are logging.", "good"))

    return out
