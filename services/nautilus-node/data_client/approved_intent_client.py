from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock
from typing import Any, Callable, Optional, Protocol
from uuid import UUID

from pydantic import ValidationError

from data_client.durable_intent_inbox import JsonDurableIntentInbox
from execution_domain.contracts import (
    ApprovedTradeIntentV1,
    IntentAction,
)
from execution_domain.control_plane import (
    ControlPlaneIntentSource,
    IntentAckStatus,
    IntentItem,
    TradingState,
)
from runtime.bounded_task_worker import BoundedTaskWorker


class IntentPublisher(Protocol):
    def publish(self, intent: ApprovedTradeIntentV1) -> None: ...


@dataclass
class IntentOffsetState:
    last_cursor: Optional[str] = None
    processed_intents: set[str] = field(default_factory=set)
    processed_idempotency_keys: set[str] = field(default_factory=set)

    def intent_key(self, intent: ApprovedTradeIntentV1) -> str:
        return f"{intent.account_id}:{intent.intent_id}"

    def has_processed(self, intent: ApprovedTradeIntentV1) -> Optional[str]:
        if self.intent_key(intent) in self.processed_intents:
            return "duplicate_intent"
        if intent.idempotency_key in self.processed_idempotency_keys:
            return "duplicate_idempotency_key"
        return None

    def record_processed(self, intent: ApprovedTradeIntentV1) -> None:
        self.processed_intents.add(self.intent_key(intent))
        self.processed_idempotency_keys.add(intent.idempotency_key)


class JsonIntentOffsetStore:
    """Small durable cursor store for one account/node intent stream."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> IntentOffsetState:
        if not self._path.exists():
            return IntentOffsetState()
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return IntentOffsetState(
            last_cursor=raw.get("last_cursor"),
            processed_intents=set(raw.get("processed_intents", [])),
            processed_idempotency_keys=set(raw.get("processed_idempotency_keys", [])),
        )

    def save(self, state: IntentOffsetState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_cursor": state.last_cursor,
            "processed_intents": sorted(state.processed_intents),
            "processed_idempotency_keys": sorted(state.processed_idempotency_keys),
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(payload, tmp, sort_keys=True)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self._path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


class ApprovedIntentDataClient:
    """Consumes approved trade intents from control-plane into Nautilus data."""

    _NEW_POSITION_ACTIONS = {
        IntentAction.OPEN_POSITION,
        IntentAction.ADD_POSITION,
    }
    _DURABLE_INBOX_CAPACITY = 256
    _DURABLE_INBOX_TASK_TIMEOUT_SECONDS = 2.0

    def __init__(
        self,
        account_id: str,
        node_id: str,
        source: ControlPlaneIntentSource,
        publisher: IntentPublisher,
        offset_store: JsonIntentOffsetStore,
        intent_inbox: Optional[JsonDurableIntentInbox] = None,
        now: Optional[Callable[[], datetime]] = None,
        trading_state: Optional[Callable[[], TradingState]] = None,
    ) -> None:
        self._account_id = account_id
        self._node_id = node_id
        self._source = source
        self._publisher = publisher
        self._offset_store = offset_store
        self._state = offset_store.load()
        inbox = intent_inbox
        if inbox is None:
            inbox_path = offset_store.path.with_suffix(
                ".inbox.json"
            )
            inbox = JsonDurableIntentInbox(inbox_path)
        self._intent_inbox = inbox
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._trading_state = trading_state or (lambda: TradingState.HALTED)
        self._delivery_mailbox: Queue[ApprovedTradeIntentV1] = Queue(
            maxsize=self._DURABLE_INBOX_CAPACITY
        )
        self._degraded_lock = Lock()
        self._degraded_reasons: list[str] = []
        self._hard_failure_lock = Lock()
        self._hard_failure_requested_reason = ""
        self._hard_failure_reason = ""
        self._hard_failure_handler: Callable[[str], None] | None = None
        self._hard_failure_mailbox: Queue[str] = Queue(maxsize=1)
        self._replay_enqueued = False
        worker_name = f"{node_id}.durable-intent-inbox"
        self._durable_inbox_worker = BoundedTaskWorker(
            worker_name,
            self._process_durable_inbox_task,
            capacity=self._DURABLE_INBOX_CAPACITY,
            task_timeout_seconds=(
                self._DURABLE_INBOX_TASK_TIMEOUT_SECONDS
            ),
            on_overflow=self._request_hard_failure,
            on_error=self._request_hard_failure,
            on_timeout=self._request_hard_failure,
        )

    @property
    def state(self) -> IntentOffsetState:
        return self._state

    def poll_once(self, limit: int = 100, wait_ms: int = 0) -> int:
        items = self.fetch_once(limit=limit, wait_ms=wait_ms)
        for item in items:
            self.deliver(item)
        return len(items)

    def fetch_once(
        self,
        limit: int = 100,
        wait_ms: int = 0,
    ) -> tuple[IntentItem, ...]:
        batch = self._source.fetch_intents(
            account_id=self._account_id,
            after_cursor=self._state.last_cursor,
            limit=limit,
            wait_ms=wait_ms,
        )
        return tuple(batch.items)

    def deliver(self, item: IntentItem) -> None:
        self._process_item(item)

    def wait_for_durable_inbox(
        self,
        *,
        timeout_seconds: float,
    ) -> bool:
        return self._durable_inbox_worker.wait_empty(
            timeout_seconds=timeout_seconds
        )

    def drain_intent_delivery_mailbox(
        self,
        *,
        max_items: int = 32,
    ) -> int:
        if max_items < 1:
            raise ValueError("max_items must be positive")
        self._drain_hard_failure_mailbox()
        drained = 0
        while drained < max_items:
            try:
                intent = self._delivery_mailbox.get_nowait()
            except Empty:
                break
            try:
                self._publisher.publish(intent)
            finally:
                self._delivery_mailbox.task_done()
            drained += 1
        return drained

    def record_execution_terminal(
        self,
        intent_id: UUID | str,
        status: str,
        detail: str,
    ) -> bool:
        self._start_durable_inbox_lane()
        return self._durable_inbox_worker.submit(
            _IntentInboxTask(
                kind="complete",
                intent_id=str(intent_id),
                status=str(status),
                detail=str(detail),
            )
        )

    def durable_inbox_cleanup_worker(
        self,
    ) -> BoundedTaskWorker["_IntentInboxTask"]:
        return self._durable_inbox_worker

    def set_durable_inbox_fatal_handler(
        self,
        handler: Callable[[str], None] | None,
    ) -> None:
        self._hard_failure_handler = handler

    @property
    def degraded_reasons(self) -> tuple[str, ...]:
        with self._degraded_lock:
            return tuple(self._degraded_reasons)

    def _start_durable_inbox_lane(self) -> None:
        if not self._durable_inbox_worker.snapshot().running:
            self._durable_inbox_worker.start()
        if self._replay_enqueued:
            return
        self._replay_enqueued = True
        for receipt in self._intent_inbox.pending():
            accepted = self._durable_inbox_worker.submit(
                _IntentInboxTask(
                    kind="replay",
                    cursor=receipt.cursor,
                    intent=receipt.intent,
                )
            )
            if not accepted:
                raise RuntimeError(
                    "durable intent replay queue capacity exceeded"
                )

    def _process_durable_inbox_task(
        self,
        task: "_IntentInboxTask",
    ) -> None:
        if task.kind == "process":
            item = task.item
            if not isinstance(item, IntentItem):
                raise ValueError(
                    "durable intent process task requires an item"
                )
            self._process_item(item)
            return
        if task.kind == "replay":
            intent = task.intent
            if not isinstance(intent, ApprovedTradeIntentV1):
                raise ValueError(
                    "durable intent replay requires an intent"
                )
            self._ack_degraded(
                intent.intent_id,
                IntentAckStatus.ACCEPTED,
                None,
            )
            self._enqueue_delivery(intent)
            return
        if task.kind == "complete":
            self._intent_inbox.complete(
                task.intent_id,
                task.status,
                task.detail,
            )
            return
        raise ValueError(
            f"unsupported durable intent inbox task: {task.kind}"
        )

    def _process_item(self, item: IntentItem) -> None:
        intent_id = _extract_intent_id(item)
        intent = self._validate_intent(item)
        if intent is None:
            self._ack(intent_id, IntentAckStatus.REJECTED, "schema_mismatch")
            self._advance_cursor(item.cursor)
            return

        if intent.account_id != self._account_id:
            self._ack(intent.intent_id, IntentAckStatus.REJECTED, "wrong_account")
            self._advance_cursor(item.cursor)
            return

        if _as_aware(intent.valid_until) <= self._now_aware():
            self._ack(intent.intent_id, IntentAckStatus.EXPIRED, "expired")
            self._advance_cursor(item.cursor)
            return

        duplicate_detail = self._state.has_processed(intent)
        if duplicate_detail is not None:
            self._ack(intent.intent_id, IntentAckStatus.DUPLICATE, duplicate_detail)
            self._advance_cursor(item.cursor)
            return

        if (
            TradingState(self._trading_state()) is TradingState.HALTED
            and intent.action in self._NEW_POSITION_ACTIONS
        ):
            self._ack(intent.intent_id, IntentAckStatus.REJECTED, "halted")
            self._advance_cursor(item.cursor)
            return

        self._intent_inbox.receive(item.cursor, intent)
        candidate = IntentOffsetState(
            last_cursor=item.cursor,
            processed_intents=set(self._state.processed_intents),
            processed_idempotency_keys=set(
                self._state.processed_idempotency_keys
            ),
        )
        candidate.record_processed(intent)
        self._offset_store.save(candidate)
        self._state = candidate
        self._ack_degraded(
            intent.intent_id,
            IntentAckStatus.ACCEPTED,
            None,
        )
        self._enqueue_delivery(intent)

    def _enqueue_delivery(
        self,
        intent: ApprovedTradeIntentV1,
    ) -> None:
        try:
            self._delivery_mailbox.put_nowait(intent)
        except Full as exc:
            raise RuntimeError(
                "durable intent delivery queue capacity exceeded"
            ) from exc

    def _ack_degraded(
        self,
        intent_id: UUID,
        status: IntentAckStatus,
        detail: Optional[str],
    ) -> bool:
        try:
            self._ack(intent_id, status, detail)
            return True
        except Exception as exc:
            self._record_degraded(
                "intent ACK failed and remains recoverable: "
                f"{exc!r}"
            )
            return False

    def _record_degraded(self, reason: str) -> None:
        value = str(reason).strip()
        if not value:
            return
        with self._degraded_lock:
            self._degraded_reasons.append(value)

    def _request_hard_failure(self, reason: str) -> None:
        failure_reason = (
            "durable intent inbox failed: " + str(reason).strip()
        )
        with self._hard_failure_lock:
            if (
                self._hard_failure_reason
                or self._hard_failure_requested_reason
            ):
                return
            self._hard_failure_requested_reason = failure_reason
        try:
            self._hard_failure_mailbox.put_nowait(failure_reason)
        except Full:
            pass

    def _drain_hard_failure_mailbox(self) -> None:
        try:
            failure_reason = (
                self._hard_failure_mailbox.get_nowait()
            )
        except Empty:
            return
        try:
            with self._hard_failure_lock:
                if self._hard_failure_reason:
                    return
                self._hard_failure_reason = failure_reason
            handler = self._hard_failure_handler
            if handler is not None:
                handler(failure_reason)
        finally:
            self._hard_failure_mailbox.task_done()

    def _validate_intent(self, item: IntentItem) -> Optional[ApprovedTradeIntentV1]:
        try:
            payload = _intent_payload(item.intent)
            return ApprovedTradeIntentV1.model_validate(payload)
        except (TypeError, ValueError, ValidationError):
            return None

    def _ack(
        self,
        intent_id: UUID,
        status: IntentAckStatus,
        detail: Optional[str],
    ) -> None:
        self._source.ack_intent(
            account_id=self._account_id,
            node_id=self._node_id,
            intent_id=intent_id,
            status=status,
            detail=detail,
        )

    def _advance_cursor(self, cursor: str) -> None:
        self._state.last_cursor = cursor
        self._offset_store.save(self._state)

    def _now_aware(self) -> datetime:
        return _as_aware(self._now())


def _intent_payload(raw: object) -> object:
    if isinstance(raw, ApprovedTradeIntentV1):
        return raw.model_dump(mode="json")
    return raw


def _extract_intent_id(item: IntentItem) -> UUID:
    raw_intent = item.intent
    if isinstance(raw_intent, ApprovedTradeIntentV1):
        return raw_intent.intent_id
    if isinstance(raw_intent, dict) and "intent_id" in raw_intent:
        return UUID(str(raw_intent["intent_id"]))
    intent_id = getattr(raw_intent, "intent_id", None)
    if intent_id is not None:
        return UUID(str(intent_id))
    raise ValueError("cannot ack invalid intent without intent_id")


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class _IntentInboxTask:
    kind: str
    item: Any = False
    cursor: str = ""
    intent: Any = False
    intent_id: str = ""
    status: str = ""
    detail: str = ""
