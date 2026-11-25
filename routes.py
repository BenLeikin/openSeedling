"""The Flask app, sign-in, the pages, the live status and settings.

The other routes are blueprints in routes_camera, routes_garden,
routes_climate and routes_data, which growlight.py registers.
"""

import json
import secrets
import queue
import threading
from datetime import timedelta
from pathlib import Path
from markupsafe import Markup
from flask import (
    Flask,
    Response,
    has_request_context,
    jsonify,
    render_template,
    request,
    session,
    send_from_directory,
)
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash

from applog import log
import auth
import db
import events

import config
import hardware
import light as light_mod
import camera as camera_mod
import photos as photos_mod
import video as video_mod
import status as status_mod

app = Flask(__name__)

# The largest request the dashboard sends is the settings form with every
# tray's cells, a few tens of kB. Anything past this is refused with 413
# before it is read into memory.
app.config["MAX_CONTENT_LENGTH"] = auth.MAX_BODY_BYTES
app.before_request(auth.check_request)


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
    True breaks login on a LAN install at http://<pi>:5000, and a fixed False
    sends the session in clear behind HTTPS. "auto" marks it Secure when the
    request arrived over HTTPS: the reverse proxy named in
    GROWLIGHT_TRUSTED_PROXY said so in X-Forwarded-Proto. The header counts
    from that proxy only; the web server drops it from anyone else. An
    explicit true/false in config.json wins."""

    def get_cookie_secure(self, app):
        mode = config.settings.get("cookie_secure", "auto")
        if mode != "auto":
            return bool(mode)
        return has_request_context() and request.is_secure


app.session_interface = _SessionInterface()


# One password check at a time. Werkzeug's scrypt hash needs about 32 MB per
# check; with waitress's 16 threads, a burst of parallel logins against the
# public URL could ask for 512 MB on a 512 MB board.
_hash_lock = threading.Lock()
HASH_WAIT_S = 3.0  # how long a login waits for another one's check before it is refused


def _try_later(seconds):
    seconds = max(1, int(seconds + 0.999))
    resp = jsonify(ok=False, error=f"too many attempts; try again in {seconds} s")
    resp.headers["Retry-After"] = str(seconds)
    return resp, 429


@app.route("/api/login", methods=["POST"])
@auth.public
def login():
    with config.settings_lock:
        h = config.settings.get("password_hash", "")
    if not h:
        return jsonify(ok=True, authed=True)  # no password set -> open
    if config.login_https_only() and not request.is_secure:
        return jsonify(ok=False, error="sign in at the https address; this one is read-only"), 403
    pw = str((request.get_json(silent=True) or {}).get("password", ""))[:256]
    who = request.remote_addr or "?"
    wait = auth.logins.begin(who)
    if wait:
        return _try_later(wait)
    if not _hash_lock.acquire(timeout=HASH_WAIT_S):
        auth.logins.cancel(who)
        return _try_later(1)
    try:
        ok = check_password_hash(h, pw)
    finally:
        _hash_lock.release()
    fails = auth.logins.end(who, ok)
    if ok:
        auth.sign_in()
        return jsonify(ok=True, authed=True)
    if fails in (5, 20, 100):
        log.warning(f"{fails} failed logins in a row from {who}")
    return jsonify(ok=False, error="wrong password"), 401


@app.route("/api/logout", methods=["POST"])
@auth.public
def logout():
    session.clear()
    return jsonify(ok=True, authed=False)


def _asset_ver():
    """Static asset version: the newest mtime among the scripts
    (static/js/*.js, screen.js) and stylesheets, so browsers refetch them
    when any changes."""
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
@auth.public
def index():
    return render_template(
        "index.html",
        tzs=config.TIMEZONES,
        v=_asset_ver(),
        fa=form_attrs,
        form_spec=config.form_spec(),
        onscreen=auth.on_screen(),
        touch_cal=_touch_cal(),
    )


@app.after_request
def _revalidate_scripts(resp):
    """The dashboard's modules import each other by plain path (no ?v=), so
    the browser is told to revalidate each script on every load (a 304 when
    it is current)."""
    if request.path.startswith("/static/js/") or request.path.startswith("/static/screen"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# What every page and reply carries. The policy is the one the pages are
# written to: each script and style sheet is a file of the dashboard's own,
# nothing is loaded from another site, and no other site may frame it.
SECURITY_HEADERS = {
    "Content-Security-Policy": "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",  # style="" attributes in the pages
            "img-src 'self' data: blob:",
            "font-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ]
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


@app.after_request
def _security_headers(resp):
    for name, value in SECURITY_HEADERS.items():
        resp.headers.setdefault(name, value)
    return resp


# posts that report rather than change anything (no new status to announce)
NOT_CHANGES = {"/api/screen_event"}


@app.after_request
def _changed(resp):
    """Any change through the API announces a new status: open tabs and the
    touchscreen update at once, and the shared status build is not reused
    across the change (status.shared_payload keys its cache on this)."""
    if (
        request.method in ("POST", "PUT", "DELETE")
        and resp.status_code < 400
        and request.path not in NOT_CHANGES
    ):
        events.publish("change")
    if request.path.startswith("/api/"):
        # after the publish above, so the reply to a change names the revision
        # that includes it
        resp.headers["X-Status-Rev"] = status_mod.revision()
    return resp


@app.route("/screen")
@auth.public
def screen():
    """The touchscreen's one-page summary (scripts/kiosk.sh shows it)."""
    return render_template(
        "screen.html", v=_asset_ver(), onscreen=auth.on_screen(), touch_cal=_touch_cal()
    )


def _touch_cal():
    """The touchscreen's tap correction, for screen_touch.js (pages served to
    the Pi's own screen only)."""
    with config.settings_lock:
        return dict(config.settings.get("screen_touch_cal") or {})


@app.route("/favicon.ico")
@auth.public
def favicon():
    # browsers request this at the site root regardless of the <link> tags
    return send_from_directory(app.static_folder, "favicon.ico", mimetype="image/x-icon")


@app.route("/site.webmanifest")
@auth.public
def webmanifest():
    """Home-screen metadata. Served from a route rather than /static because
    Flask's mimetype guess for .webmanifest is application/octet-stream, which
    some browsers refuse."""
    return Response(
        json.dumps(
            {
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
            }
        ),
        mimetype="application/manifest+json",
    )


@app.route("/api/stream")
@auth.public
def api_stream():
    """Status pushed as it changes, as an EventSource stream.

    The browser reconnects on its own if this drops, and the dashboard keeps a
    slow poll running, so a stream that dies quietly does not freeze the page.
    Returns 503 when STREAM_MAX streams are already open, or when a signed-out
    viewer asks and STREAM_VISITORS_MAX of them are.
    """
    authed = auth.is_authed()  # read while the request context still exists
    with status_mod._subs_lock:
        visitors = len(status_mod._subs & status_mod._visitor_subs)
        if len(status_mod._subs) >= status_mod.STREAM_MAX or (
            not authed and visitors >= status_mod.STREAM_VISITORS_MAX
        ):
            return jsonify(error="too many live connections"), 503
        q = queue.Queue(maxsize=status_mod.STREAM_QUEUE)
        status_mod._subs.add(q)
        if not authed:
            status_mod._visitor_subs.add(q)

    def gen():
        try:
            yield "retry: 5000\n\n"  # how soon the browser retries
            # a new tab gets a fresh status, not the last push's copy, which
            # may predate things that changed without a push (the clock)
            yield f"event: status\ndata: {json.dumps(status_mod.status_payload(authed))}\n\n"
            while True:
                with status_mod._subs_lock:
                    culled = q not in status_mod._subs
                if culled or hardware.SHUTTING_DOWN.is_set():
                    return  # dropped or shutting down; the browser reconnects
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
                    # an SSE comment keeps the connection warm, and writing it
                    # fails when the peer has gone, which frees the thread
                    yield ": keepalive\n\n"
        finally:
            with status_mod._subs_lock:
                status_mod._subs.discard(q)
                status_mod._visitor_subs.discard(q)

    # No "Connection" header: it is hop-by-hop, PEP 3333 forbids a WSGI
    # application from setting it, and waitress refuses the response.
    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # nginx would otherwise buffer the stream
        },
    )


@app.route("/api/status")
@auth.public
def status():
    payload = status_mod.shared_payload(auth.is_authed())
    if payload.get("error") == "warming up":
        return jsonify(payload), 503
    if request.args.get("lite"):
        payload = status_mod.lite(payload)  # the touchscreen summary
    return jsonify(payload)


@app.route("/api/settings", methods=["POST"])
def update_settings():
    """Validate and apply every recognized field in the body.

    Partial bodies are fine. Valid fields are saved; invalid ones come back in
    `errors` by name and do not block the rest. Unknown keys are ignored."""
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
                errors[k] = (
                    "the smart plug cannot be the light and the heat mat; "
                    "pick another light backend or give the plug to the light"
                )
    # The mat keeps heating the soil for a while after it switches off, and the
    # probe is read every few minutes, so the soil overshoots the target. A
    # cut-off closer than 3F would trip on that ordinary overshoot, hold the
    # mat off and alert. One message, on the field to change; the target
    # waits with it so the pair is never saved half-changed.
    if ("heat_target_f" in new or "heat_max_f" in new) and float(mx) < float(tgt) + 3:
        new.pop("heat_target_f", None)
        new.pop("heat_max_f", None)
        errors["heat_max_f"] = (
            f"must be at least {float(tgt) + 3:g}F with the soil target at "
            f"{float(tgt):g}F (3F above it, room for the soil's overshoot "
            "after the mat switches off)"
        )
        if "heat_target_f" in data:
            errors["heat_target_f"] = "not saved until the cut-off is at least 3F above it"
    if new:
        if any(k.startswith("kasa_") for k in new):
            light_mod._kasa_dev = None  # reconnect with the new address or credentials
        with config.settings_lock:
            was = light_mod.light_backend(config.settings)
            flat_was = config.settings.get("timelapse_flatten", True)
            roi_was = config.settings.get("roi", "")
            size_was = (config.settings.get("usb_width"), config.settings.get("usb_height"))
            config.settings.update(new)
            crop_reset = False
            # The settings form resubmits the crop field unchanged, so the test
            # is that the crop was not edited in this save, not that it is absent.
            if (
                roi_was
                and new.get("roi", roi_was) == roi_was
                and (config.settings.get("usb_width"), config.settings.get("usb_height"))
                != size_was
            ):
                # the crop was drawn on the old mode's view, which a UVC camera
                # frames differently; keeping it would cut the wrong area
                config.settings["roi"] = ""
                crop_reset = True
                log.info("capture size changed: crop reset to full frame")
            # a camera removed or swapped, or a new size: the saved alignment of
            # the two cameras does not fit, so photos go back to camera 1 alone
            if camera_mod.unstitch(config.settings):
                if config.settings.get("roi"):
                    config.settings["roi"] = ""
                    crop_reset = True
                log.info("camera pair changed: stitching off until the cameras are aligned again")
            now_backend = light_mod.light_backend(config.settings)
            flat_changed = (
                config.settings.get("timelapse_flatten", True) != flat_was
                or config.settings.get("roi", "") != roi_was
            )
            config.save_config()
        if flat_changed:
            # thumbnails are built once per photo; without this the scrubber
            # would keep showing the old geometry
            photos_mod.rebuild_thumbs_async()
        if (
            "video_fps" in new
            and video_mod.VIDEO_PATH.exists()
            and db.kv_get("video_fps_rendered") != float(new["video_fps"])
        ):
            # the video file only changes when it is rendered; do it now
            # rather than leave the download at the old speed
            video_mod.start_render()
        if now_backend != was:
            # dark the abandoned output before the loop starts driving the new one
            light_mod.release_backend(was)
            db.log_event("light", f"backend {was} -> {now_backend}")
        config.wake.set()
    return jsonify(
        ok=not errors, saved=sorted(new), errors=errors, crop_reset=bool(new) and crop_reset
    )
