import json
from pathlib import Path

from freqtrade.signal_strategy.domain import SignalStatus
from freqtrade.signal_strategy.importer import normalize_watcher_signal
from freqtrade.signal_strategy.parser import parse_signal
from freqtrade.signal_strategy.store import InMemorySignalStore


FIXTURES = Path(__file__).parents[2] / "fixtures"
GOLDEN_SIGNALS = FIXTURES / "signals" / "golden_signals.json"


def load_samples():
    with GOLDEN_SIGNALS.open(encoding="utf-8") as handle:
        return {item["sample_id"]: item for item in json.load(handle)}


def test_gs_002_parses_btc_long_approved_candidate():
    samples = load_samples()

    signal = parse_signal(samples["GS-002"], pair_whitelist={"BTC/USDT:USDT"})

    assert signal.signal_id == "-1002328068747:19"
    assert signal.pair_raw == "BTCUSDT"
    assert signal.pair_freqtrade == "BTC/USDT:USDT"
    assert signal.side == "long"
    assert signal.entry.mode == "cmp"
    assert signal.stop_loss == 70400
    assert [item.price for item in signal.take_profits] == [72000]
    assert signal.status == SignalStatus.PARSED
    assert signal.review_reason_codes == []


def test_negative_channel_id_generates_cli_safe_signal_id():
    raw_signal = normalize_watcher_signal(
        {
            "chat_id": -1001234567,
            "message_id": 42,
            "chat_title": "HYPE signals",
            "date": "2026-02-08T16:32:18.000Z",
            "text": "HYPEUSDT LONG\nEntry: 0.042\nSL: 0.038\nTP: 0.050",
        }
    )

    signal = parse_signal(raw_signal, pair_whitelist={"HYPE/USDT:USDT"})

    assert signal.signal_id == "sig-c1001234567-m42"
    assert not signal.signal_id.startswith("-")
    assert signal.source_channel_id == "-1001234567"
    assert signal.source_message_id == "42"
    assert signal.pair_freqtrade == "HYPE/USDT:USDT"
    assert signal.status == SignalStatus.PARSED


def test_legacy_signal_id_format_remains_readable_and_queryable():
    legacy_signal_id = "-1001234567:42"

    signal = parse_signal(
        {
            "signal_id": legacy_signal_id,
            "source": "telegram",
            "source_channel_id": "-1001234567",
            "source_channel_name": "legacy signals",
            "source_message_id": "42",
            "received_at": "2026-02-08T16:32:18.000Z",
            "raw_text": "BTCUSDT LONG\nEntry: 42000\nSL: 41000\nTP: 43000",
            "media": [],
        },
        pair_whitelist={"BTC/USDT:USDT"},
    )
    store = InMemorySignalStore()
    store.upsert_signal(signal)

    assert signal.signal_id == legacy_signal_id
    assert store.get_signal(legacy_signal_id).signal_id == legacy_signal_id


def test_gs_003_parses_eth_short_dca_and_missing_tp_review():
    samples = load_samples()

    signal = parse_signal(samples["GS-003"], pair_whitelist={"ETH/USDT:USDT"})

    assert signal.pair_raw == "ETHUSDT"
    assert signal.pair_freqtrade == "ETH/USDT:USDT"
    assert signal.side == "short"
    assert signal.entry.mode == "limit_plus_dca"
    assert signal.entry.primary_price == 2164
    assert signal.entry.dca_prices == [2214]
    assert signal.stop_loss == 2254
    assert signal.take_profits == []
    assert signal.status == SignalStatus.PARSED
    assert "tp_price_missing_from_text" not in signal.review_reason_codes


def test_missing_take_profit_text_price_does_not_require_review_when_stop_loss_exists():
    signal = parse_signal(
        {
            "signal_id": "telegram:4327",
            "source": "telegram",
            "source_channel_id": "-1001",
            "source_channel_name": "coinAlert",
            "source_message_id": "4327",
            "received_at": "2026-06-10T12:00:00+00:00",
            "raw_text": "SYRUPUSDT LONG\nEntry: 0.1307\n补仓: 0.12\n止损: 0.11\n止盈目标：请参考图表上的标记",
            "media": [],
        },
        pair_whitelist={"SYRUP/USDT:USDT"},
    )

    assert signal.entry.mode == "limit_plus_dca"
    assert signal.entry.primary_price == 0.1307
    assert signal.take_profits == []
    assert signal.stop_loss == 0.11
    assert signal.status == SignalStatus.PARSED
    assert signal.review_reason_codes == []


def test_numeric_current_market_price_is_cmp_with_primary_price():
    signal = parse_signal(
        {
            "signal_id": "telegram:4327",
            "source": "telegram",
            "source_channel_id": "-1001",
            "source_channel_name": "coinAlert",
            "source_message_id": "4327",
            "received_at": "2026-06-10T12:00:00+00:00",
            "raw_text": "SYRUP 买入交易计划\n👉 首次建仓：当前市价（0.1307美元）\n👉 补仓位置：0.1249美元\n严格止损：0.1204美元区域",
            "media": [],
        },
        pair_whitelist={"SYRUP/USDT:USDT"},
    )

    assert signal.entry.mode == "cmp_plus_dca"
    assert signal.entry.primary_price == 0.1307
    assert signal.entry.dca_prices == [0.1249]
    assert signal.stop_loss == 0.1204
    assert signal.review_reason_codes == []


def test_cmp_upper_bound_is_extracted_as_entry_price_max():
    signal = parse_signal(
        {
            "signal_id": "telegram:4938",
            "source": "telegram",
            "source_channel_id": "-1001",
            "source_channel_name": "coinAlert",
            "source_message_id": "4938",
            "received_at": "2026-06-10T12:00:00+00:00",
            "raw_text": "HYPEUSDT LONG\nEntry: CMP 至 61\nSL: 55\nTP: 70",
            "media": [],
        },
        pair_whitelist={"HYPE/USDT:USDT"},
    )

    assert signal.entry.mode == "cmp"
    assert signal.entry.primary_price is None
    assert signal.entry.price_max == 61
    assert signal.review_reason_codes == []


def test_gs_005_marks_short_price_geometry_invalid_candidate_data():
    samples = load_samples()

    signal = parse_signal(
        samples["GS-005"],
        pair_whitelist={"ZKP/USDT:USDT"},
        current_prices={"ZKP/USDT:USDT": 0.1037},
    )

    assert signal.pair_freqtrade == "ZKP/USDT:USDT"
    assert signal.side == "short"
    assert signal.entry.mode == "cmp"
    assert signal.stop_loss == 0.0761
    assert [item.price for item in signal.take_profits] == [0.1091]
    assert signal.status == SignalStatus.NEEDS_REVIEW
    assert "short_price_geometry_invalid" in signal.review_reason_codes
