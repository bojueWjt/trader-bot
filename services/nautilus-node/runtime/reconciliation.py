from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from re import fullmatch
from typing import Any, Iterable, Mapping, Protocol
from uuid import UUID

from execution_domain.contracts import ReconciliationState


class ReconciliationProofStatus(str, Enum):
    MISSING = "missing"
    IN_FLIGHT = "in_flight"
    RELEASE_IDENTITY_MISSING = "release_identity_missing"
    IDENTITY_MISMATCH = "identity_mismatch"
    GENERATION_MISMATCH = "generation_mismatch"
    UNHEALTHY = "unhealthy"
    INVALID_TIME = "invalid_time"
    STALE = "stale"
    HEALTHY = "healthy"


@dataclass(frozen=True)
class ReconciliationDatasetSummary:
    count: int
    digest: str

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("reconciliation summary count must be non-negative")
        if fullmatch(r"[0-9a-f]{64}", self.digest) is None:
            raise ValueError(
                "reconciliation summary digest must be a lowercase sha256"
            )

    @classmethod
    def from_records(cls, records: Iterable[Any]) -> "ReconciliationDatasetSummary":
        normalized = [_normalize_record(record) for record in records]
        serialized = [_canonical_json(record) for record in normalized]
        serialized.sort()
        payload = f"[{','.join(serialized)}]".encode("utf-8")
        return cls(count=len(normalized), digest=sha256(payload).hexdigest())


@dataclass(frozen=True)
class ReconciliationProof:
    account_id: str
    node_id: str
    release_id: str
    state: ReconciliationState
    orders: ReconciliationDatasetSummary
    positions: ReconciliationDatasetSummary
    fills: ReconciliationDatasetSummary
    completed_at: datetime
    generation: int = 0

    def __post_init__(self) -> None:
        for label, value in (
            ("account_id", self.account_id),
            ("node_id", self.node_id),
            ("release_id", self.release_id),
        ):
            if not value.strip():
                raise ValueError(f"reconciliation proof {label} is required")
        if self.completed_at.tzinfo is None:
            raise ValueError("reconciliation proof completed_at must be timezone-aware")
        if not isinstance(self.state, ReconciliationState):
            raise ValueError("reconciliation proof state must be ReconciliationState")
        if self.generation < 0:
            raise ValueError("reconciliation proof generation must be non-negative")


@dataclass(frozen=True)
class ReconciliationProofSnapshot:
    status: ReconciliationProofStatus
    proof_age_seconds: float | None
    fresh: bool
    completed_at: datetime | None
    reason: str


class ReconciliationProofSink(Protocol):
    def record_reconciliation_proof(self, proof: ReconciliationProof) -> None: ...


class ReconciliationCompletionCallback:
    """Host seam invoked only after exchange-first reconciliation completes."""

    def __init__(self, sink: ReconciliationProofSink) -> None:
        self._sink = sink

    def __call__(
        self,
        *,
        account_id: str,
        node_id: str,
        release_id: str,
        state: ReconciliationState | str,
        orders: Iterable[Any],
        positions: Iterable[Any],
        fills: Iterable[Any],
        completed_at: datetime | None = None,
        generation: int | None = None,
    ) -> ReconciliationProof:
        completed = completed_at
        if completed is None:
            completed = datetime.now(timezone.utc)
        proof_generation = generation
        if proof_generation is None:
            proof_generation = int(
                getattr(self._sink, "reconciliation_generation", 0) or 0
            )
        proof = ReconciliationProof(
            account_id=account_id,
            node_id=node_id,
            release_id=release_id,
            state=_coerce_state(state),
            orders=ReconciliationDatasetSummary.from_records(orders),
            positions=ReconciliationDatasetSummary.from_records(positions),
            fills=ReconciliationDatasetSummary.from_records(fills),
            completed_at=completed,
            generation=proof_generation,
        )
        self._sink.record_reconciliation_proof(proof)
        return proof


def evaluate_reconciliation_proof(
    proof: ReconciliationProof | None,
    *,
    expected_account_id: str,
    expected_node_id: str,
    expected_release_id: str,
    now: datetime,
    max_age: timedelta,
    expected_generation: int = 0,
    in_flight: bool = False,
) -> ReconciliationProofSnapshot:
    if in_flight:
        return ReconciliationProofSnapshot(
            status=ReconciliationProofStatus.IN_FLIGHT,
            proof_age_seconds=None,
            fresh=False,
            completed_at=None,
            reason="reconciliation is in flight",
        )
    if proof is None:
        return ReconciliationProofSnapshot(
            status=ReconciliationProofStatus.MISSING,
            proof_age_seconds=None,
            fresh=False,
            completed_at=None,
            reason="reconciliation proof is missing",
        )
    if proof.generation != expected_generation:
        return _snapshot(
            ReconciliationProofStatus.GENERATION_MISMATCH,
            proof,
            now,
            "reconciliation proof generation does not match runtime",
        )
    if not expected_release_id:
        return _snapshot(
            ReconciliationProofStatus.RELEASE_IDENTITY_MISSING,
            proof,
            now,
            "runtime release identity is missing",
        )
    expected_identity = (
        expected_account_id,
        expected_node_id,
        expected_release_id,
    )
    proof_identity = (
        proof.account_id,
        proof.node_id,
        proof.release_id,
    )
    if proof_identity != expected_identity:
        return _snapshot(
            ReconciliationProofStatus.IDENTITY_MISMATCH,
            proof,
            now,
            "reconciliation proof identity does not match runtime",
        )
    if proof.state is not ReconciliationState.HEALTHY:
        return _snapshot(
            ReconciliationProofStatus.UNHEALTHY,
            proof,
            now,
            "reconciliation proof is not healthy",
        )
    age = now - proof.completed_at
    if age < timedelta(0):
        return _snapshot(
            ReconciliationProofStatus.INVALID_TIME,
            proof,
            now,
            "reconciliation proof completed_at is in the future",
        )
    if age > max_age:
        return _snapshot(
            ReconciliationProofStatus.STALE,
            proof,
            now,
            "reconciliation proof is stale",
        )
    return _snapshot(
        ReconciliationProofStatus.HEALTHY,
        proof,
        now,
        "",
        fresh=True,
    )


def _snapshot(
    status: ReconciliationProofStatus,
    proof: ReconciliationProof,
    now: datetime,
    reason: str,
    *,
    fresh: bool = False,
) -> ReconciliationProofSnapshot:
    age = (now - proof.completed_at).total_seconds()
    return ReconciliationProofSnapshot(
        status=status,
        proof_age_seconds=max(age, 0.0),
        fresh=fresh,
        completed_at=proof.completed_at,
        reason=reason,
    )


def _coerce_state(state: ReconciliationState | str) -> ReconciliationState:
    if isinstance(state, ReconciliationState):
        return state
    return ReconciliationState(str(state))


def _normalize_record(record: Any) -> Any:
    if is_dataclass(record) and not isinstance(record, type):
        return _normalize_record(asdict(record))
    if isinstance(record, Mapping):
        return {
            str(key): _normalize_record(value)
            for key, value in record.items()
        }
    if isinstance(record, (list, tuple)):
        return [_normalize_record(value) for value in record]
    if isinstance(record, datetime):
        return record.isoformat()
    if isinstance(record, Enum):
        return _normalize_record(record.value)
    if isinstance(record, (Decimal, UUID)):
        return str(record)
    if record is None or isinstance(record, (str, int, float, bool)):
        return record
    to_dict = getattr(record, "to_dict", None)
    if callable(to_dict):
        return _normalize_record(to_dict())
    raise TypeError(
        "reconciliation records must be mappings, dataclasses, or stable scalars"
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
