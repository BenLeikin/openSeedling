"""Tray and water routes: probe calibration, floats, plantings and the tray map, the pumps and auto-watering.

Registered on routes.app when routes.py imports this module.
"""

import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import jsonify, request

import db
import sensors

import config
import hardware
import water
import routes


@routes.app.route("/api/probe_cal", methods=["POST"])
@routes.require_auth
def probe_cal_set():
    """Capture a tray probe's reading as its wet (100%) or dry (0%) anchor.

    Watches the probe for a few seconds rather than taking one instant reading,
    so soil that is still absorbing water is caught before it becomes a bad
    anchor. A suspect capture is refused with the reason; pass force to store
    it regardless.
    """
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", ""))
    point = data.get("point")
    force = bool(data.get("force"))
    if tray not in (str(t) for t in sensors.PROBE_CHANNELS) or point not in ("wet", "dry"):
        return jsonify(ok=False, error="tray must be a wired probe and point wet|dry"), 200

    # Overriding a refusal stores the value that was refused, not a fresh one:
    # re-sampling would take another 15 seconds and could store something other
    # than the number the user just agreed to.
    given = data.get("volts")
    if force and given is not None:
        try:
            live = float(given)
            spread = drift = None
        except (TypeError, ValueError):
            live = None
    else:
        live, spread, drift = sensors.probe_settle(tray, seconds=water.PROBE_SETTLE_S)
        if live is None:
            live, spread = sensors.probe_spread(tray)
            drift = None
    if live is None:
        return jsonify(ok=False, error="no reading from that probe"), 200

    problems, notes = water.probe_cal_check(tray, point, live, drift, spread)
    # force means store it: the checks become advice, not a veto. They still
    # come back in the response so the reason is on record.
    if problems and not force:
        return jsonify(ok=False, tray=tray, point=point, volts=live,
                       spread=spread, drift=drift, problems=problems,
                       notes=notes, can_force=True,
                       error="not stored: " + problems[0])

    with config.settings_lock:
        cal = config.settings.setdefault("probe_cal", {})
        cal.setdefault(tray, {})[point] = live
        config.save_config()
    db.log_event("probe", f"tray {tray} {point} anchor set to {live:.4f}V"
                          + (" (forced)" if problems else ""))
    return jsonify(ok=True, tray=tray, point=point, volts=live, spread=spread,
                   drift=drift, problems=problems, notes=notes,
                   forced=bool(problems))


@routes.app.route("/api/float")
def api_float():
    return jsonify(floats={t: sensors.read_float(t) for t in sensors.FLOAT_PINS})


@routes.app.route("/api/probe_tempcomp", methods=["POST"])
@routes.require_auth
def probe_tempcomp():
    """Estimate (and optionally apply) a tray probe's temperature-drift
    coefficient from logged data. Run it over a warm, no-watering window."""
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", ""))
    if tray not in sensors.PROBE_CHANNELS:
        return jsonify(ok=False,
                       error=f"no probe wired for tray {tray} "
                             f"(probes: {', '.join(sorted(sensors.PROBE_CHANNELS))})"), 200
    try:
        hours = max(6, min(720, int(data.get("hours", 48))))
    except (TypeError, ValueError):
        hours = 48
    res = water.estimate_temp_comp(tray, hours)
    if not res.get("ok"):
        return jsonify(res), 200
    if res["span"] < 5:
        res["warning"] = (f"soil temp only varied {res['span']}F; "
                          "the estimate is weak until it swings more")
    if data.get("apply"):
        with config.settings_lock:
            cal = config.settings.setdefault("probe_cal", {}).setdefault(tray, {})
            cal["temp_comp"] = {"coeff": res["coeff"], "ref_f": water.TEMP_COMP_REF_F}
            config.save_config()
        res["applied"] = True
    return jsonify(res)


@routes.app.route("/api/planting_end", methods=["POST"])
@routes.require_auth
def api_planting_end():
    """Finish a planting: record it to history, then clear the cell.

    Body: {"tray": "1", "cell": "A1", "outcome": "transplanted"|"died"}.
    The cell is emptied so it can be replanted immediately; everything it held
    moves to the plantings table, which is what the germination stats read.
    Clearing without recording would delete a data point from the variety's
    success rate every time you potted something on.
    """
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", ""))
    cell = str(data.get("cell", ""))
    outcome = data.get("outcome")
    if outcome not in ("transplanted", "died"):
        return jsonify(ok=False, error="outcome must be transplanted or died"), 400
    with config.settings_lock:
        trays = config.settings.get("trays") or {}
        t = trays.get(tray)
        v = ((t or {}).get("cells") or {}).get(cell)
        if not v:
            return jsonify(ok=False, error="no such cell"), 404
        rec = dict(v)
    today = datetime.now(ZoneInfo(config.settings.get("timezone", "UTC"))).date().isoformat()
    pid = db.add_planting({"tray": tray, "cell": cell, "ended": today,
                           "outcome": outcome, **rec})
    with config.settings_lock:
        cells = config.settings["trays"][tray].setdefault("cells", {})
        cells.pop(cell, None)
        config.save_config()
    label = (rec.get("seed") or "cell").strip()
    db.log_event("planting", f"{tray}/{cell} {outcome}: {label}")
    return jsonify(ok=True, id=pid, cleared=True)


@routes.app.route("/api/plantings")
def api_plantings():
    """Finished plantings, newest first. Readable without login, like the rest
    of the dashboard."""
    try:
        return jsonify(plantings=db.plantings(limit=500))
    except Exception as e:
        return jsonify(plantings=[], error=str(e)[:120])


@routes.app.route("/api/planting_restore", methods=["POST"])
@routes.require_auth
def api_planting_restore():
    """Put a finished planting back in its cell, for a misclick.

    Refused when the cell has since been replanted: silently overwriting a new
    planting to undo an old mistake would be the worse failure.
    """
    data = request.get_json(silent=True) or {}
    try:
        pid = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="id required"), 400
    row = next((p for p in db.plantings(limit=500) if p["id"] == pid), None)
    if row is None:
        return jsonify(ok=False, error="no such history entry"), 404
    tray, cell = row["tray"], row["cell"]
    with config.settings_lock:
        t = (config.settings.get("trays") or {}).get(tray)
        if not t:
            return jsonify(ok=False, error=f"tray {tray} no longer exists"), 409
        cells = t.setdefault("cells", {})
        if cells.get(cell):
            return jsonify(ok=False,
                           error=f"{cell} has been replanted; clear it first"), 409
        cells[cell] = {k: row.get(k) or "" for k in
                       ("seed", "equipment", "planted", "sprouted", "source", "notes")}
        cells[cell]["count"] = row.get("count") or 0
        cells[cell]["archived"] = ""
        config.save_config()
    db.delete_planting(pid)
    db.log_event("planting", f"{tray}/{cell} restored from history")
    return jsonify(ok=True, tray=tray, cell=cell)


@routes.app.route("/api/trays", methods=["POST"])
@routes.require_auth
def api_trays():
    """Save what's planted in each cell. Body: {"tray": "1"|"2", "cells":
    {"A1": {"seed": str, "equipment": str, "planted": "YYYY-MM-DD",
            "sprouted": "YYYY-MM-DD", "archived": "YYYY-MM-DD"}, ...}}.
    `sprouted` records emergence (so days-to-germinate is measurable) and
    `archived` marks a cell transplanted out. Empty cells are dropped."""
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", ""))
    cells = data.get("cells")
    label = data.get("label")
    with config.settings_lock:
        known = set(config.settings.get("trays") or {})
    if tray not in known:
        return jsonify(ok=False, error=f"no such tray: {tray}"), 200
    if not isinstance(cells, dict):
        return jsonify(ok=False, error="cells must be an object"), 200
    clean = {}
    for cid, v in cells.items():
        if not isinstance(v, dict) or not re.fullmatch(r"[A-Z]\d{1,2}", str(cid)):
            continue
        seed = str(v.get("seed", "")).strip()[:60]
        equip = str(v.get("equipment", "")).strip()[:60]

        def _date(field, v=v):
            d = str(v.get(field, "") or "").strip()[:10]
            return d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else ""

        planted, sprouted, archived = _date("planted"), _date("sprouted"), _date("archived")
        source = str(v.get("source", "")).strip()[:60]
        notes = str(v.get("notes", "")).strip()[:200]
        try:
            count = max(0, min(99, int(v.get("count") or 0)))
        except (TypeError, ValueError):
            count = 0
        # which fields this cell shows; display-only, so an empty equipment
        # row can be hidden on the cells that will never have one
        _fields = ("seed", "equipment", "planted", "sprouted",
                   "source", "count", "notes")
        hide = [f for f in (v.get("hide") or [])
                if f in _fields or (f.startswith("!") and f[1:] in _fields)][:14]
        if (seed or equip or planted or sprouted or archived or hide
                or source or notes or count):
            clean[cid] = {"seed": seed, "equipment": equip, "planted": planted,
                          "sprouted": sprouted, "archived": archived,
                          "source": source, "count": count, "notes": notes,
                          "hide": hide}
    with config.settings_lock:
        trays = config.settings.setdefault("trays", {})
        t = trays.setdefault(tray, {"label": f"Tray {tray}", "rows": 4,
                                    "cols": 3, "cells": {}})
        t["cells"] = clean
        if label is not None:
            t["label"] = str(label).strip()[:40] or f"Tray {tray}"
        config.save_config()
    return jsonify(ok=True, tray=tray, count=len(clean))


MAX_TRAYS, MAX_DIM = 8, 12


def _cell_ids(rows, cols):
    return {f"{chr(65 + c)}{r}" for r in range(1, rows + 1) for c in range(cols)}


@routes.app.route("/api/tray_layout", methods=["POST"])
@routes.require_auth
def api_tray_layout():
    """Add, remove, rename or resize a tray.

    Body: {"action": "add"} | {"action": "remove", "tray": id}
        | {"action": "resize", "tray": id, "rows": n, "cols": n, "label": str}

    Resizing keeps every cell that still fits the new grid. Cells that fall
    outside it are reported as `dropped`, and are only discarded when the
    caller passes confirm=true, so a mis-tap cannot silently delete planting
    records.
    """
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "resize"))
    with config.settings_lock:
        trays = config.settings.setdefault("trays", {})

        if action == "add":
            if len(trays) >= MAX_TRAYS:
                return jsonify(ok=False, error=f"at most {MAX_TRAYS} trays"), 200
            nid = next(str(i) for i in range(1, MAX_TRAYS + 2) if str(i) not in trays)
            trays[nid] = {"label": f"Tray {nid}", "rows": 4, "cols": 3, "cells": {}}
            config.save_config()
            return jsonify(ok=True, tray=nid, added=True)

        tray = str(data.get("tray", ""))
        if tray not in trays:
            return jsonify(ok=False, error="no such tray"), 200

        if action == "remove":
            if len(trays) <= 1:
                return jsonify(ok=False, error="keep at least one tray"), 200
            filled = len(trays[tray].get("cells") or {})
            if filled and not data.get("confirm"):
                return jsonify(ok=False, needs_confirm=True, filled=filled,
                               error=f"tray {tray} has {filled} filled cells"), 200
            trays.pop(tray)
            config.save_config()
            return jsonify(ok=True, removed=tray)

        # resize / rename
        t = trays[tray]
        rows = t.get("rows", 4)
        cols = t.get("cols", 3)
        try:
            if "rows" in data:
                rows = max(1, min(MAX_DIM, int(data["rows"])))
            if "cols" in data:
                cols = max(1, min(MAX_DIM, int(data["cols"])))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="rows and cols must be numbers"), 200

        keep = _cell_ids(rows, cols)
        cells = t.get("cells") or {}
        dropped = sorted(c for c in cells if c not in keep)
        if dropped and not data.get("confirm"):
            return jsonify(ok=False, needs_confirm=True, dropped=dropped,
                           error=f"{len(dropped)} filled cells fall outside "
                                 f"a {cols}x{rows} grid"), 200
        t["rows"], t["cols"] = rows, cols
        t["cells"] = {k: v for k, v in cells.items() if k in keep}
        if "label" in data:
            t["label"] = str(data["label"]).strip()[:40] or f"Tray {tray}"
        config.save_config()
    return jsonify(ok=True, tray=tray, rows=rows, cols=cols, dropped=dropped)


@routes.app.route("/api/pump", methods=["POST"])
@routes.require_auth
def pump_test():
    data = request.get_json(silent=True) or {}
    tray = str(data.get("tray", "1"))
    if tray not in hardware.PUMP_PINS:
        return jsonify(ok=False,
                       error=f"no pump wired for tray {tray} "
                             f"(pumps: {', '.join(sorted(hardware.PUMP_PINS))})"), 200
    if tray not in hardware._pumps:
        return jsonify(ok=False, error=f"pump {tray} hardware not available"), 200
    try:
        secs = float(data.get("seconds", 3))
    except (TypeError, ValueError):
        secs = 3.0
    force = bool(data.get("force", False))
    if data.get("until_full"):
        threading.Thread(target=lambda: water.run_pump_until_full(tray, "fill", force),
                         daemon=True).start()
        return jsonify(ok=True, started=True, mode="fill", tray=tray)
    threading.Thread(target=lambda: water.run_pump(tray, secs, "manual", force),
                     daemon=True).start()
    return jsonify(ok=True, started=True, mode="timed", tray=tray)


@routes.app.route("/api/auto_water", methods=["POST"])
@routes.require_auth
def api_auto_water():
    """Arm or disarm autonomous watering.

    Arming is refused while any tray has a blocker, because the trigger is a
    probe reading and an untrustworthy probe means a flood or a drought.
    Disarming is always allowed and never questioned.
    """
    data = request.get_json(silent=True) or {}
    want = bool(data.get("enabled"))
    # which trays: the ones asked for (a setup's trays), else every pump tray
    asked = data.get("trays")
    trays_ = sorted({str(t) for t in asked} & set(hardware.PUMP_PINS)) if isinstance(asked, list) \
        else sorted(hardware.PUMP_PINS)
    if not trays_:
        return jsonify(ok=False, error="none of these trays has a pump"), 200
    with config.settings_lock:
        cur = set(water.armed_trays(config.settings))
    if not want:
        left = sorted(cur - set(trays_))
        with config.settings_lock:
            config.settings["auto_water_trays"], config.settings["auto_water"] = left, bool(left)
            config.save_config()
        db.log_event("auto_water", f"disarmed tray {', '.join(trays_)}")
        return jsonify(ok=True, enabled=False, armed=left)
    blockers = {t: w for t, w in water.auto_water_blockers().items() if t in trays_}
    if blockers:
        return jsonify(ok=False, enabled=False, blockers=blockers,
                       error="Auto-watering needs a calibrated probe and a "
                             "float switch on every tray it waters."), 200
    armed = sorted(cur | set(trays_))
    with config.settings_lock:
        config.settings["auto_water_trays"], config.settings["auto_water"] = armed, True
        config.save_config()
    db.log_event("auto_water", f"armed tray {', '.join(trays_)}")
    return jsonify(ok=True, enabled=True, armed=armed)
