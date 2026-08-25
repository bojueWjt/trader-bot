from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from execution_domain.control_plane import CommandType, NodeCommand  # noqa: E402
from runtime.exchange_cancel_adapter import TerminalExchangeWorker  # noqa: E402
from runtime.intent_execution_inbox import (  # noqa: E402
    IntentExecutionIdentity,
)
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
    def __init__(
        self,
        *,
        environment: str = "testnet",
        state_dir: Path | None = None,
    ) -> None:
        if state_dir is None:
            state_dir = Path(tempfile.mkdtemp())
        inbox_path = ""
        canary_path = ""
        inbox_path = str(state_dir / "intent-execution-inbox.json")
        canary_path = str(state_dir / "live-canary-execution.json")
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id="account-b",
                node_id="node-b",
                environment=environment,
                intent_execution_inbox_path=inbox_path,
                live_canary_execution_path=canary_path,
            )
        )
        self.message_bus = _MessageBus()
        self.orders: list[Any] = []
        self.positions: list[Any] = []
        self.cancelled: list[str] = []
        self.closed: list[str] = []
        self.submitted_plans: list[Any] = []

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

    def _submit_order_plan(self, plan: Any, **_kwargs: Any) -> bool:
        self.submitted_plans.append(plan)
        return True


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


def test_cancel_all_preserves_durable_entries_and_protection_only() -> None:
    with tempfile.TemporaryDirectory() as state_dir:
        strategy = _Strategy(
            environment="live",
            state_dir=Path(state_dir),
        )
        intent_id = uuid4()
        durable_order_ids = tuple(
            f"B{intent_id.hex}{sequence:02d}"
            for sequence in (1, 2, 3)
        )
        valid_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        identity = IntentExecutionIdentity(
            account_id="account-b",
            intent_id=str(intent_id),
            idempotency_key="a" * 64,
            instrument_id="SOLUSDT-PERP.BINANCE",
            action="open_position",
        )
        strategy._intent_execution_inbox.register_received(
            identity,
            {
                "schema_version": "1.0",
                "intent_id": str(intent_id),
                "idempotency_key": "a" * 64,
                "account_id": "account-b",
                "instrument_id": "SOLUSDT-PERP.BINANCE",
                "action": "open_position",
                "valid_until": valid_until.isoformat(),
                "order_plan": {
                    "type": "zone_ladder",
                    "side": "buy",
                    "tranches": [
                        {"seq": 1, "quantity": "0.4", "price": "100"},
                        {"seq": 2, "quantity": "0.3", "price": "99"},
                        {"seq": 3, "quantity": "0.2", "price": "98"},
                    ],
                },
            },
        )
        strategy._intent_execution_inbox.begin_dispatch(
            identity,
            durable_order_ids,
        )
        strategy._intent_execution_inbox.mark_exchange_confirmed(identity)
        protection_id = f"B{intent_id.hex}11"
        orphan_id = ROBOT_BTC_ORDER_ID
        orders = [
            _exchange_order(
                "regular",
                durable_order_ids[0],
                "1001",
                quantity="0.4",
                price="100",
            ),
            _exchange_order(
                "regular",
                durable_order_ids[1],
                "1002",
                quantity="0.3",
                price="99",
            ),
            _exchange_order(
                "regular",
                durable_order_ids[2],
                "1003",
                quantity="0.2",
                price="98",
            ),
            _exchange_order(
                "algo",
                protection_id,
                "2001",
                order_type="STOP_MARKET",
                reduce_only=True,
            ),
            _exchange_order(
                "regular",
                orphan_id,
                "3001",
                quantity="0.1",
                price="97",
            ),
        ]
        mirror = _Mirror(orders)
        adapter = _Adapter()
        worker = TerminalExchangeWorker(
            account_id="account-b",
            mirror=mirror,
            adapter=adapter,
            result_publisher=strategy.enqueue_terminal_exchange_result,
            capacity=4,
            total_deadline_seconds=1,
        )
        strategy.set_exchange_cancel_adapter(adapter, mirror)
        strategy.set_terminal_exchange_worker(worker)
        worker.start()
        original_ladder = [
            (order.client_order_id, order.quantity, order.price)
            for order in orders[:3]
        ]
        try:
            strategy._on_node_command(
                _command("preserve-durable", CommandType.CANCEL_ALL)
            )
            assert worker.wait_empty(timeout_seconds=1)
            strategy.drain_terminal_exchange_mailbox()
        finally:
            worker.stop()

        assert [request.client_order_id for request in adapter.requests] == [
            orphan_id
        ]
        assert [
            (order.client_order_id, order.quantity, order.price)
            for order in orders[:3]
        ] == original_ladder
        payload = strategy.message_bus.messages[0][1]
        preserved = [
            operation
            for operation in payload["operations"]
            if operation["status"] == "preserved"
        ]
        assert [item["client_order_id"] for item in preserved] == [
            *durable_order_ids,
            protection_id,
        ]
        assert [item["outcome"] for item in preserved] == [
            "durable_entry_preserved",
            "durable_entry_preserved",
            "durable_entry_preserved",
            "protective_order_preserved",
        ]
        assert [
            (item["price"], item["quantity"])
            for item in preserved[:3]
        ] == [
            ("100", "0.4"),
            ("99", "0.3"),
            ("98", "0.2"),
        ]
        assert payload["errors"] == []


def test_cancel_all_cancels_durable_entry_with_exchange_quantity_drift() -> None:
    with tempfile.TemporaryDirectory() as state_dir:
        strategy = _Strategy(
            environment="live",
            state_dir=Path(state_dir),
        )
        intent_id = uuid4()
        client_order_id = f"B{intent_id.hex}01"
        identity = IntentExecutionIdentity(
            account_id="account-b",
            intent_id=str(intent_id),
            idempotency_key="b" * 64,
            instrument_id="SOLUSDT-PERP.BINANCE",
            action="open_position",
        )
        strategy._intent_execution_inbox.register_received(
            identity,
            {
                "intent_id": str(intent_id),
                "idempotency_key": "b" * 64,
                "account_id": "account-b",
                "instrument_id": "SOLUSDT-PERP.BINANCE",
                "action": "open_position",
                "valid_until": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
                "order_plan": {
                    "type": "limit",
                    "side": "buy",
                    "quantity": "0.4",
                    "price": "100",
                },
            },
        )
        strategy._intent_execution_inbox.begin_dispatch(
            identity,
            (client_order_id,),
        )
        strategy._intent_execution_inbox.mark_exchange_confirmed(identity)
        mirror = _Mirror(
            [
                _exchange_order(
                    "regular",
                    client_order_id,
                    "1001",
                    quantity="0.5",
                    price="100",
                )
            ]
        )
        adapter = _Adapter()
        worker = TerminalExchangeWorker(
            account_id="account-b",
            mirror=mirror,
            adapter=adapter,
            result_publisher=strategy.enqueue_terminal_exchange_result,
            capacity=4,
            total_deadline_seconds=1,
        )
        strategy.set_exchange_cancel_adapter(adapter, mirror)
        strategy.set_terminal_exchange_worker(worker)
        worker.start()
        try:
            strategy._on_node_command(
                _command("cancel-drifted-durable", CommandType.CANCEL_ALL)
            )
            assert worker.wait_empty(timeout_seconds=1)
            strategy.drain_terminal_exchange_mailbox()
        finally:
            worker.stop()

        assert [request.client_order_id for request in adapter.requests] == [
            client_order_id
        ]
        payload = strategy.message_bus.messages[0][1]
        assert payload["operations"][0]["status"] == "confirmed"
        assert payload["operations"][0]["outcome"] == "canceled"
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
    strategy._entry_protection_stash["owned-sol"] = {
        "instrument_id": "SOLUSDT-PERP.BINANCE",
        "entry_side": "BUY",
        "protected_quantity": "0.1",
    }

    strategy._on_node_command(_command("close-all", CommandType.CLOSE_ALL))

    assert strategy.cancelled == [ROBOT_SOL_ORDER_ID]
    assert strategy.closed == []
    assert len(strategy.submitted_plans) == 1
    close_plan = strategy.submitted_plans[0]
    assert close_plan.reduce_only is True
    assert close_plan.side == "SELL"
    assert close_plan.quantity == "0.2"
    assert "user_directed=true" in close_plan.tags
    payload = strategy.message_bus.messages[0][1]
    close_operations = [
        item
        for item in payload["operations"]
        if item["kind"] == "close_position"
    ]
    assert len(close_operations) == 1
    assert close_operations[0]["reduce_only"] is True
    assert close_operations[0]["position_id"] == "sol-position"
    assert close_operations[0]["status"] == "submitted"
    assert close_operations[0]["outcome"] == "user_directed_account_close"
    assert close_operations[0]["robot_owned_quantity"] == "0.1"
    assert close_operations[0]["user_directed_quantity"] == "0.1"


def test_close_all_rejects_channel_authorization() -> None:
    strategy = _Strategy()
    strategy.positions = [
        SimpleNamespace(
            id="sol-position",
            instrument_id="SOLUSDT-PERP.BINANCE",
            quantity="0.2",
            side="LONG",
        )
    ]

    strategy._on_node_command(
        _command(
            "channel-close-all",
            CommandType.CLOSE_ALL,
            authorized_by_type="channel",
        )
    )

    assert strategy.submitted_plans == []
    assert strategy.message_bus.messages == []
    assert strategy.denials[-1].reason == "user_authorization_required"


def _command(
    command_id: str,
    command_type: CommandType,
    *,
    authorized_by_type: str = "user",
) -> NodeCommand:
    authorization = dict(AUTHORIZATION)
    authorization["authorized_by_type"] = authorized_by_type
    return NodeCommand(
        command_id=command_id,
        type=command_type,
        args={
            "account_id": "account-b",
            "instrument_ids": ["SOLUSDT-PERP.BINANCE"],
            "authorization": authorization,
        },
    )


def _exchange_order(
    order_kind: str,
    client_order_id: str,
    venue_order_id: str,
    *,
    symbol: str = "SOLUSDT",
    order_type: str = "LIMIT",
    quantity: str = "0.1",
    price: str = "100",
    reduce_only: bool = False,
) -> Any:
    return SimpleNamespace(
        account_id="account-b",
        symbol=symbol,
        instrument_id=f"{symbol}-PERP.BINANCE",
        position_side="LONG",
        order_kind=order_kind,
        venue_order_id=venue_order_id,
        client_order_id=client_order_id,
        order_type=order_type,
        side="BUY",
        quantity=quantity,
        price=price,
        time_in_force="GTC",
        reduce_only=reduce_only,
    )
