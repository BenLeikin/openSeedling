#!/usr/bin/env python3
"""Daily AI plant report.

Sends the latest tray photo plus the current sensor/grid/schedule data to the
Claude API and gets back a structured horticultural report: germination, health
flags, light and water assessment, concerns, and concrete recommendations. The
report is stored as JSON for the dashboard and a one-line summary is pushed to
whichever alert channels are configured (Discord, ntfy). Designed to run once a day, so cost is a few cents at most.

API key (in priority order):
  1. ANTHROPIC_API_KEY environment variable
  2. a .anthropic_key file next to this module (gitignored)

No SDK dependency -- it POSTs to the Messages API with stdlib urllib so there's
nothing extra to pip-install on the Pi.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
KEY_FILE = Path(__file__).with_name(".anthropic_key")
MAX_IMG_W = 1024   # downscale the photo before sending to keep token cost low


def api_key():
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k:
        return k.strip()
    try:
        if KEY_FILE.exists():
            return KEY_FILE.read_text().strip() or None
    except Exception:
        pass
    return None


def have_key():
    return bool(api_key())


def _image_b64(path):
    """Return a base64 JPEG of the photo, downscaled to MAX_IMG_W on the long
    edge. Falls back to the raw file bytes if OpenCV isn't available."""
    try:
        import cv2
        img = cv2.imread(str(path))
        if img is not None:
            h, w = img.shape[:2]
            if max(h, w) > MAX_IMG_W:
                s = MAX_IMG_W / float(max(h, w))
                img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))))
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                return base64.b64encode(buf.tobytes()).decode()
    except Exception:
        pass
    return base64.b64encode(Path(path).read_bytes()).decode()


PROMPT = """You are an expert horticulturist reviewing a daily top-down photo of \
a seedling tray grown indoors under an LED grow light. IMPORTANT: grow lights \
tint photos (magenta or pink from red/blue LEDs, warm yellow from white ones). \
The cast comes from the light, not the plants: judge leaf color and health \
relative to whatever tint this photo has, and don't call healthy leaves \
"discolored" because of it. The grower notes, if any, may describe the light.

Study the photo together with the controller data provided, then return ONE JSON \
object and nothing else (no prose, no code fences) with exactly these fields:

{
  "summary": "1-2 sentence plain-English status, suitable for a phone notification",
  "overall_health": "good" | "watch" | "problem",
  "germination": {"sprouted": <int or null>, "total_cells": <int or null>, "notes": "<string>"},
  "growth_stage": "<e.g. pre-emergence, cotyledon, first true leaves, ...>",
  "per_cell": [ {"cell": "B1", "note": "<short observation>"} ],
  "variety_check": [ {"cell": "B1", "expected": "<what the planting map says>", "looks_consistent": true | false | null, "why": "<only if it looks wrong>"} ],
  "light": {"assessment": "too_low" | "ok" | "too_high" | "unsure", "reason": "<cite legginess/stretch or bleaching you actually see>"},
  "water": {"assessment": "too_dry" | "ok" | "too_wet" | "unsure", "reason": "<use the probe moisture numbers AND what the soil looks like in the photo>"},
  "concerns": [ "<specific issue: damping-off, algae, fungus gnats, mould, leggy, wilting, etc.>" ],
  "recommendations": [ "<concrete action the grower can take today>" ],
  "confidence": "low" | "medium" | "high"
}

Rules:
- Be specific and honest. If the photo is unclear or you genuinely can't tell, say \
so in that field and lower "confidence" rather than inventing detail.
- Only flag a concern you can actually see or that the data supports; use an empty \
array if there are none.
- "per_cell" should list only notable cells (problems, standouts, or the largest), \
not every cell.
- "variety_check": the planting map already records what was sown in every cell, \
so do NOT guess species. Use this field only to flag a cell whose seedling looks \
inconsistent with what is recorded (wrong cotyledon shape for that variety, or a \
seedling in a cell listed as empty or as equipment). Set "looks_consistent" to \
null when the photo cannot support a judgement, and return an empty array when \
nothing looks out of place. Cotyledon-stage ID is genuinely hard, so only flag \
something you are reasonably sure about.
- Keep each string concise."""


def build_context(d):
    """Turn the controller data dict into a compact text block for the prompt."""
    L = []
    L.append(f"Date: {d.get('date','?')}")
    if d.get("days_running") is not None:
        L.append(f"Days of logged data: {d['days_running']}")
    if d.get("location"):
        L.append(f"Location: {d['location']}")
    lt = d.get("light") or {}
    if lt:
        L.append(f"Light: phase={lt.get('phase','?')}, brightness={lt.get('brightness','?')}%, "
                 f"on~{lt.get('on','?')} off~{lt.get('off','?')}, "
                 f"capture brightness={lt.get('capture_brightness','?')}%")
    g = d.get("grid") or {}
    if g:
        L.append(f"Grid: {g.get('rows','?')} rows x {g.get('cols','?')} cols")
        names = g.get("names") or {}
        if names:
            L.append("Cell labels: " + ", ".join(f"{k}={v}" for k, v in names.items()))
    cp = d.get("canopy") or {}
    if cp:
        L.append("Canopy coverage % per tray (camera-measured share of plant "
                 "pixels; useful as a trend, understated under a strongly "
                 "tinted light): " + ", ".join(f"{k}={v}" for k, v in sorted(cp.items())))
    pm = d.get("probe_moisture") or {}
    if pm:
        L.append("Soil-probe moisture % per tray (direct sensor, more reliable than "
                 "the camera estimate; 100=just watered, lower=drier): "
                 + ", ".join(f"{k}={v}" for k, v in sorted(pm.items())))
    pl = d.get("planting") or {}
    if pl:
        L.append("Planting map (what is sown where; use this instead of guessing "
                 "species where a cell is listed):")
        for tray, rows in pl.items():
            L.append(f"  {tray}: " + "; ".join(rows))
    U = d.get("units") or {"temp": "F", "press": "hPa"}
    env = d.get("environment") or {}
    if env:
        bits=[]
        if env.get("air_f") is not None: bits.append(f"air {env['air_f']}{U['temp']}")
        if env.get("humidity") is not None: bits.append(f"RH {env['humidity']}%")
        if env.get("lux") is not None: bits.append(f"light {env['lux']} lx")
        if env.get("pressure") is not None: bits.append(f"pressure {env['pressure']} {U['press']}")
        L.append("Ambient conditions: " + ", ".join(bits))
    lm = d.get("light_metrics") or {}
    if lm.get("ppfd") is not None:
        line = f"Light intensity: {lm['ppfd']} PPFD (umol/m2/s) at the sensor"
        if lm.get("dli") is not None:
            lo, hi = lm.get("dli_target") or (15, 20)
            line += (f"; daily light integral so far {lm['dli']} mol/m2/day "
                     f"(the grower's seedling target is {lo:g}-{hi:g})")
        L.append(line)
    fan = d.get("fan")
    if fan:
        L.append(f"Fan: {fan}")
    pt = d.get("pressure_trend")
    if pt:
        L.append(f"Barometric trend: {pt['words']} ({pt['change_3h']:+} hPa over 3h)"
                 + (f", {pt['change_24h']:+} hPa over 24h" if pt.get("change_24h") is not None else "")
                 + "  [trend deltas always in hPa]")
    gm = d.get("germination") or {}
    if gm:
        L.append("Germination by variety: "
                 + "; ".join(f"{k}: {v}" for k, v in gm.items()))
    stf = d.get("soil_temp_f") or {}
    if stf:
        band = ("18-29C germination, 21-27C once sprouted"
                if U["temp"] == "C" else
                "65-85F germination, 70-80F once sprouted")
        L.append(f"Soil temperature {U['temp']}: "
                 + ", ".join(f"{k}={v}" for k, v in sorted(stf.items()))
                 + f" (chiles: {band}; sustained heat past that stretches seedlings)")
    if d.get("float") is not None:
        L.append(f"Reservoir float: {d['float']}")
    if d.get("reservoir"):
        L.append(f"Source reservoir level: {d['reservoir']}"
                 + (" (pump runs are refused)" if d["reservoir"] == "empty" else ""))
    if d.get("pump_today_s") is not None:
        L.append(f"Pump runtime today: {d['pump_today_s']}s; last: {d.get('pump_last','none')}")
    if d.get("notes"):
        L.append(f"Grower notes: {d['notes']}")
    return "\n".join(L)


def _extract_json(text):
    """Pull the JSON object out of the model's reply, tolerating code fences or
    stray prose around it."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    try:
        i, j = t.index("{"), t.rindex("}")
        return json.loads(t[i:j + 1])
    except Exception:
        return None


def generate(photo_path, data, model=None, max_tokens=2048, timeout=90):
    """Call the Claude API with the photo + context. Returns a dict:
    {ok, report?, raw?, ts, model, usage?, error?}. Never raises."""
    key = api_key()
    if not key:
        return {"ok": False, "error": "no API key (set ANTHROPIC_API_KEY or add a "
                ".anthropic_key file)", "ts": int(time.time())}
    if not photo_path or not Path(photo_path).exists():
        return {"ok": False, "error": "no photo to analyze yet", "ts": int(time.time())}
    model = model or DEFAULT_MODEL
    try:
        img_b64 = _image_b64(photo_path)
    except Exception as e:
        return {"ok": False, "error": f"could not read photo: {e}", "ts": int(time.time())}
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": PROMPT + "\n\nController data:\n" + build_context(data)},
            ],
        }],
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": ANTHROPIC_VERSION}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        return {"ok": False, "error": f"API HTTP {e.code}: {detail}", "ts": int(time.time())}
    except Exception as e:
        return {"ok": False, "error": f"request failed: {e}", "ts": int(time.time())}
    text = "".join(b.get("text", "") for b in resp.get("content", [])
                   if b.get("type") == "text")
    report = _extract_json(text)
    out = {"ok": True, "ts": int(time.time()), "model": model,
           "usage": resp.get("usage"), "raw": text}
    if report is not None:
        out["report"] = report
    else:
        out["report"] = {"summary": text.strip()[:300] or "(no summary)",
                         "overall_health": "watch",
                         "concerns": ["AI reply was not valid JSON; see raw text"],
                         "recommendations": [], "confidence": "low"}
        out["parse_error"] = True
    return out


if __name__ == "__main__":
    import sys
    print("API key present:", have_key())
    if len(sys.argv) > 1:
        demo = {"date": "today", "location": "Thousand Oaks, CA",
                "canopy": {"Tray 1": 12.5, "Tray 2": 9.1}, "float": "not full",
                "notes": "peat/vermiculite/perlite; mixed germination"}
        print(json.dumps(generate(sys.argv[1], demo), indent=2)[:1500])
