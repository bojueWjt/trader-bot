"""Re-post existing execution_events through the live /execution-events endpoint so the
new C-06 derivation populates the read-model projections (idempotent on event_id)."""
import json
import os
import urllib.request

import psycopg2

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
cur.execute(
    "select event_id, event_type, node_id, account_id, intent_id, client_order_id, "
    "venue_order_id, trade_id, ts_event, payload from execution_events order by ts_event"
)
events = []
for row in cur.fetchall():
    events.append({
        "event_id": row[0], "event_type": row[1], "node_id": row[2], "account_id": row[3],
        "intent_id": str(row[4]) if row[4] else None, "client_order_id": row[5],
        "venue_order_id": row[6], "trade_id": row[7],
        "ts_event": row[8].isoformat() if row[8] else None, "payload": row[9],
    })
body = json.dumps({"events": events}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8080/v1/nodes/nautilus-node-account-a/execution-events",
    data=body, method="POST",
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + os.environ["NAUTILUS_NODE_TOKEN"]},
)
print("reposted", len(events), "events ->", json.loads(urllib.request.urlopen(req, timeout=25).read()).get("status"))
