"""Independent review of entry capital reservation (read_api occupancy).

Correct-behavior assertions. Do not patch production code to greenwash.
Reuses helpers from test_operator_add_position.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg2.extras import Json

import read_api
from test_operator_add_position import (
    ACCOUNT_B,
    NODE_B,
    RISK_TOKEN,
    SYMBOL,
    _activate_redis_epoch,
    _connect,
    _entry_body,
    _post,
    _seed_account,
    _seed_reviewed_rollout,
)


BUDGET = 60
NEW = 50
QTY = Decimal("10")
PRICE = Decimal("6")  # QTY * PRICE == BUDGET


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, migrated_db: str) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("RISK_ADMIN_TOKEN", RISK_TOKEN)
    monkeypatch.setenv("OPERATOR_MAX_LEVERAGE", "1")
    monkeypatch.setattr(read_api, "_account_risk_capital_addon", lambda _account_id: 0.0)
    monkeypatch.setattr(read_api, "_symbol_risk_ratio", lambda _symbol, _account: 0.06)
    _activate_redis_epoch(migrated_db)
    _seed_reviewed_rollout(migrated_db)
    _seed_account(migrated_db, account_id=ACCOUNT_B, node_id=NODE_B)
    test_client = TestClient(read_api.app)
    try:
        yield test_client
    finally:
        test_client.close()


def _occupancy(url: str) -> dict[str, Decimal]:
    with _connect(url) as conn, conn.cursor() as cur:
        return read_api._entry_intent_occupancy(cur, ACCOUNT_B)


def _reserve(url: str, new_notional: float, *, venue_notional: float = 0.0):
    with _connect(url) as conn, conn.cursor() as cur:
        checks: list = []
        try:
            read_api._assert_entry_capital_reservation(
                cur,
                account_id=ACCOUNT_B,
                new_notional=new_notional,
                leverage=1,
                caps={"max_leverage": 1},
                venue={"state": "known", "notional": venue_notional},
                checks=checks,
            )
        except HTTPException as exc:
            return exc, checks
        return None, checks


def _approve(client: TestClient, client_ref: str, notional: float = BUDGET) -> str:
    response = _post(client, _entry_body("open_position", client_ref, notional=notional))
    assert response.status_code == 200, response.text
    return response.json()["intent_id"]


def _intent_trace(url: str, intent_id: str) -> dict:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT valid_until < now(), valid_until FROM trade_intents WHERE intent_id=%s",
            (intent_id,),
        )
        expired, valid_until = cur.fetchone()
        cur.execute("SELECT count(*) FROM orders_projection WHERE intent_id=%s", (intent_id,))
        orders = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM execution_events WHERE intent_id=%s", (intent_id,))
        events = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM execution_commands WHERE intent_id=%s", (intent_id,))
        commands = cur.fetchone()[0]
    return {
        "expired": expired,
        "valid_until": valid_until,
        "orders": orders,
        "events": events,
        "commands": commands,
    }


def _expire(url: str, intent_id: str) -> None:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE trade_intents SET valid_until = now() - interval '1 hour' "
            "WHERE intent_id=%s",
            (intent_id,),
        )


def _insert_order(
    url: str,
    intent_id: str,
    status: str,
    *,
    ts_event: datetime | None = None,
    venue_order_id: str | None = None,
    quantity: Decimal = QTY,
    filled_quantity: Decimal = Decimal("0"),
    price: Decimal = PRICE,
    sequence: int = 1,
) -> datetime:
    event_ts = ts_event or datetime.now(timezone.utc)
    client_order_id = f"B{UUID(str(intent_id)).hex}{sequence:02d}"
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders_projection (
                order_projection_id, account_id, instrument_id, intent_id,
                client_order_id, venue_order_id, status, side, order_type,
                quantity, filled_quantity, price, ts_event, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'long', 'LIMIT', %s, %s, %s, %s, %s)
            """,
            (
                str(uuid4()),
                ACCOUNT_B,
                SYMBOL,
                intent_id,
                client_order_id,
                venue_order_id,
                status,
                quantity,
                filled_quantity,
                price,
                event_ts,
                Json({"seq": sequence}),
            ),
        )
    return event_ts


def _set_order_plan(url: str, intent_id: str, order_plan: dict) -> None:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE trade_intents SET order_plan=%s WHERE intent_id=%s",
            (Json(order_plan), intent_id),
        )


def _reject(url: str, intent_id: str) -> None:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE trade_intents SET status='rejected' WHERE intent_id=%s",
            (intent_id,),
        )


def _set_account(
    url: str,
    *,
    equity: float,
    available: float,
    updated_at: datetime,
) -> None:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE accounts_projection
            SET equity=%s,
                available_balance=%s,
                updated_at=%s,
                payload=%s
            WHERE account_id=%s
            """,
            (
                equity,
                available,
                updated_at,
                Json(
                    {
                        "account_snapshot_source": "binance_fapi_account_v3",
                        "account_snapshot_fetched_at": updated_at.isoformat(),
                        "exchange_account": {
                            "currency": "USDT",
                            "equity": equity,
                            "margin": 0,
                            "free": available,
                        },
                    }
                ),
                ACCOUNT_B,
            ),
        )


def test_expired_never_placed_approved_intent_must_not_occupy(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _approve(client, "expired-never-placed")
    _expire(migrated_db, intent_id)
    trace = _intent_trace(migrated_db, intent_id)
    assert trace["expired"] is True
    assert trace["orders"] == 0
    assert trace["events"] == 0
    assert trace["commands"] == 0

    occ = _occupancy(migrated_db)
    assert occ["off_venue"] == Decimal("0"), occ
    assert occ["venue_working"] == Decimal("0"), occ
    exc, _checks = _reserve(migrated_db, BUDGET)
    assert exc is None, getattr(exc, "detail", exc)


def test_expired_gtc_accepted_still_counts_unfilled(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _approve(client, "expired-gtc-working")
    _insert_order(migrated_db, intent_id, "accepted", venue_order_id="v-gtc-1")
    _expire(migrated_db, intent_id)
    occ = _occupancy(migrated_db)
    assert occ["venue_working"] == QTY * PRICE, occ
    assert occ["off_venue"] == Decimal("0"), occ


@pytest.mark.parametrize("status", ["initialized", "submitted"])
def test_pre_venue_order_must_occupy_available_not_venue_working(
    client: TestClient,
    migrated_db: str,
    status: str,
) -> None:
    _seed_account(
        migrated_db,
        account_id=ACCOUNT_B,
        node_id=NODE_B,
        equity=1000,
        available_balance=100,
    )
    intent_id = _approve(client, f"pre-venue-{status}")
    _insert_order(migrated_db, intent_id, status, venue_order_id=None)
    occ = _occupancy(migrated_db)
    assert occ["off_venue"] == Decimal(str(BUDGET)), occ
    assert occ["venue_working"] == Decimal("0"), occ
    exc, _checks = _reserve(migrated_db, NEW)
    assert exc is not None
    assert exc.status_code == 400
    assert "available_balance*leverage" in str(exc.detail)


def test_accepted_order_stale_account_snapshot_cannot_assume_margin(
    client: TestClient,
    migrated_db: str,
) -> None:
    now = datetime.now(timezone.utc)
    snapshot_at = now - timedelta(seconds=20)
    _seed_account(
        migrated_db,
        account_id=ACCOUNT_B,
        node_id=NODE_B,
        equity=1000,
        available_balance=100,
    )
    intent_id = _approve(client, "accepted-stale-snapshot")
    _insert_order(
        migrated_db,
        intent_id,
        "accepted",
        ts_event=now,
        venue_order_id="v-accepted-1",
    )
    _set_account(migrated_db, equity=1000, available=100, updated_at=snapshot_at)
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT updated_at, "
            "(payload->>'account_snapshot_fetched_at')::timestamptz "
            "FROM accounts_projection WHERE account_id=%s",
            (ACCOUNT_B,),
        )
        acct_updated, fetched_at = cur.fetchone()
        cur.execute(
            "SELECT ts_event FROM orders_projection WHERE intent_id=%s",
            (intent_id,),
        )
        (ts_event,) = cur.fetchone()
    assert fetched_at < ts_event
    assert acct_updated < ts_event

    occ = _occupancy(migrated_db)
    exc, _checks = _reserve(migrated_db, NEW)
    # Snapshot predates accept: remaining 60U is not in available yet, so
    # NEW=50 must fail the available gate (equity=1000 would still pass).
    assert occ["off_venue"] + occ["venue_working"] == QTY * PRICE, occ
    assert exc is not None, {"occupancy": occ, "allowed": True}
    assert exc.status_code == 400
    assert "available_balance*leverage" in str(exc.detail)


def test_concurrent_approvals_cannot_double_spend_same_available(
    client: TestClient,
    migrated_db: str,
) -> None:
    del client
    barrier = threading.Barrier(2)

    def submit(client_ref: str):
        barrier.wait(timeout=10)
        with TestClient(read_api.app) as worker:
            return worker.post(
                "/v1/operator/orders",
                headers={
                    "Authorization": f"Bearer {RISK_TOKEN}",
                    "X-Request-Id": client_ref,
                },
                json=_entry_body("open_position", client_ref, notional=BUDGET),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit, ("concurrent-a", "concurrent-b")))

    codes = sorted(response.status_code for response in responses)
    assert codes == [200, 400], [(r.status_code, r.text) for r in responses]
    rejected = next(r for r in responses if r.status_code == 400)
    assert "off-venue reservation" in rejected.json()["detail"]
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM trade_intents "
            "WHERE account_id=%s AND status='approved'",
            (ACCOUNT_B,),
        )
        assert cur.fetchone()[0] == 1


def test_rejected_parent_accepted_working_leg_still_occupies(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _approve(client, "rejected-parent-working")
    _insert_order(migrated_db, intent_id, "accepted", venue_order_id="v-rej-1")
    _reject(migrated_db, intent_id)
    occ = _occupancy(migrated_db)
    assert occ["venue_working"] == QTY * PRICE, occ
    assert occ["off_venue"] == Decimal("0"), occ
    exc, _checks = _reserve(migrated_db, NEW)
    assert exc is not None
    assert exc.status_code == 400
    assert "working" in str(exc.detail) or "occupancy" in str(exc.detail)


def test_rejected_parent_does_not_drop_ungenerated_when_lost(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _approve(client, "rejected-parent-lost")
    _insert_order(migrated_db, intent_id, "lost")
    _reject(migrated_db, intent_id)
    occupancy = _occupancy(migrated_db)
    assert occupancy["off_venue"] == Decimal(str(BUDGET))
    assert occupancy["available_hold"] == Decimal(str(BUDGET))


def test_approved_partial_ladder_holds_unsent_planned_leg(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _approve(client, "partial-ladder")
    _set_order_plan(
        migrated_db,
        intent_id,
        {
            "type": "entry_batch",
            "tranches": [
                {"seq": 1, "quantity": "5", "price": 6},
                {"seq": 2, "quantity": "5", "price": 6},
            ],
        },
    )
    _insert_order(
        migrated_db,
        intent_id,
        "filled",
        quantity=Decimal("5"),
        filled_quantity=Decimal("5"),
        sequence=1,
    )
    occ = _occupancy(migrated_db)
    assert occ["off_venue"] == Decimal("30"), occ
    assert occ["venue_working"] == Decimal("0"), occ
    exc, _checks = _reserve(migrated_db, 80)
    assert exc is not None
    assert exc.status_code == 400


def test_completed_single_entry_does_not_reserve_unused_budget(
    client: TestClient,
    migrated_db: str,
) -> None:
    intent_id = _approve(client, "lot-size-complete", notional=70)
    _set_order_plan(
        migrated_db,
        intent_id,
        {"type": "limit", "quantity": "10", "price": 6},
    )
    _insert_order(
        migrated_db,
        intent_id,
        "filled",
        quantity=Decimal("10"),
        filled_quantity=Decimal("10"),
        sequence=1,
    )
    occ = _occupancy(migrated_db)
    assert occ["off_venue"] == Decimal("0"), occ
    assert occ["venue_working"] == Decimal("0"), occ
    exc, _checks = _reserve(migrated_db, 50)
    assert exc is None, getattr(exc, "detail", exc)


@pytest.mark.parametrize("canonical", [False, True])
def test_fresh_venue_ladder_overrides_thin_projection_without_canceling(
    client: TestClient, migrated_db: str, canonical: bool,
) -> None:
    intent = _approve(client, "venue-three-legs")
    _set_order_plan(migrated_db, intent, {"entry": {"type": "zone"}})
    _insert_order(migrated_db, intent, "accepted", quantity=None, price=None)
    now = datetime.now(timezone.utc)
    working = [{
        "clientOrderId": f"B{UUID(intent).hex}{seq:02d}", "symbol": "PAXGUSDT",
        "type": "LIMIT", "status": "NEW", "origQty": "10", "executedQty": "2",
        "price": "6", "reduceOnly": False,
    } for seq in (1, 2, 3)]
    working.append({"clientOrderId": f"B{UUID(intent).hex}10", "status": "NEW"})
    working.append({"clientOrderId": "aos_user_manual", "status": "NEW", "origQty": "10000", "price": "6"})
    if canonical:
        renames = {"clientOrderId": "client_order_id", "origQty": "quantity",
                   "executedQty": "filled_quantity", "reduceOnly": "reduce_only"}
        working = [{renames.get(key, key): value for key, value in order.items()}
                   for order in working]
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO exchange_state_mirror (account_id, payload, updated_at) VALUES (%s,%s,%s) "
                    "ON CONFLICT(account_id) DO UPDATE SET payload=EXCLUDED.payload, updated_at=EXCLUDED.updated_at",
                    (ACCOUNT_B, Json({"open_orders": working, "fetched_at": now.isoformat()}),
                     now + timedelta(milliseconds=100)))
    _set_account(migrated_db, equity=1000, available=800, updated_at=now)
    assert _occupancy(migrated_db) == {
        "venue_working": Decimal("144"), "off_venue": Decimal("0"), "available_hold": Decimal("0"),
    }
    error, _checks = _reserve(migrated_db, 50)
    assert error is None
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT payload->'open_orders' FROM exchange_state_mirror WHERE account_id=%s", (ACCOUNT_B,))
        assert cur.fetchone()[0] == working
        cur.execute("SELECT count(*) FROM execution_commands WHERE intent_id=%s", (intent,))
        assert cur.fetchone()[0] == 0


def test_unknown_old_intent_becomes_funding_rejection_not_account_409(
    client: TestClient, migrated_db: str,
) -> None:
    intent = _approve(client, "unknown-old-entry")
    _insert_order(migrated_db, intent, "lost", quantity=None, price=None)
    _set_account(migrated_db, equity=1000, available=100, updated_at=datetime.now(timezone.utc))
    error, _checks = _reserve(migrated_db, 50)
    assert error is not None and error.status_code == 400
    _set_account(migrated_db, equity=1000, available=200, updated_at=datetime.now(timezone.utc))
    error, _checks = _reserve(migrated_db, 50)
    assert error is None
