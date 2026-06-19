from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from freqtrade.signal_strategy.strategy import SignalStrategy as CoreSignalStrategy
from user_data.strategies.SignalStrategy import SignalStrategy


class FakeTrade:
    def __init__(self, entry_tag="sig:sig-long", is_short=False, leverage=3.0):
        self.entry_tag = entry_tag
        self.is_short = is_short
        self.leverage = leverage


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
        self.columns = {}
        self.index = [0]
        self.at = FakeAt(self)

    def __contains__(self, column):
        return column in self.columns

    def __getitem__(self, column):
        return FakeSeries(self.columns[column])

    def __setitem__(self, column, value):
        self.columns[column] = [value]


def make_strategy(signals):
    strategy = SignalStrategy(
        {
            "stake_currency": "USDT",
            "signal_strategy": {
                "store": False,
                "risk_pct": 0.01,
                "equity": 1000,
                "default_leverage": 3,
                "max_leverage": 5,
            },
        }
    )
    strategy.store_available = True
    strategy.signals_by_id = {signal["signal_id"]: signal for signal in signals}
    strategy.entry_signals_by_pair = {}
    for signal in signals:
        pair_signals = strategy.entry_signals_by_pair.setdefault(signal["pair_freqtrade"], [])
        pair_signals.append(signal)
    return strategy


def make_signal(**overrides):
    signal = {
        "signal_id": "sig-long",
        "pair_freqtrade": "BTC/USDT:USDT",
        "side": "long",
        "status": "approved",
        "entry": {"type": "limit", "primary_price": 105.0},
        "stop_loss": 95.0,
        "risk_pct": 0.02,
        "leverage": {"selected": 10, "max": 4},
    }
    signal.update(overrides)
    return signal


class FakeCoreStore:
    def __init__(self):
        self.transitions = []
        self.released = []
        self.audit_events = []

    def transition_signal(self, signal_id, target_status, actor, context=None):
        self.transitions.append(
            {
                "signal_id": signal_id,
                "target_status": getattr(target_status, "value", target_status),
                "actor": actor,
                "context": context or {},
            }
        )
        return SimpleNamespace(approved=True)

    def release_signal_reservation(self, signal_id, operation_type="entry"):
        self.released.append((signal_id, operation_type))

    def record_audit_event(self, event_type, signal_id, **payload):
        self.audit_events.append({"type": event_type, "signal_id": signal_id, **payload})


class FakeApprovedSignalStore(FakeCoreStore):
    def __init__(self, signals):
        super().__init__()
        self.signals = signals

    def get_approved_signals(self, since_minutes, current_time):
        return self.signals


def make_core_strategy(signal, store=None, **config):
    strategy = CoreSignalStrategy(
        {
            "stake_currency": "USDT",
            "signal_strategy": {
                "store": store if store is not None else FakeCoreStore(),
                "equity": 1000,
                "risk_pct": 0.01,
                "default_leverage": 3,
                "max_leverage": 5,
                **config,
            },
        }
    )
    strategy.signals_by_id = {signal["signal_id"]: signal}
    strategy._entry_signals_by_pair = {signal["pair_freqtrade"]: [signal]}
    return strategy


def test_long_signal_sets_only_enter_long():
    strategy = make_strategy([make_signal()])

    result = strategy.populate_entry_trend(FakeDataFrame(), {"pair": "BTC/USDT:USDT"})

    assert result["enter_long"].sum() == 1
    assert result["enter_short"].sum() == 0
    assert result.columns["enter_tag"][-1] == "sig:sig-long"


def test_short_signal_sets_only_enter_short():
    strategy = make_strategy(
        [
            make_signal(
                signal_id="sig-short",
                side="short",
                stop_loss=110.0,
            )
        ]
    )

    result = strategy.populate_entry_trend(FakeDataFrame(), {"pair": "BTC/USDT:USDT"})

    assert result["enter_short"].sum() == 1
    assert result["enter_long"].sum() == 0
    assert result.columns["enter_tag"][-1] == "sig:sig-short"


def test_custom_stake_amount_uses_fixed_risk_model():
    strategy = make_strategy([make_signal()])

    stake = strategy.custom_stake_amount(
        pair="BTC/USDT:USDT",
        current_time=datetime(2026, 5, 31, 12, 10, tzinfo=timezone.utc),
        current_rate=100.0,
        proposed_stake=50.0,
        min_stake=10.0,
        max_stake=1000.0,
        leverage=2.0,
        entry_tag="sig:sig-long",
        side="long",
    )

    assert stake == 105.0


@pytest.mark.parametrize(
    ("entry", "proposed_rate", "expected"),
    [
        ({"type": "cmp", "primary_price": 105.0}, 101.0, 101.0),
        ({"mode": "cmp", "primary_price": 105.0}, 101.0, 101.0),
        ({"type": "limit", "primary_price": 105.0}, 101.0, 105.0),
        ({"price_min": 98.0, "price_max": 102.0}, 101.0, 100.0),
        ({}, 101.0, 101.0),
    ],
)
def test_custom_entry_price_resolves_price_modes(entry, proposed_rate, expected):
    strategy = make_strategy([make_signal(entry=entry)])

    price = strategy.custom_entry_price(
        pair="BTC/USDT:USDT",
        trade=None,
        current_time=datetime(2026, 5, 31, 12, 10, tzinfo=timezone.utc),
        proposed_rate=proposed_rate,
        entry_tag="sig:sig-long",
        side="long",
    )

    assert price == expected


def test_leverage_is_capped_by_signal_policy_and_strategy_limit():
    strategy = make_strategy([make_signal(leverage={"selected": 10, "max": 4})])

    leverage = strategy.leverage(
        pair="BTC/USDT:USDT",
        current_time=datetime(2026, 5, 31, 12, 10, tzinfo=timezone.utc),
        current_rate=100.0,
        proposed_leverage=8.0,
        max_leverage=20.0,
        entry_tag="sig:sig-long",
        side="long",
    )

    assert leverage == 4.0


def test_custom_stoploss_uses_signal_absolute_stop_loss():
    strategy = make_strategy([make_signal()])
    trade = FakeTrade()

    stoploss = strategy.custom_stoploss(
        pair="BTC/USDT:USDT",
        trade=trade,
        current_time=datetime(2026, 5, 31, 12, 10, tzinfo=timezone.utc),
        current_rate=100.0,
        current_profit=0.0,
    )

    assert round(stoploss, 4) == 0.15


def test_confirm_trade_entry_rejects_long_cmp_above_entry_upper_bound():
    store = FakeCoreStore()
    signal = make_signal(entry={"mode": "cmp", "price_max": 61.0}, stop_loss=55.0)
    strategy = make_core_strategy(signal, store=store)

    accepted = strategy.confirm_trade_entry(
        pair="BTC/USDT:USDT",
        order_type="market",
        amount=1.0,
        rate=62.0,
        time_in_force="gtc",
        current_time=datetime(2026, 5, 31, 12, 10, tzinfo=timezone.utc),
        entry_tag="sig:sig-long",
        side="long",
    )

    assert accepted is False
    assert signal["status"] == "rejected"
    assert store.audit_events[-1]["type"] == "trade_entry_rejected"


def test_confirm_trade_entry_allows_long_cmp_at_entry_upper_bound():
    signal = make_signal(entry={"mode": "cmp", "price_max": 61.0}, stop_loss=55.0)
    strategy = make_core_strategy(signal)

    accepted = strategy.confirm_trade_entry(
        pair="BTC/USDT:USDT",
        order_type="market",
        amount=1.0,
        rate=61.0,
        time_in_force="gtc",
        current_time=datetime(2026, 5, 31, 12, 10, tzinfo=timezone.utc),
        entry_tag="sig:sig-long",
        side="long",
    )

    assert accepted is True


def test_unfilled_entry_order_timeout_cancels_and_reapproves_signal():
    store = FakeCoreStore()
    signal = make_signal(status="sent_to_freqtrade", reserved=True)
    strategy = make_core_strategy(signal, store=store, unfilled_timeout_seconds=300)
    order = {
        "id": "order-1",
        "status": "open",
        "pair": "BTC/USDT:USDT",
        "ft_order_tag": "sig:sig-long",
        "order_date_utc": datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
        "price": 105.0,
        "amount": 1.0,
    }
    strategy._open_orders = [order]

    strategy._monitor_open_entry_orders(datetime(2026, 5, 31, 12, 6, tzinfo=timezone.utc))

    assert order["status"] == "cancelled"
    assert signal["status"] == "approved"
    assert signal["reserved"] is False
    assert store.released == [("sig-long", "entry")]
    assert store.transitions[-1]["target_status"] == "approved"
    assert store.audit_events[-1]["type"] == "unfilled_timeout_cancelled"


def test_adjust_entry_price_replaces_order_when_price_deviation_exceeds_threshold():
    signal = make_signal(status="sent_to_freqtrade", reserved=True, entry={"type": "limit", "primary_price": 100.0})
    strategy = make_core_strategy(signal, adjust_entry_price_deviation_pct=0.02)
    order = {
        "id": "order-1",
        "status": "open",
        "pair": "BTC/USDT:USDT",
        "ft_order_tag": "sig:sig-long",
        "order_date_utc": datetime(2026, 5, 31, 12, 5, tzinfo=timezone.utc),
        "price": 100.0,
        "current_rate": 103.0,
        "amount": 0.5,
    }
    strategy._open_orders = [order]

    strategy._monitor_open_entry_orders(datetime(2026, 5, 31, 12, 6, tzinfo=timezone.utc))

    assert order["status"] == "cancelled"
    assert signal["entry"]["primary_price"] == 103.0
    assert strategy._replacement_entry_orders[-1]["rate"] == 103.0
    assert strategy._replacement_entry_orders[-1]["entry_tag"].startswith("sig:sig-long")


def test_bot_loop_start_does_not_pick_up_expired_signals():
    expired_signal = make_signal(status="expired")
    store = FakeApprovedSignalStore([expired_signal])
    strategy = CoreSignalStrategy(
        {
            "stake_currency": "USDT",
            "signal_strategy": {
                "store": store,
                "lookback_minutes": 60,
            },
        }
    )

    strategy.bot_loop_start(datetime(2026, 5, 31, 12, 5, tzinfo=timezone.utc))

    assert "sig-long" not in strategy.signals_by_id
    assert strategy._entry_signals_by_pair == {}
