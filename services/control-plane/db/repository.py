from __future__ import annotations

from uuid import UUID, uuid4

from psycopg2.extras import Json
from psycopg2.extensions import connection as PsycopgConnection


def _uuid_text(value: UUID | str | None) -> str:
    if value is None:
        return str(uuid4())
    return str(value)


def _as_uuid(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def ingest_raw_message_with_outbox(
    conn: PsycopgConnection,
    raw_msg_data: dict,
    outbox_payload: dict,
) -> tuple[UUID, UUID]:
    """Insert one raw message and one outbox event in the caller's transaction."""
    raw_message_id = UUID(_uuid_text(raw_msg_data.get("id")))
    outbox_event_id = UUID(_uuid_text(outbox_payload.get("outbox_event_id")))
    trace_id = outbox_payload.get("trace_id")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_messages (
                id, source, channel_id, source_message_id, source_version,
                source_received_at, author_id, content_hash, message_text, raw_payload
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                str(raw_message_id),
                raw_msg_data["source"],
                raw_msg_data["channel_id"],
                raw_msg_data["source_message_id"],
                raw_msg_data.get("source_version", "v1"),
                raw_msg_data["source_received_at"],
                raw_msg_data.get("author_id"),
                raw_msg_data["content_hash"],
                raw_msg_data.get("message_text"),
                Json(raw_msg_data.get("raw_payload", {})),
            ),
        )
        inserted_raw_message_id = _as_uuid(cur.fetchone()[0])

        cur.execute(
            """
            INSERT INTO outbox_events (
                outbox_event_id, status, aggregate_type, aggregate_id,
                event_type, payload, trace_id
            )
            VALUES (%s, 'pending', %s, %s, %s, %s, %s)
            RETURNING outbox_event_id
            """,
            (
                str(outbox_event_id),
                outbox_payload.get("aggregate_type", "raw_message"),
                str(outbox_payload.get("aggregate_id", inserted_raw_message_id)),
                outbox_payload["event_type"],
                Json(outbox_payload.get("payload", {})),
                str(trace_id) if trace_id is not None else None,
            ),
        )
        inserted_outbox_event_id = _as_uuid(cur.fetchone()[0])

    return inserted_raw_message_id, inserted_outbox_event_id


class ProjectionWriter:
    """Only nautilus_node/projection consumer may write execution projections."""

    def __init__(self, conn: PsycopgConnection):
        self.conn = conn

    def upsert_order_projection(self, payload: dict) -> None:
        raise NotImplementedError("projection consumer interface placeholder")

    def upsert_position_projection(self, payload: dict) -> None:
        raise NotImplementedError("projection consumer interface placeholder")

    def upsert_account_projection(self, payload: dict) -> None:
        raise NotImplementedError("projection consumer interface placeholder")

    def insert_execution_event(self, payload: dict) -> None:
        raise NotImplementedError("projection consumer interface placeholder")
