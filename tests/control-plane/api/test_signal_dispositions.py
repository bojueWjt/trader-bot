from uuid import uuid4

import pytest
from psycopg2.extras import Json

import read_api
from connection import transaction
from signal_status import signal_disposition
from test_m1e_trace import AUTH, client, _seed_account, _seed_intent_chain


@pytest.mark.parametrize("row,expected", [
    ({}, ("pending", "downstream_evidence_missing_requires_review")),
    ({"task_status": "skipped", "disposition_reason": "not_a_signal"}, ("skipped", "not_a_signal")),
    ({"task_status": "failed", "disposition_reason": "lease_expired"}, ("rejected", "lease_expired")),
    ({"task_status": "expired", "disposition_reason": "claim_expired_without_operation",
      "shadow_result": {"operator_detail": {"http_status": 409, "detail": "position_revision_mismatch"}}},
     ("rejected", "claim_expired_without_operation: position_revision_mismatch")),
    ({"task_status": "shadow_dispatched"}, ("skipped", "shadow_only_no_live_submission")),
    ({"task_status": "reconciling", "disposition_reason": "http_timeout"}, ("pending", "http_timeout")),
    ({"intent_id": "i", "intent_status": "approved"}, ("pending", "operation_accepted")),
    ({"intent_id": "i", "intent_status": "rejected", "denial_reason": "position_exists"}, ("rejected", "position_exists")),
    ({"intent_id": "i", "intent_status": "approved", "orders": [
        {"status": "filled", "filled_quantity": 1, "lifecycle_role": "entry"}
    ]}, ("filled", "exchange_fill_recorded")),
    ({"intent_id": "i", "intent_status": "approved", "orders": [
        {"status": "partially_filled", "filled_quantity": 1, "lifecycle_role": "entry"}
    ]}, ("pending", "operation_partial")),
])
def test_disposition_requires_evidence(row, expected):
    assert signal_disposition(row, operation_status=read_api._intent_operation_status) == expected


def test_signal_coverage_keeps_unprocessed_input_and_unknown_timestamps(client, db_conn):
    missing_id = str(uuid4())
    with transaction(db_conn):
        _seed_account(db_conn)
        ids = _seed_intent_chain(db_conn, with_events=False, with_acks=False)
        with db_conn.cursor() as cur:
            cur.execute(
                """INSERT INTO raw_messages
                (id,source,channel_id,source_message_id,source_version,source_received_at,
                 content_hash,message_text,raw_payload)
                VALUES (%s,'telegram','-100123','unprocessed','1',now(),%s,'BTC signal',%s)""",
                (missing_id, "a" * 64, Json({})),
            )
    response = client.get("/v1/signals?days=2", headers=AUTH)
    assert response.status_code == 200, response.text
    rows = {row["raw_message_id"]: row for row in response.json()["signals"]}
    assert set(rows) == {missing_id, ids["raw_id"]}
    assert rows[missing_id]["reason"] == "downstream_evidence_missing_requires_review"
    assert rows[missing_id]["source_ts_unknown"] is True
    assert rows[missing_id]["receive_ts_unknown"] is True
    assert rows[missing_id]["receive_ts"] is None
    assert rows[ids["raw_id"]]["disposition"] == "pending"
    limited = client.get("/v1/signals?limit=1", headers=AUTH).json()
    assert limited["truncated"] is True
    assert len(limited["signals"]) == 1
