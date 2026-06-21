from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from account_budget import load_account_budget
from order_management.freshness import FreshnessConfig


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_account_budget_reads_decimal_equity_free_margin_and_margin_ratio(db_conn) -> None:
    _insert_account(db_conn, "acct-om3-a", equity="10000", margin="1250", available="8000")

    budget = load_account_budget(db_conn, "acct-om3-a", now=NOW)

    assert budget.account_id == "acct-om3-a"
    assert budget.equity == Decimal("10000")
    assert budget.free_margin == Decimal("8000")
    assert budget.margin_ratio == Decimal("0.125")
    assert budget.is_fresh is True
    assert budget.can_take_new_risk is True
    assert budget.stale_reasons == []


def test_stale_account_budget_fails_closed_to_no_new_risk(db_conn) -> None:
    _insert_account(
        db_conn,
        "acct-om3-stale",
        equity="10000",
        margin="0",
        available="9000",
        updated_at=NOW - timedelta(seconds=60),
    )

    budget = load_account_budget(
        db_conn,
        "acct-om3-stale",
        now=NOW,
        freshness=FreshnessConfig(account_data_stale_seconds=15, projection_lag_threshold_ms=5000),
    )

    assert budget.is_fresh is False
    assert budget.can_take_new_risk is False
    assert "account_data" in budget.stale_reasons


def test_missing_account_budget_fails_closed(db_conn) -> None:
    budget = load_account_budget(db_conn, "acct-om3-missing", now=NOW)

    assert budget.equity == Decimal("0")
    assert budget.is_fresh is False
    assert budget.can_take_new_risk is False
    assert budget.stale_reasons == ["account_missing"]


def test_account_budgets_are_isolated_per_account(db_conn) -> None:
    _insert_account(db_conn, "acct-om3-a", equity="10000", margin="0", available="9000")
    _insert_account(db_conn, "acct-om3-b", equity="2500", margin="500", available="1200")

    a = load_account_budget(db_conn, "acct-om3-a", now=NOW)
    b = load_account_budget(db_conn, "acct-om3-b", now=NOW)

    assert a.equity == Decimal("10000")
    assert a.free_margin == Decimal("9000")
    assert b.equity == Decimal("2500")
    assert b.free_margin == Decimal("1200")


def _insert_account(
    conn,
    account_id: str,
    *,
    equity: str,
    margin: str,
    available: str,
    updated_at: datetime = NOW,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts_projection (
                account_id, currency, equity, margin, available_balance,
                projection_lag_ms, last_execution_event_at, updated_at
            )
            VALUES (%s, 'USDT', %s, %s, %s, 0, %s, %s)
            """,
            (account_id, equity, margin, available, updated_at, updated_at),
        )
    conn.commit()

