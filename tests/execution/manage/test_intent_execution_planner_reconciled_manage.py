"""WP-C management-path tests: planner consumption of ReconciledExecutionState.

Contract: docs/plans/2026-08-28-execution-state-arch-migration.md (WP-C).
The venue-evidence fallback engages only when the cache view is empty; the
cache fast path and reconciled_state=None must behave exactly like today.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
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
    ManagementPlan,
    OrderDenied,
    PlannerContext,
    PositionSnapshot,
    plan_intent_execution,
)


ACCOUNT_ID = "account-a"
INSTRUMENT_ID = "ATOMUSDT-PERP.BINANCE"
VENUE_SYMBOL = "ATOMUSDT"
SYNTHESIZED_POSITION_ID = f"{INSTRUMENT_ID}-LONG"
VENUE_QUANTITY = "596.48"
NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
FRESH_FETCHED_AT = NOW - timedelta(seconds=5)
STALE_FETCHED_AT = NOW - timedelta(seconds=120)


class ReconciledStateManagementPlannerTest(unittest.TestCase):
    def test_atom_regression_replace_take_profits_plans_four_mit_orders(self) -> None:
        """Cache empty + fresh venue 596.48 LONG -> 4 MIT take-profit plans."""
        intent = _intent(
            action="replace_take_profits",
            target_position_id=SYNTHESIZED_POSITION_ID,
            order_plan={
                "take_profits": [
                    {"quantity": "149.12", "price": "4.5"},
                    {"quantity": "149.12", "price": "4.65"},
                    {"quantity": "149.12", "price": "4.8"},
                    {"quantity": "149.12", "price": "4.95"},
                ]
            },
        )

        result = plan_intent_execution(
            intent,
            _empty_cache_context(
                reconciled_state=_reconciled(venue_snapshot=_venue_long_snapshot()),
            ),
        )

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.target_position_id, SYNTHESIZED_POSITION_ID)
        self.assertEqual(result.target_position_side, "LONG")
        self.assertEqual(len(result.orders), 4)
        self.assertEqual(
            [order.order_type for order in result.orders],
            ["MARKET_IF_TOUCHED"] * 4,
        )
        self.assertTrue(all(order.reduce_only for order in result.orders))
        self.assertTrue(all(order.side == "SELL" for order in result.orders))
        self.assertTrue(
            all(
                "lifecycle_role=take_profit" in order.tags
                for order in result.orders
            )
        )
        self.assertEqual(
            sum(Decimal(order.quantity) for order in result.orders),
            Decimal(VENUE_QUANTITY),
        )

    def test_take_profit_total_validates_against_venue_quantity(self) -> None:
        """The synthesized snapshot carries the venue quantity: exceeding it denies."""
        intent = _intent(
            action="replace_take_profits",
            target_position_id=SYNTHESIZED_POSITION_ID,
            order_plan={
                "take_profits": [
                    {"quantity": "300", "price": "4.5"},
                    {"quantity": "300", "price": "4.8"},
                ]
            },
        )

        result = plan_intent_execution(
            intent,
            _empty_cache_context(
                reconciled_state=_reconciled(venue_snapshot=_venue_long_snapshot()),
            ),
        )

        self.assertIsInstance(result, OrderDenied)
        assert isinstance(result, OrderDenied)
        self.assertEqual(result.reason, "quantity_exceeds_position")

    def test_stale_venue_denies_management_with_position_state_unknown(self) -> None:
        intent = _close_intent(target_position_id=SYNTHESIZED_POSITION_ID)

        result = plan_intent_execution(
            intent,
            _empty_cache_context(
                reconciled_state=_reconciled(
                    venue_snapshot=_venue_long_snapshot(),
                    venue_fetched_at=STALE_FETCHED_AT,
                ),
            ),
        )

        self.assertIsInstance(result, OrderDenied)
        assert isinstance(result, OrderDenied)
        self.assertEqual(result.reason, "position_state_unknown")

    def test_missing_venue_snapshot_denies_management_with_position_state_unknown(
        self,
    ) -> None:
        intent = _close_intent(target_position_id=SYNTHESIZED_POSITION_ID)

        result = plan_intent_execution(
            intent,
            _empty_cache_context(
                reconciled_state=_reconciled(
                    venue_snapshot=None,
                    venue_fetched_at=None,
                ),
            ),
        )

        self.assertIsInstance(result, OrderDenied)
        assert isinstance(result, OrderDenied)
        self.assertEqual(result.reason, "position_state_unknown")

    def test_conflicted_book_denies_management_with_position_state_conflicted(
        self,
    ) -> None:
        """Venue fresh + flat while the ledger cache holds a phantom position."""
        intent = _close_intent(target_position_id=SYNTHESIZED_POSITION_ID)
        reconciled = _reconciled(
            venue_snapshot=_venue_flat_snapshot(),
            cache_positions=(
                {
                    "instrument_id": INSTRUMENT_ID,
                    "side": "LONG",
                    "quantity": VENUE_QUANTITY,
                    "position_id": SYNTHESIZED_POSITION_ID,
                },
            ),
        )

        result = plan_intent_execution(
            intent,
            _empty_cache_context(reconciled_state=reconciled),
        )

        self.assertIsInstance(result, OrderDenied)
        assert isinstance(result, OrderDenied)
        self.assertEqual(result.reason, "position_state_conflicted")

    def test_known_flat_keeps_position_required_denial(self) -> None:
        intent = _close_intent(target_position_id=SYNTHESIZED_POSITION_ID)

        result = plan_intent_execution(
            intent,
            _empty_cache_context(
                reconciled_state=_reconciled(venue_snapshot=_venue_flat_snapshot()),
            ),
        )

        self.assertIsInstance(result, OrderDenied)
        assert isinstance(result, OrderDenied)
        self.assertEqual(result.reason, "position_required")

    def test_reconciled_state_none_is_equivalent_to_legacy_behavior(self) -> None:
        """reconciled_state=None + empty cache keeps today's position_required."""
        intent = _close_intent(target_position_id=SYNTHESIZED_POSITION_ID)

        result = plan_intent_execution(
            intent,
            _empty_cache_context(reconciled_state=None),
        )

        self.assertEqual(
            result,
            OrderDenied(
                reason="position_required",
                detail=SYNTHESIZED_POSITION_ID,
            ),
        )

    def test_cache_fast_path_is_unchanged_when_cache_holds_position(self) -> None:
        """Non-empty cache view wins even when venue evidence is stale."""
        position = PositionSnapshot(
            instrument_id=INSTRUMENT_ID,
            side="LONG",
            quantity=VENUE_QUANTITY,
            position_id="P-1",
            entry_price="4.10",
        )
        intent = _close_intent(target_position_id="P-1")

        result = plan_intent_execution(
            intent,
            _context(
                position=position,
                positions=(position,),
                reconciled_state=_reconciled(
                    venue_snapshot=_venue_long_snapshot(),
                    venue_fetched_at=STALE_FETCHED_AT,
                ),
            ),
        )

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.target_position_id, "P-1")
        self.assertEqual(result.orders[0].side, "SELL")
        self.assertEqual(result.orders[0].quantity, VENUE_QUANTITY)

    def test_position_id_variant_matches_known_open_book_with_consistent_side(
        self,
    ) -> None:
        """Restart position_id variants match when the KNOWN_OPEN side agrees."""
        intent = _intent(
            action="close_position",
            target_position_id="P-pre-restart-variant",
            order_plan={"type": "market", "position_side": "long"},
        )

        result = plan_intent_execution(
            intent,
            _empty_cache_context(
                reconciled_state=_reconciled(venue_snapshot=_venue_long_snapshot()),
            ),
        )

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.target_position_side, "LONG")
        self.assertEqual(result.orders[0].side, "SELL")
        self.assertEqual(
            Decimal(result.orders[0].quantity),
            Decimal(VENUE_QUANTITY),
        )


def _venue_long_snapshot() -> dict[str, Any]:
    return {
        "positions": [
            {
                "symbol": VENUE_SYMBOL,
                "position_amt": VENUE_QUANTITY,
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
            price_increment="0.001",
            quantity_increment="0.01",
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


def _close_intent(**overrides: Any) -> "_Intent":
    overrides.setdefault("action", "close_position")
    overrides.setdefault("order_plan", {"type": "market"})
    return _intent(**overrides)


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
        "action": "close_position",
        "order_plan": {"type": "market"},
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
            "authorized_by_id": "reconciled-manage-test",
            "source_message_id": f"reconciled-manage-test-{intent_id}",
        },
    )
    values["order_plan"] = order_plan
    return _Intent(**values)


if __name__ == "__main__":
    unittest.main()
