from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


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


ACCOUNT_ID = "account-b"
INSTRUMENT_ID = "ALGOUSDT-PERP.BINANCE"
BOOK_ID = f"{INSTRUMENT_ID}-LONG"
NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
FRESH = NOW - timedelta(seconds=5)


class ManageCacheSideMismatchTest(unittest.TestCase):
    def test_move_stop_uses_venue_when_cache_book_id_has_opposite_side(self) -> None:
        inverted = PositionSnapshot(
            instrument_id=INSTRUMENT_ID,
            side="SHORT",
            quantity="2374.2",
            position_id=BOOK_ID,
        )
        intent = _intent(
            action="move_stop_loss",
            target_position_id=BOOK_ID,
            order_plan={
                "stop_price": "0.0934",
                "position_side": "long",
                "authorization": {
                    "authorized_by_type": "user",
                    "authorized_by_id": "manage-mismatch-test",
                    "source_message_id": "manage-mismatch-test-venue",
                },
            },
        )
        result = plan_intent_execution(
            intent,
            _context(
                position=inverted,
                positions=(inverted,),
                reconciled_state=_reconciled(),
            ),
        )
        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.orders[0].side, "SELL")
        self.assertEqual(result.orders[0].quantity, "9497.0")
        self.assertEqual(result.orders[0].trigger_price, "0.09340")
        self.assertEqual(result.target_position_side, "LONG")

    def test_move_stop_still_denies_mismatch_without_venue(self) -> None:
        inverted = PositionSnapshot(
            instrument_id=INSTRUMENT_ID,
            side="SHORT",
            quantity="2374.2",
            position_id=BOOK_ID,
        )
        intent = _intent(
            action="move_stop_loss",
            target_position_id=BOOK_ID,
            order_plan={
                "stop_price": "0.0934",
                "position_side": "long",
                "authorization": {
                    "authorized_by_type": "user",
                    "authorized_by_id": "manage-mismatch-test",
                    "source_message_id": "manage-mismatch-test-none",
                },
            },
        )
        result = plan_intent_execution(
            intent,
            _context(
                position=inverted,
                positions=(inverted,),
                reconciled_state=None,
            ),
        )
        self.assertEqual(
            result,
            OrderDenied(
                "position_side_mismatch",
                "requested=LONG,actual=SHORT",
            ),
        )


def _intent(*, action: str, target_position_id: str, order_plan: dict[str, Any]):
    intent_id = uuid4()
    return type(
        "Intent",
        (),
        {
            "intent_id": intent_id,
            "decision_id": uuid4(),
            "risk_decision_id": uuid4(),
            "idempotency_key": intent_id.hex,
            "account_id": ACCOUNT_ID,
            "instrument_id": INSTRUMENT_ID,
            "action": action,
            "target_position_id": target_position_id,
            "order_plan": order_plan,
            "valid_until": NOW + timedelta(minutes=15),
            "approved_at": NOW,
        },
    )()


def _reconciled() -> ReconciledExecutionState:
    return ReconciledExecutionState.build(
        account_id=ACCOUNT_ID,
        venue_snapshot={
            "positions": [
                {
                    "symbol": "ALGOUSDT",
                    "position_amt": "9497.0",
                    "position_side": "LONG",
                }
            ],
            "open_orders": [],
            "algo_orders": [],
        },
        venue_fetched_at=FRESH,
        cache_positions=(),
        now=NOW,
    )


def _context(**overrides: Any) -> PlannerContext:
    values: dict[str, Any] = {
        "account_id": ACCOUNT_ID,
        "trading_state": "ACTIVE",
        "now": NOW,
        "instrument": InstrumentSpec(
            instrument_id=INSTRUMENT_ID,
            price_increment="0.00001",
            quantity_increment="0.1",
        ),
        "position": None,
        "positions": (),
        "existing_orders": (),
        "existing_intent_ids": frozenset(),
    }
    values.update(overrides)
    return PlannerContext(**values)


if __name__ == "__main__":
    unittest.main()
