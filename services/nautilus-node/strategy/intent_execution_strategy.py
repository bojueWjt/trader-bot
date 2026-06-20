from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from strategy.intent_execution_planner import (
    InstrumentSpec,
    OrderDenied,
    OrderPlan,
    PlannerContext,
    PositionSnapshot,
    decode_client_order_id,
    plan_intent_execution,
)


try:  # pragma: no cover - Nautilus is unavailable on local Py3.14 dev hosts.
    from nautilus_trader.trading.strategy import Strategy  # type: ignore[import-not-found]
    from nautilus_trader.trading.config import StrategyConfig  # type: ignore[import-not-found]

    class IntentExecutionStrategyConfig(StrategyConfig, frozen=True):  # type: ignore[call-arg,misc]
        """Serializable Nautilus ``StrategyConfig`` (msgspec.Struct). Runtime-only
        collaborators (e.g. the node trading-state getter) are injected after
        construction via ``set_trading_state_getter``, never stored in config."""

        account_id: str = ""
        node_id: str = ""
        trading_state: str = "HALTED"
        existing_intent_ids: tuple[str, ...] = ()

except ImportError:  # pragma: no cover - local dev fallback without Nautilus

    class Strategy:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.config = kwargs.get("config") or (args[0] if args else None)

    @dataclass(frozen=True)
    class IntentExecutionStrategyConfig:  # type: ignore[no-redef]
        account_id: str = ""
        node_id: str = ""
        trading_state: str = "HALTED"
        existing_intent_ids: tuple[str, ...] = ()


class IntentExecutionStrategy(Strategy):
    """Execute approved open/add intents as Binance USDM entry orders.

    Mapping:
    - ``open_position`` + ``market`` -> Nautilus ``MARKET`` entry order.
    - ``open_position`` + ``limit`` -> Nautilus ``LIMIT`` entry order.
    - ``open_position`` + ``zone`` -> Nautilus ``LIMIT`` at the aggressive
      boundary: buy uses ``price_max``; sell uses ``price_min``.
    - ``add_position`` uses the same order mapping, but requires an existing
      same-side position.

    Client order id scheme is documented in ``encode_client_order_id``:
    ``B{intent_uuid_hex}{sequence:02d}``, which can be decoded back to the full
    intent id. Tags carry the full intent trace.
    """

    def __init__(self, config: IntentExecutionStrategyConfig) -> None:
        try:
            super().__init__(config=config)
        except TypeError:
            super().__init__(config)
        self._processed_intent_ids: set[str] = set(config.existing_intent_ids)
        self.denials: list[OrderDenied] = []
        self._trading_state_getter: Optional[Callable[[], Any]] = None

    def set_trading_state_getter(self, getter: Optional[Callable[[], Any]]) -> None:
        """Inject the node's live trading-state source. Kept out of the serializable
        StrategyConfig; node wiring calls this after construction."""
        self._trading_state_getter = getter

    def on_start(self) -> None:
        data_type = _approved_intent_data_type(self.config.account_id)
        # TODO(host-verify): confirm Strategy.subscribe_data accepts the DataType
        # object directly for CustomData in Nautilus 1.227.0.
        self.subscribe_data(data_type)  # type: ignore[attr-defined]

    def on_data(self, data: Any) -> None:
        intent = _intent_from_custom_data(data)
        if intent is None:
            return

        context = PlannerContext(
            account_id=self.config.account_id,
            trading_state=self._trading_state(),
            now=self._now(),
            instrument=self._instrument_spec(str(intent.instrument_id)),
            position=self._position_snapshot(str(intent.instrument_id)),
            existing_intent_ids=frozenset(
                self._processed_intent_ids | self._active_intent_ids(intent.instrument_id)
            ),
        )
        result = plan_intent_execution(intent, context)
        if isinstance(result, OrderDenied):
            self._record_denial(result)
            return

        self._submit_order_plan(result)
        self._processed_intent_ids.add(str(result.intent_id))

    def _trading_state(self) -> str:
        if self._trading_state_getter is not None:
            raw_state = self._trading_state_getter()
        else:
            raw_state = self.config.trading_state
        return str(getattr(raw_state, "value", raw_state))

    def _now(self) -> datetime:
        clock = getattr(self, "clock", None)
        if clock is not None and hasattr(clock, "utc_now"):
            return clock.utc_now()
        return datetime.now(timezone.utc)

    def _instrument_spec(self, instrument_id: str) -> Optional[InstrumentSpec]:
        instrument = self._cache_instrument(instrument_id)
        if instrument is None:
            return None
        price_increment = _extract_increment(
            instrument,
            increment_names=("price_increment", "price_tick", "tick_size"),
            precision_names=("price_precision",),
        )
        quantity_increment = _extract_increment(
            instrument,
            increment_names=("size_increment", "quantity_increment", "lot_size", "step_size"),
            precision_names=("size_precision", "quantity_precision"),
        )
        if price_increment is None or quantity_increment is None:
            self._record_denial(
                OrderDenied("instrument_precision_unavailable", instrument_id)
            )
            return None
        return InstrumentSpec(
            instrument_id=instrument_id,
            price_increment=price_increment,
            quantity_increment=quantity_increment,
        )

    def _position_snapshot(self, instrument_id: str) -> Optional[PositionSnapshot]:
        position = _first_nonzero_position(self._cache_positions(instrument_id))
        if position is None:
            return None
        return PositionSnapshot(
            instrument_id=instrument_id,
            side=_position_side(position),
            quantity=_position_quantity(position),
        )

    def _active_intent_ids(self, instrument_id: Any) -> set[str]:
        ids: set[str] = set()
        for item in list(self._cache_orders(instrument_id)) + list(
            self._cache_positions(str(instrument_id))
        ):
            ids.update(_intent_ids_from_tags(item))
            client_order_id = getattr(item, "client_order_id", None)
            if client_order_id is not None:
                try:
                    ids.add(str(decode_client_order_id(str(client_order_id)).intent_id))
                except ValueError:
                    pass
        return ids

    def _cache_instrument(self, instrument_id: str) -> Any:
        cache = getattr(self, "cache", None)
        if cache is None or not hasattr(cache, "instrument"):
            return None
        try:
            # TODO(host-verify): confirm whether cache.instrument requires
            # InstrumentId.from_str(...) instead of a string in Nautilus 1.227.0.
            return cache.instrument(instrument_id)
        except TypeError:
            try:
                from nautilus_trader.model.identifiers import InstrumentId  # type: ignore

                return cache.instrument(InstrumentId.from_str(instrument_id))
            except Exception:
                return None

    def _cache_orders(self, instrument_id: Any) -> Iterable[Any]:
        cache = getattr(self, "cache", None)
        if cache is None:
            return ()
        # TODO(host-verify): confirm active/open order cache method names on
        # Nautilus 1.227.0. These candidates keep the strategy fail-closed.
        for name in ("orders_open", "orders_active", "open_orders"):
            method = getattr(cache, name, None)
            if method is None:
                continue
            try:
                return method(instrument_id) or ()
            except TypeError:
                try:
                    return method() or ()
                except TypeError:
                    continue
        return ()

    def _cache_positions(self, instrument_id: str) -> Iterable[Any]:
        cache = getattr(self, "cache", None)
        if cache is None:
            return ()
        # TODO(host-verify): confirm open-position cache method names and
        # InstrumentId argument type on Nautilus 1.227.0.
        for name in ("positions_open", "positions", "open_positions"):
            method = getattr(cache, name, None)
            if method is None:
                continue
            try:
                return method(instrument_id) or ()
            except TypeError:
                try:
                    return method() or ()
                except TypeError:
                    continue
        return ()

    def _submit_order_plan(self, plan: OrderPlan) -> None:
        instrument = self._cache_instrument(plan.instrument_id)
        if instrument is None:
            self._record_denial(OrderDenied("instrument_not_found", plan.instrument_id))
            return

        try:
            order = self._build_nautilus_order(plan, instrument)
            self.submit_order(order)  # type: ignore[attr-defined]
        except Exception as exc:  # Fail closed: no silent drops on adapter/API mismatch.
            self._record_denial(OrderDenied("order_submit_failed", repr(exc)))

    def _build_nautilus_order(self, plan: OrderPlan, instrument: Any) -> Any:
        # TODO(host-verify): confirm OrderFactory methods and whether market orders
        # accept time_in_force/client_order_id/tags directly in Nautilus 1.227.0.
        from nautilus_trader.model.enums import OrderSide, TimeInForce  # type: ignore

        side = OrderSide.BUY if plan.side == "BUY" else OrderSide.SELL
        tif = getattr(TimeInForce, plan.time_in_force)
        quantity = _make_quantity(instrument, plan.quantity)
        kwargs = {
            "instrument_id": getattr(instrument, "id", plan.instrument_id),
            "order_side": side,
            "quantity": quantity,
            "time_in_force": tif,
            "client_order_id": plan.client_order_id,
            "tags": list(plan.tags),
        }
        if plan.order_type == "MARKET":
            return self.order_factory.market(**kwargs)  # type: ignore[attr-defined]
        if plan.order_type == "LIMIT":
            kwargs["price"] = _make_price(instrument, plan.price)
            return self.order_factory.limit(**kwargs)  # type: ignore[attr-defined]
        raise RuntimeError(f"unsupported order_type from planner: {plan.order_type}")

    def _record_denial(self, denial: OrderDenied) -> None:
        self.denials.append(denial)
        log = getattr(self, "log", None)
        if log is not None and hasattr(log, "error"):
            log.error(f"OrderDenied reason={denial.reason} detail={denial.detail}")


def _approved_intent_data_type(account_id: str) -> Any:
    from intent.custom_data import _approved_trade_intent_data_class  # type: ignore
    from nautilus_trader.model.data import DataType  # type: ignore

    data_cls = _approved_trade_intent_data_class()
    return DataType(
        data_cls,
        metadata={"schema_version": "1.0", "account_id": account_id},
    )


def _intent_from_custom_data(data: Any) -> Any:
    payload_holder = getattr(data, "data", data)
    payload = getattr(payload_holder, "payload", None)
    if payload is None:
        return None
    from intent.custom_data import ApprovedTradeIntentCustomData  # type: ignore

    return ApprovedTradeIntentCustomData.from_wire(payload).intent


def _extract_increment(
    instrument: Any,
    increment_names: tuple[str, ...],
    precision_names: tuple[str, ...],
) -> Optional[str]:
    for name in increment_names:
        value = getattr(instrument, name, None)
        if value is not None:
            return _decimalish_to_str(value)
    for name in precision_names:
        value = getattr(instrument, name, None)
        if value is not None:
            precision = int(value)
            return "1" if precision == 0 else "0." + ("0" * (precision - 1)) + "1"
    return None


def _decimalish_to_str(value: Any) -> str:
    for name in ("as_decimal", "to_decimal"):
        method = getattr(value, name, None)
        if method is not None:
            return str(method())
    return str(value)


def _first_nonzero_position(positions: Iterable[Any]) -> Optional[Any]:
    for position in positions:
        try:
            if float(_position_quantity(position)) != 0.0:
                return position
        except (TypeError, ValueError):
            continue
    return None


def _position_side(position: Any) -> str:
    for name in ("side", "position_side"):
        value = getattr(position, name, None)
        if value is not None:
            raw = str(getattr(value, "value", value)).upper()
            if "SHORT" in raw:
                return "SHORT"
            if "LONG" in raw:
                return "LONG"
    signed_qty = getattr(position, "signed_qty", None)
    if signed_qty is not None and str(signed_qty).startswith("-"):
        return "SHORT"
    return "LONG"


def _position_quantity(position: Any) -> str:
    for name in ("quantity", "qty", "signed_qty"):
        value = getattr(position, name, None)
        if value is not None:
            return str(value).lstrip("-")
    return "0"


def _intent_ids_from_tags(item: Any) -> set[str]:
    ids: set[str] = set()
    tags = getattr(item, "tags", ()) or ()
    for tag in tags:
        text = str(tag)
        if text.startswith("intent_id="):
            ids.add(text.split("=", 1)[1])
    return ids


def _make_quantity(instrument: Any, quantity: str) -> Any:
    make_qty = getattr(instrument, "make_qty", None)
    if make_qty is not None:
        return make_qty(quantity)
    from nautilus_trader.model.objects import Quantity  # type: ignore

    return Quantity.from_str(quantity)


def _make_price(instrument: Any, price: Optional[str]) -> Any:
    if price is None:
        raise ValueError("price is required")
    make_price = getattr(instrument, "make_price", None)
    if make_price is not None:
        return make_price(price)
    from nautilus_trader.model.objects import Price  # type: ignore

    return Price.from_str(price)
