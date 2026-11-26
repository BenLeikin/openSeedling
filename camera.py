"""The cameras: taking photos (the Pi camera, or one or two USB cameras
stitched into one frame), the focus sweep, and canopy tracking."""

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from applog import log
import db
import readings as readings_mod

import config
import light as light_mod
import setups as setups_mod

import photos as photos_mod
import tools

# alignment viewfinder; not a timelapse frame
PREVIEW_PATH = Path(__file__).with_name("preview.jpg")

# Camera health: every capture and preview outcome lands here, so the dashboard
# can tell "old photo because night" from "old photo because the camera died".
camera = {"last_ok": None, "fails": 0, "last_err": "", "last_err_ts": None}

# Focus sweep: walk focus_absolute, score each frame's sharpness, pin the best.
# step counts up to total through the coarse pass, then the fine one.
focus_state = {"running": False, "step": 0, "total": 0, "best": None, "error": "", "cancel": False}

focus_lock = threading.Lock()


def _camera_ok():
    with config.state_lock:
        camera.update(last_ok=time.time(), fails=0, last_err="", last_err_ts=None)


def _camera_fail(err):
    with config.state_lock:
        camera["fails"] += 1
        camera["last_err"] = str(err)[:200]
        camera["last_err_ts"] = time.time()


capturing = False  # a capture, focus sweep or alignment is using the camera

HOLD_PHOTO, HOLD_FOCUS, HOLD_ALIGN = "photo", "focus sweep", "camera alignment"

PHOTO_HOLD_S = 300  # two USB cameras at their timeouts, with room to spare

capture_lock = threading.Lock()  # serialize camera access (manual vs scheduled)

GROWTH_SCRIPT = Path(__file__).with_name("growth.py")

# ---- capture ----------------------------------------------------------------

# Each entry: config flag -> (v4l2 auto control, value for auto, value for
# manual, the manual controls it unlocks). With auto on, the camera re-decides
# exposure and white balance every frame, so brightness and colour shift
# between shots (flicker in the timelapse) and the canopy analysis moves with
# them.
USB_AUTO_GROUPS = {
    "usb_auto_focus": ("focus_automatic_continuous", 1, 0, ("focus_absolute",)),
    "usb_auto_exposure_on": ("auto_exposure", 3, 1, ("exposure_time_absolute", "gain")),
    "usb_auto_white_balance": ("white_balance_automatic", 1, 0, ("white_balance_temperature",)),
}


def _usb_apply_controls(cfg, dev):
    """Push camera settings before a capture.

    Applied in two passes: a manual control stays flagged `inactive` and
    rejects writes until its automatic counterpart has been switched off, so
    every auto flag must land before any manual value.
    """
    autos, manuals = [], []
    for flag, (ctrl, on_val, off_val, dependents) in USB_AUTO_GROUPS.items():
        auto = bool(cfg.get(flag, False))
        autos.append(f"{ctrl}={on_val if auto else off_val}")
        if auto:
            continue  # let the camera decide these
        for dep in dependents:
            val = cfg.get("usb_" + dep)
            if val not in (None, ""):
                manuals.append(f"{dep}={int(val)}")
    for extra in ("brightness", "contrast", "saturation"):
        val = cfg.get("usb_" + extra)
        if val not in (None, ""):
            manuals.append(f"{extra}={int(val)}")
    for group in (autos, manuals):
        if not group:
            continue
        args = []
        for c in group:
            args += ["-c", c]
        subprocess.run(["v4l2-ctl", "-d", dev] + args, capture_output=True, timeout=10)


def _usb_capture(cfg, out_path, width, height, warmup=None, dev=None):
    """Grab one MJPEG frame from a UVC camera via v4l2-ctl. Returns (ok, error).

    Captures a short burst (usb_warmup_frames, or `warmup`) and keeps the last
    whole frame: the first frame after opening the device is often dark or
    torn, and auto-gain needs a few frames to settle.
    """
    dev = dev or cfg.get("usb_device", "/dev/video0")
    if not Path(dev).exists():
        return False, f"{dev} not present"
    _usb_apply_controls(cfg, dev)
    n = int(cfg.get("usb_warmup_frames", 4) if warmup is None else warmup)
    n = max(1, min(20, n))
    tmp = Path(str(out_path) + ".raw")
    # Two buffers, not v4l2-ctl's default four: the driver allocates each at
    # the camera's worst-case frame size (16 MB at 3264x2448) from memory the
    # kernel cannot reclaim. Frames are kept one at a time anyway.
    cmd = [
        "v4l2-ctl",
        "-d",
        dev,
        "--set-fmt-video=width=%d,height=%d,pixelformat=MJPG" % (width, height),
        "--stream-mmap=2",
        "--stream-count=%d" % n,
        "--stream-to=%s" % tmp,
    ]
    r = tools.run(cmd, f"the {width}x{height} capture", timeout=60)
    if not r.ok or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return False, r.error or "v4l2-ctl returned no frames"
    # The stream is n JPEGs back to back; keep the last, which is the settled one.
    try:
        data = tmp.read_bytes()
        starts = []
        i = data.find(b"\xff\xd8")
        while i != -1:
            starts.append(i)
            i = data.find(b"\xff\xd8", i + 2)
        if not starts:
            tmp.unlink(missing_ok=True)
            return False, "no JPEG frame in the stream"
        # The last frame that is whole (ends in the end-of-image marker): a
        # stream cut short leaves a truncated final frame.
        frame = None
        for a, b in zip(reversed(starts), [len(data)] + list(reversed(starts))[:-1]):
            chunk = data[a:b].rstrip(b"\x00")
            if chunk.endswith(b"\xff\xd9"):
                frame = chunk
                break
        if frame is None:
            return False, "no complete JPEG frame in the stream"
        part = Path(str(out_path) + ".part")
        part.write_bytes(frame)
        os.replace(part, out_path)
    finally:
        tmp.unlink(missing_ok=True)
    return True, None


# ---- two cameras side by side ----------------------------------------------
# With a second USB camera set (usb_device2) and the pair aligned (Settings,
# Camera, Align cameras; stored in cam_stitch), every photo and preview is the
# two frames stitched into one wide view, and everything downstream works on
# that as if from one camera. The cameras are read one after the other, so
# they never share the hub's bandwidth.


def stitch_of(cfg):
    """The saved alignment when it applies to the current setup, else None.
    It is made for one pair of devices at one capture size."""
    st = cfg.get("cam_stitch") or {}
    if not cfg.get("usb_device2") or cfg.get("camera_backend", "rpicam") != "usb":
        return None
    if not st.get("h") or st.get("devices") != [cfg.get("usb_device"), cfg.get("usb_device2")]:
        return None
    if st.get("capture") != [int(cfg.get("usb_width", 2048)), int(cfg.get("usb_height", 1536))]:
        return None
    if st.get("rotations") != cam_turns(cfg):
        return None
    return st


def cam_turns(cfg):
    """How far each camera's frames are turned, clockwise: camera 1 by Rotate
    photo, camera 2 by its own setting (the two can be mounted differently)."""
    return [int(cfg.get("cam_rotate", 0) or 0), int(cfg.get("cam2_rotate", 0) or 0)]


STITCH_MAX_W = 3072  # widest the stitched frame may be; bounds memory on the Zero

# each camera's frame from the last Align cameras, shown in Settings
ALIGN_FRAMES = [PREVIEW_PATH.with_name(f"align_cam{i}.jpg") for i in (1, 2)]

# what matched between them (green: agrees with the fit, red: rejected)
ALIGN_MATCHES = PREVIEW_PATH.with_name("align_matches.jpg")


def _usb_capture_view(cfg, out_path, width, height, warmup=None):
    """One frame of the whole view, the right way up: camera 1 alone, or both
    cameras, each turned by its own rotation, stitched when a second camera is
    set and aligned. Until the pair is aligned the photos stay camera 1's, so
    the timelapse never mixes the two kinds."""
    st = stitch_of(cfg)
    turns = cam_turns(cfg)
    if not st:
        ok, err = _usb_capture(cfg, out_path, width, height, warmup=warmup)
        if ok:
            err = _rotate_file(out_path, turns[0])
        return not err, err
    # not *.jpg: everything that lists the timelapse folder takes a .jpg for a photo
    parts = [Path(f"{out_path}.cam{i}.tmp") for i in (1, 2)]
    try:
        for i, (dev, part) in enumerate(zip(st["devices"], parts), 1):
            ok, err = _usb_capture(cfg, part, width, height, warmup=warmup, dev=dev)
            err = err if not ok else _rotate_file(part, turns[i - 1])
            if err:
                return False, f"camera {i}: {err}"
        r = tools.imgtool(
            [
                "stitch",
                parts[0],
                parts[1],
                out_path,
                json.dumps(st["h"]),
                "--max-w",
                STITCH_MAX_W,
                "--q",
                92,
            ],
            "stitching",
        )
        if not r.ok:
            return False, r.error
        return True, None
    finally:
        for part in parts:
            part.unlink(missing_ok=True)


def _frame_to_cam1(pt, st, size):
    """A point given as fractions of a stored photo, in camera 1's pixels."""
    if not st:
        return [pt[0] * size[0], pt[1] * size[1]]
    (x0, y0), (cw, ch) = st["offset"], st["canvas"]
    return [pt[0] * cw + x0, pt[1] * ch + y0]


def _cam1_to_frame(px, st, size):
    if not st:
        return [px[0] / size[0], px[1] / size[1]]
    (x0, y0), (cw, ch) = st["offset"], st["canvas"]
    return [(px[0] - x0) / cw, (px[1] - y0) / ch]


def regrid_for_stitch(cfg, old_st, new_st):
    """Move the grid corners from the old photo geometry to the new one, so
    the grid stays over the same part of the trays when the photos switch
    between one camera and the stitched pair. Both are laid out in camera 1's
    frame turned the right way up. Returns the new corners, or None when
    there are none. The view crop is not moved: the caller resets it."""
    corners = (cfg.get("grid") or {}).get("corners")
    if not corners or len(corners) != 4:
        return None
    w, h = int(cfg.get("usb_width", 2048)), int(cfg.get("usb_height", 1536))
    here = [h, w] if int(cfg.get("cam_rotate", 0) or 0) in (90, 270) else [w, h]
    old_size = (old_st or {}).get("size") or here
    # camera 1's pixels are at the size they were captured at
    new_size = (new_st or {}).get("size") or old_size
    out = []
    for c in corners:
        nf = _cam1_to_frame(_frame_to_cam1(c, old_st, old_size), new_st, new_size)
        out.append([round(min(1.0, max(0.0, v)), 4) for v in nf])
    return out


def align_cameras():
    """Capture both cameras, fit camera 2 onto camera 1, and save the result.
    Returns the imgtool result (ok, inliers, overlap_pct, canvas or error).
    On success the preview shows the stitched view, the grid follows, and
    the crop is reset (it was drawn on the old view)."""
    with config.settings_lock:
        cfg = dict(config.settings)
    devs = [cfg.get("usb_device", "/dev/video0"), cfg.get("usb_device2") or ""]
    if cfg.get("camera_backend", "rpicam") != "usb":
        return {"ok": False, "error": "two cameras need the USB camera type"}
    if not devs[1]:
        return {"ok": False, "error": "set the second camera first"}
    if devs[0] == devs[1] or Path(devs[0]).resolve() == Path(devs[1]).resolve():
        return {"ok": False, "error": "camera 1 and camera 2 are the same device"}
    w, h = int(cfg.get("usb_width", 2048)), int(cfg.get("usb_height", 1536))
    parts = [ALIGN_FRAMES[0], ALIGN_FRAMES[1]]
    global capturing
    if not capture_lock.acquire(timeout=120):
        return {"ok": False, "error": "the camera is busy; try again in a minute"}
    # the light is not changed, only kept steady while the two frames are taken
    if not light_mod.claim(HOLD_ALIGN, 600):
        capture_lock.release()
        return {"ok": False, "error": f"the light is in use ({light_mod.holder()}); try again"}
    try:
        capturing = True
        turns = cam_turns(cfg)
        for i, (dev, part) in enumerate(zip(devs, parts), 1):
            ok, err = _usb_capture(cfg, part, w, h, warmup=4, dev=dev)
            # each turned the right way up first, so the two are matched as
            # they will be shown
            err = err if not ok else _rotate_file(part, turns[i - 1])
            if err:
                return {"ok": False, "error": f"camera {i}: {err}"}
        r = tools.imgtool(
            ["align", parts[0], parts[1], "--matches", ALIGN_MATCHES], "aligning", timeout=180
        )
        if not r.ok:
            return {"ok": False, "error": r.error}
        res = json.loads(r.stdout.decode(errors="replace") or "{}")
        if not res.get("ok"):
            return res
        st = {
            "h": res["h"],
            "size": res["size"],
            "capture": [w, h],
            "rotations": turns,
            "canvas": res["canvas"],
            "offset": res["offset"],
            "devices": devs,
            "inliers": res["inliers"],
            "overlap_pct": res["overlap_pct"],
            "at": int(time.time()),
        }
        tmp = PREVIEW_PATH.with_suffix(".tmp.jpg")
        r = tools.imgtool(
            ["stitch", parts[0], parts[1], tmp, json.dumps(st["h"]), "--max-w", STITCH_MAX_W]
        )
        if r.ok:
            tmp.replace(PREVIEW_PATH)
        with config.settings_lock:
            old = stitch_of(config.settings)
            corners = regrid_for_stitch(config.settings, old, st)
            config.settings["cam_stitch"] = st
            if corners:
                config.settings["grid"] = dict(config.settings.get("grid") or {}, corners=corners)
            config.settings["roi"] = ""
            config.save_config()
        photos_mod.rebuild_thumbs_async()
        log.info(
            f"cameras aligned: {res['inliers']} points, {res['overlap_pct']}% overlap, "
            f"{res['canvas'][0]}x{res['canvas'][1]}"
        )
        db.log_event("camera", f"cameras aligned ({res['inliers']} points)")
        return res
    finally:
        # the two frames stay for Settings to show, so a failed match can be
        # judged by eye
        capturing = False
        capture_lock.release()
        light_mod.release(HOLD_ALIGN)


def unstitch(settings):
    """Drop the saved alignment when it does not fit the settings (a camera
    removed or swapped, a new capture size), and move the grid back to
    camera 1's view. Caller holds settings_lock; returns True if dropped."""
    st = settings.get("cam_stitch") or {}
    if not st.get("h") or stitch_of(settings):
        return False
    corners = regrid_for_stitch(settings, st, None)
    settings["cam_stitch"] = {}
    if corners:
        settings["grid"] = dict(settings.get("grid") or {}, corners=corners)
    return True


def _rotate_file(path, degrees):
    """Rotate a JPEG in place by 90, 180 or 270 degrees; anything else leaves
    it alone. Returns an error message, or None.

    Rotation is baked into the stored file; flattening is applied when the
    image is served, so the grid corners can be re-dragged against the real
    scene."""
    try:
        degrees = int(degrees or 0)
    except (TypeError, ValueError):
        degrees = 0
    if degrees not in (90, 180, 270):
        return None
    r = tools.imgtool(["rotate", path, degrees, "--q", 90], "rotating the photo")
    return None if r.ok else (r.error or "rotating the photo failed")


def light_for_photo(cfg):
    """Whether a photo sets the main light to capture_brightness."""
    if not cfg.get("capture_set_light", False):
        return False
    cam = setups_mod.setup_with(cfg, "camera")
    return cam is None or cam.get("light") == "main"


def _rpicam_capture(cfg, out_path):
    """One full frame from the Pi camera. Returns an error message, or None.

    Always the full frame: the view crop is applied when photos are shown, so
    it can be changed or cleared later without losing any image."""
    cmd = [
        "rpicam-still",
        "-n",
        "--encoding",
        "jpg",  # stated: the file is not named *.jpg while it is being written
        "-o",
        str(out_path),
        "-t",
        "2000",
        "--width",
        str(int(cfg.get("cam_width", 4608))),
        "--height",
        str(int(cfg.get("cam_height", 2592))),
    ]
    return tools.run(cmd, "rpicam-still", timeout=90).error or None


def take_photo(cfg, now, manual=False):
    """Capture one frame; returns its path, or None.

    The light is held only until the frame is on disk, then goes back to the
    schedule while the photo is rotated and thumbnailed. `manual` tags the
    filename with an _m suffix so the daily AI report can prefer scheduled
    frames: a manual shot at midnight is a dark off-schedule photo.

    The frame is captured and rotated under a .part name and takes its .jpg
    name only when it is finished. A capture that fails or is cut short, or a
    rotation that fails, leaves no photo: a frame lying on its side, or half
    of one, never joins the timelapse."""
    global capturing
    if not light_mod.claim(HOLD_PHOTO, PHOTO_HOLD_S):
        log.info(f"photo skipped: the light is in use ({light_mod.holder()})")
        return None
    capturing = True
    usb = cfg.get("camera_backend", "rpicam") == "usb"
    fname = photos_mod.TIMELAPSE_DIR / f"{now:%Y%m%d_%H%M%S}{'_m' if manual else ''}.jpg"
    part = Path(f"{fname}.part")
    try:
        try:
            # The capture brightness is a main-light setting: it applies only
            # when "Set the light for each photo" is on and the camera watches
            # the main light's setup.
            if light_for_photo(cfg):
                light_mod.set_brightness(cfg["capture_brightness"])
            time.sleep(2)  # let light and auto-exposure settle
            if usb:  # comes back the right way up
                ok, err = _usb_capture_view(
                    cfg, part, int(cfg.get("usb_width", 2048)), int(cfg.get("usb_height", 1536))
                )
                err = None if ok else (err or "capture failed")
            else:
                err = _rpicam_capture(cfg, part)
        finally:
            light_mod.release(HOLD_PHOTO)
        if not err and not usb:
            err = _rotate_file(part, cfg.get("cam_rotate"))
        if err:
            log.error(f"capture failed: {err}")
            _camera_fail(err.splitlines()[-1])
            return None
        os.replace(part, fname)
        photos_mod.make_thumb(fname, cfg)
        _camera_ok()
        return fname
    except Exception as e:
        log.error(f"capture error: {e}")
        _camera_fail(str(e))
        return None
    finally:
        part.unlink(missing_ok=True)
        capturing = False


def record_growth(path, cfg, now):
    """Measure per-tray canopy coverage from a just-captured photo and log it.

    Measured per tray because seedlings spill across cell lines, while tray
    boundaries are physical. Runs growth.py as a subprocess so OpenCV's memory
    is freed afterwards."""
    grid = cfg.get("grid") or {}
    if not grid.get("corners"):
        return
    # Tray column spans, left to right as the trays sit under the camera
    # (tray 1 leftmost). Only the trays the camera's setup covers: the grid
    # spans those and nothing else is in the frame.
    mine = set(setups_mod.camera_trays(cfg))
    trays = [
        {"id": tid, "cols": int((t or {}).get("cols", 3))}
        for tid, t in sorted((cfg.get("trays") or {}).items())
        if tid in mine
    ]
    payload = {
        "corners": grid["corners"],
        "rows": grid.get("rows", 4),
        "cols": grid.get("cols", 4),
        "trays": trays,
        "rectify": bool(cfg.get("cam_rectify", True)),
        # the file is rotated at capture time, so analysis must not
        # rotate it a second time
        "rotate": 0,
    }
    r = tools.run(
        [sys.executable, str(GROWTH_SCRIPT), str(path), json.dumps(payload)],
        "canopy analysis",
        timeout=120,
    )
    try:
        out = json.loads(r.stdout.decode(errors="replace") or "{}")
    except ValueError:
        out = {}
    if not out.get("ok"):
        # growth.py reports its own failures as JSON; a crash or a kill is in r.error
        log.error(f"growth: {out.get('error') or r.error or 'no result'}")
        return
    readings = readings_mod.validate_readings(out.get("readings") or {})
    if readings:
        db.log_many(list(readings.items()), ts=int(now.timestamp()))


def capture_window(cfg):
    """Today's photo hours: the light window of the camera's setup (or the
    main light's when no setup holds the camera). The capture loop shoots
    only inside it."""
    with config.state_lock:
        on_time, off_time = config.state["on"], config.state["off"]
    cam = setups_mod.setup_with(cfg, "camera")
    if cam and on_time is not None:
        on_time, off_time = setups_mod.setup_window(cfg, cam, on_time, off_time)
    return on_time, off_time


def canopy_due_since(cfg, now=None):
    """Epoch seconds since when fresh canopy readings are due, or None when
    none are due now.

    Canopy is measured from each photo, and photos are taken only while the
    camera's light is on, with timelapse capture on and grid corners set.
    Inside that window the age counts from when the light came on."""
    if not (cfg.get("camera_enabled") and cfg.get("capture_enabled")):
        return None
    if not (cfg.get("grid") or {}).get("corners"):
        return None
    now = now or datetime.now(ZoneInfo(cfg["timezone"]))
    on_time, off_time = capture_window(cfg)
    if on_time is None or not (on_time <= now <= off_time):
        return None
    return on_time.timestamp()


CAPTURE_SCRATCH = ("*.raw", "*.part", "*.tmp")


def clear_capture_scratch(min_age_s=600):
    """Remove what an interrupted capture left in the timelapse folder.

    A capture cleans up after itself, but not when the process is killed part
    way through. Only files old enough that no capture can still own them.
    """
    removed = 0
    for pattern in CAPTURE_SCRATCH:
        for p in photos_mod.TIMELAPSE_DIR.glob(pattern):
            try:
                if time.time() - p.stat().st_mtime > min_age_s:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    if removed:
        log.info(
            f"removed {removed} leftover capture file(s) from {photos_mod.TIMELAPSE_DIR.name}/"
        )
    return removed


# photos the backfill could not make a thumbnail for, until the next restart
_no_thumb = set()


def capture_loop():
    clear_capture_scratch()
    last_shot = None
    while True:
        try:
            last_shot = _capture_tick(last_shot)
        except Exception:
            # One bad tick (a full disk, an odd file) must not end the thread:
            # photos would stop while the dashboard looks fine.
            log.exception("capture loop error")
        time.sleep(15)


def _capture_tick(last_shot):
    """One pass of the capture loop; returns the new last_shot."""
    # Thumbnail backfill, at most one per tick. Not while a capture is
    # running, nor for a file under 10 s old: that photo may still be being
    # written or rotated, and take_photo makes its own thumbnail.
    missing = (
        None
        if capturing
        else next(
            (
                p
                for p in sorted(photos_mod.TIMELAPSE_DIR.glob("*.jpg"))
                if not (photos_mod.THUMB_DIR / p.name).exists()
                and p.name not in _no_thumb
                and time.time() - p.stat().st_mtime > 10
            ),
            None,
        )
    )
    if missing:
        photos_mod.make_thumb(missing)
        if not (photos_mod.THUMB_DIR / missing.name).exists():
            # an unreadable photo: tried once, not on every tick
            _no_thumb.add(missing.name)
            log.warning(f"no thumbnail could be made for {missing.name}; leaving it")
    with config.settings_lock:
        cfg = dict(config.settings)
    if cfg.get("camera_enabled") and cfg["capture_enabled"]:
        tz = ZoneInfo(cfg["timezone"])
        now = datetime.now(tz)
        on_time, off_time = capture_window(cfg)
        in_day = on_time is not None and on_time <= now <= off_time
        due = last_shot is None or now - last_shot >= timedelta(minutes=cfg["capture_interval_min"])
        if time.time() - photos_mod._pruned_check[0] > 3600:
            photos_mod._pruned_check[0] = time.time()
            if photos_mod.disk_free_gb() < photos_mod.LOW_FREE_GB:
                photos_mod.prune_archives()
        if in_day and due and photos_mod.disk_free_gb() < photos_mod.MIN_FREE_GB:
            # A full SD card stops the database and settings writes too, so
            # photos give way first. Logged once an hour; monitor raises the
            # alert.
            last_shot = now
            if time.time() - _disk_warned[0] > 3600:
                _disk_warned[0] = time.time()
                log.warning(
                    f"photo skipped: only {photos_mod.disk_free_gb():.1f} GB free "
                    f"(photos stop below {photos_mod.MIN_FREE_GB} GB)"
                )
        elif in_day and due and light_mod.holder() is None:
            # with the light in use (a sweep, the lightning effect) the photo
            # waits for the next tick instead of being skipped
            last_shot = now
            with capture_lock:
                path = take_photo(cfg, now)
            if path:
                record_growth(path, cfg, now)
    return last_shot


SYSFS_V4L = Path("/sys/class/video4linux")


def usb_link_speed(dev):
    """The USB link speed of a V4L2 device in Mbit/s (1.5, 12, 480, 5000...),
    or None if it is not a USB device or cannot be read.

    A camera behind the wrong hub enumerates at 12 Mbit/s (USB 1.1) and then
    offers only tiny MJPEG sizes (160x120 is typical), which looks like a
    camera fault. The status and an alert carry this number to name the cause."""
    try:
        # a /dev/v4l/by-path name is a link to the /dev/videoN node sysfs knows
        node = SYSFS_V4L / Path(dev).resolve().name / "device"
        p = node.resolve()
        for _ in range(4):  # interface dir -> device dir with "speed"
            sp = p / "speed"
            if sp.exists():
                return float(sp.read_text().strip())
            p = p.parent
    except Exception:
        pass
    return None


_disk_warned = [0.0]


def parse_mjpeg_modes(text):
    """MJPEG frame sizes from `v4l2-ctl --list-formats-ext`, largest first."""
    modes, in_mjpg = set(), False
    for line in text.splitlines():
        m = re.search(r"\[\d+\]:\s*'(\w+)'", line)
        if m:
            in_mjpg = m.group(1) == "MJPG"
            continue
        m = re.search(r"Size:\s*Discrete\s+(\d+)x(\d+)", line)
        if m and in_mjpg:
            modes.add((int(m.group(1)), int(m.group(2))))
    return sorted(modes, key=lambda wh: wh[0] * wh[1], reverse=True)


def sharpness_score(path):
    """Variance of the Laplacian over the center half of the frame: higher is
    sharper. Center crop because the trays are centered and the frame edges
    are bench clutter that would reward focusing on the wrong thing.
    Returns None when the frame can't be read."""
    try:
        r = tools.imgtool(["sharpness", path], "the sharpness score", timeout=60)
        v = float((r.stdout or b"nan").decode().strip() or "nan")
        return None if r.returncode != 0 or v != v else v
    except Exception as e:
        log.error(f"sharpness score failed: {e}")
        return None


def run_focus_sweep():
    """Coarse pass across the full focus range, fine pass around the winner,
    then pin usb_focus_absolute to the sharpest value. Holds capture_lock for
    the duration (~60-90s) so the timelapse can't interleave, and holds the
    light steady (at capture_brightness when photos set the light, otherwise
    where it is) so every frame is scored under the same light."""
    with config.settings_lock:
        cfg = dict(config.settings)
    dev = cfg.get("usb_device", "/dev/video0")
    tmp = Path(__file__).with_name("_focus_probe.jpg")
    results = []  # (focus, score)
    global capturing
    try:
        with capture_lock:
            if not light_mod.claim(HOLD_FOCUS, 600):
                raise RuntimeError(f"the light is in use ({light_mod.holder()})")
            capturing = True
            if light_for_photo(cfg):
                light_mod.set_brightness(cfg.get("capture_brightness", 100))
            # manual focus must be active or focus_absolute writes are rejected
            subprocess.run(
                ["v4l2-ctl", "-d", dev, "-c", "focus_automatic_continuous=0"],
                capture_output=True,
                timeout=10,
            )

            def score_at(fv):
                subprocess.run(
                    ["v4l2-ctl", "-d", dev, "-c", f"focus_absolute={int(fv)}"],
                    capture_output=True,
                    timeout=10,
                )
                time.sleep(0.6)  # lens travel time
                # _usb_capture applies the configured controls first, so the
                # probe frame needs this focus value in the settings it is given
                probe = {**cfg, "usb_auto_focus": False, "usb_focus_absolute": int(fv)}
                ok, err = _usb_capture(probe, tmp, 1280, 720, warmup=3)
                if not ok:
                    raise RuntimeError(err or "capture failed")
                s = sharpness_score(tmp)
                if s is None:
                    raise RuntimeError("could not score the frame")
                return s

            coarse = list(range(0, 1024, 96)) + [1023]
            fine_span, fine_step = 96, 24
            with focus_lock:
                focus_state["total"] = len(coarse) + 2 * (fine_span // fine_step)
            done = 0
            for fv in coarse:
                if focus_state["cancel"]:
                    return
                results.append((fv, score_at(fv)))
                done += 1
                with focus_lock:
                    focus_state["step"] = done
            best = max(results, key=lambda r: r[1])[0]
            for fv in range(
                max(0, best - fine_span + fine_step), min(1023, best + fine_span), fine_step
            ):
                if focus_state["cancel"]:
                    return
                if any(r[0] == fv for r in results):
                    continue
                results.append((fv, score_at(fv)))
                done += 1
                with focus_lock:
                    focus_state["step"] = done
            best, best_score = max(results, key=lambda r: r[1])
            with config.settings_lock:
                config.settings["usb_focus_absolute"] = int(best)
                config.settings["usb_auto_focus"] = False
                config.save_config()
            with focus_lock:
                focus_state["best"] = {
                    "focus": int(best),
                    "score": round(best_score, 1),
                    "tested": len(results),
                }
            log.info(f"focus sweep: best {best} (score {best_score:.0f}, {len(results)} points)")
            db.log_event("camera", f"focus sweep pinned focus_absolute={best}")
    except Exception as e:
        with focus_lock:
            focus_state["error"] = str(e)[:200]
        log.error(f"focus sweep failed: {e}")
    finally:
        tmp.unlink(missing_ok=True)
        capturing = False
        with focus_lock:
            focus_state["running"] = False
            focus_state["cancel"] = False
        light_mod.release(HOLD_FOCUS)
