import json
import sqlite3

from freqtrade.signal_strategy.importer import import_signal_items
from freqtrade.signal_strategy.store import SQLiteSignalStore


def make_item(message_id=1):
    return {
        "chatId": "-1001",
        "id": message_id,
        "chatTitle": "coinAlert",
        "date": "2026-06-10T12:00:00+00:00",
        "text": "BTCUSDT LONG\nEntry: 65000\nSL: 64000\nTP: 67000",
    }


def signal_events(db_path, event_type):
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT event_type, signal_id, payload
            FROM signal_events
            WHERE event_type = ?
            ORDER BY id
            """,
            (event_type,),
        ).fetchall()
    return [
        {
            "event_type": row[0],
            "signal_id": row[1],
            "payload": json.loads(row[2]),
        }
        for row in rows
    ]


def test_importer_records_checked_position_context_event_and_signal_payload(tmp_path, monkeypatch):
    position = {
        "pair": "BTC/USDT:USDT",
        "side": "long",
        "amount": 0.25,
        "open_rate": 65000.0,
        "current_profit_pct": 1.7,
        "trade_id": 42,
    }
    monkeypatch.setattr("freqtrade.signal_strategy.importer.get_open_position", lambda pair: position)
    db_path = tmp_path / "signals.sqlite"

    result = import_signal_items([make_item()], f"sqlite:///{db_path}", approve_parsed=True)

    events = signal_events(db_path, "position_context_checked")
    payload = events[0]["payload"]
    stored_signal = SQLiteSignalStore(db_path).get_signal("sig-c1001-m1")

    assert result.approved == 1
    assert len(events) == 1
    assert events[0]["signal_id"] == "sig-c1001-m1"
    assert payload == {
        "pair": "BTC/USDT:USDT",
        "has_open_position": True,
        "position": position,
        "query_success": True,
    }
    assert stored_signal["risk_policy_result"]["details"]["position_context"] == payload


def test_importer_degrades_to_unknown_event_when_position_lookup_fails(tmp_path, monkeypatch):
    def fail_lookup(pair):
        raise RuntimeError("freqtrade unavailable")

    monkeypatch.setattr("freqtrade.signal_strategy.importer.get_open_position", fail_lookup)
    db_path = tmp_path / "signals.sqlite"

    result = import_signal_items([make_item(2)], f"sqlite:///{db_path}", approve_parsed=True)

    events = signal_events(db_path, "position_context_unknown")
    payload = events[0]["payload"]
    stored_signal = SQLiteSignalStore(db_path).get_signal("sig-c1001-m2")

    assert result.approved == 1
    assert len(events) == 1
    assert events[0]["signal_id"] == "sig-c1001-m2"
    assert payload == {
        "pair": "BTC/USDT:USDT",
        "has_open_position": False,
        "position": None,
        "query_success": False,
    }
    assert stored_signal["risk_policy_result"]["details"]["position_context"] == payload
