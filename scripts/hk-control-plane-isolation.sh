#!/usr/bin/env bash
set -euo pipefail

TRADER_ROOT="${TRADER_ROOT:-/srv/trader-v3}"
API_ROOT="$TRADER_ROOT/services/control-plane/api"
VENV_ROOT="$TRADER_ROOT/.venv-cp"
CADDY_FILE="${CADDY_FILE:-/etc/caddy/Caddyfile}"
SYSTEMD_ROOT="${SYSTEMD_ROOT:-/etc/systemd/system}"
RESOURCE_ROOT="${ACCOUNT_STALL_SYSTEMD_RESOURCE_ROOT:-$TRADER_ROOT/infra/systemd}"
LEGACY_UNIT="${LEGACY_CONTROL_PLANE_UNIT:-trader-v3-controlplane.service}"
NODE_PORT="${CONTROL_PLANE_NODE_PORT:-8181}"
EVENT_PORT="${CONTROL_PLANE_EVENT_PORT:-8182}"
OPERATOR_PORT="${CONTROL_PLANE_OPERATOR_PORT:-8183}"
ROUTER_PORT="${CONTROL_PLANE_ROUTER_PORT:-8080}"
OPERATION_TIMEOUT_SECONDS="${ACCOUNT_STALL_OPERATION_TIMEOUT_SECONDS:-30}"
TIMEOUT_KILL_AFTER_SECONDS="${ACCOUNT_STALL_TIMEOUT_KILL_AFTER_SECONDS:-5}"
PROBE_TIMEOUT_SECONDS="${CONTROL_PLANE_PROBE_TIMEOUT_SECONDS:-5}"
DEFAULT_OPERATION_LOCK="/var/lock/trader-v3-account-stall-operation.lock"
OPERATION_LOCK="${ACCOUNT_STALL_OPERATION_LOCK:-$DEFAULT_OPERATION_LOCK}"
OPERATION_LOCK_OWNERSHIP="${ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP:-standalone}"
OPERATION_LOCK_FD="${ACCOUNT_STALL_OPERATION_LOCK_FD:-9}"
OPERATION_LOCK_TOKEN="${ACCOUNT_STALL_OPERATION_LOCK_TOKEN:-}"
OPERATION_LOCK_UID="${ACCOUNT_STALL_OPERATION_LOCK_UID:-$(id -u)}"
OPERATION_LOCK_GID="${ACCOUNT_STALL_OPERATION_LOCK_GID:-$(id -g)}"
MAINTENANCE_FENCE_OWNERSHIP="${ACCOUNT_STALL_MAINTENANCE_FENCE_OWNERSHIP:-standalone}"
MAINTENANCE_FENCE_ID="${ACCOUNT_STALL_MAINTENANCE_FENCE_ID:-}"
MAINTENANCE_FENCE_OWNER_TOKEN="${ACCOUNT_STALL_MAINTENANCE_FENCE_OWNER_TOKEN:-}"
MAINTENANCE_FENCE_ACTOR="${ACCOUNT_STALL_MAINTENANCE_FENCE_ACTOR:-control-plane-isolation}"
MAINTENANCE_FENCE_LEASE_SECONDS="${ACCOUNT_STALL_FENCE_LEASE_SECONDS:-120}"
MAINTENANCE_HEARTBEAT_MAX_AGE_SECONDS="${ACCOUNT_STALL_HEARTBEAT_MAX_AGE_SECONDS:-15}"
BOOTSTRAP_STOPPED_GATE="${ACCOUNT_STALL_BOOTSTRAP_STOPPED_GATE:-0}"
BOOTSTRAP_REDIS_FENCING_EPOCH="${ACCOUNT_STALL_BOOTSTRAP_REDIS_FENCING_EPOCH:-}"
REDIS_FENCING_EPOCH_KEY="${ACCOUNT_STALL_REDIS_FENCING_EPOCH_KEY:-trader-bot:redis-fencing-epoch}"
MAINTENANCE_FENCE_ACQUIRED=0
MIGRATION_RUNNER="$TRADER_ROOT/services/control-plane/db/migrate.py"
MAINTENANCE_DATABASE_ENV_FILE="${ACCOUNT_STALL_MAINTENANCE_DATABASE_ENV_FILE:-$TRADER_ROOT/.env.v3}"
STAMP="${CONTROL_PLANE_ISOLATION_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
BACKUP_ROOT="${CONTROL_PLANE_ISOLATION_BACKUP_ROOT:-$TRADER_ROOT/backups/control-plane-isolation-$STAMP}"
WRITER_RESOURCE_CONF="$RESOURCE_ROOT/account-stall-control-plane-writer.conf"
READER_RESOURCE_CONF="$RESOURCE_ROOT/account-stall-control-plane-reader.conf"
MARKER_BEGIN="# BEGIN trader-v3-control-plane-role-router"
MARKER_END="# END trader-v3-control-plane-role-router"
ROLE_UNITS=(
  "trader-v3-controlplane-node-control.service"
  "trader-v3-controlplane-event-ingest.service"
  "trader-v3-controlplane-operator-query.service"
)
ROLE_USERS=(
  "${CONTROL_PLANE_NODE_CONTROL_USER:-trader-v3-cp-node-control}"
  "${CONTROL_PLANE_EVENT_INGEST_USER:-trader-v3-cp-event-ingest}"
  "${CONTROL_PLANE_OPERATOR_QUERY_USER:-trader-v3-cp-operator-query}"
)
ROLE_GROUPS=(
  "${CONTROL_PLANE_NODE_CONTROL_GROUP:-trader-v3-cp-node-control}"
  "${CONTROL_PLANE_EVENT_INGEST_GROUP:-trader-v3-cp-event-ingest}"
  "${CONTROL_PLANE_OPERATOR_QUERY_GROUP:-trader-v3-cp-operator-query}"
)
ROLE_ENV_FILES=(
  "${CONTROL_PLANE_NODE_CONTROL_ENV_FILE:-$TRADER_ROOT/secrets/control-plane/node-control.env}"
  "${CONTROL_PLANE_EVENT_INGEST_ENV_FILE:-$TRADER_ROOT/secrets/control-plane/event-ingest.env}"
  "${CONTROL_PLANE_OPERATOR_QUERY_ENV_FILE:-$TRADER_ROOT/secrets/control-plane/operator-query.env}"
)
ROLE_DATABASE_ROLES=(
  "${CONTROL_PLANE_NODE_CONTROL_DATABASE_ROLE:-trader_v3_node_control}"
  "${CONTROL_PLANE_EVENT_INGEST_DATABASE_ROLE:-trader_v3_event_ingest}"
  "${CONTROL_PLANE_OPERATOR_QUERY_DATABASE_ROLE:-trader_v3_operator_query}"
)
SECRET_OWNER_UID="${CONTROL_PLANE_SECRET_OWNER_UID:-0}"
SECRET_OWNER_GID="${CONTROL_PLANE_SECRET_OWNER_GID:-0}"
SECRET_MODE="${CONTROL_PLANE_SECRET_MODE:-600}"
TOPOLOGY_FILE="topology.tsv"
METADATA_FILE="metadata.env"

die() {
  echo "FATAL: $*" >&2
  exit 2
}

assert_positive_integer() {
  label="$1"
  value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$label is not a positive integer"
  [ "$value" -gt 0 ] || die "$label is not a positive integer"
}

require_file() {
  [ -f "$1" ] || die "required file missing: $1"
}

verify_account_stall_operation_lock() {
  python3 - \
    "$OPERATION_LOCK" \
    "$OPERATION_LOCK_UID" \
    "$OPERATION_LOCK_GID" \
    "$OPERATION_LOCK_TOKEN" <<'PY'
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected_uid = int(sys.argv[2])
expected_gid = int(sys.argv[3])
expected_token = sys.argv[4]
if not expected_token:
    raise SystemExit("account-stall operation lock token is missing")
try:
    fd_metadata = os.fstat(9)
    path_metadata = os.lstat(path)
except OSError as exc:
    raise SystemExit("account-stall operation lock is unavailable") from exc
if not stat.S_ISREG(fd_metadata.st_mode):
    raise SystemExit("account-stall operation lock fd is not regular")
if not stat.S_ISREG(path_metadata.st_mode):
    raise SystemExit("account-stall operation lock path is not regular")
if (
    fd_metadata.st_dev != path_metadata.st_dev
    or fd_metadata.st_ino != path_metadata.st_ino
):
    raise SystemExit("account-stall operation lock inode changed")
if fd_metadata.st_nlink != 1 or path_metadata.st_nlink != 1:
    raise SystemExit("account-stall operation lock link count is invalid")
if stat.S_IMODE(fd_metadata.st_mode) != 0o600:
    raise SystemExit("account-stall operation lock mode must be 0600")
if fd_metadata.st_uid != expected_uid or fd_metadata.st_gid != expected_gid:
    raise SystemExit("account-stall operation lock owner mismatch")
try:
    recorded_token = os.pread(9, 4096, 0).decode("ascii").strip()
except (OSError, UnicodeDecodeError) as exc:
    raise SystemExit("account-stall operation lock token is unreadable") from exc
if recorded_token != expected_token:
    raise SystemExit("account-stall operation lock token mismatch")
PY
}

acquire_account_stall_operation_lock() {
  ownership_token=""
  [ -n "$OPERATION_LOCK" ] || die "account-stall operation lock path is empty"
  case "$OPERATION_LOCK" in
    /*)
      ;;
    *)
      die "account-stall operation lock path must be absolute"
      ;;
  esac
  [ "$OPERATION_LOCK_FD" = "9" ] \
    || die "account-stall inherited lock fd must be 9"
  case "$OPERATION_LOCK_OWNERSHIP" in
    inherited)
      [ -n "$OPERATION_LOCK_TOKEN" ] \
        || die "account-stall inherited lock token is missing"
      [ ! -L "$OPERATION_LOCK" ] \
        || die "account-stall inherited lock path cannot be a symlink"
      [ -e "/proc/$$/fd/9" ] \
        || die "account-stall inherited lock fd 9 is not open"
      [ "$OPERATION_LOCK" -ef "/proc/$$/fd/9" ] \
        || die "account-stall inherited lock fd does not match $OPERATION_LOCK"
      flock -n 9 \
        || die "account-stall inherited lock ownership is invalid"
      IFS= read -r ownership_token <"$OPERATION_LOCK" \
        || die "account-stall inherited lock token file is unreadable"
      [ "$ownership_token" = "$OPERATION_LOCK_TOKEN" ] \
        || die "account-stall inherited lock token mismatch"
      verify_account_stall_operation_lock
      ;;
    standalone)
      [ -L "$OPERATION_LOCK" ] \
        && die "account-stall operation lock cannot be a symlink"
      umask 077
      exec 9<>"$OPERATION_LOCK"
      [ -f "$OPERATION_LOCK" ] \
        || die "account-stall operation lock must be a regular file"
      flock -n 9 \
        || die "another account-stall operation holds $OPERATION_LOCK"
      ownership_token="$(
        python3 -c 'import secrets; print(secrets.token_hex(32))'
      )"
      [[ "$ownership_token" =~ ^[0-9a-f]{64}$ ]] \
        || die "account-stall operation ownership token is invalid"
      : >"$OPERATION_LOCK"
      printf '%s\n' "$ownership_token" >&9
      python3 - "$OPERATION_LOCK_UID" "$OPERATION_LOCK_GID" <<'PY'
import os
import sys

os.fchmod(9, 0o600)
os.fchown(9, int(sys.argv[1]), int(sys.argv[2]))
os.fsync(9)
PY
      ACCOUNT_STALL_OPERATION_LOCK="$OPERATION_LOCK"
      ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP="inherited"
      ACCOUNT_STALL_OPERATION_LOCK_FD="9"
      ACCOUNT_STALL_OPERATION_LOCK_TOKEN="$ownership_token"
      export \
        ACCOUNT_STALL_OPERATION_LOCK \
        ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP \
        ACCOUNT_STALL_OPERATION_LOCK_FD \
        ACCOUNT_STALL_OPERATION_LOCK_TOKEN
      OPERATION_LOCK_TOKEN="$ownership_token"
      verify_account_stall_operation_lock
      ;;
    *)
      die "invalid account-stall operation lock ownership: $OPERATION_LOCK_OWNERSHIP"
      ;;
  esac
}

run_timed_raw() {
  timeout \
    --kill-after="${TIMEOUT_KILL_AFTER_SECONDS}s" \
    "$OPERATION_TIMEOUT_SECONDS" \
    "$@"
}

maintenance_fence_command() {
  run_timed_raw \
    "$VENV_ROOT/bin/python" \
    "$MIGRATION_RUNNER" \
    maintenance-fence \
    "$@" \
    --database-env-file "$MAINTENANCE_DATABASE_ENV_FILE"
}

verify_maintenance_fence() {
  local stage="$1"
  if [ "$MAINTENANCE_FENCE_OWNERSHIP" = "bootstrap_stopped" ]; then
    verify_bootstrap_stopped_gate "$stage"
    return
  fi
  [ "$MAINTENANCE_FENCE_ACQUIRED" = "1" ] \
    || die "maintenance fence is not acquired"
  maintenance_fence_command \
    verify \
    --fence-id "$MAINTENANCE_FENCE_ID" \
    --owner-token "$MAINTENANCE_FENCE_OWNER_TOKEN" \
    --stage "$stage" \
    --lease-seconds "$MAINTENANCE_FENCE_LEASE_SECONDS" \
    --heartbeat-max-age-seconds \
      "$MAINTENANCE_HEARTBEAT_MAX_AGE_SECONDS" \
    >/dev/null
}

verify_mutation_guards() {
  local stage="$1"
  verify_account_stall_operation_lock
  verify_maintenance_fence "$stage"
}

verify_bootstrap_stopped_gate() {
  local stage="$1"
  local node
  local running
  local live_epoch
  verify_account_stall_operation_lock
  [ "$BOOTSTRAP_STOPPED_GATE" = "1" ] \
    || die "bootstrap stopped gate is disabled"
  [ -n "$BOOTSTRAP_REDIS_FENCING_EPOCH" ] \
    || die "bootstrap Redis fencing epoch is missing"
  for node in \
    trader-v3-node-a \
    trader-v3-node-b \
    trader-v3-node-c \
    trader-v3-node-d; do
    running="$(
      docker inspect --format '{{.State.Running}}' "$node" 2>/dev/null
    )" || die "bootstrap node is unavailable: $node"
    [ "$running" = "false" ] \
      || die "bootstrap control-plane mutation requires stopped node: $node"
  done
  live_epoch="$(
    docker exec trader-v3-redis redis-cli --raw GET \
      "$REDIS_FENCING_EPOCH_KEY" \
      | tr -d '\r'
  )" || die "bootstrap Redis fencing epoch marker is unavailable"
  [ "$live_epoch" = "$BOOTSTRAP_REDIS_FENCING_EPOCH" ] \
    || die "bootstrap Redis fencing epoch marker changed at $stage"
}

acquire_or_verify_maintenance_fence() {
  local receipt
  case "$MAINTENANCE_FENCE_OWNERSHIP" in
    inherited)
      [ -n "$MAINTENANCE_FENCE_ID" ] \
        || die "inherited maintenance fence id is missing"
      [ "$MAINTENANCE_FENCE_OWNER_TOKEN" = "$OPERATION_LOCK_TOKEN" ] \
        || die "inherited maintenance fence owner token mismatch"
      MAINTENANCE_FENCE_ACQUIRED=1
      verify_mutation_guards "isolation-inherited"
      ;;
    standalone)
      MAINTENANCE_FENCE_OWNER_TOKEN="$OPERATION_LOCK_TOKEN"
      receipt="$(
        maintenance_fence_command \
          acquire \
          --operation "control-plane-isolation" \
          --actor "$MAINTENANCE_FENCE_ACTOR" \
          --owner-token "$MAINTENANCE_FENCE_OWNER_TOKEN" \
          --lease-seconds "$MAINTENANCE_FENCE_LEASE_SECONDS" \
          --heartbeat-max-age-seconds \
            "$MAINTENANCE_HEARTBEAT_MAX_AGE_SECONDS"
      )" || die "maintenance fence acquisition failed"
      MAINTENANCE_FENCE_ID="$(
        python3 -c \
          'import json,sys; print(json.loads(sys.stdin.read())["fence_id"])' \
          <<<"$receipt"
      )" || die "maintenance fence receipt is invalid"
      MAINTENANCE_FENCE_ACQUIRED=1
      verify_mutation_guards "isolation-standalone"
      ;;
    bootstrap_stopped)
      verify_mutation_guards "isolation-bootstrap-stopped"
      ;;
    *)
      die "invalid maintenance fence ownership: $MAINTENANCE_FENCE_OWNERSHIP"
      ;;
  esac
}

release_maintenance_fence() {
  local reason="${1:-control-plane-isolation-exit}"
  if [ "$MAINTENANCE_FENCE_ACQUIRED" != "1" ]; then
    return
  fi
  if [ "$MAINTENANCE_FENCE_OWNERSHIP" = "inherited" ]; then
    return
  fi
  if ! verify_account_stall_operation_lock; then
    echo "FATAL: operation lock verification blocked fence release" >&2
    return
  fi
  if ! maintenance_fence_command \
    release \
    --fence-id "$MAINTENANCE_FENCE_ID" \
    --owner-token "$MAINTENANCE_FENCE_OWNER_TOKEN" \
    --actor "$MAINTENANCE_FENCE_ACTOR" \
    --reason "$reason" \
    >/dev/null; then
    echo "FATAL: maintenance fence release failed; lease remains bounded" >&2
    return
  fi
  MAINTENANCE_FENCE_ACQUIRED=0
}

run_timed() {
  verify_mutation_guards "isolation-command"
  run_timed_raw "$@"
}

validate_resource_conf() {
  resource_conf="$1"
  first_line="$(sed -n '1p' "$resource_conf")"
  [ "$first_line" = "[Service]" ] \
    || die "resource drop-in must start with [Service]: $resource_conf"
  grep -Eq '^MemoryMax=[1-9][0-9]*[KMGTP]?$' "$resource_conf" \
    || die "resource drop-in lacks MemoryMax: $resource_conf"
  grep -Fxq 'MemorySwapMax=0' "$resource_conf" \
    || die "resource drop-in must disable swap: $resource_conf"
  grep -Eq '^CPUQuota=[1-9][0-9]*%$' "$resource_conf" \
    || die "resource drop-in lacks CPUQuota: $resource_conf"
  grep -Eq '^TasksMax=[1-9][0-9]*$' "$resource_conf" \
    || die "resource drop-in lacks TasksMax: $resource_conf"
  grep -Eq '^LimitNOFILE=[1-9][0-9]*$' "$resource_conf" \
    || die "resource drop-in lacks LimitNOFILE: $resource_conf"
  grep -Eq '^Restart=(on-failure|always)$' "$resource_conf" \
    || die "resource drop-in lacks Restart policy: $resource_conf"
  grep -Eq '^RestartSec=[1-9][0-9]*s?$' "$resource_conf" \
    || die "resource drop-in lacks RestartSec: $resource_conf"
}

validate_role_identity_contract() {
  local index
  local user
  local group
  python3 - \
    "$SECRET_OWNER_UID" \
    "$SECRET_OWNER_GID" \
    "$SECRET_MODE" \
    "node-control" \
    "${ROLE_USERS[0]}" \
    "${ROLE_GROUPS[0]}" \
    "${ROLE_ENV_FILES[0]}" \
    "${ROLE_DATABASE_ROLES[0]}" \
    "event-ingest" \
    "${ROLE_USERS[1]}" \
    "${ROLE_GROUPS[1]}" \
    "${ROLE_ENV_FILES[1]}" \
    "${ROLE_DATABASE_ROLES[1]}" \
    "operator-query" \
    "${ROLE_USERS[2]}" \
    "${ROLE_GROUPS[2]}" \
    "${ROLE_ENV_FILES[2]}" \
    "${ROLE_DATABASE_ROLES[2]}" <<'PY'
from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


def required_integer(value: str, label: str) -> int:
    if not re.fullmatch(r"(0|[1-9][0-9]*)", value):
        raise SystemExit(f"{label} is invalid")
    return int(value)


def read_database_url(path: Path, role: str) -> str:
    values = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "DATABASE_URL":
            continue
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values.append(value)
    if len(values) != 1 or not values[0]:
        raise SystemExit(
            f"{role} secret must contain exactly one DATABASE_URL"
        )
    return values[0]


owner_uid = required_integer(sys.argv[1], "secret owner uid")
owner_gid = required_integer(sys.argv[2], "secret owner gid")
mode_text = sys.argv[3]
if not re.fullmatch(r"[0-7]{3,4}", mode_text):
    raise SystemExit("secret mode is invalid")
expected_mode = int(mode_text, 8)
raw_specs = sys.argv[4:]
if len(raw_specs) != 15:
    raise SystemExit("control-plane role identity specification is invalid")
specs = [
    tuple(raw_specs[index:index + 5])
    for index in range(0, len(raw_specs), 5)
]
users = [spec[1] for spec in specs]
groups = [spec[2] for spec in specs]
env_paths = [spec[3] for spec in specs]
database_roles = [spec[4] for spec in specs]
for label, values in (
    ("service users", users),
    ("service groups", groups),
    ("secret paths", env_paths),
    ("database roles", database_roles),
):
    if len(set(values)) != len(values):
        raise SystemExit(f"control-plane {label} must be independent")

database_urls = []
for role, user, group, env_raw, expected_database_role in specs:
    for identity_label, identity in (("user", user), ("group", group)):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", identity):
            raise SystemExit(
                f"{role} service {identity_label} is invalid"
            )
    if not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_$-]{0,62}",
        expected_database_role,
    ):
        raise SystemExit(f"{role} expected DATABASE_URL role is invalid")
    env_path = Path(env_raw)
    if not env_path.is_absolute():
        raise SystemExit(f"{role} secret path must be absolute")
    if env_path.is_symlink():
        raise SystemExit(f"{role} secret path cannot be a symlink")
    try:
        metadata = env_path.stat()
    except OSError as exc:
        raise SystemExit(f"{role} secret is unavailable: {env_path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"{role} secret must be a regular file")
    actual_mode = stat.S_IMODE(metadata.st_mode)
    if actual_mode != expected_mode:
        raise SystemExit(
            f"{role} secret mode mismatch: "
            f"expected={mode_text} actual={actual_mode:03o}"
        )
    if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
        raise SystemExit(
            f"{role} secret owner mismatch: "
            f"expected={owner_uid}:{owner_gid} "
            f"actual={metadata.st_uid}:{metadata.st_gid}"
        )
    database_url = read_database_url(env_path, role)
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise SystemExit(f"{role} DATABASE_URL scheme is invalid")
    database_role = unquote(parsed.username or "")
    if database_role != expected_database_role:
        raise SystemExit(
            f"{role} DATABASE_URL role mismatch: "
            f"expected={expected_database_role} actual={database_role}"
        )
    database_urls.append(database_url)
if len(set(database_urls)) != len(database_urls):
    raise SystemExit("control-plane DATABASE_URL values must not be reused")
PY
  for index in "${!ROLE_USERS[@]}"; do
    user="${ROLE_USERS[$index]}"
    group="${ROLE_GROUPS[$index]}"
    getent passwd "$user" >/dev/null \
      || die "control-plane service user does not exist: $user"
    getent group "$group" >/dev/null \
      || die "control-plane service group does not exist: $group"
  done
}

hash_file() {
  path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
    return
  fi
  shasum -a 256 "$path" | awk '{print $1}'
}

systemctl_state() {
  state_command="$1"
  unit="$2"
  value="$(run_timed systemctl "$state_command" "$unit" 2>/dev/null || true)"
  value="${value%%$'\n'*}"
  if [ -z "$value" ]; then
    value="unknown"
  fi
  printf '%s\n' "$value"
}

write_artifact_metadata() {
  artifact_root="$1"
  {
    printf 'CADDY_FILE=%q\n' "$CADDY_FILE"
    printf 'SYSTEMD_ROOT=%q\n' "$SYSTEMD_ROOT"
    printf 'LEGACY_UNIT=%q\n' "$LEGACY_UNIT"
  } > "$artifact_root/$METADATA_FILE" || return 1
  chmod 0600 "$artifact_root/$METADATA_FILE" || return 1
}

snapshot_unit() {
  unit="$1"
  artifact_root="$2"
  destination="$SYSTEMD_ROOT/$unit"
  enabled_state="$(systemctl_state is-enabled "$unit")"
  active_state="$(systemctl_state is-active "$unit")"

  if [ ! -f "$destination" ]; then
    case "$enabled_state" in
      disabled|not-found|unknown)
        ;;
      *)
        die "absent unit has unsupported enabled state for $unit: $enabled_state"
        ;;
    esac
    case "$active_state" in
      inactive|unknown)
        ;;
      *)
        die "absent unit has unsupported active state for $unit: $active_state"
        ;;
    esac
    printf '%s\t0\t-\t%s\t%s\n' \
      "$unit" "$enabled_state" "$active_state" \
      >> "$artifact_root/$TOPOLOGY_FILE" || return 1
    return
  fi

  case "$enabled_state" in
    enabled|disabled|static|indirect|generated|transient|alias)
      ;;
    *)
      die "unsupported enabled state for $unit: $enabled_state"
      ;;
  esac
  case "$active_state" in
    active|inactive)
      ;;
    *)
      die "unsupported active state for $unit: $active_state"
      ;;
  esac

  run_timed cp -a "$destination" "$artifact_root/units/$unit" || return 1
  unit_hash="$(hash_file "$destination")" || return 1
  printf '%s\t1\t%s\t%s\t%s\n' \
    "$unit" "$unit_hash" "$enabled_state" "$active_state" \
    >> "$artifact_root/$TOPOLOGY_FILE" || return 1
}

snapshot_topology_into() {
  artifact_root="$1"
  mkdir -p "$artifact_root/units" || return 1
  chmod 0700 "$artifact_root" || return 1
  require_file "$CADDY_FILE"
  run_timed cp -a "$CADDY_FILE" "$artifact_root/Caddyfile.before" \
    || return 1
  hash_file "$CADDY_FILE" > "$artifact_root/Caddyfile.before.sha256" \
    || return 1
  : > "$artifact_root/$TOPOLOGY_FILE" || return 1
  snapshot_unit "$LEGACY_UNIT" "$artifact_root" || return 1
  for unit in "${ROLE_UNITS[@]}"; do
    snapshot_unit "$unit" "$artifact_root" || return 1
  done
  write_artifact_metadata "$artifact_root" || return 1
  printf 'bash %q --rollback %q\n' \
    "${BASH_SOURCE[0]}" "$BACKUP_ROOT" \
    > "$artifact_root/rollback-command.txt" || return 1
  chmod 0600 \
    "$artifact_root/Caddyfile.before.sha256" \
    "$artifact_root/$TOPOLOGY_FILE" \
    "$artifact_root/rollback-command.txt" || return 1
}

snapshot_topology() {
  parent="$(dirname "$BACKUP_ROOT")"
  [ ! -e "$BACKUP_ROOT" ] \
    || die "rollback artifact already exists: $BACKUP_ROOT"
  mkdir -p "$parent"
  staging_root="$(mktemp -d "$parent/.control-plane-isolation.tmp.XXXXXX")"
  if ! (snapshot_topology_into "$staging_root"); then
    run_timed rm -rf "$staging_root" >/dev/null 2>&1 || true
    die "control-plane topology snapshot failed"
  fi
  if ! run_timed mv "$staging_root" "$BACKUP_ROOT"; then
    run_timed rm -rf "$staging_root" >/dev/null 2>&1 || true
    die "control-plane topology snapshot publish failed"
  fi
}

load_artifact_metadata() {
  artifact_root="$1"
  if [ ! -f "$artifact_root/$METADATA_FILE" ]; then
    echo "required rollback metadata missing: $artifact_root/$METADATA_FILE" >&2
    return 1
  fi
  # shellcheck source=/dev/null
  source "$artifact_root/$METADATA_FILE" || return 1
}

restore_require_file() {
  if [ -f "$1" ]; then
    return 0
  fi
  echo "required rollback file missing: $1" >&2
  return 1
}

atomic_restore_file() {
  source_path="$1"
  destination="$2"
  expected_hash="$3"
  destination_root="$(dirname "$destination")"
  destination_name="$(basename "$destination")"
  temporary="$(
    mktemp "$destination_root/.${destination_name}.rollback.XXXXXX"
  )"
  if ! run_timed cp -a "$source_path" "$temporary"; then
    run_timed rm -f "$temporary" >/dev/null 2>&1 || true
    return 1
  fi
  actual_hash="$(hash_file "$temporary")"
  if [ "$actual_hash" != "$expected_hash" ]; then
    echo "rollback source hash differs for $destination_name" >&2
    run_timed rm -f "$temporary" >/dev/null 2>&1 || true
    return 1
  fi
  if ! run_timed mv -f "$temporary" "$destination"; then
    run_timed rm -f "$temporary" >/dev/null 2>&1 || true
    return 1
  fi
}

restore_enabled_state() {
  unit="$1"
  expected="$2"
  case "$expected" in
    enabled)
      run_timed systemctl enable "$unit"
      ;;
    disabled)
      run_timed systemctl disable "$unit"
      ;;
    static|indirect|generated|transient|alias)
      run_timed systemctl disable "$unit" >/dev/null 2>&1 || true
      ;;
    *)
      echo "unsupported rollback enabled state for $unit: $expected" >&2
      return 1
      ;;
  esac
}

restore_active_state() {
  unit="$1"
  expected="$2"
  case "$expected" in
    active)
      run_timed systemctl start "$unit"
      ;;
    inactive)
      run_timed systemctl stop "$unit"
      ;;
    *)
      echo "unsupported rollback active state for $unit: $expected" >&2
      return 1
      ;;
  esac
}

restore_topology() {
  artifact_root="$1"
  restore_require_file "$artifact_root/$TOPOLOGY_FILE" || return 1
  restore_require_file "$artifact_root/Caddyfile.before" || return 1
  restore_require_file "$artifact_root/Caddyfile.before.sha256" || return 1

  while IFS=$'\t' read -r unit existed _hash _enabled _active; do
    run_timed systemctl disable --now "$unit" >/dev/null 2>&1 || true
  done < "$artifact_root/$TOPOLOGY_FILE"

  while IFS=$'\t' read -r unit existed _hash _enabled _active; do
    destination="$SYSTEMD_ROOT/$unit"
    if [ "$existed" = "1" ]; then
      restore_require_file "$artifact_root/units/$unit" || return 1
      atomic_restore_file \
        "$artifact_root/units/$unit" \
        "$destination" \
        "$_hash" || return 1
    else
      run_timed rm -f "$destination" || return 1
    fi
  done < "$artifact_root/$TOPOLOGY_FILE"
  expected_caddy_hash="$(
    tr -d '[:space:]' < "$artifact_root/Caddyfile.before.sha256"
  )"
  atomic_restore_file \
    "$artifact_root/Caddyfile.before" \
    "$CADDY_FILE" \
    "$expected_caddy_hash" || return 1
  run_timed systemctl daemon-reload || return 1

  while IFS=$'\t' read -r unit existed _hash enabled_state active_state; do
    if [ "$existed" != "1" ]; then
      continue
    fi
    restore_enabled_state "$unit" "$enabled_state" || return 1
    restore_active_state "$unit" "$active_state" || return 1
  done < "$artifact_root/$TOPOLOGY_FILE"

  run_timed caddy validate --config "$CADDY_FILE" || return 1
  run_timed systemctl reload caddy.service || return 1
  run_timed systemctl is-active --quiet caddy.service || return 1
  assert_topology "$artifact_root" || return 1
}

assert_topology() {
  artifact_root="$1"
  expected_caddy_hash="$(
    tr -d '[:space:]' < "$artifact_root/Caddyfile.before.sha256"
  )" || return 1
  actual_caddy_hash="$(hash_file "$CADDY_FILE")" || return 1
  [ "$actual_caddy_hash" = "$expected_caddy_hash" ] || {
    echo "Caddy hash differs from rollback snapshot" >&2
    return 1
  }

  while IFS=$'\t' read -r unit existed expected_hash \
    enabled_state active_state; do
    destination="$SYSTEMD_ROOT/$unit"
    if [ "$existed" = "0" ]; then
      [ ! -e "$destination" ] || {
        echo "unit created during migration still exists: $unit" >&2
        return 1
      }
    else
      [ -f "$destination" ] || {
        echo "unit missing after rollback: $unit" >&2
        return 1
      }
      actual_hash="$(hash_file "$destination")" || return 1
      [ "$actual_hash" = "$expected_hash" ] || {
        echo "unit hash differs after rollback: $unit" >&2
        return 1
      }
    fi
    actual_enabled="$(systemctl_state is-enabled "$unit")"
    [ "$actual_enabled" = "$enabled_state" ] || {
      echo "unit enabled state differs after rollback: $unit" >&2
      return 1
    }
    actual_active="$(systemctl_state is-active "$unit")"
    [ "$actual_active" = "$active_state" ] || {
      echo "unit active state differs after rollback: $unit" >&2
      return 1
    }
  done < "$artifact_root/$TOPOLOGY_FILE"
}

fail_closed_control_plane() {
  echo "FAIL-CLOSED: stopping all control-plane roles" >&2
  run_timed systemctl disable --now "$LEGACY_UNIT" >/dev/null 2>&1 || true
  for unit in "${ROLE_UNITS[@]}"; do
    run_timed systemctl disable --now "$unit" >/dev/null 2>&1 || true
  done
}

restore_topology_fail_closed() {
  artifact_root="$1"
  if restore_topology "$artifact_root"; then
    return 0
  fi
  fail_closed_control_plane
  return 1
}

assert_positive_integer \
  "account-stall operation timeout" \
  "$OPERATION_TIMEOUT_SECONDS"
assert_positive_integer \
  "account-stall timeout kill-after" \
  "$TIMEOUT_KILL_AFTER_SECONDS"
assert_positive_integer \
  "control-plane probe timeout" \
  "$PROBE_TIMEOUT_SECONDS"
assert_positive_integer \
  "maintenance fence lease" \
  "$MAINTENANCE_FENCE_LEASE_SECONDS"
assert_positive_integer \
  "maintenance heartbeat max age" \
  "$MAINTENANCE_HEARTBEAT_MAX_AGE_SECONDS"
command -v flock >/dev/null || die "flock missing"
command -v timeout >/dev/null || die "timeout missing"
command -v caddy >/dev/null || die "caddy missing"
command -v systemctl >/dev/null || die "systemctl missing"
command -v python3 >/dev/null || die "python3 missing"
command -v getent >/dev/null || die "getent missing"
if [ "$MAINTENANCE_FENCE_OWNERSHIP" = "bootstrap_stopped" ]; then
  command -v docker >/dev/null || die "docker missing"
fi
acquire_account_stall_operation_lock
require_file "$MIGRATION_RUNNER"
require_file "$MAINTENANCE_DATABASE_ENV_FILE"
[ -x "$VENV_ROOT/bin/python" ] || die "control-plane python missing"
acquire_or_verify_maintenance_fence
trap 'status=$?; release_maintenance_fence "control-plane-isolation-exit"; exit "$status"' EXIT

if [ "${1:-}" = "--rollback" ]; then
  [ "$#" -eq 2 ] || die "usage: $0 --rollback <artifact-root>"
  BACKUP_ROOT="$2"
  if ! load_artifact_metadata "$BACKUP_ROOT"; then
    fail_closed_control_plane
    die "rollback metadata is unavailable; services remain stopped"
  fi
  if ! restore_topology_fail_closed "$BACKUP_ROOT"; then
    die "control-plane topology rollback failed; services remain stopped"
  fi
  release_maintenance_fence "control-plane-isolation-rollback-complete"
  trap - EXIT
  echo "CONTROL_PLANE_ISOLATION_ROLLBACK_OK backup=$BACKUP_ROOT"
  exit 0
fi
[ "$#" -eq 0 ] || die "usage: $0 [--rollback <artifact-root>]"

require_file "$API_ROOT/read_api.py"
require_file "$API_ROOT/app_roles.py"
require_file "$TRADER_ROOT/services/control-plane/db/pools.py"
require_file "$CADDY_FILE"
require_file "$WRITER_RESOURCE_CONF"
require_file "$READER_RESOURCE_CONF"
[ -x "$VENV_ROOT/bin/uvicorn" ] || die "uvicorn missing"
validate_resource_conf "$WRITER_RESOURCE_CONF"
validate_resource_conf "$READER_RESOURCE_CONF"
validate_role_identity_contract

snapshot_topology

rollback_required=1
rollback() {
  status=$?
  trap - EXIT
  if [ -n "${router_block:-}" ]; then
    run_timed rm -f "$router_block" >/dev/null 2>&1 || true
  fi
  if [ -n "${caddy_temporary:-}" ]; then
    run_timed rm -f "$caddy_temporary" >/dev/null 2>&1 || true
  fi
  if [ "$rollback_required" -eq 0 ]; then
    release_maintenance_fence "control-plane-isolation-complete"
    exit "$status"
  fi
  echo "ROLLBACK: restoring pre-migration control-plane topology" >&2
  if ! restore_topology_fail_closed "$BACKUP_ROOT"; then
    echo "FATAL: control-plane topology rollback failed; services remain stopped" >&2
    release_maintenance_fence "control-plane-isolation-rollback-failed"
    exit 3
  fi
  echo "ROLLBACK: pre-migration topology restored" >&2
  release_maintenance_fence "control-plane-isolation-rolled-back"
  exit "$status"
}
trap rollback EXIT

write_role_unit() {
  unit_name="$1"
  role="$2"
  port="$3"
  resource_conf="$4"
  service_user="$5"
  service_group="$6"
  env_file="$7"
  expected_database_role="$8"
  destination="$SYSTEMD_ROOT/$unit_name"
  temporary="$(mktemp "$SYSTEMD_ROOT/.${unit_name}.new.XXXXXX")"
  cat > "$temporary" <<EOF
[Unit]
Description=Trader v3 control-plane role $role
After=network.target

[Service]
User=$service_user
Group=$service_group
WorkingDirectory=$API_ROOT
EnvironmentFile=$env_file
Environment=CONTROL_PLANE_APP_ROLE=$role
Environment=CONTROL_PLANE_EXPECT_DATABASE_ROLE=$expected_database_role
ExecStart=$VENV_ROOT/bin/uvicorn read_api:app --host 127.0.0.1 --port $port
TimeoutStopSec=15
EOF
  sed '1d' "$resource_conf" >> "$temporary"
  cat >> "$temporary" <<'EOF'

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "$temporary"
  if ! run_timed mv -f "$temporary" "$destination"; then
    run_timed rm -f "$temporary" >/dev/null 2>&1 || true
    return 1
  fi
}

write_role_unit \
  "${ROLE_UNITS[0]}" \
  "node-control" \
  "$NODE_PORT" \
  "$WRITER_RESOURCE_CONF" \
  "${ROLE_USERS[0]}" \
  "${ROLE_GROUPS[0]}" \
  "${ROLE_ENV_FILES[0]}" \
  "${ROLE_DATABASE_ROLES[0]}"
write_role_unit \
  "${ROLE_UNITS[1]}" \
  "event-ingest" \
  "$EVENT_PORT" \
  "$WRITER_RESOURCE_CONF" \
  "${ROLE_USERS[1]}" \
  "${ROLE_GROUPS[1]}" \
  "${ROLE_ENV_FILES[1]}" \
  "${ROLE_DATABASE_ROLES[1]}"
write_role_unit \
  "${ROLE_UNITS[2]}" \
  "operator-query" \
  "$OPERATOR_PORT" \
  "$READER_RESOURCE_CONF" \
  "${ROLE_USERS[2]}" \
  "${ROLE_GROUPS[2]}" \
  "${ROLE_ENV_FILES[2]}" \
  "${ROLE_DATABASE_ROLES[2]}"
run_timed systemctl daemon-reload
run_timed systemctl stop "$LEGACY_UNIT"
for unit in "${ROLE_UNITS[@]}"; do
  run_timed systemctl enable --now "$unit"
done

run_timed "$VENV_ROOT/bin/python" - \
  "$NODE_PORT" "$EVENT_PORT" "$OPERATOR_PORT" \
  "$PROBE_TIMEOUT_SECONDS" \
  "${ROLE_DATABASE_ROLES[0]}" \
  "${ROLE_DATABASE_ROLES[1]}" \
  "${ROLE_DATABASE_ROLES[2]}" <<'PY'
from __future__ import annotations

import json
import sys
from urllib.request import urlopen

expected = {
    int(sys.argv[1]): {
        "/health/role",
        "/v1/nodes/{node_id}/heartbeat",
        "/v1/nodes/{node_id}/incidents",
        "/v1/nodes/{node_id}/commands",
        "/v1/nodes/{node_id}/intents",
    },
    int(sys.argv[2]): {
        "/health/role",
        "/v1/nodes/{node_id}/events",
        "/v1/nodes/{node_id}/execution-events",
    },
    int(sys.argv[3]): {
        "/health/role",
        "/v1/nodes",
        "/v1/orders",
        "/v1/positions",
        "/v1/operator/orders",
    },
}
probe_timeout = int(sys.argv[4])
database_roles = {
    int(sys.argv[1]): sys.argv[5],
    int(sys.argv[2]): sys.argv[6],
    int(sys.argv[3]): sys.argv[7],
}
for port, required_paths in expected.items():
    with urlopen(
        f"http://127.0.0.1:{port}/openapi.json",
        timeout=probe_timeout,
    ) as response:
        document = json.load(response)
    paths = set(document.get("paths") or {})
    missing = sorted(required_paths - paths)
    if missing:
        raise SystemExit(
            f"role port {port} lacks routes: {missing}"
        )
    with urlopen(
        f"http://127.0.0.1:{port}/health/role",
        timeout=probe_timeout,
    ) as response:
        if response.status != 200:
            raise SystemExit(
                f"role port {port} health returned {response.status}"
            )
        health = json.load(response)
    expected_database_role = database_roles[port]
    if health != {
        "status": "healthy",
        "app_role": health.get("app_role"),
        "expected_database_role": expected_database_role,
        "session_user": expected_database_role,
        "current_user": expected_database_role,
        "rollback_only_permission_probe": "pass",
    }:
        raise SystemExit(
            f"role port {port} database identity health mismatch"
        )
all_paths = {
    port: set(
        json.load(
            urlopen(
                f"http://127.0.0.1:{port}/openapi.json",
                timeout=probe_timeout,
            )
        ).get("paths")
        or {}
    )
    for port in expected
}
if (
    "/v1/nodes/{node_id}/heartbeat"
    in all_paths[int(sys.argv[2])]
):
    raise SystemExit("event-ingest exposes heartbeat route")
if (
    "/v1/nodes/{node_id}/execution-events"
    in all_paths[int(sys.argv[3])]
):
    raise SystemExit("operator-query exposes event ingest route")
PY

router_block="$(mktemp)"
cat > "$router_block" <<EOF
$MARKER_BEGIN
http://127.0.0.1:$ROUTER_PORT {
	@event_ingest path_regexp event_ingest ^/v1/nodes/[^/]+/(events|execution-events)$
	handle @event_ingest {
		reverse_proxy 127.0.0.1:$EVENT_PORT
	}

	@node_control path_regexp node_control ^/v1/nodes/[^/]+/(heartbeat|incidents|commands(/[^/]+/ack)?|intents(/[^/]+/ack)?|exchange-state)$
	handle @node_control {
		reverse_proxy 127.0.0.1:$NODE_PORT
	}

	@account_generated path_regexp account_generated ^/v1/accounts/[^/]+$
	handle @account_generated {
		reverse_proxy 127.0.0.1:$NODE_PORT
	}

	handle {
		reverse_proxy 127.0.0.1:$OPERATOR_PORT
	}
}
$MARKER_END
EOF

caddy_root="$(dirname "$CADDY_FILE")"
caddy_name="$(basename "$CADDY_FILE")"
caddy_temporary="$(
  mktemp "$caddy_root/.${caddy_name}.account-stall.XXXXXX"
)"
run_timed cp -a "$CADDY_FILE" "$caddy_temporary"
run_timed python3 - "$caddy_temporary" "$router_block" \
  "$MARKER_BEGIN" "$MARKER_END" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

caddy_path = Path(sys.argv[1])
block_path = Path(sys.argv[2])
begin = sys.argv[3]
end = sys.argv[4]
text = caddy_path.read_text(encoding="utf-8")
start = text.find(begin)
if start >= 0:
    finish = text.find(end, start)
    if finish < 0:
        raise SystemExit("control-plane router marker is incomplete")
    finish += len(end)
    text = text[:start].rstrip() + "\n" + text[finish:].lstrip()
block = block_path.read_text(encoding="utf-8").strip()
caddy_path.write_text(text.rstrip() + "\n\n" + block + "\n", encoding="utf-8")
PY
run_timed rm -f "$router_block"
router_block=""

run_timed caddy validate --config "$caddy_temporary"
run_timed mv -f "$caddy_temporary" "$CADDY_FILE"
caddy_temporary=""
run_timed systemctl reload caddy.service
run_timed systemctl is-active --quiet caddy.service

run_timed "$VENV_ROOT/bin/python" - \
  "$ROUTER_PORT" "$PROBE_TIMEOUT_SECONDS" <<'PY'
from __future__ import annotations

import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

port = int(sys.argv[1])
probe_timeout = int(sys.argv[2])
probes = (
    Request(
        f"http://127.0.0.1:{port}/v1/nodes/node-a/commands?account_id=account-a"
    ),
    Request(
        f"http://127.0.0.1:{port}/v1/nodes/node-a/execution-events",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    ),
    Request(f"http://127.0.0.1:{port}/v1/nodes"),
)
# systemctl reload applies the caddy config asynchronously, so the
# router listener may need a moment; retry within a bounded window and
# still fail closed once it expires.
deadline = time.monotonic() + 30.0
for request in probes:
    while True:
        try:
            with urlopen(request, timeout=probe_timeout) as response:
                status = response.status
            break
        except HTTPError as exc:
            status = exc.code
            break
        except URLError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1.0)
    if status == 404:
        raise SystemExit(f"router returned 404 for {request.full_url}")
PY

run_timed systemctl disable "$LEGACY_UNIT"
for unit in "${ROLE_UNITS[@]}"; do
  run_timed systemctl is-active --quiet "$unit"
done
run_timed ss -ltn | grep -q "127.0.0.1:$ROUTER_PORT"

rollback_required=0
trap - EXIT
release_maintenance_fence "control-plane-isolation-complete"
echo "CONTROL_PLANE_ISOLATION_OK backup=$BACKUP_ROOT rollback_artifact=$BACKUP_ROOT/rollback-command.txt"
