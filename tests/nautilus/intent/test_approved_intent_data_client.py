from __future__ import annotations

import sys
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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
    _flush(client)
    assert publisher.published == []
    assert control_plane.intent_acks == [
        (ACCOUNT_ID, NODE_ID, intent.intent_id, IntentAckStatus.REJECTED, "schema_mismatch")
    ]


def test_fetch_once_is_remote_read_only_and_reserves_pending_cursor(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent()
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=intent),
    )
    publisher = _RecordingPublisher()
    client = _client(tmp_path, control_plane, publisher)

    items = client.fetch_once()

    assert items == (IntentItem(cursor="c1", intent=intent),)
    assert client.fetch_once() == ()
    assert publisher.published == []
    assert control_plane.intent_acks == []
    assert not (tmp_path / "intent-offset.json").exists()
    assert not (tmp_path / "intent-offset.inbox.json").exists()


def test_deliver_owns_receipt_cursor_ack_and_publication(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent()
    item = IntentItem(cursor="c1", intent=intent)
    control_plane.add_intent(ACCOUNT_ID, item)
    original_ack = control_plane.ack_intent

    def ack(**kwargs: Any) -> None:
        events.append("ack")
        original_ack(**kwargs)

    control_plane.ack_intent = ack  # type: ignore[method-assign]
    publisher = _RecordingPublisher(events)
    client = ApprovedIntentDataClient(
        account_id=ACCOUNT_ID,
        node_id=NODE_ID,
        source=control_plane,
        publisher=publisher,
        offset_store=_RecordingOffsetStore(
            tmp_path / "intent-offset.json",
            events,
        ),
        intent_inbox=_RecordingInbox(
            tmp_path / "intent-inbox.json",
            events,
        ),
        now=lambda: NOW,
        trading_state=lambda: TradingState.ACTIVE,
    )

    fetched = client.fetch_once()
    assert fetched == (item,)
    client.deliver(item)

    assert events == [
        "receipt:RECEIVED",
        "offset",
        "receipt:PREPARED",
        "ack",
        "publish",
    ]
    assert publisher.published == [intent]


@pytest.mark.parametrize(
    ("intent_overrides", "trading_state"),
    [
        ({}, TradingState.ACTIVE),
        ({"schema_version": "9.9"}, TradingState.ACTIVE),
        ({"account_id": "account-b"}, TradingState.ACTIVE),
        (
            {"valid_until": NOW - timedelta(seconds=1)},
            TradingState.ACTIVE,
        ),
        ({"action": IntentAction.OPEN_POSITION}, TradingState.HALTED),
    ],
)
def test_ack_timeout_is_soft_for_every_intent_outcome(
    tmp_path: Path,
    intent_overrides: dict[str, Any],
    trading_state: TradingState,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(**intent_overrides)
    item = IntentItem(cursor="c1", intent=intent)
    control_plane.add_intent(ACCOUNT_ID, item)

    def failing_ack(**_kwargs: Any) -> None:
        raise TimeoutError("temporary ACK timeout")

    control_plane.ack_intent = failing_ack  # type: ignore[method-assign]
    publisher = _RecordingPublisher()
    client = _client(
        tmp_path,
        control_plane,
        publisher,
        trading_state=lambda: trading_state,
    )

    assert client.poll_once() == 1
    assert client.state.last_cursor == "c1"
    assert any(
        "ACK failed and remains recoverable" in reason
        for reason in client.degraded_reasons
    )


def test_pending_ack_retries_on_next_delivery_without_blocking_publish(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    first = _intent()
    second = _intent()
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=first),
    )
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c2", intent=second),
    )
    original_ack = control_plane.ack_intent
    attempts: list[Any] = []

    def flaky_ack(**kwargs: Any) -> None:
        attempts.append(kwargs["intent_id"])
        if len(attempts) == 1:
            raise TimeoutError("temporary ACK timeout")
        original_ack(**kwargs)

    control_plane.ack_intent = flaky_ack  # type: ignore[method-assign]
    publisher = _RecordingPublisher()
    client = _client(tmp_path, control_plane, publisher)
    items = client.fetch_once(limit=2)

    client.deliver(items[0])
    assert publisher.published == [first]
    assert client.pending_ack_count == 1

    client.deliver(items[1])
    assert publisher.published == [first, second]
    assert client.pending_ack_count == 0
    assert [ack[2] for ack in control_plane.intent_acks] == [
        first.intent_id,
        second.intent_id,
    ]


def test_pending_ack_ledger_preserves_all_failures_without_blocking_publish(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    first = _intent()
    second = _intent()
    items = (
        IntentItem(cursor="c1", intent=first),
        IntentItem(cursor="c2", intent=second),
    )
    for item in items:
        control_plane.add_intent(ACCOUNT_ID, item)

    def failing_ack(**_kwargs: Any) -> None:
        raise TimeoutError("temporary ACK timeout")

    control_plane.ack_intent = failing_ack  # type: ignore[method-assign]
    publisher = _RecordingPublisher()
    client = _client(tmp_path, control_plane, publisher)

    fetched = client.fetch_once(limit=2)
    client.deliver(fetched[0])
    client.deliver(fetched[1])

    assert publisher.published == [first, second]
    assert client.pending_ack_count == 2


def test_pending_ack_retries_during_idle_poll(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(valid_until=NOW - timedelta(seconds=1))
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=intent),
    )
    original_ack = control_plane.ack_intent
    attempts = 0

    def flaky_ack(**kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("temporary ACK timeout")
        original_ack(**kwargs)

    control_plane.ack_intent = flaky_ack  # type: ignore[method-assign]
    client = _client(
        tmp_path,
        control_plane,
        _RecordingPublisher(),
    )

    assert client.poll_once() == 1
    assert client.pending_ack_count == 1
    assert client.poll_once() == 0
    assert client.pending_ack_count == 0
    assert attempts == 2


@pytest.mark.parametrize(
    "raw_intent",
    [
        {},
        {"intent_id": "not-a-uuid"},
    ],
)
def test_poison_intent_advances_cursor_without_blocking_stream(
    tmp_path: Path,
    raw_intent: dict[str, Any],
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    item = IntentItem(cursor="poison-cursor", intent=raw_intent)
    control_plane.add_intent(ACCOUNT_ID, item)
    client = _client(
        tmp_path,
        control_plane,
        _RecordingPublisher(),
    )

    assert client.poll_once() == 1
    assert client.state.last_cursor == "poison-cursor"
    assert control_plane.intent_acks == []
    assert any(
        "poison intent skipped" in reason
        for reason in client.degraded_reasons
    )


def test_fetch_once_honors_capacity_when_source_overproduces(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    first = IntentItem(cursor="c1", intent=_intent())
    second = IntentItem(cursor="c2", intent=_intent())

    def fetch_intents(**kwargs: Any) -> Any:
        after_cursor = kwargs["after_cursor"]
        if after_cursor == "c1":
            return SimpleNamespace(items=(second,))
        return SimpleNamespace(items=(first, second))

    control_plane.fetch_intents = fetch_intents  # type: ignore[method-assign]
    publisher = _RecordingPublisher()
    client = _client(tmp_path, control_plane, publisher)

    assert client.fetch_once(limit=1) == (first,)
    client.deliver(first)
    assert client.fetch_once(limit=1) == (second,)


@pytest.mark.parametrize(
    "failure_stage",
    ["receive", "offset", "prepare"],
)
def test_durable_prepare_failure_invokes_fatal_handler(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent()
    item = IntentItem(cursor="c1", intent=intent)
    control_plane.add_intent(ACCOUNT_ID, item)
    publisher = _RecordingPublisher()
    events: list[str] = []
    inbox = _FailingInbox(
        tmp_path / "intent-inbox.json",
        events,
        failure_stage=failure_stage,
    )
    offset_store = _FailingOffsetStore(
        tmp_path / "intent-offset.json",
        events,
        fail=failure_stage == "offset",
    )
    client = ApprovedIntentDataClient(
        account_id=ACCOUNT_ID,
        node_id=NODE_ID,
        source=control_plane,
        publisher=publisher,
        offset_store=offset_store,
        intent_inbox=inbox,
        now=lambda: NOW,
        trading_state=lambda: TradingState.ACTIVE,
    )
    fatal_reasons: list[str] = []
    client.set_durable_inbox_fatal_handler(fatal_reasons.append)

    with pytest.raises(OSError, match="durable write failed"):
        client.deliver(item)

    assert len(fatal_reasons) == 1
    assert "durable intent inbox failed" in fatal_reasons[0]
    assert publisher.published == []


def test_terminal_receipt_queue_rejection_invokes_fatal_handler(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    client = _client(
        tmp_path,
        control_plane,
        _RecordingPublisher(),
    )
    fatal_reasons: list[str] = []
    client.set_durable_inbox_fatal_handler(fatal_reasons.append)

    def reject(_task: Any) -> bool:
        client._request_hard_failure("queue capacity exceeded")
        return False

    client.durable_inbox_cleanup_worker().submit = reject  # type: ignore[method-assign]

    assert client.record_execution_terminal(
        uuid4(),
        "CONFIRMED",
        "accepted",
    ) is False
    assert len(fatal_reasons) == 1
    assert "queue capacity exceeded" in fatal_reasons[0]


def test_expired_intent_acks_expired_without_publish(tmp_path: Path) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(valid_until=NOW - timedelta(seconds=1))
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=intent))
    publisher = _RecordingPublisher()

    client = _client(tmp_path, control_plane, publisher)

    assert client.poll_once() == 1
    _flush(client)
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
    _flush(client)
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
    _flush(client)
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
    _flush(client)
    assert [intent.intent_id for intent in publisher.published] == [first.intent_id]
    assert [ack[3] for ack in control_plane.intent_acks] == [
        IntentAckStatus.ACCEPTED,
        IntentAckStatus.DUPLICATE,
        IntentAckStatus.DUPLICATE,
    ]
    assert control_plane.intent_acks[1][4] == "duplicate_intent"
    assert control_plane.intent_acks[2][4] == "duplicate_idempotency_key"


def test_restart_replays_pending_receipt_then_resumes_after_cursor(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    first = _intent()
    second = _intent()
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=first))
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c2", intent=second))
    publisher = _RecordingPublisher()

    first_client = _client(tmp_path, control_plane, publisher)
    assert first_client.poll_once(limit=1) == 1
    _flush(first_client)
    first_client.durable_inbox_cleanup_worker().stop(
        timeout_seconds=1.0
    )

    restarted_client = _client(tmp_path, control_plane, publisher)
    replay = restarted_client.replay_pending()
    assert replay.commit(len(replay)) is True
    for item in replay:
        restarted_client.deliver(item)
    assert restarted_client.poll_once(limit=10) == 1
    _flush(restarted_client)

    assert [intent.intent_id for intent in publisher.published] == [
        first.intent_id,
        first.intent_id,
        second.intent_id,
    ]
    assert [ack[2] for ack in control_plane.intent_acks] == [
        first.intent_id,
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
    def __init__(self, events: list[str] | None = None) -> None:
        self.published: list[ApprovedTradeIntentV1] = []
        self._events = events

    def publish(self, intent: ApprovedTradeIntentV1) -> None:
        if self._events is not None:
            self._events.append("publish")
        self.published.append(intent)


class _RecordingInbox:
    def __init__(self, path: Path, events: list[str]) -> None:
        self.path = path
        self._events = events

    def receive(
        self,
        cursor: str,
        intent: ApprovedTradeIntentV1,
    ) -> None:
        del cursor, intent
        self._events.append("receipt:RECEIVED")

    def complete(
        self,
        intent_id: Any,
        status: str,
        detail: str,
    ) -> None:
        del intent_id, detail
        self._events.append(f"receipt:{status}")

    def pending(self) -> tuple[Any, ...]:
        return ()


class _FailingInbox(_RecordingInbox):
    def __init__(
        self,
        path: Path,
        events: list[str],
        *,
        failure_stage: str,
    ) -> None:
        super().__init__(path, events)
        self._failure_stage = failure_stage

    def receive(
        self,
        cursor: str,
        intent: ApprovedTradeIntentV1,
    ) -> None:
        if self._failure_stage == "receive":
            raise OSError("durable write failed")
        super().receive(cursor, intent)

    def complete(
        self,
        intent_id: Any,
        status: str,
        detail: str,
    ) -> None:
        if self._failure_stage == "prepare" and status == "PREPARED":
            raise OSError("durable write failed")
        super().complete(intent_id, status, detail)


class _RecordingOffsetStore(JsonIntentOffsetStore):
    def __init__(self, path: Path, events: list[str]) -> None:
        super().__init__(path)
        self._events = events

    def save(self, state: Any) -> None:
        self._events.append("offset")
        super().save(state)


class _FailingOffsetStore(_RecordingOffsetStore):
    def __init__(
        self,
        path: Path,
        events: list[str],
        *,
        fail: bool,
    ) -> None:
        super().__init__(path, events)
        self._fail = fail

    def save(self, state: Any) -> None:
        if self._fail:
            raise OSError("durable write failed")
        super().save(state)


def _flush(client: ApprovedIntentDataClient) -> None:
    assert client.wait_for_durable_inbox(timeout_seconds=1.0)
    client.drain_intent_delivery_mailbox()
