from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID, uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from strategy.intent_execution_planner import (  # noqa: E402
    ManagementPlan,
    plan_intent_execution,
)
from strategy.intent_execution_strategy import (  # noqa: E402
    IntentExecutionStrategy,
    IntentExecutionStrategyConfig,
)


ACCOUNT_ID = "account-a"
INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
POSITION_ID = "BTCUSDT-PERP.BINANCE-LONG"
NOW = datetime(2026, 7, 29, 12, 30, tzinfo=timezone.utc)


def _pump_durable(strategy: IntentExecutionStrategy) -> None:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        strategy.drain_durable_io_mailbox()
        snapshot = strategy._durable_io_worker.snapshot()
        if (
            not snapshot.in_flight
            and snapshot.queue_depth == 0
            and strategy._durable_io_mailbox.empty()
        ):
            strategy.drain_durable_io_mailbox()
            return
        time.sleep(0.001)
    strategy.drain_durable_io_mailbox()
    raise AssertionError("durable I/O did not quiesce")


def _sync_protection(
    strategy: IntentExecutionStrategy,
    intent_key: str,
) -> None:
    strategy._sync_protection(intent_key)
    _pump_durable(strategy)


class TakeProfitTombstoneTest(unittest.TestCase):
    def test_concurrent_stash_persists_use_unique_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(Path(state_dir), orders=[])
            _seed_entry_stash(strategy)
            barrier = threading.Barrier(2)
            real_replace = os.replace
            results: list[bool] = []

            def racing_replace(source: str, destination: str) -> None:
                barrier.wait(timeout=5)
                real_replace(source, destination)

            def persist() -> None:
                results.append(strategy._persist_entry_protection_stash())

            with patch(
                "strategy.intent_execution_strategy.os.replace",
                side_effect=racing_replace,
            ):
                threads = [
                    threading.Thread(target=persist),
                    threading.Thread(target=persist),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(results, [True, True])
            reloaded = strategy._load_entry_protection_stash()
            self.assertEqual(
                set(reloaded),
                set(strategy._entry_protection_stash),
            )

    def test_node_cancel_all_requires_user_or_channel_authorization(self) -> None:
        strategy = _Strategy(
            Path(tempfile.mkdtemp()),
            orders=_live_protection_orders(),
        )

        strategy._on_node_command(
            SimpleNamespace(type="cancel_all", args={})
        )

        self.assertEqual(strategy.cancelled_client_order_ids, [])

    def test_authorized_node_cancel_all_executes(self) -> None:
        strategy = _Strategy(
            Path(tempfile.mkdtemp()),
            orders=_live_protection_orders(),
        )

        strategy._on_node_command(
            SimpleNamespace(
                type="cancel_all",
                args={
                    "authorization": {
                        "authorized_by_type": "user",
                        "authorized_by_id": "risk_admin",
                        "source_message_id": "operator-command-42",
                    }
                },
            )
        )

        self.assertTrue(strategy.cancelled_client_order_ids)

    def test_authorized_empty_replace_plans_tp_only_cancellation(self) -> None:
        intent = _intent(
            action="replace_take_profits",
            order_plan={
                "take_profits": [],
                "disable_take_profits": True,
                "authorization": _authorization("user", "balen", "manual-tp-off"),
            },
        )
        strategy = _Strategy(Path(tempfile.mkdtemp()), orders=_live_protection_orders())
        context = strategy._planner_context(intent)

        result = plan_intent_execution(intent, context)

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(result.orders, ())
        self.assertEqual(result.cancel_order_ids, ("old-tp",))
        self.assertEqual(result.authorization["authorized_by_type"], "user")
        self.assertEqual(result.authorization["authorized_by_id"], "balen")

    def test_supplied_parent_is_preserved_for_inherited_management(self) -> None:
        parent_intent_id = uuid4()
        intent = _intent(
            action="replace_take_profits",
            order_plan={
                "take_profits": [],
                "disable_take_profits": True,
                "authorization": {
                    **_authorization("channel", "gauls", "inherited-management"),
                    "parent_intent_id": str(parent_intent_id),
                },
            },
        )
        strategy = _Strategy(Path(tempfile.mkdtemp()), orders=_live_protection_orders())

        result = plan_intent_execution(intent, strategy._planner_context(intent))

        self.assertIsInstance(result, ManagementPlan)
        assert isinstance(result, ManagementPlan)
        self.assertEqual(
            result.authorization["parent_intent_id"],
            str(parent_intent_id),
        )
        self.assertEqual(
            result.authorization["current_intent_id"],
            str(intent.intent_id),
        )

    def test_manual_tp_disable_cancels_tp_preserves_sl_and_repeated_sync_stays_off(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(Path(state_dir), orders=_live_protection_orders())
            entry_intent_id = _seed_entry_stash(strategy)
            disable_intent = _intent(
                action="replace_take_profits",
                order_plan={
                    "take_profits": [],
                    "disable_take_profits": True,
                    "authorization": _authorization("user", "balen", "manual-tp-off"),
                },
            )

            strategy._handle_intent(disable_intent)

            self.assertEqual(strategy.cancelled_client_order_ids, ["old-tp"])
            self.assertEqual(
                [order.client_order_id for order in strategy._orders],
                ["old-stop"],
            )
            stash = strategy._entry_protection_stash[str(entry_intent_id)]
            tombstone = stash["take_profit_tombstone"]
            self.assertEqual(tombstone["authorized_by_type"], "user")
            self.assertEqual(tombstone["authorized_by_id"], "balen")
            self.assertEqual(tombstone["source_message_id"], "manual-tp-off")
            self.assertEqual(tombstone["parent_intent_id"], str(disable_intent.intent_id))

            strategy.submitted_plans.clear()
            for _ in range(3):
                _sync_protection(strategy, str(entry_intent_id))

            self.assertEqual(
                [
                    plan
                    for plan in strategy.submitted_plans
                    if "lifecycle_role=take_profit" in plan.tags
                ],
                [],
            )
            self.assertEqual(
                [order.client_order_id for order in strategy._orders],
                ["old-stop"],
            )

    def test_new_authorized_replace_supersedes_tombstone_and_can_rehang_tp(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(Path(state_dir), orders=_live_protection_orders())
            entry_intent_id = _seed_entry_stash(strategy)
            strategy._handle_intent(
                _intent(
                    action="replace_take_profits",
                    order_plan={
                        "take_profits": [],
                        "disable_take_profits": True,
                        "position_side": "long",
                        "authorization": _authorization(
                            "user",
                            "balen",
                            "manual-tp-off",
                        ),
                    },
                )
            )
            replace_intent = _intent(
                action="replace_take_profits",
                order_plan={
                    "take_profits": [
                        {"price": "28500", "quantity": "0.2"},
                        {"price": "29500", "quantity": "0.3"},
                    ],
                    "authorization": _authorization(
                        "channel",
                        "gauls",
                        "channel-message-7788",
                    ),
                },
            )

            strategy._handle_intent(replace_intent)

            stash = strategy._entry_protection_stash[str(entry_intent_id)]
            self.assertNotIn("take_profit_tombstone", stash)
            self.assertEqual(
                stash["take_profit_parent_intent_id"],
                str(replace_intent.intent_id),
            )
            superseded = stash["last_take_profit_tombstone"]
            self.assertEqual(
                superseded["superseded_by_intent_id"],
                str(replace_intent.intent_id),
            )
            self.assertEqual(
                stash["take_profit_authorization"]["authorized_by_type"],
                "channel",
            )

            strategy._orders = [strategy._orders[0]]
            strategy.submitted_plans.clear()
            _sync_protection(strategy, str(entry_intent_id))

            tp_plans = [
                plan
                for plan in strategy.submitted_plans
                if "lifecycle_role=take_profit" in plan.tags
            ]
            self.assertEqual(
                [plan.order_type for plan in tp_plans],
                ["MARKET_IF_TOUCHED", "MARKET_IF_TOUCHED"],
            )
            self.assertEqual(
                [plan.trigger_price for plan in tp_plans],
                ["28500.00", "29500.00"],
            )
            self.assertTrue(all(plan.price is None for plan in tp_plans))
            self.assertTrue(
                all(
                    f"parent_intent_id={replace_intent.intent_id}" in plan.tags
                    for plan in tp_plans
                )
            )
            self.assertTrue(
                all("authorized_by_type=channel" in plan.tags for plan in tp_plans)
            )
            self.assertTrue(
                all("authorized_by_id=gauls" in plan.tags for plan in tp_plans)
            )

    def test_tombstone_survives_restart_and_state_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            strategy = _Strategy(state_path, orders=_live_protection_orders())
            entry_intent_id = _seed_entry_stash(strategy)
            strategy._handle_intent(
                _intent(
                    action="replace_take_profits",
                    order_plan={
                        "take_profits": [],
                        "disable_take_profits": True,
                        "authorization": _authorization(
                            "user",
                            "balen",
                            "manual-tp-off",
                        ),
                    },
                )
            )

            restarted = _Strategy(state_path, orders=[_live_protection_orders()[0]])
            restarted._entry_protection_stash = restarted._load_entry_protection_stash()
            _sync_protection(restarted, str(entry_intent_id))
            _sync_protection(restarted, str(entry_intent_id))

            stash = restarted._entry_protection_stash[str(entry_intent_id)]
            self.assertIn("take_profit_tombstone", stash)
            self.assertEqual(
                [
                    plan
                    for plan in restarted.submitted_plans
                    if "lifecycle_role=take_profit" in plan.tags
                ],
                [],
            )

    def test_tombstone_stays_pending_until_exchange_cancel_reaches_terminal_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            strategy = _Strategy(
                state_path,
                orders=_live_protection_orders(),
            )
            entry_intent_id = _seed_entry_stash(strategy)
            mirror = _StaticMirror(
                tuple(_mirror_from_local_order(order) for order in strategy._orders)
            )
            adapter = _RecordingCancelAdapter(fail_cancel=True)
            strategy.set_exchange_cancel_adapter(adapter, mirror)
            disable_intent = _intent(
                action="replace_take_profits",
                order_plan={
                    "take_profits": [],
                    "disable_take_profits": True,
                    "authorization": _authorization(
                        "user",
                        "balen",
                        "manual-tp-off",
                    ),
                },
            )

            runtime_package = ModuleType("runtime")
            runtime_package.__path__ = []
            exchange_cancel_module = _load_exchange_cancel_adapter_module()
            with patch.dict(
                sys.modules,
                {
                    "runtime": runtime_package,
                    "runtime.exchange_cancel_adapter": exchange_cancel_module,
                },
            ):
                strategy._handle_intent(disable_intent)

            restarted = _Strategy(state_path, orders=_live_protection_orders())
            restarted._entry_protection_stash = restarted._load_entry_protection_stash()
            tombstone = restarted._entry_protection_stash[str(entry_intent_id)][
                "take_profit_tombstone"
            ]
            self.assertEqual(
                tombstone["parent_intent_id"],
                str(disable_intent.intent_id),
            )
            self.assertEqual(tombstone["state"], "cancel_pending")
            self.assertEqual(strategy.denials[-1].reason, "order_cancel_failed")
            self.assertEqual(
                [
                    plan
                    for plan in restarted._protection_order_plans(
                        entry_intent_id,
                        restarted._entry_protection_stash[str(entry_intent_id)],
                        restarted._instrument_spec(INSTRUMENT_ID),
                        _position(),
                        "0.500",
                    )
                    if "lifecycle_role=take_profit" in plan.tags
                ],
                [],
            )

            adapter.fail_cancel = False
            with patch.dict(
                sys.modules,
                {
                    "runtime": runtime_package,
                    "runtime.exchange_cancel_adapter": exchange_cancel_module,
                },
            ):
                strategy._handle_intent(disable_intent)

            finalized = strategy._entry_protection_stash[str(entry_intent_id)][
                "take_profit_tombstone"
            ]
            self.assertEqual(finalized["state"], "disabled")
            self.assertIn("completed_at", finalized)

    def test_internal_sync_without_parent_intent_records_denial_only(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(Path(state_dir), orders=[])
            entry_intent_id = uuid4()
            strategy._entry_protection_stash[str(entry_intent_id)] = {
                "stop_loss": None,
                "take_profits": ("28000",),
                "instrument_id": INSTRUMENT_ID,
                "entry_side": "BUY",
                "entry_tags": (),
                "entry_sequence_max": 1,
                "protection_sequence_start": 11,
                "protection_roles": {},
                "tp_consumed": {},
                "pending_cancel_ids": (),
            }

            _sync_protection(strategy, str(entry_intent_id))

            self.assertEqual(strategy.submitted_plans, [])
            self.assertEqual(strategy.denials[-1].reason, "protection_parent_intent_missing")

    def test_internal_sync_with_uuid_but_missing_source_is_inert(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(Path(state_dir), orders=_live_protection_orders())
            entry_intent_id = uuid4()
            strategy._entry_protection_stash[str(entry_intent_id)] = {
                "stop_loss": "25000",
                "take_profits": ("64635.3",),
                "instrument_id": INSTRUMENT_ID,
                "entry_side": "BUY",
                "entry_tags": (f"intent_id={entry_intent_id}",),
                "stop_loss_parent_intent_id": str(entry_intent_id),
                "take_profit_parent_intent_id": str(entry_intent_id),
                "entry_sequence_max": 1,
                "protection_sequence_start": 11,
                "protection_roles": {},
                "tp_consumed": {},
                "pending_cancel_ids": (),
            }

            _sync_protection(strategy, str(entry_intent_id))

            self.assertEqual(strategy.submitted_plans, [])
            self.assertEqual(strategy.cancelled_client_order_ids, [])
            self.assertEqual(
                strategy.denials[-1].reason,
                "protection_parent_intent_missing",
            )

    def test_empty_replace_without_explicit_disable_is_denied_without_side_effects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(
                Path(state_dir),
                orders=_live_protection_orders(),
            )
            _seed_entry_stash(strategy)
            intent = _intent(
                action="replace_take_profits",
                order_plan={
                    "take_profits": [],
                    "authorization": _authorization(
                        "user",
                        "balen",
                        "missing-explicit-disable",
                    ),
                },
            )

            strategy._handle_intent(intent)

            self.assertEqual(strategy.submitted_plans, [])
            self.assertEqual(strategy.cancelled_client_order_ids, [])
            self.assertEqual(
                strategy.denials[-1].reason,
                "unsupported_order_spec",
            )

    def test_empty_dict_tombstone_is_inert(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(
                Path(state_dir),
                orders=_live_protection_orders(),
            )
            entry_intent_id = _seed_entry_stash(strategy)
            stash = strategy._entry_protection_stash[str(entry_intent_id)]
            stash["take_profit_tombstone"] = {}

            _sync_protection(strategy, str(entry_intent_id))

            self.assertEqual(strategy.submitted_plans, [])
            self.assertEqual(strategy.cancelled_client_order_ids, [])
            self.assertEqual(
                strategy.denials[-1].reason,
                "take_profit_tombstone_invalid",
            )

    def test_tombstone_persist_failure_prevents_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            strategy = _Strategy(
                state_path,
                orders=_live_protection_orders(),
            )
            entry_intent_id = _seed_entry_stash(strategy)
            stash_before = dict(
                strategy._entry_protection_stash[str(entry_intent_id)]
            )
            disable_intent = _intent(
                action="replace_take_profits",
                order_plan={
                    "take_profits": [],
                    "disable_take_profits": True,
                    "authorization": _authorization(
                        "user",
                        "balen",
                        "persist-failure",
                    ),
                },
            )

            with patch(
                "strategy.intent_execution_strategy.os.replace",
                side_effect=_replace_failure_for(
                    strategy._protection_stash_path()
                ),
            ):
                strategy._handle_intent(disable_intent)

            self.assertEqual(strategy.submitted_plans, [])
            self.assertEqual(strategy.cancelled_client_order_ids, [])
            self.assertEqual(
                strategy.denials[-1].reason,
                "protection_stash_persist_failed",
            )
            self.assertEqual(
                strategy._entry_protection_stash[str(entry_intent_id)],
                stash_before,
            )
            restarted = _Strategy(
                state_path,
                orders=_live_protection_orders(),
            )
            restarted._entry_protection_stash = (
                restarted._load_entry_protection_stash()
            )
            self.assertNotIn(
                "take_profit_tombstone",
                restarted._entry_protection_stash[str(entry_intent_id)],
            )

    def test_new_entry_submit_failure_restores_previous_owner(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            strategy = _Strategy(state_path, orders=[])
            old_owner_id = _seed_entry_stash(strategy)
            old_stash = dict(
                strategy._entry_protection_stash[str(old_owner_id)]
            )
            new_entry = _intent(
                action="open_position",
                order_plan={
                    "side": "buy",
                    "type": "limit",
                    "price": "26900",
                    "quantity": "0.1",
                    "stop_loss": "24900",
                    "take_profits": ["28100"],
                    "authorization": _authorization(
                        "channel",
                        "m3939",
                        "new-entry-submit-failure",
                    ),
                },
                target_position_id=None,
            )
            strategy._submit_order_plan = lambda _plan: False

            strategy._handle_intent(new_entry)

            self.assertIn(
                str(old_owner_id),
                strategy._entry_protection_stash,
            )
            self.assertEqual(
                strategy._entry_protection_stash[str(old_owner_id)],
                old_stash,
            )
            self.assertNotIn(
                str(new_entry.intent_id),
                strategy._entry_protection_stash,
            )

    def test_entry_stash_persist_failure_prevents_entry_submit(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(Path(state_dir), orders=[])
            strategy._positions = []
            entry = _intent(
                action="open_position",
                order_plan={
                    "side": "buy",
                    "type": "limit",
                    "price": "26900",
                    "quantity": "0.1",
                    "stop_loss": "24900",
                    "take_profits": ["28100"],
                    "authorization": _authorization(
                        "channel",
                        "m3939",
                        "entry-stash-persist-failure",
                    ),
                },
                target_position_id=None,
            )

            with patch(
                "strategy.intent_execution_strategy.os.replace",
                side_effect=_replace_failure_for(
                    strategy._protection_stash_path()
                ),
            ):
                strategy._handle_intent(entry)

            self.assertEqual(strategy.submitted_plans, [])
            self.assertNotIn(
                str(entry.intent_id),
                strategy._entry_protection_stash,
            )
            self.assertEqual(
                strategy.denials[-1].reason,
                "protection_stash_persist_failed",
            )

    def test_replace_cancel_failure_keeps_new_targets_in_stash(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            strategy = _Strategy(
                state_path,
                orders=_live_protection_orders(),
                fail_cancel=True,
            )
            entry_intent_id = _seed_entry_stash(strategy)
            replace_intent = _intent(
                action="replace_take_profits",
                order_plan={
                    "take_profits": [
                        {"price": "28500", "quantity": "0.2"},
                        {"price": "29500", "quantity": "0.3"},
                    ],
                    "authorization": _authorization(
                        "channel",
                        "gauls",
                        "replace-cancel-failure",
                    ),
                },
            )

            strategy._handle_intent(replace_intent)

            stash = strategy._entry_protection_stash[str(entry_intent_id)]
            self.assertEqual(
                stash["take_profits"],
                ("28500.00", "29500.00"),
            )
            self.assertEqual(strategy.denials[-1].reason, "order_cancel_failed")
            restarted = _Strategy(
                state_path,
                orders=_live_protection_orders(),
            )
            restarted._entry_protection_stash = (
                restarted._load_entry_protection_stash()
            )
            self.assertEqual(
                restarted._entry_protection_stash[str(entry_intent_id)][
                    "take_profits"
                ],
                ("28500.00", "29500.00"),
            )

    def test_incomplete_tombstone_authorization_is_inert(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(Path(state_dir), orders=_live_protection_orders())
            entry_intent_id = _seed_entry_stash(strategy)
            stash = strategy._entry_protection_stash[str(entry_intent_id)]
            stash["take_profit_tombstone"] = {
                "state": "disabled",
                "parent_intent_id": str(entry_intent_id),
            }
            stash["take_profit_authorization"] = {
                "authorized_by_type": "channel",
                "parent_intent_id": str(entry_intent_id),
            }

            _sync_protection(strategy, str(entry_intent_id))

            self.assertEqual(strategy.submitted_plans, [])
            self.assertEqual(strategy.cancelled_client_order_ids, [])
            self.assertEqual(
                strategy.denials[-1].reason,
                "take_profit_tombstone_invalid",
            )

    def test_mu_legacy_stale_owner_is_inert_and_new_message_terminates_it(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(
                Path(state_dir),
                orders=[
                    SimpleNamespace(
                        client_order_id="mu-old-stop",
                        instrument_id="MUUSDT-PERP.BINANCE",
                        order_type="STOP_MARKET",
                        side="SELL",
                        quantity="100",
                        trigger_price="770",
                        reduce_only=True,
                        status="ACCEPTED",
                        tags=(
                            "position_id=MUUSDT-PERP.BINANCE-LONG",
                            "lifecycle_role=stop_loss",
                        ),
                    )
                ],
            )
            strategy._positions = [
                SimpleNamespace(
                    id="MUUSDT-PERP.BINANCE-LONG",
                    instrument_id="MUUSDT-PERP.BINANCE",
                    side="LONG",
                    quantity="100",
                    entry_price="800",
                )
            ]
            legacy_intent_id = UUID("ffb94551-0000-4000-8000-000000000001")
            strategy._entry_protection_stash[str(legacy_intent_id)] = {
                "stop_loss": "770",
                "take_profits": (),
                "instrument_id": "MUUSDT-PERP.BINANCE",
                "entry_side": "BUY",
                "entry_tags": (
                    f"intent_id={legacy_intent_id}",
                    "action=open_position",
                    f"account_id={ACCOUNT_ID}",
                ),
                "entry_sequence_max": 1,
                "protection_sequence_start": 11,
                "protection_roles": {},
                "tp_consumed": {},
                "pending_cancel_ids": (),
            }

            _sync_protection(strategy, str(legacy_intent_id))

            self.assertEqual(strategy.submitted_plans, [])
            self.assertEqual(strategy.cancelled_client_order_ids, [])
            self.assertEqual(
                strategy.denials[-1].reason,
                "protection_parent_intent_missing",
            )

            new_entry = _intent(
                instrument_id="MUUSDT-PERP.BINANCE",
                action="open_position",
                order_plan={
                    "side": "buy",
                    "type": "limit",
                    "price": "790",
                    "quantity": "100",
                    "authorization": _authorization(
                        "channel",
                        "m3939",
                        "m3939-20260728-entry",
                    ),
                },
                target_position_id=None,
            )
            plan = SimpleNamespace(
                intent_id=new_entry.intent_id,
                instrument_id="MUUSDT-PERP.BINANCE",
                side="BUY",
                tags=_entry_authorization_tags(new_entry),
            )

            strategy._stash_entry_protection(new_entry, plan)

            self.assertNotIn(
                str(legacy_intent_id),
                strategy._entry_protection_stash,
            )
            self.assertEqual(strategy.submitted_plans, [])
            self.assertEqual(strategy.cancelled_client_order_ids, [])

    def test_same_source_message_entry_without_protection_keeps_shared_owner(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(Path(state_dir), orders=[])
            first = _intent(
                action="open_position",
                order_plan={
                    "side": "buy",
                    "type": "limit",
                    "price": "27000",
                    "quantity": "0.25",
                    "stop_loss": "25000",
                    "take_profits": ["28000"],
                    "authorization": _authorization(
                        "channel",
                        "m3939",
                        "m3939-shared-entry",
                    ),
                },
                target_position_id=None,
            )
            first_plan = SimpleNamespace(
                intent_id=first.intent_id,
                instrument_id=INSTRUMENT_ID,
                side="BUY",
                tags=_entry_authorization_tags(first),
            )
            strategy._stash_entry_protection(first, first_plan)

            second = _intent(
                action="add_position",
                order_plan={
                    "side": "buy",
                    "type": "limit",
                    "price": "26900",
                    "quantity": "0.25",
                    "authorization": _authorization(
                        "channel",
                        "m3939",
                        "m3939-shared-entry",
                    ),
                },
                target_position_id=None,
            )
            second_plan = SimpleNamespace(
                intent_id=second.intent_id,
                instrument_id=INSTRUMENT_ID,
                side="BUY",
                tags=_entry_authorization_tags(second),
            )

            strategy._stash_entry_protection(second, second_plan)

            self.assertIn(str(first.intent_id), strategy._entry_protection_stash)
            self.assertNotIn(str(second.intent_id), strategy._entry_protection_stash)

    def test_entry_stash_uses_supplied_authorization_parent(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(Path(state_dir), orders=[])
            parent_intent_id = uuid4()
            entry = _intent(
                action="add_position",
                order_plan={
                    "side": "buy",
                    "type": "market",
                    "quantity": "0.1",
                    "stop_loss": "25000",
                    "authorization": {
                        **_authorization(
                            "channel",
                            "m3939",
                            "inherited-entry",
                        ),
                        "parent_intent_id": str(parent_intent_id),
                    },
                },
                target_position_id=None,
            )
            plan = plan_intent_execution(
                entry,
                strategy._planner_context(entry),
            )
            self.assertNotIsInstance(plan, ManagementPlan)
            self.assertFalse(hasattr(plan, "reason"))

            strategy._stash_entry_protection(entry, plan)

            stash = strategy._entry_protection_stash[str(entry.intent_id)]
            self.assertEqual(
                stash["stop_loss_parent_intent_id"],
                str(parent_intent_id),
            )
            self.assertEqual(
                stash["stop_loss_authorization"]["parent_intent_id"],
                str(parent_intent_id),
            )

    def test_state_rebuild_recovers_supplied_parent_from_entry_tags(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            strategy = _Strategy(state_path, orders=[])
            parent_intent_id = uuid4()
            entry = _intent(
                action="add_position",
                order_plan={
                    "side": "buy",
                    "type": "market",
                    "quantity": "0.1",
                    "stop_loss": "25000",
                    "authorization": {
                        **_authorization(
                            "channel",
                            "m3939",
                            "inherited-entry-rebuild",
                        ),
                        "parent_intent_id": str(parent_intent_id),
                    },
                },
                target_position_id=None,
            )
            plan = plan_intent_execution(
                entry,
                strategy._planner_context(entry),
            )
            self.assertFalse(hasattr(plan, "reason"))
            strategy._stash_entry_protection(entry, plan)
            stash = strategy._entry_protection_stash[str(entry.intent_id)]
            stash.pop("stop_loss_parent_intent_id", None)
            stash.pop("take_profit_parent_intent_id", None)
            stash.pop("stop_loss_authorization", None)
            stash.pop("take_profit_authorization", None)
            strategy._persist_entry_protection_stash()

            restarted = _Strategy(state_path, orders=[])
            restarted._entry_protection_stash = (
                restarted._load_entry_protection_stash()
            )

            rebuilt = restarted._entry_protection_stash[str(entry.intent_id)]
            self.assertEqual(
                rebuilt["stop_loss_parent_intent_id"],
                str(parent_intent_id),
            )
            self.assertEqual(
                rebuilt["stop_loss_authorization"]["parent_intent_id"],
                str(parent_intent_id),
            )

    def test_disabling_long_tps_leaves_short_book_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(
                Path(state_dir),
                orders=_dual_side_protection_orders(),
            )
            strategy._positions = [_position(), _short_position()]
            long_entry_intent_id = _seed_entry_stash(strategy)
            short_entry_intent_id = _seed_short_entry_stash(strategy)
            short_stash_before = dict(
                strategy._entry_protection_stash[str(short_entry_intent_id)]
            )
            disable_intent = _intent(
                action="replace_take_profits",
                order_plan={
                    "take_profits": [],
                    "disable_take_profits": True,
                    "authorization": _authorization(
                        "user",
                        "balen",
                        "manual-long-tp-off",
                    ),
                },
                target_position_id=POSITION_ID,
            )

            strategy._handle_intent(disable_intent)

            self.assertEqual(strategy.cancelled_client_order_ids, ["long-tp"])
            self.assertEqual(
                [order.client_order_id for order in strategy._orders],
                ["long-stop", "short-stop", "short-tp"],
            )
            long_stash = strategy._entry_protection_stash[
                str(long_entry_intent_id)
            ]
            short_stash = strategy._entry_protection_stash[
                str(short_entry_intent_id)
            ]
            self.assertIn("take_profit_tombstone", long_stash)
            self.assertNotIn("take_profit_tombstone", short_stash)
            self.assertEqual(short_stash, short_stash_before)

    def test_disable_cancels_mirror_only_system_tp_and_keeps_external_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(Path(state_dir), orders=[])
            entry_intent_id = _seed_entry_stash(strategy)
            system_tp_id = "Bba444351b6414c35a1d92f350265cd6443"
            external_tp_id = "aos_manual_take_profit"
            stash = strategy._entry_protection_stash[str(entry_intent_id)]
            stash["protection_roles"] = {
                system_tp_id: {
                    "role": "take_profit",
                    "quantity": "0.008",
                    "tp_price": "65534.4",
                },
            }
            mirror = _StaticMirror(
                (
                    _mirror_order(system_tp_id),
                    _mirror_order(external_tp_id),
                )
            )
            adapter = _RecordingCancelAdapter()
            strategy.set_exchange_cancel_adapter(adapter, mirror)

            runtime_package = ModuleType("runtime")
            runtime_package.__path__ = []
            exchange_cancel_module = _load_exchange_cancel_adapter_module()
            with patch.dict(
                sys.modules,
                {
                    "runtime": runtime_package,
                    "runtime.exchange_cancel_adapter": exchange_cancel_module,
                },
            ):
                strategy._handle_intent(
                    _intent(
                        action="replace_take_profits",
                        order_plan={
                            "take_profits": [],
                            "disable_take_profits": True,
                            "authorization": _authorization(
                                "user",
                                "balen",
                                "manual-tp-off-mirror-only",
                            ),
                        },
                    )
                )

            self.assertEqual(mirror.refresh_count, 1)
            self.assertEqual(len(adapter.calls), 1)
            action, request = adapter.calls[0]
            self.assertEqual(action, "cancel_order")
            self.assertEqual(request.client_order_id, system_tp_id)
            self.assertNotEqual(request.client_order_id, external_tp_id)
            self.assertIn("take_profit_tombstone", stash)
            self.assertEqual(stash["take_profit_tombstone"]["state"], "disabled")

    def test_refresh_failure_persists_disable_and_retries_after_mirror_recovers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(Path(state_dir), orders=[])
            entry_intent_id = _seed_entry_stash(strategy)
            system_tp_id = "Bba444351b6414c35a1d92f350265cd6443"
            stash = strategy._entry_protection_stash[str(entry_intent_id)]
            stash["protection_roles"] = {
                system_tp_id: {
                    "role": "take_profit",
                    "quantity": "0.008",
                    "tp_price": "65534.4",
                },
            }
            mirror = _StaticMirror(
                (_mirror_order(system_tp_id),),
                fail_refresh=True,
            )
            adapter = _RecordingCancelAdapter()
            strategy.set_exchange_cancel_adapter(adapter, mirror)

            strategy._handle_intent(
                _intent(
                    action="replace_take_profits",
                    order_plan={
                        "take_profits": [],
                        "disable_take_profits": True,
                        "authorization": _authorization(
                            "user",
                            "balen",
                            "stale-mirror-disable",
                        ),
                    },
                )
            )

            self.assertEqual(adapter.calls, [])
            tombstone = stash["take_profit_tombstone"]
            self.assertEqual(tombstone["state"], "cancel_pending")
            self.assertEqual(
                strategy.denials[-1].reason,
                "exchange_state_refresh_failed",
            )
            strategy.submitted_plans.clear()
            _sync_protection(strategy, str(entry_intent_id))
            self.assertEqual(
                [
                    plan
                    for plan in strategy.submitted_plans
                    if "lifecycle_role=take_profit" in plan.tags
                ],
                [],
            )

            mirror._fail_refresh = False
            restarted = _Strategy(Path(state_dir), orders=[])
            restarted._entry_protection_stash = (
                restarted._load_entry_protection_stash()
            )
            restarted.set_exchange_cancel_adapter(adapter, mirror)
            restarted._on_exchange_state_timer()

            self.assertEqual(len(adapter.calls), 1)
            restarted_stash = restarted._entry_protection_stash[
                str(entry_intent_id)
            ]
            self.assertEqual(
                restarted_stash["take_profit_tombstone"]["state"],
                "disabled",
            )

    def test_fresh_mirror_absence_excludes_stale_local_tp_from_cancel_decision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(
                Path(state_dir),
                orders=_live_protection_orders(),
            )
            entry_intent_id = _seed_entry_stash(strategy)
            adapter = _RecordingCancelAdapter()
            strategy.set_exchange_cancel_adapter(adapter, _StaticMirror(()))

            strategy._handle_intent(
                _intent(
                    action="replace_take_profits",
                    order_plan={
                        "take_profits": [],
                        "disable_take_profits": True,
                        "authorization": _authorization(
                            "user",
                            "balen",
                            "fresh-mirror-no-live-tp",
                        ),
                    },
                )
            )

            stash = strategy._entry_protection_stash[str(entry_intent_id)]
            self.assertEqual(adapter.calls, [])
            self.assertEqual(
                stash["take_profit_tombstone"]["state"],
                "disabled",
            )

    def test_unconfirmed_exchange_cancel_keeps_tombstone_pending(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _Strategy(Path(state_dir), orders=[])
            entry_intent_id = _seed_entry_stash(strategy)
            system_tp_id = "Bba444351b6414c35a1d92f350265cd6443"
            stash = strategy._entry_protection_stash[str(entry_intent_id)]
            stash["protection_roles"] = {
                system_tp_id: {
                    "role": "take_profit",
                    "quantity": "0.008",
                    "tp_price": "65534.4",
                },
            }
            adapter = _RecordingCancelAdapter(terminal_status="UNKNOWN")
            strategy.set_exchange_cancel_adapter(
                adapter,
                _StaticMirror((_mirror_order(system_tp_id),)),
            )

            strategy._handle_intent(
                _intent(
                    action="replace_take_profits",
                    order_plan={
                        "take_profits": [],
                        "disable_take_profits": True,
                        "authorization": _authorization(
                            "user",
                            "balen",
                            "unconfirmed-cancel",
                        ),
                    },
                )
            )

            self.assertEqual(
                stash["take_profit_tombstone"]["state"],
                "cancel_pending",
            )
            self.assertEqual(
                strategy.denials[-1].reason,
                "order_cancel_unconfirmed",
            )


class _Strategy(IntentExecutionStrategy):
    def __init__(
        self,
        state_dir: Path,
        orders: list[Any],
        fail_cancel: bool = False,
    ) -> None:
        self._state_dir = state_dir
        self._orders = list(orders)
        self._positions = [_position()]
        self._fail_cancel = fail_cancel
        self.submitted_plans: list[Any] = []
        self.cancelled_client_order_ids: list[str] = []
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id=ACCOUNT_ID,
                trading_state="ACTIVE",
                intent_execution_inbox_path=str(
                    state_dir / "intent-execution-inbox.json"
                ),
            )
        )
        _install_exchange_cancel_test_module()
        mirror = _StaticMirror(
            tuple(_mirror_from_local_order(order) for order in self._orders)
        )
        self.set_exchange_cancel_adapter(_StrategyCancelAdapter(self), mirror)

    def _protection_stash_path(self) -> str:
        return str(self._state_dir / self._PROTECTION_STASH_FILENAME)

    def _now(self) -> datetime:
        return NOW

    def _cache_instrument(self, _instrument_id: str) -> Any:
        return SimpleNamespace(
            id=INSTRUMENT_ID,
            price_increment="0.01",
            size_increment="0.001",
        )

    def _cache_positions(self, _instrument_id: str) -> list[Any]:
        return self._positions

    def _cache_orders(self, _instrument_id: str) -> list[Any]:
        return self._orders

    def _cache_orders_all(self, _instrument_id: str) -> tuple[Any, ...]:
        return tuple(self._orders)

    def _submit_order_plan(self, plan: Any) -> bool:
        self.submitted_plans.append(plan)
        return True

    def cancel_order(self, order: Any) -> None:
        if self._fail_cancel:
            raise RuntimeError("cancel rejected")
        self.cancelled_client_order_ids.append(str(order.client_order_id))
        self._orders = [
            item
            for item in self._orders
            if str(item.client_order_id) != str(order.client_order_id)
        ]

    def _schedule_protection_sync(
        self,
        _intent_key: str,
        delay_seconds: float | None = None,
    ) -> None:
        del delay_seconds

    def _planner_context(self, intent: Any) -> Any:
        from strategy.intent_execution_planner import PlannerContext

        return PlannerContext(
            account_id=ACCOUNT_ID,
            trading_state="ACTIVE",
            now=NOW,
            instrument=self._instrument_spec(INSTRUMENT_ID),
            position=self._position_snapshot(INSTRUMENT_ID),
            positions=self._position_snapshots(INSTRUMENT_ID),
            existing_orders=self._order_snapshots(INSTRUMENT_ID),
            existing_intent_ids=frozenset(),
        )


class _StaticMirror:
    def __init__(
        self,
        orders: tuple[Any, ...],
        *,
        fail_refresh: bool = False,
    ) -> None:
        self._orders = orders
        self._fail_refresh = fail_refresh
        self.refresh_count = 0

    def refresh(self) -> tuple[Any, ...]:
        self.refresh_count += 1
        if self._fail_refresh:
            raise RuntimeError("mirror refresh failed")
        return self._orders

    def orders_for_instrument(self, instrument_id: str) -> tuple[Any, ...]:
        return tuple(
            order
            for order in self._orders
            if str(order.instrument_id) == str(instrument_id)
        )

    def find_order(self, instrument_id: str, client_order_id: str) -> Any:
        for order in self.orders_for_instrument(instrument_id):
            if str(order.client_order_id) == str(client_order_id):
                return order
        return False


class _RecordingCancelAdapter:
    def __init__(
        self,
        *,
        fail_cancel: bool = False,
        terminal_status: str = "CANCELED",
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail_cancel = fail_cancel
        self.terminal_status = terminal_status

    def cancel(self, action: str, request: Any) -> Any:
        self.calls.append((action, request))
        if self.fail_cancel:
            raise RuntimeError("cancel rejected")
        return SimpleNamespace(
            outcome="canceled",
            terminal_status=self.terminal_status,
        )


class _StrategyCancelAdapter:
    def __init__(self, strategy: _Strategy) -> None:
        self._strategy = strategy

    def cancel(self, _action: str, request: Any) -> Any:
        if self._strategy._fail_cancel:
            raise RuntimeError("cancel rejected")
        client_order_id = str(request.client_order_id)
        self._strategy.cancelled_client_order_ids.append(client_order_id)
        self._strategy._orders = [
            order
            for order in self._strategy._orders
            if str(order.client_order_id) != client_order_id
        ]
        mirror = self._strategy._exchange_state_mirror
        if hasattr(mirror, "_orders"):
            mirror._orders = tuple(
                order
                for order in mirror._orders
                if str(order.client_order_id) != client_order_id
            )
        return SimpleNamespace(outcome="canceled", terminal_status="CANCELED")


def _replace_failure_for(destination_path: str):
    expected_destination = Path(destination_path)
    real_replace = os.replace

    def replace(source: str, destination: str | Path) -> None:
        if Path(destination) == expected_destination:
            raise OSError("disk full")
        real_replace(source, destination)

    return replace


def _install_exchange_cancel_test_module() -> None:
    runtime_package = ModuleType("runtime")
    runtime_package.__path__ = []
    sys.modules["runtime"] = runtime_package
    sys.modules["runtime.exchange_cancel_adapter"] = (
        _load_exchange_cancel_adapter_module()
    )


def _load_exchange_cancel_adapter_module() -> ModuleType:
    module_name = "_exchange_cancel_adapter_for_tombstone_test"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    module_path = SERVICE_ROOT / "runtime" / "exchange_cancel_adapter.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load exchange cancel adapter: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _seed_entry_stash(strategy: _Strategy) -> UUID:
    entry_intent_id = UUID("ba444351-0000-4000-8000-000000000001")
    source_message_id = "btc-original-signal"
    strategy._entry_protection_stash[str(entry_intent_id)] = {
        "stop_loss": "25000",
        "take_profits": ("64635.3", "65534.4", "66633.3"),
        "instrument_id": INSTRUMENT_ID,
        "entry_side": "BUY",
        "entry_tags": (
            f"intent_id={entry_intent_id}",
            f"decision_id={uuid4()}",
            f"risk_decision_id={uuid4()}",
            "idempotency_key=" + ("a" * 64),
            "action=open_position",
            f"account_id={ACCOUNT_ID}",
            f"parent_intent_id={entry_intent_id}",
            "authorized_by_type=channel",
            "authorized_by_id=btc-signal-channel",
            f"source_message_id={source_message_id}",
        ),
        "stop_loss_parent_intent_id": str(entry_intent_id),
        "take_profit_parent_intent_id": str(entry_intent_id),
        "stop_loss_authorization": {
            "authorized_by_type": "channel",
            "authorized_by_id": "btc-signal-channel",
            "source_message_id": source_message_id,
            "parent_intent_id": str(entry_intent_id),
        },
        "take_profit_authorization": {
            "authorized_by_type": "channel",
            "authorized_by_id": "btc-signal-channel",
            "source_message_id": source_message_id,
            "parent_intent_id": str(entry_intent_id),
        },
        "entry_sequence_max": 1,
        "protection_sequence_start": 11,
        "protection_roles": {},
        "tp_consumed": {},
        "pending_cancel_ids": (),
    }
    strategy._persist_entry_protection_stash()
    return entry_intent_id


def _live_protection_orders() -> list[Any]:
    return [
        SimpleNamespace(
            client_order_id="old-stop",
            instrument_id=INSTRUMENT_ID,
            order_type="STOP_MARKET",
            side="SELL",
            quantity="0.5",
            trigger_price="25000",
            reduce_only=True,
            status="ACCEPTED",
            tags=(f"position_id={POSITION_ID}", "lifecycle_role=stop_loss"),
        ),
        SimpleNamespace(
            client_order_id="old-tp",
            instrument_id=INSTRUMENT_ID,
            order_type="LIMIT",
            side="SELL",
            quantity="0.5",
            price="64635.3",
            reduce_only=True,
            status="ACCEPTED",
            tags=(f"position_id={POSITION_ID}", "lifecycle_role=take_profit"),
        ),
    ]


def _mirror_order(client_order_id: str) -> Any:
    return SimpleNamespace(
        account_id=ACCOUNT_ID,
        symbol="BTCUSDT",
        position_side="LONG",
        order_kind="regular",
        venue_order_id="1087155726226",
        client_order_id=client_order_id,
        instrument_id=INSTRUMENT_ID,
        order_type="LIMIT",
        side="SELL",
        quantity="0.008",
        price="65534.4",
        trigger_price="0",
        tags=(),
    )


def _mirror_from_local_order(order: Any) -> Any:
    tags = tuple(str(tag) for tag in (getattr(order, "tags", ()) or ()))
    position_side = "LONG"
    for tag in tags:
        if tag.startswith("position_id=") and tag.upper().endswith("-SHORT"):
            position_side = "SHORT"
            break
    return SimpleNamespace(
        account_id=ACCOUNT_ID,
        symbol="BTCUSDT",
        position_side=position_side,
        order_kind="regular",
        venue_order_id=str(getattr(order, "client_order_id", "")),
        client_order_id=str(getattr(order, "client_order_id", "")),
        instrument_id=str(getattr(order, "instrument_id", INSTRUMENT_ID)),
        order_type=str(getattr(order, "order_type", "")),
        side=str(getattr(order, "side", "")),
        quantity=str(getattr(order, "quantity", "")),
        price=getattr(order, "price", None),
        trigger_price=getattr(order, "trigger_price", None),
        tags=tags,
    )


def _dual_side_protection_orders() -> list[Any]:
    return [
        SimpleNamespace(
            client_order_id="long-stop",
            instrument_id=INSTRUMENT_ID,
            order_type="STOP_MARKET",
            side="SELL",
            quantity="0.5",
            trigger_price="25000",
            reduce_only=True,
            status="ACCEPTED",
            tags=(f"position_id={POSITION_ID}", "lifecycle_role=stop_loss"),
        ),
        SimpleNamespace(
            client_order_id="long-tp",
            instrument_id=INSTRUMENT_ID,
            order_type="LIMIT",
            side="SELL",
            quantity="0.5",
            price="64635.3",
            reduce_only=True,
            status="ACCEPTED",
            tags=(f"position_id={POSITION_ID}", "lifecycle_role=take_profit"),
        ),
        SimpleNamespace(
            client_order_id="short-stop",
            instrument_id=INSTRUMENT_ID,
            order_type="STOP_MARKET",
            side="BUY",
            quantity="0.4",
            trigger_price="30000",
            reduce_only=True,
            status="ACCEPTED",
            tags=(
                f"position_id={INSTRUMENT_ID}-SHORT",
                "lifecycle_role=stop_loss",
            ),
        ),
        SimpleNamespace(
            client_order_id="short-tp",
            instrument_id=INSTRUMENT_ID,
            order_type="LIMIT",
            side="BUY",
            quantity="0.4",
            price="24000",
            reduce_only=True,
            status="ACCEPTED",
            tags=(
                f"position_id={INSTRUMENT_ID}-SHORT",
                "lifecycle_role=take_profit",
            ),
        ),
    ]


def _position() -> Any:
    return SimpleNamespace(
        id=POSITION_ID,
        instrument_id=INSTRUMENT_ID,
        side="LONG",
        quantity="0.5",
        entry_price="27000",
    )


def _short_position() -> Any:
    return SimpleNamespace(
        id=f"{INSTRUMENT_ID}-SHORT",
        instrument_id=INSTRUMENT_ID,
        side="SHORT",
        quantity="0.4",
        entry_price="27500",
    )


def _seed_short_entry_stash(strategy: _Strategy) -> UUID:
    entry_intent_id = UUID("ba444351-0000-4000-8000-000000000002")
    source_message_id = "btc-short-original-signal"
    strategy._entry_protection_stash[str(entry_intent_id)] = {
        "stop_loss": "30000",
        "take_profits": ("24000",),
        "instrument_id": INSTRUMENT_ID,
        "entry_side": "SELL",
        "entry_tags": (
            f"intent_id={entry_intent_id}",
            f"decision_id={uuid4()}",
            f"risk_decision_id={uuid4()}",
            "idempotency_key=" + ("b" * 64),
            "action=open_position",
            f"account_id={ACCOUNT_ID}",
            f"parent_intent_id={entry_intent_id}",
            "authorized_by_type=channel",
            "authorized_by_id=btc-short-signal-channel",
            f"source_message_id={source_message_id}",
        ),
        "stop_loss_parent_intent_id": str(entry_intent_id),
        "take_profit_parent_intent_id": str(entry_intent_id),
        "stop_loss_authorization": {
            "authorized_by_type": "channel",
            "authorized_by_id": "btc-short-signal-channel",
            "source_message_id": source_message_id,
            "parent_intent_id": str(entry_intent_id),
        },
        "take_profit_authorization": {
            "authorized_by_type": "channel",
            "authorized_by_id": "btc-short-signal-channel",
            "source_message_id": source_message_id,
            "parent_intent_id": str(entry_intent_id),
        },
        "entry_sequence_max": 1,
        "protection_sequence_start": 11,
        "protection_roles": {},
        "tp_consumed": {},
        "pending_cancel_ids": (),
    }
    strategy._persist_entry_protection_stash()
    return entry_intent_id


def _authorization(
    authorized_by_type: str,
    authorized_by_id: str,
    source_message_id: str,
) -> dict[str, str]:
    return {
        "authorized_by_type": authorized_by_type,
        "authorized_by_id": authorized_by_id,
        "source_message_id": source_message_id,
    }


def _entry_authorization_tags(intent: _Intent) -> tuple[str, ...]:
    authorization = intent.order_plan["authorization"]
    return (
        f"intent_id={intent.intent_id}",
        f"decision_id={intent.decision_id}",
        f"risk_decision_id={intent.risk_decision_id}",
        f"idempotency_key={intent.idempotency_key}",
        f"action={intent.action}",
        f"account_id={intent.account_id}",
        f"parent_intent_id={intent.intent_id}",
        f"authorized_by_type={authorization['authorized_by_type']}",
        f"authorized_by_id={authorization['authorized_by_id']}",
        f"source_message_id={authorization['source_message_id']}",
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
    risk_budget: Any
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
        "action": "replace_take_profits",
        "order_plan": {
            "take_profits": [],
            "disable_take_profits": True,
            "authorization": _authorization("user", "balen", "manual-tp-off"),
        },
        "target_position_id": POSITION_ID,
        "valid_until": NOW + timedelta(minutes=5),
        "idempotency_key": sha256(str(intent_id).encode("ascii")).hexdigest(),
        "approved_at": NOW - timedelta(seconds=5),
        "risk_budget": SimpleNamespace(max_notional="100000"),
    }
    values.update(overrides)
    return _Intent(**values)


if __name__ == "__main__":
    unittest.main()
