#!/usr/bin/env python3
"""Sensor I/O for the grow dashboard.

The one job of this module: talk to hardware, hand back a flat dict of clean
readings. Nothing here knows about brightness, schedules, or the web app.

    import sensors
    sensors.read_all()
    # -> {"moisture:B2": 43.1, "temp:air": 22.4, "humidity": 58.0,
    #     "lux": 1840.0, "temp:soil_1": 21.7, ...}   (None values dropped)

read_all() returns only sensors that are actually wired and enabled; anything
not yet wired is simply absent (the dashboard shows "-" for it). As each sensor
type is wired, fill in its _read_* function and flip its entry in ENABLED.
Build one type at a time and verify before moving on.

Hardware plan (all on the Pi's I2C bus, pins 3=SDA / 5=SCL, plus 1-Wire on
GPIO4 / pin 7):
  - 2x capacitive soil moisture (one per tray) -> ADS1115 ADC @ 0x48 (A0, A1)
  - BME280 air temp + humidity    -> 0x76
  - BH1750 ambient lux            -> 0x23
  - 5x DS18B20 soil temp          -> 1-Wire, /sys/bus/w1/devices/28-*
"""


import glob
import os
import statistics
import time

# Flip these to True as each sensor type is wired and its _read_* filled in.
# (Moisture probes have their own PROBE_ENABLED below.)
ENABLED = {
    "air": False,      # BME280 temp + humidity
    "lux": False,      # BH1750
    "soil_temp": True,   # DS18B20 on 1-Wire (GPIO4); auto-detects attached probes
}

# --- float switch (destination tray; reports raw switch state) ---
# Flip FLOAT_ENABLED True once wired. Pin is BCM GPIO23 (physical pin 16),
# other leg to GND, internal pull-up. read_float() reports the raw contact
# state so you can verify the mapping by hand, then mount/flip the float so
# "tray full" lands on the fail-safe (broken-wire) state.
FLOAT_PIN = 23
FLOAT_ENABLED = True
_float_dev = None
_float_init = False


def _float():
    global _float_dev, _float_init
    if _float_init:
        return _float_dev
    _float_init = True
    if not FLOAT_ENABLED:
        return None
    try:
        from gpiozero import Button
        # pull_up=True -> is_pressed is True when the pin is pulled LOW
        # (switch closed to GND). Open circuit / broken wire -> not pressed.
        _float_dev = Button(FLOAT_PIN, pull_up=True, bounce_time=0.1)
    except Exception as e:
        print(f"float switch unavailable ({e}); reporting unknown")
        _float_dev = None
    return _float_dev


def read_float():
    """Raw switch state: 1.0 = closed (pin low), 0.0 = open (pin high or
    broken wire), None = no sensor. Interpretation (which state means 'full')
    is decided during mounting; see FLOAT_ENABLED comment."""
    dev = _float()
    if dev is None:
        return None
    try:
        return 1.0 if dev.is_pressed else 0.0
    except Exception:
        return None


# Two capacitive soil-moisture probes, one per seedling tray, on a single
# ADS1115 at 0x48: tray 1 -> A0, tray 2 -> A1. read_probes() returns raw ADC
# counts; wet/dry calibration and the %-conversion live in the app so they can
# be set from the dashboard and persisted.
PROBE_ENABLED = True
PROBE_ADDR = 0x48
PROBE_BUS = 1                       # /dev/i2c-1, the Pi's primary I2C bus
PROBE_CHANNELS = {"1": 0, "2": 1}   # tray -> ADS1115 channel (A0, A1)
_probe_chans = None
_probe_init = False


# --------------------------- moisture (ADS1115) ---------------------------

def _probes():
    global _probe_chans, _probe_init
    if _probe_init:
        return _probe_chans
    _probe_init = True
    if not PROBE_ENABLED:
        return None
    try:
        from adafruit_ads1x15.ads1115 import ADS1115
        from adafruit_ads1x15.analog_in import AnalogIn
        try:
            # Address /dev/i2c-1 directly. Blinka's busio probe sometimes fails
            # to see a board that i2cdetect finds; this path is reliable.
            from adafruit_extended_bus import ExtendedI2C
            i2c = ExtendedI2C(PROBE_BUS)
        except ImportError:
            import board
            import busio
            i2c = busio.I2C(board.SCL, board.SDA)
        adc = ADS1115(i2c, address=PROBE_ADDR)
        adc.gain = 1  # +/-4.096V full scale, covers a 3.3V sensor
        _probe_chans = {t: AnalogIn(adc, ch) for t, ch in PROBE_CHANNELS.items()}
    except Exception as e:
        print(f"ADS1115 unavailable ({e}); moisture probes disabled")
        _probe_chans = None
    return _probe_chans


def read_probes(samples=8):
    """Median probe voltage per tray, e.g. {'probe:1': 1.883, 'probe:2': 1.844}.
    Capacitive probes read low in wet soil, high in dry. A median of several
    samples rejects switching noise from the light supply; a single read
    catches whatever is on the line at that instant. Absent if the ADC is not
    present. Conversion to a percentage happens in the app."""
    chans = _probes()
    if not chans:
        return {}
    out = {}
    for tray, ch in chans.items():
        try:
            vals = [ch.voltage for _ in range(max(1, samples))]
            out[f"probe:{tray}"] = round(statistics.median(vals), 4)
        except Exception as e:
            print(f"probe {tray} read error: {e}")
    return out


def probe_spread(tray, samples=10, delay=0.2):
    """Sample one probe repeatedly and return (median, spread). Used by
    calibration: a wide spread means noise on the analog run, and an anchor
    captured from it will be junk."""
    chans = _probes()
    if not chans or tray not in chans:
        return None, None
    ch = chans[tray]
    vals = []
    for _ in range(max(2, samples)):
        try:
            vals.append(ch.voltage)
        except Exception:
            pass
        time.sleep(delay)
    if len(vals) < 2:
        return None, None
    return round(statistics.median(vals), 4), round(max(vals) - min(vals), 4)


# ----------------------------- air (BME280) -------------------------------

def _read_air():
    if not ENABLED["air"]:
        return {}
    # TODO (wire-up): real read via adafruit_bme280 @ 0x76.
    #   from adafruit_bme280 import basic as bme280
    #   sensor = bme280.Adafruit_BME280_I2C(i2c, address=0x76)
    #   return {"temp:air": sensor.temperature, "humidity": sensor.humidity}
    return {}


# ------------------------------ lux (BH1750) ------------------------------

def _read_lux():
    if not ENABLED["lux"]:
        return {}
    # TODO (wire-up): real read via adafruit_bh1750 @ 0x23.
    #   import adafruit_bh1750
    #   return {"lux": adafruit_bh1750.BH1750(i2c).lux}
    return {}


# -------------------------- soil temp (DS18B20) ---------------------------

def _read_soil_temps():
    """Every DS18B20 on the 1-Wire bus, in Celsius (the dashboard converts to F,
    matching the other temp: keys). Probes appear in sysfs
    as /sys/bus/w1/devices/28-*; each w1_slave read carries a CRC line, and a
    failed CRC (usually a wiring or pull-up problem) is dropped rather than
    logged as a bogus temperature. Keys are stable per probe serial, so adding
    a second probe later won't renumber the first."""
    if not ENABLED["soil_temp"]:
        return {}
    out = {}
    devs = sorted(glob.glob("/sys/bus/w1/devices/28-*"))
    for dev in devs:
        serial = os.path.basename(dev)
        # single probe reads as "soil"; multiples get a serial suffix so the
        # key never shifts when probes are added or reordered
        key = "temp:soil" if len(devs) == 1 else f"temp:soil_{serial[-4:]}"
        try:
            with open(f"{dev}/w1_slave") as f:
                lines = f.readlines()
            if len(lines) < 2 or not lines[0].strip().endswith("YES"):
                print(f"{serial}: CRC failed, skipping")
                continue
            if "t=" not in lines[1]:
                continue
            c = int(lines[1].split("t=")[1]) / 1000.0
            if c in (85.0, -127.0):   # power-on default / disconnected
                print(f"{serial}: bogus reading {c}C, skipping")
                continue
            out[key] = round(c, 2)   # Celsius; the dashboard converts to F
        except Exception as e:
            print(f"{serial}: read error ({e})")
    return out


# ------------------------------------------------------------------------- #
# Each sensor type returns {} until its _read_* is filled in and its ENABLED
# entry flipped True. Camera-based dryness (growth.py) and the float switch are
# the live sources today; the I2C/1-Wire sensors above are wiring-pending.
# ------------------------------------------------------------------------- #


# --------------------------------- public ---------------------------------

def read_all():
    """Return {sensor_key: value} for wired sensors only. Each type is read
    independently and wrapped so one failed device never aborts the rest;
    failures and not-yet-wired types are simply absent from the result."""
    out = {}
    fv = read_float()
    if fv is not None:
        out["float:tray"] = fv
    for fn in (read_probes, _read_air, _read_lux, _read_soil_temps):
        try:
            out.update(fn())
        except Exception as e:
            print(f"sensor read error in {fn.__name__}: {e}")
    return out


if __name__ == "__main__":
    for k, v in sorted(read_all().items()):
        print(f"  {k:18s} {v}")
