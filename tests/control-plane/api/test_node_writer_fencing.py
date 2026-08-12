from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from queue import Queue
from threading import Barrier
from uuid import uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient

import read_api


ACCOUNT_ID = "account-a"
NODE_ID = "nautilus-node-account-a"
NODE_TOKEN = "node-a-token"
REDIS_FENCING_EPOCH = "11111111-1111-4111-8111-111111111111"
REPLACEMENT_REDIS_FENCING_EPOCH = (
    "22222222-2222-4222-8222-222222222222"
)
RUNTIME_GENERATION = "runtime-new"
LEASE_FENCING_TOKEN = 42


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
        headers=_writer_headers(
            runtime_generation=RUNTIME_GENERATION,
            lease_fencing_token=LEASE_FENCING_TOKEN,
        ),
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


@pytest.mark.parametrize(
    ("method", "path", "body"),
    (
        (
            "get",
            f"/v1/nodes/{NODE_ID}/intents?account_id={ACCOUNT_ID}",
            None,
        ),
        (
            "post",
            f"/v1/nodes/{NODE_ID}/events",
            {
                "event_id": str(uuid4()),
                "event_type": "OrderAccepted",
                "account_id": ACCOUNT_ID,
            },
        ),
        (
            "post",
            f"/v1/nodes/{NODE_ID}/intents/{uuid4()}/ack",
            {
                "account_id": ACCOUNT_ID,
                "status": "received",
            },
        ),
        (
            "post",
            f"/v1/nodes/{NODE_ID}/execution-events",
            {
                "account_id": ACCOUNT_ID,
                "events": [],
            },
        ),
        (
            "post",
            f"/v1/nodes/{NODE_ID}/incidents",
            {
                "account_id": ACCOUNT_ID,
                "severity": "P1",
                "reason": "writer_fence_test",
                "summary": "stale writer must be rejected",
            },
        ),
        (
            "get",
            f"/v1/nodes/{NODE_ID}/commands?account_id={ACCOUNT_ID}",
            None,
        ),
        (
            "post",
            f"/v1/nodes/{NODE_ID}/commands/{uuid4()}/ack",
            {
                "account_id": ACCOUNT_ID,
                "status": "accepted",
            },
        ),
        (
            "get",
            f"/v1/accounts/{ACCOUNT_ID}?node_id={NODE_ID}",
            None,
        ),
        (
            "get",
            f"/v1/nodes/{NODE_ID}/exchange-state?account_id={ACCOUNT_ID}",
            None,
        ),
    ),
)
def test_replacement_fences_old_writer_on_every_node_path(
    client: TestClient,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    request = getattr(client, method)
    headers = _writer_headers(
        runtime_generation="runtime-old",
        lease_fencing_token=41,
    )
    if body is None:
        response = request(path, headers=headers)
    else:
        response = request(path, headers=headers, json=body)

    assert response.status_code == 409
    assert response.headers["x-writer-fence-rejected"] == "1"
    assert response.json()["detail"] in {
        "runtime generation mismatch",
        "lease fencing token mismatch",
    }


def test_active_epoch_rejects_missing_writer_identity(
    client: TestClient,
) -> None:
    response = client.get(
        f"/v1/nodes/{NODE_ID}/commands",
        params={"account_id": ACCOUNT_ID},
        headers=_auth_headers(),
    )

    assert response.status_code == 409
    assert response.headers["x-writer-fence-rejected"] == "1"
    assert response.json()["detail"] == "node writer identity is required"


def test_current_writer_identity_can_poll_commands(
    client: TestClient,
) -> None:
    response = client.get(
        f"/v1/nodes/{NODE_ID}/commands",
        params={"account_id": ACCOUNT_ID},
        headers=_writer_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"commands": []}


def test_current_runtime_with_stale_writer_token_is_rejected(
    client: TestClient,
) -> None:
    response = client.get(
        f"/v1/nodes/{NODE_ID}/commands",
        params={"account_id": ACCOUNT_ID},
        headers=_writer_headers(lease_fencing_token=41),
    )

    assert response.status_code == 409
    assert response.headers["x-writer-fence-rejected"] == "1"
    assert response.json()["detail"] == "lease fencing token mismatch"


def test_heartbeat_rejects_header_body_writer_mismatch(
    client: TestClient,
) -> None:
    response = client.post(
        f"/v1/nodes/{NODE_ID}/heartbeat",
        headers=_writer_headers(runtime_generation="runtime-old"),
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
            "heartbeat_sequence": 2,
        },
    )

    assert response.status_code == 409
    assert response.headers["x-writer-fence-rejected"] == "1"
    assert (
        response.json()["detail"]
        == "heartbeat writer identity mismatch"
    )


def test_heartbeat_rejects_equal_and_stale_sequence_with_fence_header(
    client: TestClient,
) -> None:
    equal_response = client.post(
        f"/v1/nodes/{NODE_ID}/heartbeat",
        headers=_writer_headers(),
        json=_heartbeat_body(heartbeat_sequence=1),
    )

    assert equal_response.status_code == 409
    assert equal_response.headers["x-writer-fence-rejected"] == "1"
    assert equal_response.json()["detail"] == "stale heartbeat writer"

    advance_response = client.post(
        f"/v1/nodes/{NODE_ID}/heartbeat",
        headers=_writer_headers(),
        json=_heartbeat_body(heartbeat_sequence=2),
    )
    assert advance_response.status_code == 200

    stale_response = client.post(
        f"/v1/nodes/{NODE_ID}/heartbeat",
        headers=_writer_headers(),
        json=_heartbeat_body(heartbeat_sequence=1),
    )

    assert stale_response.status_code == 409
    assert stale_response.headers["x-writer-fence-rejected"] == "1"
    assert stale_response.json()["detail"] == "stale heartbeat writer"


def test_newer_heartbeat_takes_over_and_fences_previous_writer(
    client: TestClient,
) -> None:
    replacement_generation = "runtime-replacement"
    replacement_token = LEASE_FENCING_TOKEN + 1
    takeover_response = client.post(
        f"/v1/nodes/{NODE_ID}/heartbeat",
        headers=_writer_headers(
            runtime_generation=replacement_generation,
            lease_fencing_token=replacement_token,
        ),
        json=_heartbeat_body(
            runtime_generation=replacement_generation,
            lease_fencing_token=replacement_token,
        ),
    )

    assert takeover_response.status_code == 200

    previous_writer_response = client.get(
        f"/v1/nodes/{NODE_ID}/commands",
        params={"account_id": ACCOUNT_ID},
        headers=_writer_headers(),
    )
    assert previous_writer_response.status_code == 409
    assert (
        previous_writer_response.headers["x-writer-fence-rejected"]
        == "1"
    )

    replacement_writer_response = client.get(
        f"/v1/nodes/{NODE_ID}/commands",
        params={"account_id": ACCOUNT_ID},
        headers=_writer_headers(
            runtime_generation=replacement_generation,
            lease_fencing_token=replacement_token,
        ),
    )
    assert replacement_writer_response.status_code == 200
    assert replacement_writer_response.json() == {"commands": []}


def test_two_runtimes_compete_and_highest_token_remains_writer(
    client: TestClient,
    migrated_db: str,
) -> None:
    barrier = Barrier(2)

    def submit(runtime_generation: str, lease_fencing_token: int):
        with TestClient(read_api.app) as worker_client:
            barrier.wait()
            return _post_heartbeat(
                worker_client,
                runtime_generation=runtime_generation,
                lease_fencing_token=lease_fencing_token,
                heartbeat_sequence=1,
            )

    contenders = (
        ("runtime-contender-low", 43),
        ("runtime-contender-high", 44),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda values: submit(*values),
                contenders,
            )
        )

    assert responses[1].status_code == 200
    assert responses[0].status_code in {200, 409}
    assert _heartbeat_record(migrated_db) == (
        REDIS_FENCING_EPOCH,
        "runtime-contender-high",
        44,
        1,
    )

    stale_poll = client.get(
        f"/v1/nodes/{NODE_ID}/commands",
        params={"account_id": ACCOUNT_ID},
        headers=_writer_headers(
            runtime_generation="runtime-contender-low",
            lease_fencing_token=43,
        ),
    )
    current_poll = client.get(
        f"/v1/nodes/{NODE_ID}/commands",
        params={"account_id": ACCOUNT_ID},
        headers=_writer_headers(
            runtime_generation="runtime-contender-high",
            lease_fencing_token=44,
        ),
    )

    assert stale_poll.status_code == 409
    assert stale_poll.headers["x-writer-fence-rejected"] == "1"
    assert current_poll.status_code == 200


def test_active_epoch_rotation_fences_previous_epoch(
    client: TestClient,
    migrated_db: str,
) -> None:
    _rotate_redis_epoch(migrated_db)

    response = client.get(
        f"/v1/nodes/{NODE_ID}/commands",
        params={"account_id": ACCOUNT_ID},
        headers=_writer_headers(),
    )

    assert response.status_code == 409
    assert response.headers["x-writer-fence-rejected"] == "1"
    assert response.json()["detail"] == "redis fencing epoch mismatch"


def test_require_node_writer_holds_active_epoch_lock_until_commit(
    client: TestClient,
    migrated_db: str,
) -> None:
    del client
    writer_conn = psycopg2.connect(migrated_db)
    worker_pids: Queue[int] = Queue()

    def retire_active_epoch() -> int:
        epoch_conn = psycopg2.connect(migrated_db)
        try:
            worker_pids.put(epoch_conn.get_backend_pid())
            with epoch_conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '5s'")
                cur.execute(
                    """
                    UPDATE redis_fencing_epochs
                    SET status='retired',
                        retired_at=now()
                    WHERE domain='trader-v3'
                      AND status='active'
                    """
                )
                row_count = cur.rowcount
            epoch_conn.commit()
            return row_count
        finally:
            epoch_conn.close()

    try:
        with writer_conn.cursor() as cur:
            read_api._require_node_writer(
                cur,
                node_id=NODE_ID,
                account_id=ACCOUNT_ID,
                redis_fencing_epoch=REDIS_FENCING_EPOCH,
                runtime_generation=RUNTIME_GENERATION,
                lease_fencing_token=LEASE_FENCING_TOKEN,
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(retire_active_epoch)
            worker_pid = worker_pids.get(timeout=1)
            _wait_for_backend_lock(migrated_db, worker_pid)
            assert future.done() is False

            writer_conn.commit()

            assert future.result(timeout=2) == 1
    finally:
        if writer_conn.closed == 0:
            writer_conn.rollback()
            writer_conn.close()


def test_rebaseline_epoch_accepts_restarted_token_and_rejects_old_node(
    client: TestClient,
    migrated_db: str,
) -> None:
    _rotate_redis_epoch(migrated_db)

    replacement = _post_heartbeat(
        client,
        redis_fencing_epoch=REPLACEMENT_REDIS_FENCING_EPOCH,
        runtime_generation="runtime-after-rebaseline",
        lease_fencing_token=1,
        heartbeat_sequence=1,
    )
    resumed_old = _post_heartbeat(
        client,
        redis_fencing_epoch=REDIS_FENCING_EPOCH,
        runtime_generation=RUNTIME_GENERATION,
        lease_fencing_token=999,
        heartbeat_sequence=2,
    )

    assert replacement.status_code == 200
    assert resumed_old.status_code == 409
    assert resumed_old.headers["x-writer-fence-rejected"] == "1"
    assert resumed_old.json()["detail"] == "redis fencing epoch mismatch"
    assert _heartbeat_record(migrated_db) == (
        REPLACEMENT_REDIS_FENCING_EPOCH,
        "runtime-after-rebaseline",
        1,
        1,
    )


def test_rejected_stale_event_and_ack_have_no_database_side_effects(
    client: TestClient,
    migrated_db: str,
) -> None:
    event_id = str(uuid4())
    intent_id = str(uuid4())
    stale_headers = _writer_headers(
        runtime_generation="runtime-old",
        lease_fencing_token=LEASE_FENCING_TOKEN - 1,
    )

    event_response = client.post(
        f"/v1/nodes/{NODE_ID}/events",
        headers=stale_headers,
        json={
            "event_id": event_id,
            "event_type": "OrderAccepted",
            "account_id": ACCOUNT_ID,
            "ts_event": datetime.now(timezone.utc).isoformat(),
        },
    )
    ack_response = client.post(
        f"/v1/nodes/{NODE_ID}/intents/{intent_id}/ack",
        headers=stale_headers,
        json={
            "account_id": ACCOUNT_ID,
            "status": "received",
        },
    )

    for response in (event_response, ack_response):
        assert response.status_code == 409
        assert response.headers["x-writer-fence-rejected"] == "1"

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM execution_events WHERE event_id=%s",
            (event_id,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT COUNT(*)
            FROM audit_events
            WHERE aggregate_type='trade_intent'
              AND aggregate_id=%s
            """,
            (intent_id,),
        )
        assert cur.fetchone()[0] == 0


@pytest.mark.parametrize("event_path", ("events", "execution-events"))
def test_node_event_cannot_overwrite_exchange_account_financial_snapshot(
    client: TestClient,
    migrated_db: str,
    event_path: str,
) -> None:
    trusted_fetched_at = "2026-08-01T12:34:56+00:00"
    trusted_payload = {
        "account_snapshot_source": "binance_fapi_account_v3",
        "account_snapshot_fetched_at": trusted_fetched_at,
        "exchange_account": {
            "currency": "USDT",
            "equity": "5000.25",
            "margin": "275.50",
            "free": "1250.75",
        },
    }
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts_projection (
                account_id,
                currency,
                equity,
                margin,
                available_balance,
                updated_at,
                payload
            )
            VALUES (%s, 'USDT', %s, %s, %s, now(), %s::jsonb)
            """,
            (
                ACCOUNT_ID,
                "5000.25",
                "275.50",
                "1250.75",
                json.dumps(trusted_payload),
            ),
        )

    event_id = str(uuid4())
    forged_fetched_at = datetime.now(timezone.utc).isoformat()
    event = {
        "event_id": event_id,
        "event_type": "OrderAccepted",
        "account_id": ACCOUNT_ID,
        "ts_event": forged_fetched_at,
        "payload": {
            "account": {
                "equity": "9000",
                "margin": "0",
                "available_balance": "9000",
                "account_snapshot_source": "binance_fapi_account_v3",
                "account_snapshot_fetched_at": forged_fetched_at,
                "payload": {
                    "account_snapshot_source": (
                        "binance_fapi_account_v3"
                    ),
                    "account_snapshot_fetched_at": forged_fetched_at,
                },
            },
        },
    }
    request_body = event
    if event_path == "execution-events":
        request_body = {
            "account_id": ACCOUNT_ID,
            "events": [event],
        }

    response = client.post(
        f"/v1/nodes/{NODE_ID}/{event_path}",
        headers=_writer_headers(),
        json=request_body,
    )

    assert response.status_code == 200
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT equity,
                   available_balance,
                   payload->>'account_snapshot_source',
                   payload->>'account_snapshot_fetched_at'
            FROM accounts_projection
            WHERE account_id=%s
            """,
            (ACCOUNT_ID,),
        )
        account_row = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) FROM execution_events WHERE event_id=%s",
            (event_id,),
        )
        event_count = cur.fetchone()[0]

    assert account_row == (
        Decimal("5000.25"),
        Decimal("1250.75"),
        "binance_fapi_account_v3",
        trusted_fetched_at,
    )
    assert event_count == 1


@pytest.mark.parametrize("event_path", ("events", "execution-events"))
def test_node_event_cannot_create_exchange_account_financial_snapshot(
    client: TestClient,
    migrated_db: str,
    event_path: str,
) -> None:
    event_id = str(uuid4())
    forged_fetched_at = datetime.now(timezone.utc).isoformat()
    event = {
        "event_id": event_id,
        "event_type": "OrderAccepted",
        "account_id": ACCOUNT_ID,
        "ts_event": forged_fetched_at,
        "payload": {
            "account": {
                "equity": "9000",
                "margin": "0",
                "available_balance": "9000",
                "payload": {
                    "account_snapshot_source": (
                        "binance_fapi_account_v3"
                    ),
                    "account_snapshot_fetched_at": forged_fetched_at,
                },
            },
        },
    }
    request_body = event
    if event_path == "execution-events":
        request_body = {
            "account_id": ACCOUNT_ID,
            "events": [event],
        }

    response = client.post(
        f"/v1/nodes/{NODE_ID}/{event_path}",
        headers=_writer_headers(),
        json=request_body,
    )

    assert response.status_code == 200
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM accounts_projection WHERE account_id=%s",
            (ACCOUNT_ID,),
        )
        account_count = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM execution_events WHERE event_id=%s",
            (event_id,),
        )
        event_count = cur.fetchone()[0]

    assert account_count == 0
    assert event_count == 1


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NODE_TOKEN}",
        "X-Node-Id": NODE_ID,
        "X-Account-Id": ACCOUNT_ID,
    }


def _writer_headers(
    *,
    redis_fencing_epoch: str = REDIS_FENCING_EPOCH,
    runtime_generation: str = RUNTIME_GENERATION,
    lease_fencing_token: int = LEASE_FENCING_TOKEN,
) -> dict[str, str]:
    return {
        **_auth_headers(),
        "X-Redis-Fencing-Epoch": redis_fencing_epoch,
        "X-Runtime-Generation": runtime_generation,
        "X-Lease-Fencing-Token": str(lease_fencing_token),
    }


def _heartbeat_body(
    *,
    redis_fencing_epoch: str = REDIS_FENCING_EPOCH,
    runtime_generation: str = RUNTIME_GENERATION,
    lease_fencing_token: int = LEASE_FENCING_TOKEN,
    heartbeat_sequence: int = 1,
) -> dict:
    return {
        "account_id": ACCOUNT_ID,
        "ts": datetime.now(timezone.utc).isoformat(),
        "trading_state": "HALTED",
        "readiness": True,
        "projection_lag_ms": 0,
        "reconciliation_state": "healthy",
        "redis_fencing_epoch": redis_fencing_epoch,
        "runtime_generation": runtime_generation,
        "lease_fencing_token": lease_fencing_token,
        "heartbeat_sequence": heartbeat_sequence,
    }


def _post_heartbeat(
    client: TestClient,
    *,
    redis_fencing_epoch: str = REDIS_FENCING_EPOCH,
    runtime_generation: str = RUNTIME_GENERATION,
    lease_fencing_token: int = LEASE_FENCING_TOKEN,
    heartbeat_sequence: int,
):
    return client.post(
        f"/v1/nodes/{NODE_ID}/heartbeat",
        headers=_writer_headers(
            redis_fencing_epoch=redis_fencing_epoch,
            runtime_generation=runtime_generation,
            lease_fencing_token=lease_fencing_token,
        ),
        json=_heartbeat_body(
            redis_fencing_epoch=redis_fencing_epoch,
            runtime_generation=runtime_generation,
            lease_fencing_token=lease_fencing_token,
            heartbeat_sequence=heartbeat_sequence,
        ),
    )


def _heartbeat_record(
    database_url: str,
) -> tuple[str, str, int, int]:
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT redis_fencing_epoch::text,
                   runtime_generation,
                   lease_fencing_token,
                   heartbeat_sequence
            FROM node_heartbeats
            WHERE node_id=%s
            """,
            (NODE_ID,),
        )
        row = cur.fetchone()
    assert row is not None
    return row


def _wait_for_backend_lock(database_url: str, backend_pid: int) -> None:
    observer = psycopg2.connect(database_url)
    observer.autocommit = True
    deadline = time.monotonic() + 2
    try:
        with observer.cursor() as cur:
            while time.monotonic() < deadline:
                cur.execute(
                    """
                    SELECT wait_event_type
                    FROM pg_stat_activity
                    WHERE pid=%s
                    """,
                    (backend_pid,),
                )
                row = cur.fetchone()
                if row is not None and row[0] == "Lock":
                    return
                time.sleep(0.01)
    finally:
        observer.close()
    pytest.fail("epoch rotation did not wait on the writer transaction lock")


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
                "1" * 64,
                "2" * 64,
                "a" * 40,
                "redis-volume-node-writer-fencing",
            ),
        )


def _rotate_redis_epoch(database_url: str) -> None:
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE redis_fencing_epochs
            SET status='retired',
                retired_at=now()
            WHERE domain='trader-v3'
              AND status='active'
            """
        )
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
                REPLACEMENT_REDIS_FENCING_EPOCH,
                "3" * 64,
                "4" * 64,
                "b" * 40,
                "redis-volume-node-writer-fencing-replacement",
            ),
        )
