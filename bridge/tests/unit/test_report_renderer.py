from app.services.report_renderer import render
from app.services.report_snapshot import DailySnapshot


def test_render_includes_required_sections_and_known_values():
    snapshot = DailySnapshot(
        date="2026-06-10",
        account={"equity": 1000},
        trades=[{"trade_id": "trade-1", "pair": "BTC/USDT:USDT"}],
        signals=[{"signal_id": "signal-1", "pair": "ETH/USDT:USDT"}],
        risk={"risk_state": "normal"},
        positions=[{"position_id": "pos-1", "pair": "SOL/USDT:USDT"}],
        collected_at="2026-06-10T12:00:00+00:00",
    )

    markdown = render(snapshot)

    for header in ["## Account", "## Trades", "## Signals", "## Risk", "## Positions"]:
        assert header in markdown
    assert "BTC/USDT:USDT" in markdown
    assert "ETH/USDT:USDT" in markdown
    assert "normal" in markdown
