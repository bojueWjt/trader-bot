from __future__ import annotations

import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

_CONTROL_PLANE = Path(__file__).resolve().parents[1]
if str(_CONTROL_PLANE) not in sys.path:
    sys.path.insert(0, str(_CONTROL_PLANE))

from order_management.identifiers import canonical_account_id, canonical_instrument_key  # noqa: E402

from risk_config import RiskConfig, decimal_value  # noqa: E402


DEFAULT_CORRELATED_GROUPS = {"majors": ("BTCUSDT", "ETHUSDT")}


@dataclass(frozen=True)
class ExposureSnapshot:
    account_id: str | None
    instrument_notional: dict[str, Decimal]
    total_notional: Decimal
    total_risk_amount: Decimal = Decimal("0")


@dataclass(frozen=True)
class ProposedExposure:
    account_id: str
    instrument_id: str
    notional: Decimal
    risk_amount: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", canonical_account_id(self.account_id))
        object.__setattr__(self, "instrument_id", canonical_instrument_key(self.instrument_id))
        object.__setattr__(self, "notional", decimal_value(self.notional))
        object.__setattr__(self, "risk_amount", decimal_value(self.risk_amount))


@dataclass(frozen=True)
class ExposureCheckResult:
    status: str
    reason: str
    projected_instrument_exposure: Decimal
    projected_correlated_exposure: Decimal
    projected_total_risk: Decimal
    headroom: dict[str, Decimal] = field(default_factory=dict)
    correlated_group: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)


def exposure_from_positions(
    positions: list[dict[str, Any]],
    *,
    account_id: str | None = None,
) -> ExposureSnapshot:
    normalized_account = canonical_account_id(account_id) if account_id is not None else None
    instrument_notional: dict[str, Decimal] = {}
    total = Decimal("0")
    total_risk = Decimal("0")
    for position in positions:
        if normalized_account is not None and canonical_account_id(position.get("account_id")) != normalized_account:
            continue
        if str(position.get("status") or "open").lower() not in {"open", "external"}:
            continue
        key = canonical_instrument_key(str(position.get("instrument_id") or position.get("venue_symbol")))
        notional = _position_notional(position)
        instrument_notional[key] = instrument_notional.get(key, Decimal("0")) + notional
        total += notional
        payload = position.get("payload") or {}
        total_risk += decimal_value(position.get("risk_amount") or payload.get("risk_amount"), Decimal("0"))
    return ExposureSnapshot(normalized_account, instrument_notional, total, total_risk)


def check_exposure(
    snapshot: ExposureSnapshot,
    proposed: ProposedExposure,
    config: RiskConfig | None = None,
    *,
    correlated_groups: dict[str, tuple[str, ...]] | None = None,
) -> ExposureCheckResult:
    config = config or RiskConfig()
    groups = correlated_groups or DEFAULT_CORRELATED_GROUPS
    key = canonical_instrument_key(proposed.instrument_id)
    existing_instrument = snapshot.instrument_notional.get(key, Decimal("0"))
    group_name, group_existing = _correlated_existing(snapshot, key, groups)
    projected_instrument = existing_instrument + proposed.notional
    projected_correlated = group_existing + proposed.notional
    projected_total_risk = snapshot.total_risk_amount + proposed.risk_amount
    headroom = {
        "instrument": max(config.max_instrument_exposure - existing_instrument, Decimal("0")),
        "correlated": max(config.max_correlated_exposure - group_existing, Decimal("0")),
    }
    checks = [
        {
            "name": "instrument_exposure",
            "passed": projected_instrument <= config.max_instrument_exposure,
            "projected": projected_instrument,
            "limit": config.max_instrument_exposure,
        },
        {
            "name": "correlated_exposure",
            "passed": projected_correlated <= config.max_correlated_exposure,
            "projected": projected_correlated,
            "limit": config.max_correlated_exposure,
        },
    ]
    for check in checks:
        if not check["passed"]:
            return ExposureCheckResult(
                status="rejected",
                reason=f"{check['name']} cap exceeded",
                projected_instrument_exposure=projected_instrument,
                projected_correlated_exposure=projected_correlated,
                projected_total_risk=projected_total_risk,
                headroom=headroom,
                correlated_group=group_name,
                checks=checks,
            )
    return ExposureCheckResult(
        status="approved",
        reason="exposure within caps",
        projected_instrument_exposure=projected_instrument,
        projected_correlated_exposure=projected_correlated,
        projected_total_risk=projected_total_risk,
        headroom=headroom,
        correlated_group=group_name,
        checks=checks,
    )


def _position_notional(position: dict[str, Any]) -> Decimal:
    if position.get("notional") is not None:
        return abs(decimal_value(position["notional"]))
    quantity = abs(decimal_value(position.get("quantity")))
    price = decimal_value(position.get("avg_entry_price") or position.get("mark_price"))
    return quantity * price


def _correlated_existing(
    snapshot: ExposureSnapshot,
    instrument_key: str,
    correlated_groups: dict[str, tuple[str, ...]],
) -> tuple[str | None, Decimal]:
    for name, members in correlated_groups.items():
        canonical_members = {canonical_instrument_key(member) for member in members}
        if instrument_key in canonical_members:
            return name, sum(
                notional
                for key, notional in snapshot.instrument_notional.items()
                if canonical_instrument_key(key) in canonical_members
            )
    return None, snapshot.instrument_notional.get(instrument_key, Decimal("0"))

