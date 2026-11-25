"""Tests: watering."""

import threading
import time
import types

from harness import (
    app_source,
    c,
    config,
    db,
    hardware,
    light_mod,
    note,
    page_js,
    sensors,
    setups_mod,
    water,
)


def test_watering():
    floats = sensors._floats()

    def fill(event, cap=3):
        """Run a fill on tray 1; `event(res)` fires 0.4 s in to change the world."""
        for st in hardware.pump_state.values():
            st.update(running=False, today_seconds=0, day=config._today_str())
        with config.settings_lock:
            config.settings.update(
                fill_max_seconds=cap, auto_water=True, auto_water_trays=["1", "2"]
            )
        floats["1"].is_pressed = True  # not full yet
        res = {"reservoir": "ok"}
        real_res = water.reservoir_state
        water.reservoir_state = lambda: res["reservoir"]

        def later():
            time.sleep(0.4)
            event(res)

        threading.Thread(target=later, daemon=True).start()
        try:
            ok, why = water.run_pump_until_full("1", "auto")
        finally:
            water.reservoir_state = real_res
        return (
            ok,
            why,
            hardware.pump_state["1"]["last_detail"],
            "1" in water.armed_trays(config.settings),
        )

    ok, why, detail, _ = fill(lambda res: setattr(floats["1"], "is_pressed", False))
    assert ok and "full at" in detail, f"fill stops when the float trips ({detail})"
    assert not hardware._pumps["1"].value, "pump is off after the fill"

    ok, why, detail, armed = fill(lambda res: res.update(reservoir="empty"))
    assert not ok and "reservoir ran empty" in detail, (
        f"fill stops when the reservoir runs dry ({detail})"
    )
    assert not armed and water.armed_trays(config.settings) == ["2"], (
        "a failed fill disarms that tray only; the other stays armed"
    )
    assert not hardware._pumps["1"].value, "pump is off after a reservoir stop"

    ok, why, detail, _ = fill(lambda res: None, cap=1)
    assert not ok and "cap" in detail, f"fill stops at the time cap ({detail})"

    # the daily budget: 0.5 s left of it for tray 1, none used by tray 2
    for st in hardware.pump_state.values():
        st.update(running=False, today_seconds=0, day=config._today_str(), last_run=0.0)
    with config.settings_lock:
        saved = {k: config.settings.get(k) for k in ("pump_daily_max_seconds", "probe_cal")}
        config.settings.update(
            fill_max_seconds=3,
            pump_daily_max_seconds=100,
            auto_water=True,
            auto_water_trays=["1", "2"],
            moisture_threshold_pct=30,
            probe_cal={"1": {"wet": 1.0, "dry": 2.6}, "2": {"wet": 1.0, "dry": 2.6}},
        )
    hardware.pump_state["1"]["today_seconds"] = 99.5
    floats["1"].is_pressed = True
    water.fill_failures.clear()
    pumped = []
    real = (
        water.reservoir_state,
        water.probe_volts_filtered,
        sensors.probe_spread,
        water.auto_water_blockers,
        sensors.read_float,
        water.run_pump_until_full,
    )
    water.reservoir_state = lambda: "ok"
    try:
        ok, why = water.run_pump_until_full("1", "auto")
        detail = hardware.pump_state["1"]["last_detail"]
        assert (
            not ok
            and "daily pump limit" in why
            and "daily pump limit" in detail
            and not water.fill_failures
            and water.armed_trays(config.settings) == ["1", "2"]
        ), (
            f"a fill cut short by the daily limit is not a failed fill: no alert, "
            f"and the tray stays armed ({detail})"
        )
        r = c.post("/api/pump", json={"tray": "1", "seconds": 2}).get_json()
        assert r.get("ok") is False and "daily pump limit" in r.get("error", ""), (
            f"a pump run that would be refused says so in the reply ({r.get('error')})"
        )
        water.probe_volts_filtered = lambda ch, snap=None: (2.5, time.time())
        sensors.probe_spread = lambda ch, samples=10, delay=0.2: (2.5, 0.001)
        water.auto_water_blockers = lambda cfg=None: {}
        sensors.read_float = lambda ch="1": 1.0
        water.run_pump_until_full = lambda tray, reason="fill", force=False: (
            pumped.append(tray),
            (True, "ok"),
        )[1]
        water.auto_water_pass()
        assert pumped == ["2"], (
            f"a tray at its daily limit does not stop auto-watering the next one ({pumped})"
        )
    finally:
        (
            water.reservoir_state,
            water.probe_volts_filtered,
            sensors.probe_spread,
            water.auto_water_blockers,
            sensors.read_float,
            water.run_pump_until_full,
        ) = real
        with config.settings_lock:
            config.settings.update(saved)

    # the fill loop on its own: how each kind of fill ends
    def fill_ends(reads, cap=5.0, at=0.0, limited=False, force=False, reservoir="ok"):
        seq = iter(reads)
        real_res = water.reservoir_state
        water.reservoir_state = lambda: reservoir
        try:
            run = types.SimpleNamespace(elapsed=lambda: at)
            return water._fill(run, lambda: next(seq), cap, limited, force)
        finally:
            water.reservoir_state = real_res

    ends = {
        "steady full": fill_ends([1, 0, 0, 0, 0]),
        "slosh, then full": fill_ends([0, 0, 1, 0, 0, 0, 0]),
        "float gone": fill_ends([1, None, None, None, None]),
        "time up": fill_ends([1], at=5.0),
        "budget up": fill_ends([1], at=5.0, limited=True),
        "reservoir empty": fill_ends([1], reservoir="empty"),
        "reservoir empty, forced": fill_ends([0, 0, 0, 0], reservoir="empty", force=True),
    }
    assert ends == {
        "steady full": water.FULL,
        "slosh, then full": water.FULL,
        "float gone": water.FLOAT_LOST,
        "time up": water.TIME_CAP,
        "budget up": water.DAILY_CAP,
        "reservoir empty": water.RESERVOIR,
        "reservoir empty, forced": water.FULL,
    }, f"the fill loop ends for the right reason in each case ({ends})"

    # and what each ending means for the tray
    real_post = water.schedule_postfill
    water.schedule_postfill = lambda tray, tripped=True: None
    policy = {}
    try:
        for outcome in water.FILL_TEXT:
            water.fill_failures.clear()
            with config.settings_lock:
                config.settings.update(auto_water=True, auto_water_trays=["1", "2"])
            water._after_fill("1", outcome, 3.0)
            policy[outcome] = ("1" in water.fill_failures, water.armed_trays(config.settings))
    finally:
        water.schedule_postfill = real_post
        water.fill_failures.clear()
        with config.settings_lock:
            config.settings.update(auto_water=True, auto_water_trays=["1", "2"])
    faults = {o for o, (failed, _) in policy.items() if failed}
    assert faults == set(water.FILL_FAULTS) == {
        water.TIME_CAP,
        water.RESERVOIR,
        water.FLOAT_LOST,
    } and all(
        armed == (["2"] if o in faults else ["1", "2"]) for o, (_, armed) in policy.items()
    ), f"only a fault raises the tray's alert and disarms it, and only that tray ({sorted(faults)})"

    # failed fills are kept per tray
    import alerts

    ok, why, detail, _ = fill(lambda res: res.update(reservoir="empty"))
    after_fail = dict(water.fill_failures)
    water.fill_failures["2"] = "Tray 2 fill ran to the 60.0s cap without the float tripping."
    ok, why, detail, _ = fill(lambda res: setattr(floats["1"], "is_pressed", False))
    assert list(after_fail) == ["1"] and ok and list(water.fill_failures) == ["2"], (
        "a good fill clears that tray's failure and leaves another tray's standing"
    )
    both = {"1": "tray 1 failed", "2": "tray 2 failed"}
    fired = sorted(a[1] for a in alerts.check_all({"_fill_failed": both}, {}) if a[0] == "fire")
    later = [
        (a[0], a[1])
        for a in alerts.check_all({"_fill_failed": {"1": "tray 1 failed"}}, {})
        if a[1].startswith("fill_failed")
    ]
    alerts.check_all({"_fill_failed": {}}, {})
    assert fired == ["fill_failed:1", "fill_failed:2"] and later == [("clear", "fill_failed:2")], (
        f"each tray's failed fill is its own alert, resolved on its own ({later})"
    )
    db.kv_set("fill_failure", {"msg": "Tray 2 fill stopped at 3.0s: the reservoir ran empty."})
    water.restore_persistent_state()
    assert list(water.fill_failures) == ["2"], (
        "a failure saved before they were kept per tray is restored to the tray it names"
    )
    water.fill_failures.clear()
    water.save_persistent_state()

    # a run cut off by a restart: what was saved as the pump started is enough
    for st in hardware.pump_state.values():
        st.update(running=False, today_seconds=0, reserved=0, day=config._today_str())
    during = {}
    real_on = hardware._pumps["1"].on

    def on_and_look():
        during.update(db.kv_get("pump_state")["1"])  # what a kill right now would leave
        real_on()

    hardware._pumps["1"].on = on_and_look
    try:
        water.run_pump("1", 0.2, "manual")
    finally:
        hardware._pumps["1"].on = real_on
    after_run = dict(db.kv_get("pump_state")["1"])
    db.kv_set("pump_state", {"1": during})
    hardware.pump_state["1"].update(today_seconds=0, last_run=0.0)
    water.restore_persistent_state()
    back = hardware.pump_state["1"]
    assert (
        during.get("reserved") == 0.2
        and during.get("last_run", 0) > time.time() - 60
        and after_run.get("reserved") == 0
        and 0.19 < after_run.get("today_seconds", 0) < 0.6
        and back["today_seconds"] == 0.2
        and back["reserved"] == 0
        and back["last_run"] == during["last_run"]
    ), (
        f"a pump run is on record before the pump starts, so a restart mid-run still "
        f"counts it against the daily limit and the cooldown ({back['today_seconds']}s)"
    )

    ok, why, detail, _ = fill(lambda res: floats.pop("1", None))
    assert not ok and "float sensor stopped answering" in detail, (
        f"lost float reported as such ({detail})"
    )
    assert not hardware._pumps["1"].value, "pump is off after a lost float"

    # tray 1 has no float now: a fill is refused even when forced
    water.fill_failures.clear()
    with config.settings_lock:
        config.settings.update(auto_water=True, auto_water_trays=["1", "2"])
    ok, why = water.run_pump_until_full("1", "fill", force=True)
    assert (
        not ok
        and "refusing to fill blind" in why
        and not water.fill_failures
        and "1" in water.armed_trays(config.settings)
    ), "a forced fill with no float is refused outright, not run and reported as a fault"


def test_probe_names():
    js = page_js()
    assert "'Soil moisture '+t" in js and (
        "trayLabels[t]||('Tray '+t)" in js or "trayLabels[t]||'Tray '+t" in js
    ), "probes read as Soil moisture 1 and 2; canopy and watering rows use the tray's name"
    assert config.settings.get("probe_names") == {"2": "Left bench"}, (
        f"an old default probe name is cleared, a chosen one kept ({config.settings.get('probe_names')})"
    )
    gl = app_source()
    assert '"probe_names": {},' in gl and 'n == f"Tray {t}"' in gl, (
        "the old stored probe names (Tray 1, Tray 2) are cleared so the new ones show"
    )


def test_per_sensor_controls():
    with config.settings_lock:
        config.settings.update(auto_water=False, auto_water_trays=[])
    real_b = water.auto_water_blockers
    water.auto_water_blockers = lambda cfg=None: {}
    try:
        a = c.post("/api/auto_water", json={"enabled": True, "trays": ["1"]}).get_json()
        b = c.post("/api/auto_water", json={"enabled": True, "trays": ["2"]}).get_json()
        d = c.post("/api/auto_water", json={"enabled": False, "trays": ["1"]}).get_json()
    finally:
        water.auto_water_blockers = real_b
    assert (
        a["armed"] == ["1"]
        and b["armed"] == ["1", "2"]
        and d["armed"] == ["2"]
        and config.settings["auto_water"] is True
    ), "auto-watering arms and disarms per tray (a setup's trays)"
    water.auto_water_blockers = lambda cfg=None: {"2": ["probe not calibrated"]}
    try:
        e = c.post("/api/auto_water", json={"enabled": True, "trays": ["1"]}).get_json()
    finally:
        water.auto_water_blockers = real_b
    assert e["ok"], "a blocker on another setup's tray does not stop arming this one"
    c.post("/api/auto_water", json={"enabled": False})
    # calibrate the second light against its own sensor
    c.post(
        "/api/settings",
        json={
            "setups": [
                {
                    "name": "Seedlings",
                    "light": "second",
                    "lux": "lux:2",
                    "sensors": [],
                    "dli_low": 10,
                    "dli_high": 15,
                },
                {
                    "name": "Transplants",
                    "light": "main",
                    "lux": "lux",
                    "sensors": [],
                    "dli_low": 15,
                    "dli_high": 20,
                },
            ]
        },
    )
    main_before = config.settings.get("light_curve")
    real_read = sensors.read_all

    def sensors_fake():
        return {"lux": 999.0, "lux:2": 100.0 * float(light_mod.sweep_state.get("l2_raw") or 0)}

    sensors.read_all = sensors_fake
    light_mod.sweep_state.update(running=True, cancel=False, error="", target="second", l2_raw=0.0)
    try:
        light_mod.run_light_sweep(
            step=50, settle=0.0, linearize=False, target="second", key="lux:2"
        )
    finally:
        sensors.read_all = real_read

    # the same sweep with a disk that cannot be written
    def no_disk():
        raise OSError(28, "No space left on device")

    real_save = config.save_config
    light_mod.sweep_state.update(running=True, cancel=False, error="", target="second", l2_raw=0.0)
    config.save_config, sensors.read_all = no_disk, sensors_fake
    try:
        light_mod.run_light_sweep(
            step=50, settle=0.0, linearize=False, target="second", key="lux:2"
        )
    finally:
        config.save_config, sensors.read_all = real_save, real_read
    assert (
        light_mod.sweep_state["running"] is False
        and "not saved" in light_mod.sweep_state["error"]
        and light_mod.sweep_state["l2_raw"] == 0.0
    ), (
        f"a sweep whose result cannot be saved still hands the light back "
        f"({light_mod.sweep_state['error']})"
    )
    light_mod.sweep_state["error"] = ""
    c2 = config.settings.get("light2_curve") or {}
    assert (
        c2.get("sensor") == "lux:2"
        and [p[1] for p in c2.get("points", [])] == [0.0, 5000.0, 10000.0]
        and config.settings.get("light_curve") == main_before
    ), (
        f"the second light calibrates against its own sensor and keeps its own curve ({c2.get('points')})"
    )
    st = c.get("/api/status").get_json()
    assert "light2_cal" in st and st["light2_cal"]["light_curve"]["sensor"] == "lux:2", (
        "the Light response card gets the second light's calibration"
    )
    js = page_js()
    assert (
        "linearize:true,light:ctlTarget" in js
        and "trays:autoWaterTrays" in js
        and "reselsewhere" in js
    ), "Calibrate, Arm and the reservoir row follow the tab"
    c.post("/api/settings", json={"setups": []})


def test_tray_sensor_map():
    """Any sensor can be placed in any tray (Settings, Setups), and a
    tray's watering reads the probe and float placed in it."""
    v = config.SETTINGS_VALIDATORS["tray_sensors"]
    good = v({"1": ["probe:2", "float:2", "canopy:1", "lux"], "3": ["probe:1", "temp:soil"]})
    errs = []
    for bad in (
        {"1": ["probe:1"], "2": ["probe:1"]},
        {"1": ["float:1", "float:2"]},
        {"1": "probe:1"},
    ):
        try:
            v(bad)
            errs.append(None)
        except ValueError as e:
            errs.append(str(e))
    assert (
        good == {"1": ["float:2", "probe:2"], "3": ["probe:1", "temp:soil"]}
        and all(errs)
        and "pick one" in errs[0]
        and "one float per tray" in errs[1]
    ), (
        f"a tray takes one probe and one float, none in two trays; canopy and the light "
        f"sensor are not a tray's to claim ({errs})"
    )
    with config.settings_lock:
        saved = {
            k: config.settings.get(k)
            for k in (
                "tray_sensors",
                "trays",
                "setups",
                "probe_cal",
                "auto_water_trays",
                "auto_water",
            )
        }
        trays = dict(config.settings.get("trays") or {})
        for t in ("1", "2", "3"):
            trays.setdefault(t, {"label": f"Bed {t}", "rows": 2, "cols": 2, "cells": {}})
        config.settings.update(
            tray_sensors=good,
            trays=trays,
            setups=[
                {
                    "id": "a",
                    "name": "A",
                    "light": "main",
                    "lux": "lux",
                    "trays": ["1"],
                    "sensors": [],
                    "dli_low": 8,
                    "dli_high": 10,
                },
                {
                    "id": "b",
                    "name": "B",
                    "light": "",
                    "lux": "",
                    "trays": ["2", "3"],
                    "sensors": ["humidity"],
                    "dli_low": 8,
                    "dli_high": 10,
                },
            ],
        )
        cfg = dict(config.settings)
    try:
        legacy = setups_mod.tray_sensors({"trays": {"2": {}}}, "2")
        owner = [st["id"] for st in setups_mod.sensor_setups(cfg, "probe:2")]
        soil = [st["id"] for st in setups_mod.sensor_setups(cfg, "temp:soil")]
        assert (
            legacy == ["probe:2", "float:2"]
            and setups_mod.tray_probe(cfg, "1") == "2"
            and setups_mod.tray_float(cfg, "1") == "2"
            and setups_mod.tray_probe(cfg, "2") is None
            and setups_mod.tray_probe(cfg, "3") == "1"
            and setups_mod.key_trays(cfg, "probe:1") == ["3"]
            and setups_mod.key_trays(cfg, "temp:soil") == ["3"]
            and owner == ["a"]
            and "b" in soil
        ), (
            f"a tray without a list keeps the probe and float of its number; a placed sensor "
            f"belongs to its tray and the tray's setup (probe 2 -> {owner}, soil temp -> {soil})"
        )
        r = c.get("/api/status").get_json()
        assert (
            r["tray_map"]["1"]["probe"] == "2"
            and r["tray_map"]["2"]["probe"] is None
            and r["sensor_trays"].get("temp:soil") == ["3"]
            and r["water"]["trays"]["1"]["probe_ch"] == "2"
            and r["water"]["trays"]["1"]["float_ch"] == "2"
        ), "the status sends each tray's sensors, probe and float, and the reverse"
        # watering: tray 1 decides from probe 2 and float 2, never its own number
        seen = []
        real = (
            water.probe_volts_filtered,
            sensors.read_float,
            sensors.probe_spread,
            water.run_pump_until_full,
            water.auto_water_blockers,
        )
        water.probe_volts_filtered = lambda ch, snap=None: (
            seen.append(("probe", ch)),
            (2.5, time.time()),
        )[1]
        sensors.read_float = lambda ch="1": (seen.append(("float", ch)), 1.0)[1]
        sensors.probe_spread = lambda ch, samples=10, delay=0.2: (
            seen.append(("spread", ch)),
            (2.5, 0.001),
        )[1]
        water.run_pump_until_full = lambda tray, reason="fill", force=False: (
            seen.append(("pump", tray)),
            (True, "ok"),
        )[1]
        water.auto_water_blockers = lambda cfg=None: {}
        try:
            with config.settings_lock:
                config.settings.update(
                    probe_cal={"2": {"wet": 1.0, "dry": 2.6}, "1": {"wet": 1.0, "dry": 2.6}},
                    auto_water_trays=["1"],
                    auto_water=True,
                    moisture_threshold_pct=30,
                )
            hardware.pump_state["1"]["last_run"] = 0.0
            water.auto_water_pass()
        finally:
            (
                water.probe_volts_filtered,
                sensors.read_float,
                sensors.probe_spread,
                water.run_pump_until_full,
                water.auto_water_blockers,
            ) = real
        assert (
            ("pump", "1") in seen
            and ("probe", "2") in seen
            and ("float", "2") in seen
            and ("spread", "2") in seen
            and ("probe", "1") not in seen
            and ("float", "1") not in seen
        ), f"auto-watering tray 1 reads the probe and float placed in it ({seen})"
        blk = water.auto_water_blockers()
        assert "no moisture probe in this tray" in " ".join(blk.get("2", [])), (
            f"a pump tray with no probe placed in it cannot be armed ({blk.get('2')})"
        )
        # the fill stops on the tray's own float: float 2 reads full, float 1 not
        fl = sensors._floats()
        had = {k: getattr(fl.get(k), "is_pressed", None) for k in ("1", "2")}
        if "2" in fl:
            fl["2"].is_pressed = False
            if "1" in fl:
                fl["1"].is_pressed = True
            for st in hardware.pump_state.values():
                st.update(running=False, today_seconds=0, day=config._today_str())
            ok, why = water.run_pump_until_full("1", "fill")
            for k, val in had.items():
                if k in fl and val is not None:
                    fl[k].is_pressed = val
            assert not ok and "already full" in why, (
                f"a fill reads the float placed in the tray ({why})"
            )
        else:
            note("fill on a remapped float (no fake float 2)")
    finally:
        with config.settings_lock:
            config.settings.update(saved)
