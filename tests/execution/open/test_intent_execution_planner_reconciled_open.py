"""WP-C open-path tests: venue evidence restores the position_exists guard.

Contract: docs/plans/2026-08-28-execution-state-arch-migration.md (WP-C),
softened per the 2026-08-28 operator directive (owner-operated account,
advisory beyond legacy parity). With an empty cache view, entry actions
consult reconciled_state: KNOWN_OPEN -> position_exists (restores the guard
the blind cache lost on 8-26); UNKNOWN/CONFLICTED -> legacy pass-through (no
new blocking while evidence is unavailable); KNOWN_FLAT -> allowed.
reconciled_state=None keeps legacy behavior.
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


ACCOUNT_ID = "account-a"
INSTRUMENT_ID = "ATOMUSDT-PERP.BINANCE"
VENUE_SYMBOL = "ATOMUSDT"
VENUE_QUANTITY = "596.48"
NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
FRESH_FETCHED_AT = NOW - timedelta(seconds=5)
STALE_FETCHED_AT = NOW - timedelta(seconds=120)


class ReconciledStateOpenPlannerTest(unittest.TestCase):
    def test_duplicate_open_denied_when_venue_reports_same_side_position(self) -> None:
        """8-26 regression: cache empty + venue LONG -> open buy denies position_exists."""
        intent = _open_intent(side="buy")

        result = plan_intent_execution(
            intent,
            _empty_cache_context(
                reconciled_state=_reconciled(venue_snapshot=_venue_long_snapshot()),
            ),
        )

        self.assertIsInstance(result, OrderDenied)
        assert isinstance(result, OrderDenied)
        self.assertEqual(result.reason, "position_exists")

    def test_open_proceeds_legacy_when_venue_evidence_is_blind(
        self,
    ) -> None:
        """Owner-operated account: blind evidence never blocks an open —
        stale/missing venue snapshots fall through to legacy behavior."""
        stale = _reconciled(
            venue_snapshot=_venue_long_snapshot(),
            venue_fetched_at=STALE_FETCHED_AT,
        )
        missing = _reconciled(venue_snapshot=None, venue_fetched_at=None)

        for label, reconciled in (("stale", stale), ("missing", missing)):
            with self.subTest(venue=label):
                result = plan_intent_execution(
                    _open_intent(side="buy"),
                    _empty_cache_context(reconciled_state=reconciled),
                )

                self.assertIsInstance(result, OrderPlan)
                assert isinstance(result, OrderPlan)
                self.assertEqual(result.side, "BUY")

    def test_open_allowed_when_reconciled_state_is_known_flat(self) -> None:
        intent = _open_intent(side="buy")

        result = plan_intent_execution(
            intent,
            _empty_cache_context(
                reconciled_state=_reconciled(venue_snapshot=_venue_flat_snapshot()),
            ),
        )

        self.assertIsInstance(result, OrderPlan)
        assert isinstance(result, OrderPlan)
        self.assertEqual(result.side, "BUY")
        self.assertEqual(result.order_type, "MARKET")

    def test_open_opposite_side_book_stays_independent_in_hedge_mode(self) -> None:
        """Venue holds only the LONG book: opening SHORT assesses its own book."""
        intent = _open_intent(side="sell")

        result = plan_intent_execution(
            intent,
            _empty_cache_context(
                reconciled_state=_reconciled(venue_snapshot=_venue_long_snapshot()),
            ),
        )

        self.assertIsInstance(result, OrderPlan)
        assert isinstance(result, OrderPlan)
        self.assertEqual(result.side, "SELL")

    def test_open_with_reconciled_state_none_matches_legacy_behavior(self) -> None:
        """reconciled_state=None + empty cache -> plan is produced exactly as today."""
        intent = _open_intent(side="buy")

        result = plan_intent_execution(
            intent,
            _empty_cache_context(reconciled_state=None),
        )

        self.assertIsInstance(result, OrderPlan)
        assert isinstance(result, OrderPlan)
        self.assertEqual(result.side, "BUY")

    def test_open_cache_fast_path_unchanged_when_cache_holds_position(self) -> None:
        """Non-empty cache view keeps today's position_exists denial untouched."""
        position = PositionSnapshot(
            instrument_id=INSTRUMENT_ID,
            side="LONG",
            quantity=VENUE_QUANTITY,
            position_id=f"{INSTRUMENT_ID}-LONG",
        )
        intent = _open_intent(side="buy")

        result = plan_intent_execution(
            intent,
            _context(
                position=position,
                positions=(position,),
                reconciled_state=_reconciled(venue_snapshot=_venue_flat_snapshot()),
            ),
        )

        self.assertEqual(
            result,
            OrderDenied(reason="position_exists", detail=INSTRUMENT_ID),
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


def _open_intent(*, side: str, **overrides: Any) -> "_Intent":
    overrides.setdefault("action", "open_position")
    overrides.setdefault(
        "order_plan",
        {"type": "market", "side": side, "quantity": "10"},
    )
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
        "action": "open_position",
        "order_plan": {"type": "market", "side": "buy", "quantity": "10"},
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
            "authorized_by_id": "reconciled-open-test",
            "source_message_id": f"reconciled-open-test-{intent_id}",
        },
    )
    values["order_plan"] = order_plan
    return _Intent(**values)


if __name__ == "__main__":
    unittest.main()
