#!/usr/bin/env bash
# Check a USB hub before trusting it with the camera and the touchscreen.
#
#   bash scripts/usbcheck.sh
#
# Pass/fail on what matters: the camera's link speed (it must be 480 Mbit/s;
# behind a hub that drops it to 12 Mbit/s the camera offers only tiny sizes,
# which is what happened on 4 Sep), whether the camera still offers the size
# the app is set to capture, whether a touchscreen is seen, and undervoltage
# since boot. Read-only: it changes nothing and does not stop the app.

set -uo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fails=0
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }
info() { printf '        %s\n' "$1"; }

cfg() {   # cfg KEY DEFAULT: a value from config.json
  python3 - "$APP_DIR/config.json" "$1" "$2" <<'EOF' 2>/dev/null || echo "$3"
import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2], sys.argv[3]))
except Exception:
    print(sys.argv[3])
EOF
}

dev="$(cfg usb_device /dev/video0)"
want_w="$(cfg usb_width 0)"
want_h="$(cfg usb_height 0)"

echo "USB tree:"
lsusb -t 2>/dev/null | sed 's/^/  /'
echo

echo "Camera ($dev):"
if [[ ! -e "$dev" ]]; then
  fail "$dev not present: the camera is not detected at all"
else
  node="$(readlink -f "/sys/class/video4linux/$(basename "$dev")/device" 2>/dev/null)"
  speed=""
  p="$node"
  for _ in 1 2 3 4; do
    [[ -f "$p/speed" ]] && { speed="$(cat "$p/speed")"; break; }
    p="$(dirname "$p")"
  done
  if [[ -z "$speed" ]]; then
    fail "could not read the camera's USB speed"
  elif [[ "${speed%%.*}" -ge 480 ]]; then
    pass "camera link speed ${speed} Mbit/s"
  else
    fail "camera link speed ${speed} Mbit/s (must be 480): this hub or cable runs it at USB 1.1 speed"
  fi
  sizes="$(v4l2-ctl -d "$dev" --list-formats-ext 2>/dev/null \
           | awk '/MJPG/{m=1;next} /\[[0-9]+\]:/{m=0} m && /Size/{print $3}' | sort -t x -k1,1n -u | tr '\n' ' ')"
  if [[ -z "$sizes" ]]; then
    fail "the camera offers no MJPEG sizes"
  else
    info "MJPEG sizes: $sizes"
    if [[ "$want_w" != 0 && "$want_h" != 0 ]]; then
      if grep -qw "${want_w}x${want_h}" <<< "$sizes"; then
        pass "the configured size ${want_w}x${want_h} is offered"
      else
        fail "the configured size ${want_w}x${want_h} is not offered"
      fi
    fi
  fi
fi
echo

echo "Touchscreen:"
# a touchscreen reports multitouch positions: ABS_MT_POSITION_X is bit 53
touch_dev="$(python3 - <<'EOF' 2>/dev/null
import re
try:
    blocks = open("/proc/bus/input/devices").read().strip().split("\n\n")
except Exception:
    blocks = []
for b in blocks:
    name = re.search(r'N: Name="(.*)"', b)
    absm = re.search(r"B: ABS=([0-9a-f ]+)", b)
    if not name or not absm:
        continue
    bits = int("".join(w.zfill(16) for w in absm.group(1).split()), 16)
    if bits >> 53 & 1:
        print(name.group(1))
        break
EOF
)"
if [[ -n "$touch_dev" ]]; then
  pass "touch input found: $touch_dev"
else
  info "no touch input seen (fine if the touchscreen is not plugged in yet)"
fi
echo

echo "Power:"
thr="$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
if [[ -z "$thr" ]]; then
  info "vcgencmd not available"
elif (( (thr & 0x50000) == 0 )); then
  pass "no undervoltage or throttling since boot ($thr)"
else
  fail "undervoltage or throttling since boot ($thr): power the hub from its own supply"
fi
echo

if (( fails == 0 )); then
  echo "Result: PASS. This hub is fine for the camera and the touchscreen."
else
  echo "Result: FAIL ($fails). Do not use this setup as is; see the lines above."
fi
exit $(( fails > 0 ))
