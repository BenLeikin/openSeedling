"""The Flask app, sign-in, the pages, the live status and settings.

The other routes live in routes_camera, routes_garden, routes_climate and
routes_data, imported at the bottom.
"""

import json
import secrets
import queue
import threading
import time
from datetime import timedelta
from functools import wraps
from pathlib import Path
from markupsafe import Markup
from flask import Flask, Response, has_request_context, jsonify, render_template, request, session, send_from_directory
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash

from applog import log
import db

import config
import hardware
import light as light_mod
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
        config.atomic_write_text(SECRET_PATH, s)
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
    if not auth_enabled() or session.get("authed"):
        return True
    # The touchscreen on this Pi (scripts/kiosk.sh) connects from 127.0.0.1.
    return bool(config.TRUST_LOCALHOST and has_request_context()
                and request.remote_addr in ("127.0.0.1", "::1")
                and not request.headers.get("X-Forwarded-For"))


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
        if request.method != "GET" and config.config_broken:
            # running on defaults: no password, and nothing it saves would stick
            return jsonify(error=config.config_broken), 503
        if not is_authed():
            return jsonify(error="login required"), 401
        return fn(*a, **k)
    return wrapper


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
    """Static asset version from file mtimes: browsers refetch the scripts
    (static/js/*.js, screen.js) and stylesheets the moment any changes, so a
    deploy can never leave a stale script running against new markup."""
    try:
        st = Path(app.static_folder)
        files = list(st.glob("js/*.js")) + [st / "style.css", st / "screen.js", st / "screen.css"]
        return int(max(f.stat().st_mtime for f in files if f.exists()))
    except Exception:
        return 0


def form_attrs(key):
    """The name, type and range attributes of a Settings number field, from
    config.FORM: the form offers exactly the range the server accepts. A
    temperature shown in the display units gets no min/max (those are F)."""
    s = config.FORM[key]
    attrs = [f'name="{key}"', 'type="number"']
    if s["kind"] in ("int", "float"):
        attrs += [f'min="{s["min"]}"', f'max="{s["max"]}"']
    attrs.append(f'step="{s.get("step", "1" if s["kind"] == "int" else "any")}"')
    return Markup(" ".join(attrs))


@app.route("/")
def index():
    return render_template("index.html", tzs=config.TIMEZONES, v=_asset_ver(),
                           fa=form_attrs, form_spec=config.form_spec())


@app.after_request
def _changed(resp):
    """Any change through the API announces a new status: open tabs and the
    touchscreen update at once, and the shared status build is not reused
    across the change (status.shared_payload keys its cache on this)."""
    if request.method in ("POST", "PUT", "DELETE") and resp.status_code < 500:
        status_mod.publish("change")
    return resp


@app.route("/screen")
def screen():
    """The touchscreen's one-page summary (scripts/kiosk.sh shows it)."""
    return render_template("screen.html", v=_asset_ver())


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
    payload = status_mod.shared_payload(is_authed())
    if payload.get("error") == "warming up":
        return jsonify(payload), 503
    if request.args.get("lite"):
        payload = status_mod.lite(payload)       # the touchscreen summary
    return jsonify(payload)


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
    # the plug does one job: it cannot be the light and the heat mat at once
    with config.settings_lock:
        use = new.get("plug_use", config.settings.get("plug_use", "light"))
        backend = new.get("light_backend", config.settings.get("light_backend"))
        tgt = new.get("heat_target_f", config.settings.get("heat_target_f", 75))
        mx = new.get("heat_max_f", config.settings.get("heat_max_f", 95))
    if use == "heat" and backend == "kasa":
        for k in ("plug_use", "light_backend"):
            if k in new:
                new.pop(k)
                errors[k] = ("the smart plug cannot be the light and the heat mat; "
                             "pick another light backend or give the plug to the light")
    # The mat keeps heating the soil for a while after it switches off, and the
    # probe is read every few minutes, so the soil overshoots the target. A
    # cut-off closer than 3F would trip on that ordinary overshoot, hold the
    # mat off and alert. One message, on the field to change; the target
    # waits with it so the pair is never saved half-changed.
    if ("heat_target_f" in new or "heat_max_f" in new) and float(mx) < float(tgt) + 3:
        new.pop("heat_target_f", None)
        new.pop("heat_max_f", None)
        errors["heat_max_f"] = (f"must be at least {float(tgt) + 3:g}F with the soil target at "
                                f"{float(tgt):g}F (3F above it, room for the soil's overshoot "
                                "after the mat switches off)")
        if "heat_target_f" in data:
            errors["heat_target_f"] = "not saved until the cut-off is at least 3F above it"
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
        if ("video_fps" in new and camera_mod.VIDEO_PATH.exists()
                and db.kv_get("video_fps_rendered") != float(new["video_fps"])):
            # the video file only changes when it is rendered; do it now
            # rather than leave the download at the old speed
            camera_mod.start_render()
        if now_backend != was:
            # dark the abandoned output before the loop starts driving the new one
            light_mod.release_backend(was)
            db.log_event("light", f"backend {was} -> {now_backend}")
        config.wake.set()
    return jsonify(ok=not errors, saved=sorted(new), errors=errors,
                   crop_reset=bool(new) and crop_reset)


# The routes for each area live in their own modules; importing them
# registers their routes on app. Last, because they import this module.
import routes_camera   # noqa: E402,F401
import routes_garden   # noqa: E402,F401
import routes_climate  # noqa: E402,F401
import routes_data     # noqa: E402,F401

