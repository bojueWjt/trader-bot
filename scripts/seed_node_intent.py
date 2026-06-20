"""Seed one valid approved ApprovedTradeIntentV1 for account-a so the live node
pulls + executes it on Binance testnet. Creates the full FK chain."""
import hashlib
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
now = datetime.now(timezone.utc)
raw_id, run_id, ctx_id, dec_id, risk_id, intent_id = (uuid4() for _ in range(6))
idem = hashlib.sha256(str(intent_id).encode()).hexdigest()

cur.execute(
    "INSERT INTO raw_messages (id, source, channel_id, source_message_id, source_version, source_received_at, content_hash) "
    "VALUES (%s,'telegram','seed',%s,'v1',%s,%s)",
    (str(raw_id), f"seed-{raw_id}", now, hashlib.sha256(str(raw_id).encode()).hexdigest()),
)
cur.execute(
    "INSERT INTO message_processing_runs (processing_run_id, raw_message_id, status) VALUES (%s,%s,'succeeded')",
    (str(run_id), str(raw_id)),
)
cur.execute(
    "INSERT INTO context_snapshots (context_snapshot_id, raw_message_id, snapshot_type, context_version, snapshot) "
    "VALUES (%s,%s,'system','v1',%s)",
    (str(ctx_id), str(raw_id), Json({})),
)
cur.execute(
    "INSERT INTO hermes_decisions (decision_id, raw_message_id, processing_run_id, context_snapshot_id, message_type, "
    "action, ambiguous, account_scope, entry_type, model_version, prompt_version, context_version, temperature, created_at) "
    "VALUES (%s,%s,%s,%s,'new_signal','open_position',false,'single','market','gemini-3.1-pro','hermes-trader-v1','v1',0, now())",
    (str(dec_id), str(raw_id), str(run_id), str(ctx_id)),
)
cur.execute(
    "INSERT INTO risk_decisions (risk_decision_id, hermes_decision_id, status, account_id, decided_by) "
    "VALUES (%s,%s,'approved','account-a','seed')",
    (str(risk_id), str(dec_id)),
)
order_plan = {  # B planner format: side buy/sell, top-level type + quantity
    "side": "buy", "type": "market", "quantity": "0.002", "time_in_force": "IOC",
}
risk_budget = {"risk_fraction": 0.01, "max_notional": 1000, "max_leverage": 5}
cur.execute(
    "INSERT INTO trade_intents (intent_id, hermes_decision_id, risk_decision_id, schema_version, account_id, "
    "instrument_id, action, status, order_plan, risk_budget, valid_until, idempotency_key, approved_at) "
    "VALUES (%s,%s,%s,'1.0','account-a','BTCUSDT-PERP.BINANCE','open_position','approved',%s,%s,%s,%s, now())",
    (str(intent_id), str(dec_id), str(risk_id), Json(order_plan), Json(risk_budget),
     now + timedelta(days=1), idem),
)
conn.commit()
print(f"seeded approved intent {intent_id} -> account-a BTCUSDT-PERP.BINANCE open_position market notional=20")
