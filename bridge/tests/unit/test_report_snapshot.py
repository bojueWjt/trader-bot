import asyncio

from app.services.report_snapshot import build_snapshot
from app.services.signal_parser import parse_signal
from app.services.signal_store import SignalStore


def test_build_snapshot_includes_signal_store_signals():
    store = SignalStore()
    signal = parse_signal(
        {
            "signal_id": "report-signal-1",
            "source": "telegram",
            "source_channel_id": "-1001",
            "source_channel_name": "coinAlert",
            "source_message_id": "1",
            "received_at": "2026-06-10T12:00:00+00:00",
            "raw_text": "BTCUSDT LONG Entry: 100 SL: 90 TP: 110",
            "media": [],
        },
        pair_whitelist={"BTC/USDT:USDT"},
    )
    store.upsert_signal(signal)

    snapshot = asyncio.run(build_snapshot(date="2026-06-10", signal_store=store))

    assert any(item["signal_id"] == "report-signal-1" for item in snapshot.signals)
    assert snapshot.signals[0]["pair"] == "BTC/USDT:USDT"
