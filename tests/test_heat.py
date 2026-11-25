"""Tests: heat."""

import math
import time

from harness import (
    APP,
    Code,
    ai_report,
    c,
    config,
    db,
    hardware,
    heat,
    light_mod,
    page_js,
    readings_mod,
)


def test_heat_mat():
    """The smart plug as a heat mat thermostat on the soil temperature."""
    import alerts

    now = time.time()
    base = dict(
        config.DEFAULTS, heat_mode="auto", heat_target_f=75, heat_max_f=95, sample_interval_min=5
    )
    c75 = (75 - 32) * 5 / 9

    def d(cfg_, t, age, on_now, when, air=18.0, st=None):
        return heat.decide(cfg_, t, age, on_now, when, air, {} if st is None else st)

    rows = [
        (dict(base, heat_mode="off"), 15.0, 60, True, False),  # off is off
        (base, c75 - 3.0, 60, False, True),  # well below: on
        (base, c75 + 1.5, 60, True, False),  # well above: off
        (base, 20.0, 3600, True, False),  # stale reading: off
        (base, None, None, True, False),  # no reading: off
        (dict(base, heat_mode="on"), 36.0, 60, True, False),  # past the cut-off, even held on
        (dict(base, heat_mode="on"), None, None, False, True),  # held on without a probe
    ]
    bad = [
        i
        for i, (cfg_, t, age, on_now, want) in enumerate(rows)
        if d(cfg_, t, age, on_now, now)[0] != want
    ]
    assert not bad, (
        f"the thermostat's decisions: on well below, off well above, off on a stale "
        f"or missing probe and past the cut-off, On and Off obeyed (wrong rows: {bad})"
    )
    assert (
        d(base, 20.0, 3600, True, now)[3]
        and d(dict(base, heat_mode="on"), 36.0, 60, True, now)[3]
        and d(base, 20.0, 3600, True, now)[2]
        and not d(base, c75 - 1, 60, False, now)[3]
        and d(dict(base, heat_mode="off"), 20.0, 60, True, now)[3]
        and d(dict(base, heat_mode="on"), 20.0, 60, False, now)[3]
    ), "safety cut-offs and the grower's On and Off switch at once; Auto keeps to its cycle"
    # Auto: one pulse per window, its length set by the duty
    st = {}
    air_half = (c75 - heat.FF_BASE_C - 0.5 * heat.FF_GAIN_C) / heat.FF_AIR  # feed-forward ~ 50%
    near = [
        d(base, c75 - 0.2, 60, None, now + k, air_half, st)[0] for k in range(0, heat.WINDOW_S, 30)
    ]
    on_s = 30 * sum(near)
    first_off = near.index(False) if False in near else len(near)
    assert (
        0.3 < st["duty"] < 1.0
        and all(near[:first_off])
        and not any(near[first_off:])
        and abs(on_s - st["duty"] * heat.WINDOW_S) <= 30
    ), (
        f"Auto near the target runs one pulse of part of the {heat.WINDOW_S // 60}-min window, "
        f"then rests (duty {st['duty']:.2f}, on {on_s}s)"
    )
    st = {}
    for k in range(24):  # six hours flat out and still short
        d(base, c75 - 3, 60, None, now + k * heat.WINDOW_S, 5.0, st)
    assert (
        st["duty"] == 1.0 and st["integral"] <= heat.KP_PER_C * 3 * heat.WINDOW_S / heat.TI_S + 1e-9
    ), f"the integral does not wind up while the mat is flat out (integral {st['integral']:.3f})"
    st = {}
    tiny = [
        d(base, c75 + 0.35, 60, None, now + k, c75 - 0.2, st)[0]
        for k in range(0, heat.WINDOW_S, 30)
    ]
    assert not any(tiny) and st["duty"] == 0.0, (
        "a pulse shorter than a minute is skipped rather than clicking the relay"
    )

    # A model of the reference rig (docs/heat-tuning.md): about 12 min dead
    # time and two ~15 min lags; soil settles near 16.5 + 0.38 air + 3.69 duty C.
    # Under plain proportional control it swings about 2.8F, as the real soil
    # did, so it is fit to judge a tuning.
    def simulate(ctrl, hours=14.0):
        dt, dead, tau, g, c0, a0 = 30.0, 741.0, 0.24, 3.69, 16.5, 0.38
        tgt = (86 - 32) * 5 / 9
        x = z = tgt - 0.7
        meas, seen, hist, stc = x, [], [], {}
        for i in range(int(hours * 3600 / dt)):
            t = i * dt
            air = 26.2 + 1.6 * math.sin(2 * math.pi * (t / 86400.0 - 0.1))
            if i % 10 == 0:  # a reading every 5 min
                meas = round(x / 0.0625) * 0.0625
            hist.append(1.0 if ctrl(meas, air, t, stc) else 0.0)
            u = hist[i - int(dead / dt)] if i >= int(dead / dt) else 1.0
            h = dt / 3600
            z += h * ((c0 + a0 * air + g * u) - z) / tau
            x += h * (z - x) / tau
            if t > 3 * 3600:
                seen.append(x)
        return min(seen), max(seen), sum(seen) / len(seen)

    cfg86 = dict(base, heat_target_f=86, heat_max_f=95)
    t86 = (86 - 32) * 5 / 9

    def old_pi(meas, air, t, stc):  # the first tuning: Kp 0.6, Ti 1 h, air + 6.4C
        if stc.get("w") is None or t >= stc["w"] + 900:
            e = t86 - meas
            ff = (t86 - air) / 6.4
            raw = ff + 0.6 * e + stc.get("i", 0.0)
            if 0 < raw < 1 or (raw >= 1 and e < 0) or (raw <= 0 and e > 0):
                stc["i"] = stc.get("i", 0.0) + 0.6 * e * 900 / 3600
            duty = min(1, max(0, ff + 0.6 * e + stc.get("i", 0.0)))
            on = duty * 900
            on = 0 if on < 60 else (900 if 900 - on < 60 else on)
            stc["w"], stc["until"] = t, t + on
        return t < stc["until"]

    def auto(meas, air, t, stc):
        return heat.decide(cfg86, meas, 60, None, t, air, stc)[0]

    lo0, hi0, mean0 = simulate(old_pi)
    lo1, hi1, mean1 = simulate(auto)
    assert (hi0 - lo0) * 1.8 > 2.0 and (hi1 - lo1) * 1.8 < 1.0 and abs(mean1 - t86) * 1.8 < 0.3, (
        f"on a model of the rig that reproduces the first tuning's swing ({(hi0 - lo0) * 1.8:.1f}F), the "
        f"retuned Auto holds {(hi1 - lo1) * 1.8:.1f}F peak to peak, mean {(mean1 - t86) * 1.8:+.2f}F"
    )

    sent = []
    reading = {"v": (c75 - 2, now)}
    real_apply, real_rf = light_mod.kasa_apply, readings_mod.reading_filtered

    def fake_apply(on, retries=1):
        sent.append(bool(on))
        light_mod.kasa_state.update(on=bool(on), ok=True, error="", fails=0)
        return True

    light_mod.kasa_apply = fake_apply
    readings_mod.reading_filtered = lambda key, snap=None: (
        (18.0, reading["v"][1]) if key == "temp:air" else reading["v"]
    )
    with config.settings_lock:
        saved = {
            k: config.settings.get(k)
            for k in (
                "plug_use",
                "heat_mode",
                "heat_target_f",
                "heat_max_f",
                "kasa_host",
                "light_backend",
            )
        }
        config.settings.update(
            plug_use="light",
            heat_mode="auto",
            heat_target_f=75,
            heat_max_f=95,
            kasa_host="10.0.3.177",
            light_backend="dim",
        )
    heat.heat_state.update(
        on=None,
        since=0.0,
        sent=0.0,
        fault="",
        reason="not in use",
        duty=None,
        integral=0.0,
        window=None,
        on_until=0.0,
    )
    heat._prev_use = None
    try:
        heat.heat_pass(now)
        assert sent == [], "with the plug given to the light, the thermostat never touches it"
        with config.settings_lock:
            config.settings["plug_use"] = "heat"
        heat.heat_pass(now)  # cold: a full-power window
        reading["v"] = (c75 + 1.5, now + 30)
        heat.heat_pass(now + 30)  # warm now, but the window stands
        held = heat.heat_state["on"]
        reading["v"] = (c75 + 1.5, now + heat.WINDOW_S)
        heat.heat_pass(now + heat.WINDOW_S + 1)  # next window: no power
        assert sent[:1] == [True] and held is True and sent[-1] is False, (
            f"Auto sets the power once per {heat.WINDOW_S // 60}-min window: on while cold, "
            f"off from the next window once warm ({sent})"
        )
        ev_before = len(db.recent_events(500))
        reading["v"] = (c75 - 2, now + 2 * heat.WINDOW_S)
        heat.heat_pass(now + 2 * heat.WINDOW_S + 1)  # on again
        assert len(db.recent_events(500)) == ev_before, (
            "Auto's pulses are not logged as events (dozens a day); safety and mode changes are"
        )
        logged = db.series("heat:duty", hours=48)
        assert len(logged) >= 3 and all(0 <= v <= 100 for _, v in logged), (
            f"each window's power level is logged as heat:duty for the charts ({len(logged)} so far)"
        )
        saved_i = db.kv_get(heat.INTEGRAL_KEY) or {}
        heat._restored = False
        heat.heat_state["integral"] = 0.0
        heat._restore_integral(saved_i.get("ts", 0) + 60)
        restored = heat.heat_state["integral"]
        heat._restored = False
        heat.heat_state["integral"] = 0.0
        heat._restore_integral(saved_i.get("ts", 0) + 7200)
        assert (
            "integral" in saved_i
            and restored == saved_i["integral"]
            and heat.heat_state["integral"] == 0.0
        ), "the controller's integral survives a restart within the hour, and an old one is ignored"
        reading["v"] = (36.0, now + 2 * heat.WINDOW_S + 10)
        n = len(sent)
        heat.heat_pass(now + 2 * heat.WINDOW_S + 15)  # past the cut-off...
        assert sent[n:] == [False] and heat.heat_state["fault"], (
            "past the cut-off the mat goes off at once and the fault is raised"
        )
        failing = {"n": 0}

        def failing_apply(on, retries=1):
            failing["n"] += 1
            light_mod.kasa_state.update(ok=False, error="unreachable")
            return False

        light_mod.kasa_apply = failing_apply
        was_on = heat.heat_state["on"]
        reading["v"] = (c75 - 3, now + 3000)
        heat.heat_pass(now + 3000)
        assert (
            failing["n"] == 1
            and heat.heat_state["on"] == was_on
            and "plug not responding" in heat.heat_state["reason"]
        ), "when the plug does not answer, the mat is reported as it last was, not as asked"
        light_mod.kasa_apply = fake_apply
        sent.clear()
        light_mod.set_brightness_raw(40)
        assert sent == [], "with the plug given to the heat mat, the light's writes never switch it"
        with config.settings_lock:
            config.settings["plug_use"] = "light"
        heat.heat_pass(now + 2000)
        assert sent[:1] == [False] and heat.heat_state["on"] is None, (
            "handing the plug back to the light turns the mat off and leaves it to the light"
        )
        with config.settings_lock:
            config.settings["plug_use"] = "heat"
        sent.clear()
        heat.off_now()
        assert sent == [False] and heat.off_now in hardware._shutdown_steps["start"], (
            "shutting down turns the heat mat off"
        )
        r1 = c.post("/api/settings", json={"light_backend": "kasa"}).get_json()
        r2 = c.post("/api/settings", json={"heat_target_f": 80, "heat_max_f": 81}).get_json()
        r3 = c.post("/api/settings", json={"heat_sensor": "temp:air"}).get_json()
        assert (
            "light_backend" in r1.get("errors", {})
            and "heat_max_f" in r2.get("errors", {})
            and "heat_sensor" in r3.get("errors", {})
        ), (
            "the plug cannot be the light and the heat mat at once; the cut-off must clear "
            "the target by 3F; the probe must be a soil probe"
        )
        r4 = c.post("/api/settings", json={"heat_target_f": 85, "heat_max_f": 87}).get_json()
        e4 = r4.get("errors", {})
        js_ = page_js()
        assert (
            "88F" in e4.get("heat_max_f", "")
            and "85F" in e4.get("heat_max_f", "")
            and "not saved until" in e4.get("heat_target_f", "")
            and "fieldLabel(f,k)+' '+j.errors[k]" in js_
            and "function fieldLabel(f, k)" in js_
        ), (
            "a cut-off too close to the target says what it must be, on the page by the field's "
            "label rather than its internal name"
        )
        h1 = c.post("/api/heat", json={"mode": "on"}).get_json()
        with config.settings_lock:
            config.settings["plug_use"] = "light"
        h2 = c.post("/api/heat", json={"mode": "auto"}).get_json()
        st = c.get("/api/status").get_json().get("heat") or {}
        assert h1["ok"] and not h2["ok"] and "use" in st and "mode" in st and "on" in st, (
            "the Heat mat buttons work only when the plug is the heat mat's; the status reports it"
        )
        acfg = dict(alerts.DEFAULTS, sustain_seconds=0)
        alerts.reset()
        fired = [
            a
            for a in alerts.check_all({"_heat_fault": "no recent soil temperature"}, acfg)
            if a[1] == "heat_fault"
        ]
        assert fired and fired[0][0] == "fire", "a held-off heat mat sends an alert"
        alerts.reset()
    finally:
        light_mod.kasa_apply, readings_mod.reading_filtered = real_apply, real_rf
        with config.settings_lock:
            config.settings.update(saved)
        heat.heat_state.update(
            on=None,
            since=0.0,
            sent=0.0,
            fault="",
            reason="not in use",
            duty=None,
            integral=0.0,
            window=None,
            on_until=0.0,
        )
        heat._prev_use = None
    js = page_js()
    html = (APP / "templates" / "index.html").read_text()
    page = c.get("/").get_data(as_text=True)
    assert (
        'id="heatrow"' in html
        and 'name="plug_use"' in page
        and 'name="heat_target_f"' in page
        and "send('/api/heat'" in js
        and "renderHeat(j);" in js
        and ".lcbtn:not(.fanbtn):not(.heatbtn)" in js
        and ".lcbtn:not(.fanbtn)')" not in js
        and config.FORM["heat_target_f"]["kind"] == "tempF"
        and config.FORM["heat_target_f"]["round"] == 0.1
    ), (
        "the Light card has Heat mat buttons, Settings has the plug use and thermostat, "
        "temperatures are saved in F, and the light's buttons ignore the heat buttons"
    )
    mon = (APP / "monitor.py").read_text()
    assert (
        "if(key==='heat:duty')" in js
        and "if(key.startsWith('heat:'))return false;" in js
        and "if(k.startsWith('heat:')){const hs=setupsList.find(s=>s.heat);" in js
        and '"dry:", "growth", "moisture:", "heat:"' in mon
        and '"canopy:", "heat:"' in mon
    ), (
        "the power level is charted as Heat mat power on the heat mat's tab, never marked stale, "
        "and kept out of sensor health and stuck-sensor checks"
    )
    css = Code((APP / "static" / "style.css").read_text())
    assert (
        html.count('class="devrow ') == 2
        and 'id="heatdot"' in html
        and 'id="fandot"' in html
        and ".lightctl .lcrow{flex-wrap:nowrap}" in css
        and "#lightinfo:empty{display:none}" in css
        and "info.title=plugBad&&h.plug_error?h.plug_error:'';" in js
    ), (
        "Fan and Heat mat rows: buttons on one line, a status line with a dot under them, "
        "the plug's raw error only in a tooltip"
    )
    ctx = ai_report.build_context(
        {
            "by_setup": {"setups": []},
            "units": {"temp": "F"},
            "heat": {
                "use": True,
                "mode": "auto",
                "on": True,
                "target_f": 75,
                "reason": "below target",
            },
        }
    )
    assert "Heat mat under the trays: on now, thermostat holding the soil at 75F" in ctx, (
        "the AI report knows the heat mat's state"
    )
