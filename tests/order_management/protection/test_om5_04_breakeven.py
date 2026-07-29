from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from psycopg2.extras import Json

from order_management.breakeven import BreakevenConfig, BreakevenManager


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_breakeven_applies_exactly_once_with_offset_and_fee_buffer(db_conn) -> None:
    _seed_position_and_stop(db_conn, account_id="acct-om5-be")
    submitter = _RecordingStopMover()
    manager = BreakevenManager(submitter=submitter)

    first = manager.evaluate_and_apply(
        db_conn,
        account_id="acct-om5-be",
        position_key="acct-om5-be:BTCUSDT",
        market_price=Decimal("110"),
        price_stale=False,
        config=BreakevenConfig(trigger_r=Decimal("1"), offset_bps=Decimal("5"), fee_buffer_bps=Decimal("2")),
        now=NOW,
    )
    second = manager.evaluate_and_apply(
        db_conn,
        account_id="acct-om5-be",
        position_key="acct-om5-be:BTCUSDT",
        market_price=Decimal("120"),
        price_stale=False,
        config=BreakevenConfig(trigger_r=Decimal("1"), offset_bps=Decimal("5"), fee_buffer_bps=Decimal("2")),
        now=NOW,
    )

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT trigger_price, payload->>'breakeven_applied'
            FROM protective_orders_projection
            WHERE account_id='acct-om5-be' AND lifecycle_role='stop_loss'
            """
        )
        trigger_price, applied = cur.fetchone()

    assert first.action == "applied"
    assert second.action == "already_applied"
    assert len(submitter.moves) == 1
    assert submitter.moves[0]["trigger_price"] == Decimal("100.07")
    assert trigger_price == Decimal("100.07")
    assert applied == "true"


def test_breakeven_does_not_apply_false_side_trigger_when_market_data_is_stale(db_conn) -> None:
    _seed_position_and_stop(db_conn, account_id="acct-om5-be-stale")
    submitter = _RecordingStopMover()
    manager = BreakevenManager(submitter=submitter)

    result = manager.evaluate_and_apply(
        db_conn,
        account_id="acct-om5-be-stale",
        position_key="acct-om5-be-stale:BTCUSDT",
        market_price=Decimal("999999"),
        price_stale=True,
        config=BreakevenConfig(trigger_r=Decimal("1"), offset_bps=Decimal("5"), fee_buffer_bps=Decimal("2")),
        now=NOW,
    )

    assert result.action == "blocked"
    assert result.reason == "market_data_stale"
    assert submitter.moves == []


def _seed_position_and_stop(conn, *, account_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO positions_projection (
                account_id, position_id, instrument_id, side, quantity,
                avg_entry_price, status, updated_at, payload
            )
            VALUES (%s, %s, 'BTCUSDT-PERP.BINANCE', 'long', 1, 100, 'open', %s, %s)
            """,
            (account_id, f"{account_id}:BTCUSDT", NOW, Json({})),
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
                'stop_loss', 'stop-1', 'working', true, 1,
                90, %s, %s
            )
            """,
            (account_id, f"{account_id}:BTCUSDT", NOW, Json({})),
        )
    conn.commit()


class _RecordingStopMover:
    def __init__(self) -> None:
        self.moves: list[dict] = []

    def move_stop(self, payload: dict) -> None:
        self.moves.append(payload)

