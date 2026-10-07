"""Equity-sample side effects of exchange_state_recorder.run_once
(contracts/backend-api.md §8, T1). Uses a real ephemeral Postgres (see
../conftest.py) because the behavior under test is the SQL upsert semantics
themselves (ON CONFLICT dedupe + savepoint failure isolation), not just
Python control flow.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = (
    REPO_ROOT / "services" / "control-plane" / "tools" / "exchange_state_recorder.py"
)


def _load_module() -> types.ModuleType:
    modules: dict[str, types.ModuleType] = {}
    if "psycopg2" not in sys.modules:
        try:
            __import__("psycopg2")
        except ModuleNotFoundError:
            modules["psycopg2"] = types.ModuleType("psycopg2")
    spec = importlib.util.spec_from_file_location(
        "_exchange_state_recorder_equity_samples_under_test",
        MODULE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load exchange state recorder: {MODULE_PATH}")
    with patch.dict(sys.modules, modules):
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def _fake_snapshot(equity: str, free: str, margin: str):
    def _snapshot_account(base, key, secret, opener=None):
        return {
            "source": "binance_fapi",
            "fetched_at": "2026-09-05T00:00:00Z",
            "account": {
                "currency": "USDT",
                "equity": equity,
                "margin": margin,
                "free": free,
            },
            "positions": [],
            "open_orders": [],
            "algo_orders": [],
            "protections": [],
        }

    return _snapshot_account


def _run_once_for_all_accounts(module, conn, equity: str, free: str, margin: str) -> None:
    with patch.object(
        module,
        "container_keys",
        return_value=("api-key", "api-secret"),
    ), patch.object(
        module,
        "account_binance_opener",
        return_value=object(),
    ), patch.object(
        module,
        "snapshot_account",
        side_effect=_fake_snapshot(equity, free, margin),
    ):
        module.run_once(conn, "https://fapi.binance.com")


def test_two_samples_in_the_same_bucket_keep_only_the_latest_row(db_conn) -> None:
    module = _load_module()

    _run_once_for_all_accounts(module, db_conn, equity="1000", free="900", margin="100")
    _run_once_for_all_accounts(module, db_conn, equity="1050", free="950", margin="90")

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT equity, available, margin FROM account_equity_samples "
            "WHERE account_id = 'account-a'"
        )
        rows = cur.fetchall()

    assert len(rows) == 1, "same 30-minute bucket must dedupe to a single row"
    equity, available, margin = rows[0]
    assert str(equity) == "1050"
    assert str(available) == "950"
    assert str(margin) == "90"


def test_two_samples_in_the_same_bucket_do_not_duplicate_across_all_accounts(
    db_conn,
) -> None:
    module = _load_module()

    _run_once_for_all_accounts(module, db_conn, equity="10", free="9", margin="1")
    _run_once_for_all_accounts(module, db_conn, equity="20", free="18", margin="2")

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM account_equity_samples")
        (total,) = cur.fetchone()

    assert total == 4  # one row per account, not one per run_once call


def test_equity_sample_failure_does_not_block_the_mirror_write(db_conn) -> None:
    module = _load_module()
    with db_conn.cursor() as cur:
        cur.execute("DROP TABLE account_equity_samples")
    db_conn.commit()

    # Must not raise even though the sample table is gone.
    _run_once_for_all_accounts(module, db_conn, equity="500", free="490", margin="10")

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT equity, margin, available_balance FROM accounts_projection "
            "WHERE account_id = 'account-a'"
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT payload FROM exchange_state_mirror WHERE account_id = 'account-a'"
        )
        mirror_row = cur.fetchone()

    assert row is not None, "accounts_projection write must still happen"
    equity, margin, available = row
    assert str(equity) == "500"
    assert str(margin) == "10"
    assert str(available) == "490"
    assert mirror_row is not None, "exchange_state_mirror write must still happen"


def test_equity_sample_failure_leaves_the_connection_usable_for_later_writes(
    db_conn,
) -> None:
    """A savepoint-scoped failure must not poison the outer transaction for
    the accounts covered later in the same run_once loop."""
    module = _load_module()
    with db_conn.cursor() as cur:
        cur.execute("DROP TABLE account_equity_samples")
    db_conn.commit()

    _run_once_for_all_accounts(module, db_conn, equity="1", free="1", margin="0")

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM accounts_projection")
        (count,) = cur.fetchone()

    assert count == 4, "every account must still be written despite the sample failure"
