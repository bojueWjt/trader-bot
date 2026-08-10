#!/usr/bin/env bash
# Account-stall availability hotfix for trader-v3-node-a.
set -Eeuo pipefail

T="${T:-/srv/trader-v3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGING="${STAGING:-$SCRIPT_DIR}"
NODE_CONTAINER="trader-v3-node-a"
NODE_PORT="${NODE_PORT:-8081}"
CONTROL_PLANE_UNIT="trader-v3-controlplane.service"
CONTROL_PLANE_UNIT_SOURCE_RELATIVE="infra/systemd/trader-v3-controlplane.service"
CONTROL_PLANE_UNIT_SOURCE="$T/$CONTROL_PLANE_UNIT_SOURCE_RELATIVE"
CONTROL_PLANE_UNIT_TARGET="/etc/systemd/system/trader-v3-controlplane.service"
EXCHANGE_STATE_UNIT="trader-v3-exchange-state.service"
OPERATION_LOCK="/var/lock/trader-v3-account-stall-operation.lock"
MEMORY_LIMIT="${ACCOUNT_A_MEMORY_LIMIT:-768m}"
MEMORY_SWAP_LIMIT="${ACCOUNT_A_MEMORY_SWAP_LIMIT:-768m}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-trader-v3-postgres}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-trader}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="$T/backups/account-stall-availability-$STAMP"
ROLLBACK_PATH="$BACKUP_ROOT/rollback.sh"
MANIFEST="$STAGING/bundle-manifest.json"
CHECKSUMS="$STAGING/SHA256SUMS"
PATCH_DIR="$T/container-patches"
RECORDER_RELATIVE="services/control-plane/tools/exchange_state_recorder.py"
RECORDER_TARGET="$T/$RECORDER_RELATIVE"
RECORDER_VERIFIER="$STAGING/tools/verify-exchange-state-recorder.py"
RECORDER_WATERMARK=""
GENERATOR_TARGET="$T/hk-gen-recreate-patched.py"
RECREATE_TARGET="$T/recreate-$NODE_CONTAINER.sh"
DEPLOYED_COMMIT_TARGET="$T/DEPLOYED_COMMIT.txt"
CONTAINER_TSV=""
HOST_TSV=""
MIGRATION_TSV=""
TEMP_DIR=""


die() {
  echo "FATAL: $*" >&2
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


cleanup() {
  if [ -n "$TEMP_DIR" ]; then
    rm -rf "$TEMP_DIR"
  fi
}


on_err() {
  local status=$?
  trap - ERR
  echo "FATAL: account-a availability deployment failed" >&2
  if [ -x "$ROLLBACK_PATH" ]; then
    echo "ROLLBACK: bash $ROLLBACK_PATH" >&2
  else
    echo "ROLLBACK: no mutation completed; backup path planned at $BACKUP_ROOT" >&2
  fi
  exit "$status"
}


trap cleanup EXIT
trap on_err ERR

exec 9>"$OPERATION_LOCK"
flock -n 9 || die "another operation holds $OPERATION_LOCK"

for command in \
  curl docker flock install python3 sha256sum systemctl; do
  command -v "$command" >/dev/null || die "required command missing: $command"
done
[ "$(id -u)" = "0" ] || die "deployment must run as root"
[ -d "$T" ] || die "trader root is missing: $T"
[ -f "$MANIFEST" ] || die "bundle manifest is missing: $MANIFEST"
[ -f "$CHECKSUMS" ] || die "bundle checksums are missing: $CHECKSUMS"
[ -f "$STAGING/tools/hk-gen-recreate-patched.py" ] \
  || die "recreate generator is missing"
[ -f "$RECORDER_VERIFIER" ] \
  || die "exchange-state recorder verifier is missing"
[ -x "$RECREATE_TARGET" ] || die "existing account-a recreate script is missing"
[ -x "$T/.venv-cp/bin/python" ] \
  || die "control-plane Python environment is missing"
[ -f "$T/.env.v3" ] || die "control-plane environment file is missing"

(
  cd "$STAGING"
  sha256sum -c "$(basename "$CHECKSUMS")" >/dev/null
) || die "staging SHA256 verification failed"

EXCHANGE_STATE_PID="$(
  systemctl show \
    --property=MainPID \
    --value \
    "$EXCHANGE_STATE_UNIT"
)" || die "$EXCHANGE_STATE_UNIT MainPID is unavailable"
python3 "$RECORDER_VERIFIER" process \
  --pid "$EXCHANGE_STATE_PID" \
  --working-directory "$T" \
  --python "$T/.venv-cp/bin/python" \
  --recorder "$RECORDER_TARGET" \
  || die "$EXCHANGE_STATE_UNIT process identity is invalid"

TEMP_DIR="$(mktemp -d)"
CONTAINER_TSV="$TEMP_DIR/container.tsv"
HOST_TSV="$TEMP_DIR/host.tsv"
MIGRATION_TSV="$TEMP_DIR/migration.tsv"

RELEASE_COMMIT="$(
  python3 - \
    "$MANIFEST" \
    "$CONTAINER_TSV" \
    "$HOST_TSV" \
    "$MIGRATION_TSV" <<'PY'
from __future__ import annotations

import json
import re
import sys
from pathlib import PurePosixPath

manifest_path, container_path, host_path, migration_path = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as source:
    manifest = json.load(source)
if manifest.get("schema_version") != "2.0":
    raise SystemExit("unsupported bundle manifest schema")
commit = str(manifest.get("repo_commit") or "")
if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
    raise SystemExit("bundle repo_commit is invalid")
if manifest.get("release_id") != commit:
    raise SystemExit("bundle release identity does not match repo_commit")
if manifest.get("repo_dirty") is not False:
    raise SystemExit("dirty repository bundle is not deployable")

def validate_relative(value: object, label: str) -> str:
    normalized = str(value or "")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"{label} is invalid: {normalized!r}")
    if any(char in normalized for char in "\t\r\n"):
        raise SystemExit(f"{label} contains control characters")
    return normalized

files = manifest.get("files")
if not isinstance(files, list) or not files:
    raise SystemExit("container file manifest is empty")
seen_paths = set()
seen_targets = set()
with open(container_path, "w", encoding="utf-8") as output:
    for item in files:
        bundle_path = validate_relative(
            item.get("bundle_path"),
            "container bundle_path",
        )
        target = str(item.get("mount_target") or "")
        digest = str(item.get("sha256") or "")
        if not target.startswith("/") or any(
            char in target for char in "\t\r\n"
        ):
            raise SystemExit(f"container mount target is invalid: {target!r}")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SystemExit(f"container SHA256 is invalid: {bundle_path}")
        if bundle_path in seen_paths or target in seen_targets:
            raise SystemExit("container manifest contains duplicate paths")
        seen_paths.add(bundle_path)
        seen_targets.add(target)
        output.write(f"{bundle_path}\t{target}\t{digest}\n")

host_files = manifest.get("host_files")
if not isinstance(host_files, list):
    raise SystemExit("host file manifest is invalid")
expected_host = {
    "services/control-plane/api/read_api.py",
    "services/control-plane/tools/exchange_state_recorder.py",
    "packages/execution-domain/execution_domain/control_plane.py",
    "scripts/account_a_live_trade_executor.py",
    "scripts/account_a_live_trade_http_adapter.py",
    "infra/systemd/trader-v3-controlplane.service",
}
actual_host = set()
with open(host_path, "w", encoding="utf-8") as output:
    for item in host_files:
        bundle_path = validate_relative(
            item.get("bundle_path"),
            "host bundle_path",
        )
        target = validate_relative(
            item.get("target_relative"),
            "host target_relative",
        )
        digest = str(item.get("sha256") or "")
        install_mode = str(item.get("install_mode") or "")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SystemExit(f"host SHA256 is invalid: {bundle_path}")
        expected_mode = "0644"
        if target.startswith("scripts/account_a_live_trade_"):
            expected_mode = "0755"
        if install_mode != expected_mode:
            raise SystemExit(f"host install mode is invalid: {target}")
        actual_host.add(target)
        output.write(
            f"{bundle_path}\t{target}\t{digest}\t{install_mode}\n"
        )
if actual_host != expected_host:
    raise SystemExit("host control-plane file set is incomplete")

migration_files = manifest.get("migration_files")
if not isinstance(migration_files, list):
    raise SystemExit("migration file manifest is invalid")
expected_migrations = {
    "services/control-plane/db/migrate.py",
    "db/migrations/0005_order_management.up.sql",
    "db/migrations/0005_order_management.down.sql",
    "db/migrations/0010_evidence_and_poll_indexes.up.sql",
    "db/migrations/0010_evidence_and_poll_indexes.down.sql",
}
actual_migrations = set()
with open(migration_path, "w", encoding="utf-8") as output:
    for item in migration_files:
        bundle_path = validate_relative(
            item.get("bundle_path"),
            "migration bundle_path",
        )
        target = validate_relative(
            item.get("target_relative"),
            "migration target_relative",
        )
        digest = str(item.get("sha256") or "")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SystemExit(f"migration SHA256 is invalid: {bundle_path}")
        actual_migrations.add(target)
        output.write(f"{bundle_path}\t{target}\t{digest}\n")
if actual_migrations != expected_migrations:
    raise SystemExit("canonical migration file set is incomplete")

deployment_paths = {
    str(item.get("bundle_path") or "")
    for item in manifest.get("deployment_files") or []
}
required_tools = {
    "deploy.sh",
    "tools/hk-gen-recreate-patched.py",
    "tools/verify-exchange-state-recorder.py",
}
if not required_tools.issubset(deployment_paths):
    raise SystemExit("deployment tools are incomplete")
print(commit)
PY
)" || die "bundle manifest validation failed"

docker inspect "$NODE_CONTAINER" >"$TEMP_DIR/container-inspect.json"

BINANCE_EXEC_DST="$(
  docker exec "$NODE_CONTAINER" python -c \
    'import nautilus_trader.adapters.binance.execution as module; print(module.__file__)'
)"
BINANCE_FUTURES_EXEC_DST="$(
  docker exec "$NODE_CONTAINER" python -c \
    'import nautilus_trader.adapters.binance.futures.execution as module; print(module.__file__)'
)"
case "$BINANCE_EXEC_DST" in
  /*) ;;
  *) die "account-a Binance execution destination is invalid" ;;
esac
case "$BINANCE_FUTURES_EXEC_DST" in
  /*) ;;
  *) die "account-a Binance futures execution destination is invalid" ;;
esac

python3 - \
  "$STAGING/tools/hk-gen-recreate-patched.py" \
  "$CONTAINER_TSV" \
  "$BINANCE_EXEC_DST" \
  "$BINANCE_FUTURES_EXEC_DST" <<'PY'
import runpy
import sys

generator_path, manifest_path, binance_dst, futures_dst = sys.argv[1:]
namespace = runpy.run_path(generator_path)
expected = dict(namespace["PATCH_MOUNT_TARGETS"])
expected["binance_execution.py"] = binance_dst
expected["binance_futures_execution.py"] = futures_dst
actual = {}
with open(manifest_path, encoding="utf-8") as source:
    for raw in source:
        bundle_path, mount_target, _digest = raw.rstrip("\n").split("\t")
        actual[bundle_path] = mount_target
if actual != expected:
    missing = sorted(set(expected.items()) - set(actual.items()))
    extra = sorted(set(actual.items()) - set(expected.items()))
    raise SystemExit(
        f"bundle and recreate mount plans differ: "
        f"missing={missing} extra={extra}"
    )
PY

READY_BODY="$TEMP_DIR/ready-before.json"
curl --silent --show-error --max-time 5 \
  "http://127.0.0.1:$NODE_PORT/ready" >"$READY_BODY"
python3 - "$READY_BODY" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
if str(payload.get("trading_state") or "").upper() != "HALTED":
    raise SystemExit("account-a must be HALTED before deployment")
PY

[ ! -e "$BACKUP_ROOT" ] || die "backup path already exists: $BACKUP_ROOT"
mkdir -p "$BACKUP_ROOT/files"
chmod 0700 "$BACKUP_ROOT"
: >"$BACKUP_ROOT/index.tsv"


backup_target() {
  local target="$1"
  local relative="${target#/}"
  local backup_relative="files/$relative"
  if [ -e "$target" ] || [ -L "$target" ]; then
    mkdir -p "$BACKUP_ROOT/$(dirname "$backup_relative")"
    cp -a "$target" "$BACKUP_ROOT/$backup_relative"
    printf 'present\t%s\t%s\n' \
      "$target" "$backup_relative" >>"$BACKUP_ROOT/index.tsv"
  else
    printf 'absent\t%s\t-\n' \
      "$target" >>"$BACKUP_ROOT/index.tsv"
  fi
}

prepare_file_target() {
  local target="$1"
  if [ -L "$target" ] || {
    [ -e "$target" ] && [ ! -f "$target" ]
  }; then
    rm -rf -- "$target"
  fi
}


while IFS=$'\t' read -r bundle_path mount_target expected_sha; do
  : "$mount_target" "$expected_sha"
  backup_target "$PATCH_DIR/$bundle_path"
done <"$CONTAINER_TSV"

while IFS=$'\t' read -r \
  bundle_path target_relative expected_sha install_mode; do
  : "$bundle_path" "$expected_sha" "$install_mode"
  backup_target "$T/$target_relative"
done <"$HOST_TSV"

while IFS=$'\t' read -r bundle_path target_relative expected_sha; do
  : "$bundle_path" "$expected_sha"
  backup_target "$T/$target_relative"
done <"$MIGRATION_TSV"

backup_target "$GENERATOR_TARGET"
backup_target "$RECREATE_TARGET"
backup_target "$DEPLOYED_COMMIT_TARGET"
backup_target "$CONTROL_PLANE_UNIT_TARGET"
cp "$TEMP_DIR/container-inspect.json" "$BACKUP_ROOT/container-inspect.json"
cp "$MANIFEST" "$BACKUP_ROOT/bundle-manifest.json"
cp "$CHECKSUMS" "$BACKUP_ROOT/staging-SHA256SUMS"
cp "$RECORDER_VERIFIER" \
  "$BACKUP_ROOT/verify-exchange-state-recorder.py"
chmod 0700 "$BACKUP_ROOT/verify-exchange-state-recorder.py"

cat >"$ROLLBACK_PATH" <<'ROLLBACK'
#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INDEX="$BACKUP_ROOT/index.tsv"
LOCK="/var/lock/trader-v3-account-stall-operation.lock"
T="$(cd "$BACKUP_ROOT/../.." && pwd)"
NODE_CONTAINER="trader-v3-node-a"
CONTROL_PLANE_UNIT="trader-v3-controlplane.service"
EXCHANGE_STATE_UNIT="trader-v3-exchange-state.service"
RECREATE_TARGET="$T/recreate-$NODE_CONTAINER.sh"
RECORDER_TARGET="$T/services/control-plane/tools/exchange_state_recorder.py"
RECORDER_VERIFIER="$BACKUP_ROOT/verify-exchange-state-recorder.py"
RECORDER_WATERMARK="$BACKUP_ROOT/rollback-exchange-state-watermark.txt"

exec 9>"$LOCK"
flock -n 9 || {
  echo "FATAL: another operation holds $LOCK" >&2
  exit 1
}

(
  cd "$BACKUP_ROOT"
  sha256sum -c SHA256SUMS >/dev/null
)

systemctl stop "$CONTROL_PLANE_UNIT" "$EXCHANGE_STATE_UNIT"
"$T/.venv-cp/bin/python" "$RECORDER_VERIFIER" capture \
  --env-file "$T/.env.v3" \
  --output "$RECORDER_WATERMARK"

while IFS=$'\t' read -r status target backup_relative; do
  case "$target" in
    */services/control-plane/db/migrate.py|\
    */db/migrations/0005_order_management.*.sql|\
    */db/migrations/0010_evidence_and_poll_indexes.*.sql)
      continue
      ;;
  esac
  case "$status" in
    present)
      rm -rf -- "$target"
      mkdir -p "$(dirname "$target")"
      cp -a "$BACKUP_ROOT/$backup_relative" "$target"
      ;;
    absent)
      rm -rf -- "$target"
      ;;
    *)
      echo "FATAL: invalid rollback index status: $status" >&2
      exit 1
      ;;
  esac
done <"$INDEX"

systemctl daemon-reload
systemctl start "$CONTROL_PLANE_UNIT"
systemctl is-active --quiet "$CONTROL_PLANE_UNIT"
systemctl start "$EXCHANGE_STATE_UNIT"
systemctl is-active --quiet "$EXCHANGE_STATE_UNIT"
EXCHANGE_STATE_PID="$(
  systemctl show \
    --property=MainPID \
    --value \
    "$EXCHANGE_STATE_UNIT"
)"
"$T/.venv-cp/bin/python" "$RECORDER_VERIFIER" process \
  --pid "$EXCHANGE_STATE_PID" \
  --working-directory "$T" \
  --python "$T/.venv-cp/bin/python" \
  --recorder "$RECORDER_TARGET"
"$T/.venv-cp/bin/python" "$RECORDER_VERIFIER" wait \
  --env-file "$T/.env.v3" \
  --watermark-file "$RECORDER_WATERMARK" \
  --account account-a \
  --timeout-seconds 150 \
  --poll-seconds 5
[ -x "$RECREATE_TARGET" ] || {
  echo "FATAL: restored recreate script is unavailable" >&2
  exit 1
}
grep -Fq "NAUTILUS_INITIAL_TRADING_STATE=HALTED" "$RECREATE_TARGET" || {
  echo "FATAL: restored recreate script lacks HALTED startup" >&2
  exit 1
}
bash "$RECREATE_TARGET"
echo "rollback restored account-a in HALTED startup mode"
echo "database backup retained at $BACKUP_ROOT/postgres-pre-migration.dump"
ROLLBACK
chmod 0700 "$ROLLBACK_PATH"

DATABASE_URL="$(
  "$T/.venv-cp/bin/python" - "$T/.env.v3" <<'PY'
import sys
from pathlib import Path

database_url = ""
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
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
    raise SystemExit("DATABASE_URL is missing")
if "\n" in database_url or "\r" in database_url:
    raise SystemExit("DATABASE_URL contains a newline")
print(database_url)
PY
)" || die "DATABASE_URL could not be loaded"

docker inspect "$POSTGRES_CONTAINER" >/dev/null
DB_DUMP="$BACKUP_ROOT/postgres-pre-migration.dump"
DB_RESTORE_LIST="$BACKUP_ROOT/postgres-pre-migration.dump.list"
docker exec "$POSTGRES_CONTAINER" \
  pg_dump \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --format custom \
  --no-owner \
  --no-privileges >"$DB_DUMP"
[ -s "$DB_DUMP" ] || die "pre-migration PostgreSQL backup is empty"
docker exec -i "$POSTGRES_CONTAINER" \
  pg_restore --list <"$DB_DUMP" >"$DB_RESTORE_LIST"
[ -s "$DB_RESTORE_LIST" ] \
  || die "pre-migration PostgreSQL restore listing is empty"
sha256sum "$DB_DUMP" >"$BACKUP_ROOT/postgres-pre-migration.dump.sha256"
(
  cd "$BACKUP_ROOT"
  sha256sum -c postgres-pre-migration.dump.sha256 >/dev/null
)

DATABASE_URL="$DATABASE_URL" \
  "$T/.venv-cp/bin/python" - \
    "$BACKUP_ROOT/schema-migrations-before.json" <<'PY'
import json
import os
import sys

import psycopg2

legacy = [
    "0001",
    "0002",
    "0003",
    "0004",
    "0006",
    "0007",
    "0008",
    "0009",
]
target = [
    "0001",
    "0002",
    "0003",
    "0004",
    "0005",
    "0006",
    "0007",
    "0008",
    "0009",
    "0010",
]
with psycopg2.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        )
        rows = cursor.fetchall()
versions = [str(row[0]) for row in rows]
payload = [
    {"version": str(version), "name": str(name)}
    for version, name in rows
]
with open(sys.argv[1], "w", encoding="utf-8") as output:
    json.dump(payload, output, indent=2, sort_keys=True)
    output.write("\n")
if versions not in (legacy, target):
    raise SystemExit(
        f"unexpected pre-migration exact set: {versions}"
    )
PY

(
  cd "$BACKUP_ROOT"
  find . -type f ! -name SHA256SUMS -print0 \
    | sort -z \
    | xargs -0 sha256sum >SHA256SUMS
  sha256sum -c SHA256SUMS >/dev/null
)

echo "== backup complete: $BACKUP_ROOT"

while IFS=$'\t' read -r bundle_path target_relative expected_sha; do
  source_path="$STAGING/$bundle_path"
  target_path="$T/$target_relative"
  prepare_file_target "$target_path"
  install -D -m 0644 "$source_path" "$target_path"
  actual_sha="$(sha256sum "$target_path" | awk '{print $1}')"
  [ "$actual_sha" = "$expected_sha" ] \
    || die "migration file SHA256 mismatch: $target_path"
done <"$MIGRATION_TSV"
compile_python "$T/services/control-plane/db/migrate.py"

DATABASE_URL="$DATABASE_URL" \
  "$T/.venv-cp/bin/python" \
  "$T/services/control-plane/db/migrate.py" up \
  | tee "$BACKUP_ROOT/migrate-up.log"

DATABASE_URL="$DATABASE_URL" \
  "$T/.venv-cp/bin/python" - \
    "$BACKUP_ROOT/schema-migrations-before.json" \
    "$BACKUP_ROOT/schema-migrations-after.json" <<'PY'
import json
import os
import sys

import psycopg2

expected_after = [
    "0001",
    "0002",
    "0003",
    "0004",
    "0005",
    "0006",
    "0007",
    "0008",
    "0009",
    "0010",
]
with open(sys.argv[1], encoding="utf-8") as source:
    before = json.load(source)
before_versions = {
    str(item["version"])
    for item in before
}
with psycopg2.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        )
        rows = cursor.fetchall()
after = [
    {"version": str(version), "name": str(name)}
    for version, name in rows
]
with open(sys.argv[2], "w", encoding="utf-8") as output:
    json.dump(after, output, indent=2, sort_keys=True)
    output.write("\n")
after_versions = [item["version"] for item in after]
if after_versions != expected_after:
    raise SystemExit(
        f"unexpected post-migration exact set: {after_versions}"
    )
delta = set(after_versions) - before_versions
expected_delta = set(expected_after) - before_versions
if delta != expected_delta:
    raise SystemExit(f"unexpected migration delta: {sorted(delta)}")
PY

DATABASE_URL="$DATABASE_URL" \
  "$T/.venv-cp/bin/python" - <<'PY'
import os

import psycopg2

tables = {
    "execution_jobs",
    "order_events",
    "order_links",
    "protective_orders_projection",
    "risk_reservations",
    "order_management_settings",
    "order_management_setting_versions",
    "reconciliation_runs",
    "reconciliation_findings",
    "node_command_runs",
    "price_feed_status",
}
indexes = {
    "execution_jobs_pkey",
    "uq_execution_jobs_account_idempotency",
    "idx_execution_jobs_intent_id",
    "idx_execution_jobs_status",
    "idx_execution_jobs_request_id",
    "idx_orders_projection_execution_job_id",
    "idx_orders_projection_venue_symbol",
    "idx_orders_projection_lifecycle_role",
    "order_events_pkey",
    "uq_order_events_event_id",
    "idx_order_events_execution_job_id",
    "idx_order_events_account_event",
    "order_links_pkey",
    "uq_order_links_pair_type",
    "idx_order_links_account_position",
    "protective_orders_projection_pkey",
    "uq_protective_orders_active_role",
    "idx_protective_orders_order_projection_id",
    "risk_reservations_pkey",
    "uq_risk_reservations_account_idempotency",
    "idx_risk_reservations_execution_job_id",
    "idx_risk_reservations_status",
    "order_management_settings_pkey",
    "uq_order_management_settings_scope_version",
    "idx_order_management_settings_scope",
    "idx_order_management_settings_request_id",
    "order_management_setting_versions_pkey",
    "uq_order_management_setting_versions_scope_version",
    "reconciliation_runs_pkey",
    "idx_reconciliation_runs_status",
    "idx_reconciliation_runs_request_id",
    "reconciliation_findings_pkey",
    "idx_reconciliation_findings_run",
    "idx_reconciliation_findings_status",
    "node_command_runs_pkey",
    "uq_node_command_runs_node_idempotency",
    "idx_node_command_runs_request_id",
    "idx_node_command_runs_status",
    "price_feed_status_pkey",
    "uq_price_feed_status_identity",
    "idx_price_feed_status_stale",
    "idx_price_feed_status_updated_at",
    "idx_audit_events_request_id",
    "idx_execution_events_targeted_opening_evidence",
    "idx_trade_intents_pending_opening_symbols",
    "idx_command_node_acks_pending_poll",
}
columns = {
    ("orders_projection", "execution_job_id"),
    ("orders_projection", "venue_symbol"),
    ("orders_projection", "lifecycle_role"),
    ("audit_events", "action"),
    ("audit_events", "target"),
    ("audit_events", "before_state"),
    ("audit_events", "after_state"),
    ("audit_events", "reason"),
    ("audit_events", "request_id"),
}
with psycopg2.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname='public'
              AND tablename = ANY(%s)
            """,
            (list(tables),),
        )
        actual_tables = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname='public'
              AND indexname = ANY(%s)
            """,
            (list(indexes),),
        )
        actual_indexes = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema='public'
              AND (table_name, column_name) IN (
                ('orders_projection', 'execution_job_id'),
                ('orders_projection', 'venue_symbol'),
                ('orders_projection', 'lifecycle_role'),
                ('audit_events', 'action'),
                ('audit_events', 'target'),
                ('audit_events', 'before_state'),
                ('audit_events', 'after_state'),
                ('audit_events', 'reason'),
                ('audit_events', 'request_id')
              )
            """
        )
        actual_columns = {
            (row[0], row[1])
            for row in cursor.fetchall()
        }
        cursor.execute(
            """
            SELECT to_regprocedure(
                'public.prevent_order_management_setting_versions_update()'
            ) IS NOT NULL
            """
        )
        function_exists = bool(cursor.fetchone()[0])
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_trigger trigger
                JOIN pg_class relation
                  ON relation.oid=trigger.tgrelid
                JOIN pg_namespace namespace
                  ON namespace.oid=relation.relnamespace
                WHERE namespace.nspname='public'
                  AND relation.relname='order_management_setting_versions'
                  AND trigger.tgname=
                    'trg_order_management_setting_versions_immutable'
                  AND NOT trigger.tgisinternal
            )
            """
        )
        trigger_exists = bool(cursor.fetchone()[0])
if actual_tables != tables:
    raise SystemExit(
        f"0005 table verification failed: {sorted(tables - actual_tables)}"
    )
if actual_indexes != indexes:
    raise SystemExit(
        f"migration index verification failed: "
        f"{sorted(indexes - actual_indexes)}"
    )
if actual_columns != columns:
    raise SystemExit(
        f"0005 column verification failed: "
        f"{sorted(columns - actual_columns)}"
    )
if not function_exists or not trigger_exists:
    raise SystemExit("0005 trigger/function verification failed")
PY
echo "== migrations verified: exact delta 0005,0010"

# Control-plane is updated and verified before the node container is recreated.
while IFS=$'\t' read -r \
  bundle_path target_relative expected_sha install_mode; do
  source_path="$STAGING/$bundle_path"
  target_path="$T/$target_relative"
  prepare_file_target "$target_path"
  install -D -m "$install_mode" "$source_path" "$target_path"
  actual_sha="$(sha256sum "$target_path" | awk '{print $1}')"
  [ "$actual_sha" = "$expected_sha" ] \
    || die "host SHA256 mismatch: $target_path"
  case "$target_path" in
    *.py)
      compile_python "$target_path"
      ;;
  esac
done <"$HOST_TSV"

grep -Fq 'command_expires_at' \
  "$T/services/control-plane/api/read_api.py" \
  || die "control-plane command expiry field is missing"
grep -Fq '"source_evidence"' \
  "$T/services/control-plane/api/read_api.py" \
  || die "control-plane opening evidence contract is missing"
[ -f "$RECORDER_TARGET" ] \
  || die "exchange-state recorder install is missing"
[ -f "$CONTROL_PLANE_UNIT_SOURCE" ] \
  || die "control-plane unit source is missing"
grep -Fq -- "--timeout-graceful-shutdown 10" \
  "$CONTROL_PLANE_UNIT_SOURCE" \
  || die "control-plane graceful shutdown timeout is missing"
grep -Fq "TimeoutStopSec=20s" "$CONTROL_PLANE_UNIT_SOURCE" \
  || die "control-plane stop timeout is missing"
grep -Fq "KillMode=control-group" "$CONTROL_PLANE_UNIT_SOURCE" \
  || die "control-plane kill mode is invalid"
prepare_file_target "$CONTROL_PLANE_UNIT_TARGET"
install -D -m 0644 "$CONTROL_PLANE_UNIT_SOURCE" "$CONTROL_PLANE_UNIT_TARGET"
UNIT_SOURCE_SHA="$(sha256sum "$CONTROL_PLANE_UNIT_SOURCE" | awk '{print $1}')"
UNIT_TARGET_SHA="$(sha256sum "$CONTROL_PLANE_UNIT_TARGET" | awk '{print $1}')"
[ "$UNIT_SOURCE_SHA" = "$UNIT_TARGET_SHA" ] \
  || die "control-plane unit SHA256 mismatch"
systemctl daemon-reload
systemctl stop "$EXCHANGE_STATE_UNIT"
RECORDER_WATERMARK="$TEMP_DIR/exchange-state-watermark.txt"
"$T/.venv-cp/bin/python" "$RECORDER_VERIFIER" capture \
  --env-file "$T/.env.v3" \
  --output "$RECORDER_WATERMARK" \
  || die "exchange-state recorder watermark capture failed"
systemctl restart "$CONTROL_PLANE_UNIT"
systemctl is-active --quiet "$CONTROL_PLANE_UNIT" \
  || die "$CONTROL_PLANE_UNIT failed to restart"
systemctl start "$EXCHANGE_STATE_UNIT"
systemctl is-active --quiet "$EXCHANGE_STATE_UNIT" \
  || die "$EXCHANGE_STATE_UNIT failed to start"
EXCHANGE_STATE_PID="$(
  systemctl show \
    --property=MainPID \
    --value \
    "$EXCHANGE_STATE_UNIT"
)" || die "$EXCHANGE_STATE_UNIT MainPID is unavailable after start"
"$T/.venv-cp/bin/python" "$RECORDER_VERIFIER" process \
  --pid "$EXCHANGE_STATE_PID" \
  --working-directory "$T" \
  --python "$T/.venv-cp/bin/python" \
  --recorder "$RECORDER_TARGET" \
  || die "$EXCHANGE_STATE_UNIT process identity changed after start"
"$T/.venv-cp/bin/python" "$RECORDER_VERIFIER" wait \
  --env-file "$T/.env.v3" \
  --watermark-file "$RECORDER_WATERMARK" \
  --account account-a \
  --require-position-field leverage \
  --require-position-field margin_type \
  --require-position-field isolated_margin \
  --require-position-field is_auto_add_margin \
  --timeout-seconds 150 \
  --poll-seconds 5 \
  || die "exchange-state recorder did not publish fresh snapshots"

CONTROL_PLANE_READY=0
for _ in $(seq 1 30); do
  if curl --silent --show-error --fail --max-time 5 \
    "http://127.0.0.1:8080/openapi.json" \
    >"$TEMP_DIR/control-plane-openapi.json"; then
    CONTROL_PLANE_READY=1
    break
  fi
  sleep 1
done
[ "$CONTROL_PLANE_READY" = "1" ] \
  || die "control-plane did not become reachable on port 8080"
python3 - "$TEMP_DIR/control-plane-openapi.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
paths = payload.get("paths") or {}
if "/v1/nodes/{node_id}/commands" not in paths:
    raise SystemExit("control-plane command polling route is missing")
if "/v1/nodes/{node_id}/exchange-state" not in paths:
    raise SystemExit("control-plane exchange-state route is missing")
PY
"$T/.venv-cp/bin/python" - "$T/.env.v3" <<'PY'
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def read_environment(path: Path) -> dict[str, str]:
    environment = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        environment[key] = value
    return environment


environment = read_environment(Path(sys.argv[1]))
node_id = "nautilus-node-account-a"
account_id = "account-a"
token = environment.get("NAUTILUS_NODE_TOKEN", "").strip()
raw_bindings = environment.get("NAUTILUS_NODE_AUTH_JSON", "").strip()
if raw_bindings:
    bindings = json.loads(raw_bindings)
    binding = bindings.get(node_id)
    if not isinstance(binding, dict):
        raise SystemExit("account-a node auth binding is missing")
    if str(binding.get("account_id") or "").strip() != account_id:
        raise SystemExit("account-a node auth binding is invalid")
    token = str(binding.get("token") or "").strip()
if not token:
    raise SystemExit("account-a node token is missing")

query = urllib.parse.urlencode({"account_id": account_id})
request = urllib.request.Request(
    (
        "http://127.0.0.1:8080"
        f"/v1/nodes/{node_id}/exchange-state?{query}"
    ),
    headers={
        "Authorization": f"Bearer {token}",
        "X-Node-ID": node_id,
        "X-Account-ID": account_id,
    },
)
with urllib.request.urlopen(request, timeout=5) as response:
    payload = json.load(response)
if payload.get("account_id") != account_id:
    raise SystemExit("exchange-state account identity mismatch")
surface = payload.get("opening_execution_evidence")
if not isinstance(surface, dict):
    raise SystemExit("opening execution evidence surface is missing")
if not isinstance(surface.get("authoritative"), bool):
    raise SystemExit("opening execution evidence authority is invalid")
items = surface.get("items")
if not isinstance(items, list):
    raise SystemExit("opening execution evidence items are invalid")
for item in items:
    if not isinstance(item, dict):
        raise SystemExit("opening execution evidence item is invalid")
    source_evidence = item.get("source_evidence")
    if not isinstance(source_evidence, list) or not source_evidence:
        raise SystemExit("source-specific opening evidence is missing")
    for proof in source_evidence:
        if not isinstance(proof, dict):
            raise SystemExit("source-specific opening proof is invalid")
        required = {
            "account_id",
            "client_order_id",
            "state",
            "source",
            "observed_at",
            "filled_quantity",
        }
        if not required.issubset(proof):
            raise SystemExit("source-specific opening proof is incomplete")
PY
echo "== control-plane verified on 127.0.0.1:8080"

mkdir -p "$PATCH_DIR"
while IFS=$'\t' read -r bundle_path mount_target expected_sha; do
  : "$mount_target"
  source_path="$STAGING/$bundle_path"
  target_path="$PATCH_DIR/$bundle_path"
  prepare_file_target "$target_path"
  install -D -m 0644 "$source_path" "$target_path"
  actual_sha="$(sha256sum "$target_path" | awk '{print $1}')"
  [ "$actual_sha" = "$expected_sha" ] \
    || die "container patch SHA256 mismatch: $target_path"
done <"$CONTAINER_TSV"

install -m 0755 \
  "$STAGING/tools/hk-gen-recreate-patched.py" \
  "$GENERATOR_TARGET"

TRADER_ROOT="$T" \
NODE_MEMORY_LIMIT="$MEMORY_LIMIT" \
NODE_MEMORY_SWAP_LIMIT="$MEMORY_SWAP_LIMIT" \
  python3 "$GENERATOR_TARGET" \
    "$NODE_CONTAINER" \
    "$BINANCE_EXEC_DST" \
    "$BINANCE_FUTURES_EXEC_DST"

grep -Fq "NAUTILUS_INITIAL_TRADING_STATE=HALTED" "$RECREATE_TARGET" \
  || die "generated recreate script lacks HALTED startup"
bash "$RECREATE_TARGET"

docker inspect "$NODE_CONTAINER" >/dev/null
RESOURCE_VALUES="$(
  docker inspect --format \
    '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' \
    "$NODE_CONTAINER"
)"
python3 - "$MEMORY_LIMIT" "$MEMORY_SWAP_LIMIT" "$RESOURCE_VALUES" <<'PY'
import re
import sys

def parse(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([bkmg]?)", value.lower())
    if match is None:
        raise SystemExit(f"invalid memory value: {value}")
    number = int(match.group(1))
    exponent = {"": 0, "b": 0, "k": 1, "m": 2, "g": 3}[match.group(2)]
    return number * (1024 ** exponent)

actual = sys.argv[3].split()
if len(actual) != 2:
    raise SystemExit("Docker memory inspection is invalid")
if int(actual[0]) != parse(sys.argv[1]):
    raise SystemExit("account-a Docker memory limit mismatch")
if int(actual[1]) != parse(sys.argv[2]):
    raise SystemExit("account-a Docker memory-swap limit mismatch")
PY

NODE_READY=0
for _ in $(seq 1 45); do
  if curl --silent --show-error --max-time 5 \
    "http://127.0.0.1:$NODE_PORT/ready" \
    >"$TEMP_DIR/ready-after.json"; then
    if python3 - "$TEMP_DIR/ready-after.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
if str(payload.get("trading_state") or "").upper() != "HALTED":
    raise SystemExit(1)
if not isinstance(payload.get("ready"), bool):
    raise SystemExit(1)
PY
    then
      NODE_READY=1
      break
    fi
  fi
  sleep 2
done
[ "$NODE_READY" = "1" ] \
  || die "account-a /ready did not report HALTED"

python3 - "$MANIFEST" "$T" "$NODE_CONTAINER" <<'PY'
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

manifest_path, trader_root, container = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as source:
    files = json.load(source)["files"]
inspect_raw = subprocess.check_output(
    ["docker", "inspect", container],
    text=True,
)
inspected = json.loads(inspect_raw)[0]
mounts = {
    item["Destination"]: item
    for item in inspected.get("Mounts") or []
}
for item in files:
    bundle_path = item["bundle_path"]
    target = item["mount_target"]
    expected_source = str(
        (Path(trader_root) / "container-patches" / bundle_path).resolve()
    )
    mount = mounts.get(target)
    if mount is None:
        raise SystemExit(f"missing container mount: {target}")
    actual_source = str(Path(mount["Source"]).resolve())
    if actual_source != expected_source or mount.get("RW", True):
        raise SystemExit(f"container mount mismatch: {target}")
    result = subprocess.run(
        ["docker", "exec", container, "sha256sum", target],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise SystemExit(f"cannot hash container target: {target}")
    actual_hash = result.stdout.split()[0]
    if actual_hash != item["sha256"]:
        raise SystemExit(f"container target SHA256 mismatch: {target}")
print(f"verified {len(files)} account-a mount targets")
PY

NODE_PID="$(
  docker inspect --format '{{.State.Pid}}' "$NODE_CONTAINER"
)"
python3 - "$MANIFEST" "$NODE_PID" <<'PY'
import json
from pathlib import Path
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    targets = {
        item["mount_target"]
        for item in json.load(source)["files"]
    }
mountinfo = Path(f"/proc/{int(sys.argv[2])}/mountinfo").read_text(
    encoding="utf-8"
)
seen = set()
for line in mountinfo.splitlines():
    fields = line.split()
    if len(fields) < 6:
        continue
    target = fields[4]
    if target not in targets:
        continue
    seen.add(target)
    if "deleted" in line.lower():
        raise SystemExit(f"deleted inode mount detected: {target}")
missing = targets - seen
if missing:
    raise SystemExit(f"mountinfo targets missing: {sorted(missing)}")
PY

MARKER_TMP="$TEMP_DIR/DEPLOYED_COMMIT.txt"
printf '%s account-stall-availability bind-mount deployed=%s\n' \
  "$RELEASE_COMMIT" "$STAMP" >"$MARKER_TMP"
install -m 0644 "$MARKER_TMP" "$DEPLOYED_COMMIT_TARGET"

(
  cd "$BACKUP_ROOT"
  find . -type f ! -name SHA256SUMS -print0 \
    | sort -z \
    | xargs -0 sha256sum >SHA256SUMS
  sha256sum -c SHA256SUMS >/dev/null
)

echo "== deployed commit: $RELEASE_COMMIT"
echo "== account-a is HALTED; /ready, cgroup, mount SHA256 and inode checks passed"
echo "== rollback: bash $ROLLBACK_PATH"
