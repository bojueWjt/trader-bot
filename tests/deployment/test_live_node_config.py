from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "live_node_config.py"
POLICY = (
    REPO_ROOT
    / "services"
    / "nautilus-node"
    / "config"
    / "live-risk-policy.json"
)
SPEC = importlib.util.spec_from_file_location("live_node_config", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
live_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live_config)


def _write_configs(tmp_path: Path) -> dict[str, Path]:
    configs = {}
    for suffix in ("a", "b", "c", "d"):
        account_id = f"account-{suffix}"
        path = tmp_path / f"node-{suffix}.hk.json"
        config = {
            "account_id": account_id,
            "node_id": f"nautilus-node-{account_id}",
            "trader_id": f"trader-{account_id}",
            "instance_id": f"instance-{account_id}",
            "binance": {
                "api_key": {
                    "env": f"BINANCE_ACCOUNT_{suffix.upper()}_KEY"
                },
                "api_secret": {
                    "file": f"/run/secrets/binance-account-{suffix}"
                },
            },
            "control_plane": {
                "base_url": (
                    live_config
                    .ACCOUNT_NETWORK_CONTROL_PLANE_BASE_URLS[
                        account_id
                    ]
                ),
                "token": {
                    "env": (
                        f"CONTROL_PLANE_ACCOUNT_{suffix.upper()}_TOKEN"
                    )
                }
            },
        }
        if account_id == "account-d":
            config["binance"]["proxy_url"] = (
                "http://100.107.72.78:13128"
            )
        path.write_text(
            json.dumps(
                config,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o640)
        configs[account_id] = path
    return configs


def test_normalized_hash_rejects_account_network_gateway_mismatch() -> None:
    configs = []
    for suffix in ("a", "b", "c", "d"):
        account_id = f"account-{suffix}"
        configs.append(
            {
                "account_id": account_id,
                "node_id": f"node-{suffix}",
                "control_plane": {
                    "base_url": (
                        live_config
                        .ACCOUNT_NETWORK_CONTROL_PLANE_BASE_URLS[
                            account_id
                        ]
                    )
                },
            }
        )
    expected_hashes = {
        live_config.normalized_config_sha256(config)
        for config in configs
    }
    assert len(expected_hashes) == 1

    configs[1]["control_plane"]["base_url"] = "http://172.30.9.1:8080"
    assert live_config.normalized_config_sha256(configs[1]) not in (
        expected_hashes
    )


def _legacy_environment(
    cap: str = "100",
    *,
    include_rates: bool = True,
) -> list[str]:
    caps = {
        instrument: cap
        for instrument in sorted(live_config.ALLOWED_INSTRUMENTS)
    }
    environment = [
        (
            "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON="
            + json.dumps(caps, separators=(",", ":"), sort_keys=True)
        ),
    ]
    if include_rates:
        environment.extend(
            [
                "NAUTILUS_MAX_ORDER_SUBMIT_RATE=50/00:00:01",
                "NAUTILUS_MAX_ORDER_MODIFY_RATE=1/00:00:01",
            ]
        )
    return environment


def _capture(
    tmp_path: Path,
    configs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    cap: str = "100",
) -> Path:
    monkeypatch.setattr(
        live_config,
        "_docker_inspect_environment",
        lambda _container: _legacy_environment(cap),
    )
    path = tmp_path / "legacy-risk-capture.json"
    live_config.capture_legacy_risk(
        policy_path=POLICY,
        configs=configs,
        containers={
            "account-a": "trader-v3-node-a",
            "account-b": "trader-v3-node-b",
            "account-c": "trader-v3-node-c",
            "account-d": "trader-v3-node-d",
        },
        output_path=path,
        expected_owner_uid=os.getuid(),
    )
    return path


def test_apply_policy_updates_caps_and_preserves_secret_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _write_configs(tmp_path)
    backup_dir = tmp_path / "backup"
    legacy_risk = _capture(tmp_path, configs, monkeypatch)

    digest = live_config.apply_policy(
        policy_path=POLICY,
        legacy_risk_path=legacy_risk,
        configs=configs,
        backup_dir=backup_dir,
        expected_owner_uid=os.getuid(),
    )

    assert len(digest) == 64
    for account_id, path in configs.items():
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["account_id"] == account_id
        assert config["binance"]["api_key"]["env"]
        assert config["binance"]["api_secret"]["file"]
        caps = config["risk"]["max_notional_per_order"]
        assert set(caps) == (
            live_config.ALLOWED_INSTRUMENTS
            | {live_config.DEFAULT_NOTIONAL_KEY}
        )
        assert caps[live_config.DEFAULT_NOTIONAL_KEY] == "10000"
        assert {
            caps[instrument]
            for instrument in live_config.ALLOWED_INSTRUMENTS
        } == {"100000"}
        assert config["risk"]["max_order_submit_rate"] == "50/00:00:01"
        assert config["risk"]["max_order_modify_rate"] == "1/00:00:01"
    manifest = json.loads(
        (backup_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "applied"
    assert manifest["normalized_config_sha256"] == digest
    normalized_by_account = {
        account_id: live_config.normalized_config_sha256(
            json.loads(path.read_text(encoding="utf-8"))
        )
        for account_id, path in configs.items()
    }
    assert manifest["normalized_config_sha256_by_account"] == (
        normalized_by_account
    )
    assert digest == live_config.normalized_config_contract_sha256(
        normalized_by_account
    )
    assert len(
        {
            normalized_by_account[account_id]
            for account_id in ("account-a", "account-b", "account-c")
        }
    ) == 1
    assert normalized_by_account["account-d"] != normalized_by_account[
        "account-a"
    ]


def test_capture_uses_reviewed_defaults_for_production_shaped_legacy_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _write_configs(tmp_path)
    monkeypatch.setattr(
        live_config,
        "_docker_inspect_environment",
        lambda _container: _legacy_environment(include_rates=False),
    )
    capture_path = tmp_path / "legacy-risk-capture.json"

    captured = live_config.capture_legacy_risk(
        policy_path=POLICY,
        configs=configs,
        containers={
            "account-a": "trader-v3-node-a",
            "account-b": "trader-v3-node-b",
            "account-c": "trader-v3-node-c",
            "account-d": "trader-v3-node-d",
        },
        output_path=capture_path,
        expected_owner_uid=os.getuid(),
    )

    assert captured["risk"]["max_order_submit_rate"] == "50/00:00:01"
    assert captured["risk"]["max_order_modify_rate"] == "1/00:00:01"
    assert set(
        captured["risk"]["max_notional_per_order"].values()
    ) == {"100"}


def test_capture_rejects_rate_only_legacy_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _write_configs(tmp_path)
    monkeypatch.setattr(
        live_config,
        "_docker_inspect_environment",
        lambda _container: [
            "NAUTILUS_MAX_ORDER_SUBMIT_RATE=50/00:00:01"
        ],
    )

    with pytest.raises(
        live_config.LiveNodeConfigError,
        match="lacks max_notional",
    ):
        live_config.capture_legacy_risk(
            policy_path=POLICY,
            configs=configs,
            containers={
                "account-a": "trader-v3-node-a",
                "account-b": "trader-v3-node-b",
                "account-c": "trader-v3-node-c",
                "account-d": "trader-v3-node-d",
            },
            output_path=tmp_path / "capture.json",
            expected_owner_uid=os.getuid(),
        )


def test_applied_manifest_write_failure_rolls_back_both_configs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _write_configs(tmp_path)
    originals = {
        account_id: path.read_bytes()
        for account_id, path in configs.items()
    }
    backup_dir = tmp_path / "backup"
    legacy_risk = _capture(tmp_path, configs, monkeypatch)
    original_write_private = live_config._write_private
    failed = False

    def fail_final_applied_write(
        path: Path,
        payload: bytes,
        owner_uid: int,
    ) -> None:
        nonlocal failed
        document = json.loads(payload)
        if (
            path.name == "manifest.json"
            and document.get("status") == "applied"
            and not failed
        ):
            failed = True
            raise OSError("injected applied manifest fsync failure")
        original_write_private(path, payload, owner_uid)

    monkeypatch.setattr(
        live_config,
        "_write_private",
        fail_final_applied_write,
    )

    with pytest.raises(OSError, match="injected applied manifest"):
        live_config.apply_policy(
            policy_path=POLICY,
            legacy_risk_path=legacy_risk,
            configs=configs,
            backup_dir=backup_dir,
            expected_owner_uid=os.getuid(),
        )

    assert failed
    for account_id, path in configs.items():
        assert path.read_bytes() == originals[account_id]
    manifest = json.loads(
        (backup_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "rolled_back"


def test_capture_rejects_legacy_cap_above_reviewed_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _write_configs(tmp_path)
    monkeypatch.setattr(
        live_config,
        "_docker_inspect_environment",
        lambda _container: _legacy_environment("100001"),
    )

    with pytest.raises(
        live_config.LiveNodeConfigError,
        match="at most 100000",
    ):
        live_config.capture_legacy_risk(
            policy_path=POLICY,
            configs=configs,
            containers={
                "account-a": "trader-v3-node-a",
                "account-b": "trader-v3-node-b",
                "account-c": "trader-v3-node-c",
                "account-d": "trader-v3-node-d",
            },
            output_path=tmp_path / "capture.json",
            expected_owner_uid=os.getuid(),
        )


def test_prepare_target_creates_immutable_artifact_without_touching_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _write_configs(tmp_path)
    legacy_risk = _capture(tmp_path, configs, monkeypatch)
    originals = {
        account_id: {
            "payload": path.read_bytes(),
            "inode": path.stat().st_ino,
        }
        for account_id, path in configs.items()
    }
    artifact_root = tmp_path / "config-artifacts"
    record_path = tmp_path / "account-a-artifact.json"

    first = live_config.prepare_target_config_artifact(
        account_id="account-a",
        source_config_path=configs["account-a"],
        policy_path=POLICY,
        legacy_risk_path=legacy_risk,
        artifact_root=artifact_root,
        record_output_path=record_path,
        expected_owner_uid=os.getuid(),
    )
    second = live_config.prepare_target_config_artifact(
        account_id="account-a",
        source_config_path=configs["account-a"],
        policy_path=POLICY,
        legacy_risk_path=legacy_risk,
        artifact_root=artifact_root,
        record_output_path=record_path,
        expected_owner_uid=os.getuid(),
    )

    assert first["host_path"] == second["host_path"]
    assert first["inode"] == second["inode"]
    assert first["sha256"] == second["sha256"]
    artifact_path = Path(first["host_path"])
    assert artifact_path.name == f"{first['sha256']}.json"
    assert artifact_path.parent.name == "account-a"
    assert artifact_path.stat().st_mode & 0o222 == 0
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["account_id"] == "account-a"
    assert artifact["risk"]["max_notional_per_order"]
    caps = artifact["risk"]["max_notional_per_order"]
    assert caps[live_config.DEFAULT_NOTIONAL_KEY] == "10000"
    assert {
        caps[instrument]
        for instrument in live_config.ALLOWED_INSTRUMENTS
    } == {"100000"}
    for account_id, path in configs.items():
        assert path.read_bytes() == originals[account_id]["payload"]
        assert path.stat().st_ino == originals[account_id]["inode"]


def test_serial_target_artifacts_bind_per_account_normalized_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _write_configs(tmp_path)
    legacy_risk = _capture(tmp_path, configs, monkeypatch)
    records = {}
    for account_id in ("account-a", "account-b", "account-c", "account-d"):
        records[account_id] = (
            live_config.prepare_target_config_artifact(
                account_id=account_id,
                source_config_path=configs[account_id],
                policy_path=POLICY,
                legacy_risk_path=legacy_risk,
                artifact_root=tmp_path / "config-artifacts",
                record_output_path=tmp_path / f"{account_id}-artifact.json",
                expected_owner_uid=os.getuid(),
            )
        )

    assert len({record["sha256"] for record in records.values()}) == 4
    assert len(
        {
            records[account_id]["normalized_sha256"]
            for account_id in ("account-a", "account-b", "account-c")
        }
    ) == 1
    assert records["account-d"]["normalized_sha256"] != records[
        "account-a"
    ]["normalized_sha256"]
    for account_id, record in records.items():
        assert Path(record["host_path"]).parent.name == account_id


def test_prepare_target_rejects_mutable_or_tampered_existing_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _write_configs(tmp_path)
    legacy_risk = _capture(tmp_path, configs, monkeypatch)
    arguments = {
        "account_id": "account-a",
        "source_config_path": configs["account-a"],
        "policy_path": POLICY,
        "legacy_risk_path": legacy_risk,
        "artifact_root": tmp_path / "config-artifacts",
        "record_output_path": tmp_path / "account-a-artifact.json",
        "expected_owner_uid": os.getuid(),
    }
    record = live_config.prepare_target_config_artifact(**arguments)
    artifact_path = Path(record["host_path"])
    artifact_path.chmod(0o640)

    with pytest.raises(
        live_config.LiveNodeConfigError,
        match="must be read-only",
    ):
        live_config.prepare_target_config_artifact(**arguments)


def test_prepare_target_rejects_reused_record_for_different_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _write_configs(tmp_path)
    legacy_risk = _capture(tmp_path, configs, monkeypatch)
    record_path = tmp_path / "account-a-artifact.json"
    live_config.prepare_target_config_artifact(
        account_id="account-a",
        source_config_path=configs["account-a"],
        policy_path=POLICY,
        legacy_risk_path=legacy_risk,
        artifact_root=tmp_path / "config-artifacts",
        record_output_path=record_path,
        expected_owner_uid=os.getuid(),
    )
    config = json.loads(
        configs["account-a"].read_text(encoding="utf-8")
    )
    config["node_id"] = "nautilus-node-account-a-replacement"
    configs["account-a"].write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        live_config.LiveNodeConfigError,
        match="record already differs",
    ):
        live_config.prepare_target_config_artifact(
            account_id="account-a",
            source_config_path=configs["account-a"],
            policy_path=POLICY,
            legacy_risk_path=legacy_risk,
            artifact_root=tmp_path / "config-artifacts",
            record_output_path=record_path,
            expected_owner_uid=os.getuid(),
        )
