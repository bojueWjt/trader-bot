from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from uuid import UUID

import psycopg2


REPO_ROOT = Path(__file__).resolve().parents[3]
UP = (
    REPO_ROOT
    / "db"
    / "migrations"
    / "0010_evidence_and_poll_indexes.up.sql"
)
DOWN = (
    REPO_ROOT
    / "db"
    / "migrations"
    / "0010_evidence_and_poll_indexes.down.sql"
)
INDEX_NAMES = (
    "idx_execution_events_targeted_opening_evidence",
    "idx_trade_intents_pending_opening_symbols",
    "idx_command_node_acks_pending_poll",
)
TEST_SCHEMA = "migration_0010_test"
RECORDER = (
    REPO_ROOT
    / "services"
    / "control-plane"
    / "tools"
    / "exchange_state_recorder.py"
)
CONTROL_PLANE_API = (
    REPO_ROOT
    / "services"
    / "control-plane"
    / "api"
)
if str(CONTROL_PLANE_API) not in sys.path:
    sys.path.insert(0, str(CONTROL_PLANE_API))

import read_api  # noqa: E402


def test_migration_0010_creates_and_removes_evidence_and_poll_indexes(
    pg_cluster,
) -> None:
    with psycopg2.connect(pg_cluster["url"]) as conn:
        try:
            _create_minimal_index_tables(conn)
            with conn.cursor() as cur:
                cur.execute(UP.read_text(encoding="utf-8"))
                cur.execute(
                    """
                    SELECT indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname=%s
                      AND indexname = ANY(%s)
                    """,
                    (TEST_SCHEMA, list(INDEX_NAMES)),
                )
                index_definitions = dict(cur.fetchall())

            assert set(index_definitions) == set(INDEX_NAMES)
            assert (
                "(account_id, client_order_id, ts_event DESC, created_at DESC)"
                in index_definitions[
                    "idx_execution_events_targeted_opening_evidence"
                ]
            )
            assert (
                "WHERE (client_order_id IS NOT NULL)"
                in index_definitions[
                    "idx_execution_events_targeted_opening_evidence"
                ]
            )
            assert (
                "(account_id, instrument_id, updated_at DESC, intent_id)"
                in index_definitions[
                    "idx_trade_intents_pending_opening_symbols"
                ]
            )
            pending_definition = index_definitions[
                "idx_trade_intents_pending_opening_symbols"
            ]
            assert "'approved'::text" in pending_definition
            assert "'expired'::text" in pending_definition
            assert "'open_position'::text" in pending_definition
            assert "'add_position'::text" in pending_definition
            assert (
                "(node_id, created_at, command_id)"
                in index_definitions[
                    "idx_command_node_acks_pending_poll"
                ]
            )
            assert (
                "WHERE (status = 'pending'::text)"
                in index_definitions[
                    "idx_command_node_acks_pending_poll"
                ]
            )

            with conn.cursor() as cur:
                cur.execute(DOWN.read_text(encoding="utf-8"))
                cur.execute(
                    """
                    SELECT count(*)
                    FROM pg_indexes
                    WHERE schemaname=%s
                      AND indexname = ANY(%s)
                    """,
                    (TEST_SCHEMA, list(INDEX_NAMES)),
                )
                assert cur.fetchone() == (0,)
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"
                )


def test_pending_opening_symbols_are_distinct_rotating_and_evidence_scoped(
    pg_cluster,
) -> None:
    recorder = _load_recorder()
    with psycopg2.connect(pg_cluster["url"]) as conn:
        try:
            _create_minimal_index_tables(conn)
            _seed_opening_intents(conn)
            with conn.cursor() as cur:
                cur.execute(UP.read_text(encoding="utf-8"))

            first_batch = recorder.pending_opening_symbols(
                conn,
                "account-a",
                limit=2,
            )
            rotated_batch = recorder.pending_opening_symbols(
                conn,
                "account-a",
                limit=2,
                after_symbol="ETHUSDT",
            )

            assert first_batch == ("BTCUSDT", "ETHUSDT")
            assert rotated_batch == ("SOLUSDT", "BTCUSDT")
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"
                )


def test_pending_command_poll_uses_stable_keyset_and_restarts_unknown_cursor(
    pg_cluster,
) -> None:
    with psycopg2.connect(pg_cluster["url"]) as conn:
        try:
            _create_minimal_index_tables(conn)
            _seed_operator_commands(conn)
            with conn.cursor() as cur:
                cur.execute(UP.read_text(encoding="utf-8"))

            first_page = read_api._pending_node_commands(
                conn,
                node_id="node-a",
                after=None,
                limit=2,
            )
            first_ids = tuple(row[0] for row in first_page)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE command_node_acks
                    SET status='acked'
                    WHERE node_id='node-a'
                      AND command_id=%s
                    """,
                    (first_ids[1],),
                )

            second_page = read_api._pending_node_commands(
                conn,
                node_id="node-a",
                after=first_ids[1],
                limit=2,
            )
            unknown_cursor_page = read_api._pending_node_commands(
                conn,
                node_id="node-a",
                after="not-a-command-id",
                limit=2,
            )
            other_node_cursor_page = read_api._pending_node_commands(
                conn,
                node_id="node-a",
                after="00000000-0000-0000-0000-000000000099",
                limit=2,
            )

            assert first_ids == (
                "00000000-0000-0000-0000-000000000001",
                "00000000-0000-0000-0000-000000000002",
            )
            assert tuple(row[0] for row in second_page) == (
                "00000000-0000-0000-0000-000000000003",
                "00000000-0000-0000-0000-000000000004",
            )
            expected_restart = (
                "00000000-0000-0000-0000-000000000001",
                "00000000-0000-0000-0000-000000000003",
            )
            assert (
                tuple(row[0] for row in unknown_cursor_page)
                == expected_restart
            )
            assert (
                tuple(row[0] for row in other_node_cursor_page)
                == expected_restart
            )
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"
                )


def _create_minimal_index_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE;
            CREATE SCHEMA {TEST_SCHEMA};
            SET search_path TO {TEST_SCHEMA};

            CREATE TABLE execution_events (
                account_id text NOT NULL,
                intent_id uuid,
                client_order_id text,
                venue_order_id text,
                trade_id text,
                event_type text NOT NULL,
                ts_event timestamptz NOT NULL,
                created_at timestamptz NOT NULL
            );

            CREATE TABLE trade_intents (
                intent_id uuid PRIMARY KEY,
                account_id text NOT NULL,
                instrument_id text NOT NULL,
                action text NOT NULL,
                status text NOT NULL,
                updated_at timestamptz NOT NULL
            );

            CREATE TABLE orders_projection (
                account_id text NOT NULL,
                intent_id uuid,
                venue_order_id text,
                status text NOT NULL,
                filled_quantity numeric NOT NULL DEFAULT 0
            );

            CREATE TABLE command_node_acks (
                command_id uuid NOT NULL,
                node_id text NOT NULL,
                status text NOT NULL,
                created_at timestamptz NOT NULL
            );

            CREATE TABLE operator_commands (
                command_id uuid PRIMARY KEY,
                command_type text NOT NULL,
                scope jsonb NOT NULL,
                created_at timestamptz NOT NULL
            );
            """
        )


def _seed_opening_intents(conn) -> None:
    intents = (
        (
            "00000000-0000-0000-0000-000000000001",
            "BTCUSDT-PERP.BINANCE",
            "approved",
            "2026-08-09T00:00:01Z",
        ),
        (
            "00000000-0000-0000-0000-000000000002",
            "BTCUSDT-PERP.BINANCE",
            "approved",
            "2026-08-09T00:00:02Z",
        ),
        (
            "00000000-0000-0000-0000-000000000003",
            "ETHUSDT-PERP.BINANCE",
            "approved",
            "2026-08-09T00:00:03Z",
        ),
        (
            "00000000-0000-0000-0000-000000000004",
            "SOLUSDT-PERP.BINANCE",
            "expired",
            "2026-08-09T00:00:04Z",
        ),
        (
            "00000000-0000-0000-0000-000000000005",
            "XRPUSDT-PERP.BINANCE",
            "approved",
            "2026-08-09T00:00:05Z",
        ),
        (
            "00000000-0000-0000-0000-000000000006",
            "ADAUSDT-PERP.BINANCE",
            "approved",
            "2026-08-09T00:00:06Z",
        ),
    )
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO trade_intents (
                intent_id,
                account_id,
                instrument_id,
                action,
                status,
                updated_at
            )
            VALUES (%s, 'account-a', %s, 'open_position', %s, %s)
            """,
            intents,
        )
        cur.execute(
            """
            INSERT INTO execution_events (
                account_id,
                intent_id,
                client_order_id,
                event_type,
                ts_event,
                created_at
            )
            VALUES (
                'account-a',
                '00000000-0000-0000-0000-000000000004',
                'B0000000000000000000000000000000401',
                'OrderSubmitted',
                '2026-08-09T00:00:04Z',
                '2026-08-09T00:00:04Z'
            ), (
                'account-a',
                '00000000-0000-0000-0000-000000000005',
                'B0000000000000000000000000000000501',
                'OrderAccepted',
                '2026-08-09T00:00:05Z',
                '2026-08-09T00:00:05Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO orders_projection (
                account_id,
                intent_id,
                venue_order_id,
                status,
                filled_quantity
            )
            VALUES (
                'account-a',
                '00000000-0000-0000-0000-000000000006',
                'venue-ada',
                'accepted',
                0
            )
            """
        )


def _seed_operator_commands(conn) -> None:
    rows = [
        (
            str(UUID(int=index)),
            f"2026-08-09T00:00:0{index}Z",
        )
        for index in range(1, 5)
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO operator_commands (
                command_id,
                command_type,
                scope,
                created_at
            )
            VALUES (%s, 'HALT', '{}'::jsonb, %s)
            """,
            rows,
        )
        cur.executemany(
            """
            INSERT INTO command_node_acks (
                command_id,
                node_id,
                status,
                created_at
            )
            VALUES (%s, 'node-a', 'pending', %s)
            """,
            rows,
        )
        cur.execute(
            """
            INSERT INTO operator_commands (
                command_id,
                command_type,
                scope,
                created_at
            )
            VALUES (
                '00000000-0000-0000-0000-000000000099',
                'HALT',
                '{}'::jsonb,
                '2026-08-09T00:00:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO command_node_acks (
                command_id,
                node_id,
                status,
                created_at
            )
            VALUES (
                '00000000-0000-0000-0000-000000000099',
                'node-b',
                'pending',
                '2026-08-09T00:00:00Z'
            )
            """
        )


def _load_recorder():
    spec = importlib.util.spec_from_file_location(
        "_exchange_state_recorder_migration_test",
        RECORDER,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load exchange state recorder: {RECORDER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
