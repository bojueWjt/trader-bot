"""Persistent OM6 node command run orchestrator.

The legacy ``operator_commands`` table keeps its coarse pending/acked rollup for
dashboard and API compatibility. OM6's frozen command lifecycle is persisted per
node in ``node_command_runs`` where the schema already allows the canonical
states: accepted -> running -> verifying -> terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from psycopg2.extras import Json

from db.connection import transaction

from .metrics import inject_trace_context
from .state_descriptor import legal_transition


TERMINAL_STATUSES = frozenset({"completed", "partial", "failed", "timed_out"})


class CommandRunError(RuntimeError):
    pass


class CommandRunTransitionError(CommandRunError):
    pass


@dataclass(frozen=True)
class CommandRunRef:
    request_id: str


def request_command_run(
    conn,
    *,
    node_id: str,
    command_type: str,
    request_id: str,
    payload: dict[str, Any] | None = None,
    command_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Create or replay a durable command run.

    ``request_id`` is the public idempotency boundary. The DB uniqueness is
    ``(node_id, idempotency_key)``, so the default idempotency key is the request
    id. A replay returns the exact current/terminal row instead of creating a new
    run or re-running side effects.
    """

    _require("node_id", node_id)
    _require("command_type", command_type)
    request_id = _require("request_id", request_id)
    idem = idempotency_key or request_id
    run_payload = inject_trace_context(
        payload or {},
        request_id=request_id,
        idempotency_key=idem,
        command_id=command_id,
    )

    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    node_command_run_id::text,
                    command_id::text,
                    node_id,
                    command_type,
                    request_id,
                    idempotency_key,
                    status,
                    payload,
                    started_at,
                    completed_at
                FROM node_command_runs
                WHERE node_id=%s AND idempotency_key=%s
                """,
                (node_id, idem),
            )
            existing = cur.fetchone()
            if existing is not None:
                return {**_row_to_dict(existing), "idempotent": True}

            run_id = str(uuid4())
            cur.execute(
                """
                INSERT INTO node_command_runs (
                    node_command_run_id,
                    command_id,
                    node_id,
                    command_type,
                    request_id,
                    idempotency_key,
                    status,
                    payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'accepted', %s)
                RETURNING
                    node_command_run_id::text,
                    command_id::text,
                    node_id,
                    command_type,
                    request_id,
                    idempotency_key,
                    status,
                    payload,
                    started_at,
                    completed_at
                """,
                (
                    run_id,
                    command_id,
                    node_id,
                    command_type,
                    request_id,
                    idem,
                    Json(run_payload),
                ),
            )
            return {**_row_to_dict(cur.fetchone()), "idempotent": False}


def mark_running(
    conn,
    *,
    request_id: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _transition(conn, request_id=request_id, status="running", result=result)


def mark_verifying(
    conn,
    *,
    request_id: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _transition(conn, request_id=request_id, status="verifying", result=result)


def mark_terminal(
    conn,
    *,
    request_id: str,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"terminal command status expected, got {status!r}")
    return _transition(conn, request_id=request_id, status=status, result=result, error=error)


def get_command_run(conn, *, request_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                node_command_run_id::text,
                command_id::text,
                node_id,
                command_type,
                request_id,
                idempotency_key,
                status,
                payload,
                started_at,
                completed_at
            FROM node_command_runs
            WHERE request_id=%s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (request_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise CommandRunError(f"unknown command request_id {request_id!r}")
    return _row_to_dict(row)


def _transition(
    conn,
    *,
    request_id: str,
    status: str,
    result: dict[str, Any] | None,
    error: str | None = None,
) -> dict[str, Any]:
    request_id = _require("request_id", request_id)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    node_command_run_id::text,
                    command_id::text,
                    node_id,
                    command_type,
                    request_id,
                    idempotency_key,
                    status,
                    payload,
                    started_at,
                    completed_at
                FROM node_command_runs
                WHERE request_id=%s
                ORDER BY created_at DESC
                LIMIT 1
                FOR UPDATE
                """,
                (request_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise CommandRunError(f"unknown command request_id {request_id!r}")
            current = str(row[6])
            if current == status or current in TERMINAL_STATUSES:
                return _row_to_dict(row)
            if not legal_transition("command", current, status):
                raise CommandRunTransitionError(f"illegal command transition {current!r} -> {status!r}")

            payload = inject_trace_context(
                row[7] or {},
                request_id=request_id,
                idempotency_key=row[5],
                command_id=row[1],
            )
            if result is not None:
                payload["result"] = dict(result)
            if error is not None:
                payload["error"] = str(error)
            payload["last_status"] = status

            cur.execute(
                """
                UPDATE node_command_runs
                   SET status=%s,
                       payload=%s,
                       completed_at=CASE
                           WHEN %s THEN COALESCE(completed_at, now())
                           ELSE completed_at
                       END
                 WHERE node_command_run_id=%s
                 RETURNING
                    node_command_run_id::text,
                    command_id::text,
                    node_id,
                    command_type,
                    request_id,
                    idempotency_key,
                    status,
                    payload,
                    started_at,
                    completed_at
                """,
                (status, Json(payload), status in TERMINAL_STATUSES, row[0]),
            )
            return _row_to_dict(cur.fetchone())


def _row_to_dict(row) -> dict[str, Any]:
    status = str(row[6])
    return {
        "node_command_run_id": row[0],
        "command_id": row[1],
        "node_id": row[2],
        "command_type": row[3],
        "request_id": row[4],
        "idempotency_key": row[5],
        "status": status,
        "payload": dict(row[7] or {}),
        "started_at": row[8],
        "completed_at": row[9],
        "completed": status in TERMINAL_STATUSES,
    }


def _require(name: str, value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text
