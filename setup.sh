#!/usr/bin/env bash
# Install an openSeedling update zip on the Pi.
#
#   bash update.sh openSeedling-update.zip
#
# With no argument it uses the newest openSeedling*.zip in the current
# directory, next to this script, or in your home directory.
#
# Steps, stopping at the first problem:
#   1. unpack the zip and list what it changes in ~/growlight
#   2. back up config.json, .env, .secret, a consistent copy of growlight.db,
#      and every file the update replaces or deletes
#   3. copy the new files in and compile every Python file
#   4. run scripts/setup.sh --defaults if deploy/ or the requirements changed
#   5. restart growlight and wait for it to answer with no tracebacks
#   6. if ~/growlight is a git checkout, commit the result there
# If 3 or 5 fails, every file goes back to how it was and the old version is
# restarted. Changes setup.sh made to the box (packages, boot config) stay.
#
# Options:
#   --dir PATH   app directory (default ~/growlight)
#   --check      list what would change, change nothing
#   --yes        do not ask before installing

set -euo pipefail

APP="$HOME/growlight"; ZIP=""; CHECK=0; YES=0
SERVICE=growlight; PORT="${GROWLIGHT_PORT:-5000}"; KEEP_BACKUPS=5
BK=""; STAGE=""; APPLIED=0; ROLLING=0
declare -a CHANGED=() ADDED=() REMOVED=()

step() { echo "==> $*"; }
ok()   { echo "    ok: $*"; }
note() { echo "    $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

confirm() {
  [[ "$YES" -eq 1 ]] && return 0
  { : < /dev/tty; } 2>/dev/null || return 0        # no terminal: proceed
  local r; read -r -p "    $1 [Y/n] " r < /dev/tty || true
  [[ -z "$r" || "$r" =~ ^[Yy] ]]
}

healthy() {       # healthy SECONDS: service active and /api/status answers 200
  local deadline=$(( SECONDS + $1 ))
  while (( SECONDS < deadline )); do
    if systemctl is-active --quiet "$SERVICE" && python3 - "$PORT" << 'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/status", timeout=5) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
    then return 0; fi
    sleep 3
  done
  return 1
}

restore_files() { # put back every file this update replaced, removed or added
  local f
  for f in "${ADDED[@]}"; do rm -f -- "$APP/$f"; done
  for f in "${CHANGED[@]}" "${REMOVED[@]}"; do
    mkdir -p "$(dirname "$APP/$f")"
    cp -p -- "$BK/code/$f" "$APP/$f"
  done
}

rollback() {
  echo "ERROR: $1" >&2
  [[ "$APPLIED" -eq 1 && "$ROLLING" -eq 0 ]] || exit 1
  ROLLING=1
  echo "Putting the previous files back." >&2
  restore_files
  [[ -f "$BK/.secret" && ! -f "$APP/.secret" ]] && cp -p "$BK/.secret" "$APP/.secret"
  sudo systemctl restart "$SERVICE" || true
  if healthy 90; then
    echo "Back on the previous version and running. Backup: $BK" >&2
  else
    echo "Files restored, but the service is not answering: journalctl -u $SERVICE -b" >&2
  fi
  exit 1
}

cleanup() { [[ -n "$STAGE" ]] && rm -rf "$STAGE"; return 0; }
trap cleanup EXIT

# ---- arguments ------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)   APP="${2:?--dir needs a path}"; shift ;;
    --check) CHECK=1 ;;
    --yes)   YES=1 ;;
    -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1 (try --help)" ;;
    *)  ZIP="$1" ;;
  esac
  shift
done

[[ $EUID -ne 0 ]] || die "run as your normal user, not root"
[[ -f "$APP/growlight.py" ]] || die "$APP does not look like the app (use --dir)"
if [[ -z "$ZIP" ]]; then
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  ZIP="$(ls -1t ./openSeedling*.zip "$here"/openSeedling*.zip "$HOME"/openSeedling*.zip \
           2>/dev/null | head -1 || true)"
  [[ -n "$ZIP" ]] || die "no zip given and no openSeedling*.zip found; usage: bash update.sh <zip>"
fi
[[ -f "$ZIP" ]] || die "$ZIP not found"
ZIP="$(cd "$(dirname "$ZIP")" && pwd)/$(basename "$ZIP")"

# ---- 1. unpack and compare ------------------------------------------------
step "Reading $(basename "$ZIP")"
STAGE="$(mktemp -d)"
python3 -m zipfile -e "$ZIP" "$STAGE" || die "not a readable zip"
SRC="$(dirname "$(find "$STAGE" -maxdepth 3 -name growlight.py -print -quit)")"
[[ -n "$SRC" && "$SRC" != . ]] || die "the zip has no growlight.py; is it an openSeedling update?"

while IFS= read -r -d '' f; do
  f="${f#"$SRC"/}"
  [[ "$f" == REMOVED || "$f" == */__pycache__/* || "$f" == *.pyc ]] && continue
  if [[ ! -e "$APP/$f" ]]; then ADDED+=("$f")
  elif ! cmp -s "$SRC/$f" "$APP/$f"; then CHANGED+=("$f"); fi
done < <(find "$SRC" -type f -print0 | sort -z)
if [[ -f "$SRC/REMOVED" ]]; then
  while IFS= read -r f; do
    [[ -z "$f" || "$f" == \#* ]] && continue
    [[ -e "$APP/$f" ]] && REMOVED+=("$f")
  done < "$SRC/REMOVED"
fi

for f in "${CHANGED[@]}"; do echo "    modified  $f"; done
for f in "${ADDED[@]}";   do echo "    new       $f"; done
for f in "${REMOVED[@]}"; do echo "    removed   $f"; done
N=$(( ${#CHANGED[@]} + ${#ADDED[@]} + ${#REMOVED[@]} ))
if [[ $N -eq 0 ]]; then ok "already installed; nothing to do"; exit 0; fi

BOX=0
for f in "${CHANGED[@]}" "${ADDED[@]}"; do
  case "$f" in deploy/*|requirements*.txt|scripts/setup.sh) BOX=1 ;; esac
done
[[ $BOX -eq 1 ]] && note "includes box config: setup.sh --defaults will apply it"

GIT=0
if [[ -d "$APP/.git" ]] && command -v git > /dev/null; then
  GIT=1
  edited="$(cd "$APP" && git status --porcelain --untracked-files=no -- \
              "${CHANGED[@]}" "${REMOVED[@]}" 2>/dev/null || true)"
  if [[ -n "$edited" ]]; then
    note "these were edited on the Pi and will be replaced (the backup keeps them):"
    echo "$edited" | sed 's/^/      /'
  fi
fi
[[ "$CHECK" -eq 1 ]] && { echo "Check only: nothing changed."; exit 0; }
confirm "Install these $N change(s)?" || die "stopped; nothing changed"

# ---- 2. backup ------------------------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
BK="$HOME/growlight-backups/$STAMP"
step "Backing up to $BK"
mkdir -p "$BK/code"; chmod 700 "$BK"
for f in config.json .env .secret; do [[ -f "$APP/$f" ]] && cp -p "$APP/$f" "$BK/"; done
if [[ -f "$APP/growlight.db" ]]; then
  # SQLite's online backup: a consistent copy while the app keeps writing
  python3 - "$APP/growlight.db" "$BK/growlight.db" << 'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=30)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
fi
for f in "${CHANGED[@]}" "${REMOVED[@]}"; do
  mkdir -p "$(dirname "$BK/code/$f")"
  cp -p -- "$APP/$f" "$BK/code/$f"
done
printf '%s\n' "${ADDED[@]}" > "$BK/added-files"
cp -p "$ZIP" "$BK/"
ok "$(du -sh "$BK" | cut -f1)"
ls -1d "$HOME"/growlight-backups/*/ 2>/dev/null | sort | head -n -"$KEEP_BACKUPS" | xargs -r rm -rf

# ---- 3. install and compile -----------------------------------------------
step "Installing"
APPLIED=1
for f in "${CHANGED[@]}" "${ADDED[@]}"; do
  mkdir -p "$(dirname "$APP/$f")"
  cp -- "$SRC/$f" "$APP/$f"
done
for f in "${REMOVED[@]}"; do rm -f -- "$APP/$f"; done
find "$APP/scripts" -maxdepth 1 -name '*.sh' -exec chmod +x {} + 2>/dev/null || true
# The session key was committed once and is public on the mirror. If git
# still tracks it, drop it so the app writes a new one (you log in again).
if [[ $GIT -eq 1 ]] && (cd "$APP" && git ls-files --error-unmatch .secret > /dev/null 2>&1); then
  rm -f "$APP/.secret"
  note "session key rotated: log in again after the restart"
fi
PY="$APP/venv/bin/python"; [[ -x "$PY" ]] || PY=python3
if ! errs="$("$PY" - "$APP" 2>&1 << 'PY'
import pathlib, py_compile, sys
bad = 0
for p in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    if "venv" in p.parts:
        continue
    try:
        py_compile.compile(str(p), doraise=True)
    except py_compile.PyCompileError as e:
        print(e.msg); bad += 1
sys.exit(1 if bad else 0)
PY
)"; then
  echo "$errs" | sed 's/^/    /'
  rollback "the new code does not compile"
fi
ok "$N file(s) in place; all Python files compile"

# ---- 4. box changes -------------------------------------------------------
REBOOT=0
if [[ $BOX -eq 1 ]]; then
  step "Applying box config (scripts/setup.sh --defaults)"
  rc=0
  bash "$APP/scripts/setup.sh" --defaults > "$BK/setup.log" 2>&1 || rc=$?
  grep -E "DRIFT|updated|added|installed|created|WARNING" "$BK/setup.log" | sed 's/^/  /' || true
  grep -q "needs a reboot\|REBOOT REQUIRED" "$BK/setup.log" && REBOOT=1
  if [[ "$rc" -ne 0 ]]; then
    tail -15 "$BK/setup.log" | sed 's/^/    | /'
    note "setup.sh exited $rc (full log: $BK/setup.log); checking the service anyway"
  fi
fi

# ---- 5. restart and check -------------------------------------------------
# Restarted here even if setup.sh already did, so the check below is looking
# at the new code. Boot settings wait for a reboot; the code does not need them.
step "Restarting $SERVICE"
sudo systemctl restart "$SERVICE"
healthy 90 || rollback "the service did not come back (journalctl -u $SERVICE -n 50)"
# give the loops a few passes, then read the NEW process's log only
sleep 15
pid="$(systemctl show -p MainPID --value "$SERVICE")"
if [[ -n "$pid" && "$pid" != 0 ]] && \
   journalctl _PID="$pid" --no-pager 2>/dev/null | grep -q Traceback; then
  journalctl _PID="$pid" --no-pager | grep -A12 Traceback | head -30 | sed 's/^/    | /'
  rollback "the new version is logging tracebacks"
fi
ok "running and answering on port $PORT"

# ---- 6. record it in git --------------------------------------------------
if [[ $GIT -eq 1 ]]; then
  (
    cd "$APP"
    git add -- "${CHANGED[@]}" "${ADDED[@]}" 2>/dev/null || true
    for f in "${REMOVED[@]}"; do git rm -q --cached --ignore-unmatch -- "$f"; done
    # gitignored files that were committed once: stop tracking them
    for f in .secret preview.jpg; do git rm -q --cached --ignore-unmatch -- "$f"; done
    if ! git diff --cached --quiet; then
      git -c user.name="$(git config user.name || echo "$USER")" \
          -c user.email="$(git config user.email || echo "$USER@$(hostname)")" \
          commit -q -m "Install $(basename "$ZIP") on $(hostname)" \
        && echo "    ok: committed in $APP as $(git rev-parse --short HEAD)"
    fi
  ) || note "installed, but the git commit failed; run 'git status' in $APP"
fi

echo
echo "Installed $(basename "$ZIP")."
echo "Backup: $BK"
echo "Undo:   cp -a $BK/code/. $APP/ && xargs -r -a $BK/added-files -I{} rm -f $APP/{} && sudo systemctl restart $SERVICE"
[[ "$REBOOT" -eq 1 ]] && { echo; echo "Boot settings changed: they take effect after  sudo reboot"; }
exit 0
