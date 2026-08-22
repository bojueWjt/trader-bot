#!/usr/bin/env bash
set -Eeuo pipefail

MODE="${1:-apply}"
TRADER_ROOT="${TRADER_ROOT:-/srv/trader-v3}"
SOURCE_ROOT="${JP24_P1_SOURCE_ROOT:-/srv/trader-staging/p1-source}"
VENV_ROOT="$TRADER_ROOT/.venv-cp"
RUNTIME_ENV_FILE="$TRADER_ROOT/.env.v3"
ROLE_SECRET_ROOT="$TRADER_ROOT/secrets/control-plane"
SYSTEMD_ROOT="/etc/systemd/system"

ROLE_NAMES=(node-control event-ingest operator-query)
ROLE_PORTS=(8181 8182 8183)
ROLE_USERS=(
  trader-v3-cp-node-control
  trader-v3-cp-event-ingest
  trader-v3-cp-operator-query
)
ROLE_DATABASES=(
  trader_v3_node_control
  trader_v3_event_ingest
  trader_v3_operator_query
)
ROLE_ENV_FILES=(
  "$ROLE_SECRET_ROOT/node-control.env"
  "$ROLE_SECRET_ROOT/event-ingest.env"
  "$ROLE_SECRET_ROOT/operator-query.env"
)
ROLE_RESOURCE_FILES=(
  "$TRADER_ROOT/infra/systemd/account-stall-control-plane-writer.conf"
  "$TRADER_ROOT/infra/systemd/account-stall-control-plane-writer.conf"
  "$TRADER_ROOT/infra/systemd/account-stall-control-plane-reader.conf"
)

die() {
  echo "FATAL: $*" >&2
  exit 2
}

require_root() {
  [ "$(id -u)" -eq 0 ] || die "must run as root"
}

require_jp24() {
  [ "$(hostname)" = "jp-24" ] || die "host must be jp-24"
}

require_halted() {
  local container
  local running
  for container in \
    trader-v3-node-a \
    trader-v3-node-b \
    trader-v3-node-c \
    trader-v3-node-d; do
    running="$(
      docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null \
        || true
    )"
    [ "$running" != "true" ] \
      || die "account node must remain stopped: $container"
  done
}

require_source() {
  local path
  for path in \
    "$SOURCE_ROOT/services/control-plane/api/read_api.py" \
    "$SOURCE_ROOT/services/control-plane/db/migrate.py" \
    "$SOURCE_ROOT/services/nautilus-node/observability/__init__.py" \
    "$SOURCE_ROOT/packages/execution-domain/execution_domain/__init__.py" \
    "$SOURCE_ROOT/db/migrations/0014_cancel_order_contract.up.sql" \
    "$SOURCE_ROOT/db/migrations/0015_refresh_evidence_command.up.sql" \
    "$SOURCE_ROOT/db/migrations/0016_control_plane_lock_privileges.up.sql" \
    "$SOURCE_ROOT/db/migrations/0017_operator_query_projection_reads.up.sql" \
    "$SOURCE_ROOT/infra/systemd/account-stall-control-plane-writer.conf" \
    "$SOURCE_ROOT/infra/systemd/account-stall-control-plane-reader.conf" \
    "$SOURCE_ROOT/scripts/bootstrap_control_plane_roles.py"; do
    [ -f "$path" ] || die "staged source is incomplete: $path"
  done
}

install_source() {
  install -d -m 0755 "$TRADER_ROOT/services"
  install -d -m 0755 "$TRADER_ROOT/services/nautilus-node"
  install -d -m 0755 "$TRADER_ROOT/packages"
  install -d -m 0755 "$TRADER_ROOT/db/migrations"
  install -d -m 0755 "$TRADER_ROOT/infra/systemd"
  install -d -m 0755 "$TRADER_ROOT/scripts"
  cp -a "$SOURCE_ROOT/services/control-plane" "$TRADER_ROOT/services/"
  cp -a \
    "$SOURCE_ROOT/services/nautilus-node/observability" \
    "$TRADER_ROOT/services/nautilus-node/"
  cp -a "$SOURCE_ROOT/packages/execution-domain" "$TRADER_ROOT/packages/"
  cp -a "$SOURCE_ROOT/db/migrations/." "$TRADER_ROOT/db/migrations/"
  cp -a "$SOURCE_ROOT/infra/systemd/." "$TRADER_ROOT/infra/systemd/"
  cp -a \
    "$SOURCE_ROOT/scripts/bootstrap_control_plane_roles.py" \
    "$TRADER_ROOT/scripts/bootstrap_control_plane_roles.py"
  cp -a \
    "$SOURCE_ROOT/scripts/hk-control-plane-isolation.sh" \
    "$TRADER_ROOT/scripts/hk-control-plane-isolation.sh"
  find "$TRADER_ROOT/services" "$TRADER_ROOT/packages" "$TRADER_ROOT/db" \
    -type d -exec chmod 0755 {} +
  find "$TRADER_ROOT/services" "$TRADER_ROOT/packages" "$TRADER_ROOT/db" \
    -type f -exec chmod 0644 {} +
  chmod 0755 \
    "$TRADER_ROOT/scripts/bootstrap_control_plane_roles.py" \
    "$TRADER_ROOT/scripts/hk-control-plane-isolation.sh"
}

install_python_runtime() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y python3-venv
  if [ ! -x "$VENV_ROOT/bin/python" ]; then
    python3 -m venv "$VENV_ROOT"
  fi
  "$VENV_ROOT/bin/python" -m pip install \
    --disable-pip-version-check \
    fastapi==0.138.0 \
    httpx==0.28.1 \
    jsonschema==4.26.0 \
    psycopg2-binary==2.9.12 \
    pydantic==2.13.4 \
    uvicorn==0.49.0
}

database_url() {
  "$VENV_ROOT/bin/python" - "$RUNTIME_ENV_FILE" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

values = []
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    if name.strip() == "DATABASE_URL":
        values.append(value.strip())
if len(values) != 1 or not values[0]:
    raise SystemExit("DATABASE_URL must appear exactly once")
print(values[0])
PY
}

apply_migrations() {
  local url
  url="$(database_url)"
  DATABASE_URL="$url" \
    "$VENV_ROOT/bin/python" \
    "$TRADER_ROOT/services/control-plane/db/migrate.py" \
    up
}

reconcile_role_contracts() {
  "$VENV_ROOT/bin/python" - "$RUNTIME_ENV_FILE" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

import psycopg2


def read_database_url(path: Path) -> str:
    values = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == "DATABASE_URL":
            values.append(value.strip())
    if len(values) != 1 or not values[0]:
        raise SystemExit("DATABASE_URL must appear exactly once")
    return values[0]


# Migration 0016 owns this contract; reconciliation remains idempotent.
lock_privileges = (
    ("trader_v3_node_control", "redis_fencing_epochs", "created_at", False),
    ("trader_v3_event_ingest", "redis_fencing_epochs", "created_at", False),
    ("trader_v3_event_ingest", "node_heartbeats", "created_at", "status"),
    ("trader_v3_operator_query", "node_heartbeats", "created_at", "status"),
    (
        "trader_v3_operator_query",
        "control_plane_maintenance_fences",
        "acquired_at",
        False,
    ),
)

connection = psycopg2.connect(read_database_url(Path(sys.argv[1])))
try:
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER ROLE trader_v3_node_control CONNECTION LIMIT 32"
        )
        for role, table, lock_column, _forbidden in lock_privileges:
            cursor.execute(
                "GRANT UPDATE ({}) ON {} TO {}".format(
                    lock_column,
                    table,
                    role,
                )
            )
    connection.commit()
    with connection.cursor() as cursor:
        for role, table, lock_column, forbidden in lock_privileges:
            cursor.execute(
                "SELECT has_column_privilege(%s, %s, %s, 'UPDATE')",
                (role, table, lock_column),
            )
            row = cursor.fetchone()
            if row is None or row[0] is not True:
                raise SystemExit(
                    f"missing lock privilege: {role}.{table}.{lock_column}"
                )
            if forbidden is False:
                continue
            cursor.execute(
                "SELECT has_column_privilege(%s, %s, %s, 'UPDATE')",
                (role, table, forbidden),
            )
            forbidden_row = cursor.fetchone()
            if forbidden_row is None or forbidden_row[0] is not False:
                raise SystemExit(
                    f"forbidden probe column became writable: "
                    f"{role}.{table}.{forbidden}"
                )
        cursor.execute(
            "SELECT rolconnlimit FROM pg_roles "
            "WHERE rolname='trader_v3_node_control'"
        )
        limit_row = cursor.fetchone()
        if limit_row is None or int(limit_row[0]) != 32:
            raise SystemExit("node-control connection limit mismatch")
finally:
    connection.close()

print("CONTROL_PLANE_LOCK_PRIVILEGES_OK")
print("node_control_connection_limit=32")
PY
}

ensure_service_users() {
  local index
  local user
  for index in "${!ROLE_USERS[@]}"; do
    user="${ROLE_USERS[$index]}"
    if ! getent group "$user" >/dev/null; then
      groupadd --system "$user"
    fi
    if ! getent passwd "$user" >/dev/null; then
      useradd \
        --system \
        --gid "$user" \
        --home-dir /nonexistent \
        --shell /usr/sbin/nologin \
        "$user"
    fi
  done
}

bootstrap_roles() {
  "$VENV_ROOT/bin/python" \
    "$TRADER_ROOT/scripts/bootstrap_control_plane_roles.py" \
    apply \
    --env-file "$RUNTIME_ENV_FILE" \
    --output-dir "$ROLE_SECRET_ROOT" \
    --owner-uid 0 \
    --owner-gid 0
}

write_role_unit() {
  local index="$1"
  local role="${ROLE_NAMES[$index]}"
  local port="${ROLE_PORTS[$index]}"
  local user="${ROLE_USERS[$index]}"
  local database_role="${ROLE_DATABASES[$index]}"
  local env_file="${ROLE_ENV_FILES[$index]}"
  local resource_file="${ROLE_RESOURCE_FILES[$index]}"
  local unit="$SYSTEMD_ROOT/trader-v3-controlplane-$role.service"
  local temporary
  temporary="$(mktemp "$SYSTEMD_ROOT/.trader-v3-controlplane-$role.XXXXXX")"
  cat > "$temporary" <<EOF
[Unit]
Description=Trader v3 control-plane role $role
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
User=$user
Group=$user
WorkingDirectory=$TRADER_ROOT/services/control-plane/api
EnvironmentFile=$env_file
Environment=CONTROL_PLANE_APP_ROLE=$role
Environment=CONTROL_PLANE_EXPECT_DATABASE_ROLE=$database_role
ExecStartPre=+/usr/bin/docker exec trader-v3-postgres pg_isready -U postgres -d trader
ExecStart=$VENV_ROOT/bin/uvicorn read_api:app --host 127.0.0.1 --port $port
TimeoutStopSec=15
EOF
  sed '1d' "$resource_file" >> "$temporary"
  cat >> "$temporary" <<'EOF'

[Install]
WantedBy=multi-user.target
EOF
  install -m 0644 "$temporary" "$unit"
  rm -f "$temporary"
}

install_role_units() {
  local index
  for index in "${!ROLE_NAMES[@]}"; do
    write_role_unit "$index"
  done
  systemctl daemon-reload
  for index in "${!ROLE_NAMES[@]}"; do
    systemctl enable \
      "trader-v3-controlplane-${ROLE_NAMES[$index]}.service"
    systemctl restart \
      "trader-v3-controlplane-${ROLE_NAMES[$index]}.service"
  done
}

verify_role_health() {
  "$VENV_ROOT/bin/python" - <<'PY'
from __future__ import annotations

import json
import time
from urllib.error import URLError
from urllib.request import urlopen

expected = {
    8181: ("node-control", "trader_v3_node_control"),
    8182: ("event-ingest", "trader_v3_event_ingest"),
    8183: ("operator-query", "trader_v3_operator_query"),
}
deadline = time.monotonic() + 45
for port, (app_role, database_role) in expected.items():
    url = f"http://127.0.0.1:{port}/health/role"
    while True:
        try:
            with urlopen(url, timeout=3) as response:
                payload = json.load(response)
            break
        except URLError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    required = {
        "status": "healthy",
        "app_role": app_role,
        "expected_database_role": database_role,
        "session_user": database_role,
        "current_user": database_role,
        "rollback_only_permission_probe": "pass",
    }
    if payload != required:
        raise SystemExit(f"role health mismatch on port {port}: {payload}")
    print(json.dumps(payload, sort_keys=True))

with urlopen("http://127.0.0.1:8080/health/role", timeout=5) as response:
    router_payload = json.load(response)
if router_payload.get("app_role") != "operator-query":
    raise SystemExit("Caddy default route is not operator-query")
print("caddy_router_role=operator-query")
PY
}

verify_role_units() {
  local index
  local unit
  for index in "${!ROLE_NAMES[@]}"; do
    unit="trader-v3-controlplane-${ROLE_NAMES[$index]}.service"
    systemctl is-active "$unit"
    systemctl show "$unit" \
      -p ActiveState \
      -p SubState \
      -p MainPID \
      -p MemoryMax \
      -p NRestarts
  done
  grep -E \
    '^CONTROL_PLANE_NODE_CONTROL_DB_(POOL_SIZE|CHECKOUT_TIMEOUT_SECONDS)=' \
    "$ROLE_SECRET_ROOT/node-control.env"
}

run_checks() {
  require_halted
  verify_role_health
  verify_role_units
  echo "JP24_P1_CONTROL_PLANE_OK"
}

main() {
  require_root
  require_jp24
  case "$MODE" in
    apply)
      require_halted
      require_source
      install_source
      install_python_runtime
      apply_migrations
      reconcile_role_contracts
      ensure_service_users
      bootstrap_roles
      install_role_units
      run_checks
      ;;
    check)
      run_checks
      ;;
    *)
      die "usage: $0 [apply|check]"
      ;;
  esac
}

main "$@"
