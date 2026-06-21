from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Optional

from strategy.intent_execution_planner import (
    InstrumentSpec,
    ManagementPlan,
    OrderDenied,
    OrderPlan,
    OrderSnapshot,
    PlannerContext,
    PositionSnapshot,
    decode_client_order_id,
    plan_intent_execution,
)
from strategy.price_guard import PriceGuardDecision


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
        self._price_guard: Any = None

    def set_trading_state_getter(self, getter: Optional[Callable[[], Any]]) -> None:
        """Inject the node's live trading-state source. Kept out of the serializable
        StrategyConfig; node wiring calls this after construction."""
        self._trading_state_getter = getter

    def set_price_guard(self, guard: Any) -> None:
        self._price_guard = guard

    def on_start(self) -> None:
        # C-08 host-verify fix: subscribe_data(data_type) is rejected for clientless
        # custom data in Nautilus 1.227.0 (it requires client_id/instrument_id).
        # Approved intents are internal actor->strategy data, so deliver them over the
        # msgbus on a controlled per-account topic the IntentPublisher publishes to.
        self.msgbus.subscribe(  # type: ignore[attr-defined]
            topic=f"intents.{self.config.account_id}",
            handler=self._on_intent_msg,
        )
        # Kill-switch operator actions (cancel_all/close_all) are routed here by the
        # CommandPollerActor: only a Strategy may submit/cancel/close on Nautilus.
        self.msgbus.subscribe(  # type: ignore[attr-defined]
            topic=f"node.commands.{self.config.account_id}",
            handler=self._on_node_command,
        )

    def _on_intent_msg(self, intent: Any) -> None:
        # msgbus delivers the ApprovedTradeIntentV1 directly.
        if intent is not None:
            self._handle_intent(intent)

    def _on_node_command(self, cmd: Any) -> None:
        """Execute operator cancel_all / close_all. Best-effort per item: a failure on
        one order/position is recorded but does not stop the rest (kill-switch must be
        as complete as possible)."""
        ctype = getattr(cmd, "type", cmd)
        ctype = str(getattr(ctype, "value", ctype))
        if ctype == "cancel_all":
            for order in self._all_open_orders():
                try:
                    self.cancel_order(order)  # type: ignore[attr-defined]
                except Exception as exc:
                    self._record_denial(OrderDenied("order_cancel_failed", repr(exc)))
        elif ctype == "close_all":
            for position in self._all_open_positions():
                try:
                    # Nautilus submits a reduce-only market order to flatten.
                    self.close_position(position)  # type: ignore[attr-defined]
                except Exception as exc:
                    self._record_denial(OrderDenied("position_close_failed", repr(exc)))

    def _all_open_orders(self) -> tuple[Any, ...]:
        cache = getattr(self, "cache", None)
        if cache is None:
            return ()
        for name in ("orders_open", "orders"):
            method = getattr(cache, name, None)
            if method is None:
                continue
            try:
                return tuple(method() or ())
            except TypeError:
                continue
        return ()

    def _all_open_positions(self) -> tuple[Any, ...]:
        cache = getattr(self, "cache", None)
        if cache is None:
            return ()
        for name in ("positions_open", "positions"):
            method = getattr(cache, name, None)
            if method is None:
                continue
            try:
                return tuple(
                    p for p in (method() or ())
                    if Decimal(str(_position_quantity(p) or 0)) != 0
                )
            except TypeError:
                continue
        return ()

    def on_data(self, data: Any) -> None:
        # Retained for the publish_data path / tests: unwrap CustomData if used.
        intent = _intent_from_custom_data(data)
        if intent is None:
            return
        self._handle_intent(intent)

    def _handle_intent(self, intent: Any) -> None:
        context = PlannerContext(
            account_id=self.config.account_id,
            trading_state=self._trading_state(),
            now=self._now(),
            instrument=self._instrument_spec(str(intent.instrument_id)),
            position=self._position_snapshot(str(intent.instrument_id)),
            positions=self._position_snapshots(str(intent.instrument_id)),
            existing_orders=self._order_snapshots(str(intent.instrument_id)),
            existing_intent_ids=frozenset(
                self._processed_intent_ids | self._active_intent_ids(intent.instrument_id)
            ),
        )
        result = plan_intent_execution(intent, context)
        if isinstance(result, OrderDenied):
            self._record_denial(result)
            return

        if isinstance(result, ManagementPlan):
            submitted = self._submit_management_plan(result)
        else:
            submitted = self._submit_order_plan(result)
        if submitted:
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
        return self._position_snapshot_from_cache(position, instrument_id)

    def _position_snapshots(self, instrument_id: str) -> tuple[PositionSnapshot, ...]:
        return tuple(
            self._position_snapshot_from_cache(position, instrument_id)
            for position in _nonzero_positions(self._cache_positions(instrument_id))
        )

    def _position_snapshot_from_cache(self, position: Any, instrument_id: str) -> PositionSnapshot:
        return PositionSnapshot(
            instrument_id=instrument_id,
            side=_position_side(position),
            quantity=_position_quantity(position),
            position_id=_position_id(position),
            entry_price=_position_entry_price(position),
        )

    def _order_snapshots(self, instrument_id: str) -> tuple[OrderSnapshot, ...]:
        snapshots: list[OrderSnapshot] = []
        for order in self._cache_orders(instrument_id):
            client_order_id = getattr(order, "client_order_id", None)
            if client_order_id is None:
                continue
            snapshots.append(
                OrderSnapshot(
                    client_order_id=str(client_order_id),
                    instrument_id=str(getattr(order, "instrument_id", instrument_id)),
                    order_type=_enum_name(getattr(order, "order_type", "")),
                    side=_enum_name(getattr(order, "side", getattr(order, "order_side", ""))),
                    quantity=str(getattr(order, "quantity", getattr(order, "qty", ""))),
                    price=_optional_str(getattr(order, "price", None)),
                    trigger_price=_optional_str(
                        getattr(order, "trigger_price", getattr(order, "stop_price", None))
                    ),
                    tags=tuple(str(tag) for tag in (getattr(order, "tags", ()) or ())),
                )
            )
        return tuple(snapshots)

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
        target = str(instrument_id)
        iid = self._as_instrument_id(instrument_id)
        # Prefer positions_open (excludes closed positions). Try an InstrumentId-typed
        # scoped query first, then a string arg, then the no-arg form.
        # TODO(host-verify): confirm open-position cache method names on Nautilus 1.227.0.
        for name in ("positions_open", "positions", "open_positions"):
            method = getattr(cache, name, None)
            if method is None:
                continue
            raw = None
            for call in (lambda: method(iid), lambda: method(instrument_id), lambda: method()):
                try:
                    raw = call() or ()
                    break
                except TypeError:
                    continue
            if raw is None:
                continue
            # ALWAYS filter by instrument: a no-arg (unscoped) result must never leak
            # other instruments' positions, e.g. an open ETH blocking a BTC open
            # (position_exists false-positive). Closed positions are also excluded here.
            return [
                p for p in raw
                if str(getattr(p, "instrument_id", "")) == target
                and Decimal(str(_position_quantity(p) or 0)) != 0
            ]
        return ()

    def _as_instrument_id(self, instrument_id: str) -> Any:
        try:
            from nautilus_trader.model.identifiers import InstrumentId  # type: ignore

            return InstrumentId.from_str(str(instrument_id))
        except Exception:
            return str(instrument_id)

    def _submit_order_plan(self, plan: OrderPlan) -> bool:
        instrument = self._cache_instrument(plan.instrument_id)
        if instrument is None:
            self._record_denial(OrderDenied("instrument_not_found", plan.instrument_id))
            return False

        try:
            guard_decision = self._check_price_guard(plan)
            if guard_decision is not None and not guard_decision.allowed:
                self._record_denial(OrderDenied(guard_decision.reason, repr(guard_decision)))
                return False
            order = self._build_nautilus_order(plan, instrument)
            self.submit_order(order)  # type: ignore[attr-defined]
            return True
        except Exception as exc:  # Fail closed: no silent drops on adapter/API mismatch.
            self._record_denial(OrderDenied("order_submit_failed", repr(exc)))
            return False

    def _check_price_guard(self, plan: OrderPlan) -> PriceGuardDecision | None:
        if self._price_guard is None:
            return None
        if plan.order_type not in {"MARKET", "LIMIT"}:
            return None
        max_slippage = Decimal(plan.max_slippage_bps) if plan.max_slippage_bps is not None else None
        intended_raw = plan.price if plan.price is not None else plan.guard_price
        intended_price = Decimal(intended_raw) if intended_raw is not None else None
        return self._price_guard.check(
            account_id=self.config.account_id,
            venue_symbol=plan.instrument_id,
            side=plan.side,
            order_type=plan.order_type,
            intended_price=intended_price,
            max_slippage_bps=max_slippage,
            now=self._now(),
        )

    def _submit_management_plan(self, plan: ManagementPlan) -> bool:
        for client_order_id in plan.cancel_order_ids:
            if not self._cancel_order_by_client_order_id(plan.instrument_id, client_order_id):
                return False
        for order_plan in plan.orders:
            if not self._submit_order_plan(order_plan):
                return False
        return True

    def _cancel_order_by_client_order_id(
        self,
        instrument_id: str,
        client_order_id: str,
    ) -> bool:
        for order in self._cache_orders(instrument_id):
            if str(getattr(order, "client_order_id", "")) != client_order_id:
                continue
            try:
                # TODO(host-verify): confirm Strategy.cancel_order takes the order
                # object directly in Nautilus 1.227.0 for Binance futures.
                self.cancel_order(order)  # type: ignore[attr-defined]
                return True
            except Exception as exc:
                self._record_denial(OrderDenied("order_cancel_failed", repr(exc)))
                return False
        self._record_denial(OrderDenied("order_cancel_not_found", client_order_id))
        return False

    def _build_nautilus_order(self, plan: OrderPlan, instrument: Any) -> Any:
        # TODO(host-verify): confirm OrderFactory methods and whether market,
        # stop_market, stop_limit, and limit_if_touched orders accept
        # time_in_force/client_order_id/tags/reduce_only directly in Nautilus
        # 1.227.0.
        from nautilus_trader.model.enums import OrderSide, TimeInForce  # type: ignore
        from nautilus_trader.model.identifiers import ClientOrderId  # type: ignore

        side = OrderSide.BUY if plan.side == "BUY" else OrderSide.SELL
        tif = getattr(TimeInForce, plan.time_in_force)
        quantity = _make_quantity(instrument, plan.quantity)
        kwargs = {
            "instrument_id": getattr(instrument, "id", plan.instrument_id),
            "order_side": side,
            "quantity": quantity,
            "time_in_force": tif,
            # host-verify: Nautilus order factory needs a ClientOrderId, not a str.
            "client_order_id": ClientOrderId(str(plan.client_order_id)),
            "tags": list(plan.tags),
        }
        if plan.reduce_only:
            kwargs["reduce_only"] = True
        if plan.post_only:
            kwargs["post_only"] = True
        if plan.order_type == "MARKET":
            return self.order_factory.market(**kwargs)  # type: ignore[attr-defined]
        if plan.order_type == "LIMIT":
            kwargs["price"] = _make_price(instrument, plan.price)
            return self.order_factory.limit(**kwargs)  # type: ignore[attr-defined]
        if plan.order_type == "STOP_MARKET":
            kwargs["trigger_price"] = _make_price(instrument, plan.trigger_price)
            return self.order_factory.stop_market(**kwargs)  # type: ignore[attr-defined]
        if plan.order_type == "STOP_LIMIT":
            kwargs["price"] = _make_price(instrument, plan.price)
            kwargs["trigger_price"] = _make_price(instrument, plan.trigger_price)
            return self.order_factory.stop_limit(**kwargs)  # type: ignore[attr-defined]
        if plan.order_type == "LIMIT_IF_TOUCHED":
            kwargs["price"] = _make_price(instrument, plan.price)
            kwargs["trigger_price"] = _make_price(instrument, plan.trigger_price)
            return self.order_factory.limit_if_touched(**kwargs)  # type: ignore[attr-defined]
        if plan.order_type == "MARKET_IF_TOUCHED":
            kwargs["trigger_price"] = _make_price(instrument, plan.trigger_price)
            return self.order_factory.market_if_touched(**kwargs)  # type: ignore[attr-defined]
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
    for position in _nonzero_positions(positions):
        return position
    return None


def _nonzero_positions(positions: Iterable[Any]) -> tuple[Any, ...]:
    result: list[Any] = []
    for position in positions:
        try:
            if float(_position_quantity(position)) != 0.0:
                result.append(position)
        except (TypeError, ValueError):
            continue
    return tuple(result)


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


def _position_id(position: Any) -> Optional[str]:
    for name in ("id", "position_id"):
        value = getattr(position, name, None)
        if value is not None:
            return str(value)
    return None


def _position_entry_price(position: Any) -> Optional[str]:
    for name in ("entry_price", "avg_px_open", "average_open_price"):
        value = getattr(position, name, None)
        if value is not None:
            return _decimalish_to_str(value)
    return None


def _intent_ids_from_tags(item: Any) -> set[str]:
    ids: set[str] = set()
    tags = getattr(item, "tags", ()) or ()
    for tag in tags:
        text = str(tag)
        if text.startswith("intent_id="):
            ids.add(text.split("=", 1)[1])
    return ids


def _enum_name(value: Any) -> str:
    raw = getattr(value, "name", getattr(value, "value", value))
    return str(raw).upper()


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return _decimalish_to_str(value)


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
