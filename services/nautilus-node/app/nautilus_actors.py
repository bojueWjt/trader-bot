from __future__ import annotations

import json
import os
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, RLock, Thread
from typing import Any, Callable, Iterable

try:  # pragma: no cover - Nautilus is unavailable on local dev hosts.
    from nautilus_trader.common.actor import Actor  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover

    class Actor:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs


CustomDataBuilder = Callable[[Any], Any]


DEFAULT_EXECUTION_EVENT_TOPICS: tuple[str, ...] = (
    # TODO(host-verify): confirm Nautilus 1.227.0 execution event topic names and
    # wildcard syntax against the hk wheel. These are intentionally centralized so
    # host validation has one place to adjust if MessageBus uses different topics.
    "events.order.*",
    "events.position.*",
    "events.account.*",
    "events.execution.*",
)


ORDER_SNAPSHOT_LIMIT = 100
CONTROL_PLANE_STALE_AFTER_SECONDS = 15.0
DEFAULT_PENDING_INTENT_LIMIT = 100
DEFAULT_PENDING_COMMAND_ACK_LIMIT = 256
DEFAULT_COMMAND_ACK_BATCH_SIZE = 16
MAX_COMMAND_ACK_BATCH_SIZE = 64
DEFAULT_COMMAND_ACK_ATTEMPTS = 5
DEFAULT_COMMAND_POLL_RESULT_CAPACITY = 2
DEFAULT_WORKER_SHUTDOWN_WAIT_SECONDS = 0.5
DEFAULT_ACTOR_TICK_RESTART_AFTER_SECONDS = 60.0
DEFAULT_CALLBACK_MAX_ITEMS = 16
DEFAULT_CALLBACK_TIME_BUDGET_SECONDS = 0.005
DEFAULT_PROJECTION_DURABLE_INGRESS_DEADLINE_SECONDS = 0.5
DEFAULT_PENDING_COMMAND_LIMIT = 128
MAX_PENDING_COMMAND_LIMIT = 1024
DEFAULT_EXECUTION_EVENT_QUEUE_CAPACITY = 1024
DEFAULT_QUEUE_DEGRADED_RATIO = 0.8
DEFAULT_NAMESPACE_LEASE_REFRESH_INTERVAL_SECONDS = 60.0
DEFAULT_COMMAND_JOURNAL_MAX_ENTRIES = 4096
DEFAULT_COMMAND_JOURNAL_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_RESUME_COMMAND_MAX_AGE_SECONDS = 30.0
DEFAULT_COMMAND_PERSISTENCE_QUEUE_CAPACITY = 256
DEFAULT_TERMINAL_RESULT_QUEUE_CAPACITY = 128
DEFAULT_TERMINAL_VERIFY_ATTEMPTS = 4
DEFAULT_TERMINAL_VERIFY_DELAY_SECONDS = 0.25
TERMINAL_COMMAND_TYPES = frozenset({"cancel_all", "close_all"})


@dataclass
class _PendingCommandAck:
    command_id: Any
    status: Any
    error: str | None
    result: dict[str, Any] | None = None
    attempts: int = 0
    durability: Any = None


@dataclass(frozen=True)
class _CommandGenerationContext:
    runtime_generation: str
    reconciliation_generation: int
    lease_generation: Any


@dataclass(frozen=True)
class _CommandJournalEntry:
    command_id: str
    command_type: str
    phase: str
    runtime_generation: str
    reconciliation_generation: int
    lease_generation: Any
    issued_at: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    status: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    acked_status: str | None = None
    attempts: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "_CommandJournalEntry":
        return cls(
            command_id=str(payload["command_id"]),
            command_type=str(payload["command_type"]),
            phase=str(payload["phase"]),
            runtime_generation=str(payload.get("runtime_generation") or ""),
            reconciliation_generation=int(
                payload.get("reconciliation_generation") or 0
            ),
            lease_generation=payload.get("lease_generation", 0),
            issued_at=_optional_text(payload.get("issued_at")),
            args=_json_object(payload.get("args")),
            status=_optional_text(payload.get("status")),
            error=_optional_text(payload.get("error")),
            result=_optional_json_object(payload.get("result")),
            acked_status=_optional_text(payload.get("acked_status")),
            attempts=max(int(payload.get("attempts") or 0), 0),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": self.command_type,
            "phase": self.phase,
            "runtime_generation": self.runtime_generation,
            "reconciliation_generation": self.reconciliation_generation,
            "lease_generation": self.lease_generation,
            "issued_at": self.issued_at,
            "args": self.args,
            "status": self.status,
            "error": self.error,
            "result": self.result,
            "acked_status": self.acked_status,
            "attempts": self.attempts,
        }


@dataclass
class _CommandPersistenceTask:
    operation: str
    commands: tuple[Any, ...] = ()
    context: _CommandGenerationContext | None = None
    command_id: str = ""
    status: Any = None
    error_text: str | None = None
    phase: str = "applied"
    result_payload: dict[str, Any] | None = None
    attempts: dict[str, int] = field(default_factory=dict)
    acknowledgements: tuple[tuple[str, str], ...] = ()
    notify_actor: bool = True
    completed: Event = field(default_factory=Event)
    journal_entry: _CommandJournalEntry | None = None
    error: Exception | None = None


class _JsonCommandJournal:
    def __init__(
        self,
        path: str | Path | None,
        *,
        max_entries: int = DEFAULT_COMMAND_JOURNAL_MAX_ENTRIES,
        max_bytes: int = DEFAULT_COMMAND_JOURNAL_MAX_BYTES,
    ) -> None:
        if max_entries < 1:
            raise ValueError("command journal max_entries must be positive")
        if max_bytes < 1:
            raise ValueError("command journal max_bytes must be positive")
        self._path = Path(path) if path is not None else None
        self._max_entries = int(max_entries)
        self._max_bytes = int(max_bytes)
        self._lock = RLock()
        self._entries: dict[str, _CommandJournalEntry] = {}
        self._staged_entries: dict[str, _CommandJournalEntry] = {}
        self._load()

    def entries(self) -> tuple[_CommandJournalEntry, ...]:
        entries = dict(self._entries)
        entries.update(self._staged_entries)
        return tuple(entries.values())

    def get(self, command_id: str) -> _CommandJournalEntry | None:
        normalized_id = str(command_id)
        staged = self._staged_entries.get(normalized_id)
        if staged is not None:
            return staged
        return self._entries.get(normalized_id)

    def record_received(
        self,
        command: Any,
        context: _CommandGenerationContext,
    ) -> _CommandJournalEntry:
        entries = self.record_received_many((command,), context)
        return entries[0]

    def record_received_many(
        self,
        commands: Iterable[Any],
        context: _CommandGenerationContext,
    ) -> tuple[_CommandJournalEntry, ...]:
        command_values = tuple(commands)
        if not command_values:
            return ()
        recorded = []
        changed = False
        with self._lock:
            entries = self._effective_entries()
            for command in command_values:
                command_id = str(command.command_id)
                existing = entries.get(command_id)
                if existing is not None:
                    recorded.append(existing)
                    continue
                raw_command_type = getattr(command, "type", "unknown")
                command_type = getattr(
                    raw_command_type,
                    "value",
                    raw_command_type,
                )
                entry = _CommandJournalEntry(
                    command_id=command_id,
                    command_type=str(command_type),
                    phase="received",
                    runtime_generation=context.runtime_generation,
                    reconciliation_generation=(
                        context.reconciliation_generation
                    ),
                    lease_generation=context.lease_generation,
                    issued_at=_command_issued_at_text(command),
                    args=_json_object(getattr(command, "args", {})),
                )
                entries[command_id] = entry
                recorded.append(entry)
                changed = True
            if changed:
                self._commit(entries)
        return tuple(recorded)

    def record_applied(
        self,
        command_id: str,
        status: Any,
        error: str | None,
        *,
        phase: str = "applied",
        result: dict[str, Any] | None = None,
    ) -> _CommandJournalEntry:
        with self._lock:
            existing = self._require(command_id)
            status_value = getattr(status, "value", status)
            entry = replace(
                existing,
                phase=phase,
                status=str(status_value),
                error=error,
                result=result,
                attempts=0,
            )
            entries = self._effective_entries()
            entries[entry.command_id] = entry
            self._commit(entries)
            self._clear_staged_entry(entry)
            return entry

    def stage_applied(
        self,
        command_id: str,
        status: Any,
        error: str | None,
        *,
        phase: str = "applied",
        result: dict[str, Any] | None = None,
    ) -> _CommandJournalEntry:
        existing = self._require(command_id)
        status_value = getattr(status, "value", status)
        entry = replace(
            existing,
            phase=phase,
            status=str(status_value),
            error=error,
            result=result,
            attempts=0,
        )
        staged = dict(self._staged_entries)
        staged[entry.command_id] = entry
        self._staged_entries = staged
        return entry

    def record_attempts(
        self,
        attempts: dict[str, int],
    ) -> None:
        if not attempts:
            return
        with self._lock:
            entries = self._effective_entries()
            for command_id, count in attempts.items():
                existing = entries.get(command_id)
                if existing is None:
                    continue
                entries[command_id] = replace(existing, attempts=max(count, 0))
            self._commit(entries)

    def record_acked(
        self,
        acknowledgements: Iterable[tuple[str, str]],
    ) -> None:
        completed = tuple(
            (str(command_id), str(status))
            for command_id, status in acknowledgements
        )
        if not completed:
            return
        with self._lock:
            entries = self._effective_entries()
            for command_id, status in completed:
                existing = entries.get(command_id)
                if existing is None:
                    continue
                phase = existing.phase
                if status in {"completed", "failed"}:
                    phase = "acked"
                entries[command_id] = replace(
                    existing,
                    phase=phase,
                    acked_status=status,
                    attempts=0,
                )
            self._commit(entries)

    def _require(self, command_id: str) -> _CommandJournalEntry:
        normalized_id = str(command_id)
        entry = self._staged_entries.get(normalized_id)
        if entry is None:
            entry = self._entries.get(normalized_id)
        if entry is None:
            raise KeyError(f"command receipt is missing for {command_id}")
        return entry

    def _effective_entries(self) -> dict[str, _CommandJournalEntry]:
        entries = dict(self._entries)
        entries.update(self._staged_entries)
        return entries

    def _clear_staged_entry(self, entry: _CommandJournalEntry) -> None:
        staged = self._staged_entries.get(entry.command_id)
        if staged != entry:
            return
        entries = dict(self._staged_entries)
        entries.pop(entry.command_id, None)
        self._staged_entries = entries

    def _load(self) -> None:
        path = self._path
        if path is None or not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("command journal root must be a JSON object")
        entries = payload.get("entries", [])
        if not isinstance(entries, list):
            raise TypeError("command journal entries must be a list")
        loaded = [
            _CommandJournalEntry.from_payload(item)
            for item in entries
            if isinstance(item, dict)
        ]
        self._entries = {entry.command_id: entry for entry in loaded}

    def _commit(self, entries: dict[str, _CommandJournalEntry]) -> None:
        compacted = self._compact(entries)
        self._save(compacted)
        self._entries = compacted

    def _compact(
        self,
        entries: dict[str, _CommandJournalEntry],
    ) -> dict[str, _CommandJournalEntry]:
        if len(entries) <= self._max_entries:
            return entries
        removable = [
            command_id
            for command_id, entry in entries.items()
            if entry.phase == "acked"
        ]
        compacted = dict(entries)
        for command_id in removable:
            if len(compacted) <= self._max_entries:
                return compacted
            compacted.pop(command_id, None)
        if len(compacted) > self._max_entries:
            raise RuntimeError("command journal capacity exceeded")
        return compacted

    def _save(self, entries: dict[str, _CommandJournalEntry]) -> None:
        path = self._path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1.0",
            "entries": [entry.to_payload() for entry in entries.values()],
        }
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if len(encoded) > self._max_bytes:
            raise RuntimeError(
                "command journal byte capacity exceeded: "
                f"{len(encoded)} > {self._max_bytes}"
            )
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(encoded)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, path)
            _fsync_directory(path.parent)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


class _CommandJournalWriteError(RuntimeError):
    pass


@dataclass(frozen=True)
class _CommandAckResult:
    acked: tuple[tuple[str, str], ...]
    failed_command_ids: tuple[str, ...]


@dataclass(frozen=True)
class _CommandPollResult:
    commands: tuple[Any, ...]
    overflowed: bool = False


@dataclass(frozen=True)
class _TerminalCommandResult:
    command_id: str
    command_type: str
    account_id: str
    instrument_ids: tuple[str, ...]
    operations: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]
    dispatched_at: datetime
    completed_at: datetime


@dataclass(frozen=True)
class _TerminalVerificationResult:
    command_id: str
    status: str
    error: str | None
    result: dict[str, Any]


@dataclass
class _IntentPublication:
    intent: Any
    completed: Event = field(default_factory=Event)
    cancelled: Event = field(default_factory=Event)
    error: Exception | None = None


@dataclass
class _SessionCommandPublication:
    command: Any
    completed: Event = field(default_factory=Event)
    acknowledgement: _PendingCommandAck | None = None
    error: Exception | None = None


@dataclass
class _ProjectionPublication:
    event: Any = None
    flush_only: bool = False
    submitted_at: float = 0.0
    deadline_at: float | None = None
    worker_started: Event = field(default_factory=Event)
    completed: Event = field(default_factory=Event)
    durable: bool = False
    error: Exception | None = None


class _QueueingIntentPublisher:
    def __init__(
        self,
        pending: Queue[_IntentPublication],
        *,
        enqueue_timeout_seconds: float,
        completion_timeout_seconds: float,
    ) -> None:
        self._pending = pending
        self._enqueue_timeout_seconds = enqueue_timeout_seconds
        self._completion_timeout_seconds = completion_timeout_seconds
        self._stopped = Event()

    def stop(self) -> None:
        self._stopped.set()
        while True:
            try:
                publication = self._pending.get_nowait()
            except Empty:
                return
            publication.error = RuntimeError(
                "intent publisher actor stopped before publication"
            )
            publication.completed.set()

    def publish(self, intent: Any) -> None:
        if self._stopped.is_set():
            raise RuntimeError("intent publisher actor is stopped")
        publication = _IntentPublication(intent=intent)
        try:
            self._pending.put(
                publication,
                timeout=self._enqueue_timeout_seconds,
            )
        except Full as exc:
            raise RuntimeError("intent publication backlog full") from exc

        deadline = time.monotonic() + self._completion_timeout_seconds
        while not publication.completed.wait(timeout=0.05):
            if self._stopped.is_set():
                publication.cancelled.set()
                raise RuntimeError("intent publisher actor stopped before publication")
            if time.monotonic() >= deadline:
                publication.cancelled.set()
                raise RuntimeError(
                    "intent publication timed out waiting for actor thread"
                )
        if publication.error is not None:
            raise publication.error


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "value"):
        value = value.value
    return str(value)


def _safe_attr(source: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        try:
            value = getattr(source, name)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _safe_call(source: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        fn = _safe_attr(source, (name,))
        if not callable(fn):
            continue
        try:
            return fn()
        except Exception:
            continue
    return None


def _order_field(order: Any, names: tuple[str, ...]) -> Any:
    value = _safe_attr(order, names)
    if value is not None:
        return value
    return _safe_call(order, names)


def order_snapshot_payload(order: Any) -> dict:
    fields = {
        "client_order_id": _order_field(order, ("client_order_id", "client_id")),
        "instrument_id": _order_field(order, ("instrument_id", "instrument")),
        "order_type": _order_field(order, ("order_type", "type")),
        "side": _order_field(order, ("side", "order_side")),
        "quantity": _order_field(order, ("quantity", "qty")),
        "price": _order_field(order, ("price", "limit_price")),
        "trigger_price": _order_field(order, ("trigger_price", "stop_price")),
        "reduce_only": _order_field(order, ("reduce_only", "is_reduce_only")),
        "position_id": _order_field(order, ("position_id",)),
    }
    return {
        key: _string_value(value) for key, value in fields.items() if value is not None
    }


def open_orders_snapshot(cache: Any, limit: int = ORDER_SNAPSHOT_LIMIT) -> list[dict]:
    orders = _safe_call(cache, ("orders_open", "open_orders", "orders_active"))
    if orders is None:
        orders = _safe_attr(cache, ("orders_open", "open_orders", "orders_active"))
    if orders is None:
        return []
    out = []
    try:
        iterator = iter(orders)
    except TypeError:
        return []
    for order in iterator:
        try:
            payload = order_snapshot_payload(order)
        except Exception:
            continue
        if payload.get("client_order_id"):
            out.append(payload)
        if len(out) >= limit:
            break
    return out


def _cache_order(cache: Any, client_order_id: Any) -> Any:
    if cache is None or client_order_id is None:
        return None
    for method in ("order", "order_by_client_order_id"):
        fn = _safe_attr(cache, (method,))
        if not callable(fn):
            continue
        try:
            found = fn(client_order_id)
        except Exception:
            continue
        if found is not None:
            return found
    for order in (
        _safe_call(cache, ("orders_open", "open_orders", "orders_active")) or ()
    ):
        if _string_value(
            _order_field(order, ("client_order_id", "client_id"))
        ) == _string_value(client_order_id):
            return order
    return None


def order_event_payload_fields(event: Any, cache: Any = None) -> dict:
    client_order_id = _safe_attr(event, ("client_order_id", "client_id"))
    order = _safe_attr(event, ("order",)) or _cache_order(cache, client_order_id)
    if client_order_id is None and order is None:
        return {}
    merged = order_snapshot_payload(order) if order is not None else {}
    event_fields = order_snapshot_payload(event)
    merged.update(
        {key: value for key, value in event_fields.items() if value is not None}
    )
    if client_order_id is not None:
        merged.setdefault("client_order_id", _string_value(client_order_id))
    return merged


class IntentPublisherActor(Actor):
    """Nautilus ``Actor`` wrapper around the plain ``ApprovedIntentDataClient``."""

    def __init__(
        self,
        intent_data_client: Any,
        *,
        poll_interval_seconds: float = 1.0,
        poll_limit: int = 100,
        wait_ms: int = 0,
        timer_name: str = "approved-intents.poll",
        custom_data_builder: CustomDataBuilder | None = None,
        lifecycle: Any = None,
        stale_after_seconds: float = CONTROL_PLANE_STALE_AFTER_SECONDS,
        pending_intent_limit: int = DEFAULT_PENDING_INTENT_LIMIT,
        publication_enqueue_timeout_seconds: float = 0.1,
        publication_completion_timeout_seconds: float = (
            CONTROL_PLANE_STALE_AFTER_SECONDS
        ),
        worker_shutdown_wait_seconds: float = DEFAULT_WORKER_SHUTDOWN_WAIT_SECONDS,
        callback_max_items: int = DEFAULT_CALLBACK_MAX_ITEMS,
        callback_time_budget_seconds: float = (DEFAULT_CALLBACK_TIME_BUDGET_SECONDS),
        control_plane_session: Any = None,
        manage_control_plane_session: bool = True,
    ) -> None:
        _init_actor_base(self)
        if pending_intent_limit < 1:
            raise ValueError("pending_intent_limit must be positive")
        if callback_max_items < 1:
            raise ValueError("callback_max_items must be positive")
        if callback_time_budget_seconds <= 0:
            raise ValueError("callback_time_budget_seconds must be positive")
        self._intent_data_client = intent_data_client
        self._poll_interval_seconds = poll_interval_seconds
        self._poll_limit = poll_limit
        self._wait_ms = wait_ms
        self._timer_name = timer_name
        self._custom_data_builder = custom_data_builder
        self._lifecycle = lifecycle
        self._stale_after_seconds = stale_after_seconds
        self._worker_shutdown_wait_seconds = max(
            float(worker_shutdown_wait_seconds),
            0.0,
        )
        self._callback_max_items = int(callback_max_items)
        self._callback_time_budget_seconds = float(callback_time_budget_seconds)
        self._control_plane_session = control_plane_session
        self._manage_control_plane_session = bool(
            manage_control_plane_session
        )
        self._stopped = Event()
        self._executor: ThreadPoolExecutor | None = None
        self._poll_future: Future[int] | None = None
        self._pending_intents: Queue[_IntentPublication] = Queue(
            maxsize=pending_intent_limit
        )
        self._queued_publisher = _QueueingIntentPublisher(
            self._pending_intents,
            enqueue_timeout_seconds=max(
                float(publication_enqueue_timeout_seconds),
                0.0,
            ),
            completion_timeout_seconds=max(
                float(publication_completion_timeout_seconds),
                0.1,
            ),
        )
        self._started_at = time.monotonic()
        self._last_poll_success_at: float | None = None
        self._intent_stream_failed = False
        self._attach_to_plain_client_publisher(self._queued_publisher)

    def on_start(self) -> None:
        self._started_at = time.monotonic()
        session = self._control_plane_session
        if session is not None:
            if self._manage_control_plane_session:
                session.start()
            self._register_poll_timer()
            return
        self._ensure_executor()
        self._register_poll_timer()

    def on_stop(self) -> None:
        self._stopped.set()
        session = self._control_plane_session
        if session is not None:
            if self._manage_control_plane_session:
                deadline = (
                    time.monotonic()
                    + self._worker_shutdown_wait_seconds
                )
                session.stop(deadline)
            self._queued_publisher.stop()
            return
        self._queued_publisher.stop()
        executor = self._executor
        self._executor = None
        _shutdown_executor(
            executor,
            self._poll_future,
            deadline=time.monotonic() + self._worker_shutdown_wait_seconds,
        )
        self._poll_future = None

    def poll_once(self) -> int:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._poll_client_once)
            while not future.done():
                self._drain_pending_intents()
                time.sleep(0.001)
            self._drain_pending_intents()
            return int(future.result())

    def _poll_client_once(self) -> int:
        poll_once = getattr(self._intent_data_client, "poll_once", None)
        if not callable(poll_once):
            raise RuntimeError("intent_data_client does not expose poll_once")
        return int(poll_once(limit=self._poll_limit, wait_ms=self._wait_ms))

    def publish(self, intent: Any) -> None:
        # C-08 host-verify fix: deliver the approved intent over the msgbus on the
        # per-account topic the IntentExecutionStrategy subscribes to. publish_data +
        # subscribe_data does not route clientless custom data in Nautilus 1.227.0.
        message_bus = self._message_bus()
        if message_bus is not None and hasattr(message_bus, "publish"):
            account_id = getattr(intent, "account_id", None)
            message_bus.publish(topic=f"intents.{account_id}", msg=intent)
            return

        self._publish_via_engine_or_bus(intent)

    def _on_poll_timer(self, *_args: Any, **_kwargs: Any) -> None:
        if self._stopped.is_set():
            return
        if self._control_plane_session is not None:
            self._drain_pending_intents()
            self._evaluate_session_health()
            return
        self._drain_pending_intents()
        self._harvest_poll()
        self._submit_poll()
        self._evaluate_poll_staleness()

    def _ensure_executor(self) -> ThreadPoolExecutor:
        executor = self._executor
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"{self._timer_name}.worker",
            )
            self._executor = executor
            self._started_at = time.monotonic()
        return executor

    def _submit_poll(self) -> None:
        if self._stopped.is_set():
            return
        future = self._poll_future
        if future is not None:
            return
        self._poll_future = self._ensure_executor().submit(self._poll_client_once)

    def _harvest_poll(self) -> None:
        future = self._poll_future
        if future is None or not future.done():
            return
        self._poll_future = None
        try:
            future.result()
        except Exception as exc:
            print(f"[IntentPublisherActor] poll failed: {exc!r}", flush=True)
            return
        self._record_poll_progress()

    def _drain_pending_intents(self) -> int:
        published = 0
        inspected = 0
        deadline = time.monotonic() + self._callback_time_budget_seconds
        while inspected < self._callback_max_items:
            if inspected > 0 and time.monotonic() >= deadline:
                return published
            try:
                publication = self._pending_intents.get_nowait()
            except Empty:
                return published
            inspected += 1
            try:
                if publication.cancelled.is_set():
                    continue
                self.publish(publication.intent)
                published += 1
                self._record_poll_progress()
            except Exception as exc:
                publication.error = exc
            finally:
                publication.completed.set()

    def _evaluate_poll_staleness(self) -> None:
        now = time.monotonic()
        reference = self._last_poll_success_at
        if reference is None:
            reference = self._started_at
        if now - reference < self._stale_after_seconds:
            return
        self._mark_dependency_failed("intent_stream", "approved intent poll stale")

    def _evaluate_session_health(self) -> None:
        session = self._control_plane_session
        if session is None:
            return
        snapshot = session.snapshot()
        lanes = getattr(snapshot, "lanes", {})
        fetch_lane = lanes.get("intent_fetch")
        delivery_lane = lanes.get("intent_delivery")
        failures = [
            str(getattr(lane, "failure", "") or "").strip()
            for lane in (fetch_lane, delivery_lane)
            if lane is not None
        ]
        failures = [failure for failure in failures if failure]
        if failures:
            self._mark_dependency_failed(
                "intent_stream",
                failures[0],
            )
            return
        last_success = getattr(fetch_lane, "last_success_at", False)
        if last_success is not False:
            self._record_poll_progress()

    def _record_poll_progress(self) -> None:
        self._last_poll_success_at = time.monotonic()
        self._mark_dependency_ready("intent_stream")

    def _register_poll_timer(self) -> None:
        clock = getattr(self, "clock", None)
        if clock is None:
            return
        set_timer = getattr(clock, "set_timer", None)
        if not callable(set_timer):
            return

        interval = timedelta(seconds=self._poll_interval_seconds)
        # TODO(host-verify): confirm the exact Actor.clock.set_timer signature on
        # Nautilus 1.227.0. These variants keep local tests independent from the
        # C-extension signature while failing closed if hk exposes neither form.
        try:
            set_timer(
                name=self._timer_name,
                interval=interval,
                callback=self._on_poll_timer,
            )
            return
        except TypeError:
            pass
        set_timer(self._timer_name, interval, self._on_poll_timer)

    def _attach_to_plain_client_publisher(self, selected_publisher: Any) -> None:
        publisher = getattr(self._intent_data_client, "_publisher", None)
        attach = getattr(publisher, "attach", None)
        if callable(attach):
            attach(selected_publisher)

    def _message_bus(self) -> Any:
        return _first_attr(self, ("msgbus", "message_bus", "_msgbus"))

    def _mark_dependency_ready(self, dependency_value: str) -> None:
        dependency = _dependency_by_value(dependency_value)
        marker = getattr(self._lifecycle, "mark_dependency_ready", None)
        if dependency is None or not callable(marker):
            self._intent_stream_failed = False
            return
        marker(dependency)
        self._intent_stream_failed = False

    def _mark_dependency_failed(self, dependency_value: str, reason: str) -> None:
        if self._intent_stream_failed:
            return
        dependency = _dependency_by_value(dependency_value)
        marker = getattr(self._lifecycle, "mark_dependency_failed", None)
        if dependency is None or not callable(marker):
            self._intent_stream_failed = True
            return
        marker(dependency, reason)
        self._intent_stream_failed = True

    def _build_custom_data(self, intent: Any) -> Any:
        if self._custom_data_builder is not None:
            return self._custom_data_builder(intent)
        from intent.custom_data import build_nautilus_custom_data  # type: ignore

        return build_nautilus_custom_data(intent)

    def _publish_via_engine_or_bus(self, intent: Any) -> None:
        from intent.custom_data import NautilusCustomDataPublisher  # type: ignore

        data_engine = _first_attr(self, ("data_engine", "_data_engine"))
        if data_engine is not None:
            NautilusCustomDataPublisher(data_engine=data_engine).publish(intent)
            return

        message_bus = self._message_bus()
        if message_bus is not None:
            # TODO(host-verify): confirm whether direct MessageBus.publish(CustomData)
            # is accepted for custom data in Nautilus 1.227.0. Actor.publish_data is
            # preferred above when available.
            NautilusCustomDataPublisher(message_bus=message_bus).publish(intent)
            return

        raise RuntimeError("no Nautilus publish_data/data_engine/message_bus available")


class ExecutionProjectionActor(Actor):
    """Nautilus ``Actor`` wrapper around the plain execution ``ProjectionActor``."""

    def __init__(
        self,
        projection_actor: Any,
        *,
        event_topics: Iterable[str] = DEFAULT_EXECUTION_EVENT_TOPICS,
        event_queue_capacity: int = DEFAULT_EXECUTION_EVENT_QUEUE_CAPACITY,
        queue_degraded_ratio: float = DEFAULT_QUEUE_DEGRADED_RATIO,
        worker_shutdown_wait_seconds: float = DEFAULT_WORKER_SHUTDOWN_WAIT_SECONDS,
        callback_time_budget_seconds: float = (
            DEFAULT_CALLBACK_TIME_BUDGET_SECONDS
        ),
        durable_ingress_deadline_seconds: float = (
            DEFAULT_PROJECTION_DURABLE_INGRESS_DEADLINE_SECONDS
        ),
        fatal_callback: Callable[[str], None] | None = None,
        control_plane_session: Any = None,
        manage_control_plane_session: bool = True,
    ) -> None:
        _init_actor_base(self)
        if event_queue_capacity < 1:
            raise ValueError("event_queue_capacity must be positive")
        if queue_degraded_ratio <= 0 or queue_degraded_ratio >= 1:
            raise ValueError("queue_degraded_ratio must be between zero and one")
        if callback_time_budget_seconds <= 0:
            raise ValueError(
                "callback_time_budget_seconds must be positive"
            )
        if durable_ingress_deadline_seconds <= 0:
            raise ValueError(
                "durable_ingress_deadline_seconds must be positive"
            )
        if (
            callback_time_budget_seconds
            > durable_ingress_deadline_seconds
        ):
            raise ValueError(
                "callback_time_budget_seconds cannot exceed "
                "durable_ingress_deadline_seconds"
            )
        self._projection_actor = projection_actor
        self._event_topics = tuple(event_topics)
        self._event_queue_capacity = int(event_queue_capacity)
        self._queue_degraded_ratio = float(queue_degraded_ratio)
        self._worker_shutdown_wait_seconds = max(
            float(worker_shutdown_wait_seconds),
            0.0,
        )
        self._callback_time_budget_seconds = float(
            callback_time_budget_seconds
        )
        self._durable_ingress_deadline_seconds = float(
            durable_ingress_deadline_seconds
        )
        self._event_queue: Queue[_ProjectionPublication] = Queue(
            maxsize=self._event_queue_capacity
        )
        self._worker_stop = Event()
        self._worker_busy = Event()
        self._worker_started = Event()
        self._worker_thread: Thread | None = None
        self._worker_stop_deadline: float | None = None
        self._durable_publications: dict[
            int,
            _ProjectionPublication,
        ] = {}
        self._durable_publications_lock = RLock()
        self._deadline_stop = Event()
        self._deadline_wake = Event()
        self._deadline_started = Event()
        self._deadline_thread: Thread | None = None
        self._egress_stop = Event()
        self._egress_wake = Event()
        self._egress_started = Event()
        self._egress_thread: Thread | None = None
        self._egress_stop_deadline: float | None = None
        self._capacity_drain_active = False
        self._capacity_drain_deadline: float | None = None
        self._queue_degraded = False
        self._halted_reason = ""
        self._halt_lock = RLock()
        self._fatal_callback = fatal_callback
        self._fatal_reported = False
        self._control_plane_session = control_plane_session
        self._session_started = False
        self._manage_control_plane_session = bool(
            manage_control_plane_session
        )
        self._subscribed_execution_topics: list[
            tuple[Any, str]
        ] = []
        self._durable_ingress = callable(
            getattr(self._projection_actor, "ingest_event", None)
        )

    @property
    def halted_reason(self) -> str:
        return self._halted_reason

    def on_start(self) -> None:
        deadline = time.monotonic() + self._worker_shutdown_wait_seconds
        try:
            self._start_worker()
            if self._durable_ingress:
                self._start_deadline_worker()
            session = self._control_plane_session
            if session is not None:
                if self._manage_control_plane_session:
                    session.start()
                    self._session_started = True
            elif self._durable_ingress:
                self._start_egress_worker()
            self._await_startup_barrier(deadline)
            if self._durable_ingress:
                if session is not None:
                    if self._submit_durable_flush_wake(False) is False:
                        self._rollback_startup(deadline)
                        return
                else:
                    self._egress_wake.set()
            for topic in self._event_topics:
                self._subscribe_execution_topic(topic)
        except Exception as exc:
            reason = (
                "execution projection startup failed: "
                f"{exc!r}"
            )
            self._mark_sticky_halt(reason)
            self._rollback_startup(deadline)
            self._report_fatal(reason)
            raise

    def on_stop(self) -> None:
        deadline = time.monotonic() + self._worker_shutdown_wait_seconds
        if self._unsubscribe_execution_topics(deadline) is False:
            self._halt_egress(
                "execution projection subscription rollback "
                "deadline exceeded"
            )
        thread = self._worker_thread
        if thread is not None:
            self._worker_stop_deadline = deadline
            self._worker_stop.set()
            thread.join(timeout=max(deadline - time.monotonic(), 0.0))
            if thread.is_alive():
                self._halt_egress(
                    "execution projection persistence worker "
                    "shutdown deadline exceeded"
                )
            else:
                self._worker_thread = None
        if self._durable_ingress and self._has_durable_publications():
            self._halt_egress(
                "execution projection persistence worker stopped "
                "with durable ingress pending"
            )
        self._stop_deadline_worker(deadline)
        session = self._control_plane_session
        if session is not None:
            if self._manage_control_plane_session and self._session_started:
                stopped = bool(session.stop(deadline))
                self._session_started = False
                if not stopped:
                    self._halt_egress(
                        "execution projection session shutdown "
                        "deadline exceeded"
                    )
            return
        egress_thread = self._egress_thread
        if egress_thread is None:
            return
        self._egress_stop_deadline = deadline
        self._egress_stop.set()
        self._egress_wake.set()
        egress_thread.join(
            timeout=max(deadline - time.monotonic(), 0.0)
        )
        if egress_thread.is_alive():
            self._halt_egress(
                "execution projection egress worker "
                "shutdown deadline exceeded"
            )
            return
        self._egress_thread = None

    def on_event(self, event: Any) -> bool:
        if self._worker_stop.is_set():
            return False
        if self._halted_reason:
            return False
        worker = self._worker_thread
        if worker is None or not worker.is_alive():
            self._halt_egress(
                "execution projection persistence worker is not running"
            )
            return False
        submitted_at = time.monotonic()
        deadline_at = None
        if self._durable_ingress:
            deadline_at = (
                submitted_at
                + self._durable_ingress_deadline_seconds
            )
        publication = _ProjectionPublication(
            event=event,
            submitted_at=submitted_at,
            deadline_at=deadline_at,
        )
        if self._durable_ingress:
            self._register_durable_publication(publication)
        try:
            self._event_queue.put_nowait(publication)
        except Full:
            self._complete_durable_publication(publication)
            self._begin_capacity_drain(
                "execution projection persistence queue capacity exceeded"
            )
            return False
        self._update_queue_pressure()
        return True

    def _persist_durable_event(
        self,
        publication: _ProjectionPublication,
    ) -> bool:
        self._attach_order_payload_fields(publication.event)
        ingest = getattr(self._projection_actor, "ingest_event")
        try:
            result = ingest(publication.event)
        except Exception as exc:
            publication.error = exc
            self._halt_egress(
                f"execution projection durable ingress failed: {exc!r}"
            )
            return False
        outcome = self._projection_ingest_outcome(result)
        if outcome == "IGNORED":
            error = RuntimeError(
                "execution projection ignored subscribed execution event"
            )
            publication.error = error
            self._halt_egress(str(error))
            return False
        if outcome not in {"DURABLE", "DEDUPED"}:
            error = RuntimeError(
                "execution projection durable ingress returned "
                f"invalid outcome: {outcome or 'missing'}"
            )
            publication.error = error
            self._halt_egress(str(error))
            return False
        publication.durable = True
        deadline_at = publication.deadline_at
        if (
            deadline_at is not None
            and time.monotonic() >= deadline_at
        ):
            self._halt_egress(
                "execution projection durable ingress deadline exceeded"
            )
            return False
        return True

    def _projection_ingest_outcome(self, result: Any) -> str:
        outcome = getattr(result, "outcome", result)
        value = getattr(outcome, "value", outcome)
        return str(value or "").strip().upper()

    def _submit_durable_flush_wake(self, event: Any) -> bool:
        session = self._control_plane_session
        if session is not None:
            result = session.submit_execution_event(event)
            if _submission_was_accepted(result):
                return True
            self._halt_egress(
                "execution projection session wake backpressured"
            )
            return False
        try:
            self._event_queue.put_nowait(
                _ProjectionPublication(flush_only=True)
            )
        except Full:
            return True
        return True

    def _start_worker(self) -> None:
        thread = self._worker_thread
        if thread is not None and thread.is_alive():
            return
        self._worker_stop.clear()
        self._worker_stop_deadline = None
        self._worker_started.clear()
        thread = Thread(
            target=self._run_worker,
            name="execution-projection.egress",
            daemon=True,
        )
        self._worker_thread = thread
        thread.start()

    def _start_deadline_worker(self) -> None:
        thread = self._deadline_thread
        if thread is not None and thread.is_alive():
            return
        self._deadline_stop.clear()
        self._deadline_wake.clear()
        self._deadline_started.clear()
        thread = Thread(
            target=self._run_deadline_worker,
            name="execution-projection.ingress-deadline",
            daemon=True,
        )
        self._deadline_thread = thread
        thread.start()

    def _stop_deadline_worker(self, deadline: float) -> None:
        thread = self._deadline_thread
        if thread is None:
            return
        self._deadline_stop.set()
        self._deadline_wake.set()
        thread.join(timeout=max(deadline - time.monotonic(), 0.0))
        if thread.is_alive():
            self._halt_egress(
                "execution projection durable ingress deadline worker "
                "shutdown deadline exceeded"
            )
            return
        self._deadline_thread = None

    def _start_egress_worker(self) -> None:
        thread = self._egress_thread
        if thread is not None and thread.is_alive():
            return
        self._egress_stop.clear()
        self._egress_stop_deadline = None
        self._egress_started.clear()
        thread = Thread(
            target=self._run_egress_worker,
            name="execution-projection.egress",
            daemon=True,
        )
        self._egress_thread = thread
        thread.start()

    def _run_worker(self) -> None:
        self._worker_started.set()
        while True:
            if self._worker_should_stop():
                return
            timeout = self._worker_poll_timeout()
            try:
                publication = self._event_queue.get(timeout=timeout)
            except Empty:
                continue
            self._worker_busy.set()
            publication.worker_started.set()
            try:
                if publication.flush_only:
                    self._egress_wake.set()
                elif self._durable_ingress:
                    if not self._persist_durable_event(publication):
                        return
                    session = self._control_plane_session
                    if session is not None:
                        if not self._submit_durable_flush_wake(
                            publication.event
                        ):
                            return
                    else:
                        self._egress_wake.set()
                else:
                    self._project_event(publication.event)
            except Exception as exc:
                publication.error = exc
                self._halt_egress(
                    f"execution projection persistence worker failed: {exc!r}"
                )
                return
            finally:
                publication.completed.set()
                self._complete_durable_publication(publication)
                self._worker_busy.clear()
                self._event_queue.task_done()
                self._update_queue_pressure()

    def _run_deadline_worker(self) -> None:
        self._deadline_started.set()
        while not self._deadline_stop.is_set():
            publication = self._next_durable_publication()
            capacity_active, capacity_deadline = (
                self._capacity_drain_snapshot()
            )
            if capacity_active and not self._has_durable_publications():
                self._finish_capacity_drain()
                self._report_fatal(self._halted_reason)
                continue

            now = time.monotonic()
            publication_deadline = None
            if publication is not None:
                publication_deadline = publication.deadline_at
                if publication_deadline is None:
                    self._complete_durable_publication(publication)
                    continue
                if publication.completed.is_set():
                    self._complete_durable_publication(publication)
                    continue
                if now >= publication_deadline:
                    self._halt_egress(
                        "execution projection durable ingress "
                        "deadline exceeded"
                    )
                    continue

            if (
                capacity_active
                and capacity_deadline is not None
                and now >= capacity_deadline
            ):
                self._finish_capacity_drain()
                self._report_fatal(self._halted_reason)
                continue

            wait_timeout = 0.05
            deadlines = [
                deadline
                for deadline in (
                    publication_deadline,
                    capacity_deadline if capacity_active else None,
                )
                if deadline is not None
            ]
            if deadlines:
                wait_timeout = min(
                    wait_timeout,
                    max(min(deadlines) - now, 0.0),
                )
            self._deadline_wake.wait(timeout=wait_timeout)
            self._deadline_wake.clear()

    def _register_durable_publication(
        self,
        publication: _ProjectionPublication,
    ) -> None:
        with self._durable_publications_lock:
            self._durable_publications[id(publication)] = publication
        self._deadline_wake.set()

    def _complete_durable_publication(
        self,
        publication: _ProjectionPublication,
    ) -> None:
        with self._durable_publications_lock:
            self._durable_publications.pop(id(publication), None)
        self._deadline_wake.set()

    def _next_durable_publication(
        self,
    ) -> _ProjectionPublication | None:
        with self._durable_publications_lock:
            publications = tuple(self._durable_publications.values())
        pending = [
            publication
            for publication in publications
            if publication.completed.is_set() is False
            and publication.deadline_at is not None
        ]
        if not pending:
            return None
        return min(
            pending,
            key=lambda publication: float(
                publication.deadline_at
                if publication.deadline_at is not None
                else float("inf")
            ),
        )

    def _has_durable_publications(self) -> bool:
        with self._durable_publications_lock:
            return bool(self._durable_publications)

    def _run_egress_worker(self) -> None:
        self._egress_started.set()
        while True:
            if self._egress_should_stop():
                return
            timeout = self._egress_poll_timeout()
            if not self._egress_wake.wait(timeout=timeout):
                continue
            self._egress_wake.clear()
            try:
                self._drain_durable_spool()
            except Exception as exc:
                self._halt_egress(
                    f"execution projection egress worker failed: {exc!r}"
                )
                return

    def _drain_durable_spool(self) -> None:
        while True:
            before, after = self._flush_projection()
            if before is None or after is None:
                return
            if after <= 0:
                return
            if after >= before:
                return
            if self._egress_deadline_reached():
                return

    def _egress_should_stop(self) -> bool:
        if not self._egress_stop.is_set():
            return False
        pending_count = self._durable_pending_count()
        if pending_count is None or pending_count <= 0:
            return True
        return self._egress_deadline_reached()

    def _egress_deadline_reached(self) -> bool:
        deadline = self._egress_stop_deadline
        if deadline is None:
            return False
        return time.monotonic() >= deadline

    def _egress_poll_timeout(self) -> float:
        timeout = 0.05
        if not self._egress_stop.is_set():
            return timeout
        deadline = self._egress_stop_deadline
        if deadline is None:
            return 0.0
        return min(timeout, max(deadline - time.monotonic(), 0.0))

    def _worker_should_stop(self) -> bool:
        if not self._worker_stop.is_set():
            return False
        if self._event_queue.empty():
            return True
        deadline = self._worker_stop_deadline
        if deadline is None:
            return True
        return time.monotonic() >= deadline

    def _worker_poll_timeout(self) -> float:
        timeout = 0.05
        if not self._worker_stop.is_set():
            return timeout
        deadline = self._worker_stop_deadline
        if deadline is None:
            return 0.0
        return min(timeout, max(deadline - time.monotonic(), 0.0))

    def _flush_projection(self) -> tuple[int | None, int | None]:
        before = self._durable_pending_count()
        flush = getattr(self._projection_actor, "flush", None)
        if callable(flush):
            flush()
        return before, self._durable_pending_count()

    def _durable_pending_count(self) -> int | None:
        spool = getattr(self._projection_actor, "spool", None)
        pending_count = getattr(spool, "pending_count", None)
        if pending_count is None:
            return None
        return int(pending_count)

    def _schedule_followup_flush(
        self,
        *,
        before: int | None,
        after: int | None,
    ) -> None:
        if not self._durable_ingress:
            return
        if before is None or after is None:
            return
        if after <= 0 or after >= before:
            return
        try:
            self._event_queue.put_nowait(
                _ProjectionPublication(flush_only=True)
            )
        except Full:
            return

    def _project_event(self, event: Any) -> Any:
        self._attach_order_payload_fields(event)
        return self._projection_actor.on_event(event)

    def _attach_order_payload_fields(self, event: Any) -> None:
        extra = order_event_payload_fields(event, self._cache())
        if extra:
            attach = getattr(
                self._projection_actor, "attach_order_payload_fields", None
            )
            if callable(attach):
                try:
                    attach(event, extra)
                except Exception:
                    pass
            else:
                try:
                    setattr(event, "_projection_payload_extra", extra)
                except Exception:
                    pass

    def _update_queue_pressure(self) -> None:
        queue_size = self._event_queue.qsize()
        usage_ratio = queue_size / self._event_queue_capacity
        if queue_size >= self._event_queue_capacity:
            self._begin_capacity_drain(
                "execution projection persistence queue reached capacity"
            )
            return
        if usage_ratio >= self._queue_degraded_ratio:
            self._mark_egress_degraded(
                "execution projection persistence queue usage "
                f"{usage_ratio:.1%} exceeds degraded threshold"
            )
            return
        self._clear_egress_degraded()

    def _mark_egress_degraded(self, reason: str) -> None:
        if self._queue_degraded:
            return
        self._queue_degraded = True
        marker = getattr(self._projection_actor, "mark_egress_degraded", None)
        if callable(marker):
            marker(reason)

    def _clear_egress_degraded(self) -> None:
        if not self._queue_degraded:
            return
        if self._halted_reason:
            return
        self._queue_degraded = False
        clearer = getattr(self._projection_actor, "clear_egress_degraded", None)
        if callable(clearer):
            clearer()

    def _begin_capacity_drain(self, reason: str) -> None:
        self._mark_sticky_halt(reason)
        report_immediately = False
        with self._halt_lock:
            if self._capacity_drain_active is False:
                self._capacity_drain_active = True
                self._capacity_drain_deadline = (
                    time.monotonic()
                    + self._durable_ingress_deadline_seconds
                )
            if (
                self._durable_ingress is False
                or self._has_durable_publications() is False
            ):
                self._capacity_drain_active = False
                self._capacity_drain_deadline = None
                report_immediately = True
        self._deadline_wake.set()
        if report_immediately:
            self._report_fatal(self._halted_reason)

    def _capacity_drain_snapshot(self) -> tuple[bool, float | None]:
        with self._halt_lock:
            return (
                self._capacity_drain_active,
                self._capacity_drain_deadline,
            )

    def _finish_capacity_drain(self) -> None:
        with self._halt_lock:
            self._capacity_drain_active = False
            self._capacity_drain_deadline = None

    def _mark_sticky_halt(self, reason: str) -> None:
        should_halt = False
        with self._halt_lock:
            if not self._halted_reason:
                self._halted_reason = reason
                should_halt = True
        if should_halt is False:
            return
        halt = getattr(self._projection_actor, "halt_egress", None)
        if callable(halt):
            try:
                halt(reason)
            except Exception:
                return

    def _report_fatal(self, reason: str) -> None:
        fatal_callback = None
        fatal_reason = reason
        with self._halt_lock:
            if self._fatal_reported:
                return
            self._fatal_reported = True
            if self._halted_reason:
                fatal_reason = self._halted_reason
            fatal_callback = self._fatal_callback
        if fatal_callback is not None:
            fatal_callback(fatal_reason)

    def _halt_egress(self, reason: str) -> None:
        self._mark_sticky_halt(reason)
        self._report_fatal(reason)

    def _cache(self) -> Any:
        return _first_attr(self, ("cache", "_cache")) or _first_attr(
            self._projection_actor, ("cache", "_cache")
        )

    def on_order_event(self, event: Any) -> Any:
        return self.on_event(event)

    def on_position_event(self, event: Any) -> Any:
        return self.on_event(event)

    def on_account_state(self, event: Any) -> Any:
        return self.on_event(event)

    def _on_bus_event(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs:
            event = kwargs.get("event") or kwargs.get("message") or kwargs.get("msg")
            if event is not None:
                return self.on_event(event)
        if not args:
            return None
        return self.on_event(args[-1])

    def _subscription_targets(self) -> tuple[Any, ...]:
        # Resolved at runtime from the registered node. On a bare (unregistered)
        # Actor these are None and subscription is a no-op; Nautilus sets msgbus on
        # register. Extracted as a seam so tests can inject a recording bus without
        # assigning to the read-only ``Actor.msgbus`` property.
        return (
            _first_attr(self, ("msgbus", "message_bus", "_msgbus")),
            _first_attr(self, ("trader", "_trader")),
        )

    def _subscribe_execution_topic(self, topic: str) -> None:
        for subscriber in self._subscription_targets():
            if subscriber is None:
                continue
            subscribe = getattr(subscriber, "subscribe", None)
            if not callable(subscribe):
                continue
            subscribe(topic=topic, handler=self._on_bus_event)
            self._subscribed_execution_topics.append((subscriber, topic))
            return

    def _await_startup_barrier(self, deadline: float) -> None:
        lanes: list[tuple[str, Event, Thread | None]] = [
            (
                "persistence",
                self._worker_started,
                self._worker_thread,
            ),
        ]
        if self._durable_ingress:
            lanes.append(
                (
                    "deadline",
                    self._deadline_started,
                    self._deadline_thread,
                )
            )
            if self._control_plane_session is None:
                lanes.append(
                    (
                        "egress",
                        self._egress_started,
                        self._egress_thread,
                    )
                )
        for lane_name, started, thread in lanes:
            remaining = max(deadline - time.monotonic(), 0.0)
            if started.wait(timeout=remaining) is False:
                raise RuntimeError(
                    "execution projection "
                    f"{lane_name} startup barrier timed out"
                )
            if thread is None or thread.is_alive() is False:
                raise RuntimeError(
                    "execution projection "
                    f"{lane_name} lane exited during startup"
                )

    def _rollback_startup(self, deadline: float) -> None:
        self._unsubscribe_execution_topics(deadline)
        worker = self._worker_thread
        if worker is not None:
            self._worker_stop_deadline = deadline
            self._worker_stop.set()
            worker.join(timeout=max(deadline - time.monotonic(), 0.0))
            if worker.is_alive() is False:
                self._worker_thread = None
        self._stop_deadline_worker(deadline)

        session = self._control_plane_session
        if session is not None:
            if self._manage_control_plane_session and self._session_started:
                session.stop(deadline)
                self._session_started = False
            return

        egress = self._egress_thread
        if egress is None:
            return
        self._egress_stop_deadline = deadline
        self._egress_stop.set()
        self._egress_wake.set()
        egress.join(timeout=max(deadline - time.monotonic(), 0.0))
        if egress.is_alive() is False:
            self._egress_thread = None

    def _unsubscribe_execution_topics(
        self,
        deadline: float | None = None,
    ) -> bool:
        if deadline is None:
            deadline = (
                time.monotonic()
                + self._worker_shutdown_wait_seconds
            )
        subscriptions = tuple(
            reversed(self._subscribed_execution_topics)
        )
        self._subscribed_execution_topics.clear()
        for subscriber, topic in subscriptions:
            if time.monotonic() >= deadline:
                return False
            if self._unsubscribe_execution_topic(
                subscriber,
                topic,
                deadline,
            ) is False:
                return False
        return True

    def _unsubscribe_execution_topic(
        self,
        subscriber: Any,
        topic: str,
        deadline: float,
    ) -> bool:
        unsubscribe = getattr(subscriber, "unsubscribe", None)
        if not callable(unsubscribe):
            return True
        completed = Event()
        succeeded = Event()

        def invoke() -> None:
            try:
                unsubscribe(
                    topic=topic,
                    handler=self._on_bus_event,
                )
                succeeded.set()
            except Exception:
                return
            finally:
                completed.set()

        thread = Thread(
            target=invoke,
            name="execution-projection.unsubscribe",
            daemon=True,
        )
        thread.start()
        finished = completed.wait(
            timeout=max(deadline - time.monotonic(), 0.0)
        )
        return finished and succeeded.is_set()


class CommandPollerActor(Actor):
    """Polls operator commands from the control-plane and applies them to the node
    lifecycle (kill-switch path: HALT/RESUME/SET_REDUCING -> trading state). The intent
    consumer already gates new positions on HALTED, so HALT stops new entries at once."""

    def __init__(
        self,
        control_plane: Any,
        lifecycle: Any,
        node_id: str,
        *,
        account_id: str | None = None,
        poll_interval_seconds: float = 2.0,
        timer_name: str = "operator-commands.poll",
        stale_after_seconds: float = CONTROL_PLANE_STALE_AFTER_SECONDS,
        max_pending_acks: int = DEFAULT_PENDING_COMMAND_ACK_LIMIT,
        ack_batch_size: int = DEFAULT_COMMAND_ACK_BATCH_SIZE,
        max_ack_attempts: int = DEFAULT_COMMAND_ACK_ATTEMPTS,
        worker_shutdown_wait_seconds: float = DEFAULT_WORKER_SHUTDOWN_WAIT_SECONDS,
        actor_tick_stale_after_seconds: float = CONTROL_PLANE_STALE_AFTER_SECONDS,
        actor_tick_restart_after_seconds: float = (
            DEFAULT_ACTOR_TICK_RESTART_AFTER_SECONDS
        ),
        actor_tick_check_interval_seconds: float = 1.0,
        actor_tick_stale_callback: Callable[[float], None] | None = None,
        restart_required_callback: Callable[[float], None] | None = None,
        max_pending_commands: int = DEFAULT_PENDING_COMMAND_LIMIT,
        command_apply_max_items: int = DEFAULT_CALLBACK_MAX_ITEMS,
        command_apply_time_budget_seconds: float = (
            DEFAULT_CALLBACK_TIME_BUDGET_SECONDS
        ),
        namespace_lease: Any = None,
        namespace_lease_refresh_interval_seconds: float = (
            DEFAULT_NAMESPACE_LEASE_REFRESH_INTERVAL_SECONDS
        ),
        namespace_lease_freshness_seconds: float | None = None,
        namespace_lease_lost_callback: Callable[[str], None] | None = None,
        command_journal_path: str | Path | None = None,
        command_journal_max_entries: int = DEFAULT_COMMAND_JOURNAL_MAX_ENTRIES,
        command_journal_max_bytes: int = DEFAULT_COMMAND_JOURNAL_MAX_BYTES,
        resume_command_max_age_seconds: float = (
            DEFAULT_RESUME_COMMAND_MAX_AGE_SECONDS
        ),
        command_now: Callable[[], datetime] | None = None,
        command_persistence_capacity: int = (
            DEFAULT_COMMAND_PERSISTENCE_QUEUE_CAPACITY
        ),
        command_persistence_degraded_ratio: float = (
            DEFAULT_QUEUE_DEGRADED_RATIO
        ),
        fatal_callback: Callable[[str], None] | None = None,
        shutdown_callback: Callable[[], None] | None = None,
        exchange_evidence_provider: Any = None,
        terminal_result_queue_capacity: int = (
            DEFAULT_TERMINAL_RESULT_QUEUE_CAPACITY
        ),
        terminal_verify_attempts: int = DEFAULT_TERMINAL_VERIFY_ATTEMPTS,
        terminal_verify_delay_seconds: float = (
            DEFAULT_TERMINAL_VERIFY_DELAY_SECONDS
        ),
        control_plane_session: Any = None,
        manage_control_plane_session: bool = True,
        session_enqueue_timeout_seconds: float = 0.1,
        session_completion_timeout_seconds: float = (
            CONTROL_PLANE_STALE_AFTER_SECONDS
        ),
    ) -> None:
        _init_actor_base(self)
        if max_pending_acks < 1:
            raise ValueError("max_pending_acks must be positive")
        if ack_batch_size < 1:
            raise ValueError("ack_batch_size must be positive")
        if max_ack_attempts < 1:
            raise ValueError("max_ack_attempts must be positive")
        if max_pending_commands < 1:
            raise ValueError("max_pending_commands must be positive")
        if command_apply_max_items < 1:
            raise ValueError("command_apply_max_items must be positive")
        if command_apply_time_budget_seconds <= 0:
            raise ValueError("command_apply_time_budget_seconds must be positive")
        if command_persistence_capacity < 1:
            raise ValueError(
                "command_persistence_capacity must be positive"
            )
        if command_journal_max_bytes < 1:
            raise ValueError(
                "command_journal_max_bytes must be positive"
            )
        if resume_command_max_age_seconds <= 0:
            raise ValueError(
                "resume_command_max_age_seconds must be positive"
            )
        if (
            command_persistence_degraded_ratio <= 0
            or command_persistence_degraded_ratio >= 1
        ):
            raise ValueError(
                "command_persistence_degraded_ratio must be "
                "between zero and one"
            )
        if terminal_result_queue_capacity < 1:
            raise ValueError("terminal_result_queue_capacity must be positive")
        if terminal_verify_attempts < 2:
            raise ValueError("terminal_verify_attempts must be at least 2")
        if terminal_verify_delay_seconds < 0:
            raise ValueError(
                "terminal_verify_delay_seconds must be non-negative"
            )
        if session_enqueue_timeout_seconds < 0:
            raise ValueError(
                "session_enqueue_timeout_seconds must be non-negative"
            )
        if session_completion_timeout_seconds <= 0:
            raise ValueError(
                "session_completion_timeout_seconds must be positive"
            )
        if namespace_lease_refresh_interval_seconds <= 0:
            raise ValueError(
                "namespace_lease_refresh_interval_seconds must be positive"
            )
        if (
            namespace_lease_freshness_seconds is not None
            and namespace_lease_freshness_seconds
            <= namespace_lease_refresh_interval_seconds
        ):
            raise ValueError(
                "namespace_lease_freshness_seconds must exceed refresh interval"
            )
        self._control_plane = control_plane
        self._lifecycle = lifecycle
        self._node_id = str(node_id or "").strip()
        self._account_id = str(account_id or "").strip()
        if not self._node_id:
            raise ValueError("command poller node_id is required")
        if not self._account_id:
            raise ValueError("command poller account_id is required")
        self._poll_interval_seconds = poll_interval_seconds
        self._timer_name = timer_name
        self._stale_after_seconds = stale_after_seconds
        self._max_pending_acks = max_pending_acks
        self._ack_batch_size = min(
            ack_batch_size,
            max_pending_acks,
            MAX_COMMAND_ACK_BATCH_SIZE,
        )
        self._max_ack_attempts = max_ack_attempts
        self._worker_shutdown_wait_seconds = max(
            float(worker_shutdown_wait_seconds),
            0.0,
        )
        self._actor_tick_stale_after_seconds = actor_tick_stale_after_seconds
        self._actor_tick_restart_after_seconds = actor_tick_restart_after_seconds
        self._actor_tick_check_interval_seconds = actor_tick_check_interval_seconds
        self._actor_tick_stale_callback = actor_tick_stale_callback
        self._restart_required_callback = restart_required_callback
        self._max_pending_commands = min(
            int(max_pending_commands),
            MAX_PENDING_COMMAND_LIMIT,
        )
        self._command_apply_max_items = int(command_apply_max_items)
        self._command_apply_time_budget_seconds = float(
            command_apply_time_budget_seconds
        )
        self._namespace_lease = namespace_lease
        self._namespace_lease_process_owned = bool(
            getattr(namespace_lease, "process_owned", False)
        )
        self._namespace_lease_refresh_interval_seconds = float(
            namespace_lease_refresh_interval_seconds
        )
        lease_freshness_seconds = namespace_lease_freshness_seconds
        if lease_freshness_seconds is None:
            lease_freshness_seconds = (
                self._namespace_lease_refresh_interval_seconds * 2
            )
        self._namespace_lease_freshness_seconds = float(
            lease_freshness_seconds
        )
        self._namespace_lease_lost_callback = namespace_lease_lost_callback
        self._fatal_callback = fatal_callback
        self._shutdown_callback = shutdown_callback
        self._exchange_evidence_provider = exchange_evidence_provider
        self._terminal_verify_attempts = int(terminal_verify_attempts)
        self._terminal_verify_delay_seconds = float(
            terminal_verify_delay_seconds
        )
        self._control_plane_session = control_plane_session
        self._manage_control_plane_session = bool(
            manage_control_plane_session
        )
        self._session_enqueue_timeout_seconds = float(
            session_enqueue_timeout_seconds
        )
        self._session_completion_timeout_seconds = float(
            session_completion_timeout_seconds
        )
        self._command_persistence_capacity = int(
            command_persistence_capacity
        )
        self._resume_command_max_age_seconds = float(
            resume_command_max_age_seconds
        )
        self._command_now = command_now
        if self._command_now is None:
            self._command_now = lambda: datetime.now(timezone.utc)
        self._command_persistence_degraded_ratio = float(
            command_persistence_degraded_ratio
        )
        self._session_commands: Queue[_SessionCommandPublication] = Queue(
            maxsize=self._max_pending_commands
        )
        self._fatal_reason = ""
        self._last_namespace_lease_refresh_at: float | None = None
        self._last_namespace_lease_refreshed_at_epoch: int | None = None
        self._tick_watchdog: Any = None
        self._stopped = Event()
        self._lease_executor: ThreadPoolExecutor | None = None
        self._heartbeat_executor: ThreadPoolExecutor | None = None
        self._command_executor: ThreadPoolExecutor | None = None
        self._ack_executor: ThreadPoolExecutor | None = None
        self._terminal_executor: ThreadPoolExecutor | None = None
        self._lease_future: Future[Any] | None = None
        self._heartbeat_future: Future[None] | None = None
        self._command_future: Future[_CommandPollResult] | None = None
        self._command_poll_results: Queue[
            Future[_CommandPollResult]
        ] = Queue(maxsize=DEFAULT_COMMAND_POLL_RESULT_CAPACITY)
        self._command_poll_lock = RLock()
        self._command_poll_due = False
        self._ack_future: Future[_CommandAckResult] | None = None
        self._terminal_future: Future[_TerminalVerificationResult] | None = None
        self._command_persistence_queue: Queue[
            _CommandPersistenceTask
        ] = Queue(maxsize=self._command_persistence_capacity)
        self._command_persistence_results: Queue[
            _CommandPersistenceTask
        ] = Queue(maxsize=self._command_persistence_capacity)
        self._command_persistence_stop = Event()
        self._command_persistence_thread: Thread | None = None
        self._command_persistence_degraded = False
        self._command_receipt_tasks: dict[
            str,
            _CommandPersistenceTask,
        ] = {}
        self._pending_commands: tuple[Any, ...] = ()
        self._pending_command_index = 0
        self._pending_acks: dict[str, _PendingCommandAck] = {}
        self._terminal_results: Queue[Any] = Queue(
            maxsize=terminal_result_queue_capacity
        )
        self._pending_terminal_results: dict[
            str,
            _TerminalCommandResult,
        ] = {}
        self._pending_terminal_persistence: dict[
            str,
            _CommandPersistenceTask,
        ] = {}
        self._terminal_replays: dict[str, _CommandJournalEntry] = {}
        self._started_at = time.monotonic()
        self._last_heartbeat_success_at: float | None = None
        self._last_command_success_at: float | None = None
        self._exchange_evidence_available = exchange_evidence_provider is None
        self._exchange_evidence_failure_reason = ""
        self._failed_dependencies: set[str] = set()
        self._degraded_dependencies: set[str] = set()
        self._applied_commands: dict[str, tuple[Any, str | None]] = {}
        self._command_stream_failure_reason: str | None = None
        self._command_journal = _JsonCommandJournal(
            command_journal_path,
            max_entries=command_journal_max_entries,
            max_bytes=command_journal_max_bytes,
        )
        self._restore_command_journal()

    @property
    def pending_command_count(self) -> int:
        return max(
            len(self._pending_commands) - self._pending_command_index,
            0,
        )

    @property
    def command_persistence_queue_depth(self) -> int:
        return self._command_persistence_queue.qsize()

    @property
    def command_persistence_degraded(self) -> bool:
        return self._command_persistence_degraded

    def on_start(self) -> None:
        self._started_at = time.monotonic()
        self._acquire_namespace_lease()
        self._ensure_open_orders_provider()
        self._subscribe_terminal_command_results()
        watchdog = self._ensure_tick_watchdog()
        watchdog.start()
        watchdog.record_tick()
        self._ensure_command_persistence_worker()
        session = self._control_plane_session
        if session is not None:
            if self._manage_control_plane_session:
                session.start()
            self._submit_acks()
            self._register_poll_timer()
            return
        self._ensure_executors()
        self._submit_acks()
        self._register_poll_timer()

    def on_stop(self) -> None:
        self._stopped.set()
        shutdown_callback = self._shutdown_callback
        self._shutdown_callback = None
        if shutdown_callback is not None:
            shutdown_callback()
        watchdog = self._tick_watchdog
        if watchdog is not None:
            watchdog.stop()
        deadline = time.monotonic() + self._worker_shutdown_wait_seconds
        session = self._control_plane_session
        network_stopped = True
        if session is not None:
            if self._manage_control_plane_session:
                network_stopped = bool(session.stop(deadline))
            ack_stopped = _shutdown_executor(
                self._ack_executor,
                self._ack_future,
                deadline=deadline,
            )
            self._ack_executor = None
            self._ack_future = None
            network_stopped = network_stopped and ack_stopped
        else:
            lanes = (
                (self._lease_executor, self._lease_future),
                (self._heartbeat_executor, self._heartbeat_future),
                (self._command_executor, self._command_future),
                (self._ack_executor, self._ack_future),
                (self._terminal_executor, self._terminal_future),
            )
            self._lease_executor = None
            self._heartbeat_executor = None
            self._command_executor = None
            self._ack_executor = None
            self._terminal_executor = None
            lane_results = []
            for executor, future in lanes:
                lane_results.append(
                    _shutdown_executor(
                        executor,
                        future,
                        deadline=deadline,
                    )
                )
            self._lease_future = None
            self._heartbeat_future = None
            self._command_future = None
            self._ack_future = None
            self._terminal_future = None
            network_stopped = all(lane_results)
        persistence_stopped = self._stop_command_persistence_worker(
            deadline
        )
        if network_stopped and persistence_stopped:
            self._release_namespace_lease()
            return
        print(
            "[CommandPollerActor] namespace lease release skipped because "
            "a runtime worker remained in flight at shutdown deadline",
            flush=True,
        )
        self._fatal_runtime(
            "runtime worker shutdown deadline exceeded; "
            "namespace lease retained"
        )

    def _register_poll_timer(self) -> None:
        clock = getattr(self, "clock", None)
        set_timer = getattr(clock, "set_timer", None) if clock is not None else None
        if not callable(set_timer):
            return
        interval = timedelta(seconds=self._poll_interval_seconds)
        try:
            set_timer(
                name=self._timer_name, interval=interval, callback=self._on_poll_timer
            )
            return
        except TypeError:
            pass
        set_timer(self._timer_name, interval, self._on_poll_timer)

    def _on_poll_timer(self, *_args: Any, **_kwargs: Any) -> None:
        if self._stopped.is_set():
            return
        self._record_actor_tick()
        self._harvest_command_persistence()
        self._observe_process_namespace_lease()
        if self._control_plane_session is not None:
            self._harvest_namespace_lease_refresh()
            self._submit_namespace_lease_refresh()
            self._drain_session_commands()
            self._drain_terminal_command_results()
            self._harvest_terminal_verification()
            self._replay_terminal_commands()
            self._submit_terminal_verification()
            self._evaluate_session_health()
            return
        self._harvest_namespace_lease_refresh()
        self._harvest_heartbeat()
        self._harvest_commands()
        self._drain_terminal_command_results()
        self._harvest_terminal_verification()
        self._replay_terminal_commands()
        if self._namespace_lease_refresh_barrier_required():
            self._submit_pending_command_receipt()
            self._harvest_acks()
            self._submit_namespace_lease_refresh()
            self._submit_heartbeat()
            self._submit_acks()
            self._submit_terminal_verification()
            self._evaluate_poll_staleness()
            return
        self._schedule_pending_commands()
        self._harvest_acks()
        self._submit_namespace_lease_refresh()
        self._submit_heartbeat()
        self._submit_commands()
        self._submit_acks()
        self._submit_terminal_verification()
        self._evaluate_poll_staleness()

    def _ensure_executors(
        self,
    ) -> tuple[
        ThreadPoolExecutor | None,
        ThreadPoolExecutor,
        ThreadPoolExecutor,
        ThreadPoolExecutor,
        ThreadPoolExecutor,
    ]:
        lease_executor = self._lease_executor
        if (
            lease_executor is None
            and not self._namespace_lease_process_owned
        ):
            lease_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"{self._timer_name}.redis-lease",
            )
            self._lease_executor = lease_executor

        heartbeat_executor = self._heartbeat_executor
        if heartbeat_executor is None:
            heartbeat_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"{self._timer_name}.heartbeat",
            )
            self._heartbeat_executor = heartbeat_executor

        command_executor = self._command_executor
        if command_executor is None:
            command_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"{self._timer_name}.commands",
            )
            self._command_executor = command_executor

        ack_executor = self._ack_executor
        if ack_executor is None:
            ack_executor = self._ensure_ack_executor()

        terminal_executor = self._terminal_executor
        if terminal_executor is None:
            terminal_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"{self._timer_name}.terminal",
            )
            self._terminal_executor = terminal_executor

        return (
            lease_executor,
            heartbeat_executor,
            command_executor,
            ack_executor,
            terminal_executor,
        )

    def _ensure_ack_executor(self) -> ThreadPoolExecutor:
        executor = self._ack_executor
        if executor is not None:
            return executor
        executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"{self._timer_name}.acks",
        )
        self._ack_executor = executor
        return executor

    def _ensure_command_persistence_worker(self) -> Thread:
        thread = self._command_persistence_thread
        if thread is not None and thread.is_alive():
            return thread
        if self._command_persistence_stop.is_set():
            raise RuntimeError("command persistence lane is stopped")
        thread = Thread(
            target=self._run_command_persistence_worker,
            name=f"{self._timer_name}.persistence",
            daemon=True,
        )
        self._command_persistence_thread = thread
        thread.start()
        return thread

    def _run_command_persistence_worker(self) -> None:
        while True:
            if (
                self._command_persistence_stop.is_set()
                and self._command_persistence_queue.empty()
            ):
                return
            try:
                task = self._command_persistence_queue.get(timeout=0.05)
            except Empty:
                continue
            try:
                self._execute_command_persistence_task(task)
            except Exception as exc:
                task.error = exc
            finally:
                task.completed.set()
                if task.notify_actor:
                    try:
                        self._command_persistence_results.put_nowait(task)
                    except Full:
                        self._fatal_runtime(
                            "command persistence completion mailbox "
                            "capacity exceeded"
                        )
                self._command_persistence_queue.task_done()

    def _execute_command_persistence_task(
        self,
        task: _CommandPersistenceTask,
    ) -> None:
        operation = task.operation
        if operation == "received_batch":
            context = task.context
            if context is None:
                raise RuntimeError(
                    "command receipt persistence context is missing"
                )
            entries = self._command_journal.record_received_many(
                task.commands,
                context,
            )
            if entries:
                task.journal_entry = entries[-1]
            return
        if operation == "applied":
            task.journal_entry = self._command_journal.record_applied(
                task.command_id,
                task.status,
                task.error_text,
                phase=task.phase,
                result=task.result_payload,
            )
            return
        if operation == "attempts":
            self._command_journal.record_attempts(task.attempts)
            return
        if operation == "acked":
            self._command_journal.record_acked(task.acknowledgements)
            return
        raise ValueError(
            f"unknown command persistence operation: {operation}"
        )

    def _submit_command_persistence(
        self,
        task: _CommandPersistenceTask,
    ) -> bool:
        if self._fatal_reason:
            return False
        if self._command_persistence_stop.is_set():
            self._fatal_runtime("command persistence lane is stopped")
            return False
        if task.operation == "applied":
            task.journal_entry = self._command_journal.stage_applied(
                task.command_id,
                task.status,
                task.error_text,
                phase=task.phase,
                result=task.result_payload,
            )
        self._ensure_command_persistence_worker()
        try:
            self._command_persistence_queue.put_nowait(task)
        except Full:
            self._fatal_runtime(
                "command persistence queue capacity exceeded"
            )
            return False
        self._update_command_persistence_pressure()
        return True

    def _persist_command_task_blocking(
        self,
        task: _CommandPersistenceTask,
    ) -> _CommandPersistenceTask:
        task.notify_actor = False
        if not self._submit_command_persistence(task):
            raise RuntimeError(self._fatal_reason)
        return self._wait_for_command_persistence(task)

    def _wait_for_command_persistence(
        self,
        task: _CommandPersistenceTask,
    ) -> _CommandPersistenceTask:
        deadline = (
            time.monotonic()
            + self._session_completion_timeout_seconds
        )
        while not task.completed.wait(timeout=0.05):
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "command persistence operation timed out"
                )
        if task.error is not None:
            raise task.error
        return task

    def _harvest_command_persistence(self) -> int:
        harvested = 0
        deadline = (
            time.monotonic()
            + self._command_apply_time_budget_seconds
        )
        while harvested < self._command_apply_max_items:
            if harvested > 0 and time.monotonic() >= deadline:
                break
            try:
                task = self._command_persistence_results.get_nowait()
            except Empty:
                break
            try:
                if task.error is not None:
                    self._fatal_runtime(
                        "command journal persistence failed: "
                        f"{task.error!r}"
                    )
            finally:
                self._command_persistence_results.task_done()
            harvested += 1
        self._update_command_persistence_pressure()
        return harvested

    def _update_command_persistence_pressure(self) -> None:
        depth = self._command_persistence_queue.qsize()
        if depth >= self._command_persistence_capacity:
            self._fatal_runtime(
                "command persistence queue reached capacity"
            )
            return
        usage_ratio = depth / self._command_persistence_capacity
        if usage_ratio >= self._command_persistence_degraded_ratio:
            if self._command_persistence_degraded:
                return
            self._command_persistence_degraded = True
            self._mark_dependency_degraded(
                "command_stream",
                "command persistence queue usage "
                f"{usage_ratio:.1%} exceeds degraded threshold",
            )
            return
        if not self._command_persistence_degraded:
            return
        self._command_persistence_degraded = False
        command_usage = (
            self.pending_command_count / self._max_pending_commands
        )
        if command_usage >= DEFAULT_QUEUE_DEGRADED_RATIO:
            return
        self._clear_dependency_degraded("command_stream")

    def _stop_command_persistence_worker(self, deadline: float) -> bool:
        self._command_persistence_stop.set()
        thread = self._command_persistence_thread
        if thread is None:
            return True
        thread.join(timeout=max(deadline - time.monotonic(), 0.0))
        if thread.is_alive():
            self._fatal_runtime(
                "command persistence worker shutdown deadline exceeded"
            )
            return False
        self._command_persistence_thread = None
        if self._command_persistence_queue.unfinished_tasks > 0:
            self._fatal_runtime(
                "command persistence queue failed to drain before shutdown"
            )
            return False
        return True

    def _submit_namespace_lease_refresh(self) -> None:
        if self._stopped.is_set():
            return
        if self._namespace_lease is None:
            return
        if self._namespace_lease_process_owned:
            return
        if self._lease_future is not None:
            return
        if not self._namespace_lease_refresh_due():
            return
        lease_executor, _, _, _, _ = self._ensure_executors()
        if lease_executor is None:
            raise RuntimeError("namespace lease executor is unavailable")
        self._lease_future = lease_executor.submit(
            self._refresh_namespace_lease,
        )

    def _refresh_namespace_lease(self) -> Any:
        lease = self._namespace_lease
        if lease is None:
            return None
        return lease.refresh()

    def _submit_heartbeat(self) -> None:
        if self._stopped.is_set() or self._heartbeat_future is not None:
            return
        self._ensure_open_orders_provider()
        _, heartbeat_executor, _, _, _ = self._ensure_executors()
        self._heartbeat_future = heartbeat_executor.submit(
            self._run_heartbeat_lane,
        )

    def _run_heartbeat_lane(self) -> None:
        evidence_provider = self._exchange_evidence_provider
        if evidence_provider is None:
            heartbeat = self._lifecycle.build_heartbeat()
        else:
            try:
                snapshot = evidence_provider.snapshot()
            except Exception as exc:
                detail = str(exc).strip()
                if not detail:
                    detail = type(exc).__name__
                reason = f"exchange evidence unavailable: {detail}"
                self._exchange_evidence_available = False
                self._exchange_evidence_failure_reason = reason
                self._mark_dependency_failed("reconciliation", reason)
                print(
                    "[CommandPollerActor] exchange evidence unavailable; "
                    "sending heartbeat without exchange snapshot: "
                    f"{detail}",
                    flush=True,
                )
                heartbeat = self._lifecycle.build_heartbeat()
            else:
                self._exchange_evidence_available = True
                self._exchange_evidence_failure_reason = ""
                heartbeat = self._lifecycle.build_heartbeat(
                    exchange_evidence=snapshot,
                )
        receipt = self._control_plane.heartbeat(
            self._node_id,
            heartbeat,
        )
        if _heartbeat_receipt_requires_halt(
            receipt,
            live=_lifecycle_is_live(self._lifecycle),
        ):
            reason = _heartbeat_receipt_halt_reason(receipt)
            self._mark_dependency_failed("control_plane", reason)
            raise RuntimeError(reason)
        receipt_recorder = getattr(
            self._lifecycle,
            "record_heartbeat_receipt",
            None,
        )
        if callable(receipt_recorder):
            receipt_recorder(receipt)
        self._last_heartbeat_success_at = time.monotonic()
        self._mark_dependency_ready("control_plane")

    def session_send_heartbeat(self) -> None:
        self._run_heartbeat_lane()

    def session_poll_commands(
        self,
        capacity: int,
    ) -> tuple[Any, ...]:
        if capacity < 1:
            return ()
        result = self._poll_commands()
        commands = result.commands
        if result.overflowed or len(commands) > capacity:
            raise RuntimeError(
                "operator command delivery capacity exceeded"
            )
        return commands

    def session_apply_command(
        self,
        command: Any,
    ) -> _PendingCommandAck:
        if self._stopped.is_set():
            raise RuntimeError("command poller actor is stopped")
        publication = _SessionCommandPublication(command=command)
        try:
            self._session_commands.put(
                publication,
                timeout=self._session_enqueue_timeout_seconds,
            )
        except Full as exc:
            self._fail_command_stream(
                "operator command actor mailbox capacity exceeded"
            )
            raise RuntimeError(
                "operator command actor mailbox capacity exceeded"
            ) from exc
        deadline = (
            time.monotonic()
            + self._session_completion_timeout_seconds
        )
        while not publication.completed.wait(timeout=0.05):
            if self._stopped.is_set():
                raise RuntimeError(
                    "command poller actor stopped before apply"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "operator command apply timed out waiting for actor thread"
                )
        if publication.error is not None:
            raise publication.error
        acknowledgement = publication.acknowledgement
        if acknowledgement is None:
            raise RuntimeError(
                "operator command apply produced no acknowledgement"
            )
        return acknowledgement

    def session_ack_command(
        self,
        acknowledgement: _PendingCommandAck,
    ) -> None:
        command_id = str(acknowledgement.command_id)
        raw_status = getattr(
            acknowledgement.status,
            "value",
            acknowledgement.status,
        )
        entry = self._command_journal.get(command_id)
        if (
            entry is not None
            and entry.phase == "acked"
            and entry.acked_status == str(raw_status)
        ):
            self._pending_acks.pop(command_id, None)
            self._applied_commands.pop(command_id, None)
            return
        result = self._run_durable_ack_batch((acknowledgement,))
        if result.failed_command_ids:
            command_id = result.failed_command_ids[0]
            raise RuntimeError(
                f"command ACK failed for {command_id}"
            )
        for command_id, _status in result.acked:
            entry = self._command_journal.get(command_id)
            if entry is not None and entry.phase == "acked":
                self._applied_commands.pop(command_id, None)

    def _drain_session_commands(self) -> int:
        drained = 0
        deadline = (
            time.monotonic()
            + self._command_apply_time_budget_seconds
        )
        while drained < self._command_apply_max_items:
            if drained > 0 and time.monotonic() >= deadline:
                break
            try:
                publication = self._session_commands.get_nowait()
            except Empty:
                break
            try:
                publication.acknowledgement = (
                    self._apply_session_command(publication.command)
                )
            except Exception as exc:
                publication.error = exc
            finally:
                publication.completed.set()
                self._session_commands.task_done()
            drained += 1
        return drained

    def _apply_session_command(
        self,
        command: Any,
    ) -> _PendingCommandAck:
        if self.pending_command_count > 0:
            raise RuntimeError(
                "legacy command apply queue is active during session delivery"
            )
        self._pending_commands = (command,)
        self._pending_command_index = 0
        command_id = str(command.command_id)
        applied = self._schedule_pending_commands()
        acknowledgement = self._pending_acks.pop(command_id, None)
        if acknowledgement is not None:
            return acknowledgement
        if applied < 1:
            raise RuntimeError(
                f"operator command was not applied: {command_id}"
            )
        raise RuntimeError(
            f"operator command acknowledgement missing: {command_id}"
        )

    def _evaluate_session_health(self) -> None:
        session = self._control_plane_session
        if session is None:
            return
        snapshot = session.snapshot()
        lanes = getattr(snapshot, "lanes", {})
        heartbeat_lane = lanes.get("heartbeat")
        command_lane = lanes.get("command_poll")
        heartbeat_failure = str(
            getattr(heartbeat_lane, "failure", "") or ""
        ).strip()
        if heartbeat_failure:
            self._mark_dependency_failed(
                "control_plane",
                heartbeat_failure,
            )
        heartbeat_success = getattr(
            heartbeat_lane,
            "last_success_at",
            False,
        )
        if not heartbeat_failure and heartbeat_success is not False:
            self._last_heartbeat_success_at = float(heartbeat_success)
            self._mark_dependency_ready("control_plane")
        command_failure = str(
            getattr(command_lane, "failure", "") or ""
        ).strip()
        if command_failure:
            self._mark_dependency_failed(
                "command_stream",
                command_failure,
            )
        command_success = getattr(
            command_lane,
            "last_success_at",
            False,
        )
        if not command_failure and command_success is not False:
            self._last_command_success_at = float(command_success)
            if self._command_stream_failure_reason is None:
                self._mark_dependency_ready("command_stream")
        self._evaluate_poll_staleness()

    def _subscribe_terminal_command_results(self) -> None:
        message_bus = self._message_bus()
        subscribe = getattr(message_bus, "subscribe", None)
        if not callable(subscribe):
            return
        topic = f"node.command-results.{self._account_id}"
        try:
            subscribe(topic=topic, handler=self._on_terminal_command_result)
            return
        except TypeError:
            pass
        try:
            subscribe(topic, self._on_terminal_command_result)
            return
        except TypeError:
            pass
        subscribe(topic, callback=self._on_terminal_command_result)

    def _on_terminal_command_result(self, *args: Any, **kwargs: Any) -> None:
        payload = None
        if kwargs:
            payload = (
                kwargs.get("result")
                or kwargs.get("message")
                or kwargs.get("msg")
            )
        if payload is None and args:
            payload = args[-1]
        try:
            self._terminal_results.put_nowait(payload)
        except Full:
            self._fatal_runtime("terminal command result queue capacity exceeded")

    def _drain_terminal_command_results(self) -> int:
        drained = 0
        deadline = time.monotonic() + self._command_apply_time_budget_seconds
        while drained < self._command_apply_max_items:
            if drained > 0 and time.monotonic() >= deadline:
                break
            try:
                payload = self._terminal_results.get_nowait()
            except Empty:
                break
            try:
                result = _parse_terminal_command_result(payload)
                self._accept_terminal_command_result(result)
            except Exception as exc:
                self._fatal_runtime(
                    f"terminal command result persistence failed: {exc!r}"
                )
                return drained
            finally:
                self._terminal_results.task_done()
            drained += 1
        return drained

    def _accept_terminal_command_result(
        self,
        result: _TerminalCommandResult,
    ) -> None:
        entry = self._command_journal.get(result.command_id)
        if entry is None:
            raise RuntimeError(
                f"terminal result lacks command receipt: {result.command_id}"
            )
        if entry.command_type != result.command_type:
            raise RuntimeError(
                f"terminal result type mismatch: {result.command_id}"
            )
        if result.account_id != self._account_id:
            raise RuntimeError(
                f"terminal result account mismatch: {result.command_id}"
            )
        payload = _terminal_command_result_payload(result)
        task = _CommandPersistenceTask(
            operation="applied",
            command_id=result.command_id,
            status="running",
            phase="verifying",
            result_payload=payload,
        )
        if not self._submit_command_persistence(task):
            raise RuntimeError(self._fatal_reason)
        self._pending_terminal_results[result.command_id] = result
        self._pending_terminal_persistence[result.command_id] = task

    def _submit_terminal_verification(self) -> None:
        if self._stopped.is_set():
            return
        if self._terminal_future is not None:
            return
        if not self._pending_terminal_results:
            return
        command_id = next(iter(self._pending_terminal_results))
        persistence = self._pending_terminal_persistence.get(command_id)
        result = self._pending_terminal_results[command_id]
        self._pending_terminal_persistence.pop(command_id, None)
        _, _, _, _, executor = self._ensure_executors()
        self._terminal_future = executor.submit(
            self._verify_terminal_command_after_persistence,
            result,
            persistence,
        )

    def _verify_terminal_command_after_persistence(
        self,
        result: _TerminalCommandResult,
        persistence: _CommandPersistenceTask | None,
    ) -> _TerminalVerificationResult:
        if persistence is not None:
            self._wait_for_command_persistence(persistence)
        return self._verify_terminal_command(result)

    def _verify_terminal_command(
        self,
        result: _TerminalCommandResult,
    ) -> _TerminalVerificationResult:
        provider = self._exchange_evidence_provider
        if provider is None:
            raise RuntimeError("exchange evidence provider unavailable")
        clean_streak = 0
        snapshots = []
        last_residual: dict[str, list[dict[str, Any]]] = {
            "regular_orders": [],
            "algo_orders": [],
            "positions": [],
        }
        for attempt in range(1, self._terminal_verify_attempts + 1):
            snapshot = provider.snapshot(force_refresh=True)
            fetched_at = _terminal_snapshot_fetched_at(snapshot)
            if fetched_at < result.dispatched_at:
                raise RuntimeError(
                    "exchange evidence predates terminal command dispatch"
                )
            residual = _terminal_snapshot_residual(
                result.command_type,
                result.instrument_ids,
                snapshot,
            )
            last_residual = residual
            clean = not any(residual.values())
            snapshots.append(
                {
                    "attempt": attempt,
                    "fetched_at": fetched_at.isoformat(),
                    "clean": clean,
                    "residual": residual,
                }
            )
            if clean:
                clean_streak += 1
            else:
                clean_streak = 0
            if clean_streak >= 2:
                break
            if attempt < self._terminal_verify_attempts:
                time.sleep(self._terminal_verify_delay_seconds)

        operation_errors = list(result.errors)
        status = "completed"
        error = None
        if operation_errors:
            status = "failed"
            error = "terminal command dispatch reported errors"
        if clean_streak < 2:
            status = "failed"
            error = "terminal exchange state contains residual risk"
        verification = {
            "status": status,
            "clean_streak": clean_streak,
            "required_clean_streak": 2,
            "snapshots": snapshots,
            "residual": last_residual,
        }
        payload = {
            **_terminal_command_result_payload(result),
            "verification": verification,
        }
        return _TerminalVerificationResult(
            command_id=result.command_id,
            status=status,
            error=error,
            result=payload,
        )

    def _harvest_terminal_verification(self) -> None:
        future = self._terminal_future
        if future is None or not future.done():
            return
        self._terminal_future = None
        try:
            result = future.result()
        except Exception as exc:
            if not self._pending_terminal_results:
                self._fatal_runtime(
                    f"terminal command verification failed: {exc!r}"
                )
                return
            command_id = next(iter(self._pending_terminal_results))
            pending = self._pending_terminal_results[command_id]
            result = _TerminalVerificationResult(
                command_id=command_id,
                status="failed",
                error=f"terminal command verification failed: {exc!r}",
                result={
                    **_terminal_command_result_payload(pending),
                    "verification": {
                        "status": "failed",
                        "reason": repr(exc),
                    },
                },
            )
        self._finish_terminal_command(result)

    def _finish_terminal_command(
        self,
        result: _TerminalVerificationResult,
    ) -> None:
        from execution_domain.control_plane import CommandAckStatus  # type: ignore

        status = CommandAckStatus(result.status)
        task = _CommandPersistenceTask(
            operation="applied",
            command_id=result.command_id,
            status=status,
            error_text=result.error,
            result_payload=result.result,
        )
        if not self._submit_command_persistence(task):
            return
        self._pending_terminal_results.pop(result.command_id, None)
        self._applied_commands[result.command_id] = (
            status,
            result.error,
        )
        self._pending_acks[result.command_id] = _PendingCommandAck(
            command_id=result.command_id,
            status=status,
            error=result.error,
            result=result.result,
            durability=task,
        )

    def _replay_terminal_commands(self) -> None:
        if not self._terminal_replays:
            return
        command_id = next(iter(self._terminal_replays))
        entry = self._terminal_replays.pop(command_id)
        try:
            from execution_domain.control_plane import (
                CommandAckStatus,
                CommandType,
                NodeCommand,
            )

            command = NodeCommand(
                command_id=entry.command_id,
                type=CommandType(entry.command_type),
                args=dict(entry.args),
            )
            status, error = self._apply(command)
            phase = "applied"
            result_payload = None
            if status is CommandAckStatus.RUNNING:
                phase = "running"
                replay_result = dict(entry.result or {})
                replay_result["replayed_after_restart"] = True
                result_payload = replay_result
            task = _CommandPersistenceTask(
                operation="applied",
                command_id=command_id,
                status=status,
                error_text=error,
                phase=phase,
                result_payload=result_payload,
            )
            if not self._submit_command_persistence(task):
                return
            self._applied_commands[command_id] = (status, error)
            self._pending_acks[command_id] = _PendingCommandAck(
                command_id=command_id,
                status=status,
                error=error,
                result=result_payload,
                durability=task,
            )
        except Exception as exc:
            error = f"terminal command replay failed: {exc!r}"
            task = _CommandPersistenceTask(
                operation="applied",
                command_id=command_id,
                status=CommandAckStatus.FAILED,
                error_text=error,
            )
            if not self._submit_command_persistence(task):
                return
            self._pending_acks[command_id] = _PendingCommandAck(
                command_id=command_id,
                status=CommandAckStatus.FAILED,
                error=error,
                durability=task,
            )

    def _namespace_lease_refresh_due(self) -> bool:
        if self._namespace_lease_process_owned:
            return False
        refreshed_at = self._last_namespace_lease_refresh_at
        if refreshed_at is None:
            return True
        return (
            time.monotonic() - refreshed_at
            >= self._namespace_lease_refresh_interval_seconds
        )

    def _namespace_lease_refresh_barrier_required(self) -> bool:
        if self._namespace_lease is None:
            return False
        if self._namespace_lease_process_owned:
            return bool(
                getattr(self._namespace_lease, "failure_reason", "")
            )
        if self._lease_future is not None:
            return True
        return self._namespace_lease_refresh_due()

    def _submit_commands(self) -> None:
        if self._stopped.is_set():
            return
        _, _, command_executor, _, _ = self._ensure_executors()
        with self._command_poll_lock:
            future = self._command_future
            if future is not None and not future.done():
                self._command_poll_due = True
                return
            self._start_command_poll(command_executor)

    def _start_command_poll(
        self,
        executor: ThreadPoolExecutor,
    ) -> None:
        future = executor.submit(self._poll_commands)
        self._command_future = future
        future.add_done_callback(self._on_command_poll_done)

    def _on_command_poll_done(
        self,
        future: Future[_CommandPollResult],
    ) -> None:
        try:
            self._command_poll_results.put_nowait(future)
        except Full:
            self._fatal_runtime(
                "command poll completion mailbox capacity exceeded"
            )
            return
        with self._command_poll_lock:
            if not self._command_poll_due:
                return
            self._command_poll_due = False
            if self._stopped.is_set():
                return
            executor = self._command_executor
            if executor is None:
                return
            self._start_command_poll(executor)

    def _poll_commands(self) -> _CommandPollResult:
        commands = []
        polled = self._control_plane.poll_commands(self._node_id, None)
        for command in polled:
            if len(commands) >= self._max_pending_commands:
                return _CommandPollResult(
                    commands=tuple(commands),
                    overflowed=True,
                )
            commands.append(command)
        context = self._command_generation_context()
        if not commands:
            return _CommandPollResult(commands=())
        try:
            self._persist_command_task_blocking(
                _CommandPersistenceTask(
                    operation="received_batch",
                    commands=tuple(commands),
                    context=context,
                )
            )
        except Exception as exc:
            raise _CommandJournalWriteError(
                f"command receipt persistence failed: {exc!r}"
            ) from exc
        return _CommandPollResult(commands=tuple(commands))

    def _submit_acks(self) -> None:
        if self._stopped.is_set() or self._ack_future is not None:
            return
        pending = [
            item
            for item in self._pending_acks.values()
            if item.attempts < self._max_ack_attempts
        ]
        if not pending:
            return
        batch = tuple(pending[: self._ack_batch_size])
        for item in batch:
            item.attempts += 1
        ack_executor = self._ensure_ack_executor()
        self._ack_future = ack_executor.submit(
            self._run_durable_ack_batch,
            batch,
        )

    def _run_durable_ack_batch(
        self,
        batch: tuple[_PendingCommandAck, ...],
    ) -> _CommandAckResult:
        for item in batch:
            durability = item.durability
            if isinstance(durability, _CommandPersistenceTask):
                self._wait_for_command_persistence(durability)
        self._persist_command_task_blocking(
            _CommandPersistenceTask(
                operation="attempts",
                attempts={
                    str(item.command_id): item.attempts
                    for item in batch
                },
            )
        )
        result = self._run_ack_batch(batch)
        if result.acked:
            self._persist_command_task_blocking(
                _CommandPersistenceTask(
                    operation="acked",
                    acknowledgements=result.acked,
                )
            )
        return result

    def _run_ack_batch(
        self,
        batch: tuple[_PendingCommandAck, ...],
    ) -> _CommandAckResult:
        acked = []
        failed_command_ids = []
        for item in batch:
            command_id = str(item.command_id)
            try:
                if item.result is None:
                    self._control_plane.ack_command(
                        self._node_id,
                        item.command_id,
                        item.status,
                        error=item.error,
                    )
                else:
                    self._control_plane.ack_command(
                        self._node_id,
                        item.command_id,
                        item.status,
                        result=item.result,
                        error=item.error,
                    )
            except Exception:
                failed_command_ids.append(command_id)
            else:
                raw_status = getattr(item.status, "value", item.status)
                acked.append((command_id, str(raw_status)))
        return _CommandAckResult(
            acked=tuple(acked),
            failed_command_ids=tuple(failed_command_ids),
        )

    def _harvest_heartbeat(self) -> None:
        future = self._heartbeat_future
        if future is None or not future.done():
            return
        self._heartbeat_future = None
        try:
            future.result()
        except Exception as exc:
            print(
                f"[CommandPollerActor] heartbeat failed: {exc!r}",
                flush=True,
            )
            return
        self._last_heartbeat_success_at = time.monotonic()
        self._mark_dependency_ready("control_plane")

    def _harvest_namespace_lease_refresh(self) -> None:
        future = self._lease_future
        if future is None or not future.done():
            return
        self._lease_future = None
        try:
            record = future.result()
            self._record_namespace_lease(record, acquired=False)
        except Exception as exc:
            reason = _namespace_lease_failure_reason(exc)
            invalidator = getattr(self._lifecycle, "invalidate_lease", None)
            if callable(invalidator):
                invalidator(reason)
            self._mark_dependency_failed("redis", reason)
            self._stopped.set()
            callback = self._namespace_lease_lost_callback
            if callback is not None:
                callback(reason)
            return
        self._last_namespace_lease_refresh_at = time.monotonic()
        self._mark_dependency_ready("redis")

    def _acquire_namespace_lease(self) -> None:
        lease = self._namespace_lease
        if lease is None:
            return
        try:
            record = lease.acquire()
            self._record_namespace_lease(record, acquired=True)
        except Exception as exc:
            self._mark_dependency_failed(
                "redis",
                "Redis namespace lease acquisition failed",
            )
            raise RuntimeError(
                "Redis namespace lease acquisition failed"
            ) from exc
        self._last_namespace_lease_refresh_at = time.monotonic()
        refreshed_at_epoch = getattr(record, "refreshed_at_epoch", None)
        if refreshed_at_epoch is not None:
            self._last_namespace_lease_refreshed_at_epoch = int(
                refreshed_at_epoch
            )
        self._mark_dependency_ready("redis")

    def _record_namespace_lease(
        self,
        record: Any,
        *,
        acquired: bool,
    ) -> None:
        generation = _namespace_lease_generation(record)
        redis_fencing_epoch = _namespace_lease_redis_fencing_epoch(record)
        if acquired:
            configure = getattr(self._lifecycle, "configure_lease", None)
            if callable(configure):
                configure(
                    redis_fencing_epoch=redis_fencing_epoch,
                    generation=generation,
                    freshness_seconds=self._namespace_lease_freshness_seconds,
                )
            return
        recorder = getattr(self._lifecycle, "record_lease_refresh", None)
        if callable(recorder):
            recorder(
                redis_fencing_epoch=redis_fencing_epoch,
                generation=generation,
            )

    def _release_namespace_lease(self) -> None:
        lease = self._namespace_lease
        if lease is None:
            return
        if self._namespace_lease_process_owned:
            return
        try:
            lease.release()
        except Exception as exc:
            print(
                "[CommandPollerActor] namespace lease release failed: "
                f"{exc!r}",
                flush=True,
            )

    def _observe_process_namespace_lease(self) -> None:
        if not self._namespace_lease_process_owned:
            return
        guard = self._namespace_lease
        reason = str(getattr(guard, "failure_reason", "") or "").strip()
        if reason:
            self._stopped.set()
            if not bool(getattr(guard, "has_failure_callbacks", False)):
                invalidator = getattr(self._lifecycle, "invalidate_lease", None)
                if callable(invalidator):
                    invalidator(reason)
                self._mark_dependency_failed("redis", reason)
                callback = self._namespace_lease_lost_callback
                if callback is not None:
                    callback(reason)
            return
        record = getattr(guard, "record", False)
        if record is False:
            return
        refreshed_at_epoch = getattr(record, "refreshed_at_epoch", None)
        if refreshed_at_epoch is None:
            return
        refreshed_at_value = int(refreshed_at_epoch)
        if (
            self._last_namespace_lease_refreshed_at_epoch
            == refreshed_at_value
        ):
            return
        self._last_namespace_lease_refreshed_at_epoch = refreshed_at_value
        self._record_namespace_lease(record, acquired=False)
        self._last_namespace_lease_refresh_at = time.monotonic()
        self._mark_dependency_ready("redis")

    def _harvest_commands(self) -> None:
        try:
            future = self._command_poll_results.get_nowait()
        except Empty:
            return
        try:
            result = future.result()
        except _CommandJournalWriteError as exc:
            self._fatal_runtime(str(exc))
            return
        except Exception as exc:
            print(
                f"[CommandPollerActor] command poll failed: {exc!r}",
                flush=True,
            )
            return
        finally:
            self._command_poll_results.task_done()

        if isinstance(result, _CommandPollResult):
            commands = result.commands
            overflowed = result.overflowed
        else:
            commands = tuple(result)
            overflowed = len(commands) > self._max_pending_commands
            commands = commands[: self._max_pending_commands]

        self._last_command_success_at = time.monotonic()
        if self._command_stream_failure_reason is None:
            self._mark_dependency_ready("command_stream")
        pending = self._pending_commands[self._pending_command_index :]
        merged, merge_overflowed = _merge_commands(
            pending,
            commands,
            capacity=self._max_pending_commands,
        )
        self._pending_commands = merged
        self._pending_command_index = 0
        if overflowed or merge_overflowed:
            self._fail_command_stream("operator command queue capacity exceeded")
        self._update_command_queue_pressure()

    def _apply_pending_commands(self) -> int:
        command_ids = {
            str(command.command_id)
            for command in self._pending_commands[
                self._pending_command_index :
            ]
        }
        applied = 0
        deadline = (
            time.monotonic()
            + self._session_completion_timeout_seconds
        )
        while self.pending_command_count > 0:
            applied += self._schedule_pending_commands()
            self._harvest_command_persistence()
            if self._fatal_reason:
                break
            if self.pending_command_count <= 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "command apply timed out waiting for persistence"
                )
            time.sleep(0.001)
        for command_id in command_ids:
            pending = self._pending_acks.get(command_id)
            if pending is None:
                continue
            durability = pending.durability
            if not isinstance(durability, _CommandPersistenceTask):
                continue
            self._wait_for_command_persistence(durability)
        self._harvest_command_persistence()
        return applied

    def _schedule_pending_commands(self) -> int:
        applied = 0
        inspected = 0
        deadline = time.monotonic() + self._command_apply_time_budget_seconds
        while self.pending_command_count > 0:
            if inspected >= self._command_apply_max_items:
                break
            if inspected > 0 and time.monotonic() >= deadline:
                break
            cmd = self._pending_commands[self._pending_command_index]
            inspected += 1
            command_id = str(cmd.command_id)
            journal_entry = self._command_journal.get(command_id)
            receipt_task = self._command_receipt_tasks.get(command_id)
            if (
                journal_entry is not None
                and receipt_task is not None
                and receipt_task.completed.is_set()
            ):
                self._command_receipt_tasks.pop(command_id, None)
                if receipt_task.error is not None:
                    self._fatal_runtime(
                        "command receipt persistence failed: "
                        f"{receipt_task.error!r}"
                    )
                    break
            if journal_entry is not None and journal_entry.phase in {
                "applied",
                "acked",
            }:
                self._queue_journal_ack(
                    journal_entry,
                    force=journal_entry.phase == "acked",
                )
                self._pending_command_index += 1
                continue
            if (
                journal_entry is not None
                and journal_entry.phase in {"running", "verifying"}
                and journal_entry.command_type in TERMINAL_COMMAND_TYPES
            ):
                self._terminal_replays[command_id] = journal_entry
                self._queue_journal_ack(journal_entry)
                self._pending_command_index += 1
                continue
            if len(self._applied_commands) >= self._max_pending_acks:
                self._fail_command_stream("command ack backlog full")
                break
            if journal_entry is None:
                receipt_task = self._submit_pending_command_receipt()
                if receipt_task is None:
                    break
                if not receipt_task.completed.is_set():
                    break
                self._command_receipt_tasks.pop(command_id, None)
                if receipt_task.error is not None:
                    self._fatal_runtime(
                        "command receipt persistence failed: "
                        f"{receipt_task.error!r}"
                    )
                    break
                journal_entry = self._command_journal.get(command_id)
                if journal_entry is None:
                    self._fatal_runtime(
                        "command receipt missing after persistence: "
                        f"{command_id}"
                    )
                    break
            try:
                self._validate_command_generation(cmd, journal_entry)
            except Exception as exc:
                status, error = self._failed_command_result(exc)
            else:
                resume_error = self._resume_command_rejection(cmd)
                if resume_error is not None:
                    status, error = self._reject_resume_command(
                        resume_error
                    )
                else:
                    status, error = self._apply(cmd)
            raw_status = getattr(status, "value", status)
            phase = "applied"
            if str(raw_status) == "running":
                phase = "running"
            result = None
            if phase == "running":
                result = {
                    "phase": "dispatched",
                    "command_type": journal_entry.command_type,
                    "instrument_ids": list(
                        _terminal_command_instrument_ids(
                            journal_entry.args
                        )
                    ),
                }
            persistence = _CommandPersistenceTask(
                operation="applied",
                command_id=command_id,
                status=status,
                error_text=error,
                phase=phase,
                result_payload=result,
            )
            if not self._submit_command_persistence(persistence):
                break
            self._applied_commands[command_id] = (status, error)
            self._pending_acks[command_id] = _PendingCommandAck(
                command_id=command_id,
                status=status,
                error=error,
                result=result,
                durability=persistence,
            )
            self._pending_command_index += 1
            applied += 1

        if self.pending_command_count == 0:
            self._pending_commands = ()
            self._pending_command_index = 0
        self._update_command_queue_pressure()
        return applied

    def _submit_pending_command_receipt(
        self,
    ) -> _CommandPersistenceTask | None:
        if self.pending_command_count <= 0:
            return None
        command = self._pending_commands[self._pending_command_index]
        command_id = str(command.command_id)
        if self._command_journal.get(command_id) is not None:
            return self._command_receipt_tasks.get(command_id)
        existing = self._command_receipt_tasks.get(command_id)
        if existing is not None:
            return existing
        task = _CommandPersistenceTask(
            operation="received_batch",
            commands=(command,),
            context=self._command_generation_context(),
        )
        if not self._submit_command_persistence(task):
            return None
        self._command_receipt_tasks[command_id] = task
        return task

    def _update_command_queue_pressure(self) -> None:
        pending = self.pending_command_count
        if pending >= self._max_pending_commands:
            self._fail_command_stream("operator command queue reached capacity")
            return
        usage_ratio = pending / self._max_pending_commands
        if usage_ratio >= DEFAULT_QUEUE_DEGRADED_RATIO:
            self._mark_dependency_degraded(
                "command_stream",
                "operator command queue usage "
                f"{usage_ratio:.1%} exceeds degraded threshold",
            )
            return
        if self._command_persistence_degraded:
            return
        self._clear_dependency_degraded("command_stream")

    def _harvest_acks(self) -> None:
        future = self._ack_future
        if future is None or not future.done():
            return
        self._ack_future = None
        try:
            result = future.result()
        except Exception as exc:
            print(
                f"[CommandPollerActor] ack batch failed: {exc!r}",
                flush=True,
            )
            return

        for command_id, _status in result.acked:
            self._pending_acks.pop(command_id, None)
            entry = self._command_journal.get(command_id)
            if entry is not None and entry.phase == "acked":
                self._applied_commands.pop(command_id, None)
                continue
            if entry is not None:
                self._queue_journal_ack(entry)

        for command_id in result.failed_command_ids:
            pending = self._pending_acks.get(command_id)
            if pending is None:
                continue
            if pending.attempts < self._max_ack_attempts:
                continue
            self._pending_acks.pop(command_id, None)
            self._fail_command_stream(
                f"command ack retry limit reached for {command_id}"
            )

    def _evaluate_poll_staleness(self) -> None:
        now = time.monotonic()
        heartbeat_reference = self._last_heartbeat_success_at
        if heartbeat_reference is None:
            heartbeat_reference = self._started_at
        if now - heartbeat_reference >= self._stale_after_seconds:
            self._mark_dependency_failed(
                "control_plane",
                "control-plane heartbeat stale",
            )

        command_reference = self._last_command_success_at
        if command_reference is None:
            command_reference = self._started_at
        if now - command_reference >= self._stale_after_seconds:
            self._mark_dependency_failed(
                "command_stream",
                "operator command poll stale",
            )

    def _mark_dependency_ready(self, dependency_value: str) -> None:
        dependency = _dependency_by_value(dependency_value)
        marker = getattr(self._lifecycle, "mark_dependency_ready", None)
        if dependency is None or not callable(marker):
            self._failed_dependencies.discard(dependency_value)
            return
        marker(dependency)
        self._failed_dependencies.discard(dependency_value)

    def _mark_dependency_failed(self, dependency_value: str, reason: str) -> None:
        if dependency_value in self._failed_dependencies:
            return
        dependency = _dependency_by_value(dependency_value)
        marker = getattr(self._lifecycle, "mark_dependency_failed", None)
        if dependency is None or not callable(marker):
            self._failed_dependencies.add(dependency_value)
            return
        marker(dependency, reason)
        self._failed_dependencies.add(dependency_value)

    def _mark_dependency_degraded(
        self,
        dependency_value: str,
        reason: str,
    ) -> None:
        if dependency_value in self._degraded_dependencies:
            return
        self._degraded_dependencies.add(dependency_value)
        marker = getattr(self._lifecycle, "mark_dependency_degraded", None)
        dependency = _dependency_by_value(dependency_value)
        if dependency is not None and callable(marker):
            marker(dependency, reason)
            return
        print(f"[CommandPollerActor] DEGRADED: {reason}", flush=True)

    def _clear_dependency_degraded(self, dependency_value: str) -> None:
        if dependency_value not in self._degraded_dependencies:
            return
        self._degraded_dependencies.discard(dependency_value)
        clearer = getattr(self._lifecycle, "clear_dependency_degraded", None)
        dependency = _dependency_by_value(dependency_value)
        if dependency is not None and callable(clearer):
            clearer(dependency)

    def _fail_command_stream(self, reason: str) -> None:
        self._command_stream_failure_reason = reason
        self._mark_dependency_failed("command_stream", reason)

    def _fatal_runtime(self, reason: str) -> None:
        if self._fatal_reason:
            return
        self._fatal_reason = reason
        self._fail_command_stream(reason)
        self._stopped.set()
        callback = self._fatal_callback
        if callback is not None:
            callback(reason)

    def _restore_command_journal(self) -> None:
        from execution_domain.control_plane import CommandAckStatus  # type: ignore

        for entry in self._command_journal.entries():
            restored = entry
            if entry.phase == "received":
                if entry.command_type in TERMINAL_COMMAND_TYPES:
                    self._terminal_replays[entry.command_id] = entry
                    continue
                persistence = self._persist_command_task_blocking(
                    _CommandPersistenceTask(
                        operation="applied",
                        command_id=entry.command_id,
                        status=CommandAckStatus.FAILED,
                        error_text=(
                            "command outcome uncertain after runtime restart"
                        ),
                    )
                )
                restored = persistence.journal_entry
                if restored is None:
                    raise RuntimeError(
                        "restored command journal entry is missing"
                    )
            if entry.phase in {"running", "verifying"}:
                if entry.command_type in TERMINAL_COMMAND_TYPES:
                    self._terminal_replays[entry.command_id] = entry
                    self._queue_journal_ack(entry)
                    continue
            if restored.phase not in {"applied", "acked"}:
                continue
            restored = self._refresh_restored_resume(restored)
            self._queue_journal_ack(restored)

    def _queue_journal_ack(
        self,
        entry: _CommandJournalEntry,
        *,
        force: bool = False,
    ) -> None:
        if entry.command_id in self._pending_acks:
            return
        if entry.status is None:
            raise RuntimeError(
                f"command journal applied entry lacks status: {entry.command_id}"
            )
        from execution_domain.control_plane import CommandAckStatus  # type: ignore

        status_value = entry.status
        if (
            entry.command_type in TERMINAL_COMMAND_TYPES
            and entry.acked_status is None
        ):
            status_value = CommandAckStatus.RUNNING.value
        if entry.acked_status == status_value and not force:
            return
        if len(self._pending_acks) >= self._max_pending_acks:
            self._fatal_runtime(
                "command ACK durable outbox capacity exceeded"
            )
            return
        status = CommandAckStatus(status_value)
        self._pending_acks[entry.command_id] = _PendingCommandAck(
            command_id=entry.command_id,
            status=status,
            error=entry.error,
            result=entry.result,
            attempts=entry.attempts,
        )

    def _command_generation_context(self) -> _CommandGenerationContext:
        runtime_generation = str(
            getattr(self._lifecycle, "runtime_generation", "") or ""
        )
        reconciliation_generation = int(
            getattr(self._lifecycle, "reconciliation_generation", 0) or 0
        )
        lease_generation = getattr(self._lifecycle, "lease_generation", 0)
        return _CommandGenerationContext(
            runtime_generation=runtime_generation,
            reconciliation_generation=reconciliation_generation,
            lease_generation=lease_generation,
        )

    def _validate_command_generation(
        self,
        command: Any,
        entry: _CommandJournalEntry,
    ) -> None:
        raw_command_type = getattr(command, "type", "unknown")
        command_type = getattr(raw_command_type, "value", raw_command_type)
        if str(command_type) != "resume":
            return
        validator = getattr(
            self._lifecycle,
            "validate_risk_generation",
            None,
        )
        if not callable(validator):
            return
        validator(
            runtime_generation=entry.runtime_generation,
            reconciliation_generation=entry.reconciliation_generation,
            lease_generation=entry.lease_generation,
        )

    def _resume_command_rejection(self, command: Any) -> str | None:
        raw_command_type = getattr(command, "type", "unknown")
        command_type = getattr(raw_command_type, "value", raw_command_type)
        if str(command_type) != "resume":
            return None
        issued_at = getattr(command, "issued_at", None)
        return self._resume_issued_at_rejection(issued_at)

    def _resume_issued_at_rejection(
        self,
        issued_at: Any,
    ) -> str | None:
        if issued_at is None:
            return "resume command issued_at is required"
        if isinstance(issued_at, str):
            try:
                issued_at = datetime.fromisoformat(
                    issued_at.replace("Z", "+00:00")
                )
            except ValueError:
                return "resume command issued_at is invalid"
        if not isinstance(issued_at, datetime):
            return "resume command issued_at is invalid"
        if issued_at.tzinfo is None:
            return "resume command issued_at must include timezone"
        now = self._command_now()
        if now.tzinfo is None:
            raise RuntimeError("command_now must return timezone-aware datetime")
        age_seconds = (
            now.astimezone(timezone.utc)
            - issued_at.astimezone(timezone.utc)
        ).total_seconds()
        if age_seconds <= self._resume_command_max_age_seconds:
            return None
        return (
            "resume command expired: age "
            f"{age_seconds:.3f}s exceeds "
            f"{self._resume_command_max_age_seconds:.3f}s"
        )

    def _reject_resume_command(
        self,
        reason: str,
    ) -> tuple[Any, str]:
        from execution_domain.control_plane import (  # type: ignore
            CommandAckStatus,
            TradingState,
        )

        self._lifecycle.apply_operator_state(
            TradingState.HALTED,
            "resume_command_rejected",
        )
        return CommandAckStatus.FAILED, reason

    def _refresh_restored_resume(
        self,
        entry: _CommandJournalEntry,
    ) -> _CommandJournalEntry:
        if entry.command_type != "resume":
            return entry
        if entry.status != "completed":
            return entry
        reason = self._resume_issued_at_rejection(entry.issued_at)
        if reason is None:
            return entry
        status, error = self._reject_resume_command(reason)
        return self._command_journal.record_applied(
            entry.command_id,
            status,
            error,
        )

    @staticmethod
    def _failed_command_result(exc: Exception) -> tuple[Any, str]:
        from execution_domain.control_plane import CommandAckStatus  # type: ignore

        return CommandAckStatus.FAILED, repr(exc)

    def _message_bus(self) -> Any:
        return _first_attr(self, ("msgbus", "message_bus", "_msgbus"))

    def _ensure_open_orders_provider(self) -> None:
        if getattr(self, "_open_orders_provider_registered", False):
            return
        register = getattr(self._lifecycle, "set_open_orders_provider", None)
        if not callable(register):
            return
        register(lambda: open_orders_snapshot(self._cache()))
        self._open_orders_provider_registered = True

    def _ensure_tick_watchdog(self) -> Any:
        watchdog = self._tick_watchdog
        if watchdog is not None:
            return watchdog
        from runtime.lifecycle import ActorTickWatchdog  # type: ignore

        watchdog = ActorTickWatchdog(
            self._lifecycle,
            stale_after_seconds=self._actor_tick_stale_after_seconds,
            restart_after_seconds=self._actor_tick_restart_after_seconds,
            check_interval_seconds=self._actor_tick_check_interval_seconds,
            on_stale=self._actor_tick_stale_callback,
            on_restart_required=self._restart_required_callback,
        )
        self._tick_watchdog = watchdog
        return watchdog

    def _record_actor_tick(self) -> None:
        watchdog = self._tick_watchdog
        if watchdog is not None:
            watchdog.record_tick()
            return
        recorder = getattr(self._lifecycle, "record_actor_tick", None)
        if callable(recorder):
            recorder()

    def _cache(self) -> Any:
        return _first_attr(self, ("cache", "_cache")) or _first_attr(
            self._lifecycle, ("cache", "_cache")
        )

    def _apply(self, cmd: Any):
        from execution_domain.control_plane import (  # type: ignore
            CommandAckStatus,
            CommandType,
            TradingState,
        )

        try:
            if cmd.type == CommandType.HALT:
                self._lifecycle.apply_operator_state(
                    TradingState.HALTED, "operator_command"
                )
            elif cmd.type == CommandType.RESUME:
                if _lifecycle_is_live(self._lifecycle):
                    from runtime.live_canary_execution import (
                        is_live_canary_account,
                    )

                    if is_live_canary_account(self._account_id):
                        command_account_id = _node_command_account_id(cmd)
                        if not command_account_id:
                            return self._reject_resume_command(
                                "resume_command_account_required"
                            )
                        if command_account_id != self._account_id:
                            return self._reject_resume_command(
                                "resume_command_account_mismatch"
                            )
                        args = _json_object(
                            getattr(cmd, "args", {})
                        )
                        canary_resume = isinstance(
                            args.get("canary_permit"),
                            dict,
                        )
                        validate_gate = getattr(
                            self._lifecycle,
                            "validate_live_open_gate",
                            None,
                        )
                        if not callable(validate_gate):
                            return self._reject_resume_command(
                                "resume_live_open_gate_validator_unavailable"
                            )
                        try:
                            validate_gate(
                                args.get("live_open_gate"),
                                require_normal=not canary_resume,
                            )
                        except Exception as exc:
                            return self._reject_resume_command(str(exc))
                self._lifecycle.apply_operator_state(
                    TradingState.ACTIVE, "operator_command"
                )
            elif cmd.type == CommandType.SET_REDUCING:
                self._lifecycle.apply_operator_state(
                    TradingState.REDUCING, "operator_command"
                )
            elif cmd.type in (CommandType.CANCEL_ALL, CommandType.CLOSE_ALL):
                if not _node_command_has_authorization(cmd):
                    return CommandAckStatus.FAILED, "authorization_source_required"
                command_account_id = _node_command_account_id(cmd)
                if not command_account_id:
                    return CommandAckStatus.FAILED, "command_account_required"
                if command_account_id != self._account_id:
                    return CommandAckStatus.FAILED, "command_account_mismatch"
                _terminal_command_instrument_ids(
                    _json_object(getattr(cmd, "args", {}))
                )
                if cmd.type == CommandType.CLOSE_ALL:
                    self._lifecycle.apply_operator_state(
                        TradingState.REDUCING,
                        "operator_close_all",
                    )
                message_bus = self._message_bus()
                if message_bus is not None and hasattr(message_bus, "publish"):
                    message_bus.publish(
                        topic=f"node.commands.{self._account_id}", msg=cmd
                    )
                    return CommandAckStatus.RUNNING, None
                return CommandAckStatus.FAILED, "no_msgbus_for_dispatch"
            else:
                return CommandAckStatus.ACCEPTED, "node_action_not_wired"
            return CommandAckStatus.COMPLETED, None
        except Exception as exc:  # e.g. readiness gate on RESUME
            return CommandAckStatus.FAILED, repr(exc)


def _lifecycle_is_live(lifecycle: Any) -> bool:
    config = getattr(lifecycle, "config", None)
    binance = getattr(config, "binance", None)
    environment = str(
        getattr(binance, "environment", "") or ""
    ).strip().lower()
    return environment == "live"


def _command_issued_at_text(command: Any) -> str | None:
    issued_at = getattr(command, "issued_at", None)
    if issued_at is None:
        return None
    if isinstance(issued_at, datetime):
        if issued_at.tzinfo is None:
            return issued_at.isoformat()
        return issued_at.astimezone(timezone.utc).isoformat()
    return str(issued_at)


def _heartbeat_receipt_requires_halt(
    receipt: Any,
    *,
    live: bool,
) -> bool:
    if receipt is None:
        return live
    requires_halt = getattr(receipt, "requires_sticky_halt", None)
    if requires_halt is None:
        return live
    if bool(requires_halt):
        return True
    if not live:
        return False
    from runtime.live_canary_execution import (
        normalize_live_open_gate,
    )

    gate = getattr(receipt, "release_gate", None)
    raw_live_open_gate = {
        "mode": getattr(gate, "live_open_mode", None),
        "release_id": getattr(gate, "release_id", None),
        "rollout_phase": getattr(gate, "rollout_phase", None),
        "phase_version": getattr(gate, "phase_version", None),
    }
    return normalize_live_open_gate(raw_live_open_gate) is False


def _heartbeat_receipt_halt_reason(receipt: Any) -> str:
    if receipt is None:
        return "heartbeat receipt is missing"
    gate = getattr(receipt, "release_gate", None)
    gate_status = str(
        getattr(gate, "status", "") or ""
    ).strip()
    if gate_status != "pass":
        return (
            "heartbeat release gate failed: "
            f"{gate_status or 'missing'}"
        )
    from runtime.live_canary_execution import (
        normalize_live_open_gate,
    )

    raw_live_open_gate = {
        "mode": getattr(gate, "live_open_mode", None),
        "release_id": getattr(gate, "release_id", None),
        "rollout_phase": getattr(gate, "rollout_phase", None),
        "phase_version": getattr(gate, "phase_version", None),
    }
    if normalize_live_open_gate(raw_live_open_gate) is False:
        return "heartbeat live open gate is invalid"
    peers = tuple(getattr(receipt, "peers", ()) or ())
    for peer in peers:
        node_id = str(
            getattr(peer, "node_id", "") or "unknown"
        )
        fresh = getattr(peer, "fresh", False) is True
        identity_matches = (
            getattr(peer, "identity_matches", False) is True
        )
        if not fresh:
            return f"heartbeat peer is stale: {node_id}"
        if not identity_matches:
            return f"heartbeat peer release drift: {node_id}"
    return "heartbeat receipt requires sticky HALT"


def _init_actor_base(instance: Actor) -> None:
    try:
        Actor.__init__(instance)
    except TypeError:
        Actor.__init__(instance, config=None)


def _submission_was_accepted(result: Any) -> bool:
    value = getattr(result, "value", result)
    return str(value).strip().lower() == "accepted"


def _shutdown_executor(
    executor: ThreadPoolExecutor | None,
    future: Future[Any] | None,
    *,
    deadline: float,
) -> bool:
    if executor is None:
        return True
    if future is not None and not future.done():
        remaining = max(deadline - time.monotonic(), 0.0)
        try:
            future.result(timeout=remaining)
        except FutureTimeoutError:
            pass
        except Exception:
            pass
    wait = future is None or future.done()
    executor.shutdown(wait=wait, cancel_futures=True)
    return wait


def _first_attr(source: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        try:
            value = getattr(source, name, None)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError("expected a JSON object")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError("expected a JSON object")
    return decoded


def _optional_json_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return _json_object(value)


def _merge_commands(
    pending: Iterable[Any],
    incoming: Iterable[Any],
    *,
    capacity: int,
) -> tuple[tuple[Any, ...], bool]:
    merged = []
    command_ids = set()
    overflowed = False
    commands = (*tuple(pending), *tuple(incoming))
    for command in commands:
        command_id = str(getattr(command, "command_id", ""))
        if command_id in command_ids:
            continue
        if len(merged) >= capacity:
            overflowed = True
            continue
        command_ids.add(command_id)
        merged.append(command)
    return tuple(merged), overflowed


def _terminal_command_instrument_ids(
    args: dict[str, Any],
) -> tuple[str, ...]:
    raw = args.get("instrument_ids")
    alias = args.get("instruments")
    if raw is not None and alias is not None and raw != alias:
        raise ValueError("instrument scope fields disagree")
    if raw is None:
        raw = alias
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("instrument_ids must be a list")
    result = []
    seen = set()
    for item in raw:
        value = str(item or "").strip()
        if not value:
            raise ValueError(
                "instrument_ids must contain non-empty strings"
            )
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _parse_terminal_command_result(
    payload: Any,
) -> _TerminalCommandResult:
    if not isinstance(payload, dict):
        raise TypeError("terminal command result must be an object")
    command_id = str(payload.get("command_id") or "").strip()
    command_type = str(payload.get("command_type") or "").strip().lower()
    account_id = str(payload.get("account_id") or "").strip()
    if not command_id or command_type not in TERMINAL_COMMAND_TYPES:
        raise ValueError("terminal command result identity is invalid")
    if not account_id:
        raise ValueError("terminal command result account_id is required")
    raw_instruments = payload.get("instrument_ids", [])
    instrument_ids = _terminal_command_instrument_ids(
        {"instrument_ids": raw_instruments}
    )
    raw_operations = payload.get("operations", [])
    if not isinstance(raw_operations, list):
        raise TypeError("terminal command operations must be a list")
    operations = tuple(_json_object(item) for item in raw_operations)
    raw_errors = payload.get("errors", [])
    if not isinstance(raw_errors, list):
        raise TypeError("terminal command errors must be a list")
    errors = tuple(str(item) for item in raw_errors if str(item))
    dispatched_at = _parse_terminal_datetime(
        payload.get("dispatched_at"),
        "dispatched_at",
    )
    completed_at = _parse_terminal_datetime(
        payload.get("completed_at"),
        "completed_at",
    )
    if completed_at < dispatched_at:
        raise ValueError("terminal command result time order is invalid")
    return _TerminalCommandResult(
        command_id=command_id,
        command_type=command_type,
        account_id=account_id,
        instrument_ids=instrument_ids,
        operations=operations,
        errors=errors,
        dispatched_at=dispatched_at,
        completed_at=completed_at,
    )


def _parse_terminal_datetime(value: Any, label: str) -> datetime:
    parsed = value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError(f"terminal command {label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _terminal_command_result_payload(
    result: _TerminalCommandResult,
) -> dict[str, Any]:
    return {
        "command_id": result.command_id,
        "command_type": result.command_type,
        "account_id": result.account_id,
        "instrument_ids": list(result.instrument_ids),
        "operations": [dict(item) for item in result.operations],
        "errors": list(result.errors),
        "dispatched_at": result.dispatched_at.isoformat(),
        "completed_at": result.completed_at.isoformat(),
    }


def _terminal_snapshot_fetched_at(snapshot: Any) -> datetime:
    if not isinstance(snapshot, dict):
        raise TypeError("exchange evidence snapshot must be an object")
    return _parse_terminal_datetime(
        snapshot.get("fetched_at"),
        "snapshot fetched_at",
    )


def _terminal_snapshot_residual(
    command_type: str,
    instrument_ids: tuple[str, ...],
    snapshot: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    target_symbols = {
        _canonical_terminal_symbol(instrument_id)
        for instrument_id in instrument_ids
    }
    regular_orders = _scoped_terminal_rows(
        snapshot.get("regular_orders"),
        target_symbols,
    )
    algo_orders = _scoped_terminal_rows(
        snapshot.get("algo_orders"),
        target_symbols,
    )
    positions: list[dict[str, Any]] = []
    if command_type == "close_all":
        positions = _scoped_terminal_rows(
            snapshot.get("positions"),
            target_symbols,
        )
    return {
        "regular_orders": regular_orders,
        "algo_orders": algo_orders,
        "positions": positions,
    }


def _scoped_terminal_rows(
    raw_rows: Any,
    target_symbols: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(raw_rows, list):
        raise TypeError("exchange evidence rows must be a list")
    rows = []
    for raw in raw_rows:
        row = _json_object(raw)
        symbol = _canonical_terminal_symbol(row.get("symbol"))
        if target_symbols and symbol not in target_symbols:
            continue
        rows.append(row)
    return rows


def _canonical_terminal_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        raise ValueError("terminal command instrument symbol is required")
    return text.split("-")[0].split(".")[0]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _dependency_by_value(value: str) -> Any:
    try:
        from runtime.lifecycle import DependencyName  # type: ignore
    except ImportError:
        return None
    for dependency in DependencyName:
        if dependency.value == value:
            return dependency
    return None


def _namespace_lease_failure_reason(exc: Exception) -> str:
    detail = str(exc).strip()
    if not detail:
        detail = type(exc).__name__
    return f"Redis namespace lease refresh failed: {detail}"


def _namespace_lease_generation(record: Any) -> int:
    generation = getattr(record, "fencing_token", None)
    if generation is None:
        raise RuntimeError("Redis namespace lease fencing token is missing")
    value = int(generation)
    if value < 1:
        raise RuntimeError("Redis namespace lease fencing token is invalid")
    return value


def _namespace_lease_redis_fencing_epoch(record: Any) -> str:
    value = str(
        getattr(record, "redis_fencing_epoch", "") or ""
    ).strip()
    if not value:
        raise RuntimeError("Redis namespace lease fencing epoch is missing")
    return value


def _node_command_has_authorization(cmd: Any) -> bool:
    args = getattr(cmd, "args", {})
    if not isinstance(args, dict):
        return False
    authorization = args.get("authorization")
    if not isinstance(authorization, dict):
        return False
    authorized_by_type = str(authorization.get("authorized_by_type") or "").strip()
    authorized_by_id = str(authorization.get("authorized_by_id") or "").strip()
    source_message_id = str(authorization.get("source_message_id") or "").strip()
    return (
        authorized_by_type in {"user", "channel"}
        and bool(authorized_by_id)
        and bool(source_message_id)
    )


def _node_command_account_id(cmd: Any) -> str:
    args = getattr(cmd, "args", {})
    if not isinstance(args, dict):
        return ""
    return str(args.get("account_id") or "").strip()
