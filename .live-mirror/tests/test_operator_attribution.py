from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pytest

from conftest import read_api


def _open_body(client_ref="tg-sig-c1002136478186-m5026"):
    return {
        "action": "open_position",
        "symbol": "BTCUSDT",
        "side": "long",
        "notional_usdt": 100,
        "account_id": "account-b",
        "reason": "test open",
        "client_ref": client_ref,
        "authorized_by_type": "channel",
        "authorized_by_id": "-1002136478186",
        "source_message_id": client_ref,
        "created_by_service": "hermes-agent",
    }


def _manage_body(**overrides):
    body = {
        "action": "close_position",
        "symbol": "BTCUSDT",
        "position_side": "long",
        "account_id": "account-a",
        "reason": "test close",
        "client_ref": "close-btc-5026",
        "authorized_by_type": "user",
        "authorized_by_id": "balen",
        "source_message_id": "close-btc-5026",
        "created_by_service": "hermes-agent",
    }
    body.update(overrides)
    channel = str(body.get("channel") or "").strip()
    if channel and channel != "operator":
        body["authorized_by_type"] = "channel"
        body["authorized_by_id"] = channel
        body["source_message_id"] = str(
            body.get("source_message_id") or "management-message-5026"
        )
    return body


def _post(api_client, auth_headers, body, request_id=None):
    headers = dict(auth_headers)
    if request_id:
        headers["X-Request-Id"] = request_id
    return api_client.post("/v1/operator/orders", headers=headers, json=body)


def _old_idem(account, entry_ref):
    material = f"operator|{account}|{entry_ref}"
    return hashlib.sha256(material.encode()).hexdigest()


def _seed_entry(
    fake_db,
    entry_ref,
    account="account-a",
    symbol="BTCUSDT",
    side="long",
    channel="-1002136478186",
    source_message_id=False,
):
    if source_message_id is False:
        source_message_id = entry_ref
    fake_db.attribution_by_idem[_old_idem(account, entry_ref)] = (
        "11111111-1111-1111-1111-111111111111",
        "open_position",
        symbol,
        account,
        {"side": side},
        channel,
        source_message_id,
    )


def _raw_insert(fake_db):
    return next(
        item for item in fake_db.executions
        if item[0].startswith("INSERT INTO raw_messages")
    )


def _insert(fake_db, prefix):
    return next(
        item for item in fake_db.executions
        if item[0].startswith(prefix)
    )


def _json_value(value):
    return getattr(value, "adapted", value)


def _assert_no_order_writes(fake_db):
    write_prefixes = (
        "INSERT INTO raw_messages",
        "INSERT INTO trade_intents",
        "INSERT INTO outbox_events",
    )
    assert not any(
        sql.startswith(write_prefixes)
        for sql, _ in fake_db.executions
    )


def test_operator_command_scope_persists_user_authorization() -> None:
    scope = read_api._operator_command_scope(
        {"scope": {"account_id": "account-a"}},
        "operator-request-42",
        "user requested cancel all",
    )

    assert scope["authorization"] == {
        "authorized_by_type": "user",
        "authorized_by_id": "risk_admin",
        "source_message_id": "operator-request-42",
        "created_by_service": "control-plane",
    }
    assert scope["request_id"] == "operator-request-42"
    assert scope["reason"] == "user requested cancel all"


def _use_parent_row(monkeypatch, fake_db, row):
    class ParentCursor:
        def __init__(self):
            self.result = row

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            compact = " ".join(sql.split())
            fake_db.executions.append((compact, params or ()))

        def fetchone(self):
            return self.result

    class ParentConnection:
        def cursor(self):
            return ParentCursor()

        def close(self):
            return None

    monkeypatch.setattr(
        read_api.psycopg2,
        "connect",
        lambda database_url: ParentConnection(),
    )


def test_non_dry_run_requires_explicit_account_before_writes(
    api_client, auth_headers, fake_db
):
    body = _open_body()
    body.pop("account_id")

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "account_id is required" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


@pytest.mark.parametrize(
    "account_id",
    ["account-a", "account-b", "account-c", "account-d"],
)
def test_configured_four_account_registry_accepts_each_account(
    api_client,
    auth_headers,
    fake_db,
    monkeypatch,
    account_id,
):
    monkeypatch.setenv(
        "OPERATOR_ACCOUNT_REGISTRY_JSON",
        json.dumps(
            {
                candidate: {}
                for candidate in (
                    "account-a",
                    "account-b",
                    "account-c",
                    "account-d",
                )
            }
        ),
    )
    body = _open_body(f"tg-sig-c1002136478186-m{5026 + len(fake_db.executions)}")
    body["account_id"] = account_id
    if account_id == "account-a":
        body["dry_run"] = True

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 200
    assert response.json()["account_id"] == account_id


def test_non_dry_run_requires_authorization_evidence_before_writes(
    api_client, auth_headers, fake_db
):
    body = _open_body()
    for field in (
        "authorized_by_type",
        "authorized_by_id",
        "source_message_id",
        "created_by_service",
    ):
        body.pop(field)

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "authorized_by_type" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_operator_is_not_an_authority_class(
    api_client, auth_headers, fake_db
):
    body = _manage_body(authorized_by_type="operator")

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "['user', 'channel']" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_channel_source_cannot_be_labeled_as_user(
    api_client, auth_headers, fake_db
):
    body = _open_body()
    body["authorized_by_type"] = "user"
    body["authorized_by_id"] = "balen"

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "authorized_by_type=channel" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


@pytest.mark.parametrize("created_by_service", ["internal", "watchdog", "reconciler"])
def test_internal_sources_without_parent_create_zero_order_records(
    api_client, auth_headers, fake_db, created_by_service
):
    body = _manage_body(
        source=created_by_service,
        created_by_service=created_by_service,
    )

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "parent_intent_id" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


@pytest.mark.parametrize("created_by_service", ["internal", "watchdog", "reconciler"])
def test_internal_sources_with_unknown_parent_create_zero_order_records(
    api_client, auth_headers, fake_db, created_by_service
):
    body = _manage_body(
        source=created_by_service,
        created_by_service=created_by_service,
        parent_intent_id="22222222-2222-2222-2222-222222222222",
    )

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "authorized trade intent" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_internal_source_rejects_cross_symbol_parent_before_writes(
    api_client, auth_headers, fake_db, monkeypatch
):
    _use_parent_row(
        monkeypatch,
        fake_db,
        (
            "account-a",
            "BTCUSDT",
            "open_position",
            "approved",
            {
                "authorization": {
                    "authorized_by_type": "user",
                    "authorized_by_id": "balen",
                    "source_message_id": "original-user-request",
                },
            },
        ),
    )
    body = _manage_body(
        symbol="MUUSDT",
        source="position-reconciler",
        created_by_service="position-reconciler",
        parent_intent_id="11111111-1111-1111-1111-111111111111",
        source_message_id="original-user-request",
    )

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "instrument does not match" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


@pytest.mark.parametrize(
    ("parent_action", "parent_status", "expected"),
    [
        ("open_position", "rejected", "status is not executable"),
        ("hold", "approved", "action is not executable"),
    ],
)
def test_internal_source_rejects_non_executable_parent_lineage(
    api_client,
    auth_headers,
    fake_db,
    monkeypatch,
    parent_action,
    parent_status,
    expected,
):
    _use_parent_row(
        monkeypatch,
        fake_db,
        (
            "account-a",
            "MUUSDT-PERP.BINANCE",
            parent_action,
            parent_status,
            {
                "authorization": {
                    "authorized_by_type": "user",
                    "authorized_by_id": "balen",
                    "source_message_id": "original-user-request",
                },
            },
        ),
    )
    body = _manage_body(
        symbol="MUUSDT",
        source="position-reconciler",
        created_by_service="position-reconciler",
        parent_intent_id="11111111-1111-1111-1111-111111111111",
        source_message_id="original-user-request",
    )

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert expected in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_internal_source_requires_parent_authorization_match(
    api_client, auth_headers, fake_db, monkeypatch
):
    monkeypatch.setattr(
        read_api,
        "_authorized_parent",
        lambda *args: {
            "authorized_by_type": "user",
            "authorized_by_id": "balen",
            "source_message_id": "original-user-request",
        },
    )
    body = _manage_body(
        source="position-reconciler",
        created_by_service="position-reconciler",
        parent_intent_id="11111111-1111-1111-1111-111111111111",
        source_message_id="different-request",
    )

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "does not match parent_intent_id" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_internal_source_persists_matching_parent_authorization(
    api_client, auth_headers, fake_db, monkeypatch
):
    parent_intent_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(
        read_api,
        "_authorized_parent",
        lambda *args: {
            "authorized_by_type": "user",
            "authorized_by_id": "balen",
            "source_message_id": "original-user-request",
        },
    )
    body = _manage_body(
        source="position-reconciler",
        created_by_service="position-reconciler",
        parent_intent_id=parent_intent_id,
        source_message_id="original-user-request",
    )

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 200
    _, intent_params = _insert(fake_db, "INSERT INTO trade_intents")
    authorization = _json_value(intent_params[6])["authorization"]
    assert authorization["authorized_by_type"] == "user"
    assert authorization["parent_intent_id"] == parent_intent_id


def test_management_persists_target_position_from_parent_intent(
    api_client,
    auth_headers,
    fake_db,
    monkeypatch,
):
    parent_intent_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(
        read_api,
        "_authorized_parent",
        lambda *args: {
            "authorized_by_type": "user",
            "authorized_by_id": "balen",
            "source_message_id": "original-user-request",
            "_target_position_id": "BTCUSDT-PERP.BINANCE-LONG",
        },
    )
    body = _manage_body(
        source="position-reconciler",
        created_by_service="position-reconciler",
        parent_intent_id=parent_intent_id,
        source_message_id="original-user-request",
    )

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 200
    assert response.json()["target_position_id"] == "BTCUSDT-PERP.BINANCE-LONG"
    _, intent_params = _insert(fake_db, "INSERT INTO trade_intents")
    assert intent_params[8] == "BTCUSDT-PERP.BINANCE-LONG"


def test_user_authorization_is_persisted_in_every_audit_payload(
    api_client, auth_headers, fake_db
):
    body = _open_body("user-btc-long-5026")
    body.update({
        "authorized_by_type": "user",
        "authorized_by_id": "forged-user",
        "source_message_id": "forged-request",
        "created_by_service": "forged-service",
        "source_channel": "operator",
    })

    response = _post(
        api_client,
        auth_headers,
        body,
        request_id="codex-request-5026",
    )

    assert response.status_code == 200
    expected = {
        "authorized_by_type": "user",
        "authorized_by_id": "risk_admin",
        "reason": "test open",
        "source_message_id": "user-btc-long-5026",
        "created_by_service": "control-plane",
        "parent_intent_id": False,
    }
    _, raw_params = _raw_insert(fake_db)
    assert raw_params[2] == "user-btc-long-5026"
    assert raw_params[4] == "risk_admin"
    assert _json_value(raw_params[7])["authorization"] == expected
    _, decision_params = _insert(fake_db, "INSERT INTO hermes_decisions")
    assert expected in _json_value(decision_params[17])
    _, intent_params = _insert(fake_db, "INSERT INTO trade_intents")
    assert _json_value(intent_params[6])["authorization"] == expected
    _, outbox_params = _insert(fake_db, "INSERT INTO outbox_events")
    assert _json_value(outbox_params[2])["authorization"] == expected
    _, audit_params = _insert(fake_db, "INSERT INTO audit_events")
    assert audit_params[2] == "risk_admin"
    assert _json_value(audit_params[7])["authorization"] == expected


def test_channel_management_authorization_requires_and_persists_attribution(
    api_client, auth_headers, fake_db
):
    entry_ref = "tg-sig-c1002136478186-m5026"
    _seed_entry(fake_db, entry_ref)
    body = _manage_body(
        channel="-1002136478186",
        entry_ref=entry_ref,
        source_message_id="tg-msg-6010",
    )

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 200
    expected = {
        "authorized_by_type": "channel",
        "authorized_by_id": "-1002136478186",
        "reason": "test close",
        "source_message_id": "tg-msg-6010",
        "created_by_service": "hermes-agent",
        "parent_intent_id": False,
    }
    _, raw_params = _raw_insert(fake_db)
    assert raw_params[1] == "-1002136478186"
    assert raw_params[2] == "close-btc-5026"
    _, intent_params = _insert(fake_db, "INSERT INTO trade_intents")
    assert _json_value(intent_params[6])["authorization"] == expected
    assert _json_value(intent_params[6])["attribution"]["resolution"] == "intent"
    assert intent_params[8] == "BTCUSDT-PERP.BINANCE-LONG"
    assert response.json()["target_position_id"] == "BTCUSDT-PERP.BINANCE-LONG"
    _, outbox_params = _insert(fake_db, "INSERT INTO outbox_events")
    assert _json_value(outbox_params[2])["authorization"] == expected


def test_channel_management_uses_original_entry_account_after_route_change(
    api_client,
    auth_headers,
    fake_db,
):
    entry_ref = "tg-sig-c1002136478186-m5026"
    _seed_entry(fake_db, entry_ref, account="account-a")
    body = _manage_body(
        account_id="account-c",
        channel="-1002136478186",
        entry_ref=entry_ref,
        source_message_id="tg-sig-c1002136478186-m6011",
    )

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 200
    _, intent_params = _insert(fake_db, "INSERT INTO trade_intents")
    assert intent_params[3] == "account-a"
    assert response.json()["attribution"] == {
        "resolution": "intent",
        "owner_channel": "-1002136478186",
        "channel_match": True,
        "would_reject": False,
    }


def test_open_persists_real_and_effective_equity_in_intent_and_audit(
    api_client,
    auth_headers,
    fake_db,
    monkeypatch,
):
    def fake_size(*args):
        checks = args[-2]
        checks.append(
            {
                "name": "account_equity_basis",
                "passed": True,
                "real_equity": 5000.0,
                "available_balance": 1000.0,
                "effective_equity": 9000.0,
                "risk_capital_multiplier": 1.8,
            }
        )
        return 900.0

    monkeypatch.setattr(read_api, "_size_open_order", fake_size)
    body = _open_body("tg-sig-c1002136478186-m6001")
    body["account_id"] = "account-b"

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 200
    _, intent_params = _insert(fake_db, "INSERT INTO trade_intents")
    assert _json_value(intent_params[6])["equity"] == {
        "real_equity": 5000.0,
        "available_balance": 1000.0,
        "risk_capital_multiplier": 1.8,
        "effective_equity": 9000.0,
    }
    _, audit_params = _insert(fake_db, "INSERT INTO audit_events")
    audit = _json_value(audit_params[7])
    assert audit["real_equity"] == 5000.0
    assert audit["effective_equity"] == 9000.0
    assert audit["risk_capital_multiplier"] == 1.8


def test_empty_take_profits_without_disable_flag_is_rejected(
    api_client, auth_headers, fake_db
):
    body = _manage_body(
        action="replace_take_profits",
        take_profits=[],
    )

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "requires take_profits" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_disable_take_profits_requires_position_side(
    api_client, auth_headers, fake_db
):
    body = _manage_body(
        action="replace_take_profits",
        position_side=None,
        take_profits=[],
        disable_take_profits=True,
    )

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "position_side" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_explicit_disable_take_profits_persists_tombstone_and_audit(
    api_client, auth_headers, fake_db
):
    body = _manage_body(
        action="replace_take_profits",
        take_profits=[],
        disable_take_profits=True,
        client_ref="disable-tps-btc-user-request-7001",
        source_message_id="user-request-7001",
    )

    response = _post(
        api_client,
        auth_headers,
        body,
        request_id="user-request-7001",
    )

    assert response.status_code == 200
    _, decision_params = _insert(fake_db, "INSERT INTO hermes_decisions")
    evidence = _json_value(decision_params[17])
    assert evidence[0]["authorized_by_type"] == "user"
    assert evidence[0]["source_message_id"] == "disable-tps-btc-user-request-7001"
    _, intent_params = _insert(fake_db, "INSERT INTO trade_intents")
    order_plan = _json_value(intent_params[6])
    assert order_plan["take_profits"] == []
    assert order_plan["disable_take_profits"] is True
    assert order_plan["position_side"] == "long"
    _, outbox_params = _insert(fake_db, "INSERT INTO outbox_events")
    outbox = _json_value(outbox_params[2])
    assert (
        outbox["authorization"]["source_message_id"]
        == "disable-tps-btc-user-request-7001"
    )


def test_disable_take_profits_dry_run_has_tombstone_and_zero_writes(
    api_client, auth_headers, fake_db
):
    body = _manage_body(
        action="replace_take_profits",
        take_profits=[],
        disable_take_profits=True,
        dry_run=True,
    )

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 200
    preview = response.json()["order_plan_preview"]
    assert preview["take_profits"] == []
    assert preview["disable_take_profits"] is True
    assert preview["position_side"] == "long"
    assert response.json()["authorization"] == {
        "authorized_by_type": "user",
        "authorized_by_id": "risk_admin",
        "reason": "test close",
        "source_message_id": "close-btc-5026",
        "created_by_service": "control-plane",
        "parent_intent_id": False,
    }
    assert not fake_db.executions


def test_channel_dry_run_cannot_be_labeled_as_user(
    api_client, auth_headers, fake_db
):
    body = _open_body()
    body["dry_run"] = True
    body["authorized_by_type"] = "user"
    body["authorized_by_id"] = "balen"

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "authorized_by_type=channel" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_open_provenance_parses_e2_suffix(api_client, auth_headers, fake_db):
    response = _post(
        api_client,
        auth_headers,
        _open_body("tg-sig-c1002136478186-m5026-e2"),
    )

    assert response.status_code == 200
    _, params = _raw_insert(fake_db)
    assert "-1002136478186" in params


def test_open_provenance_accepts_source_channel_without_ref_parse(
    api_client, auth_headers, fake_db
):
    body = _open_body("verbal-btc-long-0714")
    body["source_channel"] = "-1002136478186"
    body["source_message_id"] = "telegram-message-0714"

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 200
    _, params = _raw_insert(fake_db)
    assert "-1002136478186" in params


def test_open_provenance_rejects_conflicting_source_channel(
    api_client, auth_headers, fake_db
):
    body = _open_body()
    body["source_channel"] = "-1002228497993"

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "source_channel conflicts" in response.json()["detail"]
    assert not any(sql.startswith("INSERT INTO raw_messages") for sql, _ in fake_db.executions)


def test_direct_user_open_derives_authorization_from_risk_admin(
    api_client, auth_headers, fake_db
):
    body = _open_body("verbal-btc-long-0714")
    body.pop("authorized_by_type")
    body.pop("authorized_by_id")
    body.pop("source_message_id")
    body.pop("created_by_service")

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 200
    assert response.json()["authorization"] == {
        "authorized_by_type": "user",
        "authorized_by_id": "risk_admin",
        "reason": "test open",
        "source_message_id": "verbal-btc-long-0714",
        "created_by_service": "control-plane",
        "parent_intent_id": False,
    }


@pytest.mark.parametrize(
    ("action", "overrides"),
    [
        ("close_position", {}),
        ("partial_close", {"quantity": 0.01}),
        ("move_stop_loss", {"stop_loss": 60000}),
        (
            "replace_take_profits",
            {"take_profits": [{"price": 70000, "quantity": 0.01}]},
        ),
        (
            "cancel_order",
            {"client_order_id": "B" + "a" * 32 + "01", "position_side": None},
        ),
    ],
)
def test_management_requires_stable_client_ref(
    api_client, auth_headers, action, overrides
):
    body = _manage_body(action=action, **overrides)
    body.pop("client_ref")

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "stable operation ref" in response.json()["detail"]


def _cancel_body(
    *,
    channel: str = "-1002136478186",
    account_id: str = "account-a",
    symbol: str = "BTCUSDT",
) -> dict:
    client_order_id = "B" + ("a" * 32) + "01"
    source_message_id = "tg-sig-c1002136478186-m7002"
    return {
        "action": "cancel_order",
        "symbol": symbol,
        "client_order_id": client_order_id,
        "account_id": account_id,
        "reason": "cancel owned resting order",
        "client_ref": "cancel-owned-order-7002",
        "channel": channel,
        "authorized_by_type": "channel",
        "authorized_by_id": channel,
        "source_message_id": source_message_id,
        "created_by_service": "hermes-agent",
    }


def _seed_cancel_owner(
    fake_db,
    *,
    account_id: str = "account-a",
    symbol: str = "BTCUSDT",
    channel: str = "-1002136478186",
) -> None:
    fake_db.cancel_owner = [
        (
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            account_id,
            symbol,
            {
                "authorization": {
                    "authorized_by_type": "channel",
                    "authorized_by_id": channel,
                    "source_message_id": (
                        "tg-sig-c1002136478186-m7001"
                    ),
                }
            },
            channel,
            "tg-sig-c1002136478186-m7001",
        )
    ]


def test_cancel_order_binds_account_instrument_intent_and_channel(
    api_client,
    auth_headers,
    fake_db,
) -> None:
    _seed_cancel_owner(fake_db)

    response = _post(
        api_client,
        auth_headers,
        _cancel_body(),
    )

    assert response.status_code == 200
    ownership_query = next(
        item
        for item in fake_db.executions
        if "FROM orders_projection AS op" in item[0]
    )
    assert ownership_query[1] == (
        "B" + ("a" * 32) + "01",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "BTCUSDT",
        "BTCUSDT",
    )


def test_channel_cancel_uses_order_owner_after_channel_account_remap(
    api_client,
    auth_headers,
    fake_db,
) -> None:
    _seed_cancel_owner(fake_db, account_id="account-a")
    response = _post(
        api_client,
        auth_headers,
        _cancel_body(account_id="account-c"),
    )

    assert response.status_code == 200
    _, intent_params = _insert(fake_db, "INSERT INTO trade_intents")
    assert intent_params[3] == "account-a"


def test_user_cancel_rejects_cross_account(
    api_client,
    auth_headers,
    fake_db,
) -> None:
    _seed_cancel_owner(fake_db, account_id="account-a")
    body = _cancel_body(account_id="account-b")
    body.pop("channel")
    body["authorized_by_type"] = "user"
    body["authorized_by_id"] = "balen"
    body["source_message_id"] = "user-cancel-7002"

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "account and instrument" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_cancel_order_rejects_cross_instrument(
    api_client,
    auth_headers,
    fake_db,
) -> None:
    _seed_cancel_owner(fake_db)

    response = _post(
        api_client,
        auth_headers,
        _cancel_body(symbol="ETHUSDT"),
    )

    assert response.status_code == 400
    assert "account and instrument" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_cancel_order_rejects_authorization_from_another_channel(
    api_client,
    auth_headers,
    fake_db,
) -> None:
    _seed_cancel_owner(fake_db)
    body = _cancel_body(channel="-1002198013097")

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "authorization channel does not own order" in (
        response.json()["detail"]
    )
    _assert_no_order_writes(fake_db)


def test_channel_management_requires_explicit_position_side(
    api_client,
    auth_headers,
    fake_db,
) -> None:
    body = _manage_body(
        channel="-1002136478186",
        position_side=None,
        entry_ref="tg-sig-c1002136478186-m7001",
    )

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "channel management requires position_side" in (
        response.json()["detail"]
    )
    _assert_no_order_writes(fake_db)


def test_management_idempotency_v2_replays_existing_intent(
    api_client, auth_headers, fake_db, tmp_path
):
    body = _manage_body()
    first = _post(api_client, auth_headers, body)
    second = _post(api_client, auth_headers, body)

    expected = hashlib.sha256(
        b"operator-v2|account-a|close_position|BTCUSDT|long|close-btc-5026"
    ).hexdigest()
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["replay"] is True
    assert expected in fake_db.intents_by_idem
    assert second.json()["intent_id"] == first.json()["intent_id"]
    assert "attribution" in second.json()
    log_lines = (tmp_path / "attribution.jsonl").read_text().splitlines()
    assert len(log_lines) == 2


def test_management_requires_server_resolved_target_position(
    api_client,
    auth_headers,
    fake_db,
):
    body = _manage_body(position_side=None)

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "server-resolved target_position_id" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_management_idempotency_locks_before_lookup(
    api_client,
    auth_headers,
    fake_db,
):
    response = _post(api_client, auth_headers, _manage_body())

    assert response.status_code == 200
    statements = [sql for sql, _params in fake_db.executions]
    lock_index = next(
        index
        for index, sql in enumerate(statements)
        if sql.startswith("SELECT pg_advisory_xact_lock")
    )
    lookup_index = next(
        index
        for index, sql in enumerate(statements)
        if sql.startswith(
            "SELECT intent_id::text, status::text, valid_until, order_plan"
        )
    )
    assert lock_index < lookup_index


def test_legacy_management_idempotency_record_requires_new_ref(
    api_client,
    auth_headers,
    fake_db,
):
    body = _manage_body()
    first = _post(api_client, auth_headers, body)
    assert first.status_code == 200

    expected = hashlib.sha256(
        b"operator-v2|account-a|close_position|BTCUSDT|long|close-btc-5026"
    ).hexdigest()
    existing = fake_db.intents_by_idem[expected]
    legacy_plan = dict(existing[3])
    legacy_plan.pop("request_semantics")
    fake_db.intents_by_idem[expected] = (
        existing[0],
        existing[1],
        existing[2],
        legacy_plan,
    )

    second = _post(api_client, auth_headers, body)

    assert second.status_code == 409
    assert "legacy idempotency record" in second.json()["detail"]


def test_management_idempotency_replays_when_request_id_changes(
    api_client, auth_headers, fake_db
):
    body = _manage_body()
    first = _post(
        api_client,
        auth_headers,
        body,
        request_id="operator-request-original",
    )
    second = _post(
        api_client,
        auth_headers,
        body,
        request_id="operator-request-forged",
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["replay"] is True
    assert second.json()["intent_id"] == first.json()["intent_id"]
    assert (
        second.json()["authorization"]["source_message_id"]
        == body["client_ref"]
    )
    assert sum(
        sql.startswith("INSERT INTO raw_messages")
        for sql, _params in fake_db.executions
    ) == 1


@pytest.mark.parametrize(
    ("body", "field", "changed_value"),
    [
        (
            _manage_body(action="partial_close", quantity=0.01),
            "quantity",
            0.02,
        ),
        (
            _manage_body(action="move_stop_loss", stop_loss=60000),
            "stop_loss",
            61000,
        ),
        (
            _manage_body(
                action="replace_take_profits",
                take_profits=[{"price": 70000, "quantity": 0.01}],
            ),
            "take_profits",
            [{"price": 71000, "quantity": 0.01}],
        ),
    ],
)
def test_management_idempotency_rejects_changed_request_semantics(
    api_client,
    auth_headers,
    fake_db,
    body,
    field,
    changed_value,
):
    first = _post(api_client, auth_headers, body)
    changed = dict(body)
    changed[field] = changed_value

    second = _post(api_client, auth_headers, changed)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "idempotency key request payload mismatch"
    assert sum(
        sql.startswith("INSERT INTO raw_messages")
        for sql, _params in fake_db.executions
    ) == 1


def test_channel_idempotency_rejects_stable_authorization_evidence_change(
    api_client, auth_headers, fake_db
):
    body = _open_body()
    first = _post(api_client, auth_headers, body)
    changed = dict(body)
    changed["source_message_id"] = "different-channel-message"

    second = _post(api_client, auth_headers, changed)

    assert first.status_code == 200
    assert second.status_code == 409
    assert "authorization evidence mismatch" in second.json()["detail"]
    assert sum(
        sql.startswith("INSERT INTO raw_messages")
        for sql, _params in fake_db.executions
    ) == 1


def test_management_dry_run_requires_stable_request_ref(
    api_client, auth_headers, fake_db
):
    body = _manage_body(dry_run=True)
    body.pop("client_ref")
    body.pop("account_id")
    body.pop("authorized_by_type")
    body.pop("authorized_by_id")
    body.pop("source_message_id")
    body.pop("created_by_service")

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 400
    assert "stable operation ref" in response.json()["detail"]
    assert not fake_db.executions


def test_open_keeps_legacy_idempotency_formula(api_client, auth_headers, fake_db):
    body = _open_body()
    response = _post(api_client, auth_headers, body)

    expected = hashlib.sha256(
        b"operator|account-b|tg-sig-c1002136478186-m5026"
    ).hexdigest()
    assert response.status_code == 200
    assert expected in fake_db.intents_by_idem


def test_shadow_intent_resolution_recovers_legacy_operator_channel(
    api_client, auth_headers, fake_db
):
    entry_ref = "tg-sig-c1002136478186-m5026-e2"
    _seed_entry(
        fake_db,
        entry_ref,
        channel="hermes-operator",
        source_message_id=entry_ref,
    )

    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="-1002136478186", entry_ref=entry_ref),
    )

    assert response.status_code == 200
    assert response.json()["attribution"] == {
        "resolution": "intent",
        "owner_channel": "-1002136478186",
        "channel_match": True,
        "would_reject": False,
    }


def test_shadow_ref_parse_only_remains_unproven(
    api_client, auth_headers, fake_db
):
    entry_ref = "tg-sig-c1002136478186-m5026"

    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="-1002136478186", entry_ref=entry_ref),
    )

    assert response.status_code == 400
    assert "channel authorization attribution failed" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_shadow_unresolved_entry_ref(api_client, auth_headers, fake_db):
    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="-1002136478186", entry_ref="legacy-5026"),
    )

    assert response.status_code == 400
    assert "channel authorization attribution failed" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_shadow_channel_mismatch_would_reject(api_client, auth_headers, fake_db):
    entry_ref = "tg-sig-c1002136478186-m5026"
    _seed_entry(fake_db, entry_ref)

    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="-1002228497993", entry_ref=entry_ref),
    )

    assert response.status_code == 400
    assert "channel authorization attribution failed" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_direct_user_management_can_reference_operator_channel(
    api_client, auth_headers, fake_db
):
    entry_ref = "tg-sig-c1002136478186-m5026"
    _seed_entry(fake_db, entry_ref)

    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="operator", entry_ref=entry_ref),
    )

    attribution = response.json()["attribution"]
    assert response.json()["authorization"]["authorized_by_type"] == "user"
    assert attribution["channel_match"] is False
    assert attribution["would_reject"] is False


@pytest.mark.parametrize(
    ("entry_account", "entry_symbol", "expected"),
    [
        ("account-a", "ETHUSDT", "instrument"),
    ],
)
def test_shadow_rejects_pure_entry_identity_mismatch(
    api_client,
    auth_headers,
    fake_db,
    tmp_path,
    monkeypatch,
    entry_account,
    entry_symbol,
    expected,
):
    log_path = tmp_path / f"{expected}.jsonl"
    monkeypatch.setenv("ATTRIBUTION_SHADOW_LOG", str(log_path))
    entry_ref = "tg-sig-c1002136478186-m5026"
    _seed_entry(
        fake_db,
        entry_ref,
        account=entry_account,
        symbol=entry_symbol,
    )

    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="-1002136478186", entry_ref=entry_ref),
    )

    assert response.status_code == 400
    assert expected in response.json()["detail"]
    event = json.loads(log_path.read_text().strip())
    assert event["would_reject"] is True
    assert expected in event["error"]


def test_user_management_rejects_entry_from_another_account(
    api_client,
    auth_headers,
    fake_db,
):
    entry_ref = "tg-sig-c1002136478186-m5026"
    _seed_entry(
        fake_db,
        entry_ref,
        account="account-b",
    )

    response = _post(
        api_client,
        auth_headers,
        _manage_body(entry_ref=entry_ref),
    )

    assert response.status_code == 400
    assert "account" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_shadow_side_mismatch_rejects_channel_management(
    api_client, auth_headers, fake_db
):
    entry_ref = "tg-sig-c1002136478186-m5026"
    _seed_entry(fake_db, entry_ref, side="short")

    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="-1002136478186", entry_ref=entry_ref),
    )

    assert response.status_code == 400
    assert "channel authorization attribution failed" in response.json()["detail"]
    _assert_no_order_writes(fake_db)


def test_shadow_log_contains_required_fields(
    api_client, auth_headers, fake_db, tmp_path, monkeypatch
):
    log_path = tmp_path / "shadow.jsonl"
    monkeypatch.setenv("ATTRIBUTION_SHADOW_LOG", str(log_path))
    entry_ref = "tg-sig-c1002136478186-m5026"
    _seed_entry(fake_db, entry_ref)

    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="-1002136478186", entry_ref=entry_ref),
    )

    assert response.status_code == 200
    event = json.loads(log_path.read_text().strip())
    assert set(event) == {
        "ts",
        "action",
        "symbol",
        "account",
        "channel",
        "entry_ref",
        "resolution",
        "owner_channel",
        "channel_match",
        "would_reject",
        "bypass",
        "error",
    }
    datetime.fromisoformat(event["ts"])


def test_shadow_log_failure_does_not_block_order(
    api_client, auth_headers, fake_db, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("ATTRIBUTION_SHADOW_LOG", str(tmp_path))
    entry_ref = "tg-sig-c1002136478186-m5026"
    _seed_entry(fake_db, entry_ref)

    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="-1002136478186", entry_ref=entry_ref),
    )

    assert response.status_code == 200
    assert "attribution shadow log write failed" in capsys.readouterr().err
