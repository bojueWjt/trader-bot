from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from unittest import mock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_immutable_watcher_image.py"
SPEC = importlib.util.spec_from_file_location(
    "build_immutable_watcher_image",
    SCRIPT,
)
assert SPEC is not None
assert SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(REPO_ROOT / "scripts"))
SPEC.loader.exec_module(builder)

BASE_IMAGE = "sha256:" + "a" * 64
BUILT_IMAGE = "sha256:" + "b" * 64
BASE_LAYERS = [
    "sha256:" + ("c" * 64),
    "sha256:" + ("d" * 64),
]
BUILT_LAYER = "sha256:" + ("e" * 64)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_release(root: Path) -> Path:
    files = []
    for index, (
        source_path,
        release_path,
        target_path,
    ) in enumerate(builder.WATCHER_RELEASE_FILES):
        path = root / release_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{index}:{source_path}:{target_path}\n",
            encoding="utf-8",
        )
        files.append(
            {
                "source_path": source_path,
                "release_path": release_path,
                "target_path": target_path,
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
        )
    manifest = {
        "schema_version": (
            builder.WATCHER_RUNTIME_MANIFEST_SCHEMA_VERSION
        ),
        "files": files,
        "payload_subject_sha256": builder._payload_subject_sha256(
            files
        ),
    }
    manifest_path = root / builder.WATCHER_RUNTIME_MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    source_manifest = {
        "schema_version": "trader-v3-release-source/v1",
        "watcher_runtime": {
            "manifest": manifest_path.name,
            "manifest_sha256": _sha256(manifest_path),
        },
    }
    (root / "release-source-manifest.json").write_text(
        json.dumps(source_manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _build_with_layer_results(
    tmp_path: Path,
    layer_results: list[object],
    image_id_results: list[object] | None = None,
) -> dict[str, object]:
    manifest_path = _write_release(tmp_path / "release")
    iid_output = tmp_path / "watcher-image.id"
    attestation_output = tmp_path / "watcher-attestation.json"
    captured: dict[str, object] = {}

    def fake_run(command, check):
        assert check is True
        if command[:3] == ["docker", "image", "tag"]:
            captured["tag_command"] = command
            return mock.Mock(returncode=0)
        captured["command"] = command
        labels = {}
        for index, token in enumerate(command):
            if token != "--label":
                continue
            key, separator, value = command[index + 1].partition("=")
            assert separator == "="
            labels[key] = value
        captured["labels"] = labels
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
            builder.subprocess,
            "run",
            side_effect=fake_run,
        ),
        mock.patch.object(
            builder,
            "_docker_image_labels",
            side_effect=lambda _image: captured["labels"],
        ),
    ):
        built = builder.build_immutable_watcher_image(
            manifest_path=manifest_path,
            base_image=BASE_IMAGE,
            iid_output=iid_output,
            attestation_output=attestation_output,
        )

    return {
        "attestation_output": attestation_output,
        "built": built,
        "captured": captured,
        "iid_output": iid_output,
        "image_id": image_id,
        "image_layers": image_layers,
    }


def test_prepare_context_copies_exact_runtime_set_and_checks_base_lock(
    tmp_path: Path,
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    context = tmp_path / "context"

    files = builder.prepare_build_context(manifest_path, context)

    assert {
        (item["release_path"], item["target_path"])
        for item in files
    } == {
        (release_path, target_path)
        for _source_path, release_path, target_path
        in builder.WATCHER_RELEASE_FILES
    }
    dockerfile_body = (context / "Dockerfile.body").read_text(
        encoding="utf-8"
    )
    assert "/app/package.json" in dockerfile_body
    assert "/app/package-lock.json" in dockerfile_body
    for _source_path, _release_path, target_path in (
        builder.WATCHER_RELEASE_FILES
    ):
        assert target_path in dockerfile_body


def test_context_payload_modes_survive_restrictive_umask(
    tmp_path: Path,
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    context = tmp_path / "context"

    previous_umask = os.umask(0o077)
    try:
        builder.prepare_build_context(manifest_path, context)
    finally:
        os.umask(previous_umask)

    payload_files = sorted((context / "payload").iterdir())
    assert payload_files
    for path in payload_files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o644, path.name


def test_manifest_rejects_missing_runtime_member(
    tmp_path: Path,
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].pop()
    manifest["payload_subject_sha256"] = (
        builder._payload_subject_sha256(manifest["files"])
    )
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        builder.ImmutableWatcherBuildError,
        match="exact-set mismatch",
    ):
        builder.validate_watcher_runtime_manifest(manifest_path)


def test_cli_validate_runtime_manifest_checks_release_source_contract(
    tmp_path: Path,
) -> None:
    manifest_path = _write_release(tmp_path / "release")

    with mock.patch.object(
        builder,
        "build_immutable_watcher_image",
    ) as build_image:
        assert builder.main(
            ["--validate-runtime-manifest", str(manifest_path)]
        ) == 0

    build_image.assert_not_called()

    source_manifest_path = manifest_path.parent / "release-source-manifest.json"
    source_manifest = json.loads(
        source_manifest_path.read_text(encoding="utf-8")
    )
    source_manifest["watcher_runtime"]["manifest_sha256"] = "0" * 64
    source_manifest_path.write_text(
        json.dumps(source_manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert builder.main(
        ["--validate-runtime-manifest", str(manifest_path)]
    ) == 2


def test_build_uses_digest_network_none_and_attested_labels(
    tmp_path: Path,
) -> None:
    result = _build_with_layer_results(
        tmp_path,
        [BASE_LAYERS, [*BASE_LAYERS, BUILT_LAYER]],
    )
    attestation_output = result["attestation_output"]
    captured = result["captured"]
    iid_output = result["iid_output"]
    image_id = result["image_id"]
    image_layers = result["image_layers"]

    assert image_id.call_count == 4
    assert result["built"] == BUILT_IMAGE
    assert iid_output.read_text(encoding="utf-8").strip() == BUILT_IMAGE
    command = captured["command"]
    assert "--network=none" in command
    assert "--pull=false" in command
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
    attestation = json.loads(
        attestation_output.read_text(encoding="utf-8")
    )
    assert attestation["schema_version"] == (
        builder.WATCHER_BUILD_ATTESTATION_SCHEMA_VERSION
    )
    assert attestation["base_image_digest"] == BASE_IMAGE
    assert attestation["image_digest"] == BUILT_IMAGE
    assert attestation["image_labels"] == captured["labels"]


def test_build_rejects_mismatched_base_layer_prefix(
    tmp_path: Path,
) -> None:
    mismatched_layers = [
        "sha256:" + ("f" * 64),
        BASE_LAYERS[1],
        BUILT_LAYER,
    ]

    with pytest.raises(
        builder.ImmutableWatcherBuildError,
        match="base layer prefix mismatch",
    ):
        _build_with_layer_results(
            tmp_path,
            [BASE_LAYERS, mismatched_layers],
        )


def test_build_fails_closed_when_built_layer_inspect_fails(
    tmp_path: Path,
) -> None:
    inspect_error = builder.ReleaseManifestError(
        "cannot inspect Docker image layers"
    )

    with pytest.raises(
        builder.ImmutableWatcherBuildError,
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
        builder.ImmutableWatcherBuildError,
        match="base image changed after build",
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
