"""Standalone market snapshot recorder (record-only; not a decision engine).

Watches hermes_decisions for fresh actionable rows and writes one
context_snapshots(snapshot_type='market') row per raw message — the same
market-v1 payload the (currently dormant) worker path produces. Fully
decoupled from ordering: it reads decision rows, writes snapshot rows, and
touches nothing else; any failure here is invisible to the trading pipeline.

Freshness guard: only decisions younger than --max-age-seconds are snapshotted.
Attaching current market data to an old decision would poison later analysis,
so older unsnapshotted rows are skipped permanently.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from market_context import MARKET_CONTEXT_VERSION, MarketContextFetcher  # noqa: E402

RECORDER_VERSION = "market-snapshot-recorder-v1"
ACTIONABLE_ACTIONS = (
    "open_position",
    "add_position",
    "partial_close",
    "close_position",
    "move_stop_loss",
    "move_stop_to_entry",
    "replace_take_profits",
)

PENDING_SQL = """
    SELECT hd.decision_id::text, hd.raw_message_id::text, hd.instrument_symbol
    FROM hermes_decisions hd
    WHERE hd.action::text = ANY(%(actions)s)
      AND hd.instrument_symbol IS NOT NULL
      AND btrim(hd.instrument_symbol) <> ''
      AND hd.created_at >= now() - make_interval(secs => %(max_age)s)
      AND NOT EXISTS (
          SELECT 1 FROM context_snapshots cs
          WHERE cs.raw_message_id = hd.raw_message_id
            AND cs.snapshot_type = 'market'
      )
    ORDER BY hd.created_at
    LIMIT 50
"""

INSERT_SQL = """
    INSERT INTO context_snapshots
        (context_snapshot_id, raw_message_id, snapshot_type, context_version, snapshot)
    VALUES (%s, %s, 'market', %s, %s)
"""


def _log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} {message}", flush=True)


def _skipped_snapshot(symbol: str, reason: str, detail: str) -> dict[str, Any]:
    return {
        "context_version": MARKET_CONTEXT_VERSION,
        "symbol": symbol,
        "status": "skipped",
        "reason": reason,
        "detail": detail[:500],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "partial_failures": [],
    }


def scan_once(conn: Any, fetcher: Any, *, max_age_seconds: int) -> int:
    """One scan pass; returns number of snapshot rows written."""
    with conn.cursor() as cur:
        cur.execute(PENDING_SQL, {"actions": list(ACTIONABLE_ACTIONS), "max_age": max_age_seconds})
        pending = cur.fetchall()

    written = 0
    for decision_id, raw_message_id, symbol in pending:
        try:
            snapshot = fetcher.fetch(symbol)
            if not isinstance(snapshot, dict):
                snapshot = _skipped_snapshot(symbol, "market_context_fetch_failed", "non-object snapshot")
        except Exception as exc:  # fail-open: a bad fetch still leaves a traceable record
            snapshot = _skipped_snapshot(symbol, "market_context_fetch_failed", str(exc))

        snapshot.setdefault("context_version", MARKET_CONTEXT_VERSION)
        snapshot.setdefault("symbol", symbol)
        snapshot.setdefault("partial_failures", [])
        snapshot["recorder"] = RECORDER_VERSION
        snapshot["decision_id"] = decision_id

        with conn.cursor() as cur:
            cur.execute(INSERT_SQL, (str(uuid4()), raw_message_id, MARKET_CONTEXT_VERSION, Json(snapshot)))
        conn.commit()
        written += 1
        _log(f"snapshot written decision={decision_id} symbol={symbol} status={snapshot.get('status', 'ok')}")
    return written


def run_loop(db_url: str, *, interval: float, max_age_seconds: int) -> None:
    stop = {"flag": False}

    def _sigterm(_signo: int, _frame: Any) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    fetcher = MarketContextFetcher()
    conn = None
    _log(f"{RECORDER_VERSION} started interval={interval}s max_age={max_age_seconds}s")
    while not stop["flag"]:
        try:
            if conn is None or conn.closed:
                conn = psycopg2.connect(db_url)
            scan_once(conn, fetcher, max_age_seconds=max_age_seconds)
        except Exception as exc:
            _log(f"scan error (will retry): {exc}")
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None
        deadline = time.monotonic() + interval
        while not stop["flag"] and time.monotonic() < deadline:
            time.sleep(0.5)
    if conn is not None and not conn.closed:
        conn.close()
    _log("stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record market context snapshots for fresh Hermes decisions.")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"), help="Postgres URL (default: $DATABASE_URL)")
    parser.add_argument("--interval", type=float, default=20.0, help="Loop scan interval seconds")
    parser.add_argument("--max-age-seconds", type=int, default=900, help="Only snapshot decisions younger than this")
    parser.add_argument("--once", action="store_true", help="Single scan pass, then exit")
    parser.add_argument("--probe", metavar="SYMBOL", help="Fetch SYMBOL and print the payload; no DB access")
    args = parser.parse_args(argv)

    if args.probe:
        payload = MarketContextFetcher().fetch(args.probe)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    if not args.db_url:
        print("DATABASE_URL not set and --db-url missing", file=sys.stderr)
        return 2

    if args.once:
        conn = psycopg2.connect(args.db_url)
        try:
            written = scan_once(conn, MarketContextFetcher(), max_age_seconds=args.max_age_seconds)
        finally:
            conn.close()
        _log(f"once: wrote {written} snapshot(s)")
        return 0

    run_loop(args.db_url, interval=args.interval, max_age_seconds=args.max_age_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
