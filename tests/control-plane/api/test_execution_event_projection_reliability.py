"""WP-A regression tests: post_node_events projection channel reliability.

Covers the 2026-08 live bug where OrderProjectionReducer.apply_event was called
with manage_transaction=False against a signature that did not accept it: every
order event raised TypeError inside the savepoint, the bare except swallowed it,
the event was ACKed anyway, and orders_projection silently stopped updating.

Target behaviour under test (see
docs/plans/2026-08-28-execution-state-arch-migration.md, WP-A):
- Order events posted through /v1/nodes/{node_id}/execution-events project
  into orders_projection (direct regression for the live bug).
- A bad payload keeps the raw-event-durable / batch-survives semantics but must
  leave a row in projection_failures instead of being silently swallowed.
- projection_watermarks advances on successful derivation (projector='orders'
  for order events, projector='positions' for the position hint path) and does
  not advance on failed derivation.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient
from psycopg2.extensions import make_dsn

import read_api
from app_roles import AppRole


ACCOUNT_ID = "account-a"
NODE_ID = "nautilus-node-account-a"
NODE_TOKEN = "node-a-token"
VIEWER_TOKEN = "viewer-token"
REDIS_FENCING_EPOCH = "11111111-1111-4111-8111-111111111111"
RUNTIME_GENERATION = "runtime-proj"
LEASE_FENCING_TOKEN = 7

INSTRUMENT_ID = "ATOMUSDT-PERP.BINANCE"

T1 = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 8, 28, 10, 0, 5, tzinfo=timezone.utc)
T3 = datetime(2026, 8, 28, 10, 0, 10, tzinfo=timezone.utc)


@pytest.fixture()
def client(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: str,
) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("VIEWER_TOKEN", VIEWER_TOKEN)
    monkeypatch.setenv(
        "NAUTILUS_NODE_AUTH_JSON",
        json.dumps(
            {
                NODE_ID: {
                    "account_id": ACCOUNT_ID,
                    "token": NODE_TOKEN,
                }
            }
        ),
    )
    _activate_redis_epoch(migrated_db)
    test_client = TestClient(read_api.app)
    response = test_client.post(
        f"/v1/nodes/{NODE_ID}/heartbeat",
        headers=_writer_headers(),
        json={
            "account_id": ACCOUNT_ID,
            "ts": datetime.now(timezone.utc).isoformat(),
            "trading_state": "HALTED",
            "readiness": True,
            "projection_lag_ms": 0,
            "reconciliation_state": "healthy",
            "redis_fencing_epoch": REDIS_FENCING_EPOCH,
            "runtime_generation": RUNTIME_GENERATION,
            "lease_fencing_token": LEASE_FENCING_TOKEN,
            "heartbeat_sequence": 1,
        },
    )
    assert response.status_code == 200
    return test_client


def test_order_accepted_event_projects_into_orders_projection(
    client: TestClient,
    migrated_db: str,
) -> None:
    """Direct regression for the live bug: an order event pushed through
    post_node_events must produce an orders_projection row."""
    client_order_id = _client_order_id()
    event = _order_accepted_event(client_order_id, ts_event=T1)

    response = _post_events(client, [event])

    assert response.status_code == 200
    assert response.json()["acked_event_ids"] == [event["event_id"]]

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, instrument_id, side::text, quantity
            FROM orders_projection
            WHERE account_id=%s AND client_order_id=%s
            """,
            (ACCOUNT_ID, client_order_id),
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) FROM execution_events WHERE event_id=%s",
            (event["event_id"],),
        )
        raw_count = cur.fetchone()[0]

    assert raw_count == 1
    assert row is not None, (
        "order event was ACKed but orders_projection has no row "
        "(the silent projection-stall bug)"
    )
    status, instrument_id, side, quantity = row
    assert status == "accepted"
    assert instrument_id == INSTRUMENT_ID
    assert side == "long"
    assert quantity == Decimal("5")


def test_three_native_close_fills_project_actual_cumulative_quantity(
    client: TestClient, migrated_db: str,
) -> None:
    intent_id = _seed_approved_intent(migrated_db)
    client_order_id = f"B{intent_id.replace('-', '')}01"
    accepted = _order_accepted_event(client_order_id, ts_event=T1, intent_id=intent_id)
    accepted["payload"].update(quantity="1.936", side="SELL", order_type="MARKET", reduce_only=True)
    assert _post_events(client, [accepted]).status_code == 200
    fills = []
    for index, quantity in enumerate(("0.5", "0.5", "0.936"), 1):
        event = _order_filled_event(client_order_id, ts_event=T1 + timedelta(seconds=index), intent_id=intent_id)
        event["trade_id"] = f"venue-close-fill-{index}"
        event["payload"] = {"instrument_id": INSTRUMENT_ID, "last_qty": quantity, "last_px": "2452.19"}
        fills.append(event)
    for events in ([fills[2]], [fills[0], fills[1]], fills):
        assert _post_events(client, events).status_code == 200
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status,quantity,filled_quantity,average_fill_price,order_type FROM orders_projection WHERE client_order_id=%s", (client_order_id,))
        assert cur.fetchone() == ("filled", Decimal("1.936"), Decimal("1.936"), Decimal("2452.19"), "MARKET")
        cur.execute("SELECT count(*) FROM execution_events WHERE intent_id=%s AND event_type='OrderFilled'", (intent_id,))
        assert cur.fetchone()[0] == 3


def test_order_denied_event_is_durable_and_queryable(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _seed_approved_intent(migrated_db)
    filled_client_order_id = f"B{intent_id.replace('-', '')}01"
    denied_client_order_id = f"B{intent_id.replace('-', '')}02"
    filled = _order_filled_event(
        filled_client_order_id,
        ts_event=T1,
        intent_id=intent_id,
    )
    denied = _order_denied_event(
        denied_client_order_id,
        ts_event=T2,
        intent_id=intent_id,
    )

    posted = _post_events(client, [filled, denied])
    assert posted.status_code == 200
    assert posted.json()["acked_event_ids"] == [
        filled["event_id"],
        denied["event_id"],
    ]

    ack = client.post(
        f"/v1/nodes/{NODE_ID}/intents/{intent_id}/ack",
        headers=_writer_headers(),
        json={
            "account_id": ACCOUNT_ID,
            "status": "rejected",
            "detail": "denied:lot_size:filter",
        },
    )
    assert ack.status_code == 200

    query = client.get(
        f"/v1/operator/orders/{intent_id}",
        headers=_reader_headers(),
    )
    assert query.status_code == 200
    body = query.json()
    assert body["intent"]["status"] == "rejected"
    assert body["status"] == "partial"
    assert body["denial_reason"]
    assert "lot" in str(body["denial_reason"]).lower()
    assert body["intent"].get("denial_reason") == body["denial_reason"]
    orders = body["orders"]
    filled_rows = [
        row
        for row in orders
        if row["client_order_id"] == filled_client_order_id
    ]
    assert filled_rows
    assert str(filled_rows[0]["status"]).lower() == "filled"
    assert Decimal(str(filled_rows[0]["filled_quantity"])) == Decimal("5")
    event_types = {row["event_type"] for row in body["execution_events"]}
    assert "OrderDenied" in event_types
    assert "OrderFilled" in event_types

    dashboard_orders = client.get("/v1/orders", headers=_reader_headers())
    assert dashboard_orders.status_code == 200
    by_id = {
        row["client_order_id"]: row
        for row in dashboard_orders.json()["orders"]
        if row.get("client_order_id")
        in {filled_client_order_id, denied_client_order_id}
    }
    assert by_id[filled_client_order_id]["status"] == "filled"
    assert Decimal(str(by_id[filled_client_order_id]["filled"])) == Decimal("5")
    assert by_id[denied_client_order_id]["status"] == "denied"
    denied_events = by_id[denied_client_order_id].get("events") or []
    assert denied_events
    assert "lot" in str(
        denied_events[0].get("detail") or denied_events[0].get("message") or ""
    ).lower()
    trades = client.get("/v1/trades", headers=_reader_headers())
    assert trades.status_code == 200
    assert all(
        row.get("status") != "rejected"
        for row in trades.json()["trades"]
    )


def test_zero_fill_deny_is_rejected_on_operator_and_orders(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _seed_approved_intent(migrated_db)
    denied_client_order_id = f"B{intent_id.replace('-', '')}01"
    denied = _order_denied_event(
        denied_client_order_id,
        ts_event=T1,
        intent_id=intent_id,
    )
    posted = _post_events(client, [denied])
    assert posted.status_code == 200
    ack = client.post(
        f"/v1/nodes/{NODE_ID}/intents/{intent_id}/ack",
        headers=_writer_headers(),
        json={
            "account_id": ACCOUNT_ID,
            "status": "rejected",
            "detail": "denied:lot_size:filter",
        },
    )
    assert ack.status_code == 200
    body = client.get(
        f"/v1/operator/orders/{intent_id}",
        headers=_reader_headers(),
    ).json()
    assert body["intent"]["status"] == "rejected"
    assert body["status"] == "rejected"
    assert "lot" in str(body["denial_reason"]).lower()
    dashboard_orders = client.get("/v1/orders", headers=_reader_headers()).json()
    denied_rows = [
        row
        for row in dashboard_orders["orders"]
        if row.get("client_order_id") == denied_client_order_id
    ]
    assert denied_rows
    assert denied_rows[0]["status"] == "denied"
    denied_events = denied_rows[0].get("events") or []
    assert denied_events
    assert "lot" in str(
        denied_events[0].get("detail") or denied_events[0].get("message") or ""
    ).lower()


def test_protection_leg_deny_does_not_reject_filled_operation(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _seed_approved_intent(migrated_db)
    filled_client_order_id = f"B{intent_id.replace('-', '')}01"
    protection_client_order_id = f"B{intent_id.replace('-', '')}11"
    filled = _order_filled_event(
        filled_client_order_id,
        ts_event=T1,
        intent_id=intent_id,
    )
    denied = _order_denied_event(
        protection_client_order_id,
        ts_event=T2,
        intent_id=intent_id,
        lifecycle_role="stop_loss",
        order_type="STOP_MARKET",
    )
    posted = _post_events(client, [filled, denied])
    assert posted.status_code == 200
    body = client.get(
        f"/v1/operator/orders/{intent_id}",
        headers=_reader_headers(),
    ).json()
    assert body["intent"]["status"] == "approved"
    assert body["status"] == "filled"
    filled_rows = [
        row
        for row in body["orders"]
        if row["client_order_id"] == filled_client_order_id
    ]
    assert filled_rows
    assert str(filled_rows[0]["status"]).lower() == "filled"
    dashboard_orders = client.get("/v1/orders", headers=_reader_headers()).json()
    by_id = {
        row["client_order_id"]: row
        for row in dashboard_orders["orders"]
        if row.get("client_order_id")
        in {filled_client_order_id, protection_client_order_id}
    }
    assert by_id[filled_client_order_id]["status"] == "filled"
    assert Decimal(str(by_id[filled_client_order_id]["filled"])) == Decimal("5")
    assert by_id[protection_client_order_id]["status"] == "denied"


def test_operator_partially_filled_entry_is_partial(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _seed_approved_intent(migrated_db)
    client_order_id = f"B{intent_id.replace('-', '')}01"
    posted = _post_events(
        client,
        [
            _order_filled_event(
                client_order_id,
                ts_event=T1,
                intent_id=intent_id,
                quantity="5",
                filled_qty="2",
                leaves_qty="3",
            )
        ],
    )
    assert posted.status_code == 200
    body = client.get(
        f"/v1/operator/orders/{intent_id}",
        headers=_reader_headers(),
    ).json()
    assert body["intent"]["status"] == "approved"
    assert body["status"] == "partial"
    assert str(body["orders"][0]["status"]).lower() == "partially_filled"
    assert Decimal(str(body["orders"][0]["filled_quantity"])) == Decimal("2")


def test_operator_filled_plus_pending_entry_is_partial(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _seed_approved_intent(migrated_db)
    filled_id = f"B{intent_id.replace('-', '')}01"
    pending_id = f"B{intent_id.replace('-', '')}02"
    posted = _post_events(
        client,
        [
            _order_filled_event(
                filled_id,
                ts_event=T1,
                intent_id=intent_id,
            ),
            _order_accepted_event(
                pending_id,
                ts_event=T2,
                intent_id=intent_id,
            ),
        ],
    )
    assert posted.status_code == 200
    body = client.get(
        f"/v1/operator/orders/{intent_id}",
        headers=_reader_headers(),
    ).json()
    assert body["intent"]["status"] == "approved"
    assert body["status"] == "partial"
    by_id = {row["client_order_id"]: row for row in body["orders"]}
    assert str(by_id[filled_id]["status"]).lower() == "filled"
    assert Decimal(str(by_id[filled_id]["filled_quantity"])) == Decimal("5")
    assert str(by_id[pending_id]["status"]).lower() == "accepted"


def test_v1_orders_does_not_invent_deny_events_from_generic_reason(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _seed_approved_intent(migrated_db)
    filled_id = f"B{intent_id.replace('-', '')}01"
    canceled_id = f"B{intent_id.replace('-', '')}02"
    filled = _order_filled_event(
        filled_id,
        ts_event=T1,
        intent_id=intent_id,
    )
    filled["payload"]["reason"] = "user_requested"
    canceled = {
        "event_id": str(uuid4()),
        "event_type": "OrderCanceled",
        "account_id": ACCOUNT_ID,
        "node_id": NODE_ID,
        "client_order_id": canceled_id,
        "intent_id": intent_id,
        "ts_event": T2.isoformat(),
        "payload": {
            "instrument_id": INSTRUMENT_ID,
            "client_order_id": canceled_id,
            "reason": "user_requested",
            "order_type": "LIMIT",
            "quantity": "5",
        },
    }
    posted = _post_events(client, [filled, canceled])
    assert posted.status_code == 200
    dashboard_orders = client.get("/v1/orders", headers=_reader_headers()).json()
    by_id = {
        row["client_order_id"]: row
        for row in dashboard_orders["orders"]
        if row.get("client_order_id") in {filled_id, canceled_id}
    }
    assert by_id[filled_id]["status"] == "filled"
    assert by_id[filled_id].get("events") == []
    assert not by_id[filled_id].get("denial_reason")
    assert str(by_id[canceled_id]["status"]).lower() in {"canceled", "cancelled"}
    assert by_id[canceled_id].get("events") == []
    assert not by_id[canceled_id].get("denial_reason")


def test_order_denied_projects_raw_denied_status(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _seed_approved_intent(migrated_db)
    client_order_id = f"B{intent_id.replace('-', '')}01"
    denied = _order_denied_event(
        client_order_id,
        ts_event=T1,
        intent_id=intent_id,
    )
    posted = _post_events(client, [denied])
    assert posted.status_code == 200
    assert _projection_status(migrated_db, client_order_id) == "denied"
    dashboard = client.get("/v1/orders", headers=_reader_headers()).json()
    row = next(
        item
        for item in dashboard["orders"]
        if item.get("client_order_id") == client_order_id
    )
    assert row["status"] == "denied"
    events = row.get("events") or []
    assert events
    assert events[0].get("event_type") == "OrderDenied"
    assert "lot" in str(events[0].get("detail") or events[0].get("message") or "").lower()


def test_pending_submit_and_submitted_order_denied_persists(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _seed_approved_intent(migrated_db)
    pending_id = f"B{intent_id.replace('-', '')}01"
    submitted_id = f"B{intent_id.replace('-', '')}02"
    _insert_orders_projection(
        migrated_db,
        client_order_id=pending_id,
        status="pending_submit",
        intent_id=intent_id,
    )
    posted = _post_events(
        client,
        [
            _order_submitted_event(
                submitted_id,
                ts_event=T1,
                intent_id=intent_id,
            ),
            _order_denied_event(
                pending_id,
                ts_event=T2,
                intent_id=intent_id,
            ),
            _order_denied_event(
                submitted_id,
                ts_event=T3,
                intent_id=intent_id,
            ),
        ],
    )
    assert posted.status_code == 200
    assert _projection_status(migrated_db, pending_id) == "denied"
    assert _projection_status(migrated_db, submitted_id) == "denied"
    dashboard = client.get("/v1/orders", headers=_reader_headers()).json()
    by_id = {
        row["client_order_id"]: row
        for row in dashboard["orders"]
        if row.get("client_order_id") in {pending_id, submitted_id}
    }
    assert by_id[pending_id]["status"] == "denied"
    assert by_id[submitted_id]["status"] == "denied"
    assert by_id[pending_id].get("events")
    assert by_id[submitted_id].get("events")


def test_lost_projection_with_reason_is_not_denied(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _seed_approved_intent(migrated_db)
    lost_id = f"B{intent_id.replace('-', '')}01"
    _insert_orders_projection(
        migrated_db,
        client_order_id=lost_id,
        status="lost",
        intent_id=intent_id,
        payload={"reason": "ws-timeout", "detail": "gap after reconnect"},
    )
    dashboard = client.get("/v1/orders", headers=_reader_headers()).json()
    row = next(
        item
        for item in dashboard["orders"]
        if item.get("client_order_id") == lost_id
    )
    assert row["status"] == "lost"
    assert row.get("events") == []
    assert not row.get("denial_reason")
    body = client.get(
        f"/v1/operator/orders/{intent_id}",
        headers=_reader_headers(),
    ).json()
    assert body["intent"]["status"] == "approved"
    assert body["status"] != "rejected"
    assert str(body["orders"][0]["status"]).lower() == "lost"


def test_filled_order_denied_does_not_degrade_status(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _seed_approved_intent(migrated_db)
    client_order_id = f"B{intent_id.replace('-', '')}01"
    filled = _order_filled_event(
        client_order_id,
        ts_event=T1,
        intent_id=intent_id,
    )
    denied = _order_denied_event(
        client_order_id,
        ts_event=T2,
        intent_id=intent_id,
    )
    posted = _post_events(client, [filled, denied])
    assert posted.status_code == 200
    assert _projection_status(migrated_db, client_order_id) == "filled"
    dashboard = client.get("/v1/orders", headers=_reader_headers()).json()
    row = next(
        item
        for item in dashboard["orders"]
        if item.get("client_order_id") == client_order_id
    )
    assert row["status"] == "filled"
    assert Decimal(str(row["filled"])) == Decimal("5")
    assert row.get("events") == []
    assert not row.get("denial_reason")
    body = client.get(
        f"/v1/operator/orders/{intent_id}",
        headers=_reader_headers(),
    ).json()
    assert str(body["orders"][0]["status"]).lower() == "filled"
    assert body["status"] == "filled"


def test_order_filled_event_updates_projection_row(
    client: TestClient,
    migrated_db: str,
) -> None:
    client_order_id = _client_order_id()
    accepted = _order_accepted_event(client_order_id, ts_event=T1)
    filled = _order_filled_event(client_order_id, ts_event=T2)

    response = _post_events(client, [accepted, filled])

    assert response.status_code == 200
    assert response.json()["acked_event_ids"] == [
        accepted["event_id"],
        filled["event_id"],
    ]

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, filled_quantity
            FROM orders_projection
            WHERE account_id=%s AND client_order_id=%s
            """,
            (ACCOUNT_ID, client_order_id),
        )
        row = cur.fetchone()

    assert row is not None
    status, filled_quantity = row
    assert status == "filled"
    assert filled_quantity == Decimal("5")


def test_bad_payload_is_acked_recorded_and_does_not_break_batch(
    client: TestClient,
    migrated_db: str,
) -> None:
    """A malformed payload must keep the existing durability semantics (raw
    event stored, event ACKed, rest of the batch derived) but leave an
    unresolved projection_failures row instead of a silent swallow."""
    good_before_id = _client_order_id()
    good_after_id = _client_order_id()
    good_before = _order_accepted_event(good_before_id, ts_event=T1)
    bad = _poison_position_event(ts_event=T2)
    good_after = _order_accepted_event(good_after_id, ts_event=T3)

    response = _post_events(client, [good_before, bad, good_after])

    assert response.status_code == 200
    assert response.json()["acked_event_ids"] == [
        good_before["event_id"],
        bad["event_id"],
        good_after["event_id"],
    ]

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        for event in (good_before, bad, good_after):
            cur.execute(
                "SELECT COUNT(*) FROM execution_events WHERE event_id=%s",
                (event["event_id"],),
            )
            assert cur.fetchone()[0] == 1, (
                f"raw event {event['event_id']} must stay durable"
            )
        for client_order_id in (good_before_id, good_after_id):
            cur.execute(
                """
                SELECT status FROM orders_projection
                WHERE account_id=%s AND client_order_id=%s
                """,
                (ACCOUNT_ID, client_order_id),
            )
            row = cur.fetchone()
            assert row == ("accepted",), (
                "healthy events in the batch must still derive"
            )
        cur.execute(
            """
            SELECT account_id, projector, error, resolved_at
            FROM projection_failures
            WHERE event_id=%s
            """,
            (bad["event_id"],),
        )
        failure = cur.fetchone()

    assert failure is not None, (
        "failed derivation must be recorded in projection_failures"
    )
    account_id, projector, error, resolved_at = failure
    assert account_id == ACCOUNT_ID
    assert projector in ("positions", "orders")
    assert error, "error column must carry the exception detail"
    assert resolved_at is None


def test_watermark_advances_with_each_successful_derivation(
    client: TestClient,
    migrated_db: str,
) -> None:
    client_order_id = _client_order_id()
    accepted = _order_accepted_event(client_order_id, ts_event=T1)
    filled = _order_filled_event(client_order_id, ts_event=T2)

    assert _post_events(client, [accepted]).status_code == 200
    watermark = _watermark(migrated_db, projector="orders")
    assert watermark is not None, (
        "successful derivation must upsert projection_watermarks"
    )
    assert watermark[0] == accepted["event_id"]
    assert watermark[1] == T1

    assert _post_events(client, [filled]).status_code == 200
    watermark = _watermark(migrated_db, projector="orders")
    assert watermark is not None
    assert watermark[0] == filled["event_id"]
    assert watermark[1] == T2


def test_watermark_does_not_advance_on_failed_derivation(
    client: TestClient,
    migrated_db: str,
) -> None:
    good = _position_opened_event(ts_event=T1)
    bad = _poison_position_event(ts_event=T2)

    assert _post_events(client, [good]).status_code == 200
    watermark = _watermark(migrated_db, projector="positions")
    assert watermark is not None
    assert watermark[0] == good["event_id"]

    response = _post_events(client, [bad])
    assert response.status_code == 200
    assert response.json()["acked_event_ids"] == [bad["event_id"]]

    watermark = _watermark(migrated_db, projector="positions")
    assert watermark is not None
    assert watermark[0] == good["event_id"], (
        "watermark must not advance past a failed derivation"
    )
    assert watermark[1] == T1

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM projection_failures WHERE event_id=%s",
            (bad["event_id"],),
        )
        assert cur.fetchone()[0] == 1


def test_position_hint_success_updates_positions_watermark_and_projection(
    client: TestClient,
    migrated_db: str,
) -> None:
    event = _position_opened_event(ts_event=T1)

    response = _post_events(client, [event])

    assert response.status_code == 200
    assert response.json()["acked_event_ids"] == [event["event_id"]]

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT side::text, quantity, status
            FROM positions_projection
            WHERE account_id=%s AND position_id=%s
            """,
            (ACCOUNT_ID, f"{INSTRUMENT_ID}-LONG"),
        )
        row = cur.fetchone()

    assert row is not None
    side, quantity, status = row
    assert side == "long"
    assert quantity == Decimal("5")
    assert status == "open"

    watermark = _watermark(migrated_db, projector="positions")
    assert watermark is not None
    assert watermark[0] == event["event_id"]
    assert watermark[1] == T1


def test_event_ingest_role_projects_order_event_without_failures(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: str,
) -> None:
    """P0-1 regression (batch 1.1): the production ingest role
    trader_v3_event_ingest must hold the reducer's full table surface
    (order_events SELECT/INSERT, reconciliation_findings/_runs INSERT).
    Before 0018 the role had zero privileges on order_events, so every order
    event failed inside the savepoint under the real deployment role."""
    client_order_id = _client_order_id()
    event = _order_accepted_event(client_order_id, ts_event=T1)

    # The heartbeat above ran on the owner connection; the event push itself
    # must go through a real trader_v3_event_ingest database connection.
    monkeypatch.setenv(
        "DATABASE_URL",
        make_dsn(migrated_db, user="trader_v3_event_ingest"),
    )
    with TestClient(read_api.create_app(AppRole.EVENT_INGEST)) as ingest_client:
        response = ingest_client.post(
            f"/v1/nodes/{NODE_ID}/execution-events",
            headers=_writer_headers(),
            json={"account_id": ACCOUNT_ID, "events": [event]},
        )

    assert response.status_code == 200
    assert response.json()["acked_event_ids"] == [event["event_id"]]

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status FROM orders_projection
            WHERE account_id=%s AND client_order_id=%s
            """,
            (ACCOUNT_ID, client_order_id),
        )
        projection = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM projection_failures")
        failure_count = cur.fetchone()[0]
        cur.execute(
            """
            SELECT order_projection_id FROM order_events
            WHERE event_id=%s
            """,
            (event["event_id"],),
        )
        order_event = cur.fetchone()

    assert projection == ("accepted",), (
        "order event pushed under trader_v3_event_ingest must derive the "
        "projection row (role privilege gap)"
    )
    assert failure_count == 0, (
        "a privilege failure under the production role must not occur"
    )
    assert order_event is not None and order_event[0] is not None


def test_watermark_upsert_is_monotonic(
    migrated_db: str,
) -> None:
    """P1-2 (batch 1.1): a late replay/out-of-order upsert must not move the
    watermark backwards."""
    from repository import ProjectionWriter

    newer = (str(uuid4()), T2)
    older = (str(uuid4()), T1)

    with psycopg2.connect(migrated_db) as conn:
        writer = ProjectionWriter(conn)
        writer.upsert_projection_watermark(
            account_id=ACCOUNT_ID,
            projector="orders",
            event_id=newer[0],
            ts_event=newer[1],
        )
        writer.upsert_projection_watermark(
            account_id=ACCOUNT_ID,
            projector="orders",
            event_id=older[0],
            ts_event=older[1],
        )
        conn.commit()

    watermark = _watermark(migrated_db, projector="orders")
    assert watermark is not None
    assert watermark == (newer[0], newer[1]), (
        "watermark must not regress on an older upsert"
    )


# --- helpers ---------------------------------------------------------------


def _post_events(client: TestClient, events: list[dict]):
    return client.post(
        f"/v1/nodes/{NODE_ID}/execution-events",
        headers=_writer_headers(),
        json={"account_id": ACCOUNT_ID, "events": events},
    )


def _client_order_id() -> str:
    return f"B{uuid4().hex}01"


def _projection_status(database_url: str, client_order_id: str) -> str | None:
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status
            FROM orders_projection
            WHERE account_id=%s AND client_order_id=%s
            """,
            (ACCOUNT_ID, client_order_id),
        )
        row = cur.fetchone()
    return None if row is None else str(row[0])


def _insert_orders_projection(
    database_url: str,
    *,
    client_order_id: str,
    status: str,
    intent_id: str | None = None,
    payload: dict | None = None,
) -> None:
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders_projection (
                order_projection_id, account_id, instrument_id, intent_id,
                client_order_id, status, side, order_type, quantity,
                filled_quantity, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, 'long', 'LIMIT', 5, 0, %s::jsonb)
            """,
            (
                str(uuid4()),
                ACCOUNT_ID,
                INSTRUMENT_ID,
                intent_id,
                client_order_id,
                status,
                json.dumps(payload or {}),
            ),
        )


def _order_submitted_event(
    client_order_id: str,
    *,
    ts_event: datetime,
    intent_id: str | None = None,
) -> dict:
    event = {
        "event_id": str(uuid4()),
        "event_type": "OrderSubmitted",
        "account_id": ACCOUNT_ID,
        "node_id": NODE_ID,
        "client_order_id": client_order_id,
        "ts_event": ts_event.isoformat(),
        "payload": {
            "instrument_id": INSTRUMENT_ID,
            "client_order_id": client_order_id,
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": "5",
            "price": "4.5",
        },
    }
    if intent_id is not None:
        event["intent_id"] = intent_id
    return event


def _order_accepted_event(
    client_order_id: str,
    *,
    ts_event: datetime,
    intent_id: str | None = None,
) -> dict:
    event = {
        "event_id": str(uuid4()),
        "event_type": "OrderAccepted",
        "account_id": ACCOUNT_ID,
        "node_id": NODE_ID,
        "client_order_id": client_order_id,
        "ts_event": ts_event.isoformat(),
        "payload": {
            "instrument_id": INSTRUMENT_ID,
            "client_order_id": client_order_id,
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": "5",
            "price": "4.5",
        },
    }
    if intent_id is not None:
        event["intent_id"] = intent_id
    return event


def _order_denied_event(
    client_order_id: str,
    *,
    ts_event: datetime,
    intent_id: str | None = None,
    lifecycle_role: str | None = None,
    order_type: str | None = None,
) -> dict:
    payload = {
        "instrument_id": INSTRUMENT_ID,
        "client_order_id": client_order_id,
        "reason": "lot-size",
        "source": "intent_rejection",
    }
    if lifecycle_role:
        payload["lifecycle_role"] = lifecycle_role
        payload["tags"] = [f"lifecycle_role={lifecycle_role}"]
    if order_type:
        payload["order_type"] = order_type
    event = {
        "event_id": str(uuid4()),
        "event_type": "OrderDenied",
        "account_id": ACCOUNT_ID,
        "node_id": NODE_ID,
        "client_order_id": client_order_id,
        "ts_event": ts_event.isoformat(),
        "payload": payload,
    }
    if intent_id is not None:
        event["intent_id"] = intent_id
    return event


def _order_filled_event(
    client_order_id: str,
    *,
    ts_event: datetime,
    intent_id: str | None = None,
    quantity: str = "5",
    filled_qty: str = "5",
    leaves_qty: str = "0",
) -> dict:
    event = {
        "event_id": str(uuid4()),
        "event_type": "OrderFilled",
        "account_id": ACCOUNT_ID,
        "node_id": NODE_ID,
        "client_order_id": client_order_id,
        "ts_event": ts_event.isoformat(),
        "payload": {
            "instrument_id": INSTRUMENT_ID,
            "client_order_id": client_order_id,
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": quantity,
            "filled_qty": filled_qty,
            "leaves_qty": leaves_qty,
            "last_qty": filled_qty,
            "avg_px": "4.6",
        },
    }
    if intent_id is not None:
        event["intent_id"] = intent_id
    return event


def _position_opened_event(*, ts_event: datetime) -> dict:
    return {
        "event_id": str(uuid4()),
        "event_type": "PositionOpened",
        "account_id": ACCOUNT_ID,
        "node_id": NODE_ID,
        "ts_event": ts_event.isoformat(),
        "payload": {
            "position": {
                "account_id": ACCOUNT_ID,
                "instrument_id": INSTRUMENT_ID,
                "side": "LONG",
                "quantity": "5",
                "avg_entry_price": "4.2",
            },
        },
    }


def _poison_position_event(*, ts_event: datetime) -> dict:
    """A payload that normalizes into a position hint but fails at the
    projection write (avg_entry_price cannot be adapted to numeric)."""
    return {
        "event_id": str(uuid4()),
        "event_type": "PositionChanged",
        "account_id": ACCOUNT_ID,
        "node_id": NODE_ID,
        "ts_event": ts_event.isoformat(),
        "payload": {
            "position": {
                "account_id": ACCOUNT_ID,
                "instrument_id": INSTRUMENT_ID,
                "side": "LONG",
                "quantity": "3",
                "avg_entry_price": {"malformed": True},
            },
        },
    }


def _watermark(
    database_url: str,
    *,
    projector: str,
) -> tuple[str, datetime] | None:
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT last_event_id, last_event_ts
            FROM projection_watermarks
            WHERE account_id=%s AND projector=%s
            """,
            (ACCOUNT_ID, projector),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return str(row[0]), row[1]


def _reader_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEWER_TOKEN}"}


def _seed_approved_intent(database_url: str) -> str:
    now = datetime.now(timezone.utc)
    raw_id, run_id, context_id, decision_id, risk_id, intent_id = (
        uuid4() for _ in range(6)
    )
    idempotency_key = hashlib.sha256(str(intent_id).encode()).hexdigest()
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_messages (
                id, source, channel_id, source_message_id, source_version,
                source_received_at, content_hash
            )
            VALUES (%s, 'telegram', 'deny-query', %s, 'v1', %s, %s)
            """,
            (
                str(raw_id),
                f"deny-query-{raw_id}",
                now,
                hashlib.sha256(str(raw_id).encode()).hexdigest(),
            ),
        )
        cur.execute(
            """
            INSERT INTO message_processing_runs (
                processing_run_id, raw_message_id, status
            )
            VALUES (%s, %s, 'succeeded')
            """,
            (str(run_id), str(raw_id)),
        )
        cur.execute(
            """
            INSERT INTO context_snapshots (
                context_snapshot_id, raw_message_id, snapshot_type,
                context_version, snapshot
            )
            VALUES (%s, %s, 'system', 'v1', '{}'::jsonb)
            """,
            (str(context_id), str(raw_id)),
        )
        cur.execute(
            """
            INSERT INTO hermes_decisions (
                decision_id, raw_message_id, processing_run_id,
                context_snapshot_id, message_type, action, ambiguous,
                account_scope, entry_type, model_version, prompt_version,
                context_version, temperature, created_at
            )
            VALUES (
                %s, %s, %s, %s,
                'new_signal', 'open_position', false, 'single', 'limit',
                'deny-query', 'deny-query', 'v1', 0, %s
            )
            """,
            (
                str(decision_id),
                str(raw_id),
                str(run_id),
                str(context_id),
                now,
            ),
        )
        cur.execute(
            """
            INSERT INTO risk_decisions (
                risk_decision_id, hermes_decision_id, status, account_id,
                instrument_id, decided_by
            )
            VALUES (%s, %s, 'approved', %s, %s, 'deny-query')
            """,
            (str(risk_id), str(decision_id), ACCOUNT_ID, INSTRUMENT_ID),
        )
        cur.execute(
            """
            INSERT INTO trade_intents (
                intent_id, hermes_decision_id, risk_decision_id, account_id,
                instrument_id, action, status, order_plan, risk_budget,
                valid_until, idempotency_key, approved_at
            )
            VALUES (
                %s, %s, %s, %s, %s, 'open_position', 'approved',
                '{}'::jsonb, '{}'::jsonb, %s, %s, %s
            )
            """,
            (
                str(intent_id),
                str(decision_id),
                str(risk_id),
                ACCOUNT_ID,
                INSTRUMENT_ID,
                now + timedelta(days=1),
                idempotency_key,
                now,
            ),
        )
        conn.commit()
    return str(intent_id)


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NODE_TOKEN}",
        "X-Node-Id": NODE_ID,
        "X-Account-Id": ACCOUNT_ID,
    }


def _writer_headers() -> dict[str, str]:
    return {
        **_auth_headers(),
        "X-Redis-Fencing-Epoch": REDIS_FENCING_EPOCH,
        "X-Runtime-Generation": RUNTIME_GENERATION,
        "X-Lease-Fencing-Token": str(LEASE_FENCING_TOKEN),
    }


def _activate_redis_epoch(database_url: str) -> None:
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO redis_fencing_epochs (
                redis_fencing_epoch,
                domain,
                status,
                marker_sha256,
                capacity_evidence_sha256,
                initial_redis_run_id,
                active_volume,
                activated_by,
                activated_at
            )
            VALUES (
                %s, 'trader-v3', 'active', %s, %s, %s, %s,
                'test', now()
            )
            """,
            (
                REDIS_FENCING_EPOCH,
                "5" * 64,
                "6" * 64,
                "c" * 40,
                "redis-volume-projection-reliability",
            ),
        )
