from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "make_container_bundle.py"
SPEC = importlib.util.spec_from_file_location("make_container_bundle", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)

EXPECTED_REDIS_SCHEMA_EPOCH = "fenced-generation-namespace/v2"
EXPECTED_RUNTIME_FILES = {
    "health_server.py": "/app/app/health_server.py",
    "run_node.py": "/app/app/run_node.py",
    "health.py": "/app/runtime/health.py",
    "bounded_task_worker.py": "/app/runtime/bounded_task_worker.py",
    "control_plane_session.py": "/app/runtime/control_plane_session.py",
    "intent_execution_inbox.py": "/app/runtime/intent_execution_inbox.py",
    "redis_safety.py": "/app/runtime/redis_safety.py",
    "reconciliation.py": "/app/runtime/reconciliation.py",
    "live_canary_execution.py": "/app/runtime/live_canary_execution.py",
    "routing_init.py": "/app/routing/__init__.py",
    "routing_multi_account.py": "/app/routing/multi_account.py",
    "node_config.py": "/app/config/node_config.py",
    "risk_config.py": "/app/risk/config.py",
    "risk_init.py": "/app/risk/__init__.py",
    "projection_spool.py": "/app/projection/spool.py",
    "approved_intent_client.py": "/app/data_client/approved_intent_client.py",
    "nautilus_config.py": "/app/persistence/nautilus_config.py",
    "persistence_init.py": "/app/persistence/__init__.py",
    "redis_namespace_lease.py": "/app/persistence/redis_namespace_lease.py",
    "redis_resp_client.py": "/app/persistence/redis_resp_client.py",
}
EXPECTED_BUNDLE_FILES = {
    (
        "intent_execution_planner.py",
        "services/nautilus-node/strategy/intent_execution_planner.py",
        "/app/strategy/intent_execution_planner.py",
    ),
    (
        "contracts.py",
        "services/nautilus-node/projection/contracts.py",
        "/app/execution_domain/contracts.py",
    ),
    (
        "control_plane.py",
        "packages/execution-domain/execution_domain/control_plane.py",
        "/app/execution_domain/control_plane.py",
    ),
    (
        "portfolio_baseline.py",
        "packages/execution-domain/execution_domain/portfolio_baseline.py",
        "/app/execution_domain/portfolio_baseline.py",
    ),
    (
        "order_ownership.py",
        "packages/execution-domain/execution_domain/order_ownership.py",
        "/app/execution_domain/order_ownership.py",
    ),
    (
        "http_client.py",
        "packages/execution-domain/execution_domain/http_client.py",
        "/app/execution_domain/http_client.py",
    ),
    (
        "projection_actor.py",
        "services/nautilus-node/projection/actor.py",
        "/app/projection/actor.py",
    ),
    (
        "projection_spool.py",
        "services/nautilus-node/projection/spool.py",
        "/app/projection/spool.py",
    ),
    (
        "event_mapper.py",
        "services/nautilus-node/projection/event_mapper.py",
        "/app/projection/event_mapper.py",
    ),
    (
        "intent_execution_strategy.py",
        "services/nautilus-node/strategy/intent_execution_strategy.py",
        "/app/strategy/intent_execution_strategy.py",
    ),
    (
        "exchange_cancel_adapter.py",
        "services/nautilus-node/runtime/exchange_cancel_adapter.py",
        "/app/runtime/exchange_cancel_adapter.py",
    ),
    (
        "lifecycle.py",
        "services/nautilus-node/runtime/lifecycle.py",
        "/app/runtime/lifecycle.py",
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
        "binance_adapter_config.py",
        "services/nautilus-node/runtime/binance_adapter_config.py",
        "/app/runtime/binance_adapter_config.py",
    ),
    (
        "live_canary_execution.py",
        "services/nautilus-node/runtime/live_canary_execution.py",
        "/app/runtime/live_canary_execution.py",
    ),
    (
        "routing_init.py",
        "services/nautilus-node/routing/__init__.py",
        "/app/routing/__init__.py",
    ),
    (
        "routing_multi_account.py",
        "services/nautilus-node/routing/multi_account.py",
        "/app/routing/multi_account.py",
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
        "node.py",
        "services/nautilus-node/app/node.py",
        "/app/app/node.py",
    ),
    (
        "run_node.py",
        "services/nautilus-node/app/run_node.py",
        "/app/app/run_node.py",
    ),
    (
        "health_server.py",
        "services/nautilus-node/app/health_server.py",
        "/app/app/health_server.py",
    ),
    (
        "nautilus_actors.py",
        "services/nautilus-node/app/nautilus_actors.py",
        "/app/app/nautilus_actors.py",
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
    (
        "binance_execution.py",
        "container-patches/binance_execution.py",
        (
            "/usr/local/lib/python3.12/site-packages/"
            "nautilus_trader/adapters/binance/execution.py"
        ),
    ),
    (
        "binance_futures_execution.py",
        "container-patches/binance_futures_execution.py",
        (
            "/usr/local/lib/python3.12/site-packages/"
            "nautilus_trader/adapters/binance/futures/execution.py"
        ),
    ),
}
DEPENDENCY_CLOSURE_FILES = {
    "/app/projection/actor.py": (
        "projection_actor.py",
        "services/nautilus-node/projection/actor.py",
    ),
    "/app/projection/spool.py": (
        "projection_spool.py",
        "services/nautilus-node/projection/spool.py",
    ),
    "/app/risk/config.py": (
        "risk_config.py",
        "services/nautilus-node/risk/config.py",
    ),
    "/app/risk/__init__.py": (
        "risk_init.py",
        "services/nautilus-node/risk/__init__.py",
    ),
    "/app/runtime/intent_execution_inbox.py": (
        "intent_execution_inbox.py",
        "services/nautilus-node/runtime/intent_execution_inbox.py",
    ),
    "/app/strategy/intent_execution_planner.py": (
        "intent_execution_planner.py",
        "services/nautilus-node/strategy/intent_execution_planner.py",
    ),
    "/app/routing/__init__.py": (
        "routing_init.py",
        "services/nautilus-node/routing/__init__.py",
    ),
    "/app/routing/multi_account.py": (
        "routing_multi_account.py",
        "services/nautilus-node/routing/multi_account.py",
    ),
}


def _seed_sources(root: Path) -> None:
    for bundle_name, source_relative, _mount_target in bundle.BUNDLE_FILES:
        source = root / source_relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"source={source_relative}\nbundle={bundle_name}\n")


def _commit_sources(root: Path) -> str:
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
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
    ).strip()


def test_bundle_uses_pinned_sources_and_writes_commit_hash_manifest(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    output_dir = tmp_path / "bundle"
    _seed_sources(repo_root)

    manifest = bundle.build_bundle(
        repo_root,
        output_dir,
        repo_commit="a" * 40,
        repo_dirty=False,
    )

    assert manifest["repo_commit"] == "a" * 40
    assert manifest["repo_dirty"] is False
    assert manifest["source_mode"] == "worktree"
    assert manifest["schema_version"] == bundle.BUNDLE_SCHEMA_VERSION
    assert manifest["schema_epochs"] == bundle.SCHEMA_EPOCHS
    assert manifest["schema_epochs"]["redis"] == EXPECTED_REDIS_SCHEMA_EPOCH
    assert "stable-account-namespace/v1" not in json.dumps(manifest)
    assert set(bundle.BUNDLE_FILES) == EXPECTED_BUNDLE_FILES
    assert len(manifest["files"]) == len(bundle.BUNDLE_FILES)
    assert {
        (
            item["bundle_path"],
            item["source_path"],
            item["mount_target"],
        )
        for item in manifest["files"]
    } == EXPECTED_BUNDLE_FILES
    assert set(manifest["runtime_bundle_contract"]) == (
        bundle.REQUIRED_RUNTIME_BUNDLE_TARGETS
    )
    assert bundle.REQUIRED_RUNTIME_BUNDLE_TARGETS.issubset(
        {item["mount_target"] for item in manifest["files"]}
    )
    runtime_files = {
        bundle_name: mount_target
        for bundle_name, _source_relative, mount_target in bundle.BUNDLE_FILES
        if mount_target in bundle.REQUIRED_RUNTIME_BUNDLE_TARGETS
    }
    assert runtime_files == EXPECTED_RUNTIME_FILES
    assert set(manifest["runtime_bundle_contract"]) == set(
        EXPECTED_RUNTIME_FILES.values()
    )
    assert len({
        item["bundle_path"]
        for item in manifest["files"]
    }) == len(bundle.BUNDLE_FILES)
    assert len({
        item["mount_target"]
        for item in manifest["files"]
    }) == len(bundle.BUNDLE_FILES)
    contracts = next(
        item for item in manifest["files"] if item["bundle_path"] == "contracts.py"
    )
    assert contracts["source_path"] == (
        "services/nautilus-node/projection/contracts.py"
    )
    contracts_path = output_dir / "contracts.py"
    assert contracts["sha256"] == hashlib.sha256(
        contracts_path.read_bytes()
    ).hexdigest()
    persisted = json.loads(
        (output_dir / bundle.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert persisted == manifest
    assert {
        path.name
        for path in output_dir.iterdir()
        if path.is_file()
    } == {
        bundle.MANIFEST_NAME,
        *(bundle_name for bundle_name, _source, _target in bundle.BUNDLE_FILES),
    }


def test_bundle_fails_closed_when_a_pinned_source_is_missing(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    output_dir = tmp_path / "bundle"
    _seed_sources(repo_root)
    missing = repo_root / bundle.BUNDLE_FILES[0][1]
    missing.unlink()

    with pytest.raises(bundle.BundleError, match="bundle source is missing"):
        bundle.build_bundle(
            repo_root,
            output_dir,
            repo_commit="b" * 40,
            repo_dirty=False,
        )


def test_bundle_closes_new_runtime_dependencies_over_legacy_base(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "bundle"
    manifest = bundle.build_bundle(
        REPO_ROOT,
        output_dir,
        repo_commit="c" * 40,
        repo_dirty=True,
    )
    files_by_target = {
        item["mount_target"]: item
        for item in manifest["files"]
    }
    for mount_target, (
        expected_bundle_path,
        expected_source_path,
    ) in DEPENDENCY_CLOSURE_FILES.items():
        item = files_by_target[mount_target]
        assert item["bundle_path"] == expected_bundle_path
        assert item["source_path"] == expected_source_path

    app_root = tmp_path / "legacy-image" / "app"
    risk_root = app_root / "risk"
    projection_root = app_root / "projection"
    risk_root.mkdir(parents=True)
    projection_root.mkdir(parents=True)
    (risk_root / "__init__.py").write_text("", encoding="utf-8")
    (risk_root / "limits.py").write_text(
        "class InstrumentPrecision:\n"
        "    pass\n"
        "class LimitOrderRequest:\n"
        "    pass\n"
        "class RiskLimitDecision:\n"
        "    pass\n"
        "class RiskLimitMirror:\n"
        "    pass\n"
        "class TradingStateOrderAction:\n"
        "    pass\n"
        "class TradingStateOrderGate:\n"
        "    pass\n"
        "class TradingStateOrderRequest:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (risk_root / "config.py").write_text(
        "class RiskLimitConfig:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (projection_root / "__init__.py").write_text("", encoding="utf-8")
    (projection_root / "contracts.py").write_text(
        "class ExecutionEventEnvelopeV1:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (projection_root / "event_mapper.py").write_text(
        "from dataclasses import dataclass\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class ProjectionConfig:\n"
        "    node_id: str\n"
        "    account_id: str\n"
        "    lag_degrade_threshold_ms: int = 30000\n"
        "    max_flush_batch_size: int = 100\n"
        "\n"
        "class ProjectionEventMapper:\n"
        "    def __init__(self, config, now=None):\n"
        "        self.config = config\n"
        "        self.now = now\n"
        "\n"
        "    def to_envelope(self, event):\n"
        "        return None\n",
        encoding="utf-8",
    )
    (projection_root / "spool.py").write_text(
        "class JsonExecutionSpool:\n"
        "    def __init__(self, path):\n"
        "        self.path = path\n",
        encoding="utf-8",
    )

    for mount_target, (bundle_path, _source_path) in (
        DEPENDENCY_CLOSURE_FILES.items()
    ):
        destination = app_root / mount_target.removeprefix("/app/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output_dir / bundle_path, destination)

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from risk import (\n"
                "    RiskLimitConfig,\n"
                "    build_live_entry_notional_inventory,\n"
                ")\n"
                "from projection.actor import ProjectionActor\n"
                "from projection.event_mapper import ProjectionConfig\n"
                "from projection.spool import JsonExecutionSpool\n"
                "from runtime.intent_execution_inbox import (\n"
                "    JsonIntentExecutionInbox,\n"
                ")\n"
                "from routing import AccountRoute\n"
                "\n"
                "risk = RiskLimitConfig(\n"
                "    max_notional_per_order={'SOLUSDT-PERP.BINANCE': '12'},\n"
                "    max_order_submit_rate='1/00:00:01',\n"
                "    max_order_modify_rate='1/00:00:01',\n"
                ")\n"
                "assert build_live_entry_notional_inventory(risk) == (\n"
                "    ('SOLUSDT-PERP.BINANCE', '12'),\n"
                ")\n"
                "spool = JsonExecutionSpool(sys.argv[1], max_bytes=4096)\n"
                "assert hasattr(spool, 'is_degraded')\n"
                "assert hasattr(spool, 'usage_ratio')\n"
                "\n"
                "class Sink:\n"
                "    def post_events(self, node_id, events):\n"
                "        return []\n"
                "\n"
                "actor = ProjectionActor(\n"
                "    ProjectionConfig(node_id='node-a', account_id='account-a'),\n"
                "    Sink(),\n"
                "    spool,\n"
                ")\n"
                "assert actor.egress_degraded_reason == ''\n"
                "inbox = JsonIntentExecutionInbox(sys.argv[2])\n"
                "assert inbox.pending() == ()\n"
                "route = AccountRoute(\n"
                "    account_id='account-a',\n"
                "    node_id='node-a',\n"
                "    redis_key_prefix='nautilus:account-a:',\n"
                "    control_plane_base_url='http://control-plane',\n"
                "    spool_root=sys.argv[3],\n"
                ")\n"
                "assert route.redis_key('commands') == (\n"
                "    'nautilus:account-a:commands'\n"
                ")\n"
            ),
            str(tmp_path / "probe-spool.json"),
            str(tmp_path / "probe-inbox.json"),
            str(tmp_path / "routing-spool"),
        ],
        env={
            **os.environ,
            "PYTHONPATH": str(app_root),
        },
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stdout + probe.stderr


def test_git_object_bundle_ignores_concurrent_worktree_bytes(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    output_dir = tmp_path / "bundle"
    repo_root.mkdir()
    _seed_sources(repo_root)
    commit = _commit_sources(repo_root)
    source_relative = bundle.BUNDLE_FILES[0][1]
    source = repo_root / source_relative
    committed_payload = source.read_bytes()
    source.write_text("concurrent worktree replacement\n", encoding="utf-8")

    manifest = bundle.build_bundle_from_commit(
        repo_root,
        output_dir,
        source_ref=commit,
        repo_dirty=True,
        expected_head=commit,
    )

    assert manifest["repo_commit"] == commit
    assert manifest["source_mode"] == "git_object"
    assert manifest["repo_dirty"] is True
    assert manifest["schema_epochs"] == bundle.SCHEMA_EPOCHS
    assert manifest["schema_epochs"]["redis"] == EXPECTED_REDIS_SCHEMA_EPOCH
    assert (output_dir / bundle.BUNDLE_FILES[0][0]).read_bytes() == (
        committed_payload
    )
    assert all(
        item["source_git_blob"]
        for item in manifest["files"]
    )


def test_git_object_bundle_rejects_head_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    output_dir = tmp_path / "bundle"
    repo_root.mkdir()
    _seed_sources(repo_root)
    commit = _commit_sources(repo_root)
    original_read = bundle.read_git_file
    changed = False

    def read_with_head_change(
        root: Path,
        source_commit: str,
        source_relative: str,
    ) -> tuple[bytes, str, str]:
        nonlocal changed
        result = original_read(root, source_commit, source_relative)
        if not changed:
            changed = True
            marker = root / "head-change.txt"
            marker.write_text("changed\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "concurrent head change"],
                cwd=root,
                check=True,
            )
        return result

    monkeypatch.setattr(bundle, "read_git_file", read_with_head_change)

    with pytest.raises(bundle.BundleError, match="HEAD changed"):
        bundle.build_bundle_from_commit(
            repo_root,
            output_dir,
            source_ref=commit,
            repo_dirty=False,
            expected_head=commit,
        )
