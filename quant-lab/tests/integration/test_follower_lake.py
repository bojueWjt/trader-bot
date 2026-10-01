"""Synthetic silver lake -> loader -> follower kernel -> reporting boundaries."""
import datetime as dt
from decimal import Decimal as D
import json

import polars as pl
import pytest

from quant_lab.market import contract as c, execution as x, l0_replay as l0, partition_check as pc
from quant_lab.market.vision import LakePaths, partition_id
from tests.market.test_follower_execution import T, bar, case, fills


def lake(root, bars, funding=None, bad_at=None):
    paths=LakePaths(root)
    for kind in ("klines","markPriceKlines"):
        frame=pl.DataFrame({"open_time":[b.open_time for b in bars],"open":[float(b.o) for b in bars],
            "high":[float(b.h) for b in bars],"low":[float(b.l) for b in bars],"close":[float(b.c) for b in bars],
            "volume":[float(b.volume) for b in bars],"gap_flag":[False]*len(bars),
            "ohlc_valid":[b.open_time!=bad_at for b in bars],"source_sha256":["synthetic"]*len(bars)})
        for day in sorted({b.open_time.date() for b in bars}):
            dest=paths.silver_dir(kind,"1m","BTCUSDT")/f"date={day}"/"part.parquet"
            dest.parent.mkdir(parents=True,exist_ok=True)
            frame.filter(pl.col("open_time").dt.date()==day).write_parquet(dest)
        manifest=paths.manifest(partition_id(kind,"1m","BTCUSDT","2024-01"))
        manifest.parent.mkdir(parents=True,exist_ok=True)
        manifest.write_text(json.dumps({"partition_id":kind,"check_status":"ok","source_sha256":"synthetic"}))
    if funding:
        df=pl.DataFrame({"calc_time":[r.calc_time for r in funding],"funding_rate":[float(r.rate) for r in funding],
                         "funding_interval_hours":[r.interval_hours for r in funding]})
        for day in sorted({r.calc_time.date() for r in funding}):
            dest=paths.silver_dir("fundingRate","8h","BTCUSDT")/f"date={day}"/"part.parquet"
            dest.parent.mkdir(parents=True,exist_ok=True)
            df.filter(pl.col("calc_time").dt.date()==day).write_parquet(dest)
        # Monthly quarantine must not erase valid settlement rows in the holding period.
        paths.manifest(partition_id("fundingRate","8h","BTCUSDT","2024-01")).write_text(json.dumps({"check_status":"quarantined","source_sha256":"synthetic"}))
    pc.write_rules(paths,pl.DataFrame([{"instrument_id":"BTCUSDT-PERP.BINANCE-UM","effective_from":T-dt.timedelta(days=1),
        "effective_to":None,"tick_size":"1","step_size":"1","min_notional":"0","multiplier":"1",
        "funding_interval_hours":8,"status":"TRADING","source":"synthetic"}],schema=pc.RULES_SCHEMA))


def test_loader_preminute_and_quarantined_funding_month_are_evaluable(tmp_path):
    start=T+dt.timedelta(minutes=1,seconds=30)
    req=case(start=start,tps=[c.TakeProfit(level=110,fraction=1)])
    # A passive buy is already marketable at submission, so q(t-) exists at 08:00.
    req=c.ExecutionRequest.model_validate({**req.model_dump(),"order_plan":req.order_plan.model_copy(update={
        "entries":[c.Entry(kind="limit",price_lo=100,price_hi=100)]})})
    rows=[c.FundingRow(calc_time=T+dt.timedelta(minutes=2),rate=D(".001"),interval_hours=8)]
    lake(tmp_path,[bar(0),bar(1),bar(2),bar(3,o=110)],rows)
    market=x.load_market_from_lake(req,lake_root=tmp_path)
    assert any(b.open_time==T+dt.timedelta(minutes=1) for b in market.bars_mark)
    result=x.simulate(req,market=market)
    assert fills(result,"entry")
    assert fills(result,"entry")[0].ts==start
    assert result.censor_reason is None and result.funding==D("-.2")
    assert x.result_row(req,result)["evaluable"]


@pytest.mark.parametrize("closed",[True,False])
def test_lake_quality_gap_and_missing_tail_only_affect_open_position(tmp_path,closed):
    req=case(tps=[c.TakeProfit(level=110,fraction=1)] if closed else [])
    lake(tmp_path,[bar(0),bar(1,o=110 if closed else 100),bar(2),bar(3)],bad_at=T+dt.timedelta(minutes=2))
    market=x.load_market_from_lake(req,lake_root=tmp_path)
    result=x.simulate(req,market=market)
    row=x.result_row(req,result)
    assert result.censor_reason==(None if closed else "BAR_GAP")
    assert row["evaluable"] is closed
    assert l0.metrics([row])["n_evaluable"]==int(closed)


def test_lake_holding_period_missing_settlement_row_censors(tmp_path):
    req=case(tps=[c.TakeProfit(level=110,fraction=1)])
    # Prior row proves 8h schedule; 08:00 is absent while the position is open.
    rows=[c.FundingRow(calc_time=T.replace(hour=0,minute=0),rate=D(".001"),interval_hours=8)]
    lake(tmp_path,[bar(i,o=110 if i==3 else 100) for i in range(4)],rows)
    result=x.simulate(req,market=x.load_market_from_lake(req,lake_root=tmp_path))
    assert result.censor_reason=="FUNDING_SCHEDULE_GAP" and result.net_R is None
