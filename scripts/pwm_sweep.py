#!/usr/bin/env python3
"""Sweep the light PWM so you can watch the converter output on a meter.

Run this with the growlight service STOPPED: both drive the same PWM channel,
and two owners fighting over one channel produces readings that make no sense.

    sudo systemctl stop growlight
    ~/growlight/venv/bin/python pwm_sweep.py            # step through, pausing
    ~/growlight/venv/bin/python pwm_sweep.py --hold 50  # park at one duty
    ~/growlight/venv/bin/python pwm_sweep.py --auto     # continuous ramp
    sudo systemctl start growlight

Expected on VOUT, measured against the converter's GND:
    0%   ->  0.0V     the light should be dark
    50%  ->  ~5.0V
    100% -> ~10.0V    full brightness

If the top reads about 5V instead of 10V, the board's range pads are bridged
for the 0-5V setting. If it barely moves, the supply is not 12V or the PWM pin
is wrong.
"""
import argparse
import sys
import time

# Same defaults the app uses, so a sweep here matches what the service will do.
DEFAULT_PIN = 19        # BCM; physical pin 35, hardware PWM channel 1
DEFAULT_HZ = 1000


def channel_for(pin):
    """GPIO18 and GPIO12 are channel 0; GPIO19 and GPIO13 are channel 1."""
    if pin in (18, 12):
        return 0
    if pin in (19, 13):
        return 1
    raise SystemExit(f"GPIO{pin} is not a hardware PWM pin (use 12, 13, 18 or 19)")


def main():
    ap = argparse.ArgumentParser(description="Sweep the light PWM for testing")
    ap.add_argument("--pin", type=int, default=DEFAULT_PIN,
                    help=f"BCM pin (default {DEFAULT_PIN}, physical 35)")
    ap.add_argument("--hz", type=int, default=DEFAULT_HZ)
    ap.add_argument("--step", type=int, default=10, help="percent per step")
    ap.add_argument("--hold", type=float, default=None,
                    help="hold one duty percent until interrupted")
    ap.add_argument("--auto", action="store_true",
                    help="ramp up and down continuously instead of pausing")
    ap.add_argument("--dwell", type=float, default=1.5,
                    help="seconds per step in --auto mode")
    args = ap.parse_args()

    try:
        from rpi_hardware_pwm import HardwarePWM
    except ImportError:
        raise SystemExit("rpi-hardware-pwm is not installed in this interpreter; "
                         "run it with ~/growlight/venv/bin/python")

    ch = channel_for(args.pin)
    try:
        pwm = HardwarePWM(pwm_channel=ch, hz=args.hz, chip=0)
    except Exception as e:
        raise SystemExit(
            f"could not open PWM channel {ch} for GPIO{args.pin}: {e}\n"
            "Check that the overlay is enabled in /boot/firmware/config.txt:\n"
            "  dtoverlay=pwm-2chan,pin=18,func=2,pin2=19,func2=2\n"
            "and that the growlight service is stopped.")

    print(f"GPIO{args.pin} (physical {'12' if ch == 0 else '35'}), "
          f"PWM channel {ch} at {args.hz}Hz")
    print("Measure VOUT against the converter's GND.\n")
    pwm.start(0)
    try:
        if args.hold is not None:
            duty = max(0.0, min(100.0, args.hold))
            pwm.change_duty_cycle(duty)
            print(f"holding {duty:.0f}% -- expect about {duty/10:.1f}V. "
                  "Ctrl-C to stop.")
            while True:
                time.sleep(1)

        steps = list(range(0, 101, max(1, args.step)))
        if 100 not in steps:
            steps.append(100)

        if args.auto:
            print("ramping continuously, Ctrl-C to stop\n")
            while True:
                for d in steps + steps[::-1][1:]:
                    pwm.change_duty_cycle(d)
                    print(f"  {d:3d}%  expect ~{d/10:4.1f}V", flush=True)
                    time.sleep(args.dwell)
        else:
            print("press Enter to advance, Ctrl-C to stop\n")
            for d in steps:
                pwm.change_duty_cycle(d)
                try:
                    input(f"  {d:3d}%  expect ~{d/10:4.1f}V   [Enter] ")
                except EOFError:
                    time.sleep(args.dwell)
            print("\nsweep complete")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        # leave the light off rather than wherever the sweep happened to end
        try:
            pwm.change_duty_cycle(0)
            pwm.stop()
        except Exception:
            pass
        print("PWM set to 0 and released. "
              "Start the service again: sudo systemctl start growlight")


if __name__ == "__main__":
    main()
