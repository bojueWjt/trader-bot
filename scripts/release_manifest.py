#!/usr/bin/env python3
"""Build and verify the trader-v3 four-node release identity.

The manifest binds a reviewed source bundle to the currently selected image,
the normalized account node configuration, and the dependency lock. Live
verification checks every selected container without printing environment values or
configuration contents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = "trader-v3-release/v4"
PREVIOUS_SCHEMA_VERSION = "trader-v3-release/v3"
LEGACY_SCHEMA_VERSION = "trader-v3-release/v2"
SUPPORTED_SCHEMA_VERSIONS = {
    LEGACY_SCHEMA_VERSION,
    PREVIOUS_SCHEMA_VERSION,
    SCHEMA_VERSION,
}
NODE_CONFIG_SCHEMA_VERSIONS = {
    PREVIOUS_SCHEMA_VERSION,
    SCHEMA_VERSION,
}
SCHEMA_EPOCHS = {
    "app": "account-stall-hardening-runtime/v1",
    "db": "0018_projection_reliability",
    "redis": "fenced-generation-namespace/v2",
}
DEFAULT_CONTAINERS = (
    "trader-v3-node-a",
    "trader-v3-node-b",
    "trader-v3-node-c",
    "trader-v3-node-d",
)
REQUIRED_ACCOUNTS = {
    "account-a",
    "account-b",
    "account-c",
    "account-d",
}
EXPECTED_CONTAINERS = {
    "account-a": "trader-v3-node-a",
    "account-b": "trader-v3-node-b",
    "account-c": "trader-v3-node-c",
    "account-d": "trader-v3-node-d",
}
CONFIG_MOUNT_TARGET = "/cfg.json"
DEFAULT_PATCH_ROOT = Path("/srv/trader-v3/container-patches")
DELIVERY_IMMUTABLE = "immutable_image"
DELIVERY_TRANSITION = "transition_bind_mount"
DELIVERY_MODES = (DELIVERY_IMMUTABLE, DELIVERY_TRANSITION)
RELEASE_PURPOSE_HARDENING = "account_stall_hardening"
RELEASE_PURPOSE_EMERGENCY_ROLLBACK = "emergency_rollback"
RELEASE_PURPOSES = (
    RELEASE_PURPOSE_HARDENING,
    RELEASE_PURPOSE_EMERGENCY_ROLLBACK,
)
RUNTIME_RESOURCES_SCHEMA_VERSION = "trader-v3-runtime-resources/v1"
NODE_CONFIG_CONTRACT_SCHEMA_VERSION = (
    "trader-v3-node-config-contract/v1"
)
LABEL_RELEASE_COMMIT = "com.trader.release.commit"
LABEL_RELEASE_CONFIG = "com.trader.release.config-sha256"
LABEL_RELEASE_DELIVERY = "com.trader.release.delivery-mode"
LABEL_RELEASE_ID = "com.trader.release.id"
LABEL_RELEASE_IMAGE = "com.trader.release.image-digest"
LABEL_BUILD_ATTESTATION_SUBJECT = (
    "com.trader.build.attestation-subject-sha256"
)
LABEL_BUILD_BASE_IMAGE = "com.trader.build.base-image-digest"
LABEL_BUILD_BUNDLE_MANIFEST = (
    "com.trader.build.bundle-manifest-sha256"
)
LABEL_BUILD_DEPENDENCY_INVENTORY = (
    "com.trader.build.dependency-inventory-sha256"
)
LABEL_BUILD_DEPENDENCY_LOCK = (
    "com.trader.build.dependency-lock-sha256"
)
LABEL_BUILD_DOCKERFILE = "com.trader.build.dockerfile-sha256"
LABEL_BUILD_MIGRATION_MANIFEST = (
    "com.trader.build.migration-manifest-sha256"
)
LABEL_BUILD_RELEASE_SOURCE_MANIFEST = (
    "com.trader.build.release-source-manifest-sha256"
)
IMMUTABLE_DEPENDENCY_LOCK_TARGET = "/app/.release/dependency.lock"
IMMUTABLE_DEPENDENCY_INVENTORY_TARGET = (
    "/app/.release/dependency-inventory.json"
)
IMMUTABLE_MIGRATION_MANIFEST_TARGET = (
    "/app/.release/migration-manifest.json"
)
DEPENDENCY_INVENTORY_SCHEMA_VERSION = (
    "trader-v3-dependency-inventory/v1"
)
MIGRATION_MANIFEST_SCHEMA_VERSION = "trader-v3-migration-manifest/v1"
MIGRATION_MANIFEST_NAME = "migration-manifest.json"
WATCHER_RUNTIME_MANIFEST_NAME = "watcher-runtime-manifest.json"
RELEASE_SOURCE_MANIFEST_NAME = "release-source-manifest.json"
SYSTEMD_RESOURCE_CONTRACT_NAME = "systemd-resource-contract.json"
SHA256SUMS_NAME = "SHA256SUMS"
RELEASE_DEPENDENCY_LOCK_NAME = "uv.node.lock"
DEPENDENCY_INVENTORY_NAME = "dependency-inventory.json"
BUILD_ATTESTATION_NAME = "immutable-build-attestation.json"
REVIEWER_TRUST_PROOF_NAME = "reviewer-trust-proof.json"
BUILD_ATTESTATION_SCHEMA_VERSION = (
    "trader-v3-immutable-build-attestation/v1"
)
BUILD_ATTESTATION_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "generated_at",
        "base_image_digest",
        "dockerfile_sha256",
        "bundle_manifest",
        "release_source_manifest",
        "dependency_lock",
        "dependency_inventory",
        "migration_manifest",
        "build_subject_sha256",
        "image_digest",
        "image_labels",
    }
)
REVIEWER_TRUST_PROOF_SCHEMA_VERSION = (
    "trader-v3-reviewer-trust-proof/v1"
)
SYSTEMD_RESOURCE_CONTRACT_SCHEMA_VERSION = (
    "trader-v3-systemd-resource-contract/v1"
)
MIGRATION_RUNNER_PATH = "services/control-plane/db/migrate.py"
MIGRATION_EVIDENCE_INDEXES_UP_PATH = (
    "db/migrations/0010_evidence_and_poll_indexes.up.sql"
)
MIGRATION_EVIDENCE_INDEXES_DOWN_PATH = (
    "db/migrations/0010_evidence_and_poll_indexes.down.sql"
)
MIGRATION_UP_PATH = "db/migrations/0011_live_safety.up.sql"
MIGRATION_DOWN_PATH = "db/migrations/0011_live_safety.down.sql"
MIGRATION_MAINTENANCE_FENCE_UP_PATH = (
    "db/migrations/0012_control_plane_maintenance_fence.up.sql"
)
MIGRATION_MAINTENANCE_FENCE_DOWN_PATH = (
    "db/migrations/0012_control_plane_maintenance_fence.down.sql"
)
MIGRATION_FOUR_ACCOUNT_ROLLOUT_UP_PATH = (
    "db/migrations/0013_four_account_rollout.up.sql"
)
MIGRATION_FOUR_ACCOUNT_ROLLOUT_DOWN_PATH = (
    "db/migrations/0013_four_account_rollout.down.sql"
)
MIGRATION_CANCEL_ORDER_CONTRACT_UP_PATH = (
    "db/migrations/0014_cancel_order_contract.up.sql"
)
MIGRATION_CANCEL_ORDER_CONTRACT_DOWN_PATH = (
    "db/migrations/0014_cancel_order_contract.down.sql"
)
MIGRATION_REFRESH_EVIDENCE_COMMAND_UP_PATH = (
    "db/migrations/0015_refresh_evidence_command.up.sql"
)
MIGRATION_REFRESH_EVIDENCE_COMMAND_DOWN_PATH = (
    "db/migrations/0015_refresh_evidence_command.down.sql"
)
MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_UP_PATH = (
    "db/migrations/0016_control_plane_lock_privileges.up.sql"
)
MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_DOWN_PATH = (
    "db/migrations/0016_control_plane_lock_privileges.down.sql"
)
MIGRATION_OPERATOR_QUERY_PROJECTION_READS_UP_PATH = (
    "db/migrations/0017_operator_query_projection_reads.up.sql"
)
MIGRATION_OPERATOR_QUERY_PROJECTION_READS_DOWN_PATH = (
    "db/migrations/0017_operator_query_projection_reads.down.sql"
)
MIGRATION_PROJECTION_RELIABILITY_UP_PATH = (
    "db/migrations/0018_projection_reliability.up.sql"
)
MIGRATION_PROJECTION_RELIABILITY_DOWN_PATH = (
    "db/migrations/0018_projection_reliability.down.sql"
)
MIGRATION_PREREQUISITE_PATHS = (
    "db/migrations/0005_order_management.up.sql",
)
CANONICAL_MIGRATION_PATHS = (
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
    MIGRATION_EVIDENCE_INDEXES_UP_PATH,
    MIGRATION_EVIDENCE_INDEXES_DOWN_PATH,
    MIGRATION_UP_PATH,
    MIGRATION_DOWN_PATH,
    MIGRATION_MAINTENANCE_FENCE_UP_PATH,
    MIGRATION_MAINTENANCE_FENCE_DOWN_PATH,
    MIGRATION_FOUR_ACCOUNT_ROLLOUT_UP_PATH,
    MIGRATION_FOUR_ACCOUNT_ROLLOUT_DOWN_PATH,
    MIGRATION_CANCEL_ORDER_CONTRACT_UP_PATH,
    MIGRATION_CANCEL_ORDER_CONTRACT_DOWN_PATH,
    MIGRATION_REFRESH_EVIDENCE_COMMAND_UP_PATH,
    MIGRATION_REFRESH_EVIDENCE_COMMAND_DOWN_PATH,
    MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_UP_PATH,
    MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_DOWN_PATH,
    MIGRATION_OPERATOR_QUERY_PROJECTION_READS_UP_PATH,
    MIGRATION_OPERATOR_QUERY_PROJECTION_READS_DOWN_PATH,
    MIGRATION_PROJECTION_RELIABILITY_UP_PATH,
    MIGRATION_PROJECTION_RELIABILITY_DOWN_PATH,
)
CANONICAL_MIGRATION_STEPS = (
    {
        "version": "0010",
        "name": "evidence_and_poll_indexes",
        "up": MIGRATION_EVIDENCE_INDEXES_UP_PATH,
        "down": MIGRATION_EVIDENCE_INDEXES_DOWN_PATH,
        "prerequisites": list(MIGRATION_PREREQUISITE_PATHS),
    },
    {
        "version": "0011",
        "name": "live_safety",
        "up": MIGRATION_UP_PATH,
        "down": MIGRATION_DOWN_PATH,
        "prerequisites": [MIGRATION_EVIDENCE_INDEXES_UP_PATH],
    },
    {
        "version": "0012",
        "name": "control_plane_maintenance_fence",
        "up": MIGRATION_MAINTENANCE_FENCE_UP_PATH,
        "down": MIGRATION_MAINTENANCE_FENCE_DOWN_PATH,
        "prerequisites": [MIGRATION_UP_PATH],
    },
    {
        "version": "0013",
        "name": "four_account_rollout",
        "up": MIGRATION_FOUR_ACCOUNT_ROLLOUT_UP_PATH,
        "down": MIGRATION_FOUR_ACCOUNT_ROLLOUT_DOWN_PATH,
        "prerequisites": [
            MIGRATION_UP_PATH,
            MIGRATION_MAINTENANCE_FENCE_UP_PATH,
        ],
    },
    {
        "version": "0014",
        "name": "cancel_order_contract",
        "up": MIGRATION_CANCEL_ORDER_CONTRACT_UP_PATH,
        "down": MIGRATION_CANCEL_ORDER_CONTRACT_DOWN_PATH,
        "prerequisites": [MIGRATION_FOUR_ACCOUNT_ROLLOUT_UP_PATH],
    },
    {
        "version": "0015",
        "name": "refresh_evidence_command",
        "up": MIGRATION_REFRESH_EVIDENCE_COMMAND_UP_PATH,
        "down": MIGRATION_REFRESH_EVIDENCE_COMMAND_DOWN_PATH,
        "prerequisites": [MIGRATION_CANCEL_ORDER_CONTRACT_UP_PATH],
    },
    {
        "version": "0016",
        "name": "control_plane_lock_privileges",
        "up": MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_UP_PATH,
        "down": MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_DOWN_PATH,
        "prerequisites": [MIGRATION_REFRESH_EVIDENCE_COMMAND_UP_PATH],
    },
    {
        "version": "0017",
        "name": "operator_query_projection_reads",
        "up": MIGRATION_OPERATOR_QUERY_PROJECTION_READS_UP_PATH,
        "down": MIGRATION_OPERATOR_QUERY_PROJECTION_READS_DOWN_PATH,
        "prerequisites": [MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_UP_PATH],
    },
    {
        "version": "0018",
        "name": "projection_reliability",
        "up": MIGRATION_PROJECTION_RELIABILITY_UP_PATH,
        "down": MIGRATION_PROJECTION_RELIABILITY_DOWN_PATH,
        "prerequisites": [
            MIGRATION_OPERATOR_QUERY_PROJECTION_READS_UP_PATH
        ],
    },
)
STRICT_V3_REQUIRED_FIELDS = {
    "build_attestation_sha256",
    "dependency_inventory_sha256",
    "migration_manifest_sha256",
    "release_payload",
    "release_source_manifest_sha256",
    "review_subject_sha256",
    "sha256sums_sha256",
    "systemd_resource_contract_sha256",
}
NODE_DOCKER_RESOURCE_ARTIFACT = (
    "infra/systemd/account-stall-account-node.conf"
)
REDIS_DOCKER_RESOURCE_ARTIFACT = (
    "infra/systemd/account-stall-redis.conf"
)
SYSTEMD_RESOURCE_BINDINGS = {
    NODE_DOCKER_RESOURCE_ARTIFACT: {
        "consumer_entrypoint": "hk-gen-recreate-patched.py",
        "owner_units": [
            "trader-v3-node-a",
            "trader-v3-node-b",
            "trader-v3-node-c",
            "trader-v3-node-d",
        ],
        "destinations": [
            "docker://trader-v3-node-a/HostConfig",
            "docker://trader-v3-node-b/HostConfig",
            "docker://trader-v3-node-c/HostConfig",
            "docker://trader-v3-node-d/HostConfig",
        ],
    },
    REDIS_DOCKER_RESOURCE_ARTIFACT: {
        "consumer_entrypoint": "hk-redis-rebaseline.sh",
        "owner_units": ["trader-v3-redis"],
        "destinations": ["docker://trader-v3-redis/HostConfig"],
    },
    "infra/systemd/account-stall-control-plane-reader.conf": {
        "consumer_entrypoint": "hk-control-plane-isolation.sh",
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
    "infra/systemd/account-stall-control-plane-writer.conf": {
        "consumer_entrypoint": "hk-control-plane-isolation.sh",
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
}
DOCKER_RESOURCE_FIELDS = {
    "memory_bytes",
    "memory_swap_bytes",
    "nano_cpus",
    "pids_limit",
    "nofile_soft",
    "nofile_hard",
    "restart_policy",
}
DEPENDENCY_INVENTORY_ALLOWED_UNLOCKED = (
    "pip",
    "setuptools",
    "wheel",
)
DEPENDENCY_TARGET_ENVIRONMENT = {
    "os_name": "posix",
    "platform_system": "Linux",
    "python_full_version": "3.12.0",
    "python_version": "3.12",
    "sys_platform": "linux",
}
CONTROL_PLANE_SESSION_RESOURCE_DEFAULTS = {
    "command_delivery_capacity": 128,
    "command_ack_capacity": 256,
    "intent_delivery_capacity": 256,
    "execution_event_capacity": 1024,
    "queue_degraded_ratio": 0.8,
    "retry_budget": 3,
    "retry_base_delay_seconds": 0.05,
    "retry_max_delay_seconds": 1.0,
    "retry_jitter_ratio": 0.2,
    "circuit_reset_seconds": 5.0,
    "operation_timeout_seconds": 15.0,
    "shutdown_timeout_seconds": 1.0,
}
STRATEGY_DURABLE_IO_RESOURCE_DEFAULTS = {
    "queue_capacity": 128,
    "task_timeout_seconds": 1.0,
    "shutdown_timeout_seconds": 2.0,
}
TERMINAL_EXCHANGE_RESOURCE_DEFAULTS = {
    "queue_capacity": 64,
    "result_queue_capacity": 128,
    "degraded_ratio": 0.8,
    "total_deadline_seconds": 6.0,
    "shutdown_timeout_seconds": 7.0,
}
REPORTER_WORKER_RESOURCE_DEFAULTS = {
    "denial_queue_capacity": 256,
    "protection_event_queue_capacity": 1024,
    "live_canary_risk_queue_capacity": 128,
    "incident_queue_capacity": 64,
    "task_timeout_seconds": 5.0,
    "shutdown_timeout_seconds": 5.0,
}
IMMUTABLE_CODE_ROOTS = (
    "/app",
    "/usr/local/lib/python3.12/site-packages/nautilus_trader",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
LOCK_ARTIFACT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PACKAGE_NAME_SEPARATOR_RE = re.compile(r"[-_.]+")
MARKER_ATOM_RE = re.compile(
    r"""^
    (?P<left>[a-z_]+)
    \s*
    (?P<operator>==|!=|<=|>=|<|>|not\s+in|in)
    \s*
    (?P<quote>['"])
    (?P<right>[^'"]+)
    (?P=quote)
    $""",
    re.VERBOSE,
)
IDENTITY_PATTERNS = (
    re.compile(r"trader[-_]?account[-_]?[abcd]", re.IGNORECASE),
    re.compile(r"instance[-_]?account[-_]?[abcd]", re.IGNORECASE),
    re.compile(r"account[-_]?[abcd]", re.IGNORECASE),
    re.compile(r"node[-_]?[abcd]", re.IGNORECASE),
)
ACCOUNT_NETWORK_CONTROL_PLANE_BASE_URLS = {
    "account-a": "http://172.30.1.1:8080",
    "account-b": "http://172.30.2.1:8080",
    "account-c": "http://172.30.3.1:8080",
    "account-d": "http://172.30.4.1:8080",
}
TRANSITION_RUNTIME_FILES = (
    (
        "execution_domain_init.py",
        "packages/execution-domain/execution_domain/__init__.py",
        "/app/execution_domain/__init__.py",
    ),
    (
        "account_execution_ledger.py",
        (
            "packages/execution-domain/execution_domain/"
            "account_execution_ledger.py"
        ),
        "/app/execution_domain/account_execution_ledger.py",
    ),
    (
        "health_server.py",
        "services/nautilus-node/app/health_server.py",
        "/app/app/health_server.py",
    ),
    (
        "run_node.py",
        "services/nautilus-node/app/run_node.py",
        "/app/app/run_node.py",
    ),
    (
        "health.py",
        "services/nautilus-node/runtime/health.py",
        "/app/runtime/health.py",
    ),
    (
        "bounded_task_worker.py",
        "services/nautilus-node/runtime/bounded_task_worker.py",
        "/app/runtime/bounded_task_worker.py",
    ),
    (
        "control_plane_session.py",
        "services/nautilus-node/runtime/control_plane_session.py",
        "/app/runtime/control_plane_session.py",
    ),
    (
        "intent_execution_inbox.py",
        "services/nautilus-node/runtime/intent_execution_inbox.py",
        "/app/runtime/intent_execution_inbox.py",
    ),
    (
        "redis_safety.py",
        "services/nautilus-node/runtime/redis_safety.py",
        "/app/runtime/redis_safety.py",
    ),
    (
        "reconciliation.py",
        "services/nautilus-node/runtime/reconciliation.py",
        "/app/runtime/reconciliation.py",
    ),
    (
        "nautilus_reconciliation_scope.py",
        "services/nautilus-node/runtime/nautilus_reconciliation_scope.py",
        "/app/runtime/nautilus_reconciliation_scope.py",
    ),
    (
        "live_canary_execution.py",
        "services/nautilus-node/runtime/live_canary_execution.py",
        "/app/runtime/live_canary_execution.py",
    ),
    (
        "node_config.py",
        "services/nautilus-node/config/node_config.py",
        "/app/config/node_config.py",
    ),
    (
        "risk_config.py",
        "services/nautilus-node/risk/config.py",
        "/app/risk/config.py",
    ),
    (
        "risk_init.py",
        "services/nautilus-node/risk/__init__.py",
        "/app/risk/__init__.py",
    ),
    (
        "projection_spool.py",
        "services/nautilus-node/projection/spool.py",
        "/app/projection/spool.py",
    ),
    (
        "approved_intent_client.py",
        "services/nautilus-node/data_client/approved_intent_client.py",
        "/app/data_client/approved_intent_client.py",
    ),
    (
        "nautilus_config.py",
        "services/nautilus-node/persistence/nautilus_config.py",
        "/app/persistence/nautilus_config.py",
    ),
    (
        "persistence_init.py",
        "services/nautilus-node/persistence/__init__.py",
        "/app/persistence/__init__.py",
    ),
    (
        "redis_namespace_lease.py",
        "services/nautilus-node/persistence/redis_namespace_lease.py",
        "/app/persistence/redis_namespace_lease.py",
    ),
    (
        "redis_resp_client.py",
        "services/nautilus-node/persistence/redis_resp_client.py",
        "/app/persistence/redis_resp_client.py",
    ),
)


class ReleaseManifestError(ValueError):
    pass


DEPENDENCY_INVENTORY_VERIFY_SCRIPT = r"""
import hashlib
import importlib.metadata
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
document = json.loads(path.read_text(encoding="utf-8"))
separator = re.compile(r"[-_.]+")

def canonical_name(value):
    return separator.sub("-", str(value).strip().lower())

expected = {
    canonical_name(item["name"]): str(item["version"])
    for item in document["packages"]
}
allowed = {
    canonical_name(name)
    for name in document["allowed_unlocked_packages"]
}
actual = {}
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name")
    if not name:
        raise SystemExit("installed distribution lacks Name metadata")
    canonical = canonical_name(name)
    version = str(distribution.version)
    previous = actual.get(canonical)
    if previous is not None and previous != version:
        raise SystemExit(
            f"duplicate installed distribution versions: {canonical}"
        )
    actual[canonical] = version
checked = {
    name: version
    for name, version in actual.items()
    if name not in allowed
}
if checked != expected:
    missing = sorted(set(expected) - set(checked))
    extra = sorted(set(checked) - set(expected))
    changed = sorted(
        name
        for name in set(expected) & set(checked)
        if expected[name] != checked[name]
    )
    raise SystemExit(
        "dependency inventory mismatch "
        + json.dumps(
            {
                "missing": missing,
                "extra": extra,
                "changed": changed,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
print(hashlib.sha256(path.read_bytes()).hexdigest())
""".strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return text.encode("utf-8")


def canonical_package_name(value: str) -> str:
    normalized = PACKAGE_NAME_SEPARATOR_RE.sub(
        "-",
        str(value).strip().lower(),
    )
    if not normalized or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized) is None:
        raise ReleaseManifestError(
            f"dependency package name is invalid: {value!r}"
        )
    return normalized


def _marker_operand(value: str) -> tuple[int, ...] | str:
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
        return tuple(int(part) for part in value.split("."))
    return value


def _marker_atom_applies(atom: str) -> bool:
    match = MARKER_ATOM_RE.fullmatch(atom.strip())
    if match is None:
        raise ReleaseManifestError(
            f"dependency marker is unsupported: {atom}"
        )
    left_name = match.group("left")
    if left_name not in DEPENDENCY_TARGET_ENVIRONMENT:
        raise ReleaseManifestError(
            f"dependency marker variable is unsupported: {left_name}"
        )
    left = _marker_operand(
        DEPENDENCY_TARGET_ENVIRONMENT[left_name]
    )
    right = _marker_operand(match.group("right"))
    operator = re.sub(r"\s+", " ", match.group("operator"))
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    if operator == "in":
        return str(left) in str(right)
    if operator == "not in":
        return str(left) not in str(right)
    if type(left) is not type(right):
        raise ReleaseManifestError(
            f"dependency marker operands are incompatible: {atom}"
        )
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    raise ReleaseManifestError(
        f"dependency marker operator is unsupported: {operator}"
    )


def _marker_applies(marker: Any) -> bool:
    if marker is None:
        return True
    if not isinstance(marker, str) or not marker.strip():
        raise ReleaseManifestError(
            "dependency marker must be a non-empty string"
        )
    expression = marker.strip()
    if "(" in expression or ")" in expression:
        raise ReleaseManifestError(
            f"dependency marker grouping is unsupported: {marker}"
        )
    for disjunction in re.split(r"\s+or\s+", expression):
        atoms = re.split(r"\s+and\s+", disjunction)
        if all(_marker_atom_applies(atom) for atom in atoms):
            return True
    return False


def _validated_lock_artifacts(
    package: dict[str, Any],
    label: str,
) -> list[dict[str, Any]]:
    artifacts: list[tuple[str, dict[str, Any]]] = []
    sdist = package.get("sdist")
    if isinstance(sdist, dict):
        artifacts.append(("sdist", sdist))
    wheels = package.get("wheels")
    if isinstance(wheels, list):
        for wheel in wheels:
            if not isinstance(wheel, dict):
                raise ReleaseManifestError(
                    f"{label} distribution artifact is invalid"
                )
            artifacts.append(("wheel", wheel))
    if not artifacts:
        raise ReleaseManifestError(
            f"{label} lacks hashed distribution artifacts"
        )
    validated = []
    for kind, artifact in artifacts:
        artifact_hash = str(artifact.get("hash") or "").lower()
        if LOCK_ARTIFACT_HASH_RE.fullmatch(artifact_hash) is None:
            raise ReleaseManifestError(
                f"{label} distribution artifact hash is invalid"
            )
        if artifact_hash == f"sha256:{'0' * 64}":
            raise ReleaseManifestError(
                f"{label} distribution artifact hash cannot be zero"
            )
        artifact_url = str(artifact.get("url") or "").strip()
        if not artifact_url.startswith("https://"):
            raise ReleaseManifestError(
                f"{label} distribution artifact URL is invalid"
            )
        size = artifact.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ReleaseManifestError(
                f"{label} distribution artifact size is invalid"
            )
        validated.append(
            {
                "kind": kind,
                "url": artifact_url,
                "hash": artifact_hash,
                "size": size,
            }
        )
    return sorted(
        validated,
        key=lambda item: (
            item["kind"],
            item["url"],
            item["hash"],
        ),
    )


def build_dependency_inventory(path: Path) -> dict[str, Any]:
    if path.name != RELEASE_DEPENDENCY_LOCK_NAME:
        raise ReleaseManifestError(
            "dependency lock release path must equal "
            f"{RELEASE_DEPENDENCY_LOCK_NAME}"
        )
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseManifestError(
            f"dependency lock is invalid TOML: {path}"
        ) from exc
    if document.get("version") != 1:
        raise ReleaseManifestError("dependency lock version must equal 1")
    revision = document.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ReleaseManifestError(
            "dependency lock revision must be a positive integer"
        )
    if revision <= 0:
        raise ReleaseManifestError(
            "dependency lock revision must be a positive integer"
        )
    requires_python = document.get("requires-python")
    if requires_python != "==3.12.*":
        raise ReleaseManifestError(
            "dependency lock requires-python must equal ==3.12.*"
        )
    raw_packages = document.get("package")
    if not isinstance(raw_packages, list) or not raw_packages:
        raise ReleaseManifestError(
            "dependency lock packages must be non-empty"
        )

    packages: dict[str, dict[str, Any]] = {}
    virtual_roots = []
    for raw_package in raw_packages:
        if not isinstance(raw_package, dict):
            raise ReleaseManifestError(
                "dependency lock package entry must be an object"
            )
        name = canonical_package_name(str(raw_package.get("name") or ""))
        version = str(raw_package.get("version") or "").strip()
        if not version:
            raise ReleaseManifestError(
                f"dependency lock package version is missing: {name}"
            )
        if name in packages:
            raise ReleaseManifestError(
                f"dependency lock package is duplicated: {name}"
            )
        source = raw_package.get("source")
        if not isinstance(source, dict):
            raise ReleaseManifestError(
                f"dependency lock package source is invalid: {name}"
            )
        if "registry" in source:
            registry = source.get("registry")
            if not isinstance(registry, str) or not registry.startswith(
                "https://"
            ):
                raise ReleaseManifestError(
                    f"dependency registry source is invalid: {name}"
                )
            artifacts = _validated_lock_artifacts(
                raw_package,
                f"dependency package {name}",
            )
        elif source.get("virtual") == ".":
            virtual_roots.append(name)
            registry = False
            artifacts = []
        else:
            raise ReleaseManifestError(
                f"dependency source type is unsupported: {name}"
            )
        packages[name] = {
            "name": name,
            "version": version,
            "source": source,
            "registry": registry,
            "artifacts": artifacts,
            "dependencies": raw_package.get("dependencies", []),
        }
    if virtual_roots != ["nautilus-node-runtime"]:
        raise ReleaseManifestError(
            "dependency lock must contain one nautilus-node-runtime root"
        )

    for package in packages.values():
        dependencies = package["dependencies"]
        if not isinstance(dependencies, list):
            raise ReleaseManifestError(
                f"dependency list is invalid: {package['name']}"
            )
        for dependency in dependencies:
            if not isinstance(dependency, dict):
                raise ReleaseManifestError(
                    f"dependency edge is invalid: {package['name']}"
                )
            dependency_name = canonical_package_name(
                str(dependency.get("name") or "")
            )
            if dependency_name not in packages:
                raise ReleaseManifestError(
                    "dependency lock references an unknown package: "
                    f"{dependency_name}"
                )
    all_reachable = set()
    all_pending = list(virtual_roots)
    while all_pending:
        name = all_pending.pop()
        if name in all_reachable:
            continue
        all_reachable.add(name)
        package = packages[name]
        for dependency in package["dependencies"]:
            all_pending.append(
                canonical_package_name(
                    str(dependency.get("name") or "")
                )
            )
    if all_reachable != set(packages):
        unused = sorted(set(packages) - all_reachable)
        raise ReleaseManifestError(
            f"dependency lock contains unreachable packages: {unused}"
        )

    required = set()
    pending = list(virtual_roots)
    visited = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        package = packages[name]
        source = package["source"]
        if "registry" in source:
            required.add(name)
        for dependency in package["dependencies"]:
            if _marker_applies(dependency.get("marker")):
                pending.append(
                    canonical_package_name(
                        str(dependency.get("name") or "")
                    )
                )
    inventory_packages = [
        {
            "name": name,
            "version": packages[name]["version"],
            "registry": packages[name]["registry"],
            "artifacts": packages[name]["artifacts"],
        }
        for name in sorted(required)
    ]
    if not inventory_packages:
        raise ReleaseManifestError(
            "dependency lock resolves to an empty package inventory"
        )
    return {
        "schema_version": DEPENDENCY_INVENTORY_SCHEMA_VERSION,
        "lock_version": 1,
        "lock_revision": revision,
        "lock_path": RELEASE_DEPENDENCY_LOCK_NAME,
        "lock_sha256": sha256_file(path),
        "requires_python": requires_python,
        "target_environment": dict(DEPENDENCY_TARGET_ENVIRONMENT),
        "allowed_unlocked_packages": list(
            DEPENDENCY_INVENTORY_ALLOWED_UNLOCKED
        ),
        "packages": inventory_packages,
    }


def dependency_inventory_sha256(value: dict[str, Any]) -> str:
    payload = canonical_json_bytes(value) + b"\n"
    return hashlib.sha256(payload).hexdigest()


def write_dependency_inventory(lock_path: Path, output_path: Path) -> str:
    inventory = build_dependency_inventory(lock_path)
    payload = canonical_json_bytes(inventory) + b"\n"
    output_path.write_bytes(payload)
    return dependency_inventory_sha256(inventory)


def dependency_inventory_verifier_command(path: str) -> list[str]:
    return [
        "python3",
        "-c",
        DEPENDENCY_INVENTORY_VERIFY_SCRIPT,
        path,
    ]


def validate_migration_manifest(
    path: Path,
    *,
    payload_root: Path | None = None,
) -> dict[str, Any]:
    document = _load_json(path)
    if document.get("schema_version") != MIGRATION_MANIFEST_SCHEMA_VERSION:
        raise ReleaseManifestError(
            "migration manifest schema version mismatch"
        )
    if document.get("schema_epoch") != SCHEMA_EPOCHS["db"]:
        raise ReleaseManifestError(
            "migration manifest schema epoch mismatch"
        )
    runner = document.get("runner")
    if not isinstance(runner, dict):
        raise ReleaseManifestError(
            "migration manifest runner must be an object"
        )
    runner_path = _validated_release_relative_path(
        str(runner.get("path") or ""),
        label="migration runner path",
        required_prefix="services/control-plane/db/",
    )
    if runner_path != MIGRATION_RUNNER_PATH:
        raise ReleaseManifestError(
            "migration runner must equal canonical runner"
        )
    runner_sha256 = _require_sha256(
        str(runner.get("sha256") or ""),
        "migration runner sha256",
    )
    migrations = document.get("migrations")
    if not isinstance(migrations, list) or not migrations:
        raise ReleaseManifestError(
            "migration manifest migrations must be non-empty"
        )
    validated_migrations = []
    migration_paths = set()
    for raw in migrations:
        if not isinstance(raw, dict):
            raise ReleaseManifestError(
                "migration manifest entry must be an object"
            )
        migration_path = _validated_release_relative_path(
            str(raw.get("path") or ""),
            label="migration path",
            required_prefix="db/migrations/",
        )
        if migration_path in migration_paths:
            raise ReleaseManifestError(
                f"migration path is duplicated: {migration_path}"
            )
        migration_paths.add(migration_path)
        validated_migrations.append(
            {
                "path": migration_path,
                "sha256": _require_sha256(
                    str(raw.get("sha256") or ""),
                    "migration sha256",
                ),
            }
        )
    ordered_migration_paths = [
        item["path"]
        for item in validated_migrations
    ]
    if ordered_migration_paths != list(CANONICAL_MIGRATION_PATHS):
        raise ReleaseManifestError(
            "migration manifest migrations must equal canonical exact-set"
        )
    up = str(document.get("up") or "")
    down = str(document.get("down") or "")
    if up != MIGRATION_UP_PATH:
        raise ReleaseManifestError(
            "migration up path must equal canonical live-safety migration"
        )
    if down != MIGRATION_DOWN_PATH:
        raise ReleaseManifestError(
            "migration down path must equal canonical live-safety migration"
        )
    prerequisites = document.get("prerequisites")
    if not isinstance(prerequisites, list):
        raise ReleaseManifestError(
            "migration manifest prerequisites must be a list"
        )
    prerequisite_paths = [str(item) for item in prerequisites]
    if prerequisite_paths != list(MIGRATION_PREREQUISITE_PATHS):
        raise ReleaseManifestError(
            "migration prerequisites must equal canonical exact-set"
        )
    steps = _validated_migration_steps(document.get("steps"))
    required_paths = {
        up,
        down,
        MIGRATION_EVIDENCE_INDEXES_UP_PATH,
        MIGRATION_EVIDENCE_INDEXES_DOWN_PATH,
        MIGRATION_MAINTENANCE_FENCE_UP_PATH,
        MIGRATION_MAINTENANCE_FENCE_DOWN_PATH,
        MIGRATION_FOUR_ACCOUNT_ROLLOUT_UP_PATH,
        MIGRATION_FOUR_ACCOUNT_ROLLOUT_DOWN_PATH,
        MIGRATION_CANCEL_ORDER_CONTRACT_UP_PATH,
        MIGRATION_CANCEL_ORDER_CONTRACT_DOWN_PATH,
        MIGRATION_REFRESH_EVIDENCE_COMMAND_UP_PATH,
        MIGRATION_REFRESH_EVIDENCE_COMMAND_DOWN_PATH,
        MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_UP_PATH,
        MIGRATION_CONTROL_PLANE_LOCK_PRIVILEGES_DOWN_PATH,
        MIGRATION_OPERATOR_QUERY_PROJECTION_READS_UP_PATH,
        MIGRATION_OPERATOR_QUERY_PROJECTION_READS_DOWN_PATH,
        MIGRATION_PROJECTION_RELIABILITY_UP_PATH,
        MIGRATION_PROJECTION_RELIABILITY_DOWN_PATH,
        *prerequisite_paths,
    }
    if not required_paths.issubset(migration_paths):
        raise ReleaseManifestError(
            "migration manifest apply paths are incomplete"
        )
    normalized = {
        "schema_version": MIGRATION_MANIFEST_SCHEMA_VERSION,
        "schema_epoch": SCHEMA_EPOCHS["db"],
        "runner": {
            "path": runner_path,
            "sha256": runner_sha256,
        },
        "up": up,
        "down": down,
        "prerequisites": prerequisite_paths,
        "steps": steps,
        "migrations": validated_migrations,
    }
    if payload_root is not None:
        runner_file = payload_root / runner_path
        if not runner_file.is_file():
            raise ReleaseManifestError(
                f"migration runner payload is missing: {runner_path}"
            )
        if sha256_file(runner_file) != runner_sha256:
            raise ReleaseManifestError(
                "migration runner payload hash mismatch"
            )
        for item in validated_migrations:
            migration_file = payload_root / item["path"]
            if not migration_file.is_file():
                raise ReleaseManifestError(
                    "migration payload is missing: "
                    f"{item['path']}"
                )
            if sha256_file(migration_file) != item["sha256"]:
                raise ReleaseManifestError(
                    "migration payload hash mismatch: "
                    f"{item['path']}"
                )
    return normalized


def _validated_migration_steps(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ReleaseManifestError(
            "migration manifest steps must be a list"
        )
    expected = [dict(item) for item in CANONICAL_MIGRATION_STEPS]
    if raw != expected:
        raise ReleaseManifestError(
            "migration manifest steps must equal canonical mapping"
        )
    return expected


def _validated_release_relative_path(
    value: str,
    *,
    label: str,
    required_prefix: str,
) -> str:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != value
        or not value.startswith(required_prefix)
    ):
        raise ReleaseManifestError(f"{label} is invalid: {value}")
    return value


def _normalize_identity_string(value: str) -> str:
    normalized = value
    for pattern in IDENTITY_PATTERNS:
        normalized = pattern.sub("<node-identity>", normalized)
    return normalized


def normalize_node_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: normalize_node_config(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [normalize_node_config(item) for item in value]
    if isinstance(value, str):
        return _normalize_identity_string(value)
    return value


def node_config_sha256(path: Path) -> str:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError(
            f"cannot read node config as JSON: {path}"
        ) from exc
    return node_config_value_sha256(config)


def node_config_value_sha256(config: dict[str, Any]) -> str:
    normalized = normalize_node_config(config)
    account_id = config.get("account_id")
    expected_base_url = ACCOUNT_NETWORK_CONTROL_PLANE_BASE_URLS.get(
        account_id
    )
    control_plane = config.get("control_plane")
    normalized_control_plane = normalized.get("control_plane")
    if (
        isinstance(control_plane, dict)
        and isinstance(normalized_control_plane, dict)
        and control_plane.get("base_url") == expected_base_url
    ):
        normalized_control_plane["base_url"] = (
            "<account-network-control-plane>"
        )
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def node_config_contract_sha256(
    node_configs: list[dict[str, Any]],
) -> str:
    accounts = [
        {
            "account_id": str(entry["account_id"]),
            "normalized_sha256": _require_sha256(
                str(entry["normalized_sha256"]),
                "node config normalized_sha256",
            ),
        }
        for entry in sorted(
            node_configs,
            key=lambda item: str(item["account_id"]),
        )
    ]
    contract = {
        "schema_version": NODE_CONFIG_CONTRACT_SCHEMA_VERSION,
        "accounts": accounts,
    }
    return hashlib.sha256(canonical_json_bytes(contract)).hexdigest()


def _release_config_sha256(
    node_configs: list[dict[str, Any]],
    *,
    schema_version: str,
) -> str:
    normalized_hashes = {
        item["normalized_sha256"]
        for item in node_configs
    }
    if schema_version == PREVIOUS_SCHEMA_VERSION:
        if len(normalized_hashes) != 1:
            raise ReleaseManifestError(
                "release schema v3 requires one shared node normalized "
                "config hash"
            )
        return next(iter(normalized_hashes))
    if schema_version == SCHEMA_VERSION:
        return node_config_contract_sha256(node_configs)
    raise ReleaseManifestError(
        "node config contract requires a supported release schema"
    )


def _validated_runtime_resources(
    raw: Any,
    *,
    label: str,
    legacy_control_plane_session: Any = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ReleaseManifestError(f"{label} must be an object")
    if raw.get("schema_version") != RUNTIME_RESOURCES_SCHEMA_VERSION:
        raise ReleaseManifestError(f"{label} schema version mismatch")
    allowed_fields = {
        "schema_version",
        "redis",
        "command_journal",
        "control_plane_session",
        "strategy_durable_io",
        "terminal_exchange",
        "reporter_workers",
    }
    required_fields = set(allowed_fields)
    if not required_fields.issubset(raw) or not set(raw).issubset(
        allowed_fields
    ):
        raise ReleaseManifestError(f"{label} fields mismatch")
    redis = raw.get("redis")
    command_journal = raw.get("command_journal")
    if not isinstance(redis, dict):
        raise ReleaseManifestError(f"{label}.redis must be an object")
    if not isinstance(command_journal, dict):
        raise ReleaseManifestError(
            f"{label}.command_journal must be an object"
        )
    expected_redis_fields = {
        "stream_max_entries",
        "stream_max_bytes",
        "total_stream_max_bytes",
        "scan_count",
        "sample_interval_seconds",
        "critical_window_seconds",
        "thread_join_timeout_seconds",
        "memory_warning_ratio",
        "memory_degraded_ratio",
        "memory_critical_ratio",
    }
    if set(redis) != expected_redis_fields:
        raise ReleaseManifestError(f"{label}.redis fields mismatch")
    if set(command_journal) != {"max_bytes"}:
        raise ReleaseManifestError(
            f"{label}.command_journal fields mismatch"
        )

    positive_integers = (
        "stream_max_entries",
        "stream_max_bytes",
        "total_stream_max_bytes",
        "scan_count",
    )
    validated_redis: dict[str, int | float] = {}
    for field in positive_integers:
        value = redis.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ReleaseManifestError(
                f"{label}.redis.{field} must be a positive integer"
            )
        validated_redis[field] = value
    positive_numbers = (
        "sample_interval_seconds",
        "critical_window_seconds",
        "thread_join_timeout_seconds",
    )
    for field in positive_numbers:
        value = redis.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ReleaseManifestError(
                f"{label}.redis.{field} must be a positive number"
            )
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ReleaseManifestError(
                f"{label}.redis.{field} must be a positive number"
            )
        validated_redis[field] = number
    ratio_fields = (
        "memory_warning_ratio",
        "memory_degraded_ratio",
        "memory_critical_ratio",
    )
    ratios = []
    for field in ratio_fields:
        value = redis.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ReleaseManifestError(
                f"{label}.redis.{field} must be a ratio"
            )
        ratio = float(value)
        if not math.isfinite(ratio) or ratio <= 0 or ratio >= 1:
            raise ReleaseManifestError(
                f"{label}.redis.{field} must be between zero and one"
            )
        validated_redis[field] = ratio
        ratios.append(ratio)
    if not ratios[0] < ratios[1] < ratios[2]:
        raise ReleaseManifestError(
            f"{label}.redis memory ratios must increase"
        )
    if (
        validated_redis["total_stream_max_bytes"]
        < validated_redis["stream_max_bytes"]
    ):
        raise ReleaseManifestError(
            f"{label}.redis total stream bytes must cover one stream"
        )
    command_journal_max_bytes = command_journal.get("max_bytes")
    if (
        isinstance(command_journal_max_bytes, bool)
        or not isinstance(command_journal_max_bytes, int)
        or command_journal_max_bytes <= 0
    ):
        raise ReleaseManifestError(
            f"{label}.command_journal.max_bytes must be a positive integer"
        )
    control_plane_session = _validated_control_plane_session_resources(
        raw.get("control_plane_session"),
        label=f"{label}.control_plane_session",
    )
    if legacy_control_plane_session is not None:
        legacy_session = _validated_control_plane_session_resources(
            legacy_control_plane_session,
            label="control_plane.session",
        )
        overlapping_fields = set(
            CONTROL_PLANE_SESSION_RESOURCE_DEFAULTS
        )
        for field in overlapping_fields:
            if control_plane_session[field] != legacy_session[field]:
                raise ReleaseManifestError(
                    f"{label}.control_plane_session differs from "
                    f"control_plane.session at {field}"
                )
    strategy_durable_io = _validated_resource_group(
        raw.get("strategy_durable_io"),
        defaults=STRATEGY_DURABLE_IO_RESOURCE_DEFAULTS,
        positive_integer_fields={"queue_capacity"},
        positive_number_fields={
            "task_timeout_seconds",
            "shutdown_timeout_seconds",
        },
        ratio_fields=set(),
        label=f"{label}.strategy_durable_io",
    )
    terminal_exchange = _validated_resource_group(
        raw.get("terminal_exchange"),
        defaults=TERMINAL_EXCHANGE_RESOURCE_DEFAULTS,
        positive_integer_fields={
            "queue_capacity",
            "result_queue_capacity",
        },
        positive_number_fields={
            "total_deadline_seconds",
            "shutdown_timeout_seconds",
        },
        ratio_fields={"degraded_ratio"},
        label=f"{label}.terminal_exchange",
    )
    reporter_workers = _validated_resource_group(
        raw.get("reporter_workers"),
        defaults=REPORTER_WORKER_RESOURCE_DEFAULTS,
        positive_integer_fields={
            "denial_queue_capacity",
            "protection_event_queue_capacity",
            "live_canary_risk_queue_capacity",
            "incident_queue_capacity",
        },
        positive_number_fields={
            "task_timeout_seconds",
            "shutdown_timeout_seconds",
        },
        ratio_fields=set(),
        label=f"{label}.reporter_workers",
    )
    return {
        "schema_version": RUNTIME_RESOURCES_SCHEMA_VERSION,
        "redis": validated_redis,
        "command_journal": {
            "max_bytes": command_journal_max_bytes,
        },
        "control_plane_session": control_plane_session,
        "strategy_durable_io": strategy_durable_io,
        "terminal_exchange": terminal_exchange,
        "reporter_workers": reporter_workers,
    }


def _validated_control_plane_session_resources(
    raw: Any,
    *,
    label: str,
) -> dict[str, Any]:
    session = _validated_resource_group(
        raw,
        defaults=CONTROL_PLANE_SESSION_RESOURCE_DEFAULTS,
        positive_integer_fields={
            "command_delivery_capacity",
            "command_ack_capacity",
            "intent_delivery_capacity",
            "execution_event_capacity",
            "retry_budget",
        },
        positive_number_fields={
            "retry_base_delay_seconds",
            "retry_max_delay_seconds",
            "circuit_reset_seconds",
            "operation_timeout_seconds",
            "shutdown_timeout_seconds",
        },
        ratio_fields={
            "queue_degraded_ratio",
        },
        zero_ratio_fields={
            "retry_jitter_ratio",
        },
        label=label,
    )
    if (
        session["retry_max_delay_seconds"]
        < session["retry_base_delay_seconds"]
    ):
        raise ReleaseManifestError(
            f"{label}.retry_max_delay_seconds must be at least "
            "retry_base_delay_seconds"
        )
    return session


def _validated_resource_group(
    raw: Any,
    *,
    defaults: dict[str, int | float],
    positive_integer_fields: set[str],
    positive_number_fields: set[str],
    ratio_fields: set[str],
    label: str,
    zero_ratio_fields: set[str] | None = None,
) -> dict[str, Any]:
    if isinstance(raw, dict):
        source = raw
    else:
        raise ReleaseManifestError(f"{label} must be an object")
    if set(source) != set(defaults):
        raise ReleaseManifestError(f"{label} fields mismatch")
    validated: dict[str, Any] = {}
    for field, default in defaults.items():
        value = source[field]
        if field in positive_integer_fields:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ReleaseManifestError(
                    f"{label}.{field} must be a positive integer"
                )
            validated[field] = value
            continue
        if field in positive_number_fields:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
            ):
                raise ReleaseManifestError(
                    f"{label}.{field} must be a positive number"
                )
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                raise ReleaseManifestError(
                    f"{label}.{field} must be a positive number"
                )
            validated[field] = number
            continue
        allow_zero = zero_ratio_fields is not None and field in zero_ratio_fields
        if field in ratio_fields or allow_zero:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
            ):
                raise ReleaseManifestError(
                    f"{label}.{field} must be a ratio"
                )
            ratio = float(value)
            minimum_valid = ratio > 0
            if allow_zero:
                minimum_valid = ratio >= 0
            if (
                not math.isfinite(ratio)
                or not minimum_valid
                or ratio >= 1
            ):
                raise ReleaseManifestError(
                    f"{label}.{field} must be between zero and one"
                )
            validated[field] = ratio
            continue
        raise ReleaseManifestError(
            f"{label}.{field} has no validation contract"
        )
    return validated


def runtime_resources_sha256(
    value: dict[str, Any],
    *,
    legacy_control_plane_session: Any = None,
) -> str:
    validated = _validated_runtime_resources(
        value,
        label="runtime_resources",
        legacy_control_plane_session=legacy_control_plane_session,
    )
    return hashlib.sha256(canonical_json_bytes(validated)).hexdigest()


def _read_node_config(path: Path) -> tuple[bytes, dict[str, Any], os.stat_result]:
    flags = os.O_RDONLY
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    flags |= no_follow
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReleaseManifestError(
            f"cannot open node config artifact: {path}"
        ) from exc
    try:
        file_stat = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            payload = handle.read()
        config = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ReleaseManifestError(
            f"cannot read node config artifact: {path}"
        ) from exc
    finally:
        os.close(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        raise ReleaseManifestError(
            f"node config artifact must be regular: {path}"
        )
    if stat.S_IMODE(file_stat.st_mode) & 0o222:
        raise ReleaseManifestError(
            f"node config artifact must be read-only: {path}"
        )
    if path.is_symlink():
        raise ReleaseManifestError(
            f"node config artifact cannot be a symlink: {path}"
        )
    if not isinstance(config, dict):
        raise ReleaseManifestError(
            f"node config artifact root must be an object: {path}"
        )
    try:
        current_stat = path.stat()
    except OSError as exc:
        raise ReleaseManifestError(
            f"node config artifact path disappeared: {path}"
        ) from exc
    opened_inode = (file_stat.st_dev, file_stat.st_ino)
    current_inode = (current_stat.st_dev, current_stat.st_ino)
    if current_inode != opened_inode:
        raise ReleaseManifestError(
            f"node config artifact path changed while reading: {path}"
        )
    return payload, config, file_stat


def build_node_config_artifacts(
    specs: dict[str, tuple[str, Path]],
) -> list[dict[str, Any]]:
    if not specs or not set(specs).issubset(REQUIRED_ACCOUNTS):
        raise ReleaseManifestError(
            "config artifacts must be a non-empty supported account collection"
        )
    entries = []
    for account_id, raw_spec in sorted(specs.items()):
        container, raw_path = raw_spec
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", container):
            raise ReleaseManifestError(
                f"invalid config artifact container: {container}"
            )
        if container != EXPECTED_CONTAINERS[account_id]:
            raise ReleaseManifestError(
                f"config artifact container identity mismatch: {account_id}"
            )
        if raw_path.is_symlink():
            raise ReleaseManifestError(
                f"node config artifact cannot be a symlink: {raw_path}"
            )
        path = raw_path.resolve()
        payload, config, file_stat = _read_node_config(path)
        if config.get("account_id") != account_id:
            raise ReleaseManifestError(
                f"node config artifact account mismatch: {path}"
            )
        node_id = str(config.get("node_id") or "").strip()
        if not node_id:
            raise ReleaseManifestError(
                f"node config artifact node_id is missing: {path}"
            )
        control_plane = config.get("control_plane")
        legacy_session = None
        if isinstance(control_plane, dict):
            legacy_session = control_plane.get("session")
        runtime_resources = _validated_runtime_resources(
            config.get("runtime_resources"),
            label=f"{account_id} runtime_resources",
            legacy_control_plane_session=legacy_session,
        )
        resources_sha256 = runtime_resources_sha256(
            runtime_resources,
        )
        digest = hashlib.sha256(payload).hexdigest()
        if path.name != f"{digest}.json":
            raise ReleaseManifestError(
                f"node config artifact path is not content-addressed: {path}"
            )
        entries.append(
            {
                "account_id": account_id,
                "container": container,
                "node_id": node_id,
                "host_path": str(path),
                "mount_target": CONFIG_MOUNT_TARGET,
                "sha256": digest,
                "normalized_sha256": node_config_value_sha256(config),
                "runtime_resources": runtime_resources,
                "runtime_resources_sha256": resources_sha256,
                "size": file_stat.st_size,
            }
        )
    return _validated_node_configs(entries)


def _validated_node_configs(raw_entries: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ReleaseManifestError(
            "release manifest node_configs must contain at least one entry"
        )
    entries = []
    account_ids = set()
    containers = set()
    node_ids = set()
    host_paths = set()
    runtime_resource_hashes = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ReleaseManifestError(
                "release node config entry must be an object"
            )
        account_id = str(raw.get("account_id") or "")
        container = str(raw.get("container") or "")
        node_id = str(raw.get("node_id") or "").strip()
        host_path = str(raw.get("host_path") or "")
        mount_target = str(raw.get("mount_target") or "")
        digest = _require_sha256(
            str(raw.get("sha256") or ""),
            "node config sha256",
        )
        normalized_digest = _require_sha256(
            str(raw.get("normalized_sha256") or ""),
            "node config normalized_sha256",
        )
        runtime_resources = _validated_runtime_resources(
            raw.get("runtime_resources"),
            label=f"{account_id} runtime_resources",
        )
        resources_digest = _require_sha256(
            str(raw.get("runtime_resources_sha256") or ""),
            "node config runtime_resources_sha256",
        )
        if resources_digest != runtime_resources_sha256(runtime_resources):
            raise ReleaseManifestError(
                "node config runtime_resources hash mismatch"
            )
        size = raw.get("size")
        if account_id not in REQUIRED_ACCOUNTS:
            raise ReleaseManifestError(
                f"release node config account is invalid: {account_id}"
            )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", container):
            raise ReleaseManifestError(
                f"release node config container is invalid: {container}"
            )
        if container != EXPECTED_CONTAINERS[account_id]:
            raise ReleaseManifestError(
                f"release node config container identity mismatch: {account_id}"
            )
        if not node_id or len(node_id) > 255:
            raise ReleaseManifestError(
                f"release node config node_id is invalid: {node_id}"
            )
        if not Path(host_path).is_absolute():
            raise ReleaseManifestError(
                "release node config host_path must be absolute"
            )
        if Path(host_path).name != f"{digest}.json":
            raise ReleaseManifestError(
                "release node config host_path must be content-addressed"
            )
        if Path(host_path).parent.name != account_id:
            raise ReleaseManifestError(
                "release node config host_path must be account-scoped"
            )
        if mount_target != CONFIG_MOUNT_TARGET:
            raise ReleaseManifestError(
                "release node config mount_target must be /cfg.json"
            )
        if not isinstance(size, int) or size <= 0:
            raise ReleaseManifestError(
                "release node config size must be positive"
            )
        if account_id in account_ids:
            raise ReleaseManifestError(
                f"duplicate release node config account: {account_id}"
            )
        if container in containers:
            raise ReleaseManifestError(
                f"duplicate release node config container: {container}"
            )
        if node_id in node_ids:
            raise ReleaseManifestError(
                f"duplicate release node config node_id: {node_id}"
            )
        if host_path in host_paths:
            raise ReleaseManifestError(
                f"duplicate release node config host_path: {host_path}"
            )
        account_ids.add(account_id)
        containers.add(container)
        node_ids.add(node_id)
        host_paths.add(host_path)
        runtime_resource_hashes.add(resources_digest)
        entries.append(
            {
                "account_id": account_id,
                "container": container,
                "node_id": node_id,
                "host_path": host_path,
                "mount_target": CONFIG_MOUNT_TARGET,
                "sha256": digest,
                "normalized_sha256": normalized_digest,
                "runtime_resources": runtime_resources,
                "runtime_resources_sha256": resources_digest,
                "size": size,
            }
        )
    if not account_ids.issubset(REQUIRED_ACCOUNTS):
        raise ReleaseManifestError("release node config accounts are invalid")
    if len(runtime_resource_hashes) != 1:
        raise ReleaseManifestError(
            "node runtime resource config hashes differ"
        )
    return sorted(entries, key=lambda item: item["account_id"])


def _require_sha256(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if not SHA256_RE.fullmatch(normalized):
        raise ReleaseManifestError(f"{field} must be a lowercase sha256")
    return normalized


def _require_image_digest(value: str) -> str:
    normalized = value.strip().lower()
    if not IMAGE_DIGEST_RE.fullmatch(normalized):
        raise ReleaseManifestError(
            "image_digest must be a sha256 content digest"
        )
    return normalized


def _pinned_local_base_reference(image_digest: str) -> str:
    normalized = _require_image_digest(image_digest)
    digest_hex = normalized.split(":", 1)[1]
    return f"trader-bot/immutable-base:{digest_hex}"


def _docker_image_id(image: str) -> str:
    try:
        output = subprocess.check_output(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                image,
            ],
            text=True,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseManifestError(
            f"local Docker image is unavailable: {image}"
        ) from exc
    try:
        return _require_image_digest(output.strip())
    except ReleaseManifestError as exc:
        raise ReleaseManifestError(
            "docker returned an invalid image ID"
        ) from exc


def _prepare_pinned_local_base(
    image_digest: str,
    *,
    # image_id_resolver is a test seam; production callers must pass
    # the real _docker_image_id (or omit it) so identity checks stay strict.
    image_id_resolver: Callable[[str], str] | None = None,
) -> str:
    normalized = _require_image_digest(image_digest)
    reference = _pinned_local_base_reference(normalized)
    try:
        subprocess.run(
            [
                "docker",
                "image",
                "tag",
                normalized,
                reference,
            ],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseManifestError(
            "cannot create pinned local base image reference"
        ) from exc
    resolver = image_id_resolver
    if resolver is None:
        resolver = _docker_image_id
    if resolver(reference) != normalized:
        raise ReleaseManifestError(
            "pinned local base image reference identity mismatch"
        )
    return reference


def _docker_image_layers(image: str) -> list[str]:
    try:
        output = subprocess.check_output(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .RootFS.Layers}}",
                image,
            ],
            text=True,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseManifestError(
            f"cannot inspect Docker image layers: {image}"
        ) from exc
    try:
        raw_layers = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ReleaseManifestError(
            "docker returned invalid image layers"
        ) from exc
    if not isinstance(raw_layers, list):
        raise ReleaseManifestError(
            "docker returned invalid image layers"
        )
    layers = []
    for raw_layer in raw_layers:
        if not isinstance(raw_layer, str):
            raise ReleaseManifestError(
                "docker returned invalid image layers"
            )
        try:
            layer = _require_image_digest(raw_layer)
        except ReleaseManifestError as exc:
            raise ReleaseManifestError(
                "docker returned invalid image layers"
            ) from exc
        layers.append(layer)
    return layers


def _require_strict_image_layer_prefix(
    base_layers: list[str],
    built_layers: list[str],
) -> None:
    if len(built_layers) <= len(base_layers):
        raise ReleaseManifestError(
            "built image must add at least one layer to the base image"
        )
    if built_layers[: len(base_layers)] != base_layers:
        raise ReleaseManifestError(
            "built image base layer prefix mismatch"
        )


def _require_delivery_mode(value: str) -> str:
    normalized = value.strip()
    if normalized not in DELIVERY_MODES:
        raise ReleaseManifestError(
            f"delivery_mode must be one of: {', '.join(DELIVERY_MODES)}"
        )
    return normalized


def _require_release_purpose(value: str) -> str:
    normalized = value.strip()
    if normalized not in RELEASE_PURPOSES:
        raise ReleaseManifestError(
            f"release_purpose must be one of: {', '.join(RELEASE_PURPOSES)}"
        )
    return normalized


def _validate_release_topology(
    delivery_mode: str,
    release_purpose: str,
) -> None:
    if release_purpose == RELEASE_PURPOSE_HARDENING:
        if delivery_mode != DELIVERY_IMMUTABLE:
            raise ReleaseManifestError(
                "account-stall hardening requires immutable_image"
            )
        return
    if delivery_mode != DELIVERY_TRANSITION:
        raise ReleaseManifestError(
            "emergency rollback requires transition_bind_mount"
        )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReleaseManifestError(f"JSON root must be an object: {path}")
    return value


def _validated_payload_relative_path(
    value: str,
    *,
    label: str,
) -> str:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise ReleaseManifestError(f"{label} is invalid: {value}")
    return value


def parse_sha256sums(path: Path) -> list[dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseManifestError(
            f"cannot read release SHA256SUMS: {path}"
        ) from exc
    entries = []
    seen = set()
    for line in lines:
        digest, separator, raw_relative = line.partition("  ")
        relative = raw_relative.removeprefix("*").removeprefix("./")
        digest = _require_sha256(digest, "SHA256SUMS sha256")
        relative = _validated_payload_relative_path(
            relative,
            label="SHA256SUMS path",
        )
        if not separator or relative in seen:
            raise ReleaseManifestError(
                "release SHA256SUMS contains an invalid or duplicate entry"
            )
        if relative == SHA256SUMS_NAME:
            raise ReleaseManifestError(
                "release SHA256SUMS cannot include itself"
            )
        seen.add(relative)
        entries.append(
            {
                "path": relative,
                "sha256": digest,
            }
        )
    if not entries:
        raise ReleaseManifestError("release SHA256SUMS is empty")
    return sorted(entries, key=lambda item: item["path"])


def _validated_release_payload(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise ReleaseManifestError(
            "release_payload must be a non-empty list"
        )
    entries = []
    paths = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ReleaseManifestError(
                "release_payload entry fields mismatch"
            )
        relative = _validated_payload_relative_path(
            str(item.get("path") or ""),
            label="release payload path",
        )
        if relative in paths:
            raise ReleaseManifestError(
                f"release payload path is duplicated: {relative}"
            )
        paths.add(relative)
        entries.append(
            {
                "path": relative,
                "sha256": _require_sha256(
                    str(item.get("sha256") or ""),
                    "release payload sha256",
                ),
            }
        )
    return sorted(entries, key=lambda item: item["path"])


def _payload_hash_map(
    entries: list[dict[str, str]],
) -> dict[str, str]:
    return {
        item["path"]: item["sha256"]
        for item in entries
    }


def _verify_payload_hashes(
    payload_root: Path,
    entries: list[dict[str, str]],
) -> None:
    root = payload_root.resolve()
    for item in entries:
        path = (root / item["path"]).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ReleaseManifestError(
                f"release payload escapes root: {item['path']}"
            ) from exc
        if not path.is_file() or path.is_symlink():
            raise ReleaseManifestError(
                f"release payload is missing or unsafe: {item['path']}"
            )
        if sha256_file(path) != item["sha256"]:
            raise ReleaseManifestError(
                f"release payload hash mismatch: {item['path']}"
            )


def validate_release_source_manifest(
    path: Path,
    *,
    payload_root: Path,
) -> dict[str, Any]:
    document = _load_json(path)
    if document.get("schema_version") != "trader-v3-release-source/v1":
        raise ReleaseManifestError(
            "release source manifest schema version mismatch"
        )
    if document.get("source_mode") != "git_object":
        raise ReleaseManifestError(
            "release source manifest must be Git-object backed"
        )
    source_commit = str(document.get("source_commit") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ReleaseManifestError(
            "release source manifest commit is invalid"
        )
    source_tree = str(document.get("source_tree") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", source_tree) is None:
        raise ReleaseManifestError(
            "release source manifest tree is invalid"
        )
    _validated_schema_epochs(
        document,
        label="release source manifest",
    )
    files = document.get("files")
    if not isinstance(files, list) or not files:
        raise ReleaseManifestError(
            "release source manifest files must be non-empty"
        )
    release_files = []
    release_paths = set()
    for raw in files:
        if not isinstance(raw, dict):
            raise ReleaseManifestError(
                "release source manifest file entry is invalid"
            )
        release_path = _validated_payload_relative_path(
            str(raw.get("release_path") or ""),
            label="release source path",
        )
        if release_path in release_paths:
            raise ReleaseManifestError(
                f"release source path is duplicated: {release_path}"
            )
        release_paths.add(release_path)
        entry = {
            "release_path": release_path,
            "sha256": _require_sha256(
                str(raw.get("sha256") or ""),
                "release source file sha256",
            ),
        }
        # Rollout registration verifies live-adapter provenance against
        # these fields, so normalization must not strip them.
        for provenance_field in (
            "size",
            "source_git_blob",
            "source_git_mode",
            "source_path",
        ):
            if provenance_field in raw:
                entry[provenance_field] = raw[provenance_field]
        release_files.append(entry)
    bundle_sha256 = _require_sha256(
        str(document.get("bundle_manifest_sha256") or ""),
        "release source bundle manifest sha256",
    )
    migration = document.get("migration")
    if not isinstance(migration, dict):
        raise ReleaseManifestError(
            "release source migration metadata is invalid"
        )
    if migration.get("runner") != MIGRATION_RUNNER_PATH:
        raise ReleaseManifestError(
            "release source migration runner mismatch"
        )
    if migration.get("up") != MIGRATION_UP_PATH:
        raise ReleaseManifestError(
            "release source migration up mismatch"
        )
    if migration.get("down") != MIGRATION_DOWN_PATH:
        raise ReleaseManifestError(
            "release source migration down mismatch"
        )
    if migration.get("prerequisites") != list(
        MIGRATION_PREREQUISITE_PATHS
    ):
        raise ReleaseManifestError(
            "release source migration prerequisites mismatch"
        )
    if migration.get("steps") != [
        dict(item)
        for item in CANONICAL_MIGRATION_STEPS
    ]:
        raise ReleaseManifestError(
            "release source migration steps mismatch"
        )
    if migration.get("migration_files") != list(
        CANONICAL_MIGRATION_PATHS
    ):
        raise ReleaseManifestError(
            "release source migration files mismatch"
        )
    if migration.get("db_schema_epoch") != SCHEMA_EPOCHS["db"]:
        raise ReleaseManifestError(
            "release source migration schema epoch mismatch"
        )
    if migration.get("python_dependencies") != ["psycopg2"]:
        raise ReleaseManifestError(
            "release source migration dependency mismatch"
        )
    migration_manifest_sha256 = _require_sha256(
        str(migration.get("manifest_sha256") or ""),
        "release source migration manifest sha256",
    )
    systemd = document.get("systemd_resource_contract")
    if not isinstance(systemd, dict):
        raise ReleaseManifestError(
            "release source systemd resource metadata is invalid"
        )
    if systemd.get("path") != SYSTEMD_RESOURCE_CONTRACT_NAME:
        raise ReleaseManifestError(
            "release source systemd resource path mismatch"
        )
    systemd_sha256 = _require_sha256(
        str(systemd.get("sha256") or ""),
        "release source systemd resource sha256",
    )
    watcher_runtime = document.get("watcher_runtime")
    if not isinstance(watcher_runtime, dict):
        raise ReleaseManifestError(
            "release source watcher runtime metadata is invalid"
        )
    if watcher_runtime.get("manifest") != WATCHER_RUNTIME_MANIFEST_NAME:
        raise ReleaseManifestError(
            "release source watcher runtime manifest path mismatch"
        )
    watcher_runtime_manifest_sha256 = _require_sha256(
        str(watcher_runtime.get("manifest_sha256") or ""),
        "release source watcher runtime manifest sha256",
    )
    root = payload_root.resolve()
    bundle_path = root / "bundle-manifest.json"
    if not bundle_path.is_file() or sha256_file(bundle_path) != bundle_sha256:
        raise ReleaseManifestError(
            "release source bundle manifest hash mismatch"
        )
    migration_path = root / MIGRATION_MANIFEST_NAME
    if (
        not migration_path.is_file()
        or sha256_file(migration_path) != migration_manifest_sha256
    ):
        raise ReleaseManifestError(
            "release source migration manifest hash mismatch"
        )
    systemd_path = root / SYSTEMD_RESOURCE_CONTRACT_NAME
    if (
        not systemd_path.is_file()
        or sha256_file(systemd_path) != systemd_sha256
    ):
        raise ReleaseManifestError(
            "release source systemd resource hash mismatch"
        )
    watcher_manifest_path = root / WATCHER_RUNTIME_MANIFEST_NAME
    if (
        not watcher_manifest_path.is_file()
        or sha256_file(watcher_manifest_path)
        != watcher_runtime_manifest_sha256
    ):
        raise ReleaseManifestError(
            "release source watcher runtime manifest hash mismatch"
        )
    return {
        "source_commit": source_commit,
        "source_tree": source_tree,
        "bundle_manifest_sha256": bundle_sha256,
        "migration_manifest_sha256": migration_manifest_sha256,
        "systemd_resource_contract_sha256": systemd_sha256,
        "watcher_runtime_manifest_sha256": (
            watcher_runtime_manifest_sha256
        ),
        "files": sorted(
            release_files,
            key=lambda item: item["release_path"],
        ),
    }


def _validated_docker_resource_contract(
    raw: Any,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != DOCKER_RESOURCE_FIELDS:
        raise ReleaseManifestError(f"{label} fields mismatch")
    validated: dict[str, Any] = {}
    for field in (
        "memory_bytes",
        "memory_swap_bytes",
        "nano_cpus",
        "pids_limit",
        "nofile_soft",
        "nofile_hard",
    ):
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ReleaseManifestError(
                f"{label}.{field} must be a positive integer"
            )
        validated[field] = value
    if validated["memory_swap_bytes"] != validated["memory_bytes"]:
        raise ReleaseManifestError(
            f"{label}.memory_swap_bytes must equal memory_bytes"
        )
    if validated["nofile_soft"] != validated["nofile_hard"]:
        raise ReleaseManifestError(
            f"{label} nofile soft/hard limits must match"
        )
    restart_policy = str(raw.get("restart_policy") or "").strip()
    if restart_policy not in {"always", "on-failure"}:
        raise ReleaseManifestError(
            f"{label}.restart_policy is invalid"
        )
    validated["restart_policy"] = restart_policy
    return validated


def validate_systemd_resource_contract(
    path: Path,
    *,
    payload_root: Path,
) -> dict[str, Any]:
    document = _load_json(path)
    if (
        document.get("schema_version")
        != SYSTEMD_RESOURCE_CONTRACT_SCHEMA_VERSION
    ):
        raise ReleaseManifestError(
            "systemd resource contract schema version mismatch"
        )
    resources = document.get("resources")
    if not isinstance(resources, list) or len(resources) != 4:
        raise ReleaseManifestError(
            "systemd resource contract must contain four resources"
        )
    validated = []
    artifacts = set()
    root = payload_root.resolve()
    for raw in resources:
        if not isinstance(raw, dict):
            raise ReleaseManifestError(
                "systemd resource contract entry is invalid"
            )
        artifact = _validated_payload_relative_path(
            str(raw.get("artifact") or ""),
            label="systemd resource artifact",
        )
        if artifact in artifacts:
            raise ReleaseManifestError(
                f"systemd resource artifact is duplicated: {artifact}"
            )
        artifacts.add(artifact)
        digest = _require_sha256(
            str(raw.get("sha256") or ""),
            "systemd resource artifact sha256",
        )
        artifact_path = root / artifact
        if not artifact_path.is_file() or sha256_file(artifact_path) != digest:
            raise ReleaseManifestError(
                f"systemd resource artifact hash mismatch: {artifact}"
            )
        application = str(raw.get("application") or "").strip()
        effective_docker_host_config = False
        if artifact in {
            NODE_DOCKER_RESOURCE_ARTIFACT,
            REDIS_DOCKER_RESOURCE_ARTIFACT,
        }:
            if application != "docker_host_config":
                raise ReleaseManifestError(
                    f"{artifact} must use docker_host_config"
                )
            effective_docker_host_config = (
                _validated_docker_resource_contract(
                    raw.get("effective_docker_host_config"),
                    label=f"{artifact} docker HostConfig",
                )
            )
        elif application not in {
            "inline_service_unit",
            "systemd_service_drop_in",
        }:
            raise ReleaseManifestError(
                f"{artifact} systemd application is invalid"
            )
        expected_binding = SYSTEMD_RESOURCE_BINDINGS.get(artifact)
        if expected_binding is None:
            raise ReleaseManifestError(
                f"systemd resource artifact is unexpected: {artifact}"
            )
        consumer_entrypoint = str(
            raw.get("consumer_entrypoint") or ""
        ).strip()
        if (
            consumer_entrypoint
            != expected_binding["consumer_entrypoint"]
        ):
            raise ReleaseManifestError(
                f"{artifact} consumer entrypoint mismatch"
            )
        owner_units = raw.get("owner_units")
        if owner_units != expected_binding["owner_units"]:
            raise ReleaseManifestError(
                f"{artifact} owner units mismatch"
            )
        destinations = raw.get("destinations")
        if destinations != expected_binding["destinations"]:
            raise ReleaseManifestError(
                f"{artifact} destinations mismatch"
            )
        validated.append(
            {
                "artifact": artifact,
                "sha256": digest,
                "application": application,
                "consumer_entrypoint": consumer_entrypoint,
                "owner_units": list(owner_units),
                "destinations": list(destinations),
                "effective_docker_host_config": (
                    effective_docker_host_config
                ),
            }
        )
    expected_artifacts = set(SYSTEMD_RESOURCE_BINDINGS)
    if artifacts != expected_artifacts:
        raise ReleaseManifestError(
            "systemd resource artifact exact-set mismatch"
        )
    return {
        "schema_version": SYSTEMD_RESOURCE_CONTRACT_SCHEMA_VERSION,
        "resources": sorted(
            validated,
            key=lambda item: item["artifact"],
        ),
    }


def docker_resource_contract(
    contract: dict[str, Any],
    artifact: str,
) -> dict[str, Any]:
    matches = [
        item["effective_docker_host_config"]
        for item in contract["resources"]
        if item["artifact"] == artifact
    ]
    if len(matches) != 1 or matches[0] is False:
        raise ReleaseManifestError(
            f"docker resource contract is missing: {artifact}"
        )
    return dict(matches[0])


def build_attestation_subject_sha256(
    value: dict[str, Any],
) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def build_attestation_labels(
    inputs: dict[str, Any],
) -> dict[str, str]:
    return {
        LABEL_BUILD_ATTESTATION_SUBJECT: (
            build_attestation_subject_sha256(inputs)
        ),
        LABEL_BUILD_BASE_IMAGE: inputs["base_image_digest"],
        LABEL_BUILD_BUNDLE_MANIFEST: inputs["bundle_manifest_sha256"],
        LABEL_BUILD_DEPENDENCY_INVENTORY: (
            inputs["dependency_inventory_sha256"]
        ),
        LABEL_BUILD_DEPENDENCY_LOCK: inputs["dependency_lock_sha256"],
        LABEL_BUILD_DOCKERFILE: inputs["dockerfile_sha256"],
        LABEL_BUILD_MIGRATION_MANIFEST: (
            inputs["migration_manifest_sha256"]
        ),
        LABEL_BUILD_RELEASE_SOURCE_MANIFEST: (
            inputs["release_source_manifest_sha256"]
        ),
    }


def _docker_image_labels(image_digest: str) -> dict[str, str]:
    try:
        output = subprocess.check_output(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                image_digest,
            ],
            text=True,
            stderr=subprocess.PIPE,
        )
        raw = json.loads(output)
    except (
        OSError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        raise ReleaseManifestError(
            "cannot inspect immutable image build labels"
        ) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ReleaseManifestError(
            "immutable image build labels are invalid"
        )
    return {
        str(key): str(value)
        for key, value in raw.items()
    }


def validate_build_attestation(
    path: Path,
    *,
    payload_root: Path,
    expected_image_digest: str | None = None,
    verify_image_labels: bool = False,
) -> dict[str, Any]:
    document = _load_json(path)
    if set(document) != BUILD_ATTESTATION_REQUIRED_FIELDS:
        raise ReleaseManifestError(
            "immutable build attestation fields mismatch"
        )
    if (
        document.get("schema_version")
        != BUILD_ATTESTATION_SCHEMA_VERSION
    ):
        raise ReleaseManifestError(
            "immutable build attestation schema version mismatch"
        )
    root = payload_root.resolve()
    references = {}
    for field, expected_path in (
        ("bundle_manifest", "bundle-manifest.json"),
        ("release_source_manifest", RELEASE_SOURCE_MANIFEST_NAME),
        ("dependency_lock", RELEASE_DEPENDENCY_LOCK_NAME),
        ("dependency_inventory", DEPENDENCY_INVENTORY_NAME),
        ("migration_manifest", MIGRATION_MANIFEST_NAME),
    ):
        raw = document.get(field)
        if not isinstance(raw, dict) or set(raw) != {"path", "sha256"}:
            raise ReleaseManifestError(
                f"immutable build attestation {field} is invalid"
            )
        if raw.get("path") != expected_path:
            raise ReleaseManifestError(
                f"immutable build attestation {field} path mismatch"
            )
        digest = _require_sha256(
            str(raw.get("sha256") or ""),
            f"immutable build attestation {field} sha256",
        )
        artifact_path = root / expected_path
        if (
            not artifact_path.is_file()
            or sha256_file(artifact_path) != digest
        ):
            raise ReleaseManifestError(
                f"immutable build attestation {field} hash mismatch"
            )
        references[field] = digest
    inputs = {
        "base_image_digest": _require_image_digest(
            str(document.get("base_image_digest") or "")
        ),
        "dockerfile_sha256": _require_sha256(
            str(document.get("dockerfile_sha256") or ""),
            "immutable build Dockerfile sha256",
        ),
        "bundle_manifest_sha256": references["bundle_manifest"],
        "release_source_manifest_sha256": references[
            "release_source_manifest"
        ],
        "dependency_lock_sha256": references["dependency_lock"],
        "dependency_inventory_sha256": references[
            "dependency_inventory"
        ],
        "migration_manifest_sha256": references["migration_manifest"],
    }
    build_subject = _require_sha256(
        str(document.get("build_subject_sha256") or ""),
        "immutable build subject sha256",
    )
    if build_subject != build_attestation_subject_sha256(inputs):
        raise ReleaseManifestError(
            "immutable build attestation subject mismatch"
        )
    image_digest = _require_image_digest(
        str(document.get("image_digest") or "")
    )
    if (
        expected_image_digest is not None
        and image_digest != _require_image_digest(expected_image_digest)
    ):
        raise ReleaseManifestError(
            "immutable build attestation image digest mismatch"
        )
    expected_labels = build_attestation_labels(inputs)
    raw_labels = document.get("image_labels")
    if not isinstance(raw_labels, dict):
        raise ReleaseManifestError(
            "immutable build attestation labels are invalid"
        )
    attested_labels = {
        str(key): str(value)
        for key, value in raw_labels.items()
    }
    if attested_labels != expected_labels:
        raise ReleaseManifestError(
            "immutable build attestation labels mismatch"
        )
    if verify_image_labels:
        actual_labels = _docker_image_labels(image_digest)
        for key, expected in expected_labels.items():
            if actual_labels.get(key) != expected:
                raise ReleaseManifestError(
                    f"immutable image build label mismatch: {key}"
                )
    inventory = _load_json(root / DEPENDENCY_INVENTORY_NAME)
    if inventory.get("lock_path") != RELEASE_DEPENDENCY_LOCK_NAME:
        raise ReleaseManifestError(
            "dependency inventory lock path mismatch"
        )
    if inventory.get("lock_sha256") != references["dependency_lock"]:
        raise ReleaseManifestError(
            "dependency inventory lock hash mismatch"
        )
    return {
        "schema_version": BUILD_ATTESTATION_SCHEMA_VERSION,
        "inputs": inputs,
        "build_subject_sha256": build_subject,
        "image_digest": image_digest,
        "image_labels": expected_labels,
    }


def release_review_subject_sha256(
    manifest: dict[str, Any],
) -> str:
    excluded = {
        "generated_at",
        "release_id",
        "review_subject_sha256",
        "reviewer_trust_proof",
    }
    payload = {}
    for key, value in manifest.items():
        if key in excluded:
            continue
        if key == "node_configs" and isinstance(value, list):
            # host_path embeds the per-run backup directory, so a proof
            # signed against one deploy run could never validate in the
            # next; the review subject binds artifact content (sha256,
            # normalized hash, runtime resources), not staging paths.
            value = [
                {
                    field: field_value
                    for field, field_value in entry.items()
                    if field != "host_path"
                }
                if isinstance(entry, dict)
                else entry
                for entry in value
            ]
        payload[key] = value
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def validate_reviewer_trust_proof(
    path: Path,
    *,
    pinned_sha256: str,
    expected_subject_sha256: str,
    expected_source_commit: str,
    expected_build_attestation_sha256: str,
) -> dict[str, str]:
    pinned = _require_sha256(
        pinned_sha256,
        "pinned reviewer trust proof sha256",
    )
    actual = sha256_file(path)
    if actual != pinned:
        raise ReleaseManifestError(
            "reviewer trust proof differs from externally pinned hash"
        )
    document = _load_json(path)
    required_fields = {
        "schema_version",
        "reviewer",
        "decision",
        "source_commit",
        "review_subject_sha256",
        "build_attestation_sha256",
    }
    if set(document) != required_fields:
        raise ReleaseManifestError(
            "reviewer trust proof fields mismatch"
        )
    if (
        document.get("schema_version")
        != REVIEWER_TRUST_PROOF_SCHEMA_VERSION
    ):
        raise ReleaseManifestError(
            "reviewer trust proof schema version mismatch"
        )
    reviewer = str(document.get("reviewer") or "").strip()
    if not reviewer:
        raise ReleaseManifestError(
            "reviewer trust proof reviewer is required"
        )
    if document.get("decision") != "approved":
        raise ReleaseManifestError(
            "reviewer trust proof decision must be approved"
        )
    if document.get("source_commit") != expected_source_commit:
        raise ReleaseManifestError(
            "reviewer trust proof source commit mismatch"
        )
    if document.get("review_subject_sha256") != expected_subject_sha256:
        raise ReleaseManifestError(
            "reviewer trust proof subject mismatch"
        )
    if (
        document.get("build_attestation_sha256")
        != expected_build_attestation_sha256
    ):
        raise ReleaseManifestError(
            "reviewer trust proof build attestation mismatch"
        )
    return {
        "path": path.name,
        "sha256": actual,
        "reviewer": reviewer,
        "decision": "approved",
        "review_subject_sha256": expected_subject_sha256,
    }


def _validated_schema_epochs(
    document: dict[str, Any],
    *,
    label: str,
    allow_missing: bool = False,
) -> dict[str, str]:
    raw_epochs = document.get("schema_epochs")
    if raw_epochs is None and allow_missing:
        return dict(SCHEMA_EPOCHS)
    if not isinstance(raw_epochs, dict):
        raise ReleaseManifestError(f"{label} schema_epochs must be an object")
    epochs = {
        str(key): str(value).strip()
        for key, value in raw_epochs.items()
    }
    if epochs != SCHEMA_EPOCHS:
        raise ReleaseManifestError(
            f"{label} schema_epochs must equal {SCHEMA_EPOCHS}"
        )
    return epochs


def _validated_bundle_files(
    bundle: dict[str, Any],
    patch_root: Path,
) -> list[dict[str, Any]]:
    raw_files = bundle.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ReleaseManifestError("bundle manifest files must be non-empty")

    files = []
    bundle_paths = set()
    mount_targets = set()
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise ReleaseManifestError("bundle file entry must be an object")
        bundle_path = str(raw.get("bundle_path", "")).strip()
        mount_target = str(raw.get("mount_target", "")).strip()
        digest = _require_sha256(str(raw.get("sha256", "")), "file sha256")
        if not bundle_path or Path(bundle_path).name != bundle_path:
            raise ReleaseManifestError(
                f"bundle_path must be a basename: {bundle_path}"
            )
        if not mount_target.startswith("/"):
            raise ReleaseManifestError(
                f"mount_target must be absolute: {mount_target}"
            )
        if bundle_path in bundle_paths:
            raise ReleaseManifestError(
                f"duplicate bundle_path: {bundle_path}"
            )
        if mount_target in mount_targets:
            raise ReleaseManifestError(
                f"duplicate mount_target: {mount_target}"
            )
        bundle_paths.add(bundle_path)
        mount_targets.add(mount_target)
        files.append(
            {
                "bundle_path": bundle_path,
                "host_path": str(patch_root / bundle_path),
                "mount_target": mount_target,
                "sha256": digest,
            }
        )
    return sorted(files, key=lambda item: item["mount_target"])


def _file_contract(
    files: list[dict[str, Any]],
) -> set[tuple[str, str, str]]:
    return {
        (
            item["bundle_path"],
            item["mount_target"],
            item["sha256"],
        )
        for item in files
    }


def require_release_matches_bundle(
    manifest: dict[str, Any],
    bundle_path: Path,
) -> None:
    validated_manifest = validate_release_manifest(manifest)
    bundle = _load_json(bundle_path)
    bundle_commit = str(bundle.get("repo_commit", "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", bundle_commit):
        raise ReleaseManifestError("bundle repo_commit is invalid")
    if bundle.get("repo_dirty") is not False:
        raise ReleaseManifestError("bundle must record repo_dirty=false")
    if validated_manifest["release_commit"] != bundle_commit:
        raise ReleaseManifestError(
            "release manifest commit differs from bundle repo_commit"
        )
    bundle_epochs = _validated_schema_epochs(bundle, label="bundle")
    if validated_manifest["schema_epochs"] != bundle_epochs:
        raise ReleaseManifestError(
            "release manifest and bundle schema_epochs differ"
        )
    bundle_files = _validated_bundle_files(
        bundle,
        bundle_path.parent,
    )
    manifest_contract = _file_contract(validated_manifest["files"])
    bundle_contract = _file_contract(bundle_files)
    if manifest_contract != bundle_contract:
        missing = sorted(bundle_contract - manifest_contract)
        extra = sorted(manifest_contract - bundle_contract)
        raise ReleaseManifestError(
            "release manifest and bundle file contracts differ: "
            f"missing={missing} extra={extra}"
        )


def require_transition_runtime_files(files: list[dict[str, Any]]) -> None:
    actual = {
        (item["bundle_path"], item["mount_target"])
        for item in files
    }
    required = {
        (bundle_path, mount_target)
        for bundle_path, _, mount_target in TRANSITION_RUNTIME_FILES
    }
    missing = sorted(required - actual)
    if missing:
        raise ReleaseManifestError(
            f"bundle lacks transition runtime mounts: {missing}"
        )


def validate_bundle_payload(
    bundle_path: Path,
    patch_root: Path,
    *,
    require_transition_runtime: bool,
) -> list[dict[str, Any]]:
    bundle = _load_json(bundle_path)
    _validated_schema_epochs(
        bundle,
        label="bundle",
        allow_missing=True,
    )
    files = _validated_bundle_files(bundle, patch_root)
    if require_transition_runtime:
        require_transition_runtime_files(files)
    bundle_dir = bundle_path.parent
    for item in files:
        payload_path = bundle_dir / item["bundle_path"]
        if not payload_path.is_file():
            raise ReleaseManifestError(
                f"bundle payload is missing: {item['bundle_path']}"
            )
        actual_hash = sha256_file(payload_path)
        if actual_hash != item["sha256"]:
            raise ReleaseManifestError(
                f"bundle payload hash mismatch: {item['bundle_path']}"
            )
    return files


def augment_transition_bundle(
    bundle_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    bundle = _load_json(bundle_path)
    bundle["schema_epochs"] = _validated_schema_epochs(
        bundle,
        label="bundle",
        allow_missing=True,
    )
    raw_files = bundle.get("files")
    if not isinstance(raw_files, list):
        raise ReleaseManifestError("bundle manifest files must be a list")
    by_target = {}
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise ReleaseManifestError("bundle file entry must be an object")
        target = str(raw.get("mount_target", "")).strip()
        if target:
            by_target[target] = raw

    for bundle_name, source_relative, mount_target in TRANSITION_RUNTIME_FILES:
        source = (repo_root / source_relative).resolve()
        try:
            source.relative_to(repo_root.resolve())
        except ValueError as exc:
            raise ReleaseManifestError(
                f"runtime source escapes repository: {source_relative}"
            ) from exc
        if not source.is_file():
            raise ReleaseManifestError(
                f"runtime source is missing: {source_relative}"
            )
        destination = bundle_path.parent / bundle_name
        shutil.copyfile(source, destination)
        by_target[mount_target] = {
            "bundle_path": bundle_name,
            "source_path": source_relative,
            "mount_target": mount_target,
            "sha256": sha256_file(destination),
            "size": destination.stat().st_size,
        }

    bundle["files"] = sorted(
        by_target.values(),
        key=lambda item: str(item.get("mount_target", "")),
    )
    bundle["transition_runtime_augmented_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    bundle_path.write_text(
        json.dumps(bundle, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    validate_bundle_payload(
        bundle_path,
        DEFAULT_PATCH_ROOT,
        require_transition_runtime=True,
    )
    return bundle


def _release_id_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in manifest.items()
        if key not in {"generated_at", "release_id"}
    }


def calculate_release_id(manifest: dict[str, Any]) -> str:
    payload = _release_id_payload(manifest)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _expected_release_payload(
    *,
    payload_root: Path,
    source_manifest: dict[str, Any],
) -> list[dict[str, str]]:
    root = payload_root.resolve()
    bundle_path = root / "bundle-manifest.json"
    bundle = _load_json(bundle_path)
    bundle_files = _validated_bundle_files(bundle, root)
    expected: dict[str, str] = {
        "bundle-manifest.json": sha256_file(bundle_path),
        RELEASE_SOURCE_MANIFEST_NAME: sha256_file(
            root / RELEASE_SOURCE_MANIFEST_NAME
        ),
        MIGRATION_MANIFEST_NAME: source_manifest[
            "migration_manifest_sha256"
        ],
        SYSTEMD_RESOURCE_CONTRACT_NAME: source_manifest[
            "systemd_resource_contract_sha256"
        ],
        WATCHER_RUNTIME_MANIFEST_NAME: source_manifest[
            "watcher_runtime_manifest_sha256"
        ],
    }
    for item in bundle_files:
        relative = item["bundle_path"]
        digest = item["sha256"]
        previous = expected.get(relative)
        if previous is not None and previous != digest:
            raise ReleaseManifestError(
                f"release payload path has conflicting hashes: {relative}"
            )
        expected[relative] = digest
    for item in source_manifest["files"]:
        relative = item["release_path"]
        digest = item["sha256"]
        previous = expected.get(relative)
        if previous is not None and previous != digest:
            raise ReleaseManifestError(
                f"release payload path has conflicting hashes: {relative}"
            )
        expected[relative] = digest
    return [
        {
            "path": relative,
            "sha256": expected[relative],
        }
        for relative in sorted(expected)
    ]


def validate_release_payload_envelope(
    *,
    payload_root: Path,
) -> dict[str, Any]:
    root = payload_root.resolve()
    source_path = root / RELEASE_SOURCE_MANIFEST_NAME
    source_manifest = validate_release_source_manifest(
        source_path,
        payload_root=root,
    )
    checksums_path = root / SHA256SUMS_NAME
    checksum_entries = parse_sha256sums(checksums_path)
    expected_entries = _expected_release_payload(
        payload_root=root,
        source_manifest=source_manifest,
    )
    if checksum_entries != expected_entries:
        expected_map = _payload_hash_map(expected_entries)
        actual_map = _payload_hash_map(checksum_entries)
        raise ReleaseManifestError(
            "release SHA256SUMS exact-set mismatch: "
            f"missing={sorted(set(expected_map) - set(actual_map))} "
            f"extra={sorted(set(actual_map) - set(expected_map))}"
        )
    _verify_payload_hashes(root, checksum_entries)
    systemd_contract = validate_systemd_resource_contract(
        root / SYSTEMD_RESOURCE_CONTRACT_NAME,
        payload_root=root,
    )
    migration_manifest = validate_migration_manifest(
        root / MIGRATION_MANIFEST_NAME,
        payload_root=root,
    )
    lock_path = root / RELEASE_DEPENDENCY_LOCK_NAME
    inventory_path = root / DEPENDENCY_INVENTORY_NAME
    inventory = build_dependency_inventory(lock_path)
    written_inventory = _load_json(inventory_path)
    if written_inventory != inventory:
        raise ReleaseManifestError(
            "dependency inventory differs from reviewed lock"
        )
    source_file_map = {
        item["release_path"]: item["sha256"]
        for item in source_manifest["files"]
    }
    lock_sha256 = sha256_file(lock_path)
    if source_file_map.get(RELEASE_DEPENDENCY_LOCK_NAME) != lock_sha256:
        raise ReleaseManifestError(
            "dependency lock differs from release source manifest"
        )
    checksum_map = _payload_hash_map(checksum_entries)
    if checksum_map.get(RELEASE_DEPENDENCY_LOCK_NAME) != lock_sha256:
        raise ReleaseManifestError(
            "dependency lock differs from release SHA256SUMS"
        )
    return {
        "release_payload": checksum_entries,
        "sha256sums_sha256": sha256_file(checksums_path),
        "release_source_manifest_sha256": sha256_file(source_path),
        "systemd_resource_contract_sha256": sha256_file(
            root / SYSTEMD_RESOURCE_CONTRACT_NAME
        ),
        "dependency_lock_sha256": lock_sha256,
        "dependency_inventory_sha256": sha256_file(inventory_path),
        "migration_manifest_sha256": sha256_file(
            root / MIGRATION_MANIFEST_NAME
        ),
        "source_manifest": source_manifest,
        "systemd_resource_contract": systemd_contract,
        "migration_manifest": migration_manifest,
    }


def validate_strict_release_envelope(
    manifest: dict[str, Any],
    *,
    payload_root: Path,
    reviewer_trust_sha256: str,
    verify_image_labels: bool,
) -> dict[str, Any]:
    if manifest.get("schema_version") not in NODE_CONFIG_SCHEMA_VERSIONS:
        raise ReleaseManifestError(
            "hardening live requires a strict node-config release schema"
        )
    if manifest.get("release_purpose") != RELEASE_PURPOSE_HARDENING:
        raise ReleaseManifestError(
            "strict release envelope requires hardening purpose"
        )
    root = payload_root.resolve()
    payload = validate_release_payload_envelope(payload_root=root)
    for field in (
        "sha256sums_sha256",
        "release_source_manifest_sha256",
        "systemd_resource_contract_sha256",
        "dependency_inventory_sha256",
        "migration_manifest_sha256",
    ):
        if manifest.get(field) != payload[field]:
            raise ReleaseManifestError(
                f"strict release envelope hash mismatch: {field}"
            )
    if manifest.get("dependency_lock_sha256") != payload[
        "dependency_lock_sha256"
    ]:
        raise ReleaseManifestError(
            "strict release dependency lock hash mismatch"
        )
    if manifest.get("release_payload") != payload["release_payload"]:
        raise ReleaseManifestError(
            "strict release payload exact-set mismatch"
        )
    build_attestation_path = root / BUILD_ATTESTATION_NAME
    build_attestation_sha256 = sha256_file(build_attestation_path)
    if manifest.get("build_attestation_sha256") != (
        build_attestation_sha256
    ):
        raise ReleaseManifestError(
            "strict release build attestation hash mismatch"
        )
    build_attestation = validate_build_attestation(
        build_attestation_path,
        payload_root=root,
        expected_image_digest=str(manifest.get("image_digest") or ""),
        verify_image_labels=verify_image_labels,
    )
    attested_inputs = build_attestation["inputs"]
    input_expectations = {
        "bundle_manifest_sha256": payload["source_manifest"][
            "bundle_manifest_sha256"
        ],
        "release_source_manifest_sha256": payload[
            "release_source_manifest_sha256"
        ],
        "dependency_lock_sha256": payload["dependency_lock_sha256"],
        "dependency_inventory_sha256": payload[
            "dependency_inventory_sha256"
        ],
        "migration_manifest_sha256": payload[
            "migration_manifest_sha256"
        ],
    }
    for field, expected in input_expectations.items():
        if attested_inputs[field] != expected:
            raise ReleaseManifestError(
                f"build attestation release input mismatch: {field}"
            )
    review_subject = release_review_subject_sha256(manifest)
    if manifest.get("review_subject_sha256") != review_subject:
        raise ReleaseManifestError(
            "strict release review subject mismatch"
        )
    proof_metadata = manifest.get("reviewer_trust_proof")
    if not isinstance(proof_metadata, dict):
        payload["build_attestation"] = build_attestation
        return payload
    try:
        proof_path_value = str(proof_metadata.get("path") or "")
        proof_path_value = _validated_payload_relative_path(
            proof_path_value,
            label="reviewer trust proof path",
        )
        proof_path = root / proof_path_value
        proof = validate_reviewer_trust_proof(
            proof_path,
            pinned_sha256=reviewer_trust_sha256,
            expected_subject_sha256=review_subject,
            expected_source_commit=str(manifest["release_commit"]),
            expected_build_attestation_sha256=build_attestation_sha256,
        )
        if proof_metadata == proof:
            payload["reviewer_trust_proof"] = proof
    except ReleaseManifestError:
        pass
    payload["build_attestation"] = build_attestation
    return payload


def build_release_manifest(
    bundle: dict[str, Any],
    *,
    image_digest: str,
    config_sha256: str | None,
    dependency_lock_sha256: str,
    patch_root: Path,
    delivery_mode: str = DELIVERY_IMMUTABLE,
    node_configs: list[dict[str, Any]] | None = None,
    emergency_rollback: bool = False,
    dependency_inventory_sha256: str | None = None,
    migration_manifest_sha256: str | None = None,
    release_payload: list[dict[str, str]] | None = None,
    sha256sums_sha256: str | None = None,
    release_source_manifest_sha256: str | None = None,
    systemd_resource_contract_sha256: str | None = None,
    build_attestation_sha256: str | None = None,
    reviewer_trust_proof: dict[str, str] | None = None,
    _allow_unreviewed_subject: bool = False,
) -> dict[str, Any]:
    release_commit = str(bundle.get("repo_commit", "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", release_commit):
        raise ReleaseManifestError("bundle repo_commit is invalid")
    if bundle.get("repo_dirty"):
        raise ReleaseManifestError(
            "bundle was built from a dirty repository"
        )
    schema_epochs = _validated_schema_epochs(
        bundle,
        label="bundle",
        allow_missing=True,
    )

    schema_version = LEGACY_SCHEMA_VERSION
    validated_config_sha256 = ""
    validated_node_configs = []
    if node_configs is None:
        if config_sha256 is None:
            raise ReleaseManifestError(
                "legacy release manifest requires config_sha256"
            )
        validated_config_sha256 = _require_sha256(
            config_sha256,
            "config_sha256",
        )
    else:
        schema_version = SCHEMA_VERSION
        validated_node_configs = _validated_node_configs(node_configs)
        validated_config_sha256 = _release_config_sha256(
            validated_node_configs,
            schema_version=schema_version,
        )
        if config_sha256 is not None:
            requested_config_sha256 = _require_sha256(
                config_sha256,
                "config_sha256",
            )
            if requested_config_sha256 != validated_config_sha256:
                raise ReleaseManifestError(
                    "config_sha256 differs from node config artifacts"
                )

    validated_delivery_mode = _require_delivery_mode(delivery_mode)
    release_purpose = RELEASE_PURPOSE_HARDENING
    if emergency_rollback:
        release_purpose = RELEASE_PURPOSE_EMERGENCY_ROLLBACK
    _validate_release_topology(
        validated_delivery_mode,
        release_purpose,
    )
    if (
        schema_version == LEGACY_SCHEMA_VERSION
        and release_purpose != RELEASE_PURPOSE_EMERGENCY_ROLLBACK
    ):
        raise ReleaseManifestError(
            "legacy release schema v2 is reserved for emergency rollback"
        )
    manifest = {
        "schema_version": schema_version,
        "schema_epochs": schema_epochs,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_commit": release_commit,
        "delivery_mode": validated_delivery_mode,
        "release_purpose": release_purpose,
        "image_digest": _require_image_digest(image_digest),
        "config_sha256": validated_config_sha256,
        "dependency_lock_sha256": _require_sha256(
            dependency_lock_sha256,
            "dependency_lock_sha256",
        ),
        "files": _validated_bundle_files(bundle, patch_root),
    }
    if dependency_inventory_sha256 is not None:
        manifest["dependency_inventory_sha256"] = _require_sha256(
            dependency_inventory_sha256,
            "dependency_inventory_sha256",
        )
    if migration_manifest_sha256 is not None:
        manifest["migration_manifest_sha256"] = _require_sha256(
            migration_manifest_sha256,
            "migration_manifest_sha256",
        )
    if validated_node_configs:
        manifest["node_configs"] = validated_node_configs
    if release_purpose == RELEASE_PURPOSE_HARDENING:
        if schema_version != SCHEMA_VERSION:
            raise ReleaseManifestError(
                "account-stall hardening build requires strict release "
                "schema v4"
            )
        missing_values = []
        for field, value in (
            ("release_payload", release_payload),
            ("sha256sums_sha256", sha256sums_sha256),
            (
                "release_source_manifest_sha256",
                release_source_manifest_sha256,
            ),
            (
                "systemd_resource_contract_sha256",
                systemd_resource_contract_sha256,
            ),
            ("build_attestation_sha256", build_attestation_sha256),
        ):
            if value is None:
                missing_values.append(field)
        if (
            reviewer_trust_proof is None
            and not _allow_unreviewed_subject
        ):
            missing_values.append("reviewer_trust_proof")
        if dependency_inventory_sha256 is None:
            missing_values.append("dependency_inventory_sha256")
        if migration_manifest_sha256 is None:
            missing_values.append("migration_manifest_sha256")
        if missing_values:
            raise ReleaseManifestError(
                "strict release fields are required: "
                f"{sorted(missing_values)}"
            )
        manifest["release_payload"] = _validated_release_payload(
            release_payload
        )
        manifest["sha256sums_sha256"] = _require_sha256(
            str(sha256sums_sha256),
            "sha256sums_sha256",
        )
        manifest["release_source_manifest_sha256"] = _require_sha256(
            str(release_source_manifest_sha256),
            "release_source_manifest_sha256",
        )
        manifest["systemd_resource_contract_sha256"] = _require_sha256(
            str(systemd_resource_contract_sha256),
            "systemd_resource_contract_sha256",
        )
        manifest["build_attestation_sha256"] = _require_sha256(
            str(build_attestation_sha256),
            "build_attestation_sha256",
        )
        review_subject = release_review_subject_sha256(manifest)
        if _allow_unreviewed_subject and reviewer_trust_proof is None:
            manifest["review_subject_sha256"] = review_subject
        else:
            if not isinstance(reviewer_trust_proof, dict):
                raise ReleaseManifestError(
                    "reviewer_trust_proof must be an object"
                )
            expected_proof_fields = {
                "path",
                "sha256",
                "reviewer",
                "decision",
                "review_subject_sha256",
            }
            if set(reviewer_trust_proof) != expected_proof_fields:
                raise ReleaseManifestError(
                    "reviewer_trust_proof fields mismatch"
                )
            if reviewer_trust_proof.get("path") != REVIEWER_TRUST_PROOF_NAME:
                raise ReleaseManifestError(
                    "reviewer trust proof path mismatch"
                )
            if reviewer_trust_proof.get("decision") != "approved":
                raise ReleaseManifestError(
                    "reviewer trust proof decision must be approved"
                )
            reviewer = str(
                reviewer_trust_proof.get("reviewer") or ""
            ).strip()
            if not reviewer:
                raise ReleaseManifestError(
                    "reviewer trust proof reviewer is required"
                )
            proof_subject = _require_sha256(
                str(
                    reviewer_trust_proof.get(
                        "review_subject_sha256"
                    )
                    or ""
                ),
                "reviewer trust proof subject sha256",
            )
            if proof_subject != review_subject:
                raise ReleaseManifestError(
                    "reviewer trust proof subject differs from release"
                )
            manifest["review_subject_sha256"] = review_subject
            manifest["reviewer_trust_proof"] = {
                "path": REVIEWER_TRUST_PROOF_NAME,
                "sha256": _require_sha256(
                    str(reviewer_trust_proof.get("sha256") or ""),
                    "reviewer trust proof sha256",
                ),
                "reviewer": reviewer,
                "decision": "approved",
                "review_subject_sha256": proof_subject,
            }
    manifest["release_id"] = calculate_release_id(manifest)
    return manifest


def validate_release_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    schema_version = manifest.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ReleaseManifestError("unsupported release manifest schema")
    manifest["schema_epochs"] = _validated_schema_epochs(
        manifest,
        label="release manifest",
    )
    release_commit = str(manifest.get("release_commit", "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", release_commit):
        raise ReleaseManifestError("release_commit is invalid")
    manifest["release_commit"] = release_commit
    manifest["delivery_mode"] = _require_delivery_mode(
        str(manifest.get("delivery_mode", ""))
    )
    manifest["release_purpose"] = _require_release_purpose(
        str(manifest.get("release_purpose", ""))
    )
    _validate_release_topology(
        manifest["delivery_mode"],
        manifest["release_purpose"],
    )
    if (
        schema_version == LEGACY_SCHEMA_VERSION
        and manifest["release_purpose"]
        != RELEASE_PURPOSE_EMERGENCY_ROLLBACK
    ):
        raise ReleaseManifestError(
            "legacy release schema v2 is reserved for emergency rollback"
        )
    manifest["image_digest"] = _require_image_digest(
        str(manifest.get("image_digest", ""))
    )
    manifest["config_sha256"] = _require_sha256(
        str(manifest.get("config_sha256", "")),
        "config_sha256",
    )
    if schema_version in NODE_CONFIG_SCHEMA_VERSIONS:
        manifest["node_configs"] = _validated_node_configs(
            manifest.get("node_configs")
        )
        expected_config_sha256 = _release_config_sha256(
            manifest["node_configs"],
            schema_version=schema_version,
        )
        if manifest["config_sha256"] != expected_config_sha256:
            raise ReleaseManifestError(
                "config_sha256 differs from node config artifacts"
            )
    manifest["dependency_lock_sha256"] = _require_sha256(
        str(manifest.get("dependency_lock_sha256", "")),
        "dependency_lock_sha256",
    )
    if "dependency_inventory_sha256" in manifest:
        manifest["dependency_inventory_sha256"] = _require_sha256(
            str(manifest.get("dependency_inventory_sha256", "")),
            "dependency_inventory_sha256",
        )
    if "migration_manifest_sha256" in manifest:
        manifest["migration_manifest_sha256"] = _require_sha256(
            str(manifest.get("migration_manifest_sha256", "")),
            "migration_manifest_sha256",
        )
    if manifest["release_purpose"] == RELEASE_PURPOSE_HARDENING:
        if schema_version not in NODE_CONFIG_SCHEMA_VERSIONS:
            raise ReleaseManifestError(
                "account-stall hardening requires a strict node-config "
                "release schema"
            )
        missing = STRICT_V3_REQUIRED_FIELDS - set(manifest)
        if missing:
            raise ReleaseManifestError(
                "strict release fields are required: "
                f"{sorted(missing)}"
            )
        manifest["release_payload"] = _validated_release_payload(
            manifest.get("release_payload")
        )
        for field in (
            "sha256sums_sha256",
            "release_source_manifest_sha256",
            "systemd_resource_contract_sha256",
            "build_attestation_sha256",
        ):
            manifest[field] = _require_sha256(
                str(manifest.get(field) or ""),
                field,
            )
        proof = manifest.get("reviewer_trust_proof")
        review_subject = _require_sha256(
            str(manifest.get("review_subject_sha256") or ""),
            "review_subject_sha256",
        )
        calculated_subject = release_review_subject_sha256(manifest)
        if review_subject != calculated_subject:
            raise ReleaseManifestError(
                "strict release review subject mismatch"
            )
        if proof is None:
            manifest["review_subject_sha256"] = review_subject
        elif not isinstance(proof, dict):
            raise ReleaseManifestError(
                "reviewer_trust_proof must be an object"
            )
        else:
            if set(proof) != {
                "path",
                "sha256",
                "reviewer",
                "decision",
                "review_subject_sha256",
            }:
                raise ReleaseManifestError(
                    "reviewer_trust_proof fields mismatch"
                )
            if proof.get("path") != REVIEWER_TRUST_PROOF_NAME:
                raise ReleaseManifestError(
                    "reviewer trust proof path mismatch"
                )
            reviewer = str(proof.get("reviewer") or "").strip()
            if not reviewer or proof.get("decision") != "approved":
                raise ReleaseManifestError(
                    "reviewer trust proof approval is invalid"
                )
            proof_subject = _require_sha256(
                str(proof.get("review_subject_sha256") or ""),
                "reviewer trust proof subject sha256",
            )
            if proof_subject != calculated_subject:
                raise ReleaseManifestError(
                    "strict release review subject mismatch"
                )
            manifest["reviewer_trust_proof"] = {
                "path": REVIEWER_TRUST_PROOF_NAME,
                "sha256": _require_sha256(
                    str(proof.get("sha256") or ""),
                    "reviewer trust proof sha256",
                ),
                "reviewer": reviewer,
                "decision": "approved",
                "review_subject_sha256": proof_subject,
            }
            manifest["review_subject_sha256"] = review_subject
    patch_root = Path("/")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ReleaseManifestError("release manifest files must be non-empty")
    validated_files = []
    host_paths = set()
    mount_targets = set()
    for raw in files:
        if not isinstance(raw, dict):
            raise ReleaseManifestError("release file entry must be an object")
        bundle_path = str(raw.get("bundle_path", "")).strip()
        host_path = str(raw.get("host_path", "")).strip()
        mount_target = str(raw.get("mount_target", "")).strip()
        digest = _require_sha256(str(raw.get("sha256", "")), "file sha256")
        if not bundle_path or Path(bundle_path).name != bundle_path:
            raise ReleaseManifestError(
                f"bundle_path must be a basename: {bundle_path}"
            )
        if not host_path.startswith("/"):
            raise ReleaseManifestError(
                f"host_path must be absolute: {host_path}"
            )
        if not mount_target.startswith("/"):
            raise ReleaseManifestError(
                f"mount_target must be absolute: {mount_target}"
            )
        if host_path in host_paths:
            raise ReleaseManifestError(f"duplicate host_path: {host_path}")
        if mount_target in mount_targets:
            raise ReleaseManifestError(
                f"duplicate mount_target: {mount_target}"
            )
        host_paths.add(host_path)
        mount_targets.add(mount_target)
        validated_files.append(
            {
                "bundle_path": bundle_path,
                "host_path": str(patch_root / host_path.lstrip("/")),
                "mount_target": mount_target,
                "sha256": digest,
            }
        )
    manifest["files"] = sorted(
        validated_files,
        key=lambda item: item["mount_target"],
    )
    release_id = _require_sha256(
        str(manifest.get("release_id", "")),
        "release_id",
    )
    calculated = calculate_release_id(manifest)
    if release_id != calculated:
        raise ReleaseManifestError(
            "release_id does not match manifest contents"
        )
    manifest["release_id"] = release_id
    return manifest


def _docker_inspect(containers: list[str]) -> list[dict[str, Any]]:
    try:
        output = subprocess.check_output(
            ["docker", "inspect", *containers],
            text=True,
            stderr=subprocess.PIPE,
        )
        inspected = json.loads(output)
    except (
        OSError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        raise ReleaseManifestError("docker inspect failed") from exc
    if not isinstance(inspected, list) or len(inspected) != len(containers):
        raise ReleaseManifestError("docker inspect returned unexpected nodes")
    by_name = {}
    for item in inspected:
        if not isinstance(item, dict):
            raise ReleaseManifestError("docker inspect entry is invalid")
        name = str(item.get("Name", "")).lstrip("/")
        by_name[name] = item
    missing = [name for name in containers if name not in by_name]
    if missing:
        raise ReleaseManifestError(
            f"docker inspect missing containers: {missing}"
        )
    return [by_name[name] for name in containers]


def _find_mount(inspected: dict[str, Any], destination: str) -> dict[str, Any]:
    mounts = inspected.get("Mounts")
    if not isinstance(mounts, list):
        raise ReleaseManifestError("docker inspect Mounts is invalid")
    matches = [
        mount
        for mount in mounts
        if isinstance(mount, dict)
        and mount.get("Destination") == destination
    ]
    if len(matches) != 1:
        name = str(inspected.get("Name", "")).lstrip("/")
        raise ReleaseManifestError(
            f"{name}: expected one mount for {destination}"
        )
    return matches[0]


def _verify_docker_resource_contract(
    inspected: dict[str, Any],
    *,
    expected: dict[str, Any],
    container: str,
) -> dict[str, Any]:
    host_config = inspected.get("HostConfig")
    if not isinstance(host_config, dict):
        raise ReleaseManifestError(
            f"{container}: docker HostConfig is invalid"
        )
    actual = {
        "memory_bytes": host_config.get("Memory"),
        "memory_swap_bytes": host_config.get("MemorySwap"),
        "nano_cpus": host_config.get("NanoCpus"),
        "pids_limit": host_config.get("PidsLimit"),
    }
    restart_policy = host_config.get("RestartPolicy")
    if not isinstance(restart_policy, dict):
        raise ReleaseManifestError(
            f"{container}: Docker restart policy is invalid"
        )
    actual["restart_policy"] = restart_policy.get("Name")
    ulimits = host_config.get("Ulimits")
    if not isinstance(ulimits, list):
        raise ReleaseManifestError(
            f"{container}: Docker ulimits are invalid"
        )
    nofile = [
        item
        for item in ulimits
        if isinstance(item, dict) and item.get("Name") == "nofile"
    ]
    if len(nofile) != 1:
        raise ReleaseManifestError(
            f"{container}: Docker nofile ulimit exact-set mismatch"
        )
    actual["nofile_soft"] = nofile[0].get("Soft")
    actual["nofile_hard"] = nofile[0].get("Hard")
    if actual != expected:
        raise ReleaseManifestError(
            f"{container}: Docker resource contract mismatch"
        )
    return actual


def _runtime_identity(
    containers: list[str],
) -> tuple[list[dict[str, Any]], str, str]:
    inspected_nodes = _docker_inspect(containers)
    image_digests = []
    config_hashes = []
    for inspected in inspected_nodes:
        image_digests.append(
            _require_image_digest(str(inspected.get("Image", "")))
        )
        config_mount = _find_mount(inspected, "/cfg.json")
        source = Path(str(config_mount.get("Source", "")))
        if not source.is_absolute():
            raise ReleaseManifestError("config mount source must be absolute")
        config_hashes.append(node_config_sha256(source))
    if len(set(image_digests)) != 1:
        raise ReleaseManifestError(
            "node image content digests differ"
        )
    if len(set(config_hashes)) != 1:
        raise ReleaseManifestError(
            "node normalized config hashes differ"
        )
    return inspected_nodes, image_digests[0], config_hashes[0]


def _runtime_image_identity(
    containers: list[str],
) -> tuple[list[dict[str, Any]], str]:
    inspected_nodes = _docker_inspect(containers)
    image_digests = {
        _require_image_digest(str(inspected.get("Image", "")))
        for inspected in inspected_nodes
    }
    if len(image_digests) != 1:
        raise ReleaseManifestError("node image content digests differ")
    return inspected_nodes, next(iter(image_digests))


def capture_release_manifest(
    *,
    bundle_path: Path,
    dependency_lock_path: Path,
    output_path: Path,
    patch_root: Path,
    containers: list[str],
    require_transition_runtime: bool = False,
    delivery_mode: str = DELIVERY_IMMUTABLE,
    image_digest: str | None = None,
    node_config_specs: dict[str, tuple[str, Path]] | None = None,
    emergency_rollback: bool = False,
    migration_manifest_path: Path | None = None,
    build_attestation_path: Path | None = None,
    reviewer_trust_proof_path: Path | None = None,
    reviewer_trust_sha256: str | None = None,
    preview_review_subject: bool = False,
) -> dict[str, Any]:
    if preview_review_subject and delivery_mode != DELIVERY_IMMUTABLE:
        raise ReleaseManifestError(
            "review subject preview requires immutable delivery"
        )
    bundle = _load_json(bundle_path)
    bundle_files = _validated_bundle_files(bundle, patch_root)
    if require_transition_runtime:
        require_transition_runtime_files(bundle_files)
    delivery_mode = _require_delivery_mode(delivery_mode)
    if node_config_specs is None:
        raise ReleaseManifestError(
            "capture requires immutable node config artifacts"
        )
    node_configs = build_node_config_artifacts(node_config_specs)
    configured_containers = {
        item["container"]
        for item in node_configs
    }
    if configured_containers != set(containers):
        raise ReleaseManifestError(
            "node config artifact containers differ from capture nodes"
        )
    release_root = bundle_path.parent.resolve()
    reviewed_dependency_lock = (
        release_root / RELEASE_DEPENDENCY_LOCK_NAME
    )
    if dependency_lock_path.resolve() != reviewed_dependency_lock:
        raise ReleaseManifestError(
            "capture dependency lock must use reviewed release payload path"
        )
    dependency_inventory = build_dependency_inventory(
        reviewed_dependency_lock
    )
    dependency_lock_sha256 = sha256_file(dependency_lock_path)
    inventory_sha256 = dependency_inventory_sha256(
        dependency_inventory
    )
    selected_migration_manifest = migration_manifest_path
    if selected_migration_manifest is None:
        selected_migration_manifest = (
            bundle_path.parent / MIGRATION_MANIFEST_NAME
        )
    validate_migration_manifest(
        selected_migration_manifest,
        payload_root=bundle_path.parent,
    )
    selected_migration_sha256 = sha256_file(
        selected_migration_manifest
    )
    envelope: dict[str, Any] = {}
    reviewer_proof: dict[str, str] | None = None
    if delivery_mode == DELIVERY_IMMUTABLE:
        selected_attestation = build_attestation_path
        if selected_attestation is None:
            selected_attestation = release_root / BUILD_ATTESTATION_NAME
        attestation = validate_build_attestation(
            selected_attestation,
            payload_root=release_root,
            expected_image_digest=image_digest,
            verify_image_labels=True,
        )
        selected_image_digest = attestation["image_digest"]
        if image_digest is not None:
            requested_image = _require_image_digest(image_digest)
            if requested_image != selected_image_digest:
                raise ReleaseManifestError(
                    "requested image digest differs from build attestation"
                )
        envelope = validate_release_payload_envelope(
            payload_root=release_root
        )
        if envelope["dependency_lock_sha256"] != (
            dependency_lock_sha256
        ):
            raise ReleaseManifestError(
                "capture dependency lock differs from release envelope"
            )
        if envelope["dependency_inventory_sha256"] != inventory_sha256:
            raise ReleaseManifestError(
                "capture dependency inventory differs from release envelope"
            )
        if envelope["migration_manifest_sha256"] != (
            selected_migration_sha256
        ):
            raise ReleaseManifestError(
                "capture migration manifest differs from release envelope"
            )
        selected_proof_path = reviewer_trust_proof_path
        if selected_proof_path is None:
            selected_proof_path = (
                release_root / REVIEWER_TRUST_PROOF_NAME
            )
        if selected_proof_path.resolve() != (
            release_root / REVIEWER_TRUST_PROOF_NAME
        ):
            raise ReleaseManifestError(
                "reviewer trust proof must use the reviewed release path"
            )
        pinned_proof_sha256 = str(
            reviewer_trust_sha256
            or os.environ.get(
                "TRADER_RELEASE_REVIEWER_TRUST_SHA256",
                "",
            )
        ).strip()
        provisional = build_release_manifest(
            bundle,
            image_digest=selected_image_digest,
            config_sha256=None,
            dependency_lock_sha256=dependency_lock_sha256,
            patch_root=patch_root,
            delivery_mode=delivery_mode,
            node_configs=node_configs,
            emergency_rollback=emergency_rollback,
            dependency_inventory_sha256=inventory_sha256,
            migration_manifest_sha256=selected_migration_sha256,
            release_payload=envelope["release_payload"],
            sha256sums_sha256=envelope["sha256sums_sha256"],
            release_source_manifest_sha256=envelope[
                "release_source_manifest_sha256"
            ],
            systemd_resource_contract_sha256=envelope[
                "systemd_resource_contract_sha256"
            ],
            build_attestation_sha256=sha256_file(
                selected_attestation
            ),
            _allow_unreviewed_subject=True,
        )
        if preview_review_subject:
            return {
                "review_subject_sha256": provisional[
                    "review_subject_sha256"
                ],
                "build_attestation_sha256": sha256_file(
                    selected_attestation
                ),
                "source_commit": str(bundle["repo_commit"]),
            }
        reviewer_proof = None
        try:
            reviewer_proof = validate_reviewer_trust_proof(
                selected_proof_path,
                pinned_sha256=pinned_proof_sha256,
                expected_subject_sha256=provisional[
                    "review_subject_sha256"
                ],
                expected_source_commit=str(bundle["repo_commit"]),
                expected_build_attestation_sha256=sha256_file(
                    selected_attestation
                ),
            )
        except ReleaseManifestError as exc:
            print(
                f"WARNING: reviewer trust proof ignored: {exc}",
                file=sys.stderr,
            )
    else:
        _, selected_image_digest = _runtime_image_identity(containers)
    manifest = build_release_manifest(
        bundle,
        image_digest=selected_image_digest,
        config_sha256=None,
        dependency_lock_sha256=dependency_lock_sha256,
        patch_root=patch_root,
        delivery_mode=delivery_mode,
        node_configs=node_configs,
        emergency_rollback=emergency_rollback,
        dependency_inventory_sha256=inventory_sha256,
        migration_manifest_sha256=selected_migration_sha256,
        release_payload=envelope.get("release_payload"),
        sha256sums_sha256=envelope.get("sha256sums_sha256"),
        release_source_manifest_sha256=envelope.get(
            "release_source_manifest_sha256"
        ),
        systemd_resource_contract_sha256=envelope.get(
            "systemd_resource_contract_sha256"
        ),
        build_attestation_sha256=(
            sha256_file(
                build_attestation_path
                or release_root / BUILD_ATTESTATION_NAME
            )
            if delivery_mode == DELIVERY_IMMUTABLE
            else None
        ),
        reviewer_trust_proof=reviewer_proof,
        _allow_unreviewed_subject=True,
    )
    output_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def mountinfo_has_deleted_source(text: str) -> bool:
    for line in text.splitlines():
        for token in line.split():
            if "//deleted" in token:
                return True
            if "(deleted)" in token:
                return True
            if "\\040(deleted)" in token:
                return True
    return False


def _read_mountinfo(pid: int) -> list[tuple[str, str]]:
    path = Path(f"/proc/{pid}/mountinfo")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ReleaseManifestError(
            f"cannot read mountinfo for pid={pid}"
        ) from exc
    if mountinfo_has_deleted_source(text):
        raise ReleaseManifestError(
            f"pid={pid} has a deleted bind mount"
        )
    pairs = []
    for line in text.splitlines():
        left, separator, _ = line.partition(" - ")
        if not separator:
            continue
        fields = left.split()
        if len(fields) < 5:
            continue
        source = fields[3].replace("\\040", " ")
        destination = fields[4].replace("\\040", " ")
        pairs.append((source, destination))
    return pairs


def _container_sha256(container: str, path: str) -> str:
    try:
        output = subprocess.check_output(
            ["docker", "exec", container, "sha256sum", path],
            text=True,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseManifestError(
            f"{container}: cannot hash target {path}"
        ) from exc
    fields = output.split()
    if not fields:
        raise ReleaseManifestError(
            f"{container}: empty sha256 output for {path}"
        )
    return _require_sha256(fields[0], "container file sha256")


def _container_dependency_inventory_sha256(
    container: str,
) -> str:
    command = [
        "docker",
        "exec",
        container,
        *dependency_inventory_verifier_command(
            IMMUTABLE_DEPENDENCY_INVENTORY_TARGET
        ),
    ]
    try:
        output = subprocess.check_output(
            command,
            text=True,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseManifestError(
            f"{container}: dependency inventory verification failed"
        ) from exc
    return _require_sha256(
        output.strip(),
        "container dependency inventory sha256",
    )


def _mount_covers_target(destination: str, target: str) -> bool:
    normalized = destination.rstrip("/")
    if not normalized:
        return False
    if target == normalized:
        return True
    return target.startswith(f"{normalized}/")


def _paths_overlap(first: str, second: str) -> bool:
    normalized_first = first.rstrip("/") or "/"
    normalized_second = second.rstrip("/") or "/"
    if normalized_first == normalized_second:
        return True
    return (
        normalized_first.startswith(f"{normalized_second}/")
        or normalized_second.startswith(f"{normalized_first}/")
    )


def _path_is_within_root(path: str, root: str) -> bool:
    normalized_path = path.rstrip("/") or "/"
    normalized_root = root.rstrip("/") or "/"
    return (
        normalized_path == normalized_root
        or normalized_path.startswith(f"{normalized_root}/")
    )


def _verify_immutable_code_mounts(
    *,
    container: str,
    inspected: dict[str, Any],
    mount_pairs: list[tuple[str, str]],
) -> None:
    mounts = inspected.get("Mounts")
    if not isinstance(mounts, list):
        raise ReleaseManifestError(
            f"{container}: docker inspect Mounts is invalid"
        )
    inspected_destinations = []
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        inspected_destinations.append(str(mount.get("Destination", "")))
    for destination in inspected_destinations:
        if not destination:
            continue
        for code_root in IMMUTABLE_CODE_ROOTS:
            if _paths_overlap(destination, code_root):
                raise ReleaseManifestError(
                    f"{container}: immutable code root is bind-mounted: "
                    f"{destination}"
                )
    for _, destination in mount_pairs:
        for code_root in IMMUTABLE_CODE_ROOTS:
            if _path_is_within_root(destination, code_root):
                raise ReleaseManifestError(
                    f"{container}: immutable code root appears in mountinfo: "
                    f"{destination}"
                )


def _verify_transition_file(
    *,
    container: str,
    inspected: dict[str, Any],
    mount_pairs: list[tuple[str, str]],
    file_entry: dict[str, Any],
) -> str:
    host_path = file_entry["host_path"]
    mount_target = file_entry["mount_target"]
    expected_hash = file_entry["sha256"]
    actual_host_hash = sha256_file(Path(host_path))
    if actual_host_hash != expected_hash:
        raise ReleaseManifestError(
            f"host source hash mismatch for {host_path}"
        )
    mount = _find_mount(inspected, mount_target)
    if mount.get("Source") != host_path:
        raise ReleaseManifestError(
            f"{container}: mount source mismatch for {mount_target}"
        )
    if mount_pairs.count((host_path, mount_target)) != 1:
        raise ReleaseManifestError(
            f"{container}: mountinfo mismatch for {mount_target}"
        )
    actual_hash = _container_sha256(container, mount_target)
    if actual_hash != expected_hash:
        raise ReleaseManifestError(
            f"{container}: source hash mismatch for {mount_target}"
        )
    return actual_hash


def _verify_immutable_file(
    *,
    container: str,
    inspected: dict[str, Any],
    mount_pairs: list[tuple[str, str]],
    file_entry: dict[str, Any],
) -> str:
    host_path = file_entry["host_path"]
    mount_target = file_entry["mount_target"]
    expected_hash = file_entry["sha256"]
    actual_bundle_hash = sha256_file(Path(host_path))
    if actual_bundle_hash != expected_hash:
        raise ReleaseManifestError(
            f"bundle payload hash mismatch for {host_path}"
        )

    mounts = inspected.get("Mounts")
    if not isinstance(mounts, list):
        raise ReleaseManifestError(
            f"{container}: docker inspect Mounts is invalid"
        )
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        destination = str(mount.get("Destination", ""))
        if _mount_covers_target(destination, mount_target):
            raise ReleaseManifestError(
                f"{container}: immutable target is mount-covered: "
                f"{mount_target}"
            )
    for _, destination in mount_pairs:
        if _mount_covers_target(destination, mount_target):
            raise ReleaseManifestError(
                f"{container}: immutable target appears in mountinfo: "
                f"{mount_target}"
            )

    actual_hash = _container_sha256(container, mount_target)
    if actual_hash != expected_hash:
        raise ReleaseManifestError(
            f"{container}: immutable target hash mismatch for {mount_target}"
        )
    return actual_hash


def _stat_path(path: Path, label: str) -> os.stat_result:
    try:
        return path.stat()
    except OSError as exc:
        raise ReleaseManifestError(f"cannot stat {label}: {path}") from exc


def _verify_live_node_config(
    *,
    container: str,
    inspected: dict[str, Any],
    pid: int,
    mount_pairs: list[tuple[str, str]],
    config_entry: dict[str, Any],
) -> dict[str, Any]:
    host_path = config_entry["host_path"]
    mount_target = config_entry["mount_target"]
    expected_hash = config_entry["sha256"]
    mount = _find_mount(inspected, mount_target)
    if mount.get("Source") != host_path:
        raise ReleaseManifestError(
            f"{container}: config artifact mount source mismatch"
        )
    if mount.get("RW") is not False:
        raise ReleaseManifestError(
            f"{container}: config artifact mount must be read-only"
        )
    if mount_pairs.count((host_path, mount_target)) != 1:
        raise ReleaseManifestError(
            f"{container}: config artifact mountinfo mismatch"
        )

    artifact_path = Path(host_path)
    payload, config, host_stat = _read_node_config(artifact_path)
    host_hash = hashlib.sha256(payload).hexdigest()
    if host_hash != expected_hash:
        raise ReleaseManifestError(
            f"{container}: host config artifact hash differs from manifest"
        )
    if host_stat.st_size != config_entry["size"]:
        raise ReleaseManifestError(
            f"{container}: host config artifact size differs from manifest"
        )
    if config.get("account_id") != config_entry["account_id"]:
        raise ReleaseManifestError(
            f"{container}: host config artifact account mismatch"
        )
    normalized_hash = node_config_value_sha256(config)
    if normalized_hash != config_entry["normalized_sha256"]:
        raise ReleaseManifestError(
            f"{container}: host config normalized hash differs from manifest"
        )
    control_plane = config.get("control_plane")
    legacy_session = None
    if isinstance(control_plane, dict):
        legacy_session = control_plane.get("session")
    runtime_resources = _validated_runtime_resources(
        config.get("runtime_resources"),
        label=f"{container} runtime_resources",
        legacy_control_plane_session=legacy_session,
    )
    resources_sha256 = runtime_resources_sha256(runtime_resources)
    if runtime_resources != config_entry["runtime_resources"]:
        raise ReleaseManifestError(
            f"{container}: runtime resource config differs from manifest"
        )
    if resources_sha256 != config_entry["runtime_resources_sha256"]:
        raise ReleaseManifestError(
            f"{container}: runtime resource hash differs from manifest"
        )

    process_path = Path(f"/proc/{pid}/root{mount_target}")
    process_stat = _stat_path(process_path, "container config artifact")
    host_inode = (host_stat.st_dev, host_stat.st_ino)
    process_inode = (process_stat.st_dev, process_stat.st_ino)
    if process_inode != host_inode:
        raise ReleaseManifestError(
            f"{container}: config artifact inode differs from host source"
        )
    container_hash = _container_sha256(container, mount_target)
    if container_hash != expected_hash:
        raise ReleaseManifestError(
            f"{container}: config artifact bytes differ from manifest"
        )
    if artifact_path.is_symlink():
        raise ReleaseManifestError(
            f"{container}: config artifact path became a symlink"
        )
    final_host_stat = _stat_path(
        artifact_path,
        "host config artifact",
    )
    final_host_inode = (final_host_stat.st_dev, final_host_stat.st_ino)
    if final_host_inode != host_inode:
        raise ReleaseManifestError(
            f"{container}: config artifact host source was replaced"
        )
    return {
        "account_id": config_entry["account_id"],
        "host_path": host_path,
        "sha256": container_hash,
        "normalized_sha256": normalized_hash,
        "runtime_resources_sha256": resources_sha256,
        "device": host_stat.st_dev,
        "inode": host_stat.st_ino,
    }


def verify_live_release(
    manifest: dict[str, Any],
    containers: list[str],
    *,
    payload_root: Path | None = None,
    reviewer_trust_sha256: str | None = None,
) -> list[dict[str, Any]]:
    manifest = validate_release_manifest(manifest)
    strict_envelope = None
    node_resource_contract = None
    if manifest["release_purpose"] == RELEASE_PURPOSE_HARDENING:
        if payload_root is None:
            raise ReleaseManifestError(
                "hardening live verification requires release payload root"
            )
        pinned_reviewer = str(
            reviewer_trust_sha256
            or os.environ.get(
                "TRADER_RELEASE_REVIEWER_TRUST_SHA256",
                "",
            )
        ).strip()
        strict_envelope = validate_strict_release_envelope(
            manifest,
            payload_root=payload_root,
            reviewer_trust_sha256=pinned_reviewer,
            verify_image_labels=True,
        )
        node_resource_contract = docker_resource_contract(
            strict_envelope["systemd_resource_contract"],
            NODE_DOCKER_RESOURCE_ARTIFACT,
        )
    node_config_by_container = {}
    if manifest["schema_version"] in NODE_CONFIG_SCHEMA_VERSIONS:
        inspected_nodes, image_digest = _runtime_image_identity(containers)
        node_config_by_container = {
            item["container"]: item
            for item in manifest["node_configs"]
        }
        missing_config_entries = [
            container
            for container in containers
            if container not in node_config_by_container
        ]
        if missing_config_entries:
            raise ReleaseManifestError(
                "release manifest lacks selected node configs: "
                f"{missing_config_entries}"
            )
        config_sha256 = manifest["config_sha256"]
    else:
        inspected_nodes, image_digest, config_sha256 = _runtime_identity(
            containers
        )
    if image_digest != manifest["image_digest"]:
        raise ReleaseManifestError("live image digest differs from manifest")
    if config_sha256 != manifest["config_sha256"]:
        raise ReleaseManifestError("live config hash differs from manifest")

    results = []
    expected_labels = {
        LABEL_RELEASE_COMMIT: manifest["release_commit"],
        LABEL_RELEASE_CONFIG: manifest["config_sha256"],
        LABEL_RELEASE_DELIVERY: manifest["delivery_mode"],
        LABEL_RELEASE_ID: manifest["release_id"],
        LABEL_RELEASE_IMAGE: manifest["image_digest"],
    }
    if strict_envelope is not None:
        expected_labels.update(
            strict_envelope["build_attestation"]["image_labels"]
        )
    verified_nodes = []
    for container, inspected in zip(containers, inspected_nodes, strict=True):
        config = inspected.get("Config")
        if not isinstance(config, dict):
            raise ReleaseManifestError(
                f"{container}: docker Config is invalid"
            )
        labels = config.get("Labels")
        if not isinstance(labels, dict):
            labels = {}
        for key, expected in expected_labels.items():
            if labels.get(key) != expected:
                raise ReleaseManifestError(
                    f"{container}: release label mismatch: {key}"
                )
        if node_resource_contract is not None:
            _verify_docker_resource_contract(
                inspected,
                expected=node_resource_contract,
                container=container,
            )

        state = inspected.get("State")
        if not isinstance(state, dict):
            raise ReleaseManifestError(
                f"{container}: docker State is invalid"
            )
        pid = state.get("Pid")
        if not isinstance(pid, int) or pid <= 0:
            raise ReleaseManifestError(
                f"{container}: container pid is invalid"
            )
        verified_nodes.append((container, inspected, pid))

    for container, inspected, pid in verified_nodes:
        mount_pairs = _read_mountinfo(pid)
        config_artifact = False
        if node_config_by_container:
            config_artifact = _verify_live_node_config(
                container=container,
                inspected=inspected,
                pid=pid,
                mount_pairs=mount_pairs,
                config_entry=node_config_by_container[container],
            )
        dependency_lock_sha256 = ""
        dependency_inventory_sha256 = ""
        migration_manifest_sha256 = ""
        if manifest["delivery_mode"] == DELIVERY_IMMUTABLE:
            _verify_immutable_code_mounts(
                container=container,
                inspected=inspected,
                mount_pairs=mount_pairs,
            )
            dependency_lock_sha256 = _container_sha256(
                container,
                IMMUTABLE_DEPENDENCY_LOCK_TARGET,
            )
            if dependency_lock_sha256 != manifest["dependency_lock_sha256"]:
                raise ReleaseManifestError(
                    f"{container}: dependency lock hash differs from manifest"
                )
            expected_inventory_sha256 = manifest.get(
                "dependency_inventory_sha256"
            )
            if expected_inventory_sha256 is not None:
                dependency_inventory_sha256 = _container_sha256(
                    container,
                    IMMUTABLE_DEPENDENCY_INVENTORY_TARGET,
                )
                if (
                    dependency_inventory_sha256
                    != expected_inventory_sha256
                ):
                    raise ReleaseManifestError(
                        f"{container}: dependency inventory hash differs "
                        "from manifest"
                    )
                verified_inventory_sha256 = (
                    _container_dependency_inventory_sha256(container)
                )
                if (
                    verified_inventory_sha256
                    != expected_inventory_sha256
                ):
                    raise ReleaseManifestError(
                        f"{container}: installed package inventory differs "
                        "from release lock"
                    )
            expected_migration_sha256 = manifest.get(
                "migration_manifest_sha256"
            )
            if expected_migration_sha256 is not None:
                migration_manifest_sha256 = _container_sha256(
                    container,
                    IMMUTABLE_MIGRATION_MANIFEST_TARGET,
                )
                if (
                    migration_manifest_sha256
                    != expected_migration_sha256
                ):
                    raise ReleaseManifestError(
                        f"{container}: migration manifest hash differs "
                        "from release manifest"
                    )
            if strict_envelope is not None:
                if not dependency_inventory_sha256:
                    raise ReleaseManifestError(
                        f"{container}: strict dependency inventory missing"
                    )
                if not migration_manifest_sha256:
                    raise ReleaseManifestError(
                        f"{container}: strict migration manifest missing"
                    )
        file_hashes = {}
        for file_entry in manifest["files"]:
            mount_target = file_entry["mount_target"]
            actual_hash = ""
            if manifest["delivery_mode"] == DELIVERY_IMMUTABLE:
                actual_hash = _verify_immutable_file(
                    container=container,
                    inspected=inspected,
                    mount_pairs=mount_pairs,
                    file_entry=file_entry,
                )
            else:
                actual_hash = _verify_transition_file(
                    container=container,
                    inspected=inspected,
                    mount_pairs=mount_pairs,
                    file_entry=file_entry,
                )
            file_hashes[mount_target] = actual_hash
        results.append(
            {
                "container": container,
                "image_digest": image_digest,
                "config_sha256": config_sha256,
                "delivery_mode": manifest["delivery_mode"],
                "release_commit": manifest["release_commit"],
                "release_id": manifest["release_id"],
                "dependency_lock_sha256": dependency_lock_sha256,
                "dependency_inventory_sha256": (
                    dependency_inventory_sha256
                ),
                "migration_manifest_sha256": (
                    migration_manifest_sha256
                ),
                "build_attestation_sha256": manifest.get(
                    "build_attestation_sha256",
                    "",
                ),
                "config_artifact": config_artifact,
                "source_hashes": file_hashes,
            }
        )

    reference_hashes = results[0]["source_hashes"]
    for result in results[1:]:
        if result["source_hashes"] != reference_hashes:
            raise ReleaseManifestError(
                "node mounted source byte hashes differ"
            )
    return results


def _print_identity(manifest: dict[str, Any], containers: list[str]) -> None:
    config_artifact_count = 0
    node_configs = manifest.get("node_configs")
    if isinstance(node_configs, list):
        config_artifact_count = len(node_configs)
    print(
        "release identity:"
        f" commit={manifest['release_commit']}"
        f" release_id={manifest['release_id']}"
        f" delivery={manifest['delivery_mode']}"
        f" purpose={manifest['release_purpose']}"
        f" image={manifest['image_digest']}"
        f" config={manifest['config_sha256']}"
        f" config_artifacts={config_artifact_count}"
        f" nodes={','.join(containers)}"
    )


def _parse_node_config_specs(
    values: list[str] | None,
) -> dict[str, tuple[str, Path]] | None:
    if values is None:
        return None
    specs = {}
    for raw in values:
        parts = raw.split("=", 2)
        if len(parts) != 3:
            raise ReleaseManifestError(
                f"invalid --node-config value: {raw}"
            )
        account_id, container, path = parts
        if account_id in specs or not container or not path:
            raise ReleaseManifestError(
                f"invalid --node-config value: {raw}"
            )
        specs[account_id] = (container, Path(path))
    if not specs or not set(specs).issubset(REQUIRED_ACCOUNTS):
        raise ReleaseManifestError(
            "--node-config values must be a non-empty supported account collection"
        )
    return specs


def _parse_containers(
    values: list[str] | None,
    *,
    exact_count: int | None = None,
) -> list[str]:
    containers = list(values or DEFAULT_CONTAINERS)
    if len(containers) not in {1, 2, len(DEFAULT_CONTAINERS)} or len(
        set(containers)
    ) != len(containers):
        raise ReleaseManifestError(
            "one, two, or four distinct containers are required"
        )
    if exact_count is not None and len(containers) != exact_count:
        raise ReleaseManifestError(
            f"exactly {exact_count} distinct containers are required"
        )
    for container in containers:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", container):
            raise ReleaseManifestError(
                f"invalid container name: {container}"
            )
    return containers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and verify trader-v3 release identity."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture_parser = subparsers.add_parser(
        "capture",
        help="capture reviewed bundle plus current four-node image/config identity",
    )
    capture_parser.add_argument("--bundle-manifest", type=Path, required=True)
    capture_parser.add_argument("--dependency-lock", type=Path, required=True)
    capture_parser.add_argument("--migration-manifest", type=Path)
    capture_parser.add_argument("--build-attestation", type=Path)
    capture_parser.add_argument("--reviewer-trust-proof", type=Path)
    capture_parser.add_argument("--reviewer-trust-sha256")
    capture_parser.add_argument("--output", type=Path)
    capture_parser.add_argument(
        "--preview-review-subject",
        action="store_true",
        help=(
            "Print the provisional review subject, build attestation "
            "hash and source commit for reviewer trust proof signing, "
            "without validating a proof or writing a manifest."
        ),
    )
    capture_parser.add_argument(
        "--patch-root",
        type=Path,
        default=DEFAULT_PATCH_ROOT,
    )
    capture_parser.add_argument(
        "--container",
        action="append",
        dest="containers",
    )
    capture_parser.add_argument(
        "--require-transition-runtime",
        action="store_true",
    )
    capture_parser.add_argument(
        "--delivery-mode",
        choices=DELIVERY_MODES,
        default=DELIVERY_IMMUTABLE,
    )
    capture_parser.add_argument(
        "--emergency-rollback",
        action="store_true",
    )
    capture_parser.add_argument(
        "--image-digest",
        help="explicit target image content digest",
    )
    capture_parser.add_argument(
        "--node-config",
        action="append",
        dest="node_configs",
        required=True,
        metavar="ACCOUNT=CONTAINER=PATH",
        help=(
            "bind an immutable content-addressed config artifact to one node"
        ),
    )

    validate_parser = subparsers.add_parser(
        "validate-bundle",
        help="validate bundle files, hashes and required transition mounts",
    )
    validate_parser.add_argument("--bundle-manifest", type=Path, required=True)
    validate_parser.add_argument(
        "--patch-root",
        type=Path,
        default=DEFAULT_PATCH_ROOT,
    )
    validate_parser.add_argument(
        "--require-transition-runtime",
        action="store_true",
    )

    augment_parser = subparsers.add_parser(
        "augment-transition-bundle",
        help="add changed persistence/data-client runtime files to a bundle",
    )
    augment_parser.add_argument("--bundle-manifest", type=Path, required=True)
    augment_parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )

    verify_parser = subparsers.add_parser(
        "verify-live",
        help="verify both live nodes against a release manifest",
    )
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.add_argument("--payload-root", type=Path)
    verify_parser.add_argument("--reviewer-trust-sha256")
    verify_parser.add_argument(
        "--container",
        action="append",
        dest="containers",
    )
    verify_parser.add_argument("--bundle-manifest", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "validate-bundle":
            files = validate_bundle_payload(
                args.bundle_manifest,
                args.patch_root,
                require_transition_runtime=args.require_transition_runtime,
            )
            print(f"PASS: bundle files={len(files)}")
            return 0
        if args.command == "augment-transition-bundle":
            bundle = augment_transition_bundle(
                args.bundle_manifest,
                args.repo_root.resolve(),
            )
            print(
                "WROTE transition runtime bundle:"
                f" {args.bundle_manifest} files={len(bundle['files'])}"
            )
            return 0
        if args.command == "capture":
            containers = _parse_containers(
                args.containers,
                exact_count=len(DEFAULT_CONTAINERS),
            )
            if args.output is None and not args.preview_review_subject:
                capture_parser.error(
                    "--output is required unless previewing the "
                    "review subject"
                )
            manifest = capture_release_manifest(
                bundle_path=args.bundle_manifest,
                dependency_lock_path=args.dependency_lock,
                output_path=args.output,
                patch_root=args.patch_root,
                containers=containers,
                require_transition_runtime=args.require_transition_runtime,
                delivery_mode=args.delivery_mode,
                image_digest=args.image_digest,
                node_config_specs=_parse_node_config_specs(
                    args.node_configs
                ),
                emergency_rollback=args.emergency_rollback,
                migration_manifest_path=args.migration_manifest,
                build_attestation_path=args.build_attestation,
                reviewer_trust_proof_path=args.reviewer_trust_proof,
                reviewer_trust_sha256=args.reviewer_trust_sha256,
                preview_review_subject=args.preview_review_subject,
            )
            if args.preview_review_subject:
                print(
                    "PREVIEW"
                    " review_subject_sha256="
                    f"{manifest['review_subject_sha256']}"
                    " build_attestation_sha256="
                    f"{manifest['build_attestation_sha256']}"
                    f" source_commit={manifest['source_commit']}"
                )
                return 0
            print(f"WROTE {args.output}")
            _print_identity(manifest, containers)
            return 0
        containers = _parse_containers(args.containers)
        manifest = _load_json(args.manifest)
        if args.bundle_manifest is not None:
            require_release_matches_bundle(
                manifest,
                args.bundle_manifest,
            )
        payload_root = args.payload_root
        if payload_root is None:
            payload_root = args.manifest.parent
        results = verify_live_release(
            manifest,
            containers,
            payload_root=payload_root,
            reviewer_trust_sha256=args.reviewer_trust_sha256,
        )
        _print_identity(manifest, containers)
        print(
            "PASS: image, per-node config, release labels, "
            f"delivery contract and {len(results[0]['source_hashes'])} "
            "target hashes match"
        )
        return 0
    except (OSError, ReleaseManifestError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
