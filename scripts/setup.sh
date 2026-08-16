#!/usr/bin/env bash
# Environment provisioner for the grow light controller.
#
# This sets up the BOX (packages, venv, PWM overlay, swap, systemd service,
# shell convenience). It does NOT contain the application code -- that lives
# in the repo next to this script. Run it from a checkout:
#
#   git clone <repo> ~/growlight && cd ~/growlight && bash scripts/setup.sh
#
# Safe to re-run any time (idempotent). Preserves config.json and growlight.db.

set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Run as your normal user, not root. The script uses sudo where needed."
  exit 1
fi

# Repo root is one level up from scripts/. The app already lives there (checkout).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$REPO_ROOT"
RUN_USER="$(whoami)"
NEED_REBOOT=0

# Sanity: the app code must be present. This script no longer ships it.
if [[ ! -f "$APP_DIR/growlight.py" ]]; then
  echo "ERROR: growlight.py not found in $APP_DIR"
  echo "Run this from a repo checkout: git clone <repo> ~/growlight"
  exit 1
fi
echo "==> App directory: $APP_DIR  (user: $RUN_USER)"

echo "==> [1/7] System packages"
sudo apt-get update
# i2c-tools provides i2cdetect, which is how you confirm a sensor is wired
# correctly before blaming the software for not seeing it.
sudo apt-get install -y python3-venv python3-pip rpicam-apps ffmpeg i2c-tools

echo "==> [2/7] Boot config: PWM, I2C and 1-Wire"
CONFIG_TXT=/boot/firmware/config.txt
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT=/boot/config.txt

# Each line is required by a different part of the system:
#   pwm      - hardware dimming of the light on GPIO18
#   i2c_arm  - ADS1115 soil probes, BME/BMP280 air, BH1750 lux
#   w1-gpio  - DS18B20 soil temperature on GPIO4
add_boot_line() {
  local line="$1" why="$2"
  if grep -qxF "$line" "$CONFIG_TXT"; then
    echo "    present: $line"
  else
    echo "$line" | sudo tee -a "$CONFIG_TXT" > /dev/null
    echo "    added:   $line   ($why)"
    NEED_REBOOT=1
  fi
}
add_boot_line "dtoverlay=pwm,pin=18,func=2" "light dimming"
add_boot_line "dtparam=i2c_arm=on"          "soil probes, air and lux sensors"
add_boot_line "dtoverlay=w1-gpio,gpiopin=4" "DS18B20 soil temperature"

echo "==> [3/7] Swap (>=1G so renders don't OOM on a 512MB board)"
SWAPFILE=/swapfile
cur_kb="$(awk '/SwapTotal/{print $2}' /proc/meminfo)"
if [[ "${cur_kb:-0}" -lt 1000000 && ! -f "$SWAPFILE" ]]; then
  sudo fallocate -l 2G "$SWAPFILE"
  sudo chmod 600 "$SWAPFILE"
  sudo mkswap "$SWAPFILE" >/dev/null
  sudo swapon "$SWAPFILE"
  grep -qxF "$SWAPFILE none swap sw 0 0" /etc/fstab \
    || echo "$SWAPFILE none swap sw 0 0" | sudo tee -a /etc/fstab > /dev/null
  echo "    created $SWAPFILE (2G) and added to /etc/fstab"
else
  echo "    swap already adequate; leaving it alone"
fi

echo "==> [4/7] Python venv and dependencies"
[[ -d "$APP_DIR/venv" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
echo "    core ..."
"$APP_DIR/venv/bin/pip" install --quiet astral rpi-hardware-pwm flask gpiozero lgpio

# Sensor drivers. Each is optional at runtime: sensors.py probes for the device
# and disables that reading if either the library or the hardware is absent, so
# a failure here degrades one sensor rather than the whole install.
echo "    sensors (ADS1115, BME/BMP280, BH1750) ..."
"$APP_DIR/venv/bin/pip" install --quiet \
  adafruit-circuitpython-ads1x15 \
  adafruit-circuitpython-bme280 \
  adafruit-circuitpython-bmp280 \
  adafruit-circuitpython-bh1750 \
  adafruit-extended-bus \
  || echo "    WARNING: sensor libraries failed to install; probes/air/lux will be off"

echo "    optional: grid auto-detect (opencv) ..."
"$APP_DIR/venv/bin/pip" install --quiet opencv-python-headless numpy \
  || echo "    opencv unavailable; grid auto-detect off, manual placement still works"

echo "==> [5/7] systemd service"
sudo tee /etc/systemd/system/growlight.service > /dev/null << UNIT
[Unit]
Description=Grow light controller and dashboard
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/growlight.py
WorkingDirectory=$APP_DIR
Restart=always
RestartSec=5
User=$RUN_USER

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable growlight

echo "==> [6/7] Shell convenience (venv auto-activate)"
LINE="source $APP_DIR/venv/bin/activate"
grep -qxF "$LINE" "$HOME/.bashrc" || echo "$LINE" >> "$HOME/.bashrc"

echo "==> [7/7] Hardware check"
# Report what is actually detected. This is the difference between "the code is
# broken" and "the sensor is not wired", which is the first question every time.
if [[ "$NEED_REBOOT" -eq 1 ]]; then
  echo "    skipped: buses are enabled but need the reboot below first"
else
  if command -v i2cdetect >/dev/null && [[ -e /dev/i2c-1 ]]; then
    FOUND="$(i2cdetect -y 1 2>/dev/null | tail -n +2 | grep -oE '[0-9a-f]{2}' | tr '\n' ' ')"
    echo "    I2C devices: ${FOUND:-none found}"
    for pair in "48:ADS1115 soil probes" "76:BME/BMP280 air" "77:BME/BMP280 air (alt)" "23:BH1750 lux" "5c:BH1750 lux (alt)"; do
      addr="${pair%%:*}"; name="${pair#*:}"
      case " $FOUND " in *" $addr "*) echo "      $addr  $name";; esac
    done
  else
    echo "    I2C bus not available (is dtparam=i2c_arm=on set?)"
  fi
  if compgen -G "/sys/bus/w1/devices/28-*" > /dev/null; then
    echo "    1-Wire: $(ls -d /sys/bus/w1/devices/28-* | wc -l) DS18B20 sensor(s)"
  else
    echo "    1-Wire: no DS18B20 found (check GPIO4 wiring and the pull-up)"
  fi
fi

echo
echo "=================================================="
if [[ "$NEED_REBOOT" -eq 1 ]]; then
  echo "PWM overlay was just added: REBOOT REQUIRED."
  echo "Run:  sudo reboot   (the service is enabled and starts on boot)"
else
  sudo systemctl restart growlight
  sleep 2
  IP="$(hostname -I | awk '{print $1}')"
  echo "Done. Dashboard:  http://$IP:5000"
  systemctl --no-pager --full status growlight || true
fi
echo "--------------------------------------------------"
if [[ -f "$APP_DIR/config.json" ]] && \
   grep -Eq '"password_hash"[[:space:]]*:[[:space:]]*"[^"]' "$APP_DIR/config.json"; then
  echo "Dashboard login: password is SET. To change it:"
else
  echo "Dashboard login: NONE (dashboard is open to anyone who can reach it)."
  echo "To require login, set a password:"
fi
echo "    $APP_DIR/venv/bin/python $APP_DIR/scripts/set_password.py"
echo "Logs:  journalctl -u growlight -f"
echo "Update flow:  git pull && sudo systemctl restart growlight"
echo "=================================================="
