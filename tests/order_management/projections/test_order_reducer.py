from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from order_management.order_reducer import OrderProjectionReducer


BASE_TS = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_order_reducer_replays_ordered_events_to_filled(db_conn) -> None:
    reducer = OrderProjectionReducer()
    for event in (
        _order_event("evt-submit", "OrderSubmitted", 0),
        _order_event("evt-accept", "OrderAccepted", 1),
        _order_event(
            "evt-fill-1",
            "OrderFilled",
            2,
            payload={"filled_qty": "0.4", "leaves_qty": "0.6", "quantity": "1"},
        ),
        _order_event(
            "evt-fill-2",
            "OrderFilled",
            3,
            payload={"filled_qty": "1", "leaves_qty": "0", "quantity": "1"},
        ),
    ):
        reducer.apply_event(db_conn, event)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, filled_quantity FROM orders_projection "
            "WHERE account_id='acct-om2' AND client_order_id='coid-1'"
        )
        status, filled_quantity = cur.fetchone()

    assert status == "filled"
    assert filled_quantity == Decimal("1")


def test_order_reducer_duplicate_and_out_of_order_events_converge(db_conn) -> None:
    reducer = OrderProjectionReducer()
    accepted = _order_event("evt-accept", "OrderAccepted", 2)
    submitted = _order_event("evt-submit", "OrderSubmitted", 1)

    reducer.apply_event(db_conn, accepted)
    reducer.apply_event(db_conn, submitted)
    reducer.apply_event(db_conn, accepted)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, updated_from_event_id FROM orders_projection "
            "WHERE account_id='acct-om2' AND client_order_id='coid-1'"
        )
        status, event_id = cur.fetchone()
        cur.execute("SELECT count(*) FROM order_events")
        event_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM reconciliation_findings")
        finding_count = cur.fetchone()[0]

    assert status == "accepted"
    assert event_id == "evt-accept"
    assert event_count == 2
    assert finding_count == 0


def test_order_reducer_records_illegal_backward_transition_without_overwrite(db_conn) -> None:
    reducer = OrderProjectionReducer()
    reducer.apply_event(db_conn, _order_event("evt-submit", "OrderSubmitted", 0))
    reducer.apply_event(db_conn, _order_event("evt-accept", "OrderAccepted", 1))
    reducer.apply_event(db_conn, _order_event("evt-late-submit", "OrderSubmitted", 3))

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, updated_from_event_id FROM orders_projection "
            "WHERE account_id='acct-om2' AND client_order_id='coid-1'"
        )
        status, event_id = cur.fetchone()
        cur.execute(
            "SELECT finding_type, severity, payload->>'from_status', payload->>'to_status' "
            "FROM reconciliation_findings"
        )
        finding_type, severity, from_status, to_status = cur.fetchone()

    assert status == "accepted"
    assert event_id == "evt-accept"
    assert finding_type == "illegal_order_transition"
    assert severity == "error"
    assert from_status == "accepted"
    assert to_status == "submitted"


def _order_event(
    event_id: str,
    event_type: str,
    seconds: int,
    *,
    payload: dict | None = None,
) -> dict:
    body = {
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "side": "long",
        "order_type": "limit",
        "quantity": "1",
        "filled_qty": "0",
        "leaves_qty": "1",
    }
    body.update(payload or {})
    return {
        "event_id": event_id,
        "schema_version": "1.0",
        "node_id": "node-1",
        "account_id": "acct-om2",
        "client_order_id": "coid-1",
        "venue_order_id": "venue-1",
        "event_type": event_type,
        "ts_event": BASE_TS + timedelta(seconds=seconds),
        "payload": body,
    }
