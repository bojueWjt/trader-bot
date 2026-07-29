from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Protocol
from uuid import uuid4

from psycopg2.extras import Json

from execution_domain.identifiers import canonical_account_id, canonical_instrument_key


class MarketDataClient(Protocol):
    def subscribe_mark_price(self, symbol: str) -> None: ...

    def subscribe_last_trade(self, symbol: str) -> None: ...

    def subscribe_book_ticker(self, symbol: str) -> None: ...


@dataclass
class PriceFeedStatus:
    account_id: str
    venue_symbol: str
    source: str = "combined"
    mark_price: Decimal | None = None
    last_price: Decimal | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    last_event_at: datetime | None = None
    stale: bool = True
    payload: dict[str, Any] = field(default_factory=dict)


class MarketDataStatusStore:
    def __init__(self, conn) -> None:
        self._conn = conn

    def upsert(self, status: PriceFeedStatus) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO price_feed_status (
                    price_feed_status_id, account_id, venue_symbol, source,
                    mark_price, last_price, bid_price, ask_price, last_event_at,
                    stale, payload, updated_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT (account_id, venue_symbol, source) DO UPDATE SET
                    mark_price=COALESCE(EXCLUDED.mark_price, price_feed_status.mark_price),
                    last_price=COALESCE(EXCLUDED.last_price, price_feed_status.last_price),
                    bid_price=COALESCE(EXCLUDED.bid_price, price_feed_status.bid_price),
                    ask_price=COALESCE(EXCLUDED.ask_price, price_feed_status.ask_price),
                    last_event_at=COALESCE(EXCLUDED.last_event_at, price_feed_status.last_event_at),
                    stale=EXCLUDED.stale,
                    payload=price_feed_status.payload || EXCLUDED.payload,
                    updated_at=now()
                """,
                (
                    str(uuid4()),
                    status.account_id,
                    status.venue_symbol,
                    status.source,
                    status.mark_price,
                    status.last_price,
                    status.bid_price,
                    status.ask_price,
                    status.last_event_at,
                    status.stale,
                    Json(status.payload),
                ),
            )


class PriceFeedMonitor:
    def __init__(
        self,
        *,
        account_id: str,
        symbols: list[str],
        client: MarketDataClient,
        store: MarketDataStatusStore,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.account_id = canonical_account_id(account_id)
        self.symbols = list(symbols)
        self._client = client
        self._store = store
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.metrics = {"disconnects": 0, "reconnects": 0, "updates": 0}
        self._latest: dict[str, PriceFeedStatus] = {}

    def start(self) -> None:
        for symbol in self.symbols:
            self._subscribe_symbol(symbol)

    def on_mark_price(self, symbol: str, price: str | Decimal, *, event_at: datetime | None = None) -> None:
        status = self._status(symbol, event_at)
        status.mark_price = Decimal(str(price))
        self._publish(status)

    def on_last_trade(self, symbol: str, price: str | Decimal, *, event_at: datetime | None = None) -> None:
        status = self._status(symbol, event_at)
        status.last_price = Decimal(str(price))
        self._publish(status)

    def on_book_ticker(
        self,
        symbol: str,
        *,
        bid: str | Decimal,
        ask: str | Decimal,
        event_at: datetime | None = None,
    ) -> None:
        status = self._status(symbol, event_at)
        status.bid_price = Decimal(str(bid))
        status.ask_price = Decimal(str(ask))
        self._publish(status)

    def on_disconnect(self, reason: str) -> None:
        self.metrics["disconnects"] += 1
        for symbol in self.symbols:
            status = self._status(symbol, None)
            status.stale = True
            status.payload["disconnect_reason"] = reason
            self._store.upsert(status)

    def on_reconnect(self) -> None:
        self.metrics["reconnects"] += 1
        for symbol in self.symbols:
            self._subscribe_symbol(symbol)

    def _subscribe_symbol(self, symbol: str) -> None:
        self._client.subscribe_mark_price(symbol)
        self._client.subscribe_last_trade(symbol)
        self._client.subscribe_book_ticker(symbol)

    def _status(self, symbol: str, event_at: datetime | None) -> PriceFeedStatus:
        venue_symbol = canonical_instrument_key(symbol)
        status = self._latest.get(venue_symbol)
        if status is None:
            status = PriceFeedStatus(
                account_id=self.account_id,
                venue_symbol=venue_symbol,
            )
            self._latest[venue_symbol] = status
        if event_at is not None:
            status.last_event_at = _aware(event_at)
        elif status.last_event_at is None:
            status.last_event_at = _aware(self._now())
        return status

    def _publish(self, status: PriceFeedStatus) -> None:
        status.stale = False
        self.metrics["updates"] += 1
        self._store.upsert(status)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
