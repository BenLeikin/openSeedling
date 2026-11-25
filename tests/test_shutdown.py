"""Tests: shutdown."""

import threading
import time

from harness import (
    PWM_WRITES,
    config,
    hardware,
    light_mod,
    routes,
    sensors,
    water,
)


def test_shutdown():
    """Last: sets the shutdown flag for good, the way SIGTERM does."""
    with config.settings_lock:
        config.settings.update(fill_max_seconds=5, auto_water=True)
    water.fill_failures.clear()
    for st in hardware.pump_state.values():
        st.update(running=False, today_seconds=0, day=config._today_str())
    fl = sensors._floats()
    if "1" not in fl:  # the watering checks removed it
        sensors._float_init = False
        sensors._float_devs.clear()
        fl = sensors._floats()
    fl["1"].is_pressed = True  # not full: the fill keeps running
    with routes.app.test_request_context("/api/stream"):
        sg = iter(routes.api_stream().response)
    next(sg)
    next(sg)
    ended = {}

    def drain():
        for _ in sg:
            pass
        ended["at"] = time.time()

    threading.Thread(target=drain, daemon=True).start()
    out = {}
    t = threading.Thread(target=lambda: out.update(r=water.run_pump_until_full("1", "auto")))
    t.start()
    time.sleep(0.4)
    ran = []

    def broken_step():
        ran.append("broken")
        raise RuntimeError("a shutdown step that fails")

    hardware.at_shutdown(broken_step)
    hardware.at_shutdown(lambda: ran.append("end"), "end")
    PWM_WRITES.clear()
    t0 = time.time()
    try:
        hardware.cleanup()
        exited = False
    except SystemExit as e:
        exited = e.code in (0, None)
    t.join(3)
    assert exited, "cleanup finishes with a clean exit"
    assert ran == ["broken", "end"] and PWM_WRITES and not hardware._pumps["1"].value, (
        f"a shutdown step that fails does not keep the rest from running ({ran})"
    )
    time.sleep(0.2)
    assert "at" in ended and ended["at"] - t0 < 1.5, (
        "an open live stream ends at shutdown instead of holding the server for 5 s"
    )
    ok, why = out.get("r", (True, ""))
    assert not ok and "shutting down" in why and time.time() - t0 < 2.5, (
        f"a running fill stops when shutdown starts ({why})"
    )
    assert not hardware._pumps["1"].value, "pump is off after shutdown"
    assert config.settings.get("auto_water") and not water.fill_failures, (
        "a fill cut short by shutdown is not a failure: auto-water stays armed"
    )
    ok, why = water.run_pump("1", 3, "manual")
    assert not ok and "shutting down" in why and not hardware._pumps["1"].value, (
        "no pump can start after shutdown begins"
    )
    n = len(PWM_WRITES)
    light_mod.set_brightness_raw(80)
    assert len(PWM_WRITES) == n, "no light write can relight the fixture after shutdown begins"
    if hardware._fan is not None:
        hardware._fan.value = 0
        hardware.set_fan(60, "test")
        assert hardware._fan.value == 0, "fan cannot restart after shutdown begins"
