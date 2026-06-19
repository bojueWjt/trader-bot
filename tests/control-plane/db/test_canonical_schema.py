from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import psycopg2
from psycopg2 import errors


REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE = REPO_ROOT / "services" / "control-plane"

CORE_TABLES = {
    "raw_messages",
    "media_assets",
    "message_processing_runs",
    "hermes_decisions",
    "risk_decisions",
    "trade_intents",
    "execution_commands",
    "execution_events",
    "orders_projection",
    "positions_projection",
    "accounts_projection",
    "risk_state",
    "node_heartbeats",
    "context_snapshots",
    "audit_events",
    "outbox_events",
    "replay_runs",
    "replay_results",
}

CONTRACT_ENUMS = {
    "message_type_v1": [
        "new_signal",
        "position_update",
        "close_update",
        "analysis",
        "noise",
        "ambiguous",
    ],
    "hermes_action_v1": [
        "open_position",
        "add_position",
        "partial_close",
        "close_position",
        "move_stop_loss",
        "move_stop_to_entry",
        "replace_take_profits",
        "hold",
        "ignore",
        "needs_review",
    ],
    "approved_trade_action_v1": [
        "open_position",
        "add_position",
        "partial_close",
        "close_position",
        "move_stop_loss",
        "move_stop_to_entry",
        "replace_take_profits",
    ],
    "account_scope_v1": ["unassigned", "single", "all"],
    "position_side_v1": ["long", "short"],
    "entry_type_v1": ["market", "limit", "zone", "none"],
    "reconciliation_state_v1": ["healthy", "degraded", "failed"],
}


def _insert_raw_message(conn, *, raw_id: UUID | None = None, source_message_id: str = "msg-1"):
    raw_id = raw_id or uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_messages (
                id, source, channel_id, source_message_id, source_version,
                source_received_at, content_hash, raw_payload
            )
            VALUES (%s, 'telegram', 'channel-a', %s, 'v1',
                    '2026-06-19T12:00:00Z', %s, '{}'::jsonb)
            RETURNING id
            """,
            (str(raw_id), source_message_id, f"sha-{source_message_id}"),
        )
        return UUID(cur.fetchone()[0])


def test_fresh_migration_creates_required_tables_constraints_indexes_and_projection_role(
    run_migration, db_url
):
    run_migration("down")
    run_migration("up")

    with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        tables = {row[0] for row in cur.fetchall()}
        assert CORE_TABLES.issubset(tables)

        cur.execute(
            """
            SELECT t.typname, e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE t.typname = ANY(%s)
            ORDER BY t.typname, e.enumsortorder
            """,
            (list(CONTRACT_ENUMS.keys()),),
        )
        enum_values: dict[str, list[str]] = {}
        for enum_name, enum_value in cur.fetchall():
            enum_values.setdefault(enum_name, []).append(enum_value)
        assert enum_values == CONTRACT_ENUMS

        cur.execute(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conname = ANY(%s)
            """,
            (
                [
                    "uq_raw_messages_source_identity",
                    "ck_trade_intents_approved_chain",
                    "uq_trade_intents_idempotency_key",
                    "uq_execution_events_event_id",
                ],
            ),
        )
        constraints = {row[0] for row in cur.fetchall()}
        assert constraints == {
            "uq_raw_messages_source_identity",
            "ck_trade_intents_approved_chain",
            "uq_trade_intents_idempotency_key",
            "uq_execution_events_event_id",
        }

        cur.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = ANY(%s)
            """,
            (
                [
                    "idx_raw_messages_channel_received",
                    "idx_outbox_events_status",
                    "idx_orders_projection_account_id",
                    "idx_positions_projection_account_id",
                    "idx_accounts_projection_account_id",
                ],
            ),
        )
        indexes = {row[0] for row in cur.fetchall()}
        assert indexes == {
            "idx_raw_messages_channel_received",
            "idx_outbox_events_status",
            "idx_orders_projection_account_id",
            "idx_positions_projection_account_id",
            "idx_accounts_projection_account_id",
        }

        cur.execute(
            """
            SELECT tgname
            FROM pg_trigger
            WHERE tgname = 'trg_raw_messages_source_received_at_insert_only'
            """
        )
        assert cur.fetchone()[0] == "trg_raw_messages_source_received_at_insert_only"

        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'nautilus_projection_writer'")
        assert cur.fetchone() == (1,)
        cur.execute(
            """
            SELECT
                has_table_privilege('nautilus_projection_writer', 'orders_projection', 'INSERT'),
                has_table_privilege('nautilus_projection_writer', 'orders_projection', 'UPDATE'),
                has_table_privilege('nautilus_projection_writer', 'orders_projection', 'SELECT'),
                has_table_privilege('nautilus_projection_writer', 'raw_messages', 'INSERT')
            """
        )
        assert cur.fetchone() == (True, True, False, False)


def test_transaction_rollback_removes_raw_message(db_conn, db_url):
    raw_id = uuid4()
    _insert_raw_message(db_conn, raw_id=raw_id, source_message_id="rollback")
    db_conn.rollback()

    with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_messages WHERE id = %s", (str(raw_id),))
        assert cur.fetchone() == (0,)


def test_unique_raw_message_key_rejects_concurrent_duplicate(db_conn, db_url):
    _insert_raw_message(db_conn, source_message_id="duplicate")
    results: queue.Queue[str] = queue.Queue()

    def insert_duplicate():
        try:
            with psycopg2.connect(db_url) as conn2, conn2.cursor() as cur2:
                cur2.execute("SET lock_timeout = '5s'")
                _insert_raw_message(conn2, source_message_id="duplicate")
                conn2.commit()
                results.put("inserted")
        except errors.UniqueViolation:
            results.put("unique_violation")

    thread = threading.Thread(target=insert_duplicate)
    thread.start()
    time.sleep(0.2)
    db_conn.commit()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert results.get_nowait() == "unique_violation"


def test_repository_commits_raw_message_and_outbox_in_same_transaction(migrated_db):
    sys.path.insert(0, str(CONTROL_PLANE))
    from db.connection import connect, transaction
    from db.repository import ingest_raw_message_with_outbox

    raw_id = uuid4()
    with connect(migrated_db) as conn:
        with transaction(conn):
            inserted_raw_id, outbox_event_id = ingest_raw_message_with_outbox(
                conn,
                {
                    "id": raw_id,
                    "source": "telegram",
                    "channel_id": "channel-a",
                    "source_message_id": "repo-commit",
                    "source_version": "v1",
                    "source_received_at": "2026-06-19T12:00:00Z",
                    "content_hash": "sha-repo-commit",
                    "message_text": "BTC long",
                    "raw_payload": {"message": "BTC long"},
                },
                {
                    "event_type": "raw_message.ingested",
                    "payload": {"raw_message_id": str(raw_id)},
                    "trace_id": raw_id,
                },
            )

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_messages WHERE id = %s", (str(raw_id),))
        assert cur.fetchone() == (1,)
        cur.execute(
            """
            SELECT aggregate_type, aggregate_id, event_type, status
            FROM outbox_events
            WHERE outbox_event_id = %s
            """,
            (str(outbox_event_id),),
        )
        assert cur.fetchone() == (
            "raw_message",
            str(inserted_raw_id),
            "raw_message.ingested",
            "pending",
        )


def test_down_migration_removes_schema_objects(run_migration, db_url):
    run_migration("down")
    run_migration("up")
    run_migration("down")

    with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        assert cur.fetchall() == []
        cur.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'nautilus_projection_writer'"
        )
        assert cur.fetchone() is None
