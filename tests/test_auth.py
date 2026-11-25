"""Tests: signing in, sessions, and what a signed-out viewer may start."""

import threading
import time

import pytest
from werkzeug.security import generate_password_hash

from harness import auth, config, routes, set_password


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
