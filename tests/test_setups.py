"""Tests: setups."""

import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from harness import (
    APP,
    Code,
    ai_report,
    c,
    camera_mod,
    config,
    control,
    db,
    hardware,
    light_mod,
    monitor,
    page_js,
    readings_mod,
    sensors,
    setups_mod,
)


def test_setups():
    st = c.get("/api/status").get_json()
    one = st.get("setups") or []
    assert len(one) == 1 and one[0]["lux"] == "lux" and one[0]["sensors"] == [], (
        "with no setups defined there is one, covering every sensor"
    )
    good = [
        {
            "name": "Seedlings",
            "light": "main",
            "lux": "lux",
            "sensors": ["temp:soil", "probe:1"],
            "dli_low": 10,
            "dli_high": 15,
        },
        {
            "name": "Transplants",
            "light": "second",
            "lux": "lux:2",
            "k": 70,
            "sensors": ["probe:2"],
            "dli_low": 15,
            "dli_high": 20,
        },
        {"name": "Shelf", "light": "", "lux": "", "sensors": [], "dli_low": 6, "dli_high": 12},
    ]
    bad_light = [dict(good[0]), dict(good[1], light="main")]
    bad_lux = [dict(good[0]), dict(good[1], lux="lux")]
    bad_band = [dict(good[0], dli_low=15, dli_high=10)]
    errs = [
        c.post("/api/settings", json={"setups": b}).get_json()
        for b in (bad_light, bad_lux, bad_band)
    ]
    assert all(not e["ok"] and "setups" in e["errors"] for e in errs), (
        "a light or light sensor used twice, or a backwards band, is refused"
    )
    r = c.post("/api/settings", json={"setups": good}).get_json()
    assert r["ok"] and [x["id"] for x in config.settings["setups"]] == [
        "seedlings",
        "transplants",
        "shelf",
    ], "three setups save, each with an id"
    now = int(time.time())
    midnight = int(
        datetime.now(ZoneInfo(config.settings["timezone"]))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    t0 = max(midnight + 60, now - 3 * 3600)
    rows = []
    for t in range(t0, now, 300):
        rows += [("lux", 12000.0, t), ("lux:2", 24000.0, t)]
    for key, v, t in rows:
        db.log_many([(key, v)], ts=t)
    out = {x["id"]: x for x in c.get("/api/status").get_json()["setups"]}
    d1, d2 = (
        (out["seedlings"]["day"] or {}).get("dli"),
        (out["transplants"]["day"] or {}).get("dli"),
    )
    assert d1 and d2 and abs(d2 / d1 - 2 * 60 / 70) < 0.02, (
        f"each setup's DLI comes from its own sensor and factor ({d1} vs {d2})"
    )
    cur = ((out["seedlings"]["day"] or {}).get("curve") or {}).get("today") or []
    vals_ = [v for _, v in cur]
    assert len(cur) > 3 and vals_ == sorted(vals_) and abs(vals_[-1] - d1) < 0.2, (
        f"the Day card's DLI curve climbs to today's total ({vals_[-1] if vals_ else None} vs {d1})"
    )
    js2 = page_js()
    assert (
        "function setLight2" in js2
        and "light2_override" in js2
        and "S.schedule_mode==='light2'" in js2
    ), "the Light card and schedule chart drive the selected setup's light"
    assert 'id="dlichart"' in (APP / "templates" / "index.html").read_text(), (
        "the Day card has the DLI-against-target chart"
    )
    assert (
        out["transplants"]["band"] == [15.0, 20.0] and out["shelf"]["plan"]["status"] == "no_sensor"
    ), "each setup keeps its own band; a setup without a light sensor says so"
    import alerts

    alerts.reset()
    acts = alerts.check_all(
        {"_dli_setups": {"seedlings": 3.0, "transplants": 16.0}},
        {
            "dli_low": 4,
            "setups": [
                {"id": "seedlings", "name": "Seedlings", "band": (10, 15)},
                {"id": "transplants", "name": "Transplants", "band": (15, 20)},
            ],
        },
    )
    fired = [a[1] for a in acts if a[0] == "fire"]
    assert fired == ["dli_low:seedlings"] and "Seedlings" in acts[0][2], (
        "the short-day alert is judged per setup and names it"
    )
    ctx = ai_report.build_context(
        {
            "light_metrics": {
                "ppfd": 200,
                "dli": 5,
                "setups": [
                    {"name": "Seedlings", "dli": 5, "band": [10, 15]},
                    {"name": "Transplants", "dli": None, "band": [15, 20]},
                ],
            }
        }
    )
    assert "Seedlings: 5 mol" in ctx and "Transplants: not measured" in ctx, (
        "the AI report is told about each setup"
    )
    with config.settings_lock:
        config.settings["light_backend"] = "dim"
    opts = {o["value"]: o["label"] for o in c.get("/api/status").get_json()["light_options"]}
    with config.settings_lock:
        config.settings["light_backend"] = "pwm"
    opts2 = {o["value"]: o["label"] for o in setups_mod.light_options()}
    assert opts.get("main") == "AC fixture (dim line)" and opts2.get("main") == "5V LED panel", (
        f"setups list lights by fixture, following the backend ({opts} / {opts2})"
    )
    js = page_js()
    assert "'Main light'" not in js and "lightChoices(" in js, (
        "the Setups editor offers fixtures, not 'main' and 'second'"
    )
    assert (
        'id="setuptabs"' in (APP / "templates" / "index.html").read_text()
        and "Object.keys(sensorData).filter(inSetup)" in js
        and "&&inSetup(k)" in js
    ), "setup tabs exist and filter the sensor chips and charts"
    # two BH1750s, keyed by address
    import types as _types

    vals = {0x23: 111.0, 0x5C: 222.0}

    class FakeBH:
        def __init__(self, i2c, address):
            self.a = address

        @property
        def lux(self):
            return vals[self.a]

    real_i2c, real_mod = sensors._i2c, sys.modules.get("adafruit_bh1750")
    sensors._i2c = lambda: None
    sys.modules["adafruit_bh1750"] = _types.SimpleNamespace(BH1750=FakeBH)
    for st_ in sensors._lux.values():
        st_.update(dev=None, init=False, fail=0)
    try:
        got = sensors._read_lux()
    finally:
        sensors._i2c = real_i2c
        sys.modules["adafruit_bh1750"] = real_mod
        for st_ in sensors._lux.values():
            st_.update(dev=None, init=False, fail=0)
    assert got == {"lux": 111.0, "lux:2": 222.0}, f"two light sensors read as lux and lux:2 ({got})"
    with config.settings_lock:
        config.settings["trays"] = dict(
            config.settings.get("trays") or {},
            T3={"label": "Transplants", "rows": 4, "cols": 5, "cells": {}},
        )
    r = c.post(
        "/api/settings",
        json={"setups": [dict(good[0], trays=["1", "2", "gone"]), dict(good[1], trays=["T3"])]},
    ).get_json()
    got_t = {x["id"]: x["trays"] for x in c.get("/api/status").get_json()["setups"]}
    assert r["ok"] and got_t == {"seedlings": ["1", "2"], "transplants": ["T3"]}, (
        f"trays are assigned per setup, and a tray that no longer exists drops out ({got_t})"
    )
    js3 = page_js()
    assert (
        "filter(trayInSetup)" in js3 and "trayInSetup(t)?'':'none'" in js3 and 'data-t="' in js3
    ), "the planting map, watering rows and the Setups editor follow each setup's trays"
    c.post("/api/settings", json={"setups": []})


def test_fan_camera_timing():
    tz = ZoneInfo(config.settings["timezone"])
    base = [
        {
            "name": "Seedlings",
            "light": "second",
            "lux": "",
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
    two_fans = [dict(base[0], fan=True), dict(base[1], fan=True)]
    e = c.post("/api/settings", json={"setups": two_fans}).get_json()
    assert not e["ok"] and "fan" in e["errors"].get("setups", ""), (
        "the fan can belong to only one setup"
    )
    with config.settings_lock:
        config.settings.update(light2_start="20:00", light2_end="02:00", light2_on=True)
    r = c.post(
        "/api/settings", json={"setups": [dict(base[0], fan=True, camera=True), base[1]]}
    ).get_json()
    cfg = dict(config.settings)
    now = datetime.now(tz)
    mon, moff = now.replace(hour=7, minute=0), now.replace(hour=19, minute=0)
    sd, tr = setups_mod.setups(cfg)
    on2, off2 = setups_mod.setup_window(cfg, sd, mon, moff)
    assert (
        r["ok"]
        and (on2.hour, off2.hour) == (20, 2)
        and off2 > on2
        and setups_mod.setup_window(cfg, tr, mon, moff) == (mon, moff)
    ), "each setup uses its own light's hours; an overnight window ends the next day"
    none_on, none_off = setups_mod.setup_window(cfg, dict(sd, light=""), mon, moff)
    assert none_on == mon and none_off == off2, "a setup with no light spans both lights"
    fon, foff = setups_mod.setup_window(cfg, setups_mod.setup_with(cfg, "fan"), mon, moff)
    want, _ = hardware.fan_should_run(
        dict(cfg, fan_humidity_on=0), now.replace(hour=21, minute=0), fon, foff
    )
    fan_set = []
    real_fan, real_rf = hardware.set_fan, readings_mod.reading_filtered
    hardware.set_fan = lambda speed, why: fan_set.append((speed > 0, why))
    readings_mod.reading_filtered = lambda key, snap=None: (None, None)
    try:
        # the control loop's fan step, at 21:00 and at noon, given the main light's window
        fan_cfg = dict(cfg, fan_mode="auto", fan_humidity_on=0, fan_with_light=True)
        control._fan(fan_cfg, now.replace(hour=21, minute=0), mon, moff)
        control._fan(fan_cfg, now.replace(hour=12, minute=0), mon, moff)
    finally:
        hardware.set_fan, readings_mod.reading_filtered = real_fan, real_rf
    assert want and fan_set == [(True, "photoperiod"), (False, "idle")], (
        f"the fan runs on its setup's light hours (21:00 under a 20:00-02:00 panel) ({fan_set})"
    )
    st = {x["id"]: x for x in c.get("/api/status").get_json()["setups"]}
    assert (
        st["seedlings"]["on"]
        and datetime.fromisoformat(st["seedlings"]["on"]).hour == 20
        and st["seedlings"]["camera"]
        and st["seedlings"]["fan"]
    ), "the status gives each setup its own light window and its fan and camera"
    calls = {"n": 0}  # writes the photo makes; the schedule's own are not counted
    real_sb, real_usb = light_mod.set_brightness, camera_mod._usb_capture

    def count(*a, **k):
        if light_mod.holder() == camera_mod.HOLD_PHOTO:
            calls["n"] += 1

    light_mod.set_brightness = count
    camera_mod._usb_capture = lambda cfg_, out, w, h, warmup=None: (False, "test")
    try:
        with config.settings_lock:
            config.settings["camera_backend"] = "usb"
            config.settings["capture_set_light"] = True  # the bump is opt-in
        camera_mod.take_photo(dict(config.settings), now)
        n_second = calls["n"]
        c.post(
            "/api/settings", json={"setups": [dict(base[0], fan=True), dict(base[1], camera=True)]}
        )
        camera_mod.take_photo(dict(config.settings), now)
        n_main = calls["n"] - n_second
    finally:
        light_mod.set_brightness, camera_mod._usb_capture = real_sb, real_usb
        with config.settings_lock:
            config.settings["capture_set_light"] = False
    assert n_second == 0 and n_main == 1, (
        "the capture brightness bump only touches the main light when the camera watches it"
    )
    lm = monitor.gather_report_data()["light_metrics"]
    assert lm["dli_target"] == [15.0, 20.0] and lm["photo_setup"] == "Transplants", (
        f"the AI report judges the camera's setup ({lm.get('photo_setup')}, {lm.get('dli_target')})"
    )
    js = page_js()
    assert "camelsewhere" in js and "fanelsewhere" in js and 'data-flag="fan"' in js, (
        "the camera cards and fan controls show only on their setup's tab"
    )
    c.post("/api/settings", json={"setups": []})


def test_shared_sensors():
    """One air sensor between two close areas counts for both setups; the heat
    mat belongs to one setup like the fan."""
    with config.settings_lock:
        trays = sorted(config.settings.get("trays") or {})
        saved = {k: config.settings.get(k) for k in ("setups", "plug_use", "heat_mode")}
    t1, t2 = trays[0], trays[1]
    tran = {
        "name": "Transplants",
        "light": "main",
        "lux": "lux",
        "trays": [t2],
        "fan": True,
        "sensors": ["humidity", "temp:air", "pressure", "lux", f"float:{t1}", "reservoir:low"],
        "dli_low": 12,
        "dli_high": 15,
    }
    seed = {
        "name": "Seedlings",
        "light": "second",
        "lux": "",
        "trays": [t1],
        "heat": True,
        "sensors": ["humidity", "temp:air", "pressure", "temp:soil"],
        "dli_low": 8,
        "dli_high": 10,
    }
    try:
        r = c.post("/api/settings", json={"setups": [tran, seed]}).get_json()
        with config.settings_lock:
            sts = {x["name"]: x for x in config.settings["setups"]}
        assert (
            r["ok"]
            and sts["Transplants"]["sensors"] == ["humidity", "pressure", "temp:air"]
            and "humidity" in sts["Seedlings"]["sensors"]
        ), (
            "a sensor may be ticked in two setups; a tray's, the light sensor's and the "
            "reservoir's keys are dropped from sensor lists (they are placed elsewhere)"
        )
        with config.settings_lock:
            cfg = dict(config.settings)
        both = [x["name"] for x in setups_mod.sensor_setups(cfg, "humidity")]
        assert (
            both == ["Transplants", "Seedlings"]
            and [x["name"] for x in setups_mod.sensor_setups(cfg, "temp:soil")] == ["Seedlings"]
            and [x["name"] for x in setups_mod.sensor_setups(cfg, f"probe:{t1}")] == ["Seedlings"]
        ), "a shared sensor counts for every setup that ticks it; tray readings follow the tray"
        r2 = c.post("/api/settings", json={"setups": [dict(tran, heat=True), seed]}).get_json()
        st = {x["name"]: x for x in c.get("/api/status").get_json()["setups"]}
        assert not r2["ok"] and st["Seedlings"]["heat"] and not st["Transplants"]["heat"], (
            "the heat mat belongs to one setup, like the fan and camera"
        )
        db.log_many([("humidity", 55.0), ("temp:air", 24.0), ("temp:soil", 29.0)])
        with config.settings_lock:
            config.settings.update(plug_use="heat", heat_mode="auto")
        ctx = ai_report.build_context(monitor.gather_report_data())
        blk = {}
        cur = None
        for line in ctx.splitlines():
            m = re.match(r'Setup "(.+?)"', line)
            if m:
                cur = m.group(1)
                blk[cur] = ""
            elif cur and line.startswith("  "):
                blk[cur] += line + "\n"
            else:
                cur = None
        assert (
            "RH 55% (same sensor as Seedlings)" in blk.get("Transplants", "")
            and "RH 55% (same sensor as Transplants)" in blk.get("Seedlings", "")
            and "Heat mat" in blk.get("Seedlings", "")
            and "Heat mat" not in blk.get("Transplants", "")
            and not any(ln.startswith("Heat mat") for ln in ctx.splitlines())
        ), (
            "the AI report lists a shared sensor under both setups, says it is one sensor, "
            "and puts the heat mat under its own setup"
        )
    finally:
        c.post("/api/settings", json={"setups": saved["setups"] or []})
        with config.settings_lock:
            config.settings.update(plug_use=saved["plug_use"], heat_mode=saved["heat_mode"])
    js = page_js()
    css = Code((APP / "static" / "style.css").read_text())
    assert (
        "const keys=all.filter(k=>!/^(probe|canopy|float|reservoir):|^lux(:|$)/.test(k));" in js
        and 'data-flag="heat"' in js
        and "heatelsewhere" in js
        and "body.heatelsewhere #heatrow{display:none !important}" in css
    ), (
        "the Setups editor offers only sensors that need placing, has a Heat mat box, and the "
        "heat mat controls show on their setup's tab"
    )
