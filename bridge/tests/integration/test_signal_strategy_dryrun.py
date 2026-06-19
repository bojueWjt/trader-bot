from datetime import datetime, timezone
import json
from pathlib import Path

from app.services.signal_parser import SignalStatus, parse_signal
from app.services.signal_store import SignalStore
from user_data.strategies.SignalStrategy import SignalStrategy

from tests.unit.test_signalstrategy_loading import FakeDataFrame


class FakeDryRunAdapter:
    def __init__(self):
        self.trades = []

    def process_entry(self, dataframe, pair):
        if dataframe["enter_long"].sum() <= 0:
            return False
        tags = dataframe.columns.get("enter_tag", [""])
        self.trades.append({"pair": pair, "entry_tag": tags[-1]})
        return self.trades[-1]


def test_gs_002_reaches_entered_and_duplicate_bot_loop_does_not_duplicate_trade():
    store = SignalStore()
    signal = _load_gs_002()
    signal.status = SignalStatus.APPROVED
    store.upsert_signal(signal)
    strategy = SignalStrategy({"signal_strategy": {"store": store, "lookback_minutes": 240}})
    adapter = FakeDryRunAdapter()
    current_time = datetime(2026, 2, 8, 16, 35, tzinfo=timezone.utc)

    strategy.bot_loop_start(current_time)
    first_frame = strategy.populate_entry_trend(FakeDataFrame(), {"pair": "BTC/USDT:USDT"})
    trade = adapter.process_entry(first_frame, "BTC/USDT:USDT")
    strategy.order_filled("BTC/USDT:USDT", _trade(trade["entry_tag"]), object(), current_time)

    strategy.bot_loop_start(current_time)
    second_frame = strategy.populate_entry_trend(FakeDataFrame(), {"pair": "BTC/USDT:USDT"})
    adapter.process_entry(second_frame, "BTC/USDT:USDT")

    stored = store.get_signal("-1002328068747:19")
    assert stored.status == SignalStatus.ENTERED
    assert len(adapter.trades) == 1


def _load_gs_002():
    fixture_path = Path("fixtures/signals/golden_signals.json")
    samples = json.loads(fixture_path.read_text())
    for sample in samples:
        if sample["sample_id"] == "GS-002":
            return parse_signal(sample, pair_whitelist={"BTC/USDT:USDT"})
    raise AssertionError("GS-002 fixture missing")


def _trade(entry_tag):
    class FakeTrade:
        id = 1001
        pair = "BTC/USDT:USDT"
        is_short = False

        def __init__(self, active_entry_tag):
            self.entry_tag = active_entry_tag

    return FakeTrade(entry_tag)
