from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import Event, get_ident
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from data_client.approved_intent_client import (
    ApprovedIntentDataClient,
    JsonIntentOffsetStore,
)
from data_client.durable_intent_inbox import JsonDurableIntentInbox
from execution_domain.contracts import (
    ApprovedTradeIntentV1,
    IntentAction,
    RiskBudget,
)
from execution_domain.control_plane import (
    IntentAckStatus,
    IntentItem,
    TradingState,
)
from execution_domain.testing import InMemoryControlPlane

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[ApprovedTradeIntentV1] = []

    def publish(self, intent: ApprovedTradeIntentV1) -> None:
        self.published.append(intent)


class _BlockingInbox(JsonDurableIntentInbox):
    def __init__(
        self,
        path: Path,
        started: Event,
        release: Event,
    ) -> None:
        self._started = started
        self._release = release
        super().__init__(path)

    def _write_records(self, records: dict[str, Any]) -> None:
        self._started.set()
        self._release.wait(timeout=5.0)
        super()._write_records(records)


class _FailingInbox(JsonDurableIntentInbox):
    def _write_records(self, records: dict[str, Any]) -> None:
        del records
        raise OSError("disk unavailable")


def test_accepted_ack_and_cursor_follow_durable_inbox_receipt(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent()
    control_plane.add_intent(
        "account-a",
        IntentItem(cursor="cursor-1", intent=intent),
    )
    publisher = _RecordingPublisher()
    started = Event()
    release = Event()
    inbox_path = tmp_path / "intent-inbox.json"
    client = ApprovedIntentDataClient(
        account_id="account-a",
        node_id="node-a",
        source=control_plane,
        publisher=publisher,
        offset_store=JsonIntentOffsetStore(
            tmp_path / "intent-offset.json"
        ),
        intent_inbox=_BlockingInbox(
            inbox_path,
            started,
            release,
        ),
        now=lambda: NOW,
        trading_state=lambda: TradingState.ACTIVE,
    )
    try:
        assert client.poll_once() == 1
        assert started.wait(timeout=1.0)
        assert control_plane.intent_acks == []
        assert publisher.published == []
        assert not (tmp_path / "intent-offset.json").exists()

        release.set()
        assert client.wait_for_durable_inbox(timeout_seconds=1.0)
        payload = json.loads(inbox_path.read_text(encoding="utf-8"))
        offset = json.loads(
            (tmp_path / "intent-offset.json").read_text(
                encoding="utf-8"
            )
        )

        assert sorted(payload["records"]) == [str(intent.intent_id)]
        assert offset["last_cursor"] == "cursor-1"
        assert control_plane.intent_acks == [
            (
                "account-a",
                "node-a",
                intent.intent_id,
                IntentAckStatus.ACCEPTED,
                None,
            )
        ]
        assert publisher.published == []

        assert client.drain_intent_delivery_mailbox() == 1
        assert publisher.published == [intent]
    finally:
        release.set()
        client.durable_inbox_cleanup_worker().stop(
            timeout_seconds=1.0
        )


def test_restart_replays_accepted_intent_before_fetching_after_cursor(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent()
    control_plane.add_intent(
        "account-a",
        IntentItem(cursor="cursor-1", intent=intent),
    )
    first_publisher = _RecordingPublisher()
    first = _client(tmp_path, control_plane, first_publisher)
    assert first.poll_once() == 1
    assert first.wait_for_durable_inbox(timeout_seconds=1.0)
    assert first_publisher.published == []
    first.durable_inbox_cleanup_worker().stop(
        timeout_seconds=1.0
    )

    replay_publisher = _RecordingPublisher()
    restarted = _client(tmp_path, control_plane, replay_publisher)
    try:
        assert restarted.poll_once() == 0
        assert restarted.wait_for_durable_inbox(
            timeout_seconds=1.0
        )
        assert restarted.drain_intent_delivery_mailbox() == 1

        assert replay_publisher.published == [intent]
        assert control_plane.intent_acks[-1] == (
            "account-a",
            "node-a",
            intent.intent_id,
            IntentAckStatus.ACCEPTED,
            None,
        )
    finally:
        restarted.durable_inbox_cleanup_worker().stop(
            timeout_seconds=1.0
        )


def test_actor_terminal_receipt_removes_replay_record_on_worker(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent()
    control_plane.add_intent(
        "account-a",
        IntentItem(cursor="cursor-1", intent=intent),
    )
    publisher = _RecordingPublisher()
    client = _client(tmp_path, control_plane, publisher)
    try:
        assert client.poll_once() == 1
        assert client.wait_for_durable_inbox(timeout_seconds=1.0)
        assert client.record_execution_terminal(
            intent.intent_id,
            "EXCHANGE_CONFIRMED",
            "",
        )
        assert client.wait_for_durable_inbox(timeout_seconds=1.0)

        inbox = JsonDurableIntentInbox(
            tmp_path / "intent-inbox.json"
        )
        assert inbox.pending() == ()
    finally:
        client.durable_inbox_cleanup_worker().stop(
            timeout_seconds=1.0
        )


def test_ack_timeout_is_degraded_and_does_not_block_delivery(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent()
    control_plane.add_intent(
        "account-a",
        IntentItem(cursor="cursor-1", intent=intent),
    )

    def failing_ack(**_kwargs: Any) -> None:
        raise TimeoutError("temporary ACK timeout")

    control_plane.ack_intent = failing_ack  # type: ignore[method-assign]
    publisher = _RecordingPublisher()
    client = _client(tmp_path, control_plane, publisher)
    try:
        assert client.poll_once() == 1
        assert client.wait_for_durable_inbox(
            timeout_seconds=1.0
        )
        assert client.drain_intent_delivery_mailbox() == 1

        assert publisher.published == [intent]
        assert any(
            "ACK failed and remains recoverable" in reason
            for reason in client.degraded_reasons
        )
    finally:
        client.durable_inbox_cleanup_worker().stop(
            timeout_seconds=1.0
        )


def test_durable_inbox_write_failure_is_fatal_on_actor_thread(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent()
    control_plane.add_intent(
        "account-a",
        IntentItem(cursor="cursor-1", intent=intent),
    )
    actor_thread_id = get_ident()
    fatal_threads: list[int] = []
    client = ApprovedIntentDataClient(
        account_id="account-a",
        node_id="node-a",
        source=control_plane,
        publisher=_RecordingPublisher(),
        offset_store=JsonIntentOffsetStore(
            tmp_path / "intent-offset.json"
        ),
        intent_inbox=_FailingInbox(
            tmp_path / "intent-inbox.json"
        ),
        now=lambda: NOW,
        trading_state=lambda: TradingState.ACTIVE,
    )
    client.set_durable_inbox_fatal_handler(
        lambda _reason: fatal_threads.append(get_ident())
    )
    try:
        assert client.poll_once() == 1
        assert client.wait_for_durable_inbox(
            timeout_seconds=1.0
        )
        assert fatal_threads == []

        client.drain_intent_delivery_mailbox()

        assert fatal_threads == [actor_thread_id]
    finally:
        client.durable_inbox_cleanup_worker().stop(
            timeout_seconds=1.0
        )


def _client(
    tmp_path: Path,
    control_plane: InMemoryControlPlane,
    publisher: _RecordingPublisher,
) -> ApprovedIntentDataClient:
    return ApprovedIntentDataClient(
        account_id="account-a",
        node_id="node-a",
        source=control_plane,
        publisher=publisher,
        offset_store=JsonIntentOffsetStore(
            tmp_path / "intent-offset.json"
        ),
        intent_inbox=JsonDurableIntentInbox(
            tmp_path / "intent-inbox.json"
        ),
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
        account_id="account-a",
        instrument_id="BTCUSDT-PERP.BINANCE",
        action=IntentAction.OPEN_POSITION,
        order_plan={
            "type": "limit",
            "side": "buy",
            "price": "26000",
            "quantity": "0.1",
        },
        risk_budget=RiskBudget(
            risk_fraction=0.01,
            max_notional=100.0,
            max_leverage=2.0,
        ),
        target_position_id=None,
        valid_until=NOW + timedelta(minutes=5),
        idempotency_key=sha256(
            str(intent_id).encode("ascii")
        ).hexdigest(),
        approved_at=NOW,
    )
