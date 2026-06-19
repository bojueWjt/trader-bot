"""Deterministic risk policy configuration for the Decision Gateway.

Pure data + loaders; no semantics. All thresholds are explicit so risk decisions
are reproducible and auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

UPDATE_ACTIONS = frozenset(
    {"partial_close", "close_position", "move_stop_loss", "move_stop_to_entry", "replace_take_profits"}
)
OPENING_ACTIONS = frozenset({"open_position", "add_position"})
NON_ACTIONABLE_ACTIONS = frozenset({"hold", "ignore", "needs_review"})
UPDATE_MESSAGE_TYPES = frozenset({"position_update", "close_update"})


@dataclass(frozen=True)
class RiskPolicy:
    # instrument allow-list and price/qty precision (decimal places)
    instrument_whitelist: frozenset[str] = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"})
    price_precision: int = 2
    # per-trade caps
    max_risk_fraction: float = 0.02
    default_risk_fraction: float = 0.01
    max_leverage: float = 10.0
    max_notional: float = 50_000.0
    # exposure caps
    max_instrument_notional: float = 100_000.0
    max_total_risk_fraction: float = 0.10
    correlated_groups: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {"majors": ("BTCUSDT", "ETHUSDT")}
    )
    max_correlated_notional: float = 150_000.0
    # freshness window for a decision (seconds since created_at)
    freshness_seconds: int = 1800
    # account routing default when scope is single without an explicit target
    default_account_id: str | None = None
    # intent validity if the decision does not carry valid_until (seconds)
    intent_ttl_seconds: int = 900

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RiskPolicy":
        if not data:
            return cls()
        base = cls()
        fields: dict[str, Any] = {}
        for key, value in data.items():
            if not hasattr(base, key):
                continue
            if key == "instrument_whitelist":
                fields[key] = frozenset(value)
            elif key == "correlated_groups":
                fields[key] = {group: tuple(members) for group, members in value.items()}
            else:
                fields[key] = value
        return replace(base, **fields)

    def correlated_group_for(self, instrument: str) -> tuple[str, str, ...] | None:
        for name, members in self.correlated_groups.items():
            if instrument in members:
                return (name, *members)
        return None
