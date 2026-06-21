"""Canonical order-management identifiers.

Instrument matching is based on the venue symbol: the substring before the first
``.`` and then before the first ``-``, uppercased after stripping whitespace.

Position identity supports both exchange-provided EXTERNAL identifiers and BOTH
position modes. Netting mode yields one key per account and symbol
(``acct:SYMBOL``). Hedge mode yields distinct long and short keys
(``acct:SYMBOL:long`` and ``acct:SYMBOL:short``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = [
    "CanonicalInstrument",
    "PositionKey",
    "canonical_account_id",
    "canonical_instrument_key",
    "canonical_position_key",
    "instruments_match",
    "parse_instrument",
    "venue_symbol",
]


@dataclass(frozen=True)
class CanonicalInstrument:
    raw: str
    venue_symbol: str
    venue: Optional[str]
    product: Optional[str]


def venue_symbol(x: str) -> str:
    raw = str(x).strip()
    before_venue = raw.split(".", 1)[0]
    before_product = before_venue.split("-", 1)[0]
    return before_product.strip().upper()


def parse_instrument(x: str) -> CanonicalInstrument:
    raw = str(x).strip()
    before_venue, sep, after_venue = raw.partition(".")
    before_product, product_sep, product = before_venue.partition("-")
    venue = after_venue.strip().upper() if sep and after_venue.strip() else None
    product_value = product.strip().upper() if product_sep and product.strip() else None
    return CanonicalInstrument(
        raw=raw,
        venue_symbol=venue_symbol(before_product),
        venue=venue,
        product=product_value,
    )


def canonical_instrument_key(x: str) -> str:
    return venue_symbol(x)


def instruments_match(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    left = canonical_instrument_key(a)
    right = canonical_instrument_key(b)
    return bool(left and right and left == right)


def canonical_account_id(x: str) -> str:
    if x is None:
        raise ValueError("account_id must not be empty")
    account_id = str(x).strip()
    if not account_id:
        raise ValueError("account_id must not be empty")
    return account_id


@dataclass(frozen=True)
class PositionKey:
    account_id: str
    venue_symbol: str
    side: Optional[str] = None

    def key(self) -> str:
        account_id = canonical_account_id(self.account_id)
        symbol = canonical_instrument_key(self.venue_symbol)
        if not symbol:
            raise ValueError("venue_symbol must not be empty")
        if self.side is None:
            return f"{account_id}:{symbol}"
        side = str(self.side).strip().lower()
        if side not in {"long", "short"}:
            raise ValueError("side must be 'long' or 'short'")
        return f"{account_id}:{symbol}:{side}"


def canonical_position_key(
    account_id: str,
    instrument: str,
    side: Optional[str] = None,
) -> str:
    return PositionKey(account_id, instrument, side).key()
