"""Deterministic idempotency helpers for order-management commands."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

__all__ = [
    "RequestId",
    "execution_job_key",
    "intent_idempotency_key",
]


def _material(*parts: object | None) -> str:
    return "|".join("" if part is None else str(part) for part in parts)


def _sha256_hex(*parts: object | None) -> str:
    return hashlib.sha256(_material(*parts).encode("utf-8")).hexdigest()


def intent_idempotency_key(
    decision_id: object,
    action: str,
    instrument: str | None,
    account: str | None,
) -> str:
    return _sha256_hex(decision_id, action, instrument or "", account or "")


def execution_job_key(
    intent_id: object,
    action: str,
    instrument: str | None,
    account: str | None,
) -> str:
    return _sha256_hex("execution_job", intent_id, action, instrument or "", account or "")


@dataclass(frozen=True)
class RequestId:
    value: str

    def __post_init__(self) -> None:
        value = str(self.value).strip()
        if not value:
            raise ValueError("request_id must not be empty")
        object.__setattr__(self, "value", value)

    @classmethod
    def from_value(cls, value: str) -> "RequestId":
        return cls(value)

    @classmethod
    def for_material(cls, *parts: object | None) -> "RequestId":
        return cls("req_" + _sha256_hex(*parts)[:32])

    def __str__(self) -> str:
        return self.value
