"""Signal-purpose intake is atomic and cannot coexist with a legacy executable outbox."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg2
import pytest

from test_ingress_service import _base_payload
from ingress.service import IngressValidationError, ingest_raw_telegram_update


def _payload(**overrides):
    values = {"account_id": "account-a", "processing_purpose": "signal", "action": "evaluate"}
    values.update(overrides)
    return _base_payload(**values)


def _rows(url, sql, params=()):
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def test_signal_intake_has_no_legacy_outbox_and_no_receive_based_evaluation_expiry(migrated_db):
    payload = _payload(raw_payload={"source_ts": None, "source_ts_unknown": True})
    result = ingest_raw_telegram_update(payload, migrated_db)
    assert result["processing_purpose"] == "signal"
    assert result["outbox_event_id"] is None
    assert _rows(migrated_db, "SELECT processing_purpose,status,expires_at FROM signal_dispatch_tasks") == [("signal", "pending", None)]
    assert _rows(migrated_db, "SELECT count(*) FROM outbox_events WHERE event_type='queued_for_hermes'") == [(0,)]
    raw = _rows(migrated_db, "SELECT raw_payload FROM raw_messages")[0][0]
    assert raw["source_ts"] is None and raw["source_ts_unknown"] is True
    duplicate = ingest_raw_telegram_update(payload, migrated_db)
    assert duplicate["task_id"] == result["task_id"]
    assert duplicate["processing_purpose"] == "signal" and duplicate["task_inserted"] is False
    assert _rows(migrated_db, "SELECT count(*) FROM signal_dispatch_tasks") == [(1,)]


@pytest.mark.parametrize("first,second", [("shadow", "signal"), ("signal", "shadow")])
def test_existing_source_purpose_cannot_be_promoted_or_fall_back(migrated_db, first, second):
    result = ingest_raw_telegram_update(_payload(processing_purpose=first), migrated_db)
    with pytest.raises(IngressValidationError, match="processing_purpose_conflict"):
        ingest_raw_telegram_update(_payload(processing_purpose=second), migrated_db)
    assert _rows(migrated_db, "SELECT processing_purpose FROM signal_dispatch_tasks") == [(first,)]
    assert _rows(migrated_db, "SELECT count(*) FROM raw_messages") == [(1,)]
    expected = 1 if first == "shadow" else 0
    assert _rows(migrated_db, "SELECT count(*) FROM outbox_events") == [(expected,)]


def test_same_raw_cross_account_purpose_conflict_is_rejected(migrated_db):
    ingest_raw_telegram_update(_payload(processing_purpose="shadow"), migrated_db)
    with pytest.raises(IngressValidationError, match="processing_purpose_conflict"):
        ingest_raw_telegram_update(_payload(account_id="account-b"), migrated_db)
    assert _rows(migrated_db, "SELECT account_id,processing_purpose FROM signal_dispatch_tasks") == [("account-a", "shadow")]


def test_signal_source_can_fan_out_to_another_signal_account_without_legacy_work(migrated_db):
    first = ingest_raw_telegram_update(_payload(), migrated_db)
    second = ingest_raw_telegram_update(_payload(account_id="account-b"), migrated_db)
    assert first["raw_message_id"] == second["raw_message_id"]
    assert first["task_id"] != second["task_id"]
    assert _rows(migrated_db, "SELECT account_id,processing_purpose FROM signal_dispatch_tasks ORDER BY account_id") == [
        ("account-a", "signal"), ("account-b", "signal"),
    ]
    assert _rows(migrated_db, "SELECT count(*) FROM outbox_events") == [(0,)]


def test_cutover_history_is_durable_skipped_and_cannot_be_reactivated(migrated_db):
    first = ingest_raw_telegram_update(_payload(route_error="signal_cutover_history"), migrated_db)
    assert first["task_status"] == "skipped"
    retry = ingest_raw_telegram_update(_payload(), migrated_db)
    assert retry["task_inserted"] is False
    assert _rows(migrated_db, "SELECT status,disposition_reason FROM signal_dispatch_tasks") == [("skipped", "signal_cutover_history")]
    assert _rows(migrated_db, "SELECT count(*) FROM outbox_events") == [(0,)]


def test_unassigned_signal_is_rejected_without_partial_raw_write(migrated_db):
    with pytest.raises(IngressValidationError, match="assigned execution account"):
        ingest_raw_telegram_update(_payload(account_id="unassigned"), migrated_db)
    assert _rows(migrated_db, "SELECT count(*) FROM raw_messages") == [(0,)]


def test_enqueue_failure_rolls_back_raw_and_media(migrated_db, monkeypatch):
    import ingress.service as service
    def fail(*args, **kwargs):
        raise RuntimeError("enqueue unavailable")
    monkeypatch.setattr(service, "_enqueue_signal_task", fail)
    with pytest.raises(RuntimeError, match="enqueue unavailable"):
        service.ingest_raw_telegram_update(_payload(), migrated_db)
    assert _rows(migrated_db, "SELECT count(*) FROM raw_messages") == [(0,)]
    assert _rows(migrated_db, "SELECT count(*) FROM outbox_events") == [(0,)]


def test_concurrent_shadow_signal_race_selects_only_one_execution_writer(migrated_db):
    barrier = Barrier(2)
    def ingest(purpose):
        barrier.wait(timeout=5)
        try:
            return ingest_raw_telegram_update(_payload(processing_purpose=purpose), migrated_db)["processing_purpose"]
        except IngressValidationError as exc:
            assert "processing_purpose_conflict" in str(exc)
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(ingest, ["shadow", "signal"]))
    assert results.count("conflict") == 1
    purpose = _rows(migrated_db, "SELECT processing_purpose FROM signal_dispatch_tasks")[0][0]
    assert _rows(migrated_db, "SELECT count(*) FROM outbox_events") == [(1 if purpose == "shadow" else 0,)]


def test_future_feeder_message_reaches_real_signal_queue_without_legacy_dispatch(migrated_db, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    helper_path = Path(__file__).resolve().parents[1] / "test_hermes_signal_feeder.py"
    spec = importlib.util.spec_from_file_location("signal_feeder_fixture_helpers", helper_path)
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    module, watcher, old_id = helpers._signal_cutover_fixture(monkeypatch, tmp_path)
    new_id = helpers._insert_telegram_message(watcher, msg_id=9001, created_at="2026-09-15 00:01:00")
    watcher.commit()
    monkeypatch.setenv("INGRESS_DATABASE_URL", migrated_db)
    monkeypatch.setattr(module, "run_hermes", lambda *args, **kwargs: pytest.fail("legacy writer invoked"))
    try:
        cursor, count = module.persist_new_watcher_messages(watcher, f"telegram_messages:{old_id}", dry_run=False)
        assert (cursor, count) == (f"telegram_messages:{new_id}", 1)
        batch = module.fetch_new(watcher, f"telegram_messages:{old_id}")
        assert module.attempt_batch_delivery(batch, False) == "success"
        module.commit_batch_cursor(cursor)
        assert module.fetch_new(watcher, module.load_cursor()) == []
        assert _rows(migrated_db, "SELECT account_id,processing_purpose,status,attempt,expires_at FROM signal_dispatch_tasks") == [
            ("account-a", "signal", "pending", 0, None),
        ]
        assert _rows(migrated_db, "SELECT count(*) FROM outbox_events") == [(0,)]
        raw_payload = _rows(migrated_db, "SELECT raw_payload FROM raw_messages")[0][0]
        assert raw_payload["source_ts_unknown"] is True and raw_payload["source_ts"] is None
    finally:
        watcher.close()


def _post_signal(port, payload, token='secret'):
    import http.client
    import json
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    conn.request('POST', '/telegram/raw/signal', body=json.dumps(payload), headers={
        'authorization': f'Bearer {token}', 'content-type': 'application/json',
    })
    response = conn.getresponse()
    result = response.status, json.loads(response.read())
    conn.close()
    return result


def test_dedicated_http_signal_path_enforces_signal_purpose_and_no_legacy_outbox(migrated_db):
    from test_ingress_http_auth import _server
    payload = _payload()
    payload.pop('processing_purpose')
    with _server('secret', migrated_db) as port:
        status, result = _post_signal(port, payload)
        assert status == 201 and result['processing_purpose'] == 'signal'
        status, duplicate = _post_signal(port, payload)
        assert status == 200 and duplicate['task_id'] == result['task_id']
    assert _rows(migrated_db, 'SELECT processing_purpose FROM signal_dispatch_tasks') == [('signal',)]
    assert _rows(migrated_db, 'SELECT count(*) FROM outbox_events') == [(0,)]


def test_dedicated_http_signal_path_rejects_shadow_and_unauthorized(migrated_db):
    from test_ingress_http_auth import _server
    with _server('secret', migrated_db) as port:
        status, _ = _post_signal(port, _payload(processing_purpose='shadow'))
        assert status == 400
        status, _ = _post_signal(port, _payload(), token='wrong')
        assert status == 401
    assert _rows(migrated_db, 'SELECT count(*) FROM raw_messages') == [(0,)]
