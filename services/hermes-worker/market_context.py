"""Record-only market context snapshots for Hermes decisions.

This module deliberately keeps market-data IO separate from indicator math so the
math stays deterministic and independently testable. It never derives a symbol
from message text; callers must pass a structured instrument symbol.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

MARKET_CONTEXT_VERSION = "market-v1"
DEFAULT_TIMEOUT_SECONDS = 3.0
KLINE_LIMIT = 250
KLINE_INTERVALS = ("1m", "15m", "1h", "4h")


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int | None = None


def parse_klines(rows: list[Any]) -> list[Candle]:
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        candles.append(
            Candle(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                close_time=int(row[6]) if len(row) > 6 and row[6] is not None else None,
            )
        )
    return candles


def calculate_atr(candles: list[Candle], *, period: int = 14) -> float | None:
    true_ranges = _true_ranges(candles)
    if len(true_ranges) < period:
        return None
    atr = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        atr = ((atr * (period - 1)) + true_range) / period
    return atr


def calculate_ema(values: list[float], *, period: int) -> float | None:
    if not values or period <= 0 or len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema = values[0]
    for value in values[1:]:
        ema = (value * multiplier) + (ema * (1.0 - multiplier))
    return ema


def classify_ema_alignment(emas: dict[str, float | None]) -> str:
    ema20 = emas.get("ema20")
    ema50 = emas.get("ema50")
    ema200 = emas.get("ema200")
    if ema20 is None or ema50 is None or ema200 is None:
        return "mixed"
    if ema20 > ema50 > ema200:
        return "bull"
    if ema20 < ema50 < ema200:
        return "bear"
    return "mixed"


def calculate_adx(candles: list[Candle], *, period: int = 14) -> float | None:
    if len(candles) < (period * 2):
        return None

    true_ranges = _true_ranges(candles)
    plus_dm = [0.0]
    minus_dm = [0.0]
    for i in range(1, len(candles)):
        up_move = candles[i].high - candles[i - 1].high
        down_move = candles[i - 1].low - candles[i].low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

    smoothed_tr = sum(true_ranges[1 : period + 1])
    smoothed_plus = sum(plus_dm[1 : period + 1])
    smoothed_minus = sum(minus_dm[1 : period + 1])

    dx_values: list[float] = []
    for i in range(period, len(candles)):
        if i > period:
            smoothed_tr = smoothed_tr - (smoothed_tr / period) + true_ranges[i]
            smoothed_plus = smoothed_plus - (smoothed_plus / period) + plus_dm[i]
            smoothed_minus = smoothed_minus - (smoothed_minus / period) + minus_dm[i]
        if smoothed_tr == 0:
            dx_values.append(0.0)
            continue
        plus_di = 100.0 * (smoothed_plus / smoothed_tr)
        minus_di = 100.0 * (smoothed_minus / smoothed_tr)
        denom = plus_di + minus_di
        dx_values.append(0.0 if denom == 0 else 100.0 * abs(plus_di - minus_di) / denom)

    if len(dx_values) < period:
        return None
    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = ((adx * (period - 1)) + dx) / period
    return adx


def kaufman_efficiency_ratio(values: list[float], *, period: int = 10) -> float | None:
    if len(values) <= period:
        return None
    change = abs(values[-1] - values[-period - 1])
    volatility = sum(abs(values[i] - values[i - 1]) for i in range(len(values) - period, len(values)))
    if volatility == 0:
        return 0.0
    return change / volatility


def utc_session_label(iso_timestamp: str | None = None) -> str:
    if iso_timestamp:
        normalized = iso_timestamp.replace("Z", "+00:00")
        current = datetime.fromisoformat(normalized).astimezone(timezone.utc)
    else:
        current = datetime.now(timezone.utc)
    hour = current.hour
    if 0 <= hour < 8:
        return "asia"
    if 8 <= hour < 13:
        return "eu"
    return "us"


class MarketContextFetcher:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        env_url = os.environ.get("HERMES_MARKET_DATA_BASE_URL", "")
        self._base_url = (base_url if base_url is not None else env_url).strip().rstrip("/")
        self._timeout_seconds = min(float(timeout_seconds), DEFAULT_TIMEOUT_SECONDS)
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(self._timeout_seconds),
            trust_env=False,
        )

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    def fetch(self, symbol: str, *, now_iso: str | None = None) -> dict[str, Any]:
        fetched_at = now_iso or _utc_now_iso()
        if not self.configured:
            return {
                "context_version": MARKET_CONTEXT_VERSION,
                "symbol": symbol,
                "status": "skipped",
                "reason": "market_data_base_url_unconfigured",
                "fetched_at": fetched_at,
                "partial_failures": [],
            }

        started = time.monotonic()
        partial_failures: list[dict[str, str]] = []
        raw: dict[str, Any] = {"klines": {}}
        candles_by_interval: dict[str, list[Candle]] = {}
        success_count = 0

        for interval in KLINE_INTERVALS:
            endpoint = f"klines_{interval}"
            payload = self._get_json(
                "/fapi/v1/klines",
                endpoint=endpoint,
                params={"symbol": symbol, "interval": interval, "limit": KLINE_LIMIT},
                partial_failures=partial_failures,
            )
            if payload is None:
                continue
            success_count += 1
            candles = parse_klines(payload)
            candles_by_interval[interval] = candles
            raw["klines"][interval] = _latest_candle(candles)

        premium = self._get_json(
            "/fapi/v1/premiumIndex",
            endpoint="premium_index",
            params={"symbol": symbol},
            partial_failures=partial_failures,
        )
        if isinstance(premium, dict):
            success_count += 1
            raw["premium_index"] = {
                "mark_price": _float_or_none(premium.get("markPrice")),
                "last_funding_rate": _float_or_none(premium.get("lastFundingRate")),
                "next_funding_time": premium.get("nextFundingTime"),
            }

        open_interest = self._get_json(
            "/fapi/v1/openInterest",
            endpoint="open_interest",
            params={"symbol": symbol},
            partial_failures=partial_failures,
        )
        if isinstance(open_interest, dict):
            success_count += 1
            raw["open_interest"] = {
                "open_interest": _float_or_none(open_interest.get("openInterest")),
                "time": open_interest.get("time"),
            }

        book = self._get_json(
            "/fapi/v1/ticker/bookTicker",
            endpoint="book_ticker",
            params={"symbol": symbol},
            partial_failures=partial_failures,
        )
        if isinstance(book, dict):
            success_count += 1
            bid = _float_or_none(book.get("bidPrice"))
            ask = _float_or_none(book.get("askPrice"))
            spread = (ask - bid) if bid is not None and ask is not None else None
            mid = ((ask + bid) / 2.0) if bid is not None and ask is not None else None
            raw["book_ticker"] = {
                "bid_price": bid,
                "ask_price": ask,
                "spread": spread,
                "spread_bps": (spread / mid * 10_000.0) if spread is not None and mid else None,
            }

        ticker_24h = self._get_json(
            "/fapi/v1/ticker/24hr",
            endpoint="ticker_24h",
            params={"symbol": symbol},
            partial_failures=partial_failures,
        )
        if isinstance(ticker_24h, dict):
            success_count += 1
            raw["ticker_24h"] = {
                "volume": _float_or_none(ticker_24h.get("volume")),
                "quote_volume": _float_or_none(ticker_24h.get("quoteVolume")),
                "price_change_percent": _float_or_none(ticker_24h.get("priceChangePercent")),
            }

        latency_ms = int(round((time.monotonic() - started) * 1000.0))
        if success_count == 0:
            return {
                "context_version": MARKET_CONTEXT_VERSION,
                "symbol": symbol,
                "status": "skipped",
                "reason": "market_context_fetch_failed",
                "fetched_at": fetched_at,
                "fetch_latency_ms": latency_ms,
                "partial_failures": partial_failures,
            }
        return {
            "context_version": MARKET_CONTEXT_VERSION,
            "symbol": symbol,
            "status": "ok",
            "fetched_at": fetched_at,
            "fetch_latency_ms": latency_ms,
            "raw": raw,
            "indicators": _build_indicators(candles_by_interval, fetched_at),
            "partial_failures": partial_failures,
        }

    def _get_json(
        self,
        path: str,
        *,
        endpoint: str,
        params: dict[str, Any],
        partial_failures: list[dict[str, str]],
    ) -> Any | None:
        try:
            response = self._client.get(
                f"{self._base_url}{path}",
                params=params,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException:
            partial_failures.append({"endpoint": endpoint, "reason": "timeout"})
        except httpx.HTTPStatusError as exc:
            partial_failures.append(
                {"endpoint": endpoint, "reason": f"HTTP {exc.response.status_code}"}
            )
        except Exception as exc:
            partial_failures.append({"endpoint": endpoint, "reason": str(exc)[:200]})
        return None


def _build_indicators(candles_by_interval: dict[str, list[Candle]], fetched_at: str) -> dict[str, Any]:
    ema: dict[str, dict[str, float | str | None]] = {}
    adx: dict[str, float | None] = {}
    efficiency_ratio: dict[str, float | None] = {}
    for interval in ("15m", "1h", "4h"):
        closes = [candle.close for candle in candles_by_interval.get(interval, [])]
        values = {
            "ema20": calculate_ema(closes, period=20),
            "ema50": calculate_ema(closes, period=50),
            "ema200": calculate_ema(closes, period=200),
        }
        ema[interval] = {**values, "alignment": classify_ema_alignment(values)}
        candles = candles_by_interval.get(interval, [])
        adx[interval] = calculate_adx(candles, period=14)
        efficiency_ratio[interval] = kaufman_efficiency_ratio(closes, period=10)

    return {
        "atr_14": {
            "1m": calculate_atr(candles_by_interval.get("1m", []), period=14),
            "1h": calculate_atr(candles_by_interval.get("1h", []), period=14),
        },
        "ema": ema,
        "adx_14": adx,
        "efficiency_ratio_10": efficiency_ratio,
        "utc_session": utc_session_label(fetched_at),
    }


def _true_ranges(candles: list[Candle]) -> list[float]:
    if not candles:
        return []
    ranges = [candles[0].high - candles[0].low]
    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return ranges


def _latest_candle(candles: list[Candle]) -> dict[str, Any] | None:
    if not candles:
        return None
    candle = candles[-1]
    return {
        "open_time": candle.open_time,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "close_time": candle.close_time,
    }


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
