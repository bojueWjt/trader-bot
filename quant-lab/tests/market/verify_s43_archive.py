"""Read-only archive loader check and real-model/parquet S43 reproduction."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
import tempfile

from quant_lab.market import contract as c, execution
from quant_lab.market.kernel_a import KernelA
from tests.market.test_single_source import _request, _write_lake


def reproduction():
    start = dt.datetime(2024, 1, 1, 1, tzinfo=dt.UTC)
    end = start + dt.timedelta(minutes=3)
    req = _request(t_dec=start, t_start=None, horizon_end=end)
    opens = [start + dt.timedelta(microseconds=value) for value in (0, 59999999, 120000000)]
    bars = [c.Bar(open_time=time, interval_s=60, o=Decimal(100), h=Decimal(100),
                  l=Decimal(100), c=Decimal(100)) for time in opens]
    market = c.MarketView(manifest_id=req.market_manifest, bars_last=bars, bars_mark=bars)
    result = execution.simulate(req, market=market)
    gap = KernelA(req, market)._first_bar_gap(bars, end)
    print(f'S43 AFTER explicit FIRST_GAP={gap} censor_reason={result.censor_reason} '
          f'bars_ok={result.coverage_mask.bars_ok}', flush=True)
    assert gap == start + dt.timedelta(minutes=1)
    assert result.censor_reason == 'BAR_GAP' and not result.coverage_mask.bars_ok
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
        _write_lake(Path(root), start, end, opens)
        loaded = execution.load_market_from_lake(req, lake_root=root)
        result = execution.simulate(req, market=loaded)
        print(f'S43 AFTER loader bars_complete={loaded.bars_complete} '
              f'bars_quality_ok={loaded.bars_quality_ok} bars_ok={result.coverage_mask.bars_ok}', flush=True)
        for note in loaded.quality_notes:
            if 'off-grid' in note or '网格' in note:
                print(note, flush=True)
        assert not loaded.bars_complete and not loaded.bars_quality_ok
        assert not result.coverage_mask.bars_ok


def archive():
    # Daily caller windows stay within the existing month and the 14-day safety cap.
    # The final microsecond carries no expected 1m bar or funding settlement.
    streams = {'bars_last': {}, 'bars_mark': {}}
    funding = {}
    for day in range(1, 32):
        start = dt.datetime(2024, 1, day, tzinfo=dt.UTC)
        end = start + dt.timedelta(days=1) - dt.timedelta(microseconds=1)
        req = _request(t_dec=start, t_start=None, horizon_end=end)
        market = execution.load_market_from_lake(req, lake_root='data/lake/market')
        print(f'BTCUSDT {start.date()} last={len(market.bars_last)} mark={len(market.bars_mark)} '
              f'bars_complete={market.bars_complete} bars_quality_ok={market.bars_quality_ok} '
              f'funding_complete={market.funding_schedule_complete}', flush=True)
        assert market.bars_complete and market.bars_quality_ok, market.quality_notes
        assert market.funding_schedule_complete, market.quality_notes
        for name, collected in streams.items():
            for bar in getattr(market, name):
                assert bar.open_time not in collected
                collected[bar.open_time] = bar
        for row in market.funding:
            funding[row.calc_time] = row
    for name, collected in streams.items():
        off_grid = sum(c.first_grid_point(time, 60) != time for time in collected)
        print(f'ARCHIVE {name}: rows={len(collected)} off_grid={off_grid} bars_complete=True (31/31 windows)')
        assert len(collected) == 44640 and off_grid == 0
    jitter = sum(c.first_grid_point(time, 8 * 3600) != time for time in funding)
    print(f'ARCHIVE fundingRate: rows={len(funding)} jitter_rows={jitter} retained=True funding_complete=True')
    assert len(funding) == 93 and jitter == 15


if __name__ == '__main__':
    reproduction()
    archive()
