from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


try:  # pragma: no cover - exercised in the Nautilus/container environment.
    from execution_domain.contracts import ExecutionEventEnvelopeV1 as _Envelope

    ExecutionEventEnvelopeV1 = _Envelope
except ModuleNotFoundError:  # pragma: no cover - local Py3.14 host lacks pydantic.

    @dataclass(frozen=True)
    class ExecutionEventEnvelopeV1:  # type: ignore[no-redef]
        """Local test fallback for hosts without ``pydantic`` installed.

        Runtime containers import the real contract from ``execution_domain``.
        This fallback keeps pure projection logic unit-testable on the documented
        host while preserving the same public fields.
        """

        event_id: str
        node_id: str
        account_id: str
        event_type: str
        ts_event: datetime
        ts_ingest: datetime
        payload: dict[str, Any] = field(default_factory=dict)
        schema_version: str = "1.0"
        intent_id: UUID | None = None
        client_order_id: str | None = None
        venue_order_id: str | None = None
        trade_id: str | None = None

        def model_dump(self, mode: str = "python") -> dict[str, Any]:
            del mode
            return {
                "schema_version": self.schema_version,
                "event_id": self.event_id,
                "node_id": self.node_id,
                "account_id": self.account_id,
                "intent_id": str(self.intent_id) if self.intent_id is not None else None,
                "client_order_id": self.client_order_id,
                "venue_order_id": self.venue_order_id,
                "trade_id": self.trade_id,
                "event_type": self.event_type,
                "ts_event": self.ts_event,
                "ts_ingest": self.ts_ingest,
                "payload": dict(self.payload),
            }


__all__ = ["ExecutionEventEnvelopeV1"]
