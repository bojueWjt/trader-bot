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
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "IntentAction",
    "ReconciliationState",
    "RiskBudget",
    "Execution",
    "Protection",
    "TakeProfitLevel",
    "Sizing",
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


class ExecutionZone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low: Optional[Decimal] = Field(default=None, gt=0)
    high: Optional[Decimal] = Field(default=None, gt=0)


class Execution(BaseModel):
    """Optional order-management execution hints.

    Absence means legacy/default v1 behavior: the execution layer consumes the
    existing open ``order_plan`` and applies runtime defaults.
    """

    model_config = ConfigDict(extra="forbid")

    order_type: Optional[Literal["market", "limit", "zone"]] = None
    time_in_force: Optional[Literal["GTC", "IOC", "FOK", "GTD"]] = None
    post_only: Optional[bool] = None
    reduce_only: Optional[bool] = None
    max_slippage_bps: Optional[Decimal] = Field(default=None, ge=0)
    limit_price: Optional[Decimal] = Field(default=None, gt=0)
    zone: Optional[ExecutionZone] = None


class StopSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Optional[Literal["stop_market", "stop_limit"]] = None
    trigger_basis: Optional[Literal["mark", "last"]] = None
    price: Optional[Decimal] = Field(default=None, gt=0)
    distance_pct: Optional[Decimal] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _price_xor_distance(self) -> "StopSpec":
        if (self.price is None) == (self.distance_pct is None):
            raise ValueError("stop requires exactly one of price or distance_pct")
        return self


class TakeProfitLevel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: Optional[Decimal] = Field(default=None, gt=0)
    r_multiple: Optional[Decimal] = Field(default=None, gt=0)
    fraction: Optional[Decimal] = Field(default=None, gt=0, le=1)
    quantity: Optional[Decimal] = Field(default=None, gt=0)
    time_in_force: Optional[Literal["GTC", "IOC", "FOK", "GTD"]] = None

    @model_validator(mode="after")
    def _price_and_size_are_xor(self) -> "TakeProfitLevel":
        if (self.price is None) == (self.r_multiple is None):
            raise ValueError("take_profit level requires exactly one of price or r_multiple")
        if (self.fraction is None) == (self.quantity is None):
            raise ValueError("take_profit level requires exactly one of fraction or quantity")
        return self


class Breakeven(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    trigger_r: Optional[Decimal] = Field(default=None, ge=0)
    offset_bps: Optional[Decimal] = Field(default=None, ge=0)


class Trailing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    activation_r: Optional[Decimal] = Field(default=None, ge=0)
    callback_rate: Optional[Decimal] = Field(default=None, gt=0)
    min_step: Optional[Decimal] = Field(default=None, ge=0)


class Protection(BaseModel):
    """Optional order-management protection hints.

    Absence means legacy/default v1 behavior: no extra protection metadata is
    required by the contract envelope, and runtime policy supplies defaults.
    """

    model_config = ConfigDict(extra="forbid")

    require_stop: Optional[bool] = None
    stop: Optional[StopSpec] = None
    take_profits: Optional[list[TakeProfitLevel]] = None
    breakeven: Optional[Breakeven] = None
    trailing: Optional[Trailing] = None

    @model_validator(mode="after")
    def _take_profit_fraction_cap(self) -> "Protection":
        total = sum(
            (level.fraction for level in self.take_profits or [] if level.fraction is not None),
            Decimal("0"),
        )
        if total > Decimal("1.0"):
            raise ValueError("take_profit fractions must sum to <= 1.0")
        return self


class Sizing(BaseModel):
    """Optional order-management sizing hint.

    Absence means legacy/default v1 behavior: sizing remains in the existing
    risk budget/order plan path.
    """

    model_config = ConfigDict(extra="forbid")

    quantity: Optional[Decimal] = Field(default=None, gt=0)
    fraction: Optional[Decimal] = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def _quantity_xor_fraction(self) -> "Sizing":
        if (self.quantity is None) == (self.fraction is None):
            raise ValueError("sizing requires exactly one of quantity or fraction")
        return self


class ApprovedTradeIntentV1(BaseModel):
    """Inbound to window B: consumed from the control-plane outbox/stream and
    carried into Nautilus as CustomData (PLAN §3.2).

    The order-management ``execution``, ``protection``, and ``sizing`` objects
    are optional additive v1 fields. Their absence is the compatibility strategy:
    legacy intents continue through the existing ``order_plan``/runtime-default
    path without requiring a contract version bump.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    intent_id: UUID
    decision_id: UUID
    risk_decision_id: UUID
    account_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)  # Nautilus InstrumentId string
    action: IntentAction
    order_plan: dict[str, Any]
    execution: Optional[Execution] = None
    protection: Optional[Protection] = None
    sizing: Optional[Sizing] = None
    risk_budget: RiskBudget
    target_position_id: Optional[str] = None
    valid_until: datetime
    idempotency_key: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")  # sha256 hex
    approved_at: datetime

    @model_validator(mode="after")
    def _management_actions_do_not_carry_entry_fields(self) -> "ApprovedTradeIntentV1":
        if self.action not in POSITION_REQUIRED_ACTIONS:
            return self
        if self.sizing is not None:
            raise ValueError("management actions must not carry sizing")
        if self.execution is not None:
            entry_only = [
                name
                for name in ("order_type", "zone", "limit_price")
                if getattr(self.execution, name) is not None
            ]
            if entry_only:
                raise ValueError(
                    "management actions must not carry entry-only execution fields: "
                    + ", ".join(entry_only)
                )
        return self


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
