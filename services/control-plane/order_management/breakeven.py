from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Protocol

from psycopg2.extras import Json

from db.connection import transaction


class StopMover(Protocol):
    def move_stop(self, payload: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class BreakevenConfig:
    trigger_r: Decimal
    offset_bps: Decimal = Decimal("0")
    fee_buffer_bps: Decimal = Decimal("0")


@dataclass(frozen=True)
class BreakevenResult:
    action: str
    reason: str
    trigger_price: Decimal | None = None


class BreakevenManager:
    def __init__(self, *, submitter: StopMover) -> None:
        self._submitter = submitter

    def evaluate_and_apply(
        self,
        conn,
        *,
        account_id: str,
        position_key: str,
        market_price: Decimal,
        price_stale: bool,
        config: BreakevenConfig,
        now: datetime,
    ) -> BreakevenResult:
        if price_stale:
            return BreakevenResult("blocked", "market_data_stale")
        with transaction(conn):
            position = _load_position(conn, account_id, position_key)
            stop = _load_active_stop(conn, account_id, position_key)
            if position is None or stop is None:
                return BreakevenResult("blocked", "position_or_stop_missing")
            payload = dict(stop["payload"] or {})
            if payload.get("breakeven_applied") is True:
                return BreakevenResult("already_applied", "breakeven_already_applied", stop["trigger_price"])

            entry = position["entry_price"]
            current_stop = stop["trigger_price"]
            risk = abs(entry - current_stop)
            if risk <= 0:
                return BreakevenResult("blocked", "invalid_initial_risk")
            if not _triggered(position["side"], entry, risk, Decimal(str(market_price)), config.trigger_r):
                return BreakevenResult("wait", "trigger_not_reached")

            target = _breakeven_price(position["side"], entry, config)
            move_payload = {
                "account_id": account_id,
                "position_key": position_key,
                "client_order_id": stop["client_order_id"],
                "trigger_price": target,
                "reason": "breakeven",
                "applied_at": now.isoformat(),
            }
            self._submitter.move_stop(move_payload)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE protective_orders_projection
                    SET trigger_price=%s,
                        payload=payload || %s::jsonb,
                        updated_at=now()
                    WHERE protective_order_projection_id=%s
                    """,
                    (
                        target,
                        Json({"breakeven_applied": True, "breakeven_applied_at": now.isoformat()}),
                        stop["protective_order_projection_id"],
                    ),
                )
            return BreakevenResult("applied", "breakeven_applied", target)


def _load_position(conn, account_id: str, position_key: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT side, avg_entry_price
            FROM positions_projection
            WHERE account_id=%s AND position_id=%s
            FOR UPDATE
            """,
            (account_id, position_key),
        )
        row = cur.fetchone()
    if row is None or row[1] is None:
        return None
    return {"side": row[0], "entry_price": Decimal(str(row[1]))}


def _load_active_stop(conn, account_id: str, position_key: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT protective_order_projection_id::text, client_order_id, trigger_price, payload
            FROM protective_orders_projection
            WHERE account_id=%s AND position_key=%s AND lifecycle_role='stop_loss' AND active
            FOR UPDATE
            """,
            (account_id, position_key),
        )
        row = cur.fetchone()
    if row is None or row[2] is None:
        return None
    return {
        "protective_order_projection_id": row[0],
        "client_order_id": row[1],
        "trigger_price": Decimal(str(row[2])),
        "payload": row[3],
    }


def _triggered(side: str, entry: Decimal, risk: Decimal, market: Decimal, trigger_r: Decimal) -> bool:
    if str(side).lower() == "long":
        return market >= entry + risk * Decimal(str(trigger_r))
    return market <= entry - risk * Decimal(str(trigger_r))


def _breakeven_price(side: str, entry: Decimal, config: BreakevenConfig) -> Decimal:
    bps = (Decimal(str(config.offset_bps)) + Decimal(str(config.fee_buffer_bps))) / Decimal("10000")
    raw = entry * (Decimal("1") + bps if str(side).lower() == "long" else Decimal("1") - bps)
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

