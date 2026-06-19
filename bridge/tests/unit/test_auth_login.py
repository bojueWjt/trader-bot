from fastapi.testclient import TestClient

from app.main import create_app


def build_client() -> TestClient:
    return TestClient(create_app())


def clear_auth_env(monkeypatch):
    for name in (
        "AUTH_USERNAME",
        "AUTH_PASSWORD",
        "AUTH_ROLE",
        "AUTH_CREDENTIALS",
        "AUTH_SECRET_KEY",
        "AUTH_TOKEN_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def configure_password_login(monkeypatch, *, ttl: str = "3600", role: str = "risk_admin") -> None:
    clear_auth_env(monkeypatch)
    monkeypatch.setenv("AUTH_USERNAME", "admin")
    monkeypatch.setenv("AUTH_PASSWORD", "correct-password")
    monkeypatch.setenv("AUTH_ROLE", role)
    monkeypatch.setenv("AUTH_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("AUTH_TOKEN_TTL_SECONDS", ttl)


def test_login_returns_422_when_credentials_are_not_configured(monkeypatch):
    clear_auth_env(monkeypatch)
    monkeypatch.setenv("AUTH_SECRET_KEY", "unit-test-secret-key")
    client = build_client()

    response = client.post("/auth/login", json={"username": "admin", "password": "correct-password"})

    assert response.status_code == 422


def test_login_returns_401_for_wrong_credentials(monkeypatch):
    configure_password_login(monkeypatch)
    client = build_client()

    response = client.post("/auth/login", json={"username": "admin", "password": "wrong-password"})

    assert response.status_code == 401


def test_login_returns_bearer_token_for_valid_credentials(monkeypatch):
    configure_password_login(monkeypatch, role="reviewer")
    client = build_client()

    response = client.post("/auth/login", json={"username": "admin", "password": "correct-password"})

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert body["role"] == "reviewer"
    assert isinstance(body["access_token"], str)
    assert body["access_token"].count(".") == 2


def test_login_token_is_accepted_by_protected_endpoint(monkeypatch):
    configure_password_login(monkeypatch, role="system_observer")
    client = build_client()
    login = client.post("/api/auth/login", json={"username": "admin", "password": "correct-password"})
    token = login.json()["access_token"]

    response = client.get("/api/risk/overview", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


def test_expired_and_invalid_login_tokens_return_401(monkeypatch):
    configure_password_login(monkeypatch, ttl="-1")
    client = build_client()
    login = client.post("/auth/login", json={"username": "admin", "password": "correct-password"})
    expired_token = login.json()["access_token"]

    expired = client.get("/api/risk/overview", headers={"Authorization": f"Bearer {expired_token}"})
    invalid = client.get("/api/risk/overview", headers={"Authorization": "Bearer invalid.jwt.token"})

    assert expired.status_code == 401
    assert invalid.status_code == 401
