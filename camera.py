"""Camera: capture, crop and flatten, thumbnails, focus sweep, the
timelapse render, and canopy tracking."""

import hashlib
import json
import os
import shutil
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
import growth as growth_mod

import config
import light as light_mod
import setups as setups_mod
import monitor

TIMELAPSE_DIR = Path(__file__).with_name("timelapse")
PREVIEW_PATH = Path(__file__).with_name("preview.jpg")  # alignment viewfinder; not a timelapse frame
THUMB_DIR     = TIMELAPSE_DIR / "thumbs"
# ----------------------------------------------------------------------------

TIMELAPSE_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)
# camera health: every capture/preview outcome lands here so the dashboard
# can tell "old photo because night" from "old photo because the camera died"
camera = {"last_ok": None, "fails": 0, "last_err": "", "last_err_ts": None}

# focus sweep: walk focus_absolute, score each frame's sharpness, pin the best.
# Replaces guessing focus values over SSH (a guessed 68 produced a week of
# blurry photos). step counts down as coarse then fine passes run.
focus_state = {"running": False, "step": 0, "total": 0, "best": None,
               "error": "", "cancel": False}
focus_lock = threading.Lock()


def _camera_ok():
    with config.state_lock:
        camera.update(last_ok=time.time(), fails=0, last_err="", last_err_ts=None)


def _camera_fail(err):
    with config.state_lock:
        camera["fails"] += 1
        camera["last_err"] = str(err)[:200]
        camera["last_err_ts"] = time.time()
capturing = False   # capture thread holds the light; control loop defers
capture_lock = threading.Lock()  # serialize camera access (manual vs scheduled)
render = {"state": "idle", "msg": "", "frames": 0,
          "started": None, "elapsed": None}   # idle|running|done|error
render_lock = threading.Lock()
VIDEO_PATH = TIMELAPSE_DIR / "timelapse.mp4"
ARCHIVE_DIR = Path(__file__).with_name("timelapse_archive")
GROWTH_SCRIPT = Path(__file__).with_name("growth.py")


def crop_box(cfg=None):
    """The configured view crop as (x, y, w, h) fractions, or None for full frame."""
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    try:
        return parse_roi(cfg.get("roi", ""))
    except ValueError:
        return None


def crop_filter(roi):
    """The same crop as an ffmpeg filter, sized to even pixel counts."""
    x, y, w, h = roi
    return (f"crop=trunc(iw*{w:.4f}/2)*2:trunc(ih*{h:.4f}/2)*2:"
            f"trunc(iw*{x:.4f}):trunc(ih*{y:.4f})")


def crop_array(img, roi):
    """Crop an OpenCV image to the view crop."""
    ih, iw = img.shape[:2]
    x, y, w, h = roi
    x0, y0 = int(round(iw * x)), int(round(ih * y))
    x1, y1 = min(iw, x0 + max(2, int(round(iw * w)))), min(ih, y0 + max(2, int(round(ih * h))))
    return img[y0:y1, x0:x1]


def parse_roi(s):
    """Validate 'x,y,w,h' fraction string. Returns tuple or None for blank."""
    s = (s or "").strip()
    if not s:
        return None
    parts = [float(p) for p in s.split(",")]
    if len(parts) != 4:
        raise ValueError("need four numbers")
    x, y, w, h = parts
    if not (0 <= x < 1 and 0 <= y < 1 and 0.05 <= w <= 1 and 0.05 <= h <= 1
            and x + w <= 1.001 and y + h <= 1.001):
        raise ValueError("out of range")
    return x, y, w, h



def oom_first(cmd):
    """Run cmd as the kernel's first choice to kill when memory runs out.

    The Pi has little memory the kernel can hand to drivers, and a big camera
    capture or an image job can exhaust it. On 25 Sep the camera's buffer
    request tipped the box over three times and the kernel killed the whole
    controller (lights, pumps, dashboard) each time. A helper started through
    this raises its own oom_score_adj to the maximum before exec, so the
    helper dies instead, the job fails and is logged, and the controller keeps
    running. Raising one's own score needs no privilege.
    """
    return (["sh", "-c", 'echo 1000 > /proc/self/oom_score_adj 2>/dev/null; exec "$@"',
             "oom-first"] + [str(c) for c in cmd])


def killed_msg(r, what):
    """Name an out-of-memory kill instead of reporting "no output"."""
    if r.returncode in (-9, 137):
        return (f"{what} was killed by the kernel for running out of memory; "
                "a smaller camera mode needs less (see Settings, Camera, Camera modes)")
    return None

_thumb_lock = threading.Lock()


def make_thumb(photo_path, cfg=None, dst_dir=None):
    """640px thumbnail for the browser player. Cheap, one-time per photo.

    Rectified to match the snapshot and the rendered video, so scrubbing the
    timelapse shows the same corrected view as everything else. Falls back to a
    plain scale if the grid corners are not set or OpenCV is unavailable.

    One at a time, and written under a temporary name then renamed: a manual
    capture and the capture loop's backfill used to thumbnail the same new
    photo at once (two ffmpeg runs writing one file), and a thumbnail is
    served as immutable for a year, so a bad one would stick.
    """
    dst = (dst_dir or THUMB_DIR) / photo_path.name
    if dst.exists():
        return
    with _thumb_lock:
        if dst.exists():                 # made while this call waited
            return
        _make_thumb(photo_path, cfg, dst)


def _make_thumb(photo_path, cfg, dst):
    part = dst.with_name(dst.name + ".part")     # not *.jpg: never listed
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    grid = (cfg.get("grid") or {})
    corners = grid.get("corners")
    if cfg.get("timelapse_flatten", True) and corners and len(corners) == 4:
        try:
            import cv2
            img = cv2.imread(str(photo_path))
            if img is not None:
                warped = growth_mod.rectify(img, corners,
                                            cols=int(grid.get("cols", 4)),
                                            rows=int(grid.get("rows", 4)))
                h, w = warped.shape[:2]
                if w > 640:
                    warped = cv2.resize(warped, (640, max(1, int(h * 640 / w))))
                ok, buf = cv2.imencode(".jpg", warped, [cv2.IMWRITE_JPEG_QUALITY, 82])
                if ok:
                    part.write_bytes(buf.tobytes())
                    os.replace(part, dst)
                    return
        except Exception as e:
            log.error(f"thumb rectify failed for {photo_path.name} ({e}); plain scale")
    roi = crop_box(cfg)
    vf = (crop_filter(roi) + "," if roi else "") + "scale=640:-2"
    try:
        r = subprocess.run(
            oom_first(["ffmpeg", "-loglevel", "error", "-y", "-i", str(photo_path),
                       "-vf", vf, "-q:v", "7", "-f", "mjpeg", str(part)]),
            capture_output=True, timeout=120)
        if r.returncode == 0 and part.exists() and part.stat().st_size:
            os.replace(part, dst)
        else:
            log.error(f"thumbnail error for {photo_path.name}: "
                      f"{(r.stderr or b'').decode(errors='replace')[-200:] or 'no output'}")
    except Exception as e:
        log.error(f"thumbnail error for {photo_path.name}: {e}")
    finally:
        part.unlink(missing_ok=True)

_frame_lock = threading.Lock()   # one full-size warp at a time on a 512 MB board


def frame_view(photo_path, cfg=None):
    """A stored photo at full resolution, framed the way its thumbnail is.

    The player scrubs through 640px thumbnails, which is right for playing
    at 8 frames a second but soft once the browser stretches one across the
    card (twice over on a phone or a HiDPI screen). A paused frame is shown
    through this instead, at the snapshot's resolution. Same rules as
    make_thumb, so swapping one for the other never reframes the picture:
    flattened when timelapse_flatten is on and the grid has corners, else cut
    to the view crop, else the photo itself.

    Returns (jpeg bytes, None), or (None, path) to send the file as it is.
    """
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    grid = cfg.get("grid") or {}
    corners = grid.get("corners")
    flatten = bool(cfg.get("timelapse_flatten", True) and corners and len(corners) == 4)
    roi = None if flatten else crop_box(cfg)
    if not flatten and not roi:
        return None, photo_path
    import cv2
    with _frame_lock:
        img = cv2.imread(str(photo_path))
        if img is None:
            raise ValueError("unreadable photo")
        if flatten:
            out = growth_mod.rectify(img, corners, cols=int(grid.get("cols", 4)),
                                     rows=int(grid.get("rows", 4)))
            q = 85                       # as /rectified.jpg
        else:
            out = crop_array(img, roi)
            q = 88                       # as /photo/cropped.jpg
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, q])
    if not ok:
        raise ValueError("encode failed")
    return buf.tobytes(), None


def frame_etag(photo_path, cfg):
    """Changes whenever what frame_view would return changes."""
    grid = cfg.get("grid") or {}
    key = json.dumps([photo_path.name, photo_path.stat().st_mtime,
                      bool(cfg.get("timelapse_flatten", True)), grid.get("corners"),
                      grid.get("cols"), grid.get("rows"), cfg.get("roi")])
    return hashlib.sha1(key.encode()).hexdigest()[:20]

def photo_inventory():
    # leading underscore marks render scratch, which is not a captured photo
    photos = sorted(p for p in TIMELAPSE_DIR.glob("*.jpg")
                    if not p.name.startswith("_"))
    if not photos:
        return 0, None, None
    latest = photos[-1]
    return len(photos), latest, datetime.fromtimestamp(latest.stat().st_mtime)


# --------------------------- capture loop ---------------------------

# Each entry: config flag -> (v4l2 auto control, value for auto, value for
# manual, the manual controls it unlocks). Auto is offered because it is what
# people expect, but note that for a timelapse it produces visible flicker:
# the camera re-decides exposure and white balance every frame, so brightness
# and colour shift between shots and the per-cell analysis moves with them.
USB_AUTO_GROUPS = {
    "usb_auto_focus": ("focus_automatic_continuous", 1, 0, ("focus_absolute",)),
    "usb_auto_exposure_on": ("auto_exposure", 3, 1,
                             ("exposure_time_absolute", "gain")),
    "usb_auto_white_balance": ("white_balance_automatic", 1, 0,
                               ("white_balance_temperature",)),
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
            continue                      # let the camera decide these
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
        subprocess.run(["v4l2-ctl", "-d", dev] + args,
                       capture_output=True, timeout=10)


def _usb_capture(cfg, out_path, width, height, warmup=None):
    """Grab one MJPEG frame from a UVC camera via v4l2-ctl.

    UVC sensors need a few frames before auto-gain settles, and the very first
    frame after opening the device is frequently dark or torn. Capturing a
    short burst and keeping the last frame costs a second and removes that
    whole class of bad photo.
    """
    dev = cfg.get("usb_device", "/dev/video0")
    if not Path(dev).exists():
        return False, f"{dev} not present"
    _usb_apply_controls(cfg, dev)
    n = int(cfg.get("usb_warmup_frames", 4) if warmup is None else warmup)
    n = max(1, min(20, n))
    tmp = Path(str(out_path) + ".raw")
    # Two buffers, not v4l2-ctl's default four: the driver allocates each at
    # the camera's worst-case frame size (16 MB at 3264x2448) from memory the
    # kernel cannot take from its contiguous pool, and four of those is what
    # ran the Pi out of memory. Frames are kept one at a time anyway.
    cmd = ["v4l2-ctl", "-d", dev,
           "--set-fmt-video=width=%d,height=%d,pixelformat=MJPG" % (width, height),
           "--stream-mmap=2", "--stream-count=%d" % n, "--stream-to=%s" % tmp]
    try:
        r = subprocess.run(oom_first(cmd), capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        return False, "capture timed out"
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        oom = killed_msg(r, f"the {width}x{height} capture")
        if oom:
            log.warning(oom)
            return False, oom
        return False, (r.stderr.decode(errors="replace")[-200:].strip()
                       or "v4l2-ctl returned no frames")
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
        Path(out_path).write_bytes(data[starts[-1]:])
    finally:
        tmp.unlink(missing_ok=True)
    return True, None


def _postprocess_file(path, cfg):
    """Rotate a just-captured JPEG in place.

    Rotation is baked in because it is not a matter of interpretation: the
    camera is mounted upside down and every consumer wants it the right way up.
    Flattening deliberately is NOT baked in -- it is applied when the image is
    served, so the stored frame stays raw and the grid corners can always be
    re-dragged against the real scene.

    Failure is non-fatal: the original frame is kept and the reason is logged.
    """
    degrees = int(cfg.get("cam_rotate", 0) or 0)
    if degrees not in (90, 180, 270):
        return
    try:
        import cv2
        img = cv2.imread(str(path))
        if img is None:
            return
        if degrees in (90, 180, 270):
            img = cv2.rotate(img, {90: cv2.ROTATE_90_CLOCKWISE,
                                   180: cv2.ROTATE_180,
                                   270: cv2.ROTATE_90_COUNTERCLOCKWISE}[degrees])
        cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    except Exception as e:
        log.error(f"post-process failed ({e}); keeping the frame as captured")


# kept for callers that only need the rotation
def _rotate_file(path, degrees):
    _postprocess_file(path, {"cam_rotate": degrees})



def light_for_photo(cfg):
    """Whether a photo sets the main light to capture_brightness."""
    if not cfg.get("capture_set_light", False):
        return False
    cam = setups_mod.setup_with(cfg, "camera")
    return cam is None or cam.get("light") == "main"

def take_photo(cfg, now, manual=False):
    """Capture one frame. `manual` tags the filename with an _m suffix so the
    daily AI report can prefer scheduled frames: a manual shot at midnight is
    a dark off-schedule photo that would otherwise become the report's input."""
    global capturing
    capturing = True
    saved = None
    try:
        # The light is left as it is unless "Set the light for each photo" is
        # on. The capture brightness is a main-light setting: only apply it
        # when the camera watches the main light's setup, or it would change
        # the wrong area and leave the photographed one as it was.
        if light_for_photo(cfg):
            light_mod.set_brightness(cfg["capture_brightness"])
        time.sleep(2)  # let light and auto-exposure settle
        suffix = "_m" if manual else ""
        fname = TIMELAPSE_DIR / f"{now:%Y%m%d_%H%M%S}{suffix}.jpg"
        cw = int(cfg.get("cam_width", 4608))
        ch = int(cfg.get("cam_height", 2592))
        if cfg.get("camera_backend", "rpicam") == "usb":
            ok, err = _usb_capture(cfg, fname,
                                   int(cfg.get("usb_width", 2048)),
                                   int(cfg.get("usb_height", 1536)))
            if ok:
                _postprocess_file(fname, cfg)
                make_thumb(fname, cfg)
                saved = fname
                _camera_ok()
            else:
                log.warning(f"capture failed: {err}")
                _camera_fail(err)
            return saved

        # Full frame always: the view crop is applied when photos are shown,
        # so it can be changed or cleared later without losing any image.
        cmd = ["rpicam-still", "-n", "-o", str(fname), "-t", "2000",
               "--width", str(cw), "--height", str(ch)]
        r = subprocess.run(oom_first(cmd), capture_output=True, timeout=90)
        if r.returncode != 0:
            err = killed_msg(r, "rpicam-still") or r.stderr.decode(errors="replace")[-300:]
            log.error(f"capture failed: {err}")
            _camera_fail(err.strip().splitlines()[-1] if err.strip() else "capture failed")
        else:
            _postprocess_file(fname, cfg)
            make_thumb(fname, cfg)
            saved = fname
            _camera_ok()
    except Exception as e:
        log.error(f"capture error: {e}")
        _camera_fail(str(e))
    finally:
        capturing = False
        config.wake.set()  # control loop restores scheduled brightness now
    return saved


def record_growth(path, cfg, now):
    """Measure per-tray canopy coverage from a just-captured photo and log it.
    Per-cell measurement was retired: seedlings spill across cell lines and the
    attribution becomes fiction, while tray boundaries are physical. Runs
    growth.py as a subprocess so OpenCV memory is freed afterwards."""
    grid = cfg.get("grid") or {}
    if not grid.get("corners"):
        return
    # tray column spans, left to right, mirroring how the trays sit under the
    # camera (tray 1 leftmost)
    # only the trays the camera's setup covers: the grid spans those, left to
    # right, and nothing else is in the frame to measure
    mine = set(setups_mod.camera_trays(cfg))
    trays = [{"id": tid, "cols": int((t or {}).get("cols", 3))}
             for tid, t in sorted((cfg.get("trays") or {}).items()) if tid in mine]
    payload = {"corners": grid["corners"],
               "rows": grid.get("rows", 4), "cols": grid.get("cols", 4),
               "trays": trays,
               "rectify": bool(cfg.get("cam_rectify", True)),
               # the file is rotated at capture time, so analysis must not
               # rotate it a second time
               "rotate": 0}
    try:
        r = subprocess.run(oom_first([sys.executable, str(GROWTH_SCRIPT), str(path),
                                      json.dumps(payload)]),
                           capture_output=True, timeout=120)
        if killed_msg(r, "canopy analysis"):
            log.warning(killed_msg(r, "canopy analysis"))
        out = json.loads((r.stdout or b"{}").decode(errors="replace") or "{}")
    except Exception as e:
        log.error(f"growth analyze error: {e}")
        return
    if not out.get("ok"):
        if out.get("error"):
            log.error(f"growth: {out['error']}")
        return
    readings = monitor.validate_readings(out.get("readings") or {})
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
    camera's light is on, with timelapse capture on and grid corners set. At
    night the last reading is hours old by design, so it must not read as
    stale; and in the morning the age counts from when the light came on,
    not from last night's final photo.
    """
    if not (cfg.get("camera_enabled") and cfg.get("capture_enabled")):
        return None
    if not (cfg.get("grid") or {}).get("corners"):
        return None
    now = now or datetime.now(ZoneInfo(cfg["timezone"]))
    on_time, off_time = capture_window(cfg)
    if on_time is None or not (on_time <= now <= off_time):
        return None
    return on_time.timestamp()

def capture_loop():
    last_shot = None
    while True:
        # opportunistic thumbnail backfill, at most one per tick
        # Not while a capture is running, nor for a file under 10 s old: that
        # photo may still be being written or rotated, and take_photo makes
        # its own thumbnail once it is done.
        missing = None if capturing else next(
            (p for p in sorted(TIMELAPSE_DIR.glob("*.jpg"))
             if not (THUMB_DIR / p.name).exists()
             and time.time() - p.stat().st_mtime > 10), None)
        if missing:
            make_thumb(missing)
        with config.settings_lock:
            cfg = dict(config.settings)
        if cfg.get("camera_enabled") and cfg["capture_enabled"]:
            tz = ZoneInfo(cfg["timezone"])
            now = datetime.now(tz)
            on_time, off_time = capture_window(cfg)
            in_day = on_time is not None and on_time <= now <= off_time
            due = (last_shot is None or
                   now - last_shot >= timedelta(minutes=cfg["capture_interval_min"]))
            if in_day and due:
                last_shot = now
                with capture_lock:
                    path = take_photo(cfg, now)
                if path:
                    record_growth(path, cfg, now)
        time.sleep(15)


# ----------------------------- video render -----------------------------

def _flatten_frames_to(dest, frames, cfg):
    """Write rectified copies of `frames` into `dest`, numbered in order.

    The timelapse is rendered from flattened frames so the video matches what
    the dashboard shows, but the originals on disk stay raw: that is what keeps
    the grid corners re-draggable against the real scene. Frames that cannot be
    rectified are copied through unchanged rather than dropped, so a bad frame
    leaves a blip instead of a gap in the timeline.
    """
    import shutil
    grid = cfg.get("grid") or {}
    corners = grid.get("corners")
    if not corners or len(corners) != 4:
        return None
    try:
        import cv2
    except Exception:
        return None
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.jpg"):
        old.unlink()
    cols, rows = int(grid.get("cols", 4)), int(grid.get("rows", 4))
    for i, src in enumerate(frames):
        out = dest / f"{i:06d}.jpg"
        try:
            img = cv2.imread(str(src))
            if img is None:
                raise ValueError("unreadable")
            warped = growth_mod.rectify(img, corners, cols=cols, rows=rows)
            cv2.imwrite(str(out), warped, [cv2.IMWRITE_JPEG_QUALITY, 88])
        except Exception:
            shutil.copyfile(src, out)
    return dest


def render_worker():
    import time as _t
    t0 = _t.monotonic()
    frames = sorted(p for p in TIMELAPSE_DIR.glob("*.jpg")
                    if not p.name.startswith("_"))
    with render_lock:
        render.update(state="running", frames=len(frames),
                      started=datetime.now(ZoneInfo(config.settings["timezone"])).isoformat(),
                      elapsed=None, msg=f"Rendering {len(frames)} frames...")
    flat_dir = None
    try:
        with config.settings_lock:
            cfg_r = dict(config.settings)
        if cfg_r.get("timelapse_flatten", True):
            with render_lock:
                render["msg"] = f"Flattening {len(frames)} frames..."
            flat_dir = _flatten_frames_to(TIMELAPSE_DIR / "_flat", frames, cfg_r)
        src_glob = str((flat_dir or TIMELAPSE_DIR) / "*.jpg")
        # the view crop applies to raw frames; flattened ones are already the tray
        roi = None if flat_dir else crop_box(cfg_r)
        # lanczos: a sharper downscale than the default bicubic, same memory
        vf_scale = "scale=1280:-16:flags=lanczos:in_range=full:out_range=tv"
        vf = (crop_filter(roi) + "," + vf_scale) if roi else vf_scale
        tmp = TIMELAPSE_DIR / "_render_tmp.mp4"
        # Encode pass: small footprint so the 512MB Zero never OOMs.
        # 1280-wide, ultrafast, single thread, no faststart here (the
        # +faststart second pass rewrites the whole file in memory and is
        # what tips the box over). We add faststart as a cheap remux after.
        r = subprocess.run(oom_first(
            ["ffmpeg", "-loglevel", "error", "-y",
             "-framerate", "24", "-pattern_type", "glob",
             "-i", src_glob,
             # JPEG stills are full-range (yuvj420p/pc); browsers render that as
             # black. Remap to limited-range yuv420p and tag it. Height is forced
             # to a multiple of 16 (-16, not -2): a non-mod16 height makes the
             # encoder signal a crop that some hardware decoders render as black.
             "-vf", vf,
             # crf 20, not 24: ultrafast turns off adaptive quantization and
             # deblocking, so at 24 fine leaf and soil texture smeared. Same
             # preset, so encode time and memory stay the same; files grow.
             "-c:v", "libx264", "-preset", "ultrafast",
             "-crf", "20", "-threads", "1",
             "-pix_fmt", "yuv420p", "-color_range", "tv",
             # Fully specify the colour metadata (BT.601, matching the JPEG
             # source). An unspecified matrix makes some hardware decoders
             # (VLC's, browsers') render the video as black.
             "-colorspace", "smpte170m", "-color_primaries", "smpte170m",
             "-color_trc", "smpte170m",
             str(tmp)]),
            capture_output=True, timeout=3600)
        # Faststart as a stream-copy remux: no re-encode, trivial memory.
        if r.returncode == 0 and tmp.exists():
            # Remux beside the finished video, then swap it in with one
            # rename: a download running during the remux keeps reading the
            # old file instead of a half-written one.
            part = VIDEO_PATH.with_name("timelapse.part.mp4")
            r2 = subprocess.run(
                oom_first(["ffmpeg", "-loglevel", "error", "-y", "-i", str(tmp),
                           "-c", "copy", "-movflags", "+faststart", str(part)]),
                capture_output=True, timeout=600)
            tmp.unlink(missing_ok=True)
            if r2.returncode == 0 and part.exists():
                os.replace(part, VIDEO_PATH)
            else:
                part.unlink(missing_ok=True)
                r = r2  # surface the remux error below
        dt = _t.monotonic() - t0
        if r.returncode == 0 and VIDEO_PATH.exists():
            mb = VIDEO_PATH.stat().st_size / 1e6
            with render_lock:
                render.update(state="done", elapsed=round(dt, 1),
                              msg=f"{len(frames)} frames, {mb:.1f} MB, {dt:.0f}s")
        else:
            err = r.stderr.decode(errors="replace")[-200:]
            with render_lock:
                render.update(state="error", elapsed=round(dt, 1),
                              msg=err or "ffmpeg failed")
    except Exception as e:
        with render_lock:
            render.update(state="error", elapsed=round(_t.monotonic() - t0, 1),
                          msg=str(e))
    finally:
        if flat_dir and flat_dir.exists():
            # scratch only: the SD card cannot afford a second copy of the set
            import shutil
            shutil.rmtree(flat_dir, ignore_errors=True)


def start_render():
    with render_lock:
        if render["state"] == "running":
            return False
        render.update(state="running", msg="Starting...")
    threading.Thread(target=render_worker, daemon=True).start()
    return True


_thumbs_lock = threading.Lock()


def rebuild_thumbs_async():
    """Regenerate every thumbnail in the background, one rebuild at a time."""
    def work():
        if not _thumbs_lock.acquire(blocking=False):
            return                     # one already running; it reads settings fresh
        try:
            with config.settings_lock:
                cfg = dict(config.settings)
            photos = [p for p in sorted(TIMELAPSE_DIR.glob("*.jpg"))
                      if not p.name.startswith("_")]
            # Build each new thumbnail beside the old ones and swap it in, so
            # the scrubber's frame list never empties while this runs.
            tmp = THUMB_DIR / "_rebuild"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            keep = set()
            for ph in photos:
                make_thumb(ph, cfg, dst_dir=tmp)
                if (tmp / ph.name).exists():
                    os.replace(tmp / ph.name, THUMB_DIR / ph.name)
                    keep.add(ph.name)
            for old in THUMB_DIR.glob("*.jpg"):
                if old.name not in keep:
                    old.unlink(missing_ok=True)     # its photo is gone
            shutil.rmtree(tmp, ignore_errors=True)
            # thumbnails are cached by browsers for a year under their name;
            # a new version in the URL is what makes them fetch the new ones
            db.kv_set("thumbs_version", int(time.time()))
            log.info(f"rebuilt {len(photos)} thumbnails "
                     f"({'flattened' if cfg.get('timelapse_flatten', True) else 'raw'})")
        finally:
            _thumbs_lock.release()
    threading.Thread(target=work, daemon=True, name="thumbs").start()


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
        import cv2
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        h, w = img.shape[:2]
        img = img[h // 4: 3 * h // 4, w // 4: 3 * w // 4]
        return float(cv2.Laplacian(img, cv2.CV_64F).var())
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
    results = []          # (focus, score)
    global capturing
    try:
        with capture_lock:
            capturing = True                  # control loop leaves the light alone
            if light_for_photo(cfg):
                light_mod.set_brightness(cfg.get("capture_brightness", 100))
            # manual focus must be active or focus_absolute writes are rejected
            subprocess.run(["v4l2-ctl", "-d", dev,
                            "-c", "focus_automatic_continuous=0"],
                           capture_output=True, timeout=10)

            def score_at(fv):
                subprocess.run(["v4l2-ctl", "-d", dev,
                                "-c", f"focus_absolute={int(fv)}"],
                               capture_output=True, timeout=10)
                time.sleep(0.6)               # lens travel time
                ok, err = _usb_capture(cfg, tmp, 1280, 720, warmup=3)
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
            for fv in range(max(0, best - fine_span + fine_step),
                            min(1023, best + fine_span), fine_step):
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
                focus_state["best"] = {"focus": int(best),
                                       "score": round(best_score, 1),
                                       "tested": len(results)}
            log.info(f"focus sweep: best {best} "
                  f"(score {best_score:.0f}, {len(results)} points)")
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
        config.wake.set()                            # control loop restores the light


_rect_cache = {"key": None, "bytes": None}
_rect_lock = threading.Lock()
_crop_cache = {"key": None, "bytes": None}
