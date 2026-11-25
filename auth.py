"""Who may do what: the dashboard password, the touchscreen's local trust, and
the check every route that changes something goes through."""

import ipaddress

from flask import current_app, has_request_context, jsonify, request, session

import config


def auth_enabled():
    with config.settings_lock:
        return bool(config.settings.get("password_hash"))


def on_screen():
    """The request comes from the touchscreen on this Pi: it arrived on the
    screen's own listener (config.SCREEN_PORT, bound to 127.0.0.1) while the
    kiosk's local trust is on.

    The test is the port the server accepted the request on. Nothing sent in
    the request (a Host or forwarding header, or the lack of one) makes a
    request to the main port count.
    """
    return bool(
        config.TRUST_LOCALHOST
        and has_request_context()
        and request.environ.get("SERVER_PORT") == str(config.SCREEN_PORT)
        and request.remote_addr in ("127.0.0.1", "::1")
    )


def is_authed():
    """Signed in, on the Pi's own screen, or no password has been set.

    While config.json cannot be read the app runs on defaults, which have no
    password. That must not open what a password was protecting, so only the
    Pi's own screen counts as signed in until the file is readable again."""
    if config.config_broken:
        return on_screen()
    return not auth_enabled() or bool(session.get("authed")) or on_screen()


def public(fn):
    """Mark a route as open to anyone who can reach the dashboard.

    Every other route needs a login: a route is guarded unless it says
    otherwise, so a new one cannot be left open by forgetting a decorator."""
    fn.is_public = True
    return fn


# routes Flask provides itself, which cannot carry the mark
PUBLIC_ENDPOINTS = {"static"}
MAX_BODY_BYTES = 1024 * 1024  # the largest request the dashboard sends is a few tens of kB
_CHANGING = ("POST", "PUT", "PATCH", "DELETE")


def host_allowed(host):
    """Whether `host`, a request's Host header, is a name this dashboard goes by:
    an IP address, localhost, this machine's hostname (plain or .local), or a
    name listed in GROWLIGHT_ALLOWED_HOSTS."""
    name = host[1 : host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
    name = name.strip().lower().rstrip(".")
    if name in config.ALLOWED_HOSTS:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def check_request():
    """The one gate every request passes before its route runs.

    In order: the body is not oversized; the dashboard is being addressed by
    one of its own names; a request that changes something carries a JSON
    object; and the caller is signed in unless the route is marked public.

    The JSON rule is the CSRF defense. Another site can make a browser send a
    form or text/plain POST here without asking, but not an application/json
    one: that needs a CORS preflight this app never answers. It does not stop
    DNS rebinding, where the attacker's page becomes same-origin with this
    server. A password does (the session cookie belongs to the real name), so
    the Host check applies while there is none.
    """
    # Flask's MAX_CONTENT_LENGTH only acts when a handler reads the body
    if (request.content_length or 0) > MAX_BODY_BYTES:
        return jsonify(error="request too large"), 413
    if not auth_enabled() and not host_allowed(request.host):
        return jsonify(
            error=f"This dashboard has no password, so it answers only to its IP address, "
            f"localhost and its own hostname, not to {request.host!r}. Set a password, or "
            "add the name to GROWLIGHT_ALLOWED_HOSTS in .env."
        ), 403
    view = current_app.view_functions.get(request.endpoint or "")
    if view is None:
        return None  # no such route; Flask answers 404 or 405
    is_public = getattr(view, "is_public", False) or request.endpoint in PUBLIC_ENDPOINTS
    if request.method in _CHANGING:
        if not request.is_json:
            return jsonify(error="Content-Type must be application/json"), 415
        body = request.get_json(silent=True)
        if body is not None and not isinstance(body, dict):
            return jsonify(error="the request body must be a JSON object"), 400
        if config.config_broken and not is_public:
            # running on defaults: nothing it saves would stick
            return jsonify(error=config.config_broken), 503
    if not is_public and not is_authed():
        return jsonify(error="login required"), 401
    return None
