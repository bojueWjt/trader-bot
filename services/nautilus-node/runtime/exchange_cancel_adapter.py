"""Account-scoped Binance cancel adapter and exchange-state mirror.

The adapter is intentionally narrow: only an explicit single-order cancel intent
may call it. It never submits, modifies, or scans-and-cancels orders.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError


REGULAR_ORDER = "regular"
ALGO_ORDER = "algo"
_CANCEL_ACTIONS = frozenset({"cancel", "cancel_order"})
_CANCELED_STATUSES = frozenset({"CANCELED", "CANCELLED"})
_FILLED_STATUSES = frozenset({"FILLED", "EXECUTED", "TRIGGERED"})
_ABSENT_ORDER_CODES = frozenset({-2011, -2013})
OPENING_CONFIRMED_EXECUTED = "confirmed_executed"
OPENING_DEFINITIVELY_ABSENT = "definitively_absent"
OPENING_UNKNOWN = "unknown"
_OPENING_EXECUTION_STATES = frozenset(
    {
        OPENING_CONFIRMED_EXECUTED,
        OPENING_DEFINITIVELY_ABSENT,
        OPENING_UNKNOWN,
    }
)
_DETERMINISTIC_OPENING_CLIENT_ORDER_ID = re.compile(
    r"^B[0-9a-fA-F]{32}0[0-9]$"
)
DEFAULT_RECV_WINDOW_MS = 30_000
MAX_RECV_WINDOW_MS = 60_000


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
    def __init__(self, code: int, message: str) -> None:
        self.code = int(code)
        self.message = str(message)
        super().__init__(f"Binance API {self.code}: {self.message}")


class ExchangeTransport(Protocol):
    def request(self, method: str, path: str, params: dict[str, Any]) -> Any: ...


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

    @property
    def instrument_id(self) -> str:
        return f"{self.symbol}-PERP.BINANCE"


@dataclass(frozen=True)
class OpeningExecutionEvidence:
    account_id: str
    client_order_id: str
    state: str
    order_status: str | None = None
    instrument_id: str | None = None
    venue_order_id: str | None = None
    filled_quantity: str | None = None
    sources: tuple[str, ...] = ()
    observed_at: str | None = None
    reason: str | None = None


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

    def cancel(self, intent_action: str, request: CancelOrderRequest) -> CancelResult:
        action = str(intent_action).strip().lower()
        if action not in _CANCEL_ACTIONS:
            raise CancelIntentRequiredError(
                f"exchange cancel adapter requires cancel intent, got {intent_action!r}"
            )
        if request.account_id != self._account_id:
            raise WrongAccountError(
                f"adapter account {self._account_id!r} cannot cancel for {request.account_id!r}"
            )

        delete_succeeded = False
        try:
            self._transport.request(
                "DELETE",
                self._cancel_path(request.order_kind),
                self._identity_params(request),
            )
            delete_succeeded = True
        except BinanceApiError as exc:
            if exc.code not in _ABSENT_ORDER_CODES:
                raise

        self._wait_until_absent(request)
        status = self._read_terminal_status(request)
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

    def _wait_until_absent(self, request: CancelOrderRequest) -> None:
        deadline = self._monotonic() + self._confirmation_timeout_seconds
        while True:
            rows = self._open_orders(request)
            if not any(self._matches(row, request) for row in rows):
                return
            if self._monotonic() >= deadline:
                raise CancelConfirmationTimeoutError(
                    f"order remained open after cancel timeout: "
                    f"account={request.account_id} symbol={request.symbol} "
                    f"position_side={request.position_side} kind={request.order_kind}"
                )
            self._sleep(self._poll_interval_seconds)

    def _open_orders(self, request: CancelOrderRequest) -> list[Mapping[str, Any]]:
        path = "/fapi/v1/openOrders"
        params: dict[str, Any] = {"symbol": request.symbol}
        if request.order_kind == ALGO_ORDER:
            path = "/fapi/v1/openAlgoOrders"
            params = {}
        payload = self._transport.request("GET", path, params)
        rows: Any = payload
        if isinstance(payload, Mapping):
            rows = payload.get("orders", [])
        if not isinstance(rows, list):
            raise CancelStateError(f"{path} returned invalid order collection")
        return [row for row in rows if isinstance(row, Mapping)]

    def _read_terminal_status(self, request: CancelOrderRequest) -> str:
        path = "/fapi/v1/order"
        if request.order_kind == ALGO_ORDER:
            path = "/fapi/v1/algoOrder"
        try:
            payload = self._transport.request("GET", path, self._identity_params(request))
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
    """Minimal signed Binance Futures HTTP transport used only by cancel adapter."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        timeout_seconds: float = 10.0,
        recv_window_ms: int = DEFAULT_RECV_WINDOW_MS,
        timestamp_ms: Callable[[], int] | None = None,
    ) -> None:
        if recv_window_ms <= 0 or recv_window_ms > MAX_RECV_WINDOW_MS:
            raise ValueError(
                f"recv_window_ms must be between 1 and {MAX_RECV_WINDOW_MS}"
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeout_seconds = timeout_seconds
        self._recv_window_ms = recv_window_ms
        self._timestamp_ms = timestamp_ms

    def request(self, method: str, path: str, params: dict[str, Any]) -> Any:
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
        request = urllib.request.Request(
            f"{self._base_url}{path}?{query}&signature={signature}",
            headers={"X-MBX-APIKEY": self._api_key, "Accept": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            raw_error = exc.read()
            try:
                error = json.loads(raw_error.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error = {}
            code = error.get("code", exc.code)
            message = error.get("msg", raw_error.decode("utf-8", errors="replace"))
            raise BinanceApiError(int(code), str(message)) from exc
        except URLError as exc:
            raise ExchangeCancelError(f"{method} {path} failed: {exc.reason}") from exc
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))


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
    ) -> None:
        self._account_id = account_id
        self._node_id = node_id
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._orders: tuple[ExchangeOrderRef, ...] = ()
        self._opening_evidence: dict[str, OpeningExecutionEvidence] = {}
        self._opening_evidence_authoritative = False
        self._fresh = False
        self._lock = threading.Lock()

    def refresh(self) -> tuple[ExchangeOrderRef, ...]:
        self._invalidate()
        query = urllib.parse.urlencode({"account_id": self._account_id})
        request = urllib.request.Request(
            f"{self._base_url}/v1/nodes/{self._node_id}/exchange-state?{query}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "X-Node-Id": self._node_id,
                "X-Account-Id": self._account_id,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except (HTTPError, URLError) as exc:
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
        evidence, evidence_authoritative = _parse_opening_execution_evidence(
            self._account_id,
            payload.get("opening_execution_evidence"),
        )
        with self._lock:
            self._orders = orders
            self._opening_evidence = evidence
            self._opening_evidence_authoritative = evidence_authoritative
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

    def opening_execution_state(
        self,
        client_order_id: str,
    ) -> OpeningExecutionEvidence:
        normalized_id = str(client_order_id or "")
        if not _DETERMINISTIC_OPENING_CLIENT_ORDER_ID.fullmatch(normalized_id):
            return _unknown_opening_evidence(
                self._account_id,
                normalized_id,
                reason="unsupported_client_order_id",
            )
        with self._lock:
            fresh = self._fresh
            authoritative = self._opening_evidence_authoritative
            evidence = self._opening_evidence.get(normalized_id)
        if not fresh:
            return _unknown_opening_evidence(
                self._account_id,
                normalized_id,
                reason="mirror_not_fresh",
            )
        if not authoritative:
            return _unknown_opening_evidence(
                self._account_id,
                normalized_id,
                reason="evidence_not_authoritative",
            )
        if evidence is None:
            return _unknown_opening_evidence(
                self._account_id,
                normalized_id,
                reason="no_authoritative_evidence",
            )
        return evidence

    def _invalidate(self) -> None:
        with self._lock:
            self._orders = ()
            self._opening_evidence = {}
            self._opening_evidence_authoritative = False
            self._fresh = False


def _parse_opening_execution_evidence(
    account_id: str,
    raw_surface: Any,
) -> tuple[dict[str, OpeningExecutionEvidence], bool]:
    if not isinstance(raw_surface, Mapping):
        return {}, False
    if raw_surface.get("authoritative") is not True:
        return {}, False
    raw_items = raw_surface.get("items")
    if not isinstance(raw_items, list):
        return {}, False

    parsed: dict[str, OpeningExecutionEvidence] = {}
    conflicts: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        item_account_id = str(raw_item.get("account_id") or "")
        client_order_id = str(raw_item.get("client_order_id") or "")
        state = str(raw_item.get("state") or "")
        if item_account_id != account_id:
            continue
        if not _DETERMINISTIC_OPENING_CLIENT_ORDER_ID.fullmatch(client_order_id):
            continue
        if state not in _OPENING_EXECUTION_STATES:
            continue
        evidence = OpeningExecutionEvidence(
            account_id=account_id,
            client_order_id=client_order_id,
            state=state,
            order_status=_optional_text(raw_item.get("order_status")),
            instrument_id=_optional_text(raw_item.get("instrument_id")),
            venue_order_id=_optional_text(raw_item.get("venue_order_id")),
            filled_quantity=_optional_text(raw_item.get("filled_quantity")),
            sources=_string_tuple(raw_item.get("sources")),
            observed_at=_optional_text(raw_item.get("observed_at")),
            reason=_optional_text(raw_item.get("reason")),
        )
        previous = parsed.get(client_order_id)
        if previous is not None and previous.state != evidence.state:
            conflicts.add(client_order_id)
            continue
        parsed[client_order_id] = evidence

    for client_order_id in conflicts:
        parsed[client_order_id] = _unknown_opening_evidence(
            account_id,
            client_order_id,
            reason="conflicting_authoritative_evidence",
        )
    return parsed, True


def _unknown_opening_evidence(
    account_id: str,
    client_order_id: str,
    *,
    reason: str,
) -> OpeningExecutionEvidence:
    return OpeningExecutionEvidence(
        account_id=account_id,
        client_order_id=client_order_id,
        state=OPENING_UNKNOWN,
        reason=reason,
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return text


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item))


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
    )
