#!/usr/bin/env bash
set -Eeuo pipefail

MODE="${1:-apply}"
TRADER_ROOT="${TRADER_ROOT:-/srv/trader-v3}"
STAGING_ROOT="${TRADER_STAGING_ROOT:-/srv/trader-staging}"
TAILSCALE_IP="${JP24_TAILSCALE_IP:-100.89.58.40}"
PRIMARY_INTERFACE="${JP24_PRIMARY_INTERFACE:-eth0}"
POSTGRES_IMAGE="${JP24_POSTGRES_IMAGE:-postgres@sha256:081f1bc7bd5e143dbb6e487b710bbc27712cdcfaced4c071b8e47349aa1b4171}"
REDIS_IMAGE="${JP24_REDIS_IMAGE:-redis@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99}"
REDIS_MAXMEMORY_BYTES="${JP24_REDIS_MAXMEMORY_BYTES:-1610612736}"
REDIS_CONTAINER_MEMORY_BYTES="${JP24_REDIS_CONTAINER_MEMORY_BYTES:-2147483648}"
POSTGRES_CONTAINER="trader-v3-postgres"
POSTGRES_VOLUME="trader-v3-pgdata"
REDIS_CONTAINER="trader-v3-redis"
REDIS_VOLUME="trader-v3-redis-data"
POSTGRES_PASSWORD_FILE="$TRADER_ROOT/secrets/postgres/admin-password"
REDIS_FENCING_EPOCH_FILE="$TRADER_ROOT/secrets/redis/fencing-epoch"
CONTROL_PLANE_NODE_TOKEN_FILE="$TRADER_ROOT/secrets/control-plane/bootstrap-node-token"
CONTROL_PLANE_RISK_ADMIN_TOKEN_FILE="$TRADER_ROOT/secrets/control-plane/bootstrap-risk-admin-token"
WATCHER_DATABASE_FILE="$TRADER_ROOT/state/watcher/trading.db"
RUNTIME_ENV_FILE="$TRADER_ROOT/.env.v3"
EGRESS_SCRIPT="/usr/local/sbin/trader-v3-egress-rules"
EGRESS_UNIT="/etc/systemd/system/trader-v3-egress-rules.service"
CADDY_FILE="/etc/caddy/Caddyfile"
REDIS_FENCING_EPOCH_KEY="trader-bot:redis-fencing-epoch"

ACCOUNT_LABELS=(a b c d)
ACCOUNT_NETWORKS=(
  trader-v3-account-a
  trader-v3-account-b
  trader-v3-account-c
  trader-v3-account-d
)
ACCOUNT_SUBNETS=(
  172.30.1.0/24
  172.30.2.0/24
  172.30.3.0/24
  172.30.4.0/24
)
ACCOUNT_GATEWAYS=(
  172.30.1.1
  172.30.2.1
  172.30.3.1
  172.30.4.1
)
ACCOUNT_EGRESS_IPS=(
  170.205.39.79
  170.205.39.82
  170.205.39.88
  170.205.39.88
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

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y caddy curl iptables
}

prepare_directories() {
  install -d -m 0755 "$TRADER_ROOT"
  install -d -m 0755 "$STAGING_ROOT"
  install -d -m 0700 "$TRADER_ROOT/secrets"
  install -d -m 0700 "$TRADER_ROOT/secrets/postgres"
  install -d -m 0700 "$TRADER_ROOT/secrets/redis"
  install -d -m 0700 "$TRADER_ROOT/secrets/control-plane"
  install -d -m 0750 "$TRADER_ROOT/state"
  install -d -m 0750 "$TRADER_ROOT/state/watcher"
  install -d -m 0755 "$TRADER_ROOT/infra/systemd"
  local filesystem
  filesystem="$(findmnt -n -o FSTYPE --target "$STAGING_ROOT")"
  [ -n "$filesystem" ] || die "staging filesystem cannot be resolved"
  [ "$filesystem" != "tmpfs" ] || die "staging filesystem must not be tmpfs"
}

ensure_secret() {
  local path="$1"
  local kind="$2"
  python3 - "$path" "$kind" <<'PY'
from __future__ import annotations

import os
import secrets
import stat
import sys
from pathlib import Path
from uuid import uuid4

path = Path(sys.argv[1])
kind = sys.argv[2]
if path.exists():
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"secret is not a regular file: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SystemExit(f"secret mode must be 0600: {path}")
    if metadata.st_uid != 0 or metadata.st_gid != 0:
        raise SystemExit(f"secret owner must be root: {path}")
    if not path.read_text(encoding="utf-8").strip():
        raise SystemExit(f"secret is empty: {path}")
    raise SystemExit(0)

value = ""
if kind == "password":
    value = secrets.token_urlsafe(48)
elif kind == "uuid":
    value = str(uuid4())
else:
    raise SystemExit(f"unsupported secret kind: {kind}")

flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
descriptor = os.open(path, flags, 0o600)
try:
    os.write(descriptor, (value + "\n").encode("utf-8"))
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

write_runtime_env() {
  python3 - \
    "$POSTGRES_PASSWORD_FILE" \
    "$CONTROL_PLANE_NODE_TOKEN_FILE" \
    "$CONTROL_PLANE_RISK_ADMIN_TOKEN_FILE" \
    "$WATCHER_DATABASE_FILE" \
    "$RUNTIME_ENV_FILE" <<'PY'
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

password_path = Path(sys.argv[1])
node_token_path = Path(sys.argv[2])
risk_admin_token_path = Path(sys.argv[3])
watcher_database_path = Path(sys.argv[4])
output_path = Path(sys.argv[5])
password = password_path.read_text(encoding="utf-8").strip()
if not password:
    raise SystemExit("PostgreSQL password is empty")
node_token = node_token_path.read_text(encoding="utf-8").strip()
if not node_token:
    raise SystemExit("control-plane node token is empty")
risk_admin_token = risk_admin_token_path.read_text(
    encoding="utf-8",
).strip()
if not risk_admin_token:
    raise SystemExit("control-plane risk-admin token is empty")
watcher_database_path.touch(mode=0o600, exist_ok=True)
os.chmod(watcher_database_path, 0o600)
os.chown(watcher_database_path, 0, 0)

lines = [
    "DATABASE_URL=postgresql://postgres:"
    + quote(password, safe="")
    + "@127.0.0.1:5432/trader",
    "REDIS_URL=redis://127.0.0.1:6379/0",
    "CONTROL_PLANE_NODE_CONTROL_DB_POOL_SIZE=24",
    "CONTROL_PLANE_NODE_CONTROL_DB_CHECKOUT_TIMEOUT_SECONDS=2",
    "CONTROL_PLANE_NODE_CONTROL_DB_CONNECT_TIMEOUT_SECONDS=1",
    "NAUTILUS_NODE_TOKEN=" + node_token,
    "RISK_ADMIN_TOKEN=" + risk_admin_token,
    "WATCHER_TRADING_DB=" + str(watcher_database_path),
    "BINANCE_EGRESS_MODE=account_networks",
    "BINANCE_PROXY_URL=",
    "BINANCE_EXPECTED_EGRESS_IP=170.205.39.79",
    "BINANCE_EXPECTED_EGRESS_IP_A=170.205.39.79",
    "BINANCE_EXPECTED_EGRESS_IP_B=170.205.39.82",
    "BINANCE_EXPECTED_EGRESS_IP_C=170.205.39.88",
    "BINANCE_EXPECTED_EGRESS_IP_D=170.205.39.88",
]
payload = ("\n".join(lines) + "\n").encode("utf-8")
descriptor, temporary_name = tempfile.mkstemp(
    prefix=".env.v3.",
    dir=output_path.parent,
)
temporary_path = Path(temporary_name)
try:
    os.write(descriptor, payload)
    os.fsync(descriptor)
    os.close(descriptor)
    descriptor = -1
    os.chmod(temporary_path, 0o600)
    os.chown(temporary_path, 0, 0)
    os.replace(temporary_path, output_path)
    directory_descriptor = os.open(output_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
finally:
    if descriptor >= 0:
        os.close(descriptor)
    if temporary_path.exists():
        temporary_path.unlink()
PY
}

wait_for_postgres() {
  local attempt
  for attempt in $(seq 1 60); do
    if docker exec "$POSTGRES_CONTAINER" \
      pg_isready -U postgres -d trader >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  die "PostgreSQL did not become ready"
}

ensure_postgres() {
  docker pull "$POSTGRES_IMAGE"
  docker volume create "$POSTGRES_VOLUME" >/dev/null
  if ! docker inspect "$POSTGRES_CONTAINER" >/dev/null 2>&1; then
    docker run -d \
      --name "$POSTGRES_CONTAINER" \
      --restart unless-stopped \
      --label com.balen.trader-v3.role=postgres \
      -e POSTGRES_DB=trader \
      -e POSTGRES_USER=postgres \
      -e POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password \
      -v "$POSTGRES_PASSWORD_FILE:/run/secrets/postgres_password:ro" \
      -v "$POSTGRES_VOLUME:/var/lib/postgresql/data" \
      -p 127.0.0.1:5432:5432 \
      "$POSTGRES_IMAGE" \
      -c max_connections=100 >/dev/null
  fi
  [ "$(docker inspect --format '{{.State.Running}}' "$POSTGRES_CONTAINER")" = "true" ] \
    || docker start "$POSTGRES_CONTAINER" >/dev/null
  wait_for_postgres
}

wait_for_redis() {
  local attempt
  for attempt in $(seq 1 60); do
    if docker exec "$REDIS_CONTAINER" redis-cli PING 2>/dev/null \
      | grep -Fxq PONG; then
      return
    fi
    sleep 1
  done
  die "Redis did not become ready"
}

ensure_redis() {
  docker pull "$REDIS_IMAGE"
  docker volume create "$REDIS_VOLUME" >/dev/null
  if ! docker inspect "$REDIS_CONTAINER" >/dev/null 2>&1; then
    docker run -d \
      --name "$REDIS_CONTAINER" \
      --restart always \
      --label com.balen.trader-v3.role=redis \
      --memory "$REDIS_CONTAINER_MEMORY_BYTES" \
      --memory-swap "$REDIS_CONTAINER_MEMORY_BYTES" \
      --cpus 1.5 \
      --pids-limit 256 \
      --ulimit nofile=65536:65536 \
      -v "$REDIS_VOLUME:/data" \
      -p 127.0.0.1:6379:6379 \
      "$REDIS_IMAGE" \
      redis-server \
      --appendonly yes \
      --save "3600 1 300 100 60 10000" \
      --maxmemory "$REDIS_MAXMEMORY_BYTES" \
      --maxmemory-policy noeviction >/dev/null
  fi
  [ "$(docker inspect --format '{{.State.Running}}' "$REDIS_CONTAINER")" = "true" ] \
    || docker start "$REDIS_CONTAINER" >/dev/null
  wait_for_redis
  local epoch
  local current
  epoch="$(tr -d '[:space:]' < "$REDIS_FENCING_EPOCH_FILE")"
  current="$(
    docker exec "$REDIS_CONTAINER" redis-cli --raw GET \
      "$REDIS_FENCING_EPOCH_KEY"
  )"
  if [ -z "$current" ]; then
    docker exec "$REDIS_CONTAINER" redis-cli SET \
      "$REDIS_FENCING_EPOCH_KEY" "$epoch" NX >/dev/null
    current="$(
      docker exec "$REDIS_CONTAINER" redis-cli --raw GET \
        "$REDIS_FENCING_EPOCH_KEY"
    )"
  fi
  [ "$current" = "$epoch" ] || die "Redis fencing epoch differs from local secret"
}

ensure_networks() {
  local index
  local network
  local actual_subnet
  for index in "${!ACCOUNT_NETWORKS[@]}"; do
    network="${ACCOUNT_NETWORKS[$index]}"
    if ! docker network inspect "$network" >/dev/null 2>&1; then
      docker network create \
        --driver bridge \
        --subnet "${ACCOUNT_SUBNETS[$index]}" \
        --gateway "${ACCOUNT_GATEWAYS[$index]}" \
        --label com.balen.trader-v3.account="${ACCOUNT_LABELS[$index]}" \
        "$network" >/dev/null
    fi
    actual_subnet="$(
      docker network inspect "$network" \
        --format '{{(index .IPAM.Config 0).Subnet}}'
    )"
    [ "$actual_subnet" = "${ACCOUNT_SUBNETS[$index]}" ] \
      || die "Docker network subnet mismatch: $network"
  done
}

allow_account_network_control_plane() {
  local bridge
  local gateway
  local index
  local network
  local network_id
  ufw status | grep -Fxq 'Status: active' \
    || die "UFW must be active before account-network ingress is allowed"
  for index in "${!ACCOUNT_NETWORKS[@]}"; do
    network="${ACCOUNT_NETWORKS[$index]}"
    gateway="${ACCOUNT_GATEWAYS[$index]}"
    network_id="$(
      docker network inspect "$network" --format '{{.Id}}'
    )"
    [[ "$network_id" =~ ^[0-9a-f]{64}$ ]] \
      || die "Docker network ID is invalid: $network"
    bridge="$(
      docker network inspect "$network" \
        --format '{{with index .Options "com.docker.network.bridge.name"}}{{.}}{{end}}'
    )"
    if [ -z "$bridge" ]; then
      bridge="br-${network_id:0:12}"
    fi
    ip link show dev "$bridge" >/dev/null \
      || die "Docker network bridge is unavailable: $network"
    ufw allow in \
      on "$bridge" \
      to "$gateway" \
      port 8080 \
      proto tcp \
      comment "Trader v3 account-${ACCOUNT_LABELS[$index]} control"
  done
}

connect_redis_networks() {
  local attached
  local network
  for network in "${ACCOUNT_NETWORKS[@]}"; do
    attached="$(
      docker inspect "$REDIS_CONTAINER" \
        --format "{{if index .NetworkSettings.Networks \"$network\"}}yes{{end}}"
    )"
    if [ "$attached" != "yes" ]; then
      docker network connect \
        --alias trader-v3-redis \
        "$network" \
        "$REDIS_CONTAINER"
    fi
    attached="$(
      docker inspect "$REDIS_CONTAINER" \
        --format "{{if index .NetworkSettings.Networks \"$network\"}}yes{{end}}"
    )"
    [ "$attached" = "yes" ] \
      || die "Redis is not attached to account network: $network"
  done
}

write_egress_rules() {
  local temporary
  temporary="$(mktemp)"
  cat > "$temporary" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail

CHAIN=TRADER_V3_EGRESS
INTERFACE=$PRIMARY_INTERFACE
/usr/sbin/iptables -t nat -N "\$CHAIN" 2>/dev/null || true
/usr/sbin/iptables -t nat -F "\$CHAIN"
/usr/sbin/iptables -t nat -A "\$CHAIN" -s 172.30.1.0/24 -o "\$INTERFACE" -j SNAT --to-source 170.205.39.79
/usr/sbin/iptables -t nat -A "\$CHAIN" -s 172.30.2.0/24 -o "\$INTERFACE" -j SNAT --to-source 170.205.39.82
/usr/sbin/iptables -t nat -A "\$CHAIN" -s 172.30.3.0/24 -o "\$INTERFACE" -j SNAT --to-source 170.205.39.88
/usr/sbin/iptables -t nat -A "\$CHAIN" -s 172.30.4.0/24 -o "\$INTERFACE" -j SNAT --to-source 170.205.39.88
while /usr/sbin/iptables -t nat -C POSTROUTING -j "\$CHAIN" 2>/dev/null; do
  /usr/sbin/iptables -t nat -D POSTROUTING -j "\$CHAIN"
done
/usr/sbin/iptables -t nat -I POSTROUTING 1 -j "\$CHAIN"
EOF
  install -m 0755 "$temporary" "$EGRESS_SCRIPT"
  rm -f "$temporary"

  temporary="$(mktemp)"
  cat > "$temporary" <<EOF
[Unit]
Description=Trader v3 account-scoped IPv4 egress SNAT
After=network-online.target docker.service
Wants=network-online.target docker.service
Requires=docker.service
PartOf=docker.service

[Service]
Type=oneshot
ExecStart=$EGRESS_SCRIPT
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target docker.service
EOF
  install -m 0644 "$temporary" "$EGRESS_UNIT"
  rm -f "$temporary"
  systemctl daemon-reload
  systemctl enable trader-v3-egress-rules.service
  systemctl restart trader-v3-egress-rules.service
}

write_caddy_config() {
  local temporary
  temporary="$(mktemp /etc/caddy/.Caddyfile.jp24.XXXXXX)"
  cat > "$temporary" <<EOF
{
	admin 127.0.0.1:2019
	auto_https off
}

http://127.0.0.1:8080, http://$TAILSCALE_IP:8080, http://172.30.1.1:8080, http://172.30.2.1:8080, http://172.30.3.1:8080, http://172.30.4.1:8080 {
	bind 127.0.0.1 $TAILSCALE_IP 172.30.1.1 172.30.2.1 172.30.3.1 172.30.4.1

	@event_ingest path_regexp event_ingest ^/v1/nodes/[^/]+/(events|execution-events)$
	handle @event_ingest {
		reverse_proxy 127.0.0.1:8182
	}

	@node_control path_regexp node_control ^/v1/nodes/[^/]+/(heartbeat|incidents|commands(/[^/]+/ack)?|intents(/[^/]+/ack)?|exchange-state)$
	handle @node_control {
		reverse_proxy 127.0.0.1:8181
	}

	@account_generated path_regexp account_generated ^/v1/accounts/[^/]+$
	handle @account_generated {
		reverse_proxy 127.0.0.1:8181
	}

	handle {
		reverse_proxy 127.0.0.1:8183
	}
}
EOF
  caddy validate --adapter caddyfile --config "$temporary"
  install -m 0644 "$temporary" "$CADDY_FILE"
  rm -f "$temporary"
  systemctl enable caddy.service
  systemctl restart caddy.service
  systemctl is-active --quiet caddy.service
}

install_resource_contract() {
  local target="$TRADER_ROOT/infra/systemd/account-stall-account-node.conf"
  local temporary
  temporary="$(mktemp)"
  cat > "$temporary" <<'EOF'
[Service]
MemoryMax=640M
MemorySwapMax=0
CPUQuota=100%
TasksMax=512
LimitNOFILE=65536
Restart=always
RestartSec=5s
EOF
  install -m 0644 "$temporary" "$target"
  rm -f "$temporary"
}

verify_postgres() {
  local version
  local max_connections
  version="$(
    docker exec "$POSTGRES_CONTAINER" \
      psql -U postgres -d trader -Atc "SHOW server_version;"
  )"
  max_connections="$(
    docker exec "$POSTGRES_CONTAINER" \
      psql -U postgres -d trader -Atc "SHOW max_connections;"
  )"
  [[ "$version" =~ ^16\. ]] || die "PostgreSQL major version must be 16"
  [ "$max_connections" -ge 100 ] \
    || die "PostgreSQL max_connections must be at least 100"
  printf 'postgres_version=%s\n' "$version"
  printf 'postgres_max_connections=%s\n' "$max_connections"
}

verify_redis() {
  local maxmemory
  local policy
  local database_size
  maxmemory="$(
    docker exec "$REDIS_CONTAINER" redis-cli --raw CONFIG GET maxmemory \
      | tail -n 1
  )"
  policy="$(
    docker exec "$REDIS_CONTAINER" redis-cli --raw CONFIG GET maxmemory-policy \
      | tail -n 1
  )"
  database_size="$(
    docker exec "$REDIS_CONTAINER" redis-cli --raw DBSIZE
  )"
  [ "$maxmemory" = "$REDIS_MAXMEMORY_BYTES" ] \
    || die "Redis maxmemory mismatch"
  [ "$policy" = "noeviction" ] || die "Redis policy must be noeviction"
  [ "$database_size" = "1" ] \
    || die "fresh Redis must contain only the fencing epoch"
  printf 'redis_maxmemory=%s\n' "$maxmemory"
  printf 'redis_policy=%s\n' "$policy"
  printf 'redis_dbsize=%s\n' "$database_size"
  printf 'redis_fencing_epoch_sha256='
  sha256sum "$REDIS_FENCING_EPOCH_FILE" | awk '{print $1}'
}

verify_networks() {
  local index
  local actual_ip
  local network
  local expected_ip
  docker pull curlimages/curl:8.12.1 >/dev/null
  for index in "${!ACCOUNT_NETWORKS[@]}"; do
    network="${ACCOUNT_NETWORKS[$index]}"
    expected_ip="${ACCOUNT_EGRESS_IPS[$index]}"
    actual_ip="$(
      docker run --rm \
        --network "$network" \
        curlimages/curl:8.12.1 \
        --fail \
        --silent \
        --show-error \
        --max-time 20 \
        https://api.ipify.org
    )"
    [ "$actual_ip" = "$expected_ip" ] \
      || die "egress mismatch for account-${ACCOUNT_LABELS[$index]}"
    printf 'account-%s_egress=%s\n' \
      "${ACCOUNT_LABELS[$index]}" \
      "$actual_ip"
    docker run --rm \
      --network "$network" \
      curlimages/curl:8.12.1 \
      --fail \
      --silent \
      --show-error \
      --max-time 20 \
      http://"${ACCOUNT_GATEWAYS[$index]}":8080/health/role \
      >/dev/null
    docker run --rm \
      --network "$network" \
      "$REDIS_IMAGE" \
      redis-cli \
      -h trader-v3-redis \
      PING \
      | grep -Fxq PONG \
      || die "Redis is unavailable from account-${ACCOUNT_LABELS[$index]}"
    printf 'account-%s_internal_services=OK\n' \
      "${ACCOUNT_LABELS[$index]}"
  done
}

verify_filesystem() {
  findmnt -T "$STAGING_ROOT" -o TARGET,SOURCE,FSTYPE,OPTIONS
  local filesystem
  filesystem="$(findmnt -n -o FSTYPE --target "$STAGING_ROOT")"
  [ "$filesystem" != "tmpfs" ] || die "staging filesystem must not be tmpfs"
}

verify_pool_contract() {
  grep -Fxq 'CONTROL_PLANE_NODE_CONTROL_DB_POOL_SIZE=24' "$RUNTIME_ENV_FILE" \
    || die "node-control pool size contract missing"
  grep -Fxq \
    'CONTROL_PLANE_NODE_CONTROL_DB_CHECKOUT_TIMEOUT_SECONDS=2' \
    "$RUNTIME_ENV_FILE" \
    || die "node-control checkout timeout contract missing"
  echo "node_control_pool_size=24"
  echo "node_control_checkout_timeout_seconds=2"
}

verify_caddy() {
  systemctl is-active --quiet caddy.service \
    || die "Caddy must be active"
  caddy validate --adapter caddyfile --config "$CADDY_FILE"
  ss -lnt | awk \
    '$4 ~ /^(127\.0\.0\.1|100\.89\.58\.40|172\.30\.[1-4]\.1):8080$/'
}

verify_node_memory_contract() {
  grep -Fxq 'MemoryMax=640M' \
    "$TRADER_ROOT/infra/systemd/account-stall-account-node.conf" \
    || die "account node MemoryMax contract mismatch"
  echo "account_node_MemoryMax=640M"
}

run_checks() {
  require_halted
  verify_filesystem
  verify_postgres
  verify_redis
  verify_networks
  verify_pool_contract
  verify_caddy
  verify_node_memory_contract
  echo "JP24_P1_BASELINE_OK"
}

main() {
  require_root
  require_jp24
  case "$MODE" in
    apply)
      require_halted
      install_packages
      prepare_directories
      ensure_secret "$POSTGRES_PASSWORD_FILE" password
      ensure_secret "$REDIS_FENCING_EPOCH_FILE" uuid
      ensure_secret "$CONTROL_PLANE_NODE_TOKEN_FILE" password
      ensure_secret "$CONTROL_PLANE_RISK_ADMIN_TOKEN_FILE" password
      write_runtime_env
      ensure_postgres
      ensure_redis
      ensure_networks
      allow_account_network_control_plane
      connect_redis_networks
      write_egress_rules
      write_caddy_config
      install_resource_contract
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
