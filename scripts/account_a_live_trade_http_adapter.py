#!/usr/bin/env python3
"""Control-plane-only adapter for the account-a live canary executor."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid5

ACCOUNT_ID = "account-a"
SYMBOL = "SOLUSDT"
EXCHANGE_SOURCE = "exchange"
NODE_SOURCE = "node"
EXCHANGE_HISTORY_SOURCES = frozenset(
    {
        "exchange_state.recent_order_history",
        "exchange_state.recent_algo_order_history",
    }
)
MAX_FUTURE_TIMESTAMP_SKEW_SECONDS = 30.0
TERMINAL_ORDER_STATUSES = {
    "CANCELED",
    "CANCELLED",
    "CLOSED",
    "DENIED",
    "DONE",
    "EXPIRED",
    "FILLED",
    "REJECTED",
}
SOFT_HTTP_STATUSES = {408, 425, 429}
IDENTITY_FIELDS = (
    "account_id",
    "symbol",
    "release_id",
    "image_digest",
    "config_sha256",
    "dependency_lock_sha256",
    "permit_id",
    "intent_id",
)
ACTION_NAMES = {
    "resume",
    "open",
    "observe",
    "cancel-open",
    "position",
    "close",
    "final-snapshot",
    "halt",
    "preflight",
}


class AdapterError(RuntimeError):
    pass


class SoftAdapterError(AdapterError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class Config:
    base_url: str
    risk_token: str
    node_token: str
    node_id: str
    http_timeout_seconds: float
    http_max_attempts: int
    poll_interval_seconds: float
    action_timeout_seconds: float
    exchange_freshness_seconds: float
    node_freshness_seconds: float

    @classmethod
    def from_environment(cls) -> Config:
        base_url = os.environ.get(
            "ACCOUNT_A_LIVE_TRADE_CONTROL_PLANE_URL",
            "http://127.0.0.1:8080",
        ).rstrip("/")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"}:
            raise AdapterError("control-plane URL must use http or https")
        if not parsed.hostname:
            raise AdapterError("control-plane URL requires a host")
        risk_token = _read_token_file(
            "ACCOUNT_A_LIVE_TRADE_RISK_ADMIN_TOKEN_FILE"
        )
        node_token = _read_token_file(
            "ACCOUNT_A_LIVE_TRADE_NODE_TOKEN_FILE"
        )
        node_id = _required_text(
            os.environ.get(
                "ACCOUNT_A_LIVE_TRADE_NODE_ID",
                "nautilus-node-account-a",
            ),
            "node id",
        )
        return cls(
            base_url=base_url,
            risk_token=risk_token,
            node_token=node_token,
            node_id=node_id,
            http_timeout_seconds=_positive_float(
                os.environ.get(
                    "ACCOUNT_A_LIVE_TRADE_HTTP_TIMEOUT_SECONDS",
                    "3",
                ),
                "HTTP timeout",
            ),
            http_max_attempts=_positive_int(
                os.environ.get(
                    "ACCOUNT_A_LIVE_TRADE_HTTP_MAX_ATTEMPTS",
                    "3",
                ),
                "HTTP max attempts",
            ),
            poll_interval_seconds=_non_negative_float(
                os.environ.get(
                    "ACCOUNT_A_LIVE_TRADE_POLL_INTERVAL_SECONDS",
                    "0.5",
                ),
                "poll interval",
            ),
            action_timeout_seconds=_positive_float(
                os.environ.get(
                    "ACCOUNT_A_LIVE_TRADE_ACTION_TIMEOUT_SECONDS",
                    "120",
                ),
                "action timeout",
            ),
            exchange_freshness_seconds=_positive_float(
                os.environ.get(
                    "ACCOUNT_A_LIVE_TRADE_FRESHNESS_SECONDS",
                    "120",
                ),
                "exchange freshness threshold",
            ),
            node_freshness_seconds=_positive_float(
                os.environ.get(
                    "ACCOUNT_A_LIVE_TRADE_NODE_FRESHNESS_SECONDS",
                    "10",
                ),
                "node freshness threshold",
            ),
        )


class ControlPlaneClient:
    def __init__(self, config: Config) -> None:
        self._config = config

    def risk_get(
        self,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            path,
            token=self._config.risk_token,
            query=query,
        )

    def risk_post(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            path,
            token=self._config.risk_token,
            body=body,
            request_id=request_id,
        )

    def node_get(
        self,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            path,
            token=self._config.node_token,
            query=query,
            node_headers=True,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        request_id: str = "",
        node_headers: bool = False,
    ) -> dict[str, Any]:
        url = f"{self._config.base_url}{path}"
        if query:
            encoded_query = urllib.parse.urlencode(
                {
                    key: str(value)
                    for key, value in query.items()
                    if value is not None
                }
            )
            url = f"{url}?{encoded_query}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if request_id:
            headers["X-Request-ID"] = request_id
        if node_headers:
            headers["X-Node-ID"] = self._config.node_id
            headers["X-Account-ID"] = ACCOUNT_ID
        data = None
        if body is not None:
            data = _canonical_json_bytes(dict(body))
            headers["Content-Type"] = "application/json"

        last_error: BaseException | None = None
        for attempt in range(1, self._config.http_max_attempts + 1):
            request = urllib.request.Request(
                url,
                data=data,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self._config.http_timeout_seconds,
                ) as response:
                    raw = response.read()
                    return _decode_object(raw, f"{method} {path}")
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                detail = _http_error_detail(raw, exc.reason)
                if exc.code == 409:
                    raise AdapterError(
                        f"ownership/fencing conflict: HTTP 409 {detail}"
                    ) from exc
                if _is_soft_http_status(exc.code):
                    last_error = SoftAdapterError(
                        f"HTTP {exc.code} {detail}",
                        code=f"HTTP_{exc.code}",
                        status_code=exc.code,
                    )
                else:
                    raise AdapterError(
                        f"control-plane HTTP {exc.code}: {detail}"
                    ) from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                last_error = SoftAdapterError(
                    f"HTTP timeout or transport failure: {exc}",
                    code="HTTP_TIMEOUT",
                )
            if attempt < self._config.http_max_attempts:
                time.sleep(self._config.poll_interval_seconds)
        if last_error is not None:
            raise last_error
        raise AdapterError(f"control-plane request failed: {method} {path}")


class AccountALiveTradeHttpAdapter:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._client = ControlPlaneClient(config)

    def dispatch(
        self,
        action: str,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        _validate_base_request(request)
        if action == "resume":
            return self._set_trading_state(
                request,
                command="RESUME",
                expected_states={"ACTIVE"},
            )
        if action == "halt":
            return self._set_trading_state(
                request,
                command="HALT",
                expected_states={"HALTED", "STOPPED"},
            )
        if action == "open":
            return self._submit_open(request)
        if action == "observe":
            return self._observe(request)
        if action == "cancel-open":
            return self._cancel_open(request)
        if action == "position":
            return self._position(request)
        if action == "close":
            return self._submit_close(request)
        if action == "final-snapshot":
            return self._final_snapshot(request)
        if action == "preflight":
            return self._preflight(request)
        raise AdapterError(f"unsupported action: {action}")

    def _set_trading_state(
        self,
        request: Mapping[str, Any],
        *,
        command: str,
        expected_states: set[str],
    ) -> dict[str, Any]:
        side_effect_id = _required_text(
            request.get("side_effect_id"),
            "side_effect_id",
        )
        command_body = {
            "type": command,
            "reason": (
                f"account-a SOLUSDT canary {command.lower()} "
                f"permit={request['permit_id']}"
            ),
            "confirm": True,
            "request_id": side_effect_id,
            "idempotency_key": side_effect_id,
            "target_nodes": [self._config.node_id],
            "scope": {
                "account_id": ACCOUNT_ID,
                "symbol": SYMBOL,
                "executor_identity": _identity(request),
                "permit_id": request["permit_id"],
                "max_round_trips": request.get("max_round_trips", 1),
            },
        }
        self._client.risk_post(
            "/v1/commands",
            command_body,
            request_id=side_effect_id,
        )
        node = self._wait_for_node_state(
            expected_states,
            request=request,
        )
        trading_state = str(
            node.get("trading_state")
            or node.get("status")
            or ""
        ).upper()
        action = command.lower()
        payload = {
            **_identity(request),
            "accepted": True,
            "action": action,
            "status": "READY",
            "trading_state": trading_state,
            "source": NODE_SOURCE,
            "observed_at": _now(),
            "side_effect_id": side_effect_id,
            "node_snapshot": _redacted_node_snapshot(node),
        }
        return _with_evidence(payload)

    def _wait_for_node_state(
        self,
        expected_states: set[str],
        *,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        deadline = self._deadline(request)
        last_state = ""
        while time.monotonic() < deadline:
            response = self._client.risk_get("/v1/nodes")
            nodes = response.get("nodes")
            if not isinstance(nodes, list):
                raise AdapterError("node list response is invalid")
            for raw_node in nodes:
                if not isinstance(raw_node, dict):
                    continue
                if raw_node.get("node_id") != self._config.node_id:
                    continue
                if raw_node.get("account_id") != ACCOUNT_ID:
                    raise AdapterError(
                        "ownership/fencing conflict: node account mismatch"
                    )
                last_state = str(
                    raw_node.get("trading_state")
                    or raw_node.get("status")
                    or ""
                ).upper()
                if last_state in expected_states:
                    return raw_node
            time.sleep(self._config.poll_interval_seconds)
        raise SoftAdapterError(
            f"node state poll timeout; last_state={last_state or 'missing'}",
            code="HTTP_TIMEOUT",
        )

    def _submit_open(
        self,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        intent_id = _canonical_uuid(request.get("intent_id"), "intent_id")
        client_order_id = _required_text(
            request.get("client_order_id"),
            "client_order_id",
        )
        expected_client_order_id = f"B{UUID(intent_id).hex}01"
        if client_order_id != expected_client_order_id:
            raise AdapterError("OPEN client order ID does not bind intent")
        if str(request.get("order_type") or "").upper() != "LIMIT":
            raise AdapterError("OPEN order_type must be LIMIT")
        if str(request.get("time_in_force") or "").upper() != "IOC":
            raise AdapterError("OPEN time_in_force must be IOC")
        side = str(request.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            raise AdapterError("OPEN side must be BUY or SELL")
        quantity = _positive_decimal(request.get("quantity"), "quantity")
        limit_price = _positive_decimal(
            request.get("limit_price_usdt"),
            "limit_price_usdt",
        )
        notional = quantity * limit_price
        max_notional = _positive_decimal(
            request.get("max_actual_open_notional_usdt"),
            "max_actual_open_notional_usdt",
        )
        if notional > max_notional or notional > Decimal(12):
            raise AdapterError("OPEN requested notional exceeds 12 USDT")
        side_name = "long"
        if side == "SELL":
            side_name = "short"
        side_effect_id = _required_text(
            request.get("side_effect_id"),
            "side_effect_id",
        )
        body = {
            "intent_id": intent_id,
            "action": "open_position",
            "account_id": ACCOUNT_ID,
            "symbol": SYMBOL,
            "side": side_name,
            "entry": {
                "type": "limit",
                "price": _decimal_text(limit_price),
                "time_in_force": "IOC",
            },
            "notional_usdt": _decimal_text(notional),
            "client_ref": _open_client_ref(intent_id),
            "reason": (
                "account-a availability canary LIMIT IOC "
                f"permit={request['permit_id']}"
            ),
            "source": "control-plane",
            "valid_seconds": 300,
        }
        response = self._client.risk_post(
            "/v1/operator/orders",
            body,
            request_id=side_effect_id,
        )
        if str(response.get("intent_id") or "") != intent_id:
            raise AdapterError("OPEN response intent_id mismatch")
        payload = {
            **_identity(request),
            "accepted": True,
            "action": "open",
            "client_order_id": client_order_id,
            "side_effect_id": side_effect_id,
            "operator_response": _compact_operator_response(response),
        }
        return _with_evidence(payload)

    def _observe(
        self,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        open_client_order_id = _required_text(
            request.get("open_client_order_id"),
            "open_client_order_id",
        )
        deadline = self._deadline(request)
        exchange_not_before = _exchange_not_before(request)
        status = self._operator_status(str(request["intent_id"]))
        mirror = self._exchange_state_after(
            (open_client_order_id,),
            not_before=exchange_not_before,
            deadline=deadline,
        )
        summary = _execution_summary(
            status,
            expected_client_order_id=open_client_order_id,
        )
        evidence_state = _opening_evidence_state(
            mirror,
            open_client_order_id,
            not_before=exchange_not_before,
        )
        position = _target_position(mirror)
        open_status = _open_status(summary, evidence_state)
        mark_price = position["mark_price"]
        if mark_price <= 0:
            mark_price = summary["average_fill_price"]
        fees = summary["fees"]
        unrealized_pnl = position["unrealized_pnl"]
        net_pnl = summary["realized_pnl"] + unrealized_pnl - fees
        cumulative_loss = max(Decimal(0), -net_pnl)
        fetched_at = _mirror_fetched_at(mirror)
        node_snapshot, health_warnings = (
            self._optional_node_snapshot()
        )
        loss_monitor = _loss_monitor_evidence(
            mirror,
            node_snapshot=node_snapshot,
            exchange_freshness_seconds=(
                self._config.exchange_freshness_seconds
            ),
            node_freshness_seconds=(
                self._config.node_freshness_seconds
            ),
        )
        payload = {
            **_identity(request),
            "open_client_order_id": open_client_order_id,
            "open_status": open_status,
            "filled_quantity": _decimal_text(
                summary["filled_quantity"]
            ),
            "average_fill_price_usdt": _decimal_text(
                summary["average_fill_price"]
            ),
            "cumulative_net_loss_usdt": _decimal_text(
                cumulative_loss
            ),
            "mark_fresh": loss_monitor["mirror_fresh"],
            "loss_monitor_healthy": loss_monitor["healthy"],
            "observed_at": _now(),
            "mark_at": fetched_at,
            "loss_monitor_at": loss_monitor["observed_at"],
            "loss_monitor_source": loss_monitor["source"],
            "loss_monitor_evidence": loss_monitor["evidence"],
            "health_evidence_degraded": bool(health_warnings),
            "warnings": health_warnings,
            "exchange_evidence_state": evidence_state,
            "fees_usdt": _decimal_text(fees),
            "unrealized_pnl_usdt": _decimal_text(unrealized_pnl),
            "mark_price_usdt": _decimal_text(mark_price),
        }
        if node_snapshot is not False:
            payload["node_snapshot"] = node_snapshot
        return _with_evidence(payload)

    def _cancel_open(
        self,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        open_client_order_id = _required_text(
            request.get("open_client_order_id"),
            "open_client_order_id",
        )
        open_intent_id = str(request["intent_id"])
        cancel_intent_id = str(
            uuid5(
                UUID(open_intent_id),
                "trader-v3/account-a/SOLUSDT/cancel-open",
            )
        )
        side_effect_id = _required_text(
            request.get("side_effect_id"),
            "side_effect_id",
        )
        body = {
            "intent_id": cancel_intent_id,
            "action": "cancel_order",
            "account_id": ACCOUNT_ID,
            "symbol": SYMBOL,
            "client_order_id": open_client_order_id,
            "client_ref": f"account-a-canary-cancel-{cancel_intent_id}",
            "channel": "operator",
            "entry_ref": _open_client_ref(open_intent_id),
            "reason": (
                "account-a canary cleanup cancel OPEN "
                f"permit={request['permit_id']}"
            ),
            "source": "control-plane",
            "valid_seconds": 300,
        }
        response = self._client.risk_post(
            "/v1/operator/orders",
            body,
            request_id=side_effect_id,
        )
        payload = {
            **_identity(request),
            "accepted": True,
            "action": "cancel-open",
            "client_order_id": open_client_order_id,
            "side_effect_id": side_effect_id,
            "operator_response": _compact_operator_response(response),
        }
        return _with_evidence(payload)

    def _position(
        self,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        mirror = self._exchange_state_after(
            (),
            not_before=_exchange_not_before(request),
            deadline=self._deadline(request),
        )
        position = _target_position(mirror)
        payload = {
            **_identity(request),
            "position_side": position["side"],
            "position_quantity": _decimal_text(position["quantity"]),
            "source": EXCHANGE_SOURCE,
            "fetched_at": _mirror_fetched_at(mirror),
            "mark_price_usdt": _decimal_text(position["mark_price"]),
            "unrealized_pnl_usdt": _decimal_text(
                position["unrealized_pnl"]
            ),
        }
        return _with_evidence(payload)

    def _submit_close(
        self,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        close_intent_id = _canonical_uuid(
            request.get("intent_id"),
            "close intent_id",
        )
        client_order_id = _required_text(
            request.get("client_order_id"),
            "close client_order_id",
        )
        expected_client_order_id = f"B{UUID(close_intent_id).hex}01"
        if client_order_id != expected_client_order_id:
            raise AdapterError("CLOSE client order ID does not bind intent")
        quantity = _positive_decimal(request.get("quantity"), "quantity")
        if request.get("reduce_only") is not True:
            raise AdapterError("CLOSE requires reduce_only=true")
        side = str(request.get("side") or "").upper()
        position_side = str(
            request.get("position_side") or ""
        ).upper()
        expected_side = "SELL"
        if position_side == "SHORT":
            expected_side = "BUY"
        if position_side not in {"LONG", "SHORT"}:
            raise AdapterError("CLOSE position_side must be LONG or SHORT")
        if side != expected_side:
            raise AdapterError("CLOSE side does not reduce position")
        side_effect_id = _required_text(
            request.get("side_effect_id"),
            "side_effect_id",
        )
        open_intent_id = _canonical_uuid(
            request.get("open_intent_id"),
            "open intent_id",
        )
        if _close_intent_id(open_intent_id) != close_intent_id:
            raise AdapterError(
                "CLOSE intent does not bind the original OPEN intent"
            )
        body = {
            "intent_id": close_intent_id,
            "action": "partial_close",
            "account_id": ACCOUNT_ID,
            "symbol": SYMBOL,
            "position_side": position_side.lower(),
            "quantity": _decimal_text(quantity),
            "client_ref": f"account-a-canary-close-{close_intent_id}",
            "channel": "operator",
            "entry_ref": _open_client_ref(open_intent_id),
            "reason": (
                "account-a canary exact reduce-only close "
                f"permit={request['permit_id']}"
            ),
            "source": "control-plane",
            "valid_seconds": 300,
        }
        close_dispatched_at = datetime.now(timezone.utc)
        requested_not_before = _exchange_not_before(request)
        exchange_not_before = close_dispatched_at
        if (
            requested_not_before is not False
            and requested_not_before > exchange_not_before
        ):
            exchange_not_before = requested_not_before
        response = self._client.risk_post(
            "/v1/operator/orders",
            body,
            request_id=side_effect_id,
        )
        if str(response.get("intent_id") or "") != close_intent_id:
            raise AdapterError("CLOSE response intent_id mismatch")
        deadline = self._deadline(request)
        last_reason = "close evidence pending"
        while time.monotonic() < deadline:
            projection_warning = ""
            try:
                status = self._operator_status(close_intent_id)
                summary = _execution_summary(
                    status,
                    expected_client_order_id=client_order_id,
                )
            except AdapterError as exc:
                if _is_ownership_error(exc):
                    raise
                projection_warning = _enrichment_warning(
                    "close operator projection",
                    exc,
                )
                summary = _empty_execution_summary()
            mirror = self._exchange_state_after(
                (client_order_id,),
                not_before=exchange_not_before,
                deadline=deadline,
            )
            position = _target_position(mirror)
            venue_quantity = _venue_exact_fill_quantity(
                mirror,
                client_order_id,
                not_before=exchange_not_before,
            )
            venue_exact_fill = venue_quantity == quantity
            if (
                position["quantity"] == 0
                and venue_exact_fill
            ):
                warnings = []
                if projection_warning:
                    warnings.append(projection_warning)
                elif summary["filled_quantity"] != quantity:
                    warnings.append(
                        "close operator projection degraded: "
                        "filled quantity differs from exchange history"
                    )
                payload = {
                    **_identity(
                        request,
                        intent_id=close_intent_id,
                    ),
                    "accepted": True,
                    "action": "close",
                    "client_order_id": client_order_id,
                    "filled_quantity": _decimal_text(quantity),
                    "side_effect_id": side_effect_id,
                    "source": EXCHANGE_SOURCE,
                    "fetched_at": _mirror_fetched_at(mirror),
                    "exchange_evidence_state": "confirmed_executed",
                    "close_proof_source": "exchange_history",
                    "enrichment_degraded": bool(warnings),
                    "warnings": warnings,
                }
                return _with_evidence(payload)
            last_reason = (
                "position remains open or exact fill evidence is pending"
            )
            time.sleep(self._config.poll_interval_seconds)
        raise AdapterError(f"exact CLOSE unproven: {last_reason}")

    def _final_snapshot(
        self,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        mirror = self._exchange_state_after(
            (),
            not_before=_exchange_not_before(request),
            deadline=self._deadline(request),
        )
        exchange_payload = _exchange_payload(mirror)
        position = _target_position(mirror)
        regular_orders = _target_orders(
            exchange_payload.get("open_orders"),
        )
        algo_orders = _target_orders(
            exchange_payload.get("algo_orders"),
        )
        baseline = _portfolio_baseline_sha256(exchange_payload)
        open_intent_id = str(request["intent_id"])
        close_intent_id = _close_intent_id(open_intent_id)
        warnings: list[str] = []
        open_status, open_summary = (
            self._operator_execution_enrichment(
                open_intent_id,
                client_order_id=_open_client_order_id(
                    open_intent_id
                ),
                label="open",
                warnings=warnings,
            )
        )
        _close_status, close_summary = (
            self._operator_execution_enrichment(
                close_intent_id,
                client_order_id=_open_client_order_id(
                    close_intent_id
                ),
                label="close",
                warnings=warnings,
            )
        )
        fees = open_summary["fees"] + close_summary["fees"]
        gross_pnl = (
            open_summary["realized_pnl"]
            + close_summary["realized_pnl"]
        )
        if gross_pnl == 0:
            gross_pnl = _derived_round_trip_pnl(
                open_status,
                open_summary,
                close_summary,
            )
        net_pnl = gross_pnl - fees
        cumulative_loss = max(Decimal(0), -net_pnl)
        payload = {
            **_identity(request),
            "target_symbol_flat": position["quantity"] == 0,
            "target_symbol_regular_orders_zero": not regular_orders,
            "target_symbol_algo_orders_zero": not algo_orders,
            "position_quantity": _decimal_text(position["quantity"]),
            "non_target_portfolio_baseline_sha256": baseline,
            "gross_pnl_usdt": _decimal_text(gross_pnl),
            "fees_usdt": _decimal_text(fees),
            "net_pnl_usdt": _decimal_text(net_pnl),
            "cumulative_net_loss_usdt": _decimal_text(
                cumulative_loss
            ),
            "source": EXCHANGE_SOURCE,
            "fetched_at": _mirror_fetched_at(mirror),
            "regular_order_count": len(regular_orders),
            "algo_order_count": len(algo_orders),
            "enrichment_degraded": bool(warnings),
            "financial_proof_complete": not warnings,
            "warnings": warnings,
        }
        return _with_evidence(payload)

    def _operator_execution_enrichment(
        self,
        intent_id: str,
        *,
        client_order_id: str,
        label: str,
        warnings: list[str],
    ) -> tuple[dict[str, Any], dict[str, Decimal | str]]:
        try:
            status = self._operator_status(intent_id)
            summary = _execution_summary(
                status,
                expected_client_order_id=client_order_id,
            )
            return status, summary
        except AdapterError as exc:
            if _is_ownership_error(exc):
                raise
            warnings.append(
                _enrichment_warning(
                    f"{label} operator projection",
                    exc,
                )
            )
            return {}, _empty_execution_summary()

    def _preflight(
        self,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        mirror = self._exchange_state(())
        exchange_payload = _exchange_payload(mirror)
        position = _target_position(mirror)
        nodes = self._client.risk_get("/v1/nodes")
        node = _find_node(nodes, self._config.node_id)
        if node.get("account_id") != ACCOUNT_ID:
            raise AdapterError(
                "ownership/fencing conflict: node account mismatch"
            )
        payload = {
            **_identity(request),
            "action": "preflight",
            "source": EXCHANGE_SOURCE,
            "fetched_at": _mirror_fetched_at(mirror),
            "mirror_stale": mirror.get("stale") is True,
            "target_position_side": position["side"],
            "target_position_quantity": _decimal_text(
                position["quantity"]
            ),
            "target_mark_price_usdt": _decimal_text(
                position["mark_price"]
            ),
            "target_regular_order_count": len(
                _target_orders(exchange_payload.get("open_orders"))
            ),
            "target_algo_order_count": len(
                _target_orders(exchange_payload.get("algo_orders"))
            ),
            "non_target_portfolio_baseline_sha256": (
                _portfolio_baseline_sha256(exchange_payload)
            ),
            "node_snapshot": _redacted_node_snapshot(node),
        }
        return _with_evidence(payload)

    def _exchange_state(
        self,
        client_order_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"account_id": ACCOUNT_ID}
        if client_order_ids:
            query["client_order_ids"] = ",".join(client_order_ids)
        response = self._client.node_get(
            f"/v1/nodes/{self._config.node_id}/exchange-state",
            query=query,
        )
        if response.get("account_id") != ACCOUNT_ID:
            raise AdapterError(
                "ownership/fencing conflict: exchange account mismatch"
            )
        _exchange_payload(response)
        return response

    def _exchange_state_after(
        self,
        client_order_ids: tuple[str, ...],
        *,
        not_before: datetime | bool,
        deadline: float,
    ) -> dict[str, Any]:
        last_code = "EXCHANGE_MIRROR_LAG"
        last_reason = (
            "exchange mirror did not advance past the required action boundary"
        )
        while True:
            mirror = self._exchange_state(client_order_ids)
            fetched_at = _mirror_fetched_datetime(mirror)
            mirror_stale = mirror.get("stale") is True
            mirror_future = _timestamp_is_far_future(fetched_at)
            causal_sample = (
                not_before is False or fetched_at >= not_before
            )
            if causal_sample and not mirror_stale and not mirror_future:
                return mirror
            if mirror_stale:
                last_code = "EXCHANGE_MIRROR_STALE"
                last_reason = "exchange mirror remained stale"
            elif mirror_future:
                last_reason = (
                    "exchange mirror timestamp exceeds allowed clock skew"
                )
            if time.monotonic() >= deadline:
                raise SoftAdapterError(
                    last_reason,
                    code=last_code,
                )
            time.sleep(self._config.poll_interval_seconds)

    def _operator_status(
        self,
        intent_id: str,
    ) -> dict[str, Any]:
        return self._client.risk_get(
            f"/v1/operator/orders/{intent_id}"
        )

    def _optional_node_snapshot(
        self,
    ) -> tuple[dict[str, Any] | bool, list[str]]:
        warnings: list[str] = []
        try:
            response = self._client.risk_get("/v1/nodes")
            node = _find_node(response, self._config.node_id)
        except AdapterError as exc:
            if _is_ownership_error(exc):
                raise
            warnings.append(
                _enrichment_warning("node health evidence", exc)
            )
            return False, warnings
        if node.get("account_id") != ACCOUNT_ID:
            raise AdapterError(
                "ownership/fencing conflict: node account mismatch"
            )
        return _redacted_node_snapshot(node), warnings

    def _deadline(self, request: Mapping[str, Any]) -> float:
        timeout = self._config.action_timeout_seconds
        requested = request.get("hard_timeout_seconds")
        if requested is not None:
            timeout = min(
                timeout,
                _positive_float(requested, "hard timeout"),
            )
        return time.monotonic() + timeout


def _validate_base_request(request: Mapping[str, Any]) -> None:
    if request.get("account_id") != ACCOUNT_ID:
        raise AdapterError("adapter account must be account-a")
    if str(request.get("symbol") or "").upper() != SYMBOL:
        raise AdapterError("adapter symbol must be SOLUSDT")
    for field_name in IDENTITY_FIELDS:
        _required_text(request.get(field_name), field_name)
    _canonical_uuid(request.get("intent_id"), "intent_id")


def _identity(
    request: Mapping[str, Any],
    *,
    intent_id: str = "",
) -> dict[str, str]:
    payload = {
        field_name: str(request[field_name])
        for field_name in IDENTITY_FIELDS
    }
    if intent_id:
        payload["intent_id"] = intent_id
    return payload


def _exchange_payload(mirror: Mapping[str, Any]) -> dict[str, Any]:
    payload = mirror.get("payload")
    if not isinstance(payload, dict):
        raise AdapterError("exchange mirror payload is invalid")
    return payload


def _mirror_fetched_at(mirror: Mapping[str, Any]) -> str:
    payload = _exchange_payload(mirror)
    fetched_at = payload.get("fetched_at") or mirror.get("updated_at")
    return _timestamp_text(fetched_at, "exchange mirror fetched_at")


def _mirror_fetched_datetime(
    mirror: Mapping[str, Any],
) -> datetime:
    return _timestamp_datetime(_mirror_fetched_at(mirror))


def _exchange_not_before(
    request: Mapping[str, Any],
) -> datetime | bool:
    raw_value = request.get("exchange_not_before")
    if raw_value is None or raw_value == "":
        return False
    return _timestamp_datetime(raw_value)


def _target_position(mirror: Mapping[str, Any]) -> dict[str, Any]:
    payload = _exchange_payload(mirror)
    rows = payload.get("positions")
    if not isinstance(rows, list):
        raise AdapterError("exchange positions are invalid")
    matches: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("symbol") or "").upper() != SYMBOL:
            continue
        quantity = _decimal(
            raw.get("position_amt"),
            "position_amt",
        )
        if quantity == 0:
            continue
        matches.append(raw)
    if len(matches) > 1:
        raise AdapterError("target position is ambiguous")
    if not matches:
        return {
            "side": "FLAT",
            "quantity": Decimal(0),
            "mark_price": Decimal(0),
            "unrealized_pnl": Decimal(0),
        }
    row = matches[0]
    signed_quantity = _decimal(
        row.get("position_amt"),
        "position_amt",
    )
    side = str(row.get("position_side") or "").upper()
    if side not in {"LONG", "SHORT"}:
        side = "LONG"
        if signed_quantity < 0:
            side = "SHORT"
    return {
        "side": side,
        "quantity": abs(signed_quantity),
        "mark_price": _non_negative_decimal(
            row.get("mark_price"),
            "mark_price",
        ),
        "unrealized_pnl": _decimal(
            row.get("unrealized_pnl"),
            "unrealized_pnl",
        ),
    }


def _target_orders(raw_rows: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_rows, list):
        raise AdapterError("exchange orders are invalid")
    return [
        row
        for row in raw_rows
        if isinstance(row, dict)
        and str(row.get("symbol") or "").upper() == SYMBOL
    ]


def _opening_evidence_state(
    mirror: Mapping[str, Any],
    client_order_id: str,
    *,
    not_before: datetime | bool = False,
) -> str:
    item = _opening_evidence_item(
        mirror,
        client_order_id,
        not_before=not_before,
    )
    if item is False:
        return "unknown"
    return str(item.get("state") or "unknown")


def _opening_evidence_item(
    mirror: Mapping[str, Any],
    client_order_id: str,
    *,
    not_before: datetime | bool,
) -> Mapping[str, Any] | bool:
    surface = mirror.get("opening_execution_evidence")
    if not isinstance(surface, dict):
        return False
    if surface.get("authoritative") is not True:
        return False
    items = surface.get("items")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("account_id") != ACCOUNT_ID:
            continue
        if item.get("client_order_id") != client_order_id:
            continue
        observed_at = item.get("observed_at")
        if observed_at is None or observed_at == "":
            continue
        observed_datetime = _timestamp_datetime(observed_at)
        if _timestamp_is_far_future(observed_datetime):
            continue
        if not_before is not False and observed_datetime < not_before:
            continue
        return item
    return False


def _venue_exact_fill_quantity(
    mirror: Mapping[str, Any],
    client_order_id: str,
    *,
    not_before: datetime,
) -> Decimal | bool:
    item = _opening_evidence_item(
        mirror,
        client_order_id,
        not_before=not_before,
    )
    if item is False:
        return False
    source_evidence = item.get("source_evidence")
    if not isinstance(source_evidence, list):
        return False
    quantities: set[Decimal] = set()
    venue_order_ids: set[str] = set()
    for proof in source_evidence:
        if not isinstance(proof, Mapping):
            raise AdapterError(
                "source-specific opening evidence is invalid"
            )
        if proof.get("source") not in EXCHANGE_HISTORY_SOURCES:
            continue
        if (
            proof.get("account_id") != ACCOUNT_ID
            or proof.get("client_order_id") != client_order_id
            or proof.get("state") != "confirmed_executed"
        ):
            raise AdapterError(
                "exchange history evidence conflicts with CLOSE identity"
            )
        instrument_id = str(
            proof.get("instrument_id") or ""
        ).upper()
        if _opening_instrument_symbol(instrument_id) != SYMBOL:
            raise AdapterError(
                "exchange history evidence conflicts with CLOSE symbol"
            )
        observed_at = proof.get("observed_at")
        if observed_at is None or observed_at == "":
            raise AdapterError(
                "exchange history evidence lacks an observation time"
            )
        observed_datetime = _timestamp_datetime(observed_at)
        if _timestamp_is_far_future(observed_datetime):
            raise AdapterError(
                "exchange history evidence exceeds allowed clock skew"
            )
        if observed_datetime < not_before:
            raise AdapterError(
                "exchange history evidence predates CLOSE dispatch"
            )
        quantity = _non_negative_decimal(
            proof.get("filled_quantity"),
            "exchange history filled_quantity",
        )
        if quantity <= 0:
            raise AdapterError(
                "exchange history evidence has no CLOSE fill"
            )
        quantities.add(quantity)
        venue_order_id = str(
            proof.get("venue_order_id") or ""
        ).strip()
        if venue_order_id:
            venue_order_ids.add(venue_order_id)
    if not quantities:
        return False
    if len(quantities) != 1 or len(venue_order_ids) > 1:
        raise AdapterError(
            "exchange history CLOSE evidence is ambiguous"
        )
    return next(iter(quantities))


def _opening_instrument_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "-PERP." in text:
        text = text.split("-", 1)[0]
    return text


def _execution_summary(
    status: Mapping[str, Any],
    *,
    expected_client_order_id: str,
) -> dict[str, Decimal | str]:
    filled_quantity = Decimal(0)
    average_fill_price = Decimal(0)
    order_status = "UNKNOWN"
    orders = status.get("orders")
    if isinstance(orders, list):
        for raw in orders:
            if not isinstance(raw, dict):
                continue
            client_order_id = str(
                raw.get("client_order_id") or ""
            )
            if (
                expected_client_order_id
                and client_order_id != expected_client_order_id
            ):
                continue
            candidate_quantity = _non_negative_decimal(
                raw.get("filled_quantity"),
                "filled_quantity",
            )
            if candidate_quantity >= filled_quantity:
                filled_quantity = candidate_quantity
                average_fill_price = _non_negative_decimal(
                    raw.get("average_fill_price"),
                    "average_fill_price",
                )
                order_status = str(
                    raw.get("status") or "UNKNOWN"
                ).upper()
    fees = Decimal(0)
    realized_pnl = Decimal(0)
    weighted_quote = Decimal(0)
    weighted_quantity = Decimal(0)
    events = status.get("execution_events")
    if isinstance(events, list):
        seen_trade_ids: set[str] = set()
        for raw in events:
            if not isinstance(raw, dict):
                continue
            event_client_order_id = str(
                raw.get("client_order_id") or ""
            )
            if (
                expected_client_order_id
                and event_client_order_id != expected_client_order_id
            ):
                continue
            payload = raw.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            fees += _asset_amount(payload.get("commission"))
            realized_pnl += _asset_amount(
                payload.get("realized_pnl")
            )
            event_type = str(raw.get("event_type") or "")
            if event_type != "OrderFilled":
                continue
            trade_id = str(raw.get("trade_id") or "")
            if trade_id and trade_id in seen_trade_ids:
                continue
            if trade_id:
                seen_trade_ids.add(trade_id)
            last_quantity = _first_decimal(
                payload,
                ("last_qty", "quantity", "qty"),
            )
            last_price = _first_decimal(
                payload,
                ("last_px", "price", "avg_px"),
            )
            if last_quantity > 0 and last_price > 0:
                weighted_quantity += last_quantity
                weighted_quote += last_quantity * last_price
    if filled_quantity == 0 and weighted_quantity > 0:
        filled_quantity = weighted_quantity
    if average_fill_price == 0 and weighted_quantity > 0:
        average_fill_price = weighted_quote / weighted_quantity
    return {
        "filled_quantity": filled_quantity,
        "average_fill_price": average_fill_price,
        "fees": fees,
        "realized_pnl": realized_pnl,
        "status": order_status,
    }


def _empty_execution_summary() -> dict[str, Decimal | str]:
    return {
        "filled_quantity": Decimal(0),
        "average_fill_price": Decimal(0),
        "fees": Decimal(0),
        "realized_pnl": Decimal(0),
        "status": "UNKNOWN",
    }


def _open_status(
    summary: Mapping[str, Any],
    evidence_state: str,
) -> str:
    filled_quantity = Decimal(str(summary["filled_quantity"]))
    order_status = str(summary["status"]).upper()
    if filled_quantity > 0:
        if order_status in TERMINAL_ORDER_STATUSES:
            return order_status
        return "FILLED"
    if evidence_state == "definitively_absent":
        return "REJECTED"
    if order_status in TERMINAL_ORDER_STATUSES:
        return order_status
    if evidence_state == "confirmed_executed":
        return "FILLED"
    return "UNKNOWN"


def _portfolio_baseline_sha256(
    exchange_payload: Mapping[str, Any],
) -> str:
    baseline = {
        "positions": _non_target_rows(
            exchange_payload.get("positions"),
        ),
        "open_orders": _non_target_rows(
            exchange_payload.get("open_orders"),
        ),
        "algo_orders": _non_target_rows(
            exchange_payload.get("algo_orders"),
        ),
    }
    return hashlib.sha256(_canonical_json_bytes(baseline)).hexdigest()


def _non_target_rows(raw_rows: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_rows, list):
        raise AdapterError("portfolio collection is invalid")
    rows = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or "").upper()
        if symbol == SYMBOL:
            continue
        rows.append(_json_safe(raw))
    rows.sort(
        key=lambda row: json.dumps(
            row,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    )
    return rows


def _derived_round_trip_pnl(
    open_status: Mapping[str, Any],
    open_summary: Mapping[str, Any],
    close_summary: Mapping[str, Any],
) -> Decimal:
    open_quantity = Decimal(str(open_summary["filled_quantity"]))
    close_quantity = Decimal(str(close_summary["filled_quantity"]))
    quantity = min(open_quantity, close_quantity)
    if quantity <= 0:
        return Decimal(0)
    open_price = Decimal(str(open_summary["average_fill_price"]))
    close_price = Decimal(str(close_summary["average_fill_price"]))
    if open_price <= 0 or close_price <= 0:
        return Decimal(0)
    side = ""
    intent = open_status.get("intent")
    if isinstance(intent, dict):
        plan = intent.get("order_plan")
        if isinstance(plan, dict):
            side = str(plan.get("side") or "").lower()
    if side == "short":
        return (open_price - close_price) * quantity
    return (close_price - open_price) * quantity


def _close_intent_id(open_intent_id: str) -> str:
    return str(
        uuid5(
            UUID(open_intent_id),
            "trader-v3/account-a/SOLUSDT/close",
        )
    )


def _open_client_order_id(intent_id: str) -> str:
    return f"B{UUID(intent_id).hex}01"


def _open_client_ref(intent_id: str) -> str:
    return f"account-a-canary-open-{intent_id}"


def _compact_operator_response(
    response: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        key: response.get(key)
        for key in (
            "intent_id",
            "status",
            "replay",
            "account_id",
            "instrument_id",
            "action",
            "valid_until",
        )
        if key in response
    }


def _find_node(
    payload: Mapping[str, Any],
    node_id: str,
) -> dict[str, Any]:
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        raise AdapterError("node response is invalid")
    for node in nodes:
        if isinstance(node, dict) and node.get("node_id") == node_id:
            return node
    raise AdapterError(f"node is missing: {node_id}")


def _redacted_node_snapshot(
    node: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        key: node.get(key)
        for key in (
            "node_id",
            "account_id",
            "trading_state",
            "status",
            "readiness",
            "last_heartbeat_at",
            "projection_lag_ms",
            "reconciliation_state",
            "version",
            "process_liveness",
            "actor_tick_at",
            "loss_monitor_healthy",
            "loss_monitor_at",
        )
        if key in node
    }


def _loss_monitor_evidence(
    mirror: Mapping[str, Any],
    *,
    node_snapshot: Mapping[str, Any] | bool,
    exchange_freshness_seconds: float,
    node_freshness_seconds: float,
) -> dict[str, Any]:
    fetched_at = _mirror_fetched_at(mirror)
    mirror_stale = mirror.get("stale") is True
    mirror_fresh = (
        not mirror_stale
        and _timestamp_is_fresh(
            fetched_at,
            freshness_seconds=exchange_freshness_seconds,
        )
    )
    healthy = mirror_fresh
    progress_timestamps = [fetched_at]
    source_parts = ["exchange_mirror"]
    evidence: dict[str, Any] = {
        "exchange_mirror_fetched_at": fetched_at,
        "exchange_mirror_stale": mirror_stale,
        "node_snapshot_available": node_snapshot is not False,
    }
    if isinstance(node_snapshot, Mapping):
        node_progress_available = False
        node_progress_fields = (
            ("actor_tick_at", "actor_tick_at"),
            ("loss_monitor_at", "node_loss_monitor_at"),
        )
        for node_field, evidence_field in node_progress_fields:
            value = node_snapshot.get(node_field)
            if value is None or value == "":
                continue
            timestamp = _timestamp_text(
                value,
                f"node {node_field}",
            )
            evidence[evidence_field] = timestamp
            progress_timestamps.append(timestamp)
            node_progress_available = True
            if not _timestamp_is_fresh(
                timestamp,
                freshness_seconds=node_freshness_seconds,
            ):
                healthy = False
        if node_snapshot.get("loss_monitor_healthy") is False:
            healthy = False
        if node_snapshot.get("process_liveness") is False:
            healthy = False
        if node_progress_available:
            source_parts.append("node")
    observed_at = min(
        progress_timestamps,
        key=_timestamp_datetime,
    )
    return {
        "healthy": healthy,
        "mirror_fresh": mirror_fresh,
        "observed_at": observed_at,
        "source": "+".join(source_parts),
        "evidence": evidence,
    }


def _timestamp_is_fresh(
    value: Any,
    *,
    freshness_seconds: float,
) -> bool:
    observed_at = _timestamp_datetime(value)
    age_seconds = (
        datetime.now(timezone.utc) - observed_at
    ).total_seconds()
    return 0 <= age_seconds <= freshness_seconds


def _timestamp_is_far_future(value: Any) -> bool:
    observed_at = value
    if not isinstance(observed_at, datetime):
        observed_at = _timestamp_datetime(value)
    future_seconds = (
        observed_at - datetime.now(timezone.utc)
    ).total_seconds()
    return future_seconds > MAX_FUTURE_TIMESTAMP_SKEW_SECONDS


def _timestamp_datetime(value: Any) -> datetime:
    text = _required_text(value, "timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdapterError("timestamp must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise AdapterError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _enrichment_warning(
    label: str,
    error: AdapterError,
) -> str:
    code = "ADAPTER_ERROR"
    if isinstance(error, SoftAdapterError):
        code = error.code
    else:
        match = re.search(r"\bHTTP (\d{3})\b", str(error))
        if match is not None:
            code = f"HTTP_{match.group(1)}"
    reason = re.sub(r"\s+", " ", str(error)).strip()
    return f"{label} degraded: {code} {reason}"[:500]


def _is_ownership_error(error: AdapterError) -> bool:
    return re.search(
        r"ownership|fencing",
        str(error),
        re.IGNORECASE,
    ) is not None


def _is_soft_http_status(status_code: int) -> bool:
    if status_code in SOFT_HTTP_STATUSES:
        return True
    return 500 <= status_code <= 599


def _read_token_file(environment_name: str) -> str:
    raw_path = _required_text(
        os.environ.get(environment_name),
        environment_name,
    )
    path = Path(raw_path)
    if not path.is_absolute():
        raise AdapterError(f"{environment_name} must be absolute")
    try:
        resolved = path.resolve(strict=True)
        file_stat = path.stat()
    except OSError as exc:
        raise AdapterError(
            f"cannot read protected token file: {environment_name}"
        ) from exc
    if resolved != path:
        raise AdapterError(
            f"{environment_name} must not contain symlinks"
        )
    if not stat.S_ISREG(file_stat.st_mode):
        raise AdapterError(
            f"{environment_name} must reference a regular file"
        )
    if file_stat.st_mode & 0o077:
        raise AdapterError(
            f"{environment_name} must not grant group/world access"
        )
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AdapterError(
            f"cannot read protected token file: {environment_name}"
        ) from exc
    return _required_text(token, environment_name)


def _with_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _json_safe(dict(payload))
    normalized["evidence_sha256"] = hashlib.sha256(
        _canonical_json_bytes(normalized)
    ).hexdigest()
    return normalized


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _decode_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"{label} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AdapterError(f"{label} returned a non-object")
    return payload


def _http_error_detail(raw: bytes, fallback: Any) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            return json.dumps(
                detail,
                sort_keys=True,
                separators=(",", ":"),
            )
        if detail:
            return str(detail)
    text = raw.decode("utf-8", errors="replace").strip()
    if text:
        return text[:500]
    return str(fallback)


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AdapterError(f"{label} is required")
    return text


def _canonical_uuid(value: Any, label: str) -> str:
    try:
        return str(UUID(_required_text(value, label)))
    except ValueError as exc:
        raise AdapterError(f"{label} must be a UUID") from exc


def _decimal(value: Any, label: str) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    text = str(value).strip()
    match = re.match(
        r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)",
        text,
    )
    if match is None:
        raise AdapterError(f"{label} must be numeric")
    try:
        number = Decimal(match.group(0))
    except InvalidOperation as exc:
        raise AdapterError(f"{label} must be numeric") from exc
    if not number.is_finite():
        raise AdapterError(f"{label} must be finite")
    return number


def _positive_decimal(value: Any, label: str) -> Decimal:
    number = _decimal(value, label)
    if number <= 0:
        raise AdapterError(f"{label} must be positive")
    return number


def _non_negative_decimal(value: Any, label: str) -> Decimal:
    number = _decimal(value, label)
    if number < 0:
        raise AdapterError(f"{label} must be non-negative")
    return number


def _first_decimal(
    payload: Mapping[str, Any],
    field_names: tuple[str, ...],
) -> Decimal:
    for field_name in field_names:
        if field_name not in payload:
            continue
        number = _decimal(payload.get(field_name), field_name)
        if number != 0:
            return abs(number)
    return Decimal(0)


def _asset_amount(value: Any) -> Decimal:
    return abs(_decimal(value, "asset amount"))


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _positive_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"{label} must be numeric") from exc
    if number <= 0:
        raise AdapterError(f"{label} must be positive")
    return number


def _non_negative_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"{label} must be numeric") from exc
    if number < 0:
        raise AdapterError(f"{label} must be non-negative")
    return number


def _positive_int(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"{label} must be an integer") from exc
    if number <= 0:
        raise AdapterError(f"{label} must be positive")
    return number


def _timestamp_text(value: Any, label: str) -> str:
    text = _required_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdapterError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise AdapterError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _soft_error_payload(
    action: str,
    request: Mapping[str, Any],
    error: SoftAdapterError,
) -> dict[str, Any]:
    payload = {
        **_identity(request),
        "accepted": False,
        "action": action,
        "error_code": error.code,
        "reason": str(error),
        "status_code": error.status_code,
    }
    side_effect_id = request.get("side_effect_id")
    if side_effect_id:
        payload["side_effect_id"] = str(side_effect_id)
    return _with_evidence(payload)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1 or arguments[0] not in ACTION_NAMES:
        print(
            "usage: account_a_live_trade_http_adapter.py ACTION",
            file=sys.stderr,
        )
        return 2
    action = arguments[0]
    request: dict[str, Any] | bool = False
    try:
        request = _decode_object(
            sys.stdin.buffer.read(),
            "adapter request",
        )
        adapter = AccountALiveTradeHttpAdapter(
            Config.from_environment()
        )
        payload = adapter.dispatch(action, request)
    except SoftAdapterError as exc:
        if request is False:
            print(str(exc), file=sys.stderr)
            return 1
        payload = _soft_error_payload(action, request, exc)
    except AdapterError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.buffer.write(_canonical_json_bytes(payload) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
