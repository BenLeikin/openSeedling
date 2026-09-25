#!/usr/bin/env bash
# openSeedling installer and box provisioner.
#
# Fresh Raspberry Pi, one command (as your normal user, not root):
#
#   curl -fsSL https://raw.githubusercontent.com/BenLeikin/openSeedling/main/scripts/setup.sh | bash
#
# That clones the app to ~/growlight and runs this script from the checkout.
# From an existing checkout:
#
#   bash scripts/setup.sh              install or update, prompting where needed
#   bash scripts/setup.sh --defaults   no prompts; keep existing answers
#   bash scripts/setup.sh --check      report drift from deploy/, change nothing
#
# Options: --dir PATH (install location, default ~/growlight), --repo URL,
# --branch NAME, --force (continue on untested hardware without asking).
#
# Everything this changes on the box comes from deploy/ and requirements*.txt.
# Idempotent: re-running is how you update the box. It never overwrites
# config.json, growlight.db or .secret; any .env key it does not ask about is
# carried over unchanged. --check exits 1 when the box has drifted.

set -euo pipefail

REPO_URL_DEFAULT="https://github.com/BenLeikin/openSeedling.git"

# The whole script lives in main(), called on the last line, so a truncated
# download through `curl | bash` runs nothing at all.
main() {
  local MODE=apply NONINTERACTIVE=0 FORCE=0
  local INSTALL_DIR="$HOME/growlight" REPO_URL="$REPO_URL_DEFAULT" BRANCH=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --check)    MODE=check ;;
      --defaults) NONINTERACTIVE=1 ;;
      --force)    FORCE=1 ;;
      --dir)      INSTALL_DIR="${2:?--dir needs a path}"; shift ;;
      --repo)     REPO_URL="${2:?--repo needs a URL}"; shift ;;
      --branch)   BRANCH="${2:?--branch needs a name}"; shift ;;
      -h|--help)  sed -n '2,22p' "${BASH_SOURCE[0]}" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) echo "unknown option: $1 (try --help)"; exit 2 ;;
    esac
    shift
  done

  if [[ $EUID -eq 0 ]]; then
    echo "Run this as your normal user, not with sudo or as root."
    echo "It asks for your password when it needs sudo."
    exit 1
  fi

  # Prompts read the terminal directly, so they work under `curl | bash`,
  # where stdin is the script itself.
  if [[ "$NONINTERACTIVE" -eq 0 ]] && ! { : < /dev/tty; } 2>/dev/null; then
    NONINTERACTIVE=1
  fi

  # --- bootstrap: not inside a checkout yet ---------------------------------
  local self="${BASH_SOURCE[0]:-}" REPO_ROOT=""
  if [[ -n "$self" && -f "$self" ]]; then
    REPO_ROOT="$(cd "$(dirname "$self")/.." && pwd)"
  fi
  if [[ -z "$REPO_ROOT" || ! -f "$REPO_ROOT/growlight.py" ]]; then
    [[ "$MODE" == check ]] && { echo "--check needs an existing checkout"; exit 2; }
    bootstrap "$INSTALL_DIR" "$REPO_URL" "$BRANCH"
    local fwd=()
    [[ "$NONINTERACTIVE" -eq 1 ]] && fwd+=(--defaults)
    [[ "$FORCE" -eq 1 ]] && fwd+=(--force)
    exec bash "$INSTALL_DIR/scripts/setup.sh" "${fwd[@]}" < /dev/null
  fi

  provision
}

# --------------------------------------------------------------------------
# helpers

say()   { echo "$*"; }
ok()    { echo "    ok:    $*"; }
note()  { echo "    note:  $*"; }
drift() { echo "    DRIFT: $*"; DRIFT=$((DRIFT + 1)); }
STEP=0
step()  { STEP=$((STEP + 1)); echo "==> [$STEP] $*"; }
strip_comments() { grep -vE '^[[:space:]]*(#|$)' "$1" || true; }

prompt() {        # prompt VAR "question" "default"; reads the terminal
  local __var="$1" q="$2" def="${3:-}" reply=""
  if [[ "$NONINTERACTIVE" -eq 1 ]]; then printf -v "$__var" '%s' "$def"; return; fi
  read -r -p "    $q [$def]: " reply < /dev/tty || true
  printf -v "$__var" '%s' "${reply:-$def}"
}

confirm() {       # confirm "question" y|n -> 0 for yes
  local q="$1" def="${2:-y}" reply
  if [[ "$NONINTERACTIVE" -eq 1 ]]; then [[ "$def" == y ]]; return; fi
  local hint="[Y/n]"; [[ "$def" == n ]] && hint="[y/N]"
  read -r -p "    $q $hint: " reply < /dev/tty || true
  reply="${reply:-$def}"
  [[ "$reply" =~ ^[Yy] ]]
}

bootstrap() {     # bootstrap DIR URL BRANCH: get a checkout at DIR
  local dir="$1" url="$2" branch="$3"
  echo "==> openSeedling installer"
  if ! command -v git > /dev/null; then
    echo "    installing git"
    sudo apt-get update -qq
    sudo apt-get install -y -qq git
  fi
  if [[ -d "$dir/.git" ]]; then
    echo "    $dir is already a checkout; updating it"
    git -C "$dir" pull --ff-only
  elif [[ -e "$dir" && -n "$(ls -A "$dir" 2>/dev/null)" ]]; then
    echo "ERROR: $dir exists and is not a git checkout. Move it aside or pass --dir."
    exit 1
  else
    echo "    cloning $url into $dir"
    git clone ${branch:+--branch "$branch"} "$url" "$dir"
  fi
}

# --------------------------------------------------------------------------
provision() {
  APP_DIR="$REPO_ROOT"
  DEPLOY="$REPO_ROOT/deploy"
  RUN_USER="$(whoami)"
  VENV="$APP_DIR/venv"
  NEED_REBOOT=0
  DRIFT=0
  FIRST_INSTALL=0
  [[ -f "$APP_DIR/config.json" ]] || FIRST_INSTALL=1

  # System paths; overridable only so the script can be tested off the Pi.
  : "${CONFIG_TXT:=/boot/firmware/config.txt}"
  [[ -f "$CONFIG_TXT" ]] || CONFIG_TXT=/boot/config.txt
  : "${UNIT_PATH:=/etc/systemd/system/growlight.service}"
  : "${MODULES_FILE:=/etc/modules}"

  for f in deploy/apt-packages.txt deploy/boot-config.txt deploy/growlight.service \
           requirements.txt requirements-optional.txt; do
    [[ -f "$REPO_ROOT/$f" ]] || { echo "ERROR: $f missing from the checkout"; exit 1; }
  done
  echo "==> openSeedling setup: $APP_DIR  (user: $RUN_USER, mode: $MODE)"

  [[ "$MODE" == apply ]] && preflight
  step_packages
  step_env
  step_boot
  step_modules
  step_groups
  step_swap
  step_venv
  step_unit
  [[ "$MODE" == apply ]] && step_location
  step_shell
  step_hardware
  finish
}

# --------------------------------------------------------------------------
preflight() {
  step "Checking this Pi"
  local model arch codename=""
  model="$(tr -d '\0' 2>/dev/null < /proc/device-tree/model || true)"
  arch="$(uname -m)"
  [[ -r /etc/os-release ]] && codename="$(. /etc/os-release; echo "${VERSION_CODENAME:-}")"
  echo "    ${model:-unknown board}, $arch, ${codename:-unknown OS}"

  local problems=()
  [[ "$model" == Raspberry\ Pi* ]] || problems+=("not a Raspberry Pi; this script edits the Pi boot config")
  [[ "$model" == *"Pi 5"* ]] && problems+=("Pi 5 is untested: its PWM controller differs, so the light may not dim")
  [[ "$arch" == aarch64 ]] || problems+=("$arch is untested; use 64-bit Raspberry Pi OS, or pinned packages may build from source for hours")
  [[ "$codename" =~ ^(trixie|bookworm)$ ]] || problems+=("tested on Raspberry Pi OS Trixie; ${codename:-this OS} is untested")
  local free_mb
  free_mb="$(df -Pm "$APP_DIR" | awk 'NR==2{print $4}')"
  [[ "${free_mb:-0}" -ge 1500 ]] || problems+=("only ${free_mb} MB free; the install needs about 1.5 GB")

  if [[ ${#problems[@]} -gt 0 ]]; then
    local p; for p in "${problems[@]}"; do echo "    WARNING: $p"; done
    if [[ "$FORCE" -eq 0 ]] && ! confirm "Continue anyway?" n; then
      echo "Stopped. Re-run with --force to skip this check."; exit 1
    fi
  fi

  echo "    sudo is needed for packages, boot config and the service"
  sudo -v
}

# --------------------------------------------------------------------------
step_packages() {
  step "System packages (deploy/apt-packages.txt)"
  local APT_REQ=() APT_OPT=() missing=() missing_opt=() p
  while read -r p; do
    [[ "$p" == *\? ]] && APT_OPT+=("${p%\?}") || APT_REQ+=("$p")
  done < <(strip_comments "$DEPLOY/apt-packages.txt")
  for p in "${APT_REQ[@]}"; do is_installed "$p" || missing+=("$p"); done
  for p in "${APT_OPT[@]}"; do is_installed "$p" || missing_opt+=("$p"); done

  if [[ ${#missing[@]} -eq 0 && ${#missing_opt[@]} -eq 0 ]]; then
    ok "all $(( ${#APT_REQ[@]} + ${#APT_OPT[@]} )) installed"
  elif [[ "$MODE" == check ]]; then
    [[ ${#missing[@]} -gt 0 ]] && drift "not installed: ${missing[*]}"
    [[ ${#missing_opt[@]} -gt 0 ]] && note "optional, not installed: ${missing_opt[*]}"
  else
    sudo apt-get update -qq
    [[ ${#missing[@]} -gt 0 ]] && sudo apt-get install -y "${missing[@]}"
    for p in "${missing_opt[@]}"; do
      sudo apt-get install -y "$p" || echo "    $p not available on this release; skipped"
    done
  fi
  return 0
}
is_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }

# --------------------------------------------------------------------------
# .env: pins and secrets, read by the systemd unit.
declare -A CFG
ORIG_KEYS=()
HW_KEYS=(GROWLIGHT_DIM_PIN GROWLIGHT_PUMP_PINS GROWLIGHT_FLOAT_PINS
         GROWLIGHT_FAN_PIN GROWLIGHT_RESERVOIR_PINS)

is_none() { [[ "${1:-}" =~ ^(none|NONE|None|no|n|-|skip)$ ]]; }

ask() {                            # ask VAR "prompt" "default" ["hint"]
  local var="$1" q="$2" hint="${4:-}" def reply=""
  if [[ -n "${CFG[${1}__NONE]:-}" ]]; then def="none"; else def="${CFG[$1]:-$3}"; fi
  if [[ "$NONINTERACTIVE" -eq 1 ]]; then CFG["$var"]="$def"; return; fi
  [[ -n "$hint" ]] && echo "    $hint"
  read -r -p "    $q [$def]: " reply < /dev/tty || true
  CFG["$var"]="${reply:-$def}"
}

ask_secret() {                     # ask_secret VAR "prompt" "hint"
  local var="$1" q="$2" hint="${3:-}" reply="" cur="${CFG[$1]:-}"
  if [[ "$NONINTERACTIVE" -eq 1 ]]; then CFG["$var"]="$cur"; return; fi
  [[ -n "$hint" ]] && echo "    $hint"
  if [[ -n "$cur" ]]; then
    read -r -p "    $q [keep existing]: " reply < /dev/tty || true
    CFG["$var"]="${reply:-$cur}"
  else
    read -r -p "    $q [skip]: " reply < /dev/tty || true
    CFG["$var"]="$reply"
  fi
}

step_env() {
  step "Pins and secrets (.env)"
  local ENV_FILE="$APP_DIR/.env" line k
  if [[ -f "$ENV_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" =~ ^[A-Z_][A-Z0-9_]*= ]] || continue
      k="${line%%=*}"; CFG["$k"]="${line#*=}"; ORIG_KEYS+=("$k")
    done < "$ENV_FILE"
    # An explicitly empty hardware key means "none"; remember that so the
    # suggested default does not creep back in on a re-run.
    for k in "${HW_KEYS[@]}"; do
      grep -q "^$k=$" "$ENV_FILE" && { CFG["$k"]=""; CFG["${k}__NONE"]=1; }
    done
  fi

  if [[ "$MODE" == check ]]; then
    if [[ ! -f "$ENV_FILE" ]]; then
      drift ".env missing; run setup.sh to write it"
    else
      local perm; perm="$(stat -c %a "$ENV_FILE")"
      [[ "$perm" == 600 ]] && ok ".env present, mode 600" || drift ".env mode $perm, want 600"
    fi
    return 0
  fi

  if [[ "$NONINTERACTIVE" -eq 0 ]]; then
    echo "    Which GPIO (BCM numbering) each device is on. Press Enter for the"
    echo "    suggested pin, or type 'none' for anything you have not wired."
    echo "    deploy/env.example documents every key."
  fi
  ask GROWLIGHT_LIGHT_PIN "5V LED panel PWM pin (18 or 19)" "18" \
    "Only GPIO18 and GPIO19 have hardware PWM. 18 = header pin 12, 19 = pin 35."
  case "${CFG[GROWLIGHT_LIGHT_PIN]}" in
    18|19) ;;
    *) echo "    GPIO${CFG[GROWLIGHT_LIGHT_PIN]} cannot do hardware PWM; using 18"
       CFG[GROWLIGHT_LIGHT_PIN]=18 ;;
  esac
  local other_pwm=19; [[ "${CFG[GROWLIGHT_LIGHT_PIN]}" == 19 ]] && other_pwm=18
  ask GROWLIGHT_DIM_PIN "AC fixture dim pin ($other_pwm, or 'none')" "$other_pwm" \
    "Optocoupler to a dimmable AC fixture's 0-10V line. Must be the other PWM pin. 'none' without one."
  if ! is_none "${CFG[GROWLIGHT_DIM_PIN]}" && [[ -n "${CFG[GROWLIGHT_DIM_PIN]}" \
       && "${CFG[GROWLIGHT_DIM_PIN]}" != "$other_pwm" ]]; then
    echo "    the dim pin must be GPIO$other_pwm (the other PWM channel); using $other_pwm"
    CFG[GROWLIGHT_DIM_PIN]="$other_pwm"
  fi
  ask GROWLIGHT_PUMP_PINS "Pump pins, tray:pin (or 'none')" "1:24,2:26" \
    "One entry per tray with a pump. 24 = pin 18, 26 = pin 37."
  ask GROWLIGHT_FLOAT_PINS "Float switch pins, tray:pin (or 'none')" "1:23,2:22" \
    "23 = pin 16, 22 = pin 15. Other leg to ground; rising water should OPEN the switch."
  ask GROWLIGHT_FAN_PIN "Fan pin (or 'none')" "20" "20 = pin 38. Any free GPIO; software PWM."
  ask GROWLIGHT_RESERVOIR_PINS "Reservoir level pins, low:pin,high:pin (or 'none')" "low:27,high:17" \
    "XKC-Y23A non-contact sensors on the water reservoir. 27 = pin 13, 17 = pin 11."
  ask GROWLIGHT_KASA_HOST "Smart plug IP for an on/off AC light (or blank)" "" \
    "Only for a TP-Link Kasa plug switching the light. Leave blank otherwise."
  if [[ -n "${CFG[GROWLIGHT_KASA_HOST]:-}" ]]; then
    ask GROWLIGHT_KASA_USER "TP-Link account email (blank for older plugs)" "" \
      "Newer plug firmware needs account credentials even for local control."
    [[ -n "${CFG[GROWLIGHT_KASA_USER]:-}" ]] && \
      ask GROWLIGHT_KASA_PASS "TP-Link account password" "" "Stored in .env (mode 600, never committed)."
  fi
  if [[ -n "${CFG[GROWLIGHT_RESERVOIR_PINS]:-}" ]] && ! is_none "${CFG[GROWLIGHT_RESERVOIR_PINS]}"; then
    ask GROWLIGHT_RESERVOIR_INVERT "Reservoir sensors inverted? (1 or blank)" "" \
      "Blank for standard NPN units. Set 1 only if the dashboard reads them backwards."
  fi
  for k in "${HW_KEYS[@]}"; do
    if is_none "${CFG[$k]:-}"; then CFG["$k"]=""; echo "    $k: none"; fi
  done

  [[ "$NONINTERACTIVE" -eq 0 ]] && echo "    Optional integrations (Enter to skip):"
  ask_secret ANTHROPIC_API_KEY "Anthropic API key" "Enables the daily AI plant report."
  ask_secret DISCORD_WEBHOOK "Discord webhook URL" "Enables alerts to a Discord channel."

  local -A WRITTEN=()
  umask 077
  {
    echo "# Written by scripts/setup.sh; every key is documented in deploy/env.example."
    echo "# Edit here and 'sudo systemctl restart growlight' to apply."
    for k in GROWLIGHT_LIGHT_PIN "${HW_KEYS[@]}" GROWLIGHT_KASA_HOST; do
      echo "$k=${CFG[$k]:-}"; WRITTEN[$k]=1
    done
    for k in GROWLIGHT_KASA_USER GROWLIGHT_KASA_PASS GROWLIGHT_RESERVOIR_INVERT \
             ANTHROPIC_API_KEY DISCORD_WEBHOOK; do
      WRITTEN[$k]=1
      [[ -n "${CFG[$k]:-}" ]] && echo "$k=${CFG[$k]}"
    done
    for k in "${ORIG_KEYS[@]}"; do          # keys this script does not manage
      [[ -n "${WRITTEN[$k]:-}" ]] && continue
      echo "$k=${CFG[$k]}"; WRITTEN[$k]=1
    done
  } > "$ENV_FILE.tmp"
  chmod 600 "$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  umask 022
  echo "    saved to $ENV_FILE (mode 600)"
}

# --------------------------------------------------------------------------
step_boot() {
  step "Boot config ($CONFIG_TXT)"
  export BOOT_BLOCK="# BEGIN growlight (managed by scripts/setup.sh from deploy/boot-config.txt)
$(strip_comments "$DEPLOY/boot-config.txt")
# END growlight"
  local want
  want="$(awk '
    /^# BEGIN growlight/ { skip = 1; next }
    /^# END growlight/   { skip = 0; next }
    skip { next }
    /^[[:space:]]*(enable_uart=|dtparam=i2c_arm(_baudrate)?[=,]|dtoverlay=w1-gpio([,[:space:]]|$)|dtoverlay=pwm(-2chan)?([,[:space:]]|$))/ {
      print "#growlight# " $0; next
    }
    { print }
    END { print ENVIRON["BOOT_BLOCK"] }
  ' "$CONFIG_TXT")"

  if [[ "$want" == "$(cat "$CONFIG_TXT")" ]]; then
    ok "managed block current"
  elif [[ "$MODE" == check ]]; then
    drift "config.txt differs from deploy/boot-config.txt:"
    diff -u --label current --label wanted "$CONFIG_TXT" <(printf '%s\n' "$want") \
      | tail -n +3 | sed 's/^/      /' || true
  else
    local bak; bak="$CONFIG_TXT.growlight-$(date +%Y%m%d-%H%M%S)"
    sudo cp -p "$CONFIG_TXT" "$bak"
    printf '%s\n' "$want" | sudo tee "$CONFIG_TXT" > /dev/null
    echo "    updated; previous file saved as $bak"
    NEED_REBOOT=1
  fi

  local want_baud have_baud f=/sys/class/i2c-adapter/i2c-1/of_node/clock-frequency
  want_baud="$(grep -oE 'i2c_arm_baudrate=[0-9]+' <<< "$BOOT_BLOCK" | cut -d= -f2 || true)"
  if [[ -n "$want_baud" && -r "$f" ]]; then
    have_baud="$(od -An -tu4 --endian=big "$f" | tr -d ' ')"
    if [[ "$have_baud" == "$want_baud" ]]; then ok "I2C bus running at $have_baud Hz"
    else note "I2C bus running at $have_baud Hz, configured $want_baud Hz; reboot to apply"; fi
  fi
  return 0
}

step_modules() {
  step "Kernel modules ($MODULES_FILE)"
  if grep -qxF "i2c-dev" "$MODULES_FILE" 2>/dev/null; then
    ok "i2c-dev listed"
  elif [[ "$MODE" == check ]]; then
    drift "i2c-dev not in $MODULES_FILE"
  else
    echo "i2c-dev" | sudo tee -a "$MODULES_FILE" > /dev/null
    echo "    added i2c-dev"
    NEED_REBOOT=1
  fi
}

step_groups() {
  step "Groups for $RUN_USER"
  local missing=() g have=" $(id -nG "$RUN_USER") "
  for g in gpio i2c video; do
    getent group "$g" > /dev/null || continue
    [[ "$have" == *" $g "* ]] || missing+=("$g")
  done
  if [[ ${#missing[@]} -eq 0 ]]; then
    ok "in gpio, i2c, video"
  elif [[ "$MODE" == check ]]; then
    drift "$RUN_USER not in: ${missing[*]}"
  else
    sudo usermod -aG "$(IFS=,; echo "${missing[*]}")" "$RUN_USER"
    echo "    added to ${missing[*]}"
  fi
}

step_swap() {
  step "Swap"
  # Trixie's rpi-swap provides zram; leave any active swap alone. A box with
  # none gets a swapfile so a timelapse render cannot OOM a 512 MB board.
  local kb; kb="$(awk '/SwapTotal/{print $2}' /proc/meminfo)"
  if [[ "${kb:-0}" -gt 0 ]]; then
    ok "$(( kb / 1024 )) MB active"
  elif [[ "$MODE" == check ]]; then
    drift "no swap active"
  else
    sudo fallocate -l 1G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile > /dev/null
    sudo swapon /swapfile
    grep -qxF "/swapfile none swap sw 0 0" /etc/fstab \
      || echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab > /dev/null
    echo "    created /swapfile (1 GB)"
  fi
}

pin_check() {     # pin_check FILE -> prints mismatches, returns 1 if any
  "$VENV/bin/python" - "$1" << 'PY'
import sys
from importlib.metadata import version, PackageNotFoundError
bad = 0
for line in open(sys.argv[1]):
    line = line.split("#")[0].strip()
    if not line:
        continue
    name, _, want = line.partition("==")
    try:
        have = version(name)
    except PackageNotFoundError:
        have = None
    if have != want:
        print(f"{name}: want {want}, have {have or 'missing'}")
        bad += 1
sys.exit(1 if bad else 0)
PY
}

step_venv() {
  step "Python environment (requirements.txt)"
  if [[ ! -x "$VENV/bin/python" ]]; then
    if [[ "$MODE" == check ]]; then drift "no venv at $VENV"; return 0; fi
    python3 -m venv --system-site-packages "$VENV"
    echo "    created $VENV"
  fi
  if grep -qx "include-system-site-packages = true" "$VENV/pyvenv.cfg"; then
    ok "venv sees system packages (gpiozero, lgpio from apt)"
  else
    drift "venv was created without --system-site-packages; fix: rm -rf $VENV and re-run"
  fi
  local req out
  for req in requirements.txt requirements-optional.txt; do
    if out="$(pin_check "$REPO_ROOT/$req")"; then
      ok "$req satisfied"
    elif [[ "$MODE" == check ]]; then
      drift "$req:"; sed 's/^/      /' <<< "$out"
    elif [[ "$req" == requirements.txt ]]; then
      echo "    installing $req (several minutes on a Pi Zero)"
      "$VENV/bin/pip" install --quiet --disable-pip-version-check -r "$REPO_ROOT/$req"
    else
      echo "    installing $req (optional)"
      "$VENV/bin/pip" install --quiet --disable-pip-version-check -r "$REPO_ROOT/$req" \
        || echo "    WARNING: $req failed; grid auto-detect and canopy tracking will be off"
    fi
  done
  return 0
}

step_unit() {
  step "Service (deploy/growlight.service)"
  local want
  want="$(sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@RUN_USER@|$RUN_USER|g" "$DEPLOY/growlight.service")"
  if [[ -f "$UNIT_PATH" && "$want" == "$(cat "$UNIT_PATH")" ]]; then
    ok "unit current"
  elif [[ "$MODE" == check ]]; then
    drift "unit differs from deploy/growlight.service:"
    diff -u --label current --label wanted "$UNIT_PATH" <(printf '%s\n' "$want") 2>&1 \
      | tail -n +3 | sed 's/^/      /' || true
  else
    printf '%s\n' "$want" | sudo tee "$UNIT_PATH" > /dev/null
    sudo systemctl daemon-reload
    echo "    written"
  fi
  if [[ "$MODE" == apply ]] && ! systemctl is-enabled --quiet growlight 2>/dev/null; then
    sudo systemctl enable --quiet growlight
  fi

  # Drop-ins silently change the unit: real ones are drift, editor leftovers
  # from an interrupted 'systemctl edit' are removed.
  local d="$UNIT_PATH.d" f
  if [[ -d "$d" ]]; then
    shopt -s nullglob dotglob
    for f in "$d"/*; do
      case "$(basename "$f")" in
        .\#*) if [[ "$MODE" == check ]]; then note "editor leftover $f (removed on apply)"
              else sudo rm -f "$f"; echo "    removed editor leftover $f"; fi ;;
        *.conf) drift "drop-in $f overrides the unit and is not in deploy/" ;;
      esac
    done
    shopt -u nullglob dotglob
    [[ "$MODE" == apply ]] && sudo rmdir "$d" 2>/dev/null || true
  fi
  return 0
}

# First install only: where the plants are, so sun times and the day's
# schedule are right from the first boot. Changed later in Settings, Location.
step_location() {
  [[ "$FIRST_INSTALL" -eq 1 ]] || return 0
  step "Location and time zone"
  local sys_tz lat="" lon="" tz
  sys_tz="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)"
  if [[ "$NONINTERACTIVE" -eq 1 ]]; then
    note "no prompts: set Location in the dashboard Settings before relying on the schedule"
    return 0
  fi
  echo "    Used for sunrise/sunset and the daily light schedule. Decimal degrees,"
  echo "    e.g. 51.5 and -0.12 for London. Enter to skip and set it in Settings."
  while :; do
    prompt lat "Latitude (-90 to 90)" ""
    [[ -z "$lat" ]] && break
    prompt lon "Longitude (-180 to 180)" ""
    if python3 -c "import sys; a,b=float(sys.argv[1]),float(sys.argv[2]); sys.exit(not(-90<=a<=90 and -180<=b<=180))" \
         "$lat" "$lon" 2>/dev/null; then break; fi
    echo "    not valid coordinates; try again"
  done
  while :; do
    prompt tz "Time zone (IANA name)" "$sys_tz"
    [[ -f "/usr/share/zoneinfo/$tz" ]] && break
    echo "    unknown time zone '$tz'; examples: America/New_York, Europe/London"
  done
  if [[ "$tz" != "$sys_tz" ]] && confirm "Set the Pi's own clock to $tz too?" y; then
    sudo timedatectl set-timezone "$tz"
  fi
  python3 - "$APP_DIR/config.json" "$lat" "$lon" "$tz" << 'PY'
import json, sys
path, lat, lon, tz = sys.argv[1:]
cfg = {"timezone": tz}
if lat:
    cfg.update(latitude=float(lat), longitude=float(lon))
open(path, "w").write(json.dumps(cfg, indent=2))
PY
  echo "    saved to config.json"
  if [[ -z "$lat" ]]; then
    note "no coordinates given; the defaults are Thousand Oaks, CA until you set Location in Settings"
  fi
}

step_shell() {
  local line="source $VENV/bin/activate"
  grep -qxF "$line" "$HOME/.bashrc" 2>/dev/null && return 0
  [[ "$MODE" == check ]] && { note "venv auto-activate not in ~/.bashrc"; return 0; }
  echo "$line" >> "$HOME/.bashrc"
}

step_hardware() {
  step "Hardware check"
  if [[ "$NEED_REBOOT" -eq 1 ]]; then
    echo "    skipped until after the reboot; then run: bash $APP_DIR/scripts/setup.sh --check"
    return 0
  fi
  if command -v i2cdetect > /dev/null && [[ -e /dev/i2c-1 ]]; then
    local found pair addr
    found="$(i2cdetect -y 1 2>/dev/null | tail -n +2 | grep -oE ' [0-9a-f]{2}' | tr -d ' ' | tr '\n' ' ')"
    echo "    I2C devices: ${found:-none found}"
    for pair in "48:ADS1115 soil probes" "76:BME/BMP280 air" "77:BME/BMP280 air (alt)" \
                "23:BH1750 light" "5c:BH1750 light (alt)"; do
      addr="${pair%%:*}"
      case " $found " in *" $addr "*) echo "      $addr  ${pair#*:}" ;; esac
    done
  else
    echo "    I2C bus not available"
  fi
  if compgen -G "/sys/bus/w1/devices/28-*" > /dev/null; then
    echo "    1-Wire: $(ls -d /sys/bus/w1/devices/28-* | wc -l) DS18B20 sensor(s)"
  else
    echo "    1-Wire: no DS18B20 found"
  fi
}

# --------------------------------------------------------------------------
has_password() {
  [[ -f "$APP_DIR/config.json" ]] && \
    grep -Eq '"password_hash"[[:space:]]*:[[:space:]]*"[^"]' "$APP_DIR/config.json"
}

finish() {
  echo
  echo "=================================================="
  if [[ "$MODE" == check ]]; then
    if [[ "$DRIFT" -eq 0 ]]; then echo "No drift: this box matches deploy/."
    else echo "$DRIFT item(s) drifted. 'bash scripts/setup.sh --defaults' applies deploy/ and keeps .env."; fi
    echo "=================================================="
    exit $(( DRIFT > 0 ))
  fi

  if ! has_password; then
    echo "The dashboard has no password: anyone on your network can run the pumps."
    if [[ "$NONINTERACTIVE" -eq 0 ]] && confirm "Set a password now?" y; then
      "$VENV/bin/python" "$APP_DIR/scripts/set_password.py" < /dev/tty || true
    else
      echo "Set one later:  $VENV/bin/python $APP_DIR/scripts/set_password.py"
    fi
  fi

  local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "--------------------------------------------------"
  echo "Dashboard:  http://${ip:-<pi-address>}:5000"
  echo "Logs:       journalctl -u growlight -f"
  echo "Check:      bash $APP_DIR/scripts/setup.sh --check"
  echo "Update:     bash $APP_DIR/scripts/update.sh <openSeedling zip>"
  echo "=================================================="

  if [[ "$NEED_REBOOT" -eq 1 ]]; then
    echo "Boot settings changed, so the Pi needs a reboot. The service starts on boot."
    if [[ "$NONINTERACTIVE" -eq 0 ]] && confirm "Reboot now?" y; then
      sudo reboot
    else
      echo "Run:  sudo reboot"
    fi
  else
    sudo systemctl restart growlight
    sleep 2
    systemctl --no-pager --lines=5 status growlight || true
  fi
}

main "$@"
