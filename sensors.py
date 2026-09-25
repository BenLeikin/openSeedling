#!/usr/bin/env python3
"""Sensor I/O for the grow dashboard.

The one job of this module: talk to hardware, hand back a flat dict of clean
readings. Nothing here knows about brightness, schedules, or the web app.

    import sensors
    sensors.read_all()
    # -> {"probe:1": 1.883, "float:1": 1.0, "reservoir:low": 1.0,
    #     "temp:air": 22.4, "humidity": 58.0, "pressure": 1013.2,
    #     "lux": 1840.0, "temp:soil": 21.7, ...}

read_all() returns only the sensors actually present. Every device is probed
once at first use and disabled gracefully if absent, so a bare Pi (or a test
box) runs the whole app without them and the dashboard shows "-" for the
missing readings rather than failing.

Wired hardware (I2C on pins 3=SDA / 5=SCL, 1-Wire on GPIO4 / pin 7):
  - 2x capacitive soil probes -> ADS1115 ADC @ 0x48, channels A0 and A1
  - BME280 air temp / humidity / pressure -> 0x76 (BMP280 at 0x77 also
    detected; it has no humidity and that reading is simply absent)
  - BH1750 ambient lux -> 0x23
  - DS18B20 soil temp -> 1-Wire, auto-discovered from /sys/bus/w1/devices/28-*
  - 2x float switches (GPIO23, GPIO22): open = tray full, the fail-safe sense
  - 2x XKC-Y23A reservoir level sensors (GPIO27 low, GPIO17 high)

All pin assignments are overridable by environment variable, so moving a
signal off a bad pin is an .env change rather than a code edit.
"""


import glob
import os
import statistics
import threading
import time

from applog import log     # levelled logging; see applog.py

# One lock for every I2C read. The sample loop, the calibration endpoints, and
# the light sweep all touch these devices from different threads. Blinka only
# locks per bus transaction, but an ADS1115 read is write-config-then-read:
# two threads interleaving can attribute tray 2's voltage to tray 1, which
# would silently corrupt a calibration anchor. Held per read, never across
# sleeps, so the worst-case wait is one conversion.
_io_lock = threading.Lock()

# Per-type master switches. All wired; set one False to stop reading that
# device without unplugging it.
ENABLED = {
    "air": True,       # BME280/BMP280 air temp (+ humidity, + pressure)
    "lux": True,       # BH1750 ambient light
    "soil_temp": True,   # DS18B20 on 1-Wire (GPIO4); auto-detects attached probes
}

# --- float switches (one per tray) ---
# Other leg to GND, internal pull-up. Mounted so that rising water OPENS the
# switch: open reads as "full" and refuses to pump, which is also the
# broken-wire state, so a cut lead fails safe.
# Override via GROWLIGHT_FLOAT_PINS="1:23,2:22" to move one off a bad pin
# without editing code.
FLOAT_PINS = {"1": 23, "2": 22}   # tray -> BCM pin (physical 16, 15)
_fp = os.environ.get("GROWLIGHT_FLOAT_PINS")
if _fp is not None and not _fp.strip():
    FLOAT_PINS = {}                     # explicitly configured as "no floats"
    log.info("float pins: none configured")
elif (_fp or "").strip():
    _fp = _fp.strip()
    try:
        FLOAT_PINS = {a.strip(): int(b) for a, b in
                      (part.split(":") for part in _fp.split(","))}
        log.info(f"float pins from environment: {FLOAT_PINS}")
    except Exception as _e:
        log.warning(f"GROWLIGHT_FLOAT_PINS unreadable ({_e}); using {FLOAT_PINS}")
FLOAT_ENABLED = True
_float_devs = {}
_float_init = False

# --- reservoir level (XKC-Y23A NPN non-contact, through the bucket wall) ---
# Two sensors on the SOURCE reservoir: "low" mounted at the minimum-safe
# height, "high" near the top rim. NPN output pulls LOW on water detect, so
# with the internal pull-up is_pressed means water at that height. Some units
# ship inverted; set GROWLIGHT_RESERVOIR_INVERT=1 if the bench test reads
# backwards rather than reswapping wires.
# Pins overridable via GROWLIGHT_RESERVOIR_PINS="low:27,high:17"; empty
# string = no reservoir sensors.
RESERVOIR_PINS = {"low": 27, "high": 17}   # BCM (physical 13, 11), as built
_rp = os.environ.get("GROWLIGHT_RESERVOIR_PINS")
if _rp is not None and not _rp.strip():
    RESERVOIR_PINS = {}
    log.info("reservoir pins: none configured")
elif (_rp or "").strip():
    _rp = _rp.strip()
    try:
        RESERVOIR_PINS = {a.strip(): int(b) for a, b in
                          (part.split(":") for part in _rp.split(","))}
        log.info(f"reservoir pins from environment: {RESERVOIR_PINS}")
    except Exception as _e:
        log.warning(f"GROWLIGHT_RESERVOIR_PINS unreadable ({_e}); using {RESERVOIR_PINS}")
RESERVOIR_INVERT = bool((os.environ.get("GROWLIGHT_RESERVOIR_INVERT") or "")
                        .strip())
_res_devs = {}
_res_init = False

# ---- change notification ----------------------------------------------------
# The float and reservoir switches are binary and matter the moment they flip:
# a tray reading full should reach the dashboard in well under a second, not
# at the next five-minute sample. gpiozero calls these from its own thread on
# each debounced edge, so no polling is involved.
_listeners = []


def on_change(callback):
    """Call callback(key) whenever a float or reservoir switch changes."""
    _listeners.append(callback)


def _fire(key):
    for cb in list(_listeners):
        try:
            cb(key)
        except Exception as e:
            log.error(f"change listener failed for {key}: {e}")


def _edge(key):
    # A zero-argument callable on purpose: gpiozero passes the device to any
    # callback that accepts an argument, which would silently replace a
    # default-argument key with the Button object.
    return lambda: _fire(key)


def arm_watchers():
    """Open the switches now so their edge callbacks are live from startup,
    instead of from whenever something first happens to read them."""
    _floats()
    _reservoirs()


def _reservoirs():
    global _res_devs, _res_init
    if _res_init:
        return _res_devs
    _res_init = True
    if not RESERVOIR_PINS:
        return _res_devs
    try:
        from gpiozero import Button
    except Exception as e:
        log.warning(f"reservoir sensors unavailable ({e}); reporting unknown")
        return _res_devs
    for which, pin in RESERVOIR_PINS.items():
        try:
            dev = Button(pin, pull_up=True, bounce_time=0.1)
            dev.when_pressed = dev.when_released = _edge(f"reservoir:{which}")
            _res_devs[which] = dev
        except Exception as e:
            log.warning(f"reservoir {which} (GPIO{pin}) unavailable ({e}); "
                  "reporting unknown")
    return _res_devs


def read_reservoir_level(which):
    """One reservoir sensor: 1.0 water present at that height, 0.0 dry,
    None if that sensor isn't available."""
    dev = _reservoirs().get(str(which))
    if dev is None:
        return None
    try:
        wet = bool(dev.is_pressed)
        if RESERVOIR_INVERT:
            wet = not wet
        return 1.0 if wet else 0.0
    except Exception as e:
        log.error(f"reservoir {which} read error: {e}")
        return None


def read_reservoirs():
    """All wired reservoir sensors, e.g. {'reservoir:low': 1.0}."""
    out = {}
    for which in RESERVOIR_PINS:
        v = read_reservoir_level(which)
        if v is not None:
            out[f"reservoir:{which}"] = v
    return out


def _floats():
    global _float_devs, _float_init
    if _float_init:
        return _float_devs
    _float_init = True
    if not FLOAT_ENABLED:
        return _float_devs
    try:
        from gpiozero import Button
    except Exception as e:
        log.warning(f"float switches unavailable ({e}); reporting unknown")
        return _float_devs
    for tray, pin in FLOAT_PINS.items():
        try:
            dev = Button(pin, pull_up=True, bounce_time=0.1)
            dev.when_pressed = dev.when_released = _edge(f"float:{tray}")
            _float_devs[tray] = dev
        except Exception as e:
            log.warning(f"float {tray} (GPIO{pin}) unavailable ({e}); reporting unknown")
    return _float_devs


def read_float(tray="1"):
    """One tray's float contact: 1.0 closed (not full), 0.0 open (full),
    None if that switch isn't available. Open=full is the fail-safe sense:
    a broken wire reads full and refuses to pump."""
    dev = _floats().get(str(tray))
    if dev is None:
        return None
    try:
        return 1.0 if dev.is_pressed else 0.0
    except Exception as e:
        log.error(f"float {tray} read error: {e}")
        return None


def read_floats():
    """All wired floats, e.g. {'float:1': 1.0, 'float:2': 0.0}."""
    out = {}
    for tray in FLOAT_PINS:
        v = read_float(tray)
        if v is not None:
            out[f"float:{tray}"] = v
    return out


PROBE_BUS = 1            # /dev/i2c-1 (pins 3/5)
PROBE_ADDR = 0x48        # ADS1115, ADDR tied to GND
PROBE_CHANNELS = {"1": 0, "2": 1}   # tray -> ADS input (probe 1 -> A0, 2 -> A1)

_probe_chans = None
_probe_init = False


def _probes():
    """ADS1115 channels per tray, initialized once. Uses ExtendedI2C on bus 1:
    the plain Blinka bus probe has missed devices that i2cdetect sees, and the
    extended path talks to the kernel device directly. Gain 1 covers the
    probes' 0-3.3 V output. Returns {} if the ADC or libraries are absent, so
    a bare Pi (or the sandbox) runs without probes rather than crashing."""
    global _probe_chans, _probe_init
    if _probe_init:
        return _probe_chans or {}
    with _io_lock:
        if _probe_init:                 # another thread initialized while we waited
            return _probe_chans or {}
        _probe_init = True
        return _probes_init_locked()


def _probes_init_locked():
    global _probe_chans
    try:
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn
        ads = ADS.ADS1115(_i2c(), address=PROBE_ADDR)
        ads.gain = 1
        # AnalogIn takes a plain channel number. Some driver versions also
        # export P0..P3 constants, but they're just ints 0-3 and not all
        # versions have them, so pass the integer directly.
        _probe_chans = {tray: AnalogIn(ads, idx)
                        for tray, idx in PROBE_CHANNELS.items()}
    except Exception as e:
        log.warning(f"ADS1115 unavailable ({e}); moisture probes disabled")
        _probe_chans = {}
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
            with _io_lock:
                vals = [ch.voltage for _ in range(max(1, samples))]
            out[f"probe:{tray}"] = round(statistics.median(vals), 4)
        except Exception as e:
            log.error(f"probe {tray} read error: {e}")
    return out


def probe_settle(tray, seconds=15.0, delay=0.5):
    """Watch one probe for `seconds` and report whether it has settled.

    A two-second burst tells you about electrical noise; it cannot tell you the
    soil is still absorbing water, which is the thing that ruins a wet anchor.
    Returns (median, spread, drift) where drift is the change from the first
    third of the window to the last: still falling means water is still working
    its way in and the reading has not finished moving.
    """
    chans = _probes()
    if not chans or tray not in chans:
        return None, None, None
    ch = chans[tray]
    vals = []
    end = time.time() + max(2.0, seconds)
    while time.time() < end:
        try:
            with _io_lock:
                vals.append(ch.voltage)
        except Exception:
            pass
        time.sleep(delay)
    if len(vals) < 6:
        return None, None, None
    third = max(2, len(vals) // 3)
    early = statistics.median(vals[:third])
    late = statistics.median(vals[-third:])
    return (round(statistics.median(vals), 4),
            round(max(vals) - min(vals), 4),
            round(late - early, 4))


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
            with _io_lock:              # per sample, so sampling isn't starved
                vals.append(ch.voltage)
        except Exception:
            pass
        time.sleep(delay)
    if len(vals) < 2:
        return None, None
    return round(statistics.median(vals), 4), round(max(vals) - min(vals), 4)


# ----------------------------- air (BME280) -------------------------------

_i2c_bus = None


def _i2c():
    """One shared I2C handle for the BME/BMP280 and BH1750. Prefers the
    extended-bus path (same reliability reasoning as the ADS1115)."""
    global _i2c_bus
    if _i2c_bus is not None:
        return _i2c_bus
    try:
        from adafruit_extended_bus import ExtendedI2C
        _i2c_bus = ExtendedI2C(PROBE_BUS)
    except ImportError:
        import board
        import busio
        _i2c_bus = busio.I2C(board.SCL, board.SDA)
    return _i2c_bus


_air_dev = None
_air_init = False
_air_has_humidity = True


def _read_air():
    """BME280 (temp/humidity/pressure) or BMP280 (temp/pressure only); the two
    ship on identical-looking boards at 0x76 or 0x77, so probe both addresses
    and detect the variant by chip id (BME=0x60, BMP=0x58). Temps in Celsius."""
    global _air_dev, _air_init, _air_has_humidity
    if not ENABLED["air"]:
        return {}
    if not _air_init:
        _air_init = True
        for addr in (0x76, 0x77):
            try:
                i2c = _i2c()
                # read chip id first to pick the right driver
                from adafruit_bus_device.i2c_device import I2CDevice
                buf = bytearray(1)
                with I2CDevice(i2c, addr) as dev:
                    dev.write_then_readinto(bytes([0xD0]), buf)
                chip = buf[0]
                if chip == 0x60:          # BME280: has humidity
                    from adafruit_bme280 import basic as bme280
                    _air_dev = bme280.Adafruit_BME280_I2C(i2c, address=addr)
                    _air_has_humidity = True
                elif chip == 0x58:        # BMP280: no humidity
                    import adafruit_bmp280
                    _air_dev = adafruit_bmp280.Adafruit_BMP280_I2C(i2c, address=addr)
                    _air_has_humidity = False
                else:
                    continue
                log.info(f"air sensor: {'BME280' if _air_has_humidity else 'BMP280'} at {hex(addr)}")
                break
            except Exception:
                continue
        if _air_dev is None:
            log.warning("air sensor (BME/BMP280) not found at 0x76/0x77; disabled")
    if _air_dev is None:
        return {}
    try:
        with _io_lock:
            out = {"temp:air": round(_air_dev.temperature, 2),
                   "pressure": round(_air_dev.pressure, 1)}
            if _air_has_humidity:
                out["humidity"] = round(_air_dev.relative_humidity, 1)
        return out
    except Exception as e:
        log.error(f"air sensor read error: {e}")
        return {}


# ------------------------------ lux (BH1750) ------------------------------

_lux_dev = None
_lux_init = False


def _read_lux():
    """BH1750 ambient light, at 0x23 (ADDR low) or 0x5C (ADDR high)."""
    global _lux_dev, _lux_init
    if not ENABLED["lux"]:
        return {}
    if not _lux_init:
        _lux_init = True
        for addr in (0x23, 0x5C):
            try:
                import adafruit_bh1750
                _lux_dev = adafruit_bh1750.BH1750(_i2c(), address=addr)
                _lux_dev.lux  # probe read
                log.info(f"lux sensor: BH1750 at {hex(addr)}")
                break
            except Exception:
                _lux_dev = None
        if _lux_dev is None:
            if _lux_fail["n"]:
                # recovering from errors, not a sensor that was never fitted:
                # keep looking on later reads instead of giving up for good
                _lux_init = False
                _lux_fail["n"] += 1
                if _lux_fail["n"] % 50 == 0:
                    log.error(f"lux sensor still not answering "
                              f"({_lux_fail['n']} attempts)")
            else:
                log.warning("lux sensor (BH1750) not found at 0x23/0x5C; disabled")
    if _lux_dev is None:
        return {}
    try:
        with _io_lock:
            v = round(_lux_dev.lux, 1)
    except Exception as e:
        _lux_failed(e)
        return {}
    if _lux_fail["n"]:
        log.info(f"lux sensor recovered after {_lux_fail['n']} failed reads")
        _lux_fail["n"] = 0
    return {"lux": v}


# A BH1750 that loses power, even for a moment through a poor contact, wakes up
# powered down and needs its measurement mode set again. The driver only does
# that when it is created, so after a glitch the old handle can keep failing
# until the service restarts. After a few failures in a row the handle is
# dropped and the chip is found and configured afresh on the next read, so a
# reseated sensor comes back on its own. Errors are logged on the first
# failure and then sparingly, not once per read.
LUX_REINIT_AFTER = 3
_lux_fail = {"n": 0}


def _lux_failed(err):
    global _lux_dev, _lux_init
    _lux_fail["n"] += 1
    n = _lux_fail["n"]
    if n == 1 or n % 50 == 0:
        log.error(f"lux read error ({n} in a row): {err}")
    if n % LUX_REINIT_AFTER == 0:
        _lux_dev = None
        _lux_init = False          # the next read finds and configures it again
        if n == LUX_REINIT_AFTER:
            log.warning("lux sensor: re-initializing after repeated read errors")


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
        # A single probe reads as "soil"; with two or more, each gets a serial
        # suffix, so reordering never swaps them. Adding a second probe does
        # rename the first one's series (temp:soil -> temp:soil_xxxx).
        key = "temp:soil" if len(devs) == 1 else f"temp:soil_{serial[-4:]}"
        try:
            with open(f"{dev}/w1_slave") as f:
                lines = f.readlines()
            if len(lines) < 2 or not lines[0].strip().endswith("YES"):
                log.warning(f"{serial}: CRC failed, skipping")
                continue
            if "t=" not in lines[1]:
                continue
            c = int(lines[1].split("t=")[1]) / 1000.0
            if c in (85.0, -127.0):   # power-on default / disconnected
                log.info(f"{serial}: bogus reading {c}C, skipping")
                continue
            out[key] = round(c, 2)   # Celsius; the dashboard converts to F
        except Exception as e:
            log.error(f"{serial}: read error ({e})")
    return out


# --------------------------------- public ---------------------------------

# Everything except the 1-Wire soil probes, which need a 750ms conversion
# each and change far too slowly to be worth that every few seconds.
FAST_READS = (read_probes, read_floats, read_reservoirs, _read_air, _read_lux)
ALL_READS = FAST_READS + (_read_soil_temps,)
# What the dashboard's live refresh actually shows. Kept separate so the
# every-few-seconds loop does not also run 16 ADS1115 conversions it discards.
LIVE_READS = (_read_air, _read_lux)


def _read_set(fns):
    """Read a set of devices, one failure never aborting the rest."""
    out = {}
    for fn in fns:
        try:
            out.update(fn())
        except Exception as e:
            log.error(f"sensor read error in {fn.__name__}: {e}")
    return out


def read_all():
    """Every wired sensor. Failures and unwired types are simply absent."""
    return _read_set(ALL_READS)


def read_fast():
    """The quick sensors only (everything but 1-Wire)."""
    return _read_set(FAST_READS)


def read_live():
    """Air and light only: the readings the live refresh pushes to the page."""
    return _read_set(LIVE_READS)


if __name__ == "__main__":
    for k, v in sorted(read_all().items()):
        if k.startswith("temp:"):
            log.info(f"  {k:18s} {v * 9 / 5 + 32:.1f} F   ({v} C)")
        elif k.startswith("probe:"):
            log.info(f"  {k:18s} {v} V")
        else:
            log.info(f"  {k:18s} {v}")
