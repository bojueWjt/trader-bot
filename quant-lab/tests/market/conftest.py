"""G2 market 测试公共夹具。只用合成数据与公开归档，零生产凭据。"""
from __future__ import annotations

import pathlib
import datetime as dt
import json

import polars as pl
import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
EPISODES = FIXTURES / "episodes"


@pytest.fixture(scope="session")
def fixtures_dir() -> pathlib.Path:
    return FIXTURES


@pytest.fixture(scope="session")
def episodes_dir() -> pathlib.Path:
    return EPISODES


@pytest.fixture
def lake_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "lake" / "market"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def synthetic_archive_lake(tmp_path):
    """Former archive smokes keep their checks without reading local real data."""
    from quant_lab.market.partition_check import RULES_SCHEMA, write_rules
    from quant_lab.market.vision import LakePaths, partition_id
    paths = LakePaths(tmp_path / "synthetic-market")
    inst = "BTCUSDT-PERP.BINANCE-UM"
    start = dt.datetime(2024, 1, 15, 11, 59, tzinfo=dt.UTC)
    times = [start + dt.timedelta(minutes=i) for i in range(361)]
    mark_path = None
    for kind in ("klines", "markPriceKlines"):
        frame = pl.DataFrame({"instrument_id": [inst] * len(times), "open_time": times,
            "close_time": [t + dt.timedelta(minutes=1) for t in times],
            "available_at": [t + dt.timedelta(minutes=1) for t in times],
            "open": [42000.0] * len(times), "high": [42010.0] * len(times),
            "low": [41990.0] * len(times), "close": [42000.0] * len(times),
            "volume": [10000.0] * len(times), "gap_flag": [False] * len(times),
            "ohlc_valid": [True] * len(times), "source_sha256": ["synthetic"] * len(times)})
        path = paths.silver_dir(kind, "1m", "BTCUSDT") / "date=2024-01-15" / "part.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(path)
        if kind == "markPriceKlines":
            mark_path = path
        pid = partition_id(kind, "1m", "BTCUSDT", "2024-01")
        manifest = paths.manifest(pid)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"partition_id": pid, "check_status": "ok", "source_sha256": "synthetic"}))
    funding = paths.silver_dir("fundingRate", "8h", "BTCUSDT") / "date=2024-01-15" / "part.parquet"
    funding.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"calc_time": [start.replace(hour=h, minute=0) for h in (0, 8, 16)],
                  "funding_rate": [0.0] * 3, "funding_interval_hours": [8] * 3}).write_parquet(funding)
    paths.manifest(partition_id("fundingRate", "8h", "BTCUSDT", "2024-01")).write_text(
        json.dumps({"check_status": "ok", "source_sha256": "synthetic"}))
    write_rules(paths, pl.DataFrame([{"instrument_id": inst, "effective_from": start - dt.timedelta(days=1),
        "effective_to": None, "tick_size": "0.1", "step_size": "0.001", "min_notional": "0",
        "multiplier": "1", "funding_interval_hours": 8, "status": "TRADING", "source": "synthetic"}], schema=RULES_SCHEMA))
    return paths.root, mark_path
