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

# Auto is time-proportional control, not on/off. Each WINDOW_S the mat gets a
# duty (0 to 1) and is on for that share of the window, once, so the soil sees
# the average power instead of full-on/full-off swings.
#
# Tuning history (Ben's rig, 26 Sep):
# - Update 21 tuned on a model fitted to 10 h of on/off data (slow soil,
#   soil = air + 6.4C at full power). It predicted 0.9F peak to peak; the rig
#   swung 2.8F at a ~70 min period. That model had the lag wrong.
# - Replaying update 21's own decisions over the 09:14-13:48 readings
#   (the replay matched the live 77% duty) and fitting to that period gives a
#   faster, delayed rig: about 12 min dead time and two ~15 min lags, soil
#   settling near 16.5 + 0.38 x air + 3.69 x duty (C). In a closed-loop
#   simulation that model reproduces the observed swing (2.75F), which is
#   what makes it worth tuning on.
# - Retuned on it: KP 0.25 per C (was 0.6), TI 2 h (was 1 h), and the
#   feed-forward from that fit. Simulated over the same day: about 0.4F peak
#   to peak, mean on target; under 1F with the rig's gain off by 30%, its
#   lags 40-60% off, or its delay 50% off. About 165 plug switches a day.
WINDOW_S = 900              # one on-pulse per 15 min
MIN_PULSE_S = 60            # skip on or off pulses shorter than this (relay wear)
KP_PER_C = 0.25             # duty per degree C below target
TI_S = 7200                 # integral time: a steady 1C error adds 0.25 duty per 2 h
# feed-forward: the duty that holds the target at this air temperature, from
# the fit above (soil ~ FF_BASE_C + FF_AIR x air + FF_GAIN_C x duty); the
# integral trims whatever it gets wrong
FF_BASE_C = 16.5
FF_AIR = 0.38
FF_GAIN_C = 3.69
INTEGRAL_KEY = "heat_integral"   # kv: the integral survives a restart (under an hour old)
REASSERT_S = 600            # re-send the state now and then (the Kasa app can flip it)
STALE_SAMPLES = 3           # a reading older than this many sample intervals is gone

heat_state = {"on": None,          # what we last commanded (None: never)
              "reason": "not in use",
              "temp_c": None,       # the reading the last decision used
              "since": 0.0,         # when the plug last changed
              "sent": 0.0,          # when we last sent a command
              "fault": "",          # why the mat is being held off, for alerts
              "duty": None,         # Auto's share of the current window (0 to 1)
              "integral": 0.0,      # the PI controller's integral term
              "window": None,       # start of the current window (epoch s)
              "on_until": 0.0}      # the mat is on until this time in the window
_lock = threading.Lock()
_pass_lock = threading.Lock()     # the loop and a button press never decide at once
_prev_use = None


def in_use(cfg):
    return cfg.get("plug_use", "light") == "heat"


def f_to_c(f):
    return (float(f) - 32.0) * 5.0 / 9.0


def duty_for(cfg, temp_c, air_c, st, window_s=WINDOW_S):
    """PI with an air-temperature feed-forward: the share of the next window
    the mat is on. Updates st["integral"] (not while pinned at 0 or 1 in the
    direction of the error, so it cannot wind up overnight when the mat is
    flat out and still short)."""
    target_c = f_to_c(cfg.get("heat_target_f", 75))
    err = target_c - temp_c
    ff = ((target_c - FF_BASE_C - FF_AIR * air_c) / FF_GAIN_C
          if air_c is not None else 0.7)
    integ = st.get("integral", 0.0)
    raw = ff + KP_PER_C * err + integ
    if 0.0 < raw < 1.0 or (raw >= 1.0 and err < 0) or (raw <= 0.0 and err > 0):
        integ += KP_PER_C * err * window_s / TI_S
    integ = max(-1.0, min(1.0, integ))
    st["integral"] = integ
    return max(0.0, min(1.0, ff + KP_PER_C * err + integ))


def decide(cfg, temp_c, age_s, on_now, now, air_c=None, st=None):
    """The thermostat: (want_on, reason, fault, urgent).

    urgent means switch now: a safety cut-off, or the grower's own On or Off.
    Auto switches only at the planned points of its window. st carries Auto's
    state between passes (integral, window, duty); heat_state when omitted.
    """
    st = heat_state if st is None else st
    mode = cfg.get("heat_mode", "off")
    if mode == "off":
        st.update(duty=None, window=None)
        return False, "off", "", True
    stale_s = STALE_SAMPLES * max(1, int(cfg.get("sample_interval_min", 5))) * 60
    have = temp_c is not None and age_s is not None and age_s <= stale_s
    max_c = f_to_c(cfg.get("heat_max_f", 95))
    if have and temp_c >= max_c:
        st.update(duty=None, window=None)
        return (False, f"soil at the {cfg.get('heat_max_f', 95)}F cut-off",
                "soil temperature reached the heat mat cut-off", True)
    if mode == "on":
        st.update(duty=None, window=None)
        return True, ("held on" if have else "held on, no soil reading: cut-off inactive"), "", True
    # auto
    if not have:
        st.update(duty=None, window=None)
        return (False, "no recent soil temperature",
                "no recent soil temperature, so the heat mat is held off", True)
    win = st.get("window")
    if win is None or now >= win + WINDOW_S or now < win:
        duty = duty_for(cfg, temp_c, air_c, st)
        on_s = duty * WINDOW_S
        if on_s < MIN_PULSE_S:
            on_s = 0.0
        elif WINDOW_S - on_s < MIN_PULSE_S:
            on_s = float(WINDOW_S)
        st.update(window=now, duty=on_s / WINDOW_S, on_until=now + on_s)
        st["new_window"] = True
    want = now < st["on_until"]
    pct = round(st["duty"] * 100)
    return want, f"{pct}% power", "", False


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
            on_now, sent = heat_state["on"], heat_state["sent"]
        _restore_integral(now)
        air_c, air_ts = monitor.reading_filtered("temp:air")
        if air_ts is None or now - air_ts > 3600:
            air_c = None                            # feed-forward falls back to a guess
        with _lock:
            want, reason, fault, urgent = decide(cfg, temp_c, age, on_now, now, air_c)
            fresh = heat_state.pop("new_window", False)
            duty, integ = heat_state.get("duty"), heat_state.get("integral", 0.0)
        if fresh and duty is not None:
            # the power it chose, charted beside the soil temperature, and the
            # integral kept so a restart does not throw away what it learned
            try:
                db.log_reading("heat:duty", round(duty * 100, 1), ts=now)
                db.kv_set(INTEGRAL_KEY, {"integral": integ, "ts": now})
            except Exception as e:
                log.warning(f"heat mat: could not record the power level ({e})")
        if hardware.SHUTTING_DOWN.is_set():
            want, reason = False, "shutting down"
        change = want != on_now
        ok = True
        if change or now - sent >= REASSERT_S or not light_mod.kasa_state.get("ok"):
            ok = _send(want)
            if ok and change:
                # Auto pulses the mat dozens of times a day; those go to the
                # debug log only. Mode changes and safety cut-offs are events.
                line = (f"heat mat {'on' if want else 'off'}: {reason}"
                        + (f", soil {temp_c:.1f}C" if temp_c is not None else ""))
                if urgent:
                    db.log_event("heat", line)
                    log.info(line)
                else:
                    log.debug(line)
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


_restored = False


def _restore_integral(now):
    """Once per process: pick up the integral a previous run left, if recent."""
    global _restored
    if _restored:
        return
    _restored = True
    try:
        saved = db.kv_get(INTEGRAL_KEY) or {}
        if saved and now - float(saved.get("ts", 0)) < 3600:
            with _lock:
                heat_state["integral"] = max(-1.0, min(1.0, float(saved.get("integral", 0.0))))
    except Exception as e:
        log.warning(f"heat mat: could not restore the controller state ({e})")


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
            "duty": st["duty"], "window_s": WINDOW_S,
            "max_f": cfg.get("heat_max_f", 95), "sensor": cfg.get("heat_sensor", "temp:soil"),
            "plug_ok": light_mod.kasa_state.get("ok"), "plug_error": light_mod.kasa_state.get("error", "")}
