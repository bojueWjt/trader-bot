#!/usr/bin/env bash
# Recreate account-b from a reviewed 3922a7c-based runtime bundle.
set -Eeuo pipefail

T="${T:-/srv/trader-v3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGING="${STAGING:-$SCRIPT_DIR}"
NODE_CONTAINER="trader-v3-node-b"
PEER_CONTAINER="trader-v3-node-a"
NODE_ID="nautilus-node-account-b"
ACCOUNT_ID="account-b"
NODE_PORT="${NODE_PORT:-8082}"
TARGET_IMAGE="trader-bot/nautilus-node:b10-verify"
EXPECTED_PEER_BASELINE_COMMIT="3922a7c176a872452ff4ddde791ce42069684017"
EXPECTED_OBSERVABILITY_BUNDLE="control_plane_session.py"
EXPECTED_OBSERVABILITY_TARGET="/app/runtime/control_plane_session.py"
OPERATION_LOCK="${OPERATION_LOCK:-/var/lock/trader-v3-account-stall-operation.lock}"
MEMORY_LIMIT="${ACCOUNT_B_MEMORY_LIMIT:-768m}"
MEMORY_SWAP_LIMIT="${ACCOUNT_B_MEMORY_SWAP_LIMIT:-768m}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-trader-v3-postgres}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-trader}"
REDIS_CONTAINER="${REDIS_CONTAINER:-trader-v3-redis}"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
BACKUP_ROOT="${BACKUP_ROOT:-$T/backups/account-b-hardening-$STAMP}"
ROLLBACK_PATH="$BACKUP_ROOT/rollback.sh"
MANIFEST="$STAGING/bundle-manifest.json"
CHECKSUMS="$STAGING/SHA256SUMS"
PATCH_DIR="$T/container-patches"
GENERATOR_SOURCE="$STAGING/tools/hk-gen-recreate-patched.py"
GENERATOR_TARGET="$T/hk-gen-recreate-patched.py"
RECREATE_TARGET="$T/recreate-$NODE_CONTAINER.sh"
STATE_DIR="$T/node-state/b"
DEPLOYED_COMMIT_TARGET="$T/DEPLOYED_COMMIT.txt"
TEMP_DIR=""
CONTAINER_TSV=""
PEER_TSV=""
DELTA_TSV=""
RELEASE_COMMIT=""
TARGET_IMAGE_ID=""
BINANCE_EXEC_DST=""
BINANCE_FUTURES_EXEC_DST=""
EXCHANGE_BEFORE=""
MUTATION_STARTED=0
FAILURE_REPORTED=0


die() {
  echo "FATAL: $*" >&2
  return 1
}


cleanup() {
  if [ -n "$TEMP_DIR" ]; then
    rm -rf "$TEMP_DIR"
  fi
}


report_failure() {
  local status="$1"
  if [ "$FAILURE_REPORTED" = "1" ]; then
    return
  fi
  FAILURE_REPORTED=1
  echo "FATAL: account-b hardening rollout failed" >&2
  if [ "$MUTATION_STARTED" = "1" ] && [ -x "$ROLLBACK_PATH" ]; then
    echo "ROLLBACK: bash $ROLLBACK_PATH" >&2
  else
    echo "ROLLBACK: no mutation completed" >&2
  fi
  return "$status"
}


on_err() {
  local status=$?
  trap - ERR
  report_failure "$status" || true
  exit "$status"
}


trap cleanup EXIT
trap on_err ERR


sha256_file() {
  sha256sum "$1" | awk '{print $1}'
}


load_env_value() {
  python3 - "$T/.env.v3" "$1" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
target = sys.argv[2]
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    if line.startswith("export "):
        line = line[len("export "):]
    key, value = line.split("=", 1)
    if key.strip() != target:
        continue
    value = value.strip()
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {"'", '"'}
    ):
        value = value[1:-1]
    print(value)
    raise SystemExit(0)
raise SystemExit(f"{target} is missing from {path}")
PY
}


validate_bundle() {
  (
    cd "$STAGING"
    sha256sum -c "$(basename "$CHECKSUMS")" >/dev/null
  ) || die "staging SHA256 verification failed"

  RELEASE_COMMIT="$(
    python3 - \
      "$MANIFEST" \
      "$CONTAINER_TSV" \
      "$PEER_TSV" \
      "$DELTA_TSV" \
      "$EXPECTED_PEER_BASELINE_COMMIT" \
      "$EXPECTED_OBSERVABILITY_BUNDLE" \
      "$EXPECTED_OBSERVABILITY_TARGET" <<'PY'
from __future__ import annotations

import json
from pathlib import PurePosixPath
import re
import sys

(
    manifest_path,
    container_path,
    peer_path,
    delta_path,
    expected_baseline,
    expected_delta_bundle,
    expected_delta_target,
) = sys.argv[1:]
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


def relative(value: object, label: str) -> str:
    normalized = str(value or "")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"{label} is invalid: {normalized!r}")
    if re.search(r"[\t\r\n]", normalized):
        raise SystemExit(f"{label} contains control characters")
    return normalized


files = manifest.get("files")
if not isinstance(files, list) or not files:
    raise SystemExit("container file manifest is empty")
release = {}
with open(container_path, "w", encoding="utf-8") as output:
    for item in files:
        if not isinstance(item, dict):
            raise SystemExit("container file manifest item is invalid")
        bundle_path = relative(
            item.get("bundle_path"),
            "container bundle_path",
        )
        target = str(item.get("mount_target") or "")
        digest = str(item.get("sha256") or "")
        if not target.startswith("/") or re.search(r"[\t\r\n]", target):
            raise SystemExit(
                f"container mount target is invalid: {target!r}"
            )
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SystemExit(f"container SHA256 is invalid: {bundle_path}")
        if bundle_path in release:
            raise SystemExit("container manifest has duplicate bundle paths")
        if target in {value[0] for value in release.values()}:
            raise SystemExit("container manifest has duplicate mount targets")
        release[bundle_path] = (target, digest)
        output.write(f"{bundle_path}\t{target}\t{digest}\n")

contract = manifest.get("account_b_peer_contract")
if not isinstance(contract, dict):
    raise SystemExit("account_b_peer_contract is missing")
if contract.get("schema_version") != "1.0":
    raise SystemExit("account_b_peer_contract schema is invalid")
if contract.get("peer_container") != "trader-v3-node-a":
    raise SystemExit("account-b peer container is invalid")
baseline = str(contract.get("baseline_repo_commit") or "")
if baseline != expected_baseline:
    raise SystemExit(
        "account-b peer baseline must be "
        f"{expected_baseline}, got {baseline}"
    )
peer_files = contract.get("peer_files")
if not isinstance(peer_files, list) or not peer_files:
    raise SystemExit("account-b peer file manifest is empty")
peer = {}
with open(peer_path, "w", encoding="utf-8") as output:
    for item in peer_files:
        if not isinstance(item, dict):
            raise SystemExit("account-b peer file item is invalid")
        bundle_path = relative(
            item.get("bundle_path"),
            "peer bundle_path",
        )
        target = str(item.get("mount_target") or "")
        digest = str(item.get("sha256") or "")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SystemExit(f"peer SHA256 is invalid: {bundle_path}")
        if bundle_path in peer:
            raise SystemExit("peer manifest has duplicate bundle paths")
        peer[bundle_path] = (target, digest)
        output.write(f"{bundle_path}\t{target}\t{digest}\n")
if {
    key: value[0]
    for key, value in peer.items()
} != {
    key: value[0]
    for key, value in release.items()
}:
    raise SystemExit("account-b release and peer mount sets differ")

delta = contract.get("observability_delta")
if not isinstance(delta, dict):
    raise SystemExit("account-b observability_delta is missing")
delta_bundle = relative(
    delta.get("bundle_path"),
    "observability delta bundle_path",
)
delta_target = str(delta.get("mount_target") or "")
delta_peer = str(delta.get("peer_sha256") or "")
delta_release = str(delta.get("release_sha256") or "")
if delta_bundle not in release:
    raise SystemExit("observability delta is outside the runtime file set")
if delta_bundle != expected_delta_bundle:
    raise SystemExit("account-b observability bundle path is invalid")
if delta_target != expected_delta_target:
    raise SystemExit("account-b observability mount target is invalid")
if release[delta_bundle] != (delta_target, delta_release):
    raise SystemExit("observability release hash binding is invalid")
if peer[delta_bundle] != (delta_target, delta_peer):
    raise SystemExit("observability peer hash binding is invalid")
if delta_peer == delta_release:
    raise SystemExit("observability delta hashes must differ")
changed = {
    bundle_path
    for bundle_path in release
    if release[bundle_path][1] != peer[bundle_path][1]
}
if changed != {delta_bundle}:
    raise SystemExit(
        "runtime hashes differ beyond the reviewed observability delta"
    )
with open(delta_path, "w", encoding="utf-8") as output:
    output.write(
        f"{delta_bundle}\t{delta_target}\t"
        f"{delta_peer}\t{delta_release}\n"
    )

deployment_paths = {
    str(item.get("bundle_path") or "")
    for item in manifest.get("deployment_files") or []
    if isinstance(item, dict)
}
required_tools = {
    "tools/hk-deploy-account-b-hardening.sh",
    "tools/hk-gen-recreate-patched.py",
}
if not required_tools.issubset(deployment_paths):
    raise SystemExit("account-b deployment tools are incomplete")
print(commit)
PY
  )" || die "bundle manifest validation failed"
}


verify_stopped_baseline() {
  docker inspect "$NODE_CONTAINER" >"$TEMP_DIR/container-inspect.json"
  docker inspect "$PEER_CONTAINER" >"$TEMP_DIR/peer-inspect.json"
  python3 - \
    "$TEMP_DIR/container-inspect.json" \
    "$TEMP_DIR/peer-inspect.json" \
    "$TARGET_IMAGE" \
    "$TARGET_IMAGE_ID" \
    "$NODE_PORT" \
    "$CONTAINER_TSV" \
    "$T" <<'PY'
import json
from pathlib import Path
import sys

(
    node_path,
    peer_path,
    target_image,
    target_image_id,
    node_port,
    manifest_path,
    trader_root,
) = sys.argv[1:]
with open(node_path, encoding="utf-8") as source:
    node_payload = json.load(source)
with open(peer_path, encoding="utf-8") as source:
    peer_payload = json.load(source)
if not isinstance(node_payload, list) or len(node_payload) != 1:
    raise SystemExit("account-b docker inspect payload is invalid")
if not isinstance(peer_payload, list) or len(peer_payload) != 1:
    raise SystemExit("account-a docker inspect payload is invalid")
node = node_payload[0]
peer = peer_payload[0]
state = node.get("State") or {}
if state.get("Running") is not False or state.get("Status") != "exited":
    raise SystemExit("account-b container must be stopped")
if state.get("ExitCode") != 137:
    raise SystemExit("account-b exit code baseline changed")
if state.get("OOMKilled") is not False:
    raise SystemExit("account-b OOMKilled baseline changed")
finished_at = str(state.get("FinishedAt") or "")
if not finished_at.startswith("2026-08-08T07:26:57"):
    raise SystemExit("account-b stopped timestamp baseline changed")
config = node.get("Config") or {}
if config.get("Image") != target_image:
    raise SystemExit("account-b image tag differs from rollout target")
node_image_id = str(node.get("Image") or "")
peer_image_id = str(peer.get("Image") or "")
if (
    not target_image_id
    or node_image_id != target_image_id
    or peer_image_id != target_image_id
):
    raise SystemExit("account-a/account-b image ID baseline changed")
mounts = node.get("Mounts") or []
expected_targets = set()
with open(manifest_path, encoding="utf-8") as source:
    for raw in source:
        _bundle_path, target, _digest = raw.rstrip("\n").split("\t")
        expected_targets.add(target)
mount_by_target = {
    str(item.get("Destination") or ""): item
    for item in mounts
}
allowed_targets = expected_targets | {"/state", "/cfg.json"}
unexpected_targets = set(mount_by_target) - allowed_targets
if unexpected_targets:
    raise SystemExit(
        "account-b legacy mount baseline changed: "
        f"{sorted(unexpected_targets)}"
    )
state_mount = mount_by_target.get("/state")
config_mount = mount_by_target.get("/cfg.json")
expected_state = str(
    (Path(trader_root) / "node-state" / "b").resolve()
)
expected_config = str(
    (Path(trader_root) / "node-b.hk.json").resolve()
)
if state_mount is None or state_mount.get("RW") is not True:
    raise SystemExit("account-b state mount baseline changed")
actual_state = str(
    Path(str(state_mount.get("Source") or "")).resolve()
)
if actual_state != expected_state:
    raise SystemExit("account-b state mount source changed")
if config_mount is None or config_mount.get("RW", True):
    raise SystemExit("account-b config mount baseline changed")
actual_config = str(
    Path(str(config_mount.get("Source") or "")).resolve()
)
if actual_config != expected_config:
    raise SystemExit("account-b config mount source changed")
bindings = (node.get("HostConfig") or {}).get("PortBindings") or {}
expected_binding = bindings.get(f"{node_port}/tcp") or []
if not any(
    str(item.get("HostPort") or "") == node_port
    for item in expected_binding
    if isinstance(item, dict)
):
    raise SystemExit("account-b health port baseline changed")
peer_state = peer.get("State") or {}
if peer_state.get("Running") is not True:
    raise SystemExit("account-a peer container must be running")
peer_config = peer.get("Config") or {}
if peer_config.get("Image") != target_image:
    raise SystemExit("account-a image tag differs from rollout target")
PY
}


resolve_binance_mounts() {
  BINANCE_EXEC_DST="$(
    docker exec "$PEER_CONTAINER" python -c \
      'import nautilus_trader.adapters.binance.execution as module; print(module.__file__)'
  )"
  BINANCE_FUTURES_EXEC_DST="$(
    docker exec "$PEER_CONTAINER" python -c \
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
}


verify_generator_mount_plan() {
  python3 - \
    "$GENERATOR_SOURCE" \
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
        "bundle and recreate mount plans differ: "
        f"missing={missing} extra={extra}"
    )
PY
}


verify_peer_runtime() {
  python3 - "$PEER_TSV" "$PEER_CONTAINER" "$T" <<'PY'
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

manifest_path, container, trader_root = sys.argv[1:]
expected = {}
with open(manifest_path, encoding="utf-8") as source:
    for raw in source:
        bundle_path, target, digest = raw.rstrip("\n").split("\t")
        expected[target] = (bundle_path, digest)
inspect_raw = subprocess.check_output(
    ["docker", "inspect", container],
    text=True,
)
inspected = json.loads(inspect_raw)[0]
mounts = {
    str(item.get("Destination") or ""): item
    for item in inspected.get("Mounts") or []
}
runtime_targets = {target for target in mounts if target in expected}
if runtime_targets != set(expected):
    raise SystemExit(
        "account-a runtime mount set differs from peer manifest"
    )
state_mount = mounts.get("/state")
expected_state_source = str(
    (Path(trader_root) / "node-state" / "a").resolve()
)
if state_mount is None:
    raise SystemExit("account-a state mount is missing")
actual_state_source = str(
    Path(str(state_mount.get("Source") or "")).resolve()
)
if actual_state_source != expected_state_source:
    raise SystemExit("account-a state mount source changed")
if state_mount.get("RW") is not True:
    raise SystemExit("account-a state mount must be writable")
config_mount = mounts.get("/cfg.json")
expected_config_source = str(
    (Path(trader_root) / "node-a.hk.json").resolve()
)
if config_mount is None:
    raise SystemExit("account-a config mount is missing")
actual_config_source = str(
    Path(str(config_mount.get("Source") or "")).resolve()
)
if actual_config_source != expected_config_source:
    raise SystemExit("account-a config mount source changed")
if config_mount.get("RW", True):
    raise SystemExit("account-a config mount must be read-only")
for target, (_bundle_path, digest) in expected.items():
    mount = mounts[target]
    if mount.get("RW", True):
        raise SystemExit(f"account-a runtime mount is writable: {target}")
    result = subprocess.run(
        ["docker", "exec", container, "sha256sum", target],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise SystemExit(f"cannot hash account-a runtime target: {target}")
    actual = result.stdout.split()[0]
    if actual != digest:
        raise SystemExit(f"unexpected account-a runtime hash: {target}")
PY
}


exchange_snapshot() {
  docker exec "$POSTGRES_CONTAINER" \
    psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --tuples-only \
    --no-align \
    --field-separator=$'\t' \
    --command "
      SELECT
        to_char(
          updated_at AT TIME ZONE 'UTC',
          'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'
        ),
        ((CURRENT_TIMESTAMP - updated_at) > interval '180 seconds')::text,
        payload::text
      FROM exchange_state_mirror
      WHERE account_id = '$ACCOUNT_ID'
    "
}


verify_exchange_zero() {
  local phase="$1"
  local previous="${2:-}"
  local output="$TEMP_DIR/exchange-$phase.tsv"
  local error_output="$TEMP_DIR/exchange-$phase.error"
  local attempts=1
  local status
  if [ "$phase" = "after" ]; then
    attempts=30
  fi
  for _ in $(seq 1 "$attempts"); do
    exchange_snapshot >"$output"
    if python3 - "$output" "$phase" "$previous" 2>"$error_output" <<'PY'
from datetime import datetime, timezone
import json
import sys

path, phase, previous = sys.argv[1:]
lines = [
    line
    for line in open(path, encoding="utf-8").read().splitlines()
    if line.strip()
]
if len(lines) != 1:
    raise SystemExit("account-b exchange mirror row is missing")
parts = lines[0].split("\t", 2)
if len(parts) != 3:
    raise SystemExit("account-b exchange mirror row is invalid")
updated_at, stale, raw_payload = parts
if stale.strip().lower() != "false":
    raise SystemExit("account-b exchange mirror is stale")
payload = json.loads(raw_payload)
for field in ("positions", "open_orders", "algo_orders"):
    value = payload.get(field)
    if not isinstance(value, list) or value:
        raise SystemExit("account-b exchange state is not zero")
if phase == "after":
    if not previous:
        raise SystemExit("account-b exchange watermark is missing")
    normalized = updated_at.replace("Z", "+00:00")
    previous_normalized = previous.replace("Z", "+00:00")
    current_time = datetime.fromisoformat(normalized)
    previous_time = datetime.fromisoformat(previous_normalized)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    if previous_time.tzinfo is None:
        previous_time = previous_time.replace(tzinfo=timezone.utc)
    if current_time <= previous_time:
        raise SystemExit(
            "account-b exchange mirror did not advance after recreate"
        )
print(updated_at)
PY
    then
      return
    else
      status=$?
    fi
    if [ "$phase" != "after" ]; then
      cat "$error_output" >&2
      return "$status"
    fi
    if ! grep -Eq \
      'mirror is stale|mirror did not advance after recreate' \
      "$error_output"; then
      cat "$error_output" >&2
      return "$status"
    fi
    sleep 5
  done
  cat "$error_output" >&2
  return 1
}


verify_risk_halted() {
  local output="$TEMP_DIR/risk-state.tsv"
  docker exec "$POSTGRES_CONTAINER" \
    psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --tuples-only \
    --no-align \
    --field-separator=$'\t' \
    --command "
      SELECT instrument_id, upper(coalesce(state->>'mode', ''))
      FROM risk_state
      WHERE account_id = '$ACCOUNT_ID'
        AND instrument_id IN ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
      ORDER BY instrument_id
    " >"$output"
  python3 - "$output" <<'PY'
import sys

expected = {
    "BTCUSDT": "HALTED",
    "ETHUSDT": "HALTED",
    "SOLUSDT": "HALTED",
}
actual = {}
for raw in open(sys.argv[1], encoding="utf-8"):
    line = raw.rstrip("\n")
    if not line:
        continue
    parts = line.split("\t")
    if len(parts) != 2:
        raise SystemExit("account-b risk-state row is invalid")
    instrument, mode = parts
    actual[instrument] = mode
if actual != expected:
    raise SystemExit("account-b BTC/ETH/SOL risk modes are not HALTED")
PY
}


verify_quiescent_runtime_state() {
  local redis_keys="$TEMP_DIR/account-b-redis-keys.txt"
  local database_state="$TEMP_DIR/account-b-pending-state.tsv"
  if find "$STATE_DIR" -mindepth 1 -print -quit 2>/dev/null \
    | grep -q .; then
    die "account-b node state is not empty"
  fi
  docker exec "$REDIS_CONTAINER" redis-cli --raw --scan \
    --pattern 'nautilus:account-b:node-b*' >"$redis_keys"
  if [ -s "$redis_keys" ]; then
    die "account-b Redis namespace is not empty"
  fi
  docker exec "$POSTGRES_CONTAINER" \
    psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --tuples-only \
    --no-align \
    --field-separator=$'\t' \
    --command "
      SELECT 'approved_intents', count(*)
      FROM trade_intents
      WHERE account_id = '$ACCOUNT_ID'
        AND status = 'approved'
        AND valid_until > now();
      SELECT 'pending_commands', count(*)
      FROM operator_commands oc
      JOIN command_node_acks na ON na.command_id = oc.command_id
      WHERE na.node_id = '$NODE_ID'
        AND na.status = 'pending'
        AND (
              oc.command_type <> 'RESUME'
              OR (
                  na.expires_at IS NOT NULL
                  AND na.expires_at > now()
              )
            );
    " >"$database_state"
  python3 - "$database_state" <<'PY'
import sys

expected = {
    "approved_intents": 0,
    "pending_commands": 0,
}
actual = {}
for raw in open(sys.argv[1], encoding="utf-8"):
    line = raw.rstrip("\n")
    if not line:
        continue
    name, value = line.split("\t")
    actual[name] = int(value)
if actual != expected:
    raise SystemExit(
        f"account-b pending runtime work is not empty: {actual}"
    )
PY
}


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


snapshot_state_metadata() {
  local state_dir="$1"
  local output="$BACKUP_ROOT/node-state-metadata.json"
  python3 - "$state_dir" "$output" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
output = Path(sys.argv[2])
records = []
if root.exists():
    candidates = [root]
    candidates.extend(sorted(root.rglob("*")))
    for path in candidates:
        details = path.lstat()
        relative = "."
        if path != root:
            relative = path.relative_to(root).as_posix()
        record = {
            "path": relative,
            "mode": stat.S_IMODE(details.st_mode),
            "uid": details.st_uid,
            "gid": details.st_gid,
            "size": details.st_size,
            "mtime_ns": details.st_mtime_ns,
        }
        if path.is_symlink():
            record["type"] = "symlink"
            record["target"] = os.readlink(path)
        elif path.is_file():
            record["type"] = "file"
            record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            record["type"] = "directory"
        else:
            record["type"] = "other"
        records.append(record)
payload = {
    "state_dir": str(root),
    "present": root.exists(),
    "records": records,
}
output.write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}


write_rollback() {
  cat >"$ROLLBACK_PATH" <<'ROLLBACK'
#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(cd "$BACKUP_ROOT/../.." && pwd)"
INDEX="$BACKUP_ROOT/index.tsv"
LOCK="${OPERATION_LOCK:-/var/lock/trader-v3-account-stall-operation.lock}"
NODE_CONTAINER="trader-v3-node-b"
RECREATE_TARGET="$T/recreate-$NODE_CONTAINER.sh"
STATE_DIR="$T/node-state/b"
FAILED_STATE="$BACKUP_ROOT/failed-node-state-$(date -u +%Y%m%dT%H%M%SZ)"

exec 9>"$LOCK"
flock -n 9 || {
  echo "FATAL: another operation holds $LOCK" >&2
  exit 1
}

(
  cd "$BACKUP_ROOT"
  sha256sum -c SHA256SUMS >/dev/null
)

docker rm -f "$NODE_CONTAINER" 2>/dev/null || true

while IFS=$'\t' read -r status target backup_relative; do
  case "$status" in
    present)
      temporary="$(mktemp "$(dirname "$target")/.rollback.XXXXXX")"
      cp -a "$BACKUP_ROOT/$backup_relative" "$temporary"
      mv -fT -- "$temporary" "$target"
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

if [ -e "$STATE_DIR" ] || [ -L "$STATE_DIR" ]; then
  mv "$STATE_DIR" "$FAILED_STATE"
fi
if [ -e "$BACKUP_ROOT/node-state" ]; then
  mkdir -p "$(dirname "$STATE_DIR")"
  cp -a "$BACKUP_ROOT/node-state" "$STATE_DIR"
fi

[ -x "$RECREATE_TARGET" ] || {
  echo "FATAL: restored account-b recreate script is unavailable" >&2
  exit 1
}
grep -Fq "NAUTILUS_INITIAL_TRADING_STATE=HALTED" "$RECREATE_TARGET" || {
  echo "FATAL: restored recreate script lacks HALTED startup" >&2
  exit 1
}
if grep -Eq '(^|[[:space:]])docker[[:space:]]+start([[:space:]]|$)' \
  "$RECREATE_TARGET"; then
  echo "FATAL: restored recreate script starts a legacy container" >&2
  exit 1
fi
bash "$RECREATE_TARGET"

READY_BODY="$BACKUP_ROOT/rollback-ready.json"
for _ in $(seq 1 45); do
  if curl --silent --show-error --max-time 5 \
    "http://127.0.0.1:8082/ready" >"$READY_BODY"; then
    if python3 - "$READY_BODY" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
if str(payload.get("trading_state") or "").upper() != "HALTED":
    raise SystemExit(1)
PY
    then
      echo "rollback recreated account-b in HALTED startup mode"
      exit 0
    fi
  fi
  sleep 2
done
echo "FATAL: rolled-back account-b did not report HALTED" >&2
exit 1
ROLLBACK
  chmod 0700 "$ROLLBACK_PATH"
}


create_backup() {
  [ ! -e "$BACKUP_ROOT" ] \
    || die "backup path already exists: $BACKUP_ROOT"
  mkdir -p "$BACKUP_ROOT/files"
  chmod 0700 "$BACKUP_ROOT"
  : >"$BACKUP_ROOT/index.tsv"

  while IFS=$'\t' read -r bundle_path mount_target expected_sha; do
    : "$mount_target" "$expected_sha"
    backup_target "$PATCH_DIR/$bundle_path"
  done <"$CONTAINER_TSV"
  backup_target "$GENERATOR_TARGET"
  backup_target "$RECREATE_TARGET"
  backup_target "$DEPLOYED_COMMIT_TARGET"

  cp "$TEMP_DIR/container-inspect.json" \
    "$BACKUP_ROOT/container-inspect.json"
  cp "$TEMP_DIR/peer-inspect.json" "$BACKUP_ROOT/peer-inspect.json"
  cp "$MANIFEST" "$BACKUP_ROOT/bundle-manifest.json"
  cp "$CHECKSUMS" "$BACKUP_ROOT/staging-SHA256SUMS"
  snapshot_state_metadata "$STATE_DIR"
  if [ -e "$STATE_DIR" ] || [ -L "$STATE_DIR" ]; then
    cp -a "$STATE_DIR" "$BACKUP_ROOT/node-state"
  fi
  write_rollback

  (
    cd "$BACKUP_ROOT"
    find . -type f ! -name SHA256SUMS -print0 \
      | sort -z \
      | xargs -0 sha256sum >SHA256SUMS
    sha256sum -c SHA256SUMS >/dev/null
  )
  MUTATION_STARTED=1
  echo "== backup complete: $BACKUP_ROOT"
}


atomic_install() {
  local source="$1"
  local target="$2"
  local expected_sha="$3"
  local mode="$4"
  local target_dir
  local temporary
  local actual_sha

  target_dir="$(dirname "$target")"
  mkdir -p "$target_dir"
  if [ -e "$target" ] && [ ! -f "$target" ]; then
    die "atomic install target is not a regular file: $target"
  fi
  if [ -L "$target" ]; then
    die "atomic install target is a symlink: $target"
  fi
  temporary="$(mktemp "$target_dir/.account-b-hardening.XXXXXX")"
  install -m "$mode" "$source" "$temporary"
  actual_sha="$(sha256_file "$temporary")"
  if [ "$actual_sha" != "$expected_sha" ]; then
    rm -f "$temporary"
    die "atomic install SHA256 mismatch: $target"
  fi
  mv -fT -- "$temporary" "$target"
  actual_sha="$(sha256_file "$target")"
  [ "$actual_sha" = "$expected_sha" ] \
    || die "installed SHA256 mismatch: $target"
}


install_container_patches() {
  mkdir -p "$PATCH_DIR"
  while IFS=$'\t' read -r bundle_path mount_target expected_sha; do
    : "$mount_target"
    atomic_install \
      "$STAGING/$bundle_path" \
      "$PATCH_DIR/$bundle_path" \
      "$expected_sha" \
      0644
  done <"$CONTAINER_TSV"
  GENERATOR_SHA="$(sha256_file "$GENERATOR_SOURCE")"
  atomic_install \
    "$GENERATOR_SOURCE" \
    "$GENERATOR_TARGET" \
    "$GENERATOR_SHA" \
    0755
}


generate_recreate() {
  TRADER_ROOT="$T" \
  NODE_MEMORY_LIMIT="$MEMORY_LIMIT" \
  NODE_MEMORY_SWAP_LIMIT="$MEMORY_SWAP_LIMIT" \
    python3 "$GENERATOR_TARGET" \
      "$NODE_CONTAINER" \
      "$BINANCE_EXEC_DST" \
      "$BINANCE_FUTURES_EXEC_DST"

  grep -Fq "NAUTILUS_INITIAL_TRADING_STATE=HALTED" "$RECREATE_TARGET" \
    || die "generated recreate script lacks HALTED startup"
  if grep -Fq "NAUTILUS_INITIAL_TRADING_STATE=ACTIVE" "$RECREATE_TARGET"; then
    die "generated recreate script retains ACTIVE startup"
  fi
  if grep -Eq '(^|[[:space:]])docker[[:space:]]+start([[:space:]]|$)' \
    "$RECREATE_TARGET"; then
    die "generated recreate script starts the legacy container"
  fi
  grep -Fq "$TARGET_IMAGE" "$RECREATE_TARGET" \
    || die "generated recreate script uses the wrong image"
  grep -Fq "$NODE_PORT:$NODE_PORT/tcp" "$RECREATE_TARGET" \
    || die "generated recreate script uses the wrong health port"
}


verify_rollback_source() {
  [ -x "$RECREATE_TARGET" ] \
    || die "existing account-b recreate script is missing"
  grep -Fq "NAUTILUS_INITIAL_TRADING_STATE=HALTED" "$RECREATE_TARGET" \
    || die "existing account-b recreate script lacks HALTED startup"
  if grep -Eq '(^|[[:space:]])docker[[:space:]]+start([[:space:]]|$)' \
    "$RECREATE_TARGET"; then
    die "existing account-b recreate script starts the legacy container"
  fi
}


verify_memory_and_image() {
  local resource_values
  local image_values
  local restart_count
  resource_values="$(
    docker inspect --format \
      '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' \
      "$NODE_CONTAINER"
  )"
  image_values="$(
    docker inspect --format '{{.Config.Image}} {{.Image}}' "$NODE_CONTAINER"
  )"
  restart_count="$(
    docker inspect --format '{{.RestartCount}}' "$NODE_CONTAINER"
  )"
  python3 - \
    "$MEMORY_LIMIT" \
    "$MEMORY_SWAP_LIMIT" \
    "$resource_values" \
    "$image_values" \
    "$TARGET_IMAGE" \
    "$TARGET_IMAGE_ID" \
    "$restart_count" <<'PY'
import re
import sys


def parse(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([bkmg]?)", value.lower())
    if match is None:
        raise SystemExit(f"invalid memory value: {value}")
    number = int(match.group(1))
    exponent = {
        "": 0,
        "b": 0,
        "k": 1,
        "m": 2,
        "g": 3,
    }[match.group(2)]
    return number * (1024 ** exponent)


actual = sys.argv[3].split()
if len(actual) != 2:
    raise SystemExit("Docker memory inspection is invalid")
if int(actual[0]) != parse(sys.argv[1]):
    raise SystemExit("account-b Docker memory limit mismatch")
if int(actual[1]) != parse(sys.argv[2]):
    raise SystemExit("account-b Docker memory-swap limit mismatch")
image = sys.argv[4].split()
if len(image) != 2:
    raise SystemExit("Docker image inspection is invalid")
if image[0] != sys.argv[5]:
    raise SystemExit("account-b Docker image tag mismatch")
if image[1] != sys.argv[6]:
    raise SystemExit("account-b Docker image ID mismatch")
if int(sys.argv[7]) != 0:
    raise SystemExit("account-b Docker restart count is not zero")
PY
}


verify_ready_halted() {
  local ready_body="$TEMP_DIR/ready-after.json"
  for _ in $(seq 1 45); do
    if curl --silent --show-error --max-time 5 \
      "http://127.0.0.1:$NODE_PORT/ready" >"$ready_body"; then
      if python3 - "$ready_body" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
if str(payload.get("trading_state") or "").upper() != "HALTED":
    raise SystemExit(1)
if payload.get("ready") is not True:
    raise SystemExit(1)
PY
      then
        return
      fi
    fi
    sleep 2
  done
  die "account-b /ready did not report HALTED"
}


verify_node_heartbeat() {
  local observer_token
  local nodes_body="$TEMP_DIR/nodes-after.json"
  observer_token="$(load_env_value SYSTEM_OBSERVER_TOKEN)"
  curl \
    --silent \
    --show-error \
    --fail \
    --max-time 5 \
    --header "Authorization: Bearer $observer_token" \
    "http://127.0.0.1:8080/v1/nodes" >"$nodes_body"
  python3 - "$nodes_body" "$NODE_ID" "$ACCOUNT_ID" <<'PY'
import json
import sys

path, node_id, account_id = sys.argv[1:]
with open(path, encoding="utf-8") as source:
    payload = json.load(source)
matches = [
    item
    for item in payload.get("nodes") or []
    if isinstance(item, dict) and item.get("node_id") == node_id
]
if len(matches) != 1:
    raise SystemExit("account-b heartbeat row is missing")
node = matches[0]
if node.get("account_id") != account_id:
    raise SystemExit("account-b heartbeat account identity changed")
if str(node.get("trading_state") or "").upper() != "HALTED":
    raise SystemExit("account-b reported trading state must be HALTED")
if node.get("heartbeat_stale") is not False:
    raise SystemExit("heartbeat_stale must be false")
if node.get("operational_state") != "ONLINE":
    raise SystemExit("operational_state must be ONLINE")
if node.get("effective_readiness") is not True:
    raise SystemExit("effective_readiness must be true")
if node.get("admission_eligible") is not False:
    raise SystemExit("admission_eligible must be false")
if str(node.get("reconciliation_state") or "").lower() != "healthy":
    raise SystemExit("reconciliation_state must be healthy")
if node.get("process_liveness") is not True:
    raise SystemExit("process_liveness must be true")
PY
}


verify_observability_logs() {
  local log_output="$TEMP_DIR/account-b-observability.log"
  for _ in $(seq 1 30); do
    docker logs "$NODE_CONTAINER" >"$log_output" 2>&1
    if grep -Fq '"event":"control_plane_session.started"' "$log_output" \
      && grep -Fq '"event":"control_plane_session.health"' "$log_output"; then
      return
    fi
    sleep 2
  done
  die "account-b structured control-plane logs are missing"
}


verify_runtime_mounts() {
  python3 - \
    "$CONTAINER_TSV" \
    "$PEER_TSV" \
    "$DELTA_TSV" \
    "$T" \
    "$NODE_CONTAINER" <<'PY'
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

release_path, peer_path, delta_path, trader_root, container = sys.argv[1:]


def read_manifest(path: str) -> dict[str, tuple[str, str]]:
    result = {}
    with open(path, encoding="utf-8") as source:
        for raw in source:
            bundle_path, target, digest = raw.rstrip("\n").split("\t")
            result[bundle_path] = (target, digest)
    return result


release = read_manifest(release_path)
peer = read_manifest(peer_path)
with open(delta_path, encoding="utf-8") as source:
    delta_bundle, delta_target, delta_peer, delta_release = (
        source.read().rstrip("\n").split("\t")
    )
inspect_raw = subprocess.check_output(
    ["docker", "inspect", container],
    text=True,
)
inspected = json.loads(inspect_raw)[0]
mounts = {
    str(item.get("Destination") or ""): item
    for item in inspected.get("Mounts") or []
}
runtime_targets = {
    target
    for target in mounts
    if target.startswith("/app/")
    or target in {value[0] for value in release.values()}
}
expected_targets = {
    target
    for target, _digest in release.values()
}
if runtime_targets != expected_targets:
    raise SystemExit("account-b runtime mount set differs from manifest")
state_mount = mounts.get("/state")
expected_state_source = str(
    (Path(trader_root) / "node-state" / "b").resolve()
)
if state_mount is None:
    raise SystemExit("account-b state mount is missing")
actual_state_source = str(
    Path(str(state_mount.get("Source") or "")).resolve()
)
if actual_state_source != expected_state_source:
    raise SystemExit("account-b state mount source changed")
if state_mount.get("RW") is not True:
    raise SystemExit("account-b state mount must be writable")
for bundle_path, (target, digest) in release.items():
    mount = mounts.get(target)
    if mount is None:
        raise SystemExit(f"missing container mount: {target}")
    expected_source = str(
        (
            Path(trader_root)
            / "container-patches"
            / bundle_path
        ).resolve()
    )
    actual_source = str(Path(str(mount.get("Source") or "")).resolve())
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
    if actual_hash != digest:
        raise SystemExit(f"container target SHA256 mismatch: {target}")
    peer_hash = peer[bundle_path][1]
    if bundle_path == delta_bundle:
        if (
            target != delta_target
            or peer_hash != delta_peer
            or digest != delta_release
        ):
            raise SystemExit("observability delta binding changed")
    elif peer_hash != digest:
        raise SystemExit(
            f"unexpected account-a/account-b hash delta: {target}"
        )
PY

  docker exec "$NODE_CONTAINER" cat /proc/1/mountinfo \
    >"$TEMP_DIR/account-b-mountinfo.txt"
  python3 - "$CONTAINER_TSV" "$TEMP_DIR/account-b-mountinfo.txt" <<'PY'
import sys

targets = set()
with open(sys.argv[1], encoding="utf-8") as source:
    for raw in source:
        _bundle_path, target, _digest = raw.rstrip("\n").split("\t")
        targets.add(target)
mountinfo = open(sys.argv[2], encoding="utf-8").read()
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
}


write_deployed_marker() {
  local marker="$TEMP_DIR/DEPLOYED_COMMIT.txt"
  local marker_sha
  printf '%s account-b-hardening deployed=%s peer=%s\n' \
    "$RELEASE_COMMIT" \
    "$STAMP" \
    "$EXPECTED_PEER_BASELINE_COMMIT" >"$marker"
  marker_sha="$(sha256_file "$marker")"
  atomic_install \
    "$marker" \
    "$DEPLOYED_COMMIT_TARGET" \
    "$marker_sha" \
    0644
}


main() {
  exec 9>"$OPERATION_LOCK"
  flock -n 9 || die "another operation holds $OPERATION_LOCK"

  for command in \
    awk cp curl docker find flock grep id install mktemp mv \
    python3 sha256sum sort xargs; do
    command -v "$command" >/dev/null \
      || die "required command missing: $command"
  done
  [ "$(id -u)" = "0" ] || die "deployment must run as root"
  [ -d "$T" ] || die "trader root is missing: $T"
  [ -f "$T/.env.v3" ] || die "control-plane environment file is missing"
  [ -f "$MANIFEST" ] || die "bundle manifest is missing: $MANIFEST"
  [ -f "$CHECKSUMS" ] || die "bundle checksums are missing: $CHECKSUMS"
  [ -f "$GENERATOR_SOURCE" ] || die "recreate generator is missing"

  TEMP_DIR="$(mktemp -d)"
  CONTAINER_TSV="$TEMP_DIR/container.tsv"
  PEER_TSV="$TEMP_DIR/peer.tsv"
  DELTA_TSV="$TEMP_DIR/delta.tsv"

  validate_bundle
  TARGET_IMAGE_ID="$(
    docker image inspect --format '{{.Id}}' "$TARGET_IMAGE"
  )"
  verify_stopped_baseline
  verify_rollback_source
  resolve_binance_mounts
  verify_generator_mount_plan
  verify_peer_runtime
  EXCHANGE_BEFORE="$(verify_exchange_zero "before")"
  verify_risk_halted
  verify_quiescent_runtime_state
  create_backup
  install_container_patches
  generate_recreate
  bash "$RECREATE_TARGET"
  verify_memory_and_image
  verify_ready_halted
  verify_node_heartbeat
  verify_observability_logs
  verify_runtime_mounts
  verify_exchange_zero "after" "$EXCHANGE_BEFORE" >/dev/null
  verify_risk_halted
  write_deployed_marker

  echo "== deployed commit: $RELEASE_COMMIT"
  echo "== account-b is HALTED; health, peer, mount, inode, memory and exchange gates passed"
  echo "== rollback: bash $ROLLBACK_PATH"
}


main "$@"
