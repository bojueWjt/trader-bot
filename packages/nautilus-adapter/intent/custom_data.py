from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from execution_domain.contracts import ApprovedTradeIntentV1

APPROVED_TRADE_INTENT_DATA_TYPE = "ApprovedTradeIntentV1"


@dataclass(frozen=True)
class ApprovedTradeIntentCustomData:
    """Nautilus-facing payload wrapper for a frozen ApprovedTradeIntentV1."""

    intent: ApprovedTradeIntentV1
    data_type: str = APPROVED_TRADE_INTENT_DATA_TYPE

    @classmethod
    def from_intent(
        cls, intent: ApprovedTradeIntentV1
    ) -> "ApprovedTradeIntentCustomData":
        return cls(intent=intent)

    def to_wire(self) -> dict[str, Any]:
        return {
            "data_type": self.data_type,
            "schema_version": self.intent.schema_version,
            "intent": self.intent.model_dump(mode="json"),
        }

    @classmethod
    def from_wire(
        cls, payload: Mapping[str, Any]
    ) -> "ApprovedTradeIntentCustomData":
        if payload.get("data_type") != APPROVED_TRADE_INTENT_DATA_TYPE:
            raise ValueError("unexpected approved trade intent data_type")
        intent = ApprovedTradeIntentV1.model_validate(payload.get("intent"))
        return cls(intent=intent)


_DATA_CLASS: Any = None


def _approved_trade_intent_data_class() -> Any:
    """Lazily define (once) the Nautilus ``Data`` subclass that carries an approved
    intent. Defined lazily so this module imports without ``nautilus_trader`` on dev
    hosts, and cached so the type identity is stable (DataType subscription matching
    keys on the class)."""

    global _DATA_CLASS
    if _DATA_CLASS is None:
        from nautilus_trader.core.data import Data  # type: ignore[import-not-found]

        class ApprovedTradeIntentData(Data):
            def __init__(self, ts_event: int, payload: dict[str, Any]) -> None:
                self._ts_event = ts_event
                self.payload = payload

            @property
            def ts_event(self) -> int:
                return self._ts_event

            @property
            def ts_init(self) -> int:
                return self._ts_event

        _DATA_CLASS = ApprovedTradeIntentData
    return _DATA_CLASS


def _approved_at_ns(intent: ApprovedTradeIntentV1) -> int:
    """UNIX epoch nanoseconds for the intent approval time (Nautilus ts unit)."""
    return int(intent.approved_at.timestamp() * 1_000_000_000)


def build_nautilus_custom_data(intent: ApprovedTradeIntentV1) -> Any:
    """Build Nautilus ``CustomData`` wrapping a frozen ``ApprovedTradeIntentV1``.

    Verified against nautilus_trader==1.227.0 (hk, 2026-06-19): ``DataType`` takes the
    ``Data`` subclass (a type) plus a metadata dict, and ``CustomData(data_type, data)``
    takes a ``Data`` instance.
    """

    payload = ApprovedTradeIntentCustomData.from_intent(intent).to_wire()
    try:
        from nautilus_trader.model.data import CustomData, DataType  # type: ignore
    except ImportError as exc:  # pragma: no cover - dev host has no Nautilus.
        raise RuntimeError("nautilus_trader is required to build CustomData") from exc

    data_cls = _approved_trade_intent_data_class()
    data_type = DataType(
        data_cls,
        metadata={
            "schema_version": intent.schema_version,
            "account_id": intent.account_id,
        },
    )
    return CustomData(data_type, data_cls(_approved_at_ns(intent), payload))


class NautilusCustomDataPublisher:
    """Publishes approved intents into Nautilus DataEngine or MessageBus."""

    def __init__(
        self,
        data_engine: Optional[Any] = None,
        message_bus: Optional[Any] = None,
    ) -> None:
        if data_engine is None and message_bus is None:
            raise ValueError("data_engine or message_bus is required")
        self._data_engine = data_engine
        self._message_bus = message_bus

    def publish(self, intent: ApprovedTradeIntentV1) -> None:
        custom_data = build_nautilus_custom_data(intent)

        if self._data_engine is not None:
            if hasattr(self._data_engine, "process"):
                self._data_engine.process(custom_data)
                return
            if hasattr(self._data_engine, "process_data"):
                self._data_engine.process_data(custom_data)
                return
            raise RuntimeError("data_engine does not expose process/process_data")

        if hasattr(self._message_bus, "publish"):
            self._message_bus.publish(custom_data)
            return
        raise RuntimeError("message_bus does not expose publish")
