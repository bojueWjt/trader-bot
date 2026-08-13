#!/usr/bin/env python3
"""Build a reviewed Telegram watcher overlay without network access."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from release_manifest import (
    ReleaseManifestError,
    _docker_image_id,
    _docker_image_labels,
    _docker_image_layers,
    _pinned_local_base_reference,
    _prepare_pinned_local_base,
    _require_image_digest,
    _require_strict_image_layer_prefix,
    sha256_file,
)


class ImmutableWatcherBuildError(ValueError):
    pass


WATCHER_RUNTIME_RELATIVE_PATHS = (
    "ecosystem.config.js",
    "lib/env-flags.js",
    "lib/hermes-cron.js",
    "lib/safe-log.js",
    "lib/signal-importer.js",
    "lib/telegram-proxy.js",
    "lib/telegram-utils.js",
    "lib/trading-api.js",
    "lib/watched-entry-routing.js",
    "package-lock.json",
    "package.json",
    "price-monitor.js",
    "public/index.html",
    "scripts/configure-remote-importer.sh",
    "server.js",
    "skills/crypto-trader/SKILL.md",
    "skills/crypto-trader/references/deployment-guide.md",
    "skills/crypto-trader/references/position-sizing.md",
    "skills/crypto-trader/references/signal-examples.md",
    "skills/crypto-trader/scripts/2026-02-10-c0fbef.html",
    "skills/crypto-trader/scripts/binance_trade.py",
    "skills/crypto-trader/scripts/briefing.py",
    "skills/crypto-trader/scripts/db_manager.py",
    "skills/crypto-trader/scripts/generate_report_html.py",
    "skills/crypto-trader/scripts/report_image.py",
    "skills/crypto-trader/scripts/report_renderer.py",
    "skills/crypto-trader/scripts/upload_report.sh",
    "skills/crypto-trader/scripts/valuescan_scraper.py",
)
WATCHER_RELEASE_FILES = tuple(
    (
        f"bridge/services/telegram-watcher/{relative}",
        f"watcher/{relative}",
        f"/app/{relative}",
    )
    for relative in WATCHER_RUNTIME_RELATIVE_PATHS
)
WATCHER_RUNTIME_MANIFEST_NAME = "watcher-runtime-manifest.json"
WATCHER_RUNTIME_MANIFEST_SCHEMA_VERSION = (
    "trader-v3-watcher-runtime-manifest/v1"
)
WATCHER_BUILD_ATTESTATION_NAME = (
    "immutable-watcher-build-attestation.json"
)
WATCHER_BUILD_ATTESTATION_SCHEMA_VERSION = (
    "trader-v3-watcher-build-attestation/v1"
)
LABEL_PREFIX = "org.trader.account-stall.watcher"
_EXPECTED_RELEASE_TARGETS = {
    release_path: target_path
    for _source_path, release_path, target_path in WATCHER_RELEASE_FILES
}


def _payload_subject_sha256(files: list[dict[str, object]]) -> str:
    subject = [
        {
            "release_path": str(item["release_path"]),
            "target_path": str(item["target_path"]),
            "sha256": str(item["sha256"]),
        }
        for item in files
    ]
    encoded = json.dumps(
        subject,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_watcher_runtime_manifest(
    manifest_path: Path,
    *,
    payload_root: Path | None = None,
) -> list[dict[str, object]]:
    root = payload_root or manifest_path.parent
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImmutableWatcherBuildError(
            "watcher runtime manifest is unreadable"
        ) from exc
    if manifest.get("schema_version") != (
        WATCHER_RUNTIME_MANIFEST_SCHEMA_VERSION
    ):
        raise ImmutableWatcherBuildError(
            "watcher runtime manifest schema mismatch"
        )
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise ImmutableWatcherBuildError(
            "watcher runtime manifest files must be a list"
        )
    files: list[dict[str, object]] = []
    release_targets: dict[str, str] = {}
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise ImmutableWatcherBuildError(
                "watcher runtime manifest file entry is invalid"
            )
        release_path = str(raw.get("release_path") or "")
        target_path = str(raw.get("target_path") or "")
        digest = str(raw.get("sha256") or "")
        size = raw.get("size")
        if release_path in release_targets:
            raise ImmutableWatcherBuildError(
                "watcher runtime manifest has duplicate paths"
            )
        if _EXPECTED_RELEASE_TARGETS.get(release_path) != target_path:
            raise ImmutableWatcherBuildError(
                "watcher runtime target exact-set mismatch"
            )
        source = root / release_path
        if not source.is_file():
            raise ImmutableWatcherBuildError(
                f"watcher runtime payload is missing: {release_path}"
            )
        if sha256_file(source) != digest:
            raise ImmutableWatcherBuildError(
                f"watcher runtime payload hash mismatch: {release_path}"
            )
        if not isinstance(size, int) or size != source.stat().st_size:
            raise ImmutableWatcherBuildError(
                f"watcher runtime payload size mismatch: {release_path}"
            )
        release_targets[release_path] = target_path
        files.append(
            {
                "release_path": release_path,
                "target_path": target_path,
                "sha256": digest,
                "size": size,
            }
        )
    if release_targets != _EXPECTED_RELEASE_TARGETS:
        raise ImmutableWatcherBuildError(
            "watcher runtime payload exact-set mismatch"
        )
    expected_subject = _payload_subject_sha256(files)
    if manifest.get("payload_subject_sha256") != expected_subject:
        raise ImmutableWatcherBuildError(
            "watcher runtime payload subject hash mismatch"
        )
    return files


def _base_dependency_hashes(
    files: list[dict[str, object]],
) -> dict[str, str]:
    by_path = {
        str(item["release_path"]): str(item["sha256"])
        for item in files
    }
    return {
        "/app/package.json": by_path["watcher/package.json"],
        "/app/package-lock.json": by_path["watcher/package-lock.json"],
    }


def prepare_build_context(
    manifest_path: Path,
    context_dir: Path,
) -> list[dict[str, object]]:
    if context_dir.exists() and any(context_dir.iterdir()):
        raise ImmutableWatcherBuildError(
            f"watcher build context must be empty: {context_dir}"
        )
    context_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(context_dir, 0o700)
    payload_dir = context_dir / "payload"
    payload_dir.mkdir(mode=0o700)
    files = validate_watcher_runtime_manifest(manifest_path)
    dockerfile_lines = []
    for target_path, digest in _base_dependency_hashes(files).items():
        dockerfile_lines.append(
            f"RUN echo '{digest}  {target_path}' | sha256sum -c -"
        )
    for index, item in enumerate(files):
        context_name = f"{index:04d}"
        source = manifest_path.parent / str(item["release_path"])
        destination = payload_dir / context_name
        shutil.copyfile(source, destination)
        # COPY preserves context file modes and the image runs as a
        # non-root user; pin 0o644 so the caller's umask cannot produce
        # unreadable in-image payloads.
        os.chmod(destination, 0o644)
        if sha256_file(destination) != item["sha256"]:
            raise ImmutableWatcherBuildError(
                f"watcher context copy hash mismatch: {item['release_path']}"
            )
        dockerfile_lines.append(
            "COPY "
            + json.dumps(
                [f"payload/{context_name}", item["target_path"]],
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    (context_dir / "Dockerfile.body").write_text(
        "\n".join(dockerfile_lines) + "\n",
        encoding="utf-8",
    )
    return files


def _require_release_source_contract(
    manifest_path: Path,
) -> Path:
    source_manifest_path = (
        manifest_path.parent / "release-source-manifest.json"
    )
    try:
        source_manifest = json.loads(
            source_manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ImmutableWatcherBuildError(
            "release source manifest is unreadable"
        ) from exc
    contract = source_manifest.get("watcher_runtime")
    if not isinstance(contract, dict):
        raise ImmutableWatcherBuildError(
            "release source manifest lacks watcher runtime contract"
        )
    if contract.get("manifest") != manifest_path.name:
        raise ImmutableWatcherBuildError(
            "release source watcher manifest path mismatch"
        )
    if contract.get("manifest_sha256") != sha256_file(manifest_path):
        raise ImmutableWatcherBuildError(
            "release source watcher manifest hash mismatch"
        )
    return source_manifest_path


def build_immutable_watcher_image(
    *,
    manifest_path: Path,
    base_image: str,
    iid_output: Path | None = None,
    attestation_output: Path | None = None,
) -> str:
    expected_base = _require_image_digest(base_image)
    reviewed_manifest = manifest_path.resolve()
    source_manifest_path = _require_release_source_contract(
        reviewed_manifest
    )
    with tempfile.TemporaryDirectory(
        prefix="trader-watcher-immutable-",
    ) as raw_context:
        context_dir = Path(raw_context)
        files = prepare_build_context(reviewed_manifest, context_dir)
        try:
            local_base = _docker_image_id(expected_base)
        except ReleaseManifestError as exc:
            raise ImmutableWatcherBuildError(str(exc)) from exc
        if local_base != expected_base:
            raise ImmutableWatcherBuildError(
                "local watcher base image differs from requested digest"
            )
        try:
            base_layers = _docker_image_layers(expected_base)
            local_base_reference = _prepare_pinned_local_base(
                expected_base,
                image_id_resolver=_docker_image_id,
            )
        except ReleaseManifestError as exc:
            raise ImmutableWatcherBuildError(str(exc)) from exc
        body_path = context_dir / "Dockerfile.body"
        dockerfile = context_dir / "Dockerfile"
        dockerfile.write_text(
            f"FROM {local_base_reference}\n"
            + body_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        build_inputs = {
            "base_image_digest": expected_base,
            "dockerfile_sha256": sha256_file(dockerfile),
            "watcher_manifest_sha256": sha256_file(reviewed_manifest),
            "release_source_manifest_sha256": sha256_file(
                source_manifest_path
            ),
            "payload_subject_sha256": _payload_subject_sha256(files),
        }
        labels = {
            f"{LABEL_PREFIX}.base-image": expected_base,
            f"{LABEL_PREFIX}.dockerfile-sha256": build_inputs[
                "dockerfile_sha256"
            ],
            f"{LABEL_PREFIX}.manifest-sha256": build_inputs[
                "watcher_manifest_sha256"
            ],
            f"{LABEL_PREFIX}.source-manifest-sha256": build_inputs[
                "release_source_manifest_sha256"
            ],
            f"{LABEL_PREFIX}.payload-subject-sha256": build_inputs[
                "payload_subject_sha256"
            ],
        }
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
        for key, value in sorted(labels.items()):
            command.extend(["--label", f"{key}={value}"])
        command.extend(
            ["--file", str(dockerfile), str(context_dir)]
        )
        try:
            subprocess.run(command, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ImmutableWatcherBuildError(
                "docker immutable watcher build failed"
            ) from exc
        if not iid_file.is_file():
            raise ImmutableWatcherBuildError(
                "docker watcher build did not write an image ID"
            )
        try:
            image_id = _require_image_digest(
                iid_file.read_text(encoding="utf-8").strip()
            )
            inspected_id = _docker_image_id(image_id)
        except ReleaseManifestError as exc:
            raise ImmutableWatcherBuildError(str(exc)) from exc
        if inspected_id != image_id:
            raise ImmutableWatcherBuildError(
                "built watcher image failed local identity verification"
            )
        try:
            built_layers = _docker_image_layers(image_id)
            _require_strict_image_layer_prefix(
                base_layers,
                built_layers,
            )
            alias_id = _docker_image_id(local_base_reference)
        except ReleaseManifestError as exc:
            raise ImmutableWatcherBuildError(str(exc)) from exc
        if alias_id != expected_base:
            raise ImmutableWatcherBuildError(
                "pinned local watcher base image changed after build"
            )
        try:
            actual_labels = _docker_image_labels(image_id)
        except ReleaseManifestError as exc:
            raise ImmutableWatcherBuildError(str(exc)) from exc
        for key, expected in labels.items():
            if actual_labels.get(key) != expected:
                raise ImmutableWatcherBuildError(
                    f"built watcher image label mismatch: {key}"
                )
    if iid_output is not None:
        iid_output.parent.mkdir(parents=True, exist_ok=True)
        iid_output.write_text(f"{image_id}\n", encoding="utf-8")
    selected_attestation = attestation_output
    if selected_attestation is None:
        selected_attestation = (
            reviewed_manifest.parent / WATCHER_BUILD_ATTESTATION_NAME
        )
    attestation = {
        "schema_version": WATCHER_BUILD_ATTESTATION_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **build_inputs,
        "image_digest": image_id,
        "image_labels": labels,
    }
    selected_attestation.parent.mkdir(parents=True, exist_ok=True)
    selected_attestation.write_text(
        json.dumps(attestation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return image_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a reviewed immutable Telegram watcher overlay."
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--base-image")
    parser.add_argument("--iid-output", type=Path)
    parser.add_argument("--attestation-output", type=Path)
    parser.add_argument(
        "--validate-runtime-manifest",
        type=Path,
        help=(
            "Validate watcher runtime payload exact-set and hashes without "
            "building an image."
        ),
    )
    args = parser.parse_args(argv)
    try:
        if args.validate_runtime_manifest is not None:
            _require_release_source_contract(
                args.validate_runtime_manifest.resolve()
            )
            validate_watcher_runtime_manifest(
                args.validate_runtime_manifest
            )
            return 0
        if args.manifest is None:
            parser.error("--manifest is required unless validating only")
        if args.base_image is None:
            parser.error("--base-image is required unless validating only")
        build_immutable_watcher_image(
            manifest_path=args.manifest,
            base_image=args.base_image,
            iid_output=args.iid_output,
            attestation_output=args.attestation_output,
        )
    except (
        ImmutableWatcherBuildError,
        OSError,
        ReleaseManifestError,
    ) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
