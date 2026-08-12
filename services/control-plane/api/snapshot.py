"""Real SystemSnapshotV1 built from PostgreSQL projections — no fixtures.

When there is no data the snapshot is a real empty state (zero balances, empty
arrays). Data-quality fields (projection lag, stale flag, missing nodes,
reconciliation state) are computed from the projections so stale/unavailable
conditions are visible and can block new risk upstream.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from psycopg2.extras import RealDictCursor

_REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = _REPO_ROOT / "packages" / "contracts" / "v1" / "system_snapshot.v1.json"

DEFAULT_STALENESS_MS = 60_000
RECENT_LIMIT = 50


def build_system_snapshot(
    conn,
    *,
    staleness_threshold_ms: int = DEFAULT_STALENESS_MS,
    now: datetime | None = None,
    limit: int = RECENT_LIMIT,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        accounts = _rows(cur, "SELECT * FROM accounts_projection ORDER BY account_id")
        orders = _rows(cur, "SELECT * FROM orders_projection ORDER BY updated_at DESC LIMIT %s", (limit,))
        positions = _rows(cur, "SELECT * FROM positions_projection ORDER BY updated_at DESC LIMIT %s", (limit,))
        # price_feed_status arrives with migration 0005; deployments still on 0004
        # (hk live as of 2026-07) must keep serving snapshots with an empty list.
        cur.execute("SELECT to_regclass('public.price_feed_status') IS NOT NULL AS present")
        if cur.fetchone()["present"]:
            market_prices = _rows(
                cur,
                "SELECT account_id, venue_symbol, source, mark_price, last_price, bid_price, ask_price, "
                "last_event_at, stale FROM price_feed_status ORDER BY account_id, venue_symbol, source",
            )
        else:
            market_prices = []
        # exchange_state_mirror arrives with migration 0007 (read-only truth of
        # positions/orders/algo orders polled from the venue); older deployments
        # keep serving snapshots without it.
        cur.execute("SELECT to_regclass('public.exchange_state_mirror') IS NOT NULL AS present")
        if cur.fetchone()["present"]:
            exchange_state = _rows(
                cur,
                "SELECT account_id, payload, updated_at, "
                "(now() - updated_at) > interval '180 seconds' AS stale "
                "FROM exchange_state_mirror ORDER BY account_id",
            )
        else:
            exchange_state = []
        recent_messages = _rows(
            cur,
            "SELECT id, channel_id, source_received_at, message_text FROM raw_messages "
            "ORDER BY source_received_at DESC LIMIT %s",
            (limit,),
        )
        hermes_decisions = _rows(
            cur,
            "SELECT decision_id, raw_message_id, message_type::text, action::text, created_at "
            "FROM hermes_decisions ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        risk_decisions = _rows(
            cur,
            "SELECT risk_decision_id, hermes_decision_id, status::text, account_id, instrument_id, decided_at "
            "FROM risk_decisions ORDER BY decided_at DESC LIMIT %s",
            (limit,),
        )
        node_health = _rows(cur, "SELECT * FROM node_heartbeats ORDER BY last_seen_at DESC")
        audit_trail = _rows(
            cur,
            "SELECT audit_event_id, event_type, aggregate_type, aggregate_id, created_at "
            "FROM audit_events ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        cur.execute("SELECT max(ts_event) AS last_event FROM execution_events")
        last_event = cur.fetchone()["last_event"]

    equity = sum(float(a.get("equity") or 0) for a in accounts)
    margin = sum(float(a.get("margin") or 0) for a in accounts)

    projection_lag_ms = 0
    if last_event is not None:
        projection_lag_ms = max(0, int((now - last_event).total_seconds() * 1000))

    reconciliation_state = _worst_reconciliation_state(accounts)
    missing_nodes = _missing_nodes(node_health, now, staleness_threshold_ms)
    stale = bool(
        (last_event is not None and projection_lag_ms > staleness_threshold_ms)
        or reconciliation_state == "failed"
        or missing_nodes
    )

    snapshot = {
        "schema_version": "1.0",
        "data_source": "postgres_projection",
        "snapshot_id": str(uuid4()),
        "generated_at": now.isoformat(),
        "last_execution_event_at": last_event.isoformat() if last_event is not None else None,
        "projection_lag_ms": projection_lag_ms,
        "stale": stale,
        "missing_nodes": missing_nodes,
        "reconciliation_state": reconciliation_state,
        "data": {
            "account": {},  # frozen schema keeps account as an empty placeholder object
            "balances": {"equity": equity, "margin": margin},
            "orders": _jsonify(orders),
            "positions": _jsonify(positions),
            "market_prices": _jsonify(market_prices),
            "exchange_state": _jsonify(exchange_state),
            "recent_messages": _jsonify(recent_messages),
            "hermes_decisions": _jsonify(hermes_decisions),
            "risk_decisions": _jsonify(risk_decisions),
            "node_health": _jsonify(node_health),
            "audit_trail": _jsonify(audit_trail),
        },
    }
    return snapshot


@lru_cache(maxsize=1)
def _snapshot_validator():
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    _snapshot_validator().validate(snapshot)


def _rows(cur, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _worst_reconciliation_state(accounts: list[dict[str, Any]]) -> str:
    order = {"healthy": 0, "degraded": 1, "failed": 2}
    worst = "healthy"
    for account in accounts:
        state = account.get("reconciliation_state") or "healthy"
        if order.get(state, 0) > order[worst]:
            worst = state
    return worst


def _missing_nodes(nodes: list[dict[str, Any]], now: datetime, threshold_ms: int) -> list[str]:
    missing = []
    for node in nodes:
        last_seen = node.get("last_seen_at")
        if last_seen is None:
            missing.append(node["node_id"])
            continue
        if (now - last_seen).total_seconds() * 1000 > threshold_ms:
            missing.append(node["node_id"])
    return sorted(missing)


def _jsonify(value: Any) -> Any:
    if isinstance(value, list):
        return [_jsonify(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "hex") and value.__class__.__name__ == "UUID":
        return str(value)
    return value
