#!/usr/bin/env python3
"""Profile the fixture's whole dimming curve at the finest resolution it has.

The PWM's real granularity is set by the period, not by the percentages the
app uses: at 1kHz the kernel counts in nanoseconds, so a step of 0.1% is
1000ns and is genuinely distinct. This walks the entire range at that step,
measures each level with the BH1750, and reports where the light actually
changes and where it does not.

Run with the growlight service STOPPED (both own the same PWM channel):

    sudo systemctl stop growlight
    ~/growlight/venv/bin/python pwm_curve.py                 # 0.1% steps
    ~/growlight/venv/bin/python pwm_curve.py --step 0.05     # finer
    ~/growlight/venv/bin/python pwm_curve.py --csv curve.csv # save it
    sudo systemctl start growlight

A full 0.1% sweep is 1001 points. At the default settle that is roughly 12
minutes, so it starts at the dark end and prints as it goes: you can stop it
early with Ctrl-C and still keep what it measured.

Duty is PWM duty cycle. On the optocoupler wiring 0% duty is full brightness
and 100% is dark, so the sweep runs from 100 down to 0 to go dark to bright.
"""
import argparse
import csv
import statistics
import sys
import time

DEFAULT_PIN = 19
DEFAULT_HZ = 1000


def channel_for(pin):
    if pin in (18, 12):
        return 0
    if pin in (19, 13):
        return 1
    raise SystemExit(f"GPIO{pin} is not a hardware PWM pin (12, 13, 18 or 19)")


def get_lux_reader(samples, app_dir):
    try:
        sys.path.insert(0, app_dir)
        import sensors
        if "lux" not in sensors._read_lux():
            return None

        def read():
            vals = []
            for _ in range(samples):
                r = sensors._read_lux()
                if "lux" in r:
                    vals.append(r["lux"])
                time.sleep(0.05)
            return statistics.median(vals) if vals else None
        return read
    except Exception as e:
        print(f"lux sensor unavailable: {e}")
        return None


def summarize(rows, step):
    """rows: [(duty, brightness, lux)] measured dark-to-bright."""
    lit = [r for r in rows if r[2] is not None and r[2] > 1]
    print("\n--- curve ---")
    if not lit:
        print("nothing registered as lit; check the wiring and the sensor")
        return
    top = max(r[2] for r in rows if r[2] is not None)
    cutoff = lit[0]
    print(f"peak            {top:.0f} lx at {cutoff[1]:.1f}%..100% brightness")
    print(f"first light at  {cutoff[1]:.2f}% brightness ({cutoff[0]:.2f}% duty)")
    print(f"usable range    {cutoff[1]:.1f}% to 100% brightness "
          f"({100 - cutoff[1]:.1f} points of the scale)")

    # how many steps actually changed the reading, and by how much
    deltas = []
    for a, b in zip(lit, lit[1:]):
        if a[2] is not None and b[2] is not None:
            deltas.append(abs(b[2] - a[2]))
    if deltas:
        moved = sum(1 for d in deltas if d > max(1.0, top * 0.0005))
        print(f"\nof {len(deltas)} steps of {step}% inside the lit range, "
              f"{moved} changed the reading")
        print(f"median change per step   {statistics.median(deltas):.1f} lx")
        print(f"largest single step      {max(deltas):.1f} lx")
        if moved < len(deltas) * 0.5:
            print(f"\nMore than half the {step}% steps did nothing measurable. "
                  f"The practical resolution is coarser than {step}%.")
        else:
            print(f"\nMost {step}% steps moved the light, so the fixture "
                  f"resolves at least that finely.")

    # where the curve is steep: those regions need finer control in software
    if len(lit) > 20:
        chunk = max(1, len(lit) // 10)
        print("\nsteepness by tenth of the lit range (lx change per step):")
        for i in range(0, len(lit) - 1, chunk):
            part = lit[i:i + chunk + 1]
            ds = [abs(b[2] - a[2]) for a, b in zip(part, part[1:])
                  if a[2] is not None and b[2] is not None]
            if ds:
                print(f"  {part[0][1]:5.1f}% to {part[-1][1]:5.1f}% brightness: "
                      f"{statistics.mean(ds):7.1f}")


def main():
    ap = argparse.ArgumentParser(
        description="Sweep dark to full at the finest usable resolution")
    ap.add_argument("--pin", type=int, default=DEFAULT_PIN)
    ap.add_argument("--hz", type=int, default=DEFAULT_HZ)
    ap.add_argument("--step", type=float, default=0.1,
                    help="duty step in percent (default 0.1)")
    ap.add_argument("--settle", type=float, default=0.35,
                    help="seconds to wait after each change (default 0.35)")
    ap.add_argument("--samples", type=int, default=3,
                    help="lux readings median-averaged per point")
    ap.add_argument("--csv", default=None, help="write the curve to this file")
    ap.add_argument("--app", default="/home/ben/growlight",
                    help="app directory, for the sensors module")
    ap.add_argument("--quiet", action="store_true",
                    help="only print points where the reading changed")
    args = ap.parse_args()

    if args.step <= 0:
        raise SystemExit("--step must be positive")

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

    read_lux = get_lux_reader(args.samples, args.app)
    if read_lux is None:
        raise SystemExit("this needs the BH1750: without a light reading there "
                         "is nothing to measure. Use pwm_sweep.py to step by "
                         "hand instead.")

    points = int(round(100.0 / args.step)) + 1
    est = points * (args.settle + args.samples * 0.05) / 60.0
    print(f"{points} points at {args.step}% steps, roughly {est:.0f} minutes.")
    print("Dark to full. Ctrl-C stops early and keeps what was measured.\n")
    print("  duty   bright        lux")

    pwm.start(100)
    time.sleep(1.0)
    rows = []
    last = None
    try:
        for i in range(points):
            duty = round(100.0 - i * args.step, 4)
            if duty < 0:
                duty = 0.0
            pwm.change_duty_cycle(duty)
            time.sleep(args.settle)
            lux = read_lux()
            rows.append((duty, round(100.0 - duty, 4), lux))
            changed = last is None or lux is None or abs(lux - last) > 0.5
            if changed or not args.quiet:
                mark = "" if last is None or lux is None else f"  {lux - last:+9.1f}"
                print(f"{duty:6.2f}  {100 - duty:6.2f}  {lux:9.1f}{mark}")
            last = lux
    except KeyboardInterrupt:
        print("\nstopped early")
    finally:
        try:
            pwm.change_duty_cycle(100)
            pwm.stop()
        except Exception:
            pass

    if args.csv and rows:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["duty_percent", "brightness_percent", "lux"])
            w.writerows(rows)
        print(f"\nwrote {len(rows)} points to {args.csv}")

    if rows:
        summarize(rows, args.step)
    print("\nPWM released. sudo systemctl start growlight")


if __name__ == "__main__":
    main()
