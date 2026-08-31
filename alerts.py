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
    "dli_high": 0.0,            # ceiling counterpart; 0 disables
}


def _rule(key):
    return _state.setdefault(key, {"active": False, "since": 0.0,
                                   "last_sent": 0.0, "clear_since": 0.0})


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
                st["since"] = now             # start the sustain clock
            if now - st["since"] >= sustain:
                st.update(active=True, last_sent=now, clear_since=0.0)
                return "fire"
            return None                        # still settling
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
        return None                            # recovering, not confirmed yet
    st["clear_since"] = 0.0
    if now - st["last_sent"] >= cooldown:
        st["last_sent"] = now
        return "remind"
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

    sustain = cfg.get("sustain_seconds", DEFAULTS["sustain_seconds"])
    TEMP_MARGIN_F = 2.0     # hysteresis: clear 2F past the threshold
    PCT_MARGIN = 3          # ...and 3 points for the percentage rules

    # --- soil temperature, the one that ruins a germination run ---
    hi = cfg.get("soil_temp_high_f") or 0
    lo = cfg.get("soil_temp_low_f", DEFAULTS["soil_temp_low_f"])
    for key, val in snapshot.items():
        if not key.startswith("temp:soil"):
            continue
        f = val * 9 / 5 + 32
        label = "Soil" if key == "temp:soil" else key.split(":", 1)[1]
        if hi:
            act = evaluate(f"soil_hot:{key}", f > hi, now,
                           release=f < hi - TEMP_MARGIN_F, clear_hold=sustain)
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
            act = evaluate(f"soil_cold:{key}", f < lo, now,
                           release=f > lo + TEMP_MARGIN_F, clear_hold=sustain)
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
        act = evaluate(f"dry:{tray}", pct <= dry_at, now,
                       release=pct > dry_at + PCT_MARGIN, clear_hold=sustain)
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
        act = evaluate("humidity_high", rh >= rh_hi, now,
                       release=rh < rh_hi - PCT_MARGIN, clear_hold=sustain)
        if act in ("fire", "remind"):
            out.append((act, "humidity_high", "Humidity high",
                        f"Air is {rh:.0f}% RH, at or above {rh_hi}%. Combined "
                        "with warm soil this is damping-off weather; increase "
                        "airflow.", "warn"))
        elif act == "clear":
            out.append((act, "humidity_high", "Humidity back down",
                        f"Air is {rh:.0f}% RH.", "good"))

    # --- daily light total, judged just after lights-off. The caller only
    # supplies _dli inside that window, so no evaluation (and no clear)
    # happens outside it; a low day fires once, a good day clears.
    dli_low = cfg.get("dli_low", DEFAULTS["dli_low"])
    d = snapshot.get("_dli")
    if d is not None and dli_low:
        act = evaluate("dli_low", d < dli_low, now, hold=0)
        if act in ("fire", "remind"):
            out.append((act, "dli_low", "Short light day",
                        f"Today finished at {d:.1f} mol/m2, below the "
                        f"{dli_low:g} mol target. The light was off, dimmed, "
                        "or blocked for part of the photoperiod; seedlings "
                        "want 6-12 mol/day.", "warn"))
        elif act == "clear":
            out.append((act, "dli_low", "Light back on target",
                        f"Today finished at {d:.1f} mol/m2.", "good"))

    # ceiling: the other half of the loop when intensity is set by hand on the
    # fixture and the controller can only observe the result
    dli_high = cfg.get("dli_high", DEFAULTS["dli_high"])
    if d is not None and dli_high:
        act = evaluate("dli_high", d > dli_high, now, hold=0)
        if act in ("fire", "remind"):
            out.append((act, "dli_high", "Too much light today",
                        f"Today finished at {d:.1f} mol/m2, above the "
                        f"{dli_high:g} mol ceiling. Turn the fixture down or "
                        "raise it; too much light bleaches seedlings and wastes "
                        "power.", "warn"))
        elif act == "clear":
            out.append((act, "dli_high", "Light back under the ceiling",
                        f"Today finished at {d:.1f} mol/m2.", "good"))

    # --- reservoir level: empty stops watering, fault means a lying sensor ---
    res = snapshot.get("_reservoir")
    if res:
        # sustained so pump slosh or a wave during a refill can't flap it;
        # clear only once water is solidly back at the low sensor
        act = evaluate("res_empty", res == "empty", now,
                       release=res in ("ok", "full"), clear_hold=sustain)
        if act in ("fire", "remind"):
            out.append((act, "res_empty", "Reservoir empty",
                        "No water at either reservoir sensor. Pump runs are "
                        "refused until it is refilled.", "error"))
        elif act == "clear":
            out.append((act, "res_empty", "Reservoir refilled",
                        f"Water level is back ({res}).", "good"))
        act = evaluate("res_fault", res == "fault", now)
        if act in ("fire", "remind"):
            out.append((act, "res_fault", "Reservoir sensor fault",
                        "The high sensor reads water but the low one does "
                        "not, which is physically impossible. A sensor died, "
                        "slipped off the wall, or needs its sensitivity pot "
                        "adjusted.", "warn"))
        elif act == "clear":
            out.append((act, "res_fault", "Reservoir sensors agree again",
                        f"Level reads {res}.", "good"))

    if snapshot.get("_plug_failed"):
        act = evaluate("plug_failed", True, now)
        if act in ("fire", "remind"):
            out.append((act, "plug_failed", "Light plug not responding",
                        "The smart plug is not accepting commands, so the light "
                        "is stuck wherever it last was. "
                        + str(snapshot["_plug_failed"]), "error"))
    else:
        act = evaluate("plug_failed", False, now)
        if act == "clear":
            out.append((act, "plug_failed", "Light plug responding again",
                        "Plug commands are succeeding.", "good"))

    stuck = snapshot.get("_stuck") or {}
    for key, why in sorted(stuck.items()):
        act = evaluate(f"stuck:{key}", True, now, hold=0)
        if act in ("fire", "remind"):
            out.append((act, f"stuck:{key}", f"{key} is not changing", why, "warn"))
    for key in [k[6:] for k in list(_state) if k.startswith("stuck:")]:
        if key not in stuck:
            act = evaluate(f"stuck:{key}", False, now, hold=0)
            if act == "clear":
                out.append((act, f"stuck:{key}", f"{key} is changing again",
                            "The sensor is reporting varying values.", "good"))

    # --- reservoir / fill failure, surfaced by the caller ---
    if snapshot.get("_fill_failed"):
        act = evaluate("fill_failed", True, now, hold=0)
        if act in ("fire", "remind"):
            out.append((act, "fill_failed", "Watering did not complete",
                        str(snapshot["_fill_failed"]), "error"))
    else:
        act = evaluate("fill_failed", False, now, hold=0)
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
