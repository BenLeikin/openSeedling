"""Camera routes: photos, thumbnails and frames, the grid, capture and preview, the timelapse and its video.

Registered on routes.app when routes.py imports this module.
"""

import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from flask import Response, jsonify, request, send_file, send_from_directory

from applog import log
import db

import config
import camera as camera_mod
import routes


@routes.app.route("/photo/latest")
def latest_photo():
    count, latest, _ = camera_mod.photo_inventory()
    if not count:
        return "no photos yet", 404
    resp = send_file(latest, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@routes.app.route("/api/photos")
def photo_list():
    try:
        v = db.kv_get("thumbs_version") or 0
    except Exception:
        v = 0
    return jsonify(names=[p.name for p in sorted(camera_mod.THUMB_DIR.glob("*.jpg"))], v=v)


@routes.app.route("/thumb/<name>")
def thumb(name):
    resp = send_from_directory(camera_mod.THUMB_DIR, name, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@routes.app.route("/frame/<name>")
def frame(name):
    """One stored photo at full resolution, framed like its thumbnail: the
    timelapse player shows this once it stops on a frame."""
    if (Path(name).name != name or not name.endswith(".jpg")
            or name.startswith((".", "_"))):
        return ("no such photo", 404)
    path = camera_mod.TIMELAPSE_DIR / name
    if not path.is_file():
        return ("no such photo", 404)
    with config.settings_lock:
        cfg = dict(config.settings)
    tag = camera_mod.frame_etag(path, cfg)
    if request.if_none_match.contains(tag):
        resp = Response(status=304)     # unchanged: skip the warp entirely
    else:
        try:
            data, raw = camera_mod.frame_view(path, cfg)
        except Exception as e:
            return (f"frame failed: {e}", 500)
        resp = (send_file(raw, mimetype="image/jpeg") if raw is not None
                else Response(data, mimetype="image/jpeg"))
    resp.set_etag(tag)
    # revalidate each time: moving the corners or the crop reframes it
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@routes.app.route("/api/grid", methods=["POST"])
@routes.require_auth
def update_grid():
    data = request.get_json(silent=True) or {}
    try:
        corners = data["corners"]
        if len(corners) != 4:
            raise ValueError
        corners = [[float(x), float(y)] for x, y in corners]
        for x, y in corners:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError
        rows = int(data.get("rows", 4))
        cols = int(data.get("cols", 4))
        if not (1 <= rows <= 12 and 1 <= cols <= 12):
            raise ValueError
        names = {str(k): str(v)[:40] for k, v in (data.get("names") or {}).items()}
        show = bool(data.get("show", True))
        locked = bool(data.get("locked", False))
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Invalid grid data."), 400
    with config.settings_lock:
        cur = config.settings.get("grid") or {}
        cur_locked = bool(cur.get("locked", False))
        # Server-side lock: while locked, the geometry cannot be changed. A
        # stale tab or stray save can't move a locked grid; you must unlock
        # first (which leaves the geometry untouched).
        if cur_locked:
            def _same(a, b):
                try:
                    return all(abs(p[i] - q[i]) < 1e-9
                               for p, q in zip(a, b) for i in (0, 1))
                except Exception:
                    return False
            geom_changed = (not _same(corners, cur.get("corners", [])) or
                            rows != cur.get("rows") or cols != cur.get("cols"))
            if geom_changed:
                return jsonify(error="grid is locked; unlock before editing"), 409
        config.settings["grid"] = {"corners": corners, "rows": rows, "cols": cols,
                            "names": names, "show": show, "locked": locked}
        config.save_config()
    # audit trail so a future revert can be traced to who/when/what
    try:
        db.log_event("grid", f"saved corners[0]={corners[0]} "
                             f"rows={rows} cols={cols} locked={locked}")
    except Exception:
        pass
    log.info(f"grid saved: corners[0]={corners[0]} rows={rows} cols={cols} locked={locked}")
    return jsonify(ok=True)


@routes.app.route("/api/detect_grid", methods=["POST"])
@routes.require_auth
def detect_grid():
    count, latest, _ = camera_mod.photo_inventory()
    if not count or latest is None:
        return jsonify(ok=False, error="No photo to detect from yet."), 200
    helper = Path(__file__).with_name("detect_corners.py")
    if not helper.exists():
        return jsonify(ok=False, error="Detector not installed."), 200
    try:
        r = subprocess.run(camera_mod.oom_first([sys.executable, str(helper), str(latest)]),
                           capture_output=True, timeout=60)
        out = r.stdout.decode(errors="replace").strip()
        return jsonify(json.loads(out) if out else
                       {"ok": False, "error": "Detector returned nothing."}), 200
    except Exception as e:
        return jsonify(ok=False, error=f"Detector error: {e}"), 200


@routes.app.route("/api/reset_timelapse", methods=["POST"])
@routes.require_auth
def api_reset_timelapse():
    """Archive the current timelapse and start a fresh one.

    Photos are MOVED, not deleted: a grow run is not reproducible, so the old
    frames go to timelapse_archive/<timestamp>/ and can be restored or removed
    by hand once you are sure. Thumbnails and the rendered video are rebuilt
    from scratch, and the camera-derived per-cell history is optionally cleared
    since it was measured against the old geometry.
    """
    data = request.get_json(silent=True) or {}
    if not data.get("confirm"):
        count, _, _ = camera_mod.photo_inventory()
        return jsonify(ok=False, needs_confirm=True, photos=count,
                       error=f"{count} photos would be archived"), 200
    stamp = datetime.now(ZoneInfo(config.settings["timezone"])).strftime("%Y%m%d_%H%M%S")
    dest = camera_mod.ARCHIVE_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for ph in sorted(camera_mod.TIMELAPSE_DIR.glob("*.jpg")):
        if ph.name.startswith("_"):
            continue
        try:
            ph.rename(dest / ph.name)
            moved += 1
        except Exception as e:
            log.error(f"archive {ph.name} failed: {e}")
    for t in camera_mod.THUMB_DIR.glob("*.jpg"):
        t.unlink(missing_ok=True)
    if camera_mod.VIDEO_PATH.exists():
        try:
            camera_mod.VIDEO_PATH.rename(dest / camera_mod.VIDEO_PATH.name)
        except Exception:
            camera_mod.VIDEO_PATH.unlink(missing_ok=True)
    cleared = 0
    if data.get("clear_readings"):
        # camera-derived series only: probes, temperature and light stay
        for prefix in ("canopy:", "growth:", "growth_px:", "dry:", "moisture:"):
            try:
                cleared += db.delete_series_prefix(prefix)
            except Exception as e:
                log.error(f"clear {prefix} failed: {e}")
    with camera_mod.render_lock:
        camera_mod.render.update(state="idle", frames=0, msg="", started=None, elapsed=None)
    log.info(f"timelapse reset: {moved} photos archived to {dest}, "
          f"{cleared} readings cleared")
    return jsonify(ok=True, archived=moved, cleared=cleared, path=str(dest))


@routes.app.route("/api/rebuild_thumbs", methods=["POST"])
@routes.require_auth
def api_rebuild_thumbs():
    """Regenerate every thumbnail from its source photo.

    Needed after the grid corners move or flattening is toggled: thumbnails are
    built once at capture time, so existing ones keep whatever geometry was
    current then and the scrubber would show a mix.
    """
    camera_mod.rebuild_thumbs_async()
    return jsonify(ok=True, started=True)


@routes.app.route("/api/render", methods=["POST"])
@routes.require_auth
def api_render():
    count, _, _ = camera_mod.photo_inventory()
    if count < 2:
        return jsonify(error="Need at least 2 photos to render."), 400
    camera_mod.start_render()
    return jsonify(ok=True)


@routes.app.route("/api/capture", methods=["POST"])
@routes.require_auth
def api_capture():
    """Take a photo right now, using the same light-hold and exposure as the
    timelapse so it lines up with the grid and growth analysis."""
    with config.settings_lock:
        if not config.settings.get("camera_enabled"):
            return jsonify(ok=False, error="camera features are disabled in settings"), 200
    if not camera_mod.capture_lock.acquire(blocking=False):
        return jsonify(ok=False, error="A capture is already in progress."), 200
    try:
        with config.settings_lock:
            cfg = dict(config.settings)
        now = datetime.now(ZoneInfo(cfg["timezone"]))
        path = camera_mod.take_photo(cfg, now, manual=True)
    finally:
        camera_mod.capture_lock.release()
    if not path:
        return jsonify(ok=False, error="Capture failed; check the camera and the log."), 200
    # analyze growth in the background so the response returns as soon as the
    # photo is on disk -- but only inside the photoperiod: a night capture is
    # lit differently and would pollute the growth/dryness series
    with config.state_lock:
        on_t, off_t = config.state["on"], config.state["off"]
    if on_t is not None and off_t is not None and on_t <= now <= off_t:
        threading.Thread(target=camera_mod.record_growth, args=(path, cfg, now),
                         daemon=True).start()
    return jsonify(ok=True, photo=path.name)


@routes.app.route("/api/preview", methods=["POST"])
@routes.require_auth
def api_preview():
    """Grab a quick full-frame still for camera alignment. Not saved to the
    timelapse, not logged, and the light is left as-is, so the dashboard can
    poll it as a live-ish viewfinder while positioning the camera."""
    with config.settings_lock:
        if not config.settings.get("camera_enabled"):
            return jsonify(ok=False, error="camera features are disabled in settings"), 200
    if not camera_mod.capture_lock.acquire(blocking=False):
        return jsonify(ok=False, busy=True), 200
    try:
        with config.settings_lock:
            cw = int(config.settings.get("cam_width", 4608))
            ch = int(config.settings.get("cam_height", 2592))
        # half resolution keeps the full field of view but reads out faster
        pw = max(2, (cw // 2) // 2 * 2)
        ph = max(2, (ch // 2) // 2 * 2)
        tmp = camera_mod.PREVIEW_PATH.with_suffix(".tmp.jpg")
        with config.settings_lock:
            backend = config.settings.get("camera_backend", "rpicam")
            cfg_snapshot = dict(config.settings)
        if backend == "usb":
            # The same mode as a real photo, only fewer warmup frames. A UVC
            # camera reads a different part of its sensor in each mode: the
            # old 1280x720 preview showed a wider 16:9 view than the 4:3
            # photos, so what you aligned was not what got captured.
            ok, err = camera_mod._usb_capture(cfg_snapshot, tmp,
                                   int(cfg_snapshot.get("usb_width", 2048)),
                                   int(cfg_snapshot.get("usb_height", 1536)),
                                   warmup=2)
            if not ok:
                camera_mod._camera_fail(err)
                return jsonify(ok=False, error=err), 200
            # rotation only: the corners are dragged on the unflattened scene
            camera_mod._rotate_file(tmp, int(cfg_snapshot.get("cam_rotate", 0)))
        else:
            cmd = ["rpicam-still", "-n", "-o", str(tmp), "-t", "500",
                   "--width", str(pw), "--height", str(ph)]
            r = subprocess.run(camera_mod.oom_first(cmd), capture_output=True, timeout=20)
            if r.returncode != 0:
                camera_mod._camera_fail(camera_mod.killed_msg(r, "the preview")
                                        or r.stderr.decode(errors="replace")[-150:] or "preview failed")
                return jsonify(ok=False,
                               error="camera error; check the log"), 200
        tmp.replace(camera_mod.PREVIEW_PATH)  # atomic, so a half-written frame is never served
        camera_mod._camera_ok()
    except Exception as e:
        camera_mod._camera_fail(str(e))
        return jsonify(ok=False, error=str(e)), 200
    finally:
        camera_mod.capture_lock.release()
    # sharpness rides along so the align view doubles as a focus aid; the
    # number is only comparable between frames of the same scene and light
    return jsonify(ok=True, ts=int(time.time()),
                   sharpness=camera_mod.sharpness_score(camera_mod.PREVIEW_PATH))


@routes.app.route("/api/camera_modes")
@routes.require_auth
def api_camera_modes():
    """The USB camera's MJPEG capture sizes, largest first. The largest is
    normally the whole sensor: the widest view to crop down from."""
    with config.settings_lock:
        dev = config.settings.get("usb_device", "/dev/video0")
    try:
        r = subprocess.run(["v4l2-ctl", "-d", dev, "--list-formats-ext"],
                           capture_output=True, timeout=10)
        modes = camera_mod.parse_mjpeg_modes(r.stdout.decode(errors="replace"))
    except Exception as e:
        return jsonify(ok=False, error=str(e), modes=[])
    return jsonify(ok=bool(modes), modes=[f"{w}x{h}" for w, h in modes],
                   error=None if modes else "no MJPEG modes reported")


@routes.app.route("/preview.jpg")
def preview_img():
    if not camera_mod.PREVIEW_PATH.exists():
        return "no preview yet", 404
    return send_file(camera_mod.PREVIEW_PATH, mimetype="image/jpeg")


@routes.app.route("/video")
def video():
    if not camera_mod.VIDEO_PATH.exists():
        return "no video rendered yet", 404
    return send_file(camera_mod.VIDEO_PATH, mimetype="video/mp4", as_attachment=True,
                     download_name="grow_timelapse.mp4")


@routes.app.route("/api/focus_sweep", methods=["POST"])
@routes.require_auth
def api_focus_sweep():
    """Start (or cancel) a focus sweep on the USB camera."""
    data = request.get_json(silent=True) or {}
    if data.get("cancel"):
        with camera_mod.focus_lock:
            camera_mod.focus_state["cancel"] = True
        return jsonify(ok=True, cancelled=True)
    with config.settings_lock:
        cfg = dict(config.settings)
    if not cfg.get("camera_enabled"):
        return jsonify(ok=False, error="camera features are disabled in settings"), 200
    if cfg.get("camera_backend") != "usb":
        return jsonify(ok=False, error="focus sweep needs the USB camera backend"), 200
    if not Path(cfg.get("usb_device", "/dev/video0")).exists():
        return jsonify(ok=False, error=f"{cfg.get('usb_device')} not present"), 200
    with camera_mod.focus_lock:
        if camera_mod.focus_state["running"]:
            return jsonify(ok=False, error="a focus sweep is already running"), 200
        camera_mod.focus_state.update(running=True, step=0, total=0, best=None,
                           error="", cancel=False)
    threading.Thread(target=camera_mod.run_focus_sweep, daemon=True).start()
    return jsonify(ok=True, started=True, estimate_seconds=90)


@routes.app.route("/api/frame_context")
def frame_context():
    """Sensor readings nearest a timelapse frame's timestamp, so scrubbing the
    player shows what conditions were when the frame was taken. Storage units
    (Celsius); the frontend converts for display."""
    try:
        ts = int(request.args.get("ts", ""))
    except (TypeError, ValueError):
        return jsonify(error="ts required (unix seconds)"), 400
    out = {}
    snap_keys = db.latest()
    # first DS18B20 key, whatever its serial suffix is
    soil_key = next((k for k in snap_keys if k.startswith("temp:soil")), None)
    for key, label in ((soil_key, "soil_c"), ("temp:air", "air_c"),
                       ("humidity", "humidity"), ("lux", "lux")):
        if not key:
            continue
        v = db.reading_near(key, ts, window=1800)
        if v is not None:
            out[label] = round(v, 1)
    return jsonify(ts=ts, readings=out)


@routes.app.route("/photo/cropped.jpg")
def cropped_image():
    """The latest photo cut to the view crop, cached per (photo, crop)."""
    _, latest, _ = camera_mod.photo_inventory()
    if not latest:
        return ("no photo yet", 404)
    roi = camera_mod.crop_box()
    if not roi:
        return ("no crop set", 404)     # the page falls back to the full frame
    key = (str(latest), latest.stat().st_mtime, roi)
    with camera_mod._rect_lock:
        if camera_mod._crop_cache["key"] == key:
            return Response(camera_mod._crop_cache["bytes"], mimetype="image/jpeg",
                            headers={"Cache-Control": "no-store"})
    try:
        r = camera_mod.imgtool(["crop", latest, "-", json.dumps(list(roi)), "--q", 88])
        if r.returncode != 0 or not r.stdout:
            return (f"crop failed: {camera_mod._imgtool_err(r)}", 500)
        data = r.stdout
        with camera_mod._rect_lock:
            camera_mod._crop_cache.update(key=key, bytes=data)
        return Response(data, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        return (f"crop failed: {e}", 500)


@routes.app.route("/rectified.jpg")
def rectified_image():
    """The latest photo flattened through the grid corners. This is what the
    per-cell analysis actually sees, so it is the honest way to check corner
    placement: if the tray edges are not straight here, the corners are wrong.

    Cached per (photo, geometry): the warp is recomputed only when a new photo
    lands or the corners move, not once per open browser tab."""
    _, latest, _ = camera_mod.photo_inventory()
    if not latest:
        return ("no photo yet", 404)
    with config.settings_lock:
        grid = dict(config.settings.get("grid") or {})
    corners = grid.get("corners")
    if not corners or len(corners) != 4:
        # nothing to rectify against; the page falls back to the raw frame
        return ("no grid corners set", 404)
    key = (str(latest), latest.stat().st_mtime,
           json.dumps([corners, grid.get("rows"), grid.get("cols")]))
    with camera_mod._rect_lock:
        if camera_mod._rect_cache["key"] == key:
            return Response(camera_mod._rect_cache["bytes"], mimetype="image/jpeg",
                            headers={"Cache-Control": "no-store"})
    try:
        # photos are stored already rotated
        r = camera_mod.imgtool(["rectify", latest, "-", json.dumps(corners),
                                int(grid.get("cols", 4)), int(grid.get("rows", 4)), "--q", 85])
        if r.returncode != 0 or not r.stdout:
            return (f"rectify failed: {camera_mod._imgtool_err(r)}", 500)
        data = r.stdout
        with camera_mod._rect_lock:
            camera_mod._rect_cache.update(key=key, bytes=data)
        return Response(data, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        return (f"rectify failed: {e}", 500)
