from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedExecutionError:
    category: ErrorCategory
    reason: str
    raw: str
    stage: str | None = None


PERMANENT_MARKERS = (
    "invalid quantity",
    "invalid price",
    "precision",
    "min_notional",
    "insufficient margin",
    "insufficient balance",
    "post only would take",
    "rejected",
    "denied",
    "filter failure",
)

RETRYABLE_MARKERS = (
    "rate limit",
    "too many requests",
    "503",
    "502",
    "504",
    "temporarily unavailable",
    "exchange unavailable",
    "service unavailable",
    "throttled",
)

UNKNOWN_MARKERS = (
    "timeout",
    "connection lost",
    "network",
    "unknown",
    "socket closed",
    "submit_uncertain",
)


def classify_execution_error(error: Any, *, stage: str | None = None) -> ClassifiedExecutionError:
    raw = _raw_error(error)
    text = f"{stage or ''} {raw}".lower()
    if any(marker in text for marker in UNKNOWN_MARKERS):
        return ClassifiedExecutionError(ErrorCategory.UNKNOWN, "network_uncertain", raw, stage)
    if any(marker in text for marker in PERMANENT_MARKERS):
        return ClassifiedExecutionError(ErrorCategory.PERMANENT, "permanent_rejection", raw, stage)
    if any(marker in text for marker in RETRYABLE_MARKERS):
        return ClassifiedExecutionError(ErrorCategory.RETRYABLE, "transient_failure", raw, stage)
    if stage and stage.endswith("_uncertain"):
        return ClassifiedExecutionError(ErrorCategory.UNKNOWN, "network_uncertain", raw, stage)
    return ClassifiedExecutionError(ErrorCategory.UNKNOWN, "unclassified_execution_error", raw, stage)


def _raw_error(error: Any) -> str:
    if isinstance(error, BaseException):
        return repr(error)
    return str(error)
