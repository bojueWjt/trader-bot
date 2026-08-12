from __future__ import annotations

import json
import math
import re
import socket
from dataclasses import asdict
from datetime import datetime
from queue import Empty, Queue
from threading import Lock, Thread
from typing import Any, Callable, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

from .contracts import ExecutionEventEnvelopeV1
from .control_plane import (
    CommandAckStatus,
    CommandType,
    ControlPlaneClient,
    Heartbeat,
    HeartbeatReceipt,
    IncidentSeverity,
    IntentAckStatus,
    IntentBatch,
    IntentItem,
    NodeWriterIdentity,
    NodeCommand,
    PeerIdentityReceipt,
    ProductionIncidentReceipt,
    ProductionIncidentReport,
    ReleaseGateReceipt,
    ReleaseIdentity,
)


class ControlPlaneHttpError(RuntimeError):
    pass


class ControlPlaneTransportError(ControlPlaneHttpError):
    pass


class ControlPlaneConnectTimeout(ControlPlaneTransportError):
    pass


class ControlPlaneReadTimeout(ControlPlaneTransportError):
    pass


class ControlPlaneTotalTimeout(ControlPlaneTransportError):
    pass


class ControlPlaneIdentityError(ControlPlaneHttpError):
    pass


class ControlPlaneFencingError(ControlPlaneIdentityError):
    pass


class HttpControlPlaneClient(ControlPlaneClient):
    """HTTP implementation of the window-B control-plane seam."""

    def __init__(
        self,
        base_url: str,
        token: str,
        node_id: str,
        account_id: str,
        timeout_seconds: float = 5.0,
        redis_fencing_epoch: str | None = None,
        runtime_generation: str | None = None,
        lease_fencing_token: int | None = None,
        connect_timeout_seconds: float | None = None,
        read_timeout_seconds: float | None = None,
        total_timeout_seconds: float | None = None,
        fatal_fence_hook: Callable[[str], None] | None = None,
        fatal_transport_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._node_id = _required_identity(node_id, "node_id")
        self._account_id = _required_identity(account_id, "account_id")
        default_timeout = _positive_timeout(
            timeout_seconds,
            "timeout_seconds",
        )
        self._connect_timeout_seconds = _timeout_or_default(
            connect_timeout_seconds,
            default_timeout,
            "connect_timeout_seconds",
        )
        self._read_timeout_seconds = _timeout_or_default(
            read_timeout_seconds,
            default_timeout,
            "read_timeout_seconds",
        )
        self._total_timeout_seconds = _timeout_or_default(
            total_timeout_seconds,
            default_timeout,
            "total_timeout_seconds",
        )
        self._writer_identity_lock = Lock()
        self._writer_identity = _optional_writer_identity(
            redis_fencing_epoch=redis_fencing_epoch,
            runtime_generation=runtime_generation,
            lease_fencing_token=lease_fencing_token,
        )
        if fatal_fence_hook is not None and not callable(fatal_fence_hook):
            raise TypeError("fatal fence hook must be callable")
        if (
            fatal_transport_hook is not None
            and not callable(fatal_transport_hook)
        ):
            raise TypeError("fatal transport hook must be callable")
        self._fatal_fence_hook = fatal_fence_hook
        self._fatal_fence_lock = Lock()
        self._fatal_fence_reason: str | bool = False
        self._fatal_fence_hook_invoked = False
        self._fatal_transport_hook = fatal_transport_hook
        self._fatal_transport_lock = Lock()
        self._fatal_transport_reason: str | bool = False
        self._fatal_transport_hook_invoked = False

    @property
    def writer_identity(self) -> NodeWriterIdentity | bool:
        with self._writer_identity_lock:
            writer_identity = self._writer_identity
        if writer_identity is None:
            return False
        return writer_identity

    def bind_writer_identity(
        self,
        *,
        redis_fencing_epoch: str,
        runtime_generation: str,
        lease_fencing_token: int,
    ) -> None:
        candidate = _optional_writer_identity(
            redis_fencing_epoch=redis_fencing_epoch,
            runtime_generation=runtime_generation,
            lease_fencing_token=lease_fencing_token,
        )
        if candidate is None:
            raise ControlPlaneIdentityError(
                "writer identity is required"
            )
        with self._writer_identity_lock:
            current = self._writer_identity
            if current is not None and current != candidate:
                raise ControlPlaneIdentityError(
                    "writer identity is already bound"
                )
            self._writer_identity = candidate

    def bind_fatal_fence_hook(
        self,
        hook: Callable[[str], None],
    ) -> None:
        if not callable(hook):
            raise TypeError("fatal fence hook must be callable")
        invoke_reason: str | bool = False
        with self._fatal_fence_lock:
            current = self._fatal_fence_hook
            if current is not None and current is not hook:
                raise ControlPlaneIdentityError(
                    "fatal fence hook is already bound"
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

    def bind_fatal_transport_hook(
        self,
        hook: Callable[[str], None],
    ) -> None:
        if not callable(hook):
            raise TypeError("fatal transport hook must be callable")
        invoke_reason: str | bool = False
        with self._fatal_transport_lock:
            current = self._fatal_transport_hook
            if current is not None and current is not hook:
                raise ControlPlaneTransportError(
                    "fatal transport hook is already bound"
                )
            self._fatal_transport_hook = hook
            if (
                self._fatal_transport_reason is not False
                and not self._fatal_transport_hook_invoked
            ):
                self._fatal_transport_hook_invoked = True
                invoke_reason = self._fatal_transport_reason
        if invoke_reason is not False:
            self._invoke_fatal_transport_hook(
                hook,
                str(invoke_reason),
            )

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
                intent=item["intent"],
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

    def heartbeat(self, node_id: str, hb: Heartbeat) -> HeartbeatReceipt:
        self._require_node_id(node_id)
        self._require_account_id(hb.account_id)
        payload = self._request_json(
            "POST",
            f"/v1/nodes/{node_id}/heartbeat",
            _heartbeat_dump(hb),
        )
        return _heartbeat_receipt(payload)

    def report_incident(
        self,
        node_id: str,
        report: ProductionIncidentReport,
    ) -> ProductionIncidentReceipt:
        self._require_node_id(node_id)
        self._require_account_id(report.account_id)
        payload = self._request_json(
            "POST",
            f"/v1/nodes/{node_id}/incidents",
            {
                "account_id": report.account_id,
                "severity": report.severity.value,
                "reason": report.reason,
                "summary": report.summary,
            },
        )
        receipt = _production_incident_receipt(payload)
        self._require_node_id(receipt.node_id)
        self._require_account_id(receipt.account_id)
        return receipt

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
        fatal_fence_reason = self._current_fatal_fence_reason()
        if fatal_fence_reason is not False:
            raise ControlPlaneFencingError(str(fatal_fence_reason))
        fatal_transport_reason = self._current_fatal_transport_reason()
        if fatal_transport_reason is not False:
            raise ControlPlaneTotalTimeout(
                str(fatal_transport_reason)
            )
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "X-Node-Id": self._node_id,
            "X-Account-Id": self._account_id,
        }
        with self._writer_identity_lock:
            writer_identity = self._writer_identity
        if writer_identity is not None:
            headers["X-Redis-Fencing-Epoch"] = (
                writer_identity.redis_fencing_epoch
            )
            headers["X-Runtime-Generation"] = (
                writer_identity.runtime_generation
            )
            headers["X-Lease-Fencing-Token"] = str(
                writer_identity.lease_fencing_token
            )
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        raw = self._request_bytes_with_total_deadline(
            request,
            method=method,
            path=path,
        )

        if not raw:
            if allow_empty:
                return {}
            raise ControlPlaneHttpError(f"{method} {path} returned an empty response")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlPlaneHttpError(
                f"{method} {path} returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ControlPlaneHttpError(
                f"{method} {path} returned a non-object JSON response"
            )
        return payload

    def _request_bytes_with_total_deadline(
        self,
        request: Request,
        *,
        method: str,
        path: str,
    ) -> bytes:
        result: Queue[tuple[bool, Any]] = Queue(maxsize=1)

        def perform() -> None:
            try:
                raw = self._perform_request(
                    request,
                    method=method,
                    path=path,
                )
            except BaseException as exc:
                result.put((False, exc))
                return
            result.put((True, raw))

        worker = Thread(
            target=perform,
            name=f"control-plane-http.{method.lower()}",
            daemon=True,
        )
        worker.start()
        try:
            succeeded, value = result.get(
                timeout=self._total_timeout_seconds
            )
        except Empty as exc:
            reason = (
                f"{method} {path} total deadline exceeded "
                f"after {self._total_timeout_seconds:.3f}s"
            )
            self._trigger_fatal_transport(reason)
            raise ControlPlaneTotalTimeout(reason) from exc
        if not succeeded:
            raise value
        return value

    def _perform_request(
        self,
        request: Request,
        *,
        method: str,
        path: str,
    ) -> bytes:
        try:
            response = urlopen(
                request,
                timeout=self._connect_timeout_seconds,
            )
        except HTTPError as exc:
            error = self._classify_http_error(method, path, exc)
            raise error from exc
        except URLError as exc:
            if _is_timeout_reason(exc.reason):
                raise ControlPlaneConnectTimeout(
                    f"{method} {path} connect timeout after "
                    f"{self._connect_timeout_seconds:.3f}s"
                ) from exc
            raise ControlPlaneTransportError(
                f"{method} {path} transport failed: {exc.reason}"
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ControlPlaneConnectTimeout(
                f"{method} {path} connect timeout after "
                f"{self._connect_timeout_seconds:.3f}s"
            ) from exc

        try:
            with response:
                _set_response_read_timeout(
                    response,
                    self._read_timeout_seconds,
                )
                try:
                    return response.read()
                except URLError as exc:
                    if _is_timeout_reason(exc.reason):
                        raise ControlPlaneReadTimeout(
                            f"{method} {path} read timeout after "
                            f"{self._read_timeout_seconds:.3f}s"
                        ) from exc
                    raise ControlPlaneTransportError(
                        f"{method} {path} read failed: {exc.reason}"
                    ) from exc
                except (socket.timeout, TimeoutError) as exc:
                    raise ControlPlaneReadTimeout(
                        f"{method} {path} read timeout after "
                        f"{self._read_timeout_seconds:.3f}s"
                    ) from exc
        except ControlPlaneHttpError:
            raise
        except OSError as exc:
            raise ControlPlaneTransportError(
                f"{method} {path} read failed: {exc}"
            ) from exc

    def _classify_http_error(
        self,
        method: str,
        path: str,
        exc: HTTPError,
    ) -> ControlPlaneHttpError:
        raw_detail = exc.read().decode("utf-8", errors="replace")
        detail = _http_error_detail(raw_detail)
        if exc.code == 409 and _is_writer_fence_rejection(
            exc.headers,
            detail,
        ):
            reason = f"control-plane rejected stale writer: {detail}"
            self._trigger_fatal_fence(reason)
            return ControlPlaneFencingError(reason)
        return ControlPlaneHttpError(
            f"{method} {path} failed with HTTP {exc.code}: {detail}"
        )

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
        if hook is None:
            return
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

    def _trigger_fatal_transport(self, reason: str) -> None:
        hook: Callable[[str], None] | None = None
        with self._fatal_transport_lock:
            if self._fatal_transport_reason is not False:
                return
            self._fatal_transport_reason = reason
            configured_hook = self._fatal_transport_hook
            if (
                callable(configured_hook)
                and not self._fatal_transport_hook_invoked
            ):
                self._fatal_transport_hook_invoked = True
                hook = configured_hook
        if hook is None:
            return
        self._invoke_fatal_transport_hook(hook, reason)

    def _invoke_fatal_transport_hook(
        self,
        hook: Callable[[str], None],
        reason: str,
    ) -> None:
        try:
            hook(reason)
        except Exception:
            return

    def _current_fatal_transport_reason(self) -> str | bool:
        with self._fatal_transport_lock:
            return self._fatal_transport_reason


def _heartbeat_dump(hb: Heartbeat) -> dict[str, Any]:
    payload = asdict(hb)
    payload["ts"] = hb.ts.isoformat()
    payload["trading_state"] = hb.trading_state.value
    payload["reconciliation_state"] = hb.reconciliation_state.value
    for field_name in (
        "positions_snapshot_at",
        "regular_orders_snapshot_at",
        "algo_orders_snapshot_at",
        "reconciliation_completed_at",
    ):
        value = payload.get(field_name)
        if isinstance(value, datetime):
            payload[field_name] = value.isoformat()
    return payload


def _production_incident_receipt(
    payload: dict[str, Any],
) -> ProductionIncidentReceipt:
    incident_id = _required_response_string(payload, "incident_id")
    account_id = _required_response_string(payload, "account_id")
    node_id = _required_response_string(payload, "node_id")
    reason = _required_response_string(payload, "reason")
    status = _required_response_string(payload, "status")
    summary = _required_response_string(payload, "summary")
    try:
        severity = IncidentSeverity(
            _required_response_string(payload, "severity")
        )
    except ValueError as exc:
        raise ControlPlaneHttpError(
            "incident receipt severity is invalid"
        ) from exc
    opened_at = _parse_datetime(payload.get("opened_at"))
    if opened_at is None:
        raise ControlPlaneHttpError(
            "incident receipt opened_at is required"
        )
    deduplicated = payload.get("deduplicated")
    if not isinstance(deduplicated, bool):
        raise ControlPlaneHttpError(
            "incident receipt deduplicated must be a boolean"
        )
    return ProductionIncidentReceipt(
        incident_id=incident_id,
        account_id=account_id,
        node_id=node_id,
        reason=reason,
        severity=severity,
        status=status,
        summary=summary,
        opened_at=opened_at,
        deduplicated=deduplicated,
    )


def _heartbeat_receipt(payload: dict[str, Any]) -> HeartbeatReceipt:
    raw_gate = payload.get("release_gate")
    if not isinstance(raw_gate, dict):
        return HeartbeatReceipt(
            release_gate=ReleaseGateReceipt(
                status="missing",
                release_id=None,
                reviewed_manifest=None,
                rollout_phase=None,
                live_open_mode=None,
                phase_version=None,
            ),
            peers=(),
        )
    raw_manifest = raw_gate.get("reviewed_manifest")
    manifest = None
    if isinstance(raw_manifest, dict):
        manifest = _release_identity(raw_manifest)
    gate = ReleaseGateReceipt(
        status=str(raw_gate.get("status") or "").strip(),
        release_id=_optional_string(raw_gate.get("release_id")),
        reviewed_manifest=manifest,
        rollout_phase=_optional_string(raw_gate.get("rollout_phase")),
        live_open_mode=_optional_string(
            raw_gate.get("live_open_mode")
        ),
        phase_version=_optional_positive_integer(
            raw_gate.get("phase_version")
        ),
    )
    if not gate.status:
        raise ControlPlaneHttpError(
            "heartbeat receipt release_gate status is required"
        )

    raw_peers = payload.get("peers")
    if raw_peers is None:
        raw_peers = []
    if not isinstance(raw_peers, list):
        raise ControlPlaneHttpError("heartbeat receipt peers must be a list")
    peers = []
    for raw_peer in raw_peers:
        if not isinstance(raw_peer, dict):
            raise ControlPlaneHttpError(
                "heartbeat receipt peer must be an object"
            )
        node_id = str(raw_peer.get("node_id") or "").strip()
        if not node_id:
            raise ControlPlaneHttpError(
                "heartbeat receipt peer node_id is required"
            )
        raw_age = raw_peer.get("freshness_age_seconds")
        age = None
        if raw_age is not None and raw_age is not False:
            try:
                age = float(raw_age)
            except (TypeError, ValueError) as exc:
                raise ControlPlaneHttpError(
                    "heartbeat receipt peer freshness age is invalid"
                ) from exc
        fresh = raw_peer.get("fresh") is True
        identity_matches = raw_peer.get("identity_matches") is True
        peer_status = str(raw_peer.get("status") or "").strip()
        if not peer_status:
            if fresh and identity_matches:
                peer_status = "consistent"
            elif not fresh:
                peer_status = "stale"
            else:
                peer_status = "identity_drift"
        if peer_status not in {
            "consistent",
            "rollout_pending",
            "missing",
            "stale",
            "identity_drift",
        }:
            raise ControlPlaneHttpError(
                "heartbeat receipt peer status is invalid"
            )
        peers.append(
            PeerIdentityReceipt(
                node_id=node_id,
                account_id=_optional_string(raw_peer.get("account_id")),
                release_id=_optional_string(raw_peer.get("release_id")),
                image_digest=_optional_string(raw_peer.get("image_digest")),
                config_sha256=_optional_string(raw_peer.get("config_sha256")),
                dependency_lock_sha256=_optional_string(
                    raw_peer.get("dependency_lock_sha256")
                ),
                schema_epoch=_optional_string(raw_peer.get("schema_epoch")),
                redis_fencing_epoch=_optional_string(
                    raw_peer.get("redis_fencing_epoch")
                ),
                freshness_age_seconds=age,
                fresh=fresh,
                identity_matches=identity_matches,
                status=peer_status,
            )
        )
    return HeartbeatReceipt(release_gate=gate, peers=tuple(peers))


def _release_identity(payload: dict[str, Any]) -> ReleaseIdentity:
    return ReleaseIdentity(
        release_id=_optional_string(payload.get("release_id")),
        image_digest=_optional_string(payload.get("image_digest")),
        config_sha256=_optional_string(payload.get("config_sha256")),
        dependency_lock_sha256=_optional_string(
            payload.get("dependency_lock_sha256")
        ),
        schema_epoch=_optional_string(payload.get("schema_epoch")),
    )


def _optional_string(value: Any) -> Optional[str]:
    if value is None or value is False:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return normalized


def _optional_positive_integer(value: Any) -> Optional[int]:
    if value is None or value is False:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControlPlaneHttpError(
            "heartbeat receipt phase_version must be an integer"
        )
    if value < 1:
        raise ControlPlaneHttpError(
            "heartbeat receipt phase_version must be positive"
        )
    return value


def _required_response_string(
    payload: dict[str, Any],
    field_name: str,
) -> str:
    value = _optional_string(payload.get(field_name))
    if value is None:
        raise ControlPlaneHttpError(
            f"response field {field_name} is required"
        )
    return value


def _required_identity(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ControlPlaneIdentityError(f"{field_name} is required")
    return normalized


def _optional_writer_identity(
    *,
    redis_fencing_epoch: str | None,
    runtime_generation: str | None,
    lease_fencing_token: int | None,
) -> NodeWriterIdentity | None:
    values = (
        redis_fencing_epoch,
        runtime_generation,
        lease_fencing_token,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ControlPlaneIdentityError(
            "writer identity requires redis_fencing_epoch, "
            "runtime_generation, and lease_fencing_token"
        )
    try:
        return NodeWriterIdentity(
            redis_fencing_epoch=str(redis_fencing_epoch),
            runtime_generation=str(runtime_generation),
            lease_fencing_token=lease_fencing_token,
        )
    except (TypeError, ValueError) as exc:
        raise ControlPlaneIdentityError(str(exc)) from exc


def _positive_timeout(value: float, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be positive")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be positive") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{field_name} must be positive")
    return timeout


def _timeout_or_default(
    value: float | None,
    default: float,
    field_name: str,
) -> float:
    if value is None:
        return default
    return _positive_timeout(value, field_name)


def _is_timeout_reason(reason: Any) -> bool:
    return isinstance(reason, (socket.timeout, TimeoutError))


def _set_response_read_timeout(
    response: Any,
    timeout_seconds: float,
) -> None:
    fp = getattr(response, "fp", None)
    if fp is None:
        return
    raw = getattr(fp, "raw", None)
    if raw is None:
        return
    sock = getattr(raw, "_sock", None)
    if sock is None:
        return
    setter = getattr(sock, "settimeout", None)
    if callable(setter):
        setter(timeout_seconds)


def _http_error_detail(raw_detail: str) -> str:
    normalized = str(raw_detail or "").strip()
    if not normalized:
        return "empty error response"
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError:
        return normalized
    if not isinstance(payload, dict):
        return normalized
    detail = payload.get("detail")
    if detail is None:
        return normalized
    rendered = str(detail).strip()
    if not rendered:
        return normalized
    return rendered


def _is_writer_fence_rejection(headers: Any, detail: str) -> bool:
    marker = ""
    if headers is not None:
        raw_marker = headers.get("X-Writer-Fence-Rejected")
        marker = str(raw_marker or "").strip().lower()
    if marker in {"1", "true", "yes"}:
        return True
    return re.search(
        (
            r"(?:stale\s+(?:node\s+)?writer|"
            r"redis\s+fencing\s+epoch\s+mismatch|"
            r"runtime\s+generation\s+mismatch|"
            r"lease\s+fencing\s+token\s+mismatch|"
            r"writer\s+identity\s+mismatch)"
        ),
        detail,
        flags=re.IGNORECASE,
    ) is not None


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
