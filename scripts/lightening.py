#!/usr/bin/env python3
"""Lightning effects on the grow light. Purely for fun.

Real lightning is not one flash: a stroke is several strikes down the same
channel a few tens of milliseconds apart, which is why it looks like it
stutters. That is what makes a convincing effect, and it is also what the
hardware here struggles with, so the timing is worth knowing about.

Run with the growlight service STOPPED, since both own the same PWM channel:

    sudo systemctl stop growlight
    ~/growlight/venv/bin/python lightning.py                # a storm
    ~/growlight/venv/bin/python lightning.py --style strike # single strokes
    ~/growlight/venv/bin/python lightning.py --style flicker
    ~/growlight/venv/bin/python lightning.py --calibrate    # measure the limit
    sudo systemctl start growlight

One caveat that shapes everything below: the optocoupler's RC filter exists
to smooth 1kHz PWM into a steady level, and it does the same to anything
faster than roughly 50ms. The driver adds its own lag. So a 5ms flash gets
averaged away. --calibrate finds the real limit for your wiring, and the
defaults are deliberately slow enough to survive it.

Duty is inverted on this wiring: 0% duty is full brightness, 100% is dark.
"""
import argparse
import random
import sys
import time

DEFAULT_PIN = 19
DEFAULT_HZ = 1000
DARK = 100.0            # duty that means dark
BRIGHT = 0.0            # duty that means full


def channel_for(pin):
    if pin in (18, 12):
        return 0
    if pin in (19, 13):
        return 1
    raise SystemExit(f"GPIO{pin} is not a hardware PWM pin (12, 13, 18 or 19)")


def level(pwm, brightness):
    """Set brightness 0-100, handling the inversion in one place."""
    b = max(0.0, min(100.0, brightness))
    pwm.change_duty_cycle(DARK - b)


class Effects:
    def __init__(self, pwm, base, peak, fast):
        self.pwm = pwm
        self.base = base      # ambient level between strikes
        self.peak = peak      # flash level
        self.fast = fast      # shortest pulse worth attempting, seconds

    def idle(self):
        level(self.pwm, self.base)

    def strike(self):
        """One stroke: a leader, the main return, then a few restrikes.

        The restrike count and spacing are what sell it. A single on-off
        looks like a light switch; three unevenly spaced flashes look like
        weather.
        """
        p = self.pwm
        # faint leader, sometimes
        if random.random() < 0.6:
            level(p, self.base + (self.peak - self.base) * random.uniform(.1, .25))
            time.sleep(self.fast)
            level(p, self.base)
            time.sleep(random.uniform(.02, .06))
        # main return stroke
        level(p, self.peak)
        time.sleep(self.fast * random.uniform(1.0, 2.2))
        # restrikes down the same channel, decaying
        for i in range(random.randint(1, 4)):
            level(p, self.base)
            time.sleep(random.uniform(.03, .09))
            level(p, self.peak * random.uniform(.55, .95))
            time.sleep(self.fast * random.uniform(.8, 1.6))
        # afterglow, then back to ambient
        level(p, self.base + (self.peak - self.base) * .15)
        time.sleep(random.uniform(.05, .15))
        level(p, self.base)

    def sheet(self):
        """Cloud-to-cloud: no sharp edges, just the sky lighting up."""
        p = self.pwm
        steps = 14
        top = self.peak * random.uniform(.45, .8)
        for i in range(steps):          # swell
            level(p, self.base + (top - self.base) * (i + 1) / steps)
            time.sleep(0.012)
        time.sleep(random.uniform(.05, .12))
        for i in range(steps * 2):      # slower fade
            level(p, top - (top - self.base) * (i + 1) / (steps * 2))
            time.sleep(0.018)
        level(p, self.base)

    def flicker(self):
        """The stuttering, near-continuous flashing of a storm overhead."""
        p = self.pwm
        for _ in range(random.randint(6, 16)):
            level(p, self.peak * random.uniform(.4, 1.0))
            time.sleep(self.fast * random.uniform(.8, 1.8))
            level(p, self.base)
            time.sleep(random.uniform(.03, .12))


def calibrate(pwm, app_dir):
    """Find the shortest flash this wiring can actually produce.

    The RC filter and the driver both slow things down; below some pulse
    width the light barely responds. This measures where that is instead of
    guessing, which is the difference between a convincing effect and a
    vague shimmer.
    """
    try:
        sys.path.insert(0, app_dir)
        import sensors
        if "lux" not in sensors._read_lux():
            raise RuntimeError("no reading")
    except Exception as e:
        print(f"no lux sensor ({e}); using the 60ms default")
        return 0.06

    level(pwm, 0)
    time.sleep(1.0)
    dark = sensors._read_lux()["lux"]
    level(pwm, 100)
    time.sleep(1.0)
    full = sensors._read_lux()["lux"]
    print(f"dark {dark:.0f} lx, full {full:.0f} lx")
    if full - dark < 10:
        print("not enough contrast to calibrate; using 60ms")
        return 0.06

    print("\nflash width   peak reached")
    best = 0.25
    for width in (0.25, 0.18, 0.12, 0.09, 0.06, 0.04, 0.03, 0.02, 0.01):
        level(pwm, 0)
        time.sleep(0.5)
        level(pwm, 100)
        time.sleep(width)
        lux = sensors._read_lux()["lux"]     # read while still lit
        level(pwm, 0)
        frac = (lux - dark) / (full - dark)
        print(f"  {width*1000:5.0f} ms     {frac*100:5.0f}%")
        if frac > 0.6:
            best = width
        time.sleep(0.3)
    print(f"\nshortest flash that still reaches 60% of full: {best*1000:.0f} ms")
    print("Anything shorter gets averaged away by the RC filter and the driver.")
    return best


def main():
    ap = argparse.ArgumentParser(description="Lightning effects on the grow light")
    ap.add_argument("--pin", type=int, default=DEFAULT_PIN)
    ap.add_argument("--hz", type=int, default=DEFAULT_HZ)
    ap.add_argument("--style", default="storm",
                    choices=("storm", "strike", "sheet", "flicker"))
    ap.add_argument("--base", type=float, default=0.0,
                    help="ambient brightness between strikes (default 0)")
    ap.add_argument("--peak", type=float, default=100.0)
    ap.add_argument("--fast", type=float, default=0.06,
                    help="shortest flash in seconds (default 0.06)")
    ap.add_argument("--gap", type=float, default=4.0,
                    help="average seconds between strikes in storm mode")
    ap.add_argument("--count", type=int, default=0,
                    help="stop after this many events (0 = until Ctrl-C)")
    ap.add_argument("--calibrate", action="store_true",
                    help="measure the shortest usable flash, then exit")
    ap.add_argument("--app", default="/home/ben/growlight")
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
    pwm.start(DARK)
    try:
        if args.calibrate:
            calibrate(pwm, args.app)
            return

        fx = Effects(pwm, args.base, args.peak, args.fast)
        fx.idle()
        print(f"{args.style}: Ctrl-C to stop")
        n = 0
        while True:
            if args.style == "strike":
                fx.strike()
            elif args.style == "sheet":
                fx.sheet()
            elif args.style == "flicker":
                fx.flicker()
            else:
                # a storm mixes them, mostly distant sheet with the odd
                # close strike, which is what a real one sounds like on film
                r = random.random()
                (fx.sheet if r < .5 else fx.strike if r < .9 else fx.flicker)()
            n += 1
            if args.count and n >= args.count:
                break
            # gaps are lumpy in a real storm, not evenly spaced
            time.sleep(random.expovariate(1.0 / max(0.2, args.gap)))
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        try:
            pwm.change_duty_cycle(DARK)
            pwm.stop()
        except Exception:
            pass
        print("PWM released. sudo systemctl start growlight")


if __name__ == "__main__":
    main()
