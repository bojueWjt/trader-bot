"""Persistent semantic position revisions, independent of position quantity.

The caller owns the transaction and must hold _account_risk_increase_lock for
capture/bind/invalidate operations. A close advances a book even if its eventual
replacement has exactly the same quantity. CLOSE_ALL advances the account
wildcard, including books which have never existed. No revision is reset at flat.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


class StalePositionRevision(ValueError):
    """The model's original position snapshot has been invalidated."""


def _book(instrument_id: str, position_side: str) -> tuple[str, str]:
    symbol = str(instrument_id).strip().upper().removesuffix("-PERP.BINANCE")
    side = str(position_side).strip().upper()
    if not re.fullmatch(r"[A-Z0-9_]+", symbol):
        raise ValueError("invalid position revision instrument_id")
    if side not in {"LONG", "SHORT"}:
        raise ValueError("position revision side must be LONG or SHORT")
    return symbol, side


def _values(row: Any, names: tuple[str, ...]) -> tuple:
    if isinstance(row, Mapping):
        return tuple(row[name] for name in names)
    return tuple(row)


def capture_versions(cur, account_id: str) -> dict:
    """Capture every known book and the wildcard with one statement snapshot."""
    cur.execute(
        """
        SELECT instrument_id, position_side, revision
        FROM position_revisions WHERE account_id = %s
        """,
        (account_id,),
    )
    versions = {"account_id": account_id, "account_revision": 0, "books": {}}
    for row in cur.fetchall():
        instrument, side, revision = _values(
            row, ("instrument_id", "position_side", "revision")
        )
        if instrument == "*":
            versions["account_revision"] = int(revision)
        else:
            versions["books"][f"{instrument}|{side}"] = int(revision)
    return versions


def read_current(cur, account_id: str, instrument_id: str, side: str) -> dict:
    """Read the account/book pair atomically; never creates rows or takes locks."""
    instrument, position_side = _book(instrument_id, side)
    cur.execute(
        """
        SELECT instrument_id, revision FROM position_revisions
        WHERE account_id = %s
          AND ((instrument_id = '*' AND position_side = '*')
            OR (instrument_id = %s AND position_side = %s))
        """,
        (account_id, instrument, position_side),
    )
    current = {
        "account_id": account_id,
        "account_revision": 0,
        "book_revision": 0,
        "instrument_id": instrument,
        "position_side": position_side,
    }
    for row in cur.fetchall():
        scope, revision = _values(row, ("instrument_id", "revision"))
        key = "account_revision" if scope == "*" else "book_revision"
        current[key] = int(revision)
    return current


def bind_precondition(
    cur,
    account_id: str,
    instrument_id: str,
    position_side: str,
    expected_versions: dict | None = None,
) -> dict:
    """Bind a human's current book or verify the server's pre-model snapshot."""
    current = read_current(cur, account_id, instrument_id, position_side)
    if expected_versions is None:
        return current
    if not isinstance(expected_versions, Mapping):
        raise StalePositionRevision("missing position revision snapshot")
    expected_account = expected_versions.get("account_id")
    if expected_account != account_id:
        raise StalePositionRevision("position revision snapshot account mismatch")
    books = expected_versions.get("books")
    if not isinstance(books, Mapping):
        raise StalePositionRevision("invalid position revision book snapshot")
    key = f"{current['instrument_id']}|{current['position_side']}"
    account_revision = expected_versions.get("account_revision")
    book_revision = books.get(key, 0)
    for revision in (account_revision, book_revision):
        if type(revision) is not int or revision < 0:
            raise StalePositionRevision("invalid position revision snapshot counter")
    if (
        account_revision != current["account_revision"]
        or book_revision != current["book_revision"]
    ):
        raise StalePositionRevision("position revision changed since snapshot")
    return current


def _invalidate(cur, account_id: str, instrument: str, side: str, operation_id: str) -> None:
    if operation_id is None or not str(operation_id).strip():
        raise ValueError("position revision invalidation requires operation_id")
    operation = str(operation_id)
    cur.execute(
        """
        INSERT INTO position_revision_invalidations
            (account_id, operation_id, instrument_id, position_side)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (account_id, operation_id) DO NOTHING
        RETURNING operation_id
        """,
        (account_id, operation, instrument, side),
    )
    if cur.fetchone() is None:
        # A second statement sees a concurrently committed winner under READ
        # COMMITTED. Reusing an operation for a different scope is never a no-op.
        cur.execute(
            """
            SELECT instrument_id, position_side FROM position_revision_invalidations
            WHERE account_id = %s AND operation_id = %s
            """,
            (account_id, operation),
        )
        row = cur.fetchone()
        if row is None or _values(row, ("instrument_id", "position_side")) != (instrument, side):
            raise ValueError("position revision operation_id reused for another scope")
        return
    cur.execute(
        """
        INSERT INTO position_revisions (account_id, instrument_id, position_side, revision)
        VALUES (%s, %s, %s, 1)
        ON CONFLICT (account_id, instrument_id, position_side)
        DO UPDATE SET revision = position_revisions.revision + 1, updated_at = now()
        """,
        (account_id, instrument, side),
    )


def invalidate_book(cur, account_id: str, instrument_id: str, position_side: str, operation_id: str) -> None:
    """Advance one book exactly once for this account/operation, within caller TX."""
    instrument, side = _book(instrument_id, position_side)
    _invalidate(cur, account_id, instrument, side, operation_id)


def invalidate_account(cur, account_id: str, operation_id: str) -> None:
    """Invalidate every existing and future book for an account-wide close."""
    _invalidate(cur, account_id, "*", "*", operation_id)
