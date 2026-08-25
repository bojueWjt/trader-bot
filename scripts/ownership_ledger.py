#!/usr/bin/env python3
"""Audit and record robot-owned position rebaseline markers."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json

REPO_ROOT = Path(__file__).resolve().parents[1]
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
if str(EXECUTION_DOMAIN_ROOT) not in sys.path:
    sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from execution_domain.order_ownership import row_is_robot_order
from execution_domain.ownership_ledger import (
    OWNERSHIP_REBASELINE_EVENT_TYPE,
    OwnershipBalance,
    OwnershipLedgerError,
    build_rebaseline_payload,
    canonical_symbol,
    load_latest_ownership_baselines,
    load_robot_owned_balance,
    load_robot_owned_balances,
    signed_fill_quantity,
)

MANUAL_CLIENT_ORDER_ID_PATTERN = re.compile(r"^(?:aos_|stToAg_)")
ACCOUNT_IDS = (
    "account-a",
    "account-b",
    "account-c",
    "account-d",
)
MAX_HEARTBEAT_AGE_SECONDS = Decimal(5)
QUANTITY_TOLERANCE = Decimal("0.00000001")


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OwnershipLedgerError(f"{label} is invalid") from exc
    if not result.is_finite():
        raise OwnershipLedgerError(f"{label} is invalid")
    return result


def _database_url(argument: str | None) -> str:
    value = str(argument or os.environ.get("DATABASE_URL") or "").strip()
    if not value:
        raise SystemExit("--database-url or DATABASE_URL is required")
    return value


def _event_symbol(
    payload: Mapping[str, Any],
    projection_instrument: Any,
) -> str:
    for field_name in ("instrument_id", "symbol", "instrument"):
        symbol = canonical_symbol(payload.get(field_name))
        if symbol:
            return symbol
    return canonical_symbol(projection_instrument)


def _exchange_evidence(cur) -> dict[str, dict[str, Any]]:
    cur.execute(
        """
        SELECT account_id,
               node_id,
               status,
               extract(epoch from clock_timestamp() - last_seen_at),
               positions,
               regular_orders,
               algo_orders,
               positions_snapshot_at
        FROM node_heartbeats
        WHERE account_id = ANY(%s)
        ORDER BY account_id, node_id
        """,
        (list(ACCOUNT_IDS),),
    )
    evidence: dict[str, dict[str, Any]] = {}
    for row in cur.fetchall():
        (
            account_id,
            node_id,
            status,
            heartbeat_age,
            positions,
            regular_orders,
            algo_orders,
            positions_snapshot_at,
        ) = row
        account = str(account_id or "")
        if account in evidence:
            raise OwnershipLedgerError(
                f"{account} has multiple heartbeat reporters"
            )
        evidence[account] = {
            "node_id": str(node_id or ""),
            "status": str(status or ""),
            "heartbeat_age_seconds": _decimal(
                heartbeat_age,
                "heartbeat age",
            ),
            "positions": positions,
            "regular_orders": regular_orders,
            "algo_orders": algo_orders,
            "positions_snapshot_at": positions_snapshot_at,
        }
    return evidence


def _position_quantities(
    positions: Any,
) -> dict[str, Decimal]:
    if not isinstance(positions, list):
        raise OwnershipLedgerError(
            "exchange position evidence is invalid"
        )
    quantities: dict[str, Decimal] = {}
    for row in positions:
        if not isinstance(row, Mapping):
            raise OwnershipLedgerError(
                "exchange position evidence is invalid"
            )
        symbol = canonical_symbol(
            row.get("symbol")
            or row.get("instrument_id")
        )
        if not symbol:
            raise OwnershipLedgerError(
                "exchange position evidence is invalid"
            )
        raw_quantity = row.get("quantity")
        if raw_quantity is None:
            raw_quantity = row.get("position_amt")
        if raw_quantity is None:
            raw_quantity = row.get("positionAmt")
        quantity = _decimal(
            raw_quantity,
            "exchange position quantity",
        )
        quantities[symbol] = quantities.get(
            symbol,
            Decimal(0),
        ) + quantity
    return quantities


def _manual_fill_balances(
    cur,
    *,
    account_id: str,
    baselines: Mapping[str, Any],
) -> tuple[dict[str, Decimal], dict[str, int]]:
    cur.execute(
        """
        SELECT ee.client_order_id,
               ee.event_type,
               ee.ts_event,
               ee.created_at,
               ee.event_id,
               ee.payload,
               op.instrument_id
        FROM execution_events AS ee
        LEFT JOIN orders_projection AS op
          ON op.account_id=ee.account_id
         AND op.client_order_id=ee.client_order_id
        WHERE ee.account_id=%s
          AND ee.event_type IN ('OrderFilled', 'OrderPartiallyFilled')
          AND ee.client_order_id ~ '^(aos_|stToAg_)'
        ORDER BY ee.ts_event, ee.created_at, ee.event_id
        """,
        (account_id,),
    )
    quantities: dict[str, Decimal] = {
        symbol: baseline.manual_quantity
        for symbol, baseline in baselines.items()
        if baseline.manual_quantity is not None
    }
    counts: dict[str, int] = {}
    for row in cur.fetchall():
        (
            client_order_id,
            event_type,
            ts_event,
            created_at,
            event_id,
            raw_payload,
            projection_instrument,
        ) = row
        if not MANUAL_CLIENT_ORDER_ID_PATTERN.match(
            str(client_order_id or "")
        ):
            continue
        if not isinstance(raw_payload, Mapping):
            continue
        symbol = _event_symbol(raw_payload, projection_instrument)
        if not symbol:
            continue
        baseline = baselines.get(symbol)
        if baseline is not None and baseline.manual_quantity is not None:
            event_cutoff = (ts_event, created_at, str(event_id))
            if event_cutoff <= baseline.cutoff:
                continue
        try:
            signed_quantity = signed_fill_quantity(
                raw_payload,
                event_type=str(event_type or ""),
            )
        except OwnershipLedgerError:
            continue
        quantities[symbol] = quantities.get(
            symbol,
            Decimal(0),
        ) + signed_quantity
        counts[symbol] = counts.get(symbol, 0) + 1
    return quantities, counts


def _legacy_unknown_fill_counts(
    cur,
    *,
    account_id: str,
) -> dict[str, int]:
    cur.execute(
        """
        SELECT ee.client_order_id,
               ee.payload,
               op.instrument_id
        FROM execution_events AS ee
        LEFT JOIN orders_projection AS op
          ON op.account_id=ee.account_id
         AND op.client_order_id=ee.client_order_id
        WHERE ee.account_id=%s
          AND ee.event_type IN ('OrderFilled', 'OrderPartiallyFilled')
          AND coalesce(ee.client_order_id, '') !~
              '^B[0-9a-f]{32}[0-9]{2}$'
          AND coalesce(ee.client_order_id, '') !~ '^(aos_|stToAg_)'
        """,
        (account_id,),
    )
    counts: dict[str, int] = {}
    for _client_order_id, raw_payload, projection_instrument in cur.fetchall():
        if not isinstance(raw_payload, Mapping):
            continue
        symbol = _event_symbol(raw_payload, projection_instrument)
        if not symbol:
            continue
        counts[symbol] = counts.get(symbol, 0) + 1
    return counts


def build_audit(cur) -> dict[str, Any]:
    heartbeat_evidence = _exchange_evidence(cur)
    rows: list[dict[str, Any]] = []
    account_summaries: list[dict[str, Any]] = []
    for account_id in ACCOUNT_IDS:
        evidence = heartbeat_evidence.get(account_id)
        if evidence is None:
            account_summaries.append(
                {
                    "account_id": account_id,
                    "status": "heartbeat_missing",
                    "symbol_count": 0,
                    "mismatch_count": 0,
                }
            )
            continue
        exchange = _position_quantities(evidence["positions"])
        raw_balances = load_robot_owned_balances(
            cur,
            account_id=account_id,
            apply_rebaseline=False,
        )
        rebased_balances = load_robot_owned_balances(
            cur,
            account_id=account_id,
            apply_rebaseline=True,
        )
        baselines = load_latest_ownership_baselines(
            cur,
            account_id=account_id,
        )
        manual_fills, manual_fill_counts = _manual_fill_balances(
            cur,
            account_id=account_id,
            baselines=baselines,
        )
        legacy_counts = _legacy_unknown_fill_counts(
            cur,
            account_id=account_id,
        )
        symbols = set(exchange)
        symbols.update(raw_balances)
        symbols.update(rebased_balances)
        symbols.update(manual_fills)
        symbols.update(baselines)
        mismatch_count = 0
        for symbol in sorted(symbols):
            raw_balance = raw_balances.get(symbol)
            rebased_balance = rebased_balances.get(symbol)
            raw_quantity = Decimal(0)
            rebased_quantity = Decimal(0)
            if raw_balance is not None:
                raw_quantity = raw_balance.quantity
            if rebased_balance is not None:
                rebased_quantity = rebased_balance.quantity
            exchange_quantity = exchange.get(symbol, Decimal(0))
            manual_quantity = manual_fills.get(
                symbol,
                Decimal(0),
            )
            manual_source = "explicit_manual_fill_prefix"
            baseline = baselines.get(symbol)
            if baseline is not None and baseline.manual_quantity is not None:
                manual_source = (
                    "ownership_rebaseline_plus_post_marker_manual_fills"
                )
            expected_robot_quantity = (
                exchange_quantity - manual_quantity
            )
            difference = (
                rebased_quantity - expected_robot_quantity
            )
            status = "match"
            if abs(difference) > QUANTITY_TOLERANCE:
                status = "mismatch"
                mismatch_count += 1
            rows.append(
                {
                    "account_id": account_id,
                    "symbol": symbol,
                    "raw_robot_quantity": format(raw_quantity, "f"),
                    "rebased_robot_quantity": format(
                        rebased_quantity,
                        "f",
                    ),
                    "exchange_quantity": format(
                        exchange_quantity,
                        "f",
                    ),
                    "manual_attributed_quantity": format(
                        manual_quantity,
                        "f",
                    ),
                    "manual_attribution_source": manual_source,
                    "expected_robot_quantity": format(
                        expected_robot_quantity,
                        "f",
                    ),
                    "difference": format(difference, "f"),
                    "explicit_manual_fill_events": manual_fill_counts.get(
                        symbol,
                        0,
                    ),
                    "legacy_or_unknown_fill_events": legacy_counts.get(
                        symbol,
                        0,
                    ),
                    "baseline_event_id": (
                        baseline.event_id
                        if baseline is not None
                        else ""
                    ),
                    "status": status,
                }
            )
        account_summaries.append(
            {
                "account_id": account_id,
                "status": evidence["status"],
                "node_id": evidence["node_id"],
                "heartbeat_age_seconds": format(
                    evidence["heartbeat_age_seconds"],
                    "f",
                ),
                "positions_snapshot_at": _iso(
                    evidence["positions_snapshot_at"]
                ),
                "symbol_count": len(symbols),
                "mismatch_count": mismatch_count,
            }
        )
    return {
        "schema_version": "ownership-audit/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "accounts": account_summaries,
        "rows": rows,
    }


def record_rebaseline(
    conn,
    *,
    account_id: str,
    symbol: str,
    baseline_quantity: Decimal,
    reason: str,
    adjudicated_by: str,
    request_id: str,
    expected_exchange_quantity: Decimal,
    manual_quantity: Decimal,
) -> dict[str, Any]:
    target_symbol = canonical_symbol(symbol)
    if account_id not in ACCOUNT_IDS:
        raise OwnershipLedgerError("ownership account is invalid")
    if not target_symbol:
        raise OwnershipLedgerError("ownership symbol is required")
    event_id = f"ownership-rebaseline:{request_id}"
    cur = conn.cursor()
    try:
        cur.execute(
            "LOCK TABLE execution_events IN SHARE ROW EXCLUSIVE MODE"
        )
        cur.execute(
            """
                SELECT event_id, payload
                FROM execution_events
                WHERE event_id=%s
                """,
            (event_id,),
        )
        existing = cur.fetchone()
        if existing is not None:
            balance = load_robot_owned_balance(
                cur,
                account_id=account_id,
                symbol=target_symbol,
            )
            conn.commit()
            return {
                "status": "existing",
                "event_id": str(existing[0]),
                "balance": _balance_json(balance),
            }
        evidence = _exchange_evidence(cur).get(account_id)
        if evidence is None:
            raise OwnershipLedgerError(
                "account heartbeat evidence is unavailable"
            )
        if evidence["status"] != "ACTIVE":
            raise OwnershipLedgerError(
                "account heartbeat is not ACTIVE"
            )
        if (
            evidence["heartbeat_age_seconds"]
            > MAX_HEARTBEAT_AGE_SECONDS
        ):
            raise OwnershipLedgerError(
                "account heartbeat evidence is stale"
            )
        _validate_no_robot_orders(
            evidence,
            target_symbol=target_symbol,
        )
        exchange_quantities = _position_quantities(
            evidence["positions"]
        )
        exchange_quantity = exchange_quantities.get(
            target_symbol,
            Decimal(0),
        )
        if exchange_quantity != expected_exchange_quantity:
            raise OwnershipLedgerError(
                "exchange quantity differs from reviewed evidence"
            )
        previous = load_robot_owned_balance(
            cur,
            account_id=account_id,
            symbol=target_symbol,
        )
        raw = load_robot_owned_balance(
            cur,
            account_id=account_id,
            symbol=target_symbol,
            apply_rebaseline=False,
        )
        payload = build_rebaseline_payload(
            symbol=target_symbol,
            baseline_quantity=baseline_quantity,
            reason=reason,
            adjudicated_by=adjudicated_by,
            request_id=request_id,
            exchange_quantity=exchange_quantity,
            manual_quantity=manual_quantity,
            previous_robot_owned_quantity=previous.quantity,
            raw_robot_fill_quantity=raw.quantity,
        )
        marker_time = _database_time(cur)
        execution_event_row_id = str(uuid4())
        cur.execute(
            """
                INSERT INTO execution_events (
                    execution_event_row_id,
                    event_id,
                    schema_version,
                    node_id,
                    account_id,
                    event_type,
                    ts_event,
                    ts_ingest,
                    payload
                )
                VALUES (
                    %s, %s, '1.0', 'operator-ownership-ledger',
                    %s, %s, %s, %s, %s
                )
                """,
            (
                execution_event_row_id,
                event_id,
                account_id,
                OWNERSHIP_REBASELINE_EVENT_TYPE,
                marker_time,
                marker_time,
                Json(payload),
            ),
        )
        cur.execute(
            """
                INSERT INTO audit_events (
                    audit_event_id,
                    event_type,
                    aggregate_type,
                    aggregate_id,
                    actor,
                    action,
                    target,
                    before_state,
                    after_state,
                    reason,
                    payload
                )
                VALUES (
                    %s, %s, 'ownership_ledger', %s, %s,
                    'rebaseline', %s, %s, %s, %s, %s
                )
                """,
            (
                str(uuid4()),
                OWNERSHIP_REBASELINE_EVENT_TYPE,
                f"{account_id}:{target_symbol}",
                adjudicated_by,
                f"{account_id}:{target_symbol}",
                Json(
                    {
                        "robot_owned_quantity": format(
                            previous.quantity,
                            "f",
                        )
                    }
                ),
                Json(
                    {
                        "robot_owned_quantity": format(
                            baseline_quantity,
                            "f",
                        )
                    }
                ),
                reason,
                Json(
                    {
                        "request_id": request_id,
                        "execution_event_id": event_id,
                        "exchange_quantity": format(
                            exchange_quantity,
                            "f",
                        ),
                        "manual_quantity": format(
                            manual_quantity,
                            "f",
                        ),
                    }
                ),
            ),
        )
        balance = load_robot_owned_balance(
            cur,
            account_id=account_id,
            symbol=target_symbol,
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        cur.close()
    return {
        "status": "inserted",
        "event_id": event_id,
        "balance": _balance_json(balance),
    }


def _validate_no_robot_orders(
    evidence: Mapping[str, Any],
    *,
    target_symbol: str,
) -> None:
    for field_name in ("regular_orders", "algo_orders"):
        rows = evidence.get(field_name)
        if not isinstance(rows, list):
            raise OwnershipLedgerError(
                "exchange order evidence is invalid"
            )
        for row in rows:
            if not isinstance(row, Mapping):
                raise OwnershipLedgerError(
                    "exchange order evidence is invalid"
                )
            symbol = canonical_symbol(
                row.get("symbol")
                or row.get("instrument_id")
            )
            if symbol != target_symbol:
                continue
            if row_is_robot_order(row):
                raise OwnershipLedgerError(
                    "target symbol has active robot orders"
                )


def _database_time(cur) -> datetime:
    cur.execute("SELECT clock_timestamp()")
    row = cur.fetchone()
    if row is None or not isinstance(row[0], datetime):
        raise OwnershipLedgerError("database clock is unavailable")
    return row[0]


def _balance_json(balance: OwnershipBalance) -> dict[str, Any]:
    baseline_event_id = ""
    if balance.baseline is not None:
        baseline_event_id = balance.baseline.event_id
    return {
        "account_id": balance.account_id,
        "symbol": balance.symbol,
        "baseline_quantity": format(
            balance.baseline_quantity,
            "f",
        ),
        "post_baseline_fill_quantity": format(
            balance.post_baseline_fill_quantity,
            "f",
        ),
        "robot_owned_quantity": format(balance.quantity, "f"),
        "fill_event_count": balance.fill_event_count,
        "baseline_event_id": baseline_event_id,
    }


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL; defaults to DATABASE_URL",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser("audit")
    audit.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print JSON output",
    )

    show = subparsers.add_parser("show")
    show.add_argument("--account-id", required=True)
    show.add_argument("--symbol", required=True)

    rebaseline = subparsers.add_parser("rebaseline")
    rebaseline.add_argument("--account-id", required=True)
    rebaseline.add_argument("--symbol", required=True)
    rebaseline.add_argument("--baseline-quantity", required=True)
    rebaseline.add_argument("--reason", required=True)
    rebaseline.add_argument("--adjudicated-by", required=True)
    rebaseline.add_argument("--request-id", required=True)
    rebaseline.add_argument(
        "--expected-exchange-quantity",
        required=True,
    )
    rebaseline.add_argument("--manual-quantity", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = _database_url(args.database_url)
    try:
        with psycopg2.connect(database_url) as conn:
            if args.command == "audit":
                with conn.cursor() as cur:
                    result = build_audit(cur)
                indent = None
                if args.pretty:
                    indent = 2
                print(json.dumps(result, indent=indent, sort_keys=True))
                return 0
            if args.command == "show":
                with conn.cursor() as cur:
                    balance = load_robot_owned_balance(
                        cur,
                        account_id=args.account_id,
                        symbol=args.symbol,
                    )
                print(
                    json.dumps(
                        _balance_json(balance),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            result = record_rebaseline(
                conn,
                account_id=args.account_id,
                symbol=args.symbol,
                baseline_quantity=_decimal(
                    args.baseline_quantity,
                    "baseline quantity",
                ),
                reason=args.reason,
                adjudicated_by=args.adjudicated_by,
                request_id=args.request_id,
                expected_exchange_quantity=_decimal(
                    args.expected_exchange_quantity,
                    "expected exchange quantity",
                ),
                manual_quantity=_decimal(
                    args.manual_quantity,
                    "manual quantity",
                ),
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    except (OwnershipLedgerError, psycopg2.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
