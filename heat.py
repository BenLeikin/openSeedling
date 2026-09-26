"""Heat mat: a thermostat on the smart plug, driven by a soil temperature probe.

The plug was the light's on/off backend. Settings, Smart plug, "Use the plug
for" now gives it to one job: the light, or the heat mat. With the heat mat
the light code never touches it, and this module is its only writer.

Temperatures are settings in Fahrenheit, like the soil temperature band, and
readings are Celsius, like every stored reading.
"""

import threading
import time

from applog import log
import db

import config
import hardware
import light as light_mod
import monitor

LOOP_S = 30                 # how often the thermostat decides
HYSTERESIS_F = 1.0          # on below target - this, off at the target
MIN_SWITCH_S = 120          # plug relay wear: no faster than this, except for safety
REASSERT_S = 600            # re-send the state now and then (the Kasa app can flip it)
STALE_SAMPLES = 3           # a reading older than this many sample intervals is gone

heat_state = {"on": None,          # what we last commanded (None: never)
              "reason": "not in use",
              "temp_c": None,       # the reading the last decision used
              "since": 0.0,         # when the plug last changed
              "sent": 0.0,          # when we last sent a command
              "fault": ""}          # why the mat is being held off, for alerts
_lock = threading.Lock()
_pass_lock = threading.Lock()     # the loop and a button press never decide at once
_prev_use = None


def in_use(cfg):
    return cfg.get("plug_use", "light") == "heat"


def f_to_c(f):
    return (float(f) - 32.0) * 5.0 / 9.0


def decide(cfg, temp_c, age_s, on_now, now):
    """The thermostat as a pure function: (want_on, reason, fault, urgent).

    urgent means switch now even inside MIN_SWITCH_S: a safety cut-off, or the
    grower's own On or Off. Only the thermostat's own cycling waits.
    """
    mode = cfg.get("heat_mode", "off")
    if mode == "off":
        return False, "off", "", True
    stale_s = STALE_SAMPLES * max(1, int(cfg.get("sample_interval_min", 5))) * 60
    have = temp_c is not None and age_s is not None and age_s <= stale_s
    max_c = f_to_c(cfg.get("heat_max_f", 95))
    if have and temp_c >= max_c:
        return (False, f"soil at the {cfg.get('heat_max_f', 95)}F cut-off",
                "soil temperature reached the heat mat cut-off", True)
    if mode == "on":
        return True, ("held on" if have else "held on, no soil reading: cut-off inactive"), "", True
    # auto
    if not have:
        return (False, "no recent soil temperature",
                "no recent soil temperature, so the heat mat is held off", True)
    target_c = f_to_c(cfg.get("heat_target_f", 75))
    band_c = HYSTERESIS_F * 5.0 / 9.0
    if temp_c < target_c - band_c:
        return True, "below target", "", False
    if temp_c >= target_c:
        return False, "at target", "", False
    return bool(on_now), "holding", "", False


def _send(want):
    ok = light_mod.kasa_apply(want)
    with _lock:
        heat_state["sent"] = time.time()
    return ok


def heat_pass(now=None):
    """One thermostat decision. Never raises."""
    with _pass_lock:
        _heat_pass(now)


def _heat_pass(now=None):
    global _prev_use
    now = now or time.time()
    try:
        with config.settings_lock:
            cfg = dict(config.settings)
        use = in_use(cfg)
        if _prev_use and not use:
            # handed back to the light (or to nothing): leave it off, and let
            # the light code take it from here
            _send(False)
            with _lock:
                heat_state.update(on=None, reason="not in use", fault="", temp_c=None)
            log.info("heat mat: plug released")
        _prev_use = use
        if not use:
            return
        key = cfg.get("heat_sensor") or "temp:soil"
        temp_c, ts = monitor.reading_filtered(key)
        age = None if ts is None else now - ts
        with _lock:
            on_now, since, sent = heat_state["on"], heat_state["since"], heat_state["sent"]
        want, reason, fault, urgent = decide(cfg, temp_c, age, on_now, now)
        if hardware.SHUTTING_DOWN.is_set():
            want, reason = False, "shutting down"
        change = want != on_now
        if change and not urgent and on_now is not None and now - since < MIN_SWITCH_S:
            want, change = on_now, False            # too soon; the next pass decides
            reason += " (waiting to switch)"
        ok = True
        if change or now - sent >= REASSERT_S or not light_mod.kasa_state.get("ok"):
            ok = _send(want)
            if ok and change:
                db.log_event("heat", f"heat mat {'on' if want else 'off'}: {reason}"
                             + (f", soil {temp_c:.1f}C" if temp_c is not None else ""))
                log.info(f"heat mat {'on' if want else 'off'} ({reason})")
        with _lock:
            if ok:
                if change:
                    heat_state["since"] = now
                heat_state["on"] = want
            else:
                # the plug did not take it: report what it last did, retry next pass
                reason += " (plug not responding)"
            heat_state.update(reason=reason, fault=fault, temp_c=temp_c)
    except Exception:
        log.exception("heat mat pass failed")


def heat_loop():
    while True:
        heat_pass()
        time.sleep(LOOP_S)


def off_now():
    """Shutdown: the mat off, if the plug is the heat mat's. Quick: one try."""
    with config.settings_lock:
        use = in_use(config.settings)
    if not use:
        return
    try:
        light_mod.kasa_apply(False, retries=0)
    except Exception as e:
        log.error(f"could not turn the heat mat off: {e}")


def status(cfg):
    with _lock:
        st = dict(heat_state)
    return {"use": in_use(cfg), "mode": cfg.get("heat_mode", "off"),
            "on": st["on"], "reason": st["reason"], "fault": st["fault"],
            "temp_c": st["temp_c"], "target_f": cfg.get("heat_target_f", 75),
            "max_f": cfg.get("heat_max_f", 95), "sensor": cfg.get("heat_sensor", "temp:soil"),
            "plug_ok": light_mod.kasa_state.get("ok"), "plug_error": light_mod.kasa_state.get("error", "")}
