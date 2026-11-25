"""Tests: routes."""

import re
import threading
import time

from harness import (
    c,
    config,
    db,
    routes,
    sensors,
    set_password,
    status_mod,
)


def test_routes():
    errors = []
    for rule in routes.app.url_map.iter_rules():
        if rule.endpoint == "static" or "GET" not in rule.methods or rule.rule == "/api/stream":
            continue
        path = re.sub(r"<[^>]+>", "x.jpg", rule.rule)
        q = {"/api/series": "?sensor=lux", "/api/frame_context": "?ts=1"}.get(path, "")
        code = c.get(path + q).status_code
        if code >= 500:
            errors.append(f"GET {path} {code}")
    assert not errors, "every GET route answers without a 5xx" + (f" {errors}" if errors else "")

    mutating = [
        r.rule
        for r in routes.app.url_map.iter_rules()
        if "POST" in r.methods and r.rule not in ("/api/login", "/api/logout")
    ]
    leaks = [
        p for p in mutating if c.post(p, data="x", content_type="text/plain").status_code != 415
    ]
    assert not leaks, f"all {len(mutating)} mutating routes refuse non-JSON with 415" + (
        f" {leaks}" if leaks else ""
    )
    big = b'{"pad": "' + b"x" * (routes.MAX_BODY_BYTES + 1) + b'"}'
    assert (
        c.post("/api/pump", data=big, content_type="application/json").status_code == 413
        and c.post("/api/login", data=big, content_type="application/json").status_code == 413
    ), "a request body over the size limit is refused with 413, signed in or not"


def test_sign_in():
    with config.settings_lock:
        config.settings.update(kasa_user="me@example.com", latitude=34.2, longitude=-118.8)
    s = c.get("/api/status").get_json()["settings"]
    assert "kasa_user" not in s and "password_hash" not in s, "credentials never in /api/status"
    assert "latitude" in s, "no password set: location shown"

    set_password(True)
    s = c.get("/api/status").get_json()["settings"]
    assert "latitude" not in s and "longitude" not in s, "signed out: location hidden"
    assert c.post("/api/pump", json={}).status_code == 401, "signed out: JSON POST gets 401"

    routes.LOGIN_DELAY_S, routes.LOGIN_DELAY_MAX_S = 0.01, 0.2
    real_check = routes.check_password_hash
    active = {"n": 0, "max": 0}
    lk = threading.Lock()

    def tracked(h, pw):
        with lk:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.03)
        try:
            return real_check(h, pw)
        finally:
            with lk:
                active["n"] -= 1

    routes.check_password_hash = tracked
    routes._login_fails["n"] = 0
    threads = [
        threading.Thread(
            target=lambda: routes.app.test_client().post("/api/login", json={"password": "wrong"})
        )
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert active["max"] == 1, f"parallel logins are checked one at a time (max {active['max']})"
    assert routes._login_fails["n"] == 6, "failed logins are counted"
    t0 = time.time()
    c.post("/api/login", json={"password": "wrong"})
    assert time.time() - t0 >= 0.15, "delay grows after repeated failures"
    r = c.post("/api/login", json={"password": "pw"})
    assert r.status_code == 200 and routes._login_fails["n"] == 0, (
        "right password signs in and resets the count"
    )
    routes.check_password_hash = real_check
    s = c.get("/api/status").get_json()
    assert s["authed"] and "latitude" in s["settings"], "signed in: location shown"
    c.post("/api/logout", json={})
    set_password(False)


def test_live_stream():
    with routes.app.test_request_context("/api/stream"):
        resp = routes.api_stream()
    gen = iter(resp.response)
    first, second = next(gen), next(gen)
    assert first.startswith("retry") and "event: status" in second, "stream opens with a status"
    with status_mod._subs_lock:
        status_mod._subs.clear()  # what publish() does to a subscriber that fell behind
    try:
        next(gen)
        ended = False
    except StopIteration:
        ended = True
    assert ended, "a dropped subscriber's stream ends instead of idling"

    # three open tabs, one change: the status is built once and shared
    gens = []
    for _ in range(3):
        with routes.app.test_request_context("/api/stream"):
            gi = iter(routes.api_stream().response)
        next(gi)
        next(gi)  # retry line + the fresh status
        gens.append(gi)
    builds = {"n": 0}
    real_sp = status_mod.status_payload

    def counting(*a, **k):
        builds["n"] += 1
        return real_sp(*a, **k)

    status_mod.status_payload = counting
    try:
        status_mod.publish("test")
        texts = [next(gi) for gi in gens]
    finally:
        status_mod.status_payload = real_sp
    assert builds["n"] == 1 and len(set(texts)) == 1, (
        f"one change is rendered once for all open tabs ({builds['n']} builds for 3 tabs)"
    )
    for gi in gens:
        gi.close()


def test_data():
    nowi = int(time.time())
    errs = []

    def writer(n):
        try:
            for i in range(100):
                db.log_many([(f"test:w{n}", i)], ts=nowi - 90000 - i)
                db.kv_set(f"test:{n}", i)
        except Exception as e:
            errs.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = db._c().execute("SELECT COUNT(*) FROM readings WHERE sensor LIKE 'test:w%'").fetchone()[0]
    assert not errs and got == 600, f"six threads writing at once lose nothing ({got}/600)"

    plan = " ".join(
        str(tuple(r))
        for r in db._c().execute(
            "EXPLAIN QUERY PLAN SELECT value, ABS(ts-1) d FROM readings "
            "WHERE sensor='lux' AND ts BETWEEN 0 AND 5 ORDER BY d LIMIT 1"
        )
    )
    assert "USING INDEX" in plan, "reading lookups by time use the index"

    db.log_many([("probe:1", 1.5), ("temp:soil", 30.0)], ts=nowi - 7200)  # 86 F then
    db.log_many([("probe:1", 1.5), ("temp:soil", 20.0)], ts=nowi - 60)  # 68 F now
    with config.settings_lock:
        config.settings["probe_cal"] = {
            "1": {"wet": 1.0, "dry": 2.2, "temp_comp": {"coeff": 0.01, "ref_f": 70}}
        }
    pts = dict(map(tuple, c.get("/api/series_all?hours=3").get_json()["series"]["probe:1"]))
    old, new = pts.get(nowi - 7200), pts.get(nowi - 60)
    assert old is not None and abs(old - 1.34) < 1e-6 and abs(new - 1.52) < 1e-6, (
        f"each chart point is corrected with the soil temperature of its time ({old}, {new})"
    )

    assert sensors.LIVE_READS == (sensors._read_air, sensors._read_lux), (
        "live refresh reads only air and light"
    )
