#!/usr/bin/env python3
"""Behavior tests for the dashboard in a real (headless) browser.

    python3 tests/test_ui.py              # from the repo, on a desktop

Runs the app from a throwaway copy of the repo on a spare port, with GPIO and
PWM faked (tests/fakehw) and a day of made-up readings, then drives the page
the way a person would and checks what happened: what was saved, what the
form shows after a reload, what is on screen. Unlike the main suite it does
not look at how the page's code is written, so rewording or reorganizing the
code cannot fail these tests, and a real regression written differently
cannot pass them.

Needs Playwright with Chromium (pip install playwright; playwright install
chromium). Without it every test is skipped and the run still exits 0, so it
is harmless on the Pi, which does not run it.
"""

import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKEHW = REPO / "tests" / "fakehw"
results = {"pass": 0, "fail": 0, "skip": 0}


def check(ok, label):
    results["pass" if ok else "fail"] += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


def skip(label):
    results["skip"] += 1
    print(f"  SKIP  {label}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# a throwaway app with test settings and a day of readings


def make_app():
    work = Path(tempfile.mkdtemp(prefix="openseedling-ui-"))
    app = work / "app"
    shutil.copytree(
        REPO,
        app,
        ignore=shutil.ignore_patterns(
            ".git",
            "venv",
            "__pycache__",
            "timelapse",
            "timelapse_archive",
            "growlight.db*",
            "config.json",
            ".env",
            ".secret",
            "ai_report.json",
            "*.jpg",
            "*.mp4",
            "*.log",
        ),
    )
    cfg = {
        "trays": {
            "1": {"label": "Seedling 1", "rows": 3, "cols": 4, "cells": {}},
            "2": {"label": "Seedling 2", "rows": 3, "cols": 4, "cells": {}},
            "3": {
                "label": "Transplants",
                "rows": 4,
                "cols": 5,
                "cells": {
                    f"{c}{r}": {"seed": "Fatalii", "planted": "2026-09-20"}
                    for c in "ABCDE"
                    for r in range(1, 5)
                },
            },
        },
        "setups": [
            {
                "id": "t",
                "name": "Transplants",
                "light": "main",
                "lux": "lux",
                "trays": ["3"],
                "sensors": ["humidity", "temp:air", "pressure"],
                "fan": True,
                "camera": True,
                "dli_low": 12,
                "dli_high": 15,
            },
            {
                "id": "s",
                "name": "Seedlings",
                "light": "second",
                "lux": "",
                "trays": ["1", "2"],
                "sensors": ["humidity", "temp:air", "pressure", "temp:soil"],
                "heat": True,
                "dli_low": 8,
                "dli_high": 10,
            },
        ],
        "plug_use": "heat",
        "light2_on": True,
        "schedule_mode": "fixed",
        "fixed_on": "07:00",
        "fixed_off": "19:00",
    }
    (app / "config.json").write_text(json.dumps(cfg))
    seed = r"""
import math, time, db
db.init()
now = time.time()
rows = []
for i in range(0, 26 * 3600, 300):
    t = now - 26 * 3600 + i
    hr = time.localtime(t).tm_hour + time.localtime(t).tm_min / 60
    day = 7 <= hr < 19
    rows += [(t, "temp:air", 23.0 + (3 * math.sin(math.pi * (hr - 7) / 12) if day else 0)),
             (t, "humidity", 50.0), (t, "pressure", 1013.0),
             (t, "lux", 14000.0 if day else 0.0), (t, "temp:soil", 29.6),
             (t, "probe:1", 1.42), (t, "probe:2", 1.5),
             (t, "sys:mem_free", 180.0), (t, "sys:app_mem", 45.0)]
    if i % 900 == 0:
        rows.append((t, "heat:duty", 100.0 if not day else 80.0))
for t, k, v in rows:
    db.log_reading(k, v, ts=t)
"""
    env = dict(os.environ, PYTHONPATH=str(FAKEHW))
    subprocess.run(
        [sys.executable, "-c", seed], cwd=app, env=env, check=True, capture_output=True, timeout=120
    )
    return work, app


def start(app, port):
    env = dict(os.environ, PYTHONPATH=str(FAKEHW), GROWLIGHT_HTTP_PORT=str(port))
    log = open(app / "ui-test.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-u", "growlight.py"],
        cwd=app,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            with urllib.request.urlopen(base + "/api/status", timeout=2) as r:
                if r.status == 200:
                    return proc, base
        except Exception:
            time.sleep(0.5)
    proc.kill()
    sys.exit("the app did not start; see " + str(app / "ui-test.log"))


def saved(app):
    return json.loads((app / "config.json").read_text())


# ---------------------------------------------------------------------------
# tests


def open_all(page):
    page.evaluate("()=>document.querySelectorAll('details').forEach(d=>d.open=true)")


def settings_round_trip(page, base, app):
    """Every Settings field: change it, save, check what was stored, reload,
    check what the form shows. This is the whole save and fill path for all
    of config.FORM, field by field."""
    page.goto(base + "/")
    page.wait_for_timeout(2500)
    open_all(page)
    spec = page.evaluate("()=>JSON.parse(document.getElementById('formspec').textContent)")
    before = saved(app)
    want = {}  # key -> value we typed (as the form holds it)
    for k, s in spec.items():
        el = page.locator(f"#cfgform [name='{k}']")
        if el.count() == 0:
            continue
        kind = s["kind"]
        tag = el.evaluate("e=>e.tagName.toLowerCase()")
        cur = el.evaluate("e=>e.type==='checkbox'?e.checked:e.value")
        if kind == "bool":
            want[k] = not cur
        elif kind == "choice":
            opts = [o for o in el.evaluate("e=>[...e.options].map(o=>o.value)") if o != str(cur)]
            pick = {"light_backend": "dim", "plug_use": "heat", "units": "imperial"}.get(k)
            want[k] = (
                pick if pick and pick != str(cur) or k == "units" else (opts[0] if opts else cur)
            )
            if k == "units":
                want[k] = "imperial"  # temperatures below are typed in F
        elif kind == "time":
            want[k] = "06:15"
        elif kind == "secret":
            want[k] = "pw-test"
        elif kind == "text":
            want[k] = {
                "timezone": "America/Denver",
                "roi": "0.1,0.1,0.8,0.8",
                "usb_device": "/dev/video2",
                "kasa_host": "10.0.0.5",
                "kasa_user": "tester",
                "heat_sensor": "temp:soil",
            }.get(k, cur)
        elif kind == "tempF":
            want[k] = {
                "heat_target_f": "80",
                "heat_max_f": "90",
                "soil_temp_low_f": "79",
                "soil_temp_high_f": "84",
            }[k]
        else:
            lo, hi = s["min"], s["max"]
            step = (
                1
                if kind == "int"
                else (float(s["step"]) if s.get("step") not in (None, "any") else 0.5)
            )
            try:
                c = float(cur)
            except ValueError:
                c = lo
            v = c + step if c + step <= hi else c - step
            if v < lo:
                v = lo
            want[k] = str(int(v)) if kind == "int" else str(round(v, 4))
        val = want[k]
        if kind == "bool":
            el.evaluate(
                "(e,v)=>{e.checked=v;e.dispatchEvent(new Event('change',{bubbles:true}))}", val
            )
        elif tag == "select":
            el.evaluate(
                "(e,v)=>{e.value=v;e.dispatchEvent(new Event('change',{bubbles:true}))}", str(val)
            )
        else:
            el.evaluate(
                "(e,v)=>{e.value=v;e.dispatchEvent(new Event('input',{bubbles:true}))}", str(val)
            )
    page.locator("#cfgform button[type=submit]").click()
    page.wait_for_timeout(2500)
    msg = page.inner_text("#msg")
    after = saved(app)
    stored_wrong, silent = [], []
    for k, v in want.items():
        kind = spec[k]["kind"]
        got = after.get(k)
        if kind == "bool":
            ok = got is v
        elif kind == "secret":
            ok = got == v
        elif kind in ("int", "float", "tempF"):
            ok = got is not None and math.isclose(float(got), float(v), abs_tol=0.051)
        elif spec[k].get("ints"):
            ok = str(got) == str(v)
        else:
            ok = str(got) == str(v)
        if not ok:
            (stored_wrong if got != before.get(k) else silent).append(
                f"{k}: typed {v!r}, stored {got!r}"
            )
    check(
        not stored_wrong and not silent,
        f"all {len(want)} Settings fields save as typed ({msg!r}; wrong: {stored_wrong[:3]}; "
        f"not saved: {silent[:3]})",
    )
    # the form after a reload shows what was stored
    page.goto(base + "/")
    page.wait_for_timeout(2500)
    open_all(page)
    shown_wrong = []
    for k, v in want.items():
        el = page.locator(f"#cfgform [name='{k}']")
        kind = spec[k]["kind"]
        cur = el.evaluate("e=>e.type==='checkbox'?e.checked:e.value")
        if kind == "secret":
            ok = cur == ""  # never sent back to the page
        elif kind == "bool":
            ok = cur is v
        elif kind in ("int", "float", "tempF"):
            ok = math.isclose(float(cur or "nan"), float(v), abs_tol=0.051)
        else:
            ok = str(cur) == str(v)
        if not ok:
            shown_wrong.append(f"{k}: stored {v!r}, form shows {cur!r}")
    check(
        not shown_wrong,
        f"after a reload the form shows every stored value, and never the "
        f"plug password ({shown_wrong[:3]})",
    )


def edit_survives_update(page, base, app):
    """An edited setting stays as typed while new statuses arrive (it used to
    revert seconds later, as soon as the field lost focus)."""
    page.goto(base + "/")
    page.wait_for_timeout(2500)
    open_all(page)
    page.evaluate(
        "()=>{const e=document.querySelector('#cfgform [name=dim_below_min]');"
        "e.value=e.value==='hold'?'cycle':'hold';e.dispatchEvent(new Event('change',{bubbles:true}))}"
    )
    typed = page.evaluate("()=>document.querySelector('#cfgform [name=dim_below_min]').value")
    for _ in range(3):  # each change through the API pushes a new status
        page.evaluate(
            "()=>fetch('/api/fan',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({mode:'auto'})})"
        )
        page.wait_for_timeout(1200)
    now = page.evaluate("()=>document.querySelector('#cfgform [name=dim_below_min]').value")
    check(
        now == typed and "Unsaved" in page.inner_text("#msg"),
        f"an unsaved edit is kept while new statuses arrive ({typed!r} -> {now!r})",
    )


def rejected_field(page, base, app):
    page.goto(base + "/")
    page.wait_for_timeout(2500)
    open_all(page)
    page.evaluate(
        "()=>{const e=document.querySelector('#cfgform [name=video_fps]');"
        "e.value='500';e.dispatchEvent(new Event('input',{bubbles:true}))}"
    )
    page.evaluate(
        "()=>document.querySelectorAll('#cfgform details.fgroup').forEach(d=>d.open=false)"
    )
    page.locator("#cfgform button[type=submit]").click()
    page.wait_for_timeout(2000)
    msg = page.inner_text("#msg")
    opened = page.evaluate(
        "()=>document.querySelector('#cfgform [name=video_fps]').closest('details.fgroup').open"
    )
    marked = page.evaluate(
        "()=>document.querySelector('#cfgform [name=video_fps]').getAttribute('aria-invalid')"
    )
    check(
        "Video speed" in msg and opened and marked == "true" and saved(app).get("video_fps") != 500,
        f"a bad value in a closed section is refused, named by its label, its section opened "
        f"and the field outlined ({msg[:60]!r})",
    )


def metric_temperature(page, base, app):
    page.goto(base + "/")
    page.wait_for_timeout(2500)
    open_all(page)
    page.evaluate(
        "()=>{const u=document.querySelector('#cfgform [name=units]');u.value='metric';"
        "u.dispatchEvent(new Event('change',{bubbles:true}))}"
    )
    page.locator("#cfgform button[type=submit]").click()
    page.wait_for_timeout(2000)
    page.goto(base + "/")
    page.wait_for_timeout(2500)
    open_all(page)
    page.evaluate(
        "()=>{const t=document.querySelector('#cfgform [name=heat_target_f]');t.value='30';"
        "t.dispatchEvent(new Event('input',{bubbles:true}))}"
    )
    page.locator("#cfgform button[type=submit]").click()
    page.wait_for_timeout(2000)
    got = saved(app).get("heat_target_f")
    check(
        got is not None and abs(float(got) - 86.0) < 0.06,
        f"with metric units a 30 C heat mat target is stored as 86 F ({got})",
    )
    page.evaluate(
        "()=>{const u=document.querySelector('#cfgform [name=units]');u.value='imperial';"
        "u.dispatchEvent(new Event('change',{bubbles:true}))}"
    )
    page.locator("#cfgform button[type=submit]").click()
    page.wait_for_timeout(1500)


def setups_by_main_save(page, base, app):
    page.goto(base + "/")
    page.wait_for_timeout(2500)
    open_all(page)
    page.locator('#setupcfg fieldset[data-i="1"] [data-f="dli_low"]').fill("9")
    page.locator('#setupcfg fieldset[data-i="1"] [data-f="dli_high"]').fill("13")
    page.locator("#cfgform button[type=submit]").click()
    page.wait_for_timeout(2000)
    s = [x for x in saved(app)["setups"] if x["name"] == "Seedlings"][0]
    check(
        s["dli_low"] == 9 and s["dli_high"] == 13,
        "a Setups edit is saved by the main Save button too",
    )


def remove_asks(page, base, app):
    """Removing a setup or a tray asks first; cancelling changes nothing, and
    a tray with plants says how many it would discard, in one question."""
    page.goto(base + "/")
    page.wait_for_timeout(2500)
    open_all(page)
    asked = []

    def answer(d, ok):
        asked.append(d.message)
        d.accept() if ok else d.dismiss()

    page.once("dialog", lambda d: answer(d, False))
    page.locator('#setupcfg fieldset[data-i="0"] .setuprm').click()
    page.wait_for_timeout(500)
    kept = page.locator("#setupcfg fieldset[data-i]").count()
    page.once("dialog", lambda d: answer(d, False))
    page.locator('.trayrow[data-tray="3"] .trm').click()
    page.wait_for_timeout(1000)
    tray_kept = "3" in saved(app)["trays"]
    page.once("dialog", lambda d: answer(d, True))
    page.locator('.trayrow[data-tray="3"] .trm').click()
    page.wait_for_timeout(2000)
    gone = "3" not in saved(app)["trays"]
    check(
        len(asked) == 3
        and kept == 2
        and tray_kept
        and gone
        and 'Remove the setup "Transplants"' in asked[0]
        and "20 planted cells" in asked[1],
        f"Remove asks first and Cancel keeps it; a planted tray says what it discards "
        f"({[a.split(chr(10))[0] for a in asked]})",
    )
    opts = page.evaluate(
        "()=>[...document.querySelectorAll('#cfgform [name=plug_use] option')].map(o=>o.textContent)"
    )
    check(opts == ["Light", "Heat Mat"], f"the smart plug's uses read Light and Heat Mat ({opts})")


def charts(page, base):
    page.goto(base + "/")
    page.wait_for_timeout(3500)
    page.locator("#setuptabs button", has_text="Seedlings").click()  # the heat mat's tab
    page.wait_for_timeout(1500)
    page.locator("#chartgrid").scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    sections = page.evaluate("""()=>Object.fromEntries([...document.querySelectorAll('#chartgrid .csection')]
        .map(s=>[s.querySelector('h3').textContent,[...s.querySelectorAll('.ccard')].map(c=>c.textContent.slice(0,40))]))""")
    dev = page.evaluate("()=>document.getElementById('devcharts')?.textContent||''")
    check(
        "Heat mat power" in json.dumps(sections.get("Soil", []))
        and "Device" not in sections
        and "Memory free" in dev,
        f"Heat mat power charts with Soil; memory charts sit in the Device card, not the "
        f"chart grid ({list(sections)})",
    )
    sized = page.evaluate("""()=>[...document.querySelectorAll('svg.cmini')].every(s=>{
        const r=s.getBoundingClientRect(),vb=s.getAttribute('viewBox').split(' ').map(Number);
        return Math.abs(vb[2]-r.width)<=1&&Math.abs(vb[3]-r.height)<=1;})""")
    check(sized, "every chart is drawn at its real size (no stretched text)")
    rows = page.evaluate("""()=>[...document.querySelectorAll('#chartgrid .cgrid')].map(g=>{
        const rows={};[...g.children].forEach(c=>{const r=c.getBoundingClientRect();
          (rows[Math.round(r.top)]=rows[Math.round(r.top)]||[]).push(Math.round(r.width));});
        return {w:g.clientWidth, rows:Object.values(rows)};})""")
    even = all(
        max(r) - min(r) <= 2 and abs(sum(r) + 10 * (len(r) - 1) - g["w"]) <= 4
        for g in rows
        for r in g["rows"]
    )
    check(even, f"each chart row fills the width with equal charts ({[g['rows'] for g in rows]})")
    first = page.locator("svg.cmini").first
    bb = first.bounding_box()
    page.mouse.move(bb["x"] + bb["width"] * 0.6, bb["y"] + bb["height"] * 0.5)
    page.wait_for_timeout(300)
    shown = page.evaluate(
        "()=>[...document.querySelectorAll('svg.cmini .hvl')].filter(e=>e.style.display!=='none').length"
    )
    total = page.evaluate("()=>document.querySelectorAll('svg.cmini').length")
    check(
        shown == total and total > 2,
        f"hovering one chart shows the same moment on all ({shown} of {total})",
    )


def buddies(pw, base):
    """Little Buddy's colors hold in both themes: each sprite's own leg colour
    (not the shared leaf green), dark faces on the dark theme, a seven-spot
    ladybug."""
    b = pw.chromium.launch()
    ctx = b.new_context(
        viewport={"width": 900, "height": 600}, color_scheme="dark", reduced_motion="reduce"
    )
    page = ctx.new_page()
    page.goto(base + "/")
    page.wait_for_timeout(2500)
    got = page.evaluate("""async ()=>{
        const m = await import('/static/js/buddy.js');
        const box = document.createElement('div');
        box.innerHTML = ['cat','snail','ladybug','sprout'].map(k=>m.walkerSvg(k)).join('');
        document.body.appendChild(box);
        const cs = (sel, prop) => getComputedStyle(box.querySelector(sel))[prop];
        return {catLeg: cs('.buddy-cat .legs .cleg:not(.cleg-far)', 'stroke'),
                stalk: cs('.buddy-snail .legs .stalk', 'stroke'),
                eye: cs('.buddy-sprout .eye', 'fill'),
                spots: box.querySelectorAll('.buddy-ladybug .spot').length};
    }""")
    check(
        got["catLeg"] == "rgb(154, 168, 162)"
        and got["stalk"] == "rgb(216, 196, 156)"
        and got["eye"] == "rgb(38, 48, 31)"
        and got["spots"] == 8,
        f"buddies keep their own leg colours, dark faces on the dark theme, and the "
        f"ladybug has seven spots (one split across the halves) ({got})",
    )
    b.close()


def phone(pw, base):
    b = pw.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(base + "/")
    page.wait_for_timeout(3000)
    w = page.evaluate("()=>document.documentElement.scrollWidth")
    check(
        w <= 390 and not errs,
        f"on a phone the page is no wider than the screen ({w} px; errors {errs[:1]})",
    )
    b.close()


def screen(pw, base, app):
    b = pw.chromium.launch()
    ctx = b.new_context(viewport={"width": 600, "height": 1024}, has_touch=True)
    page = ctx.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(base + "/screen")
    page.wait_for_timeout(2500)
    h = page.evaluate("()=>document.documentElement.scrollHeight")
    check(
        h <= 1024 and not errs, f"the touchscreen summary fits 600x1024 without scrolling ({h} px)"
    )
    page.click('.seg[data-ctl="fan"] button[data-v="off"]')
    page.wait_for_timeout(1500)
    check(
        saved(app).get("fan_mode") == "off" and "Off" in page.inner_text("#fan"),
        "a tap on the summary's fan Off is saved and shown",
    )
    page.click('.seg[data-ctl="fan"] button[data-v="auto"]')
    page.wait_for_timeout(800)
    page.click("a.btn")
    page.wait_for_timeout(2500)
    check(
        page.locator(".kioskback").count() == 1,
        "the summary's Full dashboard has a way back to the summary",
    )
    b.close()


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        skip(
            "every UI test (Playwright is not installed: pip install playwright; "
            "playwright install chromium)"
        )
        print(f"\n{results['pass']} passed, {results['fail']} failed, {results['skip']} skipped")
        return 0
    work, app = make_app()
    port = free_port()
    proc, base = start(app, port)
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            page = b.new_context(viewport={"width": 1400, "height": 1000}).new_page()
            errs = []
            page.on("pageerror", lambda e: errs.append(str(e)))
            for name, fn in (
                ("Settings", lambda: settings_round_trip(page, base, app)),
                ("Unsaved edits", lambda: edit_survives_update(page, base, app)),
                ("Refused values", lambda: rejected_field(page, base, app)),
                ("Units", lambda: metric_temperature(page, base, app)),
                ("Setups", lambda: setups_by_main_save(page, base, app)),
                ("Charts", lambda: charts(page, base)),
                ("Removing", lambda: remove_asks(page, base, app)),
            ):
                print(name)
                try:
                    fn()
                except Exception as e:
                    check(False, f"{name}: crashed with {type(e).__name__}: {e}")
            check(not errs, f"no script errors on the dashboard ({errs[:2]})")
            b.close()
            print("Little Buddy")
            buddies(pw, base)
            print("Phone")
            phone(pw, base)
            print("Touchscreen")
            screen(pw, base, app)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{results['pass']} passed, {results['fail']} failed, {results['skip']} skipped")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
