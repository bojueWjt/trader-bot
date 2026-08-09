from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from typing import Any
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from strategy.intent_execution_planner import ManagementPlan, OrderPlan
from strategy.intent_execution_strategy import (
    IntentExecutionStrategy,
    IntentExecutionStrategyConfig,
)


class _ProbeStrategy(IntentExecutionStrategy):
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self.submitted: list[str] = []
        self.scheduled: list[str] = []
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id="account-a",
                node_id="node-a",
                trading_state="ACTIVE",
            )
        )

    def _protection_stash_path(self) -> str:
        return str(self._state_dir / self._PROTECTION_STASH_FILENAME)

    def _submit_order_plan(self, plan: OrderPlan) -> bool:
        self.submitted.append(plan.client_order_id)
        return True

    def _schedule_protection_sync(
        self,
        intent_key: str,
        delay_seconds: float | None = None,
    ) -> None:
        del delay_seconds
        self.scheduled.append(intent_key)


class _SmallExternalQueueProbeStrategy(_ProbeStrategy):
    _DURABLE_IO_QUEUE_CAPACITY = 1


def test_entry_fsync_continuation_rechecks_live_halt_before_opening(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    state = "ACTIVE"
    strategy.set_trading_state_getter(lambda: state)
    strategy._start_durable_io_lane()
    started = Event()
    release = Event()
    original_write = strategy._write_entry_protection_stash
    order = _order_plan(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "entry-after-halt",
    )

    def blocked_write(payload: Any) -> None:
        started.set()
        release.wait(timeout=5.0)
        original_write(payload)

    strategy._write_entry_protection_stash = blocked_write  # type: ignore[method-assign]
    strategy._entry_protection_stash[str(order.intent_id)] = {
        "instrument_id": order.instrument_id,
        "entry_side": order.side,
    }
    try:
        assert strategy._queue_entry_protection_stash_persist(
            continuation={
                "kind": "entry_submit",
                "plan": order,
                "source_intent": False,
                "protection_preimage": {},
            }
        )
        assert started.wait(timeout=1.0)
        state = "HALTED"
        release.set()
        assert strategy.wait_for_durable_io(timeout_seconds=1.0)
        _drain_until_idle(strategy)

        assert strategy.submitted == []
        assert str(order.intent_id) not in strategy._processed_intent_ids
        assert any(
            denial.reason == "trading_not_active"
            for denial in strategy.denials
        )
    finally:
        release.set()
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def test_stale_continuation_does_not_execute_against_latest_owner_snapshot(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    strategy._start_durable_io_lane()
    started = Event()
    release = Event()
    original_write = strategy._write_entry_protection_stash
    first = _order_plan(
        UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        "entry-first-owner",
    )
    latest = _order_plan(
        UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        "entry-latest-owner",
    )
    writes = 0

    def blocked_first_write(payload: Any) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            started.set()
            release.wait(timeout=5.0)
        original_write(payload)

    strategy._write_entry_protection_stash = blocked_first_write  # type: ignore[method-assign]
    strategy._entry_protection_stash[str(first.intent_id)] = {
        "instrument_id": first.instrument_id,
        "entry_side": first.side,
        "owner": "first",
    }
    try:
        assert strategy._queue_entry_protection_stash_persist(
            continuation={
                "kind": "entry_submit",
                "plan": first,
                "source_intent": False,
                "protection_preimage": {},
            }
        )
        assert started.wait(timeout=1.0)
        strategy._entry_protection_stash.clear()
        strategy._entry_protection_stash[str(latest.intent_id)] = {
            "instrument_id": latest.instrument_id,
            "entry_side": latest.side,
            "owner": "latest",
        }
        assert strategy._queue_entry_protection_stash_persist(
            continuation={
                "kind": "entry_submit",
                "plan": latest,
                "source_intent": False,
                "protection_preimage": {},
            }
        )
        release.set()
        assert strategy.wait_for_durable_io(timeout_seconds=1.0)
        _drain_until_idle(strategy)
        persisted = json.loads(
            Path(strategy._protection_stash_path()).read_text(
                encoding="utf-8"
            )
        )

        assert sorted(persisted) == [str(latest.intent_id)]
        assert strategy.submitted == ["entry-latest-owner"]
        assert str(first.intent_id) not in strategy._processed_intent_ids
        assert str(latest.intent_id) in strategy._processed_intent_ids
    finally:
        release.set()
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def test_worker_failure_is_applied_by_actor_mailbox_thread(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    actor_thread_id = get_ident()
    denial_threads: list[int] = []
    fatal_threads: list[int] = []
    original_record = strategy._record_denial

    def record(denial: Any) -> None:
        denial_threads.append(get_ident())
        original_record(denial)

    def failing_write(_payload: Any) -> None:
        raise RuntimeError("disk failed")

    strategy._record_denial = record  # type: ignore[method-assign]
    strategy.set_durable_io_fatal_handler(
        lambda _reason: fatal_threads.append(get_ident())
    )
    strategy._write_entry_protection_stash = failing_write  # type: ignore[method-assign]
    strategy._entry_protection_stash["intent"] = {"state": "pending"}
    strategy._start_durable_io_lane()
    try:
        assert strategy._queue_entry_protection_stash_persist()
        assert strategy.wait_for_durable_io(timeout_seconds=1.0)
        assert denial_threads == []
        assert fatal_threads == []

        strategy.drain_durable_io_mailbox()

        assert denial_threads == [actor_thread_id]
        assert fatal_threads == [actor_thread_id]
    finally:
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def test_large_stash_enqueue_stays_within_actor_callback_budget(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    strategy._start_durable_io_lane()
    started = Event()
    release = Event()

    def blocked_write(_payload: Any) -> None:
        started.set()
        release.wait(timeout=5.0)

    strategy._write_entry_protection_stash = blocked_write  # type: ignore[method-assign]
    _seed_large_stash(strategy)
    try:
        started_at = time.perf_counter()
        accepted = strategy._queue_entry_protection_stash_persist()
        elapsed_ms = (time.perf_counter() - started_at) * 1000

        assert accepted is True
        assert elapsed_ms < 10.0
        assert started.wait(timeout=1.0)
    finally:
        release.set()
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def test_continuation_drain_enforces_item_and_time_budgets(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    strategy._start_durable_io_lane()
    for index in range(8):
        strategy._entry_protection_stash[f"intent-{index}"] = {
            "state": index,
        }
        assert strategy._queue_entry_protection_stash_persist(
            continuation={
                "kind": "protection_schedule",
                "intent_key": f"intent-{index}",
            }
        )
    assert strategy.wait_for_durable_io(timeout_seconds=1.0)
    try:
        drained = strategy.drain_durable_io_mailbox(
            max_results=16,
            max_items=1,
            time_budget_ms=5.0,
        )

        assert drained == 1
        assert len(strategy.scheduled) <= 1
    finally:
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def test_exchange_cancel_continuation_runs_on_worker_lane(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    actor_thread_id = get_ident()
    adapter_threads: list[int] = []
    owner_intent_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    management_intent_id = UUID(
        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    )
    plan = ManagementPlan(
        intent_id=management_intent_id,
        action="replace_take_profits",
        instrument_id="BTCUSDT-PERP.BINANCE",
        target_position_id="BTCUSDT-PERP.BINANCE-LONG",
        target_position_side="LONG",
        cancel_order_ids=("old-tp",),
        orders=(),
        disable_take_profits=True,
        authorization={
            "authorized_by_type": "user",
            "authorized_by_id": "risk-admin",
            "source_message_id": "management-1",
            "parent_intent_id": str(owner_intent_id),
        },
    )

    class Adapter:
        def cancel(self, _operation: str, _request: Any) -> Any:
            adapter_threads.append(get_ident())
            return SimpleNamespace(terminal_status="CANCELED")

    class Mirror:
        def find_order(
            self,
            _instrument_id: str,
            _client_order_id: str,
        ) -> Any:
            return SimpleNamespace(
                account_id="account-a",
                symbol="BTCUSDT",
                position_side="LONG",
                order_kind="regular",
                venue_order_id="123",
            )

    strategy.set_exchange_cancel_adapter(Adapter(), Mirror())
    strategy._entry_protection_stash[str(owner_intent_id)] = {
        "instrument_id": plan.instrument_id,
        "entry_side": "BUY",
        "take_profit_parent_intent_id": str(owner_intent_id),
        "take_profit_tombstone": {
            "state": "cancel_pending",
            "parent_intent_id": str(owner_intent_id),
            "authorized_by_type": "user",
            "authorized_by_id": "risk-admin",
            "source_message_id": "management-1",
            "pending_cancel_ids": ["old-tp"],
        },
        "protection_roles": {},
        "tp_consumed": {},
        "pending_cancel_ids": (),
    }
    strategy._start_durable_io_lane()
    try:
        assert strategy._queue_entry_protection_stash_persist(
            continuation={
                "kind": "take_profit_disable_cancel",
                "plan": plan,
                "source_intent": False,
                "cancel_order_ids": ("old-tp",),
            }
        )
        _drain_until_idle(strategy)

        assert adapter_threads
        assert all(thread_id != actor_thread_id for thread_id in adapter_threads)
        assert str(plan.intent_id) in strategy._processed_intent_ids
    finally:
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def test_recoverable_external_errors_keep_active_opening_admission(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    opening_plan = _order_plan(
        UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
        "entry-after-soft-error",
    )

    class Adapter:
        def cancel(self, _operation: str, _request: Any) -> Any:
            raise TimeoutError("temporary cancel timeout")

    class Mirror:
        def refresh(self) -> None:
            raise ConnectionError("temporary refresh failure")

        def find_order(
            self,
            _instrument_id: str,
            _client_order_id: str,
        ) -> Any:
            return SimpleNamespace(
                account_id="account-a",
                symbol="BTCUSDT",
                position_side="LONG",
                order_kind="regular",
                venue_order_id="123",
            )

    strategy.set_exchange_cancel_adapter(Adapter(), Mirror())
    strategy._start_durable_io_lane()
    try:
        assert strategy._queue_exchange_refresh()
        assert strategy._queue_exchange_cancel(
            instrument_id=opening_plan.instrument_id,
            cancel_order_ids=("old-order",),
        )
        assert strategy.external_io_cleanup_worker().submit(object())
        _drain_until_idle(strategy)

        denial_reasons = {denial.reason for denial in strategy.denials}
        assert "exchange_state_refresh_failed" in denial_reasons
        assert "order_cancel_failed" in denial_reasons
        assert "strategy_external_io_degraded" in denial_reasons
        assert strategy.durable_io_halted_reason == ""
        assert strategy._opening_side_effect_allowed(opening_plan) is True
    finally:
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)
        strategy.external_io_cleanup_worker().stop(
            timeout_seconds=1.0
        )


def test_external_queue_backpressure_is_soft_degraded(
    tmp_path: Path,
) -> None:
    strategy = _SmallExternalQueueProbeStrategy(tmp_path)
    first_started = Event()
    first_release = Event()
    second_started = Event()
    second_release = Event()
    refresh_calls = 0

    class Mirror:
        def refresh(self) -> None:
            nonlocal refresh_calls
            refresh_calls += 1
            if refresh_calls == 1:
                first_started.set()
                first_release.wait(timeout=5.0)
                return
            second_started.set()
            second_release.wait(timeout=5.0)

    strategy.set_exchange_cancel_adapter(False, Mirror())
    strategy._start_durable_io_lane()
    try:
        assert strategy._queue_exchange_refresh()
        assert first_started.wait(timeout=1.0)
        assert strategy._queue_exchange_refresh()
        assert strategy._queue_exchange_refresh() is False
        strategy.drain_durable_io_mailbox()

        denial_reasons = {denial.reason for denial in strategy.denials}
        assert "strategy_external_io_backpressure" in denial_reasons
        assert strategy.durable_io_halted_reason == ""

        first_release.set()
        assert second_started.wait(timeout=1.0)
        strategy.drain_durable_io_mailbox()
        second_release.set()
        assert strategy.wait_for_durable_io(timeout_seconds=1.0)
        strategy.drain_durable_io_mailbox()
    finally:
        first_release.set()
        second_release.set()
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)
        strategy.external_io_cleanup_worker().stop(
            timeout_seconds=1.0
        )


def test_external_submit_after_session_stopped_is_hard(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    strategy._strategy_stopping = True
    try:
        assert strategy._queue_exchange_refresh() is False
        strategy.drain_durable_io_mailbox()

        assert "session is stopped" in strategy.durable_io_halted_reason
    finally:
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)
        strategy.external_io_cleanup_worker().stop(
            timeout_seconds=1.0
        )


def _drain_until_idle(strategy: IntentExecutionStrategy) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        strategy.wait_for_durable_io(timeout_seconds=0.05)
        drained = strategy.drain_durable_io_mailbox(
            max_results=32,
            max_items=32,
            time_budget_ms=8.0,
        )
        worker = strategy.durable_io_cleanup_worker().snapshot()
        if drained == 0 and worker.queue_depth == 0 and not worker.in_flight:
            return
        time.sleep(0.001)
    raise AssertionError("strategy durable lane did not become idle")


def _order_plan(intent_id: UUID, client_order_id: str) -> OrderPlan:
    return OrderPlan(
        intent_id=intent_id,
        client_order_id=client_order_id,
        tags=(f"intent_id={intent_id}",),
        instrument_id="BTCUSDT-PERP.BINANCE",
        side="BUY",
        order_type="LIMIT",
        quantity="0.1",
        price="26000",
        time_in_force="GTC",
    )


def _seed_large_stash(strategy: IntentExecutionStrategy) -> None:
    terminal_event = {
        "event_type": "OrderRejected",
        "reason": "would immediately trigger " + ("x" * 96),
        "error_code": "-2021",
        "client_order_id": "B" + ("a" * 34),
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "role": "take_profit",
        "tp_price": "28000.00",
        "protection_revision": 8,
        "observed_at": "2026-08-09T12:00:00+00:00",
    }
    for index in range(256):
        intent_id = f"{index:032x}"
        strategy._entry_protection_stash[intent_id] = {
            "stop_loss": "25000",
            "take_profits": tuple(
                str(28000 + tier) for tier in range(8)
            ),
            "instrument_id": f"ASSET{index}USDT-PERP.BINANCE",
            "entry_side": "BUY",
            "entry_tags": tuple(
                f"tag-{tag}" for tag in range(12)
            ),
            "protection_ids": tuple(
                f"B{index:032x}{sequence:02d}"
                for sequence in range(11, 20)
            ),
            "protection_terminal_events": [
                dict(terminal_event) for _ in range(32)
            ],
            "protection_roles": {
                f"B{index:032x}{sequence:02d}": {
                    "role": "take_profit",
                    "quantity": "0.1",
                    "tp_price": str(28000 + sequence),
                    "submitted_at": "2026-08-09T12:00:00+00:00",
                    "tags": tuple(
                        f"tag-{tag}" for tag in range(12)
                    ),
                }
                for sequence in range(11, 20)
            },
        }
