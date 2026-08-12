from __future__ import annotations

from pathlib import Path
import sys
from uuid import uuid4

import pytest


pytest.importorskip("nautilus_trader")


TESTS_ROOT = Path(__file__).resolve().parents[2]
tests_root_value = str(TESTS_ROOT)
if tests_root_value not in sys.path:
    sys.path.insert(0, tests_root_value)

from nautilus_simulated_harness import (  # noqa: E402
    EXPECTED_NAUTILUS_VERSION,
    NautilusSimulatedEngine,
    build_intent,
    nautilus_version,
)
from execution_domain.contracts import IntentAction  # noqa: E402
from nautilus_trader.model.enums import (  # noqa: E402
    OrderStatus,
    OrderType,
)


def test_stop_loss_replace_cancels_old_stop_and_submits_reduce_only_stop_market() -> None:
    assert nautilus_version == EXPECTED_NAUTILUS_VERSION
    engine = NautilusSimulatedEngine()
    try:
        strategy = engine.start_strategy()
        position = engine.seed_long_position(
            strategy,
            quantity="0.010",
        )
        old_stop = engine.seed_stop_order(
            strategy,
            position,
            trigger_price="95000.0",
        )
        engine.attach_real_cancel_bridge(strategy)
        intent = build_intent(
            action=IntentAction.MOVE_STOP_LOSS,
            target_position_id=str(position.id),
            order_plan={
                "type": "stop_market",
                "stop_price": "97000.0",
                "position_side": "LONG",
            },
        )

        engine.deliver_intent(strategy, intent)
        engine.process_exchange()

        assert old_stop.status is OrderStatus.CANCELED
        assert type(old_stop.last_event).__name__ == "OrderCanceled"
        open_orders = engine.cache.orders_open(
            instrument_id=engine.instrument.id
        )
        assert len(open_orders) == 1
        replacement = open_orders[0]
        assert replacement.order_type is OrderType.STOP_MARKET
        assert replacement.status is OrderStatus.ACCEPTED
        assert replacement.is_reduce_only is True
        assert str(replacement.trigger_price) == "97000.0"
        assert "lifecycle_role=stop_loss" in replacement.tags
        assert type(replacement.last_event).__name__ == "OrderAccepted"
        assert strategy.denials == []
    finally:
        engine.close()


def test_take_profit_replace_cancels_old_tps_and_submits_reduce_only_conditional_tps() -> None:
    assert nautilus_version == EXPECTED_NAUTILUS_VERSION
    engine = NautilusSimulatedEngine()
    try:
        strategy = engine.start_strategy()
        position = engine.seed_long_position(
            strategy,
            quantity="0.010",
        )
        old_take_profit = engine.seed_take_profit_order(
            strategy,
            position,
            trigger_price="104000.0",
        )
        engine.attach_real_cancel_bridge(strategy)
        intent = build_intent(
            action=IntentAction.REPLACE_TAKE_PROFITS,
            target_position_id=str(position.id),
            order_plan={
                "position_side": "LONG",
                "take_profits": [
                    {
                        "quantity": "0.004",
                        "price": "105000.0",
                    },
                    {
                        "quantity": "0.006",
                        "trigger_price": "110000.0",
                        "limit_price": "109900.0",
                    },
                ],
            },
        )

        engine.deliver_intent(strategy, intent)
        engine.process_exchange()

        assert old_take_profit.status is OrderStatus.CANCELED
        assert type(old_take_profit.last_event).__name__ == "OrderCanceled"
        open_orders = engine.cache.orders_open(
            instrument_id=engine.instrument.id
        )
        assert len(open_orders) == 2
        replacements = sorted(
            open_orders,
            key=lambda order: str(order.trigger_price),
        )
        assert [
            order.order_type
            for order in replacements
        ] == [
            OrderType.MARKET_IF_TOUCHED,
            OrderType.MARKET_IF_TOUCHED,
        ]
        assert [
            str(order.trigger_price)
            for order in replacements
        ] == [
            "105000.0",
            "110000.0",
        ]
        assert [
            str(order.quantity)
            for order in replacements
        ] == [
            "0.004",
            "0.006",
        ]
        assert all(order.is_reduce_only for order in replacements)
        assert all(
            "lifecycle_role=take_profit" in order.tags
            for order in replacements
        )
        assert all(
            type(order.last_event).__name__ == "OrderAccepted"
            for order in replacements
        )
        assert strategy.denials == []
    finally:
        engine.close()


def test_restart_rebuilds_stop_and_take_profit_lifecycle_from_cache() -> None:
    assert nautilus_version == EXPECTED_NAUTILUS_VERSION
    engine = NautilusSimulatedEngine()
    try:
        first = engine.start_strategy(
            state_name="restart",
            strategy_id="INTENT-RESTART-001",
        )
        position = engine.seed_long_position(
            first,
            quantity="0.010",
        )
        owner_intent_id = uuid4()
        old_stop = engine.seed_stop_order(
            first,
            position,
            intent_id=owner_intent_id,
            trigger_price="95000.0",
        )
        old_take_profit = engine.seed_take_profit_order(
            first,
            position,
            intent_id=owner_intent_id,
            trigger_price="105000.0",
        )
        first._entry_protection_stash[str(owner_intent_id)] = {
            "instrument_id": str(engine.instrument.id),
            "entry_side": "BUY",
            "entry_tags": (
                f"intent_id={owner_intent_id}",
                f"position_id={position.id}",
            ),
            "stop_loss": "95000.0",
            "take_profits": ("105000.0",),
            "take_profit_quantities": ("0.010",),
            "tp_consumed": {},
            "protection_ids": (
                str(old_stop.client_order_id),
                str(old_take_profit.client_order_id),
            ),
            "protection_roles": {
                str(old_stop.client_order_id): {
                    "role": "stop_loss",
                },
                str(old_take_profit.client_order_id): {
                    "role": "take_profit",
                    "take_profit_index": 1,
                },
            },
            "pending_cancel_ids": (),
            "protected_quantity": "0.010",
        }
        assert first._persist_entry_protection_stash() is True
        first.stop()

        restarted = engine.start_strategy(
            state_name="restart",
            strategy_id="INTENT-RESTART-001",
        )

        positions = restarted._position_snapshots(
            str(engine.instrument.id)
        )
        assert len(positions) == 1
        assert positions[0].position_id == str(position.id)
        assert positions[0].side == "LONG"
        cached_orders = restarted._order_snapshots(
            str(engine.instrument.id)
        )
        assert {
            order.client_order_id
            for order in cached_orders
        } == {
            str(old_stop.client_order_id),
            str(old_take_profit.client_order_id),
        }
        recovered = restarted._entry_protection_stash[
            str(owner_intent_id)
        ]
        assert recovered["stop_loss"] == "95000.0"
        assert recovered["take_profits"] == ("105000.0",)
        assert recovered["protected_quantity"] == "0.010"

        engine.attach_real_cancel_bridge(restarted)
        replacement_intent = build_intent(
            action=IntentAction.MOVE_STOP_LOSS,
            target_position_id=str(position.id),
            order_plan={
                "type": "stop_market",
                "stop_price": "98000.0",
                "position_side": "LONG",
            },
        )
        engine.deliver_intent(restarted, replacement_intent)
        engine.process_exchange()

        assert old_stop.status is OrderStatus.CANCELED
        assert old_take_profit.status is OrderStatus.ACCEPTED
        open_orders = engine.cache.orders_open(
            instrument_id=engine.instrument.id
        )
        stop_orders = [
            order
            for order in open_orders
            if order.order_type is OrderType.STOP_MARKET
        ]
        assert len(stop_orders) == 1
        assert str(stop_orders[0].trigger_price) == "98000.0"
        assert stop_orders[0].is_reduce_only is True
        assert restarted.denials == []
    finally:
        engine.close()
