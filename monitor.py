"""Sampling and live readings, the reading filter, validation, sensor
health, threshold alerts, and the daily AI report."""

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from applog import log
import db
import sensors
import notify
import ai_report
import discord_alert
import alerts
import quality

import config
import hardware
import light as light_mod
import setups as setups_mod
import water

AI_REPORT_PATH = Path(__file__).with_name("ai_report.json")
report_lock = threading.Lock()
report_state = {"generating": False}


# How large a departure from the recent baseline counts as suspicious, per
# sensor. These are in each sensor's own units, which is the whole point: 0.08V
# is meaningful for a probe and meaningless for humidity. A value under the
# threshold is passed through untouched, so ordinary drift is never filtered.
SENSOR_JUMP = {
    "probe:":    0.08,   # volts
    "temp:soil": 2.0,    # Celsius; a real soil change is slower than this
    "temp:air":  2.5,    # Celsius
    "humidity":  6.0,    # percent
    "pressure":  2.0,    # hPa
    "canopy:":   5.0,    # percent of tray area
}
# Deliberately absent: lux. It legitimately steps by thousands the moment the
# light switches or a capture raises brightness, and the DLI integration needs
# the real values. Filtering it would either reject genuine transitions or
# corrupt the day's light total.


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
        vals = db.recent_values(key, n=max(depth, water.PROBE_CONFIRM + 6))
    except Exception as e:
        log.info(f"filter fell back to raw for {key} ({e})")
        return raw, ts
    if len(vals) < 3:
        return raw, ts
    val, verdict = quality.spike_or_step(vals, jump=jump, confirm=water.PROBE_CONFIRM)
    _filter_verdict[key] = verdict
    return (val if val is not None else raw), ts


_filter_verdict = {}    # sensor -> "steady" | "spike" | "step" | "trend"


def gather_report_data():
    """Assemble the controller snapshot the AI report is built from."""
    with config.settings_lock:
        cfg = dict(config.settings)
    # the photo is of the camera's setup, so its light is the one that matters
    _cam = setups_mod.setup_with(cfg, "camera") or setups_mod.setups(cfg)[0]
    with config.state_lock:
        st = dict(config.state)
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    pcal = cfg.get("probe_cal") or {}
    pnames = cfg.get("probe_names") or {}
    canopy, probes, soiltemp, env = {}, {}, {}, {}
    trays_cfg = cfg.get("trays") or {}
    snap = db.latest()
    stf = water.latest_soil_temp_f(snap)
    for k, (ts, v) in snap.items():
        if k.startswith("canopy:"):
            tid = k[7:]
            if tid not in setups_mod.camera_trays(cfg):
                continue                  # an old reading from a tray it no longer sees
            label = ((trays_cfg.get(tid) or {}).get("label")
                     or f"Tray {tid}")
            canopy[label] = round(v, 1)
        elif k.startswith("probe:"):
            t = k[6:]
            tcal = pcal.get(t) or {}
            fv, _ = water.probe_volts_filtered(t, snap)
            if fv is None:
                fv = v
            pm, approx = water.probe_moisture_any(fv, tcal, stf)
            nm = pnames.get(t) or f"Soil moisture {t}"
            flag = water.probe_cal_flag(fv, tcal)
            if flag:
                nm += (" (reading beyond the wet anchor - recalibrate wet)"
                       if flag == "below_wet"
                       else " (reading beyond the dry anchor - recalibrate dry)")
            elif approx:
                nm += " (approx)"
            if pm is not None:
                probes[nm] = pm
            else:
                probes[nm] = round(v, 3)
        elif k.startswith("temp:soil"):
            label = "Soil" if k == "temp:soil" else f"Soil {k.split('_')[-1]}"
            soiltemp[label] = status_mod.temp_out(v)
        elif k == "temp:air":
            env["air_f"] = status_mod.temp_out(v)
        elif k == "pressure":
            env["pressure"] = status_mod.press_out(v)
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
            sown = spr = None
            if v.get("planted"):
                try:
                    sown = datetime.strptime(v["planted"], "%Y-%m-%d").date()
                except ValueError:
                    sown = None
            if v.get("sprouted"):
                try:
                    spr = datetime.strptime(v["sprouted"], "%Y-%m-%d").date()
                except ValueError:
                    spr = None
            if v.get("archived"):
                bits.append(f"TRANSPLANTED {v['archived']}")
            elif spr and sown:
                bits.append(f"sprouted in {(spr - sown).days}d, "
                            f"{(now.date() - spr).days}d old")
            elif spr:
                bits.append(f"sprouted {v['sprouted']}")
            elif sown:
                bits.append(f"sown {v['planted']} ({(now.date() - sown).days}d ago, "
                            "not yet sprouted)")
            if v.get("count"):
                bits.append(f"({v['count']} seeds)")
            if v.get("notes"):
                bits.append(f"- {v['notes']}")
            if bits:
                rows.append(f"{cid}: {' '.join(bits)}")
        if rows:
            planting[t.get("label", f"Tray {tid}")] = rows

    # Per-variety germination, from live cells AND finished plantings. A cell
    # that was transplanted still germinated; one that died before sprouting is
    # a real failure. Counting only what is currently in the trays would make
    # the rate drift upward every time a success was potted on.
    germ = {}
    hist = []
    try:
        hist = db.plantings(limit=500)
    except Exception as e:
        log.warning(f"planting history unavailable ({e})")
    for h in hist:
        seed = (h.get("seed") or "").strip()
        if not seed or not h.get("planted"):
            continue
        g = germ.setdefault(seed, {"sown": 0, "up": 0, "days": [],
                                   "seeds": 0, "sources": set(),
                                   "died": 0, "moved": 0})
        g["sown"] += 1
        g["seeds"] += int(h.get("count") or 0)
        if h.get("source"):
            g["sources"].add(h["source"])
        if h.get("outcome") == "died":
            g["died"] += 1
        else:
            g["moved"] += 1
        if h.get("sprouted"):
            g["up"] += 1
            try:
                d0 = datetime.strptime(h["planted"], "%Y-%m-%d").date()
                d1 = datetime.strptime(h["sprouted"], "%Y-%m-%d").date()
                g["days"].append((d1 - d0).days)
            except ValueError:
                pass
    for tid, t in sorted((cfg.get("trays") or {}).items()):
        for cid, v in (t.get("cells") or {}).items():
            seed = (v.get("seed") or "").strip()
            if not seed or not v.get("planted"):
                continue
            g = germ.setdefault(seed, {"sown": 0, "up": 0, "days": [],
                                       "seeds": 0, "sources": set(),
                                       "died": 0, "moved": 0})
            g["sown"] += 1
            g["seeds"] += int(v.get("count") or 0)
            if v.get("source"):
                g["sources"].add(v["source"])
            if v.get("sprouted"):
                g["up"] += 1
                try:
                    d0 = datetime.strptime(v["planted"], "%Y-%m-%d").date()
                    d1 = datetime.strptime(v["sprouted"], "%Y-%m-%d").date()
                    g["days"].append((d1 - d0).days)
                except ValueError:
                    pass
    germ_out = {}
    for seed, g in sorted(germ.items()):
        line = f"{g['up']}/{g['sown']} cells up"
        if g["seeds"]:
            line += f" ({g['seeds']} seeds sown)"
        if g["days"]:
            line += f", avg {sum(g['days']) / len(g['days']):.1f}d to sprout"
        if g["sources"]:
            line += f", seed from {', '.join(sorted(g['sources']))}"
        # losses are the signal the report needs to tell a variety that
        # germinates badly from one that germinates and then damps off
        if g.get("died"):
            line += f", {g['died']} lost"
        if g.get("moved"):
            line += f", {g['moved']} transplanted out"
        germ_out[seed] = line
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
        "canopy": canopy,
        "probe_moisture": probes,
        "soil_temp_f": soiltemp,
        "environment": env,
        "fan": (f"{'on' if hardware.fan_state['on'] else 'off'}"
                + (f" at {hardware.fan_state['speed']}% ({hardware.fan_state['reason']})"
                   if hardware.fan_state["on"] else f" ({hardware.fan_state['reason']})")
                + f", mode {cfg.get('fan_mode', 'auto')}") if hardware.FAN_HW else None,
        "pressure_trend": pressure_tendency(),
        "light_metrics": {"ppfd": setups_mod.ppfd_from_lux((snap.get("lux") or (None, None))[1]),
                          "dli": (setups_mod.dli_today(_cam["lux"], setups_mod.setup_k(_cam))
                                  if _cam.get("lux") else None),
                          "dli_target": list(setups_mod.setup_band(_cam)),
                          "photo_setup": _cam.get("name") if len(setups_mod.setups(cfg)) > 1 else None,
                          "setups": [{"name": st.get("name"),
                                      "dli": (setups_mod.dli_today(st["lux"], setups_mod.setup_k(st))
                                              if st.get("lux") else None),
                                      "band": list(setups_mod.setup_band(st))}
                                     for st in setups_mod.setups(cfg)]},
        "planting": planting,
        "germination": germ_out,
        "units": {"temp": status_mod.temp_unit(), "press": status_mod.press_unit()},
        "float": flabel,
        "reservoir": water.reservoir_state(),
        # pump_state is keyed by tray; sum across trays, latest detail wins
        "pump_today_s": round(sum(s["today_seconds"]
                                  for s in hardware.pump_state.values()), 1),
        "pump_last": next((s["last_detail"] for s in
                           sorted(hardware.pump_state.values(),
                                  key=lambda s: s["last_run"], reverse=True)
                           if s["last_detail"]), "none"),
        "notes": cfg.get("ai_notes", ""),
    }


def run_report(reason="daily"):
    """Generate one AI report: gather data + latest photo, call the API, store
    the result, and push the summary. Serialized via report_lock."""
    with config.settings_lock:
        if not config.settings.get("camera_enabled"):
            return {"ok": False, "error": "camera features are disabled in settings"}
    with report_lock:
        if report_state["generating"]:
            return {"ok": False, "error": "a report is already being generated"}
        report_state["generating"] = True
    try:
        with config.settings_lock:
            cfg = dict(config.settings)
        if not ai_report.have_key():
            return {"ok": False, "error": "no API key on the controller"}
        photos = sorted(p for p in camera_mod.TIMELAPSE_DIR.glob("*.jpg")
                        if not p.name.startswith("_"))
        # prefer scheduled frames: a manual (_m) capture can be off-schedule
        # and dark; fall back to manual only when nothing else exists
        sched = [p for p in photos if not p.stem.endswith("_m")]
        photo = (sched or photos)[-1] if photos else None
        with config.settings_lock:
            _flat_on = config.settings.get("timelapse_flatten", True)
        result = ai_report.generate(photo, gather_report_data(),
                                    crop=None if _flat_on else camera_mod.crop_box(),
                                    model=cfg.get("ai_model"))
        result["reason"] = reason
        try:
            AI_REPORT_PATH.write_text(json.dumps(result, indent=2))
        except Exception as e:
            log.error(f"report save error: {e}")
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


def _kernel_clock_synced():
    """Ask the kernel whether an NTP daemon has synchronized the clock.

    chrony and systemd-timesyncd both clear STA_UNSYNC once synced; adjtimex()
    then returns something other than TIME_ERROR. This is the same flag behind
    timedatectl's "System clock synchronized". Returns None when the call is
    unavailable (off-Linux, restricted container)."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        buf = ctypes.create_string_buffer(512)   # struct timex; modes=0 only reads
        state = libc.adjtimex(buf)
    except Exception:
        return None
    if state < 0:
        return None
    return state != 5                            # 5 = TIME_ERROR: not synchronized


def clock_synced():
    """True once the system clock is trustworthy. The Pi Zero 2 W has no RTC, so
    at boot the time is wrong until NTP corrects it. Priming the report schedule
    on that wrong time, then having the clock jump forward past the target, is
    what fires a report on every reboot.

    systemd-timesyncd creates the flag file below; chrony does not, so the
    kernel's sync status is the general check. The year test is only a last
    resort: at boot the clock already reads a recent year, so it proves little."""
    if os.path.exists("/run/systemd/timesync/synchronized"):
        return True
    synced = _kernel_clock_synced()
    if synced is not None:
        return synced
    return datetime.now().year >= 2025


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
            with config.settings_lock:
                cfg = dict(config.settings)
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
                    log.info("running daily AI report")
                    run_report("daily")
                    last_day = now.date()
        except Exception as e:
            log.exception(f"report_loop error: {e}")
        time.sleep(60)


def sample_loop():
    """Read all sensors on an interval, log them in one transaction, and run
    daily downsampling. Tolerant: a read failure logs nothing and tries again
    next tick rather than killing the thread."""
    db.init()
    last_prune = 0.0
    while True:
        with config.settings_lock:
            interval = max(1, int(config.settings.get("sample_interval_min", 5)))
        try:
            readings = validate_readings(sensors.read_all())
            if readings:
                db.log_many(list(readings.items()))
                run_alerts(readings)
                water.check_postfill(readings)
                status_mod.publish("sample")
        except Exception as e:
            log.exception(f"sample_loop error: {e}")
        # housekeeping once a day: roll raw -> hourly, prune old raw
        now = time.time()
        if now - last_prune > 86400:
            try:
                db.downsample_and_prune()
            except Exception as e:
                log.error(f"prune error: {e}")
            last_prune = now
        time.sleep(interval * 60)


# --- sensor data quality state (in memory; nothing here is worth persisting) ---
# sensor -> count of implausible readings rejected in the current session, and
# the reason for the most recent one. Feeds the health score and the dashboard.
_reject_counts = {}
_reject_last = {}
# cached health payload; recomputing per status poll would hammer sqlite
_health_cache = {"ts": 0.0, "data": {}}


def validate_readings(readings):
    """Drop implausible values before they ever reach the database.

    A probe reading the 3.3V rail means the signal is gone, not that the soil
    is bone dry. Letting that into history corrupts the charts, the alert
    thresholds and any calibration done afterwards, and it is indistinguishable
    from real data later. Rejections are counted and logged as events so a
    wiring fault is visible rather than silent.
    """
    clean, dropped = {}, []
    for k, v in readings.items():
        why = quality.check_bounds(k, v)
        if why is None:
            clean[k] = v
            continue
        dropped.append((k, v, why))
        _reject_counts[k] = _reject_counts.get(k, 0) + 1
        _reject_last[k] = why
    for k, v, why in dropped:
        log.info(f"rejected {k}={v}: {why}")
        try:
            db.log_event("sensor", f"rejected {k}={v}: {why}")
        except Exception:
            pass
    return clean


# sensor -> last-seen unix ts; touched only by the sample thread via run_alerts
_seen_sensors = {}
SEEN_TTL = 3 * 86400    # a sensor silent this long is treated as removed


# binary sensors are legitimately constant for days; a flatline there means
# nothing and accusing them would train you to ignore the rule
NON_STUCK_PREFIXES = ("float:", "reservoir:", "canopy:")


def stuck_sensors(cadence_s):
    """Sensors reporting on time but reporting the same number forever."""
    out = {}
    for key in sorted(_seen_sensors):
        if key.startswith(NON_STUCK_PREFIXES):
            continue
        try:
            vals = db.recent_values(key, n=24, max_age=48 * 3600)
        except Exception:
            continue
        why = quality.check_stuck(vals, cadence_s)
        if why:
            out[key] = why
    return out


def sensor_health(cfg, snap=None, max_age=60):
    """Per-sensor health for the dashboard, cached: recomputing this on every
    15-second status poll would mean a dozen queries per client per poll."""
    now = time.time()
    if now - _health_cache["ts"] < max_age and _health_cache["data"]:
        return _health_cache["data"]
    snap = snap if snap is not None else db.latest()
    cadence = max(1, int(cfg.get("sample_interval_min", 5))) * 60
    cap = max(1, int(cfg.get("capture_interval_min", 10))) * 60
    out = {}
    for key, (ts, _v) in snap.items():
        if key.startswith(("dry:", "growth", "moisture:")):
            continue
        try:
            vals = db.recent_values(key, n=24, max_age=48 * 3600)
        except Exception:
            vals = []
        verdict = (water._probe_verdict.get(key[6:], "steady")
                   if key.startswith("probe:") else "steady")
        if key.startswith(NON_STUCK_PREFIXES):
            vals = vals[:1]      # binary sensors are meant to sit still
        score, grade, why = quality.health(
            vals, age_s=now - ts,
            cadence_s=cap if key.startswith("canopy:") else cadence,
            rejects=_reject_counts.get(key, 0), verdict=verdict,
            noise_ref=0.01 if key.startswith("probe:") else None)
        if _reject_last.get(key) and not any("implausible" in w for w in why):
            why.append(_reject_last[key])
        out[key] = {"score": score, "grade": grade, "why": why}
    _health_cache.update(ts=now, data=out)
    return out


def run_alerts(readings):
    """Evaluate the alert rules against this tick's readings and push anything
    that changed state. Never raises: a failure here must not stop sampling."""
    try:
        with config.settings_lock:
            cfg = dict(config.settings)
        if not cfg.get("alerts_enabled", True):
            return
        acfg = {
            "sustain_seconds": cfg.get("alert_sustain_min", 10) * 60,
            "cooldown_seconds": cfg.get("alert_cooldown_hours", 6) * 3600,
            "soil_temp_high_f": cfg.get("soil_temp_high_f", 0),
            "soil_temp_low_f": cfg.get("soil_temp_low_f", 0),
            "probe_dry_pct": cfg.get("alert_dry_pct", 15),
            "humidity_high": cfg.get("alert_humidity_high", 80),
            "dli_low": cfg.get("alert_dli_low", 4),
            "dli_high": cfg.get("alert_dli_high", 0),
            "dli_target": setups_mod.dli_target(cfg),
        }
        acfg["setups"] = [{"id": st.get("id"), "name": st.get("name"),
                           "band": setups_mod.setup_band(st)} for st in setups_mod.setups(cfg)]
        # The rules judge filtered values: a lone bad reading should not fire a
        # soil-temperature or humidity alert, and the sustain window cannot help
        # when the excursion lasts longer than one sample. Charts and the DLI
        # code keep using the raw series.
        snap = dict(readings)
        for key in list(snap):
            if sensor_jump(key) is None or key.startswith("probe:"):
                continue          # probes are handled by their own filter below
            fv, _ = reading_filtered(key)
            if fv is not None:
                snap[key] = fv

        # the day's light total, judged once the day is over: only within a
        # window after lights-off is _dli present, so the rule fires at most
        # once per evening and a mid-morning low total can't false-alarm
        with config.state_lock:
            off_t = config.state.get("off")
        if off_t is not None and (cfg.get("alert_dli_low")
                                  or cfg.get("alert_dli_high")):
            now_dt = datetime.now(off_t.tzinfo)
            if off_t <= now_dt <= off_t + timedelta(minutes=45):
                multi = setups_mod.setups(cfg)
                vals = {st.get("id"): setups_mod.dli_today(st["lux"], setups_mod.setup_k(st))
                        for st in multi if st.get("lux")}
                if len(multi) == 1:
                    d = next(iter(vals.values()), None)
                    if d is not None:
                        snap["_dli"] = d
                else:
                    snap["_dli_setups"] = {i: v for i, v in vals.items()
                                           if v is not None}

        # tray moisture as percentages, using each tray's calibration
        pcal = cfg.get("probe_cal") or {}
        names = cfg.get("probe_names") or {}
        stf = water.latest_soil_temp_f()
        moist = {}
        for k, v in readings.items():
            if not k.startswith("probe:"):
                continue
            t = k[6:]
            fv, _ = water.probe_volts_filtered(t)      # transient-rejected, not raw
            pct, approx = water.probe_moisture_any(fv if fv is not None else v,
                                             pcal.get(t) or {}, stf)
            if pct is not None and not approx:      # only alert on real calibration
                moist[names.get(t) or f"Soil moisture {t}"] = pct
        snap["_moisture"] = moist

        with config.state_lock:
            snap["_camera_fails"] = camera_mod.camera["fails"] if cfg.get("camera_enabled") else 0

        snap["_fill_failed"] = water.fill_failure["msg"]
        snap["_reservoir"] = water.reservoir_state()
        # a plug that stops responding means the light is stuck wherever it
        # was; the sustain window absorbs a wifi blip, repeated failures do not
        # the plug is worth alerting on whether it is the main light or the
        # third fixture: either way a dead plug means a light stuck on or off
        snap["_plug_failed"] = (light_mod.kasa_state["error"]
                                if (light_mod.light_backend(cfg) == "kasa"
                                    and light_mod.kasa_state["fails"] >= 2) else "")

        # sensors that have gone quiet: keys seen recently but missing from
        # this read. Tracked in memory only (persisting it once made a removed
        # sensor alert forever), and aged out after SEEN_TTL so unwiring a
        # device stops the reminders after a few days instead of never.
        now_ts = time.time()
        for k in readings:
            _seen_sensors[k] = now_ts
        for k in [k for k, t in _seen_sensors.items()
                  if now_ts - t > SEEN_TTL]:
            _seen_sensors.pop(k, None)
        snap["_stale"] = sorted(set(_seen_sensors) - set(readings))
        # reporting on time but not measuring: invisible to the stale check
        snap["_stuck"] = stuck_sensors(
            max(1, int(cfg.get("sample_interval_min", 5))) * 60)

        for action, key, title, message, level in alerts.check_all(
                snap, acfg, "C" if cfg.get("units") == "metric" else "F"):
            prefix = {"fire": "", "remind": "Still: ", "clear": "Resolved: "}[action]
            discord_alert.send(prefix + title, message,
                               level="good" if action == "clear" else level)
            log.info(f"alert {action}: {key}")
    except Exception as e:
        log.error(f"alert check error: {e}")


# ---- live sensor refresh ----------------------------------------------------
# The sample interval is how often a reading is WRITTEN to the database, and
# it is deliberately slow: every sample is a row, and this runs on an SD card.
# How often the dashboard is refreshed need not be tied to that. This loop
# reads the quick sensors every few seconds and pushes them to open pages
# without storing anything, so the page is live while the record stays sparse.
live_readings = {"ts": 0.0, "values": {}}
LIVE_KEYS = ("lux", "lux:2", "temp:air", "humidity", "pressure")


def live_loop():
    last = {}
    while True:
        with config.settings_lock:
            every = int(config.settings.get("live_interval_s") or 0)
        if every <= 0:
            time.sleep(5)
            continue
        try:
            vals = {k: v for k, v in sensors.read_live().items()
                    if k in LIVE_KEYS}
            if vals:
                live_readings.update(ts=time.time(), values=vals)
                # push only on a change worth seeing, so a still room does not
                # wake every open page every few seconds
                moved = any(k not in last or abs(v - last[k]) >
                            max(0.05, abs(last[k]) * 0.002)
                            for k, v in vals.items())
                if moved:
                    last = dict(vals)
                    status_mod.publish("live")
        except Exception as e:
            log.exception(f"live sensor read failed: {e}")
        time.sleep(max(2, every))


def pressure_tendency():
    """Barometric trend, the way a barometer's 3-hour tendency works. Returns
    the change over 3h and 24h in hPa plus a plain-language reading. Falling
    pressure precedes unsettled weather; rising precedes clearing. Thresholds
    follow the conventional 3-hour bands used in surface observation."""
    pts = db.series("pressure", hours=26)
    if len(pts) < 4:
        return None
    now_ts, now_v = pts[-1]
    # the current end of the trend is filtered: one bad sample at the newest
    # point swings a 3-hour tendency across a band and mislabels the weather
    fv, _ = reading_filtered("pressure")
    if fv is not None:
        now_v = fv

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


def _soil_f_lookup(snap, hours):
    """ts -> soil temperature in F nearest that time (within an hour), from
    the logged history. A probe point from last Tuesday is corrected for
    last Tuesday's soil, not today's."""
    import bisect
    key = next((k for k in snap if k.startswith("temp:soil")), None)
    hist = db.series(key, hours + 1) if key else []
    times = [t for t, _ in hist]

    def at(ts, default):
        if not times:
            return default
        i = bisect.bisect_left(times, ts)
        best = min((j for j in (i - 1, i) if 0 <= j < len(times)),
                   key=lambda j: abs(times[j] - ts))
        if abs(times[best] - ts) > 3600:
            return default
        return hist[best][1] * 9 / 5 + 32
    return at

# Imported last: these modules import this one, and their import-time
# code runs only after everything above is defined. Their names are
# used inside functions, at call time, always as module.name.
import camera as camera_mod
import status as status_mod
