from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

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
    OrderSide,
    OrderStatus,
    OrderType,
)
from nautilus_trader.model.identifiers import Venue  # noqa: E402
from risk.config import (  # noqa: E402
    RiskLimitConfig,
    build_risk_engine_config,
)


def test_margin_risk_engine_cap_is_bypassed_and_live_strategy_fails_closed() -> None:
    """Nautilus 1.227.0 bypasses pre-trade balance/notional risk for MARGIN.

    Keep the version behavior visible, then prove the node's independent live
    entry cap blocks the same order before exchange submission.
    """
    assert nautilus_version == EXPECTED_NAUTILUS_VERSION
    risk_config = build_risk_engine_config(
        RiskLimitConfig(
            max_notional_per_order={
                "BTCUSDT-PERP.BINANCE": "100",
            },
            max_order_submit_rate="10/00:00:01",
            max_order_modify_rate="10/00:00:01",
        )
    )
    margin_engine = NautilusSimulatedEngine(
        risk_config=risk_config
    )
    try:
        strategy = margin_engine.start_strategy()
        margin_engine.prime_quote()
        intent = build_intent(
            action=IntentAction.OPEN_POSITION,
            order_plan={
                "type": "limit",
                "side": "buy",
                "quantity": "0.002",
                "price": "100000.0",
            },
            max_notional=1_000_000.0,
        )

        margin_engine.deliver_intent(strategy, intent)

        orders = margin_engine.cache.orders()
        assert len(orders) == 1
        order = orders[0]
        assert order.status is OrderStatus.SUBMITTED
        assert type(order.last_event).__name__ == "OrderSubmitted"
        assert margin_engine.risk_engine.max_notional_per_order(
            margin_engine.instrument.id
        ) == 100
        account = margin_engine.portfolio.account(Venue("BINANCE"))
        assert account is not None
        assert account.is_margin_account is True
    finally:
        margin_engine.close()

    fail_closed_engine = NautilusSimulatedEngine(
        risk_config=risk_config
    )
    try:
        instrument_id = str(fail_closed_engine.instrument.id)
        strategy = fail_closed_engine.start_strategy(
            account_id="account-b",
            environment="live",
            live_entry_notional_inventory=(
                (instrument_id, "100"),
            ),
        )
        fail_closed_engine.prime_quote()
        intent = build_intent(
            account_id="account-b",
            action=IntentAction.OPEN_POSITION,
            order_plan={
                "type": "limit",
                "side": "buy",
                "quantity": "0.002",
                "price": "100000.0",
            },
            max_notional=1_000_000.0,
        )

        fail_closed_engine.deliver_intent(strategy, intent)

        assert fail_closed_engine.cache.orders() == []
        assert strategy.denials[-1].reason == (
            "live_entry_notional_exceeded"
        )
        assert "actual=200.0000" in strategy.denials[-1].detail
        assert "cap=100" in strategy.denials[-1].detail
    finally:
        fail_closed_engine.close()


def test_emergency_cancel_all_and_reduce_only_close_all_confirm_events() -> None:
    assert nautilus_version == EXPECTED_NAUTILUS_VERSION
    engine = NautilusSimulatedEngine()
    try:
        strategy = engine.start_strategy()
        position_events: list[object] = []
        engine.msgbus.subscribe(
            topic="events.position.*",
            handler=position_events.append,
        )
        position = engine.seed_long_position(
            strategy,
            quantity="0.010",
        )
        add_intent = build_intent(
            action=IntentAction.ADD_POSITION,
            order_plan={
                "type": "limit",
                "side": "buy",
                "quantity": "0.001",
                "price": "99000.0",
            },
        )
        engine.deliver_intent(strategy, add_intent)
        engine.process_exchange()
        open_orders = engine.cache.orders_open(
            instrument_id=engine.instrument.id
        )
        assert len(open_orders) == 1
        pending_order = open_orders[0]
        assert pending_order.status is OrderStatus.ACCEPTED

        results: list[dict[str, object]] = []
        engine.msgbus.subscribe(
            topic="node.command-results.account-a",
            handler=results.append,
        )
        command = SimpleNamespace(
            command_id="emergency-close-001",
            type="close_all",
            args={
                "account_id": "account-a",
                "instrument_ids": [
                    str(engine.instrument.id),
                ],
                "authorization": {
                    "authorized_by_type": "user",
                    "authorized_by_id": "risk-operator",
                    "source_message_id": "emergency-close-001",
                },
            },
        )
        known_order_ids = {
            str(order.client_order_id)
            for order in engine.cache.orders()
        }

        engine.msgbus.publish(
            topic="node.commands.account-a",
            msg=command,
            external_pub=False,
        )
        engine.process_exchange()

        assert len(results) == 1
        result = results[0]
        assert result["command_id"] == "emergency-close-001"
        assert result["errors"] == []
        assert {
            operation["kind"]
            for operation in result["operations"]
        } == {
            "cancel_order",
            "close_position",
        }
        assert pending_order.status is OrderStatus.CANCELED
        assert type(pending_order.last_event).__name__ == "OrderCanceled"
        close_orders = [
            order
            for order in engine.cache.orders()
            if str(order.client_order_id) not in known_order_ids
        ]
        assert len(close_orders) == 1
        close_order = close_orders[0]
        assert close_order.order_type is OrderType.MARKET
        assert close_order.side is OrderSide.SELL
        assert close_order.is_reduce_only is True
        assert close_order.status is OrderStatus.FILLED
        assert type(close_order.last_event).__name__ == "OrderFilled"
        assert engine.cache.positions_open(
            instrument_id=engine.instrument.id
        ) == []
        closed_events = [
            event
            for event in position_events
            if type(event).__name__ == "PositionClosed"
        ]
        assert len(closed_events) == 1
        assert str(closed_events[0].position_id) == str(position.id)
        assert str(closed_events[0].closing_order_id) == str(
            close_order.client_order_id
        )
    finally:
        engine.close()
