#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
ROOT_WINDOW="$REPO_ROOT/scripts/hk-root-window-20260724.sh"
VERIFY="$REPO_ROOT/scripts/verify_hk_deployment.sh"
HARDENING_DEPLOY="$REPO_ROOT/scripts/hk-deploy-20260803.sh"
GEN_RECREATE="$REPO_ROOT/scripts/hk-gen-recreate-patched.py"
REVIEWED_ROLLOUT="$REPO_ROOT/scripts/reviewed_release_rollout.py"
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

assert_not_contains() {
  local output="$1"
  local unexpected="$2"
  if [[ "$output" == *"$unexpected"* ]]; then
    fail "expected output to exclude: $unexpected"
  fi
}

test_hardening_deploy_contract_is_fail_closed() {
  local text
  local generator
  local rollout
  text=$(cat "$HARDENING_DEPLOY")
  generator=$(cat "$GEN_RECREATE")
  rollout=$(cat "$REVIEWED_ROLLOUT")

  assert_contains "$text" 'DELIVERY_MODE="${DELIVERY_MODE:-immutable_image}"'
  assert_contains "$text" 'ROLLOUT_NODE="${ROLLOUT_NODE:-trader-v3-node-a}"'
  assert_contains "$text" 'SKIP_RESUME="${SKIP_RESUME:-1}"'
  assert_contains "$text" \
    'ACCOUNT_STALL_OPERATION_LOCK="${ACCOUNT_STALL_OPERATION_LOCK:-/var/lock/trader-v3-account-stall-operation.lock}"'
  assert_contains "$text" 'acquire_account_stall_operation_lock'
  assert_contains "$text" 'automatic RESUME is disabled; keep SKIP_RESUME=1'
  assert_contains "$text" 'return 1'
  assert_contains "$text" "trap 'on_err \$?' ERR"
  assert_contains "$text" 'BACKUP_CAPTURED=1'
  assert_contains "$text" 'FILES_INSTALLED=1'
  assert_contains "$text" 'ROLLOUT_RECREATE_STARTED=1'
  assert_contains "$text" 'recreate-$ROLLOUT_NODE.sh'
  assert_contains "$text" 'old image restore verification FAILED'
  assert_contains "$text" '"scope": {"account_id": rollout_account}'
  assert_contains "$text" 'accepted_ack_statuses = {"acked", "completed"}'
  assert_contains "$generator" 'NAUTILUS_INITIAL_TRADING_STATE=HALTED'
  assert_contains "$text" 'did not reach ready+HALTED'
  assert_contains "$text" \
    'NODE_STARTUP_MIN_AVAILABLE_BYTES=$((3 * 1024 * 1024 * 1024))'
  assert_contains "$text" 'verify_node_startup_memory_reserve'
  assert_contains "$text" 'recreate_release_node'
  assert_contains "$text" 'startup left MemAvailable below 3 GiB'
  assert_contains "$text" 'ACCOUNT_B_ROLLOUT_GATE'
  assert_contains "$text" 'ACCOUNT_B_RELEASE_MANIFEST'
  assert_contains "$text" 'ACCOUNT_B_EVIDENCE_FILE'
  assert_contains "$text" '"verify_live_passed": True'
  assert_contains "$text" '"canary_halted": True'
  assert_contains "$text" '"soak_seconds", 0)) < 1800'
  assert_contains "$text" 'ACCOUNT_B_EVIDENCE_SIGNATURE'
  assert_contains "$text" 'ACCOUNT_B_REVIEWER_PUBLIC_KEY_SHA256'
  assert_contains "$text" 'write_account_b_reviewer_public_key'
  assert_contains "$text" '"reviewer_public_key_sha256": reviewer_public_key_sha256'
  assert_not_contains "$text" 'ACCOUNT_B_EVIDENCE_PUBLIC_KEY'
  assert_contains "$text" \
    'db/migrations/0010_evidence_and_poll_indexes.up.sql'
  assert_contains "$text" \
    'db/migrations/0010_evidence_and_poll_indexes.down.sql'
  assert_contains "$text" 'db/migrations/0011_live_safety.up.sql'
  assert_contains "$text" 'db/migrations/0011_live_safety.down.sql'
  assert_contains "$text" \
    'db/migrations/0012_control_plane_maintenance_fence.up.sql'
  assert_contains "$text" \
    'db/migrations/0012_control_plane_maintenance_fence.down.sql'
  assert_contains "$text" \
    'db/migrations/0013_four_account_rollout.up.sql'
  assert_contains "$text" \
    'db/migrations/0013_four_account_rollout.down.sql'
  assert_contains "$text" \
    'db/migrations/0014_cancel_order_contract.up.sql'
  assert_contains "$text" \
    'db/migrations/0014_cancel_order_contract.down.sql'
  assert_contains "$text" \
    'db/migrations/0015_refresh_evidence_command.up.sql'
  assert_contains "$text" \
    'db/migrations/0015_refresh_evidence_command.down.sql'
  assert_contains "$text" 'db/migrations/0005_order_management.up.sql'
  assert_contains "$text" 'db/migrations/0005_order_management.down.sql'
  assert_contains "$text" '"0005", "order_management"'
  assert_contains "$text" '"evidence_and_poll_indexes",'
  assert_contains "$text" '"0011", "live_safety"'
  assert_contains "$text" '"0012",'
  assert_contains "$text" '"control_plane_maintenance_fence",'
  assert_contains "$text" '"0013",'
  assert_contains "$text" '"four_account_rollout",'
  assert_contains "$text" '"cancel_order_contract",'
  assert_contains "$text" '"0015",'
  assert_contains "$text" '"refresh_evidence_command",'
  assert_contains "$text" 'release migration metadata mismatch: steps'
  assert_contains "$text" \
    'release migration metadata lacks four-account files'
  assert_contains "$text" 'apply_and_verify_database_migration'
  assert_contains "$text" 'VALUES (%s, %s, %s)'
  assert_contains "$text" 'for version, name, sql, digest in migrations'
  assert_contains "$text" 'up_sha256'
  assert_contains "$text" 'migration was not durably committed'
  assert_contains "$text" \
    '0013 active rollout index lacks'
  assert_contains "$text" \
    'post-migration recovery lacks required migrations'
  assert_contains "$text" \
    '"0013": "four_account_rollout"'
  assert_contains "$text" \
    '"0014": "cancel_order_contract"'
  assert_contains "$text" \
    '"0015": "refresh_evidence_command"'
  assert_contains "$text" 'capture_pre_migration_database_backup'
  assert_contains "$text" 'pg_dump'
  assert_contains "$text" 'pg_restore'
  assert_contains "$text" 'PG_BACKUP_TIMEOUT_SECONDS'
  assert_contains "$text" 'postgres-pre-migration.dump.sha256'
  assert_contains "$text" 'capture_pre_migration_backup_expectation'
  assert_contains "$text" 'capture_post_migration_recovery_expectation'
  assert_contains "$text" 'prepare_post_migration_recovery_recreate'
  assert_contains "$text" 'prepare_legacy_rollback_recreate_fleet'
  assert_contains "$text" '--snapshot-runtime'
  assert_contains "$text" '--verify-snapshot-evidence'
  assert_contains "$text" 'legacy-recreate-bootstrap.json'
  assert_contains "$text" 'legacy-recreate-generated.tsv'
  assert_contains "$text" \
    'existing rollback recreate script changed during bootstrap'
  assert_contains "$text" \
    'legacy snapshot recreate verification failed'
  assert_contains "$text" \
    'POST_MIGRATION_RECOVERY_MANIFEST="$POST_MIGRATION_RECOVERY_PAYLOAD_ROOT/release-manifest.json"'
  assert_contains "$text" \
    'POST_MIGRATION_RECOVERY_ROOT="$BACKUP_ROOT/post-migration-recovery"'
  assert_contains "$text" \
    'recovery_dir="$POST_MIGRATION_RECOVERY_ROOT/$node"'
  assert_contains "$text" 'recovery_env="$recovery_dir/container.env"'
  assert_contains "$text" 'for node in "${RECREATE_NODES[@]}"; do'
  assert_contains "$text" 'POST_MIGRATION_RECOVERY_REQUIRED'
  assert_contains "$text" 'recover_post_migration_node'
  assert_contains "$text" 'post-migration recovery nodes remain HALTED'
  assert_not_contains "$text" 'capture_rollback_expectation'
  assert_contains "$text" '"runtime_generation"'
  assert_contains "$text" '"lease_fencing_token"'
  assert_contains "$text" '"heartbeat_sequence"'
  assert_contains "$text" 'database_schema_epoch'
  assert_contains "$text" 'manifest_schema_version'
  assert_contains "$text" 'redis_schema_epoch'
  assert_contains "$text" 'fenced-generation-namespace/v2'
  assert_not_contains "$text" 'stable-account-namespace/v1'
  assert_contains "$text" 'trader-v3-redis-cold-backup/v2'
  assert_contains "$text" 'trader-v3-redis-capacity-evidence/v3'
  assert_not_contains "$text" 'trader-v3-redis-cold-backup/v1'
  assert_not_contains "$text" 'trader-v3-redis-capacity-evidence/v1'
  assert_not_contains "$text" 'trader-v3-redis-capacity-evidence/v2'
  assert_contains "$text" 'redis-check-rdb'
  assert_contains "$text" 'redis-check-aof'
  assert_contains "$text" \
    'REDIS_AOF_VALIDATION_ROOT="$T/redis-aof-validation"'
  assert_contains "$text" \
    '-v "$validation_dir:/evidence:rw"'
  assert_contains "$text" \
    'Redis cold backup AOF checker changed canonical artifact'
  assert_not_contains "$text" \
    '-v "$artifact_path:/evidence/appendonly.aof:ro"'
  assert_contains "$text" 'validator_output_sha256'
  assert_contains "$text" 'source_container_preserved'
  assert_contains "$text" 'active_volume_source'
  assert_contains "$text" 'active_container_id'
  assert_contains "$text" 'source_container_id'
  assert_contains "$text" 'redis_cgroup_limit_bytes'
  assert_contains "$text" 'memory_limit_bytes'
  assert_contains "$text" 'memory_swap_limit_bytes'
  assert_contains "$text" \
    'SYSTEMD_RESOURCE_CONTRACT="$STAGING/systemd-resource-contract.json"'
  assert_contains "$text" \
    'require_checksum_artifact "build_immutable_node_image.py"'
  assert_contains "$text" \
    'Redis capacity cgroup differs from release resource contract'
  assert_contains "$text" \
    'Redis capacity memory limit differs from release resource contract'
  assert_contains "$text" \
    'Redis capacity memory swap differs from release resource contract'
  assert_contains "$text" 'legacy Redis container identity differs from backup evidence'
  assert_contains "$text" 'running Redis active volume source differs from evidence'
  assert_contains "$text" 'running Redis is missing the fencing epoch marker'
  assert_contains "$text" 'running Redis maxmemory policy must be noeviction'
  assert_contains "$text" 'running Redis MemorySwap differs from capacity evidence'
  assert_contains "$text" 'running Redis active volume differs from evidence'
  assert_contains "$text" 'live host budget cannot preserve system reserve'
  assert_contains "$text" 'account-b report hash mismatch'
  assert_contains "$text" 'trader-v3-live-trade-report/v1'
  assert_contains "$text" '"failure_reason": ""'
  assert_contains "$text" '"authorization_signatures_verified": True'
  assert_contains "$text" 'if max_loss >= Decimal("1.5"):'
  assert_contains "$text" 'testnet_emergency_close'
  assert_contains "$text" 'mainnet_round_trip'
  assert_contains "$text" 'mainnet close quantity differs from actual open fill'
  assert_contains "$text" 'mainnet cumulative net loss reached the stop limit'
  assert_contains "$text" 'account_a_safety_state_sha256'
  assert_contains "$text" '"target_symbol_flat": True'
  assert_contains "$text" '"target_symbol_regular_orders_zero": True'
  assert_contains "$text" '"target_symbol_algo_orders_zero": True'
  assert_contains "$text" 'non_target_portfolio_baseline_sha256'
  assert_not_contains "$text" '"account_a_flat": True'
  assert_contains "$text" 'live_node_config.py'
  assert_contains "$text" 'live-risk-policy.json'
  assert_contains "$text" \
    'IMMUTABLE_WATCHER_BUILDER="$STAGING/build_immutable_watcher_image.py"'
  assert_contains "$text" 'resolve-common-base'
  assert_contains "$text" 'common immutable base resolution failed'
  assert_contains "$text" \
    'WATCHER_RUNTIME_MANIFEST="$STAGING/watcher-runtime-manifest.json"'
  assert_contains "$text" \
    'WATCHER_ROOT="${WATCHER_ROOT:-/srv/trader}"'
  assert_contains "$text" \
    'WATCHER_CONTAINER="${WATCHER_CONTAINER:-trader-watcher-1}"'
  assert_contains "$text" \
    'WATCHER_TRADING_DB="${WATCHER_TRADING_DB:-/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db}"'
  assert_contains "$text" \
    'FOUR_CHANNEL_MAPPING_EVIDENCE="$BACKUP_ROOT/four-channel-account-mapping-pre-watcher.json"'
  assert_contains "$text" \
    'FOUR_CHANNEL_MAPPING_POST_WATCHER_EVIDENCE="$BACKUP_ROOT/four-channel-account-mapping-post-watcher.json"'
  assert_contains "$text" \
    'FOUR_CHANNEL_MAPPING_ROLLBACK_EVIDENCE="$BACKUP_ROOT/four-channel-account-mapping-rollback.json"'
  assert_contains "$text" 'verify_four_channel_account_mapping'
  assert_contains "$text" \
    '"schema_version": "trader-v3-four-channel-account-mapping/v2"'
  assert_contains "$text" \
    'verify_four_channel_account_mapping "" pre_restart'
  assert_contains "$text" \
    'dynamic-routing-columns-present/enabled-column-optional-v1'
  assert_contains "$text" \
    'detect_watcher_schema_restart_requirement'
  assert_contains "$text" \
    'if [ "$WATCHER_SCHEMA_RESTART_REQUIRED" = "1" ]; then'
  assert_contains "$text" 'connection.execute("PRAGMA query_only = ON")'
  assert_contains "$text" 'connection.execute("BEGIN")'
  assert_contains "$text" 'connection.execute("PRAGMA quick_check")'
  assert_contains "$text" 'os.fchmod(file_descriptor, 0o400)'
  assert_contains "$text" \
    '"$FOUR_CHANNEL_MAPPING_EVIDENCE" \'
  assert_contains "$text" \
    '"$FOUR_CHANNEL_MAPPING_POST_WATCHER_EVIDENCE" \'
  assert_contains "$text" \
    'verify_post_restart_four_channel_account_mapping'
  assert_contains "$text" \
    'restore_watcher_runtime_files'
  assert_contains "$text" \
    'watcher strict mapping gate failed; restoring prior watcher runtime'
  assert_contains "$text" \
    'write_watcher_mapping_rollback_evidence'
  assert_contains "$text" \
    'runtime_rebuild_health_hash_passed'
  assert_contains "$text" \
    'WATCHER_COMPOSE_SERVICE="${WATCHER_COMPOSE_SERVICE:-watcher}"'
  assert_contains "$text" \
    'EXCHANGE_STATE_RECORDER_UNIT="${EXCHANGE_STATE_RECORDER_UNIT:-trader-v3-exchange-state}"'
  assert_contains "$text" \
    'LIVE_TRADE_EXECUTOR="$STAGING/account_a_live_trade_executor.py"'
  assert_contains "$text" \
    'LIVE_TRADE_HTTP_ADAPTER="$STAGING/account_a_live_trade_http_adapter.py"'
  assert_contains "$text" \
    'require_staging_artifact \'
  assert_contains "$text" \
    '"$LIVE_TRADE_EXECUTOR" \'
  assert_contains "$text" \
    '"$LIVE_TRADE_HTTP_ADAPTER" \'
  assert_contains "$text" 'normalize_live_trade_release_metadata'
  assert_contains "$text" \
    '(Path(sys.argv[1]), 0o500, "live trade HTTP adapter")'
  assert_contains "$text" \
    '(Path(sys.argv[2]), 0o400, "release source manifest")'
  assert_contains "$text" \
    'flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW'
  assert_contains "$text" \
    'os.fchown(descriptor, 0, 0)'
  assert_contains "$text" \
    'os.fchmod(descriptor, mode)'
  assert_contains "$text" 'account_a_live_trade_executor.py \'
  assert_contains "$text" 'account_a_live_trade_http_adapter.py \'
  assert_contains "$text" 'build_immutable_watcher_image.py \'
  assert_contains "$text" 'watcher-runtime-manifest.json \'
  assert_contains "$text" 'host/exchange_state_recorder.py \'
  assert_contains "$text" 'require_checksum_artifact "$required"'
  assert_contains "$text" 'validate_watcher_runtime_payload'
  assert_contains "$text" '--validate-runtime-manifest "$WATCHER_RUNTIME_MANIFEST"'
  assert_contains "$text" 'SHA256SUMS lacks watcher runtime payload entries'
  assert_contains "$text" 'NODE_CONFIG_A="$T/node-a.hk.json"'
  assert_contains "$text" 'NODE_CONFIG_B="$T/node-b.hk.json"'
  assert_contains "$text" \
    'OPERATOR_ACCOUNT_REGISTRY_JSON='\''{"account-a":{},"account-b":{},"account-c":{},"account-d":{}}'\'''
  assert_contains "$text" \
    'V3_OPERATOR_ACCOUNTS="account-a,account-b,account-c,account-d"'
  assert_contains "$text" 'load_binance_proxy_settings'
  assert_contains "$text" 'verify_binance_proxy_egress'
  assert_contains "$text" 'BINANCE_EXPECTED_EGRESS_IP'
  assert_contains "$text" \
    'updated_binance["proxy_url"] = proxy_url'
  assert_contains "$text" \
    'BINANCE_PROXY_URL must not contain credentials'
  assert_contains "$text" 'install_operator_account_registry_environment'
  assert_contains "$text" 'operator account registry must not hard-code account equity'
  assert_not_contains "$text" '$T/config/node-a.hk.json'
  assert_not_contains "$text" '$T/config/node-b.hk.json'
  assert_contains "$text" '"$LIVE_NODE_CONFIG_TOOL" capture'
  assert_contains "$text" '--legacy-risk "$LEGACY_RISK_CAPTURE"'
  assert_contains "$text" '"$LIVE_NODE_CONFIG_TOOL" prepare-target'
  assert_contains "$text" '"$LIVE_NODE_CONFIG_TOOL" verify-target'
  assert_contains "$text" 'CONFIG_ARTIFACT_ROOT="$BACKUP_ROOT/node-config-artifacts"'
  assert_contains "$text" 'account-a=trader-v3-node-a=$CONFIG_ARTIFACT_A'
  assert_contains "$text" 'account-b=trader-v3-node-b=$CONFIG_ARTIFACT_B'
  assert_not_contains "$text" '"$LIVE_NODE_CONFIG_TOOL" apply'
  assert_not_contains "$text" 'CONFIG_UPDATE_APPLIED'
  assert_contains "$text" 'CONTROL_PLANE_ISOLATION_ACTIVATED=1'
  assert_contains "$text" '--rollback "$BACKUP_ROOT/control-plane-isolation"'
  assert_contains "$text" 'reviewed_release_rollout.py'
  assert_contains "$text" '--to-phase account_b_rollout'
  assert_contains "$text" '--to-phase account_c_rollout'
  assert_contains "$text" '--to-phase account_d_rollout'
  assert_not_contains "$text" '--to-phase fleet_complete'
  assert_contains "$rollout" 'PHASE_FLEET_COMPLETE'
  assert_contains "$rollout" 'def finalize_fleet_rollout('
  assert_contains "$rollout" '"finalize"'
  assert_contains "$text" '--closure-report "$PRIOR_CLOSURE_REPORT_COPY"'
  assert_contains "$text" \
    '--closure-signature "$PRIOR_CLOSURE_SIGNATURE_COPY"'
  assert_contains "$text" \
    '--closure-public-key "$PRIOR_CLOSURE_PUBLIC_KEY"'
  assert_contains "$text" '--to-phase aborted'
  assert_contains "$generator" 'NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON|'
  assert_contains "$generator" 'NAUTILUS_MAX_ORDER_SUBMIT_RATE|'
  assert_contains "$generator" 'NAUTILUS_MAX_ORDER_MODIFY_RATE|'
  assert_contains "$text" 'before-account-b-recreate'
  assert_contains "$text" 'after-account-b-recreate'
  assert_contains "$generator" '--database-schema-epoch'
  assert_contains "$generator" 'HostConfig.PortBindings'
  assert_contains "$generator" 'HostConfig.Ulimits'
  assert_contains "$generator" 'HostConfig.LogConfig'
  assert_contains "$generator" 'docker", "network", "connect'
  assert_contains "$text" 'verify_version_endpoint'
  assert_contains "$text" 'staging missing host/read_api.py'
  assert_contains "$text" 'staging missing host/snapshot.py'
  assert_contains "$text" 'staging missing host/decision_gateway/gateway.py'
  assert_contains "$text" 'staging missing host/exchange_state_recorder.py'
  assert_contains "$text" 'staging missing host/hermes_signal_feeder.py'
  assert_contains "$text" 'staging missing host/v3_trade.py'
  assert_contains "$text" 'staging missing host/v3-trader/SKILL.md'
  assert_contains "$text" 'staging missing host/system_snapshot.v1.json'
  assert_contains "$text" \
    'staging missing host/execution_domain/control_plane.py'
  assert_contains "$text" 'EXECUTION_DOMAIN_CONTROL_PLANE_TGT'
  assert_contains "$text" 'DECISION_GATEWAY_TGT'
  assert_contains "$text" 'EXCHANGE_STATE_RECORDER_TGT'
  assert_contains "$text" 'HERMES_FEEDER_TGT'
  assert_contains "$text" 'HERMES_V3_TRADE_TGT'
  assert_contains "$text" 'HERMES_V3_SKILL_TGT'
  assert_contains "$text" \
    '/srv/hermes/profiles/trader/skills/trading/v3-trader'
  assert_contains "$text" 'trader-v3-hermes-feeder'
  assert_contains "$text" 'hermes-gateway-trader'
  assert_contains "$text" 'host__decision_gateway.py'
  assert_contains "$text" 'host__exchange_state_recorder.py'
  assert_contains "$text" 'host__hermes_signal_feeder.py'
  assert_contains "$text" 'host__v3_trade.py'
  assert_contains "$text" 'host__v3_trader_SKILL.md'
  assert_contains "$text" 'cat host/decision_gateway/gateway.py'
  assert_contains "$text" 'install_payload_atomically'
  assert_contains "$text" 'install_watcher_runtime_atomically'
  assert_contains "$text" 'capture_watcher_runtime_backup'
  assert_contains "$text" 'WATCHER_RESTART_REQUIRED=1'
  assert_contains "$text" 'restart_watcher_runtime'
  assert_contains "$text" 'rollback_restart_watcher_runtime'
  assert_contains "$text" 'docker_compose_watcher build "$WATCHER_COMPOSE_SERVICE"'
  assert_contains "$text" '--force-recreate \'
  assert_contains "$text" 'verify_watcher_health'
  assert_contains "$text" 'verify_watcher_container_runtime'
  assert_contains "$text" 'verify_watcher_container_runtime_rollback'
  assert_contains "$text" 'watcher container hash mismatch'
  assert_contains "$text" 'watcher rollback container hash mismatch'
  assert_contains "$text" 'telegram-watcher health endpoint failed'
  assert_contains "$text" 'docker inspect "$WATCHER_CONTAINER"'
  assert_contains "$text" 'restart_exchange_state_recorder'
  assert_contains "$text" 'rollback_restart_exchange_state_recorder'
  assert_contains "$text" 'EXCHANGE_STATE_RESTART_REQUIRED=1'
  assert_contains "$text" 'post-install mismatch: host/exchange_state_recorder.py'
  assert_contains "$text" 'post-install watcher hash mismatch'
  assert_contains "$text" 'restore_installed_runtime_files'
  assert_contains "$text" 'live recreate promotion failed'
  assert_contains "$text" 'recovery release manifest promotion failed'
  assert_contains "$text" 'recovered commit metadata promotion failed'
  assert_contains "$text" \
    'post-migration forward recovery retained release runtime files'
  assert_contains "$text" 'restart_hermes_units'
  assert_not_contains "$text" '/Users/balen'
  assert_not_contains "$text" 'TELEGRAM_WATCHER_PM2'
  assert_not_contains "$text" 'pm2 '
  assert_not_contains "$text" 'host/order_lifecycle_monitor.py'
  assert_not_contains "$text" 'host/trader-v3-trade-outcomes.service'
  assert_contains "$text" 'require_release_matches_bundle'
  assert_contains "$text" 'sha256sum -c SHA256SUMS'
  assert_contains "$text" '--bundle-manifest "$STAGING/bundle-manifest.json"'
  assert_not_contains "$text" '"type": "RESUME"'

  local runtime_file
  for runtime_file in \
    run_node.py \
    reconciliation.py \
    redis_namespace_lease.py \
    redis_resp_client.py; do
    assert_contains "$text" "$runtime_file"
  done
  assert_contains "$text" 'redis_namespace_janitor.py'
  assert_contains "$text" 'redis_namespace_registry.py'
  assert_contains "$text" 'INSTANCE_SCOPE_COUNT=$('
  assert_contains "$text" 'grep -c '"'"'"use_instance_id": instance_id is not False'"'"''
  assert_contains "$text" '[ "$INSTANCE_SCOPE_COUNT" -eq 2 ]'
  assert_contains "$text" "grep -q 'UUID4.from_str' node.py"
  assert_contains "$text" "grep -q '_active_persistence_instance_id' node.py"
  assert_contains "$text" "grep -q 'persistence_instance_id' redis_namespace_lease.py"
  assert_contains "$text" "grep -q 'persistence_namespace' redis_namespace_lease.py"
  assert_not_contains "$text" 'grep -q '"'"'"use_instance_id": True'"'"''
}

extract_function() {
  local function_name="$1"
  python3 - "$HARDENING_DEPLOY" "$function_name" <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
name = sys.argv[2]
start = text.index(f"{name}() {{")
marker = "\n}\n"
end = text.index(marker, start) + len(marker)
print(text[start:end])
PY
}

extract_function_until() {
  local function_name="$1"
  local next_function_name="$2"
  python3 - "$HARDENING_DEPLOY" "$function_name" \
    "$next_function_name" <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
name = sys.argv[2]
next_name = sys.argv[3]
start = text.index(f"{name}() {{")
end = text.index(f"\n{next_name}() {{", start)
print(text[start:end])
PY
}

test_live_trade_release_artifacts_are_independently_required() {
  local staging_definition
  local checksum_definition
  staging_definition=$(extract_function require_staging_artifact)
  checksum_definition=$(extract_function require_checksum_artifact)
  local artifact
  for artifact in \
    account_a_live_trade_executor.py \
    account_a_live_trade_http_adapter.py; do
    local case_dir="$TMP_DIR/live-trade-required/$artifact"
    local staging="$case_dir/staging"
    mkdir -p "$staging"

    local output
    local status
    set +e
    output=$(
      REQUIRE_STAGING_DEFINITION="$staging_definition" \
        bash -c '
          die() {
            printf "FATAL: %s\n" "$*" >&2
            exit 23
          }
          eval "$REQUIRE_STAGING_DEFINITION"
          require_staging_artifact "$1" "$2"
        ' _ "$staging/$artifact" "$artifact" 2>&1
    )
    status=$?
    set -e
    if [ "$status" -ne 23 ]; then
      fail "staging preflight accepted missing artifact: $artifact"
    fi
    assert_contains "$output" "$artifact missing in staging"
    : >"$staging/$artifact"
    REQUIRE_STAGING_DEFINITION="$staging_definition" \
      bash -c '
        die() {
          printf "FATAL: %s\n" "$*" >&2
          exit 23
        }
        eval "$REQUIRE_STAGING_DEFINITION"
        require_staging_artifact "$1" "$2"
      ' _ "$staging/$artifact" "$artifact"

    printf '%064d  unrelated-file\n' 0 >"$staging/SHA256SUMS"
    set +e
    output=$(
      REQUIRE_CHECKSUM_DEFINITION="$checksum_definition" \
      STAGING="$staging" \
        bash -c '
          die() {
            printf "FATAL: %s\n" "$*" >&2
            exit 23
          }
          eval "$REQUIRE_CHECKSUM_DEFINITION"
          require_checksum_artifact "$1"
        ' _ "$artifact" 2>&1
    )
    status=$?
    set -e
    if [ "$status" -ne 23 ]; then
      fail "checksum preflight accepted missing artifact: $artifact"
    fi
    assert_contains "$output" \
      "SHA256SUMS does not cover required artifact: $artifact"
    printf '%064d  %s\n' 1 "$artifact" >>"$staging/SHA256SUMS"
    REQUIRE_CHECKSUM_DEFINITION="$checksum_definition" \
    STAGING="$staging" \
      bash -c '
        die() {
          printf "FATAL: %s\n" "$*" >&2
          exit 23
        }
        eval "$REQUIRE_CHECKSUM_DEFINITION"
        require_checksum_artifact "$1"
      ' _ "$artifact"
  done
}

test_account_stall_operation_lock_is_exclusive_and_configurable() {
  local case_dir="$TMP_DIR/account-stall-lock"
  local fake_bin="$case_dir/bin"
  local lock_path="$case_dir/custom-operation.lock"
  local ready_path="$case_dir/ready"
  local release_path="$case_dir/release"
  local definition
  local holder_pid
  local output
  local status
  mkdir -p "$fake_bin"
  cat >"$fake_bin/flock" <<'EOF'
#!/usr/bin/env python3
import fcntl
import os
import sys

arguments = list(sys.argv[1:])
non_blocking = False
if arguments and arguments[0] == "-n":
    non_blocking = True
    arguments.pop(0)
if len(arguments) != 1:
    raise SystemExit(2)
fd = int(arguments[0])
operation = fcntl.LOCK_EX
if non_blocking:
    operation |= fcntl.LOCK_NB
try:
    fcntl.flock(fd, operation)
except BlockingIOError:
    raise SystemExit(1)
os.fstat(fd)
EOF
  chmod +x "$fake_bin/flock"
  definition=$(extract_function acquire_account_stall_operation_lock)

  PATH="$fake_bin:$PATH" \
  ACCOUNT_STALL_OPERATION_LOCK="$lock_path" \
  ACCOUNT_STALL_OPERATION_LOCK_UID="$(id -u)" \
  ACCOUNT_STALL_OPERATION_LOCK_GID="$(id -g)" \
  LOCK_READY_PATH="$ready_path" \
  LOCK_RELEASE_PATH="$release_path" \
  LOCK_DEFINITION="$definition" \
    bash -c '
      set -euo pipefail
      verify_account_stall_operation_lock() { return 0; }
      eval "$LOCK_DEFINITION"
      acquire_account_stall_operation_lock
      touch "$LOCK_READY_PATH"
      while [ ! -e "$LOCK_RELEASE_PATH" ]; do
        sleep 0.05
      done
    ' &
  holder_pid=$!
  for _ in $(seq 1 100); do
    if [ -e "$ready_path" ]; then
      break
    fi
    sleep 0.05
  done
  if [ ! -e "$ready_path" ]; then
    kill "$holder_pid" >/dev/null 2>&1 || true
    wait "$holder_pid" >/dev/null 2>&1 || true
    fail "operation lock holder did not become ready"
  fi

  set +e
  output=$(
    PATH="$fake_bin:$PATH" \
    ACCOUNT_STALL_OPERATION_LOCK="$lock_path" \
    ACCOUNT_STALL_OPERATION_LOCK_UID="$(id -u)" \
    ACCOUNT_STALL_OPERATION_LOCK_GID="$(id -g)" \
    LOCK_DEFINITION="$definition" \
      bash -c '
        set -euo pipefail
        verify_account_stall_operation_lock() { return 0; }
        eval "$LOCK_DEFINITION"
        acquire_account_stall_operation_lock
      ' 2>&1
  )
  status=$?
  set -e
  touch "$release_path"
  wait "$holder_pid"

  if [ "$status" -eq 0 ]; then
    fail "operation lock allowed overlapping deployment"
  fi
  assert_contains "$output" \
    "another account-stall operation holds $lock_path"
}

test_pre_migration_database_backup_is_bounded_and_validated() {
  local case_dir="$TMP_DIR/postgres-backup"
  local fake_bin="$case_dir/bin"
  local dump_path="$case_dir/postgres-pre-migration.dump"
  local list_path="$case_dir/postgres-pre-migration.dump.list"
  local hash_path="$case_dir/postgres-pre-migration.dump.sha256"
  local log="$case_dir/actions.log"
  local die_definition
  local backup_definition
  mkdir -p "$fake_bin"
  cat >"$fake_bin/timeout" <<'EOF'
#!/usr/bin/env bash
printf 'timeout %s\n' "$*" >>"$BACKUP_TEST_LOG"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --signal=*|--kill-after=*|*[0-9]s)
      shift
      ;;
    *)
      break
      ;;
  esac
done
exec "$@"
EOF
  cat >"$fake_bin/pg_dump" <<'EOF'
#!/usr/bin/env bash
printf 'pg_dump %s\n' "$*" >>"$BACKUP_TEST_LOG"
for argument in "$@"; do
  case "$argument" in
    --file=*)
      output="${argument#--file=}"
      ;;
  esac
done
printf 'fixture-custom-dump\n' >"$output"
EOF
  cat >"$fake_bin/pg_restore" <<'EOF'
#!/usr/bin/env bash
printf 'pg_restore %s\n' "$*" >>"$BACKUP_TEST_LOG"
printf '123; 1259 456 TABLE public node_heartbeats trader\n'
EOF
  chmod +x "$fake_bin/timeout" "$fake_bin/pg_dump" "$fake_bin/pg_restore"
  die_definition=$(extract_function die)
  backup_definition=$(extract_function capture_pre_migration_database_backup)

  PATH="$fake_bin:$PATH" \
  BACKUP_TEST_LOG="$log" \
  DIE_DEFINITION="$die_definition" \
  BACKUP_DEFINITION="$backup_definition" \
  PG_BACKUP_TIMEOUT_SECONDS=17 \
  PG_DUMP_BIN=pg_dump \
  PG_RESTORE_BIN=pg_restore \
    bash -c '
      set -Eeuo pipefail
      eval "$DIE_DEFINITION"
      eval "$BACKUP_DEFINITION"
      capture_pre_migration_database_backup \
        "postgresql://fixture.invalid/trader" \
        "'"$dump_path"'" \
        "'"$list_path"'" \
        "'"$hash_path"'"
    '

  [ -s "$dump_path" ] || fail "database dump was not created"
  [ -s "$list_path" ] || fail "database restore listing was not created"
  (
    cd "$case_dir"
    sha256sum -c "$(basename "$hash_path")" >/dev/null
  ) || fail "database dump sha256 record is invalid"
  assert_contains "$(cat "$log")" \
    "timeout --signal=TERM --kill-after=10s 17s pg_dump"
  assert_contains "$(cat "$log")" \
    "timeout --signal=TERM --kill-after=10s 17s pg_restore --list"
}

test_pre_migration_database_backup_rejects_invalid_restore_listing() {
  local case_dir="$TMP_DIR/postgres-backup-invalid"
  local fake_bin="$case_dir/bin"
  local die_definition
  local backup_definition
  local status
  mkdir -p "$fake_bin"
  cat >"$fake_bin/timeout" <<'EOF'
#!/usr/bin/env bash
while [ "$#" -gt 0 ]; do
  case "$1" in
    --signal=*|--kill-after=*|*[0-9]s)
      shift
      ;;
    *)
      break
      ;;
  esac
done
exec "$@"
EOF
  cat >"$fake_bin/pg_dump" <<'EOF'
#!/usr/bin/env bash
for argument in "$@"; do
  case "$argument" in
    --file=*)
      output="${argument#--file=}"
      ;;
  esac
done
printf 'fixture-custom-dump\n' >"$output"
EOF
  cat >"$fake_bin/pg_restore" <<'EOF'
#!/usr/bin/env bash
printf '123; 1259 456 TABLE public unrelated trader\n'
EOF
  chmod +x "$fake_bin/timeout" "$fake_bin/pg_dump" "$fake_bin/pg_restore"
  die_definition=$(extract_function die)
  backup_definition=$(extract_function capture_pre_migration_database_backup)

  set +e
  PATH="$fake_bin:$PATH" \
  DIE_DEFINITION="$die_definition" \
  BACKUP_DEFINITION="$backup_definition" \
  PG_BACKUP_TIMEOUT_SECONDS=17 \
  PG_DUMP_BIN=pg_dump \
  PG_RESTORE_BIN=pg_restore \
    bash -c '
      set -Eeuo pipefail
      eval "$DIE_DEFINITION"
      eval "$BACKUP_DEFINITION"
      capture_pre_migration_database_backup \
        "postgresql://fixture.invalid/trader" \
        "'"$case_dir"'/postgres.dump" \
        "'"$case_dir"'/postgres.dump.list" \
        "'"$case_dir"'/postgres.dump.sha256"
    ' >/dev/null 2>&1
  status=$?
  set -e

  if [ "$status" -eq 0 ]; then
    fail "database backup accepted an unusable restore listing"
  fi
}

test_die_routes_failure_through_err_trap() {
  local definition
  local output
  local status
  definition=$(extract_function die)

  set +e
  output=$(
    DIE_DEFINITION="$definition" bash -c '
      set -Eeuo pipefail
      eval "$DIE_DEFINITION"
      trap "echo ERR_TRAP_FIRED" ERR
      die injected-failure
    ' 2>&1
  )
  status=$?
  set -e

  if [ "$status" -ne 1 ]; then
    fail "die returned unexpected status: $status"
  fi
  assert_contains "$output" "FATAL: injected-failure"
  assert_contains "$output" "ERR_TRAP_FIRED"
}

test_node_recreate_is_sequential_and_memory_gated() {
  local case_dir="$TMP_DIR/node-recreate-memory-gate"
  local trader_root="$case_dir/trader-v3"
  local meminfo="$case_dir/meminfo"
  local log="$case_dir/actions.log"
  local recreate_definition
  local memory_definition
  local output
  local status
  mkdir -p "$trader_root"
  cat >"$trader_root/recreate-trader-v3-node-a.sh" <<'EOF'
#!/usr/bin/env bash
printf 'recreate\n' >>"$NODE_RECREATE_TEST_LOG"
EOF
  chmod +x "$trader_root/recreate-trader-v3-node-a.sh"
  printf 'MemAvailable:    4194304 kB\n' >"$meminfo"
  recreate_definition=$(extract_function recreate_release_node)
  memory_definition=$(extract_function verify_node_startup_memory_reserve)

  T="$trader_root" \
  NODE_STARTUP_MEMINFO_PATH="$meminfo" \
  NODE_STARTUP_MIN_AVAILABLE_BYTES=$((3 * 1024 * 1024 * 1024)) \
  NODE_RELEASE_MEMORY_LIMIT_BYTES=671088640 \
  NODE_RECREATE_TEST_LOG="$log" \
  RECREATE_DEFINITION="$recreate_definition" \
  MEMORY_DEFINITION="$memory_definition" \
    bash -c '
      set -Eeuo pipefail
      die() {
        printf "FATAL: %s\n" "$*" >&2
        return 1
      }
      verify_maintenance_fence() {
        printf "fence:%s\n" "$1" >>"$NODE_RECREATE_TEST_LOG"
      }
      verify_node_ready_halted() {
        printf "ready:%s\n" "$1" >>"$NODE_RECREATE_TEST_LOG"
      }
      node_ready_port() {
        printf "8081\n"
      }
      verify_version_endpoint() {
        printf "version:%s\n" "$1" >>"$NODE_RECREATE_TEST_LOG"
      }
        write_node_startup_resource_evidence() {
          printf "evidence:%s\n" "$1" >>"$NODE_RECREATE_TEST_LOG"
        }
      docker() {
        if [ "$1" = "inspect" ]; then
          printf "false 0 671088640\n"
          return 0
        fi
        if [ "$1" = "exec" ]; then
          case "$*" in
            *memory.current*)
              printf "196083712\n"
              ;;
            *memory.peak*)
              printf "268435456\n"
              ;;
          esac
          return 0
        fi
        return 1
      }
      eval "$MEMORY_DEFINITION"
      eval "$RECREATE_DEFINITION"
      recreate_release_node trader-v3-node-a
    '
  assert_contains "$(cat "$log")" \
    $'fence:node-recreate-trader-v3-node-a\nrecreate\nready:trader-v3-node-a\nversion:8081\nevidence:trader-v3-node-a'

  printf 'MemAvailable:    3145727 kB\n' >"$meminfo"
  set +e
  output=$(
    T="$trader_root" \
    NODE_STARTUP_MEMINFO_PATH="$meminfo" \
    NODE_STARTUP_MIN_AVAILABLE_BYTES=$((3 * 1024 * 1024 * 1024)) \
    NODE_RELEASE_MEMORY_LIMIT_BYTES=671088640 \
    NODE_RECREATE_TEST_LOG="$log" \
    RECREATE_DEFINITION="$recreate_definition" \
    MEMORY_DEFINITION="$memory_definition" \
      bash -c '
        set -Eeuo pipefail
        die() {
          printf "FATAL: %s\n" "$*" >&2
          return 1
        }
        verify_maintenance_fence() {
          return 0
        }
        verify_node_ready_halted() {
          return 0
        }
        node_ready_port() {
          printf "8081\n"
        }
        verify_version_endpoint() {
          return 0
        }
      write_node_startup_resource_evidence() {
        printf "evidence:%s\n" "$1" >>"$NODE_RECREATE_TEST_LOG"
      }
        docker() {
          if [ "$1" = "inspect" ]; then
            printf "false 0 671088640\n"
            return 0
          fi
          if [ "$1" = "exec" ]; then
            case "$*" in
              *memory.current*)
                printf "196083712\n"
                ;;
              *memory.peak*)
                printf "268435456\n"
                ;;
            esac
            return 0
          fi
          return 1
        }
        trap "printf rollback-path-entered >&2" ERR
        eval "$MEMORY_DEFINITION"
        eval "$RECREATE_DEFINITION"
        recreate_release_node trader-v3-node-a
      ' 2>&1
  )
  status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    fail "node recreate accepted startup memory below 3 GiB"
  fi
  assert_contains "$output" \
    "trader-v3-node-a startup left MemAvailable below 3 GiB"
  assert_contains "$output" "rollback-path-entered"
  if [ "$(grep -c '^evidence:' "$log")" -ne 1 ]; then
    fail "failed startup wrote final resource evidence"
  fi
}

test_bootstrap_generated_recreate_cleanup_preserves_existing_scripts() {
  local case_dir="$TMP_DIR/bootstrap-recreate-cleanup"
  local trader_root="$case_dir/trader-v3"
  local backup_root="$case_dir/backup"
  local generated_list="$backup_root/legacy-recreate-generated.tsv"
  local existing_a="$trader_root/recreate-trader-v3-node-a.sh"
  local generated_c="$trader_root/recreate-trader-v3-node-c.sh"
  local generated_d="$trader_root/recreate-trader-v3-node-d.sh"
  local backup_c="$backup_root/recreate-trader-v3-node-c.sh"
  local backup_d="$backup_root/recreate-trader-v3-node-d.sh"
  local cleanup_definition
  local remove_definition
  local existing_hash
  mkdir -p "$trader_root" "$backup_root"
  printf '#!/bin/bash\nprintf existing-a\\n\n' >"$existing_a"
  printf '#!/bin/bash\nprintf generated-c\\n\n' >"$generated_c"
  printf '#!/bin/bash\nprintf generated-d\\n\n' >"$generated_d"
  cp "$generated_c" "$backup_c"
  cp "$generated_d" "$backup_d"
  chmod 0700 \
    "$existing_a" \
    "$generated_c" \
    "$generated_d" \
    "$backup_c" \
    "$backup_d"
  printf '%s\t%s\t%s\t%s\n' \
    trader-v3-node-c \
    "$backup_c" \
    "$backup_root/c-env.json" \
    "$backup_root/c-evidence.json" \
    >"$generated_list"
  printf '%s\t%s\t%s\t%s\n' \
    trader-v3-node-d \
    "$backup_d" \
    "$backup_root/d-env.json" \
    "$backup_root/d-evidence.json" \
    >>"$generated_list"
  existing_hash="$(sha256sum "$existing_a" | awk '{print $1}')"
  remove_definition=$(
    extract_function remove_generated_legacy_recreate_if_unchanged
  )
  cleanup_definition=$(extract_function remove_bootstrap_generated_live_recreate)

  T="$trader_root" \
  BACKUP_CAPTURED=0 \
  LEGACY_RECREATE_BOOTSTRAP_PREPARED=1 \
  LEGACY_RECREATE_GENERATED_LIST="$generated_list" \
  REMOVE_DEFINITION="$remove_definition" \
  CLEANUP_DEFINITION="$cleanup_definition" \
    bash -c '
      set -Eeuo pipefail
      eval "$REMOVE_DEFINITION"
      eval "$CLEANUP_DEFINITION"
      remove_bootstrap_generated_live_recreate
    '

  [ -f "$existing_a" ] \
    || fail "bootstrap recreate cleanup removed an existing script"
  [ "$(sha256sum "$existing_a" | awk '{print $1}')" = "$existing_hash" ] \
    || fail "bootstrap recreate cleanup changed an existing script"
  [ ! -e "$generated_c" ] \
    || fail "bootstrap recreate cleanup retained generated node-c script"
  [ ! -e "$generated_d" ] \
    || fail "bootstrap recreate cleanup retained generated node-d script"
}

test_existing_recreate_mode_remains_compatible() {
  local case_dir="$TMP_DIR/existing-recreate-mode"
  local existing="$case_dir/recreate-trader-v3-node-a.sh"
  local verify_definition
  mkdir -p "$case_dir"
  printf '#!/bin/bash\nexit 0\n' >"$existing"
  chmod 0755 "$existing"
  verify_definition=$(extract_function verify_legacy_recreate_artifact)

  VERIFY_DEFINITION="$verify_definition" \
  EXISTING_RECREATE="$existing" \
    bash -c '
      set -Eeuo pipefail
      eval "$VERIFY_DEFINITION"
      verify_legacy_recreate_artifact \
        "$EXISTING_RECREATE" \
        "existing rollback recreate" \
        existing \
        >/dev/null
    '
}

test_generated_recreate_promotion_rejects_link_attacks() {
  local case_dir="$TMP_DIR/generated-recreate-promotion-links"
  local source="$case_dir/generated.sh"
  local linked_source="$case_dir/generated-linked.sh"
  local destination="$case_dir/live.sh"
  local attack_target="$case_dir/attack-target.sh"
  local promote_definition
  local output
  local status
  mkdir -p "$case_dir"
  printf '#!/bin/bash\nexit 0\n' >"$source"
  chmod 0700 "$source"
  promote_definition=$(extract_function promote_generated_legacy_recreate)

  ln "$source" "$linked_source"
  set +e
  output=$(
    PROMOTE_DEFINITION="$promote_definition" \
    SOURCE="$source" \
    DESTINATION="$destination" \
      bash -c '
        set -Eeuo pipefail
        eval "$PROMOTE_DEFINITION"
        promote_generated_legacy_recreate "$SOURCE" "$DESTINATION"
      ' 2>&1
  )
  status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    fail "generated recreate promotion accepted a hard-linked source"
  fi
  assert_contains "$output" "generated recreate source is invalid"
  [ ! -e "$destination" ] \
    || fail "hard-linked source attack created the live destination"
  rm "$linked_source"

  printf '#!/bin/bash\nprintf protected\\n\n' >"$attack_target"
  ln -s "$attack_target" "$destination"
  set +e
  output=$(
    PROMOTE_DEFINITION="$promote_definition" \
    SOURCE="$source" \
    DESTINATION="$destination" \
      bash -c '
        set -Eeuo pipefail
        eval "$PROMOTE_DEFINITION"
        promote_generated_legacy_recreate "$SOURCE" "$DESTINATION"
      ' 2>&1
  )
  status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    fail "generated recreate promotion accepted a symlink destination"
  fi
  assert_contains "$output" "generated recreate destination exists"
  [ "$(cat "$attack_target")" = $'#!/bin/bash\nprintf protected\\n' ] \
    || fail "symlink destination attack changed its target"
}

test_bootstrap_recreate_fleet_preserves_a_b_and_materializes_c_d() {
  local case_dir="$TMP_DIR/bootstrap-recreate-fleet"
  local fake_bin="$case_dir/bin"
  local inspect_dir="$case_dir/inspect"
  local trader_root="$case_dir/trader-v3"
  local backup_root="$case_dir/backup"
  local docker_log="$case_dir/docker.log"
  local definitions
  local node
  local existing_a="$trader_root/recreate-trader-v3-node-a.sh"
  local existing_b="$trader_root/recreate-trader-v3-node-b.sh"
  local existing_a_hash
  local existing_b_hash
  mkdir -p "$fake_bin" "$inspect_dir" "$trader_root" "$backup_root"
  printf '#!/bin/bash\nexit 0\n' >"$existing_a"
  printf '#!/bin/bash\nexit 0\n' >"$existing_b"
  chmod 0755 "$existing_a" "$existing_b"
  existing_a_hash="$(sha256sum "$existing_a" | awk '{print $1}')"
  existing_b_hash="$(sha256sum "$existing_b" | awk '{print $1}')"

  python3 - "$inspect_dir" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
binance = "/app/nautilus/binance_execution.py"
futures = "/app/nautilus/binance_futures_execution.py"
for suffix in ("a", "b", "c", "d"):
    name = f"trader-v3-node-{suffix}"
    payload = [
        {
            "Id": suffix * 64,
            "Image": "sha256:" + suffix * 64,
            "State": {
                "Running": False,
            },
            "Config": {
                "Image": f"legacy:{suffix}",
                "Hostname": name,
                "User": "",
                "WorkingDir": "/app",
                "Labels": {},
                "Env": [
                    f"ACCOUNT_ID=account-{suffix}",
                    "NAUTILUS_INITIAL_TRADING_STATE=HALTED",
                ],
                "Cmd": [],
                "Entrypoint": None,
            },
            "NetworkSettings": {
                "Networks": {
                    "trader-v3": {
                        "Aliases": [name],
                    },
                },
            },
            "HostConfig": {
                "RestartPolicy": {
                    "Name": "no",
                    "MaximumRetryCount": 0,
                },
                "NetworkMode": "trader-v3",
                "PortBindings": {},
                "Ulimits": [],
                "SecurityOpt": [],
                "CapAdd": [],
                "CapDrop": [],
                "Devices": [],
                "ReadonlyRootfs": False,
                "PidMode": "",
                "IpcMode": "",
                "CgroupnsMode": "",
                "Runtime": "",
                "ShmSize": 67108864,
                "Memory": 0,
                "MemorySwap": 0,
                "NanoCpus": 0,
                "CpuShares": 0,
                "PidsLimit": 0,
                "LogConfig": {},
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": f"/legacy/{suffix}/binance_execution.py",
                    "Destination": binance,
                    "Mode": "ro",
                    "RW": False,
                    "Propagation": "rprivate",
                },
                {
                    "Type": "bind",
                    "Source": (
                        f"/legacy/{suffix}/"
                        "binance_futures_execution.py"
                    ),
                    "Destination": futures,
                    "Mode": "ro",
                    "RW": False,
                    "Propagation": "rprivate",
                },
            ],
        },
    ]
    (root / f"{name}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
PY

  cat >"$fake_bin/docker" <<'EOF'
#!/bin/bash
set -euo pipefail
command_name="$1"
shift
case "$command_name" in
  inspect)
    if [ "${1:-}" = "--format" ]; then
      format="$2"
      name="$3"
      case "$format" in
        '{{.State.Pid}}')
          printf '0\n'
          ;;
        '{{.Image}}')
          python3 - "$FAKE_INSPECT_DIR/$name.json" <<'PY'
import json
import sys

print(json.load(open(sys.argv[1], encoding="utf-8"))[0]["Image"])
PY
          ;;
        *)
          exit 1
          ;;
      esac
      exit 0
    fi
    cat "$FAKE_INSPECT_DIR/$1.json"
    ;;
  rm)
    exit 0
    ;;
  run)
    printf '%q ' run "$@" >>"$FAKE_DOCKER_LOG"
    printf '\n' >>"$FAKE_DOCKER_LOG"
    env_file=""
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--env-file" ]; then
        env_file="$2"
        shift 2
        continue
      fi
      shift
    done
    [ -r "$env_file" ]
    grep -qx 'NAUTILUS_INITIAL_TRADING_STATE=HALTED' "$env_file"
    printf 'snapshot-container-id\n'
    ;;
  network)
    [ "$1" = "connect" ]
    ;;
  *)
    exit 1
    ;;
esac
EOF
  chmod 0755 "$fake_bin/docker"

  definitions="$(
    extract_function die
    extract_function_until \
      discover_legacy_binance_mount_targets \
      verify_legacy_recreate_artifact
    extract_function_until \
      verify_legacy_recreate_artifact \
      capture_existing_legacy_recreate
    extract_function_until \
      capture_existing_legacy_recreate \
      promote_generated_legacy_recreate
    extract_function_until \
      promote_generated_legacy_recreate \
      remove_generated_legacy_recreate_if_unchanged
    extract_function_until \
      remove_generated_legacy_recreate_if_unchanged \
      write_legacy_recreate_bootstrap_evidence
    extract_function_until \
      write_legacy_recreate_bootstrap_evidence \
      verify_legacy_rollback_recreate_fleet
    extract_function_until \
      verify_legacy_rollback_recreate_fleet \
      prepare_legacy_rollback_recreate_fleet
    extract_function_until \
      prepare_legacy_rollback_recreate_fleet \
      remove_bootstrap_generated_live_recreate
  )"

  PATH="$fake_bin:$PATH" \
  FAKE_INSPECT_DIR="$inspect_dir" \
  FAKE_DOCKER_LOG="$docker_log" \
  T="$trader_root" \
  BACKUP_ROOT="$backup_root" \
  GEN_RECREATE="$GEN_RECREATE" \
  DEPLOY_GATE_MODE=bootstrap_stopped \
  LEGACY_RECREATE_BOOTSTRAP_EVIDENCE="$backup_root/legacy-recreate-bootstrap.json" \
  LEGACY_RECREATE_GENERATED_LIST="$backup_root/legacy-recreate-generated.tsv" \
  LEGACY_RECREATE_RECORDS="$backup_root/legacy-recreate-records.tsv" \
  LEGACY_RECREATE_SNAPSHOT_ROOT="$backup_root/legacy-recreate-snapshots" \
  DEFINITIONS="$definitions" \
    bash -c '
      set -Eeuo pipefail
      eval "$DEFINITIONS"
      RECREATE_NODES=(
        trader-v3-node-a
        trader-v3-node-b
        trader-v3-node-c
        trader-v3-node-d
      )
      prepare_legacy_rollback_recreate_fleet
      verify_legacy_rollback_recreate_fleet
    '

  [ "$(sha256sum "$existing_a" | awk '{print $1}')" = "$existing_a_hash" ] \
    || fail "bootstrap changed account-a recreate"
  [ "$(sha256sum "$existing_b" | awk '{print $1}')" = "$existing_b_hash" ] \
    || fail "bootstrap changed account-b recreate"
  [ "$(stat -f '%Lp' "$backup_root/recreate-trader-v3-node-a.sh")" = "755" ] \
    || fail "bootstrap changed account-a recreate mode"
  [ "$(stat -f '%Lp' "$backup_root/recreate-trader-v3-node-b.sh")" = "755" ] \
    || fail "bootstrap changed account-b recreate mode"
  for node in trader-v3-node-c trader-v3-node-d; do
    [ -x "$trader_root/recreate-$node.sh" ] \
      || fail "bootstrap did not promote $node recreate"
    [ -x "$backup_root/recreate-$node.sh" ] \
      || fail "bootstrap did not back up $node recreate"
    [ -r "$backup_root/legacy-recreate-snapshots/$node/container-env.json" ] \
      || fail "bootstrap did not preserve $node environment"
    [ -r "$backup_root/legacy-recreate-snapshots/$node/snapshot-evidence.json" ] \
      || fail "bootstrap did not preserve $node evidence"
  done
  [ "$(wc -l <"$backup_root/legacy-recreate-records.tsv")" -eq 4 ] \
    || fail "bootstrap recreate record set is incomplete"
  [ "$(wc -l <"$backup_root/legacy-recreate-generated.tsv")" -eq 2 ] \
    || fail "bootstrap generated recreate set is incomplete"

  (
    cd "$backup_root"
    find . -type f ! -name SHA256SUMS -print0 \
      | sort -z \
      | xargs -0 sha256sum
  ) >"$backup_root/SHA256SUMS"
  (
    cd "$backup_root"
    sha256sum -c SHA256SUMS >/dev/null
  )
  for relative in \
    legacy-recreate-bootstrap.json \
    legacy-recreate-generated.tsv \
    legacy-recreate-records.tsv \
    recreate-trader-v3-node-c.sh \
    recreate-trader-v3-node-d.sh \
    legacy-recreate-snapshots/trader-v3-node-c/container-env.json \
    legacy-recreate-snapshots/trader-v3-node-d/container-env.json; do
    grep -Fq "  ./$relative" "$backup_root/SHA256SUMS" \
      || fail "backup checksum omitted $relative"
  done

  : >"$docker_log"
  PATH="$fake_bin:$PATH" \
  FAKE_INSPECT_DIR="$inspect_dir" \
  FAKE_DOCKER_LOG="$docker_log" \
    bash "$backup_root/recreate-trader-v3-node-c.sh"
  PATH="$fake_bin:$PATH" \
  FAKE_INSPECT_DIR="$inspect_dir" \
  FAKE_DOCKER_LOG="$docker_log" \
    bash "$backup_root/recreate-trader-v3-node-d.sh"
  grep -Fq -- \
    "--env-file $backup_root/legacy-recreate-snapshots/trader-v3-node-c/container-env.json" \
    "$docker_log" \
    || fail "account-c rollback did not use its backup environment"
  grep -Fq -- \
    "--env-file $backup_root/legacy-recreate-snapshots/trader-v3-node-d/container-env.json" \
    "$docker_log" \
    || fail "account-d rollback did not use its backup environment"
}

test_bootstrap_recreate_fleet_rejects_missing_a_b() {
  local case_dir="$TMP_DIR/bootstrap-recreate-missing-a"
  local trader_root="$case_dir/trader-v3"
  local backup_root="$case_dir/backup"
  local definitions
  local output
  local status
  mkdir -p "$trader_root" "$backup_root"
  definitions="$(
    extract_function die
    extract_function_until \
      prepare_legacy_rollback_recreate_fleet \
      remove_bootstrap_generated_live_recreate
  )"

  set +e
  output=$(
    T="$trader_root" \
    BACKUP_ROOT="$backup_root" \
    DEPLOY_GATE_MODE=bootstrap_stopped \
    LEGACY_RECREATE_BOOTSTRAP_EVIDENCE="$backup_root/legacy-recreate-bootstrap.json" \
    LEGACY_RECREATE_GENERATED_LIST="$backup_root/legacy-recreate-generated.tsv" \
    LEGACY_RECREATE_RECORDS="$backup_root/legacy-recreate-records.tsv" \
    LEGACY_RECREATE_SNAPSHOT_ROOT="$backup_root/legacy-recreate-snapshots" \
    DEFINITIONS="$definitions" \
      bash -c '
        set -Eeuo pipefail
        eval "$DEFINITIONS"
        RECREATE_NODES=(trader-v3-node-a)
        prepare_legacy_rollback_recreate_fleet
      ' 2>&1
  )
  status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    fail "bootstrap generated a missing account-a recreate"
  fi
  assert_contains "$output" \
    "existing rollback recreate script missing: trader-v3-node-a"
}

test_on_err_restores_files_topology_and_old_image() {
  local case_dir="$TMP_DIR/on-err"
  local fake_bin="$case_dir/bin"
  local backup_root="$case_dir/backup"
  local trader_root="$case_dir/trader-v3"
  local destination="$case_dir/live.py"
  local new_file="$case_dir/new.py"
  local log="$case_dir/actions.log"
  local definition
  local restore_definition
  local restore_files_definition
  local rollback_runtime_definition
  local status
  mkdir -p "$fake_bin" "$backup_root/files" "$trader_root"
  printf 'old\n' >"$backup_root/files/live.py"
  printf 'live.py\t%s\n' "$destination" >"$backup_root/index.tsv"
  printf '%s\n' "$new_file" >"$backup_root/new-files.txt"
  printf 'trader-v3-node-a\tsha256:old-image\n' \
    >"$backup_root/old-images.tsv"
  printf 'new\n' >"$destination"
  printf 'new\n' >"$new_file"
  cat >"$backup_root/recreate-trader-v3-node-a.sh" <<'EOF'
#!/usr/bin/env bash
printf 'recreate-old-image\n' >>"$ROLLBACK_TEST_LOG"
EOF
  chmod +x "$backup_root/recreate-trader-v3-node-a.sh"
  cp "$backup_root/recreate-trader-v3-node-a.sh" \
    "$trader_root/recreate-trader-v3-node-a.sh"
  cat >"$fake_bin/docker" <<'EOF'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >>"$ROLLBACK_TEST_LOG"
if [ "$1" = "inspect" ]; then
  printf 'sha256:old-image\n'
fi
EOF
  cat >"$fake_bin/python3" <<'EOF'
#!/usr/bin/env bash
printf 'python3 %s\n' "$*" >>"$ROLLBACK_TEST_LOG"
EOF
  cat >"$fake_bin/systemctl" <<'EOF'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >>"$ROLLBACK_TEST_LOG"
EOF
  cat >"$case_dir/control-plane-isolation.sh" <<'EOF'
#!/usr/bin/env bash
printf 'isolation %s\n' "$*" >>"$ROLLBACK_TEST_LOG"
EOF
  chmod +x \
    "$fake_bin/docker" \
    "$fake_bin/python3" \
    "$fake_bin/systemctl" \
    "$case_dir/control-plane-isolation.sh"
  definition=$(extract_function on_err)
  restore_definition=$(extract_function restore_pre_migration_state)
  restore_files_definition=$(extract_function restore_installed_runtime_files)
  rollback_runtime_definition=$(extract_function rollback_restart_changed_runtimes)

  set +e
  PATH="$fake_bin:$PATH" \
  ROLLBACK_TEST_LOG="$log" \
  ON_ERR_DEFINITION="$definition" \
  RESTORE_DEFINITION="$restore_definition" \
  RESTORE_FILES_DEFINITION="$restore_files_definition" \
  ROLLBACK_RUNTIME_DEFINITION="$rollback_runtime_definition" \
  T="$trader_root" \
  BACKUP_ROOT="$backup_root" \
  ROLLOUT_NODE="trader-v3-node-a" \
  CONTROL_PLANE_ISOLATION_SCRIPT="$case_dir/control-plane-isolation.sh" \
  CONTROL_PLANE_ISOLATION_ACTIVATED=1 \
  CONTROL_PLANE_RESTARTED=1 \
  WATCHER_RESTARTED=0 \
  EXCHANGE_STATE_RESTARTED=0 \
  HERMES_RESTARTED=0 \
  BACKUP_CAPTURED=1 \
  FILES_INSTALLED=1 \
  ROLLOUT_RECREATE_STARTED=1 \
  ROLLOUT_TRACKED=0 \
  ROLLOUT_FINALIZED=0 \
  ROLLBACK_IN_PROGRESS=0 \
  bash -c '
    RECREATE_NODES=(trader-v3-node-a)
    CONTROL_PLANE_UNITS=(
      trader-v3-controlplane-node-control.service
      trader-v3-controlplane-event-ingest.service
      trader-v3-controlplane-operator-query.service
    )
    verify_maintenance_fence() { return 0; }
    eval "$RESTORE_FILES_DEFINITION"
    eval "$ROLLBACK_RUNTIME_DEFINITION"
    rollback_restart_watcher_runtime() { return 0; }
    rollback_restart_exchange_state_recorder() { return 0; }
    rollback_restart_hermes_units() { return 0; }
    eval "$RESTORE_DEFINITION"
    eval "$ON_ERR_DEFINITION"
    on_err 37
  ' >/dev/null 2>&1
  status=$?
  set -e

  if [ "$status" -ne 37 ]; then
    fail "on_err returned unexpected status: $status"
  fi
  if [ "$(cat "$destination")" != "old" ]; then
    fail "on_err did not restore installed file"
  fi
  if [ -e "$new_file" ]; then
    fail "on_err did not remove newly installed file"
  fi
  assert_contains "$(cat "$log")" \
    "isolation --rollback $backup_root/control-plane-isolation"
  assert_not_contains "$(cat "$log")" "systemctl restart"
  assert_contains "$(cat "$log")" "recreate-old-image"
  assert_contains "$(cat "$log")" "docker stop --time 30 trader-v3-node-a"
}

test_immutable_config_and_rollout_ordering() {
  python3 - "$HARDENING_DEPLOY" <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
execution_start = text.index("# ---------- HALT ----------")
prepare_a = text.index(
    '"$LIVE_NODE_CONFIG_TOOL" prepare-target \\\n'
    "    --account-id account-a",
    execution_start,
)
prepare_d = text.index(
    '"$LIVE_NODE_CONFIG_TOOL" prepare-target \\\n'
    "    --account-id account-d",
    prepare_a,
)
capture_release = text.index(
    'python3 "$RELEASE_TOOL" capture',
    prepare_d,
)
phase_only = text.index(
    'if [ "$PHASE_ONLY_ROLLOUT" = "1" ]; then',
    capture_release,
)
database_backup = text.index(
    "capture_pre_migration_database_backup",
    phase_only,
)
legacy_recreate_bootstrap = text.index(
    "prepare_legacy_rollback_recreate_fleet",
    phase_only,
)
pre_migration_expectation = text.index(
    "capture_pre_migration_backup_expectation",
    database_backup,
)
recovery_prepare = text.index(
    "prepare_post_migration_recovery_recreate",
    pre_migration_expectation,
)
post_migration_expectation = text.index(
    "capture_post_migration_recovery_expectation",
    recovery_prepare,
)
recovery_armed = text.index(
    "\nPOST_MIGRATION_RECOVERY_REQUIRED=1\n",
    post_migration_expectation,
)
migrate = text.index(
    "\napply_and_verify_database_migration\n",
    recovery_armed,
)
bootstrap_register = text.index(
    "\n    ensure_bootstrap_rollout_registration\n",
    migrate,
)
install = text.index("# ---------- install ----------", bootstrap_register)
recreate_section = text.index(
    "# ---------- recreate & verify ----------",
    install,
)
recreate_loop = text.index(
    'for node in "${RECREATE_NODES[@]}"; do\n'
    '  recreate_release_node "$node"',
    recreate_section,
)
verify_nodes = text.index(
    'verify_release_nodes "${RECREATE_NODES[@]}"',
    recreate_loop,
)
heartbeat_fence = text.index(
    "\n  acquire_maintenance_fence_after_bootstrap\n",
    verify_nodes,
)
account_d_pending = text.index(
    "signed account-d closure is required for fleet_complete",
    heartbeat_fence,
)

if not (
    prepare_a
    < prepare_d
    < capture_release
    < phase_only
    < legacy_recreate_bootstrap
    < database_backup
    < pre_migration_expectation
    < recovery_prepare
    < post_migration_expectation
    < recovery_armed
    < migrate
    < bootstrap_register
    < install
    < recreate_section
    < recreate_loop
    < verify_nodes
    < heartbeat_fence
    < account_d_pending
):
    raise SystemExit("immutable config or rollout phase ordering changed")

phase_block = text[phase_only:database_backup]
required_phase_only_steps = (
    'verify_release_nodes "${ALL_NODES[@]}"',
    "acquire_maintenance_fence_after_bootstrap",
    "advance_reviewed_rollout_for_node",
    "exit 0",
)
for step in required_phase_only_steps:
    if step not in phase_block:
        raise SystemExit(f"phase-only rollout lacks: {step}")
for forbidden in (
    "capture_pre_migration_database_backup",
    "apply_and_verify_database_migration",
    "install_operator_account_registry_environment",
    'bash "$T/recreate-$node.sh"',
):
    if forbidden in phase_block:
        raise SystemExit(f"phase-only rollout mutates release state: {forbidden}")
PY
}

test_migration_expectations_have_separate_schema_contracts() {
  local pre_definition
  local post_definition
  pre_definition=$(
    extract_function_until \
      capture_pre_migration_backup_expectation \
      capture_post_migration_recovery_expectation
  )
  post_definition=$(
    extract_function_until \
      capture_post_migration_recovery_expectation \
      verify_post_migration_recovery
  )

  assert_contains "$pre_definition" \
    "trader-v3-pre-migration-backup-expectation/v2"
  assert_contains "$pre_definition" "postgres_dump_sha256"
  assert_contains "$pre_definition" "previous_image_digest"
  assert_contains "$pre_definition" "previous_nodes"
  assert_contains "$pre_definition" "old_images_sha256"
  assert_contains "$pre_definition" \
    "old image record differs from recreate node exact-set"
  assert_not_contains "$pre_definition" "/ready"
  assert_not_contains "$pre_definition" "FROM node_heartbeats"
  assert_not_contains "$pre_definition" "redis_fencing_epoch"
  assert_not_contains "$pre_definition" "runtime_generation"
  assert_not_contains "$pre_definition" "lease_fencing_token"

  assert_contains "$post_definition" \
    "trader-v3-post-migration-recovery-expectation/v1"
  assert_contains "$post_definition" "database_schema_epoch"
  assert_contains "$post_definition" "redis_fencing_epoch"
  assert_contains "$post_definition" "recovery_manifest_sha256"
  assert_contains "$post_definition" "recovery_image_digest"
  assert_contains "$post_definition" "recovery_script_sha256"
  assert_contains "$post_definition" "recovery_env_sha256"
  assert_contains "$post_definition" "recovery_docker_sha256"
  assert_contains "$post_definition" '"expected_trading_state": "HALTED"'
}

test_on_err_uses_compatible_recovery_after_migration() {
  local definition
  local recovery_definition
  definition=$(extract_function on_err)
  recovery_definition=$(extract_function recover_post_migration_node)

  assert_contains "$definition" 'POST_MIGRATION_RECOVERY_REQUIRED'
  assert_contains "$definition" 'recover_post_migration_node'
  assert_contains "$definition" 'POST_MIGRATION_RECOVERY_VERIFIED=1'
  assert_contains "$recovery_definition" \
    'post-migration recovery nodes remain HALTED'
  assert_contains "$recovery_definition" \
    'PATH="$POST_MIGRATION_RECOVERY_BIN:$PATH"'
  assert_contains "$recovery_definition" \
    'verify_node_ready_halted "$node"'
  assert_contains "$recovery_definition" \
    'verify_version_endpoint "$recovery_port"'
  assert_contains "$recovery_definition" \
    'verify_node_startup_memory_reserve "$node"'
  assert_not_contains "$recovery_definition" \
    'generate_release_recreate'
  assert_not_contains "$definition" \
    'bash "$BACKUP_ROOT/recreate-$ROLLOUT_NODE.sh"'
  assert_not_contains "$recovery_definition" \
    'docker stop --time 30 "$ROLLOUT_NODE" >/dev/null 2>&1 || true'
}

test_post_migration_recovery_resource_failure_blocks_promotion() {
  local case_dir="$TMP_DIR/post-migration-resource-gate"
  local recovery_root="$case_dir/recovery"
  local recovery_bin="$case_dir/bin"
  local trader_root="$case_dir/trader-v3"
  local node="trader-v3-node-a"
  local log="$case_dir/actions.log"
  local recovery_definition
  local status
  mkdir -p "$recovery_root/$node" "$recovery_bin" "$trader_root"
  : >"$recovery_root/$node/expectation.json"
  : >"$recovery_root/$node/container.env"
  cat >"$recovery_root/$node/recreate.sh" <<'EOF'
#!/usr/bin/env bash
printf 'recreate\n' >>"$RECOVERY_RESOURCE_TEST_LOG"
EOF
  chmod 0700 "$recovery_root/$node/recreate.sh"
  : >"$case_dir/recovery-manifest.json"
  cat >"$recovery_bin/docker" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod 0700 "$recovery_bin/docker"
  recovery_definition=$(extract_function recover_post_migration_node)

  set +e
  RECOVERY_RESOURCE_TEST_LOG="$log" \
  RECOVERY_DEFINITION="$recovery_definition" \
  POST_MIGRATION_RECOVERY_ROOT="$recovery_root" \
  POST_MIGRATION_RECOVERY_BIN="$recovery_bin" \
  POST_MIGRATION_RECOVERY_DOCKER="$recovery_bin/docker" \
  POST_MIGRATION_RECOVERY_MANIFEST="$case_dir/recovery-manifest.json" \
  T="$trader_root" \
  RECOVERY_NODE="$node" \
    bash -c '
      set -Eeuo pipefail
      RECREATE_NODES=("$RECOVERY_NODE")
      verify_maintenance_fence() {
        return 0
      }
      python3() {
        return 0
      }
      docker() {
        return 0
      }
      verify_node_ready_halted() {
        printf "ready:%s\n" "$1" >>"$RECOVERY_RESOURCE_TEST_LOG"
      }
      node_ready_port() {
        printf "8081\n"
      }
      verify_version_endpoint() {
        printf "version:%s\n" "$1" >>"$RECOVERY_RESOURCE_TEST_LOG"
      }
      verify_node_startup_memory_reserve() {
        printf "resource:%s\n" "$1" >>"$RECOVERY_RESOURCE_TEST_LOG"
        return 1
      }
      verify_post_migration_recovery() {
        printf "post:%s\n" "$1" >>"$RECOVERY_RESOURCE_TEST_LOG"
      }
      eval "$RECOVERY_DEFINITION"
      recover_post_migration_node
    ' >/dev/null 2>&1
  status=$?
  set -e

  if [ "$status" -eq 0 ]; then
    fail "post-migration recovery accepted failed resource gate"
  fi
  assert_contains "$(cat "$log")" \
    $'recreate\nready:trader-v3-node-a\nversion:8081\nresource:trader-v3-node-a'
  assert_not_contains "$(cat "$log")" "post:"
  if [ -e "$trader_root/recreate-$node.sh" ]; then
    fail "failed post-migration recovery promoted recreate script"
  fi
}

test_on_err_selects_post_migration_recovery_branch() {
  local case_dir="$TMP_DIR/on-err-post-migration"
  local fake_bin="$case_dir/bin"
  local log="$case_dir/actions.log"
  local definition
  local status
  mkdir -p "$fake_bin"
  cat >"$fake_bin/docker" <<'EOF'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >>"$ROLLBACK_TEST_LOG"
EOF
  chmod +x "$fake_bin/docker"
  definition=$(extract_function on_err)

  set +e
  PATH="$fake_bin:$PATH" \
  ROLLBACK_TEST_LOG="$log" \
  ON_ERR_DEFINITION="$definition" \
  ROLLOUT_NODE="trader-v3-node-a" \
  BACKUP_ROOT="$case_dir/backup" \
  POST_MIGRATION_RECOVERY_REQUIRED=1 \
  POST_MIGRATION_RECOVERY_VERIFIED=0 \
  MIGRATION_COMMIT_MARKER="$case_dir/migration-marker.json" \
  ROLLBACK_IN_PROGRESS=0 \
  ROLLOUT_TRACKED=0 \
  ROLLOUT_FINALIZED=0 \
    bash -c '
      recover_post_migration_node() {
        printf "recover-post-migration\n" >>"$ROLLBACK_TEST_LOG"
      }
      restore_pre_migration_state() {
        printf "restore-pre-migration\n" >>"$ROLLBACK_TEST_LOG"
      }
      eval "$ON_ERR_DEFINITION"
      on_err 43
    ' >/dev/null 2>&1
  status=$?
  set -e

  if [ "$status" -ne 43 ]; then
    fail "post-migration on_err returned unexpected status: $status"
  fi
  assert_contains "$(cat "$log")" "recover-post-migration"
  assert_not_contains "$(cat "$log")" "restore-pre-migration"
  if [ "$(grep -c '^docker stop ' "$log")" -ne 1 ]; then
    fail "verified post-migration recovery was stopped again"
  fi
}

test_watcher_restart_and_rollback_use_compose_health_and_hash_gate() {
  local case_dir="$TMP_DIR/watcher-compose"
  local fake_bin="$case_dir/bin"
  local watcher_root="$case_dir/srv-trader"
  local watcher_source_root="$watcher_root/services/telegram-watcher"
  local manifest="$case_dir/watcher-runtime-manifest.json"
  local log="$case_dir/actions.log"
  local digest
  local die_definition
  local compose_definition
  local health_definition
  local container_definition
  local rollback_container_definition
  local restart_definition
  local rollback_definition
  local changed_list="$case_dir/watcher-runtime-changed.tsv"
  mkdir -p "$fake_bin" "$watcher_source_root"
  printf 'fixture-server\n' >"$case_dir/server.js"
  printf 'old-server\n' >"$watcher_source_root/server.js"
  printf 'watcher/server.js\t%s/server.js\n' "$watcher_source_root" \
    >"$changed_list"
  digest=$(sha256sum "$case_dir/server.js" | awk '{print $1}')
  python3 - "$manifest" "$digest" <<'PY'
import json
import sys
from pathlib import Path

manifest = {
    "schema_version": "trader-v3-watcher-runtime-manifest/v1",
    "files": [
        {
            "release_path": "watcher/server.js",
            "target_path": "/app/server.js",
            "sha256": sys.argv[2],
            "size": 15,
        }
    ],
}
Path(sys.argv[1]).write_text(json.dumps(manifest), encoding="utf-8")
PY
  : >"$watcher_root/docker-compose.yml"
  cat >"$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
printf 'curl %s\n' "$*" >>"$WATCHER_TEST_LOG"
printf '{"configured":true,"connected":false,"loggedIn":true,"watchGroups":[]}\n'
EOF
  cat >"$fake_bin/docker" <<'EOF'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >>"$WATCHER_TEST_LOG"
if [ "$1" = "inspect" ]; then
  printf 'true\n'
  exit 0
fi
  if [ "$1" = "exec" ]; then
    payload="$(cat)"
    case "$payload" in
    *'/app/server.js'*'watcher rollback container hash mismatch'*)
      exit 0
      ;;
    *'/app/server.js'*'watcher container hash mismatch'*)
      exit 0
      ;;
    *)
      printf 'unexpected docker exec payload\n' >&2
      exit 19
      ;;
  esac
fi
if [ "$1" = "compose" ]; then
  exit 0
fi
exit 17
EOF
  chmod +x "$fake_bin/curl" "$fake_bin/docker"
  die_definition=$(extract_function die)
  compose_definition=$(extract_function docker_compose_watcher)
  health_definition=$(extract_function verify_watcher_health)
  container_definition=$(extract_function verify_watcher_container_runtime)
  rollback_container_definition=$(
    extract_function verify_watcher_container_runtime_rollback
  )
  restart_definition=$(extract_function restart_watcher_runtime)
  rollback_definition=$(extract_function rollback_restart_watcher_runtime)

  PATH="$fake_bin:$PATH" \
  WATCHER_TEST_LOG="$log" \
  DIE_DEFINITION="$die_definition" \
  COMPOSE_DEFINITION="$compose_definition" \
  HEALTH_DEFINITION="$health_definition" \
  CONTAINER_DEFINITION="$container_definition" \
  ROLLBACK_CONTAINER_DEFINITION="$rollback_container_definition" \
  RESTART_DEFINITION="$restart_definition" \
  ROLLBACK_DEFINITION="$rollback_definition" \
  WATCHER_ROOT="$watcher_root" \
  WATCHER_SOURCE_ROOT="$watcher_source_root" \
  WATCHER_COMPOSE_FILE="$watcher_root/docker-compose.yml" \
  WATCHER_COMPOSE_PROJECT="trader" \
  WATCHER_COMPOSE_SERVICE="watcher" \
  WATCHER_CONTAINER="trader-watcher-1" \
  WATCHER_HEALTH_URL="http://127.0.0.1:9090/api/status" \
  WATCHER_RUNTIME_MANIFEST="$manifest" \
  WATCHER_RUNTIME_CHANGED_LIST="$changed_list" \
  WATCHER_RESTART_REQUIRED=1 \
  WATCHER_RESTARTED=1 \
    bash -c '
      set -Eeuo pipefail
      verify_maintenance_fence() { return 0; }
      eval "$DIE_DEFINITION"
      eval "$COMPOSE_DEFINITION"
      eval "$HEALTH_DEFINITION"
      eval "$CONTAINER_DEFINITION"
      eval "$ROLLBACK_CONTAINER_DEFINITION"
      eval "$RESTART_DEFINITION"
      eval "$ROLLBACK_DEFINITION"
      restart_watcher_runtime
      rollback_restart_watcher_runtime
    '

  assert_contains "$(cat "$log")" \
    "docker compose --project-name trader --file $watcher_root/docker-compose.yml build watcher"
  assert_contains "$(cat "$log")" \
    "docker compose --project-name trader --file $watcher_root/docker-compose.yml up -d --no-deps --force-recreate watcher"
  assert_contains "$(cat "$log")" \
    "curl -sf http://127.0.0.1:9090/api/status"
  assert_contains "$(cat "$log")" \
    "docker inspect --format {{.State.Running}} trader-watcher-1"
  assert_contains "$(cat "$log")" \
    "docker exec -i trader-watcher-1 python3 -"
}

test_strict_watcher_mapping_failure_restores_prior_runtime() {
  local case_dir="$TMP_DIR/watcher-strict-mapping-rollback"
  local fake_bin="$case_dir/bin"
  local backup_root="$case_dir/backup"
  local files_root="$backup_root/files"
  local watcher_project_root="$case_dir/srv-trader"
  local watcher_root="$watcher_project_root/services/telegram-watcher"
  local destination="$watcher_root/server.js"
  local changed_list="$backup_root/watcher-runtime-changed.tsv"
  local manifest="$case_dir/watcher-runtime-manifest.json"
  local log="$case_dir/actions.log"
  local compose_definition
  local health_definition
  local rollback_container_definition
  local rollback_definition
  local restore_definition
  local evidence_definition
  local checksum_definition
  local verify_definition
  mkdir -p "$fake_bin" "$files_root" "$watcher_root"
  printf 'old-watcher-runtime\n' >"$files_root/watcher__fixture__server.js"
  printf 'new-watcher-runtime\n' >"$destination"
  printf 'watcher__fixture__server.js\t%s\n' "$destination" \
    >"$backup_root/index.tsv"
  : >"$backup_root/new-files.txt"
  printf 'watcher/server.js\t%s\n' "$destination" >"$changed_list"
  printf '{"schema_version":"pre-restart"}\n' \
    >"$backup_root/four-channel-account-mapping-pre-watcher.json"
  : >"$watcher_project_root/docker-compose.yml"
  python3 - "$manifest" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

runtime = b"old-watcher-runtime\n"
payload = {
    "schema_version": "trader-v3-watcher-runtime-manifest/v1",
    "files": [
        {
            "release_path": "watcher/server.js",
            "target_path": "/app/server.js",
            "sha256": hashlib.sha256(runtime).hexdigest(),
            "size": len(runtime),
        }
    ],
}
Path(sys.argv[1]).write_text(json.dumps(payload), encoding="utf-8")
PY
  cat >"$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
printf 'curl %s\n' "$*" >>"$WATCHER_MAPPING_TEST_LOG"
printf '{"configured":true,"connected":true,"loggedIn":true,"watchGroups":[]}\n'
EOF
  cat >"$fake_bin/docker" <<'EOF'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >>"$WATCHER_MAPPING_TEST_LOG"
if [ "$1" = "inspect" ]; then
  printf 'true\n'
  exit 0
fi
if [ "$1" = "exec" ]; then
  payload="$(cat)"
  case "$payload" in
    *'/app/server.js'*'watcher rollback container hash mismatch'*)
      exit 0
      ;;
    *)
      printf 'unexpected docker exec payload\n' >&2
      exit 19
      ;;
  esac
fi
if [ "$1" = "compose" ]; then
  exit 0
fi
exit 17
EOF
  chmod +x "$fake_bin/curl" "$fake_bin/docker"
  compose_definition=$(extract_function docker_compose_watcher)
  health_definition=$(extract_function verify_watcher_health)
  rollback_container_definition=$(
    extract_function verify_watcher_container_runtime_rollback
  )
  rollback_definition=$(extract_function rollback_restart_watcher_runtime)
  restore_definition=$(extract_function restore_watcher_runtime_files)
  evidence_definition=$(
    extract_function_until \
      write_watcher_mapping_rollback_evidence \
      restore_watcher_runtime_files
  )
  checksum_definition=$(extract_function refresh_backup_checksums)
  verify_definition=$(
    extract_function verify_post_restart_four_channel_account_mapping
  )

  PATH="$fake_bin:$PATH" \
  BACKUP_ROOT="$backup_root" \
  WATCHER_RUNTIME_CHANGED_LIST="$changed_list" \
  WATCHER_RUNTIME_MANIFEST="$manifest" \
  WATCHER_ROOT="$watcher_project_root" \
  WATCHER_SOURCE_ROOT="$watcher_root" \
  WATCHER_COMPOSE_FILE="$watcher_project_root/docker-compose.yml" \
  WATCHER_COMPOSE_PROJECT="trader" \
  WATCHER_COMPOSE_SERVICE="watcher" \
  WATCHER_CONTAINER="trader-watcher-1" \
  WATCHER_HEALTH_URL="http://127.0.0.1:9090/api/status" \
  FOUR_CHANNEL_MAPPING_EVIDENCE="$backup_root/four-channel-account-mapping-pre-watcher.json" \
  FOUR_CHANNEL_MAPPING_POST_WATCHER_EVIDENCE="$case_dir/evidence.json" \
  FOUR_CHANNEL_MAPPING_ROLLBACK_EVIDENCE="$backup_root/four-channel-account-mapping-rollback.json" \
  DESTINATION="$destination" \
  WATCHER_MAPPING_TEST_LOG="$log" \
  COMPOSE_DEFINITION="$compose_definition" \
  HEALTH_DEFINITION="$health_definition" \
  ROLLBACK_CONTAINER_DEFINITION="$rollback_container_definition" \
  ROLLBACK_DEFINITION="$rollback_definition" \
  RESTORE_DEFINITION="$restore_definition" \
  EVIDENCE_DEFINITION="$evidence_definition" \
  CHECKSUM_DEFINITION="$checksum_definition" \
  VERIFY_DEFINITION="$verify_definition" \
  BACKUP_CAPTURED=1 \
  FILES_INSTALLED=1 \
  WATCHER_SCHEMA_RESTART_REQUIRED=1 \
  WATCHER_RESTARTED=1 \
    bash -c '
      set -Eeuo pipefail
      die() {
        printf "die %s\n" "$*" >>"$WATCHER_MAPPING_TEST_LOG"
        return 1
      }
      verify_four_channel_account_mapping() {
        printf "strict-gate %s %s\n" "$1" "$2" \
          >>"$WATCHER_MAPPING_TEST_LOG"
        return 23
      }
      eval "$COMPOSE_DEFINITION"
      eval "$HEALTH_DEFINITION"
      eval "$ROLLBACK_CONTAINER_DEFINITION"
      eval "$ROLLBACK_DEFINITION"
      eval "$RESTORE_DEFINITION"
      eval "$EVIDENCE_DEFINITION"
      eval "$CHECKSUM_DEFINITION"
      eval "$VERIFY_DEFINITION"
      set +e
      verify_post_restart_four_channel_account_mapping
      status=$?
      set -e
      printf "status=%s restarted=%s\n" \
        "$status" "$WATCHER_RESTARTED" >>"$WATCHER_MAPPING_TEST_LOG"
      [ "$status" -eq 23 ]
      [ "$WATCHER_RESTARTED" -eq 0 ]
    '

  if [ "$(cat "$destination")" != "old-watcher-runtime" ]; then
    fail "strict watcher mapping failure did not restore prior runtime"
  fi
  assert_contains "$(cat "$log")" \
    "strict-gate $case_dir/evidence.json strict"
  assert_contains "$(cat "$log")" \
    "docker compose --project-name trader --file $watcher_project_root/docker-compose.yml build watcher"
  assert_contains "$(cat "$log")" \
    "docker compose --project-name trader --file $watcher_project_root/docker-compose.yml up -d --no-deps --force-recreate watcher"
  assert_contains "$(cat "$log")" \
    "curl -sf http://127.0.0.1:9090/api/status"
  assert_contains "$(cat "$log")" \
    "docker exec -i trader-watcher-1 python3 -"
  assert_contains "$(cat "$log")" "status=23 restarted=0"
  python3 - "$backup_root/four-channel-account-mapping-rollback.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload["schema_version"] == (
    "trader-v3-four-channel-mapping-rollback/v2"
)
assert payload["strict_gate_status"] == 23
assert payload["file_restore_passed"] is True
assert payload["runtime_rollback_required"] is True
assert payload["runtime_rollback_attempted"] is True
assert payload["runtime_rollback_not_required"] is False
assert payload["runtime_rebuild_health_hash_passed"] is True
assert payload["watcher_schema_restart_required"] is True
PY
  (
    cd "$backup_root"
    sha256sum -c SHA256SUMS >/dev/null
  )

  : >"$log"
  PATH="$fake_bin:$PATH" \
  BACKUP_ROOT="$backup_root" \
  WATCHER_RUNTIME_CHANGED_LIST="$changed_list" \
  WATCHER_RUNTIME_MANIFEST="$manifest" \
  WATCHER_ROOT="$watcher_project_root" \
  WATCHER_SOURCE_ROOT="$watcher_root" \
  WATCHER_COMPOSE_FILE="$watcher_project_root/docker-compose.yml" \
  WATCHER_COMPOSE_PROJECT="trader" \
  WATCHER_COMPOSE_SERVICE="watcher" \
  WATCHER_CONTAINER="trader-watcher-1" \
  WATCHER_HEALTH_URL="http://127.0.0.1:9090/api/status" \
  FOUR_CHANNEL_MAPPING_EVIDENCE="$backup_root/four-channel-account-mapping-pre-watcher.json" \
  FOUR_CHANNEL_MAPPING_POST_WATCHER_EVIDENCE="$case_dir/evidence.json" \
  FOUR_CHANNEL_MAPPING_ROLLBACK_EVIDENCE="$backup_root/four-channel-account-mapping-rollback.json" \
  WATCHER_MAPPING_TEST_LOG="$log" \
  COMPOSE_DEFINITION="$compose_definition" \
  HEALTH_DEFINITION="$health_definition" \
  ROLLBACK_CONTAINER_DEFINITION="$rollback_container_definition" \
  ROLLBACK_DEFINITION="$rollback_definition" \
  RESTORE_DEFINITION="$restore_definition" \
  EVIDENCE_DEFINITION="$evidence_definition" \
  CHECKSUM_DEFINITION="$checksum_definition" \
  VERIFY_DEFINITION="$verify_definition" \
  BACKUP_CAPTURED=1 \
  FILES_INSTALLED=0 \
  WATCHER_SCHEMA_RESTART_REQUIRED=0 \
  WATCHER_RESTARTED=0 \
    bash -c '
      set -Eeuo pipefail
      die() {
        printf "die %s\n" "$*" >>"$WATCHER_MAPPING_TEST_LOG"
        return 1
      }
      verify_four_channel_account_mapping() {
        printf "strict-gate %s %s\n" "$1" "$2" \
          >>"$WATCHER_MAPPING_TEST_LOG"
        return 23
      }
      eval "$COMPOSE_DEFINITION"
      eval "$HEALTH_DEFINITION"
      eval "$ROLLBACK_CONTAINER_DEFINITION"
      eval "$ROLLBACK_DEFINITION"
      eval "$RESTORE_DEFINITION"
      eval "$EVIDENCE_DEFINITION"
      eval "$CHECKSUM_DEFINITION"
      eval "$VERIFY_DEFINITION"
      set +e
      verify_post_restart_four_channel_account_mapping
      status=$?
      set -e
      printf "status=%s restarted=%s\n" \
        "$status" "$WATCHER_RESTARTED" >>"$WATCHER_MAPPING_TEST_LOG"
      [ "$status" -eq 23 ]
      [ "$WATCHER_RESTARTED" -eq 0 ]
    '
  assert_contains "$(cat "$log")" \
    "strict-gate $case_dir/evidence.json strict"
  assert_contains "$(cat "$log")" "status=23 restarted=0"
  assert_not_contains "$(cat "$log")" "docker "
  assert_not_contains "$(cat "$log")" "curl "
  python3 - "$backup_root/four-channel-account-mapping-rollback.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload["runtime_rollback_required"] is False
assert payload["runtime_rollback_attempted"] is False
assert payload["runtime_rollback_not_required"] is True
assert payload["runtime_rebuild_health_hash_passed"] is False
assert payload["watcher_schema_restart_required"] is False
PY
  (
    cd "$backup_root"
    sha256sum -c SHA256SUMS >/dev/null
  )
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
    printf 'DELETED\t101\t0\n'
    printf 'DELETED\t202\t0\n'
    printf 'TARGET\t101\t%s\t%s\n' "$expected_dst" "$hash"
    printf 'TARGET\t202\t%s\t%s\n' "$expected_dst" "$hash"
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
    printf 'DELETED\t101\t0\n'
    printf 'DELETED\t202\t0\n'
    printf 'TARGET\t101\t%s\t%s\n' "$expected_dst" "$hash"
    printf 'TARGET\t202\t%s\t%s\n' "$expected_dst" "$hash"
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

test_verify_rejects_live_double_slash_deleted_mount() {
  local case_dir="$TMP_DIR/verify-live-deleted"
  local fake_bin="$case_dir/bin"
  local manifest="$case_dir/manifest.txt"
  local hash
  local source_path="/srv/trader-v3/container-patches/node.py"
  local deleted_source="${source_path}//deleted"
  local expected_dst="/app/app/node.py"
  hash=$(printf fixture | sha256sum | awk '{print $1}')
  make_fake_ssh "$fake_bin"
  mkdir -p "$case_dir"
  printf '%s %s %s\n' "$hash" "$source_path" "$expected_dst" >"$manifest"
  export FAKE_REMOTE_OUT
  FAKE_REMOTE_OUT=$(
    printf 'HASH\t%s\t%s\n' "$source_path" "$hash"
    printf 'PIDS\t101 202 \n'
    printf 'DELETED\t101\t0\n'
    printf 'DELETED\t202\t1\n'
    printf 'TARGET\t101\t%s\t%s\n' "$expected_dst" "$hash"
    printf 'TARGET\t202\t%s\t%s\n' "$expected_dst" "$hash"
    printf 'MOUNT\t101\t%s\t%s\n' "$source_path" "$expected_dst"
    printf 'MOUNT\t202\t%s\t%s\n' "$deleted_source" "$expected_dst"
  )

  local output
  local status
  set +e
  output=$(PATH="$fake_bin:$PATH" bash "$VERIFY" "$manifest" 2>&1)
  status=$?
  set -e

  if [ "$status" -ne 1 ]; then
    fail "verify accepted live //deleted mount source"
  fi
  assert_contains "$output" "pid=202 mountinfo 含 1 个 deleted mount"
}

test_verify_rejects_node_b_old_target_bytes() {
  local case_dir="$TMP_DIR/verify-node-b-old-bytes"
  local fake_bin="$case_dir/bin"
  local manifest="$case_dir/manifest.txt"
  local hash
  local old_hash
  local source_path="/srv/trader-v3/container-patches/nautilus_config.py"
  local expected_dst="/app/persistence/nautilus_config.py"
  hash=$(printf new-release | sha256sum | awk '{print $1}')
  old_hash=$(printf old-node-b | sha256sum | awk '{print $1}')
  make_fake_ssh "$fake_bin"
  mkdir -p "$case_dir"
  printf '%s %s %s\n' "$hash" "$source_path" "$expected_dst" >"$manifest"
  export FAKE_REMOTE_OUT
  FAKE_REMOTE_OUT=$(
    printf 'HASH\t%s\t%s\n' "$source_path" "$hash"
    printf 'PIDS\t101 202 \n'
    printf 'DELETED\t101\t0\n'
    printf 'DELETED\t202\t0\n'
    printf 'TARGET\t101\t%s\t%s\n' "$expected_dst" "$hash"
    printf 'TARGET\t202\t%s\t%s\n' "$expected_dst" "$old_hash"
    printf 'MOUNT\t101\t%s\t%s\n' "$source_path" "$expected_dst"
    printf 'MOUNT\t202\t%s\t%s\n' "$source_path" "$expected_dst"
  )

  local output
  local status
  set +e
  output=$(PATH="$fake_bin:$PATH" bash "$VERIFY" "$manifest" 2>&1)
  status=$?
  set -e

  if [ "$status" -ne 1 ]; then
    fail "verify accepted node-b old target bytes"
  fi
  assert_contains "$output" "pid=202 target sha256 不符"
}

test_hardening_deploy_contract_is_fail_closed
test_live_trade_release_artifacts_are_independently_required
test_account_stall_operation_lock_is_exclusive_and_configurable
test_pre_migration_database_backup_is_bounded_and_validated
test_pre_migration_database_backup_rejects_invalid_restore_listing
test_die_routes_failure_through_err_trap
test_node_recreate_is_sequential_and_memory_gated
test_bootstrap_generated_recreate_cleanup_preserves_existing_scripts
test_existing_recreate_mode_remains_compatible
test_generated_recreate_promotion_rejects_link_attacks
test_bootstrap_recreate_fleet_preserves_a_b_and_materializes_c_d
test_bootstrap_recreate_fleet_rejects_missing_a_b
test_on_err_restores_files_topology_and_old_image
test_immutable_config_and_rollout_ordering
test_migration_expectations_have_separate_schema_contracts
test_on_err_uses_compatible_recovery_after_migration
test_post_migration_recovery_resource_failure_blocks_promotion
test_on_err_selects_post_migration_recovery_branch
test_watcher_restart_and_rollback_use_compose_health_and_hash_gate
test_strict_watcher_mapping_failure_restores_prior_runtime
test_root_window_preflight_covers_stage5_inputs
test_root_window_fails_when_binance_destination_is_missing
test_verify_requires_patch_destination
test_verify_rejects_correct_source_at_wrong_destination
test_verify_accepts_exact_source_destination_pairs
test_verify_rejects_live_double_slash_deleted_mount
test_verify_rejects_node_b_old_target_bytes
echo "PASS: deployment fail-closed checks"
