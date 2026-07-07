from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from market_context import (
    MARKET_CONTEXT_VERSION,
    MarketContextFetcher,
    calculate_adx,
    calculate_atr,
    calculate_ema,
    classify_ema_alignment,
    kaufman_efficiency_ratio,
    parse_klines,
    utc_session_label,
)


def _trend_klines(count: int, *, start: float = 100.0, step: float = 1.0) -> list[list[str]]:
    rows = []
    for i in range(count):
        close = start + i * step
        rows.append(
            [
                i * 60_000,
                str(close),
                str(close + 1.0),
                str(close - 1.0),
                str(close),
                str(10 + i),
                (i + 1) * 60_000 - 1,
            ]
        )
    return rows


def test_indicator_math_has_known_answers_for_trending_fixture():
    candles = parse_klines(_trend_klines(40))

    assert calculate_atr(candles, period=14) == pytest.approx(2.0)
    assert calculate_adx(candles, period=14) == pytest.approx(100.0)
    assert kaufman_efficiency_ratio([c.close for c in candles], period=10) == pytest.approx(1.0)


def test_ema_math_and_alignment_have_known_answers():
    assert calculate_ema([float(i) for i in range(1, 21)], period=20) == pytest.approx(
        11.918650526094963
    )
    assert classify_ema_alignment({"ema20": 120.0, "ema50": 110.0, "ema200": 100.0}) == "bull"
    assert classify_ema_alignment({"ema20": 80.0, "ema50": 90.0, "ema200": 100.0}) == "bear"
    assert classify_ema_alignment({"ema20": 100.0, "ema50": 120.0, "ema200": 90.0}) == "mixed"


@pytest.mark.parametrize(
    ("iso_timestamp", "expected"),
    [
        ("2026-06-19T02:00:00Z", "asia"),
        ("2026-06-19T10:00:00Z", "eu"),
        ("2026-06-19T18:00:00Z", "us"),
    ],
)
def test_utc_session_label(iso_timestamp, expected):
    assert utc_session_label(iso_timestamp) == expected


def test_fetcher_collects_binance_compatible_market_context():
    seen: list[tuple[str, dict[str, list[str]]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode("ascii"))
        seen.append((request.url.path, query))
        if request.url.path == "/fapi/v1/klines":
            return httpx.Response(200, json=_trend_klines(250))
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "markPrice": "123.45",
                    "lastFundingRate": "0.0001",
                    "nextFundingTime": 1_803_000_000_000,
                },
            )
        if request.url.path == "/fapi/v1/openInterest":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "openInterest": "987.65"})
        if request.url.path == "/fapi/v1/ticker/bookTicker":
            return httpx.Response(
                200, json={"symbol": "BTCUSDT", "bidPrice": "123.40", "askPrice": "123.50"}
            )
        if request.url.path == "/fapi/v1/ticker/24hr":
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "volume": "111",
                    "quoteVolume": "222",
                    "priceChangePercent": "1.5",
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = MarketContextFetcher(base_url="https://market.example", client=client)

    snapshot = fetcher.fetch("BTCUSDT", now_iso="2026-06-19T18:00:00Z")

    assert snapshot["context_version"] == MARKET_CONTEXT_VERSION
    assert snapshot["symbol"] == "BTCUSDT"
    assert snapshot["raw"]["premium_index"]["mark_price"] == 123.45
    assert snapshot["raw"]["book_ticker"]["spread"] == pytest.approx(0.10)
    assert snapshot["indicators"]["atr_14"]["1m"] == pytest.approx(2.0)
    assert snapshot["indicators"]["ema"]["1h"]["alignment"] == "bull"
    assert snapshot["indicators"]["adx_14"]["1h"] == pytest.approx(100.0)
    assert snapshot["indicators"]["efficiency_ratio_10"]["1h"] == pytest.approx(1.0)
    assert snapshot["indicators"]["utc_session"] == "us"
    assert snapshot["partial_failures"] == []
    kline_intervals = [
        query["interval"][0] for path, query in seen if path == "/fapi/v1/klines"
    ]
    assert kline_intervals == ["1m", "15m", "1h", "4h"]


def test_fetcher_records_partial_failures_without_dropping_other_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/openInterest":
            return httpx.Response(500, json={"msg": "temporary failure"})
        if request.url.path == "/fapi/v1/klines":
            return httpx.Response(200, json=_trend_klines(250))
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = MarketContextFetcher(base_url="https://market.example", client=client)

    snapshot = fetcher.fetch("BTCUSDT", now_iso="2026-06-19T18:00:00Z")

    assert snapshot["indicators"]["atr_14"]["1m"] == pytest.approx(2.0)
    assert snapshot["partial_failures"] == [
        {"endpoint": "open_interest", "reason": "HTTP 500"}
    ]


def test_fetcher_records_timeout_as_partial_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/premiumIndex":
            raise httpx.ReadTimeout("deadline", request=request)
        if request.url.path == "/fapi/v1/klines":
            return httpx.Response(200, json=_trend_klines(250))
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = MarketContextFetcher(base_url="https://market.example", client=client)

    snapshot = fetcher.fetch("BTCUSDT", now_iso="2026-06-19T18:00:00Z")

    assert snapshot["indicators"]["atr_14"]["1m"] == pytest.approx(2.0)
    assert snapshot["partial_failures"] == [
        {"endpoint": "premium_index", "reason": "timeout"}
    ]


def test_fetcher_without_base_url_returns_skipped_snapshot():
    fetcher = MarketContextFetcher(base_url="")

    snapshot = fetcher.fetch("BTCUSDT", now_iso="2026-06-19T18:00:00Z")

    assert snapshot == {
        "context_version": MARKET_CONTEXT_VERSION,
        "symbol": "BTCUSDT",
        "status": "skipped",
        "reason": "market_data_base_url_unconfigured",
        "fetched_at": "2026-06-19T18:00:00Z",
        "partial_failures": [],
    }
