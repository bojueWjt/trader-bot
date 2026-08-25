"""Account-scoped Binance cancel adapter and exchange-state mirror.

The adapter is intentionally narrow: only an explicit single-order cancel intent
may call it. It never submits, modifies, or scans-and-cancels orders.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import math
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from queue import Empty, Full, Queue
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from uuid import UUID

from execution_domain.order_ownership import (
    object_client_order_id,
    object_is_robot_order,
)

REGULAR_ORDER = "regular"
ALGO_ORDER = "algo"
_CANCEL_ACTIONS = frozenset({"cancel", "cancel_order"})
_CANCELED_STATUSES = frozenset({"CANCELED", "CANCELLED"})
_FILLED_STATUSES = frozenset({"FILLED", "EXECUTED", "TRIGGERED"})
_ABSENT_ORDER_CODES = frozenset({-2011, -2013})
DEFAULT_RECV_WINDOW_MS = 30_000
MAX_RECV_WINDOW_MS = 60_000
CONTROL_PLANE_FRESHNESS_BUDGET_SECONDS = 5.0
DEFAULT_EVIDENCE_TOTAL_DEADLINE_SECONDS = 4.0
DEFAULT_EVIDENCE_BACKOFF_BASE_SECONDS = 0.5
DEFAULT_EVIDENCE_BACKOFF_MAX_SECONDS = 4.0
DEFAULT_EVIDENCE_CIRCUIT_FAILURES = 3
DEFAULT_TERMINAL_EXCHANGE_QUEUE_CAPACITY = 64
DEFAULT_TERMINAL_EXCHANGE_DEGRADED_RATIO = 0.8
DEFAULT_TERMINAL_EXCHANGE_DEADLINE_SECONDS = 6.0
_TERMINAL_EXCHANGE_OPERATIONS = frozenset(
    {"refresh", "cancel_batch", "terminal_command"}
)
_PROTECTIVE_ORDER_TYPES = frozenset(
    {
        "STOP",
        "STOP_MARKET",
        "STOP_LOSS",
        "STOP_LOSS_LIMIT",
        "TAKE_PROFIT",
        "TAKE_PROFIT_MARKET",
        "TAKE_PROFIT_LIMIT",
        "TRAILING_STOP_MARKET",
    }
)


class ExchangeCancelError(RuntimeError):
    """Base class for explicit cancel failures."""


class CancelIntentRequiredError(ExchangeCancelError):
    pass


class WrongAccountError(ExchangeCancelError):
    pass


class CancelConfirmationTimeoutError(ExchangeCancelError):
    pass


class CancelStateError(ExchangeCancelError):
    pass


class OrderAlreadyFilledError(CancelStateError):
    pass


class BinanceApiError(ExchangeCancelError):
    def __init__(
        self,
        code: int,
        message: str,
        *,
        http_status: int | None = None,
        headers: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = int(code)
        self.message = str(message)
        self.http_status = int(http_status) if http_status is not None else None
        raw_headers = headers or {}
        self.headers = {
            str(key).strip().lower(): str(value).strip()
            for key, value in raw_headers.items()
        }
        super().__init__(f"Binance API {self.code}: {self.message}")


class ExchangeTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class CancelOrderRequest:
    account_id: str
    symbol: str
    position_side: str
    order_kind: str
    venue_order_id: str | None = None
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        if self.order_kind not in {REGULAR_ORDER, ALGO_ORDER}:
            raise ValueError(f"unsupported order_kind: {self.order_kind}")
        if not self.symbol:
            raise ValueError("symbol is required")
        if not self.position_side:
            raise ValueError("position_side is required")
        if self.position_side.upper() not in {"BOTH", "LONG", "SHORT"}:
            raise ValueError(f"unsupported position_side: {self.position_side}")
        if self.order_kind == ALGO_ORDER and not self.venue_order_id:
            raise ValueError("algo cancellation requires venue_order_id")
        if not self.venue_order_id and not self.client_order_id:
            raise ValueError("venue_order_id or client_order_id is required")


@dataclass(frozen=True)
class CancelResult:
    account_id: str
    symbol: str
    position_side: str
    order_kind: str
    outcome: str
    terminal_status: str


@dataclass(frozen=True)
class ExchangeOrderRef:
    account_id: str
    symbol: str
    position_side: str
    order_kind: str
    venue_order_id: str
    client_order_id: str
    order_type: str
    side: str
    quantity: str
    price: str | None
    trigger_price: str | None
    tags: tuple[str, ...]
    time_in_force: str = ""
    reduce_only: bool = False

    @property
    def instrument_id(self) -> str:
        return f"{self.symbol}-PERP.BINANCE"


@dataclass(frozen=True)
class DurableEntryOrderPreservation:
    client_order_id: str
    instrument_id: str
    side: str
    quantity: str
    price: str

    def __post_init__(self) -> None:
        client_order_id = str(self.client_order_id).strip()
        match = re.fullmatch(
            r"B[0-9a-f]{32}(?P<sequence>[0-9]{2})",
            client_order_id,
        )
        if match is None:
            raise ValueError(
                "durable entry preservation client_order_id is invalid"
            )
        sequence = int(match.group("sequence"))
        if sequence < 1 or sequence > 9:
            raise ValueError(
                "durable entry preservation sequence must be 01..09"
            )
        if not _canonical_exchange_symbol(self.instrument_id):
            raise ValueError(
                "durable entry preservation instrument_id is required"
            )
        side = _exchange_order_side(self.side)
        if side not in {"BUY", "SELL"}:
            raise ValueError(
                "durable entry preservation side is invalid"
            )
        for field_name in ("quantity", "price"):
            value = getattr(self, field_name)
            if _positive_exchange_decimal(value) is False:
                raise ValueError(
                    f"durable entry preservation {field_name} is invalid"
                )


@dataclass(frozen=True)
class TerminalExchangeRequest:
    request_id: str
    account_id: str
    operation: str
    purpose: str
    deadline_monotonic: float
    instrument_ids: tuple[str, ...] = ()
    cancel_requests: tuple[CancelOrderRequest, ...] = ()
    durable_entry_preservations: tuple[
        DurableEntryOrderPreservation,
        ...,
    ] = ()

    def __post_init__(self) -> None:
        if not str(self.request_id).strip():
            raise ValueError("terminal exchange request_id is required")
        if not str(self.account_id).strip():
            raise ValueError("terminal exchange account_id is required")
        if self.operation not in _TERMINAL_EXCHANGE_OPERATIONS:
            raise ValueError(
                f"unsupported terminal exchange operation: {self.operation}"
            )
        if not str(self.purpose).strip():
            raise ValueError("terminal exchange purpose is required")
        deadline = float(self.deadline_monotonic)
        if not math.isfinite(deadline) or deadline <= 0:
            raise ValueError(
                "terminal exchange deadline_monotonic must be positive"
            )
        if not isinstance(self.instrument_ids, tuple):
            raise TypeError(
                "terminal exchange instrument_ids must be a tuple"
            )
        if not isinstance(self.cancel_requests, tuple):
            raise TypeError(
                "terminal exchange cancel_requests must be a tuple"
            )
        if not isinstance(self.durable_entry_preservations, tuple):
            raise TypeError(
                "terminal exchange durable_entry_preservations must be a tuple"
            )
        if self.operation != "cancel_batch" and self.cancel_requests:
            raise ValueError(
                "cancel_requests require cancel_batch operation"
            )
        if (
            self.operation != "terminal_command"
            and self.durable_entry_preservations
        ):
            raise ValueError(
                "durable_entry_preservations require terminal_command operation"
            )
        if any(
            not isinstance(item, DurableEntryOrderPreservation)
            for item in self.durable_entry_preservations
        ):
            raise TypeError(
                "terminal exchange durable_entry_preservations are invalid"
            )
        preservation_ids = tuple(
            item.client_order_id
            for item in self.durable_entry_preservations
        )
        if len(set(preservation_ids)) != len(preservation_ids):
            raise ValueError(
                "durable_entry_preservations must be unique"
            )


@dataclass(frozen=True)
class TerminalExchangeCancelOutcome:
    request: CancelOrderRequest
    status: str
    outcome: str = ""
    terminal_status: str = ""
    error: str = ""


@dataclass(frozen=True)
class TerminalExchangeResult:
    request_id: str
    account_id: str
    operation: str
    purpose: str
    cancel_outcomes: tuple[TerminalExchangeCancelOutcome, ...]
    error: str
    timed_out: bool
    started_monotonic: float
    completed_monotonic: float


@dataclass(frozen=True)
class TerminalExchangeWorkerSnapshot:
    name: str
    running: bool
    in_flight: bool
    queue_depth: int
    queue_capacity: int
    pressure_ratio: float
    degraded: bool
    halted: bool
    accepted: int
    completed: int
    failed: int
    timed_out: int
    rejected: int
    last_progress_monotonic: float
    last_error: str


class TerminalExchangeDeadlineError(ExchangeCancelError):
    pass


class TerminalExchangeWorker:
    """Single-account bounded lane for mirror refresh and Binance cancellation."""

    def __init__(
        self,
        *,
        account_id: str,
        mirror: Any,
        adapter: Any,
        result_publisher: Callable[[TerminalExchangeResult], None],
        capacity: int = DEFAULT_TERMINAL_EXCHANGE_QUEUE_CAPACITY,
        degraded_ratio: float = DEFAULT_TERMINAL_EXCHANGE_DEGRADED_RATIO,
        total_deadline_seconds: float = DEFAULT_TERMINAL_EXCHANGE_DEADLINE_SECONDS,
        on_degraded: Callable[[str], None] | None = None,
        on_halt: Callable[[str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        normalized_account_id = str(account_id).strip()
        if not normalized_account_id:
            raise ValueError("terminal exchange account_id is required")
        if capacity < 1:
            raise ValueError(
                "terminal exchange queue capacity must be positive"
            )
        if degraded_ratio <= 0 or degraded_ratio > 1:
            raise ValueError(
                "terminal exchange degraded_ratio must be in (0, 1]"
            )
        if total_deadline_seconds <= 0:
            raise ValueError(
                "terminal exchange total deadline must be positive"
            )
        self._account_id = normalized_account_id
        self._mirror = mirror
        self._adapter = adapter
        self._result_publisher = result_publisher
        self._capacity = int(capacity)
        self._degraded_ratio = float(degraded_ratio)
        self._total_deadline_seconds = float(total_deadline_seconds)
        self._on_degraded = on_degraded
        self._on_halt = on_halt
        self._monotonic = monotonic
        self._name = f"{normalized_account_id}.terminal-exchange"
        self._queue: Queue[TerminalExchangeRequest] = Queue(
            maxsize=self._capacity
        )
        self._stop_requested = threading.Event()
        self._in_flight = threading.Event()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._active_ids: set[str] = set()
        self._recent_ids: deque[str] = deque()
        self._recent_results: dict[
            str,
            TerminalExchangeResult,
        ] = {}
        self._recent_id_limit = max(self._capacity * 4, 64)
        self._degraded = False
        self._halted = False
        self._accepted = 0
        self._completed = 0
        self._failed = 0
        self._timed_out = 0
        self._rejected = 0
        self._last_progress_monotonic = self._monotonic()
        self._last_error = ""

    def start(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        if self._stop_requested.is_set():
            raise RuntimeError(
                "terminal exchange worker cannot restart after stop"
            )
        thread = threading.Thread(
            target=self._run,
            name=self._name,
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def new_deadline(self) -> float:
        return self._monotonic() + self._total_deadline_seconds

    def submit(self, request: TerminalExchangeRequest) -> bool:
        if request.account_id != self._account_id:
            raise WrongAccountError(
                f"terminal exchange worker {self._account_id!r} "
                f"cannot accept {request.account_id!r}"
            )
        degraded_reason = ""
        halt_reason = ""
        cached_result: TerminalExchangeResult | None = None
        with self._state_lock:
            if self._halted:
                self._rejected += 1
                return False
            if self._stop_requested.is_set():
                self._rejected += 1
                return False
            if request.request_id in self._active_ids:
                return True
            cached_result = self._recent_results.get(
                request.request_id
            )
            if cached_result is not None:
                pass
            else:
                try:
                    self._queue.put_nowait(request)
                except Full:
                    self._rejected += 1
                    halt_reason = (
                        f"{self._name} queue capacity exceeded"
                    )
                    self._halted = True
                    self._last_error = halt_reason
                else:
                    self._active_ids.add(request.request_id)
                    self._accepted += 1
                    pressure_ratio = (
                        self._queue.qsize() / self._capacity
                    )
                    if (
                        pressure_ratio >= self._degraded_ratio
                        and not self._degraded
                    ):
                        self._degraded = True
                        degraded_reason = (
                            f"{self._name} queue pressure "
                            f"{pressure_ratio:.3f}"
                        )
        if cached_result is not None:
            return self._publish_result(cached_result)
        if degraded_reason:
            self._notify(self._on_degraded, degraded_reason)
        if halt_reason:
            self._notify(self._on_halt, halt_reason)
            return False
        return True

    def wait_empty(self, *, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = self._monotonic() + timeout_seconds
        while self._monotonic() <= deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.001)
        return self._queue.unfinished_tasks == 0

    def stop(self, *, timeout_seconds: float = 7.0) -> None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        self._stop_requested.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout_seconds)
        if thread.is_alive():
            reason = (
                f"{self._name} shutdown drain exceeded "
                f"{timeout_seconds:.3f}s"
            )
            self._sticky_halt(reason)
            raise RuntimeError(reason)
        self._thread = None

    def snapshot(self) -> TerminalExchangeWorkerSnapshot:
        thread = self._thread
        running = thread is not None and thread.is_alive()
        with self._state_lock:
            queue_depth = self._queue.qsize()
            pressure_ratio = queue_depth / self._capacity
            return TerminalExchangeWorkerSnapshot(
                name=self._name,
                running=running,
                in_flight=self._in_flight.is_set(),
                queue_depth=queue_depth,
                queue_capacity=self._capacity,
                pressure_ratio=pressure_ratio,
                degraded=self._degraded,
                halted=self._halted,
                accepted=self._accepted,
                completed=self._completed,
                failed=self._failed,
                timed_out=self._timed_out,
                rejected=self._rejected,
                last_progress_monotonic=self._last_progress_monotonic,
                last_error=self._last_error,
            )

    def _run(self) -> None:
        while True:
            if (
                self._stop_requested.is_set()
                and self._queue.empty()
            ):
                return
            try:
                request = self._queue.get(timeout=0.05)
            except Empty:
                continue
            self._clear_degraded_if_recovered()
            self._in_flight.set()
            result: TerminalExchangeResult | None = None
            try:
                result = self._execute(request)
                self._publish_result(result)
            finally:
                if result is not None:
                    self._remember_completed_request(result)
                self._in_flight.clear()
                self._queue.task_done()

    def _execute(
        self,
        request: TerminalExchangeRequest,
    ) -> TerminalExchangeResult:
        started = self._monotonic()
        outcomes: tuple[TerminalExchangeCancelOutcome, ...] = ()
        error = ""
        timed_out = False
        try:
            self._check_deadline(request.deadline_monotonic)
            if request.operation == "refresh":
                self._refresh(request.deadline_monotonic)
            elif request.operation == "cancel_batch":
                outcomes = self._cancel_batch(
                    request.cancel_requests,
                    request.deadline_monotonic,
                )
            else:
                outcomes = self._execute_terminal_command(request)
            self._check_deadline(request.deadline_monotonic)
        except TerminalExchangeDeadlineError as exc:
            timed_out = True
            error = str(exc)
        except Exception as exc:
            error = repr(exc)
        completed = self._monotonic()
        with self._state_lock:
            self._completed += 1
            self._last_progress_monotonic = completed
            if timed_out:
                self._timed_out += 1
            if error:
                self._failed += 1
                self._last_error = error
        if timed_out:
            self._sticky_halt(error)
        return TerminalExchangeResult(
            request_id=request.request_id,
            account_id=request.account_id,
            operation=request.operation,
            purpose=request.purpose,
            cancel_outcomes=outcomes,
            error=error,
            timed_out=timed_out,
            started_monotonic=started,
            completed_monotonic=completed,
        )

    def _execute_terminal_command(
        self,
        request: TerminalExchangeRequest,
    ) -> tuple[TerminalExchangeCancelOutcome, ...]:
        orders = self._refresh(request.deadline_monotonic)
        durable_entry_preservations = {
            item.client_order_id: item
            for item in request.durable_entry_preservations
        }
        preserve_protection = request.purpose == "cancel_all"
        outcomes: list[TerminalExchangeCancelOutcome] = []
        cancel_requests: list[CancelOrderRequest] = []
        for order in orders:
            instrument_id = str(
                getattr(order, "instrument_id", "") or ""
            )
            if not _terminal_exchange_instrument_matches(
                instrument_id,
                request.instrument_ids,
            ):
                continue
            if not object_is_robot_order(order):
                continue
            cancel_request = _cancel_request_from_exchange_order(order)
            client_order_id = object_client_order_id(order)
            preservation_outcome = _terminal_order_preservation_outcome(
                order,
                durable_entry_preservation=(
                    durable_entry_preservations.get(client_order_id)
                ),
                preserve_protection=preserve_protection,
            )
            if preservation_outcome:
                outcomes.append(
                    TerminalExchangeCancelOutcome(
                        request=cancel_request,
                        status="preserved",
                        outcome=preservation_outcome,
                        terminal_status="WORKING",
                    )
                )
                continue
            cancel_requests.append(cancel_request)
        outcomes.extend(
            self._cancel_batch(
                tuple(cancel_requests),
                request.deadline_monotonic,
            )
        )
        return tuple(outcomes)

    def _refresh(self, deadline_monotonic: float) -> tuple[Any, ...]:
        self._check_deadline(deadline_monotonic)
        refresh = getattr(self._mirror, "refresh", None)
        if not callable(refresh):
            raise ExchangeCancelError(
                "exchange state mirror refresh is unavailable"
            )
        try:
            result = refresh(
                deadline_monotonic=deadline_monotonic
            )
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            result = refresh()
        return tuple(result or ())

    def _cancel_batch(
        self,
        requests: tuple[CancelOrderRequest, ...],
        deadline_monotonic: float,
    ) -> tuple[TerminalExchangeCancelOutcome, ...]:
        outcomes: list[TerminalExchangeCancelOutcome] = []
        for request in requests:
            self._check_deadline(deadline_monotonic)
            try:
                result = self._cancel(request, deadline_monotonic)
            except TerminalExchangeDeadlineError:
                raise
            except Exception as exc:
                outcomes.append(
                    TerminalExchangeCancelOutcome(
                        request=request,
                        status="failed",
                        error=repr(exc),
                    )
                )
                continue
            terminal_status = str(
                getattr(result, "terminal_status", "") or ""
            ).upper()
            status = "confirmed"
            error = ""
            if terminal_status not in _CANCELED_STATUSES:
                status = "failed"
                error = (
                    "order cancel terminal status "
                    f"{terminal_status or 'UNKNOWN'}"
                )
            outcomes.append(
                TerminalExchangeCancelOutcome(
                    request=request,
                    status=status,
                    outcome=str(
                        getattr(result, "outcome", "") or ""
                    ),
                    terminal_status=terminal_status,
                    error=error,
                )
            )
        return tuple(outcomes)

    def _cancel(
        self,
        request: CancelOrderRequest,
        deadline_monotonic: float,
    ) -> Any:
        cancel = getattr(self._adapter, "cancel", None)
        if not callable(cancel):
            raise ExchangeCancelError(
                "exchange cancel adapter is unavailable"
            )
        try:
            return cancel(
                "cancel_order",
                request,
                deadline_monotonic=deadline_monotonic,
            )
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            return cancel("cancel_order", request)

    def _check_deadline(self, deadline_monotonic: float) -> None:
        if self._monotonic() < deadline_monotonic:
            return
        raise TerminalExchangeDeadlineError(
            f"{self._name} total deadline exceeded"
        )

    def _sticky_halt(self, reason: str) -> None:
        notify = False
        with self._state_lock:
            if not self._halted:
                notify = True
            self._halted = True
            self._last_error = str(reason)
        if notify:
            self._notify(self._on_halt, str(reason))

    def _clear_degraded_if_recovered(self) -> None:
        with self._state_lock:
            pressure_ratio = self._queue.qsize() / self._capacity
            if pressure_ratio < self._degraded_ratio:
                self._degraded = False

    def _remember_completed_request(
        self,
        result: TerminalExchangeResult,
    ) -> None:
        request_id = result.request_id
        with self._state_lock:
            self._active_ids.discard(request_id)
            if request_id in self._recent_results:
                return
            self._recent_ids.append(request_id)
            self._recent_results[request_id] = result
            while len(self._recent_ids) > self._recent_id_limit:
                expired = self._recent_ids.popleft()
                self._recent_results.pop(expired, None)

    def _publish_result(
        self,
        result: TerminalExchangeResult,
    ) -> bool:
        try:
            self._result_publisher(result)
        except Exception as exc:
            self._sticky_halt(
                f"{self._name} result mailbox failed: {exc!r}"
            )
            return False
        return True

    @staticmethod
    def _notify(
        callback: Callable[[str], None] | None,
        reason: str,
    ) -> None:
        if callback is None:
            return
        callback(reason)


class BinanceExchangeCancelAdapter:
    def __init__(
        self,
        *,
        account_id: str,
        transport: ExchangeTransport,
        confirmation_timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if confirmation_timeout_seconds <= 0:
            raise ValueError("confirmation_timeout_seconds must be positive")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        self._account_id = account_id
        self._transport = transport
        self._confirmation_timeout_seconds = confirmation_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    def cancel(
        self,
        intent_action: str,
        request: CancelOrderRequest,
        *,
        deadline_monotonic: float | None = None,
    ) -> CancelResult:
        action = str(intent_action).strip().lower()
        if action not in _CANCEL_ACTIONS:
            raise CancelIntentRequiredError(
                f"exchange cancel adapter requires cancel intent, got {intent_action!r}"
            )
        if request.account_id != self._account_id:
            raise WrongAccountError(
                f"adapter account {self._account_id!r} cannot cancel for {request.account_id!r}"
            )

        operation_deadline = deadline_monotonic
        if deadline_monotonic is not None:
            confirmation_deadline = (
                self._monotonic()
                + self._confirmation_timeout_seconds
            )
            operation_deadline = min(
                float(deadline_monotonic),
                confirmation_deadline,
            )
        delete_succeeded = False
        try:
            self._transport.request(
                "DELETE",
                self._cancel_path(request.order_kind),
                self._identity_params(request),
                timeout_seconds=self._remaining_timeout(
                    operation_deadline
                ),
            )
            delete_succeeded = True
        except BinanceApiError as exc:
            if exc.code not in _ABSENT_ORDER_CODES:
                raise

        self._wait_until_absent(
            request,
            deadline_monotonic=operation_deadline,
        )
        status = self._read_terminal_status(
            request,
            deadline_monotonic=operation_deadline,
        )
        if status in _FILLED_STATUSES:
            raise OrderAlreadyFilledError(
                f"{request.order_kind} order was already {status}: "
                f"account={request.account_id} symbol={request.symbol} "
                f"position_side={request.position_side}"
            )
        if status in _CANCELED_STATUSES:
            outcome = "already_canceled"
            if delete_succeeded:
                outcome = "canceled"
            return CancelResult(
                account_id=request.account_id,
                symbol=request.symbol,
                position_side=request.position_side,
                order_kind=request.order_kind,
                outcome=outcome,
                terminal_status=status,
            )
        raise CancelStateError(
            f"order disappeared from open endpoint with terminal status {status}: "
            f"account={request.account_id} symbol={request.symbol} "
            f"position_side={request.position_side}"
        )

    def _wait_until_absent(
        self,
        request: CancelOrderRequest,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        deadline = deadline_monotonic
        if deadline is None:
            deadline = (
                self._monotonic()
                + self._confirmation_timeout_seconds
            )
        while True:
            rows = self._open_orders(
                request,
                deadline_monotonic=deadline_monotonic,
            )
            if not any(self._matches(row, request) for row in rows):
                return
            if self._monotonic() >= deadline:
                raise CancelConfirmationTimeoutError(
                    f"order remained open after cancel timeout: "
                    f"account={request.account_id} symbol={request.symbol} "
                    f"position_side={request.position_side} kind={request.order_kind}"
                )
            self._sleep(self._poll_interval_seconds)

    def _open_orders(
        self,
        request: CancelOrderRequest,
        *,
        deadline_monotonic: float | None = None,
    ) -> list[Mapping[str, Any]]:
        path = "/fapi/v1/openOrders"
        params: dict[str, Any] = {"symbol": request.symbol}
        if request.order_kind == ALGO_ORDER:
            path = "/fapi/v1/openAlgoOrders"
            params = {}
        payload = self._transport.request(
            "GET",
            path,
            params,
            timeout_seconds=self._remaining_timeout(
                deadline_monotonic
            ),
        )
        rows: Any = payload
        if isinstance(payload, Mapping):
            rows = payload.get("orders", [])
        if not isinstance(rows, list):
            raise CancelStateError(f"{path} returned invalid order collection")
        return [row for row in rows if isinstance(row, Mapping)]

    def _read_terminal_status(
        self,
        request: CancelOrderRequest,
        *,
        deadline_monotonic: float | None = None,
    ) -> str:
        path = "/fapi/v1/order"
        if request.order_kind == ALGO_ORDER:
            path = "/fapi/v1/algoOrder"
        try:
            payload = self._transport.request(
                "GET",
                path,
                self._identity_params(request),
                timeout_seconds=self._remaining_timeout(
                    deadline_monotonic
                ),
            )
        except BinanceApiError as exc:
            if exc.code in _ABSENT_ORDER_CODES:
                return "UNKNOWN"
            raise
        if not isinstance(payload, Mapping):
            return "UNKNOWN"
        raw_status = payload.get("algoStatus")
        if raw_status is None:
            raw_status = payload.get("status")
        if raw_status is None:
            return "UNKNOWN"
        return str(raw_status).upper()

    def _remaining_timeout(
        self,
        deadline_monotonic: float | None,
    ) -> float | None:
        if deadline_monotonic is None:
            return None
        remaining = float(deadline_monotonic) - self._monotonic()
        if remaining > 0:
            return remaining
        raise TerminalExchangeDeadlineError(
            "exchange cancel total deadline exceeded"
        )

    @staticmethod
    def _cancel_path(order_kind: str) -> str:
        if order_kind == ALGO_ORDER:
            return "/fapi/v1/algoOrder"
        return "/fapi/v1/order"

    @staticmethod
    def _identity_params(request: CancelOrderRequest) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if request.order_kind == ALGO_ORDER:
            params["algoId"] = request.venue_order_id
            return params
        params["symbol"] = request.symbol
        if request.venue_order_id:
            params["orderId"] = request.venue_order_id
        else:
            params["origClientOrderId"] = request.client_order_id
        return params

    @staticmethod
    def _matches(row: Mapping[str, Any], request: CancelOrderRequest) -> bool:
        symbol = str(row.get("symbol") or "")
        if symbol and symbol != request.symbol:
            return False
        position_side = str(row.get("positionSide") or row.get("position_side") or "").upper()
        if position_side and position_side != request.position_side.upper():
            return False
        venue_key = "orderId"
        client_key = "clientOrderId"
        if request.order_kind == ALGO_ORDER:
            venue_key = "algoId"
            client_key = "clientAlgoId"
        if request.venue_order_id:
            return str(row.get(venue_key) or "") == request.venue_order_id
        return str(row.get(client_key) or "") == str(request.client_order_id)


class SignedBinanceTransport:
    """Minimal signed Binance Futures HTTP transport."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        proxy_url: str | bool | None = None,
        timeout_seconds: float = 10.0,
        recv_window_ms: int = DEFAULT_RECV_WINDOW_MS,
        timestamp_ms: Callable[[], int] | None = None,
        connection_factory: Callable[[float], Any] | None = None,
    ) -> None:
        if recv_window_ms <= 0 or recv_window_ms > MAX_RECV_WINDOW_MS:
            raise ValueError(
                f"recv_window_ms must be between 1 and {MAX_RECV_WINDOW_MS}"
            )
        self._base_url = base_url.rstrip("/")
        parsed_base_url = urllib.parse.urlsplit(self._base_url)
        if (
            parsed_base_url.scheme not in {"http", "https"}
            or not parsed_base_url.hostname
        ):
            raise ValueError("base_url must be an http(s) URL")
        if parsed_base_url.username is not None:
            raise ValueError("base_url must not contain user information")
        if parsed_base_url.password is not None:
            raise ValueError("base_url must not contain user information")
        try:
            parsed_base_url.port
        except ValueError as exc:
            raise ValueError("base_url contains an invalid port") from exc
        self._base_scheme = parsed_base_url.scheme
        self._base_host = parsed_base_url.hostname
        self._base_port = parsed_base_url.port
        if self._base_port is None:
            self._base_port = 443
            if self._base_scheme == "http":
                self._base_port = 80
        self._base_path = parsed_base_url.path.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeout_seconds = timeout_seconds
        self._recv_window_ms = recv_window_ms
        self._timestamp_ms = timestamp_ms
        self._proxy = _validated_binance_proxy(proxy_url)
        self._connection_factory = connection_factory
        self._connection_lock = threading.Lock()
        self._connection: Any = False

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        selected_timeout = self._timeout_seconds
        if timeout_seconds is not None:
            requested_timeout = float(timeout_seconds)
            if requested_timeout <= 0:
                raise ValueError("timeout_seconds must be positive")
            selected_timeout = min(selected_timeout, requested_timeout)
        query_params = dict(params)
        query_params["recvWindow"] = self._recv_window_ms
        if self._timestamp_ms is None:
            timestamp = int(time.time() * 1000)
        else:
            timestamp = int(self._timestamp_ms())
        query_params["timestamp"] = timestamp
        query = urllib.parse.urlencode(query_params)
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        target = (
            f"{self._base_path}{path}?{query}&signature={signature}"
        )
        proxy = self._proxy
        if proxy is not False and self._base_scheme == "http":
            target = f"{self._base_url}{path}?{query}&signature={signature}"
        headers = {
            "X-MBX-APIKEY": self._api_key,
            "Accept": "application/json",
            "Connection": "keep-alive",
        }
        with self._connection_lock:
            try:
                connection = self._ensure_connection(selected_timeout)
                self._set_connection_timeout(
                    connection,
                    selected_timeout,
                )
                connection.request(
                    method,
                    target,
                    body=None,
                    headers=headers,
                )
                response = connection.getresponse()
                try:
                    raw = response.read()
                    status = int(response.status)
                    response_headers = response.headers
                    will_close = bool(
                        getattr(response, "will_close", False)
                    )
                finally:
                    response.close()
                if will_close:
                    self._close_connection_unlocked()
            except (OSError, http.client.HTTPException) as exc:
                self._close_connection_unlocked()
                detail = str(exc).strip()
                if not detail:
                    detail = type(exc).__name__
                raise ExchangeCancelError(
                    f"{method} {path} failed: {detail}"
                ) from exc
        if status >= 400:
            try:
                error = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error = {}
            code = error.get("code", status)
            message = error.get(
                "msg",
                raw.decode("utf-8", errors="replace"),
            )
            raise BinanceApiError(
                int(code),
                str(message),
                http_status=status,
                headers=response_headers,
            )
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def close(self) -> None:
        with self._connection_lock:
            self._close_connection_unlocked()

    def _ensure_connection(self, timeout_seconds: float) -> Any:
        connection = self._connection
        if connection is not False:
            return connection
        factory = self._connection_factory
        if factory is None:
            connection = self._build_connection(timeout_seconds)
        else:
            connection = factory(timeout_seconds)
        self._connection = connection
        return connection

    def _build_connection(self, timeout_seconds: float) -> Any:
        proxy = self._proxy
        if proxy is False:
            if self._base_scheme == "https":
                return http.client.HTTPSConnection(
                    self._base_host,
                    self._base_port,
                    timeout=timeout_seconds,
                )
            return http.client.HTTPConnection(
                self._base_host,
                self._base_port,
                timeout=timeout_seconds,
            )
        proxy_port = proxy.port
        if proxy_port is None:
            proxy_port = 443
            if proxy.scheme == "http":
                proxy_port = 80
        if self._base_scheme == "https":
            connection = http.client.HTTPSConnection(
                proxy.hostname,
                proxy_port,
                timeout=timeout_seconds,
            )
            connection.set_tunnel(
                self._base_host,
                self._base_port,
            )
            return connection
        connection_type = http.client.HTTPConnection
        if proxy.scheme == "https":
            connection_type = http.client.HTTPSConnection
        return connection_type(
            proxy.hostname,
            proxy_port,
            timeout=timeout_seconds,
        )

    @staticmethod
    def _set_connection_timeout(
        connection: Any,
        timeout_seconds: float,
    ) -> None:
        connection.timeout = timeout_seconds
        connection_socket = getattr(connection, "sock", None)
        if connection_socket is not None:
            connection_socket.settimeout(timeout_seconds)

    def _close_connection_unlocked(self) -> None:
        connection = self._connection
        self._connection = False
        if connection is False:
            return
        try:
            connection.close()
        except OSError:
            return


def _validated_binance_proxy(
    proxy_url: str | bool | None,
) -> Any:
    if proxy_url is None or proxy_url is False:
        return False
    if not isinstance(proxy_url, str):
        raise ValueError("proxy_url must be an http(s) URL")
    normalized_proxy_url = proxy_url.strip()
    parsed_proxy = urllib.parse.urlsplit(normalized_proxy_url)
    if (
        parsed_proxy.scheme not in {"http", "https"}
        or not parsed_proxy.hostname
    ):
        raise ValueError("proxy_url must be an http(s) URL")
    if (
        parsed_proxy.username is not None
        or parsed_proxy.password is not None
    ):
        raise ValueError("proxy_url must not contain user information")
    try:
        parsed_proxy.port
    except ValueError as exc:
        raise ValueError("proxy_url contains an invalid port") from exc
    return parsed_proxy


class BinanceExchangeEvidenceProvider:
    """Read-only live venue evidence for the control-plane heartbeat gate."""

    def __init__(
        self,
        *,
        transport: ExchangeTransport,
        refresh_interval_seconds: float = 5.0,
        total_deadline_seconds: float = DEFAULT_EVIDENCE_TOTAL_DEADLINE_SECONDS,
        backoff_base_seconds: float = DEFAULT_EVIDENCE_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = DEFAULT_EVIDENCE_BACKOFF_MAX_SECONDS,
        circuit_failure_threshold: int = DEFAULT_EVIDENCE_CIRCUIT_FAILURES,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        if refresh_interval_seconds <= 0:
            raise ValueError("refresh_interval_seconds must be positive")
        if (
            total_deadline_seconds <= 0
            or total_deadline_seconds
            >= CONTROL_PLANE_FRESHNESS_BUDGET_SECONDS
        ):
            raise ValueError(
                "total_deadline_seconds must be positive and below "
                "the control-plane freshness budget"
            )
        if backoff_base_seconds <= 0:
            raise ValueError("backoff_base_seconds must be positive")
        if backoff_max_seconds < backoff_base_seconds:
            raise ValueError(
                "backoff_max_seconds must be at least backoff_base_seconds"
            )
        if circuit_failure_threshold < 1:
            raise ValueError("circuit_failure_threshold must be positive")
        self._transport = transport
        self._refresh_interval_seconds = float(refresh_interval_seconds)
        self._total_deadline_seconds = float(total_deadline_seconds)
        self._backoff_base_seconds = float(backoff_base_seconds)
        self._backoff_max_seconds = float(backoff_max_seconds)
        self._circuit_failure_threshold = int(circuit_failure_threshold)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._jitter = jitter or (
            lambda delay: random.uniform(0.0, delay * 0.25)
        )
        self._lock = threading.Lock()
        self._refresh_condition = threading.Condition(self._lock)
        self._cached: dict[str, Any] | None = None
        self._cached_at: float | None = None
        self._consecutive_failures = 0
        self._next_attempt_at = 0.0
        self._circuit_open_until = 0.0
        self._refresh_in_flight = False
        self._refresh_generation = 0
        self._refresh_error: ExchangeCancelError | bool = False
        self._refresh_error_generation = 0
        self._margin_cached: dict[str, Any] | None = None
        self._margin_cached_at: float | None = None

    def snapshot(self, *, force_refresh: bool = False) -> dict[str, Any]:
        deadline = self._monotonic() + self._total_deadline_seconds
        with self._refresh_condition:
            current_monotonic = self._monotonic()
            cached = self._cached
            cached_at = self._cached_at
            if (
                not force_refresh
                and cached is not None
                and cached_at is not None
                and current_monotonic - cached_at
                < self._refresh_interval_seconds
            ):
                return _copy_exchange_evidence(cached)
            if self._refresh_in_flight:
                refresh_generation = self._refresh_generation + 1
                return self._await_refresh(
                    refresh_generation,
                    deadline=deadline,
                )
            self._raise_if_backoff_active(current_monotonic)
            self._refresh_in_flight = True
            refresh_generation = self._refresh_generation + 1
        try:
            evidence = self._refresh_evidence(deadline)
        except Exception as exc:
            with self._refresh_condition:
                failure = self._record_failure(exc)
                self._refresh_generation = refresh_generation
                self._refresh_error = failure
                self._refresh_error_generation = refresh_generation
                self._refresh_in_flight = False
                self._refresh_condition.notify_all()
            raise failure from exc
        with self._refresh_condition:
            self._cached = evidence
            self._cached_at = self._monotonic()
            self._consecutive_failures = 0
            self._next_attempt_at = 0.0
            self._circuit_open_until = 0.0
            self._refresh_generation = refresh_generation
            self._refresh_error = False
            self._refresh_error_generation = 0
            self._refresh_in_flight = False
            self._refresh_condition.notify_all()
            return _copy_exchange_evidence(evidence)

    def _await_refresh(
        self,
        refresh_generation: int,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        while self._refresh_generation < refresh_generation:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ExchangeCancelError(
                    "exchange evidence singleflight deadline exceeded"
                )
            self._refresh_condition.wait(timeout=remaining)
        if self._refresh_error_generation == refresh_generation:
            error = self._refresh_error
            if error is not False:
                raise ExchangeCancelError(str(error))
        cached = self._cached
        if cached is None:
            raise ExchangeCancelError(
                "exchange evidence singleflight completed without a snapshot"
            )
        return _copy_exchange_evidence(cached)

    def _refresh_evidence(self, deadline: float) -> dict[str, Any]:
        positions_payload, positions_fetched_at = self._fetch_endpoint(
            "/fapi/v2/positionRisk",
            deadline=deadline,
        )
        positions = _position_evidence_rows(positions_payload)
        regular_payload, regular_orders_fetched_at = self._fetch_endpoint(
            "/fapi/v1/openOrders",
            deadline=deadline,
        )
        regular_orders = _order_evidence_rows(
            regular_payload,
            order_kind=REGULAR_ORDER,
        )
        algo_payload, algo_orders_fetched_at = self._fetch_endpoint(
            "/fapi/v1/openAlgoOrders",
            deadline=deadline,
        )
        algo_orders = _order_evidence_rows(
            algo_payload,
            order_kind=ALGO_ORDER,
        )
        fetched_at = min(
            positions_fetched_at,
            regular_orders_fetched_at,
            algo_orders_fetched_at,
        )
        return {
            "positions": positions,
            "regular_orders": regular_orders,
            "algo_orders": algo_orders,
            "positions_fetched_at": positions_fetched_at,
            "regular_orders_fetched_at": regular_orders_fetched_at,
            "algo_orders_fetched_at": algo_orders_fetched_at,
            "fetched_at": fetched_at,
        }

    def cached_snapshot(
        self,
        *,
        max_age_seconds: float,
    ) -> dict[str, Any] | bool:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        with self._lock:
            cached = self._cached
            cached_at = self._cached_at
            if cached is None or cached_at is None:
                return False
            age_seconds = self._monotonic() - cached_at
            if age_seconds > max_age_seconds:
                return False
            return _copy_exchange_evidence(cached)

    def margin_snapshot(
        self,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        current_monotonic = self._monotonic()
        with self._lock:
            cached = self._margin_cached
            cached_at = self._margin_cached_at
            if (
                not force_refresh
                and cached is not None
                and cached_at is not None
                and current_monotonic - cached_at
                < self._refresh_interval_seconds
            ):
                return dict(cached)
        deadline = current_monotonic + self._total_deadline_seconds
        payload, fetched_at = self._fetch_endpoint(
            "/fapi/v2/account",
            deadline=deadline,
        )
        evidence = _account_margin_evidence(
            payload,
            fetched_at=fetched_at,
        )
        with self._lock:
            self._margin_cached = evidence
            self._margin_cached_at = self._monotonic()
            return dict(evidence)

    def _fetch_endpoint(
        self,
        path: str,
        *,
        deadline: float,
    ) -> tuple[Any, datetime]:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise ExchangeCancelError(
                "exchange evidence round deadline exceeded"
            )
        payload = self._transport.request(
            "GET",
            path,
            {},
            timeout_seconds=remaining,
        )
        if self._monotonic() > deadline:
            raise ExchangeCancelError(
                "exchange evidence round deadline exceeded"
            )
        completed_at = self._now()
        if completed_at.tzinfo is None:
            raise ValueError("exchange evidence clock must be timezone-aware")
        return payload, completed_at.astimezone(timezone.utc)

    def _raise_if_backoff_active(self, current_monotonic: float) -> None:
        if current_monotonic < self._circuit_open_until:
            remaining = self._circuit_open_until - current_monotonic
            raise ExchangeCancelError(
                "exchange evidence circuit open; "
                f"backoff active for {remaining:.3f}s"
            )
        if current_monotonic < self._next_attempt_at:
            remaining = self._next_attempt_at - current_monotonic
            raise ExchangeCancelError(
                f"exchange evidence backoff active for {remaining:.3f}s"
            )

    def _record_failure(self, exc: Exception) -> ExchangeCancelError:
        self._consecutive_failures += 1
        exponent = self._consecutive_failures - 1
        exponential_delay = self._backoff_base_seconds * (2**exponent)
        exponential_delay = min(
            exponential_delay,
            self._backoff_max_seconds,
        )
        jitter = float(self._jitter(exponential_delay))
        jitter = max(jitter, 0.0)
        jitter = min(jitter, exponential_delay * 0.25)
        bounded_delay = min(
            exponential_delay + jitter,
            self._backoff_max_seconds,
        )

        rate_limited = (
            isinstance(exc, BinanceApiError)
            and exc.http_status in {418, 429}
        )
        retry_after = 0.0
        if rate_limited:
            retry_after = _retry_after_seconds(
                exc.headers.get("retry-after"),
                now=self._now(),
            )
        retry_delay = max(bounded_delay, retry_after)
        current_monotonic = self._monotonic()
        self._next_attempt_at = current_monotonic + retry_delay

        circuit_open = rate_limited
        if self._consecutive_failures >= self._circuit_failure_threshold:
            circuit_open = True
        if circuit_open:
            self._circuit_open_until = self._next_attempt_at

        detail = str(exc).strip()
        if not detail:
            detail = type(exc).__name__
        if rate_limited:
            return ExchangeCancelError(
                "exchange evidence rate limited; "
                f"retry in {retry_delay:.3f}s: {detail}"
            )
        return ExchangeCancelError(
            "exchange evidence unavailable; "
            f"retry in {retry_delay:.3f}s: {detail}"
        )


def _copy_exchange_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "positions": [dict(item) for item in evidence["positions"]],
        "regular_orders": [dict(item) for item in evidence["regular_orders"]],
        "algo_orders": [dict(item) for item in evidence["algo_orders"]],
        "positions_fetched_at": evidence["positions_fetched_at"],
        "regular_orders_fetched_at": evidence["regular_orders_fetched_at"],
        "algo_orders_fetched_at": evidence["algo_orders_fetched_at"],
        "fetched_at": evidence["fetched_at"],
    }


def _retry_after_seconds(
    value: str | None,
    *,
    now: datetime,
) -> float:
    if not value:
        return 0.0
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            raise ValueError("exchange evidence clock must be timezone-aware")
        seconds = (
            retry_at.astimezone(timezone.utc)
            - now.astimezone(timezone.utc)
        ).total_seconds()
    return max(seconds, 0.0)


def _position_evidence_rows(payload: Any) -> list[dict[str, Any]]:
    rows = _required_exchange_rows(payload, "positionRisk")
    evidence = []
    for row in rows:
        raw_quantity = row.get("positionAmt")
        try:
            quantity = Decimal(str(raw_quantity))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ExchangeCancelError(
                "positionRisk returned an invalid positionAmt"
            ) from exc
        if not quantity.is_finite():
            raise ExchangeCancelError(
                "positionRisk returned a non-finite positionAmt"
            )
        if quantity == 0:
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            raise ExchangeCancelError(
                "positionRisk returned a position without symbol"
            )
        evidence.append(
            {
                "symbol": symbol,
                "quantity": format(quantity, "f"),
                "position_side": str(
                    row.get("positionSide") or "BOTH"
                ).upper(),
                "entry_price": str(row.get("entryPrice") or ""),
                "mark_price": str(row.get("markPrice") or ""),
            }
        )
    return evidence


def _account_margin_evidence(
    payload: Any,
    *,
    fetched_at: datetime,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ExchangeCancelError(
            "account endpoint returned an invalid object"
        )
    try:
        available_balance = Decimal(
            str(payload.get("availableBalance"))
        )
        total_margin_balance = Decimal(
            str(payload.get("totalMarginBalance"))
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeCancelError(
            "account endpoint returned invalid margin balances"
        ) from exc
    if (
        not available_balance.is_finite()
        or not total_margin_balance.is_finite()
        or available_balance < 0
        or total_margin_balance <= 0
    ):
        raise ExchangeCancelError(
            "account endpoint returned invalid margin balances"
        )
    return {
        "available_balance": format(available_balance, "f"),
        "total_margin_balance": format(total_margin_balance, "f"),
        "margin_ratio": format(
            available_balance / total_margin_balance,
            "f",
        ),
        "fetched_at": fetched_at,
    }


def _order_evidence_rows(
    payload: Any,
    *,
    order_kind: str,
) -> list[dict[str, Any]]:
    raw_rows = payload
    if isinstance(payload, Mapping):
        raw_rows = payload.get("orders")
    rows = _required_exchange_rows(raw_rows, f"{order_kind} orders")
    evidence = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            raise ExchangeCancelError(
                f"{order_kind} order returned without symbol"
            )
        venue_order_id = row.get("orderId")
        client_order_id = row.get("clientOrderId")
        if order_kind == ALGO_ORDER:
            venue_order_id = row.get("algoId")
            client_order_id = row.get("clientAlgoId")
        evidence.append(
            {
                "symbol": symbol,
                "position_side": str(
                    row.get("positionSide") or "BOTH"
                ).upper(),
                "side": str(row.get("side") or "").upper(),
                "order_type": str(
                    row.get("type") or row.get("orderType") or ""
                ).upper(),
                "quantity": str(
                    row.get("origQty") or row.get("quantity") or ""
                ),
                "executed_quantity": str(
                    row.get("executedQty")
                    or row.get("executedQuantity")
                    or ""
                ),
                "price": str(row.get("price") or ""),
                "stop_price": str(
                    row.get("stopPrice")
                    or row.get("triggerPrice")
                    or ""
                ),
                "activation_price": str(
                    row.get("activatePrice")
                    or row.get("activationPrice")
                    or ""
                ),
                "callback_rate": str(row.get("priceRate") or ""),
                "time_in_force": str(
                    row.get("timeInForce") or ""
                ).upper(),
                "working_type": str(
                    row.get("workingType") or ""
                ).upper(),
                "price_match": str(
                    row.get("priceMatch") or ""
                ).upper(),
                "reduce_only": bool(row.get("reduceOnly") is True),
                "close_position": bool(row.get("closePosition") is True),
                "price_protect": bool(row.get("priceProtect") is True),
                "good_till_date": str(row.get("goodTillDate") or ""),
                "client_order_id": str(client_order_id or ""),
                "venue_order_id": str(venue_order_id or ""),
                "order_kind": order_kind,
            }
        )
    return evidence


def _required_exchange_rows(payload: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(payload, list):
        raise ExchangeCancelError(f"{label} returned an invalid collection")
    rows = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise ExchangeCancelError(f"{label} returned a non-object entry")
        rows.append(item)
    return rows


class ControlPlaneExchangeStateMirror:
    """Account-filtered, read-only view of exchange_state_mirror."""

    def __init__(
        self,
        *,
        account_id: str,
        node_id: str,
        base_url: str,
        token: str,
        timeout_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._account_id = account_id
        self._node_id = node_id
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._monotonic = monotonic
        self._orders: tuple[ExchangeOrderRef, ...] = ()
        self._fresh = False
        self._lock = threading.Lock()
        self._writer_identity_lock = threading.Lock()
        self._writer_identity: tuple[str, str, int] | None = None
        self._fatal_fence_lock = threading.Lock()
        self._fatal_fence_hook: Callable[[str], None] | None = None
        self._fatal_fence_reason: str | bool = False
        self._fatal_fence_hook_invoked = False

    def bind_writer_identity(
        self,
        *,
        redis_fencing_epoch: str,
        runtime_generation: str,
        lease_fencing_token: int,
    ) -> None:
        candidate = _exchange_mirror_writer_identity(
            redis_fencing_epoch=redis_fencing_epoch,
            runtime_generation=runtime_generation,
            lease_fencing_token=lease_fencing_token,
        )
        with self._writer_identity_lock:
            current = self._writer_identity
            if current is not None and current != candidate:
                raise ExchangeCancelError(
                    "exchange state mirror writer identity is already bound"
                )
            self._writer_identity = candidate

    def bind_fatal_fence_hook(
        self,
        hook: Callable[[str], None],
    ) -> None:
        if not callable(hook):
            raise TypeError("exchange state mirror fatal fence hook must be callable")
        invoke_reason: str | bool = False
        with self._fatal_fence_lock:
            current = self._fatal_fence_hook
            if current is not None and current is not hook:
                raise ExchangeCancelError(
                    "exchange state mirror fatal fence hook is already bound"
                )
            self._fatal_fence_hook = hook
            if (
                self._fatal_fence_reason is not False
                and not self._fatal_fence_hook_invoked
            ):
                self._fatal_fence_hook_invoked = True
                invoke_reason = self._fatal_fence_reason
        if invoke_reason is not False:
            self._invoke_fatal_fence_hook(hook, str(invoke_reason))

    def refresh(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[ExchangeOrderRef, ...]:
        fatal_fence_reason = self._current_fatal_fence_reason()
        if fatal_fence_reason is not False:
            raise ExchangeCancelError(str(fatal_fence_reason))
        self._invalidate()
        timeout_seconds = self._timeout_seconds
        if deadline_monotonic is not None:
            remaining = (
                float(deadline_monotonic) - self._monotonic()
            )
            if remaining <= 0:
                raise TerminalExchangeDeadlineError(
                    "exchange state mirror total deadline exceeded"
                )
            timeout_seconds = min(timeout_seconds, remaining)
        query = urllib.parse.urlencode({"account_id": self._account_id})
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "X-Node-Id": self._node_id,
            "X-Account-Id": self._account_id,
        }
        with self._writer_identity_lock:
            writer_identity = self._writer_identity
        if writer_identity is not None:
            (
                redis_fencing_epoch,
                runtime_generation,
                lease_fencing_token,
            ) = writer_identity
            headers["X-Redis-Fencing-Epoch"] = redis_fencing_epoch
            headers["X-Runtime-Generation"] = runtime_generation
            headers["X-Lease-Fencing-Token"] = str(
                lease_fencing_token
            )
        request = urllib.request.Request(
            f"{self._base_url}/v1/nodes/{self._node_id}/exchange-state?{query}",
            headers=headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_seconds,
            ) as response:
                raw = response.read()
        except HTTPError as exc:
            if exc.code == 409:
                detail = _exchange_mirror_http_error_detail(exc)
                reason = (
                    "exchange state mirror rejected stale writer: "
                    f"{detail}"
                )
                self._trigger_fatal_fence(reason)
                raise ExchangeCancelError(reason) from exc
            raise ExchangeCancelError(
                f"exchange state mirror refresh failed: {exc}"
            ) from exc
        except URLError as exc:
            raise ExchangeCancelError(f"exchange state mirror refresh failed: {exc}") from exc
        payload = json.loads(raw.decode("utf-8"))
        response_account = str(payload.get("account_id") or "")
        if response_account != self._account_id:
            raise WrongAccountError(
                f"mirror returned account {response_account!r} for {self._account_id!r}"
            )
        if payload.get("stale") is not False:
            raise ExchangeCancelError("exchange state mirror is stale")
        exchange_payload = payload.get("payload")
        if not isinstance(exchange_payload, Mapping):
            raise ExchangeCancelError("exchange state mirror payload is missing")
        orders = _parse_exchange_orders(self._account_id, exchange_payload)
        with self._lock:
            self._orders = orders
            self._fresh = True
        return orders

    def orders_for_instrument(self, instrument_id: str) -> tuple[ExchangeOrderRef, ...]:
        target = str(instrument_id)
        with self._lock:
            if not self._fresh:
                raise ExchangeCancelError("exchange state mirror is not fresh")
            orders = self._orders
        return tuple(order for order in orders if order.instrument_id == target)

    def find_order(self, instrument_id: str, client_order_id: str) -> ExchangeOrderRef | bool:
        for order in self.orders_for_instrument(instrument_id):
            if order.client_order_id == client_order_id:
                return order
        return False

    def _invalidate(self) -> None:
        with self._lock:
            self._orders = ()
            self._fresh = False

    def _trigger_fatal_fence(self, reason: str) -> None:
        hook: Callable[[str], None] | None = None
        with self._fatal_fence_lock:
            if self._fatal_fence_reason is not False:
                return
            self._fatal_fence_reason = reason
            configured_hook = self._fatal_fence_hook
            if (
                callable(configured_hook)
                and not self._fatal_fence_hook_invoked
            ):
                self._fatal_fence_hook_invoked = True
                hook = configured_hook
        if hook is not None:
            self._invoke_fatal_fence_hook(hook, reason)

    def _invoke_fatal_fence_hook(
        self,
        hook: Callable[[str], None],
        reason: str,
    ) -> None:
        try:
            hook(reason)
        except Exception:
            return

    def _current_fatal_fence_reason(self) -> str | bool:
        with self._fatal_fence_lock:
            return self._fatal_fence_reason


def _exchange_mirror_writer_identity(
    *,
    redis_fencing_epoch: str,
    runtime_generation: str,
    lease_fencing_token: int,
) -> tuple[str, str, int]:
    epoch = str(redis_fencing_epoch or "").strip()
    try:
        parsed_epoch = UUID(epoch)
    except ValueError as exc:
        raise ValueError(
            "exchange state mirror redis_fencing_epoch is invalid"
        ) from exc
    if parsed_epoch.version != 4 or str(parsed_epoch) != epoch:
        raise ValueError(
            "exchange state mirror redis_fencing_epoch is invalid"
        )
    generation = str(runtime_generation or "").strip()
    if not generation:
        raise ValueError(
            "exchange state mirror runtime_generation is required"
        )
    token = lease_fencing_token
    if isinstance(token, bool) or not isinstance(token, int):
        raise ValueError(
            "exchange state mirror lease_fencing_token must be an integer"
        )
    if token < 1:
        raise ValueError(
            "exchange state mirror lease_fencing_token must be positive"
        )
    return epoch, generation, token


def _exchange_mirror_http_error_detail(exc: HTTPError) -> str:
    raw = exc.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, Mapping):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    detail = raw.strip()
    if detail:
        return detail
    return f"HTTP {exc.code}"


def _cancel_request_from_exchange_order(
    order: Any,
) -> CancelOrderRequest:
    venue_order_id = str(
        getattr(order, "venue_order_id", "") or ""
    )
    client_order_id = str(
        getattr(order, "client_order_id", "") or ""
    )
    return CancelOrderRequest(
        account_id=str(getattr(order, "account_id", "") or ""),
        symbol=str(getattr(order, "symbol", "") or ""),
        position_side=str(
            getattr(order, "position_side", "") or ""
        ),
        order_kind=str(getattr(order, "order_kind", "") or ""),
        venue_order_id=venue_order_id or None,
        client_order_id=client_order_id or None,
    )


def _terminal_exchange_instrument_matches(
    instrument_id: str,
    instrument_ids: tuple[str, ...],
) -> bool:
    if not instrument_ids:
        return True
    target = _canonical_exchange_symbol(instrument_id)
    for candidate in instrument_ids:
        if _canonical_exchange_symbol(candidate) == target:
            return True
    return False


def _canonical_exchange_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    for suffix in (
        "-PERP.BINANCE",
        ".BINANCE",
        "-PERP",
        "/USDT",
    ):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break
    return re.sub(r"[^A-Z0-9]", "", symbol)


def _parse_exchange_orders(
    account_id: str,
    payload: Mapping[str, Any],
) -> tuple[ExchangeOrderRef, ...]:
    parsed: list[ExchangeOrderRef] = []
    for collection_name, order_kind in (
        ("open_orders", REGULAR_ORDER),
        ("algo_orders", ALGO_ORDER),
    ):
        rows = payload.get(collection_name, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            parsed_order = _parse_exchange_order(account_id, row, order_kind)
            if parsed_order:
                parsed.append(parsed_order)
    return tuple(parsed)


def _parse_exchange_order(
    account_id: str,
    row: Mapping[str, Any],
    order_kind: str,
) -> ExchangeOrderRef | bool:
    symbol = str(row.get("symbol") or "")
    client_order_id = str(row.get("client_order_id") or "")
    venue_order_id = str(
        row.get("venue_order_id")
        or row.get("order_id")
        or row.get("algoId")
        or row.get("orderId")
        or ""
    )
    if not symbol or not client_order_id:
        return False
    position_side = str(row.get("position_side") or row.get("positionSide") or "BOTH").upper()
    order_type = str(row.get("type") or row.get("order_type") or "")
    lifecycle_role = ""
    if "TAKE_PROFIT" in order_type.upper():
        lifecycle_role = "take_profit"
    elif "STOP" in order_type.upper():
        lifecycle_role = "stop_loss"
    tags: tuple[str, ...] = ()
    if lifecycle_role:
        tags = (f"lifecycle_role={lifecycle_role}",)
    price = row.get("price")
    trigger_price = row.get("trigger_price")
    return ExchangeOrderRef(
        account_id=account_id,
        symbol=symbol,
        position_side=position_side,
        order_kind=order_kind,
        venue_order_id=venue_order_id,
        client_order_id=client_order_id,
        order_type=order_type,
        side=str(row.get("side") or ""),
        quantity=str(row.get("quantity") or ""),
        price=str(price) if price is not None else None,
        trigger_price=str(trigger_price) if trigger_price is not None else None,
        tags=tags,
        time_in_force=str(
            row.get("time_in_force") or row.get("timeInForce") or ""
        ).upper(),
        reduce_only=bool(
            row.get("reduce_only") is True
            or row.get("reduceOnly") is True
        ),
    )


def _terminal_order_preservation_outcome(
    order: Any,
    *,
    durable_entry_preservation: DurableEntryOrderPreservation | None,
    preserve_protection: bool,
) -> str | bool:
    if (
        durable_entry_preservation is not None
        and _terminal_order_matches_durable_entry_preservation(
            order,
            durable_entry_preservation,
        )
    ):
        return "durable_entry_preserved"
    if preserve_protection and _terminal_order_is_protection(order):
        return "protective_order_preserved"
    return False


def _terminal_order_matches_durable_entry_preservation(
    order: Any,
    preservation: DurableEntryOrderPreservation,
) -> bool:
    order_kind = str(
        getattr(order, "order_kind", "") or ""
    ).strip().lower()
    if order_kind != REGULAR_ORDER:
        return False
    if getattr(order, "reduce_only", None) is not False:
        return False
    order_type = str(
        getattr(order, "order_type", "") or ""
    ).strip().upper()
    if order_type != "LIMIT":
        return False
    time_in_force = str(
        getattr(order, "time_in_force", "") or ""
    ).strip().upper()
    if time_in_force != "GTC":
        return False
    actual_instrument_id = str(
        getattr(order, "instrument_id", "") or ""
    )
    if (
        _canonical_exchange_symbol(actual_instrument_id)
        != _canonical_exchange_symbol(preservation.instrument_id)
    ):
        return False
    if _exchange_order_side(order) != _exchange_order_side(
        preservation.side
    ):
        return False
    actual_quantity = _positive_exchange_decimal(
        getattr(order, "quantity", None)
    )
    expected_quantity = _positive_exchange_decimal(
        preservation.quantity
    )
    actual_price = _positive_exchange_decimal(
        getattr(order, "price", None)
    )
    expected_price = _positive_exchange_decimal(preservation.price)
    if (
        actual_quantity is False
        or expected_quantity is False
        or actual_price is False
        or expected_price is False
    ):
        return False
    return (
        actual_quantity == expected_quantity
        and actual_price == expected_price
    )


def _terminal_order_is_protection(order: Any) -> bool:
    order_kind = str(
        getattr(order, "order_kind", "") or ""
    ).strip().lower()
    if order_kind != ALGO_ORDER:
        return False
    if getattr(order, "reduce_only", None) is not True:
        return False
    order_type = str(
        getattr(order, "order_type", "") or ""
    ).strip().upper()
    return order_type in _PROTECTIVE_ORDER_TYPES


def _exchange_order_side(value: Any) -> str:
    raw = value
    if not isinstance(value, str):
        raw = getattr(value, "side", value)
    side = str(getattr(raw, "value", raw) or "").strip().upper()
    if "." in side:
        side = side.rsplit(".", 1)[-1]
    if side in {"BUY", "LONG"}:
        return "BUY"
    if side in {"SELL", "SHORT"}:
        return "SELL"
    return ""


def _positive_exchange_decimal(value: Any) -> Decimal | bool:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not number.is_finite() or number <= 0:
        return False
    return number
