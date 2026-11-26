"""Tests: dashboard."""

import json
import re
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

from harness import (
    photos_mod,
    tools,
    APP,
    Code,
    WORK,
    app_source,
    c,
    camera_mod,
    code_has,
    config,
    js_fn,
    monitor,
    note,
    page_js,
    sensors,
    status_mod,
)


def test_charts():
    """The chart grid: labelled axes, lights-off shading, one crosshair across
    every chart, and the heat mat's power where it belongs."""
    js = page_js()
    html = (APP / "templates" / "index.html").read_text()
    css = Code((APP / "static" / "style.css").read_text())
    dm = js_fn(js, "drawMini")
    body = dm.group(0) if dm else ""
    assert (
        "h+=nightBands(x0,x1,sx,P,H-B);" in body
        and "niceTicks(y0,y1,big?5:3)" in body
        and "h+=timeTicks(x0,x1,sx,P,H-B,W,FS,big);" in body
        and 'class="cyax"' in body
        and 'stroke="#e6f0de"' not in body
    ), (
        "every chart has value labels at round numbers, time labels at round hours, and "
        "themed gridlines instead of bright white ones"
    )
    assert (
        "match:k=>k.startsWith('temp:soil')||k.startsWith('probe:')||k.startsWith('heat:')" in js
        and "s.startsWith('canopy:')||s.startsWith('heat:'))return '%';" in js
        and "const stepped=key.startsWith('heat:');" in body
    ), (
        "Heat mat power sits with Soil, in percent on a 0-100 scale, drawn as steps (one "
        "level per 15-minute window)"
    )
    mv = js_fn(js, "chartMove")
    assert (
        mv
        and "for(const k of Object.keys(chartPlots))" in mv.group(0)
        and "showCross(k, best.t, k===key);" in mv.group(0)
        and "function showCross(k, t, own)" in js
    ), "hovering one chart shows the same moment on every chart"
    assert (
        "chartRO.observe(s)" in js
        and "new ResizeObserver(" in js
        and "if(r.width<10||r.height<10)return;" in body
        and "Math.round(r.width)||320" not in body
    ), (
        "a chart is drawn at its real on-screen size and redrawn when that changes, never "
        "at a fallback size stretched to fit"
    )
    lay = js_fn(js, "layoutChartRows")
    assert (
        lay
        and "const rows=Math.ceil(n/cmax), cols=Math.ceil(n/rows);" in lay.group(0)
        and "#chartgrid .cgrid,#devcharts .cgrid{display:flex;flex-wrap:wrap;gap:10px}" in css
        and "var(--cols,3)" in css
        and "rowRO.observe(grid)" in js
        and 'class="cgl"' in js
        and "svg.cmini .cgl{" in css
    ), (
        "chart rows fill the width with equal-size charts, balanced (4 across 3 slots go 2+2), "
        "recomputed on resize; the SVG gridline class does not collide with the grid container"
    )
    assert (
        'data-h="6"' in html
        and 'data-h="72"' in html
        and "nightkey" in html
        and "svg.cmini .cnight" in css
    ), "6-hour and 3-day ranges, and a key for the lights-off shading"


def test_phone_layout():
    """On a phone the planting map scrolls inside its card and the page stays
    the width of the screen."""
    js = page_js()
    css = Code((APP / "static" / "style.css").read_text())
    assert (
        '<div class="tscroll"><div class="tgrid" style="--tcols:${cols}">' in js
        and "h+='</div></div></div>';" in js
        and ".tscroll{overflow-x:auto" in css
        and "grid-template-columns:repeat(var(--tcols,4),minmax(132px,1fr))" in css
        and ".tdlbl{width:" not in css
    ), (
        "on a phone a tray scrolls sideways inside its card at a readable cell width, and the "
        "page stays the width of the screen"
    )
    assert "filter(v=>v&&(v.seed||v.equipment||v.planted||v.sprouted||v.archived)).length" in js, (
        "a cleared cell does not count as filled in the tray's count"
    )


def test_page_modules():
    """The dashboard's scripts are ES modules with explicit imports, entered
    through static/js/main.js."""
    page = c.get("/").get_data(as_text=True)
    jsdir = APP / "static" / "js"
    # store.js is imported by the others and has nothing to start
    mods = sorted(p.name for p in jsdir.glob("*.js") if p.name not in ("main.js", "store.js"))
    main = (jsdir / "main.js").read_text()
    imported = re.findall(r"from '\./(\w+\.js)'", main)
    no_export = [m for m in mods if "export " not in (jsdir / m).read_text()]
    assert (
        re.search(r'<script type="module" src="[^"]*js/main\.js', page) is not None
        and sorted(imported) == mods
        and not no_export
        and "if (typeof m.start === 'function') m.start();" in main
    ), (
        f"the page loads one module, main.js, which imports every dashboard module and "
        f"starts them in order ({len(mods)} modules)"
    )
    r = c.get("/static/js/main.js")
    assert r.headers.get("Cache-Control") == "no-cache", (
        "the dashboard's modules are revalidated on every load, so an update is never "
        "half-applied in a browser"
    )
    cfg = (APP / "eslint.config.mjs").read_text()
    pkg = json.loads((APP / "package.json").read_text())
    assert 'sourceType: "module"' in cfg and "eslint" in pkg.get("devDependencies", {}), (
        "ESLint is configured for the modules (npm install && npx eslint static)"
    )


def test_clean_start():
    """A clean start logs no false alarms, and two thumbnail jobs never collide."""
    import logging

    # 1. "lux sensor not found; disabled" logged just before "found"
    seen = []

    class H(logging.Handler):
        def emit(self, rec):
            seen.append(rec.getMessage())

    h = H()
    sensors.log.addHandler(h)
    opened = {"n": 0}

    class FakeBH:
        def __init__(self, i2c, address):
            if address != 0x23:
                raise OSError("no device")
            opened["n"] += 1
            self.first = True

        @property
        def lux(self):
            if self.first:
                self.first = False
                time.sleep(0.3)  # a slow first read, as on the Pi
            return 100.0

    saved_mod, saved_i2c = sys.modules.get("adafruit_bh1750"), sensors._i2c
    saved_state = {a: dict(st) for a, st in sensors._lux.items()}
    saved_en = sensors.ENABLED["lux"]
    sys.modules["adafruit_bh1750"] = types.SimpleNamespace(BH1750=FakeBH)
    sensors._i2c = lambda: object()
    sensors.ENABLED["lux"] = True
    for st in sensors._lux.values():
        st.update(dev=None, init=False, fail=0, seen=False)
    if hasattr(sensors._read_lux, "_warned"):
        del sensors._read_lux._warned
    got = []
    try:
        th = [threading.Thread(target=lambda: got.append(sensors._read_lux())) for _ in range(2)]
        for t in th:
            t.start()
        for t in th:
            t.join(5)
    finally:
        sys.modules["adafruit_bh1750"], sensors._i2c = saved_mod, saved_i2c
        sensors.ENABLED["lux"] = saved_en
        for a, st in saved_state.items():
            sensors._lux[a].update(st)
        sensors.log.removeHandler(h)
    assert (
        not any("not found" in m for m in seen)
        and opened["n"] == 1
        and all(g.get("lux") == 100.0 for g in got)
        and len(got) == 2
    ), "two loops reading the light sensor at start never log it missing while it is being opened"
    # 2. the RuntimeWarning about I2C frequency
    args = {}

    class FakeExt:
        def __init__(self, bus, frequency=400000):
            args["frequency"] = frequency

    saved_ext, saved_bus = sys.modules.get("adafruit_extended_bus"), sensors._i2c_bus
    sys.modules["adafruit_extended_bus"] = types.SimpleNamespace(ExtendedI2C=FakeExt)
    sensors._i2c_bus = None
    try:
        sensors._i2c()
    finally:
        sys.modules["adafruit_extended_bus"], sensors._i2c_bus = saved_ext, saved_bus
    assert "frequency" in args and args["frequency"] is None, (
        f"the I2C bus is opened without a frequency the library ignores and warns about ({args})"
    )
    # 3. two ffmpeg runs thumbnailing the same new photo at once
    name = "20260925_160943_m.jpg"
    photo = photos_mod.TIMELAPSE_DIR / name
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    dst = photos_mod.THUMB_DIR / name
    dst.unlink(missing_ok=True)
    runs = []
    real_run = camera_mod.subprocess.run

    def fake_run(a, **kw):
        runs.append(list(a))
        time.sleep(0.3)
        Path(a[-1]).write_bytes(b"\xff\xd8thumb\xff\xd9")
        return types.SimpleNamespace(returncode=0, stderr=b"", stdout=b"")

    with config.settings_lock:
        cfgt = dict(config.settings, timelapse_flatten=False, roi="")
    camera_mod.subprocess.run = fake_run
    try:
        th = [threading.Thread(target=photos_mod.make_thumb, args=(photo, cfgt)) for _ in range(2)]
        for t in th:
            t.start()
        for t in th:
            t.join(5)
    finally:
        camera_mod.subprocess.run = real_run
    parts = list(photos_mod.THUMB_DIR.glob("*.part"))
    assert len(runs) == 1 and dst.exists() and not parts and runs[0][-1].endswith(".part"), (
        f"a new photo is thumbnailed once, written aside and renamed ({len(runs)} runs, {len(parts)} leftovers)"
    )
    dst.unlink(missing_ok=True)
    camera_mod.subprocess.run = lambda a, **kw: types.SimpleNamespace(
        returncode=1, stderr=b"bad", stdout=b""
    )
    try:
        photos_mod.make_thumb(photo, cfgt)
    finally:
        camera_mod.subprocess.run = real_run
    assert not dst.exists() and not list(photos_mod.THUMB_DIR.glob("*.part")), (
        "a failed thumbnail leaves nothing behind, so it is tried again"
    )
    tick = re.search(r"def _capture_tick\([\s\S]*?\n(?=def )", app_source())
    assert tick and code_has(
        tick.group(0), "None if capturing else", "st_mtime > 10", "make_thumb(missing)"
    ), "the backfill leaves a photo alone while it is being captured or is under 10 s old"
    photo.unlink(missing_ok=True)


def test_memory_and_status():
    """OpenCV stays out of the controller, memory is charted, a polled status
    is built once for all readers, and the touchscreen gets a lite status."""
    # 1. no OpenCV in the controller after every image path has been used
    import sys as _sys

    for path in ("/rectified.jpg", "/photo/cropped.jpg"):
        c.get(path)
    loaded = [m for m in ("cv2", "numpy") if m in _sys.modules]
    srcs = {
        f: (APP / f).read_text()
        for f in (
            "camera.py",
            "photos.py",
            "video.py",
            "tools.py",
            "routes.py",
            "routes_camera.py",
            "routes_garden.py",
            "routes_climate.py",
            "routes_data.py",
            "ai_report.py",
            "monitor.py",
            "status.py",
            "light.py",
            "control.py",
            "water.py",
            "heat.py",
        )
    }
    no_import = [f for f, t in srcs.items() if "import cv2" in t or "import numpy" in t]
    assert not no_import and tools.IMGTOOL.name == "imgtool.py" and (APP / "imgtool.py").exists(), (
        f"no controller module imports OpenCV or NumPy; image work runs in imgtool.py "
        f"(importers: {no_import or 'none'}; already loaded here by the test harness: {loaded})"
    )
    try:
        import cv2
        import numpy as np
    except Exception:
        note("imgtool round trip (OpenCV not installed here)")
        cv2 = None
    if cv2 is not None:
        src = WORK / "it.jpg"
        img = np.zeros((600, 800, 3), np.uint8)
        img[:, 400:] = (0, 200, 0)
        cv2.imwrite(str(src), img)
        r1 = tools.imgtool(["crop", src, "-", "[0.5, 0, 0.5, 1]", "--q", 90])
        dec = (
            cv2.imdecode(np.frombuffer(r1.stdout, np.uint8), cv2.IMREAD_COLOR)
            if r1.stdout
            else None
        )
        r2 = tools.imgtool(
            [
                "rectify",
                src,
                WORK / "it_r.jpg",
                "[[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]]",
                4,
                3,
                "--max-w",
                200,
            ]
        )
        rr = cv2.imread(str(WORK / "it_r.jpg"))
        r3 = tools.imgtool(["sharpness", src])
        r4 = tools.imgtool(["rotate", src, 90])
        rot = cv2.imread(str(src))
        assert (
            r1.returncode == 0
            and dec is not None
            and dec.shape[:2] == (600, 400)
            and int(dec[:, :, 1].mean()) > 150
            and r2.returncode == 0
            and rr is not None
            and max(rr.shape[:2]) == 200
            and r3.returncode == 0
            and float(r3.stdout) >= 0
            and r4.returncode == 0
            and rot.shape[:2] == (800, 600)
        ), "imgtool crops, flattens and resizes, scores sharpness and rotates in place"
    # 2. memory readings
    mem = monitor.memory_readings()
    js = page_js()
    assert (
        {"sys:mem_free", "sys:app_mem"} <= set(mem)
        and mem["sys:mem_free"] > 0
        and mem["sys:app_mem"] > 0
        and all(v >= 0 for v in mem.values())  # swap in use can be zero
        and "{id:'device', title:'Device',           match:k=>k.startsWith('sys:')}" in js
        and "if(s.startsWith('sys:'))return 'MB';" in js
    ), f"memory is logged each sample and charted under Device ({mem})"
    # 4. the polled status is built once between changes, and a change is seen at once
    calls = []
    real = status_mod.status_payload
    status_mod.status_payload = lambda authed=None: calls.append(1) or real(authed)
    status_mod.POLL_REUSE_S = 5.0
    try:
        # the controller's own loops also announce changes now and then; one
        # landing mid-run is a real rebuild, so a run gets three tries
        for _attempt in range(3):
            calls.clear()
            status_mod._poll_cache.clear()
            for _ in range(3):
                c.get("/api/status")
            built_before = len(calls)
            c.post("/api/fan", json={"mode": "auto"})
            c.get("/api/status")
            built_after = len(calls)
            if (built_before, built_after) == (1, 2):
                break
    finally:
        status_mod.status_payload = real
        status_mod.POLL_REUSE_S = 0.0
    assert built_before == 1 and built_after == 2, (
        f"three polls between changes build the status once; a change rebuilds it "
        f"({built_before} then {built_after})"
    )
    # 5. the touchscreen's lite status
    full = c.get("/api/status").get_data()
    litej = c.get("/api/status?lite=1").get_json()
    lit = c.get("/api/status?lite=1").get_data()
    sjs = Code((APP / "static" / "screen.js").read_text())
    # 6. the system log is capped
    sh = (APP / "scripts" / "setup.sh").read_text()
    jc = (APP / "deploy" / "journald.conf").read_text()
    assert (
        "step_journal" in sh
        and "SystemMaxUse=50M" in jc
        and "SystemKeepFree=1G" in jc
        and subprocess.run(["bash", "-n", str(APP / "scripts" / "setup.sh")]).returncode == 0
    ), "setup.sh caps the system log (50 MB, never within 1 GB of full)"
    # 7. archived runs make room, oldest first, never the current run
    arch = WORK / "archive_test"
    shutil.rmtree(arch, ignore_errors=True)
    for name in ("20260801_090000", "20260901_090000", "20260920_090000"):
        (arch / name).mkdir(parents=True)
        (arch / name / "a.jpg").write_bytes(b"x" * 1000)
    free = {"gb": 2.0}
    real_arch = photos_mod.ARCHIVE_DIR
    photos_mod.ARCHIVE_DIR = arch
    try:
        first = photos_mod.prune_archives(
            lambda: (
                free["gb"]
                + 1.0
                * len(
                    [p for p in ("20260801_090000", "20260901_090000") if not (arch / p).exists()]
                )
            )
        )
        left = sorted(p.name for p in arch.iterdir())
        free["gb"] = 5.0
        none = photos_mod.prune_archives(lambda: free["gb"])
    finally:
        photos_mod.ARCHIVE_DIR = real_arch
    assert (
        first == ["20260801_090000", "20260901_090000"]
        and left == ["20260920_090000"]
        and none == []
    ), (
        f"below 3 GB free the oldest archived runs go first until 4 GB is free, the newest "
        f"kept; plenty of space removes nothing (removed {first}, left {left})"
    )
    assert (
        len(lit) < len(full)
        and "quality" not in litej
        and "curve" not in (litej.get("day_light") or {})
        and set(litej["settings"]) - {k for k in litej["settings"] if k.startswith("screen_")}
        == {"units", "probe_cal", "trays", "little_buddy", "buddy_model"}
        and "fetch('/api/status?lite=1'" in sjs
    ), f"the touchscreen fetches a lite status ({len(lit)} bytes against {len(full)})"


def test_a_stale_page_saves_only_the_cells_it_changed():
    """Two pages have the same tray open. Each save carries the tray as the
    page had it, and only the cells that page changed are written."""

    def cell(seed):
        return {"seed": seed, "equipment": "", "planted": "2026-11-01"}

    with config.settings_lock:
        tray = next(iter(config.settings["trays"]))
    start = {"A1": cell("basil"), "B1": cell("thyme")}
    c.post("/api/trays", json={"tray": tray, "cells": start})

    # the first page changes A1, the second (still showing the start) changes B1
    c.post("/api/trays", json={"tray": tray, "base": start, "cells": {**start, "A1": cell("dill")}})
    c.post("/api/trays", json={"tray": tray, "base": start, "cells": {**start, "B1": cell("sage")}})
    with config.settings_lock:
        cells = dict(config.settings["trays"][tray]["cells"])
    assert (cells["A1"]["seed"], cells["B1"]["seed"]) == ("dill", "sage"), (
        f"the second page's save does not put the first page's cell back ({cells})"
    )

    # emptying a cell is a change like any other, and so is filling a new one
    r = c.post(
        "/api/trays",
        json={"tray": tray, "base": start, "cells": {"A1": start["A1"], "C1": cell("mint")}},
    ).get_json()
    with config.settings_lock:
        cells = dict(config.settings["trays"][tray]["cells"])
    assert r["ok"] and set(cells) == {"A1", "C1"} and cells["A1"]["seed"] == "dill", cells

    # without a base the tray becomes exactly what was sent
    c.post("/api/trays", json={"tray": tray, "cells": {"A2": cell("chive")}})
    with config.settings_lock:
        assert set(config.settings["trays"][tray]["cells"]) == {"A2"}
