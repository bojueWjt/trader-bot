"""Canonical hedge-book identity for venue, projection, and operator refs.

Open quantity and side come from exchange evidence. Projection IDs such as
``-EXTERNAL`` / ``-BOTH`` name the same book as ``-LONG`` / ``-SHORT`` for a
given account and venue symbol. Netting/BOTH uses the signed quantity.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

def canonical_instrument_key(raw: str) -> str:
    # Keep in lockstep with execution_domain.identifiers.venue_symbol.
    before_venue = str(raw).strip().split(".", 1)[0]
    return before_venue.split("-", 1)[0].strip().upper()

_LONG = frozenset({"LONG", "BUY"})
_SHORT = frozenset({"SHORT", "SELL"})
_BOOK_SUFFIX = frozenset({"LONG", "SHORT", "BOTH", "EXTERNAL", "NET"})


def hedge_book(side: Any, quantity: Any = None) -> str | None:
    token = str(side or "").strip().upper()
    if token in _LONG:
        return "long"
    if token in _SHORT:
        return "short"
    amount = _signed_decimal(quantity)
    if amount > 0:
        return "long"
    if amount < 0:
        return "short"
    return None


def nautilus_instrument_id(raw: str | None) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if "." in text:
        return text
    symbol = canonical_instrument_key(text)
    if not symbol:
        return None
    return f"{symbol}-PERP.BINANCE"


def canonical_position_id(instrument: str | None, side: str | None) -> str | None:
    instrument_id = nautilus_instrument_id(instrument)
    book = hedge_book(side)
    if not instrument_id or book not in {"long", "short"}:
        return None
    return f"{instrument_id}-{book.upper()}"


def position_book_key(
    *,
    account_id: str | None,
    instrument: str | None,
    side: Any = None,
    quantity: Any = None,
    position_id: str | None = None,
) -> tuple[str, str, str] | None:
    account = str(account_id or "").strip()
    symbol = canonical_instrument_key(str(instrument or ""))
    book = _book_from_position_id(position_id, side) if position_id else None
    if not book:
        book = hedge_book(side, quantity)
    if not symbol and position_id:
        symbol = canonical_instrument_key(_instrument_from_position_id(position_id) or "")
    if not account or not symbol or book not in {"long", "short"}:
        return None
    return (account, symbol, book)


def position_ids_equivalent(
    left: str | None,
    right: str | None,
    *,
    side: Any = None,
) -> bool:
    supplied = str(left or "").strip()
    resolved = str(right or "").strip()
    if not supplied or not resolved:
        return False
    if supplied == resolved:
        return True
    left_key = position_book_key(
        account_id="_",
        instrument=_instrument_from_position_id(supplied),
        side=side,
        position_id=supplied,
    )
    right_key = position_book_key(
        account_id="_",
        instrument=_instrument_from_position_id(resolved),
        side=side,
        position_id=resolved,
    )
    return bool(left_key and right_key and left_key == right_key)


def open_books_from_mirror_payload(
    account_id: str,
    payload: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in payload.get("positions") or [] if isinstance(payload, Mapping) else []:
        if not isinstance(raw, dict):
            continue
        quantity = _signed_decimal(
            raw.get("position_amt")
            if raw.get("position_amt") is not None
            else raw.get("quantity") or raw.get("qty")
        )
        book = hedge_book(
            raw.get("position_side") or raw.get("positionSide") or raw.get("side"),
            quantity,
        )
        symbol = canonical_instrument_key(str(raw.get("symbol") or raw.get("instrument_id") or ""))
        if not book or not symbol or quantity == 0:
            continue
        key = (account_id, symbol, book)
        if key in seen:
            continue
        seen.add(key)
        instrument_id = nautilus_instrument_id(symbol)
        rows.append(
            {
                "account_id": account_id,
                "instrument_id": instrument_id,
                "instrument_symbol": symbol,
                "side": book,
                "position_id": canonical_position_id(instrument_id, book),
                "quantity": _abs_text(quantity),
                "entry_price": raw.get("entry_price") or raw.get("entryPrice"),
                "mark_price": raw.get("mark_price") or raw.get("markPrice"),
                "unrealized_pnl": raw.get("unrealized_pnl") or raw.get("unRealizedProfit"),
                "leverage": raw.get("leverage"),
            }
        )
    return rows


def annotate_with_projection(
    venue_row: Mapping[str, Any],
    projection_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any] | None:
    key = position_book_key(
        account_id=venue_row.get("account_id"),
        instrument=venue_row.get("instrument_id") or venue_row.get("instrument_symbol"),
        side=venue_row.get("side"),
        quantity=venue_row.get("quantity"),
        position_id=venue_row.get("position_id"),
    )
    if key is None:
        return None
    match: Mapping[str, Any] | None = None
    for row in projection_rows:
        candidate = position_book_key(
            account_id=row.get("account_id"),
            instrument=row.get("instrument_id") or row.get("instrument_symbol"),
            side=row.get("side") or row.get("position_side"),
            quantity=row.get("quantity"),
            position_id=row.get("position_id"),
        )
        if candidate == key:
            match = row
            break
    merged = dict(venue_row)
    if match is None:
        return merged
    payload = match.get("payload") if isinstance(match.get("payload"), dict) else {}
    if match.get("instrument_id"):
        merged["instrument_id"] = match["instrument_id"]
        merged["position_id"] = canonical_position_id(
            match["instrument_id"], merged.get("side")
        )
    for src_key, dest_key in (
        ("avg_entry_price", "entry_price"),
        ("mark_price", "mark_price"),
        ("unrealized_pnl", "unrealized_pnl"),
    ):
        if merged.get(dest_key) in (None, "", "0"):
            merged[dest_key] = match.get(src_key)
    merged["intent_id"] = payload.get("intent_id")
    merged["signal_id"] = payload.get("signal_id") or payload.get("intent_id")
    merged["raw_signal"] = payload.get("raw_signal")
    merged["stop_loss"] = payload.get("stop_loss")
    merged["take_profit"] = payload.get("take_profit")
    if payload.get("leverage") is not None and merged.get("leverage") is None:
        merged["leverage"] = payload.get("leverage")
    if payload.get("opened_at") is not None:
        merged["opened_at"] = payload.get("opened_at")
    original = match.get("position_id")
    if original and original != merged.get("position_id"):
        merged["source_position_id"] = original
    return merged


def _instrument_from_position_id(position_id: str | None) -> str | None:
    text = str(position_id or "").strip()
    if not text:
        return None
    parts = text.rsplit("-", 1)
    if len(parts) == 2 and parts[1].upper() in _BOOK_SUFFIX:
        return parts[0]
    return text


def _book_from_position_id(position_id: str | None, side: Any = None) -> str | None:
    text = str(position_id or "").strip()
    suffix = text.rsplit("-", 1)[-1].upper() if text else ""
    if suffix in _LONG:
        return "long"
    if suffix in _SHORT:
        return "short"
    if suffix in {"EXTERNAL", "BOTH", "NET"}:
        return hedge_book(side)
    return hedge_book(side)


def _signed_decimal(value: Any) -> Decimal:
    if value is None or str(value).strip() == "":
        return Decimal("0")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    if not number.is_finite():
        return Decimal("0")
    return number


def _abs_text(value: Decimal) -> str:
    return format(abs(value).normalize(), "f")
