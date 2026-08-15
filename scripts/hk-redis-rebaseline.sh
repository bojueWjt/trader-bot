#!/usr/bin/env bash
# Rebaseline trader-v3 Redis onto a bounded empty volume.
# The source container, source volume, and a verified cold copy remain intact.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAPACITY_PLANNER="${REDIS_CAPACITY_PLANNER:-$SCRIPT_DIR/redis_capacity_config.py}"
TRADER_ROOT="${TRADER_ROOT:-/srv/trader-v3}"
REDIS_CONTAINER="${REDIS_CONTAINER:-trader-v3-redis}"
NODE_CONTAINERS=(
  trader-v3-node-a
  trader-v3-node-b
  trader-v3-node-c
  trader-v3-node-d
)
MAXMEMORY_BYTES="${REDIS_MAXMEMORY_BYTES:-536870912}"
CGROUP_LIMIT_BYTES=""
REDIS_CPU_LIMIT=""
REDIS_NANO_CPUS=""
REDIS_PIDS_LIMIT=""
REDIS_NOFILE_LIMIT=""
REDIS_RESTART_POLICY=""
DEFAULT_REDIS_RESOURCE_ARTIFACT="$SCRIPT_DIR/infra/systemd/account-stall-redis.conf"
if [ ! -f "$DEFAULT_REDIS_RESOURCE_ARTIFACT" ]; then
  DEFAULT_REDIS_RESOURCE_ARTIFACT="$SCRIPT_DIR/../infra/systemd/account-stall-redis.conf"
fi
REDIS_RESOURCE_ARTIFACT="${REDIS_RESOURCE_ARTIFACT:-$DEFAULT_REDIS_RESOURCE_ARTIFACT}"
SYSTEM_RESERVE_BYTES="${REDIS_SYSTEM_RESERVE_BYTES:-3221225472}"
OTHER_SERVICES_RESERVE_BYTES="${REDIS_OTHER_SERVICES_RESERVE_BYTES:-2415919104}"
HEADROOM_PERCENT="${REDIS_HEADROOM_PERCENT:-20}"
CONTAINER_HEADROOM_PERCENT="${REDIS_CONTAINER_HEADROOM_PERCENT:-20}"
NEW_SAVE_POLICY="${REDIS_SAVE_POLICY:-3600 1 300 100 60 10000}"
STREAM_MAX_ENTRIES="${REDIS_STREAM_MAX_ENTRIES:-100000}"
STREAM_MAX_BYTES="${REDIS_STREAM_MAX_BYTES:-67108864}"
TOTAL_STREAM_MAX_BYTES="${REDIS_TOTAL_STREAM_MAX_BYTES:-268435456}"
NAMESPACE_SCHEMA_EPOCH="${REDIS_NAMESPACE_SCHEMA_EPOCH:-fenced-generation-namespace/v2}"
ACCOUNT_A_STABLE_NAMESPACE="${REDIS_ACCOUNT_A_STABLE_NAMESPACE:-trader-TRADER-ACCOUNT-A}"
ACCOUNT_B_STABLE_NAMESPACE="${REDIS_ACCOUNT_B_STABLE_NAMESPACE:-trader-TRADER-ACCOUNT-B}"
ACCOUNT_C_STABLE_NAMESPACE="${REDIS_ACCOUNT_C_STABLE_NAMESPACE:-trader-TRADER-ACCOUNT-C}"
ACCOUNT_D_STABLE_NAMESPACE="${REDIS_ACCOUNT_D_STABLE_NAMESPACE:-trader-TRADER-ACCOUNT-D}"
MEMINFO_PATH="${REDIS_MEMINFO_PATH:-/proc/meminfo}"
CGROUP_ROOT="${REDIS_CGROUP_ROOT:-/sys/fs/cgroup}"
STAMP="${REDIS_REBASELINE_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
EVIDENCE_ROOT="$TRADER_ROOT/redis-rebaseline/$STAMP"
BACKUP_ROOT="$EVIDENCE_ROOT/cold-backup"
VALIDATOR_ROOT="$EVIDENCE_ROOT/validators"
BACKUP_MANIFEST="$EVIDENCE_ROOT/cold-backup-manifest.json"
CAPACITY_PLAN="$EVIDENCE_ROOT/capacity-plan.json"
CAPACITY_EVIDENCE="$EVIDENCE_ROOT/capacity-evidence.json"
SOURCE_NETWORK_ATTACHMENTS="$EVIDENCE_ROOT/source-network-attachments.json"
ROLLBACK_SCRIPT="$EVIDENCE_ROOT/rollback-redis.sh"
LEGACY_CONTAINER="${REDIS_CONTAINER}-legacy-${STAMP}"
NEW_VOLUME="${REDIS_CONTAINER}-hardening-${STAMP}"
DEFAULT_OPERATION_LOCK="/var/lock/trader-v3-account-stall-operation.lock"
LOCK="${ACCOUNT_STALL_OPERATION_LOCK:-${REDIS_REBASELINE_LOCK:-$DEFAULT_OPERATION_LOCK}}"
REBASELINE_LABEL="trader-v3.redis-rebaseline=$STAMP"
REDIS_FENCING_EPOCH_KEY="trader-bot:redis-fencing-epoch"
PHASE="preflight"
PHASE_FILE="$EVIDENCE_ROOT/phase"
SOURCE_CONTAINER_ID=""
SOURCE_WAS_RUNNING=0
MUTATION_STARTED=0
NEW_CONTAINER_ID=""
REBASELINE_COMPLETE=0
RECOVERY_ACTIVE=0

die() {
  echo "FATAL: $*" >&2
  return 1
}

require_root() {
  [ "${EUID:-$(id -u)}" -eq 0 ] || die "run as root on hk"
}

set_phase() {
  PHASE="$1"
  if [ -d "$EVIDENCE_ROOT" ]; then
    printf '%s\n' "$PHASE" > "$PHASE_FILE"
    chmod 0600 "$PHASE_FILE"
  fi
}

inspect_value() {
  local format="$1"
  local container="$2"
  docker inspect --format "$format" "$container"
}

capture_source_network_attachments() {
  local source_networks
  source_networks="$(
    inspect_value '{{json .NetworkSettings.Networks}}' "$REDIS_CONTAINER"
  )"
  python3 - "$SOURCE_NETWORK_ATTACHMENTS" "$source_networks" <<'PY'
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

output = Path(sys.argv[1])
networks = json.loads(sys.argv[2])
if not isinstance(networks, dict):
    raise SystemExit("source Redis networks must be an object")

safe_name = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
attachments = []
for network_name, network in sorted(networks.items()):
    if network_name == "bridge":
        continue
    if not isinstance(network_name, str) or safe_name.fullmatch(network_name) is None:
        raise SystemExit("source Redis network name is unsafe")
    if not isinstance(network, dict):
        raise SystemExit(f"source Redis network is invalid: {network_name}")
    raw_aliases = network.get("Aliases") or []
    if not isinstance(raw_aliases, list):
        raise SystemExit(f"source Redis aliases are invalid: {network_name}")
    aliases = []
    for alias in raw_aliases:
        if not isinstance(alias, str) or safe_name.fullmatch(alias) is None:
            raise SystemExit(f"source Redis alias is unsafe: {network_name}")
        if alias not in aliases:
            aliases.append(alias)
    attachments.append(
        {
            "network": network_name,
            "aliases": aliases,
        }
    )

output.write_text(
    json.dumps(attachments, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
  chmod 0600 "$SOURCE_NETWORK_ATTACHMENTS"
}

restore_source_network_attachments() {
  local alias
  local aliases
  local network
  local -a network_args
  local -a source_aliases
  while IFS=$'\t' read -r network aliases; do
    [ -n "$network" ] || continue
    network_args=(docker network connect)
    if [ -n "$aliases" ]; then
      IFS=',' read -r -a source_aliases <<< "$aliases"
      for alias in "${source_aliases[@]}"; do
        network_args+=(--alias "$alias")
      done
    fi
    network_args+=("$network" "$REDIS_CONTAINER")
    "${network_args[@]}"
  done < <(
    python3 - "$SOURCE_NETWORK_ATTACHMENTS" <<'PY'
from __future__ import annotations

import json
import sys

attachments = json.load(open(sys.argv[1], encoding="utf-8"))
for attachment in attachments:
    print(
        f"{attachment['network']}\t"
        f"{','.join(attachment['aliases'])}"
    )
PY
  )
}

verify_source_network_attachments() {
  local active_networks
  active_networks="$(
    inspect_value '{{json .NetworkSettings.Networks}}' "$REDIS_CONTAINER"
  )"
  python3 - "$SOURCE_NETWORK_ATTACHMENTS" "$active_networks" <<'PY'
from __future__ import annotations

import json
import sys

expected = json.load(open(sys.argv[1], encoding="utf-8"))
active = json.loads(sys.argv[2])
if not isinstance(active, dict):
    raise SystemExit("active Redis networks must be an object")

for attachment in expected:
    network_name = attachment["network"]
    network = active.get(network_name)
    if not isinstance(network, dict):
        raise SystemExit(f"active Redis network is missing: {network_name}")
    aliases = network.get("Aliases") or []
    if not isinstance(aliases, list):
        raise SystemExit(f"active Redis aliases are invalid: {network_name}")
    missing = sorted(set(attachment["aliases"]) - set(aliases))
    if missing:
        raise SystemExit(
            f"active Redis aliases are missing for {network_name}: "
            f"{','.join(missing)}"
        )
PY
}

container_id_or_empty() {
  local container="$1"
  docker inspect --format '{{.Id}}' "$container" 2>/dev/null || true
}

container_label_or_empty() {
  local container="$1"
  docker inspect \
    --format '{{index .Config.Labels "trader-v3.redis-rebaseline"}}' \
    "$container" 2>/dev/null || true
}

redis_info_value() {
  local container="$1"
  local section="$2"
  local key="$3"
  docker exec "$container" redis-cli --raw INFO "$section" \
    | sed -n "s/^${key}://p" \
    | tr -d '\r'
}

redis_config_value() {
  local container="$1"
  local key="$2"
  docker exec "$container" redis-cli --raw CONFIG GET "$key" \
    | tail -1 \
    | tr -d '\r'
}

generate_redis_fencing_epoch() {
  python3 - <<'PY'
from uuid import uuid4

print(uuid4())
PY
}

persist_redis_fencing_epoch() {
  local container="$1"
  local epoch="$2"
  local persisted_epoch
  local changes_after_save
  [[ "$epoch" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] \
    || die "Redis fencing epoch is not a canonical UUID4"
  [ "$(
    docker exec "$container" redis-cli SET \
      "$REDIS_FENCING_EPOCH_KEY" "$epoch" \
      | tr -d '\r'
  )" = "OK" ] || die "Redis fencing epoch marker write failed"
  docker exec "$container" redis-cli SAVE >/dev/null \
    || die "Redis fencing epoch synchronous SAVE failed"
  changes_after_save="$(
    redis_info_value \
      "$container" \
      persistence \
      rdb_changes_since_last_save
  )"
  [ "$changes_after_save" = "0" ] \
    || die "Redis fencing epoch changed after synchronous SAVE"
  persisted_epoch="$(
    docker exec "$container" redis-cli --raw GET \
      "$REDIS_FENCING_EPOCH_KEY" \
      | tr -d '\r'
  )"
  [ "$persisted_epoch" = "$epoch" ] \
    || die "Redis fencing epoch marker verification failed"
}

assert_positive_integer() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$label is not an integer"
  [ "$value" -gt 0 ] || die "$label must be positive"
}

assert_nonnegative_integer() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$label is not a nonnegative integer"
}

systemd_resource_value() {
  local key="$1"
  local matches
  matches="$(
    sed -n "s/^${key}=//p" "$REDIS_RESOURCE_ARTIFACT"
  )"
  [ "$(printf '%s\n' "$matches" | sed '/^$/d' | wc -l | tr -d ' ')" = "1" ] \
    || die "Redis resource contract must define $key exactly once"
  printf '%s\n' "$matches"
}

systemd_memory_bytes() {
  local value="$1"
  case "$value" in
    *K)
      printf '%s\n' "$(( ${value%K} * 1024 ))"
      ;;
    *M)
      printf '%s\n' "$(( ${value%M} * 1024 * 1024 ))"
      ;;
    *G)
      printf '%s\n' "$(( ${value%G} * 1024 * 1024 * 1024 ))"
      ;;
    *)
      die "Redis MemoryMax is invalid: $value"
      ;;
  esac
}

load_redis_resource_contract() {
  local memory_max
  local memory_swap_max
  local cpu_quota
  local cpu_quota_percent
  local restart_sec
  [ -f "$REDIS_RESOURCE_ARTIFACT" ] \
    || die "Redis resource artifact missing: $REDIS_RESOURCE_ARTIFACT"
  [ "$(sed -n '/^\[Service\]$/p' "$REDIS_RESOURCE_ARTIFACT" | wc -l | tr -d ' ')" = "1" ] \
    || die "Redis resource artifact must contain one Service section"
  memory_max="$(systemd_resource_value MemoryMax)"
  memory_swap_max="$(systemd_resource_value MemorySwapMax)"
  cpu_quota="$(systemd_resource_value CPUQuota)"
  REDIS_PIDS_LIMIT="$(systemd_resource_value TasksMax)"
  REDIS_NOFILE_LIMIT="$(systemd_resource_value LimitNOFILE)"
  REDIS_RESTART_POLICY="$(systemd_resource_value Restart)"
  restart_sec="$(systemd_resource_value RestartSec)"
  [ "$memory_swap_max" = "0" ] \
    || die "Redis resource contract requires MemorySwapMax=0"
  [[ "$cpu_quota" =~ ^[1-9][0-9]*%$ ]] \
    || die "Redis CPUQuota is invalid"
  cpu_quota_percent="${cpu_quota%\%}"
  [[ "$restart_sec" =~ ^[1-9][0-9]*s$ ]] \
    || die "Redis RestartSec is invalid"
  [ "$REDIS_RESTART_POLICY" = "always" ] \
    || die "Redis resource contract requires Restart=always"
  assert_positive_integer "Redis TasksMax" "$REDIS_PIDS_LIMIT"
  assert_positive_integer "Redis LimitNOFILE" "$REDIS_NOFILE_LIMIT"
  CGROUP_LIMIT_BYTES="$(systemd_memory_bytes "$memory_max")"
  REDIS_CPU_LIMIT="$(
    python3 - "$cpu_quota_percent" <<'PY'
from decimal import Decimal
import sys

print(Decimal(sys.argv[1]) / Decimal(100))
PY
  )"
  REDIS_NANO_CPUS="$(( cpu_quota_percent * 10000000 ))"
}

assert_node_stopped() {
  local node="$1"
  local running
  local pid
  local container_id
  local process_file
  running="$(inspect_value '{{.State.Running}}' "$node")"
  pid="$(inspect_value '{{.State.Pid}}' "$node")"
  container_id="$(inspect_value '{{.Id}}' "$node")"
  [ "$running" = "false" ] || die "$node remained running"
  [ "$pid" = "0" ] || die "$node retained writer PID $pid"
  if docker top "$node" >/dev/null 2>&1; then
    die "$node still exposes writer processes"
  fi
  if [ ! -d "$CGROUP_ROOT" ]; then
    return 0
  fi
  while IFS= read -r process_file; do
    if grep -Eq '[0-9]' "$process_file"; then
      die "$node retained processes in $process_file"
    fi
  done < <(
    find "$CGROUP_ROOT" \
      -type f \
      \( -name cgroup.procs -o -name tasks \) \
      -path "*${container_id}*" \
      -print 2>/dev/null
  )
}

stop_execution_nodes() {
  local node
  local running
  for node in "${NODE_CONTAINERS[@]}"; do
    running="$(inspect_value '{{.State.Running}}' "$node")"
    if [ "$running" = "true" ]; then
      docker stop --time 30 "$node" >/dev/null
    fi
    assert_node_stopped "$node"
  done
}

stop_execution_nodes_for_recovery() {
  local node
  local running
  for node in "${NODE_CONTAINERS[@]}"; do
    running="$(inspect_value '{{.State.Running}}' "$node" 2>/dev/null || true)"
    if [ "$running" = "true" ]; then
      docker stop --time 30 "$node" >/dev/null 2>&1 || true
    fi
    assert_node_stopped "$node" >/dev/null 2>&1 \
      || echo "ERROR: recovery could not prove $node stopped" >&2
  done
}

volume_label_or_empty() {
  local volume="$1"
  docker volume inspect \
    --format '{{index .Labels "trader-v3.redis-rebaseline"}}' \
    "$volume" 2>/dev/null || true
}

recover_source_redis() {
  local original_status="$1"
  local active_id
  local active_label
  local legacy_id
  local source_id_after
  local volume_label
  if [ "$RECOVERY_ACTIVE" = "1" ]; then
    return "$original_status"
  fi
  RECOVERY_ACTIVE=1
  trap - ERR INT TERM
  set +e
  echo "ERROR: Redis rebaseline failed in phase=$PHASE; restoring source Redis" >&2

  if [ "$MUTATION_STARTED" = "1" ]; then
    stop_execution_nodes_for_recovery
  fi
  if [ -z "$SOURCE_CONTAINER_ID" ]; then
    echo "ERROR: source Redis identity was not captured; Redis was untouched" >&2
    return "$original_status"
  fi

  active_id="$(container_id_or_empty "$REDIS_CONTAINER")"
  legacy_id="$(container_id_or_empty "$LEGACY_CONTAINER")"
  if [ -n "$active_id" ] && [ "$active_id" != "$SOURCE_CONTAINER_ID" ]; then
    active_label="$(container_label_or_empty "$REDIS_CONTAINER")"
    if [ "$active_label" = "$STAMP" ]; then
      docker stop --time 30 "$REDIS_CONTAINER" >/dev/null 2>&1 || true
      docker rm -f "$REDIS_CONTAINER" >/dev/null 2>&1 || true
      active_id=""
    else
      echo "ERROR: refusing to remove unowned container $REDIS_CONTAINER" >&2
    fi
  fi

  active_id="$(container_id_or_empty "$REDIS_CONTAINER")"
  legacy_id="$(container_id_or_empty "$LEGACY_CONTAINER")"
  if [ "$active_id" = "$SOURCE_CONTAINER_ID" ]; then
    :
  elif [ "$legacy_id" = "$SOURCE_CONTAINER_ID" ] && [ -z "$active_id" ]; then
    docker rename "$LEGACY_CONTAINER" "$REDIS_CONTAINER" >/dev/null 2>&1 || true
  else
    echo "ERROR: source Redis identity is not recoverable under expected names" >&2
  fi

  source_id_after="$(container_id_or_empty "$REDIS_CONTAINER")"
  if [ "$source_id_after" = "$SOURCE_CONTAINER_ID" ] \
    && [ "$SOURCE_WAS_RUNNING" = "1" ]; then
    docker start "$REDIS_CONTAINER" >/dev/null 2>&1 || true
  fi

  volume_label="$(volume_label_or_empty "$NEW_VOLUME")"
  if [ "$volume_label" = "$STAMP" ]; then
    docker volume rm "$NEW_VOLUME" >/dev/null 2>&1 || true
  fi
  echo "ERROR: execution nodes remain stopped" >&2
  return "$original_status"
}

on_err() {
  local status=$?
  if [ "$REBASELINE_COMPLETE" = "1" ]; then
    exit "$status"
  fi
  recover_source_redis "$status"
  exit "$status"
}

on_signal() {
  local status="$1"
  local signal_name="$2"
  echo "ERROR: Redis rebaseline interrupted by $signal_name" >&2
  recover_source_redis "$status"
  exit "$status"
}

run_redis_checker() {
  local checker="$1"
  local relative_path="$2"
  local canonical_after
  local canonical_before
  local canonical_path
  local safe_name="${relative_path//\//_}"
  local report="$VALIDATOR_ROOT/${checker}-${safe_name}.txt"
  local validation_dir
  local validation_name
  if [ "$checker" = "redis-check-aof" ]; then
    canonical_path="$BACKUP_ROOT/$relative_path"
    canonical_before="$(
      sha256sum "$canonical_path" | awk '{print $1}'
    )"
    validation_dir="$VALIDATOR_ROOT/aof-work-$safe_name"
    validation_name="$(basename "$relative_path")"
    mkdir -m 0700 "$validation_dir"
    cp -- "$canonical_path" "$validation_dir/$validation_name"
    chmod 0600 "$validation_dir/$validation_name"
    docker run --rm \
      --network none \
      --entrypoint "$checker" \
      -v "$validation_dir:/backup:rw" \
      "$OLD_IMAGE" \
      "/backup/$validation_name" > "$report" 2>&1
    canonical_after="$(
      sha256sum "$canonical_path" | awk '{print $1}'
    )"
    [ "$canonical_after" = "$canonical_before" ] \
      || die "redis-check-aof changed the canonical cold backup"
    printf 'canonical_sha256_before=%s\n' "$canonical_before" >> "$report"
    printf 'canonical_sha256_after=%s\n' "$canonical_after" >> "$report"
    rm -f "$validation_dir/$validation_name"
    rmdir "$validation_dir"
    [ -s "$report" ] || die "$checker returned no validation evidence"
    return
  fi
  docker run --rm \
    --network none \
    --entrypoint "$checker" \
    -v "$BACKUP_ROOT/data:/backup:ro" \
    "$OLD_IMAGE" \
    "/backup/${relative_path#data/}" > "$report" 2>&1
  [ -s "$report" ] || die "$checker returned no validation evidence"
}

validate_aof_manifest() {
  local relative_path="$1"
  local safe_name="${relative_path//\//_}"
  local report="$VALIDATOR_ROOT/redis-aof-manifest-parser-${safe_name}.txt"
  python3 - "$BACKUP_ROOT/$relative_path" > "$report" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
pattern = re.compile(r"^file (?P<filename>[^/\s]+) seq [0-9]+ type [bih]$")
entries = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
entries = [line for line in entries if line]
if not entries:
    raise SystemExit("AOF manifest is empty")
for entry in entries:
    match = pattern.fullmatch(entry)
    if match is None:
        raise SystemExit(f"invalid AOF manifest entry: {entry}")
    artifact = path.parent / match.group("filename")
    if not artifact.is_file():
        raise SystemExit(f"AOF manifest references missing file: {artifact.name}")
print(f"validated_entries={len(entries)}")
PY
  [ -s "$report" ] || die "AOF manifest parser returned no evidence"
}

write_backup_manifest() {
  python3 - \
    "$BACKUP_MANIFEST" \
    "$EVIDENCE_ROOT" \
    "$BACKUP_ROOT" \
    "$VALIDATOR_ROOT" \
    "$OLD_IMAGE" \
    "$OLD_CONFIG_IMAGE" \
    "$SOURCE_CONTAINER_ID" \
    "$OLD_RUN_ID" \
    "$OLD_KEY_COUNT" \
    "$OLD_USED_MEMORY" \
    "$OLD_DATASET_MEMORY" \
    "$OLD_AOF_ENABLED" \
    "$OLD_DATA_ROOT" \
    "$OLD_VOLUME_NAME" \
    "$OLD_REDIS_DIR" \
    "$OLD_RDB_FILENAME" \
    "$OLD_SAVE_POLICY" \
    "$OLD_APPENDONLY" \
    "$OLD_APPENDFILENAME" \
    "$OLD_APPENDDIRNAME" <<'PY'
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

(
    output_raw,
    evidence_root_raw,
    backup_root_raw,
    validator_root_raw,
    image_digest,
    config_image,
    source_container_id,
    run_id,
    key_count,
    used_memory,
    dataset_memory,
    aof_enabled,
    source_data_root,
    source_volume_name,
    redis_dir,
    rdb_filename,
    save_policy,
    appendonly,
    appendfilename,
    appenddirname,
) = sys.argv[1:]
output = Path(output_raw)
evidence_root = Path(evidence_root_raw)
backup_root = Path(backup_root_raw)
validator_root = Path(validator_root_raw)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


files = []
for path in sorted((backup_root / "data").rglob("*")):
    if path.is_symlink():
        raise SystemExit(f"cold backup contains symlink: {path}")
    if not path.is_file():
        continue
    files.append(
        {
            "path": str(path.relative_to(backup_root)),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    )
if not files:
    raise SystemExit("cold backup contains no Redis data files")
file_map = {entry["path"]: entry for entry in files}

artifacts = []
for relative_path, entry in file_map.items():
    path = backup_root / relative_path
    kind = ""
    validator = ""
    if relative_path == f"data/{rdb_filename}":
        kind = "rdb"
        validator = "redis-check-rdb"
    elif path.suffix.lower() == ".aof":
        kind = "aof"
        validator = "redis-check-aof"
    elif path.name == f"{appendfilename}.manifest":
        kind = "aof-manifest"
        validator = "redis-aof-manifest-parser"
    if not kind:
        continue
    safe_name = relative_path.replace("/", "_")
    report = validator_root / f"{validator}-{safe_name}.txt"
    if not report.is_file() or report.stat().st_size == 0:
        raise SystemExit(f"validator report missing: {report}")
    artifacts.append(
        {
            "kind": kind,
            "path": relative_path,
            "sha256": entry["sha256"],
            "validator": validator,
            "validation_passed": True,
            "validator_output_path": str(report.relative_to(evidence_root)),
            "validator_output_sha256": sha256_file(report),
        }
    )
completed_at = datetime.now(timezone.utc)
payload = {
    "schema_version": "trader-v3-redis-cold-backup/v2",
    "created_at": completed_at.isoformat(),
    "completed_at_epoch": int(completed_at.timestamp()),
    "mode": "cold",
    "source_container": "trader-v3-redis",
    "source_container_id": source_container_id,
    "source_image_digest": image_digest,
    "source_config_image": config_image,
    "source_redis_run_id": run_id,
    "source_run_id": run_id,
    "source_key_count": int(key_count),
    "source_used_memory_bytes": int(used_memory),
    "source_dataset_memory_bytes": int(dataset_memory),
    "source_aof_enabled": int(aof_enabled),
    "source_appendonly": appendonly,
    "source_appendfilename": appendfilename,
    "source_appenddirname": appenddirname,
    "source_save_policy": save_policy,
    "source_redis_dir": redis_dir,
    "source_rdb_filename": rdb_filename,
    "source_mount_type": "volume",
    "source_volume_name": source_volume_name,
    "source_data_root": source_data_root,
    "nodes_stopped": True,
    "rdb_changes_since_last_save": 0,
    "backup_root": str(backup_root),
    "artifacts": artifacts,
    "files": files,
}
output.write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

write_capacity_evidence() {
  local backup_manifest_sha256="$1"
  python3 - \
    "$CAPACITY_PLAN" \
    "$CAPACITY_EVIDENCE" \
    "$BACKUP_MANIFEST" \
    "$backup_manifest_sha256" \
    "$NEW_CONTAINER_ID" \
    "$NEW_RUN_ID" \
    "$NEW_VOLUME" \
    "$ACTIVE_VOLUME_SOURCE" \
    "$ACTUAL_MEMORY" \
    "$ACTUAL_MEMORY_SWAP" \
    "$NEW_KEY_COUNT" \
    "$NEW_USED_MEMORY" \
    "$NEW_DATASET_MEMORY" \
    "$ACTUAL_APPENDONLY" \
    "$ACTUAL_AOF_ENABLED" \
    "$ACTUAL_SAVE_POLICY" \
    "$ACTUAL_RDB_STATUS" \
    "$HOST_TOTAL_BYTES" \
    "$HOST_AVAILABLE_BYTES" \
    "$LEGACY_CONTAINER" \
    "$SOURCE_CONTAINER_ID" \
    "$OLD_RUN_ID" \
    "$REDIS_FENCING_EPOCH" \
    "$REDIS_FENCING_EPOCH_KEY" \
    "$REDIS_FENCING_EPOCH_SHA256" \
    "$STREAM_MAX_ENTRIES" \
    "$STREAM_MAX_BYTES" \
    "$TOTAL_STREAM_MAX_BYTES" \
    "$NAMESPACE_SCHEMA_EPOCH" \
    "$ACCOUNT_A_STABLE_NAMESPACE" \
    "$ACCOUNT_B_STABLE_NAMESPACE" \
    "$ACCOUNT_C_STABLE_NAMESPACE" \
    "$ACCOUNT_D_STABLE_NAMESPACE" <<'PY'
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

(
    plan_raw,
    output_raw,
    backup_manifest,
    backup_manifest_sha256,
    active_container_id,
    active_run_id,
    active_volume,
    active_volume_source,
    memory_limit,
    memory_swap_limit,
    key_count,
    used_memory,
    dataset_memory,
    appendonly,
    aof_enabled,
    save_policy,
    rdb_status,
    host_total,
    host_available,
    legacy_container,
    source_container_id,
    source_run_id,
    redis_fencing_epoch,
    redis_fencing_epoch_key,
    redis_fencing_epoch_sha256,
    stream_max_entries,
    stream_max_bytes,
    total_stream_max_bytes,
    namespace_schema_epoch,
    account_a_stable_namespace,
    account_b_stable_namespace,
    account_c_stable_namespace,
    account_d_stable_namespace,
) = sys.argv[1:]
plan = json.loads(Path(plan_raw).read_text(encoding="utf-8"))
stable_namespaces = {
    "account-a": account_a_stable_namespace,
    "account-b": account_b_stable_namespace,
    "account-c": account_c_stable_namespace,
    "account-d": account_d_stable_namespace,
}
stream_retention = {
    "stream_max_entries": int(stream_max_entries),
    "stream_max_bytes": int(stream_max_bytes),
    "total_stream_max_bytes": int(total_stream_max_bytes),
}
checks = {
    "active_volume_matches": True,
    "aof_disabled": appendonly == "no" and int(aof_enabled) == 0,
    "active_marker_only": int(key_count) == 1,
    "epoch_marker_persisted": True,
    "memory_limit_matches": int(memory_limit) == plan["redis_cgroup_limit_bytes"],
    "memory_swap_is_finite": (
        int(memory_swap_limit) == plan["redis_cgroup_limit_bytes"]
    ),
    "maxmemory_is_explicit": plan["maxmemory_bytes"] > 0,
    "maxmemory_policy_noeviction": plan["maxmemory_policy"] == "noeviction",
    "namespace_schema_epoch_bound": (
        namespace_schema_epoch == "fenced-generation-namespace/v2"
    ),
    "nodes_stopped": True,
    "rdb_status_ok": rdb_status == "ok",
    "save_policy_configured": bool(save_policy.strip()),
    "source_container_preserved": True,
    "source_run_id_changed": active_run_id != source_run_id,
    "stable_namespaces_bound": stable_namespaces
    == {
        "account-a": "trader-TRADER-ACCOUNT-A",
        "account-b": "trader-TRADER-ACCOUNT-B",
        "account-c": "trader-TRADER-ACCOUNT-C",
        "account-d": "trader-TRADER-ACCOUNT-D",
    },
    "stream_retention_configured": (
        stream_retention["stream_max_entries"] > 0
        and stream_retention["stream_max_bytes"] > 0
        and stream_retention["total_stream_max_bytes"]
        >= stream_retention["stream_max_bytes"]
    ),
    "swap_disabled": int(memory_swap_limit) == int(memory_limit),
}
if not all(checks.values()):
    raise SystemExit("runtime capacity checks did not all pass")
payload = dict(plan)
payload.update(
    {
        "schema_version": "trader-v3-redis-capacity-evidence/v3",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "passed": True,
        "dataset_mode": "empty-volume-exchange-first-rebaseline",
        "active_container": "trader-v3-redis",
        "active_container_id": active_container_id,
        "active_redis_run_id": active_run_id,
        "initial_redis_run_id": active_run_id,
        "active_volume": active_volume,
        "active_volume_source": active_volume_source,
        "redis_fencing_epoch": redis_fencing_epoch,
        "redis_fencing_epoch_key": redis_fencing_epoch_key,
        "redis_fencing_epoch_sha256": redis_fencing_epoch_sha256,
        "runtime_resource_policy": {
            "schema_version": "trader-v3-runtime-resources/v1",
            "namespace_schema_epoch": namespace_schema_epoch,
            "stable_namespaces": stable_namespaces,
            "stream_retention": stream_retention,
        },
        "control_keys_reinitialized": True,
        "memory_limit_bytes": int(memory_limit),
        "memory_swap_limit_bytes": int(memory_swap_limit),
        "active_key_count": int(key_count),
        "active_used_memory_bytes": int(used_memory),
        "active_dataset_memory_bytes": int(dataset_memory),
        "active_appendonly": appendonly,
        "active_aof_enabled": int(aof_enabled),
        "active_save_policy": save_policy,
        "active_rdb_last_bgsave_status": rdb_status,
        "host_total_memory_bytes": int(host_total),
        "host_available_memory_bytes": int(host_available),
        "source_backup_manifest": backup_manifest,
        "source_backup_manifest_sha256": backup_manifest_sha256,
        "source_container_id": source_container_id,
        "source_redis_run_id": source_run_id,
        "legacy_container": legacy_container,
        "nodes_stopped": True,
        "runtime_checks": checks,
    }
)
Path(output_raw).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

verify_capacity_evidence() {
  python3 - \
    "$CAPACITY_EVIDENCE" \
    "$BACKUP_MANIFEST" \
    "$NEW_CONTAINER_ID" \
    "$NEW_RUN_ID" \
    "$NEW_VOLUME" \
    "$ACTIVE_VOLUME_SOURCE" \
    "$ACTUAL_MAXMEMORY" \
    "$ACTUAL_MEMORY" \
    "$ACTUAL_MEMORY_SWAP" \
    "$HOST_TOTAL_BYTES" \
    "$HOST_AVAILABLE_BYTES" \
    "$REDIS_FENCING_EPOCH" \
    "$REDIS_FENCING_EPOCH_KEY" \
    "$REDIS_FENCING_EPOCH_SHA256" \
    "$STREAM_MAX_ENTRIES" \
    "$STREAM_MAX_BYTES" \
    "$TOTAL_STREAM_MAX_BYTES" \
    "$NAMESPACE_SCHEMA_EPOCH" \
    "$ACCOUNT_A_STABLE_NAMESPACE" \
    "$ACCOUNT_B_STABLE_NAMESPACE" \
    "$ACCOUNT_C_STABLE_NAMESPACE" \
    "$ACCOUNT_D_STABLE_NAMESPACE" <<'PY'
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

(
    evidence_raw,
    backup_manifest_raw,
    container_id,
    run_id,
    volume,
    volume_source,
    maxmemory,
    memory_limit,
    memory_swap,
    host_total,
    host_available,
    redis_fencing_epoch,
    redis_fencing_epoch_key,
    redis_fencing_epoch_sha256,
    stream_max_entries,
    stream_max_bytes,
    total_stream_max_bytes,
    namespace_schema_epoch,
    account_a_stable_namespace,
    account_b_stable_namespace,
    account_c_stable_namespace,
    account_d_stable_namespace,
) = sys.argv[1:]
evidence = json.loads(Path(evidence_raw).read_text(encoding="utf-8"))
backup_manifest = Path(backup_manifest_raw)
digest = hashlib.sha256(backup_manifest.read_bytes()).hexdigest()
if evidence.get("schema_version") != "trader-v3-redis-capacity-evidence/v3":
    raise SystemExit("capacity evidence schema mismatch")
if evidence.get("passed") is not True:
    raise SystemExit("capacity evidence did not pass")
if not re.fullmatch(r"[0-9a-f]{64}", digest):
    raise SystemExit("capacity evidence backup hash is invalid")
expected = {
    "active_container_id": container_id,
    "active_redis_run_id": run_id,
    "initial_redis_run_id": run_id,
    "active_volume": volume,
    "active_volume_source": volume_source,
    "active_key_count": 1,
    "redis_fencing_epoch": redis_fencing_epoch,
    "redis_fencing_epoch_key": redis_fencing_epoch_key,
    "redis_fencing_epoch_sha256": redis_fencing_epoch_sha256,
    "control_keys_reinitialized": True,
    "maxmemory_bytes": int(maxmemory),
    "redis_cgroup_limit_bytes": int(memory_limit),
    "memory_limit_bytes": int(memory_limit),
    "memory_swap_limit_bytes": int(memory_swap),
    "host_total_memory_bytes": int(host_total),
    "host_available_memory_bytes": int(host_available),
    "source_backup_manifest_sha256": digest,
    "runtime_resource_policy": {
        "schema_version": "trader-v3-runtime-resources/v1",
        "namespace_schema_epoch": namespace_schema_epoch,
        "stable_namespaces": {
            "account-a": account_a_stable_namespace,
            "account-b": account_b_stable_namespace,
            "account-c": account_c_stable_namespace,
            "account-d": account_d_stable_namespace,
        },
        "stream_retention": {
            "stream_max_entries": int(stream_max_entries),
            "stream_max_bytes": int(stream_max_bytes),
            "total_stream_max_bytes": int(total_stream_max_bytes),
        },
    },
}
for key, expected_value in expected.items():
    if evidence.get(key) != expected_value:
        raise SystemExit(f"capacity evidence mismatch: {key}")
if re.fullmatch(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    redis_fencing_epoch,
) is None:
    raise SystemExit("capacity evidence Redis fencing epoch is invalid")
actual_epoch_hash = hashlib.sha256(
    redis_fencing_epoch.encode("ascii")
).hexdigest()
if actual_epoch_hash != redis_fencing_epoch_sha256:
    raise SystemExit("capacity evidence Redis fencing epoch hash mismatch")
checks = evidence.get("runtime_checks")
if not isinstance(checks, dict) or not checks or not all(checks.values()):
    raise SystemExit("capacity runtime checks are incomplete")
PY
}

write_rollback_script() {
  local q_redis
  local q_node_a
  local q_node_b
  local q_node_c
  local q_node_d
  local q_legacy
  local q_new_id
  local q_source_id
  local q_source_volume
  local q_source_data_root
  local q_stamp
  local q_lock
  local q_cgroup_root
  local q_epoch_key
  printf -v q_redis '%q' "$REDIS_CONTAINER"
  printf -v q_node_a '%q' "${NODE_CONTAINERS[0]}"
  printf -v q_node_b '%q' "${NODE_CONTAINERS[1]}"
  printf -v q_node_c '%q' "${NODE_CONTAINERS[2]}"
  printf -v q_node_d '%q' "${NODE_CONTAINERS[3]}"
  printf -v q_legacy '%q' "$LEGACY_CONTAINER"
  printf -v q_new_id '%q' "$NEW_CONTAINER_ID"
  printf -v q_source_id '%q' "$SOURCE_CONTAINER_ID"
  printf -v q_source_volume '%q' "$OLD_VOLUME_NAME"
  printf -v q_source_data_root '%q' "$OLD_DATA_ROOT"
  printf -v q_stamp '%q' "$STAMP"
  printf -v q_lock '%q' "$LOCK"
  printf -v q_cgroup_root '%q' "$CGROUP_ROOT"
  printf -v q_epoch_key '%q' "$REDIS_FENCING_EPOCH_KEY"
  cat > "$ROLLBACK_SCRIPT" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail

REDIS_CONTAINER=$q_redis
NODE_CONTAINERS=($q_node_a $q_node_b $q_node_c $q_node_d)
LEGACY_CONTAINER=$q_legacy
EXPECTED_NEW_CONTAINER_ID=$q_new_id
SOURCE_CONTAINER_ID=$q_source_id
SOURCE_VOLUME_NAME=$q_source_volume
SOURCE_DATA_ROOT=$q_source_data_root
REBASELINE_STAMP=$q_stamp
LOCK=$q_lock
CGROUP_ROOT=$q_cgroup_root
REDIS_FENCING_EPOCH_KEY=$q_epoch_key

die() {
  echo "ROLLBACK FAILED: \$*" >&2
  exit 1
}

require_root() {
  [ "\${EUID:-\$(id -u)}" -eq 0 ] || die "run as root on hk"
}

inspect_value() {
  docker inspect --format "\$1" "\$2"
}

generate_redis_fencing_epoch() {
  python3 - <<'PY'
from uuid import uuid4

print(uuid4())
PY
}

assert_node_stopped() {
  local node="\$1"
  local running
  local pid
  local container_id
  local process_file
  running="\$(inspect_value '{{.State.Running}}' "\$node")"
  pid="\$(inspect_value '{{.State.Pid}}' "\$node")"
  container_id="\$(inspect_value '{{.Id}}' "\$node")"
  [ "\$running" = "false" ] || die "\$node remained running"
  [ "\$pid" = "0" ] || die "\$node retained writer PID \$pid"
  if docker top "\$node" >/dev/null 2>&1; then
    die "\$node still exposes writer processes"
  fi
  if [ ! -d "\$CGROUP_ROOT" ]; then
    return 0
  fi
  while IFS= read -r process_file; do
    if grep -Eq '[0-9]' "\$process_file"; then
      die "\$node retained processes in \$process_file"
    fi
  done < <(
    find "\$CGROUP_ROOT" \
      -type f \
      \\( -name cgroup.procs -o -name tasks \\) \
      -path "*\${container_id}*" \
      -print 2>/dev/null
  )
}

main() {
  local node
  local running
  local active_id
  local legacy_id
  local label
  local run_id
  local mount_meta
  local mount_type
  local mount_name
  local mount_source
  local mount_rw
  local rollback_epoch
  local persisted_epoch
  local changes_after_save
  require_root
  command -v docker >/dev/null || die "docker missing"
  exec 9>"\$LOCK"
  flock -n 9 || die "another account-stall operation holds \$LOCK"

  for node in "\${NODE_CONTAINERS[@]}"; do
    running="\$(inspect_value '{{.State.Running}}' "\$node")"
    if [ "\$running" = "true" ]; then
      docker stop --time 30 "\$node" >/dev/null
    fi
    assert_node_stopped "\$node"
  done

  active_id="\$(docker inspect --format '{{.Id}}' "\$REDIS_CONTAINER" 2>/dev/null || true)"
  legacy_id="\$(docker inspect --format '{{.Id}}' "\$LEGACY_CONTAINER" 2>/dev/null || true)"
  if [ "\$active_id" = "\$SOURCE_CONTAINER_ID" ] && [ -z "\$legacy_id" ]; then
    echo "PASS: source Redis is already restored; execution nodes remain stopped"
    return 0
  fi
  [ "\$active_id" = "\$EXPECTED_NEW_CONTAINER_ID" ] \
    || die "active Redis identity differs from rebaseline evidence"
  [ "\$legacy_id" = "\$SOURCE_CONTAINER_ID" ] \
    || die "legacy Redis identity differs from cold-backup evidence"
  label="\$(
    docker inspect \
      --format '{{index .Config.Labels "trader-v3.redis-rebaseline"}}' \
      "\$REDIS_CONTAINER"
  )"
  [ "\$label" = "\$REBASELINE_STAMP" ] \
    || die "active Redis lacks the reviewed rebaseline label"

  docker stop --time 30 "\$REDIS_CONTAINER" >/dev/null
  docker rm "\$REDIS_CONTAINER" >/dev/null
  docker rename "\$LEGACY_CONTAINER" "\$REDIS_CONTAINER"
  docker start "\$REDIS_CONTAINER" >/dev/null
  for _ in \$(seq 1 60); do
    if docker exec "\$REDIS_CONTAINER" redis-cli PING 2>/dev/null \
      | grep -Fxq PONG; then
      break
    fi
    sleep 1
  done
  docker exec "\$REDIS_CONTAINER" redis-cli PING | grep -Fxq PONG \
    || die "source Redis did not become ready"
  [ "\$(inspect_value '{{.Id}}' "\$REDIS_CONTAINER")" = "\$SOURCE_CONTAINER_ID" ] \
    || die "restored Redis container identity mismatch"
  mount_meta="\$(
    inspect_value \
      '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Type}}|{{.Name}}|{{.Source}}|{{.RW}}{{end}}{{end}}' \
      "\$REDIS_CONTAINER"
  )"
  IFS='|' read -r mount_type mount_name mount_source mount_rw \
    <<< "\$mount_meta"
  [ "\$mount_type" = "volume" ] \
    || die "restored Redis /data mount type mismatch"
  [ "\$mount_name" = "\$SOURCE_VOLUME_NAME" ] \
    || die "restored Redis source volume mismatch"
  [ "\$mount_source" = "\$SOURCE_DATA_ROOT" ] \
    || die "restored Redis source volume mountpoint mismatch"
  [ "\$mount_rw" = "true" ] \
    || die "restored Redis source volume is read-only"
  run_id="\$(
    docker exec "\$REDIS_CONTAINER" redis-cli --raw INFO server \
      | sed -n 's/^run_id://p' \
      | tr -d '\r'
  )"
  [ -n "\$run_id" ] || die "restored Redis run_id is missing"
  rollback_epoch="\$(generate_redis_fencing_epoch)"
  [[ "\$rollback_epoch" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\$ ]] \
    || die "rollback Redis fencing epoch is not a canonical UUID4"
  [ "\$(
    docker exec "\$REDIS_CONTAINER" redis-cli SET \
      "\$REDIS_FENCING_EPOCH_KEY" "\$rollback_epoch" \
      | tr -d '\r'
  )" = "OK" ] || die "rollback Redis fencing epoch marker write failed"
  docker exec "\$REDIS_CONTAINER" redis-cli SAVE >/dev/null \
    || die "rollback Redis fencing epoch synchronous SAVE failed"
  changes_after_save="\$(
    docker exec "\$REDIS_CONTAINER" redis-cli --raw INFO persistence \
      | sed -n 's/^rdb_changes_since_last_save://p' \
      | tr -d '\r'
  )"
  [ "\$changes_after_save" = "0" ] \
    || die "rollback Redis fencing epoch changed after synchronous SAVE"
  persisted_epoch="\$(
    docker exec "\$REDIS_CONTAINER" redis-cli --raw GET \
      "\$REDIS_FENCING_EPOCH_KEY" \
      | tr -d '\r'
  )"
  [ "\$persisted_epoch" = "\$rollback_epoch" ] \
    || die "rollback Redis fencing epoch marker verification failed"
  echo "PASS: source Redis restored; execution nodes remain stopped"
  echo "restored_redis_run_id=\$run_id"
  echo "redis_fencing_epoch=\$rollback_epoch"
}

if [ "\${BASH_SOURCE[0]}" = "\$0" ]; then
  main "\$@"
fi
EOF
  chmod 0700 "$ROLLBACK_SCRIPT"
  {
    echo "Reviewed rollback command:"
    printf '%q\n' "$ROLLBACK_SCRIPT"
    echo "The script stops accounts A-D and proves writer PID/cgroup emptiness before switching Redis."
    echo "The hardened Redis volume is preserved for forensic review."
  } > "$EVIDENCE_ROOT/ROLLBACK.txt"
}

main() {
  local old_mount_meta
  local new_mount_meta
  local backup_manifest_sha256
  local keyspace_databases
  local old_volume_mountpoint
  local new_volume_mountpoint
  local aof_path
  local aof_relative
  local aof_count=0
  local aof_manifest_relative
  local pre_epoch_key_count

  require_root
  command -v docker >/dev/null || die "docker missing"
  command -v python3 >/dev/null || die "python3 missing"
  command -v flock >/dev/null || die "flock missing"
  [ -f "$CAPACITY_PLANNER" ] || die "capacity planner missing"
  load_redis_resource_contract
  exec 9>"$LOCK"
  flock -n 9 || die "another account-stall operation holds $LOCK"
  trap on_err ERR
  trap 'on_signal 130 INT' INT
  trap 'on_signal 143 TERM' TERM

  assert_positive_integer "Redis maxmemory" "$MAXMEMORY_BYTES"
  assert_positive_integer "Redis cgroup limit" "$CGROUP_LIMIT_BYTES"
  assert_positive_integer "Redis stream max entries" "$STREAM_MAX_ENTRIES"
  assert_positive_integer "Redis stream max bytes" "$STREAM_MAX_BYTES"
  assert_positive_integer \
    "Redis total stream max bytes" \
    "$TOTAL_STREAM_MAX_BYTES"
  [ "$CGROUP_LIMIT_BYTES" -gt "$MAXMEMORY_BYTES" ] \
    || die "Redis cgroup limit must exceed maxmemory"
  [ "$TOTAL_STREAM_MAX_BYTES" -ge "$STREAM_MAX_BYTES" ] \
    || die "Redis total stream budget must cover one stream"
  [ "$NAMESPACE_SCHEMA_EPOCH" = "fenced-generation-namespace/v2" ] \
    || die "Redis namespace schema epoch is unsupported"
  [ "$ACCOUNT_A_STABLE_NAMESPACE" = "trader-TRADER-ACCOUNT-A" ] \
    || die "account-a stable Redis namespace is invalid"
  [ "$ACCOUNT_B_STABLE_NAMESPACE" = "trader-TRADER-ACCOUNT-B" ] \
    || die "account-b stable Redis namespace is invalid"
  [ "$ACCOUNT_C_STABLE_NAMESPACE" = "trader-TRADER-ACCOUNT-C" ] \
    || die "account-c stable Redis namespace is invalid"
  [ "$ACCOUNT_D_STABLE_NAMESPACE" = "trader-TRADER-ACCOUNT-D" ] \
    || die "account-d stable Redis namespace is invalid"
  [ "$(
    printf '%s\n' \
      "$ACCOUNT_A_STABLE_NAMESPACE" \
      "$ACCOUNT_B_STABLE_NAMESPACE" \
      "$ACCOUNT_C_STABLE_NAMESPACE" \
      "$ACCOUNT_D_STABLE_NAMESPACE" \
      | sort -u \
      | wc -l \
      | tr -d ' '
  )" = "4" ] || die "A-D stable Redis namespaces must be distinct"

  docker inspect "$REDIS_CONTAINER" "${NODE_CONTAINERS[@]}" >/dev/null \
    || die "required trader-v3 containers are missing"
  [ ! -e "$EVIDENCE_ROOT" ] || die "evidence path exists: $EVIDENCE_ROOT"
  mkdir -p "$BACKUP_ROOT/data" "$VALIDATOR_ROOT"
  chmod 0700 "$EVIDENCE_ROOT" "$BACKUP_ROOT" "$VALIDATOR_ROOT"
  set_phase "preflight"

  SOURCE_CONTAINER_ID="$(inspect_value '{{.Id}}' "$REDIS_CONTAINER")"
  [ -n "$SOURCE_CONTAINER_ID" ] || die "source Redis identity is missing"
  capture_source_network_attachments
  if [ "$(inspect_value '{{.State.Running}}' "$REDIS_CONTAINER")" = "true" ]; then
    SOURCE_WAS_RUNNING=1
  else
    die "source Redis must be running before rebaseline"
  fi
  OLD_IMAGE="$(inspect_value '{{.Image}}' "$REDIS_CONTAINER")"
  OLD_CONFIG_IMAGE="$(inspect_value '{{.Config.Image}}' "$REDIS_CONTAINER")"
  old_mount_meta="$(
    inspect_value \
      '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Type}}|{{.Name}}|{{.Source}}|{{.RW}}{{end}}{{end}}' \
      "$REDIS_CONTAINER"
  )"
  IFS='|' read -r OLD_MOUNT_TYPE OLD_VOLUME_NAME OLD_DATA_ROOT OLD_MOUNT_RW \
    <<< "$old_mount_meta"
  [ "$OLD_MOUNT_TYPE" = "volume" ] \
    || die "source Redis /data must use a Docker volume"
  [ -n "$OLD_VOLUME_NAME" ] || die "source Redis volume name is missing"
  [ "$OLD_MOUNT_RW" = "true" ] || die "source Redis /data volume is read-only"
  [ -d "$OLD_DATA_ROOT" ] || die "source Redis /data mount is invalid"
  old_volume_mountpoint="$(
    docker volume inspect --format '{{.Mountpoint}}' "$OLD_VOLUME_NAME"
  )"
  [ "$old_volume_mountpoint" = "$OLD_DATA_ROOT" ] \
    || die "source Redis active volume mountpoint mismatch"

  OLD_RUN_ID="$(redis_info_value "$REDIS_CONTAINER" server run_id)"
  [ -n "$OLD_RUN_ID" ] || die "cannot read source Redis run_id"
  OLD_KEY_COUNT="$(
    docker exec "$REDIS_CONTAINER" redis-cli --raw DBSIZE | tr -dc '0-9'
  )"
  assert_nonnegative_integer "source Redis key count" "$OLD_KEY_COUNT"
  OLD_USED_MEMORY="$(redis_info_value "$REDIS_CONTAINER" memory used_memory)"
  OLD_DATASET_MEMORY="$(
    redis_info_value "$REDIS_CONTAINER" memory used_memory_dataset
  )"
  assert_positive_integer "source Redis used_memory" "$OLD_USED_MEMORY"
  assert_nonnegative_integer "source Redis dataset memory" "$OLD_DATASET_MEMORY"
  OLD_AOF_ENABLED="$(
    redis_info_value "$REDIS_CONTAINER" persistence aof_enabled
  )"
  [ "$OLD_AOF_ENABLED" = "0" ] || [ "$OLD_AOF_ENABLED" = "1" ] \
    || die "source Redis AOF state is invalid"
  OLD_REDIS_DIR="$(redis_config_value "$REDIS_CONTAINER" dir)"
  [ "$OLD_REDIS_DIR" = "/data" ] \
    || die "source Redis dir differs from /data volume"
  OLD_RDB_FILENAME="$(redis_config_value "$REDIS_CONTAINER" dbfilename)"
  [ -n "$OLD_RDB_FILENAME" ] || die "source Redis dbfilename is missing"
  [[ "$OLD_RDB_FILENAME" =~ ^[^/]+$ ]] \
    || die "source Redis dbfilename is unsafe"
  OLD_SAVE_POLICY="$(redis_config_value "$REDIS_CONTAINER" save)"
  [ -n "$OLD_SAVE_POLICY" ] || die "source Redis save policy is empty"
  OLD_APPENDONLY="$(redis_config_value "$REDIS_CONTAINER" appendonly)"
  OLD_APPENDFILENAME="$(
    redis_config_value "$REDIS_CONTAINER" appendfilename
  )"
  OLD_APPENDDIRNAME="$(
    redis_config_value "$REDIS_CONTAINER" appenddirname 2>/dev/null || true
  )"
  [ -n "$OLD_APPENDDIRNAME" ] || OLD_APPENDDIRNAME="appendonlydir"

  MUTATION_STARTED=1
  set_phase "stopping_nodes"
  stop_execution_nodes
  docker exec "$REDIS_CONTAINER" redis-cli SAVE >/dev/null \
    || die "synchronous Redis SAVE failed"
  CHANGES_AFTER_SAVE="$(
    redis_info_value \
      "$REDIS_CONTAINER" \
      persistence \
      rdb_changes_since_last_save
  )"
  [ "$CHANGES_AFTER_SAVE" = "0" ] \
    || die "Redis changed after SAVE while execution nodes were stopped"

  set_phase "source_stopping"
  docker stop --time 60 "$REDIS_CONTAINER" >/dev/null
  [ "$(inspect_value '{{.State.Running}}' "$REDIS_CONTAINER")" = "false" ] \
    || die "source Redis remained running"
  [ "$(inspect_value '{{.State.Pid}}' "$REDIS_CONTAINER")" = "0" ] \
    || die "source Redis retained a PID after stop"

  set_phase "copying_cold_backup"
  cp -a "$OLD_DATA_ROOT"/. "$BACKUP_ROOT/data/"
  python3 - "$BACKUP_ROOT" > "$EVIDENCE_ROOT/SHA256SUMS.tmp" <<'PY'
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

root = Path(sys.argv[1])
for path in sorted((root / "data").rglob("*")):
    if not path.is_file() or path.is_symlink():
        continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"{digest}  {path.relative_to(root)}")
PY
  mv "$EVIDENCE_ROOT/SHA256SUMS.tmp" "$BACKUP_ROOT/SHA256SUMS"
  [ -s "$BACKUP_ROOT/SHA256SUMS" ] \
    || die "cold backup file inventory is empty"

  RDB_RELATIVE="data/$OLD_RDB_FILENAME"
  [ -f "$BACKUP_ROOT/$RDB_RELATIVE" ] \
    || die "cold backup lacks configured RDB $OLD_RDB_FILENAME"
  run_redis_checker redis-check-rdb "$RDB_RELATIVE"

  if [ "$OLD_AOF_ENABLED" = "1" ]; then
    while IFS= read -r aof_path; do
      aof_relative="${aof_path#$BACKUP_ROOT/}"
      run_redis_checker redis-check-aof "$aof_relative"
      aof_count=$((aof_count + 1))
    done < <(find "$BACKUP_ROOT/data" -type f -name '*.aof' -print)
    [ "$aof_count" -gt 0 ] || die "enabled AOF has no copied AOF artifacts"
    aof_manifest_relative="data/$OLD_APPENDDIRNAME/${OLD_APPENDFILENAME}.manifest"
    if [ -f "$BACKUP_ROOT/$aof_manifest_relative" ]; then
      validate_aof_manifest "$aof_manifest_relative"
    fi
  fi

  write_backup_manifest
  (
    cd "$BACKUP_ROOT"
    sha256sum -c SHA256SUMS
  ) >/dev/null
  python3 "$CAPACITY_PLANNER" verify-backup \
    --manifest "$BACKUP_MANIFEST" \
    --backup-root "$BACKUP_ROOT" \
    --source-run-id "$OLD_RUN_ID" \
    --source-volume "$OLD_VOLUME_NAME" \
    --source-container-id "$SOURCE_CONTAINER_ID" >/dev/null
  set_phase "cold_backup_verified"

  docker rename "$REDIS_CONTAINER" "$LEGACY_CONTAINER"
  [ "$(container_id_or_empty "$LEGACY_CONTAINER")" = "$SOURCE_CONTAINER_ID" ] \
    || die "source Redis rename identity mismatch"
  set_phase "source_renamed"

  docker volume create \
    --label "$REBASELINE_LABEL" \
    "$NEW_VOLUME" >/dev/null
  [ "$(volume_label_or_empty "$NEW_VOLUME")" = "$STAMP" ] \
    || die "new Redis volume lacks rebaseline ownership label"
  set_phase "new_volume_created"

  NEW_CONTAINER_ID="$(
    docker run -d \
      --name "$REDIS_CONTAINER" \
      --label "$REBASELINE_LABEL" \
      --restart "$REDIS_RESTART_POLICY" \
      --memory "$CGROUP_LIMIT_BYTES" \
      --memory-swap "$CGROUP_LIMIT_BYTES" \
      --cpus "$REDIS_CPU_LIMIT" \
      --pids-limit "$REDIS_PIDS_LIMIT" \
      --ulimit "nofile=$REDIS_NOFILE_LIMIT:$REDIS_NOFILE_LIMIT" \
      -p 127.0.0.1:6379:6379 \
      -v "$NEW_VOLUME:/data" \
      "$OLD_IMAGE" \
      redis-server \
      --appendonly no \
      --save "$NEW_SAVE_POLICY" \
      --maxmemory "$MAXMEMORY_BYTES" \
      --maxmemory-policy noeviction
  )"
  [ -n "$NEW_CONTAINER_ID" ] || die "new Redis container identity is missing"
  [ "$(container_label_or_empty "$REDIS_CONTAINER")" = "$STAMP" ] \
    || die "new Redis container lacks rebaseline ownership label"
  restore_source_network_attachments
  verify_source_network_attachments
  set_phase "new_container_created"

  for _ in $(seq 1 60); do
    if docker exec "$REDIS_CONTAINER" redis-cli PING 2>/dev/null \
      | grep -Fxq PONG; then
      break
    fi
    sleep 1
  done
  docker exec "$REDIS_CONTAINER" redis-cli PING | grep -Fxq PONG \
    || die "bounded Redis did not become ready"

  NEW_RUN_ID="$(redis_info_value "$REDIS_CONTAINER" server run_id)"
  [ -n "$NEW_RUN_ID" ] || die "new Redis run_id is missing"
  [ "$NEW_RUN_ID" != "$OLD_RUN_ID" ] \
    || die "new Redis reused the source run_id"
  pre_epoch_key_count="$(
    docker exec "$REDIS_CONTAINER" redis-cli --raw DBSIZE | tr -dc '0-9'
  )"
  assert_nonnegative_integer "new Redis key count" "$pre_epoch_key_count"
  [ "$pre_epoch_key_count" = "0" ] || die "new Redis volume is not empty"
  keyspace_databases="$(
    docker exec "$REDIS_CONTAINER" redis-cli --raw INFO keyspace \
      | sed -n '/^db[0-9][0-9]*:/p'
  )"
  [ -z "$keyspace_databases" ] \
    || die "new Redis has keys outside DB 0"
  stop_execution_nodes
  REDIS_FENCING_EPOCH="$(generate_redis_fencing_epoch)"
  persist_redis_fencing_epoch \
    "$REDIS_CONTAINER" \
    "$REDIS_FENCING_EPOCH"
  REDIS_FENCING_EPOCH_SHA256="$(
    printf '%s' "$REDIS_FENCING_EPOCH" \
      | sha256sum \
      | awk '{print $1}'
  )"
  NEW_KEY_COUNT="$(
    docker exec "$REDIS_CONTAINER" redis-cli --raw DBSIZE | tr -dc '0-9'
  )"
  assert_nonnegative_integer "new Redis key count" "$NEW_KEY_COUNT"
  [ "$NEW_KEY_COUNT" = "1" ] \
    || die "new Redis must contain only the fencing epoch marker"
  keyspace_databases="$(
    docker exec "$REDIS_CONTAINER" redis-cli --raw INFO keyspace \
      | sed -n '/^db[0-9][0-9]*:/p'
  )"
  [[ "$keyspace_databases" =~ ^db0:keys=1, ]] \
    || die "new Redis fencing epoch marker is outside DB 0"
  [ "$(printf '%s\n' "$keyspace_databases" | wc -l | tr -d ' ')" = "1" ] \
    || die "new Redis has keys outside DB 0"
  set_phase "epoch_persisted"

  NEW_USED_MEMORY="$(redis_info_value "$REDIS_CONTAINER" memory used_memory)"
  NEW_DATASET_MEMORY="$(
    redis_info_value "$REDIS_CONTAINER" memory used_memory_dataset
  )"
  assert_positive_integer "new Redis used_memory" "$NEW_USED_MEMORY"
  assert_nonnegative_integer "new Redis dataset memory" "$NEW_DATASET_MEMORY"
  ACTUAL_MAXMEMORY="$(redis_config_value "$REDIS_CONTAINER" maxmemory)"
  ACTUAL_POLICY="$(
    redis_config_value "$REDIS_CONTAINER" maxmemory-policy
  )"
  ACTUAL_APPENDONLY="$(redis_config_value "$REDIS_CONTAINER" appendonly)"
  ACTUAL_SAVE_POLICY="$(redis_config_value "$REDIS_CONTAINER" save)"
  ACTUAL_AOF_ENABLED="$(
    redis_info_value "$REDIS_CONTAINER" persistence aof_enabled
  )"
  ACTUAL_RDB_STATUS="$(
    redis_info_value "$REDIS_CONTAINER" persistence rdb_last_bgsave_status
  )"
  ACTUAL_MEMORY="$(inspect_value '{{.HostConfig.Memory}}' "$REDIS_CONTAINER")"
  ACTUAL_MEMORY_SWAP="$(
    inspect_value '{{.HostConfig.MemorySwap}}' "$REDIS_CONTAINER"
  )"
  ACTUAL_NANO_CPUS="$(
    inspect_value '{{.HostConfig.NanoCpus}}' "$REDIS_CONTAINER"
  )"
  ACTUAL_PIDS_LIMIT="$(
    inspect_value '{{.HostConfig.PidsLimit}}' "$REDIS_CONTAINER"
  )"
  ACTUAL_NOFILE="$(
    inspect_value \
      '{{range .HostConfig.Ulimits}}{{if eq .Name "nofile"}}{{.Soft}}:{{.Hard}}{{end}}{{end}}' \
      "$REDIS_CONTAINER"
  )"
  ACTUAL_RESTART_POLICY="$(
    inspect_value '{{.HostConfig.RestartPolicy.Name}}' "$REDIS_CONTAINER"
  )"
  [ "$ACTUAL_MAXMEMORY" = "$MAXMEMORY_BYTES" ] \
    || die "new Redis maxmemory mismatch"
  [ "$ACTUAL_POLICY" = "noeviction" ] \
    || die "new Redis policy mismatch"
  [ "$ACTUAL_MEMORY" = "$CGROUP_LIMIT_BYTES" ] \
    || die "new Redis cgroup limit mismatch"
  [ "$ACTUAL_MEMORY_SWAP" = "$CGROUP_LIMIT_BYTES" ] \
    || die "new Redis memory-swap mismatch"
  [ "$ACTUAL_NANO_CPUS" = "$REDIS_NANO_CPUS" ] \
    || die "new Redis CPU limit mismatch"
  [ "$ACTUAL_PIDS_LIMIT" = "$REDIS_PIDS_LIMIT" ] \
    || die "new Redis tasks limit mismatch"
  [ "$ACTUAL_NOFILE" = "$REDIS_NOFILE_LIMIT:$REDIS_NOFILE_LIMIT" ] \
    || die "new Redis nofile limit mismatch"
  [ "$ACTUAL_RESTART_POLICY" = "$REDIS_RESTART_POLICY" ] \
    || die "new Redis restart policy mismatch"
  [ "$ACTUAL_APPENDONLY" = "no" ] \
    || die "new Redis appendonly mode mismatch"
  [ "$ACTUAL_AOF_ENABLED" = "0" ] \
    || die "new Redis AOF runtime state mismatch"
  [ "$ACTUAL_SAVE_POLICY" = "$NEW_SAVE_POLICY" ] \
    || die "new Redis save policy mismatch"
  [ "$ACTUAL_RDB_STATUS" = "ok" ] \
    || die "new Redis RDB status is unhealthy"

  new_mount_meta="$(
    inspect_value \
      '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Type}}|{{.Name}}|{{.Source}}|{{.RW}}{{end}}{{end}}' \
      "$REDIS_CONTAINER"
  )"
  IFS='|' read -r ACTIVE_MOUNT_TYPE ACTIVE_VOLUME_NAME \
    ACTIVE_VOLUME_SOURCE ACTIVE_MOUNT_RW <<< "$new_mount_meta"
  [ "$ACTIVE_MOUNT_TYPE" = "volume" ] \
    || die "new Redis /data does not use a Docker volume"
  [ "$ACTIVE_VOLUME_NAME" = "$NEW_VOLUME" ] \
    || die "new Redis active volume name mismatch"
  [ "$ACTIVE_MOUNT_RW" = "true" ] \
    || die "new Redis active volume is read-only"
  new_volume_mountpoint="$(
    docker volume inspect --format '{{.Mountpoint}}' "$NEW_VOLUME"
  )"
  [ "$new_volume_mountpoint" = "$ACTIVE_VOLUME_SOURCE" ] \
    || die "new Redis active volume mountpoint mismatch"
  [ "$(container_id_or_empty "$LEGACY_CONTAINER")" = "$SOURCE_CONTAINER_ID" ] \
    || die "source Redis container was not preserved"
  [ "$(inspect_value '{{.State.Running}}' "$LEGACY_CONTAINER")" = "false" ] \
    || die "source Redis legacy container unexpectedly restarted"
  stop_execution_nodes

  python3 - "$MEMINFO_PATH" > "$EVIDENCE_ROOT/host-memory-probe.txt" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

values = {}
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    parts = line.split()
    if len(parts) != 3 or parts[2].lower() != "kb":
        continue
    key = parts[0].rstrip(":")
    if key in {"MemTotal", "MemAvailable"}:
        values[key] = int(parts[1]) * 1024
if set(values) != {"MemTotal", "MemAvailable"}:
    raise SystemExit("MemTotal and MemAvailable are required")
print(values["MemTotal"], values["MemAvailable"])
PY
  read -r HOST_TOTAL_BYTES HOST_AVAILABLE_BYTES \
    < "$EVIDENCE_ROOT/host-memory-probe.txt"
  assert_positive_integer "host total memory" "$HOST_TOTAL_BYTES"
  assert_positive_integer "host available memory" "$HOST_AVAILABLE_BYTES"

  python3 "$CAPACITY_PLANNER" generate \
    --maxmemory "$ACTUAL_MAXMEMORY" \
    --current-used-memory "$NEW_USED_MEMORY" \
    --current-dataset-size "$NEW_DATASET_MEMORY" \
    --headroom-percent "$HEADROOM_PERCENT" \
    --redis-cgroup-limit "$ACTUAL_MEMORY" \
    --host-total-memory "$HOST_TOTAL_BYTES" \
    --host-available-memory "$HOST_AVAILABLE_BYTES" \
    --other-services-reserve "$OTHER_SERVICES_RESERVE_BYTES" \
    --system-reserve "$SYSTEM_RESERVE_BYTES" \
    --container-headroom-percent "$CONTAINER_HEADROOM_PERCENT" \
    > "$CAPACITY_PLAN"
  backup_manifest_sha256="$(
    sha256sum "$BACKUP_MANIFEST" | awk '{print $1}'
  )"
  write_capacity_evidence "$backup_manifest_sha256"
  verify_capacity_evidence
  set_phase "runtime_verified"

  write_rollback_script
  chmod 0600 \
    "$BACKUP_MANIFEST" \
    "$CAPACITY_PLAN" \
    "$CAPACITY_EVIDENCE" \
    "$SOURCE_NETWORK_ATTACHMENTS" \
    "$EVIDENCE_ROOT/ROLLBACK.txt"
  ln -sfn "$EVIDENCE_ROOT" "$TRADER_ROOT/redis-rebaseline/current"
  set_phase "complete"
  REBASELINE_COMPLETE=1
  trap - ERR INT TERM

  echo "PASS: Redis rebaseline complete"
  echo "cold_backup_manifest=$BACKUP_MANIFEST"
  echo "capacity_evidence=$CAPACITY_EVIDENCE"
  echo "rollback_script=$ROLLBACK_SCRIPT"
  echo "legacy_container=$LEGACY_CONTAINER"
  echo "new_volume=$NEW_VOLUME"
  echo "execution nodes remain stopped"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
