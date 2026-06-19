from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import timedelta
from enum import Enum
import json
from typing import Any, Optional


try:
    from pydantic import BaseModel, ConfigDict, Field
except ModuleNotFoundError:
    ConfigDict = dict

    class _Field:
        def __init__(self, default: Any = None, default_factory: Any = None) -> None:
            self.default = default
            self.default_factory = default_factory

        def make_default(self) -> Any:
            if self.default_factory:
                return self.default_factory()
            return self.default

    def Field(default: Any = None, default_factory: Any = None) -> _Field:
        return _Field(default=default, default_factory=default_factory)

    class BaseModel:
        def __init__(self, **kwargs: Any) -> None:
            annotations: dict[str, Any] = {}
            for cls in reversed(self.__class__.mro()):
                cls_annotations = getattr(cls, "__annotations__", {})
                annotations.update(cls_annotations)

            for name in annotations:
                if name in kwargs:
                    setattr(self, name, kwargs[name])
                    continue

                default = getattr(self.__class__, name, None)
                if isinstance(default, _Field):
                    setattr(self, name, default.make_default())
                    continue

                setattr(self, name, default)

            for name, value in kwargs.items():
                if name in annotations:
                    continue
                setattr(self, name, value)

        @classmethod
        def model_validate_json(cls, data: str) -> Any:
            return cls(**json.loads(data))

        def model_dump(self, mode: str = "python") -> dict[str, Any]:
            return {
                name: _dump_value(value, mode)
                for name, value in self.__dict__.items()
                if not name.startswith("_")
            }


def _dump_value(value: Any, mode: str) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode=mode)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if mode == "json":
            return value.isoformat()
        return value
    if isinstance(value, list):
        return [_dump_value(item, mode) for item in value]
    if isinstance(value, dict):
        return {key: _dump_value(item, mode) for key, item in value.items()}
    return value


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def parse_utc_datetime(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo:
            return value
        return value.replace(tzinfo=UTC)

    if not value:
        return utc_now()

    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo:
        return parsed
    return parsed.replace(tzinfo=UTC)


class SignalStatus(str, Enum):
    RAW = "raw"
    PARSED = "parsed"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    RESERVED = "reserved"
    SENT_TO_FREQTRADE = "sent_to_freqtrade"
    ENTERED = "entered"
    PARTIALLY_EXITED = "partially_exited"
    EXITED = "exited"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"
    IGNORED = "ignored"
    BLOCKED_BY_RISK = "blocked_by_risk"


class MessageType(str, Enum):
    NEW_SIGNAL = "new_signal"
    UPDATE = "update"
    POSITION_SCREENSHOT = "position_screenshot"
    NOISE = "noise"


class DirectiveKind(str, Enum):
    CLOSE = "close"
    PARTIAL_CLOSE = "partial_close"
    MOVE_SL_TO_ENTRY = "move_sl_to_entry"
    MOVE_SL = "move_sl"


@dataclass
class PositionDirective:
    kind: DirectiveKind
    pair: str
    fraction: Optional[float] = None
    price: Optional[float] = None
    source_message_id: Optional[int] = None
    status: str = "pending"
    created_at: datetime = field(default_factory=utc_now)
    ttl_hours: int = 24
    directive_id: Optional[int] = None

    def is_expired(self, current_time: datetime | None = None) -> bool:
        active_time = current_time or utc_now()
        if active_time.tzinfo is None:
            active_time = active_time.replace(tzinfo=UTC)
        created_at = self.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return active_time >= created_at + timedelta(hours=self.ttl_hours)


class MediaAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    path: str
    mime_type: str = ""


class EntryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "cmp"
    primary_price: float | None = None
    price_min: float | None = None
    price_max: float | None = None
    dca_prices: list[float] = Field(default_factory=list)
    valid_from: datetime | None = None
    expires_at: datetime | None = None


class TakeProfit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: float
    close_pct: float = 100


class LeveragePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: int = 3
    max: int = 3
    selected: int = 3


class RiskPolicyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = "pending"
    reason_codes: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class TradingSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False, validate_assignment=True)

    signal_id: str
    source: str
    source_channel_id: str
    source_message_id: str
    # default: new_signal — backward compat with pre-M0-02 callers
    message_type: MessageType = MessageType.NEW_SIGNAL
    raw_text: str
    media: list[MediaAsset] = Field(default_factory=list)
    parser_version: str = "signal-parser-v1"
    pair_raw: str
    pair_freqtrade: str
    side: str
    entry: EntryPlan
    stop_loss: float | None = None
    take_profits: list[TakeProfit] = Field(default_factory=list)
    take_profit_parse_status: str = "parsed"
    leverage: LeveragePlan = Field(default_factory=LeveragePlan)
    confidence: float = 0.0
    status: SignalStatus = SignalStatus.RAW
    review_reason_codes: list[str] = Field(default_factory=list)
    risk_policy_result: RiskPolicyResult = Field(default_factory=RiskPolicyResult)
    received_at: datetime = Field(default_factory=utc_now)
    approved_at: datetime | None = None
    source_channel_name: str = ""


class PriceGeometryResult(BaseModel):
    valid: bool
    reason_codes: list[str] = Field(default_factory=list)


class ReservationResult(BaseModel):
    reserved: bool
    signal_id: str
    operation_type: str
    reason: str = ""


class ApprovalResult(BaseModel):
    approved: bool
    signal_id: str
    status: SignalStatus
    reason: str = ""
