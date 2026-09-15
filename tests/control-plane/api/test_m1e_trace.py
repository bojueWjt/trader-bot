"""B-05 / M1e: GET /v1/intents/{intent_id}/trace and GET /v1/incidents (T1-11).

Postgres: reuse tests/control-plane/api/conftest.py session fixture
(`migrated_db` / `db_conn`). That cluster binds a random free port and
always pg_ctl stop (see ephemeral_pg.py). Do not start a private 55440 cluster.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import Json

import read_api
from app_roles import AppRole
from connection import transaction

VIEWER = "viewer-token"
RISK_ADMIN = "risk-admin-token"
AUTH = {"Authorization": f"Bearer {VIEWER}"}
RISK_AUTH = {"Authorization": f"Bearer {RISK_ADMIN}"}
TRACE_KEYS = (
    "raw_message",
    "hermes_decision",
    "risk_decision",
    "intent",
    "execution_events",
    "node_acks",
)
ENVELOPE_KEYS = (
    "schema_version",
    "data_source",
    "snapshot_id",
    "generated_at",
    "stale",
)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, migrated_db: str) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("VIEWER_TOKEN", VIEWER)
    monkeypatch.setenv("RISK_ADMIN_TOKEN", RISK_ADMIN)
    return TestClient(read_api.app)


def _hex64(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _seed_account(conn, account_id: str = "account-a") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts_projection"
            " (account_id, currency, equity, margin, reconciliation_state)"
            " VALUES (%s,'USDT',1000,100,'healthy')",
            (account_id,),
        )


def _seed_node(conn, *, account_id: str, node_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO node_heartbeats (node_id, account_id, status, last_seen_at)
            VALUES (%s, %s, 'ACTIVE', now())
            """,
            (node_id, account_id),
        )


def _seed_intent_chain(
    conn,
    *,
    source: str = "telegram",
    prompt_version: str = "hermes-trader-v1",
    model_version: str = "hermes-v1",
    action: str = "open_position",
    message_type: str = "new_signal",
    instrument: str = "ETHUSDT",
    account_id: str = "account-a",
    with_media: bool = True,
    with_events: bool = True,
    with_acks: bool = True,
) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    raw_id, run_id, ctx_id, dec_id, risk_id, intent_id = (str(uuid4()) for _ in range(6))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_messages (
                id, source, channel_id, source_message_id, source_version,
                source_received_at, content_hash, message_text, raw_payload
            ) VALUES (%s, %s, %s, %s, 'v1', %s, %s, %s, '{}'::jsonb)
            """,
            (
                raw_id,
                source,
                "operator" if source == "operator" else "-1001234567890",
                f"msg-{raw_id}",
                now,
                _hex64("raw", raw_id),
                "long ETHUSDT 5x from telegram channel"
                if source != "operator"
                else "manual operator close ETHUSDT long",
            ),
        )
        cur.execute(
            """
            INSERT INTO message_processing_runs (processing_run_id, raw_message_id, status)
            VALUES (%s, %s, 'succeeded')
            """,
            (run_id, raw_id),
        )
        cur.execute(
            """
            INSERT INTO context_snapshots (
                context_snapshot_id, raw_message_id, snapshot_type, context_version, snapshot
            ) VALUES (%s, %s, 'system', 'v1', '{}'::jsonb)
            """,
            (ctx_id, raw_id),
        )
        cur.execute(
            """
            INSERT INTO hermes_decisions (
                decision_id, raw_message_id, processing_run_id, context_snapshot_id,
                message_type, action, ambiguous, account_scope, target_account_id,
                instrument_symbol, side, entry_type, evidence, model_provider,
                model_version, prompt_version, context_version, temperature, created_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, false, 'single', %s, %s, 'long', 'limit',
                %s::jsonb, 'hermes', %s, %s, 'v1', 0, %s
            )
            """,
            (
                dec_id,
                raw_id,
                run_id,
                ctx_id,
                message_type,
                action,
                account_id,
                instrument,
                Json([{"confidence": 0.81, "symbol": instrument}]),
                model_version,
                prompt_version,
                now,
            ),
        )
        cur.execute(
            """
            INSERT INTO risk_decisions (
                risk_decision_id, hermes_decision_id, status, account_id,
                instrument_id, reason, decided_by
            ) VALUES (%s, %s, 'approved', %s, %s, NULL, 'risk-governor')
            """,
            (risk_id, dec_id, account_id, instrument),
        )
        cur.execute(
            """
            INSERT INTO trade_intents (
                intent_id, hermes_decision_id, risk_decision_id, account_id,
                instrument_id, action, status, order_plan, risk_budget,
                valid_until, idempotency_key, approved_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 'approved', '{}'::jsonb, '{}'::jsonb,
                %s, %s, %s
            )
            """,
            (
                intent_id,
                dec_id,
                risk_id,
                account_id,
                instrument,
                action,
                now,
                _hex64("intent", intent_id),
                now,
            ),
        )
        if with_media:
            cur.execute(
                """
                INSERT INTO media_assets (
                    asset_id, raw_message_id, sha256, object_key, mime, download_status
                ) VALUES (%s, %s, %s, %s, 'image/jpeg', 'downloaded')
                """,
                (
                    str(uuid4()),
                    raw_id,
                    _hex64("media", raw_id),
                    "media/eth-long-setup.jpg",
                ),
            )
        if with_events:
            cur.execute(
                """
                INSERT INTO execution_events (
                    execution_event_row_id, event_id, node_id, account_id, intent_id,
                    client_order_id, event_type, ts_event, payload
                ) VALUES (%s, %s, 'node-a', %s, %s, %s, 'OrderFilled', %s, %s)
                """,
                (
                    str(uuid4()),
                    f"evt-{intent_id}",
                    account_id,
                    intent_id,
                    "B" + ("a" * 32) + "01",
                    now,
                    Json({"last_px": "3510.50", "last_qty": "1.25"}),
                ),
            )
        if with_acks:
            command_id = str(uuid4())
            cur.execute(
                """
                INSERT INTO operator_commands (
                    command_id, command_type, scope, status, requested_by, reason,
                    idempotency_key
                ) VALUES (%s, 'HALT', %s, 'completed', 'risk_admin', 'trace fixture', %s)
                """,
                (
                    command_id,
                    Json({"intent_id": intent_id, "account_id": account_id}),
                    f"idem-{command_id}",
                ),
            )
            cur.execute(
                """
                INSERT INTO command_node_acks (
                    command_id, node_id, status, detail, ack_at
                ) VALUES (%s, 'node-a', 'acked', 'filled', %s)
                """,
                (command_id, now),
            )
    return {
        "raw_id": raw_id,
        "decision_id": dec_id,
        "risk_id": risk_id,
        "intent_id": intent_id,
    }


def _assert_envelope(body: dict) -> None:
    for key in ENVELOPE_KEYS:
        assert key in body, key
    assert body["schema_version"] == "1.0"
    assert body["data_source"] == "postgres_projection"
    assert "data" in body


def test_t1_11_complete_six_layer_fixture_aggregates(client, db_conn):
    with transaction(db_conn):
        _seed_account(db_conn)
        ids = _seed_intent_chain(db_conn)

    response = client.get(f"/v1/intents/{ids['intent_id']}/trace", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    data = body["data"]
    assert set(data) >= set(TRACE_KEYS)

    raw = data["raw_message"]
    assert raw["id"] == ids["raw_id"]
    assert raw["channel_id"] == "-1001234567890"
    assert "ETHUSDT" in (raw.get("message_text") or "")
    assert raw["source_received_at"]
    assert raw["media"] == [
        {"mime": "image/jpeg", "object_key": "media/eth-long-setup.jpg"}
    ]

    hermes = data["hermes_decision"]
    assert hermes["decision_id"] == ids["decision_id"]
    assert hermes["action"] == "open_position"
    assert hermes["message_type"] == "new_signal"
    assert hermes.get("evidence") is not None

    risk = data["risk_decision"]
    assert risk["risk_decision_id"] == ids["risk_id"]
    assert risk["status"] == "approved"
    assert risk["account_id"] == "account-a"
    assert risk["instrument_id"] == "ETHUSDT"
    assert "reason" in risk

    intent = data["intent"]
    assert intent["intent_id"] == ids["intent_id"]
    assert intent["account_id"] == "account-a"
    assert intent["action"] == "open_position"
    assert intent["instrument_id"] == "ETHUSDT"
    assert intent["status"] == "approved"

    events = data["execution_events"]
    assert isinstance(events, list) and len(events) == 1
    assert events[0]["event_type"] == "OrderFilled"
    assert events[0]["client_order_id"].startswith("B")
    assert events[0]["ts_event"]
    assert events[0]["payload"]["last_qty"] == "1.25"

    acks = data["node_acks"]
    assert isinstance(acks, list) and len(acks) == 1
    assert acks[0]["node_id"] == "node-a"
    assert acks[0]["status"] == "acked"
    assert acks[0]["ack_at"]
    assert acks[0].get("detail") == "filled"

    legacy = client.get(f"/v1/operator/orders/{ids['intent_id']}", headers=AUTH)
    assert legacy.status_code == 200
    legacy_body = legacy.json()
    assert legacy_body["intent"]["intent_id"] == ids["intent_id"]
    assert "orders" in legacy_body
    assert "execution_events" in legacy_body
    assert "open_positions" in legacy_body


def test_t1_11_manual_intent_without_hermes_is_null_not_error(client, db_conn):
    with transaction(db_conn):
        _seed_account(db_conn)
        ids = _seed_intent_chain(
            db_conn,
            source="operator",
            prompt_version="operator-v1",
            model_version="hermes-operator",
            action="close_position",
            message_type="position_update",
            with_media=False,
        )

    response = client.get(f"/v1/intents/{ids['intent_id']}/trace", headers=AUTH)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["hermes_decision"] is None
    assert data["intent"]["intent_id"] == ids["intent_id"]
    assert data["intent"]["action"] == "close_position"
    assert data["raw_message"]["id"] == ids["raw_id"]
    assert data["risk_decision"]["risk_decision_id"] == ids["risk_id"]
    assert data["execution_events"][0]["event_type"] == "OrderFilled"
    assert data["node_acks"][0]["node_id"] == "node-a"


def test_t1_11_incidents_open_list_is_readonly(client, db_conn):
    open_id = str(uuid4())
    closed_id = str(uuid4())
    opened_at = datetime(2026, 8, 30, 11, 58, tzinfo=timezone.utc)
    with transaction(db_conn):
        _seed_account(db_conn, "account-a")
        _seed_account(db_conn, "account-b")
        _seed_node(db_conn, account_id="account-a", node_id="node-a")
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO production_incidents (
                    incident_id, account_id, severity, status, summary, opened_at
                ) VALUES (%s, 'account-a', 'P1', 'open', 'node heartbeat frozen', %s)
                """,
                (open_id, opened_at),
            )
            cur.execute(
                """
                INSERT INTO production_incidents (
                    incident_id, account_id, severity, status, summary, opened_at, closed_at
                ) VALUES (%s, 'account-b', 'P2', 'closed', 'already resolved', %s, now())
                """,
                (closed_id, opened_at),
            )

    response = client.get("/v1/incidents?status=open", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    rows = body["data"]
    assert isinstance(rows, list)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) >= {"id", "node", "summary", "opened_at"}
    assert row["id"] == open_id
    assert row["node"] == "node-a"
    assert row["summary"] == "node heartbeat frozen"
    assert row["opened_at"].startswith("2026-08-30T11:58:00")
    assert closed_id not in {item["id"] for item in rows}

    listed = {item["id"] for item in rows}
    with transaction(db_conn), db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM production_incidents WHERE status='open'")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT status FROM production_incidents WHERE incident_id=%s",
            (open_id,),
        )
        assert cur.fetchone()[0] == "open"
    assert listed == {open_id}


def test_t1_11_commands_status_has_terminal_fields(client, db_conn):
    command_id = str(uuid4())
    with transaction(db_conn):
        _seed_account(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO operator_commands (
                    command_id, command_type, scope, status, requested_by, reason,
                    idempotency_key, completed_at
                ) VALUES (%s, 'HALT', '{}'::jsonb, 'completed', 'risk_admin', 'done', %s, now())
                """,
                (command_id, f"idem-{command_id}"),
            )
            cur.execute(
                """
                INSERT INTO command_node_acks (command_id, node_id, status, ack_at)
                VALUES (%s, 'node-a', 'acked', now())
                """,
                (command_id,),
            )

    response = client.get(f"/v1/commands/{command_id}", headers=RISK_AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["command_id"] == command_id
    assert body["status"] == "completed"
    assert body["completed"] is True
    assert body["acks"] == [{"node_id": "node-a", "status": "acked"}]


def _role_paths(role: AppRole) -> set[str]:
    return {
        route.path
        for route in read_api.create_app(role).routes
        if hasattr(route, "path")
    }


def test_t1_11_trace_routes_belong_to_operator_query():
    operator_query = _role_paths(AppRole.OPERATOR_QUERY)
    node_control = _role_paths(AppRole.NODE_CONTROL)
    event_ingest = _role_paths(AppRole.EVENT_INGEST)
    assert "/v1/intents/{intent_id}/trace" in operator_query
    assert "/v1/incidents" in operator_query
    assert "/v1/intents/{intent_id}/trace" not in node_control
    assert "/v1/incidents" not in node_control
    assert "/v1/intents/{intent_id}/trace" not in event_ingest
    assert "/v1/incidents" not in event_ingest
