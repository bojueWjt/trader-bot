from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

MIRROR_ROOT = Path(__file__).resolve().parents[1]
RECORDER_PATH = MIRROR_ROOT / "tools" / "exchange_state_recorder.py"


def _load_recorder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("exchange_state_recorder", RECORDER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingCursor:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def execute(self, sql, params) -> None:
        self.connection.events.append(("execute", sql, params))


class RecordingConnection:
    def __init__(self) -> None:
        self.events = []

    def cursor(self):
        return RecordingCursor(self)

    def commit(self) -> None:
        self.events.append(("commit",))

    def rollback(self) -> None:
        self.events.append(("rollback",))


class FailingSecondExecuteCursor(RecordingCursor):
    def execute(self, sql, params) -> None:
        super().execute(sql, params)
        execute_count = sum(event[0] == "execute" for event in self.connection.events)
        if execute_count == 2:
            raise RuntimeError("account projection write failed")


class FailingSecondExecuteConnection(RecordingConnection):
    def cursor(self):
        return FailingSecondExecuteCursor(self)


def test_snapshot_account_preserves_position_risk_mark_price(monkeypatch) -> None:
    recorder = _load_recorder()
    responses = {
        "/fapi/v3/account": {
            "totalMarginBalance": "1000.00",
            "totalInitialMargin": "100.00",
            "availableBalance": "900.00",
        },
        "/fapi/v2/positionRisk": [
            {
                "symbol": "ATOMUSDT",
                "positionAmt": "12.5",
                "entryPrice": "4.20",
                "markPrice": "4.35",
                "unRealizedProfit": "1.875",
                "positionSide": "LONG",
            },
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0",
                "entryPrice": "100000",
                "markPrice": "100100",
                "unRealizedProfit": "0",
                "positionSide": "BOTH",
            },
        ],
        "/fapi/v1/openOrders": [],
        "/fapi/v1/openAlgoOrders": {"orders": []},
    }

    def fake_signed_get(base, path, key, secret, params=None):
        return responses[path]

    monkeypatch.setattr(recorder, "signed_get", fake_signed_get)

    snapshot = recorder.snapshot_account("https://example.invalid", "key", "secret")

    assert snapshot["positions"] == [
        {
            "symbol": "ATOMUSDT",
            "position_amt": "12.5",
            "entry_price": "4.20",
            "mark_price": "4.35",
            "unrealized_pnl": "1.875",
            "position_side": "LONG",
        }
    ]


def test_snapshot_account_builds_canonical_summary_from_usdm_account_v3(monkeypatch) -> None:
    recorder = _load_recorder()
    requested_paths = []
    responses = {
        "/fapi/v3/account": {
            "totalMarginBalance": "1250.50",
            "totalInitialMargin": "225.25",
            "availableBalance": "1025.25",
        },
        "/fapi/v2/positionRisk": [],
        "/fapi/v1/openOrders": [],
        "/fapi/v1/openAlgoOrders": {"orders": []},
    }

    def fake_signed_get(base, path, key, secret, params=None):
        requested_paths.append(path)
        return responses[path]

    monkeypatch.setattr(recorder, "signed_get", fake_signed_get)

    snapshot = recorder.snapshot_account("https://example.invalid", "key", "secret")

    assert "/fapi/v3/account" in requested_paths
    assert snapshot["account"] == {
        "currency": "USDT",
        "equity": "1250.50",
        "margin": "225.25",
        "free": "1025.25",
    }


def test_run_once_upserts_exchange_and_account_projections_in_one_transaction(monkeypatch) -> None:
    recorder = _load_recorder()
    account = {
        "currency": "USDT",
        "equity": "1250.50",
        "margin": "225.25",
        "free": "1025.25",
    }
    snapshot = {
        "source": "binance_fapi",
        "fetched_at": "2026-07-29T12:00:00Z",
        "account": account,
        "positions": [],
        "open_orders": [],
        "algo_orders": [],
        "protections": [],
    }
    conn = RecordingConnection()
    monkeypatch.setattr(
        recorder,
        "ACCOUNTS",
        {"account-a": ("trader-v3-node-a", "BINANCE_ACCOUNT_A")},
    )
    monkeypatch.setattr(recorder, "container_keys", lambda container, prefix: ("key", "secret"))
    monkeypatch.setattr(recorder, "snapshot_account", lambda base, key, secret: snapshot)

    recorder.run_once(conn, "https://example.invalid")

    assert [event[0] for event in conn.events] == ["execute", "execute", "commit"]
    mirror_event, account_event, _ = conn.events
    assert "INSERT INTO exchange_state_mirror" in mirror_event[1]
    assert "ON CONFLICT (account_id) DO UPDATE" in mirror_event[1]
    assert mirror_event[2][0] == "account-a"
    assert json.loads(mirror_event[2][1]) == snapshot
    assert "INSERT INTO accounts_projection" in account_event[1]
    assert "ON CONFLICT (account_id) DO UPDATE" in account_event[1]
    assert "reconciliation_state" not in account_event[1]
    assert "last_execution_event_at" not in account_event[1]
    assert "projection_lag_ms" not in account_event[1]
    assert "updated_from_event_id" not in account_event[1]
    assert account_event[2][:5] == (
        "account-a",
        "USDT",
        "1250.50",
        "225.25",
        "1025.25",
    )
    assert json.loads(account_event[2][5]) == account


@pytest.mark.parametrize(
    ("account_info", "expected_error"),
    [
        (
            {
                "totalInitialMargin": "100.00",
                "availableBalance": "900.00",
            },
            "totalMarginBalance missing",
        ),
        (
            {
                "totalMarginBalance": "0",
                "totalInitialMargin": "0",
                "availableBalance": "0",
            },
            "totalMarginBalance must be positive",
        ),
    ],
)
def test_run_once_invalid_account_summary_preserves_existing_rows(
    monkeypatch,
    capsys,
    account_info,
    expected_error,
) -> None:
    recorder = _load_recorder()
    conn = RecordingConnection()
    monkeypatch.setattr(
        recorder,
        "ACCOUNTS",
        {"account-a": ("trader-v3-node-a", "BINANCE_ACCOUNT_A")},
    )
    monkeypatch.setattr(recorder, "container_keys", lambda container, prefix: ("key", "secret"))

    def fake_signed_get(base, path, key, secret, params=None):
        assert path == "/fapi/v3/account"
        return account_info

    monkeypatch.setattr(recorder, "signed_get", fake_signed_get)

    recorder.run_once(conn, "https://example.invalid")

    assert conn.events == []
    assert expected_error in capsys.readouterr().out


def test_run_once_rolls_back_when_account_projection_write_fails(monkeypatch) -> None:
    recorder = _load_recorder()
    account = {
        "currency": "USDT",
        "equity": "1250.50",
        "margin": "225.25",
        "free": "1025.25",
    }
    snapshot = {
        "source": "binance_fapi",
        "fetched_at": "2026-07-29T12:00:00Z",
        "account": account,
        "positions": [],
        "open_orders": [],
        "algo_orders": [],
        "protections": [],
    }
    conn = FailingSecondExecuteConnection()
    monkeypatch.setattr(
        recorder,
        "ACCOUNTS",
        {"account-a": ("trader-v3-node-a", "BINANCE_ACCOUNT_A")},
    )
    monkeypatch.setattr(recorder, "container_keys", lambda container, prefix: ("key", "secret"))
    monkeypatch.setattr(recorder, "snapshot_account", lambda base, key, secret: snapshot)

    with pytest.raises(RuntimeError, match="account projection write failed"):
        recorder.run_once(conn, "https://example.invalid")

    assert [event[0] for event in conn.events] == ["execute", "execute", "rollback"]
