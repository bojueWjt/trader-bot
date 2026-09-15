#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from build_immutable_watcher_image import (
    WATCHER_RELEASE_FILES,
    WATCHER_RUNTIME_MANIFEST_NAME,
    WATCHER_RUNTIME_MANIFEST_SCHEMA_VERSION,
    _payload_subject_sha256,
)
from make_container_bundle import (
    SCHEMA_EPOCHS,
    BundleError,
    build_bundle_from_commit,
    git_status_porcelain,
    read_git_file,
    resolve_git_commit,
    resolve_git_tree,
    verify_git_backed_bundle,
)


class ReleaseBundleError(ValueError):
    pass


MIGRATION_FILES = (
    "db/migrations/0001_canonical_schema.up.sql",
    "db/migrations/0001_canonical_schema.down.sql",
    "db/migrations/0002_processing_run_lease_and_statuses.up.sql",
    "db/migrations/0002_processing_run_lease_and_statuses.down.sql",
    "db/migrations/0003_operator_commands.up.sql",
    "db/migrations/0003_operator_commands.down.sql",
    "db/migrations/0004_hermes_decisions_unique_raw_message.up.sql",
    "db/migrations/0004_hermes_decisions_unique_raw_message.down.sql",
    "db/migrations/0005_order_management.up.sql",
    "db/migrations/0005_order_management.down.sql",
    "db/migrations/0006_trade_outcomes.up.sql",
    "db/migrations/0006_trade_outcomes.down.sql",
    "db/migrations/0007_exchange_state_mirror.up.sql",
    "db/migrations/0007_exchange_state_mirror.down.sql",
    "db/migrations/0008_orders_projection_protection_fields.up.sql",
    "db/migrations/0008_orders_projection_protection_fields.down.sql",
    "db/migrations/0009_trade_outcome_job_runs.up.sql",
    "db/migrations/0009_trade_outcome_job_runs.down.sql",
    "db/migrations/0010_evidence_and_poll_indexes.up.sql",
    "db/migrations/0010_evidence_and_poll_indexes.down.sql",
    "db/migrations/0011_live_safety.up.sql",
    "db/migrations/0011_live_safety.down.sql",
    "db/migrations/0012_control_plane_maintenance_fence.up.sql",
    "db/migrations/0012_control_plane_maintenance_fence.down.sql",
    "db/migrations/0013_four_account_rollout.up.sql",
    "db/migrations/0013_four_account_rollout.down.sql",
    "db/migrations/0014_cancel_order_contract.up.sql",
    "db/migrations/0014_cancel_order_contract.down.sql",
    "db/migrations/0015_refresh_evidence_command.up.sql",
    "db/migrations/0015_refresh_evidence_command.down.sql",
    "db/migrations/0016_control_plane_lock_privileges.up.sql",
    "db/migrations/0016_control_plane_lock_privileges.down.sql",
    "db/migrations/0017_operator_query_projection_reads.up.sql",
    "db/migrations/0017_operator_query_projection_reads.down.sql",
    "db/migrations/0018_projection_reliability.up.sql",
    "db/migrations/0018_projection_reliability.down.sql",
    "db/migrations/0019_signal_dispatch_queue_g2.up.sql",
    "db/migrations/0019_signal_dispatch_queue_g2.down.sql",
    "db/migrations/0020_signal_execution_g3.up.sql",
    "db/migrations/0020_signal_execution_g3.down.sql",
    "db/migrations/0021_position_revision_g3.up.sql",
    "db/migrations/0021_position_revision_g3.down.sql",
)
SYSTEMD_RESOURCE_FILES = (
    "infra/systemd/account-stall-account-node.conf",
    "infra/systemd/account-stall-control-plane-reader.conf",
    "infra/systemd/account-stall-control-plane-writer.conf",
    "infra/systemd/account-stall-redis.conf",
)
SIGNAL_RUNTIME_RELEASE_FILES = (
    ('services/control-plane/api/intent_trace.py', 'host/intent_trace.py'),
    ('services/control-plane/api/operator_queries.py', 'host/operator_queries.py'),
    ('services/control-plane/api/position_protection.py', 'host/position_protection.py'),
    ('services/control-plane/api/position_revision.py', 'host/position_revision.py'),
    ('services/control-plane/api/signal_handoff.py', 'host/signal_handoff.py'),
    ('services/control-plane/api/signal_status.py', 'host/signal_status.py'),
    ('services/control-plane/api/v1_mirror.py', 'host/v1_mirror.py'),
    ('services/control-plane/security/__init__.py', 'host/security/__init__.py'),
    ('services/control-plane/security/audit.py', 'host/security/audit.py'),
    ('services/control-plane/security/dangerous_ops.py', 'host/security/dangerous_ops.py'),
    ('services/control-plane/security/permissions.py', 'host/security/permissions.py'),
    ('services/control-plane/security/principal.py', 'host/security/principal.py'),
    ('services/control-plane/security/session_auth.py', 'host/security/session_auth.py'),
    ('services/control-plane/db/__init__.py', 'host/db/__init__.py'),
    ('services/control-plane/db/connection.py', 'host/db/connection.py'),
    ('services/control-plane/db/enums.py', 'host/db/enums.py'),
    ('services/control-plane/order_management/state_descriptor.py', 'host/order_management/state_descriptor.py'),
    ('services/control-plane/order_management/execution_jobs.py', 'host/order_management/execution_jobs.py'),
    ('services/control-plane/commands/commands.py', 'host/commands/commands.py'),
    ('services/control-plane/risk_state/risk_state.py', 'host/risk_state/risk_state.py'),
    ('services/control-plane/risk/governor.py', 'host/risk/governor.py'),
    ('services/control-plane/risk/policy.py', 'host/risk/policy.py'),
    ('services/hermes-worker/worker.py', 'host/hermes-worker/worker.py'),
    ('services/hermes-worker/hermes_client.py', 'host/hermes-worker/hermes_client.py'),
    ('services/hermes-worker/prompt.py', 'host/hermes-worker/prompt.py'),
    ('services/hermes-worker/operator_diagnostics.py', 'host/hermes-worker/operator_diagnostics.py'),
    ('services/hermes-worker/signal_operator.py', 'host/hermes-worker/signal_operator.py'),
    ('services/hermes-worker/queue/__init__.py', 'host/hermes-worker/queue/__init__.py'),
    ('services/hermes-worker/queue/claims.py', 'host/hermes-worker/queue/claims.py'),
    ('services/hermes-worker/queue/signal_queue.py', 'host/hermes-worker/queue/signal_queue.py'),
    ('services/ingress/ingress/__init__.py', 'host/ingress/__init__.py'),
    ('services/ingress/ingress/http.py', 'host/ingress/http.py'),
    ('services/ingress/ingress/service.py', 'host/ingress/service.py'),
    ('scripts/order_lifecycle_monitor.py', 'host/order_lifecycle_monitor.py'),
    ('hermes-profile/skills/trading/v3-trader/scripts/v3_query.py', 'host/v3_query.py'),
    ('packages/execution-domain/execution_domain/http_client.py', 'host/execution_domain/http_client.py'),
    ('packages/contracts/v1/hermes_decision.v1.json', 'packages/contracts/v1/hermes_decision.v1.json'),
    ('packages/contracts/v1/order_state.v1.json', 'packages/contracts/v1/order_state.v1.json'),
    ('packages/contracts/v1/order_management_settings.v1.json', 'packages/contracts/v1/order_management_settings.v1.json'),
    ('infra/systemd/trader-v3-signal-worker@.service', 'infra/systemd/trader-v3-signal-worker@.service'),
    ('infra/systemd/trader-v3-hermes-feeder-signal-cutover.conf', 'infra/systemd/trader-v3-hermes-feeder-signal-cutover.conf'),
)
RELEASE_FILES = (
    ("scripts/hk-deploy-20260803.sh", "hk-deploy-20260803.sh"),
    ("scripts/hk-gen-recreate-patched.py", "hk-gen-recreate-patched.py"),
    ("scripts/release_manifest.py", "release_manifest.py"),
    (
        "scripts/reviewed_release_rollout.py",
        "reviewed_release_rollout.py",
    ),
    ("scripts/live_node_config.py", "live_node_config.py"),
    (
        "services/nautilus-node/config/live-risk-policy.json",
        "live-risk-policy.json",
    ),
    (
        "scripts/build_immutable_node_image.py",
        "build_immutable_node_image.py",
    ),
    (
        "scripts/build_immutable_watcher_image.py",
        "build_immutable_watcher_image.py",
    ),
    (
        "scripts/hermes_signal_feeder.py",
        "host/hermes_signal_feeder.py",
    ),
    (
        "hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py",
        "host/v3_trade.py",
    ),
    (
        "hermes-profile/skills/trading/v3-trader/SKILL.md",
        "host/v3-trader/SKILL.md",
    ),
    ("scripts/hk-redis-rebaseline.sh", "hk-redis-rebaseline.sh"),
    (
        "scripts/hk-control-plane-isolation.sh",
        "hk-control-plane-isolation.sh",
    ),
    (
        "scripts/bootstrap_control_plane_roles.py",
        "bootstrap_control_plane_roles.py",
    ),
    (
        "scripts/account_a_live_trade_executor.py",
        "account_a_live_trade_executor.py",
    ),
    (
        "scripts/account_a_live_trade_http_adapter.py",
        "account_a_live_trade_http_adapter.py",
    ),
    ("scripts/redis_capacity_config.py", "redis_capacity_config.py"),
    (
        "scripts/refresh_redis_capacity_evidence.py",
        "refresh_redis_capacity_evidence.py",
    ),
    ("scripts/redis_namespace_janitor.py", "redis_namespace_janitor.py"),
    ("scripts/redis_namespace_registry.py", "redis_namespace_registry.py"),
    (
        "scripts/jp24_redis_dead_instance_janitor.py",
        "jp24_redis_dead_instance_janitor.py",
    ),
    (
        "infra/systemd/trader-v3-redis-dead-instance-janitor.service",
        "infra/systemd/trader-v3-redis-dead-instance-janitor.service",
    ),
    (
        "infra/systemd/trader-v3-redis-dead-instance-janitor.timer",
        "infra/systemd/trader-v3-redis-dead-instance-janitor.timer",
    ),
    (
        "infra/systemd/trader-v3-redis-namespace-janitor.service",
        "infra/systemd/trader-v3-redis-namespace-janitor.service",
    ),
    (
        "infra/systemd/trader-v3-redis-namespace-janitor.timer",
        "infra/systemd/trader-v3-redis-namespace-janitor.timer",
    ),
    ("infra/docker/nautilus/uv.node.lock", "uv.node.lock"),
    ("services/control-plane/api/read_api.py", "host/read_api.py"),
    ("services/control-plane/api/snapshot.py", "host/snapshot.py"),
    (
        "services/control-plane/decision_gateway/gateway.py",
        "host/decision_gateway/gateway.py",
    ),
    (
        "services/control-plane/tools/exchange_state_recorder.py",
        "host/exchange_state_recorder.py",
    ),
    (
        "packages/contracts/v1/system_snapshot.v1.json",
        "host/system_snapshot.v1.json",
    ),
    (
        "packages/execution-domain/execution_domain/__init__.py",
        "host/execution_domain/__init__.py",
    ),
    (
        "packages/execution-domain/execution_domain/contracts.py",
        "host/execution_domain/contracts.py",
    ),
    (
        "packages/execution-domain/execution_domain/control_plane.py",
        "host/execution_domain/control_plane.py",
    ),
    (
        "packages/execution-domain/execution_domain/portfolio_baseline.py",
        "host/execution_domain/portfolio_baseline.py",
    ),
    (
        (
            "packages/execution-domain/execution_domain/"
            "account_execution_ledger.py"
        ),
        "host/execution_domain/account_execution_ledger.py",
    ),
    (
        "packages/execution-domain/execution_domain/order_ownership.py",
        "host/execution_domain/order_ownership.py",
    ),
    (
        "packages/execution-domain/execution_domain/ownership_ledger.py",
        "host/execution_domain/ownership_ledger.py",
    ),
    (
        'packages/execution-domain/execution_domain/entry_batch.py',
        'host/execution_domain/entry_batch.py',
    ),
    (
        "packages/execution-domain/execution_domain/idempotency.py",
        "host/execution_domain/idempotency.py",
    ),
    (
        "packages/execution-domain/execution_domain/identifiers.py",
        "host/execution_domain/identifiers.py",
    ),
    (
        "services/control-plane/settings/__init__.py",
        "host/settings/__init__.py",
    ),
    (
        "services/control-plane/settings/apply_plan.py",
        "host/settings/apply_plan.py",
    ),
    (
        "services/control-plane/settings/import_export.py",
        "host/settings/import_export.py",
    ),
    (
        "services/control-plane/settings/permissions.py",
        "host/settings/permissions.py",
    ),
    (
        "services/control-plane/settings/publisher.py",
        "host/settings/publisher.py",
    ),
    (
        "services/control-plane/settings/resolver.py",
        "host/settings/resolver.py",
    ),
    (
        "services/control-plane/settings/router.py",
        "host/settings/router.py",
    ),
    (
        "services/control-plane/settings/schema.py",
        "host/settings/schema.py",
    ),
    (
        "services/control-plane/settings/service.py",
        "host/settings/service.py",
    ),
    (
        "services/control-plane/settings/versioning.py",
        "host/settings/versioning.py",
    ),
    (
        "services/control-plane/audit/__init__.py",
        "host/audit/__init__.py",
    ),
    (
        "services/control-plane/audit/settings_audit.py",
        "host/audit/settings_audit.py",
    ),
    (
        "services/control-plane/order_management/__init__.py",
        "host/order_management/__init__.py",
    ),
    (
        "services/control-plane/order_management/db_helpers.py",
        "host/order_management/db_helpers.py",
    ),
    (
        "services/control-plane/order_management/order_reducer.py",
        "host/order_management/order_reducer.py",
    ),
    (
        "services/control-plane/order_management/identifiers.py",
        "host/order_management/identifiers.py",
    ),
    (
        "services/control-plane/order_management/metrics.py",
        "host/order_management/metrics.py",
    ),
    (
        "services/control-plane/order_management/outbox.py",
        "host/order_management/outbox.py",
    ),
    (
        "services/control-plane/order_management/protection_watchdog.py",
        "host/order_management/protection_watchdog.py",
    ),
    ("scripts/ownership_ledger.py", "ownership_ledger.py"),
    (
        "services/nautilus-node/observability/__init__.py",
        "host/observability/__init__.py",
    ),
    (
        "services/nautilus-node/observability/_shared.py",
        "host/observability/_shared.py",
    ),
    (
        "services/nautilus-node/observability/logs.py",
        "host/observability/logs.py",
    ),
    (
        "services/nautilus-node/observability/metrics.py",
        "host/observability/metrics.py",
    ),
    (
        "services/nautilus-node/observability/tracing.py",
        "host/observability/tracing.py",
    ),
    (
        "services/control-plane/api/app_roles.py",
        "host/app_roles.py",
    ),
    (
        "services/control-plane/db/pools.py",
        "host/pools.py",
    ),
    (
        "services/control-plane/db/repository.py",
        "host/repository.py",
    ),
    (
        "scripts/rebuild_orders_projection.py",
        "scripts/rebuild_orders_projection.py",
    ),
    (
        "services/control-plane/db/migrate.py",
        "services/control-plane/db/migrate.py",
    ),
) + SIGNAL_RUNTIME_RELEASE_FILES + tuple(
    (source_path, release_path)
    for source_path, release_path, _target_path in WATCHER_RELEASE_FILES
) + tuple(
    (path, path)
    for path in MIGRATION_FILES + SYSTEMD_RESOURCE_FILES
)
REQUIRED_REDIS_RELEASE_PATHS = {
    "jp24_redis_dead_instance_janitor.py",
    "redis_namespace_janitor.py",
    "redis_namespace_registry.py",
    "infra/systemd/trader-v3-redis-dead-instance-janitor.service",
    "infra/systemd/trader-v3-redis-dead-instance-janitor.timer",
    "infra/systemd/trader-v3-redis-namespace-janitor.service",
    "infra/systemd/trader-v3-redis-namespace-janitor.timer",
}
REQUIRED_HERMES_RELEASE_PATHS = {
    "host/hermes_signal_feeder.py",
    "host/v3_trade.py",
    "host/v3-trader/SKILL.md",
}
HERMES_FEEDER_SOURCE_PATH = "scripts/hermes_signal_feeder.py"
HERMES_FEEDER_REQUIRED_SHA256 = (
    "1c8518eba5332fadc64276d4c475c1e0c6c0e9974ec785c7b7a2067f5e5ef8a2"
)
REQUIRED_CONTROL_PLANE_HOST_RELEASE_PATHS = {
    "host/read_api.py",
    "host/snapshot.py",
    "host/decision_gateway/gateway.py",
    "host/exchange_state_recorder.py",
}
REQUIRED_CONTROL_PLANE_BOOTSTRAP_RELEASE_FILES = {
    (
        "scripts/bootstrap_control_plane_roles.py",
        "bootstrap_control_plane_roles.py",
    ),
}
REQUIRED_LIVE_TRADE_RELEASE_FILES = {
    (
        "scripts/account_a_live_trade_executor.py",
        "account_a_live_trade_executor.py",
    ),
    (
        "scripts/account_a_live_trade_http_adapter.py",
        "account_a_live_trade_http_adapter.py",
    ),
}
FORBIDDEN_RELEASE_PATH_RE = re.compile(
    r"(?:^|/)(?:"
    r"apps/attention-android|"
    r"services/attention-(?:service|feishu-bridge)|"
    r"services/control-plane/attention|"
    r"packages/contracts/attention|"
    r"tests/(?:attention|control-plane/attention)|"
    r"db/migrations/0011_attention"
    r")(?:/|[._-]|$)",
    re.IGNORECASE,
)
FORBIDDEN_RELEASE_CONTENT_RE = re.compile(
    rb"(?:"
    rb"/v1/attention(?:/|[\"'])|"
    rb"ATTENTION_EVENT_PUBLIC_KEYS|"
    rb"from\s+attention\s+import|"
    rb"0011_attention"
    rb")",
    re.IGNORECASE,
)
SOURCE_MANIFEST_NAME = "release-source-manifest.json"
MIGRATION_MANIFEST_NAME = "migration-manifest.json"
DASHBOARD_MANIFEST_NAME = "dashboard-manifest.json"
DASHBOARD_SOURCE = "bridge/apps/dashboard"
DASHBOARD_BUILD_INPUTS = (
    DASHBOARD_SOURCE,
    "tests/nautilus/_fixtures/contracts-v1/data_quality_envelope_v1.schema.json",
)
SYSTEMD_RESOURCE_CONTRACT_NAME = "systemd-resource-contract.json"
MIGRATION_MANIFEST_SCHEMA_VERSION = "trader-v3-migration-manifest/v1"
SYSTEMD_RESOURCE_CONTRACT_SCHEMA_VERSION = (
    "trader-v3-systemd-resource-contract/v1"
)
NODE_RESOURCE_OWNER_UNITS = (
    "trader-v3-node-a",
    "trader-v3-node-b",
    "trader-v3-node-c",
    "trader-v3-node-d",
)
NODE_RESOURCE_DESTINATIONS = tuple(
    f"docker://{unit}/HostConfig"
    for unit in NODE_RESOURCE_OWNER_UNITS
)
MIGRATION_RUNNER = "services/control-plane/db/migrate.py"
MIGRATION_EVIDENCE_INDEXES_UP = (
    "db/migrations/0010_evidence_and_poll_indexes.up.sql"
)
MIGRATION_EVIDENCE_INDEXES_DOWN = (
    "db/migrations/0010_evidence_and_poll_indexes.down.sql"
)
MIGRATION_UP = "db/migrations/0011_live_safety.up.sql"
MIGRATION_DOWN = "db/migrations/0011_live_safety.down.sql"
MIGRATION_MAINTENANCE_FENCE_UP = (
    "db/migrations/0012_control_plane_maintenance_fence.up.sql"
)
MIGRATION_MAINTENANCE_FENCE_DOWN = (
    "db/migrations/0012_control_plane_maintenance_fence.down.sql"
)
MIGRATION_FOUR_ACCOUNT_ROLLOUT_UP = (
    "db/migrations/0013_four_account_rollout.up.sql"
)
MIGRATION_FOUR_ACCOUNT_ROLLOUT_DOWN = (
    "db/migrations/0013_four_account_rollout.down.sql"
)
MIGRATION_CANCEL_ORDER_CONTRACT_UP = (
    "db/migrations/0014_cancel_order_contract.up.sql"
)
MIGRATION_CANCEL_ORDER_CONTRACT_DOWN = (
    "db/migrations/0014_cancel_order_contract.down.sql"
)
MIGRATION_REFRESH_EVIDENCE_COMMAND_UP = (
    "db/migrations/0015_refresh_evidence_command.up.sql"
)
MIGRATION_REFRESH_EVIDENCE_COMMAND_DOWN = (
    "db/migrations/0015_refresh_evidence_command.down.sql"
)
MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_UP = (
    "db/migrations/0016_control_plane_lock_privileges.up.sql"
)
MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_DOWN = (
    "db/migrations/0016_control_plane_lock_privileges.down.sql"
)
MIGRATION_OPERATOR_QUERY_PROJECTION_READS_UP = (
    "db/migrations/0017_operator_query_projection_reads.up.sql"
)
MIGRATION_OPERATOR_QUERY_PROJECTION_READS_DOWN = (
    "db/migrations/0017_operator_query_projection_reads.down.sql"
)
MIGRATION_PROJECTION_RELIABILITY_UP = (
    "db/migrations/0018_projection_reliability.up.sql"
)
MIGRATION_PROJECTION_RELIABILITY_DOWN = (
    "db/migrations/0018_projection_reliability.down.sql"
)
MIGRATION_SIGNAL_DISPATCH_QUEUE_UP = "db/migrations/0019_signal_dispatch_queue_g2.up.sql"
MIGRATION_SIGNAL_DISPATCH_QUEUE_DOWN = "db/migrations/0019_signal_dispatch_queue_g2.down.sql"
MIGRATION_SIGNAL_EXECUTION_UP = "db/migrations/0020_signal_execution_g3.up.sql"
MIGRATION_SIGNAL_EXECUTION_DOWN = "db/migrations/0020_signal_execution_g3.down.sql"
MIGRATION_POSITION_REVISION_UP = "db/migrations/0021_position_revision_g3.up.sql"
MIGRATION_POSITION_REVISION_DOWN = "db/migrations/0021_position_revision_g3.down.sql"
MIGRATION_PREREQUISITES = (
    "db/migrations/0005_order_management.up.sql",
)
MIGRATION_STEPS = (
    {
        "version": "0010",
        "name": "evidence_and_poll_indexes",
        "up": MIGRATION_EVIDENCE_INDEXES_UP,
        "down": MIGRATION_EVIDENCE_INDEXES_DOWN,
        "prerequisites": list(MIGRATION_PREREQUISITES),
    },
    {
        "version": "0011",
        "name": "live_safety",
        "up": MIGRATION_UP,
        "down": MIGRATION_DOWN,
        "prerequisites": [MIGRATION_EVIDENCE_INDEXES_UP],
    },
    {
        "version": "0012",
        "name": "control_plane_maintenance_fence",
        "up": MIGRATION_MAINTENANCE_FENCE_UP,
        "down": MIGRATION_MAINTENANCE_FENCE_DOWN,
        "prerequisites": [MIGRATION_UP],
    },
    {
        "version": "0013",
        "name": "four_account_rollout",
        "up": MIGRATION_FOUR_ACCOUNT_ROLLOUT_UP,
        "down": MIGRATION_FOUR_ACCOUNT_ROLLOUT_DOWN,
        "prerequisites": [
            MIGRATION_UP,
            MIGRATION_MAINTENANCE_FENCE_UP,
        ],
    },
    {
        "version": "0014",
        "name": "cancel_order_contract",
        "up": MIGRATION_CANCEL_ORDER_CONTRACT_UP,
        "down": MIGRATION_CANCEL_ORDER_CONTRACT_DOWN,
        "prerequisites": [MIGRATION_FOUR_ACCOUNT_ROLLOUT_UP],
    },
    {
        "version": "0015",
        "name": "refresh_evidence_command",
        "up": MIGRATION_REFRESH_EVIDENCE_COMMAND_UP,
        "down": MIGRATION_REFRESH_EVIDENCE_COMMAND_DOWN,
        "prerequisites": [MIGRATION_CANCEL_ORDER_CONTRACT_UP],
    },
    {
        "version": "0016",
        "name": "control_plane_lock_privileges",
        "up": MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_UP,
        "down": MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_DOWN,
        "prerequisites": [MIGRATION_REFRESH_EVIDENCE_COMMAND_UP],
    },
    {
        "version": "0017",
        "name": "operator_query_projection_reads",
        "up": MIGRATION_OPERATOR_QUERY_PROJECTION_READS_UP,
        "down": MIGRATION_OPERATOR_QUERY_PROJECTION_READS_DOWN,
        "prerequisites": [MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_UP],
    },
    {
        "version": "0018",
        "name": "projection_reliability",
        "up": MIGRATION_PROJECTION_RELIABILITY_UP,
        "down": MIGRATION_PROJECTION_RELIABILITY_DOWN,
        "prerequisites": [MIGRATION_OPERATOR_QUERY_PROJECTION_READS_UP],
    },
    {"version": "0019", "name": "signal_dispatch_queue_g2",
     "up": MIGRATION_SIGNAL_DISPATCH_QUEUE_UP, "down": MIGRATION_SIGNAL_DISPATCH_QUEUE_DOWN,
     "prerequisites": [MIGRATION_PROJECTION_RELIABILITY_UP]},
    {"version": "0020", "name": "signal_execution_g3",
     "up": MIGRATION_SIGNAL_EXECUTION_UP, "down": MIGRATION_SIGNAL_EXECUTION_DOWN,
     "prerequisites": [MIGRATION_SIGNAL_DISPATCH_QUEUE_UP]},
    {"version": "0021", "name": "position_revision_g3",
     "up": MIGRATION_POSITION_REVISION_UP, "down": MIGRATION_POSITION_REVISION_DOWN,
     "prerequisites": [MIGRATION_SIGNAL_EXECUTION_UP]},
)
MIGRATION_PYTHON_DEPENDENCIES = ("psycopg2",)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SYSTEMD_RESOURCE_CONSUMERS = (
    {
        "artifact": "infra/systemd/account-stall-account-node.conf",
        "consumer_entrypoint": "hk-gen-recreate-patched.py",
        "application": "docker_host_config",
        "owner_units": list(NODE_RESOURCE_OWNER_UNITS),
        "destinations": list(NODE_RESOURCE_DESTINATIONS),
    },
    {
        "artifact": (
            "infra/systemd/account-stall-control-plane-reader.conf"
        ),
        "consumer_entrypoint": "hk-control-plane-isolation.sh",
        "application": "inline_service_unit",
        "owner_units": [
            "trader-v3-controlplane-operator-query.service",
        ],
        "destinations": [
            (
                "/etc/systemd/system/"
                "trader-v3-controlplane-operator-query.service"
            ),
        ],
    },
    {
        "artifact": (
            "infra/systemd/account-stall-control-plane-writer.conf"
        ),
        "consumer_entrypoint": "hk-control-plane-isolation.sh",
        "application": "inline_service_unit",
        "owner_units": [
            "trader-v3-controlplane-node-control.service",
            "trader-v3-controlplane-event-ingest.service",
        ],
        "destinations": [
            (
                "/etc/systemd/system/"
                "trader-v3-controlplane-node-control.service"
            ),
            (
                "/etc/systemd/system/"
                "trader-v3-controlplane-event-ingest.service"
            ),
        ],
    },
    {
        "artifact": "infra/systemd/account-stall-redis.conf",
        "consumer_entrypoint": "hk-redis-rebaseline.sh",
        "application": "docker_host_config",
        "owner_units": [
            "trader-v3-redis",
        ],
        "destinations": [
            "docker://trader-v3-redis/HostConfig",
        ],
    },
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_release_contract() -> None:
    for source_relative, destination_relative in RELEASE_FILES:
        if FORBIDDEN_RELEASE_PATH_RE.search(source_relative):
            raise ReleaseBundleError(
                "account-stall release includes forbidden Attention path: "
                f"{source_relative}"
            )
        if FORBIDDEN_RELEASE_PATH_RE.search(destination_relative):
            raise ReleaseBundleError(
                "account-stall release includes forbidden Attention path: "
                f"{destination_relative}"
            )
    release_paths = {
        destination_relative
        for _source_relative, destination_relative in RELEASE_FILES
    }
    missing_runtime = set(SIGNAL_RUNTIME_RELEASE_FILES) - set(RELEASE_FILES)
    if missing_runtime:
        raise ReleaseBundleError(f"release lacks signal runtime dependencies: {sorted(missing_runtime)}")
    missing = REQUIRED_REDIS_RELEASE_PATHS - release_paths
    if missing:
        raise ReleaseBundleError(
            "release lacks Redis namespace maintenance tools: "
            f"{sorted(missing)}"
        )
    missing_hermes = REQUIRED_HERMES_RELEASE_PATHS - release_paths
    if missing_hermes:
        raise ReleaseBundleError(
            "release lacks Hermes routing runtime files: "
            f"{sorted(missing_hermes)}"
        )
    missing_control_plane_host = (
        REQUIRED_CONTROL_PLANE_HOST_RELEASE_PATHS - release_paths
    )
    if missing_control_plane_host:
        raise ReleaseBundleError(
            "release lacks control-plane host runtime files: "
            f"{sorted(missing_control_plane_host)}"
        )
    missing_control_plane_bootstrap = (
        REQUIRED_CONTROL_PLANE_BOOTSTRAP_RELEASE_FILES - set(RELEASE_FILES)
    )
    if missing_control_plane_bootstrap:
        raise ReleaseBundleError(
            "release lacks control-plane role bootstrap files: "
            f"{sorted(missing_control_plane_bootstrap)}"
        )
    missing_live_trade = (
        REQUIRED_LIVE_TRADE_RELEASE_FILES - set(RELEASE_FILES)
    )
    if missing_live_trade:
        raise ReleaseBundleError(
            "release lacks live trade execution files: "
            f"{sorted(missing_live_trade)}"
        )
    watcher_release_paths = {
        destination_relative
        for _source_relative, destination_relative in RELEASE_FILES
        if destination_relative.startswith("watcher/")
    }
    expected_watcher_release_paths = {
        release_path
        for _source_path, release_path, _target_path in WATCHER_RELEASE_FILES
    }
    if watcher_release_paths != expected_watcher_release_paths:
        raise ReleaseBundleError(
            "release watcher runtime exact-set mismatch"
        )
    systemd_release_paths = {
        destination_relative
        for source_relative, destination_relative in RELEASE_FILES
        if source_relative in SYSTEMD_RESOURCE_FILES
    }
    if systemd_release_paths != set(SYSTEMD_RESOURCE_FILES):
        raise ReleaseBundleError(
            "release systemd resource exact-set mismatch"
        )
    consumer_paths = {
        str(item["artifact"])
        for item in SYSTEMD_RESOURCE_CONSUMERS
    }
    if consumer_paths != set(SYSTEMD_RESOURCE_FILES):
        raise ReleaseBundleError(
            "systemd resource consumer exact-set mismatch"
        )
    for item in SYSTEMD_RESOURCE_CONSUMERS:
        entrypoint = str(item["consumer_entrypoint"])
        owner_units = item["owner_units"]
        destinations = item["destinations"]
        if entrypoint not in release_paths:
            raise ReleaseBundleError(
                "systemd resource consumer entrypoint is absent from "
                f"release: {entrypoint}"
            )
        if (
            not isinstance(owner_units, list)
            or not owner_units
            or not isinstance(destinations, list)
            or len(owner_units) != len(destinations)
        ):
            raise ReleaseBundleError(
                "systemd resource owner/destination contract is invalid"
            )
        application = str(item["application"])
        if application == "docker_host_config":
            if any(
                not str(destination).startswith("docker://")
                for destination in destinations
            ):
                raise ReleaseBundleError(
                    "Docker resource destination is invalid"
                )
        elif any(
            not str(destination).startswith("/etc/systemd/system/")
            for destination in destinations
        ):
            raise ReleaseBundleError(
                "systemd resource destination escapes systemd root"
            )
        if item["artifact"] == (
            "infra/systemd/account-stall-account-node.conf"
        ):
            if tuple(owner_units) != NODE_RESOURCE_OWNER_UNITS:
                raise ReleaseBundleError(
                    "account node resource owners must cover A-D exact-set"
                )
            if tuple(destinations) != NODE_RESOURCE_DESTINATIONS:
                raise ReleaseBundleError(
                    "account node resource destinations must cover A-D "
                    "exact-set"
                )


def _validate_release_payload(
    source_relative: str,
    payload: bytes,
) -> None:
    if source_relative == HERMES_FEEDER_SOURCE_PATH:
        digest = _sha256_bytes(payload)
        if digest != HERMES_FEEDER_REQUIRED_SHA256:
            raise ReleaseBundleError(
                "Hermes feeder SHA256 does not match the canonical "
                f"watcher ingress version: {digest}"
            )
    if FORBIDDEN_RELEASE_CONTENT_RE.search(payload):
        raise ReleaseBundleError(
            "account-stall release includes forbidden Attention content: "
            f"{source_relative}"
        )


def _copy_release_files(
    repo_root: Path,
    output_dir: Path,
    source_commit: str,
) -> list[dict[str, object]]:
    files = []
    for source_relative, destination_relative in RELEASE_FILES:
        payload, blob_id, git_mode = read_git_file(
            repo_root,
            source_commit,
            source_relative,
        )
        _validate_release_payload(source_relative, payload)
        destination = output_dir / destination_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        if git_mode == "100755":
            destination.chmod(0o755)
        digest = _sha256_bytes(payload)
        if _sha256(destination) != digest:
            raise ReleaseBundleError(
                f"release copy hash mismatch: {destination_relative}"
            )
        files.append(
            {
                "source_path": source_relative,
                "release_path": destination_relative,
                "source_git_blob": blob_id,
                "source_git_mode": git_mode,
                "sha256": digest,
                "size": len(payload),
            }
        )
    return files


def _validate_live_risk_policy(output_dir: Path) -> None:
    release_manifest_path = output_dir / "release_manifest.py"
    policy_path = output_dir / "live-risk-policy.json"
    validator = """
import importlib.util
import json
import sys
from pathlib import Path

release_manifest_path = Path(sys.argv[1])
policy_path = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location(
    "release_payload_manifest",
    release_manifest_path,
)
if spec is None or spec.loader is None:
    raise SystemExit("release manifest module cannot be loaded")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
policy = json.loads(policy_path.read_text(encoding="utf-8"))
module._validated_runtime_resources(
    policy.get("runtime_resource_contract"),
    label="live risk policy runtime_resource_contract",
)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            validator,
            str(release_manifest_path),
            str(policy_path),
        ],
        cwd=output_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip()
        if not detail:
            detail = result.stdout.strip()
        if not detail:
            detail = "runtime resource validation failed"
        raise ReleaseBundleError(
            "release live risk policy runtime resource contract is invalid: "
            f"{detail}"
        )


def _write_source_manifest(
    output_dir: Path,
    *,
    source_ref: str,
    source_commit: str,
    source_tree: str,
    initial_head: str,
    bundle_manifest: dict,
    release_files: list[dict[str, object]],
    migration_manifest_path: Path,
    systemd_resource_contract_path: Path,
    watcher_runtime_manifest_path: Path,
    dashboard_manifest_path: Path,
) -> Path:
    bundle_manifest_path = output_dir / "bundle-manifest.json"
    manifest = {
        "schema_version": "trader-v3-release-source/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_mode": "git_object",
        "source_ref": source_ref,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "head_at_start": initial_head,
        "head_at_end": initial_head,
        "source_commit_at_end": source_commit,
        "source_tree_at_end": source_tree,
        "schema_epochs": dict(SCHEMA_EPOCHS),
        "bundle_manifest_sha256": _sha256(bundle_manifest_path),
        "bundle_repo_commit": bundle_manifest["repo_commit"],
        "bundle_repo_tree": bundle_manifest["repo_tree"],
        "migration": {
            "runner": MIGRATION_RUNNER,
            "up": MIGRATION_UP,
            "down": MIGRATION_DOWN,
            "prerequisites": list(MIGRATION_PREREQUISITES),
            "steps": [dict(item) for item in MIGRATION_STEPS],
            "python_dependencies": list(
                MIGRATION_PYTHON_DEPENDENCIES
            ),
            "migration_files": list(MIGRATION_FILES),
            "db_schema_epoch": SCHEMA_EPOCHS["db"],
            "manifest": MIGRATION_MANIFEST_NAME,
            "manifest_sha256": _sha256(migration_manifest_path),
        },
        "systemd_resource_contract": {
            "path": SYSTEMD_RESOURCE_CONTRACT_NAME,
            "sha256": _sha256(systemd_resource_contract_path),
        },
        "watcher_runtime": {
            "manifest": WATCHER_RUNTIME_MANIFEST_NAME,
            "manifest_sha256": _sha256(
                watcher_runtime_manifest_path
            ),
        },
        "dashboard": {
            "manifest": DASHBOARD_MANIFEST_NAME,
            "manifest_sha256": _sha256(dashboard_manifest_path),
        },
        "files": release_files,
    }
    path = output_dir / SOURCE_MANIFEST_NAME
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _build_dashboard(repo_root: Path, output_dir: Path, source_commit: str) -> Path:
    """Build only committed frontend sources and bind derived bytes to this release."""
    versions = {}
    for tool in ("node", "npm"):
        binary = shutil.which(tool)
        if binary is None:
            raise ReleaseBundleError(f"dashboard build requires {tool} on PATH")
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True)
        versions[tool] = result.stdout.strip()
    node_version = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", versions["node"])
    if node_version is None or tuple(map(int, node_version.groups())) < (22, 12, 0):
        raise ReleaseBundleError("dashboard build requires Node >=22.12.0")
    archive = subprocess.run(
        ["git", "archive", "--format=tar", source_commit, *DASHBOARD_BUILD_INPUTS],
        cwd=repo_root, capture_output=True, check=True,
    ).stdout
    source_tree = resolve_git_tree(repo_root, source_commit)
    with tempfile.TemporaryDirectory(prefix="dashboard-build-", dir=output_dir.parent) as temporary:
        work = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                relative = Path(member.name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ReleaseBundleError("dashboard Git archive contains an unsafe path")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ReleaseBundleError("dashboard Git sources must be regular files")
                if relative.is_relative_to(DASHBOARD_SOURCE):
                    source_relative = relative.relative_to(DASHBOARD_SOURCE)
                    if any(part in {"dist", "node_modules"} or part.startswith(".env") for part in source_relative.parts):
                        continue
                elif relative.as_posix() not in DASHBOARD_BUILD_INPUTS:
                    raise ReleaseBundleError("dashboard archive contains an undeclared build input")
                target = work / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())
        app = work / DASHBOARD_SOURCE
        lock = app / "package-lock.json"
        if not lock.is_file():
            raise ReleaseBundleError("dashboard requires a committed npm lockfile")
        lock_sha256 = _sha256(lock)
        env = {key: value for key, value in os.environ.items() if not key.startswith("VITE_")}
        env.update(VITE_API_BASE_URL="", VITE_API_BASE="", VITE_BASE_PATH="/", VITE_AUTH_DISABLED="false")
        for command in (["npm", "ci", "--no-audit", "--no-fund"], ["npm", "run", "build"]):
            result = subprocess.run(command, cwd=app, env=env, capture_output=True, text=True, timeout=600)
            if result.returncode:
                raise ReleaseBundleError("dashboard build failed: " + (result.stderr or result.stdout)[-2000:])
        if _sha256(lock) != lock_sha256:
            raise ReleaseBundleError("dashboard build modified the committed lockfile")
        dist = app / "dist"
        if not (dist / "index.html").is_file():
            raise ReleaseBundleError("dashboard build did not produce dist/index.html")
        files = {}
        for source in sorted(dist.rglob("*")):
            if source.is_symlink():
                raise ReleaseBundleError("dashboard build output must not contain symlinks")
            if not source.is_file():
                continue
            relative = "dashboard/dist/" + source.relative_to(dist).as_posix()
            target = output_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            files[relative] = _sha256(target)
    manifest = {
        "schema_version": "trader-v3-dashboard/v1", "source_commit": source_commit,
        "source_tree": source_tree, "source_subdir": DASHBOARD_SOURCE,
        "build_inputs": list(DASHBOARD_BUILD_INPUTS),
        "package_lock_sha256": lock_sha256, "tool_versions": versions,
        "api_base": "", "base_path": "/", "auth_disabled": False,
        "operator_path": "/m/v1/operator/orders", "files": files,
    }
    path = output_dir / DASHBOARD_MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_migration_manifest(output_dir: Path) -> Path:
    migrations = []
    for relative in MIGRATION_FILES:
        migrations.append(
            {
                "path": relative,
                "sha256": _sha256(output_dir / relative),
            }
        )
    manifest = {
        "schema_version": MIGRATION_MANIFEST_SCHEMA_VERSION,
        "schema_epoch": SCHEMA_EPOCHS["db"],
        "runner": {
            "path": MIGRATION_RUNNER,
            "sha256": _sha256(output_dir / MIGRATION_RUNNER),
        },
        "up": MIGRATION_UP,
        "down": MIGRATION_DOWN,
        "prerequisites": list(MIGRATION_PREREQUISITES),
        "steps": [dict(item) for item in MIGRATION_STEPS],
        "migrations": migrations,
    }
    path = output_dir / MIGRATION_MANIFEST_NAME
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _write_watcher_runtime_manifest(
    output_dir: Path,
    release_files: list[dict[str, object]],
) -> Path:
    by_release_path = {
        str(item["release_path"]): item
        for item in release_files
    }
    files = []
    for source_path, release_path, target_path in WATCHER_RELEASE_FILES:
        item = by_release_path.get(release_path)
        if item is None:
            raise ReleaseBundleError(
                f"watcher runtime release file is missing: {release_path}"
            )
        if item["source_path"] != source_path:
            raise ReleaseBundleError(
                f"watcher runtime source mismatch: {release_path}"
            )
        files.append(
            {
                "source_path": source_path,
                "release_path": release_path,
                "target_path": target_path,
                "sha256": item["sha256"],
                "size": item["size"],
            }
        )
    manifest = {
        "schema_version": WATCHER_RUNTIME_MANIFEST_SCHEMA_VERSION,
        "files": files,
        "payload_subject_sha256": _payload_subject_sha256(files),
    }
    path = output_dir / WATCHER_RUNTIME_MANIFEST_NAME
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _systemd_resource_properties(path: Path) -> dict[str, str]:
    properties = {}
    section = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, separator, value = line.partition("=")
        if section != "Service":
            continue
        if not separator:
            raise ReleaseBundleError(
                f"systemd resource artifact is invalid: {path}"
            )
        normalized_key = key.strip()
        if normalized_key in properties:
            raise ReleaseBundleError(
                f"systemd resource property is duplicated: {normalized_key}"
            )
        properties[normalized_key] = value.strip()
    expected = {
        "MemoryMax",
        "MemorySwapMax",
        "CPUQuota",
        "TasksMax",
        "LimitNOFILE",
        "Restart",
        "RestartSec",
    }
    if set(properties) != expected:
        raise ReleaseBundleError(
            f"systemd resource properties mismatch: {path}"
        )
    return properties


def _systemd_memory_bytes(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([KMG])", value)
    if match is None:
        raise ReleaseBundleError(
            f"systemd memory value is invalid: {value}"
        )
    multipliers = {
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
    }
    return int(match.group(1)) * multipliers[match.group(2)]


def _docker_host_config_from_systemd(path: Path) -> dict[str, object]:
    properties = _systemd_resource_properties(path)
    memory_bytes = _systemd_memory_bytes(properties["MemoryMax"])
    if properties["MemorySwapMax"] != "0":
        raise ReleaseBundleError(
            "Docker resource contract requires MemorySwapMax=0"
        )
    quota = properties["CPUQuota"]
    match = re.fullmatch(r"([1-9][0-9]*)%", quota)
    if match is None:
        raise ReleaseBundleError(
            f"systemd CPUQuota is invalid: {quota}"
        )
    nano_cpus = int(match.group(1)) * 10_000_000
    tasks_max = properties["TasksMax"]
    nofile = properties["LimitNOFILE"]
    if re.fullmatch(r"[1-9][0-9]*", tasks_max) is None:
        raise ReleaseBundleError("systemd TasksMax is invalid")
    if re.fullmatch(r"[1-9][0-9]*", nofile) is None:
        raise ReleaseBundleError("systemd LimitNOFILE is invalid")
    restart = properties["Restart"]
    if restart not in {"always", "on-failure"}:
        raise ReleaseBundleError(
            "systemd Restart cannot map to Docker HostConfig"
        )
    return {
        "memory_bytes": memory_bytes,
        "memory_swap_bytes": memory_bytes,
        "nano_cpus": nano_cpus,
        "pids_limit": int(tasks_max),
        "nofile_soft": int(nofile),
        "nofile_hard": int(nofile),
        "restart_policy": restart,
    }


def _write_systemd_resource_contract(output_dir: Path) -> Path:
    resources = []
    for raw in SYSTEMD_RESOURCE_CONSUMERS:
        item = dict(raw)
        artifact = str(item["artifact"])
        item["sha256"] = _sha256(output_dir / artifact)
        if item["application"] == "docker_host_config":
            item["effective_docker_host_config"] = (
                _docker_host_config_from_systemd(
                    output_dir / artifact
                )
            )
        resources.append(item)
    manifest = {
        "schema_version": SYSTEMD_RESOURCE_CONTRACT_SCHEMA_VERSION,
        "resources": resources,
    }
    path = output_dir / SYSTEMD_RESOURCE_CONTRACT_NAME
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _verify_release_sources(
    repo_root: Path,
    output_dir: Path,
    source_commit: str,
    release_files: list[dict[str, object]],
) -> None:
    for item in release_files:
        source_relative = str(item["source_path"])
        payload, blob_id, git_mode = read_git_file(
            repo_root,
            source_commit,
            source_relative,
        )
        _validate_release_payload(source_relative, payload)
        digest = _sha256_bytes(payload)
        if item["source_git_blob"] != blob_id:
            raise ReleaseBundleError(
                f"release Git blob changed: {source_relative}"
            )
        if item["source_git_mode"] != git_mode:
            raise ReleaseBundleError(
                f"release Git mode changed: {source_relative}"
            )
        if item["sha256"] != digest:
            raise ReleaseBundleError(
                f"release source hash changed: {source_relative}"
            )
        destination = output_dir / str(item["release_path"])
        if not destination.is_file() or _sha256(destination) != digest:
            raise ReleaseBundleError(
                f"release payload hash mismatch: {item['release_path']}"
            )


def _write_checksums(output_dir: Path) -> Path:
    checksum_path = output_dir / "SHA256SUMS"
    entries = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or path == checksum_path:
            continue
        relative = path.relative_to(output_dir).as_posix()
        entries.append(f"{_sha256(path)}  {relative}")
    checksum_path.write_text(
        "\n".join(entries) + "\n",
        encoding="utf-8",
    )
    return checksum_path


def _verify_checksums(output_dir: Path, checksum_path: Path) -> None:
    result = subprocess.run(
        ["sha256sum", "-c", checksum_path.name],
        cwd=output_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseBundleError(
            result.stderr.strip()
            or "release SHA256SUMS verification failed"
        )
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if (
            _SHA256_RE.fullmatch(digest) is None
            or not separator
            or not relative
        ):
            raise ReleaseBundleError("release SHA256SUMS entry is invalid")


def _publish_staged_release(staging_dir: Path, output_dir: Path) -> None:
    if output_dir.exists():
        if any(output_dir.iterdir()):
            raise ReleaseBundleError(
                f"output directory is not empty: {output_dir}"
            )
        output_dir.rmdir()
    os.replace(staging_dir, output_dir)


def build_release(
    repo_root: Path,
    output_dir: Path,
    *,
    source_ref: str = "HEAD",
) -> Path:
    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ReleaseBundleError(
            f"output directory is not empty: {output_dir}"
        )
    initial_status = git_status_porcelain(repo_root)
    if initial_status:
        raise ReleaseBundleError(
            "release must be built from a clean git worktree"
        )
    _validate_release_contract()
    initial_head = resolve_git_commit(repo_root, "HEAD")
    source_commit = resolve_git_commit(repo_root, source_ref)
    source_tree = resolve_git_tree(repo_root, source_commit)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.build-",
        dir=output_dir.parent,
    ) as temp_root:
        staging_dir = Path(temp_root) / "payload"
        staging_dir.mkdir()
        bundle_manifest = build_bundle_from_commit(
            repo_root,
            staging_dir,
            source_ref=source_ref,
            repo_dirty=False,
            expected_head=initial_head,
        )
        release_files = _copy_release_files(
            repo_root,
            staging_dir,
            source_commit,
        )
        _validate_live_risk_policy(staging_dir)
        migration_manifest_path = _write_migration_manifest(staging_dir)
        watcher_runtime_manifest_path = (
            _write_watcher_runtime_manifest(
                staging_dir,
                release_files,
            )
        )
        systemd_resource_contract_path = (
            _write_systemd_resource_contract(staging_dir)
        )
        dashboard_manifest_path = _build_dashboard(repo_root, staging_dir, source_commit)
        _write_source_manifest(
            staging_dir,
            source_ref=source_ref,
            source_commit=source_commit,
            source_tree=source_tree,
            initial_head=initial_head,
            bundle_manifest=bundle_manifest,
            release_files=release_files,
            migration_manifest_path=migration_manifest_path,
            systemd_resource_contract_path=(
                systemd_resource_contract_path
            ),
            watcher_runtime_manifest_path=(
                watcher_runtime_manifest_path
            ),
            dashboard_manifest_path=dashboard_manifest_path,
        )
        checksum_path = _write_checksums(staging_dir)

        verify_git_backed_bundle(
            repo_root,
            staging_dir,
            bundle_manifest,
        )
        _verify_release_sources(
            repo_root,
            staging_dir,
            source_commit,
            release_files,
        )
        _verify_checksums(staging_dir, checksum_path)

        final_head = resolve_git_commit(repo_root, "HEAD")
        if final_head != initial_head:
            raise ReleaseBundleError(
                "HEAD changed during release build"
            )
        final_source_commit = resolve_git_commit(
            repo_root,
            source_ref,
        )
        if final_source_commit != source_commit:
            raise ReleaseBundleError(
                "source Git ref changed during release build"
            )
        if resolve_git_tree(repo_root, source_commit) != source_tree:
            raise ReleaseBundleError(
                "source Git tree changed during release build"
            )
        if git_status_porcelain(repo_root) != initial_status:
            raise ReleaseBundleError(
                "worktree changed during release build"
            )

        _publish_staged_release(staging_dir, output_dir)
    return output_dir / "SHA256SUMS"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the complete account-stall hardening HK release."
    )
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--source-commit", default="HEAD")
    args = parser.parse_args(argv)
    try:
        checksum_path = build_release(
            args.repo_root,
            args.output_dir,
            source_ref=args.source_commit,
        )
    except (BundleError, OSError, ReleaseBundleError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    print(f"WROTE account-stall release: {args.output_dir}")
    print(f"SHA256SUMS={checksum_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
