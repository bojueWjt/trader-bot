#!/usr/bin/env python3
"""Synthetic watcher data for the wac-100 local test backend.

Everything here is generated locally and deterministically from a seed:
no network, no real channel content, no credentials. The output is a
watcher-shaped SQLite file (``telegram_messages``, ``briefings``,
``active_orders``) plus a media directory of PNG files in four size tiers.

Size tiers (bytes) and why they exist:

* ``small``      < 1 MiB        -- under the app thumbnail cap (1 MiB).
* ``large``      ~2 MiB         -- over the thumbnail cap, under the detail cap.
* ``huge``       > 5 MiB, < 20 MiB -- 6 MiB (under the app detail cap of 8 MiB)
                                   and 9 MiB (over it). The gateway serves both.
* ``over_limit`` > 20 MiB       -- over the contract ``budgets.media.max_file_bytes``
                                   (20 MiB): watcher and gateway must answer
                                   503 ``media_too_large`` before any header.

Timestamps use the watcher's own storage format (UTC ``YYYY-MM-DD HH:MM:SS``).
Rows inside the 24h window sit in ``[now-23h, now-60s]`` so a verify run within
the next hour still sees the same window; rows outside sit in
``[now-72h, now-25h]``, which survives the watcher's 7-day purge.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import struct
import sys
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

KIB = 1024
MIB = 1024 * 1024
CONTRACT_MAX_FILE_BYTES = 20 * MIB  # contracts/watcher-gateway-routes.yaml budgets.media.max_file_bytes
APP_THUMB_MAX_BYTES = 1 * MIB  # alert-personal watcherApi.MEDIA_THUMB_MAX_BYTES
APP_DETAIL_MAX_BYTES = 8 * MIB  # alert-personal watcherApi.MEDIA_DETAIL_MAX_BYTES

TIER_BOUNDS = {
    "small": (1, APP_THUMB_MAX_BYTES - 1),
    "large": (int(1.8 * MIB), int(2.3 * MIB)),
    "huge": (5 * MIB + 1, CONTRACT_MAX_FILE_BYTES - 1),
    "over_limit": (CONTRACT_MAX_FILE_BYTES + 1, 32 * MIB),
}
HUGE_TARGETS = (6 * MIB, 9 * MIB)
OVER_LIMIT_TARGET = 21 * MIB
LARGE_TARGET = 2 * MIB

WINDOW_HOURS = 24
IN_WINDOW_OLDEST = timedelta(hours=23)
IN_WINDOW_NEWEST = timedelta(seconds=60)
OUT_WINDOW_NEWEST = timedelta(hours=25)
OUT_WINDOW_OLDEST = timedelta(hours=72)
PURGE_AGE = timedelta(days=7)

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# Watcher DDL, copied verbatim from bridge/services/telegram-watcher/lib/trading-api.js.
# tests/dev/test_wac_local_backend.py fails if these drift from the watcher source.
DDL = {
    "telegram_messages": """
      CREATE TABLE IF NOT EXISTS telegram_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id INTEGER,
        channel_id TEXT,
        chat_title TEXT DEFAULT '',
        sender TEXT DEFAULT '',
        text TEXT DEFAULT '',
        has_media INTEGER DEFAULT 0,
        media_type TEXT DEFAULT '',
        media_filename TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
      )""",
    "briefings": """
CREATE TABLE IF NOT EXISTS briefings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          TEXT,
    content             TEXT NOT NULL,
    category            TEXT DEFAULT 'analysis',
    created_at          TEXT DEFAULT (datetime('now'))
)""",
    "active_orders": """
CREATE TABLE IF NOT EXISTS active_orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          TEXT,
    account_id          TEXT,
    symbol              TEXT,
    side                TEXT,
    entry_price         REAL,
    stop_loss           REAL,
    take_profit         REAL,
    quantity            REAL,
    binance_order_id    TEXT,
    binance_sl_order_id TEXT,
    binance_tp_order_id TEXT,
    status              TEXT DEFAULT 'PENDING',
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
)""",
}

CHANNELS = (
    {
        "channel_id": "-1009990000001",
        "title": "WAC 测试·合约信号 🚀",
        "senders": ("信号官 小王 🐂", "Analyst Ada", "风控助手"),
        "msg_id_base": 10_000,
    },
    {
        "channel_id": "-1009990000002",
        "title": "WAC Test Alpha Calls 🧪",
        "senders": ("Trader Bob", "かずき", "Ops Bot 🤖"),
        "msg_id_base": 500_000,
    },
    {
        "channel_id": "-1009990000003",
        "title": "WAC 测试·长文复盘 📊",
        "senders": ("复盘老张", "Макс", "🦊 Fox"),
        "msg_id_base": 900_000,
    },
)

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "XAUUSDT")
EMOJI = ("🚀", "🟢", "🔴", "📈", "📉", "🔥", "⚠️", "✅", "❌", "💰", "🐳", "👨‍👩‍👧", "🇯🇵", "🧧", "🫡")
SHORT_LINES = (
    "好的，收到 👍",
    "Market update: funding flipped negative on SOL 🔥",
    "注意回踩，别追高 ⚠️",
    "止盈一半，剩下拿住 ✅",
    "今晚 CPI，仓位减半 🧯",
    "LFG 🚀🚀🚀",
    "テスト用のメッセージです。",
    "Это тестовое сообщение.",
    "这条是纯测试数据，不代表任何交易建议。",
    "https://example.com/wac-test/chart?id=42",
)
LONG_PARAGRAPHS = (
    "【复盘】今日大饼在亚盘时段横盘整理，成交量持续萎缩，美盘开盘后放量突破前高，"
    "随后在整数关口附近出现明显抛压。多空双方在此区域反复争夺，短线结构仍偏强，"
    "但需要注意资金费率已经连续三个周期为正，追多性价比不高。🐂🐻",
    "Risk note: this is synthetic test data for tablet QA only. It deliberately mixes "
    "English, 中文, 日本語 and emoji (👨‍👩‍👧 🇯🇵 🫡) to exercise line breaking, glyph "
    "fallback and text measurement in the message list.",
    "策略要点：一、只在关键支撑位附近分批埋伏；二、止损放在结构低点下方，不扛单；"
    "三、止盈分三档，第一档到位后把止损移到成本；四、任何情况下单笔风险不超过账户的百分之一。📌",
    "超长无空格行测试：" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" * 6,
    "山寨季观察：ETH/BTC 汇率连续两周走弱，板块轮动速度加快，热点持续时间缩短到一到两天，"
    "追热点的胜率明显下降。建议以观察为主，等待汇率企稳再考虑配置。🧐📉📈",
)
BRIEFING_CATEGORIES = ("analysis", "summary", "alert")
ORDER_STATUSES = ("PENDING", "OPEN", "PARTIAL_CLOSED", "CLOSED", "CANCELLED")


@dataclass
class SeedSpec:
    seed: int = 20260929
    messages_in_window: int = 1800
    messages_out_window: int = 300
    briefings_in_window: int = 240
    briefings_out_window: int = 60
    orders: int = 24
    tie_ratio: float = 0.12
    long_ratio: float = 0.10
    small_media: int = 120
    large_media: int = 12
    huge_media: int = 6
    over_limit_media: int = 2
    missing_media: int = 4
    album_groups: int = 20


@dataclass
class SeedResult:
    now: str
    db_path: str
    media_dir: str
    messages_total: int = 0
    messages_in_window: int = 0
    messages_out_window: int = 0
    messages_in_window_by_channel: dict = field(default_factory=dict)
    briefings_total: int = 0
    briefings_in_window: int = 0
    orders_total: int = 0
    tied_message_rows: int = 0
    media: list = field(default_factory=list)
    missing_media: list = field(default_factory=list)
    channels: list = field(default_factory=list)


def fmt_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(TS_FORMAT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FORMAT).replace(tzinfo=timezone.utc)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_noise(width: int, height: int, rng: random.Random) -> bytes:
    """RGB noise stored uncompressed, so file size tracks width*height*3."""
    row_bytes = width * 3
    raw = bytearray()
    noise = rng.randbytes(row_bytes * height)
    for row in range(height):
        raw.append(0)
        raw += noise[row * row_bytes:(row + 1) * row_bytes]
    return _png(width, height, zlib.compress(bytes(raw), 0))


def png_solid(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    row = b"\x00" + bytes(rgb) * width
    return _png(width, height, zlib.compress(row * height, 9))


def _png(width: int, height: int, idat: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


def png_noise_near(target_bytes: int, rng: random.Random, width: int = 1600) -> bytes:
    width = max(8, min(width, target_bytes // 64))
    height = max(1, (target_bytes - 64) // (width * 3 + 1))
    return png_noise(width, height, rng)


def png_dimensions(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


def _message_text(rng: random.Random, kind: str) -> str:
    if kind == "long":
        count = rng.randint(10, 20)
        return "\n\n".join(rng.choice(LONG_PARAGRAPHS) for _ in range(count))
    if kind == "signal":
        symbol = rng.choice(SYMBOLS)
        side = rng.choice(("多 🟢", "空 🔴", "LONG 🟢", "SHORT 🔴"))
        base = rng.uniform(0.1, 70000)
        return (
            f"#{symbol} {side}\n入场: {base:.2f} - {base * 1.004:.2f}\n止损: {base * 0.97:.2f}\n"
            f"止盈: {base * 1.02:.2f} / {base * 1.05:.2f} / {base * 1.09:.2f}\n杠杆 {rng.choice((3, 5, 10, 20))}x "
            f"{rng.choice(EMOJI)}\n(WAC 测试数据，非交易建议)"
        )
    if kind == "media_only":
        return ""
    return rng.choice(SHORT_LINES) + " " + "".join(rng.choice(EMOJI) for _ in range(rng.randint(0, 3)))


def _spread(rng: random.Random, count: int, oldest: datetime, newest: datetime, tie_ratio: float) -> list[datetime]:
    span = int((newest - oldest).total_seconds())
    stamps = []
    for _ in range(count):
        if stamps and rng.random() < tie_ratio:
            stamps.append(rng.choice(stamps[-8:]))
        else:
            stamps.append(oldest + timedelta(seconds=rng.randint(0, span)))
    return stamps


def _plan_messages(spec: SeedSpec, rng: random.Random, now: datetime) -> list[dict]:
    rows = []
    in_stamps = _spread(rng, spec.messages_in_window, now - IN_WINDOW_OLDEST, now - IN_WINDOW_NEWEST, spec.tie_ratio)
    out_stamps = _spread(rng, spec.messages_out_window, now - OUT_WINDOW_OLDEST, now - OUT_WINDOW_NEWEST, spec.tie_ratio)
    for stamp in in_stamps + out_stamps:
        channel = rng.choice(CHANNELS)
        roll = rng.random()
        kind = "long" if roll < spec.long_ratio else "signal" if roll < 0.45 else "short"
        rows.append({"created": stamp, "channel": channel, "kind": kind, "media_tier": None, "album": None})
    # Albums: several photo messages in the same channel at the same second.
    album_rows = []
    for group in range(spec.album_groups):
        anchor = now - timedelta(seconds=rng.randint(int(IN_WINDOW_NEWEST.total_seconds()), int(IN_WINDOW_OLDEST.total_seconds())))
        channel = rng.choice(CHANNELS)
        for index in range(rng.randint(3, 4)):
            album_rows.append({"created": anchor, "channel": channel, "kind": "media_only" if index else "signal", "media_tier": "small", "album": group})
    # Albums replace plain in-window rows so the headline counts stay as specified.
    in_rows = [row for row in rows if row["created"] > now - timedelta(hours=WINDOW_HOURS)]
    for victim in rng.sample(in_rows, len(album_rows)):
        rows.remove(victim)
    rows.extend(album_rows)

    tiers = (["small"] * max(0, spec.small_media - len(album_rows)) + ["large"] * spec.large_media
             + ["huge"] * spec.huge_media + ["over_limit"] * spec.over_limit_media + ["missing"] * spec.missing_media)
    plain = [row for row in rows if row["media_tier"] is None and row["created"] > now - timedelta(hours=WINDOW_HOURS)]
    rng.shuffle(plain)
    # The newest rows carry at least one of every tier so the first page shows them.
    newest = sorted(plain, key=lambda row: row["created"], reverse=True)
    distinct_first = []
    for tier in ("large", "huge", "over_limit", "missing"):
        if tier in tiers:
            tiers.remove(tier)
            distinct_first.append(tier)
    for row, tier in zip(newest[:len(distinct_first)], distinct_first):
        row["media_tier"] = tier
    remaining = [row for row in plain if row["media_tier"] is None]
    for row, tier in zip(remaining, tiers):
        row["media_tier"] = tier
    rows.sort(key=lambda row: (row["created"], row["channel"]["channel_id"]))
    return rows


def _write_media(path: Path, tier: str, ordinal: int, rng: random.Random) -> bytes:
    if tier == "small":
        if ordinal % 3 == 0:
            data = png_solid(rng.choice((640, 1280, 1920)), rng.choice((480, 720, 1080)), (rng.randrange(256), rng.randrange(256), rng.randrange(256)))
        else:
            data = png_noise_near(rng.randint(8 * KIB, 256 * KIB), rng, width=rng.choice((128, 256, 320)))
    elif tier == "large":
        data = png_noise_near(LARGE_TARGET + rng.randint(-64 * KIB, 64 * KIB), rng)
    elif tier == "huge":
        data = png_noise_near(HUGE_TARGETS[ordinal % len(HUGE_TARGETS)], rng, width=2048)
    elif tier == "over_limit":
        data = png_noise_near(OVER_LIMIT_TARGET, rng, width=2048)
    else:
        raise ValueError(tier)
    low, high = TIER_BOUNDS[tier]
    if not low <= len(data) <= high:
        raise AssertionError(f"{tier} media size {len(data)} outside {low}..{high}")
    path.write_bytes(data)
    return data


def generate(db_path: Path, media_dir: Path, *, now: datetime | None = None, spec: SeedSpec | None = None) -> SeedResult:
    spec = spec or SeedSpec()
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    rng = random.Random(spec.seed)
    db_path = Path(db_path)
    media_dir = Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        raise FileExistsError(f"refusing to seed into an existing database: {db_path}")
    result = SeedResult(now=fmt_ts(now), db_path=str(db_path), media_dir=str(media_dir))
    result.channels = [{"channel_id": c["channel_id"], "title": c["title"]} for c in CHANNELS]

    rows = _plan_messages(spec, rng, now)
    window_start = now - timedelta(hours=WINDOW_HOURS)
    next_msg_id = {c["channel_id"]: c["msg_id_base"] for c in CHANNELS}
    tier_ordinal = {}
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            for ddl in DDL.values():
                conn.execute(ddl)
            stamp_counts = {}
            for row in rows:
                stamp_counts[row["created"]] = stamp_counts.get(row["created"], 0) + 1
            for row in rows:
                channel = row["channel"]
                msg_id = next_msg_id[channel["channel_id"]]
                next_msg_id[channel["channel_id"]] = msg_id + rng.randint(1, 3)
                created = fmt_ts(row["created"])
                filename = ""
                tier = row["media_tier"]
                if tier:
                    epoch_ms = int(row["created"].timestamp() * 1000) + rng.randint(0, 999)
                    filename = f"{epoch_ms}-{msg_id}.png"
                    if tier == "missing":
                        result.missing_media.append(filename)
                    else:
                        ordinal = tier_ordinal.get(tier, 0)
                        tier_ordinal[tier] = ordinal + 1
                        data = _write_media(media_dir / filename, tier, ordinal, rng)
                        width, height = png_dimensions(data)
                        result.media.append({"filename": filename, "tier": tier, "size": len(data), "width": width, "height": height})
                conn.execute(
                    "INSERT INTO telegram_messages (msg_id, channel_id, chat_title, sender, text, has_media, media_type, media_filename, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (msg_id, channel["channel_id"], channel["title"], rng.choice(channel["senders"]),
                     _message_text(rng, row["kind"]), 1 if filename else 0, "photo" if filename else "", filename, created),
                )
                result.messages_total += 1
                if stamp_counts[row["created"]] > 1:
                    result.tied_message_rows += 1
                if row["created"] > window_start:
                    result.messages_in_window += 1
                    by_channel = result.messages_in_window_by_channel
                    by_channel[channel["channel_id"]] = by_channel.get(channel["channel_id"], 0) + 1
                else:
                    result.messages_out_window += 1

            briefing_stamps = sorted(
                _spread(rng, spec.briefings_in_window, now - IN_WINDOW_OLDEST, now - IN_WINDOW_NEWEST, spec.tie_ratio)
                + _spread(rng, spec.briefings_out_window, now - OUT_WINDOW_OLDEST, now - OUT_WINDOW_NEWEST, spec.tie_ratio)
            )
            for stamp in briefing_stamps:
                channel = rng.choice(CHANNELS)
                content = f"【{rng.choice(('早报', '午间简报', '晚间复盘', 'Alert'))}】{rng.choice(SYMBOLS)} " + _message_text(
                    rng, "long" if rng.random() < 0.3 else "signal")
                conn.execute(
                    "INSERT INTO briefings (channel_id, content, category, created_at) VALUES (?, ?, ?, ?)",
                    (channel["channel_id"], content, rng.choice(BRIEFING_CATEGORIES), fmt_ts(stamp)),
                )
                result.briefings_total += 1
                if stamp > window_start:
                    result.briefings_in_window += 1

            for index in range(spec.orders):
                channel = rng.choice(CHANNELS)
                entry = rng.uniform(0.5, 70000)
                side = rng.choice(("LONG", "SHORT"))
                sign = 1 if side == "LONG" else -1
                stamp = fmt_ts(now - timedelta(minutes=rng.randint(1, 60 * 23)))
                conn.execute(
                    "INSERT INTO active_orders (channel_id, account_id, symbol, side, entry_price, stop_loss, take_profit, quantity,"
                    " binance_order_id, binance_sl_order_id, binance_tp_order_id, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', '', ?, ?, ?)",
                    (channel["channel_id"], f"wac-test-{'abcd'[index % 4]}", rng.choice(SYMBOLS), side, round(entry, 4),
                     round(entry * (1 - sign * 0.03), 4), round(entry * (1 + sign * 0.06), 4), round(rng.uniform(0.001, 50), 4),
                     ORDER_STATUSES[index % len(ORDER_STATUSES)], stamp, stamp),
                )
                result.orders_total += 1
    finally:
        conn.close()
    return result


def write_manifest(result: SeedResult, path: Path) -> None:
    Path(path).write_text(json.dumps(asdict(result), ensure_ascii=False, indent=1), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, type=Path, help="new SQLite file to create (must not exist)")
    parser.add_argument("--media-dir", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--seed", type=int, default=SeedSpec.seed)
    args = parser.parse_args(argv)
    result = generate(args.db, args.media_dir, spec=SeedSpec(seed=args.seed))
    if args.manifest:
        write_manifest(result, args.manifest)
    print(f"seeded {result.messages_total} messages ({result.messages_in_window} in 24h window), "
          f"{result.briefings_total} briefings, {result.orders_total} orders, {len(result.media)} media files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
