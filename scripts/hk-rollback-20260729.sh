#!/usr/bin/env bash
set -Eeuo pipefail

D="${DEPLOY_DIR:-/home/balen/deploy-20260729}"
T="${TRADER_ROOT:-/srv/trader-v3}"
NODE_A="${NODE_A:-trader-v3-node-a}"
NODE_B="${NODE_B:-trader-v3-node-b}"
BACKUP_ROOT="${1:-}"
MUTATION_STARTED=0

die() {
  echo "FATAL: $*" >&2
  if [ "$MUTATION_STARTED" -eq 1 ]; then
    docker stop -t 20 "$NODE_A" "$NODE_B" >/dev/null 2>&1 || true
    echo "Rollback failed closed: nodes are stopped" >&2
  fi
  exit 1
}

on_error() {
  local status=$?
  if [ "$MUTATION_STARTED" -eq 1 ]; then
    docker stop -t 20 "$NODE_A" "$NODE_B" >/dev/null 2>&1 || true
  fi
  echo "ROLLBACK FAILED: nodes are stopped" >&2
  exit "$status"
}
trap on_error ERR INT TERM

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
  [ -n "$DATABASE_URL" ] || die "DATABASE_URL missing"
  export DATABASE_URL
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
raise SystemExit(f"node {port} rollback state invalid: {last_error}")
PY
}

[ "$(id -u)" -eq 0 ] || die "run as root with sudo"
[ -n "$BACKUP_ROOT" ] || die "usage: $0 BACKUP_ROOT"
[ -r "$BACKUP_ROOT/index.tsv" ] || die "backup index missing"

exec 9>/var/lock/trader-v3-deploy-20260729.lock
flock -n 9 || die "another trader-v3 deployment is running"

docker inspect "$NODE_A" "$NODE_B" >/dev/null
python3 - "$NODE_A" "$NODE_B" "$T/container-patches/node.py" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

inspected = json.loads(
    subprocess.check_output(["docker", "inspect", *sys.argv[1:3]])
)
node_source = sys.argv[3]
node_text = Path(node_source).read_text(encoding="utf-8")
safe_default = (
    'os.environ.get("NAUTILUS_INITIAL_TRADING_STATE", "HALTED")'
)
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
    if not states and safe_default not in node_text:
        raise SystemExit(
            f"{name} has no explicit HALTED env and node.py lacks safe default"
        )
    print(f"{name} restart state is HALTED or defaults to HALTED")
PY

MUTATION_STARTED=1
systemctl stop trader-v3-trade-outcomes.timer >/dev/null 2>&1 || true
systemctl stop trader-v3-trade-outcomes.service >/dev/null 2>&1 || true
systemctl disable trader-v3-trade-outcomes.timer >/dev/null 2>&1 || true
docker stop -t 30 "$NODE_A" "$NODE_B"

load_database_url
mapfile -t DB_CONTAINERS < <(
  docker ps --filter publish=5432 --format '{{.Names}}'
)
[ "${#DB_CONTAINERS[@]}" -eq 1 ] || die "expected one container publishing port 5432"
DB_CONTAINER="${DB_CONTAINERS[0]}"

if [ "$(cat "$BACKUP_ROOT/preexisting-0009")" = "0" ]; then
  DOWN_MIGRATION="$T/db/migrations/0009_trade_outcome_job_runs.down.sql"
  if [ ! -r "$DOWN_MIGRATION" ]; then
    DOWN_MIGRATION="$D/db/0009_trade_outcome_job_runs.down.sql"
  fi
  "$T/.venv-cp/bin/python" - "$DOWN_MIGRATION" <<'PY'
from pathlib import Path
import os
import sys

import psycopg2

sql = Path(sys.argv[1]).read_text(encoding="utf-8")
conn = psycopg2.connect(os.environ["DATABASE_URL"])
try:
    with conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "DELETE FROM schema_migrations WHERE version = '0009'"
            )
finally:
    conn.close()
print("migration 0009 rolled back")
PY
fi

{
  printf '%s\n' "$DATABASE_URL"
  printf '%s\n' 'BEGIN;'
  printf '%s\n' 'TRUNCATE TABLE public.trade_outcomes;'
  cat "$BACKUP_ROOT/trade-outcomes-data.sql"
  printf '%s\n' 'COMMIT;'
} | docker exec \
  -i \
  "$DB_CONTAINER" \
  sh -c \
  'IFS= read -r DATABASE_URL; export DATABASE_URL; exec psql --dbname="$DATABASE_URL" -v ON_ERROR_STOP=1'

if [ "$(cat "$BACKUP_ROOT/preexisting-0009")" = "1" ]; then
  {
    printf '%s\n' "$DATABASE_URL"
    printf '%s\n' 'BEGIN;'
    printf '%s\n' 'TRUNCATE TABLE public.trade_outcome_job_runs;'
    cat "$BACKUP_ROOT/trade-outcome-job-runs-data.sql"
    printf '%s\n' 'COMMIT;'
  } | docker exec \
    -i \
    "$DB_CONTAINER" \
    sh -c \
    'IFS= read -r DATABASE_URL; export DATABASE_URL; exec psql --dbname="$DATABASE_URL" -v ON_ERROR_STOP=1'
fi

while IFS=$'\t' read -r state path; do
  [ -n "$path" ] || continue
  if [ "$state" = "present" ]; then
    source_path="$BACKUP_ROOT/files$path"
    [ -e "$source_path" ] || die "backup file missing: $source_path"
    mkdir -p "$(dirname "$path")"
    rm -f "$path"
    cp -a "$source_path" "$path"
    continue
  fi
  if [ "$state" = "absent" ]; then
    rm -f "$path"
    continue
  fi
  die "unknown backup state: $state"
done <"$BACKUP_ROOT/index.tsv"

systemctl daemon-reload
systemctl restart trader-v3-controlplane
systemctl restart trader-v3-report
systemctl restart trader-v3-exchange-state
systemctl restart trader-v3-lifecycle-monitor

TIMER_ENABLED_STATE=$(cat "$BACKUP_ROOT/timer-enabled-state")
TIMER_ACTIVE_STATE=$(cat "$BACKUP_ROOT/timer-active-state")
if [ "$TIMER_ENABLED_STATE" = "enabled" ] \
  || [ "$TIMER_ENABLED_STATE" = "enabled-runtime" ]; then
  systemctl enable trader-v3-trade-outcomes.timer
fi
if [ "$TIMER_ACTIVE_STATE" = "active" ] \
  || [ "$TIMER_ACTIVE_STATE" = "activating" ]; then
  systemctl start trader-v3-trade-outcomes.timer
fi

docker start "$NODE_A"
verify_node_halted 8081
docker start "$NODE_B"
verify_node_halted 8082

echo "ROLLBACK SUCCEEDED"
echo "Database backups remain at $BACKUP_ROOT"
echo "Trading remains HALTED"
