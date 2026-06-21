from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable
from uuid import UUID

from commands.close_all import CloseAllSettings, close_all
from order_management.order_reducer import OrderProjectionReducer
from order_management.partial_fill import PartialFillRequest, handle_partial_fill
from order_management.position_reducer import PositionProjectionReducer
from order_management.unfilled_manager import (
    UnfilledOrder,
    UnfilledSettings,
    build_reprice_link_payload,
    decide_unfilled_action,
    record_reprice_link,
)
from strategy.intent_execution_planner import InstrumentSpec, OrderPlan
from strategy.protection import PositionProtectionSnapshot, StopProtectionSpec, build_stop_order_plan

from .fake_venue import FakeVenue, VenueEvent, VenueFault, VenueTimeout


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
BTC = "BTCUSDT-PERP.BINANCE"
ETH = "ETHUSDT-PERP.BINANCE"


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    event_count: int
    failed_checks: list[str]


class MemoryProjection:
    def __init__(self) -> None:
        self.orders: dict[tuple[str, str], dict] = {}
        self.positions: dict[tuple[str, str], dict] = {}
        self.order_event_ids: set[str] = set()
        self.position_event_ids: set[str] = set()
        self.order_links: list[dict] = []

    def apply_event(self, event: VenueEvent) -> None:
        if event.event_type.startswith("Order"):
            self._apply_order_event(event)
        elif event.event_type.startswith("Position"):
            self._apply_position_event(event)
        else:
            raise AssertionError(f"unsupported simulated event {event.event_type}")

    def record_reprice_link(self, *, account_id: str, parent: str, child: str, reprice_count: int) -> None:
        self.order_links.append(
            {
                "account_id": account_id,
                "parent_order_projection_id": parent,
                "child_order_projection_id": child,
                "reprice_count": reprice_count,
            }
        )

    def order_status(self, account_id: str, client_order_id: str) -> str | None:
        row = self.orders.get((account_id, client_order_id))
        return row["status"] if row else None

    def order_projection_id(self, account_id: str, client_order_id: str) -> str:
        row = self.orders.get((account_id, client_order_id))
        assert row is not None
        return row["order_projection_id"]

    def position_status(self, account_id: str, position_key: str) -> str | None:
        row = self.positions.get((account_id, position_key))
        return row["status"] if row else None

    def position_quantity(self, account_id: str, position_key: str) -> Decimal | None:
        row = self.positions.get((account_id, position_key))
        return row["quantity"] if row else None

    def count_orders(self, account_id: str, client_order_id: str) -> int:
        return int((account_id, client_order_id) in self.orders)

    def count_rows(self, table: str, account_id: str) -> int:
        if table == "order_links":
            return sum(1 for row in self.order_links if row["account_id"] == account_id)
        raise AssertionError(f"unsupported memory count table {table}")

    def _apply_order_event(self, event: VenueEvent) -> None:
        if event.event_id in self.order_event_ids:
            return
        self.order_event_ids.add(event.event_id)
        assert event.client_order_id is not None
        key = (event.account_id, event.client_order_id)
        current = self.orders.get(key)
        target = _order_target(event)
        if current is not None and _order_rank(target) <= _order_rank(current["status"]):
            current["events"].append(event.event_id)
            return
        self.orders[key] = {
            "order_projection_id": current["order_projection_id"] if current else f"mem-order-{len(self.orders) + 1}",
            "account_id": event.account_id,
            "client_order_id": event.client_order_id,
            "status": target,
            "filled_quantity": Decimal(str(event.payload.get("filled_qty", "0"))),
            "ts_event": event.ts_event,
            "events": [*(current or {}).get("events", []), event.event_id],
        }

    def _apply_position_event(self, event: VenueEvent) -> None:
        if event.event_id in self.position_event_ids:
            return
        self.position_event_ids.add(event.event_id)
        position_key = str(event.payload["position_key"])
        key = (event.account_id, position_key)
        current = self.positions.get(key)
        quantity = Decimal(str(event.payload.get("quantity", "0")))
        if event.event_type == "PositionClosed":
            status = "closed"
            quantity = Decimal("0")
        elif event.event_type == "PositionChanged" and current and quantity < current["quantity"]:
            status = "reducing"
        else:
            status = "open"
        self.positions[key] = {
            "account_id": event.account_id,
            "position_key": position_key,
            "status": status,
            "quantity": quantity,
            "ts_event": event.ts_event,
        }


@dataclass
class ScenarioContext:
    conn: object
    name: str
    venue: FakeVenue
    event_count: int = 0
    failed_checks: list[str] | None = None

    def __post_init__(self) -> None:
        if self.failed_checks is None:
            self.failed_checks = []
        self.orders = None if isinstance(self.conn, MemoryProjection) else OrderProjectionReducer()
        self.positions = None if isinstance(self.conn, MemoryProjection) else PositionProjectionReducer()

    @property
    def account_id(self) -> str:
        return self.venue.account_id

    def apply(self, events: list[VenueEvent]) -> None:
        for event in events:
            if isinstance(self.conn, MemoryProjection):
                self.conn.apply_event(event)
            elif event.event_type.startswith("Order"):
                assert self.orders is not None
                self.orders.apply_event(self.conn, event.as_projection_event())
            elif event.event_type.startswith("Position"):
                assert self.positions is not None
                self.positions.apply_event(self.conn, event.as_projection_event())
            else:
                raise AssertionError(f"unsupported simulated event {event.event_type}")
            self.event_count += 1

    def drain_apply(self) -> None:
        self.apply(self.venue.drain_events())

    def check(self, condition: bool, label: str) -> None:
        if not condition:
            assert self.failed_checks is not None
            self.failed_checks.append(label)


ScenarioFn = Callable[[ScenarioContext], None]


def run_scenario(conn, scenario_name: str) -> ScenarioResult:
    scenario = SCENARIOS[scenario_name]
    projection = conn if conn is not None else MemoryProjection()
    ctx = ScenarioContext(
        conn=projection,
        name=scenario_name,
        venue=FakeVenue(account_id=f"acct-sim-{scenario_name}", start=NOW),
    )
    scenario(ctx)
    return ScenarioResult(
        name=scenario_name,
        event_count=ctx.event_count,
        failed_checks=list(ctx.failed_checks or []),
    )


def market_open_sl_tp(ctx: ScenarioContext) -> None:
    ctx.venue.submit_order(
        client_order_id="entry-market",
        instrument_id=BTC,
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("1"),
    )
    ctx.venue.fill("entry-market", quantity=Decimal("1"), price=Decimal("64000"))
    ctx.drain_apply()

    stop_plan = build_stop_order_plan(
        intent_id=UUID("10000000-0000-4000-8000-000000000001"),
        account_id=ctx.account_id,
        instrument_id=BTC,
        position=PositionProtectionSnapshot(
            position_key=f"{ctx.account_id}:BTCUSDT",
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("64000"),
        ),
        stop=StopProtectionSpec(trigger_price=Decimal("63000")),
        instrument=_instrument(BTC),
    )
    ctx.venue.submit_plan(stop_plan)
    ctx.venue.submit_plan(_tp_plan("tp-1", ctx.account_id, Decimal("0.5"), Decimal("65000")))
    ctx.venue.submit_plan(_tp_plan("tp-2", ctx.account_id, Decimal("0.5"), Decimal("66000")))
    ctx.drain_apply()

    ctx.check(_order_status(ctx.conn, ctx.account_id, "entry-market") == "filled", "entry filled")
    ctx.check(_position_status(ctx.conn, ctx.account_id, f"{ctx.account_id}:BTCUSDT") == "open", "position open")
    ctx.check({order["lifecycle_role"] for order in ctx.venue.working_orders()} == {"stop_loss", "take_profit"}, "sl tp working")


def limit_timeout_cancel(ctx: ScenarioContext) -> None:
    ctx.venue.submit_order(
        client_order_id="limit-timeout",
        instrument_id=BTC,
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("1"),
        price=Decimal("63000"),
    )
    ctx.drain_apply()

    decision = decide_unfilled_action(
        UnfilledOrder(
            order_projection_id="limit-timeout-projection",
            client_order_id="limit-timeout",
            status="accepted",
            submitted_at=NOW,
        ),
        UnfilledSettings(unfilled_timeout_seconds=10),
        now=NOW + timedelta(seconds=11),
    )
    ctx.check(decision.action == "request_cancel", "timeout requests cancel")
    ctx.venue.cancel("limit-timeout")
    ctx.drain_apply()
    ctx.check(_order_status(ctx.conn, ctx.account_id, "limit-timeout") == "cancelled", "limit cancelled")


def reprice_fill(ctx: ScenarioContext) -> None:
    ctx.venue.submit_order(
        client_order_id="limit-parent",
        instrument_id=BTC,
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("1"),
        price=Decimal("63000"),
    )
    ctx.drain_apply()
    ctx.venue.cancel("limit-parent")
    ctx.drain_apply()

    decision = decide_unfilled_action(
        UnfilledOrder(
            order_projection_id=_order_projection_id(ctx.conn, ctx.account_id, "limit-parent"),
            client_order_id="limit-parent",
            status="cancelled",
            submitted_at=NOW,
            reprice_count=0,
        ),
        UnfilledSettings(unfilled_timeout_seconds=10, max_reprices=1),
        now=NOW + timedelta(seconds=12),
    )
    ctx.check(decision.action == "reprice", "cancel terminal reprices")
    ctx.venue.submit_order(
        client_order_id="limit-child",
        instrument_id=BTC,
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("1"),
        price=Decimal("63100"),
    )
    ctx.venue.fill("limit-child", quantity=Decimal("1"), price=Decimal("63100"))
    ctx.drain_apply()
    if isinstance(ctx.conn, MemoryProjection):
        ctx.conn.record_reprice_link(
            account_id=ctx.account_id,
            parent=_order_projection_id(ctx.conn, ctx.account_id, "limit-parent"),
            child=_order_projection_id(ctx.conn, ctx.account_id, "limit-child"),
            reprice_count=1,
        )
    else:
        record_reprice_link(
            ctx.conn,
            account_id=ctx.account_id,
            parent_order_projection_id=_order_projection_id(ctx.conn, ctx.account_id, "limit-parent"),
            child_order_projection_id=_order_projection_id(ctx.conn, ctx.account_id, "limit-child"),
            payload=build_reprice_link_payload(
                parent_client_order_id="limit-parent",
                child_client_order_id="limit-child",
                reprice_count=1,
            ),
        )
    ctx.check(_order_status(ctx.conn, ctx.account_id, "limit-child") == "filled", "reprice child filled")
    ctx.check(_count_rows(ctx.conn, "order_links", ctx.account_id) == 1, "reprice link recorded")


def partial_fill_keep_remainder(ctx: ScenarioContext) -> None:
    _partial_entry(ctx, "partial-keep")
    decision = handle_partial_fill(
        PartialFillRequest(
            policy="keep_remainder",
            original_quantity=Decimal("1"),
            filled_quantity=Decimal("0.4"),
            quantity_increment=Decimal("0.1"),
        )
    )
    ctx.check(decision.action == "keep_remainder", "keep policy")
    ctx.check(ctx.venue.order("partial-keep").status == "partially_filled", "venue remainder stays")
    ctx.check(_order_status(ctx.conn, ctx.account_id, "partial-keep") == "partially_filled", "projection partial")


def partial_fill_cancel_remainder(ctx: ScenarioContext) -> None:
    _partial_entry(ctx, "partial-cancel")
    decision = handle_partial_fill(
        PartialFillRequest(
            policy="cancel_remainder",
            original_quantity=Decimal("1"),
            filled_quantity=Decimal("0.4"),
            quantity_increment=Decimal("0.1"),
        )
    )
    ctx.check(decision.action == "cancel_remainder", "cancel policy")
    ctx.venue.cancel("partial-cancel")
    ctx.drain_apply()
    ctx.check(_order_status(ctx.conn, ctx.account_id, "partial-cancel") == "cancelled", "partial remainder cancelled")
    ctx.check(_position_quantity(ctx.conn, ctx.account_id, f"{ctx.account_id}:BTCUSDT") == Decimal("0.4"), "filled retained")


def stop_loss_fill(ctx: ScenarioContext) -> None:
    _seed_open_position(ctx)
    ctx.venue.submit_order(
        client_order_id="stop-loss",
        instrument_id=BTC,
        side="SELL",
        order_type="STOP_MARKET",
        quantity=Decimal("1"),
        trigger_price=Decimal("63000"),
        lifecycle_role="stop_loss",
        position_key=f"{ctx.account_id}:BTCUSDT",
        reduce_only=True,
    )
    ctx.drain_apply()
    ctx.venue.stop("stop-loss", price=Decimal("62990"))
    ctx.drain_apply()
    ctx.check(_order_status(ctx.conn, ctx.account_id, "stop-loss") == "filled", "stop filled")
    ctx.check(_position_status(ctx.conn, ctx.account_id, f"{ctx.account_id}:BTCUSDT") == "closed", "stop closed")


def batched_take_profit(ctx: ScenarioContext) -> None:
    _seed_open_position(ctx)
    for index, price in enumerate((Decimal("65000"), Decimal("66000")), start=1):
        ctx.venue.submit_order(
            client_order_id=f"tp-{index}",
            instrument_id=BTC,
            side="SELL",
            order_type="LIMIT",
            quantity=Decimal("0.5"),
            price=price,
            lifecycle_role="take_profit",
            position_key=f"{ctx.account_id}:BTCUSDT",
            reduce_only=True,
        )
    ctx.drain_apply()
    ctx.venue.take_profit("tp-1", quantity=Decimal("0.5"), price=Decimal("65000"))
    ctx.drain_apply()
    ctx.venue.take_profit("tp-2", quantity=Decimal("0.5"), price=Decimal("66000"))
    ctx.drain_apply()
    ctx.check(_position_status(ctx.conn, ctx.account_id, f"{ctx.account_id}:BTCUSDT") == "closed", "batched tp flat")
    ctx.check(all(_order_status(ctx.conn, ctx.account_id, f"tp-{i}") == "filled" for i in (1, 2)), "tp orders filled")


def move_stop(ctx: ScenarioContext) -> None:
    _seed_open_position(ctx)
    ctx.venue.submit_order(
        client_order_id="old-stop",
        instrument_id=BTC,
        side="SELL",
        order_type="STOP_MARKET",
        quantity=Decimal("1"),
        trigger_price=Decimal("63000"),
        lifecycle_role="stop_loss",
        position_key=f"{ctx.account_id}:BTCUSDT",
        reduce_only=True,
    )
    ctx.drain_apply()
    ctx.venue.submit_order(
        client_order_id="new-stop",
        instrument_id=BTC,
        side="SELL",
        order_type="STOP_MARKET",
        quantity=Decimal("1"),
        trigger_price=Decimal("63500"),
        lifecycle_role="stop_loss",
        position_key=f"{ctx.account_id}:BTCUSDT",
        reduce_only=True,
    )
    ctx.venue.cancel("old-stop")
    ctx.drain_apply()
    ctx.check(_order_status(ctx.conn, ctx.account_id, "old-stop") == "cancelled", "old stop cancelled")
    ctx.check(_order_status(ctx.conn, ctx.account_id, "new-stop") == "accepted", "new stop accepted")


def partial_close(ctx: ScenarioContext) -> None:
    _seed_open_position(ctx)
    ctx.venue.submit_order(
        client_order_id="partial-close",
        instrument_id=BTC,
        side="SELL",
        order_type="MARKET",
        quantity=Decimal("0.4"),
        lifecycle_role="partial_close",
        position_key=f"{ctx.account_id}:BTCUSDT",
        reduce_only=True,
    )
    ctx.venue.fill("partial-close", quantity=Decimal("0.4"), price=Decimal("64100"))
    ctx.drain_apply()
    ctx.check(_position_quantity(ctx.conn, ctx.account_id, f"{ctx.account_id}:BTCUSDT") == Decimal("0.6"), "partial close quantity")
    ctx.check(_position_status(ctx.conn, ctx.account_id, f"{ctx.account_id}:BTCUSDT") == "reducing", "partial close reducing")


def full_close(ctx: ScenarioContext) -> None:
    _seed_open_position(ctx)
    ctx.venue.submit_order(
        client_order_id="full-close",
        instrument_id=BTC,
        side="SELL",
        order_type="MARKET",
        quantity=Decimal("1"),
        lifecycle_role="close",
        position_key=f"{ctx.account_id}:BTCUSDT",
        reduce_only=True,
    )
    ctx.venue.fill("full-close", quantity=Decimal("1"), price=Decimal("64100"))
    ctx.drain_apply()
    ctx.check(_position_status(ctx.conn, ctx.account_id, f"{ctx.account_id}:BTCUSDT") == "closed", "full close flat")


def cancel_all(ctx: ScenarioContext) -> None:
    for client_id, role in (("entry-a", "entry"), ("stop-a", "stop_loss"), ("tp-a", "take_profit")):
        ctx.venue.submit_order(
            client_order_id=client_id,
            instrument_id=BTC,
            side="BUY" if role == "entry" else "SELL",
            order_type="LIMIT",
            quantity=Decimal("1"),
            price=Decimal("64000"),
            lifecycle_role=role,
            reduce_only=role != "entry",
        )
    ctx.drain_apply()
    ctx.venue.cancel_all()
    ctx.drain_apply()
    ctx.check(all(_order_status(ctx.conn, ctx.account_id, client_id) == "cancelled" for client_id in ("entry-a", "stop-a", "tp-a")), "all cancelled")


def close_all_multi_position(ctx: ScenarioContext) -> None:
    ctx.venue.open_position(instrument_id=BTC, quantity=Decimal("1"), price=Decimal("64000"))
    ctx.venue.open_position(instrument_id=ETH, quantity=Decimal("2"), price=Decimal("3400"))
    ctx.drain_apply()

    def close_one(**kwargs):
        return ctx.venue.close_position(kwargs["position_key"])

    result = close_all(
        None,
        account_id=ctx.account_id,
        venue=ctx.venue,
        close_one=close_one,
        settings=CloseAllSettings(max_attempts=1, verify_attempts=1),
        sleep=lambda _seconds: None,
    )
    ctx.drain_apply()
    ctx.check(result["status"] == "completed", "close_all completed")
    ctx.check(ctx.venue.list_open_positions(account_id=ctx.account_id) == [], "venue flat")


def node_restart_recovery(ctx: ScenarioContext) -> None:
    ctx.venue.submit_order(
        client_order_id="restart-order",
        instrument_id=BTC,
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("1"),
        price=Decimal("64000"),
    )
    ctx.drain_apply()
    snapshot = ctx.venue.snapshot()
    restarted = FakeVenue.from_snapshot(snapshot)
    ctx.apply(ctx.venue.event_history)
    ctx.check(restarted.submission_count == ctx.venue.submission_count, "restart no resubmit")
    ctx.check(_count_orders(ctx.conn, ctx.account_id, "restart-order") == 1, "restart replay idempotent")


def duplicate_out_of_order_no_duplicate_orders(ctx: ScenarioContext) -> None:
    ctx.venue.submit_order(
        client_order_id="ooo-order",
        instrument_id=BTC,
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("1"),
    )
    ctx.venue.fill("ooo-order", quantity=Decimal("1"), price=Decimal("64000"))
    events = ctx.venue.drain_events()
    injected = ctx.venue.with_duplicate_events(
        ctx.venue.out_of_order(events),
        event_ids=[events[-2].event_id],
    )
    ctx.apply(injected)
    ctx.check(_count_orders(ctx.conn, ctx.account_id, "ooo-order") == 1, "one order projection")
    ctx.check(_order_status(ctx.conn, ctx.account_id, "ooo-order") == "filled", "final status filled")


def accept_then_unknown_recovery(ctx: ScenarioContext) -> None:
    ctx.venue.inject_fault("uncertain-order", VenueFault.ACCEPT_THEN_UNKNOWN)
    try:
        ctx.venue.submit_order(
            client_order_id="uncertain-order",
            instrument_id=BTC,
            side="BUY",
            order_type="MARKET",
            quantity=Decimal("1"),
        )
    except VenueTimeout as exc:
        ctx.check(str(exc) == "accepted_unknown", "uncertain timeout")
    ctx.drain_apply()
    ctx.venue.submit_order(
        client_order_id="uncertain-order",
        instrument_id=BTC,
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("1"),
    )
    ctx.venue.fill("uncertain-order", quantity=Decimal("1"), price=Decimal("64000"))
    ctx.drain_apply()
    ctx.check(_count_orders(ctx.conn, ctx.account_id, "uncertain-order") == 1, "uncertain no duplicate")
    ctx.check(ctx.venue.submission_count == 1, "venue accepted once")


SCENARIOS: dict[str, ScenarioFn] = {
    "market_open_sl_tp": market_open_sl_tp,
    "limit_timeout_cancel": limit_timeout_cancel,
    "reprice_fill": reprice_fill,
    "partial_fill_keep_remainder": partial_fill_keep_remainder,
    "partial_fill_cancel_remainder": partial_fill_cancel_remainder,
    "stop_loss_fill": stop_loss_fill,
    "batched_take_profit": batched_take_profit,
    "move_stop": move_stop,
    "partial_close": partial_close,
    "full_close": full_close,
    "cancel_all": cancel_all,
    "close_all_multi_position": close_all_multi_position,
    "node_restart_recovery": node_restart_recovery,
    "duplicate_out_of_order_no_duplicate_orders": duplicate_out_of_order_no_duplicate_orders,
    "accept_then_unknown_recovery": accept_then_unknown_recovery,
}


def _partial_entry(ctx: ScenarioContext, client_order_id: str) -> None:
    ctx.venue.submit_order(
        client_order_id=client_order_id,
        instrument_id=BTC,
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("1"),
        price=Decimal("64000"),
    )
    ctx.venue.partial(client_order_id, quantity=Decimal("0.4"), price=Decimal("64000"))
    ctx.drain_apply()


def _seed_open_position(ctx: ScenarioContext) -> None:
    ctx.venue.open_position(instrument_id=BTC, quantity=Decimal("1"), price=Decimal("64000"))
    ctx.drain_apply()


def _instrument(instrument_id: str) -> InstrumentSpec:
    return InstrumentSpec(instrument_id=instrument_id, price_increment="0.01", quantity_increment="0.001")


def _tp_plan(client_order_id: str, account_id: str, quantity: Decimal, price: Decimal) -> OrderPlan:
    return OrderPlan(
        intent_id=UUID("20000000-0000-4000-8000-000000000001"),
        client_order_id=client_order_id,
        tags=(
            f"account_id={account_id}",
            "lifecycle_role=take_profit",
            f"position_id={account_id}:BTCUSDT",
        ),
        instrument_id=BTC,
        side="SELL",
        order_type="LIMIT",
        quantity=str(quantity),
        price=str(price),
        time_in_force="GTC",
        reduce_only=True,
    )


def _order_status(conn, account_id: str, client_order_id: str) -> str | None:
    if isinstance(conn, MemoryProjection):
        return conn.order_status(account_id, client_order_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status
            FROM orders_projection
            WHERE account_id=%s AND client_order_id=%s
            """,
            (account_id, client_order_id),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _order_projection_id(conn, account_id: str, client_order_id: str) -> str:
    if isinstance(conn, MemoryProjection):
        return conn.order_projection_id(account_id, client_order_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT order_projection_id::text
            FROM orders_projection
            WHERE account_id=%s AND client_order_id=%s
            """,
            (account_id, client_order_id),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _position_status(conn, account_id: str, position_key: str) -> str | None:
    if isinstance(conn, MemoryProjection):
        return conn.position_status(account_id, position_key)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status
            FROM positions_projection
            WHERE account_id=%s AND position_id=%s
            """,
            (account_id, position_key),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _position_quantity(conn, account_id: str, position_key: str) -> Decimal | None:
    if isinstance(conn, MemoryProjection):
        return conn.position_quantity(account_id, position_key)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT quantity
            FROM positions_projection
            WHERE account_id=%s AND position_id=%s
            """,
            (account_id, position_key),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _count_orders(conn, account_id: str, client_order_id: str) -> int:
    if isinstance(conn, MemoryProjection):
        return conn.count_orders(account_id, client_order_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)
            FROM orders_projection
            WHERE account_id=%s AND client_order_id=%s
            """,
            (account_id, client_order_id),
        )
        return int(cur.fetchone()[0])


def _count_rows(conn, table: str, account_id: str) -> int:
    if isinstance(conn, MemoryProjection):
        return conn.count_rows(table, account_id)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE account_id=%s", (account_id,))
        return int(cur.fetchone()[0])


def _order_target(event: VenueEvent) -> str:
    if event.event_type == "OrderSubmitted":
        return "submitted"
    if event.event_type == "OrderAccepted":
        return "accepted"
    if event.event_type == "OrderPendingCancel":
        return "pending_cancel"
    if event.event_type == "OrderCanceled":
        return "cancelled"
    if event.event_type == "OrderRejected":
        return "rejected"
    if event.event_type == "OrderExpired":
        return "expired"
    if event.event_type == "OrderFilled":
        leaves = Decimal(str(event.payload.get("leaves_qty", "0")))
        return "filled" if leaves <= 0 else "partially_filled"
    raise AssertionError(f"unsupported order event {event.event_type}")


def _order_rank(status: str) -> int:
    ranks = {
        "submitted": 1,
        "accepted": 2,
        "pending_cancel": 3,
        "partially_filled": 4,
        "cancelled": 5,
        "rejected": 5,
        "expired": 5,
        "filled": 6,
    }
    return ranks.get(status, 0)
