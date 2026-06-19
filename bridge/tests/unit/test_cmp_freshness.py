from datetime import datetime, timezone
import json
from pathlib import Path

from freqtrade.signal_strategy.domain import SignalStatus
from freqtrade.signal_strategy.importer import import_signal_items
from freqtrade.signal_strategy.risk import RiskPolicy


CORPUS_PATH = Path(__file__).parents[2] / "fixtures" / "signals" / "telegram_latest20" / "messages.latest20.json"


def make_item(message_id, date, text):
    return {
        "chatId": -1001,
        "id": message_id,
        "chatTitle": "coinAlert",
        "date": date,
        "text": text,
    }


def load_corpus_items(*message_ids):
    items = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    wanted = set(message_ids)
    matches = {}
    for item in items:
        if item.get("id") in wanted:
            matches[item["id"]] = item
    return [matches[message_id] for message_id in message_ids]


def test_fresh_cmp_signal_without_numeric_entry_auto_approves(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "freqtrade.signal_strategy.importer.utc_now",
        lambda: datetime(2026, 6, 10, 12, 20, tzinfo=timezone.utc),
    )

    result = import_signal_items(
        [
            make_item(
                4934,
                "2026-06-10T12:00:00+00:00",
                "BTCUSDT LONG\nEntry: CMP\nSL: 104000\nTP: 110000",
            )
        ],
        f"sqlite:///{tmp_path / 'signals.sqlite'}",
        approve_parsed=True,
    )

    assert result.approved == 1
    assert result.needs_review == 0
    assert result.rejected == 0


def test_stale_cmp_signal_without_numeric_entry_expires_instead_of_review(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "freqtrade.signal_strategy.importer.utc_now",
        lambda: datetime(2026, 6, 10, 12, 31, tzinfo=timezone.utc),
    )

    db_path = tmp_path / "signals.sqlite"
    result = import_signal_items(
        [
            make_item(
                4934,
                "2026-06-10T12:00:00+00:00",
                "BTCUSDT LONG\nEntry: CMP\nSL: 104000\nTP: 110000",
            )
        ],
        f"sqlite:///{db_path}",
        approve_parsed=True,
    )

    assert result.approved == 0
    assert result.needs_review == 0
    assert result.rejected == 0

    from freqtrade.signal_strategy.store import SQLiteSignalStore

    signal = SQLiteSignalStore(db_path).get_signal("sig-c1001-m4934")
    assert signal["status"] == SignalStatus.EXPIRED.value
    assert "cmp_signal_expired" in signal["review_reason_codes"]


def test_limit_signal_without_take_profit_auto_approves_with_stop_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "freqtrade.signal_strategy.importer.utc_now",
        lambda: datetime(2026, 6, 10, 12, 20, tzinfo=timezone.utc),
    )

    result = import_signal_items(
        [
            make_item(
                4327,
                "2026-06-10T12:00:00+00:00",
                "SYRUPUSDT LONG\nEntry: 0.1307\n补仓: 0.12\nSL: 0.11\n止盈目标：请参考图表上的标记",
            )
        ],
        f"sqlite:///{tmp_path / 'signals.sqlite'}",
        approve_parsed=True,
    )

    assert result.approved == 1
    assert result.needs_review == 0
    assert result.rejected == 0


def test_cmp_freshness_uses_policy_window(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "freqtrade.signal_strategy.importer.utc_now",
        lambda: datetime(2026, 6, 10, 12, 31, tzinfo=timezone.utc),
    )

    result = import_signal_items(
        [
            make_item(
                4934,
                "2026-06-10T12:00:00+00:00",
                "BTCUSDT LONG\nEntry: CMP\nSL: 104000\nTP: 110000",
            )
        ],
        f"sqlite:///{tmp_path / 'signals.sqlite'}",
        approve_parsed=True,
        risk_policy=RiskPolicy(cmp_max_age_minutes=45),
    )

    assert result.approved == 1
    assert result.needs_review == 0
    assert result.rejected == 0


def test_real_corpus_targets_approve_when_cmp_messages_are_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "freqtrade.signal_strategy.importer.utc_now",
        lambda: datetime(2026, 6, 8, 8, 20, 42, tzinfo=timezone.utc),
    )

    fresh_items = []
    for item in load_corpus_items(4327, 4934, 4938):
        cloned = dict(item)
        cloned["date"] = "2026-06-10T12:00:00+00:00"
        fresh_items.append(cloned)

    result = import_signal_items(
        fresh_items,
        f"sqlite:///{tmp_path / 'signals.sqlite'}",
        approve_parsed=True,
    )

    assert result.approved == 3
    assert result.needs_review == 0
    assert result.rejected == 0
