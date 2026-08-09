from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
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

from data_client.durable_intent_inbox import JsonDurableIntentInbox
from execution_domain.contracts import (
    ApprovedTradeIntentV1,
    IntentAction,
    RiskBudget,
)
from strategy.intent_execution_planner import (
    ManagementPlan,
    OrderPlan,
    encode_client_order_id,
)
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


class _ResubmitCrashProbeStrategy(_ProbeStrategy):
    def __init__(
        self,
        state_dir: Path,
        *,
        zone: bool = False,
    ) -> None:
        self.zone = zone
        self.submit_counts: dict[str, int] = {}
        super().__init__(state_dir)

    def _submit_order_plan(self, plan: OrderPlan) -> bool:
        client_order_id = str(plan.client_order_id)
        self.submit_counts[client_order_id] = (
            self.submit_counts.get(client_order_id, 0) + 1
        )
        self.submitted.append(client_order_id)
        return True

    def _handle_intent(
        self,
        intent: Any,
        *,
        exchange_state_ready: bool = False,
        exchange_refresh_degraded: bool = False,
        opening_resubmit_authorized: bool = False,
    ) -> None:
        del exchange_state_ready, exchange_refresh_degraded
        if not opening_resubmit_authorized:
            return
        intent_id = UUID(str(intent.intent_id))
        if self.zone:
            plans = tuple(
                _order_plan(
                    intent_id,
                    encode_client_order_id(
                        intent_id,
                        sequence=sequence,
                    ),
                )
                for sequence in range(1, 4)
            )
            self._continue_zone_submit(
                {
                    "plans": plans,
                    "source_intent": intent,
                    "preserve_resubmitting_receipt": True,
                }
            )
            return
        plan = _order_plan(
            intent_id,
            encode_client_order_id(intent_id, sequence=1),
        )
        self._continue_entry_submit(
            {
                "plan": plan,
                "source_intent": intent,
                "preserve_resubmitting_receipt": True,
            }
        )


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


def test_independent_opening_continuations_each_execute_exact_version(
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
            },
            changed_intent_keys=(
                str(first.intent_id),
                str(latest.intent_id),
            ),
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
        assert strategy.submitted == [
            "entry-first-owner",
            "entry-latest-owner",
        ]
        assert str(first.intent_id) in strategy._processed_intent_ids
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


def test_superseded_version_for_same_logical_intent_runs_latest_only(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    strategy._start_durable_io_lane()
    first_started = Event()
    first_release = Event()
    original_write = strategy._write_entry_protection_stash
    intent_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    first = _order_plan(intent_id, "entry-version-1")
    latest = _order_plan(intent_id, "entry-version-2")
    writes = 0

    def blocked_first_write(payload: Any) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            first_started.set()
            first_release.wait(timeout=5.0)
        original_write(payload)

    strategy._write_entry_protection_stash = blocked_first_write  # type: ignore[method-assign]
    try:
        assert strategy._queue_entry_protection_stash_persist(
            continuation={
                "kind": "entry_submit",
                "plan": first,
                "source_intent": False,
                "protection_preimage": {},
            }
        )
        assert first_started.wait(timeout=1.0)
        assert strategy._queue_entry_protection_stash_persist(
            continuation={
                "kind": "entry_submit",
                "plan": latest,
                "source_intent": False,
                "protection_preimage": {},
            }
        )
        first_release.set()
        _drain_until_idle(strategy)

        assert strategy.submitted == ["entry-version-2"]
    finally:
        first_release.set()
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def test_immediate_tp_fallback_is_not_superseded_by_protection_schedule(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    intent_id = UUID("d1111111-1111-4111-8111-111111111111")
    intent_key = str(intent_id)
    fallback_id = encode_client_order_id(
        intent_id,
        sequence=91,
    )
    source_client_order_id = encode_client_order_id(
        intent_id,
        sequence=11,
    )
    fallback_plan = OrderPlan(
        intent_id=intent_id,
        client_order_id=fallback_id,
        tags=(
            f"intent_id={intent_id}",
            "lifecycle_role=take_profit",
        ),
        instrument_id="BTCUSDT-PERP.BINANCE",
        side="SELL",
        order_type="MARKET",
        quantity="0.1",
        price=None,
        time_in_force="GTC",
        reduce_only=True,
    )
    strategy._entry_protection_stash[intent_key] = {
        "instrument_id": fallback_plan.instrument_id,
        "entry_side": "BUY",
        "protection_ids": (source_client_order_id,),
        "protection_roles": {},
        "pending_cancel_ids": (),
        "protection_revision": 1,
        "tp_market_fallbacks": {
            fallback_id: {
                "source_client_order_id": source_client_order_id,
                "client_order_id": fallback_id,
                "tp_price": "27000",
                "quantity": "0.1",
                "remaining_quantity": "0.1",
                "side": "SELL",
                "status": "submitting",
                "event_key": (
                    f"{intent_key}:{source_client_order_id}:"
                    f"{fallback_id}"
                ),
                "event_sent": False,
            }
        },
    }
    first_started = Event()
    first_release = Event()
    original_write = strategy._write_entry_protection_stash
    writes = 0

    def blocked_first_write(payload: Any) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            first_started.set()
            first_release.wait(timeout=5.0)
        original_write(payload)

    strategy._write_entry_protection_stash = blocked_first_write  # type: ignore[method-assign]
    strategy._start_durable_io_lane()
    try:
        assert strategy._queue_entry_protection_stash_persist(
            continuation={
                "kind": "immediate_tp_market_fallback",
                "intent_key": intent_key,
                "fallback_id": fallback_id,
                "source_client_order_id": source_client_order_id,
                "tp_price": "27000",
                "event_key": (
                    f"{intent_key}:{source_client_order_id}:"
                    f"{fallback_id}"
                ),
                "plan": fallback_plan,
            },
            changed_intent_keys=intent_key,
        )
        assert first_started.wait(timeout=1.0)
        assert strategy._queue_entry_protection_stash_persist(
            continuation={
                "kind": "protection_schedule",
                "intent_key": intent_key,
            },
            changed_intent_keys=intent_key,
        )

        first_release.set()
        _drain_until_idle(strategy)

        assert strategy.submitted == [fallback_id]
        assert strategy.scheduled == [intent_key]
        fallback_state = strategy._entry_protection_stash[
            intent_key
        ]["tp_market_fallbacks"][fallback_id]
        assert fallback_state["status"] == "submitted"
        assert strategy.durable_io_halted_reason == ""
    finally:
        first_release.set()
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)
        strategy.external_io_cleanup_worker().stop(
            timeout_seconds=1.0
        )


def test_opening_without_protection_waits_for_durable_prepare(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    strategy._start_durable_io_lane()
    writer_started = Event()
    writer_release = Event()
    original_write = strategy._write_entry_protection_stash
    plan = _authorized_order_plan(
        UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        "entry-no-protection",
    )
    intent = SimpleNamespace(order_plan={})

    def blocked_write(payload: Any) -> None:
        writer_started.set()
        writer_release.wait(timeout=5.0)
        original_write(payload)

    strategy._write_entry_protection_stash = blocked_write  # type: ignore[method-assign]
    try:
        assert strategy._stash_entry_protection(
            intent,
            plan,
            continuation={
                "kind": "entry_submit",
                "plan": plan,
                "source_intent": False,
                "protection_preimage": {},
            },
        )
        assert writer_started.wait(timeout=1.0)
        assert strategy.submitted == []

        writer_release.set()
        _drain_until_idle(strategy)
        assert strategy.submitted == ["entry-no-protection"]
    finally:
        writer_release.set()
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)


def test_zone_partial_submit_stays_dispatched_without_canceling_live_rungs(
    tmp_path: Path,
) -> None:
    intent_id = UUID("e1111111-1111-4111-8111-111111111111")
    source_intent = SimpleNamespace(
        intent_id=intent_id,
        instrument_id="BTCUSDT-PERP.BINANCE",
    )
    plans = tuple(
        OrderPlan(
            **{
                **_authorized_order_plan(
                    intent_id,
                    encode_client_order_id(
                        intent_id,
                        sequence=sequence,
                    ),
                ).__dict__,
                "price": str(26000 - sequence),
            }
        )
        for sequence in range(1, 4)
    )
    receipts: list[tuple[Any, str, str]] = []

    class PartialSubmitStrategy(_ProbeStrategy):
        def __init__(self, state_dir: Path) -> None:
            self.canceled: list[str] = []
            super().__init__(state_dir)

        def _submit_order_plan(self, plan: OrderPlan) -> bool:
            self.submitted.append(plan.client_order_id)
            return len(self.submitted) != 2

        def _cancel_order_by_client_order_id(
            self,
            _instrument_id: str,
            client_order_id: str,
        ) -> bool:
            self.canceled.append(client_order_id)
            return True

    strategy = PartialSubmitStrategy(tmp_path)
    strategy.set_intent_receipt_handler(
        lambda receipt_intent_id, status, detail: (
            receipts.append(
                (receipt_intent_id, status, detail)
            )
            or True
        )
    )

    assert strategy._continue_zone_submit(
        {
            "plans": plans,
            "source_intent": source_intent,
        }
    )

    assert strategy.submitted == [
        plans[0].client_order_id,
        plans[1].client_order_id,
    ]
    assert strategy.canceled == []
    assert receipts == [(intent_id, "DISPATCHED", "")]
    assert str(intent_id) in strategy._processed_intent_ids
    assert str(intent_id) in (
        strategy._pending_opening_reconciliations
    )
    assert any(
        denial.reason == "zone_partial_submit"
        for denial in strategy.denials
    )


def test_zone_partial_submit_empty_mirror_does_not_resubmit(
    tmp_path: Path,
) -> None:
    intent_id = UUID("e2222222-2222-4222-8222-222222222222")
    source_intent = SimpleNamespace(
        intent_id=intent_id,
        instrument_id="BTCUSDT-PERP.BINANCE",
    )

    class Mirror:
        def orders_for_instrument(
            self,
            _instrument_id: str,
        ) -> tuple[Any, ...]:
            return ()

    strategy = _ProbeStrategy(tmp_path)
    strategy.set_exchange_cancel_adapter(False, Mirror())
    strategy._defer_opening_reconciliation(
        source_intent,
        allow_resubmit=False,
    )

    strategy._retry_pending_opening_reconciliations()

    assert strategy.submitted == []
    assert str(intent_id) in (
        strategy._pending_opening_reconciliations
    )
    assert (
        strategy._opening_reconciliation_resubmit_allowed[
            str(intent_id)
        ]
        is False
    )


def test_dispatched_replay_reconciles_exchange_before_submit(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    intent_id = UUID("f1111111-1111-4111-8111-111111111111")
    client_order_id = encode_client_order_id(
        intent_id,
        sequence=1,
    )
    receipts: list[tuple[Any, str, str]] = []

    class Mirror:
        def refresh(self) -> None:
            return None

        def orders_for_instrument(
            self,
            _instrument_id: str,
        ) -> tuple[Any, ...]:
            return (
                SimpleNamespace(
                    client_order_id=client_order_id,
                    instrument_id="BTCUSDT-PERP.BINANCE",
                ),
            )

    strategy.set_exchange_cancel_adapter(False, Mirror())
    strategy.set_intent_receipt_status_getter(
        lambda _intent_id: "DISPATCHED"
    )
    strategy.set_intent_receipt_handler(
        lambda replay_intent_id, status, detail: (
            receipts.append(
                (replay_intent_id, status, detail)
            )
            or True
        )
    )
    replayed_intent = SimpleNamespace(
        intent_id=intent_id,
        instrument_id="BTCUSDT-PERP.BINANCE",
        action="open_position",
        order_plan={
            "type": "market",
            "side": "buy",
            "quantity": "0.1",
        },
    )

    strategy._handle_intent(replayed_intent)

    assert strategy.submitted == []
    assert receipts == [
        (
            intent_id,
            "CONFIRMED",
            f"reconciled:{client_order_id}",
        )
    ]
    assert str(intent_id) in strategy._processed_intent_ids


def test_dispatched_replay_refresh_timeout_retries_without_rejection(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    intent_id = UUID("f3333333-3333-4333-8333-333333333333")
    client_order_id = encode_client_order_id(
        intent_id,
        sequence=1,
    )
    receipts: list[tuple[Any, str, str]] = []
    refresh_calls = 0

    class Mirror:
        def refresh(self) -> None:
            nonlocal refresh_calls
            refresh_calls += 1
            if refresh_calls == 1:
                raise TimeoutError("temporary mirror timeout")

        def orders_for_instrument(
            self,
            _instrument_id: str,
        ) -> tuple[Any, ...]:
            return (
                SimpleNamespace(
                    client_order_id=client_order_id,
                    instrument_id="BTCUSDT-PERP.BINANCE",
                ),
            )

    strategy.set_exchange_cancel_adapter(False, Mirror())
    strategy.set_intent_receipt_status_getter(
        lambda _intent_id: "DISPATCHED"
    )
    strategy.set_intent_receipt_handler(
        lambda replay_intent_id, status, detail: (
            receipts.append(
                (replay_intent_id, status, detail)
            )
            or True
        )
    )
    replayed_intent = SimpleNamespace(
        intent_id=intent_id,
        instrument_id="BTCUSDT-PERP.BINANCE",
        action="open_position",
        order_plan={
            "type": "market",
            "side": "buy",
            "quantity": "0.1",
        },
    )

    strategy._handle_intent(replayed_intent)

    assert strategy.submitted == []
    assert receipts == []
    assert str(intent_id) in (
        strategy._pending_opening_reconciliations
    )

    strategy._on_exchange_state_timer()

    assert strategy.submitted == []
    assert receipts == [
        (
            intent_id,
            "CONFIRMED",
            f"reconciled:{client_order_id}",
        )
    ]
    assert strategy._pending_opening_reconciliations == {}
    assert not any(
        status == "REJECTED"
        for _receipt_id, status, _detail in receipts
    )


def test_dispatched_filled_opening_uses_historical_evidence(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    intent_id = UUID("f4444444-4444-4444-8444-444444444444")
    client_order_id = encode_client_order_id(
        intent_id,
        sequence=1,
    )
    receipts: list[tuple[Any, str, str]] = []

    class Mirror:
        def refresh(self) -> None:
            return None

        def orders_for_instrument(
            self,
            _instrument_id: str,
        ) -> tuple[Any, ...]:
            return ()

        def opening_execution_state(
            self,
            requested_client_order_id: str,
        ) -> Any:
            assert requested_client_order_id == client_order_id
            return SimpleNamespace(
                client_order_id=client_order_id,
                instrument_id="BTCUSDT-PERP.BINANCE",
                state="confirmed_executed",
            )

    strategy.set_exchange_cancel_adapter(False, Mirror())
    strategy.set_intent_receipt_status_getter(
        lambda _intent_id: "DISPATCHED"
    )
    strategy.set_intent_receipt_handler(
        lambda replay_intent_id, status, detail: (
            receipts.append(
                (replay_intent_id, status, detail)
            )
            or True
        )
    )
    replayed_intent = _replayed_opening_intent(intent_id)

    strategy._handle_intent(replayed_intent)

    assert strategy.submitted == []
    assert receipts == [
        (
            intent_id,
            "CONFIRMED",
            f"historical_execution:{client_order_id}",
        )
    ]
    assert str(intent_id) in strategy._processed_intent_ids


def test_authoritative_absence_requires_separate_durable_authority(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    intent_id = UUID("f5555555-5555-4555-8555-555555555555")
    client_order_id = encode_client_order_id(
        intent_id,
        sequence=1,
    )

    class Mirror:
        def orders_for_instrument(
            self,
            _instrument_id: str,
        ) -> tuple[Any, ...]:
            return ()

        def opening_execution_state(
            self,
            requested_client_order_id: str,
        ) -> Any:
            assert requested_client_order_id == client_order_id
            return SimpleNamespace(
                client_order_id=client_order_id,
                instrument_id="BTCUSDT-PERP.BINANCE",
                state="definitively_absent",
            )

    intent = _replayed_opening_intent(intent_id)
    strategy.set_exchange_cancel_adapter(False, Mirror())

    first = strategy._reconcile_existing_opening(
        intent,
        exchange_state_ready=True,
        require_historical_evidence=True,
    )
    second = strategy._reconcile_existing_opening(
        intent,
        exchange_state_ready=True,
        require_historical_evidence=True,
    )

    assert first is False
    assert second is False


@pytest.mark.parametrize("zone", [False, True])
def test_resubmit_success_keeps_receipt_resubmitting_across_restart(
    tmp_path: Path,
    zone: bool,
) -> None:
    intent_id = UUID("f5757575-5757-4575-8575-575757575757")
    intent = _durable_replayed_opening_intent(
        intent_id,
        zone=zone,
    )
    inbox_path = tmp_path / "intent-inbox.json"
    inbox = _seed_dispatched_receipt(inbox_path, intent)
    strategy = _ResubmitCrashProbeStrategy(
        tmp_path,
        zone=zone,
    )
    _wire_resubmit_receipt(strategy, inbox)
    strategy._start_durable_io_lane()
    try:
        assert not strategy._opening_reconciliation_allows_execution(
            intent,
            receipt_status="DISPATCHED",
            reconciled=False,
            resubmit_authorized=False,
        )
        assert strategy.wait_for_durable_io(
            timeout_seconds=1.0
        )
        _drain_until_idle(strategy)

        restarted_inbox = JsonDurableIntentInbox(inbox_path)
        assert restarted_inbox.status(
            intent_id
        ) == "RESUBMITTING"
        expected_sequences = (1, 2, 3) if zone else (1,)
        expected_ids = tuple(
            encode_client_order_id(
                intent_id,
                sequence=sequence,
            )
            for sequence in expected_sequences
        )
        assert strategy.submit_counts == {
            client_order_id: 1
            for client_order_id in expected_ids
        }
    finally:
        strategy.durable_io_cleanup_worker().stop(
            timeout_seconds=1.0
        )
        strategy.external_io_cleanup_worker().stop(
            timeout_seconds=1.0
        )

    restarted = _ResubmitCrashProbeStrategy(
        tmp_path / "restart",
        zone=zone,
    )
    restarted.set_intent_receipt_status_getter(
        restarted_inbox.status
    )

    assert not restarted._opening_reconciliation_allows_execution(
        intent,
        receipt_status="RESUBMITTING",
        reconciled=False,
        resubmit_authorized=False,
    )
    assert restarted.submit_counts == {}
    assert JsonDurableIntentInbox(inbox_path).status(
        intent_id
    ) == "RESUBMITTING"


def test_crash_after_resubmitting_persist_before_authority_never_replays(
    tmp_path: Path,
) -> None:
    intent_id = UUID("f5858585-5858-4585-8585-585858585858")
    intent = _durable_replayed_opening_intent(intent_id)
    inbox_path = tmp_path / "intent-inbox.json"
    inbox = _seed_dispatched_receipt(inbox_path, intent)
    strategy = _ResubmitCrashProbeStrategy(tmp_path)
    _wire_resubmit_receipt(strategy, inbox)
    strategy._start_durable_io_lane()

    assert not strategy._opening_reconciliation_allows_execution(
        intent,
        receipt_status="DISPATCHED",
        reconciled=False,
        resubmit_authorized=False,
    )
    assert strategy.durable_io_cleanup_worker().wait_empty(
        timeout_seconds=1.0
    )
    assert JsonDurableIntentInbox(inbox_path).status(
        intent_id
    ) == "RESUBMITTING"
    assert strategy.submit_counts == {}
    strategy.durable_io_cleanup_worker().stop(
        timeout_seconds=1.0
    )
    strategy.external_io_cleanup_worker().stop(
        timeout_seconds=1.0
    )

    restarted_inbox = JsonDurableIntentInbox(inbox_path)
    restarted = _ResubmitCrashProbeStrategy(
        tmp_path / "restart"
    )
    restarted.set_intent_receipt_status_getter(
        restarted_inbox.status
    )

    assert not restarted._opening_reconciliation_allows_execution(
        intent,
        receipt_status="RESUBMITTING",
        reconciled=False,
        resubmit_authorized=False,
    )
    assert restarted.submit_counts == {}


def test_dispatched_opening_unknown_evidence_remains_pending(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    intent_id = UUID("f6666666-6666-4666-8666-666666666666")
    client_order_id = encode_client_order_id(
        intent_id,
        sequence=1,
    )
    receipts: list[tuple[Any, str, str]] = []

    class Mirror:
        def refresh(self) -> None:
            return None

        def orders_for_instrument(
            self,
            _instrument_id: str,
        ) -> tuple[Any, ...]:
            return ()

        def opening_execution_state(
            self,
            requested_client_order_id: str,
        ) -> Any:
            assert requested_client_order_id == client_order_id
            return SimpleNamespace(
                client_order_id=client_order_id,
                instrument_id=None,
                state="unknown",
            )

    strategy.set_exchange_cancel_adapter(False, Mirror())
    strategy.set_intent_receipt_status_getter(
        lambda _intent_id: "DISPATCHED"
    )
    strategy.set_intent_receipt_handler(
        lambda replay_intent_id, status, detail: (
            receipts.append(
                (replay_intent_id, status, detail)
            )
            or True
        )
    )
    replayed_intent = _replayed_opening_intent(intent_id)

    strategy._handle_intent(replayed_intent)

    assert strategy.submitted == []
    assert receipts == []
    assert str(intent_id) in (
        strategy._pending_opening_reconciliations
    )
    assert any(
        denial.reason == "opening_execution_evidence_unknown"
        for denial in strategy.denials
    )


def test_soft_opening_refresh_failure_keeps_new_intent_admitted(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    intent = _replayed_opening_intent(
        UUID("f6767676-6767-4676-8676-676767676767")
    )
    admitted: list[tuple[Any, dict[str, Any]]] = []
    strategy.set_intent_receipt_status_getter(
        lambda _intent_id: "PREPARED"
    )
    strategy._handle_intent = (  # type: ignore[method-assign]
        lambda replayed, **kwargs: admitted.append(
            (replayed, kwargs)
        )
    )

    strategy._run_protection_continuation(
        {
            "kind": "intent_exchange_refresh_failed",
            "intent": intent,
        }
    )

    assert admitted == [
        (
            intent,
            {"exchange_refresh_degraded": True},
        )
    ]
    assert strategy.durable_io_halted_reason == ""


def test_stale_order_mirror_with_durable_evidence_keeps_new_intent_admitted(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    intent = _replayed_opening_intent(
        UUID("f6777777-6777-4777-8777-677777777777")
    )
    admitted: list[tuple[Any, dict[str, Any]]] = []

    class Mirror:
        def orders_are_fresh(self) -> bool:
            return False

    strategy.set_exchange_cancel_adapter(False, Mirror())
    strategy.set_intent_receipt_status_getter(
        lambda _intent_id: "PREPARED"
    )
    strategy._handle_intent = (  # type: ignore[method-assign]
        lambda replayed, **kwargs: admitted.append(
            (replayed, kwargs)
        )
    )

    strategy._run_protection_continuation(
        {
            "kind": "handle_intent_after_exchange_refresh",
            "intent": intent,
        }
    )

    assert admitted == [
        (
            intent,
            {"exchange_refresh_degraded": True},
        )
    ]
    assert strategy.durable_io_halted_reason == ""


def test_unknown_evidence_retry_is_deduplicated_and_backoff_advances(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    intent_id = UUID("f6868686-6868-4686-8686-686868686868")
    client_order_id = encode_client_order_id(
        intent_id,
        sequence=1,
    )

    class Mirror:
        def orders_for_instrument(
            self,
            _instrument_id: str,
        ) -> tuple[Any, ...]:
            return ()

        def opening_execution_state(
            self,
            _client_order_id: str,
        ) -> Any:
            return SimpleNamespace(
                client_order_id=client_order_id,
                instrument_id=None,
                state="unknown",
            )

    intent = _replayed_opening_intent(intent_id)
    strategy.set_exchange_cancel_adapter(False, Mirror())
    strategy.set_intent_receipt_status_getter(
        lambda _intent_id: "DISPATCHED"
    )
    strategy._defer_opening_reconciliation(intent)
    initial_attempt = strategy._opening_reconciliation_attempt

    for _ in range(20):
        strategy._retry_pending_opening_reconciliations()

    unknown_denials = [
        denial
        for denial in strategy.denials
        if denial.reason
        == "opening_execution_evidence_unknown"
    ]
    assert len(unknown_denials) == 1
    assert strategy._opening_reconciliation_attempt > (
        initial_attempt
    )
    assert strategy.durable_io_halted_reason == ""


def test_stop_cancels_opening_reconcile_without_hard_callback(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    canceled: list[str] = []
    strategy._cancel_clock_timer = (  # type: ignore[method-assign]
        canceled.append
    )
    strategy._opening_reconciliation_retry_scheduled = True

    strategy.on_stop()
    strategy._on_opening_reconciliation_alert()

    assert "intent-opening.reconcile" in canceled
    assert (
        strategy._opening_reconciliation_retry_scheduled
        is False
    )
    assert strategy.durable_io_halted_reason == ""


def test_order_accepted_confirms_durable_intent_receipt(
    tmp_path: Path,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    intent_id = UUID("f2222222-2222-4222-8222-222222222222")
    receipts: list[tuple[Any, str, str]] = []
    strategy.set_intent_receipt_handler(
        lambda confirmed_intent_id, status, detail: (
            receipts.append(
                (confirmed_intent_id, status, detail)
            )
            or True
        )
    )
    client_order_id = encode_client_order_id(
        intent_id,
        sequence=1,
    )

    strategy.on_order_accepted(
        SimpleNamespace(client_order_id=client_order_id)
    )

    assert receipts == [
        (
            intent_id,
            "CONFIRMED",
            f"order_accepted:{client_order_id}",
        )
    ]


@pytest.mark.parametrize("stash_size", [1024, 2048])
def test_large_stash_enqueue_stays_within_actor_callback_budget(
    tmp_path: Path,
    stash_size: int,
) -> None:
    strategy = _ProbeStrategy(tmp_path)
    started = Event()
    release = Event()

    def blocked_write(_payload: Any) -> None:
        started.set()
        release.wait(timeout=5.0)

    strategy._write_entry_protection_stash = blocked_write  # type: ignore[method-assign]
    _seed_large_stash(strategy, stash_size)
    strategy._start_durable_io_lane()
    changed_intent_key = f"{stash_size - 1:032x}"
    strategy._entry_protection_stash[
        changed_intent_key
    ]["protected_quantity"] = "0.2"
    try:
        started_at = time.perf_counter()
        accepted = strategy._queue_entry_protection_stash_persist(
            changed_intent_keys=changed_intent_key
        )
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


def test_external_cancel_results_wait_for_actor_drain_without_halt(
    tmp_path: Path,
) -> None:
    strategy = _SmallExternalQueueProbeStrategy(tmp_path)
    cancel_calls: list[str] = []
    second_cancel_called = Event()

    class Adapter:
        def cancel(self, _operation: str, request: Any) -> Any:
            client_order_id = str(request.client_order_id)
            cancel_calls.append(client_order_id)
            if len(cancel_calls) == 2:
                second_cancel_called.set()
            return SimpleNamespace(terminal_status="CANCELED")

    class Mirror:
        def find_order(
            self,
            _instrument_id: str,
            client_order_id: str,
        ) -> Any:
            return SimpleNamespace(
                account_id="account-a",
                symbol="BTCUSDT",
                position_side="LONG",
                order_kind="regular",
                venue_order_id=f"venue-{client_order_id}",
            )

    opening_plan = _order_plan(
        UUID("a1111111-1111-4111-8111-111111111111"),
        "entry-during-external-result-pressure",
    )
    strategy.set_exchange_cancel_adapter(Adapter(), Mirror())
    strategy._entry_protection_stash["durable-fill"] = {
        "state": "pending",
    }
    strategy._start_durable_io_lane()
    try:
        assert strategy._queue_entry_protection_stash_persist(
            changed_intent_keys="durable-fill"
        )
        assert strategy.durable_io_cleanup_worker().wait_empty(
            timeout_seconds=1.0
        )
        assert strategy._durable_io_mailbox.qsize() == 1

        assert strategy._queue_exchange_cancel(
            instrument_id=opening_plan.instrument_id,
            cancel_order_ids=("cancel-1",),
            success_continuation={
                "kind": "protection_schedule",
                "intent_key": "cancel-result-1",
            },
        )
        deadline = time.monotonic() + 1.0
        while (
            strategy._external_cancel_result_mailbox.qsize() < 1
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        assert strategy._external_cancel_result_mailbox.qsize() == 1

        assert strategy._queue_exchange_cancel(
            instrument_id=opening_plan.instrument_id,
            cancel_order_ids=("cancel-2",),
            success_continuation={
                "kind": "protection_schedule",
                "intent_key": "cancel-result-2",
            },
        )
        assert second_cancel_called.wait(timeout=1.0)
        time.sleep(0.06)

        assert strategy._opening_side_effect_allowed(opening_plan) is True
        assert strategy.drain_durable_io_mailbox(
            max_results=1
        ) == 1
        _drain_until_idle(strategy)

        assert cancel_calls == ["cancel-1", "cancel-2"]
        assert strategy.scheduled == [
            "cancel-result-1",
            "cancel-result-2",
        ]
        denial_reasons = {
            denial.reason for denial in strategy.denials
        }
        assert "strategy_external_io_backpressure" in denial_reasons
        assert strategy.durable_io_halted_reason == ""
        assert strategy._opening_side_effect_allowed(opening_plan) is True
    finally:
        strategy.durable_io_cleanup_worker().stop(timeout_seconds=1.0)
        strategy.external_io_cleanup_worker().stop(
            timeout_seconds=1.0
        )


def test_external_refresh_result_is_latest_wins_under_durable_pressure(
    tmp_path: Path,
) -> None:
    class RefreshProbeStrategy(_SmallExternalQueueProbeStrategy):
        def __init__(self, state_dir: Path) -> None:
            self.refresh_markers: list[str] = []
            super().__init__(state_dir)

        def _on_exchange_refresh_result(self, result: Any) -> None:
            continuation = result.task.success_continuation
            self.refresh_markers.append(
                str(continuation.get("marker") or "")
            )

    refresh_calls = 0

    class Mirror:
        def refresh(self) -> None:
            nonlocal refresh_calls
            refresh_calls += 1

    strategy = RefreshProbeStrategy(tmp_path)
    opening_plan = _order_plan(
        UUID("a2222222-2222-4222-8222-222222222222"),
        "entry-during-refresh-result-pressure",
    )
    strategy.set_exchange_cancel_adapter(False, Mirror())
    strategy._entry_protection_stash["durable-fill"] = {
        "state": "pending",
    }
    strategy._start_durable_io_lane()
    try:
        assert strategy._queue_entry_protection_stash_persist(
            changed_intent_keys="durable-fill"
        )
        assert strategy.durable_io_cleanup_worker().wait_empty(
            timeout_seconds=1.0
        )
        assert strategy._durable_io_mailbox.qsize() == 1

        assert strategy._queue_exchange_refresh(
            success_continuation={
                "kind": "protection_schedule",
                "intent_key": "refresh-owner",
                "marker": "stale",
            }
        )
        assert strategy.external_io_cleanup_worker().wait_empty(
            timeout_seconds=1.0
        )
        assert strategy._queue_exchange_refresh(
            success_continuation={
                "kind": "protection_schedule",
                "intent_key": "refresh-owner",
                "marker": "latest",
            }
        )
        assert strategy.external_io_cleanup_worker().wait_empty(
            timeout_seconds=1.0
        )

        assert refresh_calls == 2
        assert strategy._opening_side_effect_allowed(opening_plan) is True
        _drain_until_idle(strategy)

        assert strategy.refresh_markers == ["latest"]
        assert strategy.durable_io_halted_reason == ""
        assert strategy._opening_side_effect_allowed(opening_plan) is True
    finally:
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


def _authorized_order_plan(
    intent_id: UUID,
    client_order_id: str,
) -> OrderPlan:
    plan = _order_plan(intent_id, client_order_id)
    return OrderPlan(
        **{
            **plan.__dict__,
            "tags": (
                f"intent_id={intent_id}",
                "authorized_by_type=user",
                "authorized_by_id=risk-admin",
                "source_message_id=message-1",
            ),
        }
    )


def _replayed_opening_intent(intent_id: UUID) -> Any:
    return SimpleNamespace(
        intent_id=intent_id,
        instrument_id="BTCUSDT-PERP.BINANCE",
        action="open_position",
        order_plan={
            "type": "market",
            "side": "buy",
            "quantity": "0.1",
        },
    )


def _durable_replayed_opening_intent(
    intent_id: UUID,
    *,
    zone: bool = False,
) -> ApprovedTradeIntentV1:
    order_plan: dict[str, Any] = {
        "type": "market",
        "side": "buy",
        "quantity": "0.1",
    }
    if zone:
        order_plan = {
            "type": "zone_ladder",
            "side": "buy",
            "tranches": [
                {"price": "26000", "quantity": "0.03"},
                {"price": "25900", "quantity": "0.03"},
                {"price": "25800", "quantity": "0.04"},
            ],
        }
    now = datetime(
        2026,
        8,
        9,
        12,
        0,
        tzinfo=timezone.utc,
    )
    return ApprovedTradeIntentV1.model_construct(
        schema_version="1.0",
        intent_id=intent_id,
        decision_id=UUID(
            "11111111-1111-4111-8111-111111111111"
        ),
        risk_decision_id=UUID(
            "22222222-2222-4222-8222-222222222222"
        ),
        account_id="account-a",
        instrument_id="BTCUSDT-PERP.BINANCE",
        action=IntentAction.OPEN_POSITION,
        order_plan=order_plan,
        risk_budget=RiskBudget(
            risk_fraction=0.01,
            max_notional=100.0,
            max_leverage=2.0,
        ),
        target_position_id=None,
        valid_until=now + timedelta(minutes=5),
        idempotency_key="a" * 64,
        approved_at=now,
    )


def _seed_dispatched_receipt(
    path: Path,
    intent: ApprovedTradeIntentV1,
) -> JsonDurableIntentInbox:
    inbox = JsonDurableIntentInbox(path)
    inbox.receive("cursor-1", intent)
    inbox.complete(intent.intent_id, "DISPATCHED", "submitted")
    return inbox


def _wire_resubmit_receipt(
    strategy: IntentExecutionStrategy,
    inbox: JsonDurableIntentInbox,
) -> None:
    strategy.set_intent_receipt_status_getter(inbox.status)
    strategy.set_intent_receipt_transition_handler(
        lambda intent_id, expected, status, detail: (
            inbox.transition(
                intent_id,
                expected_status=expected,
                status=status,
                detail=detail,
            )
        )
    )
    strategy.set_intent_receipt_handler(
        lambda intent_id, status, detail: (
            inbox.complete(intent_id, status, detail) is None
        )
    )


def _seed_large_stash(
    strategy: IntentExecutionStrategy,
    stash_size: int,
) -> None:
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
    for index in range(stash_size):
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
