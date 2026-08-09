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


def test_limit_ioc_preserves_explicit_quantity_end_to_end(
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app)
    body = _open_position_body(
        client_ref="operator-limit-ioc-explicit-quantity",
        intent_id=EXPLICIT_INTENT_ID,
        time_in_force="IOC",
    )
    body["symbol"] = "SOLUSDT"
    body["quantity"] = "0.07"
    body["entry"]["price"] = 77.28
    body["stop_loss"] = 70
    body["notional_usdt"] = 5.4096

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
    assert previewed_payload["order_plan_preview"]["quantity"] == "0.07"
    assert previewed_payload["execution_preview"]["quantity"] == "0.07"
    assert previewed_payload["execution_preview"]["price"] == 77.28
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
    assert placed_payload["order_plan"]["quantity"] == "0.07"
    assert placed_payload["execution_preview"]["quantity"] == "0.07"

    persisted_plan = _persisted_order_plan(
        migrated_db,
        placed_payload["intent_id"],
    )
    assert persisted_plan["quantity"] == "0.07"

    downlink = client.get(
        f"/v1/nodes/{NODE_ID}/intents",
        params={"account_id": ACCOUNT_ID},
        headers={"Authorization": f"Bearer {NODE_TOKEN}"},
    )

    assert downlink.status_code == 200, downlink.text
    items = downlink.json()["items"]
    assert len(items) == 1
    execution_plan = items[0]["intent"]["order_plan"]
    assert execution_plan["quantity"] == "0.07"

    replayed = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )

    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["replay"] is True
    assert replayed.json()["intent_id"] == EXPLICIT_INTENT_ID
    assert _intent_count(migrated_db) == 1
    replayed_persisted_plan = _persisted_order_plan(
        migrated_db,
        EXPLICIT_INTENT_ID,
    )
    assert replayed_persisted_plan["quantity"] == "0.07"
    assert replayed_persisted_plan == persisted_plan

    replay_downlink = client.get(
        f"/v1/nodes/{NODE_ID}/intents",
        params={"account_id": ACCOUNT_ID},
        headers={"Authorization": f"Bearer {NODE_TOKEN}"},
    )

    assert replay_downlink.status_code == 200, replay_downlink.text
    replay_execution_plan = replay_downlink.json()["items"][0]["intent"][
        "order_plan"
    ]
    assert replay_execution_plan["quantity"] == "0.07"
    assert replay_execution_plan == execution_plan


def test_open_position_replay_rejects_trade_semantic_drift(
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app)
    body = _open_position_body(
        client_ref="operator-replay-trade-semantics",
        intent_id=EXPLICIT_INTENT_ID,
        time_in_force="IOC",
    )
    body["symbol"] = "SOLUSDT"
    body["quantity"] = "0.07"
    body["entry"]["price"] = 77.28
    body["stop_loss"] = 70
    body["notional_usdt"] = 12

    placed = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )

    assert placed.status_code == 200, placed.text
    persisted_plan = _persisted_order_plan(
        migrated_db,
        EXPLICIT_INTENT_ID,
    )

    drifted_requests = [
        {**body, "quantity": "0.06"},
        {
            **body,
            "entry": {
                **body["entry"],
                "price": 77.27,
            },
        },
        {**body, "symbol": "BTCUSDT"},
        {**body, "notional_usdt": 11},
        {
            **body,
            "entry": {
                **body["entry"],
                "time_in_force": "GTC",
            },
        },
    ]
    for drifted_body in drifted_requests:
        replayed = client.post(
            "/v1/operator/orders",
            json=drifted_body,
            headers=_operator_headers(),
        )

        assert replayed.status_code == 409, replayed.text
        assert replayed.json()["detail"] == (
            "idempotency key trade semantics mismatch"
        )

    assert _intent_count(migrated_db) == 1
    assert _persisted_order_plan(
        migrated_db,
        EXPLICIT_INTENT_ID,
    ) == persisted_plan


def test_management_replay_rejects_target_position_id_drift(
    tmp_path,
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    monkeypatch.setenv(
        "ATTRIBUTION_SHADOW_LOG",
        str(tmp_path / "attribution-shadow.jsonl"),
    )
    client = TestClient(read_api.app)
    entry_ref = "operator-management-replay-entry"
    entry = client.post(
        "/v1/operator/orders",
        json=_open_position_body(client_ref=entry_ref),
        headers=_operator_headers(),
    )
    assert entry.status_code == 200, entry.text
    body = _management_order_body(
        client_ref="operator-management-replay-target-binding",
        entry_ref=entry_ref,
        target_position_id="BTCUSDT-PERP.BINANCE-LONG",
    )
    placed = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )
    assert placed.status_code == 200, placed.text

    drifted_body = dict(body)
    drifted_body["target_position_id"] = (
        "BTCUSDT-PERP.BINANCE-OTHER"
    )
    replayed = client.post(
        "/v1/operator/orders",
        json=drifted_body,
        headers=_operator_headers(),
    )

    assert replayed.status_code == 409, replayed.text
    assert replayed.json()["detail"] == (
        "idempotency key trade semantics mismatch"
    )
    assert _persisted_target_position_id(
        migrated_db,
        placed.json()["intent_id"],
    ) == "BTCUSDT-PERP.BINANCE-LONG"


def test_management_replay_with_identical_target_uses_persisted_attribution(
    tmp_path,
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    monkeypatch.setenv(
        "ATTRIBUTION_SHADOW_LOG",
        str(tmp_path / "attribution-shadow.jsonl"),
    )
    client = TestClient(read_api.app)
    entry_ref = "operator-management-replay-attribution-entry"
    entry = client.post(
        "/v1/operator/orders",
        json=_open_position_body(client_ref=entry_ref),
        headers=_operator_headers(),
    )
    assert entry.status_code == 200, entry.text
    body = _management_order_body(
        client_ref="operator-management-replay-attribution-binding",
        entry_ref=entry_ref,
        target_position_id="BTCUSDT-PERP.BINANCE-LONG",
    )
    placed = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )
    assert placed.status_code == 200, placed.text
    placed_payload = placed.json()
    persisted_attribution = placed_payload["attribution"]
    assert persisted_attribution["resolution"] == "intent"

    replay_body = dict(body)
    replay_body["entry_ref"] = "missing-entry-ref"
    replayed = client.post(
        "/v1/operator/orders",
        json=replay_body,
        headers=_operator_headers(),
    )

    assert replayed.status_code == 200, replayed.text
    replayed_payload = replayed.json()
    assert replayed_payload["replay"] is True
    assert replayed_payload["intent_id"] == placed_payload["intent_id"]
    assert replayed_payload["attribution"] == persisted_attribution
    assert _persisted_target_position_id(
        migrated_db,
        placed_payload["intent_id"],
    ) == "BTCUSDT-PERP.BINANCE-LONG"


def test_limit_ioc_rejects_quantity_above_notional_cap(
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app)
    body = _open_position_body(
        client_ref="operator-limit-ioc-quantity-cap",
        time_in_force="IOC",
    )
    body["quantity"] = "0.2"
    body["notional_usdt"] = 10
    body["dry_run"] = True

    response = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "quantity * entry.price exceeds notional_usdt"
    )


def test_limit_ioc_rejects_decimal_cap_breach_hidden_by_float_rounding(
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app)
    body = _open_position_body(
        client_ref="operator-limit-ioc-decimal-cap",
        time_in_force="IOC",
    )
    body["quantity"] = "0.07"
    body["entry"]["price"] = "100.00000000000000001"
    body["notional_usdt"] = "7"
    body["dry_run"] = True

    response = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "quantity * entry.price exceeds notional_usdt"
    )


def test_limit_ioc_rejects_cap_breach_beyond_default_decimal_precision(
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app)
    body = _open_position_body(
        client_ref="operator-limit-ioc-high-precision-cap",
        time_in_force="IOC",
    )
    body["quantity"] = "0.070000000000000001"
    body["entry"]["price"] = "100.930000000000000001"
    body["notional_usdt"] = "7.065100000000000101"
    body["dry_run"] = True

    response = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "quantity * entry.price exceeds notional_usdt"
    )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("entry.price", "NaN"),
        ("entry.price", "Infinity"),
        ("notional_usdt", "NaN"),
        ("notional_usdt", "Infinity"),
    ],
)
def test_open_position_rejects_non_finite_numeric_input(
    field_name,
    value,
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app, raise_server_exceptions=False)
    body = _open_position_body(
        client_ref=f"operator-non-finite-{field_name}-{value}",
        time_in_force="IOC",
    )
    if field_name == "entry.price":
        body["entry"]["price"] = value
    else:
        body["notional_usdt"] = value
    body["dry_run"] = True

    response = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == f"{field_name} must be a number"


@pytest.mark.parametrize(
    ("value", "expected_detail"),
    [
        ("1e1000000", "quantity exponent is out of range"),
        ("1e-1000000", "quantity exponent is out of range"),
        (
            "12345678901234567890123456789",
            "quantity has too many total digits",
        ),
        (
            "0.0000000000000000001",
            "quantity has too many decimal places",
        ),
        (
            "1000000000000000001",
            "quantity magnitude exceeds limit",
        ),
    ],
)
def test_explicit_quantity_rejects_unreasonable_decimal_bounds(
    value,
    expected_detail,
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app, raise_server_exceptions=False)
    body = _open_position_body(
        client_ref=f"operator-quantity-bound-{value}",
        time_in_force="IOC",
    )
    body["quantity"] = value
    body["dry_run"] = True

    response = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == expected_detail


def test_explicit_quantity_requires_limit_entry(
    migrated_db,
    monkeypatch,
) -> None:
    _configure(monkeypatch, migrated_db)
    client = TestClient(read_api.app)
    body = _open_position_body(
        client_ref="operator-market-explicit-quantity",
    )
    body["entry"] = {"type": "market"}
    body["quantity"] = "0.07"
    body["dry_run"] = True

    response = client.post(
        "/v1/operator/orders",
        json=body,
        headers=_operator_headers(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "explicit quantity requires a limit entry"
    )


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


def _management_order_body(
    *,
    client_ref: str,
    entry_ref: str,
    target_position_id: str,
) -> dict:
    return {
        "action": "close_position",
        "account_id": ACCOUNT_ID,
        "symbol": "BTCUSDT",
        "position_side": "long",
        "target_position_id": target_position_id,
        "entry_ref": entry_ref,
        "channel": "operator",
        "reason": "management intent replay binding contract test",
        "client_ref": client_ref,
    }


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


def _persisted_target_position_id(
    database_url: str,
    intent_id: str,
) -> str | None:
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT target_position_id "
                "FROM trade_intents WHERE intent_id::text=%s",
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
