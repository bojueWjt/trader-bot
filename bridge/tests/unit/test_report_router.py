from app.reports.router import _trades_summary


def test_trades_summary_counts_realized_pnl_wins_and_losses():
    summary = _trades_summary(
        [
            {"trade_id": "win", "realized_pnl": 12.5},
            {"trade_id": "loss", "realized_pnl": -3.25},
            {"trade_id": "flat", "realized_pnl": 0},
            {"trade_id": "missing"},
        ]
    )

    assert summary == {
        "closed_today_count": 4,
        "closed_count": 4,
        "win_count": 1,
        "loss_count": 1,
    }
