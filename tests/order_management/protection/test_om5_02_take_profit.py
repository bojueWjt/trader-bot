from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from psycopg2.extras import Json

from strategy.intent_execution_planner import InstrumentSpec
from strategy.protection import PositionProtectionSnapshot
from strategy.take_profit import (
    TakeProfitDenied,
    TakeProfitTarget,
    build_take_profit_ladder,
    recompute_remaining_quantities,
    record_take_profit_links,
)


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_take_profit_ladder_batches_levels_and_caps_total_quantity() -> None:
    plans = build_take_profit_ladder(
        intent_id="33333333-3333-4333-8333-333333333333",
        account_id="acct-om5-tp",
        instrument_id="BTCUSDT-PERP.BINANCE",
        position=PositionProtectionSnapshot(
            position_key="acct-om5-tp:BTCUSDT",
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
        ),
        initial_stop_price=Decimal("90"),
        targets=(
            TakeProfitTarget(price=Decimal("110"), fraction=Decimal("0.3")),
            TakeProfitTarget(r_multiple=Decimal("2"), quantity=Decimal("0.2")),
        ),
        instrument=InstrumentSpec(
            instrument_id="BTCUSDT-PERP.BINANCE",
            price_increment="0.01",
            quantity_increment="0.001",
        ),
    )

    assert [plan.order_type for plan in plans] == ["LIMIT_IF_TOUCHED", "LIMIT_IF_TOUCHED"]
    assert [plan.quantity for plan in plans] == ["0.300", "0.200"]
    assert [plan.trigger_price for plan in plans] == ["110.00", "120.00"]
    assert all(plan.reduce_only for plan in plans)
    assert "take_profit_index=1" in plans[0].tags
    assert "take_profit_index=2" in plans[1].tags


def test_take_profit_ladder_rejects_total_quantity_above_position() -> None:
    result = build_take_profit_ladder(
        intent_id="44444444-4444-4444-8444-444444444444",
        account_id="acct-om5-tp",
        instrument_id="BTCUSDT-PERP.BINANCE",
        position=PositionProtectionSnapshot(
            position_key="acct-om5-tp:BTCUSDT",
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
        ),
        initial_stop_price=Decimal("90"),
        targets=(
            TakeProfitTarget(price=Decimal("110"), fraction=Decimal("0.6")),
            TakeProfitTarget(price=Decimal("120"), quantity=Decimal("0.5")),
        ),
        instrument=InstrumentSpec(
            instrument_id="BTCUSDT-PERP.BINANCE",
            price_increment="0.01",
            quantity_increment="0.001",
        ),
    )

    assert result == TakeProfitDenied("quantity_exceeds_position", "1.100>1")


def test_take_profit_levels_are_traceable_through_order_links(db_conn) -> None:
    parent_id = _insert_order(db_conn, client_order_id="entry-1", role="entry")
    child_ids = (
        _insert_order(db_conn, client_order_id="tp-1", role="take_profit"),
        _insert_order(db_conn, client_order_id="tp-2", role="take_profit"),
    )

    record_take_profit_links(
        db_conn,
        account_id="acct-om5-tp",
        position_key="acct-om5-tp:BTCUSDT",
        parent_order_projection_id=parent_id,
        child_order_projection_ids=child_ids,
        levels=(
            {"index": 1, "trigger_price": "110.00"},
            {"index": 2, "trigger_price": "120.00"},
        ),
    )

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT lifecycle_role, link_type, payload->>'level_index'
            FROM order_links
            WHERE account_id='acct-om5-tp'
            ORDER BY payload->>'level_index'
            """
        )
        rows = cur.fetchall()

    assert rows == [
        ("take_profit", "protection", "1"),
        ("take_profit", "protection", "2"),
    ]


def test_partial_fill_recompute_caps_remaining_stop_and_tp_quantities() -> None:
    result = recompute_remaining_quantities(
        position_quantity=Decimal("0.7"),
        stop_quantity=Decimal("1.0"),
        take_profit_quantities=(Decimal("0.4"), Decimal("0.4")),
    )

    assert result.stop_quantity == Decimal("0.7")
    assert result.take_profit_quantities == (Decimal("0.4"), Decimal("0.3"))
    assert sum(result.take_profit_quantities) <= Decimal("0.7")


def _insert_order(db_conn, *, client_order_id: str, role: str) -> str:
    order_id = str(uuid4())
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders_projection (
                order_projection_id, account_id, instrument_id, venue_symbol,
                client_order_id, status, side, order_type, quantity,
                lifecycle_role, updated_at, payload
            )
            VALUES (%s, 'acct-om5-tp', 'BTCUSDT-PERP.BINANCE', 'BTCUSDT',
                    %s, 'working', 'long', 'LIMIT', 1, %s, %s, %s)
            """,
            (order_id, client_order_id, role, NOW, Json({})),
        )
    db_conn.commit()
    return order_id

