"""Tests: report."""

import pytest
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from harness import (
    WORK,
    ai_report,
    c,
    config,
    db,
    monitor,
)


def test_report_once():
    """The daily report runs once at its time, also when it fails."""
    runs = []

    def failing(kind):
        runs.append(kind)
        raise RuntimeError("report failed")

    keys = ("ai_enabled", "camera_enabled", "ai_report_hour", "ai_report_minute", "timezone")
    with config.settings_lock:
        saved = {k: config.settings.get(k) for k in keys}
        config.settings.update(
            ai_enabled=True, camera_enabled=True, ai_report_hour=8, ai_report_minute=0
        )
        tz_ = ZoneInfo(config.settings["timezone"])
    real = monitor.run_report, monitor.ai_report.have_key, monitor.clock_synced
    monitor.run_report = failing
    monitor.ai_report.have_key = monitor.clock_synced = lambda: True
    state = {"last_day": None, "primed": False}
    errors = 0
    try:
        for hour, minute in ((7, 58), (8, 0), (8, 1), (8, 2), (20, 0)):
            try:
                monitor._report_tick(state, datetime(2026, 3, 10, hour, minute, tzinfo=tz_))
            except RuntimeError:
                errors += 1
        late = {"last_day": None, "primed": False}
        monitor._report_tick(late, datetime(2026, 3, 10, 9, 0, tzinfo=tz_))
    finally:
        monitor.run_report, monitor.ai_report.have_key, monitor.clock_synced = real
        with config.settings_lock:
            config.settings.update(saved)
    assert runs == ["daily"] and errors == 1, (
        f"a daily report that fails is not run again that day ({len(runs)} run)"
    )
    assert late["last_day"] is not None, "starting after the report time does not run a report"


def test_ai_reply():
    import io
    import json as _json

    photo = WORK / "ai.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    sent = {}

    def fake_open(resp):
        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=None):
            sent["body"] = _json.loads(req.data)
            sent["timeout"] = timeout
            return R(_json.dumps(resp).encode())

        return opener

    real_open, real_key = ai_report.urllib.request.urlopen, ai_report.api_key
    ai_report.api_key = lambda: "test-key"
    try:
        ai_report.urllib.request.urlopen = fake_open(
            {
                "content": [{"type": "thinking", "thinking": ""}],
                "stop_reason": "max_tokens",
                "usage": {"output_tokens": 8000},
            }
        )
        r1 = ai_report.generate(photo, {})
        ai_report.urllib.request.urlopen = fake_open(
            {
                "content": [
                    {"type": "thinking", "thinking": ""},
                    {
                        "type": "text",
                        "text": '{"summary": "Looking good", "overall_health": "good"}',
                    },
                ],
                "stop_reason": "end_turn",
            }
        )
        r2 = ai_report.generate(photo, {})
    finally:
        ai_report.urllib.request.urlopen, ai_report.api_key = real_open, real_key
    assert sent["body"]["max_tokens"] >= 8000 and sent["timeout"] >= 180, (
        f"the report leaves room for thinking ({sent['body']['max_tokens']} tokens, {sent['timeout']} s)"
    )
    assert not r1["ok"] and "thinking" in r1["error"], (
        f"a reply that is all thinking is an error, not an empty report ({r1.get('error')})"
    )
    assert r2["ok"] and r2["report"]["summary"] == "Looking good", (
        "thinking blocks before the JSON are skipped"
    )


def test_report_by_setup():
    """The AI report files every reading under the setup it is in, from the
    setups as saved when the report runs."""

    def blocks(ctx):
        out, cur = {}, None
        for line in ctx.splitlines():
            m = re.match(r'Setup "(.+?)"( \(THE PHOTO SHOWS THIS SETUP\))?:$', line)
            if m:
                cur = m.group(1)
                out[cur] = [line]
            elif line.startswith("Not assigned to one setup"):
                cur = "_shared"
                out[cur] = [line]
            elif cur and line.startswith("  "):
                out[cur].append(line)
            else:
                cur = None
        return {k: "\n".join(v) for k, v in out.items()}

    with config.settings_lock:
        trays = sorted(config.settings.get("trays") or {})
        saved = {k: config.settings.get(k) for k in ("setups", "light2_on", "trays")}
        config.settings.update(light2_on=True)
    if len(trays) < 2:
        pytest.fail("the report test needs two trays in the default config")
        return
    t1, t2 = trays[0], trays[1]
    lab = {t: (config.settings["trays"][t].get("label") or f"Tray {t}") for t in (t1, t2)}
    db.log_many(
        [
            ("lux", 14000.0),
            ("temp:air", 24.0),
            ("humidity", 55.0),
            ("temp:soil", 26.0),
            (f"probe:{t1}", 1.3),
            (f"probe:{t2}", 1.8),
            ("pressure", 1012.0),
        ]
    )
    seed = {
        "name": "Seedlings",
        "light": "second",
        "lux": "",
        "trays": [t1],
        "sensors": ["temp:soil"],
        "camera": True,
        "reservoir": True,
        "dli_low": 10,
        "dli_high": 15,
    }
    tran = {
        "name": "Transplants",
        "light": "main",
        "lux": "lux",
        "trays": [t2],
        "sensors": ["temp:air", "humidity"],
        "fan": True,
        "dli_low": 15,
        "dli_high": 20,
    }
    try:
        r = c.post("/api/settings", json={"setups": [tran, seed]}).get_json()
        ctx = ai_report.build_context(monitor.gather_report_data())
        b = blocks(ctx)
        sd, tr = b.get("Seedlings", ""), b.get("Transplants", "")
        assert (
            r["ok"]
            and "THE PHOTO SHOWS THIS SETUP" in (sd.splitlines() or [""])[0]
            and "Light sensor: none" in sd
            and "PPFD" not in sd
            and "lx" not in sd
            and "air " not in sd
        ), (
            "the photo's setup is marked, and another setup's light sensor and air are not put under it"
        )
        assert (
            f'Tray "{lab[t1]}": soil moisture' in sd
            and "soil temperature" in sd
            and "Source reservoir" in sd
            and "Fan:" not in sd
        ), "the seedling setup gets its own tray's probe, its soil temperature and the reservoir"
        assert (
            "PPFD" in tr
            and "10-15" not in tr
            and "15-20" in tr
            and "air " in tr
            and "RH " in tr
            and f'Tray "{lab[t2]}": soil moisture' in tr
            and "Fan:" in tr
            and "THE PHOTO" not in tr
        ), "the transplant setup gets its light sensor, band, air, humidity, tray and fan"
        assert (
            "pressure" in b.get("_shared", "")
            and "Ambient conditions" not in ctx
            and "Light intensity:" not in ctx
            and "Soil-probe moisture % per tray" not in ctx
        ), (
            "a reading no setup claims is listed as shared; no unlabeled light or ambient lines remain"
        )
        # new selections, no restart: the next report follows them
        seed2 = dict(seed, camera=False, trays=[], sensors=["temp:soil", "temp:air"])
        tran2 = dict(tran, camera=True, trays=[t1, t2], sensors=["humidity"])
        r2 = c.post("/api/settings", json={"setups": [tran2, seed2]}).get_json()
        b2 = blocks(ai_report.build_context(monitor.gather_report_data()))
        sd2, tr2 = b2.get("Seedlings", ""), b2.get("Transplants", "")
        assert (
            r2["ok"]
            and "THE PHOTO SHOWS" in (tr2.splitlines() or [""])[0]
            and "THE PHOTO" not in sd2
            and "air " in sd2
            and "air " not in tr2
            and f'Tray "{lab[t1]}"' in tr2
            and f'Tray "{lab[t1]}"' not in sd2
        ), (
            "changing the setups moves the photo mark, the air sensor and the tray in the next report"
        )
        c.post("/api/settings", json={"setups": []})
        one = ai_report.build_context(monitor.gather_report_data())
        assert (
            "split into separate setups" not in one
            and "PPFD" in one
            and "air " in one
            and f'Tray "{lab[t1]}"' in one
            and f'Tray "{lab[t2]}"' in one
            and "Not assigned" not in one
        ), "with one setup everything is reported together, as before"
    finally:
        c.post("/api/settings", json={"setups": saved["setups"] or []})
        with config.settings_lock:
            config.settings["light2_on"] = saved["light2_on"]
