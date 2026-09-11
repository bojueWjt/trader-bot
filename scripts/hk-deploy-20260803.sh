#!/usr/bin/env bash
# Account-stall hardening deployment — run as root ON hk.
# Immutable derived image and isolated control-plane roles are mandatory for
# reviewed hardening rollouts. Legacy topology is reserved for an explicit
# emergency rollback which remains HALTED.
# Fail-closed: any error leaves nodes HALTED/stopped and prints rollback commands.
# SKIP_RESUME defaults to 1. Set SKIP_RESUME=0 only in an audited resume window.
set -Eeuo pipefail

DEPLOY_PHASE="${1:-execute}"
case "$DEPLOY_PHASE" in
  preflight|execute)
    ;;
  *)
    echo "usage: $0 [preflight|execute]" >&2
    exit 2
    ;;
esac

T=/srv/trader-v3
STAGING="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TAG="deploy-${STAMP}-account-stall-hardening"
BACKUP_ROOT="$T/backups/$TAG"
WATCHER_TRADING_DB="${WATCHER_TRADING_DB:-/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db}"
FOUR_CHANNEL_MAPPING_EVIDENCE="$BACKUP_ROOT/four-channel-account-mapping-pre-watcher.json"
FOUR_CHANNEL_MAPPING_POST_WATCHER_EVIDENCE="$BACKUP_ROOT/four-channel-account-mapping-post-watcher.json"
FOUR_CHANNEL_MAPPING_ROLLBACK_EVIDENCE="$BACKUP_ROOT/four-channel-account-mapping-rollback.json"
ACCOUNT_STALL_OPERATION_LOCK="${ACCOUNT_STALL_OPERATION_LOCK:-/var/lock/trader-v3-account-stall-operation.lock}"
ACCOUNT_STALL_OPERATION_LOCK_UID="${ACCOUNT_STALL_OPERATION_LOCK_UID:-$(id -u)}"
ACCOUNT_STALL_OPERATION_LOCK_GID="${ACCOUNT_STALL_OPERATION_LOCK_GID:-$(id -g)}"
ACCOUNT_STALL_OPERATION_TIMEOUT_SECONDS="${ACCOUNT_STALL_OPERATION_TIMEOUT_SECONDS:-30}"
ACCOUNT_STALL_FENCE_LEASE_SECONDS="${ACCOUNT_STALL_FENCE_LEASE_SECONDS:-120}"
ACCOUNT_STALL_HEARTBEAT_MAX_AGE_SECONDS="${ACCOUNT_STALL_HEARTBEAT_MAX_AGE_SECONDS:-15}"
NODE_STARTUP_MEMINFO_PATH="${NODE_STARTUP_MEMINFO_PATH:-/proc/meminfo}"
NODE_STARTUP_MIN_AVAILABLE_BYTES=$((3 * 1024 * 1024 * 1024))
NODE_STARTUP_RESOURCE_EVIDENCE="$BACKUP_ROOT/node-startup-resources.jsonl"
NODE_RELEASE_MEMORY_LIMIT_BYTES=""
RELEASE_TOOL="$STAGING/release_manifest.py"
REVIEWED_ROLLOUT_TOOL="$STAGING/reviewed_release_rollout.py"
GEN_RECREATE="$STAGING/hk-gen-recreate-patched.py"
LIVE_NODE_CONFIG_TOOL="$STAGING/live_node_config.py"
LIVE_RISK_POLICY="$STAGING/live-risk-policy.json"
IMMUTABLE_BUILDER="$STAGING/build_immutable_node_image.py"
IMMUTABLE_WATCHER_BUILDER="$STAGING/build_immutable_watcher_image.py"
WATCHER_RUNTIME_MANIFEST="$STAGING/watcher-runtime-manifest.json"
LIVE_TRADE_EXECUTOR="$STAGING/account_a_live_trade_executor.py"
LIVE_TRADE_HTTP_ADAPTER="$STAGING/account_a_live_trade_http_adapter.py"
BOOTSTRAP_CONTROL_PLANE_ROLES="$STAGING/bootstrap_control_plane_roles.py"
RELEASE_MANIFEST="$STAGING/release-manifest.json"
RELEASE_SOURCE_MANIFEST="$STAGING/release-source-manifest.json"
SYSTEMD_RESOURCE_CONTRACT="$STAGING/systemd-resource-contract.json"
DEPENDENCY_LOCK="${RELEASE_DEPENDENCY_LOCK:-$STAGING/uv.node.lock}"
PG_BACKUP_TIMEOUT_SECONDS="${PG_BACKUP_TIMEOUT_SECONDS:-300}"
PG_DUMP_BIN="${PG_DUMP_BIN:-pg_dump}"
PG_RESTORE_BIN="${PG_RESTORE_BIN:-pg_restore}"
MIGRATION_RUNNER="$STAGING/services/control-plane/db/migrate.py"
MIGRATION_ORDER_MANAGEMENT_UP="$STAGING/db/migrations/0005_order_management.up.sql"
MIGRATION_EVIDENCE_INDEXES_UP="$STAGING/db/migrations/0010_evidence_and_poll_indexes.up.sql"
MIGRATION_EVIDENCE_INDEXES_DOWN="$STAGING/db/migrations/0010_evidence_and_poll_indexes.down.sql"
MIGRATION_UP="$STAGING/db/migrations/0011_live_safety.up.sql"
MIGRATION_DOWN="$STAGING/db/migrations/0011_live_safety.down.sql"
MIGRATION_MAINTENANCE_FENCE_UP="$STAGING/db/migrations/0012_control_plane_maintenance_fence.up.sql"
MIGRATION_MAINTENANCE_FENCE_DOWN="$STAGING/db/migrations/0012_control_plane_maintenance_fence.down.sql"
MIGRATION_FOUR_ACCOUNT_ROLLOUT_UP="$STAGING/db/migrations/0013_four_account_rollout.up.sql"
MIGRATION_FOUR_ACCOUNT_ROLLOUT_DOWN="$STAGING/db/migrations/0013_four_account_rollout.down.sql"
MIGRATION_CANCEL_ORDER_CONTRACT_UP="$STAGING/db/migrations/0014_cancel_order_contract.up.sql"
MIGRATION_CANCEL_ORDER_CONTRACT_DOWN="$STAGING/db/migrations/0014_cancel_order_contract.down.sql"
MIGRATION_REFRESH_EVIDENCE_COMMAND_UP="$STAGING/db/migrations/0015_refresh_evidence_command.up.sql"
MIGRATION_REFRESH_EVIDENCE_COMMAND_DOWN="$STAGING/db/migrations/0015_refresh_evidence_command.down.sql"
MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_UP="$STAGING/db/migrations/0016_control_plane_lock_privileges.up.sql"
MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_DOWN="$STAGING/db/migrations/0016_control_plane_lock_privileges.down.sql"
MIGRATION_OPERATOR_QUERY_PROJECTION_READS_UP="$STAGING/db/migrations/0017_operator_query_projection_reads.up.sql"
MIGRATION_OPERATOR_QUERY_PROJECTION_READS_DOWN="$STAGING/db/migrations/0017_operator_query_projection_reads.down.sql"
MIGRATION_PROJECTION_RELIABILITY_UP="$STAGING/db/migrations/0018_projection_reliability.up.sql"
MIGRATION_PROJECTION_RELIABILITY_DOWN="$STAGING/db/migrations/0018_projection_reliability.down.sql"
JP24_REDIS_DEAD_INSTANCE_JANITOR="$STAGING/jp24_redis_dead_instance_janitor.py"
CONTROL_PLANE_ISOLATION_SCRIPT="$STAGING/hk-control-plane-isolation.sh"
CONTROL_PLANE_ISOLATION_MODE="${CONTROL_PLANE_ISOLATION_MODE:-require}"
LEGACY_CONTROL_PLANE_UNIT="${LEGACY_CONTROL_PLANE_UNIT:-trader-v3-controlplane.service}"
REDIS_COLD_BACKUP_MANIFEST="${REDIS_COLD_BACKUP_MANIFEST:-$T/redis-rebaseline/current/cold-backup-manifest.json}"
REDIS_CAPACITY_EVIDENCE="${REDIS_CAPACITY_EVIDENCE:-$T/redis-rebaseline/current/capacity-evidence.json}"
REDIS_CAPACITY_REFRESH="${REDIS_CAPACITY_REFRESH:-$T/redis-rebaseline/current/capacity-refresh.json}"
REDIS_CAPACITY_REFRESH_TOOL="$STAGING/refresh_redis_capacity_evidence.py"
REDIS_CAPACITY_VALIDITY_SECONDS=$((24 * 60 * 60))
DEPLOY_PIPELINE_WORST_CASE_SECONDS="${DEPLOY_PIPELINE_WORST_CASE_SECONDS:-46800}"
DEPLOY_MIN_FREE_BYTES="${DEPLOY_MIN_FREE_BYTES:-$((8 * 1024 * 1024 * 1024))}"
IMMUTABLE_BUILD_ATTESTATION="$STAGING/immutable-build-attestation.json"
REVIEWER_TRUST_PROOF="$STAGING/reviewer-trust-proof.json"
if [ -f "$REVIEWER_TRUST_PROOF" ] && [ ! -L "$REVIEWER_TRUST_PROOF" ]; then
  TRADER_RELEASE_REVIEWER_TRUST_SHA256="$(
    sha256sum "$REVIEWER_TRUST_PROOF" | awk '{print $1}'
  )"
  export TRADER_RELEASE_REVIEWER_TRUST_SHA256
fi
ACCOUNT_B_EVIDENCE_REFRESHER="${ACCOUNT_B_EVIDENCE_REFRESHER:-$T/account-a-canary/bin/refresh-account-b-evidence}"
ACCOUNT_B_EVIDENCE_REFRESH_TIMEOUT_SECONDS="${ACCOUNT_B_EVIDENCE_REFRESH_TIMEOUT_SECONDS:-900}"
REDIS_FENCING_EPOCH_KEY="trader-bot:redis-fencing-epoch"
REDIS_NAMESPACE_JANITOR="$STAGING/redis_namespace_janitor.py"
REDIS_NAMESPACE_REGISTRY="$STAGING/redis_namespace_registry.py"
HERMES_V3_TRADER_ROOT="${HERMES_V3_TRADER_ROOT:-/srv/hermes/profiles/trader/skills/trading/v3-trader}"
HERMES_FEEDER_TGT="$T/scripts/hermes_signal_feeder.py"
HERMES_V3_TRADE_TGT="$HERMES_V3_TRADER_ROOT/scripts/v3_trade.py"
HERMES_V3_SKILL_TGT="$HERMES_V3_TRADER_ROOT/SKILL.md"
WATCHER_ROOT="${WATCHER_ROOT:-/srv/trader}"
WATCHER_SOURCE_ROOT="$WATCHER_ROOT/services/telegram-watcher"
WATCHER_COMPOSE_FILE="${WATCHER_COMPOSE_FILE:-$WATCHER_ROOT/docker-compose.yml}"
WATCHER_COMPOSE_PROJECT="${WATCHER_COMPOSE_PROJECT:-trader}"
WATCHER_COMPOSE_SERVICE="${WATCHER_COMPOSE_SERVICE:-watcher}"
WATCHER_CONTAINER="${WATCHER_CONTAINER:-trader-watcher-1}"
WATCHER_HEALTH_URL="${WATCHER_HEALTH_URL:-http://127.0.0.1:9090/api/status}"
EXCHANGE_STATE_RECORDER_UNIT="${EXCHANGE_STATE_RECORDER_UNIT:-trader-v3-exchange-state}"
NODE_CONFIG_A="$T/node-a.hk.json"
NODE_CONFIG_B="$T/node-b.hk.json"
NODE_CONFIG_C="$T/node-c.hk.json"
NODE_CONFIG_D="$T/node-d.hk.json"
LEGACY_RISK_CAPTURE="$BACKUP_ROOT/live-risk-capture.json"
CONFIG_ARTIFACT_ROOT="$BACKUP_ROOT/node-config-artifacts"
CONFIG_RECORD_A="$BACKUP_ROOT/account-a-config-artifact.json"
CONFIG_RECORD_B="$BACKUP_ROOT/account-b-config-artifact.json"
CONFIG_RECORD_C="$BACKUP_ROOT/account-c-config-artifact.json"
CONFIG_RECORD_D="$BACKUP_ROOT/account-d-config-artifact.json"
RELEASE_CONFIG_ARTIFACT_ROOT="${RELEASE_CONFIG_ARTIFACT_ROOT:-}"
PINNED_RELEASE_MANIFEST="${PINNED_RELEASE_MANIFEST:-}"
DELIVERY_MODE="${DELIVERY_MODE:-immutable_image}"
EMERGENCY_ROLLBACK="${EMERGENCY_ROLLBACK:-0}"
EMERGENCY_ROLLBACK_REASON="${EMERGENCY_ROLLBACK_REASON:-}"
SKIP_RESUME="${SKIP_RESUME:-1}"
ROLLOUT_NODE="${ROLLOUT_NODE:-trader-v3-node-a}"
ACCOUNT_A_RELEASE_MANIFEST="${ACCOUNT_A_RELEASE_MANIFEST:-}"
ROLLOUT_REVIEWED_BY="${ROLLOUT_REVIEWED_BY:-codex-account-stall-hardening}"
ROLLOUT_ACTOR="${ROLLOUT_ACTOR:-codex-account-stall-hardening}"
ALL_NODES=(
  trader-v3-node-a
  trader-v3-node-b
  trader-v3-node-c
  trader-v3-node-d
)
ROLE_CONTROL_PLANE_UNITS=(
  trader-v3-controlplane-node-control.service
  trader-v3-controlplane-event-ingest.service
  trader-v3-controlplane-operator-query.service
)
CONTROL_PLANE_ROLE_ENV_FILES=(
  "${CONTROL_PLANE_NODE_CONTROL_ENV_FILE:-$T/secrets/control-plane/node-control.env}"
  "${CONTROL_PLANE_EVENT_INGEST_ENV_FILE:-$T/secrets/control-plane/event-ingest.env}"
  "${CONTROL_PLANE_OPERATOR_QUERY_ENV_FILE:-$T/secrets/control-plane/operator-query.env}"
)
HERMES_REQUIRED_UNITS=(
  trader-v3-hermes-feeder
  hermes-gateway-trader
)
OPERATOR_ACCOUNT_REGISTRY_JSON='{"account-a":{},"account-b":{},"account-c":{},"account-d":{}}'
V3_OPERATOR_ACCOUNTS="account-a,account-b,account-c,account-d"
BINANCE_EGRESS_MODE=""
BINANCE_PROXY_URL=""
BINANCE_PROXY_URL_A=""
BINANCE_PROXY_URL_B=""
BINANCE_PROXY_URL_C=""
BINANCE_PROXY_URL_D=""
BINANCE_EXPECTED_EGRESS_IP=""
BINANCE_EXPECTED_EGRESS_IP_A=""
BINANCE_EXPECTED_EGRESS_IP_B=""
BINANCE_EXPECTED_EGRESS_IP_C=""
BINANCE_EXPECTED_EGRESS_IP_D=""
BINANCE_ROUTE_INTERFACE="wg0"
BINANCE_ACCOUNT_NETWORKS=(
  trader-v3-account-a
  trader-v3-account-b
  trader-v3-account-c
  trader-v3-account-d
)
CONTROL_PLANE_UNITS=()
TEMP_FILES=()
DEPLOY_WARNINGS_LOG="$BACKUP_ROOT/deploy-gate-warnings.log"
BACKUP_CAPTURED=0
FILES_INSTALLED=0
HOST_RUNTIME_INSTALL_COMPLETE=0
CONTROL_PLANE_RESTARTED=0
HERMES_RESTART_REQUIRED=0
HERMES_RESTARTED=0
WATCHER_RESTART_REQUIRED=0
WATCHER_RESTARTED=0
WATCHER_SCHEMA_RESTART_REQUIRED=0
EXCHANGE_STATE_RESTART_REQUIRED=0
EXCHANGE_STATE_RESTARTED=0
CONTROL_PLANE_ISOLATION_REQUIRED=0
CONTROL_PLANE_ISOLATION_ACTIVATED=0
CONTROL_PLANE_TOPOLOGY=""
CONTROL_PLANE_ROLE_BOOTSTRAP_REQUIRED=0
ROLLOUT_RECREATE_STARTED=0
ROLLOUT_TRACKED=0
ROLLOUT_FINALIZED=0
DOWNTIME_WINDOW_ENTERED=0
BOOTSTRAP_ALL_NODE_RELEASE=0
BOOTSTRAP_REGISTRATION_COMPLETED=0
PRESERVE_ROLLOUT_FOR_RETRY=0
DEPLOY_GATE_MODE="maintenance_fence"
REQUIRED_DEPLOY_GATE_MODE="${REQUIRED_DEPLOY_GATE_MODE:-}"
RECREATE_NODES=()
PHASE_ONLY_ROLLOUT=0
ROLLBACK_IN_PROGRESS=0
POST_MIGRATION_RECOVERY_REQUIRED=0
POST_MIGRATION_RECOVERY_VERIFIED=0
POSTGRES_PRE_MIGRATION_DUMP="$BACKUP_ROOT/postgres-pre-migration.dump"
POSTGRES_PRE_MIGRATION_RESTORE_LIST="$BACKUP_ROOT/postgres-pre-migration.dump.list"
POSTGRES_PRE_MIGRATION_SHA256="$BACKUP_ROOT/postgres-pre-migration.dump.sha256"
PRE_MIGRATION_BACKUP_EXPECTATION="$BACKUP_ROOT/pre-migration-backup-expectation.json"
# The recovery manifest is validated as a strict release envelope with
# its own directory as the payload root, so the durable copy must live
# inside a full copy of the reviewed release payload.
POST_MIGRATION_RECOVERY_PAYLOAD_ROOT="$BACKUP_ROOT/release-payload"
POST_MIGRATION_RECOVERY_MANIFEST="$POST_MIGRATION_RECOVERY_PAYLOAD_ROOT/release-manifest.json"
POST_MIGRATION_RECOVERY_ROOT="$BACKUP_ROOT/post-migration-recovery"
POST_MIGRATION_RECOVERY_BIN="$BACKUP_ROOT/post-migration-recovery-bin"
POST_MIGRATION_RECOVERY_DOCKER="$POST_MIGRATION_RECOVERY_BIN/docker"
WATCHER_RUNTIME_CHANGED_LIST="$BACKUP_ROOT/watcher-runtime-changed.tsv"
BOOTSTRAP_STOPPED_GATE_LOG="$BACKUP_ROOT/bootstrap-stopped-gates.jsonl"
BOOTSTRAP_REDIS_FENCING_EPOCH=""
BOOTSTRAP_RECOVERY_BLOCKED_EVIDENCE="$BACKUP_ROOT/bootstrap-recovery-blocked.json"
PARTIAL_INSTALL_RECOVERY_EVIDENCE="$BACKUP_ROOT/partial-install-recovery.json"
LEGACY_RECREATE_BOOTSTRAP_EVIDENCE="$BACKUP_ROOT/legacy-recreate-bootstrap.json"
LEGACY_RECREATE_GENERATED_LIST="$BACKUP_ROOT/legacy-recreate-generated.tsv"
LEGACY_RECREATE_RECORDS="$BACKUP_ROOT/legacy-recreate-records.tsv"
LEGACY_RECREATE_SNAPSHOT_ROOT="$BACKUP_ROOT/legacy-recreate-snapshots"
MIGRATION_COMMIT_MARKER="$BACKUP_ROOT/0018-migration-committed.json"
MAINTENANCE_FENCE_STATE="$BACKUP_ROOT/maintenance-fence.json"
MAINTENANCE_FENCE_ID=""
MAINTENANCE_FENCE_ACQUIRED=0
ACCOUNT_B_REVIEWER_PUBLIC_KEY_SHA256="2b149fe2d7357dfea74441a1f6d6f1dd9ff6ea6a1a800fd5f2f7c2778339f928"
LEGACY_RECREATE_BOOTSTRAP_PREPARED=0

verify_account_stall_operation_lock() {
  python3 - \
    "$ACCOUNT_STALL_OPERATION_LOCK" \
    "$ACCOUNT_STALL_OPERATION_LOCK_UID" \
    "$ACCOUNT_STALL_OPERATION_LOCK_GID" \
    "$ACCOUNT_STALL_OPERATION_LOCK_TOKEN" <<'PY'
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
  local lock_gid="${ACCOUNT_STALL_OPERATION_LOCK_GID:-$(id -g)}"
  local lock_uid="${ACCOUNT_STALL_OPERATION_LOCK_UID:-$(id -u)}"
  local ownership_token
  if ! command -v flock >/dev/null 2>&1; then
    echo "FATAL: flock is required for account-stall operations" >&2
    return 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "FATAL: python3 is required for account-stall operations" >&2
    return 1
  fi
  if [ -z "$ACCOUNT_STALL_OPERATION_LOCK" ]; then
    echo "FATAL: account-stall operation lock path is empty" >&2
    return 1
  fi
  case "$ACCOUNT_STALL_OPERATION_LOCK" in
    /*)
      ;;
    *)
      echo "FATAL: account-stall operation lock path must be absolute" >&2
      return 1
      ;;
  esac
  if [ -L "$ACCOUNT_STALL_OPERATION_LOCK" ]; then
    echo "FATAL: account-stall operation lock cannot be a symlink" >&2
    return 1
  fi
  umask 077
  exec 9<>"$ACCOUNT_STALL_OPERATION_LOCK"
  if [ ! -f "$ACCOUNT_STALL_OPERATION_LOCK" ]; then
    echo "FATAL: account-stall operation lock must be a regular file" >&2
    return 1
  fi
  if ! flock -n 9; then
    echo "FATAL: another account-stall operation holds $ACCOUNT_STALL_OPERATION_LOCK" >&2
    return 1
  fi
  ownership_token="$(
    python3 -c 'import secrets; print(secrets.token_hex(32))'
  )"
  if [[ ! "$ownership_token" =~ ^[0-9a-f]{64}$ ]]; then
    echo "FATAL: account-stall operation ownership token is invalid" >&2
    return 1
  fi
  : > "$ACCOUNT_STALL_OPERATION_LOCK"
  printf '%s\n' "$ownership_token" >&9
  python3 - \
    "$lock_uid" \
    "$lock_gid" <<'PY'
import os
import sys

os.fchmod(9, 0o600)
os.fchown(9, int(sys.argv[1]), int(sys.argv[2]))
os.fsync(9)
PY
  ACCOUNT_STALL_OPERATION_LOCK_UID="$lock_uid"
  ACCOUNT_STALL_OPERATION_LOCK_GID="$lock_gid"
  ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP="inherited"
  ACCOUNT_STALL_OPERATION_LOCK_FD="9"
  ACCOUNT_STALL_OPERATION_LOCK_TOKEN="$ownership_token"
  export \
    ACCOUNT_STALL_OPERATION_LOCK \
    ACCOUNT_STALL_OPERATION_LOCK_UID \
    ACCOUNT_STALL_OPERATION_LOCK_GID \
    ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP \
    ACCOUNT_STALL_OPERATION_LOCK_FD \
    ACCOUNT_STALL_OPERATION_LOCK_TOKEN
  if declare -F verify_account_stall_operation_lock >/dev/null; then
    verify_account_stall_operation_lock
  fi
}
acquire_account_stall_operation_lock
case "$ROLLOUT_NODE" in
  trader-v3-node-a)
    ROLLOUT_ACCOUNT="account-a"
    ROLLOUT_PORT=8081
    ;;
  trader-v3-node-b)
    ROLLOUT_ACCOUNT="account-b"
    ROLLOUT_PORT=8082
    ;;
  trader-v3-node-c)
    ROLLOUT_ACCOUNT="account-c"
    ROLLOUT_PORT=8083
    ;;
  trader-v3-node-d)
    ROLLOUT_ACCOUNT="account-d"
    ROLLOUT_PORT=8084
    ;;
  *)
    echo "FATAL: ROLLOUT_NODE must select account-a through account-d" >&2
    exit 1
    ;;
esac
[ "$SKIP_RESUME" = "1" ] || {
  echo "FATAL: automatic RESUME is disabled; keep SKIP_RESUME=1" >&2
  exit 1
}
case "$CONTROL_PLANE_ISOLATION_MODE" in
  require|disable)
    ;;
  *)
    echo "FATAL: invalid CONTROL_PLANE_ISOLATION_MODE: $CONTROL_PLANE_ISOLATION_MODE" >&2
    exit 1
    ;;
esac
case "$EMERGENCY_ROLLBACK" in
  0)
    [ "$DELIVERY_MODE" = "immutable_image" ] || {
      echo "FATAL: hardening rollout requires DELIVERY_MODE=immutable_image" >&2
      exit 1
    }
    [ "$CONTROL_PLANE_ISOLATION_MODE" = "require" ] || {
      echo "FATAL: hardening rollout requires CONTROL_PLANE_ISOLATION_MODE=require" >&2
      exit 1
    }
    ;;
  1)
    [ "$DELIVERY_MODE" = "transition_bind_mount" ] || {
      echo "FATAL: emergency rollback requires DELIVERY_MODE=transition_bind_mount" >&2
      exit 1
    }
    [ -n "${EMERGENCY_ROLLBACK_REASON//[[:space:]]/}" ] || {
      echo "FATAL: emergency rollback requires EMERGENCY_ROLLBACK_REASON" >&2
      exit 1
    }
    ;;
  *)
    echo "FATAL: EMERGENCY_ROLLBACK must be 0 or 1" >&2
    exit 1
    ;;
esac

die() {
  echo "FATAL: $*" >&2
  return 1
}
verify_four_channel_account_mapping() {
  local evidence_path="${1:-}"
  local schema_mode="${2:-strict}"
  python3 - "$WATCHER_TRADING_DB" "$evidence_path" "$schema_mode" <<'PY'
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sys


EXPECTED_ROUTES = {
    "-1002136478186": {
        "credential_account_id": "jiataotx@gmail.com",
        "execution_account_id": "account-a",
    },
    "-1002198013097": {
        "credential_account_id": "balenwong3@gmail.com",
        "execution_account_id": "account-b",
    },
    "-1002189417451": {
        "credential_account_id": "泰山",
        "execution_account_id": "account-c",
    },
    "-1002193304023": {
        "credential_account_id": "黄山",
        "execution_account_id": "account-d",
    },
}
EXPECTED_EXECUTION_ACCOUNTS = {
    "account-a",
    "account-b",
    "account-c",
    "account-d",
}
PRE_RESTART_REQUIRED_COLUMNS = {
    "account_configs": {
        "account_id",
        "execution_account_id",
        "risk_capital_multiplier",
        "is_testnet",
    },
    "channel_routing": {
        "channel_id",
        "target_account_id",
    },
}


def fail(message: str) -> None:
    raise SystemExit(f"watcher four-channel mapping gate failed: {message}")


def file_metadata(
    path: Path,
    *,
    required: bool,
) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            fail(f"SQLite database is unavailable: {path}")
        return {
            "path": str(path),
            "present": False,
        }
    except OSError as exc:
        fail(f"SQLite database is unavailable: {exc}")
    if not stat.S_ISREG(metadata.st_mode):
        fail(f"SQLite file must be a regular file: {path}")
    return {
        "path": str(path),
        "present": True,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size_bytes": metadata.st_size,
        "modified_at_ns": metadata.st_mtime_ns,
    }


def capture_sqlite_files(path: Path) -> dict[str, dict[str, object]]:
    return {
        "database": file_metadata(path, required=True),
        "wal": file_metadata(Path(f"{path}-wal"), required=False),
        "shm": file_metadata(Path(f"{path}-shm"), required=False),
    }


def validate_database_identity(
    before: dict[str, dict[str, object]],
    after: dict[str, dict[str, object]],
) -> None:
    before_database = before["database"]
    after_database = after["database"]
    before_identity = (
        before_database["device"],
        before_database["inode"],
    )
    after_identity = (
        after_database["device"],
        after_database["inode"],
    )
    if before_identity != after_identity:
        fail("SQLite database identity changed during mapping gate")


def validate_schema(
    connection: sqlite3.Connection,
    *,
    require_explicit_enabled: bool,
) -> dict[str, set[str]]:
    rows = connection.execute(
        "SELECT name, type FROM sqlite_schema "
        "WHERE name IN ('account_configs', 'channel_routing')"
    ).fetchall()
    objects = {str(row["name"]): str(row["type"]) for row in rows}
    table_columns: dict[str, set[str]] = {}
    for table_name, required_columns in PRE_RESTART_REQUIRED_COLUMNS.items():
        if objects.get(table_name) != "table":
            fail(f"required table is missing: {table_name}")
        columns = {
            str(row["name"])
            for row in connection.execute(
                f"PRAGMA table_info({table_name})"
            ).fetchall()
        }
        table_columns[table_name] = columns
        missing = sorted(required_columns - columns)
        if missing:
            fail(
                f"{table_name} lacks required columns: {','.join(missing)}"
            )
    account_columns = table_columns["account_configs"]
    if require_explicit_enabled and "is_enabled" not in account_columns:
        fail("account_configs lacks required columns: is_enabled")
    return table_columns


def load_mapping(
    connection: sqlite3.Connection,
    *,
    account_columns: set[str],
) -> list[dict[str, object]]:
    channel_ids = sorted(EXPECTED_ROUTES)
    placeholders = ",".join("?" for _ in channel_ids)
    enabled_field = "1 AS is_enabled"
    if "is_enabled" in account_columns:
        enabled_field = "account.is_enabled AS is_enabled"
    rows = connection.execute(
        "SELECT "
        "route.channel_id AS channel_id, "
        "route.target_account_id AS credential_account_id, "
        "account.execution_account_id AS execution_account_id, "
        "account.risk_capital_multiplier AS risk_capital_multiplier, "
        f"{enabled_field}, "
        "account.is_testnet AS is_testnet "
        "FROM channel_routing AS route "
        "LEFT JOIN account_configs AS account "
        "ON account.account_id = route.target_account_id "
        f"WHERE route.channel_id IN ({placeholders}) "
        "ORDER BY route.channel_id",
        channel_ids,
    ).fetchall()
    actual_channels = {str(row["channel_id"]) for row in rows}
    missing_channels = sorted(set(channel_ids) - actual_channels)
    if missing_channels:
        fail(
            "required channel routes are missing: "
            + ",".join(missing_channels)
        )
    if len(rows) != len(channel_ids):
        fail("required channel route cardinality is invalid")

    mappings: list[dict[str, object]] = []
    for row in rows:
        channel_id = str(row["channel_id"])
        credential_account_id = row["credential_account_id"]
        execution_account_id = row["execution_account_id"]
        if credential_account_id is None or execution_account_id is None:
            fail(f"channel route has no account config: {channel_id}")
        mappings.append(
            {
                "channel_id": channel_id,
                "credential_account_id": str(credential_account_id),
                "execution_account_id": str(execution_account_id),
                "risk_capital_multiplier": row["risk_capital_multiplier"],
                "is_enabled": row["is_enabled"],
                "is_testnet": row["is_testnet"],
            }
        )
    return mappings


def validate_mapping(mappings: list[dict[str, object]]) -> None:
    execution_counts = Counter(
        str(mapping["execution_account_id"]) for mapping in mappings
    )
    if set(execution_counts) != EXPECTED_EXECUTION_ACCOUNTS:
        fail(
            "execution accounts must cover account-a through account-d "
            "exactly once"
        )
    if any(count != 1 for count in execution_counts.values()):
        fail(
            "execution accounts must cover account-a through account-d "
            "exactly once"
        )

    for mapping in mappings:
        channel_id = str(mapping["channel_id"])
        expected = EXPECTED_ROUTES[channel_id]
        credential_account_id = str(mapping["credential_account_id"])
        execution_account_id = str(mapping["execution_account_id"])
        if credential_account_id != expected["credential_account_id"]:
            fail(f"credential account mapping drifted for {channel_id}")
        if execution_account_id != expected["execution_account_id"]:
            fail(f"execution account mapping drifted for {channel_id}")
        if mapping["is_enabled"] != 1:
            fail(f"mapped account is disabled for {channel_id}")
        if mapping["is_testnet"] != 0:
            fail(f"mapped account is not live for {channel_id}")
        try:
            multiplier = float(mapping["risk_capital_multiplier"])
        except (TypeError, ValueError):
            fail(f"risk capital multiplier is invalid for {channel_id}")
        if not math.isfinite(multiplier) or multiplier <= 0:
            fail(f"risk capital multiplier is invalid for {channel_id}")
        mapping["risk_capital_multiplier"] = multiplier
        mapping["is_enabled"] = 1
        mapping["is_testnet"] = 0


def write_evidence(
    output_path: Path,
    payload: dict[str, object],
) -> None:
    if output_path.exists() or output_path.is_symlink():
        fail(f"evidence path already exists: {output_path}")
    parent = output_path.parent
    if not parent.is_dir():
        fail(f"evidence directory is unavailable: {parent}")
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    temporary_path = parent / f".{output_path.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor = os.open(temporary_path, flags, 0o400)
    try:
        os.fchmod(file_descriptor, 0o400)
        with os.fdopen(file_descriptor, "wb", closefd=False) as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(file_descriptor)
    os.replace(temporary_path, output_path)
    os.chmod(output_path, 0o400)
    directory_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


database_path = Path(sys.argv[1])
evidence_argument = sys.argv[2]
schema_mode = sys.argv[3]
if schema_mode not in {"pre_restart", "strict"}:
    fail(f"schema mode is invalid: {schema_mode}")
require_explicit_enabled = schema_mode == "strict"
sqlite_files_before = capture_sqlite_files(database_path)
database_uri = database_path.resolve(strict=True).as_uri() + "?mode=ro"
try:
    connection = sqlite3.connect(
        database_uri,
        uri=True,
        timeout=5,
    )
except sqlite3.Error as exc:
    fail(f"SQLite database could not be opened read-only: {exc}")
connection.row_factory = sqlite3.Row
data_version_before = 0
data_version_after = 0
journal_mode = ""
try:
    connection.execute("PRAGMA query_only = ON")
    connection.execute("BEGIN")
    journal_mode = str(
        connection.execute("PRAGMA journal_mode").fetchone()[0]
    )
    data_version_before = int(
        connection.execute("PRAGMA data_version").fetchone()[0]
    )
    quick_check = [
        str(row[0]) for row in connection.execute("PRAGMA quick_check")
    ]
    if quick_check != ["ok"]:
        fail("SQLite PRAGMA quick_check failed")
    table_columns = validate_schema(
        connection,
        require_explicit_enabled=require_explicit_enabled,
    )
    account_columns = table_columns["account_configs"]
    mappings = load_mapping(
        connection,
        account_columns=account_columns,
    )
    validate_mapping(mappings)
    data_version_after = int(
        connection.execute("PRAGMA data_version").fetchone()[0]
    )
finally:
    connection.close()
sqlite_files_after = capture_sqlite_files(database_path)
validate_database_identity(sqlite_files_before, sqlite_files_after)

if evidence_argument:
    evidence_path = Path(evidence_argument)
    enabled_column_mode = "explicit"
    if "is_enabled" not in account_columns:
        enabled_column_mode = "legacy_implicit_enabled"
    evidence = {
        "schema_version": "trader-v3-four-channel-account-mapping/v2",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "schema_mode": schema_mode,
        "pre_restart_contract": (
            "dynamic-routing-columns-present/enabled-column-optional-v1"
        ),
        "enabled_column_mode": enabled_column_mode,
        "database": {
            "path": str(database_path),
            "quick_check": "ok",
            "journal_mode": journal_mode,
            "data_version_before": data_version_before,
            "data_version_after": data_version_after,
            "files_before_read": sqlite_files_before,
            "files_after_read": sqlite_files_after,
        },
        "mapping": mappings,
    }
    write_evidence(evidence_path, evidence)
PY
}
# FOUR_CHANNEL_MAPPING_GATE_END

detect_watcher_schema_restart_requirement() {
  python3 - "$WATCHER_TRADING_DB" <<'PY'
from pathlib import Path
import sqlite3
import sys

database_path = Path(sys.argv[1])
database_uri = database_path.resolve(strict=True).as_uri() + "?mode=ro"
connection = sqlite3.connect(database_uri, uri=True, timeout=5)
try:
    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(account_configs)"
        ).fetchall()
    }
finally:
    connection.close()
print("0" if "is_enabled" in columns else "1")
PY
}

refresh_backup_checksums() {
  (
    cd "$BACKUP_ROOT"
    find . -type f \
      ! -name SHA256SUMS \
      ! -name SHA256SUMS.new \
      -print0 \
      | sort -z \
      | xargs -0 sha256sum
  ) > "$BACKUP_ROOT/SHA256SUMS.new"
  mv "$BACKUP_ROOT/SHA256SUMS.new" "$BACKUP_ROOT/SHA256SUMS"
}

require_staging_artifact() {
  local path="$1"
  local label="$2"
  [ -f "$path" ] || die "$label missing in staging"
}
require_checksum_artifact() {
  local relative="$1"
  awk -v required="$relative" '
    {
      path = $2
      sub(/^\*/, "", path)
      sub(/^\.\//, "", path)
      if (path == required) {
        found = 1
      }
    }
    END {
      exit found ? 0 : 1
    }
  ' "$STAGING/SHA256SUMS" \
    || die "SHA256SUMS does not cover required artifact: $relative"
}
normalize_live_trade_release_metadata() {
  python3 - \
    "$LIVE_TRADE_HTTP_ADAPTER" \
    "$RELEASE_SOURCE_MANIFEST" <<'PY'
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


artifacts = (
    (Path(sys.argv[1]), 0o500, "live trade HTTP adapter"),
    (Path(sys.argv[2]), 0o400, "release source manifest"),
)
if os.geteuid() != 0:
    raise SystemExit("live trade release metadata normalization requires root")
for path, mode, label in artifacts:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SystemExit(f"{label} must be a regular file")
        if metadata.st_nlink != 1:
            raise SystemExit(f"{label} link count must equal one")
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, mode)
        normalized = os.fstat(descriptor)
        if normalized.st_uid != 0 or normalized.st_gid != 0:
            raise SystemExit(f"{label} owner normalization failed")
        if stat.S_IMODE(normalized.st_mode) != mode:
            raise SystemExit(f"{label} mode normalization failed")
    finally:
        os.close(descriptor)
PY
}
validate_watcher_runtime_payload() {
  require_staging_artifact \
    "$IMMUTABLE_WATCHER_BUILDER" \
    "build_immutable_watcher_image.py"
  require_staging_artifact \
    "$WATCHER_RUNTIME_MANIFEST" \
    "watcher-runtime-manifest.json"
  require_checksum_artifact "build_immutable_watcher_image.py"
  require_checksum_artifact "watcher-runtime-manifest.json"
  python3 "$IMMUTABLE_WATCHER_BUILDER" \
    --validate-runtime-manifest "$WATCHER_RUNTIME_MANIFEST"
  python3 - "$WATCHER_RUNTIME_MANIFEST" "$STAGING/SHA256SUMS" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
checksum_path = Path(sys.argv[2])
covered = set()
for raw_line in checksum_path.read_text(encoding="utf-8").splitlines():
    _digest, separator, raw_path = raw_line.partition("  ")
    if not separator:
        continue
    path = raw_path
    if path.startswith("*"):
        path = path[1:]
    if path.startswith("./"):
        path = path[2:]
    covered.add(path)
files = manifest.get("files")
if not isinstance(files, list):
    raise SystemExit("watcher runtime manifest files must be a list")
required = {"watcher-runtime-manifest.json"}
for item in files:
    if not isinstance(item, dict):
        raise SystemExit("watcher runtime manifest file entry is invalid")
    release_path = str(item.get("release_path") or "")
    if not release_path:
        raise SystemExit("watcher runtime manifest file lacks release_path")
    required.add(release_path)
missing = sorted(required - covered)
if missing:
    raise SystemExit(
        "SHA256SUMS lacks watcher runtime payload entries: "
        + ", ".join(missing)
    )
PY
}
watcher_runtime_change_count() {
  python3 - "$WATCHER_RUNTIME_MANIFEST" "$WATCHER_SOURCE_ROOT" <<'PY'
from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
watcher_root = Path(sys.argv[2])
if not watcher_root.is_dir() or watcher_root.is_symlink():
    raise SystemExit(f"watcher root is invalid: {watcher_root}")
resolved_root = watcher_root.resolve()
changed = 0
for item in manifest["files"]:
    release_path = str(item["release_path"])
    if not release_path.startswith("watcher/"):
        raise SystemExit(f"watcher release path is invalid: {release_path}")
    relative = release_path[len("watcher/") :]
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise SystemExit(f"watcher target path is invalid: {relative}")
    target = watcher_root / relative_path
    try:
        target.resolve().relative_to(resolved_root)
    except ValueError as exc:
        raise SystemExit(
            f"watcher runtime target escapes root: {target}"
        ) from exc
    if target.is_symlink():
        raise SystemExit(f"watcher runtime target cannot be a symlink: {target}")
    if not target.exists():
        changed += 1
        continue
    metadata = target.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"watcher runtime target must be regular: {target}")
    if sha256_file(target) != str(item["sha256"]):
        changed += 1
print(changed)
PY
}
capture_watcher_runtime_backup() {
  local changed_list="$1"
  python3 - \
    "$WATCHER_RUNTIME_MANIFEST" \
    "$WATCHER_SOURCE_ROOT" \
    "$BACKUP_ROOT" \
    "$changed_list" <<'PY'
from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import sys
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
watcher_root = Path(sys.argv[2])
backup_root = Path(sys.argv[3])
changed_list = Path(sys.argv[4])
resolved_root = watcher_root.resolve()
files_root = backup_root / "files"
index_path = backup_root / "index.tsv"
new_files_path = backup_root / "new-files.txt"
changed_entries = []
for item in manifest["files"]:
    release_path = str(item["release_path"])
    relative = release_path[len("watcher/") :]
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise SystemExit(f"watcher target path is invalid: {relative}")
    target = watcher_root / relative_path
    try:
        target.resolve().relative_to(resolved_root)
    except ValueError as exc:
        raise SystemExit(
            f"watcher runtime target escapes root: {target}"
        ) from exc
    if target.is_symlink():
        raise SystemExit(f"watcher runtime target cannot be a symlink: {target}")
    digest = str(item["sha256"])
    if target.exists():
        metadata = target.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise SystemExit(
                f"watcher runtime target must be regular: {target}"
            )
        if sha256_file(target) == digest:
            continue
        label_source = hashlib.sha256(relative.encode("utf-8")).hexdigest()
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", relative)
        label = f"watcher__{label_source[:12]}__{safe_name}"
        shutil.copy2(target, files_root / label)
        with index_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{label}\t{target}\n")
    else:
        with new_files_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{target}\n")
    changed_entries.append(f"{release_path}\t{target}\n")
changed_list.write_text("".join(changed_entries), encoding="utf-8")
PY
}
install_watcher_runtime_atomically() {
  local changed_list="$1"
  python3 "$IMMUTABLE_WATCHER_BUILDER" \
    --validate-runtime-manifest "$WATCHER_RUNTIME_MANIFEST"
  python3 - \
    "$WATCHER_RUNTIME_MANIFEST" \
    "$WATCHER_SOURCE_ROOT" \
    "$changed_list" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def target_for_release_path(root: Path, release_path: str) -> Path:
    relative = release_path[len("watcher/") :]
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise SystemExit(f"watcher target path is invalid: {relative}")
    target = root / relative_path
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise SystemExit(
            f"watcher runtime target escapes root: {target}"
        ) from exc
    return target


manifest_path = Path(sys.argv[1])
watcher_root = Path(sys.argv[2])
changed_list = Path(sys.argv[3])
payload_root = manifest_path.parent
root_metadata = watcher_root.stat()
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
files = {
    str(item["release_path"]): item
    for item in manifest["files"]
}
changed = []
if changed_list.is_file():
    for raw_line in changed_list.read_text(encoding="utf-8").splitlines():
        release_path, separator, _target = raw_line.partition("\t")
        if separator:
            changed.append(release_path)
for release_path in changed:
    item = files.get(release_path)
    if item is None:
        raise SystemExit(
            f"watcher changed-list entry is not in manifest: {release_path}"
        )
    source = payload_root / release_path
    target = target_for_release_path(watcher_root, release_path)
    if target.is_symlink():
        raise SystemExit(f"watcher runtime target cannot be a symlink: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        metadata = target.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise SystemExit(
                f"watcher runtime target must be regular: {target}"
            )
        mode = stat.S_IMODE(metadata.st_mode)
        owner_uid = metadata.st_uid
        owner_gid = metadata.st_gid
    else:
        source_mode = stat.S_IMODE(source.stat().st_mode)
        mode = 0o755 if source_mode & 0o111 else 0o644
        owner_uid = root_metadata.st_uid
        owner_gid = root_metadata.st_gid
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f".{target.name}.deploy.",
        dir=target.parent,
    )
    temporary = Path(temporary_raw)
    try:
        with source.open("rb") as reader:
            with os.fdopen(descriptor, "wb") as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, owner_uid, owner_gid)
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
for release_path, item in files.items():
    target = target_for_release_path(watcher_root, release_path)
    if not target.is_file() or target.is_symlink():
        raise SystemExit(f"watcher runtime target missing: {target}")
    if sha256_file(target) != str(item["sha256"]):
        raise SystemExit(
            f"post-install watcher hash mismatch: {release_path}"
        )
PY
}

load_maintenance_fence_state() {
  MAINTENANCE_FENCE_ID="$(
    python3 - "$MAINTENANCE_FENCE_STATE" <<'PY'
import json
import sys
from uuid import UUID

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
fence_id = str(payload.get("fence_id") or "")
UUID(fence_id)
print(fence_id)
PY
  )" || die "maintenance fence state is invalid"
  [ -n "$MAINTENANCE_FENCE_ID" ] \
    || die "maintenance fence state lacks fence_id"
  MAINTENANCE_FENCE_ACQUIRED=1
  ACCOUNT_STALL_MAINTENANCE_FENCE_OWNERSHIP="inherited"
  ACCOUNT_STALL_MAINTENANCE_FENCE_ID="$MAINTENANCE_FENCE_ID"
  ACCOUNT_STALL_MAINTENANCE_FENCE_OWNER_TOKEN="$ACCOUNT_STALL_OPERATION_LOCK_TOKEN"
  ACCOUNT_STALL_MAINTENANCE_FENCE_EVIDENCE_SHA256="$(
    maintenance_fence_evidence_sha256
  )" || die "maintenance fence state evidence is invalid"
  if [ "${DOWNTIME_WINDOW_ENTERED:-0}" = "1" ]; then
    ACCOUNT_STALL_MAINTENANCE_FENCE_VERIFICATION_MODE="frozen"
  else
    ACCOUNT_STALL_MAINTENANCE_FENCE_VERIFICATION_MODE="online"
  fi
  export \
    ACCOUNT_STALL_MAINTENANCE_FENCE_OWNERSHIP \
    ACCOUNT_STALL_MAINTENANCE_FENCE_ID \
    ACCOUNT_STALL_MAINTENANCE_FENCE_OWNER_TOKEN \
    ACCOUNT_STALL_MAINTENANCE_FENCE_EVIDENCE_SHA256 \
    ACCOUNT_STALL_MAINTENANCE_FENCE_VERIFICATION_MODE
}

maintenance_fence_evidence_sha256() {
  python3 - "$MAINTENANCE_FENCE_STATE" <<'PY'
import hashlib
import json
import os
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
evidence = payload.get("account_evidence")
if not isinstance(evidence, list):
    raise SystemExit("maintenance fence state lacks account_evidence")
encoded = json.dumps(
    evidence,
    ensure_ascii=True,
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8")
canonical_sha256 = hashlib.sha256(encoded).hexdigest()
recorded_sha256 = str(
    payload.get("account_evidence_sha256") or ""
).strip()
if not recorded_sha256:
    payload["account_evidence_sha256"] = canonical_sha256
    temporary = path.with_name(f"{path.name}.canonical")
    encoded_payload = (
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o400,
    )
    try:
        offset = 0
        while offset < len(encoded_payload):
            offset += os.write(descriptor, encoded_payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
elif re.fullmatch(r"[0-9a-f]{64}", recorded_sha256) is None:
    raise SystemExit(
        "maintenance fence state has invalid canonical evidence hash"
    )
elif recorded_sha256 != canonical_sha256:
    raise SystemExit("maintenance fence state evidence hash mismatch")
if recorded_sha256 != canonical_sha256:
    recorded_sha256 = canonical_sha256
print(canonical_sha256)
PY
}

verify_online_maintenance_fence() {
  local stage="$1"
  local tmp_state="$MAINTENANCE_FENCE_STATE.new"
  verify_account_stall_operation_lock
  [ "$MAINTENANCE_FENCE_ACQUIRED" = "1" ] \
    || die "maintenance fence is not acquired"
  rm -f "$tmp_state"
  if ! timeout \
      --signal=TERM \
      --kill-after=5s \
      "${ACCOUNT_STALL_OPERATION_TIMEOUT_SECONDS}s" \
      "$T/.venv-cp/bin/python" \
      "$MIGRATION_RUNNER" \
      maintenance-fence \
      verify \
      --database-env-file "$T/.env.v3" \
      --fence-id "$MAINTENANCE_FENCE_ID" \
      --owner-token "$ACCOUNT_STALL_OPERATION_LOCK_TOKEN" \
      --stage "$stage" \
      --lease-seconds "$ACCOUNT_STALL_FENCE_LEASE_SECONDS" \
      --heartbeat-max-age-seconds \
        "$ACCOUNT_STALL_HEARTBEAT_MAX_AGE_SECONDS" \
      >"$tmp_state"; then
    rm -f "$tmp_state"
    return 1
  fi
  chmod 0400 "$tmp_state" || {
    rm -f "$tmp_state"
    return 1
  }
  mv "$tmp_state" "$MAINTENANCE_FENCE_STATE" || {
    rm -f "$tmp_state"
    return 1
  }
  load_maintenance_fence_state
}

verify_frozen_maintenance_fence() {
  local stage="$1"
  local evidence_sha256
  local tmp_state="$MAINTENANCE_FENCE_STATE.new"
  verify_account_stall_operation_lock
  [ "$MAINTENANCE_FENCE_ACQUIRED" = "1" ] \
    || die "maintenance fence is not acquired"
  evidence_sha256="$(maintenance_fence_evidence_sha256)" \
    || die "maintenance fence frozen evidence is invalid"
  if ! "$T/.venv-cp/bin/python" \
      "$MIGRATION_RUNNER" \
      maintenance-fence \
      verify-frozen \
      --help >/dev/null 2>&1; then
    echo "== maintenance fence frozen evidence retained sha256=$evidence_sha256"
    load_maintenance_fence_state
    return 0
  fi
  rm -f "$tmp_state"
  if ! timeout \
      --signal=TERM \
      --kill-after=5s \
      "${ACCOUNT_STALL_OPERATION_TIMEOUT_SECONDS}s" \
      "$T/.venv-cp/bin/python" \
      "$MIGRATION_RUNNER" \
      maintenance-fence \
      verify-frozen \
      --database-env-file "$T/.env.v3" \
      --fence-id "$MAINTENANCE_FENCE_ID" \
      --owner-token "$ACCOUNT_STALL_OPERATION_LOCK_TOKEN" \
      --stage "$stage" \
      --lease-seconds "$ACCOUNT_STALL_FENCE_LEASE_SECONDS" \
      --account-evidence-sha256 "$evidence_sha256" \
      >"$tmp_state"; then
    rm -f "$tmp_state"
    return 1
  fi
  chmod 0400 "$tmp_state" || {
    rm -f "$tmp_state"
    return 1
  }
  mv "$tmp_state" "$MAINTENANCE_FENCE_STATE" || {
    rm -f "$tmp_state"
    return 1
  }
  load_maintenance_fence_state
}

verify_maintenance_fence() {
  local stage="$1"
  if [[ "${DEPLOY_GATE_MODE:-maintenance_fence}" =~ ^(bootstrap(_resume)?|migration_rebaseline)_stopped$ ]]; then
    verify_bootstrap_stopped_gate "$stage"
    return
  fi
  if [ "${DOWNTIME_WINDOW_ENTERED:-0}" = "1" ]; then
    verify_frozen_maintenance_fence "$stage"
    return
  fi
  verify_online_maintenance_fence "$stage"
}

release_maintenance_fence() {
  local reason="${1:-deploy-exit}"
  if [ "$MAINTENANCE_FENCE_ACQUIRED" != "1" ]; then
    return
  fi
  verify_account_stall_operation_lock || {
    echo "!! maintenance fence release lock verification FAILED" >&2
    return
  }
  if ! timeout \
    --signal=TERM \
    --kill-after=5s \
    "${ACCOUNT_STALL_OPERATION_TIMEOUT_SECONDS}s" \
    "$T/.venv-cp/bin/python" \
    "$MIGRATION_RUNNER" \
    maintenance-fence \
    release \
    --database-env-file "$T/.env.v3" \
    --fence-id "$MAINTENANCE_FENCE_ID" \
    --owner-token "$ACCOUNT_STALL_OPERATION_LOCK_TOKEN" \
    --actor "$ROLLOUT_ACTOR" \
    --reason "$reason" \
    >/dev/null; then
    echo "!! maintenance fence release FAILED; lease remains bounded" >&2
    return
  fi
  MAINTENANCE_FENCE_ACQUIRED=0
}

read_database_url() {
  "$T/.venv-cp/bin/python" - "$T/.env.v3" <<'PY'
import sys
from pathlib import Path

environment_path = Path(sys.argv[1])
database_url = ""
for raw in environment_path.read_text(encoding="utf-8").splitlines():
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
    database_url = value
    break
if not database_url:
    raise SystemExit("DATABASE_URL is missing from control-plane environment")
if "\n" in database_url or "\r" in database_url:
    raise SystemExit("DATABASE_URL contains a newline")
print(database_url)
PY
}
configure_deploy_gate_mode() {
  local fleet_state="live_or_restartable"
  local role_env_count
  if all_execution_accounts_stopped; then
    fleet_state="all_stopped"
  fi
  DEPLOY_GATE_MODE="$(
    "$T/.venv-cp/bin/python" - \
      "$T/.env.v3" \
      "$ROLLOUT_NODE" \
      "$EMERGENCY_ROLLBACK" \
      "$ACCOUNT_A_RELEASE_MANIFEST" \
      "$REDIS_CAPACITY_EVIDENCE" \
      "$STAGING/bundle-manifest.json" \
      "$T/RELEASE_MANIFEST.json" \
      "$RELEASE_MANIFEST" \
      "$SKIP_RESUME" \
      "$fleet_state" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

import psycopg2


def read_environment(path):
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key.strip()] = value
    return values


database_url = read_environment(sys.argv[1]).get("DATABASE_URL", "")
if not database_url:
    raise SystemExit("DATABASE_URL is missing from control-plane environment")
rollout_node = sys.argv[2]
emergency_rollback = sys.argv[3]
resume_manifest_raw = sys.argv[4]
capacity_evidence_path = Path(sys.argv[5])
bundle_manifest_path = Path(sys.argv[6])
live_manifest_path = Path(sys.argv[7])
release_manifest_path = Path(sys.argv[8])
skip_resume = sys.argv[9]
fleet_state = sys.argv[10]
if fleet_state not in {"all_stopped", "live_or_restartable"}:
    raise SystemExit("execution fleet state is invalid")
if emergency_rollback == "1":
    print("maintenance_fence")
    raise SystemExit(0)
resume_manifest_path = False
migration_rebaseline = False
bootstrap_stopped_recovery = False
bootstrap_stopped_replay = False
if resume_manifest_raw:
    resume_manifest_path = Path(resume_manifest_raw)
    if rollout_node != "trader-v3-node-a":
        raise SystemExit(
            "ACCOUNT_A_RELEASE_MANIFEST requires trader-v3-node-a"
        )
    if (
        not resume_manifest_path.is_file()
        or resume_manifest_path.is_symlink()
    ):
        raise SystemExit("ACCOUNT_A_RELEASE_MANIFEST is invalid")
    if (
        not capacity_evidence_path.is_file()
        or capacity_evidence_path.is_symlink()
    ):
        raise SystemExit("Redis capacity evidence is invalid")
    if (
        not bundle_manifest_path.is_file()
        or bundle_manifest_path.is_symlink()
    ):
        raise SystemExit("bundle manifest is invalid")
conn = psycopg2.connect(database_url)
try:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT to_regclass('public.redis_fencing_epochs') IS NOT NULL,
                   to_regclass('public.reviewed_release_rollouts') IS NOT NULL
            """
        )
        redis_table_exists, rollout_table_exists = cur.fetchone()
        if not redis_table_exists and not rollout_table_exists:
            redis_count = 0
            rollout_count = 0
        elif redis_table_exists and rollout_table_exists:
            cur.execute("SELECT count(*) FROM redis_fencing_epochs")
            redis_count = int(cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM reviewed_release_rollouts")
            rollout_count = int(cur.fetchone()[0])
            if resume_manifest_path is not False:
                manifest = json.loads(
                    resume_manifest_path.read_text(encoding="utf-8")
                )
                capacity_evidence = json.loads(
                    capacity_evidence_path.read_text(encoding="utf-8")
                )
                cur.execute(
                    """
                    SELECT release_id,
                           redis_fencing_epoch::text,
                           image_digest,
                           config_sha256,
                           dependency_lock_sha256,
                           schema_epoch,
                           manifest_sha256,
                           bundle_manifest_sha256,
                           registration_idempotency_key,
                           phase
                    FROM reviewed_release_rollouts
                    WHERE phase='account_a_canary'
                    """
                )
                active_rollouts = cur.fetchall()
                if len(active_rollouts) != 1:
                    raise SystemExit(
                        "bootstrap resume requires one account_a_canary rollout"
                    )
                rollout = active_rollouts[0]
                manifest_bytes = resume_manifest_path.read_bytes()
                bundle_bytes = bundle_manifest_path.read_bytes()
                expected = (
                    manifest.get("release_id"),
                    capacity_evidence.get("redis_fencing_epoch"),
                    manifest.get("image_digest"),
                    manifest.get("config_sha256"),
                    manifest.get("dependency_lock_sha256"),
                    # Registration derives schema_epoch from the db entry
                    # of the schema_epochs map; the release manifest has
                    # no top-level schema_epoch key.
                    (manifest.get("schema_epochs") or {}).get("db"),
                    hashlib.sha256(manifest_bytes).hexdigest(),
                    hashlib.sha256(bundle_bytes).hexdigest(),
                    f"bootstrap-register:{manifest.get('release_id')}",
                    "account_a_canary",
                )
                if tuple(rollout) != expected:
                    raise SystemExit(
                        "bootstrap resume manifest differs from active rollout"
                    )
                cur.execute(
                    """
                    SELECT redis_fencing_epoch::text,
                           capacity_evidence_sha256
                    FROM redis_fencing_epochs
                    WHERE domain='trader-v3'
                      AND status='active'
                    """
                )
                active_epochs = cur.fetchall()
                expected_epoch = (
                    capacity_evidence.get("redis_fencing_epoch"),
                    hashlib.sha256(
                        capacity_evidence_path.read_bytes()
                    ).hexdigest(),
                )
                if active_epochs != [expected_epoch]:
                    raise SystemExit(
                        "bootstrap resume Redis epoch evidence differs"
                    )
        else:
            raise SystemExit("partial rollout history tables detected")
        migration_live_manifest = False
        live_manifest_present = False
        migration_candidate = False
        if resume_manifest_path is False:
            if live_manifest_path.is_symlink():
                raise SystemExit("live release manifest cannot be a symlink")
            if live_manifest_path.exists():
                live_manifest_present = True
                if not live_manifest_path.is_file():
                    raise SystemExit("live release manifest is invalid")
                if release_manifest_path.is_symlink():
                    raise SystemExit("release manifest cannot be a symlink")
                if release_manifest_path.exists():
                    if not release_manifest_path.is_file():
                        raise SystemExit("release manifest is invalid")
                    if (
                        live_manifest_path.read_bytes()
                        != release_manifest_path.read_bytes()
                    ):
                        migration_candidate = True
                    else:
                        migration_live_manifest = json.loads(
                            live_manifest_path.read_text(encoding="utf-8")
                        )
                        if not isinstance(migration_live_manifest, dict):
                            raise SystemExit(
                                "live release manifest root is invalid"
                            )
                        migration_candidate = True
                else:
                    migration_candidate = True
            else:
                migration_candidate = True
        if migration_candidate:
            if rollout_node != "trader-v3-node-a":
                raise SystemExit(
                    "migration rebaseline requires trader-v3-node-a"
                )
            if skip_resume != "1":
                raise SystemExit(
                    "migration rebaseline requires SKIP_RESUME=1"
                )
            for path, label in (
                (capacity_evidence_path, "Redis capacity evidence"),
                (bundle_manifest_path, "bundle manifest"),
            ):
                if not path.is_file() or path.is_symlink():
                    raise SystemExit(f"{label} is invalid")
            target_manifest = False
            if release_manifest_path.is_symlink():
                raise SystemExit("release manifest cannot be a symlink")
            if release_manifest_path.exists():
                if not release_manifest_path.is_file():
                    raise SystemExit("release manifest is invalid")
                target_manifest = json.loads(
                    release_manifest_path.read_text(encoding="utf-8")
                )
                if not isinstance(target_manifest, dict):
                    raise SystemExit("release manifest root is invalid")
            capacity_evidence = json.loads(
                capacity_evidence_path.read_text(encoding="utf-8")
            )
            if not isinstance(capacity_evidence, dict):
                raise SystemExit("Redis capacity evidence root is invalid")
            cur.execute(
                """
                SELECT release_id,
                       redis_fencing_epoch::text,
                       image_digest,
                       config_sha256,
                       dependency_lock_sha256,
                       schema_epoch,
                       manifest_sha256,
                       bundle_manifest_sha256,
                       registration_idempotency_key,
                       phase
                FROM reviewed_release_rollouts
                WHERE phase IN (
                    'account_a_canary',
                    'account_b_rollout',
                    'account_c_rollout',
                    'account_d_rollout'
                )
                """
            )
            active_rollouts = cur.fetchall()
            if len(active_rollouts) != 1:
                if live_manifest_present and fleet_state == "live_or_restartable":
                    migration_candidate = False
                    migration_rebaseline = False
                    print("maintenance_fence")
                    raise SystemExit(0)
                raise SystemExit(
                    "migration rebaseline requires one active rollout"
                )
            cur.execute(
                """
                SELECT redis_fencing_epoch::text,
                       capacity_evidence_sha256
                FROM redis_fencing_epochs
                WHERE domain='trader-v3'
                  AND status='active'
                """
            )
            active_epochs = cur.fetchall()
            if len(active_epochs) != 1:
                raise SystemExit(
                    "migration rebaseline requires one active Redis epoch"
                )
            active_rollout = active_rollouts[0]
            active_epoch = active_epochs[0]
            if active_rollout[1] != active_epoch[0]:
                raise SystemExit(
                    "migration rebaseline active rollout epoch differs"
                )
            target_epoch = capacity_evidence.get("redis_fencing_epoch")
            if not isinstance(target_epoch, str) or not target_epoch.strip():
                raise SystemExit(
                    "migration rebaseline capacity evidence lacks epoch"
                )
            target_evidence_sha256 = hashlib.sha256(
                capacity_evidence_path.read_bytes()
            ).hexdigest()
            if active_epoch[0] == target_epoch:
                if target_manifest is False:
                    if fleet_state == "all_stopped":
                        if rollout_node != "trader-v3-node-a":
                            raise SystemExit(
                                "bootstrap stopped recovery requires "
                                "trader-v3-node-a"
                            )
                        if skip_resume != "1":
                            raise SystemExit(
                                "bootstrap stopped recovery requires "
                                "SKIP_RESUME=1"
                            )
                        if active_epoch != (
                            target_epoch,
                            target_evidence_sha256,
                        ):
                            raise SystemExit(
                                "bootstrap stopped recovery Redis evidence "
                                "differs from the active epoch"
                            )
                        bootstrap_stopped_recovery = True
                        migration_candidate = False
                    elif live_manifest_present:
                        migration_candidate = False
                        migration_rebaseline = False
                        print("maintenance_fence")
                        raise SystemExit(0)
                    else:
                        raise SystemExit(
                            "migration rebaseline replay requires "
                            "release manifest"
                        )
                if target_manifest is not False:
                    target_release_id = target_manifest.get("release_id")
                    if (
                        not isinstance(target_release_id, str)
                        or not target_release_id.strip()
                    ):
                        raise SystemExit(
                            "migration rebaseline release manifest lacks "
                            "release_id"
                        )
                else:
                    target_release_id = False
                if target_release_id is not False:
                    target_manifest_sha256 = hashlib.sha256(
                        release_manifest_path.read_bytes()
                    ).hexdigest()
                    target_bundle_sha256 = hashlib.sha256(
                        bundle_manifest_path.read_bytes()
                    ).hexdigest()
                    bootstrap_registration_key = (
                        f"bootstrap-register:{target_release_id}"
                    )
                    expected_bootstrap_replay = (
                        target_release_id,
                        target_epoch,
                        target_manifest.get("image_digest"),
                        target_manifest.get("config_sha256"),
                        target_manifest.get("dependency_lock_sha256"),
                        (target_manifest.get("schema_epochs") or {}).get("db"),
                        target_manifest_sha256,
                        target_bundle_sha256,
                        bootstrap_registration_key,
                        "account_a_canary",
                    )
                else:
                    expected_bootstrap_replay = False
                if (
                    fleet_state == "all_stopped"
                    and expected_bootstrap_replay is not False
                    and tuple(active_rollout) == expected_bootstrap_replay
                ):
                    if active_epoch != (
                        target_epoch,
                        target_evidence_sha256,
                    ):
                        raise SystemExit(
                            "bootstrap stopped replay Redis evidence differs"
                        )
                    bootstrap_stopped_replay = True
                    migration_candidate = False
                elif (
                    target_release_id is not False
                    and active_rollout[0] != target_release_id
                ):
                    if fleet_state == "all_stopped":
                        if rollout_node != "trader-v3-node-a":
                            raise SystemExit(
                                "bootstrap stopped recovery requires "
                                "trader-v3-node-a"
                            )
                        if skip_resume != "1":
                            raise SystemExit(
                                "bootstrap stopped recovery requires "
                                "SKIP_RESUME=1"
                            )
                        if active_epoch != (
                            target_epoch,
                            target_evidence_sha256,
                        ):
                            raise SystemExit(
                                "bootstrap stopped recovery Redis evidence "
                                "differs from the active epoch"
                            )
                        cur.execute(
                            """
                            SELECT count(*)
                            FROM reviewed_release_rollouts
                            WHERE release_id=%s
                            """,
                            (target_release_id,),
                        )
                        if int(cur.fetchone()[0]) != 0:
                            raise SystemExit(
                                "bootstrap stopped recovery target release "
                                "already exists"
                            )
                        bootstrap_stopped_recovery = True
                        migration_candidate = False
                    elif live_manifest_present:
                        migration_candidate = False
                        migration_rebaseline = False
                        print("maintenance_fence")
                        raise SystemExit(0)
                    else:
                        raise SystemExit(
                            "migration rebaseline active release differs "
                            "from the fresh Redis epoch"
                        )
                expected_registration_key = False
                same_epoch_hotfix_key = False
                if target_release_id is not False:
                    expected_registration_key = (
                        "migration-rebaseline-register:"
                        f"{target_release_id}"
                    )
                    same_epoch_hotfix_key = f"register:{target_release_id}"
                if bootstrap_stopped_replay:
                    pass
                elif bootstrap_stopped_recovery:
                    pass
                elif (
                    live_manifest_present
                    and active_rollout[8] == same_epoch_hotfix_key
                ):
                    migration_candidate = False
                    migration_rebaseline = False
                    print("maintenance_fence")
                    raise SystemExit(0)
                elif (
                    migration_live_manifest is not False
                    and active_rollout[8] != expected_registration_key
                ):
                    migration_candidate = False
                else:
                    if target_manifest is False:
                        raise SystemExit(
                            "migration rebaseline replay requires "
                            "release manifest"
                        )
                    expected = (
                        target_release_id,
                        target_epoch,
                        target_manifest.get("image_digest"),
                        target_manifest.get("config_sha256"),
                        target_manifest.get("dependency_lock_sha256"),
                        (target_manifest.get("schema_epochs") or {}).get("db"),
                        target_manifest_sha256,
                        target_bundle_sha256,
                        expected_registration_key,
                        "account_a_canary",
                    )
                    if tuple(active_rollout) != expected:
                        raise SystemExit(
                            "migration rebaseline replay release differs"
                        )
                    if active_epoch != (
                        target_epoch,
                        target_evidence_sha256,
                    ):
                        raise SystemExit(
                            "migration rebaseline replay Redis evidence differs"
                        )
            else:
                if migration_live_manifest is not False:
                    raise SystemExit(
                        "new migration rebaseline requires no live release manifest"
                    )
                cur.execute(
                    """
                    SELECT count(*)
                    FROM redis_fencing_epochs
                    WHERE redis_fencing_epoch=%s
                    """,
                    (target_epoch,),
                )
                if int(cur.fetchone()[0]) != 0:
                    raise SystemExit(
                        "migration rebaseline target Redis epoch already exists"
                    )
                if target_manifest is not False:
                    target_release_id = target_manifest.get("release_id")
                    if (
                        not isinstance(target_release_id, str)
                        or not target_release_id.strip()
                    ):
                        raise SystemExit(
                            "migration rebaseline release manifest lacks "
                            "release_id"
                        )
                    cur.execute(
                        """
                        SELECT count(*)
                        FROM reviewed_release_rollouts
                        WHERE release_id=%s
                        """,
                        (target_release_id,),
                    )
                    if int(cur.fetchone()[0]) != 0:
                        raise SystemExit(
                            "migration rebaseline target release already exists"
                        )
            migration_rebaseline = migration_candidate
finally:
    conn.close()
if redis_count == 0 and rollout_count == 0:
    if resume_manifest_path is not False:
        raise SystemExit("bootstrap resume requires existing rollout history")
    if rollout_node != "trader-v3-node-a":
        raise SystemExit(
            "empty rollout history bootstrap requires trader-v3-node-a"
        )
    print("bootstrap_stopped")
    raise SystemExit(0)
if redis_count > 0 and rollout_count > 0:
    if bootstrap_stopped_replay:
        print("bootstrap_resume_stopped")
        raise SystemExit(0)
    if bootstrap_stopped_recovery:
        print("bootstrap_stopped")
        raise SystemExit(0)
    if resume_manifest_path is not False:
        print("bootstrap_resume_stopped")
        raise SystemExit(0)
    if migration_rebaseline:
        print("migration_rebaseline_stopped")
        raise SystemExit(0)
    print("maintenance_fence")
    raise SystemExit(0)
raise SystemExit("partial rollout history detected")
PY
  )" || die "deploy gate mode detection failed"
  if [ -n "$REQUIRED_DEPLOY_GATE_MODE" ] \
    && [ "$DEPLOY_GATE_MODE" != "$REQUIRED_DEPLOY_GATE_MODE" ]; then
    die "deploy gate mode $DEPLOY_GATE_MODE differs from required $REQUIRED_DEPLOY_GATE_MODE"
  fi
  case "$DEPLOY_GATE_MODE" in
    bootstrap_stopped)
      seal_all_execution_accounts_stopped
      BOOTSTRAP_ALL_NODE_RELEASE=1
      RECREATE_NODES=("${ALL_NODES[@]}")
      role_env_count=0
      for env_file in "${CONTROL_PLANE_ROLE_ENV_FILES[@]}"; do
        if [ -f "$env_file" ]; then
          role_env_count=$((role_env_count + 1))
        fi
      done
      if [ "$role_env_count" -eq 0 ]; then
        CONTROL_PLANE_ROLE_BOOTSTRAP_REQUIRED=1
      elif [ "$role_env_count" -eq "${#CONTROL_PLANE_ROLE_ENV_FILES[@]}" ]; then
        CONTROL_PLANE_ROLE_BOOTSTRAP_REQUIRED=0
      else
        die "partial control-plane role environment detected during bootstrap"
      fi
      ;;
    bootstrap_resume_stopped)
      seal_all_execution_accounts_stopped
      BOOTSTRAP_ALL_NODE_RELEASE=1
      RECREATE_NODES=("${ALL_NODES[@]}")
      role_env_count=0
      for env_file in "${CONTROL_PLANE_ROLE_ENV_FILES[@]}"; do
        if [ -f "$env_file" ]; then
          role_env_count=$((role_env_count + 1))
        fi
      done
      if [ "$role_env_count" -eq 0 ]; then
        CONTROL_PLANE_ROLE_BOOTSTRAP_REQUIRED=1
      elif [ "$role_env_count" -eq "${#CONTROL_PLANE_ROLE_ENV_FILES[@]}" ]; then
        CONTROL_PLANE_ROLE_BOOTSTRAP_REQUIRED=0
      else
        die "partial control-plane role environment detected during bootstrap resume"
      fi
      ;;
    migration_rebaseline_stopped)
      [ "$SKIP_RESUME" = "1" ] \
        || die "migration rebaseline requires SKIP_RESUME=1"
      seal_all_execution_accounts_stopped
      verify_bootstrap_gate_quiescence
      BOOTSTRAP_ALL_NODE_RELEASE=1
      RECREATE_NODES=("${ALL_NODES[@]}")
      role_env_count=0
      for env_file in "${CONTROL_PLANE_ROLE_ENV_FILES[@]}"; do
        if [ -f "$env_file" ]; then
          role_env_count=$((role_env_count + 1))
        fi
      done
      if [ "$role_env_count" -eq 0 ]; then
        CONTROL_PLANE_ROLE_BOOTSTRAP_REQUIRED=1
      elif [ "$role_env_count" -eq "${#CONTROL_PLANE_ROLE_ENV_FILES[@]}" ]; then
        CONTROL_PLANE_ROLE_BOOTSTRAP_REQUIRED=0
      else
        die "partial control-plane role environment detected during migration rebaseline"
      fi
      ;;
    maintenance_fence)
      CONTROL_PLANE_ROLE_BOOTSTRAP_REQUIRED=0
      if [ "$EMERGENCY_ROLLBACK" = "1" ]; then
        RECREATE_NODES=("$ROLLOUT_NODE")
      elif [ "$ROLLOUT_NODE" = "trader-v3-node-a" ]; then
        BOOTSTRAP_ALL_NODE_RELEASE=1
        RECREATE_NODES=("${ALL_NODES[@]}")
      else
        PHASE_ONLY_ROLLOUT=1
        RECREATE_NODES=()
      fi
      ;;
    *)
      die "invalid deploy gate mode: $DEPLOY_GATE_MODE"
      ;;
  esac
  echo "== deploy gate mode: $DEPLOY_GATE_MODE"
}
BOOTSTRAP_GATE_QUIESCED_BY="ready-halted-or-stopped-container"
migration_rebaseline_live_manifest_exists() {
  local live_manifest="$T/RELEASE_MANIFEST.json"
  [ -e "$live_manifest" ] || [ -L "$live_manifest" ]
}
verify_migration_rebaseline_live_manifest() {
  local live_manifest="$T/RELEASE_MANIFEST.json"
  if [ ! -e "$live_manifest" ] && [ ! -L "$live_manifest" ]; then
    return 1
  fi
  [ -f "$live_manifest" ] && [ ! -L "$live_manifest" ] \
    || die "migration rebaseline live release manifest is invalid"
  if [ ! -f "$RELEASE_MANIFEST" ] || [ -L "$RELEASE_MANIFEST" ]; then
    return 1
  fi
  if cmp -s "$live_manifest" "$RELEASE_MANIFEST"; then
    echo "== migration rebaseline live release manifest matches staging"
    return 0
  fi
  return 1
}
verify_bootstrap_gate_quiescence() {
  BOOTSTRAP_GATE_QUIESCED_BY="ready-halted-or-stopped-container"
  if [ "$DEPLOY_GATE_MODE" != "migration_rebaseline_stopped" ]; then
    verify_all_execution_accounts_quiesced
    return
  fi
  if migration_rebaseline_live_manifest_exists \
    && verify_migration_rebaseline_live_manifest; then
    verify_all_execution_accounts_quiesced
    return
  fi
  verify_all_execution_accounts_stopped
  BOOTSTRAP_GATE_QUIESCED_BY="stopped-container"
}
verify_bootstrap_stopped_gate() {
  local stage="$1"
  local expected_epoch
  local live_epoch
  verify_account_stall_operation_lock
  case "${DEPLOY_GATE_MODE:-}" in
    bootstrap_stopped|bootstrap_resume_stopped|migration_rebaseline_stopped)
      ;;
    *)
      die "bootstrap stopped gate used outside bootstrap mode"
      ;;
  esac
  [ "$SKIP_RESUME" = "1" ] \
    || die "bootstrap stopped gate requires SKIP_RESUME=1"
  verify_bootstrap_gate_quiescence
  expected_epoch="$(
    python3 - "$REDIS_CAPACITY_EVIDENCE" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
print(str(payload.get("redis_fencing_epoch") or "").strip())
PY
  )" || die "bootstrap Redis capacity evidence is unreadable"
  [ -n "$expected_epoch" ] \
    || die "bootstrap Redis capacity evidence lacks epoch"
  live_epoch="$(
    docker exec trader-v3-redis redis-cli --raw GET \
      "$REDIS_FENCING_EPOCH_KEY" \
      | tr -d '\r'
  )" || die "bootstrap Redis epoch marker is unavailable"
  [ "$live_epoch" = "$expected_epoch" ] \
    || die "bootstrap Redis epoch differs from capacity evidence"
  BOOTSTRAP_REDIS_FENCING_EPOCH="$expected_epoch"
  export BOOTSTRAP_REDIS_FENCING_EPOCH
  if [ -d "$BACKUP_ROOT" ]; then
    python3 - \
      "$BOOTSTRAP_STOPPED_GATE_LOG" \
      "$stage" \
      "$expected_epoch" \
      "$BOOTSTRAP_GATE_QUIESCED_BY" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
entry = {
    "schema_version": "trader-v3-bootstrap-stopped-gate/v1",
    "stage": sys.argv[2],
    "checked_at": datetime.now(timezone.utc).isoformat(),
    "redis_fencing_epoch": sys.argv[3],
    "accounts": ["account-a", "account-b", "account-c", "account-d"],
    "quiesced_by": sys.argv[4],
    "skip_resume": True,
}
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(entry, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
PY
  fi
}
acquire_maintenance_fence_after_bootstrap() {
  local tmp_state
  if ! verify_account_stall_operation_lock; then
    return 1
  fi
  if [[ "$DEPLOY_GATE_MODE" =~ ^(bootstrap(_resume)?|migration_rebaseline)_stopped$ ]]; then
    [ "$ROLLOUT_TRACKED" = "1" ] \
      || die "bootstrap maintenance fence requires a tracked rollout"
  fi
  [ "$MAINTENANCE_FENCE_ACQUIRED" = "0" ] \
    || die "maintenance fence was already acquired"
  if ! verify_all_execution_accounts_quiesced; then
    return 1
  fi
  MAINTENANCE_FENCE_ID="$(
    python3 -c 'from uuid import uuid4; print(uuid4())'
  )"
  tmp_state="$MAINTENANCE_FENCE_STATE.new"
  rm -f "$tmp_state"
  if ! timeout \
      --signal=TERM \
      --kill-after=5s \
      "${ACCOUNT_STALL_OPERATION_TIMEOUT_SECONDS}s" \
      "$T/.venv-cp/bin/python" \
      "$MIGRATION_RUNNER" \
      maintenance-fence \
      acquire \
      --database-env-file "$T/.env.v3" \
      --fence-id "$MAINTENANCE_FENCE_ID" \
      --operation deploy \
      --actor "$ROLLOUT_ACTOR" \
      --owner-token "$ACCOUNT_STALL_OPERATION_LOCK_TOKEN" \
      --lease-seconds "$ACCOUNT_STALL_FENCE_LEASE_SECONDS" \
      --heartbeat-max-age-seconds \
        "$ACCOUNT_STALL_HEARTBEAT_MAX_AGE_SECONDS" \
      >"$tmp_state"; then
    rm -f "$tmp_state"
    return 1
  fi
  chmod 0400 "$tmp_state" || {
    rm -f "$tmp_state"
    return 1
  }
  mv "$tmp_state" "$MAINTENANCE_FENCE_STATE" || {
    rm -f "$tmp_state"
    return 1
  }
  if ! load_maintenance_fence_state; then
    return 1
  fi
  DEPLOY_GATE_MODE="maintenance_fence"
  if ! verify_maintenance_fence "post-bootstrap-heartbeat-fence"; then
    return 1
  fi
  echo "== four-account heartbeat maintenance fence acquired"
}
capture_pre_migration_database_backup() {
  local database_url="$1"
  local dump_path="$2"
  local list_path="$3"
  local hash_path="$4"
  local dump_directory
  local dump_name
  local hash_name
  local artifact
  if [ -z "$database_url" ]; then
    die "DATABASE_URL is required for database backup"
  fi
  if [[ "$database_url" == *$'\n'* || "$database_url" == *$'\r'* ]]; then
    die "DATABASE_URL contains a newline"
  fi
  if [[ ! "$PG_BACKUP_TIMEOUT_SECONDS" =~ ^[1-9][0-9]{0,3}$ ]]; then
    die "PG_BACKUP_TIMEOUT_SECONDS must be an integer from 1 to 3600"
  fi
  if [ "$PG_BACKUP_TIMEOUT_SECONDS" -gt 3600 ]; then
    die "PG_BACKUP_TIMEOUT_SECONDS must be an integer from 1 to 3600"
  fi
  dump_directory="$(dirname "$dump_path")"
  if [ "$(dirname "$list_path")" != "$dump_directory" ]; then
    die "database backup list must share the dump directory"
  fi
  if [ "$(dirname "$hash_path")" != "$dump_directory" ]; then
    die "database backup hash must share the dump directory"
  fi
  for artifact in "$dump_path" "$list_path" "$hash_path"; do
    if [ -e "$artifact" ]; then
      die "database backup artifact already exists: $artifact"
    fi
  done
  umask 077
  # libpq only expands connection URLs passed as --dbname; PGDATABASE is
  # taken literally as a database name and would fall back to the local
  # Unix socket.
  PGCONNECT_TIMEOUT=10 \
    timeout \
      --signal=TERM \
      --kill-after=10s \
      "${PG_BACKUP_TIMEOUT_SECONDS}s" \
      "$PG_DUMP_BIN" \
      --format=custom \
      --no-owner \
      --no-privileges \
      --dbname="$database_url" \
      --file="$dump_path"
  if [ ! -s "$dump_path" ]; then
    die "pre-migration PostgreSQL backup is empty"
  fi
  timeout \
    --signal=TERM \
    --kill-after=10s \
    "${PG_BACKUP_TIMEOUT_SECONDS}s" \
    "$PG_RESTORE_BIN" \
    --list \
    "$dump_path" >"$list_path"
  if [ ! -s "$list_path" ]; then
    die "pre-migration PostgreSQL restore listing is empty"
  fi
  grep -Eq ' TABLE( DATA)? public node_heartbeats ' "$list_path" \
    || die "pre-migration PostgreSQL backup lacks node_heartbeats"
  dump_name="$(basename "$dump_path")"
  hash_name="$(basename "$hash_path")"
  (
    cd "$dump_directory"
    sha256sum "$dump_name" >"$hash_name"
    sha256sum -c "$hash_name" >/dev/null
  )
  chmod 0600 "$dump_path" "$list_path" "$hash_path"
}
unit_exists() {
  local unit="$1"
  local load_state
  load_state="$(
    systemctl show --property=LoadState --value "$unit" 2>/dev/null \
      || true
  )"
  [ -n "$load_state" ] && [ "$load_state" != "not-found" ]
}
discover_control_plane_units() {
  local role_count=0
  local unit
  CONTROL_PLANE_UNITS=()
  CONTROL_PLANE_ISOLATION_REQUIRED=0
  for unit in "${ROLE_CONTROL_PLANE_UNITS[@]}"; do
    if unit_exists "$unit"; then
      role_count=$((role_count + 1))
    fi
  done
  if [ "$role_count" -ne 0 ] \
    && [ "$role_count" -ne "${#ROLE_CONTROL_PLANE_UNITS[@]}" ]; then
    die "partial control-plane role topology detected: $role_count/${#ROLE_CONTROL_PLANE_UNITS[@]} units"
  fi
  if [ "$role_count" -eq "${#ROLE_CONTROL_PLANE_UNITS[@]}" ]; then
    if unit_exists "$LEGACY_CONTROL_PLANE_UNIT" \
      && systemctl is-active --quiet "$LEGACY_CONTROL_PLANE_UNIT"; then
      die "mixed control-plane topology: legacy and role units are active"
    fi
    CONTROL_PLANE_UNITS=("${ROLE_CONTROL_PLANE_UNITS[@]}")
    CONTROL_PLANE_TOPOLOGY="roles"
    return
  fi
  unit_exists "$LEGACY_CONTROL_PLANE_UNIT" \
    || die "legacy control-plane unit not found: $LEGACY_CONTROL_PLANE_UNIT"
  CONTROL_PLANE_UNITS=("$LEGACY_CONTROL_PLANE_UNIT")
  CONTROL_PLANE_TOPOLOGY="legacy"
  case "$CONTROL_PLANE_ISOLATION_MODE" in
    require)
      [ -f "$CONTROL_PLANE_ISOLATION_SCRIPT" ] \
        || die "control-plane isolation script missing: $CONTROL_PLANE_ISOLATION_SCRIPT"
      CONTROL_PLANE_ISOLATION_REQUIRED=1
      ;;
    disable)
      [ "$EMERGENCY_ROLLBACK" = "1" ] \
        || die "legacy control-plane topology requires emergency rollback"
      ;;
    *)
      die "invalid CONTROL_PLANE_ISOLATION_MODE: $CONTROL_PLANE_ISOLATION_MODE"
      ;;
  esac
}
verify_control_plane_isolation_artifact() {
  if [ "$CONTROL_PLANE_ISOLATION_REQUIRED" != "1" ]; then
    return
  fi
  awk '{print $2}' "$STAGING/SHA256SUMS" \
    | sed -e 's/^\*//' -e 's|^\./||' \
    | grep -Fxq "hk-control-plane-isolation.sh" \
    || die "SHA256SUMS does not cover hk-control-plane-isolation.sh"
}
reconcile_control_plane_lock_privileges() {
  # Migration 0016 is the schema source of truth. Repeating its column grants
  # keeps older databases recoverable while the migration is reconciled.
  if [ "${EMERGENCY_ROLLBACK:-0}" = "1" ]; then
    return
  fi
  "$T/.venv-cp/bin/python" - "$T/.env.v3" <<'PY'
import sys
from pathlib import Path

import psycopg2

# These grants mirror migration 0016. The probe column remains unwritable.
# Each entry is (role, table, lock_column,
# probe_column_that_must_stay_forbidden_or_None).
LOCK_PRIVILEGES = (
    ("trader_v3_node_control", "redis_fencing_epochs", "created_at", None),
    ("trader_v3_event_ingest", "redis_fencing_epochs", "created_at", None),
    ("trader_v3_event_ingest", "node_heartbeats", "created_at", "status"),
    ("trader_v3_operator_query", "node_heartbeats", "created_at", "status"),
    (
        "trader_v3_operator_query",
        "control_plane_maintenance_fences",
        "acquired_at",
        None,
    ),
)


def read_database_url(path):
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
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
        return value
    return ""


database_url = read_database_url(sys.argv[1])
if not database_url:
    raise SystemExit("DATABASE_URL is missing from control-plane environment")
conn = psycopg2.connect(database_url)
try:
    with conn.cursor() as cur:
        for role, table, lock_column, _forbidden in LOCK_PRIVILEGES:
            cur.execute(
                'GRANT UPDATE ({}) ON {} TO {}'.format(
                    lock_column, table, role
                )
            )
    conn.commit()
    with conn.cursor() as cur:
        for role, table, lock_column, forbidden in LOCK_PRIVILEGES:
            cur.execute(
                "SELECT has_column_privilege(%s, %s, %s, 'UPDATE')",
                (role, table, lock_column),
            )
            row = cur.fetchone()
            if row is None or row[0] is not True:
                raise SystemExit(
                    "lock privilege verification failed: "
                    f"{role} {table}.{lock_column}"
                )
            if forbidden is None:
                continue
            cur.execute(
                "SELECT has_column_privilege(%s, %s, %s, 'UPDATE')",
                (role, table, forbidden),
            )
            row = cur.fetchone()
            if row is None or row[0] is not False:
                raise SystemExit(
                    "forbidden write column is grantable: "
                    f"{role} {table}.{forbidden}"
                )
finally:
    conn.close()
print("CONTROL_PLANE_ROLE_LOCK_PRIVILEGES_OK")
PY
}
activate_control_plane_topology() {
  if [ "$CONTROL_PLANE_ISOLATION_REQUIRED" != "1" ]; then
    return
  fi
  verify_maintenance_fence "control-plane-isolation"
  CONTROL_PLANE_RESTARTED=1
  if [[ "${DEPLOY_GATE_MODE:-maintenance_fence}" =~ ^(bootstrap(_resume)?|migration_rebaseline)_stopped$ ]]; then
    TRADER_ROOT="$T" \
    ACCOUNT_STALL_SYSTEMD_RESOURCE_ROOT="$STAGING/infra/systemd" \
    CONTROL_PLANE_ISOLATION_BACKUP_ROOT="$BACKUP_ROOT/control-plane-isolation" \
    LEGACY_CONTROL_PLANE_UNIT="$LEGACY_CONTROL_PLANE_UNIT" \
    ACCOUNT_STALL_MAINTENANCE_FENCE_OWNERSHIP="bootstrap_stopped" \
    ACCOUNT_STALL_BOOTSTRAP_STOPPED_GATE=1 \
    ACCOUNT_STALL_BOOTSTRAP_REDIS_FENCING_EPOCH="$BOOTSTRAP_REDIS_FENCING_EPOCH" \
    ACCOUNT_STALL_REDIS_FENCING_EPOCH_KEY="$REDIS_FENCING_EPOCH_KEY" \
      bash "$CONTROL_PLANE_ISOLATION_SCRIPT"
  else
    TRADER_ROOT="$T" \
    ACCOUNT_STALL_SYSTEMD_RESOURCE_ROOT="$STAGING/infra/systemd" \
    CONTROL_PLANE_ISOLATION_BACKUP_ROOT="$BACKUP_ROOT/control-plane-isolation" \
    LEGACY_CONTROL_PLANE_UNIT="$LEGACY_CONTROL_PLANE_UNIT" \
      bash "$CONTROL_PLANE_ISOLATION_SCRIPT"
  fi
  CONTROL_PLANE_ISOLATION_ACTIVATED=1
  discover_control_plane_units
  [ "$CONTROL_PLANE_TOPOLOGY" = "roles" ] \
    || die "control-plane isolation did not activate all role units"
}
restart_control_plane_units() {
  local unit
  [ "${#CONTROL_PLANE_UNITS[@]}" -gt 0 ] \
    || die "control-plane restart set is empty"
  verify_maintenance_fence "control-plane-restart"
  CONTROL_PLANE_RESTARTED=1
  systemctl restart "${CONTROL_PLANE_UNITS[@]}"
  sleep 2
  for unit in "${CONTROL_PLANE_UNITS[@]}"; do
    systemctl is-active --quiet "$unit" \
      || die "$unit failed to restart"
  done
}
verify_control_plane_router_contract() {
  local router_url
  router_url="${CONTROL_PLANE_ROUTER_URL:-http://127.0.0.1:8080}"
  python3 - "$router_url" <<'PY'
from __future__ import annotations

import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

base_url = sys.argv[1].rstrip("/")
request = Request(
    (
        f"{base_url}/v1/nodes/"
        "control-plane-router-contract/incidents/resolve"
    ),
    data=b"{}",
    method="POST",
    headers={"Content-Type": "application/json"},
)
try:
    with urlopen(request, timeout=5) as response:
        status = response.status
except HTTPError as exc:
    status = exc.code
except URLError as exc:
    raise SystemExit(
        f"incident resolution route is unavailable: {exc.reason}"
    ) from exc
if status == 404:
    raise SystemExit("incident resolution route returned 404")
if status >= 500:
    raise SystemExit(
        f"incident resolution route returned unhealthy HTTP {status}"
    )
PY
}
restart_hermes_units() {
  local unit
  if [ "$HERMES_RESTART_REQUIRED" != "1" ]; then
    return
  fi
  verify_maintenance_fence "hermes-restart"
  for unit in "${HERMES_REQUIRED_UNITS[@]}"; do
    unit_exists "$unit" || die "Hermes unit not found: $unit"
  done
  HERMES_RESTARTED=1
  systemctl restart "${HERMES_REQUIRED_UNITS[@]}"
  sleep 2
  for unit in "${HERMES_REQUIRED_UNITS[@]}"; do
    systemctl is-active --quiet "$unit" \
      || die "$unit failed to restart"
  done
}
rollback_restart_hermes_units() {
  local unit
  if [ "$HERMES_RESTARTED" != "1" ]; then
    return
  fi
  for unit in "${HERMES_REQUIRED_UNITS[@]}"; do
    if ! unit_exists "$unit"; then
      echo "!! Hermes rollback restart unit missing: $unit" >&2
      return 1
    fi
  done
  systemctl restart "${HERMES_REQUIRED_UNITS[@]}" >/dev/null 2>&1 \
    || { echo "!! Hermes rollback restart FAILED: ${HERMES_REQUIRED_UNITS[*]}" >&2; return 1; }
}
restart_exchange_state_recorder() {
  local action_required=0
  if [ "$EXCHANGE_STATE_RESTART_REQUIRED" = "1" ]; then
    action_required=1
  fi
  if ! systemctl is-active --quiet "$EXCHANGE_STATE_RECORDER_UNIT"; then
    action_required=1
  fi
  if ! systemctl is-enabled --quiet "$EXCHANGE_STATE_RECORDER_UNIT"; then
    action_required=1
  fi
  if [ "$action_required" != "1" ]; then
    return
  fi
  verify_maintenance_fence "exchange-state-restart"
  unit_exists "$EXCHANGE_STATE_RECORDER_UNIT" \
    || die "exchange-state unit not found: $EXCHANGE_STATE_RECORDER_UNIT"
  EXCHANGE_STATE_RESTARTED=1
  systemctl enable "$EXCHANGE_STATE_RECORDER_UNIT"
  if [ "$EXCHANGE_STATE_RESTART_REQUIRED" = "1" ]; then
    systemctl restart "$EXCHANGE_STATE_RECORDER_UNIT"
  else
    systemctl start "$EXCHANGE_STATE_RECORDER_UNIT"
  fi
  sleep 2
  systemctl is-active --quiet "$EXCHANGE_STATE_RECORDER_UNIT" \
    || die "$EXCHANGE_STATE_RECORDER_UNIT failed to restart"
  systemctl is-enabled --quiet "$EXCHANGE_STATE_RECORDER_UNIT" \
    || die "$EXCHANGE_STATE_RECORDER_UNIT failed to enable"
}
rollback_restart_exchange_state_recorder() {
  if [ "$EXCHANGE_STATE_RESTARTED" != "1" ]; then
    return
  fi
  if ! unit_exists "$EXCHANGE_STATE_RECORDER_UNIT"; then
    echo "!! exchange-state rollback restart unit missing: $EXCHANGE_STATE_RECORDER_UNIT" >&2
    return 1
  fi
  systemctl restart "$EXCHANGE_STATE_RECORDER_UNIT" >/dev/null 2>&1 \
    || { echo "!! exchange-state rollback restart FAILED: $EXCHANGE_STATE_RECORDER_UNIT" >&2; return 1; }
}
docker_compose_watcher() {
  docker compose \
    --project-name "$WATCHER_COMPOSE_PROJECT" \
    --file "$WATCHER_COMPOSE_FILE" \
    "$@"
}
verify_watcher_health() {
  local payload
  local healthy=0
  local attempt
  # A recreated watcher needs time to restore its Telegram session
  # before the status endpoint answers; probe with a bounded retry
  # window instead of a single shot.
  for attempt in $(seq 1 30); do
    if payload="$(curl -sf "$WATCHER_HEALTH_URL")"; then
      healthy=1
      break
    fi
    sleep 2
  done
  [ "$healthy" = "1" ] \
    || die "telegram-watcher health endpoint failed: $WATCHER_HEALTH_URL"
  python3 - "$payload" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
for key in ("configured", "connected", "loggedIn"):
    if key not in payload:
        raise SystemExit(f"telegram-watcher health lacks {key}")
if payload.get("configured") is not True:
    raise SystemExit("telegram-watcher is not configured")
PY
  docker inspect --format '{{.State.Running}}' "$WATCHER_CONTAINER" \
    | grep -Fxq "true" \
    || die "telegram-watcher container is not running: $WATCHER_CONTAINER"
}
verify_watcher_container_runtime() {
  python3 - "$WATCHER_RUNTIME_MANIFEST" <<'PY' \
    | docker exec -i "$WATCHER_CONTAINER" python3 -
from __future__ import annotations

import json
import sys
from pathlib import PurePosixPath

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
files = manifest.get("files")
if not isinstance(files, list):
    raise SystemExit("watcher runtime manifest files must be a list")
expected = {}
for item in files:
    if not isinstance(item, dict):
        raise SystemExit("watcher runtime manifest file entry is invalid")
    target_path = str(item.get("target_path") or "")
    if not target_path.startswith("/app/"):
        raise SystemExit(f"watcher container target is invalid: {target_path}")
    path = PurePosixPath(target_path)
    if path.is_absolute() is not True or ".." in path.parts:
        raise SystemExit(f"watcher container target is invalid: {target_path}")
    expected[target_path] = str(item.get("sha256") or "")
print(
    "from __future__ import annotations\n"
    "import hashlib\n"
    "import json\n"
    "from pathlib import Path\n"
    f"expected = {json.dumps(expected, sort_keys=True)!r}\n"
    "expected = json.loads(expected)\n"
    "for raw_path, digest in sorted(expected.items()):\n"
    "    path = Path(raw_path)\n"
    "    if not path.is_file() or path.is_symlink():\n"
    "        raise SystemExit(f'watcher container file missing: {raw_path}')\n"
    "    actual = hashlib.sha256(path.read_bytes()).hexdigest()\n"
    "    if actual != digest:\n"
    "        raise SystemExit(f'watcher container hash mismatch: {raw_path}')\n"
)
PY
}
verify_watcher_container_runtime_rollback() {
  python3 - "$WATCHER_RUNTIME_CHANGED_LIST" "$WATCHER_SOURCE_ROOT" <<'PY' \
    | docker exec -i "$WATCHER_CONTAINER" python3 -
from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path, PurePosixPath


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


changed_list = Path(sys.argv[1])
watcher_root = Path(sys.argv[2])
if not changed_list.is_file() or changed_list.is_symlink():
    raise SystemExit("watcher rollback changed-list is missing")
if not watcher_root.is_dir() or watcher_root.is_symlink():
    raise SystemExit(f"watcher root is invalid: {watcher_root}")
resolved_root = watcher_root.resolve()
expected = {}
absent = []
for raw_line in changed_list.read_text(encoding="utf-8").splitlines():
    release_path, separator, _target = raw_line.partition("\t")
    if not separator:
        continue
    if not release_path.startswith("watcher/"):
        raise SystemExit(f"watcher release path is invalid: {release_path}")
    relative = release_path[len("watcher/") :]
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise SystemExit(f"watcher target path is invalid: {relative}")
    host_target = watcher_root / relative_path
    try:
        host_target.resolve().relative_to(resolved_root)
    except ValueError as exc:
        raise SystemExit(
            f"watcher runtime target escapes root: {host_target}"
        ) from exc
    container_target = PurePosixPath("/app") / relative_path.as_posix()
    if host_target.exists():
        if host_target.is_symlink():
            raise SystemExit(
                f"watcher runtime target cannot be a symlink: {host_target}"
            )
        metadata = host_target.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise SystemExit(
                f"watcher runtime target must be regular: {host_target}"
            )
        expected[str(container_target)] = sha256_file(host_target)
    else:
        absent.append(str(container_target))
print(
    "from __future__ import annotations\n"
    "import hashlib\n"
    "import json\n"
    "from pathlib import Path\n"
    f"expected = {json.dumps(expected, sort_keys=True)!r}\n"
    f"absent = {json.dumps(sorted(absent))!r}\n"
    "expected = json.loads(expected)\n"
    "absent = json.loads(absent)\n"
    "for raw_path, digest in sorted(expected.items()):\n"
    "    path = Path(raw_path)\n"
    "    if not path.is_file() or path.is_symlink():\n"
    "        raise SystemExit(f'watcher rollback container file missing: {raw_path}')\n"
    "    actual = hashlib.sha256(path.read_bytes()).hexdigest()\n"
    "    if actual != digest:\n"
    "        raise SystemExit(f'watcher rollback container hash mismatch: {raw_path}')\n"
    "for raw_path in absent:\n"
    "    path = Path(raw_path)\n"
    "    if path.exists() or path.is_symlink():\n"
    "        raise SystemExit(f'watcher rollback container file should be absent: {raw_path}')\n"
)
PY
}
restart_watcher_runtime() {
  if [ "$WATCHER_RESTART_REQUIRED" != "1" ]; then
    return
  fi
  verify_maintenance_fence "telegram-watcher-restart"
  [ -d "$WATCHER_ROOT" ] \
    || die "telegram-watcher root missing: $WATCHER_ROOT"
  [ -f "$WATCHER_COMPOSE_FILE" ] \
    || die "telegram-watcher compose file missing: $WATCHER_COMPOSE_FILE"
  [ -d "$WATCHER_SOURCE_ROOT" ] \
    || die "telegram-watcher source root missing: $WATCHER_SOURCE_ROOT"
  WATCHER_RESTARTED=1
  (
    cd "$WATCHER_ROOT"
    docker_compose_watcher build "$WATCHER_COMPOSE_SERVICE"
    docker_compose_watcher up \
      -d \
      --no-deps \
      --force-recreate \
      "$WATCHER_COMPOSE_SERVICE"
  )
  verify_watcher_health
  verify_watcher_container_runtime
}
rollback_restart_watcher_runtime() {
  if [ "$WATCHER_RESTARTED" != "1" ]; then
    return
  fi
  if [ ! -d "$WATCHER_ROOT" ]; then
    echo "!! telegram-watcher rollback restart root missing: $WATCHER_ROOT" >&2
    return 1
  fi
  (
    cd "$WATCHER_ROOT"
    docker_compose_watcher build "$WATCHER_COMPOSE_SERVICE" \
      >/dev/null 2>&1
    docker_compose_watcher up \
      -d \
      --no-deps \
      --force-recreate \
      "$WATCHER_COMPOSE_SERVICE" \
      >/dev/null 2>&1
  ) || {
    echo "!! telegram-watcher rollback restart FAILED: $WATCHER_CONTAINER" >&2
    return 1
  }
  verify_watcher_health >/dev/null 2>&1 || {
    echo "!! telegram-watcher rollback health FAILED: $WATCHER_CONTAINER" >&2
    return 1
  }
  verify_watcher_container_runtime_rollback >/dev/null 2>&1 || {
    echo "!! telegram-watcher rollback hash verification FAILED: $WATCHER_CONTAINER" >&2
    return 1
  }
}
write_watcher_mapping_rollback_evidence() {
  local gate_status="$1"
  local gate_error="$2"
  local file_restore_status="$3"
  local runtime_restore_status="$4"
  local runtime_rollback_required="$5"
  local runtime_rollback_attempted="$6"
  python3 - \
    "$FOUR_CHANNEL_MAPPING_ROLLBACK_EVIDENCE" \
    "$FOUR_CHANNEL_MAPPING_EVIDENCE" \
    "$WATCHER_RUNTIME_CHANGED_LIST" \
    "$gate_status" \
    "$gate_error" \
    "$file_restore_status" \
    "$runtime_restore_status" \
    "$WATCHER_SCHEMA_RESTART_REQUIRED" \
    "$runtime_rollback_required" \
    "$runtime_rollback_attempted" <<'PY'
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


output_path = Path(sys.argv[1])
pre_restart_evidence = Path(sys.argv[2])
changed_list = Path(sys.argv[3])
gate_error = str(sys.argv[5])[:2048]
runtime_rollback_required = sys.argv[9] == "1"
runtime_rollback_attempted = sys.argv[10] == "1"
payload = {
    "schema_version": "trader-v3-four-channel-mapping-rollback/v2",
    "captured_at": datetime.now(timezone.utc).isoformat(),
    "strict_gate_status": int(sys.argv[4]),
    "strict_gate_error": gate_error,
    "pre_restart_evidence_sha256": sha256_file(pre_restart_evidence),
    "changed_list_sha256": sha256_file(changed_list),
    "watcher_schema_restart_required": sys.argv[8] == "1",
    "file_restore_passed": int(sys.argv[6]) == 0,
    "runtime_rollback_required": runtime_rollback_required,
    "runtime_rollback_attempted": runtime_rollback_attempted,
    "runtime_rollback_not_required": not runtime_rollback_required,
    "runtime_rebuild_health_hash_passed": (
        runtime_rollback_attempted and int(sys.argv[7]) == 0
    ),
}
serialized = (
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
).encode("utf-8")
temporary = output_path.with_name(
    f".{output_path.name}.{os.getpid()}.tmp"
)
descriptor = os.open(
    temporary,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
    0o400,
)
try:
    offset = 0
    while offset < len(serialized):
        offset += os.write(descriptor, serialized[offset:])
    os.fsync(descriptor)
finally:
    os.close(descriptor)
os.replace(temporary, output_path)
os.chmod(output_path, 0o400)
directory_descriptor = os.open(output_path.parent, os.O_RDONLY)
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
PY
}
restore_watcher_runtime_files() {
  local release_path
  local destination
  local label
  local indexed_destination
  local restored
  if [ "$BACKUP_CAPTURED" != "1" ] || [ "$FILES_INSTALLED" != "1" ]; then
    return
  fi
  [ -f "$WATCHER_RUNTIME_CHANGED_LIST" ] \
    || die "watcher rollback changed-list is missing"
  while IFS=$'\t' read -r release_path destination; do
    if [ -z "$release_path" ] || [ -z "$destination" ]; then
      continue
    fi
    restored=0
    while IFS=$'\t' read -r label indexed_destination; do
      if [ "$indexed_destination" != "$destination" ]; then
        continue
      fi
      cp -a -- "$BACKUP_ROOT/files/$label" "$destination" \
        || die "watcher file rollback failed: $destination"
      restored=1
      break
    done < "$BACKUP_ROOT/index.tsv"
    if [ "$restored" = "1" ]; then
      continue
    fi
    grep -Fxq "$destination" "$BACKUP_ROOT/new-files.txt" \
      || die "watcher rollback source is missing: $destination"
    rm -f -- "$destination" \
      || die "watcher new file rollback failed: $destination"
  done < "$WATCHER_RUNTIME_CHANGED_LIST"
}
verify_post_restart_four_channel_account_mapping() {
  local gate_status=0
  local gate_error=""
  local file_restore_status=0
  local runtime_restore_status=0
  local runtime_rollback_required="$WATCHER_RESTARTED"
  local runtime_rollback_attempted=0
  if gate_error="$(
    verify_four_channel_account_mapping \
      "$FOUR_CHANNEL_MAPPING_POST_WATCHER_EVIDENCE" \
      strict 2>&1
  )"; then
    return
  else
    gate_status=$?
  fi
  printf '%s\n' "$gate_error" >&2
  echo "!! watcher strict mapping gate failed; restoring prior watcher runtime" >&2
  if restore_watcher_runtime_files; then
    file_restore_status=0
  else
    file_restore_status=$?
  fi
  if [ "$file_restore_status" = "0" ] \
    && [ "$runtime_rollback_required" = "1" ]; then
    runtime_rollback_attempted=1
    if rollback_restart_watcher_runtime; then
      runtime_restore_status=0
    else
      runtime_restore_status=$?
    fi
  elif [ "$file_restore_status" = "0" ]; then
    runtime_restore_status=0
  else
    runtime_restore_status=1
  fi
  write_watcher_mapping_rollback_evidence \
    "$gate_status" \
    "$gate_error" \
    "$file_restore_status" \
    "$runtime_restore_status" \
    "$runtime_rollback_required" \
    "$runtime_rollback_attempted"
  refresh_backup_checksums
  if [ "$file_restore_status" != "0" ] \
    || [ "$runtime_restore_status" != "0" ]; then
    docker stop --time 30 "$WATCHER_CONTAINER" >/dev/null 2>&1 || true
    return 1
  fi
  WATCHER_RESTARTED=0
  return "$gate_status"
}
restore_installed_runtime_files() {
  local label
  local destination
  local restore_failed=0
  if [ "$BACKUP_CAPTURED" != "1" ] || [ "$FILES_INSTALLED" != "1" ]; then
    return
  fi
  if [ -f "$BACKUP_ROOT/index.tsv" ]; then
    while IFS=$'\t' read -r label destination; do
      if [ -z "$label" ] || [ -z "$destination" ]; then
        continue
      fi
      cp -a -- "$BACKUP_ROOT/files/$label" "$destination" \
        || {
          echo "!! file rollback FAILED: $destination" >&2
          restore_failed=1
        }
    done < "$BACKUP_ROOT/index.tsv"
  fi
  if [ -f "$BACKUP_ROOT/new-files.txt" ]; then
    while IFS= read -r destination; do
      if [ -z "$destination" ]; then
        continue
      fi
      rm -f -- "$destination" \
        || {
          echo "!! new file cleanup FAILED: $destination" >&2
          restore_failed=1
          continue
        }
      if [ -e "$destination" ] || [ -L "$destination" ]; then
        echo "!! new file cleanup verification FAILED: $destination" >&2
        restore_failed=1
      fi
    done < "$BACKUP_ROOT/new-files.txt"
  fi
  return "$restore_failed"
}
rollback_restart_changed_runtimes() {
  if [ "$WATCHER_RESTARTED" = "1" ]; then
    rollback_restart_watcher_runtime || true
  fi
  if [ "$EXCHANGE_STATE_RESTARTED" = "1" ]; then
    rollback_restart_exchange_state_recorder || true
  fi
  if [ "$HERMES_RESTARTED" = "1" ]; then
    rollback_restart_hermes_units || true
  fi
}
install_payload_atomically() {
  local source="$1"
  local target="$2"
  python3 - "$source" "$target" <<'PY'
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
if not source.is_file() or source.is_symlink():
    raise SystemExit(f"source payload is invalid: {source}")
if target.is_symlink():
    raise SystemExit(f"target payload cannot be a symlink: {target}")
if not target.parent.is_dir() or target.parent.is_symlink():
    raise SystemExit(f"target parent is invalid: {target.parent}")
if target.exists():
    metadata = target.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"target payload must be regular: {target}")
    target_mode = stat.S_IMODE(metadata.st_mode)
    target_uid = metadata.st_uid
    target_gid = metadata.st_gid
else:
    target_mode = 0o644
    target_uid = 0
    target_gid = 0
descriptor, temporary_raw = tempfile.mkstemp(
    prefix=f".{target.name}.deploy.",
    dir=target.parent,
)
temporary = Path(temporary_raw)
try:
    with source.open("rb") as reader:
        with os.fdopen(descriptor, "wb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
    os.chmod(temporary, target_mode)
    os.chown(temporary, target_uid, target_gid)
    os.replace(temporary, target)
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
PY
}
install_host_python_module() {
  local source="$1"
  local target="$2"
  mkdir -p "$(dirname "$target")"
  install_payload_atomically "$source" "$target"
}
verify_optional_host_python_module_target() {
  local target="$1"
  python3 - "$target" <<'PY'
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

target = Path(sys.argv[1])
try:
    parent_metadata = os.lstat(target.parent)
except OSError as exc:
    raise SystemExit(
        f"optional host module parent is unavailable: {target.parent}"
    ) from exc
if not stat.S_ISDIR(parent_metadata.st_mode):
    raise SystemExit(
        f"optional host module parent must be a real directory: {target.parent}"
    )
try:
    target_metadata = os.lstat(target)
except FileNotFoundError:
    raise SystemExit(0)
except OSError as exc:
    raise SystemExit(
        f"optional host module target is unavailable: {target}"
    ) from exc
if stat.S_ISLNK(target_metadata.st_mode):
    raise SystemExit(
        f"optional host module target cannot be a symlink: {target}"
    )
if not stat.S_ISREG(target_metadata.st_mode):
    raise SystemExit(
        f"optional host module target must be regular: {target}"
    )
PY
}
install_operator_account_registry_environment() {
  local environment_paths=("$T/.env.v3")
  local env_file
  for env_file in "${CONTROL_PLANE_ROLE_ENV_FILES[@]}"; do
    if [ -f "$env_file" ]; then
      environment_paths+=("$env_file")
    fi
  done
  python3 - \
    "$OPERATOR_ACCOUNT_REGISTRY_JSON" \
    "$V3_OPERATOR_ACCOUNTS" \
    "${environment_paths[@]}" <<'PY'
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

registry_raw = sys.argv[1]
operator_accounts = sys.argv[2]
paths = [Path(raw) for raw in sys.argv[3:]]
expected_accounts = (
    "account-a",
    "account-b",
    "account-c",
    "account-d",
)
registry = json.loads(registry_raw)
if tuple(registry) != expected_accounts:
    raise SystemExit("operator account registry must contain ordered A-D accounts")
if any(value != {} for value in registry.values()):
    raise SystemExit("operator account registry must not hard-code account equity")
if tuple(operator_accounts.split(",")) != expected_accounts:
    raise SystemExit("V3 operator account list must contain ordered A-D accounts")

updates = {
    "OPERATOR_ACCOUNT_REGISTRY_JSON": f"'{registry_raw}'",
    "V3_OPERATOR_ACCOUNTS": operator_accounts,
}
removed_keys = {"OPERATOR_DEFAULT_EFFECTIVE_EQUITY_USDT"}

for path in paths:
    if path.is_symlink():
        raise SystemExit(f"operator account environment cannot be a symlink: {path}")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"operator account environment must be regular: {path}")
    retained = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            retained.append(raw_line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates or key in removed_keys:
            continue
        retained.append(raw_line)
    if retained and retained[-1]:
        retained.append("")
    retained.extend(
        f"{key}={value}"
        for key, value in updates.items()
    )
    content = "\n".join(retained) + "\n"
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f".{path.name}.operator-accounts.",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

for path in paths:
    values = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key in updates:
            if key in values:
                raise SystemExit(f"duplicate {key} in {path}")
            values[key] = value.strip().strip("'").strip('"')
    if values.get("OPERATOR_ACCOUNT_REGISTRY_JSON") != registry_raw:
        raise SystemExit(f"operator account registry verification failed: {path}")
    if values.get("V3_OPERATOR_ACCOUNTS") != operator_accounts:
        raise SystemExit(f"V3 operator account verification failed: {path}")
PY
}
scan_control_plane_journals() {
  local unit
  local journal_output
  for unit in "${CONTROL_PLANE_UNITS[@]}"; do
    journal_output="$(
      journalctl -u "$unit" --since "-2 min" --no-pager 2>&1
    )" || die "control-plane journal scan failed: $unit"
    if grep -qiE "traceback|validationerror" <<<"$journal_output"; then
      die "control-plane logging errors after restart: $unit"
    fi
  done
}
probe_execution_account_quiesced() {
  local account_id="$1"
  local container="$2"
  local port="$3"
  local ready_file
  local trading_state
  local running
  ready_file="$(mktemp)"
  TEMP_FILES+=("$ready_file")
  if curl \
    --silent \
    --show-error \
    --fail \
    --max-time 5 \
    "http://127.0.0.1:$port/ready" >"$ready_file"; then
    if ! trading_state="$(
      python3 - "$ready_file" "$account_id" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
expected_account_id = sys.argv[2]
if payload.get("account_id") != expected_account_id:
    raise SystemExit("ready payload account_id mismatch")
trading_state = payload.get("trading_state")
if not isinstance(trading_state, str) or not trading_state.strip():
    raise SystemExit("ready payload lacks trading_state")
print(trading_state.strip().upper())
PY
    )"; then
      QUIESCE_PROBE_FAILURE="soft:$account_id readiness payload is invalid"
      return 1
    fi
    if [ "$trading_state" != "HALTED" ]; then
      QUIESCE_PROBE_FAILURE="hard:$account_id trading state is $trading_state; shared mutation requires HALTED"
      return 1
    fi
    echo "== $account_id quiesced via /ready HALTED"
    return 0
  fi
  if ! running="$(
    docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null
  )"; then
    QUIESCE_PROBE_FAILURE="soft:$account_id readiness is unavailable and $container cannot be inspected"
    return 1
  fi
  case "$running" in
    false)
      echo "== $account_id quiesced via stopped container $container"
      return 0
      ;;
    true)
      QUIESCE_PROBE_FAILURE="soft:$account_id readiness is unavailable while $container is running"
      return 1
      ;;
    *)
      die "$account_id container running state is invalid: $running"
      ;;
  esac
}
verify_execution_account_quiesced() {
  local account_id="$1"
  local container="$2"
  local port="$3"
  local attempt
  # A RUNNING release node can be transiently unready (503) while its
  # exchange-evidence heartbeats recover from venue backoff, so soft
  # probe failures retry within a bounded window; hard failures (an
  # ACTIVE peer during shared mutation) die immediately.
  for attempt in $(seq 1 "${ACCOUNT_QUIESCE_PROBE_ATTEMPTS:-45}"); do
    QUIESCE_PROBE_FAILURE=""
    if probe_execution_account_quiesced "$account_id" "$container" "$port"; then
      return
    fi
    case "$QUIESCE_PROBE_FAILURE" in
      hard:*)
        die "${QUIESCE_PROBE_FAILURE#hard:}"
        ;;
    esac
    sleep "${ACCOUNT_QUIESCE_PROBE_INTERVAL_SECONDS:-2}"
  done
  die "${QUIESCE_PROBE_FAILURE#soft:} (after $attempt probes)"
}
verify_all_execution_accounts_quiesced() {
  verify_execution_account_quiesced \
    "account-a" \
    "trader-v3-node-a" \
    "8081"
  verify_execution_account_quiesced \
    "account-b" \
    "trader-v3-node-b" \
    "8082"
  verify_execution_account_quiesced \
    "account-c" \
    "trader-v3-node-c" \
    "8083"
  verify_execution_account_quiesced \
    "account-d" \
    "trader-v3-node-d" \
    "8084"
}
all_execution_accounts_stopped() {
  local node
  local running
  for node in "${ALL_NODES[@]}"; do
    if ! running="$(
      docker inspect \
        --format '{{.State.Running}}' \
        "$node" 2>/dev/null
    )"; then
      return 1
    fi
    if [ "$running" != "false" ]; then
      return 1
    fi
  done
}
seal_all_execution_accounts_stopped() {
  local node
  all_execution_accounts_stopped \
    || die "bootstrap stopped sealing requires all execution accounts stopped"
  for node in "${ALL_NODES[@]}"; do
    docker update --restart=no "$node" >/dev/null \
      || die "bootstrap stopped sealing failed for $node"
  done
  verify_all_execution_accounts_stopped
  echo "== bootstrap stopped A-D sealed with restart=no"
}
verify_all_execution_accounts_stopped() {
  local node
  local restart_policy
  local running
  for node in "${ALL_NODES[@]}"; do
    read -r running restart_policy < <(
      docker inspect \
        --format '{{.State.Running}} {{.HostConfig.RestartPolicy.Name}}' \
        "$node"
    ) || die "migration rebaseline cannot inspect $node"
    [ "$running" = "false" ] \
      || die "migration rebaseline requires $node stopped"
    [ "$restart_policy" = "no" ] \
      || die "migration rebaseline requires $node restart=no"
  done
  echo "== migration rebaseline A-D stopped with restart=no"
}
stop_recreate_nodes() {
  local node
  if [ "${#RECREATE_NODES[@]}" -eq 0 ]; then
    return
  fi
  for node in "${RECREATE_NODES[@]}"; do
    docker stop --time 30 "$node" >/dev/null 2>&1 || true
    [ "$(docker inspect --format '{{.State.Running}}' "$node")" = "false" ] \
      || die "$node remained running before shared deployment mutation"
  done
  verify_all_execution_accounts_quiesced
  echo "== recreate set stopped: ${RECREATE_NODES[*]}"
}
node_ready_port() {
  local node="$1"
  case "$node" in
    trader-v3-node-a) echo 8081 ;;
    trader-v3-node-b) echo 8082 ;;
    trader-v3-node-c) echo 8083 ;;
    trader-v3-node-d) echo 8084 ;;
    *) die "unknown rollout node: $node" ;;
  esac
}
verify_node_ready_halted() {
  local node="$1"
  local port
  local state
  local ok=0
  port="$(node_ready_port "$node")"
  for _ in $(seq 1 45); do
    state="$(
      curl -sf "http://127.0.0.1:$port/ready" \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("ready"), d.get("trading_state"))' \
          2>/dev/null \
        || echo bad
    )"
    case "$state" in
      "True HALTED")
        ok=1
        break
        ;;
    esac
    sleep 4
  done
  [ "$ok" = "1" ] || die "$node did not reach ready+HALTED"
  echo "== $node ready (HALTED/startup)"
}
write_node_startup_resource_evidence() {
  python3 - \
    "$NODE_STARTUP_RESOURCE_EVIDENCE" \
    "$1" \
    "$2" \
    "$3" \
    "$4" \
    "$5" \
    "$NODE_STARTUP_MIN_AVAILABLE_BYTES" \
    "$6" \
    "$7" <<'PY'
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

(
    output_raw,
    node,
    memory_limit,
    memory_current,
    memory_peak,
    host_available,
    host_minimum,
    oom_killed,
    restart_count,
) = sys.argv[1:]
output = Path(output_raw)
if output.is_symlink():
    raise SystemExit("node startup resource evidence cannot be a symlink")
payload = {
    "schema_version": "trader-v3-node-startup-resources/v1",
    "captured_at": datetime.now(timezone.utc).isoformat(),
    "node": node,
    "memory_limit_bytes": int(memory_limit),
    "memory_current_bytes": int(memory_current),
    "memory_peak_bytes": int(memory_peak),
    "host_mem_available_bytes": int(host_available),
    "host_min_available_bytes": int(host_minimum),
    "oom_killed": oom_killed == "true",
    "restart_count": int(restart_count),
}
flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(output, flags, 0o600)
try:
    os.fchmod(descriptor, 0o600)
    os.write(
        descriptor,
        (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
    )
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}
verify_node_startup_memory_reserve() {
  local node="$1"
  local available_bytes
  local current_bytes
  local memory_limit_bytes
  local oom_killed
  local peak_bytes
  local restart_count
  read -r oom_killed restart_count memory_limit_bytes < <(
    docker inspect \
      --format '{{.State.OOMKilled}} {{.RestartCount}} {{.HostConfig.Memory}}' \
      "$node"
  )
  current_bytes="$(
    docker exec "$node" cat /sys/fs/cgroup/memory.current 2>/dev/null \
      || docker exec "$node" \
        cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null
  )"
  peak_bytes="$(
    docker exec "$node" cat /sys/fs/cgroup/memory.peak 2>/dev/null \
      || docker exec "$node" \
        cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes 2>/dev/null
  )"
  available_bytes="$(
    awk '/^MemAvailable:/ {printf "%.0f", $2 * 1024}' \
      "$NODE_STARTUP_MEMINFO_PATH"
  )"
  [ "$oom_killed" = "false" ] \
    || die "$node was OOM-killed during startup"
  [ "$restart_count" = "0" ] \
    || die "$node restarted during startup"
  [[ "$memory_limit_bytes" =~ ^[1-9][0-9]*$ ]] \
    || die "$node startup memory limit is invalid"
  [ "$memory_limit_bytes" = "$NODE_RELEASE_MEMORY_LIMIT_BYTES" ] \
    || die "$node startup memory limit differs from release contract"
  [[ "$current_bytes" =~ ^[1-9][0-9]*$ ]] \
    || die "$node startup memory.current is invalid"
  [[ "$peak_bytes" =~ ^[1-9][0-9]*$ ]] \
    || die "$node startup memory.peak is invalid"
  [ "$current_bytes" -le "$peak_bytes" ] \
    || die "$node startup memory.current exceeds memory.peak"
  [ "$peak_bytes" -le "$memory_limit_bytes" ] \
    || die "$node startup memory.peak exceeds its release limit"
  [[ "$available_bytes" =~ ^[0-9]+$ ]] \
    || die "$node startup MemAvailable is unavailable"
  [ "$available_bytes" -ge "$NODE_STARTUP_MIN_AVAILABLE_BYTES" ] \
    || die "$node startup left MemAvailable below 3 GiB"
  write_node_startup_resource_evidence \
    "$node" \
    "$memory_limit_bytes" \
    "$current_bytes" \
    "$peak_bytes" \
    "$available_bytes" \
    "$oom_killed" \
    "$restart_count"
  echo "== $node startup resources verified: memory_current=$current_bytes memory_peak=$peak_bytes memory_limit=$memory_limit_bytes MemAvailable=$available_bytes OOMKilled=false RestartCount=0"
}
finalize_node_startup_resource_evidence() {
  if [ ! -f "$NODE_STARTUP_RESOURCE_EVIDENCE" ]; then
    return
  fi
  chmod 0400 "$NODE_STARTUP_RESOURCE_EVIDENCE"
  refresh_backup_checksums
}
recreate_release_node() {
  local node="$1"
  local port
  verify_maintenance_fence "node-recreate-$node"
  bash "$T/recreate-$node.sh"
  verify_node_ready_halted "$node"
  port="$(node_ready_port "$node")"
  verify_version_endpoint "$port"
  verify_node_startup_memory_reserve "$node"
}
verify_version_endpoint() {
  local port="$1"
  python3 - \
    "$RELEASE_MANIFEST" \
    "$port" \
    "$DATABASE_SCHEMA_EPOCH" <<'PY'
import json
import sys
from urllib.request import urlopen

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
port = int(sys.argv[2])
database_schema_epoch = sys.argv[3]
with urlopen(f"http://127.0.0.1:{port}/version", timeout=5) as response:
    version = json.load(response)
expected = {
    "git_sha": manifest["release_commit"],
    "image_digest": manifest["image_digest"],
    "build_id": manifest["release_id"],
    "dependency_lock_sha256": manifest["dependency_lock_sha256"],
    "config_sha256": manifest["config_sha256"],
    "schema_epoch": database_schema_epoch,
    "complete": True,
}
for key, value in expected.items():
    if version.get(key) != value:
        raise SystemExit(f"/version mismatch: {key}")
if not str(version.get("started_at") or "").strip():
    raise SystemExit("/version missing started_at")
PY
}
verify_release_nodes() {
  local node
  local port
  local verify_containers=()
  [ "$#" -gt 0 ] || die "release verification node set is empty"
  for node in "$@"; do
    verify_node_ready_halted "$node"
    port="$(node_ready_port "$node")"
    verify_version_endpoint "$port"
    verify_containers+=(--container "$node")
  done
  python3 "$RELEASE_TOOL" verify-live \
    --manifest "$RELEASE_MANIFEST" \
    --bundle-manifest "$STAGING/bundle-manifest.json" \
    "${verify_containers[@]}"
}
cleanup() {
  local path
  release_maintenance_fence "deploy-process-exit"
  for path in "${TEMP_FILES[@]:-}"; do
    [ -n "$path" ] || continue
    rm -f "$path"
  done
}
prepare_release_bound_config_source() {
  local source_config="$1"
  local output_config="$2"
  python3 - \
    "$RELEASE_TOOL" \
    "$LIVE_RISK_POLICY" \
    "$source_config" \
    "$output_config" \
    "$BINANCE_EGRESS_MODE" \
    "$BINANCE_PROXY_URL" \
    "$BINANCE_PROXY_URL_A" \
    "$BINANCE_PROXY_URL_B" \
    "$BINANCE_PROXY_URL_C" \
    "$BINANCE_PROXY_URL_D" <<'PY'
import json
import os
import sys
from pathlib import Path

(
    release_tool,
    policy_raw,
    source_raw,
    output_raw,
    egress_mode,
    shared_proxy_url,
    proxy_url_a,
    proxy_url_b,
    proxy_url_c,
    proxy_url_d,
) = sys.argv[1:]
sys.path.insert(0, str(Path(release_tool).resolve().parent))
import release_manifest

policy_path = Path(policy_raw)
source_path = Path(source_raw)
output_path = Path(output_raw)
policy = json.loads(policy_path.read_text(encoding="utf-8"))
source = json.loads(source_path.read_text(encoding="utf-8"))
account_id = source.get("account_id")
account_proxy_urls = dict(
    (
        ("account-a", proxy_url_a),
        ("account-b", proxy_url_b),
        ("account-c", proxy_url_c),
        ("account-d", proxy_url_d),
    )
)
if account_id not in account_proxy_urls:
    raise SystemExit("live node account_id is invalid")
if egress_mode == "route":
    if shared_proxy_url or any(account_proxy_urls.values()):
        raise SystemExit(
            "route egress mode cannot configure a Binance proxy"
        )
    proxy_url = False
elif egress_mode == "proxy":
    if not shared_proxy_url:
        raise SystemExit(
            "proxy egress mode requires a Binance proxy"
        )
    if any(account_proxy_urls.values()):
        raise SystemExit(
            "proxy egress mode cannot configure account Binance proxies"
        )
    proxy_url = shared_proxy_url
elif egress_mode == "account_networks":
    if shared_proxy_url:
        raise SystemExit(
            "account_networks egress mode cannot configure a shared proxy"
        )
    proxy_url = account_proxy_urls[account_id] or False
else:
    raise SystemExit("Binance egress mode is invalid")

resources = release_manifest._validated_runtime_resources(
    policy.get("runtime_resource_contract"),
    label="live risk policy runtime_resource_contract",
)
current = source.get("runtime_resources")
if current is not None:
    validated_current = release_manifest._validated_runtime_resources(
        current,
        label="live node runtime_resources",
    )
    if validated_current != resources:
        raise SystemExit(
            "live node runtime_resources differ from reviewed policy"
        )
updated = dict(source)
binance = source.get("binance")
if not isinstance(binance, dict):
    raise SystemExit("live node binance config is invalid")
existing_proxy = binance.get("proxy_url")
if existing_proxy is not None and existing_proxy != proxy_url:
    raise SystemExit("live node Binance proxy differs from deployment proxy")
updated_binance = dict(binance)
if proxy_url:
    updated_binance["proxy_url"] = proxy_url
else:
    # Route egress uses no proxy; a literal false would be rejected by
    # the node's secret-reference validation, so omit the field.
    updated_binance.pop("proxy_url", None)
updated["binance"] = updated_binance
updated["runtime_resources"] = resources
payload = (
    json.dumps(updated, indent=2, sort_keys=True) + "\n"
).encode("utf-8")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
fd = os.open(output_path, flags, 0o600)
try:
    os.write(fd, payload)
    os.fsync(fd)
finally:
    os.close(fd)
PY
}
load_binance_egress_settings() {
  local settings=()
  local settings_output
  if ! settings_output="$(
    python3 - "$T/.env.v3" <<'PY'
import ipaddress
import sys
from pathlib import Path
from urllib.parse import urlsplit


def read_environment(path):
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key.strip()] = value
    return values


values = read_environment(sys.argv[1])
egress_mode = values.get("BINANCE_EGRESS_MODE", "").strip()
proxy_url = values.get("BINANCE_PROXY_URL", "").strip()
account_proxy_urls = [
    values.get(f"BINANCE_PROXY_URL_{label}", "").strip()
    for label in ("A", "B", "C", "D")
]
expected_egress = values.get(
    "BINANCE_EXPECTED_EGRESS_IP",
    "",
).strip()
account_expected = [
    values.get(f"BINANCE_EXPECTED_EGRESS_IP_{label}", "").strip()
    for label in ("A", "B", "C", "D")
]
if egress_mode not in {"route", "proxy", "account_networks"}:
    raise SystemExit(
        "BINANCE_EGRESS_MODE must be route, proxy, or account_networks"
    )


def validate_proxy_url(raw_value, label):
    if not raw_value:
        return ""
    parsed_proxy = urlsplit(raw_value)
    if (
        parsed_proxy.scheme not in {"http", "https"}
        or not parsed_proxy.hostname
    ):
        raise SystemExit(
            f"{label} must be an http(s) URL"
        )
    if (
        parsed_proxy.username is not None
        or parsed_proxy.password is not None
    ):
        if label == "BINANCE_PROXY_URL":
            raise SystemExit(
                "BINANCE_PROXY_URL must not contain credentials"
            )
        raise SystemExit(
            f"{label} must not contain credentials"
        )
    try:
        parsed_proxy.port
    except ValueError as exc:
        raise SystemExit(f"{label} contains an invalid port") from exc
    return raw_value


proxy_url = validate_proxy_url(proxy_url, "BINANCE_PROXY_URL")
normalized_account_proxies = [
    validate_proxy_url(raw_value, f"BINANCE_PROXY_URL_{label}")
    for label, raw_value in zip(
        ("A", "B", "C", "D"),
        account_proxy_urls,
    )
]
if egress_mode == "route":
    if proxy_url:
        raise SystemExit(
            "BINANCE_PROXY_URL must be empty in route mode"
        )
    if any(normalized_account_proxies):
        raise SystemExit(
            "route mode must not configure Binance proxies"
        )
if egress_mode == "proxy":
    if not proxy_url:
        raise SystemExit(
            "BINANCE_PROXY_URL must be an http(s) URL"
        )
    if any(normalized_account_proxies):
        raise SystemExit(
            "proxy mode must not configure account Binance proxies"
        )
if egress_mode == "account_networks":
    if proxy_url:
        raise SystemExit(
            "BINANCE_PROXY_URL must be empty in account_networks mode"
        )
    if any(normalized_account_proxies[:3]):
        raise SystemExit(
            "account A, B, and C Binance proxies must be empty"
        )
    if not normalized_account_proxies[3]:
        raise SystemExit(
            "BINANCE_PROXY_URL_D must be an http(s) URL"
        )
try:
    expected_ip = ipaddress.ip_address(expected_egress)
except ValueError as exc:
    raise SystemExit(
        "BINANCE_EXPECTED_EGRESS_IP must be an IP address"
    ) from exc
if expected_ip.version != 4:
    raise SystemExit(
        "BINANCE_EXPECTED_EGRESS_IP must be IPv4"
    )
normalized_account_expected = []
for label, raw_value in zip(("A", "B", "C", "D"), account_expected):
    if egress_mode != "account_networks":
        normalized_account_expected.append("")
        continue
    try:
        account_ip = ipaddress.ip_address(raw_value)
    except ValueError as exc:
        raise SystemExit(
            f"BINANCE_EXPECTED_EGRESS_IP_{label} must be an IP address"
        ) from exc
    if account_ip.version != 4:
        raise SystemExit(
            f"BINANCE_EXPECTED_EGRESS_IP_{label} must be IPv4"
        )
    normalized_account_expected.append(str(account_ip))
if egress_mode == "account_networks":
    account_a, account_b, account_c, account_d = (
        normalized_account_expected
    )
    if str(expected_ip) != account_a:
        raise SystemExit(
            "BINANCE_EXPECTED_EGRESS_IP must equal account A egress"
        )
    if len({account_a, account_b, account_c}) != 3:
        raise SystemExit(
            "account A, B, and C egress IPs must be distinct"
        )
    if len({account_a, account_b, account_c, account_d}) != 4:
        raise SystemExit(
            "account A through D egress IPs must be distinct"
        )
print(f"mode={egress_mode}")
print(f"proxy={proxy_url}")
for label, value in zip(
    ("a", "b", "c", "d"),
    normalized_account_proxies,
):
    print(f"proxy_{label}={value}")
print(f"expected={expected_ip}")
for label, value in zip(
    ("a", "b", "c", "d"),
    normalized_account_expected,
):
    print(f"expected_{label}={value}")
PY
  )"; then
    die "Binance JP egress settings are invalid"
  fi
  while IFS= read -r setting; do
    settings+=("${setting#*=}")
  done <<<"$settings_output"
  [ "${#settings[@]}" -eq 11 ] \
    || die "Binance JP egress settings are incomplete"
  BINANCE_EGRESS_MODE="${settings[0]}"
  BINANCE_PROXY_URL="${settings[1]}"
  BINANCE_PROXY_URL_A="${settings[2]}"
  BINANCE_PROXY_URL_B="${settings[3]}"
  BINANCE_PROXY_URL_C="${settings[4]}"
  BINANCE_PROXY_URL_D="${settings[5]}"
  BINANCE_EXPECTED_EGRESS_IP="${settings[6]}"
  BINANCE_EXPECTED_EGRESS_IP_A="${settings[7]}"
  BINANCE_EXPECTED_EGRESS_IP_B="${settings[8]}"
  BINANCE_EXPECTED_EGRESS_IP_C="${settings[9]}"
  BINANCE_EXPECTED_EGRESS_IP_D="${settings[10]}"
}
load_binance_proxy_settings() {
  load_binance_egress_settings
  [ "$BINANCE_EGRESS_MODE" = "proxy" ] \
    || die "Binance JP egress mode is not proxy"
}
verify_binance_proxy_egress() {
  local actual_egress
  actual_egress="$(
    curl \
      --fail \
      --silent \
      --show-error \
      --max-time 15 \
      --proxy "$BINANCE_PROXY_URL" \
      https://api.ipify.org
  )" || die "Binance JP proxy egress probe failed"
  [ "$actual_egress" = "$BINANCE_EXPECTED_EGRESS_IP" ] \
    || die "Binance proxy egress differs from expected JP IP"
  echo "== Binance proxy egress verified: $actual_egress"
}
verify_binance_route_egress() {
  local actual_egress
  local address
  local dns_records
  local fapi_http_code
  local link_flags
  local link_state
  local route_result
  local route_addresses=()
  [ "$BINANCE_ROUTE_INTERFACE" = "wg0" ] \
    || die "Binance route interface must be wg0"
  command -v curl >/dev/null 2>&1 \
    || die "curl is required for Binance route verification"
  command -v getent >/dev/null 2>&1 \
    || die "getent is required for Binance route verification"
  command -v ip >/dev/null 2>&1 \
    || die "ip is required for Binance route verification"
  link_state="$(
    ip -o link show dev "$BINANCE_ROUTE_INTERFACE" 2>/dev/null
  )" || die "Binance route interface wg0 is unavailable"
  link_flags="${link_state#*<}"
  [ "$link_flags" != "$link_state" ] \
    || die "Binance route interface wg0 flags are unavailable"
  link_flags="${link_flags%%>*}"
  case ",$link_flags," in
    *,UP,*)
      ;;
    *)
      die "Binance route interface wg0 is down"
      ;;
  esac
  actual_egress="$(
    curl \
      --fail \
      --silent \
      --show-error \
      --max-time 15 \
      --interface "$BINANCE_ROUTE_INTERFACE" \
      https://api.ipify.org
  )" || die "Binance JP route egress probe failed"
  [ "$actual_egress" = "$BINANCE_EXPECTED_EGRESS_IP" ] \
    || die "Binance route egress differs from expected JP IP"
  dns_records="$(
    getent ahostsv4 fapi.binance.com
  )" || die "fapi.binance.com IPv4 resolution failed"
  while IFS= read -r address; do
    route_addresses+=("$address")
  done < <(
    printf '%s\n' "$dns_records" \
      | awk 'NF {print $1}' \
      | LC_ALL=C sort -u
  )
  [ "${#route_addresses[@]}" -gt 0 ] \
    || die "fapi.binance.com has no current IPv4 addresses"
  for address in "${route_addresses[@]}"; do
    if [[ ! "$address" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
      die "fapi.binance.com returned an invalid IPv4 address"
    fi
    route_result="$(
      ip -4 route get "$address"
    )" || die "route lookup failed for fapi.binance.com IPv4 address"
    case " $route_result " in
      *" dev $BINANCE_ROUTE_INTERFACE "*)
        ;;
      *)
        die "fapi.binance.com IPv4 route does not use wg0"
        ;;
    esac
  done
  fapi_http_code="$(
    curl \
      --silent \
      --show-error \
      --max-time 15 \
      --output /dev/null \
      --write-out '%{http_code}' \
      --interface "$BINANCE_ROUTE_INTERFACE" \
      https://fapi.binance.com/fapi/v1/time
  )" || die "Binance FAPI route probe failed"
  [ "$fapi_http_code" = "200" ] \
    || die "Binance FAPI route probe did not return HTTP 200"
  echo "== Binance route egress verified via wg0: $actual_egress"
}
probe_existing_node_egress() {
  local node="$1"
  local network="$2"
  local proxy_url="$3"
  local image
  local running
  local probe=()
  running="$(
    docker inspect --format '{{.State.Running}}' "$node"
  )" || die "Binance node running-state inspection failed: $node"
  if [ "$running" = "true" ]; then
    probe=(docker exec -i "$node" python3 - "$proxy_url")
  elif [ "$running" = "false" ]; then
    image="$(
      docker inspect --format '{{.Image}}' "$node"
    )" || die "Binance node image inspection failed: $node"
    [ -n "$image" ] \
      || die "Binance stopped-node image is empty: $node"
    probe=(
      docker run
      --rm
      -i
      --network "$network"
      --entrypoint python3
      "$image"
      -
      "$proxy_url"
    )
  else
    die "Binance node running state is invalid: $node"
  fi
  "${probe[@]}" <<'PY'
import sys
import urllib.error
import urllib.request

proxy_url = sys.argv[1]
proxy_config = {}
if proxy_url:
    proxy_config = {
        "http": proxy_url,
        "https": proxy_url,
    }
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler(proxy_config)
)
with opener.open("https://api.ipify.org", timeout=15) as response:
    actual_egress = response.read().decode("ascii").strip()
retry_after = ""
try:
    with opener.open(
        "https://fapi.binance.com/fapi/v1/time",
        timeout=15,
    ) as response:
        fapi_http_code = response.status
except urllib.error.HTTPError as exc:
    if exc.code != 429:
        raise
    fapi_http_code = exc.code
    retry_after = str(exc.headers.get("Retry-After") or "").strip()
print(f"{actual_egress}\t{fapi_http_code}\t{retry_after}")
PY
}
verify_binance_account_network_egress() {
  local actual_egress
  local attempt
  local attempts
  local delay_seconds
  local expected_egress
  local fapi_http_code
  local index
  local max_retry_after_seconds
  local network
  local network_names
  local node
  local probe_output
  local proxy_url
  local retry_after
  local retry_after_seconds
  attempts="${BINANCE_ACCOUNT_NETWORK_PROBE_ATTEMPTS:-6}"
  max_retry_after_seconds="${BINANCE_ACCOUNT_NETWORK_PROBE_MAX_RETRY_AFTER_SECONDS:-60}"
  [[ "$attempts" =~ ^[1-9][0-9]{0,2}$ ]] \
    || die "BINANCE_ACCOUNT_NETWORK_PROBE_ATTEMPTS must be 1 through 999"
  [[ "$max_retry_after_seconds" =~ ^(0|[1-9][0-9]{0,3})$ ]] \
    || die "BINANCE_ACCOUNT_NETWORK_PROBE_MAX_RETRY_AFTER_SECONDS must be 0 through 9999"
  local expected_egress_ips=(
    "$BINANCE_EXPECTED_EGRESS_IP_A"
    "$BINANCE_EXPECTED_EGRESS_IP_B"
    "$BINANCE_EXPECTED_EGRESS_IP_C"
    "$BINANCE_EXPECTED_EGRESS_IP_D"
  )
  local proxy_urls=(
    "$BINANCE_PROXY_URL_A"
    "$BINANCE_PROXY_URL_B"
    "$BINANCE_PROXY_URL_C"
    "$BINANCE_PROXY_URL_D"
  )
  for index in "${!BINANCE_ACCOUNT_NETWORKS[@]}"; do
    network="${BINANCE_ACCOUNT_NETWORKS[$index]}"
    node="${ALL_NODES[$index]}"
    expected_egress="${expected_egress_ips[$index]}"
    proxy_url="${proxy_urls[$index]}"
    docker network inspect "$network" >/dev/null \
      || die "Binance account network is unavailable: $network"
    network_names="$(
      docker inspect "$node" \
        | python3 -c \
          'import json,sys; print("\n".join(sorted((json.load(sys.stdin)[0].get("NetworkSettings") or {}).get("Networks") or {})))'
    )" || die "Binance node network inspection failed: $node"
    [ "$network_names" = "$network" ] \
      || die "Binance node network differs from account allocation: $node"
    for ((attempt = 1; attempt <= attempts; attempt++)); do
      probe_output="$(
        probe_existing_node_egress "$node" "$network" "$proxy_url"
      )" || die "Binance account-network probe failed: $network"
      IFS=$'\t' read -r actual_egress fapi_http_code retry_after \
        <<<"$probe_output"
      [ "$actual_egress" = "$expected_egress" ] \
        || die "Binance account-network egress differs: $network"
      if [ "$fapi_http_code" = "200" ]; then
        break
      fi
      [ "$fapi_http_code" = "429" ] \
        || die "Binance account-network FAPI probe did not return HTTP 200: $network"
      [ "$attempt" -lt "$attempts" ] \
        || die "Binance account-network FAPI probe exhausted HTTP 429 retries: $network"
      [[ "$retry_after" =~ ^[0-9]{1,6}$ ]] \
        || die "Binance account-network HTTP 429 Retry-After is invalid: $network"
      retry_after_seconds=$((10#$retry_after))
      delay_seconds="$retry_after_seconds"
      if [ "$delay_seconds" -gt "$max_retry_after_seconds" ]; then
        delay_seconds="$max_retry_after_seconds"
      fi
      printf '== Binance account network rate limited: %s network=%s attempt=%s/%s retry_after=%ss sleep=%ss\n' \
        "$node" \
        "$network" \
        "$attempt" \
        "$attempts" \
        "$retry_after_seconds" \
        "$delay_seconds" >&2
      sleep "$delay_seconds"
    done
    printf '== Binance account network verified: %s network=%s egress=%s proxy=%s\n' \
      "$node" \
      "$network" \
      "$actual_egress" \
      "${proxy_url:-direct}"
  done
}
verify_binance_egress() {
  case "$BINANCE_EGRESS_MODE" in
    proxy)
      verify_binance_proxy_egress
      ;;
    route)
      verify_binance_route_egress
      ;;
    account_networks)
      verify_binance_account_network_egress
      ;;
    *)
      die "Binance egress mode is invalid"
      ;;
  esac
}
require_rollout_phase() {
  local expected_phase="$1"
  local status_file
  status_file="$(mktemp)"
  TEMP_FILES+=("$status_file")
  run_reviewed_rollout status \
    --release-id "$RELEASE_ID" >"$status_file"
  python3 - "$status_file" "$expected_phase" <<'PY'
import json
import sys

status = json.load(open(sys.argv[1], encoding="utf-8"))
expected_phase = sys.argv[2]
if status.get("phase") != expected_phase:
    raise SystemExit(
        "reviewed rollout phase mismatch: "
        f"expected={expected_phase} actual={status.get('phase')}"
    )
PY
}
capture_pre_migration_backup_expectation() {
  local output="$1"
  local postgres_dump="$2"
  local postgres_dump_sha256="$3"
  local postgres_restore_list="$4"
  local old_images="$5"
  "$T/.venv-cp/bin/python" - \
    "$T/RELEASE_MANIFEST.json" \
    "$ROLLOUT_ACCOUNT" \
    "$ROLLOUT_NODE" \
    "$T/recreate-$ROLLOUT_NODE.sh" \
    "$postgres_dump" \
    "$postgres_dump_sha256" \
    "$postgres_restore_list" \
    "$old_images" \
    "$T" \
    "$output" \
    "$DEPLOY_GATE_MODE" \
    "${RECREATE_NODES[@]}" <<'PY'
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_durable_json(path, payload):
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o400)
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(fd, encoded[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


(
    manifest_raw,
    account_id,
    container,
    recreate_raw,
    postgres_dump_raw,
    postgres_dump_sha256_raw,
    postgres_restore_list_raw,
    old_images_raw,
    trader_root_raw,
    output_raw,
    gate_mode_raw,
    *recreate_nodes,
) = sys.argv[1:]
manifest_path = Path(manifest_raw)
recreate_path = Path(recreate_raw)
postgres_dump_path = Path(postgres_dump_raw)
postgres_dump_sha256_path = Path(postgres_dump_sha256_raw)
postgres_restore_list_path = Path(postgres_restore_list_raw)
old_images_path = Path(old_images_raw)
trader_root = Path(trader_root_raw)
output_path = Path(output_raw)
# Bootstrapping over the legacy topology has no previous release of this
# lineage: no live release manifest, no release labels, and recreate
# scripts only for nodes the legacy flow managed. Record what exists
# instead of failing, but only in the audited bootstrap gate modes.
bootstrap = gate_mode_raw in {
    "bootstrap_stopped",
    "bootstrap_resume_stopped",
    "migration_rebaseline_stopped",
}
required_files = [
    postgres_dump_path,
    postgres_dump_sha256_path,
    postgres_restore_list_path,
    old_images_path,
]
if not bootstrap:
    required_files = [manifest_path, recreate_path, *required_files]
for path in required_files:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"pre-migration backup artifact is invalid: {path}")
if bootstrap:
    for path in (manifest_path, recreate_path):
        if path.exists() and (not path.is_file() or path.is_symlink()):
            raise SystemExit(
                f"pre-migration backup artifact is invalid: {path}"
            )
if not recreate_nodes:
    raise SystemExit("pre-migration backup expectation node set is empty")
if len(recreate_nodes) != len(set(recreate_nodes)):
    raise SystemExit("pre-migration backup expectation node set is duplicated")
old_images = {}
for raw_line in old_images_path.read_text(encoding="utf-8").splitlines():
    node, separator, image_digest = raw_line.partition("\t")
    if not separator or not node or not image_digest:
        raise SystemExit("old image record is invalid")
    if node in old_images:
        raise SystemExit(f"old image record is duplicated: {node}")
    old_images[node] = image_digest
if set(old_images) != set(recreate_nodes):
    raise SystemExit("old image record differs from recreate node exact-set")
identity = None
if manifest_path.is_file():
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    identity_fields = (
        "release_id",
        "image_digest",
        "config_sha256",
        "dependency_lock_sha256",
    )
    identity = {}
    for field in identity_fields:
        value = str(manifest.get(field) or "").strip()
        if not value:
            raise SystemExit(f"previous release manifest lacks {field}")
        identity[field] = value
elif not bootstrap:
    raise SystemExit(
        f"pre-migration backup artifact is invalid: {manifest_path}"
    )
inspected = json.loads(
    subprocess.check_output(
        ["docker", "inspect", container],
        text=True,
    )
)[0]
previous_image_digest = str(inspected.get("Image") or "").strip()
if identity is not None:
    if previous_image_digest != identity["image_digest"]:
        raise SystemExit(
            "previous container image differs from release manifest"
        )
    labels = inspected.get("Config", {}).get("Labels", {})
    expected_labels = {
        "com.trader.release.id": identity["release_id"],
        "com.trader.release.image-digest": identity["image_digest"],
        "com.trader.release.config-sha256": identity["config_sha256"],
    }
    for key, expected in expected_labels.items():
        if labels.get(key) != expected:
            raise SystemExit(
                f"pre-migration container label mismatch: {key}"
            )
previous_nodes = []
for node in recreate_nodes:
    inspected_node = json.loads(
        subprocess.check_output(
            ["docker", "inspect", node],
            text=True,
        )
    )[0]
    inspected_image = str(inspected_node.get("Image") or "").strip()
    if inspected_image != old_images[node]:
        raise SystemExit(f"old image record differs from container: {node}")
    node_recreate_path = trader_root / f"recreate-{node}.sh"
    node_recreate_present = (
        node_recreate_path.is_file()
        and not node_recreate_path.is_symlink()
    )
    if not node_recreate_present and not bootstrap:
        raise SystemExit(f"pre-migration recreate artifact is invalid: {node}")
    previous_nodes.append(
        {
            "container": node,
            "previous_image_digest": inspected_image,
            "previous_recreate_path": (
                str(node_recreate_path)
                if node_recreate_present
                else None
            ),
            "previous_recreate_sha256": (
                sha256_file(node_recreate_path)
                if node_recreate_present
                else None
            ),
        }
    )
hash_record = postgres_dump_sha256_path.read_text(
    encoding="utf-8"
).strip().split()
if len(hash_record) != 2:
    raise SystemExit("PostgreSQL backup sha256 record is invalid")
recorded_dump_sha256 = hash_record[0]
if re.fullmatch(r"[0-9a-f]{64}", recorded_dump_sha256) is None:
    raise SystemExit("PostgreSQL backup sha256 is invalid")
actual_dump_sha256 = sha256_file(postgres_dump_path)
if actual_dump_sha256 != recorded_dump_sha256:
    raise SystemExit("PostgreSQL backup sha256 mismatch")
restore_listing = postgres_restore_list_path.read_text(encoding="utf-8")
if " TABLE public node_heartbeats " not in restore_listing:
    raise SystemExit("PostgreSQL restore listing lacks node_heartbeats")
payload = {
    "schema_version": (
        "trader-v3-pre-migration-backup-expectation/v2"
    ),
    "account_id": account_id,
    "container": container,
    "bootstrap_without_previous_release": identity is None,
    "previous_manifest_identity": identity,
    "previous_manifest_sha256": (
        sha256_file(manifest_path)
        if manifest_path.is_file()
        else None
    ),
    "previous_image_digest": previous_image_digest,
    "previous_recreate_sha256": (
        sha256_file(recreate_path)
        if recreate_path.is_file() and not recreate_path.is_symlink()
        else None
    ),
    "previous_nodes": previous_nodes,
    "old_images_sha256": sha256_file(old_images_path),
    "postgres_dump_path": str(postgres_dump_path),
    "postgres_dump_sha256": actual_dump_sha256,
    "postgres_restore_list_sha256": sha256_file(
        postgres_restore_list_path
    ),
}
write_durable_json(output_path, payload)
PY
}
capture_post_migration_recovery_expectation() {
  local output="$1"
  local recovery_manifest="$2"
  local pre_migration_expectation="$3"
  local recovery_script="$4"
  local recovery_env="$5"
  local recovery_docker="$6"
  local target_node="${7:-$ROLLOUT_NODE}"
  local target_account
  local target_port
  case "$target_node" in
    trader-v3-node-a)
      target_account="account-a"
      target_port="8081"
      ;;
    trader-v3-node-b)
      target_account="account-b"
      target_port="8082"
      ;;
    trader-v3-node-c)
      target_account="account-c"
      target_port="8083"
      ;;
    trader-v3-node-d)
      target_account="account-d"
      target_port="8084"
      ;;
    *)
      die "unknown recovery target node: $target_node"
      ;;
  esac
  "$T/.venv-cp/bin/python" - \
    "$recovery_manifest" \
    "$pre_migration_expectation" \
    "$recovery_script" \
    "$recovery_env" \
    "$recovery_docker" \
    "$target_account" \
    "$target_node" \
    "$target_port" \
    "$DATABASE_SCHEMA_EPOCH" \
    "$REDIS_SCHEMA_EPOCH" \
    "$REDIS_FENCING_EPOCH_KEY" \
    "$output" <<'PY'
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import UUID


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_durable_json(path, payload):
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o400)
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(fd, encoded[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


(
    recovery_manifest_raw,
    pre_migration_expectation_raw,
    recovery_script_raw,
    recovery_env_raw,
    recovery_docker_raw,
    account_id,
    container,
    port_raw,
    database_schema_epoch,
    redis_schema_epoch,
    redis_epoch_key,
    output_raw,
) = sys.argv[1:]
recovery_manifest_path = Path(recovery_manifest_raw)
pre_migration_expectation_path = Path(pre_migration_expectation_raw)
recovery_script_path = Path(recovery_script_raw)
recovery_env_path = Path(recovery_env_raw)
recovery_docker_path = Path(recovery_docker_raw)
output_path = Path(output_raw)
for path in (
    recovery_manifest_path,
    pre_migration_expectation_path,
    recovery_script_path,
    recovery_env_path,
    recovery_docker_path,
):
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"post-migration recovery artifact is invalid: {path}")
pre_migration_expectation = json.loads(
    pre_migration_expectation_path.read_text(encoding="utf-8")
)
if pre_migration_expectation.get("schema_version") != (
    "trader-v3-pre-migration-backup-expectation/v2"
):
    raise SystemExit("pre-migration backup expectation schema mismatch")
manifest = json.loads(
    recovery_manifest_path.read_text(encoding="utf-8")
)
identity_fields = (
    "release_id",
    "image_digest",
    "config_sha256",
    "dependency_lock_sha256",
)
identity = {}
for field in identity_fields:
    value = str(manifest.get(field) or "").strip()
    if not value:
        raise SystemExit(f"recovery release manifest lacks {field}")
    identity[field] = value
recovery_image_digest = identity["image_digest"]
if re.fullmatch(r"sha256:[0-9a-f]{64}", recovery_image_digest) is None:
    raise SystemExit("recovery image digest is invalid")
subprocess.check_call(
    ["docker", "image", "inspect", recovery_image_digest],
    stdout=subprocess.DEVNULL,
)
redis_fencing_epoch = subprocess.check_output(
    [
        "docker",
        "exec",
        "trader-v3-redis",
        "redis-cli",
        "--raw",
        "GET",
        redis_epoch_key,
    ],
    text=True,
).strip()
try:
    UUID(redis_fencing_epoch)
except ValueError as exc:
    raise SystemExit("post-migration Redis fencing epoch is invalid") from exc
if database_schema_epoch != "0018_projection_reliability":
    raise SystemExit("post-migration database schema epoch is invalid")
if redis_schema_epoch != "fenced-generation-namespace/v2":
    raise SystemExit("post-migration Redis schema epoch is invalid")
payload = {
    "schema_version": (
        "trader-v3-post-migration-recovery-expectation/v1"
    ),
    "account_id": account_id,
    "container": container,
    "port": int(port_raw),
    "manifest_identity": identity,
    "recovery_manifest_path": str(recovery_manifest_path),
    "recovery_manifest_sha256": sha256_file(recovery_manifest_path),
    "recovery_image_digest": recovery_image_digest,
    "recovery_script_path": str(recovery_script_path),
    "recovery_script_sha256": sha256_file(recovery_script_path),
    "recovery_env_path": str(recovery_env_path),
    "recovery_env_sha256": sha256_file(recovery_env_path),
    "recovery_docker_path": str(recovery_docker_path),
    "recovery_docker_sha256": sha256_file(recovery_docker_path),
    "database_schema_epoch": database_schema_epoch,
    "redis_schema_epoch": redis_schema_epoch,
    "redis_fencing_epoch": redis_fencing_epoch,
    "pre_migration_expectation_sha256": sha256_file(
        pre_migration_expectation_path
    ),
    "expected_trading_state": "HALTED",
}
write_durable_json(output_path, payload)
PY
}
verify_post_migration_recovery() {
  local expectation="$1"
  "$T/.venv-cp/bin/python" - \
    "$T/.env.v3" \
    "$expectation" \
    "$REDIS_FENCING_EPOCH_KEY" <<'PY'
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import psycopg2


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_environment(path):
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key.strip()] = value
    return values


env_path, expectation_raw, redis_epoch_key = sys.argv[1:]
expectation = json.load(open(expectation_raw, encoding="utf-8"))
if expectation.get("schema_version") != (
    "trader-v3-post-migration-recovery-expectation/v1"
):
    raise SystemExit("post-migration recovery expectation schema mismatch")
account_id = expectation["account_id"]
container = expectation["container"]
port = int(expectation["port"])
identity = expectation["manifest_identity"]
recovery_manifest_path = Path(expectation["recovery_manifest_path"])
if not recovery_manifest_path.is_file() or recovery_manifest_path.is_symlink():
    raise SystemExit("post-migration recovery manifest is invalid")
if sha256_file(recovery_manifest_path) != expectation[
    "recovery_manifest_sha256"
]:
    raise SystemExit("post-migration recovery manifest hash mismatch")
manifest = json.loads(
    recovery_manifest_path.read_text(encoding="utf-8")
)
for key, expected in identity.items():
    if manifest.get(key) != expected:
        raise SystemExit(f"post-migration recovery manifest mismatch: {key}")
deadline = time.monotonic() + 90
ready = {}
while time.monotonic() < deadline:
    try:
        ready = json.load(
            urlopen(
                f"http://127.0.0.1:{port}/ready",
                timeout=5,
            )
        )
    except (OSError, URLError, ValueError):
        time.sleep(2)
        continue
    if (
        ready.get("ready") is True
        and ready.get("trading_state") == "HALTED"
        and ready.get("reconciliation_status") == "healthy"
        and ready.get("reconciliation_proof_fresh") is True
    ):
        break
    time.sleep(2)
else:
    raise SystemExit(
        "post-migration recovery failed HALTED/readiness/reconciliation gate"
    )
inspected = json.loads(
    subprocess.check_output(
        ["docker", "inspect", container],
        text=True,
    )
)[0]
if inspected.get("Image") != expectation["recovery_image_digest"]:
    raise SystemExit("post-migration recovery image digest mismatch")
labels = inspected.get("Config", {}).get("Labels", {})
expected_labels = {
    "com.trader.release.id": identity["release_id"],
    "com.trader.release.image-digest": identity["image_digest"],
    "com.trader.release.config-sha256": identity["config_sha256"],
}
for key, expected in expected_labels.items():
    if labels.get(key) != expected:
        raise SystemExit(
            f"post-migration recovery label mismatch: {key}"
        )
redis_epoch = subprocess.check_output(
    [
        "docker",
        "exec",
        "trader-v3-redis",
        "redis-cli",
        "--raw",
        "GET",
        redis_epoch_key,
    ],
    text=True,
).strip()
if redis_epoch != expectation["redis_fencing_epoch"]:
    raise SystemExit("post-migration recovery Redis fencing epoch mismatch")
database_url = read_environment(env_path).get("DATABASE_URL", "")
if not database_url:
    raise SystemExit("DATABASE_URL is missing from control-plane environment")
with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
    cur.execute(
        """
        SELECT node_id,
               status,
               release_id,
               image_digest,
               config_sha256,
               dependency_lock_sha256,
               schema_epoch,
               redis_fencing_epoch::text,
               runtime_generation,
               lease_fencing_token,
               heartbeat_sequence
        FROM node_heartbeats
        WHERE account_id=%s
          AND last_seen_at >= now() - interval '15 seconds'
        ORDER BY last_seen_at DESC
        """,
        (account_id,),
    )
    rows = cur.fetchall()
    cur.execute(
        """
        SELECT version, name
        FROM schema_migrations
        WHERE version = ANY(%s)
        """,
        (["0010", "0011", "0012", "0013", "0014", "0015"],),
    )
    applied_migrations = dict(cur.fetchall())
if len(rows) != 1:
    raise SystemExit(
        "post-migration recovery requires one fresh heartbeat writer"
    )
if applied_migrations != {
    "0010": "evidence_and_poll_indexes",
    "0011": "live_safety",
    "0012": "control_plane_maintenance_fence",
    "0013": "four_account_rollout",
    "0014": "cancel_order_contract",
    "0015": "refresh_evidence_command",
}:
    raise SystemExit(
        "post-migration recovery lacks required migrations"
    )
row = rows[0]
actual_identity = (
    str(row[2] or ""),
    str(row[3] or ""),
    str(row[4] or ""),
    str(row[5] or ""),
    str(row[6] or ""),
    str(row[7] or ""),
)
expected_identity = (
    identity["release_id"],
    identity["image_digest"],
    identity["config_sha256"],
    identity["dependency_lock_sha256"],
    expectation["database_schema_epoch"],
    expectation["redis_fencing_epoch"],
)
if actual_identity != expected_identity:
    raise SystemExit(
        "post-migration recovery heartbeat release identity mismatch"
    )
if str(row[1] or "").upper() != expectation["expected_trading_state"]:
    raise SystemExit("post-migration recovery heartbeat must remain HALTED")
runtime_generation = str(row[8] or "")
lease_fencing_token = int(row[9] or 0)
heartbeat_sequence = int(row[10] or 0)
if (
    not runtime_generation
    or lease_fencing_token <= 0
    or heartbeat_sequence <= 0
):
    raise SystemExit(
        "post-migration recovery heartbeat writer identity is invalid"
    )
PY
}
run_reviewed_rollout() {
  if [ "${1:-}" != "status" ]; then
    verify_maintenance_fence "reviewed-rollout-${1:-mutation}"
  fi
  "$T/.venv-cp/bin/python" - \
    "$T/.env.v3" \
    "$REVIEWED_ROLLOUT_TOOL" \
    "$@" <<'PY'
import os
import sys
from pathlib import Path


def read_environment(path):
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key.strip()] = value
    return values


env = os.environ.copy()
database_url = read_environment(sys.argv[1]).get("DATABASE_URL", "")
if not database_url:
    raise SystemExit("DATABASE_URL is missing from control-plane environment")
env["DATABASE_URL"] = database_url
tool = sys.argv[2]
arguments = [sys.executable, tool, *sys.argv[3:]]
os.execve(sys.executable, arguments, env)
PY
}
ensure_bootstrap_rollout_registration() {
  case "$DEPLOY_GATE_MODE" in
    bootstrap_stopped|bootstrap_resume_stopped|migration_rebaseline_stopped)
      ;;
    *)
      return
      ;;
  esac
  if [ "$BOOTSTRAP_REGISTRATION_COMPLETED" = "1" ]; then
    return
  fi
  if [ "$DEPLOY_GATE_MODE" = "migration_rebaseline_stopped" ]; then
    if ! run_reviewed_rollout migration-rebaseline-register \
      --manifest "$RELEASE_MANIFEST" \
      --bundle-manifest "$STAGING/bundle-manifest.json" \
      --capacity-evidence "$REDIS_CAPACITY_EVIDENCE" \
      --reviewed-by "$ROLLOUT_REVIEWED_BY" \
      --idempotency-key "migration-rebaseline-register:$RELEASE_ID"; then
      return 1
    fi
  else
    if ! run_reviewed_rollout bootstrap-register \
      --manifest "$RELEASE_MANIFEST" \
      --bundle-manifest "$STAGING/bundle-manifest.json" \
      --capacity-evidence "$REDIS_CAPACITY_EVIDENCE" \
      --reviewed-by "$ROLLOUT_REVIEWED_BY" \
      --idempotency-key "bootstrap-register:$RELEASE_ID"; then
      return 1
    fi
  fi
  BOOTSTRAP_REGISTRATION_COMPLETED=1
  ROLLOUT_TRACKED=1
  echo "== stopped-gate reviewed rollout registered in account_a_canary"
}
write_bootstrap_recovery_blocked_evidence() {
  local reason="$1"
  local registration_key="bootstrap-register:${RELEASE_ID:-unknown}"
  if [ "$DEPLOY_GATE_MODE" = "migration_rebaseline_stopped" ]; then
    registration_key="migration-rebaseline-register:${RELEASE_ID:-unknown}"
  fi
  if ! python3 - \
      "$BOOTSTRAP_RECOVERY_BLOCKED_EVIDENCE" \
      "$reason" \
      "${RELEASE_ID:-}" \
      "${BOOTSTRAP_REDIS_FENCING_EPOCH:-}" \
      "$MIGRATION_COMMIT_MARKER" \
      "$registration_key" \
      "${RECREATE_NODES[@]}" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

(
    output_raw,
    reason,
    release_id,
    redis_fencing_epoch,
    migration_marker_raw,
    idempotency_key,
    *nodes,
) = sys.argv[1:]
output = Path(output_raw)
payload = {
    "schema_version": "trader-v3-bootstrap-recovery-blocked/v1",
    "recorded_at": datetime.now(timezone.utc).isoformat(),
    "reason": reason,
    "release_id": release_id,
    "redis_fencing_epoch": redis_fencing_epoch,
    "migration_commit_marker": migration_marker_raw,
    "bootstrap_registration_idempotency_key": idempotency_key,
    "nodes": nodes,
    "required_state": "A-D stopped",
    "required_action": (
        "complete the idempotent bootstrap registration before starting A-D"
    ),
}
encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
temporary = output.with_name(f".{output.name}.new")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
descriptor = os.open(temporary, flags, 0o400)
try:
    offset = 0
    while offset < len(encoded):
        offset += os.write(descriptor, encoded[offset:])
    os.fsync(descriptor)
finally:
    os.close(descriptor)
os.replace(temporary, output)
directory_descriptor = os.open(output.parent, os.O_RDONLY)
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
PY
  then
    echo "!! bootstrap recovery evidence write FAILED" >&2
    return 1
  fi
  echo "!! bootstrap recovery evidence: $BOOTSTRAP_RECOVERY_BLOCKED_EVIDENCE" >&2
}
write_partial_install_recovery_evidence() {
  local reason="$1"
  local cleanup_passed="$2"
  if ! python3 - \
      "$PARTIAL_INSTALL_RECOVERY_EVIDENCE" \
      "$reason" \
      "$cleanup_passed" \
      "${RELEASE_ID:-}" \
      "$BACKUP_ROOT/index.tsv" \
      "$BACKUP_ROOT/new-files.txt" \
      "${RECREATE_NODES[@]}" <<'PY'
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def sha256_if_file(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


(
    output_raw,
    reason,
    cleanup_passed_raw,
    release_id,
    index_raw,
    new_files_raw,
    *nodes,
) = sys.argv[1:]
output = Path(output_raw)
index_path = Path(index_raw)
new_files_path = Path(new_files_raw)
payload = {
    "schema_version": "trader-v3-partial-install-recovery/v1",
    "recorded_at": datetime.now(timezone.utc).isoformat(),
    "reason": reason,
    "cleanup_passed": cleanup_passed_raw == "1",
    "release_id": release_id,
    "index_path": str(index_path),
    "index_sha256": sha256_if_file(index_path),
    "new_files_path": str(new_files_path),
    "new_files_sha256": sha256_if_file(new_files_path),
    "nodes": nodes,
    "required_state": "A-D stopped",
}
encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(
    "utf-8"
)
temporary = output.with_name(f".{output.name}.new")
try:
    temporary.unlink()
except FileNotFoundError:
    pass
descriptor = os.open(
    temporary,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
    0o400,
)
try:
    offset = 0
    while offset < len(encoded):
        offset += os.write(descriptor, encoded[offset:])
    os.fsync(descriptor)
finally:
    os.close(descriptor)
os.replace(temporary, output)
os.chmod(output, 0o400)
directory_descriptor = os.open(output.parent, os.O_RDONLY)
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
PY
  then
    echo "!! partial install recovery evidence write FAILED" >&2
    return 1
  fi
  if ! refresh_backup_checksums; then
    echo "!! partial install recovery evidence checksum refresh FAILED" >&2
    return 1
  fi
  echo "!! partial install recovery evidence: $PARTIAL_INSTALL_RECOVERY_EVIDENCE" >&2
}
advance_reviewed_rollout_for_node() {
  die "reviewed rollout phase advancement requires the separate user-confirmed command"
}
bootstrap_control_plane_roles() {
  local output_dir
  if [ "$CONTROL_PLANE_ROLE_BOOTSTRAP_REQUIRED" != "1" ]; then
    return
  fi
  output_dir="$(dirname "${CONTROL_PLANE_ROLE_ENV_FILES[0]}")"
  verify_maintenance_fence "control-plane-role-bootstrap"
  "$T/.venv-cp/bin/python" "$BOOTSTRAP_CONTROL_PLANE_ROLES" apply \
    --env-file "$T/.env.v3" \
    --output-dir "$output_dir" \
    --owner-uid 0 \
    --owner-gid 0
  for env_file in "${CONTROL_PLANE_ROLE_ENV_FILES[@]}"; do
    [ -f "$env_file" ] \
      || die "control-plane role bootstrap omitted: $env_file"
    [ ! -L "$env_file" ] \
      || die "control-plane role env cannot be a symlink: $env_file"
    [ "$(stat -c '%a:%u:%g' "$env_file")" = "600:0:0" ] \
      || die "control-plane role env ownership or mode mismatch: $env_file"
  done
  echo "== isolated control-plane role credentials bootstrapped"
}
write_account_b_reviewer_public_key() {
  local output="$1"
  cat > "$output" <<'PEM'
-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEApjHjatn5idd5XstlZLx1
MFWAPDRf1GZlZ+fLFfJ5Nb1ybpRQECUDHB/jl57KZjoploI7fqoxNEwP/K0hYcDj
FPcNx/wL/VrUUheTHIrdGaMlk/cdHDIwnAGeZcwe7L5FbTOI+Iw+X9RyAjjSfQ4Z
qo+V7F9kZAMpw9qyhCNns5WvEXVgWnpCcUx97QnyKfc5BrA6AkB5xaQJu82Rvjk9
s+fcOkBQ/fBaaMFeIKbAIkIs4KzKuhtBFjsBt9OqR6cu/Vqpz4fiUoX/2VCS2EtJ
a3esQkc/SkSCt0a3THx+0U81FMlC3iDAB6GF0tY7jWcJsNHhzC1cn6BGd1ltWlqz
ZAbGOIjCHv04FkRl2/F5hK2/4KUNWmw+ex/hlk+HGFkTKNe0+wPDSIKDXzAvqjQb
fqQJhUV+CFfITBiuHNIQ496G3T/BNz0h5aGNrmOyDwFyOGcP8f1+heMiw5kZVTrq
auYNLbl4px6yIcL0B9n7N2wQapy1k2noWdiF4GcJmYSDkwkSkeSDc5ppzr71flAo
VhNQxsEncM6SbE9brYoUTwQAjnBWZyt9ZUokyflrb5GGD5B9pjC9GTx3I5SKpj0H
lQLbDsFdfjVXp4HsbHuYNQFvSKsC6sDQOfeeL+uhZRdOdmfvQywEF0om+z4tV55x
IiM3ZM2rzP4RahFrKl2JrPUCAwEAAQ==
-----END PUBLIC KEY-----
PEM
  chmod 0400 "$output"
  [ "$(sha256sum "$output" | awk '{print $1}')" = "$ACCOUNT_B_REVIEWER_PUBLIC_KEY_SHA256" ] \
    || die "embedded account-b reviewer trust root hash mismatch"
}
generate_release_recreate() {
  local manifest="$1"
  local target_node="${2:-$ROLLOUT_NODE}"
  local delivery_mode
  local binance_execution_target
  local binance_futures_execution_target
  delivery_mode="$(
    python3 -c \
      'import json,sys; print(json.load(open(sys.argv[1]))["delivery_mode"])' \
      "$manifest"
  )"
  case "$delivery_mode" in
    immutable_image)
      TRADER_ROOT="$T" python3 "$GEN_RECREATE" \
        "$target_node" \
        --release-manifest "$manifest" \
        --database-schema-epoch "$DATABASE_SCHEMA_EPOCH"
      ;;
    transition_bind_mount)
      binance_execution_target="$(
        python3 -c \
          'import json,sys; m=json.load(open(sys.argv[1])); print(next(f["mount_target"] for f in m["files"] if f["bundle_path"]=="binance_execution.py"))' \
          "$manifest"
      )"
      binance_futures_execution_target="$(
        python3 -c \
          'import json,sys; m=json.load(open(sys.argv[1])); print(next(f["mount_target"] for f in m["files"] if f["bundle_path"]=="binance_futures_execution.py"))' \
          "$manifest"
      )"
      TRADER_ROOT="$T" python3 "$GEN_RECREATE" \
        "$target_node" \
        "$binance_execution_target" \
        "$binance_futures_execution_target" \
        --release-manifest "$manifest" \
        --database-schema-epoch "$DATABASE_SCHEMA_EPOCH"
      ;;
    *)
      echo "FATAL: recovery manifest delivery mode is invalid" >&2
      return 1
      ;;
  esac
}
discover_legacy_binance_mount_targets() {
  local node="$1"
  python3 - "$node" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

container = sys.argv[1]
inspected = json.loads(
    subprocess.check_output(
        ["docker", "inspect", container],
        text=True,
    )
)[0]
required = (
    "binance_execution.py",
    "binance_futures_execution.py",
)
by_filename = {}
for mount in inspected.get("Mounts") or []:
    source = str(mount.get("Source") or "")
    destination = str(mount.get("Destination") or "")
    filename = Path(source).name
    if filename not in required:
        continue
    if not destination.startswith("/") or ":" in destination:
        raise SystemExit(
            f"legacy Binance mount destination is invalid: {destination}"
        )
    if filename in by_filename:
        raise SystemExit(
            f"legacy Binance mount is duplicated: {filename}"
        )
    by_filename[filename] = destination
missing = [filename for filename in required if filename not in by_filename]
if missing:
    raise SystemExit(
        "legacy Binance mount targets are missing: " + ",".join(missing)
    )
for filename in required:
    print(by_filename[filename])
PY
}
verify_legacy_recreate_artifact() {
  local path="$1"
  local label="$2"
  local source_kind="${3:-generated}"
  local emit_hash="${4:-0}"
  python3 - "$path" "$label" "$source_kind" "$emit_hash" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
label = sys.argv[2]
source_kind = sys.argv[3]
emit_hash = sys.argv[4] == "1"
if source_kind not in {"existing", "generated"}:
    raise SystemExit(f"{label} source kind is invalid")
flags = os.O_RDONLY
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
try:
    descriptor = os.open(path, flags)
except OSError as exc:
    raise SystemExit(f"{label} is unavailable: {path}") from exc
try:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise SystemExit(f"{label} must be a regular file: {path}")
    mode = stat.S_IMODE(before.st_mode)
    if source_kind == "generated":
        if mode != 0o700:
            raise SystemExit(f"{label} mode must be 0700: {path}")
        if before.st_nlink != 1:
            raise SystemExit(f"{label} link count is invalid: {path}")
        if before.st_uid != os.geteuid():
            raise SystemExit(f"{label} owner mismatch: {path}")
    elif mode & 0o111 == 0:
        raise SystemExit(f"{label} must be executable: {path}")
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    after = os.fstat(descriptor)
    path_metadata = os.lstat(path)
    stable = (
        before.st_dev == after.st_dev == path_metadata.st_dev,
        before.st_ino == after.st_ino == path_metadata.st_ino,
        before.st_size == after.st_size == path_metadata.st_size,
        before.st_mtime_ns
        == after.st_mtime_ns
        == path_metadata.st_mtime_ns,
        before.st_ctime_ns
        == after.st_ctime_ns
        == path_metadata.st_ctime_ns,
    )
    if not all(stable):
        raise SystemExit(f"{label} changed during verification: {path}")
    if emit_hash:
        print(digest.hexdigest())
finally:
    os.close(descriptor)
PY
}
capture_existing_legacy_recreate() {
  local source="$1"
  local destination="$2"
  python3 - "$source" "$destination" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
source_flags = os.O_RDONLY
destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    source_flags |= os.O_NOFOLLOW
    destination_flags |= os.O_NOFOLLOW
source_descriptor = os.open(source, source_flags)
destination_descriptor = False
try:
    before = os.fstat(source_descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise SystemExit(f"existing recreate must be regular: {source}")
    mode = stat.S_IMODE(before.st_mode)
    if mode & 0o111 == 0:
        raise SystemExit(f"existing recreate must be executable: {source}")
    destination_descriptor = os.open(
        destination,
        destination_flags,
        mode,
    )
    os.fchmod(destination_descriptor, mode)
    os.fchown(destination_descriptor, before.st_uid, before.st_gid)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(source_descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        offset = 0
        while offset < len(chunk):
            offset += os.write(destination_descriptor, chunk[offset:])
    os.fsync(destination_descriptor)
    after = os.fstat(source_descriptor)
    path_metadata = os.lstat(source)
    stable = (
        before.st_dev == after.st_dev == path_metadata.st_dev,
        before.st_ino == after.st_ino == path_metadata.st_ino,
        before.st_size == after.st_size == path_metadata.st_size,
        before.st_mtime_ns
        == after.st_mtime_ns
        == path_metadata.st_mtime_ns,
        before.st_ctime_ns
        == after.st_ctime_ns
        == path_metadata.st_ctime_ns,
    )
    if not all(stable):
        raise SystemExit(
            f"existing recreate changed during capture: {source}"
        )
    captured = os.fstat(destination_descriptor)
    if not stat.S_ISREG(captured.st_mode) or captured.st_nlink != 1:
        raise SystemExit(
            f"captured recreate identity is invalid: {destination}"
        )
    print(digest.hexdigest())
except BaseException:
    if destination_descriptor is not False:
        os.close(destination_descriptor)
        destination_descriptor = False
    try:
        destination.unlink()
    except FileNotFoundError:
        pass
    raise
finally:
    os.close(source_descriptor)
    if destination_descriptor is not False:
        os.close(destination_descriptor)
directory_descriptor = os.open(destination.parent, os.O_RDONLY)
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
PY
}
promote_generated_legacy_recreate() {
  local source="$1"
  local destination="$2"
  python3 - "$source" "$destination" <<'PY'
import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
source_flags = os.O_RDONLY
if hasattr(os, "O_NOFOLLOW"):
    source_flags |= os.O_NOFOLLOW
source_descriptor = os.open(source, source_flags)
temporary_descriptor = False
temporary_path = False
destination_created = False
try:
    before = os.fstat(source_descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o700
        or before.st_nlink != 1
        or before.st_uid != os.geteuid()
    ):
        raise SystemExit(
            f"generated recreate source is invalid: {source}"
        )
    temporary_descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_raw)
    os.fchmod(temporary_descriptor, 0o700)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(source_descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        offset = 0
        while offset < len(chunk):
            offset += os.write(temporary_descriptor, chunk[offset:])
    os.fsync(temporary_descriptor)
    after = os.fstat(source_descriptor)
    path_metadata = os.lstat(source)
    stable = (
        before.st_dev == after.st_dev == path_metadata.st_dev,
        before.st_ino == after.st_ino == path_metadata.st_ino,
        before.st_size == after.st_size == path_metadata.st_size,
        before.st_mtime_ns
        == after.st_mtime_ns
        == path_metadata.st_mtime_ns,
        before.st_ctime_ns
        == after.st_ctime_ns
        == path_metadata.st_ctime_ns,
    )
    if not all(stable):
        raise SystemExit(
            f"generated recreate changed during promotion: {source}"
        )
    try:
        os.lstat(destination)
    except FileNotFoundError:
        pass
    else:
        raise SystemExit(
            f"generated recreate destination exists: {destination}"
        )
    os.link(temporary_path, destination, follow_symlinks=False)
    destination_created = True
    temporary_path.unlink()
    temporary_path = False
    destination_metadata = os.lstat(destination)
    if (
        not stat.S_ISREG(destination_metadata.st_mode)
        or stat.S_IMODE(destination_metadata.st_mode) != 0o700
        or destination_metadata.st_nlink != 1
        or destination_metadata.st_uid != os.geteuid()
    ):
        raise SystemExit(
            f"promoted recreate identity is invalid: {destination}"
        )
    directory_descriptor = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    print(digest.hexdigest())
except BaseException:
    if destination_created:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
    if temporary_path is not False:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    raise
finally:
    os.close(source_descriptor)
    if temporary_descriptor is not False:
        os.close(temporary_descriptor)
PY
}
remove_generated_legacy_recreate_if_unchanged() {
  local source="$1"
  local destination="$2"
  python3 - "$source" "$destination" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
if not destination.exists() and not destination.is_symlink():
    raise SystemExit(0)
flags = os.O_RDONLY
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
source_descriptor = os.open(source, flags)
destination_descriptor = os.open(destination, flags)
try:
    source_metadata = os.fstat(source_descriptor)
    destination_metadata = os.fstat(destination_descriptor)
    for metadata, path in (
        (source_metadata, source),
        (destination_metadata, destination),
    ):
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
        ):
            raise SystemExit(
                f"generated recreate cleanup identity is invalid: {path}"
            )
    def read_all(descriptor):
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    if read_all(source_descriptor) != read_all(destination_descriptor):
        raise SystemExit(
            f"generated recreate cleanup hash changed: {destination}"
        )
    path_metadata = os.lstat(destination)
    if (
        path_metadata.st_dev != destination_metadata.st_dev
        or path_metadata.st_ino != destination_metadata.st_ino
    ):
        raise SystemExit(
            f"generated recreate cleanup identity changed: {destination}"
        )
    destination.unlink()
finally:
    os.close(source_descriptor)
    os.close(destination_descriptor)
directory_descriptor = os.open(destination.parent, os.O_RDONLY)
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
PY
}
write_legacy_recreate_bootstrap_evidence() {
  python3 - \
    "$LEGACY_RECREATE_RECORDS" \
    "$LEGACY_RECREATE_GENERATED_LIST" \
    "$LEGACY_RECREATE_BOOTSTRAP_EVIDENCE" \
    "${RECREATE_NODES[@]}" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


def read_recreate(path, source):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SystemExit(f"legacy backup recreate is invalid: {path}")
        mode = stat.S_IMODE(before.st_mode)
        if source == "generated":
            if (
                mode != 0o700
                or before.st_nlink != 1
                or before.st_uid != os.geteuid()
            ):
                raise SystemExit(
                    f"legacy backup recreate is invalid: {path}"
                )
        elif mode & 0o111 == 0:
            raise SystemExit(
                f"legacy backup recreate is not executable: {path}"
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        path_metadata = os.lstat(path)
        stable = (
            before.st_dev == after.st_dev == path_metadata.st_dev,
            before.st_ino == after.st_ino == path_metadata.st_ino,
            before.st_size == after.st_size == path_metadata.st_size,
            before.st_mtime_ns
            == after.st_mtime_ns
            == path_metadata.st_mtime_ns,
            before.st_ctime_ns
            == after.st_ctime_ns
            == path_metadata.st_ctime_ns,
        )
        if not all(stable):
            raise SystemExit(
                f"legacy backup recreate changed during read: {path}"
            )
        return digest.hexdigest(), mode
    finally:
        os.close(descriptor)


records_path = Path(sys.argv[1])
generated_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])
expected_nodes = sys.argv[4:]
records = []
seen = set()
for raw in records_path.read_text(encoding="utf-8").splitlines():
    fields = raw.split("\t")
    if len(fields) != 5:
        raise SystemExit("legacy recreate record is invalid")
    node, source, live_raw, backup_raw, original_sha256 = fields
    if node in seen:
        raise SystemExit(f"legacy recreate record is duplicated: {node}")
    seen.add(node)
    live_path = Path(live_raw)
    backup_path = Path(backup_raw)
    if source not in {"existing", "generated"}:
        raise SystemExit(f"legacy recreate source is invalid: {node}")
    backup_sha256, backup_mode = read_recreate(backup_path, source)
    if original_sha256 != backup_sha256:
        raise SystemExit(f"legacy recreate capture hash differs: {node}")
    records.append(
        {
            "node": node,
            "source": source,
            "live_path": str(live_path),
            "backup_path": str(backup_path),
            "mode": format(backup_mode, "04o"),
            "recreate_sha256": backup_sha256,
        }
    )
if seen != set(expected_nodes) or len(seen) != len(expected_nodes):
    raise SystemExit("legacy recreate records differ from node exact-set")
generated_nodes = []
for raw in generated_path.read_text(encoding="utf-8").splitlines():
    fields = raw.split("\t")
    if len(fields) != 4:
        raise SystemExit("legacy generated recreate record is invalid")
    node, recreate_raw, environment_raw, evidence_raw = fields
    generated_nodes.append(node)
    matching = [item for item in records if item["node"] == node]
    if len(matching) != 1 or matching[0]["source"] != "generated":
        raise SystemExit(
            f"legacy generated recreate source differs: {node}"
        )
    matching[0]["environment_path"] = environment_raw
    matching[0]["snapshot_evidence_path"] = evidence_raw
    if matching[0]["backup_path"] != recreate_raw:
        raise SystemExit(
            f"legacy generated recreate path differs: {node}"
        )
if len(generated_nodes) != len(set(generated_nodes)):
    raise SystemExit("legacy generated recreate nodes are duplicated")
payload = {
    "schema_version": "trader-v3-legacy-recreate-bootstrap/v1",
    "nodes": sorted(records, key=lambda item: item["node"]),
    "generated_nodes": sorted(generated_nodes),
}
encoded = (
    json.dumps(payload, indent=2, sort_keys=True) + "\n"
).encode("utf-8")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(output_path, flags, 0o400)
try:
    offset = 0
    while offset < len(encoded):
        offset += os.write(descriptor, encoded[offset:])
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory_descriptor = os.open(output_path.parent, os.O_RDONLY)
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
PY
}
verify_legacy_rollback_recreate_fleet() {
  local node
  local source
  local live_path
  local backup_path
  local expected_sha256
  local actual_sha256
  local snapshot_evidence
  local legacy_targets_output
  local -a legacy_binance_targets=()
  [ -f "$LEGACY_RECREATE_BOOTSTRAP_EVIDENCE" ] \
    || die "legacy recreate bootstrap evidence is missing"
  while IFS=$'\t' read -r \
    node source live_path backup_path expected_sha256; do
    [ -n "$node" ] || continue
    verify_legacy_recreate_artifact \
      "$live_path" \
      "live rollback recreate" \
      "$source"
    verify_legacy_recreate_artifact \
      "$backup_path" \
      "backup rollback recreate" \
      "$source"
    actual_sha256="$(
      verify_legacy_recreate_artifact \
        "$live_path" \
        "live rollback recreate" \
        "$source" \
        1
    )"
    if [ "$actual_sha256" != "$expected_sha256" ]; then
      if [ "$source" = "existing" ]; then
        die "existing rollback recreate script changed during bootstrap: $node"
      fi
      die "generated rollback recreate script changed during bootstrap: $node"
    fi
    [ "$(
      verify_legacy_recreate_artifact \
        "$backup_path" \
        "backup rollback recreate" \
        "$source" \
        1
    )" = "$expected_sha256" ] \
      || die "backup rollback recreate hash differs: $node"
  done < "$LEGACY_RECREATE_RECORDS"
  while IFS=$'\t' read -r \
    node backup_path environment_path snapshot_evidence; do
    [ -n "$node" ] || continue
    if ! legacy_targets_output="$(
      discover_legacy_binance_mount_targets "$node"
    )"; then
      die "legacy Binance mount target discovery failed: $node"
    fi
    legacy_binance_targets=()
    while IFS= read -r target; do
      [ -n "$target" ] || continue
      legacy_binance_targets+=("$target")
    done <<< "$legacy_targets_output"
    [ "${#legacy_binance_targets[@]}" -eq 2 ] \
      || die "legacy Binance mount target count is invalid: $node"
    if ! python3 "$GEN_RECREATE" \
      "$node" \
      "${legacy_binance_targets[0]}" \
      "${legacy_binance_targets[1]}" \
      --verify-snapshot-evidence "$snapshot_evidence"; then
      die "legacy snapshot recreate verification failed: $node"
    fi
  done < "$LEGACY_RECREATE_GENERATED_LIST"
}
prepare_legacy_rollback_recreate_fleet() {
  local node
  local live_path
  local backup_path
  local snapshot_dir
  local environment_path
  local snapshot_evidence
  local before_sha256
  local promoted_sha256
  local legacy_targets_output
  local target
  local -a legacy_binance_targets=()
  local -a promoted_paths=()
  mkdir -p "$LEGACY_RECREATE_SNAPSHOT_ROOT"
  chmod 0700 "$LEGACY_RECREATE_SNAPSHOT_ROOT"
  : > "$LEGACY_RECREATE_RECORDS"
  : > "$LEGACY_RECREATE_GENERATED_LIST"
  chmod 0600 \
    "$LEGACY_RECREATE_RECORDS" \
    "$LEGACY_RECREATE_GENERATED_LIST"
  for node in "${RECREATE_NODES[@]}"; do
    live_path="$T/recreate-$node.sh"
    backup_path="$BACKUP_ROOT/recreate-$node.sh"
    if [ -e "$live_path" ] || [ -L "$live_path" ]; then
      verify_legacy_recreate_artifact \
        "$live_path" \
        "existing rollback recreate" \
        existing
      before_sha256="$(
        capture_existing_legacy_recreate \
          "$live_path" \
          "$backup_path"
      )" || die "existing rollback recreate capture failed: $node"
      [ "$(
        verify_legacy_recreate_artifact \
          "$live_path" \
          "existing rollback recreate" \
          existing \
          1
      )" = "$before_sha256" ] \
        || die "existing rollback recreate script changed during bootstrap: $node"
      [ "$(
        verify_legacy_recreate_artifact \
          "$backup_path" \
          "captured rollback recreate" \
          existing \
          1
      )" = "$before_sha256" ] \
        || die "existing rollback recreate capture hash differs: $node"
      printf '%s\t%s\t%s\t%s\t%s\n' \
        "$node" \
        existing \
        "$live_path" \
        "$backup_path" \
        "$before_sha256" \
        >> "$LEGACY_RECREATE_RECORDS"
      continue
    fi
    case "$node" in
      trader-v3-node-c|trader-v3-node-d)
        ;;
      *)
        die "existing rollback recreate script missing: $node"
        ;;
    esac
    if [[ ! "$DEPLOY_GATE_MODE" =~ ^(bootstrap(_resume)?|migration_rebaseline)_stopped$ ]]; then
      die "existing rollback recreate script missing outside bootstrap: $node"
    fi
    snapshot_dir="$LEGACY_RECREATE_SNAPSHOT_ROOT/$node"
    environment_path="$snapshot_dir/container-env.json"
    snapshot_evidence="$snapshot_dir/snapshot-evidence.json"
    mkdir -p "$snapshot_dir"
    chmod 0700 "$snapshot_dir"
    if ! legacy_targets_output="$(
      discover_legacy_binance_mount_targets "$node"
    )"; then
      die "legacy Binance mount target discovery failed: $node"
    fi
    legacy_binance_targets=()
    while IFS= read -r target; do
      [ -n "$target" ] || continue
      legacy_binance_targets+=("$target")
    done <<< "$legacy_targets_output"
    [ "${#legacy_binance_targets[@]}" -eq 2 ] \
      || die "legacy Binance mount target count is invalid: $node"
    python3 "$GEN_RECREATE" \
      "$node" \
      "${legacy_binance_targets[0]}" \
      "${legacy_binance_targets[1]}" \
      --snapshot-runtime \
      --output "$backup_path" \
      --environment-output "$environment_path" \
      --evidence-output "$snapshot_evidence"
    verify_legacy_recreate_artifact \
      "$backup_path" \
      "generated rollback recreate" \
      generated
    if ! python3 "$GEN_RECREATE" \
      "$node" \
      "${legacy_binance_targets[0]}" \
      "${legacy_binance_targets[1]}" \
      --verify-snapshot-evidence "$snapshot_evidence"; then
      die "legacy snapshot recreate verification failed: $node"
    fi
    before_sha256="$(
      verify_legacy_recreate_artifact \
        "$backup_path" \
        "generated rollback recreate" \
        generated \
        1
    )"
    printf '%s\t%s\t%s\t%s\t%s\n' \
      "$node" \
      generated \
      "$live_path" \
      "$backup_path" \
      "$before_sha256" \
      >> "$LEGACY_RECREATE_RECORDS"
    printf '%s\t%s\t%s\t%s\n' \
      "$node" \
      "$backup_path" \
      "$environment_path" \
      "$snapshot_evidence" \
      >> "$LEGACY_RECREATE_GENERATED_LIST"
  done
  write_legacy_recreate_bootstrap_evidence
  LEGACY_RECREATE_BOOTSTRAP_PREPARED=1
  while IFS=$'\t' read -r \
    node backup_path environment_path snapshot_evidence; do
    [ -n "$node" ] || continue
    live_path="$T/recreate-$node.sh"
    if [ -e "$live_path" ] || [ -L "$live_path" ]; then
      for live_path in "${promoted_paths[@]}"; do
        remove_generated_legacy_recreate_if_unchanged \
          "$BACKUP_ROOT/$(basename "$live_path")" \
          "$live_path" \
          || true
      done
      die "generated rollback recreate live path appeared: $node"
    fi
    if ! promoted_sha256="$(
      promote_generated_legacy_recreate \
        "$backup_path" \
        "$live_path"
    )"; then
      for live_path in "${promoted_paths[@]}"; do
        remove_generated_legacy_recreate_if_unchanged \
          "$BACKUP_ROOT/$(basename "$live_path")" \
          "$live_path" \
          || true
      done
      return 1
    fi
    [ "$promoted_sha256" = "$(
      verify_legacy_recreate_artifact \
        "$backup_path" \
        "generated rollback recreate" \
        generated \
        1
    )" ] || die "generated rollback recreate promotion hash differs: $node"
    promoted_paths+=("$live_path")
  done < "$LEGACY_RECREATE_GENERATED_LIST"
  verify_legacy_rollback_recreate_fleet
}
remove_bootstrap_generated_live_recreate() {
  local node
  local backup_path
  local environment_path
  local snapshot_evidence
  local live_path
  if [ "$LEGACY_RECREATE_BOOTSTRAP_PREPARED" != "1" ]; then
    return
  fi
  if [ "$BACKUP_CAPTURED" = "1" ]; then
    return
  fi
  if [ ! -f "$LEGACY_RECREATE_GENERATED_LIST" ]; then
    return
  fi
  while IFS=$'\t' read -r \
    node backup_path environment_path snapshot_evidence; do
    [ -n "$node" ] || continue
    live_path="$T/recreate-$node.sh"
    if ! remove_generated_legacy_recreate_if_unchanged \
      "$backup_path" \
      "$live_path"; then
      echo "!! generated rollback recreate cleanup hash changed: $node" >&2
    fi
  done < "$LEGACY_RECREATE_GENERATED_LIST"
}
prepare_post_migration_recovery_recreate() {
  local node
  local recovery_dir
  local recovery_env
  local recovery_script
  local live_recreate
  local previous_recreate
  mkdir -p "$POST_MIGRATION_RECOVERY_ROOT"
  chmod 0700 "$POST_MIGRATION_RECOVERY_ROOT"
  for node in "${RECREATE_NODES[@]}"; do
    recovery_dir="$POST_MIGRATION_RECOVERY_ROOT/$node"
    recovery_env="$recovery_dir/container.env"
    recovery_script="$recovery_dir/recreate.sh"
    live_recreate="$T/recreate-$node.sh"
    previous_recreate="$BACKUP_ROOT/recreate-$node.sh"
    mkdir -p "$recovery_dir"
    chmod 0700 "$recovery_dir"
    if ! docker inspect \
      --format '{{range .Config.Env}}{{println .}}{{end}}' \
      "$node" >"$recovery_env"; then
      return 1
    fi
    if [ ! -s "$recovery_env" ]; then
      echo "FATAL: recovery container environment snapshot is empty: $node" >&2
      return 1
    fi
    chmod 0400 "$recovery_env"
    if ! generate_release_recreate \
      "$POST_MIGRATION_RECOVERY_MANIFEST" \
      "$node"; then
      cp -a "$previous_recreate" "$live_recreate"
      return 1
    fi
    if ! cp -a "$live_recreate" "$recovery_script"; then
      cp -a "$previous_recreate" "$live_recreate"
      return 1
    fi
    chmod 0700 "$recovery_script"
    cp -a "$previous_recreate" "$live_recreate"
    if ! cmp -s "$previous_recreate" "$live_recreate"; then
      echo "FATAL: live recreate script was not restored: $node" >&2
      return 1
    fi
  done
  mkdir -p "$POST_MIGRATION_RECOVERY_BIN"
  chmod 0700 "$POST_MIGRATION_RECOVERY_BIN"
  cat >"$POST_MIGRATION_RECOVERY_DOCKER" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

REAL_DOCKER_BIN="${RECOVERY_REAL_DOCKER_BIN:?}"
RECOVERY_ENV_FILE="${RECOVERY_ENV_FILE:?}"
RECOVERY_CONTAINER="${RECOVERY_CONTAINER:?}"
if [ "$#" -ge 4 ] \
  && [ "$1" = "inspect" ] \
  && [ "$2" = "--format" ] \
  && [ "$4" = "$RECOVERY_CONTAINER" ]; then
  case "$3" in
    '{{range .Config.Env}}{{println .}}{{end}}')
      cat "$RECOVERY_ENV_FILE"
      exit 0
      ;;
    '{{.State.Pid}}')
      if "$REAL_DOCKER_BIN" inspect "$RECOVERY_CONTAINER" \
        >/dev/null 2>&1; then
        exec "$REAL_DOCKER_BIN" "$@"
      fi
      printf '0\n'
      exit 0
      ;;
  esac
fi
exec "$REAL_DOCKER_BIN" "$@"
SH
  chmod 0700 "$POST_MIGRATION_RECOVERY_DOCKER"
}
recover_post_migration_node() {
  local node
  local recovery_port
  local recovery_dir
  local recovery_expectation
  local recovery_env
  local recovery_script
  local real_docker_bin
  if ! verify_maintenance_fence "post-migration-recovery"; then
    echo "FATAL: maintenance fence verification blocked recovery" >&2
    return 1
  fi
  if [ ! -f "$POST_MIGRATION_RECOVERY_MANIFEST" ]; then
    echo "FATAL: post-migration recovery manifest is missing" >&2
    return 1
  fi
  if [ ! -x "$POST_MIGRATION_RECOVERY_DOCKER" ]; then
    echo "FATAL: post-migration recovery Docker proxy is missing" >&2
    return 1
  fi
  real_docker_bin="$(command -v docker)"
  if [ -z "$real_docker_bin" ]; then
    echo "FATAL: docker is missing during post-migration recovery" >&2
    return 1
  fi
  for node in "${RECREATE_NODES[@]}"; do
    recovery_dir="$POST_MIGRATION_RECOVERY_ROOT/$node"
    recovery_expectation="$recovery_dir/expectation.json"
    recovery_env="$recovery_dir/container.env"
    recovery_script="$recovery_dir/recreate.sh"
    if [ ! -f "$recovery_expectation" ]; then
      echo "FATAL: post-migration recovery expectation is missing: $node" >&2
      return 1
    fi
    if [ ! -x "$recovery_script" ]; then
      echo "FATAL: post-migration recovery script is missing: $node" >&2
      return 1
    fi
    if [ ! -f "$recovery_env" ]; then
      echo "FATAL: post-migration recovery environment is missing: $node" >&2
      return 1
    fi
    if ! python3 - \
      "$recovery_expectation" \
      "$POST_MIGRATION_RECOVERY_MANIFEST" \
      "$recovery_script" \
      "$recovery_env" \
      "$POST_MIGRATION_RECOVERY_DOCKER" <<'PY'
import hashlib
import json
import sys
from pathlib import Path


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


expectation = json.load(open(sys.argv[1], encoding="utf-8"))
artifact_specs = (
    ("recovery_manifest", Path(sys.argv[2])),
    ("recovery_script", Path(sys.argv[3])),
    ("recovery_env", Path(sys.argv[4])),
    ("recovery_docker", Path(sys.argv[5])),
)
for label, path in artifact_specs:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"post-migration {label} artifact is invalid")
    if expectation.get(f"{label}_path") != str(path):
        raise SystemExit(f"post-migration {label} path mismatch")
    if expectation.get(f"{label}_sha256") != sha256_file(path):
        raise SystemExit(f"post-migration {label} hash mismatch")
PY
    then
      return 1
    fi
    if ! RECOVERY_REAL_DOCKER_BIN="$real_docker_bin" \
      RECOVERY_ENV_FILE="$recovery_env" \
      RECOVERY_CONTAINER="$node" \
      PATH="$POST_MIGRATION_RECOVERY_BIN:$PATH" \
        bash "$recovery_script"; then
      return 1
    fi
    if ! verify_node_ready_halted "$node"; then
      return 1
    fi
    recovery_port="$(node_ready_port "$node")"
    if ! verify_version_endpoint "$recovery_port"; then
      return 1
    fi
    if ! verify_node_startup_memory_reserve "$node"; then
      return 1
    fi
    if ! verify_post_migration_recovery "$recovery_expectation"; then
      return 1
    fi
    if ! cp -a "$recovery_script" "$T/recreate-$node.sh"; then
      echo "FATAL: live recreate promotion failed: $node" >&2
      return 1
    fi
    chmod 0700 "$T/recreate-$node.sh"
  done
  if ! cp -- "$POST_MIGRATION_RECOVERY_MANIFEST" "$T/RELEASE_MANIFEST.json"; then
    echo "FATAL: recovery release manifest promotion failed" >&2
    return 1
  fi
  chmod 0400 "$T/RELEASE_MANIFEST.json"
  if ! python3 - \
    "$POST_MIGRATION_RECOVERY_MANIFEST" \
    "$T/DEPLOYED_COMMIT.txt" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
target = Path(sys.argv[2])
payload = (
    f"{manifest['release_commit']} "
    f"release_id={manifest['release_id']} "
    f"recovered_at={datetime.now(timezone.utc).isoformat()}\n"
).encode("utf-8")
temporary = target.with_name(f".{target.name}.recovery")
descriptor = os.open(
    temporary,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
    0o644,
)
try:
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])
    os.fsync(descriptor)
finally:
    os.close(descriptor)
os.replace(temporary, target)
directory_descriptor = os.open(target.parent, os.O_RDONLY)
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
PY
  then
    echo "FATAL: recovered commit metadata promotion failed" >&2
    return 1
  fi
  echo "== post-migration recovery nodes remain HALTED"
}
restore_pre_migration_state() {
  local node
  local expected_image
  local actual_image
  if ! verify_maintenance_fence "rollback-restore"; then
    echo "!! maintenance fence verification blocked rollback mutation" >&2
    return 1
  fi
  if [ "$BACKUP_CAPTURED" = "1" ]; then
    for node in "${RECREATE_NODES[@]}"; do
      if [ -f "$BACKUP_ROOT/recreate-$node.sh" ]; then
        cp -a "$BACKUP_ROOT/recreate-$node.sh" \
          "$T/recreate-$node.sh" \
          || echo "!! live recreate rollback FAILED: $node" >&2
      fi
    done
  fi
  restore_installed_runtime_files
  rollback_restart_changed_runtimes
	if [ "$CONTROL_PLANE_ISOLATION_ACTIVATED" = "1" ]; then
	  bash "$CONTROL_PLANE_ISOLATION_SCRIPT" \
      --rollback "$BACKUP_ROOT/control-plane-isolation" \
      >/dev/null 2>&1 \
      || echo "!! control-plane topology rollback FAILED" >&2
  elif [ "$CONTROL_PLANE_RESTARTED" = "1" ]; then
    systemctl restart "${CONTROL_PLANE_UNITS[@]}" >/dev/null 2>&1 \
      || echo "!! control-plane rollback restart FAILED: ${CONTROL_PLANE_UNITS[*]}" >&2
  fi
  if [ "$BACKUP_CAPTURED" = "1" ] \
    && [ "$ROLLOUT_RECREATE_STARTED" = "1" ]; then
    for node in "${RECREATE_NODES[@]}"; do
      bash "$BACKUP_ROOT/recreate-$node.sh" >/dev/null 2>&1 \
        || echo "!! old node recreation FAILED: $node" >&2
      expected_image="$(
        awk -F $'\t' -v node="$node" \
          '$1 == node { print $2 }' "$BACKUP_ROOT/old-images.tsv"
      )"
      actual_image="$(
        docker inspect --format '{{.Image}}' "$node" 2>/dev/null
      )"
      if [ -z "$expected_image" ] || [ "$actual_image" != "$expected_image" ]; then
        echo "!! old image restore verification FAILED: $node" >&2
      fi
      docker stop --time 30 "$node" >/dev/null 2>&1 || true
    done
  fi
}
preflight_gate_checkpoint() {
  local gate="$1"
  local requested="${DEPLOY_INJECT_GATE_FAILURE:-}"
  if [ -z "$requested" ]; then
    return
  fi
  [ "${DEPLOY_ALLOW_GATE_FAILURE_INJECTION:-0}" = "1" ] \
    || die "gate failure injection requires DEPLOY_ALLOW_GATE_FAILURE_INJECTION=1"
  if [ "$requested" = "$gate" ]; then
    die "injected pre-downtime gate failure: $gate"
  fi
}
warn_gate() {
  local gate="$1"
  local message="$2"
  printf '!! gate warning [%s]: %s\n' "$gate" "$message" >&2
  if [ -d "$BACKUP_ROOT" ]; then
    printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$gate" "$message" \
      >>"$DEPLOY_WARNINGS_LOG" 2>/dev/null || true
  fi
}
degraded_gate_checkpoint() {
  local gate="$1"
  local requested="${DEPLOY_INJECT_DEGRADED_GATE:-}"
  if [ -z "$requested" ]; then
    return
  fi
  [ "${DEPLOY_ALLOW_GATE_FAILURE_INJECTION:-0}" = "1" ] \
    || die "gate failure injection requires DEPLOY_ALLOW_GATE_FAILURE_INJECTION=1"
  if [ "$requested" = "$gate" ]; then
    warn_gate "$gate" "injected degraded gate failure"
  fi
}
verify_preflight_disk_reserve() {
  local docker_root
  local path
  local available
  [[ "$DEPLOY_MIN_FREE_BYTES" =~ ^[1-9][0-9]*$ ]] \
    || die "DEPLOY_MIN_FREE_BYTES must be a positive integer"
  if ! docker_root="$(docker info --format '{{.DockerRootDir}}')"; then
    warn_gate "disk" "Docker root directory is unavailable"
    return
  fi
  if [ -z "$docker_root" ]; then
    warn_gate "disk" "Docker root directory is empty"
    return
  fi
  for path in "$STAGING" "$T" "$docker_root"; do
    if [ ! -d "$path" ]; then
      warn_gate "disk" "disk gate path is missing: $path"
      return
    fi
    available="$(
      python3 - "$path" <<'PY'
import os
import sys

stats = os.statvfs(sys.argv[1])
print(stats.f_bavail * stats.f_frsize)
PY
    )" || {
      warn_gate "disk" "disk free bytes are unavailable for $path"
      return
    }
    if [[ ! "$available" =~ ^[0-9]+$ ]]; then
      warn_gate "disk" "disk free bytes are invalid for $path"
      return
    fi
    if [ "$available" -lt "$DEPLOY_MIN_FREE_BYTES" ]; then
      warn_gate "disk" "disk free space below deployment reserve: $path"
      return
    fi
  done
  echo "== disk reserve verified: minimum=$DEPLOY_MIN_FREE_BYTES"
}
refresh_short_lived_preflight_evidence() {
  local threshold
  local expected_hash
  [ -f "$STAGING/SHA256SUMS" ] \
    || die "SHA256SUMS missing in staging"
  [[ "$DEPLOY_PIPELINE_WORST_CASE_SECONDS" =~ ^[1-9][0-9]*$ ]] \
    || die "DEPLOY_PIPELINE_WORST_CASE_SECONDS must be a positive integer"
  threshold=$((2 * DEPLOY_PIPELINE_WORST_CASE_SECONDS))
  if [ ! -f "$REDIS_CAPACITY_REFRESH_TOOL" ]; then
    warn_gate "capacity-age" "capacity refresh tool missing in staging"
    degraded_gate_checkpoint "capacity-age"
    return
  fi
  if [ -L "$REDIS_CAPACITY_REFRESH_TOOL" ]; then
    warn_gate "capacity-age" "capacity refresh tool is a symlink"
    degraded_gate_checkpoint "capacity-age"
    return
  fi
  expected_hash="$(
    awk '$2 == "refresh_redis_capacity_evidence.py" {print $1}' \
      "$STAGING/SHA256SUMS"
  )"
  if [[ ! "$expected_hash" =~ ^[0-9a-f]{64}$ ]]; then
    warn_gate "capacity-age" "SHA256SUMS lacks capacity refresh tool"
  elif [ "$(sha256sum "$REDIS_CAPACITY_REFRESH_TOOL" | awk '{print $1}')" != "$expected_hash" ]; then
    warn_gate "capacity-age" "capacity refresh tool checksum mismatch"
  fi
  if [ "$REDIS_CAPACITY_VALIDITY_SECONDS" -lt "$threshold" ]; then
    if ! python3 "$REDIS_CAPACITY_REFRESH_TOOL" refresh \
      --base-evidence "$REDIS_CAPACITY_EVIDENCE" \
      --output "$REDIS_CAPACITY_REFRESH" \
      --minimum-disk-free-bytes "$DEPLOY_MIN_FREE_BYTES"; then
      warn_gate "capacity-age" "capacity evidence refresh failed"
    fi
  fi
  if ! python3 "$REDIS_CAPACITY_REFRESH_TOOL" verify \
    --base-evidence "$REDIS_CAPACITY_EVIDENCE" \
    --receipt "$REDIS_CAPACITY_REFRESH" \
    --max-age-seconds "$REDIS_CAPACITY_VALIDITY_SECONDS"; then
    warn_gate "capacity-age" "capacity evidence refresh receipt is stale or invalid"
  fi
  degraded_gate_checkpoint "capacity-age"
  echo "== short-lived capacity evidence refreshed"
}
refresh_account_b_short_lived_evidence() {
  local metadata
  [ "$ROLLOUT_NODE" = "trader-v3-node-b" ] || return
  [ "$EMERGENCY_ROLLBACK" = "0" ] || return
  if [ ! -x "$ACCOUNT_B_EVIDENCE_REFRESHER" ]; then
    warn_gate "account-b-evidence-refresh" "account-b evidence refresher is unavailable"
    return
  fi
  metadata="$(
    python3 - "$ACCOUNT_B_EVIDENCE_REFRESHER" <<'PY'
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.is_symlink():
    raise SystemExit("account-b evidence refresher cannot be a symlink")
metadata = path.stat()
if not stat.S_ISREG(metadata.st_mode):
    raise SystemExit("account-b evidence refresher must be a regular file")
if metadata.st_uid != os.geteuid():
    raise SystemExit("account-b evidence refresher owner mismatch")
if metadata.st_mode & 0o022:
    raise SystemExit("account-b evidence refresher is group/world writable")
print(f"{metadata.st_uid}:{metadata.st_gid}:{metadata.st_mode & 0o777:o}")
PY
  )" || {
    warn_gate "account-b-evidence-refresh" "account-b evidence refresher trust check failed"
    return
  }
  if [ -z "$metadata" ]; then
    warn_gate "account-b-evidence-refresh" "account-b evidence refresher metadata is empty"
    return
  fi
  if ! timeout \
    --signal=TERM \
    --kill-after=30s \
    "${ACCOUNT_B_EVIDENCE_REFRESH_TIMEOUT_SECONDS}s" \
    "$ACCOUNT_B_EVIDENCE_REFRESHER" \
    --release-manifest "$ACCOUNT_B_RELEASE_MANIFEST" \
    --evidence-file "$ACCOUNT_B_EVIDENCE_FILE" \
    --signature-file "$ACCOUNT_B_EVIDENCE_SIGNATURE" \
    --reviewer-public-key-sha256 \
      "$ACCOUNT_B_REVIEWER_PUBLIC_KEY_SHA256"; then
    warn_gate "account-b-evidence-refresh" "account-b evidence refresh command failed"
  fi
  degraded_gate_checkpoint "account-b-evidence-refresh"
  echo "== account-b one-hour evidence refreshed"
}
verify_immutable_trust_chain() {
  local labels_file
  local proof_status
  if [ "$DELIVERY_MODE" != "immutable_image" ]; then
    return
  fi
  [ -f "$IMMUTABLE_BUILD_ATTESTATION" ] \
    || die "immutable build attestation is missing"
  [ ! -L "$IMMUTABLE_BUILD_ATTESTATION" ] \
    || die "immutable build attestation cannot be a symlink"
  proof_status="present"
  if [ ! -f "$REVIEWER_TRUST_PROOF" ]; then
    proof_status="missing"
    warn_gate "reviewer-trust" "reviewer trust proof is missing"
  elif [ -L "$REVIEWER_TRUST_PROOF" ]; then
    proof_status="symlink"
    warn_gate "reviewer-trust" "reviewer trust proof is a symlink"
  fi
  labels_file="$(mktemp)"
  TEMP_FILES+=("$labels_file")
  docker image inspect \
    --format '{{json .Config.Labels}}' \
    "$TARGET_IMAGE" >"$labels_file"
  python3 - \
    "$IMMUTABLE_BUILD_ATTESTATION" \
    "$REVIEWER_TRUST_PROOF" \
    "$STAGING/bundle-manifest.json" \
    "$RELEASE_SOURCE_MANIFEST" \
    "$DEPENDENCY_LOCK" \
    "$labels_file" \
    "$TARGET_IMAGE" \
    "$RELEASE_COMMIT" \
    "$proof_status" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


(
    attestation_raw,
    proof_raw,
    bundle_raw,
    source_raw,
    lock_raw,
    labels_raw,
    target_image,
    release_commit,
    proof_status,
) = sys.argv[1:]
attestation_path = Path(attestation_raw)
proof_path = Path(proof_raw)
bundle_path = Path(bundle_raw)
source_path = Path(source_raw)
lock_path = Path(lock_raw)
attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
proof = {}
if proof_status == "present":
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
labels = json.loads(Path(labels_raw).read_text(encoding="utf-8"))
if attestation.get("schema_version") != (
    "trader-v3-immutable-build-attestation/v1"
):
    raise SystemExit("immutable build attestation schema mismatch")
if proof_status == "present":
    warnings = []
    if proof.get("schema_version") != "trader-v3-reviewer-trust-proof/v1":
        warnings.append("reviewer trust proof schema mismatch")
    if proof.get("decision") != "approved":
        warnings.append("reviewer trust decision is not approved")
    if not str(proof.get("reviewer") or "").strip():
        warnings.append("reviewer trust proof lacks reviewer identity")
    if proof.get("source_commit") != release_commit:
        warnings.append("reviewer trust source commit mismatch")
    if proof.get("build_attestation_sha256") != sha256_file(attestation_path):
        warnings.append("reviewer trust attestation hash mismatch")
    subject = str(proof.get("review_subject_sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", subject) is None:
        warnings.append("reviewer trust subject hash is invalid")
    for warning in warnings:
        print(f"!! gate warning [reviewer-trust]: {warning}", file=sys.stderr)
if attestation.get("image_digest") != target_image:
    raise SystemExit("immutable attestation image digest mismatch")
for key, path in (
    ("bundle_manifest", bundle_path),
    ("release_source_manifest", source_path),
    ("dependency_lock", lock_path),
):
    reference = attestation.get(key)
    if not isinstance(reference, dict):
        raise SystemExit(f"immutable attestation lacks {key}")
    if reference.get("sha256") != sha256_file(path):
        raise SystemExit(f"immutable attestation {key} hash mismatch")
image_labels = attestation.get("image_labels")
if not isinstance(image_labels, dict) or not image_labels:
    raise SystemExit("immutable attestation image labels are missing")
if not isinstance(labels, dict):
    raise SystemExit("immutable image labels are invalid")
for key, value in image_labels.items():
    if labels.get(key) != value:
        raise SystemExit(f"immutable image label mismatch: {key}")
PY
  degraded_gate_checkpoint "reviewer-trust"
  echo "== immutable image trust chain verified"
}
on_err() {
  local node
  local failed_status="${1:-1}"
  local post_migration_recovery_required=0
  local recovery_allowed=1
  local failure_nodes=()
  if [ "$ROLLBACK_IN_PROGRESS" = "1" ]; then
    exit "$failed_status"
  fi
  if [ "${DOWNTIME_WINDOW_ENTERED:-0}" != "1" ]; then
    trap - ERR
    echo "!! preflight gate FAILED — A-D runtime remains untouched." >&2
    exit "$failed_status"
  fi
  ROLLBACK_IN_PROGRESS=1
  trap - ERR
  set +e
  echo "!! deploy FAILED — nodes remain HALTED/stopped (fail-closed)." >&2
  if declare -p RECREATE_NODES >/dev/null 2>&1 \
    && [ "${#RECREATE_NODES[@]}" -gt 0 ]; then
    failure_nodes=("${RECREATE_NODES[@]}")
  elif [ "${PHASE_ONLY_ROLLOUT:-0}" = "1" ] \
    && declare -p ALL_NODES >/dev/null 2>&1; then
    failure_nodes=("${ALL_NODES[@]}")
  else
    failure_nodes=("$ROLLOUT_NODE")
  fi
  for node in "${failure_nodes[@]}"; do
    docker stop --time 30 "$node" >/dev/null 2>&1 || true
  done
  if [ "${POST_MIGRATION_RECOVERY_REQUIRED:-0}" = "1" ] \
    || [ -f "${MIGRATION_COMMIT_MARKER:-}" ]; then
    post_migration_recovery_required=1
  fi
  if [ "$post_migration_recovery_required" = "1" ]; then
    if [ "${FILES_INSTALLED:-0}" = "1" ] \
      && [ "${HOST_RUNTIME_INSTALL_COMPLETE:-0}" != "1" ]; then
      if restore_installed_runtime_files; then
        rollback_restart_changed_runtimes
        FILES_INSTALLED=0
        write_partial_install_recovery_evidence \
          "partial-host-install-restored-before-forward-recovery" \
          "1" \
          || recovery_allowed=0
      else
        recovery_allowed=0
        write_partial_install_recovery_evidence \
          "partial-host-install-restore-failed" \
          "0" \
          || true
      fi
    fi
    if [[ "$DEPLOY_GATE_MODE" =~ ^(bootstrap(_resume)?|migration_rebaseline)_stopped$ ]] \
      && ! ensure_bootstrap_rollout_registration; then
      recovery_allowed=0
      write_bootstrap_recovery_blocked_evidence \
        "bootstrap-registration-failed-after-migration"
    fi
    if [ "$recovery_allowed" = "1" ] \
      && recover_post_migration_node; then
      POST_MIGRATION_RECOVERY_VERIFIED=1
      if [[ "$DEPLOY_GATE_MODE" =~ ^(bootstrap(_resume)?|migration_rebaseline)_stopped$ ]]; then
        if ! acquire_maintenance_fence_after_bootstrap; then
          POST_MIGRATION_RECOVERY_VERIFIED=0
          recovery_allowed=0
          write_bootstrap_recovery_blocked_evidence \
            "maintenance-fence-failed-after-recovery"
        fi
      fi
      if [ "$recovery_allowed" = "1" ]; then
        if [ "$BOOTSTRAP_REGISTRATION_COMPLETED" = "1" ]; then
          PRESERVE_ROLLOUT_FOR_RETRY=1
        fi
        echo "!! post-migration forward recovery retained release runtime files" >&2
      fi
    fi
    if [ "$recovery_allowed" != "1" ] \
      || [ "$POST_MIGRATION_RECOVERY_VERIFIED" != "1" ]; then
      echo "!! post-migration recovery FAILED; nodes remain stopped" >&2
      for node in "${failure_nodes[@]}"; do
        docker stop --time 30 "$node" >/dev/null 2>&1
      done
    else
      echo "!! post-migration forward recovery complete; nodes remain HALTED" >&2
    fi
  else
    restore_pre_migration_state
  fi
  remove_bootstrap_generated_live_recreate
  if [ "$ROLLOUT_TRACKED" = "1" ] \
    && [ "$ROLLOUT_FINALIZED" != "1" ] \
    && [ "$PRESERVE_ROLLOUT_FOR_RETRY" != "1" ] \
    && [ -n "${RELEASE_ID:-}" ]; then
    run_reviewed_rollout advance \
      --release-id "$RELEASE_ID" \
      --to-phase aborted \
      --actor "$ROLLOUT_ACTOR" \
      --reason "deployment failed for $ROLLOUT_NODE" \
      --idempotency-key "abort:$RELEASE_ID:$ROLLOUT_NODE:$TAG" \
      >/dev/null 2>&1 \
      || echo "!! reviewed rollout abort recording FAILED" >&2
  fi
  finalize_node_startup_resource_evidence \
    || echo "!! node startup resource evidence finalization FAILED" >&2
  echo "!! rollback evidence: $BACKUP_ROOT" >&2
  exit "$failed_status"
}
trap 'on_err $?' ERR
trap cleanup EXIT

# ---------- preflight ----------
cd "$STAGING"
refresh_short_lived_preflight_evidence
degraded_gate_checkpoint "capacity"
sha256sum -c SHA256SUMS >/dev/null || die "staging payload integrity check failed"
[ -f bundle-manifest.json ] || die "bundle-manifest.json missing"
[ -f "$RELEASE_SOURCE_MANIFEST" ] \
  || die "release-source-manifest.json missing"
[ -f "$SYSTEMD_RESOURCE_CONTRACT" ] \
  || die "systemd-resource-contract.json missing"
[ -f "$RELEASE_TOOL" ] || die "release_manifest.py missing in staging"
[ -f "$REVIEWED_ROLLOUT_TOOL" ] \
  || die "reviewed_release_rollout.py missing in staging"
[ -f "$GEN_RECREATE" ] || die "hk-gen-recreate-patched.py missing in staging"
[ -f "$LIVE_NODE_CONFIG_TOOL" ] \
  || die "live_node_config.py missing in staging"
[ -f "$LIVE_RISK_POLICY" ] || die "live-risk-policy.json missing in staging"
require_staging_artifact \
  "$LIVE_TRADE_EXECUTOR" \
  "account_a_live_trade_executor.py"
require_staging_artifact \
  "$LIVE_TRADE_HTTP_ADAPTER" \
  "account_a_live_trade_http_adapter.py"
normalize_live_trade_release_metadata
require_staging_artifact \
  "$BOOTSTRAP_CONTROL_PLANE_ROLES" \
  "bootstrap_control_plane_roles.py"
[ -f "$NODE_CONFIG_A" ] || die "account-a live node config missing"
[ -f "$NODE_CONFIG_B" ] || die "account-b live node config missing"
[ -f "$NODE_CONFIG_C" ] || die "account-c live node config missing"
[ -f "$NODE_CONFIG_D" ] || die "account-d live node config missing"
[ -f "$DEPENDENCY_LOCK" ] || die "dependency lock missing: $DEPENDENCY_LOCK"
[ -f "$MIGRATION_RUNNER" ] || die "control-plane migrate.py missing"
[ -f "$MIGRATION_ORDER_MANAGEMENT_UP" ] \
  || die "0005 up migration missing"
[ -f "$MIGRATION_EVIDENCE_INDEXES_UP" ] \
  || die "0010 up migration missing"
[ -f "$MIGRATION_EVIDENCE_INDEXES_DOWN" ] \
  || die "0010 down migration missing"
[ -f "$MIGRATION_UP" ] || die "0011 up migration missing"
[ -f "$MIGRATION_DOWN" ] || die "0011 down migration missing"
[ -f "$MIGRATION_MAINTENANCE_FENCE_UP" ] \
  || die "0012 up migration missing"
[ -f "$MIGRATION_MAINTENANCE_FENCE_DOWN" ] \
  || die "0012 down migration missing"
[ -f "$MIGRATION_FOUR_ACCOUNT_ROLLOUT_UP" ] \
  || die "0013 up migration missing"
[ -f "$MIGRATION_FOUR_ACCOUNT_ROLLOUT_DOWN" ] \
  || die "0013 down migration missing"
[ -f "$MIGRATION_CANCEL_ORDER_CONTRACT_UP" ] \
  || die "0014 up migration missing"
[ -f "$MIGRATION_CANCEL_ORDER_CONTRACT_DOWN" ] \
  || die "0014 down migration missing"
[ -f "$MIGRATION_REFRESH_EVIDENCE_COMMAND_UP" ] \
  || die "0015 up migration missing"
[ -f "$MIGRATION_REFRESH_EVIDENCE_COMMAND_DOWN" ] \
  || die "0015 down migration missing"
[ -f "$MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_UP" ] \
  || die "0016 up migration missing"
[ -f "$MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_DOWN" ] \
  || die "0016 down migration missing"
[ -f "$MIGRATION_OPERATOR_QUERY_PROJECTION_READS_UP" ] \
  || die "0017 up migration missing"
[ -f "$MIGRATION_OPERATOR_QUERY_PROJECTION_READS_DOWN" ] \
  || die "0017 down migration missing"
[ -f "$MIGRATION_PROJECTION_RELIABILITY_UP" ] \
  || die "0018 up migration missing"
[ -f "$MIGRATION_PROJECTION_RELIABILITY_DOWN" ] \
  || die "0018 down migration missing"
case "$DELIVERY_MODE" in
  immutable_image|transition_bind_mount) ;;
  *) die "invalid DELIVERY_MODE: $DELIVERY_MODE" ;;
esac
if [ "$DELIVERY_MODE" = "transition_bind_mount" ]; then
  [ "$EMERGENCY_ROLLBACK" = "1" ] \
    || die "transition_bind_mount requires explicit emergency rollback"
fi
if [ "$CONTROL_PLANE_ISOLATION_MODE" = "disable" ]; then
  [ "$EMERGENCY_ROLLBACK" = "1" ] \
    || die "control-plane isolation disable requires emergency rollback"
fi
if [ "$DELIVERY_MODE" = "immutable_image" ]; then
  [ -f "$IMMUTABLE_BUILDER" ] || die "immutable image builder missing"
fi
command -v python3 >/dev/null || die "python3 missing"
verify_four_channel_account_mapping "" pre_restart
WATCHER_SCHEMA_RESTART_REQUIRED="$(
  detect_watcher_schema_restart_requirement
)"
case "$WATCHER_SCHEMA_RESTART_REQUIRED" in
  0|1)
    ;;
  *)
    die "watcher schema restart requirement is invalid"
    ;;
esac
validate_watcher_runtime_payload
command -v timeout >/dev/null || die "timeout missing"
command -v docker >/dev/null || die "docker missing"
docker compose version >/dev/null || die "docker compose plugin missing"
verify_preflight_disk_reserve
degraded_gate_checkpoint "disk"
command -v "$PG_DUMP_BIN" >/dev/null \
  || die "pg_dump missing: $PG_DUMP_BIN"
command -v "$PG_RESTORE_BIN" >/dev/null \
  || die "pg_restore missing: $PG_RESTORE_BIN"
[ -x "$T/.venv-cp/bin/python" ] || die "$T/.venv-cp/bin/python missing"
[ -f "$T/.env.v3" ] || die "$T/.env.v3 missing"
load_binance_egress_settings
verify_binance_egress
configure_deploy_gate_mode
if [ "$CONTROL_PLANE_ROLE_BOOTSTRAP_REQUIRED" = "1" ]; then
  "$T/.venv-cp/bin/python" "$BOOTSTRAP_CONTROL_PLANE_ROLES" check \
    --env-file "$T/.env.v3" \
    --output-dir "$(dirname "${CONTROL_PLANE_ROLE_ENV_FILES[0]}")" \
    --owner-uid 0 \
    --owner-gid 0 \
    >/dev/null
else
  for env_file in "${CONTROL_PLANE_ROLE_ENV_FILES[@]}"; do
    [ -f "$env_file" ] || die "control-plane role env missing: $env_file"
  done
fi
[ -f "$REDIS_COLD_BACKUP_MANIFEST" ] \
  || die "Redis cold backup manifest missing: $REDIS_COLD_BACKUP_MANIFEST"
[ -f "$REDIS_CAPACITY_EVIDENCE" ] \
  || die "Redis capacity evidence missing: $REDIS_CAPACITY_EVIDENCE"
[ -f "$REDIS_NAMESPACE_JANITOR" ] \
  || die "redis_namespace_janitor.py missing in staging"
[ -f "$REDIS_NAMESPACE_REGISTRY" ] \
  || die "redis_namespace_registry.py missing in staging"
[ -f "$JP24_REDIS_DEAD_INSTANCE_JANITOR" ] \
  || die "jp24_redis_dead_instance_janitor.py missing in staging"
for required in \
  bundle-manifest.json \
  release-source-manifest.json \
  systemd-resource-contract.json \
  release_manifest.py \
  reviewed_release_rollout.py \
  hk-gen-recreate-patched.py \
  live_node_config.py \
  live-risk-policy.json \
	  account_a_live_trade_executor.py \
	  account_a_live_trade_http_adapter.py \
    bootstrap_control_plane_roles.py \
    build_immutable_watcher_image.py \
    watcher-runtime-manifest.json \
	  host/decision_gateway/gateway.py \
    host/exchange_state_recorder.py \
	  host/hermes_signal_feeder.py \
	  host/v3_trade.py \
	  host/v3-trader/SKILL.md \
	  redis_namespace_janitor.py \
	  redis_namespace_registry.py \
  jp24_redis_dead_instance_janitor.py \
  infra/systemd/trader-v3-redis-dead-instance-janitor.service \
  infra/systemd/trader-v3-redis-dead-instance-janitor.timer \
  infra/systemd/trader-v3-redis-namespace-janitor.service \
  infra/systemd/trader-v3-redis-namespace-janitor.timer \
		  services/control-plane/db/migrate.py \
  db/migrations/0005_order_management.up.sql \
  db/migrations/0005_order_management.down.sql \
  db/migrations/0010_evidence_and_poll_indexes.up.sql \
  db/migrations/0010_evidence_and_poll_indexes.down.sql \
  db/migrations/0011_live_safety.up.sql \
  db/migrations/0011_live_safety.down.sql \
  db/migrations/0012_control_plane_maintenance_fence.up.sql \
  db/migrations/0012_control_plane_maintenance_fence.down.sql \
  db/migrations/0013_four_account_rollout.up.sql \
  db/migrations/0013_four_account_rollout.down.sql \
  db/migrations/0014_cancel_order_contract.up.sql \
  db/migrations/0014_cancel_order_contract.down.sql \
  db/migrations/0015_refresh_evidence_command.up.sql \
  db/migrations/0015_refresh_evidence_command.down.sql \
  db/migrations/0016_control_plane_lock_privileges.up.sql \
  db/migrations/0016_control_plane_lock_privileges.down.sql \
  db/migrations/0017_operator_query_projection_reads.up.sql \
  db/migrations/0017_operator_query_projection_reads.down.sql \
  db/migrations/0018_projection_reliability.up.sql \
  db/migrations/0018_projection_reliability.down.sql \
  "$(basename "$DEPENDENCY_LOCK")"; do
  require_checksum_artifact "$required"
done
if [ "$DELIVERY_MODE" = "immutable_image" ]; then
  require_checksum_artifact "build_immutable_node_image.py"
fi

# no-regression guards for the 08-03 live hotfix lineage
grep -q 'CANCEL_ORDER' contracts.py || die "staged contracts.py lost CANCEL_ORDER"
grep -q 'class Execution(' contracts.py || die "staged contracts.py lost OM models"
grep -q 'if not order.tags' intent_execution_planner.py || die "staged planner lost ownership-marker hotfix"
grep -q '_submit_immediate_tp_market_fallback' intent_execution_strategy.py || die "staged strategy lost -2021 fallback"
INSTANCE_SCOPE_COUNT=$(
  grep -c '"use_instance_id": instance_id is not False' \
    nautilus_config.py \
    || true
)
[ "$INSTANCE_SCOPE_COUNT" -eq 2 ] \
  || die "staged nautilus_config.py must fence cache and message bus generations"
grep -q 'UUID4.from_str' node.py \
  || die "staged node.py lost Nautilus UUID4 generation injection"
grep -q '_active_persistence_instance_id' node.py \
  || die "staged node.py lost active Redis generation validation"
grep -q 'persistence_instance_id' redis_namespace_lease.py \
  || die "staged Redis lease lost persistence generation metadata"
grep -q 'persistence_namespace' redis_namespace_lease.py \
  || die "staged Redis lease lost persistence namespace metadata"
grep -q '"autotrim_mins": resolve_message_bus_autotrim_mins()' nautilus_config.py \
  || die "staged nautilus_config.py lost message-bus autotrim"
python3 "$RELEASE_TOOL" validate-bundle \
  --bundle-manifest "$STAGING/bundle-manifest.json" \
  --patch-root "$T/container-patches" \
  --require-transition-runtime

read -r APP_SCHEMA_EPOCH DATABASE_SCHEMA_EPOCH REDIS_SCHEMA_EPOCH < <(
  python3 - \
    "$STAGING/bundle-manifest.json" \
    "$RELEASE_SOURCE_MANIFEST" <<'PY'
import json
import sys
from pathlib import Path

bundle_path = Path(sys.argv[1])
source_path = Path(sys.argv[2])
bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
source = json.loads(source_path.read_text(encoding="utf-8"))
epochs = bundle.get("schema_epochs")
if not isinstance(epochs, dict):
    raise SystemExit("bundle manifest lacks schema_epochs")
required_epochs = ("app", "db", "redis")
for key in required_epochs:
    value = str(epochs.get(key) or "").strip()
    if not value:
        raise SystemExit(f"bundle manifest lacks {key} schema epoch")
if source.get("source_mode") != "git_object":
    raise SystemExit("release source manifest is not Git-object backed")
if source.get("schema_epochs") != epochs:
    raise SystemExit("release source and bundle schema epochs differ")
migration = source.get("migration")
if not isinstance(migration, dict):
    raise SystemExit("release source manifest lacks migration metadata")
expected_migration = {
    "runner": "services/control-plane/db/migrate.py",
    "up": "db/migrations/0011_live_safety.up.sql",
    "down": "db/migrations/0011_live_safety.down.sql",
    "prerequisites": [
        "db/migrations/0005_order_management.up.sql",
    ],
    "db_schema_epoch": epochs["db"],
}
for key, value in expected_migration.items():
    if migration.get(key) != value:
        raise SystemExit(f"release migration metadata mismatch: {key}")
expected_steps = [
    {
        "version": "0010",
        "name": "evidence_and_poll_indexes",
        "up": (
            "db/migrations/"
            "0010_evidence_and_poll_indexes.up.sql"
        ),
        "down": (
            "db/migrations/"
            "0010_evidence_and_poll_indexes.down.sql"
        ),
        "prerequisites": [
            "db/migrations/0005_order_management.up.sql",
        ],
    },
    {
        "version": "0011",
        "name": "live_safety",
        "up": "db/migrations/0011_live_safety.up.sql",
        "down": "db/migrations/0011_live_safety.down.sql",
        "prerequisites": [
            (
                "db/migrations/"
                "0010_evidence_and_poll_indexes.up.sql"
            ),
        ],
    },
    {
        "version": "0012",
        "name": "control_plane_maintenance_fence",
        "up": (
            "db/migrations/"
            "0012_control_plane_maintenance_fence.up.sql"
        ),
        "down": (
            "db/migrations/"
            "0012_control_plane_maintenance_fence.down.sql"
        ),
        "prerequisites": [
            "db/migrations/0011_live_safety.up.sql",
        ],
    },
    {
        "version": "0013",
        "name": "four_account_rollout",
        "up": "db/migrations/0013_four_account_rollout.up.sql",
        "down": "db/migrations/0013_four_account_rollout.down.sql",
        "prerequisites": [
            "db/migrations/0011_live_safety.up.sql",
            (
                "db/migrations/"
                "0012_control_plane_maintenance_fence.up.sql"
            ),
        ],
    },
    {
        "version": "0014",
        "name": "cancel_order_contract",
        "up": "db/migrations/0014_cancel_order_contract.up.sql",
        "down": "db/migrations/0014_cancel_order_contract.down.sql",
        "prerequisites": [
            "db/migrations/0013_four_account_rollout.up.sql",
        ],
    },
    {
        "version": "0015",
        "name": "refresh_evidence_command",
        "up": "db/migrations/0015_refresh_evidence_command.up.sql",
        "down": "db/migrations/0015_refresh_evidence_command.down.sql",
        "prerequisites": [
            "db/migrations/0014_cancel_order_contract.up.sql",
        ],
    },
    {
        "version": "0016",
        "name": "control_plane_lock_privileges",
        "up": (
            "db/migrations/"
            "0016_control_plane_lock_privileges.up.sql"
        ),
        "down": (
            "db/migrations/"
            "0016_control_plane_lock_privileges.down.sql"
        ),
        "prerequisites": [
            "db/migrations/0015_refresh_evidence_command.up.sql",
        ],
    },
    {
        "version": "0017",
        "name": "operator_query_projection_reads",
        "up": (
            "db/migrations/"
            "0017_operator_query_projection_reads.up.sql"
        ),
        "down": (
            "db/migrations/"
            "0017_operator_query_projection_reads.down.sql"
        ),
        "prerequisites": [
            (
                "db/migrations/"
                "0016_control_plane_lock_privileges.up.sql"
            ),
        ],
    },
    {
        "version": "0018",
        "name": "projection_reliability",
        "up": "db/migrations/0018_projection_reliability.up.sql",
        "down": "db/migrations/0018_projection_reliability.down.sql",
        "prerequisites": [
            (
                "db/migrations/"
                "0017_operator_query_projection_reads.up.sql"
            ),
        ],
    },
]
if migration.get("steps") != expected_steps:
    raise SystemExit("release migration metadata mismatch: steps")
required_migration_files = {
    "db/migrations/0010_evidence_and_poll_indexes.up.sql",
    "db/migrations/0010_evidence_and_poll_indexes.down.sql",
    "db/migrations/0011_live_safety.up.sql",
    "db/migrations/0011_live_safety.down.sql",
    "db/migrations/0012_control_plane_maintenance_fence.up.sql",
    "db/migrations/0012_control_plane_maintenance_fence.down.sql",
    "db/migrations/0013_four_account_rollout.up.sql",
    "db/migrations/0013_four_account_rollout.down.sql",
    "db/migrations/0014_cancel_order_contract.up.sql",
    "db/migrations/0014_cancel_order_contract.down.sql",
    "db/migrations/0015_refresh_evidence_command.up.sql",
    "db/migrations/0015_refresh_evidence_command.down.sql",
    "db/migrations/0016_control_plane_lock_privileges.up.sql",
    "db/migrations/0016_control_plane_lock_privileges.down.sql",
    "db/migrations/0017_operator_query_projection_reads.up.sql",
    "db/migrations/0017_operator_query_projection_reads.down.sql",
    "db/migrations/0018_projection_reliability.up.sql",
    "db/migrations/0018_projection_reliability.down.sql",
}
migration_files = migration.get("migration_files")
if not isinstance(migration_files, list):
    raise SystemExit("release migration metadata mismatch: migration_files")
if not required_migration_files.issubset(set(migration_files)):
    raise SystemExit("release migration metadata lacks canonical files")
print(epochs["app"], epochs["db"], epochs["redis"])
PY
)
[ "$DATABASE_SCHEMA_EPOCH" = "0018_projection_reliability" ] \
  || die "reviewed database schema epoch must be 0018_projection_reliability"
[ "$REDIS_SCHEMA_EPOCH" = "fenced-generation-namespace/v2" ] \
  || die "reviewed Redis schema epoch mismatch"

RELEASE_RESOURCE_LIMITS="$(
python3 - \
  "$REDIS_COLD_BACKUP_MANIFEST" \
  "$REDIS_CAPACITY_EVIDENCE" \
  "$T/redis-rebaseline" \
  "$REDIS_SCHEMA_EPOCH" \
  "$LIVE_RISK_POLICY" \
  "$RELEASE_TOOL" \
  "$SYSTEMD_RESOURCE_CONTRACT" <<'PY'
# REDIS_EVIDENCE_V3_VALIDATOR_BEGIN
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

backup_path = Path(sys.argv[1]).resolve()
capacity_path = Path(sys.argv[2]).resolve()
trusted_root = Path(sys.argv[3]).resolve()
expected_redis_epoch = sys.argv[4]
risk_policy_path = Path(sys.argv[5]).resolve()
release_tool_path = Path(sys.argv[6]).resolve()
resource_contract_path = Path(sys.argv[7]).resolve()
backup = json.loads(backup_path.read_text(encoding="utf-8"))
capacity = json.loads(capacity_path.read_text(encoding="utf-8"))
risk_policy = json.loads(risk_policy_path.read_text(encoding="utf-8"))
if backup.get("schema_version") != "trader-v3-redis-cold-backup/v2":
    raise SystemExit("Redis cold backup schema mismatch")
if capacity.get("schema_version") != "trader-v3-redis-capacity-evidence/v3":
    raise SystemExit("Redis capacity evidence schema mismatch")
if expected_redis_epoch != "fenced-generation-namespace/v2":
    raise SystemExit("Redis application schema epoch mismatch")
backup_fields = {
    "schema_version",
    "created_at",
    "completed_at_epoch",
    "mode",
    "source_container",
    "source_container_id",
    "source_image_digest",
    "source_config_image",
    "source_redis_run_id",
    "source_run_id",
    "source_key_count",
    "source_used_memory_bytes",
    "source_dataset_memory_bytes",
    "source_aof_enabled",
    "source_appendonly",
    "source_appendfilename",
    "source_appenddirname",
    "source_save_policy",
    "source_redis_dir",
    "source_rdb_filename",
    "source_mount_type",
    "source_volume_name",
    "source_data_root",
    "nodes_stopped",
    "rdb_changes_since_last_save",
    "backup_root",
    "artifacts",
    "files",
}
capacity_fields = {
    "maxmemory_bytes",
    "used_memory_bytes",
    "dataset_bytes",
    "headroom_percent",
    "required_maxmemory_bytes",
    "remaining_headroom_bytes",
    "redis_cgroup_limit_bytes",
    "container_headroom_percent",
    "required_container_bytes",
    "host_total_memory_bytes",
    "host_available_memory_bytes",
    "other_services_reserve_bytes",
    "system_reserve_bytes",
    "required_host_total_bytes",
    "growth_to_maxmemory_bytes",
    "required_host_available_bytes",
    "maxmemory_policy",
    "docker_args",
    "redis_argument_fragment",
    "docker_memory_args",
    "redis_config",
    "schema_version",
    "created_at",
    "passed",
    "dataset_mode",
    "active_container",
    "active_container_id",
    "active_redis_run_id",
    "initial_redis_run_id",
    "active_volume",
    "active_volume_source",
    "redis_fencing_epoch",
    "redis_fencing_epoch_key",
    "redis_fencing_epoch_sha256",
    "control_keys_reinitialized",
    "memory_limit_bytes",
    "memory_swap_limit_bytes",
    "active_key_count",
    "active_used_memory_bytes",
    "active_dataset_memory_bytes",
    "active_appendonly",
    "active_aof_enabled",
    "active_save_policy",
    "active_rdb_last_bgsave_status",
    "source_backup_manifest",
    "source_backup_manifest_sha256",
    "source_container_id",
    "source_redis_run_id",
    "legacy_container",
    "nodes_stopped",
    "runtime_resource_policy",
    "runtime_checks",
}
if set(backup) != backup_fields:
    raise SystemExit("Redis cold backup v2 fields mismatch")
if set(capacity) != capacity_fields:
    raise SystemExit("Redis capacity evidence v3 fields mismatch")
for evidence_path in (backup_path, capacity_path):
    try:
        evidence_path.relative_to(trusted_root)
    except ValueError as exc:
        raise SystemExit(
            f"Redis evidence escapes trusted root: {evidence_path}"
        ) from exc
if backup_path.parent != capacity_path.parent:
    raise SystemExit("Redis evidence documents are from different runs")


def require_int(document, key, *, minimum=0):
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SystemExit(f"Redis evidence integer is invalid: {key}")
    if value < minimum:
        raise SystemExit(f"Redis evidence integer is below minimum: {key}")
    return value


def require_text(document, key):
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"Redis evidence text is invalid: {key}")
    return value.strip()


if risk_policy.get("schema_version") != "trader-v3-live-risk-policy/v2":
    raise SystemExit("live risk policy schema mismatch")
runtime_resource_contract = risk_policy.get("runtime_resource_contract")
if not isinstance(runtime_resource_contract, dict):
    raise SystemExit("live risk runtime resource contract is missing")
if runtime_resource_contract.get("schema_version") != (
    "trader-v3-runtime-resources/v1"
):
    raise SystemExit("live risk runtime resource schema mismatch")
redis_resource_contract = runtime_resource_contract.get("redis")
if not isinstance(redis_resource_contract, dict):
    raise SystemExit("live risk Redis resource contract is missing")
expected_runtime_resource_policy = {
    "schema_version": runtime_resource_contract["schema_version"],
    "namespace_schema_epoch": expected_redis_epoch,
    "stable_namespaces": {
        "account-a": "trader-TRADER-ACCOUNT-A",
        "account-b": "trader-TRADER-ACCOUNT-B",
        "account-c": "trader-TRADER-ACCOUNT-C",
        "account-d": "trader-TRADER-ACCOUNT-D",
    },
    "stream_retention": {
        "stream_max_entries": require_int(
            redis_resource_contract,
            "stream_max_entries",
            minimum=1,
        ),
        "stream_max_bytes": require_int(
            redis_resource_contract,
            "stream_max_bytes",
            minimum=1,
        ),
        "total_stream_max_bytes": require_int(
            redis_resource_contract,
            "total_stream_max_bytes",
            minimum=1,
        ),
    },
}
if (
    expected_runtime_resource_policy["stream_retention"][
        "total_stream_max_bytes"
    ]
    < expected_runtime_resource_policy["stream_retention"][
        "stream_max_bytes"
    ]
):
    raise SystemExit("live risk Redis stream retention is invalid")
if capacity.get("runtime_resource_policy") != expected_runtime_resource_policy:
    raise SystemExit(
        "Redis capacity runtime resource policy differs from release"
    )

module_name = "_trader_release_manifest_validator"
module_spec = importlib.util.spec_from_file_location(
    module_name,
    release_tool_path,
)
if module_spec is None or module_spec.loader is None:
    raise SystemExit("release manifest validator could not be loaded")
release_manifest = importlib.util.module_from_spec(module_spec)
sys.modules[module_name] = release_manifest
module_spec.loader.exec_module(release_manifest)
validated_resource_contract = (
    release_manifest.validate_systemd_resource_contract(
        resource_contract_path,
        payload_root=resource_contract_path.parent,
    )
)
release_host_config = release_manifest.docker_resource_contract(
    validated_resource_contract,
    release_manifest.REDIS_DOCKER_RESOURCE_ARTIFACT,
)
node_release_host_config = release_manifest.docker_resource_contract(
    validated_resource_contract,
    release_manifest.NODE_DOCKER_RESOURCE_ARTIFACT,
)
release_redis_memory_bytes = int(release_host_config["memory_bytes"])
release_redis_memory_swap_bytes = int(
    release_host_config["memory_swap_bytes"]
)


def parse_timestamp(document, key):
    raw = require_text(document, key)
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"Redis evidence timestamp is invalid: {key}") from exc
    if value.tzinfo is None:
        raise SystemExit(f"Redis evidence timestamp lacks timezone: {key}")
    return value


def resolve_relative_file(root, raw_path, label):
    if not isinstance(raw_path, str) or not raw_path:
        raise SystemExit(f"{label} path is invalid")
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise SystemExit(f"{label} path escapes its root")
    candidate = root / relative
    if candidate.is_symlink():
        raise SystemExit(f"{label} cannot be a symlink")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SystemExit(f"{label} path escapes its root") from exc
    return resolved


def hash_regular_file(path, label):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise SystemExit(f"{label} is not a regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        stable = (
            before.st_dev == after.st_dev
            and before.st_ino == after.st_ino
            and before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
        )
        if not stable:
            raise SystemExit(f"{label} changed during hashing")
        return digest.hexdigest()
    finally:
        os.close(fd)


backup_created_at = parse_timestamp(backup, "created_at")
completed_at_epoch = require_int(backup, "completed_at_epoch", minimum=1)
if completed_at_epoch != int(backup_created_at.timestamp()):
    raise SystemExit("Redis cold backup completion epoch mismatch")
if backup.get("mode") != "cold":
    raise SystemExit("Redis backup mode must be cold")
if backup.get("source_container") != "trader-v3-redis":
    raise SystemExit("Redis backup source container mismatch")
if backup.get("nodes_stopped") is not True:
    raise SystemExit("Redis cold backup lacks stopped-node proof")
if int(backup.get("rdb_changes_since_last_save", -1)) != 0:
    raise SystemExit("Redis cold backup was not captured after a clean SAVE")
source_container_id = require_text(backup, "source_container_id")
if re.fullmatch(r"[0-9a-f]{64}", source_container_id) is None:
    raise SystemExit("Redis cold backup source container ID is invalid")
source_image_digest = require_text(backup, "source_image_digest")
if re.fullmatch(r"sha256:[0-9a-f]{64}", source_image_digest) is None:
    raise SystemExit("Redis cold backup source image digest is invalid")
require_text(backup, "source_config_image")
source_run_id = require_text(backup, "source_redis_run_id")
if re.fullmatch(r"[0-9a-f]{40}", source_run_id) is None:
    raise SystemExit("Redis cold backup source run_id is invalid")
if backup.get("source_run_id") != source_run_id:
    raise SystemExit("Redis cold backup source run_id fields differ")
source_key_count = require_int(backup, "source_key_count")
source_used_memory = require_int(
    backup,
    "source_used_memory_bytes",
    minimum=1,
)
source_dataset_memory = require_int(
    backup,
    "source_dataset_memory_bytes",
)
if source_dataset_memory > source_used_memory:
    raise SystemExit("Redis cold backup dataset memory exceeds used memory")
source_aof_enabled = require_int(backup, "source_aof_enabled")
if source_aof_enabled not in {0, 1}:
    raise SystemExit("Redis cold backup AOF state is invalid")
expected_source_appendonly = "no"
if source_aof_enabled == 1:
    expected_source_appendonly = "yes"
if backup.get("source_appendonly") != expected_source_appendonly:
    raise SystemExit("Redis cold backup AOF config and runtime state differ")
require_text(backup, "source_appendfilename")
require_text(backup, "source_appenddirname")
require_text(backup, "source_save_policy")
if backup.get("source_redis_dir") != "/data":
    raise SystemExit("Redis cold backup source dir mismatch")
source_rdb_filename = require_text(backup, "source_rdb_filename")
if Path(source_rdb_filename).name != source_rdb_filename:
    raise SystemExit("Redis cold backup RDB filename is unsafe")
if backup.get("source_mount_type") != "volume":
    raise SystemExit("Redis cold backup source mount must be a volume")
source_volume_name = require_text(backup, "source_volume_name")
if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", source_volume_name) is None:
    raise SystemExit("Redis cold backup source volume is invalid")
source_data_root = Path(
    require_text(backup, "source_data_root")
).resolve()
if not source_data_root.is_dir():
    raise SystemExit("Redis cold backup source data root is missing")
backup_root = Path(str(backup.get("backup_root") or "")).resolve()
if not backup_root.is_dir():
    raise SystemExit("Redis cold backup root is missing")
try:
    backup_root.relative_to(trusted_root)
except ValueError as exc:
    raise SystemExit("Redis cold backup root escapes trusted root") from exc
if backup_root != backup_path.parent / "cold-backup":
    raise SystemExit("Redis cold backup manifest/root layout mismatch")

files = backup.get("files")
artifacts = backup.get("artifacts")
if not isinstance(files, list) or not files:
    raise SystemExit("Redis cold backup files are missing")
if not isinstance(artifacts, list) or not artifacts:
    raise SystemExit("Redis cold backup artifacts are missing")
files_by_path = {}
for item in files:
    if not isinstance(item, dict) or set(item) != {
        "path",
        "size_bytes",
        "sha256",
    }:
        raise SystemExit("Redis cold backup file entry is invalid")
    raw_path = item.get("path")
    if raw_path in files_by_path:
        raise SystemExit("Redis cold backup file inventory has duplicates")
    path = resolve_relative_file(
        backup_root,
        raw_path,
        "Redis cold backup file",
    )
    if not str(raw_path).startswith("data/"):
        raise SystemExit("Redis cold backup file is outside data inventory")
    if not path.is_file():
        raise SystemExit(f"Redis cold backup file missing: {path}")
    expected_digest = str(item.get("sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        raise SystemExit(f"Redis cold backup hash is invalid: {path}")
    if require_int(item, "size_bytes") != path.stat().st_size:
        raise SystemExit(f"Redis cold backup size mismatch: {path}")
    if hash_regular_file(path, "Redis cold backup file") != expected_digest:
        raise SystemExit(f"Redis cold backup hash mismatch: {path}")
    files_by_path[str(raw_path)] = expected_digest
actual_files = set()
for path in (backup_root / "data").rglob("*"):
    if path.is_symlink():
        raise SystemExit("Redis cold backup data contains a symlink")
    if path.is_file():
        actual_files.add(str(path.relative_to(backup_root)))
if actual_files != set(files_by_path):
    raise SystemExit("Redis cold backup inventory differs from copied data")

expected_validators = {
    "rdb": "redis-check-rdb",
    "aof": "redis-check-aof",
    "aof-manifest": "redis-aof-manifest-parser",
}
artifact_paths = set()
artifact_kinds = set()
for artifact in artifacts:
    expected_artifact_fields = {
        "kind",
        "path",
        "sha256",
        "validator",
        "validation_passed",
        "validator_output_path",
        "validator_output_sha256",
    }
    if not isinstance(artifact, dict) or set(artifact) != expected_artifact_fields:
        raise SystemExit("Redis cold backup artifact entry is invalid")
    kind = artifact.get("kind")
    if kind not in expected_validators:
        raise SystemExit("Redis cold backup artifact kind is invalid")
    raw_path = artifact.get("path")
    if raw_path in artifact_paths:
        raise SystemExit("Redis cold backup artifact inventory has duplicates")
    expected_digest = files_by_path.get(str(raw_path))
    if expected_digest is None:
        raise SystemExit("Redis artifact is absent from backup files")
    if artifact.get("sha256") != expected_digest:
        raise SystemExit("Redis artifact hash differs from backup file entry")
    if artifact.get("validator") != expected_validators[kind]:
        raise SystemExit("Redis artifact checker mismatch")
    if artifact.get("validation_passed") is not True:
        raise SystemExit("Redis artifact checker did not pass")
    report_hash = str(artifact.get("validator_output_sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", report_hash) is None:
        raise SystemExit("Redis artifact checker report hash is invalid")
    report_path = resolve_relative_file(
        backup_path.parent,
        artifact.get("validator_output_path"),
        "Redis artifact checker report",
    )
    if not report_path.is_file():
        raise SystemExit("Redis artifact checker report is missing")
    actual_report_hash = hash_regular_file(
        report_path,
        "Redis artifact checker report",
    )
    if actual_report_hash != report_hash:
        raise SystemExit("Redis artifact checker report hash mismatch")
    artifact_paths.add(str(raw_path))
    artifact_kinds.add(kind)
expected_rdb_path = f"data/{source_rdb_filename}"
if expected_rdb_path not in artifact_paths or "rdb" not in artifact_kinds:
    raise SystemExit("Redis cold backup lacks configured RDB artifact")
if source_aof_enabled == 1 and "aof" not in artifact_kinds:
    raise SystemExit("Redis cold backup lacks enabled AOF artifacts")

capacity_created_at = parse_timestamp(capacity, "created_at")
if capacity_created_at < backup_created_at:
    raise SystemExit("Redis capacity evidence predates cold backup")
if capacity.get("passed") is not True:
    raise SystemExit("Redis capacity evidence is not PASS")
if capacity.get("nodes_stopped") is not True:
    raise SystemExit("Redis capacity evidence lacks stopped-node proof")
if capacity.get("dataset_mode") != "empty-volume-exchange-first-rebaseline":
    raise SystemExit("Redis capacity evidence dataset mode mismatch")
if capacity.get("active_container") != "trader-v3-redis":
    raise SystemExit("Redis capacity evidence active container mismatch")
integer_fields = {
    "maxmemory_bytes": 1,
    "used_memory_bytes": 1,
    "dataset_bytes": 0,
    "headroom_percent": 1,
    "required_maxmemory_bytes": 1,
    "remaining_headroom_bytes": 1,
    "redis_cgroup_limit_bytes": 1,
    "container_headroom_percent": 1,
    "required_container_bytes": 1,
    "host_total_memory_bytes": 1,
    "host_available_memory_bytes": 1,
    "other_services_reserve_bytes": 1,
    "system_reserve_bytes": 1,
    "required_host_total_bytes": 1,
    "growth_to_maxmemory_bytes": 1,
    "required_host_available_bytes": 1,
    "memory_limit_bytes": 1,
    "memory_swap_limit_bytes": 1,
    "active_key_count": 0,
    "active_used_memory_bytes": 1,
    "active_dataset_memory_bytes": 0,
    "active_aof_enabled": 0,
}
numbers = {
    key: require_int(capacity, key, minimum=minimum)
    for key, minimum in integer_fields.items()
}
if numbers["system_reserve_bytes"] < 3 * 1024**3:
    raise SystemExit("Redis capacity evidence reserves less than 3 GiB")
if numbers["dataset_bytes"] > numbers["used_memory_bytes"]:
    raise SystemExit("Redis capacity dataset memory exceeds used memory")
if numbers["maxmemory_bytes"] <= numbers["used_memory_bytes"]:
    raise SystemExit("Redis capacity maxmemory lacks current-memory headroom")
expected_required_maxmemory = (
    numbers["used_memory_bytes"]
    * (100 + numbers["headroom_percent"])
    + 99
) // 100
if numbers["required_maxmemory_bytes"] != expected_required_maxmemory:
    raise SystemExit("Redis capacity required maxmemory calculation mismatch")
if numbers["maxmemory_bytes"] < numbers["required_maxmemory_bytes"]:
    raise SystemExit("Redis capacity maxmemory lacks required headroom")
expected_remaining = (
    numbers["maxmemory_bytes"] - numbers["used_memory_bytes"]
)
if numbers["remaining_headroom_bytes"] != expected_remaining:
    raise SystemExit("Redis capacity remaining headroom mismatch")
if numbers["growth_to_maxmemory_bytes"] != expected_remaining:
    raise SystemExit("Redis capacity growth budget mismatch")
expected_required_container = (
    numbers["maxmemory_bytes"]
    * (100 + numbers["container_headroom_percent"])
    + 99
) // 100
if numbers["required_container_bytes"] != expected_required_container:
    raise SystemExit("Redis capacity container budget calculation mismatch")
if numbers["redis_cgroup_limit_bytes"] < expected_required_container:
    raise SystemExit("Redis cgroup limit lacks maxmemory headroom")
if numbers["redis_cgroup_limit_bytes"] != release_redis_memory_bytes:
    raise SystemExit(
        "Redis capacity cgroup differs from release resource contract"
    )
if numbers["memory_limit_bytes"] != release_redis_memory_bytes:
    raise SystemExit(
        "Redis capacity memory limit differs from release resource contract"
    )
if numbers["memory_swap_limit_bytes"] != release_redis_memory_swap_bytes:
    raise SystemExit(
        "Redis capacity memory swap differs from release resource contract"
    )
if numbers["memory_limit_bytes"] != numbers["redis_cgroup_limit_bytes"]:
    raise SystemExit("Redis capacity memory limit differs from cgroup plan")
if numbers["memory_swap_limit_bytes"] != numbers["redis_cgroup_limit_bytes"]:
    raise SystemExit("Redis capacity evidence allows swap beyond memory limit")
expected_required_host_total = (
    expected_required_container
    + numbers["other_services_reserve_bytes"]
    + numbers["system_reserve_bytes"]
)
if numbers["required_host_total_bytes"] != expected_required_host_total:
    raise SystemExit("Redis capacity host total calculation mismatch")
container_overhead = (
    expected_required_container - numbers["maxmemory_bytes"]
)
expected_required_available = (
    expected_remaining
    + container_overhead
    + numbers["other_services_reserve_bytes"]
    + numbers["system_reserve_bytes"]
)
if numbers["required_host_available_bytes"] != expected_required_available:
    raise SystemExit("Redis capacity host available calculation mismatch")
if numbers["host_total_memory_bytes"] < expected_required_host_total:
    raise SystemExit("Redis capacity host total budget is insufficient")
if numbers["host_available_memory_bytes"] < expected_required_available:
    raise SystemExit("Redis capacity host available budget is insufficient")
if (
    numbers["host_available_memory_bytes"]
    > numbers["host_total_memory_bytes"]
):
    raise SystemExit("Redis capacity host available exceeds host total")
if capacity.get("maxmemory_policy") != "noeviction":
    raise SystemExit("Redis capacity evidence policy mismatch")
expected_redis_args = [
    "--maxmemory",
    str(numbers["maxmemory_bytes"]),
    "--maxmemory-policy",
    "noeviction",
]
if capacity.get("docker_args") != expected_redis_args:
    raise SystemExit("Redis capacity Docker arguments mismatch")
if capacity.get("redis_argument_fragment") != expected_redis_args:
    raise SystemExit("Redis capacity argument fragment mismatch")
expected_memory_args = [
    "--memory",
    str(numbers["redis_cgroup_limit_bytes"]),
    "--memory-swap",
    str(numbers["redis_cgroup_limit_bytes"]),
]
if capacity.get("docker_memory_args") != expected_memory_args:
    raise SystemExit("Redis capacity Docker memory arguments mismatch")
expected_redis_config = (
    f"maxmemory {numbers['maxmemory_bytes']}\n"
    "maxmemory-policy noeviction\n"
)
if capacity.get("redis_config") != expected_redis_config:
    raise SystemExit("Redis capacity config fragment mismatch")
active_container_id = require_text(capacity, "active_container_id")
if re.fullmatch(r"[0-9a-f]{64}", active_container_id) is None:
    raise SystemExit("Redis active container ID evidence is invalid")
if active_container_id == source_container_id:
    raise SystemExit("Redis active container reused source identity")
active_run_id = require_text(capacity, "active_redis_run_id")
if re.fullmatch(r"[0-9a-f]{40}", active_run_id) is None:
    raise SystemExit("Redis active run_id evidence is invalid")
if active_run_id == source_run_id:
    raise SystemExit("Redis rebaseline did not create a new run_id")
initial_run_id = require_text(capacity, "initial_redis_run_id")
if initial_run_id != active_run_id:
    raise SystemExit("Redis initial run_id differs from active run_id evidence")
redis_fencing_epoch = require_text(capacity, "redis_fencing_epoch")
try:
    parsed_fencing_epoch = UUID(redis_fencing_epoch)
except ValueError as exc:
    raise SystemExit("Redis fencing epoch is invalid") from exc
if (
    parsed_fencing_epoch.version != 4
    or str(parsed_fencing_epoch) != redis_fencing_epoch
):
    raise SystemExit("Redis fencing epoch is invalid")
if capacity.get("redis_fencing_epoch_key") != (
    "trader-bot:redis-fencing-epoch"
):
    raise SystemExit("Redis fencing epoch marker key is invalid")
epoch_sha256 = require_text(capacity, "redis_fencing_epoch_sha256")
if re.fullmatch(r"[0-9a-f]{64}", epoch_sha256) is None:
    raise SystemExit("Redis fencing epoch hash is invalid")
actual_epoch_sha256 = hashlib.sha256(
    redis_fencing_epoch.encode("ascii")
).hexdigest()
if epoch_sha256 != actual_epoch_sha256:
    raise SystemExit("Redis fencing epoch hash mismatch")
if capacity.get("control_keys_reinitialized") is not True:
    raise SystemExit("Redis control keys were not reinitialized")
active_volume = require_text(capacity, "active_volume")
if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", active_volume) is None:
    raise SystemExit("Redis active volume evidence is invalid")
if active_volume == source_volume_name:
    raise SystemExit("Redis active volume reused the source volume")
active_volume_source = Path(
    require_text(capacity, "active_volume_source")
).resolve()
if not active_volume_source.is_dir():
    raise SystemExit("Redis active volume source is missing")
if active_volume_source == source_data_root:
    raise SystemExit("Redis active volume source reused the legacy volume")
if Path(str(capacity.get("source_backup_manifest") or "")).resolve() != backup_path:
    raise SystemExit("Redis capacity evidence references another backup path")
expected_backup_hash = str(
    capacity.get("source_backup_manifest_sha256") or ""
)
if re.fullmatch(r"[0-9a-f]{64}", expected_backup_hash) is None:
    raise SystemExit("Redis capacity backup manifest hash is invalid")
actual_backup_hash = hashlib.sha256(backup_path.read_bytes()).hexdigest()
if actual_backup_hash != expected_backup_hash:
    raise SystemExit("Redis capacity evidence references another backup")
if capacity.get("source_container_id") != source_container_id:
    raise SystemExit("Redis capacity source container identity mismatch")
if capacity.get("source_redis_run_id") != source_run_id:
    raise SystemExit("Redis capacity source run_id mismatch")
legacy_container = require_text(capacity, "legacy_container")
if re.fullmatch(
    r"trader-v3-redis-legacy-[a-zA-Z0-9_.-]+",
    legacy_container,
) is None:
    raise SystemExit("Redis legacy container evidence is invalid")
if numbers["active_key_count"] != 1:
    raise SystemExit("Redis capacity evidence requires only the epoch marker")
if numbers["active_used_memory_bytes"] != numbers["used_memory_bytes"]:
    raise SystemExit("Redis capacity active used memory differs from plan")
if numbers["active_dataset_memory_bytes"] != numbers["dataset_bytes"]:
    raise SystemExit("Redis capacity active dataset memory differs from plan")
if capacity.get("active_appendonly") != "no":
    raise SystemExit("Redis capacity active appendonly mode mismatch")
if numbers["active_aof_enabled"] != 0:
    raise SystemExit("Redis capacity active AOF state mismatch")
require_text(capacity, "active_save_policy")
if capacity.get("active_rdb_last_bgsave_status") != "ok":
    raise SystemExit("Redis capacity active RDB status mismatch")
runtime_checks = capacity.get("runtime_checks")
expected_runtime_checks = {
    "active_volume_matches",
    "aof_disabled",
    "active_marker_only",
    "epoch_marker_persisted",
    "memory_limit_matches",
    "memory_swap_is_finite",
    "maxmemory_is_explicit",
    "maxmemory_policy_noeviction",
    "namespace_schema_epoch_bound",
    "nodes_stopped",
    "rdb_status_ok",
    "save_policy_configured",
    "source_container_preserved",
    "source_run_id_changed",
    "stable_namespaces_bound",
    "stream_retention_configured",
    "swap_disabled",
}
if not isinstance(runtime_checks, dict):
    raise SystemExit("Redis capacity runtime checks are missing")
if set(runtime_checks) != expected_runtime_checks:
    raise SystemExit("Redis capacity runtime checks are incomplete")
if any(value is not True for value in runtime_checks.values()):
    raise SystemExit("Redis capacity runtime check did not pass")
print(
    f"{int(node_release_host_config['memory_bytes'])}\t"
    f"{release_redis_memory_bytes}\t"
    f"{release_redis_memory_swap_bytes}"
)
# REDIS_EVIDENCE_V3_VALIDATOR_END
PY
)"
read -r \
  NODE_RELEASE_MEMORY_LIMIT_BYTES \
  REDIS_RELEASE_MEMORY_LIMIT_BYTES \
  REDIS_RELEASE_MEMORY_SWAP_BYTES \
  <<<"$RELEASE_RESOURCE_LIMITS"
[[ "$NODE_RELEASE_MEMORY_LIMIT_BYTES" =~ ^[1-9][0-9]*$ ]] \
  || die "release node memory limit is invalid"
[[ "$REDIS_RELEASE_MEMORY_LIMIT_BYTES" =~ ^[1-9][0-9]*$ ]] \
  || die "release Redis memory limit is invalid"
[ "$REDIS_RELEASE_MEMORY_SWAP_BYTES" = "$REDIS_RELEASE_MEMORY_LIMIT_BYTES" ] \
  || die "release Redis memory swap contract is invalid"
echo "== immutable Redis backup hashes and checker reports verified"

CURRENT_REDIS_RUN_ID="$(
  docker exec trader-v3-redis redis-cli INFO server \
    | sed -n 's/^run_id://p' \
    | tr -d '\r'
)"
CURRENT_REDIS_DBSIZE="$(
  docker exec trader-v3-redis redis-cli DBSIZE \
    | tr -dc '0-9'
)"
CURRENT_REDIS_FENCING_EPOCH="$(
  docker exec trader-v3-redis redis-cli --raw GET \
    "$REDIS_FENCING_EPOCH_KEY" \
    | tr -d '\r'
)"
CURRENT_REDIS_MAXMEMORY="$(
  docker exec trader-v3-redis redis-cli --raw CONFIG GET maxmemory \
    | tail -1 \
    | tr -d '\r'
)"
CURRENT_REDIS_POLICY="$(
  docker exec trader-v3-redis redis-cli --raw CONFIG GET maxmemory-policy \
    | tail -1 \
    | tr -d '\r'
)"
CURRENT_REDIS_APPENDONLY="$(
  docker exec trader-v3-redis redis-cli --raw CONFIG GET appendonly \
    | tail -1 \
    | tr -d '\r'
)"
CURRENT_REDIS_SAVE_POLICY="$(
  docker exec trader-v3-redis redis-cli --raw CONFIG GET save \
    | tail -1 \
    | tr -d '\r'
)"
CURRENT_REDIS_AOF_ENABLED="$(
  docker exec trader-v3-redis redis-cli INFO persistence \
    | sed -n 's/^aof_enabled://p' \
    | tr -d '\r'
)"
CURRENT_REDIS_RDB_STATUS="$(
  docker exec trader-v3-redis redis-cli INFO persistence \
    | sed -n 's/^rdb_last_bgsave_status://p' \
    | tr -d '\r'
)"
CURRENT_REDIS_USED_MEMORY="$(
  docker exec trader-v3-redis redis-cli INFO memory \
    | sed -n 's/^used_memory://p' \
    | tr -d '\r'
)"
CURRENT_REDIS_DATASET_MEMORY="$(
  docker exec trader-v3-redis redis-cli INFO memory \
    | sed -n 's/^used_memory_dataset://p' \
    | tr -d '\r'
)"
CURRENT_REDIS_MEMORY="$(
  docker inspect --format '{{.HostConfig.Memory}}' trader-v3-redis
)"
CURRENT_REDIS_MEMORY_SWAP="$(
  docker inspect --format '{{.HostConfig.MemorySwap}}' trader-v3-redis
)"
CURRENT_REDIS_INSPECT="$(docker inspect trader-v3-redis)"
HOST_TOTAL_BYTES="$(
  awk '/^MemTotal:/ {printf "%.0f", $2 * 1024}' /proc/meminfo
)"
HOST_AVAILABLE_BYTES="$(
  awk '/^MemAvailable:/ {printf "%.0f", $2 * 1024}' /proc/meminfo
)"

python3 - \
  "$REDIS_COLD_BACKUP_MANIFEST" \
  "$REDIS_CAPACITY_EVIDENCE" \
  "$CURRENT_REDIS_RUN_ID" \
  "$CURRENT_REDIS_DBSIZE" \
  "$CURRENT_REDIS_FENCING_EPOCH" \
  "$CURRENT_REDIS_MAXMEMORY" \
  "$CURRENT_REDIS_POLICY" \
  "$CURRENT_REDIS_APPENDONLY" \
  "$CURRENT_REDIS_SAVE_POLICY" \
  "$CURRENT_REDIS_AOF_ENABLED" \
  "$CURRENT_REDIS_RDB_STATUS" \
  "$CURRENT_REDIS_USED_MEMORY" \
  "$CURRENT_REDIS_DATASET_MEMORY" \
  "$CURRENT_REDIS_MEMORY" \
  "$CURRENT_REDIS_MEMORY_SWAP" \
  "$HOST_TOTAL_BYTES" \
  "$HOST_AVAILABLE_BYTES" \
  "$CURRENT_REDIS_INSPECT" \
  "$RELEASE_TOOL" \
  "$SYSTEMD_RESOURCE_CONTRACT" <<'PY'
# REDIS_LIVE_STATE_VALIDATOR_BEGIN
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from uuid import UUID

(
    backup_path,
    capacity_path,
    run_id,
    dbsize,
    redis_fencing_epoch,
    maxmemory,
    policy,
    appendonly,
    save_policy,
    aof_enabled,
    rdb_status,
    used_memory,
    dataset_memory,
    memory_limit,
    memory_swap,
    host_total,
    host_available,
    inspect_raw,
    release_tool_path,
    resource_contract_path,
) = sys.argv[1:]
backup = json.load(open(backup_path, encoding="utf-8"))
capacity = json.load(open(capacity_path, encoding="utf-8"))
inspected = json.loads(inspect_raw)[0]
module_name = "_trader_live_release_manifest_validator"
module_spec = importlib.util.spec_from_file_location(
    module_name,
    release_tool_path,
)
if module_spec is None or module_spec.loader is None:
    raise SystemExit("release manifest validator could not be loaded")
release_manifest = importlib.util.module_from_spec(module_spec)
sys.modules[module_name] = release_manifest
module_spec.loader.exec_module(release_manifest)
validated_resource_contract = (
    release_manifest.validate_systemd_resource_contract(
        Path(resource_contract_path),
        payload_root=Path(resource_contract_path).parent,
    )
)
release_host_config = release_manifest.docker_resource_contract(
    validated_resource_contract,
    release_manifest.REDIS_DOCKER_RESOURCE_ARTIFACT,
)

expected_run_id = str(capacity["active_redis_run_id"])
if run_id != expected_run_id:
    raise SystemExit("running Redis differs from capacity evidence")
if inspected.get("Id") != capacity["active_container_id"]:
    raise SystemExit("running Redis container identity differs from evidence")
# Capacity evidence already proves this exact container/run_id/volume began
# with only the epoch marker. Account namespace keys are expected after nodes
# have run against that fresh volume.
if int(dbsize) < 1:
    raise SystemExit("running Redis is missing the fencing epoch marker")
expected_fencing_epoch = str(capacity["redis_fencing_epoch"])
if redis_fencing_epoch != expected_fencing_epoch:
    raise SystemExit("running Redis fencing epoch differs from evidence")
try:
    parsed_fencing_epoch = UUID(redis_fencing_epoch)
except ValueError as exc:
    raise SystemExit("running Redis fencing epoch is invalid") from exc
if (
    parsed_fencing_epoch.version != 4
    or str(parsed_fencing_epoch) != redis_fencing_epoch
):
    raise SystemExit("running Redis fencing epoch is invalid")
if capacity.get("redis_fencing_epoch_key") != (
    "trader-bot:redis-fencing-epoch"
):
    raise SystemExit("running Redis fencing epoch key differs from evidence")
actual_epoch_sha256 = hashlib.sha256(
    redis_fencing_epoch.encode("ascii")
).hexdigest()
if actual_epoch_sha256 != capacity.get("redis_fencing_epoch_sha256"):
    raise SystemExit("running Redis fencing epoch hash differs from evidence")
if int(maxmemory) != int(capacity["maxmemory_bytes"]):
    raise SystemExit("running Redis maxmemory differs from capacity evidence")
if policy != capacity["maxmemory_policy"] or policy != "noeviction":
    raise SystemExit("running Redis maxmemory policy must be noeviction")
if int(memory_limit) != int(capacity["memory_limit_bytes"]):
    raise SystemExit("running Redis cgroup differs from capacity evidence")
if int(memory_limit) != int(capacity["redis_cgroup_limit_bytes"]):
    raise SystemExit("running Redis cgroup differs from capacity plan")
if int(memory_swap) != int(capacity["memory_swap_limit_bytes"]):
    raise SystemExit("running Redis MemorySwap differs from capacity evidence")
if int(memory_swap) != int(memory_limit):
    raise SystemExit("running Redis permits swap beyond its memory limit")
if int(memory_limit) != int(release_host_config["memory_bytes"]):
    raise SystemExit("running Redis memory differs from release resource contract")
if int(memory_swap) != int(release_host_config["memory_swap_bytes"]):
    raise SystemExit(
        "running Redis memory swap differs from release resource contract"
    )
if (
    appendonly != capacity["active_appendonly"]
    or int(aof_enabled) != int(capacity["active_aof_enabled"])
):
    raise SystemExit("running Redis appendonly persistence differs from review")
if appendonly != "no" or int(aof_enabled) != 0:
    raise SystemExit("running Redis appendonly persistence is unsafe")
if save_policy != capacity["active_save_policy"]:
    raise SystemExit("running Redis RDB save policy differs from review")
if rdb_status != capacity["active_rdb_last_bgsave_status"]:
    raise SystemExit("running Redis last RDB save status is unhealthy")

mounts = [
    item
    for item in inspected.get("Mounts") or []
    if item.get("Destination") == "/data"
]
if len(mounts) != 1:
    raise SystemExit("running Redis must have exactly one /data mount")
mount = mounts[0]
if mount.get("Type") != "volume" or mount.get("RW") is not True:
    raise SystemExit("running Redis /data must be a writable Docker volume")
if mount.get("Name") != capacity["active_volume"]:
    raise SystemExit("running Redis active volume differs from evidence")
active_volume_source = Path(
    str(capacity["active_volume_source"])
).resolve()
if Path(str(mount.get("Source") or "")).resolve() != active_volume_source:
    raise SystemExit("running Redis active volume source differs from evidence")
source_data_root = Path(str(backup["source_data_root"])).resolve()
if Path(str(mount.get("Source") or "")).resolve() == source_data_root:
    raise SystemExit("running Redis still uses the legacy source volume")

legacy_name = str(capacity.get("legacy_container") or "")
if not legacy_name:
    raise SystemExit("Redis capacity evidence lacks legacy container")
if int(dataset_memory) > int(used_memory):
    raise SystemExit("running Redis dataset memory exceeds used memory")
host_total_value = int(host_total)
host_available_value = int(host_available)
evidence_total = int(capacity["host_total_memory_bytes"])
if host_total_value != evidence_total:
    raise SystemExit("live host total memory differs from capacity evidence")
system_reserve = int(capacity["system_reserve_bytes"])
cgroup_limit = int(capacity["redis_cgroup_limit_bytes"])
maxmemory_value = int(capacity["maxmemory_bytes"])
required_container = int(capacity["required_container_bytes"])
other_services_reserve = int(capacity["other_services_reserve_bytes"])
projected_growth = max(0, maxmemory_value - int(used_memory))
container_overhead = max(0, required_container - maxmemory_value)
required_available = (
    projected_growth
    + container_overhead
    + other_services_reserve
    + system_reserve
)
if host_total_value < cgroup_limit + system_reserve:
    raise SystemExit("live host total memory cannot preserve Redis reserve")
if host_available_value < required_available:
    raise SystemExit("live host budget cannot preserve system reserve")
# REDIS_LIVE_STATE_VALIDATOR_END
PY

LEGACY_REDIS_CONTAINER="$(
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["legacy_container"])' \
    "$REDIS_CAPACITY_EVIDENCE"
)"
docker inspect "$LEGACY_REDIS_CONTAINER" >/dev/null \
  || die "legacy Redis container from capacity evidence is missing"
[ "$(docker inspect --format '{{.State.Running}}' "$LEGACY_REDIS_CONTAINER")" = "false" ] \
  || die "legacy Redis container must remain stopped"
LEGACY_REDIS_INSPECT="$(docker inspect "$LEGACY_REDIS_CONTAINER")"
python3 - \
  "$REDIS_COLD_BACKUP_MANIFEST" \
  "$LEGACY_REDIS_INSPECT" <<'PY'
import json
import sys
from pathlib import Path

backup = json.load(open(sys.argv[1], encoding="utf-8"))
inspected = json.loads(sys.argv[2])[0]
if inspected.get("Id") != backup.get("source_container_id"):
    raise SystemExit("legacy Redis container identity differs from backup evidence")
if inspected.get("Image") != backup.get("source_image_digest"):
    raise SystemExit("legacy Redis image differs from cold backup evidence")
config = inspected.get("Config")
if not isinstance(config, dict):
    raise SystemExit("legacy Redis config is missing")
if config.get("Image") != backup.get("source_config_image"):
    raise SystemExit("legacy Redis config image differs from backup evidence")
mounts = [
    item
    for item in inspected.get("Mounts") or []
    if item.get("Destination") == "/data"
]
if len(mounts) != 1:
    raise SystemExit("legacy Redis must retain exactly one /data mount")
mount = mounts[0]
if mount.get("Type") != "volume" or mount.get("RW") is not True:
    raise SystemExit("legacy Redis source mount identity is invalid")
if mount.get("Name") != backup.get("source_volume_name"):
    raise SystemExit("legacy Redis source volume name differs from evidence")
source = Path(str(mount.get("Source") or "")).resolve()
if source != Path(str(backup.get("source_data_root") or "")).resolve():
    raise SystemExit("legacy Redis volume differs from cold backup evidence")
PY

# host-side target
API_TGT="$T/services/control-plane/api/read_api.py"
SNAPSHOT_TGT="$T/services/control-plane/api/snapshot.py"
DECISION_GATEWAY_TGT="$T/services/control-plane/decision_gateway/gateway.py"
EXCHANGE_STATE_RECORDER_TGT="$T/services/control-plane/tools/exchange_state_recorder.py"
SYSTEM_SNAPSHOT_SCHEMA_TGT="$T/packages/contracts/v1/system_snapshot.v1.json"
EXECUTION_DOMAIN_TGT="$T/packages/execution-domain/execution_domain"
EXECUTION_DOMAIN_INIT_TGT="$EXECUTION_DOMAIN_TGT/__init__.py"
EXECUTION_DOMAIN_CONTRACTS_TGT="$EXECUTION_DOMAIN_TGT/contracts.py"
EXECUTION_DOMAIN_CONTROL_PLANE_TGT="$EXECUTION_DOMAIN_TGT/control_plane.py"
EXECUTION_DOMAIN_PORTFOLIO_BASELINE_TGT="$EXECUTION_DOMAIN_TGT/portfolio_baseline.py"
EXECUTION_DOMAIN_ACCOUNT_EXECUTION_LEDGER_TGT="$EXECUTION_DOMAIN_TGT/account_execution_ledger.py"
EXECUTION_DOMAIN_ENTRY_BATCH_TGT="$EXECUTION_DOMAIN_TGT/entry_batch.py"
EXECUTION_DOMAIN_ORDER_OWNERSHIP_TGT="$EXECUTION_DOMAIN_TGT/order_ownership.py"
EXECUTION_DOMAIN_IDEMPOTENCY_TGT="$EXECUTION_DOMAIN_TGT/idempotency.py"
EXECUTION_DOMAIN_IDENTIFIERS_TGT="$EXECUTION_DOMAIN_TGT/identifiers.py"
SETTINGS_PACKAGE_TGT="$T/services/control-plane/settings"
SETTINGS_PACKAGE_FILES=(__init__.py apply_plan.py import_export.py permissions.py publisher.py resolver.py router.py schema.py service.py versioning.py)
AUDIT_PACKAGE_TGT="$T/services/control-plane/audit"
AUDIT_PACKAGE_FILES=(__init__.py settings_audit.py)
ORDER_MANAGEMENT_PACKAGE_TGT="$T/services/control-plane/order_management"
ORDER_MANAGEMENT_PACKAGE_FILES=(__init__.py db_helpers.py identifiers.py metrics.py order_reducer.py outbox.py)
OBSERVABILITY_PACKAGE_TGT="$T/services/nautilus-node/observability"
OBSERVABILITY_PACKAGE_FILES=(__init__.py _shared.py logs.py metrics.py tracing.py)
APP_ROLES_TGT="$T/services/control-plane/api/app_roles.py"
DB_POOLS_TGT="$T/services/control-plane/db/pools.py"
DB_REPOSITORY_TGT="$T/services/control-plane/db/repository.py"
REBUILD_ORDERS_PROJECTION_TGT="$T/scripts/rebuild_orders_projection.py"
[ -f host/read_api.py ] || die "staging missing host/read_api.py"
[ -f host/snapshot.py ] || die "staging missing host/snapshot.py"
[ -f host/decision_gateway/gateway.py ] \
  || die "staging missing host/decision_gateway/gateway.py"
[ -f host/exchange_state_recorder.py ] \
  || die "staging missing host/exchange_state_recorder.py"
[ -f host/hermes_signal_feeder.py ] \
  || die "staging missing host/hermes_signal_feeder.py"
[ -f host/v3_trade.py ] || die "staging missing host/v3_trade.py"
[ -f host/v3-trader/SKILL.md ] \
  || die "staging missing host/v3-trader/SKILL.md"
[ -f host/system_snapshot.v1.json ] \
  || die "staging missing host/system_snapshot.v1.json"
[ -f host/execution_domain/__init__.py ] \
  || die "staging missing host/execution_domain/__init__.py"
[ -f host/execution_domain/contracts.py ] \
  || die "staging missing host/execution_domain/contracts.py"
[ -f host/execution_domain/control_plane.py ] \
  || die "staging missing host/execution_domain/control_plane.py"
[ -f host/execution_domain/portfolio_baseline.py ] \
  || die "staging missing host/execution_domain/portfolio_baseline.py"
[ -f host/execution_domain/account_execution_ledger.py ] \
  || die "staging missing host/execution_domain/account_execution_ledger.py"
[ -f host/execution_domain/entry_batch.py ] \
  || die "staging missing host/execution_domain/entry_batch.py"
[ -f host/execution_domain/order_ownership.py ] \
  || die "staging missing host/execution_domain/order_ownership.py"
[ -f host/execution_domain/idempotency.py ] \
  || die "staging missing host/execution_domain/idempotency.py"
[ -f host/execution_domain/identifiers.py ] \
  || die "staging missing host/execution_domain/identifiers.py"
for settings_file in "${SETTINGS_PACKAGE_FILES[@]}"; do
  [ -f "host/settings/$settings_file" ] \
    || die "staging missing host/settings/$settings_file"
done
for audit_file in "${AUDIT_PACKAGE_FILES[@]}"; do
  [ -f "host/audit/$audit_file" ] \
    || die "staging missing host/audit/$audit_file"
done
for om_file in "${ORDER_MANAGEMENT_PACKAGE_FILES[@]}"; do
  [ -f "host/order_management/$om_file" ] \
    || die "staging missing host/order_management/$om_file"
done
for obs_file in "${OBSERVABILITY_PACKAGE_FILES[@]}"; do
  [ -f "host/observability/$obs_file" ] \
    || die "staging missing host/observability/$obs_file"
done
[ -f host/app_roles.py ] || die "staging missing host/app_roles.py"
[ -f host/pools.py ] || die "staging missing host/pools.py"
[ -f host/repository.py ] || die "staging missing host/repository.py"
[ -f scripts/rebuild_orders_projection.py ] \
  || die "staging missing scripts/rebuild_orders_projection.py"
[ -f "$API_TGT" ] || die "live read_api not found at $API_TGT"
[ -f "$SNAPSHOT_TGT" ] || die "live snapshot not found at $SNAPSHOT_TGT"
[ -f "$DECISION_GATEWAY_TGT" ] \
  || die "live decision gateway not found at $DECISION_GATEWAY_TGT"
[ -f "$EXCHANGE_STATE_RECORDER_TGT" ] \
  || die "live exchange_state_recorder not found at $EXCHANGE_STATE_RECORDER_TGT"
[ -f "$HERMES_FEEDER_TGT" ] \
  || die "live Hermes feeder not found at $HERMES_FEEDER_TGT"
[ -f "$HERMES_V3_TRADE_TGT" ] \
  || die "live Hermes v3_trade not found at $HERMES_V3_TRADE_TGT"
[ -f "$HERMES_V3_SKILL_TGT" ] \
  || die "live Hermes v3-trader SKILL not found at $HERMES_V3_SKILL_TGT"
[ -f "$SYSTEM_SNAPSHOT_SCHEMA_TGT" ] \
  || die "live SystemSnapshot schema not found at $SYSTEM_SNAPSHOT_SCHEMA_TGT"
verify_optional_host_python_module_target "$APP_ROLES_TGT"
verify_optional_host_python_module_target "$DB_POOLS_TGT"
verify_optional_host_python_module_target "$DB_REPOSITORY_TGT"
verify_optional_host_python_module_target "$REBUILD_ORDERS_PROJECTION_TGT"
[ -d "$WATCHER_ROOT" ] \
  || die "telegram-watcher root missing: $WATCHER_ROOT"
[ -f "$WATCHER_COMPOSE_FILE" ] \
  || die "telegram-watcher compose file missing: $WATCHER_COMPOSE_FILE"
[ -d "$WATCHER_SOURCE_ROOT" ] \
  || die "telegram-watcher source root missing: $WATCHER_SOURCE_ROOT"
docker inspect "$WATCHER_CONTAINER" >/dev/null \
  || die "telegram-watcher container missing: $WATCHER_CONTAINER"

# service unit discovery (fail-closed on partial or mixed role topology)
discover_control_plane_units
verify_control_plane_isolation_artifact
docker inspect "${ALL_NODES[@]}" >/dev/null || die "node containers missing"

apply_and_verify_database_migration() {
  verify_account_stall_operation_lock
  if [ "$DEPLOY_GATE_MODE" = "maintenance_fence" ]; then
    MAINTENANCE_FENCE_ID="$(
      python3 -c 'from uuid import uuid4; print(uuid4())'
    )"
  else
    MAINTENANCE_FENCE_ID=""
  fi
  "$T/.venv-cp/bin/python" - \
    "$T/.env.v3" \
    "$MIGRATION_ORDER_MANAGEMENT_UP" \
    "$MIGRATION_EVIDENCE_INDEXES_UP" \
    "$MIGRATION_UP" \
    "$MIGRATION_MAINTENANCE_FENCE_UP" \
    "$MIGRATION_FOUR_ACCOUNT_ROLLOUT_UP" \
    "$MIGRATION_CANCEL_ORDER_CONTRACT_UP" \
    "$MIGRATION_REFRESH_EVIDENCE_COMMAND_UP" \
    "$MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_UP" \
    "$MIGRATION_OPERATOR_QUERY_PROJECTION_READS_UP" \
    "$MIGRATION_PROJECTION_RELIABILITY_UP" \
    "$DATABASE_SCHEMA_EPOCH" \
    "$MIGRATION_COMMIT_MARKER" \
    "$MAINTENANCE_FENCE_ID" \
    "$ACCOUNT_STALL_OPERATION_LOCK_TOKEN" \
    "$ROLLOUT_ACTOR" \
    "$MAINTENANCE_FENCE_STATE" \
    "$ACCOUNT_STALL_FENCE_LEASE_SECONDS" \
    "$ACCOUNT_STALL_HEARTBEAT_MAX_AGE_SECONDS" \
    "$DEPLOY_GATE_MODE" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import UUID

import psycopg2


def read_environment(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key.strip()] = value
    return values


env = read_environment(Path(sys.argv[1]))
order_management_path = Path(sys.argv[2])
evidence_indexes_path = Path(sys.argv[3])
migration_path = Path(sys.argv[4])
maintenance_fence_path = Path(sys.argv[5])
four_account_rollout_path = Path(sys.argv[6])
cancel_order_contract_path = Path(sys.argv[7])
refresh_evidence_command_path = Path(sys.argv[8])
control_plane_lock_privileges_path = Path(sys.argv[9])
operator_query_projection_reads_path = Path(sys.argv[10])
projection_reliability_path = Path(sys.argv[11])
expected_epoch = sys.argv[12]
migration_commit_marker = Path(sys.argv[13])
maintenance_fence_id_raw = sys.argv[14]
maintenance_owner_token = sys.argv[15]
maintenance_actor = sys.argv[16]
maintenance_fence_state = Path(sys.argv[17])
maintenance_lease_seconds = int(sys.argv[18])
heartbeat_max_age_seconds = int(sys.argv[19])
deploy_gate_mode = sys.argv[20]
if deploy_gate_mode not in {
    "bootstrap_stopped",
    "bootstrap_resume_stopped",
    "migration_rebaseline_stopped",
    "maintenance_fence",
}:
    raise SystemExit("invalid database migration gate mode")
maintenance_fence_id = False
if deploy_gate_mode == "maintenance_fence":
    maintenance_fence_id = UUID(maintenance_fence_id_raw)
elif maintenance_fence_id_raw:
    raise SystemExit("bootstrap migration must not carry a fence id")
database_url = env.get("DATABASE_URL", "")
if not database_url:
    raise SystemExit("DATABASE_URL is required for migration")
if expected_epoch != "0018_projection_reliability":
    raise SystemExit("unexpected database schema epoch")
migration_specs = (
    ("0005", "order_management", order_management_path),
    (
        "0010",
        "evidence_and_poll_indexes",
        evidence_indexes_path,
    ),
    ("0011", "live_safety", migration_path),
    (
        "0012",
        "control_plane_maintenance_fence",
        maintenance_fence_path,
    ),
    (
        "0013",
        "four_account_rollout",
        four_account_rollout_path,
    ),
    (
        "0014",
        "cancel_order_contract",
        cancel_order_contract_path,
    ),
    (
        "0015",
        "refresh_evidence_command",
        refresh_evidence_command_path,
    ),
    (
        "0016",
        "control_plane_lock_privileges",
        control_plane_lock_privileges_path,
    ),
    (
        "0017",
        "operator_query_projection_reads",
        operator_query_projection_reads_path,
    ),
    (
        "0018",
        "projection_reliability",
        projection_reliability_path,
    ),
)
migrations = []
for version, name, path in migration_specs:
    sql = path.read_text(encoding="utf-8")
    if not sql.strip():
        raise SystemExit(f"{version} migration is empty")
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    migrations.append((version, name, sql, digest))

required_columns = {
    "release_id",
    "image_digest",
    "config_sha256",
    "dependency_lock_sha256",
    "schema_epoch",
    "positions",
    "regular_orders",
    "algo_orders",
    "positions_snapshot_at",
    "regular_orders_snapshot_at",
    "algo_orders_snapshot_at",
    "reconciliation_completed_at",
    "redis_fencing_epoch",
    "runtime_generation",
    "lease_fencing_token",
    "heartbeat_sequence",
}
required_tables = {
    "execution_jobs",
    "node_command_runs",
    "order_events",
    "order_links",
    "order_management_settings",
    "order_management_setting_versions",
    "price_feed_status",
    "protective_orders_projection",
    "reconciliation_findings",
    "reconciliation_runs",
    "risk_reservations",
    "redis_fencing_epochs",
    "reviewed_release_manifests",
    "reviewed_release_rollouts",
    "live_canary_permits",
    "production_incidents",
    "projection_failures",
    "projection_watermarks",
    "control_plane_maintenance_fences",
    "control_plane_maintenance_fence_events",
}
required_rollout_columns = {
    "redis_fencing_epoch",
}
required_permit_columns = {
    "max_notional_usdt",
    "max_cumulative_loss_usdt",
    "testnet_emergency_close_evidence_sha256",
    "testnet_emergency_close_verified_at",
    "portfolio_baseline_sha256",
}
required_constraints = {
    "ck_node_heartbeats_positions_array",
    "ck_node_heartbeats_regular_orders_array",
    "ck_node_heartbeats_algo_orders_array",
    "ck_node_heartbeats_writer_identity_complete",
    "fk_node_heartbeats_redis_fencing_epoch",
    "ck_redis_fencing_epochs_lifecycle",
    "ck_live_canary_permits_account",
    "ck_live_canary_permits_symbol",
    "ck_live_canary_permits_notional",
    "ck_live_canary_permits_cumulative_loss",
    "ck_live_canary_permits_emergency_close_evidence",
    "ck_live_canary_permits_emergency_close_verified_at",
    "ck_live_canary_permits_open_count",
    "ck_live_canary_permits_consumed_count",
    "ck_live_canary_permits_status",
    "ck_live_canary_permits_consumed_identity",
    "ck_live_canary_permits_portfolio_baseline",
    "ck_reviewed_release_rollouts_phase",
    "ck_reviewed_release_rollout_events_shape",
    "ck_control_plane_maintenance_fence_evidence",
    "ck_control_plane_maintenance_fence_event_evidence",
}
required_indexes = {
    "idx_execution_events_targeted_opening_evidence",
    "idx_trade_intents_pending_opening_symbols",
    "idx_command_node_acks_pending_poll",
    "uq_redis_fencing_epochs_active",
    "idx_node_heartbeats_account_freshness",
    "idx_live_canary_permits_account_status_expiry",
    "idx_production_incidents_open_account",
    "uq_reviewed_release_rollouts_active",
    "uq_control_plane_maintenance_fence_active",
    "idx_control_plane_maintenance_fence_events_fence",
    "idx_projection_failures_account_created",
    "idx_projection_failures_unresolved",
}
required_order_management_columns = {
    "orders_projection": {
        "execution_job_id",
        "venue_symbol",
        "lifecycle_role",
    },
    "audit_events": {
        "action",
        "target",
        "before_state",
        "after_state",
        "reason",
        "request_id",
    },
}

conn = psycopg2.connect(database_url)
fence_receipt = None
try:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("trader-v3-schema-migration",),
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version text PRIMARY KEY,
                    name text NOT NULL,
                    up_sha256 text,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                ALTER TABLE schema_migrations
                    ADD COLUMN IF NOT EXISTS up_sha256 text
                """
            )
            for version, name, sql, digest in migrations:
                cur.execute(
                    """
                    SELECT name, up_sha256
                    FROM schema_migrations
                    WHERE version=%s
                    FOR UPDATE
                    """,
                    (version,),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(sql)
                    cur.execute(
                        """
                        INSERT INTO schema_migrations (
                            version,
                            name,
                            up_sha256
                        )
                        VALUES (%s, %s, %s)
                        """,
                        (version, name, digest),
                    )
                    continue
                if row != (name, digest):
                    raise SystemExit(
                        "schema_migrations "
                        f"{version} name or SQL hash mismatch"
                    )

            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name='node_heartbeats'
                """
            )
            columns = {item[0] for item in cur.fetchall()}
            missing_columns = sorted(required_columns - columns)
            if missing_columns:
                raise SystemExit(
                    f"0011 node_heartbeats columns missing: {missing_columns}"
                )
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name='live_canary_permits'
                """
            )
            permit_columns = {item[0] for item in cur.fetchall()}
            missing_permit_columns = sorted(
                required_permit_columns - permit_columns
            )
            if missing_permit_columns:
                raise SystemExit(
                    "0011 live_canary_permits columns missing: "
                    f"{missing_permit_columns}"
                )
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name='reviewed_release_rollouts'
                  AND is_nullable='NO'
                """
            )
            rollout_columns = {item[0] for item in cur.fetchall()}
            missing_rollout_columns = sorted(
                required_rollout_columns - rollout_columns
            )
            if missing_rollout_columns:
                raise SystemExit(
                    "0011 reviewed_release_rollouts columns missing: "
                    f"{missing_rollout_columns}"
                )
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema='public'
                """
            )
            tables = {item[0] for item in cur.fetchall()}
            missing_tables = sorted(required_tables - tables)
            if missing_tables:
                raise SystemExit(
                    f"release schema tables missing: {missing_tables}"
                )
            for table_name, expected_columns in (
                required_order_management_columns.items()
            ):
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public'
                      AND table_name=%s
                    """,
                    (table_name,),
                )
                actual_columns = {item[0] for item in cur.fetchall()}
                missing = sorted(expected_columns - actual_columns)
                if missing:
                    raise SystemExit(
                        f"0005 {table_name} columns missing: {missing}"
                    )
            cur.execute(
                """
                SELECT conname
                FROM pg_constraint
                WHERE conname = ANY(%s)
                """,
                (list(required_constraints),),
            )
            constraints = {item[0] for item in cur.fetchall()}
            missing_constraints = sorted(
                required_constraints - constraints
            )
            if missing_constraints:
                raise SystemExit(
                    f"0011-0013 constraints missing: {missing_constraints}"
                )
            cur.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname='public'
                  AND indexname = ANY(%s)
                """,
                (list(required_indexes),),
            )
            indexes = {item[0] for item in cur.fetchall()}
            missing_indexes = sorted(required_indexes - indexes)
            if missing_indexes:
                raise SystemExit(
                    f"0010-0013 indexes missing: {missing_indexes}"
                )
            cur.execute(
                """
                SELECT conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname IN (
                    'ck_reviewed_release_rollouts_phase',
                    'ck_reviewed_release_rollout_events_shape',
                    'ck_control_plane_maintenance_fence_evidence',
                    'ck_control_plane_maintenance_fence_event_evidence'
                )
                """
            )
            constraint_definitions = dict(cur.fetchall())
            for constraint_name in (
                "ck_reviewed_release_rollouts_phase",
                "ck_reviewed_release_rollout_events_shape",
                "ck_control_plane_maintenance_fence_evidence",
                "ck_control_plane_maintenance_fence_event_evidence",
            ):
                definition = constraint_definitions.get(
                    constraint_name,
                    "",
                )
                if "account-c" not in definition and (
                    constraint_name.startswith(
                        "ck_control_plane_maintenance"
                    )
                ):
                    raise SystemExit(
                        f"0013 {constraint_name} lacks account-c"
                    )
                if "account-d" not in definition and (
                    constraint_name.startswith(
                        "ck_control_plane_maintenance"
                    )
                ):
                    raise SystemExit(
                        f"0013 {constraint_name} lacks account-d"
                    )
                if (
                    constraint_name.startswith(
                        "ck_control_plane_maintenance"
                    )
                    and (
                        "jsonb_array_length(account_evidence) = 4"
                        not in definition
                    )
                ):
                    raise SystemExit(
                        f"0013 {constraint_name} is not an A-D exact-set"
                    )
                if (
                    constraint_name.startswith(
                        "ck_reviewed_release_rollout"
                    )
                    and "account_c_rollout" not in definition
                ):
                    raise SystemExit(
                        f"0013 {constraint_name} lacks account_c_rollout"
                    )
                if (
                    constraint_name.startswith(
                        "ck_reviewed_release_rollout"
                    )
                    and "account_d_rollout" not in definition
                ):
                    raise SystemExit(
                        f"0013 {constraint_name} lacks account_d_rollout"
                    )
            cur.execute(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE schemaname='public'
                  AND indexname='uq_reviewed_release_rollouts_active'
                """
            )
            active_rollout_index = cur.fetchone()
            if active_rollout_index is None:
                raise SystemExit("0013 active rollout index is missing")
            active_rollout_index_definition = active_rollout_index[0]
            for phase in (
                "account_a_canary",
                "account_b_rollout",
                "account_c_rollout",
                "account_d_rollout",
            ):
                if phase not in active_rollout_index_definition:
                    raise SystemExit(
                        f"0013 active rollout index lacks {phase}"
                    )
            for type_name in (
                "hermes_action_v1",
                "approved_trade_action_v1",
            ):
                cur.execute(
                    """
                    SELECT enumlabel
                    FROM pg_enum
                    JOIN pg_type ON pg_type.oid = pg_enum.enumtypid
                    WHERE pg_type.typname=%s
                    """,
                    (type_name,),
                )
                enum_values = {item[0] for item in cur.fetchall()}
                if "cancel_order" not in enum_values:
                    raise SystemExit(
                        f"0014 {type_name} lacks cancel_order"
                    )
            for version, name, _sql, digest in migrations:
                cur.execute(
                    """
                    SELECT version, name, up_sha256
                    FROM schema_migrations
                    WHERE version=%s
                    """,
                    (version,),
                )
                if cur.fetchone() != (version, name, digest):
                    raise SystemExit(
                        f"{version} migration tracking mismatch"
                    )
            if deploy_gate_mode == "maintenance_fence":
                cur.execute(
                    """
                    SELECT fence_id::text,
                           lease_version,
                           expires_at,
                           account_evidence
                    FROM acquire_control_plane_maintenance_fence(
                        %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        str(maintenance_fence_id),
                        "deploy",
                        maintenance_actor,
                        maintenance_owner_token,
                        maintenance_lease_seconds,
                        heartbeat_max_age_seconds,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    raise SystemExit(
                        "maintenance fence acquisition returned no row"
                    )
                fence_receipt = {
                    "schema_version": (
                        "trader-v3-maintenance-fence-receipt/v1"
                    ),
                    "fence_id": row[0],
                    "lease_version": int(row[1]),
                    "expires_at": row[2].isoformat(),
                    "account_evidence": row[3],
                    "account_evidence_sha256": hashlib.sha256(
                        json.dumps(
                            row[3],
                            ensure_ascii=True,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("utf-8")
                    ).hexdigest(),
                }

    if migration_commit_marker is not False:
        marker_payload = {
            "schema_version": "trader-v3-migration-commit-marker/v1",
            "database_schema_epoch": expected_epoch,
            "migrations": [
                {
                    "version": version,
                    "name": name,
                    "up_sha256": digest,
                }
                for version, name, _sql, digest in migrations
            ],
        }
        encoded_marker = (
            json.dumps(marker_payload, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        marker_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        marker_fd = os.open(
            migration_commit_marker,
            marker_flags,
            0o400,
        )
        try:
            marker_offset = 0
            while marker_offset < len(encoded_marker):
                marker_offset += os.write(
                    marker_fd,
                    encoded_marker[marker_offset:],
                )
            os.fsync(marker_fd)
        finally:
            os.close(marker_fd)
        marker_directory_fd = os.open(
            str(migration_commit_marker.parent),
            os.O_RDONLY,
        )
        try:
            os.fsync(marker_directory_fd)
        finally:
            os.close(marker_directory_fd)

    if deploy_gate_mode == "maintenance_fence":
        if fence_receipt is None:
            raise SystemExit("maintenance fence receipt is unavailable")
        encoded_fence_receipt = (
            json.dumps(fence_receipt, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        fence_state_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fence_state_fd = os.open(
            maintenance_fence_state,
            fence_state_flags,
            0o400,
        )
        try:
            fence_state_offset = 0
            while fence_state_offset < len(encoded_fence_receipt):
                fence_state_offset += os.write(
                    fence_state_fd,
                    encoded_fence_receipt[fence_state_offset:],
                )
            os.fsync(fence_state_fd)
        finally:
            os.close(fence_state_fd)
        fence_state_directory_fd = os.open(
            str(maintenance_fence_state.parent),
            os.O_RDONLY,
        )
        try:
            os.fsync(fence_state_directory_fd)
        finally:
            os.close(fence_state_directory_fd)

    with conn.cursor() as cur:
        for version, name, _sql, digest in migrations:
            cur.execute(
                """
                SELECT version, name, up_sha256
                FROM schema_migrations
                WHERE version=%s
                """,
                (version,),
            )
            if cur.fetchone() != (version, name, digest):
                raise SystemExit(
                    f"{version} migration was not durably committed"
                )
finally:
    conn.close()
print("database_schema_epoch=0018_projection_reliability")
PY
  if [ "$DEPLOY_GATE_MODE" = "maintenance_fence" ]; then
    load_maintenance_fence_state
  fi
}

capture_account_a_safety_snapshot() {
  local output="$1"
  local expected_hash="$2"
  local target_symbol="$3"
  local expected_portfolio_baseline_sha256="$4"
  local phase="$5"
  "$T/.venv-cp/bin/python" - \
    "$T/.env.v3" \
    "$RELEASE_MANIFEST" \
    "$DATABASE_SCHEMA_EPOCH" \
    "$APP_SCHEMA_EPOCH" \
    "$REDIS_SCHEMA_EPOCH" \
    "$expected_hash" \
    "$target_symbol" \
    "$expected_portfolio_baseline_sha256" \
    "$phase" \
    "$output" <<'PY'
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.request import urlopen

import psycopg2


def read_environment(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def require_fresh(value, now: datetime, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise SystemExit(f"account-a snapshot lacks {field_name}")
    age_seconds = (now - value).total_seconds()
    if age_seconds < -1 or age_seconds > 5:
        raise SystemExit(f"account-a {field_name} is stale")


def position_quantity(item: dict) -> Decimal:
    for key in (
        "quantity",
        "qty",
        "position_amt",
        "positionAmt",
        "size",
    ):
        if key not in item:
            continue
        try:
            return Decimal(str(item[key]))
        except InvalidOperation as exc:
            raise SystemExit(
                f"account-a position quantity is invalid: {key}"
            ) from exc
    raise SystemExit("account-a position lacks a quantity field")


def canonical_symbol(item: dict) -> str:
    raw = str(
        item.get("symbol")
        or item.get("instrument_id")
        or item.get("instrumentId")
        or ""
    ).strip().upper()
    return raw.replace("-PERP.BINANCE", "")


def normalize_position(item: dict) -> dict:
    return {
        "symbol": canonical_symbol(item),
        "position_side": str(
            item.get("positionSide")
            or item.get("position_side")
            or ""
        ).upper(),
        "quantity": format(position_quantity(item), "f"),
        "entry_price": str(
            item.get("entryPrice")
            or item.get("entry_price")
            or "0"
        ),
        "leverage": str(item.get("leverage") or ""),
        "margin_type": str(
            item.get("marginType")
            or item.get("margin_type")
            or ""
        ).lower(),
    }


def normalize_order(item: dict) -> dict:
    return {
        "symbol": canonical_symbol(item),
        "order_id": str(item.get("orderId") or item.get("order_id") or ""),
        "client_order_id": str(
            item.get("clientOrderId")
            or item.get("client_order_id")
            or ""
        ),
        "side": str(item.get("side") or "").upper(),
        "position_side": str(
            item.get("positionSide")
            or item.get("position_side")
            or ""
        ).upper(),
        "type": str(item.get("type") or item.get("order_type") or "").upper(),
        "time_in_force": str(
            item.get("timeInForce")
            or item.get("time_in_force")
            or ""
        ).upper(),
        "quantity": str(
            item.get("origQty")
            or item.get("quantity")
            or item.get("qty")
            or ""
        ),
        "price": str(item.get("price") or ""),
        "stop_price": str(
            item.get("stopPrice")
            or item.get("stop_price")
            or ""
        ),
        "reduce_only": bool(
            item.get("reduceOnly")
            or item.get("reduce_only")
            or False
        ),
        "close_position": bool(
            item.get("closePosition")
            or item.get("close_position")
            or False
        ),
    }


def canonical_hash(value) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


(
    env_path,
    manifest_path,
    database_epoch,
    app_epoch,
    redis_epoch,
    expected_hash,
    target_symbol,
    expected_portfolio_baseline_sha256,
    phase,
    output_path,
) = sys.argv[1:]
if len(expected_hash) != 64:
    raise SystemExit("signed account-a safety state hash is invalid")
target_symbol = target_symbol.strip().upper()
if not target_symbol:
    raise SystemExit("account-b gate target symbol is invalid")
if len(expected_portfolio_baseline_sha256) != 64:
    raise SystemExit("non-target portfolio baseline hash is invalid")
env = read_environment(Path(env_path))
database_url = env.get("DATABASE_URL", "")
if not database_url:
    raise SystemExit("DATABASE_URL is required for account-a safety gate")
manifest = json.load(open(manifest_path, encoding="utf-8"))
expected_identity = (
    manifest["release_id"],
    manifest["image_digest"],
    manifest["config_sha256"],
    manifest["dependency_lock_sha256"],
    database_epoch,
)

with urlopen("http://127.0.0.1:8081/ready", timeout=5) as response:
    ready = json.load(response)
if ready.get("ready") is not True:
    raise SystemExit("account-a /ready is not ready")
if str(ready.get("trading_state") or "").upper() != "HALTED":
    raise SystemExit("account-a /ready is not HALTED")

conn = psycopg2.connect(database_url)
try:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT node_id,
                       status,
                       payload,
                       release_id,
                       image_digest,
                       config_sha256,
                       dependency_lock_sha256,
                       schema_epoch,
                       positions,
                       regular_orders,
                       algo_orders,
                       positions_snapshot_at,
                       regular_orders_snapshot_at,
                       algo_orders_snapshot_at,
                       reconciliation_completed_at,
                       last_seen_at,
                       now()
                FROM node_heartbeats
                WHERE account_id='account-a'
                  AND last_seen_at >= now() - interval '5 seconds'
                ORDER BY last_seen_at DESC
                FOR SHARE
                """
            )
            rows = cur.fetchall()
            if len(rows) != 1:
                raise SystemExit(
                    f"account-a requires one fresh heartbeat, found {len(rows)}"
                )
            (
                node_id,
                status,
                payload,
                release_id,
                image_digest,
                config_sha256,
                dependency_lock_sha256,
                schema_epoch,
                positions,
                regular_orders,
                algo_orders,
                positions_snapshot_at,
                regular_orders_snapshot_at,
                algo_orders_snapshot_at,
                reconciliation_completed_at,
                last_seen_at,
                database_now,
            ) = rows[0]
            cur.execute(
                """
                SELECT image_digest,
                       config_sha256,
                       dependency_lock_sha256,
                       schema_epoch
                FROM reviewed_release_manifests
                WHERE account_id='account-a'
                  AND release_id=%s
                  AND review_status='reviewed'
                """,
                (manifest["release_id"],),
            )
            reviewed = cur.fetchone()
            cur.execute(
                """
                SELECT count(*)
                FROM production_incidents
                WHERE account_id='account-a'
                  AND status='open'
                  AND severity IN ('P0', 'P1')
                """
            )
            open_incidents = int(cur.fetchone()[0])
            cur.execute(
                """
        SELECT version, name
        FROM schema_migrations
        WHERE version = ANY(%s)
        """,
        (["0010", "0011", "0012", "0013", "0014", "0015"],),
    )
            applied_migrations = dict(cur.fetchall())
finally:
    conn.close()

if str(status or "").upper() != "HALTED":
    raise SystemExit("account-a heartbeat is not HALTED")
if not isinstance(payload, dict) or payload.get("readiness") is not True:
    raise SystemExit("account-a heartbeat readiness is invalid")
if str(payload.get("reconciliation_state") or "").lower() != "healthy":
    raise SystemExit("account-a reconciliation is unhealthy")
try:
    projection_lag_ms = int(payload.get("projection_lag_ms"))
except (TypeError, ValueError) as exc:
    raise SystemExit("account-a projection lag is invalid") from exc
if projection_lag_ms < 0 or projection_lag_ms > 5000:
    raise SystemExit("account-a projection lag is unhealthy")
for value, field_name in (
    (last_seen_at, "heartbeat"),
    (positions_snapshot_at, "positions snapshot"),
    (regular_orders_snapshot_at, "regular orders snapshot"),
    (algo_orders_snapshot_at, "algo orders snapshot"),
    (reconciliation_completed_at, "reconciliation"),
):
    require_fresh(value, database_now, field_name)
heartbeat_identity = (
    release_id,
    image_digest,
    config_sha256,
    dependency_lock_sha256,
    schema_epoch,
)
if heartbeat_identity != expected_identity:
    raise SystemExit("account-a heartbeat release identity mismatch")
if reviewed != expected_identity[1:]:
    raise SystemExit("account-a reviewed release identity mismatch")
if applied_migrations != {
    "0010": "evidence_and_poll_indexes",
    "0011": "live_safety",
    "0012": "control_plane_maintenance_fence",
    "0013": "four_account_rollout",
    "0014": "cancel_order_contract",
    "0015": "refresh_evidence_command",
}:
    raise SystemExit("account-a database schema epoch mismatch")
if not isinstance(positions, list):
    raise SystemExit("account-a positions snapshot is invalid")
if not isinstance(regular_orders, list):
    raise SystemExit("account-a regular orders snapshot is invalid")
if not isinstance(algo_orders, list):
    raise SystemExit("account-a algo orders snapshot is invalid")
target_positions = []
non_target_positions = []
for position in positions:
    if not isinstance(position, dict):
        raise SystemExit("account-a position snapshot entry is invalid")
    symbol = canonical_symbol(position)
    quantity = position_quantity(position)
    if symbol == target_symbol:
        target_positions.append(normalize_position(position))
        if quantity != 0:
            raise SystemExit("account-a target symbol is not flat")
    elif quantity != 0:
        non_target_positions.append(normalize_position(position))
target_regular_orders = []
non_target_regular_orders = []
for order in regular_orders:
    if not isinstance(order, dict):
        raise SystemExit("account-a regular order entry is invalid")
    normalized_order = normalize_order(order)
    if canonical_symbol(order) == target_symbol:
        target_regular_orders.append(normalized_order)
    else:
        non_target_regular_orders.append(normalized_order)
target_algo_orders = []
non_target_algo_orders = []
for order in algo_orders:
    if not isinstance(order, dict):
        raise SystemExit("account-a algo order entry is invalid")
    normalized_order = normalize_order(order)
    if canonical_symbol(order) == target_symbol:
        target_algo_orders.append(normalized_order)
    else:
        non_target_algo_orders.append(normalized_order)
if target_regular_orders:
    raise SystemExit("account-a target symbol has regular open orders")
if target_algo_orders:
    raise SystemExit("account-a target symbol has algo open orders")
if open_incidents:
    raise SystemExit("account-a has an open P0/P1 incident")

non_target_portfolio = {
    "positions": sorted(
        non_target_positions,
        key=lambda item: json.dumps(
            item,
            sort_keys=True,
            separators=(",", ":"),
        ),
    ),
    "regular_orders": sorted(
        non_target_regular_orders,
        key=lambda item: json.dumps(
            item,
            sort_keys=True,
            separators=(",", ":"),
        ),
    ),
    "algo_orders": sorted(
        non_target_algo_orders,
        key=lambda item: json.dumps(
            item,
            sort_keys=True,
            separators=(",", ":"),
        ),
    ),
}
portfolio_baseline_sha256 = canonical_hash(non_target_portfolio)
if portfolio_baseline_sha256 != expected_portfolio_baseline_sha256:
    raise SystemExit(
        "account-a non-target portfolio baseline changed"
    )
safety_state = {
    "account_id": "account-a",
    "node_id": node_id,
    "trading_state": "HALTED",
    "release_id": release_id,
    "image_digest": image_digest,
    "config_sha256": config_sha256,
    "dependency_lock_sha256": dependency_lock_sha256,
    "database_schema_epoch": database_epoch,
    "app_schema_epoch": app_epoch,
    "redis_schema_epoch": redis_epoch,
    "ready": True,
    "reconciliation_state": "healthy",
    "target_symbol": target_symbol,
    "target_symbol_flat": True,
    "target_symbol_positions": sorted(
        target_positions,
        key=lambda item: json.dumps(
            item,
            sort_keys=True,
            separators=(",", ":"),
        ),
    ),
    "target_symbol_regular_orders": [],
    "target_symbol_algo_orders": [],
    "non_target_portfolio_baseline_sha256": portfolio_baseline_sha256,
    "no_open_p0_p1_incidents": True,
}
safety_hash = canonical_hash(safety_state)
if safety_hash != expected_hash:
    raise SystemExit(
        "account-a live safety state differs from signed evidence"
    )
snapshot = {
    "schema_version": "trader-v3-account-a-safety-snapshot/v2",
    "phase": phase,
    "captured_at": database_now.isoformat(),
    "last_seen_at": last_seen_at.isoformat(),
    "positions_snapshot_at": positions_snapshot_at.isoformat(),
    "regular_orders_snapshot_at": regular_orders_snapshot_at.isoformat(),
    "algo_orders_snapshot_at": algo_orders_snapshot_at.isoformat(),
    "reconciliation_completed_at": reconciliation_completed_at.isoformat(),
    "projection_lag_ms": projection_lag_ms,
    "safety_state": safety_state,
    "safety_state_sha256": safety_hash,
    "non_target_portfolio": non_target_portfolio,
}
Path(output_path).write_text(
    json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(f"account_a_safety_state_sha256={safety_hash}")
PY
}

# Transition mode writes reviewed bundle files to container-patches. Immutable
# mode leaves every business Python host source untouched.
CHANGED_CONTAINER=()
NEW_CONTAINER=()
if [ "$DELIVERY_MODE" = "transition_bind_mount" ]; then
  while IFS=$'\t' read -r bundle_path; do
    live="$T/container-patches/$bundle_path"
    if [ ! -f "$live" ]; then
      case "$bundle_path" in
        entry_batch.py|account_execution_ledger.py|idempotency.py|identifiers.py|owned_order_recovery.py|execution_domain_init.py|approved_intent_client.py|bounded_task_worker.py|control_plane_session.py|health.py|health_server.py|run_node.py|reconciliation.py|redis_safety.py|live_canary_execution.py|node_config.py|risk_config.py|risk_init.py|projection_spool.py|nautilus_config.py|persistence_init.py|redis_namespace_lease.py|redis_resp_client.py)
          NEW_CONTAINER+=("$bundle_path")
          CHANGED_CONTAINER+=("$bundle_path")
          continue
          ;;
        *)
          die "live container-patch missing: $live (unapproved NEW file)"
          ;;
      esac
    fi
    if ! cmp -s "$bundle_path" "$live"; then
      CHANGED_CONTAINER+=("$bundle_path")
    fi
  done < <(python3 -c "import json;[print(f['bundle_path']) for f in json.load(open('bundle-manifest.json'))['files']]")
fi

CHANGED_HOST=()
cmp -s host/read_api.py "$API_TGT" || CHANGED_HOST+=("read_api")
cmp -s host/snapshot.py "$SNAPSHOT_TGT" || CHANGED_HOST+=("snapshot")
cmp -s host/decision_gateway/gateway.py "$DECISION_GATEWAY_TGT" \
  || CHANGED_HOST+=("decision_gateway")
cmp -s host/exchange_state_recorder.py "$EXCHANGE_STATE_RECORDER_TGT" \
  || CHANGED_HOST+=("exchange_state_recorder")
cmp -s host/hermes_signal_feeder.py "$HERMES_FEEDER_TGT" \
  || CHANGED_HOST+=("hermes_signal_feeder")
cmp -s host/v3_trade.py "$HERMES_V3_TRADE_TGT" \
  || CHANGED_HOST+=("hermes_v3_trade")
cmp -s host/v3-trader/SKILL.md "$HERMES_V3_SKILL_TGT" \
  || CHANGED_HOST+=("hermes_v3_skill")
cmp -s host/system_snapshot.v1.json "$SYSTEM_SNAPSHOT_SCHEMA_TGT" \
  || CHANGED_HOST+=("system_snapshot_schema")
cmp -s host/execution_domain/__init__.py "$EXECUTION_DOMAIN_INIT_TGT" \
  || CHANGED_HOST+=("execution_domain_init")
cmp -s host/execution_domain/contracts.py "$EXECUTION_DOMAIN_CONTRACTS_TGT" \
  || CHANGED_HOST+=("execution_domain_contracts")
cmp -s host/execution_domain/portfolio_baseline.py \
  "$EXECUTION_DOMAIN_PORTFOLIO_BASELINE_TGT" \
  || CHANGED_HOST+=("execution_domain_portfolio_baseline")
cmp -s host/execution_domain/account_execution_ledger.py \
  "$EXECUTION_DOMAIN_ACCOUNT_EXECUTION_LEDGER_TGT" \
  || CHANGED_HOST+=("execution_domain_account_execution_ledger")
cmp -s host/execution_domain/entry_batch.py \
  "$EXECUTION_DOMAIN_ENTRY_BATCH_TGT" \
  || CHANGED_HOST+=("execution_domain_entry_batch")
cmp -s host/execution_domain/order_ownership.py \
  "$EXECUTION_DOMAIN_ORDER_OWNERSHIP_TGT" \
  || CHANGED_HOST+=("execution_domain_order_ownership")
cmp -s host/execution_domain/idempotency.py \
  "$EXECUTION_DOMAIN_IDEMPOTENCY_TGT" \
  || CHANGED_HOST+=("execution_domain_idempotency")
cmp -s host/execution_domain/identifiers.py \
  "$EXECUTION_DOMAIN_IDENTIFIERS_TGT" \
  || CHANGED_HOST+=("execution_domain_identifiers")
for settings_file in "${SETTINGS_PACKAGE_FILES[@]}"; do
  if ! cmp -s "host/settings/$settings_file" \
    "$SETTINGS_PACKAGE_TGT/$settings_file"; then
    CHANGED_HOST+=("settings_package")
    break
  fi
done
for audit_file in "${AUDIT_PACKAGE_FILES[@]}"; do
  if ! cmp -s "host/audit/$audit_file" \
    "$AUDIT_PACKAGE_TGT/$audit_file"; then
    CHANGED_HOST+=("audit_package")
    break
  fi
done
for om_file in "${ORDER_MANAGEMENT_PACKAGE_FILES[@]}"; do
  if ! cmp -s "host/order_management/$om_file" \
    "$ORDER_MANAGEMENT_PACKAGE_TGT/$om_file"; then
    CHANGED_HOST+=("order_management_package")
    break
  fi
done
for obs_file in "${OBSERVABILITY_PACKAGE_FILES[@]}"; do
  if ! cmp -s "host/observability/$obs_file" \
    "$OBSERVABILITY_PACKAGE_TGT/$obs_file"; then
    CHANGED_HOST+=("observability_package")
    break
  fi
done
cmp -s host/execution_domain/control_plane.py \
  "$EXECUTION_DOMAIN_CONTROL_PLANE_TGT" \
  || CHANGED_HOST+=("execution_domain_control_plane")
cmp -s host/app_roles.py "$APP_ROLES_TGT" \
  || CHANGED_HOST+=("app_roles")
cmp -s host/pools.py "$DB_POOLS_TGT" || CHANGED_HOST+=("pools")
cmp -s host/repository.py "$DB_REPOSITORY_TGT" \
  || CHANGED_HOST+=("repository")
cmp -s scripts/rebuild_orders_projection.py \
  "$REBUILD_ORDERS_PROJECTION_TGT" \
  || CHANGED_HOST+=("rebuild_orders_projection")
CHANGED_WATCHER_COUNT="$(watcher_runtime_change_count)"
[[ "$CHANGED_WATCHER_COUNT" =~ ^[0-9]+$ ]] \
  || die "watcher runtime change count is invalid"

echo "== changed container files: ${CHANGED_CONTAINER[*]:-none}"
echo "== new transition files: ${NEW_CONTAINER[*]:-none}"
echo "== changed host files: ${CHANGED_HOST[*]:-none}"
echo "== changed watcher files: $CHANGED_WATCHER_COUNT"
if [ "$DELIVERY_MODE" = "transition_bind_mount" ]; then
  [ "${#CHANGED_CONTAINER[@]}" -gt 0 ] \
    || [ "${#CHANGED_HOST[@]}" -gt 0 ] \
    || [ "$CHANGED_WATCHER_COUNT" -gt 0 ] \
    || [ "$WATCHER_SCHEMA_RESTART_REQUIRED" = "1" ] \
    || [ "$CONTROL_PLANE_ISOLATION_REQUIRED" = "1" ] \
    || { echo "nothing to deploy"; exit 0; }
fi

# ---------- online quiesce function ----------
quiesce_rollout_account() {
  if [[ "$DEPLOY_GATE_MODE" =~ ^(bootstrap(_resume)?|migration_rebaseline)_stopped$ ]]; then
    verify_maintenance_fence "bootstrap-pre-release"
  elif "$T/.venv-cp/bin/python" - \
  "$T/.env.v3" "$ROLLOUT_ACCOUNT" "$ROLLOUT_PORT" <<'PY'
from datetime import datetime, timezone
import json, sys, time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
import psycopg2

def read_environment(path):
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in {"'", '"'}:
            v = v[1:-1]
        values[k.strip()] = v
    return values

env = read_environment(sys.argv[1])
rollout_account = sys.argv[2]
rollout_port = int(sys.argv[3])
db, token = env.get("DATABASE_URL", ""), env.get("RISK_ADMIN_TOKEN", "")
if not db or not token:
    raise SystemExit("DATABASE_URL/RISK_ADMIN_TOKEN missing")
conn = psycopg2.connect(db); conn.autocommit = True
try:
    with conn.cursor() as cur:
        cur.execute("SELECT node_id FROM node_heartbeats WHERE last_seen_at >= now() - interval '5 minutes' ORDER BY node_id")
        candidates = [r[0] for r in cur.fetchall()]
    account_token = rollout_account.replace("-", "").lower()
    nodes = [
        node
        for node in candidates
        if account_token in node.replace("-", "").replace("_", "").lower()
    ]
    if len(nodes) != 1:
        raise SystemExit(
            f"expected one fresh {rollout_account} heartbeat, found {nodes}"
        )
    rid = f"deploy-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}-halt"
    body = json.dumps({
        "type": "HALT",
        "reason": "account-stall hardening release deployment",
        "confirm": True,
        "request_id": rid,
        "target_nodes": nodes,
        "scope": {"account_id": rollout_account},
    }).encode()
    req = Request("http://127.0.0.1:8080/v1/commands", data=body, method="POST",
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-Request-Id": rid})
    try:
        with urlopen(req, timeout=15) as r:
            cid = json.load(r).get("command_id")
    except (HTTPError, URLError) as e:
        raise SystemExit(f"HALT request failed: {e}")
    if not cid:
        raise SystemExit("HALT command id missing")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        states = {}
        for port in (rollout_port,):
            try:
                with urlopen(f"http://127.0.0.1:{port}/ready", timeout=5) as r:
                    states[port] = json.load(r).get("trading_state")
            except (OSError, URLError, ValueError) as e:
                states[port] = str(e)
        with conn.cursor() as cur:
            cur.execute("SELECT node_id, status FROM command_node_acks WHERE command_id=%s", (cid,))
            acks = cur.fetchall()
            cur.execute("SELECT status FROM operator_commands WHERE command_id=%s", (cid,))
            row = cur.fetchone()
        accepted_ack_statuses = {"acked", "completed"}
        accepted_command_statuses = {"acknowledged", "completed"}
        if set(states.values()) == {"HALTED"} and len(acks) == 1 \
           and all(s in accepted_ack_statuses for _, s in acks) \
           and row and row[0] in accepted_command_statuses:
            print(f"HALT acked by {nodes}; command_id={cid}")
            raise SystemExit(0)
        time.sleep(2)
    raise SystemExit(f"HALT acceptance timed out: {states}")
finally:
    conn.close()
PY
  then
    echo "== HALT command acknowledged by $ROLLOUT_NODE"
  else
    die "HALT ACK gate failed; execution containers remain online"
  fi
}

# ---------- reviewed live risk config ----------
[ ! -e "$BACKUP_ROOT" ] || die "backup path exists: $BACKUP_ROOT"
mkdir -p "$BACKUP_ROOT"
chmod 0700 "$BACKUP_ROOT"
verify_four_channel_account_mapping \
  "$FOUR_CHANNEL_MAPPING_EVIDENCE" \
  pre_restart
CONFIG_ARTIFACT_A=""
CONFIG_ARTIFACT_B=""
CONFIG_ARTIFACT_C=""
CONFIG_ARTIFACT_D=""
if [ "$ROLLOUT_NODE" = "trader-v3-node-a" ] \
  && [ "$DEPLOY_GATE_MODE" != "bootstrap_resume_stopped" ]; then
  python3 "$LIVE_NODE_CONFIG_TOOL" capture \
    --policy "$LIVE_RISK_POLICY" \
    --output "$LEGACY_RISK_CAPTURE" \
    --config "account-a=$NODE_CONFIG_A" \
    --config "account-b=$NODE_CONFIG_B" \
    --config "account-c=$NODE_CONFIG_C" \
    --config "account-d=$NODE_CONFIG_D" \
    --container "account-a=trader-v3-node-a" \
    --container "account-b=trader-v3-node-b" \
    --container "account-c=trader-v3-node-c" \
    --container "account-d=trader-v3-node-d" \
    --expected-owner-uid 0
  RELEASE_BOUND_SOURCE_A="$BACKUP_ROOT/account-a-release-bound-source.json"
  RELEASE_BOUND_SOURCE_B="$BACKUP_ROOT/account-b-release-bound-source.json"
  RELEASE_BOUND_SOURCE_C="$BACKUP_ROOT/account-c-release-bound-source.json"
  RELEASE_BOUND_SOURCE_D="$BACKUP_ROOT/account-d-release-bound-source.json"
  prepare_release_bound_config_source \
    "$NODE_CONFIG_A" \
    "$RELEASE_BOUND_SOURCE_A"
  prepare_release_bound_config_source \
    "$NODE_CONFIG_B" \
    "$RELEASE_BOUND_SOURCE_B"
  prepare_release_bound_config_source \
    "$NODE_CONFIG_C" \
    "$RELEASE_BOUND_SOURCE_C"
  prepare_release_bound_config_source \
    "$NODE_CONFIG_D" \
    "$RELEASE_BOUND_SOURCE_D"
  python3 "$LIVE_NODE_CONFIG_TOOL" prepare-target \
    --account-id account-a \
    --source-config "$RELEASE_BOUND_SOURCE_A" \
    --policy "$LIVE_RISK_POLICY" \
    --legacy-risk "$LEGACY_RISK_CAPTURE" \
    --artifact-root "$CONFIG_ARTIFACT_ROOT" \
    --record-output "$CONFIG_RECORD_A" \
    --expected-owner-uid 0
  python3 "$LIVE_NODE_CONFIG_TOOL" prepare-target \
    --account-id account-b \
    --source-config "$RELEASE_BOUND_SOURCE_B" \
    --policy "$LIVE_RISK_POLICY" \
    --legacy-risk "$LEGACY_RISK_CAPTURE" \
    --artifact-root "$CONFIG_ARTIFACT_ROOT" \
    --record-output "$CONFIG_RECORD_B" \
    --expected-owner-uid 0
  python3 "$LIVE_NODE_CONFIG_TOOL" prepare-target \
    --account-id account-c \
    --source-config "$RELEASE_BOUND_SOURCE_C" \
    --policy "$LIVE_RISK_POLICY" \
    --legacy-risk "$LEGACY_RISK_CAPTURE" \
    --artifact-root "$CONFIG_ARTIFACT_ROOT" \
    --record-output "$CONFIG_RECORD_C" \
    --expected-owner-uid 0
  python3 "$LIVE_NODE_CONFIG_TOOL" prepare-target \
    --account-id account-d \
    --source-config "$RELEASE_BOUND_SOURCE_D" \
    --policy "$LIVE_RISK_POLICY" \
    --legacy-risk "$LEGACY_RISK_CAPTURE" \
    --artifact-root "$CONFIG_ARTIFACT_ROOT" \
    --record-output "$CONFIG_RECORD_D" \
    --expected-owner-uid 0
  python3 "$LIVE_NODE_CONFIG_TOOL" verify-target \
    --record "$CONFIG_RECORD_A" \
    --expected-owner-uid 0
  python3 "$LIVE_NODE_CONFIG_TOOL" verify-target \
    --record "$CONFIG_RECORD_B" \
    --expected-owner-uid 0
  python3 "$LIVE_NODE_CONFIG_TOOL" verify-target \
    --record "$CONFIG_RECORD_C" \
    --expected-owner-uid 0
  python3 "$LIVE_NODE_CONFIG_TOOL" verify-target \
    --record "$CONFIG_RECORD_D" \
    --expected-owner-uid 0
  CONFIG_ARTIFACT_A="$(
    python3 -c \
      'import json,sys; print(json.load(open(sys.argv[1]))["host_path"])' \
      "$CONFIG_RECORD_A"
  )"
  CONFIG_ARTIFACT_B="$(
    python3 -c \
      'import json,sys; print(json.load(open(sys.argv[1]))["host_path"])' \
      "$CONFIG_RECORD_B"
  )"
  CONFIG_ARTIFACT_C="$(
    python3 -c \
      'import json,sys; print(json.load(open(sys.argv[1]))["host_path"])' \
      "$CONFIG_RECORD_C"
  )"
  CONFIG_ARTIFACT_D="$(
    python3 -c \
      'import json,sys; print(json.load(open(sys.argv[1]))["host_path"])' \
      "$CONFIG_RECORD_D"
  )"
  # The node container reads /cfg.json as the image's nautilus user
  # (uid 999); the artifacts are written read-only for root under
  # umask 077, so hand them to that uid without widening the mode.
  chown 999:999 "$CONFIG_ARTIFACT_ROOT"/account-*/*.json \
    || die "cannot assign config artifacts to the container user"
  if [ -n "$RELEASE_CONFIG_ARTIFACT_ROOT" ]; then
    [ -d "$RELEASE_CONFIG_ARTIFACT_ROOT" ] \
      || die "release config artifact root is invalid"
    [ ! -L "$RELEASE_CONFIG_ARTIFACT_ROOT" ] \
      || die "release config artifact root cannot be a symlink"
    CONFIG_ARTIFACT_A="$RELEASE_CONFIG_ARTIFACT_ROOT/account-a/509b019b7a97c608da807be9b01dda60b4d65193296e93c7d01d6d8359be04a9.json"
    CONFIG_ARTIFACT_B="$RELEASE_CONFIG_ARTIFACT_ROOT/account-b/860fa60888b100d9c0e97b62333b079be91c7b274b2ee26595b40b62396e46f8.json"
    CONFIG_ARTIFACT_C="$RELEASE_CONFIG_ARTIFACT_ROOT/account-c/26c20be35cb4fbf0511e8d19c227cbe2c5226f0687523cdcc0fc47dde3e3e297.json"
    CONFIG_ARTIFACT_D="$RELEASE_CONFIG_ARTIFACT_ROOT/account-d/914d12d7d87d79f243ac28bc680e097dcb47f389d26c6d589813654dbf196b04.json"
    for artifact in \
      "$CONFIG_ARTIFACT_A" \
      "$CONFIG_ARTIFACT_B" \
      "$CONFIG_ARTIFACT_C" \
      "$CONFIG_ARTIFACT_D"
    do
      [ -f "$artifact" ] || die "release config artifact is missing: $artifact"
      [ ! -L "$artifact" ] || die "release config artifact cannot be a symlink: $artifact"
    done
    echo "== release capture will use pinned config artifacts: $RELEASE_CONFIG_ARTIFACT_ROOT"
  fi
  echo "== immutable reviewed account-a through account-d config artifacts prepared"
elif [ "$DEPLOY_GATE_MODE" = "bootstrap_resume_stopped" ]; then
  echo "== bootstrap resume will reuse the signed account-a release manifest"
else
  echo "== fleet rollout will reuse account-a release config artifacts"
fi

# Build the reviewed derived image from the local content-addressed base. The
# context contains only numbered bundle payload files and a generated
# Dockerfile; network access is disabled.
TARGET_IMAGE=""
MANIFEST_SOURCE_ROOT="$T/container-patches"
CAPTURE_IMAGE_ARGS=()
CAPTURE_PURPOSE_ARGS=()
if [ "$EMERGENCY_ROLLBACK" = "1" ]; then
  CAPTURE_PURPOSE_ARGS=(--emergency-rollback)
fi
ACCOUNT_B_EVIDENCE_COPY=""
ACCOUNT_B_SIGNATURE_COPY=""
ACCOUNT_B_REVIEWER_PUBLIC_KEY=""
ACCOUNT_A_EXPECTED_SAFETY_STATE_SHA256=""
ACCOUNT_A_TARGET_SYMBOL=""
ACCOUNT_A_NON_TARGET_PORTFOLIO_BASELINE_SHA256=""
ACCOUNT_A_PRE_SNAPSHOT=""
ACCOUNT_A_POST_SNAPSHOT=""
PRIOR_CLOSURE_ACCOUNT=""
PRIOR_CLOSURE_REPORT_SOURCE=""
PRIOR_CLOSURE_SIGNATURE_SOURCE=""
PRIOR_CLOSURE_REPORT_COPY=""
PRIOR_CLOSURE_SIGNATURE_COPY=""
PRIOR_CLOSURE_PUBLIC_KEY=""
if [ "$DEPLOY_GATE_MODE" = "bootstrap_resume_stopped" ]; then
  cp "$ACCOUNT_A_RELEASE_MANIFEST" "$RELEASE_MANIFEST"
  TARGET_IMAGE="$(
    python3 -c \
      'import json,sys; print(json.load(open(sys.argv[1]))["image_digest"])' \
      "$RELEASE_MANIFEST"
  )"
  docker image inspect "$TARGET_IMAGE" >/dev/null \
    || die "bootstrap resume target image is unavailable locally"
elif [ "$ROLLOUT_NODE" = "trader-v3-node-b" ]; then
  if [ "$EMERGENCY_ROLLBACK" = "0" ]; then
    [ "$DELIVERY_MODE" = "immutable_image" ] \
      || die "account-b rollout requires immutable_image"
    [ "${ACCOUNT_B_ROLLOUT_GATE:-0}" = "1" ] \
      || die "account-b rollout requires ACCOUNT_B_ROLLOUT_GATE=1"
    refresh_account_b_short_lived_evidence
  fi
  [ -f "${ACCOUNT_B_RELEASE_MANIFEST:-}" ] \
    || die "account-b rollout requires ACCOUNT_B_RELEASE_MANIFEST"
  if [ "$EMERGENCY_ROLLBACK" = "0" ]; then
    [ -f "${ACCOUNT_B_EVIDENCE_FILE:-}" ] \
      || die "account-b rollout requires ACCOUNT_B_EVIDENCE_FILE"
    [ ! -L "$ACCOUNT_B_EVIDENCE_FILE" ] \
      || die "account-b evidence file cannot be a symlink"
    [ -f "${ACCOUNT_B_EVIDENCE_SIGNATURE:-}" ] \
      || die "account-b rollout requires ACCOUNT_B_EVIDENCE_SIGNATURE"
    [ ! -L "$ACCOUNT_B_EVIDENCE_SIGNATURE" ] \
      || die "account-b evidence signature cannot be a symlink"
  fi
  cp "${ACCOUNT_B_RELEASE_MANIFEST}" "$RELEASE_MANIFEST"
  TARGET_IMAGE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image_digest"])' "$RELEASE_MANIFEST")"
  docker image inspect "$TARGET_IMAGE" >/dev/null \
    || die "account-b target image is unavailable locally"
elif [ "$ROLLOUT_NODE" = "trader-v3-node-c" ] \
  || [ "$ROLLOUT_NODE" = "trader-v3-node-d" ]; then
  [ -f "${ACCOUNT_FLEET_RELEASE_MANIFEST:-}" ] \
    || die "$ROLLOUT_ACCOUNT rollout requires ACCOUNT_FLEET_RELEASE_MANIFEST"
  cp "${ACCOUNT_FLEET_RELEASE_MANIFEST}" "$RELEASE_MANIFEST"
  TARGET_IMAGE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image_digest"])' "$RELEASE_MANIFEST")"
  docker image inspect "$TARGET_IMAGE" >/dev/null \
    || die "$ROLLOUT_ACCOUNT target image is unavailable locally"
elif [ -n "$PINNED_RELEASE_MANIFEST" ]; then
  [ -f "$PINNED_RELEASE_MANIFEST" ] \
    || die "pinned release manifest is invalid"
  [ ! -L "$PINNED_RELEASE_MANIFEST" ] \
    || die "pinned release manifest cannot be a symlink"
  cp "$PINNED_RELEASE_MANIFEST" "$RELEASE_MANIFEST"
  TARGET_IMAGE="$(
    python3 -c \
      'import json,sys; print(json.load(open(sys.argv[1]))["image_digest"])' \
      "$RELEASE_MANIFEST"
  )"
  docker image inspect "$TARGET_IMAGE" >/dev/null \
    || die "pinned release target image is unavailable locally"
  echo "== release capture will reuse pinned manifest: $PINNED_RELEASE_MANIFEST"
else
  if [ "$DELIVERY_MODE" = "immutable_image" ]; then
    BASE_IMAGE_ARGS=()
    for node in "${ALL_NODES[@]}"; do
      base_image="$(docker inspect --format '{{.Image}}' "$node")"
      [ -n "$base_image" ] \
        || die "source image digest is missing before account-a canary: $node"
      BASE_IMAGE_ARGS+=(--image "$base_image")
    done
    BASE_IMAGE_A="$(
      python3 "$IMMUTABLE_BUILDER" \
        resolve-common-base \
        "${BASE_IMAGE_ARGS[@]}"
    )" || die "common immutable base resolution failed"
    [ -n "$BASE_IMAGE_A" ] \
      || die "common immutable base image digest is missing"
    DERIVED_IID="$STAGING/derived-image.id"
    rm -f "$DERIVED_IID"
    python3 "$IMMUTABLE_BUILDER" \
      --bundle-manifest "$STAGING/bundle-manifest.json" \
      --dependency-lock "$DEPENDENCY_LOCK" \
      --base-image "$BASE_IMAGE_A" \
      --iid-output "$DERIVED_IID"
    TARGET_IMAGE="$(tr -d '[:space:]' < "$DERIVED_IID")"
    [ -n "$TARGET_IMAGE" ] || die "derived image ID missing"
    MANIFEST_SOURCE_ROOT="$STAGING"
    CAPTURE_IMAGE_ARGS=(--image-digest "$TARGET_IMAGE")
  fi
  python3 "$RELEASE_TOOL" capture \
    --bundle-manifest "$STAGING/bundle-manifest.json" \
    --dependency-lock "$DEPENDENCY_LOCK" \
    --patch-root "$MANIFEST_SOURCE_ROOT" \
    --require-transition-runtime \
    --delivery-mode "$DELIVERY_MODE" \
    "${CAPTURE_IMAGE_ARGS[@]}" \
    "${CAPTURE_PURPOSE_ARGS[@]}" \
    --node-config \
      "account-a=trader-v3-node-a=$CONFIG_ARTIFACT_A" \
    --node-config \
      "account-b=trader-v3-node-b=$CONFIG_ARTIFACT_B" \
    --node-config \
      "account-c=trader-v3-node-c=$CONFIG_ARTIFACT_C" \
    --node-config \
      "account-d=trader-v3-node-d=$CONFIG_ARTIFACT_D" \
    --output "$RELEASE_MANIFEST"
fi
RELEASE_COMMIT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release_commit"])' "$RELEASE_MANIFEST")"
RELEASE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release_id"])' "$RELEASE_MANIFEST")"
python3 - \
  "$RELEASE_TOOL" \
  "$RELEASE_MANIFEST" \
  "$STAGING/bundle-manifest.json" \
  "$EMERGENCY_ROLLBACK" <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent))
import release_manifest

manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
emergency_rollback = sys.argv[4] == "1"
release_manifest.require_release_matches_bundle(
    manifest,
    Path(sys.argv[3]),
)
if manifest.get("schema_version") != release_manifest.SCHEMA_VERSION:
    raise SystemExit("release manifest must use immutable node config schema")
expected_purpose = release_manifest.RELEASE_PURPOSE_HARDENING
expected_delivery = release_manifest.DELIVERY_IMMUTABLE
if emergency_rollback:
    expected_purpose = release_manifest.RELEASE_PURPOSE_EMERGENCY_ROLLBACK
    expected_delivery = release_manifest.DELIVERY_TRANSITION
if manifest.get("release_purpose") != expected_purpose:
    raise SystemExit("release manifest purpose differs from deploy mode")
if manifest.get("delivery_mode") != expected_delivery:
    raise SystemExit("release manifest delivery differs from deploy mode")
expected = {
    ("account-a", "trader-v3-node-a"),
    ("account-b", "trader-v3-node-b"),
    ("account-c", "trader-v3-node-c"),
    ("account-d", "trader-v3-node-d"),
}
actual = {
    (item["account_id"], item["container"])
    for item in manifest["node_configs"]
}
if actual != expected:
    raise SystemExit("release manifest node config bindings differ")
PY
if [ "$EMERGENCY_ROLLBACK" = "0" ]; then
  case "$ROLLOUT_NODE" in
    trader-v3-node-b)
      PRIOR_CLOSURE_ACCOUNT="account-a"
      PRIOR_CLOSURE_REPORT_SOURCE="${ACCOUNT_A_CLOSURE_REPORT:-}"
      PRIOR_CLOSURE_SIGNATURE_SOURCE="${ACCOUNT_A_CLOSURE_SIGNATURE:-}"
      ;;
    trader-v3-node-c)
      PRIOR_CLOSURE_ACCOUNT="account-b"
      PRIOR_CLOSURE_REPORT_SOURCE="${ACCOUNT_B_CLOSURE_REPORT:-}"
      PRIOR_CLOSURE_SIGNATURE_SOURCE="${ACCOUNT_B_CLOSURE_SIGNATURE:-}"
      ;;
    trader-v3-node-d)
      PRIOR_CLOSURE_ACCOUNT="account-c"
      PRIOR_CLOSURE_REPORT_SOURCE="${ACCOUNT_C_CLOSURE_REPORT:-}"
      PRIOR_CLOSURE_SIGNATURE_SOURCE="${ACCOUNT_C_CLOSURE_SIGNATURE:-}"
      ;;
  esac
fi
if [ -n "$PRIOR_CLOSURE_ACCOUNT" ]; then
  [ -f "$PRIOR_CLOSURE_REPORT_SOURCE" ] \
    || die "$ROLLOUT_ACCOUNT rollout requires signed $PRIOR_CLOSURE_ACCOUNT closure report"
  [ ! -L "$PRIOR_CLOSURE_REPORT_SOURCE" ] \
    || die "$PRIOR_CLOSURE_ACCOUNT closure report cannot be a symlink"
  [ -f "$PRIOR_CLOSURE_SIGNATURE_SOURCE" ] \
    || die "$ROLLOUT_ACCOUNT rollout requires $PRIOR_CLOSURE_ACCOUNT closure signature"
  [ ! -L "$PRIOR_CLOSURE_SIGNATURE_SOURCE" ] \
    || die "$PRIOR_CLOSURE_ACCOUNT closure signature cannot be a symlink"
  PRIOR_CLOSURE_REPORT_COPY="$(mktemp)"
  PRIOR_CLOSURE_SIGNATURE_COPY="$(mktemp)"
  PRIOR_CLOSURE_PUBLIC_KEY="$(mktemp)"
  TEMP_FILES+=(
    "$PRIOR_CLOSURE_REPORT_COPY"
    "$PRIOR_CLOSURE_SIGNATURE_COPY"
    "$PRIOR_CLOSURE_PUBLIC_KEY"
  )
  cp -- "$PRIOR_CLOSURE_REPORT_SOURCE" "$PRIOR_CLOSURE_REPORT_COPY"
  cp -- "$PRIOR_CLOSURE_SIGNATURE_SOURCE" \
    "$PRIOR_CLOSURE_SIGNATURE_COPY"
  chmod 0400 \
    "$PRIOR_CLOSURE_REPORT_COPY" \
    "$PRIOR_CLOSURE_SIGNATURE_COPY"
  write_account_b_reviewer_public_key "$PRIOR_CLOSURE_PUBLIC_KEY"
fi
if [ "$ROLLOUT_NODE" = "trader-v3-node-b" ] \
  && [ "$EMERGENCY_ROLLBACK" = "0" ]; then
  require_rollout_phase account_a_canary
  command -v openssl >/dev/null || die "openssl missing"
  ACCOUNT_B_EVIDENCE_COPY="$(mktemp)"
  ACCOUNT_B_SIGNATURE_COPY="$(mktemp)"
  ACCOUNT_B_REVIEWER_PUBLIC_KEY="$(mktemp)"
  TEMP_FILES+=(
    "$ACCOUNT_B_EVIDENCE_COPY"
    "$ACCOUNT_B_SIGNATURE_COPY"
    "$ACCOUNT_B_REVIEWER_PUBLIC_KEY"
  )
  cp -- "$ACCOUNT_B_EVIDENCE_FILE" "$ACCOUNT_B_EVIDENCE_COPY"
  cp -- "$ACCOUNT_B_EVIDENCE_SIGNATURE" "$ACCOUNT_B_SIGNATURE_COPY"
  chmod 0400 "$ACCOUNT_B_EVIDENCE_COPY" "$ACCOUNT_B_SIGNATURE_COPY"
  write_account_b_reviewer_public_key "$ACCOUNT_B_REVIEWER_PUBLIC_KEY"
  openssl dgst -sha256 \
    -verify "$ACCOUNT_B_REVIEWER_PUBLIC_KEY" \
    -signature "$ACCOUNT_B_SIGNATURE_COPY" \
    "$ACCOUNT_B_EVIDENCE_COPY" >/dev/null \
    || die "account-b evidence signature verification failed"
  IFS=$'\t' read -r \
    ACCOUNT_A_EXPECTED_SAFETY_STATE_SHA256 \
    ACCOUNT_A_TARGET_SYMBOL \
    ACCOUNT_A_NON_TARGET_PORTFOLIO_BASELINE_SHA256 < <(
    python3 - \
      "$ACCOUNT_B_EVIDENCE_COPY" \
      "$ACCOUNT_B_EVIDENCE_FILE" \
      "$RELEASE_MANIFEST" \
      "$APP_SCHEMA_EPOCH" \
      "$DATABASE_SCHEMA_EPOCH" \
      "$REDIS_SCHEMA_EPOCH" \
      "$ACCOUNT_B_REVIEWER_PUBLIC_KEY_SHA256" <<'PY'
# ACCOUNT_B_EVIDENCE_VALIDATOR_BEGIN
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

(
    evidence_copy_raw,
    evidence_source_raw,
    release_manifest_raw,
    app_schema_epoch,
    database_schema_epoch,
    redis_schema_epoch,
    reviewer_public_key_sha256,
) = sys.argv[1:]
evidence_copy = Path(evidence_copy_raw)
evidence_source = Path(evidence_source_raw).resolve()
evidence_root = evidence_source.parent
source_stat = evidence_source.stat()
if source_stat.st_uid != os.geteuid():
    raise SystemExit("account-b evidence owner differs from deploy user")
if source_stat.st_mode & 0o022:
    raise SystemExit("account-b evidence is group/world writable")
expected_owner_uid = source_stat.st_uid
evidence = json.loads(evidence_copy.read_text(encoding="utf-8"))
manifest = json.loads(
    Path(release_manifest_raw).read_text(encoding="utf-8")
)
if evidence.get("schema_version") != "trader-v3-account-a-canary-evidence/v2":
    raise SystemExit("account-b evidence schema mismatch")
required = {
    "account_a_container": "trader-v3-node-a",
    "release_id": manifest["release_id"],
    "image_digest": manifest["image_digest"],
    "config_sha256": manifest["config_sha256"],
    "dependency_lock_sha256": manifest["dependency_lock_sha256"],
    "manifest_schema_version": manifest["schema_version"],
    "app_schema_epoch": app_schema_epoch,
    "database_schema_epoch": database_schema_epoch,
    "redis_schema_epoch": redis_schema_epoch,
    "rollout_phase": "account_a_canary",
    "reviewer_public_key_sha256": reviewer_public_key_sha256,
    "verify_live_passed": True,
    "canary_halted": True,
    "target_symbol_flat": True,
    "target_symbol_regular_orders_zero": True,
    "target_symbol_algo_orders_zero": True,
    "fault_gate_passed": True,
    "live_gate_passed": True,
    "no_open_p0_p1_incidents": True,
}
for key, expected in required.items():
    if evidence.get(key) != expected:
        raise SystemExit(f"account-b evidence mismatch: {key}")
if int(evidence.get("soak_seconds", 0)) < 1800:
    print(
        "!! gate warning [account-b-soak]: "
        "account-b evidence has less than 1800 soak seconds",
        file=sys.stderr,
    )
safety_hash = str(
    evidence.get("account_a_safety_state_sha256") or ""
).strip()
if re.fullmatch(r"[0-9a-f]{64}", safety_hash) is None:
    raise SystemExit("account-b evidence safety state hash is invalid")
target_symbol = str(evidence.get("target_symbol") or "").strip().upper()
if re.fullmatch(r"[A-Z0-9]{5,20}", target_symbol) is None:
    raise SystemExit("account-b evidence target symbol is invalid")
portfolio_baseline_sha256 = str(
    evidence.get("non_target_portfolio_baseline_sha256") or ""
).strip()
if re.fullmatch(r"[0-9a-f]{64}", portfolio_baseline_sha256) is None:
    raise SystemExit(
        "account-b evidence non-target portfolio baseline is invalid"
    )


def hash_regular_file(path: Path) -> tuple[str, bytes]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise SystemExit(f"account-b report is not regular: {path}")
        if before.st_uid != expected_owner_uid:
            raise SystemExit(
                f"account-b report owner differs from evidence: {path}"
            )
        if before.st_mode & 0o022:
            raise SystemExit(
                f"account-b report is group/world writable: {path}"
            )
        digest = hashlib.sha256()
        payload_parts = []
        payload_size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            payload_size += len(chunk)
            if payload_size > 1024 * 1024:
                raise SystemExit(f"account-b report is too large: {path}")
            digest.update(chunk)
            payload_parts.append(chunk)
        after = os.fstat(fd)
        stable_fields = (
            before.st_dev == after.st_dev,
            before.st_ino == after.st_ino,
            before.st_size == after.st_size,
            before.st_mtime_ns == after.st_mtime_ns,
        )
        if not all(stable_fields):
            raise SystemExit(f"account-b report changed during hash: {path}")
        return digest.hexdigest(), b"".join(payload_parts)
    finally:
        os.close(fd)


reports = {}
for report_name in (
    "exchange_snapshot",
    "postgres_snapshot",
    "fault_report",
    "live_trade_report",
):
    path_key = f"{report_name}_path"
    hash_key = f"{report_name}_sha256"
    raw_path = str(evidence.get(path_key) or "").strip()
    expected_hash = str(evidence.get(hash_key) or "").strip()
    if not raw_path:
        raise SystemExit(f"account-b evidence missing: {path_key}")
    if re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
        raise SystemExit(f"account-b evidence hash invalid: {hash_key}")
    report_path = Path(raw_path)
    if not report_path.is_absolute():
        report_path = evidence_root / report_path
    if report_path.is_symlink():
        raise SystemExit(
            f"account-b report cannot be a symlink: {report_path}"
        )
    report_path = report_path.resolve()
    try:
        report_path.relative_to(evidence_root)
    except ValueError as exc:
        raise SystemExit(
            f"account-b report escapes evidence root: {report_path}"
        ) from exc
    actual_hash, report_payload = hash_regular_file(report_path)
    if actual_hash != expected_hash:
        raise SystemExit(f"account-b report hash mismatch: {report_name}")
    try:
        report = json.loads(report_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"account-b report JSON invalid: {report_name}"
        ) from exc
    if not isinstance(report, dict):
        raise SystemExit(f"account-b report must be an object: {report_name}")
    if len(report) < 2:
        raise SystemExit(f"account-b report is empty: {report_name}")
    reports[report_name] = report


def require_report_fields(
    report_name: str,
    expected: dict,
) -> None:
    report = reports[report_name]
    for key, value in expected.items():
        if report.get(key) != value:
            raise SystemExit(f"{report_name} mismatch: {key}")


def require_fresh_timestamp(
    report_name: str,
    key: str,
    *,
    max_age_seconds: int,
) -> None:
    report = reports[report_name]
    try:
        timestamp = datetime.fromisoformat(
            str(report.get(key) or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise SystemExit(
            f"{report_name} timestamp invalid: {key}"
        ) from exc
    age_seconds = (datetime.now(timezone.utc) - timestamp).total_seconds()
    if age_seconds < 0 or age_seconds > max_age_seconds:
        print(
            "!! gate warning [account-b-report-age]: "
            f"{report_name} timestamp stale or in the future: {key} "
            f"age_seconds={age_seconds:.3f}",
            file=sys.stderr,
        )


require_report_fields(
    "exchange_snapshot",
    {
        "schema_version": "trader-v3-exchange-snapshot/v1",
        "account_id": "account-a",
        "symbol": target_symbol,
        "release_id": manifest["release_id"],
        "image_digest": manifest["image_digest"],
        "config_sha256": manifest["config_sha256"],
        "target_symbol_flat": True,
        "target_symbol_regular_orders_zero": True,
        "target_symbol_algo_orders_zero": True,
    },
)
require_fresh_timestamp(
    "exchange_snapshot",
    "captured_at",
    max_age_seconds=3600,
)

require_report_fields(
    "postgres_snapshot",
    {
        "schema_version": "trader-v3-postgres-snapshot/v1",
        "account_id": "account-a",
        "release_id": manifest["release_id"],
        "image_digest": manifest["image_digest"],
        "config_sha256": manifest["config_sha256"],
        "database_schema_epoch": database_schema_epoch,
        "rollout_phase": "account_a_canary",
        "heartbeat_status": "HALTED",
        "reconciliation_status": "healthy",
        "no_open_p0_p1_incidents": True,
    },
)
postgres_snapshot = reports["postgres_snapshot"]
runtime_generation = str(
    postgres_snapshot.get("runtime_generation") or ""
).strip()
lease_fencing_token = postgres_snapshot.get("lease_fencing_token")
heartbeat_sequence = postgres_snapshot.get("heartbeat_sequence")
if (
    not runtime_generation
    or isinstance(lease_fencing_token, bool)
    or not isinstance(lease_fencing_token, int)
    or lease_fencing_token <= 0
    or isinstance(heartbeat_sequence, bool)
    or not isinstance(heartbeat_sequence, int)
    or heartbeat_sequence <= 0
):
    raise SystemExit("postgres_snapshot heartbeat writer identity is invalid")
require_fresh_timestamp(
    "postgres_snapshot",
    "captured_at",
    max_age_seconds=3600,
)

require_report_fields(
    "fault_report",
    {
        "schema_version": "trader-v3-fault-report/v1",
        "account_id": "account-a",
        "release_id": manifest["release_id"],
        "image_digest": manifest["image_digest"],
        "config_sha256": manifest["config_sha256"],
        "rollout_phase": "account_a_canary",
        "passed": True,
        "canary_halted": True,
    },
)
fault_scenarios = reports["fault_report"].get("scenarios")
if not isinstance(fault_scenarios, list) or not fault_scenarios:
    raise SystemExit("fault_report scenarios must be non-empty")
for scenario in fault_scenarios:
    if not isinstance(scenario, dict):
        raise SystemExit("fault_report scenario must be an object")
    if not str(scenario.get("name") or "").strip():
        raise SystemExit("fault_report scenario name is required")
    if scenario.get("passed") is not True:
        raise SystemExit("fault_report scenario did not pass")
require_fresh_timestamp(
    "fault_report",
    "completed_at",
    max_age_seconds=3600,
)


def required_decimal(
    record: dict,
    key: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    try:
        value = Decimal(str(record.get(key)))
    except (InvalidOperation, ValueError) as exc:
        raise SystemExit(f"live trade report decimal invalid: {key}") from exc
    if not value.is_finite():
        raise SystemExit(f"live trade report decimal invalid: {key}")
    if positive and value <= 0:
        raise SystemExit(f"live trade report requires positive {key}")
    if non_negative and value < 0:
        raise SystemExit(f"live trade report requires non-negative {key}")
    return value


live_trade = reports["live_trade_report"]
live_required = {
    "schema_version": "trader-v3-live-trade-report/v1",
    "account_id": "account-a",
    "symbol": target_symbol,
    "release_id": manifest["release_id"],
    "image_digest": manifest["image_digest"],
    "config_sha256": manifest["config_sha256"],
    "dependency_lock_sha256": manifest["dependency_lock_sha256"],
    "passed": True,
    "failure_reason": "",
    "authorization_signatures_verified": True,
    "rollout_phase": "account_a_canary",
    "round_trip_count": 1,
    "finished_halted": True,
    "non_target_portfolio_before_sha256": portfolio_baseline_sha256,
    "non_target_portfolio_after_sha256": portfolio_baseline_sha256,
}
for key, expected in live_required.items():
    if live_trade.get(key) != expected:
        raise SystemExit(f"live trade report mismatch: {key}")

testnet = live_trade.get("testnet_emergency_close")
if not isinstance(testnet, dict):
    raise SystemExit("live trade report lacks testnet emergency close")
testnet_required = {
    "verified": True,
    "environment": "testnet",
    "release_id": manifest["release_id"],
    "image_digest": manifest["image_digest"],
    "symbol": target_symbol,
    "open_order_type": "LIMIT",
    "open_time_in_force": "IOC",
    "close_order_type": "MARKET",
    "close_reduce_only": True,
    "target_symbol_flat": True,
    "target_symbol_regular_orders_zero": True,
    "target_symbol_algo_orders_zero": True,
}
for key, expected in testnet_required.items():
    if testnet.get(key) != expected:
        raise SystemExit(f"testnet emergency close mismatch: {key}")
testnet_open_fill = required_decimal(
    testnet,
    "open_filled_quantity",
    positive=True,
)
testnet_close_quantity = required_decimal(
    testnet,
    "close_quantity",
    positive=True,
)
testnet_close_fill = required_decimal(
    testnet,
    "close_filled_quantity",
    positive=True,
)
if (
    testnet_close_quantity != testnet_open_fill
    or testnet_close_fill != testnet_open_fill
):
    raise SystemExit(
        "testnet emergency close quantity differs from actual open fill"
    )
testnet_verified_at = datetime.fromisoformat(
    str(testnet.get("verified_at") or "").replace("Z", "+00:00")
)
testnet_age_seconds = (
    datetime.now(timezone.utc) - testnet_verified_at
).total_seconds()
if testnet_age_seconds < 0 or testnet_age_seconds > 86400:
    print(
        "!! gate warning [testnet-emergency-close-age]: "
        "testnet emergency close evidence is stale or in the future "
        f"age_seconds={testnet_age_seconds:.3f}",
        file=sys.stderr,
    )

mainnet = live_trade.get("mainnet_round_trip")
if not isinstance(mainnet, dict):
    raise SystemExit("live trade report lacks mainnet round trip")
mainnet_required = {
    "open_order_type": "LIMIT",
    "open_time_in_force": "IOC",
    "close_order_type": "MARKET",
    "close_reduce_only": True,
    "target_symbol_flat": True,
    "target_symbol_regular_orders_zero": True,
    "target_symbol_algo_orders_zero": True,
    "emergency_close_available": True,
}
for key, expected in mainnet_required.items():
    if mainnet.get(key) != expected:
        raise SystemExit(f"mainnet round trip mismatch: {key}")
open_fill = required_decimal(
    mainnet,
    "open_filled_quantity",
    positive=True,
)
close_quantity = required_decimal(
    mainnet,
    "close_quantity",
    positive=True,
)
close_fill = required_decimal(
    mainnet,
    "close_filled_quantity",
    positive=True,
)
if close_quantity != open_fill or close_fill != open_fill:
    raise SystemExit(
        "mainnet close quantity differs from actual open fill"
    )
actual_notional = required_decimal(
    mainnet,
    "actual_open_notional_usdt",
    positive=True,
)
if actual_notional > Decimal("12"):
    raise SystemExit("mainnet round trip exceeds 12 USDT")
max_loss = required_decimal(
    mainnet,
    "max_cumulative_loss_usdt",
    positive=True,
)
if max_loss >= Decimal("1.5"):
    raise SystemExit("mainnet loss limit must be below 1.5 USDT")
gross_pnl = required_decimal(mainnet, "gross_pnl_usdt")
fees = required_decimal(mainnet, "fees_usdt", non_negative=True)
net_pnl = required_decimal(mainnet, "net_pnl_usdt")
cumulative_loss = required_decimal(
    mainnet,
    "cumulative_net_loss_usdt",
    non_negative=True,
)
expected_net_pnl = gross_pnl - fees
if net_pnl != expected_net_pnl:
    raise SystemExit("mainnet net PnL does not equal gross PnL minus fees")
expected_loss = max(Decimal("0"), -net_pnl)
if cumulative_loss != expected_loss:
    raise SystemExit("mainnet cumulative net loss is inconsistent")
if cumulative_loss >= max_loss or cumulative_loss >= Decimal("1.5"):
    raise SystemExit("mainnet cumulative net loss reached the stop limit")
issued_at = datetime.fromisoformat(
    str(evidence.get("issued_at") or "").replace("Z", "+00:00")
)
age_seconds = (datetime.now(timezone.utc) - issued_at).total_seconds()
if age_seconds < 0 or age_seconds > 3600:
    print(
        "!! gate warning [account-b-evidence-age]: "
        "account-b evidence issued_at is stale or in the future "
        f"age_seconds={age_seconds:.3f}",
        file=sys.stderr,
    )
print(
    f"{safety_hash}\t{target_symbol}\t"
    f"{portfolio_baseline_sha256}"
)
# ACCOUNT_B_EVIDENCE_VALIDATOR_END
PY
  )
  python3 "$RELEASE_TOOL" verify-live \
    --manifest "$RELEASE_MANIFEST" \
    --bundle-manifest "$STAGING/bundle-manifest.json" \
    --container trader-v3-node-a
  ACCOUNT_A_PRE_SNAPSHOT="$(mktemp)"
  TEMP_FILES+=("$ACCOUNT_A_PRE_SNAPSHOT")
  capture_account_a_safety_snapshot \
    "$ACCOUNT_A_PRE_SNAPSHOT" \
    "$ACCOUNT_A_EXPECTED_SAFETY_STATE_SHA256" \
    "$ACCOUNT_A_TARGET_SYMBOL" \
    "$ACCOUNT_A_NON_TARGET_PORTFOLIO_BASELINE_SHA256" \
    "before-account-b-recreate"
elif [ "$ROLLOUT_NODE" = "trader-v3-node-c" ]; then
  require_rollout_phase account_b_rollout
elif [ "$ROLLOUT_NODE" = "trader-v3-node-d" ]; then
  require_rollout_phase account_c_rollout
fi
echo "== release captured commit=$RELEASE_COMMIT release_id=$RELEASE_ID"
verify_immutable_trust_chain
degraded_gate_checkpoint "trust"
if [ "$DELIVERY_MODE" = "immutable_image" ]; then
  docker image inspect "$TARGET_IMAGE" >/dev/null \
    || die "target immutable image is unavailable"
fi
preflight_gate_checkpoint "image"

# ---------- backup ----------
mkdir -p "$BACKUP_ROOT/files"
: > "$BACKUP_ROOT/index.tsv"
: > "$BACKUP_ROOT/new-files.txt"
prepare_legacy_rollback_recreate_fleet
verify_legacy_rollback_recreate_fleet
docker inspect "${ALL_NODES[@]}" > "$BACKUP_ROOT/containers-before.json"
for node in "${RECREATE_NODES[@]}"; do
  printf '%s\t%s\n' "$node" "$(docker inspect --format '{{.Image}}' "$node")"
done > "$BACKUP_ROOT/old-images.tsv"
bk() { # bk <src> <label>
  cp -a "$1" "$BACKUP_ROOT/files/$2"
  printf '%s\t%s\n' "$2" "$1" >> "$BACKUP_ROOT/index.tsv"
}
bk "$T/.env.v3" "config__.env.v3"
for index in "${!CONTROL_PLANE_ROLE_ENV_FILES[@]}"; do
  env_file="${CONTROL_PLANE_ROLE_ENV_FILES[$index]}"
  if [ -f "$env_file" ]; then
    bk "$env_file" "config__control-plane-role-$index.env"
  else
    printf '%s\n' "$env_file" >> "$BACKUP_ROOT/new-files.txt"
  fi
done
for f in "${CHANGED_CONTAINER[@]:-}"; do
  [ -n "$f" ] || continue
  if [ -f "$T/container-patches/$f" ]; then
    bk "$T/container-patches/$f" "cp__$f"
  else
    printf '%s\n' "$T/container-patches/$f" >> "$BACKUP_ROOT/new-files.txt"
  fi
done
for h in "${CHANGED_HOST[@]:-}"; do
  case "$h" in
	    read_api) bk "$API_TGT" "host__read_api.py" ;;
	    snapshot) bk "$SNAPSHOT_TGT" "host__snapshot.py" ;;
	    decision_gateway) bk "$DECISION_GATEWAY_TGT" "host__decision_gateway.py" ;;
      exchange_state_recorder)
        bk "$EXCHANGE_STATE_RECORDER_TGT" \
          "host__exchange_state_recorder.py"
        ;;
	    hermes_signal_feeder)
	      bk "$HERMES_FEEDER_TGT" "host__hermes_signal_feeder.py"
	      ;;
	    hermes_v3_trade) bk "$HERMES_V3_TRADE_TGT" "host__v3_trade.py" ;;
	    hermes_v3_skill)
	      bk "$HERMES_V3_SKILL_TGT" "host__v3_trader_SKILL.md"
	      ;;
	    system_snapshot_schema)
      bk "$SYSTEM_SNAPSHOT_SCHEMA_TGT" "host__system_snapshot.v1.json"
      ;;
    execution_domain_init)
      if [ -f "$EXECUTION_DOMAIN_INIT_TGT" ]; then
        bk "$EXECUTION_DOMAIN_INIT_TGT" "host__execution_domain_init.py"
      else
        printf '%s\n' "$EXECUTION_DOMAIN_INIT_TGT" \
          >> "$BACKUP_ROOT/new-files.txt"
      fi
      ;;
    execution_domain_contracts)
      if [ -f "$EXECUTION_DOMAIN_CONTRACTS_TGT" ]; then
        bk "$EXECUTION_DOMAIN_CONTRACTS_TGT" \
          "host__execution_domain_contracts.py"
      else
        printf '%s\n' "$EXECUTION_DOMAIN_CONTRACTS_TGT" \
          >> "$BACKUP_ROOT/new-files.txt"
      fi
      ;;
    execution_domain_control_plane)
      if [ -f "$EXECUTION_DOMAIN_CONTROL_PLANE_TGT" ]; then
        bk "$EXECUTION_DOMAIN_CONTROL_PLANE_TGT" \
          "host__execution_domain_control_plane.py"
      else
        printf '%s\n' "$EXECUTION_DOMAIN_CONTROL_PLANE_TGT" \
          >> "$BACKUP_ROOT/new-files.txt"
      fi
      ;;
    execution_domain_portfolio_baseline)
      if [ -f "$EXECUTION_DOMAIN_PORTFOLIO_BASELINE_TGT" ]; then
        bk "$EXECUTION_DOMAIN_PORTFOLIO_BASELINE_TGT" \
          "host__execution_domain_portfolio_baseline.py"
      else
        printf '%s\n' "$EXECUTION_DOMAIN_PORTFOLIO_BASELINE_TGT" \
          >> "$BACKUP_ROOT/new-files.txt"
      fi
      ;;
    execution_domain_account_execution_ledger)
      if [ -f "$EXECUTION_DOMAIN_ACCOUNT_EXECUTION_LEDGER_TGT" ]; then
        bk "$EXECUTION_DOMAIN_ACCOUNT_EXECUTION_LEDGER_TGT" \
          "host__execution_domain_account_execution_ledger.py"
      else
        printf '%s\n' "$EXECUTION_DOMAIN_ACCOUNT_EXECUTION_LEDGER_TGT" \
          >> "$BACKUP_ROOT/new-files.txt"
      fi
      ;;
    execution_domain_entry_batch)
      if [ -f "$EXECUTION_DOMAIN_ENTRY_BATCH_TGT" ]; then
        bk "$EXECUTION_DOMAIN_ENTRY_BATCH_TGT" \
          "host__execution_domain_entry_batch.py"
      else
        printf '%s\n' "$EXECUTION_DOMAIN_ENTRY_BATCH_TGT" \
          >> "$BACKUP_ROOT/new-files.txt"
      fi
      ;;
    execution_domain_order_ownership)
      if [ -f "$EXECUTION_DOMAIN_ORDER_OWNERSHIP_TGT" ]; then
        bk "$EXECUTION_DOMAIN_ORDER_OWNERSHIP_TGT" \
          "host__execution_domain_order_ownership.py"
      else
        printf '%s\n' "$EXECUTION_DOMAIN_ORDER_OWNERSHIP_TGT" \
          >> "$BACKUP_ROOT/new-files.txt"
      fi
      ;;
    execution_domain_idempotency)
      if [ -f "$EXECUTION_DOMAIN_IDEMPOTENCY_TGT" ]; then
        bk "$EXECUTION_DOMAIN_IDEMPOTENCY_TGT" \
          "host__execution_domain_idempotency.py"
      else
        printf '%s\n' "$EXECUTION_DOMAIN_IDEMPOTENCY_TGT" \
          >> "$BACKUP_ROOT/new-files.txt"
      fi
      ;;
    execution_domain_identifiers)
      if [ -f "$EXECUTION_DOMAIN_IDENTIFIERS_TGT" ]; then
        bk "$EXECUTION_DOMAIN_IDENTIFIERS_TGT" \
          "host__execution_domain_identifiers.py"
      else
        printf '%s\n' "$EXECUTION_DOMAIN_IDENTIFIERS_TGT" \
          >> "$BACKUP_ROOT/new-files.txt"
      fi
      ;;
    settings_package)
      for settings_file in "${SETTINGS_PACKAGE_FILES[@]}"; do
        if [ -f "$SETTINGS_PACKAGE_TGT/$settings_file" ]; then
          bk "$SETTINGS_PACKAGE_TGT/$settings_file" \
            "host__settings_$settings_file"
        else
          printf '%s\n' "$SETTINGS_PACKAGE_TGT/$settings_file" \
            >> "$BACKUP_ROOT/new-files.txt"
        fi
      done
      ;;
    audit_package)
      for audit_file in "${AUDIT_PACKAGE_FILES[@]}"; do
        if [ -f "$AUDIT_PACKAGE_TGT/$audit_file" ]; then
          bk "$AUDIT_PACKAGE_TGT/$audit_file" \
            "host__audit_$audit_file"
        else
          printf '%s\n' "$AUDIT_PACKAGE_TGT/$audit_file" \
            >> "$BACKUP_ROOT/new-files.txt"
        fi
      done
      ;;
    order_management_package)
      for om_file in "${ORDER_MANAGEMENT_PACKAGE_FILES[@]}"; do
        if [ -f "$ORDER_MANAGEMENT_PACKAGE_TGT/$om_file" ]; then
          bk "$ORDER_MANAGEMENT_PACKAGE_TGT/$om_file" \
            "host__order_management_$om_file"
        else
          printf '%s\n' "$ORDER_MANAGEMENT_PACKAGE_TGT/$om_file" \
            >> "$BACKUP_ROOT/new-files.txt"
        fi
      done
      ;;
    observability_package)
      for obs_file in "${OBSERVABILITY_PACKAGE_FILES[@]}"; do
        if [ -f "$OBSERVABILITY_PACKAGE_TGT/$obs_file" ]; then
          bk "$OBSERVABILITY_PACKAGE_TGT/$obs_file" \
            "host__observability_$obs_file"
        else
          printf '%s\n' "$OBSERVABILITY_PACKAGE_TGT/$obs_file" \
            >> "$BACKUP_ROOT/new-files.txt"
        fi
      done
      ;;
    app_roles)
      if [ -f "$APP_ROLES_TGT" ]; then
        bk "$APP_ROLES_TGT" "host__app_roles.py"
      else
        printf '%s\n' "$APP_ROLES_TGT" \
          >> "$BACKUP_ROOT/new-files.txt"
      fi
      ;;
    pools)
      if [ -f "$DB_POOLS_TGT" ]; then
        bk "$DB_POOLS_TGT" "host__pools.py"
      else
        printf '%s\n' "$DB_POOLS_TGT" \
          >> "$BACKUP_ROOT/new-files.txt"
      fi
      ;;
    repository)
      if [ -f "$DB_REPOSITORY_TGT" ]; then
        bk "$DB_REPOSITORY_TGT" "host__repository.py"
      else
        printf '%s\n' "$DB_REPOSITORY_TGT" \
          >> "$BACKUP_ROOT/new-files.txt"
      fi
      ;;
    rebuild_orders_projection)
      if [ -f "$REBUILD_ORDERS_PROJECTION_TGT" ]; then
        bk "$REBUILD_ORDERS_PROJECTION_TGT" \
          "host__rebuild_orders_projection.py"
      else
        printf '%s\n' "$REBUILD_ORDERS_PROJECTION_TGT" \
          >> "$BACKUP_ROOT/new-files.txt"
      fi
      ;;
  esac
done
if [ "$CHANGED_WATCHER_COUNT" -gt 0 ]; then
  capture_watcher_runtime_backup "$WATCHER_RUNTIME_CHANGED_LIST"
else
  : >"$WATCHER_RUNTIME_CHANGED_LIST"
fi
DATABASE_URL="$(read_database_url)"
capture_pre_migration_database_backup \
  "$DATABASE_URL" \
  "$POSTGRES_PRE_MIGRATION_DUMP" \
  "$POSTGRES_PRE_MIGRATION_RESTORE_LIST" \
  "$POSTGRES_PRE_MIGRATION_SHA256"
unset DATABASE_URL
mkdir -p "$POST_MIGRATION_RECOVERY_PAYLOAD_ROOT"
chmod 0700 "$POST_MIGRATION_RECOVERY_PAYLOAD_ROOT"
cp -a "$STAGING/." "$POST_MIGRATION_RECOVERY_PAYLOAD_ROOT/"
[ -f "$POST_MIGRATION_RECOVERY_MANIFEST" ] \
  || die "post-migration recovery payload lacks release-manifest.json"
chmod 0400 "$POST_MIGRATION_RECOVERY_MANIFEST"
capture_pre_migration_backup_expectation \
  "$PRE_MIGRATION_BACKUP_EXPECTATION" \
  "$POSTGRES_PRE_MIGRATION_DUMP" \
  "$POSTGRES_PRE_MIGRATION_SHA256" \
  "$POSTGRES_PRE_MIGRATION_RESTORE_LIST" \
  "$BACKUP_ROOT/old-images.tsv"
cp bundle-manifest.json "$BACKUP_ROOT/deployed-bundle-manifest.json"
cp "$RELEASE_MANIFEST" "$BACKUP_ROOT/deployed-release-manifest.json"
if [ "$ROLLOUT_NODE" = "trader-v3-node-b" ] \
  && [ "$EMERGENCY_ROLLBACK" = "0" ]; then
  mkdir -p "$BACKUP_ROOT/account-b-gate"
  cp "$ACCOUNT_B_EVIDENCE_COPY" \
    "$BACKUP_ROOT/account-b-gate/account-a-evidence.json"
  cp "$ACCOUNT_B_SIGNATURE_COPY" \
    "$BACKUP_ROOT/account-b-gate/account-a-evidence.sig"
  cp "$ACCOUNT_B_REVIEWER_PUBLIC_KEY" \
    "$BACKUP_ROOT/account-b-gate/reviewer-public-key.pem"
  cp "$ACCOUNT_A_PRE_SNAPSHOT" \
    "$BACKUP_ROOT/account-b-gate/account-a-safety-pre.json"
  chmod 0400 "$BACKUP_ROOT"/account-b-gate/*
fi
if [ -f "$T/RELEASE_MANIFEST.json" ]; then
  bk "$T/RELEASE_MANIFEST.json" "meta__RELEASE_MANIFEST.json"
else
  printf '%s\n' "$T/RELEASE_MANIFEST.json" >> "$BACKUP_ROOT/new-files.txt"
fi
if [ -f "$T/DEPLOYED_COMMIT.txt" ]; then
  bk "$T/DEPLOYED_COMMIT.txt" "meta__DEPLOYED_COMMIT.txt"
else
  printf '%s\n' "$T/DEPLOYED_COMMIT.txt" >> "$BACKUP_ROOT/new-files.txt"
fi
(
  cd "$BACKUP_ROOT"
  find . -type f ! -name SHA256SUMS -print0 \
    | sort -z \
    | xargs -0 sha256sum
) > "$BACKUP_ROOT/SHA256SUMS"
BACKUP_CAPTURED=1
prepare_post_migration_recovery_recreate
for node in "${RECREATE_NODES[@]}"; do
  recovery_dir="$POST_MIGRATION_RECOVERY_ROOT/$node"
  capture_post_migration_recovery_expectation \
    "$recovery_dir/expectation.json" \
    "$POST_MIGRATION_RECOVERY_MANIFEST" \
    "$PRE_MIGRATION_BACKUP_EXPECTATION" \
    "$recovery_dir/recreate.sh" \
    "$recovery_dir/container.env" \
    "$POST_MIGRATION_RECOVERY_DOCKER" \
    "$node"
done
(
  cd "$BACKUP_ROOT"
  find . -type f \
    ! -name SHA256SUMS \
    ! -name SHA256SUMS.new \
    -print0 \
    | sort -z \
    | xargs -0 sha256sum
) > "$BACKUP_ROOT/SHA256SUMS.new"
mv "$BACKUP_ROOT/SHA256SUMS.new" "$BACKUP_ROOT/SHA256SUMS"
echo "== backup at $BACKUP_ROOT"

if [ "$DEPLOY_PHASE" = "preflight" ]; then
  echo "== PREFLIGHT OK release_id=$RELEASE_ID image=$TARGET_IMAGE"
  echo "== A-D runtime and heartbeats remained online"
  exit 0
fi

# ---------- HALT ----------
quiesce_rollout_account
verify_all_execution_accounts_quiesced
preflight_gate_checkpoint "ledger"
preflight_gate_checkpoint "fence"

if [ "$PHASE_ONLY_ROLLOUT" = "1" ]; then
  verify_release_nodes "${ALL_NODES[@]}"
  acquire_maintenance_fence_after_bootstrap
  degraded_gate_checkpoint "rollout"
  echo "== phase advancement requires the separate user-confirmed command"
  echo "== PHASE ONLY OK account=$ROLLOUT_ACCOUNT release_id=$RELEASE_ID"
  echo "== A-D remain HALTED; run the audited canary executor separately"
  exit 0
fi

# ---------- database schema ----------
verify_all_execution_accounts_quiesced
POST_MIGRATION_RECOVERY_REQUIRED=1
apply_and_verify_database_migration
echo "== database schema verified epoch=$DATABASE_SCHEMA_EPOCH"
degraded_gate_checkpoint "rollout"
if [ "$EMERGENCY_ROLLBACK" = "1" ]; then
  echo "== emergency rollback skips reviewed rollout registration and advancement"
elif [ "$ROLLOUT_NODE" = "trader-v3-node-a" ]; then
  if [[ "$DEPLOY_GATE_MODE" =~ ^(bootstrap(_resume)?|migration_rebaseline)_stopped$ ]]; then
    ensure_bootstrap_rollout_registration
  else
    run_reviewed_rollout register \
      --manifest "$RELEASE_MANIFEST" \
      --bundle-manifest "$STAGING/bundle-manifest.json" \
      --capacity-evidence "$REDIS_CAPACITY_EVIDENCE" \
      --reviewed-by "$ROLLOUT_REVIEWED_BY" \
      --idempotency-key "register:$RELEASE_ID"
    ROLLOUT_TRACKED=1
    echo "== reviewed rollout registered in account_a_canary"
  fi
fi

DOWNTIME_WINDOW_ENTERED=1
load_maintenance_fence_state

stop_recreate_nodes

# ---------- install ----------
verify_all_execution_accounts_quiesced
verify_maintenance_fence "host-install"
FILES_INSTALLED=1
install_operator_account_registry_environment
bootstrap_control_plane_roles
if [ "$DELIVERY_MODE" = "transition_bind_mount" ]; then
  for f in "${CHANGED_CONTAINER[@]:-}"; do
    [ -n "$f" ] || continue
    live="$T/container-patches/$f"
    if [ -f "$live" ]; then
      cat "$f" > "$live"
    else
      install -m 0644 "$f" "$live"
    fi
    cmp -s "$f" "$live" || die "post-install mismatch: $f"
  done
fi
for h in "${CHANGED_HOST[@]:-}"; do
  case "$h" in
	    read_api) cat host/read_api.py > "$API_TGT" ;;
	    snapshot) cat host/snapshot.py > "$SNAPSHOT_TGT" ;;
	    decision_gateway) cat host/decision_gateway/gateway.py > "$DECISION_GATEWAY_TGT" ;;
      exchange_state_recorder)
        install_payload_atomically \
          host/exchange_state_recorder.py \
          "$EXCHANGE_STATE_RECORDER_TGT"
        EXCHANGE_STATE_RESTART_REQUIRED=1
        ;;
	    hermes_signal_feeder)
	      install_payload_atomically host/hermes_signal_feeder.py \
	        "$HERMES_FEEDER_TGT"
	      HERMES_RESTART_REQUIRED=1
	      ;;
	    hermes_v3_trade)
	      install_payload_atomically host/v3_trade.py "$HERMES_V3_TRADE_TGT"
	      HERMES_RESTART_REQUIRED=1
	      ;;
	    hermes_v3_skill)
	      install_payload_atomically host/v3-trader/SKILL.md \
	        "$HERMES_V3_SKILL_TGT"
	      HERMES_RESTART_REQUIRED=1
	      ;;
	    system_snapshot_schema)
      cat host/system_snapshot.v1.json > "$SYSTEM_SNAPSHOT_SCHEMA_TGT"
      ;;
    execution_domain_init)
      mkdir -p "$EXECUTION_DOMAIN_TGT"
      if [ -f "$EXECUTION_DOMAIN_INIT_TGT" ]; then
        cat host/execution_domain/__init__.py \
          > "$EXECUTION_DOMAIN_INIT_TGT"
      else
        install -m 0644 host/execution_domain/__init__.py \
          "$EXECUTION_DOMAIN_INIT_TGT"
      fi
      ;;
    execution_domain_contracts)
      mkdir -p "$EXECUTION_DOMAIN_TGT"
      if [ -f "$EXECUTION_DOMAIN_CONTRACTS_TGT" ]; then
        cat host/execution_domain/contracts.py \
          > "$EXECUTION_DOMAIN_CONTRACTS_TGT"
      else
        install -m 0644 host/execution_domain/contracts.py \
          "$EXECUTION_DOMAIN_CONTRACTS_TGT"
      fi
      ;;
    execution_domain_control_plane)
      mkdir -p "$EXECUTION_DOMAIN_TGT"
      if [ -f "$EXECUTION_DOMAIN_CONTROL_PLANE_TGT" ]; then
        cat host/execution_domain/control_plane.py \
          > "$EXECUTION_DOMAIN_CONTROL_PLANE_TGT"
      else
        install -m 0644 host/execution_domain/control_plane.py \
          "$EXECUTION_DOMAIN_CONTROL_PLANE_TGT"
      fi
      ;;
    execution_domain_portfolio_baseline)
      mkdir -p "$EXECUTION_DOMAIN_TGT"
      if [ -f "$EXECUTION_DOMAIN_PORTFOLIO_BASELINE_TGT" ]; then
        cat host/execution_domain/portfolio_baseline.py \
          > "$EXECUTION_DOMAIN_PORTFOLIO_BASELINE_TGT"
      else
        install -m 0644 host/execution_domain/portfolio_baseline.py \
          "$EXECUTION_DOMAIN_PORTFOLIO_BASELINE_TGT"
      fi
      ;;
    execution_domain_account_execution_ledger)
      mkdir -p "$EXECUTION_DOMAIN_TGT"
      install_payload_atomically \
        host/execution_domain/account_execution_ledger.py \
        "$EXECUTION_DOMAIN_ACCOUNT_EXECUTION_LEDGER_TGT"
      ;;
    execution_domain_entry_batch)
      mkdir -p "$EXECUTION_DOMAIN_TGT"
      install_payload_atomically \
        host/execution_domain/entry_batch.py \
        "$EXECUTION_DOMAIN_ENTRY_BATCH_TGT"
      ;;
    execution_domain_order_ownership)
      mkdir -p "$EXECUTION_DOMAIN_TGT"
      if [ -f "$EXECUTION_DOMAIN_ORDER_OWNERSHIP_TGT" ]; then
        cat host/execution_domain/order_ownership.py \
          > "$EXECUTION_DOMAIN_ORDER_OWNERSHIP_TGT"
      else
        install -m 0644 host/execution_domain/order_ownership.py \
          "$EXECUTION_DOMAIN_ORDER_OWNERSHIP_TGT"
      fi
      ;;
    execution_domain_idempotency)
      mkdir -p "$EXECUTION_DOMAIN_TGT"
      if [ -f "$EXECUTION_DOMAIN_IDEMPOTENCY_TGT" ]; then
        cat host/execution_domain/idempotency.py \
          > "$EXECUTION_DOMAIN_IDEMPOTENCY_TGT"
      else
        install -m 0644 host/execution_domain/idempotency.py \
          "$EXECUTION_DOMAIN_IDEMPOTENCY_TGT"
      fi
      ;;
    execution_domain_identifiers)
      mkdir -p "$EXECUTION_DOMAIN_TGT"
      if [ -f "$EXECUTION_DOMAIN_IDENTIFIERS_TGT" ]; then
        cat host/execution_domain/identifiers.py \
          > "$EXECUTION_DOMAIN_IDENTIFIERS_TGT"
      else
        install -m 0644 host/execution_domain/identifiers.py \
          "$EXECUTION_DOMAIN_IDENTIFIERS_TGT"
      fi
      ;;
    settings_package)
      mkdir -p "$SETTINGS_PACKAGE_TGT"
      # The deploy runs under umask 077 and the role services run as
      # non-root users; pin the package directory world-readable.
      chmod 0755 "$SETTINGS_PACKAGE_TGT"
      for settings_file in "${SETTINGS_PACKAGE_FILES[@]}"; do
        if [ -f "$SETTINGS_PACKAGE_TGT/$settings_file" ]; then
          cat "host/settings/$settings_file" \
            > "$SETTINGS_PACKAGE_TGT/$settings_file"
        else
          install -m 0644 "host/settings/$settings_file" \
            "$SETTINGS_PACKAGE_TGT/$settings_file"
        fi
      done
      ;;
    audit_package)
      mkdir -p "$AUDIT_PACKAGE_TGT"
      chmod 0755 "$AUDIT_PACKAGE_TGT"
      for audit_file in "${AUDIT_PACKAGE_FILES[@]}"; do
        if [ -f "$AUDIT_PACKAGE_TGT/$audit_file" ]; then
          cat "host/audit/$audit_file" \
            > "$AUDIT_PACKAGE_TGT/$audit_file"
        else
          install -m 0644 "host/audit/$audit_file" \
            "$AUDIT_PACKAGE_TGT/$audit_file"
        fi
      done
      ;;
    order_management_package)
      mkdir -p "$ORDER_MANAGEMENT_PACKAGE_TGT"
      chmod 0755 "$ORDER_MANAGEMENT_PACKAGE_TGT"
      for om_file in "${ORDER_MANAGEMENT_PACKAGE_FILES[@]}"; do
        if [ -f "$ORDER_MANAGEMENT_PACKAGE_TGT/$om_file" ]; then
          cat "host/order_management/$om_file" \
            > "$ORDER_MANAGEMENT_PACKAGE_TGT/$om_file"
        else
          install -m 0644 "host/order_management/$om_file" \
            "$ORDER_MANAGEMENT_PACKAGE_TGT/$om_file"
        fi
      done
      ;;
    observability_package)
      mkdir -p "$OBSERVABILITY_PACKAGE_TGT"
      chmod 0755 "$OBSERVABILITY_PACKAGE_TGT"
      for obs_file in "${OBSERVABILITY_PACKAGE_FILES[@]}"; do
        if [ -f "$OBSERVABILITY_PACKAGE_TGT/$obs_file" ]; then
          cat "host/observability/$obs_file" \
            > "$OBSERVABILITY_PACKAGE_TGT/$obs_file"
        else
          install -m 0644 "host/observability/$obs_file" \
            "$OBSERVABILITY_PACKAGE_TGT/$obs_file"
        fi
      done
      ;;
	    app_roles)
	      install_host_python_module host/app_roles.py "$APP_ROLES_TGT"
	      ;;
	    pools)
	      install_host_python_module host/pools.py "$DB_POOLS_TGT"
	      ;;
    repository)
      install_host_python_module host/repository.py "$DB_REPOSITORY_TGT"
      ;;
    rebuild_orders_projection)
      install_host_python_module \
        scripts/rebuild_orders_projection.py \
        "$REBUILD_ORDERS_PROJECTION_TGT"
      ;;
	  esac
	done
  if [ "$CHANGED_WATCHER_COUNT" -gt 0 ]; then
    install_watcher_runtime_atomically "$WATCHER_RUNTIME_CHANGED_LIST"
    WATCHER_RESTART_REQUIRED=1
  fi
  if [ "$WATCHER_SCHEMA_RESTART_REQUIRED" = "1" ]; then
    WATCHER_RESTART_REQUIRED=1
  fi
	cmp -s host/exchange_state_recorder.py "$EXCHANGE_STATE_RECORDER_TGT" \
	  || die "post-install mismatch: host/exchange_state_recorder.py"
	cmp -s host/hermes_signal_feeder.py "$HERMES_FEEDER_TGT" \
	  || die "post-install mismatch: host/hermes_signal_feeder.py"
	cmp -s host/v3_trade.py "$HERMES_V3_TRADE_TGT" \
	  || die "post-install mismatch: host/v3_trade.py"
	cmp -s host/v3-trader/SKILL.md "$HERMES_V3_SKILL_TGT" \
	  || die "post-install mismatch: host/v3-trader/SKILL.md"
	cmp -s host/execution_domain/order_ownership.py \
	  "$EXECUTION_DOMAIN_ORDER_OWNERSHIP_TGT" \
	  || die "post-install mismatch: host/execution_domain/order_ownership.py"
	cmp -s host/execution_domain/account_execution_ledger.py \
	  "$EXECUTION_DOMAIN_ACCOUNT_EXECUTION_LEDGER_TGT" \
	  || die "post-install mismatch: host/execution_domain/account_execution_ledger.py"
	cmp -s host/execution_domain/entry_batch.py \
	  "$EXECUTION_DOMAIN_ENTRY_BATCH_TGT" \
	  || die "post-install mismatch: host/execution_domain/entry_batch.py"
	cmp -s host/app_roles.py "$APP_ROLES_TGT" \
	  || die "post-install mismatch: host/app_roles.py"
	cmp -s host/pools.py "$DB_POOLS_TGT" \
	  || die "post-install mismatch: host/pools.py"
	cmp -s host/repository.py "$DB_REPOSITORY_TGT" \
	  || die "post-install mismatch: host/repository.py"
	cmp -s scripts/rebuild_orders_projection.py \
	  "$REBUILD_ORDERS_PROJECTION_TGT" \
	  || die "post-install mismatch: scripts/rebuild_orders_projection.py"
	cat "$RELEASE_MANIFEST" > "$T/RELEASE_MANIFEST.json"
	printf '%s release_id=%s deployed_at=%s\n' \
	  "$RELEASE_COMMIT" "$RELEASE_ID" "$STAMP" > "$T/DEPLOYED_COMMIT.txt"
	HOST_RUNTIME_INSTALL_COMPLETE=1
	echo "== files installed"
	restart_watcher_runtime
  verify_post_restart_four_channel_account_mapping
  refresh_backup_checksums
	restart_exchange_state_recorder
	restart_hermes_units

# ---------- recreate & verify ----------
verify_all_execution_accounts_quiesced
verify_maintenance_fence "topology-replacement"
reconcile_control_plane_lock_privileges
activate_control_plane_topology
restart_control_plane_units
verify_control_plane_router_contract
echo "== control-plane topology=$CONTROL_PLANE_TOPOLOGY units=${CONTROL_PLANE_UNITS[*]}"

verify_maintenance_fence "node-recreate-plan"
for node in "${RECREATE_NODES[@]}"; do
  generate_release_recreate "$RELEASE_MANIFEST" "$node"
done
ROLLOUT_RECREATE_STARTED=1
for node in "${RECREATE_NODES[@]}"; do
  recreate_release_node "$node"
done
verify_release_nodes "${RECREATE_NODES[@]}"
finalize_node_startup_resource_evidence
if [[ "$DEPLOY_GATE_MODE" =~ ^(bootstrap(_resume)?|migration_rebaseline)_stopped$ ]]; then
  acquire_maintenance_fence_after_bootstrap
fi
if [ "$ROLLOUT_NODE" = "trader-v3-node-b" ]; then
  ACCOUNT_A_POST_SNAPSHOT="$(mktemp)"
  TEMP_FILES+=("$ACCOUNT_A_POST_SNAPSHOT")
  capture_account_a_safety_snapshot \
    "$ACCOUNT_A_POST_SNAPSHOT" \
    "$ACCOUNT_A_EXPECTED_SAFETY_STATE_SHA256" \
    "$ACCOUNT_A_TARGET_SYMBOL" \
    "$ACCOUNT_A_NON_TARGET_PORTFOLIO_BASELINE_SHA256" \
    "after-account-b-recreate"
  cp "$ACCOUNT_A_POST_SNAPSHOT" \
    "$BACKUP_ROOT/account-b-gate/account-a-safety-post.json"
  chmod 0400 \
    "$BACKUP_ROOT/account-b-gate/account-a-safety-post.json"
  (
    cd "$BACKUP_ROOT"
    find . -type f \
      ! -name SHA256SUMS \
      ! -name SHA256SUMS.new \
      -print0 \
      | sort -z \
      | xargs -0 sha256sum
  ) > "$BACKUP_ROOT/SHA256SUMS.new"
  mv "$BACKUP_ROOT/SHA256SUMS.new" "$BACKUP_ROOT/SHA256SUMS"
fi
if [ "$ROLLOUT_NODE" = "trader-v3-node-a" ] \
  && [ "$EMERGENCY_ROLLBACK" = "0" ]; then
  echo "== A-D deployed with one immutable digest in account_a_canary"
elif [ "$ROLLOUT_NODE" = "trader-v3-node-d" ] \
  && [ "$EMERGENCY_ROLLBACK" = "0" ]; then
  echo "== account-d deployed in account_d_rollout; signed account-d closure is required for fleet_complete"
fi

# journal error scan (30s window)
sleep 5
scan_control_plane_journals

echo "== $ROLLOUT_NODE remains HALTED; RESUME requires a separate audited command"
if [ "$EMERGENCY_ROLLBACK" = "1" ]; then
  echo "== emergency rollback reason: $EMERGENCY_ROLLBACK_REASON"
fi
echo "== DEPLOY OK  node=$ROLLOUT_NODE tag=$TAG backup=$BACKUP_ROOT"
echo "== rollback: use old-images.tsv plus containers-before.json, recreate ${RECREATE_NODES[*]}, keep HALTED, then verify-live"
