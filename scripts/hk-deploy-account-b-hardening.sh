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
EXPECTED_GENERATOR_SHA256="0a0026cd307080c57037b94726f83533930a60711756e957f79f1f38afe4a1df"
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
PEER_PATCH_DIR="$T/container-patches"
GENERATOR_SOURCE="$STAGING/tools/hk-gen-recreate-patched.py"
GENERATOR_TARGET="$T/hk-gen-recreate-patched.py"
RECREATE_TARGET="$T/recreate-$NODE_CONTAINER.sh"
STATE_DIR="$T/node-state/b"
DEPLOYED_COMMIT_TARGET="$T/DEPLOYED_COMMIT.txt"
TEMP_DIR=""
CONTAINER_TSV=""
PEER_TSV=""
DELTA_TSV=""
LEGACY_MOUNTS_TSV=""
RELEASE_MOUNTS_TSV=""
RECREATE_VALIDATOR=""
RELEASE_COMMIT=""
TARGET_IMAGE_ID=""
B_RELEASE_ROOT=""
B_PATCH_DIR=""
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
      "$EXPECTED_OBSERVABILITY_TARGET" \
      "$EXPECTED_GENERATOR_SHA256" <<'PY'
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
    expected_generator_sha,
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

deployment_files = manifest.get("deployment_files")
if not isinstance(deployment_files, list):
    raise SystemExit("account-b deployment files are invalid")
deployment_hashes = {}
for item in deployment_files:
    if not isinstance(item, dict):
        raise SystemExit("account-b deployment file item is invalid")
    bundle_path = relative(
        item.get("bundle_path"),
        "deployment bundle_path",
    )
    digest = str(item.get("sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise SystemExit(
            f"deployment SHA256 is invalid: {bundle_path}"
        )
    deployment_hashes[bundle_path] = digest
required_tools = {
    "tools/hk-deploy-account-b-hardening.sh",
    "tools/hk-gen-recreate-patched.py",
}
if not required_tools.issubset(deployment_hashes):
    raise SystemExit("account-b deployment tools are incomplete")
if (
    deployment_hashes["tools/hk-gen-recreate-patched.py"]
    != expected_generator_sha
):
    raise SystemExit("account-b recreate generator hash is invalid")
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
host_config = node.get("HostConfig") or {}
bindings = host_config.get("PortBindings") or {}
expected_binding = bindings.get(f"{node_port}/tcp") or []
published = any(
    str(item.get("HostPort") or "") == node_port
    for item in expected_binding
    if isinstance(item, dict)
)
environment = {
    str(value)
    for value in config.get("Env") or []
}
host_network = str(host_config.get("NetworkMode") or "") == "host"
host_health = f"NAUTILUS_HEALTH_PORT={node_port}" in environment
if not published and not (host_network and host_health):
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
expected_mount_targets = set(expected) | {"/state", "/cfg.json"}
if set(mounts) != expected_mount_targets:
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
for target, (bundle_path, digest) in expected.items():
    mount = mounts[target]
    expected_source = str(
        (
            Path(trader_root)
            / "container-patches"
            / bundle_path
        ).resolve()
    )
    actual_source = str(
        Path(str(mount.get("Source") or "")).resolve()
    )
    if actual_source != expected_source:
        raise SystemExit(
            f"account-a runtime mount source changed: {target}"
        )
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
        ((CURRENT_TIMESTAMP - updated_at) > interval '90 seconds')::text,
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
        AND na.status = 'pending';
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


write_recreate_validator() {
  cat >"$RECREATE_VALIDATOR" <<'PY'
from __future__ import annotations

from pathlib import Path
import shlex
import sys


_VALUE_OPTIONS = {
    "--entrypoint",
    "--env",
    "--hostname",
    "--memory",
    "--memory-swap",
    "--name",
    "--network",
    "--network-alias",
    "--publish",
    "--restart",
    "--user",
    "--volume",
    "--workdir",
    "-e",
    "-v",
}
_FLAG_OPTIONS = {
    "--detach",
    "--init",
    "--read-only",
    "-d",
}
_PREFIX_OPTIONS = tuple(
    f"{name}="
    for name in _VALUE_OPTIONS
    if name.startswith("--")
)


def _run_image_index(tokens: list[str]) -> int:
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            if index + 1 >= len(tokens):
                raise SystemExit("recreate Docker image is missing")
            return index + 1
        if token in _FLAG_OPTIONS:
            index += 1
            continue
        if token in _VALUE_OPTIONS:
            if index + 1 >= len(tokens):
                raise SystemExit(
                    f"recreate Docker option lacks value: {token}"
                )
            index += 2
            continue
        if token.startswith(_PREFIX_OPTIONS):
            index += 1
            continue
        if token.startswith("-"):
            raise SystemExit(
                f"recreate Docker option is not allowed: {token}"
            )
        return index
    raise SystemExit("recreate Docker image is missing")


def _option_values(
    tokens: list[str],
    image_index: int,
    names: set[str],
) -> list[str]:
    values = []
    index = 2
    while index < image_index:
        token = tokens[index]
        if token in names:
            values.append(tokens[index + 1])
            index += 2
            continue
        matched = False
        for name in names:
            prefix = f"{name}="
            if token.startswith(prefix):
                values.append(token[len(prefix):])
                matched = True
                break
        if matched or token in _FLAG_OPTIONS or token.startswith(_PREFIX_OPTIONS):
            index += 1
            continue
        if token in _VALUE_OPTIONS:
            index += 2
            continue
        raise SystemExit(f"recreate Docker option parse failed: {token}")
    return values


def _read_mount_plan(path: str) -> dict[str, tuple[str, str]]:
    mounts = {}
    with open(path, encoding="utf-8") as source:
        for raw in source:
            source_path, target, mode = raw.rstrip("\n").split("\t")
            if target in mounts:
                raise SystemExit(
                    f"recreate mount plan target is duplicated: {target}"
                )
            mounts[target] = (source_path, mode)
    return mounts


def _run_mounts(
    tokens: list[str],
    image_index: int,
) -> dict[str, tuple[str, str]]:
    mounts = {}
    for value in _option_values(
        tokens,
        image_index,
        {"-v", "--volume"},
    ):
        parts = value.rsplit(":", 2)
        if len(parts) != 3:
            raise SystemExit("recreate volume argument is invalid")
        source, target, mode = parts
        if target in mounts:
            raise SystemExit(
                f"recreate mount target is duplicated: {target}"
            )
        mounts[target] = (source, mode)
    return mounts


def validate(
    path: str,
    mount_plan: str,
    container: str,
    image: str,
    node_port: str,
    state_dir: str,
) -> None:
    expected_mounts = _read_mount_plan(mount_plan)
    run_count = 0
    remove_count = 0
    state_count = 0
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in {"set -Eeuo pipefail", "set -euo pipefail"}:
            continue
        tokens = shlex.split(line)
        if tokens[:3] == ["docker", "rm", "-f"]:
            expected_remove = (
                f"docker rm -f {shlex.quote(container)} "
                "2>/dev/null || true"
            )
            if line != expected_remove:
                raise SystemExit(
                    "recreate docker rm command is invalid"
                )
            remove_count += 1
            continue
        if line != shlex.join(tokens):
            raise SystemExit("recreate command is not canonical")
        if tokens[:2] == ["mkdir", "-p"]:
            if tokens[2:] != [state_dir]:
                raise SystemExit(
                    "recreate state mkdir command is invalid"
                )
            state_count += 1
            continue
        if tokens[:2] == ["docker", "run"]:
            run_count += 1
            image_index = _run_image_index(tokens)
            if tokens[image_index] != image:
                raise SystemExit("recreate Docker image is invalid")
            names = _option_values(
                tokens,
                image_index,
                {"--name"},
            )
            if names != [container]:
                raise SystemExit("recreate Docker name is invalid")
            environments = _option_values(
                tokens,
                image_index,
                {"-e", "--env"},
            )
            initial_states = [
                value
                for value in environments
                if value.startswith(
                    "NAUTILUS_INITIAL_TRADING_STATE="
                )
            ]
            if initial_states != [
                "NAUTILUS_INITIAL_TRADING_STATE=HALTED"
            ]:
                raise SystemExit(
                    "recreate HALTED startup is invalid"
                )
            state_values = [
                value
                for value in environments
                if value.startswith("NODE_STATE_DIR=")
            ]
            if (
                not state_values
                or set(state_values) != {"NODE_STATE_DIR=/state"}
            ):
                raise SystemExit(
                    "recreate node state environment is invalid"
                )
            networks = _option_values(
                tokens,
                image_index,
                {"--network"},
            )
            published = _option_values(
                tokens,
                image_index,
                {"--publish"},
            )
            expected_port = f"{node_port}:{node_port}/tcp"
            port_is_published = any(
                value == expected_port
                or value.endswith(f":{expected_port}")
                for value in published
            )
            health_values = [
                value
                for value in environments
                if value.startswith("NAUTILUS_HEALTH_PORT=")
            ]
            host_health = (
                "host" in networks
                and f"NAUTILUS_HEALTH_PORT={node_port}"
                in health_values
            )
            if not port_is_published and not host_health:
                raise SystemExit(
                    "recreate health port is invalid"
                )
            if _run_mounts(tokens, image_index) != expected_mounts:
                raise SystemExit(
                    "recreate mount plan is invalid"
                )
            continue
        if tokens[:3] == ["docker", "network", "connect"]:
            if tokens[-1] != container:
                raise SystemExit(
                    "recreate network target is invalid"
                )
            continue
        raise SystemExit(
            f"recreate command is not allowed: {tokens[:3]}"
        )
    if remove_count != 1 or state_count != 1 or run_count != 1:
        raise SystemExit("recreate command set is incomplete")


def rewrite_image(
    path: str,
    current_image: str,
    immutable_image: str,
) -> None:
    lines = []
    rewritten = 0
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        tokens = shlex.split(stripped)
        if tokens[:2] != ["docker", "run"]:
            lines.append(line)
            continue
        if stripped != shlex.join(tokens):
            raise SystemExit("recreate command is not canonical")
        image_index = _run_image_index(tokens)
        if tokens[image_index] not in {
            current_image,
            immutable_image,
        }:
            raise SystemExit("recreate Docker image is invalid")
        tokens[image_index] = immutable_image
        lines.append(shlex.join(tokens))
        rewritten += 1
    if rewritten != 1:
        raise SystemExit("recreate Docker image rewrite is incomplete")
    Path(path).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


command = sys.argv[1]
if command == "validate":
    validate(*sys.argv[2:])
elif command == "rewrite-image":
    rewrite_image(*sys.argv[2:])
else:
    raise SystemExit(f"unknown recreate validator command: {command}")
PY
  chmod 0700 "$RECREATE_VALIDATOR"
}


validate_recreate_script() {
  local script_path="$1"
  local mount_plan="$2"
  local image="$3"
  python3 \
    "$RECREATE_VALIDATOR" \
    validate \
    "$script_path" \
    "$mount_plan" \
    "$NODE_CONTAINER" \
    "$image" \
    "$NODE_PORT" \
    "$STATE_DIR"
}


write_recreate_mount_plans() {
  python3 - \
    "$TEMP_DIR/container-inspect.json" \
    "$CONTAINER_TSV" \
    "$LEGACY_MOUNTS_TSV" \
    "$RELEASE_MOUNTS_TSV" \
    "$B_PATCH_DIR" \
    "$STATE_DIR" \
    "$T/node-b.hk.json" <<'PY'
import json
from pathlib import Path
import sys

(
    inspect_path,
    manifest_path,
    legacy_path,
    release_path,
    release_patch_dir,
    state_dir,
    config_path,
) = sys.argv[1:]
with open(inspect_path, encoding="utf-8") as source:
    inspected = json.load(source)[0]


def write_plan(path: str, mounts: dict[str, tuple[str, str]]) -> None:
    with open(path, "w", encoding="utf-8") as output:
        for target in sorted(mounts):
            source, mode = mounts[target]
            output.write(f"{source}\t{target}\t{mode}\n")


legacy = {}
for mount in inspected.get("Mounts") or []:
    source = str(mount.get("Source") or "")
    target = str(mount.get("Destination") or "")
    if not source or not target or target in legacy:
        raise SystemExit("account-b legacy mount plan is invalid")
    mode = "rw"
    if not mount.get("RW", True):
        mode = "ro"
    legacy[target] = (source, mode)

release = {
    "/state": (state_dir, "rw"),
    "/cfg.json": (config_path, "ro"),
}
with open(manifest_path, encoding="utf-8") as source:
    for raw in source:
        bundle_path, target, _digest = raw.rstrip("\n").split("\t")
        if target in release:
            raise SystemExit("account-b release mount plan is invalid")
        release[target] = (
            str(Path(release_patch_dir) / bundle_path),
            "ro",
        )

write_plan(legacy_path, legacy)
write_plan(release_path, release)
PY
}


rewrite_recreate_mounts() {
  python3 - \
    "$RECREATE_TARGET" \
    "$CONTAINER_TSV" \
    "$PEER_PATCH_DIR" \
    "$B_PATCH_DIR" \
    "$TARGET_IMAGE" <<'PY'
from pathlib import Path
import shlex
import sys

(
    script_path,
    manifest_path,
    peer_patch_dir,
    release_patch_dir,
    current_image,
) = sys.argv[1:]
bundle_by_target = {}
with open(manifest_path, encoding="utf-8") as source:
    for raw in source:
        bundle_path, target, _digest = raw.rstrip("\n").split("\t")
        bundle_by_target[target] = bundle_path

rewritten_targets = set()
lines = []
for raw in open(script_path, encoding="utf-8"):
    line = raw.rstrip("\n")
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped == "set -Eeuo pipefail":
        lines.append(line)
        continue
    tokens = shlex.split(stripped)
    if tokens[:2] != ["docker", "run"]:
        lines.append(line)
        continue
    image_indexes = [
        index
        for index, token in enumerate(tokens)
        if token == current_image
    ]
    if len(image_indexes) != 1:
        raise SystemExit("generated Docker image is invalid")
    image_index = image_indexes[0]
    index = 0
    while index < image_index:
        token = tokens[index]
        if token not in {"-v", "--volume"}:
            index += 1
            continue
        if index + 1 >= len(tokens):
            raise SystemExit("recreate volume argument is incomplete")
        source, target, mode = tokens[index + 1].split(":", 2)
        bundle_path = bundle_by_target.get(target)
        if bundle_path is None:
            index += 2
            continue
        expected_source = str(Path(peer_patch_dir) / bundle_path)
        if source != expected_source or mode != "ro":
            raise SystemExit(
                f"generated runtime mount is invalid: {target}"
            )
        tokens[index + 1] = (
            f"{Path(release_patch_dir) / bundle_path}:{target}:ro"
        )
        rewritten_targets.add(target)
        index += 2
    lines.append(shlex.join(tokens))

if rewritten_targets != set(bundle_by_target):
    missing = sorted(set(bundle_by_target) - rewritten_targets)
    raise SystemExit(f"recreate runtime mounts were not rewritten: {missing}")
Path(script_path).write_text(
    "\n".join(lines) + "\n",
    encoding="utf-8",
)
PY
  chmod 0700 "$RECREATE_TARGET"
}


write_rollback() {
  cat >"$ROLLBACK_PATH" <<'ROLLBACK'
#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INDEX="$BACKUP_ROOT/index.tsv"
IFS=$'\t' read -r \
  T NODE_PORT B_RELEASE_ROOT DEFAULT_OPERATION_LOCK TARGET_IMAGE_ID \
  <"$BACKUP_ROOT/rollback-config.tsv"
LOCK="${OPERATION_LOCK:-$DEFAULT_OPERATION_LOCK}"
NODE_CONTAINER="trader-v3-node-b"
TARGET_IMAGE="trader-bot/nautilus-node:b10-verify"
RECREATE_TARGET="$T/recreate-$NODE_CONTAINER.sh"
STATE_DIR="$T/node-state/b"
RECREATE_VALIDATOR="$BACKUP_ROOT/validate-recreate.py"
LEGACY_MOUNTS_TSV="$BACKUP_ROOT/legacy-mounts.tsv"
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
rm -rf -- "$B_RELEASE_ROOT"

[ -x "$RECREATE_TARGET" ] || {
  echo "FATAL: restored account-b recreate script is unavailable" >&2
  exit 1
}
[ -x "$RECREATE_VALIDATOR" ] || {
  echo "FATAL: recreate validator is unavailable" >&2
  exit 1
}
python3 \
  "$RECREATE_VALIDATOR" \
  rewrite-image \
  "$RECREATE_TARGET" \
  "$TARGET_IMAGE" \
  "$TARGET_IMAGE_ID"
python3 \
  "$RECREATE_VALIDATOR" \
  validate \
  "$RECREATE_TARGET" \
  "$LEGACY_MOUNTS_TSV" \
  "$NODE_CONTAINER" \
  "$TARGET_IMAGE_ID" \
  "$NODE_PORT" \
  "$STATE_DIR"
bash "$RECREATE_TARGET"

READY_BODY="$BACKUP_ROOT/rollback-ready.json"
for _ in $(seq 1 45); do
  if curl --silent --show-error --max-time 5 \
    "http://127.0.0.1:$NODE_PORT/ready" >"$READY_BODY"; then
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
  [ ! -e "$B_RELEASE_ROOT" ] \
    || die "account-b release path already exists: $B_RELEASE_ROOT"
  mkdir -p "$BACKUP_ROOT/files"
  chmod 0700 "$BACKUP_ROOT"
  : >"$BACKUP_ROOT/index.tsv"

  backup_target "$GENERATOR_TARGET"
  backup_target "$RECREATE_TARGET"
  backup_target "$DEPLOYED_COMMIT_TARGET"
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "$T" \
    "$NODE_PORT" \
    "$B_RELEASE_ROOT" \
    "$OPERATION_LOCK" \
    "$TARGET_IMAGE_ID" >"$BACKUP_ROOT/rollback-config.tsv"

  cp "$TEMP_DIR/container-inspect.json" \
    "$BACKUP_ROOT/container-inspect.json"
  cp "$TEMP_DIR/peer-inspect.json" "$BACKUP_ROOT/peer-inspect.json"
  cp "$RECREATE_VALIDATOR" "$BACKUP_ROOT/validate-recreate.py"
  cp "$LEGACY_MOUNTS_TSV" "$BACKUP_ROOT/legacy-mounts.tsv"
  cp "$RELEASE_MOUNTS_TSV" "$BACKUP_ROOT/release-mounts.tsv"
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
  mkdir -p "$B_PATCH_DIR"
  while IFS=$'\t' read -r bundle_path mount_target expected_sha; do
    : "$mount_target"
    atomic_install \
      "$STAGING/$bundle_path" \
      "$B_PATCH_DIR/$bundle_path" \
      "$expected_sha" \
      0644
  done <"$CONTAINER_TSV"
  atomic_install \
    "$GENERATOR_SOURCE" \
    "$GENERATOR_TARGET" \
    "$EXPECTED_GENERATOR_SHA256" \
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

  rewrite_recreate_mounts
  python3 \
    "$RECREATE_VALIDATOR" \
    rewrite-image \
    "$RECREATE_TARGET" \
    "$TARGET_IMAGE" \
    "$TARGET_IMAGE_ID"
  validate_recreate_script \
    "$RECREATE_TARGET" \
    "$RELEASE_MOUNTS_TSV" \
    "$TARGET_IMAGE_ID"
}


verify_rollback_source() {
  [ -x "$RECREATE_TARGET" ] \
    || die "existing account-b recreate script is missing"
  validate_recreate_script \
    "$RECREATE_TARGET" \
    "$LEGACY_MOUNTS_TSV" \
    "$TARGET_IMAGE"
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
    raise SystemExit("account-b Docker image reference mismatch")
if image[1] != sys.argv[5]:
    raise SystemExit("account-b Docker image ID mismatch")
if int(sys.argv[6]) != 0:
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
    "$B_PATCH_DIR" \
    "$NODE_CONTAINER" <<'PY'
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

(
    release_path,
    peer_path,
    delta_path,
    trader_root,
    release_patch_dir,
    container,
) = sys.argv[1:]


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
expected_targets = {
    target
    for target, _digest in release.values()
}
expected_mount_targets = expected_targets | {"/state", "/cfg.json"}
if set(mounts) != expected_mount_targets:
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
config_mount = mounts.get("/cfg.json")
expected_config_source = str(
    (Path(trader_root) / "node-b.hk.json").resolve()
)
if config_mount is None:
    raise SystemExit("account-b config mount is missing")
actual_config_source = str(
    Path(str(config_mount.get("Source") or "")).resolve()
)
if actual_config_source != expected_config_source:
    raise SystemExit("account-b config mount source changed")
if config_mount.get("RW", True):
    raise SystemExit("account-b config mount must be read-only")
for bundle_path, (target, digest) in release.items():
    mount = mounts.get(target)
    if mount is None:
        raise SystemExit(f"missing container mount: {target}")
    expected_source = str(
        (Path(release_patch_dir) / bundle_path).resolve()
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
  LEGACY_MOUNTS_TSV="$TEMP_DIR/legacy-mounts.tsv"
  RELEASE_MOUNTS_TSV="$TEMP_DIR/release-mounts.tsv"
  RECREATE_VALIDATOR="$TEMP_DIR/validate-recreate.py"

  validate_bundle
  [ "$(sha256_file "$GENERATOR_SOURCE")" = "$EXPECTED_GENERATOR_SHA256" ] \
    || die "recreate generator SHA256 differs from the reviewed baseline"
  B_RELEASE_ROOT="$T/account-b-releases/$RELEASE_COMMIT"
  B_PATCH_DIR="$B_RELEASE_ROOT/container-patches"
  TARGET_IMAGE_ID="$(
    docker image inspect --format '{{.Id}}' "$TARGET_IMAGE"
  )"
  verify_stopped_baseline
  write_recreate_validator
  write_recreate_mount_plans
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
  verify_stopped_baseline
  EXCHANGE_BEFORE="$(verify_exchange_zero "before")"
  verify_risk_halted
  verify_quiescent_runtime_state
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
