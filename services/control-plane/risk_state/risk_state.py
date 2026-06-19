"""Canonical risk mode (kill switch state) stored on risk_state.state.mode.

The Decision Gateway/governor reads this mode; ACTIVE allows new risk, REDUCING
blocks opening, HALTED blocks all new risk.
"""

from __future__ import annotations

from uuid import uuid4

VALID_MODES = ("ACTIVE", "REDUCING", "HALTED")


def set_mode(cur, account_id: str, instrument_id: str, mode: str) -> None:
    if mode not in VALID_MODES:
        raise ValueError(f"invalid risk mode {mode!r}")
    cur.execute(
        """
        INSERT INTO risk_state (risk_state_id, account_id, instrument_id, state)
        VALUES (%s, %s, %s, jsonb_build_object('mode', %s::text))
        ON CONFLICT (account_id, instrument_id) DO UPDATE
            SET state = jsonb_set(coalesce(risk_state.state, '{}'::jsonb), '{mode}', to_jsonb(%s::text)),
                updated_at = now()
        """,
        (str(uuid4()), account_id, instrument_id, mode, mode),
    )


def get_mode(cur, account_id: str, instrument_id: str) -> str:
    cur.execute(
        "SELECT state->>'mode' FROM risk_state WHERE account_id = %s AND instrument_id = %s",
        (account_id, instrument_id),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else "ACTIVE"
