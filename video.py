"""The timelapse video: rendering the stored photos with ffmpeg."""

import json
import os
import subprocess
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from applog import log
import db
import config

import photos as photos_mod
import tools

# state is idle, running, done or error
render = {
    "state": "idle",
    "msg": "",
    "frames": 0,
    "started": None,
    "elapsed": None,
}

render_lock = threading.Lock()

VIDEO_PATH = photos_mod.TIMELAPSE_DIR / "timelapse.mp4"

# ---- video render -----------------------------------------------------------


def _flatten_frames_to(dest, frames, cfg):
    """Write rectified copies of `frames` into `dest`, numbered in order.

    Returns dest, or None when the grid has no corners or the job fails. The
    video is rendered from these, while the originals on disk stay raw. A
    frame that cannot be rectified is copied through unchanged."""
    grid = cfg.get("grid") or {}
    corners = grid.get("corners")
    if not corners or len(corners) != 4:
        return None
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.jpg"):
        old.unlink()
    listfile = dest / "frames.txt"
    listfile.write_text("\n".join(str(f) for f in frames) + "\n")
    try:
        r = tools.imgtool(
            [
                "flatten",
                dest,
                listfile,
                json.dumps(corners),
                int(grid.get("cols", 4)),
                int(grid.get("rows", 4)),
                "--q",
                88,
            ],
            timeout=4 * 3600,
        )
    finally:
        listfile.unlink(missing_ok=True)
    if r.returncode != 0:
        log.error(f"flattening frames failed ({tools._imgtool_err(r)})")
        return None
    return dest


def render_worker():
    import time as _t

    t0 = _t.monotonic()
    frames = sorted(p for p in photos_mod.TIMELAPSE_DIR.glob("*.jpg") if not p.name.startswith("_"))
    with render_lock:
        render.update(
            state="running",
            frames=len(frames),
            started=datetime.now(ZoneInfo(config.settings["timezone"])).isoformat(),
            elapsed=None,
            msg=f"Rendering {len(frames)} frames...",
        )
    flat_dir = None
    try:
        with config.settings_lock:
            cfg_r = dict(config.settings)
        if cfg_r.get("timelapse_flatten", True):
            with render_lock:
                render["msg"] = f"Flattening {len(frames)} frames..."
            flat_dir = _flatten_frames_to(photos_mod.TIMELAPSE_DIR / "_flat", frames, cfg_r)
        src_glob = str((flat_dir or photos_mod.TIMELAPSE_DIR) / "*.jpg")
        # the view crop applies to raw frames; flattened ones are already the tray
        roi = None if flat_dir else photos_mod.crop_box(cfg_r)
        # lanczos: a sharper downscale than the default bicubic, same memory
        vf_scale = "scale=1280:-16:flags=lanczos:in_range=full:out_range=tv"
        # One frame size for the whole video, from the newest frame: a run that
        # changed shape (a second camera, a new capture size) is letterboxed
        # instead of failing the encode when the size changes mid-stream.
        srcs = sorted((flat_dir or photos_mod.TIMELAPSE_DIR).glob("*.jpg"))
        size = photos_mod.jpeg_size(srcs[-1]) if srcs else None
        if size:
            fw, fh = size
            if roi:
                fw, fh = fw * roi[2], fh * roi[3]
            oh = max(16, int(round(1280 * fh / fw / 16.0)) * 16)
            vf_scale = (
                f"scale=1280:{oh}:force_original_aspect_ratio=decrease:force_divisible_by=2"
                f":flags=lanczos:in_range=full:out_range=tv,pad=1280:{oh}:(ow-iw)/2:(oh-ih)/2"
            )
        vf = (photos_mod.crop_filter(roi) + "," + vf_scale) if roi else vf_scale
        tmp = photos_mod.TIMELAPSE_DIR / "_render_tmp.mp4"
        # Encode pass, sized for the 512 MB Zero: 1280 wide, ultrafast, single
        # thread, and no +faststart here (its second pass rewrites the whole
        # file in memory). Faststart is added by a stream-copy remux afterwards.
        fps = max(1.0, float(cfg_r.get("video_fps", 8)))
        r = subprocess.run(
            tools.oom_first(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-y",
                    "-framerate",
                    f"{fps:g}",
                    "-pattern_type",
                    "glob",
                    "-i",
                    src_glob,
                    # JPEG stills are full-range (yuvj420p/pc); browsers render that as
                    # black. Remap to limited-range yuv420p and tag it. Height is forced
                    # to a multiple of 16 (-16, not -2): a non-mod16 height makes the
                    # encoder signal a crop that some hardware decoders render as black.
                    "-vf",
                    vf,
                    # crf 20: ultrafast turns off adaptive quantization and
                    # deblocking, and a higher crf smears fine leaf and soil
                    # texture. crf changes file size, not encode time or memory.
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "20",
                    "-threads",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    "-color_range",
                    "tv",
                    # Fully specify the colour metadata (BT.601, matching the JPEG
                    # source). An unspecified matrix makes some hardware decoders
                    # (VLC's, browsers') render the video as black.
                    "-colorspace",
                    "smpte170m",
                    "-color_primaries",
                    "smpte170m",
                    "-color_trc",
                    "smpte170m",
                    str(tmp),
                ]
            ),
            capture_output=True,
            timeout=3600,
        )
        if r.returncode == 0 and tmp.exists():
            # Faststart as a stream-copy remux: no re-encode, little memory.
            # Written beside the finished video, then swapped in with one
            # rename: a download running during the remux keeps reading the
            # old file.
            part = VIDEO_PATH.with_name("timelapse.part.mp4")
            r2 = subprocess.run(
                tools.oom_first(
                    [
                        "ffmpeg",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(tmp),
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        str(part),
                    ]
                ),
                capture_output=True,
                timeout=600,
            )
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
                render.update(
                    state="done",
                    elapsed=round(dt, 1),
                    msg=f"{len(frames)} frames at {fps:g} fps, {mb:.1f} MB, {dt:.0f}s",
                )
            try:
                db.kv_set("video_fps_rendered", fps)
            except Exception:
                pass
        else:
            err = r.stderr.decode(errors="replace")[-200:]
            with render_lock:
                render.update(state="error", elapsed=round(dt, 1), msg=err or "ffmpeg failed")
    except Exception as e:
        with render_lock:
            render.update(state="error", elapsed=round(_t.monotonic() - t0, 1), msg=str(e))
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
