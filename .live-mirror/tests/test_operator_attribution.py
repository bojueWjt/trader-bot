from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from conftest import read_api


def _open_body(client_ref="tg-sig-c1002136478186-m5026"):
    return {
        "action": "open_position",
        "symbol": "BTCUSDT",
        "side": "long",
        "notional_usdt": 100,
        "account_id": "account-a",
        "reason": "test open",
        "client_ref": client_ref,
    }


def _manage_body(**overrides):
    body = {
        "action": "close_position",
        "symbol": "BTCUSDT",
        "position_side": "long",
        "account_id": "account-a",
        "reason": "test close",
        "client_ref": "close-btc-5026",
    }
    body.update(overrides)
    return body


def _post(api_client, auth_headers, body):
    return api_client.post("/v1/operator/orders", headers=auth_headers, json=body)


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


def test_open_without_provenance_keeps_operator_channel_and_logs_counter(
    api_client, auth_headers, fake_db, tmp_path, monkeypatch
):
    log_path = tmp_path / "no-provenance.jsonl"
    monkeypatch.setenv("ATTRIBUTION_SHADOW_LOG", str(log_path))

    response = _post(api_client, auth_headers, _open_body("verbal-btc-long-0714"))

    assert response.status_code == 200
    _, params = _raw_insert(fake_db)
    assert "hermes-operator" in params
    event = json.loads(log_path.read_text().strip())
    assert event["action"] == "open_position"
    assert event["error"] == "no_provenance"


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


def test_management_dry_run_keeps_legacy_no_ref_behavior(
    api_client, auth_headers, fake_db
):
    body = _manage_body(dry_run=True)
    body.pop("client_ref")

    response = _post(api_client, auth_headers, body)

    assert response.status_code == 200
    assert response.json()["dry_run"] is True
    assert not fake_db.executions


def test_open_keeps_legacy_idempotency_formula(api_client, auth_headers, fake_db):
    body = _open_body()
    response = _post(api_client, auth_headers, body)

    expected = hashlib.sha256(
        b"operator|account-a|tg-sig-c1002136478186-m5026"
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


def test_shadow_ref_parse_only_remains_unproven(api_client, auth_headers):
    entry_ref = "tg-sig-c1002136478186-m5026"

    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="-1002136478186", entry_ref=entry_ref),
    )

    attribution = response.json()["attribution"]
    assert attribution["resolution"] == "ref_parse_only"
    assert attribution["owner_channel"] == "-1002136478186"
    assert attribution["channel_match"] is True
    assert attribution["would_reject"] is True


def test_shadow_unresolved_entry_ref(api_client, auth_headers):
    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="-1002136478186", entry_ref="legacy-5026"),
    )

    assert response.json()["attribution"] == {
        "resolution": "none",
        "owner_channel": False,
        "channel_match": "unknown",
        "would_reject": True,
    }


def test_shadow_channel_mismatch_would_reject(api_client, auth_headers, fake_db):
    entry_ref = "tg-sig-c1002136478186-m5026"
    _seed_entry(fake_db, entry_ref)

    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="-1002228497993", entry_ref=entry_ref),
    )

    attribution = response.json()["attribution"]
    assert attribution["channel_match"] is False
    assert attribution["would_reject"] is True


def test_shadow_operator_channel_bypasses_rejection(api_client, auth_headers, fake_db):
    entry_ref = "tg-sig-c1002136478186-m5026"
    _seed_entry(fake_db, entry_ref)

    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="operator", entry_ref=entry_ref),
    )

    attribution = response.json()["attribution"]
    assert attribution["channel_match"] is False
    assert attribution["would_reject"] is False


@pytest.mark.parametrize(
    ("entry_account", "entry_symbol", "expected"),
    [
        ("account-a", "ETHUSDT", "instrument"),
        ("account-b", "BTCUSDT", "account"),
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


def test_shadow_side_mismatch_is_observed_only(api_client, auth_headers, fake_db):
    entry_ref = "tg-sig-c1002136478186-m5026"
    _seed_entry(fake_db, entry_ref, side="short")

    response = _post(
        api_client,
        auth_headers,
        _manage_body(channel="-1002136478186", entry_ref=entry_ref),
    )

    assert response.status_code == 200
    assert response.json()["attribution"]["would_reject"] is True


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
