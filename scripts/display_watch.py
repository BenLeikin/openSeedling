#!/usr/bin/env python3
"""Keeps the touchscreen's rotation and display mode in step with Settings.

Started by kiosk-session.sh inside the kiosk session (it needs the session's
Wayland display to talk to the compositor), so a change made in Settings,
External display, applies within a few seconds without restarting anything.
It also writes what the display reports (its modes, the one in use, the
rotation) to display_state.json beside config.json, which Settings shows.

Exits when the browser it was started beside exits, so a restarted kiosk
never leaves an old copy running.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
CONFIG = APP / "config.json"
STATE = APP / "display_state.json"
POLL_S = 5
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


def randr(*args):
    return subprocess.run(["wlr-randr", *args], capture_output=True, text=True, timeout=10)


def step(default_rotate, log=print):
    """Read Settings and the display once; change what differs. Returns the state."""
    try:
        cfg = json.loads(CONFIG.read_text())
    except Exception:
        cfg = {}
    r = randr()
    st = parse(r.stdout) if r.returncode == 0 else None
    if not st:
        return None
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
    tmp = STATE.with_suffix(".part")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, STATE)
    return st


def main():
    try:
        default_rotate = int(os.environ.get("KIOSK_ROTATE") or 0)
    except ValueError:
        default_rotate = 0
    parent = os.getppid()
    while os.getppid() == parent:
        try:
            step(default_rotate, log=lambda m: print(m, file=sys.stderr, flush=True))
        except Exception as e:
            print(f"display: {e}", file=sys.stderr, flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
