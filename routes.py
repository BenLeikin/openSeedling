"""The Flask app, sign-in, and every route."""

import json
import re
import secrets
import subprocess
import sys
import queue
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo
from flask import Flask, Response, has_request_context, jsonify, render_template, request, session, send_file, send_from_directory
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash

from applog import log
import db
import sensors
import growth as growth_mod
import ai_report
import hoststats

import config
import hardware
import light as light_mod
import setups as setups_mod
import water
import monitor
import camera as camera_mod
import status as status_mod

# ----------------------------- dashboard -----------------------------

app = Flask(__name__)

SECRET_PATH = Path(__file__).with_name(".secret")
def _load_secret():
    try:
        s = SECRET_PATH.read_text().strip()
        if s:
            return s
    except Exception:
        pass
    s = secrets.token_hex(32)
    try:
        SECRET_PATH.write_text(s)
        SECRET_PATH.chmod(0o600)
        log.info(f"new session secret written to {SECRET_PATH}")
    except Exception:
        pass
    return s

app.secret_key = _load_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


class _SessionInterface(SecureCookieSessionInterface):
    """Decide the cookie's Secure flag per request.

    A Secure cookie sent over plain http is dropped by the browser, so a fixed
    True made login silently fail on a LAN install at http://<pi>:5000, and a
    fixed False sent the session in clear behind HTTPS. "auto" marks it Secure
    when the request arrived over HTTPS, directly or via a proxy that sets
    X-Forwarded-Proto. An explicit true/false in config.json still wins."""
    def get_cookie_secure(self, app):
        mode = config.settings.get("cookie_secure", "auto")
        if mode != "auto":
            return bool(mode)
        if not has_request_context():
            return False
        return (request.is_secure or
                request.headers.get("X-Forwarded-Proto", "").lower() == "https")


app.session_interface = _SessionInterface()


def auth_enabled():
    with config.settings_lock:
        return bool(config.settings.get("password_hash"))


def is_authed():
    # If no password is configured, the dashboard is open (legacy behaviour).
    return (not auth_enabled()) or bool(session.get("authed"))


def require_auth(fn):
    """Gate a route on the login, and gate every non-GET route on a JSON body.

    The JSON rule is the CSRF defense. Another site can make a browser send a
    form or text/plain POST here without asking, but not an application/json
    one: that needs a CORS preflight this app never answers. SameSite=Lax alone
    does not cover it, because it lets a sibling subdomain through, and with no
    password set there is no cookie to protect at all."""
    @wraps(fn)
    def wrapper(*a, **k):
        if request.method != "GET" and not request.is_json:
            return jsonify(error="Content-Type must be application/json"), 415
        if not is_authed():
            return jsonify(error="login required"), 401
        return fn(*a, **k)
    return wrapper


@app.route("/api/lightning", methods=["POST"])
@require_auth
def api_lightning():
    """Run a short lightning effect on the grow light. Entirely for fun."""
    data = request.get_json(silent=True) or {}
    if data.get("stop"):
        light_mod._lightning_stop.set()
        return jsonify(ok=True, stopping=True)
    with config.settings_lock:
        cfg = dict(config.settings)
    if not light_mod.lightning_available(cfg):
        return jsonify(ok=False, error="only available on a dimmable fixture "
                                       "wired through the inverted PWM"), 200
    try:
        seconds = max(3, min(light_mod.LIGHTNING_MAX_S, int(data.get("seconds", 20))))
    except (TypeError, ValueError):
        seconds = 20
    style = data.get("style") if data.get("style") in (
        "storm", "strike", "sheet", "flicker") else "storm"
    with light_mod._lightning_lock:
        if light_mod.lightning_state["running"]:
            return jsonify(ok=False, error="already running"), 200
        light_mod._lightning_stop.clear()
        light_mod.lightning_state.update(running=True, until=time.time() + seconds,
                               style=style)
    db.log_event("light", f"lightning: {style} for {seconds}s")
    threading.Thread(target=light_mod._storm, args=(seconds, style), daemon=True,
                     name="lightning").start()
    return jsonify(ok=True, seconds=seconds, style=style)


@app.route("/api/probe_cal", methods=["POST"])
@require_auth
def probe_cal_set():
    """Capture a tray probe's reading as its wet (100%) or dry (0%) anchor.

    Watches the probe for a few seconds rather than taking one instant reading,
    so soil that is still absorbing water is caught before it becomes a bad
    anchor. A suspect capture is refused with the reason; pass force to store
    it regardless.
    """
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", ""))
    point = data.get("point")
    force = bool(data.get("force"))
    if tray not in (str(t) for t in sensors.PROBE_CHANNELS) or point not in ("wet", "dry"):
        return jsonify(ok=False, error="tray must be a wired probe and point wet|dry"), 200

    # Overriding a refusal stores the value that was refused, not a fresh one:
    # re-sampling would take another 15 seconds and could store something other
    # than the number the user just agreed to.
    given = data.get("volts")
    if force and given is not None:
        try:
            live = float(given)
            spread = drift = None
        except (TypeError, ValueError):
            live = None
    else:
        live, spread, drift = sensors.probe_settle(tray, seconds=water.PROBE_SETTLE_S)
        if live is None:
            live, spread = sensors.probe_spread(tray)
            drift = None
    if live is None:
        return jsonify(ok=False, error="no reading from that probe"), 200

    problems, notes = water.probe_cal_check(tray, point, live, drift, spread)
    # force means store it: the checks become advice, not a veto. They still
    # come back in the response so the reason is on record.
    if problems and not force:
        return jsonify(ok=False, tray=tray, point=point, volts=live,
                       spread=spread, drift=drift, problems=problems,
                       notes=notes, can_force=True,
                       error="not stored: " + problems[0])

    with config.settings_lock:
        cal = config.settings.setdefault("probe_cal", {})
        cal.setdefault(tray, {})[point] = live
        config.save_config()
    db.log_event("probe", f"tray {tray} {point} anchor set to {live:.4f}V"
                          + (" (forced)" if problems else ""))
    return jsonify(ok=True, tray=tray, point=point, volts=live, spread=spread,
                   drift=drift, problems=problems, notes=notes,
                   forced=bool(problems))


@app.route("/api/report")
def api_report_get():
    """Latest stored AI report (or nulls if none yet), plus generating flag."""
    try:
        data = json.loads(monitor.AI_REPORT_PATH.read_text())
    except Exception:
        data = {"ok": None, "report": None, "ts": None}
    data["generating"] = monitor.report_state["generating"]
    data["have_key"] = ai_report.have_key()
    return jsonify(data)


@app.route("/api/ai_settings", methods=["POST"])
@require_auth
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


@app.route("/api/report", methods=["POST"])
@require_auth
def api_report_run():
    if not ai_report.have_key():
        return jsonify(ok=False, error="no API key set on the controller"), 200
    return jsonify(monitor.run_report("manual"))


@app.route("/api/float")
def api_float():
    return jsonify(floats={t: sensors.read_float(t) for t in sensors.FLOAT_PINS})


# One password check at a time. Werkzeug's scrypt hash needs about 32 MB per
# check; with waitress's 16 threads, a burst of parallel logins against the
# public URL could ask for 512 MB on a 512 MB board. Serializing also makes
# the delay a real limit on guessing rather than a per-thread pause.
_login_lock = threading.Lock()
_login_fails = {"n": 0}
LOGIN_DELAY_S = 0.5
LOGIN_DELAY_MAX_S = 8.0


@app.route("/api/login", methods=["POST"])
def login():
    with config.settings_lock:
        h = config.settings.get("password_hash", "")
    if not h:
        return jsonify(ok=True, authed=True)  # no password set -> open
    pw = str((request.get_json(silent=True) or {}).get("password", ""))[:256]
    with _login_lock:
        # doubles per consecutive failure, whoever is guessing
        time.sleep(min(LOGIN_DELAY_MAX_S, LOGIN_DELAY_S * 2 ** min(_login_fails["n"], 4)))
        ok = check_password_hash(h, pw)
        _login_fails["n"] = 0 if ok else _login_fails["n"] + 1
    if ok:
        session["authed"] = True
        session.permanent = True
        return jsonify(ok=True, authed=True)
    if _login_fails["n"] in (5, 20, 100):
        log.warning(f"{_login_fails['n']} failed logins in a row")
    return jsonify(ok=False, error="wrong password"), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify(ok=True, authed=False)


def _asset_ver():
    """Static asset version from file mtimes: browsers refetch app.js/style.css
    the moment either changes, so a deploy can never leave a stale script
    running against new markup."""
    try:
        st = Path(app.static_folder)
        return int(max((st / f).stat().st_mtime for f in ("app.js", "style.css")))
    except Exception:
        return 0


@app.route("/")
def index():
    return render_template("index.html", tzs=config.TIMEZONES, v=_asset_ver())


@app.route("/favicon.ico")
def favicon():
    # browsers request this at the site root regardless of the <link> tags
    return send_from_directory(app.static_folder, "favicon.ico",
                               mimetype="image/x-icon")


@app.route("/site.webmanifest")
def webmanifest():
    """Home-screen metadata. Served from a route rather than /static because
    Flask's mimetype guess for .webmanifest is application/octet-stream, which
    some browsers refuse."""
    return Response(json.dumps({
        "name": "OpenSeedling",
        "short_name": "Seedling",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f0f6ea",
        "theme_color": "#f0f6ea",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"},
        ],
    }), mimetype="application/manifest+json")


@app.route("/photo/latest")
def latest_photo():
    count, latest, _ = camera_mod.photo_inventory()
    if not count:
        return "no photos yet", 404
    resp = send_file(latest, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/photos")
def photo_list():
    try:
        v = db.kv_get("thumbs_version") or 0
    except Exception:
        v = 0
    return jsonify(names=[p.name for p in sorted(camera_mod.THUMB_DIR.glob("*.jpg"))], v=v)


@app.route("/thumb/<name>")
def thumb(name):
    resp = send_from_directory(camera_mod.THUMB_DIR, name, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/api/grid", methods=["POST"])
@require_auth
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


@app.route("/api/detect_grid", methods=["POST"])
@require_auth
def detect_grid():
    count, latest, _ = camera_mod.photo_inventory()
    if not count or latest is None:
        return jsonify(ok=False, error="No photo to detect from yet."), 200
    helper = Path(__file__).with_name("detect_corners.py")
    if not helper.exists():
        return jsonify(ok=False, error="Detector not installed."), 200
    try:
        r = subprocess.run([sys.executable, str(helper), str(latest)],
                           capture_output=True, timeout=60)
        out = r.stdout.decode(errors="replace").strip()
        return jsonify(json.loads(out) if out else
                       {"ok": False, "error": "Detector returned nothing."}), 200
    except Exception as e:
        return jsonify(ok=False, error=f"Detector error: {e}"), 200


@app.route("/api/light", methods=["POST"])
@require_auth
def api_light():
    """Manual light hold. 'on' forces manual_bright, 'off' forces dark, 'auto'
    hands control back to the sun schedule. An optional 'brightness' (0-100)
    sets the level the 'on' hold uses."""
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    bright = data.get("brightness")
    if mode is not None and mode not in ("auto", "on", "off"):
        return jsonify(ok=False, error="mode must be auto|on|off"), 200
    if bright is not None:
        try:
            bright = max(0, min(100, int(bright)))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="brightness must be 0-100"), 200
    if mode is None and bright is None:
        return jsonify(ok=False, error="nothing to set"), 200
    with config.settings_lock:
        if mode is not None:
            config.settings["light_override"] = mode
        if bright is not None:
            config.settings["manual_bright"] = bright
        config.save_config()
        cur_mode = config.settings["light_override"]
        cur_bright = config.settings["manual_bright"]
    config.wake.set()          # apply now instead of waiting for the next loop pass
    if mode is not None:
        db.log_event("light", f"override set to {mode}")
    return jsonify(ok=True, mode=cur_mode, brightness=cur_bright)


@app.route("/api/probe_tempcomp", methods=["POST"])
@require_auth
def probe_tempcomp():
    """Estimate (and optionally apply) a tray probe's temperature-drift
    coefficient from logged data. Run it over a warm, no-watering window."""
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", ""))
    if tray not in sensors.PROBE_CHANNELS:
        return jsonify(ok=False,
                       error=f"no probe wired for tray {tray} "
                             f"(probes: {', '.join(sorted(sensors.PROBE_CHANNELS))})"), 200
    try:
        hours = max(6, min(720, int(data.get("hours", 48))))
    except (TypeError, ValueError):
        hours = 48
    res = water.estimate_temp_comp(tray, hours)
    if not res.get("ok"):
        return jsonify(res), 200
    if res["span"] < 5:
        res["warning"] = (f"soil temp only varied {res['span']}F; "
                          "the estimate is weak until it swings more")
    if data.get("apply"):
        with config.settings_lock:
            cal = config.settings.setdefault("probe_cal", {}).setdefault(tray, {})
            cal["temp_comp"] = {"coeff": res["coeff"], "ref_f": water.TEMP_COMP_REF_F}
            config.save_config()
        res["applied"] = True
    return jsonify(res)


@app.route("/api/planting_end", methods=["POST"])
@require_auth
def api_planting_end():
    """Finish a planting: record it to history, then clear the cell.

    Body: {"tray": "1", "cell": "A1", "outcome": "transplanted"|"died"}.
    The cell is emptied so it can be replanted immediately; everything it held
    moves to the plantings table, which is what the germination stats read.
    Clearing without recording would delete a data point from the variety's
    success rate every time you potted something on.
    """
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", ""))
    cell = str(data.get("cell", ""))
    outcome = data.get("outcome")
    if outcome not in ("transplanted", "died"):
        return jsonify(ok=False, error="outcome must be transplanted or died"), 400
    with config.settings_lock:
        trays = config.settings.get("trays") or {}
        t = trays.get(tray)
        v = ((t or {}).get("cells") or {}).get(cell)
        if not v:
            return jsonify(ok=False, error="no such cell"), 404
        rec = dict(v)
    today = datetime.now(ZoneInfo(config.settings.get("timezone", "UTC"))).date().isoformat()
    pid = db.add_planting({"tray": tray, "cell": cell, "ended": today,
                           "outcome": outcome, **rec})
    with config.settings_lock:
        cells = config.settings["trays"][tray].setdefault("cells", {})
        cells.pop(cell, None)
        config.save_config()
    label = (rec.get("seed") or "cell").strip()
    db.log_event("planting", f"{tray}/{cell} {outcome}: {label}")
    return jsonify(ok=True, id=pid, cleared=True)


@app.route("/api/plantings")
def api_plantings():
    """Finished plantings, newest first. Readable without login, like the rest
    of the dashboard."""
    try:
        return jsonify(plantings=db.plantings(limit=500))
    except Exception as e:
        return jsonify(plantings=[], error=str(e)[:120])


@app.route("/api/planting_restore", methods=["POST"])
@require_auth
def api_planting_restore():
    """Put a finished planting back in its cell, for a misclick.

    Refused when the cell has since been replanted: silently overwriting a new
    planting to undo an old mistake would be the worse failure.
    """
    data = request.get_json(silent=True) or {}
    try:
        pid = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="id required"), 400
    row = next((p for p in db.plantings(limit=500) if p["id"] == pid), None)
    if row is None:
        return jsonify(ok=False, error="no such history entry"), 404
    tray, cell = row["tray"], row["cell"]
    with config.settings_lock:
        t = (config.settings.get("trays") or {}).get(tray)
        if not t:
            return jsonify(ok=False, error=f"tray {tray} no longer exists"), 409
        cells = t.setdefault("cells", {})
        if cells.get(cell):
            return jsonify(ok=False,
                           error=f"{cell} has been replanted; clear it first"), 409
        cells[cell] = {k: row.get(k) or "" for k in
                       ("seed", "equipment", "planted", "sprouted", "source", "notes")}
        cells[cell]["count"] = row.get("count") or 0
        cells[cell]["archived"] = ""
        config.save_config()
    db.delete_planting(pid)
    db.log_event("planting", f"{tray}/{cell} restored from history")
    return jsonify(ok=True, tray=tray, cell=cell)


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


@app.route("/api/backup")
@require_auth
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


@app.route("/api/trays", methods=["POST"])
@require_auth
def api_trays():
    """Save what's planted in each cell. Body: {"tray": "1"|"2", "cells":
    {"A1": {"seed": str, "equipment": str, "planted": "YYYY-MM-DD",
            "sprouted": "YYYY-MM-DD", "archived": "YYYY-MM-DD"}, ...}}.
    `sprouted` records emergence (so days-to-germinate is measurable) and
    `archived` marks a cell transplanted out. Empty cells are dropped."""
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", ""))
    cells = data.get("cells")
    label = data.get("label")
    with config.settings_lock:
        known = set(config.settings.get("trays") or {})
    if tray not in known:
        return jsonify(ok=False, error=f"no such tray: {tray}"), 200
    if not isinstance(cells, dict):
        return jsonify(ok=False, error="cells must be an object"), 200
    clean = {}
    for cid, v in cells.items():
        if not isinstance(v, dict) or not re.fullmatch(r"[A-Z]\d{1,2}", str(cid)):
            continue
        seed = str(v.get("seed", "")).strip()[:60]
        equip = str(v.get("equipment", "")).strip()[:60]

        def _date(field):
            d = str(v.get(field, "") or "").strip()[:10]
            return d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else ""

        planted, sprouted, archived = _date("planted"), _date("sprouted"), _date("archived")
        source = str(v.get("source", "")).strip()[:60]
        notes = str(v.get("notes", "")).strip()[:200]
        try:
            count = max(0, min(99, int(v.get("count") or 0)))
        except (TypeError, ValueError):
            count = 0
        # which fields this cell shows; display-only, so an empty equipment
        # row can be hidden on the cells that will never have one
        _fields = ("seed", "equipment", "planted", "sprouted",
                   "source", "count", "notes")
        hide = [f for f in (v.get("hide") or [])
                if f in _fields or (f.startswith("!") and f[1:] in _fields)][:14]
        if (seed or equip or planted or sprouted or archived or hide
                or source or notes or count):
            clean[cid] = {"seed": seed, "equipment": equip, "planted": planted,
                          "sprouted": sprouted, "archived": archived,
                          "source": source, "count": count, "notes": notes,
                          "hide": hide}
    with config.settings_lock:
        trays = config.settings.setdefault("trays", {})
        t = trays.setdefault(tray, {"label": f"Tray {tray}", "rows": 4,
                                    "cols": 3, "cells": {}})
        t["cells"] = clean
        if label is not None:
            t["label"] = str(label).strip()[:40] or f"Tray {tray}"
        config.save_config()
    return jsonify(ok=True, tray=tray, count=len(clean))


MAX_TRAYS, MAX_DIM = 8, 12


def _cell_ids(rows, cols):
    return {f"{chr(65 + c)}{r}" for r in range(1, rows + 1) for c in range(cols)}


@app.route("/api/reset_timelapse", methods=["POST"])
@require_auth
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


@app.route("/api/rebuild_thumbs", methods=["POST"])
@require_auth
def api_rebuild_thumbs():
    """Regenerate every thumbnail from its source photo.

    Needed after the grid corners move or flattening is toggled: thumbnails are
    built once at capture time, so existing ones keep whatever geometry was
    current then and the scrubber would show a mix.
    """
    camera_mod.rebuild_thumbs_async()
    return jsonify(ok=True, started=True)


@app.route("/api/purge_series", methods=["POST"])
@require_auth
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


@app.route("/api/tray_layout", methods=["POST"])
@require_auth
def api_tray_layout():
    """Add, remove, rename or resize a tray.

    Body: {"action": "add"} | {"action": "remove", "tray": id}
        | {"action": "resize", "tray": id, "rows": n, "cols": n, "label": str}

    Resizing keeps every cell that still fits the new grid. Cells that fall
    outside it are reported as `dropped`, and are only discarded when the
    caller passes confirm=true, so a mis-tap cannot silently delete planting
    records.
    """
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "resize"))
    with config.settings_lock:
        trays = config.settings.setdefault("trays", {})

        if action == "add":
            if len(trays) >= MAX_TRAYS:
                return jsonify(ok=False, error=f"at most {MAX_TRAYS} trays"), 200
            nid = next(str(i) for i in range(1, MAX_TRAYS + 2) if str(i) not in trays)
            trays[nid] = {"label": f"Tray {nid}", "rows": 4, "cols": 3, "cells": {}}
            config.save_config()
            return jsonify(ok=True, tray=nid, added=True)

        tray = str(data.get("tray", ""))
        if tray not in trays:
            return jsonify(ok=False, error="no such tray"), 200

        if action == "remove":
            if len(trays) <= 1:
                return jsonify(ok=False, error="keep at least one tray"), 200
            filled = len(trays[tray].get("cells") or {})
            if filled and not data.get("confirm"):
                return jsonify(ok=False, needs_confirm=True, filled=filled,
                               error=f"tray {tray} has {filled} filled cells"), 200
            trays.pop(tray)
            config.save_config()
            return jsonify(ok=True, removed=tray)

        # resize / rename
        t = trays[tray]
        rows = t.get("rows", 4)
        cols = t.get("cols", 3)
        try:
            if "rows" in data:
                rows = max(1, min(MAX_DIM, int(data["rows"])))
            if "cols" in data:
                cols = max(1, min(MAX_DIM, int(data["cols"])))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="rows and cols must be numbers"), 200

        keep = _cell_ids(rows, cols)
        cells = t.get("cells") or {}
        dropped = sorted(c for c in cells if c not in keep)
        if dropped and not data.get("confirm"):
            return jsonify(ok=False, needs_confirm=True, dropped=dropped,
                           error=f"{len(dropped)} filled cells fall outside "
                                 f"a {cols}x{rows} grid"), 200
        t["rows"], t["cols"] = rows, cols
        t["cells"] = {k: v for k, v in cells.items() if k in keep}
        if "label" in data:
            t["label"] = str(data["label"]).strip()[:40] or f"Tray {tray}"
        config.save_config()
    return jsonify(ok=True, tray=tray, rows=rows, cols=cols, dropped=dropped)


@app.route("/api/render", methods=["POST"])
@require_auth
def api_render():
    count, _, _ = camera_mod.photo_inventory()
    if count < 2:
        return jsonify(error="Need at least 2 photos to render."), 400
    camera_mod.start_render()
    return jsonify(ok=True)


@app.route("/api/capture", methods=["POST"])
@require_auth
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


@app.route("/api/preview", methods=["POST"])
@require_auth
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
            r = subprocess.run(cmd, capture_output=True, timeout=20)
            if r.returncode != 0:
                camera_mod._camera_fail(r.stderr.decode(errors="replace")[-150:] or "preview failed")
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


@app.route("/api/camera_modes")
@require_auth
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


@app.route("/preview.jpg")
def preview_img():
    if not camera_mod.PREVIEW_PATH.exists():
        return "no preview yet", 404
    return send_file(camera_mod.PREVIEW_PATH, mimetype="image/jpeg")


@app.route("/video")
def video():
    if not camera_mod.VIDEO_PATH.exists():
        return "no video rendered yet", 404
    return send_file(camera_mod.VIDEO_PATH, mimetype="video/mp4", as_attachment=True,
                     download_name="grow_timelapse.mp4")


@app.route("/api/stream")
def api_stream():
    """Status pushed as it changes, as an EventSource stream.

    The browser reconnects on its own if this drops, and the dashboard keeps a
    slow poll running regardless, so a stream that dies quietly degrades to the
    old behaviour instead of freezing the page.
    """
    authed = is_authed()          # read while the request context still exists
    with status_mod._subs_lock:
        if len(status_mod._subs) >= status_mod.STREAM_MAX:
            return jsonify(error="too many live connections"), 503
        q = queue.Queue(maxsize=status_mod.STREAM_QUEUE)
        status_mod._subs.add(q)

    def gen():
        try:
            yield "retry: 5000\n\n"          # how soon the browser retries
            # a new tab gets a fresh status, not the last push's copy, which
            # may predate things that changed without a push (the clock)
            yield f"event: status\ndata: {json.dumps(status_mod.status_payload(authed))}\n\n"
            while True:
                with status_mod._subs_lock:
                    culled = q not in status_mod._subs
                if culled or hardware.SHUTTING_DOWN.is_set():
                    return      # dropped or shutting down; the browser reconnects
                try:
                    q.get(timeout=status_mod.STREAM_HEARTBEAT)
                    if hardware.SHUTTING_DOWN.is_set():
                        return
                    # coalesce a burst: one render per batch of changes
                    while True:
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            break
                    yield status_mod._status_event(authed)
                except queue.Empty:
                    # a comment keeps the connection warm and, more usefully,
                    # fails here when the peer has gone so the thread is freed
                    yield ": keepalive\n\n"
        finally:
            with status_mod._subs_lock:
                status_mod._subs.discard(q)

    # No "Connection" header: it is hop-by-hop, and PEP 3333 forbids a WSGI
    # application from setting it. The dev server tolerated it; waitress
    # refuses the response outright.
    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",    # nginx would otherwise buffer the stream
    })


@app.route("/api/status")
def status():
    payload = status_mod.status_payload()
    if payload.get("error") == "warming up":
        return jsonify(payload), 503
    return jsonify(payload)


@app.route("/api/pump", methods=["POST"])
@require_auth
def pump_test():
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", "1"))
    if tray not in hardware.PUMP_PINS:
        return jsonify(ok=False,
                       error=f"no pump wired for tray {tray} "
                             f"(pumps: {', '.join(sorted(hardware.PUMP_PINS))})"), 200
    if tray not in hardware._pumps:
        return jsonify(ok=False, error=f"pump {tray} hardware not available"), 200
    try:
        secs = float(data.get("seconds", 3))
    except (TypeError, ValueError):
        secs = 3.0
    force = bool(data.get("force", False))
    if data.get("until_full"):
        threading.Thread(target=lambda: water.run_pump_until_full(tray, "fill", force),
                         daemon=True).start()
        return jsonify(ok=True, started=True, mode="fill", tray=tray)
    threading.Thread(target=lambda: water.run_pump(tray, secs, "manual", force),
                     daemon=True).start()
    return jsonify(ok=True, started=True, mode="timed", tray=tray)


@app.route("/api/series")
def series():
    sensor = request.args.get("sensor", "")
    try:
        hours = max(1, min(24 * 90, int(request.args.get("hours", 168))))
    except ValueError:
        hours = 168
    if not sensor:
        return jsonify(error="sensor required"), 400
    return jsonify(sensor=sensor, hours=hours, points=db.series(sensor, hours))


@app.route("/api/focus_sweep", methods=["POST"])
@require_auth
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


@app.route("/api/light_sweep", methods=["POST"])
@require_auth
def api_light_sweep():
    """Start (or cancel) a light response sweep."""
    data = request.get_json(silent=True) or {}
    if data.get("cancel"):
        with light_mod.sweep_lock:
            light_mod.sweep_state["cancel"] = True     # the worker restores the light
        return jsonify(ok=True, cancelled=True)
    target = "second" if data.get("light") == "second" else "main"
    if target == "second" and light_mod.light2_fixture() is None:
        return jsonify(ok=False, error="the second light is not available "
                                       "(turned off, or no second PWM channel)"), 200
    key = light_mod.lux_key_for_light(target)
    if not key or sensors.read_all().get(key) is None:
        return jsonify(ok=False, error=f"no light sensor reading for the "
                                       f"{setups_mod.light_label(target)}; assign one in "
                                       "Settings, Setups"), 200
    try:
        step = max(1, min(25, int(data.get("step", 5))))
        settle = max(0.5, min(10.0, float(data.get("settle", 2.0))))
    except (TypeError, ValueError):
        step, settle = 5, 2.0
    linearize = bool(data.get("linearize"))
    if linearize:
        # a knee at 35% and a cutoff at 4% both vanish between 5% steps; the
        # calibration needs every percent to find them
        step = 1
        settle = max(settle, 1.5)
    with light_mod.sweep_lock:
        if light_mod.sweep_state["running"]:
            return jsonify(ok=False, error="a sweep is already running"), 200
        light_mod.sweep_state.update(running=True, pct=0, error="", cancel=False,
                           started=time.time(), target=target, l2_raw=0.0)
    threading.Thread(target=light_mod.run_light_sweep, args=(step, settle, linearize, target),
                     daemon=True).start()
    est = int((101 / step + 1) * (settle + 0.3))
    return jsonify(ok=True, started=True, estimate_seconds=est)


@app.route("/api/frame_context")
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


@app.route("/api/plug_discover", methods=["POST"])
@require_auth
def api_plug_discover():
    """Find TP-Link plugs on the local network.

    Broadcast discovery, so it only sees devices on the same subnet as the Pi.
    Credentials are optional: without them, newer KLAP devices still answer
    discovery with their model and address, they just report that they could
    not be authenticated, which is exactly what the user needs to know before
    typing a password.
    """
    data = request.get_json(silent=True) or {}
    user = str(data.get("user") or "").strip()
    password = str(data.get("pass") or "")
    try:
        timeout = max(2, min(15, int(data.get("timeout", 6))))
    except (TypeError, ValueError):
        timeout = 6

    async def scan():
        from kasa import Discover, Credentials
        kw = {"discovery_timeout": timeout}
        if user or password:
            kw["credentials"] = Credentials(user, password)
        found = await Discover.discover(**kw)
        out = []
        for host, dev in (found or {}).items():
            entry = {"host": host, "alias": "", "model": "", "on": None,
                     "needs_auth": False, "error": ""}
            try:
                await dev.update()
                entry["alias"] = getattr(dev, "alias", "") or ""
                entry["model"] = getattr(dev, "model", "") or ""
                entry["on"] = bool(getattr(dev, "is_on", False))
            except Exception as e:
                entry["needs_auth"] = "auth" in str(e).lower()
                entry["error"] = str(e)[:120]
                entry["model"] = getattr(dev, "model", "") or ""
            out.append(entry)
        return sorted(out, key=lambda d: d["host"])

    try:
        devices = light_mod._kasa_run(scan(), timeout=timeout + 10)
    except ImportError:
        return jsonify(ok=False, error="python-kasa is not installed"), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200
    return jsonify(ok=True, devices=devices)


@app.route("/api/plug_test", methods=["POST"])
@require_auth
def api_plug_test():
    """Connect to a plug and report what it is, without saving anything.

    Takes the host and credentials from the body so a plug can be tried before
    it is committed to settings; falls back to whatever is configured, which
    makes this double as a health check for the current plug.
    """
    data = request.get_json(silent=True) or {}
    host = str(data.get("host") or "").strip()
    user = str(data.get("user") or "").strip()
    password = str(data.get("pass") or "")
    if not host:
        host, user, password = light_mod.kasa_conf()
    if not host:
        return jsonify(ok=False, error="no plug address to test"), 200

    async def probe():
        dev = await light_mod._kasa_connect(host, user, password)
        await dev.update()
        return {"alias": getattr(dev, "alias", "") or "",
                "model": getattr(dev, "model", "") or "",
                "on": bool(getattr(dev, "is_on", False))}

    try:
        info = light_mod._kasa_run(probe(), timeout=20)
    except ImportError:
        return jsonify(ok=False, error="python-kasa is not installed"), 200
    except Exception as e:
        msg = str(e)[:200]
        hint = ""
        if "credential" in msg.lower() or "auth" in msg.lower():
            hint = ("This plug wants TP-Link account credentials. If they are "
                    "correct and it still fails, remove the plug in the Kasa "
                    "app and add it again: changing the account password "
                    "leaves the device holding the old one.")
        return jsonify(ok=False, error=msg, hint=hint), 200
    return jsonify(ok=True, **info)


@app.route("/api/auto_water", methods=["POST"])
@require_auth
def api_auto_water():
    """Arm or disarm autonomous watering.

    Arming is refused while any tray has a blocker, because the trigger is a
    probe reading and an untrustworthy probe means a flood or a drought.
    Disarming is always allowed and never questioned.
    """
    data = request.get_json(silent=True) or {}
    want = bool(data.get("enabled"))
    # which trays: the ones asked for (a setup's trays), else every pump tray
    asked = data.get("trays")
    trays_ = sorted({str(t) for t in asked} & set(hardware.PUMP_PINS)) if isinstance(asked, list) \
        else sorted(hardware.PUMP_PINS)
    if not trays_:
        return jsonify(ok=False, error="none of these trays has a pump"), 200
    with config.settings_lock:
        cur = set(water.armed_trays(config.settings))
    if not want:
        left = sorted(cur - set(trays_))
        with config.settings_lock:
            config.settings["auto_water_trays"], config.settings["auto_water"] = left, bool(left)
            config.save_config()
        db.log_event("auto_water", f"disarmed tray {', '.join(trays_)}")
        return jsonify(ok=True, enabled=False, armed=left)
    blockers = {t: w for t, w in water.auto_water_blockers().items() if t in trays_}
    if blockers:
        return jsonify(ok=False, enabled=False, blockers=blockers,
                       error="Auto-watering needs a calibrated probe and a "
                             "float switch on every tray it waters."), 200
    armed = sorted(cur | set(trays_))
    with config.settings_lock:
        config.settings["auto_water_trays"], config.settings["auto_water"] = armed, True
        config.save_config()
    db.log_event("auto_water", f"armed tray {', '.join(trays_)}")
    return jsonify(ok=True, enabled=True, armed=armed)


@app.route("/api/fan", methods=["POST"])
@require_auth
def api_fan():
    """Set the fan mode: auto (schedule + humidity), on, or off."""
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).strip()
    if mode and mode not in ("auto", "on", "off"):
        return jsonify(ok=False, error="mode must be auto, on or off"), 200
    with config.settings_lock:
        if mode:
            config.settings["fan_mode"] = mode
        if "speed" in data:
            try:
                config.settings["fan_speed"] = max(0, min(100, int(float(data["speed"]))))
            except (TypeError, ValueError):
                pass
        if "auto_speed" in data:
            try:
                config.settings["fan_auto_speed"] = max(0, min(100, int(float(data["auto_speed"]))))
            except (TypeError, ValueError):
                pass
        mode = config.settings.get("fan_mode", "auto")
        config.save_config()
    config.wake.set()                      # apply on the next loop pass immediately
    return jsonify(ok=True, mode=mode)


@app.route("/photo/cropped.jpg")
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
        import cv2
        img = cv2.imread(str(latest))
        if img is None:
            return ("could not read the photo", 500)
        ok, buf = cv2.imencode(".jpg", camera_mod.crop_array(img, roi),
                               [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            return ("encode failed", 500)
        data = buf.tobytes()
        with camera_mod._rect_lock:
            camera_mod._crop_cache.update(key=key, bytes=data)
        return Response(data, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        return (f"crop failed: {e}", 500)


@app.route("/rectified.jpg")
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
        import cv2
        img = cv2.imread(str(latest))   # photos are stored already rotated
        if img is None:
            return ("could not read the photo", 500)
        out = growth_mod.rectify(img, corners,
                                 cols=int(grid.get("cols", 4)),
                                 rows=int(grid.get("rows", 4)))
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return ("encode failed", 500)
        data = buf.tobytes()
        with camera_mod._rect_lock:
            camera_mod._rect_cache.update(key=key, bytes=data)
        return Response(data, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        return (f"rectify failed: {e}", 500)


@app.route("/api/schedule", methods=["POST"])
@require_auth
def api_schedule():
    """Update just the schedule window. Separate from /api/settings so the
    chart's drag handles can commit a change without resubmitting every
    unrelated setting. Only meaningful in fixed and duration modes."""
    data = request.get_json(silent=True) or {}
    with config.settings_lock:
        mode = config.settings.get("schedule_mode", "solar")
        if mode == "solar":
            return jsonify(ok=False, error="switch to fixed or duration mode "
                                           "to drag the schedule"), 200
        changed = {}
        for k in ("fixed_on", "fixed_off", "duration_end"):
            if k in data:
                v = str(data[k] or "").strip()
                if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", v):
                    config.settings[k] = v
                    changed[k] = v
        if "duration_hours" in data:
            try:
                h = max(0.0, min(24.0, float(data["duration_hours"])))
                config.settings["duration_hours"] = round(h, 2)
                changed["duration_hours"] = config.settings["duration_hours"]
            except (TypeError, ValueError):
                pass
        if changed:
            config.save_config()
    if changed:
        config.wake.set()                      # recompute the window immediately
        log.info(f"schedule updated from chart: {changed}")
    return jsonify(ok=True, changed=changed, mode=mode)


@app.route("/api/host")
def api_host():
    """Pi health. Separate from /api/status so the 15s poll stays cheap:
    vcgencmd shells out, and none of this changes fast."""
    return jsonify(hoststats.all_stats())


@app.route("/api/series_all")
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


@app.route("/api/settings", methods=["POST"])
@require_auth
def update_settings():
    """Validate and apply every recognized field in the body.

    Partial bodies are fine, and one bad field no longer discards the rest:
    valid fields are saved, invalid ones come back in `errors` by name so the
    form can say exactly what was rejected. Unknown keys are ignored."""
    data = request.get_json(silent=True) or {}
    new, errors = {}, {}
    for k, v in data.items():
        fn = config.SETTINGS_VALIDATORS.get(k)
        if fn is None:
            continue
        try:
            new[k] = fn(v)
        except ValueError as e:
            errors[k] = str(e) or "invalid"
    if "dli_target_low" in new or "dli_target_high" in new:
        with config.settings_lock:
            lo = new.get("dli_target_low", config.settings.get("dli_target_low"))
            hi = new.get("dli_target_high", config.settings.get("dli_target_high"))
        if lo is not None and hi is not None and lo >= hi:
            for k in ("dli_target_low", "dli_target_high"):
                if k in new:
                    new.pop(k)
                    errors[k] = "the DLI target low must be below the high"
    if new:
        if any(k.startswith("kasa_") for k in new):
            light_mod._kasa_dev = None      # reconnect with the new address or credentials
        with config.settings_lock:
            was = light_mod.light_backend(config.settings)
            flat_was = config.settings.get("timelapse_flatten", True)
            roi_was = config.settings.get("roi", "")
            size_was = (config.settings.get("usb_width"), config.settings.get("usb_height"))
            config.settings.update(new)
            crop_reset = False
            # (the settings form resubmits the crop field unchanged, so "the
            # crop was not edited in this save" is the test, not "absent")
            if (roi_was and new.get("roi", roi_was) == roi_was and
                    (config.settings.get("usb_width"), config.settings.get("usb_height")) != size_was):
                # the crop was drawn on the old mode's view, which a UVC camera
                # frames differently; keeping it would cut the wrong area
                config.settings["roi"] = ""
                crop_reset = True
                log.info("capture size changed: crop reset to full frame")
            now_backend = light_mod.light_backend(config.settings)
            flat_changed = (config.settings.get("timelapse_flatten", True) != flat_was
                            or config.settings.get("roi", "") != roi_was)
            config.save_config()
        if flat_changed:
            # thumbnails are built once per photo; without this the scrubber
            # would keep showing the old geometry
            camera_mod.rebuild_thumbs_async()
        if now_backend != was:
            # dark the abandoned output before the loop starts driving the new one
            light_mod.release_backend(was)
            db.log_event("light", f"backend {was} -> {now_backend}")
        config.wake.set()
    return jsonify(ok=not errors, saved=sorted(new), errors=errors,
                   crop_reset=bool(new) and crop_reset)
