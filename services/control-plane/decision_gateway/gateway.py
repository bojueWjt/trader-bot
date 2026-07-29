"""Decision Gateway.

Consumes a persisted HermesDecisionV1 (never natural language), re-validates it
against contracts-v1, runs the deterministic risk governor, and writes a
risk_decision. On approval it writes an ApprovedTradeIntentV1 + outbox event in the
same transaction. Idempotent: a decision is processed once; approved intents carry a
deterministic idempotency key.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from psycopg2.extras import Json, RealDictCursor
from psycopg2.extensions import connection as PsycopgConnection

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (
    str(_REPO_ROOT / "services" / "control-plane"),
    str(_REPO_ROOT / "services" / "control-plane" / "db"),
    str(_REPO_ROOT / "services" / "control-plane" / "risk"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import governor  # noqa: E402
from connection import transaction  # noqa: E402
from order_management.execution_jobs import create_execution_job_for_approved_intent  # noqa: E402
from policy import RiskPolicy  # noqa: E402

SCHEMA_PATH = _REPO_ROOT / "packages" / "contracts" / "v1" / "hermes_decision.v1.json"


class GatewayError(RuntimeError):
    pass


def _validator():
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def process_one_decision(
    conn: PsycopgConnection, *, policy: RiskPolicy | None = None
) -> dict[str, Any] | None:
    """Process the oldest hermes_decision that has no risk_decision yet."""
    policy = policy or RiskPolicy()

    with transaction(conn):
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT hd.*
                FROM hermes_decisions hd
                LEFT JOIN risk_decisions rd ON rd.hermes_decision_id = hd.decision_id
                WHERE rd.risk_decision_id IS NULL
                ORDER BY hd.created_at ASC, hd.decision_id ASC
                LIMIT 1
                FOR UPDATE OF hd SKIP LOCKED
                """
            )
            row = cur.fetchone()
            if row is None:
                return None

            decision = _row_to_decision(row)

            # non-Hermes provenance is rejected outright
            if row["model_provider"] != "hermes":
                return _write_outcome(
                    cur, row, governor.RiskDecision(
                        "rejected", None, row["instrument_symbol"], {},
                        "non-hermes decision provenance", [{"name": "provenance", "passed": False}],
                    ),
                )

            errors = sorted(_validator().iter_errors(decision), key=lambda e: list(e.path))
            if errors:
                first = errors[0]
                location = "$" + "".join(f".{p}" for p in first.path)
                return _write_outcome(
                    cur, row, governor.RiskDecision(
                        "rejected", None, row["instrument_symbol"], {},
                        f"schema invalid {location}: {first.message}",
                        [{"name": "schema", "passed": False}],
                    ),
                )

            stale = _freshness_problem(row, decision, policy)
            if stale:
                return _write_outcome(
                    cur, row, governor.RiskDecision(
                        "needs_review", None, row["instrument_symbol"], {},
                        stale, [{"name": "freshness", "passed": False}],
                    ),
                )

            account_id = decision["intent"].get("target_account_id") or policy.default_account_id
            positions = _load_positions(cur, account_id) if account_id else []
            risk_state = (
                _load_risk_state(cur, account_id, row["instrument_symbol"])
                if account_id and row["instrument_symbol"]
                else {}
            )

            outcome = governor.evaluate(
                decision, positions=positions, risk_state=risk_state, policy=policy
            )
            # Contract §2.2: never AUTO-approve new risk on a stale projection. The signal
            # is already classified + recorded (never dropped); a stale-context approval is
            # held for human review instead of auto-executing. This moves the staleness gate
            # from "drop the message" (wrong) to "hold the approval" (correct).
            if outcome.status == "approved" and _projection_is_stale(cur):
                outcome.status = "needs_review"
                outcome.reason = "context_stale: approval held pending fresh projection"
                outcome.checks = list(outcome.checks) + [
                    {"name": "context_freshness", "passed": False}
                ]
            return _write_outcome(cur, row, outcome, decision=decision, policy=policy)


def _freshness_problem(row: dict[str, Any], decision: dict[str, Any], policy: RiskPolicy) -> str | None:
    """Stale-context guard: a decision created too long ago, or already past its
    valid_until, must not produce new risk (fail closed -> needs_review)."""
    now = datetime.now(timezone.utc)
    created = row.get("created_at")
    if isinstance(created, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (now - created).total_seconds()
        if age > policy.freshness_seconds:
            return f"decision_stale: age {int(age)}s > freshness {policy.freshness_seconds}s"
    valid_until = (decision.get("intent") or {}).get("valid_until")
    if valid_until:
        try:
            vu = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
        except ValueError:
            return f"decision_valid_until_unparseable: {valid_until}"
        if vu.tzinfo is None:
            vu = vu.replace(tzinfo=timezone.utc)
        if now > vu:
            return f"decision_expired: valid_until {valid_until} passed"
    return None


def _write_outcome(
    cur,
    row: dict[str, Any],
    outcome: "governor.RiskDecision",
    *,
    decision: dict[str, Any] | None = None,
    policy: RiskPolicy | None = None,
) -> dict[str, Any]:
    risk_decision_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO risk_decisions (
            risk_decision_id, hermes_decision_id, status, account_id, instrument_id,
            risk_budget, checks, reason, decided_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'decision-gateway')
        """,
        (
            risk_decision_id, str(row["decision_id"]), outcome.status,
            outcome.account_id or "unassigned", outcome.instrument_id,
            Json(outcome.risk_budget), Json(outcome.checks), outcome.reason,
        ),
    )

    result = {
        "risk_decision_id": risk_decision_id,
        "status": outcome.status,
        "reason": outcome.reason,
        "intent_id": None,
    }

    if outcome.status == "approved" and decision is not None and policy is not None:
        result["intent_id"] = _write_trade_intent(
            cur, row, outcome, decision, policy, risk_decision_id
        )
        if result["intent_id"] is not None:
            create_execution_job_for_approved_intent(
                cur.connection,
                result["intent_id"],
                manage_transaction=False,
            )
    return result


def _write_trade_intent(
    cur, row, outcome, decision, policy, risk_decision_id
) -> str | None:
    idempotency_key = _idempotency_key(row, outcome)
    cur.execute(
        "SELECT intent_id::text FROM trade_intents WHERE idempotency_key = %s",
        (idempotency_key,),
    )
    existing = cur.fetchone()
    if existing:
        return existing["intent_id"]

    intent = decision["intent"]
    authorization = _raw_message_authorization(cur, row)
    order_plan = {
        "side": intent.get("side"),
        "entry": intent.get("entry"),
        "stop_loss": intent.get("stop_loss"),
        "take_profits": intent.get("take_profits", []),
        "leverage": intent.get("leverage"),
        "authorization": authorization,
    }
    intent_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO trade_intents (
            intent_id, hermes_decision_id, risk_decision_id, schema_version, account_id,
            instrument_id, action, status, order_plan, risk_budget, target_position_id,
            valid_until, idempotency_key, approved_at
        )
        VALUES (
            %s, %s, %s, '1.0', %s,
            %s, %s, 'approved', %s, %s, %s,
            COALESCE(%s, now() + (%s * interval '1 second')), %s, now()
        )
        """,
        (
            intent_id, str(row["decision_id"]), risk_decision_id, outcome.account_id,
            outcome.instrument_id, row["action"], Json(order_plan),
            Json(outcome.risk_budget), intent.get("target_position_id"),
            intent.get("valid_until"), policy.intent_ttl_seconds, idempotency_key,
        ),
    )
    cur.execute(
        """
        INSERT INTO outbox_events (outbox_event_id, status, aggregate_type, aggregate_id, event_type, payload)
        VALUES (%s, 'pending', 'trade_intent', %s, 'trade_intent.approved', %s)
        """,
        (
            str(uuid4()),
            intent_id,
            Json(
                {
                    "intent_id": intent_id,
                    "risk_decision_id": risk_decision_id,
                    "authorization": authorization,
                }
            ),
        ),
    )
    return intent_id


def _raw_message_authorization(cur, row: dict[str, Any]) -> dict[str, Any]:
    cur.execute(
        """
        SELECT source, channel_id, source_message_id, author_id
        FROM raw_messages
        WHERE id = %s
        """,
        (str(row["raw_message_id"]),),
    )
    raw_message = cur.fetchone()
    if raw_message is None:
        raise GatewayError("raw message authorization source missing")

    source = str(raw_message["source"] or "").strip().lower()
    channel_id = str(raw_message["channel_id"] or "").strip()
    source_message_id = str(raw_message["source_message_id"] or "").strip()
    author_id = str(raw_message["author_id"] or "").strip()
    if not source_message_id:
        raise GatewayError("raw message source_message_id missing")

    user_source = source in {"user", "operator", "manual"}
    authorized_by_type = "user" if user_source else "channel"
    authorized_by_id = channel_id
    if user_source:
        authorized_by_id = author_id or channel_id
    if not authorized_by_id:
        raise GatewayError("raw message authorization identity missing")

    authorization = {
        "authorized_by_type": authorized_by_type,
        "authorized_by_id": authorized_by_id,
        "source_message_id": source_message_id,
        "created_by_service": "decision-gateway",
        "parent_intent_id": False,
    }
    if authorized_by_type == "channel":
        authorization["channel_id"] = channel_id
    return authorization


def _projection_is_stale(cur, threshold_ms: int = 60_000) -> bool:
    """Freshness for auto-approval = the execution projection is CURRENT, which a live node
    proves by HEARTBEATING — not by having traded recently. A quiet (no new fills) period
    must not block new risk while the node is connected and pushing events, otherwise every
    actionable signal during a calm market is held forever (auto-trading deadlock). Fail
    closed: stale when no node has heartbeat within the threshold (node down / no node)."""
    cur.execute("SELECT max(last_seen_at) AS t FROM node_heartbeats")
    row = cur.fetchone()
    last = row["t"] if row else None
    if last is None:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() * 1000 > threshold_ms


def _idempotency_key(row, outcome) -> str:
    material = "|".join(
        [str(row["decision_id"]), row["action"], outcome.instrument_id or "", outcome.account_id or ""]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _load_positions(cur, account_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT account_id, position_id, instrument_id, side::text AS side,
               quantity, avg_entry_price
        FROM positions_projection WHERE account_id = %s AND status = 'open'
        """,
        (account_id,),
    )
    rows = cur.fetchall()
    positions = []
    for r in rows:
        qty = float(r["quantity"] or 0)
        price = float(r["avg_entry_price"] or 0)
        positions.append({
            "account_id": r["account_id"],
            "position_id": r["position_id"],
            "instrument_id": r["instrument_id"],
            "side": r["side"],
            "quantity": qty,
            "notional": qty * price,
        })
    return positions


def _load_risk_state(cur, account_id: str, instrument: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT exposure_notional, open_risk_fraction, state
        FROM risk_state WHERE account_id = %s AND instrument_id = %s
        """,
        (account_id, instrument),
    )
    r = cur.fetchone()
    if r is None:
        return {}
    state = r["state"] or {}
    return {
        "exposure_notional": float(r["exposure_notional"] or 0),
        "open_risk_fraction": float(r["open_risk_fraction"] or 0),
        "mode": state.get("mode", "ACTIVE"),
    }


def _row_to_decision(row: dict[str, Any]) -> dict[str, Any]:
    decision = {
        "schema_version": row["schema_version"],
        "decision_id": str(row["decision_id"]),
        "raw_message_id": str(row["raw_message_id"]),
        "processing_run_id": str(row["processing_run_id"]),
        "context_snapshot_id": str(row["context_snapshot_id"]),
        "classification": {
            "message_type": row["message_type"],
            "action": row["action"],
            "ambiguous": row["ambiguous"],
            "ambiguity_reasons": row["ambiguity_reasons"],
        },
        "intent": {
            "account_scope": row["account_scope"],
            "target_account_id": row["target_account_id"],
            "target_position_id": row["target_position_id"],
            "instrument_symbol": row["instrument_symbol"],
            "side": row["side"],
            "entry": {
                "type": row["entry_type"],
                "price": _num(row["entry_price"]),
                "price_min": _num(row["entry_price_min"]),
                "price_max": _num(row["entry_price_max"]),
            },
            "stop_loss": _num(row["stop_loss"]),
            "take_profits": [_num(x) for x in (row["take_profits"] or [])],
            "leverage": _num(row["leverage"]),
            "valid_until": _iso(row["valid_until"]),
        },
        "evidence": row["evidence"],
        "model": {
            "provider": row["model_provider"],
            "model_version": row["model_version"],
            "prompt_version": row["prompt_version"],
            "temperature": _num(row["temperature"]),
        },
        "created_at": _iso(row["created_at"]),
    }
    if row["confidence"] is not None:
        decision["confidence"] = _num(row["confidence"])
    return decision


def _num(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _iso(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
