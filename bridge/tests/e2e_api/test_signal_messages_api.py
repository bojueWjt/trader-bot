from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.signal_parser import parse_signal
from app.services.signal_store import SignalStore
from app.signals.router import reset_message_processing_store, router as signals_router


def auth_headers(role="trader"):
    token = "test-trader-token"
    if role == "viewer":
        token = "test-viewer-token"
    if role == "risk_admin":
        token = "test-risk-admin-token"
    return {"authorization": f"Bearer {token}"}


def build_client():
    reset_message_processing_store()
    app = FastAPI()
    app.include_router(signals_router, prefix="/api/signals")
    return TestClient(app)


def build_client_with_signal(media):
    client = build_client()
    store = SignalStore()
    store.upsert_signal(
        parse_signal(
            {
                "signal_id": "sig-media-1",
                "source": "telegram",
                "source_channel_id": "-1001",
                "source_channel_name": "coinAlert",
                "source_message_id": "1",
                "received_at": "2026-06-10T10:00:00Z",
                "raw_text": "BTCUSDT LONG Entry: 70000 SL: 69000 TP: 72000",
                "media": media,
            },
            pair_whitelist={"BTC/USDT:USDT"},
        )
    )
    client.app.state.signal_store = store
    return client


def test_status_endpoint_still_mounted():
    client = build_client()

    response = client.get("/api/signals/status")

    assert response.status_code == 200
    assert response.json() == {"status": "mounted"}


def test_post_messages_creates_received_record():
    client = build_client()

    response = client.post(
        "/api/signals/messages",
        headers=auth_headers(),
        json={
            "message_id": "msg-001",
            "channel_id": "chan-a",
            "signal_id": "sig-a",
            "metadata": {"pair": "BTC/USDT"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["message_id"] == "msg-001"
    assert body["source"] == "telegram"
    assert body["channel_id"] == "chan-a"
    assert body["signal_id"] == "sig-a"
    assert body["status"] == "received"
    assert body["metadata"] == {"pair": "BTC/USDT"}
    assert body["created_at"]
    assert body["updated_at"]


def test_duplicate_post_preserves_lifecycle_status_and_updates_identity_fields():
    client = build_client()
    client.post(
        "/api/signals/messages",
        headers=auth_headers(),
        json={"message_id": "msg-dup", "signal_id": "sig-old"},
    )
    client.post(
        "/api/signals/messages/msg-dup/status",
        headers=auth_headers(),
        json={"status": "db_saved"},
    )

    response = client.post(
        "/api/signals/messages",
        headers=auth_headers(),
        json={
            "message_id": "msg-dup",
            "source": "hermes",
            "channel_id": "chan-new",
            "signal_id": "sig-new",
            "metadata": {"ignored": True},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "db_saved"
    assert body["source"] == "hermes"
    assert body["channel_id"] == "chan-new"
    assert body["signal_id"] == "sig-new"


def test_status_transition_persists_cron_job_id_in_get_record():
    client = build_client()
    client.post("/api/signals/messages", headers=auth_headers(), json={"message_id": "msg-cron"})
    client.post(
        "/api/signals/messages/msg-cron/status",
        headers=auth_headers(),
        json={"status": "db_saved"},
    )

    response = client.post(
        "/api/signals/messages/msg-cron/status",
        headers=auth_headers(),
        json={
            "status": "cron_created",
            "actor": "watcher",
            "reason": "scheduled",
            "cron_job_id": "cron-123",
        },
    )
    get_response = client.get("/api/signals/messages/msg-cron", headers=auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message_id"] == "msg-cron"
    assert body["status"] == "cron_created"
    assert body["reason"] == "scheduled"
    assert body["record"]["cron_job_id"] == "cron-123"
    assert get_response.json()["cron_job_id"] == "cron-123"


def test_invalid_transition_returns_422_with_result_reason():
    client = build_client()
    client.post(
        "/api/signals/messages",
        headers=auth_headers(),
        json={"message_id": "msg-invalid"},
    )

    response = client.post(
        "/api/signals/messages/msg-invalid/status",
        headers=auth_headers(),
        json={"status": "cron_created"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["result"]["ok"] is False
    assert body["result"]["message_id"] == "msg-invalid"
    assert body["result"]["status"] == "received"
    assert body["result"]["reason"] == "invalid_transition"


def test_get_missing_message_returns_404():
    client = build_client()

    response = client.get("/api/signals/messages/missing", headers=auth_headers())

    assert response.status_code == 404


def test_events_endpoint_returns_ordered_event_status_strings():
    client = build_client()
    client.post("/api/signals/messages", headers=auth_headers(), json={"message_id": "msg-events"})
    client.post(
        "/api/signals/messages/msg-events/status",
        headers=auth_headers(),
        json={"status": "db_saved"},
    )
    client.post(
        "/api/signals/messages/msg-events/status",
        headers=auth_headers(),
        json={"status": "cron_created"},
    )

    response = client.get("/api/signals/messages/msg-events/events", headers=auth_headers())

    assert response.status_code == 200
    events = response.json()["events"]
    assert [event["to_status"] for event in events] == ["received", "db_saved", "cron_created"]
    assert events[0]["from_status"] is False
    assert events[1]["from_status"] == "received"
    assert events[2]["from_status"] == "db_saved"


def test_signal_id_message_events_filters_events_by_signal():
    client = build_client()
    client.post(
        "/api/signals/messages",
        headers=auth_headers(),
        json={"message_id": "msg-a", "signal_id": "sig-filter"},
    )
    client.post(
        "/api/signals/messages",
        headers=auth_headers(),
        json={"message_id": "msg-b", "signal_id": "sig-other"},
    )
    client.post(
        "/api/signals/messages/msg-a/status",
        headers=auth_headers(),
        json={"status": "db_saved", "actor": "watcher"},
    )
    client.post(
        "/api/signals/messages/msg-b/status",
        headers=auth_headers(),
        json={"status": "db_saved", "actor": "watcher"},
    )

    response = client.get("/api/signals/sig-filter/message-events", headers=auth_headers())

    assert response.status_code == 200
    events = response.json()["events"]
    assert [event["message_id"] for event in events] == ["msg-a", "msg-a"]
    assert [event["to_status"] for event in events] == ["received", "db_saved"]


def test_write_endpoints_require_trader_or_risk_admin():
    client = build_client()

    missing_token = client.post("/api/signals/messages", json={"message_id": "msg-auth"})
    viewer_write = client.post(
        "/api/signals/messages",
        headers=auth_headers(role="viewer"),
        json={"message_id": "msg-auth"},
    )
    risk_admin_write = client.post(
        "/api/signals/messages",
        headers=auth_headers(role="risk_admin"),
        json={"message_id": "msg-auth"},
    )
    viewer_transition = client.post(
        "/api/signals/messages/msg-auth/status",
        headers=auth_headers(role="viewer"),
        json={"status": "db_saved"},
    )

    assert missing_token.status_code == 401
    assert viewer_write.status_code == 403
    assert risk_admin_write.status_code == 200
    assert viewer_transition.status_code == 403


def test_write_endpoints_auth_boundary_runs_before_empty_body_validation():
    client = build_client()

    missing_token_write = client.post("/api/signals/messages")
    missing_token_transition = client.post("/api/signals/messages/m1/status")
    viewer_write = client.post("/api/signals/messages", headers=auth_headers(role="viewer"))
    viewer_transition = client.post(
        "/api/signals/messages/m1/status",
        headers=auth_headers(role="viewer"),
    )

    assert missing_token_write.status_code == 401
    assert missing_token_transition.status_code == 401
    assert viewer_write.status_code == 403
    assert viewer_transition.status_code == 403


def test_message_record_and_event_reads_require_login_but_allow_viewer():
    client = build_client()
    client.post(
        "/api/signals/messages",
        headers=auth_headers(),
        json={"message_id": "msg-readable", "signal_id": "sig-readable"},
    )

    missing_record = client.get("/api/signals/messages/msg-readable")
    viewer_record = client.get(
        "/api/signals/messages/msg-readable",
        headers=auth_headers(role="viewer"),
    )
    missing_events = client.get("/api/signals/messages/msg-readable/events")
    viewer_events = client.get(
        "/api/signals/messages/msg-readable/events",
        headers=auth_headers(role="viewer"),
    )
    viewer_signal_events = client.get(
        "/api/signals/sig-readable/message-events",
        headers=auth_headers(role="viewer"),
    )

    assert missing_record.status_code == 401
    assert viewer_record.status_code == 200
    assert missing_events.status_code == 401
    assert viewer_events.status_code == 200
    assert viewer_signal_events.status_code == 200


def test_signal_media_endpoint_streams_file_with_declared_mime_type(tmp_path, monkeypatch):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    image_path = media_dir / "signal-photo.jpg"
    image_path.write_bytes(b"fake-jpeg-bytes")
    monkeypatch.setenv("SIGNAL_MEDIA_DIRS", f"{tmp_path / 'missing'}:{media_dir}")
    client = build_client_with_signal(
        [
            {
                "mime_type": "image/jpeg",
                "path": "/source-machine/absolute/signal-photo.jpg",
                "type": "photo",
            }
        ]
    )

    response = client.get("/api/signals/sig-media-1/media/0", headers=auth_headers(role="viewer"))

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == b"fake-jpeg-bytes"


def test_signal_media_endpoint_returns_json_404_when_file_missing(tmp_path, monkeypatch):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    monkeypatch.setenv("SIGNAL_MEDIA_DIRS", str(media_dir))
    client = build_client_with_signal(
        [
            {
                "mime_type": "image/jpeg",
                "path": "/source-machine/absolute/missing-photo.jpg",
                "type": "photo",
            }
        ]
    )

    response = client.get("/api/signals/sig-media-1/media/0", headers=auth_headers(role="viewer"))

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "media missing"}


def test_signal_media_endpoint_uses_basename_for_path_traversal_payload(tmp_path, monkeypatch):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    monkeypatch.setenv("SIGNAL_MEDIA_DIRS", str(media_dir))
    client = build_client_with_signal(
        [
            {
                "mime_type": "image/jpeg",
                "path": "../../../etc/passwd",
                "type": "photo",
            }
        ]
    )

    response = client.get("/api/signals/sig-media-1/media/0", headers=auth_headers(role="viewer"))

    assert response.status_code == 404
    assert response.json() == {"detail": "media missing"}


def test_malformed_message_payloads_return_422():
    client = build_client()

    cases = [
        [],
        {"message_id": 123},
        {"message_id": ["x"]},
        {"message_id": "msg-bad-metadata", "metadata": []},
    ]
    for payload in cases:
        response = client.post(
            "/api/signals/messages",
            headers=auth_headers(),
            json=payload,
        )

        assert response.status_code == 422


def test_malformed_status_payloads_return_422():
    client = build_client()
    client.post("/api/signals/messages", headers=auth_headers(), json={"message_id": "msg-status"})

    cases = [
        [],
        {"status": 123},
        {"status": ["db_saved"]},
        {"status": "missing_status"},
        {"status": "db_saved", "actor": 123},
    ]
    for payload in cases:
        response = client.post(
            "/api/signals/messages/msg-status/status",
            headers=auth_headers(),
            json=payload,
        )

        assert response.status_code == 422


def test_create_app_uses_sqlite_message_store_across_instances(monkeypatch, tmp_path):
    database_path = tmp_path / "signals.db"
    monkeypatch.setenv("HERMES_SIGNAL_STORE_URL", f"sqlite:///{database_path}")

    app1 = create_app()
    client1 = TestClient(app1)
    created = client1.post(
        "/api/signals/messages",
        headers=auth_headers(),
        json={
            "message_id": "msg-persisted",
            "signal_id": "sig-persisted",
            "metadata": {"pair": "ETH/USDT"},
        },
    )

    app2 = create_app()
    client2 = TestClient(app2)
    loaded = client2.get("/api/signals/messages/msg-persisted", headers=auth_headers())

    assert created.status_code == 200
    assert loaded.status_code == 200
    assert loaded.json()["message_id"] == "msg-persisted"
    assert loaded.json()["metadata"] == {"pair": "ETH/USDT"}
