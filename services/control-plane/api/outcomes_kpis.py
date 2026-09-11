"""KPI arithmetic for GET /v1/outcomes (contracts/backend-api.md §4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

WATERMARK_STALE = timedelta(hours=36)

# Copied from services/report/report_service.py bucket_r_values.
R_BUCKETS = (
    ("≤-2R", None, -2),
    ("-2~-1R", -2, -1),
    ("-1~0R", -1, 0),
    ("0~1R", 0, 1),
    ("1~2R", 1, 2),
    (">2R", 2, None),
)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def bucket_r_values(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counts = {label: 0 for label, _, _ in R_BUCKETS}
    for row in rows:
        value = row.get("r_multiple")
        if value is None:
            continue
        r = float(value)
        for label, low, high in R_BUCKETS:
            if (low is None or r > low) and (high is None or r <= high):
                counts[label] += 1
                break
    return [{"bucket": label, "count": count} for label, count in counts.items()]


def compute_kpis(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    if count == 0:
        return {
            "win_rate": None,
            "profit_factor": None,
            "avg_r": None,
            "avg_holding_seconds": None,
        }

    wins = 0
    gross_profit = 0.0
    gross_loss = 0.0
    r_values: list[float] = []
    holding_values: list[float] = []
    for row in rows:
        pnl = _to_float(row.get("realized_pnl"))
        if pnl is not None:
            if pnl > 0:
                wins += 1
                gross_profit += pnl
            elif pnl < 0:
                gross_loss += pnl
        r_value = _to_float(row.get("r_multiple"))
        if r_value is not None:
            r_values.append(r_value)
        holding = row.get("holding_seconds")
        if holding is not None:
            holding_values.append(float(holding))

    if gross_loss == 0:
        profit_factor: float | None = None
    elif gross_profit == 0:
        profit_factor = 0.0
    else:
        profit_factor = gross_profit / abs(gross_loss)

    return {
        "win_rate": wins / count,
        "profit_factor": profit_factor,
        "avg_r": None if not r_values else sum(r_values) / len(r_values),
        "avg_holding_seconds": (
            None if not holding_values else sum(holding_values) / len(holding_values)
        ),
    }


def watermark(
    completed_at: datetime | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    if completed_at is None:
        return {"materialized_at": None, "stale": True}
    stamped = completed_at
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    stamped = stamped.astimezone(timezone.utc)
    return {
        "materialized_at": stamped.isoformat(),
        "stale": (current - stamped) > WATERMARK_STALE,
    }
