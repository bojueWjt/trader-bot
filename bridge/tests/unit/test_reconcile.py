from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from scripts import reconcile


def _env() -> dict[str, str]:
    return {
        "FREQTRADE_API_URL": "http://freqtrade.test",
        "FREQTRADE_API_USERNAME": "user",
        "FREQTRADE_API_PASSWORD": "pass",
        "SIGNAL_STORE_URL": "http://signals.test",
    }


def _signal(signal_id: str, status: str, **extra: object) -> dict:
    return {"signal_id": signal_id, "status": status, **extra}


def _trade(trade_id: int, enter_tag: str) -> dict:
    return {"trade_id": trade_id, "id": trade_id, "enter_tag": enter_tag, "is_open": True}


def _run_reconcile(tmp_path, *, signals: list[dict], trades: list[dict], trade_details: dict[int, dict] | None = None):
    output_path = tmp_path / "reconcile-latest.json"
    with (
        patch.object(reconcile.SignalStoreClient, "list_signals", return_value=signals) as list_signals,
        patch.object(reconcile.FreqtradeClient, "list_open_trades", return_value=trades) as list_open_trades,
        patch.object(
            reconcile.FreqtradeClient,
            "get_trade",
            side_effect=lambda trade_id: (trade_details or {}).get(int(trade_id)),
        ) as get_trade,
        patch.object(reconcile, "send_telegram_alert") as send_alert,
    ):
        exit_code = reconcile.main(env=_env(), output_path=output_path)

    report = json.loads(output_path.read_text())
    return exit_code, report, list_signals, list_open_trades, get_trade, send_alert


def test_consistent_has_no_discrepancies_and_exits_zero(tmp_path):
    exit_code, report, list_signals, list_open_trades, get_trade, send_alert = _run_reconcile(
        tmp_path,
        signals=[_signal("sig-1", "entered")],
        trades=[_trade(101, "sig:sig-1")],
    )

    assert exit_code == 0
    assert report["discrepancies"] == []
    list_signals.assert_called_once_with(["sent_to_freqtrade", "entered", "partially_exited"])
    list_open_trades.assert_called_once_with()
    get_trade.assert_not_called()
    send_alert.assert_not_called()


def test_missing_trade_for_entered_signal_exits_two(tmp_path):
    exit_code, report, *_ = _run_reconcile(
        tmp_path,
        signals=[_signal("sig-1", "entered")],
        trades=[],
    )

    assert exit_code == 2
    assert report["discrepancies"] == [
        {
            "type": "missing_trade",
            "severity": "warning",
            "signal_id": "sig-1",
            "status": "entered",
        }
    ]


def test_orphan_trade_for_unknown_enter_tag_signal_exits_two(tmp_path):
    exit_code, report, *_ = _run_reconcile(
        tmp_path,
        signals=[],
        trades=[_trade(202, "sig:missing-signal")],
    )

    assert exit_code == 2
    assert report["discrepancies"] == [
        {
            "type": "orphan_trade",
            "severity": "warning",
            "trade_id": 202,
            "signal_id": "missing-signal",
            "enter_tag": "sig:missing-signal",
        }
    ]


def test_api_unreachable_exits_two_with_alert_level_discrepancy(tmp_path):
    output_path = tmp_path / "reconcile-latest.json"
    with (
        patch.object(reconcile.SignalStoreClient, "list_signals", return_value=[]),
        patch.object(reconcile.FreqtradeClient, "list_open_trades", side_effect=ConnectionError("down")),
        patch.object(reconcile, "send_telegram_alert") as send_alert,
    ):
        exit_code = reconcile.main(env=_env(), output_path=output_path)

    report = json.loads(output_path.read_text())
    assert exit_code == 2
    assert report["discrepancies"] == [
        {
            "type": "api_unreachable",
            "severity": "alert",
            "message": "down",
        }
    ]
    send_alert.assert_called_once()
