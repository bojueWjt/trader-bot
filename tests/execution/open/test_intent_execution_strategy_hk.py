from __future__ import annotations

from pathlib import Path
import sys

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
from nautilus_trader.model.enums import (  # noqa: E402
    OrderStatus,
    OrderType,
)
from execution_domain.contracts import IntentAction  # noqa: E402


def test_market_open_submits_real_order_in_simulated_engine() -> None:
    assert nautilus_version == EXPECTED_NAUTILUS_VERSION
    engine = NautilusSimulatedEngine()
    try:
        strategy = engine.start_strategy()
        engine.prime_quote()
        intent = build_intent(
            action=IntentAction.OPEN_POSITION,
            order_plan={
                "type": "market",
                "side": "buy",
                "quantity": "0.001",
            },
        )

        engine.deliver_intent(strategy, intent)

        orders = engine.cache.orders()
        assert len(orders) == 1
        order = orders[0]
        assert order.order_type is OrderType.MARKET
        assert order.status is OrderStatus.SUBMITTED
        assert [type(event).__name__ for event in order.events] == [
            "OrderInitialized",
            "OrderSubmitted",
        ]

        engine.process_exchange()

        assert order.status is OrderStatus.FILLED
        assert type(order.last_event).__name__ == "OrderFilled"
        positions = engine.cache.positions_open(
            instrument_id=engine.instrument.id
        )
        assert len(positions) == 1
        assert str(positions[0].quantity) == "0.001"
    finally:
        engine.close()


def test_add_position_submits_real_limit_order_in_simulated_engine() -> None:
    assert nautilus_version == EXPECTED_NAUTILUS_VERSION
    engine = NautilusSimulatedEngine()
    try:
        strategy = engine.start_strategy()
        position = engine.seed_long_position(
            strategy,
            quantity="0.001",
        )
        intent = build_intent(
            action=IntentAction.ADD_POSITION,
            order_plan={
                "type": "limit",
                "side": "buy",
                "quantity": "0.002",
                "price": "99000.0",
            },
        )

        engine.deliver_intent(strategy, intent)
        engine.process_exchange()

        orders = engine.cache.orders_open(
            instrument_id=engine.instrument.id
        )
        assert len(orders) == 1
        order = orders[0]
        assert order.order_type is OrderType.LIMIT
        assert order.status is OrderStatus.ACCEPTED
        assert str(order.price) == "99000.0"
        assert str(order.quantity) == "0.002"
        assert order.is_reduce_only is False
        assert type(order.last_event).__name__ == "OrderAccepted"
        assert str(position.quantity) == "0.001"
    finally:
        engine.close()
