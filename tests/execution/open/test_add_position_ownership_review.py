"""Independent review: add owned-fill exit accounting and SL resize order.

Does not modify planner/strategy. Reuses the add-protection strategy fixture
and execution_domain record_fill/owned_quantity helpers.

Contract: docs/plans/2026-09-14-same-side-add-position.md Codex header §3
(protect this add's traceable fills only; establish full protection before
replacing an undersized same-intent stop).
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
OPEN_TESTS = Path(__file__).resolve().parent
for _path in (str(SERVICE_ROOT), str(DOMAIN_ROOT), str(OPEN_TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from execution_domain.entry_batch import owned_quantity  # noqa: E402
from strategy.intent_execution_planner import encode_client_order_id  # noqa: E402
from test_add_position_protection import (  # noqa: E402
    INSTRUMENT_ID,
    POSITION_ID,
    _ProtectionStrategy,
    _add_intent_and_plan,
    _live_order,
)


ORIGINAL_CACHE = "0.055"
ADD_FILL = "0.088"
PARTIAL_ENTRY = "0.04"
PARTIAL_STOP = "0.04"


def _qty(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _entry_and_stop_orders(intent_id: UUID, *, entry_filled: str, stop_filled: str | None):
    entry = SimpleNamespace(
        client_order_id=encode_client_order_id(intent_id, sequence=1),
        filled_qty=entry_filled,
        quantity=ADD_FILL,
        status="FILLED" if _qty(entry_filled) == _qty(ADD_FILL) else "PARTIALLY_FILLED",
    )
    orders = [entry]
    if stop_filled is not None:
        orders.append(
            _live_order(
                encode_client_order_id(intent_id, sequence=11),
                "STOP_MARKET",
                ADD_FILL,
                "80000",
                tags=("lifecycle_role=stop_loss",),
            )
        )
        orders[-1].filled_qty = stop_filled
        orders[-1].status = (
            "FILLED" if _qty(stop_filled) == _qty(ADD_FILL) else "PARTIALLY_FILLED"
        )
    return orders


def _record_fill(strategy, client_order_id: str, last_qty: str, trade_id: str) -> None:
    strategy._record_batch_fill(
        SimpleNamespace(
            client_order_id=client_order_id,
            last_qty=last_qty,
            trade_id=trade_id,
        )
    )


class AddPositionOwnershipReviewTest(unittest.TestCase):
    def test_owned_fill_role_treats_this_intent_protection_seq_as_exit(self) -> None:
        strategy = _ProtectionStrategy()
        intent, plan = _add_intent_and_plan(quantity=ADD_FILL)
        assert strategy._stage_entry_protection(intent, plan)
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        key = str(intent.intent_id)
        stop_id = encode_client_order_id(intent.intent_id, sequence=11)

        assert strategy._owned_fill_role(stash, key, plan.client_order_id) == "entry"
        assert strategy._owned_fill_role(stash, key, stop_id) == "exit"
        assert strategy._owned_fill_role(stash, key, "aos_half_sl_1789061188") is False

    def test_full_add_and_stop_fill_does_not_reprotect_old_cache(self) -> None:
        """Add 0.088 then this-intent SL 0.088; cache still 0.055 original.

        Owned protection must be 0. Sync must not submit another 0.088 stop
        that would cover the leftover book.
        """
        strategy = _ProtectionStrategy()
        intent, plan = _add_intent_and_plan(quantity=ADD_FILL)
        assert strategy._stage_entry_protection(intent, plan)
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        stop_id = encode_client_order_id(intent.intent_id, sequence=11)
        cache_position = {
            "id": POSITION_ID,
            "side": "SHORT",
            "quantity": ORIGINAL_CACHE,
        }
        strategy._positions = [cache_position]
        strategy._orders = _entry_and_stop_orders(
            intent.intent_id, entry_filled=ADD_FILL, stop_filled=ADD_FILL
        )

        _record_fill(strategy, plan.client_order_id, ADD_FILL, "e-add")
        _record_fill(strategy, stop_id, ADD_FILL, "x-sl")
        _record_fill(strategy, stop_id, ADD_FILL, "x-sl")

        owned = strategy._protection_quantity(stash, cache_position)
        assert _qty(owned) == Decimal("0"), owned
        assert owned_quantity(stash["batch_fills"]) == Decimal("0")

        strategy._sync_protection(str(intent.intent_id))
        submitted_qty = [_qty(plan.quantity) for plan in strategy.submitted_plans]
        assert submitted_qty == []
        assert stash.get("pending_protection_revision") is None

    def test_repair_from_cache_subtracts_this_intent_stop_fill(self) -> None:
        """Missed SL notification: cache filled_qty on seq 11 must still deduct."""
        strategy = _ProtectionStrategy()
        intent, plan = _add_intent_and_plan(quantity=ADD_FILL)
        assert strategy._stage_entry_protection(intent, plan)
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        cache_position = {"quantity": ORIGINAL_CACHE, "side": "SHORT"}
        strategy._orders = _entry_and_stop_orders(
            intent.intent_id, entry_filled=ADD_FILL, stop_filled=ADD_FILL
        )

        owned = strategy._protection_quantity(stash, cache_position)
        assert _qty(owned) == Decimal("0"), owned

    def test_partial_stop_fill_deducts_and_replay_is_idempotent(self) -> None:
        strategy = _ProtectionStrategy()
        intent, plan = _add_intent_and_plan(quantity=ADD_FILL)
        assert strategy._stage_entry_protection(intent, plan)
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        stop_id = encode_client_order_id(intent.intent_id, sequence=11)
        cache_position = {"quantity": "0.103", "side": "SHORT"}
        strategy._orders = _entry_and_stop_orders(
            intent.intent_id, entry_filled=ADD_FILL, stop_filled=PARTIAL_STOP
        )

        _record_fill(strategy, plan.client_order_id, ADD_FILL, "e-add")
        _record_fill(strategy, stop_id, PARTIAL_STOP, "x-partial")
        _record_fill(strategy, stop_id, PARTIAL_STOP, "x-partial")

        owned = strategy._protection_quantity(stash, cache_position)
        assert _qty(owned) == _qty(ADD_FILL) - _qty(PARTIAL_STOP), owned

        strategy._orders[-1].filled_qty = ADD_FILL
        strategy._orders[-1].status = "FILLED"
        _record_fill(strategy, stop_id, "0.048", "x-rest")
        owned = strategy._protection_quantity(stash, {"quantity": ORIGINAL_CACHE})
        assert _qty(owned) == Decimal("0"), owned

    def test_partial_entry_resize_submits_full_stop_before_canceling_short_stop(
        self,
    ) -> None:
        """0.04 fill then 0.088: place 0.088 stop, then cancel 0.04. No gap."""
        strategy = _ProtectionStrategy()
        intent, plan = _add_intent_and_plan(quantity=ADD_FILL)
        assert strategy._stage_entry_protection(intent, plan)
        cache_position = {
            "id": POSITION_ID,
            "side": "SHORT",
            "quantity": "0.095",
        }
        strategy._positions = [cache_position]
        timeline: list[tuple[str, Decimal, str]] = []
        raw_submit = strategy._submit_order_plan
        raw_cancel = strategy._cancel_order_object

        def submit(order_plan):
            timeline.append(
                (
                    "submit",
                    _qty(order_plan.quantity),
                    str(order_plan.client_order_id),
                )
            )
            return raw_submit(order_plan)

        def cancel(order):
            timeline.append(
                (
                    "cancel",
                    _qty(getattr(order, "quantity", "0")),
                    str(getattr(order, "client_order_id", "")),
                )
            )
            return raw_cancel(order)

        strategy._submit_order_plan = submit  # type: ignore[method-assign]
        strategy._cancel_order_object = cancel  # type: ignore[method-assign]

        entry_order = SimpleNamespace(
            client_order_id=plan.client_order_id,
            filled_qty=PARTIAL_ENTRY,
            quantity=ADD_FILL,
            status="PARTIALLY_FILLED",
        )
        strategy._orders = [entry_order]
        _record_fill(strategy, plan.client_order_id, PARTIAL_ENTRY, "e1")
        strategy._sync_protection(str(intent.intent_id))

        first_stops = [
            item for item in timeline if item[0] == "submit" and item[1] == _qty(PARTIAL_ENTRY)
        ]
        assert first_stops, timeline
        first_stop_id = first_stops[0][2]
        live_stop = _live_order(
            first_stop_id,
            "STOP_MARKET",
            PARTIAL_ENTRY,
            "80000",
            tags=("lifecycle_role=stop_loss", f"position_id={POSITION_ID}"),
        )
        entry_order.filled_qty = ADD_FILL
        strategy._orders = [entry_order, live_stop]
        _record_fill(strategy, plan.client_order_id, "0.048", "e2")
        strategy._sync_protection(str(intent.intent_id))

        submit_full = next(
            (
                index
                for index, item in enumerate(timeline)
                if item[0] == "submit" and item[1] == _qty(ADD_FILL)
            ),
            None,
        )
        cancel_short = next(
            (
                index
                for index, item in enumerate(timeline)
                if item[0] == "cancel"
                and item[1] == _qty(PARTIAL_ENTRY)
                and item[2] == first_stop_id
            ),
            None,
        )
        assert submit_full is not None, timeline
        assert cancel_short is not None, timeline
        assert submit_full < cancel_short, timeline
        cancel_before_full = [
            item
            for item in timeline[:submit_full]
            if item[0] == "cancel" and item[2] == first_stop_id
        ]
        assert cancel_before_full == [], timeline
        instrument = strategy._instrument_spec(INSTRUMENT_ID)
        stash = strategy._entry_protection_stash[str(intent.intent_id)]
        desired = strategy._protection_order_plans(
            intent.intent_id,
            stash,
            instrument,
            cache_position,
            ADD_FILL,
            revision=1,
        )
        actions, _keep, replace_ids = strategy._protection_replacement_actions(
            stash,
            (live_stop,),
            desired,
            instrument,
        )
        assert any(_qty(action.quantity) == _qty(ADD_FILL) for action in actions)
        assert first_stop_id in replace_ids
