"""Robot-owned position ledger with explicit rebaseline markers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .order_ownership import is_robot_client_order_id

OWNERSHIP_REBASELINE_EVENT_TYPE = "OwnershipRebaseline"
OWNERSHIP_REBASELINE_SCHEMA_VERSION = "ownership-rebaseline/v1"
ROBOT_FILL_EVENT_TYPES = ("OrderFilled", "OrderPartiallyFilled")

_ORDER_SIDE = {
    1: "buy",
    2: "sell",
    "1": "buy",
    "2": "sell",
    "BUY": "buy",
    "SELL": "sell",
    "LONG": "buy",
    "SHORT": "sell",
    "buy": "buy",
    "sell": "sell",
    "long": "buy",
    "short": "sell",
}


class OwnershipLedgerError(ValueError):
    """Raised when ownership evidence cannot be interpreted safely."""


@dataclass(frozen=True)
class OwnershipBaseline:
    account_id: str
    symbol: str
    quantity: Decimal
    event_id: str
    ts_event: datetime
    created_at: datetime
    reason: str
    adjudicated_by: str
    request_id: str
    exchange_quantity: Decimal | None
    manual_quantity: Decimal | None
    payload: Mapping[str, Any]

    @property
    def cutoff(self) -> tuple[datetime, datetime, str]:
        return (self.ts_event, self.created_at, self.event_id)


@dataclass(frozen=True)
class OwnershipBalance:
    account_id: str
    symbol: str
    baseline_quantity: Decimal
    post_baseline_fill_quantity: Decimal
    quantity: Decimal
    fill_event_count: int
    baseline: OwnershipBaseline | None


def canonical_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    return text.split("-", 1)[0].split(".", 1)[0]


def load_robot_owned_balance(
    cur,
    *,
    account_id: str,
    symbol: str,
    apply_rebaseline: bool = True,
) -> OwnershipBalance:
    target_symbol = canonical_symbol(symbol)
    if not target_symbol:
        raise OwnershipLedgerError("ownership symbol is required")
    balances = load_robot_owned_balances(
        cur,
        account_id=account_id,
        apply_rebaseline=apply_rebaseline,
    )
    balance = balances.get(target_symbol)
    if balance is not None:
        return balance
    return OwnershipBalance(
        account_id=account_id,
        symbol=target_symbol,
        baseline_quantity=Decimal(0),
        post_baseline_fill_quantity=Decimal(0),
        quantity=Decimal(0),
        fill_event_count=0,
        baseline=None,
    )


def load_robot_owned_balances(
    cur,
    *,
    account_id: str,
    apply_rebaseline: bool = True,
) -> dict[str, OwnershipBalance]:
    baselines: dict[str, OwnershipBaseline] = {}
    if apply_rebaseline:
        baselines = load_latest_ownership_baselines(
            cur,
            account_id=account_id,
        )

    fill_totals: dict[str, Decimal] = {}
    fill_counts: dict[str, int] = {}
    cur.execute(
        """
        SELECT ee.client_order_id,
               ee.event_type,
               ee.ts_event,
               ee.created_at,
               ee.event_id,
               ee.payload,
               op.instrument_id
        FROM execution_events AS ee
        LEFT JOIN orders_projection AS op
          ON op.account_id=ee.account_id
         AND op.client_order_id=ee.client_order_id
        WHERE ee.account_id=%s
          AND ee.client_order_id ~ '^B[0-9a-f]{32}[0-9]{2}$'
          AND ee.event_type IN ('OrderFilled', 'OrderPartiallyFilled')
        ORDER BY ee.ts_event, ee.created_at, ee.event_id
        """,
        (account_id,),
    )
    for row in cur.fetchall():
        (
            client_order_id,
            event_type,
            ts_event,
            created_at,
            event_id,
            raw_payload,
            projection_instrument,
        ) = row
        if not is_robot_client_order_id(client_order_id):
            continue
        if not isinstance(raw_payload, Mapping):
            raise OwnershipLedgerError(
                "robot-owned fill evidence is invalid"
            )
        event_symbol = _fill_symbol(
            raw_payload,
            projection_instrument=projection_instrument,
        )
        if not event_symbol:
            continue
        baseline = baselines.get(event_symbol)
        if baseline is not None:
            event_cutoff = (ts_event, created_at, str(event_id))
            if event_cutoff <= baseline.cutoff:
                continue
        signed_quantity = signed_fill_quantity(
            raw_payload,
            event_type=str(event_type or ""),
        )
        current = fill_totals.get(event_symbol, Decimal(0))
        fill_totals[event_symbol] = current + signed_quantity
        fill_counts[event_symbol] = fill_counts.get(event_symbol, 0) + 1

    symbols = set(baselines)
    symbols.update(fill_totals)
    balances: dict[str, OwnershipBalance] = {}
    for event_symbol in sorted(symbols):
        baseline = baselines.get(event_symbol)
        baseline_quantity = Decimal(0)
        if baseline is not None:
            baseline_quantity = baseline.quantity
        fill_quantity = fill_totals.get(event_symbol, Decimal(0))
        balances[event_symbol] = OwnershipBalance(
            account_id=account_id,
            symbol=event_symbol,
            baseline_quantity=baseline_quantity,
            post_baseline_fill_quantity=fill_quantity,
            quantity=baseline_quantity + fill_quantity,
            fill_event_count=fill_counts.get(event_symbol, 0),
            baseline=baseline,
        )
    return balances


def load_latest_ownership_baselines(
    cur,
    *,
    account_id: str,
) -> dict[str, OwnershipBaseline]:
    cur.execute(
        """
        SELECT event_id,
               ts_event,
               created_at,
               payload
        FROM execution_events
        WHERE account_id=%s
          AND event_type=%s
        ORDER BY ts_event DESC, created_at DESC, event_id DESC
        """,
        (account_id, OWNERSHIP_REBASELINE_EVENT_TYPE),
    )
    baselines: dict[str, OwnershipBaseline] = {}
    for event_id, ts_event, created_at, raw_payload in cur.fetchall():
        baseline = _parse_baseline(
            account_id=account_id,
            event_id=str(event_id),
            ts_event=ts_event,
            created_at=created_at,
            raw_payload=raw_payload,
        )
        if baseline.symbol in baselines:
            continue
        baselines[baseline.symbol] = baseline
    return baselines


def select_flat_ownership_anchor(
    cur,
    *,
    account_id: str,
    candidates: Sequence[str],
) -> str:
    normalized = []
    for candidate in candidates:
        symbol = canonical_symbol(candidate)
        if symbol and symbol not in normalized:
            normalized.append(symbol)
    if not normalized:
        raise OwnershipLedgerError("ownership anchor candidates are required")
    for symbol in normalized:
        balance = load_robot_owned_balance(
            cur,
            account_id=account_id,
            symbol=symbol,
        )
        if balance.quantity == 0:
            return symbol
    raise OwnershipLedgerError(
        "robot-owned target symbol position is not flat"
    )


def signed_fill_quantity(
    payload: Mapping[str, Any],
    *,
    event_type: str,
) -> Decimal:
    if event_type not in ROBOT_FILL_EVENT_TYPES:
        return Decimal(0)
    raw_quantity = None
    for field_name in (
        "last_qty",
        "last_fill_qty",
        "filled_qty",
        "quantity",
    ):
        value = payload.get(field_name)
        if value is None:
            continue
        raw_quantity = value
        break
    try:
        quantity = Decimal(str(raw_quantity))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OwnershipLedgerError(
            "robot-owned fill evidence is invalid"
        ) from exc
    if not quantity.is_finite() or quantity <= 0:
        raise OwnershipLedgerError(
            "robot-owned fill evidence is invalid"
        )

    side_value = payload.get("side")
    if side_value is None:
        side_value = payload.get("order_side")
    side = _ORDER_SIDE.get(side_value)
    if side is None:
        side = _ORDER_SIDE.get(str(side_value or "").strip())
    if side == "buy":
        return quantity
    if side == "sell":
        return -quantity
    raise OwnershipLedgerError("robot-owned fill evidence is invalid")


def build_rebaseline_payload(
    *,
    symbol: str,
    baseline_quantity: Decimal,
    reason: str,
    adjudicated_by: str,
    request_id: str,
    exchange_quantity: Decimal,
    manual_quantity: Decimal,
    previous_robot_owned_quantity: Decimal,
    raw_robot_fill_quantity: Decimal,
) -> dict[str, str]:
    target_symbol = canonical_symbol(symbol)
    if not target_symbol:
        raise OwnershipLedgerError("ownership symbol is required")
    if not reason.strip():
        raise OwnershipLedgerError("ownership rebaseline reason is required")
    if not adjudicated_by.strip():
        raise OwnershipLedgerError(
            "ownership rebaseline adjudicator is required"
        )
    if not request_id.strip():
        raise OwnershipLedgerError(
            "ownership rebaseline request id is required"
        )
    for quantity in (
        baseline_quantity,
        exchange_quantity,
        manual_quantity,
        previous_robot_owned_quantity,
        raw_robot_fill_quantity,
    ):
        if not quantity.is_finite():
            raise OwnershipLedgerError(
                "ownership rebaseline quantity is invalid"
            )
    if baseline_quantity + manual_quantity != exchange_quantity:
        raise OwnershipLedgerError(
            "exchange quantity must equal robot baseline plus manual quantity"
        )
    return {
        "ownership_schema_version": (
            OWNERSHIP_REBASELINE_SCHEMA_VERSION
        ),
        "symbol": target_symbol,
        "instrument_id": f"{target_symbol}-PERP.BINANCE",
        "baseline_quantity": format(baseline_quantity, "f"),
        "exchange_quantity_at_baseline": format(
            exchange_quantity,
            "f",
        ),
        "manual_quantity_at_baseline": format(
            manual_quantity,
            "f",
        ),
        "previous_robot_owned_quantity": format(
            previous_robot_owned_quantity,
            "f",
        ),
        "raw_robot_fill_quantity": format(
            raw_robot_fill_quantity,
            "f",
        ),
        "reason": reason.strip(),
        "adjudicated_by": adjudicated_by.strip(),
        "request_id": request_id.strip(),
        "cutoff_semantics": (
            "fills strictly after marker "
            "(ts_event,created_at,event_id)"
        ),
    }


def _parse_baseline(
    *,
    account_id: str,
    event_id: str,
    ts_event: datetime,
    created_at: datetime,
    raw_payload: Any,
) -> OwnershipBaseline:
    if not isinstance(raw_payload, Mapping):
        raise OwnershipLedgerError(
            "ownership rebaseline evidence is invalid"
        )
    if (
        raw_payload.get("ownership_schema_version")
        != OWNERSHIP_REBASELINE_SCHEMA_VERSION
    ):
        raise OwnershipLedgerError(
            "ownership rebaseline evidence is invalid"
        )
    symbol = canonical_symbol(
        raw_payload.get("symbol")
        or raw_payload.get("instrument_id")
    )
    reason = str(raw_payload.get("reason") or "").strip()
    adjudicated_by = str(
        raw_payload.get("adjudicated_by") or ""
    ).strip()
    request_id = str(raw_payload.get("request_id") or "").strip()
    if not symbol or not reason or not adjudicated_by or not request_id:
        raise OwnershipLedgerError(
            "ownership rebaseline evidence is invalid"
        )
    quantity = _payload_decimal(
        raw_payload,
        "baseline_quantity",
        required=True,
    )
    exchange_quantity = _payload_decimal(
        raw_payload,
        "exchange_quantity_at_baseline",
        required=False,
    )
    manual_quantity = _payload_decimal(
        raw_payload,
        "manual_quantity_at_baseline",
        required=False,
    )
    if (
        exchange_quantity is not None
        and manual_quantity is not None
        and quantity + manual_quantity != exchange_quantity
    ):
        raise OwnershipLedgerError(
            "ownership rebaseline evidence is invalid"
        )
    return OwnershipBaseline(
        account_id=account_id,
        symbol=symbol,
        quantity=quantity,
        event_id=event_id,
        ts_event=ts_event,
        created_at=created_at,
        reason=reason,
        adjudicated_by=adjudicated_by,
        request_id=request_id,
        exchange_quantity=exchange_quantity,
        manual_quantity=manual_quantity,
        payload=raw_payload,
    )


def _payload_decimal(
    payload: Mapping[str, Any],
    field_name: str,
    *,
    required: bool,
) -> Decimal | None:
    raw_value = payload.get(field_name)
    if raw_value is None and not required:
        return None
    try:
        quantity = Decimal(str(raw_value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OwnershipLedgerError(
            "ownership rebaseline evidence is invalid"
        ) from exc
    if not quantity.is_finite():
        raise OwnershipLedgerError(
            "ownership rebaseline evidence is invalid"
        )
    return quantity


def _fill_symbol(
    payload: Mapping[str, Any],
    *,
    projection_instrument: Any,
) -> str:
    for field_name in ("instrument_id", "symbol", "instrument"):
        symbol = canonical_symbol(payload.get(field_name))
        if symbol:
            return symbol
    return canonical_symbol(projection_instrument)
