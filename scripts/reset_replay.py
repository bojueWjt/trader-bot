"""Re-queue raw-message outbox for a clean replay re-run (FK-safe).

Deletes only processing runs that produced NO decision (the failed/stale runs);
keeps runs referenced by hermes_decisions. Then sets raw_message outbox back to
pending so the worker re-claims them.
"""
import os

import psycopg2

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
cur.execute(
    "DELETE FROM message_processing_runs WHERE processing_run_id NOT IN "
    "(SELECT processing_run_id FROM hermes_decisions WHERE processing_run_id IS NOT NULL)"
)
runs = cur.rowcount
cur.execute("UPDATE outbox_events SET status='pending' WHERE aggregate_type='raw_message'")
ob = cur.rowcount
conn.commit()
print(f"reset: deleted {runs} decision-less runs, re-queued {ob} raw_message outbox events")
