from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock
from typing import Any, Callable, Iterator, Optional, Protocol
from uuid import UUID

from pydantic import ValidationError

from data_client.atomic_json import write_json_atomic
from data_client.durable_intent_inbox import (
    DurableIntentResubmitClaim,
    JsonDurableIntentInbox,
)
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


class IntentReplayPage:
    """A replay page whose cursor advances after delivery-lane admission."""

    def __init__(
        self,
        items: tuple[IntentItem, ...],
        commit: Callable[[int], bool],
    ) -> None:
        self._items = items
        self._commit = commit
        self._committed = False

    def __iter__(self) -> Iterator[IntentItem]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index: int) -> IntentItem:
        return self._items[index]

    def commit(self, accepted_count: int) -> bool:
        if self._committed:
            raise RuntimeError("intent replay page already committed")
        replay_complete = self._commit(int(accepted_count))
        self._committed = True
        return replay_complete


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
        payload = {
            "last_cursor": state.last_cursor,
            "processed_intents": sorted(state.processed_intents),
            "processed_idempotency_keys": sorted(state.processed_idempotency_keys),
        }
        write_json_atomic(self._path, payload)


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
        self._degraded_lock = Lock()
        self._degraded_reasons: list[str] = []
        self._pending_ack_lock = Lock()
        self._pending_acks: OrderedDict[
            tuple[str, str, str],
            tuple[UUID, IntentAckStatus, Optional[str]],
        ] = OrderedDict()
        self._pending_lock = Lock()
        self._pending_cursors: set[str] = set()
        self._replay_receipts = tuple(self._intent_inbox.pending())
        self._replay_lock = Lock()
        self._replay_offset = 0
        self._active_replay_page: object | bool = False
        self._hard_failure_lock = Lock()
        self._hard_failure_requested_reason = ""
        self._hard_failure_reason = ""
        self._hard_failure_handler: Callable[[str], None] | None = None
        self._hard_failure_mailbox: Queue[str] = Queue(maxsize=1)
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

    def intent_receipt_status(
        self,
        intent_id: UUID | str,
    ) -> str | bool:
        status_getter = getattr(
            self._intent_inbox,
            "status",
            None,
        )
        if not callable(status_getter):
            receipt = self._intent_inbox.get(intent_id)
            if receipt is None:
                return False
            return str(receipt.status)
        return status_getter(intent_id)

    def poll_once(self, limit: int = 100, wait_ms: int = 0) -> int:
        self._raise_if_hard_failure()
        items = self.fetch_once(limit=limit, wait_ms=wait_ms)
        for index, item in enumerate(items):
            try:
                self.deliver(item)
            except BaseException:
                with self._pending_lock:
                    for pending in items[index:]:
                        self._pending_cursors.discard(
                            str(pending.cursor)
                        )
                raise
        return len(items)

    def fetch_once(
        self,
        limit: int = 100,
        wait_ms: int = 0,
    ) -> tuple[IntentItem, ...]:
        self._raise_if_hard_failure()
        self._retry_pending_acks()
        if limit < 1:
            raise ValueError("limit must be positive")
        batch = self._source.fetch_intents(
            account_id=self._account_id,
            after_cursor=self._state.last_cursor,
            limit=limit,
            wait_ms=wait_ms,
        )
        accepted: list[IntentItem] = []
        with self._pending_lock:
            for item in batch.items:
                cursor = str(item.cursor)
                if cursor in self._pending_cursors:
                    continue
                self._pending_cursors.add(cursor)
                accepted.append(item)
                if len(accepted) >= limit:
                    break
        return tuple(accepted)

    def deliver(self, item: IntentItem) -> None:
        self._raise_if_hard_failure()
        self._retry_pending_acks()
        self._process_item(item)
        with self._pending_lock:
            self._pending_cursors.discard(str(item.cursor))

    def replay_pending(
        self,
        limit: int = 100,
    ) -> IntentReplayPage:
        self._raise_if_hard_failure()
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._replay_lock:
            if self._active_replay_page is not False:
                raise RuntimeError(
                    "intent replay page must be committed"
                )
            start = self._replay_offset
            end = min(
                start + int(limit),
                len(self._replay_receipts),
            )
            receipts = self._replay_receipts[start:end]
            items = tuple(
                IntentItem(
                    cursor=receipt.cursor,
                    intent=receipt.intent,
                )
                for receipt in receipts
            )
            if not items:
                return IntentReplayPage(
                    (),
                    lambda accepted_count: (
                        self._commit_empty_replay_page(
                            accepted_count
                        )
                    ),
                )
            token = object()
            self._active_replay_page = token
            with self._pending_lock:
                self._pending_cursors.update(
                    str(item.cursor) for item in items
                )
        return IntentReplayPage(
            items,
            lambda accepted_count: self._commit_replay_page(
                token,
                start,
                items,
                accepted_count,
            ),
        )

    def _commit_empty_replay_page(
        self,
        accepted_count: int,
    ) -> bool:
        if accepted_count != 0:
            raise ValueError(
                "empty intent replay page accepts zero receipts"
            )
        return True

    def _commit_replay_page(
        self,
        token: object,
        start: int,
        items: tuple[IntentItem, ...],
        accepted_count: int,
    ) -> bool:
        if accepted_count < 0 or accepted_count > len(items):
            raise ValueError(
                "accepted replay count exceeds page size"
            )
        with self._replay_lock:
            if self._active_replay_page is not token:
                raise RuntimeError("stale intent replay page")
            self._replay_offset = start + accepted_count
            self._active_replay_page = False
            rejected = items[accepted_count:]
            with self._pending_lock:
                for item in rejected:
                    self._pending_cursors.discard(
                        str(item.cursor)
                    )
            return self._replay_offset >= len(
                self._replay_receipts
            )

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
        return 0

    def record_execution_terminal(
        self,
        intent_id: UUID | str,
        status: str,
        detail: str,
    ) -> bool:
        self._drain_hard_failure_mailbox()
        self._start_durable_inbox_lane()
        accepted = self._durable_inbox_worker.submit(
            _IntentInboxTask(
                kind="complete",
                intent_id=str(intent_id),
                status=str(status),
                detail=str(detail),
            )
        )
        if not accepted:
            self._drain_hard_failure_mailbox()
        return accepted

    def persist_execution_receipt_transition(
        self,
        intent_id: UUID | str,
        expected_status: str,
        status: str,
        detail: str,
    ) -> DurableIntentResubmitClaim | bool:
        return self._intent_inbox.transition(
            intent_id,
            expected_status=expected_status,
            status=status,
            detail=detail,
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

    @property
    def pending_ack_count(self) -> int:
        with self._pending_ack_lock:
            return len(self._pending_acks)

    def _start_durable_inbox_lane(self) -> None:
        if not self._durable_inbox_worker.snapshot().running:
            self._durable_inbox_worker.start()

    def _process_durable_inbox_task(
        self,
        task: "_IntentInboxTask",
    ) -> None:
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
        intent = self._validate_intent(item)
        if intent is None:
            intent_id = _try_extract_intent_id(item)
            if intent_id is not False:
                self._ack_degraded(
                    intent_id,
                    IntentAckStatus.REJECTED,
                    "schema_mismatch",
                )
            else:
                self._record_degraded(
                    "poison intent skipped without acknowledgement: "
                    f"cursor={item.cursor}"
                )
            self._advance_cursor(item.cursor)
            return

        if intent.account_id != self._account_id:
            self._ack_degraded(
                intent.intent_id,
                IntentAckStatus.REJECTED,
                "wrong_account",
            )
            self._advance_cursor(item.cursor)
            return

        if _as_aware(intent.valid_until) <= self._now_aware():
            self._ack_degraded(
                intent.intent_id,
                IntentAckStatus.EXPIRED,
                "expired",
            )
            self._advance_cursor(item.cursor)
            return

        receipt_getter = getattr(self._intent_inbox, "get", None)
        receipt = False
        if callable(receipt_getter):
            receipt = receipt_getter(intent.intent_id)
        if (
            receipt is not None
            and receipt is not False
            and str(receipt.cursor) == str(item.cursor)
        ):
            self._prepare_received_intent(
                item,
                intent,
                receipt_status=str(receipt.status),
            )
            return

        duplicate_detail = self._state.has_processed(intent)
        if duplicate_detail is not None:
            self._ack_degraded(
                intent.intent_id,
                IntentAckStatus.DUPLICATE,
                duplicate_detail,
            )
            self._advance_cursor(item.cursor)
            return

        if (
            TradingState(self._trading_state()) is TradingState.HALTED
            and intent.action in self._NEW_POSITION_ACTIONS
        ):
            self._ack_degraded(
                intent.intent_id,
                IntentAckStatus.REJECTED,
                "halted",
            )
            self._advance_cursor(item.cursor)
            return

        self._run_durable_write(
            "receipt receive",
            lambda: self._intent_inbox.receive(
                item.cursor,
                intent,
            ),
        )
        self._prepare_received_intent(
            item,
            intent,
            receipt_status="RECEIVED",
        )

    def _prepare_received_intent(
        self,
        item: IntentItem,
        intent: ApprovedTradeIntentV1,
        *,
        receipt_status: str,
    ) -> None:
        if self._state.has_processed(intent) is None:
            candidate = IntentOffsetState(
                last_cursor=item.cursor,
                processed_intents=set(self._state.processed_intents),
                processed_idempotency_keys=set(
                    self._state.processed_idempotency_keys
                ),
            )
            candidate.record_processed(intent)
            self._run_durable_write(
                "offset prepare",
                lambda: self._offset_store.save(candidate),
            )
            self._state = candidate
        if receipt_status == "RECEIVED":
            self._run_durable_write(
                "receipt prepare",
                lambda: self._intent_inbox.complete(
                    intent.intent_id,
                    "PREPARED",
                    "",
                ),
            )
        self._ack_degraded(
            intent.intent_id,
            IntentAckStatus.ACCEPTED,
            None,
        )
        self._publisher.publish(intent)

    def _ack_degraded(
        self,
        intent_id: UUID,
        status: IntentAckStatus,
        detail: Optional[str],
    ) -> bool:
        try:
            self._ack(intent_id, status, detail)
            self._remove_pending_ack(intent_id, status, detail)
            return True
        except Exception as exc:
            self._store_pending_ack(
                intent_id,
                status,
                detail,
            )
            self._record_degraded(
                "intent ACK failed and remains recoverable: "
                f"{exc!r}"
            )
            return False

    def _retry_pending_acks(self) -> None:
        with self._pending_ack_lock:
            pending = tuple(self._pending_acks.items())
        for key, acknowledgement in pending:
            intent_id, status, detail = acknowledgement
            try:
                self._ack(intent_id, status, detail)
            except Exception as exc:
                self._record_degraded(
                    "intent ACK retry failed and remains recoverable: "
                    f"{exc!r}"
                )
                continue
            with self._pending_ack_lock:
                self._pending_acks.pop(key, None)

    def _store_pending_ack(
        self,
        intent_id: UUID,
        status: IntentAckStatus,
        detail: Optional[str],
    ) -> None:
        key = self._pending_ack_key(
            intent_id,
            status,
            detail,
        )
        with self._pending_ack_lock:
            if key in self._pending_acks:
                return
            self._pending_acks[key] = (
                intent_id,
                status,
                detail,
            )

    def _remove_pending_ack(
        self,
        intent_id: UUID,
        status: IntentAckStatus,
        detail: Optional[str],
    ) -> None:
        key = self._pending_ack_key(
            intent_id,
            status,
            detail,
        )
        with self._pending_ack_lock:
            self._pending_acks.pop(key, None)

    def _pending_ack_key(
        self,
        intent_id: UUID,
        status: IntentAckStatus,
        detail: Optional[str],
    ) -> tuple[str, str, str]:
        return (
            str(intent_id),
            str(status.value),
            str(detail or ""),
        )

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

    def _raise_if_hard_failure(self) -> None:
        self._drain_hard_failure_mailbox()
        with self._hard_failure_lock:
            reason = self._hard_failure_reason
        if reason:
            raise RuntimeError(reason)

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
        candidate = IntentOffsetState(
            last_cursor=cursor,
            processed_intents=set(self._state.processed_intents),
            processed_idempotency_keys=set(
                self._state.processed_idempotency_keys
            ),
        )
        self._run_durable_write(
            "offset advance",
            lambda: self._offset_store.save(candidate),
        )
        self._state = candidate

    def _run_durable_write(
        self,
        operation: str,
        action: Callable[[], Any],
    ) -> Any:
        try:
            return action()
        except Exception as exc:
            self._request_hard_failure(
                f"{operation} failed: {exc!r}"
            )
            self._drain_hard_failure_mailbox()
            raise

    def _now_aware(self) -> datetime:
        return _as_aware(self._now())


def _intent_payload(raw: object) -> object:
    if isinstance(raw, ApprovedTradeIntentV1):
        return raw.model_dump(mode="json")
    return raw


def _try_extract_intent_id(item: IntentItem) -> UUID | bool:
    try:
        raw_intent = item.intent
        if isinstance(raw_intent, ApprovedTradeIntentV1):
            return raw_intent.intent_id
        if isinstance(raw_intent, dict) and "intent_id" in raw_intent:
            return UUID(str(raw_intent["intent_id"]))
        intent_id = getattr(raw_intent, "intent_id", None)
        if intent_id is not None:
            return UUID(str(intent_id))
    except (TypeError, ValueError):
        return False
    return False


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
