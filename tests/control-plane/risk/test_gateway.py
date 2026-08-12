from __future__ import annotations

import json
from uuid import uuid4

import psycopg2
import pytest

import gateway
from connection import transaction
from policy import RiskPolicy

POLICY = RiskPolicy(default_account_id="acct-1")
ROLLOUT_ACCOUNTS = ("account-a", "account-b", "account-c", "account-d")
ROLLOUT_PHASE_BY_ACCOUNT = {
    "account-a": "account_a_canary",
    "account-b": "account_b_rollout",
    "account-c": "account_c_rollout",
    "account-d": "account_d_rollout",
}
ROLLOUT_PHASE_VERSION = {
    "account_a_canary": 1,
    "account_b_rollout": 2,
    "account_c_rollout": 3,
    "account_d_rollout": 4,
    "fleet_complete": 5,
    "aborted": 2,
}


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


def seed_node_heartbeat(conn, account_id="acct-1"):
    with transaction(conn), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO node_heartbeats (
                node_id,
                account_id,
                status,
                last_seen_at
            )
            VALUES (%s, %s, 'ACTIVE', now())
            """,
            (f"node-{account_id}", account_id),
        )


def seed_reviewed_release_rollout(
    conn,
    *,
    phase: str,
    release_id: str = "release-a",
    created_age_minutes: int = 0,
) -> dict:
    redis_epoch = _active_redis_epoch(conn)
    image_digest = "sha256:" + ("1" * 64)
    config_sha256 = "2" * 64
    dependency_lock_sha256 = "3" * 64
    schema_epoch = "0013_four_account_rollout"
    with transaction(conn), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reviewed_release_rollouts (
                release_id,
                redis_fencing_epoch,
                image_digest,
                config_sha256,
                dependency_lock_sha256,
                schema_epoch,
                manifest_sha256,
                bundle_manifest_sha256,
                registration_idempotency_key,
                phase,
                phase_version,
                reviewed_by,
                reviewed_at,
                created_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                'account_a_canary', 1,
                'risk-test-reviewer',
                now(),
                now() - (%s * interval '1 minute')
            )
            """,
            (
                release_id,
                redis_epoch,
                image_digest,
                config_sha256,
                dependency_lock_sha256,
                schema_epoch,
                "4" * 64,
                "5" * 64,
                f"{release_id}-registration",
                created_age_minutes,
            ),
        )
        if phase == "aborted":
            cur.execute(
                """
                UPDATE reviewed_release_rollouts
                SET phase='aborted',
                    phase_version=2
                WHERE release_id=%s
                """,
                (release_id,),
            )
        else:
            sequence = (
                ("account_b_rollout", 2),
                ("account_c_rollout", 3),
                ("account_d_rollout", 4),
                ("fleet_complete", 5),
            )
            for next_phase, phase_version in sequence:
                if phase == "account_a_canary":
                    break
                cur.execute(
                    """
                    UPDATE reviewed_release_rollouts
                    SET phase=%s,
                        phase_version=%s
                    WHERE release_id=%s
                    """,
                    (next_phase, phase_version, release_id),
                )
                if next_phase == phase:
                    break
    return {
        "release_id": release_id,
        "rollout_phase": phase,
        "phase_version": ROLLOUT_PHASE_VERSION[phase],
    }


def _active_redis_epoch(conn) -> str:
    existing = _one(
        conn,
        """
        SELECT redis_fencing_epoch::text
        FROM redis_fencing_epochs
        WHERE status='active'
        LIMIT 1
        """,
    )
    if existing:
        return existing[0]
    redis_epoch = str(uuid4())
    with transaction(conn), conn.cursor() as cur:
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
                %s, 'trader-v3', 'active',
                %s, %s, %s,
                'risk-test-volume',
                'risk-test',
                now()
            )
            """,
            (
                redis_epoch,
                "6" * 64,
                "7" * 64,
                "8" * 40,
            ),
        )
    return redis_epoch


def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _live_open_gate_check(checks, *, passed: bool):
    matches = [
        check
        for check in checks
        if check.get("name") == "live_open_gate"
        and check.get("passed") is passed
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.fixture(autouse=True)
def cleanup_rollout_state(db_conn):
    yield
    with transaction(db_conn), db_conn.cursor() as cur:
        cur.execute("DELETE FROM live_canary_permits")
        cur.execute("DELETE FROM reviewed_release_rollout_events")
        cur.execute("DELETE FROM reviewed_release_rollouts")


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


@pytest.mark.parametrize("account_id", ROLLOUT_ACCOUNTS)
def test_rollout_accounts_fail_closed_when_rollout_gate_is_missing(db_conn, account_id):
    seed_decision(db_conn, target_account_id=account_id)
    seed_risk_state(db_conn, account_id=account_id)
    seed_node_heartbeat(db_conn, account_id=account_id)

    result = gateway.process_one_decision(db_conn, policy=POLICY)

    assert result["status"] == "needs_review"
    assert result["intent_id"] is None
    assert _one(db_conn, "SELECT count(*) FROM trade_intents")[0] == 0
    checks = _one(
        db_conn,
        "SELECT checks FROM risk_decisions WHERE risk_decision_id=%s",
        (result["risk_decision_id"],),
    )[0]
    _live_open_gate_check(checks, passed=False)


@pytest.mark.parametrize("account_id", ROLLOUT_ACCOUNTS)
def test_rollout_accounts_hold_channel_open_during_canary_only_gate(
    db_conn,
    account_id,
):
    phase = ROLLOUT_PHASE_BY_ACCOUNT[account_id]
    expected_gate = seed_reviewed_release_rollout(db_conn, phase=phase)
    seed_decision(db_conn, target_account_id=account_id)
    seed_risk_state(db_conn, account_id=account_id)
    seed_node_heartbeat(db_conn, account_id=account_id)

    result = gateway.process_one_decision(db_conn, policy=POLICY)

    assert result["status"] == "needs_review"
    assert result["intent_id"] is None
    assert result["reason"] == (
        "live_open_gate_canary_only: opening action held until "
        "fleet_complete; canary opens require the operator permit path"
    )
    assert _one(db_conn, "SELECT count(*) FROM trade_intents")[0] == 0
    checks = _one(
        db_conn,
        "SELECT checks FROM risk_decisions WHERE risk_decision_id=%s",
        (result["risk_decision_id"],),
    )[0]
    gate_check = _live_open_gate_check(checks, passed=False)
    assert expected_gate["release_id"] in gate_check["detail"]
    assert expected_gate["rollout_phase"] in gate_check["detail"]
    assert f"phase_version={expected_gate['phase_version']}" in gate_check["detail"]
    assert "mode=canary_only" in gate_check["detail"]


@pytest.mark.parametrize("account_id", ROLLOUT_ACCOUNTS)
def test_rollout_accounts_stamp_normal_gate_after_fleet_complete(
    db_conn,
    account_id,
):
    expected_gate = seed_reviewed_release_rollout(
        db_conn,
        phase="fleet_complete",
    )
    seed_decision(db_conn, target_account_id=account_id)
    seed_risk_state(db_conn, account_id=account_id)
    seed_node_heartbeat(db_conn, account_id=account_id)

    result = gateway.process_one_decision(db_conn, policy=POLICY)

    assert result["status"] == "approved"
    assert result["intent_id"] is not None
    order_plan = _one(
        db_conn,
        "SELECT order_plan FROM trade_intents WHERE intent_id=%s",
        (result["intent_id"],),
    )[0]
    assert order_plan["live_open_gate"] == {
        "mode": "normal",
        "release_id": expected_gate["release_id"],
        "rollout_phase": "fleet_complete",
        "phase_version": expected_gate["phase_version"],
    }
    checks = _one(
        db_conn,
        "SELECT checks FROM risk_decisions WHERE risk_decision_id=%s",
        (result["risk_decision_id"],),
    )[0]
    gate_check = _live_open_gate_check(checks, passed=True)
    assert expected_gate["release_id"] in gate_check["detail"]
    assert "rollout_phase=fleet_complete" in gate_check["detail"]
    assert f"phase_version={expected_gate['phase_version']}" in gate_check["detail"]
    assert "mode=normal" in gate_check["detail"]


def test_legacy_account_open_remains_compatible_during_rollout(db_conn):
    seed_reviewed_release_rollout(db_conn, phase="account_a_canary")
    dec_id = seed_decision(db_conn, target_account_id="acct-1")
    seed_risk_state(db_conn, account_id="acct-1")
    seed_node_heartbeat(db_conn, account_id="acct-1")

    result = gateway.process_one_decision(db_conn, policy=POLICY)

    assert result["status"] == "approved"
    assert result["intent_id"] is not None
    order_plan = _one(
        db_conn,
        "SELECT order_plan FROM trade_intents WHERE hermes_decision_id=%s",
        (dec_id,),
    )[0]
    assert "live_open_gate" not in order_plan


def test_latest_aborted_rollout_fails_closed_over_older_fleet_complete(db_conn):
    seed_reviewed_release_rollout(
        db_conn,
        phase="fleet_complete",
        release_id="release-old",
        created_age_minutes=10,
    )
    seed_reviewed_release_rollout(
        db_conn,
        phase="aborted",
        release_id="release-new",
    )
    seed_decision(db_conn, target_account_id="account-a")
    seed_risk_state(db_conn, account_id="account-a")
    seed_node_heartbeat(db_conn, account_id="account-a")

    result = gateway.process_one_decision(db_conn, policy=POLICY)

    assert result["status"] == "needs_review"
    assert result["intent_id"] is None
    assert _one(db_conn, "SELECT count(*) FROM trade_intents")[0] == 0
    checks = _one(
        db_conn,
        "SELECT checks FROM risk_decisions WHERE risk_decision_id=%s",
        (result["risk_decision_id"],),
    )[0]
    _live_open_gate_check(checks, passed=False)


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


def test_stale_node_heartbeat_holds_approval(db_conn):
    seed_decision(db_conn)
    seed_risk_state(db_conn)
    seed_node_heartbeat(db_conn)
    with transaction(db_conn), db_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE node_heartbeats
            SET last_seen_at = now() - interval '2 minutes'
            """
        )

    result = gateway.process_one_decision(db_conn, policy=POLICY)

    assert result["status"] == "needs_review"
    assert result["intent_id"] is None
    assert _one(db_conn, "SELECT count(*) FROM trade_intents")[0] == 0


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
