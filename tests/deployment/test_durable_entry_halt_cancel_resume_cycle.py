from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_API = REPO_ROOT / "services" / "control-plane" / "api"
NAUTILUS_SERVICE = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN = REPO_ROOT / "packages" / "execution-domain"
for module_path in (
    CONTROL_PLANE_API,
    NAUTILUS_SERVICE,
    EXECUTION_DOMAIN,
):
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

import read_api  # noqa: E402
from app.nautilus_actors import CommandPollerActor  # noqa: E402
from execution_domain.control_plane import (  # noqa: E402
    CommandAckStatus,
    CommandType,
    NodeCommand,
)
from runtime.exchange_cancel_adapter import TerminalExchangeWorker  # noqa: E402
from runtime.intent_execution_inbox import IntentExecutionIdentity  # noqa: E402
from strategy.intent_execution_strategy import (  # noqa: E402
    IntentExecutionStrategy,
    IntentExecutionStrategyConfig,
)


ACCOUNT_ID = "account-a"
INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
SYMBOL = "BTCUSDT"
AUTHORIZATION = {
    "authorized_by_type": "user",
    "authorized_by_id": "risk-admin",
    "source_message_id": "deployment-cycle-test",
}


class _MessageBus:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def publish(self, topic: str, msg: dict[str, Any]) -> None:
        del topic
        self.messages.append(msg)


class _CycleLifecycle:
    def __init__(self) -> None:
        self.trading_state = "ACTIVE"
        self.config = SimpleNamespace()

    def apply_operator_state(self, state: Any, reason: str) -> None:
        del reason
        self.trading_state = str(getattr(state, "value", state))


class _CommandDispatchBus:
    def __init__(self, strategy: _Strategy) -> None:
        self.strategy = strategy

    def publish(self, topic: str, msg: NodeCommand) -> None:
        assert topic == f"node.commands.{ACCOUNT_ID}"
        self.strategy._on_node_command(msg)


class _CycleCommandActor(CommandPollerActor):
    def __init__(
        self,
        lifecycle: _CycleLifecycle,
        strategy: _Strategy,
    ) -> None:
        self._lifecycle = lifecycle
        self._account_id = ACCOUNT_ID
        self._bound_message_bus = _CommandDispatchBus(strategy)

    def _message_bus(self) -> Any:
        return self._bound_message_bus


class _Strategy(IntentExecutionStrategy):
    def __init__(self, state_dir: Path) -> None:
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id=ACCOUNT_ID,
                node_id="node-a",
                environment="live",
                intent_execution_inbox_path=str(
                    state_dir / "intent-execution-inbox.json"
                ),
                live_canary_execution_path=str(
                    state_dir / "live-canary-execution.json"
                ),
            )
        )
        self.message_bus = _MessageBus()

    def _publish_terminal_command_result(
        self,
        payload: dict[str, Any],
    ) -> None:
        self.message_bus.publish("node.command-results.account-a", payload)


class _Mirror:
    def __init__(self, orders: list[Any]) -> None:
        self.orders = orders

    def refresh(self, **_kwargs: Any) -> tuple[Any, ...]:
        return tuple(self.orders)


class _Adapter:
    def __init__(self, mirror: _Mirror) -> None:
        self.mirror = mirror
        self.canceled: list[str] = []

    def cancel(self, action: str, request: Any, **_kwargs: Any) -> Any:
        assert action == "cancel_order"
        client_order_id = str(request.client_order_id)
        self.canceled.append(client_order_id)
        self.mirror.orders = [
            order
            for order in self.mirror.orders
            if str(order.client_order_id) != client_order_id
        ]
        return SimpleNamespace(
            outcome="canceled",
            terminal_status="CANCELED",
        )


class _ResumeCursor:
    def __init__(
        self,
        orders: list[Any],
        *,
        intent_id: str | None,
        valid_until: datetime,
    ) -> None:
        self.orders = orders
        self.intent_id = intent_id
        self.valid_until = valid_until
        self.query = ""
        self.params: tuple[Any, ...] = ()

    def execute(self, query: str, params: tuple[Any, ...]) -> None:
        self.query = query
        self.params = params

    def fetchone(self) -> tuple[Any, ...] | None:
        if "FROM trade_intents" in self.query and "JOIN" not in self.query:
            # Intent-backed fallback for orders missing from the projection:
            # an orphan has no intent row either.
            return None
        if "JOIN trade_intents AS intent" not in self.query:
            raise AssertionError(f"unexpected fetchone query: {self.query}")
        if self.intent_id is None:
            return None
        client_order_id = str(self.params[1])
        order = next(
            item
            for item in self.orders
            if str(item.client_order_id) == client_order_id
        )
        return (
            self.intent_id,
            "working",
            INSTRUMENT_ID,
            "long",
            "LIMIT",
            order.quantity,
            order.price,
            False,
            {"side": "BUY", "reduce_only": False},
            "approved",
            "open_position",
            INSTRUMENT_ID,
            self.valid_until,
            datetime.now(timezone.utc),
        )

    def fetchall(self) -> list[tuple[Any, ...]]:
        if "FROM orders_projection" not in self.query:
            raise AssertionError(f"unexpected fetchall query: {self.query}")
        return [
            (
                order.client_order_id,
                "working",
                "LIMIT",
                False,
                {"side": "BUY", "reduce_only": False},
            )
            for order in self.orders
        ]


def test_halt_cancel_resume_preserves_durable_ladder_exactly() -> None:
    with tempfile.TemporaryDirectory() as state_dir:
        strategy = _Strategy(Path(state_dir))
        intent_id = uuid4()
        valid_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        order_specs = (
            (1, "0.4", "100"),
            (2, "0.3", "99"),
            (3, "0.2", "98"),
        )
        durable_order_ids = tuple(
            f"B{intent_id.hex}{sequence:02d}"
            for sequence, _quantity, _price in order_specs
        )
        identity = IntentExecutionIdentity(
            account_id=ACCOUNT_ID,
            intent_id=str(intent_id),
            idempotency_key="d" * 64,
            instrument_id=INSTRUMENT_ID,
            action="open_position",
        )
        strategy._intent_execution_inbox.register_received(
            identity,
            {
                "intent_id": str(intent_id),
                "idempotency_key": "d" * 64,
                "account_id": ACCOUNT_ID,
                "instrument_id": INSTRUMENT_ID,
                "action": "open_position",
                "valid_until": valid_until.isoformat(),
                "order_plan": {
                    "type": "zone_ladder",
                    "side": "buy",
                    "tranches": [
                        {
                            "seq": sequence,
                            "quantity": quantity,
                            "price": price,
                        }
                        for sequence, quantity, price in order_specs
                    ],
                },
            },
        )
        strategy._intent_execution_inbox.begin_dispatch(
            identity,
            durable_order_ids,
        )
        strategy._intent_execution_inbox.mark_exchange_confirmed(identity)
        orders = [
            _exchange_order(client_order_id, venue_order_id, quantity, price)
            for (
                client_order_id,
                venue_order_id,
                (_sequence, quantity, price),
            ) in zip(
                durable_order_ids,
                ("1001", "1002", "1003"),
                order_specs,
            )
        ]
        orphan_id = "B" + ("f" * 32) + "09"
        orders.append(_exchange_order(orphan_id, "9009", "0.1", "97"))
        mirror = _Mirror(orders)
        adapter = _Adapter(mirror)
        worker = TerminalExchangeWorker(
            account_id=ACCOUNT_ID,
            mirror=mirror,
            adapter=adapter,
            result_publisher=strategy.enqueue_terminal_exchange_result,
            capacity=4,
            total_deadline_seconds=1,
        )
        strategy.set_exchange_cancel_adapter(adapter, mirror)
        strategy.set_terminal_exchange_worker(worker)
        lifecycle = _CycleLifecycle()
        command_actor = _CycleCommandActor(lifecycle, strategy)
        baseline = [
            (order.client_order_id, order.quantity, order.price)
            for order in orders[:3]
        ]

        halt_status, halt_error = command_actor._apply(_halt_command())
        assert halt_status is CommandAckStatus.COMPLETED
        assert halt_error is None
        assert lifecycle.trading_state == "HALTED"
        worker.start()
        try:
            cancel_status, cancel_error = command_actor._apply(
                _cancel_all_command()
            )
            assert cancel_status is CommandAckStatus.RUNNING
            assert cancel_error is None
            assert worker.wait_empty(timeout_seconds=1)
            strategy.drain_terminal_exchange_mailbox()
        finally:
            worker.stop()

        assert adapter.canceled == [orphan_id]
        remaining_ladder = [
            (order.client_order_id, order.quantity, order.price)
            for order in mirror.orders
        ]
        assert remaining_ladder == baseline

        cursor = _ResumeCursor(
            mirror.orders,
            intent_id=str(intent_id),
            valid_until=valid_until,
        )
        exemptions = read_api._validate_owned_orders_terminal(
            cursor,
            heartbeat={
                "regular_orders": [
                    _heartbeat_order(order) for order in mirror.orders
                ],
                "algo_orders": [],
            },
            account_id=ACCOUNT_ID,
        )
        resume_status, resume_error = command_actor._apply(
            _resume_command()
        )

        assert resume_status is CommandAckStatus.COMPLETED
        assert resume_error is None
        assert lifecycle.trading_state == "ACTIVE"
        assert [item["client_order_id"] for item in exemptions] == list(
            durable_order_ids
        )
        command_result = strategy.message_bus.messages[0]
        assert [
            operation["outcome"]
            for operation in command_result["operations"]
        ] == [
            "durable_entry_preserved",
            "durable_entry_preserved",
            "durable_entry_preserved",
            "canceled",
        ]


def test_resume_gate_allows_expired_exchange_accepted_durable_entry() -> None:
    intent_id = str(uuid4())
    order = _exchange_order(
        f"B{UUID(intent_id).hex}01",
        "7001",
        "0.4",
        "100",
    )
    cursor = _ResumeCursor(
        [order],
        intent_id=intent_id,
        valid_until=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    exemptions = read_api._validate_owned_orders_terminal(
        cursor,
        heartbeat={
            "regular_orders": [_heartbeat_order(order)],
            "algo_orders": [],
        },
        account_id=ACCOUNT_ID,
    )

    assert [item["client_order_id"] for item in exemptions] == [
        order.client_order_id
    ]


def test_resume_gate_still_blocks_robot_orphan_order() -> None:
    orphan = _exchange_order(
        "B" + ("e" * 32) + "01",
        "8001",
        "0.1",
        "101",
    )
    cursor = _ResumeCursor(
        [orphan],
        intent_id=None,
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    with pytest.raises(
        read_api.HTTPException,
        match="robot-owned orders are not terminal",
    ):
        read_api._validate_owned_orders_terminal(
            cursor,
            heartbeat={
                "regular_orders": [_heartbeat_order(orphan)],
                "algo_orders": [],
            },
            account_id=ACCOUNT_ID,
        )


def _exchange_order(
    client_order_id: str,
    venue_order_id: str,
    quantity: str,
    price: str,
) -> Any:
    return SimpleNamespace(
        account_id=ACCOUNT_ID,
        symbol=SYMBOL,
        instrument_id=INSTRUMENT_ID,
        position_side="LONG",
        order_kind="regular",
        venue_order_id=venue_order_id,
        client_order_id=client_order_id,
        order_type="LIMIT",
        side="BUY",
        quantity=quantity,
        price=price,
        time_in_force="GTC",
        reduce_only=False,
    )


def _heartbeat_order(order: Any) -> dict[str, Any]:
    return {
        "symbol": SYMBOL,
        "client_order_id": order.client_order_id,
        "order_type": "LIMIT",
        "order_kind": "regular",
        "side": "BUY",
        "quantity": order.quantity,
        "price": order.price,
        "time_in_force": "GTC",
        "reduce_only": False,
    }


def _cancel_all_command() -> NodeCommand:
    return NodeCommand(
        command_id="deployment-cycle-cancel-all",
        type=CommandType.CANCEL_ALL,
        args={
            "account_id": ACCOUNT_ID,
            "instrument_ids": [INSTRUMENT_ID],
            "authorization": dict(AUTHORIZATION),
        },
    )


def _halt_command() -> NodeCommand:
    return NodeCommand(
        command_id="deployment-cycle-halt",
        type=CommandType.HALT,
        args={
            "account_id": ACCOUNT_ID,
            "authorization": dict(AUTHORIZATION),
        },
    )


def _resume_command() -> NodeCommand:
    return NodeCommand(
        command_id="deployment-cycle-resume",
        type=CommandType.RESUME,
        args={
            "account_id": ACCOUNT_ID,
            "authorization": dict(AUTHORIZATION),
        },
    )
