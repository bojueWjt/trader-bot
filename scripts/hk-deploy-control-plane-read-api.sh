#!/usr/bin/env bash
# Version-locked control-plane read API update for the HK host.
set -Eeuo pipefail

T="${T:-/srv/trader-v3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STAGING="${STAGING:-$REPO_ROOT}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
OPERATION_LOCK="${OPERATION_LOCK:-/var/lock/trader-v3-account-stall-operation.lock}"
ACCOUNT_A_READY_URL="${ACCOUNT_A_READY_URL:-http://127.0.0.1:8081/ready}"
CONTROL_PLANE_SSE_PROXY_URL="${CONTROL_PLANE_SSE_PROXY_URL:-http://100.104.27.123:8088/v1/stream}"
CONTROL_PLANE_DIRECT_SSE_URL="${CONTROL_PLANE_DIRECT_SSE_URL:-http://127.0.0.1:8080/v1/stream}"

DEPLOYMENT_FILE_COUNT=2
READ_API_RELATIVE="services/control-plane/api/read_api.py"
UNIT_RELATIVE="infra/systemd/trader-v3-controlplane.service"
UNIT_NAME="trader-v3-controlplane.service"
READ_API_SOURCE="$STAGING/$READ_API_RELATIVE"
UNIT_SOURCE="$STAGING/$UNIT_RELATIVE"
READ_API_TARGET="$T/$READ_API_RELATIVE"
UNIT_TARGET="$SYSTEMD_UNIT_DIR/$UNIT_NAME"

READ_API_EXPECTED_SHA256="953a429f3290e64409784e1fc9ce5e69f1fbff2eb94646ff313fdae7769ee943"
UNIT_EXPECTED_SHA256="9a010de0d2486b37669ec8b293102aff7230086961ecb1aeec2478ef36d448c9"
READ_API_OLD_SHA256_ALLOWLIST="${CONTROL_PLANE_READ_API_OLD_SHA256_ALLOWLIST:-4226533812c96b267c929246752f4a7036cccfed21a9e7b7a862c3ab2d039026 953a429f3290e64409784e1fc9ce5e69f1fbff2eb94646ff313fdae7769ee943}"
UNIT_OLD_SHA256_ALLOWLIST="${CONTROL_PLANE_UNIT_OLD_SHA256_ALLOWLIST:-cdce9a88f429b760df255ee09f17ae80b30a338d9cb3fe8169bc508718b90d0f 9a010de0d2486b37669ec8b293102aff7230086961ecb1aeec2478ef36d448c9}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="$T/backups/control-plane-read-api-$STAMP"
ROLLBACK_PATH="$BACKUP_ROOT/rollback.sh"
UNIT_STATE_FILE="$BACKUP_ROOT/control-plane-unit-state.tsv"
UNIT_TARGET_STATE_FILE="$BACKUP_ROOT/control-plane-unit-target-state.txt"
TEMP_DIR=""
READY_BODY=""
SSE_HEADERS=""
SSE_BODY=""
SSE_ERROR=""
SSE_PID=""
MUTATION_STARTED=0


die() {
  echo "FATAL: $*" >&2
  return 1
}


sha256_of() {
  sha256sum "$1" | awk '{print $1}'
}


sha256_is_allowed() {
  local actual="$1"
  local allowlist="$2"
  local normalized="${allowlist//,/ }"
  local approved
  for approved in $normalized; do
    if [ "$actual" = "$approved" ]; then
      return 0
    fi
  done
  return 1
}


compile_python() {
  python3 - "$1" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY
}


http_status() {
  local url="$1"
  curl \
    --silent \
    --show-error \
    --no-buffer \
    --connect-timeout 2 \
    --max-time 3 \
    --output /dev/null \
    --write-out '%{http_code}' \
    "$url" \
    || true
}


probe_sse_contract() {
  local phase="$1"
  local proxy_status
  local direct_status
  proxy_status="$(http_status "$CONTROL_PLANE_SSE_PROXY_URL")"
  if [ "$proxy_status" != "200" ]; then
    die "SSE proxy expected HTTP 200, got $proxy_status during $phase"
  fi
  direct_status="$(http_status "$CONTROL_PLANE_DIRECT_SSE_URL")"
  if [ "$direct_status" != "401" ]; then
    die "direct anonymous SSE expected HTTP 401, got $direct_status during $phase"
  fi
}


verify_account_a_halted() {
  local phase="$1"
  curl \
    --silent \
    --show-error \
    --connect-timeout 2 \
    --max-time 5 \
    --output "$READY_BODY" \
    "$ACCOUNT_A_READY_URL"
  python3 - "$READY_BODY" "$phase" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
phase = sys.argv[2]
payload = json.loads(path.read_text(encoding="utf-8"))
state = str(payload.get("trading_state") or "").upper()
if state != "HALTED":
    raise SystemExit(
        f"account-a /ready must report HALTED during {phase}, got {state!r}"
    )
PY
}


start_sse_hold() {
  local attempt=0
  : >"$SSE_HEADERS"
  : >"$SSE_BODY"
  : >"$SSE_ERROR"
  curl \
    --silent \
    --show-error \
    --no-buffer \
    --connect-timeout 2 \
    --max-time 30 \
    --dump-header "$SSE_HEADERS" \
    --output "$SSE_BODY" \
    "$CONTROL_PLANE_SSE_PROXY_URL" \
    2>"$SSE_ERROR" &
  SSE_PID=$!

  while [ "$attempt" -lt 160 ]; do
    if grep -Eq '^HTTP/[0-9.]+ 200([[:space:]]|$)' "$SSE_HEADERS" \
      && grep -Fq "event: dashboard_snapshot" "$SSE_BODY"; then
      break
    fi
    if ! kill -0 "$SSE_PID" 2>/dev/null; then
      wait "$SSE_PID" 2>/dev/null || true
      die "Caddy SSE hold ended before the first event"
    fi
    attempt=$((attempt + 1))
    sleep 0.05
  done

  if ! grep -Eq '^HTTP/[0-9.]+ 200([[:space:]]|$)' "$SSE_HEADERS"; then
    die "Caddy SSE hold did not establish HTTP 200"
  fi
  if ! grep -Fq "event: dashboard_snapshot" "$SSE_BODY"; then
    die "Caddy SSE hold did not receive the first event"
  fi
  if ! kill -0 "$SSE_PID" 2>/dev/null; then
    die "Caddy SSE hold was not active before restart"
  fi
}


wait_for_sse_hold_shutdown() {
  local attempt=0
  local status=0
  if [ -z "$SSE_PID" ]; then
    die "Caddy SSE hold PID is missing after restart"
  fi
  while kill -0 "$SSE_PID" 2>/dev/null && [ "$attempt" -lt 40 ]; do
    attempt=$((attempt + 1))
    sleep 0.05
  done
  if kill -0 "$SSE_PID" 2>/dev/null; then
    die "Caddy SSE hold did not close during graceful restart"
  fi
  if wait "$SSE_PID"; then
    status=0
  else
    status=$?
  fi
  SSE_PID=""
  if [ "$status" -ne 0 ]; then
    die "Caddy SSE hold exited unsafely during restart: status=$status"
  fi
}


restart_control_plane_bounded() {
  local phase="$1"
  local started_ns
  local finished_ns
  started_ns="$(
    python3 - <<'PY'
import time

print(time.monotonic_ns())
PY
  )"
  systemctl restart "$UNIT_NAME"
  finished_ns="$(
    python3 - <<'PY'
import time

print(time.monotonic_ns())
PY
  )"
  python3 - "$started_ns" "$finished_ns" "$phase" <<'PY'
import sys

started = int(sys.argv[1])
finished = int(sys.argv[2])
phase = sys.argv[3]
elapsed_seconds = (finished - started) / 1_000_000_000
if elapsed_seconds >= 15:
    raise SystemExit(
        f"{phase} restart must complete in less than 15 seconds: "
        f"{elapsed_seconds:.3f}s"
    )
print(f"{phase} restart completed in {elapsed_seconds:.3f}s")
PY
}


stop_sse_hold() {
  local attempt=0
  if [ -z "$SSE_PID" ]; then
    return
  fi
  kill "$SSE_PID" 2>/dev/null || true
  while kill -0 "$SSE_PID" 2>/dev/null && [ "$attempt" -lt 40 ]; do
    attempt=$((attempt + 1))
    sleep 0.05
  done
  if kill -0 "$SSE_PID" 2>/dev/null; then
    kill -KILL "$SSE_PID" 2>/dev/null || true
  fi
  wait "$SSE_PID" 2>/dev/null || true
  SSE_PID=""
}


cleanup() {
  stop_sse_hold
  if [ -n "$TEMP_DIR" ]; then
    rm -rf -- "$TEMP_DIR"
  fi
}


on_err() {
  local status=$?
  trap - ERR
  stop_sse_hold
  echo "FATAL: control-plane read API update failed" >&2
  if [ "$MUTATION_STARTED" = "1" ] && [ -x "$ROLLBACK_PATH" ]; then
    exec 9>&-
    if bash "$ROLLBACK_PATH"; then
      echo "ROLLBACK: automatic rollback completed" >&2
    else
      echo "ROLLBACK: automatic rollback failed; run $ROLLBACK_PATH" >&2
    fi
  elif [ -x "$ROLLBACK_PATH" ]; then
    echo "ROLLBACK: bash $ROLLBACK_PATH" >&2
  fi
  exit "$status"
}


trap cleanup EXIT
trap on_err ERR

for command in \
  awk cp curl date flock grep id install journalctl mkdir mktemp \
  python3 rm sha256sum sleep systemctl tr; do
  command -v "$command" >/dev/null \
    || die "required command missing: $command"
done

if [ "$(id -u)" != "0" ]; then
  die "update must run as root"
fi
if [ "$DEPLOYMENT_FILE_COUNT" -ne 2 ]; then
  die "invalid deployment file count"
fi
if [ ! -d "$T" ]; then
  die "trader root is missing: $T"
fi
if [ ! -f "$READ_API_SOURCE" ] || [ -L "$READ_API_SOURCE" ]; then
  die "staging read API must be a regular file"
fi
if [ ! -f "$UNIT_SOURCE" ] || [ -L "$UNIT_SOURCE" ]; then
  die "staging control-plane unit must be a regular file"
fi
if [ ! -f "$READ_API_TARGET" ] || [ -L "$READ_API_TARGET" ]; then
  die "live read API baseline must be a regular file"
fi
if [ -L "$UNIT_TARGET" ]; then
  die "control-plane unit target symlinks are unsupported"
fi
if [ -e "$UNIT_TARGET" ] && [ ! -f "$UNIT_TARGET" ]; then
  die "control-plane unit target must be a regular file or absent"
fi

exec 9>"$OPERATION_LOCK"
flock -n 9 || die "another operation holds $OPERATION_LOCK"

READ_API_SOURCE_SHA="$(sha256_of "$READ_API_SOURCE")"
if [ "$READ_API_SOURCE_SHA" != "$READ_API_EXPECTED_SHA256" ]; then
  die "staging read API SHA256 mismatch"
fi
UNIT_SOURCE_SHA="$(sha256_of "$UNIT_SOURCE")"
if [ "$UNIT_SOURCE_SHA" != "$UNIT_EXPECTED_SHA256" ]; then
  die "staging control-plane unit SHA256 mismatch"
fi
compile_python "$READ_API_SOURCE"
grep -Fq -- "--timeout-graceful-shutdown 10" "$UNIT_SOURCE" \
  || die "control-plane graceful shutdown timeout is missing"
grep -Fq "TimeoutStopSec=20s" "$UNIT_SOURCE" \
  || die "control-plane stop limit is missing"
grep -Fq "KillMode=control-group" "$UNIT_SOURCE" \
  || die "control-plane kill mode is invalid"

READ_API_OLD_SHA="$(sha256_of "$READ_API_TARGET")"
if ! sha256_is_allowed \
  "$READ_API_OLD_SHA" \
  "$READ_API_OLD_SHA256_ALLOWLIST"; then
  die "read API baseline SHA256 is not approved: $READ_API_OLD_SHA"
fi

TEMP_DIR="$(mktemp -d)"
READY_BODY="$TEMP_DIR/account-a-ready.json"
SSE_HEADERS="$TEMP_DIR/caddy-sse.headers"
SSE_BODY="$TEMP_DIR/caddy-sse.body"
SSE_ERROR="$TEMP_DIR/caddy-sse.stderr"
JOURNAL_BODY="$TEMP_DIR/control-plane-restart.journal"

verify_account_a_halted "before update"
probe_sse_contract "before update"

UNIT_LOAD_STATE="$(
  systemctl show \
    --property=LoadState \
    --value \
    "$UNIT_NAME"
)" || die "$UNIT_NAME LoadState is unavailable"
UNIT_FRAGMENT_PATH="$(
  systemctl show \
    --property=FragmentPath \
    --value \
    "$UNIT_NAME"
)" || die "$UNIT_NAME FragmentPath is unavailable"
case "$UNIT_FRAGMENT_PATH" in
  *$'\t'*|*$'\n'*)
    die "control-plane FragmentPath contains control characters"
    ;;
esac

case "$UNIT_LOAD_STATE" in
  loaded)
    case "$UNIT_FRAGMENT_PATH" in
      /*)
        ;;
      *)
        die "loaded control-plane unit has invalid FragmentPath"
        ;;
    esac
    if [ ! -f "$UNIT_FRAGMENT_PATH" ]; then
      die "loaded control-plane fragment is unavailable"
    fi
    UNIT_OLD_SHA="$(sha256_of "$UNIT_FRAGMENT_PATH")"
    if ! sha256_is_allowed \
      "$UNIT_OLD_SHA" \
      "$UNIT_OLD_SHA256_ALLOWLIST"; then
      die "control-plane unit baseline SHA256 is not approved: $UNIT_OLD_SHA"
    fi
    UNIT_ENABLEMENT="$(
      systemctl is-enabled "$UNIT_NAME" 2>/dev/null || true
    )"
    case "$UNIT_ENABLEMENT" in
      enabled|disabled)
        ;;
      *)
        die "unsupported control-plane enablement: $UNIT_ENABLEMENT"
        ;;
    esac
    UNIT_ACTIVE_STATE="$(
      systemctl is-active "$UNIT_NAME" 2>/dev/null || true
    )"
    case "$UNIT_ACTIVE_STATE" in
      active|inactive)
        ;;
      *)
        die "unsupported control-plane active state: $UNIT_ACTIVE_STATE"
        ;;
    esac
    ;;
  not-found)
    if [ -n "$UNIT_FRAGMENT_PATH" ]; then
      die "absent control-plane unit has a FragmentPath"
    fi
    UNIT_ENABLEMENT="$(
      systemctl is-enabled "$UNIT_NAME" 2>/dev/null || true
    )"
    if [ "$UNIT_ENABLEMENT" != "not-found" ]; then
      die "absent control-plane unit has invalid enablement"
    fi
    UNIT_ACTIVE_STATE="$(
      systemctl is-active "$UNIT_NAME" 2>/dev/null || true
    )"
    if [ "$UNIT_ACTIVE_STATE" != "inactive" ]; then
      die "absent control-plane unit has invalid active state"
    fi
    UNIT_FRAGMENT_PATH="-"
    ;;
  *)
    die "unsupported control-plane LoadState: $UNIT_LOAD_STATE"
    ;;
esac

if [ -e "$BACKUP_ROOT" ]; then
  die "backup path already exists: $BACKUP_ROOT"
fi
mkdir -p "$BACKUP_ROOT"
chmod 0700 "$BACKUP_ROOT"
cp -a "$READ_API_TARGET" "$BACKUP_ROOT/read_api.py"
printf '%s\t%s\t%s\t%s\n' \
  "$UNIT_ENABLEMENT" \
  "$UNIT_LOAD_STATE" \
  "$UNIT_FRAGMENT_PATH" \
  "$UNIT_ACTIVE_STATE" \
  >"$UNIT_STATE_FILE"
if [ -e "$UNIT_TARGET" ]; then
  cp -a "$UNIT_TARGET" "$BACKUP_ROOT/systemd-unit-target"
  printf 'present\n' >"$UNIT_TARGET_STATE_FILE"
else
  printf 'absent\n' >"$UNIT_TARGET_STATE_FILE"
fi
(
  cd "$BACKUP_ROOT"
  sha256sum \
    read_api.py \
    control-plane-unit-state.tsv \
    control-plane-unit-target-state.txt \
    >SHA256SUMS
  if [ -f systemd-unit-target ]; then
    sha256sum systemd-unit-target >>SHA256SUMS
  fi
)

cat >"$ROLLBACK_PATH" <<'ROLLBACK'
#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="${T:-/srv/trader-v3}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
OPERATION_LOCK="${OPERATION_LOCK:-/var/lock/trader-v3-account-stall-operation.lock}"
READ_API_RELATIVE="services/control-plane/api/read_api.py"
UNIT_NAME="trader-v3-controlplane.service"
READ_API_TARGET="$T/$READ_API_RELATIVE"
UNIT_TARGET="$SYSTEMD_UNIT_DIR/$UNIT_NAME"
UNIT_STATE_FILE="$BACKUP_ROOT/control-plane-unit-state.tsv"
UNIT_TARGET_STATE_FILE="$BACKUP_ROOT/control-plane-unit-target-state.txt"

rollback_die() {
  echo "FATAL: $*" >&2
  return 1
}

exec 9>"$OPERATION_LOCK"
flock -n 9 || rollback_die "another operation holds $OPERATION_LOCK"

(
  cd "$BACKUP_ROOT"
  sha256sum -c SHA256SUMS >/dev/null
) || rollback_die "backup SHA256 verification failed"

IFS=$'\t' read -r \
  ORIGINAL_ENABLEMENT \
  ORIGINAL_LOAD_STATE \
  ORIGINAL_FRAGMENT_PATH \
  ORIGINAL_ACTIVE_STATE \
  <"$UNIT_STATE_FILE" \
  || rollback_die "control-plane unit state snapshot is unreadable"
UNIT_TARGET_STATE="$(tr -d '\r\n' <"$UNIT_TARGET_STATE_FILE")"

case "$ORIGINAL_LOAD_STATE" in
  loaded)
    case "$ORIGINAL_ENABLEMENT" in
      enabled|disabled)
        ;;
      *)
        rollback_die "invalid original enablement"
        ;;
    esac
    case "$ORIGINAL_FRAGMENT_PATH" in
      /*)
        ;;
      *)
        rollback_die "invalid original FragmentPath"
        ;;
    esac
    case "$ORIGINAL_ACTIVE_STATE" in
      active|inactive)
        ;;
      *)
        rollback_die "invalid original active state"
        ;;
    esac
    ;;
  not-found)
    if [ "$ORIGINAL_ENABLEMENT" != "not-found" ] \
      || [ "$ORIGINAL_FRAGMENT_PATH" != "-" ] \
      || [ "$ORIGINAL_ACTIVE_STATE" != "inactive" ]; then
      rollback_die "invalid absent unit snapshot"
    fi
    ;;
  *)
    rollback_die "invalid original LoadState"
    ;;
esac
case "$UNIT_TARGET_STATE" in
  present|absent)
    ;;
  *)
    rollback_die "invalid unit target snapshot"
    ;;
esac

DEPLOYED_LOAD_STATE="$(
  systemctl show \
    --property=LoadState \
    --value \
    "$UNIT_NAME" \
    2>/dev/null || true
)"
case "$DEPLOYED_LOAD_STATE" in
  loaded)
    systemctl stop "$UNIT_NAME"
    systemctl disable "$UNIT_NAME"
    ;;
  not-found)
    systemctl stop "$UNIT_NAME" 2>/dev/null || true
    systemctl disable "$UNIT_NAME" 2>/dev/null || true
    ;;
  *)
    rollback_die "deployed unit LoadState is unsupported"
    ;;
esac
DEPLOYED_ENABLEMENT="$(
  systemctl is-enabled "$UNIT_NAME" 2>/dev/null || true
)"
case "$DEPLOYED_ENABLEMENT" in
  disabled|not-found)
    ;;
  *)
    rollback_die "deployed unit could not be disabled"
    ;;
esac

install -D -m 0644 "$BACKUP_ROOT/read_api.py" "$READ_API_TARGET"
case "$UNIT_TARGET_STATE" in
  present)
    rm -f -- "$UNIT_TARGET"
    mkdir -p "$(dirname "$UNIT_TARGET")"
    cp -a "$BACKUP_ROOT/systemd-unit-target" "$UNIT_TARGET"
    ;;
  absent)
    if [ -e "$UNIT_TARGET" ] && [ ! -f "$UNIT_TARGET" ]; then
      rollback_die "unit target changed type before rollback"
    fi
    rm -f -- "$UNIT_TARGET"
    ;;
esac

systemctl daemon-reload
RESTORED_LOAD_STATE="$(
  systemctl show \
    --property=LoadState \
    --value \
    "$UNIT_NAME"
)"
RESTORED_FRAGMENT_PATH="$(
  systemctl show \
    --property=FragmentPath \
    --value \
    "$UNIT_NAME"
)"

case "$ORIGINAL_LOAD_STATE" in
  loaded)
    if [ "$RESTORED_LOAD_STATE" != "loaded" ] \
      || [ "$RESTORED_FRAGMENT_PATH" != "$ORIGINAL_FRAGMENT_PATH" ]; then
      rollback_die "restored control-plane fragment changed"
    fi
    case "$ORIGINAL_ENABLEMENT" in
      enabled)
        systemctl enable "$UNIT_NAME"
        ;;
      disabled)
        systemctl disable "$UNIT_NAME"
        ;;
    esac
    RESTORED_ENABLEMENT="$(
      systemctl is-enabled "$UNIT_NAME" 2>/dev/null || true
    )"
    if [ "$RESTORED_ENABLEMENT" != "$ORIGINAL_ENABLEMENT" ]; then
      rollback_die "restored control-plane enablement changed"
    fi
    case "$ORIGINAL_ACTIVE_STATE" in
      active)
        systemctl start "$UNIT_NAME"
        systemctl is-active --quiet "$UNIT_NAME" \
          || rollback_die "restored control-plane unit is inactive"
        ;;
      inactive)
        RESTORED_ACTIVE_STATE="$(
          systemctl is-active "$UNIT_NAME" 2>/dev/null || true
        )"
        if [ "$RESTORED_ACTIVE_STATE" != "inactive" ]; then
          rollback_die "restored control-plane active state changed"
        fi
        ;;
    esac
    ;;
  not-found)
    if [ "$RESTORED_LOAD_STATE" != "not-found" ] \
      || [ -n "$RESTORED_FRAGMENT_PATH" ]; then
      rollback_die "absent control-plane fragment was not restored"
    fi
    RESTORED_ENABLEMENT="$(
      systemctl is-enabled "$UNIT_NAME" 2>/dev/null || true
    )"
    if [ "$RESTORED_ENABLEMENT" != "not-found" ]; then
      rollback_die "absent control-plane enablement was not restored"
    fi
    ;;
esac

BACKUP_READ_SHA="$(sha256sum "$BACKUP_ROOT/read_api.py" | awk '{print $1}')"
RESTORED_READ_SHA="$(sha256sum "$READ_API_TARGET" | awk '{print $1}')"
if [ "$BACKUP_READ_SHA" != "$RESTORED_READ_SHA" ]; then
  rollback_die "restored read API SHA256 mismatch"
fi
echo "rollback restored the control-plane read API and unit state"
ROLLBACK
chmod 0700 "$ROLLBACK_PATH"

MUTATION_STARTED=1

install -D -m 0644 "$READ_API_SOURCE" "$READ_API_TARGET"
install -D -m 0644 "$UNIT_SOURCE" "$UNIT_TARGET"

READ_API_TARGET_SHA="$(sha256_of "$READ_API_TARGET")"
if [ "$READ_API_TARGET_SHA" != "$READ_API_EXPECTED_SHA256" ]; then
  die "installed read API SHA256 mismatch"
fi
UNIT_TARGET_SHA="$(sha256_of "$UNIT_TARGET")"
if [ "$UNIT_TARGET_SHA" != "$UNIT_EXPECTED_SHA256" ]; then
  die "installed control-plane unit SHA256 mismatch"
fi
compile_python "$READ_API_TARGET"

systemctl daemon-reload
systemctl enable "$UNIT_NAME"
systemctl is-enabled --quiet "$UNIT_NAME" \
  || die "$UNIT_NAME is not enabled"

JOURNAL_SINCE="$(
  python3 - <<'PY'
import time

print(f"@{time.time():.6f}")
PY
)"
restart_control_plane_bounded "bootstrap"
systemctl is-active --quiet "$UNIT_NAME" \
  || die "$UNIT_NAME is inactive after bootstrap restart"
probe_sse_contract "after bootstrap restart"
verify_account_a_halted "after bootstrap restart"

start_sse_hold
if ! kill -0 "$SSE_PID" 2>/dev/null; then
  die "Caddy SSE hold ended before validation restart"
fi
restart_control_plane_bounded "SSE validation"
systemctl is-active --quiet "$UNIT_NAME" \
  || die "$UNIT_NAME is inactive after SSE validation restart"
wait_for_sse_hold_shutdown

JOURNAL_UNTIL="$(
  python3 - <<'PY'
import time

print(f"@{time.time():.6f}")
PY
)"
journalctl \
  -u "$UNIT_NAME" \
  --since "$JOURNAL_SINCE" \
  --until "$JOURNAL_UNTIL" \
  --no-pager \
  --output=cat \
  >"$JOURNAL_BODY"
if grep -Eqi \
  "stop.*timed out|timed out.*stop|result.*timeout|timeout.*result|SIGKILL|status=9/KILL|signal KILL" \
  "$JOURNAL_BODY"; then
  die "unsafe control-plane stop found in journal"
fi

probe_sse_contract "after restart"
verify_account_a_halted "after restart"
echo "update complete"
echo "backup: $BACKUP_ROOT"
echo "rollback: $ROLLBACK_PATH"
