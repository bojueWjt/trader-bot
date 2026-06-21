#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2


ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE = ROOT / "services" / "control-plane"
EXECUTION_DOMAIN = ROOT / "packages" / "execution-domain"

for path in (CONTROL_PLANE, EXECUTION_DOMAIN):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from order_management.order_reducer import OrderProjectionReducer  # noqa: E402


BASE_TS = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "acct-om2-live-replay"
CLIENT_ORDER_ID = "coid-live-replay"


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "postgresql:///om_v3_om2")
    conn = psycopg2.connect(database_url)
    try:
        cleanup(conn)
        reducer = OrderProjectionReducer()
        for event in (
            order_event("evt-live-accept", "OrderAccepted", 2),
            order_event("evt-live-submit", "OrderSubmitted", 1),
            order_event("evt-live-accept", "OrderAccepted", 2),
            order_event("evt-live-late-submit", "OrderSubmitted", 3),
        ):
            reducer.apply_event(conn, event)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, updated_from_event_id
                FROM orders_projection
                WHERE account_id=%s AND client_order_id=%s
                """,
                (ACCOUNT_ID, CLIENT_ORDER_ID),
            )
            status, updated_from_event_id = cur.fetchone()
            cur.execute(
                """
                SELECT finding_type, payload->>'from_status', payload->>'to_status'
                FROM reconciliation_findings
                WHERE account_id=%s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (ACCOUNT_ID,),
            )
            finding = cur.fetchone()
        print(
            json.dumps(
                {
                    "status": status,
                    "updated_from_event_id": updated_from_event_id,
                    "finding": {
                        "type": finding[0],
                        "from_status": finding[1],
                        "to_status": finding[2],
                    }
                    if finding
                    else None,
                },
                sort_keys=True,
            )
        )
    finally:
        cleanup(conn)
        conn.close()
    return 0


def cleanup(conn) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM reconciliation_findings WHERE account_id=%s",
                (ACCOUNT_ID,),
            )
            cur.execute(
                "DELETE FROM reconciliation_runs WHERE account_id=%s",
                (ACCOUNT_ID,),
            )
            cur.execute(
                "DELETE FROM order_events WHERE account_id=%s",
                (ACCOUNT_ID,),
            )
            cur.execute(
                "DELETE FROM orders_projection WHERE account_id=%s",
                (ACCOUNT_ID,),
            )
            cur.execute(
                "DELETE FROM execution_events WHERE account_id=%s",
                (ACCOUNT_ID,),
            )


def order_event(event_id: str, event_type: str, seconds: int) -> dict:
    return {
        "event_id": event_id,
        "schema_version": "1.0",
        "node_id": "node-1",
        "account_id": ACCOUNT_ID,
        "client_order_id": CLIENT_ORDER_ID,
        "venue_order_id": "venue-live-replay",
        "event_type": event_type,
        "ts_event": BASE_TS + timedelta(seconds=seconds),
        "payload": {
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "side": "long",
            "order_type": "limit",
            "quantity": "1",
            "filled_qty": "0",
            "leaves_qty": "1",
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
