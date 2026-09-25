"""The status payload, display units, redaction, and the live event stream."""

import json
import queue
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from applog import log
import db
import sensors
import quality

import config
import hardware
import light as light_mod
import setups as setups_mod
import water
import monitor
import camera as camera_mod

def _units():
    with config.settings_lock:
        return config.settings.get("units", "imperial")


def temp_out(c):
    """Celsius storage -> the configured display unit."""
    return round(c if _units() == "metric" else c * 9 / 5 + 32, 1)


def temp_unit():
    return "C" if _units() == "metric" else "F"


def press_out(hpa):
    return round(hpa if _units() == "metric" else hpa * 0.0295299830714,
                 0 if _units() == "metric" else 2)


def press_unit():
    return "hPa" if _units() == "metric" else "inHg"


# never sent to the browser: /api/status is readable without login, and the
# frontend has no use for any of these
SECRET_SETTINGS = ("password_hash", "discord_webhook", "ntfy_topic",
                   "kasa_user", "kasa_pass")
# Only for a signed-in viewer: where the grow is. The public dashboard does not
# need coordinates to render, and a precise latitude/longitude is a home address.
PRIVATE_SETTINGS = ("latitude", "longitude")


def public_settings(cfg, authed=True):
    hide = SECRET_SETTINGS if authed else SECRET_SETTINGS + PRIVATE_SETTINGS
    return {k: v for k, v in cfg.items() if k not in hide}


# ---- live updates (server-sent events) ----------------------------------
# Waitress can hold a connection open per worker thread, which the development
# server could not; that is what makes this possible at all. Each subscriber
# costs one of the server's threads for as long as it is connected, so the
# count is capped well below the pool: a browser left open on a dozen tabs must
# not be able to starve the controller of threads to answer with.
STREAM_MAX = 6              # of waitress's 16 threads
STREAM_QUEUE = 8            # events buffered per subscriber before it is culled
STREAM_HEARTBEAT = 10.0     # seconds. Also how quickly a closed tab frees its
                            # slot: the write that fails is what tells us the
                            # peer is gone, so this doubles as the reclaim time

_subs = set()
_subs_lock = threading.Lock()

# One rendered status per change, shared by every open tab. Without this each
# subscriber built its own copy on every push: with six tabs open, six times
# the SQLite work on a Pi Zero for the same answer. Two variants at most,
# because a signed-out viewer gets a redacted copy.
_pub_seq = [0]                     # bumped by every publish()
_status_cache = {}                 # authed -> (seq it was built at, SSE text)
_status_build_lock = threading.Lock()


def _status_event(authed):
    """The SSE message for the current status, built at most once per change."""
    authed = bool(authed)
    with _status_build_lock:
        seq = _pub_seq[0]
        hit = _status_cache.get(authed)
        if hit and hit[0] == seq:
            return hit[1]
        text = f"event: status\ndata: {json.dumps(status_payload(authed))}\n\n"
        _status_cache[authed] = (seq, text)
        return text


def _end_streams():
    """Shutdown: release every stream at once. Each one holds a server thread,
    and the server gives threads 5 s to finish before it exits anyway."""
    with _subs_lock:
        subs = list(_subs)
        _subs.clear()
    for q in subs:
        try:
            q.put_nowait("shutdown")
        except queue.Full:
            pass                   # it is awake already and sees the flag


def publish(reason="update"):
    """Hand the current status to every listening browser.

    Called from the loops after something actually changes, which is the whole
    point: a poll asks every 15 seconds whether anything happened, this says so
    the moment it does. Never raises and never blocks a control loop: a
    subscriber whose queue has backed up is dropped rather than waited for.
    """
    with _subs_lock:
        _pub_seq[0] += 1
        subs = list(_subs)
    if not subs:
        return
    for q in subs:
        try:
            q.put_nowait(reason)
        except queue.Full:
            with _subs_lock:
                _subs.discard(q)      # not keeping up; it will reconnect


def _switch_changed(key):
    """A float or reservoir switch flipped: push the status right away."""
    log.info(f"{key} changed")
    publish(key)


sensors.on_change(_switch_changed)
try:
    sensors.arm_watchers()
except Exception as e:           # a missing switch must never stop the app
    log.info(f"switch watchers not armed: {e}")


def status_payload(authed=None):
    """The dashboard's whole view of the world, as a dict.

    Shared by the polled endpoint and the live stream so both render from
    byte-identical data; building it twice in two places is how they drift.

    `authed` is passed in by the stream: its generator runs after the request
    context has been torn down, so the session is not readable from there. The
    flag is captured once when the stream opens, and a login or logout
    reconnects the stream anyway."""
    with config.state_lock:
        s = dict(config.state)
        cam = dict(camera_mod.camera)
    with config.settings_lock:
        cfg = dict(config.settings)
    if s["on"] is None:
        return {"error": "warming up"}
    tz = ZoneInfo(cfg["timezone"])
    count, _, latest_time = camera_mod.photo_inventory()
    snap = db.latest()                        # one query serves the whole response
    # Overlay the live read so the chips show the room as it is now, not as it
    # was at the last logged sample. db.latest() holds (ts, value) tuples, so
    # the overlay must use that shape. Charts and alerts still read the record.
    lv = monitor.live_readings
    if lv["values"] and time.time() - lv["ts"] < 120:
        for k, v in lv["values"].items():
            prev = snap.get(k)
            if prev is None or lv["ts"] >= prev[0]:
                snap[k] = (lv["ts"], v)
    stf = water.latest_soil_temp_f(snap)
    pcal = cfg.get("probe_cal") or {}
    cam_on = bool(cfg.get("camera_enabled"))
    _cam_trays = set(setups_mod.camera_trays(cfg))
    sensors_out = {k: {"ts": ts,
                       "value": (water.compensated_volts(v, pcal.get(k[6:]) or {}, stf)
                                 if k.startswith("probe:") else v)}
                   for k, (ts, v) in snap.items()
                   # legacy per-cell camera series (dry:/growth:/moisture:) are
                   # no longer written or shown; canopy hides with the camera
                   if not (k.startswith("dry:") or k.startswith("growth")
                           or k.startswith("moisture:"))
                   and (cam_on or not k.startswith("canopy:"))
                   and (not k.startswith("canopy:") or k[7:] in _cam_trays)}
    setups_out = [setups_mod.setup_status(cfg, st, s["on"], s["off"], tz) for st in setups_mod.setups(cfg)]
    day = setups_out[0]["day"]
    return dict(
        now=datetime.now(tz).isoformat(),
        brightness=s["brightness"],
        light_override=s.get("override", cfg.get("light_override", "auto")),
        manual_bright=cfg.get("manual_bright", 100),
        on=s["on"].isoformat(), off=s["off"].isoformat(),
        sunrise=s["sunrise"].isoformat(), sunset=s["sunset"].isoformat(),
        ramp=cfg["ramp_min"], max=cfg["max_bright"],
        schedule_mode=cfg.get("schedule_mode", "solar"),
        fan={"hw": hardware.FAN_HW, "on": hardware.fan_state["on"], "reason": hardware.fan_state["reason"],
             "mode": cfg.get("fan_mode", "auto"),
             "speed": hardware.fan_state.get("speed", 0),
             "manual_speed": cfg.get("fan_speed", 100),
             "auto_speed": cfg.get("fan_auto_speed", 70)},
        gpio=hardware.GPIO_PIN, freq=hardware.PWM_FREQ, loop=light_mod.LOOP_SECONDS,
        photo_count=count,
        latest_photo_time=latest_time.isoformat() if latest_time else None,
        camera={"fails": cam["fails"], "last_err": cam["last_err"],
                "last_ok": (datetime.fromtimestamp(cam["last_ok"], tz).isoformat()
                            if cam["last_ok"] else None)},
        capturing=camera_mod.capturing,
        render=dict(camera_mod.render),
        video_time=(datetime.fromtimestamp(camera_mod.VIDEO_PATH.stat().st_mtime)
                    .isoformat() if camera_mod.VIDEO_PATH.exists() else None),
        light_backend=light_mod.light_backend(cfg),
        dim_pin=hardware.GPIO_PIN2 if hardware.pwm2 is not None else None,
        light2={"enabled": bool(cfg.get("light2_on")),
                "fixture": light_mod.light2_fixture(cfg),
                "level": light_mod.light2_state["level"],
                "why": light_mod.light2_state["why"],
                "start": cfg.get("light2_start"), "end": cfg.get("light2_end"),
                "override": cfg.get("light2_override", "auto"),
                "bright": cfg.get("light2_bright", 50),
                "ramp_min": cfg.get("light2_ramp_min", 0)},
        lightning={"available": light_mod.lightning_available(cfg),
                   "running": light_mod.lightning_state["running"]},
        kasa={"host": light_mod.kasa_conf(cfg)[0],
              "from_env": bool(light_mod.KASA_HOST_ENV),
              "on": light_mod.kasa_state["on"], "ok": light_mod.kasa_state["ok"],
              "error": light_mod.kasa_state["error"], "fails": light_mod.kasa_state["fails"]}
             if light_mod.light_backend(cfg) == "kasa" else None,
        settings=public_settings(cfg, authed=routes.is_authed() if authed is None else bool(authed)),
        probe_default_cal=water.PROBE_DEFAULT_CAL,
        probe_cal_flags={t: f for t in
                         [k[6:] for k in snap if k.startswith("probe:")]
                         if (f := water.probe_cal_flag(
                             (water.probe_volts_filtered(t, snap)[0] or 0),
                             (pcal.get(t) or {})))},
        # filtered volts per tray, so the readout matches what decisions use
        probe_filtered={t: water.probe_volts_filtered(t, snap)[0]
                        for t in [k[6:] for k in snap if k.startswith("probe:")]},
        # and the same for every other smoothed sensor; charts stay raw
        filtered={k: monitor.reading_filtered(k, snap)[0] for k in snap
                  if monitor.sensor_jump(k) is not None and not k.startswith("probe:")},
        quality={
            "health": monitor.sensor_health(cfg, snap),
            # advisory only: cross-sensor checks can accuse the wrong sensor,
            # so they are shown but never wired to alerts or watering
            "contradictions": [
                {"subject": subj, "message": msg} for subj, msg in
                quality.contradictions(
                    {k: v for k, (ts, v) in snap.items()},
                    {**cfg, "_light_on": bool(s["brightness"] > 1)},
                    pumped_recently=any(
                        time.time() - (st.get("last_run") or 0) < 3600
                        for st in hardware.pump_state.values()))],
            "postfill": water.postfill_result,
            "rejects": {k: {"count": n, "last": monitor._reject_last.get(k, "")}
                        for k, n in monitor._reject_counts.items()},
            "probe_verdict": dict(water._probe_verdict),
        },
        focus={"running": camera_mod.focus_state["running"], "step": camera_mod.focus_state["step"],
               "best": camera_mod.focus_state["best"], "error": camera_mod.focus_state["error"]},
        sensors=sensors_out,
        pressure_tendency=monitor.pressure_tendency(),
        sweep={"running": light_mod.sweep_state["running"], "pct": light_mod.sweep_state["pct"],
               "error": light_mod.sweep_state["error"], "target": light_mod.sweep_state.get("target", "main")},
        light_curve=cfg.get("light_curve"),
        light_linear={k: v for k, v in (cfg.get("light_linear") or {}).items()
                      if k != "table"} or None,
        light_linear_on=bool(cfg.get("light_linear_on")),
        light_linear_stale=bool(cfg.get("light_linear_on")
                                and (cfg.get("light_linear") or {}).get("table")
                                and light_mod.linear_table(cfg) is None),
        light_curve_effective=light_mod.effective_curve(cfg),
        light2_cal=(lambda c2: {
            "light_curve": c2.get("light_curve"),
            "light_linear": {k: v for k, v in (c2.get("light_linear") or {}).items()
                             if k != "table"} or None,
            "light_linear_on": bool(c2.get("light_linear_on")),
            "light_linear_stale": bool(c2.get("light_linear_on")
                                       and (c2.get("light_linear") or {}).get("table")
                                       and light_mod.linear_table(c2) is None),
            "light_curve_effective": light_mod.effective_curve(c2)})(light_mod.light2_cfg(cfg)),
        day_light=day,
        light_plan=setups_out[0]["plan"],
        setups=setups_out,
        light_options=setups_mod.light_options(cfg),
        light_metrics=(lambda lx: {
            "k": setups_mod.lux_k(),
            "canopy": setups_mod.canopy_factor(),
            "ppfd": setups_mod.ppfd_from_lux(lx),
            "dli": setups_mod.dli_today(),
        } if lx is not None and setups_mod.lux_k() else None)(
            (snap.get("lux") or (None, None))[1]),
        authed=routes.is_authed() if authed is None else bool(authed),
        auth_enabled=routes.auth_enabled(),
        water={
            "pump_hw": hardware.PUMP_HW,
            "auto_water": bool(water.armed_trays(cfg)),
            "armed": water.armed_trays(cfg),
            "reservoir": {"state": water.reservoir_state(),
                          "wired": bool(sensors.RESERVOIR_PINS)},
            "auto_blockers": water.auto_water_blockers(cfg),
            "moisture_threshold_pct": cfg.get("moisture_threshold_pct", 30),
            "pump_cooldown_min": cfg.get("pump_cooldown_min", 30),
            "trays": {t: {
                "float": sensors.read_float(t),
                "pump_hw": t in hardware._pumps,
                "running": hardware.pump_state[t]["running"],
                "last": hardware.pump_state[t]["last_detail"],
                "last_run": int(hardware.pump_state[t]["last_run"]) or None,
                "today_seconds": round(hardware.pump_state[t]["today_seconds"], 1),
            } for t in hardware.PUMP_PINS},
        },
    )

# Imported last: these modules import this one, and their import-time
# code runs only after everything above is defined. Their names are
# used inside functions, at call time, always as module.name.
import routes
