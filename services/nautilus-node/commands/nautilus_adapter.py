from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .handler import PositionSnapshot


@dataclass(frozen=True)
class NautilusCommandAdapter:
    """Adapter boundary for live Nautilus Strategy/Cache command methods.

    TODO(host-verify): execute in the hk container against
    ``nautilus_trader==1.227.0`` to confirm exact signatures for
    ``Strategy.cancel_all_orders``, ``Strategy.close_all_positions``, and
    ``Strategy.close_position`` when using Binance USDT-M futures instruments.
    This adapter keeps those uncertain method names and ``reduce_only`` semantics
    isolated from the pure command state machine.
    """

    strategy: Any
    cache: Any

    def cancel_all(self, instrument_ids: Optional[tuple[str, ...]] = None) -> dict[str, Any]:
        if instrument_ids is None:
            self.strategy.cancel_all_orders()
        else:
            for instrument_id in instrument_ids:
                self.strategy.cancel_all_orders(instrument_id=instrument_id)
        return {"cancel_requested": True}

    def wait_for_no_working_orders(self) -> None:
        # TODO(host-verify): replace with event-driven confirmation from Nautilus
        # order status events once B-07 projection/event mapping is available.
        return None

    def open_positions(self, instrument_ids: Optional[tuple[str, ...]] = None) -> list[PositionSnapshot]:
        # TODO(host-verify): confirm cache open-position iterator API and the
        # position id/instrument id attributes for Nautilus 1.227.0.
        positions = list(self.cache.positions_open())
        if instrument_ids is not None:
            wanted = set(instrument_ids)
            positions = [
                position
                for position in positions
                if str(position.instrument_id) in wanted
            ]
        return [
            PositionSnapshot(
                position_id=str(position.id),
                instrument_id=str(position.instrument_id),
            )
            for position in positions
        ]

    def close_position_reduce_only(self, position: PositionSnapshot) -> dict[str, Any]:
        self.strategy.close_position(position_id=position.position_id, reduce_only=True)
        return {"position_id": position.position_id, "reduce_only": True}

    def wait_until_flat(self, positions: list[PositionSnapshot]) -> None:
        # TODO(host-verify): replace with event-driven position close confirmation
        # once B-07 publishes Nautilus position event semantics.
        del positions
        return None
