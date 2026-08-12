from __future__ import annotations

import ast
import hashlib
import importlib.util
import math
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "patch_legacy_control_plane_multiplier.py"
PRODUCTION_BLOB = "bc68016d29180524b0777afe60e975f1b112f177"
PRODUCTION_SHA256 = (
    "953a429f3290e64409784e1fc9ce5e69f1fbff2eb94646ff313fdae7769ee943"
)


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "patch_legacy_control_plane_multiplier",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _production_source() -> bytes:
    result = subprocess.run(
        ["git", "cat-file", "-p", PRODUCTION_BLOB],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return result.stdout


def _patched_source(module) -> str:
    source = _production_source()
    assert hashlib.sha256(source).hexdigest() == PRODUCTION_SHA256
    return module.patch_source(source.decode("utf-8"))


def _runtime_namespace(patched_source: str) -> dict:
    selected_names = {
        "_watcher_account_is_enabled",
        "_watcher_risk_capital_multiplier",
        "_channel_risk_capital_multiplier",
        "_account_risk_capital_multiplier",
        "_account_financial_state",
        "_account_equity",
        "_size_open_order",
    }
    tree = ast.parse(patched_source)
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in selected_names
    ]
    namespace = {
        "HTTPException": HTTPException,
        "math": math,
        "os": os,
        "psycopg2": SimpleNamespace(),
        "_OPERATOR_ACCOUNTS": (
            "account-a",
            "account-b",
            "account-c",
            "account-d",
        ),
        "_WATCHER_TRADING_DB": "",
    }
    runtime_module = ast.Module(body=selected, type_ignores=[])
    exec(  # noqa: S102 - executes reviewed functions extracted from pinned source
        compile(runtime_module, "patched-read-api.py", "exec"),
        namespace,
    )
    return namespace


def _create_watcher_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE account_configs (
                account_id TEXT PRIMARY KEY,
                execution_account_id TEXT NOT NULL,
                risk_capital_multiplier REAL NOT NULL,
                is_enabled INTEGER NOT NULL
            );
            CREATE TABLE channel_routing (
                channel_id TEXT PRIMARY KEY,
                target_account_id TEXT NOT NULL
            );
            INSERT INTO account_configs VALUES
                ('jiataotx@gmail.com', 'account-a', 1.25, 1),
                ('balenwong3@gmail.com', 'account-b', 1.50, 1),
                ('泰山', 'account-c', 2.00, 1),
                ('黄山', 'account-d', 3.00, 1);
            INSERT INTO channel_routing VALUES
                ('-1002136478186', 'jiataotx@gmail.com'),
                ('-1002198013097', 'balenwong3@gmail.com'),
                ('-1002189417451', '泰山'),
                ('-1002193304023', '黄山');
            """
        )
        conn.commit()
    finally:
        conn.close()


def _caps() -> dict:
    return {
        "max_notional": None,
        "max_leverage": 10.0,
        "max_risk_fraction": 0.06,
        "no_sl_equity_fraction": 0.2,
    }


def test_production_preimage_patch_is_deterministic_and_compiles() -> None:
    module = _load_script()
    source = _production_source()

    assert hashlib.sha256(source).hexdigest() == module.EXPECTED_SOURCE_SHA256
    patched_a = module.patch_source(source.decode("utf-8"))
    patched_b = module.patch_source(source.decode("utf-8"))

    assert patched_a == patched_b
    assert (
        hashlib.sha256(patched_a.encode("utf-8")).hexdigest()
        == module.EXPECTED_PATCHED_SHA256
    )
    compile(patched_a, "patched-read-api.py", "exec")
    assert '"account-c"' in patched_a
    assert '"account-d"' in patched_a
    assert "effective_equity = real_equity * multiplier" in patched_a
    assert "available_ceiling = available_balance * hard_leverage" in patched_a
    assert "max_age_seconds = 60.0" in patched_a
    assert "binance_fapi_account_v3" in patched_a
    assert "account_snapshot_fetched_at" in patched_a
    assert "OPERATOR_EQUITY_" not in patched_a
    assert "OPERATOR_AVAILABLE_BALANCE_" not in patched_a
    assert "max(equity - margin" not in patched_a
    assert "9000" not in patched_a


def test_patch_file_writes_backup_atomically_and_is_idempotent(
    tmp_path: Path,
) -> None:
    module = _load_script()
    target = tmp_path / "read_api.py"
    source = _production_source()
    target.write_bytes(source)
    target.chmod(0o640)

    first = module.patch_file(target)
    backup = Path(first["backup"])
    first_bytes = target.read_bytes()

    assert first["status"] == "patched"
    assert first["patched_sha256"] == module.EXPECTED_PATCHED_SHA256
    assert backup.read_bytes() == source
    assert target.stat().st_mode & 0o777 == 0o640
    assert backup.stat().st_mode & 0o777 == 0o640

    second = module.patch_file(target)

    assert second["status"] == "already_patched"
    assert target.read_bytes() == first_bytes
    assert backup.read_bytes() == source


def test_check_only_reports_ready_without_writing(tmp_path: Path) -> None:
    module = _load_script()
    target = tmp_path / "read_api.py"
    source = _production_source()
    target.write_bytes(source)

    result = module.patch_file(target, check_only=True)

    assert result["status"] == "ready"
    assert target.read_bytes() == source
    assert not Path(result["backup"]).exists()


def test_patch_rejects_sha_drift_and_partial_marker(tmp_path: Path) -> None:
    module = _load_script()
    target = tmp_path / "read_api.py"
    target.write_bytes(_production_source() + b"\n")

    with pytest.raises(module.PatchError, match="SHA256 mismatch"):
        module.patch_file(target)

    target.write_text(
        module.PATCH_MARKER + "\n",
        encoding="utf-8",
    )
    with pytest.raises(module.PatchError, match="partial or unreviewed"):
        module.patch_file(target)


def test_patch_rejects_missing_or_duplicated_anchors() -> None:
    module = _load_script()
    source = _production_source().decode("utf-8")

    with pytest.raises(module.PatchError, match="operator accounts"):
        module._replace_exact(
            source.replace(module._OLD_ACCOUNTS, ""),
            module._OLD_ACCOUNTS,
            module._NEW_ACCOUNTS,
            "operator accounts",
        )
    with pytest.raises(module.PatchError, match="operator accounts"):
        module._replace_exact(
            source + "\n" + module._OLD_ACCOUNTS,
            module._OLD_ACCOUNTS,
            module._NEW_ACCOUNTS,
            "operator accounts",
        )


def test_watcher_routes_main_and_subaccounts_by_execution_id(
    tmp_path: Path,
) -> None:
    module = _load_script()
    namespace = _runtime_namespace(_patched_source(module))
    watcher_db = tmp_path / "watcher.db"
    _create_watcher_db(watcher_db)
    namespace["_WATCHER_TRADING_DB"] = str(watcher_db)

    assert namespace["_channel_risk_capital_multiplier"](
        "-1002136478186",
        "account-a",
    ) == pytest.approx(1.25)
    assert namespace["_channel_risk_capital_multiplier"](
        "-1002189417451",
        "account-c",
    ) == pytest.approx(2.0)
    assert namespace["_account_risk_capital_multiplier"](
        "account-b"
    ) == pytest.approx(1.5)
    assert namespace["_account_risk_capital_multiplier"](
        "account-d"
    ) == pytest.approx(3.0)

    with pytest.raises(HTTPException) as exc_info:
        namespace["_channel_risk_capital_multiplier"](
            "-1002189417451",
            "account-a",
        )
    assert exc_info.value.status_code == 409
    assert "conflicts" in exc_info.value.detail


def test_watcher_route_fails_closed_on_duplicate_execution_identity(
    tmp_path: Path,
) -> None:
    module = _load_script()
    namespace = _runtime_namespace(_patched_source(module))
    watcher_db = tmp_path / "watcher.db"
    _create_watcher_db(watcher_db)
    conn = sqlite3.connect(watcher_db)
    try:
        conn.execute(
            "INSERT INTO account_configs VALUES (?, ?, ?, ?)",
            ("duplicate-child", "account-c", 2.0, 1),
        )
        conn.commit()
    finally:
        conn.close()
    namespace["_WATCHER_TRADING_DB"] = str(watcher_db)

    with pytest.raises(HTTPException) as exc_info:
        namespace["_channel_risk_capital_multiplier"](
            "-1002189417451",
            "account-c",
        )

    assert exc_info.value.status_code == 503
    assert "must be unique" in exc_info.value.detail


def test_financial_state_reads_fresh_postgres_equity_and_available_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    namespace = _runtime_namespace(_patched_source(module))
    row = (
        5000.0,
        1200.0,
        1.0,
        "binance_fapi_account_v3",
        2.0,
    )

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def execute(self, query, params):
            assert "available_balance" in query
            assert "account_snapshot_source" in query
            assert "account_snapshot_fetched_at" in query
            assert params == ("account-c",)

        def fetchone(self):
            return row

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            return None

    namespace["psycopg2"] = SimpleNamespace(
        connect=lambda database_url: Connection()
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")

    state = namespace["_account_financial_state"]("account-c")

    assert state == {
        "real_equity": 5000.0,
        "available_balance": 1200.0,
    }


@pytest.mark.parametrize(
    "row",
    [
        (5000.0, None, 1.0, "binance_fapi_account_v3", 2.0),
        (5000.0, 1200.0, 61.0, "binance_fapi_account_v3", 2.0),
        (5000.0, 1200.0, -0.1, "binance_fapi_account_v3", 2.0),
        (5000.0, 1200.0, 1.0, "operator_hint", 2.0),
        (5000.0, 1200.0, 1.0, "binance_fapi_account_v3", None),
        (5000.0, 1200.0, 1.0, "binance_fapi_account_v3", 61.0),
        (5000.0, 1200.0, 1.0, "binance_fapi_account_v3", -0.1),
    ],
)
def test_financial_state_rejects_untrusted_or_stale_projection(
    monkeypatch: pytest.MonkeyPatch,
    row: tuple,
) -> None:
    module = _load_script()
    namespace = _runtime_namespace(_patched_source(module))

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def execute(self, query, params):
            return None

        def fetchone(self):
            return row

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            return None

    namespace["psycopg2"] = SimpleNamespace(
        connect=lambda database_url: Connection()
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setenv("OPERATOR_EQUITY_MAX_AGE_SECONDS", "3600")

    assert namespace["_account_financial_state"]("account-c") is False


def test_financial_state_has_no_static_environment_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    namespace = _runtime_namespace(_patched_source(module))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OPERATOR_EQUITY_ACCOUNT_C", "12345")
    monkeypatch.setenv("OPERATOR_AVAILABLE_BALANCE_ACCOUNT_C", "12345")

    assert namespace["_account_financial_state"]("account-c") is False


def test_sizing_uses_live_equity_times_multiplier_on_every_call() -> None:
    module = _load_script()
    namespace = _runtime_namespace(_patched_source(module))
    live_equity = {"value": 5000.0}
    namespace["_account_financial_state"] = lambda account_id: {
        "real_equity": live_equity["value"],
        "available_balance": live_equity["value"],
    }
    namespace["_symbol_risk_ratio"] = lambda symbol: 0.01
    checks = []

    initial = namespace["_size_open_order"](
        None,
        "BTCUSDT",
        "account-c",
        "long",
        "limit",
        100.0,
        None,
        None,
        90.0,
        10.0,
        _caps(),
        checks,
        1.7,
    )
    live_equity["value"] = 4500.0
    after_loss = namespace["_size_open_order"](
        None,
        "BTCUSDT",
        "account-c",
        "long",
        "limit",
        100.0,
        None,
        None,
        90.0,
        10.0,
        _caps(),
        [],
        1.7,
    )
    live_equity["value"] = 5500.0
    after_profit = namespace["_size_open_order"](
        None,
        "BTCUSDT",
        "account-c",
        "long",
        "limit",
        100.0,
        None,
        None,
        90.0,
        10.0,
        _caps(),
        [],
        1.7,
    )

    assert initial == pytest.approx(850.0)
    assert after_loss == pytest.approx(765.0)
    assert after_profit == pytest.approx(935.0)
    assert checks[0]["effective_equity"] == pytest.approx(8500.0)
    assert checks[0]["risk_capital_multiplier"] == pytest.approx(1.7)


@pytest.mark.parametrize(
    ("real_equity", "available_balance", "multiplier", "expected_detail"),
    [
        (50.0, 50.0, 200.0, "real_equity*leverage"),
        (5000.0, 50.0, 2.0, "available_balance*leverage"),
    ],
)
def test_sizing_preserves_both_real_funds_hard_gates(
    real_equity: float,
    available_balance: float,
    multiplier: float,
    expected_detail: str,
) -> None:
    module = _load_script()
    namespace = _runtime_namespace(_patched_source(module))
    namespace["_account_financial_state"] = lambda account_id: {
        "real_equity": real_equity,
        "available_balance": available_balance,
    }
    namespace["_symbol_risk_ratio"] = lambda symbol: 0.01

    with pytest.raises(HTTPException) as exc_info:
        namespace["_size_open_order"](
            600.0,
            "BTCUSDT",
            "account-c",
            "long",
            "limit",
            100.0,
            None,
            None,
            90.0,
            10.0,
            _caps(),
            [],
            multiplier,
        )

    assert exc_info.value.status_code == 400
    assert expected_detail in exc_info.value.detail


def test_sizing_rejects_explicit_notional_above_dynamic_auto() -> None:
    module = _load_script()
    namespace = _runtime_namespace(_patched_source(module))
    namespace["_account_financial_state"] = lambda account_id: {
        "real_equity": 5000.0,
        "available_balance": 1000.0,
    }
    namespace["_symbol_risk_ratio"] = lambda symbol: 0.01

    with pytest.raises(HTTPException) as exc_info:
        namespace["_size_open_order"](
            901.0,
            "BTCUSDT",
            "account-c",
            "long",
            "limit",
            100.0,
            None,
            None,
            90.0,
            10.0,
            _caps(),
            [],
            1.8,
        )

    assert exc_info.value.status_code == 400
    assert "dynamic auto sizing 900.0U" in exc_info.value.detail


@pytest.mark.parametrize("mark_price", [None, 0.0, float("nan")])
def test_market_sizing_fails_closed_without_usable_mark_price(
    mark_price: float | None,
) -> None:
    module = _load_script()
    namespace = _runtime_namespace(_patched_source(module))
    namespace["_account_financial_state"] = lambda account_id: {
        "real_equity": 5000.0,
        "available_balance": 1000.0,
    }
    namespace["_binance_mark_price"] = lambda symbol: mark_price

    with pytest.raises(HTTPException) as exc_info:
        namespace["_size_open_order"](
            1.0,
            "BTCUSDT",
            "account-c",
            "long",
            "market",
            None,
            None,
            None,
            None,
            10.0,
            _caps(),
            [],
            1.8,
        )

    assert exc_info.value.status_code == 503
    assert "sizing price unavailable for BTCUSDT" in exc_info.value.detail


def test_operator_open_uses_route_or_direct_account_multiplier() -> None:
    module = _load_script()
    patched = _patched_source(module)

    assert (
        "open_raw_channel not in "
        '("hermes-operator", "operator")' in patched
    )
    assert "_channel_risk_capital_multiplier(" in patched
    assert "_account_risk_capital_multiplier(account_id)" in patched
    assert "open_risk_capital_multiplier," in patched
    assert "raw_channel = open_raw_channel" in patched
    assert "for candidate in _OPERATOR_ACCOUNTS:" in patched
    assert "action in _OPERATOR_MANAGEMENT_ACTIONS" in patched
    assert '"cancel_order",' in patched
