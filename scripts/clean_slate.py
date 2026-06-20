"""Truncate transactional tables for a clean replay run (keeps schema + risk_state)."""
import os

import psycopg2

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
cur.execute(
    "TRUNCATE raw_messages, media_assets, message_processing_runs, context_snapshots, "
    "hermes_decisions, risk_decisions, trade_intents, execution_events, execution_commands, "
    "outbox_events, positions_projection, orders_projection, accounts_projection "
    "RESTART IDENTITY CASCADE"
)
conn.commit()
print("clean slate: transactional tables truncated")
