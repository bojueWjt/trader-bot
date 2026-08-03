from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from projection.contracts import ApprovedTradeIntentV1, IntentAction  # noqa: E402


def _intent_payload() -> dict:
    now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    return {
        "schema_version": "1.0",
        "intent_id": str(uuid4()),
        "decision_id": str(uuid4()),
        "risk_decision_id": str(uuid4()),
        "account_id": "account-a",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "action": "open_position",
        "order_plan": {"side": "buy", "type": "limit"},
        "execution": {"order_type": "limit", "limit_price": "65000"},
        "protection": {
            "stop": {"type": "stop_market", "price": "62000"},
            "take_profits": [
                {"price": "68000", "fraction": "0.5"},
                {"r_multiple": "2", "fraction": "0.5"},
            ],
        },
        "sizing": {"quantity": "0.01"},
        "risk_budget": {
            "risk_fraction": 0.01,
            "max_notional": 650,
            "max_leverage": 3,
        },
        "valid_until": now + timedelta(minutes=5),
        "idempotency_key": "a" * 64,
        "approved_at": now,
    }


def test_projection_contract_accepts_om_extensions_and_cancel_order() -> None:
    payload = _intent_payload()
    model = ApprovedTradeIntentV1.model_validate(payload)

    assert model.execution is not None
    assert model.protection is not None
    assert model.sizing is not None
    assert IntentAction("cancel_order") is IntentAction.CANCEL_ORDER


def test_projection_contract_rejects_invalid_management_sizing() -> None:
    payload = _intent_payload()
    payload["action"] = "close_position"

    with pytest.raises(ValidationError, match="management actions must not carry sizing"):
        ApprovedTradeIntentV1.model_validate(payload)
