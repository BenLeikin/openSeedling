"""pytest configuration for the server tests. The app itself is set up once,
in harness.py; the fixture here keeps one test's changes away from the next."""

import copy
import logging
import os
import threading
import time

import pytest
from harness import (
    APP,
    alerts,
    camera_mod,
    config,
    control,
    db,
    hardware,
    light_mod,
    monitor,
    water,
)

# worker threads the app leaves running can log after pytest has closed the
# stream it captured; that is not an error worth a traceback at exit
logging.raiseExceptions = False

# a standalone script, not a pytest module: the browser tests (Playwright)
collect_ignore = ["test_ui.py"]


@pytest.fixture(autouse=True, scope="session")
def in_the_app_directory():
    """Run the tests from the app's temp copy, as the service runs from its
    own directory. Done here, not when harness.py is imported: pytest is still
    working out what to collect then, relative to where it was started."""
    started_in = os.getcwd()
    os.chdir(APP)
    yield
    os.chdir(started_in)


@pytest.fixture(autouse=True)
def clean_slate():
    """Give every test the same starting point, and put it back afterwards.

    The app is one process full of shared state (settings, the schedule's
    published state, the readings database, pump and alert state), and tests
    change it directly. Each test starts with an empty readings history and
    gets the settings it found restored when it ends, so no test depends on
    which ones ran before it.
    """
    with config.settings_lock:
        settings = copy.deepcopy(config.settings)
    with config.state_lock:
        state = dict(config.state)
    with db._lock:
        for table in ("readings", "readings_hourly", "events"):
            db._c().execute(f"DELETE FROM {table}")
        db._c().commit()
    monitor._health_cache["ts"] = 0
    _empty_photo_folder()
    yield
    _let_image_jobs_finish()
    with config.settings_lock:
        config.settings.clear()
        config.settings.update(settings)
    with config.state_lock:
        config.state.clear()
        config.state.update(state)
    for st in hardware.pump_state.values():
        st.update(running=False, reserved=0.0)
    water.fill_failures.clear()
    alerts.reset()
    light_mod.release(light_mod.holder())
    hardware.SHUTTING_DOWN.clear()


def _let_image_jobs_finish():
    """A thumbnail rebuild or a render started by a test runs in a thread of
    its own; wait for it, so it cannot touch the next test's photos."""
    if camera_mod._thumbs_lock.acquire(timeout=30):
        camera_mod._thumbs_lock.release()
    deadline = time.time() + 30
    while camera_mod.render.get("state") == "running" and time.time() < deadline:
        time.sleep(0.05)


def _empty_photo_folder():
    for folder in (camera_mod.TIMELAPSE_DIR, camera_mod.THUMB_DIR):
        for p in folder.glob("*"):
            if p.is_file():
                p.unlink(missing_ok=True)
    for cache in (camera_mod._rect_cache, camera_mod._crop_cache):
        cache.update(key=None, bytes=None)


@pytest.fixture
def control_loop():
    """The real control loop, passing every 0.1 s, for the length of one test."""
    stop = threading.Event()
    pace, control.LOOP_SECONDS = control.LOOP_SECONDS, 0.1
    thread = threading.Thread(target=control.control_loop, args=(stop,), daemon=True)
    thread.start()
    yield thread
    stop.set()
    config.wake.set()
    thread.join(5)
    control.LOOP_SECONDS = pace
