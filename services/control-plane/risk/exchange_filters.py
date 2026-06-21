from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from risk_config import RiskConfig, decimal_value


@dataclass(frozen=True)
class InstrumentMetadata:
    instrument_id: str
    min_notional: Decimal
    min_quantity: Decimal
    quantity_step: Decimal
    price_tick: Decimal
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    max_leverage: Decimal | None = None
    allowed_margin_modes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in (
            "min_notional",
            "min_quantity",
            "quantity_step",
            "price_tick",
            "min_price",
            "max_price",
            "max_leverage",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, decimal_value(value))


class ExchangeMetadataSource(Protocol):
    def get_instrument(self, instrument_id: str) -> InstrumentMetadata:
        ...


@dataclass(frozen=True)
class OrderFilterRequest:
    instrument_id: str
    quantity: Decimal
    price: Decimal
    leverage: Decimal | None = None
    margin_mode: str | None = None

    def __post_init__(self) -> None:
        for name in ("quantity", "price", "leverage"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, decimal_value(value))


@dataclass(frozen=True)
class FilterResult:
    status: str
    reason: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def validate_exchange_filters(
    request: OrderFilterRequest,
    *,
    metadata_source: ExchangeMetadataSource,
    config: RiskConfig | None = None,
) -> FilterResult:
    config = config or RiskConfig()
    metadata = metadata_source.get_instrument(request.instrument_id)
    notional = request.quantity * request.price
    checks = [
        _check("min_quantity", request.quantity >= metadata.min_quantity, request.quantity, metadata.min_quantity),
        _check("quantity_step", _aligned(request.quantity, metadata.quantity_step), request.quantity, metadata.quantity_step),
        _check("min_notional", notional >= metadata.min_notional, notional, metadata.min_notional),
        _check("price_tick", _aligned(request.price, metadata.price_tick), request.price, metadata.price_tick),
    ]
    if metadata.min_price is not None:
        checks.append(_check("min_price", request.price >= metadata.min_price, request.price, metadata.min_price))
    if metadata.max_price is not None:
        checks.append(_check("max_price", request.price <= metadata.max_price, request.price, metadata.max_price))
    if request.leverage is not None:
        max_leverage = min(config.max_leverage, metadata.max_leverage or config.max_leverage)
        checks.append(_check("leverage", request.leverage <= max_leverage, request.leverage, max_leverage))
    if metadata.allowed_margin_modes and request.margin_mode is not None:
        checks.append(
            {
                "name": "margin_mode",
                "passed": request.margin_mode in metadata.allowed_margin_modes,
                "value": request.margin_mode,
                "allowed": sorted(metadata.allowed_margin_modes),
            }
        )
    failed = [check for check in checks if not check["passed"]]
    return FilterResult(
        status="rejected" if failed else "approved",
        reason=failed[0]["name"] if failed else "filters passed",
        checks=checks,
        metadata={
            "instrument_id": metadata.instrument_id,
            "min_notional": metadata.min_notional,
            "min_quantity": metadata.min_quantity,
            "quantity_step": metadata.quantity_step,
            "price_tick": metadata.price_tick,
            "max_leverage": metadata.max_leverage,
        },
    )


def _aligned(value: Decimal, increment: Decimal) -> bool:
    if increment <= 0:
        return True
    return value % increment == 0


def _check(name: str, passed: bool, value: Decimal, limit: Decimal) -> dict[str, Any]:
    return {"name": name, "passed": passed, "value": value, "limit": limit}

