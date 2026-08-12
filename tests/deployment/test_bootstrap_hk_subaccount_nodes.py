from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bootstrap_hk_subaccount_nodes.py"
POLICY = (
    ROOT
    / "services"
    / "nautilus-node"
    / "config"
    / "live-risk-policy.json"
)
SPEC = importlib.util.spec_from_file_location(
    "bootstrap_hk_subaccount_nodes",
    SCRIPT,
)
assert SPEC is not None
assert SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)


API_KEY_C = "c-api-key-sensitive"
API_SECRET_C = "c-api-secret-sensitive"
API_KEY_D = "d-api-key-sensitive"
API_SECRET_D = "d-api-secret-sensitive"
TOKEN_A = "existing-a-token-sensitive"
TOKEN_B = "existing-b-token-sensitive"
TOKEN_C = "generated-c-token-sensitive"
TOKEN_D = "generated-d-token-sensitive"


class FakeDocker:
    def __init__(
        self,
        inspections: dict[str, dict],
        *,
        drift_on_a_inspect: int | bool = False,
    ) -> None:
        self.inspections = inspections
        self.calls: list[list[str]] = []
        self.a_inspect_count = 0
        self.drift_on_a_inspect = drift_on_a_inspect

    def __call__(
        self,
        argv: Sequence[str],
    ) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.calls.append(command)
        if command[1] == "version":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="27.0.0\n",
                stderr="",
            )
        if command[1] != "inspect":
            return subprocess.CompletedProcess(
                command,
                64,
                stdout="",
                stderr="unexpected command",
            )
        container = command[2]
        inspected = self.inspections.get(container)
        if inspected is None:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="missing",
            )
        payload = json.loads(json.dumps(inspected))
        if container == "trader-v3-node-a":
            self.a_inspect_count += 1
            if self.a_inspect_count == self.drift_on_a_inspect:
                payload["RestartCount"] += 1
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps([payload]),
            stderr="",
        )


def _image_id(character: str) -> str:
    return "sha256:" + character * 64


def _container_inspect(
    *,
    name: str,
    container_id: str,
    image_id: str,
    patch_source: Path,
    config_source: Path,
    state_source: Path,
) -> dict:
    return {
        "Id": container_id,
        "Image": image_id,
        "RestartCount": 0,
        "State": {
            "Running": True,
            "StartedAt": "2026-08-11T01:02:03.000000000Z",
        },
        "Config": {
            "Image": "trader-v3:test",
            "Entrypoint": ["/app/bin/run-nautilus-node"],
            "Cmd": ["--foreground"],
            "User": "nautilus",
        },
        "HostConfig": {
            "NetworkMode": "host",
            "RestartPolicy": {
                "Name": "unless-stopped",
                "MaximumRetryCount": 0,
            },
            "Memory": 805306368,
            "MemorySwap": 0,
            "NanoCpus": 0,
            "PidsLimit": 0,
            "Ulimits": [],
            "SecurityOpt": [],
            "CapAdd": [],
            "CapDrop": [],
            "Devices": [],
            "ReadonlyRootfs": False,
            "PidMode": "",
            "IpcMode": "",
            "LogConfig": {
                "Type": "json-file",
                "Config": {},
            },
            "Privileged": False,
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(config_source),
                "Destination": "/cfg.json",
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": str(state_source),
                "Destination": "/state",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": str(patch_source),
                "Destination": "/app/app/health_server.py",
                "RW": False,
            },
        ],
        "Name": f"/{name}",
    }


def _write_watcher_db(path: Path, *, testnet: int = 0) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE account_configs (
                account_id TEXT PRIMARY KEY,
                api_key TEXT NOT NULL,
                api_secret TEXT NOT NULL,
                is_testnet INTEGER NOT NULL,
                account_type TEXT NOT NULL,
                parent_account_id TEXT NOT NULL,
                execution_account_id TEXT NOT NULL,
                risk_capital_multiplier REAL NOT NULL,
                is_enabled INTEGER NOT NULL
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO account_configs (
                account_id,
                api_key,
                api_secret,
                is_testnet,
                account_type,
                parent_account_id,
                execution_account_id,
                risk_capital_multiplier,
                is_enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "main-account",
                    "main-key",
                    "main-secret",
                    testnet,
                    "main",
                    "",
                    "account-a",
                    1.0,
                    1,
                ),
                (
                    "泰山",
                    API_KEY_C,
                    API_SECRET_C,
                    testnet,
                    "subaccount",
                    "main-account",
                    "account-c",
                    2.96902319,
                    1,
                ),
                (
                    "黄山",
                    API_KEY_D,
                    API_SECRET_D,
                    testnet,
                    "subaccount",
                    "main-account",
                    "account-d",
                    3.0,
                    1,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    path.chmod(0o640)


def _write_auth_env(path: Path) -> None:
    bindings = {
        "nautilus-node-account-a": {
            "account_id": "account-a",
            "token": TOKEN_A,
        },
        "nautilus-node-account-b": {
            "account_id": "account-b",
            "token": TOKEN_B,
        },
    }
    serialized = json.dumps(
        bindings,
        sort_keys=True,
        separators=(",", ":"),
    )
    path.write_text(
        f"NAUTILUS_NODE_AUTH_JSON='{serialized}'\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _fixture(
    tmp_path: Path,
    *,
    mode: str = "check",
    expected_pin: str = "",
    drift_on_a_inspect: int | bool = False,
    testnet: int = 0,
):
    trader_root = tmp_path / "trader-v3"
    trader_root.mkdir(mode=0o700)
    patch_dir = trader_root / "container-patches"
    patch_dir.mkdir()
    patch_source = patch_dir / "health_server.py"
    patch_source.write_text("# reviewed patch\n", encoding="utf-8")
    for suffix in ("a", "b"):
        config = trader_root / f"node-{suffix}.hk.json"
        config.write_text("{}\n", encoding="utf-8")
        state = trader_root / "node-state" / suffix
        state.mkdir(parents=True)
    watcher_db = tmp_path / "watcher-trading.db"
    _write_watcher_db(watcher_db, testnet=testnet)
    auth_env = trader_root / ".env.v3"
    _write_auth_env(auth_env)
    inspections = {
        "trader-v3-node-a": _container_inspect(
            name="trader-v3-node-a",
            container_id="a" * 64,
            image_id=_image_id("1"),
            patch_source=patch_source,
            config_source=trader_root / "node-a.hk.json",
            state_source=trader_root / "node-state" / "a",
        ),
        "trader-v3-node-b": _container_inspect(
            name="trader-v3-node-b",
            container_id="b" * 64,
            image_id=_image_id("2"),
            patch_source=patch_source,
            config_source=trader_root / "node-b.hk.json",
            state_source=trader_root / "node-state" / "b",
        ),
    }
    docker = FakeDocker(
        inspections,
        drift_on_a_inspect=drift_on_a_inspect,
    )
    targets = (
        bootstrap.AccountTarget(
            suffix="c",
            credential_account_id="泰山",
            execution_account_id="account-c",
            channel_name="坚果",
            health_port=8083,
        ),
        bootstrap.AccountTarget(
            suffix="d",
            credential_account_id="黄山",
            execution_account_id="account-d",
            channel_name="03Cash",
            health_port=8084,
        ),
    )
    options = bootstrap.BootstrapOptions(
        mode=mode,
        watcher_db=watcher_db,
        risk_policy=POLICY,
        control_plane_env_files=(auth_env,),
        paths=bootstrap.BootstrapPaths(
            trader_root=trader_root,
            secret_dir=trader_root / "secrets" / "subaccount-nodes",
            artifact_dir=trader_root / "bootstrap" / "subaccount-nodes",
        ),
        template_container="trader-v3-node-b",
        docker_binary="docker",
        expected_ab_pin_sha256=expected_pin,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
        node_uid=os.getuid(),
        node_gid=os.getgid(),
        targets=targets,
    )
    return options, docker


def _all_sensitive_values() -> tuple[str, ...]:
    return (
        API_KEY_C,
        API_SECRET_C,
        API_KEY_D,
        API_SECRET_D,
        TOKEN_A,
        TOKEN_B,
        TOKEN_C,
        TOKEN_D,
    )


def test_default_cli_mode_is_check() -> None:
    options = bootstrap.parse_options([])

    assert options.mode == "check"


def test_check_is_read_only_and_report_is_secret_free(
    tmp_path: Path,
) -> None:
    options, docker = _fixture(tmp_path)

    report = bootstrap.run(options, runner=docker)

    assert report["status"] == "check_passed"
    assert report["writes_performed"] is False
    assert report["docker_run_specs"]["account-c"]["argv"]
    assert report["docker_run_specs"]["account-d"]["argv"]
    assert not options.paths.secret_dir.exists()
    assert not options.paths.artifact_dir.exists()
    assert not options.paths.config_path(options.targets[0]).exists()
    serialized = json.dumps(report, ensure_ascii=False)
    for secret_value in _all_sensitive_values():
        assert secret_value not in serialized
    executed_commands = {
        call[1]
        for call in docker.calls
    }
    assert executed_commands == {"version", "inspect"}


def test_check_generates_halted_host_network_isolated_specs(
    tmp_path: Path,
) -> None:
    options, docker = _fixture(tmp_path)

    report = bootstrap.run(options, runner=docker)
    spec_c = report["docker_run_specs"]["account-c"]
    spec_d = report["docker_run_specs"]["account-d"]
    config_c = report["live_configs"]["account-c"]
    config_d = report["live_configs"]["account-d"]

    for spec, port in ((spec_c, 8083), (spec_d, 8084)):
        argv = spec["argv"]
        assert "--network=host" in argv
        assert "--user=999:999" in argv
        assert "NAUTILUS_INITIAL_TRADING_STATE=HALTED" in argv
        assert f"NAUTILUS_HEALTH_PORT={port}" in argv
        assert all("--publish" != value for value in argv)
    assert config_c["binance"]["environment"] == "live"
    assert config_d["binance"]["environment"] == "live"
    assert config_c["redis"]["key_prefix"] == "nautilus:account-c:node-c"
    assert config_d["redis"]["key_prefix"] == "nautilus:account-d:node-d"
    assert config_c["trader_id"] != config_d["trader_id"]
    assert config_c["instance_id"] != config_d["instance_id"]
    assert config_c["runtime_resources"]["control_plane_session"]
    assert config_d["runtime_resources"]["reporter_workers"]
    assert "proxy_url" not in config_c["binance"]


def test_apply_requires_reviewed_ab_pin(tmp_path: Path) -> None:
    options, docker = _fixture(tmp_path, mode="apply")

    with pytest.raises(
        bootstrap.BootstrapError,
        match="expected-ab-pin",
    ):
        bootstrap.run(options, runner=docker)

    assert not options.paths.secret_dir.exists()
    assert not options.paths.artifact_dir.exists()


def test_apply_writes_private_artifacts_and_preserves_secrets(
    tmp_path: Path,
) -> None:
    check_options, check_docker = _fixture(tmp_path)
    check_report = bootstrap.run(check_options, runner=check_docker)
    apply_options = bootstrap.BootstrapOptions(
        **{
            **check_options.__dict__,
            "mode": "apply",
            "expected_ab_pin_sha256": check_report["ab_pin_sha256"],
        }
    )
    apply_docker = FakeDocker(check_docker.inspections)
    tokens = iter((TOKEN_C, TOKEN_D))

    report = bootstrap.run(
        apply_options,
        runner=apply_docker,
        token_factory=lambda: next(tokens),
    )

    assert report["status"] == "prepared"
    assert report["docker_run_executed"] is False
    assert report["secret_file_count"] == 6
    target_c, target_d = apply_options.targets
    expected_secret_values = {
        apply_options.paths.secret_path(
            target_c,
            "binance_api_key",
        ): API_KEY_C,
        apply_options.paths.secret_path(
            target_c,
            "binance_api_secret",
        ): API_SECRET_C,
        apply_options.paths.secret_path(
            target_c,
            "control_plane_token",
        ): TOKEN_C,
        apply_options.paths.secret_path(
            target_d,
            "binance_api_key",
        ): API_KEY_D,
        apply_options.paths.secret_path(
            target_d,
            "binance_api_secret",
        ): API_SECRET_D,
        apply_options.paths.secret_path(
            target_d,
            "control_plane_token",
        ): TOKEN_D,
    }
    for path, expected_value in expected_secret_values.items():
        assert path.read_text(encoding="utf-8").strip() == expected_value
        metadata = path.stat()
        assert stat.S_IMODE(metadata.st_mode) == 0o440
        assert metadata.st_uid == os.getuid()
        assert metadata.st_gid == os.getgid()
    auth_proposal = json.loads(
        apply_options.paths.auth_proposal_path.read_text(
            encoding="utf-8"
        )
    )
    bindings = auth_proposal["bindings"]
    assert bindings["nautilus-node-account-a"]["token"] == TOKEN_A
    assert bindings["nautilus-node-account-b"]["token"] == TOKEN_B
    assert bindings["nautilus-node-account-c"]["token"] == TOKEN_C
    assert bindings["nautilus-node-account-d"]["token"] == TOKEN_D
    assert len(
        {
            binding["token"]
            for binding in bindings.values()
        }
    ) == 4
    assert (
        stat.S_IMODE(
            apply_options.paths.auth_proposal_path.stat().st_mode
        )
        == 0o400
    )
    serialized_report = json.dumps(report, ensure_ascii=False)
    manifest_text = apply_options.paths.manifest_path.read_text(
        encoding="utf-8"
    )
    for secret_value in _all_sensitive_values():
        assert secret_value not in serialized_report
        assert secret_value not in manifest_text


def test_apply_records_rollback_without_a_b_destructive_actions(
    tmp_path: Path,
) -> None:
    check_options, check_docker = _fixture(tmp_path)
    check_report = bootstrap.run(check_options, runner=check_docker)
    apply_options = bootstrap.BootstrapOptions(
        **{
            **check_options.__dict__,
            "mode": "apply",
            "expected_ab_pin_sha256": check_report["ab_pin_sha256"],
        }
    )
    tokens = iter((TOKEN_C, TOKEN_D))

    bootstrap.run(
        apply_options,
        runner=FakeDocker(check_docker.inspections),
        token_factory=lambda: next(tokens),
    )

    rollback = json.loads(
        apply_options.paths.rollback_path.read_text(encoding="utf-8")
    )
    docker_targets = {
        action["container"]
        for action in rollback["actions"]
        if action["action"] == (
            "docker_remove_if_created_from_reviewed_spec"
        )
    }
    assert docker_targets == {
        "trader-v3-node-c",
        "trader-v3-node-d",
    }
    assert rollback["forbidden_targets"] == [
        "trader-v3-node-a",
        "trader-v3-node-b",
    ]
    rollback_text = json.dumps(rollback, ensure_ascii=False)
    for secret_value in _all_sensitive_values():
        assert secret_value not in rollback_text


def test_apply_fails_before_writes_when_ab_pin_drifts(
    tmp_path: Path,
) -> None:
    check_options, check_docker = _fixture(tmp_path)
    check_report = bootstrap.run(check_options, runner=check_docker)
    apply_options = bootstrap.BootstrapOptions(
        **{
            **check_options.__dict__,
            "mode": "apply",
            "expected_ab_pin_sha256": check_report["ab_pin_sha256"],
        }
    )
    drifting_docker = FakeDocker(
        check_docker.inspections,
        drift_on_a_inspect=2,
    )

    with pytest.raises(
        bootstrap.BootstrapError,
        match="pins drifted",
    ):
        bootstrap.run(
            apply_options,
            runner=drifting_docker,
            token_factory=lambda: TOKEN_C,
        )

    assert not apply_options.paths.secret_dir.exists()
    assert not apply_options.paths.artifact_dir.exists()
    assert not apply_options.paths.config_path(
        apply_options.targets[0]
    ).exists()


def test_apply_rejects_group_writable_watcher_database(
    tmp_path: Path,
) -> None:
    check_options, check_docker = _fixture(tmp_path)
    check_report = bootstrap.run(check_options, runner=check_docker)
    check_options.watcher_db.chmod(0o666)
    apply_options = bootstrap.BootstrapOptions(
        **{
            **check_options.__dict__,
            "mode": "apply",
            "expected_ab_pin_sha256": check_report["ab_pin_sha256"],
        }
    )

    with pytest.raises(
        bootstrap.BootstrapError,
        match="group/world writable",
    ):
        bootstrap.run(
            apply_options,
            runner=FakeDocker(check_docker.inspections),
        )

    assert not apply_options.paths.secret_dir.exists()


def test_check_rejects_testnet_subaccounts(tmp_path: Path) -> None:
    options, docker = _fixture(tmp_path, testnet=1)

    with pytest.raises(
        bootstrap.BootstrapError,
        match="must use live credentials",
    ):
        bootstrap.run(options, runner=docker)


def test_run_specs_and_live_configs_contain_no_secret_values(
    tmp_path: Path,
) -> None:
    options, docker = _fixture(tmp_path)

    report = bootstrap.run(options, runner=docker)
    material = json.dumps(
        {
            "configs": report["live_configs"],
            "specs": report["docker_run_specs"],
        },
        ensure_ascii=False,
    )

    for secret_value in _all_sensitive_values():
        assert secret_value not in material
