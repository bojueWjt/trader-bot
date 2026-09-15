"""Add-position protection: owned fills only, keep other SL/TP, stop_only.

Contract: docs/plans/2026-09-14-same-side-add-position.md (Codex header).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
for _path in (str(SERVICE_ROOT), str(DOMAIN_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from execution_domain.account_execution_ledger import (  # noqa: E402
    ReconciledExecutionState,
)
from execution_domain.order_ownership import (  # noqa: E402
    is_robot_client_order_id,
)
from strategy.intent_execution_planner import (  # noqa: E402
    InstrumentSpec,
    OrderDenied,
    OrderPlan,
    PlannerContext,
    PositionSnapshot,
    encode_client_order_id,
)
from strategy.intent_execution_strategy import (  # noqa: E402
    IntentExecutionStrategy,
    IntentExecutionStrategyConfig,
)


ACCOUNT_ID = "account-c"
INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
POSITION_ID = f"{INSTRUMENT_ID}-SHORT"


class AddPositionProtectionTest(unittest.TestCase):
    def test_add_protection_quantity_is_this_intent_fill_not_venue_net(self) -> None:
        strategy = _ProtectionStrategy()
        intent, plan = _add_intent_and_plan(quantity="0.08")
        self.assertTrue(strategy._stage_entry_protection(intent, plan))
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        event = SimpleNamespace(
            client_order_id=plan.client_order_id,
            last_qty="0.03",
            trade_id="t-partial",
        )
        strategy._record_batch_fill(event)
        strategy._record_batch_fill(event)

        self.assertEqual(
            strategy._protection_quantity(stash, {"quantity": "0.135"}),
            "0.03",
        )

    def test_duplicate_fill_event_does_not_over_protect(self) -> None:
        strategy = _ProtectionStrategy()
        intent, plan = _add_intent_and_plan(quantity="0.08")
        strategy._stage_entry_protection(intent, plan)
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        event = SimpleNamespace(
            client_order_id=plan.client_order_id,
            last_qty="0.08",
            filled_qty="0.08",
            trade_id="t-full",
        )
        strategy._record_batch_fill(event)
        strategy._record_batch_fill(event)
        strategy._record_batch_fill(event)

        self.assertEqual(
            strategy._protection_quantity(stash, {"quantity": "0.135"}),
            "0.08",
        )

    def test_partial_fill_protection_plan_uses_fill_quantity(self) -> None:
        strategy = _ProtectionStrategy()
        intent, plan = _add_intent_and_plan(quantity="0.08")
        strategy._stage_entry_protection(intent, plan)
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        strategy._record_batch_fill(
            SimpleNamespace(
                client_order_id=plan.client_order_id,
                last_qty="0.03",
                trade_id="t-partial",
            )
        )
        quantity = strategy._protection_quantity(stash, {"quantity": "0.085"})
        plans = strategy._protection_order_plans(
            intent.intent_id,
            stash,
            strategy._instrument_spec(INSTRUMENT_ID),
            {"id": POSITION_ID, "side": "SHORT", "quantity": "0.085"},
            quantity,
        )

        self.assertEqual(quantity, "0.03")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].order_type, "STOP_MARKET")
        self.assertEqual(plans[0].quantity, "0.03")
        self.assertEqual(plans[0].trigger_price, "80000.0")
        self.assertTrue(plans[0].reduce_only)

    def test_stop_only_does_not_replace_old_robot_take_profits(self) -> None:
        strategy = _ProtectionStrategy()
        intent, plan = _add_intent_and_plan(quantity="0.08")
        strategy._stage_entry_protection(intent, plan)
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        old_tp_id = encode_client_order_id(uuid4(), sequence=12)
        live = (
            _live_order(
                old_tp_id,
                "MARKET_IF_TOUCHED",
                "0.055",
                "76000",
                tags=(
                    f"position_id={POSITION_ID}",
                    "lifecycle_role=take_profit",
                ),
            ),
            _live_order(
                "aos_half_sl_1789061188",
                "STOP_MARKET",
                "0.055",
                "79000",
                tags=("manual=1",),
            ),
        )
        desired = strategy._protection_order_plans(
            intent.intent_id,
            stash,
            strategy._instrument_spec(INSTRUMENT_ID),
            {"id": POSITION_ID, "side": "SHORT", "quantity": "0.08"},
            "0.08",
        )

        actions, keep_ids, replace_ids = strategy._protection_replacement_actions(
            stash,
            live,
            desired,
            strategy._instrument_spec(INSTRUMENT_ID),
        )

        self.assertEqual(len(desired), 1)
        self.assertEqual(desired[0].order_type, "STOP_MARKET")
        self.assertTrue(any(action.order_type == "STOP_MARKET" for action in actions))
        self.assertNotIn(old_tp_id, replace_ids)
        self.assertNotIn("aos_half_sl_1789061188", replace_ids)
        self.assertEqual(keep_ids, ())

    def test_add_does_not_drop_other_source_protection_stash(self) -> None:
        strategy = _ProtectionStrategy()
        old_intent, old_plan = _open_intent_and_plan()
        self.assertTrue(strategy._stage_entry_protection(old_intent, old_plan))
        add_intent, add_plan = _add_intent_and_plan(quantity="0.08")
        self.assertTrue(strategy._stage_entry_protection(add_intent, add_plan))

        self.assertIn(str(old_intent.intent_id), strategy._entry_protection_stash)
        self.assertIn(str(add_intent.intent_id), strategy._entry_protection_stash)
        old_stash = strategy._entry_protection_stash[str(old_intent.intent_id)]
        add_stash = strategy._entry_protection_stash[str(add_intent.intent_id)]
        self.assertEqual(old_stash.get("take_profits"), ({"price": "76000"},))
        self.assertEqual(add_stash.get("protection_policy"), "stop_only")
        self.assertEqual(add_stash.get("take_profits"), ())

    def test_add_live_protection_orders_stay_intent_isolated(self) -> None:
        strategy = _ProtectionStrategy()
        intent, plan = _add_intent_and_plan(quantity="0.08")
        strategy._stage_entry_protection(intent, plan)
        old_tp_id = encode_client_order_id(uuid4(), sequence=12)
        manual_id = "aos_half_sl_1789061188"
        strategy._orders = [
            _live_order(
                old_tp_id,
                "MARKET_IF_TOUCHED",
                "0.055",
                "76000",
                tags=(
                    f"position_id={POSITION_ID}",
                    "lifecycle_role=take_profit",
                ),
            ),
            _live_order(
                manual_id,
                "STOP_MARKET",
                "0.055",
                "79000",
            ),
        ]
        live = strategy._live_protection_orders(
            INSTRUMENT_ID,
            str(intent.intent_id),
            11,
            position_id=POSITION_ID,
        )
        live_ids = {str(getattr(order, "client_order_id", "")) for order in live}
        self.assertNotIn(old_tp_id, live_ids)
        self.assertNotIn(manual_id, live_ids)

    def test_manual_sttoag_ids_are_not_replaced_or_cancelled(self) -> None:
        strategy = _ProtectionStrategy()
        intent, plan = _add_intent_and_plan(quantity="0.08")
        strategy._stage_entry_protection(intent, plan)
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        robot_id = encode_client_order_id(intent.intent_id, sequence=11)
        manual_ids = (
            "aos_half_sl_1789061188",
            "stToAg_manual_stop_1",
        )
        live = (
            _live_order(
                robot_id,
                "STOP_MARKET",
                "0.03",
                "80000",
                tags=(
                    f"position_id={POSITION_ID}",
                    "lifecycle_role=stop_loss",
                ),
            ),
            _live_order(
                manual_ids[0],
                "STOP_MARKET",
                "0.055",
                "79000",
                tags=("manual=1",),
            ),
            _live_order(
                manual_ids[1],
                "STOP_MARKET",
                "0.055",
                "78500",
                tags=("manual=1",),
            ),
        )
        desired = strategy._protection_order_plans(
            intent.intent_id,
            stash,
            strategy._instrument_spec(INSTRUMENT_ID),
            {"id": POSITION_ID, "side": "SHORT", "quantity": "0.08"},
            "0.08",
        )
        _actions, _keep_ids, replace_ids = strategy._protection_replacement_actions(
            stash,
            live,
            desired,
            strategy._instrument_spec(INSTRUMENT_ID),
        )
        live_ids = {
            str(getattr(order, "client_order_id", ""))
            for order in strategy._live_protection_orders(
                INSTRUMENT_ID,
                str(intent.intent_id),
                11,
                position_id=POSITION_ID,
            )
        }
        for manual_id in manual_ids:
            self.assertNotIn(manual_id, replace_ids)
            self.assertNotIn(manual_id, live_ids)
        self.assertTrue(is_robot_client_order_id(robot_id))

    def test_sync_does_not_size_from_venue_before_add_fill(self) -> None:
        strategy = _ProtectionStrategy()
        intent, plan = _add_intent_and_plan(quantity="0.08")
        strategy._stage_entry_protection(intent, plan)
        strategy._positions = [
            {
                "id": POSITION_ID,
                "side": "SHORT",
                "quantity": "0.055",
            }
        ]
        strategy._sync_protection(str(intent.intent_id))
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        self.assertIsNone(stash.get("pending_protection_revision"))
        self.assertIsNone(stash.get("protected_quantity"))
        self.assertEqual(strategy.submitted_plans, [])

    def test_partial_fill_sync_uses_owned_quantity_even_if_cache_empty(self) -> None:
        strategy = _ProtectionStrategy()
        intent, plan = _add_intent_and_plan(quantity="0.08")
        strategy._stage_entry_protection(intent, plan)
        strategy._record_batch_fill(
            SimpleNamespace(
                client_order_id=plan.client_order_id,
                last_qty="0.03",
                trade_id="t-partial",
            )
        )
        strategy._positions = []
        strategy._sync_protection(str(intent.intent_id))
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        self.assertIn(str(intent.intent_id), strategy._entry_protection_stash)
        self.assertEqual(
            Decimal(str(stash.get("protected_quantity") or "0")),
            Decimal("0.03"),
        )
        self.assertEqual(len(strategy.submitted_plans), 1)
        self.assertEqual(strategy.submitted_plans[0].order_type, "STOP_MARKET")
        self.assertEqual(
            Decimal(strategy.submitted_plans[0].quantity),
            Decimal("0.03"),
        )

    def test_zone_ladder_add_uses_validate_position(self) -> None:
        strategy = _ProtectionStrategy()
        intent = _zone_ladder_add_intent()
        context = PlannerContext(
            account_id=ACCOUNT_ID,
            trading_state="ACTIVE",
            now=NOW,
            instrument=strategy._instrument_spec(INSTRUMENT_ID),
            position=None,
            positions=(),
            existing_intent_ids=frozenset(),
            reconciled_state=ReconciledExecutionState.build(
                account_id=ACCOUNT_ID,
                venue_snapshot={
                    "positions": [
                        {
                            "symbol": "BTCUSDT",
                            "position_amt": "0.055",
                            "position_side": "SHORT",
                        }
                    ],
                    "open_orders": [],
                    "algo_orders": [],
                },
                venue_fetched_at=NOW,
                cache_positions=(),
                now=NOW,
            ),
        )
        plans = strategy._zone_ladder_order_plans(
            intent,
            intent.order_plan,
            context,
            "add_position",
        )
        self.assertIsInstance(plans, tuple)
        assert isinstance(plans, tuple)
        self.assertEqual(len(plans), 3)
        self.assertTrue(all(plan.side == "SELL" for plan in plans))
        self.assertTrue(all(plan.reduce_only is False for plan in plans))

        empty = strategy._zone_ladder_order_plans(
            intent,
            intent.order_plan,
            PlannerContext(
                account_id=ACCOUNT_ID,
                trading_state="ACTIVE",
                now=NOW,
                instrument=strategy._instrument_spec(INSTRUMENT_ID),
                position=None,
                positions=(),
                existing_intent_ids=frozenset(),
                reconciled_state=ReconciledExecutionState.build(
                    account_id=ACCOUNT_ID,
                    venue_snapshot={
                        "positions": [],
                        "open_orders": [],
                        "algo_orders": [],
                    },
                    venue_fetched_at=NOW,
                    cache_positions=(),
                    now=NOW,
                ),
            ),
            "add_position",
        )
        self.assertIsInstance(empty, OrderDenied)
        assert isinstance(empty, OrderDenied)
        self.assertEqual(empty.reason, "position_required")

    def test_new_sl_accepted_old_cancel_fail_retry_does_not_duplicate(
        self,
    ) -> None:
        strategy = _PartialProtectStrategy()
        intent, plan = _add_intent_and_plan(quantity="0.08")
        self.assertTrue(strategy._stash_entry_protection(intent, plan))
        old_sl = encode_client_order_id(intent.intent_id, sequence=11)
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        stash["protection_revision"] = 0
        stash["protection_ids"] = (old_sl,)
        stash["protection_roles"] = {
            old_sl: {
                "role": "stop_loss",
                "tp_price": None,
                "quantity": "0.08",
                "submitted_at": "",
                "order_type": "STOP_MARKET",
                "side": "BUY",
                "tags": (
                    f"position_id={POSITION_ID}",
                    "lifecycle_role=stop_loss",
                ),
            }
        }
        manual_ids = ("aos_half_sl_1789061188", "stToAg_manual_stop_1")
        strategy._orders = [
            SimpleNamespace(
                client_order_id=plan.client_order_id,
                filled_qty="0.08",
                quantity="0.08",
                status="FILLED",
                instrument_id=INSTRUMENT_ID,
                order_type="LIMIT",
                side="SELL",
                tags=plan.tags,
            ),
            _live_order(
                old_sl,
                "STOP_MARKET",
                "0.08",
                "85000",
                tags=(
                    f"position_id={POSITION_ID}",
                    "lifecycle_role=stop_loss",
                ),
            ),
            _live_order(manual_ids[0], "STOP_MARKET", "0.055", "79000"),
            _live_order(manual_ids[1], "STOP_MARKET", "0.055", "78500"),
        ]
        strategy._record_batch_fill(
            SimpleNamespace(
                client_order_id=plan.client_order_id,
                last_qty="0.08",
                trade_id="add-fill",
            )
        )
        strategy._positions = [
            {"id": POSITION_ID, "side": "SHORT", "quantity": "0.08"}
        ]
        strategy.cancel_fail_ids.add(old_sl)

        strategy._sync_protection(str(intent.intent_id))

        self.assertEqual(len(strategy.submitted_plans), 1)
        new_sl = strategy.submitted_plans[0].client_order_id
        self.assertTrue(is_robot_client_order_id(new_sl))
        self.assertNotEqual(new_sl, old_sl)
        self.assertEqual(
            new_sl,
            encode_client_order_id(intent.intent_id, sequence=21),
        )
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        self.assertIn(old_sl, tuple(stash.get("pending_cancel_ids") or ()))
        self.assertNotIn(manual_ids[0], strategy.cancelled)
        self.assertNotIn(manual_ids[1], strategy.cancelled)

        restarted = _PartialProtectStrategy(state_dir=strategy._state_dir)
        restarted._entry_protection_stash = restarted._load_entry_protection_stash()
        restarted._orders = list(strategy._orders)
        restarted._positions = list(strategy._positions)
        restarted.cancel_fail_ids.add(old_sl)
        restarted._sync_protection(str(intent.intent_id))

        self.assertEqual(restarted.submitted_plans, [])
        live_robot = [
            str(order.client_order_id)
            for order in restarted._orders
            if is_robot_client_order_id(str(order.client_order_id))
        ]
        self.assertEqual(live_robot.count(new_sl), 1)
        self.assertIn(old_sl, live_robot)
        self.assertNotIn(manual_ids[0], restarted.cancelled)
        self.assertNotIn(manual_ids[1], restarted.cancelled)


class _PartialProtectStrategy(IntentExecutionStrategy):
    def __init__(self, state_dir: Path | None = None) -> None:
        self._state_dir = state_dir or Path(tempfile.mkdtemp())
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id=ACCOUNT_ID,
                node_id="node-c",
                trading_state="ACTIVE",
                environment="testnet",
                intent_execution_inbox_path=str(
                    self._state_dir / "intent-execution-inbox.json"
                ),
            )
        )
        self._positions: list[Any] = []
        self._orders: list[Any] = []
        self.submitted_plans: list[OrderPlan] = []
        self.cancelled: list[str] = []
        self.cancel_fail_ids: set[str] = set()

    def _protection_stash_path(self) -> str:
        return str(self._state_dir / self._PROTECTION_STASH_FILENAME)

    def _now(self):
        return NOW

    def _instrument_spec(self, instrument_id: str) -> InstrumentSpec:
        return InstrumentSpec(
            instrument_id=instrument_id,
            price_increment="0.1",
            quantity_increment="0.001",
        )

    def _cache_positions(self, _instrument_id):
        return tuple(self._positions)

    def _cache_orders(self, _instrument_id):
        return tuple(self._orders)

    def _cache_orders_all(self, _instrument_id):
        return tuple(self._orders)

    def _queue_entry_protection_stash_persist(self, continuation=False):
        self._persist_entry_protection_stash()
        kind = ""
        if isinstance(continuation, dict):
            kind = str(continuation.get("kind") or "")
        if kind == "protection_submit_revision":
            self._continue_protection_revision_submit(continuation)
            self._persist_entry_protection_stash()
        return True

    def _submit_order_plan(self, plan):
        self.submitted_plans.append(plan)
        self._orders.append(
            _live_order(
                plan.client_order_id,
                plan.order_type,
                plan.quantity,
                str(plan.trigger_price or ""),
                tags=plan.tags,
            )
        )
        return True

    def _cancel_order_object(self, order):
        oid = str(getattr(order, "client_order_id", ""))
        self.cancelled.append(oid)
        if oid in self.cancel_fail_ids:
            return False
        self._orders = [
            item
            for item in self._orders
            if str(getattr(item, "client_order_id", "")) != oid
        ]
        return True


class _ProtectionStrategy(IntentExecutionStrategy):
    def __init__(self) -> None:
        state_dir = Path(tempfile.mkdtemp())
        self._state_dir = state_dir
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id=ACCOUNT_ID,
                node_id="node-c",
                trading_state="ACTIVE",
                environment="testnet",
                intent_execution_inbox_path=str(
                    state_dir / "intent-execution-inbox.json"
                ),
            )
        )
        self._positions: list[Any] = []
        self._orders: list[Any] = []
        self.submitted_plans: list[OrderPlan] = []

    def _protection_stash_path(self) -> str:
        return str(self._state_dir / self._PROTECTION_STASH_FILENAME)

    def _now(self):
        return NOW

    def _instrument_spec(self, instrument_id: str) -> InstrumentSpec:
        return InstrumentSpec(
            instrument_id=instrument_id,
            price_increment="0.1",
            quantity_increment="0.001",
        )

    def _cache_positions(self, _instrument_id):
        return tuple(self._positions)

    def _cache_orders(self, _instrument_id):
        return tuple(self._orders)

    def _cache_orders_all(self, _instrument_id):
        return tuple(self._orders)

    def _queue_entry_protection_stash_persist(self, continuation=False):
        kind = ""
        if isinstance(continuation, dict):
            kind = str(continuation.get("kind") or "")
        if kind == "protection_submit_revision":
            self._continue_protection_revision_submit(continuation)
            return True
        return True

    def _submit_order_plan(self, plan):
        self.submitted_plans.append(plan)
        return True

    def _cancel_order_object(self, order):
        self.cancelled = getattr(self, "cancelled", [])
        self.cancelled.append(order)
        return True


def _add_intent_and_plan(*, quantity: str) -> tuple[SimpleNamespace, OrderPlan]:
    intent_id = uuid4()
    intent = SimpleNamespace(
        intent_id=intent_id,
        decision_id=uuid4(),
        risk_decision_id=uuid4(),
        idempotency_key=f"idempotency-{intent_id}",
        account_id=ACCOUNT_ID,
        instrument_id=INSTRUMENT_ID,
        action="add_position",
        order_plan={
            "type": "limit",
            "side": "sell",
            "quantity": quantity,
            "price": "78800",
            "stop_loss": "80000",
            "take_profits": (),
            "protection_policy": "stop_only",
            "authorization": {
                "authorized_by_type": "user",
                "authorized_by_id": "add-protection-test",
                "source_message_id": f"add-{intent_id}",
            },
        },
    )
    plan = OrderPlan(
        intent_id=intent_id,
        client_order_id=encode_client_order_id(intent_id, sequence=1),
        tags=_tags(intent, "add_position"),
        instrument_id=INSTRUMENT_ID,
        side="SELL",
        order_type="LIMIT",
        quantity=quantity,
        price="78800",
        time_in_force="GTC",
    )
    return intent, plan


def _open_intent_and_plan() -> tuple[SimpleNamespace, OrderPlan]:
    intent_id = uuid4()
    intent = SimpleNamespace(
        intent_id=intent_id,
        decision_id=uuid4(),
        risk_decision_id=uuid4(),
        idempotency_key=f"idempotency-{intent_id}",
        account_id=ACCOUNT_ID,
        instrument_id=INSTRUMENT_ID,
        action="open_position",
        order_plan={
            "type": "market",
            "side": "sell",
            "quantity": "0.111",
            "stop_loss": "85000",
            "take_profits": ({"price": "76000"},),
            "authorization": {
                "authorized_by_type": "user",
                "authorized_by_id": "open-protection-test",
                "source_message_id": f"open-{intent_id}",
            },
        },
    )
    plan = OrderPlan(
        intent_id=intent_id,
        client_order_id=encode_client_order_id(intent_id, sequence=1),
        tags=_tags(intent, "open_position"),
        instrument_id=INSTRUMENT_ID,
        side="SELL",
        order_type="MARKET",
        quantity="0.111",
        price=None,
        time_in_force="IOC",
    )
    return intent, plan


def _zone_ladder_add_intent() -> SimpleNamespace:
    intent_id = uuid4()
    return SimpleNamespace(
        intent_id=intent_id,
        decision_id=uuid4(),
        risk_decision_id=uuid4(),
        idempotency_key=f"idempotency-{intent_id}",
        account_id=ACCOUNT_ID,
        instrument_id=INSTRUMENT_ID,
        action="add_position",
        valid_until=NOW + timedelta(minutes=5),
        order_plan={
            "type": "zone_ladder",
            "side": "sell",
            "authorization": {
                "authorized_by_type": "user",
                "authorized_by_id": "zone-add-test",
                "source_message_id": f"zone-{intent_id}",
            },
            "tranches": [
                {"seq": 1, "quantity": "0.02", "price": "78800"},
                {"seq": 2, "quantity": "0.03", "price": "78700"},
                {"seq": 3, "quantity": "0.03", "price": "78600"},
            ],
        },
        risk_budget=SimpleNamespace(max_notional="100000"),
    )


def _tags(intent: SimpleNamespace, action: str) -> tuple[str, ...]:
    auth = intent.order_plan["authorization"]
    return (
        f"intent_id={intent.intent_id}",
        f"decision_id={intent.decision_id}",
        f"risk_decision_id={intent.risk_decision_id}",
        f"idempotency_key={intent.idempotency_key}",
        f"action={action}",
        f"account_id={intent.account_id}",
        f"parent_intent_id={intent.intent_id}",
        f"authorized_by_type={auth['authorized_by_type']}",
        f"authorized_by_id={auth['authorized_by_id']}",
        f"source_message_id={auth['source_message_id']}",
    )


def _live_order(
    client_order_id: str,
    order_type: str,
    quantity: str,
    trigger_price: str,
    tags: tuple[str, ...] = (),
):
    return SimpleNamespace(
        client_order_id=client_order_id,
        instrument_id=INSTRUMENT_ID,
        order_type=order_type,
        side="BUY",
        quantity=quantity,
        price=None,
        trigger_price=trigger_price,
        reduce_only=True,
        status="ACCEPTED",
        tags=tags,
    )


if __name__ == "__main__":
    unittest.main()
