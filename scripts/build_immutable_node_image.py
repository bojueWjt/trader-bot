#!/usr/bin/env python3
"""Build a minimal immutable trader node image from a reviewed code bundle."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from release_manifest import (
    BUILD_ATTESTATION_NAME,
    BUILD_ATTESTATION_REQUIRED_FIELDS,
    BUILD_ATTESTATION_SCHEMA_VERSION,
    DEPENDENCY_INVENTORY_NAME,
    IMMUTABLE_DEPENDENCY_LOCK_TARGET,
    IMMUTABLE_DEPENDENCY_INVENTORY_TARGET,
    IMMUTABLE_MIGRATION_MANIFEST_TARGET,
    LABEL_BUILD_BASE_IMAGE,
    MIGRATION_MANIFEST_NAME,
    RELEASE_DEPENDENCY_LOCK_NAME,
    RELEASE_SOURCE_MANIFEST_NAME,
    ReleaseManifestError,
    _docker_image_id,
    _docker_image_labels,
    _docker_image_layers,
    _pinned_local_base_reference,
    _prepare_pinned_local_base,
    _require_image_digest,
    _require_strict_image_layer_prefix,
    build_attestation_labels,
    build_attestation_subject_sha256,
    dependency_inventory_verifier_command,
    sha256_file,
    validate_migration_manifest,
    validate_bundle_payload,
    write_dependency_inventory,
)


class ImmutableBuildError(ValueError):
    pass


def _rootfs_destination(rootfs_dir: Path, target: str) -> Path:
    target_path = Path(target)
    if not target_path.is_absolute() or ".." in target_path.parts:
        raise ImmutableBuildError(
            f"immutable target must be an absolute normalized path: {target}"
        )
    relative_parts = target_path.parts[1:]
    if not relative_parts:
        raise ImmutableBuildError("immutable target cannot be the root path")
    return rootfs_dir.joinpath(*relative_parts)


def resolve_common_base_image(images: list[str]) -> str:
    if not images:
        raise ImmutableBuildError("at least one source image is required")
    current_images = []
    for raw_image in images:
        try:
            image = _require_image_digest(raw_image)
            inspected = _docker_image_id(image)
        except ReleaseManifestError as exc:
            raise ImmutableBuildError(str(exc)) from exc
        if inspected != image:
            raise ImmutableBuildError(
                "source image content ID failed local verification"
            )
        current_images.append(image)

    unique_images = set(current_images)
    layers_by_image = {}
    candidate_sets = []
    for image in current_images:
        try:
            image_layers = layers_by_image.setdefault(
                image,
                _docker_image_layers(image),
            )
            labels = _docker_image_labels(image)
        except ReleaseManifestError as exc:
            raise ImmutableBuildError(str(exc)) from exc
        candidates = {image}
        raw_base = str(labels.get(LABEL_BUILD_BASE_IMAGE) or "").strip()
        if raw_base:
            try:
                labeled_base = _require_image_digest(raw_base)
            except ReleaseManifestError as exc:
                raise ImmutableBuildError(str(exc)) from exc
            try:
                local_labeled_base = _docker_image_id(labeled_base)
            except ReleaseManifestError:
                local_labeled_base = ""
            if local_labeled_base:
                if local_labeled_base != labeled_base:
                    raise ImmutableBuildError(
                        "labeled base image content ID failed local "
                        "verification"
                    )
                try:
                    base_layers = layers_by_image.setdefault(
                        labeled_base,
                        _docker_image_layers(labeled_base),
                    )
                    _require_strict_image_layer_prefix(
                        base_layers,
                        image_layers,
                    )
                except ReleaseManifestError as exc:
                    raise ImmutableBuildError(str(exc)) from exc
                candidates.add(labeled_base)
        candidate_sets.append(candidates)

    common = set.intersection(*candidate_sets)
    if not common:
        raise ImmutableBuildError(
            "source images do not share one available immutable base"
        )
    ranked = sorted(
        common,
        key=lambda image: len(layers_by_image[image]),
    )
    if (
        len(ranked) > 1
        and len(layers_by_image[ranked[0]])
        == len(layers_by_image[ranked[1]])
    ):
        raise ImmutableBuildError(
            "source images have ambiguous common immutable bases"
        )
    return ranked[0]


def _reusable_attested_image(
    attestation_path: Path,
    *,
    build_inputs: dict[str, str],
    build_labels: dict[str, str],
    base_layers: list[str],
    local_base_reference: str,
    expected_base: str,
) -> str | None:
    # A deploy re-run must not invalidate a reviewer trust proof that
    # pins the attestation file hash, so a previous build is reused only
    # when every attested input matches this run and the attested image
    # still verifies locally; any doubt falls back to a fresh build.
    if not attestation_path.is_file():
        return None
    try:
        document = json.loads(
            attestation_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    if set(document) != BUILD_ATTESTATION_REQUIRED_FIELDS:
        return None
    if document.get("schema_version") != (
        BUILD_ATTESTATION_SCHEMA_VERSION
    ):
        return None
    if document.get("build_subject_sha256") != (
        build_attestation_subject_sha256(build_inputs)
    ):
        return None
    if document.get("image_labels") != build_labels:
        return None
    try:
        image_id = _require_image_digest(
            str(document.get("image_digest") or "")
        )
        if _docker_image_id(image_id) != image_id:
            return None
        _require_strict_image_layer_prefix(
            base_layers,
            _docker_image_layers(image_id),
        )
        if _docker_image_id(local_base_reference) != expected_base:
            return None
        actual_labels = _docker_image_labels(image_id)
    except ReleaseManifestError:
        return None
    for key, expected in build_labels.items():
        if actual_labels.get(key) != expected:
            return None
    return image_id


def prepare_build_context(
    bundle_manifest: Path,
    context_dir: Path,
    dependency_lock: Path,
    migration_manifest: Path | None = None,
) -> list[dict[str, str]]:
    context_dir = context_dir.resolve()
    if context_dir.exists() and any(context_dir.iterdir()):
        raise ImmutableBuildError(
            f"build context must be empty: {context_dir}"
        )
    context_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(context_dir, 0o700)
    rootfs_dir = context_dir / "rootfs"
    rootfs_dir.mkdir(mode=0o755)

    files = validate_bundle_payload(
        bundle_manifest,
        bundle_manifest.parent,
        require_transition_runtime=True,
    )
    copied_files = []
    for item in files:
        if not item["bundle_path"].endswith(".py"):
            raise ImmutableBuildError(
                "immutable context accepts Python business modules only"
            )
        if not item["mount_target"].endswith(".py"):
            raise ImmutableBuildError(
                "immutable target must be a Python module path"
            )
        source = bundle_manifest.parent / item["bundle_path"]
        destination = _rootfs_destination(
            rootfs_dir,
            item["mount_target"],
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o644)
        if destination.stat().st_size != source.stat().st_size:
            raise ImmutableBuildError(
                f"context copy size mismatch: {item['bundle_path']}"
            )
        copied_files.append(
            {
                "context_path": destination.relative_to(
                    context_dir
                ).as_posix(),
                "mount_target": item["mount_target"],
                "sha256": item["sha256"],
            }
        )

    if not dependency_lock.is_file():
        raise ImmutableBuildError(
            f"dependency lock is missing: {dependency_lock}"
        )
    if dependency_lock.name != RELEASE_DEPENDENCY_LOCK_NAME:
        raise ImmutableBuildError(
            "dependency lock release path must equal "
            f"{RELEASE_DEPENDENCY_LOCK_NAME}"
        )
    if dependency_lock.resolve() != (
        bundle_manifest.parent / RELEASE_DEPENDENCY_LOCK_NAME
    ).resolve():
        raise ImmutableBuildError(
            "dependency lock must come from the reviewed release payload"
        )
    inventory_release_path = (
        bundle_manifest.parent / DEPENDENCY_INVENTORY_NAME
    )
    write_dependency_inventory(
        dependency_lock,
        inventory_release_path,
    )
    inventory_payload = _rootfs_destination(
        rootfs_dir,
        IMMUTABLE_DEPENDENCY_INVENTORY_TARGET,
    )
    inventory_payload.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(inventory_release_path, inventory_payload)
    os.chmod(inventory_payload, 0o644)
    if sha256_file(inventory_payload) != sha256_file(
        inventory_release_path
    ):
        raise ImmutableBuildError(
            "dependency inventory context copy hash mismatch"
        )
    lock_payload = _rootfs_destination(
        rootfs_dir,
        IMMUTABLE_DEPENDENCY_LOCK_TARGET,
    )
    lock_payload.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(dependency_lock, lock_payload)
    os.chmod(lock_payload, 0o644)
    if sha256_file(lock_payload) != sha256_file(dependency_lock):
        raise ImmutableBuildError("dependency lock context copy hash mismatch")

    selected_migration_manifest = migration_manifest
    if selected_migration_manifest is None:
        selected_migration_manifest = (
            bundle_manifest.parent / MIGRATION_MANIFEST_NAME
        )
    if not selected_migration_manifest.is_file():
        raise ImmutableBuildError(
            "migration manifest is missing: "
            f"{selected_migration_manifest}"
        )
    validate_migration_manifest(
        selected_migration_manifest,
        payload_root=bundle_manifest.parent,
    )
    migration_payload = _rootfs_destination(
        rootfs_dir,
        IMMUTABLE_MIGRATION_MANIFEST_TARGET,
    )
    migration_payload.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(selected_migration_manifest, migration_payload)
    os.chmod(migration_payload, 0o644)
    if sha256_file(migration_payload) != sha256_file(
        selected_migration_manifest
    ):
        raise ImmutableBuildError(
            "migration manifest context copy hash mismatch"
        )
    for directory in rootfs_dir.rglob("*"):
        if directory.is_dir():
            os.chmod(directory, 0o755)

    dockerfile_lines = [
        "COPY "
        + json.dumps(
            ["rootfs/", "/"],
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        "RUN "
        + json.dumps(
            dependency_inventory_verifier_command(
                IMMUTABLE_DEPENDENCY_INVENTORY_TARGET
            ),
            ensure_ascii=True,
            separators=(",", ":"),
        ),
    ]

    (context_dir / "Dockerfile.body").write_text(
        "\n".join(dockerfile_lines) + "\n",
        encoding="utf-8",
    )
    return copied_files


def build_immutable_image(
    *,
    bundle_manifest: Path,
    dependency_lock: Path,
    base_image: str,
    iid_output: Path | None = None,
    migration_manifest: Path | None = None,
    attestation_output: Path | None = None,
) -> str:
    expected_base = _require_image_digest(base_image)
    reviewed_bundle_manifest = bundle_manifest.resolve()
    reviewed_root = reviewed_bundle_manifest.parent
    release_source_manifest = (
        reviewed_root / RELEASE_SOURCE_MANIFEST_NAME
    )
    if not release_source_manifest.is_file():
        raise ImmutableBuildError(
            "release source manifest is missing from reviewed payload"
        )
    with tempfile.TemporaryDirectory(
        prefix="trader-node-immutable-",
    ) as raw_context:
        context_dir = Path(raw_context)
        copied_files = prepare_build_context(
            reviewed_bundle_manifest,
            context_dir,
            dependency_lock.resolve(),
            migration_manifest.resolve()
            if migration_manifest is not None
            else None,
        )
        try:
            local_base = _docker_image_id(expected_base)
        except ReleaseManifestError as exc:
            raise ImmutableBuildError(str(exc)) from exc
        if local_base != expected_base:
            raise ImmutableBuildError(
                "local base image content ID differs from requested base"
            )
        try:
            base_layers = _docker_image_layers(expected_base)
            local_base_reference = _prepare_pinned_local_base(
                expected_base,
                image_id_resolver=_docker_image_id,
            )
        except ReleaseManifestError as exc:
            raise ImmutableBuildError(str(exc)) from exc
        body_path = context_dir / "Dockerfile.body"
        body = body_path.read_text(encoding="utf-8")
        dockerfile = context_dir / "Dockerfile"
        dockerfile.write_text(
            f"FROM {local_base_reference}\n{body}",
            encoding="utf-8",
        )
        selected_migration_manifest = migration_manifest
        if selected_migration_manifest is None:
            selected_migration_manifest = (
                reviewed_root / MIGRATION_MANIFEST_NAME
            )
        selected_migration_manifest = selected_migration_manifest.resolve()
        dependency_inventory = (
            reviewed_root / DEPENDENCY_INVENTORY_NAME
        )
        build_inputs = {
            "base_image_digest": expected_base,
            "dockerfile_sha256": sha256_file(dockerfile),
            "bundle_manifest_sha256": sha256_file(
                reviewed_bundle_manifest
            ),
            "release_source_manifest_sha256": sha256_file(
                release_source_manifest
            ),
            "dependency_lock_sha256": sha256_file(
                dependency_lock.resolve()
            ),
            "dependency_inventory_sha256": sha256_file(
                dependency_inventory
            ),
            "migration_manifest_sha256": sha256_file(
                selected_migration_manifest
            ),
        }
        build_labels = build_attestation_labels(build_inputs)
        selected_attestation_output = attestation_output
        if selected_attestation_output is None:
            selected_attestation_output = (
                reviewed_root / BUILD_ATTESTATION_NAME
            )
        reused_image_id = _reusable_attested_image(
            selected_attestation_output,
            build_inputs=build_inputs,
            build_labels=build_labels,
            base_layers=base_layers,
            local_base_reference=local_base_reference,
            expected_base=expected_base,
        )
        if reused_image_id is not None:
            image_id = reused_image_id
        else:
            body_path.unlink()
            iid_file = context_dir / "image-id"
            command = [
                "docker",
                "build",
                "--network=none",
                "--pull=false",
                "--no-cache",
                "--iidfile",
                str(iid_file),
            ]
            for key, value in sorted(build_labels.items()):
                command.extend(["--label", f"{key}={value}"])
            command.extend(
                [
                    "--file",
                    str(dockerfile),
                    str(context_dir),
                ]
            )
            try:
                subprocess.run(command, check=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise ImmutableBuildError(
                    "docker immutable image build failed"
                ) from exc
            if not iid_file.is_file():
                raise ImmutableBuildError(
                    "docker build did not write an image ID"
                )
            try:
                image_id = _require_image_digest(
                    iid_file.read_text(encoding="utf-8").strip()
                )
                inspected_id = _docker_image_id(image_id)
            except ReleaseManifestError as exc:
                raise ImmutableBuildError(str(exc)) from exc
            if inspected_id != image_id:
                raise ImmutableBuildError(
                    "built image content ID failed local verification"
                )
            try:
                built_layers = _docker_image_layers(image_id)
                _require_strict_image_layer_prefix(
                    base_layers,
                    built_layers,
                )
                alias_id = _docker_image_id(local_base_reference)
            except ReleaseManifestError as exc:
                raise ImmutableBuildError(str(exc)) from exc
            if alias_id != expected_base:
                raise ImmutableBuildError(
                    "pinned local base image reference changed after build"
                )
            try:
                actual_labels = _docker_image_labels(image_id)
            except ReleaseManifestError as exc:
                raise ImmutableBuildError(str(exc)) from exc
            for key, expected in build_labels.items():
                if actual_labels.get(key) != expected:
                    raise ImmutableBuildError(
                        f"built image label verification failed: {key}"
                    )

    if iid_output is not None:
        iid_output.parent.mkdir(parents=True, exist_ok=True)
        iid_output.write_text(f"{image_id}\n", encoding="utf-8")
    if reused_image_id is not None:
        print(
            "REUSED immutable node image"
            f" image={image_id} base={expected_base}"
            f" files={len(copied_files)}"
        )
        return image_id
    attestation = {
        "schema_version": BUILD_ATTESTATION_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_image_digest": build_inputs["base_image_digest"],
        "dockerfile_sha256": build_inputs["dockerfile_sha256"],
        "bundle_manifest": {
            "path": "bundle-manifest.json",
            "sha256": build_inputs["bundle_manifest_sha256"],
        },
        "release_source_manifest": {
            "path": RELEASE_SOURCE_MANIFEST_NAME,
            "sha256": build_inputs[
                "release_source_manifest_sha256"
            ],
        },
        "dependency_lock": {
            "path": RELEASE_DEPENDENCY_LOCK_NAME,
            "sha256": build_inputs["dependency_lock_sha256"],
        },
        "dependency_inventory": {
            "path": DEPENDENCY_INVENTORY_NAME,
            "sha256": build_inputs[
                "dependency_inventory_sha256"
            ],
        },
        "migration_manifest": {
            "path": MIGRATION_MANIFEST_NAME,
            "sha256": build_inputs["migration_manifest_sha256"],
        },
        "build_subject_sha256": build_attestation_subject_sha256(
            build_inputs
        ),
        "image_digest": image_id,
        "image_labels": build_labels,
    }
    selected_attestation_output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    selected_attestation_output.write_text(
        json.dumps(attestation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "BUILT immutable node image"
        f" image={image_id} base={expected_base}"
        f" files={len(copied_files)}"
    )
    return image_id


def main(argv: list[str] | None = None) -> int:
    raw_args = list(argv if argv is not None else sys.argv[1:])
    if raw_args and raw_args[0] == "resolve-common-base":
        resolver = argparse.ArgumentParser(
            description="Resolve one verified base for current node images."
        )
        resolver.add_argument("resolve-common-base")
        resolver.add_argument(
            "--image",
            action="append",
            required=True,
            dest="images",
        )
        args = resolver.parse_args(raw_args)
        try:
            print(resolve_common_base_image(args.images))
        except (
            ImmutableBuildError,
            OSError,
            ReleaseManifestError,
        ) as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            return 2
        return 0

    parser = argparse.ArgumentParser(
        description="Build a derived immutable node image with no network."
    )
    parser.add_argument("--bundle-manifest", type=Path, required=True)
    parser.add_argument("--dependency-lock", type=Path, required=True)
    parser.add_argument("--migration-manifest", type=Path)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--iid-output", type=Path)
    parser.add_argument("--attestation-output", type=Path)
    args = parser.parse_args(raw_args)

    try:
        build_immutable_image(
            bundle_manifest=args.bundle_manifest,
            dependency_lock=args.dependency_lock,
            base_image=args.base_image,
            iid_output=args.iid_output,
            migration_manifest=args.migration_manifest,
            attestation_output=args.attestation_output,
        )
    except (
        ImmutableBuildError,
        OSError,
        ReleaseManifestError,
    ) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
