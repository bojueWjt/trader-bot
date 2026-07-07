from __future__ import annotations

import argparse
import json
import sys
import uuid
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
ORDER_SIDE = {1: "buy", 2: "sell", "BUY": "buy", "SELL": "sell", "buy": "buy", "sell": "sell"}
POSITION_SIDE = {2: "long", 3: "short", "LONG": "long", "SHORT": "short", "long": "long", "short": "short"}


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
    realized_pnl = _position_closed_realized_pnl(closed_events)
    if realized_pnl is None and entry_avg is not None and exit_avg is not None:
        realized_pnl = _fallback_realized_pnl(side, entry_avg, exit_avg, exit_qty or filled_quantity)

    fees, fee_reason = _sum_fees(fills)
    initial_risk, r_multiple, risk_detail = _risk_metrics(intent.get("order_plan") or {}, entry_avg, filled_quantity, realized_pnl)
    mae_mfe = _mae_mfe(side, entry_avg, klines or [], first_fill_at, closed_at)

    details: dict[str, Any] = {"close_basis": closure["close_basis"]}
    if fee_reason:
        details["fees"] = {"reason": fee_reason}
    if risk_detail:
        details["r_multiple"] = risk_detail
    if kline_skip_reason is not None:
        details["mae_mfe"] = {"status": "skipped", "reason": kline_skip_reason}
    elif mae_mfe["mae"] is None:
        details["mae_mfe"] = {"status": "skipped", "reason": "no klines in holding window"}

    return {
        "outcome_id": str(uuid.uuid5(OUTCOME_NAMESPACE, f"{intent.get('intent_id')}:{intent.get('account_id')}")),
        "intent_id": intent.get("intent_id"),
        "account_id": intent.get("account_id"),
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


def load_closed_intents(conn: Any, *, intent_id: str | None = None) -> list[dict[str, Any]]:
    params: list[Any] = []
    intent_filter = ""
    if intent_id:
        intent_filter = "AND ti.intent_id = %s"
        params.append(intent_id)
    sql = f"""
        SELECT
            ti.intent_id::text,
            ti.account_id,
            ti.instrument_id,
            ti.order_plan,
            jsonb_agg(
                jsonb_build_object(
                    'event_type', ee.event_type,
                    'ts_event', ee.ts_event,
                    'account_id', ee.account_id,
                    'payload', ee.payload
                )
                ORDER BY ee.ts_event, ee.event_id
            ) AS events
        FROM trade_intents ti
        JOIN execution_events ee ON ee.intent_id = ti.intent_id
        WHERE EXISTS (
            SELECT 1 FROM execution_events filled
            WHERE filled.intent_id = ti.intent_id
              AND filled.account_id = ti.account_id
              AND filled.event_type = 'OrderFilled'
        )
          {intent_filter}
        GROUP BY ti.intent_id, ti.account_id, ti.instrument_id, ti.order_plan
        ORDER BY ti.account_id, ti.intent_id
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {
            "intent_id": row[0],
            "account_id": row[1],
            "instrument_id": row[2],
            "order_plan": row[3] or {},
            "events": row[4] or [],
        }
        for row in rows
    ]


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
            outcome_id, intent_id, account_id, instrument_id, side,
            entry_avg_price, exit_avg_price, filled_quantity, realized_pnl, fees,
            initial_risk, r_multiple, mae, mfe, mae_price, mfe_price,
            holding_seconds, first_fill_at, closed_at, kline_source, details
        ) VALUES (
            %(outcome_id)s, %(intent_id)s, %(account_id)s, %(instrument_id)s, %(side)s,
            %(entry_avg_price)s, %(exit_avg_price)s, %(filled_quantity)s, %(realized_pnl)s, %(fees)s,
            %(initial_risk)s, %(r_multiple)s, %(mae)s, %(mfe)s, %(mae_price)s, %(mfe_price)s,
            %(holding_seconds)s, %(first_fill_at)s, %(closed_at)s, %(kline_source)s, %(details)s
        )
        ON CONFLICT (intent_id, account_id) DO UPDATE SET
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
            cur.execute(sql, row)
    return len(outcomes)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute closed trade outcome metrics from execution events.")
    parser.add_argument("--db-url", required=True, help="Postgres URL for the control-plane database")
    parser.add_argument("--cache-dir", required=True, help="Directory for Binance Vision daily 1m ZIP cache")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Binance Vision base URL")
    parser.add_argument("--market", default="um", choices=("um", "cm", "spot"), help="Binance market archive")
    parser.add_argument("--output", default="trade_outcomes_result.json", help="JSON result output path")
    parser.add_argument("--intent-id", help="Optional intent_id to recompute")
    args = parser.parse_args(argv)

    with psycopg2.connect(args.db_url) as conn:
        intents = load_closed_intents(conn, intent_id=args.intent_id)
        outcomes = compute_outcomes(intents, cache_dir=args.cache_dir, base_url=args.base_url, market=args.market)
        upserted_count = upsert_trade_outcomes(conn, outcomes)

    result = {
        "closed_intent_count": len(intents),
        "upserted_count": upserted_count,
        "skipped_count": len(intents) - upserted_count,
    }
    Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(f"processed {len(intents)} closed intents; upserted {upserted_count}; wrote JSON result to {args.output}")
    return 0


FULL_EXIT_TOLERANCE = Decimal("0.999999")


def _closure_info(intent: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Determine whether an intent's position is closed and how.

    Live Nautilus Position* events carry no intent_id, so intent-linked
    PositionClosed rows only exist for fixture/legacy data. The authoritative
    live signal is the intent's own fills: exit quantity covering entry
    quantity means the intent is fully exited.
    """
    fills = [event for event in events if str(event.get("event_type") or "") == "OrderFilled"]
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
    if closed_events:
        close_basis = "position_closed_event"
        closed_at = max(_event_time(event) for event in closed_events)
    elif entry_qty and exit_qty and exit_qty >= entry_qty * FULL_EXIT_TOLERANCE:
        close_basis = "fills_fully_exited"
        closed_at = max(_event_time(event) for event in exit_fills)
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


def _infer_side(intent: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> str | None:
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
        order_side = ORDER_SIDE.get(payload.get("order_side"))
        if order_side == entry_order_side:
            entries.append(fill)
        elif order_side == exit_order_side:
            exits.append(fill)
    return entries, exits


def _weighted_average(fills: Sequence[Mapping[str, Any]]) -> tuple[Decimal | None, Decimal | None]:
    total_qty = Decimal("0")
    notional = Decimal("0")
    for fill in fills:
        payload = fill.get("payload") or {}
        qty = _payload_decimal(payload, "last_qty", "last_fill_qty", "filled_qty", "quantity")
        price = _payload_decimal(payload, "avg_px", "last_px", "price")
        if qty is None or price is None:
            continue
        qty = abs(qty)
        total_qty += qty
        notional += qty * price
    if total_qty == 0:
        return None, None
    return notional / total_qty, total_qty


def _position_closed_realized_pnl(closed_events: Sequence[Mapping[str, Any]]) -> Decimal | None:
    for event in reversed(closed_events):
        payload = event.get("payload") or {}
        value = _payload_decimal(payload, "realized_pnl", "realized_pnl_total", "pnl")
        if value is not None:
            return value
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
