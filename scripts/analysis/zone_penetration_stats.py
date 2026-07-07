from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

Kline = tuple[Any, float, float, float, float]

DEFAULT_BASE_URL = "https://data.binance.vision"
DEFAULT_DEPTHS = (0.0, 0.25, 0.50, 0.85, 1.0)
DEFAULT_WINDOW_HOURS = 24
DEFAULT_ATR_LOOKBACK_HOURS = 24
CHASE_PREMIUM = 0.0035
UNSUPPORTED_BINANCE_SYMBOLS = {"XAUUSDT"}


class ZoneStatsError(Exception):
    """Base class for recoverable stats script failures."""


class UnsupportedSymbol(ZoneStatsError):
    """Raised when a signal symbol has no Binance UM kline source."""


class MissingKlines(ZoneStatsError):
    """Raised when Binance Vision has no daily kline archive for a symbol/date."""


def _side(side: str) -> str:
    normalized = str(side).lower()
    if normalized not in {"long", "short"}:
        raise ValueError(f"unsupported side: {side!r}")
    return normalized


def _validate_zone(price_min: float, price_max: float) -> tuple[float, float]:
    low = float(price_min)
    high = float(price_max)
    if low >= high:
        raise ValueError(f"invalid zone: price_min={price_min!r}, price_max={price_max!r}")
    return low, high


def zone_edges(side: str, price_min: float, price_max: float) -> tuple[float, float]:
    """Return (near_edge, far_edge) for a side-specific zone."""
    low, high = _validate_zone(price_min, price_max)
    return (high, low) if _side(side) == "long" else (low, high)


def price_at_depth(side: str, price_min: float, price_max: float, depth_fraction: float) -> float:
    near_edge, far_edge = zone_edges(side, price_min, price_max)
    return near_edge + (far_edge - near_edge) * float(depth_fraction)


def penetration_depth(side: str, price_min: float, price_max: float, extreme_price: float) -> float:
    """Return zone penetration depth, where 0 is near edge and 1 is far edge."""
    low, high = _validate_zone(price_min, price_max)
    width = high - low
    if _side(side) == "long":
        return (high - float(extreme_price)) / width
    return (float(extreme_price) - low) / width


def first_touch_near(side: str, klines: Sequence[Kline], near_edge: float) -> int | None:
    side = _side(side)
    edge = float(near_edge)
    for index, (_, _, high, low, _) in enumerate(klines):
        if side == "long" and float(low) <= edge:
            return index
        if side == "short" and float(high) >= edge:
            return index
    return None


def _target_direction(target: float, klines: Sequence[Kline]) -> str:
    if not klines:
        return "up"
    first_open = float(klines[0][1])
    return "up" if float(target) >= first_open else "down"


def _first_reach_index(
    target: float,
    klines: Sequence[Kline],
    *,
    direction: str | None = None,
) -> int | None:
    direction = direction or _target_direction(target, klines)
    target = float(target)
    for index, (_, _, high, low, _) in enumerate(klines):
        if direction == "up" and float(high) >= target:
            return index
        if direction == "down" and float(low) <= target:
            return index
    return None


def reached_before(target: float, klines: Sequence[Kline], before_index: int | None = None) -> bool:
    """Return whether target is reached before before_index.

    Direction is inferred from the first kline open. A target above the first open
    is reached by high >= target; a target below it is reached by low <= target.
    """
    if before_index is None:
        candidate_klines = klines
    else:
        candidate_klines = klines[: max(0, before_index)]
    return _first_reach_index(target, candidate_klines) is not None


def classify_retrace(
    side: str,
    price_min: float,
    price_max: float,
    tp1: float | None,
    klines_after: Sequence[Kline],
) -> dict[str, Any]:
    side = _side(side)
    near_edge, _ = zone_edges(side, price_min, price_max)
    near_index = first_touch_near(side, klines_after, near_edge)
    tp_index = None
    if tp1 is not None:
        direction = "up" if side == "long" else "down"
        tp_index = _first_reach_index(float(tp1), klines_after, direction=direction)

    if tp_index is not None and (near_index is None or tp_index < near_index):
        return {
            "classification": "no_retrace",
            "near_index": near_index,
            "tp_index": tp_index,
            "penetration_depth": None,
            "breached_far_edge": False,
        }

    if near_index is None:
        return {
            "classification": "unresolved",
            "near_index": None,
            "tp_index": tp_index,
            "penetration_depth": None,
            "breached_far_edge": False,
        }

    touched = klines_after[near_index:]
    if side == "long":
        extreme = min(float(row[3]) for row in touched)
    else:
        extreme = max(float(row[2]) for row in touched)
    depth = penetration_depth(side, price_min, price_max, extreme)
    return {
        "classification": "retrace",
        "near_index": near_index,
        "tp_index": tp_index,
        "penetration_depth": depth,
        "breached_far_edge": depth >= 1.0,
    }


def fill_at_depth(
    side: str,
    price_min: float,
    price_max: float,
    depth_fraction: float,
    klines_after: Sequence[Kline],
) -> bool:
    side = _side(side)
    price = price_at_depth(side, price_min, price_max, depth_fraction)
    for _, _, high, low, _ in klines_after:
        if side == "long" and float(low) <= price:
            return True
        if side == "short" and float(high) >= price:
            return True
    return False


def chase_window_hit(side: str, near_edge: float, current_price: float, *, premium: float = CHASE_PREMIUM) -> bool:
    side = _side(side)
    near = float(near_edge)
    current = float(current_price)
    if side == "long":
        distance = (current - near) / near
    else:
        distance = (near - current) / near
    return 0.0 <= distance <= float(premium) + 1e-12


def zone_height_pct(price_min: float, price_max: float) -> float:
    low, high = _validate_zone(price_min, price_max)
    midpoint = (low + high) / 2.0
    return (high - low) / midpoint


def atr_1h(klines_1m: Sequence[Kline], *, period: int = 14) -> float | None:
    hourly = _aggregate_1m_to_1h(klines_1m)
    if not hourly:
        return None

    true_ranges: list[float] = []
    previous_close: float | None = None
    for _, _, high, low, close in hourly:
        high = float(high)
        low = float(low)
        close = float(close)
        if previous_close is None:
            true_range = high - low
        else:
            true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(true_range)
        previous_close = close

    sample = true_ranges[-period:] if period > 0 else true_ranges
    return statistics.fmean(sample) if sample else None


def _aggregate_1m_to_1h(klines_1m: Sequence[Kline]) -> list[Kline]:
    rows = sorted(klines_1m, key=lambda row: _open_time_sort_key(row[0]))
    hourly: list[Kline] = []
    current_key: Any = None
    current: list[Any] | None = None

    for open_time, open_, high, low, close in rows:
        key = _hour_bucket(open_time)
        if current is None or key != current_key:
            if current is not None:
                hourly.append(tuple(current))  # type: ignore[arg-type]
            current_key = key
            current = [open_time, float(open_), float(high), float(low), float(close)]
            continue
        current[2] = max(float(current[2]), float(high))
        current[3] = min(float(current[3]), float(low))
        current[4] = float(close)

    if current is not None:
        hourly.append(tuple(current))  # type: ignore[arg-type]
    return hourly


def _hour_bucket(open_time: Any) -> Any:
    if isinstance(open_time, datetime):
        return open_time.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    value = float(open_time)
    if value > 10_000_000_000:
        return int(value // 3_600_000)
    if value > 1_000_000_000:
        return int(value // 3_600)
    return int(value // 3_600_000)


def _open_time_sort_key(open_time: Any) -> float:
    if isinstance(open_time, datetime):
        return open_time.timestamp()
    return float(open_time)


def normalize_symbol(symbol: str) -> str:
    normalized = _display_symbol(symbol)
    if normalized in UNSUPPORTED_BINANCE_SYMBOLS:
        raise UnsupportedSymbol(f"{normalized} has no Binance UM kline archive")
    if not normalized:
        raise UnsupportedSymbol("missing symbol")
    return normalized


def _display_symbol(symbol: Any) -> str:
    normalized = str(symbol or "").strip().upper().replace("/", "")
    for suffix in ("-PERP.BINANCE", ".PERP.BINANCE", "-PERP", ".PERP", ".BINANCE"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def load_zone_signals(conn: Any | None = None, export_json_path: str | Path | None = None) -> list[dict[str, Any]]:
    if export_json_path is not None:
        payload = json.loads(Path(export_json_path).read_text(encoding="utf-8"))
        rows = payload.get("signals", payload) if isinstance(payload, dict) else payload
        return [_canonical_signal(row) for row in rows]

    if conn is None:
        raise ValueError("conn or export_json_path is required")

    sql = """
        SELECT
            hd.decision_id::text AS decision_id,
            hd.raw_message_id::text AS raw_message_id,
            hd.instrument_symbol AS symbol,
            hd.side::text AS side,
            hd.entry_price_min::float8 AS entry_price_min,
            hd.entry_price_max::float8 AS entry_price_max,
            hd.take_profits AS take_profits,
            hd.created_at AS created_at,
            rm.source_received_at AS source_received_at
        FROM hermes_decisions hd
        JOIN raw_messages rm ON rm.id = hd.raw_message_id
        WHERE hd.entry_type = 'zone'
          AND hd.side IN ('long', 'short')
          AND hd.entry_price_min IS NOT NULL
          AND hd.entry_price_max IS NOT NULL
        ORDER BY COALESCE(rm.source_received_at, hd.created_at), hd.created_at, hd.decision_id
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    return [_canonical_signal(row if isinstance(row, Mapping) else dict(zip(columns, row))) for row in rows]


def load_klines(
    symbol: str,
    start: datetime,
    end: datetime,
    cache_dir: str | Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
    market: str = "um",
) -> list[Kline]:
    normalized_symbol = normalize_symbol(symbol)
    start_dt = _coerce_datetime(start)
    end_dt = _coerce_datetime(end)
    if end_dt <= start_dt:
        return []

    cache_root = Path(cache_dir)
    rows: list[Kline] = []
    for day in _date_range(start_dt, end_dt):
        zip_path = _kline_cache_path(cache_root, market, normalized_symbol, day)
        if not zip_path.exists():
            _download_kline_zip(zip_path, base_url, market, normalized_symbol, day)
        rows.extend(_read_kline_zip(zip_path))

    return [
        row
        for row in sorted(rows, key=lambda item: _open_time_sort_key(item[0]))
        if start_dt <= _coerce_datetime(row[0]) < end_dt
    ]


def aggregate(
    signals: Iterable[Mapping[str, Any]],
    kline_provider: Callable[[str, datetime, datetime], Sequence[Kline]],
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    depths: Sequence[float] = DEFAULT_DEPTHS,
    chase_premium: float = CHASE_PREMIUM,
    atr_lookback_hours: int = DEFAULT_ATR_LOOKBACK_HOURS,
) -> dict[str, Any]:
    fill_counts = {depth: 0 for depth in depths}
    penetration_depths: list[float] = []
    height_pcts: list[float] = []
    height_atr_multiples: list[float] = []
    per_signal: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    no_retrace_count = 0
    retrace_count = 0
    breached_count = 0
    chase_hits = 0
    sample_count = 0

    for raw_signal in signals:
        signal = _canonical_signal(raw_signal)
        try:
            symbol = normalize_symbol(signal["symbol"])
        except UnsupportedSymbol as exc:
            skipped.append({"symbol": _display_symbol(signal.get("symbol")), "reason": str(exc)})
            continue

        side = _side(signal["side"])
        price_min, price_max = _validate_zone(signal["entry_price_min"], signal["entry_price_max"])
        signal_time = _signal_time(signal)
        request_start = signal_time - timedelta(hours=atr_lookback_hours)
        request_end = signal_time + timedelta(hours=window_hours)

        try:
            all_klines = list(kline_provider(symbol, request_start, request_end))
        except (MissingKlines, UnsupportedSymbol) as exc:
            skipped.append({"symbol": symbol, "reason": str(exc)})
            continue

        klines_after = [
            row for row in all_klines if signal_time <= _coerce_datetime(row[0]) < request_end
        ]
        if not klines_after:
            skipped.append({"symbol": symbol, "reason": "no klines in post-signal window"})
            continue

        sample_count += 1
        tp1 = _first_take_profit(signal)
        classification = classify_retrace(side, price_min, price_max, tp1, klines_after)
        if classification["classification"] == "no_retrace":
            no_retrace_count += 1
        elif classification["classification"] == "retrace":
            retrace_count += 1
            depth = float(classification["penetration_depth"])
            penetration_depths.append(depth)
            if classification["breached_far_edge"]:
                breached_count += 1

        near_edge, _ = zone_edges(side, price_min, price_max)
        current_price = float(klines_after[0][1])
        if chase_window_hit(side, near_edge, current_price, premium=chase_premium):
            chase_hits += 1

        filled_depths: dict[str, bool] = {}
        for depth in depths:
            filled = fill_at_depth(side, price_min, price_max, depth, klines_after)
            if filled:
                fill_counts[depth] += 1
            filled_depths[_depth_label(depth)] = filled

        height_pct = zone_height_pct(price_min, price_max)
        height_pcts.append(height_pct)
        zone_height = price_max - price_min
        atr_source = [row for row in all_klines if _coerce_datetime(row[0]) < signal_time]
        atr_value = atr_1h(atr_source or all_klines)
        height_atr_multiple = None
        if atr_value and atr_value > 0:
            height_atr_multiple = zone_height / atr_value
            height_atr_multiples.append(height_atr_multiple)

        per_signal.append(
            {
                "decision_id": signal.get("decision_id"),
                "symbol": symbol,
                "side": side,
                "signal_time": signal_time.isoformat(),
                "classification": classification["classification"],
                "penetration_depth": classification["penetration_depth"],
                "breached_far_edge": classification["breached_far_edge"],
                "chase_window_hit": chase_window_hit(
                    side, near_edge, current_price, premium=chase_premium
                ),
                "filled_depths": filled_depths,
                "zone_height_pct": height_pct,
                "zone_height_atr_multiple": height_atr_multiple,
            }
        )

    fill_rates = {
        _depth_label(depth): {
            "filled": fill_counts[depth],
            "total": sample_count,
            "rate": _ratio(fill_counts[depth], sample_count),
        }
        for depth in depths
    }
    return {
        "sample_count": sample_count,
        "skipped": skipped,
        "no_retrace": {
            "count": no_retrace_count,
            "ratio": _ratio(no_retrace_count, sample_count),
        },
        "retrace": {
            "count": retrace_count,
            "ratio": _ratio(retrace_count, sample_count),
            "median_penetration_depth": _median_or_none(penetration_depths),
            "breached_far_edge_count": breached_count,
            "breached_far_edge_ratio": _ratio(breached_count, retrace_count),
        },
        "fill_rates": fill_rates,
        "chase_window": {
            "premium": chase_premium,
            "hits": chase_hits,
            "ratio": _ratio(chase_hits, sample_count),
        },
        "zone_height": {
            "median_price_pct": _median_or_none(height_pcts),
            "median_atr_multiple": _median_or_none(height_atr_multiples),
        },
        "per_signal": per_signal,
    }


def print_report(result: Mapping[str, Any]) -> str:
    lines = [
        "Zone penetration stats",
        f"sample_count: {result['sample_count']}",
        (
            "no_retrace: "
            f"{result['no_retrace']['count']} "
            f"({_format_pct(result['no_retrace']['ratio'])})"
        ),
        (
            "retrace: "
            f"{result['retrace']['count']} "
            f"({_format_pct(result['retrace']['ratio'])}); "
            f"median_depth={_format_number(result['retrace']['median_penetration_depth'])}; "
            f"breached={result['retrace']['breached_far_edge_count']} "
            f"({_format_pct(result['retrace']['breached_far_edge_ratio'])})"
        ),
        "fill_rates: "
        + ", ".join(
            f"{depth}={stats['filled']}/{stats['total']} ({_format_pct(stats['rate'])})"
            for depth, stats in result["fill_rates"].items()
        ),
        (
            "chase_window: "
            f"{result['chase_window']['hits']}/{result['sample_count']} "
            f"({_format_pct(result['chase_window']['ratio'])}) "
            f"at premium={result['chase_window']['premium']:.4%}"
        ),
        (
            "zone_height: "
            f"median_price_pct={_format_pct(result['zone_height']['median_price_pct'])}; "
            f"median_atr_multiple={_format_number(result['zone_height']['median_atr_multiple'])}"
        ),
    ]
    if result["skipped"]:
        lines.append(f"skipped: {len(result['skipped'])}")
        for item in result["skipped"]:
            lines.append(f"  - {item.get('symbol')}: {item.get('reason')}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay zone penetration and fill-depth stats.")
    parser.add_argument("--db-url", help="Postgres URL for the hk v3 database")
    parser.add_argument("--signals-json", help="Offline signal export JSON to replay")
    parser.add_argument("--cache-dir", help="Directory for Binance Vision daily 1m ZIP cache")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Binance Vision base URL")
    parser.add_argument("--market", default="um", choices=("um", "cm", "spot"), help="Binance market archive")
    parser.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--export", help="Write DB zone signals to this JSON path and exit")
    parser.add_argument("--output", default="zone_penetration_stats_result.json", help="JSON result output path")
    args = parser.parse_args(argv)

    if args.export:
        if not args.db_url:
            parser.error("--db-url is required with --export")
        signals = _load_signals_from_db_url(args.db_url)
        _write_json(args.export, {"signals": signals})
        print(f"exported {len(signals)} zone signals to {args.export}")
        return 0

    if args.signals_json:
        signals = load_zone_signals(export_json_path=args.signals_json)
    elif args.db_url:
        signals = _load_signals_from_db_url(args.db_url)
    else:
        parser.error("one of --db-url or --signals-json is required")

    if not args.cache_dir:
        parser.error("--cache-dir is required unless --export is used")

    def provider(symbol: str, start: datetime, end: datetime) -> Sequence[Kline]:
        return load_klines(
            symbol,
            start,
            end,
            args.cache_dir,
            base_url=args.base_url,
            market=args.market,
        )

    result = aggregate(signals, provider, window_hours=args.window_hours)
    _write_json(args.output, result)
    print(print_report(result))
    print(f"wrote JSON result to {args.output}")
    return 0


def _canonical_signal(row: Mapping[str, Any]) -> dict[str, Any]:
    symbol = row.get("symbol", row.get("instrument_symbol"))
    return {
        "decision_id": row.get("decision_id"),
        "raw_message_id": row.get("raw_message_id"),
        "symbol": symbol,
        "side": row.get("side"),
        "entry_price_min": float(row.get("entry_price_min", row.get("price_min"))),
        "entry_price_max": float(row.get("entry_price_max", row.get("price_max"))),
        "created_at": _json_datetime(row.get("created_at")),
        "source_received_at": _json_datetime(row.get("source_received_at") or row.get("created_at")),
        "take_profits": _parse_take_profits(row.get("take_profits", ())),
    }


def _parse_take_profits(value: Any) -> list[float]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Iterable):
        return []
    parsed: list[float] = []
    for item in value:
        if isinstance(item, Mapping):
            item = item.get("price")
        if item is not None:
            parsed.append(float(item))
    return parsed


def _first_take_profit(signal: Mapping[str, Any]) -> float | None:
    take_profits = _parse_take_profits(signal.get("take_profits"))
    return take_profits[0] if take_profits else None


def _signal_time(signal: Mapping[str, Any]) -> datetime:
    return _coerce_datetime(signal.get("source_received_at") or signal.get("created_at"))


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000:
            dt = datetime.fromtimestamp(numeric / 1000.0, tz=timezone.utc)
        else:
            dt = datetime.fromtimestamp(numeric, tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError(f"cannot coerce datetime from {value!r}")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _json_datetime(value: Any) -> str | None:
    if value is None:
        return None
    return _coerce_datetime(value).isoformat()


def _date_range(start: datetime, end: datetime) -> list[datetime.date]:
    last_inclusive = end - timedelta(microseconds=1)
    day = start.date()
    last_day = last_inclusive.date()
    days = []
    while day <= last_day:
        days.append(day)
        day += timedelta(days=1)
    return days


def _kline_cache_path(cache_root: Path, market: str, symbol: str, day: datetime.date) -> Path:
    filename = f"{symbol}-1m-{day.isoformat()}.zip"
    return cache_root / market / symbol / "1m" / filename


def _download_kline_zip(
    zip_path: Path,
    base_url: str,
    market: str,
    symbol: str,
    day: datetime.date,
) -> None:
    filename = zip_path.name
    if market == "spot":
        archive_path = f"data/spot/daily/klines/{symbol}/1m/{filename}"
    else:
        archive_path = f"data/futures/{market}/daily/klines/{symbol}/1m/{filename}"
    url = f"{base_url.rstrip('/')}/{archive_path}"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            zip_path.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise MissingKlines(f"missing {symbol} 1m archive for {day.isoformat()}") from exc
        raise


def _read_kline_zip(zip_path: Path) -> list[Kline]:
    rows: list[Kline] = []
    with zipfile.ZipFile(zip_path) as archive:
        csv_names = [name for name in archive.namelist() if name.endswith(".csv")]
        if not csv_names:
            return rows
        with archive.open(csv_names[0]) as raw_file:
            text_file = io.TextIOWrapper(raw_file, encoding="utf-8")
            reader = csv.reader(text_file)
            for row in reader:
                if not row or not row[0].strip().isdigit():
                    continue
                rows.append(
                    (
                        int(row[0]),
                        float(row[1]),
                        float(row[2]),
                        float(row[3]),
                        float(row[4]),
                    )
                )
    return rows


def _load_signals_from_db_url(db_url: str) -> list[dict[str, Any]]:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required for --db-url; use --signals-json for offline replay") from exc

    with psycopg.connect(db_url) as conn:
        return load_zone_signals(conn=conn)


def _write_json(path: str | Path, payload: Any) -> None:
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _depth_label(depth: float) -> str:
    return f"{int(round(float(depth) * 100))}%"


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _median_or_none(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1%}"


def _format_number(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"


if __name__ == "__main__":
    raise SystemExit(main())
