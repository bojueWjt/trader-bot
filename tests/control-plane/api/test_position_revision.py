"""Semantic generations against a fully migrated ephemeral PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from position_revision import (
    StalePositionRevision,
    bind_precondition,
    capture_versions,
    invalidate_account,
    invalidate_book,
    read_current,
)


ACCOUNT = "account-a"


def _lock(cur, account_id=ACCOUNT):
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s), 0)", (account_id,))


def test_initial_capture_and_human_bind_are_read_only(db_conn):
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        _lock(cur)
        versions = capture_versions(cur, ACCOUNT)
        assert versions == {"account_id": ACCOUNT, "account_revision": 0, "books": {}}
        assert bind_precondition(cur, ACCOUNT, "btcusdt-perp.binance", "long", versions) == {
            "account_id": ACCOUNT,
            "account_id": ACCOUNT, "account_revision": 0, "book_revision": 0,
            "instrument_id": "BTCUSDT", "position_side": "LONG",
        }
        assert bind_precondition(cur, ACCOUNT, "BTCUSDT", "LONG")["book_revision"] == 0
        cur.execute("SELECT count(*) FROM position_revisions")
        assert cur.fetchone()["count"] == 0


def test_close_reopen_same_quantity_cannot_reuse_snapshot(migrated_db):
    # Quantity can remain identical in the exchange projection throughout this
    # sequence: revision changes on the close intent, never on quantity deltas.
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        _lock(cur)
        before_close = capture_versions(cur, ACCOUNT)
        invalidate_book(cur, ACCOUNT, "BTCUSDT", "LONG", "close-1")
    # A new connection/process still sees the close after flat/reopen.
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        _lock(cur)
        with pytest.raises(StalePositionRevision):
            bind_precondition(cur, ACCOUNT, "BTCUSDT-PERP.BINANCE", "LONG", before_close)
        assert bind_precondition(cur, ACCOUNT, "BTCUSDT", "LONG")["book_revision"] == 1
        after_reopen = capture_versions(cur, ACCOUNT)
        invalidate_book(cur, ACCOUNT, "BTCUSDT", "LONG", "close-2")
        with pytest.raises(StalePositionRevision):
            bind_precondition(cur, ACCOUNT, "BTCUSDT", "LONG", after_reopen)
        assert read_current(cur, ACCOUNT, "BTCUSDT", "LONG")["book_revision"] == 2


def test_duplicate_operation_survives_new_connection(migrated_db):
    for symbol in ("BTCUSDT", "BTCUSDT-PERP.BINANCE", "BTCUSDT"):
        with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
            _lock(cur)
            invalidate_book(cur, ACCOUNT, symbol, "LONG", "same-close")
            invalidate_account(cur, ACCOUNT, "same-close-all")
            assert read_current(cur, ACCOUNT, symbol, "LONG") == {
                "account_id": ACCOUNT,
                "account_id": ACCOUNT, "account_revision": 1, "book_revision": 1,
                "instrument_id": "BTCUSDT", "position_side": "LONG",
            }


def test_rollback_rolls_back_generation_and_idempotency_record(migrated_db):
    conn = psycopg2.connect(migrated_db)
    try:
        with conn.cursor() as cur:
            _lock(cur)
            invalidate_book(cur, ACCOUNT, "BTCUSDT", "LONG", "rolled-back")
            invalidate_account(cur, ACCOUNT, "rolled-back-all")
            # Helpers have neither committed nor closed the caller's transaction.
            with psycopg2.connect(migrated_db) as observer, observer.cursor() as other:
                assert capture_versions(other, ACCOUNT)["books"] == {}
                assert capture_versions(other, ACCOUNT)["account_revision"] == 0
        conn.rollback()
    finally:
        conn.close()
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        _lock(cur)
        assert read_current(cur, ACCOUNT, "BTCUSDT", "LONG")["book_revision"] == 0
        invalidate_book(cur, ACCOUNT, "BTCUSDT", "LONG", "rolled-back")
        invalidate_account(cur, ACCOUNT, "rolled-back-all")
        assert read_current(cur, ACCOUNT, "BTCUSDT", "LONG")["book_revision"] == 1
        assert read_current(cur, ACCOUNT, "BTCUSDT", "LONG")["account_revision"] == 1


def test_book_direction_symbol_and_account_isolation(db_conn):
    with db_conn.cursor() as cur:
        _lock(cur)
        _lock(cur, "account-b")
        before = capture_versions(cur, ACCOUNT)
        other_account = capture_versions(cur, "account-b")
        invalidate_book(cur, ACCOUNT, "BTCUSDT", "LONG", "one-book")
        assert bind_precondition(cur, ACCOUNT, "BTCUSDT", "SHORT", before)["book_revision"] == 0
        assert bind_precondition(cur, ACCOUNT, "ETHUSDT", "LONG", before)["book_revision"] == 0
        assert bind_precondition(cur, "account-b", "BTCUSDT", "LONG", other_account)["book_revision"] == 0
        assert capture_versions(cur, ACCOUNT)["books"] == {"BTCUSDT|LONG": 1}


def test_close_all_invalidates_even_previously_unseen_books(db_conn):
    with db_conn.cursor() as cur:
        _lock(cur)
        invalidate_book(cur, ACCOUNT, "BTCUSDT", "LONG", "first-close")
        before = capture_versions(cur, ACCOUNT)
        invalidate_account(cur, ACCOUNT, "close-all")
        for symbol, side in (("BTCUSDT", "LONG"), ("BTCUSDT", "SHORT"), ("ETHUSDT", "LONG")):
            with pytest.raises(StalePositionRevision):
                bind_precondition(cur, ACCOUNT, symbol, side, before)
        current = bind_precondition(cur, ACCOUNT, "ETHUSDT", "LONG")
        assert current["account_revision"] == 1
        assert current["book_revision"] == 0
        assert capture_versions(cur, ACCOUNT)["books"] == {"BTCUSDT|LONG": 1}
        assert read_current(cur, "account-b", "ETHUSDT", "LONG")["account_revision"] == 0


def test_operation_reuse_for_different_scope_fails(db_conn):
    with db_conn.cursor() as cur:
        _lock(cur)
        invalidate_book(cur, ACCOUNT, "BTCUSDT", "LONG", "close-unique")
        with pytest.raises(ValueError, match="another scope"):
            invalidate_book(cur, ACCOUNT, "BTCUSDT", "SHORT", "close-unique")
        with pytest.raises(ValueError, match="another scope"):
            invalidate_account(cur, ACCOUNT, "close-unique")
        assert read_current(cur, ACCOUNT, "BTCUSDT", "SHORT")["book_revision"] == 0
        assert capture_versions(cur, ACCOUNT)["account_revision"] == 0


def test_invalid_model_snapshot_fails_closed(db_conn):
    with db_conn.cursor() as cur:
        _lock(cur)
        for snapshot in ({}, {"account_id": "account-b", "account_revision": 0, "books": {}},
                         {"account_id": ACCOUNT, "account_revision": False, "books": {}},
                         {"account_id": ACCOUNT, "account_revision": 0, "books": {"BTCUSDT|LONG": "0"}}):
            with pytest.raises(StalePositionRevision):
                bind_precondition(cur, ACCOUNT, "BTCUSDT", "LONG", snapshot)


def test_initial_row_concurrent_upsert_and_duplicate_are_atomic(migrated_db):
    # Exercise uniqueness independently of the stronger caller account lock.
    # Both same-operation dedupe and different-operation initial-row races must
    # be safe at PostgreSQL's default READ COMMITTED isolation level.
    barrier = Barrier(4)

    def close(operation):
        with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '5s'")
            barrier.wait(timeout=5)
            invalidate_book(cur, ACCOUNT, "BTCUSDT", "LONG", operation)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(close, operation) for operation in ("one", "one", "two", "three")]
        for future in futures:
            future.result(timeout=10)
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        assert read_current(cur, ACCOUNT, "BTCUSDT", "LONG")["book_revision"] == 3


def test_close_and_old_add_serialize_under_shared_account_lock(migrated_db):
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        _lock(cur)
        snapshot = capture_versions(cur, ACCOUNT)
    close_ready = Event()
    add_ready = Event()

    def close():
        with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
            _lock(cur)
            invalidate_book(cur, ACCOUNT, "BTCUSDT", "LONG", "serialized-close")
            close_ready.set()
            assert add_ready.wait(timeout=5)

    def add():
        assert close_ready.wait(timeout=5)
        with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '5s'")
            add_ready.set()
            _lock(cur)
            with pytest.raises(StalePositionRevision):
                bind_precondition(cur, ACCOUNT, "BTCUSDT", "LONG", snapshot)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(close), pool.submit(add)]
        for future in futures:
            future.result(timeout=10)


def test_roles_can_only_read_or_invalidate_as_needed(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE trader_v3_operator_query")
        _lock(cur)
        invalidate_book(cur, ACCOUNT, "BTCUSDT", "LONG", "operator-close")
        assert capture_versions(cur, ACCOUNT)["books"] == {"BTCUSDT|LONG": 1}
        for role in ("trader_v3_node_control", "trader_v3_event_ingest"):
            cur.execute("RESET ROLE")
            cur.execute(f"SET LOCAL ROLE {role}")
            assert read_current(cur, ACCOUNT, "BTCUSDT", "LONG")["book_revision"] == 1
            for table in ("position_revisions", "position_revision_invalidations"):
                cur.execute("SELECT has_table_privilege(current_user, %s, 'INSERT, UPDATE, DELETE')", (table,))
                assert cur.fetchone()[0] is False
        cur.execute("RESET ROLE")
        for table in ("accounts_projection", "orders_projection", "execution_events"):
            cur.execute("SELECT has_table_privilege('trader_v3_operator_query', %s, 'UPDATE')", (table,))
            assert cur.fetchone()[0] is False
