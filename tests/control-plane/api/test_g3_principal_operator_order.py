import pytest
from fastapi.testclient import TestClient

import read_api
from security.principal import SIGNAL_TOKEN_ENV


@pytest.fixture()
def client(monkeypatch):
    for key in (*read_api.READER_TOKEN_ENV, *SIGNAL_TOKEN_ENV,
                "NAUTILUS_NODE_TOKEN", "NAUTILUS_NODE_AUTH_JSON"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RISK_ADMIN_TOKEN", "operator-token")
    monkeypatch.setenv("VIEWER_TOKEN", "viewer-token")
    monkeypatch.setenv("SIGNAL_TOKEN_ACCOUNT_A", "signal-token")
    return TestClient(read_api.app)


def test_signal_cannot_forge_human_authority_or_write_without_claim(client):
    body = {"action": "close_position", "account_id": "account-a", "symbol": "BTCUSDT",
            "actor": "risk_admin", "authorized_by_type": "user", "authorized_by_id": "risk_admin"}
    response = client.post("/v1/operator/orders", json=body,
                           headers={"Authorization": "Bearer signal-token"})
    assert response.status_code == 403
    assert response.json()["detail"] == "signal_claim_channel_authority_required"
    body["account_id"] = "account-b"
    response = client.post("/v1/operator/orders", json=body,
                           headers={"Authorization": "Bearer signal-token"})
    assert response.status_code == 403
    assert "other accounts" in response.json()["detail"]


@pytest.mark.parametrize("path", ["/v1/incidents", "/v1/intents/00000000-0000-0000-0000-000000000001/trace",
                                 "/api/system/snapshot"])
def test_signal_cannot_read_global_data(client, path):
    response = client.get(path, headers={"Authorization": "Bearer signal-token"})
    assert response.status_code == 403


def test_readonly_credential_cannot_submit_operator_orders(client):
    response = client.post("/v1/operator/orders", json={},
                           headers={"Authorization": "Bearer viewer-token"})
    assert response.status_code == 403


def test_bad_catalog_does_not_downgrade_signal_to_operator(client, monkeypatch):
    monkeypatch.setenv("RISK_ADMIN_TOKEN", "signal-token")
    response = client.post("/v1/operator/orders", json={"account_id": "account-a"},
                           headers={"Authorization": "Bearer signal-token"})
    assert response.status_code == 503
