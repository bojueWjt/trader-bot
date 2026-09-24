#!/usr/bin/env python3
"""Dump a compact trading digest for the Hermes daily/weekly report cron jobs.

stdout is injected into the report prompt. Read-only.
Default window is completed UTC days ending today 00:00, matching
exchange_income_collector.completed_utc_window.

Usage:
  report_data.py [days]
  report_data.py --start 2026-09-23T00:00:00+00:00 --end 2026-09-23T19:00:00+00:00
Importable: render_digest(...)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

DAYS_DEFAULT = 1
PSQL = ["docker", "exec", "trader-v3-postgres", "psql", "-U", "postgres", "-d", "trader", "-Atc"]
ACCOUNTS = ("account-a", "account-b", "account-c", "account-d")
CONTROLPLANE_UNITS = (
    "trader-v3-controlplane-node-control",
    "trader-v3-controlplane-operator-query",
    "trader-v3-controlplane-event-ingest",
)
ROBOT_CLIENT_ORDER_ID_PATTERN = re.compile(r"^B[0-9a-f]{32}[0-9]{2}$")
REPORT_INCOME_TYPES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")
SNAPSHOT_MAX_AGE_SECONDS = 300
CLOCK_SKEW_SECONDS = 5
FILL_EVENT_TYPES = ("OrderFilled", "OrderPartiallyFilled")
ENTRY_ROLES = {"entry", "open", "stop_entry", "open_position"}
EXIT_ROLES = {"stop_loss", "take_profit", "exit", "close", "reduce", "sl", "tp"}

QueryFn = Callable[[str], list[str]]


def q(sql: str) -> list[str]:
    try:
        out = subprocess.run(PSQL + [sql], capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [f"(查询失败: {exc})"]
    if out.returncode != 0:
        return [f"(查询失败: {out.stderr.strip()[:120]})"]
    return [line for line in out.stdout.splitlines() if line.strip()]


def query_failed(rows: list[str] | None) -> bool:
    return bool(rows) and str(rows[0]).startswith("(查询失败")


def split_map(rows: list[str], nfields: int) -> dict[str, tuple[str, ...]]:
    parsed: dict[str, tuple[str, ...]] = {}
    for line in rows:
        parts = line.split("|")
        if len(parts) < nfields:
            continue
        parsed[parts[0]] = tuple(parts[1:nfields])
    return parsed


def is_robot_client_order_id(value: Any) -> bool:
    return isinstance(value, str) and ROBOT_CLIENT_ORDER_ID_PATTERN.fullmatch(value.strip()) is not None


def intent_uuid_from_robot_cid(cid: str) -> str | None:
    if not is_robot_client_order_id(cid):
        return None
    hex32 = cid[1:33]
    return f"{hex32[0:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"


def completed_utc_window(
    days: int,
    now: datetime,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[datetime, datetime]:
    now = _aware(now)
    if days <= 0 and (start is None or end is None):
        raise ValueError("invalid_window_days")
    if start is not None and end is not None:
        window = (_aware(start), _aware(end))
    else:
        completed_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if end is not None:
            completed_end = _aware(end)
        window_start = _aware(start) if start is not None else completed_end - timedelta(days=days)
        window = (window_start, completed_end)
    if window[1] <= window[0]:
        raise ValueError("invalid_window_start_not_before_end")
    return window


def utc_window(days: int, now: datetime) -> tuple[datetime, datetime]:
    return completed_utc_window(days, now)


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).split()[0])
    except (InvalidOperation, IndexError, TypeError, ValueError):
        return None
    if not number.is_finite():
        return None
    return number


def _parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _aware(value)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _aware(parsed)


def _sql_ts(value: datetime) -> str:
    return _aware(value).strftime("%Y-%m-%d %H:%M:%S+00")


def _normalize_symbol(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    before_venue = text.split(".", 1)[0]
    return before_venue.split("-", 1)[0].strip().upper()


def coverage_complete(rows: Sequence[Mapping[str, Any]], account_id: str, start: datetime, end: datetime) -> bool:
    start = _aware(start)
    end = _aware(end)
    if end <= start:
        return False
    covered: list[tuple[datetime, datetime]] = []
    for row in rows:
        if str(row.get("account_id")) != account_id:
            continue
        if not row.get("complete"):
            continue
        income_type = str(row.get("income_type") or "*").strip() or "*"
        if income_type != "*":
            continue
        ws = _parse_ts(row.get("window_start"))
        we = _parse_ts(row.get("window_end"))
        if ws is None or we is None:
            continue
        if we <= start or ws >= end:
            continue
        covered.append((max(ws, start), min(we, end)))
    covered.sort()
    cursor = start
    for ws, we in covered:
        if ws > cursor:
            return False
        if we > cursor:
            cursor = we
        if cursor >= end:
            return True
    return cursor >= end


def systemd_is_active(units: Sequence[str]) -> dict[str, str]:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", *units],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        return {unit: f"数据缺失:{exc}" for unit in units}
    states = [line.strip() for line in proc.stdout.splitlines()]
    result = {}
    for index, unit in enumerate(units):
        result[unit] = states[index] if index < len(states) else "数据缺失"
    return result


def _load_json_map(rows: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for line in rows:
        account_id, _, rest = line.partition("|")
        if not rest:
            continue
        try:
            out[account_id] = json.loads(rest)
        except json.JSONDecodeError:
            out[account_id] = rest
    return out


def _age_ok(
    ts: datetime | None,
    now: datetime,
    max_age: float = SNAPSHOT_MAX_AGE_SECONDS,
    skew: float = CLOCK_SKEW_SECONDS,
) -> bool:
    if ts is None:
        return False
    age = (now - ts).total_seconds()
    return -skew <= age <= max_age


def _order_client_id(row: Mapping[str, Any]) -> str:
    for field in ("client_order_id", "clientOrderId", "client_algo_id", "clientAlgoId"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _protected_side(row: Mapping[str, Any]) -> str | None:
    explicit = str(row.get("position_side") or row.get("positionSide") or "").strip().lower()
    if explicit in {"long", "short"}:
        return explicit
    side = str(row.get("side") or "").strip().lower()
    if side in {"sell", "short"}:
        return "long"
    if side in {"buy", "long"}:
        return "short"
    return None


def _snapshot_book_side(raw_side: Any, quantity: Decimal) -> str | None:
    token = str(raw_side or "").strip().upper()
    if token in {"LONG", "BUY"}:
        return "long"
    if token in {"SHORT", "SELL"}:
        return "short"
    if token in {"BOTH", "NET"}:
        if quantity > 0:
            return "long"
        if quantity < 0:
            return "short"
        return None
    return None


def parse_heartbeat_books(
    items: Any,
) -> tuple[set[tuple[str, str]], list[str]] | None:
    """Strict venue books. Any bad row rejects the whole snapshot."""
    if not isinstance(items, list):
        return None
    books: set[tuple[str, str]] = set()
    held: list[str] = []
    for row in items:
        if not isinstance(row, dict):
            return None
        symbol = _normalize_symbol(row.get("symbol") or row.get("instrument_id"))
        if not symbol:
            return None
        qty = _decimal(
            row.get("position_amt") if row.get("position_amt") is not None else row.get("quantity")
        )
        if qty is None:
            return None
        raw_side = row.get("position_side") or row.get("positionSide") or row.get("side")
        if qty == 0:
            continue
        side = _snapshot_book_side(raw_side, qty)
        if side not in {"long", "short"}:
            return None
        key = (symbol, side)
        if key in books:
            return None
        books.add(key)
        held.append(f"{symbol} {raw_side or side} {qty}")
    return books, held


def _venue_books_from_positions(items: Any) -> set[tuple[str, str]] | None:
    parsed = parse_heartbeat_books(items)
    if parsed is None:
        return None
    books, _held = parsed
    return books


def _fill_attribution(
    client_order_id: str,
    trade_key: tuple[str, str, str],
    robot_keys: set[tuple[str, str, str]],
    conflicting: bool,
) -> str:
    if conflicting:
        return "unknown"
    if is_robot_client_order_id(client_order_id) or trade_key in robot_keys:
        return "robot"
    if client_order_id:
        return "manual"
    return "unknown"


def _truthy_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y"}:
        return True
    if text in {"false", "f", "0", "no", "n"}:
        return False
    return None


def _mapping_role(src: Mapping[str, Any] | None) -> str:
    if not isinstance(src, Mapping):
        return ""
    for key in ("lifecycle_role", "role", "intent_role"):
        value = str(src.get(key) or "").strip().lower()
        if value:
            return value
    tags = src.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            text = str(tag)
            if text.startswith("lifecycle_role="):
                return text.split("=", 1)[1].strip().lower()
    return ""


def _is_reduce_only(*sources: Mapping[str, Any] | None) -> bool | None:
    for src in sources:
        if not isinstance(src, Mapping):
            continue
        parsed = _truthy_bool(src.get("reduce_only"))
        if parsed is True:
            return True
        if parsed is False:
            return False
    return None


def _is_nonreduce_entry(*sources: Mapping[str, Any] | None) -> bool:
    reduce_only = _is_reduce_only(*sources)
    if reduce_only is not False:
        return False
    role = ""
    for src in sources:
        role = _mapping_role(src)
        if role:
            break
    if role in EXIT_ROLES:
        return False
    return True


def _order_side_token(*sources: Mapping[str, Any] | None) -> str:
    for src in sources:
        if not isinstance(src, Mapping):
            continue
        side = str(src.get("side") or src.get("order_side") or "").strip().lower()
        if side in {"buy", "sell", "long", "short"}:
            return side
    return ""


def _robot_order_evidence_ok(
    raw: Mapping[str, Any],
    enrich: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> bool:
    cid = _order_client_id(raw)
    if not cid or not is_robot_client_order_id(cid):
        return False
    symbol = _normalize_symbol(raw.get("symbol") or enrich.get("instrument"))
    if not symbol:
        return False
    if not _order_side_token(raw, enrich, plan):
        return False
    if _is_reduce_only(raw, enrich, plan) is None:
        return False
    return True


def _fill_identity(
    account_id: str,
    symbol: str,
    trade_id: str,
    event_id: str,
    cid: str,
    ts: str,
) -> tuple[str, ...]:
    if trade_id:
        return ("trade", account_id, symbol, trade_id)
    if event_id:
        return ("event", account_id, event_id)
    return ("row", account_id, symbol, cid, ts)


def render_digest(
    *,
    days: int = DAYS_DEFAULT,
    now: datetime | None = None,
    query: QueryFn | None = None,
    systemd_status: Mapping[str, str] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> str:
    query = query or q
    now = _aware(now or datetime.now(timezone.utc))
    try:
        start, end = completed_utc_window(
            days, now, start=window_start, end=window_end
        )
    except ValueError:
        return "### 数据窗口: 数据缺失 无效窗口\n"
    if end > now:
        end = now
    if end <= start:
        return "### 数据窗口: 数据缺失 无效窗口\n"
    start_sql = _sql_ts(start)
    end_sql = _sql_ts(end)
    lines: list[str] = []
    lines.append(f"### 数据窗口: {start_sql} ≤ t < {end_sql} (UTC 半开, {days} 天)")

    lines.append("\n### 交易意向")
    intent_rows = query(
        "SELECT to_char(created_at,'MM-DD HH24:MI'), instrument_id, action, status, "
        "substr(order_plan::text,1,120) FROM trade_intents "
        f"WHERE created_at >= '{start_sql}' AND created_at < '{end_sql}' ORDER BY created_at"
    )
    if query_failed(intent_rows):
        lines.append("数据缺失: trade_intents")
    else:
        lines.append("\n".join(intent_rows) if intent_rows else "(无)")

    lines.append("\n### 心跳仓位")
    hb_rows = query(
        "SELECT account_id, last_seen_at, positions_snapshot_at, positions, payload, "
        "clock_timestamp() "
        "FROM node_heartbeats ORDER BY account_id"
    )
    hb_valid: dict[str, dict[str, Any]] = {}
    if query_failed(hb_rows):
        lines.append("数据缺失: heartbeat.positions")
    else:
        seen_accounts: set[str] = set()
        duplicate_accounts: set[str] = set()
        for line in hb_rows or []:
            parts = line.split("|", 5)
            if len(parts) < 4:
                continue
            account_id = parts[0]
            if account_id in seen_accounts:
                duplicate_accounts.add(account_id)
                hb_valid.pop(account_id, None)
                lines.append(f"{account_id}: 数据缺失 heartbeat 重复账户")
                continue
            seen_accounts.add(account_id)
            last_seen = _parse_ts(parts[1])
            snap_at = _parse_ts(parts[2])
            query_now = _parse_ts(parts[5]) if len(parts) > 5 else now
            try:
                positions = json.loads(parts[3])
            except json.JSONDecodeError:
                positions = None
            payload = {}
            if len(parts) > 4:
                payload_s = parts[4]
                try:
                    payload = json.loads(payload_s) if payload_s.startswith("{") else {}
                except json.JSONDecodeError:
                    payload = {}
            recon = str((payload or {}).get("reconciliation_state") or "").lower()
            clock = query_now or now
            if snap_at is None or not _age_ok(snap_at, clock) or not _age_ok(last_seen, clock) or recon != "healthy":
                lines.append(f"{account_id}: 数据缺失 heartbeat 不新鲜或不健康")
                continue
            parsed = parse_heartbeat_books(positions)
            if parsed is None:
                lines.append(f"{account_id}: 数据缺失 positions 行损坏")
                continue
            books, held = parsed
            hb_valid[account_id] = {
                "positions": positions,
                "books": books,
                "last_seen": last_seen,
                "snap_at": snap_at,
                "payload": payload,
                "clock": clock,
            }
            lines.append(
                f"{account_id}: snapshot_at={parts[2]} last_seen={parts[1]} "
                f"n={len(positions)} " + (", ".join(held) if held else "空仓")
            )
        for account_id in ACCOUNTS:
            if account_id not in seen_accounts:
                lines.append(f"{account_id}: 数据缺失 heartbeat")

    lines.append("\n### 成交 (OrderFilled/OrderPartiallyFilled, 去重后明细)")
    fill_sql = (
        "SELECT account_id, event_type, COALESCE(trade_id,''), COALESCE(client_order_id,''), "
        "COALESCE(payload->>'instrument_id',''), "
        "COALESCE(payload->>'order_side', payload->>'side',''), "
        "COALESCE(payload->>'last_qty', payload->>'filled_qty',''), "
        "COALESCE(payload->>'last_px', payload->>'avg_px',''), "
        "COALESCE(payload->>'realized_pnl',''), COALESCE(payload->>'commission',''), "
        "to_char(ts_event,'MM-DD HH24:MI:SS'), "
        "COALESCE(payload->>'source',''), "
        "COALESCE(payload->>'commission_asset','USDT'), "
        "COALESCE(event_id::text,'') "
        "FROM execution_events "
        f"WHERE event_type IN ('OrderFilled','OrderPartiallyFilled') "
        f"AND ts_event >= '{start_sql}' AND ts_event < '{end_sql}' "
        "ORDER BY ts_event, event_id"
    )
    fill_rows = query(fill_sql)
    fills_ok = not query_failed(fill_rows)
    fills_by_account: dict[str, list[dict[str, Any]]] = {account: [] for account in ACCOUNTS}
    robot_keys: set[tuple[str, str, str]] = set()
    fill_by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    cids_by_trade: dict[tuple[str, str, str], set[str]] = {}
    if not fills_ok:
        lines.append("数据缺失: execution_events fills")
    else:
        for line in fill_rows or []:
            parts = line.split("|")
            if len(parts) < 11:
                continue
            while len(parts) < 14:
                parts.append("")
            account_id, event_type, trade_id, cid, instrument, order_side, qty, px, pnl, fee, ts, source, asset, event_id = parts[:14]
            symbol = _normalize_symbol(instrument)
            if trade_id:
                cids_by_trade.setdefault((account_id, symbol, trade_id), set()).add(cid)
            key = _fill_identity(account_id, symbol, trade_id, event_id, cid, ts)
            row = {
                "account_id": account_id,
                "event_type": event_type,
                "trade_id": trade_id,
                "client_order_id": cid,
                "instrument": instrument,
                "symbol": symbol,
                "order_side": order_side,
                "qty": qty,
                "px": px,
                "pnl": pnl,
                "fee": fee,
                "ts": ts,
                "source": source,
                "asset": asset or "USDT",
                "event_id": event_id,
            }
            existing = fill_by_key.get(key)
            if existing is None:
                fill_by_key[key] = row
            else:
                existing_score = (_decimal(existing.get("pnl")) is not None) + (
                    _decimal(existing.get("fee")) is not None
                )
                new_score = (_decimal(pnl) is not None) + (_decimal(fee) is not None)
                if new_score > existing_score:
                    fill_by_key[key] = row
        for row in sorted(fill_by_key.values(), key=lambda item: (item["ts"], item["account_id"])):
            fills_by_account.setdefault(row["account_id"], []).append(row)
            if is_robot_client_order_id(row["client_order_id"]) and row["trade_id"]:
                robot_keys.add((row["account_id"], row["symbol"], row["trade_id"]))
        for account_id in ACCOUNTS:
            rows = fills_by_account.get(account_id) or []
            lines.append(f"{account_id}: {len(rows)} 笔")
            for row in rows:
                lines.append(
                    f"  {row['ts']} {row['instrument']} {row['order_side']} "
                    f"qty={row['qty']} px={row['px']} {row['event_type']}"
                    + (f" source={row['source']}" if row.get("source") else "")
                )

    lines.append("\n### 交易所收入 (REALIZED_PNL / COMMISSION / FUNDING_FEE)")
    coverage_rows_raw = query(
        "SELECT account_id, window_start, window_end, complete, income_type "
        "FROM exchange_income_coverage WHERE income_type = '*'"
    )
    coverage_query_failed = query_failed(coverage_rows_raw)
    coverage_missing_table = coverage_query_failed and (
        "does not exist" in coverage_rows_raw[0].lower() or "不存在" in coverage_rows_raw[0]
    )
    coverage_structs: list[dict[str, Any]] = []
    if not coverage_query_failed:
        for line in coverage_rows_raw or []:
            parts = line.split("|")
            if len(parts) < 4:
                continue
            coverage_structs.append(
                {
                    "account_id": parts[0],
                    "window_start": parts[1],
                    "window_end": parts[2],
                    "complete": str(parts[3]).lower() in {"t", "true", "1"},
                    "income_type": parts[4] if len(parts) > 4 else "*",
                }
            )
    income_detail = query(
        "SELECT account_id, income_type, asset, income, COALESCE(trade_id,''), COALESCE(symbol,'') "
        "FROM exchange_income "
        f"WHERE income_time >= '{start_sql}' AND income_time < '{end_sql}' "
        "AND income_type IN ('REALIZED_PNL','COMMISSION','FUNDING_FEE')"
    ) if not coverage_query_failed else ["(查询失败: coverage)"]
    income_failed = query_failed(income_detail)
    if coverage_query_failed or coverage_missing_table:
        lines.append("数据缺失: exchange_income 未采集（本窗口无完整 coverage）")
    else:
        any_complete = False
        for account_id in ACCOUNTS:
            complete = coverage_complete(coverage_structs, account_id, start, end)
            if not complete:
                lines.append(f"{account_id}: 数据缺失（无完整 income coverage）")
                continue
            if income_failed:
                lines.append(f"{account_id}: 数据缺失 income 查询失败")
                continue
            if not fills_ok:
                lines.append(f"{account_id}: 数据缺失 成交归属（fills 查询失败）")
                continue
            any_complete = True
            rows = [
                line.split("|")
                for line in income_detail or []
                if line.split("|")[0] == account_id and len(line.split("|")) >= 6
            ]
            by_type_asset: dict[tuple[str, str], Decimal] = {}
            by_type_attr: dict[tuple[str, str, str], Decimal] = {}
            close_trades: set[tuple[str, str]] = set()
            malformed = False
            for parts in rows:
                _acct, income_type, asset, income_s, trade_id, symbol = parts[:6]
                income = _decimal(income_s)
                if income is None:
                    malformed = True
                    break
                asset_name = asset or "USDT"
                by_type_asset[(income_type, asset_name)] = (
                    by_type_asset.get((income_type, asset_name), Decimal("0")) + income
                )
                if income_type == "FUNDING_FEE":
                    attr = "account"
                elif not trade_id:
                    attr = "unknown"
                else:
                    norm_symbol = _normalize_symbol(symbol)
                    key = (account_id, norm_symbol, trade_id)
                    raw_cids = cids_by_trade.get(key) or set()
                    if not raw_cids:
                        matches = [
                            item
                            for item in (fills_by_account.get(account_id) or [])
                            if item["trade_id"] == trade_id
                            and (
                                not norm_symbol
                                or item["symbol"] == norm_symbol
                            )
                        ]
                        raw_cids = {item["client_order_id"] for item in matches}
                    conflict = len(raw_cids) > 1 and not (
                        all(is_robot_client_order_id(cid) for cid in raw_cids if cid)
                        or all(not is_robot_client_order_id(cid) for cid in raw_cids)
                    )
                    cid = next(iter(raw_cids), "")
                    attr = _fill_attribution(cid, key, robot_keys, conflict)
                    if not raw_cids:
                        attr = "unknown"
                by_type_attr[(income_type, attr, asset_name)] = (
                    by_type_attr.get((income_type, attr, asset_name), Decimal("0")) + income
                )
                if income_type == "REALIZED_PNL" and trade_id:
                    close_trades.add((_normalize_symbol(symbol), trade_id))
            if malformed:
                lines.append(f"{account_id}: 数据缺失 income 行损坏")
                continue
            chunks = []
            for income_type in REPORT_INCOME_TYPES:
                assets = [
                    f"{asset}={amount}"
                    for (itype, asset), amount in sorted(by_type_asset.items())
                    if itype == income_type
                ]
                attrs = [
                    f"{attr}:{asset}={amount}"
                    for (itype, attr, asset), amount in sorted(by_type_attr.items())
                    if itype == income_type
                ]
                chunks.append(
                    f"{income_type} asset[" + (",".join(assets) or "无") + "] attr[" + (",".join(attrs) or "无") + "]"
                )
            lines.append(f"{account_id}: " + " / ".join(chunks))
            lines.append(
                f"{account_id}: 平仓成交笔数={len(close_trades)} "
                "（income REALIZED_PNL 去重 symbol+trade_id，不是完整仓位生命周期；"
                "无法用 income 单独证明保本平仓）"
            )
        if not any_complete:
            lines.append("健康: 收入未采集，禁止把缺失写成 0")

    lines.append("\n### 已观测 exchange_reconciliation 成交（含 pnl=0 手续费，不完整，非全量）")
    if not fills_ok:
        lines.append("数据缺失: exchange_reconciliation fills")
    else:
        recon_rows = [
            row
            for row in fill_by_key.values()
            if row.get("source") == "exchange_reconciliation"
        ]
        shown = 0
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in sorted(recon_rows, key=lambda item: (item["ts"], item["account_id"])):
            shown += 1
            pnl = _decimal(row["pnl"])
            fee = _decimal(row["fee"]) if row.get("fee") not in {None, ""} else None
            attr = _fill_attribution(
                row["client_order_id"],
                (row["account_id"], row["symbol"], row["trade_id"]),
                robot_keys,
                False,
            )
            gkey = (row["account_id"], row.get("asset") or "USDT", attr)
            bucket = grouped.setdefault(
                gkey, {"pnl": Decimal("0"), "fee": Decimal("0"), "fee_missing": False}
            )
            if pnl is not None:
                bucket["pnl"] += pnl
            if fee is None:
                bucket["fee_missing"] = True
            else:
                bucket["fee"] += fee
            if pnl is not None and pnl != 0:
                lines.append(
                    f"{row['account_id']} {row['ts']} {row['instrument']} "
                    f"realized_pnl={row['pnl']} commission={row['fee'] or '数据缺失'} "
                    f"cid={row['client_order_id']} （补录观测，非 Binance 全量）"
                )
            elif fee is not None or row.get("fee") in {None, ""}:
                lines.append(
                    f"{row['account_id']} {row['ts']} {row['instrument']} "
                    f"realized_pnl={row['pnl'] or '0'} commission={row['fee'] or '数据缺失'} "
                    f"cid={row['client_order_id']} （补录手续费，pnl=0）"
                )
        if shown == 0:
            lines.append("(无补录成交)")
        for (account_id, asset, attr), bucket in sorted(grouped.items()):
            fee_s = "数据缺失" if bucket["fee_missing"] else str(bucket["fee"])
            lines.append(
                f"{account_id} 补录合计 asset={asset} attr={attr} "
                f"realized_pnl={bucket['pnl']} commission={fee_s} "
                "（观测汇总，非全量）"
            )

    lines.append("\n### 四账户权益")
    equity_rows = query("SELECT account_id, equity, available_balance FROM accounts_projection")
    if query_failed(equity_rows):
        for account_id in ACCOUNTS:
            lines.append(f"{account_id}: 数据缺失 权益")
    else:
        equity = split_map(equity_rows, 3)
        for account_id in ACCOUNTS:
            if account_id not in equity:
                lines.append(f"{account_id}: 数据缺失 权益")
                continue
            eq, available = equity[account_id]
            if eq in {"?", ""} or available in {"?", ""}:
                lines.append(f"{account_id}: 数据缺失 权益")
            else:
                lines.append(f"{account_id}: 权益 {eq} / 可用 {available}")

    lines.append("\n### 在场机器人单（镜像权威，心跳仓位核对）")
    mirror_rows = query(
        "SELECT account_id, payload, updated_at, clock_timestamp() "
        "FROM exchange_state_mirror"
    )
    proj_rows = query(
        "SELECT o.account_id, o.client_order_id, o.instrument_id, o.side::text, "
        "COALESCE(o.reduce_only::text,''), o.status, "
        "COALESCE(ti.order_plan->>'entry_expires_at',''), "
        "COALESCE(o.payload->>'position_side', ti.order_plan->>'position_side',''), "
        "COALESCE(o.payload->>'lifecycle_role', ti.order_plan->>'lifecycle_role','') "
        "FROM orders_projection o "
        "LEFT JOIN trade_intents ti ON ti.intent_id = o.intent_id"
    )
    intent_plan_rows = query(
        "SELECT intent_id::text, "
        "COALESCE(order_plan->>'entry_expires_at',''), "
        "COALESCE(order_plan->>'lifecycle_role',''), "
        "COALESCE(order_plan->>'position_side',''), "
        "COALESCE(order_plan->>'reduce_only','') "
        "FROM trade_intents"
    )
    if query_failed(mirror_rows):
        lines.append("数据缺失: exchange_state_mirror")
    elif query_failed(proj_rows):
        lines.append("数据缺失: orders_projection")
    else:
        intent_by_id: dict[str, dict[str, str]] = {}
        if not query_failed(intent_plan_rows):
            for line in intent_plan_rows or []:
                parts = line.split("|")
                if len(parts) < 5:
                    continue
                intent_by_id[parts[0]] = {
                    "expires_at": parts[1],
                    "lifecycle_role": parts[2],
                    "position_side": parts[3],
                    "reduce_only": parts[4],
                }
        mirror_by: dict[str, tuple[Any, datetime | None, datetime | None]] = {}
        for line in mirror_rows or []:
            account_id, _, rest = line.partition("|")
            head, _, clock_s = rest.rpartition("|")
            clock = _parse_ts(clock_s)
            if clock is None:
                payload_s, _, updated_s = rest.rpartition("|")
                clock = now
            else:
                payload_s, _, updated_s = head.rpartition("|")
            try:
                payload = json.loads(payload_s)
            except json.JSONDecodeError:
                payload = None
            mirror_by[account_id] = (payload, _parse_ts(updated_s), clock)
        proj_by_cid: dict[tuple[str, str], dict[str, str]] = {}
        for line in proj_rows or []:
            parts = line.split("|")
            if len(parts) < 8:
                continue
            while len(parts) < 9:
                parts.append("")
            proj_by_cid[(parts[0], parts[1])] = {
                "instrument": parts[2],
                "side": parts[3],
                "reduce_only": parts[4],
                "status": parts[5],
                "expires_at": parts[6],
                "position_side": parts[7],
                "lifecycle_role": parts[8],
            }
        expired_live = []
        expiry_unknown = []
        orphan_protect = []
        missing_accounts = []
        for account_id in ACCOUNTS:
            packed = mirror_by.get(account_id)
            if packed is None:
                missing_accounts.append(account_id)
                continue
            payload, updated_at, query_now = packed
            if not isinstance(payload, Mapping):
                missing_accounts.append(account_id)
                continue
            fetched_at = _parse_ts(
                payload.get("fetched_at") or payload.get("captured_at")
            )
            snapshot_ts = updated_at
            if fetched_at and updated_at:
                snapshot_ts = min(fetched_at, updated_at)
            elif fetched_at:
                snapshot_ts = fetched_at
            clock = query_now or now
            if not _age_ok(snapshot_ts, clock):
                missing_accounts.append(account_id)
                continue
            if not isinstance(payload.get("open_orders"), list) or not isinstance(
                payload.get("algo_orders"), list
            ):
                missing_accounts.append(account_id)
                continue
            if account_id not in hb_valid:
                missing_accounts.append(account_id)
                continue
            books = hb_valid[account_id].get("books")
            if books is None:
                missing_accounts.append(account_id)
                continue
            open_orders = payload.get("open_orders")
            algo_orders = payload.get("algo_orders")
            if not isinstance(open_orders, list) or not isinstance(algo_orders, list):
                missing_accounts.append(account_id)
                continue
            robot_orders: list[tuple[Mapping[str, Any], dict[str, str], dict[str, str]]] = []
            orders_ok = True
            for raw in list(open_orders) + list(algo_orders):
                if not isinstance(raw, Mapping):
                    orders_ok = False
                    break
                cid = _order_client_id(raw)
                if not cid:
                    orders_ok = False
                    break
                if not is_robot_client_order_id(cid):
                    continue
                enrich = dict(proj_by_cid.get((account_id, cid)) or {})
                intent_id = intent_uuid_from_robot_cid(cid)
                plan = dict(intent_by_id.get(intent_id) or {}) if intent_id else {}
                if not enrich.get("expires_at") and plan.get("expires_at"):
                    enrich["expires_at"] = plan["expires_at"]
                if not enrich.get("lifecycle_role") and plan.get("lifecycle_role"):
                    enrich["lifecycle_role"] = plan["lifecycle_role"]
                if not enrich.get("position_side") and plan.get("position_side"):
                    enrich["position_side"] = plan["position_side"]
                if enrich.get("reduce_only") in {None, ""} and plan.get("reduce_only"):
                    enrich["reduce_only"] = plan["reduce_only"]
                if not _robot_order_evidence_ok(raw, enrich, plan):
                    orders_ok = False
                    break
                robot_orders.append((raw, enrich, plan))
            if not orders_ok:
                missing_accounts.append(account_id)
                continue
            for raw, enrich, plan in robot_orders:
                cid = _order_client_id(raw)
                reduce_only = _is_reduce_only(raw, enrich, plan)
                entry_like = _is_nonreduce_entry(raw, enrich, plan)
                expires = _parse_ts(enrich.get("expires_at"))
                symbol = _normalize_symbol(raw.get("symbol") or enrich.get("instrument"))
                if reduce_only is True:
                    protected = _protected_side({**plan, **enrich, **raw})
                    if protected and symbol and (symbol, protected) not in books:
                        orphan_protect.append(
                            f"{account_id} {cid} {symbol} reduce_only {raw.get('side')} "
                            f"protect={protected} 无对应仓"
                        )
                elif entry_like:
                    if expires is None:
                        expiry_unknown.append(
                            f"{account_id} {cid} {symbol} 数据缺失: entry expiry metadata"
                        )
                    elif expires <= clock:
                        expired_live.append(
                            f"{account_id} {cid} {raw.get('symbol')} "
                            f"expires={enrich.get('expires_at')} 仍在场"
                        )
        if missing_accounts:
            lines.append("数据缺失: " + ",".join(sorted(set(missing_accounts))) + " 镜像或心跳仓位")
        lines.append("过期仍在场 entry:")
        if missing_accounts and not expired_live and not expiry_unknown:
            lines.append("数据缺失")
        elif expired_live or expiry_unknown:
            lines.extend(expired_live)
            lines.extend(expiry_unknown)
        else:
            lines.append("(无)")
        lines.append("无仓保护单:")
        if missing_accounts and not orphan_protect:
            lines.append("数据缺失")
        else:
            lines.extend(orphan_protect or ["(无)"])
        lines.append("手动单不报警（clientOrderId 不匹配 "
                     r"^B[0-9a-f]{32}[0-9]{2}$ ）")

    lines.append("\n### 当前挂单")
    open_rows = query(
        "SELECT account_id, instrument_id, status, count(*) FROM orders_projection "
        "WHERE status IN ('accepted','partially_filled') GROUP BY 1,2,3 ORDER BY 1,2"
    )
    if query_failed(open_rows):
        lines.append("数据缺失: orders_projection")
    else:
        lines.append("\n".join(open_rows) if open_rows else "(无)")

    lines.append("\n### 被拒/异常事件计数")
    rej_rows = query(
        "SELECT event_type, count(*) FROM execution_events "
        f"WHERE ts_event >= '{start_sql}' AND ts_event < '{end_sql}' "
        "AND event_type IN ('OrderRejected','OrderDenied','OrderExpired','OrderCanceled') GROUP BY 1"
    )
    if query_failed(rej_rows):
        lines.append("数据缺失: rejected events")
    else:
        lines.append("\n".join(rej_rows) if rej_rows else "(无)")

    lines.append("\n### 系统健康")
    status = systemd_status if systemd_status is not None else systemd_is_active(CONTROLPLANE_UNITS)
    lines.append(
        "服务: "
        + ", ".join(f"{unit}={status.get(unit, '数据缺失')}" for unit in CONTROLPLANE_UNITS)
    )
    node_rows = query(
        "SELECT count(DISTINCT node_id) FROM execution_events "
        "WHERE ts_ingest > now() - interval '10 minutes'"
    )
    if query_failed(node_rows):
        lines.append("数据缺失: 近10分钟节点上报")
    else:
        lines.append(f"近10分钟有事件上报的节点数: {node_rows[0] if node_rows else 0}")
    try:
        state = json.load(open("/srv/trader-v3/scripts/.order_lifecycle_state.json"))
        brainfail = int(state.get("brainfail:primary", 0))
        lines.append("大脑主通道探活: " + (f"连续失败 {brainfail} 次(备胎在岗)" if brainfail else "正常"))
    except Exception:
        lines.append("大脑探活状态: 数据缺失")
    try:
        journal = subprocess.run(
            ["journalctl", "-u", "trader-v3-hermes-feeder", f"--since=-{days}d", "--no-pager", "-q"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if journal.returncode != 0:
            lines.append("窗口内大脑失败重试次数: 数据缺失")
            lines.append("\n### 信号投递异常")
            lines.append("数据缺失")
        else:
            brainfails = sum(1 for line in journal.stdout.splitlines() if "brain failure" in line)
            lines.append(f"窗口内大脑失败重试次数: {brainfails}")
            skips = [
                line.split("]: ", 1)[-1]
                for line in journal.stdout.splitlines()
                if "SKIPPING" in line or "blocked" in line.lower()
            ]
            lines.append("\n### 信号投递异常")
            lines.append("\n".join(skips[-5:]) if skips else "(无)")
    except Exception:
        lines.append("窗口内大脑失败重试次数: 数据缺失")
        lines.append("\n### 信号投递异常")
        lines.append("数据缺失")
    return "\n".join(lines) + "\n"


def _parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("days", nargs="?", type=int, default=DAYS_DEFAULT)
    parser.add_argument("--start", help="UTC ISO half-open window start")
    parser.add_argument("--end", help="UTC ISO half-open window end (must be <= now)")
    parser.add_argument("--as-of", dest="as_of", help="clock for completed-day default window")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    now = datetime.now(timezone.utc)
    if args.as_of:
        now = _parse_when(args.as_of) or now
    start = _parse_when(args.start)
    end = _parse_when(args.end)
    if end is not None and end > datetime.now(timezone.utc):
        end = datetime.now(timezone.utc)
    sys.stdout.write(
        render_digest(
            days=args.days,
            now=now,
            window_start=start,
            window_end=end,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
