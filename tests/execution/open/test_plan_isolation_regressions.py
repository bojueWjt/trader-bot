"""September incidents: plan ownership survives rejection and node restart."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from test_entry_batch_strategy import batch_fixture, _durable_payload
from test_intent_execution_strategy_shell import _durable_identity, _live_entry_intent, _pump_durable_until
from strategy.intent_execution_planner import OrderPlan, PositionSnapshot, encode_client_order_id


def _plans(strategy, intent, context):
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, 'open_position')
    assert isinstance(plans, tuple)
    assert strategy._stage_entry_protection(intent, plans[0])
    return plans


def _fill(strategy, cid, quantity, trade):
    strategy._record_batch_fill(SimpleNamespace(client_order_id=cid, last_qty=quantity, trade_id=trade))


@pytest.mark.parametrize('kind', ['market', 'limit', 'zone_ladder', 'entry_batch'])
def test_every_new_open_has_private_ledger_even_without_protection(tmp_path, kind):
    strategy, intent, context = batch_fixture(tmp_path)
    intent.order_plan['type'] = kind
    if kind == 'zone_ladder':
        intent.order_plan['tranches'].append({'seq': 3, 'type': 'limit', 'price': '85', 'quantity': '1'})
    plan = OrderPlan(intent_id=intent.intent_id, client_order_id=encode_client_order_id(intent.intent_id, 1),
                     tags=(f'parent_intent_id={intent.intent_id}', 'authorized_by_type=user',
                           'authorized_by_id=test', 'source_message_id=test'),
                     instrument_id=intent.instrument_id, side='BUY', order_type='MARKET', quantity='1', price=None, time_in_force='IOC')
    intent.order_plan.pop('stop_loss')
    assert strategy._stage_entry_protection(intent, plan)
    stash = strategy._entry_protection_stash[str(intent.intent_id)]
    expected = {'market': 1, 'limit': 1, 'zone_ladder': 3, 'entry_batch': 2}[kind]
    assert len(stash['batch_entry_ids']) == expected
    assert strategy._protection_quantity(stash, {'quantity': '0.117'}) == '0'


@pytest.mark.parametrize('filled', [False, True])
def test_account_d_old_stop_survives_three_leg_rejection_and_late_fill(tmp_path, filled):
    strategy, intent, context = batch_fixture(tmp_path)
    old_id = str(uuid4())
    old_stop = SimpleNamespace(client_order_id=encode_client_order_id(UUID(old_id), 11),
                               status='ACCEPTED', tags=(), quantity='0.117', trigger_price='80880.8')
    old = {'instrument_id': intent.instrument_id, 'entry_side': 'BUY', 'stop_loss': '80880.8',
           'protected_quantity': '0.117', 'protection_ids': [old_stop.client_order_id]}
    strategy._entry_protection_stash[old_id] = old
    intent.order_plan.update(type='zone_ladder', stop_loss='83600', tranches=[
        {'seq': i, 'type': 'limit', 'price': price, 'quantity': '0.059'}
        for i, price in enumerate(('85000', '84800', '84660'), 1)])
    plans = _plans(strategy, intent, context)
    own = strategy._entry_protection_stash[str(intent.intent_id)]
    assert old['protection_frozen'] and old['legacy_fill_evidence_unresolved']
    legs = [SimpleNamespace(client_order_id=p.client_order_id, status='ACCEPTED', filled_qty='0', tags=()) for p in plans]
    legs[1].status = 'REJECTED'
    manual = SimpleNamespace(client_order_id='aos_manual', status='ACCEPTED')
    canceled = []
    def cancel(order):
        canceled.append(order.client_order_id)
        order.status = 'CANCELED'
        return True
    with patch.object(strategy, '_cache_orders_all', return_value=(*legs, old_stop, manual)), patch.object(
        strategy, '_cancel_order_object', side_effect=cancel,
    ), patch.object(strategy, '_schedule_protection_sync'), patch.object(strategy, '_queue_entry_protection_stash_persist'):
        strategy.on_order_rejected(SimpleNamespace(client_order_id=plans[1].client_order_id, reason='-2019'))
        assert set(canceled) == {plans[0].client_order_id, plans[2].client_order_id}
        assert strategy._protection_quantity(own, {'quantity': '0.117'}) == '0'
        assert not own.get('protection_ids')
        with patch.object(strategy, '_protection_order_plans') as protective_planner:
            strategy._sync_protection(str(intent.intent_id))
            protective_planner.assert_not_called()
        if filled:
            # Fill raced the cancel acknowledgement; it must still be protected.
            _fill(strategy, plans[0].client_order_id, '0.059', 'first')
            _fill(strategy, plans[2].client_order_id, '0.059', 'third')
            quantity = strategy._protection_quantity(own, {'quantity': '0.235'})
            assert Decimal(quantity) == Decimal('0.118')
            protection_plans = strategy._protection_order_plans(
                intent.intent_id, own, context.instrument,
                PositionSnapshot(intent.instrument_id, 'LONG', '0.235'), quantity,
            )
            assert protection_plans[0].order_type == 'STOP_MARKET'
            assert Decimal(protection_plans[0].quantity) == Decimal('0.118')
            assert Decimal(protection_plans[0].trigger_price) == Decimal('83600')
            assert Decimal(strategy._protection_quantity(own, {'quantity': '0.1'})) == Decimal('0.1')
        assert old_stop.quantity == '0.117' and old_stop.trigger_price == '80880.8'
        assert old_stop.client_order_id not in canceled and manual.client_order_id not in canceled
    assert strategy._persist_entry_protection_stash()
    restarted, _, _ = batch_fixture(tmp_path)
    restarted._entry_protection_stash = restarted._load_entry_protection_stash()
    restored = restarted._entry_protection_stash[str(intent.intent_id)]
    assert restored['batch_closing']
    assert Decimal(restarted._protection_quantity(restored, {'quantity': '0.235'})) == Decimal('0.118' if filled else '0')
    assert restarted._entry_protection_stash[old_id]['stop_loss'] == '80880.8'


def test_late_accept_of_rejected_plan_is_canceled(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = _plans(strategy, intent, context)
    stash = strategy._entry_protection_stash[str(intent.intent_id)]
    stash['batch_closing'] = True
    leg = SimpleNamespace(client_order_id=plans[1].client_order_id, status='ACCEPTED')
    with patch.object(strategy, '_cache_orders_all', return_value=(leg,)), patch.object(
        strategy, '_cancel_order_object', return_value=True,
    ) as cancel, patch.object(strategy, '_schedule_protection_sync'):
        strategy.on_order_accepted(SimpleNamespace(client_order_id=leg.client_order_id))
        cancel.assert_called_once_with(leg)


@pytest.mark.parametrize('expiry', [True, False])
def test_expiry_recovers_from_durable_intent_without_stash(tmp_path, expiry):
    strategy, intent, context = batch_fixture(tmp_path)
    if expiry:
        intent.order_plan['entry_expires_at'] = (strategy._now() - timedelta(days=5)).isoformat()
    plans = strategy._zone_ladder_order_plans(intent, intent.order_plan, context, 'open_position')
    identity = _durable_identity(intent)
    strategy._intent_execution_inbox.register_received(identity, _durable_payload(intent))
    strategy._intent_execution_inbox.begin_dispatch(identity, tuple(p.client_order_id for p in plans))
    restarted, _, _ = batch_fixture(tmp_path)
    assert not restarted._entry_protection_stash
    with patch.object(restarted, '_cancel_scoped_venue_orders') as cancel:
        restarted._check_entry_expiry()
        assert _pump_durable_until(restarted, lambda: not restarted._entry_lifecycle_scan_pending, timeout=1.0)
        if expiry:
            cancel.assert_called_once_with(intent.instrument_id, tuple(p.client_order_id for p in plans), 'entry_expiry', entry=True)
        else:
            cancel.assert_not_called()


def test_net_zero_cleans_only_plan_protections_after_restart(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = _plans(strategy, intent, context)
    stop = encode_client_order_id(intent.intent_id, 11)
    tp = encode_client_order_id(intent.intent_id, 12)
    with patch.object(strategy, '_queue_entry_protection_stash_persist'):
        _fill(strategy, plans[0].client_order_id, '0.118', 'entry')
        _fill(strategy, stop, '0.118', 'stop')
    assert strategy._persist_entry_protection_stash()
    restarted, _, _ = batch_fixture(tmp_path)
    restarted._entry_protection_stash = restarted._load_entry_protection_stash()
    orders = (SimpleNamespace(client_order_id=tp, status='ACCEPTED', tags=()),
              SimpleNamespace(client_order_id=encode_client_order_id(uuid4(), 12), status='ACCEPTED', tags=()),
              SimpleNamespace(client_order_id='stToAg_manual', status='ACCEPTED', tags=()))
    with patch.object(restarted, '_cache_orders_all', return_value=orders), patch.object(
        restarted, '_has_authorized_protection_parent', return_value=True,
    ), patch.object(restarted, '_cancel_order_object', return_value=True) as cancel, patch.object(
        restarted, '_schedule_protection_sync',
    ):
        restarted._sync_protection(str(intent.intent_id))
        cancel.assert_called_once_with(orders[0])


def _raw_orphan_evidence(tmp_path, *, book="LONG", quantity="0", occupied_book=None,
                         snapshot_age=0, refresh=True):
    from runtime.exchange_cancel_adapter import BinanceExchangeEvidenceProvider

    restarted, intent, _ = batch_fixture(tmp_path)
    opposite = "SHORT" if book == "LONG" else "LONG"
    occupied_book = occupied_book or book
    raw_positions = [
        {"symbol": "BTCUSDT", "positionSide": occupied_book, "positionAmt": quantity},
        {"symbol": "BTCUSDT", "positionSide": opposite, "positionAmt": "0.02"},
        {"symbol": "ETHUSDT", "positionSide": book, "positionAmt": "1"},
    ]
    cid = encode_client_order_id(uuid4(), 11)
    raw_stop = dict(symbol="BTCUSDT", positionSide=book, clientAlgoId=cid,
                    algoId=101, reduceOnly=True, orderType="STOP_MARKET", quantity="0.118")
    raw_algos = [
        raw_stop,
        dict(raw_stop, clientAlgoId="aos_manual", algoId=102),
        dict(raw_stop, clientAlgoId="stToAg_manual", algoId=103),
        dict(raw_stop, clientAlgoId=encode_client_order_id(uuid4(), 11), algoId=104, reduceOnly=False),
        dict(raw_stop, clientAlgoId=encode_client_order_id(uuid4(), 11), algoId=105, orderType="LIMIT"),
        dict(raw_stop, clientAlgoId=encode_client_order_id(uuid4(), 11), algoId=106, positionSide=opposite),
    ]
    calls = []

    class RawExchangeTransport:
        def request(self, method, path, params, *, timeout_seconds=None):
            assert method == "GET"
            assert params == {}  # In particular positionRisk is full-account, not symbol-filtered.
            calls.append(path)
            return {
                "/fapi/v2/positionRisk": raw_positions,
                "/fapi/v1/openOrders": [],
                "/fapi/v1/openAlgoOrders": {"orders": raw_algos},
            }[path]

    provider = BinanceExchangeEvidenceProvider(
        transport=RawExchangeTransport(),
        now=lambda: restarted._now() - timedelta(seconds=snapshot_age),
        monotonic=lambda: 1.0,
    )
    restarted.set_exchange_evidence_provider(provider)
    evidence = None
    if refresh:
        # snapshot -> _refresh_evidence -> _position_evidence_rows / _order_evidence_rows.
        # Do not inject normalized snapshots: zero positionAmt rows must disappear.
        evidence = provider.snapshot()
        assert calls == ["/fapi/v2/positionRisk", "/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"]
        assert all(Decimal(row["quantity"]) != 0 for row in evidence["positions"])
    return restarted, intent, cid, evidence


@pytest.mark.parametrize("book", ["LONG", "SHORT"])
@pytest.mark.parametrize("quantity,occupied_book,expected", [
    ("0", None, True),
    ("0.01", None, False),
    ("0.01", "BOTH", False),
])
def test_orphan_cleanup_uses_full_account_parsed_absence(tmp_path, book, quantity, occupied_book, expected):
    restarted, intent, cid, evidence = _raw_orphan_evidence(
        tmp_path, book=book, quantity=quantity, occupied_book=occupied_book,
    )
    assert not restarted._entry_protection_stash  # Same path after a restart without a stash.
    if expected:
        assert not any(row["symbol"] == "BTCUSDT" and row["position_side"] == book
                       for row in evidence["positions"])
    with patch.object(restarted, "_cancel_scoped_venue_orders") as cancel:
        restarted._cleanup_orphan_protections()
        if expected:
            cancel.assert_called_once_with(intent.instrument_id, (cid,), "orphan_protection")
        else:
            cancel.assert_not_called()


@pytest.mark.parametrize("guard", ["stale", "unavailable", "late_fill"])
def test_orphan_cleanup_keeps_evidence_and_late_fill_guards(tmp_path, guard):
    snapshot_age = 0
    if guard == "stale":
        snapshot_age = 31
    elif guard == "late_fill":
        snapshot_age = 1
    restarted, intent, cid, _ = _raw_orphan_evidence(
        tmp_path, snapshot_age=snapshot_age, refresh=guard != "unavailable",
    )
    orders = ()
    if guard == "late_fill":
        orders = (SimpleNamespace(
            client_order_id=encode_client_order_id(uuid4(), 1), status="FILLED",
            ts_last=int(restarted._now().timestamp() * 1e9),
        ),)
    with patch.object(restarted, "_cache_orders_all", return_value=orders), patch.object(
        restarted, "_cancel_scoped_venue_orders",
    ) as cancel:
        restarted._cleanup_orphan_protections()
        cancel.assert_not_called()


def test_parent_management_has_private_position_orders_and_close_barrier(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = _plans(strategy, intent, context)
    sibling_id = uuid4()
    sibling = SimpleNamespace(client_order_id=encode_client_order_id(sibling_id, 1), status='ACCEPTED', side='BUY')
    with patch.object(strategy, '_queue_entry_protection_stash_persist'):
        _fill(strategy, plans[0].client_order_id, '0.118', 'entry')
    management = _live_entry_intent()
    management.action = 'close_position'
    management.order_plan['authorization'] = {
        'authorized_by_type': 'user', 'authorized_by_id': 'test',
        'source_message_id': 'close', 'parent_intent_id': str(intent.intent_id),
    }
    position = PositionSnapshot(intent.instrument_id, 'LONG', '0.235')
    narrowed = strategy._batch_management_context(management, replace(context, position=position, positions=(position,)))
    assert Decimal(narrowed.position.quantity) == Decimal('0.118')
    assert narrowed.reconciled_state is None
    own_order = SimpleNamespace(client_order_id=plans[1].client_order_id, status='ACCEPTED', side='BUY')
    with patch.object(strategy, '_cache_orders_all', return_value=(own_order, sibling)):
        assert strategy._close_entry_ids(intent.instrument_id, 'LONG', set(), str(intent.intent_id)) == {own_order.client_order_id}
    strategy._entry_protection_stash[str(intent.intent_id)]['batch_fills'] = {}
    narrowed = strategy._batch_management_context(management, replace(context, position=position, positions=(position,)))
    assert narrowed.position.quantity == '0'


@pytest.mark.parametrize('persist_accepted', [True, False])
def test_async_management_registers_exits_before_persist_and_rolls_back_on_rejection(tmp_path, persist_accepted):
    from strategy.intent_execution_planner import ManagementPlan
    strategy, intent, context = batch_fixture(tmp_path)
    plans = _plans(strategy, intent, context)
    parent = str(intent.intent_id)
    management_id = uuid4()
    exit_order = replace(plans[0], intent_id=management_id,
                         client_order_id=encode_client_order_id(management_id, 1), side='SELL', reduce_only=True)
    authorization = dict(authorized_by_type='user', authorized_by_id='test',
                         source_message_id='manage', parent_intent_id=parent)
    plan = ManagementPlan(management_id, 'partial_close', intent.instrument_id,
                          f'{intent.instrument_id}-LONG', 'LONG', (), (exit_order,), authorization=authorization)
    strategy._terminal_exchange_worker = SimpleNamespace()
    with patch.object(strategy, '_absorb_management_plan', return_value=True), patch.object(
        strategy, '_management_cancel_order_ids', return_value=(),
    ), patch.object(strategy, '_queue_entry_protection_stash_persist', return_value=persist_accepted) as persist:
        assert strategy._queue_management_plan_after_persist(plan, source_intent=intent) is persist_accepted
        persist.assert_called_once()
    stash = strategy._entry_protection_stash[parent]
    assert (exit_order.client_order_id in stash['batch_exit_ids']) is persist_accepted
    if persist_accepted:
        with patch.object(strategy, '_cache_orders_all', return_value=(SimpleNamespace(
            client_order_id=exit_order.client_order_id, status='ACCEPTED', tags=(),
        ),)):
            assert len(strategy._live_protection_orders(intent.instrument_id, parent, 11)) == 1


def test_synchronous_rejection_stops_later_leg_submission(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    intent.order_plan['type'] = 'zone_ladder'
    intent.order_plan['tranches'].append({'seq': 3, 'type': 'limit', 'price': '85', 'quantity': '0.1'})
    for leg in intent.order_plan['tranches']:
        leg['price'] = leg.get('price', '100')
        leg['quantity'] = '0.1'
    identity = _durable_identity(intent)
    strategy._intent_execution_inbox.register_received(identity, _durable_payload(intent))
    submitted = []
    canceled = []
    def submit(plan, **_kwargs):
        submitted.append(plan)
        if len(submitted) == 2:
            strategy._rollback_rejected_entry(SimpleNamespace(client_order_id=plan.client_order_id), rejected=True)
        return True
    with patch.object(strategy, '_submit_order_plan', side_effect=submit), patch.object(
        strategy, '_cancel_order_by_client_order_id', side_effect=lambda _, cid: canceled.append(cid),
    ), patch.object(strategy, '_schedule_protection_sync'), patch.object(strategy, '_queue_entry_protection_stash_persist'):
        strategy._handle_zone_ladder(intent, intent.order_plan, context, 'open_position', intent_execution=identity)
    assert len(submitted) == 2
    assert submitted[0].client_order_id in canceled
    assert strategy._entry_protection_stash[str(intent.intent_id)]['batch_closing']
    assert not strategy._entry_protection_stash[str(intent.intent_id)]['batch_fills']


def test_delayed_protection_persist_cannot_submit_after_plan_net_zero(tmp_path):
    strategy, intent, context = batch_fixture(tmp_path)
    plans = _plans(strategy, intent, context)
    key = str(intent.intent_id)
    stash = strategy._entry_protection_stash[key]
    stop = replace(plans[0], client_order_id=encode_client_order_id(intent.intent_id, 11),
                   quantity='0.118', reduce_only=True, side='SELL')
    stash['pending_protection_revision'] = {'client_order_ids': (stop.client_order_id,)}
    with patch.object(strategy, '_submit_order_plan') as submit, patch.object(strategy, '_schedule_protection_sync'):
        strategy._continue_protection_revision_submit(dict(intent_key=key, plans=(stop,), quantity='0.118'))
    submit.assert_not_called()
    assert 'pending_protection_revision' not in stash
