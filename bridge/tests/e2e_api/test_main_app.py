from fastapi.testclient import TestClient

from app.main import create_app


def build_client():
    return TestClient(create_app())


def test_main_app_mounts_bridge_routers():
    client = build_client()

    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/api/signals/status").json() == {"status": "mounted"}
    assert client.get("/api/dashboard/overview").status_code == 401
    assert client.get("/api/reports/status").json() == {"status": "mounted"}
    assert client.get("/api/risk/overview").status_code == 401
    assert client.get("/api/audit/events").status_code == 401
    assert client.post("/api/security/scan-report", json={"text": ""}).status_code == 401
    assert client.get("/api/release-gates/status").status_code == 401


def test_request_id_middleware_echoes_or_generates_id():
    client = build_client()

    provided = client.get("/healthz", headers={"x-request-id": "req-123"})
    generated = client.get("/healthz")

    assert provided.headers["x-request-id"] == "req-123"
    assert generated.headers["x-request-id"]
