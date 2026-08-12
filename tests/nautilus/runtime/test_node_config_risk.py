from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from app.node import _build_risk_limit_config  # noqa: E402
from config.node_config import NodeConfigError, load_node_config  # noqa: E402


def test_risk_limits_and_rates_load_from_node_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(
        tmp_path,
        risk={
            "max_notional_per_order": {
                "BTCUSDT-PERP.BINANCE": "12",
                "ETHUSDT-PERP.BINANCE": "8.5",
            },
            "max_order_submit_rate": "4/00:00:01",
            "max_order_modify_rate": "2/00:00:01",
        },
    )
    _set_credentials(monkeypatch)

    config = load_node_config(path)
    limits = _build_risk_limit_config(config)

    assert config.risk
    assert config.risk.max_notional_per_order == {
        "BTCUSDT-PERP.BINANCE": "12",
        "ETHUSDT-PERP.BINANCE": "8.5",
    }
    assert limits.max_notional_per_order == config.risk.max_notional_per_order
    assert limits.max_order_submit_rate == "4/00:00:01"
    assert limits.max_order_modify_rate == "2/00:00:01"


def test_live_account_a_requires_risk_in_node_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(tmp_path)
    _set_credentials(monkeypatch)
    config = load_node_config(path)

    with pytest.raises(ValueError, match="risk configuration"):
        _build_risk_limit_config(config)


@pytest.mark.parametrize(
    "env_name",
    [
        "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON",
        "NAUTILUS_MAX_ORDER_SUBMIT_RATE",
        "NAUTILUS_MAX_ORDER_MODIFY_RATE",
    ],
)
def test_live_account_a_rejects_risk_env_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    path = _write_config(
        tmp_path,
        risk={
            "max_notional_per_order": {
                "BTCUSDT-PERP.BINANCE": "12",
            },
            "max_order_submit_rate": "4/00:00:01",
            "max_order_modify_rate": "2/00:00:01",
        },
    )
    _set_credentials(monkeypatch)
    monkeypatch.setenv(env_name, "configured-outside-release")
    config = load_node_config(path)

    with pytest.raises(ValueError, match="env overrides are forbidden"):
        _build_risk_limit_config(config)


def test_live_account_a_accepts_migrated_legacy_notional_100(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(
        tmp_path,
        risk={
            "max_notional_per_order": {
                "BTCUSDT-PERP.BINANCE": "12",
                "ETHUSDT-PERP.BINANCE": "100",
            },
            "max_order_submit_rate": "4/00:00:01",
            "max_order_modify_rate": "2/00:00:01",
        },
    )
    _set_credentials(monkeypatch)
    config = load_node_config(path)

    limits = _build_risk_limit_config(config)

    assert limits.max_notional_per_order == {
        "BTCUSDT-PERP.BINANCE": "12",
        "ETHUSDT-PERP.BINANCE": "100",
    }


def test_binance_proxy_resolves_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(
        tmp_path,
        proxy_url={"env": "BINANCE_PROXY_URL"},
    )
    _set_credentials(monkeypatch)
    monkeypatch.setenv(
        "BINANCE_PROXY_URL",
        "http://100.107.72.78:13128",
    )

    config = load_node_config(path)

    assert config.binance.proxy_url == "http://100.107.72.78:13128"
    assert config.binance.proxy_source == "env:BINANCE_PROXY_URL"


def test_binance_proxy_can_be_release_bound_in_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(
        tmp_path,
        proxy_url="http://100.107.72.78:13128",
    )
    _set_credentials(monkeypatch)

    config = load_node_config(path)

    assert config.binance.proxy_url == "http://100.107.72.78:13128"
    assert config.binance.proxy_source == "config"


def test_legacy_binance_config_without_proxy_remains_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(tmp_path)
    _set_credentials(monkeypatch)

    config = load_node_config(path)

    assert config.binance.proxy_url is False
    assert config.binance.proxy_source is False


def test_binance_proxy_rejects_non_http_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(
        tmp_path,
        proxy_url={"env": "BINANCE_PROXY_URL"},
    )
    _set_credentials(monkeypatch)
    monkeypatch.setenv("BINANCE_PROXY_URL", "file:///tmp/proxy")

    with pytest.raises(NodeConfigError, match="http"):
        load_node_config(path)


def test_binance_proxy_rejects_embedded_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(
        tmp_path,
        proxy_url="http://user:password@100.107.72.78:13128",
    )
    _set_credentials(monkeypatch)

    with pytest.raises(NodeConfigError, match="credentials"):
        load_node_config(path)


def test_control_plane_session_resilience_loads_from_node_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(
        tmp_path,
        control_plane_session={
            "command_delivery_capacity": 64,
            "command_ack_capacity": 128,
            "intent_delivery_capacity": 96,
            "execution_event_capacity": 512,
            "queue_degraded_ratio": 0.75,
            "retry_budget": 4,
            "retry_base_delay_seconds": 0.1,
            "retry_max_delay_seconds": 2.0,
            "retry_jitter_ratio": 0.25,
            "circuit_reset_seconds": 10.0,
            "operation_timeout_seconds": 8.0,
        },
    )
    _set_credentials(monkeypatch)

    config = load_node_config(path)
    session = config.control_plane.session

    assert session.command_delivery_capacity == 64
    assert session.command_ack_capacity == 128
    assert session.intent_delivery_capacity == 96
    assert session.execution_event_capacity == 512
    assert session.queue_degraded_ratio == 0.75
    assert session.retry_budget == 4
    assert session.retry_base_delay_seconds == 0.1
    assert session.retry_max_delay_seconds == 2.0
    assert session.retry_jitter_ratio == 0.25
    assert session.circuit_reset_seconds == 10.0
    assert session.operation_timeout_seconds == 8.0
    assert (
        config.runtime_resources.control_plane_session
        == config.control_plane.session
    )


def test_release_bound_runtime_groups_receive_deterministic_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(tmp_path)
    _set_credentials(monkeypatch)

    config = load_node_config(path)
    resources = config.runtime_resources

    assert resources.control_plane_session.command_delivery_capacity == 128
    assert resources.control_plane_session.shutdown_timeout_seconds == 1.0
    assert resources.strategy_durable_io.queue_capacity == 128
    assert resources.strategy_durable_io.task_timeout_seconds == 1.0
    assert resources.strategy_durable_io.shutdown_timeout_seconds == 2.0
    assert resources.terminal_exchange.queue_capacity == 64
    assert resources.terminal_exchange.result_queue_capacity == 128
    assert resources.terminal_exchange.total_deadline_seconds == 6.0
    assert resources.terminal_exchange.shutdown_timeout_seconds == 7.0
    assert resources.reporter_workers.denial_queue_capacity == 256
    assert (
        resources.reporter_workers.protection_event_queue_capacity
        == 1024
    )
    assert resources.reporter_workers.incident_queue_capacity == 64
    assert resources.reporter_workers.task_timeout_seconds == 5.0
    assert resources.reporter_workers.shutdown_timeout_seconds == 5.0


def test_duplicate_control_plane_session_contract_must_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = {
        "command_delivery_capacity": 64,
        "command_ack_capacity": 128,
        "intent_delivery_capacity": 96,
        "execution_event_capacity": 512,
        "queue_degraded_ratio": 0.75,
        "retry_budget": 4,
        "retry_base_delay_seconds": 0.1,
        "retry_max_delay_seconds": 2.0,
        "retry_jitter_ratio": 0.25,
        "circuit_reset_seconds": 10.0,
        "operation_timeout_seconds": 8.0,
    }
    runtime_resources = _runtime_resources()
    runtime_resources["control_plane_session"] = dict(session)
    path = _write_config(
        tmp_path,
        control_plane_session=session,
        runtime_resources=runtime_resources,
    )
    _set_credentials(monkeypatch)

    config = load_node_config(path)

    assert config.control_plane.session.command_delivery_capacity == 64


def test_duplicate_control_plane_session_contract_rejects_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_resources = _runtime_resources()
    runtime_resources["control_plane_session"] = {
        "command_delivery_capacity": 65,
    }
    path = _write_config(
        tmp_path,
        control_plane_session={
            "command_delivery_capacity": 64,
        },
        runtime_resources=runtime_resources,
    )
    _set_credentials(monkeypatch)

    with pytest.raises(
        NodeConfigError,
        match="differs from control_plane.session",
    ):
        load_node_config(path)


def test_live_node_requires_release_bound_runtime_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(
        tmp_path,
        include_runtime_resources=False,
    )
    _set_credentials(monkeypatch)

    with pytest.raises(
        NodeConfigError,
        match="runtime_resources must be release-bound",
    ):
        load_node_config(path)


@pytest.mark.parametrize(
    "field",
    [
        "memory_warning_ratio",
        "memory_degraded_ratio",
        "memory_critical_ratio",
    ],
)
def test_live_node_requires_release_bound_redis_memory_thresholds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    runtime_resources = _runtime_resources()
    redis_resources = runtime_resources["redis"]
    assert isinstance(redis_resources, dict)
    del redis_resources[field]
    path = _write_config(
        tmp_path,
        runtime_resources=runtime_resources,
    )
    _set_credentials(monkeypatch)

    with pytest.raises(NodeConfigError, match=field):
        load_node_config(path)


def test_testnet_legacy_runtime_resources_receive_memory_threshold_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_resources = _runtime_resources()
    redis_resources = runtime_resources["redis"]
    assert isinstance(redis_resources, dict)
    for field in (
        "memory_warning_ratio",
        "memory_degraded_ratio",
        "memory_critical_ratio",
    ):
        del redis_resources[field]
    path = _write_config(
        tmp_path,
        environment="testnet",
        runtime_resources=runtime_resources,
    )
    _set_credentials(monkeypatch)

    config = load_node_config(path)

    assert config.runtime_resources.redis.memory_warning_ratio == 0.60
    assert config.runtime_resources.redis.memory_degraded_ratio == 0.75
    assert config.runtime_resources.redis.memory_critical_ratio == 0.85


@pytest.mark.parametrize(
    "value",
    [True, "0.60", 0.0, 1.0, float("nan")],
)
def test_redis_memory_threshold_validation_is_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: object,
) -> None:
    runtime_resources = _runtime_resources()
    redis_resources = runtime_resources["redis"]
    assert isinstance(redis_resources, dict)
    redis_resources["memory_warning_ratio"] = value
    path = _write_config(
        tmp_path,
        runtime_resources=runtime_resources,
    )
    _set_credentials(monkeypatch)

    with pytest.raises(NodeConfigError, match="memory_warning_ratio"):
        load_node_config(path)


def _write_config(
    tmp_path: Path,
    *,
    risk: dict[str, object] | None = None,
    proxy_url: dict[str, str] | str | None = None,
    control_plane_session: dict[str, object] | None = None,
    environment: str = "live",
    runtime_resources: dict[str, object] | None = None,
    include_runtime_resources: bool = True,
) -> Path:
    raw = json.loads(
        (
            SERVICE_ROOT
            / "config"
            / "examples"
            / "account-a.sandbox.json"
        ).read_text(encoding="utf-8")
    )
    raw["binance"]["environment"] = environment
    if proxy_url is not None:
        raw["binance"]["proxy_url"] = proxy_url
    if include_runtime_resources:
        resources = runtime_resources
        if resources is None:
            resources = _runtime_resources()
        raw["runtime_resources"] = resources
    else:
        raw.pop("runtime_resources", None)
    if risk is not None:
        raw["risk"] = risk
    if control_plane_session is not None:
        raw["control_plane"]["session"] = control_plane_session
    path = tmp_path / "account-a.live.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _runtime_resources() -> dict[str, object]:
    return {
        "schema_version": "trader-v3-runtime-resources/v1",
        "redis": {
            "stream_max_entries": 100_000,
            "stream_max_bytes": 64 * 1024 * 1024,
            "total_stream_max_bytes": 256 * 1024 * 1024,
            "scan_count": 500,
            "sample_interval_seconds": 5.0,
            "critical_window_seconds": 30.0,
            "thread_join_timeout_seconds": 5.0,
            "memory_warning_ratio": 0.60,
            "memory_degraded_ratio": 0.75,
            "memory_critical_ratio": 0.85,
        },
        "command_journal": {
            "max_bytes": 16 * 1024 * 1024,
        },
    }


def _set_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_KEY", "key")
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_SECRET", "secret")
    monkeypatch.setenv("CONTROL_PLANE_ACCOUNT_A_TOKEN", "token")
    for name in (
        "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON",
        "NAUTILUS_MAX_ORDER_SUBMIT_RATE",
        "NAUTILUS_MAX_ORDER_MODIFY_RATE",
    ):
        monkeypatch.delenv(name, raising=False)
