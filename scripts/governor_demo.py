"""C-08/C-09 acceptance tool: drive a FRESH decision through the real Decision
Gateway + Risk Governor to prove the A->B seam and the kill-switch matrix.

It does NOT fake Hermes intelligence: a fresh decision is cloned from a REAL
open_position decision the Hermes worker already produced (real enums/evidence),
with only identity + timestamp + explicit geometry refreshed so the freshness gate
does not short-circuit. Runs against the live PostgreSQL via DATABASE_URL.

Subcommands:
  set-mode <INSTRUMENT> <ACTIVE|REDUCING|HALTED>
  seed     <INSTRUMENT> <long|short>      -> prints fresh decision_id
  run                                     -> process pending decisions, print outcomes
  chain                                   -> print full traceability for fresh intents
Run from services/control-plane with PYTHONPATH=.:db:risk:decision_gateway:risk_state
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from hashlib import sha256

import psycopg2
from psycopg2.extras import Json, RealDictCursor

import decision_gateway.gateway as gw  # noqa: E402
from policy import RiskPolicy  # noqa: E402
import risk_state as rs  # noqa: E402

REF = {  # explicit, known-good geometry per instrument (testnet reference prices)
    "BTCUSDT": dict(price=63000, sl=60000, tp=66000),
    "ETHUSDT": dict(price=1710, sl=1600, tp=1850),
    "SOLUSDT": dict(price=150, sl=140, tp=170),
}
# max_notional kept small so a governor-approved order fits the testnet balance
# (quantity = max_notional / entry_price at the A->B seam).
POLICY = RiskPolicy.from_dict({"default_account_id": "account-a", "max_notional": 200})


def _conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def set_mode(instrument, mode):
    conn = _conn()
    with conn, conn.cursor() as cur:
        rs.set_mode(cur, "account-a", instrument, mode)
    print(f"risk_state(account-a,{instrument}) = {mode}")


def seed(instrument, side):
    g = REF[instrument]
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "select message_type::text mt, action::text ac, ambiguous, account_scope::text sc, "
        "side::text sd, entry_type::text et, evidence, model_provider mp, model_version mv, "
        "prompt_version pv, schema_version sv, temperature, confidence "
        "from hermes_decisions where action='open_position' limit 1"
    )
    base = cur.fetchone()
    if base is None:
        raise SystemExit("no real open_position decision to clone")

    now = datetime.now(timezone.utc)
    raw_id, run_id, ctx_id, dec_id = (uuid4() for _ in range(4))
    cur.execute(
        "INSERT INTO raw_messages (id, source, channel_id, source_message_id, source_version, "
        "source_received_at, content_hash, message_text) VALUES (%s,'telegram','gov-demo',%s,'v1',%s,%s,%s)",
        (str(raw_id), f"gov-{raw_id}", now, sha256(str(raw_id).encode()).hexdigest(),
         f"[gov-demo] {instrument} {side} fresh acceptance vector"),
    )
    cur.execute(
        "INSERT INTO message_processing_runs (processing_run_id, raw_message_id, status) "
        "VALUES (%s,%s,'succeeded')", (str(run_id), str(raw_id)),
    )
    cur.execute(
        "INSERT INTO context_snapshots (context_snapshot_id, raw_message_id, snapshot_type, "
        "context_version, snapshot) VALUES (%s,%s,'system','v1',%s)",
        (str(ctx_id), str(raw_id), Json({})),
    )
    cur.execute(
        "INSERT INTO hermes_decisions (decision_id, raw_message_id, processing_run_id, context_snapshot_id, "
        "schema_version, message_type, action, ambiguous, account_scope, target_account_id, instrument_symbol, "
        "side, entry_type, entry_price, stop_loss, take_profits, leverage, valid_until, evidence, "
        "model_provider, model_version, prompt_version, context_version, temperature, confidence, created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,'open_position',false,'single','account-a',%s,%s,'market',%s,%s,%s,%s,%s,%s,"
        "%s,%s,'hermes-trader-v1','v1',%s,%s, now())",
        (str(dec_id), str(raw_id), str(run_id), str(ctx_id), base["sv"], base["mt"], instrument,
         side, g["price"], g["sl"], Json([g["tp"]]), 2, now + timedelta(hours=1),
         Json(base["evidence"] or {"raw_message_excerpt": "gov-demo"}),
         base["mp"], base["mv"], base["temperature"], base["confidence"]),
    )
    conn.commit()
    print(f"seeded FRESH decision {dec_id} -> {instrument} {side} open_position market "
          f"px={g['price']} sl={g['sl']} tp={g['tp']} acct=account-a")


def run():
    conn = _conn()
    seen_before = _intent_ids(conn)
    n = 0
    while True:
        out = gw.process_one_decision(conn, policy=POLICY)
        if out is None:
            break
        n += 1
        status = out.get("status") if isinstance(out, dict) else getattr(out, "status", out)
        reason = out.get("reason") if isinstance(out, dict) else getattr(out, "reason", None)
        print(f"  outcome: status={status} reason={reason}")
    print(f"processed {n} decision(s)")
    new_intents = _intent_ids(conn) - seen_before
    if new_intents:
        print("NEW approved trade_intents:", sorted(new_intents))


def _intent_ids(conn):
    with conn.cursor() as cur:
        cur.execute("select intent_id::text from trade_intents")
        return {r[0] for r in cur.fetchall()}


def chain():
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "select ti.intent_id, ti.account_id, ti.instrument_id, ti.action, ti.status, "
        "rd.risk_decision_id, rd.status::text rdstatus, hd.decision_id, hd.action::text hdaction, "
        "hd.raw_message_id from trade_intents ti "
        "join risk_decisions rd on rd.risk_decision_id=ti.risk_decision_id "
        "join hermes_decisions hd on hd.decision_id=ti.hermes_decision_id "
        "where ti.account_id='account-a' order by ti.approved_at desc limit 5"
    )
    print("=== recent approved-intent traceability (raw->decision->risk->intent) ===")
    for r in cur.fetchall():
        print(f"  raw={str(r['raw_message_id'])[:8]} -> dec={str(r['decision_id'])[:8]}({r['hdaction']}) "
              f"-> risk={str(r['risk_decision_id'])[:8]}({r['rdstatus']}) -> intent={str(r['intent_id'])[:8]} "
              f"{r['instrument_id']} {r['action']} [{r['status']}]")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "set-mode":
        set_mode(sys.argv[2], sys.argv[3])
    elif cmd == "seed":
        seed(sys.argv[2], sys.argv[3])
    elif cmd == "run":
        run()
    elif cmd == "chain":
        chain()
    else:
        raise SystemExit(__doc__)
