"""Who may do what: the dashboard password, the touchscreen's local trust, and
the check every route that changes something goes through."""

from functools import wraps

from flask import has_request_context, jsonify, request, session

import config


def auth_enabled():
    with config.settings_lock:
        return bool(config.settings.get("password_hash"))


def on_screen():
    """The request comes from the touchscreen on this Pi: it arrived on the
    screen's own listener (config.SCREEN_PORT, bound to 127.0.0.1) while the
    kiosk's local trust is on.

    Which listener took the request is the server's knowledge, not the
    client's. Nothing sent in the request (a Host or forwarding header, or
    the lack of one) makes a request to the main port count.
    """
    return bool(
        config.TRUST_LOCALHOST
        and has_request_context()
        and request.environ.get("SERVER_PORT") == str(config.SCREEN_PORT)
        and request.remote_addr in ("127.0.0.1", "::1")
    )


def is_authed():
    """Signed in, on the Pi's own screen, or no password has been set."""
    return not auth_enabled() or bool(session.get("authed")) or on_screen()


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
