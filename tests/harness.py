"""Shared setup for the server tests: the app, imported safely.

Importing this module copies the app to a temp directory (your config.json,
.env, .secret, database and photos are never read or written), replaces GPIO
and PWM with fakes, blocks the I2C sensor libraries so nothing touches the
bus, and imports the app from the copy. No port is opened; requests go
through Flask's test client, `c`. Safe on the Pi next to the running service.

Test modules import what they need from here: the app's modules, the client,
and a few helpers for looking at the page's source.
"""

import atexit
import os
import re
import shutil
import sys
import tempfile
import types
import warnings
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# a disposable copy of the app


REPO = Path(__file__).resolve().parent.parent
WORK = Path(tempfile.mkdtemp(prefix="openseedling-test-"))
APP = WORK / "app"
# growlight.py is the entry point; the app's code lives in these modules
APP_MODULES = (
    "growlight.py",
    "config.py",
    "hardware.py",
    "light.py",
    "control.py",
    "setups.py",
    "water.py",
    "monitor.py",
    "heat.py",
    "camera.py",
    "status.py",
    "routes.py",
    "routes_camera.py",
    "routes_garden.py",
    "routes_climate.py",
    "routes_data.py",
)


def squash(text):
    """Code with all whitespace removed, so a check on the code's text does
    not depend on how a formatter wrapped or indented it."""
    # semicolons too: Prettier adds or drops them where they are optional
    return re.sub(r"\s+", "", text).replace(";", "")


def code_has(src, *needles):
    """Every needle appears in src, ignoring whitespace (see squash)."""
    sq = squash(src)
    return all(squash(n) in sq for n in needles)


class Code(str):
    """Source text whose `in` ignores whitespace, for checks on the code
    itself: Prettier and ruff format rewrap code without changing it."""

    def group(self, _=0):
        return self  # stands in for a regex match in older checks

    def __contains__(self, needle):
        sq = self.__dict__.get("_sq")
        if sq is None:
            sq = self.__dict__["_sq"] = squash(str(self))
        return squash(needle) in sq


def _match_brace(src, i):
    """Index just past the brace block opening at src[i] == "{", skipping
    strings, template literals and comments."""
    depth, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch in "'\"":
            j = i + 1
            while j < n and src[j] != ch:
                j += 2 if src[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "`":
            j = i + 1
            while j < n and src[j] != "`":
                if src[j] == "\\":
                    j += 2
                    continue
                if src.startswith("${", j):
                    j = _match_brace(src, j + 1)
                    continue
                j += 1
            i = j + 1
            continue
        if src.startswith("//", i):
            i = src.find("\n", i)
            i = n if i < 0 else i
            continue
        if src.startswith("/*", i):
            i = src.find("*/", i) + 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def js_fn(js, name):
    """The source of the JavaScript function `name` (declaration through its
    closing brace), as Code; None if it is not there."""
    m = re.search(r"(?:async\s+)?function\s+%s\s*\(" % re.escape(name), js)
    if not m:
        return None
    start = js.index("{", js.index(")", m.end()))
    return Code(js[m.start() : _match_brace(js, start)])


def js_after(js, needle):
    """The first brace block after `needle` (e.g. a handler's body), as Code."""
    i = squash_index(js, needle)
    if i < 0:
        return None
    start = js.index("{", i)
    return Code(js[i : _match_brace(js, start)])


def squash_index(src, needle):
    """Where needle starts in src, ignoring whitespace in both; -1 if absent."""
    want = squash(needle)
    pos = [k for k, ch in enumerate(src) if not ch.isspace()]
    sq = "".join(src[k] for k in pos)
    j = sq.find(want)
    return pos[j] if j >= 0 else -1


def page_js():
    """The dashboard's scripts (static/js/*.js, loaded in this order by the
    page), joined: what static/app.js was before the 4 Oct split."""
    order = re.findall(r"from '\./(\w+\.js)'", (APP / "static" / "js" / "main.js").read_text())
    return Code("\n".join((APP / "static" / "js" / f).read_text() for f in order))


def app_source():
    """Every app module's source, for checks that look at the code itself."""
    return "\n".join((APP / f).read_text() for f in APP_MODULES)


_skip = shutil.ignore_patterns(
    ".git",
    "venv",
    "__pycache__",
    "tests",
    "timelapse",
    "timelapse_archive",
    "growlight.db*",
    "config.json",
    ".env",
    ".secret",
    "ai_report.json",
    "*.jpg",
    "*.mp4",
    "*.log",
    ".lgd-*",
)


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

PWM_WRITES = []  # (channel, duty) for every hardware PWM write


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
    OutputDevice=FakeOutput, PWMOutputDevice=FakePWMOutput, Button=FakeButton
)
# None in sys.modules makes the import raise ImportError: sensors.py then
# treats every I2C device as absent, so the real bus is never opened.
for mod in (
    "board",
    "busio",
    "adafruit_extended_bus",
    "adafruit_ads1x15",
    "adafruit_ads1x15.ads1115",
    "adafruit_ads1x15.analog_in",
    "adafruit_bus_device",
    "adafruit_bus_device.i2c_device",
    "adafruit_bme280",
    "adafruit_bmp280",
    "adafruit_bh1750",
):
    sys.modules[mod] = None

sys.path.insert(0, str(APP))
# a config written by an older version, to check the model migration
(APP / "config.json").write_text(
    '{"ai_model": "claude-opus-4-8", '
    '"probe_names": {"1": "Tray 1", "2": "Left bench"}, "auto_water": true}'
)

from werkzeug.security import generate_password_hash
from zoneinfo import ZoneInfo

import growlight  # imports the whole app and registers every route
import config
import hardware
import light as light_mod
import control
import setups as setups_mod
import water
import monitor
import heat
import camera as camera_mod
import status as status_mod
import events
import readings as readings_mod

# Tests change settings directly and read the status straight back; the shared
# poll build (reused up to 5 s between changes) is checked on its own.
status_mod.POLL_REUSE_S = 0.0
import routes
import db
import sensors

db.init()
tz = ZoneInfo(config.settings["timezone"])
now = datetime.now(tz)
config.state.update(
    on=now.replace(hour=7),
    off=now.replace(hour=19),
    sunrise=now.replace(hour=6),
    sunset=now.replace(hour=19),
    brightness=0,
    override="auto",
)
c = routes.app.test_client()
# a request as the Pi's own touchscreen makes it: to the screen's listener
ON_SCREEN = {"base_url": f"http://127.0.0.1:{config.SCREEN_PORT}"}

import ai_report
import alerts
import auth

# everything a test module may import from here
__all__ = [
    "ai_report",
    "alerts",
    "APP",
    "APP_MODULES",
    "app_source",
    "AT_IMPORT",
    "auth",
    "c",
    "camera_mod",
    "Code",
    "code_has",
    "config",
    "control",
    "db",
    "growlight",
    "events",
    "FakeButton",
    "FakeDevice",
    "FakeHardwarePWM",
    "FakeOutput",
    "FakePWMOutput",
    "FAST_HASH",
    "hardware",
    "heat",
    "js_after",
    "js_fn",
    "light_mod",
    "monitor",
    "note",
    "now",
    "ON_SCREEN",
    "page_js",
    "PWM_WRITES",
    "readings_mod",
    "REPO",
    "routes",
    "sensors",
    "set_password",
    "setups_mod",
    "squash",
    "squash_index",
    "status_mod",
    "tz",
    "water",
    "WORK",
]

# what the app made of the old-format config.json written above, before any
# test touches the settings (tests/test_startup.py checks it)
AT_IMPORT = {
    "auto_water_trays": list(config.settings.get("auto_water_trays") or []),
    "ai_model": config.settings.get("ai_model"),
}
with config.settings_lock:
    config.settings.update(auto_water=False, auto_water_trays=[])

# a password check fast enough for tests (the real one is scrypt)
FAST_HASH = generate_password_hash("pw", method="pbkdf2:sha256:1000")


def set_password(on):
    with config.settings_lock:
        config.settings["password_hash"] = FAST_HASH if on else ""


def note(msg):
    """Part of a test could not run here (a tool is missing); say so in the
    warnings summary and carry on with the rest of the test."""
    warnings.warn(msg, stacklevel=2)


atexit.register(shutil.rmtree, WORK, ignore_errors=True)
