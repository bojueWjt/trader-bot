from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

from .contracts import ApprovedTradeIntentV1, ExecutionEventEnvelopeV1
from .control_plane import (
    CommandAckStatus,
    CommandType,
    ControlPlaneClient,
    Heartbeat,
    IntentAckStatus,
    IntentBatch,
    IntentItem,
    NodeCommand,
)


class ControlPlaneHttpError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code


class ControlPlaneIdentityError(ControlPlaneHttpError):
    pass


class ControlPlaneFenceConflictError(ControlPlaneHttpError):
    is_fence_conflict = True


class HttpControlPlaneClient(ControlPlaneClient):
    """HTTP implementation of the window-B control-plane seam."""

    def __init__(
        self,
        base_url: str,
        token: str,
        node_id: str,
        account_id: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._node_id = _required_identity(node_id, "node_id")
        self._account_id = _required_identity(account_id, "account_id")
        self._timeout_seconds = timeout_seconds

    def fetch_intents(
        self,
        account_id: str,
        after_cursor: Optional[str],
        limit: int,
        wait_ms: int = 0,
    ) -> IntentBatch:
        self._require_account_id(account_id)
        query = {
            "account_id": account_id,
            "limit": str(limit),
            "wait_ms": str(wait_ms),
        }
        if after_cursor is not None:
            query["after"] = after_cursor
        payload = self._request_json(
            "GET", f"/v1/nodes/{self._node_id}/intents?{urlencode(query)}"
        )
        items = [
            IntentItem(
                cursor=str(item["cursor"]),
                intent=ApprovedTradeIntentV1.model_validate(item["intent"]),
            )
            for item in payload.get("items", [])
        ]
        return IntentBatch(items=items, next_cursor=payload.get("next_cursor"))

    def ack_intent(
        self,
        account_id: str,
        node_id: str,
        intent_id: UUID,
        status: IntentAckStatus,
        detail: Optional[str] = None,
    ) -> None:
        self._require_node_id(node_id)
        self._require_account_id(account_id)
        body: dict[str, Any] = {"account_id": account_id, "status": status.value}
        if detail is not None:
            body["detail"] = detail
        self._request_json(
            "POST",
            f"/v1/nodes/{node_id}/intents/{intent_id}/ack",
            body,
            allow_empty=True,
        )

    def post_events(
        self, node_id: str, events: Sequence[ExecutionEventEnvelopeV1]
    ) -> list[str]:
        self._require_node_id(node_id)
        for event in events:
            self._require_node_id(event.node_id)
            self._require_account_id(event.account_id)
        payload = self._request_json(
            "POST",
            f"/v1/nodes/{node_id}/execution-events",
            {
                "account_id": self._account_id,
                "events": [_model_dump_jsonable(event) for event in events],
            },
        )
        return [str(event_id) for event_id in payload.get("acked_event_ids", [])]

    def heartbeat(self, node_id: str, hb: Heartbeat) -> None:
        self._require_node_id(node_id)
        self._require_account_id(hb.account_id)
        self._request_json(
            "POST",
            f"/v1/nodes/{node_id}/heartbeat",
            _heartbeat_dump(hb),
            allow_empty=True,
        )

    def poll_commands(self, node_id: str, after: Optional[str]) -> list[NodeCommand]:
        self._require_node_id(node_id)
        query_values = {"account_id": self._account_id}
        if after is not None:
            query_values["after"] = after
        query = urlencode(query_values)
        suffix = f"?{query}"
        payload = self._request_json("GET", f"/v1/nodes/{node_id}/commands{suffix}")
        return [
            NodeCommand(
                command_id=str(item["command_id"]),
                type=CommandType(item["type"]),
                args=dict(item.get("args", {})),
                issued_at=_parse_datetime(item.get("issued_at")),
            )
            for item in payload.get("commands", [])
        ]

    def ack_command(
        self,
        node_id: str,
        command_id: str,
        status: CommandAckStatus,
        result: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        self._require_node_id(node_id)
        body: dict[str, Any] = {
            "account_id": self._account_id,
            "status": status.value,
        }
        if result is not None:
            body["result"] = result
        if error is not None:
            body["error"] = error
        self._request_json(
            "POST",
            f"/v1/nodes/{node_id}/commands/{command_id}/ack",
            body,
            allow_empty=True,
        )

    def latest_snapshot_generated_at(self, account_id: str) -> Optional[datetime]:
        self._require_account_id(account_id)
        query = urlencode({"node_id": self._node_id})
        payload = self._request_json(
            "GET",
            f"/v1/accounts/{account_id}?{query}",
        )
        generated_at = payload.get("generated_at")
        return _parse_datetime(generated_at)

    def _require_node_id(self, node_id: str) -> None:
        candidate = _required_identity(node_id, "node_id")
        if candidate != self._node_id:
            raise ControlPlaneIdentityError(
                f"node_id mismatch: bound={self._node_id!r} requested={candidate!r}"
            )

    def _require_account_id(self, account_id: str) -> None:
        candidate = _required_identity(account_id, "account_id")
        if candidate != self._account_id:
            raise ControlPlaneIdentityError(
                "account_id mismatch: "
                f"bound={self._account_id!r} requested={candidate!r}"
            )

    def _request_json(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "X-Node-Id": self._node_id,
            "X-Account-Id": self._account_id,
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            message = (
                f"{method} {path} failed with HTTP {exc.code}: {detail}"
            )
            error_type = ControlPlaneHttpError
            if exc.code == 409:
                error_type = ControlPlaneFenceConflictError
            raise error_type(
                message,
                status_code=exc.code,
            ) from exc
        except URLError as exc:
            raise ControlPlaneHttpError(f"{method} {path} failed: {exc.reason}") from exc

        if not raw:
            if allow_empty:
                return {}
            raise ControlPlaneHttpError(f"{method} {path} returned an empty response")
        return json.loads(raw.decode("utf-8"))


def _heartbeat_dump(hb: Heartbeat) -> dict[str, Any]:
    payload = asdict(hb)
    payload["ts"] = hb.ts.isoformat()
    payload["trading_state"] = hb.trading_state.value
    payload["reconciliation_state"] = hb.reconciliation_state.value
    return payload


def _required_identity(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ControlPlaneIdentityError(f"{field_name} is required")
    return normalized


def _model_dump_jsonable(model: ExecutionEventEnvelopeV1) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ControlPlaneHttpError(f"invalid datetime value {value!r}")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
