"""Cursor-scripted regressions for occupancy, venue conflict, and replay chain.

These helpers are the Codex occupancy/venue/replay rules. They do not start
Postgres; HTTP integration lives in test_operator_add_position.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

import read_api


_NOW = datetime.now(timezone.utc)
_SNAP = (_NOW, _NOW.isoformat())


@pytest.fixture(autouse=True)
def no_venue_snapshot(monkeypatch):
    monkeypatch.setattr(read_api, "_entry_venue_orders", lambda cur, account_id: {})


class ScriptedCursor:
    def __init__(self, queue: list):
        self.queue = list(queue)
        self.statements: list[tuple] = []

    def execute(self, sql, params=None):
        self.statements.append((" ".join(str(sql).split()), params))

    def _pop(self):
        if not self.queue:
            raise AssertionError("scripted cursor exhausted")
        return self.queue.pop(0)

    def fetchall(self):
        value = self._pop()
        if value is None:
            return []
        return value

    def fetchone(self):
        value = self._pop()
        if isinstance(value, list):
            return value[0] if value else None
        return value


def test_filled_approved_intent_is_not_off_venue_reservation() -> None:
    intent_id = str(uuid4())
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 60}, False)],
            [("filled", "LIMIT", 38.7, 38.7, 1.55, {})],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("0")
    assert occupancy["venue_working"] == Decimal("0")
    assert occupancy["available_hold"] == Decimal("0")


def test_off_venue_approved_intent_counts_full_budget() -> None:
    intent_id = str(uuid4())
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 60}, False)],
            [],
            [],
            (0,),
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("60")
    assert occupancy["venue_working"] == Decimal("0")
    assert occupancy["available_hold"] == Decimal("60")


def test_working_gtc_remaining_is_venue_working_not_off_venue() -> None:
    intent_id = str(uuid4())
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 60}, False)],
            [("accepted", "LIMIT", Decimal("10"), Decimal("0"), Decimal("6"), {}, _NOW)],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("0")
    assert occupancy["venue_working"] == Decimal("60")


def test_protection_orders_are_not_entry_working_occupancy() -> None:
    intent_id = str(uuid4())
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 60}, False, "approved")],
            [
                (
                    "accepted",
                    "STOP_MARKET",
                    Decimal("10"),
                    Decimal("0"),
                    Decimal("6"),
                    {"reduce_only": True},
                    _NOW,
                )
            ],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("0")
    assert occupancy["available_hold"] == Decimal("0")
    assert occupancy["venue_working"] == Decimal("0")


def test_rejected_parent_still_occupies_accepted_working_leg() -> None:
    intent_id = str(uuid4())
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 60}, False, "rejected")],
            [("accepted", "LIMIT", Decimal("10"), Decimal("0"), Decimal("6"), {}, _NOW)],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["venue_working"] == Decimal("60")
    assert occupancy["off_venue"] == Decimal("0")


def test_rejected_parent_does_not_release_submitted_pre_venue_leg() -> None:
    intent_id = str(uuid4())
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 60}, False, "rejected")],
            [("submitted", "LIMIT", Decimal("10"), Decimal("0"), Decimal("6"), {}, _NOW)],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("60")
    assert occupancy["venue_working"] == Decimal("0")


def test_rejected_parent_filled_only_is_not_off_venue() -> None:
    intent_id = str(uuid4())
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 60}, False, "rejected")],
            [("filled", "LIMIT", Decimal("10"), Decimal("10"), Decimal("6"), {}, _NOW)],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("0")
    assert occupancy["venue_working"] == Decimal("0")


def test_lost_entry_leg_reserves_intent_budget() -> None:
    intent_id = str(uuid4())
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 60}, False, "approved")],
            [("lost", "LIMIT", Decimal("10"), Decimal("0"), Decimal("6"), {}, _NOW)],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("60")
    assert occupancy["available_hold"] == Decimal("60")
    assert occupancy["venue_working"] == Decimal("0")


def test_rejected_parent_lost_leg_reserves_intent_budget() -> None:
    intent_id = str(uuid4())
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 60}, False, "rejected")],
            [("lost", "LIMIT", Decimal("10"), Decimal("0"), Decimal("6"), {}, _NOW)],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("60")
    assert occupancy["available_hold"] == Decimal("60")
    assert occupancy["venue_working"] == Decimal("0")


def test_unsent_planned_ladder_leg_is_reserved() -> None:
    intent_id = str(uuid4())
    plan = {
        "type": "entry_batch",
        "tranches": [
            {"seq": 1, "quantity": "5", "price": 6},
            {"seq": 2, "quantity": "5", "price": 6},
        ],
    }
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 70}, False, "approved", plan)],
            [
                (
                    "filled",
                    "LIMIT",
                    Decimal("5"),
                    Decimal("5"),
                    Decimal("6"),
                    {"seq": 1},
                    _NOW,
                    f"B{intent_id.replace('-', '')}01",
                )
            ],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("30")
    assert occupancy["venue_working"] == Decimal("0")
    assert occupancy["available_hold"] == Decimal("30")


def test_lot_size_unused_budget_after_completed_plan_is_not_reserved() -> None:
    intent_id = str(uuid4())
    plan = {"type": "limit", "quantity": "10", "price": 6}
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 70}, False, "approved", plan)],
            [
                (
                    "filled",
                    "LIMIT",
                    Decimal("10"),
                    Decimal("10"),
                    Decimal("6"),
                    {"seq": 1},
                    _NOW,
                    f"B{intent_id.replace('-', '')}01",
                )
            ],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("0")
    assert occupancy["venue_working"] == Decimal("0")
    assert occupancy["available_hold"] == Decimal("0")


def test_released_invalid_budget_without_legs_does_not_block() -> None:
    intent_id = str(uuid4())
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, "bad-budget", False, "rejected")],
            [],
            [],
            (0,),
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("0")
    assert occupancy["venue_working"] == Decimal("0")


def test_ambiguous_unsent_leg_reserves_intent_budget() -> None:
    intent_id = str(uuid4())
    plan = {
        "type": "entry_batch",
        "tranches": [
            {"seq": 1, "quantity": "5"},
            {"seq": 2, "quantity": "5"},
        ],
    }
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 70}, False, "approved", plan)],
            [
                (
                    "filled",
                    "LIMIT",
                    Decimal("5"),
                    Decimal("5"),
                    Decimal("6"),
                    {"seq": 1},
                    _NOW,
                    f"B{intent_id.replace('-', '')}01",
                )
            ],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("70")
    assert occupancy["available_hold"] == Decimal("70")
    assert occupancy["venue_working"] == Decimal("0")


def test_rejected_parent_does_not_hold_ungenerated_remainder() -> None:
    intent_id = str(uuid4())
    plan = {
        "type": "entry_batch",
        "tranches": [
            {"seq": 1, "quantity": "5", "price": 6},
            {"seq": 2, "quantity": "5", "price": 6},
        ],
    }
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 70}, False, "rejected", plan)],
            [
                (
                    "filled",
                    "LIMIT",
                    Decimal("5"),
                    Decimal("5"),
                    Decimal("6"),
                    {"seq": 1},
                    _NOW,
                    f"B{intent_id.replace('-', '')}01",
                )
            ],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("0")
    assert occupancy["venue_working"] == Decimal("0")


def test_malformed_positions_payload_is_not_known_flat() -> None:
    now = datetime.now(timezone.utc)
    cur = ScriptedCursor(
        [
            None,
            ({"not": "a-list"}, now, {"reconciliation_state": "healthy"}, now),
            [],
        ]
    )
    view = read_api._load_entry_venue_view(cur, account_id="account-b", symbol="ATOMUSDT")
    assert view["state"] == "unknown"
    assert view["notional"] is None


def test_fresh_heartbeat_conflict_overrides_fresh_mirror() -> None:
    now = datetime.now(timezone.utc)
    position = {
        "symbol": "ATOMUSDT",
        "position_side": "LONG",
        "position_amt": "1",
        "mark_price": "1.55",
    }
    cur = ScriptedCursor(
        [
            ({"positions": [position]}, now),
            (
                [position],
                now,
                {"reconciliation_state": "failed"},
                now,
            ),
            [],
        ]
    )
    view = read_api._load_entry_venue_view(cur, account_id="account-b", symbol="ATOMUSDT")
    assert view["state"] == "conflict"
    assert view["notional"] is None


def test_same_side_quantity_mismatch_is_conflict() -> None:
    now = datetime.now(timezone.utc)
    mirror_pos = {
        "symbol": "ATOMUSDT",
        "position_side": "LONG",
        "position_amt": "2",
        "mark_price": "1.55",
    }
    hb_pos = {
        "symbol": "ATOMUSDT",
        "position_side": "LONG",
        "position_amt": "1",
        "mark_price": "1.55",
    }
    cur = ScriptedCursor(
        [
            ({"positions": [mirror_pos]}, now),
            ([hb_pos], now, {"reconciliation_state": "healthy"}, now),
            [],
        ]
    )
    view = read_api._load_entry_venue_view(cur, account_id="account-b", symbol="ATOMUSDT")
    assert view["state"] == "conflict"


def test_unknown_notional_rejects_open_capital_reservation() -> None:
    with pytest.raises(read_api.HTTPException) as exc:
        read_api._assert_entry_capital_reservation(
            ScriptedCursor([]),
            account_id="account-b",
            new_notional=12,
            leverage=10,
            caps={"max_leverage": 10},
            venue={"state": "unknown", "notional": None},
            checks=[],
        )
    assert exc.value.status_code == 409
    assert "unknown" in str(exc.value.detail)


def test_replay_of_requires_entry_action() -> None:
    intent_id = str(uuid4())
    cur = ScriptedCursor(
        [
            (
                intent_id,
                "account-b",
                "close_position",
                "rejected",
                "deadbeef",
                {},
            )
        ]
    )
    with pytest.raises(read_api.HTTPException) as exc:
        read_api._validate_replay_of(
            cur,
            replay_of=intent_id,
            account_id="account-b",
            client_ref="tg-sig-c1-m1",
        )
    assert exc.value.status_code == 400
    assert "entry" in str(exc.value.detail)


def test_replay_chain_live_intent_blocks_old_rejected_parent() -> None:
    parent = str(uuid4())
    live = str(uuid4())
    client_ref = "tg-sig-c1002189417451-m6901"
    account_id = "account-c"
    source_key = read_api._entry_source_idempotency_key(account_id, client_ref)
    cur = ScriptedCursor(
        [
            (
                parent,
                account_id,
                "open_position",
                "rejected",
                source_key,
                {"authorization": {"source_message_id": client_ref}},
            ),
            [
                (
                    parent,
                    "open_position",
                    "rejected",
                    source_key,
                    {"authorization": {"source_message_id": client_ref}},
                    None,
                    {"max_notional": 12},
                ),
                (
                    live,
                    "add_position",
                    "approved",
                    "otherkey",
                    {
                        "replay_of": parent,
                        "authorization": {"source_message_id": client_ref},
                    },
                    None,
                    {"max_notional": 12},
                ),
            ],
            (0,),
            [],
            (0,),
            [],
        ]
    )
    with pytest.raises(read_api.HTTPException) as exc:
        read_api._validate_replay_of(
            cur,
            replay_of=parent,
            account_id=account_id,
            client_ref=client_ref,
        )
    assert exc.value.status_code == 409
    assert "in-flight or filled" in str(exc.value.detail)


def test_expired_never_placed_is_not_off_venue() -> None:
    intent_id = str(uuid4())
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 60}, True)],
            [],
            [],
            (0,),
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("0")
    assert occupancy["venue_working"] == Decimal("0")
    assert occupancy["available_hold"] == Decimal("0")


def test_initialized_order_is_off_venue_not_venue_working() -> None:
    intent_id = str(uuid4())
    cur = ScriptedCursor(
        [
            _SNAP,
            [(intent_id, {"max_notional": 60}, False)],
            [
                (
                    "initialized",
                    "LIMIT",
                    Decimal("10"),
                    Decimal("0"),
                    Decimal("6"),
                    {},
                    _NOW,
                )
            ],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["off_venue"] == Decimal("60")
    assert occupancy["venue_working"] == Decimal("0")
    assert occupancy["available_hold"] == Decimal("60")


def test_accepted_before_account_snapshot_holds_available() -> None:
    intent_id = str(uuid4())
    accept_at = _NOW
    snapshot_at = _NOW - timedelta(seconds=20)
    cur = ScriptedCursor(
        [
            (snapshot_at, snapshot_at.isoformat()),
            [(intent_id, {"max_notional": 60}, False)],
            [
                (
                    "accepted",
                    "LIMIT",
                    Decimal("10"),
                    Decimal("0"),
                    Decimal("6"),
                    {},
                    accept_at,
                )
            ],
        ]
    )
    occupancy = read_api._entry_intent_occupancy(cur, "account-b")
    assert occupancy["venue_working"] == Decimal("60")
    assert occupancy["off_venue"] == Decimal("0")
    assert occupancy["available_hold"] == Decimal("60")


def test_account_lock_sql_is_shared_two_arg_hashtext() -> None:
    cur = ScriptedCursor([])
    read_api._account_risk_increase_lock(cur, "account-c")
    sql, params = cur.statements[0]
    assert "pg_advisory_xact_lock(hashtext(%s), 0)" in sql
    assert params == ("account-c",)


@pytest.mark.parametrize("plan", [{}, {"entry": {"type": "zone"}}, {"type": "limit", "quantity": "10", "price": 6}])
def test_three_working_legs_count_remaining_without_plan_shape_veto(plan) -> None:
    intent = str(uuid4())
    orders = [("accepted", "LIMIT", 10, 2, 6, {}, _NOW,
               f"B{intent.replace('-', '')}{seq:02d}") for seq in (1, 2, 3)]
    cur = ScriptedCursor([_SNAP, [(intent, {"max_notional": 180}, False, "approved", plan)], orders])
    assert read_api._entry_intent_occupancy(cur, "account-b") == {
        "venue_working": Decimal("144"), "off_venue": Decimal("0"),
        "available_hold": Decimal("0"),
    }


@pytest.mark.parametrize("status", ["filled", "canceled", "cancelled", "expired"])
def test_terminal_extra_legs_with_empty_quantity_never_reserve_or_veto(status) -> None:
    intent = str(uuid4())
    orders = [("accepted", "LIMIT", 10, 0, 6, {}, _NOW, f"B{intent.replace('-', '')}01")]
    orders += [(status, "MARKET", None, None, None, {}, _NOW,
                f"B{intent.replace('-', '')}{seq:02d}") for seq in (2, 3)]
    plan = {"type": "limit", "quantity": "10", "price": 6}
    cur = ScriptedCursor([_SNAP, [(intent, {"max_notional": 180}, False, "approved", plan)], orders])
    assert read_api._entry_intent_occupancy(cur, "account-b")["off_venue"] == 0


@pytest.mark.parametrize("seq,payload,order_type", [
    (10, {}, None), (11, {}, None), (99, {}, None),
    (1, {"reduceOnly": "true"}, "LIMIT"), (1, {}, "TAKE_PROFIT_MARKET"),
])
def test_protection_only_thin_shell_has_zero_occupancy(seq, payload, order_type) -> None:
    intent = str(uuid4())
    orders = [("accepted", order_type, None, None, None, payload, _NOW,
               f"B{intent.replace('-', '')}{seq:02d}")]
    cur = ScriptedCursor([_SNAP, [(intent, {"max_notional": 180}, False, "approved")], orders])
    assert read_api._entry_intent_occupancy(cur, "account-b") == {
        "venue_working": Decimal("0"), "off_venue": Decimal("0"), "available_hold": Decimal("0"),
    }


def test_unknown_leg_reserves_ceiling_without_double_counting_known_working() -> None:
    intent = str(uuid4())
    orders = [("accepted", "LIMIT", 10, 0, 6, {}, _NOW, f"B{intent.replace('-', '')}01"),
              ("lost", None, None, None, None, {}, _NOW, f"B{intent.replace('-', '')}02")]
    cur = ScriptedCursor([_SNAP, [(intent, {"max_notional": 100}, False, "approved")], orders])
    assert read_api._entry_intent_occupancy(cur, "account-b") == {
        "venue_working": Decimal("60"), "off_venue": Decimal("40"), "available_hold": Decimal("40"),
    }
