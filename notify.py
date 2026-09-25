#!/usr/bin/env python3
"""Push-notification transport for the grow dashboard.

Pure transport: it takes a title and a message and gets it to your phone. It
knows nothing about sensors, thresholds, or grows; that decision logic lives
in alerts.py. Kept dumb on purpose so it's reusable and testable.

Uses ntfy (https://ntfy.sh): install the ntfy app, subscribe to a topic, and
put that topic in config.json as "ntfy_topic". No account or API key needed.
Pick an unguessable topic name -- anyone who knows it can read your alerts.

    import notify
    notify.send("Reservoir low", "Tank under 10%", priority="high", tags="warning")

Config (read lazily from config.json so changes don't need a restart):
    "ntfy_topic": "grow-7f3k9q2x"          # required to enable
    "ntfy_server": "https://ntfy.sh"       # optional, for self-hosted
"""

import json
import urllib.request
from pathlib import Path

from applog import log     # levelled logging; see applog.py

CONFIG_PATH = Path(__file__).with_name("config.json")


def _cfg():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def enabled():
    return bool(_cfg().get("ntfy_topic"))


def send(title, message, priority="default", tags=""):
    """Send a notification. Returns True on success, False otherwise.
    Silently no-ops (returning False) if no topic is configured, so callers
    never have to guard for it."""
    cfg = _cfg()
    topic = cfg.get("ntfy_topic")
    if not topic:
        return False
    server = cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    req = urllib.request.Request(f"{server}/{topic}",
                                 data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception as e:
        log.warning(f"ntfy send failed: {e}")
        return False


if __name__ == "__main__":
    import sys
    msg = sys.argv[1] if len(sys.argv) > 1 else "Test from the grow controller."
    if not enabled():
        print('No "ntfy_topic" in config.json -- nothing to send to.')
        print('Add one (e.g. "ntfy_topic": "grow-7f3k9q2x"), subscribe in the')
        print("ntfy app, and run this again.")
    else:
        ok = send("Grow controller", msg, tags="seedling")
        print("sent" if ok else "failed")
