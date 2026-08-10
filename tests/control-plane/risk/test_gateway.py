from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg2
import pytest

import gateway
from connection import transaction
from policy import RiskPolicy

POLICY = RiskPolicy(default_account_id="acct-1")


def seed_decision(
    conn,
    *,
    action="open_position",
    message_type="new_signal",
    side="long",
    instrument="BTCUSDT",
    entry_price=100.0,
    stop_loss=90.0,
    take_profits=(110.0, 120.0),
    leverage=3.0,
    target_position_id=None,
    target_account_id="acct-1",
    ambiguous=False,
    model_provider="hermes",
    raw_source="telegram",
    channel_id="signals",
    source_message_id=None,
    author_id=None,
):
    raw_id, run_id, ctx_id, dec_id = (str(uuid4()) for _ in range(4))
    source_message_id = source_message_id or f"m-{raw_id}"
    with transaction(conn), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_messages (id, source, channel_id, source_message_id, source_version,"
            " source_received_at, author_id, content_hash)"
            " VALUES (%s,%s,%s,%s,'v1',now(),%s,'h')",
            (raw_id, raw_source, channel_id, source_message_id, author_id),
        )
        cur.execute(
            "INSERT INTO message_processing_runs (processing_run_id, raw_message_id, status)"
            " VALUES (%s,%s,'succeeded')",
            (run_id, raw_id),
        )
        cur.execute(
            "INSERT INTO context_snapshots (context_snapshot_id, raw_message_id, snapshot_type,"
            " context_version, snapshot) VALUES (%s,%s,'system_snapshot_v1','ctx-v1','{}')",
            (ctx_id, raw_id),
        )
        cur.execute(
            """
            INSERT INTO hermes_decisions (
                decision_id, raw_message_id, processing_run_id, context_snapshot_id, schema_version,
                message_type, action, ambiguous, ambiguity_reasons, account_scope, target_account_id,
                target_position_id, instrument_symbol, side, entry_type, entry_price, stop_loss,
                take_profits, leverage, evidence, model_provider, model_version, prompt_version,
                context_version, temperature, created_at
            ) VALUES (%s,%s,%s,%s,'1.0',%s,%s,%s,'[]','single',%s,%s,%s,%s,'market',%s,%s,%s,%s,'[]',
                      %s,'m','hermes-trader-v1','ctx-v1',0,now())
            """,
            (dec_id, raw_id, run_id, ctx_id, message_type, action, ambiguous, target_account_id,
             target_position_id, instrument, side, entry_price, stop_loss,
             json.dumps(list(take_profits)), leverage, model_provider),
        )
    return dec_id


def seed_risk_state(conn, account_id="acct-1", instrument="BTCUSDT", mode="ACTIVE"):
    with transaction(conn), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO risk_state (risk_state_id, account_id, instrument_id, state) "
            "VALUES (%s, %s, %s, jsonb_build_object('mode', %s::text)) "
            "ON CONFLICT (account_id, instrument_id) DO UPDATE "
            "SET state = jsonb_set(coalesce(risk_state.state, '{}'::jsonb), '{mode}', to_jsonb(%s::text))",
            (str(uuid4()), account_id, instrument, mode, mode),
        )


def seed_node_heartbeat(
    conn,
    *,
    account_id="acct-1",
    node_id="node-acct-1",
    last_seen_at=None,
):
    observed_at = last_seen_at or datetime.now(timezone.utc)
    with transaction(conn), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO node_heartbeats (
                node_id,
                account_id,
                status,
                last_seen_at
            )
            VALUES (%s, %s, 'ACTIVE', %s)
            ON CONFLICT (node_id) DO UPDATE
            SET account_id = EXCLUDED.account_id,
                status = EXCLUDED.status,
                last_seen_at = EXCLUDED.last_seen_at
            """,
            (node_id, account_id, observed_at),
        )


def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def test_no_pending_returns_none(db_conn):
    assert gateway.process_one_decision(db_conn, policy=POLICY) is None


def test_approved_decision_writes_risk_decision_intent_and_outbox(db_conn):
    dec_id = seed_decision(
        db_conn,
        channel_id="-1002136478186",
        source_message_id="tg-msg-5026",
    )
    seed_risk_state(db_conn)
    seed_node_heartbeat(db_conn)
    result = gateway.process_one_decision(db_conn, policy=POLICY)

    assert result["status"] == "approved"
    assert result["intent_id"] is not None

    rd = _one(db_conn, "SELECT status, account_id, instrument_id FROM risk_decisions WHERE hermes_decision_id=%s", (dec_id,))
    assert rd == ("approved", "acct-1", "BTCUSDT")

    ti = _one(
        db_conn,
        "SELECT status, action::text, account_id, idempotency_key, order_plan "
        "FROM trade_intents WHERE hermes_decision_id=%s",
        (dec_id,),
    )
    assert ti[0] == "approved" and ti[1] == "open_position" and ti[2] == "acct-1"
    assert len(ti[3]) == 64  # sha256 hex
    assert ti[4]["authorization"] == {
        "authorized_by_type": "channel",
        "authorized_by_id": "-1002136478186",
        "source_message_id": "tg-msg-5026",
        "created_by_service": "decision-gateway",
        "parent_intent_id": False,
        "channel_id": "-1002136478186",
    }

    ob = _one(
        db_conn,
        "SELECT payload FROM outbox_events "
        "WHERE aggregate_type='trade_intent' AND aggregate_id=%s",
        (result["intent_id"],),
    )
    assert ob[0]["authorization"] == ti[4]["authorization"]


def test_user_raw_message_builds_user_authorization(db_conn):
    dec_id = seed_decision(
        db_conn,
        raw_source="user",
        channel_id="operator",
        source_message_id="user-request-7001",
        author_id="balen",
    )
    seed_risk_state(db_conn)
    seed_node_heartbeat(db_conn)

    result = gateway.process_one_decision(db_conn, policy=POLICY)

    assert result["status"] == "approved"
    row = _one(
        db_conn,
        "SELECT order_plan FROM trade_intents WHERE hermes_decision_id=%s",
        (dec_id,),
    )
    assert row[0]["authorization"] == {
        "authorized_by_type": "user",
        "authorized_by_id": "balen",
        "source_message_id": "user-request-7001",
        "created_by_service": "decision-gateway",
        "parent_intent_id": False,
    }


def test_idempotent_second_pass_makes_no_duplicate(db_conn):
    seed_decision(db_conn)
    seed_risk_state(db_conn)
    seed_node_heartbeat(db_conn)
    first = gateway.process_one_decision(db_conn, policy=POLICY)
    second = gateway.process_one_decision(db_conn, policy=POLICY)

    assert first["status"] == "approved"
    assert second is None  # decision already has a risk_decision
    assert _one(db_conn, "SELECT count(*) FROM trade_intents")[0] == 1
    assert _one(db_conn, "SELECT count(*) FROM risk_decisions")[0] == 1


def test_bad_geometry_rejected_without_intent(db_conn):
    seed_decision(db_conn, side="long", entry_price=100.0, stop_loss=105.0, take_profits=(110.0,))
    seed_risk_state(db_conn)
    result = gateway.process_one_decision(db_conn, policy=POLICY)

    assert result["status"] == "rejected"
    assert result["intent_id"] is None
    assert _one(db_conn, "SELECT count(*) FROM trade_intents")[0] == 0


def test_missing_risk_state_fails_closed_to_needs_review(db_conn):
    # No risk_state row -> the governor must NOT treat the account as ACTIVE.
    # Fail closed: needs_review, no intent (this was a fail-OPEN approval before).
    seed_decision(db_conn)
    result = gateway.process_one_decision(db_conn, policy=POLICY)
    assert result["status"] == "needs_review"
    assert result["intent_id"] is None
    assert _one(db_conn, "SELECT count(*) FROM trade_intents")[0] == 0


def test_fresh_other_account_heartbeat_does_not_mask_stale_target_account(db_conn):
    seed_decision(db_conn)
    seed_risk_state(db_conn)
    now = datetime.now(timezone.utc)
    seed_node_heartbeat(
        db_conn,
        last_seen_at=now - timedelta(minutes=5),
    )
    seed_node_heartbeat(
        db_conn,
        account_id="acct-2",
        node_id="node-acct-2",
        last_seen_at=now,
    )

    result = gateway.process_one_decision(db_conn, policy=POLICY)

    assert result["status"] == "needs_review"
    assert result["reason"] == (
        "context_stale: approval held pending fresh projection"
    )
    assert result["intent_id"] is None


def test_stale_decision_fails_closed_to_needs_review(db_conn):
    # A decision older than the freshness window must not auto-execute.
    dec_id = seed_decision(db_conn)
    seed_risk_state(db_conn)
    with transaction(db_conn), db_conn.cursor() as cur:
        cur.execute(
            "UPDATE hermes_decisions SET created_at = now() - interval '2 hours' WHERE decision_id=%s",
            (dec_id,),
        )
    result = gateway.process_one_decision(db_conn, policy=POLICY)
    assert result["status"] == "needs_review"
    assert result["intent_id"] is None


def test_ambiguous_needs_review_without_intent(db_conn):
    seed_decision(db_conn, ambiguous=True)
    result = gateway.process_one_decision(db_conn, policy=POLICY)

    assert result["status"] == "needs_review"
    assert result["intent_id"] is None
    assert _one(db_conn, "SELECT count(*) FROM trade_intents")[0] == 0


def test_hermes_decisions_table_enforces_hermes_provenance(db_conn):
    # Provenance is enforced at the canonical-schema layer: a non-hermes decision
    # cannot even be persisted, so the gateway can only ever see hermes decisions.
    with pytest.raises(psycopg2.errors.CheckViolation):
        seed_decision(db_conn, model_provider="openai")
