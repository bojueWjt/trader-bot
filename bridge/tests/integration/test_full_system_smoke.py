from datetime import datetime, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.signal_parser import SignalStatus, parse_signal
from app.services.signal_store import SignalStore
from user_data.strategies.SignalStrategy import SignalStrategy

from tests.unit.test_signalstrategy_loading import FakeDataFrame


class FakeDryRunAdapter:
    def __init__(self):
        self.trades = []

    def process_entry(self, dataframe, pair):
        if dataframe["enter_long"].sum() == 0:
            return False
        entry_tag = dataframe.columns["enter_tag"][-1]
        trade = FakeTrade(pair=pair, entry_tag=entry_tag)
        self.trades.append(trade)
        return trade


class FakeTrade:
    id = 2001
    is_short = False

    def __init__(self, pair, entry_tag):
        self.pair = pair
        self.entry_tag = entry_tag


def test_gs_002_full_system_smoke_reaches_dashboard_and_report():
    signal_store = SignalStore()
    signal = _load_gs_002()
    signal.status = SignalStatus.APPROVED
    signal_store.upsert_signal(signal)
    strategy = SignalStrategy({"signal_strategy": {"store": signal_store, "lookback_minutes": 240}})
    dryrun = FakeDryRunAdapter()
    current_time = datetime(2026, 2, 8, 16, 35, tzinfo=timezone.utc)

    strategy.bot_loop_start(current_time)
    dataframe = strategy.populate_entry_trend(FakeDataFrame(), {"pair": "BTC/USDT:USDT"})
    trade = dryrun.process_entry(dataframe, "BTC/USDT:USDT")
    strategy.order_filled("BTC/USDT:USDT", trade, object(), current_time)

    app = create_app()
    app.state.signal_store = signal_store
    client = TestClient(app)
    dashboard = client.get(
        "/api/dashboard/overview",
        headers={"authorization": "Bearer test-viewer-token"},
    )
    report = client.get("/api/reports/daily/2026-02-08/markdown")

    assert signal_store.get_signal("-1002328068747:19").status == SignalStatus.ENTERED
    assert len(dryrun.trades) == 1
    assert dashboard.status_code == 200
    assert "risk_state" in dashboard.json()
    assert report.status_code == 200
    assert "-1002328068747:19" in report.json()["markdown"]


def _load_gs_002():
    fixture_path = Path("fixtures/signals/golden_signals.json")
    samples = json.loads(fixture_path.read_text())
    for sample in samples:
        if sample["sample_id"] == "GS-002":
            return parse_signal(sample, pair_whitelist={"BTC/USDT:USDT"})
    raise AssertionError("GS-002 fixture missing")
