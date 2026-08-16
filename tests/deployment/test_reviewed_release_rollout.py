from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import release_manifest
import reviewed_release_rollout

REDIS_FENCING_EPOCH = "123e4567-e89b-42d3-a456-426614174000"
REDIS_FENCING_EPOCH_KEY = "trader-bot:redis-fencing-epoch"


def _write_node_configs(root: Path) -> list[dict]:
    specs = {}
    for suffix in ("a", "b", "c", "d"):
        account_id = f"account-{suffix}"
        config = {
            "account_id": account_id,
            "node_id": f"nautilus-node-{account_id}",
            "trader_id": f"trader-{account_id}",
            "instance_id": f"instance-{account_id}",
            "runtime_resources": {
                "schema_version": (
                    release_manifest.RUNTIME_RESOURCES_SCHEMA_VERSION
                ),
                "redis": {
                    "stream_max_entries": 1000,
                    "stream_max_bytes": 1_048_576,
                    "total_stream_max_bytes": 8_388_608,
                    "scan_count": 100,
                    "sample_interval_seconds": 1.0,
                    "critical_window_seconds": 30.0,
                    "thread_join_timeout_seconds": 5.0,
                    "memory_warning_ratio": 0.6,
                    "memory_degraded_ratio": 0.75,
                    "memory_critical_ratio": 0.85,
                },
                "command_journal": {
                    "max_bytes": 1_048_576,
                },
                "control_plane_session": dict(
                    release_manifest.CONTROL_PLANE_SESSION_RESOURCE_DEFAULTS
                ),
                "strategy_durable_io": dict(
                    release_manifest.STRATEGY_DURABLE_IO_RESOURCE_DEFAULTS
                ),
                "terminal_exchange": dict(
                    release_manifest.TERMINAL_EXCHANGE_RESOURCE_DEFAULTS
                ),
                "reporter_workers": dict(
                    release_manifest.REPORTER_WORKER_RESOURCE_DEFAULTS
                ),
            },
        }
        payload = (
            json.dumps(config, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        digest = sha256(payload).hexdigest()
        config_root = root / "config-artifacts" / account_id
        config_root.mkdir(parents=True)
        config_path = config_root / f"{digest}.json"
        config_path.write_bytes(payload)
        config_path.chmod(0o440)
        specs[account_id] = (
            f"trader-v3-node-{suffix}",
            config_path,
        )
    return release_manifest.build_node_config_artifacts(specs)


def _write_release_material(root: Path) -> tuple[Path, Path]:
    payload = root / "node.py"
    payload.write_bytes(b"reviewed immutable runtime")
    adapter_path = (
        root / reviewed_release_rollout.LIVE_ADAPTER_RELEASE_PATH
    )
    adapter_path.write_bytes(b"#!/usr/bin/env python3\n")
    adapter_path.chmod(0o755)
    adapter_sha256 = release_manifest.sha256_file(adapter_path)
    bundle = {
        "repo_commit": "5" * 40,
        "repo_dirty": False,
        "schema_epochs": dict(release_manifest.SCHEMA_EPOCHS),
        "files": [
            {
                "bundle_path": payload.name,
                "mount_target": "/app/app/node.py",
                "sha256": release_manifest.sha256_file(payload),
            }
        ],
    }
    bundle_path = root / "bundle-manifest.json"
    bundle_path.write_text(
        json.dumps(bundle, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    source_manifest = {
        "schema_version": "trader-v3-release-source/v1",
        "files": [
            {
                "source_path": (
                    reviewed_release_rollout.LIVE_ADAPTER_SOURCE_PATH
                ),
                "release_path": (
                    reviewed_release_rollout.LIVE_ADAPTER_RELEASE_PATH
                ),
                "source_git_blob": "4" * 40,
                "source_git_mode": "100755",
                "sha256": adapter_sha256,
                "size": adapter_path.stat().st_size,
            }
        ],
    }
    source_manifest_path = (
        root / release_manifest.RELEASE_SOURCE_MANIFEST_NAME
    )
    source_manifest_path.write_text(
        json.dumps(source_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    source_manifest_sha256 = release_manifest.sha256_file(
        source_manifest_path
    )
    node_configs = _write_node_configs(root)
    strict_fields = {
        "dependency_inventory_sha256": "6" * 64,
        "migration_manifest_sha256": "7" * 64,
        "release_payload": [
            {
                "path": "bundle-manifest.json",
                "sha256": release_manifest.sha256_file(bundle_path),
            },
            {
                "path": (
                    release_manifest.RELEASE_SOURCE_MANIFEST_NAME
                ),
                "sha256": source_manifest_sha256,
            },
            {
                "path": (
                    reviewed_release_rollout.LIVE_ADAPTER_RELEASE_PATH
                ),
                "sha256": adapter_sha256,
            },
        ],
        "sha256sums_sha256": "9" * 64,
        "release_source_manifest_sha256": source_manifest_sha256,
        "systemd_resource_contract_sha256": "b" * 64,
        "build_attestation_sha256": "c" * 64,
    }
    provisional = release_manifest.build_release_manifest(
        bundle,
        image_digest="sha256:" + ("1" * 64),
        config_sha256=None,
        dependency_lock_sha256="3" * 64,
        patch_root=root,
        delivery_mode=release_manifest.DELIVERY_IMMUTABLE,
        node_configs=node_configs,
        _allow_unreviewed_subject=True,
        **strict_fields,
    )
    reviewer_trust_proof = {
        "path": release_manifest.REVIEWER_TRUST_PROOF_NAME,
        "sha256": "d" * 64,
        "reviewer": "release-reviewer",
        "decision": "approved",
        "review_subject_sha256": provisional["review_subject_sha256"],
    }
    manifest = release_manifest.build_release_manifest(
        bundle,
        image_digest="sha256:" + ("1" * 64),
        config_sha256=None,
        dependency_lock_sha256="3" * 64,
        patch_root=root,
        delivery_mode=release_manifest.DELIVERY_IMMUTABLE,
        node_configs=node_configs,
        reviewer_trust_proof=reviewer_trust_proof,
        **strict_fields,
    )
    manifest_path = root / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path, bundle_path


def _release_envelope(root: Path) -> dict:
    source_manifest_path = (
        root / release_manifest.RELEASE_SOURCE_MANIFEST_NAME
    )
    source_manifest = json.loads(
        source_manifest_path.read_text(encoding="utf-8")
    )
    adapter_path = (
        root / reviewed_release_rollout.LIVE_ADAPTER_RELEASE_PATH
    )
    return {
        "release_source_manifest_sha256": (
            release_manifest.sha256_file(source_manifest_path)
        ),
        "release_payload": [
            {
                "path": (
                    reviewed_release_rollout.LIVE_ADAPTER_RELEASE_PATH
                ),
                "sha256": release_manifest.sha256_file(adapter_path),
            }
        ],
        "source_manifest": source_manifest,
    }


def _write_signing_key(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    private_key = root / "release-gate-private.pem"
    public_key = root / "release-gate-public.pem"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        capture_output=True,
    )
    private_key.chmod(0o600)
    subprocess.run(
        [
            "/usr/bin/openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "PINNED_REVIEWER_PUBLIC_KEY_SHA256",
        sha256(public_key.read_bytes()).hexdigest(),
    )
    return private_key, public_key


def _write_capacity_evidence(root: Path) -> Path:
    epoch_sha256 = sha256(REDIS_FENCING_EPOCH.encode("ascii")).hexdigest()
    evidence = {
        "schema_version": "trader-v3-redis-capacity-evidence/v3",
        "passed": True,
        "nodes_stopped": True,
        "dataset_mode": "empty-volume-exchange-first-rebaseline",
        "active_container": "trader-v3-redis",
        "active_redis_run_id": "a" * 40,
        "initial_redis_run_id": "a" * 40,
        "active_volume": "trader-v3-redis-hardening-test",
        "active_volume_source": "/var/lib/docker/volumes/redis-test/_data",
        "active_key_count": 1,
        "redis_fencing_epoch": REDIS_FENCING_EPOCH,
        "redis_fencing_epoch_key": REDIS_FENCING_EPOCH_KEY,
        "redis_fencing_epoch_sha256": epoch_sha256,
        "control_keys_reinitialized": True,
    }
    path = root / "capacity-evidence.json"
    path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _configure_operation_lock_test(
    monkeypatch: pytest.MonkeyPatch,
    lock_path: Path,
) -> None:
    monkeypatch.setenv(reviewed_release_rollout.TEST_MODE_ENV, "1")
    monkeypatch.setenv(
        reviewed_release_rollout.TEST_OPERATION_LOCK_PATH_ENV,
        str(lock_path),
    )
    monkeypatch.delenv("ACCOUNT_STALL_OPERATION_LOCK", raising=False)


def _closure_report_payload(
    *,
    account_id: str = "account-a",
) -> dict:
    phase_by_account = {
        "account-a": reviewed_release_rollout.PHASE_ACCOUNT_A_CANARY,
        "account-b": reviewed_release_rollout.PHASE_ACCOUNT_B_ROLLOUT,
        "account-c": reviewed_release_rollout.PHASE_ACCOUNT_C_ROLLOUT,
        "account-d": reviewed_release_rollout.PHASE_ACCOUNT_D_ROLLOUT,
    }
    return {
        "schema_version": (
            reviewed_release_rollout.LIVE_CANARY_CLOSURE_REPORT_SCHEMA
        ),
        "mode": "live",
        "passed": True,
        "failure_reason": "",
        "account_id": account_id,
        "node_id": reviewed_release_rollout.EXPECTED_NODE_IDS[
            account_id
        ],
        "rollout_phase": phase_by_account[account_id],
        "release_id": "release-fixture",
        "image_digest": "sha256:" + ("1" * 64),
        "config_sha256": "2" * 64,
        "dependency_lock_sha256": "3" * 64,
        "authorization_signatures_verified": True,
        "round_trip_count": 1,
        "finished_halted": True,
        "target_symbol_flat": True,
        "target_symbol_regular_orders_zero": True,
        "target_symbol_algo_orders_zero": True,
        "non_target_portfolio_before_sha256": "4" * 64,
        "non_target_portfolio_after_sha256": "4" * 64,
        "mainnet_round_trip": {
            "open_order_type": "LIMIT",
            "open_time_in_force": "IOC",
            "open_filled_quantity": "0.1",
            "actual_open_notional_usdt": "10",
            "close_order_type": "MARKET",
            "close_reduce_only": True,
            "close_quantity": "0.1",
            "close_filled_quantity": "0.1",
            "target_symbol_flat": True,
            "target_symbol_regular_orders_zero": True,
            "target_symbol_algo_orders_zero": True,
            "max_cumulative_loss_usdt": "1.49",
            "cumulative_net_loss_usdt": "0.12",
        },
    }


def _write_signed_closure_report(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutate=None,
) -> tuple[Path, Path, Path]:
    private_key = root / "reviewer-private.pem"
    public_key = root / "reviewer-public.pem"
    report_path = root / "closure-report.json"
    signature_path = root / "closure-report.sig"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "/usr/bin/openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        capture_output=True,
    )
    report = _closure_report_payload()
    if mutate is not None:
        mutate(report)
    report_path.write_text(
        json.dumps(report, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "/usr/bin/openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(private_key),
            "-out",
            str(signature_path),
            str(report_path),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "PINNED_REVIEWER_PUBLIC_KEY_SHA256",
        sha256(public_key.read_bytes()).hexdigest(),
    )
    return report_path, signature_path, public_key


def test_load_live_canary_closure_report_verifies_signed_exact_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path, signature_path, public_key = (
        _write_signed_closure_report(tmp_path, monkeypatch)
    )

    report = reviewed_release_rollout.load_live_canary_closure_report(
        report_path,
        signature_path,
        public_key,
    )

    assert report.account_id == "account-a"
    assert report.actual_open_notional_usdt == "10"
    assert report.open_filled_quantity == "0.1"
    assert report.close_quantity == "0.1"
    assert report.close_filled_quantity == "0.1"
    assert report.public_key_sha256 == sha256(
        public_key.read_bytes()
    ).hexdigest()


@pytest.mark.parametrize(
    ("mutate", "expected"),
    (
        (
            lambda report: report["mainnet_round_trip"].update(
                {"actual_open_notional_usdt": "12.01"}
            ),
            "exceeds 12 USDT",
        ),
        (
            lambda report: report["mainnet_round_trip"].update(
                {"close_filled_quantity": "0.09"}
            ),
            "close quantity differs",
        ),
        (
            lambda report: report.update({"finished_halted": False}),
            "finished_halted",
        ),
        (
            lambda report: report.update(
                {"non_target_portfolio_after_sha256": "5" * 64}
            ),
            "changed non-target portfolio",
        ),
    ),
)
def test_load_live_canary_closure_report_rejects_unsafe_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    expected: str,
) -> None:
    report_path, signature_path, public_key = (
        _write_signed_closure_report(
            tmp_path,
            monkeypatch,
            mutate=mutate,
        )
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match=expected,
    ):
        reviewed_release_rollout.load_live_canary_closure_report(
            report_path,
            signature_path,
            public_key,
        )


def test_load_live_canary_closure_report_rejects_tampered_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path, signature_path, public_key = (
        _write_signed_closure_report(tmp_path, monkeypatch)
    )
    report_path.write_text(
        json.dumps(
            {
                **_closure_report_payload(),
                "failure_reason": "tampered",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="signature verification failed",
    ):
        reviewed_release_rollout.load_live_canary_closure_report(
            report_path,
            signature_path,
            public_key,
        )


def test_rollout_phase_requires_prior_account_signed_closure_report() -> None:
    rollout = {
        "release_id": "release-fixture",
        "image_digest": "sha256:" + ("1" * 64),
        "config_sha256": "2" * 64,
        "dependency_lock_sha256": "3" * 64,
    }

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="account-a signed closure report is required",
    ):
        reviewed_release_rollout._closure_report_for_transition(
            rollout,
            target_phase=(
                reviewed_release_rollout.PHASE_ACCOUNT_B_ROLLOUT
            ),
            report_path=None,
            signature_path=None,
            public_key_path=None,
        )


def test_rollout_phase_binds_closure_report_to_prior_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = reviewed_release_rollout.LiveCanaryClosureReport(
        account_id="account-b",
        node_id="nautilus-node-account-b",
        rollout_phase=(
            reviewed_release_rollout.PHASE_ACCOUNT_B_ROLLOUT
        ),
        release_id="release-fixture",
        image_digest="sha256:" + ("1" * 64),
        config_sha256="2" * 64,
        dependency_lock_sha256="3" * 64,
        report_sha256="4" * 64,
        signature_sha256="5" * 64,
        public_key_sha256=(
            reviewed_release_rollout.PINNED_REVIEWER_PUBLIC_KEY_SHA256
        ),
        open_filled_quantity="0.1",
        actual_open_notional_usdt="10",
        close_quantity="0.1",
        close_filled_quantity="0.1",
        non_target_portfolio_sha256="6" * 64,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "load_live_canary_closure_report",
        lambda *_args: report,
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="requires account-a closure report",
    ):
        reviewed_release_rollout._closure_report_for_transition(
            {
                "release_id": "release-fixture",
                "image_digest": "sha256:" + ("1" * 64),
                "config_sha256": "2" * 64,
                "dependency_lock_sha256": "3" * 64,
            },
            target_phase=(
                reviewed_release_rollout.PHASE_ACCOUNT_B_ROLLOUT
            ),
            report_path=Path("report.json"),
            signature_path=Path("report.sig"),
            public_key_path=Path("reviewer.pem"),
        )


def test_load_release_document_verifies_manifest_bundle_and_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, bundle_path = _write_release_material(tmp_path)
    monkeypatch.setattr(
        release_manifest,
        "validate_strict_release_envelope",
        lambda *_args, **_kwargs: _release_envelope(tmp_path),
    )

    document = reviewed_release_rollout.load_release_document(
        manifest_path,
        bundle_path,
    )

    assert document.release_id
    assert document.image_digest == "sha256:" + ("1" * 64)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert document.config_sha256 == manifest["config_sha256"]
    assert document.dependency_lock_sha256 == "3" * 64
    assert document.schema_epoch == "0015_refresh_evidence_command"
    assert document.delivery_mode == release_manifest.DELIVERY_IMMUTABLE
    assert document.manifest_sha256 == release_manifest.sha256_file(
        manifest_path
    )
    assert document.bundle_manifest_sha256 == release_manifest.sha256_file(
        bundle_path
    )
    assert document.release_root_path == str(tmp_path.resolve())
    assert document.release_source_manifest_sha256 == (
        release_manifest.sha256_file(
            tmp_path / release_manifest.RELEASE_SOURCE_MANIFEST_NAME
        )
    )
    assert document.live_adapter_sha256 == release_manifest.sha256_file(
        tmp_path / reviewed_release_rollout.LIVE_ADAPTER_RELEASE_PATH
    )


def test_load_release_document_rejects_tampered_bundle_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, bundle_path = _write_release_material(tmp_path)
    (tmp_path / "node.py").write_bytes(b"tampered runtime")
    monkeypatch.setattr(
        release_manifest,
        "validate_strict_release_envelope",
        lambda *_args, **_kwargs: _release_envelope(tmp_path),
    )

    with pytest.raises(
        release_manifest.ReleaseManifestError,
        match="bundle payload hash mismatch",
    ):
        reviewed_release_rollout.load_release_document(
            manifest_path,
            bundle_path,
        )


def test_generate_signed_release_gate_derives_immutable_release_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "release"
    release_root.mkdir()
    manifest_path, bundle_path = _write_release_material(release_root)
    envelope = _release_envelope(release_root)
    monkeypatch.setattr(
        release_manifest,
        "validate_strict_release_envelope",
        lambda *_args, **_kwargs: envelope,
    )
    private_key, public_key = _write_signing_key(
        tmp_path,
        monkeypatch,
    )
    output = tmp_path / "signed-account-b-release-gate"

    result = reviewed_release_rollout.generate_signed_release_gate(
        manifest_path,
        bundle_path,
        account_id="account-b",
        signing_private_key_path=private_key,
        output_directory=output,
        now=datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc),
    )

    gate_path = Path(result["release_gate"])
    signature_path = Path(result["release_gate_signature"])
    generated_public_key = Path(result["reviewer_public_key"])
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    assert gate["account_id"] == "account-b"
    assert gate["node_id"] == "nautilus-node-account-b"
    assert gate["rollout_phase"] == "account_b_rollout"
    assert gate["release_root_path"] == str(release_root.resolve())
    assert gate["release_source_manifest_sha256"] == (
        release_manifest.sha256_file(
            release_root
            / release_manifest.RELEASE_SOURCE_MANIFEST_NAME
        )
    )
    assert gate["live_adapter_sha256"] == release_manifest.sha256_file(
        release_root
        / reviewed_release_rollout.LIVE_ADAPTER_RELEASE_PATH
    )
    assert gate["issued_at"] == "2026-08-12T08:00:00+00:00"
    assert gate["expires_at"] == "2026-08-12T08:15:00+00:00"
    assert generated_public_key.read_bytes() == public_key.read_bytes()
    verification = subprocess.run(
        [
            "/usr/bin/openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(generated_public_key),
            "-signature",
            str(signature_path),
            str(gate_path),
        ],
        check=False,
        capture_output=True,
    )
    assert verification.returncode == 0
    assert gate_path.stat().st_mode & 0o777 == 0o400
    assert signature_path.stat().st_mode & 0o777 == 0o400
    assert generated_public_key.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize(
    ("account_id", "rollout_phase"),
    (
        ("account-a", "account_a_canary"),
        ("account-b", "account_b_rollout"),
        ("account-c", "account_c_rollout"),
        ("account-d", "account_d_rollout"),
    ),
)
def test_release_gate_target_is_fixed_by_account(
    account_id: str,
    rollout_phase: str,
) -> None:
    target = reviewed_release_rollout._release_gate_target(account_id)

    assert target.node_id == f"nautilus-node-{account_id}"
    assert target.rollout_phase == rollout_phase
    assert target.schema_version == (
        f"trader-v3-{account_id}-live-release-gate/v1"
    )
    assert target.permit_store_id == (
        f"trader-v3-{account_id}-live-permit-store/v1"
    )
    assert target.permit_store_path == (
        f"/var/lib/trader-v3/{account_id}-live-permit-ledger.json"
    )


def test_generate_signed_release_gate_rejects_source_manifest_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "release"
    release_root.mkdir()
    manifest_path, bundle_path = _write_release_material(release_root)
    envelope = _release_envelope(release_root)
    source_manifest_path = (
        release_root / release_manifest.RELEASE_SOURCE_MANIFEST_NAME
    )
    source_manifest_path.write_text(
        source_manifest_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        release_manifest,
        "validate_strict_release_envelope",
        lambda *_args, **_kwargs: envelope,
    )
    private_key, _public_key = _write_signing_key(
        tmp_path,
        monkeypatch,
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="source manifest hash changed",
    ):
        reviewed_release_rollout.generate_signed_release_gate(
            manifest_path,
            bundle_path,
            account_id="account-a",
            signing_private_key_path=private_key,
            output_directory=tmp_path / "signed-gate",
        )


def test_generate_signed_release_gate_rejects_live_adapter_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "release"
    release_root.mkdir()
    manifest_path, bundle_path = _write_release_material(release_root)
    envelope = _release_envelope(release_root)
    adapter_path = (
        release_root
        / reviewed_release_rollout.LIVE_ADAPTER_RELEASE_PATH
    )
    adapter_path.write_bytes(b"#!/usr/bin/env python3\n# drift\n")
    adapter_path.chmod(0o755)
    monkeypatch.setattr(
        release_manifest,
        "validate_strict_release_envelope",
        lambda *_args, **_kwargs: envelope,
    )
    private_key, _public_key = _write_signing_key(
        tmp_path,
        monkeypatch,
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="live adapter hash differs",
    ):
        reviewed_release_rollout.generate_signed_release_gate(
            manifest_path,
            bundle_path,
            account_id="account-a",
            signing_private_key_path=private_key,
            output_directory=tmp_path / "signed-gate",
        )


def test_generate_signed_release_gate_rejects_unpinned_signing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "release"
    release_root.mkdir()
    manifest_path, bundle_path = _write_release_material(release_root)
    monkeypatch.setattr(
        release_manifest,
        "validate_strict_release_envelope",
        lambda *_args, **_kwargs: _release_envelope(release_root),
    )
    private_key, _public_key = _write_signing_key(
        tmp_path,
        monkeypatch,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "PINNED_REVIEWER_PUBLIC_KEY_SHA256",
        "0" * 64,
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="differs from pinned reviewer",
    ):
        reviewed_release_rollout.generate_signed_release_gate(
            manifest_path,
            bundle_path,
            account_id="account-a",
            signing_private_key_path=private_key,
            output_directory=tmp_path / "signed-gate",
        )


def test_load_release_document_rejects_symlink_release_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "release"
    release_root.mkdir()
    manifest_path, bundle_path = _write_release_material(release_root)
    release_link = tmp_path / "release-link"
    release_link.symlink_to(release_root, target_is_directory=True)
    monkeypatch.setattr(
        release_manifest,
        "validate_strict_release_envelope",
        lambda *_args, **_kwargs: _release_envelope(release_root),
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="release root must be canonical",
    ):
        reviewed_release_rollout.load_release_document(
            release_link / manifest_path.name,
            release_link / bundle_path.name,
        )


def test_generate_signed_release_gate_rejects_output_in_release_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "release"
    release_root.mkdir()
    manifest_path, bundle_path = _write_release_material(release_root)
    monkeypatch.setattr(
        release_manifest,
        "validate_strict_release_envelope",
        lambda *_args, **_kwargs: _release_envelope(release_root),
    )
    private_key, _public_key = _write_signing_key(
        tmp_path,
        monkeypatch,
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="outside immutable release root",
    ):
        reviewed_release_rollout.generate_signed_release_gate(
            manifest_path,
            bundle_path,
            account_id="account-c",
            signing_private_key_path=private_key,
            output_directory=release_root / "signed-gate",
        )


def test_load_capacity_evidence_verifies_epoch_identity(
    tmp_path: Path,
) -> None:
    evidence_path = _write_capacity_evidence(tmp_path)

    evidence = reviewed_release_rollout.load_capacity_evidence(evidence_path)

    assert evidence.redis_fencing_epoch == REDIS_FENCING_EPOCH
    assert evidence.marker_sha256 == sha256(
        REDIS_FENCING_EPOCH.encode("ascii")
    ).hexdigest()
    assert evidence.capacity_evidence_sha256 == sha256(
        evidence_path.read_bytes()
    ).hexdigest()
    assert evidence.initial_redis_run_id == "a" * 40
    assert evidence.active_volume == "trader-v3-redis-hardening-test"


def test_load_capacity_evidence_rejects_marker_hash_drift(
    tmp_path: Path,
) -> None:
    evidence_path = _write_capacity_evidence(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["redis_fencing_epoch_sha256"] = "f" * 64
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="marker hash mismatch",
    ):
        reviewed_release_rollout.load_capacity_evidence(evidence_path)


def test_register_cli_requires_capacity_evidence() -> None:
    parser = reviewed_release_rollout._parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "register",
                "--manifest",
                "release-manifest.json",
                "--bundle-manifest",
                "bundle-manifest.json",
                "--reviewed-by",
                "reviewer",
                "--idempotency-key",
                "register-release",
            ]
        )


def test_bootstrap_register_cli_requires_capacity_evidence() -> None:
    parser = reviewed_release_rollout._parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "bootstrap-register",
                "--manifest",
                "release-manifest.json",
                "--bundle-manifest",
                "bundle-manifest.json",
                "--reviewed-by",
                "reviewer",
                "--idempotency-key",
                "register-release",
            ]
        )


def test_bootstrap_register_cli_uses_operation_locked_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path, bundle_path = _write_release_material(tmp_path)
    monkeypatch.setattr(
        release_manifest,
        "validate_strict_release_envelope",
        lambda *_args, **_kwargs: _release_envelope(tmp_path),
    )
    document = reviewed_release_rollout.load_release_document(
        manifest_path,
        bundle_path,
    )
    capacity = reviewed_release_rollout.load_capacity_evidence(
        _write_capacity_evidence(tmp_path)
    )
    calls = []

    class OperationLock:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return

        def require_held(self) -> None:
            calls.append("lock")

    class Connection:
        def close(self) -> None:
            calls.append("close")

    operation_lock = OperationLock()

    def operation_lock_factory(**kwargs):
        calls.append(("lock-factory", kwargs))
        return operation_lock

    monkeypatch.setattr(
        reviewed_release_rollout,
        "AccountStallOperationLock",
        operation_lock_factory,
    )
    monkeypatch.setattr(
        reviewed_release_rollout.psycopg2,
        "connect",
        lambda _database_url: Connection(),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "bootstrap_register_reviewed_release",
        lambda _conn, actual_document, actual_capacity, **kwargs: (
            kwargs["operation_lock"].require_held()
            or {
                "release_id": actual_document.release_id,
                "idempotency_key": kwargs["idempotency_key"],
                "capacity_epoch": actual_capacity.redis_fencing_epoch,
                "idempotent": False,
            }
        ),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "register_reviewed_release",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bootstrap command reached normal register")
        ),
    )

    result = reviewed_release_rollout.main(
        [
            "--database-url",
            "postgresql://fixture.invalid/trader",
            "bootstrap-register",
            "--manifest",
            str(manifest_path),
            "--bundle-manifest",
            str(bundle_path),
            "--capacity-evidence",
            str(tmp_path / "capacity-evidence.json"),
            "--reviewed-by",
            "release-reviewer",
            "--idempotency-key",
            "register:bootstrap-fixture",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["idempotency_key"] == "register:bootstrap-fixture"
    assert payload["capacity_epoch"] == REDIS_FENCING_EPOCH
    assert calls == [
        ("lock-factory", {"enabled": True}),
        "lock",
        "close",
    ]


def test_bootstrap_registration_history_requires_empty_or_exact_replay() -> None:
    empty = {
        "redis_fencing_epoch_count": 0,
        "reviewed_release_rollout_count": 0,
    }
    replay = {
        "redis_fencing_epoch_count": 1,
        "reviewed_release_rollout_count": 1,
    }

    reviewed_release_rollout._require_empty_bootstrap_history(empty)
    reviewed_release_rollout._require_bootstrap_replay_history(replay)

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="no Redis fencing epoch history",
    ):
        reviewed_release_rollout._require_empty_bootstrap_history(replay)
    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="exactly one reviewed release rollout",
    ):
        reviewed_release_rollout._require_bootstrap_replay_history(
            {
                "redis_fencing_epoch_count": 1,
                "reviewed_release_rollout_count": 2,
            }
        )


def test_bootstrap_replay_requires_halted_event_and_audit_marks() -> None:
    class EventCursor:
        def execute(self, _statement, _params=None) -> None:
            return

        def fetchone(self):
            return {
                "release_id": "bootstrap-release",
                "event_type": "registered",
                "from_phase": None,
                "to_phase": (
                    reviewed_release_rollout.PHASE_ACCOUNT_A_CANARY
                ),
                "phase_version": 1,
                "actor": "release-reviewer",
                "evidence": {
                    "registration_mode": "bootstrap",
                    "bootstrap_all_halted": True,
                },
            }

        def fetchall(self):
            return [
                {
                    "actor": "release-reviewer",
                    "payload": {
                        "idempotency_key": "register:bootstrap-release",
                        "registration_mode": "bootstrap",
                        "bootstrap_all_halted": True,
                    },
                }
            ]

    cursor = EventCursor()
    reviewed_release_rollout._require_bootstrap_registration_event(
        cursor,
        release_id="bootstrap-release",
        idempotency_key="register:bootstrap-release",
        reviewed_by="release-reviewer",
    )
    reviewed_release_rollout._require_bootstrap_registration_audit(
        cursor,
        release_id="bootstrap-release",
        idempotency_key="register:bootstrap-release",
        reviewed_by="release-reviewer",
    )

    class UnsafeEventCursor(EventCursor):
        def fetchone(self):
            event = super().fetchone()
            event["evidence"]["bootstrap_all_halted"] = False
            return event

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="halted state conflicts",
    ):
        reviewed_release_rollout._require_bootstrap_registration_event(
            UnsafeEventCursor(),
            release_id="bootstrap-release",
            idempotency_key="register:bootstrap-release",
            reviewed_by="release-reviewer",
        )


def test_bootstrap_registration_audit_marks_all_accounts_halted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = reviewed_release_rollout.ReleaseDocument(
        release_id="bootstrap-release",
        image_digest="sha256:" + ("1" * 64),
        config_sha256="2" * 64,
        dependency_lock_sha256="3" * 64,
        schema_epoch="0015_refresh_evidence_command",
        manifest_sha256="4" * 64,
        bundle_manifest_sha256="5" * 64,
        delivery_mode=release_manifest.DELIVERY_IMMUTABLE,
        release_root_path="/srv/trader-v3/releases/bootstrap",
        release_source_manifest_sha256="6" * 64,
        live_adapter_sha256="7" * 64,
        node_ids=dict(reviewed_release_rollout.EXPECTED_NODE_IDS),
    )
    capacity = reviewed_release_rollout.RedisFencingEpochEvidence(
        redis_fencing_epoch=REDIS_FENCING_EPOCH,
        marker_sha256=sha256(
            REDIS_FENCING_EPOCH.encode("ascii")
        ).hexdigest(),
        capacity_evidence_sha256="8" * 64,
        initial_redis_run_id="a" * 40,
        active_volume="trader-v3-redis-bootstrap",
    )
    events = []
    audits = []

    class OperationLock:
        def require_held(self) -> None:
            return

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return

        def execute(self, statement, _params=None) -> None:
            if "INSERT INTO reviewed_release_rollouts" in statement:
                self.inserted = True

        def fetchone(self):
            return {
                "release_id": document.release_id,
                "redis_fencing_epoch": (
                    capacity.redis_fencing_epoch
                ),
                "image_digest": document.image_digest,
                "config_sha256": document.config_sha256,
                "dependency_lock_sha256": (
                    document.dependency_lock_sha256
                ),
                "schema_epoch": document.schema_epoch,
                "manifest_sha256": document.manifest_sha256,
                "bundle_manifest_sha256": (
                    document.bundle_manifest_sha256
                ),
                "registration_idempotency_key": (
                    "register:bootstrap-release"
                ),
                "phase": (
                    reviewed_release_rollout.PHASE_ACCOUNT_A_CANARY
                ),
                "phase_version": 1,
                "reviewed_by": "release-reviewer",
                "reviewed_at": None,
                "created_at": None,
                "updated_at": None,
            }

    class Connection:
        def __init__(self) -> None:
            self.cursor_instance = Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return

        def cursor(self, **_kwargs):
            return self.cursor_instance

    monkeypatch.setattr(
        reviewed_release_rollout,
        "_require_maintenance_fence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bootstrap registration reached maintenance fence")
        ),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_lock_registration_heartbeats",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError(
                "bootstrap registration reached heartbeat gate"
            )
        ),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_rollout_by_registration_key",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_bootstrap_registration_history",
        lambda *_args, **_kwargs: {
            "redis_fencing_epoch_count": 0,
            "reviewed_release_rollout_count": 0,
        },
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_activate_redis_fencing_epoch",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_register_account_manifests",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_require_registered_manifests",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_record_rollout_event",
        lambda *_args, **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_record_global_audit",
        lambda *_args, **kwargs: audits.append(kwargs),
    )

    result = reviewed_release_rollout.bootstrap_register_reviewed_release(
        Connection(),
        document,
        capacity,
        reviewed_by="release-reviewer",
        idempotency_key="register:bootstrap-release",
        operation_lock=OperationLock(),
    )

    assert result["idempotent"] is False
    assert events[0]["evidence"]["registration_mode"] == "bootstrap"
    assert events[0]["evidence"]["bootstrap_all_halted"] is True
    assert audits[0]["payload"]["registration_mode"] == "bootstrap"
    assert audits[0]["payload"]["bootstrap_all_halted"] is True


def _migration_rebaseline_document() -> reviewed_release_rollout.ReleaseDocument:
    return reviewed_release_rollout.ReleaseDocument(
        release_id="migration-release",
        image_digest="sha256:" + ("1" * 64),
        config_sha256="2" * 64,
        dependency_lock_sha256="3" * 64,
        schema_epoch="0015_refresh_evidence_command",
        manifest_sha256="4" * 64,
        bundle_manifest_sha256="5" * 64,
        delivery_mode=release_manifest.DELIVERY_IMMUTABLE,
        release_root_path="/srv/trader-v3/releases/migration",
        release_source_manifest_sha256="6" * 64,
        live_adapter_sha256="7" * 64,
        node_ids=dict(reviewed_release_rollout.EXPECTED_NODE_IDS),
    )


def _migration_rebaseline_capacity(
) -> reviewed_release_rollout.RedisFencingEpochEvidence:
    return reviewed_release_rollout.RedisFencingEpochEvidence(
        redis_fencing_epoch=REDIS_FENCING_EPOCH,
        marker_sha256=sha256(
            REDIS_FENCING_EPOCH.encode("ascii")
        ).hexdigest(),
        capacity_evidence_sha256="8" * 64,
        initial_redis_run_id="a" * 40,
        active_volume="trader-v3-redis-migration",
    )


def _migration_rebaseline_rollout(
    document: reviewed_release_rollout.ReleaseDocument,
    capacity: reviewed_release_rollout.RedisFencingEpochEvidence,
) -> dict:
    return {
        "release_id": document.release_id,
        "redis_fencing_epoch": capacity.redis_fencing_epoch,
        "image_digest": document.image_digest,
        "config_sha256": document.config_sha256,
        "dependency_lock_sha256": document.dependency_lock_sha256,
        "schema_epoch": document.schema_epoch,
        "manifest_sha256": document.manifest_sha256,
        "bundle_manifest_sha256": document.bundle_manifest_sha256,
        "registration_idempotency_key": (
            "migration-rebaseline-register:migration-release"
        ),
        "phase": reviewed_release_rollout.PHASE_ACCOUNT_A_CANARY,
        "phase_version": 1,
        "reviewed_by": "release-reviewer",
        "reviewed_at": None,
        "created_at": None,
        "updated_at": None,
    }


def test_migration_rebaseline_registration_is_atomic_and_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _migration_rebaseline_document()
    capacity = _migration_rebaseline_capacity()
    rollout = _migration_rebaseline_rollout(document, capacity)
    predecessor = {
        "release_id": "migrated-hk-release",
        "redis_fencing_epoch": "123e4567-e89b-42d3-a456-426614174001",
        "phase": reviewed_release_rollout.PHASE_ACCOUNT_A_CANARY,
        "phase_version": 1,
    }
    calls = []
    events = []
    audits = []

    class OperationLock:
        def require_held(self) -> None:
            calls.append("lock")

    class Cursor:
        def __enter__(self):
            calls.append("cursor-enter")
            return self

        def __exit__(self, *_args) -> None:
            calls.append("cursor-exit")

        def execute(self, statement, _params=None) -> None:
            if "INSERT INTO reviewed_release_rollouts" in statement:
                calls.append("insert-successor")

        def fetchone(self):
            return rollout

    class Connection:
        def __enter__(self):
            calls.append("transaction-enter")
            return self

        def __exit__(self, *_args) -> None:
            calls.append("transaction-exit")

        def cursor(self, **_kwargs):
            return Cursor()

    monkeypatch.setattr(
        reviewed_release_rollout,
        "_rollout_by_registration_key",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_require_no_active_maintenance_fence_for_migration",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_bootstrap_registration_history",
        lambda *_args, **_kwargs: {
            "redis_fencing_epoch_count": 2,
            "reviewed_release_rollout_count": 1,
        },
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_lock_migration_rebaseline_predecessor",
        lambda *_args, **_kwargs: predecessor,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_abort_migration_rebaseline_predecessor",
        lambda *_args, **_kwargs: calls.append("abort-predecessor"),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_activate_redis_fencing_epoch",
        lambda *_args, **_kwargs: calls.append("activate-successor-epoch"),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_register_account_manifests",
        lambda *_args, **_kwargs: calls.append("register-manifests"),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_require_registered_manifests",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_record_rollout_event",
        lambda *_args, **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_record_global_audit",
        lambda *_args, **kwargs: audits.append(kwargs),
    )

    result = (
        reviewed_release_rollout.migration_rebaseline_register_reviewed_release(
            Connection(),
            document,
            capacity,
            reviewed_by="release-reviewer",
            idempotency_key=(
                "migration-rebaseline-register:migration-release"
            ),
            operation_lock=OperationLock(),
        )
    )

    assert result["idempotent"] is False
    assert calls.index("abort-predecessor") < calls.index(
        "activate-successor-epoch"
    )
    assert calls.index("activate-successor-epoch") < calls.index(
        "insert-successor"
    )
    registration = events[0]["evidence"]
    assert registration["registration_mode"] == (
        reviewed_release_rollout.MIGRATION_REBASELINE_REGISTRATION_MODE
    )
    assert registration["all_accounts_stopped"] is True
    assert registration["predecessor_release_id"] == "migrated-hk-release"
    assert audits[0]["payload"]["predecessor_release_id"] == (
        "migrated-hk-release"
    )


def test_migration_rebaseline_registration_replay_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _migration_rebaseline_document()
    capacity = _migration_rebaseline_capacity()
    rollout = _migration_rebaseline_rollout(document, capacity)
    replay_checks = []

    class OperationLock:
        def require_held(self) -> None:
            return

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return

        def execute(self, _statement, _params=None) -> None:
            return

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return

        def cursor(self, **_kwargs):
            return Cursor()

    monkeypatch.setattr(
        reviewed_release_rollout,
        "_rollout_by_registration_key",
        lambda *_args, **_kwargs: rollout,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_require_no_active_maintenance_fence_for_migration",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_bootstrap_registration_history",
        lambda *_args, **_kwargs: {
            "redis_fencing_epoch_count": 3,
            "reviewed_release_rollout_count": 2,
        },
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_require_rollout_matches_document",
        lambda *_args, **_kwargs: replay_checks.append("identity"),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_require_migration_rebaseline_replay",
        lambda *_args, **_kwargs: replay_checks.append("history"),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_require_registered_manifests",
        lambda *_args, **_kwargs: replay_checks.append("manifests"),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_activate_redis_fencing_epoch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("replay attempted to rotate Redis epoch")
        ),
    )

    result = (
        reviewed_release_rollout.migration_rebaseline_register_reviewed_release(
            Connection(),
            document,
            capacity,
            reviewed_by="release-reviewer",
            idempotency_key=(
                "migration-rebaseline-register:migration-release"
            ),
            operation_lock=OperationLock(),
        )
    )

    assert result["idempotent"] is True
    assert replay_checks == ["identity", "history", "manifests"]


def test_register_same_epoch_hotfix_supersedes_active_rollout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = reviewed_release_rollout.ReleaseDocument(
        release_id="hotfix-release",
        image_digest="sha256:" + ("1" * 64),
        config_sha256="2" * 64,
        dependency_lock_sha256="3" * 64,
        schema_epoch="0015_refresh_evidence_command",
        manifest_sha256="4" * 64,
        bundle_manifest_sha256="5" * 64,
        delivery_mode=release_manifest.DELIVERY_IMMUTABLE,
        release_root_path="/srv/trader-v3/releases/hotfix",
        release_source_manifest_sha256="6" * 64,
        live_adapter_sha256="7" * 64,
        node_ids=dict(reviewed_release_rollout.EXPECTED_NODE_IDS),
    )
    capacity = reviewed_release_rollout.RedisFencingEpochEvidence(
        redis_fencing_epoch=REDIS_FENCING_EPOCH,
        marker_sha256=sha256(
            REDIS_FENCING_EPOCH.encode("ascii")
        ).hexdigest(),
        capacity_evidence_sha256="8" * 64,
        initial_redis_run_id="a" * 40,
        active_volume="trader-v3-redis-hotfix",
    )
    rollout = {
        "release_id": document.release_id,
        "redis_fencing_epoch": capacity.redis_fencing_epoch,
        "image_digest": document.image_digest,
        "config_sha256": document.config_sha256,
        "dependency_lock_sha256": document.dependency_lock_sha256,
        "schema_epoch": document.schema_epoch,
        "manifest_sha256": document.manifest_sha256,
        "bundle_manifest_sha256": document.bundle_manifest_sha256,
        "registration_idempotency_key": "register:hotfix-release",
        "phase": reviewed_release_rollout.PHASE_ACCOUNT_A_CANARY,
        "phase_version": 1,
        "reviewed_by": "release-reviewer",
        "reviewed_at": None,
        "created_at": None,
        "updated_at": None,
    }
    predecessor = {
        "release_id": "previous-release",
        "redis_fencing_epoch": capacity.redis_fencing_epoch,
        "phase": reviewed_release_rollout.PHASE_ACCOUNT_A_CANARY,
        "phase_version": 1,
    }
    calls = []
    events = []
    audits = []

    class OperationLock:
        def require_held(self) -> None:
            calls.append("lock")

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return

        def execute(self, statement, _params=None) -> None:
            if "INSERT INTO reviewed_release_rollouts" in statement:
                calls.append("insert-successor")

        def fetchone(self):
            return rollout

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return

        def cursor(self, **_kwargs):
            return Cursor()

    monkeypatch.setattr(
        reviewed_release_rollout,
        "_require_maintenance_fence",
        lambda *_args, **_kwargs: {"fence_id": "fence-hotfix"},
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_rollout_by_registration_key",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_active_redis_fencing_epoch",
        lambda *_args, **_kwargs: capacity.redis_fencing_epoch,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_lock_same_epoch_hotfix_predecessor",
        lambda *_args, **_kwargs: predecessor,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_abort_same_epoch_hotfix_predecessor",
        lambda *_args, **_kwargs: calls.append("abort-predecessor"),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_lock_registration_heartbeats",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("same-epoch hotfix attempted heartbeat gate")
        ),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_activate_redis_fencing_epoch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("same-epoch hotfix attempted Redis epoch activation")
        ),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_register_account_manifests",
        lambda *_args, **_kwargs: calls.append("register-manifests"),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_require_registered_manifests",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_record_rollout_event",
        lambda *_args, **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_record_global_audit",
        lambda *_args, **kwargs: audits.append(kwargs),
    )

    result = reviewed_release_rollout.register_reviewed_release(
        Connection(),
        document,
        capacity,
        reviewed_by="release-reviewer",
        idempotency_key="register:hotfix-release",
        operation_lock=OperationLock(),
    )

    assert result["idempotent"] is False
    assert calls.index("abort-predecessor") < calls.index("insert-successor")
    assert calls.index("insert-successor") < calls.index("register-manifests")
    registration = events[0]["evidence"]
    assert registration["registration_mode"] == (
        reviewed_release_rollout.SAME_EPOCH_HOTFIX_REGISTRATION_MODE
    )
    assert registration["redis_epoch_reused"] is True
    assert registration["predecessor_release_id"] == "previous-release"
    assert audits[0]["payload"]["redis_epoch_reused"] is True


def test_migration_rebaseline_abort_records_transition_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _migration_rebaseline_document()
    capacity = _migration_rebaseline_capacity()
    predecessor = {
        "release_id": "migrated-hk-release",
        "redis_fencing_epoch": "123e4567-e89b-42d3-a456-426614174001",
        "phase": reviewed_release_rollout.PHASE_ACCOUNT_A_CANARY,
        "phase_version": 1,
    }
    statements = []
    events = []
    audits = []

    class Cursor:
        def execute(self, statement, params=None) -> None:
            statements.append((statement, params))

        def fetchone(self):
            return {"release_id": predecessor["release_id"]}

    monkeypatch.setattr(
        reviewed_release_rollout,
        "_record_rollout_event",
        lambda *_args, **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_record_global_audit",
        lambda *_args, **kwargs: audits.append(kwargs),
    )

    reviewed_release_rollout._abort_migration_rebaseline_predecessor(
        Cursor(),
        predecessor=predecessor,
        successor_document=document,
        successor_capacity_evidence=capacity,
        actor="release-reviewer",
    )

    assert "UPDATE reviewed_release_rollouts" in statements[0][0]
    assert events[0]["from_phase"] == (
        reviewed_release_rollout.PHASE_ACCOUNT_A_CANARY
    )
    assert events[0]["to_phase"] == reviewed_release_rollout.PHASE_ABORTED
    assert events[0]["idempotency_key"] == (
        "migration-rebaseline-abort:migration-release"
    )
    assert events[0]["evidence"]["all_accounts_stopped"] is True
    assert audits[0]["payload"]["successor_release_id"] == (
        "migration-release"
    )


def test_migration_rebaseline_rejects_active_maintenance_fence() -> None:
    class Cursor:
        def execute(self, statement, params=None) -> None:
            self.statements.append((statement, params))

        def fetchall(self):
            return [{"fence_id": "active-fence"}]

    cursor = Cursor()
    cursor.statements = []
    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="requires no active maintenance fence",
    ):
        reviewed_release_rollout._require_no_active_maintenance_fence_for_migration(
            cursor
        )

    assert "pg_advisory_xact_lock" in cursor.statements[0][0]
    assert cursor.statements[0][1] == (
        "trader-v3-control-plane-maintenance-fence",
    )
    assert "FROM control_plane_maintenance_fences" in (
        cursor.statements[1][0]
    )
    assert "FOR UPDATE" in cursor.statements[1][0]
    assert cursor.statements[1][1] == (
        reviewed_release_rollout.REDIS_FENCING_DOMAIN,
    )


def _readiness_rollout() -> dict:
    return {
        "release_id": "bootstrap-release",
        "image_digest": "sha256:" + ("1" * 64),
        "config_sha256": "2" * 64,
        "dependency_lock_sha256": "3" * 64,
        "schema_epoch": "0015_refresh_evidence_command",
        "redis_fencing_epoch": REDIS_FENCING_EPOCH,
        "registration_idempotency_key": "register:bootstrap-release",
        "reviewed_by": "release-reviewer",
    }


def _readiness_heartbeat(
    account_id: str,
    rollout: dict,
    *,
    release_id: str | None = None,
    image_digest: str | None = None,
    lease_fencing_token: int = 1,
) -> dict:
    actual_release_id = release_id
    if actual_release_id is None:
        actual_release_id = rollout["release_id"]
    actual_image_digest = image_digest
    if actual_image_digest is None:
        actual_image_digest = rollout["image_digest"]
    return {
        "account_id": account_id,
        "node_id": reviewed_release_rollout.EXPECTED_NODE_IDS[
            account_id
        ],
        "status": "HALTED",
        "release_id": actual_release_id,
        "image_digest": actual_image_digest,
        "config_sha256": rollout["config_sha256"],
        "dependency_lock_sha256": rollout[
            "dependency_lock_sha256"
        ],
        "schema_epoch": rollout["schema_epoch"],
        "redis_fencing_epoch": rollout["redis_fencing_epoch"],
        "runtime_generation": f"generation-{account_id}",
        "lease_fencing_token": lease_fencing_token,
        "heartbeat_sequence": 1,
        "last_seen_at": datetime.now(timezone.utc),
    }


class _ReadinessCursor:
    def __init__(
        self,
        *,
        event_evidence: dict,
        audit_payload: dict | None = None,
        approved_old_release: dict | None = None,
    ) -> None:
        self.event_evidence = event_evidence
        self.audit_payload = audit_payload
        self.approved_old_release = approved_old_release
        self.statement = ""
        self.manifest_queries = 0

    def execute(self, statement: str, _params=None) -> None:
        self.statement = statement
        if "FROM reviewed_release_manifests" in statement:
            self.manifest_queries += 1

    def fetchone(self):
        if "SELECT evidence" in self.statement:
            return {"evidence": self.event_evidence}
        if "FROM reviewed_release_rollout_events" in self.statement:
            return {
                "release_id": "bootstrap-release",
                "event_type": "registered",
                "from_phase": None,
                "to_phase": (
                    reviewed_release_rollout.PHASE_ACCOUNT_A_CANARY
                ),
                "phase_version": 1,
                "actor": "release-reviewer",
                "evidence": self.event_evidence,
            }
        if "FROM reviewed_release_manifests" in self.statement:
            return self.approved_old_release
        raise AssertionError(f"unexpected fetchone query: {self.statement}")

    def fetchall(self):
        if "FROM audit_events" not in self.statement:
            raise AssertionError(
                f"unexpected fetchall query: {self.statement}"
            )
        if self.audit_payload is None:
            return []
        return [
            {
                "actor": "release-reviewer",
                "payload": self.audit_payload,
            }
        ]


def _bootstrap_registration_evidence() -> dict:
    return {
        "registration_mode": "bootstrap",
        "bootstrap_all_halted": True,
    }


def _bootstrap_registration_audit() -> dict:
    return {
        "idempotency_key": "register:bootstrap-release",
        "registration_mode": "bootstrap",
        "bootstrap_all_halted": True,
    }


def _stub_readiness_heartbeats(
    monkeypatch: pytest.MonkeyPatch,
    rows_by_account: dict[str, list[dict]],
) -> None:
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_fresh_heartbeat_rows",
        lambda *_args, **_kwargs: rows_by_account,
    )


def test_bootstrap_readiness_accepts_next_account_on_new_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollout = _readiness_rollout()
    rows_by_account = {
        "account-a": [_readiness_heartbeat("account-a", rollout)],
        "account-b": [_readiness_heartbeat("account-b", rollout)],
    }
    _stub_readiness_heartbeats(monkeypatch, rows_by_account)
    cursor = _ReadinessCursor(
        event_evidence=_bootstrap_registration_evidence(),
        audit_payload=_bootstrap_registration_audit(),
    )

    evidence = reviewed_release_rollout._require_account_rollout_readiness(
        cursor,
        rollout,
        upgraded_accounts=("account-a",),
        next_account="account-b",
        max_age_seconds=5.0,
    )

    assert [row["account_id"] for row in evidence] == [
        "account-a",
        "account-b",
    ]
    assert cursor.manifest_queries == 0


def test_bootstrap_readiness_rejects_next_account_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollout = _readiness_rollout()
    rows_by_account = {
        "account-a": [_readiness_heartbeat("account-a", rollout)],
        "account-b": [
            _readiness_heartbeat(
                "account-b",
                rollout,
                image_digest="sha256:" + ("9" * 64),
            )
        ],
    }
    _stub_readiness_heartbeats(monkeypatch, rows_by_account)
    cursor = _ReadinessCursor(
        event_evidence=_bootstrap_registration_evidence(),
        audit_payload=_bootstrap_registration_audit(),
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="must run the bootstrap reviewed release",
    ):
        reviewed_release_rollout._require_account_rollout_readiness(
            cursor,
            rollout,
            upgraded_accounts=("account-a",),
            next_account="account-b",
            max_age_seconds=5.0,
        )


@pytest.mark.parametrize(
    "event_evidence",
    (
        {"registration_mode": "bootstrap"},
        {
            "registration_mode": "bootstrap",
            "bootstrap_all_halted": False,
        },
    ),
)
def test_bootstrap_readiness_rejects_incomplete_event_evidence(
    monkeypatch: pytest.MonkeyPatch,
    event_evidence: dict,
) -> None:
    rollout = _readiness_rollout()
    rows_by_account = {
        "account-a": [_readiness_heartbeat("account-a", rollout)],
        "account-b": [_readiness_heartbeat("account-b", rollout)],
    }
    _stub_readiness_heartbeats(monkeypatch, rows_by_account)
    cursor = _ReadinessCursor(
        event_evidence=event_evidence,
        audit_payload=_bootstrap_registration_audit(),
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="bootstrap registration evidence is incomplete",
    ):
        reviewed_release_rollout._require_account_rollout_readiness(
            cursor,
            rollout,
            upgraded_accounts=("account-a",),
            next_account="account-b",
            max_age_seconds=5.0,
        )


@pytest.mark.parametrize(
    "audit_payload",
    (
        {
            "idempotency_key": "register:bootstrap-release",
            "registration_mode": "bootstrap",
        },
        {
            "idempotency_key": "register:bootstrap-release",
            "registration_mode": "bootstrap",
            "bootstrap_all_halted": False,
        },
    ),
)
def test_bootstrap_readiness_rejects_incomplete_audit_evidence(
    monkeypatch: pytest.MonkeyPatch,
    audit_payload: dict,
) -> None:
    rollout = _readiness_rollout()
    rows_by_account = {
        "account-a": [_readiness_heartbeat("account-a", rollout)],
        "account-b": [_readiness_heartbeat("account-b", rollout)],
    }
    _stub_readiness_heartbeats(monkeypatch, rows_by_account)
    cursor = _ReadinessCursor(
        event_evidence=_bootstrap_registration_evidence(),
        audit_payload=audit_payload,
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="audit halted state conflicts",
    ):
        reviewed_release_rollout._require_account_rollout_readiness(
            cursor,
            rollout,
            upgraded_accounts=("account-a",),
            next_account="account-b",
            max_age_seconds=5.0,
        )


def test_normal_readiness_rejects_next_account_on_new_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollout = _readiness_rollout()
    rows_by_account = {
        "account-a": [_readiness_heartbeat("account-a", rollout)],
        "account-b": [_readiness_heartbeat("account-b", rollout)],
    }
    _stub_readiness_heartbeats(monkeypatch, rows_by_account)
    cursor = _ReadinessCursor(event_evidence={"registration_mode": "normal"})

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="must remain on an approved old release",
    ):
        reviewed_release_rollout._require_account_rollout_readiness(
            cursor,
            rollout,
            upgraded_accounts=("account-a",),
            next_account="account-b",
            max_age_seconds=5.0,
        )


def test_normal_readiness_accepts_approved_old_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollout = _readiness_rollout()
    old_release = {
        "release_id": "approved-old-release",
        "image_digest": "sha256:" + ("8" * 64),
        "config_sha256": rollout["config_sha256"],
        "dependency_lock_sha256": rollout[
            "dependency_lock_sha256"
        ],
        "schema_epoch": rollout["schema_epoch"],
        "review_status": "reviewed",
    }
    rows_by_account = {
        "account-a": [_readiness_heartbeat("account-a", rollout)],
        "account-b": [
            _readiness_heartbeat(
                "account-b",
                rollout,
                release_id=old_release["release_id"],
                image_digest=old_release["image_digest"],
            )
        ],
    }
    _stub_readiness_heartbeats(monkeypatch, rows_by_account)
    cursor = _ReadinessCursor(
        event_evidence={"registration_mode": "normal"},
        approved_old_release=old_release,
    )

    evidence = reviewed_release_rollout._require_account_rollout_readiness(
        cursor,
        rollout,
        upgraded_accounts=("account-a",),
        next_account="account-b",
        max_age_seconds=5.0,
    )

    assert len(evidence) == 2
    assert cursor.manifest_queries == 1


def test_bootstrap_readiness_requires_next_account_writer_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollout = _readiness_rollout()
    rows_by_account = {
        "account-a": [_readiness_heartbeat("account-a", rollout)],
        "account-b": [
            _readiness_heartbeat(
                "account-b",
                rollout,
                lease_fencing_token=0,
            )
        ],
    }
    _stub_readiness_heartbeats(monkeypatch, rows_by_account)
    cursor = _ReadinessCursor(
        event_evidence=_bootstrap_registration_evidence(),
        audit_payload=_bootstrap_registration_audit(),
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="heartbeat writer identity is invalid",
    ):
        reviewed_release_rollout._require_account_rollout_readiness(
            cursor,
            rollout,
            upgraded_accounts=("account-a",),
            next_account="account-b",
            max_age_seconds=5.0,
        )


@pytest.mark.parametrize(
    "forbidden_option",
    (
        "--release-root-path",
        "--release-source-manifest-sha256",
        "--live-adapter-sha256",
    ),
)
def test_sign_release_gate_cli_rejects_release_identity_injection(
    forbidden_option: str,
) -> None:
    parser = reviewed_release_rollout._parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "sign-release-gate",
                "--manifest",
                "release/release-manifest.json",
                "--bundle-manifest",
                "release/bundle-manifest.json",
                "--account-id",
                "account-a",
                "--signing-private-key",
                "reviewer-private.pem",
                "--output-directory",
                "signed-gate",
                forbidden_option,
                "caller-controlled",
            ]
        )


def test_sign_release_gate_main_does_not_connect_to_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    release_root = tmp_path / "release"
    release_root.mkdir()
    manifest_path, bundle_path = _write_release_material(release_root)
    monkeypatch.setattr(
        release_manifest,
        "validate_strict_release_envelope",
        lambda *_args, **_kwargs: _release_envelope(release_root),
    )
    private_key, _public_key = _write_signing_key(
        tmp_path,
        monkeypatch,
    )
    monkeypatch.setattr(
        reviewed_release_rollout.psycopg2,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("release gate signing reached PostgreSQL")
        ),
    )
    output = tmp_path / "signed-gate"

    result = reviewed_release_rollout.main(
        [
            "sign-release-gate",
            "--manifest",
            str(manifest_path),
            "--bundle-manifest",
            str(bundle_path),
            "--account-id",
            "account-d",
            "--signing-private-key",
            str(private_key),
            "--output-directory",
            str(output),
        ]
    )

    assert result == 0
    command_output = json.loads(capsys.readouterr().out)
    assert command_output["release_gate"] == str(
        output / "release-gate.json"
    )


def test_finalize_cli_requires_account_d_closure_material() -> None:
    parser = reviewed_release_rollout._parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "finalize",
                "--release-id",
                "release-fixture",
                "--actor",
                "reviewer",
                "--reason",
                "four account closure passed",
                "--idempotency-key",
                "fleet-complete:release-fixture",
            ]
        )


def test_finalize_fleet_rollout_binds_fence_and_account_d_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []

    class OperationLock:
        owner_token = "a" * 64

        def require_held(self) -> None:
            events.append("lock")

    def acquire(_conn, **kwargs) -> None:
        events.append(("acquire", kwargs))

    def advance(_conn, release_id, **kwargs):
        events.append(
            (
                "advance",
                release_id,
                kwargs,
                os.environ[
                    reviewed_release_rollout.MAINTENANCE_FENCE_ID_ENV
                ],
            )
        )
        return {"phase": reviewed_release_rollout.PHASE_FLEET_COMPLETE}

    def release(_conn, **kwargs) -> None:
        events.append(("release", kwargs))

    monkeypatch.setattr(
        reviewed_release_rollout,
        "_acquire_finalize_maintenance_fence",
        acquire,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "advance_rollout",
        advance,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_release_finalize_maintenance_fence",
        release,
    )
    monkeypatch.setenv(
        reviewed_release_rollout.MAINTENANCE_FENCE_ID_ENV,
        "previous-fence",
    )

    result = reviewed_release_rollout.finalize_fleet_rollout(
        object(),
        "release-fixture",
        actor="reviewer",
        reason="account-d closure passed",
        idempotency_key="fleet-complete:release-fixture",
        closure_report_path=Path("account-d-report.json"),
        closure_signature_path=Path("account-d-report.sig"),
        closure_public_key_path=Path("reviewer.pem"),
        operation_lock=OperationLock(),
    )

    assert result["phase"] == reviewed_release_rollout.PHASE_FLEET_COMPLETE
    advance_event = next(
        event for event in events if isinstance(event, tuple)
        and event[0] == "advance"
    )
    advance_kwargs = advance_event[2]
    assert advance_kwargs["to_phase"] == (
        reviewed_release_rollout.PHASE_FLEET_COMPLETE
    )
    assert advance_kwargs["closure_report_path"] == Path(
        "account-d-report.json"
    )
    assert advance_event[3] != "previous-fence"
    assert os.environ[
        reviewed_release_rollout.MAINTENANCE_FENCE_ID_ENV
    ] == "previous-fence"
    assert [event[0] for event in events if isinstance(event, tuple)] == [
        "acquire",
        "advance",
        "release",
    ]


def test_finalize_fleet_rollout_releases_fence_after_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released = []

    class OperationLock:
        owner_token = "b" * 64

        def require_held(self) -> None:
            return

    monkeypatch.setattr(
        reviewed_release_rollout,
        "_acquire_finalize_maintenance_fence",
        lambda *_args, **_kwargs: None,
    )

    def reject(*_args, **_kwargs):
        raise reviewed_release_rollout.ReleaseRolloutError(
            "account-d signed closure report is required"
        )

    monkeypatch.setattr(
        reviewed_release_rollout,
        "advance_rollout",
        reject,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "_release_finalize_maintenance_fence",
        lambda *_args, **kwargs: released.append(kwargs),
    )

    with pytest.raises(
        reviewed_release_rollout.ReleaseRolloutError,
        match="account-d signed closure report is required",
    ):
        reviewed_release_rollout.finalize_fleet_rollout(
            object(),
            "release-fixture",
            actor="reviewer",
            reason="account-d closure passed",
            idempotency_key="fleet-complete:release-fixture",
            operation_lock=OperationLock(),
        )

    assert len(released) == 1


def test_mutation_lock_conflict_precedes_database_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lock_path = tmp_path / "account-stall-operation.lock"
    _configure_operation_lock_test(monkeypatch, lock_path)

    def reject_connect(_database_url: str):
        raise AssertionError("lock conflict reached PostgreSQL")

    monkeypatch.setattr(
        reviewed_release_rollout.psycopg2,
        "connect",
        reject_connect,
    )
    with lock_path.open("a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = reviewed_release_rollout.main(
            [
                "--database-url",
                "postgresql://fixture.invalid/trader",
                "advance",
                "--release-id",
                "release-fixture",
                "--to-phase",
                "aborted",
                "--actor",
                "test",
                "--reason",
                "test",
                "--idempotency-key",
                "abort:fixture",
            ]
        )

    assert result == 1
    assert "another account-stall operation holds" in capsys.readouterr().err


def test_inherited_operation_lock_allows_deploy_owned_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "account-stall-operation.lock"
    token = "a" * 64
    connection = type(
        "Connection",
        (),
        {"close": lambda self: None},
    )()
    monkeypatch.setattr(
        reviewed_release_rollout.psycopg2,
        "connect",
        lambda _database_url: connection,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "advance_rollout",
        lambda *_args, **_kwargs: {"phase": "aborted"},
    )

    with lock_path.open("w+b") as holder:
        holder.write((token + "\n").encode("ascii"))
        holder.flush()
        os.fsync(holder.fileno())
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            saved_fd = os.dup(9)
        except OSError:
            saved_fd = False
        os.dup2(holder.fileno(), 9)
        try:
            _configure_operation_lock_test(monkeypatch, lock_path)
            monkeypatch.setenv(
                "ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP",
                "inherited",
            )
            monkeypatch.setenv(
                "ACCOUNT_STALL_OPERATION_LOCK_FD",
                "9",
            )
            monkeypatch.setenv(
                "ACCOUNT_STALL_OPERATION_LOCK_TOKEN",
                token,
            )

            result = reviewed_release_rollout.main(
                [
                    "--database-url",
                    "postgresql://fixture.invalid/trader",
                    "advance",
                    "--release-id",
                    "release-fixture",
                    "--to-phase",
                    "aborted",
                    "--actor",
                    "test",
                    "--reason",
                    "test",
                    "--idempotency-key",
                    "abort:fixture",
                ]
            )
        finally:
            if saved_fd is False:
                os.close(9)
            else:
                os.dup2(saved_fd, 9)
                os.close(saved_fd)

    assert result == 0


def test_status_remains_read_only_while_operation_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "account-stall-operation.lock"
    connection = type(
        "Connection",
        (),
        {"close": lambda self: None},
    )()
    _configure_operation_lock_test(monkeypatch, lock_path)
    monkeypatch.setattr(
        reviewed_release_rollout.psycopg2,
        "connect",
        lambda _database_url: connection,
    )
    monkeypatch.setattr(
        reviewed_release_rollout,
        "get_rollout",
        lambda *_args: {"phase": "account_a_canary"},
    )

    with lock_path.open("a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = reviewed_release_rollout.main(
            [
                "--database-url",
                "postgresql://fixture.invalid/trader",
                "status",
                "--release-id",
                "release-fixture",
            ]
        )

    assert result == 0
