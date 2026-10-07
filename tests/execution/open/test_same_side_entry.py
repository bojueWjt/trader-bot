"""Independent same-side OPEN keeps a private fill ledger."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from datetime import timedelta

from test_entry_batch_strategy import batch_fixture
from test_intent_execution_strategy_shell import (
    _LiveEntrySubmitStrategy,
    _live_entry_intent,
    _live_zone_ladder_intent,
)
from execution_domain.account_execution_ledger import ReconciledExecutionState
from strategy.intent_execution_planner import (
    ManagementPlan,
    OrderDenied,
    OrderPlan,
    PositionSnapshot,
    encode_client_order_id,
    plan_intent_execution,
)


def _tags(intent):
    return (
        f"intent_id={intent.intent_id}",
        f"parent_intent_id={intent.intent_id}",
        "authorized_by_type=user",
        "authorized_by_id=same-side",
        "source_message_id=same-side-src",
    )


def _entry_plan(intent, sequence=1, order_type="MARKET"):
    return OrderPlan(
        intent_id=intent.intent_id,
        client_order_id=encode_client_order_id(intent.intent_id, sequence),
        tags=_tags(intent),
        instrument_id=intent.instrument_id,
        side="BUY",
        order_type=order_type,
        quantity="1",
        price=None if order_type == "MARKET" else "100",
        time_in_force="IOC" if order_type == "MARKET" else "GTC",
    )


def _strategy(tmp_path):
    return _LiveEntrySubmitStrategy(
        inventory=(("BTCUSDT-PERP.BINANCE", "12000"),),
        state_dir=tmp_path,
    )


def test_market_zone_and_batches_scope_their_own_ids(tmp_path):
    strategy = _strategy(tmp_path)
    market = _live_entry_intent(max_notional="200")
    market.order_plan["stop_loss"] = "80"
    assert strategy._stage_entry_protection(market, _entry_plan(market))
    market_stash = strategy._entry_protection_stash[str(market.intent_id)]
    assert market_stash["batch_entry_ids"] == [encode_client_order_id(market.intent_id, 1)]

    zone = _live_zone_ladder_intent(max_notional="200")
    zone.order_plan["stop_loss"] = "80"
    assert strategy._stage_entry_protection(zone, _entry_plan(zone))
    assert strategy._entry_protection_stash[str(zone.intent_id)]["batch_entry_ids"] == [
        encode_client_order_id(zone.intent_id, seq) for seq in (1, 2, 3)
    ]
    assert str(market.intent_id) in strategy._entry_protection_stash

    for legs in (2, 3):
        batch_strategy, intent, context = batch_fixture(tmp_path)
        if legs == 3:
            intent.order_plan["tranches"].append(
                {"seq": 3, "type": "limit", "quantity": "1", "price": "85", "sizing_price": "85"}
            )
        plans = batch_strategy._zone_ladder_order_plans(
            intent, intent.order_plan, context, "open_position",
        )
        assert isinstance(plans, tuple) and len(plans) == legs
        assert batch_strategy._stage_entry_protection(intent, plans[0])
        stash = batch_strategy._entry_protection_stash[str(intent.intent_id)]
        assert stash["batch_entry_ids"] == [plan.client_order_id for plan in plans]


def test_two_plans_keep_individual_stops_and_manual_residual(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, "open_position")
    second = _live_entry_intent(max_notional="200")
    second.order_plan["stop_loss"] = "70"
    second.instrument_id = intent.instrument_id
    assert strategy._stage_entry_protection(intent, plans[0])
    assert strategy._stage_entry_protection(second, _entry_plan(second))
    strategy._record_batch_fill(SimpleNamespace(
        client_order_id=plans[0].client_order_id, last_qty="1", trade_id="e1",
    ))
    own_stop = encode_client_order_id(intent.intent_id, 11)
    sibling_stop = encode_client_order_id(second.intent_id, 11)
    manual = SimpleNamespace(client_order_id="aos_manual", status="ACCEPTED")
    user_stop = SimpleNamespace(client_order_id="stToAg_hand", status="ACCEPTED")
    orders = (
        SimpleNamespace(client_order_id=own_stop, status="ACCEPTED", tags=()),
        SimpleNamespace(client_order_id=sibling_stop, status="ACCEPTED", tags=()),
        manual,
        user_stop,
        SimpleNamespace(client_order_id=plans[1].client_order_id, status="ACCEPTED"),
    )
    with patch.object(strategy, "_cache_orders_all", return_value=orders):
        live = strategy._live_protection_orders(intent.instrument_id, str(intent.intent_id), 11)
        assert [order.client_order_id for order in live] == [own_stop]
        first = strategy._entry_protection_stash[str(intent.intent_id)]
        first["batch_exit_ids"] = [own_stop]
        first["batch_closing"] = True
        cancelled = []
        with patch.object(strategy, "_cancel_order_object", side_effect=lambda order: cancelled.append(order.client_order_id)):
            strategy._cancel_batch_entries(first)
        assert cancelled == [plans[1].client_order_id]
    assert strategy._protection_quantity(
        strategy._entry_protection_stash[str(second.intent_id)],
        {"quantity": "6"},
    ) == "0"
    assert strategy._protection_quantity(first, {"quantity": "6"}) == "1"


def test_zero_owned_close_denies_even_if_venue_is_known_open(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, "open_position")
    strategy._stage_entry_protection(intent, plans[0])
    now = strategy._now()
    reconciled = ReconciledExecutionState.build(
        account_id=intent.account_id,
        venue_snapshot={
            "positions": [{
                "symbol": "BTCUSDT",
                "position_amt": "6",
                "position_side": "LONG",
            }],
            "open_orders": [],
            "algo_orders": [],
        },
        venue_fetched_at=now - timedelta(seconds=5),
        cache_positions=(),
        now=now,
    )
    management = _live_zone_ladder_intent(max_notional="200")
    management.action = "close_position"
    management.order_plan = {
        "type": "market",
        "position_side": "LONG",
        "authorization": {
            "authorized_by_type": "user",
            "authorized_by_id": "same-side",
            "source_message_id": "close-zero",
            "parent_intent_id": str(intent.intent_id),
        },
    }
    occupied = replace(
        context,
        position=PositionSnapshot(intent.instrument_id, "LONG", "6", f"{intent.instrument_id}-LONG"),
        positions=(PositionSnapshot(intent.instrument_id, "LONG", "6", f"{intent.instrument_id}-LONG"),),
        reconciled_state=reconciled,
    )
    narrowed = strategy._batch_management_context(management, occupied)
    result = plan_intent_execution(management, narrowed)
    assert isinstance(result, OrderDenied)
    assert result.reason == "position_required"


def test_no_stop_entry_still_gets_its_own_ledger(tmp_path):
    strategy = _strategy(tmp_path)
    intent = _live_entry_intent(max_notional="200")
    assert "stop_loss" not in intent.order_plan
    assert strategy._stage_entry_protection(intent, _entry_plan(intent))
    stash = strategy._entry_protection_stash[str(intent.intent_id)]
    assert stash["batch_entry_ids"] == [encode_client_order_id(intent.intent_id, 1)]
    assert stash["batch_fills"] == {}
    assert stash["stop_loss"] is None


def test_two_filled_plans_sync_own_stops_and_close_leaves_the_other(tmp_path):
    strategy, first, context = batch_fixture(tmp_path)
    first_plans = strategy._zone_ladder_order_plans(first, first.order_plan, context, "open_position")
    second = _live_entry_intent(max_notional="200")
    second.order_plan["type"] = "market"
    second.order_plan["stop_loss"] = "70"
    second.order_plan.pop("price", None)
    assert strategy._stage_entry_protection(first, first_plans[0])
    assert strategy._stage_entry_protection(second, _entry_plan(second))
    strategy._record_batch_fill(SimpleNamespace(
        client_order_id=first_plans[0].client_order_id, last_qty="1", trade_id="first-fill",
    ))
    strategy._record_batch_fill(SimpleNamespace(
        client_order_id=encode_client_order_id(second.intent_id, 1),
        last_qty="0.25",
        trade_id="second-fill",
    ))
    manual = SimpleNamespace(client_order_id="aos_manual", status="ACCEPTED", tags=())
    hand = SimpleNamespace(client_order_id="stToAg_hand", status="ACCEPTED", tags=())
    cache = [manual, hand]
    stops = []

    def capture(*_args, **kwargs):
        continuation = kwargs.get("continuation") or {}
        for plan in continuation.get("plans") or ():
            if plan.order_type == "STOP_MARKET":
                stops.append(plan)
        return True

    position = {
        "quantity": "10",
        "side": "LONG",
        "entry_price": "100",
        "id": f"{first.instrument_id}-LONG",
    }
    with patch.object(strategy, "_has_authorized_protection_parent", return_value=True), patch.object(
        strategy, "_protection_position", return_value=position,
    ), patch.object(strategy, "_cache_orders_all", return_value=tuple(cache)), patch.object(
        strategy, "_queue_entry_protection_stash_persist", side_effect=capture,
    ), patch.object(strategy, "_cancel_order_object") as cancel:
        strategy._sync_protection(str(first.intent_id))
        strategy._sync_protection(str(second.intent_id))
        cancel.assert_not_called()
    by_trigger = {plan.trigger_price: plan for plan in stops}
    assert Decimal(by_trigger["80.00"].quantity) == Decimal("1")
    assert Decimal(by_trigger["70.00"].quantity) == Decimal("0.25")
    assert {plan.instrument_id for plan in stops} == {first.instrument_id}

    for plan in by_trigger.values():
        cache.append(SimpleNamespace(
            client_order_id=plan.client_order_id,
            status="ACCEPTED",
            tags=plan.tags,
            instrument_id=plan.instrument_id,
        ))
    cancelled = []

    def cancel_order(order):
        cancelled.append(str(order.client_order_id))
        order.status = "CANCELED"
        return True

    with patch.object(strategy, "_cache_orders_all", side_effect=lambda *_a, **_k: tuple(cache)), patch.object(
        strategy, "_has_authorized_protection_parent", return_value=True,
    ), patch.object(strategy, "_protection_position", return_value=position), patch.object(
        strategy, "_cancel_order_object", side_effect=cancel_order,
    ), patch.object(strategy, "_queue_entry_protection_stash_persist", return_value=True):
        strategy._record_batch_fill(SimpleNamespace(
            client_order_id=encode_client_order_id(first.intent_id, 11),
            last_qty="1",
            trade_id="first-exit",
        ))
        strategy._sync_protection(str(first.intent_id))
    assert by_trigger["80.00"].client_order_id in cancelled
    assert by_trigger["70.00"].client_order_id not in cancelled
    assert "aos_manual" not in cancelled
    assert "stToAg_hand" not in cancelled
    assert strategy._entry_protection_stash[str(second.intent_id)]["stop_loss"] == "70"


def test_legacy_incomplete_evidence_freezes_without_touching_a_lone_stash(tmp_path):
    strategy = _strategy(tmp_path)
    legacy_id = uuid4()
    legacy = {
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "entry_side": "BUY",
        "entry_sequence_max": 1,
        "stop_loss": "80",
        "protection_ids": ("keep-me",),
        "protection_frozen": "revisions_exhausted",
    }
    strategy._entry_protection_stash[str(legacy_id)] = legacy
    assert "legacy_fill_evidence_unresolved" not in legacy
    incoming = _live_entry_intent(max_notional="200")
    incoming.order_plan["stop_loss"] = "70"
    with patch.object(strategy, "_cache_orders_all", return_value=()):
        assert strategy._stage_entry_protection(incoming, _entry_plan(incoming))
    assert legacy["legacy_fill_evidence_unresolved"] is True
    assert legacy["protection_frozen"] == "revisions_exhausted"
    assert legacy["protection_ids"] == ("keep-me",)
    assert "batch_entry_ids" not in legacy
    new_stash = strategy._entry_protection_stash[str(incoming.intent_id)]
    assert new_stash["batch_entry_ids"] == [encode_client_order_id(incoming.intent_id, 1)]
    assert new_stash["batch_fills"] == {}


def test_entry_only_cache_evidence_does_not_migrate_legacy(tmp_path):
    strategy = _strategy(tmp_path)
    legacy_id = uuid4()
    entry_id = encode_client_order_id(legacy_id, 1)
    legacy = {
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "entry_side": "BUY",
        "entry_sequence_max": 1,
        "stop_loss": "80",
        "protection_ids": ("legacy-stop",),
        "protection_frozen": "operator_hold",
    }
    strategy._entry_protection_stash[str(legacy_id)] = legacy
    cached = SimpleNamespace(client_order_id=entry_id, filled_qty="1.5", status="FILLED")
    incoming = _live_entry_intent(max_notional="200")
    incoming.order_plan["stop_loss"] = "70"
    with patch.object(strategy, "_cache_orders_all", return_value=(cached,)):
        assert strategy._stage_entry_protection(incoming, _entry_plan(incoming))
    assert "batch_entry_ids" not in legacy
    assert "batch_fills" not in legacy
    assert legacy["protection_ids"] == ("legacy-stop",)
    assert legacy["protection_frozen"] == "operator_hold"
    assert legacy["legacy_fill_evidence_unresolved"] is True
    assert str(incoming.intent_id) in strategy._entry_protection_stash


def test_restart_preserves_ledger_and_async_close_records_ownership(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, "open_position")
    assert strategy._stage_entry_protection(intent, plans[0])
    strategy._record_batch_fill(SimpleNamespace(
        client_order_id=plans[0].client_order_id, last_qty="1", trade_id="e1",
    ))
    assert strategy._persist_entry_protection_stash()
    restarted = _strategy(tmp_path)
    restarted._entry_protection_stash = restarted._load_entry_protection_stash()
    reloaded = restarted._entry_protection_stash[str(intent.intent_id)]
    assert reloaded["batch_entry_ids"] == [plan.client_order_id for plan in plans]
    assert reloaded["batch_fills"][plans[0].client_order_id]["filled"] == "1"

    exit_id = encode_client_order_id(uuid4(), 11)
    management = ManagementPlan(
        intent_id=uuid4(),
        action="close_position",
        instrument_id=intent.instrument_id,
        target_position_id=None,
        target_position_side="LONG",
        cancel_order_ids=(),
        orders=(OrderPlan(
            intent_id=uuid4(),
            client_order_id=exit_id,
            tags=(),
            instrument_id=intent.instrument_id,
            side="SELL",
            order_type="MARKET",
            quantity="1",
            price=None,
            time_in_force="IOC",
            reduce_only=True,
        ),),
        authorization={
            "authorized_by_type": "user",
            "authorized_by_id": "same-side",
            "source_message_id": "close-src",
            "parent_intent_id": str(intent.intent_id),
        },
    )
    restarted._terminal_exchange_worker = object()
    source = SimpleNamespace(intent_id=management.intent_id, account_id=intent.account_id)
    with patch.object(restarted, "_management_cancel_order_ids", return_value=()), patch.object(
        restarted, "_absorb_management_plan", return_value=True,
    ), patch.object(restarted, "_queue_entry_protection_stash_persist", return_value=False):
        assert restarted._queue_management_plan_after_persist(management, source_intent=source) is False
    rolled_back = restarted._entry_protection_stash[str(intent.intent_id)]
    assert exit_id not in rolled_back.get("batch_exit_ids", [])
    assert "batch_closing" not in rolled_back
    reloaded = rolled_back

    with patch.object(restarted, "_management_cancel_order_ids", return_value=()), patch.object(
        restarted, "_absorb_management_plan", return_value=True,
    ), patch.object(restarted, "_queue_entry_protection_stash_persist", return_value=True), patch.object(
        restarted, "_cancel_batch_entries",
    ) as cancel, patch.object(restarted, "_submit_order_plan", return_value=True):
        assert restarted._queue_management_plan_after_persist(management, source_intent=source) is True
        assert exit_id in reloaded["batch_exit_ids"]
        assert reloaded["batch_closing"] is True
        restarted._continue_management_after_persist({
            "plan": management,
            "source_intent": source,
            "cancel_order_ids": (),
            "disabling_take_profits": False,
        })
        cancel.assert_called_once()
