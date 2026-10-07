"""G3 transactional handoff against the complete ephemeral PostgreSQL schema."""

from copy import deepcopy
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json, RealDictCursor
import pytest

from signal_handoff import (
    SignalHandoffError, accept_operation, canonical_request_hash, load_request,
)


ACCOUNT = "account-a"


@pytest.fixture()
def signal_db(migrated_db):
    yield migrated_db
    # Down migration restores the old status/purpose constraints without
    # relabeling real accepted operations as shadow or dropping audit records.
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM signal_dispatch_tasks")
        cur.execute("DELETE FROM message_processing_runs WHERE processing_purpose = 'signal'")


def _seed(cur, *, purpose="signal", account=ACCOUNT, related=None, status="leased", request_overrides=None, source_overrides=None):
    raw, task, run, token = [str(uuid4()) for _ in range(4)]
    source = {"source_platform": "telegram", "channel_id": "-100123",
              "source_message_id": str(uuid4()), "edit_version": "1", "account_id": account}
    if source_overrides:
        source.update(source_overrides)
    cur.execute("""INSERT INTO raw_messages
        (id, source, channel_id, source_message_id, source_version, source_received_at, content_hash,raw_payload)
        VALUES (%s, 'telegram', %s, %s, %s, now(), %s,jsonb_build_object('source_ts',clock_timestamp()))""",
        (raw, source["channel_id"], source["source_message_id"], source["edit_version"], "a" * 64))
    cur.execute("""INSERT INTO message_processing_runs
        (processing_run_id, raw_message_id, status, account_id, attempt, claim_token,
         processing_purpose, lease_expires_at)
        VALUES (%s,%s,'processing',%s,1,%s,%s,now()+interval '5 minutes')""",
        (run, raw, account, token, purpose))
    body = {"account_id": account, "action": "open_position", "symbol": "BTCUSDT", "side": "long",
            "entry": {"type": "market"}, "stop_loss": 50000, "take_profits": [],
            "raw_message_id": raw, "client_ref": "channel-reference", "source_identity": source,
            "source_message_id": source["source_message_id"],
            "signal_claim": {"task_id": task, "processing_run_id": run, "attempt": 1,
                             "claim_token": token, "stable_action_or_leg_id": "entry-leg-1"}}
    if request_overrides:
        body.update(request_overrides)
    cur.execute("""INSERT INTO signal_dispatch_tasks
        (task_id, identity_key, source_platform, channel_id, source_message_id, edit_version,
         account_id, raw_message_id, current_processing_run_id, status, attempt, claim_token,
         lease_expires_at, processing_purpose, execution_request, execution_context, related_task_id)
        VALUES (%s,%s,'telegram',%s,%s,%s,%s,%s,%s,%s,1,%s,now()+interval '5 minutes',%s,%s,%s,%s)""",
        (task, task, source["channel_id"], source["source_message_id"], source["edit_version"], account, raw, run, status,
         token, purpose, Json(body), Json({"account_id": account, "account_revision": 0, "books": {}}), related))
    return body


def _intent(cur, *, account=ACCOUNT):
    raw, run, snap, decision, risk, intent = [str(uuid4()) for _ in range(6)]
    cur.execute("""INSERT INTO raw_messages
        (id, source, channel_id, source_message_id, source_version, source_received_at, content_hash)
        VALUES (%s,'operator','test',%s,'1',now(),%s)""", (raw, raw, "b" * 64))
    cur.execute("INSERT INTO message_processing_runs (processing_run_id,raw_message_id,status) VALUES (%s,%s,'succeeded')", (run, raw))
    cur.execute("INSERT INTO context_snapshots (context_snapshot_id,raw_message_id,snapshot_type,context_version) VALUES (%s,%s,'test','v1')", (snap, raw))
    cur.execute("""INSERT INTO hermes_decisions
        (decision_id,raw_message_id,processing_run_id,context_snapshot_id,message_type,action,ambiguous,
         account_scope,target_account_id,entry_type,model_version,prompt_version,context_version,temperature,created_at)
        VALUES (%s,%s,%s,%s,'new_signal','open_position',false,'single',%s,'market','v1','v1','v1',0,now())""",
        (decision, raw, run, snap, account))
    cur.execute("""INSERT INTO risk_decisions
        (risk_decision_id,hermes_decision_id,status,account_id,decided_by)
        VALUES (%s,%s,'approved',%s,'test')""", (risk, decision, account))
    cur.execute("""INSERT INTO trade_intents
        (intent_id,hermes_decision_id,risk_decision_id,account_id,instrument_id,action,valid_until,idempotency_key)
        VALUES (%s,%s,%s,%s,'BTCUSDT','open_position',now()+interval '5 minutes',%s)""",
        (intent, decision, risk, account, uuid4().hex + uuid4().hex))
    return intent


def test_hash_ignores_claim_transport_but_preserves_business_semantics():
    body = {"signal_claim": {"attempt": 1, "claim_token": "old", "processing_run_id": "run1",
                             "task_id": "task1", "stable_action_or_leg_id": "one"}, "stop_loss": 50}
    other = deepcopy(body)
    other["signal_claim"].update(attempt=2, claim_token="new", processing_run_id="run2")
    assert canonical_request_hash(body) == canonical_request_hash(other)
    other["stop_loss"] = 51
    assert canonical_request_hash(body) != canonical_request_hash(other)


def test_acceptance_and_restart_replay_are_atomic_and_account_scoped(signal_db):
    with psycopg2.connect(signal_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        body = _seed(cur, status="reconciling")
        intent = _intent(cur)
        context = load_request(cur, ACCOUNT, body, lock=True)
        assert context["expected_versions"] == {"account_id": ACCOUNT, "account_revision": 0, "books": {}}
        assert len(context["idempotency_key"]) == 64
        accept_operation(cur, context, intent)
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE signal_dispatch_tasks SET lease_expires_at=now()-interval '1 hour'")
        cur.execute("UPDATE message_processing_runs SET lease_expires_at=now()-interval '1 hour' WHERE processing_purpose='signal'")
        replay = load_request(cur, ACCOUNT, body, lock=True)
        assert replay["operator_intent_id"] == intent
        assert replay["existing_operation"] is True
        accept_operation(cur, replay, intent)
        cur.execute("SELECT status FROM signal_dispatch_tasks WHERE task_id=%s", (context["task_id"],))
        assert cur.fetchone()[0] == "dispatched"
        cur.execute("SELECT status FROM message_processing_runs WHERE processing_run_id=%s", (context["processing_run_id"],))
        assert cur.fetchone()[0] == "succeeded"
        with pytest.raises(SignalHandoffError) as error:
            load_request(cur, "account-b", body)
        assert error.value.status_code == 403
        body["stop_loss"] += 1
        with pytest.raises(SignalHandoffError, match="differs"):
            load_request(cur, ACCOUNT, body)


def test_rollback_does_not_accept_signal_or_finish_run(signal_db):
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        body = _seed(cur)
        intent = _intent(cur)
    conn = psycopg2.connect(signal_db)
    try:
        with conn.cursor() as cur:
            accept_operation(cur, load_request(cur, ACCOUNT, body, lock=True), intent)
        conn.rollback()
    finally:
        conn.close()
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        context = load_request(cur, ACCOUNT, body, lock=True)
        assert context["existing_operation"] is False
        cur.execute("SELECT status FROM message_processing_runs WHERE processing_run_id=%s", (context["processing_run_id"],))
        assert cur.fetchone()[0] == "processing"


@pytest.mark.parametrize("mutation", ["shadow", "attempt", "token", "run", "source", "expired", "run_expired", "payload"])
def test_invalid_claims_cannot_create_operations(signal_db, mutation):
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        body = _seed(cur, purpose="shadow" if mutation == "shadow" else "signal")
        if mutation == "attempt":
            body["signal_claim"]["attempt"] = 2
        elif mutation == "token":
            body["signal_claim"]["claim_token"] = str(uuid4())
        elif mutation == "run":
            body["signal_claim"]["processing_run_id"] = str(uuid4())
        elif mutation == "source":
            body["source_identity"]["edit_version"] = "2"
        elif mutation == "expired":
            cur.execute("UPDATE signal_dispatch_tasks SET lease_expires_at=now()-interval '1 second'")
        elif mutation == "run_expired":
            cur.execute("UPDATE message_processing_runs SET lease_expires_at=now()-interval '1 second'")
        elif mutation == "payload":
            body["stop_loss"] += 1
        with pytest.raises(SignalHandoffError):
            load_request(cur, ACCOUNT, body, lock=True)


def test_accept_rechecks_clock_after_load_and_does_not_partially_finish(signal_db):
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        body = _seed(cur)
        intent = _intent(cur)
        context = load_request(cur, ACCOUNT, body, lock=True)
        cur.execute("UPDATE signal_dispatch_tasks SET lease_expires_at=clock_timestamp()-interval '1 second'")
        with pytest.raises(SignalHandoffError):
            accept_operation(cur, context, intent)
        cur.execute("SELECT operator_intent_id FROM signal_dispatch_tasks")
        assert cur.fetchone()[0] is None
        cur.execute("SELECT status FROM message_processing_runs WHERE processing_purpose='signal'")
        assert cur.fetchone()[0] == "processing"


def test_foreign_account_intent_cannot_be_accepted(signal_db):
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        body = _seed(cur)
        intent = _intent(cur, account="account-b")
        context = load_request(cur, ACCOUNT, body, lock=True)
        with pytest.raises(SignalHandoffError):
            accept_operation(cur, context, intent)
        cur.execute("SELECT status FROM message_processing_runs WHERE processing_purpose='signal'")
        assert cur.fetchone()[0] == "processing"


def test_intent_deadline_is_rechecked_before_acceptance(signal_db):
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        body = _seed(cur)
        intent = _intent(cur)
        context = load_request(cur, ACCOUNT, body, lock=True)
        cur.execute("UPDATE trade_intents SET valid_until=clock_timestamp()-interval '1 second' WHERE intent_id=%s", (intent,))
        with pytest.raises(SignalHandoffError):
            accept_operation(cur, context, intent)
        cur.execute("SELECT operator_intent_id FROM signal_dispatch_tasks")
        assert cur.fetchone()[0] is None
        cur.execute("SELECT status FROM message_processing_runs WHERE processing_purpose='signal'")
        assert cur.fetchone()[0] == "processing"


def test_edit_does_not_infer_existing_operation_as_parent(signal_db):
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        original = _seed(cur)
        intent = _intent(cur)
        accepted = load_request(cur, ACCOUNT, original, lock=True)
        accept_operation(cur, accepted, intent)
        edit = _seed(cur, related=accepted["task_id"], source_overrides={
            "source_message_id": original["source_identity"]["source_message_id"], "edit_version": "2",
        })
        with pytest.raises(SignalHandoffError) as error:
            load_request(cur, ACCOUNT, edit, lock=True)
        assert error.value.code == "signal_edit_requires_reconciliation"
        third = _seed(cur, related=edit["signal_claim"]["task_id"], source_overrides={
            "source_message_id": original["source_identity"]["source_message_id"], "edit_version": "3",
        })
        with pytest.raises(SignalHandoffError) as error:
            load_request(cur, ACCOUNT, third, lock=True)
        assert error.value.code == "signal_edit_requires_reconciliation"


def test_registered_request_and_purpose_are_immutable(signal_db):
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        body = _seed(cur)
        for column, value in (("execution_request", "{}"), ("execution_context", "{}"), ("processing_purpose", "shadow")):
            cur.execute("SAVEPOINT mutation")
            with pytest.raises(psycopg2.Error, match="immutable"):
                cur.execute(f"UPDATE signal_dispatch_tasks SET {column}=%s WHERE task_id=%s", (value, body["signal_claim"]["task_id"]))
            cur.execute("ROLLBACK TO SAVEPOINT mutation")


def test_operator_role_has_only_needed_signal_columns(signal_db):
    with psycopg2.connect(signal_db) as conn, conn.cursor() as cur:
        body = _seed(cur)
        intent = _intent(cur)
        cur.execute("SET LOCAL ROLE trader_v3_operator_query")
        context = load_request(cur, ACCOUNT, body, lock=True)
        accept_operation(cur, context, intent)
        cur.execute("SELECT has_column_privilege(current_user,'signal_dispatch_tasks','execution_request','UPDATE')")
        assert cur.fetchone()[0] is False
        cur.execute("SELECT has_table_privilege(current_user,'accounts_projection','UPDATE')")
        assert cur.fetchone()[0] is False
