from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "make_account_stall_release.py"
SPEC = importlib.util.spec_from_file_location(
    "make_account_stall_release",
    SCRIPT,
)
assert SPEC is not None
assert SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(REPO_ROOT / "scripts"))
SPEC.loader.exec_module(release)
import make_container_bundle as container_bundle
import release_manifest as release_manifest_contract

EXPECTED_REDIS_SCHEMA_EPOCH = "fenced-generation-namespace/v2"
EXECUTOR_SOURCE_PATH = "scripts/account_a_live_trade_executor.py"
EXECUTOR_RELEASE_PATH = "account_a_live_trade_executor.py"
ADAPTER_SOURCE_PATH = "scripts/account_a_live_trade_http_adapter.py"
ADAPTER_RELEASE_PATH = "account_a_live_trade_http_adapter.py"
WATCHER_BUILDER_SOURCE_PATH = (
    "scripts/build_immutable_watcher_image.py"
)
WATCHER_BUILDER_RELEASE_PATH = "build_immutable_watcher_image.py"
HERMES_FEEDER_SOURCE_PATH = "scripts/hermes_signal_feeder.py"
HERMES_FEEDER_RELEASE_PATH = "host/hermes_signal_feeder.py"
HERMES_FEEDER_REQUIRED_SHA256 = (
    "4bd121f9e34ca887568b00d2331ca7299d36941702e244751529e4384e3ee7b4"
)
V3_TRADE_SOURCE_PATH = (
    "hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py"
)
V3_TRADE_RELEASE_PATH = "host/v3_trade.py"
V3_SKILL_SOURCE_PATH = "hermes-profile/skills/trading/v3-trader/SKILL.md"
V3_SKILL_RELEASE_PATH = "host/v3-trader/SKILL.md"
EXCHANGE_STATE_RECORDER_SOURCE_PATH = (
    "services/control-plane/tools/exchange_state_recorder.py"
)
EXCHANGE_STATE_RECORDER_RELEASE_PATH = (
    "host/exchange_state_recorder.py"
)
CONTROL_PLANE_BOOTSTRAP_SOURCE_PATH = (
    "scripts/bootstrap_control_plane_roles.py"
)
CONTROL_PLANE_BOOTSTRAP_RELEASE_PATH = (
    "bootstrap_control_plane_roles.py"
)
REQUIRED_RELEASE_PATHS = {
    EXECUTOR_RELEASE_PATH,
    ADAPTER_RELEASE_PATH,
    WATCHER_BUILDER_RELEASE_PATH,
    HERMES_FEEDER_RELEASE_PATH,
    V3_TRADE_RELEASE_PATH,
    V3_SKILL_RELEASE_PATH,
    EXCHANGE_STATE_RECORDER_RELEASE_PATH,
    "redis_namespace_janitor.py",
    "redis_namespace_registry.py",
    "reviewed_release_rollout.py",
    CONTROL_PLANE_BOOTSTRAP_RELEASE_PATH,
}
REQUIRED_REDIS_RELEASE_PATHS = {
    "redis_namespace_janitor.py",
    "redis_namespace_registry.py",
}
REQUIRED_HERMES_RELEASE_PATHS = {
    HERMES_FEEDER_RELEASE_PATH,
    V3_TRADE_RELEASE_PATH,
    V3_SKILL_RELEASE_PATH,
}
REQUIRED_CONTROL_PLANE_HOST_RELEASE_PATHS = {
    "host/read_api.py",
    "host/snapshot.py",
    "host/decision_gateway/gateway.py",
    EXCHANGE_STATE_RECORDER_RELEASE_PATH,
}
REQUIRED_BUNDLE_PATHS = {
    "run_node.py",
    "bounded_task_worker.py",
    "control_plane_session.py",
    "redis_safety.py",
    "routing_init.py",
    "routing_multi_account.py",
    "redis_namespace_lease.py",
    "redis_resp_client.py",
}
REQUIRED_HOST_PATHS = {
    "host/read_api.py",
    "host/snapshot.py",
    "host/decision_gateway/gateway.py",
    EXCHANGE_STATE_RECORDER_RELEASE_PATH,
    "host/system_snapshot.v1.json",
    "host/execution_domain/__init__.py",
    "host/execution_domain/contracts.py",
    "host/execution_domain/control_plane.py",
    "host/execution_domain/portfolio_baseline.py",
    "host/execution_domain/idempotency.py",
    "host/execution_domain/identifiers.py",
    "host/settings/__init__.py",
    "host/settings/apply_plan.py",
    "host/settings/import_export.py",
    "host/settings/permissions.py",
    "host/settings/publisher.py",
    "host/settings/resolver.py",
    "host/settings/router.py",
    "host/settings/schema.py",
    "host/settings/service.py",
    "host/settings/versioning.py",
    "host/audit/__init__.py",
    "host/audit/settings_audit.py",
    "host/order_management/__init__.py",
    "host/order_management/db_helpers.py",
    "host/order_management/identifiers.py",
    "host/order_management/metrics.py",
    "host/order_management/outbox.py",
    "host/observability/__init__.py",
    "host/observability/_shared.py",
    "host/observability/logs.py",
    "host/observability/metrics.py",
    "host/observability/tracing.py",
    "host/app_roles.py",
    "host/pools.py",
}
EXPECTED_RELEASE_FILE_MAP = {
    "scripts/hk-deploy-20260803.sh": "hk-deploy-20260803.sh",
    "scripts/hk-gen-recreate-patched.py": "hk-gen-recreate-patched.py",
    "scripts/release_manifest.py": "release_manifest.py",
    "scripts/reviewed_release_rollout.py": "reviewed_release_rollout.py",
    "scripts/live_node_config.py": "live_node_config.py",
    (
        "services/nautilus-node/config/live-risk-policy.json"
    ): "live-risk-policy.json",
    (
        "scripts/build_immutable_node_image.py"
    ): "build_immutable_node_image.py",
    WATCHER_BUILDER_SOURCE_PATH: WATCHER_BUILDER_RELEASE_PATH,
    HERMES_FEEDER_SOURCE_PATH: HERMES_FEEDER_RELEASE_PATH,
    V3_TRADE_SOURCE_PATH: V3_TRADE_RELEASE_PATH,
    V3_SKILL_SOURCE_PATH: V3_SKILL_RELEASE_PATH,
    "scripts/hk-redis-rebaseline.sh": "hk-redis-rebaseline.sh",
    (
        "scripts/hk-control-plane-isolation.sh"
    ): "hk-control-plane-isolation.sh",
    (
        CONTROL_PLANE_BOOTSTRAP_SOURCE_PATH
    ): CONTROL_PLANE_BOOTSTRAP_RELEASE_PATH,
    EXECUTOR_SOURCE_PATH: EXECUTOR_RELEASE_PATH,
    ADAPTER_SOURCE_PATH: ADAPTER_RELEASE_PATH,
    "scripts/redis_capacity_config.py": "redis_capacity_config.py",
    (
        "scripts/refresh_redis_capacity_evidence.py"
    ): "refresh_redis_capacity_evidence.py",
    "scripts/redis_namespace_janitor.py": "redis_namespace_janitor.py",
    "scripts/redis_namespace_registry.py": "redis_namespace_registry.py",
    "infra/docker/nautilus/uv.node.lock": "uv.node.lock",
    "services/control-plane/api/read_api.py": "host/read_api.py",
    "services/control-plane/api/snapshot.py": "host/snapshot.py",
    (
        "services/control-plane/decision_gateway/gateway.py"
    ): "host/decision_gateway/gateway.py",
    (
        EXCHANGE_STATE_RECORDER_SOURCE_PATH
    ): EXCHANGE_STATE_RECORDER_RELEASE_PATH,
    (
        "packages/contracts/v1/system_snapshot.v1.json"
    ): "host/system_snapshot.v1.json",
    (
        "packages/execution-domain/execution_domain/__init__.py"
    ): "host/execution_domain/__init__.py",
    (
        "packages/execution-domain/execution_domain/contracts.py"
    ): "host/execution_domain/contracts.py",
    (
        "packages/execution-domain/execution_domain/control_plane.py"
    ): "host/execution_domain/control_plane.py",
    (
        "packages/execution-domain/execution_domain/portfolio_baseline.py"
    ): "host/execution_domain/portfolio_baseline.py",
    (
        "packages/execution-domain/execution_domain/idempotency.py"
    ): "host/execution_domain/idempotency.py",
    (
        "packages/execution-domain/execution_domain/identifiers.py"
    ): "host/execution_domain/identifiers.py",
    (
        "services/control-plane/settings/__init__.py"
    ): "host/settings/__init__.py",
    (
        "services/control-plane/settings/apply_plan.py"
    ): "host/settings/apply_plan.py",
    (
        "services/control-plane/settings/import_export.py"
    ): "host/settings/import_export.py",
    (
        "services/control-plane/settings/permissions.py"
    ): "host/settings/permissions.py",
    (
        "services/control-plane/settings/publisher.py"
    ): "host/settings/publisher.py",
    (
        "services/control-plane/settings/resolver.py"
    ): "host/settings/resolver.py",
    (
        "services/control-plane/settings/router.py"
    ): "host/settings/router.py",
    (
        "services/control-plane/settings/schema.py"
    ): "host/settings/schema.py",
    (
        "services/control-plane/settings/service.py"
    ): "host/settings/service.py",
    (
        "services/control-plane/settings/versioning.py"
    ): "host/settings/versioning.py",
    (
        "services/control-plane/audit/__init__.py"
    ): "host/audit/__init__.py",
    (
        "services/control-plane/audit/settings_audit.py"
    ): "host/audit/settings_audit.py",
    (
        "services/control-plane/order_management/__init__.py"
    ): "host/order_management/__init__.py",
    (
        "services/control-plane/order_management/db_helpers.py"
    ): "host/order_management/db_helpers.py",
    (
        "services/control-plane/order_management/identifiers.py"
    ): "host/order_management/identifiers.py",
    (
        "services/control-plane/order_management/metrics.py"
    ): "host/order_management/metrics.py",
    (
        "services/control-plane/order_management/outbox.py"
    ): "host/order_management/outbox.py",
    (
        "services/nautilus-node/observability/__init__.py"
    ): "host/observability/__init__.py",
    (
        "services/nautilus-node/observability/_shared.py"
    ): "host/observability/_shared.py",
    (
        "services/nautilus-node/observability/logs.py"
    ): "host/observability/logs.py",
    (
        "services/nautilus-node/observability/metrics.py"
    ): "host/observability/metrics.py",
    (
        "services/nautilus-node/observability/tracing.py"
    ): "host/observability/tracing.py",
    "services/control-plane/api/app_roles.py": "host/app_roles.py",
    "services/control-plane/db/pools.py": "host/pools.py",
    (
        "services/control-plane/db/migrate.py"
    ): "services/control-plane/db/migrate.py",
    **{
        source_path: release_path
        for source_path, release_path, _target_path
        in release.WATCHER_RELEASE_FILES
    },
    **{path: path for path in release.SYSTEMD_RESOURCE_FILES},
}


_FIXTURE_LOCK_CONTENT = """
version = 1
revision = 1
requires-python = "==3.12.*"

[[package]]
name = "nautilus-node-runtime"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "runtime-demo" },
]

[[package]]
name = "runtime-demo"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://example.invalid/runtime-demo-1.2.3.tar.gz", hash = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", size = 1 }
""".strip() + "\n"


def _seed_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "codex@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Codex"],
        cwd=root,
        check=True,
    )
    for _name, source_relative, _target in container_bundle.BUNDLE_FILES:
        path = root / source_relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"source={source_relative}\n", encoding="utf-8")
    for source_relative, _destination_relative in release.RELEASE_FILES:
        path = root / source_relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if source_relative == "infra/docker/nautilus/uv.node.lock":
            path.write_text(_FIXTURE_LOCK_CONTENT, encoding="utf-8")
        elif source_relative in {
            *release.SYSTEMD_RESOURCE_FILES,
            "scripts/release_manifest.py",
            HERMES_FEEDER_SOURCE_PATH,
            "services/nautilus-node/config/live-risk-policy.json",
        }:
            source = REPO_ROOT / source_relative
            path.write_bytes(source.read_bytes())
        else:
            path.write_text(
                f"release={source_relative}\n",
                encoding="utf-8",
            )
        if source_relative in {
            EXECUTOR_SOURCE_PATH,
            ADAPTER_SOURCE_PATH,
        }:
            path.chmod(0o755)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)


def _commit_path(root: Path, path: Path, message: str) -> None:
    subprocess.run(["git", "add", str(path)], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-qm", message],
        cwd=root,
        check=True,
    )


def test_release_builder_includes_refresh_evidence_command_migration() -> None:
    assert release.SCHEMA_EPOCHS["db"] == "0015_refresh_evidence_command"
    assert release.MIGRATION_FILES[-4:] == (
        release.MIGRATION_CANCEL_ORDER_CONTRACT_UP,
        release.MIGRATION_CANCEL_ORDER_CONTRACT_DOWN,
        release.MIGRATION_REFRESH_EVIDENCE_COMMAND_UP,
        release.MIGRATION_REFRESH_EVIDENCE_COMMAND_DOWN,
    )
    assert release.MIGRATION_STEPS[-1] == {
        "version": "0015",
        "name": "refresh_evidence_command",
        "up": release.MIGRATION_REFRESH_EVIDENCE_COMMAND_UP,
        "down": release.MIGRATION_REFRESH_EVIDENCE_COMMAND_DOWN,
        "prerequisites": [release.MIGRATION_CANCEL_ORDER_CONTRACT_UP],
    }


def test_applied_maintenance_fence_migration_is_immutable() -> None:
    migration_path = (
        REPO_ROOT
        / "db"
        / "migrations"
        / "0012_control_plane_maintenance_fence.up.sql"
    )

    assert hashlib.sha256(migration_path.read_bytes()).hexdigest() == (
        "72f076928b58e6ea8df6f809cce49fb69b8c34303a2728255c74aa0b63c9cb38"
    )


def test_reviewed_live_risk_policy_has_complete_runtime_resources() -> None:
    policy_path = (
        REPO_ROOT
        / "services"
        / "nautilus-node"
        / "config"
        / "live-risk-policy.json"
    )
    policy = json.loads(policy_path.read_text(encoding="utf-8"))

    validated = release_manifest_contract._validated_runtime_resources(
        policy["runtime_resource_contract"],
        label="live risk policy runtime_resource_contract",
    )

    assert set(validated) == {
        "schema_version",
        "redis",
        "command_journal",
        "control_plane_session",
        "strategy_durable_io",
        "terminal_exchange",
        "reporter_workers",
    }


def test_release_builder_writes_complete_checksummed_payload(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_repo(repo)
    output = tmp_path / "release"

    checksum_path = release.build_release(repo, output)

    manifest = json.loads(
        (output / "bundle-manifest.json").read_text(encoding="utf-8")
    )
    source_manifest = json.loads(
        (output / release.SOURCE_MANIFEST_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["repo_dirty"] is False
    assert manifest["source_mode"] == "git_object"
    assert manifest["schema_epochs"] == release.SCHEMA_EPOCHS
    assert manifest["schema_epochs"]["redis"] == EXPECTED_REDIS_SCHEMA_EPOCH
    assert source_manifest["source_commit"] == manifest["repo_commit"]
    assert source_manifest["source_tree"] == manifest["repo_tree"]
    assert source_manifest["head_at_start"] == manifest["repo_commit"]
    assert source_manifest["head_at_end"] == manifest["repo_commit"]
    assert source_manifest["schema_epochs"] == release.SCHEMA_EPOCHS
    assert (
        source_manifest["schema_epochs"]["redis"]
        == EXPECTED_REDIS_SCHEMA_EPOCH
    )
    assert "stable-account-namespace/v1" not in json.dumps(manifest)
    assert "stable-account-namespace/v1" not in json.dumps(source_manifest)
    migration_contract = source_manifest["migration"]
    assert migration_contract == {
        "runner": release.MIGRATION_RUNNER,
        "up": release.MIGRATION_UP,
        "down": release.MIGRATION_DOWN,
        "prerequisites": list(release.MIGRATION_PREREQUISITES),
        "python_dependencies": ["psycopg2"],
        "migration_files": list(release.MIGRATION_FILES),
        "steps": [dict(item) for item in release.MIGRATION_STEPS],
        "db_schema_epoch": "0015_refresh_evidence_command",
        "manifest": release.MIGRATION_MANIFEST_NAME,
        "manifest_sha256": hashlib.sha256(
            (output / release.MIGRATION_MANIFEST_NAME).read_bytes()
        ).hexdigest(),
    }
    assert source_manifest["systemd_resource_contract"] == {
        "path": release.SYSTEMD_RESOURCE_CONTRACT_NAME,
        "sha256": hashlib.sha256(
            (
                output / release.SYSTEMD_RESOURCE_CONTRACT_NAME
            ).read_bytes()
        ).hexdigest(),
    }
    assert source_manifest["watcher_runtime"] == {
        "manifest": release.WATCHER_RUNTIME_MANIFEST_NAME,
        "manifest_sha256": hashlib.sha256(
            (
                output / release.WATCHER_RUNTIME_MANIFEST_NAME
            ).read_bytes()
        ).hexdigest(),
    }
    assert checksum_path == output / "SHA256SUMS"
    expected_release_files = {
        **EXPECTED_RELEASE_FILE_MAP,
        **{path: path for path in release.MIGRATION_FILES},
    }
    assert dict(release.RELEASE_FILES) == expected_release_files
    for _source_relative, destination_relative in release.RELEASE_FILES:
        assert (output / destination_relative).is_file()
    assert (output / release.MIGRATION_RUNNER).is_file()
    assert (output / release.MIGRATION_UP).is_file()
    assert (output / release.MIGRATION_DOWN).is_file()
    assert {
        item["release_path"]
        for item in source_manifest["files"]
        if str(item["release_path"]).startswith("db/migrations/")
    } == set(release.MIGRATION_FILES)
    assert REQUIRED_RELEASE_PATHS.issubset(
        {
            item["release_path"]
            for item in source_manifest["files"]
        }
    )
    assert (
        REQUIRED_REDIS_RELEASE_PATHS
        == release.REQUIRED_REDIS_RELEASE_PATHS
    )
    assert REQUIRED_HERMES_RELEASE_PATHS == (
        release.REQUIRED_HERMES_RELEASE_PATHS
    )
    assert release.HERMES_FEEDER_SOURCE_PATH == HERMES_FEEDER_SOURCE_PATH
    assert (
        release.HERMES_FEEDER_REQUIRED_SHA256
        == HERMES_FEEDER_REQUIRED_SHA256
    )
    feeder_payload = (output / HERMES_FEEDER_RELEASE_PATH).read_bytes()
    assert hashlib.sha256(feeder_payload).hexdigest() == (
        HERMES_FEEDER_REQUIRED_SHA256
    )
    assert REQUIRED_CONTROL_PLANE_HOST_RELEASE_PATHS == (
        release.REQUIRED_CONTROL_PLANE_HOST_RELEASE_PATHS
    )
    assert REQUIRED_BUNDLE_PATHS.issubset(
        {
            item["bundle_path"]
            for item in manifest["files"]
        }
    )
    assert REQUIRED_HOST_PATHS.issubset(
        {
            item["release_path"]
            for item in source_manifest["files"]
        }
    )
    migration_manifest = json.loads(
        (output / release.MIGRATION_MANIFEST_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert migration_manifest["schema_version"] == (
        release.MIGRATION_MANIFEST_SCHEMA_VERSION
    )
    assert migration_manifest["schema_epoch"] == "0015_refresh_evidence_command"
    assert {
        item["path"]
        for item in migration_manifest["migrations"]
    } == set(release.MIGRATION_FILES)
    systemd_contract = json.loads(
        (output / release.SYSTEMD_RESOURCE_CONTRACT_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert systemd_contract["schema_version"] == (
        release.SYSTEMD_RESOURCE_CONTRACT_SCHEMA_VERSION
    )
    assert {
        item["artifact"]
        for item in systemd_contract["resources"]
    } == set(release.SYSTEMD_RESOURCE_FILES)
    assert all(
        item["consumer_entrypoint"]
        for item in systemd_contract["resources"]
    )
    assert all(
        item["owner_units"]
        for item in systemd_contract["resources"]
    )
    assert all(
        item["destinations"]
        for item in systemd_contract["resources"]
    )
    node_resource = next(
        item
        for item in systemd_contract["resources"]
        if item["artifact"] == (
            "infra/systemd/account-stall-account-node.conf"
        )
    )
    assert node_resource["owner_units"] == [
        "trader-v3-node-a",
        "trader-v3-node-b",
        "trader-v3-node-c",
        "trader-v3-node-d",
    ]
    assert node_resource["destinations"] == [
        "docker://trader-v3-node-a/HostConfig",
        "docker://trader-v3-node-b/HostConfig",
        "docker://trader-v3-node-c/HostConfig",
        "docker://trader-v3-node-d/HostConfig",
    ]
    watcher_manifest = json.loads(
        (output / release.WATCHER_RUNTIME_MANIFEST_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert watcher_manifest["schema_version"] == (
        release.WATCHER_RUNTIME_MANIFEST_SCHEMA_VERSION
    )
    assert {
        (
            item["source_path"],
            item["release_path"],
            item["target_path"],
        )
        for item in watcher_manifest["files"]
    } == set(release.WATCHER_RELEASE_FILES)
    assert {
        item["release_path"]
        for item in watcher_manifest["files"]
    } == {
        release_path
        for _source_path, release_path, _target_path
        in release.WATCHER_RELEASE_FILES
    }
    expected_payload_paths = {
        container_bundle.MANIFEST_NAME,
        release.SOURCE_MANIFEST_NAME,
        release.MIGRATION_MANIFEST_NAME,
        release.SYSTEMD_RESOURCE_CONTRACT_NAME,
        release.WATCHER_RUNTIME_MANIFEST_NAME,
        "SHA256SUMS",
        *(item[0] for item in container_bundle.BUNDLE_FILES),
        *expected_release_files.values(),
    }
    actual_payload_paths = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert actual_payload_paths == expected_payload_paths
    checksum_entries = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        assert separator == "  "
        checksum_entries[relative] = digest
    assert set(checksum_entries) == expected_payload_paths - {"SHA256SUMS"}
    for relative, digest in checksum_entries.items():
        actual = hashlib.sha256((output / relative).read_bytes()).hexdigest()
        assert digest == actual
    executor_manifest_entries = [
        item
        for item in source_manifest["files"]
        if item["release_path"] == EXECUTOR_RELEASE_PATH
    ]
    assert len(executor_manifest_entries) == 1
    executor_manifest = executor_manifest_entries[0]
    executor_blob = subprocess.run(
        ["git", "rev-parse", f"HEAD:{EXECUTOR_SOURCE_PATH}"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    executor_payload = subprocess.run(
        ["git", "show", f"HEAD:{EXECUTOR_SOURCE_PATH}"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    executor_digest = hashlib.sha256(executor_payload).hexdigest()
    assert executor_manifest == {
        "source_path": EXECUTOR_SOURCE_PATH,
        "release_path": EXECUTOR_RELEASE_PATH,
        "source_git_blob": executor_blob,
        "source_git_mode": "100755",
        "sha256": executor_digest,
        "size": len(executor_payload),
    }
    executor_release = output / EXECUTOR_RELEASE_PATH
    assert executor_release.read_bytes() == executor_payload
    assert executor_release.stat().st_mode & 0o111
    assert checksum_entries[EXECUTOR_RELEASE_PATH] == executor_digest
    adapter_manifest_entries = [
        item
        for item in source_manifest["files"]
        if item["release_path"] == ADAPTER_RELEASE_PATH
    ]
    assert len(adapter_manifest_entries) == 1
    adapter_manifest = adapter_manifest_entries[0]
    adapter_blob = subprocess.run(
        ["git", "rev-parse", f"HEAD:{ADAPTER_SOURCE_PATH}"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    adapter_payload = subprocess.run(
        ["git", "show", f"HEAD:{ADAPTER_SOURCE_PATH}"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    adapter_digest = hashlib.sha256(adapter_payload).hexdigest()
    assert adapter_manifest == {
        "source_path": ADAPTER_SOURCE_PATH,
        "release_path": ADAPTER_RELEASE_PATH,
        "source_git_blob": adapter_blob,
        "source_git_mode": "100755",
        "sha256": adapter_digest,
        "size": len(adapter_payload),
    }
    adapter_release = output / ADAPTER_RELEASE_PATH
    assert adapter_release.read_bytes() == adapter_payload
    assert adapter_release.stat().st_mode & 0o111
    assert checksum_entries[ADAPTER_RELEASE_PATH] == adapter_digest
    verified = subprocess.run(
        ["sha256sum", "-c", "SHA256SUMS"],
        cwd=output,
        text=True,
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0


def test_release_payload_envelope_validates_end_to_end(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_repo(repo)
    output = tmp_path / "release"
    release.build_release(repo, output)

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import release_manifest

    release_manifest.write_dependency_inventory(
        output / release_manifest.RELEASE_DEPENDENCY_LOCK_NAME,
        output / release_manifest.DEPENDENCY_INVENTORY_NAME,
    )
    payload = release_manifest.validate_release_payload_envelope(
        payload_root=output,
    )
    assert payload["source_manifest"][
        "watcher_runtime_manifest_sha256"
    ] == hashlib.sha256(
        (output / release.WATCHER_RUNTIME_MANIFEST_NAME).read_bytes()
    ).hexdigest()
    adapter_entries = [
        item
        for item in payload["source_manifest"]["files"]
        if item["release_path"] == "account_a_live_trade_http_adapter.py"
    ]
    assert len(adapter_entries) == 1
    assert adapter_entries[0]["source_path"] == (
        "scripts/account_a_live_trade_http_adapter.py"
    )
    assert adapter_entries[0]["source_git_mode"] == "100755"


def test_release_builder_rejects_incomplete_live_risk_policy(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_repo(repo)
    policy_path = (
        repo
        / "services"
        / "nautilus-node"
        / "config"
        / "live-risk-policy.json"
    )
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    del policy["runtime_resource_contract"]["reporter_workers"]
    policy_path.write_text(
        json.dumps(policy, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _commit_path(repo, policy_path, "break live risk policy")
    output = tmp_path / "release"

    with pytest.raises(
        release.ReleaseBundleError,
        match="live risk policy runtime resource contract is invalid",
    ):
        release.build_release(repo, output)

    assert not output.exists()


def test_release_contract_rejects_missing_watcher_runtime_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_source, missing_release, _target = (
        release.WATCHER_RELEASE_FILES[0]
    )
    monkeypatch.setattr(
        release,
        "RELEASE_FILES",
        tuple(
            item
            for item in release.RELEASE_FILES
            if item != (missing_source, missing_release)
        ),
    )

    with pytest.raises(
        release.ReleaseBundleError,
        match="watcher runtime exact-set mismatch",
    ):
        release._validate_release_contract()


def test_release_contract_rejects_partial_account_node_resource_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumers = []
    for raw in release.SYSTEMD_RESOURCE_CONSUMERS:
        item = dict(raw)
        item["owner_units"] = list(raw["owner_units"])
        item["destinations"] = list(raw["destinations"])
        if item["artifact"] == (
            "infra/systemd/account-stall-account-node.conf"
        ):
            item["owner_units"] = item["owner_units"][:2]
            item["destinations"] = item["destinations"][:2]
        consumers.append(item)
    monkeypatch.setattr(
        release,
        "SYSTEMD_RESOURCE_CONSUMERS",
        tuple(consumers),
    )

    with pytest.raises(
        release.ReleaseBundleError,
        match="account node resource owners must cover A-D exact-set",
    ):
        release._validate_release_contract()


def test_release_contract_rejects_missing_decision_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release,
        "RELEASE_FILES",
        tuple(
            item
            for item in release.RELEASE_FILES
            if item[1] != "host/decision_gateway/gateway.py"
        ),
    )

    with pytest.raises(
        release.ReleaseBundleError,
        match="control-plane host runtime files",
    ):
        release._validate_release_contract()


def test_release_contract_rejects_missing_exchange_state_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release,
        "RELEASE_FILES",
        tuple(
            item
            for item in release.RELEASE_FILES
            if item[1] != EXCHANGE_STATE_RECORDER_RELEASE_PATH
        ),
    )

    with pytest.raises(
        release.ReleaseBundleError,
        match="control-plane host runtime files",
    ):
        release._validate_release_contract()


def test_release_contract_rejects_missing_control_plane_role_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required_file = (
        CONTROL_PLANE_BOOTSTRAP_SOURCE_PATH,
        CONTROL_PLANE_BOOTSTRAP_RELEASE_PATH,
    )
    assert (
        required_file
        in release.REQUIRED_CONTROL_PLANE_BOOTSTRAP_RELEASE_FILES
    )
    monkeypatch.setattr(
        release,
        "RELEASE_FILES",
        tuple(
            item
            for item in release.RELEASE_FILES
            if item != required_file
        ),
    )

    with pytest.raises(
        release.ReleaseBundleError,
        match="control-plane role bootstrap files",
    ):
        release._validate_release_contract()


@pytest.mark.parametrize(
    "release_path",
    [
        HERMES_FEEDER_RELEASE_PATH,
        V3_TRADE_RELEASE_PATH,
        V3_SKILL_RELEASE_PATH,
    ],
)
def test_release_contract_rejects_missing_hermes_runtime_file(
    monkeypatch: pytest.MonkeyPatch,
    release_path: str,
) -> None:
    assert release_path in release.REQUIRED_HERMES_RELEASE_PATHS
    monkeypatch.setattr(
        release,
        "RELEASE_FILES",
        tuple(
            item
            for item in release.RELEASE_FILES
            if item[1] != release_path
        ),
    )

    with pytest.raises(
        release.ReleaseBundleError,
        match="Hermes routing runtime files",
    ):
        release._validate_release_contract()


@pytest.mark.parametrize(
    ("source_path", "release_path"),
    [
        (EXECUTOR_SOURCE_PATH, EXECUTOR_RELEASE_PATH),
        (ADAPTER_SOURCE_PATH, ADAPTER_RELEASE_PATH),
    ],
)
def test_release_contract_rejects_missing_live_trade_file(
    monkeypatch: pytest.MonkeyPatch,
    source_path: str,
    release_path: str,
) -> None:
    required_file = (source_path, release_path)
    assert required_file in release.REQUIRED_LIVE_TRADE_RELEASE_FILES
    monkeypatch.setattr(
        release,
        "RELEASE_FILES",
        tuple(
            item
            for item in release.RELEASE_FILES
            if item != required_file
        ),
    )

    with pytest.raises(
        release.ReleaseBundleError,
        match="release lacks live trade execution files",
    ):
        release._validate_release_contract()


def test_release_builder_rejects_attention_content_and_does_not_publish(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_repo(repo)
    read_api = repo / "services/control-plane/api/read_api.py"
    read_api.write_text(
        read_api.read_text(encoding="utf-8")
        + "\n@app.post('/v1/attention/resolution-events')\n"
        + "def attention_resolution_event():\n"
        + "    return {}\n",
        encoding="utf-8",
    )
    _commit_path(repo, read_api, "add forbidden attention route")
    output = tmp_path / "release"

    with pytest.raises(
        release.ReleaseBundleError,
        match="forbidden Attention content",
    ):
        release.build_release(repo, output)

    assert not output.exists()


def test_release_builder_rejects_noncanonical_hermes_feeder(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_repo(repo)
    feeder = repo / HERMES_FEEDER_SOURCE_PATH
    feeder.write_text(
        feeder.read_text(encoding="utf-8") + "\n# stale feeder payload\n",
        encoding="utf-8",
    )
    _commit_path(repo, feeder, "replace canonical Hermes feeder")
    output = tmp_path / "release"

    with pytest.raises(
        release.ReleaseBundleError,
        match="Hermes feeder SHA256 does not match",
    ):
        release.build_release(repo, output)

    assert not output.exists()


def test_release_builder_rejects_dirty_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_repo(repo)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(
        release.ReleaseBundleError,
        match="clean git worktree",
    ):
        release.build_release(repo, tmp_path / "release")


def test_release_reads_payload_from_commit_during_worktree_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_repo(repo)
    output = tmp_path / "release"
    source_relative, destination_relative = release.RELEASE_FILES[0]
    source = repo / source_relative
    committed_payload = source.read_bytes()
    original_copy = release._copy_release_files

    def copy_during_worktree_replacement(
        repo_root: Path,
        output_dir: Path,
        source_commit: str,
    ) -> list[dict[str, object]]:
        original_payload = source.read_bytes()
        source.write_text(
            "concurrent worktree replacement\n",
            encoding="utf-8",
        )
        try:
            return original_copy(repo_root, output_dir, source_commit)
        finally:
            source.write_bytes(original_payload)

    monkeypatch.setattr(
        release,
        "_copy_release_files",
        copy_during_worktree_replacement,
    )

    release.build_release(repo, output)

    assert (output / destination_relative).read_bytes() == committed_payload


def test_release_rejects_head_drift_and_does_not_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_repo(repo)
    output = tmp_path / "release"
    original_copy = release._copy_release_files

    def copy_then_move_head(
        repo_root: Path,
        output_dir: Path,
        source_commit: str,
    ) -> list[dict[str, object]]:
        files = original_copy(repo_root, output_dir, source_commit)
        marker = repo_root / "head-change.txt"
        marker.write_text("changed\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "concurrent head change"],
            cwd=repo_root,
            check=True,
        )
        return files

    monkeypatch.setattr(
        release,
        "_copy_release_files",
        copy_then_move_head,
    )

    with pytest.raises(
        release.ReleaseBundleError,
        match="HEAD changed",
    ):
        release.build_release(repo, output)

    assert not output.exists()


def test_release_rejects_payload_tampering_and_does_not_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_repo(repo)
    output = tmp_path / "release"
    original_write_checksums = release._write_checksums

    def write_checksums_then_tamper(output_dir: Path) -> Path:
        checksum_path = original_write_checksums(output_dir)
        target = output_dir / release.RELEASE_FILES[0][1]
        target.write_text("tampered\n", encoding="utf-8")
        return checksum_path

    monkeypatch.setattr(
        release,
        "_write_checksums",
        write_checksums_then_tamper,
    )

    with pytest.raises(
        release.ReleaseBundleError,
        match="release payload hash mismatch",
    ):
        release.build_release(repo, output)

    assert not output.exists()
