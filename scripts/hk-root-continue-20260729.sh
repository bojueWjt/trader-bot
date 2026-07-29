#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(
  CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd
)
D="${1:-${DEPLOY_DIR:-$SCRIPT_DIR}}"
BACKUP_ROOT="/srv/trader-v3/backups/deploy-20260729T084509Z"
EXPECTED_STAGING_MANIFEST_SHA256="${2:-${EXPECTED_STAGING_MANIFEST_SHA256:-}}"
T="${TRADER_ROOT:-/srv/trader-v3}"
CP="$T/container-patches"
NODE_A="${NODE_A:-trader-v3-node-a}"
NODE_B="${NODE_B:-trader-v3-node-b}"
CLEANUP_CONFIRMED=0

fail_closed_cleanup() {
  trap - ERR
  set +e
  local cleanup_failed=0
  local lock_clear=0
  local node_running

  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop trader-v3-trade-outcomes.timer >/dev/null 2>&1 \
      || cleanup_failed=1
    systemctl disable trader-v3-trade-outcomes.timer >/dev/null 2>&1 \
      || cleanup_failed=1
    systemctl stop trader-v3-trade-outcomes.service >/dev/null 2>&1 \
      || cleanup_failed=1
  else
    cleanup_failed=1
  fi

  if command -v flock >/dev/null 2>&1; then
    for _ in {1..30}; do
      if flock -n /var/lock/trader-v3-trade-outcomes.lock -c true; then
        lock_clear=1
        break
      fi
      sleep 1
    done
  fi
  if [ "$lock_clear" -ne 1 ]; then
    echo "FAIL-CLOSED WARNING: trade outcomes lock remains held" >&2
    cleanup_failed=1
  fi

  if command -v docker >/dev/null 2>&1; then
    docker stop -t 20 "$NODE_A" "$NODE_B" >/dev/null 2>&1
    for node in "$NODE_A" "$NODE_B"; do
      node_running=$(
        docker inspect --format '{{.State.Running}}' "$node" 2>/dev/null
      )
      if [ "$node_running" != "false" ]; then
        echo "FAIL-CLOSED WARNING: $node is not confirmed stopped" >&2
        cleanup_failed=1
      fi
    done
  else
    cleanup_failed=1
  fi

  if [ "$cleanup_failed" -eq 0 ]; then
    CLEANUP_CONFIRMED=1
    echo "Fail-closed cleanup verified: outcomes stopped and nodes stopped" >&2
    return 0
  fi
  echo "FAIL-CLOSED CLEANUP INCOMPLETE: manual verification required" >&2
  return 1
}

report_fail_closed_state() {
  if [ "$CLEANUP_CONFIRMED" -eq 1 ]; then
    echo "Trading remains fail-closed" >&2
    return
  fi
  echo "Fail-closed state requires immediate manual verification" >&2
}

die() {
  echo "FATAL: $*" >&2
  fail_closed_cleanup
  report_fail_closed_state
  echo "Rollback: sudo bash $D/hk-rollback-20260729.sh $BACKUP_ROOT" >&2
  exit 1
}

on_error() {
  local status=$?
  trap - ERR
  fail_closed_cleanup
  echo "CONTINUATION FAILED" >&2
  report_fail_closed_state
  echo "Rollback: sudo bash $D/hk-rollback-20260729.sh $BACKUP_ROOT" >&2
  exit "$status"
}

on_signal() {
  local status="$1"
  local signal_name="$2"
  trap - ERR
  fail_closed_cleanup
  echo "CONTINUATION INTERRUPTED by $signal_name" >&2
  report_fail_closed_state
  echo "Rollback: sudo bash $D/hk-rollback-20260729.sh $BACKUP_ROOT" >&2
  exit "$status"
}

trap on_error ERR
trap 'on_signal 129 HUP' HUP
trap 'on_signal 130 INT' INT
trap 'on_signal 143 TERM' TERM

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command missing: $1"
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
        if mode == "capture-time":
            cur.execute("SELECT clock_timestamp()")
            print(cur.fetchone()[0].isoformat())
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
            cur.execute("SELECT %s::timestamptz", (required_after,))
            required_timestamp = cur.fetchone()[0]
            if started_at <= required_timestamp:
                raise SystemExit(
                    f"trade_outcomes start watermark did not advance: "
                    f"required_after={required_timestamp.isoformat()} "
                    f"started_at={started_at.isoformat()}"
                )
            if completed_at <= required_timestamp:
                raise SystemExit(
                    f"trade_outcomes completion watermark did not advance: "
                    f"required_after={required_timestamp.isoformat()} "
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

verify_backup_inventory() {
  python3 - "$BACKUP_ROOT" <<'PY'
from pathlib import Path
import sys

backup_root = Path(sys.argv[1])
required = [
    "index.tsv",
    "preexisting-0009",
    "timer-enabled-state",
    "timer-active-state",
    "trader.dump",
    "trader.dump.list",
    "outcomes-and-migrations.dump",
    "outcomes-and-migrations.dump.list",
    "trade-outcomes-data.sql",
]
for relative_path in required:
    path = backup_root / relative_path
    if not path.is_file():
        raise SystemExit(f"required backup input missing: {path}")
    if path.stat().st_size == 0:
        raise SystemExit(f"required backup input is empty: {path}")

preexisting = (backup_root / "preexisting-0009").read_text(
    encoding="utf-8"
).strip()
if preexisting not in {"0", "1"}:
    raise SystemExit(f"invalid preexisting-0009 marker: {preexisting}")
if preexisting == "1":
    job_data = backup_root / "trade-outcome-job-runs-data.sql"
    if not job_data.is_file() or job_data.stat().st_size == 0:
        raise SystemExit(f"required backup input missing or empty: {job_data}")

seen = set()
for raw_line in (backup_root / "index.tsv").read_text(
    encoding="utf-8"
).splitlines():
    fields = raw_line.split("\t", 1)
    if len(fields) != 2:
        raise SystemExit(f"invalid backup index line: {raw_line!r}")
    state, path_text = fields
    if state not in {"present", "absent"}:
        raise SystemExit(f"invalid backup state: {state}")
    if not path_text.startswith("/"):
        raise SystemExit(f"backup path is not absolute: {path_text}")
    if path_text in seen:
        raise SystemExit(f"duplicate backup path: {path_text}")
    seen.add(path_text)
    if state == "present":
        backup_path = backup_root / "files" / path_text.lstrip("/")
        if not backup_path.exists():
            raise SystemExit(f"indexed backup file missing: {backup_path}")

full_list = (backup_root / "trader.dump.list").read_text(
    encoding="utf-8",
    errors="replace",
)
outcome_list = (backup_root / "outcomes-and-migrations.dump.list").read_text(
    encoding="utf-8",
    errors="replace",
)
for table_name in ("trade_outcomes", "schema_migrations"):
    marker = f"TABLE DATA public {table_name} "
    if marker not in full_list:
        raise SystemExit(f"full database backup lacks {table_name}")
    if marker not in outcome_list:
        raise SystemExit(f"outcomes backup lacks {table_name}")
print("backup inventory verified")
PY
}

verify_target_hashes() {
  python3 - "$D" "$T" <<'PY'
from pathlib import Path
import hashlib
import sys

deploy_dir = Path(sys.argv[1])
trader_root = Path(sys.argv[2])
target_pairs = [
    ("host/read_api.py", trader_root / "services/control-plane/api/read_api.py"),
    ("host/order_lifecycle_monitor.py", trader_root / "scripts/order_lifecycle_monitor.py"),
    ("host/report_service.py", trader_root / "services/report/report_service.py"),
    (
        "host/exchange_state_recorder.py",
        trader_root / "services/control-plane/tools/exchange_state_recorder.py",
    ),
    ("host/trade_outcomes.py", trader_root / "scripts/analysis/trade_outcomes.py"),
    (
        "db/0009_trade_outcome_job_runs.up.sql",
        trader_root / "db/migrations/0009_trade_outcome_job_runs.up.sql",
    ),
    (
        "db/0009_trade_outcome_job_runs.down.sql",
        trader_root / "db/migrations/0009_trade_outcome_job_runs.down.sql",
    ),
    ("container/event_mapper.py", trader_root / "container-patches/event_mapper.py"),
    (
        "container/intent_execution_strategy.py",
        trader_root / "container-patches/intent_execution_strategy.py",
    ),
    (
        "tools/verify_hk_deployment.sh",
        trader_root / "scripts/verify_hk_deployment.sh",
    ),
    (
        "systemd/trader-v3-trade-outcomes.service",
        Path("/etc/systemd/system/trader-v3-trade-outcomes.service"),
    ),
    (
        "systemd/trader-v3-trade-outcomes.timer",
        Path("/etc/systemd/system/trader-v3-trade-outcomes.timer"),
    ),
]

for relative_source, target in target_pairs:
    source = deploy_dir / relative_source
    if not source.is_file():
        raise SystemExit(f"staging source missing: {source}")
    if not target.is_file():
        raise SystemExit(f"deployed target missing: {target}")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    target_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    if source_hash != target_hash:
        raise SystemExit(
            f"deployed target hash mismatch: {target}; "
            f"expected={source_hash} actual={target_hash}"
        )
    print(f"target hash verified: {target}")
PY
}

verify_existing_node_runtime() {
  python3 - "$NODE_A" "$NODE_B" "$CP" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

node_names = sys.argv[1:3]
patch_dir = sys.argv[3]
expected = {
    f"{patch_dir}/event_mapper.py": "/app/projection/event_mapper.py",
    f"{patch_dir}/intent_execution_strategy.py": (
        "/app/strategy/intent_execution_strategy.py"
    ),
    f"{patch_dir}/node.py": "/app/app/node.py",
}
inspected = json.loads(
    subprocess.check_output(["docker", "inspect", *node_names])
)
if len(inspected) != 2:
    raise SystemExit(f"expected two inspected nodes, found {len(inspected)}")
for item in inspected:
    name = item.get("Name", "").lstrip("/")
    state = item.get("State") or {}
    if state.get("Running"):
        raise SystemExit(f"{name} is running before continuation")
    if state.get("Status") != "exited":
        raise SystemExit(
            f"{name} is not in the required stopped state: {state.get('Status')}"
        )
    environment = (item.get("Config") or {}).get("Env") or []
    states = [
        value.split("=", 1)[1]
        for value in environment
        if value.startswith("NAUTILUS_INITIAL_TRADING_STATE=")
    ]
    if states and states != ["HALTED"]:
        raise SystemExit(f"{name} has unsafe initial trading state: {states}")
    if not states:
        node_source = Path(patch_dir) / "node.py"
        node_text = node_source.read_text(encoding="utf-8")
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
    print(f"{name} is stopped with HALTED restart and exact read-only mounts")
PY
}

verify_node_halted() {
  local port="$1"
  python3 - "$port" <<'PY'
import json
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

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

verify_runtime_manifest() {
  python3 - "$D/manifest.txt" "$NODE_A" "$NODE_B" "$CP" <<'PY'
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys

manifest_path = Path(sys.argv[1])
node_names = sys.argv[2:4]
patch_dir = sys.argv[4]
required_mounts = {
    (
        f"{patch_dir}/event_mapper.py",
        "/app/projection/event_mapper.py",
    ),
    (
        f"{patch_dir}/intent_execution_strategy.py",
        "/app/strategy/intent_execution_strategy.py",
    ),
}
inspected = json.loads(
    subprocess.check_output(["docker", "inspect", *node_names])
)
node_pids = []
for item in inspected:
    name = item.get("Name", "").lstrip("/")
    state = item.get("State") or {}
    if not state.get("Running"):
        raise SystemExit(f"{name} is not running during manifest verification")
    pid = state.get("Pid")
    if not isinstance(pid, int) or pid <= 0:
        raise SystemExit(f"{name} has invalid runtime pid: {pid}")
    node_pids.append((name, str(pid)))
if len(node_pids) != 2:
    raise SystemExit(f"expected two node pids, found {node_pids}")

manifest_entries = []
seen_sources = set()
seen_mounts = set()
for line_number, raw_line in enumerate(
    manifest_path.read_text(encoding="utf-8").splitlines(),
    start=1,
):
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    fields = line.split()
    if len(fields) not in {2, 3}:
        raise SystemExit(
            f"invalid manifest line {line_number}: expected 2 or 3 fields"
        )
    expected_hash, source = fields[:2]
    if re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
        raise SystemExit(f"invalid manifest hash on line {line_number}")
    if not source.startswith("/"):
        raise SystemExit(f"manifest source is not absolute on line {line_number}")
    if source in seen_sources:
        raise SystemExit(f"duplicate manifest source: {source}")
    seen_sources.add(source)
    destination = ""
    if len(fields) == 3:
        destination = fields[2]
        if not destination.startswith("/"):
            raise SystemExit(
                f"manifest destination is not absolute on line {line_number}"
            )
        mount_pair = (source, destination)
        if mount_pair in seen_mounts:
            raise SystemExit(
                f"duplicate manifest mount: {source} -> {destination}"
            )
        seen_mounts.add(mount_pair)
    manifest_entries.append((expected_hash, source, destination))

if not manifest_entries:
    raise SystemExit("deployment manifest has no entries")
missing_mounts = required_mounts - seen_mounts
if missing_mounts:
    raise SystemExit(f"deployment manifest lacks required mounts: {missing_mounts}")

for expected_hash, source, destination in manifest_entries:
    source_path = Path(source)
    if not source_path.is_file():
        raise SystemExit(f"manifest source missing: {source}")
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if digest != expected_hash:
        raise SystemExit(
            f"manifest hash mismatch: {source}; "
            f"expected={expected_hash} actual={digest}"
        )
    if not destination:
        print(f"manifest hash verified: {source}")
        continue
    for name, pid in node_pids:
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
                f"mount mismatch node={name} pid={pid}: "
                f"{source} -> {destination}"
            )
    print(f"mount verified on both nodes: {source} -> {destination}")
PY
}

wait_for_controlplane_http() {
  local url="http://127.0.0.1:8080/openapi.json"
  local attempt
  local curl_error

  for attempt in $(seq 1 10); do
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
      echo "control plane HTTP verified: $url"
      return
    fi
    if [ "$attempt" -lt 10 ]; then
      sleep 2
    fi
  done
  die \
    "control plane HTTP unavailable after 10 attempts: ${curl_error:-unknown error}"
}

verify_outcome_files() {
  local required_after="$1"
  local json_path="$2"
  local log_path="$3"
  python3 - \
    "$required_after" \
    "$json_path" \
    "$log_path" <<'PY'
from datetime import datetime
from pathlib import Path
import json
import re
import sys

required_after = datetime.fromisoformat(
    sys.argv[1].replace("Z", "+00:00")
).timestamp()
json_path = Path(sys.argv[2])
log_path = Path(sys.argv[3])
for path in (json_path, log_path):
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f"outcome output missing or empty: {path}")
    if path.stat().st_mtime <= required_after:
        raise SystemExit(
            f"outcome output was not refreshed after the run baseline: {path}"
        )

payload = json.loads(json_path.read_text(encoding="utf-8"))
required_keys = (
    "closed_intent_count",
    "upserted_count",
    "skipped_count",
)
for key in required_keys:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SystemExit(f"invalid outcome JSON field: {key}={value!r}")
if payload["closed_intent_count"] != (
    payload["upserted_count"] + payload["skipped_count"]
):
    raise SystemExit(f"outcome JSON counts are inconsistent: {payload}")
if payload["upserted_count"] <= 0:
    raise SystemExit(f"outcome run produced no trade outcomes: {payload}")

with log_path.open("rb") as handle:
    handle.seek(max(0, log_path.stat().st_size - 65536))
    log_tail = handle.read().decode("utf-8", errors="replace")
pattern = (
    r"processed \d+ closed intents; upserted \d+; "
    r"wrote JSON result to "
)
if re.search(pattern, log_tail) is None:
    raise SystemExit("outcome log lacks a successful completion record")
print(
    "outcome files verified: "
    f"closed={payload['closed_intent_count']} "
    f"upserted={payload['upserted_count']} "
    f"skipped={payload['skipped_count']}"
)
PY
}

verify_report() {
  python3 - <<'PY'
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8090/healthz", timeout=15) as response:
    payload = json.load(response)
dependencies = payload.get("dependencies") or {}
for dependency_name in (
    "database",
    "trade_outcomes",
    "exchange_state_mirror",
):
    dependency = dependencies.get(dependency_name) or {}
    if dependency.get("status") != "ok":
        raise SystemExit(
            f"report dependency unhealthy: "
            f"{dependency_name}={dependency.get('status')}"
        )
if payload.get("status") != "ok":
    raise SystemExit(f"report health status is not ok: {payload.get('status')}")
print(
    "report health verified: "
    f"status={payload.get('status')} "
    f"outcomes={dependencies['trade_outcomes'].get('completed_at')}"
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
    missing = data.get("missing_data")
    if missing:
        raise SystemExit(
            f"{report_type} report has missing data dependencies: {missing}"
        )
    window = data.get("window") or {}
    if not window.get("start") or not window.get("end"):
        raise SystemExit(f"{report_type} report window is incomplete")
    kpis = data.get("kpis") or {}
    trade_count = kpis.get("trade_count")
    if isinstance(trade_count, bool) or not isinstance(trade_count, int):
        raise SystemExit(
            f"{report_type} report trade_count is invalid: {trade_count!r}"
        )
    positions = data.get("positions")
    if not isinstance(positions, list):
        raise SystemExit(f"{report_type} report positions are invalid")
    for position in positions:
        for field in ("symbol", "quantity", "mark_price", "updated_at"):
            if position.get(field) in (None, ""):
                raise SystemExit(
                    f"{report_type} report position field is empty: "
                    f"{field}={position}"
                )
    source_dependencies = report_service.empty_dependency_status()
    source_conn = report_service.connect_database(
        None,
        source_dependencies,
    )
    try:
        with source_conn.cursor() as source_cur:
            source_cur.execute(
                """
                SELECT account_id, payload, updated_at
                FROM exchange_state_mirror
                ORDER BY account_id
                """
            )
            mirror_rows = source_cur.fetchall()
            if mirror_rows:
                source_positions = []
                for account_id, payload, updated_at in mirror_rows:
                    source_positions.extend(
                        report_service.normalize_position_payload(
                            payload,
                            account_id,
                            updated_at,
                        )
                    )
                source_position_count = len(source_positions)
            else:
                source_cur.execute(
                    """
                    SELECT count(*)::int
                    FROM positions_projection
                    WHERE quantity > 0
                      AND status NOT IN ('closed', 'flat')
                    """
                )
                source_position_count = source_cur.fetchone()[0]
            source_cur.execute(
                """
                SELECT count(*)::int
                FROM trade_outcomes
                WHERE closed_at >= %s AND closed_at < %s
                """,
                (window["start"], window["end"]),
            )
            source_trade_count = source_cur.fetchone()[0]
    finally:
        source_conn.close()
    if trade_count != source_trade_count:
        raise SystemExit(
            f"{report_type} report trade_count mismatch: "
            f"report={trade_count} source={source_trade_count}"
        )
    if len(positions) != source_position_count:
        raise SystemExit(
            f"{report_type} report position count mismatch: "
            f"report={len(positions)} source={source_position_count}"
        )
    if report_type == "weekly" and source_trade_count <= 0:
        raise SystemExit("weekly report has no trade outcomes")
    for dependency_name in (
        "database",
        "trade_outcomes",
        "exchange_state_mirror",
    ):
        dependency = dependencies.get(dependency_name) or {}
        if dependency.get("status") != "ok":
            raise SystemExit(
                f"{report_type} dependency unhealthy: "
                f"{dependency_name}={dependency.get('status')}"
            )
    print(
        f"{report_type} report data verified: "
        f"trades={trade_count} source_trades={source_trade_count} "
        f"positions={len(positions)} "
        f"source_positions={source_position_count}"
    )
PY
  )
}

[ "$(id -u)" -eq 0 ] || die "run as root with sudo"
require_command curl
require_command docker
require_command flock
require_command python3
require_command sha256sum
require_command stat
require_command systemctl
id balen >/dev/null 2>&1 || die "service user balen is missing"
[ -n "$EXPECTED_STAGING_MANIFEST_SHA256" ] \
  || die "supply the reviewed staging SHA256SUMS digest as argument 2 or EXPECTED_STAGING_MANIFEST_SHA256"
[[ "$EXPECTED_STAGING_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || die "expected staging manifest SHA256 is invalid"

exec 9>/var/lock/trader-v3-deploy-20260729.lock
flock -n 9 || die "another trader-v3 deployment is running"

required_staging=(
  SHA256SUMS
  host/read_api.py
  host/order_lifecycle_monitor.py
  host/report_service.py
  host/exchange_state_recorder.py
  host/trade_outcomes.py
  db/0009_trade_outcome_job_runs.up.sql
  db/0009_trade_outcome_job_runs.down.sql
  container/event_mapper.py
  container/intent_execution_strategy.py
  systemd/trader-v3-trade-outcomes.service
  systemd/trader-v3-trade-outcomes.timer
  tools/verify_hk_deployment.sh
  manifest.txt
  hk-rollback-20260729.sh
  hk-root-continue-20260729.sh
  commits.txt
)
for relative_path in "${required_staging[@]}"; do
  [ -f "$D/$relative_path" ] \
    || die "missing deployment artifact: $D/$relative_path"
done

python3 - "$D/SHA256SUMS" "${required_staging[@]:1}" <<'PY'
from pathlib import Path
import re
import sys

manifest_path = Path(sys.argv[1])
required = set(sys.argv[2:])
listed = set()
for line_number, raw_line in enumerate(
    manifest_path.read_text(encoding="utf-8").splitlines(),
    start=1,
):
    match = re.fullmatch(r"([0-9a-f]{64})  ([*]?)(.+)", raw_line)
    if match is None:
        raise SystemExit(
            f"invalid staging checksum line {line_number}: {raw_line!r}"
        )
    relative_path = match.group(3)
    if relative_path.startswith("./"):
        relative_path = relative_path[2:]
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(
            f"unsafe staging checksum path on line {line_number}: "
            f"{relative_path}"
        )
    if relative_path in listed:
        raise SystemExit(
            f"duplicate staging checksum entry: {relative_path}"
        )
    listed.add(relative_path)
missing = sorted(required - listed)
if missing:
    raise SystemExit(
        f"required artifacts missing from staging SHA256SUMS: {missing}"
    )
print(f"staging checksum membership verified: {len(required)} required files")
PY

ACTUAL_STAGING_MANIFEST_SHA256=$(
  sha256sum "$D/SHA256SUMS" | awk '{print $1}'
)
[ "$ACTUAL_STAGING_MANIFEST_SHA256" = "$EXPECTED_STAGING_MANIFEST_SHA256" ] \
  || die "staging SHA256SUMS digest does not match the reviewed digest"
(cd "$D" && sha256sum -c SHA256SUMS)

[ -r "$BACKUP_ROOT/BACKUP_SHA256SUMS" ] \
  || die "backup checksum manifest missing"
(cd "$BACKUP_ROOT" && sha256sum -c BACKUP_SHA256SUMS)
verify_backup_inventory
verify_target_hashes

ENV_MODE=$(stat -c '%U:%G %a' /etc/trader-v3/trade-outcomes.env)
[ "$ENV_MODE" = "root:root 600" ] \
  || die "trade outcomes environment file ownership or mode is invalid"
[ ! -e /etc/cron.d/trader-v3-trade-outcomes ] \
  || die "legacy trade outcomes cron entry still exists"
if systemctl is-active --quiet trader-v3-trade-outcomes.service; then
  die "trade outcomes service must be inactive at the continuation checkpoint"
fi
if systemctl is-active --quiet trader-v3-trade-outcomes.timer; then
  die "trade outcomes timer must be inactive at the continuation checkpoint"
fi

for unit in \
  trader-v3-controlplane \
  trader-v3-report \
  trader-v3-exchange-state \
  trader-v3-lifecycle-monitor; do
  systemctl is-active --quiet "$unit" || die "$unit is not active"
done
wait_for_controlplane_http

for node in "$NODE_A" "$NODE_B"; do
  docker inspect "$node" >/dev/null
  NODE_STATUS=$(docker inspect --format '{{.State.Status}}' "$node")
  [ "$NODE_STATUS" = "exited" ] \
    || die "$node is not stopped: State.Status=$NODE_STATUS"
done
verify_existing_node_runtime

load_database_url
database_marker verify-0009

docker start "$NODE_A"
verify_node_halted 8081
docker start "$NODE_B"
verify_node_halted 8082
verify_runtime_manifest

OUTCOME_RUN_STARTED_AT=$(database_marker capture-time)
[ -n "$OUTCOME_RUN_STARTED_AT" ] \
  || die "failed to capture outcome run baseline"
systemctl start trader-v3-trade-outcomes.service
SERVICE_RESULT=$(
  systemctl show trader-v3-trade-outcomes.service \
    --property=Result \
    --value
)
[ "$SERVICE_RESULT" = "success" ] \
  || die "trade outcomes service result is $SERVICE_RESULT"
database_marker verify-watermark "$OUTCOME_RUN_STARTED_AT"
verify_outcome_files \
  "$OUTCOME_RUN_STARTED_AT" \
  /var/log/trader-v3/trade-outcomes-latest.json \
  /var/log/trader-v3/trade-outcomes.log

install -d -m 0755 /var/lib/systemd/timers
touch /var/lib/systemd/timers/stamp-trader-v3-trade-outcomes.timer
chown root:root /var/lib/systemd/timers/stamp-trader-v3-trade-outcomes.timer
chmod 0644 /var/lib/systemd/timers/stamp-trader-v3-trade-outcomes.timer
systemctl enable trader-v3-trade-outcomes.timer
systemctl start trader-v3-trade-outcomes.timer
systemctl is-active --quiet trader-v3-trade-outcomes.timer \
  || die "trade outcomes timer is not active"

verify_report
verify_node_halted 8081
verify_node_halted 8082

echo "CONTINUATION SUCCEEDED"
echo "Staging: $D"
echo "Backup: $BACKUP_ROOT"
echo "Trading remains HALTED pending protection E2E acceptance"
