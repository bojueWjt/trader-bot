import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from freqtrade.signal_strategy.domain import DirectiveKind, PositionDirective
from freqtrade.signal_strategy.importer import import_signal_items, normalize_watcher_signal
from freqtrade.signal_strategy.parser import extract_directives, parse_signal
from freqtrade.signal_strategy.store import InMemorySignalStore, SQLiteSignalStore
from user_data.strategies.SignalStrategy import SignalStrategy


class FakeTrade:
    def __init__(
        self,
        pair="BTC/USDT:USDT",
        entry_tag="sig:sig-long",
        stake_amount=100.0,
        open_rate=100.0,
        is_short=False,
        leverage=1.0,
    ):
        self.id = 99
        self.pair = pair
        self.entry_tag = entry_tag
        self.stake_amount = stake_amount
        self.open_rate = open_rate
        self.is_short = is_short
        self.leverage = leverage


def directive_kinds(directives):
    return [directive.kind for directive in directives]


def make_strategy(store, kill_switch_enabled=False):
    signal = {
        "signal_id": "sig-long",
        "pair_freqtrade": "BTC/USDT:USDT",
        "side": "long",
        "status": "sent_to_freqtrade",
        "entry": {"type": "limit", "primary_price": 100.0},
        "stop_loss": 90.0,
        "take_profits": [{"price": 110.0}],
        "leverage": {"selected": 1, "max": 3},
    }
    strategy = SignalStrategy(
        {
            "stake_currency": "USDT",
            "signal_strategy": {
                "store": store,
                "equity": 1000,
                "risk_pct": 0.01,
                "default_leverage": 1,
                "max_leverage": 5,
                "kill_switch_enabled": kill_switch_enabled,
            },
        }
    )
    strategy.signals_by_id = {"sig-long": signal}
    strategy.entry_signals_by_pair = {"BTC/USDT:USDT": [signal]}
    return strategy


def test_parse_directives_matches_close_keywords():
    directives = extract_directives("会员们正在平仓 #SYRUP 合约", "SYRUP/USDT:USDT", 4328)

    assert directive_kinds(directives) == [DirectiveKind.CLOSE]
    assert directives[0].pair == "SYRUP/USDT:USDT"
    assert directives[0].source_message_id == 4328


def test_parse_directives_matches_partial_close_keywords_and_fraction():
    directives = extract_directives("已锁定50%的利润", "BTC/USDT:USDT", 4935)

    assert directive_kinds(directives) == [DirectiveKind.PARTIAL_CLOSE]
    assert directives[0].fraction == 0.5


def test_parse_directives_defaults_partial_fraction_to_half():
    directives = extract_directives("锁定部分利润，规避风险", "WLD/USDT:USDT", 4943)

    assert directive_kinds(directives) == [DirectiveKind.PARTIAL_CLOSE]
    assert directives[0].fraction == 0.5


def test_parse_directives_matches_move_sl_to_entry_keywords():
    directives = extract_directives("move SL to entry and make it risk free", "BTC/USDT:USDT", 1)

    assert directive_kinds(directives) == [DirectiveKind.MOVE_SL_TO_ENTRY]


def test_parse_directives_matches_move_sl_price_keywords():
    directives = extract_directives("stop loss moved to 102.5", "BTC/USDT:USDT", 1)

    assert directive_kinds(directives) == [DirectiveKind.MOVE_SL]
    assert directives[0].price == 102.5


def test_parse_directives_returns_multiple_directives():
    directives = extract_directives("$HYPE 锁定50%+无风险", "HYPE/USDT:USDT", 4940)

    assert directive_kinds(directives) == [
        DirectiveKind.PARTIAL_CLOSE,
        DirectiveKind.MOVE_SL_TO_ENTRY,
    ]
    assert directives[0].fraction == 0.5


def test_parse_directives_ignores_analysis_noise():
    text = "如果收盘价高于1657，就可以做多单，目标价位更高。"

    assert extract_directives(text, "ETH/USDT:USDT", 4946) == []


def test_store_lifecycle_save_get_pending_consume():
    store = InMemorySignalStore()
    directive = PositionDirective(kind=DirectiveKind.CLOSE, pair="BTC/USDT:USDT")

    store.save_directive(directive)
    pending = store.get_pending_directives("BTC/USDT:USDT")
    store.consume_directive(pending[0].directive_id)

    assert pending == [directive]
    assert store.get_pending_directives("BTC/USDT:USDT") == []
    assert [event["type"] for event in store.audit_events] == [
        "directive_saved",
        "directive_consumed",
    ]


def test_ttl_expiry_marks_directive_expired_and_hides_from_pending():
    store = InMemorySignalStore()
    directive = PositionDirective(
        kind=DirectiveKind.CLOSE,
        pair="BTC/USDT:USDT",
        created_at=datetime.now(timezone.utc) - timedelta(hours=25),
    )

    store.save_directive(directive)
    expired = store.expire_old_directives()

    assert directive.is_expired() is True
    assert expired == [directive.directive_id]
    assert store.get_pending_directives("BTC/USDT:USDT") == []
    assert store.audit_events[-1]["type"] == "directive_expired"


def test_sqlite_store_persists_directive_lifecycle(tmp_path):
    store = SQLiteSignalStore(tmp_path / "signals.sqlite")
    directive = PositionDirective(
        kind=DirectiveKind.PARTIAL_CLOSE,
        pair="BTC/USDT:USDT",
        fraction=0.5,
        source_message_id=4935,
    )

    store.save_directive(directive)
    restarted = SQLiteSignalStore(tmp_path / "signals.sqlite")
    pending = restarted.get_pending_directives("BTC/USDT:USDT")
    restarted.consume_directive(pending[0].directive_id)

    assert len(pending) == 1
    assert pending[0].kind == DirectiveKind.PARTIAL_CLOSE
    assert pending[0].fraction == 0.5
    assert restarted.get_pending_directives("BTC/USDT:USDT") == []


def test_importer_saves_directives_for_update_messages(tmp_path):
    item = {
        "id": 4935,
        "chatId": "-1001",
        "chatTitle": "TraderGauls",
        "date": "2026-06-01T12:00:00+00:00",
        "text": "🎯 比特币 交易更新\n\n➡️ 已锁定50%的利润",
        "media": [],
    }
    db_path = tmp_path / "signals.sqlite"

    import_signal_items([item], f"sqlite:///{db_path}")
    directives = SQLiteSignalStore(db_path).get_pending_directives("BTC/USDT:USDT")

    assert directive_kinds(directives) == [DirectiveKind.PARTIAL_CLOSE]
    assert directives[0].fraction == 0.5


def test_strategy_close_directive_overrides_take_profit_priority():
    store = InMemorySignalStore()
    store.save_directive(PositionDirective(kind=DirectiveKind.CLOSE, pair="BTC/USDT:USDT"))
    strategy = make_strategy(store)
    trade = FakeTrade()

    result = strategy.custom_exit(
        "BTC/USDT:USDT",
        trade,
        datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        111.0,
        0.1,
    )

    assert result == "directive_close"
    assert store.get_pending_directives("BTC/USDT:USDT") == []


def test_strategy_partial_close_directive_adjusts_position():
    store = InMemorySignalStore()
    store.save_directive(
        PositionDirective(kind=DirectiveKind.PARTIAL_CLOSE, pair="BTC/USDT:USDT", fraction=0.5)
    )
    strategy = make_strategy(store)
    trade = FakeTrade(stake_amount=200.0)

    result = strategy.adjust_trade_position(
        trade,
        datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        105.0,
        0.05,
        10.0,
        1000.0,
        100.0,
        105.0,
        0.05,
        0.0,
    )

    assert result == -100.0
    assert store.get_pending_directives("BTC/USDT:USDT") == []


def test_strategy_move_sl_to_entry_overrides_static_breakeven_threshold():
    store = InMemorySignalStore()
    store.save_directive(PositionDirective(kind=DirectiveKind.MOVE_SL_TO_ENTRY, pair="BTC/USDT:USDT"))
    strategy = make_strategy(store)
    trade = FakeTrade(open_rate=100.0)

    result = strategy.custom_stoploss(
        "BTC/USDT:USDT",
        trade,
        datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        101.0,
        0.01,
    )

    assert round(result, 6) == -0.01
    assert store.get_pending_directives("BTC/USDT:USDT") == []


def test_strategy_kill_switch_blocks_stoploss_directive():
    store = InMemorySignalStore()
    store.save_directive(PositionDirective(kind=DirectiveKind.MOVE_SL_TO_ENTRY, pair="BTC/USDT:USDT"))
    strategy = make_strategy(store, kill_switch_enabled=True)
    trade = FakeTrade(open_rate=100.0)

    result = strategy.custom_stoploss(
        "BTC/USDT:USDT",
        trade,
        datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        101.0,
        0.01,
    )

    assert result != -0.01
    assert directive_kinds(store.get_pending_directives("BTC/USDT:USDT")) == [
        DirectiveKind.MOVE_SL_TO_ENTRY
    ]


def load_fixture_message(message_id):
    payload = json.loads(Path("fixtures/signals/telegram_latest20/messages.latest20.json").read_text())
    for item in payload:
        if int(item["id"]) == message_id:
            return item
    raise AssertionError(f"message {message_id} not found")


def directives_for_fixture_message(message_id):
    raw_message = normalize_watcher_signal(load_fixture_message(message_id))
    signal = parse_signal(raw_message)
    return extract_directives(
        raw_message["raw_text"],
        signal.pair_freqtrade,
        int(raw_message["source_message_id"]),
    )


def test_real_corpus_fixture_replay():
    expected = {
        4328: [(DirectiveKind.CLOSE, None)],
        4935: [(DirectiveKind.PARTIAL_CLOSE, 0.5)],
        4940: [(DirectiveKind.PARTIAL_CLOSE, 0.5), (DirectiveKind.MOVE_SL_TO_ENTRY, None)],
        4943: [(DirectiveKind.PARTIAL_CLOSE, 0.5)],
        4937: [],
        4329: [],
        4941: [],
        4946: [],
    }

    actual = {}
    for message_id in expected:
        directives = directives_for_fixture_message(message_id)
        actual[message_id] = [
            (directive.kind, directive.fraction if directive.kind == DirectiveKind.PARTIAL_CLOSE else None)
            for directive in directives
        ]

    assert actual == expected
