"""Watering: pump runs and fills, the reservoir, probe filtering and
moisture, auto-water decisions, post-fill checks and pump persistence."""

import time

from applog import log
import db
import sensors
import quality

import config
import hardware

# a fill that ran to its cap without the float tripping: the message feeds the
# alert state machine (fires on the next sample tick, reminds while unresolved)
# and is cleared by the next successful fill
fill_failure = {"msg": ""}

# What a restart must not forget. last_run drives the auto-water cooldown and
# today_seconds the daily pump cap; both used to live only in memory, so a
# restart reset them, and a service that restarted repeatedly could water past
# the cap. "running" is deliberately not kept: after a restart no pump is on.
PERSIST_PUMP_KEYS = ("last_run", "today_seconds", "day", "last_detail")


def save_persistent_state():
    """Write pump history and the failed-fill notice to the database."""
    try:
        data = {t: {k: st.get(k) for k in PERSIST_PUMP_KEYS}
                for t, st in hardware.pump_state.items()}
        db.kv_set("pump_state", data)
        db.kv_set("fill_failure", {"msg": fill_failure.get("msg", "")})
    except Exception as e:
        log.error(f"could not save pump state: {e}")


def restore_persistent_state():
    """Load what save_persistent_state wrote, validating as it goes."""
    try:
        db.init()
        data = db.kv_get("pump_state") or {}
    except Exception as e:
        log.error(f"could not restore pump state: {e}")
        return
    today = config._today_str()
    for tray, saved in (data.items() if isinstance(data, dict) else []):
        st = hardware.pump_state.get(str(tray))
        if st is None or not isinstance(saved, dict):
            continue                  # a tray that is no longer wired
        try:
            st["last_run"] = max(0.0, float(saved.get("last_run") or 0))
            st["today_seconds"] = max(0.0, float(saved.get("today_seconds") or 0))
        except (TypeError, ValueError):
            continue
        st["day"] = str(saved.get("day") or "")
        st["last_detail"] = str(saved.get("last_detail") or "")
        # a new day since it was saved: the daily total starts over, but the
        # last run time still stands for the cooldown
        if st["day"] != today:
            st["day"] = today
            st["today_seconds"] = 0.0
    ff = db.kv_get("fill_failure")
    if isinstance(ff, dict):
        fill_failure["msg"] = str(ff.get("msg") or "")
    restored = {t: round(st["today_seconds"], 1) for t, st in hardware.pump_state.items()}
    log.info(f"restored pump state: today {restored}s")

# Auto-watering used to be one switch for every tray. A config from then with
# it on arms every tray with a pump, once, so an upgrade changes nothing.
if config.settings.get("auto_water") and "auto_water_trays" not in config._file_keys:
    config.settings["auto_water_trays"] = sorted(hardware.PUMP_PINS)
    try:
        config.save_config()
    except Exception as e:
        log.warning(f"auto-water tray list not persisted ({e})")


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
    with config.settings_lock:
        cap = float(config.settings.get("pump_max_seconds", 20))
        daily_cap = float(config.settings.get("pump_daily_max_seconds", 180))
    secs = max(0.0, min(float(seconds), cap))
    with hardware.pump_lock:
        if hardware.SHUTTING_DOWN.is_set():
            return False, "shutting down"
        if tray not in hardware._pumps:
            return False, f"pump {tray} hardware not available"
        if any(st["running"] for st in hardware.pump_state.values()):
            return False, "a pump is already running"
        if not force and reservoir_state() == "empty":
            return False, "reservoir is empty; refusing to run the pump dry"
        st = hardware.pump_state[tray]
        if st["day"] != config._today_str():
            st["day"] = config._today_str()
            st["today_seconds"] = 0.0
        if not force and st["today_seconds"] + secs > daily_cap:
            return False, f"tray {tray} daily pump limit reached"
        st["running"] = True
    elapsed = 0.0
    try:
        hardware._pumps[tray].on()
        t0 = time.time()
        hardware.SHUTTING_DOWN.wait(secs)          # a shutdown ends the run early
        elapsed = time.time() - t0
    finally:
        hardware._pumps[tray].off()
        with hardware.pump_lock:
            st["running"] = False
            st["last_run"] = time.time()
            st["today_seconds"] += elapsed
            st["last_detail"] = f"{reason} {elapsed:.1f}s"
    save_persistent_state()
    try:
        db.log_event("pump", f"tray {tray}: {reason} {elapsed:.1f}s")
    except Exception:
        pass
    status_mod.publish("pump")
    return True, f"ran {elapsed:.1f}s"


def run_pump_until_full(tray, reason="fill", force=False):
    """Run the pump until the float reads full, then stop. A hard time cap is
    the backstop: if the float never trips within fill_max_seconds the pump
    stops anyway and the run is flagged, because that means the source is empty,
    the tube is off, or the float failed. Float reading None (sensor lost) is
    treated as 'stop' (fail-safe). Blocks; call in a thread. Logs the run."""
    tray = str(tray)
    with config.settings_lock:
        cap = float(config.settings.get("fill_max_seconds", 60))
        daily_cap = float(config.settings.get("pump_daily_max_seconds", 180))
    with hardware.pump_lock:
        if tray not in hardware._pumps:
            return False, f"pump {tray} hardware not available"
        if any(st["running"] for st in hardware.pump_state.values()):
            return False, "a pump is already running"
        if not force and reservoir_state() == "empty":
            return False, "reservoir is empty; refusing to run the pump dry"
        st = hardware.pump_state[tray]
        if st["day"] != config._today_str():
            st["day"] = config._today_str()
            st["today_seconds"] = 0.0
        if hardware.SHUTTING_DOWN.is_set():
            return False, "shutting down"
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
    stop_why = "cap"                         # cap | float_lost | reservoir
    confirm = 0                              # consecutive "full" reads needed
    CONFIRM_NEEDED = 4                        # ~0.4s steady, rejects slosh/bobble
    try:
        hardware._pumps[tray].on()
        t0 = time.time()
        while True:
            elapsed = time.time() - t0
            if elapsed >= run_cap:
                break                        # cap hit, float never stayed full
            if hardware.SHUTTING_DOWN.is_set():
                stop_why = "shutdown"
                break
            if not force and reservoir_state() == "empty":
                stop_why = "reservoir"       # ran the source dry mid-fill
                break
            fv = sensors.read_float(tray)
            if fv is None or fv < 1:         # full (open) or sensor lost
                confirm += 1
                if confirm >= CONFIRM_NEEDED:
                    tripped = (fv is not None and fv < 1)
                    if not tripped:
                        stop_why = "float_lost"
                    break                    # full held steady -> stop
            else:
                confirm = 0                  # a not-full read resets the count
            time.sleep(0.1)                  # poll the float ~10x/sec
    finally:
        hardware._pumps[tray].off()
        with hardware.pump_lock:
            st["running"] = False
            st["last_run"] = time.time()
            st["today_seconds"] += elapsed
            if tripped:
                detail = f"tray {tray} {reason}: full at {elapsed:.1f}s"
            elif stop_why == "reservoir":
                detail = (f"tray {tray} {reason}: STOPPED at {elapsed:.1f}s, "
                          "reservoir ran empty")
            elif stop_why == "float_lost":
                detail = (f"tray {tray} {reason}: STOPPED at {elapsed:.1f}s, "
                          "float sensor stopped answering")
            elif stop_why == "shutdown":
                detail = (f"tray {tray} {reason}: STOPPED at {elapsed:.1f}s, "
                          "service shutting down")
            else:
                detail = (f"tray {tray} {reason}: STOPPED at {elapsed:.1f}s cap, "
                          "no float trip")
            st["last_detail"] = detail
    try:
        db.log_event("pump", detail)
    except Exception:
        pass
    status_mod.publish("pump")
    if tripped:
        fill_failure["msg"] = ""             # a good fill resolves the alert
        save_persistent_state()
        schedule_postfill(tray, tripped=True)   # judge the probe once water wicks
        return True, f"filled in {elapsed:.1f}s"
    if stop_why == "shutdown":
        # Not a failed fill: no alert, and auto-watering stays as it was, so
        # the service comes back up the way it went down.
        save_persistent_state()
        return False, f"stopped at {elapsed:.1f}s, service shutting down"
    fill_failure["msg"] = {
        "reservoir": f"Tray {tray} fill stopped at {elapsed:.1f}s: the "
                     "reservoir ran empty. Refill it before watering again.",
        "float_lost": f"Tray {tray} fill stopped at {elapsed:.1f}s: the float "
                      "switch stopped answering. Check its wiring.",
    }.get(stop_why, f"Tray {tray} fill ran to the {elapsed:.1f}s cap "
                    "without the float tripping. Likely causes: source "
                    "empty, tube off, or float stuck.")
    # a fill that can't complete means auto-watering must not keep trying
    # this tray; the others keep their own arming
    with config.settings_lock:
        if tray in armed_trays(config.settings):
            left = [t for t in armed_trays(config.settings) if t != tray]
            config.settings["auto_water_trays"] = left
            config.settings["auto_water"] = bool(left)
            fill_failure["msg"] += f" Auto-watering has been switched off for tray {tray}."
            try:
                config.save_config()
            except Exception as e:
                log.warning(f"auto_water disable not persisted ({e})")
    save_persistent_state()
    return False, {"reservoir": f"stopped at {elapsed:.1f}s, reservoir empty",
                   "float_lost": f"stopped at {elapsed:.1f}s, float sensor lost",
                   }.get(stop_why,
                         f"ran to {elapsed:.1f}s cap without float trip (source empty?)")


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
    with config.settings_lock:
        depth = int(config.settings.get("probe_median_depth", 5))
    depth = max(depth, PROBE_CONFIRM + 6) if depth > 1 else depth
    if depth <= 1:
        return raw, ts
    try:
        vals = db.recent_values(key, n=depth)
    except Exception as e:
        log.info(f"probe filter fell back to raw ({e})")
        return raw, ts
    vals = [v for v in vals if isinstance(v, (int, float))]
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


def armed_trays(cfg):
    """Trays armed for auto-watering (see the startup migration for configs
    from before arming was per tray)."""
    got = [str(t) for t in (cfg.get("auto_water_trays") or [])]
    return [t for t in got if t in hardware.PUMP_PINS]


def auto_water_blockers(cfg=None):
    """Reasons auto-watering must not run, per tray -> [reasons].

    Auto-watering decides to pump based on a probe reading, so an untrustworthy
    probe is not a degraded feature, it is a flood or a drought. A tray with any
    blocker is skipped by the loop and cannot be armed from the UI.
    """
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    cal = cfg.get("probe_cal") or {}
    snap = db.latest()
    out = {}
    for tray in sorted(set(hardware.PUMP_PINS) | set(sensors.FLOAT_PINS)):
        why = []
        if tray not in hardware._pumps:
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


def schedule_postfill(tray, tripped=True):
    """After a successful fill, judge the probe against its wet anchor later.

    Water takes time to wick from the tray to the probe, so the check is
    deferred rather than taken immediately. Drift here is the early warning
    for a degrading probe or a stale calibration.
    """
    with config.settings_lock:
        cal = ((config.settings.get("probe_cal") or {}).get(str(tray)) or {}).copy()
    # Scheduled even with no wet anchor yet: there is no verdict to give in
    # that case, but it is exactly when an automatic first capture is most
    # useful, and skipping it here would silently rule that out.
    _postfill_due[str(tray)] = {"due": time.time() + POSTFILL_DELAY_S,
                                "cal": cal, "tripped": bool(tripped)}


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
        # ok is None when there is no anchor to judge against; that is not a
        # reason to skip the recapture below, which is what would create one
        postfill_result[tray] = {"ok": None if ok is None else bool(ok),
                                 "msg": msg, "ts": time.time()}
        if ok is not None:
            log.info(f"post-fill check tray {tray}: {msg}")
            try:
                db.log_event("probe", f"tray {tray} post-fill: {msg}")
            except Exception:
                pass
        note = auto_wet_calibrate(tray, pending)
        if note:
            postfill_result[tray]["cal"] = note


def auto_wet_calibrate(tray, pending):
    """Re-capture the wet anchor after a fill, when the fill deserves it.

    Fill-to-float saturates the medium, so the settled reading afterwards is a
    legitimate wet reference. The risk is not the soil, it is everything else:
    a fill that tripped the float early, a probe that has shifted in its hole,
    a partial fill. A stale anchor announces itself (pegged readings, the recal
    badge, this very check); an anchor silently rewritten to a wrong value
    looks perfectly healthy, which is why this refuses more than it accepts:

      - only after a fill that actually tripped the float, never one that ran
        to the time cap
      - only if the reading passes every check a manual capture must pass
      - only if it moves the anchor less than auto_wet_cal_max_move; a larger
        jump is real news and wants a human to look at it

    Returns a short note for the dashboard, or None when it did nothing.
    """
    with config.settings_lock:
        cfg = dict(config.settings)
    if not cfg.get("auto_wet_cal"):
        return None
    if not pending.get("tripped"):
        return "not recalibrated: that fill did not trip the float"

    live, spread, drift = sensors.probe_settle(tray, seconds=PROBE_SETTLE_S)
    if live is None:
        return None
    problems, _notes = probe_cal_check(tray, "wet", live, drift, spread)
    if problems:
        return f"not recalibrated: {problems[0]}"

    old = ((cfg.get("probe_cal") or {}).get(tray) or {}).get("wet")
    limit = float(cfg.get("auto_wet_cal_max_move", 0.10))
    if old is not None and abs(live - old) > limit:
        return (f"not recalibrated: the wet anchor would move "
                f"{abs(live - old):.3f}V, more than the {limit:.2f}V limit. "
                "Capture it by hand if that is real")

    with config.settings_lock:
        config.settings.setdefault("probe_cal", {}).setdefault(tray, {})["wet"] = live
        config.save_config()
    moved = "" if old is None else f" (was {old:.3f}V)"
    msg = f"wet anchor recalibrated to {live:.3f}V{moved}"
    log.info(f"tray {tray}: {msg}")
    try:
        db.log_event("probe", f"tray {tray} auto {msg}")
    except Exception:
        pass
    return msg


# How far a fresh reading may sit above the threshold and still count as
# agreeing with the decision to water. Enough to absorb noise between the
# logged sample and the fresh one, nowhere near enough to water a wet tray.
AUTO_WATER_AGREE_PCT = 10


def auto_water_pass():
    """One auto-watering decision: water at most one tray, or none.

    Split out of the loop so it can be exercised directly, and so every exit
    is an explicit return rather than a continue inside a thread.
    """
    with config.settings_lock:
        cfg = dict(config.settings)
    armed = set(armed_trays(cfg))
    if not armed:
        return
    blockers = auto_water_blockers(cfg)
    threshold = float(cfg.get("moisture_threshold_pct", 30))
    cooldown = float(cfg.get("pump_cooldown_min", 30)) * 60
    cal = cfg.get("probe_cal") or {}
    snap = db.latest()
    stf = latest_soil_temp_f(snap)
    for tray in sorted(hardware.PUMP_PINS):
        if tray in blockers or tray not in armed:
            continue
        volts, ts = probe_volts_filtered(tray, snap)
        if volts is None:
            continue
        if time.time() - ts > 3600:
            continue          # stale reading: do not water on old data
        pct = probe_moisture(volts, cal.get(tray) or {}, stf)
        if pct is None or pct > threshold:
            continue
        st = hardware.pump_state.get(tray) or {}
        if time.time() - st.get("last_run", 0.0) < cooldown:
            continue
        f = sensors.read_float(tray)
        if f is None or f < 1:
            continue          # no float, or already full

        # Confirm before pumping. The decision above came from the
        # logged history; take a fresh reading now and require it to
        # agree. A tray that reads wet right now is not watered, however
        # the history got there. This exists because tray 2 was once
        # filled at "0%" while every logged input said 97%, and nothing
        # recorded what the loop had actually seen.
        cal_t = cal.get(tray) or {}
        fresh_v, spread = sensors.probe_spread(tray)
        fresh_pct = (probe_moisture(fresh_v, cal_t, stf)
                     if fresh_v is not None else None)
        inputs = (f"logged {volts:.4f}V -> {pct:.0f}%, fresh "
                  f"{'n/a' if fresh_v is None else f'{fresh_v:.4f}V'}"
                  f" -> {'n/a' if fresh_pct is None else f'{fresh_pct:.0f}%'}"
                  f" (spread {'n/a' if spread is None else f'{spread:.3f}V'})"
                  f", anchors wet {cal_t.get('wet')} dry {cal_t.get('dry')}"
                  f", soil {'n/a' if stf is None else f'{stf:.1f}F'}"
                  f", threshold {threshold:.0f}%")
        if fresh_pct is None or fresh_pct > threshold + AUTO_WATER_AGREE_PCT:
            log.warning(f"auto-water tray {tray} NOT run: the fresh reading "
                        f"disagrees. {inputs}")
            try:
                db.log_event("auto_water", f"tray {tray} skipped, readings "
                             f"disagree: {inputs}")
            except Exception:
                pass
            continue

        ok, msg = run_pump_until_full(tray, reason="auto")
        log.info(f"auto-water tray {tray}: {msg}. {inputs}")
        if ok:
            try:
                db.log_event("auto_water",
                             f"tray {tray} at {pct:.0f}% moisture: {msg}. {inputs}")
            except Exception:
                pass
        return                # one pump per pass; re-evaluate next minute


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
            auto_water_pass()
        except Exception as e:
            log.exception(f"watering_loop error: {e}")
# tray -> pending post-fill probe check: {"due": ts, "cal": {...}}
_postfill_due = {}
# tray -> last post-fill verdict for display
postfill_result = {}


PROBE_SETTLE_S   = 15.0   # how long to watch before accepting an anchor
PROBE_NOISE_MAX  = 0.05   # volts of spread; above this the run is too noisy
PROBE_DRIFT_MAX  = 0.015  # volts of movement across the window; still changing
PROBE_SPAN_MIN   = 0.15   # volts between wet and dry for a usable scale


def probe_cal_check(tray, point, volts, drift, spread):
    """Decide whether a proposed anchor is believable -> (problems, notes).

    Anchors used to be stored whatever they looked like, with noise only
    mentioned afterwards. That is how two wet anchors captured on
    not-quite-saturated soil pegged both trays at 100% for a week and silently
    disabled the dry alert. A suspect capture is now refused, with the reason
    and what to do instead.
    """
    problems, notes = [], []
    with config.settings_lock:
        cal = dict((config.settings.get("probe_cal") or {}).get(tray) or {})
    other = cal.get("dry" if point == "wet" else "wet")

    if spread is not None and spread > PROBE_NOISE_MAX:
        problems.append(
            f"the reading is jumping around by {spread:.3f}V while sampling, "
            "which points at electrical noise on the probe lead rather than "
            "anything about the soil")

    if drift is not None and abs(drift) > PROBE_DRIFT_MAX:
        if point == "wet" and drift < 0:
            problems.append(
                f"still falling ({drift:+.3f}V over the sample): the soil is "
                "still absorbing. Wait until it stops moving, usually about "
                "30 minutes after watering, then capture")
        elif point == "dry" and drift > 0:
            problems.append(
                f"still rising ({drift:+.3f}V over the sample): the soil is "
                "still drying. Capture once it levels off")
        else:
            notes.append(f"moved {drift:+.3f}V during the sample")

    # a wet anchor has to be at least as wet as the tray actually gets, or every
    # ordinary reading sits below it and clamps to 100%
    try:
        recent = db.recent_values(f"probe:{tray}", n=400, max_age=14 * 86400)
    except Exception:
        recent = []
    if recent and point == "wet":
        lowest = min(recent)
        if volts > lowest + PROBE_CAL_SLOP:
            problems.append(
                f"{volts:.3f}V is drier than this tray's own recent low of "
                f"{lowest:.3f}V, so normal readings would fall below the anchor "
                "and peg at 100%. The soil is not saturated yet")
        else:
            notes.append(f"wetter than the 14-day low of {lowest:.3f}V")
    if recent and point == "dry":
        highest = max(recent)
        if volts < highest - PROBE_CAL_SLOP:
            problems.append(
                f"{volts:.3f}V is wetter than this tray's recent high of "
                f"{highest:.3f}V, so dry soil would read past the anchor. "
                "Capture this one when the tray is genuinely dry")

    if other is not None:
        span = (other - volts) if point == "wet" else (volts - other)
        if span < PROBE_SPAN_MIN:
            problems.append(
                f"only {span:.3f}V between wet and dry, too small a range to "
                "read a percentage from. Check the probe is inserted to its "
                "line and that the other anchor is right")
        else:
            notes.append(f"{span:.3f}V between wet and dry")
    return problems, notes

# Imported last: these modules import this one, and their import-time
# code runs only after everything above is defined. Their names are
# used inside functions, at call time, always as module.name.
import status as status_mod
