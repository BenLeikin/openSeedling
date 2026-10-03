#!/usr/bin/env bash
# The dashboard on a touchscreen plugged into this Pi's HDMI port.
#
#   bash scripts/kiosk.sh install [--rotate 0|90|180|270]   default 90
#   bash scripts/kiosk.sh touch-rotate 0|90|180|270         only if taps land wrong
#   bash scripts/kiosk.sh status                            running? memory used?
#   bash scripts/kiosk.sh remove                            back to headless
#
# Opt-in and separate from setup.sh: it installs a compositor and a browser
# (cage, cog, wlr-randr) and gives tty1 to the screen. Remove undoes all of it
# except the packages. The browser runs under a memory ceiling and is the first
# thing killed if the Pi runs out of memory, so it cannot take the controller
# down (see deploy/growlight-kiosk.service).

set -euo pipefail

main() {
  local cmd="${1:-}"; shift || true
  if [[ $EUID -eq 0 ]]; then
    echo "Run this as your normal user, not with sudo; it asks when it needs to."
    exit 1
  fi
  APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  RUN_USER="$(whoami)"
  : "${UNIT_PATH:=/etc/systemd/system/growlight-kiosk.service}"
  : "${PAM_PATH:=/etc/pam.d/cage}"
  : "${TOUCH_RULE:=/etc/udev/rules.d/99-openseedling-touch.rules}"
  case "$cmd" in
    install)      install_kiosk "$@" ;;
    touch-rotate) touch_rotate "${1:?touch-rotate needs 0, 90, 180 or 270}" ;;
    status)       status ;;
    remove)       remove_kiosk ;;
    *) sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
  esac
}

valid_rotation() { [[ "$1" =~ ^(0|90|180|270)$ ]]; }

set_trust() {       # set_trust 1|0: the screen counts as signed in (GROWLIGHT_TRUST_LOCALHOST in .env)
  local env="$APP_DIR/.env"
  touch "$env"
  if grep -q '^GROWLIGHT_TRUST_LOCALHOST=' "$env"; then
    sed -i "s/^GROWLIGHT_TRUST_LOCALHOST=.*/GROWLIGHT_TRUST_LOCALHOST=$1/" "$env"
  else
    echo "GROWLIGHT_TRUST_LOCALHOST=$1" >> "$env"
  fi
  sudo systemctl restart growlight
}

render_unit() {     # render_unit ROTATION: the unit with this box's paths filled in
  sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@RUN_USER@|$RUN_USER|g" -e "s|@ROTATE@|$1|g" \
    "$APP_DIR/deploy/growlight-kiosk.service"
}

install_kiosk() {
  local rot=90
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --rotate) rot="${2:?--rotate needs 0, 90, 180 or 270}"; shift ;;
      *) echo "unknown option: $1"; exit 2 ;;
    esac
    shift
  done
  valid_rotation "$rot" || { echo "--rotate must be 0, 90, 180 or 270"; exit 2; }

  echo "==> packages (cage, cog, wlr-randr)"
  sudo apt-get update -qq
  if ! sudo apt-get install -y -qq cage cog wlr-randr; then
    echo "ERROR: could not install cage, cog and wlr-randr from apt; nothing else changed."
    exit 1
  fi

  echo "==> groups for $RUN_USER (display, GPU and touch devices)"
  local g
  for g in video render input; do
    getent group "$g" > /dev/null && sudo usermod -aG "$g" "$RUN_USER"
  done

  echo "==> login session for the screen ($PAM_PATH)"
  if [[ ! -f "$PAM_PATH" ]]; then
    printf '%s\n' "auth     required pam_unix.so nullok" \
                  "account  required pam_unix.so" \
                  "session  required pam_unix.so" \
                  "session  required pam_systemd.so" | sudo tee "$PAM_PATH" > /dev/null
  fi

  echo "==> the screen may change settings without signing in (GROWLIGHT_TRUST_LOCALHOST)"
  set_trust 1

  echo "==> service (portrait rotation $rot)"
  render_unit "$rot" | sudo tee "$UNIT_PATH" > /dev/null
  chmod +x "$APP_DIR/scripts/kiosk-session.sh"
  # tty1 belongs to the screen now: no login prompt drawn under the dashboard
  sudo systemctl mask --quiet getty@tty1.service
  sudo systemctl daemon-reload
  sudo systemctl enable --quiet growlight-kiosk
  sudo systemctl restart growlight-kiosk

  echo
  echo "Installed. The dashboard should appear within about a minute."
  echo "  wrong way up:          bash scripts/kiosk.sh install --rotate 0, 90, 180 or 270"
  echo "  taps land in the wrong place:  bash scripts/kiosk.sh touch-rotate $rot"
  echo "  memory it is using:    bash scripts/kiosk.sh status"
}

touch_rotate() {
  local rot="$1" m
  valid_rotation "$rot" || { echo "rotation must be 0, 90, 180 or 270"; exit 2; }
  case "$rot" in
    0)   sudo rm -f "$TOUCH_RULE" ;;
    90)  m="0 1 0 -1 0 1" ;;
    180) m="-1 0 1 0 -1 1" ;;
    270) m="0 -1 1 1 0 0" ;;
  esac
  if [[ "$rot" != 0 ]]; then
    printf '%s\n' "# openSeedling: turn touch input to match the portrait screen (kiosk.sh touch-rotate)" \
      "ENV{ID_INPUT_TOUCHSCREEN}==\"1\", ENV{LIBINPUT_CALIBRATION_MATRIX}=\"$m\"" \
      | sudo tee "$TOUCH_RULE" > /dev/null
  fi
  sudo udevadm control --reload
  sudo udevadm trigger --subsystem-match=input
  sudo systemctl restart growlight-kiosk
  echo "Touch rotation set to $rot. If it is now worse, run: bash scripts/kiosk.sh touch-rotate 0"
}

status() {
  systemctl --no-pager status growlight-kiosk | head -12 || true
  echo
  local cur max
  cur="$(systemctl show growlight-kiosk -p MemoryCurrent --value 2>/dev/null || true)"
  max="$(systemctl show growlight-kiosk -p MemoryMax --value 2>/dev/null || true)"
  [[ "$cur" =~ ^[0-9]+$ ]] && echo "screen memory: $((cur / 1048576)) MB of $((max / 1048576)) MB allowed"
  free -m | sed -n '1,2p'
  echo "restarts: $(systemctl show growlight-kiosk -p NRestarts --value 2>/dev/null)"
}

remove_kiosk() {
  sudo systemctl disable --now --quiet growlight-kiosk 2>/dev/null || true
  sudo rm -f "$UNIT_PATH" "$TOUCH_RULE"
  sudo systemctl unmask --quiet getty@tty1.service
  sudo systemctl daemon-reload
  sudo systemctl start getty@tty1.service 2>/dev/null || true
  set_trust 0
  echo "Removed. The packages (cage, cog, wlr-randr) stay; remove them with apt if you like."
}

main "$@"
