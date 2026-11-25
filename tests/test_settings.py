"""Tests: settings."""

import json
import pytest
import re
import threading
import time
import types
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from harness import (
    APP,
    WORK,
    c,
    camera_mod,
    config,
    hardware,
    js_after,
    js_fn,
    page_js,
    sensors,
    water,
)


def _refuses(fn, value):
    try:
        fn(value)
        return False
    except Exception:
        return True


def test_unsaved_settings():
    """An edited setting is not overwritten by the next status while it is
    unsaved, whether or not its field still has focus."""
    js = page_js()
    fh = js_fn(js, "formHolds")
    ff = js_fn(js, "fillForm")
    sub2 = js_after(js, "getElementById('cfgform').addEventListener('submit'")
    assert (
        sub2
        and "if(setupDirty&&setupDraft){body.setups=setupDraft" in sub2.group(0)
        and "body.tray_sensors=trayDraft" in sub2.group(0)
        and ("setupDirty=false;" in sub2.group(0) or "set_setupDirty(false);" in sub2.group(0))
        and "if(k==='setups')return 'Setups:';" in js
    ), "the main Save also saves pending Setups edits"
    assert (
        "f.addEventListener('input',markDirty);f.addEventListener('change',markDirty);" in js
        and fh
        and "if(formDirty.has(key))return true;" in fh.group(0)
    ), "a changed setting is marked unsaved and kept over any incoming status"
    fill = js_fn(js, "fillField")
    assert (
        ff
        and "document.activeElement" not in ff.group(0)
        and "for(const k of Object.keys(FORM))fillField(f,k,FORM[k],cfg);" in ff.group(0)
        and fill
        and "formHolds(k,cfg)" in fill.group(0)
    ), "every settings field is repainted only through formHolds, not a focus check"
    sub = js_after(js, "getElementById('cfgform').addEventListener('submit'")
    assert sub and "if(!(j.errors&&k in j.errors))formDirty.delete(k);" in sub.group(0), (
        "saving clears the unsaved mark for accepted fields; a rejected field keeps what was typed"
    )
    assert (
        "formDirty.add('usb_width');formDirty.add('usb_height');" in js
        and "formDirty.add('kasa_host');" in js
    ), "fields filled in by the camera-mode and plug pickers count as unsaved edits too"


def test_config_safety():
    """Config file safety, capture error handling and input checks."""
    # 1. config.json: atomic writes, a backup, and no silent fall back to defaults
    tmpd = WORK / "cfgtest"
    tmpd.mkdir(exist_ok=True)
    f = tmpd / "x.json"
    config.atomic_write_text(f, '{"a": 1}')
    assert json.loads(f.read_text()) == {"a": 1} and not (tmpd / "x.json.tmp").exists(), (
        "settings files are written whole (temp file, fsync, rename)"
    )
    src = (APP / "config.py").read_text()
    block = src[src.index("if CONFIG_PATH.exists():") : src.index("def save_config():")]

    def load(main, bak):
        cp, cb = tmpd / "config.json", tmpd / "config.json.bak"
        for pth, txt in ((cp, main), (cb, bak)):
            if txt is None:
                pth.unlink(missing_ok=True)
            else:
                pth.write_text(txt)
        ns = {
            "CONFIG_PATH": cp,
            "CONFIG_BAK": cb,
            "settings": {},
            "_file_keys": set(),
            "log": types.SimpleNamespace(error=lambda *a: None, warning=lambda *a: None),
            "_load_json": config._load_json,
            "config_broken": "",
        }
        exec(block, ns)
        return ns["settings"], ns["config_broken"]

    good, _ = load('{"password_hash": "x", "light_on": "07:00"}', None)
    fell_back, broken1 = load('{"password_hash": "x", "light_on"', '{"password_hash": "y"}')
    none, broken2 = load("", "{not json")
    assert (
        good.get("password_hash") == "x"
        and fell_back.get("password_hash") == "y"
        and not broken1
        and none == {}
        and broken2
    ), (
        "a truncated config.json falls back to config.json.bak; with neither readable the "
        "app says so instead of quietly running on defaults"
    )
    was = config.config_broken
    try:
        config.config_broken = "config.json could not be read (test)"
        cfg_before = (APP / "config.json").read_text() if (APP / "config.json").exists() else None
        with config.settings_lock:
            config.save_config()
        cfg_after = (APP / "config.json").read_text() if (APP / "config.json").exists() else None
        r = c.post("/api/settings", json={"light_on": "08:00"})
    finally:
        config.config_broken = was
    assert cfg_before == cfg_after and r.status_code == 503, (
        "while the settings are unreadable nothing is saved over them and changes are refused "
        "(the defaults have no password)"
    )
    # 2. pump time limits on a clock that cannot jump
    floats = sensors._floats()
    if "1" not in floats:  # the watering checks removed it
        sensors._float_init = False
        sensors._float_devs.clear()
        floats = sensors._floats()
    for st in hardware.pump_state.values():
        st.update(running=False, today_seconds=0, day=config._today_str())
    with config.settings_lock:
        saved_cap = config.settings.get("fill_max_seconds")
        config.settings["fill_max_seconds"] = 1
    floats["1"].is_pressed = True  # never full: the cap must stop it
    real_res, real_time = water.reservoir_state, time.time
    water.reservoir_state = lambda: "ok"
    start = real_time()
    jumped = {"n": 0}

    def stepped_clock():  # NTP steps the clock back an hour
        jumped["n"] += 1
        return real_time() - (3600 if jumped["n"] > 2 else 0)

    out = {}
    time.time = stepped_clock
    try:
        th = threading.Thread(target=lambda: out.update(r=water.run_pump_until_full("1", "auto")))
        th.start()
        th.join(6)
    finally:
        time.time = real_time
        water.reservoir_state = real_res
        with config.settings_lock:
            config.settings["fill_max_seconds"] = saved_cap
    took = real_time() - start
    assert not th.is_alive() and took < 3 and not hardware._pumps["1"].value, (
        f"a fill's time cap holds even if the wall clock steps back an hour ({took:.1f}s)"
    )
    # 3. a full SD card: photos give way, and it is alerted
    shots = []
    real_free, real_tp = camera_mod.disk_free_gb, camera_mod.take_photo
    camera_mod.disk_free_gb = lambda path=None: 0.4
    camera_mod.take_photo = lambda cfg_, now_: shots.append(now_) or None
    with config.settings_lock:
        saved_cam = {k: config.settings.get(k) for k in ("camera_enabled", "capture_enabled")}
        config.settings.update(camera_enabled=True, capture_enabled=True)
    real_win = camera_mod.capture_window
    tzz = ZoneInfo(config.settings["timezone"])
    camera_mod.capture_window = lambda cfg_: (
        datetime.now(tzz) - timedelta(hours=1),
        datetime.now(tzz) + timedelta(hours=1),
    )
    try:
        ls = camera_mod._capture_tick(None)
    finally:
        camera_mod.disk_free_gb, camera_mod.take_photo = real_free, real_tp
        camera_mod.capture_window = real_win
        with config.settings_lock:
            config.settings.update(saved_cam)
    import alerts

    alerts.reset()
    fired = [
        a
        for a in alerts.check_all(
            {"_disk_low": "0.4 GB free on the SD card"}, dict(alerts.DEFAULTS, sustain_seconds=0)
        )
        if a[1] == "disk_low"
    ]
    alerts.reset()
    assert shots == [] and ls is not None and fired and fired[0][0] == "fire", (
        "with under 1 GB free photos are skipped (the database and settings keep working), "
        "and an alert goes out below 3 GB"
    )
    loop = re.search(r"def capture_loop\(\):[\s\S]*?\n\n\n", (APP / "camera.py").read_text())
    assert (
        loop
        and "last_shot = _capture_tick(last_shot)" in loop.group(0)
        and 'log.exception("capture loop error")' in loop.group(0)
    ), "one failed capture tick is logged and the timelapse carries on"

    # 4. a photo is the last whole frame, written whole
    def frame(n):
        return b"\xff\xd8" + bytes([n]) * 50 + b"\xff\xd9"

    stream = frame(1) + frame(2) + b"\xff\xd8" + b"\x03" * 30  # last frame cut short
    dev = WORK / "video9"
    dev.write_bytes(b"")
    real_run = camera_mod.subprocess.run

    def fake_v4l2(a, **kw):
        Path([x for x in a if str(x).startswith("--stream-to=")][0].split("=", 1)[1]).write_bytes(
            stream
        )
        return types.SimpleNamespace(returncode=0, stderr=b"", stdout=b"")

    camera_mod.subprocess.run = fake_v4l2
    real_ctl = camera_mod._usb_apply_controls
    camera_mod._usb_apply_controls = lambda cfg_, dev_: None
    outp = WORK / "shot.jpg"
    try:
        ok, err = camera_mod._usb_capture({"usb_device": str(dev)}, outp, 640, 480)
    finally:
        camera_mod.subprocess.run = real_run
        camera_mod._usb_apply_controls = real_ctl
    assert ok and outp.read_bytes() == frame(2) and not (WORK / "shot.jpg.part").exists(), (
        "a capture keeps the last complete frame, never a truncated one, and writes it whole"
    )
    # 5. big photos decoded at a reduced scale where full size is not needed
    try:
        import cv2
        import numpy as np
    except Exception:
        pytest.skip("reduced-scale decode (OpenCV not installed here)")
    big = WORK / "big.jpg"
    cv2.imwrite(str(big), np.zeros((2448, 3264, 3), np.uint8))
    import growth

    assert (
        growth.jpeg_size(big) == (3264, 2448)
        and growth.imread_min(cv2, big, 1000).shape[:2] == (1224, 1632)
        and growth.imread_min(cv2, big, 3000).shape[:2] == (2448, 3264)
    ), "an 8-megapixel photo needed at 1000 px is decoded at half size (a quarter of the memory)"


def test_loaded_settings():
    """Hand-edited settings are checked at start-up, and numbers must be finite."""
    loaded = {
        "ramp_min": "30",  # a number in quotes
        "schedule_mode": "lunar",  # no such mode
        "max_bright": 400,  # out of range
        "fixed_on": "25:99",
        "light_linear_on": "false",
        "moisture_threshold_pct": None,  # blank: left alone
        "trays": {"1": {"cells": {}}},  # not a form field: left alone
    }
    untouched = {k: loaded[k] for k in ("moisture_threshold_pct", "trays")}
    bad = config.check_loaded(loaded, set(loaded))
    assert (
        loaded["ramp_min"] == 30
        and loaded["light_linear_on"] is False
        and bad == ["fixed_on", "max_bright", "schedule_mode"]
        and all(loaded[k] == config.DEFAULTS[k] for k in bad)
        and all(loaded[k] == v for k, v in untouched.items())
    ), (
        f"a loaded setting of the wrong type is coerced, an invalid one falls back to its "
        f"default, and everything else is left as it was ({bad})"
    )
    refused = []
    for key, value in (
        ("light_floor_pct", "nan"),
        ("light_floor_pct", float("inf")),
        ("ramp_min", "1e999"),
        ("screen_touch_cal", {"m": [1, 0, 0, 0, float("nan"), 0]}),
    ):
        try:
            config.SETTINGS_VALIDATORS[key](value)
        except ValueError:
            refused.append(key)
    assert len(refused) == 4, (
        f"NaN and infinity are refused wherever a number is expected ({len(refused)} of 4)"
    )


def test_form_validity():
    """Save is never blocked by the browser's own validation of a field in a
    closed section; the server validates instead."""
    html = c.get("/").get_data(as_text=True)
    assert re.search(r'<form id="cfgform"[^>]*\bnovalidate\b', html) is not None, (
        "the settings form leaves validation to the server, so a hidden field can never "
        "silently block Save"
    )
    bad = []
    for m in re.finditer(r'<input name="(\w+)" type="number"([^>]*)>', html):
        name, attrs = m.group(1), m.group(2)
        st = re.search(r'step="([^"]+)"', attrs)
        if name not in config.DEFAULTS or not st or st.group(1) == "any":
            continue
        step = float(st.group(1))
        v = config.DEFAULTS[name]
        if isinstance(v, (int, float)) and abs(v / step - round(v / step)) > 1e-9:
            bad.append(f"{name}={v} step {step}")
    assert (
        not bad
        and 'name="video_fps" type="number" min="1" max="60" step="any"' in html
        and len(re.findall(r'<input name="(\w+)" type="number"', html)) >= 40
    ), f"every default fits its field's step, and the speeds take decimals ({bad or 'ok'})"


def test_settings_layout():
    """Settings has eight groups, and every field is in exactly one of them."""
    html = c.get("/").get_data(as_text=True)
    form = html[
        html.index('<form id="cfgform"') : html.index("</form>", html.index('<form id="cfgform"'))
    ]
    titles = re.findall(r"<summary><h3>(.*?)</h3></summary>", form)
    subs = re.findall(r'<h4 class="fsub">(.*?)</h4>', form)
    names = set(re.findall(r'name="(\w+)"', form))
    must = {
        "light_backend",
        "schedule_mode",
        "light2_start",
        "lux_to_ppfd_k",
        "heat_target_f",
        "fan_min_speed",
        "soil_temp_low_f",
        "moisture_threshold_pct",
        "probe_median_depth",
        "camera_backend",
        "alerts_enabled",
        "latitude",
        "plug_use",
        "units",
    }

    def sec(t):
        return form[form.index(f"<summary><h3>{t}</h3>") :]

    assert (
        titles
        == [
            "Light",
            "Climate",
            "Watering",
            "Camera",
            "External display",
            "Setups",
            "Trays",
            "Alerts",
            "System",
        ]
        and {
            "Fixture",
            "Schedule",
            "Second light",
            "Light metrics",
            "Heat mat",
            "Fan",
            "Target bands",
            "Location",
            "Smart plug",
            "Display",
            "Backup",
        }
        <= set(subs)
        and must <= names
        and len(re.findall(r'name="(\w+)"', form)) >= 78
        and 'name="probe_median_depth"' in sec("Watering").split("</details>")[0]
        and 'name="probe_median_depth"' not in sec("Alerts").split("</details>")[0]
    ), (
        f"settings are in nine sections with subheadings, every field still present "
        f"({len(names)} fields), sensor smoothing now under Watering"
    )


def test_form_definitions():
    """config.FORM is the single definition of each Settings field: the form,
    the server and the page's save and fill code all come from it."""
    html = c.get("/").get_data(as_text=True)
    form = html[
        html.index('<form id="cfgform"') : html.index("</form>", html.index('<form id="cfgform"'))
    ]
    names = re.findall(r'\bname="(\w+)"', form)
    fk = set(config.FORM)
    missing = sorted(fk - set(names))
    extra = sorted(set(names) - fk)
    dup = sorted({n for n in names if names.count(n) > 1})
    nodefault = sorted(k for k in fk if k not in config.DEFAULTS)
    assert not missing and not extra and not dup and not nodefault, (
        f"every field on the Settings form is defined once in config.FORM and has a default "
        f"(missing {missing}, undefined {extra}, twice {dup}, no default {nodefault})"
    )
    wrong = []
    for m in re.finditer(r'<input name="(\w+)" type="number" ([^>]*)>', form):
        k, attrs = m.group(1), m.group(2)
        sp = config.FORM[k]
        if sp["kind"] in ("int", "float"):
            if f'min="{sp["min"]}"' not in attrs or f'max="{sp["max"]}"' not in attrs:
                wrong.append(k)
    v = config.SETTINGS_VALIDATORS
    accepts = all(
        v[k](config.DEFAULTS[k]) is not None or config.DEFAULTS[k] in ("", None)
        for k in fk
        if config.FORM[k]["kind"] not in ("text", "secret")
    )
    refuses = [
        k
        for k, sp in config.FORM.items()
        if sp["kind"] in ("int", "float")
        and sp.get("clamp") is False
        and _refuses(v[k], sp["max"] + 1)
    ]
    assert (
        not wrong
        and accepts
        and len(refuses)
        == sum(
            1
            for sp in config.FORM.values()
            if sp["kind"] in ("int", "float") and sp.get("clamp") is False
        )
    ), f"the form offers exactly the range the server accepts (mismatched {wrong})"
    spec = json.loads(
        re.search(
            r'<script id="formspec" type="application/json">(.*?)</script>', html, re.S
        ).group(1)
    )
    assert set(spec) == fk and all(spec[k]["default"] == config.DEFAULTS[k] for k in fk), (
        "the page gets config.FORM, with defaults, for its save and fill code"
    )


def test_version():
    """The version in pyproject.toml is the one that heads the changelog."""
    ver = re.search(r'^version = "(\w+)"', (APP / "pyproject.toml").read_text(), re.M)
    top = re.search(r"^## (\w+) \(", (APP / "CHANGELOG.md").read_text(), re.M)
    assert ver and top and ver.group(1) == top.group(1), (
        f"pyproject.toml and CHANGELOG.md agree on the version ({ver and ver.group(1)})"
    )
