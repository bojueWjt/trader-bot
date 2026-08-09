from __future__ import annotations

import hashlib
from pathlib import Path

import read_api


AUTHORIZATION = {
    "authorized_by_type": "channel",
    "authorized_by_id": "-1002136478186",
    "source_message_id": "tg-msg-5026",
    "created_by_service": "decision-gateway",
    "parent_intent_id": False,
    "channel_id": "-1002136478186",
}
ATTRIBUTION = {
    "resolution": "intent",
    "owner_channel": "-1002136478186",
    "channel_match": True,
    "would_reject": False,
}


def _assert_authorization_metadata(plan: dict) -> None:
    assert plan["authorization"] == AUTHORIZATION
    assert plan["attribution"] == ATTRIBUTION


def test_open_position_downlink_preserves_authorization_metadata() -> None:
    plan = read_api._execution_order_plan(
        {
            "side": "long",
            "entry": {"type": "limit", "price": 100.0},
            "quantity": "2",
            "stop_loss": 90.0,
            "take_profits": [110.0, 120.0],
            "authorization": AUTHORIZATION,
            "attribution": ATTRIBUTION,
        },
        {"max_notional": 200.0},
        "BTCUSDT",
        action="open_position",
    )

    assert plan["side"] == "buy"
    assert plan["type"] == "limit"
    _assert_authorization_metadata(plan)


def test_management_downlink_preserves_authorization_metadata() -> None:
    plan = read_api._execution_order_plan(
        {
            "stop_loss": 95.0,
            "position_side": "long",
            "authorization": AUTHORIZATION,
            "attribution": ATTRIBUTION,
        },
        {"max_notional": 0.0},
        "BTCUSDT",
        action="move_stop_loss",
    )

    assert plan["stop_price"] == 95.0
    assert plan["position_side"] == "long"
    _assert_authorization_metadata(plan)


def test_zone_ladder_downlink_preserves_authorization_metadata(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        read_api,
        "_binance_mark_price",
        lambda symbol: 102.0,
    )
    plan = read_api._execution_order_plan(
        {
            "side": "long",
            "entry": {
                "type": "zone",
                "price_min": 99.0,
                "price_max": 101.0,
            },
            "stop_loss": 90.0,
            "take_profits": [110.0, 120.0],
            "authorization": AUTHORIZATION,
            "attribution": ATTRIBUTION,
        },
        {"max_notional": 1000.0},
        "BTCUSDT",
        action="open_position",
    )

    assert plan["type"] == "zone_ladder"
    assert len(plan["tranches"]) == 3
    _assert_authorization_metadata(plan)


def test_disable_take_profits_downlink_preserves_tombstone_and_authorization() -> None:
    plan = read_api._execution_order_plan(
        {
            "take_profits": [],
            "disable_take_profits": True,
            "position_side": "long",
            "authorization": AUTHORIZATION,
            "attribution": ATTRIBUTION,
        },
        {"max_notional": 0.0},
        "BTCUSDT",
        action="replace_take_profits",
    )

    assert plan["take_profits"] == []
    assert plan["disable_take_profits"] is True
    assert plan["position_side"] == "long"
    _assert_authorization_metadata(plan)


def test_live_mirror_read_api_preserves_historical_snapshot() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    mirror_api = repo_root / ".live-mirror" / "api" / "read_api.py"

    digest = hashlib.sha256(mirror_api.read_bytes()).hexdigest()
    assert digest == (
        "d9b43cda242ee7d9f69de110250275353"
        "5e179e035d1b877f4b5add375bc767b"
    )
