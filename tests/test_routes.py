"""Tests: routes."""

import re
import threading
import time

from harness import (
    events,
    auth,
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
    big = b'{"pad": "' + b"x" * (auth.MAX_BODY_BYTES + 1) + b'"}'
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

    assert c.post("/api/login", json={"password": "wrong"}).status_code == 401
    auth.logins.clear()
    assert c.post("/api/login", json={"password": "pw"}).status_code == 200
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


def test_routes_are_guarded_unless_marked_public():
    """A route needs a login unless it says otherwise."""
    public = sorted(
        r.rule
        for r in routes.app.url_map.iter_rules()
        if getattr(routes.app.view_functions[r.endpoint], "is_public", False)
    )
    assert public == [
        "/",
        "/api/float",
        "/api/frame_context",
        "/api/host",
        "/api/login",
        "/api/logout",
        "/api/photos",
        "/api/plantings",
        "/api/report",
        "/api/screen_event",
        "/api/series",
        "/api/series_all",
        "/api/status",
        "/api/stream",
        "/favicon.ico",
        "/frame/<name>",
        "/photo/cropped.jpg",
        "/photo/latest",
        "/preview.jpg",
        "/rectified.jpg",
        "/screen",
        "/site.webmanifest",
        "/thumb/<name>",
        "/video",
    ], "the list of routes that need no login changed; update it here if that is intended"

    # a route nobody marked: take the mark off one and it needs a login
    view = routes.app.view_functions["data.api_host"]
    set_password(True)
    anon = routes.app.test_client()
    try:
        assert anon.get("/api/host").status_code == 200
        del view.is_public
        assert anon.get("/api/host").status_code == 401, "an unmarked route needs a login"
        assert anon.get("/api/backup").status_code == 401
        assert anon.get("/api/status").status_code == 200, "a public route does not"
        assert anon.get("/static/style.css").status_code == 200
        assert anon.get("/no/such/route").status_code == 404
    finally:
        view.is_public = True
        set_password(False)


def test_a_change_must_be_a_json_object():
    for body in ("[1, 2]", '"text"', "7"):
        r = c.post("/api/fan", data=body, content_type="application/json")
        assert r.status_code == 400, f"a JSON body that is not an object is refused ({body})"
    assert c.post("/api/login", data="x", content_type="text/plain").status_code == 415
    assert c.post("/api/logout", data="x", content_type="text/plain").status_code == 415
    assert c.post("/api/fan", json={"mode": "auto"}).status_code == 200


def test_without_a_password_only_its_own_names_are_answered():
    """With no password a page on another site could be pointed at this server
    by DNS rebinding; its requests carry that site's name as their Host."""
    set_password(False)
    hosts = {
        "localhost:5000": 200,
        "192.168.1.20:5000": 200,
        "[fe80::1]:5000": 200,
        f"{config.ALLOWED_HOSTS and sorted(config.ALLOWED_HOSTS)[0]}": 200,
        "evil.example:5000": 403,
        "evil.example": 403,
    }
    got = {h: c.get("/api/status", headers={"Host": h}).status_code for h in hosts}
    assert got == hosts
    post = c.post("/api/fan", json={"mode": "auto"}, headers={"Host": "evil.example:5000"})
    assert post.status_code == 403
    set_password(True)
    try:
        r = routes.app.test_client().get("/api/status", headers={"Host": "grow.example.org"})
        assert r.status_code == 200, "with a password set, any name reaches the public pages"
    finally:
        set_password(False)


def test_unreadable_settings_do_not_open_guarded_routes():
    was = config.config_broken
    config.config_broken = "config.json could not be read"
    try:
        anon = routes.app.test_client()
        assert anon.get("/api/backup").status_code == 401
        assert anon.get("/api/status").status_code == 200
        assert anon.post("/api/fan", json={"mode": "auto"}).status_code == 503
    finally:
        config.config_broken = was


def test_a_refused_request_announces_no_change():
    seen = []
    events.subscribe(seen.append)
    set_password(True)
    try:
        anon = routes.app.test_client()
        anon.post("/api/fan", json={"mode": "auto"})  # 401
        anon.post("/api/fan", data="x", content_type="text/plain")  # 415
        refused = list(seen)
        signed_in = routes.app.test_client()
        signed_in.post("/api/login", json={"password": "pw"})
        seen.clear()
        signed_in.post("/api/fan", json={"mode": "auto"})
    finally:
        set_password(False)
        events.unsubscribe(seen.append)
    assert "change" not in refused, "a request that was refused changed nothing"
    assert "change" in seen, "one that was accepted announces the change"
