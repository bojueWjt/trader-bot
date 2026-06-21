from __future__ import annotations

import copy
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from threading import RLock
from typing import Any, Mapping
from uuid import UUID, uuid4


SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)
REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    request_id: str | None = None


@dataclass
class _HistogramSeries:
    labels: dict[str, str]
    count: int = 0
    sum: float = 0.0
    min: float | None = None
    max: float | None = None

    def observe(self, value: float) -> None:
        self.count += 1
        self.sum += value
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)

    def snapshot(self) -> dict[str, Any]:
        return {
            "labels": dict(self.labels),
            "count": self.count,
            "sum": self.sum,
            "min": self.min,
            "max": self.max,
        }


@dataclass
class _CounterSeries:
    labels: dict[str, str]
    value: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        value: int | float = int(self.value) if self.value.is_integer() else self.value
        return {"labels": dict(self.labels), "value": value}


@dataclass
class MetricsRegistry:
    """In-process metrics registry with exporter-friendly snapshots."""

    _counters: dict[str, dict[tuple[tuple[str, str], ...], _CounterSeries]] = field(default_factory=dict)
    _histograms: dict[str, dict[tuple[tuple[str, str], ...], _HistogramSeries]] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock)

    def increment(
        self,
        name: str,
        *,
        labels: Mapping[str, Any] | None = None,
        amount: int | float = 1,
    ) -> None:
        if amount < 0:
            raise ValueError("counter amount must be non-negative")
        key, normalized = _label_key(labels)
        with self._lock:
            series = self._counters.setdefault(name, {}).setdefault(
                key,
                _CounterSeries(labels=normalized),
            )
            series.value += float(amount)

    def observe(
        self,
        name: str,
        value: int | float,
        *,
        labels: Mapping[str, Any] | None = None,
    ) -> None:
        observed = float(value)
        if not math.isfinite(observed) or observed < 0:
            raise ValueError("histogram observation must be a finite non-negative number")
        key, normalized = _label_key(labels)
        with self._lock:
            series = self._histograms.setdefault(name, {}).setdefault(
                key,
                _HistogramSeries(labels=normalized),
            )
            series.observe(observed)

    def record_intent_latency(
        self,
        stage: str,
        *,
        intent_approved_at: datetime,
        observed_at: datetime,
        labels: Mapping[str, Any] | None = None,
    ) -> None:
        stage_name = str(stage).strip().lower()
        if stage_name not in {"submit", "accept", "fill"}:
            raise ValueError("intent latency stage must be submit, accept, or fill")
        seconds = (_aware(observed_at) - _aware(intent_approved_at)).total_seconds()
        self.observe(f"intent_to_{stage_name}_seconds", seconds, labels=labels)

    def record_failure(
        self,
        kind: str,
        *,
        labels: Mapping[str, Any] | None = None,
        amount: int | float = 1,
    ) -> None:
        self.increment(f"failure.{_metric_component(kind)}.total", labels=labels, amount=amount)

    def record_protection_install_latency(
        self,
        seconds: int | float,
        *,
        labels: Mapping[str, Any] | None = None,
    ) -> None:
        self.observe("protection.install.duration_seconds", seconds, labels=labels)

    def record_reconciliation_drift(
        self,
        *,
        labels: Mapping[str, Any] | None = None,
        amount: int | float = 1,
    ) -> None:
        self.increment("reconciliation.drift.total", labels=labels, amount=amount)

    def record_command_duration(
        self,
        command_type: str,
        seconds: int | float,
        *,
        status: str,
        labels: Mapping[str, Any] | None = None,
    ) -> None:
        merged = {"status": status, **dict(labels or {})}
        self.observe(
            f"command.{_metric_component(command_type)}.duration_seconds",
            seconds,
            labels=merged,
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = {
                name: {
                    "type": "counter",
                    "series": [series.snapshot() for _, series in sorted(items.items())],
                }
                for name, items in sorted(self._counters.items())
            }
            histograms = {
                name: {
                    "type": "histogram",
                    "series": [series.snapshot() for _, series in sorted(items.items())],
                }
                for name, items in sorted(self._histograms.items())
            }
        return {"counters": counters, "histograms": histograms}

    def clear(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


default_registry = MetricsRegistry()


def derive_trace_id(
    *,
    trace_id: Any | None = None,
    request_id: Any | None = None,
    idempotency_key: Any | None = None,
    intent_id: Any | None = None,
    execution_job_id: Any | None = None,
    order_id: Any | None = None,
    command_id: Any | None = None,
) -> str:
    if trace_id is not None and str(trace_id).strip():
        return _uuidish(str(trace_id).strip())
    material = ""
    for name, value in (
        ("request_id", request_id),
        ("idempotency_key", idempotency_key),
        ("intent_id", intent_id),
        ("execution_job_id", execution_job_id),
        ("order_id", order_id),
        ("command_id", command_id),
    ):
        if value is not None and str(value).strip():
            material = f"{name}={str(value).strip()}"
            break
    if not material:
        return str(uuid4())
    return str(UUID(hex=sha256(material.encode("utf-8")).hexdigest()[:32]))


def trace_context(
    *,
    trace_id: Any | None = None,
    request_id: Any | None = None,
    idempotency_key: Any | None = None,
    intent_id: Any | None = None,
    execution_job_id: Any | None = None,
    order_id: Any | None = None,
    command_id: Any | None = None,
) -> TraceContext:
    req = _clean_optional(request_id)
    return TraceContext(
        trace_id=derive_trace_id(
            trace_id=trace_id,
            request_id=req,
            idempotency_key=idempotency_key,
            intent_id=intent_id,
            execution_job_id=execution_job_id,
            order_id=order_id,
            command_id=command_id,
        ),
        request_id=req,
    )


def inject_trace_context(
    payload: Mapping[str, Any] | None,
    *,
    trace_id: Any | None = None,
    request_id: Any | None = None,
    idempotency_key: Any | None = None,
    intent_id: Any | None = None,
    execution_job_id: Any | None = None,
    order_id: Any | None = None,
    command_id: Any | None = None,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(payload or {}))
    request_value = _clean_optional(request_id) or _clean_optional(result.get("request_id"))
    trace_value = trace_id or result.get("trace_id")
    context = trace_context(
        trace_id=trace_value,
        request_id=request_value,
        idempotency_key=idempotency_key or result.get("idempotency_key"),
        intent_id=intent_id or result.get("intent_id"),
        execution_job_id=execution_job_id or result.get("execution_job_id"),
        order_id=order_id or result.get("order_id") or result.get("client_order_id"),
        command_id=command_id or result.get("command_id"),
    )
    if context.request_id is not None:
        result.setdefault("request_id", context.request_id)
    result.setdefault("trace_id", context.trace_id)
    return result


def structured_log_line(
    event: str,
    *,
    level: str = "INFO",
    timestamp: datetime | None = None,
    **fields: Any,
) -> str:
    payload = {
        "ts": _aware(timestamp or datetime.now(timezone.utc)).isoformat(),
        "level": str(level).upper(),
        "event": str(event),
        **fields,
    }
    return json.dumps(_redact(payload), sort_keys=True, separators=(",", ":"), default=str)


def emit_structured_log(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    logger.log(level, structured_log_line(event, level=logging.getLevelName(level), **fields))


def _label_key(labels: Mapping[str, Any] | None) -> tuple[tuple[tuple[str, str], ...], dict[str, str]]:
    normalized = {
        str(key): str(value)
        for key, value in (labels or {}).items()
        if value is not None
    }
    key = tuple(sorted(normalized.items()))
    return key, normalized


def _metric_component(value: str) -> str:
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return "".join(ch for ch in text if ch.isalnum() or ch in {"_", "."}) or "unknown"


def _clean_optional(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _uuidish(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        return str(UUID(hex=sha256(value.encode("utf-8")).hexdigest()[:32]))


def _redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _secret_key(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(item_key): _redact(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


def _secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SECRET_KEY_PARTS)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
