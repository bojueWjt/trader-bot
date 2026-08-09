from __future__ import annotations

import json
from datetime import datetime, timezone

import psycopg2
import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import RealDictCursor

import read_api


NODE_ID = "nautilus-node-account-a"
ACCOUNT_ID = "account-a"
NODE_TOKEN = "node-a-token"
READER_TOKEN = "reader-token"


def test_heartbeat_identity_and_health_round_trip(
    migrated_db,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv(
        "NAUTILUS_NODE_AUTH_JSON",
        json.dumps(
            {
                NODE_ID: {
                    "account_id": ACCOUNT_ID,
                    "token": NODE_TOKEN,
                    "writer_id": "writer-account-a",
                    "lease_id": "lease-account-a",
                    "fencing_epoch": 42,
                }
            }
        ),
    )
    monkeypatch.setenv("SYSTEM_OBSERVER_TOKEN", READER_TOKEN)
    client = TestClient(read_api.app)
    heartbeat = {
        "account_id": ACCOUNT_ID,
        "ts": "2026-08-09T12:00:00+00:00",
        "trading_state": "HALTED",
        "readiness": False,
        "projection_lag_ms": 123,
        "reconciliation_state": "DEGRADED",
        "writer_id": "writer-account-a",
        "lease_id": "lease-account-a",
        "fencing_epoch": 42,
        "process_liveness": False,
        "loss_monitor_healthy": False,
        "loss_monitor_at": "2026-08-09T11:59:59+00:00",
    }

    posted = client.post(
        f"/v1/nodes/{NODE_ID}/heartbeat",
        json=heartbeat,
        headers={
            "Authorization": f"Bearer {NODE_TOKEN}",
            "X-Node-Id": NODE_ID,
            "X-Account-Id": ACCOUNT_ID,
        },
    )

    assert posted.status_code == 200

    fetched = client.get(
        "/v1/nodes",
        headers={
            "Authorization": f"Bearer {READER_TOKEN}",
        },
    )

    assert fetched.status_code == 200
    node = fetched.json()["nodes"][0]
    for field_name in (
        "writer_id",
        "lease_id",
        "fencing_epoch",
        "process_liveness",
        "loss_monitor_healthy",
        "loss_monitor_at",
    ):
        assert node[field_name] == heartbeat[field_name]


def test_heartbeat_rejects_identity_conflicting_with_auth_binding(
    migrated_db,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv(
        "NAUTILUS_NODE_AUTH_JSON",
        json.dumps(
            {
                NODE_ID: {
                    "account_id": ACCOUNT_ID,
                    "token": NODE_TOKEN,
                    "writer_id": "writer-account-a",
                    "lease_id": "lease-account-a",
                    "fencing_epoch": 42,
                }
            }
        ),
    )
    client = TestClient(read_api.app)

    response = client.post(
        f"/v1/nodes/{NODE_ID}/heartbeat",
        json={
            "account_id": ACCOUNT_ID,
            "trading_state": "HALTED",
            "writer_id": "writer-account-b",
        },
        headers={
            "Authorization": f"Bearer {NODE_TOKEN}",
            "X-Node-Id": NODE_ID,
            "X-Account-Id": ACCOUNT_ID,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "writer_identity_conflict"
    )


def test_heartbeat_rejects_non_boolean_health(
    migrated_db,
    monkeypatch,
) -> None:
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
    client = TestClient(read_api.app)

    response = client.post(
        f"/v1/nodes/{NODE_ID}/heartbeat",
        json={
            "account_id": ACCOUNT_ID,
            "trading_state": "HALTED",
            "process_liveness": "false",
        },
        headers={
            "Authorization": f"Bearer {NODE_TOKEN}",
            "X-Node-Id": NODE_ID,
            "X-Account-Id": ACCOUNT_ID,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "process_liveness must be boolean"
    )


def test_loss_monitor_merges_health_without_extending_node_liveness(
    migrated_db,
    monkeypatch,
) -> None:
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
    monkeypatch.setenv("SYSTEM_OBSERVER_TOKEN", READER_TOKEN)
    client = TestClient(read_api.app)
    headers = {
        "Authorization": f"Bearer {NODE_TOKEN}",
        "X-Node-Id": NODE_ID,
        "X-Account-Id": ACCOUNT_ID,
    }
    heartbeat = client.post(
        f"/v1/nodes/{NODE_ID}/heartbeat",
        json={
            "account_id": ACCOUNT_ID,
            "trading_state": "ACTIVE",
            "readiness": True,
            "projection_lag_ms": 12,
        },
        headers=headers,
    )
    assert heartbeat.status_code == 200

    pinned_last_seen_at = datetime(
        2026,
        8,
        9,
        12,
        0,
        tzinfo=timezone.utc,
    )
    conn = psycopg2.connect(migrated_db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE node_heartbeats SET last_seen_at=%s "
                "WHERE node_id=%s",
                (pinned_last_seen_at, NODE_ID),
            )
        conn.commit()
    finally:
        conn.close()

    monitor_at = "2026-08-09T12:00:01+00:00"
    published = client.post(
        f"/v1/nodes/{NODE_ID}/loss-monitor",
        json={
            "account_id": ACCOUNT_ID,
            "loss_monitor_healthy": False,
            "loss_monitor_at": monitor_at,
            "readiness": False,
            "unexpected": "ignored",
        },
        headers=headers,
    )

    assert published.status_code == 200
    conn = psycopg2.connect(migrated_db)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT payload, last_seen_at FROM node_heartbeats "
                "WHERE node_id=%s",
                (NODE_ID,),
            )
            row = dict(cur.fetchone())
    finally:
        conn.close()
    assert row["last_seen_at"] == pinned_last_seen_at
    assert row["payload"] == {
        "account_id": ACCOUNT_ID,
        "last_event_id": None,
        "loss_monitor_at": monitor_at,
        "loss_monitor_healthy": False,
        "open_orders": None,
        "process_liveness": None,
        "projection_lag_ms": 12,
        "readiness": True,
        "reconciliation_state": None,
        "ts": None,
    }

    fetched = client.get(
        "/v1/nodes",
        headers={
            "Authorization": f"Bearer {READER_TOKEN}",
        },
    )

    assert fetched.status_code == 200
    node = fetched.json()["nodes"][0]
    assert node["last_heartbeat_at"] == (
        pinned_last_seen_at.isoformat()
    )
    assert node["loss_monitor_healthy"] is False
    assert node["loss_monitor_at"] == monitor_at


def test_ordinary_heartbeat_preserves_explicit_loss_monitor_failure(
    migrated_db,
    monkeypatch,
) -> None:
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
    monkeypatch.setenv("SYSTEM_OBSERVER_TOKEN", READER_TOKEN)
    client = TestClient(read_api.app)
    headers = {
        "Authorization": f"Bearer {NODE_TOKEN}",
        "X-Node-Id": NODE_ID,
        "X-Account-Id": ACCOUNT_ID,
    }

    heartbeat = client.post(
        f"/v1/nodes/{NODE_ID}/heartbeat",
        json={
            "account_id": ACCOUNT_ID,
            "trading_state": "ACTIVE",
            "process_liveness": True,
            "loss_monitor_healthy": None,
            "loss_monitor_at": None,
        },
        headers=headers,
    )
    assert heartbeat.status_code == 200

    monitor_at = "2026-08-09T12:00:01+00:00"
    published = client.post(
        f"/v1/nodes/{NODE_ID}/loss-monitor",
        json={
            "account_id": ACCOUNT_ID,
            "loss_monitor_healthy": False,
            "loss_monitor_at": monitor_at,
        },
        headers=headers,
    )
    assert published.status_code == 200

    later_heartbeat = client.post(
        f"/v1/nodes/{NODE_ID}/heartbeat",
        json={
            "account_id": ACCOUNT_ID,
            "trading_state": "ACTIVE",
            "process_liveness": True,
            "loss_monitor_healthy": None,
            "loss_monitor_at": None,
        },
        headers=headers,
    )
    assert later_heartbeat.status_code == 200

    fetched = client.get(
        "/v1/nodes",
        headers={"Authorization": f"Bearer {READER_TOKEN}"},
    )
    assert fetched.status_code == 200
    node = fetched.json()["nodes"][0]
    assert node["process_liveness"] is True
    assert node["loss_monitor_healthy"] is False
    assert node["loss_monitor_at"] == monitor_at


@pytest.mark.parametrize(
    "invalid_health",
    (
        None,
        0,
        1,
        "false",
        [],
        {},
    ),
)
def test_loss_monitor_rejects_non_boolean_health(
    migrated_db,
    monkeypatch,
    invalid_health,
) -> None:
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
    client = TestClient(read_api.app)

    response = client.post(
        f"/v1/nodes/{NODE_ID}/loss-monitor",
        json={
            "account_id": ACCOUNT_ID,
            "loss_monitor_healthy": invalid_health,
        },
        headers={
            "Authorization": f"Bearer {NODE_TOKEN}",
            "X-Node-Id": NODE_ID,
            "X-Account-Id": ACCOUNT_ID,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "loss_monitor_healthy must be boolean"
    )


def test_loss_monitor_rejects_identity_mismatch(
    migrated_db,
    monkeypatch,
) -> None:
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
    client = TestClient(read_api.app)

    response = client.post(
        f"/v1/nodes/{NODE_ID}/loss-monitor",
        json={
            "account_id": "account-b",
            "loss_monitor_healthy": True,
        },
        headers={
            "Authorization": f"Bearer {NODE_TOKEN}",
            "X-Node-Id": NODE_ID,
            "X-Account-Id": "account-b",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "writer_identity_conflict"
    )


def test_loss_monitor_without_heartbeat_is_publish_unavailable(
    migrated_db,
    monkeypatch,
) -> None:
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
    client = TestClient(read_api.app)

    response = client.post(
        f"/v1/nodes/{NODE_ID}/loss-monitor",
        json={
            "account_id": ACCOUNT_ID,
            "loss_monitor_healthy": True,
        },
        headers={
            "Authorization": f"Bearer {NODE_TOKEN}",
            "X-Node-Id": NODE_ID,
            "X-Account-Id": ACCOUNT_ID,
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "node heartbeat unavailable"
    )


def test_loss_monitor_commit_failure_returns_durable_error(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")
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
    conn = _CommitFailureConnection()
    monkeypatch.setattr(
        read_api.psycopg2,
        "connect",
        lambda _database_url: conn,
    )
    client = TestClient(read_api.app)

    response = client.post(
        f"/v1/nodes/{NODE_ID}/loss-monitor",
        json={
            "account_id": ACCOUNT_ID,
            "loss_monitor_healthy": True,
            "loss_monitor_at": "2026-08-09T12:00:01+00:00",
        },
        headers={
            "Authorization": f"Bearer {NODE_TOKEN}",
            "X-Node-Id": NODE_ID,
            "X-Account-Id": ACCOUNT_ID,
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "loss monitor write failed"
    assert conn.rollback_count == 1
    assert conn.close_count == 1


class _CommitFailureCursor:
    rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params) -> None:
        return None


class _CommitFailureConnection:
    def __init__(self) -> None:
        self.rollback_count = 0
        self.close_count = 0

    def cursor(self):
        return _CommitFailureCursor()

    def commit(self) -> None:
        raise psycopg2.OperationalError("commit failed")

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.close_count += 1
