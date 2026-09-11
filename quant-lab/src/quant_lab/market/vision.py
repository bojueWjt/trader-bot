"""quant_lab.market.vision —— Binance Vision 公开归档下载器 + per-partition manifest（契约 §1，M-03）。

数据源只用 https://data.binance.vision 公开归档（无鉴权、无私有 API）。
分区 = 一个源包（月包；metrics 只有日包）。写入用临时文件 + os.replace 原子改名，同一
(data_type, interval, symbol, period, source_sha256) 幂等：manifest 已存在且 sha256 相同则跳过。

布局（相对 lake 根目录，默认 data/lake/market）：
    bronze/binance/um/<data_type>/<interval>/<symbol>/<period>.zip          原包 + 同名 .sha256
    silver/binance/um/<data_type>/<interval>/instrument=<X>/date=<yyyy-mm-dd>/part.parquet
    _manifest/<partition_id>.json
    quarantine/market/<partition_id>.<reason>.json                           入湖闸门失败记录

铁律：尖刺只标不删；缺 bar 不插值不填零；bronze 只读带哈希；时间统一 UTC 微秒。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

import httpx
import polars as pl

from quant_lab.market import MARKET_SCHEMA_VERSION
from quant_lab.market.contract import grid_points_between

PARSER_VERSION = "vision-parser-v1"
RULE_VERSION = "vision-ingest-v1"
VENUE, MARKET = "binance", "um"
PRIMARY_HOST = "https://data.binance.vision"
MIRROR_HOST = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
HOSTS = (PRIMARY_HOST, MIRROR_HOST)

BAR_TYPES = ("klines", "markPriceKlines", "indexPriceKlines", "premiumIndexKlines")
DATA_TYPES = BAR_TYPES + ("fundingRate", "metrics")
# 契约 §1：interval ∈ {1m, 15m, 8h, 5m}；fundingRate 归 8h 槽（真实周期以行内 funding_interval_hours 为准），metrics 归 5m 槽
FIXED_INTERVAL = {"fundingRate": "8h", "metrics": "5m"}
INTERVAL_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400}

BAR_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades",
               "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
FUNDING_COLUMNS = ["calc_time", "funding_interval_hours", "funding_rate"]
METRICS_COLUMNS = ["create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
                   "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
                   "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]

US = "us"


class VisionError(Exception):
    pass


class RawHashMismatch(VisionError):
    """原因码 RAW_HASH_MISMATCH：.CHECKSUM 与实际 sha256 不符，停批并隔离。"""


# ---------------------------------------------------------------------------
# 标识与路径
# ---------------------------------------------------------------------------
def instrument_id(symbol: str) -> str:
    """G2 建议命名 `BTCUSDT-PERP.BINANCE-UM`（契约未决 #1，两者可互转）。"""
    return f"{symbol}-PERP.{VENUE.upper()}-{MARKET.upper()}"


def symbol_of(inst: str) -> str:
    return inst.split("-PERP.")[0]


def normalize_interval(data_type: str, interval: str | None) -> str:
    if data_type in FIXED_INTERVAL:
        return FIXED_INTERVAL[data_type]
    if not interval:
        raise VisionError(f"{data_type} 需要 --interval")
    return interval


def partition_id(data_type: str, interval: str, symbol: str, period: str) -> str:
    # 顺序 symbol→data_type→interval→period，与看板 verify 的 glob *BTCUSDT*markPriceKlines*1m*2024-01* 一致
    return f"{VENUE}-{MARKET}-{symbol}-{data_type}-{interval}-{period}"


def source_path(data_type: str, interval: str, symbol: str, period: str) -> str:
    """Vision 归档相对路径（不含 host）。period=yyyy-mm 月包，yyyy-mm-dd 日包。"""
    scope = "daily" if len(period) == 10 else "monthly"
    if data_type in BAR_TYPES:
        return f"data/futures/um/{scope}/{data_type}/{symbol}/{interval}/{symbol}-{interval}-{period}.zip"
    if data_type == "fundingRate":
        return f"data/futures/um/{scope}/fundingRate/{symbol}/{symbol}-fundingRate-{period}.zip"
    if data_type == "metrics":
        if scope != "daily":
            raise VisionError("metrics 归档只有日包，period 需为 yyyy-mm-dd")
        return f"data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{period}.zip"
    raise VisionError(f"未知 data_type {data_type}")


@dataclass
class LakePaths:
    root: Path

    @classmethod
    def default(cls) -> "LakePaths":
        return cls(Path(os.environ.get("QUANT_LAB_MARKET_LAKE", "data/lake/market")))

    def bronze(self, data_type, interval, symbol, period) -> Path:
        return self.root / "bronze" / VENUE / MARKET / data_type / interval / symbol / f"{period}.zip"

    def silver_dir(self, data_type, interval, symbol) -> Path:
        return self.root / "silver" / VENUE / MARKET / data_type / interval / f"instrument={instrument_id(symbol)}"

    def manifest(self, pid: str) -> Path:
        return self.root / "_manifest" / f"{pid}.json"

    def quarantine(self, pid: str, reason: str) -> Path:
        return self.root.parent.parent / "quarantine" / "market" / f"{pid}.{reason}.json"


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------
def retain_previous_bronze(path: Path, new_sha: str) -> Path | None:
    """bronze 不可变（S06）：同分区源包更新时，旧字节按其 sha256 改名保留，旧 manifest 引用仍可取回。"""
    if not path.exists():
        return None
    old_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if old_sha == new_sha:
        return None
    keep = path.with_name(f"{path.stem}.{old_sha[:12]}.zip")
    if not keep.exists():
        os.replace(path, keep)
    return keep


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_write_json(path: Path, obj) -> None:
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode())


def atomic_write_parquet(path: Path, df: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        df.write_parquet(tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------------------
# 下载（3 次退避重试 → 换镜像；不改环境）
# ---------------------------------------------------------------------------
def http_get(rel: str, *, client: httpx.Client | None = None, retries: int = 3, backoff: float = 1.5,
             hosts: Iterable[str] = HOSTS) -> tuple[bytes | None, str, dict]:
    """返回 (body|None(404), 实际 URL, HEAD 元数据)。5xx/网络错误按 host 各重试 retries 次再换镜像。"""
    own = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    last_err: Exception | None = None
    try:
        for host in hosts:
            url = f"{host}/{rel}"
            for i in range(retries):
                try:
                    r = client.get(url)
                    if r.status_code == 404:
                        return None, url, {}
                    r.raise_for_status()
                    meta = {k: r.headers.get(k) for k in ("etag", "last-modified", "content-length") if r.headers.get(k)}
                    return r.content, url, meta
                except (httpx.HTTPError, httpx.TransportError) as e:  # noqa: PERF203
                    last_err = e
                    time.sleep(backoff ** i * 0.5)
        raise VisionError(f"下载失败（已重试并换镜像）: {rel}: {last_err}")
    finally:
        if own:
            client.close()


def fetch_checksum(rel: str, client=None, hosts: Iterable[str] = HOSTS) -> str | None:
    body, _, _ = http_get(rel + ".CHECKSUM", client=client, hosts=hosts)
    if body is None:
        return None
    m = re.match(r"\s*([0-9a-fA-F]{64})", body.decode("utf-8", "replace"))
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def _to_us(v: str) -> int:
    """Binance 期货时间戳为毫秒；SPOT 2025-01 起为微秒。>1e14 视为微秒，否则毫秒→微秒。"""
    x = int(float(v))
    return x if x > 100_000_000_000_000 else x * 1000


def _metrics_time_us(s: str) -> int:
    d = dt.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.UTC)
    return int(d.timestamp()) * 1_000_000  # 秒级字符串：向下取整到秒，写死


def parse_zip(zip_bytes: bytes, data_type: str, interval: str, symbol: str) -> tuple[pl.DataFrame, str]:
    """ZIP → 标准化 DataFrame（契约修订 §1.1 列）。返回 (df, zip 内成员名)。表头自动检测。"""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise VisionError(f"ZIP 内 CSV 成员数 != 1: {names}")
        text = zf.read(names[0]).decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if r and any(c.strip() for c in r)]
    if rows and not re.match(r"^-?\d", rows[0][0].strip()):
        rows = rows[1:]  # 表头
    inst = instrument_id(symbol)
    if data_type in BAR_TYPES:
        df = _parse_bars(rows, interval, inst)
    elif data_type == "fundingRate":
        df = _parse_funding(rows, inst)
    else:
        df = _parse_metrics(rows, inst)
    return df, names[0]


def _parse_bars(rows, interval, inst) -> pl.DataFrame:
    sec = INTERVAL_SECONDS[interval]
    ot = [_to_us(r[0]) for r in rows]
    ct_raw = [_to_us(r[6]) for r in rows]
    f = lambda i: [float(r[i]) for r in rows]  # noqa: E731
    df = pl.DataFrame({
        "instrument_id": [inst] * len(rows), "interval": [interval] * len(rows),
        "open_time": ot, "close_time": [t + sec * 1_000_000 for t in ot], "close_time_raw": ct_raw,
        "open": f(1), "high": f(2), "low": f(3), "close": f(4), "volume": f(5),
        "quote_volume": f(7), "trades": [int(float(r[8])) for r in rows],
        "taker_buy_volume": f(9), "taker_buy_quote_volume": f(10),
    }, schema_overrides={"open_time": pl.Int64, "close_time": pl.Int64, "close_time_raw": pl.Int64, "trades": pl.Int64})
    for c in ("open_time", "close_time", "close_time_raw"):
        df = df.with_columns(pl.from_epoch(pl.col(c), time_unit=US).dt.replace_time_zone("UTC").alias(c))
    df = df.with_columns([
        pl.col("close_time").alias("event_time"), pl.col("close_time").alias("available_at"),
        ((pl.col("low") <= pl.min_horizontal("open", "close")) & (pl.max_horizontal("open", "close") <= pl.col("high"))
         & (pl.col("low") > 0) & pl.col("open").is_finite() & pl.col("high").is_finite()
         & pl.col("low").is_finite() & pl.col("close").is_finite() & (pl.col("volume") >= 0)).alias("ohlc_valid"),
        pl.lit(False).alias("spike_flag"), pl.lit(False).alias("gap_flag"), pl.lit(None, dtype=pl.Float64).alias("spike_score"),
    ])
    return df


def _parse_funding(rows, inst) -> pl.DataFrame:
    df = pl.DataFrame({
        "instrument_id": [inst] * len(rows),
        "calc_time": [_to_us(r[0]) for r in rows],
        "funding_interval_hours": [int(float(r[1])) for r in rows],
        "funding_rate": [float(r[2]) for r in rows],
    }, schema_overrides={"calc_time": pl.Int64, "funding_interval_hours": pl.Int64})
    df = df.with_columns(pl.from_epoch("calc_time", time_unit=US).dt.replace_time_zone("UTC"))
    return df.with_columns(pl.col("calc_time").alias("event_time"), pl.col("calc_time").alias("available_at"))


def _parse_metrics(rows, inst) -> pl.DataFrame:
    df = pl.DataFrame({
        "instrument_id": [inst] * len(rows),
        "create_time": [_metrics_time_us(r[0]) for r in rows],
        **{c: [float(r[i]) if r[i] != "" else None for r in rows] for i, c in enumerate(METRICS_COLUMNS) if i >= 2},
    }, schema_overrides={"create_time": pl.Int64})
    df = df.with_columns(pl.from_epoch("create_time", time_unit=US).dt.replace_time_zone("UTC"))
    return df.with_columns(pl.col("create_time").alias("event_time"), pl.col("create_time").alias("available_at"))


KEY_COL = {**{t: "open_time" for t in BAR_TYPES}, "fundingRate": "calc_time", "metrics": "create_time"}


MASK_COLS = ("ohlc_valid", "spike_flag", "gap_flag", "spike_score")


def schema_hash(df: pl.DataFrame) -> str:
    """数据 schema 哈希：排除体检 mask 列（它们由 M-04 回填，不属于源数据 schema）。"""
    cols = [[c, str(t)] for c, t in df.schema.items() if c not in MASK_COLS]
    return hashlib.sha256(json.dumps(cols).encode()).hexdigest()


def period_bounds(period: str) -> tuple[dt.datetime, dt.datetime]:
    if len(period) == 7:
        y, m = int(period[:4]), int(period[5:7])
        a = dt.datetime(y, m, 1, tzinfo=dt.UTC)
        b = dt.datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=dt.UTC)
    else:
        a = dt.datetime.fromisoformat(period).replace(tzinfo=dt.UTC)
        b = a + dt.timedelta(days=1)
    return a, b


def expected_rows(data_type: str, interval: str, period: str) -> int | None:
    """bar 类按日历算期望行数（上市前/下线后按 M-04 生命周期表修正）；funding/metrics 周期可变，不预设。"""
    if data_type not in BAR_TYPES:
        return None
    a, b = period_bounds(period)
    return grid_points_between(a, b, INTERVAL_SECONDS[interval])


# ---------------------------------------------------------------------------
# 入湖
# ---------------------------------------------------------------------------
@dataclass
class Manifest:
    partition_id: str
    venue: str
    market: str
    data_type: str
    interval: str
    instrument_id: str
    symbol: str
    period: str
    source_uri: str
    source_sha256: str
    checksum_source: str          # vision_CHECKSUM | computed
    head_meta: dict
    downloaded_at: str
    parser_version: str
    rule_version: str
    schema_version: str
    zip_member: str
    expected_rows: int | None
    actual_rows: int
    distinct_keys: int
    missing: int | None
    duplicates: int
    key_min: str | None
    key_max: str | None
    schema_hash: str
    quarantine_n: int
    available_at_basis: str
    status: str                   # ok | gap | quarantined | missing_source
    days: list[dict] = field(default_factory=list)
    conflict_keys: list[str] = field(default_factory=list)          # 同键异值（全部候选已隔离）
    exact_duplicate_keys: list[str] = field(default_factory=list)   # 完全相同副本（折叠，映射账）
    retained_previous: str | None = None                            # 被替换的旧 bronze（按 sha 改名保留）
    unsupported_capabilities: list[str] = field(default_factory=lambda: ["bronze_depth_replay", "multi_source_versioned_manifest"])
    superseded_days: list[str] = field(default_factory=list)        # 新版不再包含而被失效的旧日分区


def _missing(df: pl.DataFrame, data_type: str, interval: str, period: str, key: str) -> int | None:
    if data_type not in BAR_TYPES:
        return None
    a, b = period_bounds(period)
    exp = expected_rows(data_type, interval, period)
    in_range = df.filter((pl.col(key) >= a) & (pl.col(key) < b))[key].n_unique()
    return exp - in_range


def ingest_bytes(zip_bytes: bytes, *, data_type: str, interval: str, symbol: str, period: str,
                 source_uri: str, source_sha256: str, checksum_source: str, head_meta: dict,
                 lake: LakePaths) -> Manifest:
    """已下载的包 → bronze/silver/manifest（幂等、原子）。"""
    pid = partition_id(data_type, interval, symbol, period)
    df, member = parse_zip(zip_bytes, data_type, interval, symbol)
    key = KEY_COL[data_type]
    if key not in df.columns or df[key].is_null().any():
        raise ValueError("unknown partition key: cannot verify conflicts; refusing ingest")
    now = dt.datetime.now(dt.UTC)
    df = df.with_columns(
        pl.lit(now).cast(pl.Datetime(US, "UTC")).alias("ingested_at"),
        pl.lit(source_sha256).alias("source_sha256"), pl.lit(RULE_VERSION).alias("rule_version"),
    ).sort(key)
    n, nd = df.height, df[key].n_unique()
    dup = n - nd
    miss = _missing(df, data_type, interval, period, key)
    # bronze：原包 + sha256 侧车；旧源包按 sha 改名保留（不可变）
    bpath = lake.bronze(data_type, interval, symbol, period)
    retained = retain_previous_bronze(bpath, source_sha256)
    atomic_write_bytes(bpath, zip_bytes)
    atomic_write_bytes(bpath.with_suffix(".zip.sha256"), f"{source_sha256}  {bpath.name}\n".encode())
    # silver：重复键分两类（S06）——完全相同副本折叠为一行（映射账 exact_duplicate_keys）；
    # 同键异值 = 冲突：全部候选剔出 silver 并进 quarantine（KEY_DUPLICATE_OR_ORDER），不做 first-wins
    value_cols = [c for c in df.columns if c not in ("ingested_at",)]
    exact = df.unique(subset=value_cols, keep="first", maintain_order=True)
    # 同 null 候选不是已知相等；缺失主键/经济值一律隔离，不静默 unique 成有效行。
    required_values = [key]
    if data_type in BAR_TYPES:
        required_values += ["open", "high", "low", "close", "volume"]
    elif data_type == "fundingRate":
        required_values += ["funding_rate", "funding_interval_hours"]
    unknown = pl.any_horizontal([pl.col(col).is_null() for col in required_values])
    conflict_keys = exact.filter(exact.select(key).is_duplicated() | unknown)[key].unique().sort().to_list()
    exact_dup_keys = df.filter(df.select(key).is_duplicated())[key].unique().sort().to_list()
    exact_dup_keys = [k for k in exact_dup_keys if k not in conflict_keys]
    sdf = exact.filter(~pl.col(key).is_in(conflict_keys, nulls_equal=True))
    conflict_rows = exact.filter(pl.col(key).is_in(conflict_keys, nulls_equal=True))
    sdir = lake.silver_dir(data_type, interval, symbol)
    days = []
    written = set()
    for (day,), part in sdf.with_columns(pl.col(key).dt.date().alias("_d")).group_by("_d", maintain_order=True):
        part = part.drop("_d")
        atomic_write_parquet(sdir / f"date={day.isoformat()}" / "part.parquet", part)
        days.append({"date": day.isoformat(), "rows": part.height})
        written.add(day)
    # S14：本源拥有的 period 内、新版不再包含的日分区必须失效（改名 superseded，不删除历史）
    a0, b0 = period_bounds(period)
    d = a0.date()
    superseded = []
    while d < b0.date():
        if d not in written:
            old = sdir / f"date={d.isoformat()}" / "part.parquet"
            if old.exists():
                keep = old.with_name(f"part.{source_sha256[:12]}.superseded.parquet")
                os.replace(old, keep)
                superseded.append(d.isoformat())
        d += dt.timedelta(days=1)
    quarantine_n = 0
    if conflict_rows.height:
        from quant_lab.market.quarantine import append_quarantine, conflict_records
        quarantine_n = append_quarantine(lake, conflict_records(pid, conflict_rows, key=key, source_sha256=source_sha256, rule_version=RULE_VERSION))
    status = "quarantined" if conflict_rows.height else ("ok" if (dup == 0 and (miss or 0) == 0) else "gap")
    m = Manifest(
        partition_id=pid, venue=VENUE, market=MARKET, data_type=data_type, interval=interval,
        instrument_id=instrument_id(symbol), symbol=symbol, period=period, source_uri=source_uri,
        source_sha256=source_sha256, checksum_source=checksum_source, head_meta=head_meta,
        downloaded_at=now.isoformat(), parser_version=PARSER_VERSION, rule_version=RULE_VERSION,
        schema_version=MARKET_SCHEMA_VERSION, zip_member=member, expected_rows=expected_rows(data_type, interval, period),
        actual_rows=n, distinct_keys=nd, missing=miss, duplicates=dup,
        key_min=str(df[key].min()) if n else None, key_max=str(df[key].max()) if n else None,
        schema_hash=schema_hash(sdf), quarantine_n=quarantine_n, available_at_basis="H0_close_plus_0s", status=status, days=days,
        conflict_keys=[str(k) for k in conflict_keys], exact_duplicate_keys=[str(k) for k in exact_dup_keys],
        retained_previous=str(retained) if retained else None, superseded_days=superseded,
    )
    atomic_write_json(lake.manifest(pid), asdict(m))
    return m


def load_manifest(lake: LakePaths, pid: str) -> dict | None:
    p = lake.manifest(pid)
    return json.loads(p.read_text()) if p.exists() else None


def fetch(symbol: str, data_type: str, interval: str | None, period: str, *, lake: LakePaths | None = None,
          client: httpx.Client | None = None, force: bool = False, hosts: Iterable[str] = HOSTS) -> Manifest | dict:
    """下载一个分区并入湖。已存在且 sha256 相同 → 直接返回旧 manifest（幂等）。"""
    lake = lake or LakePaths.default()
    interval = normalize_interval(data_type, interval)
    pid = partition_id(data_type, interval, symbol, period)
    rel = source_path(data_type, interval, symbol, period)
    own = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        expected = fetch_checksum(rel, client, hosts=hosts)
        body, url, meta = http_get(rel, client=client, hosts=hosts)
        if body is None:
            rec = {"partition_id": pid, "status": "missing_source", "source_uri": url, "reason_code": "SOURCE_404",
                   "downloaded_at": dt.datetime.now(dt.UTC).isoformat()}
            atomic_write_json(lake.quarantine(pid, "SOURCE_404"), rec)
            return rec
        actual = hashlib.sha256(body).hexdigest()
        if expected and expected != actual:
            rec = {"partition_id": pid, "reason_code": "RAW_HASH_MISMATCH", "expected": expected, "actual": actual,
                   "source_uri": url, "downloaded_at": dt.datetime.now(dt.UTC).isoformat()}
            atomic_write_json(lake.quarantine(pid, "RAW_HASH_MISMATCH"), rec)
            raise RawHashMismatch(json.dumps(rec))
        old = load_manifest(lake, pid)
        if old and not force and old.get("source_sha256") == actual and old.get("parser_version") == PARSER_VERSION:
            return old
        return ingest_bytes(body, data_type=data_type, interval=interval, symbol=symbol, period=period,
                            source_uri=url, source_sha256=actual,
                            checksum_source="vision_CHECKSUM" if expected else "computed", head_meta=meta, lake=lake)
    finally:
        if own:
            client.close()


def months(start: str, end: str) -> list[str]:
    a, b = dt.date.fromisoformat(start + "-01"), dt.date.fromisoformat(end + "-01")
    out = []
    while a <= b:
        out.append(a.strftime("%Y-%m"))
        a = dt.date(a.year + (a.month == 12), (a.month % 12) + 1, 1)
    return out


def coverage(lake: LakePaths | None = None) -> pl.DataFrame:
    """覆盖矩阵：按 manifest 汇总。"""
    lake = lake or LakePaths.default()
    recs = [json.loads(p.read_text()) for p in sorted((lake.root / "_manifest").glob("*.json"))]
    if not recs:
        return pl.DataFrame(schema={"instrument_id": pl.Utf8, "data_type": pl.Utf8, "interval": pl.Utf8, "period": pl.Utf8,
                                    "actual_rows": pl.Int64, "missing": pl.Int64, "duplicates": pl.Int64, "status": pl.Utf8})
    return pl.DataFrame([{k: r.get(k) for k in ("instrument_id", "data_type", "interval", "period", "actual_rows",
                                                "missing", "duplicates", "status")} for r in recs])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="quant_lab.market.vision")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("fetch", help="下载一个分区（月包；metrics 为日包）")
    s.add_argument("--symbol", required=True); s.add_argument("--type", required=True, choices=DATA_TYPES)
    s.add_argument("--interval"); s.add_argument("--month", required=True, help="yyyy-mm（metrics 用 yyyy-mm-dd）")
    s.add_argument("--lake"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("fetch-many", help="按品种文件批量下载：每行一个 symbol")
    s.add_argument("--symbols-file", required=True); s.add_argument("--types", default="klines,markPriceKlines,indexPriceKlines,premiumIndexKlines,fundingRate")
    s.add_argument("--intervals", default="1m,15m"); s.add_argument("--from", dest="start", default="2024-01"); s.add_argument("--to", dest="end")
    s.add_argument("--lake")
    s = sub.add_parser("coverage", help="覆盖矩阵")
    s.add_argument("--lake"); s.add_argument("--from-seeds", help="M-11（gated）：按 G1 种子清单扩品种，未实现")
    a = p.parse_args(argv)
    lake = LakePaths(Path(a.lake)) if a.lake else LakePaths.default()
    if a.cmd == "fetch":
        m = fetch(a.symbol, a.type, a.interval, a.month, lake=lake, force=a.force)
        d = m if isinstance(m, dict) else asdict(m)
        d.pop("days", None)
        print(json.dumps(d, ensure_ascii=False, default=str))
        return 0 if d.get("status") in ("ok", "gap") else 1
    if a.cmd == "fetch-many":
        syms = [l.strip() for l in Path(a.symbols_file).read_text().splitlines() if l.strip() and not l.startswith("#")]
        end = a.end or dt.date.today().strftime("%Y-%m")
        rc = 0
        with httpx.Client(timeout=60.0, follow_redirects=True) as c:
            for sym in syms:
                for t in a.types.split(","):
                    for iv in (a.intervals.split(",") if t in BAR_TYPES else [None]):
                        for mo in months(a.start, end):
                            try:
                                m = fetch(sym, t, iv, mo, lake=lake, client=c)
                                d = m if isinstance(m, dict) else asdict(m)
                                print(f"{d['partition_id']} status={d['status']} rows={d.get('actual_rows')} missing={d.get('missing')}")
                            except VisionError as e:
                                rc = 1
                                print(f"{sym} {t} {iv} {mo} ERROR {e}", file=sys.stderr)
        return rc
    if a.cmd == "coverage":
        if a.from_seeds:
            print("coverage --from-seeds 是 M-11（gated 于 D-11），未实现", file=sys.stderr)
            return 2
        df = coverage(lake)
        print(df)
        print(f"instruments={df['instrument_id'].n_unique() if df.height else 0} partitions={df.height}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
