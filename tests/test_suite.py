#!/usr/bin/env python3
"""openSeedling test suite: one file, standard library only, no hardware.

    python3 tests/test_suite.py            # from the repo
    python3 tests/test_suite.py /path/to/repo

Needs the app's Python packages (Flask, astral); run it with the app's venv
or after `pip install -r requirements.txt`. About 20 seconds.

Safe anywhere, including on the Pi next to the running service: the app is
copied to a temp directory first (your config.json, .env, .secret, database
and photos are never read or written), GPIO and PWM are replaced with fakes,
and the I2C sensor libraries are blocked so nothing touches the bus. No port
is opened; requests go through Flask's test client.

Exit status is the number of failed checks (0 = all passed).
"""

import ast
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
from datetime import date, datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------
# locate the repo and copy it somewhere disposable

def find_repo():
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).resolve()
    here = Path(__file__).resolve().parent
    for p in (here, here.parent):
        if (p / "growlight.py").exists():
            return p
    sys.exit("growlight.py not found; pass the repo path as an argument")


REPO = find_repo()
WORK = Path(tempfile.mkdtemp(prefix="openseedling-test-"))
APP = WORK / "app"
# growlight.py is the entry point; the app's code lives in these modules
APP_MODULES = ("growlight.py", "config.py", "hardware.py", "light.py", "setups.py",
               "water.py", "monitor.py", "heat.py", "camera.py", "status.py", "routes.py")


def app_source():
    """Every app module's source, for checks that look at the code itself."""
    return "\n".join((APP / f).read_text() for f in APP_MODULES)

_skip = shutil.ignore_patterns(
    ".git", "venv", "__pycache__", "tests", "timelapse", "timelapse_archive",
    "growlight.db*", "config.json", ".env", ".secret", "ai_report.json",
    "*.jpg", "*.mp4", "*.log", ".lgd-*")


def _ignore(src, names):
    """The patterns above, plus anything that is not a plain file or folder:
    lgpio leaves named pipes (.lgd-nfy*) in the running service's directory,
    and copying a pipe fails (or blocks)."""
    skip = set(_skip(src, names))
    for n in names:
        p = os.path.join(src, n)
        if not (os.path.isfile(p) or os.path.isdir(p)):
            skip.add(n)
    return skip


shutil.copytree(REPO, APP, ignore=_ignore, ignore_dangling_symlinks=True)

# --------------------------------------------------------------------------
# hardware fakes, installed before the app is imported

PWM_WRITES = []          # (channel, duty) for every hardware PWM write


class FakeHardwarePWM:
    def __init__(self, pwm_channel=0, hz=1000, chip=0):
        self.ch, self.duty = pwm_channel, None

    def start(self, d):
        self.duty = d
        PWM_WRITES.append((self.ch, d))

    change_duty_cycle = start

    def change_frequency(self, hz):
        pass

    def stop(self):
        PWM_WRITES.append((self.ch, "stop"))


class FakeDevice:
    def __init__(self, pin, *a, **k):
        self.pin = pin

    def close(self):
        pass


class FakeOutput(FakeDevice):
    def __init__(self, pin, active_high=True, initial_value=False, **k):
        super().__init__(pin)
        self.value = 1 if initial_value else 0

    def on(self):
        self.value = 1

    def off(self):
        self.value = 0


class FakePWMOutput(FakeDevice):
    def __init__(self, pin, frequency=100, initial_value=0, **k):
        super().__init__(pin)
        self.value = initial_value


class FakeButton(FakeDevice):
    """is_pressed True = switch closed. For a float that means NOT full."""
    def __init__(self, pin, pull_up=True, bounce_time=None, **k):
        super().__init__(pin)
        self.is_pressed = True
        self.when_pressed = self.when_released = None


sys.modules["rpi_hardware_pwm"] = types.SimpleNamespace(HardwarePWM=FakeHardwarePWM)
sys.modules["gpiozero"] = types.SimpleNamespace(
    OutputDevice=FakeOutput, PWMOutputDevice=FakePWMOutput, Button=FakeButton)
# None in sys.modules makes the import raise ImportError: sensors.py then
# treats every I2C device as absent, so the real bus is never opened.
for mod in ("board", "busio", "adafruit_extended_bus", "adafruit_ads1x15",
            "adafruit_ads1x15.ads1115", "adafruit_ads1x15.analog_in",
            "adafruit_bus_device", "adafruit_bus_device.i2c_device",
            "adafruit_bme280", "adafruit_bmp280", "adafruit_bh1750"):
    sys.modules[mod] = None

os.chdir(APP)
sys.path.insert(0, str(APP))
# a config written by an older version, to check the model migration
(APP / "config.json").write_text('{"ai_model": "claude-opus-4-8", '
                                 '"probe_names": {"1": "Tray 1", "2": "Left bench"}, "auto_water": true}')

# --------------------------------------------------------------------------
# reporting

FAILS = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def skip(msg):
    print("  SKIP  " + msg)


def section(name):
    print(f"\n{name}")


# --------------------------------------------------------------------------
section("Code")

bad = []
for p in sorted(APP.rglob("*.py")):
    try:
        ast.parse(p.read_text(), str(p))
    except SyntaxError as e:
        bad.append(f"{p.relative_to(APP)}:{e.lineno}")
check(not bad, "every Python file parses" + (f" {bad}" if bad else ""))

js = (APP / "static" / "app.js").read_text()
posts = re.findall(r"fetch\((['\"`][^'\"`]+['\"`])\s*,\s*\{method:'POST'(.{0,160})", js, re.S)
no_json = [u for u, rest in posts if "application/json" not in rest]
check(posts and not no_json, f"every dashboard POST sends JSON ({len(posts)} calls)" + (f" {no_json}" if no_json else ""))
gl = app_source()
check("str(VIDEO_PATH)]" not in gl and "os.replace(part, VIDEO_PATH)" in gl,
      "timelapse is written beside the old video and swapped in, never rewritten in place")
check(re.search(r"async function doLogin[\s\S]{0,600}restartStream\(\)", js) is not None
      and re.search(r"async function doLogout[\s\S]{0,400}restartStream\(\)", js) is not None,
      "login and logout reopen the live stream")

# --------------------------------------------------------------------------
section("Import")

from werkzeug.security import generate_password_hash   # noqa: E402
from zoneinfo import ZoneInfo                          # noqa: E402

import growlight          # noqa: E402  (imports every module, in order)
import config             # noqa: E402
import hardware           # noqa: E402
import light as light_mod     # noqa: E402
import setups as setups_mod   # noqa: E402
import water              # noqa: E402
import monitor            # noqa: E402
import heat               # noqa: E402
import camera as camera_mod   # noqa: E402
import status as status_mod   # noqa: E402
import routes             # noqa: E402
import db                 # noqa: E402
import sensors            # noqa: E402

db.init()
check(Path(db.DB_PATH).parent == APP, "database lives in the temp copy, not the repo")
tz = ZoneInfo(config.settings["timezone"])
now = datetime.now(tz)
config.state.update(on=now.replace(hour=7), off=now.replace(hour=19),
               sunrise=now.replace(hour=6), sunset=now.replace(hour=19),
               brightness=0, override="auto")
c = routes.app.test_client()
check(c.get("/api/status").status_code == 200, "app imports and /api/status answers")

# Checks below swap functions and constants for fakes on the module that owns
# them. That only works if nothing else holds its own copy of the name (say a
# "from light import set_brightness"), or the fake is never called and the
# check passes without testing anything.
_mods = {"config": config, "hardware": hardware, "light_mod": light_mod,
         "setups_mod": setups_mod, "water": water, "monitor": monitor,
         "camera_mod": camera_mod, "status_mod": status_mod, "routes": routes, "heat": heat}
_patched = set()
for _n in ast.walk(ast.parse(Path(__file__).read_text())):
    if isinstance(_n, ast.Assign):
        for _t in _n.targets:
            for _e in (_t.elts if isinstance(_t, ast.Tuple) else [_t]):
                if (isinstance(_e, ast.Attribute) and isinstance(_e.value, ast.Name)
                        and _e.value.id in _mods):
                    _patched.add((_e.value.id, _e.attr))
_copies = [f"{a}.{n} also in {o}" for a, n in sorted(_patched)
           for o, m in list(_mods.items()) + [("growlight", growlight)]
           if o != a and n in vars(m)]
_unowned = [f"{a}.{n}" for a, n in sorted(_patched) if n not in vars(_mods[a])]
check(len(_patched) >= 10 and not _copies and not _unowned,
      f"every name the suite patches ({len(_patched)}) lives in one module only"
      + (f" {_copies + _unowned}" if _copies or _unowned else ""))
check(config.settings.get("auto_water_trays") == ["1", "2"],
      "an old config with auto-water on arms every pump tray once")
with config.settings_lock:
    config.settings.update(auto_water=False, auto_water_trays=[])
check(config.settings.get("ai_model") == config.DEFAULTS["ai_model"] != "claude-opus-4-8",
      f"a stored former-default AI model is moved to the current one ({config.settings.get('ai_model')})")
import ai_report          # noqa: E402
check("magenta/pink LED" not in ai_report.PROMPT and "tint" in ai_report.PROMPT,
      "AI prompt does not assume the light's color")

# a password check fast enough for tests (the real one is scrypt)
FAST_HASH = generate_password_hash("pw", method="pbkdf2:sha256:1000")


def set_password(on):
    with config.settings_lock:
        config.settings["password_hash"] = FAST_HASH if on else ""

def _sec0():
    global active, floats, calls
    errors = []
    for rule in routes.app.url_map.iter_rules():
        if rule.endpoint == "static" or "GET" not in rule.methods or rule.rule == "/api/stream":
            continue
        path = re.sub(r"<[^>]+>", "x.jpg", rule.rule)
        q = {"/api/series": "?sensor=lux", "/api/frame_context": "?ts=1"}.get(path, "")
        code = c.get(path + q).status_code
        if code >= 500:
            errors.append(f"GET {path} {code}")
    check(not errors, "every GET route answers without a 5xx" + (f" {errors}" if errors else ""))

    mutating = [r.rule for r in routes.app.url_map.iter_rules()
                if "POST" in r.methods and r.rule not in ("/api/login", "/api/logout")]
    leaks = [p for p in mutating
             if c.post(p, data="x", content_type="text/plain").status_code != 415]
    check(not leaks, f"all {len(mutating)} mutating routes refuse non-JSON with 415" + (f" {leaks}" if leaks else ""))


def _sec1():
    global active, floats, calls
    with config.settings_lock:
        config.settings.update(kasa_user="me@example.com", latitude=34.2, longitude=-118.8)
    s = c.get("/api/status").get_json()["settings"]
    check("kasa_user" not in s and "password_hash" not in s, "credentials never in /api/status")
    check("latitude" in s, "no password set: location shown")

    set_password(True)
    s = c.get("/api/status").get_json()["settings"]
    check("latitude" not in s and "longitude" not in s, "signed out: location hidden")
    check(c.post("/api/pump", json={}).status_code == 401, "signed out: JSON POST gets 401")

    routes.LOGIN_DELAY_S, routes.LOGIN_DELAY_MAX_S = 0.01, 0.2
    real_check = routes.check_password_hash
    active = {"n": 0, "max": 0}
    lk = threading.Lock()


    def tracked(h, pw):
        with lk:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.03)
        try:
            return real_check(h, pw)
        finally:
            with lk:
                active["n"] -= 1


    routes.check_password_hash = tracked
    routes._login_fails["n"] = 0
    threads = [threading.Thread(target=lambda: routes.app.test_client().post(
        "/api/login", json={"password": "wrong"})) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(active["max"] == 1, f"parallel logins are checked one at a time (max {active['max']})")
    check(routes._login_fails["n"] == 6, "failed logins are counted")
    t0 = time.time()
    c.post("/api/login", json={"password": "wrong"})
    check(time.time() - t0 >= 0.15, "delay grows after repeated failures")
    r = c.post("/api/login", json={"password": "pw"})
    check(r.status_code == 200 and routes._login_fails["n"] == 0, "right password signs in and resets the count")
    routes.check_password_hash = real_check
    s = c.get("/api/status").get_json()
    check(s["authed"] and "latitude" in s["settings"], "signed in: location shown")
    c.post("/api/logout", json={})
    set_password(False)


def _sec2():
    global active, floats, calls
    with routes.app.test_request_context("/api/stream"):
        resp = routes.api_stream()
    gen = iter(resp.response)
    first, second = next(gen), next(gen)
    check(first.startswith("retry") and "event: status" in second, "stream opens with a status")
    with status_mod._subs_lock:
        status_mod._subs.clear()            # what publish() does to a subscriber that fell behind
    try:
        next(gen)
        ended = False
    except StopIteration:
        ended = True
    check(ended, "a dropped subscriber's stream ends instead of idling")

    # three open tabs, one change: the status is built once and shared
    gens = []
    for _ in range(3):
        with routes.app.test_request_context("/api/stream"):
            gi = iter(routes.api_stream().response)
        next(gi); next(gi)                    # retry line + the fresh status
        gens.append(gi)
    builds = {"n": 0}
    real_sp = status_mod.status_payload

    def counting(*a, **k):
        builds["n"] += 1
        return real_sp(*a, **k)
    status_mod.status_payload = counting
    try:
        status_mod.publish("test")
        texts = [next(gi) for gi in gens]
    finally:
        status_mod.status_payload = real_sp
    check(builds["n"] == 1 and len(set(texts)) == 1,
          f"one change is rendered once for all open tabs ({builds['n']} builds for 3 tabs)")
    for gi in gens:
        gi.close()


def _sec3():
    global active, floats, calls
    floats = sensors._floats()


    def fill(event, cap=3):
        """Run a fill on tray 1; `event(res)` fires 0.4 s in to change the world."""
        for st in hardware.pump_state.values():
            st.update(running=False, today_seconds=0, day=config._today_str())
        with config.settings_lock:
            config.settings.update(fill_max_seconds=cap, auto_water=True, auto_water_trays=["1", "2"])
        floats["1"].is_pressed = True                 # not full yet
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
        return ok, why, hardware.pump_state["1"]["last_detail"], "1" in water.armed_trays(config.settings)


    ok, why, detail, _ = fill(lambda res: setattr(floats["1"], "is_pressed", False))
    check(ok and "full at" in detail, f"fill stops when the float trips ({detail})")
    check(not hardware._pumps["1"].value, "pump is off after the fill")

    ok, why, detail, armed = fill(lambda res: res.update(reservoir="empty"))
    check(not ok and "reservoir ran empty" in detail, f"fill stops when the reservoir runs dry ({detail})")
    check(not armed and water.armed_trays(config.settings) == ["2"],
      "a failed fill disarms that tray only; the other stays armed")
    check(not hardware._pumps["1"].value, "pump is off after a reservoir stop")

    ok, why, detail, _ = fill(lambda res: None, cap=1)
    check(not ok and "cap" in detail, f"fill stops at the time cap ({detail})")

    ok, why, detail, _ = fill(lambda res: floats.pop("1", None))
    check(not ok and "float sensor stopped answering" in detail, f"lost float reported as such ({detail})")
    check(not hardware._pumps["1"].value, "pump is off after a lost float")


def _sec4():
    global active, floats, calls
    nowi = int(time.time())
    errs = []


    def writer(n):
        try:
            for i in range(100):
                db.log_many([(f"test:w{n}", i)], ts=nowi - 90000 - i)
                db.kv_set(f"test:{n}", i)
        except Exception as e:
            errs.append(e)


    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = db._c().execute("SELECT COUNT(*) FROM readings WHERE sensor LIKE 'test:w%'").fetchone()[0]
    check(not errs and got == 600, f"six threads writing at once lose nothing ({got}/600)")

    plan = " ".join(str(tuple(r)) for r in db._c().execute(
        "EXPLAIN QUERY PLAN SELECT value, ABS(ts-1) d FROM readings "
        "WHERE sensor='lux' AND ts BETWEEN 0 AND 5 ORDER BY d LIMIT 1"))
    check("USING INDEX" in plan, "reading lookups by time use the index")

    db.log_many([("probe:1", 1.5), ("temp:soil", 30.0)], ts=nowi - 7200)   # 86 F then
    db.log_many([("probe:1", 1.5), ("temp:soil", 20.0)], ts=nowi - 60)     # 68 F now
    with config.settings_lock:
        config.settings["probe_cal"] = {"1": {"wet": 1.0, "dry": 2.2,
                                         "temp_comp": {"coeff": 0.01, "ref_f": 70}}}
    pts = dict(map(tuple, c.get("/api/series_all?hours=3").get_json()["series"]["probe:1"]))
    old, new = pts.get(nowi - 7200), pts.get(nowi - 60)
    check(old is not None and abs(old - 1.34) < 1e-6 and abs(new - 1.52) < 1e-6,
          f"each chart point is corrected with the soil temperature of its time ({old}, {new})")

    check(sensors.LIVE_READS == (sensors._read_air, sensors._read_lux),
          "live refresh reads only air and light")


def _sec5():
    global active, floats, calls
    cfg = dict(config.settings, latitude=78.2, longitude=15.6, schedule_mode="fixed",
               fixed_on="07:00", fixed_off="19:00")
    try:
        _, _, on, off = light_mod.sun_window(cfg, date(2026, 6, 21), ZoneInfo("Arctic/Longyearbyen"))
        polar = (on.hour, off.hour) == (7, 19)
    except Exception:
        polar = False
    check(polar, "fixed schedule works at a polar latitude in midsummer")

    # Last: this starts the real control loop thread, which keeps running.
    calls = {"n": 0}
    real_sw = light_mod.sun_window


    def flaky(cfg_, day, tz_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected test error (expected in the log)")
        return real_sw(cfg_, day, tz_)


    light_mod.sun_window = flaky
    light_mod.LOOP_SECONDS = 0.1
    t = threading.Thread(target=light_mod.control_loop, daemon=True)
    t.start()
    time.sleep(0.8)
    check(t.is_alive() and calls["n"] >= 2, "control loop survives an error and retries")
    light_mod.sun_window = real_sw


def _dli_band():
    cfg0 = c.get("/api/status").get_json()["settings"]
    check((cfg0.get("dli_target_low"), cfg0.get("dli_target_high")) == (10.0, 15.0),
          "default seedling DLI target is 10-15 and reaches the dashboard")
    r = c.post("/api/settings", json={"dli_target_low": 18, "dli_target_high": 12}).get_json()
    check(not r["ok"] and "dli_target_low" in r["errors"] and setups_mod.dli_target() == (10.0, 15.0),
          "a DLI target with low above high is refused and nothing changes")
    r = c.post("/api/settings", json={"dli_target_low": 10, "dli_target_high": 14}).get_json()
    check(r["ok"] and setups_mod.dli_target() == (10.0, 14.0), "a valid DLI target saves")
    real_md = setups_mod.measured_day
    setups_mod.measured_day = lambda *a, **kw: {"mol": 12.0, "lit_hours": 12.0, "day": "yesterday"}
    try:
        in_band = setups_mod.light_plan(dict(config.settings), None, None)["status"]
        c.post("/api/settings", json={"dli_target_low": 15, "dli_target_high": 20})
        below = setups_mod.light_plan(dict(config.settings), None, None)
    finally:
        setups_mod.measured_day = real_md
    check(in_band == "ok" and below["status"] == "low" and any("15 mol" in a for a in below["advice"]),
          "the Plan verdict and advice follow the configured band (12 mol: in 10-14, short of 15-20)")
    import alerts
    alerts.reset()
    out = alerts.check_all({"_dli": 3.0}, {"dli_low": 4, "dli_target": (15.0, 20.0)})
    msg = " ".join(str(x) for x in out)
    check("15-20" in msg and "6-12" not in msg, "the short-day alert quotes the configured band")
    ctx = ai_report.build_context({"light_metrics": {"ppfd": 200, "dli": 9.0, "dli_target": [15, 20]}})
    check("15-20" in ctx and "6-12" not in ctx, "the AI report is told the configured band")
    stale = [f for f in ("static/app.js", "templates/index.html", *APP_MODULES,
                         "alerts.py", "ai_report.py")
             if re.search(r"\b6-12\b", (APP / f).read_text())]
    check(not stale, "no hardcoded 6-12 band left" + (f" {stale}" if stale else ""))


def _camera_flatten():
    js = (APP / "static" / "app.js").read_text()
    html = (APP / "templates" / "index.html").read_text()
    check('name="timelapse_flatten"' in html and "timelapse_flatten===false" in js,
          "the snapshot's flattening follows a visible setting")
    check(js.count("'/thumb/'+frames[") == 2 and "?v='+thumbsV" in js,
          "scrubber thumbnail URLs carry a version, so browsers drop cached ones")
    camera_mod.TIMELAPSE_DIR.mkdir(parents=True, exist_ok=True)
    camera_mod.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (camera_mod.TIMELAPSE_DIR / f"2026092{i}_120000.jpg").write_bytes(b"photo")
        (camera_mod.THUMB_DIR / f"2026092{i}_120000.jpg").write_text("flat")
    real_mt = camera_mod.make_thumb

    def fake_thumb(ph, cfg=None, dst_dir=None):
        time.sleep(0.05)
        ((dst_dir or camera_mod.THUMB_DIR) / ph.name).write_text(
            "flat" if (cfg or {}).get("timelapse_flatten", True) else "raw")
    camera_mod.make_thumb = fake_thumb
    counts = []
    try:
        v0 = c.get("/api/photos").get_json().get("v", 0)
        with config.settings_lock:
            config.settings["timelapse_flatten"] = True
        c.post("/api/settings", json={"timelapse_flatten": False})
        for _ in range(40):
            counts.append(len(c.get("/api/photos").get_json()["names"]))
            if not camera_mod._thumbs_lock.locked() and counts[-1] and \
               c.get("/api/photos").get_json().get("v", 0) != v0:
                break
            time.sleep(0.05)
        kinds = {p.read_text() for p in camera_mod.THUMB_DIR.glob("*.jpg")}
        v1 = c.get("/api/photos").get_json().get("v", 0)
    finally:
        camera_mod.make_thumb = real_mt
    check(kinds == {"raw"}, f"turning flattening off rebuilds the thumbnails raw ({kinds})")
    check(min(counts) == 3, f"the scrubber's frame list never empties during a rebuild (min {min(counts)})")
    check(v1 != v0, "the thumbnail version changes after a rebuild")


def _camera_preview():
    seen = {}

    def fake_usb(cfg, out, w, h, warmup=None):
        seen["size"] = (w, h)
        Path(out).write_bytes(b"\xff\xd8\xff\xd9")
        return True, ""
    real = camera_mod._usb_capture
    camera_mod._usb_capture = fake_usb
    try:
        with config.settings_lock:
            config.settings.update(camera_enabled=True, camera_backend="usb",
                              usb_width=2048, usb_height=1536)
        c.post("/api/preview", json={})
    finally:
        camera_mod._usb_capture = real
    check(seen.get("size") == (2048, 1536),
          f"the align preview uses the photo's own camera mode ({seen.get('size')}), so it shows the same view")


def _camera_modes_and_reset():
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
    check(camera_mod.parse_mjpeg_modes(sample) == [(3264, 2448), (2048, 1536), (1280, 720)],
          "camera modes are read from v4l2-ctl, MJPEG only, largest first")
    with config.settings_lock:
        config.settings.update(usb_width=2048, usb_height=1536, roi="0.1,0.1,0.5,0.5")
    # the settings form resubmits the crop unchanged along with a new size
    r = c.post("/api/settings", json={"usb_width": 3264, "usb_height": 2448,
                                      "roi": "0.1,0.1,0.5,0.5"}).get_json()
    check(r.get("crop_reset") and config.settings["roi"] == "",
          "changing the capture size resets the crop, since each size frames a different view")
    r = c.post("/api/settings", json={"usb_width": 2048, "usb_height": 1536,
                                      "roi": "0.2,0.2,0.4,0.4"}).get_json()
    check(not r.get("crop_reset") and config.settings["roi"] == "0.2,0.2,0.4,0.4",
          "a crop drawn in the same save as a new size is kept")
    js = (APP / "static" / "app.js").read_text()
    html = (APP / "templates" / "index.html").read_text()
    check('id="cropreset"' in html and "getElementById('cropreset')" in js,
          "a Reset crop button sits beside Crop when a crop is set")
    check(re.search(r"function startCrop[\s\S]{0,900}/api/preview", js) is not None,
          "Crop starts from a live full camera frame, not the cropped photo")
    c.post("/api/settings", json={"roi": ""})


def _camera_crop():
    check(camera_mod.crop_box({"roi": "0.1,0.2,0.5,0.6"}) == (0.1, 0.2, 0.5, 0.6)
          and camera_mod.crop_box({"roi": ""}) is None and camera_mod.crop_box({"roi": "junk"}) is None,
          "the crop setting parses, and blank or bad means full frame")
    gl = app_source()
    check('"--roi"' not in gl, "photos are always stored full frame (no capture-time crop)")
    calls = {"n": 0}
    real_rb = camera_mod.rebuild_thumbs_async
    camera_mod.rebuild_thumbs_async = lambda: calls.__setitem__("n", calls["n"] + 1)
    try:
        r = c.post("/api/settings", json={"roi": "0.1,0.2,0.5,0.6"}).get_json()
        bad = c.post("/api/settings", json={"roi": "0.9,0.9,0.5,0.5"}).get_json()
    finally:
        camera_mod.rebuild_thumbs_async = real_rb
    check(r["ok"] and calls["n"] == 1, "saving a crop rebuilds the thumbnails")
    check(not bad["ok"] and "roi" in bad["errors"] and config.settings["roi"] == "0.1,0.2,0.5,0.6",
          "a crop running off the frame is refused and the old one kept")
    try:
        import cv2
        import numpy as np
    except Exception:
        skip("cropped snapshot, thumbnail and AI image (OpenCV not installed here)")
        return
    img = np.zeros((600, 1000, 3), np.uint8)
    for n in ("20260925_120000.jpg",):
        cv2.imwrite(str(camera_mod.TIMELAPSE_DIR / n), img)
    snap = c.get("/photo/cropped.jpg")
    shape = None
    if snap.status_code == 200:
        shape = cv2.imdecode(np.frombuffer(snap.data, np.uint8), 1).shape[:2]
    check(shape == (360, 500), f"the snapshot is served cut to the crop ({shape}, want (360, 500))")
    import base64
    b = base64.b64decode(ai_report._image_b64(camera_mod.TIMELAPSE_DIR / "20260925_120000.jpg", (0.1, 0.2, 0.5, 0.6)))
    ai_shape = cv2.imdecode(np.frombuffer(b, np.uint8), 1).shape[:2]
    check(ai_shape == (360, 500), f"the AI report gets the cropped photo ({ai_shape})")
    if shutil.which("ffmpeg"):
        with config.settings_lock:
            cfgc = dict(config.settings, timelapse_flatten=False)
        out = WORK / "thumbtest"
        out.mkdir(exist_ok=True)
        camera_mod.make_thumb(camera_mod.TIMELAPSE_DIR / "20260925_120000.jpg", cfgc, dst_dir=out)
        t = cv2.imread(str(out / "20260925_120000.jpg"))
        check(t is not None and t.shape[:2] == (460, 640),
              f"raw thumbnails are cut to the crop ({None if t is None else t.shape[:2]}, want (460, 640))")
    else:
        skip("cropped thumbnail (ffmpeg not installed here)")
    c.post("/api/settings", json={"roi": ""})



def _timelapse_sharp():
    js = (APP / "static" / "app.js").read_text()
    css = (APP / "static" / "style.css").read_text()
    show = re.search(r"function showFrame\(\)\{[\s\S]*?\n\}", js)
    stop = re.search(r"function stopPlay\(\)\{[\s\S]*?\n\}", js)
    sharp = re.search(r"function loadSharpFrame\(\)\{[\s\S]*?\n\}", js)
    check(show and "loadSharpFrame()" in show.group(0) and stop and "loadSharpFrame()" in stop.group(0)
          and sharp and "if(ptimer" in sharp.group(0) and "'/frame/'" in sharp.group(0),
          "a paused or scrubbed-to frame swaps in the full-size photo; playback stays on thumbnails")
    rule = re.search(r"#captureinfo\{([^}]*)\}", css.split("/* The capture status has its own line")[-1])
    check(rule and "flex:1 0 100%" in rule.group(1) and re.search(r"(^|;)\s*height:", rule.group(1))
          and "text-overflow:ellipsis" in rule.group(1),
          "the capture status has a fixed line of its own, so its text never moves the card")
    for bad in ("config.json", "_flat.jpg", "missing.jpg"):
        if c.get(f"/frame/{bad}").status_code != 404:
            check(False, f"/frame refuses {bad}")
            break
    else:
        check(True, "/frame serves only stored photos (config.json, _scratch and missing names are 404)")
    try:
        import cv2
        import numpy as np
    except Exception:
        skip("full-size frame, its framing and caching (OpenCV not installed here)")
        return
    name = "20260926_080000.jpg"
    img = (np.random.default_rng(3).integers(0, 255, (1200, 1600, 3))).astype(np.uint8)
    cv2.imwrite(str(camera_mod.TIMELAPSE_DIR / name), img)
    with config.settings_lock:
        saved = {k: config.settings.get(k) for k in ("grid", "timelapse_flatten", "roi")}
        config.settings.update(timelapse_flatten=True, roi="", grid={
            "corners": [[0.1, 0.1], [0.9, 0.1], [0.95, 0.9], [0.05, 0.9]], "cols": 6, "rows": 4})
        cfgf = dict(config.settings)
    try:
        r = c.get(f"/frame/{name}")
        f = cv2.imdecode(np.frombuffer(r.data, np.uint8), 1) if r.status_code == 200 else None
        out = WORK / "sharptest"
        out.mkdir(exist_ok=True)
        camera_mod.make_thumb(camera_mod.TIMELAPSE_DIR / name, cfgf, dst_dir=out)
        t = cv2.imread(str(out / name))
        ok = (f is not None and t is not None and f.shape[1] > 2 * t.shape[1]
              and abs(f.shape[1] / f.shape[0] - t.shape[1] / t.shape[0]) < 0.01)
        check(ok, "the full-size frame is flattened like its thumbnail, at full resolution "
              f"({None if f is None else f.shape[:2]} vs thumb {None if t is None else t.shape[:2]})")
        tag = r.headers.get("ETag", "").strip('"')
        r304 = c.get(f"/frame/{name}", headers={"If-None-Match": f'"{tag}"'})
        check(tag and r304.status_code == 304 and r.headers.get("Cache-Control") == "no-cache",
              "an unchanged frame is answered 304 without redoing the warp")
        c.post("/api/grid", json={"corners": [[0.2, 0.1], [0.9, 0.1], [0.95, 0.9], [0.05, 0.9]],
                                  "rows": 4, "cols": 6})
        moved = c.get(f"/frame/{name}", headers={"If-None-Match": f'"{tag}"'})
        check(moved.status_code == 200, "moving the grid corners changes the frame's tag, so it is redrawn")
        with config.settings_lock:
            config.settings.update(timelapse_flatten=False, roi="")
        raw = c.get(f"/frame/{name}")
        check(raw.status_code == 200 and raw.data == (camera_mod.TIMELAPSE_DIR / name).read_bytes(),
              "with flattening off and no crop, the frame is the photo itself")
    finally:
        with config.settings_lock:
            config.settings.update(saved)
        (camera_mod.TIMELAPSE_DIR / name).unlink(missing_ok=True)
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
        camera_mod.render_worker()
    finally:
        camera_mod.subprocess.run = real_run
        with config.settings_lock:
            config.settings.update(saved)
        with camera_mod.render_lock:
            camera_mod.render.update(state="idle", msg="", frames=0)
    enc = next((a for a in seen if "libx264" in a), [])
    vf = enc[enc.index("-vf") + 1] if "-vf" in enc else ""
    check(enc and "flags=lanczos" in vf and enc[enc.index("-crf") + 1] == "20"
          and enc[enc.index("-preset") + 1] == "ultrafast" and enc[enc.index("-threads") + 1] == "1",
          f"the video uses a lanczos downscale at crf 20, still ultrafast and one thread ({vf})")


def _lightbox():
    js = (APP / "static" / "app.js").read_text()
    html = (APP / "templates" / "index.html").read_text()
    css = (APP / "static" / "style.css").read_text()
    check(re.search(r'<div id="lightbox"[^>]*role="dialog"[^>]*hidden>', html) and 'id="lbclose"' in html
          and 'aria-label="Close"' in html and re.search(r"\.lightbox\{position:fixed;inset:0;z-index:\d{3,}", css),
          "the enlarged view is a full-screen overlay with a Close (X) button, hidden until used")
    check("document.querySelector('#photocard .imgwrap').addEventListener('click',enlargePhoto)" in js
          and "document.getElementById('vframe').addEventListener('click',enlargeFrame)" in js
          and "dblclick" not in js,
          "a single click (or tap) enlarges the snapshot and the timelapse frame")
    ep = re.search(r"function enlargePhoto\(e\)\{[\s\S]*?\n\}", js)
    ef = re.search(r"function enlargeFrame\(\)\{[\s\S]*?\n\}", js)
    check(ep and "if(cropping||gridEditable())return;" in ep.group(0)
          and "canEdit&&e&&e.target.classList&&e.target.classList.contains('gc')" in ep.group(0)
          and ef and "stopPlay()" in ef.group(0) and "'/frame/'" in ef.group(0),
          "not while editing the grid or a crop, nor on a grid cell a signed-in click names; "
          "a timelapse frame pauses the player and opens full size")
    check("e.key==='Escape')closeLightbox()" in js and "e.target.id==='lightbox'&&Date.now()-lbOpened>500" in js,
          "Esc and a click outside the picture close it; a habitual double click does not close it again")

def _ai_reply():
    import io
    import json as _json
    photo = WORK / "ai.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    sent = {}

    def fake_open(resp):
        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=None):
            sent["body"] = _json.loads(req.data)
            sent["timeout"] = timeout
            return R(_json.dumps(resp).encode())
        return opener
    real_open, real_key = ai_report.urllib.request.urlopen, ai_report.api_key
    ai_report.api_key = lambda: "test-key"
    try:
        ai_report.urllib.request.urlopen = fake_open({
            "content": [{"type": "thinking", "thinking": ""}],
            "stop_reason": "max_tokens", "usage": {"output_tokens": 8000}})
        r1 = ai_report.generate(photo, {})
        ai_report.urllib.request.urlopen = fake_open({
            "content": [{"type": "thinking", "thinking": ""},
                        {"type": "text", "text": '{"summary": "Looking good", "overall_health": "good"}'}],
            "stop_reason": "end_turn"})
        r2 = ai_report.generate(photo, {})
    finally:
        ai_report.urllib.request.urlopen, ai_report.api_key = real_open, real_key
    check(sent["body"]["max_tokens"] >= 8000 and sent["timeout"] >= 180,
          f"the report leaves room for thinking ({sent['body']['max_tokens']} tokens, {sent['timeout']} s)")
    check(not r1["ok"] and "thinking" in r1["error"],
          f"a reply that is all thinking is an error, not an empty report ({r1.get('error')})")
    check(r2["ok"] and r2["report"]["summary"] == "Looking good",
          "thinking blocks before the JSON are skipped")


def _setups():
    st = c.get("/api/status").get_json()
    one = st.get("setups") or []
    check(len(one) == 1 and one[0]["lux"] == "lux" and one[0]["sensors"] == [],
          "with no setups defined there is one, covering every sensor")
    good = [{"name": "Seedlings", "light": "main", "lux": "lux", "sensors": ["temp:soil", "probe:1"],
             "dli_low": 10, "dli_high": 15},
            {"name": "Transplants", "light": "second", "lux": "lux:2", "k": 70,
             "sensors": ["probe:2"], "dli_low": 15, "dli_high": 20},
            {"name": "Shelf", "light": "", "lux": "", "sensors": [], "dli_low": 6, "dli_high": 12}]
    bad_light = [dict(good[0]), dict(good[1], light="main")]
    bad_lux = [dict(good[0]), dict(good[1], lux="lux")]
    bad_band = [dict(good[0], dli_low=15, dli_high=10)]
    errs = [c.post("/api/settings", json={"setups": b}).get_json() for b in (bad_light, bad_lux, bad_band)]
    check(all(not e["ok"] and "setups" in e["errors"] for e in errs),
          "a light or light sensor used twice, or a backwards band, is refused")
    r = c.post("/api/settings", json={"setups": good}).get_json()
    check(r["ok"] and [x["id"] for x in config.settings["setups"]] == ["seedlings", "transplants", "shelf"],
          "three setups save, each with an id")
    now = int(time.time())
    midnight = int(datetime.now(ZoneInfo(config.settings["timezone"])).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    t0 = max(midnight + 60, now - 3 * 3600)
    rows = []
    for t in range(t0, now, 300):
        rows += [("lux", 12000.0, t), ("lux:2", 24000.0, t)]
    for key, v, t in rows:
        db.log_many([(key, v)], ts=t)
    out = {x["id"]: x for x in c.get("/api/status").get_json()["setups"]}
    d1, d2 = (out["seedlings"]["day"] or {}).get("dli"), (out["transplants"]["day"] or {}).get("dli")
    check(d1 and d2 and abs(d2 / d1 - 2 * 60 / 70) < 0.02,
          f"each setup's DLI comes from its own sensor and factor ({d1} vs {d2})")
    cur = ((out["seedlings"]["day"] or {}).get("curve") or {}).get("today") or []
    vals_ = [v for _, v in cur]
    check(len(cur) > 3 and vals_ == sorted(vals_) and abs(vals_[-1] - d1) < 0.2,
          f"the Day card's DLI curve climbs to today's total ({vals_[-1] if vals_ else None} vs {d1})")
    js2 = (APP / "static" / "app.js").read_text()
    check("function setLight2" in js2 and "light2_override" in js2 and "S.schedule_mode==='light2'" in js2,
          "the Light card and schedule chart drive the selected setup's light")
    check('id="dlichart"' in (APP / "templates" / "index.html").read_text(),
          "the Day card has the DLI-against-target chart")
    check(out["transplants"]["band"] == [15.0, 20.0] and out["shelf"]["plan"]["status"] == "no_sensor",
          "each setup keeps its own band; a setup without a light sensor says so")
    import alerts
    alerts.reset()
    acts = alerts.check_all({"_dli_setups": {"seedlings": 3.0, "transplants": 16.0}},
                            {"dli_low": 4, "setups": [{"id": "seedlings", "name": "Seedlings", "band": (10, 15)},
                                                      {"id": "transplants", "name": "Transplants", "band": (15, 20)}]})
    fired = [a[1] for a in acts if a[0] == "fire"]
    check(fired == ["dli_low:seedlings"] and "Seedlings" in acts[0][2],
          "the short-day alert is judged per setup and names it")
    ctx = ai_report.build_context({"light_metrics": {"ppfd": 200, "dli": 5, "setups": [
        {"name": "Seedlings", "dli": 5, "band": [10, 15]}, {"name": "Transplants", "dli": None, "band": [15, 20]}]}})
    check("Seedlings: 5 mol" in ctx and "Transplants: not measured" in ctx,
          "the AI report is told about each setup")
    with config.settings_lock:
        config.settings["light_backend"] = "dim"
    opts = {o["value"]: o["label"] for o in c.get("/api/status").get_json()["light_options"]}
    with config.settings_lock:
        config.settings["light_backend"] = "pwm"
    opts2 = {o["value"]: o["label"] for o in setups_mod.light_options()}
    check(opts.get("main") == "AC fixture (dim line)" and opts2.get("main") == "5V LED panel",
          f"setups list lights by fixture, following the backend ({opts} / {opts2})")
    js = (APP / "static" / "app.js").read_text()
    check("'Main light'" not in js and "lightChoices(" in js,
          "the Setups editor offers fixtures, not 'main' and 'second'")
    check('id="setuptabs"' in (APP / "templates" / "index.html").read_text()
          and "Object.keys(sensorData).filter(inSetup)" in js and "&&inSetup(k)" in js,
          "setup tabs exist and filter the sensor chips and charts")
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
    check(got == {"lux": 111.0, "lux:2": 222.0}, f"two light sensors read as lux and lux:2 ({got})")
    with config.settings_lock:
        config.settings["trays"] = dict(config.settings.get("trays") or {}, T3={"label": "Transplants", "rows": 4, "cols": 5, "cells": {}})
    r = c.post("/api/settings", json={"setups": [dict(good[0], trays=["1", "2", "gone"]),
                                                  dict(good[1], trays=["T3"])]}).get_json()
    got_t = {x["id"]: x["trays"] for x in c.get("/api/status").get_json()["setups"]}
    check(r["ok"] and got_t == {"seedlings": ["1", "2"], "transplants": ["T3"]},
          f"trays are assigned per setup, and a tray that no longer exists drops out ({got_t})")
    js3 = (APP / "static" / "app.js").read_text()
    check("filter(trayInSetup)" in js3 and "trayInSetup(t)?'':'none'" in js3 and 'data-t="' in js3,
          "the planting map, watering rows and the Setups editor follow each setup's trays")
    c.post("/api/settings", json={"setups": []})


def _fan_camera_timing():
    tz = ZoneInfo(config.settings["timezone"])
    base = [{"name": "Seedlings", "light": "second", "lux": "", "sensors": [], "dli_low": 10, "dli_high": 15},
            {"name": "Transplants", "light": "main", "lux": "lux", "sensors": [], "dli_low": 15, "dli_high": 20}]
    two_fans = [dict(base[0], fan=True), dict(base[1], fan=True)]
    e = c.post("/api/settings", json={"setups": two_fans}).get_json()
    check(not e["ok"] and "fan" in e["errors"].get("setups", ""), "the fan can belong to only one setup")
    with config.settings_lock:
        config.settings.update(light2_start="20:00", light2_end="02:00", light2_on=True)
    r = c.post("/api/settings", json={"setups": [dict(base[0], fan=True, camera=True), base[1]]}).get_json()
    cfg = dict(config.settings)
    now = datetime.now(tz)
    mon, moff = now.replace(hour=7, minute=0), now.replace(hour=19, minute=0)
    sd, tr = setups_mod.setups(cfg)
    on2, off2 = setups_mod.setup_window(cfg, sd, mon, moff)
    check(r["ok"] and (on2.hour, off2.hour) == (20, 2) and off2 > on2
          and setups_mod.setup_window(cfg, tr, mon, moff) == (mon, moff),
          "each setup uses its own light's hours; an overnight window ends the next day")
    none_on, none_off = setups_mod.setup_window(cfg, dict(sd, light=""), mon, moff)
    check(none_on == mon and none_off == off2, "a setup with no light spans both lights")
    fon, foff = setups_mod.setup_window(cfg, setups_mod.setup_with(cfg, "fan"), mon, moff)
    want, _ = hardware.fan_should_run(dict(cfg, fan_humidity_on=0), now.replace(hour=21, minute=0), fon, foff)
    gl = app_source()
    check(want and 'setups_mod.setup_window(cfg, setups_mod.setup_with(cfg, "fan")' in gl,
          "the fan runs on its setup's light hours (21:00 under a 20:00-02:00 panel)")
    st = {x["id"]: x for x in c.get("/api/status").get_json()["setups"]}
    check(st["seedlings"]["on"] and datetime.fromisoformat(st["seedlings"]["on"]).hour == 20
          and st["seedlings"]["camera"] and st["seedlings"]["fan"],
          "the status gives each setup its own light window and its fan and camera")
    calls = {"n": 0}
    real_sb, real_usb = light_mod.set_brightness, camera_mod._usb_capture
    light_mod.set_brightness = lambda *a, **k: calls.__setitem__("n", calls["n"] + 1)
    camera_mod._usb_capture = lambda cfg_, out, w, h, warmup=None: (False, "test")
    try:
        with config.settings_lock:
            config.settings["camera_backend"] = "usb"
            config.settings["capture_set_light"] = True   # the bump is opt-in
        camera_mod.take_photo(dict(config.settings), now)
        n_second = calls["n"]
        c.post("/api/settings", json={"setups": [dict(base[0], fan=True), dict(base[1], camera=True)]})
        camera_mod.take_photo(dict(config.settings), now)
        n_main = calls["n"] - n_second
    finally:
        light_mod.set_brightness, camera_mod._usb_capture = real_sb, real_usb
        with config.settings_lock:
            config.settings["capture_set_light"] = False
    check(n_second == 0 and n_main == 1,
          "the capture brightness bump only touches the main light when the camera watches it")
    lm = monitor.gather_report_data()["light_metrics"]
    check(lm["dli_target"] == [15.0, 20.0] and lm["photo_setup"] == "Transplants",
          f"the AI report judges the camera's setup ({lm.get('photo_setup')}, {lm.get('dli_target')})")
    js = (APP / "static" / "app.js").read_text()
    check("camelsewhere" in js and "fanelsewhere" in js and 'data-flag="fan"' in js,
          "the camera cards and fan controls show only on their setup's tab")
    c.post("/api/settings", json={"setups": []})


def _probe_names():
    js = (APP / "static" / "app.js").read_text()
    check("'Soil moisture '+t" in js and "trayLabels[t]||('Tray '+t)" in js,
          "probes read as Soil moisture 1 and 2; canopy and watering rows use the tray's name")
    check(config.settings.get("probe_names") == {"2": "Left bench"},
          f"an old default probe name is cleared, a chosen one kept ({config.settings.get('probe_names')})")
    gl = app_source()
    check('"probe_names": {},' in gl and 'n == f"Tray {t}"' in gl,
          "the old stored probe names (Tray 1, Tray 2) are cleared so the new ones show")


def _per_sensor_controls():
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
    check(a["armed"] == ["1"] and b["armed"] == ["1", "2"] and d["armed"] == ["2"]
          and config.settings["auto_water"] is True,
          "auto-watering arms and disarms per tray (a setup's trays)")
    water.auto_water_blockers = lambda cfg=None: {"2": ["probe not calibrated"]}
    try:
        e = c.post("/api/auto_water", json={"enabled": True, "trays": ["1"]}).get_json()
    finally:
        water.auto_water_blockers = real_b
    check(e["ok"], "a blocker on another setup's tray does not stop arming this one")
    c.post("/api/auto_water", json={"enabled": False})
    # calibrate the second light against its own sensor
    c.post("/api/settings", json={"setups": [
        {"name": "Seedlings", "light": "second", "lux": "lux:2", "sensors": [], "dli_low": 10, "dli_high": 15},
        {"name": "Transplants", "light": "main", "lux": "lux", "sensors": [], "dli_low": 15, "dli_high": 20}]})
    main_before = config.settings.get("light_curve")
    real_read = sensors.read_all
    sensors.read_all = lambda: {"lux": 999.0, "lux:2": 100.0 * float(light_mod.sweep_state.get("l2_raw") or 0)}
    light_mod.sweep_state.update(running=True, cancel=False, error="", target="second", l2_raw=0.0)
    try:
        light_mod.run_light_sweep(step=50, settle=0.0, linearize=False, target="second")
    finally:
        sensors.read_all = real_read
    c2 = config.settings.get("light2_curve") or {}
    check(c2.get("sensor") == "lux:2" and [p[1] for p in c2.get("points", [])] == [0.0, 5000.0, 10000.0]
          and config.settings.get("light_curve") == main_before,
          f"the second light calibrates against its own sensor and keeps its own curve ({c2.get('points')})")
    st = c.get("/api/status").get_json()
    check("light2_cal" in st and st["light2_cal"]["light_curve"]["sensor"] == "lux:2",
          "the Light response card gets the second light's calibration")
    js = (APP / "static" / "app.js").read_text()
    check("linearize:true,light:ctlTarget" in js and "trays:autoWaterTrays" in js
          and "reselsewhere" in js,
          "Calibrate, Arm and the reservoir row follow the tab")
    c.post("/api/settings", json={"setups": []})


def _camera_canopy():
    with config.settings_lock:
        config.settings["trays"] = {"1": {"label": "Seedling 1", "rows": 4, "cols": 3, "cells": {}},
                               "2": {"label": "Seedling 2", "rows": 4, "cols": 3, "cells": {}},
                               "T3": {"label": "Transplants", "rows": 4, "cols": 5, "cells": {}}}
    base = [{"name": "Seedlings", "light": "second", "lux": "", "sensors": [], "trays": ["1", "2"],
             "dli_low": 10, "dli_high": 15},
            {"name": "Transplants", "light": "main", "lux": "lux", "sensors": [], "trays": ["T3"],
             "camera": True, "dli_low": 15, "dli_high": 20}]
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
        camera_mod.record_growth(WORK / "x.jpg", dict(config.settings), datetime.now(ZoneInfo(config.settings["timezone"])))
    finally:
        camera_mod.subprocess.run = real_run
    ids = [t["id"] for t in (seen.get("payload") or {}).get("trays", [])]
    check(ids == ["T3"], f"canopy is measured only for the camera setup's trays ({ids})")
    db.log_many([("canopy:1", 3.1), ("canopy:T3", 7.5)])
    with config.settings_lock:
        config.settings["camera_enabled"] = True
    shown = set(c.get("/api/status").get_json()["sensors"])
    charted = set(c.get("/api/series_all?hours=1").get_json()["series"])
    ai = monitor.gather_report_data().get("canopy") or {}
    check("canopy:T3" in shown and "canopy:1" not in shown and "canopy:1" not in charted
          and list(ai) == ["Transplants"],
          "trays the camera no longer watches drop out of the chips, charts and AI report")
    c.post("/api/settings", json={"setups": []})
    shown2 = set(c.get("/api/status").get_json()["sensors"])
    check("canopy:1" in shown2, "with the camera not assigned, every tray's canopy shows")


def _shutdown():
    """Last: sets the shutdown flag for good, the way SIGTERM does."""
    with config.settings_lock:
        config.settings.update(fill_max_seconds=5, auto_water=True)
    water.fill_failure["msg"] = ""
    for st in hardware.pump_state.values():
        st.update(running=False, today_seconds=0, day=config._today_str())
    fl = sensors._floats()
    if "1" not in fl:                      # the watering checks removed it
        sensors._float_init = False
        sensors._float_devs.clear()
        fl = sensors._floats()
    fl["1"].is_pressed = True              # not full: the fill keeps running
    with routes.app.test_request_context("/api/stream"):
        sg = iter(routes.api_stream().response)
    next(sg); next(sg)
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
    t0 = time.time()
    try:
        hardware.cleanup()
        exited = False
    except SystemExit as e:
        exited = e.code in (0, None)
    t.join(3)
    check(exited, "cleanup finishes with a clean exit")
    time.sleep(0.2)
    check("at" in ended and ended["at"] - t0 < 1.5,
          "an open live stream ends at shutdown instead of holding the server for 5 s")
    ok, why = out.get("r", (True, ""))
    check(not ok and "shutting down" in why and time.time() - t0 < 2.5,
          f"a running fill stops when shutdown starts ({why})")
    check(not hardware._pumps["1"].value, "pump is off after shutdown")
    check(config.settings.get("auto_water") and not water.fill_failure["msg"],
          "a fill cut short by shutdown is not a failure: auto-water stays armed")
    ok, why = water.run_pump("1", 3, "manual")
    check(not ok and "shutting down" in why and not hardware._pumps["1"].value,
          "no pump can start after shutdown begins")
    n = len(PWM_WRITES)
    light_mod.set_brightness_raw(80)
    check(len(PWM_WRITES) == n, "no light write can relight the fixture after shutdown begins")
    if hardware._fan is not None:
        hardware._fan.value = 0
        hardware.set_fan(60, "test")
        check(hardware._fan.value == 0, "fan cannot restart after shutdown begins")



def _report_by_setup():
    """The AI report files every reading under the setup it is in, from the
    setups as saved when the report runs."""
    def blocks(ctx):
        out, cur = {}, None
        for line in ctx.splitlines():
            m = re.match(r'Setup "(.+?)"( \(THE PHOTO SHOWS THIS SETUP\))?:$', line)
            if m:
                cur = m.group(1)
                out[cur] = [line]
            elif line.startswith("Not assigned to one setup"):
                cur = "_shared"
                out[cur] = [line]
            elif cur and line.startswith("  "):
                out[cur].append(line)
            else:
                cur = None
        return {k: "\n".join(v) for k, v in out.items()}

    with config.settings_lock:
        trays = sorted(config.settings.get("trays") or {})
        saved = {k: config.settings.get(k) for k in ("setups", "light2_on", "trays")}
        config.settings.update(light2_on=True)
    if len(trays) < 2:
        check(False, "the report test needs two trays in the default config")
        return
    t1, t2 = trays[0], trays[1]
    lab = {t: (config.settings["trays"][t].get("label") or f"Tray {t}") for t in (t1, t2)}
    db.log_many([("lux", 14000.0), ("temp:air", 24.0), ("humidity", 55.0), ("temp:soil", 26.0),
                 (f"probe:{t1}", 1.3), (f"probe:{t2}", 1.8), ("pressure", 1012.0)])
    seed = {"name": "Seedlings", "light": "second", "lux": "", "trays": [t1],
            "sensors": ["temp:soil"], "camera": True, "reservoir": True, "dli_low": 10, "dli_high": 15}
    tran = {"name": "Transplants", "light": "main", "lux": "lux", "trays": [t2],
            "sensors": ["temp:air", "humidity"], "fan": True, "dli_low": 15, "dli_high": 20}
    try:
        r = c.post("/api/settings", json={"setups": [tran, seed]}).get_json()
        ctx = ai_report.build_context(monitor.gather_report_data())
        b = blocks(ctx)
        sd, tr = b.get("Seedlings", ""), b.get("Transplants", "")
        check(r["ok"] and "THE PHOTO SHOWS THIS SETUP" in (sd.splitlines() or [""])[0] and "Light sensor: none" in sd
              and "PPFD" not in sd and "lx" not in sd and "air " not in sd,
              "the photo's setup is marked, and another setup's light sensor and air are not put under it")
        check(f'Tray "{lab[t1]}": soil moisture' in sd and "soil temperature" in sd
              and "Source reservoir" in sd and "Fan:" not in sd,
              "the seedling setup gets its own tray's probe, its soil temperature and the reservoir")
        check("PPFD" in tr and "10-15" not in tr and "15-20" in tr and "air " in tr and "RH " in tr
              and f'Tray "{lab[t2]}": soil moisture' in tr and "Fan:" in tr and "THE PHOTO" not in tr,
              "the transplant setup gets its light sensor, band, air, humidity, tray and fan")
        check("pressure" in b.get("_shared", "") and "Ambient conditions" not in ctx
              and "Light intensity:" not in ctx and "Soil-probe moisture % per tray" not in ctx,
              "a reading no setup claims is listed as shared; no unlabeled light or ambient lines remain")
        # new selections, no restart: the next report follows them
        seed2 = dict(seed, camera=False, trays=[], sensors=["temp:soil", "temp:air"])
        tran2 = dict(tran, camera=True, trays=[t1, t2], sensors=["humidity"])
        r2 = c.post("/api/settings", json={"setups": [tran2, seed2]}).get_json()
        b2 = blocks(ai_report.build_context(monitor.gather_report_data()))
        sd2, tr2 = b2.get("Seedlings", ""), b2.get("Transplants", "")
        check(r2["ok"] and "THE PHOTO SHOWS" in (tr2.splitlines() or [""])[0] and "THE PHOTO" not in sd2
              and "air " in sd2 and "air " not in tr2
              and f'Tray "{lab[t1]}"' in tr2 and f'Tray "{lab[t1]}"' not in sd2,
              "changing the setups moves the photo mark, the air sensor and the tray in the next report")
        c.post("/api/settings", json={"setups": []})
        one = ai_report.build_context(monitor.gather_report_data())
        check("split into separate setups" not in one and "PPFD" in one and "air " in one
              and f'Tray "{lab[t1]}"' in one and f'Tray "{lab[t2]}"' in one and "Not assigned" not in one,
              "with one setup everything is reported together, as before")
    finally:
        c.post("/api/settings", json={"setups": saved["setups"] or []})
        with config.settings_lock:
            config.settings["light2_on"] = saved["light2_on"]


def _startup_log_noise():
    """Three things from a real start's journal (25 Sep, 16:09)."""
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
                time.sleep(0.3)           # a slow first read, as on the Pi
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
    check(not any("not found" in m for m in seen) and opened["n"] == 1
          and all(g.get("lux") == 100.0 for g in got) and len(got) == 2,
          "two loops reading the light sensor at start never log it missing while it is being opened")
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
    check("frequency" in args and args["frequency"] is None,
          f"the I2C bus is opened without a frequency the library ignores and warns about ({args})")
    # 3. two ffmpeg runs thumbnailing the same new photo at once
    name = "20260925_160943_m.jpg"
    photo = camera_mod.TIMELAPSE_DIR / name
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    dst = camera_mod.THUMB_DIR / name
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
        th = [threading.Thread(target=camera_mod.make_thumb, args=(photo, cfgt)) for _ in range(2)]
        for t in th:
            t.start()
        for t in th:
            t.join(5)
    finally:
        camera_mod.subprocess.run = real_run
    parts = list(camera_mod.THUMB_DIR.glob("*.part"))
    check(len(runs) == 1 and dst.exists() and not parts and runs[0][-1].endswith(".part"),
          f"a new photo is thumbnailed once, written aside and renamed ({len(runs)} runs, {len(parts)} leftovers)")
    dst.unlink(missing_ok=True)
    camera_mod.subprocess.run = lambda a, **kw: types.SimpleNamespace(returncode=1, stderr=b"bad", stdout=b"")
    try:
        camera_mod.make_thumb(photo, cfgt)
    finally:
        camera_mod.subprocess.run = real_run
    check(not dst.exists() and not list(camera_mod.THUMB_DIR.glob("*.part")),
          "a failed thumbnail leaves nothing behind, so it is tried again")
    loop = re.search(r"def capture_loop\(\):[\s\S]*?make_thumb\(missing\)", app_source())
    check(loop and "None if capturing else" in loop.group(0) and "st_mtime > 10" in loop.group(0),
          "the backfill leaves a photo alone while it is being captured or is under 10 s old")
    photo.unlink(missing_ok=True)


def _unsaved_settings():
    """An edited setting used to revert on the next status (every few seconds,
    from the live readings) as soon as its field lost focus."""
    js = (APP / "static" / "app.js").read_text()
    fh = re.search(r"function formHolds\(key, cfg\)\{[\s\S]*?\n\}", js)
    ff = re.search(r"function fillForm\(cfg\)\{[\s\S]*?(?=\nlet frames=)", js)
    check("f.addEventListener('input',markDirty);f.addEventListener('change',markDirty);" in js
          and fh and "if(formDirty.has(key))return true;" in fh.group(0),
          "a changed setting is marked unsaved and kept over any incoming status")
    check(ff and "document.activeElement" not in ff.group(0) and ff.group(0).count("formHolds(") >= 20,
          "every settings field is repainted only through formHolds, not a focus check")
    sub = re.search(r"getElementById\('cfgform'\)\.addEventListener\('submit'[\s\S]*?\n\}\);", js)
    check(sub and "if(!(j.errors&&k in j.errors))formDirty.delete(k);" in sub.group(0),
          "saving clears the unsaved mark for accepted fields; a rejected field keeps what was typed")
    check("formDirty.add('usb_width');formDirty.add('usb_height');" in js
          and "formDirty.add('kasa_host');" in js,
          "fields filled in by the camera-mode and plug pickers count as unsaved edits too")


def _oom():
    """25 Sep: a 3264x2448 capture asked the camera driver for four 16 MB
    buffers from the ~36 MB the kernel had outside its 256 MB contiguous pool,
    and the out-of-memory killer took the controller three times."""
    of = getattr(camera_mod, "oom_first", lambda c: list(c))
    wrapped = of(["v4l2-ctl", "--all"])
    ok_wrap = wrapped[:2] == ["sh", "-c"] and wrapped[-2:] == ["v4l2-ctl", "--all"]
    if sys.platform.startswith("linux") and Path("/proc/self/oom_score_adj").exists():
        child = subprocess.run(of(["cat", "/proc/self/oom_score_adj"]),
                               capture_output=True).stdout.decode().strip()
        mine = Path("/proc/self/oom_score_adj").read_text().strip()
        check(ok_wrap and child == "1000" and mine != "1000",
              f"a helper runs as the first thing killed on out-of-memory, not the controller "
              f"(helper {child}, controller {mine})")
    else:
        skip("helper OOM score (no /proc here)")
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
    check(cap[:2] == ["sh", "-c"] and "--stream-mmap=2" in cap and "--stream-mmap" not in cap,
          "the capture runs as an OOM-first helper and asks the driver for two buffers, not four")
    check(not ok and err and "out of memory" in err and "3264x2448" in err,
          f"a capture killed for memory says so instead of 'no frames' ({err})")
    cam_src = (APP / "camera.py").read_text()
    runs = re.findall(r"subprocess\.run\((?!\[\"v4l2-ctl\", \"-d\", dev)[^\n]*", cam_src)
    unwrapped = [r for r in runs if "oom_first(" not in r and not r.rstrip().endswith("(")]
    heavy = cam_src.count("subprocess.run(oom_first(") + cam_src.count("oom_first([\"ffmpeg\"") \
        + cam_src.count("oom_first(\n            [\"ffmpeg\"")
    rsrc = (APP / "routes.py").read_text()
    check(not unwrapped and heavy >= 5 and "camera_mod.oom_first([sys.executable, str(helper)" in rsrc
          and "camera_mod.oom_first(cmd)" in rsrc,
          "captures, thumbnails, the render, canopy analysis, previews and corner detection all run OOM-first")
    boot = (APP / "deploy" / "boot-config.txt").read_text()
    setup = (APP / "scripts" / "setup.sh").read_text()
    m = re.search(r"want=\"\$\(awk '\n([\s\S]*?)\n  ' \"\$CONFIG_TXT\"\)\"", setup)
    sample = ("dtparam=audio=on\ndtoverlay=vc4-kms-v3d\nmax_framebuffers=2\n"
              "[all]\ndtparam=i2c_arm=on\n")
    out = ""
    if m and shutil.which("awk"):
        block = "\n".join(["# BEGIN growlight"]
                          + [ln for ln in boot.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
                          + ["# END growlight"])
        sf = WORK / "config.sample"
        sf.write_text(sample)
        out = subprocess.run(["awk", m.group(1), str(sf)], capture_output=True, text=True,
                             env=dict(os.environ, BOOT_BLOCK=block)).stdout
    lines = out.splitlines()
    check("#growlight# dtoverlay=vc4-kms-v3d" in lines and "dtoverlay=vc4-kms-v3d,cma-64" in lines
          and lines.count("dtoverlay=vc4-kms-v3d") == 0 and "CmaTotal" in setup,
          "the boot block sets CMA to 64 MB, comments out the stock KMS line, and setup.sh checks CmaTotal")


def _photo_light():
    """Photos leave the light alone unless "Set the light for each photo" is on
    (Ben did not like the light changing for every photo)."""
    set_to = []
    real_sb, real_usb = light_mod.set_brightness, camera_mod._usb_capture
    light_mod.set_brightness = lambda p, *a, **k: set_to.append(p)
    camera_mod._usb_capture = lambda cfg_, out, w, h, warmup=None: (False, "test")
    with config.settings_lock:
        saved = {k: config.settings.get(k) for k in ("capture_set_light", "capture_brightness",
                                                      "camera_backend", "setups")}
        config.settings.update(camera_backend="usb", capture_brightness=80, setups=[])
        config.settings.pop("capture_set_light", None)       # as on an existing install
    try:
        camera_mod.take_photo(dict(config.DEFAULTS, **config.settings), datetime.now())
        off = list(set_to)
        r = c.post("/api/settings", json={"capture_set_light": True}).get_json()
        camera_mod.take_photo(dict(config.settings), datetime.now())
        on = set_to[len(off):]
    finally:
        light_mod.set_brightness, camera_mod._usb_capture = real_sb, real_usb
        with config.settings_lock:
            config.settings.update(saved)
    check(config.DEFAULTS.get("capture_set_light") is False and off == [],
          "by default a photo leaves the light where it is")
    check(r["ok"] and on == [80], f"with the setting on, a photo sets the main light to the photo brightness ({on})")
    html = (APP / "templates" / "index.html").read_text()
    js = (APP / "static" / "app.js").read_text()
    check('name="capture_set_light"' in html and 'class="capbright"' in html
          and "body.capture_set_light=f.elements['capture_set_light'].checked;" in js
          and "function syncCaptureLight()" in js
          and "formHolds('capture_set_light',cfg)" in js,
          "Settings, Camera has the switch, and Photo brightness shows only when it is on")
    src = (APP / "camera.py").read_text()
    fs = re.search(r"def run_focus_sweep\(\):[\s\S]*?score_at", src)
    check(fs and "if light_for_photo(cfg):" in fs.group(0)
          and "camera_mod.light_for_photo(cfg)" in (APP / "monitor.py").read_text(),
          "the focus sweep and the AI report's photo-light line follow the same switch")


def _canopy_stale():
    """Canopy comes from photos, taken only while the camera's light is on:
    at night its last reading is hours old by design and must not read stale."""
    js = (APP / "static" / "app.js").read_text()
    rs = re.search(r"function readingStale\(key, ts\)\{[\s\S]*?\n\}", js)
    check(rs and "canopyDue!=null && now-Math.max(ts,canopyDue) > 3*capMin*60" in rs.group(0)
          and js.count("readingStale(") >= 3 and "const lim=" not in js
          and "if('canopy_due_since' in j)canopyDue=j.canopy_due_since;" in js,
          "the chips and charts use the same rule, so a night-time canopy chip is not dimmed")

    tz = ZoneInfo(config.settings["timezone"])
    day = datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)
    with config.settings_lock:
        saved = {k: config.settings.get(k) for k in ("camera_enabled", "capture_enabled", "grid",
                                                      "capture_interval_min", "setups")}
        config.settings.update(camera_enabled=True, capture_enabled=True, capture_interval_min=10,
                               setups=[], grid={"corners": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                                                "rows": 4, "cols": 6})
        cfg = dict(config.settings)
    with config.state_lock:
        saved_state = {k: config.state.get(k) for k in ("on", "off")}
        config.state.update(on=day.replace(hour=7), off=day.replace(hour=19))
    try:
        night = camera_mod.canopy_due_since(cfg, day.replace(hour=22))
        noon = camera_mod.canopy_due_since(cfg, day)
        check(night is None and noon == day.replace(hour=7).timestamp(),
              "canopy readings are due only inside the camera's light window")
        cfg_off = dict(cfg, capture_enabled=False)
        check(camera_mod.canopy_due_since(cfg_off, day) is None
              and camera_mod.canopy_due_since(dict(cfg, grid={}), day) is None,
              "and never when timelapse capture is off or the grid has no corners")

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
        morning = health_why(day.replace(hour=7, minute=10),
                             (day - timedelta(days=1)).replace(hour=18, minute=55), "canopy:92")
        midday = health_why(day.replace(hour=12), day.replace(hour=9), "canopy:93")
        check("last reading" not in at_night and "last reading" not in morning and "last reading" in midday,
              "sensor health calls canopy stale only when photos are due and missing, "
              "counting from lights-on in the morning")
        st = c.get("/api/status").get_json()
        check("canopy_due_since" in st, "the status tells the page when canopy readings are due")
    finally:
        with config.settings_lock:
            config.settings.update(saved)
        with config.state_lock:
            config.state.update(saved_state)
        monitor._health_cache["ts"] = 0


def _heat_mat():
    """The smart plug as a heat mat thermostat on the soil temperature."""
    import alerts
    now = time.time()
    base = dict(config.DEFAULTS, heat_mode="auto", heat_target_f=75, heat_max_f=95,
                sample_interval_min=5)
    c75 = (75 - 32) * 5 / 9
    def d(cfg_, t, age, on_now, when, air=18.0, st=None):
        return heat.decide(cfg_, t, age, on_now, when, air, {} if st is None else st)
    rows = [
        (dict(base, heat_mode="off"), 15.0, 60, True, False),       # off is off
        (base, c75 - 3.0, 60, False, True),                           # well below: on
        (base, c75 + 1.5, 60, True, False),                           # well above: off
        (base, 20.0, 3600, True, False),                              # stale reading: off
        (base, None, None, True, False),                              # no reading: off
        (dict(base, heat_mode="on"), 36.0, 60, True, False),          # past the cut-off, even held on
        (dict(base, heat_mode="on"), None, None, False, True),        # held on without a probe
    ]
    bad = [i for i, (cfg_, t, age, on_now, want) in enumerate(rows)
           if d(cfg_, t, age, on_now, now)[0] != want]
    check(not bad, f"the thermostat's decisions: on well below, off well above, off on a stale "
          f"or missing probe and past the cut-off, On and Off obeyed (wrong rows: {bad})")
    check(d(base, 20.0, 3600, True, now)[3] and d(dict(base, heat_mode="on"), 36.0, 60, True, now)[3]
          and d(base, 20.0, 3600, True, now)[2] and not d(base, c75 - 1, 60, False, now)[3]
          and d(dict(base, heat_mode="off"), 20.0, 60, True, now)[3]
          and d(dict(base, heat_mode="on"), 20.0, 60, False, now)[3],
          "safety cut-offs and the grower's On and Off switch at once; Auto keeps to its cycle")
    # Auto: one pulse per window, its length set by the duty
    st = {}
    near = [d(base, c75 - 0.2, 60, None, now + k, c75 - 3.0, st)[0] for k in range(0, heat.WINDOW_S, 30)]
    on_s = 30 * sum(near)
    first_off = near.index(False) if False in near else len(near)
    check(0.3 < st["duty"] < 1.0 and all(near[:first_off]) and not any(near[first_off:])
          and abs(on_s - st["duty"] * heat.WINDOW_S) <= 30,
          f"Auto near the target runs one pulse of part of the {heat.WINDOW_S // 60}-min window, "
          f"then rests (duty {st['duty']:.2f}, on {on_s}s)")
    st = {}
    for k in range(24):                                   # six hours flat out and still short
        d(base, c75 - 3, 60, None, now + k * heat.WINDOW_S, 5.0, st)
    check(st["duty"] == 1.0 and st["integral"] <= heat.KP_PER_C * 3 * heat.WINDOW_S / heat.TI_S + 1e-9,
          f"the integral does not wind up while the mat is flat out (integral {st['integral']:.3f})")
    st = {}
    tiny = [d(base, c75 + 0.35, 60, None, now + k, c75 - 0.2, st)[0] for k in range(0, heat.WINDOW_S, 30)]
    check(not any(tiny) and st["duty"] == 0.0,
          "a pulse shorter than a minute is skipped rather than clicking the relay")

    # A simulation of Ben's rig (two-node mat/soil model fitted to his 25-26 Sep
    # data, 0.19C rms), with a room cooling and warming: Auto must hold the
    # soil much tighter than on/off did, centred on the target.
    def simulate(ctrl, hours=24.0):
        q, k1, k2, k3, k4 = 16.43, 0.67, 1.15, 1.28, 0.754        # per hour
        tgt = f_to_c = (86 - 32) * 5 / 9
        dt, x, m, u = 15.0, tgt - 0.5, tgt + 2.0, False
        meas, seen, hot = x, [], []
        stc = {}
        for i in range(int(hours * 3600 / dt)):
            t = i * dt
            air = 24.8 + 1.2 * math.sin(2 * math.pi * t / 86400.0)   # 23.6-26.0C
            if i % 20 == 0:                                            # a reading every 5 min
                meas = round(x / 0.0625) * 0.0625
            u = ctrl(meas, air, u, t, stc)
            h = dt / 3600
            dm = q * u - k1 * (m - x) - k3 * (m - air)
            dx = k2 * (m - x) - k4 * (x - air)
            m += h * dm
            x += h * dx
            if t > 4 * 3600:
                seen.append(x)
        return min(seen), max(seen), sum(seen) / len(seen)
    cfg86 = dict(base, heat_target_f=86, heat_max_f=95)
    t86 = (86 - 32) * 5 / 9

    def old_onoff(meas, air, u, t, stc):                  # what Auto did before
        if meas < t86 - 1.0 * 5 / 9:
            return True
        return False if meas >= t86 else u

    def auto(meas, air, u, t, stc):
        return heat.decide(cfg86, meas, 60, u, t, air, stc)[0]
    lo0, hi0, mean0 = simulate(old_onoff)
    lo1, hi1, mean1 = simulate(auto)
    check((hi1 - lo1) * 1.8 < 1.3 and abs(mean1 - t86) < 0.15 and (hi1 - lo1) < (hi0 - lo0) / 1.8,
          f"on the fitted rig model Auto holds {(hi1 - lo1) * 1.8:.1f}F peak to peak around the "
          f"target (mean {(mean1 - t86) * 1.8:+.2f}F); on/off swung {(hi0 - lo0) * 1.8:.1f}F "
          f"(mean {(mean0 - t86) * 1.8:+.2f}F)")

    sent = []
    reading = {"v": (c75 - 2, now)}
    real_apply, real_rf = light_mod.kasa_apply, monitor.reading_filtered

    def fake_apply(on, retries=1):
        sent.append(bool(on))
        light_mod.kasa_state.update(on=bool(on), ok=True, error="", fails=0)
        return True
    light_mod.kasa_apply = fake_apply
    monitor.reading_filtered = (lambda key, snap=None:
                                (18.0, reading["v"][1]) if key == "temp:air" else reading["v"])
    with config.settings_lock:
        saved = {k: config.settings.get(k) for k in ("plug_use", "heat_mode", "heat_target_f",
                                                      "heat_max_f", "kasa_host", "light_backend")}
        config.settings.update(plug_use="light", heat_mode="auto", heat_target_f=75, heat_max_f=95,
                               kasa_host="10.0.3.177", light_backend="dim")
    heat.heat_state.update(on=None, since=0.0, sent=0.0, fault="", reason="not in use",
                           duty=None, integral=0.0, window=None, on_until=0.0)
    heat._prev_use = None
    try:
        heat.heat_pass(now)
        check(sent == [], "with the plug given to the light, the thermostat never touches it")
        with config.settings_lock:
            config.settings["plug_use"] = "heat"
        heat.heat_pass(now)                                   # cold: a full-power window
        reading["v"] = (c75 + 1.5, now + 30)
        heat.heat_pass(now + 30)                              # warm now, but the window stands
        held = heat.heat_state["on"]
        reading["v"] = (c75 + 1.5, now + heat.WINDOW_S)
        heat.heat_pass(now + heat.WINDOW_S + 1)               # next window: no power
        check(sent[:1] == [True] and held is True and sent[-1] is False,
              f"Auto sets the power once per {heat.WINDOW_S // 60}-min window: on while cold, "
              f"off from the next window once warm ({sent})")
        ev_before = len(db.recent_events(500))
        reading["v"] = (c75 - 2, now + 2 * heat.WINDOW_S)
        heat.heat_pass(now + 2 * heat.WINDOW_S + 1)           # on again
        check(len(db.recent_events(500)) == ev_before,
              "Auto's pulses are not logged as events (dozens a day); safety and mode changes are")
        reading["v"] = (36.0, now + 2 * heat.WINDOW_S + 10)
        n = len(sent)
        heat.heat_pass(now + 2 * heat.WINDOW_S + 15)          # past the cut-off...
        check(sent[n:] == [False] and heat.heat_state["fault"],
              "past the cut-off the mat goes off at once and the fault is raised")
        failing = {"n": 0}

        def failing_apply(on, retries=1):
            failing["n"] += 1
            light_mod.kasa_state.update(ok=False, error="unreachable")
            return False
        light_mod.kasa_apply = failing_apply
        was_on = heat.heat_state["on"]
        reading["v"] = (c75 - 3, now + 3000)
        heat.heat_pass(now + 3000)
        check(failing["n"] == 1 and heat.heat_state["on"] == was_on
              and "plug not responding" in heat.heat_state["reason"],
              "when the plug does not answer, the mat is reported as it last was, not as asked")
        light_mod.kasa_apply = fake_apply
        sent.clear()
        light_mod.set_brightness_raw(40)
        check(sent == [], "with the plug given to the heat mat, the light's writes never switch it")
        with config.settings_lock:
            config.settings["plug_use"] = "light"
        heat.heat_pass(now + 2000)
        check(sent[:1] == [False] and heat.heat_state["on"] is None,
              "handing the plug back to the light turns the mat off and leaves it to the light")
        with config.settings_lock:
            config.settings["plug_use"] = "heat"
        sent.clear()
        heat.off_now()
        src = (APP / "hardware.py").read_text()
        check(sent == [False] and "heat_mod.off_now()" in src,
              "shutting down turns the heat mat off")
        r1 = c.post("/api/settings", json={"light_backend": "kasa"}).get_json()
        r2 = c.post("/api/settings", json={"heat_target_f": 80, "heat_max_f": 81}).get_json()
        r3 = c.post("/api/settings", json={"heat_sensor": "temp:air"}).get_json()
        check("light_backend" in r1.get("errors", {}) and "heat_max_f" in r2.get("errors", {})
              and "heat_sensor" in r3.get("errors", {}),
              "the plug cannot be the light and the heat mat at once; the cut-off must clear "
              "the target by 3F; the probe must be a soil probe")
        r4 = c.post("/api/settings", json={"heat_target_f": 85, "heat_max_f": 87}).get_json()
        e4 = r4.get("errors", {})
        js_ = (APP / "static" / "app.js").read_text()
        check("88F" in e4.get("heat_max_f", "") and "85F" in e4.get("heat_max_f", "")
              and "not saved until" in e4.get("heat_target_f", "")
              and "fieldLabel(f,k)+' '+j.errors[k]" in js_ and "function fieldLabel(f, k)" in js_,
              "a cut-off too close to the target says what it must be, on the page by the field's "
              "label rather than its internal name")
        h1 = c.post("/api/heat", json={"mode": "on"}).get_json()
        with config.settings_lock:
            config.settings["plug_use"] = "light"
        h2 = c.post("/api/heat", json={"mode": "auto"}).get_json()
        st = c.get("/api/status").get_json().get("heat") or {}
        check(h1["ok"] and not h2["ok"] and "use" in st and "mode" in st and "on" in st,
              "the Heat mat buttons work only when the plug is the heat mat's; the status reports it")
        acfg = dict(alerts.DEFAULTS, sustain_seconds=0)
        alerts.reset()
        fired = [a for a in alerts.check_all({"_heat_fault": "no recent soil temperature"}, acfg)
                 if a[1] == "heat_fault"]
        check(fired and fired[0][0] == "fire", "a held-off heat mat sends an alert")
        alerts.reset()
    finally:
        light_mod.kasa_apply, monitor.reading_filtered = real_apply, real_rf
        with config.settings_lock:
            config.settings.update(saved)
        heat.heat_state.update(on=None, since=0.0, sent=0.0, fault="", reason="not in use",
                               duty=None, integral=0.0, window=None, on_until=0.0)
        heat._prev_use = None
    js = (APP / "static" / "app.js").read_text()
    html = (APP / "templates" / "index.html").read_text()
    check('id="heatrow"' in html and 'name="plug_use"' in html and 'name="heat_target_f"' in html
          and "fetch('/api/heat'" in js and "renderHeat(j);" in js
          and ".lcbtn:not(.fanbtn):not(.heatbtn)" in js and ".lcbtn:not(.fanbtn)')" not in js
          and "body[k]=Math.round(tToF(parseFloat(f.elements[k].value))*10)/10;" in js,
          "the Light card has Heat mat buttons, Settings has the plug use and thermostat, "
          "temperatures are saved in F, and the light's buttons ignore the heat buttons")
    css = (APP / "static" / "style.css").read_text()
    check(html.count('class="devrow ') == 2 and 'id="heatdot"' in html and 'id="fandot"' in html
          and ".lightctl .lcrow{flex-wrap:nowrap}" in css and "#lightinfo:empty{display:none}" in css
          and "info.title=plugBad&&h.plug_error?h.plug_error:'';" in js,
          "Fan and Heat mat rows: buttons on one line, a status line with a dot under them, "
          "the plug's raw error only in a tooltip")
    ctx = ai_report.build_context({"by_setup": {"setups": []}, "units": {"temp": "F"},
                                   "heat": {"use": True, "mode": "auto", "on": True,
                                            "target_f": 75, "reason": "below target"}})
    check("Heat mat under the trays: on now, thermostat holding the soil at 75F" in ctx,
          "the AI report knows the heat mat's state")


def _shared_sensors():
    """One air sensor between two close areas counts for both setups; the heat
    mat belongs to one setup like the fan."""
    with config.settings_lock:
        trays = sorted(config.settings.get("trays") or {})
        saved = {k: config.settings.get(k) for k in ("setups", "plug_use", "heat_mode")}
    t1, t2 = trays[0], trays[1]
    tran = {"name": "Transplants", "light": "main", "lux": "lux", "trays": [t2], "fan": True,
            "sensors": ["humidity", "temp:air", "pressure", "lux", f"float:{t1}", "reservoir:low"],
            "dli_low": 12, "dli_high": 15}
    seed = {"name": "Seedlings", "light": "second", "lux": "", "trays": [t1], "heat": True,
            "sensors": ["humidity", "temp:air", "pressure", "temp:soil"], "dli_low": 8, "dli_high": 10}
    try:
        r = c.post("/api/settings", json={"setups": [tran, seed]}).get_json()
        with config.settings_lock:
            sts = {x["name"]: x for x in config.settings["setups"]}
        check(r["ok"] and sts["Transplants"]["sensors"] == ["humidity", "pressure", "temp:air"]
              and "humidity" in sts["Seedlings"]["sensors"],
              "a sensor may be ticked in two setups; a tray's, the light sensor's and the "
              "reservoir's keys are dropped from sensor lists (they are placed elsewhere)")
        with config.settings_lock:
            cfg = dict(config.settings)
        both = [x["name"] for x in setups_mod.sensor_setups(cfg, "humidity")]
        check(both == ["Transplants", "Seedlings"]
              and [x["name"] for x in setups_mod.sensor_setups(cfg, "temp:soil")] == ["Seedlings"]
              and [x["name"] for x in setups_mod.sensor_setups(cfg, f"probe:{t1}")] == ["Seedlings"],
              "a shared sensor counts for every setup that ticks it; tray readings follow the tray")
        r2 = c.post("/api/settings", json={"setups": [dict(tran, heat=True), seed]}).get_json()
        st = {x["name"]: x for x in c.get("/api/status").get_json()["setups"]}
        check(not r2["ok"] and st["Seedlings"]["heat"] and not st["Transplants"]["heat"],
              "the heat mat belongs to one setup, like the fan and camera")
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
        check("RH 55% (same sensor as Seedlings)" in blk.get("Transplants", "")
              and "RH 55% (same sensor as Transplants)" in blk.get("Seedlings", "")
              and "Heat mat" in blk.get("Seedlings", "") and "Heat mat" not in blk.get("Transplants", "")
              and not any(l.startswith("Heat mat") for l in ctx.splitlines()),
              "the AI report lists a shared sensor under both setups, says it is one sensor, "
              "and puts the heat mat under its own setup")
    finally:
        c.post("/api/settings", json={"setups": saved["setups"] or []})
        with config.settings_lock:
            config.settings.update(plug_use=saved["plug_use"], heat_mode=saved["heat_mode"])
    js = (APP / "static" / "app.js").read_text()
    css = (APP / "static" / "style.css").read_text()
    check("const keys=all.filter(k=>!/^(probe|canopy|float|reservoir):|^lux(:|$)/.test(k));" in js
          and 'data-flag="heat"' in js and "heatelsewhere" in js
          and "body.heatelsewhere #heatrow{display:none !important}" in css,
          "the Setups editor offers only sensors that need placing, has a Heat mat box, and the "
          "heat mat controls show on their setup's tab")

def run(name, fn):
    """A section that crashes counts as one failure; the rest still run."""
    section(name)
    try:
        fn()
    except Exception as e:
        check(False, f"{name}: crashed with {type(e).__name__}: {e}")


run('Routes', _sec0)
run('Sign-in and privacy', _sec1)
run('Live stream', _sec2)
run('Watering', _sec3)
run('Data', _sec4)
run('Light schedule', _sec5)
run('DLI target', _dli_band)
run('Camera flattening', _camera_flatten)
run('Camera preview', _camera_preview)
run('Camera modes and crop reset', _camera_modes_and_reset)
run('Camera crop', _camera_crop)
run('Timelapse sharpness and capture status', _timelapse_sharp)
run('Enlarged view', _lightbox)
run('AI report reply', _ai_reply)
run('Grow setups', _setups)
run('Fan, camera and verdict timing', _fan_camera_timing)
run('Probe names', _probe_names)
run('Per-light calibration and per-tray arming', _per_sensor_controls)
run('Camera canopy trays', _camera_canopy)
run('AI report by setup', _report_by_setup)
run('Startup log noise and thumbnail race', _startup_log_noise)
run('Unsaved settings', _unsaved_settings)
run('Out of memory', _oom)
run('Photo light', _photo_light)
run('Canopy staleness', _canopy_stale)
run('Heat mat', _heat_mat)
run('Shared sensors and the heat mat setup', _shared_sensors)
run('Shutdown', _shutdown)

# --------------------------------------------------------------------------
print(f"\n{'All checks passed.' if not FAILS else f'{len(FAILS)} FAILED:'}")
for f in FAILS:
    print(f"  - {f}")
shutil.rmtree(WORK, ignore_errors=True)
os._exit(len(FAILS))          # daemon threads (control loop) end with the process
