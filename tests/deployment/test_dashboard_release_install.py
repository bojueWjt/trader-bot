"""Exercise the existing deployment functions against isolated filesystem trees."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts/hk-deploy-20260803.sh"
FUNCTIONS = (
    "dashboard_payload_action", "verify_dashboard_payload", "backup_dashboard_payload",
    "install_dashboard_payload", "verify_dashboard_live", "restore_dashboard_payload",
)


def _function(name: str) -> str:
    source = DEPLOY.read_text()
    start = source.index(f"{name}() {{")
    return source[start:source.index("\n}\n", start) + 3]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def deployment(tmp_path: Path) -> dict[str, Path]:
    paths = {name: tmp_path / name for name in ("staging", "target", "backup", "bin")}
    for path in paths.values():
        path.mkdir()
    (paths["bin"] / "python3").symlink_to(sys.executable)
    curl = paths["bin"] / "curl"
    curl.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
from urllib.parse import urlsplit
with open(os.environ["DASHBOARD_HTTP_LOG"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("DASHBOARD_HTTP_STALE") == "1":
    sys.stdout.buffer.write(b"a different dashboard returned with HTTP 200")
    raise SystemExit(0)
url = urlsplit(sys.argv[-1])
if url.scheme != "https" or url.hostname != "jp-bot.balen.wang":
    raise SystemExit(2)
path = Path(os.environ["DASHBOARD_HTTP_ROOT"]) / (url.path.lstrip("/") or "index.html")
if not path.is_file():
    raise SystemExit(22)
sys.stdout.buffer.write(path.read_bytes())
''')
    curl.chmod(0o755)
    shutil.copy2(ROOT / "scripts/release_manifest.py", paths["staging"] / "release_manifest.py")
    dist = paths["staging"] / "dashboard/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<html><script src="/assets/new.js"></script></html>')
    (dist / "assets/new.js").write_text('console.log("reviewed build");')
    manifest = {
        "schema_version": "trader-v3-dashboard/v1", "source_commit": "a" * 40,
        "source_tree": "b" * 40, "source_subdir": "bridge/apps/dashboard",
        "build_inputs": ["bridge/apps/dashboard", "tests/nautilus/_fixtures/contracts-v1/data_quality_envelope_v1.schema.json"],
        "package_lock_sha256": "c" * 64, "tool_versions": {"node": "v22.12.0", "npm": "10.9.0"},
        "api_base": "", "base_path": "/", "auth_disabled": False, "operator_path": "/m/v1/operator/orders",
        "files": {p.relative_to(paths["staging"]).as_posix(): _sha(p) for p in dist.rglob("*") if p.is_file()},
    }
    (paths["staging"] / "dashboard-manifest.json").write_text(json.dumps(manifest))
    _bind_manifest(paths)
    old = paths["target"] / "dashboard/dist"
    old.mkdir(parents=True)
    (old / "index.html").write_text("previous dashboard")
    (old / "old.js").write_text("previous asset")
    return paths


def _bind_manifest(paths: dict[str, Path]) -> None:
    (paths["staging"] / "release-source-manifest.json").write_text(json.dumps({
        "source_commit": "a" * 40, "source_tree": "b" * 40,
        "dashboard": {"manifest": "dashboard-manifest.json", "manifest_sha256": _sha(paths["staging"] / "dashboard-manifest.json")},
    }))


def _run(paths: dict[str, Path], commands: str, *, stale_http: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{paths['bin']}:{env['PATH']}"
    env["DASHBOARD_HTTP_ROOT"] = str(paths["target"] / "dashboard/dist")
    env["DASHBOARD_HTTP_LOG"] = str(paths["target"].parent / "http.jsonl")
    env["DASHBOARD_HTTP_STALE"] = "1" if stale_http else "0"
    source = "set -Eeuo pipefail\n" + "\n".join(_function(name) for name in FUNCTIONS)
    source += "\n" + "\n".join(
        f"{name}={shlex.quote(str(paths[key]))}"
        for name, key in (("STAGING", "staging"), ("T", "target"), ("BACKUP_ROOT", "backup"))
    )
    return subprocess.run(["bash", "-c", source + "\n" + commands], text=True, capture_output=True, env=env, timeout=20)


def test_install_verifies_exact_tree_and_restore_preserves_previous_build(deployment):
    result = _run(deployment, "verify_dashboard_payload\nbackup_dashboard_payload\ninstall_dashboard_payload\nverify_dashboard_live")
    assert result.returncode == 0, result.stderr
    live = deployment["target"] / "dashboard/dist"
    assert (live / "index.html").read_bytes() == (deployment["staging"] / "dashboard/dist/index.html").read_bytes()
    assert not (live / "old.js").exists()
    assert (deployment["backup"] / "dashboard-dist/old.js").read_text() == "previous asset"
    # Rollback uses captured bytes, even when the proposed artifact becomes unreadable.
    shutil.rmtree(deployment["staging"] / "dashboard/dist")
    restored = _run(deployment, "restore_dashboard_payload")
    assert restored.returncode == 0, restored.stderr
    assert (live / "index.html").read_text() == "previous dashboard"
    assert (live / "old.js").read_text() == "previous asset"
    assert not (live / "assets/new.js").exists()


@pytest.mark.parametrize("mutation", ["asset", "extra", "missing_index", "commit", "api_base", "auth_disabled", "manifest_hash"])
def test_invalid_candidate_fails_before_touching_existing_dashboard(deployment, mutation):
    staging = deployment["staging"]
    manifest_path = staging / "dashboard-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation == "asset":
        (staging / "dashboard/dist/assets/new.js").write_text("tampered")
    elif mutation == "extra":
        (staging / "dashboard/dist/unlisted.js").write_text("unlisted")
    elif mutation == "missing_index":
        (staging / "dashboard/dist/index.html").unlink()
    elif mutation == "commit":
        manifest["source_commit"] = "d" * 40
    elif mutation == "api_base":
        manifest["api_base"] = "https://unreviewed.invalid"
    elif mutation == "auth_disabled":
        manifest["auth_disabled"] = True
    else:
        manifest["package_lock_sha256"] = "d" * 64
    manifest_path.write_text(json.dumps(manifest))
    if mutation != "manifest_hash":
        _bind_manifest(deployment)
    result = _run(deployment, "verify_dashboard_payload\nbackup_dashboard_payload\ninstall_dashboard_payload")
    assert result.returncode != 0
    assert (deployment["target"] / "dashboard/dist/index.html").read_text() == "previous dashboard"
    assert list(deployment["backup"].iterdir()) == []


@pytest.mark.parametrize("link_part", ["dashboard", "dashboard/dist"])
def test_target_symlink_is_rejected_without_writing_through_it(deployment, link_part):
    target = deployment["target"] / link_part
    redirected = deployment["target"].parent / "redirected"
    target.rename(redirected)
    target.symlink_to(redirected, target_is_directory=True)
    result = _run(deployment, "verify_dashboard_payload\ninstall_dashboard_payload")
    assert result.returncode != 0
    assert "unsafe dashboard target" in result.stderr
    assert target.is_symlink()
    index = redirected / ("dist/index.html" if link_part == "dashboard" else "index.html")
    assert index.read_text() == "previous dashboard"


def test_live_verification_detects_changed_served_bytes(deployment):
    assert _run(deployment, "install_dashboard_payload").returncode == 0
    (deployment["target"] / "dashboard/dist/index.html").write_text("stale or changed build")
    result = _run(deployment, "verify_dashboard_live")
    assert result.returncode != 0
    assert "hash mismatch" in result.stderr


def test_live_http_200_with_different_page_fails_verification(deployment):
    assert _run(deployment, "install_dashboard_payload").returncode == 0
    result = _run(deployment, "verify_dashboard_live", stale_http=True)
    assert result.returncode != 0
    assert "mismatch" in result.stderr


def test_live_http_verifies_page_and_assets_without_auth_or_service_changes(deployment):
    result = _run(deployment, "install_dashboard_payload\nverify_dashboard_live")
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (deployment["target"].parent / "http.jsonl").read_text().splitlines()]
    urls = {call[-1] for call in calls}
    assert "https://jp-bot.balen.wang/" in urls
    assert "https://jp-bot.balen.wang/assets/new.js" in urls
    for call in calls:
        assert "--resolve" in call and "jp-bot.balen.wang:443:127.0.0.1" in call
        assert "--max-time" in call
        assert not {"-H", "--header", "-X", "--request", "-d", "--data", "-L", "--location"}.intersection(call)


def test_first_install_rollback_restores_absence(deployment):
    live = deployment["target"] / "dashboard/dist"
    shutil.rmtree(live)
    result = _run(deployment, "backup_dashboard_payload\ninstall_dashboard_payload\nrestore_dashboard_payload")
    assert result.returncode == 0, result.stderr
    assert not live.exists()
    assert (deployment["backup"] / "dashboard-dist.absent").is_file()


def test_dashboard_gate_and_backup_precede_node_stop():
    source = DEPLOY.read_text()
    stop = source.index("\nstop_recreate_nodes\n")
    assert source.index("\nverify_dashboard_payload\n") < stop
    assert source.index("\nbackup_dashboard_payload\n") < stop
    assert source.index("\ninstall_dashboard_payload\n") > stop
