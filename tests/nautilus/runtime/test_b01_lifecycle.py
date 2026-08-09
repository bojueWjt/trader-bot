from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from config.node_config import (  # noqa: E402
    CredentialResolutionError,
    NodeConfigError,
    load_node_config,
)
from execution_domain.control_plane import TradingState  # noqa: E402
from execution_domain.testing import InMemoryControlPlane  # noqa: E402
from runtime.lifecycle import DependencyName, NodeLifecycle  # noqa: E402


def test_two_account_configs_keep_process_identity_and_secrets_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_KEY", "account-a-key")
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_SECRET", "account-a-secret")
    monkeypatch.setenv("CONTROL_PLANE_ACCOUNT_A_TOKEN", "node-a-token")
    monkeypatch.setenv("BINANCE_ACCOUNT_B_API_KEY", "account-b-key")
    monkeypatch.setenv("BINANCE_ACCOUNT_B_API_SECRET", "account-b-secret")
    monkeypatch.setenv("CONTROL_PLANE_ACCOUNT_B_TOKEN", "node-b-token")

    account_a = load_node_config(
        SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
    )
    account_b = load_node_config(
        SERVICE_ROOT / "config" / "examples" / "account-b.sandbox.json"
    )

    assert account_a.account_id == "account-a"
    assert account_b.account_id == "account-b"
    assert account_a.trader_id != account_b.trader_id
    assert account_a.instance_id != account_b.instance_id
    assert account_a.redis.key_prefix != account_b.redis.key_prefix
    assert account_a.route_prefix != account_b.route_prefix
    assert account_a.binance.credentials.api_key == "account-a-key"
    assert account_b.binance.credentials.api_key == "account-b-key"
    assert account_a.binance.credential_source != account_b.binance.credential_source
    assert account_a.control_plane.auth_source != account_b.control_plane.auth_source
    assert account_a.binance.environment in {"testnet", "sandbox"}
    assert account_b.binance.environment in {"testnet", "sandbox"}


def test_missing_credentials_fail_startup_without_test_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in list(os.environ):
        if name.startswith(("BINANCE_ACCOUNT_A_", "CONTROL_PLANE_ACCOUNT_A_")):
            monkeypatch.delenv(name, raising=False)

    with pytest.raises(CredentialResolutionError) as exc:
        load_node_config(SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json")

    assert "BINANCE_ACCOUNT_A_API_KEY" in str(exc.value)
    assert "test" not in exc.value.resolved_value_hint.lower()


def test_control_plane_session_defaults_are_available_to_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)

    assert vars(config.control_plane.session) == {
        "command_delivery_capacity": 128,
        "command_ack_capacity": 256,
        "intent_delivery_capacity": 256,
        "execution_event_capacity": 1024,
        "queue_degraded_ratio": 0.8,
        "retry_budget": 3,
        "retry_base_delay_seconds": 0.05,
        "retry_max_delay_seconds": 1.0,
        "retry_jitter_ratio": 0.2,
        "circuit_reset_seconds": 5.0,
        "operation_timeout_seconds": 15.0,
        "shutdown_timeout_seconds": 1.0,
    }


def test_control_plane_session_overrides_are_parsed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = {
        "command_delivery_capacity": 12,
        "command_ack_capacity": 13,
        "intent_delivery_capacity": 14,
        "execution_event_capacity": 15,
        "queue_degraded_ratio": 0.7,
        "retry_budget": 4,
        "retry_base_delay_seconds": 0.1,
        "retry_max_delay_seconds": 0.5,
        "retry_jitter_ratio": 0.1,
        "circuit_reset_seconds": 3.0,
        "operation_timeout_seconds": 8.0,
        "shutdown_timeout_seconds": 2.0,
    }

    config = load_node_config(
        _account_a_config_with_session(monkeypatch, tmp_path, session)
    )

    assert vars(config.control_plane.session) == session


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command_delivery_capacity", 0),
        ("command_ack_capacity", -1),
        ("intent_delivery_capacity", 1.5),
        ("execution_event_capacity", True),
        ("retry_budget", 0),
        ("retry_base_delay_seconds", 0),
        ("retry_max_delay_seconds", -1),
        ("circuit_reset_seconds", 0),
        ("operation_timeout_seconds", 0),
        ("shutdown_timeout_seconds", 0),
        ("queue_degraded_ratio", 0),
        ("queue_degraded_ratio", 1),
        ("retry_jitter_ratio", -0.1),
        ("retry_jitter_ratio", 1),
    ],
)
def test_control_plane_session_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    path = _account_a_config_with_session(
        monkeypatch,
        tmp_path,
        {field: value},
    )

    with pytest.raises(NodeConfigError):
        load_node_config(path)


def test_control_plane_session_rejects_retry_delay_inversion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _account_a_config_with_session(
        monkeypatch,
        tmp_path,
        {
            "retry_base_delay_seconds": 2.0,
            "retry_max_delay_seconds": 1.0,
        },
    )

    with pytest.raises(
        NodeConfigError,
        match="retry_max_delay_seconds",
    ):
        load_node_config(path)


def test_control_plane_session_must_be_an_object(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _account_a_config_with_session(
        monkeypatch,
        tmp_path,
        "invalid",
    )

    with pytest.raises(
        NodeConfigError,
        match="control_plane.session must be an object",
    ):
        load_node_config(path)


def test_startup_is_halted_and_readiness_requires_all_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(config=config, clock=_FixedClock())

    assert lifecycle.trading_state is TradingState.HALTED
    assert lifecycle.readiness.ready is False

    for dependency in DependencyName:
        lifecycle.mark_dependency_ready(dependency)

    assert lifecycle.readiness.ready is True
    assert lifecycle.trading_state is TradingState.HALTED


def test_readiness_requires_intent_and_command_stream_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(config=config, clock=_FixedClock())
    stream_dependencies = {
        DependencyName.INTENT_STREAM,
        DependencyName.COMMAND_STREAM,
    }

    for dependency in DependencyName:
        if dependency in stream_dependencies:
            continue
        lifecycle.mark_dependency_ready(dependency)

    assert set(lifecycle.readiness.missing) == stream_dependencies

    lifecycle.mark_dependency_ready(DependencyName.INTENT_STREAM)
    assert lifecycle.readiness.ready is False

    lifecycle.mark_dependency_ready(DependencyName.COMMAND_STREAM)
    assert lifecycle.readiness.ready is True


def test_recoverable_dependency_degradation_preserves_active_trading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(config=config, clock=_FixedClock())
    for dependency in DependencyName:
        lifecycle.mark_dependency_ready(dependency)
    lifecycle.apply_operator_state(
        TradingState.ACTIVE,
        reason="operator resume",
    )

    lifecycle.mark_dependency_degraded(
        DependencyName.COMMAND_STREAM,
        "command ACK HTTP 503",
    )

    assert lifecycle.trading_state is TradingState.ACTIVE
    assert lifecycle.readiness.ready is True
    assert lifecycle.readiness.missing == ()
    assert lifecycle.readiness.degraded == (
        (
            DependencyName.COMMAND_STREAM,
            "command ACK HTTP 503",
        ),
    )

    lifecycle.mark_dependency_ready(DependencyName.COMMAND_STREAM)

    assert lifecycle.readiness.degraded == ()


def test_startup_degradation_remains_unready_without_sticky_halt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(config=config, clock=_FixedClock())

    lifecycle.mark_dependency_degraded(
        DependencyName.CONTROL_PLANE,
        "heartbeat HTTP 503",
    )

    assert lifecycle.trading_state is TradingState.HALTED
    assert lifecycle.halt_reason == "startup"
    assert DependencyName.CONTROL_PLANE in lifecycle.readiness.missing
    assert lifecycle.readiness.degraded == (
        (
            DependencyName.CONTROL_PLANE,
            "heartbeat HTTP 503",
        ),
    )


def test_heartbeat_carries_the_runtime_account_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(config=config, clock=_FixedClock())

    heartbeat = lifecycle.build_heartbeat()

    assert heartbeat.account_id == config.account_id


def test_control_plane_loss_and_stale_snapshot_degrade_active_trading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FixedClock(datetime(2026, 6, 19, 12, tzinfo=timezone.utc))
    config = _load_account_a(monkeypatch)
    control_plane = InMemoryControlPlane(now=clock.now)
    lifecycle = NodeLifecycle(config=config, clock=clock, control_plane=control_plane)

    for dependency in DependencyName:
        lifecycle.mark_dependency_ready(dependency)
    lifecycle.apply_operator_state(TradingState.ACTIVE, reason="operator resume")
    control_plane.record_snapshot(account_id=config.account_id, generated_at=clock.now())

    assert lifecycle.trading_state is TradingState.ACTIVE

    clock.advance(config.control_plane.heartbeat_timeout + timedelta(seconds=1))
    lifecycle.evaluate_safety()

    assert lifecycle.trading_state is TradingState.ACTIVE
    assert lifecycle.halt_reason == ""
    assert lifecycle.readiness.ready is True
    assert lifecycle.readiness.degraded == (
        (
            DependencyName.CONTROL_PLANE,
            "control-plane heartbeat stale",
        ),
    )

    control_plane.heartbeat(config.node_id, lifecycle.build_heartbeat())
    lifecycle.mark_dependency_ready(DependencyName.CONTROL_PLANE)
    lifecycle.mark_dependency_ready(DependencyName.PROJECTION)
    lifecycle.apply_operator_state(TradingState.ACTIVE, reason="operator resume")
    control_plane.record_snapshot(
        account_id=config.account_id,
        generated_at=clock.now() - config.control_plane.snapshot_stale_after - timedelta(seconds=1),
    )
    lifecycle.evaluate_safety()

    assert lifecycle.trading_state is TradingState.ACTIVE
    assert lifecycle.halt_reason == ""
    assert lifecycle.readiness.ready is True
    assert lifecycle.readiness.degraded == (
        (
            DependencyName.PROJECTION,
            "control-plane snapshot stale",
        ),
    )


def _load_account_a(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_KEY", "account-a-key")
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_SECRET", "account-a-secret")
    monkeypatch.setenv("CONTROL_PLANE_ACCOUNT_A_TOKEN", "node-a-token")
    return load_node_config(SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json")


def _account_a_config_with_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session: object,
) -> Path:
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_KEY", "account-a-key")
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_SECRET", "account-a-secret")
    monkeypatch.setenv("CONTROL_PLANE_ACCOUNT_A_TOKEN", "node-a-token")
    source = SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["control_plane"]["session"] = session
    target = tmp_path / "account-a.session.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    return target


class _FixedClock:
    def __init__(
        self,
        value: datetime = datetime(2026, 6, 19, 12, tzinfo=timezone.utc),
    ) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value

    def advance(self, delta: timedelta) -> None:
        self._value += delta
