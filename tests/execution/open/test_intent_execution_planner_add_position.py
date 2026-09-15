"""Same-side add_position planner: venue existence, fail-closed risk, open guard.

Contract: docs/plans/2026-09-14-same-side-add-position.md (Codex header).
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
for _path in (str(SERVICE_ROOT), str(DOMAIN_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from execution_domain.account_execution_ledger import (  # noqa: E402
    ReconciledExecutionState,
)
from strategy.intent_execution_planner import (  # noqa: E402
    InstrumentSpec,
    OrderDenied,
    OrderPlan,
    PlannerContext,
    PositionSnapshot,
    plan_intent_execution,
)


ACCOUNT_ID = "account-c"
INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
VENUE_SYMBOL = "BTCUSDT"
VENUE_QUANTITY = "0.055"
NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
FRESH_FETCHED_AT = NOW - timedelta(seconds=5)
STALE_FETCHED_AT = NOW - timedelta(seconds=120)


class AddPositionPlannerTest(unittest.TestCase):
    def test_open_still_denied_when_cache_empty_and_venue_same_side(self) -> None:
        result = plan_intent_execution(
            _entry_intent(action="open_position", side="sell"),
            _empty_cache_context(
                reconciled_state=_reconciled(venue_snapshot=_venue_short_snapshot()),
            ),
        )

        self.assertIsInstance(result, OrderDenied)
        assert isinstance(result, OrderDenied)
        self.assertEqual(result.reason, "position_exists")

    def test_add_allowed_when_cache_empty_and_fresh_venue_same_side(self) -> None:
        result = plan_intent_execution(
            _entry_intent(action="add_position", side="sell", order_type="limit"),
            _empty_cache_context(
                reconciled_state=_reconciled(venue_snapshot=_venue_short_snapshot()),
            ),
        )

        self.assertIsInstance(result, OrderPlan)
        assert isinstance(result, OrderPlan)
        self.assertEqual(result.side, "SELL")
        self.assertEqual(result.order_type, "LIMIT")
        self.assertEqual(result.price, "78800.0")
        self.assertFalse(result.reduce_only)

    def test_add_denied_when_venue_flat(self) -> None:
        result = plan_intent_execution(
            _entry_intent(action="add_position", side="sell"),
            _empty_cache_context(
                reconciled_state=_reconciled(venue_snapshot=_venue_flat_snapshot()),
            ),
        )

        self.assertEqual(
            result,
            OrderDenied(reason="position_required", detail=INSTRUMENT_ID),
        )

    def test_add_denied_when_venue_stale_or_missing(self) -> None:
        stale = _reconciled(
            venue_snapshot=_venue_short_snapshot(),
            venue_fetched_at=STALE_FETCHED_AT,
        )
        missing = _reconciled(venue_snapshot=None, venue_fetched_at=None)
        for label, reconciled in (("stale", stale), ("missing", missing)):
            with self.subTest(venue=label):
                result = plan_intent_execution(
                    _entry_intent(action="add_position", side="sell"),
                    _empty_cache_context(reconciled_state=reconciled),
                )
                self.assertIsInstance(result, OrderDenied)
                assert isinstance(result, OrderDenied)
                self.assertEqual(result.reason, "position_state_unknown")

    def test_add_denied_when_cache_same_side_but_venue_conflicted(self) -> None:
        position = _short_position()
        result = plan_intent_execution(
            _entry_intent(action="add_position", side="sell"),
            _context(
                position=position,
                positions=(position,),
                reconciled_state=_reconciled(
                    venue_snapshot=_venue_flat_snapshot(),
                    cache_positions=(position,),
                ),
            ),
        )

        self.assertIsInstance(result, OrderDenied)
        assert isinstance(result, OrderDenied)
        self.assertEqual(result.reason, "position_state_conflicted")

    def test_add_denied_for_opposite_book_open_allowed_in_hedge(self) -> None:
        venue_long = _reconciled(venue_snapshot=_venue_long_snapshot())
        add_sell = plan_intent_execution(
            _entry_intent(action="add_position", side="sell"),
            _empty_cache_context(reconciled_state=venue_long),
        )
        open_sell = plan_intent_execution(
            _entry_intent(action="open_position", side="sell"),
            _empty_cache_context(reconciled_state=venue_long),
        )

        self.assertEqual(
            add_sell,
            OrderDenied(reason="position_required", detail=INSTRUMENT_ID),
        )
        self.assertIsInstance(open_sell, OrderPlan)
        assert isinstance(open_sell, OrderPlan)
        self.assertEqual(open_sell.side, "SELL")

    def test_add_ignores_opposite_cache_book_when_venue_same_side_open(self) -> None:
        long_position = PositionSnapshot(
            instrument_id=INSTRUMENT_ID,
            side="LONG",
            quantity="0.2",
            position_id=f"{INSTRUMENT_ID}-LONG",
        )
        result = plan_intent_execution(
            _entry_intent(action="add_position", side="sell", order_type="limit"),
            _context(
                position=long_position,
                positions=(long_position,),
                reconciled_state=_reconciled(venue_snapshot=_venue_short_snapshot()),
            ),
        )

        self.assertIsInstance(result, OrderPlan)
        assert isinstance(result, OrderPlan)
        self.assertEqual(result.side, "SELL")
        self.assertFalse(result.reduce_only)

    def test_add_cache_fast_path_when_same_side_matches_fresh_venue(self) -> None:
        position = _short_position()
        result = plan_intent_execution(
            _entry_intent(action="add_position", side="sell", order_type="limit"),
            _context(
                position=position,
                positions=(position,),
                reconciled_state=_reconciled(
                    venue_snapshot=_venue_short_snapshot(),
                    cache_positions=(position,),
                ),
            ),
        )

        self.assertIsInstance(result, OrderPlan)
        assert isinstance(result, OrderPlan)
        self.assertEqual(result.order_type, "LIMIT")

    def test_zone_add_reuses_validate_position_venue_fallback(self) -> None:
        intent = _entry_intent(
            action="add_position",
            side="sell",
            order_plan={
                "type": "zone",
                "side": "sell",
                "quantity": "0.08",
                "price_min": "78800",
                "price_max": "79000",
            },
        )
        result = plan_intent_execution(
            intent,
            _empty_cache_context(
                reconciled_state=_reconciled(venue_snapshot=_venue_short_snapshot()),
            ),
        )

        self.assertIsInstance(result, OrderPlan)
        assert isinstance(result, OrderPlan)
        self.assertEqual(result.side, "SELL")
        self.assertEqual(result.order_type, "LIMIT")
        self.assertEqual(result.price, "78800.0")
        self.assertFalse(result.reduce_only)

    def test_add_denied_when_reconciled_state_missing_even_with_cache(self) -> None:
        position = _short_position()
        result = plan_intent_execution(
            _entry_intent(action="add_position", side="sell", order_type="limit"),
            _context(position=position, positions=(position,), reconciled_state=None),
        )

        self.assertIsInstance(result, OrderDenied)
        assert isinstance(result, OrderDenied)
        self.assertEqual(result.reason, "position_state_unknown")
        self.assertEqual(result.detail, "reconciled_state_missing")

    def test_add_simulation_allows_cache_same_side_without_venue(self) -> None:
        position = _short_position()
        result = plan_intent_execution(
            _entry_intent(action="add_position", side="sell", order_type="limit"),
            _context(
                position=position,
                positions=(position,),
                reconciled_state=None,
                simulation=True,
            ),
        )

        self.assertIsInstance(result, OrderPlan)
        assert isinstance(result, OrderPlan)
        self.assertEqual(result.side, "SELL")
        self.assertFalse(result.reduce_only)

    def test_halted_still_denies_add(self) -> None:
        result = plan_intent_execution(
            _entry_intent(action="add_position", side="sell"),
            _empty_cache_context(
                trading_state="HALTED",
                reconciled_state=_reconciled(venue_snapshot=_venue_short_snapshot()),
            ),
        )

        self.assertEqual(
            result,
            OrderDenied(reason="trading_not_active", detail="HALTED"),
        )


def _venue_short_snapshot() -> dict[str, Any]:
    return {
        "positions": [
            {
                "symbol": VENUE_SYMBOL,
                "position_amt": VENUE_QUANTITY,
                "position_side": "SHORT",
            }
        ],
        "open_orders": [],
        "algo_orders": [],
    }


def _venue_long_snapshot() -> dict[str, Any]:
    return {
        "positions": [
            {
                "symbol": VENUE_SYMBOL,
                "position_amt": "0.2",
                "position_side": "LONG",
            }
        ],
        "open_orders": [],
        "algo_orders": [],
    }


def _venue_flat_snapshot() -> dict[str, Any]:
    return {"positions": [], "open_orders": [], "algo_orders": []}


def _reconciled(
    *,
    venue_snapshot: dict[str, Any] | None,
    venue_fetched_at: datetime | None = FRESH_FETCHED_AT,
    cache_positions: tuple[Any, ...] = (),
) -> ReconciledExecutionState:
    return ReconciledExecutionState.build(
        account_id=ACCOUNT_ID,
        venue_snapshot=venue_snapshot,
        venue_fetched_at=venue_fetched_at,
        cache_positions=cache_positions,
        now=NOW,
    )


def _context(**overrides: Any) -> PlannerContext:
    values: dict[str, Any] = {
        "account_id": ACCOUNT_ID,
        "trading_state": "ACTIVE",
        "now": NOW,
        "instrument": InstrumentSpec(
            instrument_id=INSTRUMENT_ID,
            price_increment="0.1",
            quantity_increment="0.001",
        ),
        "position": None,
        "positions": (),
        "existing_orders": (),
        "existing_intent_ids": frozenset(),
    }
    values.update(overrides)
    return PlannerContext(**values)


def _empty_cache_context(**overrides: Any) -> PlannerContext:
    overrides.setdefault("position", None)
    overrides.setdefault("positions", ())
    return _context(**overrides)


def _short_position() -> PositionSnapshot:
    return PositionSnapshot(
        instrument_id=INSTRUMENT_ID,
        side="SHORT",
        quantity=VENUE_QUANTITY,
        position_id=f"{INSTRUMENT_ID}-SHORT",
    )


def _entry_intent(
    *,
    action: str,
    side: str,
    order_type: str = "market",
    order_plan: dict[str, Any] | None = None,
) -> "_Intent":
    if order_plan is None:
        order_plan = {
            "type": order_type,
            "side": side,
            "quantity": "0.08",
        }
        if order_type == "limit":
            order_plan["price"] = "78800"
            order_plan["time_in_force"] = "GTC"
    return _intent(action=action, order_plan=order_plan)


@dataclass(frozen=True)
class _Intent:
    schema_version: str
    intent_id: UUID
    decision_id: UUID
    risk_decision_id: UUID
    account_id: str
    instrument_id: str
    action: str
    order_plan: dict[str, Any]
    risk_budget: Any
    valid_until: datetime
    idempotency_key: str
    approved_at: datetime
    target_position_id: str | None = None


def _intent(**overrides: Any) -> _Intent:
    intent_id = overrides.get("intent_id", uuid4())
    values: dict[str, Any] = {
        "schema_version": "1.0",
        "intent_id": intent_id,
        "decision_id": uuid4(),
        "risk_decision_id": uuid4(),
        "account_id": ACCOUNT_ID,
        "instrument_id": INSTRUMENT_ID,
        "action": "add_position",
        "order_plan": {"type": "market", "side": "sell", "quantity": "0.08"},
        "risk_budget": SimpleNamespace(max_notional="100000"),
        "target_position_id": None,
        "valid_until": NOW + timedelta(minutes=5),
        "idempotency_key": sha256(str(intent_id).encode("ascii")).hexdigest(),
        "approved_at": NOW - timedelta(seconds=5),
    }
    values.update(overrides)
    order_plan = dict(values["order_plan"])
    order_plan.setdefault(
        "authorization",
        {
            "authorized_by_type": "user",
            "authorized_by_id": "add-position-test",
            "source_message_id": f"add-position-test-{intent_id}",
        },
    )
    values["order_plan"] = order_plan
    return _Intent(**values)


if __name__ == "__main__":
    unittest.main()
