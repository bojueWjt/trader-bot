from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from psycopg2.extras import Json

from order_management.exits import close_position, partial_close


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_partial_close_is_reduce_only_capped_by_actual_position_and_recomputes_protection(db_conn) -> None:
    _seed_position_and_protection(db_conn, account_id="acct-om5-exit", quantity=Decimal("1"))
    exchange = _FakeExchange(actual_quantity=Decimal("0.8"), final_quantity=Decimal("0.4"))

    result = partial_close(
        db_conn,
        account_id="acct-om5-exit",
        position_key="acct-om5-exit:BTCUSDT",
        fraction=Decimal("0.5"),
        exchange=exchange,
        now=NOW,
    )

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT lifecycle_role, quantity
            FROM protective_orders_projection
            WHERE account_id='acct-om5-exit' AND active
            ORDER BY lifecycle_role, client_order_id
            """
        )
        rows = cur.fetchall()

    assert result.status == "submitted"
    assert exchange.submitted == [
        {
            "account_id": "acct-om5-exit",
            "position_key": "acct-om5-exit:BTCUSDT",
            "quantity": Decimal("0.4"),
            "reduce_only": True,
        }
    ]
    assert rows == [
        ("stop_loss", Decimal("0.4")),
        ("take_profit", Decimal("0.4")),
    ]


def test_partial_close_rejects_quantity_above_actual_position(db_conn) -> None:
    _seed_position_and_protection(db_conn, account_id="acct-om5-exceed", quantity=Decimal("1"))
    exchange = _FakeExchange(actual_quantity=Decimal("0.3"), final_quantity=Decimal("0.3"))

    result = partial_close(
        db_conn,
        account_id="acct-om5-exceed",
        position_key="acct-om5-exceed:BTCUSDT",
        quantity=Decimal("0.4"),
        exchange=exchange,
        now=NOW,
    )

    assert result.status == "denied"
    assert result.reason == "quantity_exceeds_actual_position"
    assert exchange.submitted == []


def test_close_position_uses_exchange_actual_quantity_cancels_residuals_and_requires_reconciliation(db_conn) -> None:
    _seed_position_and_protection(db_conn, account_id="acct-om5-full", quantity=Decimal("1"))
    exchange = _FakeExchange(actual_quantity=Decimal("0.8"), final_quantity=Decimal("0"))

    result = close_position(
        db_conn,
        account_id="acct-om5-full",
        position_key="acct-om5-full:BTCUSDT",
        exchange=exchange,
        reconcile=lambda: True,
        now=NOW,
    )

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT active
            FROM protective_orders_projection
            WHERE account_id='acct-om5-full'
            ORDER BY lifecycle_role
            """
        )
        active = [row[0] for row in cur.fetchall()]

    assert result.status == "completed"
    assert exchange.submitted[0]["quantity"] == Decimal("0.8")
    assert exchange.submitted[0]["reduce_only"] is True
    assert exchange.cancelled == ["stop-1", "tp-1"]
    assert active == [False, False]


def test_close_position_is_not_completed_until_final_reconciliation_passes(db_conn) -> None:
    _seed_position_and_protection(db_conn, account_id="acct-om5-verify", quantity=Decimal("1"))
    exchange = _FakeExchange(actual_quantity=Decimal("0.8"), final_quantity=Decimal("0"))

    result = close_position(
        db_conn,
        account_id="acct-om5-verify",
        position_key="acct-om5-verify:BTCUSDT",
        exchange=exchange,
        reconcile=lambda: False,
        now=NOW,
    )

    assert result.status == "verifying"
    assert result.reason == "final_reconciliation_failed"


def _seed_position_and_protection(conn, *, account_id: str, quantity: Decimal) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO positions_projection (
                account_id, position_id, instrument_id, side, quantity,
                avg_entry_price, status, updated_at, payload
            )
            VALUES (%s, %s, 'BTCUSDT-PERP.BINANCE', 'long', %s, 100, 'open', %s, %s)
            """,
            (account_id, f"{account_id}:BTCUSDT", quantity, NOW, Json({})),
        )
        cur.execute(
            """
            INSERT INTO protective_orders_projection (
                protective_order_projection_id, account_id, position_key, venue_symbol,
                lifecycle_role, client_order_id, status, active, quantity,
                trigger_price, updated_at, payload
            )
            VALUES (
                gen_random_uuid(), %s, %s, 'BTCUSDT',
                'stop_loss', 'stop-1', 'working', true, %s,
                90, %s, %s
            )
            """,
            (account_id, f"{account_id}:BTCUSDT", quantity, NOW, Json({})),
        )
        cur.execute(
            """
            INSERT INTO protective_orders_projection (
                protective_order_projection_id, account_id, position_key, venue_symbol,
                lifecycle_role, client_order_id, status, active, quantity,
                trigger_price, updated_at, payload
            )
            VALUES (
                gen_random_uuid(), %s, %s, 'BTCUSDT',
                'take_profit', 'tp-1', 'working', true, %s,
                120, %s, %s
            )
            """,
            (account_id, f"{account_id}:BTCUSDT", quantity, NOW, Json({})),
        )
    conn.commit()


class _FakeExchange:
    def __init__(self, *, actual_quantity: Decimal, final_quantity: Decimal) -> None:
        self.actual_quantity = actual_quantity
        self.final_quantity = final_quantity
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []

    def actual_position_quantity(self, *, account_id: str, position_key: str) -> Decimal:
        return self.actual_quantity

    def submit_reduce_only_market(self, *, account_id: str, position_key: str, quantity: Decimal) -> None:
        self.submitted.append(
            {
                "account_id": account_id,
                "position_key": position_key,
                "quantity": quantity,
                "reduce_only": True,
            }
        )
        self.actual_quantity = self.final_quantity

    def cancel_order(self, client_order_id: str) -> None:
        self.cancelled.append(client_order_id)

