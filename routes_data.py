"""Data routes: the AI report, backups, readings history and the host's own stats.

Registered on routes.app when routes.py imports this module.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from flask import Response, jsonify, request

from applog import log
import db
import ai_report
import hoststats

import config
import setups as setups_mod
import water
import monitor
import routes


@routes.app.route("/api/report")
def api_report_get():
    """Latest stored AI report (or nulls if none yet), plus generating flag."""
    try:
        data = json.loads(monitor.AI_REPORT_PATH.read_text())
    except Exception:
        data = {"ok": None, "report": None, "ts": None}
    data["generating"] = monitor.report_state["generating"]
    data["have_key"] = ai_report.have_key()
    return jsonify(data)


@routes.app.route("/api/ai_settings", methods=["POST"])
@routes.require_auth
def update_ai_settings():
    data = request.get_json(silent=True) or {}
    with config.settings_lock:
        if "ai_enabled" in data:
            config.settings["ai_enabled"] = bool(data["ai_enabled"])
        if "ai_notify" in data:
            config.settings["ai_notify"] = bool(data["ai_notify"])
        if "ai_report_hour" in data:
            try:
                config.settings["ai_report_hour"] = max(0, min(23, int(data["ai_report_hour"])))
            except (TypeError, ValueError):
                pass
        if "ai_report_minute" in data:
            try:
                config.settings["ai_report_minute"] = max(0, min(59, int(data["ai_report_minute"])))
            except (TypeError, ValueError):
                pass
        if "ai_notes" in data:
            config.settings["ai_notes"] = str(data["ai_notes"])[:1000]
        config.save_config()
        out = {k: config.settings[k] for k in
               ("ai_enabled", "ai_notify", "ai_report_hour", "ai_report_minute", "ai_notes")}
    return jsonify(ok=True, **out)


@routes.app.route("/api/report", methods=["POST"])
@routes.require_auth
def api_report_run():
    if not ai_report.have_key():
        return jsonify(ok=False, error="no API key set on the controller"), 200
    return jsonify(monitor.run_report("manual"))


def build_backup(include_secrets=False):
    """Build a restorable snapshot in memory and return (bytes, filename).

    The database is copied with SQLite's online backup API rather than by
    reading the file. This runs in WAL mode with the app writing continuously,
    and a plain file copy can capture a torn database that looks fine until the
    day you need it. The copy is then integrity-checked before it ships, since
    a backup nobody verifies is a hope rather than a backup.

    `.env` holds the API key, the plug password and the dashboard password
    hash, so it is excluded unless explicitly requested; config.json also
    carries the password hash and coordinates, which is why the archive is
    only ever served to a logged-in session.
    """
    import io
    import tarfile
    import tempfile

    stamp = datetime.now(ZoneInfo(config.settings.get("timezone", "UTC")))
    name = f"openseedling-backup-{stamp:%Y%m%d-%H%M}.tar.gz"
    notes = [f"OpenSeedling backup taken {stamp:%Y-%m-%d %H:%M %Z}",
             "",
             "growlight.db   readings, hourly rollups, events, planting history",
             "config.json    every setting, calibration and the planting map",
             ".env           pin overrides and secrets (only if requested)",
             "",
             "To restore: stop the service, copy these back into the app",
             "directory, then start it again.",
             ""]

    with tempfile.TemporaryDirectory() as tmp:
        dbcopy = Path(tmp) / "growlight.db"
        src = sqlite3.connect(str(db.DB_PATH))
        try:
            dst = sqlite3.connect(str(dbcopy))
            with dst:
                src.backup(dst)          # consistent against a live writer
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
            dst.close()
        finally:
            src.close()
        if ok != "ok":
            raise RuntimeError(f"database copy failed its integrity check: {ok}")
        notes.append(f"database integrity check: {ok}")

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(dbcopy, arcname="growlight.db")
            if config.CONFIG_PATH.exists():
                tar.add(config.CONFIG_PATH, arcname="config.json")
            envp = Path(__file__).with_name(".env")
            if include_secrets and envp.exists():
                tar.add(envp, arcname=".env")
            info = tarfile.TarInfo("README.txt")
            data = ("\n".join(notes)).encode()
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue(), name


@routes.app.route("/api/backup")
@routes.require_auth
def api_backup():
    """Download a restorable snapshot. Login required: the archive contains the
    password hash and, optionally, the secrets from .env."""
    want_secrets = request.args.get("secrets") == "1"
    try:
        blob, name = build_backup(include_secrets=want_secrets)
    except Exception as e:
        log.error(f"backup failed: {e}")
        return jsonify(ok=False, error=str(e)[:200]), 500
    db.log_event("backup", f"downloaded {name} ({len(blob)/1024:.0f} KB)")
    return Response(blob, mimetype="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "Content-Length": str(len(blob))})


@routes.app.route("/api/purge_series", methods=["POST"])
@routes.require_auth
def api_purge_series():
    """Delete a sensor's logged history.

    Needed when a schema changes: the old `moisture:` per-cell keys are no
    longer written by anything, so they sit on the dashboard forever showing
    whatever they last read. Deliberately explicit rather than automatic --
    this destroys data.
    """
    data = request.get_json(silent=True) or {}
    prefix = str(data.get("prefix", "")).strip()
    if not prefix or len(prefix) < 3:
        return jsonify(ok=False, error="prefix required (3+ chars)"), 200
    try:
        n = db.delete_series_prefix(prefix)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 200
    log.info(f"purged {n} readings with prefix {prefix!r}")
    return jsonify(ok=True, deleted=n, prefix=prefix)


@routes.app.route("/api/series")
def series():
    sensor = request.args.get("sensor", "")
    try:
        hours = max(1, min(24 * 90, int(request.args.get("hours", 168))))
    except ValueError:
        hours = 168
    if not sensor:
        return jsonify(error="sensor required"), 400
    return jsonify(sensor=sensor, hours=hours, points=db.series(sensor, hours))


@routes.app.route("/api/series_all")
def series_all():
    """Every logged sensor's history in one request, so the chart grid doesn't
    fire a request per card. Probe voltages are temperature-compensated here,
    same as the live readings."""
    try:
        hours = max(1, min(24 * 90, int(request.args.get("hours", 168))))
    except ValueError:
        hours = 168
    with config.settings_lock:
        pcal = dict(config.settings.get("probe_cal") or {})
    snap = db.latest()
    stf = water.latest_soil_temp_f(snap)
    out = {}
    with config.settings_lock:
        camera_on = bool(config.settings.get("camera_enabled"))
        _cam_t = set(setups_mod.camera_trays(dict(config.settings)))
    for k in snap.keys():
        if k.startswith("canopy:") and k[7:] not in _cam_t:
            continue                      # a tray the camera no longer watches
        if (k.startswith("float:") or k.startswith("reservoir:")):
            continue                      # binary states aren't charted
        if (k.startswith("dry:") or k.startswith("growth")
                or k.startswith("moisture:")):
            continue                      # retired per-cell camera series
        if not camera_on and k.startswith("canopy:"):
            continue                      # camera vision paused; hide its series
        pts = db.series(k, hours)
        if k.startswith("probe:"):
            cal = pcal.get(k[6:]) or {}
            if (cal.get("temp_comp") or {}).get("coeff"):
                soil_at = monitor._soil_f_lookup(snap, hours)
                pts = [[ts, water.compensated_volts(v, cal, soil_at(ts, stf))]
                       for ts, v in pts]
        out[k] = pts
    k = setups_mod.lux_k()
    if k and "lux" in out:
        cf = setups_mod.canopy_factor()      # charted at canopy, matching the chips
        out["ppfd"] = [[ts, round(v * cf / k, 1)] for ts, v in out["lux"]]
    return jsonify(hours=hours, series=out)


@routes.app.route("/api/host")
def api_host():
    """Pi health. Separate from /api/status so the 15s poll stays cheap:
    vcgencmd shells out, and none of this changes fast."""
    return jsonify(hoststats.all_stats())
