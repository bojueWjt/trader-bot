from decimal import Decimal

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
    conn = _Connection((Decimal("5329.25"), Decimal("12")))
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setenv("OPERATOR_EQUITY_ACCOUNT_A", "100")
    monkeypatch.setattr(read_api.psycopg2, "connect", lambda _url: conn)

    equity = read_api._account_equity("account-a")

    assert equity == 5329.25
    assert conn.closed is True
    assert conn.cursor_instance.executions[0][1] == ("account-a",)


def test_account_equity_rejects_stale_projection_without_env_fallback(monkeypatch):
    conn = _Connection((Decimal("5329.25"), Decimal("181")))
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setenv("OPERATOR_EQUITY_ACCOUNT_A", "5328")
    monkeypatch.setenv("OPERATOR_EQUITY_MAX_AGE_SECONDS", "180")
    monkeypatch.setattr(read_api.psycopg2, "connect", lambda _url: conn)

    assert read_api._account_equity("account-a") is None


def test_account_equity_uses_env_only_before_projection_exists(monkeypatch):
    conn = _Connection(False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setenv("OPERATOR_EQUITY_ACCOUNT_B", "1200.5")
    monkeypatch.setattr(read_api.psycopg2, "connect", lambda _url: conn)

    assert read_api._account_equity("account-b") == 1200.5


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
    conn = _Connection((Decimal("NaN"), Decimal("12")))
    monkeypatch.setenv("DATABASE_URL", "postgresql://projection")
    monkeypatch.setenv("OPERATOR_EQUITY_ACCOUNT_A", "inf")
    monkeypatch.setenv("OPERATOR_EQUITY_MAX_AGE_SECONDS", "inf")
    monkeypatch.setattr(read_api.psycopg2, "connect", lambda _url: conn)

    assert read_api._account_equity("account-a") is None


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
