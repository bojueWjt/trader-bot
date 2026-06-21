from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from psycopg2.extras import Json

from db.connection import transaction

from .db_helpers import decimal_or_none, ensure_aware, ensure_execution_event
from .identifiers import canonical_account_id


@dataclass(frozen=True)
class AccountApplyResult:
    event_id: str
    account_id: str


class AccountProjectionReducer:
    def apply_event(self, conn, event: dict[str, Any]) -> AccountApplyResult:
        normalized = {
            **event,
            "account_id": canonical_account_id(event["account_id"]),
            "payload": dict(event.get("payload") or {}),
            "ts_event": ensure_aware(event.get("ts_event")),
        }
        with transaction(conn):
            ensure_execution_event(conn, normalized)
            payload = normalized["payload"]
            equity = _first_decimal(payload, "equity", "balance", "total") or Decimal("0")
            free = _first_decimal(payload, "free", "available_balance")
            margin = _first_decimal(payload, "margin", "margin_balance", "locked") or Decimal("0")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO accounts_projection (
                        account_id, currency, equity, margin, available_balance,
                        last_execution_event_at, updated_from_event_id, updated_at, payload
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,now(),%s)
                    ON CONFLICT (account_id) DO UPDATE SET
                        currency=EXCLUDED.currency,
                        equity=EXCLUDED.equity,
                        margin=EXCLUDED.margin,
                        available_balance=EXCLUDED.available_balance,
                        last_execution_event_at=EXCLUDED.last_execution_event_at,
                        updated_from_event_id=EXCLUDED.updated_from_event_id,
                        updated_at=now(),
                        payload=accounts_projection.payload || EXCLUDED.payload
                    """,
                    (
                        normalized["account_id"],
                        payload.get("currency", "USDT"),
                        equity,
                        margin,
                        free,
                        normalized["ts_event"],
                        normalized["event_id"],
                        Json(payload),
                    ),
                )
        return AccountApplyResult(
            event_id=normalized["event_id"],
            account_id=normalized["account_id"],
        )


def get_account_balance(conn, account_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_id, currency, equity, available_balance, margin, payload
            FROM accounts_projection
            WHERE account_id=%s
            """,
            (canonical_account_id(account_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    payload = row[5] or {}
    return {
        "account_id": row[0],
        "currency": row[1],
        "equity": row[2],
        "free": row[3],
        "margin": row[4],
        "balance": decimal_or_none(payload.get("balance")) or row[2],
    }


def _first_decimal(payload: dict[str, Any], *names: str) -> Decimal | None:
    for name in names:
        value = decimal_or_none(payload.get(name))
        if value is not None:
            return value
    return None
