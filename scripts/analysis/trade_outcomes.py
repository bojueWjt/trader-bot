from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

import psycopg2
from psycopg2.extras import Json

try:
    from scripts.analysis.zone_penetration_stats import (
        DEFAULT_BASE_URL,
        Kline,
        MissingKlines,
        UnsupportedSymbol,
        _coerce_datetime,
        load_klines,
        normalize_symbol,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.analysis.zone_penetration_stats import (
        DEFAULT_BASE_URL,
        Kline,
        MissingKlines,
        UnsupportedSymbol,
        _coerce_datetime,
        load_klines,
        normalize_symbol,
    )


OUTCOME_NAMESPACE = uuid.UUID("1f8080b4-16ed-4b25-bd41-8ce106a76f02")
JOB_NAME = "trade_outcomes"
ROBOT_CLIENT_ORDER_ID_PATTERN = re.compile(r"^B[0-9a-f]{32}[0-9]{2}$")
ORDER_SIDE = {
    1: "buy",
    2: "sell",
    "1": "buy",
    "2": "sell",
    "BUY": "buy",
    "SELL": "sell",
    "buy": "buy",
    "sell": "sell",
}
POSITION_SIDE = {2: "long", 3: "short", "LONG": "long", "SHORT": "short", "long": "long", "short": "short"}


@dataclass
class EpisodeBuildStats:
    robot_fill_count: int = 0
    dropped_fill_count: int = 0
    skipped_non_robot_fill_count: int = 0
    unmatched_exit_count: int = 0
    unmatched_exits: list[dict[str, Any]] | None = None
    open_lot_count: int = 0
    deduped_fill_count: int = 0
    diagnostics: list[str] | None = None


def is_robot_client_order_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and ROBOT_CLIENT_ORDER_ID_PATTERN.fullmatch(value.strip()) is not None
    )


def build_trade_outcome(
    intent: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    *,
    klines: Sequence[Kline] | None,
    kline_source: str | None,
    kline_skip_reason: str | None = None,
) -> dict[str, Any] | None:
    closure = _closure_info(intent, events)
    if closure is None:
        closure = _closure_from_closed_lot(intent, events)
    if closure is None:
        return None

    side = closure["side"]
    fills = closure["fills"]
    closed_events = closure["closed_events"]
    entry_avg, entry_qty = closure["entry_avg"], closure["entry_qty"]
    exit_avg, exit_qty = closure["exit_avg"], closure["exit_qty"]
    filled_quantity = entry_qty
    first_fill_at = closure["first_fill_at"]
    closed_at = closure["closed_at"]
    holding_seconds = int((closed_at - first_fill_at).total_seconds())
    realized_pnl = intent.get("realized_pnl")
    if intent.get("pnl_unresolved"):
        realized_pnl = None
    elif realized_pnl is None:
        # Venue fill realized_pnl is the authority. Nautilus Position.realized_pnl
        # is a cumulative net that already includes commission and must not be
        # treated as Binance full-account PnL.
        realized_pnl = _fill_realized_pnl_sum(fills)
        if realized_pnl is None and entry_avg is not None and exit_avg is not None:
            realized_pnl = _fallback_realized_pnl(
                side, entry_avg, exit_avg, exit_qty or filled_quantity
            )

    fees, fee_reason = _sum_fees(fills)
    initial_risk, r_multiple, risk_detail = _risk_metrics(intent.get("order_plan") or {}, entry_avg, filled_quantity, realized_pnl)
    mae_mfe = _mae_mfe(side, entry_avg, klines or [], first_fill_at, closed_at)

    details: dict[str, Any] = {"close_basis": closure["close_basis"]}
    if intent.get("allocation_method"):
        details["allocation_method"] = intent["allocation_method"]
    if intent.get("attribution"):
        details["attribution"] = intent["attribution"]
    if intent.get("lot_key"):
        details["lot_key"] = intent["lot_key"]
    if intent.get("position_side"):
        details["position_side"] = intent["position_side"]
    if intent.get("diagnostics"):
        details["diagnostics"] = list(intent["diagnostics"])
    if intent.get("pnl_unresolved"):
        details["pnl"] = {"status": "missing", "reason": "unresolved_or_missing_prices"}
    contributing = intent.get("contributing_intent_ids")
    if contributing and len(contributing) > 1:
        details["contributing_intent_ids"] = list(contributing)
    if fee_reason:
        details["fees"] = {"reason": fee_reason}
    if risk_detail:
        details["r_multiple"] = risk_detail
    if kline_skip_reason is not None:
        details["mae_mfe"] = {"status": "skipped", "reason": kline_skip_reason}
    elif mae_mfe["mae"] is None:
        details["mae_mfe"] = {"status": "skipped", "reason": "no klines in holding window"}

    lot_key = str(intent.get("lot_key") or f"{intent.get('intent_id')}:{intent.get('account_id')}")
    return {
        "outcome_id": str(uuid.uuid5(OUTCOME_NAMESPACE, f"lot:{intent.get('account_id')}:{lot_key}")),
        "intent_id": intent.get("intent_id"),
        "account_id": intent.get("account_id"),
        "lot_key": lot_key,
        "position_side": intent.get("position_side") or side,
        "attribution": intent.get("attribution") or "unknown",
        "instrument_id": intent.get("instrument_id"),
        "side": side,
        "entry_avg_price": entry_avg,
        "exit_avg_price": exit_avg,
        "filled_quantity": filled_quantity,
        "realized_pnl": realized_pnl,
        "fees": fees,
        "initial_risk": initial_risk,
        "r_multiple": r_multiple,
        "mae": mae_mfe["mae"],
        "mfe": mae_mfe["mfe"],
        "mae_price": mae_mfe["mae_price"],
        "mfe_price": mae_mfe["mfe_price"],
        "holding_seconds": holding_seconds,
        "first_fill_at": first_fill_at,
        "closed_at": closed_at,
        "kline_source": kline_source,
        "details": details,
    }


def load_closed_intents(
    conn: Any,
    *,
    intent_id: str | None = None,
    stats: EpisodeBuildStats | None = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT
            ee.account_id,
            ee.intent_id::text,
            ee.event_type,
            ee.ts_event,
            ee.payload,
            ti.instrument_id,
            ti.order_plan,
            ee.client_order_id,
            ee.trade_id,
            ee.event_id::text
        FROM execution_events ee
        LEFT JOIN trade_intents ti ON ti.intent_id = ee.intent_id
        WHERE ee.event_type IN (
            'OrderFilled', 'OrderPartiallyFilled',
            'PositionOpened', 'PositionChanged', 'PositionClosed'
        )
        ORDER BY ee.account_id, ee.ts_event, ee.event_id
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    records = [
        {
            "account_id": row[0],
            "intent_id": row[1],
            "event_type": row[2],
            "ts_event": row[3],
            "payload": row[4] or {},
            "instrument_id": row[5] or (row[4] or {}).get("instrument_id"),
            "order_plan": row[6] or {},
            "client_order_id": row[7],
            "trade_id": row[8],
            "event_id": row[9],
        }
        for row in rows
    ]
    episodes = build_position_episodes(records, stats=stats)
    if intent_id:
        episodes = [episode for episode in episodes if episode["intent_id"] == intent_id]
    return episodes


def _normalize_order_side(value: Any) -> str | None:
    mapped = ORDER_SIDE.get(value)
    if mapped:
        return mapped
    if value is None:
        return None
    return ORDER_SIDE.get(str(value).strip())


def _signed_fill_qty(payload: Mapping[str, Any]) -> Decimal | None:
    qty = _payload_decimal(payload, "last_qty", "quantity", "filled_qty")
    if qty is None:
        return None
    order_side = _normalize_order_side(payload.get("order_side"))
    if order_side == "buy":
        return qty
    if order_side == "sell":
        return -qty
    return None


def _fill_client_order_id(record: Mapping[str, Any]) -> str:
    raw = record.get("client_order_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    payload = record.get("payload") or {}
    if isinstance(payload, Mapping):
        for field_name in (
            "client_order_id",
            "clientOrderId",
            "client_algo_id",
            "clientAlgoId",
        ):
            value = payload.get(field_name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _episode_instrument_key(record: Mapping[str, Any]) -> str | None:
    for candidate in (record.get("instrument_id"), (record.get("payload") or {}).get("instrument_id")):
        if candidate:
            try:
                return normalize_symbol(str(candidate))
            except Exception:
                return str(candidate)
    return None


def build_position_episodes(
    records: Sequence[Mapping[str, Any]],
    *,
    stats: EpisodeBuildStats | None = None,
) -> list[dict[str, Any]]:
    """Rebuild closed entry lots with FIFO exits.

    Group by (account, symbol, position_side). A new PositionOpened must not
    cut an older unclosed lot. Exit quantity is allocated FIFO onto entry
    lots; fill realized_pnl is split across the lots a fill closes and is
    never taken from Nautilus Position.realized_pnl.
    """
    counters = stats if stats is not None else EpisodeBuildStats()
    counters.unmatched_exits = []
    counters.diagnostics = []
    fills = _dedup_fills(
        [
            record
            for record in records
            if record.get("event_type") in {"OrderFilled", "OrderPartiallyFilled"}
        ],
        stats=counters,
    )
    position_closed = [
        record
        for record in records
        if str(record.get("event_type") or "") == "PositionClosed"
    ]
    robot_trade_ids = {
        (_account_id(record), _episode_instrument_key(record) or "", _fill_trade_id(record))
        for record in fills
        if is_robot_client_order_id(_fill_client_order_id(record)) and _fill_trade_id(record)
    }
    books: dict[tuple[str, str, str], list[_OpenLot]] = {}
    closed_lots: list[_OpenLot] = []
    for record in fills:
        payload = record.get("payload") or {}
        if not isinstance(payload, Mapping):
            counters.dropped_fill_count += 1
            continue
        qty = _payload_decimal(payload, "last_qty", "last_fill_qty", "filled_qty", "quantity")
        if qty is None or qty == 0:
            counters.dropped_fill_count += 1
            continue
        qty = abs(qty)
        symbol = _episode_instrument_key(record)
        if symbol is None:
            counters.dropped_fill_count += 1
            continue
        account_id = _account_id(record)
        attribution = _fill_attribution(record, robot_trade_ids, symbol)
        side = _fill_position_side(record)
        if attribution == "robot":
            counters.robot_fill_count += 1
        elif side is None:
            counters.skipped_non_robot_fill_count += 1
            continue
        order_side = _normalize_order_side(payload.get("order_side") or payload.get("side"))
        is_exit, ambiguous = _fill_is_exit(
            record, side, books, account_id, symbol
        )
        if ambiguous:
            counters.diagnostics.append(
                f"ambiguous_hedge_or_close account={account_id} symbol={symbol} "
                f"ts={record.get('ts_event')}"
            )
            continue
        if side is None:
            side = _infer_book_side(is_exit, order_side, books, account_id, symbol)
        if side not in {"long", "short"}:
            counters.dropped_fill_count += 1
            counters.diagnostics.append(
                f"unclassified fill account={account_id} symbol={symbol} ts={record.get('ts_event')}"
            )
            continue
        book_key = (account_id, symbol, side)
        lots = books.setdefault(book_key, [])
        if is_exit:
            unmatched = _allocate_exit(lots, record, qty, closed_lots)
            if unmatched > 0:
                counters.unmatched_exit_count += 1
                counters.unmatched_exits.append(
                    {
                        "account_id": account_id,
                        "instrument_id": symbol,
                        "position_side": side,
                        "qty": str(unmatched),
                        "ts_event": str(record.get("ts_event")),
                        "client_order_id": _fill_client_order_id(record),
                        "trade_id": _fill_trade_id(record),
                    }
                )
                if not _payload_bool(payload.get("reduce_only")):
                    entry_side = "long" if order_side == "buy" else "short" if order_side == "sell" else None
                    if entry_side in {"long", "short"}:
                        entry_lots = books.setdefault((account_id, symbol, entry_side), [])
                        entry_lots.append(
                            _OpenLot.open_from_fill(
                                record,
                                account_id=account_id,
                                instrument_id=record.get("instrument_id") or symbol,
                                position_side=entry_side,
                                qty=unmatched,
                                attribution=attribution,
                            )
                        )
        else:
            lots.append(
                _OpenLot.open_from_fill(
                    record,
                    account_id=account_id,
                    instrument_id=record.get("instrument_id") or symbol,
                    position_side=side,
                    qty=qty,
                    attribution=attribution,
                )
            )
    for record in position_closed:
        account_id = _account_id(record)
        symbol = _episode_instrument_key(record)
        side = _fill_position_side(record)
        if symbol is None or side not in {"long", "short"}:
            counters.diagnostics.append(
                f"unmatched_position_closed account={account_id} "
                f"ts={record.get('ts_event')}"
            )
            continue
        remaining = sum(
            lot.remaining
            for lot in (books.get((account_id, symbol, side)) or [])
            if lot.remaining > 0
        )
        if remaining > 0:
            counters.unmatched_exit_count += 1
            counters.unmatched_exits.append(
                {
                    "account_id": account_id,
                    "instrument_id": symbol,
                    "position_side": side,
                    "qty": str(remaining),
                    "ts_event": str(record.get("ts_event")),
                    "client_order_id": _fill_client_order_id(record),
                    "trade_id": _fill_trade_id(record),
                    "reason": "position_closed_without_matching_fills",
                }
            )
            counters.diagnostics.append(
                f"position_closed_without_matching_fills account={account_id} "
                f"symbol={symbol} remaining={remaining}"
            )
    open_remaining = 0
    for lots in books.values():
        open_remaining += sum(1 for lot in lots if lot.remaining > 0)
    counters.open_lot_count = open_remaining
    episodes = []
    for lot in closed_lots:
        episode = lot.as_episode()
        if episode is not None:
            episodes.append(episode)
    return episodes


def _account_id(record: Mapping[str, Any]) -> str:
    return str(record.get("account_id") or "")


@dataclass
class _OpenLot:
    lot_key: str
    account_id: str
    instrument_id: str
    position_side: str
    intent_id: str | None
    attribution: str
    remaining: Decimal
    entry_qty: Decimal
    entry_notional: Decimal | None
    entry_price_known: bool
    first_fill_at: datetime
    events: list[dict[str, Any]]
    contributing_intent_ids: set[str]
    realized_pnl: Decimal | None
    allocation_methods: set[str]
    client_order_id: str
    order_plan: dict[str, Any]
    diagnostics: list[str]
    unresolved: bool

    @classmethod
    def open_from_fill(
        cls,
        record: Mapping[str, Any],
        *,
        account_id: str,
        instrument_id: str,
        position_side: str,
        qty: Decimal,
        attribution: str,
    ) -> "_OpenLot":
        payload = record.get("payload") or {}
        price = _payload_decimal(payload, "avg_px", "last_px", "price")
        if price is not None and price == 0:
            price = None
        trade_id = _fill_trade_id(record)
        client_order_id = _fill_client_order_id(record)
        identity = (
            f"{account_id}|{instrument_id}|{position_side}|"
            f"{trade_id or ''}|{client_order_id}|{record.get('ts_event')}|{qty}"
        )
        lot_key = str(uuid.uuid5(OUTCOME_NAMESPACE, identity))
        intent_id = record.get("intent_id")
        contributing: set[str] = set()
        if intent_id:
            contributing.add(str(intent_id))
        event = _as_fill_event(record)
        event["payload"] = {
            **event["payload"],
            "last_qty": str(qty),
            "last_fill_qty": str(qty),
            "quantity": str(qty),
            "filled_qty": str(qty),
        }
        order_plan = dict(record.get("order_plan") or {})
        if "side" not in order_plan:
            order_plan["side"] = position_side
        order_plan["position_side"] = position_side
        diagnostics = []
        if price is None:
            diagnostics.append("missing_entry_price")
        return cls(
            lot_key=lot_key,
            account_id=account_id,
            instrument_id=str(instrument_id),
            position_side=position_side,
            intent_id=str(intent_id) if intent_id else None,
            attribution=attribution,
            remaining=qty,
            entry_qty=qty,
            entry_notional=None if price is None else qty * price,
            entry_price_known=price is not None,
            first_fill_at=_event_time(record),
            events=[event],
            contributing_intent_ids=contributing,
            realized_pnl=Decimal("0"),
            allocation_methods=set(),
            client_order_id=client_order_id,
            order_plan=order_plan,
            diagnostics=diagnostics,
            unresolved=False,
        )

    def as_episode(self) -> dict[str, Any] | None:
        if self.remaining != 0 or self.entry_qty <= 0:
            return None
        method = ",".join(sorted(self.allocation_methods)) or "fifo_qty"
        pnl = None if self.unresolved else self.realized_pnl
        return {
            "intent_id": self.intent_id,
            "account_id": self.account_id,
            "instrument_id": self.instrument_id,
            "lot_key": self.lot_key,
            "position_side": self.position_side,
            "attribution": self.attribution,
            "allocation_method": method,
            "realized_pnl": pnl,
            "order_plan": dict(self.order_plan),
            "events": list(self.events),
            "contributing_intent_ids": sorted(self.contributing_intent_ids),
            "pnl_unresolved": self.unresolved,
            "diagnostics": list(self.diagnostics),
        }


def _as_fill_event(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_type": record.get("event_type") or "OrderFilled",
        "ts_event": record.get("ts_event"),
        "account_id": record.get("account_id"),
        "intent_id": record.get("intent_id"),
        "client_order_id": _fill_client_order_id(record),
        "trade_id": _fill_trade_id(record),
        "event_id": record.get("event_id"),
        "payload": dict(record.get("payload") or {}),
    }


def _fill_trade_id(record: Mapping[str, Any]) -> str:
    raw = record.get("trade_id")
    if raw not in (None, ""):
        return str(raw)
    payload = record.get("payload") or {}
    if isinstance(payload, Mapping):
        value = payload.get("trade_id") or payload.get("tradeId")
        if value not in (None, ""):
            return str(value)
    return ""


def _tag_value(payload: Mapping[str, Any], prefix: str) -> str | None:
    tags = payload.get("tags")
    if not isinstance(tags, list):
        return None
    needle = f"{prefix}="
    for tag in tags:
        text = str(tag)
        if text.startswith(needle):
            value = text[len(needle):].strip()
            if value:
                return value
    return None


def _fill_position_side(record: Mapping[str, Any]) -> str | None:
    payload = record.get("payload") or {}
    if not isinstance(payload, Mapping):
        return None
    for value in (payload.get("position_side"), payload.get("positionSide")):
        mapped = POSITION_SIDE.get(value)
        if mapped in {"long", "short"}:
            return mapped
        if isinstance(value, str) and value.strip().lower() in {"long", "short"}:
            return value.strip().lower()
    position_id = payload.get("position_id") or _tag_value(payload, "position_id")
    suffix = str(position_id or "").rsplit("-", 1)[-1].upper()
    if suffix in {"LONG", "BUY"}:
        return "long"
    if suffix in {"SHORT", "SELL"}:
        return "short"
    return None


def _payload_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y"}:
        return True
    if text in {"false", "f", "0", "no", "n"}:
        return False
    return None


def _hedge_or_opposite_plan(record: Mapping[str, Any], order_side: str | None) -> bool:
    payload = record.get("payload") or {}
    order_plan = record.get("order_plan") or {}
    mode = str(
        payload.get("position_mode")
        or order_plan.get("position_mode")
        or order_plan.get("mode")
        or ""
    ).upper()
    if mode == "HEDGE":
        return True
    plan_side = None
    for value in (order_plan.get("position_side"), order_plan.get("side")):
        mapped = POSITION_SIDE.get(value)
        if mapped in {"long", "short"}:
            plan_side = mapped
            break
        if isinstance(value, str) and value.strip().lower() in {"long", "short"}:
            plan_side = value.strip().lower()
            break
    if plan_side == "short" and order_side == "sell":
        return True
    if plan_side == "long" and order_side == "buy":
        return True
    return False


def _fill_is_exit(
    record: Mapping[str, Any],
    side: str | None,
    books: Mapping[tuple[str, str, str], list[_OpenLot]],
    account_id: str,
    symbol: str,
) -> tuple[bool, bool]:
    payload = record.get("payload") or {}
    order_side = _normalize_order_side(payload.get("order_side") or payload.get("side"))
    reduce_only = _payload_bool(payload.get("reduce_only"))
    if side == "long" and order_side == "sell":
        return True, False
    if side == "short" and order_side == "buy":
        return True, False
    if reduce_only:
        return True, False
    if side is None and order_side == "sell":
        long_lots = books.get((account_id, symbol, "long")) or []
        if any(lot.remaining > 0 for lot in long_lots):
            if _hedge_or_opposite_plan(record, order_side):
                return False, True
            return True, False
    if side is None and order_side == "buy":
        short_lots = books.get((account_id, symbol, "short")) or []
        if any(lot.remaining > 0 for lot in short_lots):
            if _hedge_or_opposite_plan(record, order_side):
                return False, True
            return True, False
    return False, False


def _infer_book_side(
    is_exit: bool,
    order_side: str | None,
    books: Mapping[tuple[str, str, str], list[_OpenLot]],
    account_id: str,
    symbol: str,
) -> str | None:
    if is_exit:
        if order_side == "sell":
            return "long"
        if order_side == "buy":
            return "short"
        return None
    if order_side == "buy":
        return "long"
    if order_side == "sell":
        return "short"
    return None


def _fill_attribution(
    record: Mapping[str, Any],
    robot_trade_ids: set[tuple[str, str, str]],
    symbol: str,
) -> str:
    client_order_id = _fill_client_order_id(record)
    if is_robot_client_order_id(client_order_id):
        return "robot"
    trade_id = _fill_trade_id(record)
    if trade_id and (_account_id(record), symbol, trade_id) in robot_trade_ids:
        return "robot"
    if client_order_id:
        return "manual"
    return "unknown"


def _fill_richness(record: Mapping[str, Any]) -> int:
    payload = record.get("payload") or {}
    score = 0
    if _payload_decimal(payload, "realized_pnl") is not None:
        score += 2
    if _payload_decimal(payload, "commission", "fee") is not None:
        score += 1
    return score


def _dedup_fills(
    records: Sequence[Mapping[str, Any]],
    *,
    stats: EpisodeBuildStats,
) -> list[Mapping[str, Any]]:
    chosen: dict[tuple[str, ...], Mapping[str, Any]] = {}
    for record in records:
        account_id = _account_id(record)
        symbol = _episode_instrument_key(record) or ""
        trade_id = _fill_trade_id(record)
        event_id = str(record.get("event_id") or "")
        if trade_id:
            key = ("trade", account_id, symbol, trade_id)
        elif event_id:
            key = ("event", account_id, event_id)
        else:
            key = ("row", account_id, symbol, str(record.get("ts_event")), str(id(record)))
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = record
            continue
        stats.deduped_fill_count += 1
        if _fill_richness(record) > _fill_richness(existing):
            chosen[key] = record
    ordered = list(chosen.values())
    ordered.sort(key=lambda row: (_event_time(row), str(row.get("event_id") or "")))
    return ordered


def _allocate_exit(
    lots: list[_OpenLot],
    record: Mapping[str, Any],
    qty: Decimal,
    closed_lots: list[_OpenLot],
) -> Decimal:
    payload = record.get("payload") or {}
    remaining_exit = qty
    takes: list[tuple[_OpenLot, Decimal]] = []
    for lot in lots:
        if remaining_exit <= 0:
            break
        if lot.remaining <= 0:
            continue
        take = min(lot.remaining, remaining_exit)
        if take <= 0:
            continue
        takes.append((lot, take))
        remaining_exit -= take
    if not takes:
        return remaining_exit
    allocated = sum((take for _lot, take in takes), Decimal("0"))
    fill_pnl = _payload_decimal(payload, "realized_pnl")
    fill_fee = _payload_decimal(payload, "commission", "fee")
    exit_px = _payload_decimal(payload, "avg_px", "last_px", "price")
    if exit_px is not None and exit_px == 0:
        exit_px = None
    price_pnls: list[Decimal | None] = []
    prices_complete = True
    for lot, take in takes:
        if not lot.entry_price_known or lot.entry_notional is None or exit_px is None:
            price_pnls.append(None)
            prices_complete = False
            continue
        entry_avg = lot.entry_notional / lot.entry_qty if lot.entry_qty else None
        if entry_avg is None:
            price_pnls.append(None)
            prices_complete = False
            continue
        if lot.position_side == "long":
            price_pnls.append((exit_px - entry_avg) * take)
        else:
            price_pnls.append((entry_avg - exit_px) * take)
    if not prices_complete:
        pnls = [None] * len(takes)
        method = "fifo_missing_prices"
    elif fill_pnl is not None and allocated > 0:
        known = [item for item in price_pnls if item is not None]
        price_sum = sum(known, Decimal("0"))
        target = fill_pnl * (allocated / qty) if qty else fill_pnl
        residual = target - price_sum
        pnls = [
            (item or Decimal("0")) + residual * (take / allocated)
            for item, (_lot, take) in zip(price_pnls, takes)
        ]
        method = "fifo_price_plus_venue_residual_by_qty"
    else:
        pnls = [item if item is not None else Decimal("0") for item in price_pnls]
        method = "fifo_qty_mark_to_exit_price"
    intent_id = record.get("intent_id")
    original_payload = dict(payload)
    for (lot, take), pnl in zip(takes, pnls):
        lot.remaining -= take
        if pnl is None:
            lot.unresolved = True
            lot.diagnostics.append("missing_prices_or_unmatched_exit")
            lot.realized_pnl = None
        elif lot.realized_pnl is not None and not lot.unresolved:
            lot.realized_pnl += pnl
        lot.allocation_methods.add(method)
        event = _as_fill_event(record)
        split_fee = (fill_fee * (take / qty)) if fill_fee is not None and qty else None
        scaled_payload = dict(event["payload"])
        scaled_payload["last_qty"] = str(take)
        scaled_payload["last_fill_qty"] = str(take)
        scaled_payload["filled_qty"] = str(take)
        scaled_payload["quantity"] = str(take)
        if pnl is not None:
            scaled_payload["realized_pnl"] = str(pnl)
        elif "realized_pnl" in scaled_payload:
            scaled_payload.pop("realized_pnl", None)
        if split_fee is not None:
            scaled_payload["commission"] = str(split_fee)
        event["payload"] = scaled_payload
        event["details"] = {
            "original_payload": original_payload,
            "allocated_qty": str(take),
            "source_qty": str(qty),
        }
        lot.events.append(event)
        if intent_id:
            lot.contributing_intent_ids.add(str(intent_id))
        if lot.remaining == 0:
            closed_lots.append(lot)
    return remaining_exit


def compute_outcomes(
    intents: Sequence[Mapping[str, Any]],
    *,
    cache_dir: str | Path,
    base_url: str = DEFAULT_BASE_URL,
    market: str = "um",
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for intent in intents:
        events = list(intent.get("events") or [])
        closure = _closure_info(intent, events)
        first_fill_at = closure["first_fill_at"] if closure else None
        closed_at = closure["closed_at"] if closure else None
        klines: list[Kline] | None = None
        kline_source: str | None = None
        skip_reason: str | None = None
        if first_fill_at and closed_at:
            try:
                symbol = normalize_symbol(str(intent.get("instrument_id") or ""))
                kline_source = f"binance_vision:{market}:1m:{symbol}"
                klines = load_klines(
                    symbol,
                    first_fill_at,
                    closed_at + timedelta(minutes=1),
                    cache_dir,
                    base_url=base_url,
                    market=market,
                )
            except (MissingKlines, UnsupportedSymbol) as exc:
                skip_reason = str(exc)
            except OSError as exc:
                # kline source unreachable (e.g. geo-blocked CDN): keep the
                # PnL/R metrics and record why MAE/MFE is missing.
                skip_reason = f"kline fetch failed: {exc}"
                klines = None
                kline_source = f"binance_vision:{market}:1m"
        outcome = build_trade_outcome(
            intent,
            events,
            klines=klines,
            kline_source=kline_source,
            kline_skip_reason=skip_reason,
        )
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def upsert_trade_outcomes(conn: Any, outcomes: Sequence[Mapping[str, Any]]) -> int:
    if not outcomes:
        return 0
    sql = """
        INSERT INTO trade_outcomes (
            outcome_id, intent_id, account_id, lot_key, position_side, attribution,
            instrument_id, side,
            entry_avg_price, exit_avg_price, filled_quantity, realized_pnl, fees,
            initial_risk, r_multiple, mae, mfe, mae_price, mfe_price,
            holding_seconds, first_fill_at, closed_at, kline_source, details
        ) VALUES (
            %(outcome_id)s, %(intent_id)s, %(account_id)s, %(lot_key)s, %(position_side)s, %(attribution)s,
            %(instrument_id)s, %(side)s,
            %(entry_avg_price)s, %(exit_avg_price)s, %(filled_quantity)s, %(realized_pnl)s, %(fees)s,
            %(initial_risk)s, %(r_multiple)s, %(mae)s, %(mfe)s, %(mae_price)s, %(mfe_price)s,
            %(holding_seconds)s, %(first_fill_at)s, %(closed_at)s, %(kline_source)s, %(details)s
        )
        ON CONFLICT (account_id, lot_key) DO UPDATE SET
            intent_id = EXCLUDED.intent_id,
            position_side = EXCLUDED.position_side,
            attribution = EXCLUDED.attribution,
            instrument_id = EXCLUDED.instrument_id,
            side = EXCLUDED.side,
            entry_avg_price = EXCLUDED.entry_avg_price,
            exit_avg_price = EXCLUDED.exit_avg_price,
            filled_quantity = EXCLUDED.filled_quantity,
            realized_pnl = EXCLUDED.realized_pnl,
            fees = EXCLUDED.fees,
            initial_risk = EXCLUDED.initial_risk,
            r_multiple = EXCLUDED.r_multiple,
            mae = EXCLUDED.mae,
            mfe = EXCLUDED.mfe,
            mae_price = EXCLUDED.mae_price,
            mfe_price = EXCLUDED.mfe_price,
            holding_seconds = EXCLUDED.holding_seconds,
            first_fill_at = EXCLUDED.first_fill_at,
            closed_at = EXCLUDED.closed_at,
            kline_source = EXCLUDED.kline_source,
            details = EXCLUDED.details
    """
    with conn.cursor() as cur:
        for outcome in outcomes:
            row = dict(outcome)
            row["details"] = Json(row.get("details") or {})
            row.setdefault(
                "lot_key",
                str(row.get("outcome_id") or f"{row.get('intent_id')}:{row.get('account_id')}"),
            )
            row.setdefault("attribution", "unknown")
            row.setdefault("position_side", row.get("side"))
            cur.execute(sql, row)
    return len(outcomes)


def delete_stale_trade_outcomes(
    conn: Any,
    keep_outcome_ids: Sequence[str],
    *,
    intent_id: str | None = None,
) -> int:
    keep = [str(outcome_id) for outcome_id in keep_outcome_ids]
    with conn.cursor() as cur:
        if intent_id:
            if keep:
                cur.execute(
                    """
                    DELETE FROM trade_outcomes
                    WHERE intent_id = %s
                      AND NOT (outcome_id::text = ANY(%s))
                    """,
                    (intent_id, keep),
                )
            else:
                cur.execute(
                    "DELETE FROM trade_outcomes WHERE intent_id = %s",
                    (intent_id,),
                )
        elif not keep:
            cur.execute("DELETE FROM trade_outcomes")
        else:
            cur.execute(
                """
                DELETE FROM trade_outcomes
                WHERE NOT (outcome_id::text = ANY(%s))
                """,
                (keep,),
            )
        return int(cur.rowcount or 0)


def mark_job_running(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trade_outcome_job_runs (
                job_name, status, started_at, completed_at, outcome_count, error
            ) VALUES (%s, 'running', now(), NULL, 0, NULL)
            ON CONFLICT (job_name) DO UPDATE SET
                status = EXCLUDED.status,
                started_at = EXCLUDED.started_at,
                completed_at = NULL,
                outcome_count = 0,
                error = NULL
            """,
            (JOB_NAME,),
        )


def mark_job_succeeded(conn: Any, outcome_count: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trade_outcome_job_runs
            SET status = 'succeeded',
                completed_at = now(),
                outcome_count = %s,
                error = NULL
            WHERE job_name = %s
            """,
            (outcome_count, JOB_NAME),
        )


def mark_job_failed(conn: Any, exc: Exception) -> None:
    error = f"{type(exc).__name__}: {exc}"
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trade_outcome_job_runs
            SET status = 'failed',
                completed_at = now(),
                outcome_count = 0,
                error = %s
            WHERE job_name = %s
            """,
            (error, JOB_NAME),
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute closed trade outcome metrics from execution events.")
    parser.add_argument(
        "--db-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres URL; defaults to DATABASE_URL",
    )
    parser.add_argument("--cache-dir", required=True, help="Directory for Binance Vision daily 1m ZIP cache")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Binance Vision base URL")
    parser.add_argument("--market", default="um", choices=("um", "cm", "spot"), help="Binance market archive")
    parser.add_argument("--output", default="trade_outcomes_result.json", help="JSON result output path")
    parser.add_argument("--intent-id", help="Optional intent_id to recompute")
    args = parser.parse_args(argv)
    if not args.db_url:
        parser.error("--db-url or DATABASE_URL is required")

    conn = psycopg2.connect(args.db_url)
    try:
        mark_job_running(conn)
        conn.commit()

        try:
            stats = EpisodeBuildStats()
            intents = load_closed_intents(
                conn,
                intent_id=args.intent_id,
                stats=stats,
            )
            outcomes = compute_outcomes(intents, cache_dir=args.cache_dir, base_url=args.base_url, market=args.market)
            upserted_count = upsert_trade_outcomes(conn, outcomes)
            deleted_stale_count = delete_stale_trade_outcomes(
                conn,
                [str(outcome.get("outcome_id") or "") for outcome in outcomes],
                intent_id=args.intent_id,
            )

            result = {
                "closed_intent_count": len(intents),
                "upserted_count": upserted_count,
                "skipped_count": len(intents) - upserted_count,
                "deleted_stale_count": deleted_stale_count,
                "robot_fill_count": stats.robot_fill_count,
                "dropped_fill_count": stats.dropped_fill_count,
                "skipped_non_robot_fill_count": stats.skipped_non_robot_fill_count,
                "unmatched_exit_count": stats.unmatched_exit_count,
                "open_lot_count": stats.open_lot_count,
                "deduped_fill_count": stats.deduped_fill_count,
                "unmatched_exits": stats.unmatched_exits or [],
                "diagnostics": stats.diagnostics or [],
            }
            Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
            mark_job_succeeded(conn, upserted_count)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            try:
                mark_job_failed(conn, exc)
                conn.commit()
            except Exception:
                conn.rollback()
            raise
    finally:
        conn.close()

    print(
        f"processed {len(intents)} closed intents; upserted {upserted_count}; "
        f"deleted {deleted_stale_count} stale; dropped_fills {stats.dropped_fill_count}; "
        f"wrote JSON result to {args.output}"
    )
    return 0


FULL_EXIT_TOLERANCE = Decimal("0.999999")


def _closure_info(intent: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Determine whether an intent's position is closed and how.

    Live Nautilus Position* events carry no intent_id, so intent-linked
    PositionClosed rows only exist for fixture/legacy data. The authoritative
    live signal is the intent's own fills: exit quantity covering entry
    quantity means the intent is fully exited.
    """
    fills = [
        event
        for event in events
        if str(event.get("event_type") or "") in {"OrderFilled", "OrderPartiallyFilled"}
    ]
    if not fills:
        return None
    side = _infer_side(intent, events)
    if side is None:
        return None
    entry_fills, exit_fills = _split_entry_exit_fills(side, fills)
    if not entry_fills:
        return None
    entry_avg, entry_qty = _weighted_average(entry_fills)
    exit_avg, exit_qty = _weighted_average(exit_fills)
    closed_events = [event for event in events if str(event.get("event_type") or "") == "PositionClosed"]
    qty_closed = bool(
        entry_qty and exit_qty and exit_qty >= entry_qty * FULL_EXIT_TOLERANCE
    )
    if qty_closed:
        close_basis = "fills_fully_exited"
        closed_at = max(_event_time(event) for event in exit_fills)
        if closed_events:
            close_basis = "fills_fully_exited_and_position_closed"
            closed_at = max(closed_at, max(_event_time(event) for event in closed_events))
    else:
        return None
    return {
        "side": side,
        "fills": fills,
        "entry_avg": entry_avg,
        "entry_qty": entry_qty,
        "exit_avg": exit_avg,
        "exit_qty": exit_qty,
        "closed_events": closed_events,
        "close_basis": close_basis,
        "first_fill_at": min(_event_time(event) for event in fills),
        "closed_at": closed_at,
    }


def _closure_from_closed_lot(
    intent: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Closed FIFO lots are confirmed by remaining qty, not by priced fills."""
    if not intent.get("lot_key"):
        return None
    fills = [
        event
        for event in events
        if str(event.get("event_type") or "") in {"OrderFilled", "OrderPartiallyFilled"}
    ]
    if not fills:
        return None
    side = intent.get("position_side") or _infer_side(intent, events)
    if side not in {"long", "short"}:
        return None
    entry_fills, exit_fills = _split_entry_exit_fills(side, fills)
    entry_avg, entry_qty = _weighted_average(entry_fills or fills)
    exit_avg, exit_qty = _weighted_average(exit_fills)
    first_fill_at = min(_event_time(event) for event in fills)
    closed_at = max(_event_time(event) for event in fills)
    return {
        "side": side,
        "fills": fills,
        "entry_avg": entry_avg,
        "entry_qty": entry_qty,
        "exit_avg": exit_avg,
        "exit_qty": exit_qty or entry_qty,
        "closed_events": [],
        "close_basis": "fifo_lot_qty_closed",
        "first_fill_at": first_fill_at,
        "closed_at": closed_at,
    }


def _infer_side(intent: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> str | None:
    if intent.get("position_side") in {"long", "short"}:
        return str(intent.get("position_side"))
    order_plan = intent.get("order_plan") or {}
    for value in (order_plan.get("side"), order_plan.get("position_side")):
        normalized = POSITION_SIDE.get(value, str(value).lower() if value is not None else None)
        if normalized in {"long", "short"}:
            return normalized
    for event in events:
        if str(event.get("event_type") or "").startswith("Position"):
            payload = event.get("payload") or {}
            normalized = POSITION_SIDE.get(payload.get("side"))
            if normalized:
                return normalized
    return None


def _split_entry_exit_fills(
    side: str,
    fills: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    entry_order_side = "buy" if side == "long" else "sell"
    exit_order_side = "sell" if side == "long" else "buy"
    entries: list[Mapping[str, Any]] = []
    exits: list[Mapping[str, Any]] = []
    for fill in fills:
        payload = fill.get("payload") or {}
        order_side = _normalize_order_side(payload.get("order_side") or payload.get("side"))
        if order_side == entry_order_side:
            entries.append(fill)
        elif order_side == exit_order_side:
            exits.append(fill)
    return entries, exits


def _weighted_average(fills: Sequence[Mapping[str, Any]]) -> tuple[Decimal | None, Decimal | None]:
    total_qty = Decimal("0")
    notional = Decimal("0")
    priced_qty = Decimal("0")
    for fill in fills:
        payload = fill.get("payload") or {}
        qty = _payload_decimal(payload, "last_qty", "last_fill_qty", "filled_qty", "quantity")
        if qty is None:
            continue
        qty = abs(qty)
        total_qty += qty
        price = _payload_decimal(payload, "avg_px", "last_px", "price")
        if price is None or price == 0:
            continue
        priced_qty += qty
        notional += qty * price
    if total_qty == 0:
        return None, None
    if priced_qty == 0:
        return None, total_qty
    return notional / priced_qty, total_qty


def _fill_realized_pnl_sum(fills: Sequence[Mapping[str, Any]]) -> Decimal | None:
    total = Decimal("0")
    found = False
    for event in fills:
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        value = _payload_decimal(payload, "realized_pnl")
        if value is None:
            continue
        total += value
        found = True
    return total if found else None


def _position_closed_realized_pnl(closed_events: Sequence[Mapping[str, Any]]) -> Decimal | None:
    # Kept for fixtures; live Nautilus Position.realized_pnl is not Binance PnL.
    return None


def _fallback_realized_pnl(
    side: str,
    entry_avg: Decimal,
    exit_avg: Decimal,
    quantity: Decimal | None,
) -> Decimal | None:
    if quantity is None:
        return None
    if side == "long":
        return (exit_avg - entry_avg) * quantity
    return (entry_avg - exit_avg) * quantity


def _sum_fees(fills: Sequence[Mapping[str, Any]]) -> tuple[Decimal | None, str | None]:
    total = Decimal("0")
    found = False
    for fill in fills:
        payload = fill.get("payload") or {}
        fee = _payload_decimal(payload, "commission", "fee")
        if fee is None:
            continue
        total += fee
        found = True
    if not found:
        return None, "no commission or fee fields in fill payloads"
    return total, None


def _risk_metrics(
    order_plan: Mapping[str, Any],
    entry_avg: Decimal | None,
    quantity: Decimal | None,
    realized_pnl: Decimal | None,
) -> tuple[Decimal | None, Decimal | None, dict[str, str] | None]:
    stop_loss = _mapping_decimal(order_plan, "stop_loss")
    if stop_loss is None:
        return None, None, {"reason": "missing stop_loss"}
    if entry_avg is None or quantity is None:
        return None, None, {"reason": "missing entry price or quantity"}
    initial_risk = abs(entry_avg - stop_loss) * quantity
    if initial_risk == 0 or realized_pnl is None:
        return initial_risk, None, {"reason": "missing realized_pnl or zero initial_risk"}
    return initial_risk, realized_pnl / initial_risk, None


def _mae_mfe(
    side: str,
    entry_avg: Decimal | None,
    klines: Sequence[Kline],
    first_fill_at: datetime,
    closed_at: datetime,
) -> dict[str, Decimal | float | None]:
    if entry_avg is None or entry_avg == 0:
        return {"mae": None, "mfe": None, "mae_price": None, "mfe_price": None}
    holding_klines = [
        row
        for row in klines
        if first_fill_at <= _coerce_datetime(row[0]) <= closed_at
    ]
    if not holding_klines:
        return {"mae": None, "mfe": None, "mae_price": None, "mfe_price": None}
    if side == "long":
        mae_price = min(_decimal_from_value(row[3]) for row in holding_klines)
        mfe_price = max(_decimal_from_value(row[2]) for row in holding_klines)
        mae = (entry_avg - mae_price) / entry_avg
        mfe = (mfe_price - entry_avg) / entry_avg
    else:
        mae_price = max(_decimal_from_value(row[2]) for row in holding_klines)
        mfe_price = min(_decimal_from_value(row[3]) for row in holding_klines)
        mae = (mae_price - entry_avg) / entry_avg
        mfe = (entry_avg - mfe_price) / entry_avg
    return {"mae": float(mae), "mfe": float(mfe), "mae_price": mae_price, "mfe_price": mfe_price}


def _event_time(event: Mapping[str, Any]) -> datetime:
    return _coerce_datetime(event.get("ts_event"))


def _payload_decimal(payload: Mapping[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return _decimal_from_value(value)
    return None


def _mapping_decimal(payload: Mapping[str, Any], key: str) -> Decimal | None:
    value = payload.get(key)
    if value is None:
        return None
    return _decimal_from_value(value)


def _decimal_from_value(value: Any) -> Decimal:
    try:
        return Decimal(str(value).split()[0])
    except (InvalidOperation, IndexError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
