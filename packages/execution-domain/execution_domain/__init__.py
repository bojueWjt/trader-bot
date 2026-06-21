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
    Execution,
    ExecutionEventEnvelopeV1,
    IntentAction,
    POSITION_REQUIRED_ACTIONS,
    Protection,
    ReconciliationState,
    RiskBudget,
    Sizing,
    TakeProfitLevel,
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
from .idempotency import RequestId, execution_job_key, intent_idempotency_key

__all__ = [
    "ApprovedTradeIntentV1",
    "DataQualityEnvelopeV1",
    "ExecutionEventEnvelopeV1",
    "IntentAction",
    "ReconciliationState",
    "RiskBudget",
    "Execution",
    "Protection",
    "TakeProfitLevel",
    "Sizing",
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
    "RequestId",
    "execution_job_key",
    "intent_idempotency_key",
]
