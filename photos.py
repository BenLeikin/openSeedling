"""The stored timelapse photos: thumbnails, the cropped or flattened views the
dashboard shows, what is on disk, and making room when the card fills."""

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from applog import log
import db
import config

import tools

TIMELAPSE_DIR = Path(__file__).with_name("timelapse")

THUMB_DIR = TIMELAPSE_DIR / "thumbs"

TIMELAPSE_DIR.mkdir(exist_ok=True)

THUMB_DIR.mkdir(exist_ok=True)

ARCHIVE_DIR = Path(__file__).with_name("timelapse_archive")


def crop_box(cfg=None):
    """The configured view crop as (x, y, w, h) fractions, or None for full frame."""
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    try:
        return config.parse_roi(cfg.get("roi", ""))
    except ValueError:
        return None


def crop_filter(roi):
    """The same crop as an ffmpeg filter, sized to even pixel counts."""
    x, y, w, h = roi
    return f"crop=trunc(iw*{w:.4f}/2)*2:trunc(ih*{h:.4f}/2)*2:trunc(iw*{x:.4f}):trunc(ih*{y:.4f})"


_thumb_lock = threading.Lock()


def make_thumb(photo_path, cfg=None, dst_dir=None):
    """Write the 640 px thumbnail the browser player uses, once per photo.

    Flattened to match the snapshot and the rendered video; a plain scale if
    the grid has no corners or the rectify job fails. One at a time, and
    renamed into place: the capture loop's backfill can ask for the same photo
    as a manual capture, and a thumbnail is served as immutable for a year."""
    dst = (dst_dir or THUMB_DIR) / photo_path.name
    if dst.exists():
        return
    with _thumb_lock:
        if dst.exists():  # made while this call waited
            return
        _make_thumb(photo_path, cfg, dst)


def _make_thumb(photo_path, cfg, dst):
    part = dst.with_name(dst.name + ".part")  # not *.jpg: never listed
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    grid = cfg.get("grid") or {}
    corners = grid.get("corners")
    if cfg.get("timelapse_flatten", True) and corners and len(corners) == 4:
        try:
            # a 640 px thumbnail does not need all 8 megapixels decoded
            r = tools.imgtool(
                [
                    "rectify",
                    photo_path,
                    part,
                    json.dumps(corners),
                    int(grid.get("cols", 4)),
                    int(grid.get("rows", 4)),
                    "--max-w",
                    640,
                    "--min-side",
                    1280,
                    "--q",
                    82,
                ]
            )
            if r.returncode == 0 and part.exists() and part.stat().st_size:
                os.replace(part, dst)
                return
            log.error(
                f"thumb rectify failed for {photo_path.name} ({tools._imgtool_err(r)}); plain scale"
            )
        except Exception as e:
            log.error(f"thumb rectify failed for {photo_path.name} ({e}); plain scale")
    roi = crop_box(cfg)
    vf = (crop_filter(roi) + "," if roi else "") + "scale=640:-2"
    try:
        r = subprocess.run(
            tools.oom_first(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(photo_path),
                    "-vf",
                    vf,
                    "-q:v",
                    "7",
                    "-f",
                    "mjpeg",
                    str(part),
                ]
            ),
            capture_output=True,
            timeout=120,
        )
        if r.returncode == 0 and part.exists() and part.stat().st_size:
            os.replace(part, dst)
        else:
            log.error(
                f"thumbnail error for {photo_path.name}: "
                f"{(r.stderr or b'').decode(errors='replace')[-200:] or 'no output'}"
            )
    except Exception as e:
        log.error(f"thumbnail error for {photo_path.name}: {e}")
    finally:
        part.unlink(missing_ok=True)


_frame_lock = threading.Lock()  # one full-size warp at a time on a 512 MB board


def frame_view(photo_path, cfg=None):
    """A stored photo at full resolution, framed the way its thumbnail is.

    For a paused frame, where a stretched 640 px thumbnail looks soft.
    Flattened when timelapse_flatten is on and the grid has corners, else cut
    to the view crop, else the photo itself. Returns (jpeg bytes, None), or
    (None, path) to send the file as it is; raises ValueError if the job fails."""
    if cfg is None:
        with config.settings_lock:
            cfg = dict(config.settings)
    grid = cfg.get("grid") or {}
    corners = grid.get("corners")
    flatten = bool(cfg.get("timelapse_flatten", True) and corners and len(corners) == 4)
    roi = None if flatten else crop_box(cfg)
    if not flatten and not roi:
        return None, photo_path
    with _frame_lock:
        if flatten:  # quality as /rectified.jpg
            r = tools.imgtool(
                [
                    "rectify",
                    photo_path,
                    "-",
                    json.dumps(corners),
                    int(grid.get("cols", 4)),
                    int(grid.get("rows", 4)),
                    "--q",
                    85,
                ]
            )
        else:  # quality as /photo/cropped.jpg
            r = tools.imgtool(["crop", photo_path, "-", json.dumps(list(roi)), "--q", 88])
    if r.returncode != 0 or not r.stdout:
        raise ValueError(tools._imgtool_err(r))
    return r.stdout, None


def frame_etag(photo_path, cfg):
    """Changes whenever what frame_view would return changes."""
    grid = cfg.get("grid") or {}
    key = json.dumps(
        [
            photo_path.name,
            photo_path.stat().st_mtime,
            bool(cfg.get("timelapse_flatten", True)),
            grid.get("corners"),
            grid.get("cols"),
            grid.get("rows"),
            cfg.get("roi"),
        ]
    )
    return hashlib.sha1(key.encode()).hexdigest()[:20]


def photo_inventory():
    # leading underscore marks render scratch, which is not a captured photo
    photos = sorted(p for p in TIMELAPSE_DIR.glob("*.jpg") if not p.name.startswith("_"))
    for latest in reversed(photos):
        try:
            return len(photos), latest, datetime.fromtimestamp(latest.stat().st_mtime)
        except FileNotFoundError:
            continue  # removed since the listing (a reset, or pruning for space)
    return 0, None, None


MIN_FREE_GB = 1.0  # stop taking photos below this much free space

LOW_FREE_GB = 3.0  # alert below this, and start removing archived runs

PRUNE_TARGET_GB = 4.0  # removing archived runs stops once this much is free

_pruned_check = [0.0]


def prune_archives(free_fn=None):
    """Delete archived timelapse runs, oldest first, when the SD card is low.

    Starts below LOW_FREE_GB and stops once PRUNE_TARGET_GB is free, keeping
    the newest archive unless free space is below MIN_FREE_GB (where photos
    would stop anyway). The current run is never touched. Each removal is
    logged as an event. Returns the names removed."""
    free_fn = free_fn or disk_free_gb
    removed = []
    try:
        runs = sorted(p for p in ARCHIVE_DIR.iterdir() if p.is_dir())
    except FileNotFoundError:
        return removed
    while runs and free_fn() < PRUNE_TARGET_GB:
        if len(runs) == 1 and free_fn() >= MIN_FREE_GB:
            break  # the newest archive stays unless it is that or the photos
        if removed == [] and free_fn() >= LOW_FREE_GB:
            break  # only start below the alert level
        old = runs.pop(0)
        n = sum(1 for _ in old.glob("*.jpg"))
        mb = sum(f.stat().st_size for f in old.rglob("*") if f.is_file()) / 1e6
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old.name)
        msg = (
            f"removed archived timelapse {old.name} ({n} photos, {mb:.0f} MB): "
            f"the SD card was low on space"
        )
        log.warning(msg)
        try:
            db.log_event("disk", msg)
        except Exception:
            pass
    return removed


def disk_free_gb(path=None):
    try:
        return shutil.disk_usage(path or TIMELAPSE_DIR).free / 1e9
    except Exception:
        return 1e9  # unknown: do not block photos on a failed check


def jpeg_size(path):
    """(width, height) from a JPEG's header, without decoding it; None if
    it cannot be read."""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            while True:
                b = f.read(1)
                while b and b != b"\xff":
                    b = f.read(1)
                while b == b"\xff":
                    b = f.read(1)
                if not b:
                    return None
                marker = b[0]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    continue
                ln = int.from_bytes(f.read(2), "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    seg = f.read(5)
                    return int.from_bytes(seg[3:5], "big"), int.from_bytes(seg[1:3], "big")
                f.seek(ln - 2, 1)
    except Exception:
        return None


_thumbs_lock = threading.Lock()


def rebuild_thumbs_async():
    """Regenerate every thumbnail in the background, one rebuild at a time."""

    def work():
        if not _thumbs_lock.acquire(blocking=False):
            return  # one already running; it reads settings fresh
        try:
            with config.settings_lock:
                cfg = dict(config.settings)
            photos = [p for p in sorted(TIMELAPSE_DIR.glob("*.jpg")) if not p.name.startswith("_")]
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
                    old.unlink(missing_ok=True)  # its photo is gone
            shutil.rmtree(tmp, ignore_errors=True)
            # thumbnails are cached by browsers for a year under their name;
            # a new version in the URL is what makes them fetch the new ones
            db.kv_set("thumbs_version", int(time.time()))
            log.info(
                f"rebuilt {len(photos)} thumbnails "
                f"({'flattened' if cfg.get('timelapse_flatten', True) else 'raw'})"
            )
        finally:
            _thumbs_lock.release()

    threading.Thread(target=work, daemon=True, name="thumbs").start()


_rect_cache = {"key": None, "bytes": None}

_rect_lock = threading.Lock()

_crop_cache = {"key": None, "bytes": None}
