#!/usr/bin/env bash
# Deploy main to the Pi: push to GitLab, pull on the Pi, restart, check.
#
#   bash scripts/deploy.sh               push main, update the Pi
#   bash scripts/deploy.sh --check       show what the Pi would get, change nothing
#
# Run on your development machine, from the checkout, on main. Steps, stopping
# at the first problem:
#   1. git push origin main (refuses while there are uncommitted changes)
#   2. on the Pi: git fetch, fast-forward ~/growlight to origin/main
#   3. compile every Python file
#   4. run scripts/setup.sh --defaults if deploy/ or the requirements changed
#   5. restart growlight and wait for /api/status and no tracebacks
# If 3 or 5 fails, the Pi goes back to the commit it was on and restarts.
# Packages and boot settings setup.sh changed stay; the service unit goes back
# with the code.
#
# Options:
#   --host HOST  ssh host of the Pi (default $GROWLIGHT_HOST or grow-server)
#   --dir PATH   app directory on the Pi (default ~/growlight)
#   --check      list incoming commits and files, change nothing
#   --no-push    skip the push (origin/main is already where you want it)

set -euo pipefail

HOST="${GROWLIGHT_HOST:-grow-server}"; DIR='~/growlight'; CHECK=0; PUSH=1
BRANCH=main

die() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)    HOST="${2:?--host needs a name}"; shift ;;
    --dir)     DIR="${2:?--dir needs a path}"; shift ;;
    --check)   CHECK=1 ;;
    --no-push) PUSH=0 ;;
    -h|--help) sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# ---- 1. push ----------------------------------------------------------------
[[ "$(git rev-parse --abbrev-ref HEAD)" == "$BRANCH" ]] \
  || die "on $(git rev-parse --abbrev-ref HEAD); the Pi runs $BRANCH. Merge first."
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  git status --short --untracked-files=no | sed 's/^/    /'
  die "uncommitted changes would not be deployed; commit or stash them"
fi
if [[ "$CHECK" -eq 0 && "$PUSH" -eq 1 ]]; then
  echo "==> Pushing $BRANCH to origin"
  git push origin "$BRANCH"
else
  git fetch -q origin "$BRANCH"
  if [[ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$BRANCH")" ]]; then
    echo "    note: local $BRANCH is not origin/$BRANCH; the Pi gets origin/$BRANCH"
  fi
fi

# ---- 2-5. on the Pi -----------------------------------------------------------
echo "==> Updating $HOST:$DIR"
ssh -o BatchMode=yes "$HOST" bash -s -- "$DIR" "$BRANCH" "$CHECK" << 'REMOTE'
set -euo pipefail
APP="${1/#\~/$HOME}"; BRANCH="$2"; CHECK="$3"
SERVICE=growlight; PORT=5000

step() { echo "==> $*"; }
ok()   { echo "    ok: $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

healthy() {       # healthy SECONDS: service active and /api/status answers 200
  local deadline=$(( SECONDS + $1 ))
  while (( SECONDS < deadline )); do
    if systemctl is-active --quiet "$SERVICE" && \
       [[ "$(curl -s -m 15 -o /dev/null -w '%{http_code}' \
              "http://127.0.0.1:$PORT/api/status" || true)" == 200 ]]; then
      return 0
    fi
    sleep 3
  done
  return 1
}

cd "$APP" 2>/dev/null || die "$APP not found on $(hostname)"
[[ -d .git ]] || die "$APP is not a git checkout"
# the port the service listens on is set in .env, not in this shell
env_port="$(sed -n 's/^GROWLIGHT_HTTP_PORT=//p' .env 2>/dev/null | tail -1 | tr -d "\"' " || true)"
PORT="${env_port:-$PORT}"
[[ "$(git rev-parse --abbrev-ref HEAD)" == "$BRANCH" ]] \
  || die "$APP is on $(git rev-parse --abbrev-ref HEAD), not $BRANCH"

git fetch -q origin "$BRANCH"
OLD="$(git rev-parse HEAD)"; NEW="$(git rev-parse "origin/$BRANCH")"
if [[ "$OLD" == "$NEW" ]]; then
  ok "already on $(git log --oneline -1 HEAD); nothing to do"; exit 0
fi
git merge-base --is-ancestor "$OLD" "$NEW" \
  || die "the Pi has commits that are not on origin/$BRANCH; sort that out on the Pi"

git log --oneline "$OLD..$NEW" | sed 's/^/    /'
mapfile -t FILES < <(git diff --name-only "$OLD" "$NEW")
git diff --stat "$OLD" "$NEW" | tail -1 | sed 's/^/   /'
BOX=0
for f in "${FILES[@]}"; do
  case "$f" in deploy/*|requirements.txt|requirements-optional.txt|constraints.txt|scripts/setup.sh) BOX=1 ;; esac
done
[[ $BOX -eq 1 ]] && echo "    includes box config: setup.sh --defaults will apply it"
if [[ "$CHECK" -eq 1 ]]; then echo "Check only: nothing changed."; exit 0; fi

rollback() {
  echo "ERROR: $1" >&2
  echo "Going back to $(git log --oneline -1 "$OLD")" >&2
  git reset -q --keep "$OLD" || { echo "reset failed; fix by hand: git reset --keep $OLD" >&2; exit 1; }
  if [[ "${APPLIED:-0}" -eq 1 ]]; then
    # the service unit on the box came from the new version: put the old one back
    bash scripts/setup.sh --defaults > /dev/null 2>&1 || echo "    could not re-apply the previous box config" >&2
  fi
  sudo systemctl restart "$SERVICE" || true
  if healthy 120; then echo "Back on the previous version and running." >&2
  else echo "Rolled back, but the service is not answering: journalctl -u $SERVICE -b" >&2; fi
  exit 1
}

step "Fast-forwarding to $(git log --oneline -1 "$NEW")"
git merge -q --ff-only "$NEW"
find scripts -maxdepth 1 -name '*.sh' -exec chmod +x {} + 2>/dev/null || true

PY="$APP/venv/bin/python"; [[ -x "$PY" ]] || PY=python3
if ! errs="$("$PY" - "$APP" 2>&1 << 'PY'
import pathlib, py_compile, sys
bad = 0
for p in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    if "venv" in p.parts or "node_modules" in p.parts:
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
ok "all Python files compile"

REBOOT=0
if [[ $BOX -eq 1 ]]; then
  step "Applying box config (scripts/setup.sh --defaults)"
  LOG="$(mktemp)"; rc=0
  bash scripts/setup.sh --defaults > "$LOG" 2>&1 || rc=$?
  APPLIED=1
  grep -E "DRIFT|updated|added|installed|created|WARNING" "$LOG" | sed 's/^/  /' || true
  grep -q "needs a reboot\|REBOOT REQUIRED" "$LOG" && REBOOT=1
  if [[ "$rc" -ne 0 ]]; then
    tail -15 "$LOG" | sed 's/^/    | /'
    echo "    setup.sh exited $rc (log: $LOG); checking the service anyway"
  fi
fi

step "Restarting $SERVICE"
sudo systemctl restart "$SERVICE"
healthy 120 || rollback "the service did not come back (journalctl -u $SERVICE -n 50)"
sleep 15
pid="$(systemctl show -p MainPID --value "$SERVICE")"
tb=""
if [[ -n "$pid" && "$pid" != 0 ]]; then
  # grep reads the whole log: with pipefail, a reader that stops early
  # (grep -q, head) fails the pipeline and hides the match
  tb="$(journalctl _PID="$pid" --no-pager 2>/dev/null | grep -A12 Traceback || true)"
fi
if [[ -n "$tb" ]]; then
  sed -n '1,30s/^/    | /p' <<< "$tb"
  rollback "the new version is logging tracebacks"
fi
ok "running $(git log --oneline -1 HEAD) on port $PORT"
[[ "$REBOOT" -eq 1 ]] && echo "Boot settings changed: they take effect after  sudo reboot"
exit 0
REMOTE
