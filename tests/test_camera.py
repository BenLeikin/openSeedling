"""Tests: camera."""

import ast
import json
import os
import pytest
import re
import shutil
import subprocess
import sys
import time
import types
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from harness import (
    photos_mod,
    tools,
    video_mod,
    APP,
    Code,
    WORK,
    ai_report,
    app_source,
    c,
    camera_mod,
    code_has,
    config,
    db,
    js_fn,
    monitor,
    note,
    page_js,
    squash,
)


def test_camera_flatten():
    js = page_js()
    html = (APP / "templates" / "index.html").read_text()
    assert 'name="timelapse_flatten"' in html and "timelapse_flatten===false" in js, (
        "the snapshot's flattening follows a visible setting"
    )
    assert squash(js).count("'/thumb/'+frames[") == 2 and "?v='+thumbsV" in js, (
        "scrubber thumbnail URLs carry a version, so browsers drop cached ones"
    )
    photos_mod.TIMELAPSE_DIR.mkdir(parents=True, exist_ok=True)
    photos_mod.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (photos_mod.TIMELAPSE_DIR / f"2026092{i}_120000.jpg").write_bytes(b"photo")
        (photos_mod.THUMB_DIR / f"2026092{i}_120000.jpg").write_text("flat")
    real_mt = photos_mod.make_thumb

    def fake_thumb(ph, cfg=None, dst_dir=None):
        time.sleep(0.05)
        ((dst_dir or photos_mod.THUMB_DIR) / ph.name).write_text(
            "flat" if (cfg or {}).get("timelapse_flatten", True) else "raw"
        )

    photos_mod.make_thumb = fake_thumb
    counts = []
    try:
        v0 = c.get("/api/photos").get_json().get("v", 0)
        with config.settings_lock:
            config.settings["timelapse_flatten"] = True
        c.post("/api/settings", json={"timelapse_flatten": False})
        for _ in range(40):
            counts.append(len(c.get("/api/photos").get_json()["names"]))
            if (
                not photos_mod._thumbs_lock.locked()
                and counts[-1]
                and c.get("/api/photos").get_json().get("v", 0) != v0
            ):
                break
            time.sleep(0.05)
        kinds = {p.read_text() for p in photos_mod.THUMB_DIR.glob("*.jpg")}
        v1 = c.get("/api/photos").get_json().get("v", 0)
    finally:
        photos_mod.make_thumb = real_mt
    assert kinds == {"raw"}, f"turning flattening off rebuilds the thumbnails raw ({kinds})"
    assert min(counts) == 3, (
        f"the scrubber's frame list never empties during a rebuild (min {min(counts)})"
    )
    assert v1 != v0, "the thumbnail version changes after a rebuild"


def test_camera_preview():
    seen = {}

    def fake_usb(cfg, out, w, h, warmup=None):
        seen["size"] = (w, h)
        Path(out).write_bytes(b"\xff\xd8\xff\xd9")
        return True, ""

    real = camera_mod._usb_capture
    camera_mod._usb_capture = fake_usb
    try:
        with config.settings_lock:
            config.settings.update(
                camera_enabled=True, camera_backend="usb", usb_width=2048, usb_height=1536
            )
        c.post("/api/preview", json={})
    finally:
        camera_mod._usb_capture = real
    assert seen.get("size") == (2048, 1536), (
        f"the align preview uses the photo's own camera mode ({seen.get('size')}), so it shows the same view"
    )


def test_camera_modes_and_reset():
    sample = """ioctl: VIDIOC_ENUM_FMT
\tType: Video Capture

\t[0]: 'MJPG' (Motion-JPEG, compressed)
\t\tSize: Discrete 1280x720
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\tSize: Discrete 3264x2448
\t\tSize: Discrete 2048x1536
\t[1]: 'YUYV' (YUYV 4:2:2)
\t\tSize: Discrete 4000x3000
"""
    assert camera_mod.parse_mjpeg_modes(sample) == [(3264, 2448), (2048, 1536), (1280, 720)], (
        "camera modes are read from v4l2-ctl, MJPEG only, largest first"
    )
    with config.settings_lock:
        config.settings.update(usb_width=2048, usb_height=1536, roi="0.1,0.1,0.5,0.5")
    # the settings form resubmits the crop unchanged along with a new size
    r = c.post(
        "/api/settings", json={"usb_width": 3264, "usb_height": 2448, "roi": "0.1,0.1,0.5,0.5"}
    ).get_json()
    assert r.get("crop_reset") and config.settings["roi"] == "", (
        "changing the capture size resets the crop, since each size frames a different view"
    )
    r = c.post(
        "/api/settings", json={"usb_width": 2048, "usb_height": 1536, "roi": "0.2,0.2,0.4,0.4"}
    ).get_json()
    assert not r.get("crop_reset") and config.settings["roi"] == "0.2,0.2,0.4,0.4", (
        "a crop drawn in the same save as a new size is kept"
    )
    js = page_js()
    html = (APP / "templates" / "index.html").read_text()
    assert 'id="cropreset"' in html and "getElementById('cropreset')" in js, (
        "a Reset crop button sits beside Crop when a crop is set"
    )
    assert re.search(r"functionstartCrop[\s\S]{0,900}/api/preview", squash(js)) is not None, (
        "Crop starts from a live full camera frame, not the cropped photo"
    )
    c.post("/api/settings", json={"roi": ""})


def test_camera_crop():
    assert (
        photos_mod.crop_box({"roi": "0.1,0.2,0.5,0.6"}) == (0.1, 0.2, 0.5, 0.6)
        and photos_mod.crop_box({"roi": ""}) is None
        and photos_mod.crop_box({"roi": "junk"}) is None
    ), "the crop setting parses, and blank or bad means full frame"
    gl = app_source()
    assert '"--roi"' not in gl, "photos are always stored full frame (no capture-time crop)"
    calls = {"n": 0}
    real_rb = photos_mod.rebuild_thumbs_async
    photos_mod.rebuild_thumbs_async = lambda: calls.__setitem__("n", calls["n"] + 1)
    try:
        r = c.post("/api/settings", json={"roi": "0.1,0.2,0.5,0.6"}).get_json()
        bad = c.post("/api/settings", json={"roi": "0.9,0.9,0.5,0.5"}).get_json()
    finally:
        photos_mod.rebuild_thumbs_async = real_rb
    assert r["ok"] and calls["n"] == 1, "saving a crop rebuilds the thumbnails"
    assert (
        not bad["ok"] and "roi" in bad["errors"] and config.settings["roi"] == "0.1,0.2,0.5,0.6"
    ), "a crop running off the frame is refused and the old one kept"
    try:
        import cv2
        import numpy as np
    except Exception:
        pytest.skip("cropped snapshot, thumbnail and AI image (OpenCV not installed here)")
    img = np.zeros((600, 1000, 3), np.uint8)
    for n in ("20260925_120000.jpg",):
        cv2.imwrite(str(photos_mod.TIMELAPSE_DIR / n), img)
    snap = c.get("/photo/cropped.jpg")
    shape = None
    if snap.status_code == 200:
        shape = cv2.imdecode(np.frombuffer(snap.data, np.uint8), 1).shape[:2]
    assert shape == (360, 500), f"the snapshot is served cut to the crop ({shape}, want (360, 500))"
    import base64

    b = base64.b64decode(
        ai_report._image_b64(
            photos_mod.TIMELAPSE_DIR / "20260925_120000.jpg",
            (0.1, 0.2, 0.5, 0.6),
            tools.imgtool,
        )
    )
    ai_shape = cv2.imdecode(np.frombuffer(b, np.uint8), 1).shape[:2]
    assert ai_shape == (360, 500), f"the AI report gets the cropped photo ({ai_shape})"
    if shutil.which("ffmpeg"):
        with config.settings_lock:
            cfgc = dict(config.settings, timelapse_flatten=False)
        out = WORK / "thumbtest"
        out.mkdir(exist_ok=True)
        photos_mod.make_thumb(photos_mod.TIMELAPSE_DIR / "20260925_120000.jpg", cfgc, dst_dir=out)
        t = cv2.imread(str(out / "20260925_120000.jpg"))
        assert t is not None and t.shape[:2] == (460, 640), (
            f"raw thumbnails are cut to the crop ({None if t is None else t.shape[:2]}, want (460, 640))"
        )
    else:
        note("cropped thumbnail (ffmpeg not installed here)")
    c.post("/api/settings", json={"roi": ""})


def test_timelapse_sharp():
    js = page_js()
    css = Code((APP / "static" / "style.css").read_text())
    show = js_fn(js, "showFrame")
    stop = js_fn(js, "stopPlay")
    sharp = js_fn(js, "loadSharpFrame")
    assert (
        show
        and "loadSharpFrame()" in show.group(0)
        and stop
        and "loadSharpFrame()" in stop.group(0)
        and sharp
        and "if(ptimer" in sharp.group(0)
        and "'/frame/'" in sharp.group(0)
    ), "a paused or scrubbed-to frame swaps in the full-size photo; playback stays on thumbnails"
    rule = re.search(
        r"#captureinfo\{([^}]*)\}", squash(css.split("/* The capture status has its own line")[-1])
    )
    assert (
        rule
        and code_has(rule.group(1), "flex:1 0 100%")
        and "height:" in rule.group(1).replace("line-height:", "")
        and code_has(rule.group(1), "text-overflow:ellipsis")
    ), "the capture status has a fixed line of its own, so its text never moves the card"
    for bad in ("config.json", "_flat.jpg", "missing.jpg"):
        if c.get(f"/frame/{bad}").status_code != 404:
            pytest.fail(f"/frame refuses {bad}")
            break
    else:
        assert True, (
            "/frame serves only stored photos (config.json, _scratch and missing names are 404)"
        )
    try:
        import cv2
        import numpy as np
    except Exception:
        pytest.skip("full-size frame, its framing and caching (OpenCV not installed here)")
    name = "20260926_080000.jpg"
    img = (np.random.default_rng(3).integers(0, 255, (1200, 1600, 3))).astype(np.uint8)
    cv2.imwrite(str(photos_mod.TIMELAPSE_DIR / name), img)
    with config.settings_lock:
        saved = {k: config.settings.get(k) for k in ("grid", "timelapse_flatten", "roi")}
        config.settings.update(
            timelapse_flatten=True,
            roi="",
            grid={
                "corners": [[0.1, 0.1], [0.9, 0.1], [0.95, 0.9], [0.05, 0.9]],
                "cols": 6,
                "rows": 4,
            },
        )
        cfgf = dict(config.settings)
    try:
        r = c.get(f"/frame/{name}")
        f = cv2.imdecode(np.frombuffer(r.data, np.uint8), 1) if r.status_code == 200 else None
        out = WORK / "sharptest"
        out.mkdir(exist_ok=True)
        photos_mod.make_thumb(photos_mod.TIMELAPSE_DIR / name, cfgf, dst_dir=out)
        t = cv2.imread(str(out / name))
        ok = (
            f is not None
            and t is not None
            and f.shape[1] > 2 * t.shape[1]
            and abs(f.shape[1] / f.shape[0] - t.shape[1] / t.shape[0]) < 0.01
        )
        assert ok, (
            "the full-size frame is flattened like its thumbnail, at full resolution "
            f"({None if f is None else f.shape[:2]} vs thumb {None if t is None else t.shape[:2]})"
        )
        tag = r.headers.get("ETag", "").strip('"')
        r304 = c.get(f"/frame/{name}", headers={"If-None-Match": f'"{tag}"'})
        assert tag and r304.status_code == 304 and r.headers.get("Cache-Control") == "no-cache", (
            "an unchanged frame is answered 304 without redoing the warp"
        )
        c.post(
            "/api/grid",
            json={
                "corners": [[0.2, 0.1], [0.9, 0.1], [0.95, 0.9], [0.05, 0.9]],
                "rows": 4,
                "cols": 6,
            },
        )
        moved = c.get(f"/frame/{name}", headers={"If-None-Match": f'"{tag}"'})
        assert moved.status_code == 200, (
            "moving the grid corners changes the frame's tag, so it is redrawn"
        )
        with config.settings_lock:
            config.settings.update(timelapse_flatten=False, roi="")
        raw = c.get(f"/frame/{name}")
        assert (
            raw.status_code == 200 and raw.data == (photos_mod.TIMELAPSE_DIR / name).read_bytes()
        ), "with flattening off and no crop, the frame is the photo itself"
    finally:
        with config.settings_lock:
            config.settings.update(saved)
        (photos_mod.TIMELAPSE_DIR / name).unlink(missing_ok=True)
    # the video: sharper scaler and less compression, same preset and threads
    seen = []
    real_run = camera_mod.subprocess.run

    def fake_run(args, **kw):
        seen.append(list(args))
        return types.SimpleNamespace(returncode=1, stderr=b"test", stdout=b"")

    camera_mod.subprocess.run = fake_run
    try:
        with config.settings_lock:
            config.settings["timelapse_flatten"] = False
        video_mod.render_worker()
    finally:
        camera_mod.subprocess.run = real_run
        with config.settings_lock:
            config.settings.update(saved)
        with video_mod.render_lock:
            video_mod.render.update(state="idle", msg="", frames=0)
    enc = next((a for a in seen if "libx264" in a), [])
    vf = enc[enc.index("-vf") + 1] if "-vf" in enc else ""
    fr = enc[enc.index("-framerate") + 1] if "-framerate" in enc else ""
    js_ = page_js()
    assert (
        config.DEFAULTS.get("video_fps") == 8
        and config.DEFAULTS.get("player_fps") == 4
        and fr == "8"
        and "},Math.round(1000/Math.max(0.5,playerFps)));" in js_
        and (
            "playerFps=+j.settings.player_fps||4;" in js_
            or "set_playerFps(+j.settings.player_fps||4);" in js_
        )
        and "timelapse_speed_pct" not in js_
    ), f"the player and the video have their own speeds: video {fr} frames/s, player 4"
    src_c = (APP / "config.py").read_text()
    mig = src_c[
        src_c.index('if "timelapse_speed_pct" in settings:') : src_c.index("def save_config():")
    ]
    ns = {"settings": {"timelapse_speed_pct": 33}, "_file_keys": {"timelapse_speed_pct"}}
    exec(mig, ns)
    assert ns["settings"] == {"video_fps": 7.92}, (
        "an older config's shared speed percentage becomes the video speed (33% of 24 = 7.92 frames/s)"
    )
    started = []
    real_sr = video_mod.start_render
    video_mod.start_render = lambda: started.append(1) or True
    had_video = video_mod.VIDEO_PATH.exists()
    if not had_video:
        video_mod.VIDEO_PATH.write_bytes(b"x")
    try:
        db.kv_set("video_fps_rendered", 24.0)
        r1 = c.post("/api/settings", json={"video_fps": 8}).get_json()
        db.kv_set("video_fps_rendered", 8.0)
        r2 = c.post("/api/settings", json={"video_fps": 8, "player_fps": 5}).get_json()
        st_ = c.get("/api/status").get_json()
    finally:
        video_mod.start_render = real_sr
        if not had_video:
            video_mod.VIDEO_PATH.unlink(missing_ok=True)
    assert r1["ok"] and r2["ok"] and started == [1] and st_.get("video_fps") == 8.0, (
        "a new video speed re-renders the video; an "
        "unchanged one does not; the status says what speed the video has"
    )
    assert (
        enc
        and "flags=lanczos" in vf
        and enc[enc.index("-crf") + 1] == "20"
        and enc[enc.index("-preset") + 1] == "ultrafast"
        and enc[enc.index("-threads") + 1] == "1"
    ), f"the video uses a lanczos downscale at crf 20, still ultrafast and one thread ({vf})"


def test_lightbox():
    js = page_js()
    html = (APP / "templates" / "index.html").read_text()
    css = Code((APP / "static" / "style.css").read_text())
    assert (
        re.search(r'<div id="lightbox"[^>]*role="dialog"[^>]*hidden>', html)
        and 'id="lbclose"' in html
        and 'aria-label="Close"' in html
        and re.search(r"\.lightbox\{position:fixedinset:0z-index:\d{3,}", squash(css))
    ), "the enlarged view is a full-screen overlay with a Close (X) button, hidden until used"
    assert (
        "document.querySelector('#photocard .imgwrap').addEventListener('click',enlargePhoto)" in js
        and "document.getElementById('vframe').addEventListener('click',enlargeFrame)" in js
        and "dblclick" not in js
    ), "a single click (or tap) enlarges the snapshot and the timelapse frame"
    ep = js_fn(js, "enlargePhoto")
    ef = js_fn(js, "enlargeFrame")
    assert (
        ep
        and "if(cropping||gridEditable())return;" in ep.group(0)
        and "canEdit&&e&&e.target.classList&&e.target.classList.contains('gc')" in ep.group(0)
        and ef
        and "stopPlay()" in ef.group(0)
        and "'/frame/'" in ef.group(0)
    ), (
        "not while editing the grid or a crop, nor on a grid cell a signed-in click names; "
        "a timelapse frame pauses the player and opens full size"
    )
    assert (
        "e.key==='Escape')closeLightbox()" in js
        and "e.target.id==='lightbox'&&Date.now()-lbOpened>500" in js
    ), (
        "Esc and a click outside the picture close it; a habitual double click does not close it again"
    )


def test_camera_canopy():
    with config.settings_lock:
        config.settings["trays"] = {
            "1": {"label": "Seedling 1", "rows": 4, "cols": 3, "cells": {}},
            "2": {"label": "Seedling 2", "rows": 4, "cols": 3, "cells": {}},
            "T3": {"label": "Transplants", "rows": 4, "cols": 5, "cells": {}},
        }
    base = [
        {
            "name": "Seedlings",
            "light": "second",
            "lux": "",
            "sensors": [],
            "trays": ["1", "2"],
            "dli_low": 10,
            "dli_high": 15,
        },
        {
            "name": "Transplants",
            "light": "main",
            "lux": "lux",
            "sensors": [],
            "trays": ["T3"],
            "camera": True,
            "dli_low": 15,
            "dli_high": 20,
        },
    ]
    c.post("/api/settings", json={"setups": base})
    import json as _json

    seen = {}

    class R:
        returncode, stdout, stderr = 0, b'{"ok": true, "trays": {}}', b""

    def fake_run(cmd, **kw):
        seen["payload"] = _json.loads(cmd[-1])
        return R()

    real_run = camera_mod.subprocess.run
    camera_mod.subprocess.run = fake_run
    try:
        camera_mod.record_growth(
            WORK / "x.jpg",
            dict(config.settings),
            datetime.now(ZoneInfo(config.settings["timezone"])),
        )
    finally:
        camera_mod.subprocess.run = real_run
    ids = [t["id"] for t in (seen.get("payload") or {}).get("trays", [])]
    assert ids == ["T3"], f"canopy is measured only for the camera setup's trays ({ids})"
    db.log_many([("canopy:1", 3.1), ("canopy:T3", 7.5)])
    with config.settings_lock:
        config.settings["camera_enabled"] = True
    shown = set(c.get("/api/status").get_json()["sensors"])
    charted = set(c.get("/api/series_all?hours=1").get_json()["series"])
    ai = monitor.gather_report_data().get("canopy") or {}
    assert (
        "canopy:T3" in shown
        and "canopy:1" not in shown
        and "canopy:1" not in charted
        and list(ai) == ["Transplants"]
    ), "trays the camera no longer watches drop out of the chips, charts and AI report"
    c.post("/api/settings", json={"setups": []})
    shown2 = set(c.get("/api/status").get_json()["sensors"])
    assert "canopy:1" in shown2, "with the camera not assigned, every tray's canopy shows"


def test_oom():
    """Camera and image helpers are the kernel's first choice when memory runs
    out, ask the driver for as few buffers as they need, and report an
    out-of-memory kill as one."""
    of = tools.oom_first
    wrapped = of(["v4l2-ctl", "--all"])
    ok_wrap = wrapped[:2] == ["sh", "-c"] and wrapped[-2:] == ["v4l2-ctl", "--all"]
    if sys.platform.startswith("linux") and Path("/proc/self/oom_score_adj").exists():
        child = (
            subprocess.run(of(["cat", "/proc/self/oom_score_adj"]), capture_output=True)
            .stdout.decode()
            .strip()
        )
        mine = Path("/proc/self/oom_score_adj").read_text().strip()
        assert ok_wrap and child == "1000" and mine != "1000", (
            f"a helper runs as the first thing killed on out-of-memory, not the controller "
            f"(helper {child}, controller {mine})"
        )
    else:
        note("helper OOM score (no /proc here)")
    seen = []
    real_run = camera_mod.subprocess.run

    def fake_run(a, **kw):
        seen.append(list(a))
        return types.SimpleNamespace(returncode=-9, stderr=b"", stdout=b"")

    dev = WORK / "video0"
    dev.write_bytes(b"")
    camera_mod.subprocess.run = fake_run
    try:
        with config.settings_lock:
            cfgc = dict(config.settings, usb_device=str(dev))
        ok, err = camera_mod._usb_capture(cfgc, WORK / "oomtest.jpg", 3264, 2448)
    finally:
        camera_mod.subprocess.run = real_run
    cap = next((a for a in seen if "--stream-to" in " ".join(a)), [])
    assert cap[:2] == ["sh", "-c"] and "--stream-mmap=2" in cap and "--stream-mmap" not in cap, (
        "the capture runs as an OOM-first helper and asks the driver for two buffers, not four"
    )
    assert not ok and err and "out of memory" in err and "3264x2448" in err, (
        f"a capture killed for memory says so instead of 'no frames' ({err})"
    )
    killed = tools._why(-9, b"", "the image job")
    crashed = tools._why(-11, b"", "the image job")
    assert "out of memory" in killed and crashed == "the image job was killed by signal 11", (
        f"an image job killed by a signal reports why ({crashed})"
    )
    # tools.run is the only way a helper is started: anything else that calls
    # subprocess.run directly is a quick query (camera controls, systemctl)
    direct = []
    for f in ("camera.py", "photos.py", "video.py", "routes_camera.py", "monitor.py"):
        tree = ast.parse((APP / f).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "subprocess.run":
                first = ast.unparse(node.args[0])
                if not first.startswith(("['v4l2-ctl', '-d', dev", "['systemctl'")):
                    direct.append(f"{f}:{node.lineno}")
    assert not direct, (
        f"captures, thumbnails, the render and image jobs all start through tools.run {direct}"
    )
    seen.clear()
    camera_mod.subprocess.run = fake_run
    try:
        res = tools.run(["ffmpeg", "-version"], "the render", timeout=5)
    finally:
        camera_mod.subprocess.run = real_run
    assert seen[0][:2] == ["sh", "-c"] and "out of memory" in res.error and not res.ok, (
        f"tools.run starts every helper OOM-first and names a memory kill ({res.error})"
    )
    boot = (APP / "deploy" / "boot-config.txt").read_text()
    setup = (APP / "scripts" / "setup.sh").read_text()
    m = re.search(r"want=\"\$\(awk '\n([\s\S]*?)\n  ' \"\$CONFIG_TXT\"\)\"", setup)
    sample = (
        "dtparam=audio=on\ndtoverlay=vc4-kms-v3d\nmax_framebuffers=2\n[all]\ndtparam=i2c_arm=on\n"
    )
    out = ""
    if m and shutil.which("awk"):
        block = "\n".join(
            ["# BEGIN growlight"]
            + [ln for ln in boot.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
            + ["# END growlight"]
        )
        sf = WORK / "config.sample"
        sf.write_text(sample)
        out = subprocess.run(
            ["awk", m.group(1), str(sf)],
            capture_output=True,
            text=True,
            env=dict(os.environ, BOOT_BLOCK=block),
        ).stdout
    lines = out.splitlines()
    assert (
        "#growlight# dtoverlay=vc4-kms-v3d" in lines
        and "dtoverlay=vc4-kms-v3d,cma-128" in lines
        and lines.count("dtoverlay=vc4-kms-v3d") == 0
        and "CmaTotal" in setup
    ), (
        "the boot block sets CMA to 128 MB, comments out the stock KMS line, and setup.sh checks CmaTotal"
    )


def test_focus_sweep():
    """Each probe frame of the focus sweep is taken at the focus being tested."""
    seen = []
    real_run = camera_mod.subprocess.run

    def fake_run(a, **kw):
        seen.append(" ".join(str(x) for x in a))
        return types.SimpleNamespace(returncode=0, stderr=b"", stdout=b"")

    dev = WORK / "video0"
    dev.write_bytes(b"")
    keys = ("usb_device", "usb_auto_focus", "usb_focus_absolute")
    with config.settings_lock:
        saved = {k: config.settings.get(k) for k in keys}
        config.settings.update(usb_device=str(dev), usb_auto_focus=True, usb_focus_absolute=68)
    camera_mod.subprocess.run = fake_run
    try:
        camera_mod.run_focus_sweep()  # stops at the first probe: the fake writes no frame
    finally:
        camera_mod.subprocess.run = real_run
        with config.settings_lock:
            config.settings.update(saved)
        with camera_mod.focus_lock:
            camera_mod.focus_state["error"] = ""
    shot = next((i for i, c in enumerate(seen) if "--stream-to" in c), len(seen))
    before = " ".join(seen[:shot])
    assert (
        shot < len(seen)
        and "focus_absolute=0" in before
        and "focus_absolute=68" not in before
        and "focus_automatic_continuous=1" not in before
    ), "the focus sweep photographs each focus value it sets, not the configured one"


def test_canopy_stale():
    """Canopy comes from photos, taken only while the camera's light is on:
    at night its last reading is hours old by design and must not read stale."""
    js = page_js()
    rs = js_fn(js, "readingStale")
    assert (
        rs
        and "canopyDue!=null && now-Math.max(ts,canopyDue) > 3*capMin*60" in rs.group(0)
        and squash(js).count("readingStale(") >= 3
        and "const lim=" not in js
        and "if('canopy_due_since' in j)canopyDue=j.canopy_due_since;" in js
    ), "the chips and charts use the same rule, so a night-time canopy chip is not dimmed"

    tz = ZoneInfo(config.settings["timezone"])
    day = datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)
    with config.settings_lock:
        saved = {
            k: config.settings.get(k)
            for k in ("camera_enabled", "capture_enabled", "grid", "capture_interval_min", "setups")
        }
        config.settings.update(
            camera_enabled=True,
            capture_enabled=True,
            capture_interval_min=10,
            setups=[],
            grid={
                "corners": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                "rows": 4,
                "cols": 6,
            },
        )
        cfg = dict(config.settings)
    with config.state_lock:
        saved_state = {k: config.state.get(k) for k in ("on", "off")}
        config.state.update(on=day.replace(hour=7), off=day.replace(hour=19))
    try:
        night = camera_mod.canopy_due_since(cfg, day.replace(hour=22))
        noon = camera_mod.canopy_due_since(cfg, day)
        assert night is None and noon == day.replace(hour=7).timestamp(), (
            "canopy readings are due only inside the camera's light window"
        )
        cfg_off = dict(cfg, capture_enabled=False)
        assert (
            camera_mod.canopy_due_since(cfg_off, day) is None
            and camera_mod.canopy_due_since(dict(cfg, grid={}), day) is None
        ), "and never when timelapse capture is off or the grid has no corners"

        def health_why(now_dt, last_dt, key):
            db.log_reading(key, 40.0, ts=last_dt.timestamp())
            monitor._health_cache["ts"] = 0
            real_time, real_due = monitor.time.time, camera_mod.canopy_due_since
            monitor.time.time = lambda: now_dt.timestamp()
            camera_mod.canopy_due_since = lambda c, now=None: real_due(c, now_dt)
            try:
                return " ".join(monitor.sensor_health(cfg).get(key, {}).get("why", []))
            finally:
                monitor.time.time, camera_mod.canopy_due_since = real_time, real_due

        at_night = health_why(day.replace(hour=23), day.replace(hour=18, minute=55), "canopy:91")
        morning = health_why(
            day.replace(hour=7, minute=10),
            (day - timedelta(days=1)).replace(hour=18, minute=55),
            "canopy:92",
        )
        midday = health_why(day.replace(hour=12), day.replace(hour=9), "canopy:93")
        assert (
            "last reading" not in at_night
            and "last reading" not in morning
            and "last reading" in midday
        ), (
            "sensor health calls canopy stale only when photos are due and missing, "
            "counting from lights-on in the morning"
        )
        st = c.get("/api/status").get_json()
        assert "canopy_due_since" in st, "the status tells the page when canopy readings are due"
    finally:
        with config.settings_lock:
            config.settings.update(saved)
        with config.state_lock:
            config.state.update(saved_state)
        monitor._health_cache["ts"] = 0


def test_usb_link():
    """A camera on a USB 1.1 link offers only tiny modes. The link speed is in
    the status, raises an alert, and is checked by usbcheck.sh."""
    root = WORK / "sysfs"
    iface = root / "devices" / "usb1" / "1-1" / "1-1.3" / "1-1.3:1.0"
    iface.mkdir(parents=True, exist_ok=True)
    (root / "v4l" / "video9").mkdir(parents=True, exist_ok=True)
    link = root / "v4l" / "video9" / "device"
    if not link.exists():
        link.symlink_to(iface)
    real = camera_mod.SYSFS_V4L
    camera_mod.SYSFS_V4L = root / "v4l"
    try:
        (iface.parent / "speed").write_text("12\n")
        slow = camera_mod.usb_link_speed("/dev/video9")
        (iface.parent / "speed").write_text("480\n")
        fast = camera_mod.usb_link_speed("/dev/video9")
        none = camera_mod.usb_link_speed("/dev/video7")
    finally:
        camera_mod.SYSFS_V4L = real
    assert slow == 12.0 and fast == 480.0 and none is None, (
        f"the camera's USB link speed is read from sysfs (12 -> {slow}, 480 -> {fast}, missing -> {none})"
    )
    import alerts

    alerts.reset()
    fired = [
        a
        for a in alerts.check_all(
            {"_camera_slow_link": "12 Mbit/s"}, dict(alerts.DEFAULTS, sustain_seconds=0)
        )
        if a[1] == "camera_slow_link"
    ]
    alerts.reset()
    st = c.get("/api/status").get_json()
    sh = (APP / "scripts" / "usbcheck.sh").read_text()
    sjs = Code((APP / "static" / "screen.js").read_text())
    assert (
        fired
        and fired[0][0] == "fire"
        and "480 Mbit/s" in fired[0][3]
        and "usb_speed" in st["camera"]
        and subprocess.run(["bash", "-n", str(APP / "scripts" / "usbcheck.sh")]).returncode == 0
        and "must be 480" in sh
        and "bits >> 53 & 1" in sh
        and "on a slow USB link" in sjs
        and "Camera 2 on a slow USB link" in sjs
    ), (
        "a camera on a slow USB link is alerted and shown on the touchscreen; usbcheck.sh "
        "tests a hub (camera speed, configured size offered, touch seen, undervoltage)"
    )


def test_two_cameras():
    """Two USB cameras side by side: picked by port, aligned once,
    stitched into one wide photo; the grid follows and the crop resets."""
    ok = config._v_usb_device("/dev/v4l/by-path/platform-3f980000.usb-usb-0:1.2:1.0-video-index0")
    bad = []
    for v in ("/dev/sda", "video0", "/dev/v4l/by-path/../../etc/passwd x"):
        try:
            config._v_usb_device(v)
        except ValueError:
            bad.append(v)
    assert ok.endswith("index0") and len(bad) == 3 and config._v_usb_device2("") == "", (
        "a camera can be set by USB port (/dev/v4l/by-path); the second camera may be blank"
    )
    html = (APP / "templates" / "index.html").read_text()
    js = page_js()
    assert (
        'name="usb_device2"' in html
        and 'id="pairbtn"' in html
        and 'data-for="usb_device2"' in html
        and "/api/align_cameras" in js
        and "/api/cameras" in js
    ), "Settings, Camera has camera 2, a camera picker by port and Align cameras"
    ids = re.findall(r'\bid="([^"{}]+)"', html)
    dups = sorted({i for i in ids if ids.count(i) > 1})
    assert not dups, f"no two elements on the dashboard share an id ({dups})"
    r = c.get("/api/cameras").get_json()
    assert isinstance(r.get("cameras"), list), "the camera list answers (empty with no cameras)"
    with config.settings_lock:
        saved = {
            k: config.settings.get(k)
            for k in (
                "camera_backend",
                "usb_device",
                "usb_device2",
                "usb_width",
                "usb_height",
                "cam_stitch",
                "grid",
                "roi",
                "cam_rotate",
                "cam2_rotate",
                "camera_enabled",
            )
        }
        config.settings.update(camera_enabled=True, camera_backend="usb", usb_device2="")
    r = c.post("/api/align_cameras", json={}).get_json()
    assert not r["ok"] and "second camera" in r["error"], (
        f"Align cameras without a second camera says so ({r.get('error')})"
    )
    try:
        import cv2
        import numpy as np
    except Exception:
        pytest.skip("aligning and stitching two cameras (OpenCV not installed here)")
    rng = np.random.default_rng(7)
    scene = np.full((900, 1800, 3), (60, 80, 70), np.uint8)
    for _ in range(500):
        col = tuple(int(x) for x in rng.integers(0, 255, 3))
        cv2.circle(
            scene,
            (int(rng.integers(0, 1800)), int(rng.integers(0, 900))),
            int(rng.integers(4, 30)),
            col,
            -1,
        )
    frames = {"/dev/cam1": scene[:, :1200].copy()}
    M = cv2.getPerspectiveTransform(
        np.float32([[600, 0], [1800, 0], [1800, 900], [600, 900]]),
        np.float32([[8, 12], [1195, 0], [1200, 900], [0, 885]]),
    )
    # camera 2 is mounted upside down: its own rotation (cam2_rotate) turns it back
    frames["/dev/cam2"] = cv2.rotate(
        (cv2.warpPerspective(scene, M, (1200, 900)) * 0.9).astype(np.uint8), cv2.ROTATE_180
    )
    shots = []

    def fake_capture(cfg, out, w, h, warmup=None, dev=None):
        dev = dev or cfg.get("usb_device")
        shots.append(dev)
        # as the real capture does: JPEG bytes, whatever the file is called
        Path(out).write_bytes(cv2.imencode(".jpg", frames[dev])[1].tobytes())
        return True, None

    real = camera_mod._usb_capture
    camera_mod._usb_capture = fake_capture
    try:
        with config.settings_lock:
            config.settings.update(
                camera_backend="usb",
                camera_enabled=True,
                usb_device="/dev/cam1",
                usb_device2="/dev/cam2",
                usb_width=1200,
                usb_height=900,
                cam_stitch={},
                cam_rotate=0,
                cam2_rotate=180,
                roi="0.1,0.1,0.5,0.5",
                grid=dict(
                    config.settings.get("grid") or {},
                    corners=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
                ),
            )
            cfg = dict(config.settings)
        # not aligned yet: camera 1 alone, so the timelapse never mixes views
        out = WORK / "pair.jpg"
        okc, _ = camera_mod._usb_capture_view(cfg, out, 1200, 900)
        single = cv2.imread(str(out)).shape[:2]
        assert okc and single == (900, 1200) and shots == ["/dev/cam1"], (
            "with a second camera set but not aligned, photos are camera 1's alone"
        )
        res = camera_mod.align_cameras()
        with config.settings_lock:
            st = dict(config.settings.get("cam_stitch") or {})
            corners = config.settings["grid"]["corners"]
            roi = config.settings["roi"]
            cfg = dict(config.settings)
        H = np.array(st.get("h") or np.eye(3))
        # camera 2's left edge sits 600 px into camera 1's view
        x_at = H @ np.array([0, 450, 1.0])
        assert (
            res.get("ok")
            and res["inliers"] >= 25
            and abs(x_at[0] / x_at[2] - 600) < 15
            and 1750 <= res["canvas"][0] <= 1850
            and st.get("devices") == ["/dev/cam1", "/dev/cam2"]
            and camera_mod.stitch_of(cfg)
            and camera_mod.PREVIEW_PATH.exists()
        ), (
            f"Align cameras fits camera 2 onto camera 1 and saves it "
            f"({res.get('inliers')} points, {res.get('overlap_pct')}% overlap, x {x_at[0] / x_at[2]:.0f})"
        )
        f1 = c.get("/align_frame/1.jpg")
        f2 = c.get("/align_frame/2.jpg")
        fm = c.get("/align_frame/matches.jpg")
        mimg = cv2.imdecode(np.frombuffer(fm.data, np.uint8), 1) if fm.status_code == 200 else None
        view = c.get("/api/status").get_json()["camera"].get("align_view")
        assert (
            f1.status_code == 200
            and f2.status_code == 200
            and c.get("/align_frame/3.jpg").status_code == 404
            and mimg is not None
            and mimg.shape[1] > mimg.shape[0]
            and view
            and 'id="pairview"' in html
        ), (
            "the two frames Align cameras used, and a picture of what matched between them, "
            "can be opened from Settings to judge the overlap"
        )
        import subprocess as sp

        neg = WORK / "mirror.jpg"
        cv2.imwrite(str(neg), cv2.flip(frames["/dev/cam1"], 1))
        miss = json.loads(
            sp.run(
                [
                    sys.executable,
                    str(APP / "imgtool.py"),
                    "align",
                    str(WORK / "pair.jpg"),
                    str(neg),
                ],
                capture_output=True,
            ).stdout
            or b"{}"
        )
        assert miss.get("ok") is False and "check each camera's rotation" in miss.get(
            "error", ""
        ), (
            "a fit that would turn or rescale camera 2 is refused (each camera is already turned "
            f"the right way up), so a looser fit cannot accept a false match ({miss.get('error')})"
        )
        # the grid stays over the same part of the trays
        back = camera_mod.regrid_for_stitch(
            {"grid": {"corners": corners}, "cam_rotate": 0, "usb_width": 1200, "usb_height": 900},
            st,
            None,
        )
        assert (
            roi == ""
            and corners != [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
            and all(
                abs(a - b) < 0.002
                for p, q in zip(back, [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]])
                for a, b in zip(p, q)
            )
        ), (
            f"aligning moves the grid onto the wide view and resets the crop; the move is "
            f"exact both ways ({corners})"
        )
        shots.clear()
        okc, err = camera_mod._usb_capture_view(cfg, out, 1200, 900)
        got = cv2.imread(str(out))
        x0, y0 = st["offset"]
        seam = got[100 - y0 : 800 - y0, 800 - x0 : 1000 - x0].astype(int)
        truth = scene[100:800, 800:1000].astype(int)
        diff = float(np.abs(seam - truth).mean()) if seam.shape == truth.shape else 999
        assert (
            okc
            and shots == ["/dev/cam1", "/dev/cam2"]
            and got.shape[1] == st["canvas"][0]
            and diff < 6
            and not list(WORK.glob("pair.jpg.cam*"))
        ), (
            f"once aligned, each photo reads camera 1 then camera 2 and stitches them; the "
            f"seam matches the scene (mean difference {diff:.1f})"
        )
        # through the page: started in the background, then polled
        started = c.post("/api/align_cameras", json={}).get_json()
        for _ in range(120):
            job = c.get("/api/align_cameras").get_json()
            if not job.get("running"):
                break
            time.sleep(0.5)
        assert started.get("started") and job.get("running") is False and job["result"]["ok"], (
            "Align cameras from the page runs in the background (a proxy would cut off a "
            f"minute-long request) and the page polls for the outcome ({job.get('result', {}).get('inliers')} points)"
        )
        assert (
            'name="cam2_rotate"' in html
            and st.get("rotations") == [0, 180]
            and not camera_mod.stitch_of(dict(cfg, cam2_rotate=0))
        ), (
            "camera 2 has its own rotation, applied before matching and stitching (a camera "
            "mounted the other way round); changing it calls for a new alignment"
        )
        # a different second camera: the alignment no longer applies
        r = c.post("/api/settings", json={"usb_device2": "/dev/video4"}).get_json()
        with config.settings_lock:
            gone = config.settings.get("cam_stitch")
            moved = config.settings["grid"]["corners"]
        assert (
            r.get("ok")
            and not gone
            and all(
                abs(a - b) < 0.002
                for p, q in zip(moved, [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]])
                for a, b in zip(p, q)
            )
        ), "changing camera 2 drops the alignment and moves the grid back to camera 1's view"
        stt = c.get("/api/status").get_json()["camera"]
        assert stt.get("second") is True and stt.get("stitch") is None, (
            "the status says a second camera is set and not aligned"
        )
    finally:
        camera_mod._usb_capture = real
        with config.settings_lock:
            config.settings.update(saved)
        camera_mod.PREVIEW_PATH.unlink(missing_ok=True)
        for f in camera_mod.ALIGN_FRAMES + [camera_mod.ALIGN_MATCHES]:
            f.unlink(missing_ok=True)
    it = (APP / "imgtool.py").read_text()
    assert (
        "detail_DpSeamFinder" in it
        and "IMREAD_REDUCED_COLOR_2" in it
        and "ALIGN_WORK_PX = 640" in it
        and camera_mod.STITCH_MAX_W <= 3072
    ), (
        "stitching joins along a routed seam and keeps memory down (frames decoded at the "
        "size they are used, matching at 640 px, output at most 3072 px wide)"
    )
    sh = (APP / "scripts" / "usbcheck.sh").read_text()
    cam = (APP / "video.py").read_text()
    assert (
        'check_camera "Camera 2" "$dev2"' in sh
        and "force_original_aspect_ratio=decrease" in cam
        and "pad={VIDEO_W}:" in cam
    ), (
        "usbcheck.sh checks both cameras; the video keeps one frame size when a run changes "
        "shape (letterboxed)"
    )


def test_flatten_proportions():
    """A flattened frame keeps its cells square when it has to be shrunk."""
    try:
        import cv2  # noqa: F401
        import numpy as np
    except ImportError:
        pytest.skip("flattened frame proportions (OpenCV not installed here)")
    import growth

    wide = np.zeros((2592, 4608, 3), np.uint8)
    corners = [[0.02, 0.02], [0.98, 0.02], [0.98, 0.98], [0.02, 0.98]]
    h, w = growth.rectify(wide, corners, cols=6, rows=4).shape[:2]
    assert max(w, h) <= 4000 and abs(w / h - 6 / 4) < 0.01, (
        f"a 6x4 grid across a 4608 px frame flattens to 3:2 within the size limit ({w}x{h})"
    )


def test_capture_scratch():
    """Files a capture is still working on are never taken for photos, and
    what an interrupted capture leaves behind is cleared."""
    tl = photos_mod.TIMELAPSE_DIR
    jpeg = b"\xff\xd8\xff\xd9"
    photo = tl / "20260101_120000.jpg"
    photo.write_bytes(jpeg)
    out = tl / "20260101_121000.jpg"
    devices = ["/dev/cam1", "/dev/cam2"]
    cfg = dict(
        config.settings,
        camera_backend="usb",
        usb_device=devices[0],
        usb_device2=devices[1],
        usb_width=1200,
        usb_height=900,
        cam_rotate=0,
        cam2_rotate=0,
        cam_stitch={
            "h": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            "devices": devices,
            "capture": [1200, 900],
            "rotations": [0, 0],
        },
    )
    seen = {}

    def fake_capture(cfg_, path, w, h, warmup=None, dev=None):
        Path(path).write_bytes(jpeg)
        return True, None

    def fake_imgtool(args, what="", timeout=120):
        # both cameras' frames are on disk here; the stitched photo is not yet
        seen["listed"] = sorted(p.name for p in tl.glob("*.jpg"))
        seen["latest"] = photos_mod.photo_inventory()[1]
        Path(args[3]).write_bytes(jpeg)
        return tools.Result(0, b"", b"", "")

    real = camera_mod._usb_capture, tools.imgtool
    camera_mod._usb_capture, tools.imgtool = fake_capture, fake_imgtool
    try:
        ok, _ = camera_mod._usb_capture_view(cfg, out, 1200, 900)
    finally:
        camera_mod._usb_capture, tools.imgtool = real
    assert (
        ok
        and out.exists()
        and seen.get("listed")
        and not [n for n in seen["listed"] if ".cam" in n]
        and ".cam" not in seen["latest"].name
        and not list(tl.glob(out.name + ".cam*"))
    ), (
        f"while two cameras are being stitched, their separate frames are not listed as "
        f"photos ({len(seen.get('listed', []))} listed)"
    )

    ghost = tl / "99991231_235959.jpg"  # listed, but gone by the time it is read
    ghost.symlink_to(tl / "nowhere.jpg")
    try:
        n, latest, when = photos_mod.photo_inventory()
    finally:
        ghost.unlink()
    assert latest is not None and latest != ghost and when is not None, (
        "a photo removed between listing and reading does not break the photo count"
    )

    stale = [tl / "20260101_110000.jpg.raw", tl / "20260101_110000.jpg.cam1.tmp"]
    for f in stale:
        f.write_bytes(b"x")
        os.utime(f, (time.time() - 3600,) * 2)
    fresh = tl / "20260101_123000.jpg.part"
    fresh.write_bytes(b"x")
    try:
        removed = camera_mod.clear_capture_scratch()
        assert (
            removed == 2
            and not any(f.exists() for f in stale)
            and fresh.exists()
            and photo.exists()
        ), (
            "leftovers of an interrupted capture are cleared at start; a file a capture "
            "may still be writing, and every photo, is left alone"
        )
    finally:
        for f in (fresh, photo, out, *stale):
            f.unlink(missing_ok=True)
        (photos_mod.THUMB_DIR / out.name).unlink(missing_ok=True)


def test_the_photo_list_is_read_again_only_when_the_folder_changes():
    thumbs = photos_mod.THUMB_DIR
    for name in ("20260101_120000.jpg", "20260101_120500.jpg"):
        (thumbs / name).write_bytes(b"x")
    first = c.get("/api/photos")
    assert first.get_json()["names"] == ["20260101_120000.jpg", "20260101_120500.jpg"]

    listings = []
    real_glob = type(thumbs).glob

    def counting(self, pattern):
        listings.append(pattern)
        return real_glob(self, pattern)

    type(thumbs).glob = counting
    try:
        again = c.get("/api/photos")
        assert again.get_json() == first.get_json() and listings == [], (
            "unchanged: not listed again"
        )
        same = c.get("/api/photos", headers={"If-None-Match": first.headers["ETag"]})
        assert same.status_code == 304 and not same.data, "a browser that has the list gets 304"

        (thumbs / "20260101_121000.jpg").write_bytes(b"x")
        new = c.get("/api/photos", headers={"If-None-Match": first.headers["ETag"]})
        assert new.status_code == 200 and len(new.get_json()["names"]) == 3 and listings
        (thumbs / "20260101_120000.jpg").unlink()
        assert "20260101_120000.jpg" not in c.get("/api/photos").get_json()["names"]
    finally:
        type(thumbs).glob = real_glob


def test_a_photo_that_cannot_be_rotated_is_not_kept(monkeypatch):
    """The frame is written and rotated under a .part name; when the rotation
    fails nothing joins the timelapse, and the failure counts against the
    camera."""
    cam = camera_mod
    monkeypatch.setattr(cam.time, "sleep", lambda s: None)
    monkeypatch.setattr(photos_mod, "make_thumb", lambda *a, **k: None)
    names_seen = []

    def fake_rpicam(cfg, out_path):
        names_seen.append(Path(out_path).name)
        Path(out_path).write_bytes(b"\xff\xd8\xff\xd9")

    monkeypatch.setattr(cam, "_rpicam_capture", fake_rpicam)
    rotated = {"ok": False}
    monkeypatch.setattr(
        cam.tools,
        "imgtool",
        lambda args, what, **k: tools.Result(
            0 if rotated["ok"] else -9, b"", b"", "" if rotated["ok"] else "killed: out of memory"
        ),
    )
    cfg = dict(config.settings, camera_backend="rpicam", cam_rotate=90, capture_set_light=False)
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    fails = cam.camera["fails"]
    try:
        lost = cam.take_photo(cfg, now)
        left = sorted(p.name for p in photos_mod.TIMELAPSE_DIR.glob("*") if p.is_file())
        failed = cam.camera["fails"] - fails
        rotated["ok"] = True
        kept = cam.take_photo(cfg, now)
        after = sorted(p.name for p in photos_mod.TIMELAPSE_DIR.glob("*") if p.is_file())
    finally:
        cam.camera["fails"] = fails
    assert lost is None and left == [] and failed == 1, (
        f"a failed rotation leaves no photo and no scratch file ({left})"
    )
    assert kept is not None and after == [kept.name] and kept.suffix == ".jpg", after
    assert all(n.endswith(".jpg.part") for n in names_seen), (
        f"the camera writes under a name nothing lists as a photo ({names_seen})"
    )


def test_an_unreadable_photo_is_not_thumbnailed_on_every_tick(monkeypatch):
    cam = camera_mod
    bad = photos_mod.TIMELAPSE_DIR / "20200101_000000.jpg"
    bad.write_bytes(b"\xff\xd8 cut short")
    old = time.time() - 60
    os.utime(bad, (old, old))
    tries = []
    monkeypatch.setattr(photos_mod, "make_thumb", lambda p, *a, **k: tries.append(p.name))
    monkeypatch.setattr(cam, "_no_thumb", set())
    with config.settings_lock:
        config.settings.update(camera_enabled=False)
    for _ in range(3):
        cam._capture_tick(None)
    assert tries == [bad.name], f"the backfill tries a photo once ({len(tries)} tries)"


def test_render_refuses_when_the_card_is_short_of_space(monkeypatch):
    for i in range(4):
        (photos_mod.TIMELAPSE_DIR / f"2026092{i}_120000.jpg").write_bytes(b"photo")
    ran = []
    monkeypatch.setattr(video_mod.tools, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(video_mod.tools, "imgtool", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(photos_mod, "disk_free_gb", lambda path=None: photos_mod.MIN_FREE_GB)
    video_mod.render_worker()
    state = dict(video_mod.render)
    assert state["state"] == "error" and "free space" in state["msg"] and not ran, state
    assert video_mod.scratch_gb(1000, True) > video_mod.scratch_gb(1000, False) > 0


def test_flattened_video_frames_are_no_wider_than_the_video(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    src = tmp_path / "frame.jpg"
    cv2.imwrite(str(src), np.full((1200, 3200, 3), 128, np.uint8))
    listfile = tmp_path / "frames.txt"
    listfile.write_text(f"{src}\n")
    dest = tmp_path / "flat"
    dest.mkdir()
    corners = json.dumps([[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]])
    r = tools.imgtool(["flatten", dest, listfile, corners, 6, 4, "--max-w", 1280])
    assert r.ok, r.error
    w, h = photos_mod.jpeg_size(dest / "000000.jpg")
    assert w == 1280 and h < 1280, f"{w}x{h}"


def _wait_for_thumbs():
    deadline = time.time() + 10
    time.sleep(0.05)
    while photos_mod._thumbs_lock.locked() and time.time() < deadline:
        time.sleep(0.02)


def test_thumbnail_rebuild_keeps_up_with_what_changes(monkeypatch):
    """A rebuild asked for while one is running is run afterwards with the
    settings as they are then, and a photo taken meanwhile keeps its thumbnail."""
    tl, th = photos_mod.TIMELAPSE_DIR, photos_mod.THUMB_DIR
    th.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (tl / f"2026092{i}_120000.jpg").write_bytes(b"photo")
    (th / "20200101_000000.jpg").write_text("its photo is gone")
    new = "20260930_120000.jpg"

    def fake_thumb(ph, cfg=None, dst_dir=None):
        time.sleep(0.05)
        if not (tl / new).exists():  # a photo arrives, thumbnail and all, mid-rebuild
            (tl / new).write_bytes(b"photo")
            (th / new).write_text("new")
        ((dst_dir or th) / ph.name).write_text(
            "flat" if (cfg or {}).get("timelapse_flatten", True) else "raw"
        )

    monkeypatch.setattr(photos_mod, "make_thumb", fake_thumb)
    with config.settings_lock:
        config.settings["timelapse_flatten"] = True
    photos_mod.rebuild_thumbs_async()
    time.sleep(0.07)
    with config.settings_lock:
        config.settings["timelapse_flatten"] = False
    photos_mod.rebuild_thumbs_async()  # while the first is still running
    _wait_for_thumbs()
    kinds = {p.name: p.read_text() for p in th.glob("*.jpg")}
    assert "20200101_000000.jpg" not in kinds, "a thumbnail goes when its photo has gone"
    assert new in kinds, "a photo taken during the rebuild keeps its thumbnail"
    assert set(kinds.values()) == {"raw"}, (
        f"the second request is not dropped: every thumbnail matches the newest settings ({kinds})"
    )


def test_photo_and_video_times_carry_their_time_zone():
    (photos_mod.TIMELAPSE_DIR / "20260920_120000.jpg").write_bytes(b"photo")
    j = c.get("/api/status").get_json()
    stamp = j["latest_photo_time"]
    assert stamp and re.search(r"[+-]\d\d:\d\d$", stamp), (
        f"a browser in another time zone can still tell how old the photo is ({stamp})"
    )
