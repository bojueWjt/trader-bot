from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional


DEFAULT_LIVE_ENTRY_NOTIONAL_KEY = "*"


@dataclass(frozen=True)
class RiskLimitConfig:
    """Serializable risk limits for Nautilus ``LiveRiskEngineConfig``.

    This config is intentionally plain Python data. Nautilus config classes are
    msgspec.Structs, so callers must pass only serializable constructor values.
    """

    max_notional_per_order: Mapping[str, str]
    max_order_submit_rate: str
    max_order_modify_rate: str
    instrument_precision: Mapping[str, Any] = field(default_factory=dict)


def build_live_risk_engine_kwargs(config: RiskLimitConfig) -> dict[str, Any]:
    """Return kwargs for Nautilus ``LiveRiskEngineConfig`` with risk bypass disabled.

    TODO(host-verify): confirm the exact field names against pinned
    ``nautilus_trader==1.227.0`` in the hk container. The candidate fields used
    here are ``bypass``, ``max_order_submit_rate``, ``max_order_modify_rate``,
    and ``max_notional_per_order``.

    TODO(host-verify): confirm whether ``max_notional_per_order`` values should
    be passed as strings, Decimals, or ints. We keep strings locally to preserve
    decimal formatting while staying msgspec-serializable.

    TODO(host-verify): confirm the exact Nautilus RiskEngine path for enforcing
    instrument precision. Binance instruments expose precision/increments; this
    module does not invent hard-coded symbol rules.
    """

    inventory = _validate_limit_config(config)
    max_notional_per_order = {
        instrument_id: format(cap, "f")
        for instrument_id, cap in inventory
        if instrument_id != DEFAULT_LIVE_ENTRY_NOTIONAL_KEY
    }
    return {
        "bypass": False,
        "max_order_submit_rate": config.max_order_submit_rate,
        "max_order_modify_rate": config.max_order_modify_rate,
        "max_notional_per_order": max_notional_per_order,
    }


def build_live_risk_engine_config(config: RiskLimitConfig) -> Any:
    """Build a Nautilus ``LiveRiskEngineConfig`` object on hosts with Nautilus.

    Local pudu-mini verification only compiles this module; the import is lazy so
    Python 3.14 without Nautilus can still run pure tests.
    """

    from nautilus_trader.config import LiveRiskEngineConfig  # type: ignore[import-not-found]

    return LiveRiskEngineConfig(**build_live_risk_engine_kwargs(config))


def build_risk_engine_config(config: RiskLimitConfig) -> Any:
    """Build a non-live Nautilus ``RiskEngineConfig`` object on Nautilus hosts."""

    from nautilus_trader.config import RiskEngineConfig  # type: ignore[import-not-found]

    return RiskEngineConfig(**build_live_risk_engine_kwargs(config))


def build_live_entry_notional_inventory(
    config: RiskLimitConfig,
) -> tuple[
    tuple[str, str],
    ...,
]:
    """Return validated entry caps for the serializable strategy config."""

    inventory = _validate_limit_config(config)
    return tuple(
        (instrument_id, format(cap, "f"))
        for instrument_id, cap in inventory
    )


def _validate_limit_config(
    config: RiskLimitConfig,
) -> tuple[tuple[str, Decimal], ...]:
    if not config.max_notional_per_order:
        raise ValueError("max_notional_per_order must not be empty")
    inventory: list[tuple[str, Decimal]] = []
    for instrument_id, value in config.max_notional_per_order.items():
        if not instrument_id:
            raise ValueError("max_notional_per_order instrument id must be non-empty")
        cap = _positive_decimal(
            str(value),
            f"max_notional_per_order[{instrument_id}]",
        )
        inventory.append((str(instrument_id), cap))
    _parse_rate(config.max_order_submit_rate, "max_order_submit_rate")
    _parse_rate(config.max_order_modify_rate, "max_order_modify_rate")
    return tuple(sorted(inventory))


def _parse_rate(value: str, label: str) -> tuple[int, str]:
    if "/" not in value:
        raise ValueError(f"{label} must use Nautilus rate format count/HH:MM:SS")
    count_raw, interval = value.split("/", 1)
    try:
        count = int(count_raw)
    except ValueError as exc:
        raise ValueError(f"{label} count must be an integer") from exc
    if count <= 0:
        raise ValueError(f"{label} count must be positive")
    parts = interval.split(":")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"{label} interval must be HH:MM:SS")
    return count, interval


def _positive_decimal(value: str, label: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a decimal value") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{label} must be positive")
    return number
