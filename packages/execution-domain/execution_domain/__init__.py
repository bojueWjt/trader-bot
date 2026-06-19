"""execution-domain — window B typed contracts (contracts-v1 view) and the
control-plane client seam for the Nautilus execution layer.

See ``docs/handoff/window-b/SEAM-contracts-v1.md`` and
``tests/nautilus/_fixtures/contracts-v1/`` for the authoritative frozen shapes.
"""

from __future__ import annotations

from .contracts import (
    ApprovedTradeIntentV1,
    DataQualityEnvelopeV1,
    EXIT_ACTIONS,
    ExecutionEventEnvelopeV1,
    IntentAction,
    POSITION_REQUIRED_ACTIONS,
    ReconciliationState,
    RiskBudget,
)
from .control_plane import (
    CommandAckStatus,
    CommandType,
    ControlPlaneClient,
    ControlPlaneIntentSource,
    ControlPlaneSnapshotSource,
    ExecutionEventSink,
    Heartbeat,
    IntentAckStatus,
    IntentBatch,
    IntentItem,
    NodeCommand,
    NodeCommandChannel,
    TradingState,
)

__all__ = [
    "ApprovedTradeIntentV1",
    "DataQualityEnvelopeV1",
    "ExecutionEventEnvelopeV1",
    "IntentAction",
    "ReconciliationState",
    "RiskBudget",
    "EXIT_ACTIONS",
    "POSITION_REQUIRED_ACTIONS",
    "CommandAckStatus",
    "CommandType",
    "ControlPlaneClient",
    "ControlPlaneIntentSource",
    "ControlPlaneSnapshotSource",
    "ExecutionEventSink",
    "Heartbeat",
    "IntentAckStatus",
    "IntentBatch",
    "IntentItem",
    "NodeCommand",
    "NodeCommandChannel",
    "TradingState",
]
