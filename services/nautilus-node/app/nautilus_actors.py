from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import timedelta
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
DEFAULT_PENDING_COMMAND_LIMIT = 128
DEFAULT_PENDING_COMMAND_ACK_LIMIT = 256
DEFAULT_COMMAND_ACK_BATCH_SIZE = 16
DEFAULT_WORKER_SHUTDOWN_WAIT_SECONDS = 0.5
DEFAULT_CALLBACK_MAX_ITEMS = 16
DEFAULT_CALLBACK_TIME_BUDGET_SECONDS = 0.005
DEFAULT_EXECUTION_EVENT_QUEUE_CAPACITY = 1024
DEFAULT_PROJECTION_DURABLE_INGRESS_DEADLINE_SECONDS = 0.5


@dataclass
class _PendingCommandAck:
    command_id: Any
    status: Any
    error: str | None


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
    cancelled: Event = field(default_factory=Event)
    acknowledgement: _PendingCommandAck | None = None
    error: Exception | None = None


@dataclass
class _ProjectionPublication:
    event: Any = None
    flush_only: bool = False
    deadline_at: float | None = None
    completed: Event = field(default_factory=Event)
    error: Exception | None = None


class _QueueingIntentPublisher:
    def __init__(
        self,
        pending: Queue[_IntentPublication],
        *,
        enqueue_timeout_seconds: float,
        completion_timeout_seconds: float,
        failure_callback: Callable[[str], None],
    ) -> None:
        self._pending = pending
        self._enqueue_timeout_seconds = enqueue_timeout_seconds
        self._completion_timeout_seconds = completion_timeout_seconds
        self._failure_callback = failure_callback
        self._stopped = Event()
        self._admission_lock = RLock()

    def stop(self) -> None:
        with self._admission_lock:
            self._stopped.set()
            while True:
                try:
                    publication = self._pending.get_nowait()
                except Empty:
                    return
                publication.cancelled.set()
                publication.error = RuntimeError(
                    "intent publisher actor stopped before publication"
                )
                publication.completed.set()
                self._pending.task_done()

    def publish(self, intent: Any) -> None:
        publication = _IntentPublication(intent=intent)
        with self._admission_lock:
            if self._stopped.is_set():
                raise RuntimeError("intent publisher actor is stopped")
            try:
                self._pending.put(
                    publication,
                    timeout=self._enqueue_timeout_seconds,
                )
            except Full as exc:
                reason = "intent publication backlog full"
                self._failure_callback(reason)
                raise RuntimeError(reason) from exc

        deadline = time.monotonic() + self._completion_timeout_seconds
        while not publication.completed.wait(timeout=0.01):
            if self._stopped.is_set():
                publication.cancelled.set()
                raise RuntimeError(
                    "intent publisher actor stopped before publication"
                )
            if time.monotonic() >= deadline:
                reason = (
                    "intent publication timed out waiting for actor thread"
                )
                publication.cancelled.set()
                self._failure_callback(reason)
                raise RuntimeError(reason)
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
    return {key: _string_value(value) for key, value in fields.items() if value is not None}


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
    for order in _safe_call(cache, ("orders_open", "open_orders", "orders_active")) or ():
        if _string_value(_order_field(order, ("client_order_id", "client_id"))) == _string_value(client_order_id):
            return order
    return None


def order_event_payload_fields(event: Any, cache: Any = None) -> dict:
    client_order_id = _safe_attr(event, ("client_order_id", "client_id"))
    order = _safe_attr(event, ("order",)) or _cache_order(cache, client_order_id)
    if client_order_id is None and order is None:
        return {}
    merged = order_snapshot_payload(order) if order is not None else {}
    event_fields = order_snapshot_payload(event)
    merged.update({key: value for key, value in event_fields.items() if value is not None})
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
        worker_shutdown_wait_seconds: float = (
            DEFAULT_WORKER_SHUTDOWN_WAIT_SECONDS
        ),
        callback_max_items: int = DEFAULT_CALLBACK_MAX_ITEMS,
        callback_time_budget_seconds: float = (
            DEFAULT_CALLBACK_TIME_BUDGET_SECONDS
        ),
        control_plane_session: Any = None,
        manage_control_plane_session: bool = True,
    ) -> None:
        _init_actor_base(self)
        if pending_intent_limit < 1:
            raise ValueError("pending_intent_limit must be positive")
        if publication_enqueue_timeout_seconds < 0:
            raise ValueError(
                "publication_enqueue_timeout_seconds must be non-negative"
            )
        if publication_completion_timeout_seconds <= 0:
            raise ValueError(
                "publication_completion_timeout_seconds must be positive"
            )
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
        self._stale_after_seconds = float(stale_after_seconds)
        self._worker_shutdown_wait_seconds = max(
            float(worker_shutdown_wait_seconds),
            0.0,
        )
        self._callback_max_items = int(callback_max_items)
        self._callback_time_budget_seconds = float(
            callback_time_budget_seconds
        )
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
            enqueue_timeout_seconds=float(
                publication_enqueue_timeout_seconds
            ),
            completion_timeout_seconds=float(
                publication_completion_timeout_seconds
            ),
            failure_callback=self._record_failure,
        )
        self._started_at = time.monotonic()
        self._last_poll_success_at: float | None = None
        self._intent_stream_failed = False
        self._failure_reason = ""
        self._degraded_reason = ""
        self._attach_to_plain_client_publisher(self._queued_publisher)

    @property
    def failure_reason(self) -> str:
        return self._failure_reason

    @property
    def degraded_reason(self) -> str:
        return self._degraded_reason

    @property
    def pending_intent_count(self) -> int:
        return self._pending_intents.qsize()

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
        self._queued_publisher.stop()
        deadline = (
            time.monotonic() + self._worker_shutdown_wait_seconds
        )
        session = self._control_plane_session
        if session is not None and self._manage_control_plane_session:
            try:
                stopped = bool(session.stop(deadline))
            except Exception as exc:
                stopped = False
                self._record_failure(
                    f"control-plane session stop failed: {exc!r}"
                )
            if not stopped:
                self._record_failure(
                    "control-plane session failed to stop before deadline"
                )
        worker_stopped = _shutdown_executor(
            self._executor,
            self._poll_future,
            deadline=deadline,
        )
        if worker_stopped:
            self._executor = None
            self._poll_future = None
        else:
            self._record_failure(
                "approved intent poll worker failed to stop before deadline"
            )

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
        self._drain_pending_intents()
        if self._control_plane_session is not None:
            self._evaluate_session_health()
            return
        self._harvest_poll()
        self._submit_poll()
        self._evaluate_poll_staleness()

    def _ensure_executor(self) -> ThreadPoolExecutor:
        executor = self._executor
        if executor is not None:
            return executor
        executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"{self._timer_name}.worker",
        )
        self._executor = executor
        return executor

    def _submit_poll(self) -> None:
        if self._stopped.is_set():
            return
        future = self._poll_future
        if future is not None:
            return
        self._poll_future = self._ensure_executor().submit(
            self._poll_client_once
        )

    def _harvest_poll(self) -> None:
        future = self._poll_future
        if future is None or not future.done():
            return
        self._poll_future = None
        try:
            future.result()
        except Exception as exc:
            self._record_degraded(
                f"approved intent poll failed: {exc!r}"
            )
            return
        self._record_poll_progress()

    def _drain_pending_intents(self) -> int:
        published = 0
        inspected = 0
        deadline = (
            time.monotonic() + self._callback_time_budget_seconds
        )
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
                self._record_failure(
                    f"intent publication failed: {exc!r}"
                )
            finally:
                publication.completed.set()
                self._pending_intents.task_done()
        return published

    def _evaluate_poll_staleness(self) -> None:
        reference = self._last_poll_success_at
        if reference is None:
            reference = self._started_at
        if time.monotonic() - reference < self._stale_after_seconds:
            return
        self._record_failure("approved intent poll stale")

    def _evaluate_session_health(self) -> None:
        session = self._control_plane_session
        if session is None:
            return
        try:
            snapshot = session.snapshot()
        except Exception as exc:
            self._record_degraded(
                f"control-plane session snapshot failed: {exc!r}"
            )
            self._evaluate_poll_staleness()
            return
        lanes = getattr(snapshot, "lanes", {})
        intent_lanes = (
            ("intent_fetch", lanes.get("intent_fetch")),
            ("intent_delivery", lanes.get("intent_delivery")),
        )
        for lane_name, lane in intent_lanes:
            failure = _lane_hard_failure(lane_name, lane)
            if failure:
                self._record_failure(failure)
                return
        for lane_name, lane in intent_lanes:
            failure = _lane_degraded_failure(lane_name, lane)
            if failure:
                self._record_degraded(failure)
                self._evaluate_poll_staleness()
                return
        if bool(getattr(snapshot, "degraded", False)):
            self._record_degraded("control-plane intent session degraded")
            self._evaluate_poll_staleness()
            return
        fetch_lane = lanes.get("intent_fetch")
        last_success = getattr(fetch_lane, "last_success_at", False)
        if last_success is not False:
            self._record_poll_progress()
            return
        self._evaluate_poll_staleness()

    def _record_poll_progress(self) -> None:
        self._last_poll_success_at = time.monotonic()
        self._degraded_reason = ""
        self._mark_dependency_ready("intent_stream")

    def _record_failure(self, reason: str) -> None:
        self._failure_reason = reason
        self._degraded_reason = ""
        self._mark_dependency_failed("intent_stream", reason)

    def _record_degraded(self, reason: str) -> None:
        if self._intent_stream_failed:
            return
        self._degraded_reason = reason
        self._mark_dependency_degraded("intent_stream", reason)

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

    def _attach_to_plain_client_publisher(
        self,
        selected_publisher: Any,
    ) -> None:
        publisher = getattr(self._intent_data_client, "_publisher", None)
        attach = getattr(publisher, "attach", None)
        if callable(attach):
            attach(selected_publisher)

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

    def _message_bus(self) -> Any:
        return _first_attr(self, ("msgbus", "message_bus", "_msgbus"))

    def _mark_dependency_ready(self, dependency_value: str) -> None:
        dependency = _dependency_by_value(dependency_value)
        marker = getattr(self._lifecycle, "mark_dependency_ready", None)
        if dependency is not None and callable(marker):
            marker(dependency)
        self._intent_stream_failed = False

    def _mark_dependency_failed(
        self,
        dependency_value: str,
        reason: str,
    ) -> None:
        if self._intent_stream_failed:
            return
        dependency = _dependency_by_value(dependency_value)
        marker = getattr(self._lifecycle, "mark_dependency_failed", None)
        if dependency is not None and callable(marker):
            marker(dependency, reason)
        self._intent_stream_failed = True

    def _mark_dependency_degraded(
        self,
        dependency_value: str,
        reason: str,
    ) -> None:
        dependency = _dependency_by_value(dependency_value)
        marker = getattr(
            self._lifecycle,
            "mark_dependency_degraded",
            None,
        )
        if dependency is not None and callable(marker):
            marker(dependency, reason)


class ExecutionProjectionActor(Actor):
    """Nautilus ``Actor`` wrapper around the plain execution ``ProjectionActor``."""

    def __init__(
        self,
        projection_actor: Any,
        *,
        event_topics: Iterable[str] = DEFAULT_EXECUTION_EVENT_TOPICS,
        event_queue_capacity: int = DEFAULT_EXECUTION_EVENT_QUEUE_CAPACITY,
        worker_shutdown_wait_seconds: float = (
            DEFAULT_WORKER_SHUTDOWN_WAIT_SECONDS
        ),
        callback_time_budget_seconds: float = (
            DEFAULT_CALLBACK_TIME_BUDGET_SECONDS
        ),
        durable_ingress_deadline_seconds: float = (
            DEFAULT_PROJECTION_DURABLE_INGRESS_DEADLINE_SECONDS
        ),
        fatal_callback: Callable[[str], None] | None = None,
        degraded_callback: Callable[[str], None] | None = None,
        control_plane_session: Any = None,
        manage_control_plane_session: bool = True,
    ) -> None:
        _init_actor_base(self)
        if event_queue_capacity < 1:
            raise ValueError("event_queue_capacity must be positive")
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
            maxsize=int(event_queue_capacity)
        )
        self._worker_stop = Event()
        self._worker_started = Event()
        self._worker_thread: Thread | None = None
        self._worker_stop_deadline: float | None = None
        self._pending_publications: dict[
            int,
            _ProjectionPublication,
        ] = {}
        self._pending_lock = RLock()
        self._deadline_stop = Event()
        self._deadline_wake = Event()
        self._deadline_started = Event()
        self._deadline_thread: Thread | None = None
        self._flush_stop = Event()
        self._flush_wake = Event()
        self._flush_started = Event()
        self._flush_thread: Thread | None = None
        self._flush_stop_deadline: float | None = None
        self._halt_lock = RLock()
        self._halted_reason = ""
        self._degraded_reason = ""
        self._fatal_callback = fatal_callback
        self._degraded_callback = degraded_callback
        self._fatal_reported = False
        self._session_wake_pending = Event()
        self._control_plane_session = control_plane_session
        self._manage_control_plane_session = bool(
            manage_control_plane_session
        )
        self._session_started = False
        self._durable_ingress = callable(
            getattr(self._projection_actor, "ingest_event", None)
        )

    @property
    def halted_reason(self) -> str:
        return self._halted_reason

    @property
    def degraded_reason(self) -> str:
        return self._degraded_reason

    def on_start(self) -> None:
        if not self._durable_ingress:
            for topic in self._event_topics:
                self._subscribe_execution_topic(topic)
            self._flush_projection_once()
            return
        deadline = (
            time.monotonic() + self._worker_shutdown_wait_seconds
        )
        self._start_worker()
        if self._durable_ingress:
            self._start_deadline_worker()
        session = self._control_plane_session
        if session is not None:
            if self._manage_control_plane_session:
                session.start()
                self._session_started = True
        elif self._durable_ingress:
            self._start_flush_worker()
        self._await_worker_start(deadline)
        if self._durable_ingress:
            if session is not None:
                if not self._submit_flush_wake(False):
                    return
            else:
                self._flush_wake.set()
        else:
            self._enqueue_startup_flush()
        for topic in self._event_topics:
            self._subscribe_execution_topic(topic)

    def on_stop(self) -> None:
        if not self._durable_ingress:
            return
        deadline = (
            time.monotonic() + self._worker_shutdown_wait_seconds
        )
        self._worker_stop_deadline = deadline
        self._worker_stop.set()
        worker = self._worker_thread
        if worker is not None:
            worker.join(timeout=max(deadline - time.monotonic(), 0.0))
            if worker.is_alive():
                self._halt_egress(
                    "execution projection stopped with durable ingress pending"
                )
            else:
                self._worker_thread = None
        if (
            self._has_pending_publications()
            or not self._event_queue.empty()
            or self._session_wake_pending.is_set()
        ):
            self._halt_egress(
                "execution projection stopped with durable ingress pending"
            )
        self._stop_deadline_worker(deadline)

        session = self._control_plane_session
        if (
            session is not None
            and self._manage_control_plane_session
            and self._session_started
        ):
            try:
                stopped = bool(session.stop(deadline))
            except Exception as exc:
                stopped = False
                self._halt_egress(
                    "execution projection session stop failed: "
                    f"{exc!r}"
                )
            self._session_started = False
            if not stopped:
                self._halt_egress(
                    "execution projection session shutdown "
                    "deadline exceeded"
                )
        if session is None and self._durable_ingress:
            self._stop_flush_worker(deadline)

    def on_event(self, event: Any) -> Any:
        if not self._durable_ingress:
            self._attach_order_payload_fields(event)
            return self._projection_actor.on_event(event)
        if self._worker_stop.is_set() or self._halted_reason:
            return False
        worker = self._worker_thread
        if worker is None or not worker.is_alive():
            self._halt_egress(
                "execution projection persistence worker is not running"
            )
            return False
        deadline_at = None
        if self._durable_ingress:
            deadline_at = (
                time.monotonic()
                + self._durable_ingress_deadline_seconds
            )
        publication = _ProjectionPublication(
            event=event,
            deadline_at=deadline_at,
        )
        if self._durable_ingress:
            self._register_publication(publication)
        try:
            self._event_queue.put_nowait(publication)
        except Full:
            self._complete_publication(publication)
            self._halt_egress(
                "execution projection persistence queue capacity exceeded"
            )
            return False
        return True

    def session_flush_execution_event(self, event: Any) -> None:
        del event
        self._drain_durable_spool()

    def _enqueue_startup_flush(self) -> None:
        try:
            self._event_queue.put_nowait(
                _ProjectionPublication(flush_only=True)
            )
        except Full:
            self._halt_egress(
                "execution projection startup flush queue capacity exceeded"
            )

    def _start_worker(self) -> None:
        worker = self._worker_thread
        if worker is not None and worker.is_alive():
            return
        self._worker_stop.clear()
        self._worker_started.clear()
        worker = Thread(
            target=self._run_worker,
            name="execution-projection.ingress",
            daemon=True,
        )
        self._worker_thread = worker
        worker.start()

    def _run_worker(self) -> None:
        self._worker_started.set()
        while True:
            if self._worker_should_stop():
                return
            try:
                publication = self._event_queue.get(
                    timeout=self._worker_poll_timeout()
                )
            except Empty:
                if not self._retry_session_flush_wake():
                    return
                continue
            try:
                if publication.flush_only:
                    self._flush_projection_once()
                    continue
                if self._durable_ingress:
                    if not self._persist_durable_event(publication):
                        return
                    if self._control_plane_session is not None:
                        if not self._submit_flush_wake(publication.event):
                            return
                    else:
                        self._flush_wake.set()
                    continue
                self._project_legacy_event(publication.event)
            except Exception as exc:
                publication.error = exc
                self._halt_egress(
                    "execution projection persistence worker failed: "
                    f"{exc!r}"
                )
                return
            finally:
                publication.completed.set()
                self._complete_publication(publication)
                self._event_queue.task_done()

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
                "execution projection durable ingress failed: "
                f"{exc!r}"
            )
            return False
        outcome = self._projection_ingest_outcome(result)
        if outcome == "IGNORED":
            self._halt_egress(
                "execution projection ignored subscribed execution event"
            )
            return False
        if outcome not in {"DURABLE", "DEDUPED"}:
            self._halt_egress(
                "execution projection durable ingress returned "
                f"invalid outcome: {outcome or 'missing'}"
            )
            return False
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

    def _project_legacy_event(self, event: Any) -> None:
        self._attach_order_payload_fields(event)
        self._projection_actor.on_event(event)

    def _attach_order_payload_fields(self, event: Any) -> None:
        extra = order_event_payload_fields(event, self._cache())
        if not extra:
            return
        attach = getattr(
            self._projection_actor,
            "attach_order_payload_fields",
            None,
        )
        if callable(attach):
            try:
                attach(event, extra)
            except Exception:
                return
            return
        try:
            setattr(event, "_projection_payload_extra", extra)
        except Exception:
            return

    def _submit_flush_wake(self, event: Any) -> bool:
        session = self._control_plane_session
        if session is None:
            self._session_wake_pending.clear()
            self._clear_degraded()
            self._flush_wake.set()
            return True
        self._session_wake_pending.set()
        try:
            result = session.submit_execution_event(event)
        except Exception as exc:
            self._degrade_egress(
                "execution projection session wake failed: "
                f"{exc!r}"
            )
            return True
        if _submission_was_accepted(result):
            self._session_wake_pending.clear()
            self._clear_degraded()
            return True
        hard_failure = self._session_wake_hard_failure()
        if hard_failure:
            self._halt_egress(
                "execution projection session wake failed hard: "
                f"{hard_failure}"
            )
            return False
        self._degrade_egress(
            "execution projection session wake backpressured"
        )
        return True

    def _retry_session_flush_wake(self) -> bool:
        if not self._session_wake_pending.is_set():
            return True
        if self._worker_stop.is_set() or self._halted_reason:
            return True
        return self._submit_flush_wake(False)

    def _session_wake_hard_failure(self) -> str:
        session = self._control_plane_session
        snapshot = getattr(session, "snapshot", None)
        if not callable(snapshot):
            return ""
        try:
            health = snapshot()
        except Exception:
            return ""
        if bool(getattr(health, "stopped", False)):
            return "control-plane session is stopped"
        lanes = getattr(health, "lanes", {})
        return _lane_hard_failure(
            "execution_event",
            lanes.get("execution_event"),
        )

    def _start_deadline_worker(self) -> None:
        worker = self._deadline_thread
        if worker is not None and worker.is_alive():
            return
        self._deadline_stop.clear()
        self._deadline_wake.clear()
        self._deadline_started.clear()
        worker = Thread(
            target=self._run_deadline_worker,
            name="execution-projection.ingress-deadline",
            daemon=True,
        )
        self._deadline_thread = worker
        worker.start()

    def _run_deadline_worker(self) -> None:
        self._deadline_started.set()
        while not self._deadline_stop.is_set():
            publication = self._next_pending_publication()
            if publication is None:
                self._deadline_wake.wait(timeout=0.05)
                self._deadline_wake.clear()
                continue
            deadline_at = publication.deadline_at
            if deadline_at is None or publication.completed.is_set():
                self._complete_publication(publication)
                continue
            remaining = float(deadline_at) - time.monotonic()
            if remaining <= 0:
                self._halt_egress(
                    "execution projection durable ingress deadline exceeded"
                )
                return
            self._deadline_wake.wait(timeout=min(remaining, 0.05))
            self._deadline_wake.clear()

    def _stop_deadline_worker(self, deadline: float) -> None:
        worker = self._deadline_thread
        if worker is None:
            return
        self._deadline_stop.set()
        self._deadline_wake.set()
        worker.join(timeout=max(deadline - time.monotonic(), 0.0))
        if worker.is_alive():
            self._halt_egress(
                "execution projection deadline worker shutdown "
                "deadline exceeded"
            )
            return
        self._deadline_thread = None

    def _register_publication(
        self,
        publication: _ProjectionPublication,
    ) -> None:
        with self._pending_lock:
            self._pending_publications[id(publication)] = publication
        self._deadline_wake.set()

    def _complete_publication(
        self,
        publication: _ProjectionPublication,
    ) -> None:
        with self._pending_lock:
            self._pending_publications.pop(id(publication), None)
        self._deadline_wake.set()

    def _next_pending_publication(
        self,
    ) -> _ProjectionPublication | None:
        with self._pending_lock:
            publications = tuple(self._pending_publications.values())
        pending = [
            publication
            for publication in publications
            if not publication.completed.is_set()
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

    def _has_pending_publications(self) -> bool:
        with self._pending_lock:
            return bool(self._pending_publications)

    def _start_flush_worker(self) -> None:
        worker = self._flush_thread
        if worker is not None and worker.is_alive():
            return
        self._flush_stop.clear()
        self._flush_wake.clear()
        self._flush_started.clear()
        worker = Thread(
            target=self._run_flush_worker,
            name="execution-projection.flush",
            daemon=True,
        )
        self._flush_thread = worker
        worker.start()

    def _run_flush_worker(self) -> None:
        self._flush_started.set()
        while True:
            if self._flush_should_stop():
                return
            if not self._flush_wake.wait(
                timeout=self._flush_poll_timeout()
            ):
                continue
            self._flush_wake.clear()
            try:
                self._drain_durable_spool()
            except Exception as exc:
                self._halt_egress(
                    "execution projection flush worker failed: "
                    f"{exc!r}"
                )
                return

    def _stop_flush_worker(self, deadline: float) -> None:
        worker = self._flush_thread
        if worker is None:
            return
        self._flush_stop_deadline = deadline
        self._flush_stop.set()
        self._flush_wake.set()
        worker.join(timeout=max(deadline - time.monotonic(), 0.0))
        if worker.is_alive():
            self._halt_egress(
                "execution projection flush worker shutdown "
                "deadline exceeded"
            )
            return
        self._flush_thread = None
        pending_count = self._durable_pending_count()
        if pending_count is not None and pending_count > 0:
            self._halt_egress(
                "execution projection stopped with flush pending"
            )

    def _drain_durable_spool(self) -> None:
        while True:
            before = self._durable_pending_count()
            self._flush_projection_once()
            after = self._durable_pending_count()
            if before is None or after is None:
                return
            if after <= 0 or after >= before:
                return
            if self._flush_deadline_reached():
                return

    def _flush_projection_once(self) -> None:
        flush = getattr(self._projection_actor, "flush", None)
        if callable(flush):
            flush()

    def _durable_pending_count(self) -> int | None:
        spool = getattr(self._projection_actor, "spool", None)
        pending_count = getattr(spool, "pending_count", None)
        if pending_count is None:
            return None
        return int(pending_count)

    def _flush_should_stop(self) -> bool:
        if not self._flush_stop.is_set():
            return False
        pending_count = self._durable_pending_count()
        if pending_count is None or pending_count <= 0:
            return True
        return self._flush_deadline_reached()

    def _flush_deadline_reached(self) -> bool:
        deadline = self._flush_stop_deadline
        if deadline is None:
            return False
        return time.monotonic() >= deadline

    def _flush_poll_timeout(self) -> float:
        timeout = 0.05
        if not self._flush_stop.is_set():
            return timeout
        deadline = self._flush_stop_deadline
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

    def _await_worker_start(self, deadline: float) -> None:
        lanes = [
            ("persistence", self._worker_started, self._worker_thread),
        ]
        if self._durable_ingress:
            lanes.append(
                ("deadline", self._deadline_started, self._deadline_thread)
            )
            if self._control_plane_session is None:
                lanes.append(
                    ("flush", self._flush_started, self._flush_thread)
                )
        for name, started, worker in lanes:
            remaining = max(deadline - time.monotonic(), 0.0)
            if not started.wait(timeout=remaining):
                reason = (
                    f"execution projection {name} startup barrier timed out"
                )
                self._halt_egress(reason)
                raise RuntimeError(reason)
            if worker is None or not worker.is_alive():
                reason = (
                    f"execution projection {name} worker exited at startup"
                )
                self._halt_egress(reason)
                raise RuntimeError(reason)

    def _mark_sticky_halt(self, reason: str) -> None:
        halt_projection = False
        with self._halt_lock:
            if not self._halted_reason:
                self._halted_reason = reason
                self._degraded_reason = ""
                halt_projection = True
        if not halt_projection:
            return
        halt = getattr(self._projection_actor, "halt_egress", None)
        if callable(halt):
            try:
                halt(reason)
            except Exception:
                return

    def _degrade_egress(self, reason: str) -> None:
        callback = None
        with self._halt_lock:
            if self._halted_reason:
                return
            if self._degraded_reason == reason:
                return
            self._degraded_reason = reason
            callback = self._degraded_callback
        if callback is not None:
            callback(reason)

    def _clear_degraded(self) -> None:
        with self._halt_lock:
            self._degraded_reason = ""

    def _report_fatal(self) -> None:
        callback = None
        reason = ""
        with self._halt_lock:
            if self._fatal_reported:
                return
            self._fatal_reported = True
            callback = self._fatal_callback
            reason = self._halted_reason
        if callback is not None:
            callback(reason)

    def _halt_egress(self, reason: str) -> None:
        self._session_wake_pending.clear()
        self._mark_sticky_halt(reason)
        self._report_fatal()

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
            # TODO(host-verify): confirm Nautilus 1.227.0 MessageBus/Trader
            # subscribe signature for execution events in hk.
            try:
                subscribe(topic=topic, handler=self._on_bus_event)
                return
            except TypeError:
                pass
            try:
                subscribe(topic, self._on_bus_event)
                return
            except TypeError:
                pass
            subscribe(topic, callback=self._on_bus_event)
            return


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
        max_pending_commands: int = DEFAULT_PENDING_COMMAND_LIMIT,
        max_pending_acks: int = DEFAULT_PENDING_COMMAND_ACK_LIMIT,
        ack_batch_size: int = DEFAULT_COMMAND_ACK_BATCH_SIZE,
        command_apply_max_items: int = DEFAULT_CALLBACK_MAX_ITEMS,
        command_apply_time_budget_seconds: float = (
            DEFAULT_CALLBACK_TIME_BUDGET_SECONDS
        ),
        worker_shutdown_wait_seconds: float = (
            DEFAULT_WORKER_SHUTDOWN_WAIT_SECONDS
        ),
        control_plane_session: Any = None,
        manage_control_plane_session: bool = True,
        session_enqueue_timeout_seconds: float = 0.1,
        session_completion_timeout_seconds: float = (
            CONTROL_PLANE_STALE_AFTER_SECONDS
        ),
    ) -> None:
        _init_actor_base(self)
        if max_pending_commands < 1:
            raise ValueError("max_pending_commands must be positive")
        if max_pending_acks < 1:
            raise ValueError("max_pending_acks must be positive")
        if ack_batch_size < 1:
            raise ValueError("ack_batch_size must be positive")
        if command_apply_max_items < 1:
            raise ValueError("command_apply_max_items must be positive")
        if command_apply_time_budget_seconds <= 0:
            raise ValueError(
                "command_apply_time_budget_seconds must be positive"
            )
        if session_enqueue_timeout_seconds < 0:
            raise ValueError(
                "session_enqueue_timeout_seconds must be non-negative"
            )
        if session_completion_timeout_seconds <= 0:
            raise ValueError(
                "session_completion_timeout_seconds must be positive"
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
        self._stale_after_seconds = float(stale_after_seconds)
        self._max_pending_commands = int(max_pending_commands)
        self._max_pending_acks = int(max_pending_acks)
        self._ack_batch_size = min(
            int(ack_batch_size),
            self._max_pending_acks,
        )
        self._command_apply_max_items = int(command_apply_max_items)
        self._command_apply_time_budget_seconds = float(
            command_apply_time_budget_seconds
        )
        self._worker_shutdown_wait_seconds = max(
            float(worker_shutdown_wait_seconds),
            0.0,
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
        self._session_commands: Queue[_SessionCommandPublication] = Queue(
            maxsize=self._max_pending_commands
        )
        self._session_admission_lock = RLock()
        # Process-local until ACK succeeds. Restart durability belongs to the
        # command journal and remains outside this adapter batch.
        self._session_pending_acks: dict[str, _PendingCommandAck] = {}
        self._stopped = Event()
        self._heartbeat_executor: ThreadPoolExecutor | None = None
        self._command_executor: ThreadPoolExecutor | None = None
        self._ack_executor: ThreadPoolExecutor | None = None
        self._heartbeat_future: Future[None] | None = None
        self._command_future: Future[tuple[Any, ...]] | None = None
        self._ack_future: Future[tuple[str, ...]] | None = None
        self._pending_commands: tuple[Any, ...] = ()
        self._pending_command_index = 0
        self._pending_acks: dict[str, _PendingCommandAck] = {}
        self._started_at = time.monotonic()
        self._last_heartbeat_success_at: float | None = None
        self._last_command_success_at: float | None = None
        self._failed_dependencies: set[str] = set()
        self._failure_reason = ""
        self._degraded_reasons: dict[str, str] = {}

    @property
    def pending_command_count(self) -> int:
        legacy_pending = max(
            len(self._pending_commands) - self._pending_command_index,
            0,
        )
        return legacy_pending + self._session_commands.qsize()

    @property
    def failure_reason(self) -> str:
        return self._failure_reason

    @property
    def degraded_reason(self) -> str:
        return "; ".join(self._degraded_reasons.values())

    def on_start(self) -> None:
        self._started_at = time.monotonic()
        session = self._control_plane_session
        if session is not None:
            if self._manage_control_plane_session:
                session.start()
            self._register_poll_timer()
            return
        self._ensure_executors()
        self._register_poll_timer()

    def on_stop(self) -> None:
        with self._session_admission_lock:
            self._stopped.set()
            self._reject_session_commands(
                "command poller actor stopped before apply"
            )
        deadline = time.monotonic() + self._worker_shutdown_wait_seconds
        session = self._control_plane_session
        if session is not None and self._manage_control_plane_session:
            try:
                stopped = bool(session.stop(deadline))
            except Exception as exc:
                stopped = False
                reason = f"control-plane session stop failed: {exc!r}"
                self._mark_dependency_failed("control_plane", reason)
                self._fail_command_stream(reason)
            if not stopped:
                reason = (
                    "control-plane session failed to stop before deadline"
                )
                self._mark_dependency_failed("control_plane", reason)
                self._fail_command_stream(reason)
        lanes = (
            (
                "_heartbeat_executor",
                "_heartbeat_future",
                self._heartbeat_executor,
                self._heartbeat_future,
                "control_plane",
                "control-plane heartbeat worker failed to stop before deadline",
            ),
            (
                "_command_executor",
                "_command_future",
                self._command_executor,
                self._command_future,
                "command_stream",
                "operator command poll worker failed to stop before deadline",
            ),
            (
                "_ack_executor",
                "_ack_future",
                self._ack_executor,
                self._ack_future,
                "command_stream",
                "operator command ACK worker failed to stop before deadline",
            ),
        )
        for (
            executor_attr,
            future_attr,
            executor,
            future,
            dependency,
            reason,
        ) in lanes:
            worker_stopped = _shutdown_executor(
                executor,
                future,
                deadline=deadline,
            )
            if worker_stopped:
                setattr(self, executor_attr, None)
                setattr(self, future_attr, None)
                continue
            if dependency == "control_plane":
                self._mark_dependency_failed(dependency, reason)
                continue
            self._fail_command_stream(reason)

    def _register_poll_timer(self) -> None:
        clock = getattr(self, "clock", None)
        set_timer = getattr(clock, "set_timer", None) if clock is not None else None
        if not callable(set_timer):
            return
        interval = timedelta(seconds=self._poll_interval_seconds)
        try:
            set_timer(name=self._timer_name, interval=interval, callback=self._on_poll_timer)
            return
        except TypeError:
            pass
        set_timer(self._timer_name, interval, self._on_poll_timer)

    def _on_poll_timer(self, *_args: Any, **_kwargs: Any) -> None:
        if self._stopped.is_set():
            return
        if self._control_plane_session is not None:
            self._drain_session_commands()
            self._evaluate_session_health()
            return
        self._harvest_heartbeat()
        self._harvest_commands()
        self._drain_pending_commands()
        self._harvest_acks()
        self._submit_heartbeat()
        self._submit_commands()
        self._submit_acks()
        self._evaluate_poll_staleness()

    def _cache(self) -> Any:
        return _first_attr(self, ("cache", "_cache")) or _first_attr(
            self._lifecycle, ("cache", "_cache")
        )

    def poll_once(self) -> int:
        try:
            self.session_send_heartbeat()
        except Exception:
            pass
        commands = self._control_plane.poll_commands(self._node_id, None)
        for cmd in commands:
            status, error = self._apply(cmd)
            try:
                self._control_plane.ack_command(self._node_id, cmd.command_id, status, error=error)
            except Exception:  # ack failure must not crash the poll loop
                pass
        return len(commands)

    def session_send_heartbeat(self) -> None:
        self._ensure_open_orders_provider()
        self._lifecycle.send_heartbeat()
        self._last_heartbeat_success_at = time.monotonic()

    def session_poll_commands(
        self,
        capacity: int,
    ) -> tuple[Any, ...]:
        if capacity < 1:
            return ()
        commands = []
        for command in self._control_plane.poll_commands(
            self._node_id,
            None,
        ):
            if len(commands) >= capacity:
                reason = "operator command delivery capacity exceeded"
                self._fail_command_stream(reason)
                raise RuntimeError(reason)
            commands.append(command)
        self._last_command_success_at = time.monotonic()
        return tuple(commands)

    def session_apply_command(
        self,
        command: Any,
    ) -> _PendingCommandAck:
        command_id = str(command.command_id)
        publication = _SessionCommandPublication(command=command)
        with self._session_admission_lock:
            if self._stopped.is_set():
                raise RuntimeError("command poller actor is stopped")
            acknowledgement = self._session_pending_acks.get(command_id)
            if acknowledgement is not None:
                return acknowledgement
            try:
                self._session_commands.put(
                    publication,
                    timeout=self._session_enqueue_timeout_seconds,
                )
            except Full as exc:
                reason = "operator command actor mailbox capacity exceeded"
                self._fail_command_stream(reason)
                raise RuntimeError(reason) from exc

        deadline = (
            time.monotonic()
            + self._session_completion_timeout_seconds
        )
        while not publication.completed.wait(timeout=0.01):
            if self._stopped.is_set():
                publication.cancelled.set()
                raise RuntimeError(
                    "command poller actor stopped before apply"
                )
            if time.monotonic() >= deadline:
                reason = (
                    "operator command apply timed out waiting for actor thread"
                )
                publication.cancelled.set()
                self._fail_command_stream(reason)
                raise RuntimeError(reason)
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
        self._control_plane.ack_command(
            self._node_id,
            acknowledgement.command_id,
            acknowledgement.status,
            error=acknowledgement.error,
        )
        command_id = str(acknowledgement.command_id)
        with self._session_admission_lock:
            pending = self._session_pending_acks.get(command_id)
            if pending is acknowledgement:
                self._session_pending_acks.pop(command_id, None)

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
                if not publication.cancelled.is_set():
                    status, error = self._apply(publication.command)
                    acknowledgement = _PendingCommandAck(
                        command_id=publication.command.command_id,
                        status=status,
                        error=error,
                    )
                    publication.acknowledgement = acknowledgement
                    command_id = str(publication.command.command_id)
                    with self._session_admission_lock:
                        self._session_pending_acks[
                            command_id
                        ] = acknowledgement
            except Exception as exc:
                publication.error = exc
                self._fail_command_stream(
                    f"operator command apply failed: {exc!r}"
                )
            finally:
                publication.completed.set()
                self._session_commands.task_done()
            drained += 1
        return drained

    def _reject_session_commands(self, reason: str) -> None:
        while True:
            try:
                publication = self._session_commands.get_nowait()
            except Empty:
                return
            publication.cancelled.set()
            publication.error = RuntimeError(reason)
            publication.completed.set()
            self._session_commands.task_done()

    def _ensure_executors(
        self,
    ) -> tuple[
        ThreadPoolExecutor,
        ThreadPoolExecutor,
        ThreadPoolExecutor,
    ]:
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
            ack_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"{self._timer_name}.acks",
            )
            self._ack_executor = ack_executor
        return heartbeat_executor, command_executor, ack_executor

    def _submit_heartbeat(self) -> None:
        if self._stopped.is_set() or self._heartbeat_future is not None:
            return
        heartbeat_executor, _, _ = self._ensure_executors()
        self._heartbeat_future = heartbeat_executor.submit(
            self.session_send_heartbeat
        )

    def _harvest_heartbeat(self) -> None:
        future = self._heartbeat_future
        if future is None or not future.done():
            return
        self._heartbeat_future = None
        try:
            future.result()
        except Exception as exc:
            self._mark_dependency_degraded(
                "control_plane",
                f"control-plane heartbeat failed: {exc!r}",
            )
            return
        self._mark_dependency_ready("control_plane")

    def _submit_commands(self) -> None:
        if self._stopped.is_set() or self._command_future is not None:
            return
        if self.pending_command_count > 0:
            return
        _, command_executor, _ = self._ensure_executors()
        self._command_future = command_executor.submit(
            self.session_poll_commands,
            self._max_pending_commands,
        )

    def _harvest_commands(self) -> None:
        future = self._command_future
        if future is None or not future.done():
            return
        self._command_future = None
        try:
            commands = future.result()
        except Exception as exc:
            self._mark_dependency_degraded(
                "command_stream",
                f"operator command poll failed: {exc!r}"
            )
            return
        self._pending_commands = tuple(commands)
        self._pending_command_index = 0
        self._mark_dependency_ready("command_stream")

    def _drain_pending_commands(self) -> int:
        applied = 0
        deadline = (
            time.monotonic()
            + self._command_apply_time_budget_seconds
        )
        while self._pending_command_index < len(
            self._pending_commands
        ):
            if applied >= self._command_apply_max_items:
                break
            if applied > 0 and time.monotonic() >= deadline:
                break
            command = self._pending_commands[
                self._pending_command_index
            ]
            self._pending_command_index += 1
            status, error = self._apply(command)
            command_id = str(command.command_id)
            if len(self._pending_acks) >= self._max_pending_acks:
                self._fail_command_stream(
                    "operator command ACK backlog capacity exceeded"
                )
                break
            self._pending_acks[command_id] = _PendingCommandAck(
                command_id=command.command_id,
                status=status,
                error=error,
            )
            applied += 1
        if self._pending_command_index >= len(self._pending_commands):
            self._pending_commands = ()
            self._pending_command_index = 0
        return applied

    def _submit_acks(self) -> None:
        if self._stopped.is_set() or self._ack_future is not None:
            return
        if not self._pending_acks:
            return
        batch = tuple(self._pending_acks.values())[
            : self._ack_batch_size
        ]
        _, _, ack_executor = self._ensure_executors()
        self._ack_future = ack_executor.submit(
            self._run_ack_batch,
            batch,
        )

    def _run_ack_batch(
        self,
        batch: tuple[_PendingCommandAck, ...],
    ) -> tuple[str, ...]:
        acknowledged = []
        for item in batch:
            self.session_ack_command(item)
            acknowledged.append(str(item.command_id))
        return tuple(acknowledged)

    def _harvest_acks(self) -> None:
        future = self._ack_future
        if future is None or not future.done():
            return
        self._ack_future = None
        try:
            acknowledged = future.result()
        except Exception as exc:
            self._mark_dependency_degraded(
                "command_stream",
                f"operator command ACK failed: {exc!r}"
            )
            return
        for command_id in acknowledged:
            self._pending_acks.pop(command_id, None)

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
            self._fail_command_stream("operator command poll stale")

    def _evaluate_session_health(self) -> None:
        session = self._control_plane_session
        if session is None:
            return
        try:
            snapshot = session.snapshot()
        except Exception as exc:
            reason = f"control-plane session snapshot failed: {exc!r}"
            self._mark_dependency_degraded("control_plane", reason)
            self._mark_dependency_degraded("command_stream", reason)
            self._evaluate_poll_staleness()
            return
        lanes = getattr(snapshot, "lanes", {})
        heartbeat_lane = lanes.get("heartbeat")
        heartbeat_hard_failure = _lane_hard_failure(
            "heartbeat",
            heartbeat_lane,
        )
        heartbeat_degraded_failure = _lane_degraded_failure(
            "heartbeat",
            heartbeat_lane,
        )
        if heartbeat_hard_failure:
            self._mark_dependency_failed(
                "control_plane",
                heartbeat_hard_failure,
            )
        elif heartbeat_degraded_failure:
            self._mark_dependency_degraded(
                "control_plane",
                heartbeat_degraded_failure,
            )
        heartbeat_success = getattr(
            heartbeat_lane,
            "last_success_at",
            False,
        )
        if (
            not heartbeat_hard_failure
            and not heartbeat_degraded_failure
            and heartbeat_success is not False
        ):
            self._last_heartbeat_success_at = float(heartbeat_success)
            self._mark_dependency_ready("control_plane")

        command_lanes = (
            ("command_poll", lanes.get("command_poll")),
            ("command_delivery", lanes.get("command_delivery")),
            ("command_ack", lanes.get("command_ack")),
        )
        command_hard_failures = [
            _lane_hard_failure(lane_name, lane)
            for lane_name, lane in command_lanes
        ]
        command_hard_failures = [
            failure for failure in command_hard_failures if failure
        ]
        command_degraded_failures = [
            _lane_degraded_failure(lane_name, lane)
            for lane_name, lane in command_lanes
        ]
        command_degraded_failures = [
            failure for failure in command_degraded_failures if failure
        ]
        if command_hard_failures:
            self._fail_command_stream(command_hard_failures[0])
        elif command_degraded_failures:
            self._mark_dependency_degraded(
                "command_stream",
                command_degraded_failures[0],
            )
        command_lane = lanes.get("command_poll")
        command_success = getattr(
            command_lane,
            "last_success_at",
            False,
        )
        if (
            not command_hard_failures
            and not command_degraded_failures
            and command_success is not False
            and not self._failure_reason
        ):
            self._last_command_success_at = float(command_success)
            self._mark_dependency_ready("command_stream")
        self._evaluate_poll_staleness()

    def _ensure_open_orders_provider(self) -> None:
        if getattr(self, "_oo_provider_registered", False):
            return
        register = getattr(
            self._lifecycle,
            "set_open_orders_provider",
            None,
        )
        if not callable(register):
            return
        register(lambda: open_orders_snapshot(self._cache()))
        self._oo_provider_registered = True

    def _fail_command_stream(self, reason: str) -> None:
        self._failure_reason = reason
        self._mark_dependency_failed("command_stream", reason)

    def _mark_dependency_ready(self, dependency_value: str) -> None:
        dependency = _dependency_by_value(dependency_value)
        marker = getattr(self._lifecycle, "mark_dependency_ready", None)
        if dependency is not None and callable(marker):
            marker(dependency)
        self._failed_dependencies.discard(dependency_value)
        self._degraded_reasons.pop(dependency_value, None)

    def _mark_dependency_degraded(
        self,
        dependency_value: str,
        reason: str,
    ) -> None:
        if dependency_value in self._failed_dependencies:
            return
        self._degraded_reasons[dependency_value] = reason
        dependency = _dependency_by_value(dependency_value)
        marker = getattr(
            self._lifecycle,
            "mark_dependency_degraded",
            None,
        )
        if dependency is not None and callable(marker):
            marker(dependency, reason)

    def _mark_dependency_failed(
        self,
        dependency_value: str,
        reason: str,
    ) -> None:
        if dependency_value in self._failed_dependencies:
            return
        dependency = _dependency_by_value(dependency_value)
        marker = getattr(self._lifecycle, "mark_dependency_failed", None)
        if dependency is not None and callable(marker):
            marker(dependency, reason)
        self._failed_dependencies.add(dependency_value)
        self._degraded_reasons.pop(dependency_value, None)

    def _apply(self, cmd: Any):
        from execution_domain.control_plane import (  # type: ignore
            CommandAckStatus,
            CommandType,
            TradingState,
        )

        try:
            if cmd.type == CommandType.HALT:
                self._lifecycle.apply_operator_state(TradingState.HALTED, "operator_command")
            elif cmd.type == CommandType.RESUME:
                self._lifecycle.apply_operator_state(TradingState.ACTIVE, "operator_command")
            elif cmd.type == CommandType.SET_REDUCING:
                self._lifecycle.apply_operator_state(TradingState.REDUCING, "operator_command")
            elif cmd.type in (CommandType.CANCEL_ALL, CommandType.CLOSE_ALL):
                if not _node_command_has_authorization(cmd):
                    return CommandAckStatus.FAILED, "authorization_source_required"
                command_account_id = _node_command_account_id(cmd)
                if not command_account_id:
                    return CommandAckStatus.FAILED, "command_account_required"
                if command_account_id != self._account_id:
                    return CommandAckStatus.FAILED, "command_account_mismatch"
                # Only a Strategy may submit/cancel/close on Nautilus, so route the
                # action to the IntentExecutionStrategy over the msgbus. Returns
                # ACCEPTED (received + dispatched); the strategy executes best-effort.
                message_bus = _first_attr(self, ("msgbus", "message_bus", "_msgbus"))
                if message_bus is not None and hasattr(message_bus, "publish"):
                    message_bus.publish(topic=f"node.commands.{self._account_id}", msg=cmd)
                    return CommandAckStatus.ACCEPTED, "dispatched_to_strategy"
                return CommandAckStatus.FAILED, "no_msgbus_for_dispatch"
            else:
                return CommandAckStatus.ACCEPTED, "node_action_not_wired"
            return CommandAckStatus.COMPLETED, None
        except Exception as exc:  # e.g. readiness gate on RESUME
            return CommandAckStatus.FAILED, repr(exc)


def _init_actor_base(instance: Actor) -> None:
    try:
        Actor.__init__(instance)
    except TypeError:
        Actor.__init__(instance, config=None)


def _first_attr(source: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def _lane_hard_failure(lane_name: str, lane: Any) -> str:
    if lane is None:
        return ""
    fatal_failure = str(
        getattr(lane, "fatal_failure", "") or ""
    ).strip()
    if fatal_failure:
        return fatal_failure
    queue_pressure = str(
        getattr(lane, "queue_pressure", "") or ""
    ).strip().lower()
    if queue_pressure != "full":
        return ""
    failure = str(getattr(lane, "failure", "") or "").strip()
    if failure:
        return failure
    return f"{lane_name} queue capacity exceeded"


def _lane_degraded_failure(lane_name: str, lane: Any) -> str:
    if lane is None or _lane_hard_failure(lane_name, lane):
        return ""
    failure = str(getattr(lane, "failure", "") or "").strip()
    if failure:
        return failure
    circuit_state = str(
        getattr(lane, "circuit_state", "") or ""
    ).strip().lower()
    if circuit_state and circuit_state != "closed":
        return f"{lane_name} circuit state is {circuit_state}"
    queue_pressure = str(
        getattr(lane, "queue_pressure", "") or ""
    ).strip().lower()
    if queue_pressure == "degraded":
        return f"{lane_name} queue pressure is degraded"
    return ""


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


def _submission_was_accepted(result: Any) -> bool:
    value = getattr(result, "value", result)
    return str(value or "").strip().lower() == "accepted"


def _dependency_by_value(value: str) -> Any:
    try:
        from runtime.lifecycle import DependencyName  # type: ignore
    except ImportError:
        return None
    for dependency in DependencyName:
        if dependency.value == value:
            return dependency
    return None


def _node_command_has_authorization(cmd: Any) -> bool:
    args = getattr(cmd, "args", {})
    if not isinstance(args, dict):
        return False
    authorization = args.get("authorization")
    if not isinstance(authorization, dict):
        return False
    authorized_by_type = str(
        authorization.get("authorized_by_type") or ""
    ).strip()
    authorized_by_id = str(
        authorization.get("authorized_by_id") or ""
    ).strip()
    source_message_id = str(
        authorization.get("source_message_id") or ""
    ).strip()
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
