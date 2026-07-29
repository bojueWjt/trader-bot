from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from market_data.price_feed import MarketDataStatusStore, PriceFeedMonitor


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_price_feed_subscribes_and_persists_mark_last_and_book_status(db_conn) -> None:
    client = _FakeMarketClient()
    monitor = PriceFeedMonitor(
        account_id="acct-om2",
        symbols=["BTCUSDT-PERP.BINANCE"],
        client=client,
        store=MarketDataStatusStore(db_conn),
        now=lambda: NOW,
    )

    monitor.start()
    monitor.on_mark_price("BTCUSDT-PERP.BINANCE", "65000.10", event_at=NOW)
    monitor.on_last_trade("BTCUSDT-PERP.BINANCE", "65001.20", event_at=NOW)
    monitor.on_book_ticker("BTCUSDT-PERP.BINANCE", bid="65000", ask="65002", event_at=NOW)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT venue_symbol, source, mark_price, last_price, bid_price, ask_price, stale "
            "FROM price_feed_status WHERE account_id='acct-om2'"
        )
        row = cur.fetchone()

    assert client.subscriptions == [
        ("mark_price", "BTCUSDT-PERP.BINANCE"),
        ("last_trade", "BTCUSDT-PERP.BINANCE"),
        ("book_ticker", "BTCUSDT-PERP.BINANCE"),
    ]
    assert row == (
        "BTCUSDT",
        "combined",
        Decimal("65000.10"),
        Decimal("65001.20"),
        Decimal("65000"),
        Decimal("65002"),
        False,
    )


def test_disconnect_and_reconnect_mark_status_and_metrics(db_conn) -> None:
    client = _FakeMarketClient()
    monitor = PriceFeedMonitor(
        account_id="acct-om2",
        symbols=["BTCUSDT-PERP.BINANCE"],
        client=client,
        store=MarketDataStatusStore(db_conn),
        now=lambda: NOW,
    )
    monitor.start()
    monitor.on_mark_price("BTCUSDT-PERP.BINANCE", "65000.10", event_at=NOW)

    monitor.on_disconnect("ws closed")
    monitor.on_reconnect()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT stale, payload->>'disconnect_reason' FROM price_feed_status "
            "WHERE account_id='acct-om2' AND venue_symbol='BTCUSDT'"
        )
        stale, reason = cur.fetchone()

    assert stale is True
    assert reason == "ws closed"
    assert monitor.metrics == {"disconnects": 1, "reconnects": 1, "updates": 1}
    assert client.subscriptions.count(("mark_price", "BTCUSDT-PERP.BINANCE")) == 2


class _FakeMarketClient:
    def __init__(self) -> None:
        self.subscriptions: list[tuple[str, str]] = []

    def subscribe_mark_price(self, symbol: str) -> None:
        self.subscriptions.append(("mark_price", symbol))

    def subscribe_last_trade(self, symbol: str) -> None:
        self.subscriptions.append(("last_trade", symbol))

    def subscribe_book_ticker(self, symbol: str) -> None:
        self.subscriptions.append(("book_ticker", symbol))
