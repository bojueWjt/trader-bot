"""Control-plane seam for window B.

See ``docs/handoff/window-b/SEAM-contracts-v1.md``. Window B's Nautilus nodes talk
to window A's ``services/control-plane`` **only** through these interfaces. Concrete
HTTP clients and an in-memory mock (for node + dashboard tests) implement the
Protocols; strategy, projection, and command-handling code depend on the abstractions
so transport (pull vs. push) can change without touching execution logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Protocol, Sequence, runtime_checkable
from uuid import UUID

from .contracts import (
    ExecutionEventEnvelopeV1,
    ReconciliationState,
)
from .portfolio_baseline import portfolio_baseline_sha256

__all__ = [
    "TradingState",
    "IntentAckStatus",
    "CommandType",
    "CommandAckStatus",
    "REFRESH_EVIDENCE_ACCOUNT_IDS",
    "REFRESH_EVIDENCE_FRESHNESS_FIELDS",
    "REFRESH_EVIDENCE_HEARTBEAT_FIELDS",
    "IncidentSeverity",
    "IntentItem",
    "IntentBatch",
    "NodeCommand",
    "Heartbeat",
    "ProductionIncidentReport",
    "ProductionIncidentReceipt",
    "ProductionIncidentResolution",
    "ProductionIncidentResolutionReceipt",
    "NodeWriterIdentity",
    "ReleaseIdentity",
    "ReleaseGateReceipt",
    "PeerIdentityReceipt",
    "HeartbeatReceipt",
    "portfolio_baseline_sha256",
    "ControlPlaneIntentSource",
    "ExecutionEventSink",
    "ProductionIncidentSink",
    "ProductionIncidentResolutionSink",
    "NodeCommandChannel",
    "ControlPlaneOrderSource",
    "ControlPlaneSnapshotSource",
    "ControlPlaneClient",
]


REFRESH_EVIDENCE_ACCOUNT_IDS = (
    "account-a",
    "account-b",
    "account-c",
    "account-d",
)
REFRESH_EVIDENCE_FRESHNESS_FIELDS = (
    "last_seen_at",
    "positions_snapshot_at",
    "regular_orders_snapshot_at",
    "algo_orders_snapshot_at",
    "reconciliation_completed_at",
)
REFRESH_EVIDENCE_HEARTBEAT_FIELDS = tuple(
    field_name
    for field_name in REFRESH_EVIDENCE_FRESHNESS_FIELDS
    if field_name != "last_seen_at"
)


class TradingState(str, Enum):
    """Node-level execution gate (maps to Nautilus ``TradingState``)."""

    ACTIVE = "ACTIVE"
    HALTED = "HALTED"
    REDUCING = "REDUCING"


class IntentAckStatus(str, Enum):
    RECEIVED = "received"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXECUTED = "executed"
    EXPIRED = "expired"
    DUPLICATE = "duplicate"
    FAILED = "failed"


class CommandType(str, Enum):
    HALT = "halt"
    RESUME = "resume"
    SET_REDUCING = "set_reducing"
    CANCEL_ALL = "cancel_all"
    CLOSE_ALL = "close_all"
    REFRESH_EVIDENCE = "refresh_evidence"


class CommandAckStatus(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class IncidentSeverity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


@dataclass(frozen=True)
class IntentItem:
    cursor: str
    intent: Any


@dataclass(frozen=True)
class IntentBatch:
    items: Sequence[IntentItem]
    next_cursor: Optional[str]


@dataclass(frozen=True)
class NodeCommand:
    command_id: str
    type: CommandType
    args: dict[str, Any] = field(default_factory=dict)
    issued_at: Optional[datetime] = None


@dataclass(frozen=True)
class Heartbeat:
    account_id: str
    ts: datetime
    trading_state: TradingState
    readiness: bool
    projection_lag_ms: int
    reconciliation_state: ReconciliationState
    health_degraded_reasons: tuple[str, ...] = ()
    redis_fencing_epoch: Optional[str] = None
    runtime_generation: Optional[str] = None
    lease_fencing_token: Optional[int] = None
    heartbeat_sequence: Optional[int] = None
    last_event_id: Optional[str] = None
    release_id: Optional[str] = None
    image_digest: Optional[str] = None
    config_sha256: Optional[str] = None
    dependency_lock_sha256: Optional[str] = None
    schema_epoch: Optional[str] = None
    positions: tuple[dict[str, Any], ...] | None = None
    regular_orders: tuple[dict[str, Any], ...] | None = None
    algo_orders: tuple[dict[str, Any], ...] | None = None
    positions_snapshot_at: Optional[datetime] = None
    regular_orders_snapshot_at: Optional[datetime] = None
    algo_orders_snapshot_at: Optional[datetime] = None
    reconciliation_completed_at: Optional[datetime] = None
    open_orders: tuple[dict[str, Any], ...] | None = None

    def __post_init__(self) -> None:
        if not str(self.account_id).strip():
            raise ValueError("heartbeat account_id is required")


@dataclass(frozen=True)
class ProductionIncidentReport:
    account_id: str
    severity: IncidentSeverity
    reason: str
    summary: str

    def __post_init__(self) -> None:
        if not str(self.account_id).strip():
            raise ValueError("incident account_id is required")
        if not isinstance(self.severity, IncidentSeverity):
            raise ValueError("incident severity is invalid")
        if not str(self.reason).strip():
            raise ValueError("incident reason is required")
        if not str(self.summary).strip():
            raise ValueError("incident summary is required")


@dataclass(frozen=True)
class ProductionIncidentReceipt:
    incident_id: str
    account_id: str
    node_id: str
    reason: str
    severity: IncidentSeverity
    status: str
    summary: str
    opened_at: datetime
    deduplicated: bool


@dataclass(frozen=True)
class ProductionIncidentResolution:
    account_id: str
    reason: str
    summary: str

    def __post_init__(self) -> None:
        if not str(self.account_id).strip():
            raise ValueError("incident resolution account_id is required")
        if not str(self.reason).strip():
            raise ValueError("incident resolution reason is required")
        if not str(self.summary).strip():
            raise ValueError("incident resolution summary is required")


@dataclass(frozen=True)
class ProductionIncidentResolutionReceipt:
    account_id: str
    node_id: str
    reason: str
    status: str
    summary: str
    resolved_incident_ids: tuple[str, ...]
    resolved_count: int
    closed_at: datetime


@dataclass(frozen=True)
class NodeWriterIdentity:
    redis_fencing_epoch: str
    runtime_generation: str
    lease_fencing_token: int

    def __post_init__(self) -> None:
        epoch = str(self.redis_fencing_epoch or "").strip()
        try:
            parsed_epoch = UUID(epoch)
        except ValueError as exc:
            raise ValueError(
                "redis_fencing_epoch must be a canonical UUID4"
            ) from exc
        if parsed_epoch.version != 4 or str(parsed_epoch) != epoch:
            raise ValueError(
                "redis_fencing_epoch must be a canonical UUID4"
            )
        generation = str(self.runtime_generation or "").strip()
        if not generation:
            raise ValueError("runtime_generation is required")
        token = self.lease_fencing_token
        if isinstance(token, bool) or not isinstance(token, int):
            raise ValueError("lease_fencing_token must be an integer")
        if token < 1:
            raise ValueError("lease_fencing_token must be positive")
        object.__setattr__(self, "redis_fencing_epoch", epoch)
        object.__setattr__(self, "runtime_generation", generation)


@dataclass(frozen=True)
class ReleaseIdentity:
    release_id: Optional[str]
    image_digest: Optional[str]
    config_sha256: Optional[str]
    dependency_lock_sha256: Optional[str]
    schema_epoch: Optional[str]


@dataclass(frozen=True)
class ReleaseGateReceipt:
    status: str
    release_id: Optional[str]
    reviewed_manifest: Optional[ReleaseIdentity]
    rollout_phase: Optional[str] = None
    live_open_mode: Optional[str] = None
    phase_version: Optional[int] = None


@dataclass(frozen=True)
class PeerIdentityReceipt:
    node_id: str
    account_id: Optional[str]
    release_id: Optional[str]
    image_digest: Optional[str]
    config_sha256: Optional[str]
    dependency_lock_sha256: Optional[str]
    schema_epoch: Optional[str]
    redis_fencing_epoch: Optional[str]
    freshness_age_seconds: Optional[float]
    fresh: bool
    identity_matches: bool
    status: str = "unknown"


@dataclass(frozen=True)
class HeartbeatReceipt:
    release_gate: ReleaseGateReceipt
    peers: tuple[PeerIdentityReceipt, ...] = ()

    @property
    def requires_sticky_halt(self) -> bool:
        if self.release_gate.status != "pass":
            return True
        return False


@runtime_checkable
class ControlPlaneIntentSource(Protocol):
    """§1 inbound: durable, restart-safe consumption of approved intents."""

    def fetch_intents(
        self,
        account_id: str,
        after_cursor: Optional[str],
        limit: int,
        wait_ms: int = 0,
    ) -> IntentBatch: ...

    def ack_intent(
        self,
        account_id: str,
        node_id: str,
        intent_id: UUID,
        status: IntentAckStatus,
        detail: Optional[str] = None,
    ) -> None: ...


@runtime_checkable
class ExecutionEventSink(Protocol):
    """§2 outbound: at-least-once delivery with idempotent apply on ``event_id``."""

    def post_events(
        self, node_id: str, events: Sequence[ExecutionEventEnvelopeV1]
    ) -> list[str]:
        """Return the list of ``event_id`` values the control-plane has durably
        accepted; only those may leave the local spool."""
        ...

    def heartbeat(self, node_id: str, hb: Heartbeat) -> HeartbeatReceipt: ...


@runtime_checkable
class ProductionIncidentSink(Protocol):
    """Durable node-authenticated production incident reporting."""

    def report_incident(
        self,
        node_id: str,
        report: ProductionIncidentReport,
    ) -> ProductionIncidentReceipt: ...


@runtime_checkable
class ProductionIncidentResolutionSink(Protocol):
    """Node-authenticated resolution of recovered production incidents."""

    def resolve_incident(
        self,
        node_id: str,
        resolution: ProductionIncidentResolution,
    ) -> ProductionIncidentResolutionReceipt: ...


@runtime_checkable
class NodeCommandChannel(Protocol):
    """§3: operator/kill-switch commands; every target node must ack."""

    def poll_commands(self, node_id: str, after: Optional[str]) -> list[NodeCommand]: ...

    def ack_command(
        self,
        node_id: str,
        command_id: str,
        status: CommandAckStatus,
        result: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None: ...


@runtime_checkable
class ControlPlaneOrderSource(Protocol):
    """Account-scoped non-terminal order projection used by reconciliation."""

    def fetch_open_orders(
        self,
        account_id: str,
    ) -> Sequence[Mapping[str, Any]]: ...


@runtime_checkable
class ControlPlaneSnapshotSource(Protocol):
    """§4 read-model freshness used by node readiness/safety checks."""

    def latest_snapshot_generated_at(self, account_id: str) -> Optional[datetime]: ...


@runtime_checkable
class ControlPlaneClient(
    ControlPlaneIntentSource,
    ExecutionEventSink,
    NodeCommandChannel,
    ControlPlaneOrderSource,
    ControlPlaneSnapshotSource,
    Protocol,
):
    """Aggregate node-facing control-plane client (intents + events + commands)."""
