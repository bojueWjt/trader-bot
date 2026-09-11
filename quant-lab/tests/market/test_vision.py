"""M-03：Binance Vision 下载器 + manifest。全部走 httpx.MockTransport 合成包，零真实网络。"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import zipfile

import httpx
import polars as pl
import pytest

from quant_lab.market import vision as v


def make_zip(rows: list[list], member: str, header: list[str] | None = None) -> bytes:
    buf = io.StringIO()
    if header:
        buf.write(",".join(header) + "\n")
    for r in rows:
        buf.write(",".join(str(x) for x in r) + "\n")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, buf.getvalue())
    return out.getvalue()


def bar_rows(start: dt.datetime, n: int, step_s: int = 60, price: float = 100.0) -> list[list]:
    rows = []
    for i in range(n):
        ot = int((start + dt.timedelta(seconds=i * step_s)).timestamp() * 1000)
        rows.append([ot, price, price + 1, price - 1, price + 0.5, 10, ot + step_s * 1000 - 1, 1000, 5, 4, 400, 0])
    return rows


JAN = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


def mock_client(files: dict[str, bytes], checksums: dict[str, str] | None = None, fail_primary: bool = False):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        path = req.url.path.lstrip("/")
        if path.startswith("data.binance.vision/"):  # S3 镜像带桶名前缀
            path = path[len("data.binance.vision/"):]
        if fail_primary and req.url.host == "data.binance.vision":
            return httpx.Response(503)
        if path.endswith(".CHECKSUM"):
            base = path[: -len(".CHECKSUM")]
            if checksums and base in checksums:
                return httpx.Response(200, text=f"{checksums[base]}  {base.rsplit('/', 1)[-1]}\n")
            return httpx.Response(404)
        if path in files:
            return httpx.Response(200, content=files[path], headers={"ETag": '"abc"', "Last-Modified": "x"})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


REL = "data/futures/um/monthly/markPriceKlines/BTCUSDT/1m/BTCUSDT-1m-2024-01.zip"


def test_source_paths():
    assert v.source_path("markPriceKlines", "1m", "BTCUSDT", "2024-01") == REL
    assert v.source_path("fundingRate", "8h", "BTCUSDT", "2024-01") == "data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip"
    assert v.source_path("metrics", "5m", "BTCUSDT", "2024-01-05") == "data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2024-01-05.zip"
    with pytest.raises(v.VisionError):
        v.source_path("metrics", "5m", "BTCUSDT", "2024-01")


def test_full_month_ok_manifest(lake_dir):
    n = 31 * 24 * 60
    z = make_zip(bar_rows(JAN, n), "BTCUSDT-1m-2024-01.csv")
    sha = hashlib.sha256(z).hexdigest()
    client, calls = mock_client({REL: z}, {REL: sha})
    lake = v.LakePaths(lake_dir)
    m = v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client)
    assert m.actual_rows == n == m.expected_rows and m.missing == 0 and m.duplicates == 0 and m.status == "ok"
    assert m.checksum_source == "vision_CHECKSUM" and m.source_sha256 == sha
    mf = json.loads(lake.manifest(m.partition_id).read_text())
    assert mf["actual_rows"] > 40000 and mf["schema_hash"] and mf["available_at_basis"] == "H0_close_plus_0s"
    assert len(mf["days"]) == 31
    # bronze 带 sha256 侧车
    b = lake.bronze("markPriceKlines", "1m", "BTCUSDT", "2024-01")
    assert b.exists() and b.with_suffix(".zip.sha256").read_text().startswith(sha)
    # silver 按日分区，时间为 UTC 微秒，close_time = open_time + 60s（右端点）
    part = pl.read_parquet(lake.silver_dir("markPriceKlines", "1m", "BTCUSDT") / "date=2024-01-01" / "part.parquet")
    assert part.height == 1440 and part.schema["open_time"] == pl.Datetime("us", "UTC")
    assert (part["close_time"] - part["open_time"]).unique().to_list() == [dt.timedelta(minutes=1)]
    assert (part["close_time_raw"] - part["open_time"]).unique().to_list() == [dt.timedelta(minutes=1) - dt.timedelta(milliseconds=1)]
    assert part["ohlc_valid"].all() and part["instrument_id"][0] == "BTCUSDT-PERP.BINANCE-UM"
    # 无临时文件残留
    assert not list(lake_dir.rglob(".*.tmp-*"))


def test_idempotent_skip_and_force(lake_dir):
    z = make_zip(bar_rows(JAN, 100), "x.csv")
    client, calls = mock_client({REL: z})
    lake = v.LakePaths(lake_dir)
    m1 = v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client)
    assert m1.checksum_source == "computed"
    t1 = lake.manifest(m1.partition_id).stat().st_mtime_ns
    m2 = v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client)
    assert isinstance(m2, dict) and m2["source_sha256"] == m1.source_sha256
    assert lake.manifest(m1.partition_id).stat().st_mtime_ns == t1  # 未重写
    m3 = v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client, force=True)
    assert m3.actual_rows == 100 and m3.status == "gap" and m3.missing == 31 * 1440 - 100


def test_gap_and_duplicates_counted_not_filled(lake_dir):
    rows = bar_rows(JAN, 10)
    rows.append(list(rows[3]))          # 重复键
    del rows[5]                          # 缺口
    z = make_zip(rows, "x.csv")
    client, _ = mock_client({REL: z})
    m = v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=v.LakePaths(lake_dir), client=client)
    assert m.actual_rows == 10 and m.distinct_keys == 9 and m.duplicates == 1 and m.status == "gap"
    part = pl.read_parquet(v.LakePaths(lake_dir).silver_dir("markPriceKlines", "1m", "BTCUSDT") / "date=2024-01-01" / "part.parquet")
    assert part.height == 9                       # silver 去重，不插值
    assert part["open_time"].is_sorted()


def test_checksum_mismatch_quarantines_and_no_silver(lake_dir):
    z = make_zip(bar_rows(JAN, 5), "x.csv")
    client, _ = mock_client({REL: z}, {REL: "0" * 64})
    lake = v.LakePaths(lake_dir)
    with pytest.raises(v.RawHashMismatch):
        v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client)
    q = lake.quarantine(v.partition_id("markPriceKlines", "1m", "BTCUSDT", "2024-01"), "RAW_HASH_MISMATCH")
    assert q.exists() and json.loads(q.read_text())["reason_code"] == "RAW_HASH_MISMATCH"
    assert not lake.manifest(v.partition_id("markPriceKlines", "1m", "BTCUSDT", "2024-01")).exists()
    assert not (lake.root / "silver").exists()


def test_404_records_missing_source(lake_dir):
    client, _ = mock_client({})
    lake = v.LakePaths(lake_dir)
    r = v.fetch("BTCUSDT", "markPriceKlines", "1m", "2019-01", lake=lake, client=client)
    assert r["status"] == "missing_source" and r["reason_code"] == "SOURCE_404"


def test_mirror_fallback_after_retries(lake_dir):
    z = make_zip(bar_rows(JAN, 5), "x.csv")
    client, calls = mock_client({REL: z}, fail_primary=True)
    m = v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=v.LakePaths(lake_dir), client=client)
    assert m.actual_rows == 5 and "amazonaws" in m.source_uri
    assert sum("data.binance.vision" in c and "amazonaws" not in c for c in calls) >= 3  # 主站重试 3 次


def test_header_detection_and_microsecond_timestamps():
    rows = bar_rows(JAN, 3)
    rows_us = [[r[0] * 1000, *r[1:6], r[6] * 1000, *r[7:]] for r in rows]  # 微秒时间戳
    z = make_zip(rows_us, "x.csv", header=v.BAR_COLUMNS)
    df, member = v.parse_zip(z, "klines", "1m", "ETHUSDT")
    assert df.height == 3 and member == "x.csv"
    assert df["open_time"][0] == JAN


def test_funding_and_metrics_parse():
    frow = [[int(JAN.timestamp() * 1000) + i * 8 * 3600 * 1000, 8, 0.0001 * (i + 1)] for i in range(3)]
    df, _ = v.parse_zip(make_zip(frow, "f.csv", header=v.FUNDING_COLUMNS), "fundingRate", "8h", "BTCUSDT")
    assert df.schema["calc_time"] == pl.Datetime("us", "UTC") and df["funding_interval_hours"].to_list() == [8, 8, 8]
    mrow = [["2024-01-05 00:05:00", "BTCUSDT", 1.5, 2.5, 1.1, 1.2, 1.3, 1.4]]
    df, _ = v.parse_zip(make_zip(mrow, "m.csv", header=v.METRICS_COLUMNS), "metrics", "5m", "BTCUSDT")
    assert df["create_time"][0] == dt.datetime(2024, 1, 5, 0, 5, tzinfo=dt.UTC) and df["sum_open_interest"][0] == 1.5


def test_ohlc_invalid_flagged_not_dropped():
    rows = bar_rows(JAN, 2)
    rows[1][3] = 999  # low > high
    df, _ = v.parse_zip(make_zip(rows, "x.csv"), "klines", "1m", "BTCUSDT")
    assert df["ohlc_valid"].to_list() == [True, False] and df.height == 2


def test_coverage_and_months(lake_dir):
    assert v.months("2023-11", "2024-02") == ["2023-11", "2023-12", "2024-01", "2024-02"]
    lake = v.LakePaths(lake_dir)
    assert v.coverage(lake).height == 0
    client, _ = mock_client({REL: make_zip(bar_rows(JAN, 5), "x.csv")})
    v.fetch("BTCUSDT", "markPriceKlines", "1m", "2024-01", lake=lake, client=client)
    cov = v.coverage(lake)
    assert cov.height == 1 and cov["instrument_id"].n_unique() == 1
