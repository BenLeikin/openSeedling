"""Tests: the app as it comes up, and a check on the test files themselves."""

import ast
import re
from pathlib import Path

from harness import (
    APP,
    AT_IMPORT,
    ai_report,
    app_source,
    c,
    camera_mod,
    config,
    db,
    growlight,
    hardware,
    heat,
    light_mod,
    monitor,
    page_js,
    photos_mod,
    routes,
    setups_mod,
    squash,
    status_mod,
    tools,
    video_mod,
    water,
)


def test_every_python_file_parses():
    bad = []
    for p in sorted(APP.rglob("*.py")):
        try:
            ast.parse(p.read_text(), str(p))
        except SyntaxError as e:
            bad.append(f"{p.relative_to(APP)}:{e.lineno}")
    assert not bad, f"every Python file parses {bad}"


def test_app_imports_into_the_temp_copy():
    assert Path(db.DB_PATH).parent == APP, "database lives in the temp copy, not the repo"
    assert c.get("/api/status").status_code == 200, "app imports and /api/status answers"


def test_old_config_is_migrated():
    """harness.py writes a config.json in an old format before importing the app."""
    assert AT_IMPORT["auto_water_trays"] == ["1", "2"], (
        "an old config with auto-water on arms every pump tray once"
    )
    assert AT_IMPORT["ai_model"] == config.DEFAULTS["ai_model"] != "claude-opus-4-8", (
        f"a stored former-default AI model is moved to the current one ({AT_IMPORT['ai_model']})"
    )


def test_dashboard_posts_send_json():
    js = page_js()
    posts = re.findall(
        r"request\((['\"`][^'\"`]+['\"`]),\{method:'POST'(.{0,160})", squash(js), re.S
    )
    no_json = [u for u, rest in posts if "application/json" not in rest]
    assert posts and not no_json, f"every dashboard POST sends JSON ({len(posts)} calls) {no_json}"


def test_login_and_logout_reopen_the_stream():
    js = squash(page_js())
    assert re.search(r"asyncfunctiondoLogin[\s\S]{0,600}restartStream\(\)", js) and re.search(
        r"asyncfunctiondoLogout[\s\S]{0,400}restartStream\(\)", js
    ), "login and logout reopen the live stream"


def test_timelapse_is_swapped_in_not_rewritten():
    src = app_source()
    assert "str(VIDEO_PATH)]" not in src and "os.replace(part, VIDEO_PATH)" in src, (
        "timelapse is written beside the old video and swapped in, never rewritten in place"
    )


def test_ai_prompt_does_not_assume_the_lights_colour():
    assert "magenta/pink LED" not in ai_report.PROMPT and "tint" in ai_report.PROMPT


def test_patched_names_live_in_one_module():
    """Tests swap functions and constants for fakes on the module that owns
    them. That only works if nothing else holds its own copy of the name (say
    a "from light import set_brightness"), or the fake is never called and the
    test passes without testing anything."""
    mods = {
        "config": config,
        "photos_mod": photos_mod,
        "video_mod": video_mod,
        "tools": tools,
        "hardware": hardware,
        "light_mod": light_mod,
        "setups_mod": setups_mod,
        "water": water,
        "monitor": monitor,
        "camera_mod": camera_mod,
        "status_mod": status_mod,
        "routes": routes,
        "heat": heat,
    }
    patched = set()
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                for el in target.elts if isinstance(target, ast.Tuple) else [target]:
                    if (
                        isinstance(el, ast.Attribute)
                        and isinstance(el.value, ast.Name)
                        and el.value.id in mods
                    ):
                        patched.add((el.value.id, el.attr))
    copies = [
        f"{a}.{n} also in {o}"
        for a, n in sorted(patched)
        for o, m in list(mods.items()) + [("growlight", growlight)]
        if o != a and n in vars(m)
    ]
    unowned = [f"{a}.{n}" for a, n in sorted(patched) if n not in vars(mods[a])]
    assert len(patched) >= 10 and not copies and not unowned, (
        f"every name the tests patch ({len(patched)}) lives in one module only {copies + unowned}"
    )


def test_journal_lines_carry_their_level():
    """Under systemd every line starts with its syslog priority, tracebacks
    included, so `journalctl -p warning` filters; elsewhere the text is plain."""
    import logging

    import applog

    def record(level, exc_info=None):
        return logging.LogRecord("growlight", level, __file__, 1, "plug gone", None, exc_info)

    journal = applog._JournalFormatter(False, journal=True)
    assert journal.format(record(logging.WARNING)).startswith("<4>WARNING")
    assert journal.format(record(logging.INFO)).startswith("<6>INFO")
    try:
        raise OSError("no route")
    except OSError:
        import sys

        lines = journal.format(record(logging.ERROR, sys.exc_info())).split("\n")
    assert len(lines) > 2 and all(line.startswith("<3>") for line in lines)
    assert applog._JournalFormatter(False).format(record(logging.ERROR)).startswith("ERROR")
