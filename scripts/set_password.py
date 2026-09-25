#!/usr/bin/env python3
"""Set, change, or disable the dashboard login password.

Run with the project's venv python from the project root:

    ./venv/bin/python scripts/set_password.py

Prompts for the password with hidden input (no echo, never stored in shell
history), hashes it with scrypt, and writes it into config.json. Doing the
write in Python avoids the copy/paste corruption that mangles the '$' in the
hash when editing config.json by hand.

An empty password removes the login, making the dashboard open again.
Restart the service afterwards so the running app picks up the change:
    sudo systemctl restart growlight
"""

import getpass
import json
import os
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

# config.json lives in the project root, one level up from scripts/
CONFIG = Path(__file__).resolve().parent.parent / "config.json"


def load_config():
    if not CONFIG.exists():
        return {}
    try:
        return json.loads(CONFIG.read_text())
    except json.JSONDecodeError as e:
        print(f"config.json is not valid JSON ({e}).")
        print("Fix it before setting a password; refusing to overwrite.")
        sys.exit(1)


def save_config(cfg):
    # atomic write so a crash mid-write can't truncate config.json
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, CONFIG)


def main():
    cfg = load_config()
    pw = getpass.getpass("New dashboard password (leave blank to disable login): ")
    if pw == "":
        cfg["password_hash"] = ""
        save_config(cfg)
        print("Login disabled; the dashboard is now open.")
        print("Restart to apply:  sudo systemctl restart growlight")
        return
    if getpass.getpass("Confirm password: ") != pw:
        print("Passwords do not match. Nothing changed.")
        sys.exit(1)
    cfg["password_hash"] = generate_password_hash(pw)
    save_config(cfg)
    print("Password set.")
    print("Restart to apply:  sudo systemctl restart growlight")


if __name__ == "__main__":
    main()
