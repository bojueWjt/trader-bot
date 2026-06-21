from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any


class VenueFault(StrEnum):
    ACCEPT_THEN_UNKNOWN = "accept_then_unknown"
    TIMEOUT = "timeout"


class VenueTimeout(RuntimeError):
    pass


@dataclass
class VenueOrder:
    account_id: str
    client_order_id: str
    venue_order_id: str
    instrument_id: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None = None
    trigger_price: Decimal | None = None
    lifecycle_role: str = "entry"
    position_key: str | None = None
    reduce_only: bool = False
    status: str = "submitted"
    filled_quantity: Decimal = Decimal("0")

    @property
    def remaining_quantity(self) -> Decimal:
        remaining = self.quantity - self.filled_quantity
        return remaining if remaining > 0 else Decimal("0")

    def snapshot(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("quantity", "price", "trigger_price", "filled_quantity"):
            if data[key] is not None:
                data[key] = str(data[key])
        return data


@dataclass(frozen=True)
class VenueEvent:
    event_id: str
    event_type: str
    account_id: str
    client_order_id: str | None
    venue_order_id: str | None
    ts_event: datetime
    payload: dict[str, Any]

    def as_projection_event(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "account_id": self.account_id,
            "client_order_id": self.client_order_id,
            "venue_order_id": self.venue_order_id,
            "instrument_id": self.payload.get("instrument_id"),
            "ts_event": self.ts_event,
            "payload": dict(self.payload),
        }


class FakeVenue:
    def __init__(
        self,
        *,
        account_id: str,
        start: datetime | None = None,
        orders: dict[str, VenueOrder] | None = None,
        positions: dict[str, dict[str, Any]] | None = None,
        event_seq: int = 0,
    ) -> None:
        self.account_id = account_id
        self.start = _aware(start or datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc))
        self._orders = orders or {}
        self._positions = positions or {}
        self._event_seq = event_seq
        self._venue_seq = len(self._orders)
        self._pending_events: list[VenueEvent] = []
        self.event_history: list[VenueEvent] = []
        self._faults: dict[str, VenueFault] = {}
        self.submission_count = len(self._orders)

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "FakeVenue":
        orders = {
            client_order_id: _order_from_snapshot(order)
            for client_order_id, order in snapshot["orders"].items()
        }
        positions = {
            key: dict(position)
            for key, position in snapshot["positions"].items()
        }
        return cls(
            account_id=snapshot["account_id"],
            start=snapshot["start"],
            orders=orders,
            positions=positions,
            event_seq=snapshot["event_seq"],
        )

    def inject_fault(self, client_order_id: str, fault: VenueFault) -> None:
        self._faults[client_order_id] = fault

    def submit_order(self, **kwargs: Any) -> dict[str, Any]:
        client_order_id = str(kwargs["client_order_id"])
        existing = self._orders.get(client_order_id)
        if existing is not None:
            return existing.snapshot()

        fault = self._faults.pop(client_order_id, None)
        if fault == VenueFault.TIMEOUT:
            raise VenueTimeout("timeout")

        self._venue_seq += 1
        order = VenueOrder(
            account_id=self.account_id,
            client_order_id=client_order_id,
            venue_order_id=f"venue-{self._venue_seq:04d}",
            instrument_id=str(kwargs["instrument_id"]),
            side=str(kwargs["side"]).upper(),
            order_type=str(kwargs["order_type"]).upper(),
            quantity=_decimal(kwargs["quantity"]),
            price=_optional_decimal(kwargs.get("price")),
            trigger_price=_optional_decimal(kwargs.get("trigger_price")),
            lifecycle_role=str(kwargs.get("lifecycle_role") or "entry"),
            position_key=kwargs.get("position_key") or _position_key(self.account_id, str(kwargs["instrument_id"])),
            reduce_only=bool(kwargs.get("reduce_only") or False),
        )
        self._orders[client_order_id] = order
        self.submission_count += 1
        self._emit_order(order, "OrderSubmitted")
        if kwargs.get("auto_accept", True):
            self.accept(client_order_id)
        if fault == VenueFault.ACCEPT_THEN_UNKNOWN:
            raise VenueTimeout("accepted_unknown")
        return order.snapshot()

    def submit_plan(self, plan: Any) -> dict[str, Any]:
        tags = _parse_tags(getattr(plan, "tags", ()))
        return self.submit_order(
            client_order_id=getattr(plan, "client_order_id"),
            instrument_id=getattr(plan, "instrument_id"),
            side=getattr(plan, "side"),
            order_type=getattr(plan, "order_type"),
            quantity=Decimal(str(getattr(plan, "quantity"))),
            price=_optional_decimal(getattr(plan, "price", None)),
            trigger_price=_optional_decimal(getattr(plan, "trigger_price", None)),
            lifecycle_role=tags.get("lifecycle_role", "entry"),
            position_key=tags.get("position_id"),
            reduce_only=bool(getattr(plan, "reduce_only", False)),
        )

    def accept(self, client_order_id: str) -> dict[str, Any]:
        order = self.order(client_order_id)
        order.status = "accepted"
        self._emit_order(order, "OrderAccepted")
        return order.snapshot()

    def fill(self, client_order_id: str, *, quantity: Decimal, price: Decimal) -> dict[str, Any]:
        order = self.order(client_order_id)
        fill_quantity = min(_decimal(quantity), order.remaining_quantity)
        order.filled_quantity += fill_quantity
        order.status = "filled" if order.remaining_quantity == 0 else "partially_filled"
        self._emit_order(
            order,
            "OrderFilled",
            {
                "filled_qty": str(order.filled_quantity),
                "last_fill_qty": str(fill_quantity),
                "leaves_qty": str(order.remaining_quantity),
                "avg_px": str(price),
            },
        )
        self._apply_position_fill(order, fill_quantity, _decimal(price))
        return order.snapshot()

    def partial(self, client_order_id: str, *, quantity: Decimal, price: Decimal) -> dict[str, Any]:
        return self.fill(client_order_id, quantity=quantity, price=price)

    def cancel(self, client_order_id: str) -> dict[str, Any]:
        order = self.order(client_order_id)
        if order.status in {"filled", "cancelled", "rejected", "expired"}:
            return order.snapshot()
        order.status = "pending_cancel"
        self._emit_order(order, "OrderPendingCancel")
        order.status = "cancelled"
        self._emit_order(order, "OrderCanceled")
        return order.snapshot()

    def cancel_all(self) -> list[dict[str, Any]]:
        cancelled = []
        for order in list(self._orders.values()):
            if order.status not in {"filled", "cancelled", "rejected", "expired"}:
                cancelled.append(self.cancel(order.client_order_id))
        return cancelled

    def reject(self, client_order_id: str, *, reason: str = "rejected") -> dict[str, Any]:
        order = self.order(client_order_id)
        order.status = "rejected"
        self._emit_order(order, "OrderRejected", {"reason": reason})
        return order.snapshot()

    def expire(self, client_order_id: str) -> dict[str, Any]:
        order = self.order(client_order_id)
        order.status = "expired"
        self._emit_order(order, "OrderExpired")
        return order.snapshot()

    def trigger_stop(self, client_order_id: str, *, price: Decimal) -> dict[str, Any]:
        order = self.order(client_order_id)
        quantity = self._position_quantity(order.position_key or "")
        return self.fill(client_order_id, quantity=quantity or order.remaining_quantity, price=price)

    def stop(self, client_order_id: str, *, price: Decimal) -> dict[str, Any]:
        return self.trigger_stop(client_order_id, price=price)

    def take_profit(self, client_order_id: str, *, quantity: Decimal, price: Decimal) -> dict[str, Any]:
        return self.fill(client_order_id, quantity=quantity, price=price)

    def open_position(
        self,
        *,
        instrument_id: str,
        quantity: Decimal,
        price: Decimal,
        side: str = "long",
    ) -> dict[str, Any]:
        position_key = _position_key(self.account_id, instrument_id)
        position = {
            "position_id": position_key,
            "position_key": position_key,
            "account_id": self.account_id,
            "instrument_id": instrument_id,
            "position_side": "BOTH",
            "side": side,
            "quantity": str(_decimal(quantity)),
            "avg_entry_price": str(_decimal(price)),
        }
        self._positions[position_key] = position
        self._emit_position("PositionOpened", position)
        return dict(position)

    def close_position(self, position_key: str) -> dict[str, Any]:
        position = self._positions[position_key]
        position["quantity"] = "0"
        self._emit_position("PositionClosed", position)
        return {"position_key": position_key, "accepted": True, "status": "closed"}

    def list_open_positions(self, *, account_id: str | None = None) -> list[dict[str, Any]]:
        expected_account = account_id or self.account_id
        return [
            dict(position)
            for position in self._positions.values()
            if position["account_id"] == expected_account and _decimal(position["quantity"]) != 0
        ]

    def working_orders(self) -> list[dict[str, Any]]:
        return [
            order.snapshot()
            for order in self._orders.values()
            if order.status in {"accepted", "partially_filled", "pending_cancel"}
        ]

    def order(self, client_order_id: str) -> VenueOrder:
        order = self.find_order(client_order_id)
        if order is None:
            raise KeyError(client_order_id)
        return order

    def find_order(self, client_order_id: str) -> VenueOrder | None:
        return self._orders.get(client_order_id)

    def drain_events(self) -> list[VenueEvent]:
        events = list(self._pending_events)
        self._pending_events.clear()
        return events

    def with_duplicate_events(self, events: list[VenueEvent], *, event_ids: list[str]) -> list[VenueEvent]:
        duplicated: list[VenueEvent] = []
        targets = set(event_ids)
        for event in events:
            duplicated.append(event)
            if event.event_id in targets:
                duplicated.append(event)
        return duplicated

    def out_of_order(self, events: list[VenueEvent]) -> list[VenueEvent]:
        return list(reversed(events))

    def snapshot(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "start": self.start,
            "event_seq": self._event_seq,
            "orders": {client_id: order.snapshot() for client_id, order in self._orders.items()},
            "positions": {key: dict(position) for key, position in self._positions.items()},
        }

    def _apply_position_fill(self, order: VenueOrder, quantity: Decimal, price: Decimal) -> None:
        position_key = order.position_key or _position_key(self.account_id, order.instrument_id)
        current = self._position_quantity(position_key)
        reducing = order.reduce_only or order.lifecycle_role in {"stop_loss", "take_profit", "partial_close", "close"}
        if reducing:
            next_quantity = current - quantity
            if next_quantity <= 0:
                position = self._positions.get(position_key) or _position_payload(
                    self.account_id, order.instrument_id, position_key, Decimal("0"), price
                )
                position["quantity"] = "0"
                self._positions[position_key] = position
                self._emit_position("PositionClosed", position)
                return
            position = self._positions.get(position_key) or _position_payload(
                self.account_id, order.instrument_id, position_key, next_quantity, price
            )
            position["quantity"] = str(next_quantity)
            self._positions[position_key] = position
            self._emit_position("PositionChanged", position)
            return

        next_quantity = current + quantity
        position = self._positions.get(position_key) or _position_payload(
            self.account_id, order.instrument_id, position_key, next_quantity, price
        )
        position["quantity"] = str(next_quantity)
        position["avg_entry_price"] = str(price)
        self._positions[position_key] = position
        self._emit_position("PositionOpened" if current == 0 else "PositionChanged", position)

    def _position_quantity(self, position_key: str) -> Decimal:
        position = self._positions.get(position_key)
        if position is None:
            return Decimal("0")
        return _decimal(position["quantity"])

    def _emit_order(self, order: VenueOrder, event_type: str, extra_payload: dict[str, Any] | None = None) -> None:
        payload = {
            "instrument_id": order.instrument_id,
            "client_order_id": order.client_order_id,
            "venue_order_id": order.venue_order_id,
            "order_type": order.order_type,
            "side": order.side,
            "quantity": str(order.quantity),
            "filled_qty": str(order.filled_quantity),
            "leaves_qty": str(order.remaining_quantity),
            "price": str(order.price) if order.price is not None else None,
            "trigger_price": str(order.trigger_price) if order.trigger_price is not None else None,
            "lifecycle_role": order.lifecycle_role,
            "position_key": order.position_key,
            "reduce_only": order.reduce_only,
        }
        if extra_payload:
            payload.update(extra_payload)
        self._emit(event_type, order.client_order_id, order.venue_order_id, payload)

    def _emit_position(self, event_type: str, position: dict[str, Any]) -> None:
        payload = {
            "instrument_id": position["instrument_id"],
            "position_key": position["position_key"],
            "position_side": position.get("position_side", "BOTH"),
            "side": position.get("side", "long"),
            "quantity": position["quantity"],
            "avg_entry_price": position.get("avg_entry_price"),
        }
        self._emit(event_type, None, None, payload)

    def _emit(
        self,
        event_type: str,
        client_order_id: str | None,
        venue_order_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        self._event_seq += 1
        event = VenueEvent(
            event_id=f"sim-{self.account_id}-{self._event_seq:04d}",
            event_type=event_type,
            account_id=self.account_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            ts_event=self.start + timedelta(seconds=self._event_seq),
            payload={key: value for key, value in payload.items() if value is not None},
        )
        self._pending_events.append(event)
        self.event_history.append(event)


def _position_payload(
    account_id: str,
    instrument_id: str,
    position_key: str,
    quantity: Decimal,
    price: Decimal,
) -> dict[str, Any]:
    return {
        "position_id": position_key,
        "position_key": position_key,
        "account_id": account_id,
        "instrument_id": instrument_id,
        "position_side": "BOTH",
        "side": "long",
        "quantity": str(quantity),
        "avg_entry_price": str(price),
    }


def _order_from_snapshot(data: dict[str, Any]) -> VenueOrder:
    return VenueOrder(
        account_id=data["account_id"],
        client_order_id=data["client_order_id"],
        venue_order_id=data["venue_order_id"],
        instrument_id=data["instrument_id"],
        side=data["side"],
        order_type=data["order_type"],
        quantity=Decimal(str(data["quantity"])),
        price=_optional_decimal(data.get("price")),
        trigger_price=_optional_decimal(data.get("trigger_price")),
        lifecycle_role=data.get("lifecycle_role", "entry"),
        position_key=data.get("position_key"),
        reduce_only=bool(data.get("reduce_only", False)),
        status=data.get("status", "submitted"),
        filled_quantity=Decimal(str(data.get("filled_quantity", "0"))),
    )


def _parse_tags(tags: tuple[str, ...] | list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for tag in tags:
        if "=" in str(tag):
            key, value = str(tag).split("=", 1)
            parsed[key] = value
    return parsed


def _position_key(account_id: str, instrument_id: str) -> str:
    return f"{account_id}:{_symbol(instrument_id)}"


def _symbol(instrument_id: str) -> str:
    return str(instrument_id).split(".", 1)[0].split("-", 1)[0].upper()


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
