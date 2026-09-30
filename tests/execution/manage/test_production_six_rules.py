"""Sequences 1 and 2. Copied from the scratchpad close and fraction tests.

Restarted account-d keeps the two entry plans only in the durable inbox.
The cache is empty and the mirror has no entry. Close goes through
_handle_intent and must reduce-only 0.210 without cancelling those plans.

partial_close goes through read_api._execution_order_plan. Fraction stays a
fraction. The node closes 70% of robot-owned quantity, not venue gross.
Do not add quantity to the frozen A-shape plan to dodge a dropped fraction.
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
CONTROL_PLANE_API = REPO_ROOT / "services" / "control-plane" / "api"
MANAGE_TESTS = Path(__file__).resolve().parent
for _path in (
    str(SERVICE_ROOT),
    str(DOMAIN_ROOT),
    str(CONTROL_PLANE_API),
    str(MANAGE_TESTS),
):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import read_api  # noqa: E402
import test_intent_execution_strategy_manage_shell as shell  # noqa: E402
from execution_domain.account_execution_ledger import (  # noqa: E402
    ReconciledExecutionState,
)
from strategy.intent_execution_planner import (  # noqa: E402
    InstrumentSpec,
    ManagementPlan,
    OrderDenied,
    PlannerContext,
    PositionSnapshot,
    encode_client_order_id,
    plan_intent_execution,
)
from strategy.intent_execution_strategy import (  # noqa: E402
    _intent_execution_identity,
    _intent_execution_payload,
)

from test_intent_execution_strategy_manage_shell import (  # noqa: E402
    INSTRUMENT_ID,
    _FreshMirror,
    _HarnessStrategy,
    _RecordingTerminalWorker,
    _intent as _shell_intent,
)


ACCOUNT_D = "account-d"
PLAN_64F425AD = UUID("64f425ad-1111-4111-8111-111111111111")
PLAN_CA73523A = UUID("ca73523a-2222-4222-8222-222222222222")
CLOSE_9A4B8FF9 = UUID("9a4b8ff9-3333-4333-8333-333333333333")

FRACTION_ACCOUNT_ID = "account-a"
FRACTION_POSITION_ID = "P-1"
FRACTION_NOW = datetime(2026, 6, 19, 12, tzinfo=timezone.utc)
REQUEST_SEMANTICS_SHA256 = "a" * 64

# Frozen production A-shape order_plan. Do not reshape it to dodge the
# quantity_or_fraction_required denial that a missing-fraction seam produces.
PRODUCTION_ORDER_PLAN = {
    "side": None,
    "entry": {"type": "market", "time_in_force": "IOC"},
    "fraction": "0.7",
    "leverage": None,
    "principal": {
        "kind": "operator",
        "scope": "global",
        "actor_id": "risk_admin",
        "account_id": None,
    },
    "stop_loss": None,
    "take_profits": [],
    "position_side": "long",
    "request_semantics": {
        "version": "operator-management-v1",
        "sha256": REQUEST_SEMANTICS_SHA256,
    },
}




class _HistoryHarness(_HarnessStrategy):
    def __init__(self, positions=None, orders=None):
        super().__init__(positions=positions, orders=orders)
        self.history: list[Any] = []

    @property
    def cache(self):
        return SimpleNamespace(orders=lambda *a, **k: tuple(self.history))


def _complete_latest_terminal(strategy, *, error="", cancel_outcomes=()):
    request = strategy._terminal_exchange_worker.requests[-1]
    strategy._on_terminal_exchange_result(
        SimpleNamespace(
            request_id=request.request_id,
            account_id=ACCOUNT_D,
            error=error,
            cancel_outcomes=cancel_outcomes,
        )
    )


def _historical_cancel_ids(worker) -> set[str]:
    cancelled: set[str] = set()
    for request in worker.requests:
        if getattr(request, "operation", "") != "cancel_batch":
            continue
        for cancel in getattr(request, "cancel_requests", ()) or ():
            cancelled.add(str(getattr(cancel, "client_order_id", "") or ""))
    return cancelled


def test_account_d_filled_entries_restart_with_empty_cache_close_without_historical_cancels():
    """64f425ad / ca73523a stay in the durable inbox after restart.

    Cache orders are empty and the mirror lists no entry. close 9a4b8ff9
    must submit reduce-only 0.210 through _handle_intent and must not cancel
    the historical entry ids.
    """
    previous_account = shell.ACCOUNT_ID
    shell.ACCOUNT_ID = ACCOUNT_D
    cid_a = encode_client_order_id(PLAN_64F425AD, 1)
    cid_b = encode_client_order_id(PLAN_CA73523A, 1)
    position = SimpleNamespace(
        id=f"{INSTRUMENT_ID}-LONG",
        instrument_id=INSTRUMENT_ID,
        side="LONG",
        quantity="0.210",
        entry_price="80000",
    )
    strategy = _HistoryHarness(positions=[position], orders=[])
    acks = []
    strategy.set_denial_reporter(lambda intent, denial: acks.append((intent, denial)))
    strategy.set_exchange_cancel_adapter(False, _FreshMirror(()))
    worker = _RecordingTerminalWorker()
    strategy.set_terminal_exchange_worker(worker)
    try:
        for owner, cid, qty, confirm in (
            (PLAN_64F425AD, cid_a, "0.100", False),
            (PLAN_CA73523A, cid_b, "0.110", True),
        ):
            open_intent = _shell_intent(
                intent_id=owner,
                action="open_position",
                order_plan={"type": "limit", "side": "buy", "quantity": qty, "price": "80000"},
                target_position_id=None,
            )
            identity = _intent_execution_identity(open_intent)
            strategy._intent_execution_inbox.register_received(
                identity, _intent_execution_payload(open_intent)
            )
            strategy._intent_execution_inbox.begin_dispatch(identity, (cid,))
            if confirm:
                strategy._intent_execution_inbox.mark_exchange_confirmed(identity)
        assert strategy._orders == []
        assert strategy.history == []
        close_intent = _shell_intent(
            intent_id=CLOSE_9A4B8FF9,
            action="close_position",
            order_plan={"type": "market", "position_side": "LONG"},
            target_position_id=f"{INSTRUMENT_ID}-LONG",
        )
        strategy._handle_intent(close_intent)
        _complete_latest_terminal(strategy, error="")
        assert _historical_cancel_ids(worker).isdisjoint({cid_a, cid_b})
        assert len(strategy.submitted_plans) == 1, (strategy.submitted_plans, strategy.denials, acks)
        assert strategy.submitted_plans[0].quantity == "0.210"
        assert strategy.submitted_plans[0].reduce_only
        assert acks == []
    finally:
        shell.ACCOUNT_ID = previous_account
        strategy.on_stop()


def _production_plan(*, fraction: str | float) -> dict:
    plan = dict(PRODUCTION_ORDER_PLAN)
    plan["fraction"] = fraction
    return plan


def _attach_operator_authorization(order_plan: dict) -> dict:
    """Production chain: _order_authorization then copy onto the A-shape plan."""
    attached = dict(order_plan)
    principal = attached["principal"]
    body = {
        "action": "partial_close",
        "account_id": FRACTION_ACCOUNT_ID,
        "symbol": "BTCUSDT",
        "position_side": attached["position_side"],
        "fraction": attached["fraction"],
        "reason": "operator partial close",
    }
    attached["authorization"] = read_api._order_authorization(
        body,
        "unused",
        FRACTION_ACCOUNT_ID,
        "BTCUSDT",
        "operator partial close",
        "operator",
        principal["actor_id"],
        "operator-partial-close-fraction",
        "operator-partial-close-fraction",
    )
    return attached


def _through_execution_order_plan(order_plan: dict) -> dict:
    return read_api._execution_order_plan(
        order_plan,
        {"max_notional": 0.0, "risk_fraction": 0.0, "max_leverage": 1},
        "BTCUSDT",
        action="partial_close",
    )


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


def _fraction_intent(order_plan: dict) -> _Intent:
    intent_id = uuid4()
    return _Intent(
        schema_version="1.0",
        intent_id=intent_id,
        decision_id=uuid4(),
        risk_decision_id=uuid4(),
        account_id=FRACTION_ACCOUNT_ID,
        instrument_id=INSTRUMENT_ID,
        action="partial_close",
        order_plan=order_plan,
        target_position_id=FRACTION_POSITION_ID,
        valid_until=FRACTION_NOW + timedelta(minutes=5),
        idempotency_key=sha256(str(intent_id).encode("ascii")).hexdigest(),
        approved_at=FRACTION_NOW - timedelta(seconds=5),
    )


def _context(*, venue: str, robot: str) -> PlannerContext:
    position = PositionSnapshot(
        instrument_id=INSTRUMENT_ID,
        side="LONG",
        quantity=venue,
        position_id=FRACTION_POSITION_ID,
        entry_price="27123.456",
    )
    symbol = str(position.instrument_id).split("-", 1)[0]
    return PlannerContext(
        account_id=FRACTION_ACCOUNT_ID,
        trading_state="ACTIVE",
        now=FRACTION_NOW,
        instrument=InstrumentSpec(
            instrument_id=INSTRUMENT_ID,
            price_increment="0.01",
            quantity_increment="0.001",
        ),
        position=position,
        positions=(position,),
        existing_orders=(),
        existing_intent_ids=frozenset(),
        reconciled_state=ReconciledExecutionState.build(
            account_id=FRACTION_ACCOUNT_ID,
            venue_snapshot={
                "positions": [
                    {
                        "symbol": symbol,
                        "position_amt": venue,
                        "position_side": position.side,
                    }
                ],
                "open_orders": [],
                "algo_orders": [],
            },
            venue_fetched_at=FRACTION_NOW - timedelta(seconds=5),
            cache_positions=(position,),
            now=FRACTION_NOW,
        ),
        robot_owned_quantity=robot,
    )


def _plan_production_fraction(
    fraction: str | float,
    *,
    venue: str,
    robot: str,
):
    a_shape = _attach_operator_authorization(_production_plan(fraction=fraction))
    assert a_shape["authorization"]["authorized_by_type"] == "user"
    assert a_shape["authorization"]["authorized_by_id"] == "risk_admin"
    # User management deliberately has whole-book scope. Channel management
    # is the existing robot-owned scope; never change planner sizing for R4.
    if venue != robot:
        a_shape["authorization"] = {"authorized_by_type": "channel",
                                    "authorized_by_id": "-1002189417451",
                                    "source_message_id": "tg-sig-c1002189417451-m6925"}
    translated = _through_execution_order_plan(a_shape)
    return plan_intent_execution(
        _fraction_intent(translated),
        _context(venue=venue, robot=robot),
    ), translated


class PartialCloseRobotOwnedFractionTest(unittest.TestCase):
    def _assert_reduces(
        self,
        fraction: str | float,
        venue: str,
        robot: str,
        expected_qty: str,
    ) -> None:
        result, translated = _plan_production_fraction(
            fraction, venue=venue, robot=robot,
        )
        self.assertNotIsInstance(
            result,
            OrderDenied,
            msg=f"planner denied unexpectedly: {result!r}",
        )
        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(translated.get("fraction"), "0.7")
        self.assertNotIn("quantity", translated)
        self.assertEqual(result.orders[0].quantity, expected_qty)
        self.assertTrue(result.orders[0].reduce_only)
        self.assertEqual(result.orders[0].side, "SELL")

    def test_string_fraction_0_7_closes_robot_owned_not_venue_gross(self) -> None:
        self._assert_reduces("0.7", "10.000", "1.000", "0.700")

    def test_number_fraction_0_7_closes_robot_owned_not_venue_gross(self) -> None:
        self._assert_reduces(0.7, "10.000", "1.000", "0.700")

    def test_equal_book_string_fraction_0_7(self) -> None:
        self._assert_reduces("0.7", "0.210", "0.210", "0.147")

    def test_equal_book_number_fraction_0_7(self) -> None:
        self._assert_reduces(0.7, "0.210", "0.210", "0.147")


if __name__ == "__main__":
    unittest.main()


def test_close_cancels_only_current_same_book_robot_entries():
    from unittest.mock import patch
    entry = SimpleNamespace(client_order_id=encode_client_order_id(uuid4(), 1),
        instrument_id=INSTRUMENT_ID, position_side='LONG', side='BUY', reduce_only=False, order_kind='regular')
    orders = [entry]
    for changes in ({'client_order_id': 'aos_manual'}, {'client_order_id': 'stToAg_manual'},
                    {'reduce_only': True}, {'position_side': 'SHORT'}, {'side': 'SELL'}):
        orders.append(SimpleNamespace(**{**vars(entry), **changes, 'client_order_id': changes.get('client_order_id', encode_client_order_id(uuid4(), 1))}))
    strategy = _HistoryHarness(orders=orders)
    try:
        with patch.object(strategy, '_cancel_via_exchange_adapter', return_value=True) as cancel:
            strategy._handle_intent(_shell_intent(action='close_position', order_plan={'type': 'market', 'position_side': 'LONG'}))
        assert [call.args[1] for call in cancel.call_args_list] == [entry.client_order_id]
        assert len(strategy.submitted_plans) == 1
        assert strategy.submitted_plans[0].reduce_only
    finally:
        strategy.on_stop()
