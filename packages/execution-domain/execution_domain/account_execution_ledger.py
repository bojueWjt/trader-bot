"""Reconciled per-book execution state built from venue evidence plus cache.

Pure domain logic: no I/O, no database, no network. The venue snapshot is
structurally identical to ``exchange_state_mirror.payload`` (``positions`` /
``open_orders`` / ``algo_orders`` with Binance-native symbols such as
``"ATOMUSDT"``); cache positions are ``PositionSnapshot``-shaped objects or
mappings (``instrument_id``, ``side``, ``quantity``, ``position_id``).

A *book* is (account, instrument, position side) so hedge-mode LONG and SHORT
positions on the same symbol are assessed independently.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

ROBOT_CLIENT_ORDER_ID_PATTERN = re.compile(r"^B[0-9a-f]{32}[0-9]{2}$")
_SOURCE_MESSAGE_DERIVED_SUFFIX = re.compile(r"-e\d+$")
_DRIFT_TOLERANCE = Decimal("0.01")

_POSITION_SIDES = {
    "LONG": "LONG",
    "SHORT": "SHORT",
    "BUY": "LONG",
    "SELL": "SHORT",
}


class PositionState(str, Enum):
    KNOWN_FLAT = "known_flat"
    KNOWN_OPEN = "known_open"
    UNKNOWN = "unknown"
    CONFLICTED = "conflicted"


@dataclass(frozen=True)
class BookKey:
    account_id: str
    instrument_id: str          # e.g. "ATOMUSDT-PERP.BINANCE"
    position_side: str          # "LONG" | "SHORT" (hedge mode first-class)


@dataclass(frozen=True)
class PositionAssessment:
    state: PositionState
    quantity: Decimal           # venue-side net quantity (valid for KNOWN_*, else 0)
    venue_fresh: bool
    detail: str                 # human-readable adjudication basis


def venue_symbol_for_instrument(instrument_id: Any) -> str:
    """Map "ATOMUSDT-PERP.BINANCE" style instrument ids to venue "ATOMUSDT"."""
    text = str(instrument_id or "").strip().upper()
    if not text:
        return ""
    return text.split("-", 1)[0].split(".", 1)[0]


def normalize_position_side(value: Any) -> str:
    return _POSITION_SIDES.get(str(value or "").strip().upper(), "")


def is_robot_client_order_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and ROBOT_CLIENT_ORDER_ID_PATTERN.fullmatch(value.strip()) is not None
    )


class ReconciledExecutionState:
    """Reconciles fresh venue evidence with the local cache view per book."""

    def __init__(
        self,
        *,
        account_id: str,
        venue_fresh: bool,
        venue_detail: str,
        venue_quantities: Mapping[tuple[str, str], Decimal],
        cache_quantities: Mapping[tuple[str, str], Decimal],
        bot_order_ids: Mapping[tuple[str, str], tuple[str, ...]],
    ) -> None:
        self._account_id = account_id
        self._venue_fresh = venue_fresh
        self._venue_detail = venue_detail
        self._venue_quantities = dict(venue_quantities)
        self._cache_quantities = dict(cache_quantities)
        self._bot_order_ids = dict(bot_order_ids)

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def venue_fresh(self) -> bool:
        return self._venue_fresh

    @classmethod
    def build(
        cls,
        *,
        account_id: str,
        venue_snapshot: dict | None,
        venue_fetched_at: datetime | None,
        cache_positions: Iterable[Any],
        now: datetime,
        freshness_window: timedelta = timedelta(seconds=30),
    ) -> "ReconciledExecutionState":
        venue_fresh, venue_detail = _venue_freshness(
            venue_snapshot=venue_snapshot,
            venue_fetched_at=venue_fetched_at,
            now=now,
            freshness_window=freshness_window,
        )
        venue_quantities: dict[tuple[str, str], Decimal] = {}
        bot_order_ids: dict[tuple[str, str], tuple[str, ...]] = {}
        if isinstance(venue_snapshot, Mapping):
            venue_quantities = _venue_position_quantities(venue_snapshot)
            bot_order_ids = _venue_bot_order_ids(venue_snapshot)
        return cls(
            account_id=account_id,
            venue_fresh=venue_fresh,
            venue_detail=venue_detail,
            venue_quantities=venue_quantities,
            cache_quantities=_cache_position_quantities(cache_positions),
            bot_order_ids=bot_order_ids,
        )

    def assess(self, book: BookKey) -> PositionAssessment:
        book_key = _book_lookup_key(book)
        cache_quantity = self._cache_quantities.get(book_key, Decimal(0))
        if not self._venue_fresh:
            return PositionAssessment(
                state=PositionState.UNKNOWN,
                quantity=Decimal(0),
                venue_fresh=False,
                detail=self._venue_detail,
            )
        venue_quantity = self._venue_quantities.get(book_key, Decimal(0))
        if venue_quantity != 0:
            detail = (
                f"venue reports {venue_quantity} "
                f"{book.position_side} on {book.instrument_id}"
            )
            if cache_quantity != 0 and _drift_exceeds_tolerance(
                venue_quantity=venue_quantity,
                cache_quantity=cache_quantity,
            ):
                detail += (
                    f"; cache drift: cache={cache_quantity} "
                    f"venue={venue_quantity} (>1%)"
                )
            elif cache_quantity == 0:
                detail += "; cache has no position (venue is authoritative)"
            return PositionAssessment(
                state=PositionState.KNOWN_OPEN,
                quantity=venue_quantity,
                venue_fresh=True,
                detail=detail,
            )
        if cache_quantity != 0:
            return PositionAssessment(
                state=PositionState.CONFLICTED,
                quantity=Decimal(0),
                venue_fresh=True,
                detail=(
                    f"venue reports flat but cache holds {cache_quantity} "
                    f"{book.position_side} on {book.instrument_id} "
                    "(phantom position suspected)"
                ),
            )
        return PositionAssessment(
            state=PositionState.KNOWN_FLAT,
            quantity=Decimal(0),
            venue_fresh=True,
            detail=(
                f"venue and cache agree flat for {book.position_side} "
                f"on {book.instrument_id}"
            ),
        )

    def bot_open_order_client_ids(self, book: BookKey) -> tuple[str, ...]:
        return self._bot_order_ids.get(_book_lookup_key(book), ())


def semantic_operation_id(
    account_id: str,
    source_message_id: str,
    action: str,
    book: BookKey,
    revision: int = 0,
) -> str:
    """sha256 十六进制。source_message_id 先剥离末尾 -e\\d+ 派生后缀取 base。"""
    base_message_id = _SOURCE_MESSAGE_DERIVED_SUFFIX.sub(
        "",
        str(source_message_id or "").strip(),
    )
    material = "\x1f".join(
        (
            "semantic-operation/v1",
            str(account_id or "").strip(),
            base_message_id,
            str(action or "").strip(),
            str(book.account_id or "").strip(),
            str(book.instrument_id or "").strip(),
            normalize_position_side(book.position_side)
            or str(book.position_side or "").strip().upper(),
            str(int(revision)),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _book_lookup_key(book: BookKey) -> tuple[str, str]:
    return (
        venue_symbol_for_instrument(book.instrument_id),
        normalize_position_side(book.position_side),
    )


def _venue_freshness(
    *,
    venue_snapshot: dict | None,
    venue_fetched_at: datetime | None,
    now: datetime,
    freshness_window: timedelta,
) -> tuple[bool, str]:
    if not isinstance(venue_snapshot, Mapping):
        return False, "venue snapshot unavailable"
    if venue_fetched_at is None:
        return False, "venue snapshot has no fetch timestamp"
    try:
        age = now - venue_fetched_at
    except TypeError:
        # Naive/aware mismatch: fail closed rather than guess.
        return False, "venue snapshot timestamp is not comparable to now"
    if age > freshness_window:
        return False, (
            f"venue snapshot stale: age={age} "
            f"exceeds freshness window {freshness_window}"
        )
    return True, "venue snapshot fresh"


def _venue_position_quantities(
    venue_snapshot: Mapping[str, Any],
) -> dict[tuple[str, str], Decimal]:
    quantities: dict[tuple[str, str], Decimal] = {}
    raw_positions = venue_snapshot.get("positions")
    if not isinstance(raw_positions, (list, tuple)):
        return quantities
    for row in raw_positions:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        quantity = _to_decimal(
            row.get("position_amt")
            if row.get("position_amt") is not None
            else row.get("positionAmt")
        )
        if quantity is None or quantity == 0:
            continue
        side = normalize_position_side(
            row.get("position_side")
            if row.get("position_side") is not None
            else row.get("positionSide")
        )
        if not side:
            # One-way ("BOTH") mode: the sign carries the book.
            side = "LONG" if quantity > 0 else "SHORT"
        key = (symbol, side)
        quantities[key] = quantities.get(key, Decimal(0)) + abs(quantity)
    return {key: value for key, value in quantities.items() if value != 0}


def _venue_bot_order_ids(
    venue_snapshot: Mapping[str, Any],
) -> dict[tuple[str, str], tuple[str, ...]]:
    collected: dict[tuple[str, str], list[str]] = {}
    for section in ("open_orders", "algo_orders"):
        raw_orders = venue_snapshot.get(section)
        if not isinstance(raw_orders, (list, tuple)):
            continue
        for row in raw_orders:
            if not isinstance(row, Mapping):
                continue
            client_order_id = str(
                row.get("client_order_id")
                or row.get("clientOrderId")
                or row.get("clientAlgoId")
                or ""
            ).strip()
            if not is_robot_client_order_id(client_order_id):
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            side = normalize_position_side(
                row.get("position_side")
                if row.get("position_side") is not None
                else row.get("positionSide")
            )
            if side:
                sides = (side,)
            else:
                # One-way ("BOTH") mode orders act on whichever book exists.
                sides = ("LONG", "SHORT")
            for order_side in sides:
                bucket = collected.setdefault((symbol, order_side), [])
                if client_order_id not in bucket:
                    bucket.append(client_order_id)
    return {key: tuple(values) for key, values in collected.items()}


def _cache_position_quantities(
    cache_positions: Iterable[Any],
) -> dict[tuple[str, str], Decimal]:
    quantities: dict[tuple[str, str], Decimal] = {}
    if cache_positions is None:
        return quantities
    for position in cache_positions:
        symbol = venue_symbol_for_instrument(
            _read_field(position, "instrument_id")
        )
        if not symbol:
            continue
        side = normalize_position_side(_read_field(position, "side"))
        if not side:
            continue
        quantity = _to_decimal(_read_field(position, "quantity"))
        if quantity is None or quantity == 0:
            continue
        key = (symbol, side)
        quantities[key] = quantities.get(key, Decimal(0)) + abs(quantity)
    return {key: value for key, value in quantities.items() if value != 0}


def _drift_exceeds_tolerance(
    *,
    venue_quantity: Decimal,
    cache_quantity: Decimal,
) -> bool:
    reference = abs(venue_quantity)
    if reference == 0:
        return cache_quantity != 0
    drift = abs(abs(venue_quantity) - abs(cache_quantity)) / reference
    return drift > _DRIFT_TOLERANCE


def _read_field(source: Any, field_name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(field_name)
    return getattr(source, field_name, None)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not quantity.is_finite():
        return None
    return quantity
