from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ORIGINAL_ASYNCIO_SLEEP = asyncio.sleep

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from app.node import (  # noqa: E402
    _cancel_reconciliation_proof_refresh_task,
    _register_nautilus_reconciliation_callback,
    _run_reconciliation_proof_refresh_loop,
)
from execution_domain.contracts import ReconciliationState  # noqa: E402
from execution_domain.control_plane import TradingState  # noqa: E402
from runtime.health import HealthService  # noqa: E402
from runtime.lifecycle import DependencyName, NodeLifecycle  # noqa: E402
from runtime.reconciliation import ReconciliationCompletionCallback  # noqa: E402
from runtime.reconciliation import (  # noqa: E402
    ReconciliationDatasetSummary,
    ReconciliationProof,
)


def test_registered_callback_uses_exec_engine_result_and_cache_for_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADER_RELEASE_ID", "release-a")
    filled = OrderFilled({"trade_id": "fill-1", "quantity": "0.001"})
    ignored = OrderAccepted({"client_order_id": "order-1"})
    orders = (
        FakeOrder("order-1", events=(ignored, filled)),
        FakeOrder("order-2", events=lambda: ()),
    )
    positions = ({"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.001"},)
    engine = FakeExecEngine([True])
    node = FakeNode(engine, orders=orders, positions=positions)
    recorder = ProofRecorder()
    runtime = make_runtime(recorder)

    _register_nautilus_reconciliation_callback(
        node,
        runtime,
        schedule_refresh=False,
    )

    result = asyncio.run(engine.reconcile_execution_state(17.5))

    assert result is True
    assert engine.calls == [17.5]
    assert engine._trader_reconciliation_proof_registered is True
    assert len(recorder.calls) == 1
    proof = recorder.calls[0]
    assert proof["account_id"] == "account-a"
    assert proof["node_id"] == "node-a"
    assert proof["release_id"] == "release-a"
    assert proof["state"] is ReconciliationState.HEALTHY
    assert proof["orders"] == orders
    assert proof["positions"] == positions
    assert proof["fills"] == (filled,)
    assert proof["completed_at"].tzinfo is timezone.utc


def test_false_exec_engine_result_cannot_be_replaced_by_truthy_risk_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADER_RELEASE_ID", "release-a")
    engine = FakeExecEngine([False])
    node = FakeNode(engine)
    recorder = ProofRecorder()
    runtime = make_runtime(
        recorder,
        risk_engine_kwargs={"max_order_submit_rate": "50/00:00:01"},
    )

    _register_nautilus_reconciliation_callback(
        node,
        runtime,
        schedule_refresh=False,
    )

    result = asyncio.run(engine.reconcile_execution_state(9.0))

    assert result is False
    assert engine.calls == [9.0]
    assert recorder.states == [ReconciliationState.FAILED]


def test_exec_engine_exception_records_failed_proof_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADER_RELEASE_ID", "release-a")
    failure = RuntimeError("venue reconciliation failed")
    engine = FakeExecEngine([failure])
    node = FakeNode(engine)
    recorder = ProofRecorder()
    runtime = make_runtime(recorder)

    _register_nautilus_reconciliation_callback(
        node,
        runtime,
        schedule_refresh=False,
    )

    with pytest.raises(RuntimeError, match="venue reconciliation failed"):
        asyncio.run(engine.reconcile_execution_state(11.0))

    assert engine.calls == [11.0]
    assert recorder.states == [ReconciliationState.FAILED]


def test_reconciliation_total_deadline_records_failed_current_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADER_RELEASE_ID", "release-a")
    config = make_config()
    lifecycle = NodeLifecycle(config=config, release_id="release-a")
    mark_non_reconciliation_dependencies_ready(lifecycle)
    lifecycle.record_reconciliation_proof(
        ReconciliationProof(
            account_id=config.account_id,
            node_id=config.node_id,
            release_id="release-a",
            state=ReconciliationState.HEALTHY,
            orders=ReconciliationDatasetSummary.from_records([]),
            positions=ReconciliationDatasetSummary.from_records([]),
            fills=ReconciliationDatasetSummary.from_records([]),
            completed_at=datetime.now(timezone.utc),
        )
    )
    lifecycle.apply_operator_state(TradingState.ACTIVE, "test setup")
    runtime = make_runtime(
        ReconciliationCompletionCallback(lifecycle),
        config=config,
        lifecycle=lifecycle,
    )
    engine = BlockingExecEngine()
    node = FakeNode(engine)
    _register_nautilus_reconciliation_callback(
        node,
        runtime,
        schedule_refresh=False,
    )

    with pytest.raises(TimeoutError):
        asyncio.run(
            asyncio.wait_for(
                engine.reconcile_execution_state(0.01),
                timeout=0.1,
            )
        )

    assert lifecycle.reconciliation.status == "unhealthy"
    assert lifecycle.trading_state is TradingState.HALTED


def test_wired_proof_enforces_release_identity_and_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_at = datetime.now(timezone.utc)
    clock = FixedClock(completed_at + timedelta(seconds=1))
    config = make_config()
    lifecycle = NodeLifecycle(
        config=config,
        clock=clock,
        release_id="expected-release",
        reconciliation_proof_max_age=timedelta(seconds=5),
    )
    mark_non_reconciliation_dependencies_ready(lifecycle)
    runtime = make_runtime(
        ReconciliationCompletionCallback(lifecycle),
        config=config,
        lifecycle=lifecycle,
    )
    engine = FakeExecEngine([True, True])
    node = FakeNode(engine)
    monkeypatch.setenv("TRADER_RELEASE_ID", "wrong-release")
    _register_nautilus_reconciliation_callback(
        node,
        runtime,
        schedule_refresh=False,
    )

    asyncio.run(engine.reconcile_execution_state(5.0))

    mismatched = HealthService(lifecycle).readiness()
    assert mismatched.status_code == 503
    assert mismatched.body["reconciliation_status"] == "identity_mismatch"
    assert mismatched.body["reconciliation_proof_fresh"] is False

    monkeypatch.setenv("TRADER_RELEASE_ID", "expected-release")
    asyncio.run(engine.reconcile_execution_state(5.0))

    healthy = HealthService(lifecycle).readiness()
    assert healthy.status_code == 200
    assert healthy.body["reconciliation_status"] == "healthy"
    assert healthy.body["reconciliation_proof_fresh"] is True

    clock.advance(timedelta(seconds=5))
    stale = HealthService(lifecycle).readiness()

    assert stale.status_code == 503
    assert stale.body["reconciliation_status"] == "stale"
    assert stale.body["reconciliation_proof_fresh"] is False


def test_refresh_task_is_singleton_and_stops_when_engine_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADER_RELEASE_ID", "release-a")
    engine = FakeExecEngine([True, True, True])
    node = FakeNode(engine)
    recorder = ProofRecorder()
    runtime = make_runtime(recorder)
    sleep = ControlledSleep()
    monkeypatch.setattr("app.node.asyncio.sleep", sleep)

    async def exercise() -> None:
        _register_nautilus_reconciliation_callback(node, runtime)
        assert await engine.reconcile_execution_state(6.0) is True
        first_task = engine._trader_reconciliation_proof_task
        await sleep.wait_until_blocked()

        assert await engine.reconcile_execution_state(6.0) is True
        assert engine._trader_reconciliation_proof_task is first_task

        sleep.release_once()
        await wait_until(lambda: len(engine.calls) == 3)
        engine.running = False
        await sleep.wait_until_blocked(count=2)
        sleep.release_once()
        await asyncio.wait_for(first_task, timeout=1)

        assert first_task.done()
        assert first_task.cancelled() is False

    asyncio.run(exercise())

    assert engine.calls == [6.0, 6.0, 6.0]
    assert recorder.states == [
        ReconciliationState.HEALTHY,
        ReconciliationState.HEALTHY,
        ReconciliationState.HEALTHY,
    ]


def test_refresh_reuses_required_nautilus_timeout_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADER_RELEASE_ID", "release-a")
    engine = RequiredTimeoutExecEngine()
    node = FakeNode(engine)
    recorder = ProofRecorder()
    runtime = make_runtime(recorder)
    sleep = TwoCycleSleep()
    monkeypatch.setattr("app.node.asyncio.sleep", sleep)

    async def exercise() -> None:
        _register_nautilus_reconciliation_callback(node, runtime)
        assert await engine.reconcile_execution_state(8.0) is True
        task = engine._trader_reconciliation_proof_task
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise())

    assert (
        engine.calls,
        recorder.states,
    ) == (
        [8.0, 8.0],
        [
            ReconciliationState.HEALTHY,
            ReconciliationState.HEALTHY,
        ],
    )


def test_refresh_loop_supports_boolean_is_running_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADER_RELEASE_ID", "release-a")
    engine = BooleanStoppedExecEngine()
    node = FakeNode(engine)
    runtime = make_runtime(ProofRecorder())
    sleep = TwoCycleSleep()
    monkeypatch.setattr("app.node.asyncio.sleep", sleep)

    asyncio.run(
        _run_reconciliation_proof_refresh_loop(
            node,
            runtime,
            engine.reconcile_execution_state,
            (),
            {},
        )
    )

    assert engine.calls == []


def test_shutdown_cancels_reconciliation_refresh_task() -> None:
    engine = BooleanStoppedExecEngine()
    node = FakeNode(engine)

    async def exercise() -> None:
        task = asyncio.create_task(ORIGINAL_ASYNCIO_SLEEP(60))
        engine._trader_reconciliation_proof_task = task

        _cancel_reconciliation_proof_refresh_task(node)
        await ORIGINAL_ASYNCIO_SLEEP(0)

        assert task.done()
        assert task.cancelled()
        assert engine._trader_reconciliation_proof_task is None

    asyncio.run(exercise())


class FakeExecEngine:
    def __init__(self, outcomes: list[bool | BaseException]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[float | None] = []
        self.running = True

    async def reconcile_execution_state(
        self,
        timeout_secs: float | None = None,
    ) -> bool:
        self.calls.append(timeout_secs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def is_running(self) -> bool:
        return self.running


class RequiredTimeoutExecEngine:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def reconcile_execution_state(self, timeout_secs: float) -> bool:
        self.calls.append(timeout_secs)
        return True

    def is_running(self) -> bool:
        return True


class BooleanStoppedExecEngine:
    is_running = False

    def __init__(self) -> None:
        self.calls: list[float | None] = []

    async def reconcile_execution_state(
        self,
        timeout_secs: float | None = None,
    ) -> bool:
        self.calls.append(timeout_secs)
        return True


class BlockingExecEngine:
    is_running = True

    async def reconcile_execution_state(
        self,
        timeout_secs: float | None = None,
    ) -> bool:
        del timeout_secs
        await asyncio.Event().wait()
        return True


class FakeCache:
    def __init__(
        self,
        *,
        orders: tuple[Any, ...] = (),
        positions: tuple[Any, ...] = (),
    ) -> None:
        self._orders = orders
        self._positions = positions

    def orders(self) -> tuple[Any, ...]:
        return self._orders

    def positions(self) -> tuple[Any, ...]:
        return self._positions


class FakeNode:
    def __init__(
        self,
        engine: Any,
        *,
        orders: tuple[Any, ...] = (),
        positions: tuple[Any, ...] = (),
    ) -> None:
        self.kernel = SimpleNamespace(exec_engine=engine)
        self.cache = FakeCache(orders=orders, positions=positions)


class FakeOrder:
    def __init__(self, client_order_id: str, *, events: Any) -> None:
        self.client_order_id = client_order_id
        self.events = events

    def to_dict(self) -> dict[str, str]:
        return {"client_order_id": self.client_order_id}


class OrderFilled:
    def __init__(self, payload: dict[str, str]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, str]:
        return dict(self._payload)


class OrderAccepted:
    def __init__(self, payload: dict[str, str]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, str]:
        return dict(self._payload)


class ProofRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def states(self) -> list[ReconciliationState]:
        return [call["state"] for call in self.calls]

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value

    def advance(self, delta: timedelta) -> None:
        self._value += delta


class ControlledSleep:
    def __init__(self) -> None:
        self._permits: asyncio.Queue[None] = asyncio.Queue()
        self._blocked_count = 0

    async def __call__(self, seconds: float) -> None:
        assert seconds == 60
        self._blocked_count += 1
        await self._permits.get()

    async def wait_until_blocked(self, *, count: int = 1) -> None:
        await wait_until(lambda: self._blocked_count >= count)

    def release_once(self) -> None:
        self._permits.put_nowait(None)


class TwoCycleSleep:
    def __init__(self) -> None:
        self._calls = 0

    async def __call__(self, seconds: float) -> None:
        assert seconds == 60
        self._calls += 1
        if self._calls == 1:
            return
        raise asyncio.CancelledError


async def wait_until(predicate: Any) -> None:
    for _ in range(100):
        if predicate():
            return
        await ORIGINAL_ASYNCIO_SLEEP(0)
    raise AssertionError("condition was not reached")


def make_config() -> Any:
    return SimpleNamespace(
        account_id="account-a",
        node_id="node-a",
        reconciliation=SimpleNamespace(interval_mins=1),
    )


def make_runtime(
    reconciliation_callback: Any,
    *,
    config: Any = None,
    lifecycle: Any = None,
    risk_engine_kwargs: dict[str, Any] | None = None,
) -> Any:
    runtime_config = config
    if runtime_config is None:
        runtime_config = make_config()
    runtime_lifecycle = lifecycle
    if runtime_lifecycle is None:
        runtime_lifecycle = SimpleNamespace()
    risk_kwargs = risk_engine_kwargs
    if risk_kwargs is None:
        risk_kwargs = {}
    return SimpleNamespace(
        config=runtime_config,
        lifecycle=runtime_lifecycle,
        reconciliation_callback=reconciliation_callback,
        risk_engine_kwargs=risk_kwargs,
    )


def mark_non_reconciliation_dependencies_ready(lifecycle: NodeLifecycle) -> None:
    for dependency in DependencyName:
        if dependency is DependencyName.RECONCILIATION:
            continue
        lifecycle.mark_dependency_ready(dependency)
