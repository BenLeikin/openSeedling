"""Pins and actuators: both PWM channels, pumps, fan, the shutdown gate
and cleanup()."""

import os
import sys
import threading
import time
from rpi_hardware_pwm import HardwarePWM

from applog import log
import events

import config

# Hardware PWM channel 0. Only 18 or 19 can do hardware PWM;
# GROWLIGHT_LIGHT_PIN picks between them.
GPIO_PIN = 18
try:
    GPIO_PIN = int(os.environ.get("GROWLIGHT_LIGHT_PIN", GPIO_PIN))
except ValueError:
    pass

# Each wiring has its own pin, because the two cannot share one: a MOSFET
# driving a 5V panel wants duty high for bright, while an optocoupler on a
# 0-10V dim line wants duty high for dark.
#
# GROWLIGHT_LIGHT_PIN is the MOSFET panel, GROWLIGHT_DIM_PIN the optocoupler.
# Both must be in the overlay:
#   dtoverlay=pwm-2chan,pin=18,func=2,pin2=19,func2=2
GPIO_PIN2 = None
try:
    _p2 = (
        os.environ.get("GROWLIGHT_DIM_PIN") or os.environ.get("GROWLIGHT_LIGHT2_PIN") or ""
    ).strip()
    if _p2:
        GPIO_PIN2 = int(_p2)
except ValueError:
    GPIO_PIN2 = None
DIM_PIN_SET = bool(GPIO_PIN2 and GPIO_PIN2 != GPIO_PIN)
PWM_FREQ = 1000


def main_pin_is_dim(cfg):
    """Whether the main pin carries the dim-line fixture, where dark is duty
    100: the dim wiring is selected and has no pin of its own. With a dim pin
    configured, the main pin is always the MOSFET panel."""
    backend = cfg.get("light_backend")
    dim = backend == "dim" or (backend != "kasa" and bool(cfg.get("light_invert")))
    return dim and not DIM_PIN_SET


# Fan and pumps go through gpiozero. A pin that cannot be claimed (off the Pi,
# or unwired) disables that actuator and the controller carries on.
#
# The fan is on BCM 20, physical 38, low-side switched via a D4184 with a
# flyback across the fan. GROWLIGHT_FAN_PIN overrides the pin; set to empty,
# it means no fan.
FAN_PIN = 20
_fanenv = os.environ.get("GROWLIGHT_FAN_PIN")
if _fanenv is not None and not _fanenv.strip():
    FAN_PIN = None
else:
    try:
        FAN_PIN = int(_fanenv) if _fanenv else FAN_PIN
    except ValueError:
        log.warning(f"GROWLIGHT_FAN_PIN unreadable; using {FAN_PIN}")
# Speed control is software PWM: GPIO20 has no hardware PWM channel (those are
# 18 and 19, and 18 drives the light). Cheap fans can whine audibly or stall
# below about 30%, which is what the fan_min_speed setting is for.
_fan = None
FAN_PWM_HZ = 100
if FAN_PIN is None:
    log.info("fan: none configured")
else:
    try:
        from gpiozero import PWMOutputDevice as _PWMOut

        _fan = _PWMOut(FAN_PIN, frequency=FAN_PWM_HZ, initial_value=0)
    except Exception as _e:
        log.warning(f"fan GPIO{FAN_PIN} unavailable ({_e}); fan control disabled")
FAN_HW = _fan is not None
fan_state = {"on": False, "reason": "off", "speed": 0}

# GROWLIGHT_PUMP_PINS overrides the pump pins, e.g. "1:24,2:26" in the systemd
# unit; set to empty, it means no pumps.
PUMP_PINS = {"1": 24, "2": 26}  # tray -> BCM (physical 18, 37)
_pp = os.environ.get("GROWLIGHT_PUMP_PINS")
if _pp is not None and not _pp.strip():
    PUMP_PINS = {}
    log.info("pump pins: none configured")
elif (_pp or "").strip():
    _pp = _pp.strip()
    try:
        PUMP_PINS = {t.strip(): int(v) for t, v in (part.split(":") for part in _pp.split(","))}
        log.info(f"pump pins from environment: {PUMP_PINS}")
    except Exception as _e:
        log.warning(f"GROWLIGHT_PUMP_PINS unreadable ({_e}); using {PUMP_PINS}")
_pumps = {}
for _t, _pin in PUMP_PINS.items():
    try:
        from gpiozero import OutputDevice

        _pumps[_t] = OutputDevice(_pin, active_high=True, initial_value=False)
    except Exception as _e:
        log.warning(f"pump {_t} GPIO{_pin} unavailable ({_e}); disabled")
PUMP_HW = bool(_pumps)


def _blank_pump():
    return {
        "running": False,
        "last_run": 0.0,
        "today_seconds": 0.0,
        "reserved": 0.0,  # seconds a run in progress may still use
        "day": "",
        "last_detail": "",
    }


pump_state = {t: _blank_pump() for t in PUMP_PINS}
pump_lock = threading.Lock()  # also serializes the two pumps: one at a time

try:
    pwm = HardwarePWM(pwm_channel=(1 if GPIO_PIN == 19 else 0), hz=PWM_FREQ, chip=0)
    # Start dark: the control loop's first pass has not run yet.
    pwm.start(100.0 if main_pin_is_dim(config.settings) else 0.0)
except Exception as e:
    sys.exit(
        f"Hardware PWM unavailable ({e}). Check that "
        f"'dtoverlay=pwm,pin=18,func=2' is in /boot/firmware/config.txt "
        f"and reboot after adding it."
    )

pwm2 = None
if GPIO_PIN2 and GPIO_PIN2 != GPIO_PIN:
    try:
        pwm2 = HardwarePWM(pwm_channel=(1 if GPIO_PIN2 == 19 else 0), hz=PWM_FREQ, chip=0)
        pwm2.start(100.0)  # the dim channel: start pulled down = dark
        log.info(f"dim-line fixture on GPIO{GPIO_PIN2}")
    except Exception as e:
        # a missing second channel must not stop the controller: the main
        # light, the pumps and the sensors all still work without it
        log.warning(f"dim-line fixture on GPIO{GPIO_PIN2} unavailable ({e}); disabled")
        pwm2 = None
elif GPIO_PIN2:
    log.warning("GROWLIGHT_DIM_PIN is the same pin as the panel; ignored")


# Set by cleanup() when a shutdown starts. After that, hardware writes may only
# turn things off: a pump thread or the control loop finishing a pass must not
# relight the fixture or restart a pump.
SHUTTING_DOWN = threading.Event()


def set_fan(speed, reason):
    """Drive the fan at `speed` percent (0 = off) and record `reason`.

    A non-zero speed is raised to fan_min_speed, since a fan commanded below
    its stall point hums without turning. Does nothing when there is no fan,
    and during shutdown accepts only 0."""
    if not FAN_HW:
        return
    speed = max(0.0, min(100.0, float(speed or 0)))
    if SHUTTING_DOWN.is_set() and speed > 0:
        return
    if speed > 0:
        with config.settings_lock:
            floor = float(config.settings.get("fan_min_speed", 0) or 0)
        speed = max(speed, floor)
    try:
        _fan.value = speed / 100.0
    except Exception as e:
        log.error(f"fan control error: {e}")
        return
    with config.state_lock:
        changed = round(fan_state.get("speed", 0)) != round(speed)
        fan_state.update(on=speed > 0, reason=reason, speed=round(speed))
    if changed:
        log.info(f"fan {round(speed)}% ({reason})")
        events.publish("fan")


def fan_should_run(cfg, now, on_time, off_time, rh=None):
    """Decide the fan's state in auto mode. Returns (on, reason).

    `rh` is the relative humidity to judge by, or None when there is none.
    """
    hum_on = cfg.get("fan_humidity_on", 0)
    if hum_on and rh is not None and rh >= hum_on:
        return True, f"humidity {rh:.0f}%"
    if cfg.get("fan_with_light", True) and on_time and off_time:
        if on_time <= now <= off_time:
            return True, "photoperiod"
    return False, "idle"


# What other modules need done at shutdown. This module knows the pins; the
# light's two channels, the heat mat's plug and the open dashboard streams
# belong to modules above it, which register their part here.
_shutdown_steps = {"off": [], "start": [], "end": []}


def at_shutdown(fn, when="start"):
    """Have fn() called when the service stops.

    when: "off"   with every pass that switches the actuators off (it runs
                  twice, so it must be safe to repeat)
          "start" once, right after the first of those passes
          "end"   once, last, to leave an output in its resting state
    """
    _shutdown_steps[when].append(fn)


def _run_steps(when):
    for fn in _shutdown_steps[when]:
        try:
            fn()
        except Exception as e:  # one failed step must not skip the rest
            log.error(f"shutdown step {fn.__name__} failed: {e}")


def _all_off():
    """Every actuator off: pumps, fan, and whatever registered for "off"."""
    for _p in _pumps.values():
        try:
            _p.off()
        except Exception:
            pass
    if _fan is not None:
        try:
            _fan.value = 0
        except Exception:
            pass
    _run_steps("off")


def cleanup(*_):
    """Stop the service: everything off, then exit. The signal handler."""
    # Explicit off for every actuator: a SIGTERM mid-fill must not leave a pump
    # relying on gpiozero's teardown and a gate pulldown.
    SHUTTING_DOWN.set()
    _all_off()
    _run_steps("start")
    # A pump thread or control pass already past its check can still write
    # once more. Pump threads poll the flag every 0.1 s: wait for running pumps
    # to finish, then turn everything off a second time.
    deadline = time.time() + 2.0
    while time.time() < deadline and any(st["running"] for st in pump_state.values()):
        time.sleep(0.05)
    time.sleep(0.2)
    _all_off()
    _run_steps("end")
    sys.exit(0)
