from datetime import datetime, timezone

from app.services.signal_parser import SignalStatus
from app.services.signal_store import SignalStore
from user_data.strategies.SignalStrategy import SignalStrategy


class FakeTrade:
    def __init__(
        self,
        entry_tag="sig:sig-long",
        pair="BTC/USDT:USDT",
        is_short=False,
        open_rate=100.0,
        leverage=1.0,
    ):
        self.id = 42
        self.entry_tag = entry_tag
        self.pair = pair
        self.is_short = is_short
        self.open_rate = open_rate
        self.leverage = leverage


def make_strategy(signal, store=False, risk_policy=None):
    strategy_config = {
        "store": store,
        "equity": 1000,
        "risk_pct": 0.01,
        "default_leverage": 2,
        "max_leverage": 5,
    }
    if risk_policy is not None:
        strategy_config["risk_policy"] = risk_policy
    strategy = SignalStrategy(
        {
            "stake_currency": "USDT",
            "signal_strategy": strategy_config,
        }
    )
    strategy.signals_by_id = {signal["signal_id"]: signal}
    strategy.entry_signals_by_pair = {signal["pair_freqtrade"]: [signal]}
    return strategy


def make_signal(**overrides):
    signal = {
        "signal_id": "sig-long",
        "pair_freqtrade": "BTC/USDT:USDT",
        "side": "long",
        "status": "sent_to_freqtrade",
        "entry": {"type": "limit", "primary_price": 100.0, "dca_prices": [96.0]},
        "stop_loss": 90.0,
        "take_profits": [{"price": 110.0}],
        "dca": [{"price": 96.0, "risk_pct": 0.005}],
        "leverage": {"selected": 2, "max": 3},
    }
    signal.update(overrides)
    return signal


def absolute_stop_from_custom_stoploss(stoploss, current_rate, is_short=False, leverage=1.0):
    if is_short:
        return current_rate * (1.0 + stoploss / leverage)
    return current_rate * (1.0 - stoploss / leverage)


def test_single_take_profit_exits_once():
    signal = make_signal()
    strategy = make_strategy(signal)
    trade = FakeTrade()
    current_time = datetime(2026, 5, 31, 12, 20, tzinfo=timezone.utc)

    first = strategy.custom_exit("BTC/USDT:USDT", trade, current_time, 111.0, 0.1)
    second = strategy.custom_exit("BTC/USDT:USDT", trade, current_time, 112.0, 0.2)

    assert first == "trade_exit_event/signal_take_profit"
    assert second is False


def test_long_btc_breakeven_stoploss_moves_to_entry_with_fee_buffer():
    signal = make_signal(stop_loss=90.0)
    strategy = make_strategy(signal)
    trade = FakeTrade(open_rate=100.0)
    current_rate = 103.0

    stoploss = strategy.custom_stoploss(
        "BTC/USDT:USDT",
        trade,
        datetime(2026, 5, 31, 12, 20, tzinfo=timezone.utc),
        current_rate,
        0.021,
    )

    absolute_stop = absolute_stop_from_custom_stoploss(stoploss, current_rate)
    assert round(absolute_stop, 4) == 100.1


def test_short_btc_breakeven_stoploss_moves_to_entry_with_fee_buffer():
    signal = make_signal(side="short", stop_loss=110.0)
    strategy = make_strategy(signal)
    trade = FakeTrade(is_short=True, open_rate=100.0)
    current_rate = 97.0

    stoploss = strategy.custom_stoploss(
        "BTC/USDT:USDT",
        trade,
        datetime(2026, 5, 31, 12, 20, tzinfo=timezone.utc),
        current_rate,
        0.021,
    )

    absolute_stop = absolute_stop_from_custom_stoploss(stoploss, current_rate, is_short=True)
    assert round(absolute_stop, 4) == 99.9


def test_breakeven_stoploss_keeps_signal_stop_before_threshold():
    signal = make_signal(stop_loss=90.0)
    strategy = make_strategy(signal)
    trade = FakeTrade(open_rate=100.0)
    current_rate = 101.9

    stoploss = strategy.custom_stoploss(
        "BTC/USDT:USDT",
        trade,
        datetime(2026, 5, 31, 12, 20, tzinfo=timezone.utc),
        current_rate,
        0.019,
    )

    absolute_stop = absolute_stop_from_custom_stoploss(stoploss, current_rate)
    assert round(absolute_stop, 4) == 90.0


def test_breakeven_stoploss_pair_override_controls_threshold():
    signal = make_signal(
        signal_id="sig-eth",
        pair_freqtrade="ETH/USDT:USDT",
        stop_loss=90.0,
    )
    default_strategy = make_strategy(signal)
    override_strategy = make_strategy(
        signal,
        risk_policy={
            "breakeven_stoploss": {
                "pair_overrides": {
                    "ETH/USDT:USDT": {
                        "trigger_profit_pct": 3.0,
                        "fee_buffer_pct": 0.002,
                    }
                }
            }
        },
    )
    trade = FakeTrade(entry_tag="sig:sig-eth", pair="ETH/USDT:USDT", open_rate=100.0)
    current_rate = 103.5
    current_time = datetime(2026, 5, 31, 12, 20, tzinfo=timezone.utc)

    default_stoploss = default_strategy.custom_stoploss(
        "ETH/USDT:USDT",
        trade,
        current_time,
        current_rate,
        0.035,
    )
    override_stoploss = override_strategy.custom_stoploss(
        "ETH/USDT:USDT",
        trade,
        current_time,
        current_rate,
        0.035,
    )

    default_absolute_stop = absolute_stop_from_custom_stoploss(default_stoploss, current_rate)
    override_absolute_stop = absolute_stop_from_custom_stoploss(override_stoploss, current_rate)
    assert round(default_absolute_stop, 4) == 90.0
    assert round(override_absolute_stop, 4) == 100.2


def test_second_take_profit_can_exit_after_first_level_filled():
    signal = make_signal(
        take_profits=[
            {"price": 110.0},
            {"price": 120.0},
        ]
    )
    strategy = make_strategy(signal)
    trade = FakeTrade()
    current_time = datetime(2026, 5, 31, 12, 20, tzinfo=timezone.utc)

    first = strategy.custom_exit("BTC/USDT:USDT", trade, current_time, 111.0, 0.1)
    second = strategy.custom_exit("BTC/USDT:USDT", trade, current_time, 121.0, 0.2)

    assert first == "trade_exit_event/signal_take_profit"
    assert second == "trade_exit_event/signal_take_profit"


def test_each_dca_price_triggers_once():
    signal = make_signal(
        dca=[
            {"price": 96.0, "risk_pct": 0.005},
            {"price": 94.0, "risk_pct": 0.005},
        ]
    )
    strategy = make_strategy(signal)
    trade = FakeTrade()
    current_time = datetime(2026, 5, 31, 12, 20, tzinfo=timezone.utc)

    first = strategy.adjust_trade_position(
        trade,
        current_time,
        95.0,
        -0.05,
        10.0,
        1000.0,
        100.0,
        95.0,
        -0.05,
        0.0,
    )
    duplicate = strategy.adjust_trade_position(
        trade,
        current_time,
        95.0,
        -0.05,
        10.0,
        1000.0,
        100.0,
        95.0,
        -0.05,
        0.0,
    )
    second = strategy.adjust_trade_position(
        trade,
        current_time,
        93.0,
        -0.07,
        10.0,
        1000.0,
        100.0,
        93.0,
        -0.07,
        0.0,
    )

    assert first[1] == "sig:sig-long:dca:1"
    assert duplicate is None
    assert second[1] == "sig:sig-long:dca:2"


def test_entry_dca_prices_fallback_triggers_once():
    signal = make_signal(dca=[], entry={"type": "limit", "primary_price": 100.0, "dca_prices": [96.0]})
    strategy = make_strategy(signal)
    trade = FakeTrade()
    current_time = datetime(2026, 5, 31, 12, 20, tzinfo=timezone.utc)

    result = strategy.adjust_trade_position(
        trade,
        current_time,
        95.0,
        -0.05,
        10.0,
        1000.0,
        100.0,
        95.0,
        -0.05,
        0.0,
    )

    assert result[1] == "sig:sig-long:dca:1"


def test_order_filled_updates_signal_and_store_status():
    store = SignalStore()
    parsed_signal = _make_store_signal()
    store.upsert_signal(parsed_signal)
    store.transition_signal("sig-long", SignalStatus.APPROVED, "reviewer")
    store.transition_signal("sig-long", SignalStatus.RESERVED, "strategy")
    store.transition_signal("sig-long", SignalStatus.SENT_TO_FREQTRADE, "strategy")
    signal = make_signal()
    strategy = make_strategy(signal, store=store)
    trade = FakeTrade()

    strategy.order_filled(
        "BTC/USDT:USDT",
        trade,
        object(),
        datetime(2026, 5, 31, 12, 20, tzinfo=timezone.utc),
    )

    assert signal["status"] == "entered"
    assert signal["trade_id"] == 42
    assert store.get_signal("sig-long").status == SignalStatus.ENTERED


def _make_store_signal():
    from app.services.signal_parser import parse_signal

    return parse_signal(
        {
            "signal_id": "sig-long",
            "source": "telegram",
            "source_channel_id": "-1001",
            "source_channel_name": "coinAlert",
            "source_message_id": "1",
            "received_at": "2026-05-31T12:00:00+00:00",
            "raw_text": "BTCUSDT LONG Entry: 现价 SL: 90000 TP: 110000",
            "media": [],
        },
        pair_whitelist={"BTC/USDT:USDT"},
    )
