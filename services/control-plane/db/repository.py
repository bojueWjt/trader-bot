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

    def insert_execution_event(self, payload: dict) -> None:
        # idempotent by event_id: duplicate WS / reconciliation replay must not double-insert.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO execution_events
                    (execution_event_row_id, event_id, schema_version, node_id, account_id,
                     intent_id, client_order_id, venue_order_id, trade_id, event_type, ts_event, payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (str(uuid4()), payload["event_id"], payload.get("schema_version", "1.0"),
                 payload["node_id"], payload["account_id"], payload.get("intent_id"),
                 payload.get("client_order_id"), payload.get("venue_order_id"), payload.get("trade_id"),
                 payload["event_type"], payload["ts_event"], Json(payload.get("payload") or {})),
            )

    def upsert_position_projection(self, payload: dict) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO positions_projection
                    (account_id, position_id, instrument_id, side, quantity, avg_entry_price,
                     mark_price, unrealized_pnl, status, updated_from_event_id, ts_event, updated_at, payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), %s)
                ON CONFLICT (account_id, position_id) DO UPDATE SET
                    instrument_id=EXCLUDED.instrument_id, side=EXCLUDED.side, quantity=EXCLUDED.quantity,
                    avg_entry_price=EXCLUDED.avg_entry_price, mark_price=EXCLUDED.mark_price,
                    unrealized_pnl=EXCLUDED.unrealized_pnl, status=EXCLUDED.status,
                    updated_from_event_id=EXCLUDED.updated_from_event_id, ts_event=EXCLUDED.ts_event,
                    updated_at=now(), payload=EXCLUDED.payload
                """,
                (payload["account_id"], payload["position_id"], payload["instrument_id"],
                 payload.get("side", "long"), payload.get("quantity", 0), payload.get("avg_entry_price"),
                 payload.get("mark_price"), payload.get("unrealized_pnl"), payload.get("status", "open"),
                 payload.get("event_id"), payload.get("ts_event"), Json(payload.get("payload") or {})),
            )

    def record_projection_failure(
        self,
        *,
        event_id: str,
        account_id: str,
        projector: str,
        error: str,
    ) -> None:
        """Durable record of one failed projection derivation (0018)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projection_failures (event_id, account_id, projector, error)
                VALUES (%s,%s,%s,%s)
                """,
                (event_id, account_id, projector, error[:4000]),
            )

    def upsert_projection_watermark(
        self,
        *,
        account_id: str,
        projector: str,
        event_id: str,
        ts_event,
    ) -> None:
        """Advance the per-account projector watermark after a successful derivation."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projection_watermarks
                    (account_id, projector, last_event_id, last_event_ts, updated_at)
                VALUES (%s,%s,%s,COALESCE(%s::timestamptz, now()), now())
                ON CONFLICT (account_id, projector) DO UPDATE SET
                    last_event_id=EXCLUDED.last_event_id,
                    last_event_ts=EXCLUDED.last_event_ts,
                    updated_at=now()
                WHERE (EXCLUDED.last_event_ts, EXCLUDED.last_event_id)
                      >= (projection_watermarks.last_event_ts,
                          projection_watermarks.last_event_id)
                """,
                (account_id, projector, event_id, ts_event),
            )

    def upsert_order_projection(self, payload: dict) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders_projection
                    (order_projection_id, account_id, instrument_id, intent_id, client_order_id,
                     venue_order_id, status, side, order_type, quantity, filled_quantity,
                     updated_from_event_id, ts_event, updated_at, payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,0),%s,%s, now(), %s)
                ON CONFLICT (account_id, client_order_id) DO UPDATE SET
                    instrument_id=EXCLUDED.instrument_id, intent_id=EXCLUDED.intent_id,
                    venue_order_id=EXCLUDED.venue_order_id, status=EXCLUDED.status, side=EXCLUDED.side,
                    order_type=EXCLUDED.order_type, quantity=EXCLUDED.quantity,
                    filled_quantity=EXCLUDED.filled_quantity, updated_from_event_id=EXCLUDED.updated_from_event_id,
                    ts_event=EXCLUDED.ts_event, updated_at=now(), payload=EXCLUDED.payload
                """,
                (str(uuid4()), payload["account_id"], payload["instrument_id"], payload.get("intent_id"),
                 payload.get("client_order_id"), payload.get("venue_order_id"), payload.get("status", "submitted"),
                 payload.get("side"), payload.get("order_type"), payload.get("quantity"),
                 payload.get("filled_quantity"), payload.get("event_id"), payload.get("ts_event"),
                 Json(payload.get("payload") or {})),
            )
