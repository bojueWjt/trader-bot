from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

_CONTROL_PLANE = Path(__file__).resolve().parents[1]
if str(_CONTROL_PLANE) not in sys.path:
    sys.path.insert(0, str(_CONTROL_PLANE))

from order_management.freshness import FreshnessConfig, FreshnessState  # noqa: E402
from order_management.identifiers import canonical_account_id  # noqa: E402

from risk_config import decimal_value  # noqa: E402


@dataclass(frozen=True)
class AccountBudget:
    account_id: str
    currency: str
    equity: Decimal
    margin: Decimal
    free_margin: Decimal
    margin_ratio: Decimal
    updated_at: datetime | None
    stale_reasons: list[str]

    @property
    def is_fresh(self) -> bool:
        return not self.stale_reasons

    @property
    def can_take_new_risk(self) -> bool:
        return self.is_fresh and self.equity > 0 and self.free_margin > 0


def load_account_budget(
    conn,
    account_id: str,
    *,
    now: datetime | None = None,
    freshness: FreshnessConfig | None = None,
) -> AccountBudget:
    now = _aware(now or datetime.now(timezone.utc))
    freshness = freshness or FreshnessConfig()
    normalized_account = canonical_account_id(account_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_id, currency, equity, margin, available_balance,
                   last_execution_event_at, projection_lag_ms, updated_at
            FROM accounts_projection
            WHERE account_id=%s
            """,
            (normalized_account,),
        )
        row = cur.fetchone()

    if row is None:
        return AccountBudget(
            account_id=normalized_account,
            currency="",
            equity=Decimal("0"),
            margin=Decimal("0"),
            free_margin=Decimal("0"),
            margin_ratio=Decimal("0"),
            updated_at=None,
            stale_reasons=["account_missing"],
        )

    equity = decimal_value(row[2])
    margin = decimal_value(row[3])
    free_margin = decimal_value(row[4]) if row[4] is not None else max(equity - margin, Decimal("0"))
    updated_at = _aware(row[7])
    reasons = _account_stale_reasons(
        updated_at=updated_at,
        last_execution_event_at=_aware(row[5]) if row[5] is not None else updated_at,
        projection_lag_ms=int(row[6] or 0),
        now=now,
        freshness=freshness,
    )

    return AccountBudget(
        account_id=row[0],
        currency=row[1],
        equity=equity,
        margin=margin,
        free_margin=free_margin,
        margin_ratio=(margin / equity) if equity > 0 else Decimal("0"),
        updated_at=updated_at,
        stale_reasons=reasons,
    )


def _account_stale_reasons(
    *,
    updated_at: datetime,
    last_execution_event_at: datetime,
    projection_lag_ms: int,
    now: datetime,
    freshness: FreshnessConfig,
) -> list[str]:
    state = FreshnessState(
        config=freshness,
        market_data_last_seen_at=now,
        account_data_last_seen_at=last_execution_event_at,
        execution_event_last_seen_at=now,
        projection_applied_at=updated_at,
        reconciliation_verified_at=now,
    )
    reasons = list(state.stale_reasons(now=now))
    if projection_lag_ms > freshness.projection_lag_threshold_ms and "projection_lag" not in reasons:
        reasons.append("projection_lag")
    return reasons


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

