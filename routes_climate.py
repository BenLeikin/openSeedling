"""Light and climate routes: the light, its schedule and sweeps, the smart
plug, the fan and the heat mat.

A Flask blueprint; growlight.py registers it on the app.
"""

import re
import threading
import time
from flask import Blueprint, jsonify, request

from applog import log
import db
import sensors

import config
import light as light_mod
import setups as setups_mod
import heat as heat_mod


bp = Blueprint("climate", __name__)


@bp.route("/api/lightning", methods=["POST"])
def api_lightning():
    """Run (or stop) a short lightning effect on the grow light."""
    data = request.get_json(silent=True) or {}
    if data.get("stop"):
        light_mod._lightning_stop.set()
        return jsonify(ok=True, stopping=True)
    with config.settings_lock:
        cfg = dict(config.settings)
    if not light_mod.lightning_available(cfg):
        return jsonify(
            ok=False, error="only available on a dimmable fixture wired through the inverted PWM"
        ), 200
    try:
        seconds = max(3, min(light_mod.LIGHTNING_MAX_S, int(data.get("seconds", 20))))
    except (TypeError, ValueError):
        seconds = 20
    style = (
        data.get("style")
        if data.get("style") in ("storm", "strike", "sheet", "flicker")
        else "storm"
    )
    busy = light_mod.holder()
    if busy:
        return jsonify(ok=False, error=f"the light is in use ({busy}); try again shortly"), 200
    with light_mod._lightning_lock:
        if light_mod.lightning_state["running"]:
            return jsonify(ok=False, error="already running"), 200
        light_mod._lightning_stop.clear()
        light_mod.lightning_state.update(running=True, until=time.time() + seconds, style=style)
    # the thread clears "running" when it ends, so nothing that can fail may
    # sit between setting the flag and starting it
    threading.Thread(
        target=light_mod._storm, args=(seconds, style), daemon=True, name="lightning"
    ).start()
    db.log_event("light", f"lightning: {style} for {seconds}s")
    return jsonify(ok=True, seconds=seconds, style=style)


@bp.route("/api/light", methods=["POST"])
def api_light():
    """Manual light hold. 'on' forces manual_bright, 'off' forces dark, 'auto'
    hands control back to the schedule. An optional 'brightness' (0-100) sets
    the level the 'on' hold uses."""
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    bright = data.get("brightness")
    if mode is not None and mode not in ("auto", "on", "off"):
        return jsonify(ok=False, error="mode must be auto|on|off"), 200
    if bright is not None:
        try:
            bright = max(0, min(100, int(bright)))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="brightness must be 0-100"), 200
    if mode is None and bright is None:
        return jsonify(ok=False, error="nothing to set"), 200
    with config.settings_lock:
        if mode is not None:
            config.settings["light_override"] = mode
        if bright is not None:
            config.settings["manual_bright"] = bright
        config.save_config()
        cur_mode = config.settings["light_override"]
        cur_bright = config.settings["manual_bright"]
    config.wake.set()  # apply now instead of waiting for the next loop pass
    if mode is not None:
        db.log_event("light", f"override set to {mode}")
    return jsonify(ok=True, mode=cur_mode, brightness=cur_bright)


@bp.route("/api/light_sweep", methods=["POST"])
def api_light_sweep():
    """Start (or cancel) a light response sweep."""
    data = request.get_json(silent=True) or {}
    if data.get("cancel"):
        with light_mod.sweep_lock:
            light_mod.sweep_state["cancel"] = True  # the worker restores the light
        return jsonify(ok=True, cancelled=True)
    target = "second" if data.get("light") == "second" else "main"
    if target == "second" and light_mod.light2_fixture() is None:
        return jsonify(
            ok=False,
            error="the second light is not available (turned off, or no second PWM channel)",
        ), 200
    key = setups_mod.lux_key_for_light(target)
    if not key or sensors.read_all().get(key) is None:
        return jsonify(
            ok=False,
            error=f"no light sensor reading for the "
            f"{setups_mod.light_label(target)}; assign one in "
            "Settings, Setups",
        ), 200
    try:
        step = max(1, min(25, int(data.get("step", 5))))
        settle = max(0.5, min(10.0, float(data.get("settle", 2.0))))
    except (TypeError, ValueError):
        step, settle = 5, 2.0
    linearize = bool(data.get("linearize"))
    if linearize:
        # a knee at 35% and a cutoff at 4% both vanish between 5% steps; the
        # calibration needs every percent to find them
        step = 1
        settle = max(settle, 1.5)
    busy = light_mod.holder()
    with light_mod.sweep_lock:
        if light_mod.sweep_state["running"]:
            return jsonify(ok=False, error="a sweep is already running"), 200
        if busy:
            return jsonify(ok=False, error=f"the light is in use ({busy}); try again shortly"), 200
        light_mod.sweep_state.update(
            running=True,
            pct=0,
            error="",
            cancel=False,
            started=time.time(),
            target=target,
            l2_raw=0.0,
        )
    threading.Thread(
        target=light_mod.run_light_sweep,
        args=(step, settle, linearize, target, key),
        daemon=True,
    ).start()
    est = int((101 / step + 1) * (settle + 0.3))
    return jsonify(ok=True, started=True, estimate_seconds=est)


@bp.route("/api/plug_discover", methods=["POST"])
def api_plug_discover():
    """Find TP-Link plugs on the local network.

    Broadcast discovery, so it sees only devices on the Pi's subnet.
    Credentials are optional: without them a newer KLAP device still answers
    with its model and address, and is reported as needs_auth.
    """
    data = request.get_json(silent=True) or {}
    user = str(data.get("user") or "").strip()
    password = str(data.get("pass") or "")
    try:
        timeout = max(2, min(15, int(data.get("timeout", 6))))
    except (TypeError, ValueError):
        timeout = 6

    async def scan():
        from kasa import Discover, Credentials

        kw = {"discovery_timeout": timeout}
        if user or password:
            kw["credentials"] = Credentials(user, password)
        found = await Discover.discover(**kw)
        out = []
        for host, dev in (found or {}).items():
            entry = {
                "host": host,
                "alias": "",
                "model": "",
                "on": None,
                "needs_auth": False,
                "error": "",
            }
            try:
                await dev.update()
                entry["alias"] = getattr(dev, "alias", "") or ""
                entry["model"] = getattr(dev, "model", "") or ""
                entry["on"] = bool(getattr(dev, "is_on", False))
            except Exception as e:
                entry["needs_auth"] = "auth" in str(e).lower()
                entry["error"] = str(e)[:120]
                entry["model"] = getattr(dev, "model", "") or ""
            out.append(entry)
        return sorted(out, key=lambda d: d["host"])

    try:
        devices = light_mod._kasa_run(scan(), timeout=timeout + 10)
    except ImportError:
        return jsonify(ok=False, error="python-kasa is not installed"), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200
    return jsonify(ok=True, devices=devices)


@bp.route("/api/plug_test", methods=["POST"])
def api_plug_test():
    """Connect to a plug and report what it is, without saving anything.

    Takes the host and credentials from the body, so a plug can be tried
    before it is saved; with no host it tests the configured plug.
    """
    data = request.get_json(silent=True) or {}
    host = str(data.get("host") or "").strip()
    user = str(data.get("user") or "").strip()
    password = str(data.get("pass") or "")
    if not host:
        host, user, password = light_mod.kasa_conf()
    if not host:
        return jsonify(ok=False, error="no plug address to test"), 200

    async def probe():
        dev = await light_mod._kasa_connect(host, user, password)
        await dev.update()
        return {
            "alias": getattr(dev, "alias", "") or "",
            "model": getattr(dev, "model", "") or "",
            "on": bool(getattr(dev, "is_on", False)),
        }

    try:
        info = light_mod._kasa_run(probe(), timeout=20)
    except ImportError:
        return jsonify(ok=False, error="python-kasa is not installed"), 200
    except Exception as e:
        msg = str(e)[:200]
        hint = ""
        if "credential" in msg.lower() or "auth" in msg.lower():
            hint = (
                "This plug wants TP-Link account credentials. If they are "
                "correct and it still fails, remove the plug in the Kasa "
                "app and add it again: changing the account password "
                "leaves the device holding the old one."
            )
        return jsonify(ok=False, error=msg, hint=hint), 200
    return jsonify(ok=True, **info)


@bp.route("/api/fan", methods=["POST"])
def api_fan():
    """Set the fan mode: auto (schedule + humidity), on, or off."""
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).strip()
    if mode and mode not in ("auto", "on", "off"):
        return jsonify(ok=False, error="mode must be auto, on or off"), 200
    with config.settings_lock:
        if mode:
            config.settings["fan_mode"] = mode
        if "speed" in data:
            try:
                config.settings["fan_speed"] = max(0, min(100, int(float(data["speed"]))))
            except (TypeError, ValueError):
                pass
        if "auto_speed" in data:
            try:
                config.settings["fan_auto_speed"] = max(0, min(100, int(float(data["auto_speed"]))))
            except (TypeError, ValueError):
                pass
        mode = config.settings.get("fan_mode", "auto")
        config.save_config()
    config.wake.set()  # wake the loop to apply it now
    return jsonify(ok=True, mode=mode)


@bp.route("/api/heat", methods=["POST"])
def api_heat():
    """Set the heat mat mode: auto (thermostat), on, or off."""
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).strip()
    if mode not in ("auto", "on", "off"):
        return jsonify(ok=False, error="mode must be auto, on or off"), 200
    with config.settings_lock:
        if config.settings.get("plug_use", "light") != "heat":
            return jsonify(
                ok=False,
                error="the smart plug is not set to the heat mat (Settings, System, Smart plug)",
            ), 200
        config.settings["heat_mode"] = mode
        config.save_config()
    threading.Thread(target=heat_mod.heat_pass, daemon=True).start()  # act now
    return jsonify(ok=True, mode=mode)


@bp.route("/api/schedule", methods=["POST"])
def api_schedule():
    """Update the schedule window alone, so the chart's drag handles can
    commit a change without resubmitting every setting. Refused in solar
    mode."""
    data = request.get_json(silent=True) or {}
    with config.settings_lock:
        mode = config.settings.get("schedule_mode", "solar")
        if mode == "solar":
            return jsonify(
                ok=False, error="switch to fixed or duration mode to drag the schedule"
            ), 200
        changed = {}
        for k in ("fixed_on", "fixed_off", "duration_end"):
            if k in data:
                v = str(data[k] or "").strip()
                if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", v):
                    config.settings[k] = v
                    changed[k] = v
        if "duration_hours" in data:
            try:
                h = max(0.0, min(24.0, float(data["duration_hours"])))
                config.settings["duration_hours"] = round(h, 2)
                changed["duration_hours"] = config.settings["duration_hours"]
            except (TypeError, ValueError):
                pass
        if changed:
            config.save_config()
    if changed:
        config.wake.set()  # recompute the window immediately
        log.info(f"schedule updated from chart: {changed}")
    return jsonify(ok=True, changed=changed, mode=mode)
