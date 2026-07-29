from __future__ import annotations

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


def test_control_plane_loss_and_stale_snapshot_auto_halt(
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

    assert lifecycle.trading_state is TradingState.HALTED
    assert lifecycle.halt_reason == "control-plane heartbeat stale"

    control_plane.heartbeat(config.node_id, lifecycle.build_heartbeat())
    lifecycle.mark_dependency_ready(DependencyName.CONTROL_PLANE)
    lifecycle.mark_dependency_ready(DependencyName.PROJECTION)
    lifecycle.apply_operator_state(TradingState.ACTIVE, reason="operator resume")
    control_plane.record_snapshot(
        account_id=config.account_id,
        generated_at=clock.now() - config.control_plane.snapshot_stale_after - timedelta(seconds=1),
    )
    lifecycle.evaluate_safety()

    assert lifecycle.trading_state is TradingState.HALTED
    assert lifecycle.halt_reason == "control-plane snapshot stale"


def _load_account_a(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_KEY", "account-a-key")
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_SECRET", "account-a-secret")
    monkeypatch.setenv("CONTROL_PLANE_ACCOUNT_A_TOKEN", "node-a-token")
    return load_node_config(SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json")


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
