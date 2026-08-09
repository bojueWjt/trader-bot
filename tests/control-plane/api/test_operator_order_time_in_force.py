from __future__ import annotations

from uuid import UUID

import psycopg2
import pytest
from fastapi.testclient import TestClient

import read_api


RISK_ADMIN_TOKEN = "operator-time-in-force-token"
NODE_TOKEN = "operator-time-in-force-node-token"
ACCOUNT_ID = "account-a"
NODE_ID = "nautilus-node-account-a"
SUPPORTED_TIME_IN_FORCE = ("GTC", "IOC", "FOK", "GTD")
EXPLICIT_INTENT_ID = "123e4567-e89b-12d3-a456-426614174000"


def test_limit_ioc_persists_previews_downlinks_and_replays_stably(
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app)
    body = _open_position_body(
        client_ref="operator-limit-ioc-stable-replay",
        intent_id=EXPLICIT_INTENT_ID,
        time_in_force="IOC",
    )

    dry_run_body = dict(body)
    dry_run_body["dry_run"] = True
    previewed = client.post(
        "/v1/operator/orders",
        json=dry_run_body,
        headers=_operator_headers(),
    )

    assert previewed.status_code == 200, previewed.text
    previewed_payload = previewed.json()
    assert previewed_payload["intent_id"] == EXPLICIT_INTENT_ID
    assert previewed_payload["order_plan_preview"]["entry"][
        "time_in_force"
    ] == "IOC"
    assert previewed_payload["execution_preview"]["type"] == "limit"
    assert previewed_payload["execution_preview"]["time_in_force"] == "IOC"
    assert _intent_count(migrated_db) == 0

    placed = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )

    assert placed.status_code == 200, placed.text
    placed_payload = placed.json()
    assert placed_payload["replay"] is False
    assert placed_payload["intent_id"] == EXPLICIT_INTENT_ID
    assert placed_payload["order_plan"]["entry"]["time_in_force"] == "IOC"
    assert placed_payload["execution_preview"]["type"] == "limit"
    assert placed_payload["execution_preview"]["time_in_force"] == "IOC"

    persisted_plan = _persisted_order_plan(
        migrated_db,
        placed_payload["intent_id"],
    )
    assert persisted_plan["entry"]["time_in_force"] == "IOC"

    downlink = client.get(
        f"/v1/nodes/{NODE_ID}/intents",
        params={"account_id": ACCOUNT_ID},
        headers={"Authorization": f"Bearer {NODE_TOKEN}"},
    )

    assert downlink.status_code == 200, downlink.text
    items = downlink.json()["items"]
    assert len(items) == 1
    execution_plan = items[0]["intent"]["order_plan"]
    assert execution_plan["type"] == "limit"
    assert execution_plan["time_in_force"] == "IOC"

    replayed = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )

    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["replay"] is True
    assert replayed.json()["intent_id"] == placed_payload["intent_id"]
    assert _intent_count(migrated_db) == 1
    assert _persisted_order_plan(
        migrated_db,
        placed_payload["intent_id"],
    ) == persisted_plan

    replay_downlink = client.get(
        f"/v1/nodes/{NODE_ID}/intents",
        params={"account_id": ACCOUNT_ID},
        headers={"Authorization": f"Bearer {NODE_TOKEN}"},
    )
    assert replay_downlink.status_code == 200, replay_downlink.text
    replay_execution_plan = replay_downlink.json()["items"][0]["intent"][
        "order_plan"
    ]
    assert replay_execution_plan == execution_plan


def test_explicit_intent_id_rejects_other_idempotency_binding(
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app)
    first = client.post(
        "/v1/operator/orders",
        json=_open_position_body(
            client_ref="operator-explicit-id-first-binding",
            intent_id=EXPLICIT_INTENT_ID,
            time_in_force="IOC",
        ),
        headers=_operator_headers(),
    )
    assert first.status_code == 200, first.text

    occupied = client.post(
        "/v1/operator/orders",
        json=_open_position_body(
            client_ref="operator-explicit-id-other-binding",
            intent_id=EXPLICIT_INTENT_ID,
            time_in_force="IOC",
        ),
        headers=_operator_headers(),
    )

    assert occupied.status_code == 409
    assert occupied.json()["detail"] == (
        "intent_id is already bound to a different idempotency key"
    )
    assert _intent_count(migrated_db) == 1


def test_idempotency_rejects_different_explicit_intent_id(
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app)
    body = _open_position_body(
        client_ref="operator-explicit-id-idempotency-binding",
        intent_id=EXPLICIT_INTENT_ID,
        time_in_force="IOC",
    )
    first = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )
    assert first.status_code == 200, first.text

    rebound_body = dict(body)
    rebound_body["intent_id"] = "223e4567-e89b-12d3-a456-426614174000"
    rebound = client.post(
        "/v1/operator/orders",
        json=rebound_body,
        headers=_operator_headers(),
    )

    assert rebound.status_code == 409
    assert rebound.json()["detail"] == (
        "idempotency key is already bound to a different intent_id"
    )
    assert _intent_count(migrated_db) == 1


@pytest.mark.parametrize("intent_id", ("", "not-a-uuid", None, 1))
def test_explicit_intent_id_requires_uuid(
    intent_id,
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app)

    response = client.post(
        "/v1/operator/orders",
        json=_open_position_body(
            client_ref="operator-invalid-explicit-intent-id",
            intent_id=intent_id,
            time_in_force="IOC",
        ),
        headers=_operator_headers(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "intent_id must be a uuid"


@pytest.mark.parametrize("time_in_force", SUPPORTED_TIME_IN_FORCE)
def test_open_position_accepts_supported_uppercase_time_in_force(
    time_in_force,
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app)
    body = _open_position_body(
        client_ref=f"operator-supported-tif-{time_in_force.lower()}",
        time_in_force=time_in_force,
    )
    body["dry_run"] = True

    response = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )

    assert response.status_code == 200, response.text
    entry = response.json()["order_plan_preview"]["entry"]
    assert entry["time_in_force"] == time_in_force


@pytest.mark.parametrize(
    "time_in_force",
    ("ioc", "Ioc", " IOC", "IOC ", "DAY", "", None, 1),
)
def test_open_position_rejects_non_contract_time_in_force(
    time_in_force,
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app)

    response = client.post(
        "/v1/operator/orders",
        json=_open_position_body(
            client_ref="operator-invalid-time-in-force",
            time_in_force=time_in_force,
        ),
        headers=_operator_headers(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "entry.time_in_force must be one of ['GTC', 'IOC', 'FOK', 'GTD']"
    )


def test_open_position_omitted_time_in_force_keeps_legacy_defaults(
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app)
    body = _open_position_body(
        client_ref="operator-limit-default-time-in-force",
    )

    response = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    UUID(payload["intent_id"])
    assert "time_in_force" not in payload["order_plan"]["entry"]
    assert payload["execution_preview"]["time_in_force"] == "GTC"


def _open_position_body(
    *,
    client_ref: str,
    intent_id=...,
    time_in_force=...,
) -> dict:
    entry = {
        "type": "limit",
        "price": 100.0,
    }
    if time_in_force is not ...:
        entry["time_in_force"] = time_in_force
    body = {
        "action": "open_position",
        "account_id": ACCOUNT_ID,
        "symbol": "BTCUSDT",
        "side": "long",
        "entry": entry,
        "stop_loss": 90.0,
        "notional_usdt": 100.0,
        "reason": "operator time-in-force contract test",
        "client_ref": client_ref,
    }
    if intent_id is not ...:
        body["intent_id"] = intent_id
    return body


def _configure(monkeypatch, database_url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("RISK_ADMIN_TOKEN", RISK_ADMIN_TOKEN)
    monkeypatch.setenv("NAUTILUS_NODE_TOKEN", NODE_TOKEN)
    monkeypatch.delenv("NAUTILUS_NODE_AUTH_JSON", raising=False)
    monkeypatch.setenv("OPERATOR_EQUITY_ACCOUNT_A", "1000")


def _operator_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {RISK_ADMIN_TOKEN}",
        "X-Request-Id": "operator-time-in-force-contract",
    }


def _persisted_order_plan(database_url: str, intent_id: str) -> dict:
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_plan FROM trade_intents WHERE intent_id::text=%s",
                (intent_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0]


def _intent_count(database_url: str) -> int:
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM trade_intents")
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])
