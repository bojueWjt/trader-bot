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

import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient

import read_api


ACCOUNT_ID = "account-a"
NODE_ID = "nautilus-node-account-a"
NODE_TOKEN = "node-a-token"
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


# --- helpers ---------------------------------------------------------------


def _post_events(client: TestClient, events: list[dict]):
    return client.post(
        f"/v1/nodes/{NODE_ID}/execution-events",
        headers=_writer_headers(),
        json={"account_id": ACCOUNT_ID, "events": events},
    )


def _client_order_id() -> str:
    return f"B{uuid4().hex}01"


def _order_accepted_event(client_order_id: str, *, ts_event: datetime) -> dict:
    return {
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


def _order_filled_event(client_order_id: str, *, ts_event: datetime) -> dict:
    return {
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
            "quantity": "5",
            "filled_qty": "5",
            "leaves_qty": "0",
            "last_qty": "5",
            "avg_px": "4.6",
        },
    }


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
