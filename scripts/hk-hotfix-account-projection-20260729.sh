#!/usr/bin/env bash
set -Eeuo pipefail

D="${1:-}"
T="${TRADER_ROOT:-/srv/trader-v3}"
BACKUP_ROOT="${BACKUP_ROOT:-$T/backups/account-projection-hotfix-$(date -u +%Y%m%dT%H%M%SZ)}"
LOCK_PATH="${LOCK_PATH:-/var/lock/trader-v3-deploy-20260729.lock}"
MUTATION_STARTED=0
ROLLED_BACK=0
SERVICES_STOPPED=0

die() {
  echo "FATAL: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command missing: $1"
}

capture_projection_state() {
  "$T/.venv-cp/bin/python" - \
    "$T/.env.v3" \
    "$BACKUP_ROOT/accounts_projection-preimage.json" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg2


def read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


environment = read_environment(Path(sys.argv[1]))
database_url = environment.get("DATABASE_URL", "")
if not database_url:
    raise SystemExit("DATABASE_URL missing from environment")

conn = psycopg2.connect(database_url)
try:
    with conn.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        watermark = cur.fetchone()[0]
        cur.execute(
            """
            SELECT account_id, currency, equity, margin, available_balance,
                   reconciliation_state::text, last_execution_event_at,
                   projection_lag_ms, updated_from_event_id, updated_at, payload
            FROM accounts_projection
            WHERE account_id IN ('account-a', 'account-b')
            ORDER BY account_id
            """
        )
        rows = cur.fetchall()
finally:
    conn.close()

serialized_rows = []
for row in rows:
    serialized_rows.append(
        {
            "account_id": row[0],
            "currency": row[1],
            "equity": str(row[2]),
            "margin": str(row[3]),
            "available_balance": False if row[4] is None else str(row[4]),
            "reconciliation_state": row[5],
            "last_execution_event_at": (
                False if row[6] is None else row[6].isoformat()
            ),
            "projection_lag_ms": int(row[7]),
            "updated_from_event_id": False if row[8] is None else row[8],
            "updated_at": row[9].isoformat(),
            "payload": row[10] or {},
        }
    )

state = {
    "watermark": watermark.isoformat(),
    "rows": serialized_rows,
}
Path(sys.argv[2]).write_text(
    json.dumps(state, ensure_ascii=True, separators=(",", ":")),
    encoding="utf-8",
)
PY
  chmod 0600 "$BACKUP_ROOT/accounts_projection-preimage.json"
}

restore_projection_state() {
  "$T/.venv-cp/bin/python" - \
    "$T/.env.v3" \
    "$BACKUP_ROOT/accounts_projection-preimage.json" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import Json


def read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


environment = read_environment(Path(sys.argv[1]))
database_url = environment.get("DATABASE_URL", "")
if not database_url:
    raise SystemExit("DATABASE_URL missing from environment")
state = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

conn = psycopg2.connect(database_url)
try:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM accounts_projection "
            "WHERE account_id IN ('account-a', 'account-b')"
        )
        for row in state.get("rows", []):
            available_balance = row["available_balance"]
            if available_balance is False:
                available_balance = None
            last_execution_event_at = row["last_execution_event_at"]
            if last_execution_event_at is False:
                last_execution_event_at = None
            updated_from_event_id = row["updated_from_event_id"]
            if updated_from_event_id is False:
                updated_from_event_id = None
            cur.execute(
                """
                INSERT INTO accounts_projection (
                    account_id, currency, equity, margin, available_balance,
                    reconciliation_state, last_execution_event_at,
                    projection_lag_ms, updated_from_event_id, updated_at, payload
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s::reconciliation_state_v1, %s,
                    %s, %s, %s, %s
                )
                """,
                (
                    row["account_id"],
                    row["currency"],
                    row["equity"],
                    row["margin"],
                    available_balance,
                    row["reconciliation_state"],
                    last_execution_event_at,
                    row["projection_lag_ms"],
                    updated_from_event_id,
                    row["updated_at"],
                    Json(row["payload"]),
                ),
            )
    conn.commit()
except Exception:
    conn.rollback()
    raise
finally:
    conn.close()
PY
}

capture_verification_watermark() {
  "$T/.venv-cp/bin/python" - \
    "$T/.env.v3" \
    "$BACKUP_ROOT/verification-watermark.txt" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

import psycopg2


def read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


environment = read_environment(Path(sys.argv[1]))
database_url = environment.get("DATABASE_URL", "")
if not database_url:
    raise SystemExit("DATABASE_URL missing from environment")

conn = psycopg2.connect(database_url)
try:
    with conn.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        watermark = cur.fetchone()[0]
finally:
    conn.close()

Path(sys.argv[2]).write_text(watermark.isoformat(), encoding="utf-8")
PY
  chmod 0600 "$BACKUP_ROOT/verification-watermark.txt"
}

restore_backup() {
  if [ "$ROLLED_BACK" -eq 1 ]; then
    return
  fi
  if [ "$MUTATION_STARTED" -ne 1 ] && [ "$SERVICES_STOPPED" -ne 1 ]; then
    return
  fi
  ROLLED_BACK=1
  local rollback_failed=0
  if ! systemctl stop trader-v3-controlplane trader-v3-exchange-state; then
    rollback_failed=1
  fi
  if [ "$MUTATION_STARTED" -eq 1 ]; then
    if ! install -m 0644 \
      "$BACKUP_ROOT/read_api.py" \
      "$T/services/control-plane/api/read_api.py"; then
      rollback_failed=1
    fi
    if ! install -m 0644 \
      "$BACKUP_ROOT/exchange_state_recorder.py" \
      "$T/services/control-plane/tools/exchange_state_recorder.py"; then
      rollback_failed=1
    fi
    if ! restore_projection_state; then
      rollback_failed=1
    fi
  fi
  if ! systemctl start trader-v3-controlplane; then
    rollback_failed=1
  fi
  if ! systemctl start trader-v3-exchange-state; then
    rollback_failed=1
  fi
  if [ "$rollback_failed" -eq 1 ]; then
    echo "Rollback incomplete; inspect $BACKUP_ROOT immediately" >&2
    return
  fi
  echo "Restored account services from $BACKUP_ROOT" >&2
}

on_error() {
  local status=$?
  trap - ERR
  restore_backup
  exit "$status"
}

on_signal() {
  local status="$1"
  trap - ERR
  restore_backup
  exit "$status"
}

trap on_error ERR
trap 'on_signal 129' HUP
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

[ "$(id -u)" -eq 0 ] || die "run as root with sudo"
[ -n "$D" ] || die "usage: $0 /path/to/reviewed-hotfix"
require_command flock
require_command install
require_command cmp
require_command docker
require_command python3
require_command sha256sum
require_command systemctl

exec 9>"$LOCK_PATH"
flock -n 9 || die "another trader-v3 deployment is running"

required=(
  SHA256SUMS
  host/read_api.py
  host/exchange_state_recorder.py
)
for relative_path in "${required[@]}"; do
  [ -f "$D/$relative_path" ] || die "missing hotfix artifact: $D/$relative_path"
done
systemctl is-active --quiet trader-v3-controlplane \
  || die "trader-v3-controlplane must be active before hotfix"
systemctl is-active --quiet trader-v3-exchange-state \
  || die "trader-v3-exchange-state must be active before hotfix"
if systemctl is-active --quiet trader-v3-lifecycle-monitor; then
  die "trader-v3-lifecycle-monitor must be stopped before hotfix"
fi
for container in trader-v3-node-a trader-v3-node-b; do
  running="$(docker inspect "$container" --format '{{.State.Running}}')" \
    || die "cannot inspect $container"
  [ "$running" = "false" ] || die "$container must be stopped before hotfix"
done

mkdir -p "$BACKUP_ROOT"
chmod 0700 "$BACKUP_ROOT"
STAGE_ROOT="$BACKUP_ROOT/reviewed-artifact"
mkdir -p "$STAGE_ROOT/host"
chmod 0700 "$STAGE_ROOT" "$STAGE_ROOT/host"
install -m 0600 "$D/SHA256SUMS" "$STAGE_ROOT/SHA256SUMS"
install -m 0600 "$D/host/read_api.py" "$STAGE_ROOT/host/read_api.py"
install -m 0600 \
  "$D/host/exchange_state_recorder.py" \
  "$STAGE_ROOT/host/exchange_state_recorder.py"
(cd "$STAGE_ROOT" && sha256sum -c SHA256SUMS)
python3 -m py_compile \
  "$STAGE_ROOT/host/read_api.py" \
  "$STAGE_ROOT/host/exchange_state_recorder.py"

cp -a \
  "$T/services/control-plane/api/read_api.py" \
  "$BACKUP_ROOT/read_api.py"
cp -a \
  "$T/services/control-plane/tools/exchange_state_recorder.py" \
  "$BACKUP_ROOT/exchange_state_recorder.py"
chmod 0600 "$BACKUP_ROOT/read_api.py" "$BACKUP_ROOT/exchange_state_recorder.py"

SERVICES_STOPPED=1
systemctl stop trader-v3-controlplane trader-v3-exchange-state
capture_projection_state

MUTATION_STARTED=1
install -m 0644 \
  "$STAGE_ROOT/host/read_api.py" \
  "$T/services/control-plane/api/read_api.py"
install -m 0644 \
  "$STAGE_ROOT/host/exchange_state_recorder.py" \
  "$T/services/control-plane/tools/exchange_state_recorder.py"
cmp -s \
  "$STAGE_ROOT/host/read_api.py" \
  "$T/services/control-plane/api/read_api.py"
cmp -s \
  "$STAGE_ROOT/host/exchange_state_recorder.py" \
  "$T/services/control-plane/tools/exchange_state_recorder.py"
systemctl start trader-v3-controlplane trader-v3-exchange-state
systemctl is-active --quiet trader-v3-controlplane
systemctl is-active --quiet trader-v3-exchange-state
capture_verification_watermark

"$T/.venv-cp/bin/python" - \
  "$T/.env.v3" \
  "$BACKUP_ROOT/verification-watermark.txt" <<'PY'
from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import psycopg2


def read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def fresh_accounts(
    database_url: str,
    watermark: datetime,
) -> dict[str, float]:
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT account_id, equity,
                       EXTRACT(EPOCH FROM (now() - updated_at)) AS age_seconds,
                       updated_at,
                       payload->>'account_snapshot_source' AS snapshot_source,
                       payload->>'account_snapshot_fetched_at' AS snapshot_fetched_at
                FROM accounts_projection
                WHERE account_id IN ('account-a', 'account-b')
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    fresh: dict[str, float] = {}
    for account_id, equity_raw, age_raw, updated_at, source, fetched_at in rows:
        equity = float(equity_raw)
        age_seconds = float(age_raw)
        if not math.isfinite(equity) or not math.isfinite(age_seconds):
            continue
        if equity <= 0 or age_seconds < 0 or age_seconds > 180:
            continue
        if updated_at <= watermark:
            continue
        if source != "binance_fapi_account_v3" or not fetched_at:
            continue
        fetched_at_value = datetime.fromisoformat(
            fetched_at.replace("Z", "+00:00")
        )
        if fetched_at_value <= watermark:
            continue
        fresh[str(account_id)] = age_seconds
    return fresh


def verify_dry_run(account_id: str, token: str) -> None:
    payload = {
        "action": "open_position",
        "symbol": "BTCUSDT",
        "side": "short",
        "entry": {"type": "limit", "price": 50000},
        "account_id": account_id,
        "reason": "account projection hotfix verification",
        "source": "deployment-verifier",
        "source_channel": "operator",
        "client_ref": f"operator-account-projection-hotfix-{account_id}",
        "notional_usdt": 1,
        "dry_run": True,
    }
    request = Request(
        "http://127.0.0.1:8080/v1/operator/orders",
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        data=json.dumps(payload).encode("utf-8"),
    )
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"{account_id} dry-run failed with HTTP {exc.code}: {detail}"
        ) from exc
    if result.get("dry_run") is not True:
        raise SystemExit(f"{account_id} verification response was not dry-run")
    if result.get("account_id") != account_id:
        raise SystemExit(f"{account_id} verification returned wrong account")


environment = read_environment(Path(sys.argv[1]))
watermark = datetime.fromisoformat(
    Path(sys.argv[2]).read_text(encoding="utf-8").strip()
)
database_url = environment.get("DATABASE_URL", "")
risk_admin_token = environment.get("RISK_ADMIN_TOKEN", "")
if not database_url:
    raise SystemExit("DATABASE_URL missing from environment")
if not risk_admin_token:
    raise SystemExit("RISK_ADMIN_TOKEN missing from environment")

deadline = time.monotonic() + 150
accounts: dict[str, float] = {}
while time.monotonic() < deadline:
    accounts = fresh_accounts(database_url, watermark)
    if set(accounts) == {"account-a", "account-b"}:
        break
    time.sleep(5)
if set(accounts) != {"account-a", "account-b"}:
    raise SystemExit(f"fresh account projections missing after timeout: {accounts}")

verify_dry_run("account-a", risk_admin_token)
verify_dry_run("account-b", risk_admin_token)
print(
    "account projection hotfix verified: "
    + ", ".join(
        f"{account_id} age={accounts[account_id]:.1f}s"
        for account_id in sorted(accounts)
    )
)
PY

sha256sum \
  "$T/services/control-plane/api/read_api.py" \
  "$T/services/control-plane/tools/exchange_state_recorder.py" \
  >"$BACKUP_ROOT/deployed-SHA256SUMS"
chmod 0600 "$BACKUP_ROOT/deployed-SHA256SUMS"
MUTATION_STARTED=0
SERVICES_STOPPED=0
trap - ERR HUP INT TERM
echo "Account projection hotfix complete. Backup: $BACKUP_ROOT"
