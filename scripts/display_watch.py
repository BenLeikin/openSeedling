#!/usr/bin/env python3
"""Keeps the touchscreen's rotation and display mode in step with Settings.

Started by kiosk-session.sh inside the kiosk session (it needs the session's
Wayland display to talk to the compositor), so a change made in Settings,
External display, applies within a few seconds without restarting anything.
It also writes what the display reports (its modes, the one in use, the
rotation) to display_state.json beside config.json, which Settings shows.

With "When the lights are off" set to turn the screen off, it switches the
HDMI output off while every light is off (the panel's backlight goes dark
with it) and on again when a light comes on. A touch while it is off wakes it
for WAKE_S: the touch panel is read directly here, since with the output off
the compositor has nowhere to send the tap.

Exits when the browser it was started beside exits, so a restarted kiosk
never leaves an old copy running.
"""

import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
CONFIG = APP / "config.json"
STATE = APP / "display_state.json"
POLL_S = 5
WAKE_S = 60  # a touch while the screen is off shows it this long
TRANSFORMS = {"normal": 0, "90": 90, "180": 180, "270": 270}


def parse(text):
    """The first output in `wlr-randr` text: name, modes, current, preferred,
    rotation. Returns None when there is none."""
    out = None
    in_modes = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            if out:
                break  # only the first output: the kiosk drives one screen
            out = {"output": line.split()[0], "modes": [], "current": None, "preferred": None}
            out["transform"] = 0
            out["enabled"] = True
            in_modes = False
            continue
        if out is None:
            continue
        s = line.strip()
        if s.startswith("Modes:"):
            in_modes = True
            continue
        m = re.match(r"(\d+)x(\d+) px, ([\d.]+) Hz(.*)", s)
        if in_modes and m:
            hz = f"{float(m.group(3)):.3f}".rstrip("0").rstrip(".")
            mode = f"{m.group(1)}x{m.group(2)}@{hz}"
            if mode not in out["modes"]:
                out["modes"].append(mode)
            if "current" in m.group(4):
                out["current"] = mode
            if "preferred" in m.group(4):
                out["preferred"] = mode
            continue
        in_modes = False
        if s.startswith("Transform:"):
            out["transform"] = TRANSFORMS.get(s.split(":", 1)[1].strip(), 0)
        elif s.startswith("Enabled:"):
            out["enabled"] = s.split(":", 1)[1].strip() == "yes"
    return out


def wanted(cfg, default_rotate):
    """(rotation, mode) asked for in Settings; None where it is left as is."""
    rot = str(cfg.get("screen_rotate") or "")
    rot = int(rot) if rot in ("0", "90", "180", "270") else default_rotate
    mode = str(cfg.get("screen_mode") or "").strip() or None
    return rot, mode


def pick_mode(want, modes):
    """The listed mode that a setting like 1024x600 or 1024x600@60 means."""
    if not want:
        return None
    if want in modes:
        return want
    size = want.split("@")[0]
    for m in modes:
        if m.split("@")[0] == size:
            return m
    return None


def lights_off(status):
    """Whether every grow light is off, from the controller's lite status."""
    if not status or status.get("error"):
        return False  # no answer: keep the screen on rather than guess dark
    sets = status.get("setups") or []
    if not sets:
        return not (status.get("brightness") or 0) > 0
    for su in sets:
        level = (status.get("light2") or {}).get("level") if su.get("light") == "second" else None
        if su.get("light") != "second":
            level = status.get("brightness")
        if (level or 0) > 0:
            return False
    return True


def want_on(cfg, status, now, woke_until):
    """Whether the output should be on: always, unless Settings says to turn
    the screen off while the lights are off and they are (and nobody tapped)."""
    if cfg.get("screen_night") != "sleep":
        return True
    return now < woke_until or not lights_off(status)


def fetch_status():
    """The controller's lite status, as the summary page gets it."""
    import urllib.parse
    import urllib.request

    base = os.environ.get("KIOSK_URL") or "http://127.0.0.1:5001/screen"
    url = urllib.parse.urljoin(base, "/api/status?lite=1")
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def touch_device(text=None):
    """The touch panel's /dev/input/eventN: the input device that reports
    multitouch positions (ABS_MT_POSITION_X, bit 53), as usbcheck.sh finds it."""
    try:
        text = Path("/proc/bus/input/devices").read_text() if text is None else text
    except Exception:
        return None
    blocks = text.strip().split("\n\n")
    for b in blocks:
        absm = re.search(r"B: ABS=([0-9a-f ]+)", b)
        ev = re.search(r"H: Handlers=.*?\b(event\d+)", b)
        if not absm or not ev:
            continue
        words = absm.group(1).split()
        # the kernel prints the bitmap in words of its own size, 64 or 32 bits
        wide = int("".join(w.zfill(16) for w in words), 16)
        narrow = int("".join(w.zfill(8) for w in words), 16)
        if (wide >> 53 & 1) or (len(words) > 1 and narrow >> 53 & 1):
            return "/dev/input/" + ev.group(1)
    return None


def randr(*args):
    return subprocess.run(["wlr-randr", *args], capture_output=True, text=True, timeout=10)


def step(default_rotate, log=print, status=None, woke_until=0.0, now=None):
    """Read Settings and the display once; change what differs. Returns the state."""
    try:
        cfg = json.loads(CONFIG.read_text())
    except Exception:
        cfg = {}
    r = randr()
    st = parse(r.stdout) if r.returncode == 0 else None
    if not st:
        return None
    on = want_on(cfg, status, time.time() if now is None else now, woke_until)
    if on != st["enabled"]:
        res = randr("--output", st["output"], "--on" if on else "--off")
        log(
            f"display: {'on' if on else 'off'} -> {'ok' if res.returncode == 0 else res.stderr.strip()}"
        )
        r = randr()
        st = (parse(r.stdout) if r.returncode == 0 else None) or st
    if not st["enabled"]:
        st["at"] = int(time.time())
        _save(st)
        return st  # nothing else to set on a dark output
    rot, want_mode = wanted(cfg, default_rotate)
    mode = pick_mode(want_mode, st["modes"])
    if want_mode and not mode:
        st["error"] = f"{want_mode} is not one of this display's modes"
    args = []
    if mode and mode != st["current"]:
        w, rest = mode.split("@")
        args += ["--mode", f"{w}@{rest}Hz"]
    if rot is not None and rot != st["transform"]:
        args += ["--transform", "normal" if rot == 0 else str(rot)]
    if args:
        res = randr("--output", st["output"], *args)
        log(f"display: {' '.join(args)} -> {'ok' if res.returncode == 0 else res.stderr.strip()}")
        r = randr()
        st = (parse(r.stdout) if r.returncode == 0 else None) or st
    st["at"] = int(time.time())
    _save(st)
    return st


def _save(st):
    tmp = STATE.with_suffix(".part")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, STATE)


def main():
    try:
        default_rotate = int(os.environ.get("KIOSK_ROTATE") or 0)
    except ValueError:
        default_rotate = 0
    parent = os.getppid()
    woke_until = 0.0
    touch = None
    dev = touch_device()
    try:
        touch = os.open(dev, os.O_RDONLY | os.O_NONBLOCK) if dev else None
    except OSError as e:
        print(f"display: cannot read {dev} for tap-to-wake ({e})", file=sys.stderr, flush=True)
    st, last = None, 0.0
    while os.getppid() == parent:
        if time.time() - last >= POLL_S:
            last = time.time()
            try:
                st = step(
                    default_rotate,
                    log=lambda m: print(m, file=sys.stderr, flush=True),
                    status=fetch_status(),
                    woke_until=woke_until,
                )
            except Exception as e:
                print(f"display: {e}", file=sys.stderr, flush=True)
        wait = max(0.1, POLL_S - (time.time() - last))
        if touch is None:
            time.sleep(wait)
            continue
        # wait out the poll, but wake at once for a touch on a dark screen
        ready, _, _ = select.select([touch], [], [], wait)
        if not ready:
            continue
        try:
            while os.read(touch, 4096):
                pass
        except OSError:
            pass  # drained (EAGAIN), or the panel went away
        if st is not None and not st.get("enabled", True):
            woke_until = time.time() + WAKE_S
            last = 0.0  # turn it on now, not at the next poll
        elif woke_until > time.time():
            woke_until = time.time() + WAKE_S  # still in use: keep it up


if __name__ == "__main__":
    main()
