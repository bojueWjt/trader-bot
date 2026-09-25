"""L0 离线合成湖；原始输入只用可识别的哨兵文本。"""
import inspect
import json
import textwrap
from datetime import UTC, datetime, timedelta

import polars as pl

from quant_lab.market.partition_check import RULES_SCHEMA, write_rules
from quant_lab.market.vision import LakePaths, atomic_write_json, atomic_write_parquet, instrument_id, partition_id

T0 = datetime(2024, 7, 1, 12, tzinfo=UTC)
CHANNEL = -1002194802852
OTHER = -1002194802853
SECRET = "PRIVATE_TEXT_NEVER_IN_LAKE"


def mutant(function, before, after):
    source = textwrap.dedent(inspect.getsource(function))
    assert source.count(before) == 1
    namespace = dict(function.__globals__)
    exec(compile(source.replace(before, after), "<l0-mutant>", "exec"), namespace)
    return namespace[function.__name__]


def market_lake(root, *, days=7):
    lake = LakePaths(root / "lake" / "market")
    times = [T0 - timedelta(minutes=5) + timedelta(minutes=i) for i in range(days * 1440)]
    for kind in ("klines", "markPriceKlines"):
        # 第一笔限价成交后向上触及目标，mark 不触发止损。
        rows = [{"instrument_id": instrument_id("BTCUSDT"), "interval": "1m", "open_time": t,
                 "close_time": t + timedelta(minutes=1), "available_at": t + timedelta(minutes=1),
                 "open": 100.0, "high": 110.0 if t >= T0 + timedelta(minutes=3) else 101.0,
                 "low": 99.0, "close": 100.0, "volume": 10000.0,
                 "gap_flag": False, "ohlc_valid": True, "source_sha256": kind} for t in times]
        df = pl.DataFrame(rows)
        for (day,), part in df.with_columns(pl.col("open_time").dt.date().alias("_d")).group_by("_d"):
            atomic_write_parquet(lake.silver_dir(kind, "1m", "BTCUSDT") / f"date={day}" / "part.parquet", part.drop("_d"))
        pid = partition_id(kind, "1m", "BTCUSDT", "2024-07")
        atomic_write_json(lake.manifest(pid), {"partition_id": pid, "instrument_id": instrument_id("BTCUSDT"),
                          "data_type": kind, "interval": "1m", "period": "2024-07", "status": "gap", "check_status": "gap",
                          "source_sha256": kind, "schema_hash": "synthetic", "actual_rows": len(rows),
                          "key_min": times[0].isoformat(), "key_max": times[-1].isoformat(), "available_at_basis": "H0_close_plus_0s"})
    funding_times = [T0.replace(hour=0) + timedelta(hours=i * 8) for i in range(days * 3 + 3)]
    df = pl.DataFrame({"calc_time": funding_times, "funding_rate": [0.0] * len(funding_times),
                       "funding_interval_hours": [8] * len(funding_times)})
    for (day,), part in df.with_columns(pl.col("calc_time").dt.date().alias("_d")).group_by("_d"):
        atomic_write_parquet(lake.silver_dir("fundingRate", "8h", "BTCUSDT") / f"date={day}" / "part.parquet", part.drop("_d"))
    pid = partition_id("fundingRate", "8h", "BTCUSDT", "2024-07")
    atomic_write_json(lake.manifest(pid), {"partition_id": pid, "check_status": "ok", "source_sha256": "funding"})
    write_rules(lake, pl.DataFrame([{"instrument_id": instrument_id("BTCUSDT"), "effective_from": T0 - timedelta(days=1),
                                    "effective_to": None, "tick_size": "0.1", "step_size": "0.01", "min_notional": "0",
                                    "multiplier": "1", "funding_interval_hours": 8, "status": "TRADING", "source": "synthetic"}], schema=RULES_SCHEMA))
    return lake


def account(root):
    base = root / "import" / "telegram"
    base.mkdir(parents=True)
    (base / "channels.txt").write_text(f"{CHANNEL}\n{OTHER}\n")

    def msg(mid, text, t=T0):
        return {"id": mid, "type": "message", "date": t.isoformat(), "date_unixtime": str(int(t.timestamp())), "text": text}

    chats = [{"id": 2194802852, "type": "private_supergroup", "name": "teacher",
              "messages": [msg(1, "BTC 做多 入场 100 止损 90 目标 110")]},
             {"id": 2194802853, "type": "public_channel", "name": "other",
              "messages": [msg(1, "BTC 做空 入场 100 止损 110 目标 90")]},
             {"id": 2194802854, "type": "public_channel", "name": "unlisted",
              "messages": [msg(1, "UNLISTED_TEXT")]},
             {"id": 2194802852, "type": "personal_chat", "name": "private",
              "messages": [msg(2, SECRET)]}]
    (base / "account").mkdir()
    (base / "account" / "result.json").write_text(json.dumps({"chats": {"list": chats}}))
    (base / "pull").mkdir()
    pulled = [{"peer_id": CHANNEL, "id": 3, "date": int((T0 + timedelta(minutes=10)).timestamp()),
               "message": "BTC 做多 入场 80 止损 70 目标 100", "cohort": "history-pull-test", "snapshot_at": T0.isoformat()},
              {"peer_id": 12345, "id": 4, "date": int(T0.timestamp()), "message": SECRET}]
    (base / "pull" / f"{CHANNEL}.jsonl").write_text("\n".join(json.dumps(r) for r in pulled))
    return base
