#!/usr/bin/env python3
"""
Hardware test: ramp the grow light 0% -> 100% -> 0% over 30 seconds.

Run it with the service stopped so the two don't fight over the PWM channel:
  sudo systemctl stop growlight
  ~/growlight/venv/bin/python ~/growlight/scripts/test_ramp.py
  sudo systemctl start growlight
"""

import subprocess
import sys
import time

# refuse to run alongside the service
if subprocess.run(["systemctl", "is-active", "--quiet", "growlight"]).returncode == 0:
    sys.exit("growlight service is running. Stop it first:\n  sudo systemctl stop growlight")

from rpi_hardware_pwm import HardwarePWM

PWM_FREQ = 1000
HALF_SECS = 15  # 15 up + 15 down = 30 total
STEP_SECS = 0.1

try:
    pwm = HardwarePWM(pwm_channel=0, hz=PWM_FREQ, chip=0)
    pwm.start(0)
except Exception as e:
    sys.exit(
        f"Hardware PWM unavailable ({e}). Is the dtoverlay in place, "
        f"and did you reboot after setup?"
    )

steps = int(HALF_SECS / STEP_SECS)


def show(pct):
    bar = "#" * int(pct / 2)
    print(f"\r{pct:5.1f}% |{bar:<50}|", end="", flush=True)


print("Ramping up (15s)...")
try:
    for i in range(steps + 1):
        pct = 100.0 * i / steps
        pwm.change_duty_cycle(pct)
        show(pct)
        time.sleep(STEP_SECS)
    print("\nRamping down (15s)...")
    for i in range(steps + 1):
        pct = 100.0 * (steps - i) / steps
        pwm.change_duty_cycle(pct)
        show(pct)
        time.sleep(STEP_SECS)
    print("\nDone. Light off.")
except KeyboardInterrupt:
    print("\nInterrupted. Light off.")
finally:
    pwm.change_duty_cycle(0)
    pwm.stop()
