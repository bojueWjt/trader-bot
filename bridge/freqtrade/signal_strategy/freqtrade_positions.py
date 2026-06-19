from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request
from typing import Any


FREQTRADE_POSITION_TIMEOUT_SECONDS = 3.0
_LAST_QUERY_SUCCESS = False


def get_open_position(pair: str) -> dict[str, Any] | None:
    _set_query_success(False)
    normalized_pair = _normalize_pair(pair)
    if not normalized_pair:
        return None

    base_url = os.environ.get("FREQTRADE_BASE_URL", "").strip().rstrip("/")
    api_user = os.environ.get("FREQTRADE_API_USER", "").strip()
    api_password = os.environ.get("FREQTRADE_API_PASSWORD", "")
    if not base_url or not api_user or not api_password:
        return None

    try:
        payload = _fetch_open_trades(base_url, api_user, api_password)
    except Exception:
        _set_query_success(False)
        return None

    _set_query_success(True)
    for trade in _iter_trades(payload):
        if not _trade_is_open(trade):
            continue
        if _normalize_pair(trade.get("pair")) != normalized_pair:
            continue
        return _position_from_trade(trade)
    return None


def _fetch_open_trades(base_url: str, api_user: str, api_password: str) -> Any:
    # stdlib urllib (no third-party deps) so this runs in the watcher container's
    # bare python where httpx is absent; a hard import there would crash the
    # whole signal importer instead of degrading gracefully.
    query = urllib.parse.urlencode({"is_open": "true"})
    url = f"{base_url}/api/v1/trades?{query}"
    token = base64.b64encode(f"{api_user}:{api_password}".encode()).decode()
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(request, timeout=FREQTRADE_POSITION_TIMEOUT_SECONDS) as response:
        body = response.read().decode("utf-8")
    return json.loads(body)


def position_query_succeeded() -> bool:
    return _LAST_QUERY_SUCCESS


def _set_query_success(success: bool) -> None:
    global _LAST_QUERY_SUCCESS
    _LAST_QUERY_SUCCESS = success


def _iter_trades(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [trade for trade in payload if isinstance(trade, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("trades", "data", "result"):
        trades = payload.get(key)
        if isinstance(trades, list):
            return [trade for trade in trades if isinstance(trade, dict)]

    if isinstance(payload.get("pair"), str):
        return [payload]
    return []


def _trade_is_open(trade: dict[str, Any]) -> bool:
    if "is_open" not in trade:
        return True
    return bool(trade.get("is_open"))


def _position_from_trade(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "pair": str(trade.get("pair", "")),
        "side": _side_from_trade(trade),
        "amount": _number_from_trade(trade, "amount", "filled_amount", "stake_amount"),
        "open_rate": _number_from_trade(trade, "open_rate", "entry_rate"),
        "current_profit_pct": _profit_pct_from_trade(trade),
        "trade_id": trade.get("trade_id", trade.get("id")),
    }


def _side_from_trade(trade: dict[str, Any]) -> str:
    side = str(trade.get("side", "")).strip().lower()
    if side in {"long", "short"}:
        return side
    if bool(trade.get("is_short", trade.get("short", False))):
        return "short"
    return "long"


def _profit_pct_from_trade(trade: dict[str, Any]) -> float | int | None:
    value = _number_from_trade(
        trade,
        "current_profit_pct",
        "profit_pct",
        "profit_percent",
    )
    if value is not None:
        return value

    ratio = _number_from_trade(trade, "profit_ratio")
    if ratio is not None:
        return ratio * 100

    return _number_from_trade(trade, "current_profit")


def _number_from_trade(trade: dict[str, Any], *keys: str) -> float | int | None:
    for key in keys:
        value = trade.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, (int, float)):
            return value
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _normalize_pair(value: Any) -> str:
    return str(value or "").strip().upper()
