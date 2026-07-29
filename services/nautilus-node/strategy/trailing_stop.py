from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from strategy.intent_execution_planner import encode_client_order_id
from strategy.protection import PositionProtectionSnapshot


@dataclass(frozen=True)
class TrailingStopSpec:
    callback_rate: Decimal
    activation_price: Decimal | None = None


@dataclass(frozen=True)
class TrailingStopOrderPlan:
    intent_id: UUID
    client_order_id: str
    tags: tuple[str, ...]
    instrument_id: str
    side: str
    order_type: str
    quantity: Decimal
    reduce_only: bool
    native: bool
    callback_rate: Decimal
    activation_price: Decimal | None = None


def build_trailing_stop_order(
    *,
    intent_id: str | UUID,
    account_id: str,
    instrument_id: str,
    position: PositionProtectionSnapshot,
    spec: TrailingStopSpec,
    exchange_supports_native: bool,
) -> TrailingStopOrderPlan:
    intent_uuid = UUID(str(intent_id))
    return TrailingStopOrderPlan(
        intent_id=intent_uuid,
        client_order_id=encode_client_order_id(intent_uuid, sequence=1),
        tags=(
            f"intent_id={intent_uuid}",
            "action=install_trailing_stop",
            f"account_id={account_id}",
            "lifecycle_role=stop_loss",
            f"position_id={position.position_key}",
            "trailing=true",
        ),
        instrument_id=instrument_id,
        side="SELL" if str(position.side).lower() in {"long", "buy"} else "BUY",
        order_type="TRAILING_STOP_MARKET" if exchange_supports_native else "STOP_MARKET",
        quantity=abs(Decimal(str(position.quantity))),
        reduce_only=True,
        native=exchange_supports_native,
        callback_rate=Decimal(str(spec.callback_rate)),
        activation_price=spec.activation_price,
    )

