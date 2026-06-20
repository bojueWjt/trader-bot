"""Control-plane seam for window B.

See ``docs/handoff/window-b/SEAM-contracts-v1.md``. Window B's Nautilus nodes talk
to window A's ``services/control-plane`` **only** through these interfaces. Concrete
HTTP clients and an in-memory mock (for node + dashboard tests) implement the
Protocols; strategy, projection, and command-handling code depend on the abstractions
so transport (pull vs. push) can change without touching execution logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Protocol, Sequence, runtime_checkable
from uuid import UUID

from .contracts import (
    ApprovedTradeIntentV1,
    ExecutionEventEnvelopeV1,
    ReconciliationState,
)

__all__ = [
    "TradingState",
    "IntentAckStatus",
    "CommandType",
    "CommandAckStatus",
    "IntentItem",
    "IntentBatch",
    "NodeCommand",
    "Heartbeat",
    "ControlPlaneIntentSource",
    "ExecutionEventSink",
    "NodeCommandChannel",
    "ControlPlaneSnapshotSource",
    "ControlPlaneClient",
]


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


class CommandAckStatus(str, Enum):
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class IntentItem:
    cursor: str
    intent: ApprovedTradeIntentV1


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
    ts: datetime
    trading_state: TradingState
    readiness: bool
    projection_lag_ms: int
    reconciliation_state: ReconciliationState
    last_event_id: Optional[str] = None


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

    def heartbeat(self, node_id: str, hb: Heartbeat) -> None: ...


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
class ControlPlaneSnapshotSource(Protocol):
    """§4 read-model freshness used by node readiness/safety checks."""

    def latest_snapshot_generated_at(self, account_id: str) -> Optional[datetime]: ...


@runtime_checkable
class ControlPlaneClient(
    ControlPlaneIntentSource,
    ExecutionEventSink,
    NodeCommandChannel,
    ControlPlaneSnapshotSource,
    Protocol,
):
    """Aggregate node-facing control-plane client (intents + events + commands)."""
