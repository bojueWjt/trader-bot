from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

import gateway


class RecordingCursor:
    def __init__(self, raw_message: dict) -> None:
        self.raw_message = raw_message
        self.executions: list[tuple[str, tuple]] = []
        self.result = None

    def execute(self, sql, params=None) -> None:
        compact = " ".join(sql.split())
        values = tuple(params or ())
        self.executions.append((compact, values))
        self.result = None
        if compact.startswith("SELECT source, channel_id, source_message_id, author_id"):
            self.result = self.raw_message

    def fetchone(self):
        return self.result


def _json_value(value):
    return getattr(value, "adapted", value)


@pytest.mark.parametrize(
    ("raw_message", "expected"),
    (
        (
            {
                "source": "telegram",
                "channel_id": "-1002136478186",
                "source_message_id": "tg-msg-5026",
                "author_id": "signal-author",
            },
            {
                "authorized_by_type": "channel",
                "authorized_by_id": "-1002136478186",
                "source_message_id": "tg-msg-5026",
                "created_by_service": "decision-gateway",
                "parent_intent_id": False,
                "channel_id": "-1002136478186",
            },
        ),
        (
            {
                "source": "user",
                "channel_id": "operator",
                "source_message_id": "user-request-7001",
                "author_id": "balen",
            },
            {
                "authorized_by_type": "user",
                "authorized_by_id": "balen",
                "source_message_id": "user-request-7001",
                "created_by_service": "decision-gateway",
                "parent_intent_id": False,
            },
        ),
    ),
)
def test_trade_intent_and_outbox_persist_raw_message_authorization(
    raw_message,
    expected,
) -> None:
    cursor = RecordingCursor(raw_message)
    decision_id = str(uuid4())
    risk_decision_id = str(uuid4())
    row = {
        "decision_id": decision_id,
        "raw_message_id": str(uuid4()),
        "action": "open_position",
    }
    outcome = SimpleNamespace(
        account_id="account-a",
        instrument_id="BTCUSDT",
        risk_budget={
            "risk_fraction": 0.01,
            "max_notional": 100.0,
            "max_leverage": 3.0,
        },
    )
    decision = {
        "intent": {
            "side": "long",
            "entry": {"type": "market"},
            "stop_loss": 90000.0,
            "take_profits": [110000.0],
            "leverage": 3.0,
            "target_position_id": None,
            "valid_until": None,
        }
    }
    policy = SimpleNamespace(intent_ttl_seconds=900)

    intent_id = gateway._write_trade_intent(
        cursor,
        row,
        outcome,
        decision,
        policy,
        risk_decision_id,
    )

    assert intent_id
    trade_insert = next(
        params
        for sql, params in cursor.executions
        if sql.startswith("INSERT INTO trade_intents")
    )
    outbox_insert = next(
        params
        for sql, params in cursor.executions
        if sql.startswith("INSERT INTO outbox_events")
    )
    assert _json_value(trade_insert[6])["authorization"] == expected
    assert _json_value(outbox_insert[2])["authorization"] == expected
