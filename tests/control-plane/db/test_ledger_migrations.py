from __future__ import annotations

from pathlib import Path

import psycopg2
from psycopg2 import errors as pg_errors


def test_trade_outcomes_lot_key_unique_and_nullable_intent(migrated_db):
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='trade_outcomes'
              AND column_name IN ('lot_key','attribution','position_side','intent_id')
            """
        )
        columns = {row[0]: row[1] for row in cur.fetchall()}
        assert columns["lot_key"] == "NO"
        assert columns["attribution"] == "NO"
        assert columns["intent_id"] == "YES"
        cur.execute(
            """
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_schema='public'
              AND table_name='trade_outcomes'
              AND constraint_type='UNIQUE'
            """
        )
        names = {row[0] for row in cur.fetchall()}
        assert "uq_trade_outcomes_account_lot" in names
        assert "uq_trade_outcomes_intent_account" not in names


def test_exchange_income_and_coverage_tables(migrated_db):
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.exchange_income')")
        assert cur.fetchone()[0] is not None
        cur.execute("SELECT to_regclass('public.exchange_income_coverage')")
        assert cur.fetchone()[0] is not None
        cur.execute(
            """
            INSERT INTO exchange_income (
                account_id, tran_id, symbol, income_type, income, asset, income_time
            ) VALUES (
                'account-d', '1', 'BTCUSDT', 'REALIZED_PNL', 1, 'USDT',
                '2026-09-23 16:00:00+00'
            )
            """
        )
        conn.commit()
        cur.execute(
            """
            INSERT INTO exchange_income (
                account_id, tran_id, symbol, income_type, income, asset, income_time
            ) VALUES (
                'account-d', '1', 'BTCUSDT', 'REALIZED_PNL', 1, 'USDT',
                '2026-09-23 16:00:00+00'
            )
            ON CONFLICT DO NOTHING
            """
        )
        cur.execute("SELECT count(*) FROM exchange_income")
        assert cur.fetchone()[0] == 1
        try:
            cur.execute(
                """
                INSERT INTO exchange_income (
                    account_id, tran_id, symbol, income_type, income, asset, income_time
                ) VALUES (
                    'account-d', '1', 'ETHUSDT', 'REALIZED_PNL', 2, 'USDT',
                    '2026-09-23 17:00:00+00'
                )
                """
            )
            raise AssertionError("duplicate tran_id must not insert")
        except pg_errors.UniqueViolation:
            conn.rollback()
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM exchange_income")
        assert cur.fetchone()[0] == 1


def test_trade_outcomes_down_refuses_null_intents(migrated_db, run_migration):
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trade_outcomes (
                outcome_id, intent_id, account_id, lot_key, attribution, side,
                realized_pnl, details
            ) VALUES (
                '00000000-0000-0000-0000-000000000099', NULL, 'account-d',
                'orphan-lot', 'unknown', 'long', 0, '{}'::jsonb
            )
            """
        )
        conn.commit()
    down = (Path(__file__).resolve().parents[3] / "db" / "migrations" / "0023_trade_outcome_lots.down.sql").read_text()
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        try:
            cur.execute(down)
            raise AssertionError("down must fail closed")
        except psycopg2.Error as exc:
            assert "null intent_id" in str(exc)
            conn.rollback()
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trade_outcomes WHERE lot_key='orphan-lot'")
        assert cur.fetchone()[0] == 1
        cur.execute("DELETE FROM trade_outcomes WHERE lot_key='orphan-lot'")
        conn.commit()
