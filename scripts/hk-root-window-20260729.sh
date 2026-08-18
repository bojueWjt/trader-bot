#!/usr/bin/env bash
set -Eeuo pipefail

D="${DEPLOY_DIR:-/home/balen/deploy-20260729}"
T="${TRADER_ROOT:-/srv/trader-v3}"
CP="$T/container-patches"
STAMP="${DEPLOY_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
BACKUP_ROOT="${BACKUP_ROOT:-$T/backups/deploy-$STAMP}"
MEMINFO_PATH="${MEMINFO_PATH:-/proc/meminfo}"
NODE_A="${NODE_A:-trader-v3-node-a}"
NODE_B="${NODE_B:-trader-v3-node-b}"
MUTATION_STARTED=0
BINANCE_EXEC_DST="${BINANCE_EXEC_DST:-/usr/local/lib/python3.12/site-packages/nautilus_trader/adapters/binance/execution.py}"
BINANCE_FUTURES_EXEC_DST="${BINANCE_FUTURES_EXEC_DST:-/usr/local/lib/python3.12/site-packages/nautilus_trader/adapters/binance/futures/execution.py}"
NODE_PATCH_MOUNTS=(
  "intent_execution_planner.py=/app/strategy/intent_execution_planner.py"
  "contracts.py=/app/execution_domain/contracts.py"
  "control_plane.py=/app/execution_domain/control_plane.py"
  "portfolio_baseline.py=/app/execution_domain/portfolio_baseline.py"
  "http_client.py=/app/execution_domain/http_client.py"
  "projection_actor.py=/app/projection/actor.py"
  "event_mapper.py=/app/projection/event_mapper.py"
  "intent_execution_strategy.py=/app/strategy/intent_execution_strategy.py"
  "exchange_cancel_adapter.py=/app/runtime/exchange_cancel_adapter.py"
  "lifecycle.py=/app/runtime/lifecycle.py"
  "binance_adapter_config.py=/app/runtime/binance_adapter_config.py"
  "node.py=/app/app/node.py"
  "nautilus_actors.py=/app/app/nautilus_actors.py"
  "binance_execution.py=$BINANCE_EXEC_DST"
  "binance_futures_execution.py=$BINANCE_FUTURES_EXEC_DST"
)

die() {
  echo "FATAL: $*" >&2
  if [ "$MUTATION_STARTED" -eq 1 ]; then
    docker stop -t 20 "$NODE_A" "$NODE_B" >/dev/null 2>&1 || true
    echo "Nodes are stopped or HALTED" >&2
    echo "Rollback: sudo bash $D/hk-rollback-20260729.sh $BACKUP_ROOT" >&2
  fi
  exit 1
}

on_error() {
  local status=$?
  if [ "$MUTATION_STARTED" -eq 1 ]; then
    docker stop -t 20 "$NODE_A" "$NODE_B" >/dev/null 2>&1 || true
  fi
  echo "DEPLOY FAILED: nodes are stopped or HALTED" >&2
  echo "Rollback: sudo bash $D/hk-rollback-20260729.sh $BACKUP_ROOT" >&2
  exit "$status"
}

on_signal() {
  local status="$1"
  local signal_name="$2"
  if [ "$MUTATION_STARTED" -eq 1 ]; then
    docker stop -t 20 "$NODE_A" "$NODE_B" >/dev/null 2>&1 || true
  fi
  echo "DEPLOY INTERRUPTED by $signal_name: nodes are stopped or HALTED" >&2
  if [ -r "$BACKUP_ROOT/index.tsv" ]; then
    echo "Rollback: sudo bash $D/hk-rollback-20260729.sh $BACKUP_ROOT" >&2
  fi
  exit "$status"
}

trap on_error ERR
trap 'on_signal 129 HUP' HUP
trap 'on_signal 130 INT' INT
trap 'on_signal 143 TERM' TERM

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command missing: $1"
}

backup_path() {
  local path="$1"
  if [ -e "$path" ]; then
    printf 'present\t%s\n' "$path" >>"$BACKUP_ROOT/index.tsv"
    mkdir -p "$BACKUP_ROOT/files$(dirname "$path")"
    cp -a "$path" "$BACKUP_ROOT/files$path"
    return
  fi
  printf 'absent\t%s\n' "$path" >>"$BACKUP_ROOT/index.tsv"
}

load_database_url() {
  DATABASE_URL=$(
    python3 - "$T/.env.v3" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key.strip() != "DATABASE_URL":
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    print(value)
    break
PY
  )
  [ -n "$DATABASE_URL" ] || die "DATABASE_URL missing from $T/.env.v3"
  export DATABASE_URL
}

database_marker() {
  "$T/.venv-cp/bin/python" - "$@" <<'PY'
import os
import sys

import psycopg2

mode = sys.argv[1]
conn = psycopg2.connect(os.environ["DATABASE_URL"])
try:
    with conn.cursor() as cur:
        if mode == "has-0009":
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM schema_migrations
                    WHERE version = '0009'
                )
                """
            )
            print("1" if cur.fetchone()[0] else "0")
        elif mode == "verify-0009":
            cur.execute("SELECT to_regclass('public.trade_outcome_job_runs')")
            table_name = cur.fetchone()[0]
            cur.execute(
                """
                SELECT name
                FROM schema_migrations
                WHERE version = '0009'
                """
            )
            row = cur.fetchone()
            if table_name != "trade_outcome_job_runs":
                raise SystemExit("trade_outcome_job_runs table missing")
            if row is None or row[0] != "trade_outcome_job_runs":
                raise SystemExit("schema_migrations 0009 marker missing")
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'trade_outcome_job_runs'
                ORDER BY ordinal_position
                """
            )
            columns = cur.fetchall()
            expected_columns = [
                ("job_name", "text", "NO"),
                ("status", "text", "NO"),
                ("started_at", "timestamp with time zone", "NO"),
                ("completed_at", "timestamp with time zone", "YES"),
                ("outcome_count", "bigint", "NO"),
                ("error", "text", "YES"),
            ]
            if columns != expected_columns:
                raise SystemExit(
                    f"trade_outcome_job_runs column drift: {columns}"
                )
            cur.execute(
                """
                SELECT conname, contype, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'public.trade_outcome_job_runs'::regclass
                """
            )
            constraints = {
                name: (constraint_type, definition.lower())
                for name, constraint_type, definition in cur.fetchall()
            }
            expected_constraint_types = {
                "trade_outcome_job_runs_pkey": "p",
                "ck_trade_outcome_job_runs_status": "c",
                "ck_trade_outcome_job_runs_outcome_count": "c",
                "ck_trade_outcome_job_runs_time_order": "c",
            }
            for name, constraint_type in expected_constraint_types.items():
                actual = constraints.get(name)
                if actual is None or actual[0] != constraint_type:
                    raise SystemExit(f"missing or invalid constraint: {name}")
            status_definition = constraints[
                "ck_trade_outcome_job_runs_status"
            ][1]
            for token in ("status", "'running'", "'succeeded'", "'failed'"):
                if token not in status_definition:
                    raise SystemExit(
                        "trade_outcome_job_runs status constraint drift"
                    )
            count_definition = constraints[
                "ck_trade_outcome_job_runs_outcome_count"
            ][1]
            if "outcome_count >= 0" not in count_definition:
                raise SystemExit(
                    "trade_outcome_job_runs outcome_count constraint drift"
                )
            time_definition = constraints[
                "ck_trade_outcome_job_runs_time_order"
            ][1]
            for token in (
                "completed_at is null",
                "completed_at >= started_at",
            ):
                if token not in time_definition:
                    raise SystemExit(
                        "trade_outcome_job_runs time constraint drift"
                    )
            print("migration 0009 verified")
        elif mode == "verify-watermark":
            required_after = sys.argv[2]
            cur.execute(
                """
                SELECT status, started_at, completed_at, outcome_count, error
                FROM trade_outcome_job_runs
                WHERE job_name = 'trade_outcomes'
                """
            )
            row = cur.fetchone()
            if row is None:
                raise SystemExit("trade_outcomes watermark missing")
            status, started_at, completed_at, outcome_count, error = row
            if status != "succeeded" or completed_at is None or error is not None:
                raise SystemExit(
                    f"trade_outcomes watermark invalid: "
                    f"status={status} completed_at={completed_at} "
                    f"outcome_count={outcome_count}"
                )
            cur.execute(
                "SELECT %s::timestamptz",
                (required_after,),
            )
            required_timestamp = cur.fetchone()[0]
            if started_at < required_timestamp or completed_at < required_timestamp:
                raise SystemExit(
                    f"trade_outcomes watermark did not advance: "
                    f"required_after={required_timestamp.isoformat()} "
                    f"started_at={started_at.isoformat()} "
                    f"completed_at={completed_at.isoformat()}"
                )
            print(
                f"trade_outcomes watermark succeeded "
                f"started_at={started_at.isoformat()} "
                f"completed_at={completed_at.isoformat()} "
                f"outcome_count={outcome_count}"
            )
        else:
            raise SystemExit(f"unknown database marker mode: {mode}")
finally:
    conn.close()
PY
}

halt_nodes_for_deploy() {
  "$T/.venv-cp/bin/python" - "$T/.env.v3" <<'PY'
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg2


def read_environment(path):
    values = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
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
database_url = values.get("DATABASE_URL", "")
risk_admin_token = values.get("RISK_ADMIN_TOKEN", "")
if not database_url:
    raise SystemExit("DATABASE_URL is missing")
if not risk_admin_token:
    raise SystemExit("RISK_ADMIN_TOKEN is missing")

conn = psycopg2.connect(database_url)
conn.autocommit = True
try:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT node_id
            FROM node_heartbeats
            WHERE last_seen_at >= now() - interval '5 minutes'
            ORDER BY node_id
            """
        )
        node_ids = [row[0] for row in cur.fetchall()]
    if len(node_ids) != 2:
        raise SystemExit(f"expected two fresh node heartbeats, found {node_ids}")

    request_id = (
        f"deploy-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        f"-{uuid4().hex[:8]}-halt"
    )
    body = json.dumps(
        {
            "type": "HALT",
            "reason": "2026-07-29 audited deployment maintenance window",
            "confirm": True,
            "request_id": request_id,
            "target_nodes": node_ids,
            "scope": {},
        }
    ).encode("utf-8")
    request = Request(
        "http://127.0.0.1:8080/v1/commands",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {risk_admin_token}",
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
    except (HTTPError, URLError) as exc:
        raise SystemExit(f"HALT command request failed: {exc}") from exc
    command_id = result.get("command_id")
    if not command_id:
        raise SystemExit(f"HALT command id missing: {result}")

    last_state = {}
    last_acks = []
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        states = {}
        for port in (8081, 8082):
            try:
                with urlopen(
                    f"http://127.0.0.1:{port}/ready",
                    timeout=5,
                ) as response:
                    payload = json.load(response)
                states[str(port)] = payload.get("trading_state")
            except (OSError, URLError, ValueError) as exc:
                states[str(port)] = str(exc)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT node_id, status
                FROM command_node_acks
                WHERE command_id = %s
                ORDER BY node_id
                """,
                (command_id,),
            )
            acks = cur.fetchall()
            cur.execute(
                "SELECT status FROM operator_commands WHERE command_id = %s",
                (command_id,),
            )
            command_row = cur.fetchone()
        command_status = command_row[0] if command_row is not None else ""
        if (
            set(states.values()) == {"HALTED"}
            and len(acks) == 2
            and all(status == "acked" for _, status in acks)
            and command_status == "completed"
        ):
            print(
                f"HALT acknowledged by {node_ids}; "
                f"command_id={command_id}"
            )
            raise SystemExit(0)
        last_state = states
        last_acks = acks
        time.sleep(2)
    raise SystemExit(
        f"HALT acceptance timed out: "
        f"states={last_state} acks={last_acks}"
    )
finally:
    conn.close()
PY
}

apply_migration_0009() {
  "$T/.venv-cp/bin/python" - \
    "$T/db/migrations/0009_trade_outcome_job_runs.up.sql" <<'PY'
from pathlib import Path
import os
import sys

import psycopg2

sql = Path(sys.argv[1]).read_text(encoding="utf-8")
conn = psycopg2.connect(os.environ["DATABASE_URL"])
try:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name
                FROM schema_migrations
                WHERE version = '0009'
                """
            )
            marker = cur.fetchone()
            cur.execute("SELECT to_regclass('public.trade_outcome_job_runs')")
            table_name = cur.fetchone()[0]
            if marker is not None:
                if marker[0] != "trade_outcome_job_runs":
                    raise SystemExit(f"unexpected 0009 migration name: {marker[0]}")
                if table_name != "trade_outcome_job_runs":
                    raise SystemExit("0009 is marked applied but its table is missing")
                print("migration 0009 already applied")
                raise SystemExit(0)
            if table_name is not None:
                raise SystemExit(
                    "trade_outcome_job_runs exists without schema_migrations 0009"
                )
            cur.execute(sql)
            cur.execute(
                """
                INSERT INTO schema_migrations (version, name)
                VALUES ('0009', 'trade_outcome_job_runs')
                """
            )
    print("migration 0009 applied")
finally:
    conn.close()
PY
}

verify_existing_node_runtime() {
  python3 - \
    "$NODE_A" \
    "$NODE_B" \
    "$CP" \
    "${NODE_PATCH_MOUNTS[@]}" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

node_names = sys.argv[1:3]
patch_dir = Path(sys.argv[3])
expected = {}
for mount_spec in sys.argv[4:]:
    filename, destination = mount_spec.split("=", 1)
    expected[str(patch_dir / filename)] = destination
inspected = json.loads(
    subprocess.check_output(["docker", "inspect", *node_names])
)
if len(inspected) != 2:
    raise SystemExit(f"expected two inspected nodes, found {len(inspected)}")
for item in inspected:
    name = item.get("Name", "").lstrip("/")
    environment = item.get("Config", {}).get("Env") or []
    states = [
        value.split("=", 1)[1]
        for value in environment
        if value.startswith("NAUTILUS_INITIAL_TRADING_STATE=")
    ]
    if states and states != ["HALTED"]:
        raise SystemExit(f"{name} has unsafe initial trading state: {states}")
    if not states:
        node_source = f"{patch_dir}/node.py"
        node_text = open(node_source, encoding="utf-8").read()
        safe_default = (
            'os.environ.get("NAUTILUS_INITIAL_TRADING_STATE", "HALTED")'
        )
        if safe_default not in node_text:
            raise SystemExit(
                f"{name} has no explicit HALTED env and node.py lacks safe default"
            )
    mounts = item.get("Mounts") or []
    for source, destination in expected.items():
        matches = [
            mount
            for mount in mounts
            if mount.get("Source") == source
            and mount.get("Destination") == destination
        ]
        if len(matches) != 1:
            raise SystemExit(
                f"{name} mount mismatch: {source} -> {destination}; "
                f"matches={len(matches)}"
            )
        if matches[0].get("RW"):
            raise SystemExit(
                f"{name} patch mount is writable: {source} -> {destination}"
            )
    print(f"{name} runtime config preserves HALTED restart and full patch mounts")
PY
}

verify_node_halted() {
  local port="$1"
  python3 - "$port" <<'PY'
import json
import sys
import time
from urllib.request import urlopen
from urllib.error import URLError

port = sys.argv[1]
last_error = ""
for _ in range(45):
    try:
        with urlopen(f"http://127.0.0.1:{port}/ready", timeout=5) as response:
            payload = json.load(response)
        if payload.get("ready") and payload.get("trading_state") == "HALTED":
            print(f"node {port} ready and HALTED")
            raise SystemExit(0)
        last_error = str(payload)
    except (OSError, URLError, ValueError) as exc:
        last_error = str(exc)
    time.sleep(2)
raise SystemExit(f"node {port} did not become ready and HALTED: {last_error}")
PY
}

wait_for_controlplane_http() {
  local url="http://127.0.0.1:8080/openapi.json"
  local max_attempts=30
  local retry_delay_seconds=2
  local attempt
  local curl_error

  for attempt in $(seq 1 "$max_attempts"); do
    curl_error=""
    if curl_error=$(
      curl \
        -fsS \
        --connect-timeout 1 \
        --max-time 5 \
        "$url" \
        --output /dev/null \
        2>&1
    ); then
      echo "Control plane HTTP ready: $url (attempt $attempt/$max_attempts)"
      return
    fi

    echo \
      "Waiting for control plane HTTP: attempt $attempt/$max_attempts failed: ${curl_error:-unknown curl error}" \
      >&2
    if [ "$attempt" -lt "$max_attempts" ]; then
      echo "Retrying control plane HTTP in ${retry_delay_seconds}s" >&2
      sleep "$retry_delay_seconds"
    fi
  done

  die \
    "control plane HTTP did not become ready after $max_attempts attempts: $url; last error: ${curl_error:-unknown curl error}"
}

[ "$(id -u)" -eq 0 ] || die "run as root with sudo"
require_command docker
require_command systemctl
require_command python3
require_command sha256sum
require_command flock
require_command curl
id balen >/dev/null 2>&1 || die "service user balen is missing"

exec 9>/var/lock/trader-v3-deploy-20260729.lock
flock -n 9 || die "another trader-v3 deployment is running"

required=(
  SHA256SUMS
  host/read_api.py
  host/order_lifecycle_monitor.py
  host/report_service.py
  host/exchange_state_recorder.py
  host/trade_outcomes.py
  db/0009_trade_outcome_job_runs.up.sql
  db/0009_trade_outcome_job_runs.down.sql
  systemd/trader-v3-trade-outcomes.service
  systemd/trader-v3-trade-outcomes.timer
  tools/hk-gen-recreate-patched.py
  tools/verify_hk_deployment.sh
  manifest.txt
  hk-rollback-20260729.sh
)
for mount_spec in "${NODE_PATCH_MOUNTS[@]}"; do
  required+=("container/${mount_spec%%=*}")
done
for relative_path in "${required[@]}"; do
  [ -f "$D/$relative_path" ] || die "missing deployment artifact: $D/$relative_path"
done
(cd "$D" && sha256sum -c SHA256SUMS)

AVAILABLE_MB=$(awk '/MemAvailable/{print int($2/1024)}' "$MEMINFO_PATH")
[ -n "$AVAILABLE_MB" ] || die "cannot read MemAvailable"
echo "MemAvailable=${AVAILABLE_MB}MB"
[ "$AVAILABLE_MB" -ge 1000 ] || die "less than 1000MB memory available"

for node in "$NODE_A" "$NODE_B"; do
  docker inspect "$node" >/dev/null
done
verify_existing_node_runtime
halt_nodes_for_deploy

[ ! -e "$BACKUP_ROOT" ] || die "backup path already exists: $BACKUP_ROOT"
mkdir -p "$BACKUP_ROOT"
chmod 0700 "$BACKUP_ROOT"
: >"$BACKUP_ROOT/index.tsv"
chmod 0600 "$BACKUP_ROOT/index.tsv"

targets=(
  "$T/services/control-plane/api/read_api.py"
  "$T/scripts/order_lifecycle_monitor.py"
  "$T/services/report/report_service.py"
  "$T/services/control-plane/tools/exchange_state_recorder.py"
  "$T/scripts/analysis/trade_outcomes.py"
  "$T/scripts/.order_lifecycle_state.json"
  "$T/db/migrations/0009_trade_outcome_job_runs.up.sql"
  "$T/db/migrations/0009_trade_outcome_job_runs.down.sql"
  "$T/gen_recreate_patched.py"
  "$T/scripts/verify_hk_deployment.sh"
  "$T/recreate-$NODE_A.sh"
  "$T/recreate-$NODE_B.sh"
  "/etc/systemd/system/trader-v3-trade-outcomes.service"
  "/etc/systemd/system/trader-v3-trade-outcomes.timer"
  "/etc/trader-v3/trade-outcomes.env"
  "/etc/cron.d/trader-v3-trade-outcomes"
  "/var/lib/systemd/timers/stamp-trader-v3-trade-outcomes.timer"
)
for mount_spec in "${NODE_PATCH_MOUNTS[@]}"; do
  targets+=("$CP/${mount_spec%%=*}")
done
for target in "${targets[@]}"; do
  backup_path "$target"
done
docker inspect "$NODE_A" >"$BACKUP_ROOT/$NODE_A.inspect.json"
docker inspect "$NODE_B" >"$BACKUP_ROOT/$NODE_B.inspect.json"
chmod 0600 "$BACKUP_ROOT/"*.inspect.json
systemctl is-enabled trader-v3-trade-outcomes.timer \
  >"$BACKUP_ROOT/timer-enabled-state" 2>&1 || true
systemctl is-active trader-v3-trade-outcomes.timer \
  >"$BACKUP_ROOT/timer-active-state" 2>&1 || true

MUTATION_STARTED=1
systemctl stop trader-v3-trade-outcomes.timer >/dev/null 2>&1 || true
systemctl stop trader-v3-trade-outcomes.service >/dev/null 2>&1 || true
rm -f /etc/cron.d/trader-v3-trade-outcomes
if systemctl is-active --quiet trader-v3-trade-outcomes.timer; then
  die "trade outcomes timer is still active"
fi
if systemctl is-active --quiet trader-v3-trade-outcomes.service; then
  die "trade outcomes service is still active"
fi
OUTCOME_LOCK_CLEAR=0
for _ in $(seq 1 60); do
  if flock -n /var/lock/trader-v3-trade-outcomes.lock -c true; then
    OUTCOME_LOCK_CLEAR=1
    break
  fi
  sleep 2
done
[ "$OUTCOME_LOCK_CLEAR" -eq 1 ] \
  || die "trade outcomes lock remains held after scheduler isolation"

load_database_url
database_marker has-0009 >"$BACKUP_ROOT/preexisting-0009"

mapfile -t DB_CONTAINERS < <(
  docker ps --filter publish=5432 --format '{{.Names}}'
)
[ "${#DB_CONTAINERS[@]}" -eq 1 ] || die "expected one container publishing port 5432"
DB_CONTAINER="${DB_CONTAINERS[0]}"
docker exec "$DB_CONTAINER" pg_dump --version >/dev/null
printf '%s\n' "$DATABASE_URL" | docker exec \
  -i \
  "$DB_CONTAINER" \
  sh -c \
  'IFS= read -r DATABASE_URL; export DATABASE_URL; exec pg_dump -Fc --dbname="$DATABASE_URL"' \
  >"$BACKUP_ROOT/trader.dump"
chmod 0600 "$BACKUP_ROOT/trader.dump"
[ -s "$BACKUP_ROOT/trader.dump" ] || die "database backup is empty"
printf '%s\n' "$DATABASE_URL" | docker exec \
  -i \
  "$DB_CONTAINER" \
  sh -c \
  'IFS= read -r DATABASE_URL; export DATABASE_URL; exec pg_dump -Fc --dbname="$DATABASE_URL" --table=public.trade_outcomes --table=public.schema_migrations' \
  >"$BACKUP_ROOT/outcomes-and-migrations.dump"
chmod 0600 "$BACKUP_ROOT/outcomes-and-migrations.dump"
[ -s "$BACKUP_ROOT/outcomes-and-migrations.dump" ] \
  || die "outcomes table backup is empty"
printf '%s\n' "$DATABASE_URL" | docker exec \
  -i \
  "$DB_CONTAINER" \
  sh -c \
  'IFS= read -r DATABASE_URL; export DATABASE_URL; exec pg_dump --data-only --column-inserts --dbname="$DATABASE_URL" --table=public.trade_outcomes' \
  >"$BACKUP_ROOT/trade-outcomes-data.sql"
[ -s "$BACKUP_ROOT/trade-outcomes-data.sql" ] \
  || die "trade_outcomes data backup is empty"
if [ "$(cat "$BACKUP_ROOT/preexisting-0009")" = "1" ]; then
  printf '%s\n' "$DATABASE_URL" | docker exec \
    -i \
    "$DB_CONTAINER" \
    sh -c \
    'IFS= read -r DATABASE_URL; export DATABASE_URL; exec pg_dump --data-only --column-inserts --dbname="$DATABASE_URL" --table=public.trade_outcome_job_runs' \
    >"$BACKUP_ROOT/trade-outcome-job-runs-data.sql"
  [ -s "$BACKUP_ROOT/trade-outcome-job-runs-data.sql" ] \
    || die "trade_outcome_job_runs data backup is empty"
fi
chmod 0600 "$BACKUP_ROOT/"*-data.sql
docker exec -i "$DB_CONTAINER" pg_restore --list \
  <"$BACKUP_ROOT/trader.dump" \
  >"$BACKUP_ROOT/trader.dump.list"
docker exec -i "$DB_CONTAINER" pg_restore --list \
  <"$BACKUP_ROOT/outcomes-and-migrations.dump" \
  >"$BACKUP_ROOT/outcomes-and-migrations.dump.list"
grep -Eq 'TABLE DATA public trade_outcomes ' "$BACKUP_ROOT/trader.dump.list" \
  || die "full database backup lacks trade_outcomes"
grep -Eq 'TABLE DATA public schema_migrations ' "$BACKUP_ROOT/trader.dump.list" \
  || die "full database backup lacks schema_migrations"
grep -Eq \
  'TABLE DATA public trade_outcomes ' \
  "$BACKUP_ROOT/outcomes-and-migrations.dump.list" \
  || die "table backup lacks trade_outcomes"
grep -Eq \
  'TABLE DATA public schema_migrations ' \
  "$BACKUP_ROOT/outcomes-and-migrations.dump.list" \
  || die "table backup lacks schema_migrations"
chmod 0600 "$BACKUP_ROOT/"*.dump.list
(
  cd "$BACKUP_ROOT"
  sha256sum \
    trader.dump \
    outcomes-and-migrations.dump \
    trade-outcomes-data.sql \
    >SHA256SUMS
  if [ -f trade-outcome-job-runs-data.sql ]; then
    sha256sum trade-outcome-job-runs-data.sql >>SHA256SUMS
  fi
  sha256sum -c SHA256SUMS
)
(
  cd "$BACKUP_ROOT"
  find . -type f ! -name BACKUP_SHA256SUMS -print \
    | LC_ALL=C sort \
    | while IFS= read -r file; do
        sha256sum "$file"
      done \
    >BACKUP_SHA256SUMS
  sha256sum -c BACKUP_SHA256SUMS
)
echo "Backup complete: $BACKUP_ROOT"

docker stop -t 30 "$NODE_A" "$NODE_B"

install -m 0644 "$D/host/read_api.py" \
  "$T/services/control-plane/api/read_api.py"
install -m 0644 "$D/host/order_lifecycle_monitor.py" \
  "$T/scripts/order_lifecycle_monitor.py"
install -m 0644 "$D/host/report_service.py" \
  "$T/services/report/report_service.py"
install -m 0644 "$D/host/exchange_state_recorder.py" \
  "$T/services/control-plane/tools/exchange_state_recorder.py"
install -m 0644 "$D/host/trade_outcomes.py" \
  "$T/scripts/analysis/trade_outcomes.py"
install -m 0644 "$D/db/0009_trade_outcome_job_runs.up.sql" \
  "$T/db/migrations/0009_trade_outcome_job_runs.up.sql"
install -m 0644 "$D/db/0009_trade_outcome_job_runs.down.sql" \
  "$T/db/migrations/0009_trade_outcome_job_runs.down.sql"
for mount_spec in "${NODE_PATCH_MOUNTS[@]}"; do
  patch_filename="${mount_spec%%=*}"
  install -m 0644 \
    "$D/container/$patch_filename" \
    "$CP/$patch_filename"
done
install -m 0755 "$D/tools/hk-gen-recreate-patched.py" \
  "$T/gen_recreate_patched.py"
install -m 0755 "$D/tools/verify_hk_deployment.sh" \
  "$T/scripts/verify_hk_deployment.sh"
install -m 0644 "$D/systemd/trader-v3-trade-outcomes.service" \
  /etc/systemd/system/trader-v3-trade-outcomes.service
install -m 0644 "$D/systemd/trader-v3-trade-outcomes.timer" \
  /etc/systemd/system/trader-v3-trade-outcomes.timer

install -d -o root -g root -m 0750 /etc/trader-v3
ENV_TMP=$(mktemp /etc/trader-v3/.trade-outcomes.env.XXXXXX)
python3 - >"$ENV_TMP" <<'PY'
import os

database_url = os.environ["DATABASE_URL"]
if "\n" in database_url or "\r" in database_url:
    raise SystemExit("DATABASE_URL contains a newline")
escaped = database_url.replace("\\", "\\\\").replace('"', '\\"')
print(f'DATABASE_URL="{escaped}"')
PY
chown root:root "$ENV_TMP"
chmod 0600 "$ENV_TMP"
mv "$ENV_TMP" /etc/trader-v3/trade-outcomes.env

install -d -o balen -g balen -m 0750 /var/log/trader-v3
install -d -o balen -g balen -m 0750 /var/tmp/hermes-zone-klines
touch /var/log/trader-v3/trade-outcomes.log
touch /var/log/trader-v3/trade-outcomes-latest.json
touch /var/lock/trader-v3-trade-outcomes.lock
chown balen:balen \
  /var/log/trader-v3/trade-outcomes.log \
  /var/log/trader-v3/trade-outcomes-latest.json \
  /var/lock/trader-v3-trade-outcomes.lock
chmod 0640 \
  /var/log/trader-v3/trade-outcomes.log \
  /var/log/trader-v3/trade-outcomes-latest.json \
  /var/lock/trader-v3-trade-outcomes.lock

apply_migration_0009
database_marker verify-0009

systemctl daemon-reload
systemctl restart trader-v3-controlplane
systemctl restart trader-v3-report
systemctl restart trader-v3-exchange-state
systemctl restart trader-v3-lifecycle-monitor
for unit in \
  trader-v3-controlplane \
  trader-v3-report \
  trader-v3-exchange-state \
  trader-v3-lifecycle-monitor; do
  systemctl is-active --quiet "$unit" || die "$unit failed to start"
done
wait_for_controlplane_http

docker start "$NODE_A"
verify_node_halted 8081
docker start "$NODE_B"
verify_node_halted 8082

python3 - \
  "$D/manifest.txt" \
  "$CP" \
  "${NODE_PATCH_MOUNTS[@]}" <<'PY'
from pathlib import Path
import hashlib
import os
import sys

manifest_path = Path(sys.argv[1])
patch_dir = Path(sys.argv[2])
required_mounts = set()
for mount_spec in sys.argv[3:]:
    filename, destination = mount_spec.split("=", 1)
    required_mounts.add((str(patch_dir / filename), destination))
node_pids = []
for pid_text in os.listdir("/proc"):
    if not pid_text.isdigit():
        continue
    try:
        command = Path(f"/proc/{pid_text}/cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        continue
    if b"python -m app.run_node --config /cfg.json" in command:
        node_pids.append(pid_text)
if len(node_pids) != 2:
    raise SystemExit(f"expected two node processes, found {node_pids}")

manifest_entries = []
seen_mounts = set()
for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    fields = line.split()
    if len(fields) not in {2, 3}:
        raise SystemExit(f"invalid manifest entry: {raw_line}")
    expected_hash, source = fields[:2]
    destination = ""
    if len(fields) == 3:
        destination = fields[2]
        seen_mounts.add((source, destination))
    manifest_entries.append((expected_hash, source, destination))

missing_mounts = required_mounts - seen_mounts
if missing_mounts:
    raise SystemExit(f"deployment manifest lacks required mounts: {missing_mounts}")

for expected_hash, source, destination in manifest_entries:
    digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    if digest != expected_hash:
        raise SystemExit(f"hash mismatch: {source}")
    if not destination:
        print(f"hash verified: {source}")
        continue
    for pid in node_pids:
        pairs = []
        mountinfo = Path(f"/proc/{pid}/mountinfo").read_text(
            encoding="utf-8",
            errors="replace",
        )
        for mount_line in mountinfo.splitlines():
            left, _, right = mount_line.partition(" - ")
            if not right:
                continue
            left_fields = left.split()
            right_fields = right.split()
            if len(left_fields) < 5 or len(right_fields) < 2:
                continue
            pairs.append((left_fields[3], left_fields[4]))
        if pairs.count((source, destination)) != 1:
            raise SystemExit(
                f"mount mismatch pid={pid}: {source} -> {destination}"
            )
    print(f"mount verified on both nodes: {source} -> {destination}")
PY

OUTCOME_RUN_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
systemctl start trader-v3-trade-outcomes.service
systemctl is-failed --quiet trader-v3-trade-outcomes.service \
  && die "trade outcomes service failed"
database_marker verify-watermark "$OUTCOME_RUN_STARTED_AT"
[ -s /var/log/trader-v3/trade-outcomes-latest.json ] \
  || die "trade-outcomes-latest.json missing"
install -d -m 0755 /var/lib/systemd/timers
touch /var/lib/systemd/timers/stamp-trader-v3-trade-outcomes.timer
chown root:root /var/lib/systemd/timers/stamp-trader-v3-trade-outcomes.timer
chmod 0644 /var/lib/systemd/timers/stamp-trader-v3-trade-outcomes.timer
systemctl enable trader-v3-trade-outcomes.timer
systemctl start trader-v3-trade-outcomes.timer
systemctl is-active --quiet trader-v3-trade-outcomes.timer \
  || die "trade outcomes timer is not active"

python3 - <<'PY'
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8090/healthz", timeout=15) as response:
    payload = json.load(response)
dependencies = payload.get("dependencies") or {}
database = dependencies.get("database") or {}
outcomes = dependencies.get("trade_outcomes") or {}
if database.get("status") != "ok":
    raise SystemExit(f"report database dependency unhealthy: {payload}")
if outcomes.get("status") != "ok":
    raise SystemExit(f"report outcome dependency unhealthy: {payload}")
print(
    "report health verified: "
    f"status={payload.get('status')} "
    f"outcomes={outcomes.get('completed_at')}"
)
PY

(
  cd "$T/services/report"
  "$T/.venv-report/bin/python" - <<'PY'
from datetime import datetime, timezone

import report_service

report_date = datetime.now(timezone.utc).date().isoformat()
for report_type in ("daily", "weekly"):
    dependencies = report_service.empty_dependency_status()
    data = report_service.fetch_report_data(
        report_type,
        report_date,
        dependency_status=dependencies,
    )
    print(
        f"{report_type} report data verified: "
        f"trades={data['kpis']['trade_count']} "
        f"missing={len(data['missing_data'])}"
    )
PY
)

verify_node_halted 8081
verify_node_halted 8082
echo "DEPLOY SUCCEEDED"
echo "Backup: $BACKUP_ROOT"
echo "Trading remains HALTED pending protection E2E acceptance"
