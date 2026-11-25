"""Tests: kiosk."""

import json
import os
import pytest
import re
import subprocess
import threading

from harness import (
    db,
    APP,
    Code,
    ON_SCREEN,
    WORK,
    c,
    config,
    page_js,
    routes,
)


def test_kiosk():
    """The touchscreen on the Pi's own HDMI: a light browser under a memory
    ceiling, signed in because it connects from the Pi itself."""
    unit = (APP / "deploy" / "growlight-kiosk.service").read_text()
    sh = (APP / "scripts" / "kiosk.sh").read_text()
    sess = (APP / "scripts" / "kiosk-session.sh").read_text()
    ok_sh = all(
        subprocess.run(["bash", "-n", str(APP / "scripts" / f)]).returncode == 0
        for f in ("kiosk.sh", "kiosk-session.sh")
    )
    assert (
        ok_sh
        and "MemoryMax=210M" in unit
        and "OOMScoreAdjust=1000" in unit
        and "Conflicts=getty@tty1.service" in unit
        and "exec cog" in sess
        and 'wlr-randr --output "$out" --transform "$KIOSK_ROTATE"' in sess
        and "install_kiosk" in sh
        and "remove_kiosk" in sh
        and "touch_rotate" in sh
    ), (
        "the kiosk installs as an opt-in service, first in line for the OOM killer, with "
        "portrait rotation and a clean remove"
    )
    r = c.get("/screen")
    page = r.get_data(as_text=True)
    sjs = Code((APP / "static" / "screen.js").read_text())
    ajs = page_js()
    assert (
        r.status_code == 200
        and "/static/screen.js" in page
        and 'id="setups"' in page
        and 'KIOSK_URL="http://127.0.0.1:${port:-5001}/screen"'
        in (APP / "scripts" / "kiosk-session.sh").read_text()
        and "post('/api/heat', {mode: v})" in sjs
        and "post('/api/light', {mode: v})" in sjs
        and "post('/api/settings', {light2_override: v})" in sjs
        and "post('/api/fan', {mode: v})" in sjs
        and "setInterval(refresh, 10000)" in sjs
        and "/api/series_all?hours=24&only=" in sjs
        and "location.href='/screen'" in ajs
        and "kioskback" in ajs
    ), (
        "the touchscreen opens a one-page summary (/screen) with the light, heat mat and fan "
        "controls; the full dashboard is one tap away and returns to the summary on its own"
    )
    with config.settings_lock:
        had_pw = config.settings.get("password_hash")
        config.settings["password_hash"] = "pbkdf2:sha256:1$x$y"
    was = config.TRUST_LOCALHOST
    fan = {"json": {"mode": "auto"}}
    try:
        c2 = routes.app.test_client()
        config.TRUST_LOCALHOST = False
        off = c2.post("/api/fan", **fan, **ON_SCREEN).status_code
        config.TRUST_LOCALHOST = True
        local = c2.post("/api/fan", **fan, **ON_SCREEN).status_code
        main_port = c2.post("/api/fan", **fan).status_code
        remote = c2.post(
            "/api/fan", **fan, **ON_SCREEN, environ_base={"REMOTE_ADDR": "10.0.0.69"}
        ).status_code
    finally:
        config.TRUST_LOCALHOST = was
        with config.settings_lock:
            config.settings["password_hash"] = had_pw
    assert off == 401 and local == 200 and main_port == 401 and remote == 401, (
        f"with the kiosk on, the Pi's own screen may change things without signing in; "
        f"the main port and other machines still need the password "
        f"(off {off}, screen {local}, main port {main_port}, remote {remote})"
    )


def test_screen_listener_real():
    """The same rule against the real web server: only the screen's own
    listener is trusted, and nothing sent to the main port changes that."""
    import urllib.error
    import urllib.request

    try:
        from waitress import create_server
    except ImportError:
        pytest.skip("the screen's listener on the real web server (waitress not installed here)")
    server = create_server(routes.app, listen="127.0.0.1:0 127.0.0.1:0")
    main, screen = (port for _, port in server.effective_listen)
    threading.Thread(target=server.run, daemon=True).start()

    def post(port, **headers):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/fan",
            data=b'{"mode": "auto"}',
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    was = config.TRUST_LOCALHOST, config.SCREEN_PORT
    with config.settings_lock:
        had_pw = config.settings.get("password_hash")
        config.settings["password_hash"] = "pbkdf2:sha256:1$x$y"
    try:
        config.TRUST_LOCALHOST, config.SCREEN_PORT = True, screen
        got = {
            "screen": post(screen),
            "main": post(main),
            "main, forwarded": post(main, **{"X-Forwarded-For": "203.0.113.9"}),
            "main, claiming the screen's port": post(
                main, Host=f"127.0.0.1:{screen}", **{"X-Forwarded-Port": str(screen)}
            ),
        }
        config.TRUST_LOCALHOST = False
        got["screen, kiosk off"] = post(screen)
    finally:
        config.TRUST_LOCALHOST, config.SCREEN_PORT = was
        with config.settings_lock:
            config.settings["password_hash"] = had_pw
        server.close()
    assert got == {
        "screen": 200,
        "main": 401,
        "main, forwarded": 401,
        "main, claiming the screen's port": 401,
        "screen, kiosk off": 401,
    }, f"on the real web server only the screen's own listener is signed in ({got})"
    assert config.listen_on() == f"0.0.0.0:{config.HTTP_PORT}" or config.listen_on().endswith(
        f" 127.0.0.1:{config.SCREEN_PORT}"
    ), "the screen's listener is bound to 127.0.0.1 and opened only with the kiosk on"


def test_kiosk_unit():
    """What the screen needed on the real Pi: the browser's errors in the
    journal, WebKit's sandbox off, CPU rendering, an invisible cursor, and the
    stopgap drop-ins cleaned up by a reinstall."""
    unit = (APP / "deploy" / "growlight-kiosk.service").read_text()
    sh = (APP / "scripts" / "kiosk.sh").read_text()
    rendered = subprocess.run(
        [
            "sed",
            "-e",
            "s|@CURSOR_DIR@|/home/ben/.local/share/openseedling-cursor|g",
            "-e",
            "s|@APP_DIR@|/home/ben/growlight|g",
            "-e",
            "s|@RUN_USER@|ben|g",
            "-e",
            "s|@ROTATE@|0|g",
            str(APP / "deploy" / "growlight-kiosk.service"),
        ],
        capture_output=True,
        text=True,
    ).stdout
    assert (
        "StandardOutput=journal" in unit
        and "StandardError=journal" in unit
        and "Environment=WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1" in unit
        and "Environment=WEBKIT_SKIA_ENABLE_CPU_RENDERING=1" in unit
        and "Environment=XCURSOR_PATH=/home/ben/.local/share/openseedling-cursor" in rendered
        and not re.search(r"@[A-Z_]+@", rendered)
        and '-e "s|@CURSOR_DIR@|$CURSOR_DIR|g"' in sh
        and 'python3 "$APP_DIR/scripts/blank_cursor.py" "$CURSOR_DIR"' in sh
        and "for d in log.conf sandbox.conf render.conf; do" in sh
    ), (
        "the kiosk unit logs to the journal, runs WebKit without its sandbox and with CPU "
        "rendering, points at an invisible cursor theme, and a reinstall removes the stopgaps"
    )
    import importlib.util
    import struct

    spec = importlib.util.spec_from_file_location(
        "blank_cursor", APP / "scripts" / "blank_cursor.py"
    )
    bc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bc)
    d = bc.write_theme(WORK / "cursor")
    data = (d / "left_ptr").read_bytes()
    magic, hsize, _ver, ntoc = struct.unpack("<4sIII", data[:16])
    typ, _sub, pos = struct.unpack("<III", data[16:28])
    chunk = struct.unpack("<IIIIIIIII", data[pos : pos + 36])
    pixel = struct.unpack("<I", data[pos + 36 : pos + 40])[0]
    names = {p.name for p in d.iterdir()}
    assert (
        magic == b"Xcur"
        and hsize == 16
        and ntoc == 1
        and typ == 0xFFFD0002
        and chunk[4:6] == (1, 1)
        and pixel == 0
        and {"default", "left_ptr", "pointer", "text", "hand2", "xterm"} <= names
    ), (
        f"the cursor theme is valid Xcursor, 1x1 and fully transparent, for every common "
        f"cursor name ({len(names)} names)"
    )
    was = config.TRUST_LOCALHOST
    try:
        c2 = routes.app.test_client()
        config.TRUST_LOCALHOST = True
        local = c2.get("/screen", **ON_SCREEN).get_data(as_text=True)
        local_dash = c2.get("/", **ON_SCREEN).get_data(as_text=True)
        proxied = c2.get("/screen").get_data(as_text=True)  # the main port
        config.TRUST_LOCALHOST = False
        off = c2.get("/screen", **ON_SCREEN).get_data(as_text=True)
    finally:
        config.TRUST_LOCALHOST = was
    css = Code((APP / "static" / "screen.css").read_text())
    dcss = Code((APP / "static" / "style.css").read_text())
    rule = "body.onscreen,body.onscreen *{cursor:none !important}"
    assert (
        '<body class="onscreen">' in local
        and '<body class="onscreen">' in local_dash
        and '<body class="onscreen">' not in proxied
        and '<body class="onscreen">' not in off
        and rule in css
        and rule in dcss
    ), (
        "pages for the Pi's own screen hide the pointer; through the proxy, or without the "
        "kiosk, the pointer stays"
    )
    raw_css = (APP / "static" / "screen.css").read_text()
    px = sorted(set(re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)px", raw_css)))
    sjs = Code((APP / "static" / "screen.js").read_text())
    sess = (APP / "scripts" / "kiosk-session.sh").read_text()
    assert (
        px == ["1"]
        and "document.documentElement.style.fontSize" in sjs
        and "clientWidth / DESIGN_W" in sjs
        and "0.28 * b.clientHeight" in sjs
        and "wlr-randr 2>&1 | sed 's/^/display: /'" in sess
    ), (
        "the summary is sized in rem and its text grows into spare height on a bigger "
        f"screen (never wider than the width allows); the display mode is logged (px {px})"
    )
    db.log_many([("temp:air", 21.0), ("temp:soil", 24.0), ("heat:duty", 40.0), ("humidity", 55.0)])
    with routes.app.test_client() as c3:
        some = c3.get("/api/series_all?hours=24&only=temp:,heat:duty").get_json()["series"]
        every = c3.get("/api/series_all?hours=24").get_json()["series"]
    assert set(some) == {k for k in every if k.startswith(("temp:", "heat:duty"))} and len(
        every
    ) > len(some), (
        f"series_all?only= returns just those keys, for the touchscreen's charts ({sorted(some)})"
    )


def test_external_display():
    """Settings, External display: rotation and mode follow the settings while
    the kiosk runs, and the summary's charts and night screen are set there."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "display_watch", APP / "scripts" / "display_watch.py"
    )
    dw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dw)
    sample = """HDMI-A-1 "Unknown (HDMI-A-1)"
  Enabled: yes
  Modes:
    1024x600 px, 60.000000 Hz (preferred)
    1920x1080 px, 60.000000 Hz (current)
  Position: 0,0
  Transform: 90
  Scale: 1.000000
"""
    st = dw.parse(sample)
    assert (
        st["output"] == "HDMI-A-1"
        and st["modes"] == ["1024x600@60", "1920x1080@60"]
        and st["current"] == "1920x1080@60"
        and st["preferred"] == "1024x600@60"
        and st["transform"] == 90
    ), f"the display watcher reads wlr-randr: output, modes, the one in use, rotation ({st})"
    # a fake wlr-randr that reports the sample and records what it is told
    d = WORK / "fakerandr"
    d.mkdir(exist_ok=True)
    calls = d / "calls"
    (d / "wlr-randr").write_text(
        f"#!/bin/sh\n[ $# -gt 0 ] && echo \"$@\" >> {calls}\ncat <<'EOF'\n{sample}EOF\n"
    )
    (d / "wlr-randr").chmod(0o755)
    real_cfg, real_state = dw.CONFIG, dw.STATE
    dw.CONFIG, dw.STATE = d / "config.json", d / "display_state.json"
    path = os.environ["PATH"]
    os.environ["PATH"] = f"{d}:{path}"
    try:
        dw.CONFIG.write_text(json.dumps({"screen_rotate": "", "screen_mode": ""}))
        dw.step(90, log=lambda m: None)
        quiet = calls.read_text() if calls.exists() else ""
        dw.CONFIG.write_text(json.dumps({"screen_rotate": "180", "screen_mode": "1024x600"}))
        dw.step(90, log=lambda m: None)
        told = calls.read_text().strip()
        dw.CONFIG.write_text(json.dumps({"screen_mode": "800x480"}))
        bad = dw.step(90, log=lambda m: None)
        saved = json.loads(dw.STATE.read_text())
    finally:
        os.environ["PATH"] = path
        dw.CONFIG, dw.STATE = real_cfg, real_state
    assert (
        quiet == ""
        and told == "--output HDMI-A-1 --mode 1024x600@60Hz --transform 180"
        and "not one of this display's modes" in (bad or {}).get("error", "")
        and saved.get("modes")
    ), (
        f"it changes only what differs from Settings ({told!r}), refuses a mode the display "
        "lacks, and writes what it sees for Settings to show"
    )
    # the display off while the lights are off, back on for a touch or a light
    lit = {
        "brightness": 0,
        "light2": {"level": 0},
        "setups": [{"light": "main"}, {"light": "second"}],
    }
    sleep_cfg = {"screen_night": "sleep"}
    assert (
        dw.lights_off(lit)
        and not dw.lights_off(dict(lit, brightness=40))
        and not dw.lights_off(dict(lit, light2={"level": 20}))
        and not dw.lights_off(None)
        and dw.want_on({"screen_night": "dim"}, lit, 100, 0)
        and not dw.want_on(sleep_cfg, lit, 100, 0)
        and dw.want_on(sleep_cfg, lit, 100, 150)
        and dw.want_on(sleep_cfg, dict(lit, brightness=5), 100, 0)
        and dw.parse(sample.replace("Enabled: yes", "Enabled: no"))["enabled"] is False
        and dw.touch_device(
            'I: Bus=0003\nN: Name="mouse"\nH: Handlers=mouse0 event3\nB: ABS=3\n\n'
            'I: Bus=0003\nN: Name="wch.cn USB2IIC_CTP_CONTROL"\n'
            "H: Handlers=mouse1 event0\nB: ABS=2608000 3\n"
        )
        == "/dev/input/event0"
    ), (
        "with 'turn the display off', the screen goes dark while every light is off and "
        "comes on for a light or a touch (the panel read directly, found by its touch bits)"
    )
    path = os.environ["PATH"]
    os.environ["PATH"] = f"{d}:{path}"
    dw.CONFIG, dw.STATE = d / "config.json", d / "display_state.json"
    try:
        calls.write_text("")
        dw.CONFIG.write_text(json.dumps({"screen_night": "sleep"}))
        dw.step(90, log=lambda m: None, status=lit, woke_until=0, now=100)
        went_off = calls.read_text().strip()
    finally:
        os.environ["PATH"] = path
        dw.CONFIG, dw.STATE = real_cfg, real_state
    assert went_off == "--output HDMI-A-1 --off", (
        f"the watcher turns the output off when it should be dark ({went_off!r})"
    )
    sess = (APP / "scripts" / "kiosk-session.sh").read_text()
    html = c.get("/").get_data(as_text=True)
    js = page_js()
    sjs = Code((APP / "static" / "screen.js").read_text())
    r = c.get("/api/display").get_json()
    lite = c.get("/api/status?lite=1").get_json()["settings"]
    assert (
        'display_watch.py" &' in sess
        and all(
            f'name="{k}"' in html
            for k in (
                "screen_rotate",
                "screen_mode",
                "screen_chart_s",
                "screen_night",
                "screen_back_min",
            )
        )
        and "/api/display" in js
        and "screen_back_min" in js
        and "screen_chart_" in sjs
        and "screen_night" in sjs
        and "kiosk" in r
        and lite.get("screen_chart_s") == 15
        and lite.get("screen_night") == "off"
    ), (
        "Settings has an External display section (rotation, mode, chart timing and choice, "
        "the dashboard's return time, a night screen), and the summary gets those settings"
    )
    scr = c.get("/screen").get_data(as_text=True)
    sb = (APP / "static" / "screen_buddy.js").read_text()
    assert (
        "/static/buddy.css" in scr
        and 'type="module" src="/static/screen_buddy.js' in scr
        and "buddy.css" in html
        and "set_walkBoxes('.tile')" in sb
        and "screen_buddy" in sb
        and ".walker {" not in (APP / "static" / "style.css").read_text()
        and 'name="screen_buddy"' in html
    ), (
        "Little Buddy walks on the touchscreen summary too, from the dashboard's own "
        "module and one shared stylesheet, and can be kept off the screen"
    )
    was = config.TRUST_LOCALHOST
    try:
        config.TRUST_LOCALHOST = True
        local = c.get("/screen", **ON_SCREEN).get_data(as_text=True)
        local_dash = c.get("/", **ON_SCREEN).get_data(as_text=True)
        ok_tap = c.post(
            "/api/screen_event",
            json={"kind": "tap", "x": 5, "y": 7, "w": 600, "h": 1024, "native": False},
            **ON_SCREEN,
        ).status_code
        proxied = c.post("/api/screen_event", json={"kind": "tap"}).status_code  # main port
        config.TRUST_LOCALHOST = False
        plain = c.get("/screen", **ON_SCREEN).get_data(as_text=True)
    finally:
        config.TRUST_LOCALHOST = was
    assert (
        "/static/screen_touch.js" in local
        and "/static/screen_touch.js" in local_dash
        and "/static/screen_touch.js" not in plain
        and ok_tap == 200
        and proxied == 403
        and "/api/screen_event" in routes.NOT_CHANGES
    ), (
        "pages for the Pi's own screen click what a tap lands on when the browser does not, "
        "and report the first taps to the journal (only from that screen)"
    )
    good_cal = config._v_touch_cal({"m": [-6.6, 0, 990, 0, 21.7, -650], "at": 1})
    refused = []
    for v in ({"m": [1, 2, 3]}, {"m": [1, 0, 0, 0, 1, "x"]}, {"m": [999, 0, 0, 0, 1, 0]}):
        try:
            config._v_touch_cal(v)
        except ValueError:
            refused.append(v)
    with config.settings_lock:
        config.settings["screen_touch_cal"] = good_cal
    was = config.TRUST_LOCALHOST
    try:
        config.TRUST_LOCALHOST = True
        page_cal = c.get("/screen", **ON_SCREEN).get_data(as_text=True)
    finally:
        config.TRUST_LOCALHOST = was
        with config.settings_lock:
            config.settings["screen_touch_cal"] = {}
    tj = (APP / "static" / "screen_touch.js").read_text()
    assert (
        good_cal["m"][0] == -6.6
        and config._v_touch_cal({}) == {}
        and len(refused) == 3
        and '<script id="touchcal" type="application/json">' in page_cal
        and '"m": [-6.6' in page_cal.replace("[-6.6,", "[-6.6")
        and "HOLD_MS = 3000" in tj
        and 'id="touchcalreset"' in c.get("/").get_data(as_text=True)
    ), (
        "touch calibration: a hold on the screen fits a correction, saved as six numbers "
        "(checked), handed to the screen's pages, and reset from Settings"
    )
    # touch-rotate: the eight ways a touch layer can sit over its picture
    sh_k = (APP / "scripts" / "kiosk.sh").read_text()
    mats = dict(re.findall(r'\n\s+(\d+(?:-flip)?)\)\s+m="([-\d ]*)"', sh_k))
    maps = []
    for _name, m in sorted(mats.items()):
        a, b, cc, d, e, f = [float(v) for v in m.split()] if m else (1, 0, 0, 0, 1, 0)
        maps.append(
            tuple(
                (round(a * x + b * y + cc, 3), round(d * x + e * y + f, 3))
                for x, y in ((0, 0), (1, 0), (1, 1), (0, 1))
            )
        )
    corners = {(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)}
    assert (
        len(mats) == 8
        and all(set(mp) == corners for mp in maps)
        and len(set(maps)) == 8
        and "systemctl kill -s KILL growlight-kiosk" in sh_k
    ), (
        f"touch-rotate offers all eight orientations, rotations and mirror images, each a "
        f"distinct mapping of the screen onto itself ({sorted(mats)})"
    )
    try:
        config._v_screen_mode("1024x600@60")
        config._v_screen_mode("")
        config._v_screen_mode("big")
        ok = False
    except ValueError:
        ok = True
    assert ok, "a display mode must look like 1024x600 or 1024x600@60, or be blank"
