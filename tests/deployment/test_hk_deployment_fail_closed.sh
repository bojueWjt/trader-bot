#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
ROOT_WINDOW="$REPO_ROOT/scripts/hk-root-window-20260724.sh"
VERIFY="$REPO_ROOT/scripts/verify_hk_deployment.sh"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_contains() {
  local output="$1"
  local expected="$2"
  if [[ "$output" != *"$expected"* ]]; then
    fail "expected output to contain: $expected"
  fi
}

make_materials() {
  local deploy_dir="$1"
  local files=(
    container-patches/intent_execution_planner.py
    container-patches/contracts.py
    container-patches/projection_actor.py
    container-patches/event_mapper.py
    container-patches/exchange_cancel_adapter.py
    container-patches/node.py
    container-patches/binance_execution.py
    container-patches/intent_execution_strategy_MERGED.py
    control-plane/read_api_MERGED.py
    control-plane/repository.py
    control-plane/migrate.py
    control-plane/order_reducer.py
    control-plane/position_reducer.py
    control-plane/exchange_state_recorder.py
    control-plane/order_state.v1.json
    scripts/order_lifecycle_monitor.py
    scripts/hermes_signal_feeder.py
    scripts/rebuild_positions_projection.py
    hk-gen-recreate-patched.py
  )
  local relative_path
  for relative_path in "${files[@]}"; do
    mkdir -p "$deploy_dir/$(dirname "$relative_path")"
    printf 'fixture\n' >"$deploy_dir/$relative_path"
  done
}

make_empty_docker() {
  local fake_bin="$1"
  mkdir -p "$fake_bin"
  cat >"$fake_bin/docker" <<'EOF'
#!/bin/sh
case "$1" in
  logs)
    exit 0
    ;;
  inspect)
    printf '{}\n'
    exit 0
    ;;
  exec)
    exit 0
    ;;
esac
exit 1
EOF
  chmod +x "$fake_bin/docker"
}

test_root_window_preflight_covers_stage5_inputs() {
  local required_files=(
    container-patches/intent_execution_strategy_MERGED.py
    control-plane/read_api_MERGED.py
    container-patches/contracts.py
    container-patches/projection_actor.py
    container-patches/binance_execution.py
  )
  local relative_path
  local index=0
  for relative_path in "${required_files[@]}"; do
    local case_dir="$TMP_DIR/preflight-$index"
    local deploy_dir="$case_dir/deploy"
    local trader_root="$case_dir/trader-v3"
    local fake_bin="$case_dir/bin"
    local meminfo="$case_dir/meminfo"
    make_materials "$deploy_dir"
    rm "$deploy_dir/$relative_path"
    mkdir -p "$trader_root"
    printf 'MemAvailable:    4194304 kB\n' >"$meminfo"
    make_empty_docker "$fake_bin"

    local output
    local status
    set +e
    output=$(
      PATH="$fake_bin:$PATH" \
      DEPLOY_DIR="$deploy_dir" \
      TRADER_ROOT="$trader_root" \
      MEMINFO_PATH="$meminfo" \
      DEPLOY_NONINTERACTIVE=1 \
      bash "$ROOT_WINDOW" 2>&1
    )
    status=$?
    set -e

    if [ "$status" -eq 0 ]; then
      fail "root-window accepted missing stage 5 input: $relative_path"
    fi
    assert_contains "$output" "缺失: $deploy_dir/$relative_path"
    if [[ "$output" == *"===== 预检：内存余量"* ]]; then
      fail "root-window continued after missing stage 5 input: $relative_path"
    fi
    index=$((index + 1))
  done
}

test_root_window_fails_when_binance_destination_is_missing() {
  local case_dir="$TMP_DIR/root-window"
  local deploy_dir="$case_dir/deploy"
  local trader_root="$case_dir/trader-v3"
  local fake_bin="$case_dir/bin"
  local meminfo="$case_dir/meminfo"
  mkdir -p "$fake_bin" "$trader_root"
  make_materials "$deploy_dir"
  printf 'MemAvailable:    4194304 kB\n' >"$meminfo"
  make_empty_docker "$fake_bin"

  local output
  local status
  set +e
  output=$(
    PATH="$fake_bin:$PATH" \
    DEPLOY_DIR="$deploy_dir" \
    TRADER_ROOT="$trader_root" \
    MEMINFO_PATH="$meminfo" \
    DEPLOY_NONINTERACTIVE=1 \
    bash "$ROOT_WINDOW" 2>&1
  )
  status=$?
  set -e

  if [ "$status" -eq 0 ]; then
    fail "root-window accepted an empty BINANCE_EXEC_DST"
  fi
  assert_contains "$output" "FATAL: BINANCE_EXEC_DST"
  if [[ "$output" == *"===== 阶段2"* ]]; then
    fail "root-window continued after BINANCE_EXEC_DST detection failed"
  fi
}

make_fake_ssh() {
  local fake_bin="$1"
  mkdir -p "$fake_bin"
  cat >"$fake_bin/ssh" <<'EOF'
#!/bin/sh
printf '%b' "$FAKE_REMOTE_OUT"
EOF
  chmod +x "$fake_bin/ssh"
}

test_verify_requires_patch_destination() {
  local case_dir="$TMP_DIR/verify-missing-destination"
  local fake_bin="$case_dir/bin"
  local manifest="$case_dir/manifest.txt"
  make_fake_ssh "$fake_bin"
  mkdir -p "$case_dir"
  printf '%064d %s\n' 0 \
    "/srv/trader-v3/container-patches/projection_actor.py" >"$manifest"

  local output
  local status
  set +e
  output=$(PATH="$fake_bin:$PATH" bash "$VERIFY" "$manifest" 2>&1)
  status=$?
  set -e

  if [ "$status" -ne 2 ]; then
    fail "verify should reject a patch manifest entry without destination"
  fi
  assert_contains "$output" "容器目标路径"
}

test_verify_rejects_correct_source_at_wrong_destination() {
  local case_dir="$TMP_DIR/verify-wrong-destination"
  local fake_bin="$case_dir/bin"
  local manifest="$case_dir/manifest.txt"
  local hash
  local source_path="/srv/trader-v3/container-patches/projection_actor.py"
  local expected_dst="/app/projection/actor.py"
  hash=$(printf fixture | sha256sum | awk '{print $1}')
  make_fake_ssh "$fake_bin"
  mkdir -p "$case_dir"
  printf '%s %s %s\n' "$hash" "$source_path" "$expected_dst" >"$manifest"
  export FAKE_REMOTE_OUT
  FAKE_REMOTE_OUT=$(
    printf 'HASH\t%s\t%s\n' "$source_path" "$hash"
    printf 'PIDS\t101 202 \n'
    printf 'MOUNT\t101\t%s\t/app/wrong/actor.py\n' "$source_path"
    printf 'MOUNT\t202\t%s\t/app/wrong/actor.py\n' "$source_path"
  )

  local output
  local status
  set +e
  output=$(PATH="$fake_bin:$PATH" bash "$VERIFY" "$manifest" 2>&1)
  status=$?
  set -e

  if [ "$status" -ne 1 ]; then
    fail "verify accepted the correct source mounted at the wrong destination"
  fi
  assert_contains "$output" "期望目标 $expected_dst"
}

test_verify_accepts_exact_source_destination_pairs() {
  local case_dir="$TMP_DIR/verify-exact-pair"
  local fake_bin="$case_dir/bin"
  local manifest="$case_dir/manifest.txt"
  local hash
  local source_path="/srv/trader-v3/container-patches/projection_actor.py"
  local expected_dst="/app/projection/actor.py"
  hash=$(printf fixture | sha256sum | awk '{print $1}')
  make_fake_ssh "$fake_bin"
  mkdir -p "$case_dir"
  printf '%s %s %s\n' "$hash" "$source_path" "$expected_dst" >"$manifest"
  export FAKE_REMOTE_OUT
  FAKE_REMOTE_OUT=$(
    printf 'HASH\t%s\t%s\n' "$source_path" "$hash"
    printf 'PIDS\t101 202 \n'
    printf 'MOUNT\t101\t%s\t%s\n' "$source_path" "$expected_dst"
    printf 'MOUNT\t202\t%s\t%s\n' "$source_path" "$expected_dst"
  )

  local output
  local status
  set +e
  output=$(PATH="$fake_bin:$PATH" bash "$VERIFY" "$manifest" 2>&1)
  status=$?
  set -e

  if [ "$status" -ne 0 ]; then
    printf '%s\n' "$output" >&2
    fail "verify rejected exact source/destination pairs"
  fi
  assert_contains "$output" "结果: PASS"
}

test_root_window_preflight_covers_stage5_inputs
test_root_window_fails_when_binance_destination_is_missing
test_verify_requires_patch_destination
test_verify_rejects_correct_source_at_wrong_destination
test_verify_accepts_exact_source_destination_pairs
echo "PASS: deployment fail-closed checks"
