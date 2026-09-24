"""Read-only Binance USDT-M income collector.

Reuses exchange_state_recorder.container_keys / account_binance_opener /
signed_get. Keys stay in process memory and are never printed.

Does not place orders. This module is not invoked with live keys in tests.
Default window is the last completed UTC day ending today 00:00, matching
report_data.completed_utc_window.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from psycopg2.extras import Json

_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import exchange_state_recorder as esr  # noqa: E402

INCOME_PATH = "/fapi/v1/income"
PAGE_LIMIT = 1000
MAX_PAGES = 50
MAX_WINDOW = timedelta(days=7)
RETENTION = timedelta(days=90)
REPORT_INCOME_TYPES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")

UPSERT_INCOME_SQL = """
INSERT INTO exchange_income (
    account_id, tran_id, income_type, symbol, income, asset, info,
    trade_id, income_time, payload, ingested_at
)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb, now())
ON CONFLICT (account_id, income_type, tran_id)
DO NOTHING
"""

UPSERT_COVERAGE_SQL = """
INSERT INTO exchange_income_coverage (
    account_id, window_start, window_end, income_type, complete,
    row_count, collected_at, error
)
VALUES (%s,%s,%s,'*',%s,%s, now(), %s)
ON CONFLICT (account_id, window_start, window_end, income_type) DO UPDATE SET
    complete = EXCLUDED.complete,
    row_count = EXCLUDED.row_count,
    collected_at = EXCLUDED.collected_at,
    error = EXCLUDED.error
"""


def log(msg: str) -> None:
    esr.log(_safe_error(msg))


def _safe_error(exc: Any) -> str:
    if isinstance(exc, BaseException):
        text = f"{type(exc).__name__}"
        reason = str(exc) or ""
    else:
        text = str(exc)
        reason = text
    lowered = (text + " " + reason).lower()
    blocked = (
        "api_key",
        "api-secret",
        "api_secret",
        "signature=",
        "x-mbx-apikey",
        "http://",
        "https://",
        "?starttime",
        "recvwindow",
    )
    if any(token in lowered for token in blocked):
        if isinstance(exc, BaseException):
            return type(exc).__name__
        return "redacted_error"
    if isinstance(exc, BaseException):
        reason = reason.replace("\n", " ").strip()
        if len(reason) > 160:
            reason = reason[:160]
        return f"{type(exc).__name__}: {reason}" if reason else type(exc).__name__
    return text[:200]


def _ms(ts: datetime) -> int:
    return int(_aware(ts).timestamp() * 1000)


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _from_ms(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _decimal(value: Any) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite():
        raise InvalidOperation("income is not finite")
    return number


def completed_utc_window(
    days: int,
    now: datetime,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[datetime, datetime]:
    now = _aware(now)
    if days <= 0 and (start is None or end is None):
        raise ValueError("invalid_window_days")
    if start is not None and end is not None:
        window = (_aware(start), _aware(end))
    else:
        completed_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if end is not None:
            completed_end = _aware(end)
        window_start = _aware(start) if start is not None else completed_end - timedelta(days=days)
        window = (window_start, completed_end)
    if window[1] <= window[0]:
        raise ValueError("window_end_not_after_start")
    return window


def validate_window(start: datetime, end: datetime, *, now: datetime) -> None:
    start = _aware(start)
    end = _aware(end)
    now = _aware(now)
    if end <= start:
        raise ValueError("window_end_not_after_start")
    if end > now:
        raise ValueError("window_end_in_future")
    if start < now - RETENTION:
        raise ValueError("window_outside_90d_retention")


def chunk_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    start = _aware(start)
    end = _aware(end)
    if end <= start:
        return []
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + MAX_WINDOW, end)
        windows.append((cursor, nxt))
        cursor = nxt
    return windows


def paginate_income(
    fetch_page: Callable[[int], list[Any]],
    *,
    limit: int = PAGE_LIMIT,
    max_pages: int = MAX_PAGES,
) -> tuple[list[Any], bool]:
    rows: list[Any] = []
    seen: set[tuple[Any, ...]] = set()
    page = 1
    while page <= max_pages:
        chunk = fetch_page(page)
        if chunk is None:
            raise TypeError("income_response_none")
        if not isinstance(chunk, list):
            raise TypeError("income_response_not_list")
        for item in chunk:
            if not isinstance(item, dict):
                raise TypeError("income_row_not_object")
            key = (
                str(item.get("tranId") or ""),
                str(item.get("incomeType") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
        if len(chunk) < limit:
            return rows, True
        page += 1
    return rows, False


def normalize_income_row(account_id: str, item: Mapping[str, Any]) -> dict[str, Any]:
    tran_id = str(item.get("tranId") or "").strip()
    income_type = str(item.get("incomeType") or "").strip()
    asset = str(item.get("asset") or "").strip()
    time_ms = item.get("time")
    if not tran_id or not income_type or not asset or time_ms in (None, ""):
        raise ValueError("income_row_missing_fields")
    income = _decimal(item.get("income"))
    income_time = _from_ms(time_ms)
    symbol = str(item.get("symbol") or "")
    trade_id = item.get("tradeId")
    return {
        "account_id": account_id,
        "tran_id": tran_id,
        "symbol": symbol,
        "income_type": income_type,
        "income": income,
        "asset": asset,
        "info": item.get("info"),
        "trade_id": None if trade_id in (None, "") else str(trade_id),
        "income_time": income_time,
        "payload": dict(item),
    }


def upsert_income_rows(conn, rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    inserted = 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                UPSERT_INCOME_SQL,
                (
                    row["account_id"],
                    row["tran_id"],
                    row["income_type"],
                    row["symbol"],
                    row["income"],
                    row["asset"],
                    row.get("info"),
                    row.get("trade_id"),
                    row["income_time"],
                    Json(row.get("payload") or {}),
                ),
            )
            inserted += int(cur.rowcount or 0)
    return inserted


def upsert_coverage(
    conn,
    *,
    account_id: str,
    window_start: datetime,
    window_end: datetime,
    complete: bool,
    row_count: int,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            UPSERT_COVERAGE_SQL,
            (
                account_id,
                _aware(window_start),
                _aware(window_end),
                complete,
                int(row_count),
                error,
            ),
        )


def coverage_contains(
    rows: Sequence[Mapping[str, Any]],
    *,
    account_id: str,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    start = _aware(window_start)
    end = _aware(window_end)
    covered: list[tuple[datetime, datetime]] = []
    for row in rows:
        if str(row.get("account_id")) != account_id:
            continue
        if not row.get("complete"):
            continue
        ws = _aware(row["window_start"])
        we = _aware(row["window_end"])
        if we <= start or ws >= end:
            continue
        covered.append((max(ws, start), min(we, end)))
    covered.sort()
    cursor = start
    for ws, we in covered:
        if ws > cursor:
            return False
        if we > cursor:
            cursor = we
        if cursor >= end:
            return True
    return cursor >= end


def collect_account_window(
    *,
    account_id: str,
    start: datetime,
    end: datetime,
    fetch_page: Callable[[datetime, datetime, int], list[Any]],
    limit: int = PAGE_LIMIT,
    max_pages: int = MAX_PAGES,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Fetch [start, end) with 7-day chunks and page/limit pagination."""
    now = now or datetime.now(timezone.utc)
    try:
        validate_window(start, end, now=now)
    except ValueError as exc:
        return [], False, str(exc)
    normalized: list[dict[str, Any]] = []
    complete = True
    error = None
    pending = chunk_windows(start, end)
    if not pending:
        return [], False, "window_end_not_after_start"
    while pending:
        ws, we = pending.pop(0)
        start_ms = _ms(ws)
        end_ms = _ms(we) - 1
        if end_ms < start_ms:
            complete = False
            error = "window_chunk_empty"
            break
        try:
            raw_rows, chunk_complete = paginate_income(
                lambda page, _ws=ws, _we=we: fetch_page(_ws, _we, page),
                limit=limit,
                max_pages=max_pages,
            )
        except Exception as exc:  # noqa: BLE001
            complete = False
            error = _safe_error(exc)
            log(f"{account_id}: income fetch failed for window")
            break
        if not chunk_complete:
            mid = ws + (we - ws) / 2
            if mid <= ws or (we - ws) <= timedelta(seconds=1):
                complete = False
                error = "income_page_overflow"
                break
            pending.insert(0, (mid, we))
            pending.insert(0, (ws, mid))
            continue
        try:
            for item in raw_rows:
                row = normalize_income_row(account_id, item)
                if not (ws <= row["income_time"] < we):
                    raise ValueError("income_time_outside_window")
                normalized.append(row)
        except Exception as exc:  # noqa: BLE001
            complete = False
            error = _safe_error(exc)
            break
    if not complete:
        return [], False, error
    return normalized, True, None


def _signed_page_factory(base: str, key: str, sec: str, opener, signed_get):
    def fetch_page(window_start: datetime, window_end: datetime, page: int) -> list[Any]:
        params = {
            "startTime": _ms(window_start),
            "endTime": _ms(window_end) - 1,
            "limit": PAGE_LIMIT,
            "page": page,
        }
        payload = signed_get(
            base,
            INCOME_PATH,
            key,
            sec,
            params,
            opener=opener,
        )
        if payload is None:
            raise TypeError("income_response_none")
        if not isinstance(payload, list):
            raise TypeError("income_response_not_list")
        return payload

    return fetch_page


def run_once(
    conn,
    *,
    start: datetime,
    end: datetime,
    base: str,
    opener_factory=None,
    signed_get=None,
    keys_for_account=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    signed_get = signed_get or esr.signed_get
    opener_factory = opener_factory or esr.account_binance_opener
    keys_for_account = keys_for_account or (
        lambda account_id: esr.container_keys(*esr.ACCOUNTS[account_id])
    )
    summary: dict[str, Any] = {"accounts": {}, "complete": True}
    try:
        validate_window(start, end, now=now)
    except ValueError as exc:
        summary["complete"] = False
        summary["error"] = str(exc)
        return summary
    for account_id in esr.ACCOUNTS:
        creds = keys_for_account(account_id)
        if not creds:
            summary["accounts"][account_id] = {
                "status": "skipped",
                "reason": "keys_unavailable",
            }
            summary["complete"] = False
            upsert_coverage(
                conn,
                account_id=account_id,
                window_start=start,
                window_end=end,
                complete=False,
                row_count=0,
                error="keys_unavailable",
            )
            conn.commit()
            continue
        key, sec = creds
        opener = opener_factory(account_id)
        fetch_page = _signed_page_factory(base, key, sec, opener, signed_get)
        rows, complete, error = collect_account_window(
            account_id=account_id,
            start=start,
            end=end,
            fetch_page=fetch_page,
            now=now,
        )
        inserted = upsert_income_rows(conn, rows) if complete else 0
        upsert_coverage(
            conn,
            account_id=account_id,
            window_start=start,
            window_end=end,
            complete=complete,
            row_count=len(rows) if complete else 0,
            error=error,
        )
        conn.commit()
        summary["accounts"][account_id] = {
            "status": "ok" if complete else "incomplete",
            "rows": len(rows) if complete else 0,
            "inserted": inserted,
            "complete": complete,
            "error": error,
        }
        if not complete:
            summary["complete"] = False
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument(
        "--base-url",
        default=os.environ.get("EXCHANGE_STATE_BASE_URL", "https://fapi.binance.com"),
    )
    parser.add_argument("--start", help="UTC ISO start of half-open window")
    parser.add_argument("--end", help="UTC ISO end of half-open window")
    parser.add_argument("--as-of", dest="as_of", help="UTC ISO clock for default completed-day window")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--once", action="store_true", default=True)
    args = parser.parse_args(argv)
    if not args.db_url:
        parser.error("--db-url or DATABASE_URL is required")
    now = datetime.now(timezone.utc)
    if args.as_of:
        now = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
    start = (
        datetime.fromisoformat(args.start.replace("Z", "+00:00"))
        if args.start
        else None
    )
    end = (
        datetime.fromisoformat(args.end.replace("Z", "+00:00"))
        if args.end
        else None
    )
    window_start, window_end = completed_utc_window(
        args.days, now, start=start, end=end
    )
    import psycopg2

    conn = psycopg2.connect(args.db_url)
    try:
        summary = run_once(
            conn,
            start=window_start,
            end=window_end,
            base=args.base_url,
            now=now,
        )
    finally:
        conn.close()
    log(
        "income collector "
        f"complete={summary.get('complete')} accounts={len(summary.get('accounts') or {})}"
    )
    return 0 if summary.get("complete") else 2


if __name__ == "__main__":
    raise SystemExit(main())
