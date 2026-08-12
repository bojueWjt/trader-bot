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
    BUILD_ATTESTATION_SCHEMA_VERSION,
    DEPENDENCY_INVENTORY_NAME,
    IMMUTABLE_DEPENDENCY_LOCK_TARGET,
    IMMUTABLE_DEPENDENCY_INVENTORY_TARGET,
    IMMUTABLE_MIGRATION_MANIFEST_TARGET,
    MIGRATION_MANIFEST_NAME,
    RELEASE_DEPENDENCY_LOCK_NAME,
    RELEASE_SOURCE_MANIFEST_NAME,
    ReleaseManifestError,
    _docker_image_labels,
    _require_image_digest,
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
        raise ImmutableBuildError(
            f"local base image is unavailable: {image}"
        ) from exc
    try:
        return _require_image_digest(output.strip())
    except ReleaseManifestError as exc:
        raise ImmutableBuildError(
            "docker returned an invalid base image ID"
        ) from exc


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
    payload_dir = context_dir / "payload"
    payload_dir.mkdir(mode=0o700)

    files = validate_bundle_payload(
        bundle_manifest,
        bundle_manifest.parent,
        require_transition_runtime=True,
    )
    dockerfile_lines = []
    copied_files = []
    for index, item in enumerate(files):
        if not item["bundle_path"].endswith(".py"):
            raise ImmutableBuildError(
                "immutable context accepts Python business modules only"
            )
        if not item["mount_target"].endswith(".py"):
            raise ImmutableBuildError(
                "immutable target must be a Python module path"
            )
        context_name = f"{index:04d}"
        source = bundle_manifest.parent / item["bundle_path"]
        destination = payload_dir / context_name
        shutil.copyfile(source, destination)
        if destination.stat().st_size != source.stat().st_size:
            raise ImmutableBuildError(
                f"context copy size mismatch: {item['bundle_path']}"
            )
        copied_files.append(
            {
                "context_path": f"payload/{context_name}",
                "mount_target": item["mount_target"],
                "sha256": item["sha256"],
            }
        )
        dockerfile_lines.append(
            "COPY "
            + json.dumps(
                [f"payload/{context_name}", item["mount_target"]],
                ensure_ascii=True,
                separators=(",", ":"),
            )
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
    inventory_payload = payload_dir / "dependency-inventory.json"
    shutil.copyfile(inventory_release_path, inventory_payload)
    if sha256_file(inventory_payload) != sha256_file(
        inventory_release_path
    ):
        raise ImmutableBuildError(
            "dependency inventory context copy hash mismatch"
        )
    lock_payload = payload_dir / "dependency.lock"
    shutil.copyfile(dependency_lock, lock_payload)
    if sha256_file(lock_payload) != sha256_file(dependency_lock):
        raise ImmutableBuildError("dependency lock context copy hash mismatch")
    dockerfile_lines.append(
        "COPY "
        + json.dumps(
            [
                "payload/dependency.lock",
                IMMUTABLE_DEPENDENCY_LOCK_TARGET,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    dockerfile_lines.append(
        "COPY "
        + json.dumps(
            [
                "payload/dependency-inventory.json",
                IMMUTABLE_DEPENDENCY_INVENTORY_TARGET,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )

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
    migration_payload = payload_dir / MIGRATION_MANIFEST_NAME
    shutil.copyfile(selected_migration_manifest, migration_payload)
    if sha256_file(migration_payload) != sha256_file(
        selected_migration_manifest
    ):
        raise ImmutableBuildError(
            "migration manifest context copy hash mismatch"
        )
    dockerfile_lines.append(
        "COPY "
        + json.dumps(
            [
                f"payload/{MIGRATION_MANIFEST_NAME}",
                IMMUTABLE_MIGRATION_MANIFEST_TARGET,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    dockerfile_lines.append(
        "RUN "
        + json.dumps(
            dependency_inventory_verifier_command(
                IMMUTABLE_DEPENDENCY_INVENTORY_TARGET
            ),
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )

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
        local_base = _docker_image_id(expected_base)
        if local_base != expected_base:
            raise ImmutableBuildError(
                "local base image content ID differs from requested base"
            )
        body_path = context_dir / "Dockerfile.body"
        body = body_path.read_text(encoding="utf-8")
        dockerfile = context_dir / "Dockerfile"
        dockerfile.write_text(
            f"FROM {expected_base}\n{body}",
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
        image_id = _require_image_digest(
            iid_file.read_text(encoding="utf-8").strip()
        )
        inspected_id = _docker_image_id(image_id)
        if inspected_id != image_id:
            raise ImmutableBuildError(
                "built image content ID failed local verification"
            )
        actual_labels = _docker_image_labels(image_id)
        for key, expected in build_labels.items():
            if actual_labels.get(key) != expected:
                raise ImmutableBuildError(
                    f"built image label verification failed: {key}"
                )

    if iid_output is not None:
        iid_output.parent.mkdir(parents=True, exist_ok=True)
        iid_output.write_text(f"{image_id}\n", encoding="utf-8")
    selected_attestation_output = attestation_output
    if selected_attestation_output is None:
        selected_attestation_output = (
            reviewed_root / BUILD_ATTESTATION_NAME
        )
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
    parser = argparse.ArgumentParser(
        description="Build a derived immutable node image with no network."
    )
    parser.add_argument("--bundle-manifest", type=Path, required=True)
    parser.add_argument("--dependency-lock", type=Path, required=True)
    parser.add_argument("--migration-manifest", type=Path)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--iid-output", type=Path)
    parser.add_argument("--attestation-output", type=Path)
    args = parser.parse_args(argv)

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
