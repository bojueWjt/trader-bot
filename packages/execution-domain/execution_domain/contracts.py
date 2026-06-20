"""contracts-v1 typed view for window B (Nautilus execution layer).

Authoritative source: ``packages/contracts`` (window A). This module is window B's
local, **merge-safe** typed mirror of the frozen contracts (PLAN v3.0 §2.2/§3),
validated against ``tests/nautilus/_fixtures/contracts-v1/*.schema.json``. Do not
diverge from the frozen shapes; raise a contract-change-request instead.

At integration (window C) these models are reconciled to import from
``packages/contracts`` and the fixtures are asserted equal to the authoritative
schemas.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "IntentAction",
    "ReconciliationState",
    "RiskBudget",
    "ApprovedTradeIntentV1",
    "ExecutionEventEnvelopeV1",
    "DataQualityEnvelopeV1",
    "EXIT_ACTIONS",
    "POSITION_REQUIRED_ACTIONS",
]


class IntentAction(str, Enum):
    OPEN_POSITION = "open_position"
    ADD_POSITION = "add_position"
    PARTIAL_CLOSE = "partial_close"
    CLOSE_POSITION = "close_position"
    MOVE_STOP_LOSS = "move_stop_loss"
    MOVE_STOP_TO_ENTRY = "move_stop_to_entry"
    REPLACE_TAKE_PROFITS = "replace_take_profits"


#: Actions that may only reduce an existing position (all exits are reduce_only).
EXIT_ACTIONS = frozenset(
    {IntentAction.PARTIAL_CLOSE, IntentAction.CLOSE_POSITION}
)

#: Actions that must reference an existing, uniquely-identified target position.
POSITION_REQUIRED_ACTIONS = frozenset(
    {
        IntentAction.PARTIAL_CLOSE,
        IntentAction.CLOSE_POSITION,
        IntentAction.MOVE_STOP_LOSS,
        IntentAction.MOVE_STOP_TO_ENTRY,
        IntentAction.REPLACE_TAKE_PROFITS,
    }
)


class ReconciliationState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


class RiskBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_fraction: float = Field(ge=0)
    max_notional: float = Field(ge=0)
    max_leverage: float = Field(ge=0)


class ApprovedTradeIntentV1(BaseModel):
    """Inbound to window B: consumed from the control-plane outbox/stream and
    carried into Nautilus as CustomData (PLAN §3.2)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    intent_id: UUID
    decision_id: UUID
    risk_decision_id: UUID
    account_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)  # Nautilus InstrumentId string
    action: IntentAction
    order_plan: dict[str, Any]
    risk_budget: RiskBudget
    target_position_id: Optional[str] = None
    valid_until: datetime
    idempotency_key: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")  # sha256 hex
    approved_at: datetime


class ExecutionEventEnvelopeV1(BaseModel):
    """Outbound from window B: one per Nautilus order/position/account event;
    idempotent on ``event_id`` (PLAN §3.3)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    event_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    intent_id: Optional[UUID] = None
    client_order_id: Optional[str] = None
    venue_order_id: Optional[str] = None
    trade_id: Optional[str] = None
    event_type: str = Field(min_length=1)
    ts_event: datetime
    ts_ingest: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class DataQualityEnvelopeV1(BaseModel):
    """Wraps every dashboard/snapshot read-model response (PLAN §2.2). ``extra`` is
    allowed because the concrete read-model payload travels alongside these fields."""

    model_config = ConfigDict(extra="allow")

    data_source: str
    snapshot_id: UUID
    generated_at: datetime
    last_execution_event_at: Optional[datetime] = None
    projection_lag_ms: int = Field(ge=0)
    stale: bool
    missing_nodes: list[str] = Field(default_factory=list)
    reconciliation_state: ReconciliationState
