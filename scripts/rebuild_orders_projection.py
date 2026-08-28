#!/usr/bin/env python3
"""Rebuild orders_projection from execution_events per account.

Dry-run (default) folds every Order* execution event through the same
transition rules as order_management.order_reducer and reports the diff
against the live orders_projection rows; --apply atomically replaces the
projection (and advances the 'orders' projection watermarks) under locks.

Structure mirrors scripts/rebuild_positions_projection.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, TextIO
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE = REPO_ROOT / "services" / "control-plane"
if str(CONTROL_PLANE) not in sys.path:
    sys.path.insert(0, str(CONTROL_PLANE))

from order_management.db_helpers import decimal_or_none, ensure_aware  # noqa: E402
from order_management.identifiers import (  # noqa: E402
    canonical_account_id,
    canonical_instrument_key,
)
from order_management.order_reducer import (  # noqa: E402
    _explicit_target,
    _normalize_event,
    _side,
)
from order_management.state_descriptor import (  # noqa: E402
    event_target,
    legal_transition,
    state_rank,
    transition_target,
)

SHADOW_TABLE = "orders_projection_rebuild_shadow"

SHADOW_COLUMNS = (
    "order_projection_id",
    "account_id",
    "instrument_id",
    "venue_symbol",
    "intent_id",
    "client_order_id",
    "venue_order_id",
    "status",
    "side",
    "order_type",
    "quantity",
    "filled_quantity",
    "price",
    "average_fill_price",
    "lifecycle_role",
    "updated_from_event_id",
    "ts_event",
    "payload",
)


def _reducer_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Replicate read_api._derive_projection_from_event preprocessing."""
    event_type = str(event.get("event_type") or "")
    if not event_type.startswith("Order") or not event.get("account_id"):
        return None
    payload = dict(event.get("payload") or {})
    nested_order = payload.get("order")
    if isinstance(nested_order, dict):
        payload.update(nested_order)
    client_order_id = event.get("client_order_id") or payload.get("client_order_id")
    venue_order_id = event.get("venue_order_id") or payload.get("venue_order_id")
    if not client_order_id and not venue_order_id:
        return None
    side = _side(payload)
    if side is not None:
        payload["side"] = side
    if payload.get("order_type") is not None:
        payload["order_type"] = str(payload.get("order_type"))
    return _normalize_event(
        {
            **event,
            "client_order_id": client_order_id,
            "venue_order_id": venue_order_id,
            "payload": payload,
        }
    )


def rebuild_order_rows(
    events: Iterable[dict[str, Any]],
    *,
    accounts: frozenset[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    account_watermarks: dict[str, tuple[str, datetime]] = {}
    stats = {"events_folded": 0, "illegal_transitions": 0, "stale_backfills": 0}
    for event in sorted(events, key=_event_sort_key):
        normalized = _reducer_event(event)
        if normalized is None:
            continue
        account_id = normalized["account_id"]
        if accounts is not None and account_id not in accounts:
            continue
        stats["events_folded"] += 1
        account_watermarks[account_id] = (
            str(normalized["event_id"]),
            normalized["ts_event"],
        )

        key = (account_id, normalized["client_order_id"])
        current = rows.get(key)
        current_status = current["status"] if current else None
        payload = normalized["payload"]
        target = event_target("order", normalized["event_type"], payload)
        if target is None:
            target = transition_target("order", current_status, normalized["event_type"])
        if target is None:
            target = _explicit_target(payload) or "lost"
        if target == "self":
            target = current_status if current else "submitted"

        if current_status is not None and target != current_status:
            stale_backfill = (
                normalized["ts_event"] < current["ts_event"]
                and state_rank("order", target) <= state_rank("order", current_status)
            )
            if stale_backfill:
                stats["stale_backfills"] += 1
                continue
            if not legal_transition("order", current_status, target):
                stats["illegal_transitions"] += 1
                continue

        instrument_id = str(payload.get("instrument_id") or "UNKNOWN")
        new_filled = (
            decimal_or_none(payload.get("filled_qty"))
            or (current or {}).get("filled_quantity")
            or Decimal("0")
        )
        if current:
            new_filled = max(new_filled, current["filled_quantity"] or Decimal("0"))
        merged_payload = dict((current or {}).get("payload") or {})
        merged_payload.update(payload)
        rows[key] = {
            "order_projection_id": (
                current["order_projection_id"] if current else str(uuid4())
            ),
            "account_id": account_id,
            "instrument_id": instrument_id,
            "venue_symbol": canonical_instrument_key(instrument_id),
            "intent_id": normalized.get("intent_id")
            or (current or {}).get("intent_id"),
            "client_order_id": normalized["client_order_id"],
            "venue_order_id": normalized.get("venue_order_id")
            or (current or {}).get("venue_order_id"),
            "status": target,
            "side": _side(payload) or (current or {}).get("side"),
            "order_type": payload.get("order_type")
            or (current or {}).get("order_type"),
            "quantity": decimal_or_none(payload.get("quantity") or payload.get("qty"))
            or (current or {}).get("quantity"),
            "filled_quantity": new_filled,
            "price": decimal_or_none(payload.get("price"))
            or (current or {}).get("price"),
            "average_fill_price": decimal_or_none(
                payload.get("avg_px") or payload.get("average_fill_price")
            )
            or (current or {}).get("average_fill_price"),
            "lifecycle_role": payload.get("lifecycle_role")
            or (current or {}).get("lifecycle_role"),
            "updated_from_event_id": str(normalized["event_id"]),
            "ts_event": normalized["ts_event"],
            "payload": merged_payload,
        }
    ordered = [rows[key] for key in sorted(rows)]
    stats["accounts"] = sorted(account_watermarks)
    return ordered, {"stats": stats, "account_watermarks": account_watermarks}


def build_difference_report(
    rebuilt_rows: Iterable[dict[str, Any]],
    existing_rows: Iterable[dict[str, Any]],
    *,
    mode: str = "dry-run",
) -> dict[str, Any]:
    rebuilt = {
        (row["account_id"], row["client_order_id"]): row for row in rebuilt_rows
    }
    existing = {
        (row["account_id"], row["client_order_id"]): row for row in existing_rows
    }
    differences: list[dict[str, Any]] = []
    for key in sorted(set(rebuilt) | set(existing)):
        new = rebuilt.get(key)
        old = existing.get(key)
        account_id, client_order_id = key
        base = {"account_id": account_id, "client_order_id": client_order_id}
        if old is None:
            differences.append(
                {**base, "kind": "missing_in_projection", "rebuilt_status": new["status"]}
            )
            continue
        if new is None:
            differences.append(
                {**base, "kind": "missing_in_rebuild", "projection_status": old["status"]}
            )
            continue
        if str(new["status"]) != str(old["status"]):
            differences.append(
                {
                    **base,
                    "kind": "status_mismatch",
                    "projection_status": str(old["status"]),
                    "rebuilt_status": str(new["status"]),
                }
            )
        old_filled = decimal_or_none(old.get("filled_quantity")) or Decimal("0")
        new_filled = decimal_or_none(new.get("filled_quantity")) or Decimal("0")
        if old_filled != new_filled:
            differences.append(
                {
                    **base,
                    "kind": "filled_quantity_mismatch",
                    "projection_filled_quantity": str(old_filled),
                    "rebuilt_filled_quantity": str(new_filled),
                }
            )

    counts: dict[str, int] = {}
    for difference in differences:
        counts[difference["kind"]] = counts.get(difference["kind"], 0) + 1
    return {
        "mode": mode,
        "applied": False,
        "event_order": ["ts_event", "event_id"],
        "shadow_table": SHADOW_TABLE,
        "rebuilt_order_count": len(rebuilt),
        "projection_order_count": len(existing),
        "difference_count": len(differences),
        "difference_counts": counts,
        "differences": differences,
        "untouched_tables": ["order_events", "positions_projection"],
    }


def emit_report(report: dict[str, Any], stream: TextIO = sys.stdout) -> None:
    json.dump(report, stream, indent=2, sort_keys=True, default=str)
    stream.write("\n")


def rebuild(
    conn,
    *,
    apply: bool,
    accounts: frozenset[str] | None = None,
) -> dict[str, Any]:
    events, high_watermark = _load_order_events(conn)
    rebuilt_rows, fold_info = rebuild_order_rows(events, accounts=accounts)
    scope_accounts = (
        sorted(accounts) if accounts is not None else fold_info["stats"]["accounts"]
    )
    existing_rows = _load_existing_rows(conn, accounts=accounts)
    _build_shadow(conn, rebuilt_rows)
    conn.commit()

    mode = "apply" if apply else "dry-run"
    report = build_difference_report(rebuilt_rows, existing_rows, mode=mode)
    report["source_event_count"] = len(events)
    report["fold_stats"] = fold_info["stats"]
    report["account_scope"] = scope_accounts or "all"
    report["high_watermark"] = list(high_watermark) if high_watermark else False
    if not apply:
        return report

    _apply_shadow(
        conn,
        high_watermark,
        accounts=accounts,
        account_watermarks=fold_info["account_watermarks"],
    )
    report["applied"] = True
    return report


def _load_order_events(
    conn,
) -> tuple[list[dict[str, Any]], tuple[str, str] | None]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_id, event_type, node_id, account_id, intent_id,
                   client_order_id, venue_order_id, trade_id, ts_event, payload
            FROM execution_events
            WHERE event_type LIKE 'Order%'
            ORDER BY ts_event, event_id
            """
        )
        events = [
            {
                "event_id": row[0],
                "event_type": row[1],
                "node_id": row[2],
                "account_id": row[3],
                "intent_id": str(row[4]) if row[4] else None,
                "client_order_id": row[5],
                "venue_order_id": row[6],
                "trade_id": row[7],
                "ts_event": row[8],
                "payload": row[9] or {},
            }
            for row in cur.fetchall()
        ]
    high_watermark = None
    if events:
        last_event = events[-1]
        high_watermark = (
            ensure_aware(last_event["ts_event"]).isoformat(),
            str(last_event["event_id"]),
        )
    return events, high_watermark


def _load_existing_rows(
    conn,
    *,
    accounts: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    query = """
        SELECT account_id, client_order_id, status, filled_quantity,
               instrument_id, venue_order_id
        FROM orders_projection
    """
    params: tuple[Any, ...] = ()
    if accounts is not None:
        query += " WHERE account_id = ANY(%s)"
        params = (sorted(accounts),)
    query += " ORDER BY account_id, client_order_id"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return [
            {
                "account_id": row[0],
                "client_order_id": row[1],
                "status": row[2],
                "filled_quantity": row[3],
                "instrument_id": row[4],
                "venue_order_id": row[5],
            }
            for row in cur.fetchall()
        ]


def _build_shadow(conn, rows: Iterable[dict[str, Any]]) -> None:
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS pg_temp.{SHADOW_TABLE}")
        cur.execute(
            f"""
            CREATE TEMP TABLE {SHADOW_TABLE}
            (LIKE orders_projection INCLUDING ALL)
            ON COMMIT PRESERVE ROWS
            """
        )
        column_list = ", ".join(SHADOW_COLUMNS)
        placeholders = ", ".join(["%s"] * len(SHADOW_COLUMNS))
        for row in rows:
            cur.execute(
                f"""
                INSERT INTO {SHADOW_TABLE} ({column_list}, updated_at)
                VALUES ({placeholders}, now())
                """,
                tuple(
                    Json(row[column]) if column == "payload" else row[column]
                    for column in SHADOW_COLUMNS
                ),
            )


def _apply_shadow(
    conn,
    high_watermark: tuple[str, str] | None,
    *,
    accounts: frozenset[str] | None,
    account_watermarks: dict[str, tuple[str, datetime]],
) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("LOCK TABLE execution_events IN SHARE MODE")
            cur.execute(
                """
                SELECT ts_event, event_id
                FROM execution_events
                WHERE event_type LIKE 'Order%'
                ORDER BY ts_event DESC, event_id DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            current_watermark = None
            if row:
                current_watermark = (
                    ensure_aware(row[0]).isoformat(),
                    str(row[1]),
                )
            if current_watermark != high_watermark:
                raise RuntimeError(
                    "order events changed after shadow build; rerun the rebuild"
                )
            cur.execute("LOCK TABLE orders_projection IN ACCESS EXCLUSIVE MODE")
            if accounts is not None:
                cur.execute(
                    "DELETE FROM orders_projection WHERE account_id = ANY(%s)",
                    (sorted(accounts),),
                )
            else:
                cur.execute("DELETE FROM orders_projection")
            cur.execute(
                f"""
                INSERT INTO orders_projection
                SELECT * FROM {SHADOW_TABLE}
                """
            )
            for account_id in sorted(account_watermarks):
                event_id, ts_event = account_watermarks[account_id]
                cur.execute(
                    """
                    INSERT INTO projection_watermarks
                        (account_id, projector, last_event_id, last_event_ts, updated_at)
                    VALUES (%s, 'orders', %s, %s, now())
                    ON CONFLICT (account_id, projector) DO UPDATE SET
                        last_event_id=EXCLUDED.last_event_id,
                        last_event_ts=EXCLUDED.last_event_ts,
                        updated_at=now()
                    """,
                    (account_id, event_id, ts_event),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _event_sort_key(event: dict[str, Any]) -> tuple[datetime, str]:
    return (
        ensure_aware(event.get("ts_event")),
        str(event.get("event_id") or ""),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="atomically replace orders_projection after the dry-run checks",
    )
    parser.add_argument(
        "--account",
        action="append",
        default=None,
        help="restrict the rebuild to this account_id (repeatable); default: all",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL URL; defaults to DATABASE_URL",
    )
    args = parser.parse_args(argv)
    if not args.db_url:
        parser.error("DATABASE_URL is required through the environment or --db-url")
    accounts = (
        frozenset(canonical_account_id(value) for value in args.account)
        if args.account
        else None
    )

    conn = psycopg2.connect(args.db_url)
    try:
        report = rebuild(conn, apply=args.apply, accounts=accounts)
        emit_report(report)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
