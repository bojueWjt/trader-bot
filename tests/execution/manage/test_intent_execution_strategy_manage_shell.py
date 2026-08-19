from __future__ import annotations

import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from strategy.intent_execution_planner import (  # noqa: E402
    InstrumentSpec,
    ManagementPlan,
)
from strategy.intent_execution_strategy import (  # noqa: E402
    IntentExecutionStrategy,
    IntentExecutionStrategyConfig,
)
from runtime.intent_execution_inbox import (  # noqa: E402
    IntentExecutionIdentity,
    IntentExecutionState,
)


ACCOUNT_ID = "account-a"
INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
POSITION_ID = "P-1"
NOW = datetime(2026, 6, 19, 12, tzinfo=timezone.utc)
ROBOT_OLD_STOP_ID = "Baaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01"
ROBOT_OLD_TP_ID = "Bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb02"


class StrategyManageShellTest(unittest.TestCase):
    def test_async_management_persists_terminal_state_and_replay_is_inert(
        self,
    ) -> None:
        intent = _intent(
            action="move_stop_loss",
            order_plan={"stop_price": "26000.114"},
        )
        old_stop = SimpleNamespace(
            account_id=ACCOUNT_ID,
            symbol="BTCUSDT",
            position_side="LONG",
            order_kind="regular",
            venue_order_id="venue-old-stop",
            client_order_id=ROBOT_OLD_STOP_ID,
            instrument_id=INSTRUMENT_ID,
            order_type="STOP_MARKET",
            side="SELL",
            quantity="0.5",
            trigger_price="25500",
            tags=(
                f"position_id={POSITION_ID}",
                "lifecycle_role=stop_loss",
            ),
        )
        strategy = _HarnessStrategy(orders=[old_stop])
        worker = _RecordingTerminalWorker()
        strategy.set_terminal_exchange_worker(worker)

        try:
            self.assertTrue(strategy._queue_intent_receive(intent))
            _pump_durable(strategy)
            refresh = worker.requests[-1]
            strategy._on_terminal_exchange_result(
                SimpleNamespace(
                    request_id=refresh.request_id,
                    account_id=ACCOUNT_ID,
                    error="",
                )
            )
            _pump_durable(strategy)

            cancel = worker.requests[-1]
            cancel_outcomes = tuple(
                SimpleNamespace(
                    request=request,
                    status="confirmed",
                    error="",
                )
                for request in cancel.cancel_requests
            )
            strategy._on_terminal_exchange_result(
                SimpleNamespace(
                    request_id=cancel.request_id,
                    account_id=ACCOUNT_ID,
                    error="",
                    cancel_outcomes=cancel_outcomes,
                )
            )
            _pump_durable(strategy)

            identity = strategy._intent_execution_inbox.pending()
            self.assertEqual(identity, ())
            record = strategy._intent_execution_inbox.get(
                IntentExecutionIdentity(
                    account_id=intent.account_id,
                    intent_id=str(intent.intent_id),
                    idempotency_key=intent.idempotency_key,
                    instrument_id=intent.instrument_id,
                    action=intent.action,
                )
            )
            self.assertTrue(record)
            self.assertEqual(
                record.state,
                IntentExecutionState.EXCHANGE_CONFIRMED,
            )
            submit_count = len(strategy.submitted_plans)
            request_count = len(worker.requests)

            strategy._processed_intent_ids.clear()
            self.assertTrue(strategy._queue_intent_receive(intent))
            _pump_durable(strategy)

            self.assertEqual(
                len(strategy.submitted_plans),
                submit_count,
            )
            self.assertEqual(len(worker.requests), request_count)
        finally:
            strategy.on_stop()

    def test_on_data_replaces_stop_by_cancelling_cached_stop_and_submitting_new_one(self) -> None:
        intent = _intent(
            action="move_stop_loss",
            order_plan={"stop_price": "26000.114"},
        )
        old_stop = SimpleNamespace(
            client_order_id=ROBOT_OLD_STOP_ID,
            instrument_id=INSTRUMENT_ID,
            order_type="STOP_MARKET",
            side="SELL",
            quantity="0.5",
            trigger_price="25500",
            tags=(f"position_id={POSITION_ID}", "lifecycle_role=stop_loss"),
        )
        strategy = _HarnessStrategy(orders=[old_stop])

        strategy._handle_intent(intent)

        self.assertEqual(strategy.cancelled_client_order_ids, [ROBOT_OLD_STOP_ID])
        self.assertEqual(len(strategy.submitted_plans), 1)
        submitted = strategy.submitted_plans[0]
        self.assertEqual(submitted.order_type, "STOP_MARKET")
        self.assertEqual(submitted.trigger_price, "26000.11")
        self.assertTrue(submitted.reduce_only)
        self.assertIn("lifecycle_role=stop_loss", submitted.tags)

    def test_on_data_rejects_management_when_target_position_is_not_unique(self) -> None:
        intent = _intent(
            action="close_position",
            target_position_id=None,
            order_plan={"type": "market"},
        )
        strategy = _HarnessStrategy(
            positions=[
                _position(position_id="P-1"),
                _position(position_id="P-2"),
            ]
        )

        strategy._handle_intent(intent)

        self.assertEqual(strategy.submitted_plans, [])
        self.assertEqual(strategy.denials[-1].reason, "position_not_unique")

    def test_replace_take_profits_submits_market_if_touched_orders(self) -> None:
        intent = _intent(
            action="replace_take_profits",
            order_plan={
                "take_profits": [
                    {"quantity": "0.2", "price": "28000"},
                    {
                        "quantity": "0.3",
                        "trigger_price": "29000",
                        "limit_price": "28999",
                    },
                ]
            },
        )
        old_tp = SimpleNamespace(
            client_order_id=ROBOT_OLD_TP_ID,
            instrument_id=INSTRUMENT_ID,
            order_type="MARKET_IF_TOUCHED",
            side="SELL",
            quantity="0.5",
            price=None,
            trigger_price="27500",
            tags=(f"position_id={POSITION_ID}", "lifecycle_role=take_profit"),
        )
        strategy = _HarnessStrategy(orders=[old_tp])
        owner_intent_id = str(uuid4())
        strategy._entry_protection_stash[owner_intent_id] = _protection_stash()

        strategy._handle_intent(intent)

        self.assertEqual(strategy.cancelled_client_order_ids, [ROBOT_OLD_TP_ID])
        self.assertEqual(
            [plan.order_type for plan in strategy.submitted_plans],
            ["MARKET_IF_TOUCHED", "MARKET_IF_TOUCHED"],
        )
        self.assertEqual(
            [plan.trigger_price for plan in strategy.submitted_plans],
            ["28000.00", "29000.00"],
        )
        self.assertTrue(all(plan.price is None for plan in strategy.submitted_plans))
        self.assertTrue(all(plan.reduce_only for plan in strategy.submitted_plans))
        self.assertTrue(all("lifecycle_role=take_profit" in plan.tags for plan in strategy.submitted_plans))
        self.assertEqual(
            strategy._entry_protection_stash[owner_intent_id]["take_profits"],
            ("28000.00", "29000.00"),
        )
        self.assertEqual(
            strategy._entry_protection_stash[owner_intent_id][
                "take_profit_quantities"
            ],
            ("0.200", "0.300"),
        )

    def test_protection_sync_keeps_stop_market_and_builds_mit_tp_ladder(self) -> None:
        strategy = _HarnessStrategy()
        intent_id = uuid4()
        stash = _protection_stash()

        plans = strategy._protection_order_plans(
            intent_id,
            stash,
            _instrument_spec(),
            _position(),
            "0.500",
        )

        self.assertEqual(
            [plan.order_type for plan in plans],
            ["STOP_MARKET", "MARKET_IF_TOUCHED", "MARKET_IF_TOUCHED"],
        )
        self.assertEqual(
            [plan.trigger_price for plan in plans],
            ["26000.12", "28000.11", "29000.00"],
        )
        self.assertEqual(
            [plan.quantity for plan in plans],
            ["0.500", "0.200", "0.300"],
        )
        self.assertTrue(all(plan.price is None for plan in plans))

    def test_live_mit_orders_are_recognized_matched_and_adopted_by_trigger(self) -> None:
        strategy = _HarnessStrategy()
        intent_id = uuid4()
        stash = _protection_stash()
        instrument = _instrument_spec()
        plans = strategy._protection_order_plans(
            intent_id,
            stash,
            instrument,
            _position(),
            "0.500",
        )
        live = (
            _live_order("sl-live", "STOP_MARKET", "0.500", "26000.12"),
            _live_order("tp-live-1", "MARKET_IF_TOUCHED", "0.200", "28000.11"),
            _live_order("tp-live-2", "MARKET_IF_TOUCHED", "0.300", "29000.00"),
        )

        self.assertEqual(strategy._protection_order_role(stash, live[1]), "take_profit")
        self.assertEqual(
            strategy._protection_order_trigger_price(live[1], instrument),
            "28000.11",
        )
        self.assertTrue(strategy._live_order_matches_plan(live[1], plans[1], instrument))
        self.assertEqual(
            strategy._adopt_matching_live_protections(live, plans, instrument),
            ("sl-live", "tp-live-1", "tp-live-2"),
        )
        legacy_limit_tp = _live_order(
            "legacy-limit-tp",
            "LIMIT_IF_TOUCHED",
            "0.200",
            "28000.11",
        )
        self.assertFalse(
            strategy._live_order_matches_plan(
                legacy_limit_tp,
                plans[1],
                instrument,
            )
        )
        self.assertIsNone(
            strategy._adopt_matching_live_protections(
                (live[0], legacy_limit_tp, live[2]),
                plans,
                instrument,
            )
        )

        strategy._register_protection_role(
            str(intent_id),
            stash,
            "tp-live-1",
            plans[1],
        )
        self.assertEqual(
            stash["protection_roles"]["tp-live-1"]["tp_price"],
            "28000.11",
        )

    def test_live_mit_quantity_one_step_short_is_kept_within_tolerance(self) -> None:
        strategy = _HarnessStrategy()
        intent_id = uuid4()
        stash = _protection_stash()
        instrument = _instrument_spec()
        plans = strategy._protection_order_plans(
            intent_id,
            stash,
            instrument,
            _position(),
            "0.500",
        )
        tp_plan = plans[1]
        live = (
            _live_order(
                "tp-live-short",
                "MARKET_IF_TOUCHED",
                "0.199",
                "28000.11",
            ),
        )

        actions, keep_ids, replace_ids = strategy._protection_replacement_actions(
            stash,
            live,
            (tp_plan,),
            instrument,
        )

        self.assertEqual(actions, ())
        self.assertEqual(keep_ids, ("tp-live-short",))
        self.assertEqual(replace_ids, set())

    def test_live_mit_quantity_two_steps_short_requires_replacement(self) -> None:
        strategy = _HarnessStrategy()
        intent_id = uuid4()
        stash = _protection_stash()
        instrument = _instrument_spec()
        plans = strategy._protection_order_plans(
            intent_id,
            stash,
            instrument,
            _position(),
            "0.500",
        )
        tp_plan = plans[1]
        live = (
            _live_order(
                "tp-live-short",
                "MARKET_IF_TOUCHED",
                "0.198",
                "28000.11",
            ),
        )

        actions, keep_ids, replace_ids = strategy._protection_replacement_actions(
            stash,
            live,
            (tp_plan,),
            instrument,
        )

        self.assertEqual(actions, (tp_plan,))
        self.assertEqual(keep_ids, ())
        self.assertEqual(replace_ids, {"tp-live-short"})

    def test_cancel_failure_does_not_submit_replacement_or_mark_processed(self) -> None:
        intent = _intent(
            action="move_stop_loss",
            order_plan={"stop_price": "26000"},
        )
        old_stop = SimpleNamespace(
            client_order_id=ROBOT_OLD_STOP_ID,
            instrument_id=INSTRUMENT_ID,
            order_type="STOP_MARKET",
            side="SELL",
            quantity="0.5",
            trigger_price="25500",
            tags=(f"position_id={POSITION_ID}", "lifecycle_role=stop_loss"),
        )
        strategy = _HarnessStrategy(orders=[old_stop], fail_cancel=True)

        strategy._handle_intent(intent)

        self.assertEqual(len(strategy.submitted_plans), 1)
        self.assertEqual(strategy.submitted_plans[0].order_type, "STOP_MARKET")
        self.assertEqual(strategy.submitted_plans[0].trigger_price, "26000.00")
        self.assertEqual(strategy.denials[-1].reason, "order_cancel_failed")
        self.assertNotIn(str(intent.intent_id), strategy._processed_intent_ids)

    def test_management_plan_without_authorization_is_inert(self) -> None:
        strategy = _HarnessStrategy()
        plan = ManagementPlan(
            intent_id=uuid4(),
            action="cancel",
            instrument_id=INSTRUMENT_ID,
            target_position_id=None,
            target_position_side=None,
            cancel_order_ids=("system-order",),
            orders=(),
        )

        submitted = strategy._submit_management_plan(plan)

        self.assertFalse(submitted)
        self.assertEqual(strategy.cancelled_client_order_ids, [])
        self.assertEqual(strategy.submitted_plans, [])
        self.assertEqual(
            strategy.denials[-1].reason,
            "management_authorization_missing",
        )


class _HarnessStrategy(IntentExecutionStrategy):
    def __init__(self, positions=None, orders=None, fail_cancel=False):
        state_dir = Path(tempfile.mkdtemp())
        self._state_dir = state_dir
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id=ACCOUNT_ID,
                trading_state="ACTIVE",
                intent_execution_inbox_path=str(
                    state_dir / "intent-execution-inbox.json"
                ),
            )
        )
        self._positions = list(positions or [_position()])
        self._orders = list(orders or [])
        self.cancelled_client_order_ids: list[str] = []
        self.submitted_plans = []
        self._fail_cancel = fail_cancel
        self.set_exchange_cancel_adapter(
            False,
            _FreshMirror(tuple(self._orders)),
        )

    def _protection_stash_path(self) -> str:
        return str(
            self._state_dir / self._PROTECTION_STASH_FILENAME
        )

    def _now(self):
        return NOW

    def _cache_instrument(self, _instrument_id):
        return SimpleNamespace(
            id=INSTRUMENT_ID,
            price_increment="0.01",
            size_increment="0.001",
        )

    def _cache_positions(self, _instrument_id):
        return self._positions

    def _cache_orders(self, _instrument_id):
        return self._orders

    def _build_nautilus_order(self, plan, _instrument):
        self.submitted_plans.append(plan)
        return SimpleNamespace(client_order_id=plan.client_order_id, plan=plan)

    def submit_order(self, _order):
        return None

    def _persist_entry_protection_stash(self):
        return True

    def cancel_order(self, order):
        if self._fail_cancel:
            raise RuntimeError("cancel rejected")
        self.cancelled_client_order_ids.append(str(order.client_order_id))


class _FreshMirror:
    def __init__(self, orders: tuple[Any, ...]) -> None:
        self._orders = orders

    def refresh(self) -> tuple[Any, ...]:
        return self._orders

    def orders_for_instrument(self, instrument_id: str) -> tuple[Any, ...]:
        return tuple(
            order
            for order in self._orders
            if str(getattr(order, "instrument_id", "")) == str(instrument_id)
        )

    def find_order(self, instrument_id: str, client_order_id: str) -> Any:
        for order in self.orders_for_instrument(instrument_id):
            if str(getattr(order, "client_order_id", "")) == str(client_order_id):
                return order
        return False


class _RecordingTerminalWorker:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def new_deadline(self) -> float:
        return time.monotonic() + 1.0

    def submit(self, request: Any) -> bool:
        self.requests.append(request)
        return True


def _position(position_id: str = POSITION_ID):
    return SimpleNamespace(
        id=position_id,
        instrument_id=INSTRUMENT_ID,
        side="LONG",
        quantity="0.5",
        entry_price="27123.456",
    )


def _instrument_spec() -> InstrumentSpec:
    return InstrumentSpec(
        instrument_id=INSTRUMENT_ID,
        price_increment="0.01",
        quantity_increment="0.001",
    )


def _protection_stash() -> dict[str, Any]:
    return {
        "instrument_id": INSTRUMENT_ID,
        "entry_side": "BUY",
        "entry_tags": (),
        "stop_loss": "26000.116",
        "take_profits": (
            {"trigger_price": "28000.111", "price": "27999"},
            {"trigger_price": "29000"},
        ),
        "take_profit_quantities": ("0.200", "0.300"),
        "tp_consumed": {},
        "protection_ids": (),
        "protection_roles": {},
        "pending_cancel_ids": (),
    }


def _live_order(
    client_order_id: str,
    order_type: str,
    quantity: str,
    trigger_price: str,
):
    return SimpleNamespace(
        client_order_id=client_order_id,
        instrument_id=INSTRUMENT_ID,
        order_type=order_type,
        side="SELL",
        quantity=quantity,
        price=None,
        trigger_price=trigger_price,
        reduce_only=True,
        tags=(),
    )


@dataclass(frozen=True)
class _Intent:
    schema_version: str
    intent_id: UUID
    decision_id: UUID
    risk_decision_id: UUID
    account_id: str
    instrument_id: str
    action: str
    order_plan: dict[str, Any]
    valid_until: datetime
    idempotency_key: str
    approved_at: datetime
    target_position_id: str | None = None


def _intent(**overrides):
    intent_id = overrides.get("intent_id", uuid4())
    values = {
        "schema_version": "1.0",
        "intent_id": intent_id,
        "decision_id": uuid4(),
        "risk_decision_id": uuid4(),
        "account_id": ACCOUNT_ID,
        "instrument_id": INSTRUMENT_ID,
        "action": "move_stop_loss",
        "order_plan": {"stop_price": "26000"},
        "target_position_id": POSITION_ID,
        "valid_until": NOW + timedelta(minutes=5),
        "idempotency_key": sha256(str(intent_id).encode("ascii")).hexdigest(),
        "approved_at": NOW - timedelta(seconds=5),
    }
    values.update(overrides)
    order_plan = dict(values["order_plan"])
    order_plan.setdefault(
        "authorization",
        {
            "authorized_by_type": "user",
            "authorized_by_id": "strategy-test",
            "source_message_id": f"strategy-test-{intent_id}",
        },
    )
    values["order_plan"] = order_plan
    return _Intent(**values)


def _pump_durable(strategy: IntentExecutionStrategy) -> None:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        strategy.drain_durable_io_mailbox()
        snapshot = strategy._durable_io_worker.snapshot()
        if (
            not snapshot.in_flight
            and snapshot.queue_depth == 0
            and strategy._durable_io_mailbox.empty()
        ):
            strategy.drain_durable_io_mailbox()
            return
        time.sleep(0.001)
    raise AssertionError("durable I/O did not quiesce")


if __name__ == "__main__":
    unittest.main()
