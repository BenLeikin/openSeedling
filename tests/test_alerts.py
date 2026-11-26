"""Tests: alerts."""

import json
import time

from harness import (
    alerts,
    config,
    db,
    monitor,
)


def test_alert_reminders():
    """A standing problem is announced once, reminded once a day, and not
    announced again because the service restarted."""
    kept = dict(alerts._state)
    alerts.reset()
    try:
        alerts.configure({"sustain_seconds": 600})
        day = 86400
        seq = [
            alerts.evaluate("test:dry", True, now=1000),  # starts the sustain clock
            alerts.evaluate("test:dry", True, now=1000 + 600),
            alerts.evaluate("test:dry", True, now=1000 + 600 + 6 * 3600),
            alerts.evaluate("test:dry", True, now=1000 + 600 + day - 60),
            alerts.evaluate("test:dry", True, now=1000 + 600 + day),
        ]
        saved = json.loads(json.dumps(alerts.snapshot()))  # as it comes back from the database
        alerts.reset()
        alerts.configure({"sustain_seconds": 600})
        alerts.restore(saved)
        after_restart = [
            alerts.evaluate("test:dry", True, now=1000 + 600 + day + 300),
            alerts.evaluate("test:dry", True, now=1000 + 600 + 2 * day),
            alerts.evaluate("test:dry", False, now=1000 + 600 + 2 * day + 300),
        ]
        alerts.restore({"bad": "x", "worse": {"since": "never"}, 7: None})
        junk_ignored = set(alerts.snapshot()) == set()
    finally:
        alerts.reset()
        alerts._state.update(kept)
    assert (
        seq == [None, "fire", None, None, "remind"]
        and config.DEFAULTS["alert_cooldown_hours"] == 24
    ), f"a problem that lasts is announced once and reminded once a day ({seq})"
    # through the alert pass itself: saved to the database, read back at start
    kept = dict(alerts._state)
    real_check, real_deliver = alerts.check_all, monitor.deliver
    was_saved = monitor._alert_rules_saved
    sent = []
    monitor.deliver = lambda notices: sent.extend(notices)

    def one_rule(snap, cfg=None, unit="F", on_error=None):
        act = alerts.evaluate("test:standing", True, hold=0)
        return [(act, "test:standing", "Test", "still wrong", "warning")] if act else []

    alerts.check_all = one_rule
    try:
        alerts.reset()
        monitor._alert_rules_saved = None
        db.kv_set("alert_rules", {})
        monitor.run_alerts({"humidity": 50.0})
        stored = db.kv_get("alert_rules") or {}
        alerts.reset()  # the process restarts: memory is gone
        monitor._alert_rules_saved = None
        monitor.run_alerts({"humidity": 50.0})
    finally:
        alerts.check_all, monitor.deliver = real_check, real_deliver
        monitor._alert_rules_saved = was_saved
        db.kv_set("alert_rules", {})
        alerts.reset()
        alerts._state.update(kept)
    assert stored.get("test:standing", {}).get("active") is True and [n[0] for n in sent] == [
        "Test"
    ], (
        f"the alert pass keeps its rules in the database: one notice across a restart "
        f"({len(sent)} sent)"
    )
    assert after_restart == [None, "remind", "clear"] and junk_ignored, (
        f"after a restart a standing alert is not announced again; its reminder and its "
        f"all-clear still come ({after_restart})"
    )


def test_alert_delivery():
    """Alerts go to Discord and ntfy, and one that could not be sent is sent later."""
    sent = []
    up = {"discord": True, "ntfy": True}

    def channel(name):
        def send(title, message, level):
            if up[name]:
                sent.append((name, title, level))
            return up[name]

        return (lambda: True), send

    real = dict(monitor.CHANNELS)
    monitor.CHANNELS.update(discord=channel("discord"), ntfy=channel("ntfy"))
    monitor._undelivered.clear()
    try:
        monitor.deliver([("Soil too warm", "92F", "warning")])
        both = list(sent)
        sent.clear()
        up["discord"] = False
        monitor.deliver([("Reservoir empty", "refill", "error"), ("Fan fault", "x", "warning")])
        while_down = list(sent)
        waiting = [(n, t) for n, t, _, _ in monitor._undelivered]
        sent.clear()
        up["discord"] = True
        monitor.deliver([])
        after = list(sent)
    finally:
        monitor.CHANNELS.update(real)
        monitor._undelivered.clear()
    assert both == [
        ("discord", "Soil too warm", "warning"),
        ("ntfy", "Soil too warm", "warning"),
    ], "an alert goes to every configured channel"
    assert (
        [n for n, _, _ in while_down] == ["ntfy", "ntfy"]
        and waiting == [("discord", "Reservoir empty"), ("discord", "Fan fault")]
        and after == [("discord", "Reservoir empty", "error"), ("discord", "Fan fault", "warning")]
        and not monitor._undelivered
    ), (
        "what one channel could not take is kept and sent on the next pass, in order, "
        "without repeating it on the channel that worked"
    )


def test_stuck_in_the_dark():
    """A light sensor at zero overnight is dark, not stuck."""
    real_recent = db.recent_values
    seen_before = dict(monitor._seen_sensors)
    monitor._seen_sensors.update({"lux": time.time(), "lux:2": time.time(), "humidity": 1.0})
    db.recent_values = lambda key, n=9, max_age=7200: (
        [0.0] * 24 if key == "lux" else [431.0] * 24 if key == "lux:2" else [55.0, 54.0] * 12
    )
    try:
        stuck = monitor.stuck_sensors(300)
    finally:
        db.recent_values = real_recent
        monitor._seen_sensors.clear()
        monitor._seen_sensors.update(seen_before)
    assert "lux" not in stuck and "lux:2" in stuck and "humidity" not in stuck, (
        f"a light sensor reading zero in the dark is not reported as stuck; one frozen at "
        f"a lit value is ({sorted(stuck)})"
    )


def test_alert_levels():
    """A warning reaches Discord at a level the embed has a colour for."""
    import discord_alert

    fired = [
        a
        for a in alerts.check_all({"_stuck": {"lux:9": "24 identical readings"}}, {})
        if a[1] == "stuck:lux:9"
    ]
    assert fired and fired[0][4] in discord_alert.COLORS and fired[0][4] != "info", (
        f"a warning is sent at a level with its own colour ({fired and fired[0][4]})"
    )
    alerts.evaluate("stuck:lux:9", False, hold=0)


def test_one_failure_does_not_silence_the_other_rules(monkeypatch):
    """A part of the snapshot that raises, or a group of rules that raises,
    is skipped; everything else is still judged and still sent."""
    sent = []
    monkeypatch.setattr(monitor, "deliver", lambda notices: sent.extend(notices))
    monkeypatch.setattr(monitor, "_alert_rules_saved", None)
    with config.settings_lock:
        config.settings.update(alerts_enabled=True)
    monitor.water.fill_failures["1"] = "the float never tripped"

    def broken(*_):
        raise RuntimeError("disk gone")

    alerts.reset()
    monkeypatch.setattr(monitor.photos_mod, "disk_free_gb", broken)
    monkeypatch.setattr(alerts, "RULE_GROUPS", (broken,) + alerts.RULE_GROUPS)
    monitor.run_alerts({"humidity": 50.0})
    assert [n[0] for n in sent] == ["Watering did not complete"], (
        f"a failed fill is still announced when the disk check and another rule group fail ({sent})"
    )

    # a part that fails after having worked keeps its last values, so a
    # standing problem is not announced as resolved
    monkeypatch.setattr(monitor, "_part_last", {"watering": {"_fill_failed": {"1": "float"}}})
    alerts.reset()
    alerts.evaluate("fill_failed:1", True, hold=0)
    monkeypatch.setattr(monitor.water, "reservoir_state", broken)
    sent.clear()
    monitor.run_alerts({"humidity": 50.0})
    assert not any(t.startswith("Resolved") for t, _, _ in sent), (
        f"a failed check does not read as the problem having gone away ({sent})"
    )
