"""Operator command state machine with per-node acknowledgement.

A kill switch / cancel-all / close-all command is NOT done when the HTTP call
returns; it is done only when every target node acknowledges. Single-node failure
yields 'partial'; ack timeouts mark unacked nodes failed and recompute the command.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

from psycopg2.extras import Json

_CP = Path(__file__).resolve().parents[1]  # services/control-plane
for _p in (str(_CP / "db"), str(_CP / "risk_state")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import risk_state as risk_state_mod  # noqa: E402
from connection import transaction  # noqa: E402

MODE_FOR_TYPE = {"HALT": "HALTED", "REDUCE": "REDUCING", "RESUME": "ACTIVE"}
TERMINAL_STATUSES = ("completed", "partial", "failed")


class CommandError(RuntimeError):
    pass


def issue_command(
    conn,
    *,
    command_type: str,
    requested_by: str,
    reason: str,
    idempotency_key: str,
    target_nodes: list[str],
    scope: dict | None = None,
    ack_timeout_seconds: int = 30,
) -> dict:
    scope = scope or {}
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT command_id::text, status FROM operator_commands WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            existing = cur.fetchone()
            if existing:
                return {"command_id": existing[0], "status": existing[1], "idempotent": True}

            command_id = str(uuid4())
            cur.execute(
                """
                INSERT INTO operator_commands
                    (command_id, command_type, scope, status, requested_by, reason, idempotency_key)
                VALUES (%s, %s, %s, 'pending', %s, %s, %s)
                """,
                (command_id, command_type, Json(scope), requested_by, reason, idempotency_key),
            )
            for node in target_nodes:
                cur.execute(
                    """
                    INSERT INTO command_node_acks (command_id, node_id, status, expires_at)
                    VALUES (%s, %s, 'pending', now() + make_interval(secs => %s))
                    """,
                    (command_id, node, ack_timeout_seconds),
                )

            mode = MODE_FOR_TYPE.get(command_type)
            if mode and scope.get("account_id"):
                for instrument in scope.get("instruments", []):
                    risk_state_mod.set_mode(cur, scope["account_id"], instrument, mode)

            if not target_nodes:
                cur.execute(
                    "UPDATE operator_commands SET status='completed', completed_at=now() WHERE command_id=%s",
                    (command_id,),
                )
                status = "completed"
            else:
                status = "pending"
    return {"command_id": command_id, "status": status, "idempotent": False}


def record_ack(conn, command_id: str, node_id: str, *, status: str, detail: str | None = None) -> dict:
    if status not in ("acked", "failed"):
        raise ValueError(f"invalid ack status {status!r}")
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE command_node_acks SET status=%s, detail=%s, ack_at=now() "
                "WHERE command_id=%s AND node_id=%s",
                (status, detail, command_id, node_id),
            )
            if cur.rowcount != 1:
                raise CommandError(f"unknown command/node {command_id}/{node_id}")
            _recompute(cur, command_id)
    return get_status(conn, command_id)


def expire_timeouts(conn) -> list[str]:
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE command_node_acks
                   SET status='failed', detail=COALESCE(detail, 'ack timeout'), ack_at=now()
                 WHERE status='pending' AND expires_at IS NOT NULL AND expires_at <= now()
                 RETURNING command_id::text
                """
            )
            command_ids = sorted({row[0] for row in cur.fetchall()})
            for command_id in command_ids:
                _recompute(cur, command_id)
    return command_ids


def get_status(conn, command_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, command_type FROM operator_commands WHERE command_id = %s",
            (command_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise CommandError(f"unknown command {command_id}")
        cur.execute(
            "SELECT node_id, status FROM command_node_acks WHERE command_id=%s ORDER BY node_id",
            (command_id,),
        )
        acks = [{"node_id": node, "status": status} for node, status in cur.fetchall()]
    return {
        "command_id": command_id,
        "status": row[0],
        "command_type": row[1],
        "completed": row[0] in TERMINAL_STATUSES,
        "acks": acks,
    }


def _recompute(cur, command_id: str) -> None:
    cur.execute("SELECT status FROM command_node_acks WHERE command_id = %s", (command_id,))
    statuses = [row[0] for row in cur.fetchall()]
    if not statuses or all(s == "acked" for s in statuses):
        new_status = "completed"
    elif all(s == "failed" for s in statuses):
        new_status = "failed"
    elif any(s == "pending" for s in statuses):
        new_status = "acknowledged" if any(s == "acked" for s in statuses) else "pending"
    else:
        new_status = "partial"  # mix of acked + failed, none pending
    cur.execute(
        """
        UPDATE operator_commands
           SET status=%s,
               completed_at = CASE WHEN %s AND completed_at IS NULL THEN now() ELSE completed_at END
         WHERE command_id=%s
        """,
        (new_status, new_status in TERMINAL_STATUSES, command_id),
    )
