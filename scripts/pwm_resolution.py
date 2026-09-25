#!/usr/bin/env python3
"""Find how finely the light can actually be dimmed.

The PWM has a thousand steps and the app works in whole percent, but neither
tells you what the fixture does. The optocoupler, the driver's dim input and
the LEDs each have their own response, and the only way to know the real
resolution is to set a level and measure the light.

Run with the growlight service STOPPED (both own the same PWM channel):

    sudo systemctl stop growlight
    ~/growlight/venv/bin/python pwm_resolution.py            # coarse sweep
    ~/growlight/venv/bin/python pwm_resolution.py --fine 40  # 0.1% steps at 40
    ~/growlight/venv/bin/python pwm_resolution.py --find-off # locate cutoff
    sudo systemctl start growlight

Readings come from the BH1750 if it is present. Without it the script still
sets each level and pauses so you can read a meter or judge by eye.

Duty is PWM duty cycle, not dashboard brightness. On the optocoupler wiring
they are inverted: 0% duty is full brightness, 100% duty is dark.
"""
import argparse
import statistics
import sys
import time

DEFAULT_PIN = 19        # BCM; physical 35, hardware PWM channel 1
DEFAULT_HZ = 1000
SETTLE = 0.6            # seconds for the RC filter and the driver to follow
SAMPLES = 5             # lux readings averaged per step


def channel_for(pin):
    if pin in (18, 12):
        return 0
    if pin in (19, 13):
        return 1
    raise SystemExit(f"GPIO{pin} is not a hardware PWM pin (12, 13, 18 or 19)")


def get_lux_reader():
    """A function returning lux, or None when no sensor is reachable."""
    try:
        sys.path.insert(0, "/home/ben/growlight")
        import sensors
        probe = sensors._read_lux()
        if "lux" in probe:
            def read():
                vals = []
                for _ in range(SAMPLES):
                    r = sensors._read_lux()
                    if "lux" in r:
                        vals.append(r["lux"])
                    time.sleep(0.08)
                return statistics.median(vals) if vals else None
            return read
    except Exception as e:
        print(f"(no lux sensor: {e})")
    return None


def main():
    ap = argparse.ArgumentParser(
        description="Measure the real dimming resolution of the fixture")
    ap.add_argument("--pin", type=int, default=DEFAULT_PIN)
    ap.add_argument("--hz", type=int, default=DEFAULT_HZ)
    ap.add_argument("--step", type=float, default=5.0,
                    help="duty step for the coarse sweep (default 5)")
    ap.add_argument("--fine", type=float, default=None, metavar="DUTY",
                    help="sweep +/-2%% around this duty in 0.1%% steps")
    ap.add_argument("--find-off", action="store_true",
                    help="binary search for the duty where the light goes dark")
    ap.add_argument("--settle", type=float, default=SETTLE)
    args = ap.parse_args()

    try:
        from rpi_hardware_pwm import HardwarePWM
    except ImportError:
        raise SystemExit("run this with ~/growlight/venv/bin/python")

    ch = channel_for(args.pin)
    try:
        pwm = HardwarePWM(pwm_channel=ch, hz=args.hz, chip=0)
    except Exception as e:
        raise SystemExit(f"could not open PWM channel {ch}: {e}\n"
                         "Is the growlight service stopped?")

    read_lux = get_lux_reader()
    pwm.start(100)          # start dark on inverted wiring
    time.sleep(args.settle)

    def measure(duty):
        pwm.change_duty_cycle(duty)
        time.sleep(args.settle)
        return read_lux() if read_lux else None

    try:
        if args.find_off:
            # The cutoff is the interesting edge: below it the driver is off,
            # above it the light is live but possibly barely.
            if not read_lux:
                raise SystemExit("--find-off needs the lux sensor")
            lit = measure(0.0)
            print(f"full brightness at 0% duty: {lit:.0f} lx")
            floor = max(1.0, lit * 0.01)
            lo, hi = 0.0, 100.0           # lo is lit, hi is dark
            for _ in range(12):
                mid = (lo + hi) / 2
                v = measure(mid)
                state = "lit" if v and v > floor else "dark"
                print(f"  {mid:6.2f}% duty -> {v:8.1f} lx  {state}")
                if v and v > floor:
                    lo = mid
                else:
                    hi = mid
            print(f"\ncutoff between {lo:.2f}% and {hi:.2f}% duty "
                  f"({100-hi:.2f}% to {100-lo:.2f}% brightness)")
            return

        if args.fine is not None:
            centre = max(0.0, min(100.0, args.fine))
            lo = max(0.0, centre - 2.0)
            hi = min(100.0, centre + 2.0)
            print(f"0.1% steps from {lo:.1f}% to {hi:.1f}% duty\n")
            prev = None
            deltas = []
            d = lo
            while d <= hi + 1e-9:
                v = measure(d)
                if v is None:
                    input(f"  {d:6.2f}% duty   [Enter]")
                else:
                    ch_txt = ""
                    if prev is not None:
                        delta = v - prev
                        deltas.append(abs(delta))
                        ch_txt = f"  ({delta:+7.1f} lx)"
                    print(f"  {d:6.2f}% duty -> {v:8.1f} lx{ch_txt}")
                    prev = v
                d = round(d + 0.1, 2)
            if deltas:
                med = statistics.median(deltas)
                print(f"\nmedian change per 0.1% duty: {med:.1f} lx")
                print("A 0.1% step is meaningful here" if med > 1 else
                      "0.1% steps are below the noise; whole percent is the "
                      "practical limit")
            return

        # coarse sweep across the whole range
        print("duty    brightness      lux      change")
        results = []
        d = 0.0
        while d <= 100.0 + 1e-9:
            v = measure(d)
            if v is None:
                input(f"{d:6.1f}%  {100-d:6.1f}%      [Enter]")
            else:
                delta = "" if not results else f"{v - results[-1][1]:+9.1f}"
                print(f"{d:6.1f}%  {100-d:6.1f}%  {v:9.1f}  {delta}")
                results.append((d, v))
            d = round(d + args.step, 2)

        if len(results) > 2:
            lit = [r for r in results if r[1] > 1]
            print(f"\n{len(lit)} of {len(results)} steps produced light")
            if lit:
                top = max(r[1] for r in lit)
                print(f"brightest {top:.0f} lx at {100-lit[0][0]:.0f}% brightness")
                print(f"light present from {100-lit[-1][0]:.0f}% to "
                      f"{100-lit[0][0]:.0f}% brightness")
                print("\nRun --fine at an interesting duty to see 0.1% steps, "
                      "or --find-off to locate the cutoff precisely.")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        try:
            pwm.change_duty_cycle(100)      # dark
            pwm.stop()
        except Exception:
            pass
        print("\nPWM released. sudo systemctl start growlight")


if __name__ == "__main__":
    main()
