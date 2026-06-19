from __future__ import annotations

from enum import Enum


class MessageType(str, Enum):
    NEW_SIGNAL = "new_signal"
    POSITION_UPDATE = "position_update"
    CLOSE_UPDATE = "close_update"
    ANALYSIS = "analysis"
    NOISE = "noise"
    AMBIGUOUS = "ambiguous"


class HermesAction(str, Enum):
    OPEN_POSITION = "open_position"
    ADD_POSITION = "add_position"
    PARTIAL_CLOSE = "partial_close"
    CLOSE_POSITION = "close_position"
    MOVE_STOP_LOSS = "move_stop_loss"
    MOVE_STOP_TO_ENTRY = "move_stop_to_entry"
    REPLACE_TAKE_PROFITS = "replace_take_profits"
    HOLD = "hold"
    IGNORE = "ignore"
    NEEDS_REVIEW = "needs_review"


class ApprovedTradeAction(str, Enum):
    OPEN_POSITION = "open_position"
    ADD_POSITION = "add_position"
    PARTIAL_CLOSE = "partial_close"
    CLOSE_POSITION = "close_position"
    MOVE_STOP_LOSS = "move_stop_loss"
    MOVE_STOP_TO_ENTRY = "move_stop_to_entry"
    REPLACE_TAKE_PROFITS = "replace_take_profits"


class AccountScope(str, Enum):
    UNASSIGNED = "unassigned"
    SINGLE = "single"
    ALL = "all"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


class EntryType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    ZONE = "zone"
    NONE = "none"


class ReconciliationState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


class OutboxStatus(str, Enum):
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


CONTRACT_ENUM_VALUES = {
    "message_type": [item.value for item in MessageType],
    "hermes_action": [item.value for item in HermesAction],
    "approved_trade_action": [item.value for item in ApprovedTradeAction],
    "account_scope": [item.value for item in AccountScope],
    "side": [item.value for item in PositionSide],
    "entry.type": [item.value for item in EntryType],
    "reconciliation_state": [item.value for item in ReconciliationState],
}
