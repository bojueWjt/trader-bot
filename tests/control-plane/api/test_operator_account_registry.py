from __future__ import annotations

import inspect
import json
import sqlite3
from decimal import Decimal

import pytest
from fastapi import HTTPException

import read_api


def _caps() -> dict:
    return {
        "max_notional": None,
        "max_leverage": 10.0,
        "max_risk_fraction": 0.06,
        "no_sl_equity_fraction": 0.2,
    }


class _FinancialStateCursor:
    def __init__(self, row) -> None:
        self.row = row
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params) -> None:
        self.executions.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row


class _FinancialStateConnection:
    def __init__(self, row) -> None:
        self.cursor_instance = _FinancialStateCursor(row)
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


def _create_watcher_risk_db(path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE account_configs (
                account_id TEXT PRIMARY KEY,
                execution_account_id TEXT NOT NULL,
                risk_capital_addon REAL NOT NULL,
                account_type TEXT NOT NULL,
                parent_account_id TEXT NOT NULL,
                is_enabled INTEGER NOT NULL
            );
            CREATE TABLE channel_routing (
                channel_id TEXT PRIMARY KEY,
                target_account_id TEXT NOT NULL
            );
            INSERT INTO account_configs (
                account_id,
                execution_account_id,
                risk_capital_addon,
                account_type,
                parent_account_id,
                is_enabled
            ) VALUES (
                'credential-sub-c',
                'account-c',
                6000.0,
                'subaccount',
                'credential-main-a',
                1
            );
            INSERT INTO account_configs (
                account_id,
                execution_account_id,
                risk_capital_addon,
                account_type,
                parent_account_id,
                is_enabled
            ) VALUES (
                'credential-main-a',
                'account-a',
                0.0,
                'main',
                '',
                1
            );
            INSERT INTO channel_routing (
                channel_id,
                target_account_id
            ) VALUES (
                '-1002189417451',
                'credential-sub-c'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_registry_defaults_to_four_accounts(monkeypatch) -> None:
    monkeypatch.delenv("OPERATOR_ACCOUNT_REGISTRY_JSON", raising=False)
    assert read_api._operator_accounts() == (
        "account-a",
        "account-b",
        "account-c",
        "account-d",
    )
    assert read_api._operator_account_registry() == {
        account_id: {}
        for account_id in (
            "account-a",
            "account-b",
            "account-c",
            "account-d",
        )
    }

    monkeypatch.setenv(
        "OPERATOR_ACCOUNT_REGISTRY_JSON",
        json.dumps(
            {
                account_id: {}
                for account_id in (
                    "account-a",
                    "account-b",
                    "account-c",
                    "account-d",
                )
            }
        ),
    )
    assert read_api._operator_accounts() == (
        "account-a",
        "account-b",
        "account-c",
        "account-d",
    )


def test_registry_rejects_static_effective_equity(monkeypatch) -> None:
    monkeypatch.setenv(
        "OPERATOR_ACCOUNT_REGISTRY_JSON",
        '{"account-a":{"effective_equity":9000}}',
    )

    with pytest.raises(HTTPException) as exc:
        read_api._operator_account_registry()

    assert exc.value.status_code == 503
    assert "effective_equity is unsupported" in exc.value.detail


def test_watcher_trading_db_path_defaults_to_legacy_volume() -> None:
    assert (
        read_api._resolve_watcher_trading_db_path({})
        == read_api._DEFAULT_WATCHER_TRADING_DB
    )


def test_watcher_trading_db_path_accepts_matching_aliases() -> None:
    env = {
        "TRADER_TRADING_DB_PATH": "/data/watcher-trading.db",
        "WATCHER_TRADING_DB": "/data/watcher-trading.db",
        "TRADING_DB_PATH": "/data/watcher-trading.db",
    }

    assert (
        read_api._resolve_watcher_trading_db_path(env)
        == "/data/watcher-trading.db"
    )


def test_watcher_trading_db_path_accepts_legacy_only_alias() -> None:
    assert (
        read_api._resolve_watcher_trading_db_path(
            {"TRADING_DB_PATH": "/data/legacy.db"}
        )
        == "/data/legacy.db"
    )


def test_watcher_trading_db_path_rejects_conflicting_aliases() -> None:
    env = {
        "TRADER_TRADING_DB_PATH": "/data/a.db",
        "WATCHER_TRADING_DB": "/data/b.db",
    }

    with pytest.raises(RuntimeError, match="conflicting trading DB path"):
        read_api._resolve_watcher_trading_db_path(env)


def test_watcher_addon_resolves_by_channel_and_execution_account(
    monkeypatch,
    tmp_path,
) -> None:
    db_path = tmp_path / "watcher-trading.db"
    _create_watcher_risk_db(db_path)
    monkeypatch.setattr(read_api, "_WATCHER_TRADING_DB", str(db_path))

    assert read_api._channel_risk_capital_addon(
        "-1002189417451",
        "account-c",
    ) == pytest.approx(6000.0)
    assert read_api._account_risk_capital_addon(
        "account-c",
    ) == pytest.approx(6000.0)

    with pytest.raises(HTTPException) as exc:
        read_api._channel_risk_capital_addon(
            "-1002189417451",
            "account-d",
        )

    assert exc.value.status_code == 409
    assert "conflicts" in exc.value.detail


@pytest.mark.parametrize(
    "missing_column",
    [
        "account_type",
        "parent_account_id",
        "execution_account_id",
        "risk_capital_addon",
    ],
)
def test_watcher_addon_requires_complete_dynamic_schema(
    monkeypatch,
    tmp_path,
    missing_column,
) -> None:
    db_path = tmp_path / f"missing-{missing_column}.db"
    columns = ["account_id TEXT PRIMARY KEY"]
    if missing_column != "account_type":
        columns.append("account_type TEXT NOT NULL")
    if missing_column != "parent_account_id":
        columns.append("parent_account_id TEXT NOT NULL")
    if missing_column != "execution_account_id":
        columns.append("execution_account_id TEXT NOT NULL")
    if missing_column != "risk_capital_addon":
        columns.append("risk_capital_addon REAL NOT NULL")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            f"""
            CREATE TABLE account_configs (
                {", ".join(columns)}
            );
            CREATE TABLE channel_routing (
                channel_id TEXT PRIMARY KEY,
                target_account_id TEXT NOT NULL
            );
            """
        )
        insert_columns = ["account_id"]
        insert_values = ["credential-a"]
        if missing_column != "account_type":
            insert_columns.append("account_type")
            insert_values.append("main")
        if missing_column != "parent_account_id":
            insert_columns.append("parent_account_id")
            insert_values.append("")
        if missing_column != "execution_account_id":
            insert_columns.append("execution_account_id")
            insert_values.append("account-a")
        if missing_column != "risk_capital_addon":
            insert_columns.append("risk_capital_addon")
            insert_values.append(6000.0)
        placeholders = ", ".join("?" for _ in insert_values)
        conn.execute(
            "INSERT INTO account_configs "
            f"({', '.join(insert_columns)}) VALUES ({placeholders})",
            insert_values,
        )
        conn.execute(
            "INSERT INTO channel_routing (channel_id, target_account_id) "
            "VALUES (?, ?)",
            ("-1002136478186", "credential-a"),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(read_api, "_WATCHER_TRADING_DB", str(db_path))

    with pytest.raises(HTTPException) as channel_exc:
        read_api._channel_risk_capital_addon(
            "-1002136478186",
            "account-a",
        )
    with pytest.raises(HTTPException) as account_exc:
        read_api._account_risk_capital_addon("account-a")

    assert channel_exc.value.status_code == 503
    assert "schema is unavailable" in channel_exc.value.detail
    assert account_exc.value.status_code == 503
    assert "schema is unavailable" in account_exc.value.detail


def test_watcher_addon_rejects_duplicate_execution_identity(
    monkeypatch,
    tmp_path,
) -> None:
    db_path = tmp_path / "duplicate-execution.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE account_configs (
                account_id TEXT PRIMARY KEY,
                account_type TEXT NOT NULL,
                parent_account_id TEXT NOT NULL,
                execution_account_id TEXT NOT NULL,
                risk_capital_addon REAL NOT NULL
            );
            CREATE TABLE channel_routing (
                channel_id TEXT PRIMARY KEY,
                target_account_id TEXT NOT NULL
            );
            INSERT INTO account_configs VALUES (
                'credential-a',
                'main',
                '',
                'account-a',
                6000.0
            );
            INSERT INTO account_configs VALUES (
                'credential-b',
                'main',
                '',
                'account-a',
                5000.0
            );
            INSERT INTO channel_routing VALUES (
                '-1002136478186',
                'credential-a'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(read_api, "_WATCHER_TRADING_DB", str(db_path))

    with pytest.raises(HTTPException) as channel_exc:
        read_api._channel_risk_capital_addon(
            "-1002136478186",
            "account-a",
        )
    with pytest.raises(HTTPException) as account_exc:
        read_api._account_risk_capital_addon("account-a")

    assert channel_exc.value.status_code == 503
    assert "must be unique" in channel_exc.value.detail
    assert account_exc.value.status_code == 503
    assert "exactly one risk configuration" in account_exc.value.detail


def test_watcher_addon_rejects_orphan_subaccount(
    monkeypatch,
    tmp_path,
) -> None:
    db_path = tmp_path / "orphan-subaccount.db"
    _create_watcher_risk_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM account_configs WHERE account_id='credential-main-a'"
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(read_api, "_WATCHER_TRADING_DB", str(db_path))

    with pytest.raises(HTTPException) as channel_exc:
        read_api._channel_risk_capital_addon(
            "-1002189417451",
            "account-c",
        )
    with pytest.raises(HTTPException) as account_exc:
        read_api._account_risk_capital_addon("account-c")

    assert channel_exc.value.status_code == 503
    assert "parent must resolve" in channel_exc.value.detail
    assert account_exc.value.status_code == 503
    assert "parent must resolve" in account_exc.value.detail


@pytest.mark.parametrize(
    ("addon", "enabled", "detail"),
    [
        (-1.0, 1, "addon is invalid"),
        (6000.0, 0, "disabled"),
    ],
)
def test_watcher_addon_rejects_invalid_or_disabled_config(
    monkeypatch,
    tmp_path,
    addon,
    enabled,
    detail,
) -> None:
    db_path = tmp_path / f"invalid-{addon}-{enabled}.db"
    _create_watcher_risk_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE account_configs "
            "SET risk_capital_addon=?, is_enabled=?",
            (addon, enabled),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(read_api, "_WATCHER_TRADING_DB", str(db_path))

    with pytest.raises(HTTPException) as channel_exc:
        read_api._channel_risk_capital_addon(
            "-1002189417451",
            "account-c",
        )
    with pytest.raises(HTTPException) as account_exc:
        read_api._account_risk_capital_addon("account-c")

    assert channel_exc.value.status_code == 503
    assert detail in channel_exc.value.detail
    assert account_exc.value.status_code == 503
    assert detail in account_exc.value.detail


def test_financial_state_requires_database_and_ignores_static_equity(
    monkeypatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OPERATOR_EQUITY_ACCOUNT_C", "9000")
    monkeypatch.setenv("OPERATOR_AVAILABLE_BALANCE_ACCOUNT_C", "9000")

    assert read_api._account_financial_state("account-c") is False


def test_financial_state_uses_fresh_database_equity_and_available_balance(
    monkeypatch,
) -> None:
    conn = _FinancialStateConnection(
        (
            Decimal("5000.25"),
            Decimal("1250.75"),
            "binance_fapi_account_v3",
            "2026-08-12T00:00:00Z",
            Decimal("45"),
        )
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.delenv("OPERATOR_EQUITY_MAX_AGE_SECONDS", raising=False)
    monkeypatch.setattr(read_api, "_database_connection", lambda _url: conn)

    state = read_api._account_financial_state("account-c")

    assert state == {
        "real_equity": 5000.25,
        "available_balance": 1250.75,
    }
    assert conn.closed is True
    assert conn.cursor_instance.executions[0][1] == ("account-c",)


def test_financial_state_caps_freshness_at_sixty_seconds(
    monkeypatch,
) -> None:
    conn = _FinancialStateConnection(
        (
            Decimal("5000.25"),
            Decimal("1250.75"),
            "binance_fapi_account_v3",
            "2026-08-12T00:00:00Z",
            Decimal("61"),
        )
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setenv("OPERATOR_EQUITY_MAX_AGE_SECONDS", "180")
    monkeypatch.setattr(read_api, "_database_connection", lambda _url: conn)

    assert read_api._account_financial_state("account-c") is False


def test_financial_state_rejects_missing_available_balance(
    monkeypatch,
) -> None:
    conn = _FinancialStateConnection(
        (
            Decimal("5000.25"),
            None,
            "binance_fapi_account_v3",
            "2026-08-12T00:00:00Z",
            Decimal("45"),
        )
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setattr(read_api, "_database_connection", lambda _url: conn)

    assert read_api._account_financial_state("account-c") is False


@pytest.mark.parametrize(
    ("source", "fetched_at", "age_seconds"),
    [
        ("execution_event", "2026-08-12T00:00:00Z", Decimal("1")),
        ("binance_fapi_account_v3", None, None),
        ("binance_fapi_account_v3", "invalid", None),
        ("binance_fapi_account_v3", "2026-08-12T00:00:00Z", Decimal("-1")),
    ],
)
def test_financial_state_requires_fresh_binance_account_snapshot(
    monkeypatch,
    source,
    fetched_at,
    age_seconds,
) -> None:
    conn = _FinancialStateConnection(
        (
            Decimal("5000.25"),
            Decimal("1250.75"),
            source,
            fetched_at,
            age_seconds,
        )
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setattr(read_api, "_database_connection", lambda _url: conn)

    assert read_api._account_financial_state("account-c") is False


def test_sizing_adds_fixed_risk_capital_addon(
    monkeypatch,
) -> None:
    current_equity = {"value": 3000.0}

    def financial_state(_account_id: str) -> dict[str, float]:
        return {
            "real_equity": current_equity["value"],
            "available_balance": 3000.0,
        }

    monkeypatch.setattr(read_api, "_account_financial_state", financial_state)
    monkeypatch.setattr(read_api, "_symbol_risk_ratio", lambda _symbol: 0.01)
    addon = 6000.0

    results = []
    for real_equity in (3000.0, 4000.0):
        current_equity["value"] = real_equity
        checks = []
        notional = read_api._size_open_order(
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
            addon,
        )
        results.append((notional, checks[0]))

    initial, after_profit = results
    assert initial[0] == pytest.approx(900.0)
    assert after_profit[0] == pytest.approx(1000.0)
    assert initial[1]["real_equity"] == 3000.0
    assert initial[1]["effective_equity"] == pytest.approx(9000.0)
    assert after_profit[1]["real_equity"] == 4000.0
    assert after_profit[1]["effective_equity"] == pytest.approx(10000.0)
    assert {
        check["risk_capital_addon"]
        for _, check in results
    } == {addon}
    assert initial[1]["risk_capital_multiplier"] == pytest.approx(3.0)
    assert after_profit[1]["risk_capital_multiplier"] == pytest.approx(2.5)

    sizing_source = inspect.getsource(read_api._size_open_order)
    assert "9000" not in sizing_source
    financial_state_source = inspect.getsource(
        read_api._account_financial_state
    )
    assert "9000" not in financial_state_source


def test_market_stop_loss_sizing_fails_closed_without_live_mark_price(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        read_api,
        "_account_financial_state",
        lambda _account_id: {
            "real_equity": 5000.0,
            "available_balance": 1000.0,
        },
    )
    monkeypatch.setattr(read_api, "_binance_mark_price", lambda _symbol: None)

    with pytest.raises(HTTPException) as exc:
        read_api._size_open_order(
            100.0,
            "BTCUSDT",
            "account-c",
            "long",
            "market",
            None,
            None,
            None,
            90.0,
            10.0,
            _caps(),
            [],
            4000.0,
        )

    assert exc.value.status_code == 503
    assert "live mark price unavailable" in exc.value.detail


@pytest.mark.parametrize(
    ("addon", "detail"),
    [
        (None, "addon is unavailable"),
        (False, "addon is invalid"),
        (-1.0, "addon is invalid"),
        (float("nan"), "addon is invalid"),
        (float("inf"), "addon is invalid"),
    ],
)
def test_sizing_fails_closed_without_valid_configured_addon(
    monkeypatch,
    addon,
    detail,
) -> None:
    monkeypatch.setattr(
        read_api,
        "_account_financial_state",
        lambda _account_id: {
            "real_equity": 5000.0,
            "available_balance": 1000.0,
        },
    )

    with pytest.raises(HTTPException) as exc:
        read_api._size_open_order(
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
            addon,
        )

    assert exc.value.status_code == 503
    assert detail in exc.value.detail


def test_sizing_accepts_zero_risk_capital_addon(monkeypatch) -> None:
    monkeypatch.setattr(
        read_api,
        "_account_financial_state",
        lambda _account_id: {
            "real_equity": 3000.0,
            "available_balance": 3000.0,
        },
    )
    monkeypatch.setattr(read_api, "_symbol_risk_ratio", lambda _symbol: 0.01)
    checks = []

    notional = read_api._size_open_order(
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
        0.0,
    )

    assert notional == pytest.approx(300.0)
    assert checks[0]["risk_capital_addon"] == 0.0
    assert checks[0]["risk_capital_multiplier"] == pytest.approx(1.0)


def test_sizing_rejects_non_positive_effective_equity(monkeypatch) -> None:
    monkeypatch.setattr(
        read_api,
        "_account_financial_state",
        lambda _account_id: {
            "real_equity": -100.0,
            "available_balance": 100.0,
        },
    )

    with pytest.raises(HTTPException) as exc:
        read_api._size_open_order(
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
            0.0,
        )

    assert exc.value.status_code == 503
    assert exc.value.detail == "account effective equity is not positive"


def test_sizing_rejects_explicit_notional_above_dynamic_auto(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        read_api,
        "_account_financial_state",
        lambda _account_id: {
            "real_equity": 5000.0,
            "available_balance": 1000.0,
        },
    )
    monkeypatch.setattr(read_api, "_symbol_risk_ratio", lambda _symbol: 0.01)

    with pytest.raises(HTTPException) as exc:
        read_api._size_open_order(
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
            4000.0,
        )

    assert exc.value.status_code == 400
    assert "dynamic auto sizing 900.0U" in exc.value.detail


def test_sizing_allows_explicit_notional_below_dynamic_auto(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        read_api,
        "_account_financial_state",
        lambda _account_id: {
            "real_equity": 5000.0,
            "available_balance": 1000.0,
        },
    )
    monkeypatch.setattr(read_api, "_symbol_risk_ratio", lambda _symbol: 0.01)
    checks = []

    notional = read_api._size_open_order(
        450.0,
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
        4000.0,
    )

    assert notional == 450.0
    assert checks[1]["auto_sized"] is False


def test_explicit_notional_limit_tracks_profit_and_loss(
    monkeypatch,
) -> None:
    current_equity = {"value": 5000.0}

    def financial_state(_account_id: str) -> dict[str, float]:
        return {
            "real_equity": current_equity["value"],
            "available_balance": 1000.0,
        }

    monkeypatch.setattr(read_api, "_account_financial_state", financial_state)
    monkeypatch.setattr(read_api, "_symbol_risk_ratio", lambda _symbol: 0.01)

    def size_explicit() -> float:
        return read_api._size_open_order(
            875.0,
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
            4000.0,
        )

    assert size_explicit() == 875.0

    current_equity["value"] = 4500.0
    with pytest.raises(HTTPException) as loss_exc:
        size_explicit()
    assert "dynamic auto sizing 850.0U" in loss_exc.value.detail

    current_equity["value"] = 5500.0
    assert size_explicit() == 875.0


def test_canary_explicit_notional_override_keeps_real_funds_gate(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        read_api,
        "_account_financial_state",
        lambda _account_id: {
            "real_equity": 100.0,
            "available_balance": 1.0,
        },
    )
    monkeypatch.setattr(read_api, "_symbol_risk_ratio", lambda _symbol: 0.01)
    caps = _caps()
    caps["_canary_explicit_notional_override"] = True

    with pytest.raises(HTTPException) as exc:
        read_api._size_open_order(
            12.0,
            "BTCUSDT",
            "account-a",
            "long",
            "limit",
            100.0,
            None,
            None,
            90.0,
            10.0,
            caps,
            [],
            0.0,
        )

    assert exc.value.status_code == 400
    assert "available_balance*leverage" in exc.value.detail


def test_sizing_keeps_real_available_balance_as_hard_gate(monkeypatch) -> None:
    monkeypatch.setattr(
        read_api,
        "_account_financial_state",
        lambda _account_id: {
            "real_equity": 5000.0,
            "available_balance": 50.0,
        },
    )
    monkeypatch.setattr(read_api, "_symbol_risk_ratio", lambda _symbol: 0.01)

    with pytest.raises(HTTPException) as exc:
        read_api._size_open_order(
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
            4000.0,
        )

    assert exc.value.status_code == 400
    assert "available_balance*leverage" in exc.value.detail


def test_entry_ref_resolves_canonical_target_position(monkeypatch) -> None:
    monkeypatch.setattr(
        read_api,
        "_attribution_intent",
        lambda *_args: (
            "11111111-1111-1111-1111-111111111111",
            "open_position",
            "BTCUSDT",
            "account-c",
            {"side": "long"},
            "-1002136478186",
            "tg-sig-c1002136478186-m5026",
        ),
    )

    event, hard_error, target_position_id = read_api._resolve_attribution(
        "postgresql://unused",
        "close_position",
        "BTCUSDT",
        "account-c",
        "-1002136478186",
        "tg-sig-c1002136478186-m5026",
        "long",
    )

    assert hard_error is False
    assert event["resolution"] == "intent"
    assert target_position_id == "BTCUSDT-PERP.BINANCE-LONG"


def test_channel_rebind_keeps_historical_management_on_entry_account(
    monkeypatch,
) -> None:
    entry = (
        "11111111-1111-1111-1111-111111111111",
        "open_position",
        "BTCUSDT",
        "account-a",
        {"side": "long"},
        "-1002136478186",
        "tg-sig-c1002136478186-m5026",
    )
    monkeypatch.setattr(
        read_api,
        "_attribution_intent",
        lambda *_args: entry,
    )

    normalized_account = read_api._channel_management_entry_account(
        "postgresql://unused",
        requested_account_id="account-b",
        symbol="BTCUSDT",
        channel="-1002136478186",
        entry_ref="tg-sig-c1002136478186-m5026",
        position_side="long",
    )
    assert normalized_account == "account-a"

    event, hard_error, target_position_id = read_api._resolve_attribution(
        "postgresql://unused",
        "close_position",
        "BTCUSDT",
        normalized_account,
        "-1002136478186",
        "tg-sig-c1002136478186-m5026",
        "long",
    )
    assert hard_error is False
    assert event["channel_match"] is True
    assert target_position_id == "BTCUSDT-PERP.BINANCE-LONG"

    mismatched_account = read_api._channel_management_entry_account(
        "postgresql://unused",
        requested_account_id="account-b",
        symbol="BTCUSDT",
        channel="-1002198013097",
        entry_ref="tg-sig-c1002136478186-m5026",
        position_side="long",
    )
    assert mismatched_account == "account-b"
    mismatched_event, mismatched_error, _target = (
        read_api._resolve_attribution(
            "postgresql://unused",
            "close_position",
            "BTCUSDT",
            mismatched_account,
            "-1002198013097",
            "tg-sig-c1002136478186-m5026",
            "long",
        )
    )
    assert "account-a does not match request account account-b" in mismatched_error
    assert mismatched_event["channel_match"] is False


@pytest.mark.parametrize(
    ("field", "first_value", "second_value"),
    [
        ("quantity", "0.01", "0.02"),
        ("stop_loss", 60000.0, 61000.0),
        (
            "take_profits",
            [{"price": 70000.0, "quantity": 0.01}],
            [{"price": 71000.0, "quantity": 0.01}],
        ),
    ],
)
def test_management_semantic_digest_covers_mutating_fields(
    field,
    first_value,
    second_value,
) -> None:
    base_plan = {
        "position_side": "long",
        field: first_value,
    }
    changed_plan = {
        "position_side": "long",
        field: second_value,
    }
    common = {
        "action": "partial_close",
        "account_id": "account-c",
        "symbol": "BTCUSDT",
        "position_side": "long",
        "client_ref": "management-operation-1",
        "target_position_id": "BTCUSDT-PERP.BINANCE-LONG",
        "body": {},
        "valid_seconds": 900,
    }

    first = read_api._operator_request_semantics(
        order_plan=base_plan,
        **common,
    )
    second = read_api._operator_request_semantics(
        order_plan=changed_plan,
        **common,
    )

    assert first["sha256"] != second["sha256"]
