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
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
from datetime import date, datetime
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
               "water.py", "monitor.py", "camera.py", "status.py", "routes.py")


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
         "camera_mod": camera_mod, "status_mod": status_mod, "routes": routes}
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
        camera_mod.take_photo(dict(config.settings), now)
        n_second = calls["n"]
        c.post("/api/settings", json={"setups": [dict(base[0], fan=True), dict(base[1], camera=True)]})
        camera_mod.take_photo(dict(config.settings), now)
        n_main = calls["n"] - n_second
    finally:
        light_mod.set_brightness, camera_mod._usb_capture = real_sb, real_usb
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
run('AI report reply', _ai_reply)
run('Grow setups', _setups)
run('Fan, camera and verdict timing', _fan_camera_timing)
run('Probe names', _probe_names)
run('Per-light calibration and per-tray arming', _per_sensor_controls)
run('Camera canopy trays', _camera_canopy)
run('Shutdown', _shutdown)

# --------------------------------------------------------------------------
print(f"\n{'All checks passed.' if not FAILS else f'{len(FAILS)} FAILED:'}")
for f in FAILS:
    print(f"  - {f}")
shutil.rmtree(WORK, ignore_errors=True)
os._exit(len(FAILS))          # daemon threads (control loop) end with the process
