"""Tests: light."""

import re
import sys
import threading
import time
from datetime import date
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from harness import (
    photos_mod,
    APP,
    APP_MODULES,
    PWM_WRITES,
    ai_report,
    c,
    camera_mod,
    config,
    control,
    events,
    light_mod,
    page_js,
    sensors,
    setups_mod,
)


def test_light_schedule():
    cfg = dict(
        config.settings,
        latitude=78.2,
        longitude=15.6,
        schedule_mode="fixed",
        fixed_on="07:00",
        fixed_off="19:00",
    )
    try:
        _, _, on, off = light_mod.sun_window(
            cfg, date(2026, 6, 21), ZoneInfo("Arctic/Longyearbyen")
        )
        polar = (on.hour, off.hour) == (7, 19)
    except Exception:
        polar = False
    assert polar, "fixed schedule works at a polar latitude in midsummer"

    # windows that cross midnight
    tz_ = ZoneInfo("America/Denver")

    def lit(cfg_, hour, minute=0):
        now_ = datetime(2026, 3, 10, hour, minute, tzinfo=tz_)
        _, _, on_, off_ = light_mod.window_at(cfg_, now_)
        return light_mod.brightness_for(cfg_, now_, on_, off_) > 0

    base = dict(config.settings, timezone="America/Denver", ramp_min=10, max_bright=80)
    night = dict(base, schedule_mode="fixed", fixed_on="20:00", fixed_off="06:00")
    assert [lit(night, h) for h in (19, 21, 23)] == [False, True, True] and [
        lit(night, 0, 30),
        lit(night, 5),
        lit(night, 7),
    ] == [True, True, False], (
        "a fixed 20:00 to 06:00 schedule stays on through midnight until 06:00"
    )
    late = dict(base, schedule_mode="duration", duration_end="02:00", duration_hours=14)
    assert [lit(late, h) for h in (11, 13, 23)] == [False, True, True] and [
        lit(late, 1),
        lit(late, 3),
    ] == [True, False], "14 hours ending at 02:00 run from noon, across midnight, to 02:00"
    day = dict(base, schedule_mode="fixed", fixed_on="07:00", fixed_off="19:00")
    at_23 = datetime(2026, 3, 10, 23, tzinfo=tz_)
    _, _, on23, off23 = light_mod.window_at(day, at_23)
    assert [lit(day, h) for h in (6, 12, 20)] == [False, True, False] and (
        on23.date(),
        on23.hour,
        off23.hour,
    ) == (at_23.date(), 7, 19), (
        "a daytime schedule is unchanged, and after lights-off the window shown is still today's"
    )


def test_control_loop_survives_an_error():
    calls = {"n": 0}
    real_sw = light_mod.sun_window

    def flaky(cfg_, day, tz_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected test error (expected in the log)")
        return real_sw(cfg_, day, tz_)

    stop = threading.Event()
    pace, control.LOOP_SECONDS = control.LOOP_SECONDS, 0.1
    light_mod.sun_window = flaky
    t = threading.Thread(target=control.control_loop, args=(stop,), daemon=True)
    try:
        t.start()
        time.sleep(0.8)
        assert t.is_alive() and calls["n"] >= 2, "control loop survives an error and retries"
    finally:
        light_mod.sun_window = real_sw
        stop.set()
        config.wake.set()
        t.join(5)
        control.LOOP_SECONDS = pace
    assert not t.is_alive(), "the control loop ends when it is told to stop"


def test_light_off_pushes_no_status(control_loop):
    """Light off and steady: the loop has nothing new to tell the dashboards."""
    with config.settings_lock:
        was_ov = config.settings.get("light_override")
        config.settings["light_override"] = "off"
    time.sleep(0.5)  # a few passes to settle at zero
    pushes = []
    events.subscribe(pushes.append)
    try:
        time.sleep(0.6)
    finally:
        events.unsubscribe(pushes.append)
        with config.settings_lock:
            config.settings["light_override"] = was_ov
    assert "light" not in pushes, (
        f"with the light off and unchanged, the control loop pushes no status ({len(pushes)})"
    )


def test_dli_band():
    cfg0 = c.get("/api/status").get_json()["settings"]
    assert (cfg0.get("dli_target_low"), cfg0.get("dli_target_high")) == (10.0, 15.0), (
        "default seedling DLI target is 10-15 and reaches the dashboard"
    )
    r = c.post("/api/settings", json={"dli_target_low": 18, "dli_target_high": 12}).get_json()
    assert (
        not r["ok"] and "dli_target_low" in r["errors"] and setups_mod.dli_target() == (10.0, 15.0)
    ), "a DLI target with low above high is refused and nothing changes"
    r = c.post("/api/settings", json={"dli_target_low": 10, "dli_target_high": 14}).get_json()
    assert r["ok"] and setups_mod.dli_target() == (10.0, 14.0), "a valid DLI target saves"
    real_md = setups_mod.measured_day
    setups_mod.measured_day = lambda *a, **kw: {"mol": 12.0, "lit_hours": 12.0, "day": "yesterday"}
    try:
        in_band = setups_mod.light_plan(dict(config.settings), None, None)["status"]
        c.post("/api/settings", json={"dli_target_low": 15, "dli_target_high": 20})
        below = setups_mod.light_plan(dict(config.settings), None, None)
    finally:
        setups_mod.measured_day = real_md
    assert (
        in_band == "ok" and below["status"] == "low" and any("15 mol" in a for a in below["advice"])
    ), "the Plan verdict and advice follow the configured band (12 mol: in 10-14, short of 15-20)"
    import alerts

    alerts.reset()
    out = alerts.check_all({"_dli": 3.0}, {"dli_low": 4, "dli_target": (15.0, 20.0)})
    msg = " ".join(str(x) for x in out)
    assert "15-20" in msg and "6-12" not in msg, "the short-day alert quotes the configured band"
    ctx = ai_report.build_context(
        {"light_metrics": {"ppfd": 200, "dli": 9.0, "dli_target": [15, 20]}}
    )
    assert "15-20" in ctx and "6-12" not in ctx, "the AI report is told the configured band"
    stale = [
        f
        for f in (
            "templates/index.html",
            *APP_MODULES,
            "alerts.py",
            "ai_report.py",
            *(str(p.relative_to(APP)) for p in sorted((APP / "static" / "js").glob("*.js"))),
        )
        if re.search(r"\b6-12\b", (APP / f).read_text())
    ]
    assert not stale, "no hardcoded 6-12 band left" + (f" {stale}" if stale else "")


def test_photo_light():
    """Photos leave the light alone unless "Set the light for each photo" is on."""
    set_to = []  # what the photo itself sets; the schedule writes again once it lets go
    real_sb, real_usb = light_mod.set_brightness, camera_mod._usb_capture

    def record(p, *a, **k):
        if light_mod.holder() == camera_mod.HOLD_PHOTO:
            set_to.append(p)

    light_mod.set_brightness = record
    camera_mod._usb_capture = lambda cfg_, out, w, h, warmup=None: (False, "test")
    with config.settings_lock:
        saved = {
            k: config.settings.get(k)
            for k in ("capture_set_light", "capture_brightness", "camera_backend", "setups")
        }
        config.settings.update(camera_backend="usb", capture_brightness=80, setups=[])
        config.settings.pop("capture_set_light", None)  # as on an existing install
    try:
        camera_mod.take_photo(dict(config.DEFAULTS, **config.settings), datetime.now())
        off = list(set_to)
        r = c.post("/api/settings", json={"capture_set_light": True}).get_json()
        camera_mod.take_photo(dict(config.settings), datetime.now())
        on = set_to[len(off) :]
    finally:
        light_mod.set_brightness, camera_mod._usb_capture = real_sb, real_usb
        with config.settings_lock:
            config.settings.update(saved)
    assert config.DEFAULTS.get("capture_set_light") is False and off == [], (
        "by default a photo leaves the light where it is"
    )
    assert r["ok"] and on == [80], (
        f"with the setting on, a photo sets the main light to the photo brightness ({on})"
    )
    page = c.get("/").get_data(as_text=True)
    js = page_js()
    assert (
        'name="capture_set_light"' in page
        and 'class="capbright"' in page
        and config.FORM["capture_set_light"]["kind"] == "bool"
        and "function syncCaptureLight()" in js
    ), "Settings, Camera has the switch, and Photo brightness shows only when it is on"
    src = (APP / "camera.py").read_text()
    fs = re.search(r"def run_focus_sweep\(\):[\s\S]*?score_at", src)
    assert (
        fs
        and "if light_for_photo(cfg):" in fs.group(0)
        and "camera_mod.light_for_photo(cfg)" in (APP / "monitor.py").read_text()
    ), "the focus sweep and the AI report's photo-light line follow the same switch"


def test_light_holder(control_loop):
    """One thing at a time has the light; the schedule has it otherwise."""
    lm = light_mod
    lm.release(lm.holder())
    first = lm.claim("photo", 5)
    second = lm.claim(lm.HOLD_SWEEP, 5)
    again = lm.claim("photo", 5)
    who = lm.holder()
    lm.release(lm.HOLD_SWEEP)  # not the holder: changes nothing
    still = lm.holder()
    lm.release("photo")
    assert (
        first
        and not second
        and again
        and who == "photo"
        and still == "photo"
        and lm.holder() is None
    ), "the light has one holder at a time, and only the holder can give it back"

    # the schedule stands off while the light is held, and resumes after
    lm.claim("test", 5)
    time.sleep(0.3)  # let a pass that had already started finish
    n0 = len(PWM_WRITES)
    time.sleep(0.5)
    held_writes = len(PWM_WRITES) - n0
    lm.release("test")
    time.sleep(0.5)
    assert held_writes == 0 and len(PWM_WRITES) > n0, (
        f"the control loop leaves a held light alone and takes it up again on release "
        f"({held_writes} writes while held)"
    )

    # a holder that never lets go loses the light when its time is up
    lm.claim("test", 0.05)
    time.sleep(0.15)
    assert lm.holder() is None and lm.claim("photo", 5), (
        "a hold ends at its time limit even if it is never released"
    )
    lm.release("photo")

    # a photo and a calibration sweep keep out of each other's way
    set_to = []
    real_set, real_raw = lm.set_brightness, lm.set_brightness_raw
    lm.set_brightness = lambda v, *a, **k: set_to.append(("set", v))
    lm.set_brightness_raw = lambda v: set_to.append(("raw", v))
    with config.settings_lock:
        cfg = dict(config.settings, camera_backend="usb", capture_set_light=True)
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    try:
        lm.claim(lm.HOLD_SWEEP, 5)
        time.sleep(0.3)
        set_to.clear()
        photo = camera_mod.take_photo(cfg, now)
        during_sweep = list(set_to)
        lm.release(lm.HOLD_SWEEP)

        lm.claim(camera_mod.HOLD_PHOTO, 5)
        time.sleep(0.3)  # a schedule pass that began before the claim has finished
        set_to.clear()
        lm.sweep_state.update(running=True, cancel=False, error="", target="main")
        lm.run_light_sweep(step=50, settle=0.0)
        sweep_err = lm.sweep_state["error"]
        during_photo = list(set_to)
        lm.release(camera_mod.HOLD_PHOTO)
    finally:
        lm.set_brightness, lm.set_brightness_raw = real_set, real_raw
        lm.sweep_state["error"] = ""
    assert photo is None and during_sweep == [], (
        "a photo does not take the light from a calibration sweep"
    )
    assert (
        "in use (photo)" in sweep_err and lm.sweep_state["running"] is False and during_photo == []
    ), f"a sweep does not start while a photo has the light ({sweep_err})"

    # the photo gives the light back before it is rotated and thumbnailed
    at_thumb = []
    real = camera_mod._usb_capture_view, photos_mod.make_thumb
    camera_mod._usb_capture_view = lambda cfg_, out, w, h, warmup=None: (
        at_thumb.append(("capture", lm.holder())),
        Path(out).write_bytes(b"\xff\xd8\xff\xd9"),
        (True, None),
    )[2]
    photos_mod.make_thumb = lambda path, cfg_=None, **k: at_thumb.append(("thumb", lm.holder()))
    real_sleep = camera_mod.time.sleep
    camera_mod.time.sleep = lambda s_: None
    try:
        shot = camera_mod.take_photo(dict(cfg, capture_set_light=False), now)
    finally:
        camera_mod._usb_capture_view, photos_mod.make_thumb = real
        camera_mod.time.sleep = real_sleep
        if shot:
            shot.unlink(missing_ok=True)
    assert at_thumb == [("capture", "photo"), ("thumb", None)] and lm.holder() is None, (
        f"a photo holds the light only until the frame is on disk ({at_thumb})"
    )


def test_lux_range():
    """A calibration sweep that hits the light sensor's ceiling is refused: a
    clipped reading must not be taken for the fixture's full output."""
    import types as _types

    writes = []

    class Bus:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, b):
            writes.append(bytes(b)[0])

    class FakeBH:
        def __init__(self, i2c, address):
            self.i2c_device = Bus()
            self.resolution = 1

        @property
        def lux(self):
            return 1000.0  # what the library reports, before the range factor

    mod = _types.SimpleNamespace(BH1750=FakeBH, Resolution=_types.SimpleNamespace(MID=0, HIGH=1))
    real_i2c, real_mod = sensors._i2c, sys.modules.get("adafruit_bh1750")
    sensors._i2c = lambda: None
    sys.modules["adafruit_bh1750"] = mod
    for st_ in sensors._lux.values():
        st_.update(dev=None, init=False, fail=0, scale=1.0)
    try:
        got = sensors._read_lux()
    finally:
        sensors._i2c = real_i2c
        sys.modules["adafruit_bh1750"] = real_mod
        for st_ in sensors._lux.values():
            st_.update(dev=None, init=False, fail=0, scale=1.0)
    assert got.get("lux") == round(1000 * 69 / 31, 1) and writes[:2] == [0x40, 0x7F], (
        f"the light sensor is set to its widest range, up to 121,557 lx ({got}, {writes[:2]})"
    )
    # a sweep whose readings stop at the sensor's maximum must not calibrate
    clipped = [[p, min(27306.0, 12.9 + p * 1400)] for p in range(0, 101, 5)]
    try:
        light_mod.build_linear_table(clipped)
        refused = ""
    except ValueError as e:
        refused = str(e)
    real = [[p, 12.9 + (p - 4) * 1100 if p > 4 else 12.9] for p in range(0, 101, 5)]
    table, _info = light_mod.build_linear_table(real)
    assert "maximum" in refused and "not saved" in refused and table[100] == 100, (
        f"a sweep that hit the light sensor's maximum is refused, not taken as the fixture's "
        f"full output; a real one still maps 100% to full ({refused[:70]})"
    )
    bad = {
        "light_linear_on": True,
        "light_linear": {"peak_lux": 27306.0, "saturation_raw": 19.0},
    }
    ok = dict(bad, light_linear={"peak_lux": 88123.0, "saturation_raw": 97.0})
    with config.settings_lock:
        was = {k: config.settings.get(k) for k in ("light_linear_on", "light_linear")}
        config.settings.update(bad)
    try:
        flag = c.get("/api/status").get_json().get("light_linear_clipped")
    finally:
        with config.settings_lock:
            config.settings.update(was)
    js = page_js()
    assert (
        light_mod.calibration_clipped(bad)
        and not light_mod.calibration_clipped(ok)
        and flag is True
        and "light_linear_clipped" in js
    ), (
        "a calibration the sensor capped is flagged on the dashboard (left in use, since "
        "dropping it would take the light straight to full power)"
    )
