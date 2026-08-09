from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from data_client.approved_intent_client import (  # noqa: E402
    ApprovedIntentDataClient,
    JsonIntentOffsetStore,
)
from execution_domain.contracts import (  # noqa: E402
    ApprovedTradeIntentV1,
    IntentAction,
    RiskBudget,
)
from execution_domain.control_plane import (  # noqa: E402
    IntentAckStatus,
    IntentItem,
    TradingState,
)
from execution_domain.testing import InMemoryControlPlane  # noqa: E402

ACCOUNT_ID = "account-a"
NODE_ID = "node-a"
NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)


def test_fetch_once_returns_items_without_delivery_side_effects(
    tmp_path: Path,
) -> None:
    intent = _intent()
    item = IntentItem(cursor="cursor-1", intent=intent)
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    control_plane.add_intent(ACCOUNT_ID, item)
    publisher = _RecordingPublisher()
    offset_path = tmp_path / "intent-offset.json"
    client = _client(control_plane, publisher, offset_path)

    fetched = client.fetch_once(limit=1, wait_ms=25)

    assert fetched == (item,)
    assert publisher.published == []
    assert control_plane.intent_acks == []
    assert client.state.last_cursor is None
    assert offset_path.exists() is False


def test_deliver_publishes_acks_and_persists_one_fetched_item(
    tmp_path: Path,
) -> None:
    intent = _intent()
    item = IntentItem(cursor="cursor-1", intent=intent)
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    control_plane.add_intent(ACCOUNT_ID, item)
    publisher = _RecordingPublisher()
    offset_path = tmp_path / "intent-offset.json"
    client = _client(control_plane, publisher, offset_path)
    fetched = client.fetch_once()

    client.deliver(fetched[0])

    assert publisher.published == [intent]
    assert control_plane.intent_acks == [
        (
            ACCOUNT_ID,
            NODE_ID,
            intent.intent_id,
            IntentAckStatus.ACCEPTED,
            None,
        )
    ]
    assert client.state.last_cursor == "cursor-1"
    assert JsonIntentOffsetStore(offset_path).load().last_cursor == "cursor-1"


def test_poll_once_uses_fetch_and_deliver_public_seams(
    tmp_path: Path,
) -> None:
    intent = _intent()
    item = IntentItem(cursor="cursor-1", intent=intent)
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    publisher = _RecordingPublisher()
    client = _SeamRecordingClient(
        account_id=ACCOUNT_ID,
        node_id=NODE_ID,
        source=control_plane,
        publisher=publisher,
        offset_store=JsonIntentOffsetStore(tmp_path / "intent-offset.json"),
        now=lambda: NOW,
        trading_state=lambda: TradingState.ACTIVE,
        fetched=(item,),
    )

    count = client.poll_once(limit=7, wait_ms=11)

    assert count == 1
    assert client.fetch_calls == [(7, 11)]
    assert client.delivered == [item]
    assert publisher.published == []
    assert control_plane.intent_acks == []


def _client(
    control_plane: InMemoryControlPlane,
    publisher: _RecordingPublisher,
    offset_path: Path,
) -> ApprovedIntentDataClient:
    return ApprovedIntentDataClient(
        account_id=ACCOUNT_ID,
        node_id=NODE_ID,
        source=control_plane,
        publisher=publisher,
        offset_store=JsonIntentOffsetStore(offset_path),
        now=lambda: NOW,
        trading_state=lambda: TradingState.ACTIVE,
    )


def _intent() -> ApprovedTradeIntentV1:
    intent_id = uuid4()
    return ApprovedTradeIntentV1.model_construct(
        schema_version="1.0",
        intent_id=intent_id,
        decision_id=uuid4(),
        risk_decision_id=uuid4(),
        account_id=ACCOUNT_ID,
        instrument_id="BTCUSDT-PERP.BINANCE",
        action=IntentAction.ADD_POSITION,
        order_plan={
            "type": "market",
            "side": "buy",
            "quantity": "0.001",
        },
        risk_budget=RiskBudget(
            risk_fraction=0.01,
            max_notional=100.0,
            max_leverage=2.0,
        ),
        target_position_id=None,
        valid_until=NOW + timedelta(minutes=5),
        idempotency_key=sha256(str(intent_id).encode("ascii")).hexdigest(),
        approved_at=NOW - timedelta(seconds=5),
    )


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[ApprovedTradeIntentV1] = []

    def publish(self, intent: ApprovedTradeIntentV1) -> None:
        self.published.append(intent)


class _SeamRecordingClient(ApprovedIntentDataClient):
    def __init__(
        self,
        *args: Any,
        fetched: tuple[IntentItem, ...],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._fetched = fetched
        self.fetch_calls: list[tuple[int, int]] = []
        self.delivered: list[IntentItem] = []

    def fetch_once(
        self,
        limit: int = 100,
        wait_ms: int = 0,
    ) -> tuple[IntentItem, ...]:
        self.fetch_calls.append((limit, wait_ms))
        return self._fetched

    def deliver(self, item: IntentItem) -> None:
        self.delivered.append(item)
