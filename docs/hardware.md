# Hardware

Model numbers where the part has one; some parts are commodity classes sold
under a dozen brands, and any unit of that class works.

| Part | Model / detail |
| --- | --- |
| Computer | Raspberry Pi Zero 2 W, 64-bit Raspberry Pi OS |
| Light | 5 V USB LED grow panel, mixed red/blue/white spectrum, low-side switched through a D4184 MOSFET module. Spectrum factor K=60 lx per umol/m2/s measured for this panel; set yours from a sweep |
| Camera | 8MP USB UVC webcam, Realtek `0bda:5785`, 120-degree lens, MJPEG up to 3264x2448 (used at 2048x1536), on a micro-USB OTG adapter. The Pi Camera Module 3 was abandoned: the Zero's CSI ribbon connector was too unreliable |
| ADC | ADS1115 16-bit I2C ADC, address `0x48` (ADDR to GND) |
| Soil moisture | 2x HW-390-class capacitive probes on ADS1115 A0/A1, powered from 3.3 V so output can't exceed the ADC supply |
| Air sensor | BME280 (temp / RH / pressure), I2C `0x76`. A BMP280 also works; the code detects BME vs BMP by chip ID and drops humidity gracefully |
| Light sensor | BH1750 lux sensor, I2C `0x23` |
| Soil temp | DS18B20 1-Wire probe module (onboard 4.7 k pull-up), auto-discovered by serial |
| MOSFETs | 3x D4184 (XY-MOS) modules: two pumps and the fan, each with a 1N5819 flyback diode across the load |
| Pumps | 2x 5 V USB submersible pumps on a **separate** 5 V supply, common ground with the Pi. Only one runs at a time (they share the supply); enforced in software |
| Floats | 2x normally-open float switches, one per tray, wired so rising water OPENS the switch (open = "full" = stop, which is also the broken-wire state, so a cut wire fails safe) |
| Reservoir level | 2x XKC-Y23A-**NPN** non-contact sensors strapped outside the source bucket (low = minimum-safe height, high = near the rim). Sense through ~13 mm of non-metal wall; nothing touches the water. Buy the NPN version, not -V |
| Fan | 5 V case fan on the pump supply, software PWM with a minimum-speed floor (cheap fans stall below ~25%) |
| Heat mat (optional) | Any seedling mat. Either on its own plug-in thermostat, with the Pi only watching soil temperature through its DS18B20, or on a TP-Link Kasa smart plug with the Pi as the thermostat (see [Heat mat](features.md#heat-mat)). A plug-in thermostat between the plug and the mat is a sensible backstop either way: a Pi that loses power leaves the plug as it was |

All sensors run on 3.3 V only, never 5 V. All grounds (Pi, pump supply, light
supply) must be tied together.

## Pinout (BCM)

| Signal | GPIO | Physical pin | Notes |
| --- | --- | --- | --- |
| Light PWM | 18 | 12 | Hardware PWM channel 0, 1 kHz, to the 5V panel MOSFET gate |
| Dim line | 19 | 35 | Hardware PWM channel 1, through the PC817 to the AC fixture's dim input (inverted) |
| Pump tray 1 | 24 | 18 | To that pump's MOSFET gate (separate 5 V brick + common ground) |
| Pump tray 2 | 26 | 37 | Second pump MOSFET gate, same supply rules |
| Fan | 20 | 38 | Fan MOSFET gate; 5 V fan on the pump supply, flyback across the fan |
| Float tray 1 | 23 | 16 | Internal pull-up; other leg to GND (`FLOAT_ENABLED` in `sensors.py`) |
| Float tray 2 | 22 | 15 | Internal pull-up; other leg to GND |
| Reservoir high | 17 | 11 | XKC-Y23A OUT (near-rim sensor); internal pull-up |
| Reservoir low | 27 | 13 | XKC-Y23A OUT (minimum-safe-height sensor) |
| I2C SDA | 2 | 3 | Shared: ADS1115, BME280, BH1750 |
| I2C SCL | 3 | 5 | Same three devices |
| 1-Wire | 4 | 7 | DS18B20 soil temp (module has its own 4.7k pull-up) |

**GPIO25 (pin 22) is dead on the author's board.** Every assignment is
env-overridable precisely so a bad pin means one line in `.env`, not a code edit.

Pin overrides live in `.env` next to the app, written by `scripts/setup.sh` and
read by the systemd unit:

| Variable | Example | Notes |
| --- | --- | --- |
| `GROWLIGHT_LIGHT_PIN` | `18` | 5V panel. Hardware PWM: **only 18 or 19** |
| `GROWLIGHT_DIM_PIN` | `19` | AC fixture dim line; the other hardware PWM pin, or blank for none |
| `GROWLIGHT_PUMP_PINS` | `1:24,2:26` | One entry per tray with a pump |
| `GROWLIGHT_FLOAT_PINS` | `1:23,2:22` | One entry per tray with a float |
| `GROWLIGHT_FAN_PIN` | `20` | Any free GPIO (software PWM) |
| `GROWLIGHT_RESERVOIR_PINS` | `low:27,high:17` | Reservoir level sensors; `none` if unwired |
| `GROWLIGHT_RESERVOIR_INVERT` | `1` | Set only if the bench test reads backwards (some units invert) |
| `ANTHROPIC_API_KEY` | | Enables the daily AI report |
| `DISCORD_WEBHOOK` | | Enables threshold alerts |
| `GROWLIGHT_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `GROWLIGHT_LOG_FILE` | | Adds a rotating log file (in the app folder: the service cannot write elsewhere) |
| `GROWLIGHT_TRUSTED_PROXY` | | IP address of a reverse proxy whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed ([install.md](install.md#reverse-proxy)) |
| `GROWLIGHT_LOGIN_HTTPS_ONLY` | | `1`: a password is accepted over HTTPS only (needs `GROWLIGHT_TRUSTED_PROXY`); the plain-http address is read-only |

`deploy/env.example` documents every key. Re-running setup keeps any key it does
not ask about.

Answer `none` to any hardware prompt you do not have; that device is then never
claimed, its dashboard controls stay hidden, and re-running setup offers `none`
again rather than reverting to the suggested pin. Wire it up later and re-run
setup to enable it.

Edit `.env` and restart to apply; the journal prints the pins in use at startup.
`.env` is gitignored and written mode 600 because it holds secrets.

The ADS1115 runs off 3.3 V (pin 1) and GND (pin 9), with ADDR to GND for
address 0x48. Two capacitive soil probes go on A0 (tray 1) and A1 (tray 2),
powered from the same 3.3 V rail so their output can't exceed the ADC supply.
Enable I2C with `raspi-config` (or `dtparam=i2c_arm=on`) and confirm the board
shows up: `i2cdetect -y 1` should list `48`.

Boot settings (both PWM channels, I2C at 50 kHz, 1-Wire, UART) live in
`deploy/boot-config.txt`; `scripts/setup.sh` writes them into
`/boot/firmware/config.txt`:

```
enable_uart=1
dtparam=i2c_arm=on,i2c_arm_baudrate=50000
dtoverlay=w1-gpio
dtoverlay=pwm-2chan,pin=18,func=2,pin2=19,func2=2
```

Keep the I2C bus at 50 kHz: at 400 kHz the BH1750 at the end of its cable run
failed intermittently with `[Errno 5] Input/output error`. Confirm the running speed
with `od -An -tu4 --endian=big /sys/class/i2c-adapter/i2c-1/of_node/clock-frequency`.

**Float fail-safe.** Mount the float so that rising water *opens* the switch. Open
(reads "full") means stop, which is also the broken-wire state, so a disconnected
float fails safe by refusing to pump.

**Pump siphoning.** A float only cuts the pump; it can't stop a siphon. Either keep
the discharge above the water line or drill a ~1.5 mm vent hole at the top of the
tubing run.

There are no wiring diagrams in the repo yet; the pinout table above and the
notes in this section are the reference.

## Device and platform

- Pi health tiles: CPU temp, load, memory, disk, uptime, power/throttling
  flags, core voltage, host/IP, wifi signal.
- Read-only dashboard until login; secrets are redacted from every
  unauthenticated response. Settings saves are per-field validated: valid
  fields apply and invalid ones come back by name, so one bad field never
  silently discards a form.
- Every pin is env-overridable and every sensor optional: anything not wired is
  probed once, disabled gracefully, and its UI hides. Static assets are
  versioned by mtime, so deploys never need a hard refresh.
