"""Operator add_position + capital reservation regressions.

Covers control-plane acceptance of add_position and Codex occupancy / venue /
replay-chain rules. Does not touch nautilus-node.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import Json

import read_api


ACCOUNT_B = "account-b"
ACCOUNT_A = "account-a"
NODE_B = "nautilus-node-account-b"
NODE_A = "nautilus-node-account-a"
SYMBOL = "ATOMUSDT"
RISK_TOKEN = "risk-token"
RELEASE_ID = "release-add-position"
IMAGE_DIGEST = "sha256:" + ("1" * 64)
CONFIG_SHA256 = "2" * 64
LOCK_SHA256 = "3" * 64
SCHEMA_EPOCH = "2026-08-08"
RUNTIME_GENERATION = "runtime-generation-add"
REDIS_FENCING_EPOCH = "11111111-1111-4111-8111-111111111111"
ENTRY_PRICE = 1.55
STOP_LOSS = 1.50


def _connect(url: str):
    return psycopg2.connect(url)


def _activate_redis_epoch(url: str) -> None:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE redis_fencing_epochs
            SET status='retired', retired_at=now()
            WHERE domain='trader-v3' AND status='active'
            """
        )
        cur.execute(
            """
            INSERT INTO redis_fencing_epochs (
                redis_fencing_epoch, domain, status, marker_sha256,
                capacity_evidence_sha256, initial_redis_run_id,
                active_volume, activated_by, activated_at
            )
            VALUES (%s, 'trader-v3', 'active', %s, %s, %s, %s, 'test', now())
            """,
            (
                REDIS_FENCING_EPOCH,
                "1" * 64,
                "2" * 64,
                "a" * 40,
                f"redis-volume-{REDIS_FENCING_EPOCH}",
            ),
        )


def _seed_reviewed_rollout(url: str, *, phase: str = "account_a_canary") -> None:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reviewed_release_rollouts (
                release_id, redis_fencing_epoch, image_digest,
                config_sha256, dependency_lock_sha256, schema_epoch,
                manifest_sha256, bundle_manifest_sha256,
                registration_idempotency_key, reviewed_by, phase
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'reviewer', %s)
            ON CONFLICT (release_id) DO UPDATE SET phase=EXCLUDED.phase
            """,
            (
                RELEASE_ID,
                REDIS_FENCING_EPOCH,
                IMAGE_DIGEST,
                CONFIG_SHA256,
                LOCK_SHA256,
                SCHEMA_EPOCH,
                "5" * 64,
                "6" * 64,
                "register-add-position",
                phase,
            ),
        )


def _seed_account(
    url: str,
    *,
    account_id: str,
    node_id: str,
    equity: float = 100,
    available_balance: float = 100,
    positions: list | None = None,
    reconciliation_state: str = "healthy",
    heartbeat_age_seconds: int = 0,
) -> None:
    now = datetime.now(timezone.utc) - timedelta(seconds=heartbeat_age_seconds)
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO node_heartbeats (
                node_id, account_id, status, version, payload,
                release_id, image_digest, config_sha256,
                dependency_lock_sha256, schema_epoch,
                positions, regular_orders, algo_orders,
                positions_snapshot_at, regular_orders_snapshot_at,
                algo_orders_snapshot_at, reconciliation_completed_at,
                redis_fencing_epoch, runtime_generation,
                lease_fencing_token, heartbeat_sequence, last_seen_at
            )
            VALUES (
                %s, %s, 'ACTIVE', %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, 41, 1, %s
            )
            ON CONFLICT (node_id) DO UPDATE SET
                payload=EXCLUDED.payload,
                positions=EXCLUDED.positions,
                positions_snapshot_at=EXCLUDED.positions_snapshot_at,
                last_seen_at=EXCLUDED.last_seen_at
            """,
            (
                node_id,
                account_id,
                RELEASE_ID,
                Json(
                    {
                        "readiness": True,
                        "projection_lag_ms": 0,
                        "reconciliation_state": reconciliation_state,
                        "health_degraded_reasons": [],
                        "ts": now.isoformat(),
                    }
                ),
                RELEASE_ID,
                IMAGE_DIGEST,
                CONFIG_SHA256,
                LOCK_SHA256,
                SCHEMA_EPOCH,
                Json(positions if positions is not None else []),
                Json([]),
                Json([]),
                now,
                now,
                now,
                now,
                REDIS_FENCING_EPOCH,
                RUNTIME_GENERATION,
                now,
            ),
        )
        cur.execute(
            """
            INSERT INTO accounts_projection (
                account_id, currency, equity, margin,
                available_balance, updated_at, payload
            )
            VALUES (%s, 'USDT', %s, 0, %s, %s, %s)
            ON CONFLICT (account_id) DO UPDATE SET
                equity=EXCLUDED.equity,
                available_balance=EXCLUDED.available_balance,
                updated_at=EXCLUDED.updated_at,
                payload=EXCLUDED.payload
            """,
            (
                account_id,
                equity,
                available_balance,
                now,
                Json(
                    {
                        "account_snapshot_source": "binance_fapi_account_v3",
                        "account_snapshot_fetched_at": now.isoformat(),
                        "exchange_account": {
                            "currency": "USDT",
                            "equity": equity,
                            "margin": 0,
                            "free": available_balance,
                        },
                    }
                ),
            ),
        )


def _seed_mirror(
    url: str,
    *,
    account_id: str,
    positions: list,
    age_seconds: float = 1.0,
    payload: dict | None = None,
) -> None:
    updated = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    body = payload if payload is not None else {"positions": positions}
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO exchange_state_mirror (account_id, payload, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (account_id) DO UPDATE SET
                payload=EXCLUDED.payload,
                updated_at=EXCLUDED.updated_at
            """,
            (account_id, Json(body), updated),
        )


def _long_position(quantity: str = "1") -> dict:
    return {
        "symbol": SYMBOL,
        "position_side": "LONG",
        "position_amt": quantity,
        "quantity": quantity,
        "entry_price": str(ENTRY_PRICE),
        "mark_price": str(ENTRY_PRICE),
    }


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, migrated_db: str) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("RISK_ADMIN_TOKEN", RISK_TOKEN)
    monkeypatch.setenv("OPERATOR_MAX_LEVERAGE", "10")
    monkeypatch.setattr(read_api, "_account_risk_capital_addon", lambda _account_id: 0.0)
    monkeypatch.setattr(read_api, "_symbol_risk_ratio", lambda _symbol: 0.06)
    _activate_redis_epoch(migrated_db)
    _seed_reviewed_rollout(migrated_db)
    _seed_account(migrated_db, account_id=ACCOUNT_B, node_id=NODE_B)
    test_client = TestClient(read_api.app)
    try:
        yield test_client
    finally:
        test_client.close()


def _headers(request_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {RISK_TOKEN}",
        "X-Request-Id": request_id,
    }


def _entry_body(
    action: str,
    client_ref: str,
    *,
    account_id: str = ACCOUNT_B,
    notional: float = 12,
    second_price: float | None = None,
    replay_of: str | None = None,
    intended_action: str | None = None,
    canary_permit_id: str | None = None,
) -> dict:
    entry = {"type": "limit", "price": ENTRY_PRICE, "time_in_force": "GTC"}
    if second_price is not None:
        entry["second_price"] = second_price
    body = {
        "action": action,
        "account_id": account_id,
        "symbol": SYMBOL,
        "side": "long",
        "entry": entry,
        "notional_usdt": notional,
        "stop_loss": STOP_LOSS,
        "reason": "add-position regression",
        "client_ref": client_ref,
        "protection_policy": "stop_only",
    }
    if replay_of:
        body["replay_of"] = replay_of
    if intended_action:
        body["intended_action"] = intended_action
    if canary_permit_id:
        body["canary_permit_id"] = canary_permit_id
    return body


def _post(client: TestClient, body: dict):
    return client.post(
        "/v1/operator/orders",
        headers=_headers(body.get("client_ref") or str(uuid4())),
        json=body,
    )


def _same_side_books(url: str, quantity: str = "1") -> None:
    pos = _long_position(quantity)
    _seed_account(
        url,
        account_id=ACCOUNT_B,
        node_id=NODE_B,
        positions=[pos],
    )
    _seed_mirror(url, account_id=ACCOUNT_B, positions=[pos])


def test_add_is_accepted_and_keeps_intended_action(
    client: TestClient,
    migrated_db: str,
) -> None:
    _same_side_books(migrated_db)
    response = _post(
        client,
        _entry_body(
            "add_position",
            "add-accepted",
            intended_action="add_position",
        ),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["action"] == "add_position"
    assert payload["intended_action"] == "add_position"
    assert payload["order_plan"]["intended_action"] == "add_position"
    assert payload["order_plan"]["live_open_gate"]["mode"] == "normal"
    assert payload["risk_budget"]["max_notional"] == 12


def test_add_without_client_ref_is_400(client: TestClient, migrated_db: str) -> None:
    _same_side_books(migrated_db)
    body = _entry_body("add_position", "unused")
    body.pop("client_ref")
    response = client.post(
        "/v1/operator/orders",
        headers=_headers("missing-ref"),
        json=body,
    )
    assert response.status_code == 400
    assert "client_ref" in response.json()["detail"]


def test_second_price_with_add_is_400(client: TestClient, migrated_db: str) -> None:
    _same_side_books(migrated_db)
    response = _post(
        client,
        _entry_body("add_position", "add-second-price", second_price=1.60),
    )
    assert response.status_code == 400
    assert "second_price" in response.json()["detail"]


def test_open_is_not_rewritten_to_add(client: TestClient, migrated_db: str) -> None:
    _same_side_books(migrated_db)
    response = _post(client, _entry_body("open_position", "keep-open"))
    assert response.status_code == 200, response.text
    assert response.json()["action"] == "open_position"


def test_account_a_add_without_permit_is_canary_denied(
    client: TestClient,
    migrated_db: str,
) -> None:
    pos = _long_position()
    _seed_account(migrated_db, account_id=ACCOUNT_A, node_id=NODE_A, positions=[pos])
    _seed_mirror(migrated_db, account_id=ACCOUNT_A, positions=[pos])
    response = _post(
        client,
        _entry_body("add_position", "canary-add", account_id=ACCOUNT_A),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "canary_open_position_only"


def test_same_source_identity_cannot_switch_open_to_add(
    client: TestClient,
    migrated_db: str,
) -> None:
    _same_side_books(migrated_db)
    first = _post(client, _entry_body("open_position", "same-source"))
    assert first.status_code == 200, first.text
    second = _post(client, _entry_body("add_position", "same-source"))
    assert second.status_code == 409
    assert "payload mismatch" in second.json()["detail"]


def test_replay_of_rejected_zero_execution_allows_add(
    client: TestClient,
    migrated_db: str,
) -> None:
    _same_side_books(migrated_db)
    first = _post(client, _entry_body("open_position", "replay-source"))
    assert first.status_code == 200, first.text
    old_id = first.json()["intent_id"]
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE trade_intents SET status='rejected' WHERE intent_id=%s",
            (old_id,),
        )
    response = _post(
        client,
        _entry_body(
            "add_position",
            "replay-source",
            replay_of=old_id,
            intended_action="add_position",
        ),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["replay"] is False
    assert payload["intent_id"] != old_id
    assert payload["action"] == "add_position"
    assert payload["order_plan"]["replay_of"] == old_id


def test_replay_of_non_entry_action_is_rejected(
    client: TestClient,
    migrated_db: str,
) -> None:
    _same_side_books(migrated_db)
    first = _post(client, _entry_body("open_position", "replay-manage"))
    old_id = first.json()["intent_id"]
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE trade_intents SET status='rejected', action='close_position' "
            "WHERE intent_id=%s",
            (old_id,),
        )
    response = _post(
        client,
        _entry_body("add_position", "replay-manage", replay_of=old_id),
    )
    assert response.status_code == 400
    assert "entry" in response.json()["detail"]


def test_replay_of_old_rejected_parent_returns_live_chain_intent(
    client: TestClient,
    migrated_db: str,
) -> None:
    _same_side_books(migrated_db)
    first = _post(client, _entry_body("open_position", "chain-source"))
    a_id = first.json()["intent_id"]
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE trade_intents SET status='rejected' WHERE intent_id=%s",
            (a_id,),
        )
    second = _post(
        client,
        _entry_body("add_position", "chain-source", replay_of=a_id),
    )
    assert second.status_code == 200, second.text
    b_id = second.json()["intent_id"]
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE trade_intents SET status='rejected' WHERE intent_id=%s",
            (b_id,),
        )
    third = _post(
        client,
        _entry_body("add_position", "chain-source", replay_of=b_id),
    )
    assert third.status_code == 200, third.text
    c_id = third.json()["intent_id"]
    retry_old_parent = _post(
        client,
        _entry_body("add_position", "chain-source", replay_of=a_id),
    )
    assert retry_old_parent.status_code == 200, retry_old_parent.text
    assert retry_old_parent.json()["replay"] is True
    assert retry_old_parent.json()["intent_id"] == c_id


def test_filled_approved_intent_is_not_off_venue_reservation(
    client: TestClient,
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATOR_MAX_LEVERAGE", "1")
    first = _post(
        client,
        _entry_body("open_position", "filled-first", notional=60),
    )
    assert first.status_code == 200, first.text
    intent_id = first.json()["intent_id"]
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders_projection (
                order_projection_id, account_id, instrument_id, intent_id,
                client_order_id, status, side, order_type, quantity,
                filled_quantity, price
            ) VALUES (%s, %s, %s, %s, %s, 'filled', 'long', 'LIMIT', 38.7, 38.7, %s)
            """,
            (
                str(uuid4()),
                ACCOUNT_B,
                SYMBOL,
                intent_id,
                "B" + "a" * 32 + "01",
                ENTRY_PRICE,
            ),
        )
    second = _post(
        client,
        _entry_body("open_position", "filled-second", notional=60),
    )
    assert second.status_code == 200, second.text


def test_off_venue_approved_intents_consume_available_reservation(
    client: TestClient,
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATOR_MAX_LEVERAGE", "1")
    first = _post(
        client,
        _entry_body("open_position", "off-venue-1", notional=60),
    )
    assert first.status_code == 200, first.text
    second = _post(
        client,
        _entry_body("open_position", "off-venue-2", notional=60),
    )
    assert second.status_code == 400
    assert "off-venue reservation" in second.json()["detail"]


def test_working_gtc_is_not_double_counted_against_available(
    client: TestClient,
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATOR_MAX_LEVERAGE", "1")
    first = _post(
        client,
        _entry_body("open_position", "working-gtc", notional=60),
    )
    assert first.status_code == 200, first.text
    intent_id = first.json()["intent_id"]
    accept_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    snapshot_at = datetime.now(timezone.utc)
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders_projection (
                order_projection_id, account_id, instrument_id, intent_id,
                client_order_id, status, side, order_type, quantity,
                filled_quantity, price, ts_event
            ) VALUES (%s, %s, %s, %s, %s, 'accepted', 'long', 'LIMIT', 38.7, 0, %s, %s)
            """,
            (
                str(uuid4()),
                ACCOUNT_B,
                SYMBOL,
                intent_id,
                "B" + "b" * 32 + "01",
                ENTRY_PRICE,
                accept_at,
            ),
        )
        cur.execute(
            "UPDATE trade_intents SET valid_until = now() - interval '1 hour' "
            "WHERE intent_id=%s",
            (intent_id,),
        )
        cur.execute(
            """
            UPDATE accounts_projection
            SET available_balance=40,
                updated_at=%s,
                payload=%s
            WHERE account_id=%s
            """,
            (
                snapshot_at,
                Json(
                    {
                        "account_snapshot_source": "binance_fapi_account_v3",
                        "account_snapshot_fetched_at": snapshot_at.isoformat(),
                        "exchange_account": {
                            "currency": "USDT",
                            "equity": 100,
                            "margin": 0,
                            "free": 40,
                        },
                    }
                ),
                ACCOUNT_B,
            ),
        )
    second = _post(
        client,
        _entry_body("open_position", "working-gtc-new", notional=12),
    )
    assert second.status_code == 200, second.text


def test_historical_open_projection_does_not_block_fresh_flat_entry(client, migrated_db):
    _seed_account(migrated_db, account_id=ACCOUNT_B, node_id=NODE_B, positions=[])
    _seed_mirror(migrated_db, account_id=ACCOUNT_B, positions=[])
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO positions_projection
                (account_id, position_id, instrument_id, side, quantity, avg_entry_price, status)
            VALUES (%s, 'historical-long', 'ATOMUSDT-PERP.BINANCE', 'long', 100, 1.55, 'open')
        """, (ACCOUNT_B,))
    response = _post(client, _entry_body("open_position", "historical-flat"))
    assert response.status_code == 200, response.text
    # Admission does not fabricate PositionClosed events or rewrite accounting.
    with _connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM positions_projection WHERE account_id=%s AND position_id='historical-long'", (ACCOUNT_B,))
        assert cur.fetchone() == ("open",)


def test_fresh_heartbeat_conflict_is_not_covered_by_mirror(
    client: TestClient,
    migrated_db: str,
) -> None:
    pos = _long_position()
    _seed_account(
        migrated_db,
        account_id=ACCOUNT_B,
        node_id=NODE_B,
        positions=[pos],
        reconciliation_state="failed",
    )
    _seed_mirror(migrated_db, account_id=ACCOUNT_B, positions=[pos])
    response = _post(client, _entry_body("open_position", "conflict-mirror"))
    assert response.status_code == 409
    assert "conflict" in response.json()["detail"]


def test_same_side_quantity_conflict_is_rejected(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_account(
        migrated_db,
        account_id=ACCOUNT_B,
        node_id=NODE_B,
        positions=[_long_position("1")],
    )
    _seed_mirror(
        migrated_db,
        account_id=ACCOUNT_B,
        positions=[_long_position("2")],
    )
    response = _post(client, _entry_body("open_position", "qty-conflict"))
    assert response.status_code == 409
    assert "conflict" in response.json()["detail"]


def test_malformed_positions_are_not_known_flat(
    client: TestClient,
    migrated_db: str,
) -> None:
    _seed_account(migrated_db, account_id=ACCOUNT_B, node_id=NODE_B, positions=[])
    _seed_mirror(
        migrated_db,
        account_id=ACCOUNT_B,
        positions=[],
        payload={"positions": "not-a-list"},
    )
    response = _post(client, _entry_body("open_position", "malformed-flat"))
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "unknown" in detail or "conflict" in detail


def test_account_risk_increase_lock_matches_reservations_and_gateway() -> None:
    import inspect
    from pathlib import Path

    source = inspect.getsource(read_api._account_risk_increase_lock)
    assert "hashtext(%s), 0" in source
    gateway = (
        Path(__file__).resolve().parents[3]
        / "services"
        / "control-plane"
        / "decision_gateway"
        / "gateway.py"
    ).read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock(hashtext(%s), 0)" in gateway
