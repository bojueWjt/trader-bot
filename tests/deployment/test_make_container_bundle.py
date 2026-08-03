from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "make_container_bundle.py"
SPEC = importlib.util.spec_from_file_location("make_container_bundle", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


def _seed_sources(root: Path) -> None:
    for bundle_name, source_relative, _mount_target in bundle.BUNDLE_FILES:
        source = root / source_relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"source={source_relative}\nbundle={bundle_name}\n")


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
    assert len(manifest["files"]) == len(bundle.BUNDLE_FILES)
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
