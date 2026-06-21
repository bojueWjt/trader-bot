from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from psycopg2.extras import RealDictCursor

_REPO = Path(__file__).resolve().parents[3]
_EXECUTION_DOMAIN = _REPO / "packages" / "execution-domain"
if str(_EXECUTION_DOMAIN) not in sys.path:
    sys.path.insert(0, str(_EXECUTION_DOMAIN))

from execution_domain.identifiers import canonical_account_id, canonical_instrument_key  # noqa: E402


@dataclass(frozen=True)
class PriceReference:
    mark_price: Decimal | None = None
    last_price: Decimal | None = None
    last_event_at: datetime | None = None
    stale: bool = True


@dataclass(frozen=True)
class PriceGuardConfig:
    stale_after_seconds: int = 30


@dataclass(frozen=True)
class PriceGuardDecision:
    allowed: bool
    reason: str
    reference_price: Decimal | None = None
    intended_price: Decimal | None = None
    deviation_bps: Decimal | None = None


class PriceReferenceStore(Protocol):
    def load_reference(self, *, account_id: str, venue_symbol: str) -> PriceReference | None: ...


class DatabasePriceReferenceStore:
    def __init__(self, conn) -> None:
        self._conn = conn

    def load_reference(self, *, account_id: str, venue_symbol: str) -> PriceReference | None:
        with self._conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT mark_price, last_price, last_event_at, stale
                FROM price_feed_status
                WHERE account_id=%s AND venue_symbol=%s
                ORDER BY CASE WHEN source='combined' THEN 0 ELSE 1 END, updated_at DESC
                LIMIT 1
                """,
                (canonical_account_id(account_id), canonical_instrument_key(venue_symbol)),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return PriceReference(
            mark_price=Decimal(str(row["mark_price"])) if row["mark_price"] is not None else None,
            last_price=Decimal(str(row["last_price"])) if row["last_price"] is not None else None,
            last_event_at=row["last_event_at"],
            stale=bool(row["stale"]),
        )


class PriceGuard:
    def __init__(
        self,
        store: PriceReferenceStore,
        *,
        config: PriceGuardConfig | None = None,
    ) -> None:
        self._store = store
        self._config = config or PriceGuardConfig()

    def check(
        self,
        *,
        account_id: str,
        venue_symbol: str,
        side: str,
        order_type: str,
        intended_price: Decimal | None,
        max_slippage_bps: Decimal | None,
        now: datetime | None = None,
    ) -> PriceGuardDecision:
        del side
        now = _aware(now or datetime.now(timezone.utc))
        reference = self._store.load_reference(account_id=account_id, venue_symbol=venue_symbol)
        if reference is None:
            return PriceGuardDecision(False, "price_reference_missing")
        if reference.stale or reference.last_event_at is None:
            return PriceGuardDecision(False, "price_reference_stale")
        age = (now - _aware(reference.last_event_at)).total_seconds()
        if age > self._config.stale_after_seconds:
            return PriceGuardDecision(False, "price_reference_stale")
        reference_price = _select_reference(reference, order_type)
        if reference_price is None:
            return PriceGuardDecision(False, "price_reference_missing")
        if max_slippage_bps is not None and intended_price is None:
            return PriceGuardDecision(
                False,
                "intended_price_required",
                reference_price=reference_price,
            )
        if max_slippage_bps is None:
            return PriceGuardDecision(True, "price_guard_passed", reference_price=reference_price)
        deviation = ((abs(intended_price - reference_price) / reference_price) * Decimal("10000")).quantize(Decimal("1"))
        if deviation > max_slippage_bps:
            return PriceGuardDecision(
                False,
                "price_deviation_exceeded",
                reference_price=reference_price,
                intended_price=intended_price,
                deviation_bps=deviation,
            )
        return PriceGuardDecision(
            True,
            "price_guard_passed",
            reference_price=reference_price,
            intended_price=intended_price,
            deviation_bps=deviation,
        )


def record_price_guard_rejection(
    conn,
    *,
    execution_job_id: str,
    intent_id: str | None,
    decision: PriceGuardDecision,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE execution_jobs
            SET status='denied',
                completed_at=now(),
                payload=payload || jsonb_build_object(
                    'price_guard_rejection',
                    jsonb_build_object(
                        'intent_id', %s,
                        'reason', %s,
                        'reference_price', %s,
                        'intended_price', %s,
                        'deviation_bps', %s
                    )
                ),
                updated_at=now()
            WHERE execution_job_id=%s
            """,
            (
                intent_id,
                decision.reason,
                str(decision.reference_price) if decision.reference_price is not None else None,
                str(decision.intended_price) if decision.intended_price is not None else None,
                str(decision.deviation_bps) if decision.deviation_bps is not None else None,
                execution_job_id,
            ),
        )


def _select_reference(reference: PriceReference, order_type: str) -> Decimal | None:
    del order_type
    return reference.mark_price or reference.last_price


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
