"""C-08 gateway/governor scenario matrix (deterministic, no-order).

Seeds labeled FRESH decisions (cloning a real decision's enums) exercising each
governor gate, runs the real Decision Gateway, and asserts the outcome against the
expectation. Reject/review scenarios place no orders. Run from services/control-plane
with PYTHONPATH=.:db:risk:decision_gateway:risk_state and DATABASE_URL set.
Precondition: risk_state(account-a,BTCUSDT)=ACTIVE (gates past risk_context).
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from hashlib import sha256

import psycopg2
from psycopg2.extras import Json, RealDictCursor

import decision_gateway.gateway as gw
from policy import RiskPolicy

POLICY = RiskPolicy.from_dict({"default_account_id": "account-a", "max_notional": 200})

# label, overrides, expected (status, reason substring)
SCENARIOS = [
    ("whitelist_reject",
     dict(message_type="new_signal", action="open_position", instrument="DOGEUSDT",
          entry_price=0.12, stop_loss=0.10, take_profits=[0.15]),
     ("rejected", "not allowed")),
    ("precision_reject",
     dict(message_type="new_signal", action="open_position", instrument="BTCUSDT",
          entry_price="63000.123", stop_loss=60000, take_profits=[66000]),
     ("rejected", "precision")),
    ("ambiguous_review",
     dict(message_type="new_signal", action="open_position", instrument="BTCUSDT",
          ambiguous=True, entry_price=63000, stop_loss=60000, take_profits=[66000]),
     ("needs_review", "ambiguous")),
    ("update_message_cannot_open",
     dict(message_type="position_update", action="open_position", instrument="BTCUSDT",
          entry_price=63000, stop_loss=60000, take_profits=[66000]),
     ("rejected", "produced open_position")),  # update_message_cannot_open gate
    ("update_no_target",
     dict(message_type="close_update", action="close_position", instrument="BTCUSDT",
          entry_price=63000, stop_loss=60000, take_profits=[66000]),
     ("needs_review", "target_position_id")),
    ("leverage_reject",
     dict(message_type="new_signal", action="open_position", instrument="BTCUSDT",
          entry_price=63000, stop_loss=60000, take_profits=[66000], leverage=20),
     ("rejected", "leverage")),
]


def _conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _clone_enums(cur):
    cur.execute(
        "select side::text sd, entry_type::text et, evidence, model_provider mp, "
        "model_version mv, schema_version sv, temperature, confidence "
        "from hermes_decisions where action='open_position' limit 1"
    )
    return cur.fetchone()


def seed(cur, base, ov):
    now = datetime.now(timezone.utc)
    raw_id, run_id, ctx_id, dec_id = (uuid4() for _ in range(4))
    cur.execute(
        "INSERT INTO raw_messages (id, source, channel_id, source_message_id, source_version, "
        "source_received_at, content_hash, message_text) VALUES (%s,'telegram','c08',%s,'v1',%s,%s,%s)",
        (str(raw_id), f"c08-{raw_id}", now, sha256(str(raw_id).encode()).hexdigest(), "[c08] scenario"),
    )
    cur.execute("INSERT INTO message_processing_runs (processing_run_id, raw_message_id, status) "
                "VALUES (%s,%s,'succeeded')", (str(run_id), str(raw_id)))
    cur.execute("INSERT INTO context_snapshots (context_snapshot_id, raw_message_id, snapshot_type, "
                "context_version, snapshot) VALUES (%s,%s,'system','v1',%s)", (str(ctx_id), str(raw_id), Json({})))
    cur.execute(
        "INSERT INTO hermes_decisions (decision_id, raw_message_id, processing_run_id, context_snapshot_id, "
        "schema_version, message_type, action, ambiguous, account_scope, target_account_id, instrument_symbol, "
        "side, entry_type, entry_price, stop_loss, take_profits, leverage, valid_until, evidence, "
        "model_provider, model_version, prompt_version, context_version, temperature, confidence, created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'single','account-a',%s,%s,'market',%s,%s,%s,%s,%s,%s,%s,%s,"
        "'hermes-trader-v1','v1',%s,%s, now())",
        (str(dec_id), str(raw_id), str(run_id), str(ctx_id), base["sv"], ov["message_type"], ov["action"],
         ov.get("ambiguous", False), ov["instrument"], ov.get("side", base["sd"]), ov.get("entry_price"),
         ov.get("stop_loss"), Json(ov.get("take_profits") or []), ov.get("leverage", 2),
         now + timedelta(hours=1), Json(base["evidence"] or {"x": "c08"}),
         base["mp"], base["mv"], base["temperature"], base["confidence"]),
    )
    return str(dec_id)


def main():
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    base = _clone_enums(cur)
    ids = {}
    for label, ov, _ in SCENARIOS:
        ids[seed(cur, base, ov)] = label
    conn.commit()

    outcomes = {}
    while True:
        out = gw.process_one_decision(conn, policy=POLICY)
        if out is None:
            break
        # process_one returns the written outcome; map by re-reading risk_decisions
    # read outcomes for our seeded decisions
    cur.execute(
        "select hd.decision_id::text, rd.status::text, rd.reason from hermes_decisions hd "
        "join risk_decisions rd on rd.hermes_decision_id=hd.decision_id where hd.decision_id::text = ANY(%s)",
        (list(ids.keys()),),
    )
    for r in cur.fetchall():
        outcomes[ids[r["decision_id"]]] = (r["status"], r["reason"])

    print("=== C-08 gateway/governor scenario matrix ===")
    npass = 0
    for label, _, (exp_status, exp_sub) in SCENARIOS:
        got = outcomes.get(label)
        ok = got and got[0] == exp_status and (exp_sub in (got[1] or ""))
        npass += bool(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:28s} expect={exp_status}/{exp_sub!r:30s} got={got}")
    print(f"=== {npass}/{len(SCENARIOS)} scenarios passed ===")


if __name__ == "__main__":
    main()
