#!/usr/bin/env python3
"""Bootstrap account-c/account-d as isolated HALTED HK nodes.

Default mode is read-only validation. Pass ``--apply`` on the HK host to write
reviewed configs/secrets, extend the control-plane node binding, and create the
two containers. Secret values are never printed.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRADER_ROOT = Path("/srv/trader-v3")
WATCHER_DB = Path(
    "/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db"
)
ENV_FILE = TRADER_ROOT / ".env.v3"
SOURCE_CONTAINER = "trader-v3-node-b"
SOURCE_CONFIG = TRADER_ROOT / "node-b.hk.json"
BACKUP_ROOT = Path(
    "/srv/trader-backups/subaccount-onboarding-20260811/subaccount-nodes"
)
SECRET_ROOT = TRADER_ROOT / "secrets" / "subaccounts"
EXPECTED_IMAGE_ID = (
    "sha256:19f6f9b78c9df4c94cc1e2bc502b5429729bf3de6e9390105abbf210ac732f83"
)
EXPECTED_SOURCE_MEMORY = 805306368
EXPECTED_WATCHER_MODE = 0o600
EXPECTED_SECRET_MODE = 0o440
EXPECTED_CONFIG_MODE = 0o440
NODE_READY_TIMEOUT_SECONDS = 120
CONTROL_PLANE_READY_TIMEOUT_SECONDS = 30

RISK_CAPS = {
    "BNBUSDT-PERP.BINANCE": "100",
    "BTCUSDT-PERP.BINANCE": "100",
    "ETHUSDT-PERP.BINANCE": "100",
    "SOLUSDT-PERP.BINANCE": "100",
}

RUNTIME_RESOURCES = {
    "schema_version": "trader-v3-runtime-resources/v1",
    "redis": {
        "stream_max_entries": 100000,
        "stream_max_bytes": 268435456,
        "total_stream_max_bytes": 1073741824,
        "scan_count": 500,
        "sample_interval_seconds": 5,
        "critical_window_seconds": 30,
        "thread_join_timeout_seconds": 5,
        "memory_warning_ratio": 0.6,
        "memory_degraded_ratio": 0.75,
        "memory_critical_ratio": 0.85,
    },
    "command_journal": {
        "max_bytes": 16777216,
    },
    "control_plane_session": {
        "command_delivery_capacity": 128,
        "command_ack_capacity": 256,
        "intent_delivery_capacity": 256,
        "execution_event_capacity": 1024,
        "queue_degraded_ratio": 0.8,
        "retry_budget": 3,
        "retry_base_delay_seconds": 0.05,
        "retry_max_delay_seconds": 1.0,
        "retry_jitter_ratio": 0.2,
        "circuit_reset_seconds": 5,
        "operation_timeout_seconds": 15,
        "shutdown_timeout_seconds": 1,
        "stream_failure_halt_after_seconds": 30,
    },
    "strategy_durable_io": {
        "queue_capacity": 128,
        "task_timeout_seconds": 1,
        "shutdown_timeout_seconds": 2,
    },
    "terminal_exchange": {
        "queue_capacity": 64,
        "result_queue_capacity": 128,
        "degraded_ratio": 0.8,
        "total_deadline_seconds": 6,
        "shutdown_timeout_seconds": 7,
    },
    "reporter_workers": {
        "denial_queue_capacity": 256,
        "protection_event_queue_capacity": 1024,
        "live_canary_risk_queue_capacity": 128,
        "incident_queue_capacity": 64,
        "task_timeout_seconds": 5,
        "shutdown_timeout_seconds": 5,
    },
}


class BootstrapError(RuntimeError):
    """Raised when a production precondition or verification fails."""


@dataclass(frozen=True)
class AccountTarget:
    suffix: str
    credential_account_id: str
    execution_account_id: str
    channel_name: str
    health_port: int

    @property
    def account_id(self) -> str:
        return self.execution_account_id

    @property
    def node_id(self) -> str:
        return f"nautilus-node-{self.execution_account_id}"

    @property
    def trader_id(self) -> str:
        return f"TRADER-{self.execution_account_id.upper()}"

    @property
    def instance_id(self) -> str:
        return f"INSTANCE-{self.execution_account_id.upper()}"

    @property
    def container(self) -> str:
        return f"trader-v3-node-{self.suffix}"

    @property
    def redis_prefix(self) -> str:
        return f"nautilus:{self.execution_account_id}:node-{self.suffix}"

    @property
    def key_env(self) -> str:
        return f"BINANCE_ACCOUNT_{self.suffix.upper()}_API_KEY"

    @property
    def secret_env(self) -> str:
        return f"BINANCE_ACCOUNT_{self.suffix.upper()}_API_SECRET"

    @property
    def token_env(self) -> str:
        return f"CONTROL_PLANE_ACCOUNT_{self.suffix.upper()}_TOKEN"


@dataclass(frozen=True)
class BootstrapPaths:
    trader_root: Path
    secret_dir: Path
    artifact_dir: Path

    def config_path(self, target: AccountTarget) -> Path:
        return self.trader_root / f"node-{target.suffix}.hk.json"

    def state_path(self, target: AccountTarget) -> Path:
        return self.trader_root / "node-state" / target.suffix

    def secret_path(self, target: AccountTarget, kind: str) -> Path:
        names = {
            "binance_api_key": f"binance_account_{target.suffix}_api_key",
            "binance_api_secret": f"binance_account_{target.suffix}_api_secret",
            "control_plane_token": f"control_plane_account_{target.suffix}_token",
        }
        selected = names.get(kind)
        if not selected:
            raise BootstrapError(f"unknown secret kind: {kind}")
        return self.secret_dir / selected

    @property
    def auth_proposal_path(self) -> Path:
        return self.artifact_dir / "nautilus-node-auth.proposal.json"

    @property
    def manifest_path(self) -> Path:
        return self.artifact_dir / "bootstrap-manifest.json"

    @property
    def rollback_path(self) -> Path:
        return self.artifact_dir / "rollback-plan.json"


@dataclass(frozen=True)
class BootstrapOptions:
    mode: str
    watcher_db: Path
    risk_policy: Path
    control_plane_env_files: tuple[Path, ...]
    paths: BootstrapPaths
    template_container: str
    docker_binary: str
    expected_ab_pin_sha256: str
    owner_uid: int
    owner_gid: int
    node_uid: int
    node_gid: int
    targets: tuple[AccountTarget, ...]


@dataclass(frozen=True)
class AccountSpec:
    account_id: str
    credential_account_id: str
    node_id: str
    trader_id: str
    instance_id: str
    container: str
    suffix: str
    health_port: int
    redis_prefix: str
    key_env: str
    secret_env: str
    token_env: str

    @property
    def config_path(self) -> Path:
        return TRADER_ROOT / f"node-{self.suffix}.hk.json"

    @property
    def state_path(self) -> Path:
        return TRADER_ROOT / "node-state" / self.suffix

    @property
    def api_key_path(self) -> Path:
        return SECRET_ROOT / f"binance_account_{self.suffix}_api_key"

    @property
    def api_secret_path(self) -> Path:
        return SECRET_ROOT / f"binance_account_{self.suffix}_api_secret"

    @property
    def token_path(self) -> Path:
        return SECRET_ROOT / f"control_plane_account_{self.suffix}_token"


SPECS = (
    AccountSpec(
        account_id="account-c",
        credential_account_id="泰山",
        node_id="nautilus-node-account-c",
        trader_id="TRADER-ACCOUNT-C",
        instance_id="INSTANCE-ACCOUNT-C",
        container="trader-v3-node-c",
        suffix="c",
        health_port=8083,
        redis_prefix="nautilus:account-c:node-c",
        key_env="BINANCE_ACCOUNT_C_API_KEY",
        secret_env="BINANCE_ACCOUNT_C_API_SECRET",
        token_env="CONTROL_PLANE_ACCOUNT_C_TOKEN",
    ),
    AccountSpec(
        account_id="account-d",
        credential_account_id="黄山",
        node_id="nautilus-node-account-d",
        trader_id="TRADER-ACCOUNT-D",
        instance_id="INSTANCE-ACCOUNT-D",
        container="trader-v3-node-d",
        suffix="d",
        health_port=8084,
        redis_prefix="nautilus:account-d:node-d",
        key_env="BINANCE_ACCOUNT_D_API_KEY",
        secret_env="BINANCE_ACCOUNT_D_API_SECRET",
        token_env="CONTROL_PLANE_ACCOUNT_D_TOKEN",
    ),
)


def _run_command(
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        capture_output=capture_output,
        text=True,
        timeout=60,
    )


def run(
    args: list[str] | BootstrapOptions,
    *,
    check: bool = True,
    capture_output: bool = True,
    runner=None,
    token_factory=None,
):
    if isinstance(args, BootstrapOptions):
        return run_bootstrap(args, runner=runner, token_factory=token_factory)
    return _run_command(args, check=check, capture_output=capture_output)


def docker_inspect(container: str) -> dict[str, Any]:
    result = run(["docker", "inspect", container])
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != 1:
        raise BootstrapError(f"docker inspect did not resolve {container}")
    inspected = rows[0]
    if not isinstance(inspected, dict):
        raise BootstrapError(f"docker inspect returned invalid data for {container}")
    return inspected


def container_exists(container: str) -> bool:
    result = run(
        ["docker", "inspect", container],
        check=False,
    )
    return result.returncode == 0


def _runner_call(
    runner,
    args: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if runner is None:
        return run(args, check=check)
    result = runner(args)
    if check and result.returncode != 0:
        raise BootstrapError(result.stderr or f"command failed: {args}")
    return result


def _runner_docker_inspect(
    options: BootstrapOptions,
    runner,
    container: str,
) -> dict[str, Any]:
    result = _runner_call(
        runner,
        [options.docker_binary, "inspect", container],
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != 1:
        raise BootstrapError(f"docker inspect did not resolve {container}")
    inspected = rows[0]
    if not isinstance(inspected, dict):
        raise BootstrapError(f"docker inspect returned invalid data for {container}")
    return inspected


def _runner_container_exists(
    options: BootstrapOptions,
    runner,
    container: str,
) -> bool:
    result = _runner_call(
        runner,
        [options.docker_binary, "inspect", container],
        check=False,
    )
    return result.returncode == 0


def _runner_container_fingerprint(
    options: BootstrapOptions,
    runner,
    container: str,
) -> dict[str, Any]:
    inspected = _runner_docker_inspect(options, runner, container)
    state = inspected["State"]
    return {
        "id": inspected["Id"],
        "started_at": state["StartedAt"],
        "restart_count": inspected["RestartCount"],
        "running": bool(state["Running"]),
    }


def container_fingerprint(container: str) -> dict[str, Any]:
    inspected = docker_inspect(container)
    state = inspected["State"]
    return {
        "id": inspected["Id"],
        "started_at": state["StartedAt"],
        "restart_count": inspected["RestartCount"],
        "running": bool(state["Running"]),
    }


def validate_source_container(inspected: dict[str, Any]) -> None:
    host_config = inspected["HostConfig"]
    config = inspected["Config"]
    if inspected["Image"] != EXPECTED_IMAGE_ID:
        raise BootstrapError("source node image digest drifted")
    if config.get("User") not in {"nautilus", "999:999"}:
        raise BootstrapError("source node user drifted")
    if host_config.get("NetworkMode") != "host":
        raise BootstrapError("source node network mode drifted")
    if host_config.get("Memory") != EXPECTED_SOURCE_MEMORY:
        raise BootstrapError("source node memory limit drifted")
    if host_config.get("MemorySwap") != EXPECTED_SOURCE_MEMORY:
        raise BootstrapError("source node memory swap limit drifted")
    restart = host_config.get("RestartPolicy") or {}
    if restart.get("Name") != "unless-stopped":
        raise BootstrapError("source node restart policy drifted")
    mounts = inspected.get("Mounts") or []
    destinations = {
        str(mount.get("Destination") or "")
        for mount in mounts
        if isinstance(mount, dict)
    }
    required_destinations = {
        "/cfg.json",
        "/state",
        "/app/config/node_config.py",
        "/app/app/run_node.py",
        "/app/app/health_server.py",
        "/app/runtime/lifecycle.py",
    }
    missing = sorted(required_destinations - destinations)
    if missing:
        raise BootstrapError(
            "source node required mounts are missing: " + ", ".join(missing)
        )


def read_account_credentials() -> dict[str, tuple[str, str]]:
    watcher_stat = WATCHER_DB.stat()
    if stat.S_IMODE(watcher_stat.st_mode) != EXPECTED_WATCHER_MODE:
        raise BootstrapError("watcher database mode must be 0600")
    if watcher_stat.st_uid != 0 or watcher_stat.st_gid != 0:
        raise BootstrapError("watcher database must be root:root")
    connection = sqlite3.connect(
        f"file:{WATCHER_DB}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT account_id, api_key, api_secret, account_type, "
            "parent_account_id, execution_account_id, "
            "risk_capital_multiplier FROM account_configs "
            "WHERE execution_account_id IN ('account-c','account-d') "
            "ORDER BY execution_account_id"
        ).fetchall()
        parent = connection.execute(
            "SELECT account_id, account_type FROM account_configs "
            "WHERE account_id='jiataotx@gmail.com'"
        ).fetchone()
    finally:
        connection.close()
    if parent is None or str(parent["account_type"]).strip().lower() != "main":
        raise BootstrapError("subaccount parent main account is invalid")
    if len(rows) != 2:
        raise BootstrapError("C/D credentials must resolve to exactly two rows")
    credentials: dict[str, tuple[str, str]] = {}
    for row in rows:
        execution_account_id = str(row["execution_account_id"] or "").strip()
        expected = next(
            (
                spec
                for spec in SPECS
                if spec.account_id == execution_account_id
            ),
            None,
        )
        if expected is None:
            raise BootstrapError("unexpected execution account in watcher database")
        if str(row["account_id"] or "").strip() != expected.credential_account_id:
            raise BootstrapError(f"{execution_account_id} credential identity drifted")
        if str(row["account_type"] or "").strip().lower() != "subaccount":
            raise BootstrapError(f"{execution_account_id} is not a subaccount")
        if str(row["parent_account_id"] or "").strip() != "jiataotx@gmail.com":
            raise BootstrapError(f"{execution_account_id} parent drifted")
        try:
            multiplier = float(row["risk_capital_multiplier"])
        except (TypeError, ValueError) as exc:
            raise BootstrapError(
                f"{execution_account_id} multiplier is invalid"
            ) from exc
        if multiplier <= 0:
            raise BootstrapError(f"{execution_account_id} multiplier is invalid")
        key = str(row["api_key"] or "").strip()
        secret = str(row["api_secret"] or "").strip()
        if not key or not secret:
            raise BootstrapError(f"{execution_account_id} credentials are missing")
        credentials[execution_account_id] = (key, secret)
    return credentials


def _stat_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _validate_watcher_db_file(path: Path) -> None:
    mode = _stat_mode(path)
    if mode & 0o022:
        raise BootstrapError("watcher database must not be group/world writable")


def _read_target_credentials(
    options: BootstrapOptions,
) -> dict[str, dict[str, Any]]:
    _validate_watcher_db_file(options.watcher_db)
    connection = sqlite3.connect(
        f"file:{options.watcher_db}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT account_id, api_key, api_secret, is_testnet, "
            "account_type, parent_account_id, execution_account_id, "
            "risk_capital_multiplier, is_enabled "
            "FROM account_configs "
            "ORDER BY execution_account_id"
        ).fetchall()
    finally:
        connection.close()
    by_execution: dict[str, sqlite3.Row] = {}
    by_account: dict[str, sqlite3.Row] = {}
    for row in rows:
        by_account[str(row["account_id"] or "").strip()] = row
        execution_account_id = str(row["execution_account_id"] or "").strip()
        if execution_account_id:
            if execution_account_id in by_execution:
                raise BootstrapError(
                    f"{execution_account_id} execution identity is duplicated"
                )
            by_execution[execution_account_id] = row
    credentials: dict[str, dict[str, Any]] = {}
    for target in options.targets:
        row = by_execution.get(target.execution_account_id)
        if row is None:
            raise BootstrapError(
                f"{target.execution_account_id} credentials are missing"
            )
        account_id = str(row["account_id"] or "").strip()
        if account_id != target.credential_account_id:
            raise BootstrapError(
                f"{target.execution_account_id} credential identity drifted"
            )
        if int(row["is_testnet"]) != 0:
            raise BootstrapError(
                f"{target.execution_account_id} must use live credentials"
            )
        if int(row["is_enabled"]) != 1:
            raise BootstrapError(
                f"{target.execution_account_id} account is disabled"
            )
        if str(row["account_type"] or "").strip().lower() != "subaccount":
            raise BootstrapError(
                f"{target.execution_account_id} is not a subaccount"
            )
        parent_id = str(row["parent_account_id"] or "").strip()
        parent = by_account.get(parent_id)
        if parent is None:
            raise BootstrapError(
                f"{target.execution_account_id} parent is missing"
            )
        if str(parent["account_type"] or "").strip().lower() != "main":
            raise BootstrapError(
                f"{target.execution_account_id} parent is not main"
            )
        try:
            multiplier = float(row["risk_capital_multiplier"])
        except (TypeError, ValueError) as exc:
            raise BootstrapError(
                f"{target.execution_account_id} multiplier is invalid"
            ) from exc
        if multiplier <= 0:
            raise BootstrapError(
                f"{target.execution_account_id} multiplier is invalid"
            )
        api_key = str(row["api_key"] or "").strip()
        api_secret = str(row["api_secret"] or "").strip()
        if not api_key or not api_secret:
            raise BootstrapError(
                f"{target.execution_account_id} credentials are missing"
            )
        credentials[target.execution_account_id] = {
            "api_key": api_key,
            "api_secret": api_secret,
            "risk_capital_multiplier": multiplier,
        }
    return credentials


def _read_auth_bindings(options: BootstrapOptions) -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for env_file in options.control_plane_env_files:
        _lines, values = parse_environment_file(env_file)
        raw = values.get("NAUTILUS_NODE_AUTH_JSON", "")
        if not raw:
            continue
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BootstrapError("NAUTILUS_NODE_AUTH_JSON is invalid") from exc
        if not isinstance(decoded, dict):
            raise BootstrapError("NAUTILUS_NODE_AUTH_JSON must be an object")
        for node_id, binding in decoded.items():
            if not isinstance(binding, dict):
                raise BootstrapError(f"node binding is invalid: {node_id}")
            account_id = str(binding.get("account_id") or "").strip()
            token = str(binding.get("token") or "").strip()
            if not account_id or not token:
                raise BootstrapError(f"node binding is incomplete: {node_id}")
            merged[str(node_id)] = {
                "account_id": account_id,
                "token": token,
            }
    for node_id, account_id in (
        ("nautilus-node-account-a", "account-a"),
        ("nautilus-node-account-b", "account-b"),
    ):
        binding = merged.get(node_id)
        if binding is None:
            raise BootstrapError(f"existing binding is missing: {node_id}")
        if binding["account_id"] != account_id:
            raise BootstrapError(f"existing binding drifted: {node_id}")
    return merged


def _source_config_from_inspect(
    options: BootstrapOptions,
    inspected: dict[str, Any],
) -> dict[str, Any]:
    for mount in inspected.get("Mounts") or []:
        if not isinstance(mount, dict):
            continue
        if mount.get("Destination") != "/cfg.json":
            continue
        source = Path(str(mount.get("Source") or ""))
        if source.is_file():
            return json.loads(source.read_text(encoding="utf-8"))
    fallback = options.paths.trader_root / "node-b.hk.json"
    if fallback.is_file():
        return json.loads(fallback.read_text(encoding="utf-8"))
    return {}


def _stable_public_inspect(inspected: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": inspected.get("Id"),
        "image": inspected.get("Image"),
        "restart_count": inspected.get("RestartCount"),
        "started_at": (inspected.get("State") or {}).get("StartedAt"),
        "host_config": inspected.get("HostConfig") or {},
        "config": {
            "image": (inspected.get("Config") or {}).get("Image"),
            "entrypoint": (inspected.get("Config") or {}).get("Entrypoint"),
            "cmd": (inspected.get("Config") or {}).get("Cmd"),
            "user": (inspected.get("Config") or {}).get("User"),
        },
        "mounts": [
            {
                "source": mount.get("Source"),
                "destination": mount.get("Destination"),
                "rw": mount.get("RW"),
            }
            for mount in inspected.get("Mounts") or []
            if isinstance(mount, dict)
        ],
    }


def _ab_pin(inspections: dict[str, dict[str, Any]]) -> str:
    payload = {
        name: _stable_public_inspect(inspections[name])
        for name in sorted(inspections)
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_ab_inspections(
    options: BootstrapOptions,
    runner,
) -> dict[str, dict[str, Any]]:
    return {
        "trader-v3-node-a": _runner_docker_inspect(
            options,
            runner,
            "trader-v3-node-a",
        ),
        "trader-v3-node-b": _runner_docker_inspect(
            options,
            runner,
            options.template_container,
        ),
    }


def _validate_template_for_injected_run(inspected: dict[str, Any]) -> None:
    host_config = inspected.get("HostConfig") or {}
    if host_config.get("NetworkMode") != "host":
        raise BootstrapError("template node network mode drifted")
    restart = host_config.get("RestartPolicy") or {}
    if restart.get("Name") != "unless-stopped":
        raise BootstrapError("template node restart policy drifted")
    if not inherited_mounts(inspected):
        raise BootstrapError("template node reviewed patch mounts are missing")


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_value(child)
            for key, child in value.items()
            if not re.search(r"(api[_-]?key|api[_-]?secret|token)", str(key), re.I)
        }
    if isinstance(value, list):
        return [_sanitize_value(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_value(child) for child in value)
    return value


def parse_options(argv: list[str] | None = None) -> BootstrapOptions:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("check", "apply"),
        default="check",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--watcher-db", type=Path, default=WATCHER_DB)
    parser.add_argument(
        "--risk-policy",
        type=Path,
        default=Path("services/nautilus-node/config/live-risk-policy.json"),
    )
    parser.add_argument(
        "--control-plane-env-file",
        action="append",
        type=Path,
        default=[],
    )
    parser.add_argument("--trader-root", type=Path, default=TRADER_ROOT)
    parser.add_argument(
        "--secret-dir",
        type=Path,
        default=TRADER_ROOT / "secrets" / "subaccount-nodes",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=TRADER_ROOT / "bootstrap" / "subaccount-nodes",
    )
    parser.add_argument(
        "--template-container",
        default=SOURCE_CONTAINER,
    )
    parser.add_argument("--docker-binary", default="docker")
    parser.add_argument("--expected-ab-pin-sha256", default="")
    parsed = parser.parse_args(argv)
    mode = parsed.mode
    if parsed.apply:
        mode = "apply"
    env_files = tuple(parsed.control_plane_env_file or [ENV_FILE])
    targets = (
        AccountTarget(
            suffix="c",
            credential_account_id="泰山",
            execution_account_id="account-c",
            channel_name="坚果",
            health_port=8083,
        ),
        AccountTarget(
            suffix="d",
            credential_account_id="黄山",
            execution_account_id="account-d",
            channel_name="03Cash",
            health_port=8084,
        ),
    )
    return BootstrapOptions(
        mode=mode,
        watcher_db=parsed.watcher_db,
        risk_policy=parsed.risk_policy,
        control_plane_env_files=env_files,
        paths=BootstrapPaths(
            trader_root=parsed.trader_root,
            secret_dir=parsed.secret_dir,
            artifact_dir=parsed.artifact_dir,
        ),
        template_container=parsed.template_container,
        docker_binary=parsed.docker_binary,
        expected_ab_pin_sha256=parsed.expected_ab_pin_sha256,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
        node_uid=999,
        node_gid=999,
        targets=targets,
    )


def _write_private_file(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    atomic_write(path, payload, mode=mode, uid=uid, gid=gid)


def _write_json_private(
    path: Path,
    payload: dict[str, Any],
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    body = (
        json.dumps(
            payload,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    _write_private_file(path, body, mode=mode, uid=uid, gid=gid)


def _check_injected_bootstrap(
    options: BootstrapOptions,
    runner,
) -> dict[str, Any]:
    _runner_call(runner, [options.docker_binary, "version"])
    inspections = _read_ab_inspections(options, runner)
    source = inspections["trader-v3-node-b"]
    _validate_template_for_injected_run(source)
    credentials = _read_target_credentials(options)
    source_config = _source_config_from_inspect(options, source)
    live_configs = {
        target.execution_account_id: _config_for_target(
            options,
            source_config,
            target,
        )
        for target in options.targets
    }
    docker_run_specs = {
        target.execution_account_id: {
            "argv": _docker_run_argv_for_target(options, source, target)
        }
        for target in options.targets
    }
    return {
        "status": "check_passed",
        "writes_performed": False,
        "ab_pin_sha256": _ab_pin(inspections),
        "docker_run_specs": docker_run_specs,
        "live_configs": live_configs,
        "_credentials": credentials,
        "_bindings": _read_auth_bindings(options),
        "_inspections": inspections,
    }


def _write_apply_artifacts(
    options: BootstrapOptions,
    checked: dict[str, Any],
    *,
    token_factory,
) -> dict[str, Any]:
    credentials = checked["_credentials"]
    bindings = copy.deepcopy(checked["_bindings"])
    options.paths.secret_dir.mkdir(parents=True, mode=0o750, exist_ok=True)
    os.chmod(options.paths.secret_dir, 0o750)
    os.chown(options.paths.secret_dir, options.owner_uid, options.owner_gid)
    options.paths.artifact_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(options.paths.artifact_dir, 0o700)
    os.chown(options.paths.artifact_dir, options.owner_uid, options.owner_gid)
    secret_file_count = 0
    for target in options.targets:
        account_credentials = credentials[target.execution_account_id]
        token = token_factory()
        for path, value in (
            (
                options.paths.secret_path(target, "binance_api_key"),
                account_credentials["api_key"],
            ),
            (
                options.paths.secret_path(target, "binance_api_secret"),
                account_credentials["api_secret"],
            ),
            (
                options.paths.secret_path(target, "control_plane_token"),
                token,
            ),
        ):
            _write_private_file(
                path,
                (str(value) + "\n").encode("utf-8"),
                mode=EXPECTED_SECRET_MODE,
                uid=options.owner_uid,
                gid=options.owner_gid,
            )
            secret_file_count += 1
        config = checked["live_configs"][target.execution_account_id]
        _write_json_private(
            options.paths.config_path(target),
            config,
            mode=EXPECTED_CONFIG_MODE,
            uid=options.owner_uid,
            gid=options.owner_gid,
        )
        options.paths.state_path(target).mkdir(parents=True, exist_ok=True)
        os.chmod(options.paths.state_path(target), 0o750)
        os.chown(options.paths.state_path(target), options.owner_uid, options.owner_gid)
        bindings[target.node_id] = {
            "account_id": target.execution_account_id,
            "token": token,
        }
    tokens = [
        binding["token"]
        for binding in bindings.values()
    ]
    if len(tokens) != len(set(tokens)):
        raise BootstrapError("node auth tokens must be unique")
    _write_json_private(
        options.paths.auth_proposal_path,
        {"bindings": bindings},
        mode=0o400,
        uid=options.owner_uid,
        gid=options.owner_gid,
    )
    manifest = {
        "schema_version": "trader-v3-subaccount-bootstrap/v1",
        "ab_pin_sha256": checked["ab_pin_sha256"],
        "targets": [
            {
                "account_id": target.execution_account_id,
                "container": target.container,
                "config_path": str(options.paths.config_path(target)),
                "state_path": str(options.paths.state_path(target)),
                "secret_files": [
                    str(options.paths.secret_path(target, "binance_api_key")),
                    str(options.paths.secret_path(target, "binance_api_secret")),
                    str(options.paths.secret_path(target, "control_plane_token")),
                ],
            }
            for target in options.targets
        ],
        "docker_run_executed": False,
    }
    _write_json_private(
        options.paths.manifest_path,
        _sanitize_value(manifest),
        mode=0o400,
        uid=options.owner_uid,
        gid=options.owner_gid,
    )
    rollback = {
        "schema_version": "trader-v3-subaccount-bootstrap-rollback/v1",
        "forbidden_targets": [
            "trader-v3-node-a",
            "trader-v3-node-b",
        ],
        "actions": [
            {
                "action": "docker_remove_if_created_from_reviewed_spec",
                "container": target.container,
            }
            for target in options.targets
        ],
    }
    _write_json_private(
        options.paths.rollback_path,
        rollback,
        mode=0o400,
        uid=options.owner_uid,
        gid=options.owner_gid,
    )
    return {
        "status": "prepared",
        "writes_performed": True,
        "docker_run_executed": False,
        "secret_file_count": secret_file_count,
        "ab_pin_sha256": checked["ab_pin_sha256"],
    }


def run_bootstrap(
    options: BootstrapOptions,
    *,
    runner=None,
    token_factory=None,
) -> dict[str, Any]:
    if options.mode not in {"check", "apply"}:
        raise BootstrapError(f"unsupported mode: {options.mode}")
    checked = _check_injected_bootstrap(options, runner)
    if options.mode == "check":
        return public_result(checked)
    expected_pin = str(options.expected_ab_pin_sha256 or "").strip()
    if not expected_pin:
        raise BootstrapError("expected-ab-pin-sha256 is required for apply")
    if expected_pin != checked["ab_pin_sha256"]:
        raise BootstrapError("expected-ab-pin-sha256 does not match reviewed A/B pin")
    second = _read_ab_inspections(options, runner)
    if _ab_pin(second) != expected_pin:
        raise BootstrapError("A/B pins drifted before apply")
    if token_factory is None:
        token_factory = lambda: secrets.token_urlsafe(48)
    return _write_apply_artifacts(
        options,
        checked,
        token_factory=token_factory,
    )


def parse_environment_file(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        normalized = value.strip()
        if (
            len(normalized) >= 2
            and normalized[0] == normalized[-1]
            and normalized[0] in {"'", '"'}
        ):
            normalized = normalized[1:-1]
        values[key.strip()] = normalized
    return lines, values


def render_environment_file(
    lines: list[str],
    key: str,
    value: str,
) -> bytes:
    replacement = f"{key}='{value}'"
    rendered: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(key + "="):
            if replaced:
                raise BootstrapError(f"{key} occurs more than once")
            rendered.append(replacement)
            replaced = True
        else:
            rendered.append(line)
    if not replaced:
        rendered.append(replacement)
    return ("\n".join(rendered) + "\n").encode("utf-8")


def atomic_write(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.chown(temp_path, uid, gid)
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def build_node_config(
    source: dict[str, Any],
    spec: AccountSpec,
) -> dict[str, Any]:
    config = copy.deepcopy(source)
    config["account_id"] = spec.account_id
    config["node_id"] = spec.node_id
    config["trader_id"] = spec.trader_id
    config["instance_id"] = spec.instance_id
    config["redis"] = {
        "url": "redis://127.0.0.1:6379/0",
        "key_prefix": spec.redis_prefix,
    }
    config["risk"] = {
        "max_notional_per_order": dict(RISK_CAPS),
        "max_order_submit_rate": "50/00:00:01",
        "max_order_modify_rate": "1/00:00:01",
    }
    config["runtime_resources"] = copy.deepcopy(RUNTIME_RESOURCES)
    config["binance"] = {
        "account_type": "USDT-M",
        "environment": "live",
        "api_key": {
            "env": spec.key_env,
            "file": f"/run/secrets/{spec.api_key_path.name}",
        },
        "api_secret": {
            "env": spec.secret_env,
            "file": f"/run/secrets/{spec.api_secret_path.name}",
        },
    }
    config["control_plane"] = {
        "base_url": "http://127.0.0.1:8080",
        "token": {
            "env": spec.token_env,
            "file": f"/run/secrets/{spec.token_path.name}",
        },
        "heartbeat_timeout_seconds": 15,
        "snapshot_stale_after_seconds": 30,
    }
    return config


def _target_spec(
    options: BootstrapOptions,
    target: AccountTarget,
) -> AccountSpec:
    return AccountSpec(
        account_id=target.execution_account_id,
        credential_account_id=target.credential_account_id,
        node_id=target.node_id,
        trader_id=target.trader_id,
        instance_id=target.instance_id,
        container=target.container,
        suffix=target.suffix,
        health_port=target.health_port,
        redis_prefix=target.redis_prefix,
        key_env=target.key_env,
        secret_env=target.secret_env,
        token_env=target.token_env,
    )


def _config_for_target(
    options: BootstrapOptions,
    source_config: dict[str, Any],
    target: AccountTarget,
) -> dict[str, Any]:
    spec = _target_spec(options, target)
    config = build_node_config(source_config, spec)
    config["binance"]["api_key"]["file"] = (
        f"/run/secrets/{options.paths.secret_path(target, 'binance_api_key').name}"
    )
    config["binance"]["api_secret"]["file"] = (
        f"/run/secrets/{options.paths.secret_path(target, 'binance_api_secret').name}"
    )
    config["control_plane"]["token"]["file"] = (
        f"/run/secrets/{options.paths.secret_path(target, 'control_plane_token').name}"
    )
    return config


def _safe_environment_for_target(
    inspected: dict[str, Any],
    target: AccountTarget,
) -> list[str]:
    return safe_environment(inspected, _target_spec_for_compat(target))


def _target_spec_for_compat(target: AccountTarget) -> AccountSpec:
    return AccountSpec(
        account_id=target.execution_account_id,
        credential_account_id=target.credential_account_id,
        node_id=target.node_id,
        trader_id=target.trader_id,
        instance_id=target.instance_id,
        container=target.container,
        suffix=target.suffix,
        health_port=target.health_port,
        redis_prefix=target.redis_prefix,
        key_env=target.key_env,
        secret_env=target.secret_env,
        token_env=target.token_env,
    )


def _docker_run_argv_for_target(
    options: BootstrapOptions,
    source: dict[str, Any],
    target: AccountTarget,
) -> list[str]:
    spec = _target_spec_for_compat(target)
    command = [
        options.docker_binary,
        "run",
        "-d",
        "--name",
        target.container,
        "--restart=unless-stopped",
        "--network=host",
        "--user=999:999",
        "--workdir=/app",
        f"--memory={EXPECTED_SOURCE_MEMORY}",
        f"--memory-swap={EXPECTED_SOURCE_MEMORY}",
    ]
    for value in safe_environment(source, spec):
        command.extend(["-e", value])
    for mount_source, destination, mode in inherited_mounts(source):
        command.extend(["-v", f"{mount_source}:{destination}:{mode}"])
    command.extend(
        [
            "-v",
            f"{options.paths.config_path(target)}:/cfg.json:ro",
            "-v",
            f"{options.paths.state_path(target)}:/state:rw",
            "-v",
            (
                f"{options.paths.secret_path(target, 'binance_api_key')}:"
                f"/run/secrets/{options.paths.secret_path(target, 'binance_api_key').name}:ro"
            ),
            "-v",
            (
                f"{options.paths.secret_path(target, 'binance_api_secret')}:"
                f"/run/secrets/{options.paths.secret_path(target, 'binance_api_secret').name}:ro"
            ),
            "-v",
            (
                f"{options.paths.secret_path(target, 'control_plane_token')}:"
                f"/run/secrets/{options.paths.secret_path(target, 'control_plane_token').name}:ro"
            ),
            "--entrypoint",
            source["Config"]["Entrypoint"][0],
            source["Image"],
        ]
    )
    command.extend(source["Config"].get("Cmd") or [])
    return command


def safe_environment(inspected: dict[str, Any], spec: AccountSpec) -> list[str]:
    output: list[str] = []
    for item in inspected["Config"].get("Env") or []:
        if not isinstance(item, str) or "=" not in item:
            continue
        name, value = item.split("=", 1)
        if name in {
            "NAUTILUS_HEALTH_PORT",
            "NODE_STATE_DIR",
            "NAUTILUS_INITIAL_TRADING_STATE",
            "NODE_CONFIG_PATH",
        }:
            continue
        if any(fragment in name for fragment in ("API_KEY", "API_SECRET", "TOKEN")):
            continue
        output.append(f"{name}={value}")
    output.extend(
        [
            f"NAUTILUS_HEALTH_PORT={spec.health_port}",
            "NODE_STATE_DIR=/state",
            "NAUTILUS_INITIAL_TRADING_STATE=HALTED",
            "NODE_CONFIG_PATH=/cfg.json",
            f"{spec.key_env}=",
            f"{spec.secret_env}=",
            f"{spec.token_env}=",
        ]
    )
    return output


def inherited_mounts(
    inspected: dict[str, Any],
) -> list[tuple[str, str, str]]:
    mounts: list[tuple[str, str, str]] = []
    seen_destinations: set[str] = set()
    for mount in inspected.get("Mounts") or []:
        source = str(mount.get("Source") or "")
        destination = str(mount.get("Destination") or "")
        if destination in {"/cfg.json", "/state"}:
            continue
        if not source or not destination:
            raise BootstrapError("source node has an invalid mount")
        if mount.get("RW") is not False:
            raise BootstrapError(
                f"source patch mount must be read-only: {destination}"
            )
        if destination in seen_destinations:
            raise BootstrapError(
                f"source mount destination is duplicated: {destination}"
            )
        seen_destinations.add(destination)
        mounts.append((source, destination, "ro"))
    return mounts


def docker_run_command(
    source: dict[str, Any],
    spec: AccountSpec,
) -> list[str]:
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        spec.container,
        "--restart=unless-stopped",
        "--network=host",
        "--user=999:999",
        "--workdir=/app",
        f"--memory={EXPECTED_SOURCE_MEMORY}",
        f"--memory-swap={EXPECTED_SOURCE_MEMORY}",
    ]
    for value in safe_environment(source, spec):
        command.extend(["-e", value])
    for mount_source, destination, mode in inherited_mounts(source):
        command.extend(
            ["-v", f"{mount_source}:{destination}:{mode}"]
        )
    command.extend(
        [
            "-v",
            f"{spec.config_path}:/cfg.json:ro",
            "-v",
            f"{spec.state_path}:/state:rw",
            "-v",
            (
                f"{spec.api_key_path}:"
                f"/run/secrets/{spec.api_key_path.name}:ro"
            ),
            "-v",
            (
                f"{spec.api_secret_path}:"
                f"/run/secrets/{spec.api_secret_path.name}:ro"
            ),
            "-v",
            (
                f"{spec.token_path}:"
                f"/run/secrets/{spec.token_path.name}:ro"
            ),
            "--entrypoint",
            source["Config"]["Entrypoint"][0],
            source["Image"],
        ]
    )
    command.extend(source["Config"].get("Cmd") or [])
    return command


def validate_config_in_container(
    source: dict[str, Any],
    spec: AccountSpec,
) -> None:
    command = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--user=999:999",
        "--workdir=/app",
        "-e",
        f"{spec.key_env}=",
        "-e",
        f"{spec.secret_env}=",
        "-e",
        f"{spec.token_env}=",
    ]
    for mount_source, destination, mode in inherited_mounts(source):
        command.extend(
            ["-v", f"{mount_source}:{destination}:{mode}"]
        )
    command.extend(
        [
            "-v",
            f"{spec.config_path}:/cfg.json:ro",
            "-v",
            (
                f"{spec.api_key_path}:"
                f"/run/secrets/{spec.api_key_path.name}:ro"
            ),
            "-v",
            (
                f"{spec.api_secret_path}:"
                f"/run/secrets/{spec.api_secret_path.name}:ro"
            ),
            "-v",
            (
                f"{spec.token_path}:"
                f"/run/secrets/{spec.token_path.name}:ro"
            ),
            "--entrypoint",
            "/usr/local/bin/python",
            source["Image"],
            "-c",
            (
                "from config.node_config import load_node_config;"
                "c=load_node_config('/cfg.json');"
                f"assert c.account_id == '{spec.account_id}';"
                f"assert c.node_id == '{spec.node_id}';"
                "assert c.binance.environment == 'live';"
                "print('config-ok')"
            ),
        ]
    )
    result = run(command)
    if result.stdout.strip() != "config-ok":
        raise BootstrapError(f"{spec.account_id} config validation failed")


def http_json(url: str, timeout: float = 3) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise BootstrapError(f"invalid JSON response from {url}")
    return payload


def wait_control_plane() -> None:
    deadline = time.monotonic() + CONTROL_PLANE_READY_TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        try:
            payload = http_json(
                "http://127.0.0.1:8080/openapi.json",
            )
            if isinstance(payload.get("paths"), dict):
                return
        except Exception as exc:  # noqa: BLE001 - bounded readiness polling
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    raise BootstrapError("control plane did not recover: " + last_error)


def wait_node(spec: AccountSpec) -> dict[str, Any]:
    deadline = time.monotonic() + NODE_READY_TIMEOUT_SECONDS
    last_error = ""
    url = f"http://127.0.0.1:{spec.health_port}/ready"
    while time.monotonic() < deadline:
        try:
            payload = http_json(url)
            if (
                payload.get("ready") is True
                and str(payload.get("trading_state") or "").upper()
                == "HALTED"
            ):
                return payload
            last_error = json.dumps(
                {
                    "ready": payload.get("ready"),
                    "trading_state": payload.get("trading_state"),
                    "halt_reason": payload.get("halt_reason"),
                },
                sort_keys=True,
            )
        except Exception as exc:  # noqa: BLE001 - bounded readiness polling
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    raise BootstrapError(f"{spec.account_id} did not become HALTED: {last_error}")


def verify_runtime(spec: AccountSpec) -> None:
    inspected = docker_inspect(spec.container)
    if inspected["Image"] != EXPECTED_IMAGE_ID:
        raise BootstrapError(f"{spec.account_id} image mismatch")
    if inspected["Config"].get("User") != "999:999":
        raise BootstrapError(f"{spec.account_id} user mismatch")
    if inspected["HostConfig"].get("NetworkMode") != "host":
        raise BootstrapError(f"{spec.account_id} network mismatch")
    if inspected["RestartCount"] != 0:
        raise BootstrapError(f"{spec.account_id} restarted during bootstrap")
    destinations = {
        str(mount.get("Destination") or ""): mount
        for mount in inspected.get("Mounts") or []
        if isinstance(mount, dict)
    }
    for path in (
        f"/run/secrets/{spec.api_key_path.name}",
        f"/run/secrets/{spec.api_secret_path.name}",
        f"/run/secrets/{spec.token_path.name}",
    ):
        mount = destinations.get(path)
        if not isinstance(mount, dict) or mount.get("RW") is not False:
            raise BootstrapError(f"{spec.account_id} secret mount is invalid")


def write_secret(path: Path, value: str) -> None:
    if path.exists():
        path_stat = path.stat()
        if (
            path.read_text(encoding="utf-8").strip() == value
            and path_stat.st_uid == 0
            and path_stat.st_gid == 999
            and stat.S_IMODE(path_stat.st_mode) == EXPECTED_SECRET_MODE
        ):
            return
        raise BootstrapError(f"existing secret file drifted: {path}")
    atomic_write(
        path,
        (value + "\n").encode("utf-8"),
        mode=EXPECTED_SECRET_MODE,
        uid=0,
        gid=999,
    )


def write_config(path: Path, config: dict[str, Any]) -> None:
    payload = (
        json.dumps(
            config,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if path.exists() and path.read_bytes() == payload:
        path_stat = path.stat()
        if (
            path_stat.st_uid == 0
            and path_stat.st_gid == 999
            and stat.S_IMODE(path_stat.st_mode) == EXPECTED_CONFIG_MODE
        ):
            return
    atomic_write(
        path,
        payload,
        mode=EXPECTED_CONFIG_MODE,
        uid=0,
        gid=999,
    )


def backup_file(path: Path, label: str) -> Path:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = BACKUP_ROOT / f"{label}.{timestamp}.bak"
    shutil.copy2(path, target)
    os.chmod(target, 0o600)
    return target


def apply_bootstrap(
    source: dict[str, Any],
    credentials: dict[str, tuple[str, str]],
    before_fingerprints: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if any(container_exists(spec.container) for spec in SPECS):
        raise BootstrapError("C/D containers must both be absent before bootstrap")
    env_lines, env_values = parse_environment_file(ENV_FILE)
    raw_bindings = env_values.get("NAUTILUS_NODE_AUTH_JSON", "")
    try:
        bindings = json.loads(raw_bindings)
    except json.JSONDecodeError as exc:
        raise BootstrapError("NAUTILUS_NODE_AUTH_JSON is invalid") from exc
    if not isinstance(bindings, dict):
        raise BootstrapError("NAUTILUS_NODE_AUTH_JSON must be an object")
    for node_id, account_id in (
        ("nautilus-node-account-a", "account-a"),
        ("nautilus-node-account-b", "account-b"),
    ):
        value = bindings.get(node_id)
        if not isinstance(value, dict):
            raise BootstrapError(f"existing binding is missing: {node_id}")
        if str(value.get("account_id") or "") != account_id:
            raise BootstrapError(f"existing binding drifted: {node_id}")
        if not str(value.get("token") or ""):
            raise BootstrapError(f"existing binding token is missing: {node_id}")
    env_backup = backup_file(ENV_FILE, "env.v3.before-subaccount-bootstrap")
    created_containers: list[str] = []
    created_paths: list[Path] = []
    try:
        SECRET_ROOT.mkdir(parents=True, exist_ok=True)
        os.chown(SECRET_ROOT, 0, 999)
        os.chmod(SECRET_ROOT, 0o750)
        for spec in SPECS:
            key, secret = credentials[spec.account_id]
            token = secrets.token_urlsafe(48)
            for path, value in (
                (spec.api_key_path, key),
                (spec.api_secret_path, secret),
                (spec.token_path, token),
            ):
                existed = path.exists()
                write_secret(path, value)
                if not existed:
                    created_paths.append(path)
            bindings[spec.node_id] = {
                "account_id": spec.account_id,
                "token": token,
            }
            state_existed = spec.state_path.exists()
            spec.state_path.mkdir(parents=True, exist_ok=True)
            os.chown(spec.state_path, 999, 999)
            os.chmod(spec.state_path, 0o750)
            if not state_existed:
                created_paths.append(spec.state_path)
            config = build_node_config(
                json.loads(SOURCE_CONFIG.read_text(encoding="utf-8")),
                spec,
            )
            config_existed = spec.config_path.exists()
            write_config(spec.config_path, config)
            if not config_existed:
                created_paths.append(spec.config_path)

        tokens = [
            str(value.get("token") or "")
            for value in bindings.values()
            if isinstance(value, dict)
        ]
        if len(tokens) != len(set(tokens)):
            raise BootstrapError("node auth tokens must be unique")
        binding_json = json.dumps(
            bindings,
            separators=(",", ":"),
            sort_keys=True,
        )
        env_payload = render_environment_file(
            env_lines,
            "NAUTILUS_NODE_AUTH_JSON",
            binding_json,
        )
        env_stat = ENV_FILE.stat()
        atomic_write(
            ENV_FILE,
            env_payload,
            mode=stat.S_IMODE(env_stat.st_mode),
            uid=env_stat.st_uid,
            gid=env_stat.st_gid,
        )
        for spec in SPECS:
            validate_config_in_container(source, spec)

        run(["systemctl", "restart", "trader-v3-controlplane.service"])
        wait_control_plane()

        readiness: dict[str, dict[str, Any]] = {}
        for spec in SPECS:
            command = docker_run_command(source, spec)
            result = run(command)
            if not result.stdout.strip():
                raise BootstrapError(
                    f"docker run returned no id for {spec.account_id}"
                )
            created_containers.append(spec.container)
            readiness[spec.account_id] = wait_node(spec)
            verify_runtime(spec)

        after_fingerprints = {
            name: container_fingerprint(name)
            for name in before_fingerprints
        }
        if after_fingerprints != before_fingerprints:
            raise BootstrapError("A/B container identity changed during bootstrap")
        return {
            "status": "applied",
            "env_backup": str(env_backup),
            "created": [spec.container for spec in SPECS],
            "readiness": {
                account_id: {
                    "ready": value.get("ready"),
                    "trading_state": value.get("trading_state"),
                    "halt_reason": value.get("halt_reason"),
                }
                for account_id, value in readiness.items()
            },
            "a_b_unchanged": True,
        }
    except Exception:
        for container in reversed(created_containers):
            run(["docker", "rm", "-f", container], check=False)
        shutil.copy2(env_backup, ENV_FILE)
        run(
            ["systemctl", "restart", "trader-v3-controlplane.service"],
            check=False,
        )
        for path in reversed(created_paths):
            try:
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()
            except OSError:
                pass
        raise


def check_bootstrap() -> dict[str, Any]:
    if os.geteuid() != 0:
        raise BootstrapError("run as root on the HK host")
    source = docker_inspect(SOURCE_CONTAINER)
    validate_source_container(source)
    before_fingerprints = {
        name: container_fingerprint(name)
        for name in ("trader-v3-node-a", "trader-v3-node-b")
    }
    if not all(value["running"] for value in before_fingerprints.values()):
        raise BootstrapError("A/B source nodes must be running")
    credentials = read_account_credentials()
    source_config = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    for spec in SPECS:
        config = build_node_config(source_config, spec)
        if config["account_id"] != spec.account_id:
            raise BootstrapError(f"{spec.account_id} config generation failed")
    return {
        "status": "ready",
        "image_id": source["Image"],
        "source_container": SOURCE_CONTAINER,
        "accounts": [
            {
                "account_id": spec.account_id,
                "container": spec.container,
                "health_port": spec.health_port,
                "redis_prefix": spec.redis_prefix,
                "container_exists": container_exists(spec.container),
                "credentials_present": spec.account_id in credentials,
            }
            for spec in SPECS
        ],
        "a_b_fingerprints": before_fingerprints,
        "_source": source,
        "_credentials": credentials,
    }


def public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if not key.startswith("_")
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        checked = check_bootstrap()
        if not args.apply:
            print(json.dumps(public_result(checked), sort_keys=True))
            return 0
        result = apply_bootstrap(
            checked["_source"],
            checked["_credentials"],
            checked["a_b_fingerprints"],
        )
    except BootstrapError as exc:
        print(
            json.dumps(
                {"status": "rejected", "error": str(exc)},
                sort_keys=True,
            )
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - sanitized top-level failure
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(public_result(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
