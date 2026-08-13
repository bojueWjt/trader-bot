from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_immutable_node_image as builder
import release_manifest

BASE_IMAGE = "sha256:" + ("a" * 64)
BUILT_IMAGE = "sha256:" + ("b" * 64)
BASE_LAYERS = [
    "sha256:" + ("c" * 64),
    "sha256:" + ("d" * 64),
]
BUILT_LAYER = "sha256:" + ("e" * 64)
LOCK_CONTENT = """
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
EXPECTED_RUNTIME_ENTRIES = (
    ("health_server.py", "/app/app/health_server.py"),
    ("run_node.py", "/app/app/run_node.py"),
    ("health.py", "/app/runtime/health.py"),
    (
        "live_canary_execution.py",
        "/app/runtime/live_canary_execution.py",
    ),
    (
        "bounded_task_worker.py",
        "/app/runtime/bounded_task_worker.py",
    ),
    (
        "control_plane_session.py",
        "/app/runtime/control_plane_session.py",
    ),
    (
        "intent_execution_inbox.py",
        "/app/runtime/intent_execution_inbox.py",
    ),
    ("redis_safety.py", "/app/runtime/redis_safety.py"),
    ("reconciliation.py", "/app/runtime/reconciliation.py"),
    ("risk_config.py", "/app/risk/config.py"),
    ("risk_init.py", "/app/risk/__init__.py"),
    ("projection_spool.py", "/app/projection/spool.py"),
    (
        "approved_intent_client.py",
        "/app/data_client/approved_intent_client.py",
    ),
    ("nautilus_config.py", "/app/persistence/nautilus_config.py"),
    ("persistence_init.py", "/app/persistence/__init__.py"),
    (
        "redis_namespace_lease.py",
        "/app/persistence/redis_namespace_lease.py",
    ),
    ("redis_resp_client.py", "/app/persistence/redis_resp_client.py"),
    ("node_config.py", "/app/config/node_config.py"),
)
EXPECTED_IMMUTABLE_ENTRIES = (
    (
        "intent_execution_planner.py",
        "/app/strategy/intent_execution_planner.py",
    ),
    ("contracts.py", "/app/execution_domain/contracts.py"),
    ("control_plane.py", "/app/execution_domain/control_plane.py"),
    ("http_client.py", "/app/execution_domain/http_client.py"),
    ("projection_actor.py", "/app/projection/actor.py"),
    ("projection_spool.py", "/app/projection/spool.py"),
    ("event_mapper.py", "/app/projection/event_mapper.py"),
    (
        "intent_execution_strategy.py",
        "/app/strategy/intent_execution_strategy.py",
    ),
    (
        "exchange_cancel_adapter.py",
        "/app/runtime/exchange_cancel_adapter.py",
    ),
    ("lifecycle.py", "/app/runtime/lifecycle.py"),
    ("health.py", "/app/runtime/health.py"),
    (
        "bounded_task_worker.py",
        "/app/runtime/bounded_task_worker.py",
    ),
    (
        "control_plane_session.py",
        "/app/runtime/control_plane_session.py",
    ),
    (
        "intent_execution_inbox.py",
        "/app/runtime/intent_execution_inbox.py",
    ),
    ("redis_safety.py", "/app/runtime/redis_safety.py"),
    ("reconciliation.py", "/app/runtime/reconciliation.py"),
    (
        "binance_adapter_config.py",
        "/app/runtime/binance_adapter_config.py",
    ),
    (
        "live_canary_execution.py",
        "/app/runtime/live_canary_execution.py",
    ),
    ("node_config.py", "/app/config/node_config.py"),
    ("risk_config.py", "/app/risk/config.py"),
    ("risk_init.py", "/app/risk/__init__.py"),
    ("node.py", "/app/app/node.py"),
    ("run_node.py", "/app/app/run_node.py"),
    ("health_server.py", "/app/app/health_server.py"),
    ("nautilus_actors.py", "/app/app/nautilus_actors.py"),
    (
        "approved_intent_client.py",
        "/app/data_client/approved_intent_client.py",
    ),
    ("nautilus_config.py", "/app/persistence/nautilus_config.py"),
    ("persistence_init.py", "/app/persistence/__init__.py"),
    (
        "redis_namespace_lease.py",
        "/app/persistence/redis_namespace_lease.py",
    ),
    (
        "redis_resp_client.py",
        "/app/persistence/redis_resp_client.py",
    ),
    (
        "binance_execution.py",
        (
            "/usr/local/lib/python3.12/site-packages/"
            "nautilus_trader/adapters/binance/execution.py"
        ),
    ),
    (
        "binance_futures_execution.py",
        (
            "/usr/local/lib/python3.12/site-packages/"
            "nautilus_trader/adapters/binance/futures/execution.py"
        ),
    ),
)


def _write_bundle(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for name, target in EXPECTED_IMMUTABLE_ENTRIES:
        payload = root / name
        payload.write_text(f"module={name}\n", encoding="utf-8")
        files.append(
            {
                "bundle_path": name,
                "mount_target": target,
                "sha256": release_manifest.sha256_file(payload),
            }
        )
    manifest = root / "bundle-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "repo_commit": "b" * 40,
                "repo_dirty": False,
                "files": files,
            }
        ),
        encoding="utf-8",
    )
    _write_migration_manifest(root)
    _write_release_source_manifest(root)
    return manifest


def _write_migration_manifest(root: Path) -> Path:
    runner_path = release_manifest.MIGRATION_RUNNER_PATH
    migration_paths = list(
        release_manifest.CANONICAL_MIGRATION_PATHS
    )
    runner = root / runner_path
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text("# migration runner\n", encoding="utf-8")
    migrations = []
    for relative in migration_paths:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"-- {relative}\n", encoding="utf-8")
        migrations.append(
            {
                "path": relative,
                "sha256": release_manifest.sha256_file(path),
            }
        )
    manifest = root / release_manifest.MIGRATION_MANIFEST_NAME
    manifest.write_text(
        json.dumps(
            {
                "schema_version": (
                    release_manifest.MIGRATION_MANIFEST_SCHEMA_VERSION
                ),
                "schema_epoch": release_manifest.SCHEMA_EPOCHS["db"],
                "runner": {
                    "path": runner_path,
                    "sha256": release_manifest.sha256_file(runner),
                },
                "up": release_manifest.MIGRATION_UP_PATH,
                "down": release_manifest.MIGRATION_DOWN_PATH,
                "prerequisites": list(
                    release_manifest.MIGRATION_PREREQUISITE_PATHS
                ),
                "steps": [
                    dict(item)
                    for item in release_manifest.CANONICAL_MIGRATION_STEPS
                ],
                "migrations": migrations,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def _write_lock(root: Path) -> Path:
    path = root / "uv.node.lock"
    path.write_text(LOCK_CONTENT, encoding="utf-8")
    return path


def _write_reviewed_lock(bundle_manifest: Path) -> Path:
    return _write_lock(bundle_manifest.parent)


def _write_release_source_manifest(root: Path) -> Path:
    path = root / release_manifest.RELEASE_SOURCE_MANIFEST_NAME
    path.write_text(
        json.dumps(
            {
                "schema_version": "test-release-source/v1",
                "source_commit": "b" * 40,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _build_with_layer_results(
    tmp_path: Path,
    layer_results: list[object],
    image_id_results: list[object] | None = None,
) -> dict[str, object]:
    bundle_manifest = _write_bundle(tmp_path / "bundle")
    dependency_lock = _write_reviewed_lock(bundle_manifest)
    iid_output = tmp_path / "derived.id"
    captured: dict[str, object] = {}

    def fake_run(command, check):
        assert check is True
        if command[:3] == ["docker", "image", "tag"]:
            captured["tag_command"] = command
            return mock.Mock(returncode=0)
        captured["command"] = command
        iid_index = command.index("--iidfile") + 1
        Path(command[iid_index]).write_text(
            f"{BUILT_IMAGE}\n",
            encoding="utf-8",
        )
        context = Path(command[-1])
        captured["dockerfile"] = (
            context / "Dockerfile"
        ).read_text(encoding="utf-8")
        return mock.Mock(returncode=0)

    def fake_image_labels(_image_id):
        command = captured["command"]
        labels = {}
        for index, token in enumerate(command):
            if token != "--label":
                continue
            key, value = command[index + 1].split("=", 1)
            labels[key] = value
        return labels

    selected_image_id_results = image_id_results
    if selected_image_id_results is None:
        selected_image_id_results = [
            BASE_IMAGE,
            BASE_IMAGE,
            BUILT_IMAGE,
            BASE_IMAGE,
        ]

    with (
        mock.patch.object(
            builder,
            "_docker_image_id",
            side_effect=selected_image_id_results,
        ) as image_id,
        mock.patch.object(
            builder,
            "_docker_image_layers",
            side_effect=layer_results,
        ) as image_layers,
        mock.patch.object(
            builder,
            "_docker_image_labels",
            side_effect=fake_image_labels,
        ),
        mock.patch.object(
            builder.subprocess,
            "run",
            side_effect=fake_run,
        ),
    ):
        built = builder.build_immutable_image(
            bundle_manifest=bundle_manifest,
            dependency_lock=dependency_lock,
            base_image=BASE_IMAGE,
            iid_output=iid_output,
        )

    return {
        "built": built,
        "captured": captured,
        "iid_output": iid_output,
        "image_id": image_id,
        "image_layers": image_layers,
    }


def test_context_contains_numbered_python_payload_only(tmp_path: Path) -> None:
    actual_runtime_entries = {
        (bundle_name, mount_target)
        for bundle_name, _source_path, mount_target in (
            release_manifest.TRANSITION_RUNTIME_FILES
        )
    }
    assert actual_runtime_entries == set(EXPECTED_RUNTIME_ENTRIES)

    bundle_manifest = _write_bundle(tmp_path / "bundle")
    dependency_lock = _write_reviewed_lock(bundle_manifest)
    context = tmp_path / "context"

    copied = builder.prepare_build_context(
        bundle_manifest,
        context,
        dependency_lock,
    )

    names = sorted(
        path.relative_to(context).as_posix()
        for path in context.rglob("*")
        if path.is_file()
    )
    expected_payloads = [
        f"payload/{index:04d}"
        for index in range(len(EXPECTED_IMMUTABLE_ENTRIES))
    ]
    assert names == [
        "Dockerfile.body",
        *expected_payloads,
        "payload/dependency-inventory.json",
        "payload/dependency.lock",
        f"payload/{release_manifest.MIGRATION_MANIFEST_NAME}",
    ]
    dockerfile = (context / "Dockerfile.body").read_text(encoding="utf-8")
    assert "bundle-manifest.json" not in dockerfile
    assert "/cfg.json" not in dockerfile
    assert ".env" not in dockerfile
    assert dockerfile.count("\n") == len(EXPECTED_IMMUTABLE_ENTRIES) + 4
    assert release_manifest.IMMUTABLE_DEPENDENCY_LOCK_TARGET in dockerfile
    assert (
        release_manifest.IMMUTABLE_DEPENDENCY_INVENTORY_TARGET
        in dockerfile
    )
    assert release_manifest.IMMUTABLE_MIGRATION_MANIFEST_TARGET in dockerfile
    assert "importlib.metadata" in dockerfile
    inventory = json.loads(
        (context / "payload/dependency-inventory.json").read_text(
            encoding="utf-8"
        )
    )
    assert inventory["packages"] == [
        {
            "name": "runtime-demo",
            "version": "1.2.3",
            "registry": "https://pypi.org/simple",
            "artifacts": [
                {
                    "kind": "sdist",
                    "url": (
                        "https://example.invalid/"
                        "runtime-demo-1.2.3.tar.gz"
                    ),
                    "hash": "sha256:" + ("a" * 64),
                    "size": 1,
                },
            ],
        },
    ]
    assert {
        item["mount_target"]
        for item in copied
    } == {
        target
        for _name, target in EXPECTED_IMMUTABLE_ENTRIES
    }
    assert {
        item["context_path"]
        for item in copied
    } == set(expected_payloads)
    assert len(copied) == len(EXPECTED_IMMUTABLE_ENTRIES)


def test_context_rejects_non_python_config_payload(tmp_path: Path) -> None:
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    bundle_manifest = _write_bundle(bundle_root)
    raw = json.loads(bundle_manifest.read_text(encoding="utf-8"))
    cfg = bundle_root / "cfg.json"
    cfg.write_text('{"secret":"hidden"}', encoding="utf-8")
    raw["files"].append(
        {
            "bundle_path": "cfg.json",
            "mount_target": "/cfg.json",
            "sha256": release_manifest.sha256_file(cfg),
        }
    )
    bundle_manifest.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(
        builder.ImmutableBuildError,
        match="Python business modules only",
    ):
        builder.prepare_build_context(
            bundle_manifest,
            tmp_path / "context",
            _write_reviewed_lock(bundle_manifest),
        )


def test_context_rejects_missing_intent_execution_inbox(
    tmp_path: Path,
) -> None:
    bundle_root = tmp_path / "bundle"
    bundle_manifest = _write_bundle(bundle_root)
    raw = json.loads(bundle_manifest.read_text(encoding="utf-8"))
    raw["files"] = [
        item
        for item in raw["files"]
        if item["mount_target"] != "/app/runtime/intent_execution_inbox.py"
    ]
    bundle_manifest.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(
        release_manifest.ReleaseManifestError,
        match="transition runtime mounts",
    ):
        builder.prepare_build_context(
            bundle_manifest,
            tmp_path / "context",
            _write_reviewed_lock(bundle_manifest),
        )


def test_context_rejects_pseudo_dependency_lock(tmp_path: Path) -> None:
    bundle_manifest = _write_bundle(tmp_path / "bundle")
    pseudo_lock = bundle_manifest.parent / "uv.node.lock"
    pseudo_lock.write_text("version = 1\n", encoding="utf-8")

    with pytest.raises(
        release_manifest.ReleaseManifestError,
        match="revision",
    ):
        builder.prepare_build_context(
            bundle_manifest,
            tmp_path / "context",
            pseudo_lock,
        )


def test_context_rejects_migration_manifest_hash_drift(
    tmp_path: Path,
) -> None:
    bundle_manifest = _write_bundle(tmp_path / "bundle")
    migration = (
        bundle_manifest.parent
        / release_manifest.CANONICAL_MIGRATION_PATHS[0]
    )
    migration.write_text("-- tampered\n", encoding="utf-8")

    with pytest.raises(
        release_manifest.ReleaseManifestError,
        match="migration payload hash mismatch",
    ):
        builder.prepare_build_context(
            bundle_manifest,
            tmp_path / "context",
            _write_reviewed_lock(bundle_manifest),
        )


def test_dependency_inventory_verifier_compares_exact_installed_set(
    tmp_path: Path,
) -> None:
    allowed = set(
        release_manifest.DEPENDENCY_INVENTORY_ALLOWED_UNLOCKED
    )
    installed = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        assert name
        canonical = release_manifest.canonical_package_name(name)
        if canonical in allowed:
            continue
        installed[canonical] = str(distribution.version)
    assert installed
    inventory_path = tmp_path / "dependency-inventory.json"
    inventory = {
        "allowed_unlocked_packages": sorted(allowed),
        "packages": [
            {
                "name": name,
                "version": version,
            }
            for name, version in sorted(installed.items())
        ],
    }
    inventory_path.write_text(
        json.dumps(inventory, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    command = release_manifest.dependency_inventory_verifier_command(
        str(inventory_path)
    )
    command[0] = sys.executable

    matched = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )

    assert matched.returncode == 0
    assert matched.stdout.strip() == release_manifest.sha256_file(
        inventory_path
    )

    inventory["packages"].pop()
    inventory_path.write_text(
        json.dumps(inventory, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    mismatched = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )

    assert mismatched.returncode != 0
    assert "dependency inventory mismatch" in mismatched.stderr


def test_build_uses_local_digest_and_network_none(tmp_path: Path) -> None:
    result = _build_with_layer_results(
        tmp_path,
        [BASE_LAYERS, [*BASE_LAYERS, BUILT_LAYER]],
    )
    captured = result["captured"]
    iid_output = result["iid_output"]
    image_id = result["image_id"]
    image_layers = result["image_layers"]

    assert result["built"] == BUILT_IMAGE
    assert iid_output.read_text(encoding="utf-8").strip() == BUILT_IMAGE
    assert "--network=none" in captured["command"]
    assert "--pull=false" in captured["command"]
    local_base = builder._pinned_local_base_reference(BASE_IMAGE)
    assert captured["tag_command"] == [
        "docker",
        "image",
        "tag",
        BASE_IMAGE,
        local_base,
    ]
    assert captured["dockerfile"].splitlines()[0] == (
        f"FROM {local_base}"
    )
    assert image_id.call_args_list == [
        mock.call(BASE_IMAGE),
        mock.call(local_base),
        mock.call(BUILT_IMAGE),
        mock.call(local_base),
    ]
    assert image_layers.call_args_list == [
        mock.call(BASE_IMAGE),
        mock.call(BUILT_IMAGE),
    ]


def test_build_rejects_mismatched_base_layer_prefix(
    tmp_path: Path,
) -> None:
    mismatched_layers = [
        "sha256:" + ("f" * 64),
        BASE_LAYERS[1],
        BUILT_LAYER,
    ]

    with pytest.raises(
        builder.ImmutableBuildError,
        match="base layer prefix mismatch",
    ):
        _build_with_layer_results(
            tmp_path,
            [BASE_LAYERS, mismatched_layers],
        )


def test_build_fails_closed_when_built_layer_inspect_fails(
    tmp_path: Path,
) -> None:
    inspect_error = release_manifest.ReleaseManifestError(
        "cannot inspect Docker image layers"
    )

    with pytest.raises(
        builder.ImmutableBuildError,
        match="cannot inspect Docker image layers",
    ):
        _build_with_layer_results(
            tmp_path,
            [BASE_LAYERS, inspect_error],
        )


def test_build_rejects_alias_identity_change_after_build(
    tmp_path: Path,
) -> None:
    changed_alias = "sha256:" + ("f" * 64)

    with pytest.raises(
        builder.ImmutableBuildError,
        match="reference changed after build",
    ):
        _build_with_layer_results(
            tmp_path,
            [BASE_LAYERS, [*BASE_LAYERS, BUILT_LAYER]],
            [
                BASE_IMAGE,
                BASE_IMAGE,
                BUILT_IMAGE,
                changed_alias,
            ],
        )
