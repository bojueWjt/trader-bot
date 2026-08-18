"""Dependency-free canonical hashing for non-target exchange portfolios."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

_PORTFOLIO_SNAPSHOT_FIELDS = (
    "positions",
    "regular_orders",
    "algo_orders",
)
_PORTFOLIO_DECIMAL_FIELD_PATTERN = re.compile(
    r"(?:amount|margin|price|quantity|qty|rate)$"
)


def portfolio_baseline_sha256(
    snapshot: Mapping[str, Any],
    target_symbol: str,
) -> str:
    """Hash canonical non-target positions and orders from exchange evidence."""
    canonical_target_symbol = _canonical_portfolio_symbol(target_symbol)
    snapshots: dict[str, list[Any]] = {}
    for field_name in _PORTFOLIO_SNAPSHOT_FIELDS:
        rows = snapshot.get(field_name)
        if field_name == "regular_orders" and rows is None:
            rows = snapshot.get("open_orders")
        if not isinstance(rows, list):
            raise ValueError("portfolio snapshot fields must be lists")
        canonical_rows = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("portfolio snapshot rows must be objects")
            symbol = _portfolio_snapshot_item_symbol(row)
            if not symbol:
                raise ValueError("portfolio snapshot rows require a symbol")
            if symbol == canonical_target_symbol:
                continue
            canonical_rows.append(
                _canonical_portfolio_row(field_name, row)
            )
        canonical_rows.sort(key=_canonical_json)
        snapshots[field_name] = canonical_rows
    payload = _canonical_json(snapshots).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _portfolio_snapshot_item_symbol(item: dict[str, Any]) -> str:
    for field_name in ("symbol", "instrument_id", "instrument"):
        symbol = _canonical_portfolio_symbol(item.get(field_name))
        if symbol:
            return symbol
    return ""


def _canonical_portfolio_symbol(value: Any) -> str:
    return str(value or "").strip().upper().split("-")[0].split(".")[0]


def _canonical_portfolio_row(
    field_name: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    if field_name == "positions":
        return {
            "symbol": _portfolio_snapshot_item_symbol(row),
            "quantity": _canonical_portfolio_value(
                _portfolio_alias(
                    row,
                    "quantity",
                    "position_amt",
                    "positionAmt",
                ),
                "quantity",
            ),
            "position_side": str(
                _portfolio_alias(
                    row,
                    "position_side",
                    "positionSide",
                )
                or "BOTH"
            ).upper(),
            "entry_price": _canonical_portfolio_value(
                _portfolio_alias(
                    row,
                    "entry_price",
                    "entryPrice",
                ),
                "entry_price",
            ),
        }
    order_kind = "regular"
    if field_name == "algo_orders":
        order_kind = "algo"
    return {
        "symbol": _portfolio_snapshot_item_symbol(row),
        "position_side": str(
            _portfolio_alias(
                row,
                "position_side",
                "positionSide",
            )
            or "BOTH"
        ).upper(),
        "side": str(_portfolio_alias(row, "side") or "").upper(),
        "order_type": str(
            _portfolio_alias(
                row,
                "order_type",
                "type",
                "orderType",
            )
            or ""
        ).upper(),
        "quantity": _canonical_portfolio_value(
            _portfolio_alias(
                row,
                "quantity",
                "origQty",
            ),
            "quantity",
        ),
        "price": _canonical_portfolio_value(
            _portfolio_alias(row, "price"),
            "price",
        ),
        "stop_price": _canonical_portfolio_value(
            _portfolio_alias(
                row,
                "stop_price",
                "trigger_price",
                "stopPrice",
                "triggerPrice",
            ),
            "stop_price",
        ),
        "reduce_only": _portfolio_bool(
            _portfolio_alias(
                row,
                "reduce_only",
                "reduceOnly",
            )
        ),
        "client_order_id": str(
            _portfolio_alias(
                row,
                "client_order_id",
                "clientOrderId",
                "clientAlgoId",
            )
            or ""
        ),
        "venue_order_id": str(
            _portfolio_alias(
                row,
                "venue_order_id",
                "order_id",
                "orderId",
                "algoId",
            )
            or ""
        ),
        "order_kind": order_kind,
    }


def _portfolio_alias(
    row: Mapping[str, Any],
    *field_names: str,
) -> Any:
    for field_name in field_names:
        if field_name not in row:
            continue
        value = row[field_name]
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return ""


def _portfolio_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() == "true"


def _canonical_portfolio_value(value: Any, field_name: str = "") -> Any:
    if field_name == "symbol":
        return _canonical_portfolio_symbol(value)
    if _PORTFOLIO_DECIMAL_FIELD_PATTERN.search(field_name):
        try:
            numeric = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return str(value)
        if numeric.is_finite():
            return format(numeric.normalize(), "f")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    )
