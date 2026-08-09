from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from app import run_node  # noqa: E402
from strategy.intent_execution_strategy import (  # noqa: E402
    IntentExecutionStrategy,
    IntentExecutionStrategyConfig,
)
from strategy.intent_execution_planner import (  # noqa: E402
    ManagementPlan,
    OrderPlan,
)


class _LeaseGuard:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _RecordingStrategy(IntentExecutionStrategy):
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self.scheduled: list[tuple[str, int]] = []
        self.submitted_plans: list[OrderPlan] = []
        self.cancelled_client_order_ids: list[str] = []
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id="account-a",
                node_id="node-a",
                trading_state="ACTIVE",
            )
        )

    def _protection_stash_path(self) -> str:
        return str(self._state_dir / self._PROTECTION_STASH_FILENAME)

    def _now(self) -> datetime:
        return datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

    def _schedule_protection_sync(
        self,
        intent_key: str,
        delay_seconds: float | None = None,
    ) -> None:
        del delay_seconds
        self.scheduled.append((intent_key, get_ident()))

    def _submit_order_plan(self, plan: OrderPlan) -> bool:
        self.submitted_plans.append(plan)
        return True

    def _cancel_management_order(
        self,
        instrument_id: str,
        client_order_id: str,
    ) -> bool:
        del instrument_id
        self.cancelled_client_order_ids.append(client_order_id)
        return True


class _SmallQueueStrategy(_RecordingStrategy):
    _DURABLE_IO_QUEUE_CAPACITY = 1


class _ShortTimeoutStrategy(_RecordingStrategy):
    _DURABLE_IO_TASK_TIMEOUT_SECONDS = 0.02


def test_blocked_protection_stash_write_returns_immediately_and_actor_progress_continues(
    tmp_path: Path,
) -> None:
    strategy = _RecordingStrategy(tmp_path)
    actor_thread_id = get_ident()
    strategy._start_durable_io_lane()
    writer_started = Event()
    writer_release = Event()
    original_write = strategy._write_entry_protection_stash

    def blocking_write(payload: Any) -> None:
        writer_started.set()
        writer_release.wait(timeout=5.0)
        original_write(payload)

    strategy._write_entry_protection_stash = blocking_write  # type: ignore[method-assign]
    strategy._entry_protection_stash["intent-a"] = {
        "nested": {"value": "before"},
        "take_profits": ("10", "11"),
    }
    try:
        submitted_at = time.monotonic()
        accepted = strategy._queue_entry_protection_stash_persist(
            continuation={
                "kind": "protection_schedule",
                "intent_key": "intent-a",
            }
        )
        elapsed = time.monotonic() - submitted_at

        assert accepted is True
        assert elapsed < 0.05
        assert writer_started.wait(timeout=1.0)

        actor_progress = []
        for tick in range(5):
            actor_progress.append(tick)
            assert strategy.drain_durable_io_mailbox() == 0
        assert actor_progress == [0, 1, 2, 3, 4]
        assert strategy.scheduled == []

        strategy._entry_protection_stash["intent-a"]["nested"]["value"] = "after"
        strategy._entry_protection_stash["intent-a"]["take_profits"] = ("99",)
        writer_release.set()
        assert strategy.wait_for_durable_io(timeout_seconds=1.0)

        assert strategy.scheduled == []
        assert strategy.drain_durable_io_mailbox() == 1
        assert strategy.scheduled == [("intent-a", actor_thread_id)]
        assert strategy._protection_stash_persisted_version == 1
        persisted = json.loads(
            Path(strategy._protection_stash_path()).read_text(
                encoding="utf-8"
            )
        )
        assert persisted["intent-a"]["nested"]["value"] == "before"
        assert persisted["intent-a"]["take_profits"] == ["10", "11"]
    finally:
        writer_release.set()
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def test_stale_version_result_waits_for_latest_persist_before_continuations(
    tmp_path: Path,
) -> None:
    strategy = _RecordingStrategy(tmp_path)
    strategy._start_durable_io_lane()
    first_started = Event()
    first_release = Event()
    write_count = 0
    original_write = strategy._write_entry_protection_stash

    def blocking_first_write(payload: Any) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 1:
            first_started.set()
            first_release.wait(timeout=5.0)
        original_write(payload)

    strategy._write_entry_protection_stash = blocking_first_write  # type: ignore[method-assign]
    strategy._entry_protection_stash["intent-a"] = {"state": "first"}
    try:
        assert strategy._queue_entry_protection_stash_persist(
            continuation={
                "kind": "protection_schedule",
                "intent_key": "intent-a",
            }
        )
        assert first_started.wait(timeout=1.0)

        strategy._entry_protection_stash["intent-a"]["state"] = "latest"
        assert strategy._queue_entry_protection_stash_persist(
            continuation={
                "kind": "protection_schedule",
                "intent_key": "intent-latest",
            }
        )
        first_release.set()
        assert strategy.wait_for_durable_io(timeout_seconds=1.0)

        assert strategy.drain_durable_io_mailbox(max_results=1) == 1
        assert strategy.scheduled == []
        assert strategy.drain_durable_io_mailbox() == 1
        assert [item[0] for item in strategy.scheduled] == [
            "intent-a",
            "intent-latest",
        ]
        assert strategy._protection_stash_persisted_version == 2
    finally:
        first_release.set()
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def test_entry_submit_waits_for_actor_consumed_durable_result(
    tmp_path: Path,
) -> None:
    strategy = _RecordingStrategy(tmp_path)
    strategy._start_durable_io_lane()
    writer_started = Event()
    writer_release = Event()
    original_write = strategy._write_entry_protection_stash
    plan = _order_plan(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "entry-order",
        order_type="LIMIT",
    )

    def blocking_write(payload: Any) -> None:
        writer_started.set()
        writer_release.wait(timeout=5.0)
        original_write(payload)

    strategy._write_entry_protection_stash = blocking_write  # type: ignore[method-assign]
    strategy._entry_protection_stash[str(plan.intent_id)] = {
        "instrument_id": plan.instrument_id,
        "entry_side": plan.side,
    }
    try:
        assert strategy._queue_entry_protection_stash_persist(
            continuation={
                "kind": "entry_submit",
                "plan": plan,
                "source_intent": False,
                "protection_preimage": {},
            }
        )
        assert writer_started.wait(timeout=1.0)
        assert strategy.submitted_plans == []

        writer_release.set()
        assert strategy.wait_for_durable_io(timeout_seconds=1.0)
        assert strategy.submitted_plans == []

        assert strategy.drain_durable_io_mailbox() == 1
        assert strategy.submitted_plans == [plan]
        assert str(plan.intent_id) in strategy._processed_intent_ids
    finally:
        writer_release.set()
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def test_management_side_effects_wait_for_actor_consumed_durable_result(
    tmp_path: Path,
) -> None:
    strategy = _RecordingStrategy(tmp_path)
    strategy._start_durable_io_lane()
    writer_started = Event()
    writer_release = Event()
    original_write = strategy._write_entry_protection_stash
    owner_intent_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    management_intent_id = UUID(
        "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    )
    replacement = _order_plan(
        management_intent_id,
        "replacement-stop",
        order_type="STOP_MARKET",
    )
    plan = ManagementPlan(
        intent_id=management_intent_id,
        action="move_stop_loss",
        instrument_id=replacement.instrument_id,
        target_position_id="BTCUSDT-PERP.BINANCE-LONG",
        target_position_side="LONG",
        cancel_order_ids=("old-stop",),
        orders=(replacement,),
        authorization={
            "authorized_by_type": "user",
            "authorized_by_id": "risk-admin",
            "source_message_id": "management-1",
            "parent_intent_id": str(owner_intent_id),
        },
    )

    def blocking_write(payload: Any) -> None:
        writer_started.set()
        writer_release.wait(timeout=5.0)
        original_write(payload)

    strategy._write_entry_protection_stash = blocking_write  # type: ignore[method-assign]
    strategy._entry_protection_stash[str(owner_intent_id)] = {
        "instrument_id": replacement.instrument_id,
        "entry_side": "BUY",
        "entry_tags": (),
        "stop_loss": "25000",
        "protection_roles": {},
        "tp_consumed": {},
        "pending_cancel_ids": (),
    }
    try:
        assert strategy._absorb_management_plan(
            plan,
            continuation={
                "kind": "management_dispatch",
                "mode": "replace_protection",
                "plan": plan,
                "source_intent": False,
                "cancel_order_ids": ("old-stop",),
            },
        )
        assert writer_started.wait(timeout=1.0)
        assert strategy.submitted_plans == []
        assert strategy.cancelled_client_order_ids == []

        writer_release.set()
        assert strategy.wait_for_durable_io(timeout_seconds=1.0)
        assert strategy.submitted_plans == []
        assert strategy.cancelled_client_order_ids == []

        assert strategy.drain_durable_io_mailbox() == 1
        assert strategy.submitted_plans == [replacement]
        assert strategy.cancelled_client_order_ids == ["old-stop"]
        assert str(plan.intent_id) in strategy._processed_intent_ids
    finally:
        writer_release.set()
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def test_strategy_cleanup_retries_same_worker_before_lease_release(
    tmp_path: Path,
) -> None:
    strategy = _RecordingStrategy(tmp_path)
    strategy._DURABLE_IO_SHUTDOWN_TIMEOUT_SECONDS = 0.01
    fatal_reasons: list[str] = []
    strategy.set_durable_io_fatal_handler(fatal_reasons.append)
    strategy._start_durable_io_lane()
    writer_started = Event()
    writer_release = Event()

    def blocking_write(_payload: Any) -> None:
        writer_started.set()
        writer_release.wait(timeout=5.0)

    strategy._write_entry_protection_stash = blocking_write  # type: ignore[method-assign]
    strategy._entry_protection_stash["intent-a"] = {"state": "pending"}
    worker = strategy.durable_io_cleanup_worker()
    original_stop = worker.stop

    def short_stop(*, timeout_seconds: float = 0.01) -> bool:
        return original_stop(timeout_seconds=timeout_seconds)

    worker.stop = short_stop  # type: ignore[method-assign]
    assert strategy._queue_entry_protection_stash_persist()
    assert writer_started.wait(timeout=1.0)

    lease_guard = _LeaseGuard()
    runtime = SimpleNamespace(
        control_plane_session=None,
        trading_node=None,
        background_workers=[worker],
        redis_runtime_safety_guard=None,
        redis_runtime_safety_client=None,
        namespace_lease_guard=lease_guard,
    )
    try:
        strategy.on_stop()

        with pytest.raises(RuntimeError, match="runtime cleanup failed"):
            run_node._cleanup_runtime(runtime, False)

        assert strategy.durable_io_cleanup_worker() is worker
        assert runtime.background_workers == [worker]
        assert lease_guard.closed is False
        assert len(fatal_reasons) == 1
        assert "failed to stop" in fatal_reasons[0]

        writer_release.set()
        deadline = time.monotonic() + 1.0
        while worker.snapshot().running and time.monotonic() < deadline:
            time.sleep(0.001)
        run_node._cleanup_runtime(runtime, False)

        assert runtime.background_workers == []
        assert lease_guard.closed is True
        assert len(fatal_reasons) == 1
    finally:
        writer_release.set()
        original_stop(timeout_seconds=1.0)


def test_queue_overflow_enters_sticky_fatal_handler(tmp_path: Path) -> None:
    strategy = _SmallQueueStrategy(tmp_path)
    fatal_reasons: list[str] = []
    strategy.set_durable_io_fatal_handler(fatal_reasons.append)
    strategy._start_durable_io_lane()
    writer_started = Event()
    writer_release = Event()

    def blocking_write(_payload: Any) -> None:
        writer_started.set()
        writer_release.wait(timeout=5.0)

    strategy._write_entry_protection_stash = blocking_write  # type: ignore[method-assign]
    try:
        assert strategy._queue_entry_protection_stash_persist()
        assert writer_started.wait(timeout=1.0)
        assert strategy._queue_entry_protection_stash_persist()
        assert strategy._queue_entry_protection_stash_persist() is False
        assert len(fatal_reasons) == 1
        assert "queue capacity exceeded" in fatal_reasons[0]

        assert strategy._queue_entry_protection_stash_persist() is False
        assert len(fatal_reasons) == 1
    finally:
        writer_release.set()
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def _order_plan(
    intent_id: UUID,
    client_order_id: str,
    *,
    order_type: str,
) -> OrderPlan:
    trigger_price = None
    price = "26000"
    if order_type == "STOP_MARKET":
        trigger_price = "25000"
        price = None
    return OrderPlan(
        intent_id=intent_id,
        client_order_id=client_order_id,
        tags=(f"intent_id={intent_id}",),
        instrument_id="BTCUSDT-PERP.BINANCE",
        side="BUY",
        order_type=order_type,
        quantity="0.1",
        price=price,
        time_in_force="GTC",
        reduce_only=order_type == "STOP_MARKET",
        trigger_price=trigger_price,
    )


def test_handler_error_enters_sticky_fatal_handler(tmp_path: Path) -> None:
    strategy = _RecordingStrategy(tmp_path)
    fatal_reasons: list[str] = []
    strategy.set_durable_io_fatal_handler(fatal_reasons.append)
    strategy._start_durable_io_lane()

    def failing_write(_payload: Any) -> None:
        raise RuntimeError("disk failed")

    strategy._write_entry_protection_stash = failing_write  # type: ignore[method-assign]
    try:
        assert strategy._queue_entry_protection_stash_persist()
        assert strategy.wait_for_durable_io(timeout_seconds=1.0)
        assert len(fatal_reasons) == 1
        assert "handler failed" in fatal_reasons[0]

        assert strategy._queue_entry_protection_stash_persist() is False
        assert len(fatal_reasons) == 1
    finally:
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def test_task_timeout_enters_sticky_fatal_handler(tmp_path: Path) -> None:
    strategy = _ShortTimeoutStrategy(tmp_path)
    fatal_reasons: list[str] = []
    fatal_called = Event()

    def fatal(reason: str) -> None:
        fatal_reasons.append(reason)
        fatal_called.set()

    strategy.set_durable_io_fatal_handler(fatal)
    strategy._start_durable_io_lane()
    writer_started = Event()
    writer_release = Event()

    def blocking_write(_payload: Any) -> None:
        writer_started.set()
        writer_release.wait(timeout=5.0)

    strategy._write_entry_protection_stash = blocking_write  # type: ignore[method-assign]
    try:
        assert strategy._queue_entry_protection_stash_persist()
        assert writer_started.wait(timeout=1.0)
        assert fatal_called.wait(timeout=1.0)
        assert len(fatal_reasons) == 1
        assert "task timeout" in fatal_reasons[0]
    finally:
        writer_release.set()
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)
