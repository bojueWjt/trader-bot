from decimal import Decimal

import pytest
from fastapi import HTTPException

from conftest import read_api


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.executions.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row):
        self.cursor_instance = _Cursor(row)
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


class _FailingCursor(_Cursor):
    def execute(self, sql, params):
        raise RuntimeError("projection query failed")


class _FailingConnection(_Connection):
    def __init__(self):
        super().__init__(False)
        self.cursor_instance = _FailingCursor(False)


def test_account_equity_prefers_fresh_projection_over_env(monkeypatch):
    conn = _Connection(
        (
            Decimal("5329.25"),
            Decimal("5000.25"),
            "binance_fapi_account_v3",
            "2026-08-12T00:00:00Z",
            Decimal("12"),
        )
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setenv("OPERATOR_EQUITY_ACCOUNT_A", "100")
    monkeypatch.setattr(read_api.psycopg2, "connect", lambda _url: conn)

    equity = read_api._account_equity("account-a")

    assert equity == 5329.25
    assert conn.closed is True
    assert conn.cursor_instance.executions[0][1] == ("account-a",)


def test_account_equity_rejects_stale_projection_without_env_fallback(monkeypatch):
    conn = _Connection(
        (
            Decimal("5329.25"),
            Decimal("5000.25"),
            "binance_fapi_account_v3",
            "2026-08-12T00:00:00Z",
            Decimal("61"),
        )
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setenv("OPERATOR_EQUITY_ACCOUNT_A", "5328")
    monkeypatch.setenv("OPERATOR_EQUITY_MAX_AGE_SECONDS", "180")
    monkeypatch.setattr(read_api.psycopg2, "connect", lambda _url: conn)

    assert read_api._account_equity("account-a") is None


def test_account_equity_rejects_env_when_projection_is_missing(monkeypatch):
    conn = _Connection(False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setenv("OPERATOR_EQUITY_ACCOUNT_B", "1200.5")
    monkeypatch.setattr(read_api.psycopg2, "connect", lambda _url: conn)

    assert read_api._account_equity("account-b") is None


def test_account_equity_fails_closed_when_projection_store_errors(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setenv("OPERATOR_EQUITY_ACCOUNT_A", "5328")

    def fail_connect(_url):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(read_api.psycopg2, "connect", fail_connect)

    assert read_api._account_equity("account-a") is None


def test_account_equity_fails_closed_when_projection_query_errors(monkeypatch):
    conn = _FailingConnection()
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setenv("OPERATOR_EQUITY_ACCOUNT_A", "5328")
    monkeypatch.setattr(read_api.psycopg2, "connect", lambda _url: conn)

    assert read_api._account_equity("account-a") is None
    assert conn.closed is True


def test_account_equity_rejects_non_finite_values(monkeypatch):
    conn = _Connection(
        (
            Decimal("NaN"),
            Decimal("5000.25"),
            "binance_fapi_account_v3",
            "2026-08-12T00:00:00Z",
            Decimal("12"),
        )
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setenv("OPERATOR_EQUITY_ACCOUNT_A", "inf")
    monkeypatch.setenv("OPERATOR_EQUITY_MAX_AGE_SECONDS", "inf")
    monkeypatch.setattr(read_api.psycopg2, "connect", lambda _url: conn)

    assert read_api._account_equity("account-a") is None


def test_account_equity_rejects_missing_available_balance(monkeypatch):
    conn = _Connection(
        (
            Decimal("5329.25"),
            None,
            "binance_fapi_account_v3",
            "2026-08-12T00:00:00Z",
            Decimal("12"),
        )
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setattr(read_api.psycopg2, "connect", lambda _url: conn)

    assert read_api._account_equity("account-a") is None


def test_operator_account_registry_defaults_to_four_accounts(
    monkeypatch,
):
    monkeypatch.delenv("OPERATOR_ACCOUNT_REGISTRY_JSON", raising=False)

    assert read_api._operator_account_registry() == {
        "account-a": {},
        "account-b": {},
        "account-c": {},
        "account-d": {},
    }


def test_operator_account_registry_supports_four_configured_accounts(monkeypatch):
    monkeypatch.setenv(
        "OPERATOR_ACCOUNT_REGISTRY_JSON",
        (
            '{"account-a":{},'
            '"account-b":{},'
            '"account-c":{},'
            '"account-d":{}}'
        ),
    )

    assert read_api._operator_accounts() == (
        "account-a",
        "account-b",
        "account-c",
        "account-d",
    )


def test_operator_account_registry_rejects_static_effective_equity(monkeypatch):
    monkeypatch.setenv(
        "OPERATOR_ACCOUNT_REGISTRY_JSON",
        '{"account-a":{"effective_equity":9000}}',
    )

    with pytest.raises(HTTPException) as exc:
        read_api._operator_account_registry()

    assert exc.value.status_code == 503
    assert "effective_equity is unsupported" in exc.value.detail


def test_watcher_trading_db_path_defaults_to_legacy_volume():
    assert (
        read_api._resolve_watcher_trading_db_path({})
        == read_api._DEFAULT_WATCHER_TRADING_DB
    )


def test_watcher_trading_db_path_accepts_matching_aliases():
    env = {
        "TRADER_TRADING_DB_PATH": "/data/watcher-trading.db",
        "WATCHER_TRADING_DB": "/data/watcher-trading.db",
        "TRADING_DB_PATH": "/data/watcher-trading.db",
    }

    assert (
        read_api._resolve_watcher_trading_db_path(env)
        == "/data/watcher-trading.db"
    )


def test_watcher_trading_db_path_accepts_legacy_only_alias():
    assert (
        read_api._resolve_watcher_trading_db_path(
            {"TRADING_DB_PATH": "/data/legacy.db"}
        )
        == "/data/legacy.db"
    )


def test_watcher_trading_db_path_rejects_conflicting_aliases():
    env = {
        "TRADER_TRADING_DB_PATH": "/data/a.db",
        "WATCHER_TRADING_DB": "/data/b.db",
    }

    with pytest.raises(RuntimeError, match="conflicting trading DB path"):
        read_api._resolve_watcher_trading_db_path(env)


def test_open_sizing_uses_effective_equity_and_records_real_equity(
    monkeypatch,
):
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
        {
            "max_notional": None,
            "max_leverage": 10.0,
            "max_risk_fraction": 0.06,
            "no_sl_equity_fraction": 0.2,
        },
        checks,
        1.8,
    )

    assert notional == pytest.approx(900.0)
    assert checks[0] == {
        "name": "account_equity_basis",
        "passed": True,
        "real_equity": 5000.0,
        "available_balance": 1000.0,
        "effective_equity": 9000.0,
        "risk_capital_multiplier": 1.8,
    }
    assert checks[1]["effective_equity"] == 9000.0
    assert checks[1]["real_equity"] == 5000.0


def test_open_market_stop_loss_sizing_fails_closed_without_mark_price(
    monkeypatch,
):
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
            {
                "max_notional": None,
                "max_leverage": 10.0,
                "max_risk_fraction": 0.06,
                "no_sl_equity_fraction": 0.2,
            },
            [],
            1.8,
        )

    assert exc.value.status_code == 503
    assert "live mark price unavailable" in exc.value.detail


@pytest.mark.parametrize(
    ("multiplier", "detail"),
    [
        (None, "multiplier is unavailable"),
        (False, "multiplier is invalid"),
        (0.0, "multiplier is invalid"),
        (float("nan"), "multiplier is invalid"),
        (float("inf"), "multiplier is invalid"),
    ],
)
def test_open_sizing_fails_closed_without_valid_configured_multiplier(
    monkeypatch,
    multiplier,
    detail,
):
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
            {
                "max_notional": None,
                "max_leverage": 10.0,
                "max_risk_fraction": 0.06,
                "no_sl_equity_fraction": 0.2,
            },
            [],
            multiplier,
        )

    assert exc.value.status_code == 503
    assert detail in exc.value.detail


def test_open_sizing_keeps_available_balance_as_hard_gate(monkeypatch):
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
            {
                "max_notional": None,
                "max_leverage": 10.0,
                "max_risk_fraction": 0.06,
                "no_sl_equity_fraction": 0.2,
            },
            [],
            1.8,
        )

    assert exc.value.status_code == 400
    assert "available_balance*leverage" in exc.value.detail


def test_open_sizing_rejects_explicit_notional_above_dynamic_auto(
    monkeypatch,
):
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
            {
                "max_notional": None,
                "max_leverage": 10.0,
                "max_risk_fraction": 0.06,
                "no_sl_equity_fraction": 0.2,
            },
            [],
            1.8,
        )

    assert exc.value.status_code == 400
    assert "dynamic auto sizing 900.0U" in exc.value.detail


def test_open_sizing_allows_explicit_notional_below_dynamic_auto(
    monkeypatch,
):
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
        {
            "max_notional": None,
            "max_leverage": 10.0,
            "max_risk_fraction": 0.06,
            "no_sl_equity_fraction": 0.2,
        },
        checks,
        1.8,
    )

    assert notional == 450.0
    assert checks[1]["auto_sized"] is False


def test_open_explicit_notional_limit_tracks_profit_and_loss(
    monkeypatch,
):
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
            850.0,
            "BTCUSDT",
            "account-c",
            "long",
            "limit",
            100.0,
            None,
            None,
            90.0,
            10.0,
            {
                "max_notional": None,
                "max_leverage": 10.0,
                "max_risk_fraction": 0.06,
                "no_sl_equity_fraction": 0.2,
            },
            [],
            1.8,
        )

    assert size_explicit() == 850.0

    current_equity["value"] = 4500.0
    with pytest.raises(HTTPException) as loss_exc:
        size_explicit()
    assert "dynamic auto sizing 810.0U" in loss_exc.value.detail

    current_equity["value"] = 5500.0
    assert size_explicit() == 850.0


def test_account_projection_hint_rejects_empty_account_state():
    event = {
        "event_id": "event-1",
        "account_id": "account-a",
        "ts_event": "2026-07-29T12:00:00Z",
    }

    assert read_api._account_projection_hint(event, {"account": {}}) is None


def test_account_projection_hint_normalizes_complete_monetary_state():
    event = {
        "event_id": "event-1",
        "account_id": "account-b",
        "ts_event": "2026-07-29T12:00:00Z",
    }
    hints = {
        "account": {
            "currency": "USDT",
            "balance": "1200.5",
            "locked": "200.25",
            "free": "1000.25",
        }
    }

    assert read_api._account_projection_hint(event, hints) == {
        "currency": "USDT",
        "balance": "1200.5",
        "locked": "200.25",
        "free": "1000.25",
        "account_id": "account-b",
        "equity": 1200.5,
        "margin": 200.25,
        "available_balance": 1000.25,
        "event_id": "event-1",
        "last_execution_event_at": "2026-07-29T12:00:00Z",
    }
