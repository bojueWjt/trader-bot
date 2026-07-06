"""Pure zone-ladder expansion for approved zone signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Literal


# §2 已定参数，改动须过再校准流程.
CHASE_WINDOW = Decimal("0.0035")
NARROW_ZONE_THRESHOLD = Decimal("0.0015")
INVALIDATION_BUFFER = Decimal("0.2")
MAX_TTL = timedelta(hours=48)
POLICY = "zone-ladder-v1.0"
PLAN_VERSION = "1.1"

TRANCHE_LEVELS = (
    ("t1_near", 1, Decimal("0"), Decimal("0.55")),
    ("t2_mid", 2, Decimal("0.50"), Decimal("0.30")),
    ("t3_deep", 3, Decimal("0.85"), Decimal("0.15")),
)


@dataclass(frozen=True)
class ZoneSignal:
    side: str
    price_min: float
    price_max: float
    stop_loss: float | None
    take_profits: tuple[float, ...]
    valid_until: datetime | None


@dataclass(frozen=True)
class MarketContext:
    p0: float
    now: datetime
    price_increment: str
    quantity_increment: str


@dataclass(frozen=True)
class RiskContext:
    risk_budget_usd: float
    max_notional: float
    max_leverage: float


@dataclass(frozen=True)
class ExpandDenied:
    reason: str
    detail: str


@dataclass
class _Tranche:
    tranche_id: str
    seq: int
    price: Decimal
    risk_weight: Decimal
    style: Literal["chase", "rest"]
    limit_cap: Decimal | None = None


def expand_zone_to_plan(
    signal: ZoneSignal,
    market: MarketContext,
    risk: RiskContext,
) -> dict | ExpandDenied:
    side = signal.side.lower()
    if side not in ("long", "short"):
        raise ValueError(f"unsupported side: {signal.side}")

    price_min = _decimal(signal.price_min)
    price_max = _decimal(signal.price_max)
    if price_max <= price_min:
        raise ValueError("price_max must be greater than price_min")

    price_increment = _decimal(market.price_increment)
    quantity_increment = _decimal(market.quantity_increment)
    if price_increment <= 0 or quantity_increment <= 0:
        raise ValueError("increments must be positive")

    p0 = _decimal(market.p0)
    height = price_max - price_min
    near = price_max if side == "long" else price_min
    far = price_min if side == "long" else price_max
    depth = _depth(side, p0, near, height)

    if depth > Decimal("1"):
        return ExpandDenied(reason="needs_review", detail="zone_penetrated")
    if signal.stop_loss is None:
        return ExpandDenied(reason="needs_review", detail="missing_stop_loss")

    stop_loss = _decimal(signal.stop_loss)
    mode: Literal["zone_ladder", "single_limit"]
    if height / near < NARROW_ZONE_THRESHOLD:
        mode = "single_limit"
        tranches = [_single_limit_tranche(side, price_min, price_max, price_increment)]
    else:
        mode = "zone_ladder"
        tranches = _zone_ladder_tranches(side, p0, near, height, depth, price_increment)

    sized = _sized_tranches(
        tranches,
        stop_loss=stop_loss,
        risk_budget=_decimal(risk.risk_budget_usd),
        max_notional=_decimal(risk.max_notional),
        quantity_increment=quantity_increment,
    )
    if isinstance(sized, ExpandDenied):
        return sized

    return _order_plan(
        signal,
        market,
        side=side,
        mode=mode,
        tranches=sized,
        stop_loss=stop_loss,
        far=far,
        height=height,
    )


def _zone_ladder_tranches(
    side: str,
    p0: Decimal,
    near: Decimal,
    height: Decimal,
    depth: Decimal,
    price_increment: Decimal,
) -> list[_Tranche]:
    base = [
        _Tranche(
            tranche_id=tranche_id,
            seq=seq,
            price=_tranche_price(side, near, height, tranche_depth, price_increment),
            risk_weight=weight,
            style="rest",
        )
        for tranche_id, seq, tranche_depth, weight in TRANCHE_LEVELS
    ]

    if Decimal("0") < depth <= Decimal("1"):
        base[0].style = "chase"
        base[0].limit_cap = _limit_cap(side, near)
        return _keep_stale_favorable_tranches(side, p0, base)

    premium = _premium(side, p0, near)
    if premium <= CHASE_WINDOW:
        base[0].style = "chase"
        base[0].limit_cap = _limit_cap(side, near)
    return base


def _single_limit_tranche(
    side: str,
    price_min: Decimal,
    price_max: Decimal,
    price_increment: Decimal,
) -> _Tranche:
    midpoint = (price_min + price_max) / Decimal("2")
    return _Tranche(
        tranche_id="t1_near",
        seq=1,
        price=_rest_price(side, midpoint, price_increment),
        risk_weight=Decimal("1"),
        style="rest",
    )


def _keep_stale_favorable_tranches(
    side: str,
    p0: Decimal,
    tranches: list[_Tranche],
) -> list[_Tranche]:
    kept = [tranches[0]]
    carry = Decimal("0")

    for tranche in tranches[1:]:
        favorable = tranche.price < p0 if side == "long" else tranche.price > p0
        if favorable:
            tranche.risk_weight += carry
            carry = Decimal("0")
            kept.append(tranche)
        else:
            carry += tranche.risk_weight

    if carry:
        kept[-1].risk_weight += carry
    return kept


def _sized_tranches(
    tranches: list[_Tranche],
    *,
    stop_loss: Decimal,
    risk_budget: Decimal,
    max_notional: Decimal,
    quantity_increment: Decimal,
) -> list[tuple[_Tranche, Decimal]] | ExpandDenied:
    working = [
        _Tranche(t.tranche_id, t.seq, t.price, t.risk_weight, t.style, t.limit_cap)
        for t in tranches
    ]

    for _ in range(len(tranches) + 1):
        sized = _size_once(
            working,
            stop_loss=stop_loss,
            risk_budget=risk_budget,
            max_notional=max_notional,
            quantity_increment=quantity_increment,
        )
        if isinstance(sized, ExpandDenied):
            return sized

        zero_index = next((i for i, (_, qty) in enumerate(sized) if qty <= 0), None)
        if zero_index is None:
            return sized

        if len(working) == 1:
            return ExpandDenied(reason="needs_review", detail="quantity_below_increment")

        dropped = working.pop(zero_index)
        target_index = zero_index if zero_index < len(working) else len(working) - 1
        working[target_index].risk_weight += dropped.risk_weight

    return ExpandDenied(reason="needs_review", detail="quantity_below_increment")


def _size_once(
    tranches: list[_Tranche],
    *,
    stop_loss: Decimal,
    risk_budget: Decimal,
    max_notional: Decimal,
    quantity_increment: Decimal,
) -> list[tuple[_Tranche, Decimal]] | ExpandDenied:
    raw: list[tuple[_Tranche, Decimal]] = []
    for tranche in tranches:
        distance = abs(tranche.price - stop_loss)
        if distance <= 0:
            return ExpandDenied(reason="needs_review", detail="invalid_stop_loss")
        raw_qty = risk_budget * tranche.risk_weight / distance
        raw.append((tranche, raw_qty))

    total_notional = sum(qty * tranche.price for tranche, qty in raw)
    if max_notional <= 0:
        scale = Decimal("0")
    elif total_notional > max_notional:
        scale = max_notional / total_notional
    else:
        scale = Decimal("1")

    return [
        (tranche, _floor_to_increment(qty * scale, quantity_increment))
        for tranche, qty in raw
    ]


def _order_plan(
    signal: ZoneSignal,
    market: MarketContext,
    *,
    side: str,
    mode: str,
    tranches: list[tuple[_Tranche, Decimal]],
    stop_loss: Decimal,
    far: Decimal,
    height: Decimal,
) -> dict:
    return {
        "plan_version": PLAN_VERSION,
        "policy": POLICY,
        "mode": mode,
        "tranches": [
            _tranche_payload(tranche, quantity, _decimal(market.quantity_increment))
            for tranche, quantity in tranches
        ],
        "stop_loss": _float(stop_loss),
        "take_profits": [_float(_decimal(tp)) for tp in signal.take_profits],
        "invalidation": _invalidation(signal, market, side=side, far=far, height=height),
    }


def _tranche_payload(
    tranche: _Tranche,
    quantity: Decimal,
    quantity_increment: Decimal,
) -> dict:
    payload = {
        "tranche_id": tranche.tranche_id,
        "seq": tranche.seq,
        "price": _float(tranche.price),
        "quantity": _quantity_string(quantity, quantity_increment),
        "risk_weight": _float(tranche.risk_weight),
        "style": tranche.style,
    }
    if tranche.limit_cap is not None:
        payload["limit_cap"] = _float(tranche.limit_cap)
    return payload


def _invalidation(
    signal: ZoneSignal,
    market: MarketContext,
    *,
    side: str,
    far: Decimal,
    height: Decimal,
) -> dict:
    if side == "long":
        close_beyond = far - INVALIDATION_BUFFER * height
    else:
        close_beyond = far + INVALIDATION_BUFFER * height

    invalidation = {}
    if signal.take_profits:
        invalidation["cancel_on_price_touch"] = _float(_decimal(signal.take_profits[0]))
    invalidation["cancel_on_close_beyond"] = _float(close_beyond)
    invalidation["close_bar"] = "15m"
    invalidation["expires_at"] = _expires_at(market.now, signal.valid_until)
    return invalidation


def _depth(side: str, p0: Decimal, near: Decimal, height: Decimal) -> Decimal:
    if side == "long":
        return (near - p0) / height
    return (p0 - near) / height


def _premium(side: str, p0: Decimal, near: Decimal) -> Decimal:
    if side == "long":
        return (p0 - near) / near
    return (near - p0) / near


def _tranche_price(
    side: str,
    near: Decimal,
    height: Decimal,
    depth: Decimal,
    price_increment: Decimal,
) -> Decimal:
    raw = near - depth * height if side == "long" else near + depth * height
    return _rest_price(side, raw, price_increment)


def _rest_price(side: str, price: Decimal, price_increment: Decimal) -> Decimal:
    rounding = ROUND_FLOOR if side == "long" else ROUND_CEILING
    return _round_to_increment(price, price_increment, rounding)


def _limit_cap(side: str, near: Decimal) -> Decimal:
    if side == "long":
        return near * (Decimal("1") + CHASE_WINDOW)
    return near * (Decimal("1") - CHASE_WINDOW)


def _round_to_increment(
    value: Decimal,
    increment: Decimal,
    rounding: str,
) -> Decimal:
    return (value / increment).to_integral_value(rounding=rounding) * increment


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    return _round_to_increment(value, increment, ROUND_FLOOR)


def _quantity_string(value: Decimal, increment: Decimal) -> str:
    places = max(0, -increment.as_tuple().exponent)
    return f"{value:.{places}f}" if places else f"{value:.0f}"


def _expires_at(now: datetime, valid_until: datetime | None) -> str:
    now_utc = _as_utc(now)
    ttl = now_utc + MAX_TTL
    valid_until_utc = _as_utc(valid_until) if valid_until is not None else ttl
    expires_at = min(valid_until_utc, ttl)
    return expires_at.isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _decimal(value: float | str | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _float(value: Decimal) -> float:
    return float(value)
