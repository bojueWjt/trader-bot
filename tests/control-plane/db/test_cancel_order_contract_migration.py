from __future__ import annotations

from pathlib import Path

import psycopg2


REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_SCHEMA_UP = (
    REPO_ROOT / "db" / "migrations" / "0001_canonical_schema.up.sql"
)
CANONICAL_SCHEMA_DOWN = (
    REPO_ROOT / "db" / "migrations" / "0001_canonical_schema.down.sql"
)
CANCEL_ORDER_CONTRACT_UP = (
    REPO_ROOT / "db" / "migrations" / "0014_cancel_order_contract.up.sql"
)
CANCEL_ORDER_CONTRACT_DOWN = (
    REPO_ROOT / "db" / "migrations" / "0014_cancel_order_contract.down.sql"
)

BASELINE_ACTION_ENUMS = {
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
}

UPGRADED_ACTION_ENUMS = {
    "hermes_action_v1": [
        "open_position",
        "add_position",
        "partial_close",
        "close_position",
        "move_stop_loss",
        "move_stop_to_entry",
        "replace_take_profits",
        "cancel_order",
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
        "cancel_order",
    ],
}


def _enum_values(cur) -> dict[str, list[str]]:
    cur.execute(
        """
        SELECT t.typname, e.enumlabel
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typname = ANY(%s)
        ORDER BY t.typname, e.enumsortorder
        """,
        (list(BASELINE_ACTION_ENUMS),),
    )
    values: dict[str, list[str]] = {}
    for enum_name, enum_value in cur.fetchall():
        values.setdefault(enum_name, []).append(enum_value)
    return values


def test_cancel_order_is_a_reversible_0014_enum_increment(
    run_migration,
    db_url,
) -> None:
    run_migration("down")

    with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(CANONICAL_SCHEMA_UP.read_text(encoding="utf-8"))
        assert _enum_values(cur) == BASELINE_ACTION_ENUMS

        cur.execute(CANCEL_ORDER_CONTRACT_UP.read_text(encoding="utf-8"))
        assert _enum_values(cur) == UPGRADED_ACTION_ENUMS

        cur.execute(CANCEL_ORDER_CONTRACT_DOWN.read_text(encoding="utf-8"))
        assert _enum_values(cur) == BASELINE_ACTION_ENUMS

        cur.execute(CANONICAL_SCHEMA_DOWN.read_text(encoding="utf-8"))
