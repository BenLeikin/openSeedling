"""The control loop: on every pass, set the lights from the schedule, run the
fan, and publish what changed."""

from datetime import datetime
from zoneinfo import ZoneInfo

from applog import log
import events
import readings as readings_mod

import config
import hardware
import light as light_mod
import setups as setups_mod

LOOP_SECONDS = 30


def control_loop(stop=None):
    """Run until `stop` (an Event) is set; forever when there is none."""
    logged = {}  # the last window written to the log, and each step's last error
    while stop is None or not stop.is_set():
        with config.settings_lock:
            cfg = dict(config.settings)
        window = _step("lights", logged, _lights, cfg, logged)
        if window:
            _step("fan", logged, _fan, cfg, *window)
        config.wake.wait(timeout=LOOP_SECONDS)
        config.wake.clear()


def _step(name, logged, fn, *args):
    """Run one part of a pass; returns its result, or None if it failed.

    A failing step must not end the loop, or the fixture stays wherever it was
    last set while the dashboard looks fine. Nor may it stop the other steps.
    Its traceback is logged when the error first appears and when it changes,
    not on every pass.
    """
    try:
        out = fn(*args)
    except Exception as e:
        if logged.get(name) != str(e):
            logged[name] = str(e)
            log.exception(f"control loop: {name} failed: {e}")
        return None
    logged.pop(name, None)
    return out


def _lights(cfg, logged):
    """Set both lights for now and publish the state. Returns (now, on, off)."""
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    sunrise, sunset, on_time, off_time = light_mod.window_at(cfg, now)
    if logged.get("window") != (on_time, off_time):
        logged["window"] = (on_time, off_time)
        log.info(
            f"light window: on {on_time:%a %H:%M}, off {off_time:%a %H:%M} "
            f"({cfg['latitude']}, {cfg['longitude']}, {cfg['timezone']})"
        )
    # the second light first, so the write below carries its new level
    if light_mod.light2_fixture(cfg):
        level, why = light_mod.light2_level(cfg, now)
        light_mod.light2_state.update(level=round(level, 2), why=why)
    else:
        why = "off" if not cfg.get("light2_on") else "needs both PWM channels"
        light_mod.light2_state.update(level=0.0, why=why)
    b = light_mod.brightness_for(cfg, now, on_time, off_time)
    override = cfg.get("light_override", "auto")
    if override == "on":
        b = max(0, min(100, int(cfg.get("manual_bright", cfg["max_bright"]))))
    elif override == "off":
        b = 0
    if light_mod.holder() is None:
        light_mod.set_brightness(b)

    with config.state_lock:
        l2_now = light_mod.light2_state["level"]
        was = config.state.get("brightness")
        changed = (
            round(config.state.get("light2_level", -1), 1) != round(l2_now, 1)
            or was is None
            or round(was, 1) != round(b, 1)
            or config.state.get("override") != override
            or config.state.get("on") != on_time
        )
        config.state.update(
            brightness=b,
            on=on_time,
            off=off_time,
            sunrise=sunrise,
            sunset=sunset,
            override=override,
            light2_level=l2_now,
        )
    # only when something moved: during a ramp every pass changes brightness,
    # but a steady day should not push the same status to every open page
    if changed:
        events.publish("light")
    return now, on_time, off_time


def _fan(cfg, now, on_time, off_time):
    mode = cfg.get("fan_mode", "auto")
    if mode == "on":
        hardware.set_fan(cfg.get("fan_speed", 100), "manual")
    elif mode == "off":
        hardware.set_fan(0, "manual")
    else:
        fan_setup = setups_mod.setup_with(cfg, "fan") or {"light": "main"}
        fon, foff = setups_mod.setup_window(cfg, fan_setup, on_time, off_time)
        # filtered, so one bad humidity reading does not start the fan
        rh, _ = readings_mod.reading_filtered("humidity")
        want, why = hardware.fan_should_run(cfg, now, fon, foff, rh)
        hardware.set_fan(cfg.get("fan_auto_speed", 70) if want else 0, why)
