"""Position identity helpers for node-side command execution.

Nautilus and Binance may surface one-way positions as ``position_side=BOTH`` and
reconciled positions as ``EXTERNAL``. OM6 treats those as the same venue position
for a given account and venue symbol; only hedge-mode LONG/SHORT splits identity.
"""

from __future__ import annotations

import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


_REPO = Path(__file__).resolve().parents[3]
_EXECUTION_DOMAIN = _REPO / "packages" / "execution-domain"
if str(_EXECUTION_DOMAIN) not in sys.path:
    sys.path.insert(0, str(_EXECUTION_DOMAIN))

from execution_domain.identifiers import PositionKey, canonical_account_id, canonical_instrument_key  # noqa: E402


def position_key_for_snapshot(account_id: str, snapshot: Any) -> str:
    account = canonical_account_id(_value(snapshot, "account_id") or account_id)
    explicit_key = _value(snapshot, "position_key")
    if explicit_key:
        return _canonical_key_from_text(account, str(explicit_key))

    instrument = (
        _value(snapshot, "instrument_id")
        or _value(snapshot, "venue_symbol")
        or _value(snapshot, "symbol")
    )
    symbol = canonical_instrument_key(str(instrument or ""))
    if not symbol:
        raise ValueError("position snapshot missing instrument identity")

    return PositionKey(account, symbol, _hedge_side(snapshot)).key()


def merge_position_snapshots(account_id: str, snapshots: Iterable[Any]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        quantity = abs(_decimal(_value(snapshot, "quantity") or _value(snapshot, "qty")))
        if quantity == 0:
            continue
        key = position_key_for_snapshot(account_id, snapshot)
        current = merged.get(key)
        item = _snapshot_to_dict(snapshot)
        item["position_key"] = key
        item["quantity"] = _decimal_text(quantity)
        item["source_position_ids"] = [_position_id(account_id, snapshot)]
        if current is None:
            merged[key] = item
            continue
        current_quantity = _decimal(current.get("quantity"))
        if quantity > current_quantity:
            keep_sources = list(current["source_position_ids"])
            merged[key] = item
            merged[key]["source_position_ids"] = keep_sources
        merged[key]["source_position_ids"].append(_position_id(account_id, snapshot))
    return list(merged.values())


def residual_positions(
    account_id: str,
    target_keys: Iterable[str],
    snapshots: Iterable[Any],
) -> list[dict[str, Any]]:
    wanted = {str(key) for key in target_keys}
    return [
        position
        for position in merge_position_snapshots(account_id, snapshots)
        if position["position_key"] in wanted
    ]


def close_boundary_flat(
    account_id: str,
    target_keys: Iterable[str],
    *,
    venue_positions: Iterable[Any],
    db_positions: Iterable[Any] | None = None,
) -> bool:
    wanted = {str(key) for key in target_keys}
    if residual_positions(account_id, wanted, venue_positions):
        return False
    if db_positions is not None and residual_positions(account_id, wanted, db_positions):
        return False
    return True


def _canonical_key_from_text(account_id: str, key: str) -> str:
    parts = key.split(":")
    if len(parts) < 2:
        return key
    side = parts[2] if len(parts) >= 3 else None
    return PositionKey(account_id, parts[1], side).key()


def _hedge_side(snapshot: Any) -> str | None:
    mode = str(_value(snapshot, "position_mode") or "").upper()
    raw_side = str(_value(snapshot, "position_side") or _value(snapshot, "side") or "").lower()
    if mode == "HEDGE" and raw_side in {"long", "short"}:
        return raw_side
    return None


def _position_id(account_id: str, snapshot: Any) -> str:
    return str(
        _value(snapshot, "position_id")
        or _value(snapshot, "id")
        or _value(snapshot, "venue_position_id")
        or position_key_for_snapshot(account_id, snapshot)
    )


def _snapshot_to_dict(snapshot: Any) -> dict[str, Any]:
    if isinstance(snapshot, dict):
        return dict(snapshot)
    data: dict[str, Any] = {}
    for name in (
        "account_id",
        "position_id",
        "instrument_id",
        "venue_symbol",
        "symbol",
        "position_side",
        "position_mode",
        "side",
        "ownership",
        "source",
        "quantity",
        "qty",
    ):
        value = getattr(snapshot, name, None)
        if value is not None:
            data[name] = value
    return data


def _value(snapshot: Any, name: str) -> Any:
    if isinstance(snapshot, dict):
        return snapshot.get(name)
    return getattr(snapshot, name, None)


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")
