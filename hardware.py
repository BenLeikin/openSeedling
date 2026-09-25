"""Pins and actuators: both PWM channels, pumps, fan, the shutdown gate
and cleanup()."""

import os
import sys
import threading
import time
from rpi_hardware_pwm import HardwarePWM

from applog import log

import config

GPIO_PIN      = 18   # hardware PWM channel 0. Only 18 or 19 can do hardware
                     # PWM; GROWLIGHT_LIGHT_PIN picks between them.
try:
    GPIO_PIN = int(os.environ.get("GROWLIGHT_LIGHT_PIN", GPIO_PIN))
except ValueError:
    pass

# Each wiring gets its own pin, because the two cannot share one: a MOSFET
# driving a 5V panel wants duty high for bright, while an optocoupler on a
# 0-10V dim line wants duty high for DARK. On one pin they are always opposed
# and one of them runs backwards.
#
# GROWLIGHT_LIGHT_PIN is the MOSFET panel, GROWLIGHT_DIM_PIN the optocoupler.
# Both must be in the overlay:
#   dtoverlay=pwm-2chan,pin=18,func=2,pin2=19,func2=2
GPIO_PIN2 = None
try:
    _p2 = (os.environ.get("GROWLIGHT_DIM_PIN")
           or os.environ.get("GROWLIGHT_LIGHT2_PIN") or "").strip()
    if _p2:
        GPIO_PIN2 = int(_p2)
except ValueError:
    GPIO_PIN2 = None
PWM_FREQ      = 1000

# --- pump actuators (gpiozero, guarded so off-Pi / unwired stays safe) ---
# Pump GPIOs (BCM). Override without editing code by setting GROWLIGHT_PUMP_PINS,
# e.g. GROWLIGHT_PUMP_PINS="1:24,2:26" in the systemd unit, then restart.
FAN_PIN = 20                     # BCM; physical 38. Low-side switched via a
                                 #   D4184 with a flyback across the fan.
_fanenv = os.environ.get("GROWLIGHT_FAN_PIN")
if _fanenv is not None and not _fanenv.strip():
    FAN_PIN = None                      # explicitly configured as "no fan"
else:
    try:
        FAN_PIN = int(_fanenv) if _fanenv else FAN_PIN
    except ValueError:
        log.warning(f"GROWLIGHT_FAN_PIN unreadable; using {FAN_PIN}")
# Speed control is software PWM: GPIO20 has no hardware PWM channel (those are
# 18 and 19, and 18 drives the light). Software PWM is fine for a fan at these
# duty cycles, but cheap fans can whine audibly or stall below ~30%, which is
# why fan_min_speed exists.
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

PUMP_PINS = {"1": 24, "2": 26}   # tray -> BCM (physical 18, 37)
_pp = os.environ.get("GROWLIGHT_PUMP_PINS")
if _pp is not None and not _pp.strip():
    PUMP_PINS = {}                      # explicitly configured as "no pumps"
    log.info("pump pins: none configured")
elif (_pp or "").strip():
    _pp = _pp.strip()
    try:
        PUMP_PINS = {t.strip(): int(v) for t, v in
                     (part.split(":") for part in _pp.split(","))}
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
    return {"running": False, "last_run": 0.0,
            "today_seconds": 0.0, "day": "", "last_detail": ""}
pump_state = {t: _blank_pump() for t in PUMP_PINS}
pump_lock = threading.Lock()   # also serializes the two pumps: one at a time

try:
    pwm = HardwarePWM(pwm_channel=(1 if GPIO_PIN == 19 else 0),
                      hz=PWM_FREQ, chip=0)
    # Start DARK, not at duty 0: on inverted wiring duty 0 is full brightness,
    # so a plain start(0) would blast the light on at boot until the control
    # loop's first pass caught up.
    pwm.start(100.0 if (config.settings.get("light_backend") == "dim"
                        or config.settings.get("light_invert")) else 0.0)  # start dark
except Exception as e:
    sys.exit(f"Hardware PWM unavailable ({e}). Check that "
             f"'dtoverlay=pwm,pin=18,func=2' is in /boot/firmware/config.txt "
             f"and reboot after adding it.")

pwm2 = None
if GPIO_PIN2 and GPIO_PIN2 != GPIO_PIN:
    try:
        pwm2 = HardwarePWM(pwm_channel=(1 if GPIO_PIN2 == 19 else 0),
                           hz=PWM_FREQ, chip=0)
        pwm2.start(100.0)          # the dim channel: start pulled down = dark
        log.info(f"dim-line fixture on GPIO{GPIO_PIN2}")
    except Exception as e:
        # a missing second channel must not stop the controller: the main
        # light, the pumps and the sensors all still work without it
        log.warning(f"dim-line fixture on GPIO{GPIO_PIN2} unavailable ({e}); disabled")
        pwm2 = None
elif GPIO_PIN2:
    log.warning("GROWLIGHT_DIM_PIN is the same pin as the panel; ignored")


# Set by cleanup() the moment a shutdown starts. After that, hardware writes
# may only turn things OFF: a pump thread or the control loop finishing a pass
# must not relight the fixture or restart a pump behind cleanup's back.
SHUTTING_DOWN = threading.Event()


def set_fan(speed, reason):
    """Drive the fan at `speed` percent (0 = off) and remember why. Airflow
    does two jobs for seedlings: it dries the surface between waterings
    (damping-off is the main killer after germination) and the movement
    thickens stems. A non-zero speed is raised to fan_min_speed, since a fan
    commanded below its stall point hums without actually turning."""
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
        status_mod.publish("fan")


def fan_should_run(cfg, now, on_time, off_time):
    """Decide the fan's state in auto mode. Returns (on, reason)."""
    rh = None
    try:
        # filtered: a single bad humidity reading should not kick the fan on
        rh, _ = monitor.reading_filtered("humidity")
    except Exception:
        pass
    hum_on = cfg.get("fan_humidity_on", 0)
    if hum_on and rh is not None and rh >= hum_on:
        return True, f"humidity {rh:.0f}%"
    if cfg.get("fan_with_light", True) and on_time and off_time:
        if on_time <= now <= off_time:
            return True, "photoperiod"
    return False, "idle"


def _all_off():
    """Every actuator off: pumps, fan, both light channels."""
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
    light_mod._dither_stop()
    light_mod.light2_state["level"] = 0.0     # or the write below would keep it lit
    light_mod.set_brightness(0)               # darkens both channels


def cleanup(*_):
    # From here on, hardware writes may only turn things off (see
    # SHUTTING_DOWN). Explicit off for every actuator: a SIGTERM mid-fill must
    # not leave a pump relying on gpiozero's atexit teardown and a gate pulldown.
    SHUTTING_DOWN.set()
    _all_off()
    status_mod._end_streams()
    # A pump thread or control pass already past its check could still write
    # once more. They poll every 0.1 s and now see the flag; wait for running
    # pumps to finish, then turn everything off a second time to be sure.
    deadline = time.time() + 2.0
    while time.time() < deadline and any(st["running"] for st in pump_state.values()):
        time.sleep(0.05)
    time.sleep(0.2)
    _all_off()
    # Stopping the PWM releases the pin, and on the optocoupler wiring that
    # means the dim line floats back to its own ~10.8V and the fixture goes to
    # FULL. Exactly backwards for a shutdown. The hardware PWM lives in sysfs
    # and keeps running after the process exits, so leaving that channel
    # driving its dark duty is what actually keeps the light off.
    # The main pin carries the dim fixture when no separate dim pin is set, and
    # releasing that pin would let the fixture come on.
    light_mod._stop_pwm(pwm, "dim" if (pwm2 is None and light_mod.light_backend() == "dim") else "pwm")
    if pwm2 is not None:
        light_mod._stop_pwm(pwm2, "dim")     # dim line: keep it pulled down, or it lights
    sys.exit(0)

# Imported last: these modules import this one, and their import-time
# code runs only after everything above is defined. Their names are
# used inside functions, at call time, always as module.name.
import light as light_mod
import monitor
import status as status_mod
