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

# ---------------------------------------------------------------------------
# Interactive configuration.
#
# Answers are written to an env file that the systemd unit reads, so nothing
# here edits Python. Re-running keeps existing answers as the defaults, and
# --defaults skips every prompt (useful for reinstalling on a known box).
# ---------------------------------------------------------------------------
ENV_FILE="$APP_DIR/.env"
declare -A CFG
if [[ -f "$ENV_FILE" ]]; then
  while IFS='=' read -r k v; do
    [[ "$k" =~ ^[A-Z_]+$ ]] && CFG["$k"]="$v"
  done < "$ENV_FILE"
  # an explicitly empty hardware key means "none"; keep it rather than
  # letting the suggested default creep back in on the next run
  # An explicitly empty hardware key means the user answered "none". Record
  # that separately: an empty string cannot itself be distinguished from
  # "unset" when ask() falls back to its suggested default.
  for _k in GROWLIGHT_PUMP_PINS GROWLIGHT_FLOAT_PINS GROWLIGHT_FAN_PIN; do
    if grep -q "^$_k=$" "$ENV_FILE" 2>/dev/null; then
      CFG["$_k"]=""; CFG["${_k}__NONE"]=1
    fi
  done
fi

NONINTERACTIVE=0
[[ "${1:-}" == "--defaults" ]] && NONINTERACTIVE=1
[[ -t 0 ]] || NONINTERACTIVE=1     # piped input: do not block waiting for a human

ask() {                            # ask VAR "prompt" "default" ["hint"]
  local var="$1" prompt="$2" hint="${4:-}" reply def
  if [[ -n "${CFG[${1}__NONE]:-}" ]]; then
    def="none"                     # previously opted out; offer that again
  else
    def="${CFG[$1]:-$3}"
  fi
  if [[ "$NONINTERACTIVE" -eq 1 ]]; then CFG["$var"]="$def"; return; fi
  [[ -n "$hint" ]] && echo "    $hint"
  read -r -p "    $prompt [$def]: " reply
  CFG["$var"]="${reply:-$def}"
}

ask_secret() {                     # ask_secret VAR "prompt" "hint"
  local var="$1" prompt="$2" hint="${3:-}" reply cur="${CFG[$1]:-}"
  if [[ "$NONINTERACTIVE" -eq 1 ]]; then CFG["$var"]="$cur"; return; fi
  [[ -n "$hint" ]] && echo "    $hint"
  if [[ -n "$cur" ]]; then
    read -r -p "    $prompt [keep existing]: " reply
    CFG["$var"]="${reply:-$cur}"
  else
    read -r -p "    $prompt [skip]: " reply
    CFG["$var"]="$reply"
  fi
}

if [[ "$NONINTERACTIVE" -eq 1 ]]; then
  echo "==> Configuration: using defaults (non-interactive)"
else
  echo
  echo "=================================================="
  echo " Configuration. Press Enter to accept each default."
  echo " Everything here can be changed later in $ENV_FILE."
  echo "=================================================="
  echo
  echo "-- GPIO pins (BCM numbering) --"
fi

ask GROWLIGHT_LIGHT_PIN "Light PWM pin (18 or 19 only)" "18" \
  "Only GPIO18 and GPIO19 have hardware PWM. 18 = header pin 12, 19 = pin 35."
case "${CFG[GROWLIGHT_LIGHT_PIN]}" in
  18|19) ;;
  *) echo "    GPIO${CFG[GROWLIGHT_LIGHT_PIN]} cannot do hardware PWM; using 18"
     CFG[GROWLIGHT_LIGHT_PIN]=18 ;;
esac

# Optional hardware. "none" is a first-class answer: the variable is written
# empty, the device is never claimed, and its controls stay hidden in the UI.
ask GROWLIGHT_PUMP_PINS "Pump pins, tray:pin (or 'none')" "1:24,2:26" \
  "One entry per tray with a pump. 24 = pin 18, 26 = pin 37. Answer 'none' if you have no pumps."
ask GROWLIGHT_FLOAT_PINS "Float switch pins, tray:pin (or 'none')" "1:23,2:22" \
  "23 = pin 16, 22 = pin 15. Other leg to ground; rising water should OPEN the switch. 'none' if unwired."
ask GROWLIGHT_FAN_PIN "Fan pin (or 'none')" "20" \
  "20 = pin 38. Any free GPIO; software PWM. 'none' if you have no fan."

# normalise the opt-outs to an empty value
for _k in GROWLIGHT_PUMP_PINS GROWLIGHT_FLOAT_PINS GROWLIGHT_FAN_PIN; do
  case "${CFG[$_k]:-}" in
    none|NONE|None|no|n|-|skip) CFG["$_k"]=""; echo "    $_k: none" ;;
  esac
done

if [[ "$NONINTERACTIVE" -eq 0 ]]; then
  echo
  echo "-- Optional integrations (Enter to skip) --"
fi
ask_secret ANTHROPIC_API_KEY "Anthropic API key" \
  "Enables the daily AI garden report. Skip if you do not want it."
ask_secret DISCORD_WEBHOOK "Discord webhook URL" \
  "Enables threshold alerts. Skip to leave alerts off."

# I2C/1-Wire addresses are auto-detected at runtime, so there is nothing to ask.
umask 077
{
  echo "# Written by scripts/setup.sh. Read by the systemd unit."
  echo "# Edit here and 'sudo systemctl restart growlight' to apply."
  # Hardware keys are written even when empty: that is how "I have no pump"
  # persists across a re-run instead of reverting to the suggested default.
  for k in GROWLIGHT_LIGHT_PIN GROWLIGHT_PUMP_PINS GROWLIGHT_FLOAT_PINS GROWLIGHT_FAN_PIN; do
    echo "$k=${CFG[$k]:-}"
  done
  for k in ANTHROPIC_API_KEY DISCORD_WEBHOOK; do
    [[ -n "${CFG[$k]:-}" ]] && echo "$k=${CFG[$k]}"
  done
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
umask 022
echo "    saved to $ENV_FILE (mode 600)"

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
add_boot_line "dtoverlay=pwm,pin=${CFG[GROWLIGHT_LIGHT_PIN]},func=2" "light dimming"
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
EnvironmentFile=-$APP_DIR/.env
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
