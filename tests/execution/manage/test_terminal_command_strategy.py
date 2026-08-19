from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from execution_domain.control_plane import CommandType, NodeCommand  # noqa: E402
from strategy.intent_execution_strategy import (  # noqa: E402
    IntentExecutionStrategy,
    IntentExecutionStrategyConfig,
)


AUTHORIZATION = {
    "authorized_by_type": "user",
    "authorized_by_id": "risk-admin",
    "source_message_id": "terminal-strategy-test",
}
ROBOT_SOL_ORDER_ID = "Baaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01"
ROBOT_SOL_ALGO_ID = "Bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb02"
ROBOT_BTC_ORDER_ID = "Bcccccccccccccccccccccccccccccccc03"


class _MessageBus:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, msg: dict[str, Any]) -> None:
        self.messages.append((topic, msg))


class _Strategy(IntentExecutionStrategy):
    def __init__(self, *, environment: str = "testnet") -> None:
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id="account-b",
                node_id="node-b",
                environment=environment,
            )
        )
        self.message_bus = _MessageBus()
        self.orders: list[Any] = []
        self.positions: list[Any] = []
        self.cancelled: list[str] = []
        self.closed: list[str] = []

    def _all_open_orders(self) -> tuple[Any, ...]:
        return tuple(self.orders)

    def _all_open_positions(self) -> tuple[Any, ...]:
        return tuple(self.positions)

    def _publish_terminal_command_result(
        self,
        payload: dict[str, Any],
    ) -> None:
        self.message_bus.publish(
            topic=f"node.command-results.{self.config.account_id}",
            msg=payload,
        )

    def cancel_order(self, order: Any) -> None:
        self.cancelled.append(str(order.client_order_id))

    def close_position(self, position: Any) -> None:
        self.closed.append(str(position.id))


class _Mirror:
    def __init__(self, orders: list[Any]) -> None:
        self.orders = orders
        self.refreshes = 0

    def refresh(self) -> tuple[Any, ...]:
        self.refreshes += 1
        return tuple(self.orders)


class _Adapter:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def cancel(self, action: str, request: Any) -> Any:
        assert action == "cancel_order"
        self.requests.append(request)
        return SimpleNamespace(
            outcome="canceled",
            terminal_status="CANCELED",
        )


def test_cancel_all_uses_exchange_refs_for_regular_and_algo_orders() -> None:
    strategy = _Strategy(environment="live")
    orders = [
        _exchange_order("regular", ROBOT_SOL_ORDER_ID, "1001"),
        _exchange_order("algo", ROBOT_SOL_ALGO_ID, "2001"),
        _exchange_order(
            "regular",
            ROBOT_BTC_ORDER_ID,
            "3001",
            symbol="BTCUSDT",
        ),
    ]
    mirror = _Mirror(orders)
    adapter = _Adapter()
    strategy.set_exchange_cancel_adapter(adapter, mirror)

    strategy._on_node_command(_command("cancel-all", CommandType.CANCEL_ALL))

    assert mirror.refreshes == 1
    assert [request.order_kind for request in adapter.requests] == [
        "regular",
        "algo",
    ]
    payload = strategy.message_bus.messages[0][1]
    assert payload["command_id"] == "cancel-all"
    assert [item["status"] for item in payload["operations"]] == [
        "confirmed",
        "confirmed",
    ]
    assert payload["errors"] == []


def test_close_all_filters_scope_and_records_reduce_only_requests() -> None:
    strategy = _Strategy()
    sol_order = SimpleNamespace(
        client_order_id=ROBOT_SOL_ORDER_ID,
        instrument_id="SOLUSDT-PERP.BINANCE",
    )
    btc_order = SimpleNamespace(
        client_order_id=ROBOT_BTC_ORDER_ID,
        instrument_id="BTCUSDT-PERP.BINANCE",
    )
    sol_position = SimpleNamespace(
        id="sol-position",
        instrument_id="SOLUSDT-PERP.BINANCE",
        quantity="0.2",
        side="LONG",
    )
    btc_position = SimpleNamespace(
        id="btc-position",
        instrument_id="BTCUSDT-PERP.BINANCE",
        quantity="0.1",
        side="LONG",
    )
    strategy.orders = [sol_order, btc_order]
    strategy.positions = [sol_position, btc_position]

    strategy._on_node_command(_command("close-all", CommandType.CLOSE_ALL))

    assert strategy.cancelled == [ROBOT_SOL_ORDER_ID]
    assert strategy.closed == []
    payload = strategy.message_bus.messages[0][1]
    close_operations = [
        item
        for item in payload["operations"]
        if item["kind"] == "close_position"
    ]
    assert len(close_operations) == 1
    assert close_operations[0]["reduce_only"] is True
    assert close_operations[0]["position_id"] == "sol-position"
    assert close_operations[0]["status"] == "skipped"
    assert close_operations[0]["outcome"] == "manual_position_read_only"


def _command(command_id: str, command_type: CommandType) -> NodeCommand:
    return NodeCommand(
        command_id=command_id,
        type=command_type,
        args={
            "account_id": "account-b",
            "instrument_ids": ["SOLUSDT-PERP.BINANCE"],
            "authorization": AUTHORIZATION,
        },
    )


def _exchange_order(
    order_kind: str,
    client_order_id: str,
    venue_order_id: str,
    *,
    symbol: str = "SOLUSDT",
) -> Any:
    return SimpleNamespace(
        account_id="account-b",
        symbol=symbol,
        instrument_id=f"{symbol}-PERP.BINANCE",
        position_side="LONG",
        order_kind=order_kind,
        venue_order_id=venue_order_id,
        client_order_id=client_order_id,
    )
