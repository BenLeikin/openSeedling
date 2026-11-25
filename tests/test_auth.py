"""Tests: signing in, sessions, and what a signed-out viewer may start."""

import threading
import time

import pytest
from werkzeug.security import generate_password_hash

from harness import auth, c, config, photos_mod, routes, set_password, status_mod, tools


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_wrong_passwords_back_off_per_client():
    clock = Clock()
    t = auth.LoginThrottle(clock)
    assert t.begin("a") == 0, "a first attempt goes ahead"
    assert t.end("a", ok=False) == 1
    assert t.begin("a") == pytest.approx(1.0), "the next one waits"
    assert t.begin("b") == 0, "another client is not held up by it"
    t.clear()
    waits = []
    for _ in range(8):
        assert t.begin("a") == 0
        t.end("a", ok=False)
        waits.append(t.begin("a"))
        clock.now += waits[-1]
    assert waits[:4] == pytest.approx([1, 2, 4, 8]), "the wait doubles with each failure"
    assert waits[-1] == pytest.approx(t.MAX_S), "up to a ceiling"

    assert t.begin("a") == 0
    assert t.end("a", ok=True) == 0
    assert t.begin("a") == 0, "the right password clears the count"

    t.clear()
    assert t.begin("a") == 0
    clock.now += 0.8  # the password check, on a Pi Zero
    assert t.begin("a") > 0, "a second attempt during the check is refused"
    t.end("a", ok=False)
    assert t.begin("a") == pytest.approx(1.0), "the wait runs from when the check failed"

    t.clear()
    assert t.begin("a") == 0
    t.cancel("a")
    assert t.begin("a") == 0, "an attempt that was never checked is not charged"

    t.clear()
    t.begin("a")
    t.end("a", ok=False)
    clock.now += t.FORGET_S + 1
    assert t.begin("a") == 0 and t.end("a", ok=False) == 1, "a client is forgotten in time"

    t.clear()
    for i in range(t.MAX_CLIENTS + 50):
        clock.now += 0.001
        t.begin(f"client{i}")
    assert len(t._clients) == t.MAX_CLIENTS, "the table of clients is bounded"


def test_a_guessing_client_is_refused_without_a_password_check():
    set_password(True)
    checks = []
    real_check = routes.check_password_hash
    routes.check_password_hash = lambda h, pw: checks.append(pw) or real_check(h, pw)
    try:
        guesser = routes.app.test_client()
        first = guesser.post(
            "/api/login", json={"password": "wrong"}, environ_base={"REMOTE_ADDR": "10.9.9.9"}
        )
        again = guesser.post(
            "/api/login", json={"password": "pw"}, environ_base={"REMOTE_ADDR": "10.9.9.9"}
        )
        assert first.status_code == 401
        assert again.status_code == 429 and int(again.headers["Retry-After"]) >= 1
        assert "try again" in again.get_json()["error"]
        assert checks == ["wrong"], "a refused attempt costs no password check"

        t0 = time.monotonic()
        owner = routes.app.test_client()
        r = owner.post(
            "/api/login", json={"password": "pw"}, environ_base={"REMOTE_ADDR": "10.1.1.1"}
        )
        assert r.status_code == 200, "someone else signs in while that client is backing off"
        assert time.monotonic() - t0 < 1.0, "and is not made to wait"
    finally:
        routes.check_password_hash = real_check
        set_password(False)


def test_password_checks_run_one_at_a_time():
    set_password(True)
    real_check, real_wait = routes.check_password_hash, routes.HASH_WAIT_S
    active = {"n": 0, "max": 0}
    lk = threading.Lock()

    def tracked(h, pw):
        with lk:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.05)
        try:
            return real_check(h, pw)
        finally:
            with lk:
                active["n"] -= 1

    routes.check_password_hash = tracked
    codes = []

    def attempt(i):
        r = routes.app.test_client().post(
            "/api/login", json={"password": "wrong"}, environ_base={"REMOTE_ADDR": f"10.2.0.{i}"}
        )
        codes.append(r.status_code)

    try:
        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert active["max"] == 1, f"parallel logins are checked one at a time ({active['max']})"
        assert codes == [401] * 6

        # the check is busy for longer than a login will wait: refused, not queued
        routes.HASH_WAIT_S = 0.05
        assert routes._hash_lock.acquire(timeout=1)
        try:
            r = routes.app.test_client().post(
                "/api/login", json={"password": "pw"}, environ_base={"REMOTE_ADDR": "10.3.3.3"}
            )
        finally:
            routes._hash_lock.release()
        assert r.status_code == 429
        r = routes.app.test_client().post(
            "/api/login", json={"password": "pw"}, environ_base={"REMOTE_ADDR": "10.3.3.3"}
        )
        assert r.status_code == 200, "and that refusal is not held against the client"
    finally:
        routes.check_password_hash, routes.HASH_WAIT_S = real_check, real_wait
        set_password(False)


def test_changing_the_password_signs_everyone_out():
    set_password(True)
    try:
        browser = routes.app.test_client()
        assert browser.post("/api/login", json={"password": "pw"}).status_code == 200
        assert browser.get("/api/status").get_json()["authed"]
        assert browser.get("/api/cameras").status_code != 401

        with browser.session_transaction() as s:
            assert "authed" not in s and config.settings["password_hash"] not in str(dict(s)), (
                "the cookie does not carry the hash"
            )

        with config.settings_lock:
            config.settings["password_hash"] = generate_password_hash(
                "another", method="pbkdf2:sha256:1000"
            )
        assert not browser.get("/api/status").get_json()["authed"], "the old session is void"
        assert browser.get("/api/cameras").status_code == 401

        old_style = routes.app.test_client()
        with old_style.session_transaction() as s:
            s["authed"] = True
        assert old_style.get("/api/cameras").status_code == 401, (
            "a cookie that only says 'signed in' is not enough"
        )
    finally:
        set_password(False)


def test_signed_out_viewers_leave_live_streams_for_whoever_signs_in():
    set_password(True)
    opened = []

    def open_stream(client):
        r = client.get("/api/stream", buffered=False)
        if r.status_code == 200:
            it = iter(r.response)
            next(it)
            opened.append(r)
        return r.status_code

    try:
        visitors = [open_stream(routes.app.test_client()) for _ in range(4)]
        assert visitors == [200] * status_mod.STREAM_VISITORS_MAX + [503] * (
            4 - status_mod.STREAM_VISITORS_MAX
        )
        owner = routes.app.test_client()
        owner.post("/api/login", json={"password": "pw"})
        left = status_mod.STREAM_MAX - status_mod.STREAM_VISITORS_MAX
        assert [open_stream(owner) for _ in range(left)] == [200] * left
        assert open_stream(owner) == 503, "the total is still capped"

        set_password(False)  # no password: nobody is a visitor
        for r in opened:
            r.close()
        opened.clear()
        with status_mod._subs_lock:
            assert not status_mod._subs and not status_mod._visitor_subs, (
                "closed streams are let go"
            )
        everyone = routes.app.test_client()
        assert [open_stream(everyone) for _ in range(status_mod.STREAM_MAX)] == (
            [200] * status_mod.STREAM_MAX
        )
    finally:
        for r in opened:
            r.close()
        set_password(False)


def test_full_size_frames_need_a_login():
    (photos_mod.TIMELAPSE_DIR / "20260101_120000.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    with config.settings_lock:
        config.settings.update(timelapse_flatten=False, roi="")
    assert c.get("/frame/20260101_120000.jpg").status_code == 200, "no password: open"
    set_password(True)
    try:
        anon = routes.app.test_client()
        assert anon.get("/frame/20260101_120000.jpg").status_code == 401
        assert anon.get("/photo/latest").status_code == 200, "the photo itself stays public"
    finally:
        set_password(False)


def test_viewers_of_a_new_photo_share_one_image_job():
    photo = photos_mod.TIMELAPSE_DIR / "20260101_120000.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    with config.settings_lock:
        config.settings["roi"] = "0.1,0.1,0.5,0.5"
    jobs = []
    real = tools.imgtool

    def slow(args, *a, **k):
        jobs.append(args[0])
        time.sleep(0.2)
        return tools.Result(0, b"jpeg-bytes", b"", "")

    tools.imgtool = slow
    replies = []
    try:
        threads = [
            threading.Thread(
                target=lambda: replies.append(routes.app.test_client().get("/photo/cropped.jpg"))
            )
            for _ in range(8)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert [r.status_code for r in replies] == [200] * 8
        assert {r.data for r in replies} == {b"jpeg-bytes"}
        assert jobs == ["crop"], f"eight viewers, one crop ({len(jobs)} ran)"

        tools.imgtool = lambda *a, **k: tools.Result(1, b"", b"", "out of memory")
        with config.settings_lock:
            config.settings["roi"] = "0.2,0.2,0.5,0.5"
        r = c.get("/photo/cropped.jpg")
        assert r.status_code == 500 and b"out of memory" in r.data, "a failed job says why"
    finally:
        tools.imgtool = real


def test_only_a_single_address_can_be_the_trusted_proxy():
    was = config.TRUSTED_PROXY
    try:
        config.TRUSTED_PROXY = ""
        assert config.proxy_options() == {}
        for bad in ("*", "10.0.0.0/8", "proxy.lan", "10.0.0.5, 10.0.0.6"):
            config.TRUSTED_PROXY = bad
            assert config.proxy_options() == {}, f"{bad!r} is not one address"
        config.TRUSTED_PROXY = "10.0.0.5"
        opts = config.proxy_options()
        assert opts["trusted_proxy"] == "10.0.0.5" and opts["trusted_proxy_count"] == 1
        assert opts["trusted_proxy_headers"] == {"x-forwarded-for", "x-forwarded-proto"}
    finally:
        config.TRUSTED_PROXY = was


def test_forwarded_headers_count_from_the_trusted_proxy_only():
    """Against the real web server, as a reverse proxy on 127.0.0.1 would call it."""
    import json
    import urllib.error
    import urllib.request

    try:
        from waitress import create_server
    except ImportError:
        pytest.skip("forwarded headers on the real web server (waitress not installed here)")

    def login(port, password, **headers):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/login",
            data=json.dumps({"password": password}).encode(),
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as e:
            return e.code, ""

    def fan(port, **headers):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/fan",
            data=b'{"mode": "auto"}',
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    was = config.TRUSTED_PROXY, config.TRUST_LOCALHOST, config.SCREEN_PORT
    config.TRUSTED_PROXY = "127.0.0.1"
    behind = create_server(routes.app, listen="127.0.0.1:0 127.0.0.1:0", **config.proxy_options())
    direct = create_server(routes.app, listen="127.0.0.1:0")
    port, screen = (p for _, p in behind.effective_listen)
    for server in (behind, direct):
        threading.Thread(target=server.run, daemon=True).start()
    set_password(True)
    try:
        # through the proxy: each browser has its own back-off
        a = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}
        b = {"X-Forwarded-For": "203.0.113.10", "X-Forwarded-Proto": "https"}
        assert login(port, "wrong", **a)[0] == 401
        assert login(port, "pw", **a)[0] == 429, "the same browser has to wait"
        status, cookie = login(port, "pw", **b)
        assert status == 200, "another browser behind the same proxy does not"
        assert "Secure" in cookie, "over HTTPS the session cookie is marked Secure"
        # what the browser itself put in the header is not the address used
        spoof = {"X-Forwarded-For": "198.51.100.1, 203.0.113.9", "X-Forwarded-Proto": "https"}
        assert login(port, "pw", **spoof)[0] == 429, "the proxy's own entry is the one that counts"

        # the screen's listener stays the screen's: a forwarded request is not it
        config.TRUST_LOCALHOST, config.SCREEN_PORT = True, screen
        assert fan(screen) == 200
        assert fan(screen, **{"X-Forwarded-For": "203.0.113.9"}) == 401
        assert fan(screen, **{"X-Forwarded-Proto": "https"}) == 401
        config.TRUST_LOCALHOST = False

        # no trusted proxy: the headers are dropped
        auth.logins.clear()
        direct_port = direct.effective_port
        assert login(direct_port, "wrong", **a)[0] == 401
        assert login(direct_port, "pw", **b)[0] == 429, "a made-up address is not believed"
        auth.logins.clear()
        status, cookie = login(direct_port, "pw", **{"X-Forwarded-Proto": "https"})
        assert status == 200 and "Secure" not in cookie, "nor is a made-up scheme"
    finally:
        config.TRUSTED_PROXY, config.TRUST_LOCALHOST, config.SCREEN_PORT = was
        set_password(False)
        behind.close()
        direct.close()


def _status(client):
    return client.get("/api/status").get_json()


def test_a_signed_out_viewer_gets_only_the_settings_the_page_draws_with():
    with config.settings_lock:
        config.settings.update(
            ai_notes="the tray by the kitchen window",
            latitude=51.5,
            longitude=-0.12,
            kasa_user="me@example.com",
            screen_night="dim",
        )
    everything = _status(c)["settings"]
    assert {"ai_notes", "latitude", "timezone", "usb_device"} <= set(everything), "no password"
    assert "kasa_user" not in everything, "credentials are never in the status"

    set_password(True)
    try:
        seen = set(_status(routes.app.test_client())["settings"])
        extra = {k for k in seen if k not in status_mod.VISITOR_SETTINGS and k[:7] != "screen_"}
        assert not extra, f"a signed-out viewer got settings off the list: {sorted(extra)}"
        assert {"units", "trays", "screen_night"} <= seen, "and does get what the page needs"

        owner = routes.app.test_client()
        owner.post("/api/login", json={"password": "pw"})
        assert {"ai_notes", "latitude", "timezone"} <= set(_status(owner)["settings"])
    finally:
        set_password(False)
    assert not status_mod.VISITOR_SETTINGS & set(status_mod.SECRET_SETTINGS)


def test_every_setting_the_read_only_page_reads_is_on_the_visitor_list():
    """The dashboard's scripts name the settings they read. One that is read
    outside the Settings form but missing from the list would silently fall
    back to its default for signed-out viewers."""
    import re
    from pathlib import Path

    from harness import APP

    # read only to fill Settings controls, which a signed-out viewer never sees
    form_only = {"ai_enabled", "ai_report_hour", "ai_report_minute", "heat_sensor"}
    read = set()
    for script in list(Path(APP, "static", "js").glob("*.js")) + list(
        Path(APP, "static").glob("*.js")
    ):
        src = script.read_text()
        read |= set(re.findall(r"\bsettings\??\.([a-z_0-9]+)", src))
        read |= set(re.findall(r"\bsettings\[['\"]([a-z_0-9]+)['\"]\]", src))
        for fn, arg in re.findall(r"function (fillForm|setAiControls|setDliBand)\((\w+)\)", src):
            body = src[src.index(f"function {fn}({arg})") :]
            body = body[: body.index("\nexport ", 1) if "\nexport " in body[1:] else len(body)]
            read |= set(re.findall(rf"\b{arg}\??\.([a-z_0-9]+)", body))
            read |= set(re.findall(rf"\b{arg}\[['\"]([a-z_0-9]+)['\"]\]", body))
    read = {k for k in read if k in config.DEFAULTS}
    assert len(read) > 15, f"the scan found the page's settings ({sorted(read)})"
    missing = {
        k
        for k in read
        if k not in status_mod.VISITOR_SETTINGS and k not in form_only and k[:7] != "screen_"
    }
    assert not missing, f"read by the page but not on the visitor list: {sorted(missing)}"


def test_a_signed_out_viewer_gets_sun_times_to_the_quarter_hour():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(config.settings["timezone"])
    day = datetime.now(tz).replace(microsecond=0)
    exact = {
        "sunrise": day.replace(hour=6, minute=37, second=59),
        "sunset": day.replace(hour=16, minute=46, second=36),
        "on": day.replace(hour=6, minute=7, second=59),
        "off": day.replace(hour=17, minute=16, second=36),
    }
    with config.state_lock:
        config.state.update(exact)
    with config.settings_lock:
        config.settings["schedule_mode"] = "solar"

    def times(client):
        j = _status(client)
        return {k: datetime.fromisoformat(j[k]) for k in exact}

    assert times(c) == exact, "no password: as computed"
    set_password(True)
    try:
        anon = routes.app.test_client()
        got = times(anon)
        for k, t in got.items():
            assert (t.minute % 15, t.second) == (0, 0), f"{k} is on a quarter hour ({t})"
            assert abs((t - exact[k]).total_seconds()) <= 450, f"{k} is the nearest one"
        assert got["sunrise"].hour == 6 and got["sunrise"].minute == 45

        # a window the owner typed in says nothing about where the sun is
        with config.settings_lock:
            config.settings["schedule_mode"] = "fixed"
        got = times(anon)
        assert (got["on"], got["off"]) == (exact["on"], exact["off"])
        assert got["sunrise"] != exact["sunrise"], "sunrise is still rounded"

        owner = routes.app.test_client()
        owner.post("/api/login", json={"password": "pw"})
        assert times(owner) == exact, "signed in: as computed"
    finally:
        set_password(False)


def test_a_signed_out_viewer_does_not_get_the_machines_name_or_address():
    import hoststats

    real = hoststats.all_stats
    hoststats.all_stats = lambda: {
        "cpu_temp_c": 48.2,
        "host": "openSeedling",
        "ip": "192.0.2.20",
        "wifi": {"iface": "wlan0", "percent": 70, "dbm": -58},
        "model": "Raspberry Pi Zero 2 W",
    }
    try:
        assert set(c.get("/api/host").get_json()) >= {"host", "ip", "wifi"}, "no password"
        set_password(True)
        anon = routes.app.test_client().get("/api/host").get_json()
        assert set(anon) == {"cpu_temp_c", "model"}, f"signed out: health only ({sorted(anon)})"
        owner = routes.app.test_client()
        owner.post("/api/login", json={"password": "pw"})
        assert owner.get("/api/host").get_json()["ip"] == "192.0.2.20"
    finally:
        hoststats.all_stats = real
        set_password(False)


def test_with_https_only_a_password_is_refused_over_plain_http():
    was = config.TRUSTED_PROXY, config.LOGIN_HTTPS_ONLY
    checks = []
    real_check = routes.check_password_hash
    routes.check_password_hash = lambda h, pw: checks.append(pw) or real_check(h, pw)
    set_password(True)
    try:
        config.TRUSTED_PROXY, config.LOGIN_HTTPS_ONLY = "10.0.0.5", True
        assert _status(c)["login_https_only"] is True

        plain = routes.app.test_client()
        r = plain.post("/api/login", json={"password": "pw"})
        assert r.status_code == 403 and "https" in r.get_json()["error"]
        assert checks == [], "the password is not even checked"
        assert plain.post("/api/login", json={"password": "pw"}).status_code == 403, (
            "and the refusal is not counted as a wrong password"
        )
        assert not _status(plain)["authed"]
        assert plain.get("/api/status").status_code == 200, "the plain address still shows the page"

        https = routes.app.test_client()
        r = https.post("/api/login", json={"password": "pw"}, base_url="https://localhost")
        assert r.status_code == 200 and "Secure" in r.headers["Set-Cookie"]

        # without a proxy nothing arrives over HTTPS: the setting must not lock everyone out
        config.TRUSTED_PROXY = ""
        assert not config.login_https_only() and _status(c)["login_https_only"] is False
        assert (
            routes.app.test_client().post("/api/login", json={"password": "pw"}).status_code == 200
        )
        config.TRUSTED_PROXY = "not-an-address"
        assert not config.login_https_only()
    finally:
        config.TRUSTED_PROXY, config.LOGIN_HTTPS_ONLY = was
        routes.check_password_hash = real_check
        set_password(False)
