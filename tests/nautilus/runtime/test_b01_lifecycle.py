from __future__ import annotations

import os
import sys
import time
from dataclasses import replace
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
from execution_domain.contracts import ReconciliationState  # noqa: E402
from execution_domain.control_plane import (  # noqa: E402
    HeartbeatReceipt,
    PeerIdentityReceipt,
    ReleaseGateReceipt,
    TradingState,
)
from execution_domain.testing import InMemoryControlPlane  # noqa: E402
from runtime.health import HealthService  # noqa: E402
from runtime.lifecycle import (  # noqa: E402
    ActorTickWatchdog,
    DependencyName,
    NodeLifecycle,
    _heartbeat_receipt_requires_halt,
)
from runtime.reconciliation import (  # noqa: E402
    ReconciliationDatasetSummary,
    ReconciliationProof,
)

REDIS_FENCING_EPOCH = "11111111-1111-4111-8111-111111111111"


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
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)

    assert lifecycle.trading_state is TradingState.HALTED
    assert lifecycle.readiness.ready is False

    _mark_all_dependencies_ready(lifecycle, clock)

    assert lifecycle.readiness.ready is True
    assert lifecycle.trading_state is TradingState.HALTED


def test_projection_degraded_reason_is_exposed_and_cleared_in_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(config=config, clock=_FixedClock())
    reason = (
        "execution projection filtered subscribed event: "
        "OrderInitialized"
    )

    lifecycle.record_projection_degraded(reason)

    degraded = lifecycle.build_heartbeat()
    assert degraded.health_degraded_reasons == (reason,)

    lifecycle.clear_projection_degraded()

    recovered = lifecycle.build_heartbeat()
    assert recovered.health_degraded_reasons == ()


def test_health_service_exposes_runtime_provider_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)
    health = HealthService(lifecycle)
    health.register_provider(
        "control_plane_session",
        lambda: {
            "started": True,
            "lanes": {
                "heartbeat": {
                    "queue_depth": 0,
                    "capacity": 1,
                },
            },
        },
    )

    response = health.readiness()

    assert response.status_code == 503
    assert response.body["providers"]["control_plane_session"] == {
        "started": True,
        "lanes": {
            "heartbeat": {
                "queue_depth": 0,
                "capacity": 1,
            },
        },
    }
    assert response.body["health_provider_errors"] == {}


def test_health_provider_failure_forces_readiness_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)
    _mark_all_dependencies_ready(lifecycle, clock)
    health = HealthService(lifecycle)

    def failed_provider() -> object:
        raise RuntimeError("snapshot unavailable")

    health.register_provider("redis_runtime_safety", failed_provider)

    response = health.readiness()

    assert response.status_code == 503
    assert response.body["ready"] is False
    assert response.body["health_provider_errors"] == {
        "redis_runtime_safety": "snapshot unavailable",
    }


@pytest.mark.parametrize(
    ("snapshot", "expected_issue"),
    [
        ({"running": False, "halted": False}, "running=false"),
        ({"running": True, "halted": True}, "halted=true"),
    ],
)
def test_redis_runtime_safety_snapshot_forces_readiness_503(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: dict[str, bool],
    expected_issue: str,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)
    _mark_all_dependencies_ready(lifecycle, clock)
    health = HealthService(lifecycle)
    health.register_provider(
        "redis_runtime_safety",
        lambda: snapshot,
    )

    response = health.readiness()

    assert response.status_code == 503
    assert response.body["ready"] is False
    assert expected_issue in response.body["health_provider_issues"][
        "redis_runtime_safety"
    ]


def test_session_process_liveness_false_forces_both_health_surfaces_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)
    _mark_all_dependencies_ready(lifecycle, clock)
    health = HealthService(lifecycle)
    health.register_provider(
        "control_plane_session",
        lambda: {
            "started": True,
            "stopped": False,
            "process_liveness": False,
            "lanes": {},
        },
    )

    live = health.liveness()
    ready = health.readiness()

    assert live.status_code == 503
    assert live.body["live"] is False
    assert ready.status_code == 503
    assert ready.body["ready"] is False
    assert "process_liveness=false" in ready.body[
        "health_provider_issues"
    ]["control_plane_session"]


def test_session_lane_circuit_and_queue_pressure_force_readiness_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)
    _mark_all_dependencies_ready(lifecycle, clock)
    health = HealthService(lifecycle)
    health.register_provider(
        "control_plane_session",
        lambda: {
            "started": True,
            "stopped": False,
            "process_liveness": True,
            "lanes": {
                "heartbeat": {
                    "circuit_state": "open",
                    "queue_pressure": "normal",
                },
                "execution_event": {
                    "circuit_state": "closed",
                    "queue_pressure": "degraded",
                },
            },
        },
    )

    live = health.liveness()
    ready = health.readiness()

    assert live.status_code == 200
    assert ready.status_code == 503
    issues = ready.body["health_provider_issues"][
        "control_plane_session"
    ]
    assert "heartbeat.circuit_state=open" in issues
    assert "execution_event.queue_pressure=degraded" in issues


def test_projection_stall_remains_monitoring_only_for_runtime_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)
    _mark_all_dependencies_ready(lifecycle, clock)
    health = HealthService(lifecycle)
    health.register_provider(
        "projection_progress",
        lambda: {
            "process_liveness": True,
            "stalled": True,
        },
    )

    live = health.liveness()
    ready = health.readiness()

    assert live.status_code == 200
    assert ready.status_code == 200
    assert ready.body["providers"]["projection_progress"]["stalled"] is True
    assert "projection_progress" not in ready.body["health_provider_issues"]


def test_live_startup_ignores_active_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    live_config = replace(
        config,
        binance=replace(config.binance, environment="live"),
    )
    monkeypatch.setenv("NAUTILUS_INITIAL_TRADING_STATE", "ACTIVE")
    monkeypatch.setenv(
        "TRADER_RELEASE_IMAGE_DIGEST",
        "sha256:" + ("1" * 64),
    )
    monkeypatch.setenv("TRADER_RELEASE_CONFIG_SHA256", "2" * 64)
    monkeypatch.setenv(
        "TRADER_RELEASE_DEPENDENCY_LOCK_SHA256",
        "3" * 64,
    )
    monkeypatch.setenv(
        "TRADER_RELEASE_SCHEMA_EPOCH",
        "0017_operator_query_projection_reads",
    )

    lifecycle = NodeLifecycle(config=live_config, clock=_FixedClock())

    assert lifecycle.trading_state is TradingState.HALTED
    assert lifecycle.halt_reason == "startup"


def test_reconciliation_dependency_reports_missing_without_blocking_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)

    for dependency in DependencyName:
        lifecycle.mark_dependency_ready(dependency)

    assert lifecycle.readiness.ready is True
    assert DependencyName.RECONCILIATION not in lifecycle.readiness.missing
    assert lifecycle.reconciliation.status == "missing"
    lifecycle.apply_operator_state(
        TradingState.ACTIVE,
        reason="operator resume",
    )
    assert lifecycle.trading_state is TradingState.ACTIVE


def test_reconciliation_proof_drift_reports_without_blocking_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(
        config=config,
        clock=clock,
        reconciliation_proof_max_age=timedelta(seconds=30),
    )
    for dependency in DependencyName:
        if dependency is not DependencyName.RECONCILIATION:
            lifecycle.mark_dependency_ready(dependency)

    lifecycle.record_reconciliation_proof(
        _healthy_proof(
            config,
            completed_at=clock.now(),
            release_id="wrong-release",
        )
    )
    assert lifecycle.reconciliation.status == "identity_mismatch"
    lifecycle.apply_operator_state(
        TradingState.ACTIVE,
        reason="operator resume",
    )
    assert lifecycle.trading_state is TradingState.ACTIVE

    lifecycle.record_reconciliation_proof(
        ReconciliationProof(
            account_id=config.account_id,
            node_id=config.node_id,
            release_id="release-a",
            state=ReconciliationState.FAILED,
            orders=ReconciliationDatasetSummary.from_records([]),
            positions=ReconciliationDatasetSummary.from_records([]),
            fills=ReconciliationDatasetSummary.from_records([]),
            completed_at=clock.now(),
        )
    )
    assert lifecycle.reconciliation.status == "unhealthy"
    assert lifecycle.build_heartbeat().reconciliation_state is (
        ReconciliationState.DEGRADED
    )
    assert lifecycle.trading_state is TradingState.ACTIVE

    lifecycle.record_reconciliation_proof(
        _healthy_proof(config, completed_at=clock.now())
    )
    lifecycle.apply_operator_state(TradingState.ACTIVE, reason="operator resume")
    assert lifecycle.trading_state is TradingState.ACTIVE

    clock.advance(timedelta(seconds=31))
    lifecycle.evaluate_safety()

    assert lifecycle.reconciliation.status == "stale"
    assert lifecycle.trading_state is TradingState.ACTIVE
    assert lifecycle.halt_reason == ""


def test_reconciliation_generation_fences_old_completion_without_blocking_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)
    for dependency in DependencyName:
        if dependency is not DependencyName.RECONCILIATION:
            lifecycle.mark_dependency_ready(dependency)

    first_generation = lifecycle.begin_reconciliation()
    lifecycle.record_reconciliation_proof(
        _healthy_proof(
            config,
            completed_at=clock.now(),
            generation=first_generation,
        )
    )
    assert lifecycle.reconciliation.status == "healthy"

    second_generation = lifecycle.begin_reconciliation()

    assert second_generation == first_generation + 1
    assert lifecycle.reconciliation.status == "in_flight"
    lifecycle.apply_operator_state(
        TradingState.ACTIVE,
        reason="operator resume",
    )
    assert lifecycle.trading_state is TradingState.ACTIVE

    lifecycle.record_reconciliation_proof(
        _healthy_proof(
            config,
            completed_at=clock.now(),
            generation=first_generation,
        )
    )

    assert lifecycle.reconciliation.status == "in_flight"

    lifecycle.record_reconciliation_proof(
        _healthy_proof(
            config,
            completed_at=clock.now(),
            generation=second_generation,
        )
    )

    assert lifecycle.reconciliation.status == "healthy"
    lifecycle.apply_operator_state(TradingState.ACTIVE, reason="operator resume")
    assert lifecycle.trading_state is TradingState.ACTIVE


def test_ready_reports_reconciliation_proof_status_and_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(
        config=config,
        clock=clock,
        reconciliation_proof_max_age=timedelta(seconds=30),
    )
    health = HealthService(lifecycle)

    pending = health.readiness()
    assert pending.body["reconciliation_status"] == "missing"
    assert pending.body["reconciliation_proof_age_seconds"] is None

    _mark_all_dependencies_ready(lifecycle, clock)
    clock.advance(timedelta(seconds=12))
    ready = health.readiness()

    assert ready.status_code == 200
    assert ready.body["reconciliation_status"] == "healthy"
    assert ready.body["reconciliation_proof_age_seconds"] == 12.0
    assert ready.body["reconciliation_proof_fresh"] is True

    lifecycle.apply_operator_state(TradingState.ACTIVE, reason="operator resume")
    clock.advance(timedelta(seconds=19))
    stale = health.readiness()

    assert stale.status_code == 200
    assert stale.body["reconciliation_status"] == "stale"
    assert stale.body["reconciliation_proof_age_seconds"] == 31.0
    assert lifecycle.trading_state is TradingState.ACTIVE

    lifecycle.record_reconciliation_proof(
        _healthy_proof(config, completed_at=clock.now())
    )
    assert lifecycle.readiness.ready is True
    assert lifecycle.trading_state is TradingState.ACTIVE

    lifecycle.apply_operator_state(TradingState.ACTIVE, reason="operator resume")
    assert lifecycle.trading_state is TradingState.ACTIVE


def test_runtime_without_release_identity_reports_reconciliation_proof_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(
        config=config,
        clock=clock,
        release_id="",
    )
    for dependency in DependencyName:
        if dependency is not DependencyName.RECONCILIATION:
            lifecycle.mark_dependency_ready(dependency)
    lifecycle.record_reconciliation_proof(
        _healthy_proof(config, completed_at=clock.now())
    )

    assert lifecycle.reconciliation.status == "release_identity_missing"
    lifecycle.apply_operator_state(
        TradingState.ACTIVE,
        reason="operator resume",
    )
    assert lifecycle.trading_state is TradingState.ACTIVE


def test_reconciliation_summary_digest_is_stable_and_rejects_opaque_records() -> None:
    first = ReconciliationDatasetSummary.from_records(
        [
            {"venue_order_id": "order-2", "quantity": "2"},
            {"venue_order_id": "order-1", "quantity": "1"},
        ]
    )
    second = ReconciliationDatasetSummary.from_records(
        [
            {"quantity": "1", "venue_order_id": "order-1"},
            {"quantity": "2", "venue_order_id": "order-2"},
        ]
    )

    assert first == second
    with pytest.raises(TypeError, match="stable scalars"):
        ReconciliationDatasetSummary.from_records([object()])


def test_heartbeat_carries_the_runtime_account_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(config=config, clock=_FixedClock())

    heartbeat = lifecycle.build_heartbeat()

    assert heartbeat.account_id == config.account_id


def test_live_heartbeat_release_drift_is_sticky_halted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    live_config = replace(
        config,
        binance=replace(config.binance, environment="live"),
    )
    monkeypatch.setenv(
        "TRADER_RELEASE_IMAGE_DIGEST",
        "sha256:" + ("1" * 64),
    )
    monkeypatch.setenv("TRADER_RELEASE_CONFIG_SHA256", "2" * 64)
    monkeypatch.setenv(
        "TRADER_RELEASE_DEPENDENCY_LOCK_SHA256",
        "3" * 64,
    )
    monkeypatch.setenv(
        "TRADER_RELEASE_SCHEMA_EPOCH",
        "0017_operator_query_projection_reads",
    )

    class ControlPlane:
        def heartbeat(self, node_id, heartbeat):
            del node_id, heartbeat
            return HeartbeatReceipt(
                release_gate=ReleaseGateReceipt(
                    status="drift",
                    release_id="release-a",
                    reviewed_manifest=None,
                ),
            )

    lifecycle = NodeLifecycle(
        config=live_config,
        clock=_FixedClock(),
        control_plane=ControlPlane(),
    )

    with pytest.raises(
        RuntimeError,
        match="heartbeat release gate failed: drift",
    ):
        lifecycle.send_heartbeat()

    assert lifecycle.trading_state is TradingState.HALTED
    assert DependencyName.CONTROL_PLANE in lifecycle.readiness.missing
    assert "heartbeat release gate failed: drift" in lifecycle.halt_reason


def test_live_heartbeat_peer_stale_is_non_blocking_warning() -> None:
    receipt = HeartbeatReceipt(
        release_gate=ReleaseGateReceipt(
            status="pass",
            release_id="release-a",
            reviewed_manifest=None,
            rollout_phase="fleet_complete",
            live_open_mode="normal",
            phase_version=5,
        ),
        peers=(
            PeerIdentityReceipt(
                node_id="nautilus-node-account-d",
                account_id="account-d",
                release_id="release-a",
                image_digest="sha256:" + ("1" * 64),
                config_sha256="2" * 64,
                dependency_lock_sha256="3" * 64,
                schema_epoch="0017_operator_query_projection_reads",
                redis_fencing_epoch=REDIS_FENCING_EPOCH,
                freshness_age_seconds=3600.0,
                fresh=False,
                identity_matches=True,
                status="stale",
            ),
        ),
    )

    assert receipt.requires_sticky_halt is False
    assert _heartbeat_receipt_requires_halt(receipt, live=True) is False


def test_heartbeat_receipt_updates_rollout_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(config=config, clock=_FixedClock())
    assert lifecycle.rollout_phase is None

    lifecycle.record_heartbeat_receipt(
        HeartbeatReceipt(
            release_gate=ReleaseGateReceipt(
                status="pass",
                release_id="release-a",
                reviewed_manifest=None,
                rollout_phase="fleet_complete",
                live_open_mode="normal",
                phase_version=5,
            ),
        )
    )

    assert lifecycle.rollout_phase == "fleet_complete"
    assert lifecycle.live_open_gate == {
        "mode": "normal",
        "release_id": "release-a",
        "rollout_phase": "fleet_complete",
        "phase_version": 5,
    }


def test_live_open_gate_validation_rejects_stale_phase_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(config=config, clock=_FixedClock())
    lifecycle.record_heartbeat_receipt(
        HeartbeatReceipt(
            release_gate=ReleaseGateReceipt(
                status="pass",
                release_id="release-a",
                reviewed_manifest=None,
                rollout_phase="fleet_complete",
                live_open_mode="normal",
                phase_version=5,
            ),
        )
    )

    with pytest.raises(RuntimeError, match="live_open_gate_mismatch"):
        lifecycle.validate_live_open_gate(
            {
                "mode": "normal",
                "release_id": "release-a",
                "rollout_phase": "fleet_complete",
                "phase_version": 4,
            },
            require_normal=True,
        )


def test_heartbeat_build_monotonically_fences_runtime_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(config=config, clock=_FixedClock())
    lifecycle.configure_lease(
        redis_fencing_epoch=REDIS_FENCING_EPOCH,
        generation=41,
        freshness_seconds=10,
    )

    first = lifecycle.build_heartbeat()
    second = lifecycle.build_heartbeat()

    assert first.runtime_generation == lifecycle.runtime_generation
    assert second.runtime_generation == lifecycle.runtime_generation
    assert first.redis_fencing_epoch == REDIS_FENCING_EPOCH
    assert second.redis_fencing_epoch == REDIS_FENCING_EPOCH
    assert first.lease_fencing_token == 41
    assert second.lease_fencing_token == 41
    assert first.heartbeat_sequence == 1
    assert second.heartbeat_sequence == 2


def test_writer_bootstrap_heartbeat_omits_release_and_exchange_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(
        config=config,
        clock=_FixedClock(),
        release_id="release-a",
    )
    lifecycle._release_identity = {
        "image_digest": "sha256:" + ("1" * 64),
        "config_sha256": "2" * 64,
        "dependency_lock_sha256": "3" * 64,
        "schema_epoch": "0017_operator_query_projection_reads",
    }
    lifecycle.configure_lease(
        redis_fencing_epoch=REDIS_FENCING_EPOCH,
        generation=41,
        freshness_seconds=10,
    )

    heartbeat = lifecycle.build_writer_bootstrap_heartbeat()

    assert heartbeat.account_id == config.account_id
    assert heartbeat.runtime_generation == lifecycle.runtime_generation
    assert heartbeat.redis_fencing_epoch == REDIS_FENCING_EPOCH
    assert heartbeat.lease_fencing_token == 41
    assert heartbeat.heartbeat_sequence == 1
    assert heartbeat.release_id is None
    assert heartbeat.image_digest is None
    assert heartbeat.config_sha256 is None
    assert heartbeat.dependency_lock_sha256 is None
    assert heartbeat.schema_epoch is None
    assert heartbeat.positions is None
    assert heartbeat.regular_orders is None
    assert heartbeat.algo_orders is None


def test_heartbeat_carries_release_identity_exchange_evidence_and_proof_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TRADER_RELEASE_IMAGE_DIGEST",
        "sha256:" + ("1" * 64),
    )
    monkeypatch.setenv("TRADER_RELEASE_CONFIG_SHA256", "2" * 64)
    monkeypatch.setenv(
        "TRADER_RELEASE_DEPENDENCY_LOCK_SHA256",
        "3" * 64,
    )
    monkeypatch.setenv(
        "TRADER_RELEASE_SCHEMA_EPOCH",
        "0017_operator_query_projection_reads",
    )
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)
    generation = lifecycle.begin_reconciliation()
    lifecycle.record_reconciliation_proof(
        _healthy_proof(
            config,
            completed_at=clock.now(),
            generation=generation,
        )
    )

    heartbeat = lifecycle.build_heartbeat(
        exchange_evidence={
            "positions": [{"symbol": "ETHUSDT", "quantity": "-0.01"}],
            "regular_orders": [
                {"symbol": "BTCUSDT", "client_order_id": "regular-1"}
            ],
            "algo_orders": [
                {"symbol": "ETHUSDT", "client_order_id": "algo-1"}
            ],
            "fetched_at": clock.now(),
        }
    )

    assert heartbeat.release_id == "release-a"
    assert heartbeat.image_digest == "sha256:" + ("1" * 64)
    assert heartbeat.config_sha256 == "2" * 64
    assert heartbeat.dependency_lock_sha256 == "3" * 64
    assert heartbeat.schema_epoch == "0017_operator_query_projection_reads"
    assert heartbeat.positions == (
        {"symbol": "ETHUSDT", "quantity": "-0.01"},
    )
    assert heartbeat.regular_orders_snapshot_at == clock.now()
    assert heartbeat.algo_orders_snapshot_at == clock.now()
    assert heartbeat.reconciliation_completed_at == clock.now()


def test_heartbeat_preserves_last_completed_reconciliation_during_next_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)
    first_generation = lifecycle.begin_reconciliation()
    lifecycle.record_reconciliation_proof(
        _healthy_proof(
            config,
            completed_at=clock.now(),
            generation=first_generation,
        )
    )
    last_completed_at = clock.now()

    clock.advance(timedelta(seconds=1))
    second_generation = lifecycle.begin_reconciliation()
    heartbeat = lifecycle.build_heartbeat()

    assert lifecycle.reconciliation.status == "in_flight"
    assert heartbeat.reconciliation_completed_at == last_completed_at

    lifecycle.record_reconciliation_proof(
        _healthy_proof(
            config,
            completed_at=clock.now(),
            generation=second_generation,
        )
    )
    refreshed_heartbeat = lifecycle.build_heartbeat()

    assert lifecycle.reconciliation.status == "healthy"
    assert refreshed_heartbeat.reconciliation_completed_at == clock.now()


def test_heartbeat_build_enriches_open_orders_from_registered_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(config=config, clock=_FixedClock())
    lifecycle.set_open_orders_provider(
        lambda: [
            {
                "client_order_id": "order-1",
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "side": "BUY",
            }
        ]
    )

    heartbeat = lifecycle.build_heartbeat()

    assert heartbeat.open_orders == (
        {
            "client_order_id": "order-1",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "side": "BUY",
        },
    )


def test_control_plane_loss_and_stale_snapshot_auto_halt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FixedClock(datetime(2026, 6, 19, 12, tzinfo=timezone.utc))
    config = _load_account_a(monkeypatch)
    control_plane = InMemoryControlPlane(now=clock.now)
    lifecycle = NodeLifecycle(config=config, clock=clock, control_plane=control_plane)

    _mark_all_dependencies_ready(lifecycle, clock)
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


def test_dependency_recovery_keeps_node_halted_until_explicit_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)
    _mark_all_dependencies_ready(lifecycle, clock)
    lifecycle.apply_operator_state(TradingState.ACTIVE, reason="operator resume")

    lifecycle.mark_dependency_failed(
        DependencyName.COMMAND_STREAM,
        "operator command poll stale",
    )
    lifecycle.mark_dependency_ready(DependencyName.COMMAND_STREAM)

    assert lifecycle.readiness.ready is True
    assert lifecycle.trading_state is TradingState.HALTED
    assert lifecycle.halt_reason == "command_stream failed: operator command poll stale"

    lifecycle.apply_operator_state(TradingState.ACTIVE, reason="operator resume")

    assert lifecycle.trading_state is TradingState.ACTIVE


def test_redis_runtime_safety_failure_requires_restart_and_stays_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)
    _mark_all_dependencies_ready(lifecycle, clock)
    lifecycle.apply_operator_state(
        TradingState.ACTIVE,
        reason="operator resume",
    )

    lifecycle.mark_dependency_failed(
        DependencyName.REDIS,
        "Redis runtime safety stream byte limit exceeded",
    )
    lifecycle.mark_dependency_ready(DependencyName.REDIS)

    assert lifecycle.restart_required is True
    assert lifecycle.trading_state is TradingState.HALTED
    assert DependencyName.REDIS in lifecycle.readiness.missing
    with pytest.raises(RuntimeError, match="restart is required"):
        lifecycle.apply_operator_state(
            TradingState.ACTIVE,
            reason="operator resume",
        )

    replacement = NodeLifecycle(config=config, clock=clock)

    assert replacement.restart_required is False
    assert replacement.trading_state is TradingState.HALTED


def test_stale_lease_generation_halts_active_and_requires_refresh_then_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    monotonic = _FixedMonotonic()
    lifecycle = NodeLifecycle(
        config=config,
        clock=clock,
        monotonic=monotonic,
    )
    _mark_all_dependencies_ready(lifecycle, clock)
    lifecycle.configure_lease(
        redis_fencing_epoch=REDIS_FENCING_EPOCH,
        generation=41,
        freshness_seconds=10,
    )
    lifecycle.apply_operator_state(TradingState.ACTIVE, reason="operator resume")

    monotonic.advance(11)

    assert lifecycle.trading_state is TradingState.HALTED
    assert lifecycle.halt_reason == "Redis namespace lease freshness expired"
    with pytest.raises(RuntimeError, match="lease freshness expired"):
        lifecycle.apply_operator_state(
            TradingState.ACTIVE,
            reason="operator resume",
        )

    lifecycle.record_lease_refresh(
        redis_fencing_epoch=REDIS_FENCING_EPOCH,
        generation=41,
    )

    assert lifecycle.trading_state is TradingState.HALTED
    lifecycle.apply_operator_state(TradingState.ACTIVE, reason="operator resume")
    assert lifecycle.trading_state is TradingState.ACTIVE


def test_risk_generation_rejects_stale_runtime_and_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(config=config, clock=_FixedClock())
    lifecycle.configure_lease(
        redis_fencing_epoch=REDIS_FENCING_EPOCH,
        generation=9,
        freshness_seconds=10,
    )

    with pytest.raises(RuntimeError, match="runtime generation"):
        lifecycle.validate_risk_generation(
            runtime_generation="old-runtime",
            reconciliation_generation=lifecycle.reconciliation_generation,
            lease_generation=9,
        )
    lifecycle.validate_risk_generation(
        runtime_generation=lifecycle.runtime_generation,
        reconciliation_generation=lifecycle.reconciliation_generation + 1,
        lease_generation=9,
    )
    with pytest.raises(RuntimeError, match="lease generation"):
        lifecycle.validate_risk_generation(
            runtime_generation=lifecycle.runtime_generation,
            reconciliation_generation=lifecycle.reconciliation_generation,
            lease_generation=8,
        )


def test_actor_tick_watchdog_halts_without_actor_timer_and_requires_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_account_a(monkeypatch)
    clock = _FixedClock()
    lifecycle = NodeLifecycle(config=config, clock=clock)
    _mark_all_dependencies_ready(lifecycle, clock)
    lifecycle.apply_operator_state(TradingState.ACTIVE, reason="operator resume")
    stale_ages: list[float] = []
    restart_ages: list[float] = []
    watchdog = ActorTickWatchdog(
        lifecycle,
        stale_after_seconds=0.02,
        restart_after_seconds=0.05,
        check_interval_seconds=0.005,
        on_stale=stale_ages.append,
        on_restart_required=restart_ages.append,
    )

    watchdog.start()
    try:
        assert _wait_until(
            lambda: lifecycle.trading_state is TradingState.HALTED,
            timeout=0.5,
        )
        assert DependencyName.COMMAND_STREAM in lifecycle.readiness.missing
        assert lifecycle.actor_tick_age_seconds >= 0.02
        assert _wait_until(lambda: len(stale_ages) == 1, timeout=0.5)
        assert _wait_until(lambda: lifecycle.restart_required, timeout=0.5)
        assert _wait_until(lambda: len(restart_ages) == 1, timeout=0.5)
        health = HealthService(lifecycle)
        live = health.liveness()
        ready = health.readiness()

        assert live.status_code == 503
        assert live.body["live"] is False
        assert live.body["actor_tick_age_seconds"] >= 0.05
        assert live.body["restart_required"] is True
        assert ready.status_code == 503
        assert ready.body["ready"] is False
        assert ready.body["actor_tick_age_seconds"] >= 0.05
        assert ready.body["restart_required"] is True

        watchdog.record_tick()
        lifecycle.mark_dependency_ready(DependencyName.COMMAND_STREAM)

        assert lifecycle.restart_required is True
        assert DependencyName.COMMAND_STREAM in lifecycle.readiness.missing
        with pytest.raises(RuntimeError, match="restart is required"):
            lifecycle.apply_operator_state(
                TradingState.ACTIVE,
                reason="operator resume",
            )
    finally:
        watchdog.stop()

    watchdog.start()
    try:
        assert _wait_until(lambda: len(stale_ages) == 2, timeout=0.5)
        assert _wait_until(lambda: len(restart_ages) == 2, timeout=0.5)
    finally:
        watchdog.stop()


def _load_account_a(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_KEY", "account-a-key")
    monkeypatch.setenv("BINANCE_ACCOUNT_A_API_SECRET", "account-a-secret")
    monkeypatch.setenv("CONTROL_PLANE_ACCOUNT_A_TOKEN", "node-a-token")
    monkeypatch.setenv("TRADER_RELEASE_ID", "release-a")
    return load_node_config(SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json")


def _mark_all_dependencies_ready(
    lifecycle: NodeLifecycle,
    clock: "_FixedClock",
) -> None:
    for dependency in DependencyName:
        if dependency is DependencyName.RECONCILIATION:
            continue
        lifecycle.mark_dependency_ready(dependency)
    lifecycle.record_reconciliation_proof(
        _healthy_proof(lifecycle.config, completed_at=clock.now())
    )


def _healthy_proof(
    config,
    *,
    completed_at: datetime,
    release_id: str = "release-a",
    generation: int = 0,
) -> ReconciliationProof:
    return ReconciliationProof(
        account_id=config.account_id,
        node_id=config.node_id,
        release_id=release_id,
        state=ReconciliationState.HEALTHY,
        orders=ReconciliationDatasetSummary.from_records([]),
        positions=ReconciliationDatasetSummary.from_records([]),
        fills=ReconciliationDatasetSummary.from_records([]),
        completed_at=completed_at,
        generation=generation,
    )


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


class _FixedMonotonic:
    def __init__(self) -> None:
        self._value = 100.0

    def __call__(self) -> float:
        return self._value

    def advance(self, seconds: float) -> None:
        self._value += seconds


def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


def test_readiness_payload_carries_node_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deploy-side quiesce checks assert the /ready endpoint they reached
    # belongs to the intended account, so the readiness payload must
    # carry the node's identity fields.
    config = _load_account_a(monkeypatch)
    lifecycle = NodeLifecycle(config=config, clock=_FixedClock())
    health = HealthService(lifecycle)

    response = health.readiness()

    assert response.body["account_id"] == config.account_id
    assert response.body["node_id"] == config.node_id
