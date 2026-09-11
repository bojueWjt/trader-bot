"""GET /v1/outcomes — M1c read model (contracts/backend-api.md §4)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Header, Query
from psycopg2.extras import RealDictCursor

from outcomes_kpis import bucket_r_values, compute_kpis, watermark as kpi_watermark

router = APIRouter()

_JOB_NAME = "trade_outcomes"
_JOB_STATUS = "succeeded"


@router.get("/v1/outcomes")
def v1_outcomes(
    days: int = Query(default=7, ge=1, le=90),
    account_id: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    from read_api import (  # import inside handler to avoid circular import
        _envelope,
        _f,
        _iso,
        _read_conn,
        _symbol,
        require_reader,
    )

    require_reader(authorization)
    account_filter = account_id.strip() if account_id and account_id.strip() else None
    conn = _read_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            env = _envelope(cur)
            sql = (
                "SELECT instrument_id, side, realized_pnl, r_multiple, holding_seconds, "
                "fees, entry_avg_price, exit_avg_price, closed_at, account_id "
                "FROM trade_outcomes "
                "WHERE closed_at >= now() - (%s * INTERVAL '1 day')"
            )
            params: list[object] = [days]
            if account_filter is not None:
                sql += " AND account_id = %s"
                params.append(account_filter)
            sql += " ORDER BY closed_at DESC"
            cur.execute(sql, params)
            raw_rows = [dict(r) for r in cur.fetchall()]
            cur.execute(
                """
                SELECT completed_at
                FROM trade_outcome_job_runs
                WHERE job_name = %s AND status = %s
                """,
                (_JOB_NAME, _JOB_STATUS),
            )
            job = cur.fetchone()
            completed_at = job["completed_at"] if job else None

        rows = []
        for raw in raw_rows:
            holding = raw.get("holding_seconds")
            rows.append(
                {
                    "symbol": _symbol(raw.get("instrument_id")),
                    "direction": raw.get("side"),
                    "realized_pnl": _f(raw.get("realized_pnl")),
                    "r_multiple": _f(raw.get("r_multiple")),
                    "holding_seconds": None if holding is None else int(holding),
                    "fees": _f(raw.get("fees")),
                    "entry_avg": _f(raw.get("entry_avg_price")),
                    "exit_avg": _f(raw.get("exit_avg_price")),
                    "closed_at": _iso(raw.get("closed_at")),
                    "account_id": raw.get("account_id"),
                }
            )
        return {
            **env,
            "data": {
                "rows": rows,
                "kpis": compute_kpis(rows),
                "r_distribution": bucket_r_values(rows),
                "watermark": kpi_watermark(completed_at, datetime.now(timezone.utc)),
            },
        }
    finally:
        conn.close()
