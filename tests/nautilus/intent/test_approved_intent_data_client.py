from __future__ import annotations

import sys
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
ADAPTER_ROOT = REPO_ROOT / "packages" / "nautilus-adapter"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))
sys.path.insert(0, str(ADAPTER_ROOT))

from data_client.approved_intent_client import (  # noqa: E402
    ApprovedIntentDataClient,
    JsonIntentOffsetStore,
)
from execution_domain.contracts import ApprovedTradeIntentV1, IntentAction, RiskBudget  # noqa: E402
from execution_domain.control_plane import (  # noqa: E402
    IntentAckStatus,
    IntentItem,
    TradingState,
)
from execution_domain.testing import InMemoryControlPlane  # noqa: E402


ACCOUNT_ID = "account-a"
NODE_ID = "node-a"
NOW = datetime(2026, 6, 19, 12, tzinfo=timezone.utc)


def test_schema_mismatch_rejects_without_publish(tmp_path: Path) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(schema_version="9.9")  # type: ignore[arg-type]
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=intent))
    publisher = _RecordingPublisher()

    client = _client(tmp_path, control_plane, publisher)

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks == [
        (ACCOUNT_ID, NODE_ID, intent.intent_id, IntentAckStatus.REJECTED, "schema_mismatch")
    ]


def test_expired_intent_acks_expired_without_publish(tmp_path: Path) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(valid_until=NOW - timedelta(seconds=1))
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=intent))
    publisher = _RecordingPublisher()

    client = _client(tmp_path, control_plane, publisher)

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks == [
        (ACCOUNT_ID, NODE_ID, intent.intent_id, IntentAckStatus.EXPIRED, "expired")
    ]


def test_wrong_account_rejects_without_publish(tmp_path: Path) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(account_id="account-b")
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=intent))
    publisher = _RecordingPublisher()

    client = _client(tmp_path, control_plane, publisher)

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks == [
        (ACCOUNT_ID, NODE_ID, intent.intent_id, IntentAckStatus.REJECTED, "wrong_account")
    ]


def test_halted_node_rejects_new_position_intent(tmp_path: Path) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(action=IntentAction.OPEN_POSITION)
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=intent))
    publisher = _RecordingPublisher()

    client = _client(
        tmp_path,
        control_plane,
        publisher,
        trading_state=lambda: TradingState.HALTED,
    )

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks == [
        (ACCOUNT_ID, NODE_ID, intent.intent_id, IntentAckStatus.REJECTED, "halted")
    ]


def test_duplicate_intent_id_or_idempotency_key_does_not_publish_twice(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    first = _intent()
    same_intent_id = _intent(intent_id=first.intent_id)
    same_key = _intent(idempotency_key=first.idempotency_key)
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=first))
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c2", intent=same_intent_id))
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c3", intent=same_key))
    publisher = _RecordingPublisher()

    client = _client(tmp_path, control_plane, publisher)

    assert client.poll_once(limit=10) == 3
    assert [intent.intent_id for intent in publisher.published] == [first.intent_id]
    assert [ack[3] for ack in control_plane.intent_acks] == [
        IntentAckStatus.ACCEPTED,
        IntentAckStatus.DUPLICATE,
        IntentAckStatus.DUPLICATE,
    ]
    assert control_plane.intent_acks[1][4] == "duplicate_intent"
    assert control_plane.intent_acks[2][4] == "duplicate_idempotency_key"


def test_restart_resumes_after_safe_cursor_without_republishing(tmp_path: Path) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    first = _intent()
    second = _intent()
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=first))
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c2", intent=second))
    publisher = _RecordingPublisher()

    first_client = _client(tmp_path, control_plane, publisher)
    assert first_client.poll_once(limit=1) == 1

    restarted_client = _client(tmp_path, control_plane, publisher)
    assert restarted_client.poll_once(limit=10) == 1

    assert [intent.intent_id for intent in publisher.published] == [
        first.intent_id,
        second.intent_id,
    ]
    assert [ack[2] for ack in control_plane.intent_acks] == [
        first.intent_id,
        second.intent_id,
    ]


def _client(
    tmp_path: Path,
    control_plane: InMemoryControlPlane,
    publisher: _RecordingPublisher,
    trading_state=lambda: TradingState.ACTIVE,
) -> ApprovedIntentDataClient:
    return ApprovedIntentDataClient(
        account_id=ACCOUNT_ID,
        node_id=NODE_ID,
        source=control_plane,
        publisher=publisher,
        offset_store=JsonIntentOffsetStore(tmp_path / "intent-offset.json"),
        now=lambda: NOW,
        trading_state=trading_state,
    )


def _intent(**overrides: Any) -> ApprovedTradeIntentV1:
    intent_id = overrides.get("intent_id", uuid4())
    values: dict[str, Any] = {
        "schema_version": "1.0",
        "intent_id": intent_id,
        "decision_id": uuid4(),
        "risk_decision_id": uuid4(),
        "account_id": ACCOUNT_ID,
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "action": IntentAction.ADD_POSITION,
        "order_plan": {"type": "market", "side": "buy", "quantity": "0.001"},
        "risk_budget": RiskBudget(
            risk_fraction=0.01,
            max_notional=100.0,
            max_leverage=2.0,
        ),
        "target_position_id": None,
        "valid_until": NOW + timedelta(minutes=5),
        "idempotency_key": sha256(str(intent_id).encode("ascii")).hexdigest(),
        "approved_at": NOW - timedelta(seconds=5),
    }
    values.update(overrides)
    return ApprovedTradeIntentV1.model_construct(**values)


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[ApprovedTradeIntentV1] = []

    def publish(self, intent: ApprovedTradeIntentV1) -> None:
        self.published.append(intent)
