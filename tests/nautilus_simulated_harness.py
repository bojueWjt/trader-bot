from __future__ import annotations

import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
NAUTILUS_ADAPTER_ROOT = REPO_ROOT / "packages" / "nautilus-adapter"

for root in (
    SERVICE_ROOT,
    EXECUTION_DOMAIN_ROOT,
    NAUTILUS_ADAPTER_ROOT,
):
    value = str(root)
    if value not in sys.path:
        sys.path.insert(0, value)

from execution_domain.contracts import (  # noqa: E402
    ApprovedTradeIntentV1,
    IntentAction,
    RiskBudget,
)
from intent.custom_data import build_nautilus_custom_data  # noqa: E402
from nautilus_trader import __version__ as nautilus_version  # noqa: E402
from nautilus_trader.backtest.engine import SimulatedExchange  # noqa: E402
from nautilus_trader.backtest.execution_client import (  # noqa: E402
    BacktestExecClient,
)
from nautilus_trader.backtest.models import (  # noqa: E402
    FillModel,
    LatencyModel,
    MakerTakerFeeModel,
)
from nautilus_trader.common.component import (  # noqa: E402
    MessageBus,
    TestClock,
)
from nautilus_trader.config import ExecEngineConfig, RiskEngineConfig  # noqa: E402
from nautilus_trader.data.engine import DataEngine  # noqa: E402
from nautilus_trader.execution.engine import ExecutionEngine  # noqa: E402
from nautilus_trader.model.currencies import USDT  # noqa: E402
from nautilus_trader.model.enums import (  # noqa: E402
    AccountType,
    OmsType,
    OrderStatus,
)
from nautilus_trader.model.identifiers import (  # noqa: E402
    ClientOrderId,
    Venue,
)
from nautilus_trader.model.objects import Money, Price, Quantity  # noqa: E402
from nautilus_trader.portfolio.portfolio import Portfolio  # noqa: E402
from nautilus_trader.risk.engine import RiskEngine  # noqa: E402
from nautilus_trader.test_kit.providers import TestInstrumentProvider  # noqa: E402
from nautilus_trader.test_kit.stubs.component import (  # noqa: E402
    TestComponentStubs,
)
from nautilus_trader.test_kit.stubs.data import TestDataStubs  # noqa: E402
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs  # noqa: E402
from strategy.intent_execution_planner import encode_client_order_id  # noqa: E402
from strategy.intent_execution_strategy import (  # noqa: E402
    IntentExecutionStrategy,
    IntentExecutionStrategyConfig,
)


EXPECTED_NAUTILUS_VERSION = "1.227.0"
FIXED_TIME_NS = 1_786_233_600_000_000_000


def build_intent(
    *,
    action: IntentAction | str,
    order_plan: dict[str, Any],
    account_id: str = "account-a",
    instrument_id: str = "BTCUSDT-PERP.BINANCE",
    target_position_id: str | None = None,
    intent_id: UUID | None = None,
    max_notional: float = 1_000_000.0,
) -> ApprovedTradeIntentV1:
    created_intent_id = intent_id
    if created_intent_id is None:
        created_intent_id = uuid4()
    now = datetime.fromtimestamp(FIXED_TIME_NS / 1_000_000_000, tz=timezone.utc)
    payload = dict(order_plan)
    payload.setdefault(
        "authorization",
        {
            "authorized_by_type": "user",
            "authorized_by_id": "nautilus-sim-test",
            "source_message_id": f"sim-{created_intent_id}",
        },
    )
    return ApprovedTradeIntentV1(
        schema_version="1.0",
        intent_id=created_intent_id,
        decision_id=uuid4(),
        risk_decision_id=uuid4(),
        account_id=account_id,
        instrument_id=instrument_id,
        action=action,
        order_plan=payload,
        risk_budget=RiskBudget(
            risk_fraction=0.01,
            max_notional=max_notional,
            max_leverage=2.0,
        ),
        target_position_id=target_position_id,
        valid_until=now + timedelta(minutes=5),
        idempotency_key=sha256(
            str(created_intent_id).encode("ascii")
        ).hexdigest(),
        approved_at=now,
    )


class NautilusSimulatedEngine:
    def __init__(
        self,
        *,
        risk_config: RiskEngineConfig | None = None,
    ) -> None:
        if nautilus_version != EXPECTED_NAUTILUS_VERSION:
            raise AssertionError(
                "nautilus_trader version mismatch: "
                f"{nautilus_version} != {EXPECTED_NAUTILUS_VERSION}"
            )
        self._state = tempfile.TemporaryDirectory(
            prefix="trader-bot-nautilus-sim-"
        )
        self.state_dir = Path(self._state.name)
        self.clock = TestClock()
        self.clock.set_time(FIXED_TIME_NS)
        self.trader_id = TestIdStubs.trader_id()
        self.msgbus = MessageBus(
            trader_id=self.trader_id,
            clock=self.clock,
        )
        self.cache = TestComponentStubs.cache()
        self.portfolio = Portfolio(
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )
        self.data_engine = DataEngine(
            msgbus=self.msgbus,
            clock=self.clock,
            cache=self.cache,
        )
        self.exec_engine = ExecutionEngine(
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            config=ExecEngineConfig(debug=True),
        )
        config = risk_config
        if config is None:
            config = RiskEngineConfig(debug=True)
        self.risk_engine = RiskEngine(
            portfolio=self.portfolio,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            config=config,
        )
        self.instrument = TestInstrumentProvider.btcusdt_perp_binance()
        self.exchange = SimulatedExchange(
            venue=Venue("BINANCE"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USDT,
            starting_balances=[Money(1_000_000, USDT)],
            default_leverage=Decimal(10),
            leverages={self.instrument.id: Decimal(10)},
            modules=[],
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            portfolio=self.portfolio,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            latency_model=LatencyModel(0),
            reject_stop_orders=False,
            use_reduce_only=True,
        )
        self.exchange.add_instrument(self.instrument)
        self.exec_client = BacktestExecClient(
            exchange=self.exchange,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )
        self.exec_engine.register_client(self.exec_client)
        self.exchange.register_client(self.exec_client)
        self.cache.add_instrument(self.instrument)
        self.exchange.reset()
        self.data_engine.start()
        self.exec_engine.start()
        self.strategies: list[IntentExecutionStrategy] = []

    def start_strategy(
        self,
        *,
        state_name: str = "strategy",
        strategy_id: str = "INTENT-001",
        account_id: str = "account-a",
        environment: str = "testnet",
        live_entry_notional_inventory: tuple[
            tuple[str, str],
            ...,
        ] = (),
    ) -> IntentExecutionStrategy:
        state_dir = self.state_dir / state_name
        state_dir.mkdir(parents=True, exist_ok=True)
        strategy = _SimulatedIntentExecutionStrategy(
            IntentExecutionStrategyConfig(
                account_id=account_id,
                node_id="nautilus-sim-node",
                trading_state="ACTIVE",
                environment=environment,
                strategy_id=strategy_id,
                oms_type=OmsType.NETTING,
                live_entry_notional_inventory=(
                    live_entry_notional_inventory
                ),
                intent_execution_inbox_path=str(
                    state_dir / "intent-execution-inbox.json"
                ),
                live_canary_execution_path=str(
                    state_dir / "live-canary-execution.json"
                ),
            ),
            protection_stash_path=(
                state_dir
                / IntentExecutionStrategy._PROTECTION_STASH_FILENAME
            ),
        )
        strategy.register(
            trader_id=self.trader_id,
            portfolio=self.portfolio,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )
        strategy.start()
        self.strategies.append(strategy)
        return strategy

    def prime_quote(
        self,
        *,
        bid_price: float = 100_000.0,
        ask_price: float = 100_001.0,
    ) -> Any:
        tick = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=10,
            ask_size=10,
            ts_event=self.clock.timestamp_ns(),
            ts_init=self.clock.timestamp_ns(),
        )
        self.data_engine.process(tick)
        self.exchange.process_quote_tick(tick)
        return tick

    def deliver_intent(
        self,
        strategy: IntentExecutionStrategy,
        intent: ApprovedTradeIntentV1,
    ) -> None:
        strategy.on_data(build_nautilus_custom_data(intent))
        self.pump_durable(strategy)

    def pump_durable(
        self,
        strategy: IntentExecutionStrategy,
        *,
        timeout_seconds: float = 3.0,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            strategy.drain_durable_io_mailbox(max_results=64)
            snapshot = strategy._durable_io_worker.snapshot()
            if (
                not snapshot.in_flight
                and snapshot.queue_depth == 0
                and strategy._durable_io_mailbox.empty()
            ):
                strategy.drain_durable_io_mailbox(max_results=64)
                return
            time.sleep(0.001)
        raise AssertionError("strategy durable I/O did not quiesce")

    def process_exchange(self) -> None:
        self.exchange.process(self.clock.timestamp_ns())
        for strategy in self.strategies:
            self.pump_durable(strategy)

    def seed_long_position(
        self,
        strategy: IntentExecutionStrategy,
        *,
        quantity: str = "0.010",
    ) -> Any:
        self.prime_quote()
        intent = build_intent(
            action=IntentAction.OPEN_POSITION,
            order_plan={
                "type": "market",
                "side": "buy",
                "quantity": quantity,
            },
        )
        self.deliver_intent(strategy, intent)
        self.process_exchange()
        positions = self.cache.positions_open(
            instrument_id=self.instrument.id
        )
        if len(positions) != 1:
            raise AssertionError(
                f"expected one open position, observed {len(positions)}"
            )
        return positions[0]

    def seed_stop_order(
        self,
        strategy: IntentExecutionStrategy,
        position: Any,
        *,
        intent_id: UUID | None = None,
        trigger_price: str = "95000.0",
    ) -> Any:
        source_intent_id = intent_id
        if source_intent_id is None:
            source_intent_id = uuid4()
        order = strategy.order_factory.stop_market(
            instrument_id=self.instrument.id,
            order_side=position.closing_order_side(),
            quantity=Quantity.from_str(str(position.quantity)),
            trigger_price=Price.from_str(trigger_price),
            reduce_only=True,
            client_order_id=ClientOrderId(
                encode_client_order_id(source_intent_id, sequence=11)
            ),
            tags=[
                "lifecycle_role=stop_loss",
                f"position_id={position.id}",
                f"parent_intent_id={source_intent_id}",
            ],
        )
        strategy.submit_order(order)
        self.process_exchange()
        if order.status is not OrderStatus.ACCEPTED:
            raise AssertionError(
                f"expected accepted stop order, observed {order.status}"
            )
        return order

    def seed_take_profit_order(
        self,
        strategy: IntentExecutionStrategy,
        position: Any,
        *,
        intent_id: UUID | None = None,
        trigger_price: str = "105000.0",
    ) -> Any:
        source_intent_id = intent_id
        if source_intent_id is None:
            source_intent_id = uuid4()
        order = strategy.order_factory.market_if_touched(
            instrument_id=self.instrument.id,
            order_side=position.closing_order_side(),
            quantity=Quantity.from_str(str(position.quantity)),
            trigger_price=Price.from_str(trigger_price),
            reduce_only=True,
            client_order_id=ClientOrderId(
                encode_client_order_id(source_intent_id, sequence=12)
            ),
            tags=[
                "lifecycle_role=take_profit",
                "take_profit_index=1",
                f"position_id={position.id}",
                f"parent_intent_id={source_intent_id}",
            ],
        )
        strategy.submit_order(order)
        self.process_exchange()
        if order.status is not OrderStatus.ACCEPTED:
            raise AssertionError(
                "expected accepted take-profit order, "
                f"observed {order.status}"
            )
        return order

    def attach_real_cancel_bridge(
        self,
        strategy: IntentExecutionStrategy,
    ) -> None:
        mirror = _SimulatedExchangeOrderMirror(self)
        adapter = _SimulatedExchangeCancelAdapter(
            self,
            strategy,
            mirror,
        )
        strategy.set_exchange_cancel_adapter(adapter, mirror)

    def close(self) -> None:
        for strategy in reversed(self.strategies):
            if strategy.is_running:
                strategy.stop()
        if self.exec_engine.is_running:
            self.exec_engine.stop()
        if self.data_engine.is_running:
            self.data_engine.stop()
        self._state.cleanup()


class _SimulatedExchangeOrderMirror:
    def __init__(self, engine: NautilusSimulatedEngine) -> None:
        self._engine = engine

    def refresh(self) -> tuple[Any, ...]:
        return tuple(self._engine.cache.orders_open())

    def orders_for_instrument(
        self,
        instrument_id: str,
    ) -> tuple[Any, ...]:
        return tuple(
            order
            for order in self.refresh()
            if str(order.instrument_id) == str(instrument_id)
        )

    def find_order(
        self,
        instrument_id: str,
        client_order_id: str,
    ) -> Any:
        for order in self.orders_for_instrument(instrument_id):
            if str(order.client_order_id) == str(client_order_id):
                return order
        return False


class _SimulatedIntentExecutionStrategy(IntentExecutionStrategy):
    def __init__(
        self,
        config: IntentExecutionStrategyConfig,
        *,
        protection_stash_path: Path,
    ) -> None:
        self._simulated_protection_stash_path = protection_stash_path
        super().__init__(config)

    def _protection_stash_path(self) -> str:
        return str(self._simulated_protection_stash_path)


class _SimulatedExchangeCancelAdapter:
    def __init__(
        self,
        engine: NautilusSimulatedEngine,
        strategy: IntentExecutionStrategy,
        mirror: _SimulatedExchangeOrderMirror,
    ) -> None:
        self._engine = engine
        self._strategy = strategy
        self._mirror = mirror

    def cancel(self, _operation: str, request: Any) -> Any:
        order = self._mirror.find_order(
            str(self._engine.instrument.id),
            str(request.client_order_id),
        )
        if order is False:
            raise RuntimeError(
                f"simulated order not found: {request.client_order_id}"
            )
        self._strategy.cancel_order(order)
        self._engine.process_exchange()
        if order.status is not OrderStatus.CANCELED:
            raise RuntimeError(
                "simulated cancellation was not confirmed: "
                f"{request.client_order_id}:{order.status}"
            )
        return SimpleNamespace(
            outcome="canceled",
            terminal_status="CANCELED",
        )
