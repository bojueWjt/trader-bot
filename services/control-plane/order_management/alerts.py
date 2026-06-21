"""Order-management alert normalization, dedupe, and notification outbox routing."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from psycopg2.extras import Json

from .metrics import inject_trace_context
from .outbox import deterministic_outbox_event_id


NotificationSink = Callable[["AlertNotification"], Any]

NOTIFICATION_CHANNEL = "order-management.notifications"
ALERT_EVENT_TYPE = "notification.alert"
RECOVERY_EVENT_TYPE = "notification.recovery"

_CRITICAL_CONDITIONS = frozenset(
    {
        "no_protection",
        "missing_protection",
        "protection_missing",
        "unprotected_position",
        "close_all_failure",
        "close_all_not_completed",
        "severe_drift",
        "live_data_stale",
    }
)
_HIGH_CONDITIONS = frozenset(
    {
        "lost_order",
        "order_lost",
        "account_stale",
        "daily_loss_gate",
        "daily_loss_limit",
        "drawdown_gate",
        "max_drawdown_gate",
    }
)
_MEDIUM_CONDITIONS = frozenset(
    {
        "reprice_exhausted",
        "max_reprices_exceeded",
        "partial_fill_timeout",
        "node_settings_behind",
        "settings_version_behind",
    }
)
_INFO_CONDITIONS = frozenset(
    {
        "normal_fill",
        "settings_published",
        "reconciliation_ok",
    }
)
_DRIFT_FINDING_TYPES = frozenset(
    {
        "order_drift",
        "position_drift",
        "account_drift",
        "reconciliation_drift",
        "orphan_order",
        "external_position",
    }
)
_LIVE_STALE_REASONS = frozenset({"market_data", "execution_event", "projection", "reconciliation"})


@dataclass(frozen=True)
class AlertCondition:
    condition_type: str
    account_id: str | None = None
    entity_id: str | None = None
    payload: Mapping[str, Any] | None = None
    observed_at: datetime | None = None
    is_active: bool = True
    severity: str | None = None

    @classmethod
    def active(
        cls,
        *,
        condition_type: str,
        account_id: str | None = None,
        entity_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        observed_at: datetime | None = None,
        severity: str | None = None,
    ) -> "AlertCondition":
        return cls(
            condition_type=condition_type,
            account_id=account_id,
            entity_id=entity_id,
            payload=payload or {},
            observed_at=observed_at,
            is_active=True,
            severity=severity,
        )

    def cleared(self, *, observed_at: datetime | None = None, payload: Mapping[str, Any] | None = None) -> "AlertCondition":
        merged_payload = dict(self.payload or {})
        if payload:
            merged_payload.update(payload)
        return replace(
            self,
            is_active=False,
            observed_at=observed_at or self.observed_at,
            payload=merged_payload,
        )

    @property
    def normalized_type(self) -> str:
        return normalize_condition_type(self.condition_type, self.payload)

    @property
    def alert_key(self) -> str:
        return alert_key(
            condition_type=self.normalized_type,
            account_id=self.account_id,
            entity_id=self.entity_id,
        )


@dataclass(frozen=True)
class AlertNotification:
    alert_key: str
    condition_type: str
    severity: str
    status: str
    account_id: str | None
    entity_id: str | None
    payload: dict[str, Any]
    observed_at: datetime
    channel: str = NOTIFICATION_CHANNEL
    visible: bool = True

    @property
    def event_type(self) -> str:
        return RECOVERY_EVENT_TYPE if self.status == "recovery" else ALERT_EVENT_TYPE

    @property
    def route(self) -> dict[str, str]:
        return {"channel": self.channel, "severity": self.severity}

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "order_management.notification.v1",
            "channel": self.channel,
            "route": self.route,
            "severity": self.severity,
            "status": self.status,
            "condition_type": self.condition_type,
            "alert_key": self.alert_key,
            "account_id": self.account_id,
            "entity_id": self.entity_id,
            "observed_at": self.observed_at.isoformat(),
            "visible": self.visible,
            "payload": _jsonable(self.payload),
        }


class AlertEngine:
    def __init__(self, *, cooldown: timedelta, sink: NotificationSink | None = None) -> None:
        if cooldown.total_seconds() < 0:
            raise ValueError("alert cooldown must be non-negative")
        self._cooldown = cooldown
        self._sink = sink
        self._last_emitted_at: dict[tuple[str, str], datetime] = {}
        self._active_keys: set[str] = set()

    def evaluate(self, condition: AlertCondition, *, now: datetime | None = None) -> AlertNotification | None:
        observed_at = _aware(now or condition.observed_at or datetime.now(timezone.utc))
        status = "firing" if condition.is_active else "recovery"
        key = condition.alert_key
        if not condition.is_active and key not in self._active_keys:
            return None
        if self._is_suppressed(key, status, observed_at):
            return None

        if condition.is_active:
            self._active_keys.add(key)
        else:
            self._active_keys.discard(key)

        payload = dict(condition.payload or {})
        notification = AlertNotification(
            alert_key=key,
            condition_type=condition.normalized_type,
            severity=condition.severity or severity_for_condition(condition.normalized_type, payload),
            status=status,
            account_id=condition.account_id,
            entity_id=condition.entity_id,
            payload=payload,
            observed_at=observed_at,
        )
        self._last_emitted_at[(key, status)] = observed_at
        if self._sink is not None:
            self._sink(notification)
        return notification

    def evaluate_many(
        self,
        conditions: list[AlertCondition] | tuple[AlertCondition, ...],
        *,
        now: datetime | None = None,
    ) -> list[AlertNotification]:
        emitted: list[AlertNotification] = []
        for condition in conditions:
            notification = self.evaluate(condition, now=now)
            if notification is not None:
                emitted.append(notification)
        return emitted

    def _is_suppressed(self, key: str, status: str, now: datetime) -> bool:
        last = self._last_emitted_at.get((key, status))
        if last is None:
            return False
        return now - last < self._cooldown


class OutboxNotificationSink:
    def __init__(self, conn, *, channel: str = NOTIFICATION_CHANNEL) -> None:
        self._conn = conn
        self._channel = channel

    def __call__(self, notification: AlertNotification) -> str:
        routed = replace(notification, channel=self._channel)
        payload = routed.to_payload()
        idempotency_key = f"{routed.alert_key}:{routed.status}:{routed.observed_at.isoformat()}"
        event_id = deterministic_outbox_event_id(
            "notification",
            routed.alert_key,
            routed.event_type,
            idempotency_key,
        )
        event_payload = inject_trace_context(
            payload,
            request_id=_payload_value(routed.payload, "request_id"),
            idempotency_key=idempotency_key,
            command_id=_payload_value(routed.payload, "command_id"),
            order_id=_payload_value(routed.payload, "client_order_id"),
        )
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO outbox_events (
                    outbox_event_id,
                    status,
                    aggregate_type,
                    aggregate_id,
                    event_type,
                    payload,
                    trace_id
                )
                VALUES (%s, 'pending', %s, %s, %s, %s, %s)
                ON CONFLICT (outbox_event_id) DO NOTHING
                RETURNING outbox_event_id::text
                """,
                (
                    str(event_id),
                    "notification",
                    routed.alert_key,
                    routed.event_type,
                    Json(event_payload),
                    event_payload["trace_id"],
                ),
            )
            row = cur.fetchone()
        return row[0] if row else str(event_id)


def normalize_condition_type(condition_type: str, payload: Mapping[str, Any] | None = None) -> str:
    raw = str(condition_type).strip().lower()
    if raw in {"missing_protection", "protection_missing", "unprotected_position"}:
        return "no_protection"
    if raw in {"close_all_not_completed", "close_all_failed", "close_all_partial"}:
        return "close_all_failure"
    if raw in _DRIFT_FINDING_TYPES or raw == "severe_drift":
        return "drift"
    if raw in {"live_data_stale", "freshness_stale"}:
        return "stale"
    if raw == "stale" and str((payload or {}).get("freshness_kind") or "") == "account_data":
        return "account_stale"
    if raw in {"order_lost"}:
        return "lost_order"
    if raw in {"daily_loss_limit"}:
        return "daily_loss_gate"
    if raw in {"max_drawdown_gate"}:
        return "drawdown_gate"
    if raw in {"max_reprices_exceeded"}:
        return "reprice_exhausted"
    if raw in {"settings_version_behind"}:
        return "node_settings_behind"
    return raw


def severity_for_condition(condition_type: str, payload: Mapping[str, Any] | None = None) -> str:
    payload = payload or {}
    normalized = normalize_condition_type(condition_type, payload)
    if normalized == "stale":
        freshness_kind = str(payload.get("freshness_kind") or payload.get("kind") or "")
        return "critical" if freshness_kind in _LIVE_STALE_REASONS else "high"
    if normalized == "drift":
        drift_class = str(payload.get("drift_class") or payload.get("severity") or "severe").lower()
        return "critical" if drift_class in {"severe", "critical", "error"} else "high"
    if normalized in _CRITICAL_CONDITIONS or normalized in {"no_protection", "close_all_failure"}:
        return "critical"
    if normalized in _HIGH_CONDITIONS:
        return "high"
    if normalized in _MEDIUM_CONDITIONS:
        return "medium"
    if normalized in _INFO_CONDITIONS:
        return "info"
    return "info"


def alert_key(*, condition_type: str, account_id: str | None = None, entity_id: str | None = None) -> str:
    return "|".join(
        (
            _key_part(condition_type),
            _key_part(account_id or "global"),
            _key_part(entity_id or "none"),
        )
    )


def condition_from_reconciliation_finding(finding: Mapping[str, Any], *, active: bool = True) -> AlertCondition:
    payload = dict(finding.get("payload") or {})
    finding_type = str(finding.get("finding_type") or "")
    payload.setdefault("finding_type", finding_type)
    if finding.get("reconciliation_finding_id"):
        payload.setdefault("reconciliation_finding_id", finding.get("reconciliation_finding_id"))
    entity_id = (
        finding.get("position_key")
        or finding.get("order_projection_id")
        or payload.get("client_order_id")
        or payload.get("venue_order_id")
        or finding.get("reconciliation_finding_id")
        or finding_type
    )
    normalized = normalize_condition_type(finding_type, payload)
    return AlertCondition(
        condition_type=normalized,
        account_id=_optional_str(finding.get("account_id")),
        entity_id=_optional_str(entity_id),
        payload=payload,
        observed_at=_optional_datetime(finding.get("created_at")),
        is_active=active,
        severity=severity_for_condition(normalized, payload),
    )


def conditions_from_freshness_reasons(
    *,
    account_id: str,
    reasons: list[str] | tuple[str, ...],
    observed_at: datetime,
    active: bool = True,
) -> list[AlertCondition]:
    conditions: list[AlertCondition] = []
    for reason in reasons:
        freshness_kind = str(reason)
        condition_type = "account_stale" if freshness_kind == "account_data" else "stale"
        payload = {"freshness_kind": freshness_kind}
        conditions.append(
            AlertCondition(
                condition_type=condition_type,
                account_id=account_id,
                entity_id=freshness_kind,
                payload=payload,
                observed_at=observed_at,
                is_active=active,
                severity=severity_for_condition(condition_type, payload),
            )
        )
    return conditions


def conditions_from_freshness_state(
    *,
    account_id: str,
    state: Any,
    now: datetime,
    active: bool = True,
) -> list[AlertCondition]:
    return conditions_from_freshness_reasons(
        account_id=account_id,
        reasons=list(state.stale_reasons(now=now)),
        observed_at=now,
        active=active,
    )


def condition_from_close_all_result(
    *,
    account_id: str,
    request_id: str,
    result: Mapping[str, Any],
    observed_at: datetime,
) -> AlertCondition:
    result_payload = dict(result)
    verification = result_payload.get("verification") or {}
    flat = bool(verification.get("flat")) if isinstance(verification, Mapping) else False
    completed = str(result_payload.get("status") or "").lower() == "completed" and flat
    result_payload.setdefault("request_id", request_id)
    return AlertCondition(
        condition_type="close_all_failure",
        account_id=account_id,
        entity_id=request_id,
        payload=result_payload,
        observed_at=observed_at,
        is_active=not completed,
        severity="critical",
    )


def condition_from_loss_limit_decision(
    *,
    account_id: str,
    decision: Any,
    observed_at: datetime,
) -> AlertCondition:
    mode = str(getattr(decision, "mode", "")).upper()
    reason = str(getattr(decision, "reason", ""))
    condition_type = "drawdown_gate" if "drawdown" in reason.lower() or mode in {"HALT", "HALTED"} else "daily_loss_gate"
    payload = {
        "mode": mode,
        "reason": reason,
        "daily_loss": str(getattr(decision, "daily_loss", "")),
        "drawdown_pct": str(getattr(decision, "drawdown_pct", "")),
    }
    return AlertCondition(
        condition_type=condition_type,
        account_id=account_id,
        entity_id=condition_type,
        payload=payload,
        observed_at=observed_at,
        is_active=mode in {"REDUCING", "HALT", "HALTED"},
        severity="high",
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _key_part(value: str) -> str:
    return str(value).strip().lower().replace("|", "/")


def _payload_value(payload: Mapping[str, Any], key: str) -> Any | None:
    value = payload.get(key)
    if value is None or str(value).strip() == "":
        return None
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
