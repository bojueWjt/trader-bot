from datetime import datetime, timezone

from app.services.signal_parser import parse_signal, SignalStatus
from app.services.signal_store import SignalStore
from user_data.strategies.SignalStrategy import SignalStrategy


class FakeSeries:
    def __init__(self, values=None):
        self.values = list(values or [])

    def sum(self):
        return sum(self.values)


class FakeAt:
    def __init__(self, dataframe):
        self.dataframe = dataframe

    def __setitem__(self, key, value):
        _, column = key
        values = self.dataframe.columns.setdefault(column, [0])
        values[-1] = value


class FakeDataFrame:
    def __init__(self):
        self.columns = {
            "date": [datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)],
            "close": [68000.0],
        }
        self.index = [0]
        self.at = FakeAt(self)

    def __contains__(self, column):
        return column in self.columns

    def __getitem__(self, column):
        return FakeSeries(self.columns[column])

    def __setitem__(self, column, value):
        self.columns[column] = [value]


class FakeStore:
    def __init__(self, signals=None, error=False):
        self.signals = signals or []
        self.error = error
        self.calls = []

    def get_approved_signals(self, since_minutes, current_time):
        self.calls.append({"since_minutes": since_minutes, "current_time": current_time})
        if self.error:
            raise TimeoutError("store timeout")
        return self.signals


def make_signal(signal_id="sig-1", pair="BTC/USDT:USDT"):
    return {
        "signal_id": signal_id,
        "pair_freqtrade": pair,
        "side": "long",
        "status": "approved",
        "approved_at": "2026-05-31T12:00:00+00:00",
    }


def make_parsed_signal():
    signal = parse_signal(
        {
            "signal_id": "sig-real-1",
            "source": "telegram",
            "source_channel_id": "-1001",
            "source_channel_name": "coinAlert",
            "source_message_id": "1",
            "received_at": "2026-05-31T12:00:00+00:00",
            "raw_text": "BTCUSDT LONG Entry: 现价 SL: 64000 TP: 72000",
            "media": [],
        },
        pair_whitelist={"BTC/USDT:USDT"},
    )
    signal.status = SignalStatus.APPROVED
    return signal


def make_dataframe():
    return FakeDataFrame()


def test_bot_loop_start_reads_approved_signals():
    store = FakeStore([make_signal()])
    strategy = SignalStrategy({"signal_strategy": {"store": store, "lookback_minutes": 60}})

    strategy.bot_loop_start(datetime(2026, 5, 31, 12, 5, tzinfo=timezone.utc))

    assert store.calls[0]["since_minutes"] == 60
    assert "sig-1" in strategy.signals_by_id
    assert strategy.entry_signals_by_pair["BTC/USDT:USDT"][0]["signal_id"] == "sig-1"


def test_bot_loop_start_reads_approved_signals_from_signal_store():
    store = SignalStore()
    store.upsert_signal(make_parsed_signal())
    strategy = SignalStrategy({"signal_strategy": {"store": store, "lookback_minutes": 60}})

    strategy.bot_loop_start(datetime(2026, 5, 31, 12, 5, tzinfo=timezone.utc))

    assert strategy.store_available is True
    assert "sig-real-1" in strategy.signals_by_id
    assert strategy.entry_signals_by_pair["BTC/USDT:USDT"][0]["signal_id"] == "sig-real-1"


def test_populate_entry_reserves_and_marks_signal_sent_to_freqtrade():
    store = SignalStore()
    store.upsert_signal(make_parsed_signal())
    strategy = SignalStrategy({"signal_strategy": {"store": store, "lookback_minutes": 60}})
    strategy.bot_loop_start(datetime(2026, 5, 31, 12, 5, tzinfo=timezone.utc))

    result = strategy.populate_entry_trend(make_dataframe(), {"pair": "BTC/USDT:USDT"})

    assert result["enter_long"].sum() == 1
    assert store.get_signal("sig-real-1").status == SignalStatus.SENT_TO_FREQTRADE


def test_signal_store_timeout_does_not_throw_and_disables_entries():
    store = FakeStore([make_signal()], error=True)
    strategy = SignalStrategy({"signal_strategy": {"store": store}})

    strategy.bot_loop_start(datetime(2026, 5, 31, 12, 5, tzinfo=timezone.utc))
    result = strategy.populate_entry_trend(make_dataframe(), {"pair": "BTC/USDT:USDT"})

    assert strategy.store_available is False
    assert result["enter_long"].sum() == 0
    assert result["enter_short"].sum() == 0


def test_no_new_entries_on_store_failure():
    strategy = SignalStrategy({"signal_strategy": {"store": FakeStore([make_signal()])}})
    strategy.bot_loop_start(datetime(2026, 5, 31, 12, 5, tzinfo=timezone.utc))
    assert strategy.entry_signals_by_pair

    strategy.signal_store = FakeStore([make_signal("sig-2")], error=True)
    strategy.bot_loop_start(datetime(2026, 5, 31, 12, 6, tzinfo=timezone.utc))
    result = strategy.populate_entry_trend(make_dataframe(), {"pair": "BTC/USDT:USDT"})

    assert strategy.entry_signals_by_pair == {}
    assert result["enter_long"].sum() == 0
