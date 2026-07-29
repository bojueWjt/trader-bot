from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


class IdempotencyStoreError(ValueError):
    """Invalid idempotency identity or backend behavior."""


@dataclass(frozen=True)
class FillIdentity:
    """Stable identity for venue fills seen from websocket or reconciliation."""

    account_id: str
    venue_order_id: str
    trade_id: str
    event_kind: str = "fill"
    source: Optional[str] = None

    def dedup_key(self) -> str:
        parts = {
            "account_id": self.account_id,
            "venue_order_id": self.venue_order_id,
            "trade_id": self.trade_id,
            "event_kind": self.event_kind,
        }
        missing = [name for name, value in parts.items() if not value]
        if missing:
            raise IdempotencyStoreError(
                "fill idempotency identity missing " + ", ".join(missing)
            )
        return ":".join(
            (
                "fills",
                _escape(self.account_id),
                _escape(self.event_kind),
                _escape(self.venue_order_id),
                _escape(self.trade_id),
            )
        )


class IdempotencyStore(Protocol):
    def claim_fill(self, fill: FillIdentity) -> bool:
        """Return True only for the first observation of a fill."""


class InMemoryIdempotencyStore:
    """Local test double with the same claim-once semantics as Redis SET NX."""

    def __init__(self) -> None:
        self._keys: set[str] = set()

    @property
    def claim_count(self) -> int:
        return len(self._keys)

    def claim_fill(self, fill: FillIdentity) -> bool:
        key = fill.dedup_key()
        if key in self._keys:
            return False
        self._keys.add(key)
        return True


class RedisClient(Protocol):
    def set(
        self,
        name: str,
        value: str,
        ex: Optional[int] = None,
        nx: bool = False,
    ) -> object: ...


class RedisIdempotencyStore:
    """Redis-backed fill dedup hook for runtime/projection code.

    The client shape matches redis-py/fakeredis enough for SET key value NX EX.
    """

    def __init__(
        self,
        redis_client: RedisClient,
        key_prefix: str,
        ttl_seconds: Optional[int] = 30 * 24 * 60 * 60,
    ) -> None:
        if not key_prefix:
            raise IdempotencyStoreError("key_prefix must be non-empty")
        self._redis = redis_client
        self._key_prefix = key_prefix.rstrip(":")
        self._ttl_seconds = ttl_seconds

    def claim_fill(self, fill: FillIdentity) -> bool:
        result = self._redis.set(
            name=f"{self._key_prefix}:idempotency:{fill.dedup_key()}",
            value="1",
            ex=self._ttl_seconds,
            nx=True,
        )
        return bool(result)


def _escape(value: str) -> str:
    return value.replace(":", "_")
