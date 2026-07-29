from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.hk_nautilus

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
ADAPTER_ROOT = REPO_ROOT / "packages" / "nautilus-adapter"
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))
sys.path.insert(0, str(ADAPTER_ROOT))

pytest.importorskip("nautilus_trader")

from execution_domain.contracts import ApprovedTradeIntentV1, IntentAction, RiskBudget  # noqa: E402
from intent.custom_data import (  # noqa: E402
    APPROVED_TRADE_INTENT_DATA_TYPE,
    ApprovedTradeIntentCustomData,
    NautilusCustomDataPublisher,
    build_nautilus_custom_data,
)


def test_approved_trade_intent_custom_data_builds_and_publishes_on_hk() -> None:
    intent = _intent()
    custom_data = build_nautilus_custom_data(intent)

    assert custom_data is not None
    assert getattr(custom_data, "data_type") is not None
    assert custom_data.data.payload["data_type"] == APPROVED_TRADE_INTENT_DATA_TYPE

    engine = _RecordingDataEngine()
    NautilusCustomDataPublisher(data_engine=engine).publish(intent)

    decoded = ApprovedTradeIntentCustomData.from_wire(engine.published[0].data.payload)
    assert decoded.intent == intent


def _intent() -> ApprovedTradeIntentV1:
    intent_id = uuid4()
    now = datetime(2026, 6, 19, 12, tzinfo=timezone.utc)
    return ApprovedTradeIntentV1(
        schema_version="1.0",
        intent_id=intent_id,
        decision_id=uuid4(),
        risk_decision_id=uuid4(),
        account_id="account-a",
        instrument_id="BTCUSDT-PERP.BINANCE",
        action=IntentAction.ADD_POSITION,
        order_plan={"type": "market", "side": "buy", "quantity": "0.001"},
        risk_budget=RiskBudget(
            risk_fraction=0.01,
            max_notional=100.0,
            max_leverage=2.0,
        ),
        valid_until=now + timedelta(minutes=5),
        idempotency_key=sha256(str(intent_id).encode("ascii")).hexdigest(),
        approved_at=now,
    )


class _RecordingDataEngine:
    def __init__(self) -> None:
        self.published: list[object] = []

    def process(self, data: object) -> None:
        self.published.append(data)
