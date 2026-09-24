#!/usr/bin/env python3
"""One-shot positions_projection convergence from heartbeat.positions.

Default is dry-run. Does not talk to the exchange. Apply path locks
heartbeat and projection tables, re-reads, revalidates, then writes in
one transaction after a backup table and row-count guards.

Usage:
  python3 scripts/ops/converge_heartbeat_positions.py --db-url "$DATABASE_URL"
  python3 scripts/ops/converge_heartbeat_positions.py --db-url "$DATABASE_URL" --apply \\
      --max-closes 80 --expected-open-count 9
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO = Path(__file__).resolve().parents[2]
_API = _REPO / "services" / "control-plane" / "api"
if str(_API) not in sys.path:
    sys.path.insert(0, str(_API))

from position_mapping import (  # noqa: E402
    canonical_instrument_key,
    canonical_position_id,
    hedge_book,
    nautilus_instrument_id,
    position_ids_equivalent,
)

ACCOUNTS = ("account-a", "account-b", "account-c", "account-d")
DEFAULT_MAX_AGE_SECONDS = 10
DEFAULT_LOCK_TIMEOUT = "5s"
FUTURE_SLACK_SECONDS = 2


@dataclass
class ConvergencePlan:
    backup_table: str
    source_row_count: int
    closes: list[dict[str, Any]] = field(default_factory=list)
    upserts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    heartbeats: dict[str, dict[str, Any]] = field(default_factory=dict)
    db_now: datetime | None = None
    max_closes: int | None = None
    expected_open_count: int | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


class SnapshotRejected(Exception):
    pass


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _age_seconds(ts: datetime | None, now: datetime) -> float | None:
    aware = _aware(ts)
    if aware is None:
        return None
    return (now - aware).total_seconds()


def _require_finite_decimal(value: Any, *, field: str) -> Decimal:
    if value in (None, ""):
        raise SnapshotRejected(f"{field} missing")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SnapshotRejected(f"{field} is not numeric") from exc
    if not number.is_finite():
        raise SnapshotRejected(f"{field} is not finite")
    return number


def parse_heartbeat_positions(
    account_id: str,
    positions: Any,
) -> list[dict[str, Any]]:
    if not isinstance(positions, list):
        raise SnapshotRejected("positions snapshot is not a list")
    books: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(positions):
        if not isinstance(raw, dict):
            raise SnapshotRejected(f"row {index} is not an object")
        symbol_raw = str(raw.get("symbol") or raw.get("instrument_id") or "").strip()
        if not symbol_raw:
            raise SnapshotRejected(f"row {index} missing symbol")
        symbol = canonical_instrument_key(symbol_raw)
        if not symbol:
            raise SnapshotRejected(f"row {index} invalid symbol")
        qty_raw = (
            raw.get("position_amt")
            if raw.get("position_amt") is not None
            else raw.get("quantity") if raw.get("quantity") is not None else raw.get("qty")
        )
        quantity = _require_finite_decimal(qty_raw, field=f"row {index} quantity")
        if quantity == 0:
            continue
        side = _snapshot_book(
            raw.get("position_side") or raw.get("positionSide") or raw.get("side"),
            quantity,
            index=index,
        )
        key = (account_id, symbol, side)
        if key in seen:
            raise SnapshotRejected(f"duplicate book {symbol} {side}")
        seen.add(key)
        instrument_id = nautilus_instrument_id(symbol)
        position_id = canonical_position_id(instrument_id, side)
        if not instrument_id or not position_id:
            raise SnapshotRejected(f"row {index} cannot canonicalize")
        books.append(
            {
                "account_id": account_id,
                "position_id": position_id,
                "instrument_id": instrument_id,
                "instrument_symbol": symbol,
                "side": side,
                "quantity": abs(quantity),
                "avg_entry_price": raw.get("entry_price") or raw.get("entryPrice"),
                "mark_price": raw.get("mark_price") or raw.get("markPrice"),
                "status": "open",
            }
        )
    return books


def load_heartbeats(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_id, node_id, last_seen_at, positions_snapshot_at,
                   positions, status, payload
            FROM node_heartbeats
            WHERE account_id = ANY(%s)
            """,
            (list(ACCOUNTS),),
        )
        rows = cur.fetchall()
    out = []
    for account_id, node_id, last_seen_at, snapshot_at, positions, status, payload in rows:
        out.append(
            {
                "account_id": account_id,
                "node_id": node_id,
                "last_seen_at": last_seen_at,
                "positions_snapshot_at": snapshot_at,
                "positions": positions,
                "status": status,
                "payload": payload or {},
            }
        )
    return out


def _snapshot_book(raw_side: Any, quantity: Decimal, *, index: int) -> str:
    token = str(raw_side or "").strip().upper()
    if token in {"LONG", "BUY"}:
        return "long"
    if token in {"SHORT", "SELL"}:
        return "short"
    if token in {"BOTH", "NET"}:
        book = hedge_book(token, quantity)
        if book in {"long", "short"}:
            return book
        raise SnapshotRejected(f"row {index} BOTH/NET quantity is not a signed book")
    raise SnapshotRejected(f"row {index} unrecognized or missing side")


def load_open_projections(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_id, position_id, instrument_id, side::text, quantity,
                   avg_entry_price, mark_price, status, ts_event
            FROM positions_projection
            WHERE status = 'open' AND quantity::numeric != 0
            """
        )
        rows = cur.fetchall()
    return [
        {
            "account_id": row[0],
            "position_id": row[1],
            "instrument_id": row[2],
            "side": row[3],
            "quantity": row[4],
            "avg_entry_price": row[5],
            "mark_price": row[6],
            "status": row[7],
            "ts_event": row[8],
        }
        for row in rows
    ]


def count_positions_projection(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM positions_projection")
        return int(cur.fetchone()[0])


def db_now(conn) -> datetime:
    with conn.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return _aware(cur.fetchone()[0]) or datetime.now(timezone.utc)


def _recon_state(row: Mapping[str, Any]) -> str:
    payload = row.get("payload") or {}
    if isinstance(payload, Mapping):
        return str(payload.get("reconciliation_state") or "").strip().lower()
    return ""


def plan_convergence(
    heartbeats: Sequence[Mapping[str, Any]],
    projections: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    backup_table: str,
    source_row_count: int,
    max_closes: int | None = None,
    expected_open_count: int | None = None,
) -> ConvergencePlan:
    plan = ConvergencePlan(
        backup_table=backup_table,
        source_row_count=source_row_count,
        db_now=now,
        max_closes=max_closes,
        expected_open_count=expected_open_count,
    )
    by_account: dict[str, dict[str, Any]] = {}
    for row in heartbeats:
        account_id = str(row.get("account_id") or "")
        if account_id in by_account:
            plan.errors.append(f"{account_id}: duplicate heartbeat")
            continue
        by_account[account_id] = dict(row)
    for account_id in ACCOUNTS:
        row = by_account.get(account_id)
        if row is None:
            plan.errors.append(f"{account_id}: missing heartbeat")
            continue
        seen_at = _aware(row.get("last_seen_at"))
        snap_at = _aware(row.get("positions_snapshot_at"))
        seen_age = _age_seconds(seen_at, now)
        snap_age = _age_seconds(snap_at, now)
        if seen_age is None or seen_age > max_age_seconds:
            plan.errors.append(f"{account_id}: heartbeat stale age={seen_age}")
        if snap_age is None or snap_age > max_age_seconds:
            plan.errors.append(f"{account_id}: positions snapshot stale age={snap_age}")
        if seen_at is not None and (seen_at - now).total_seconds() > FUTURE_SLACK_SECONDS:
            plan.errors.append(f"{account_id}: last_seen_at in the future")
        if snap_at is not None and (snap_at - now).total_seconds() > FUTURE_SLACK_SECONDS:
            plan.errors.append(f"{account_id}: positions_snapshot_at in the future")
        recon = _recon_state(row)
        if recon != "healthy":
            plan.errors.append(f"{account_id}: reconciliation_state={recon or 'missing'}")
        try:
            books = parse_heartbeat_positions(account_id, row.get("positions"))
        except SnapshotRejected as exc:
            plan.errors.append(f"{account_id}: {exc}")
            continue
        plan.heartbeats[account_id] = {
            "last_seen_at": str(row.get("last_seen_at")),
            "positions_snapshot_at": str(row.get("positions_snapshot_at")),
            "n_pos": len(row.get("positions") or []),
            "open_books": len(books),
        }
        for book in books:
            book["ts_event"] = snap_at
            plan.upserts.append(book)
    if plan.errors:
        return plan

    wanted_ids = {(row["account_id"], row["position_id"]) for row in plan.upserts}
    scoped_accounts = set(ACCOUNTS)
    for row in projections:
        account_id = str(row.get("account_id") or "")
        if account_id not in scoped_accounts:
            continue
        position_id = str(row.get("position_id") or "")
        snap_at = _aware(by_account.get(account_id, {}).get("positions_snapshot_at"))
        ts_event = _aware(row.get("ts_event"))
        if ts_event is not None and snap_at is not None and ts_event > snap_at:
            plan.errors.append(
                f"{account_id} {position_id}: projection ts_event newer than snapshot"
            )
            continue
        if (account_id, position_id) in wanted_ids:
            continue
        canonical = canonical_position_id(row.get("instrument_id"), row.get("side"))
        alias_of_wanted = bool(
            canonical and (account_id, canonical) in wanted_ids and canonical != position_id
        )
        if not alias_of_wanted and canonical:
            for wanted_account, wanted_id in wanted_ids:
                if wanted_account == account_id and position_ids_equivalent(
                    position_id, wanted_id, side=row.get("side")
                ):
                    alias_of_wanted = True
                    break
        if alias_of_wanted:
            plan.closes.append(
                {
                    "account_id": account_id,
                    "position_id": position_id,
                    "instrument_id": row.get("instrument_id"),
                    "side": row.get("side"),
                    "quantity": Decimal("0"),
                    "status": "closed",
                    "reason": "alias",
                    "ts_event": by_account.get(account_id, {}).get("positions_snapshot_at"),
                }
            )
            continue
        plan.closes.append(
            {
                "account_id": account_id,
                "position_id": position_id,
                "instrument_id": row.get("instrument_id"),
                "side": row.get("side"),
                "quantity": Decimal("0"),
                "status": "closed",
                "reason": "ghost",
                "ts_event": by_account.get(account_id, {}).get("positions_snapshot_at"),
            }
        )
    if max_closes is not None and len(plan.closes) > max_closes:
        plan.errors.append(
            f"close count {len(plan.closes)} exceeds --max-closes {max_closes}"
        )
    if expected_open_count is not None and len(plan.upserts) != expected_open_count:
        plan.errors.append(
            f"open snapshot {len(plan.upserts)} != --expected-open-count {expected_open_count}"
        )
    return plan


def _quote_ident(name: str) -> str:
    if not name.replace("_", "").isalnum() or name[0].isdigit():
        raise ValueError(f"invalid backup table name: {name}")
    return name


def _lock_tables(conn, lock_timeout: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = %s", (lock_timeout,))
        cur.execute("LOCK TABLE node_heartbeats IN SHARE ROW EXCLUSIVE MODE")
        cur.execute("LOCK TABLE positions_projection IN SHARE ROW EXCLUSIVE MODE")


def apply_plan(
    conn,
    plan: ConvergencePlan,
    *,
    dry_run: bool,
    lock_timeout: str = DEFAULT_LOCK_TIMEOUT,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    max_closes: int | None = None,
    expected_open_count: int | None = None,
) -> dict[str, Any]:
    result = {
        "dry_run": dry_run,
        "ok": plan.ok,
        "errors": list(plan.errors),
        "backup_table": plan.backup_table,
        "source_row_count": plan.source_row_count,
        "close_count": len(plan.closes),
        "upsert_count": len(plan.upserts),
        "heartbeats": plan.heartbeats,
    }
    if not dry_run:
        if max_closes is None and plan.max_closes is None:
            raise RuntimeError("--apply requires --max-closes")
        if expected_open_count is None and plan.expected_open_count is None:
            raise RuntimeError("--apply requires --expected-open-count")
    if dry_run or not plan.ok:
        result["closes"] = plan.closes
        result["upserts"] = [
            {**row, "quantity": str(row["quantity"])} for row in plan.upserts
        ]
        return result

    backup = _quote_ident(plan.backup_table)
    with conn:
        _lock_tables(conn, lock_timeout)
        now = db_now(conn)
        heartbeats = load_heartbeats(conn)
        projections = load_open_projections(conn)
        source_row_count = count_positions_projection(conn)
        locked = plan_convergence(
            heartbeats,
            projections,
            now=now,
            max_age_seconds=max_age_seconds,
            backup_table=plan.backup_table,
            source_row_count=source_row_count,
            max_closes=max_closes if max_closes is not None else plan.max_closes,
            expected_open_count=(
                expected_open_count
                if expected_open_count is not None
                else plan.expected_open_count
            ),
        )
        if not locked.ok:
            raise RuntimeError("revalidation failed: " + "; ".join(locked.errors))
        plan = locked
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE {backup} AS TABLE positions_projection"
            )
            cur.execute(f"SELECT count(*) FROM {backup}")
            backup_count = int(cur.fetchone()[0])
            if backup_count != plan.source_row_count:
                raise RuntimeError(
                    f"backup row count {backup_count} != "
                    f"source {plan.source_row_count}"
                )
            for row in plan.closes:
                cur.execute(
                    """
                    UPDATE positions_projection
                       SET quantity = 0,
                           status = 'closed',
                           ts_event = COALESCE(%s, ts_event),
                           updated_at = now(),
                           payload = COALESCE(payload, '{}'::jsonb) || jsonb_build_object(
                               'quantity', 0,
                               'status', 'closed',
                               'convergence_reason', %s,
                               'convergence_snapshot_at', %s::text,
                               'convergence_backup_table', %s
                           )
                     WHERE account_id = %s AND position_id = %s
                       AND (ts_event IS NULL OR ts_event <= COALESCE(%s, ts_event))
                    """,
                    (
                        row.get("ts_event"),
                        row.get("reason") or "ghost",
                        row.get("ts_event"),
                        backup,
                        row["account_id"],
                        row["position_id"],
                        row.get("ts_event"),
                    ),
                )
                if int(cur.rowcount or 0) == 0:
                    raise RuntimeError(
                        f"close skipped {row['account_id']} {row['position_id']}: "
                        "projection newer than snapshot or missing"
                    )
            for row in plan.upserts:
                cur.execute(
                    """
                    INSERT INTO positions_projection (
                        account_id, position_id, instrument_id, side, quantity,
                        avg_entry_price, mark_price, status, ts_event, updated_at,
                        payload
                    )
                    VALUES (
                        %s,%s,%s,%s,%s,%s,%s,'open', %s, now(),
                        jsonb_build_object(
                            'quantity', %s::text,
                            'status', 'open',
                            'convergence_snapshot_at', %s::text,
                            'convergence_backup_table', %s
                        )
                    )
                    ON CONFLICT (account_id, position_id) DO UPDATE SET
                        instrument_id = EXCLUDED.instrument_id,
                        side = EXCLUDED.side,
                        quantity = EXCLUDED.quantity,
                        avg_entry_price = EXCLUDED.avg_entry_price,
                        mark_price = EXCLUDED.mark_price,
                        status = 'open',
                        ts_event = EXCLUDED.ts_event,
                        updated_at = now(),
                        payload = COALESCE(positions_projection.payload, '{}'::jsonb)
                                  || EXCLUDED.payload
                    WHERE positions_projection.ts_event IS NULL
                       OR EXCLUDED.ts_event >= positions_projection.ts_event
                    """,
                    (
                        row["account_id"],
                        row["position_id"],
                        row["instrument_id"],
                        row["side"],
                        row["quantity"],
                        row.get("avg_entry_price"),
                        row.get("mark_price"),
                        row.get("ts_event") or now,
                        str(row["quantity"]),
                        row.get("ts_event") or now,
                        backup,
                    ),
                )
                if int(cur.rowcount or 0) == 0:
                    raise RuntimeError(
                        f"upsert skipped {row['account_id']} {row['position_id']}: "
                        "projection newer than snapshot"
                    )
            cur.execute(
                """
                SELECT account_id, position_id, quantity
                FROM positions_projection
                WHERE account_id = ANY(%s)
                  AND status = 'open' AND quantity::numeric != 0
                """,
                (list(ACCOUNTS),),
            )
            got = {
                (str(item[0]), str(item[1]), Decimal(str(item[2])))
                for item in cur.fetchall()
            }
            want = {
                (str(row["account_id"]), str(row["position_id"]), Decimal(str(row["quantity"])))
                for row in plan.upserts
            }
            if got != want:
                raise RuntimeError(
                    f"open books {sorted(got)} != snapshot {sorted(want)}"
                )
    result["applied"] = True
    result["ok"] = True
    result["errors"] = []
    result["close_count"] = len(plan.closes)
    result["upsert_count"] = len(plan.upserts)
    result["open_count"] = len(plan.upserts)
    return result


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--apply", action="store_true", help="write (default dry-run)")
    parser.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    parser.add_argument("--lock-timeout", default=DEFAULT_LOCK_TIMEOUT)
    parser.add_argument("--max-closes", type=int, default=None)
    parser.add_argument("--expected-open-count", type=int, default=None)
    parser.add_argument(
        "--backup-table",
        default="",
        help="override backup table name; default positions_projection_hb_backup_<utc>",
    )
    args = parser.parse_args(argv)
    if not args.db_url:
        parser.error("--db-url or DATABASE_URL is required")
    if args.apply and (args.max_closes is None or args.expected_open_count is None):
        parser.error("--apply requires --max-closes and --expected-open-count")
    import psycopg2

    conn = psycopg2.connect(args.db_url)
    try:
        now = db_now(conn)
        suffix = now.strftime("%Y%m%dT%H%M%SZ")
        backup_table = args.backup_table or f"positions_projection_hb_backup_{suffix}"
        heartbeats = load_heartbeats(conn)
        projections = load_open_projections(conn)
        source_row_count = count_positions_projection(conn)
        plan = plan_convergence(
            heartbeats,
            projections,
            now=now,
            max_age_seconds=args.max_age_seconds,
            backup_table=backup_table,
            source_row_count=source_row_count,
            max_closes=args.max_closes,
            expected_open_count=args.expected_open_count,
        )
        result = apply_plan(
            conn,
            plan,
            dry_run=not args.apply,
            lock_timeout=args.lock_timeout,
            max_age_seconds=args.max_age_seconds,
            max_closes=args.max_closes,
            expected_open_count=args.expected_open_count,
        )
    finally:
        conn.close()
    print(json.dumps(_json_ready(result), indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
