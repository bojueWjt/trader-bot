from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID
import pytest

from test_intent_execution_strategy_shell import (
    _LiveEntrySubmitStrategy, _live_zone_ladder_intent, _normal_live_open_gate,
    _durable_identity, _durable_payload as _base_durable_payload, _pump_durable_until,
)
from strategy.intent_execution_planner import PlannerContext, PositionSnapshot, OrderDenied


def _durable_payload(intent):
    payload = _base_durable_payload(intent)
    payload['risk_budget'] = vars(intent.risk_budget).copy()
    return payload


def batch_fixture(tmp_path):
    strategy = _LiveEntrySubmitStrategy(
        inventory=(('BTCUSDT-PERP.BINANCE', '12000'),), state_dir=tmp_path,
    )
    strategy.set_live_open_gate_getter(_normal_live_open_gate)
    intent = _live_zone_ladder_intent(max_notional='200')
    intent.order_plan.update({
        'type': 'entry_batch', 'batch_version': '1', 'stop_loss': '80',
        'estimated_stop_risk': '31.1111111111',
        'tranches': [
            {'seq': 1, 'type': 'market', 'quantity': '1', 'sizing_price': '100'},
            {'seq': 2, 'type': 'limit', 'quantity': '1.11', 'price': '90', 'sizing_price': '90'},
        ],
    })
    context = PlannerContext(
        account_id=intent.account_id, trading_state='ACTIVE', now=strategy._now(),
        instrument=strategy._instrument_spec(intent.instrument_id), position=None,
        existing_intent_ids=frozenset(),
    )
    return strategy, intent, context


def test_batch_maps_exact_two_legs_and_rejects_existing_position(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, 'open_position')
    assert isinstance(plans, tuple)
    assert [plan.order_type for plan in plans] == ['MARKET', 'LIMIT']
    assert plans[1].price == '90.00'
    occupied = replace(context, position=PositionSnapshot(intent.instrument_id, 'LONG', '5'))
    denial = strategy._zone_ladder_order_plans(intent, intent.order_plan, occupied, 'open_position')
    assert isinstance(denial, OrderDenied)
    assert denial.reason == 'position_exists'


def test_batch_budget_handles_market_and_keeps_total_cap(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, 'open_position')
    assert isinstance(plans, tuple)
    assert strategy._zone_ladder_dynamic_budget_denial(plans) is None
    strategy._mark_price = '110'
    assert strategy._zone_ladder_dynamic_budget_denial(plans).reason == 'approved_max_notional_exceeded'


def test_batch_protects_only_owned_fill_quantity(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, 'open_position')
    assert isinstance(plans, tuple)
    assert strategy._stage_entry_protection(intent, plans[0])
    stash = strategy._entry_protection_stash[str(intent.intent_id)]
    event = SimpleNamespace(client_order_id=plans[0].client_order_id,
                            last_qty='1', trade_id='123')
    strategy._record_batch_fill(event)
    strategy._record_batch_fill(event)
    assert strategy._protection_quantity(stash, {'quantity': '6'}) == '1'


def test_flat_batch_keeps_context_until_remaining_entry_resolved(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, 'open_position')
    assert isinstance(plans, tuple)
    assert strategy._stage_entry_protection(intent, plans[0])
    with patch.object(strategy, '_has_authorized_protection_parent', return_value=True), patch.object(
        strategy, '_schedule_protection_sync', return_value=None,
    ):
        strategy._sync_protection(str(intent.intent_id))
    assert str(intent.intent_id) in strategy._entry_protection_stash


def test_partial_dispatch_restart_never_resubmits_unknown_second_leg(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, 'open_position')
    assert isinstance(plans, tuple)
    identity = _durable_identity(intent)
    strategy._intent_execution_inbox.register_received(identity, _durable_payload(intent))
    strategy._intent_execution_inbox.begin_dispatch(identity, tuple(p.client_order_id for p in plans))
    assert strategy._stash_entry_protection(intent, plans[0])
    restarted, _, _ = batch_fixture(tmp_path)
    restarted._entry_protection_stash = restarted._load_entry_protection_stash()
    # Only first leg is visible. Missing second may have been sent before crash.
    with patch.object(restarted, '_client_order_id_exists', side_effect=lambda instrument, cid: cid == plans[0].client_order_id):
        restarted._handle_zone_ladder(intent, intent.order_plan, context, 'open_position', intent_execution=identity)
    assert restarted.submitted_orders == []
    assert restarted.denials[-1].reason == 'intent_exchange_confirmation_required'
    assert str(intent.intent_id) in restarted._entry_protection_stash


def test_durable_batch_submits_both_and_replay_does_not_duplicate(tmp_path):
    strategy, intent, _ = batch_fixture(tmp_path)
    intent.order_plan['rollout_phase'] = 'fleet_complete'
    intent.order_plan['live_open_gate'] = _normal_live_open_gate()
    strategy._intent_execution_inbox.register_received(_durable_identity(intent), _durable_payload(intent))
    try:
        strategy._handle_intent_ready(intent, exchange_state_ready=False, durable_async=True)
        assert _pump_durable_until(strategy, lambda: len(strategy.submitted_orders) == 2, timeout=1)
        assert len(strategy.built_orders) == 2
        strategy._handle_intent_ready(intent, exchange_state_ready=False, durable_async=True)
        _pump_durable_until(strategy, lambda: not strategy._durable_entry_prepare_active, timeout=1)
        assert len(strategy.submitted_orders) == 2
    finally:
        strategy.on_stop()


def test_close_before_second_fill_cancels_only_own_entry_and_protects_late_fill(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, 'open_position')
    assert isinstance(plans, tuple)
    strategy._stage_entry_protection(intent, plans[0])
    stash = strategy._entry_protection_stash[str(intent.intent_id)]
    exit_id = f'B{intent.intent_id.hex}11'
    second_order = SimpleNamespace(client_order_id=plans[1].client_order_id, status='ACCEPTED')
    manual = SimpleNamespace(client_order_id='aos_manual', status='ACCEPTED')
    with patch.object(strategy, '_cache_orders_all', return_value=(second_order, manual)), patch.object(
        strategy, '_has_authorized_protection_parent', return_value=True,
    ), patch.object(strategy, '_cancel_order_object', side_effect=lambda order: setattr(order, 'status', 'PENDING_CANCEL')) as cancel:
        strategy._record_batch_fill(SimpleNamespace(client_order_id=plans[0].client_order_id, last_qty='1', trade_id='e1'))
        strategy._record_batch_fill(SimpleNamespace(client_order_id=exit_id, last_qty='1', trade_id='x1'))
        strategy._sync_protection(str(intent.intent_id))
        assert stash['batch_closing']
        cancel.assert_called_once_with(second_order)
        strategy._record_batch_fill(SimpleNamespace(client_order_id=plans[1].client_order_id, last_qty='1.11', trade_id='e2'))
        assert strategy._protection_quantity(stash, {'quantity': '6.11'}) == '1.11'
        assert stash['stop_loss'] == '80'


def test_fill_before_position_cache_update_does_not_close_batch(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, 'open_position')
    strategy._stage_entry_protection(intent, plans[0])
    strategy._record_batch_fill(SimpleNamespace(client_order_id=plans[0].client_order_id, last_qty='1', trade_id='e1'))
    with patch.object(strategy, '_has_authorized_protection_parent', return_value=True):
        strategy._sync_protection(str(intent.intent_id))
    assert not strategy._entry_protection_stash[str(intent.intent_id)].get('batch_closing')


def test_later_unrelated_plan_cannot_replace_batch_context(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, 'open_position')
    assert isinstance(plans, tuple)
    assert strategy._stage_entry_protection(intent, plans[0])
    second = _live_zone_ladder_intent(max_notional='200')
    other_plans = strategy._zone_ladder_order_plans(second, second.order_plan, context, 'open_position')
    assert isinstance(other_plans, tuple)
    assert not strategy._stage_entry_protection(second, other_plans[0])
    assert str(intent.intent_id) in strategy._entry_protection_stash


@pytest.mark.parametrize('halt_after_first', [False, True])
def test_immediate_first_fill_and_halt_between_legs(tmp_path, halt_after_first):
    strategy, intent, _ = batch_fixture(tmp_path)
    intent.order_plan['rollout_phase'] = 'fleet_complete'
    intent.order_plan['live_open_gate'] = _normal_live_open_gate()
    strategy._intent_execution_inbox.register_received(_durable_identity(intent), _durable_payload(intent))
    submit = strategy.submit_order

    def immediate_fill(order, position_id=None):
        submit(order, position_id)
        if len(strategy.submitted_orders) == 1:
            strategy.on_order_filled(SimpleNamespace(
                client_order_id=order.client_order_id, instrument_id=intent.instrument_id,
                last_qty='1', trade_id='instant-first',
            ))
            if halt_after_first:
                strategy.set_trading_state_getter(lambda: 'HALTED')

    try:
        with patch.object(strategy, 'submit_order', side_effect=immediate_fill):
            strategy._handle_intent_ready(intent, exchange_state_ready=False, durable_async=True)
            assert _pump_durable_until(strategy, lambda: bool(strategy.submitted_orders), timeout=1)
            assert len(strategy.submitted_orders) == (1 if halt_after_first else 2)
            stash = strategy._entry_protection_stash[str(intent.intent_id)]
            assert strategy._protection_quantity(stash, {'quantity': '6'}) == '1'
            if halt_after_first:
                assert any(denial.reason == 'trading_not_active' for denial in strategy.denials)
    finally:
        strategy.on_stop()


@pytest.mark.parametrize('field,value', [
    ('stop_loss', '95'), ('stop_loss', 'NaN'),
    ('entry_expires_at', 'invalid'), ('entry_expires_at', '2026-10-01T00:00:00'),
])
def test_runtime_rejects_invalid_batch_protection_or_expiry(tmp_path, field, value):
    strategy, intent, context = batch_fixture(tmp_path)
    intent.order_plan[field] = value
    denial = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, 'open_position')
    assert isinstance(denial, OrderDenied)
    assert strategy.submitted_orders == []


def test_management_context_limits_parent_to_owned_quantity(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, 'open_position')
    strategy._stage_entry_protection(intent, plans[0])
    strategy._record_batch_fill(SimpleNamespace(client_order_id=plans[0].client_order_id,
                                               last_qty='1', trade_id='e1'))
    management = _live_zone_ladder_intent(max_notional='200')
    management.action = 'close_position'
    management.order_plan['authorization']['parent_intent_id'] = str(intent.intent_id)
    context = replace(context, position=PositionSnapshot(intent.instrument_id, 'LONG', '6'))
    narrowed = strategy._batch_management_context(management, context)
    assert narrowed.position.quantity == '1'
    assert context.position.quantity == '6'


def test_second_fill_rebuilds_stop_for_batch_not_combined_position(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, 'open_position')
    strategy._stage_entry_protection(intent, plans[0])
    for index, plan in enumerate(plans):
        strategy._record_batch_fill(SimpleNamespace(client_order_id=plan.client_order_id,
                                                   last_qty=plan.quantity, trade_id=str(index)))
    with patch.object(strategy, '_has_authorized_protection_parent', return_value=True), patch.object(
        strategy, '_protection_position', return_value={'quantity': '7.11', 'entry_price': '95'},
    ), patch.object(strategy, '_queue_entry_protection_stash_persist') as persist:
        strategy._sync_protection(str(intent.intent_id))
    continuation = persist.call_args.kwargs['continuation']
    assert continuation['kind'] == 'protection_submit_revision'
    stops = [plan for plan in continuation['plans'] if plan.order_type == 'STOP_MARKET']
    assert len(stops) == 1
    assert stops[0].quantity == '2.11'
    assert stops[0].trigger_price == '80.00'
