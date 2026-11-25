"""Who may do what: the dashboard password, the touchscreen's local trust, and
the check every route that changes something goes through."""

import hmac
import ipaddress
import threading
import time

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
    return not auth_enabled() or signed_in() or on_screen()


def _password_mark():
    """What a session carries to show which password it signed in with.

    Keyed with the session secret: the cookie is signed, not encrypted, and
    must not give away anything about the stored hash."""
    with config.settings_lock:
        stored = config.settings.get("password_hash", "")
    key = current_app.secret_key
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(key, stored.encode(), "sha256").hexdigest()[:32]


def sign_in():
    session.clear()
    session["pw"] = _password_mark()
    session.permanent = True


def signed_in():
    """The session signed in with the password that is set now. Changing the
    password signs every browser out."""
    mark = session.get("pw")
    return isinstance(mark, str) and hmac.compare_digest(mark, _password_mark())


class LoginThrottle:
    """Back-off for wrong passwords, kept per client address.

    An attempt is charged as a failure before the password is checked, so a
    burst from one address gets one check and the rest are refused without
    one; the right password clears the charge. The wait doubles with each
    failure in a row, from BASE_S up to MAX_S, and runs from the moment the
    check came back wrong: a check takes about a second on a Pi Zero, which
    would otherwise use up the first wait. A client is forgotten after
    FORGET_S without an attempt.

    Refusing costs nothing and holds no server thread, so one client guessing
    does not keep anyone else from signing in. Behind a reverse proxy every
    client has the proxy's address and shares one back-off, unless the proxy
    is named in GROWLIGHT_TRUSTED_PROXY.
    """

    BASE_S = 1.0
    MAX_S = 60.0
    FORGET_S = 900.0
    MAX_CLIENTS = 512

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._clients = {}  # address -> [failures in a row, refused until, last attempt]

    def begin(self, who):
        """Charge an attempt to `who`. Returns 0 to go ahead with the password
        check, or the seconds this client still has to wait."""
        now = self._clock()
        with self._lock:
            c = self._clients.get(who)
            if c and now - c[2] > self.FORGET_S:
                c = None
            if c and now < c[1]:
                return c[1] - now
            if c is None:
                if len(self._clients) >= self.MAX_CLIENTS:
                    del self._clients[min(self._clients, key=lambda k: self._clients[k][2])]
                c = self._clients[who] = [0, 0.0, now]
            c[0] += 1
            c[1] = now + self._wait(c[0])
            c[2] = now
            return 0

    def _wait(self, failures):
        return min(self.MAX_S, self.BASE_S * 2 ** (failures - 1))

    def cancel(self, who):
        """Take back the charge for an attempt whose password was never checked."""
        with self._lock:
            c = self._clients.get(who)
            if c:
                c[0] -= 1
                c[1] = self._clock()
                if c[0] <= 0:
                    del self._clients[who]

    def end(self, who, ok):
        """Record how the check went. Returns the client's failures in a row."""
        with self._lock:
            if ok:
                self._clients.pop(who, None)
                return 0
            c = self._clients.get(who)
            if not c:
                return 1
            c[1] = self._clock() + self._wait(c[0])
            return c[0]

    def clear(self):
        with self._lock:
            self._clients.clear()


logins = LoginThrottle()


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
