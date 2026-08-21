from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from strategy.intent_execution_planner import (  # noqa: E402
    InstrumentSpec,
    OrderPlan,
    encode_client_order_id,
)
from strategy.intent_execution_strategy import (  # noqa: E402
    IntentExecutionStrategy,
    IntentExecutionStrategyConfig,
    _DurableIoResult,
    _DurableIoTask,
    _DurableIoTaskKind,
)
from runtime.live_canary_execution import (  # noqa: E402
    JsonLiveCanaryExecutionStore,
    LiveCanaryExecutionIdentity,
    LiveCanaryRegisterResult,
)
from runtime.intent_execution_inbox import (  # noqa: E402
    IntentDispatchResult,
    IntentExecutionIdentity,
    IntentExecutionState,
    JsonIntentExecutionInbox,
)
from runtime.exchange_cancel_adapter import (  # noqa: E402
    CancelResult,
    ExchangeOrderRef,
    TerminalExchangeWorker,
)
from risk.config import (  # noqa: E402
    RiskLimitConfig,
    build_live_risk_engine_kwargs,
)


class StrategyShellTest(unittest.TestCase):
    def test_live_entry_inventory_subscribes_mark_prices(self) -> None:
        strategy = _LiveEntryMarkSubscriptionStrategy(
            environment="live",
            inventory=(
                ("*", "100"),
                ("BTCUSDT-PERP.BINANCE", "100"),
                ("SOLUSDT-PERP.BINANCE", "100"),
            ),
        )

        strategy._subscribe_live_entry_mark_prices()

        self.assertEqual(
            strategy.mark_subscriptions,
            [
                "BTCUSDT-PERP.BINANCE",
                "SOLUSDT-PERP.BINANCE",
            ],
        )

    def test_testnet_entry_inventory_skips_mark_subscriptions(self) -> None:
        strategy = _LiveEntryMarkSubscriptionStrategy(
            environment="testnet",
            inventory=(("SOLUSDT-PERP.BINANCE", "100"),),
        )

        strategy._subscribe_live_entry_mark_prices()

        self.assertEqual(strategy.mark_subscriptions, [])

    def test_live_entry_mark_subscription_failure_blocks_startup(
        self,
    ) -> None:
        strategy = _LiveEntryMarkSubscriptionStrategy(
            environment="live",
            inventory=(("SOLUSDT-PERP.BINANCE", "100"),),
            fail_instrument_id="SOLUSDT-PERP.BINANCE",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            (
                "live entry mark price subscription failed: "
                "instrument=SOLUSDT-PERP.BINANCE"
            ),
        ):
            strategy._subscribe_live_entry_mark_prices()

    def test_exchange_dependencies_require_running_worker_on_start(
        self,
    ) -> None:
        strategy = _TerminalExchangeStrategy()
        strategy.set_exchange_cancel_adapter(
            _SlowTerminalAdapter(),
            _SlowTerminalMirror(),
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "require terminal exchange worker",
        ):
            strategy.on_start()

    def test_terminal_exchange_faults_keep_actor_callback_under_10ms(
        self,
    ) -> None:
        strategy = _TerminalExchangeStrategy()
        mirror = _SlowTerminalMirror()
        adapter = _SlowTerminalAdapter()
        worker = TerminalExchangeWorker(
            account_id="account-a",
            mirror=mirror,
            adapter=adapter,
            result_publisher=strategy.enqueue_terminal_exchange_result,
            capacity=4,
            total_deadline_seconds=1,
        )
        strategy.set_exchange_cancel_adapter(adapter, mirror)
        strategy.set_terminal_exchange_worker(worker)
        worker.start()
        command = SimpleNamespace(
            command_id="terminal-50ms-fault",
            type="cancel_all",
            args={
                "account_id": "account-a",
                "instrument_ids": ["SOLUSDT-PERP.BINANCE"],
                "authorization": {
                    "authorized_by_type": "user",
                    "authorized_by_id": "risk-admin",
                    "source_message_id": "fault-injection",
                },
            },
        )

        started_at = time.monotonic()
        strategy._on_node_command(command)
        elapsed = time.monotonic() - started_at

        try:
            self.assertLess(elapsed, 0.01)
            self.assertTrue(worker.wait_empty(timeout_seconds=1))
            strategy.drain_terminal_exchange_mailbox()
        finally:
            worker.stop()

        self.assertEqual(mirror.refresh_count, 1)
        self.assertEqual(adapter.cancel_count, 1)
        self.assertEqual(len(strategy.command_results), 1)
        payload = strategy.command_results[0]
        self.assertEqual(payload["command_id"], "terminal-50ms-fault")
        self.assertEqual(payload["operations"][0]["status"], "confirmed")

        replay_started_at = time.monotonic()
        strategy._on_node_command(command)
        replay_elapsed = time.monotonic() - replay_started_at

        self.assertLess(replay_elapsed, 0.01)
        self.assertEqual(mirror.refresh_count, 1)
        self.assertEqual(adapter.cancel_count, 1)
        self.assertEqual(len(strategy.command_results), 2)
        self.assertEqual(strategy.command_results[1], payload)

    def test_terminal_exchange_skips_manual_orders(self) -> None:
        strategy = _TerminalExchangeStrategy()
        mirror = _SlowTerminalMirror(client_order_id="manual-sol-order")
        adapter = _SlowTerminalAdapter()
        worker = TerminalExchangeWorker(
            account_id="account-a",
            mirror=mirror,
            adapter=adapter,
            result_publisher=strategy.enqueue_terminal_exchange_result,
            capacity=4,
            total_deadline_seconds=1,
        )
        strategy.set_exchange_cancel_adapter(adapter, mirror)
        strategy.set_terminal_exchange_worker(worker)
        worker.start()
        command = SimpleNamespace(
            command_id="manual-read-only",
            type="cancel_all",
            args={
                "account_id": "account-a",
                "instrument_ids": ["SOLUSDT-PERP.BINANCE"],
                "authorization": {
                    "authorized_by_type": "user",
                    "authorized_by_id": "risk-admin",
                    "source_message_id": "manual-read-only",
                },
            },
        )

        try:
            strategy._on_node_command(command)
            self.assertTrue(worker.wait_empty(timeout_seconds=1))
            strategy.drain_terminal_exchange_mailbox()
        finally:
            worker.stop()

        self.assertEqual(mirror.refresh_count, 1)
        self.assertEqual(adapter.cancel_count, 0)
        self.assertEqual(strategy.command_results[0]["operations"], [])

    def test_active_intent_ids_reads_client_order_id_and_tags(self) -> None:
        order_intent_id = uuid4()
        tagged_intent_id = uuid4()

        class _CacheStubStrategy(IntentExecutionStrategy):
            # Nautilus Actor.cache is read-only; override the cache-read helpers to
            # exercise _active_intent_ids without touching the real cache property.
            def _cache_orders(self, _instrument_id):
                return [
                    SimpleNamespace(client_order_id=encode_client_order_id(order_intent_id)),
                    SimpleNamespace(tags=[f"intent_id={tagged_intent_id}"]),
                ]

            def _cache_positions(self, _instrument_id):
                return ()

        strategy = _CacheStubStrategy(
            IntentExecutionStrategyConfig(account_id="account-a", trading_state="ACTIVE")
        )

        self.assertEqual(
            strategy._active_intent_ids("BTCUSDT-PERP.BINANCE"),
            {str(order_intent_id), str(tagged_intent_id)},
        )

    def test_symbol_open_freeze_is_symbol_scoped(self) -> None:
        strategy = IntentExecutionStrategy(
            IntentExecutionStrategyConfig(
                account_id="account-a",
                trading_state="ACTIVE",
            )
        )

        strategy._freeze_symbol_new_opens(
            "SOLUSDT-PERP.BINANCE",
            "robot order terminal confirmation pending",
        )

        sol_denial = strategy._symbol_open_freeze_denial(
            "open_position",
            "SOLUSDT-PERP.BINANCE",
        )
        btc_denial = strategy._symbol_open_freeze_denial(
            "open_position",
            "BTCUSDT-PERP.BINANCE",
        )
        sol_management = strategy._symbol_open_freeze_denial(
            "close_position",
            "SOLUSDT-PERP.BINANCE",
        )

        self.assertIsNotNone(sol_denial)
        self.assertEqual(sol_denial.reason, "symbol_new_open_frozen")
        self.assertIsNone(btc_denial)
        self.assertIsNone(sol_management)

    def test_protection_watchdog_freezes_symbol_after_two_missing_stops(
        self,
    ) -> None:
        intent_id = uuid4()
        strategy = _ProtectionWatchdogStrategy()
        strategy._entry_protection_stash[str(intent_id)] = {
            "stop_loss": "95",
            "take_profits": (),
            "instrument_id": "SOLUSDT-PERP.BINANCE",
            "entry_side": "BUY",
            "entry_tags": (f"intent_id={intent_id}",),
            "stop_loss_parent_intent_id": str(intent_id),
            "stop_loss_authorization": {
                "parent_intent_id": str(intent_id),
                "authorized_by_type": "user",
                "authorized_by_id": "risk-admin",
                "source_message_id": "watchdog-test",
            },
            "take_profit_parent_intent_id": str(intent_id),
            "take_profit_authorization": {
                "parent_intent_id": str(intent_id),
                "authorized_by_type": "user",
                "authorized_by_id": "risk-admin",
                "source_message_id": "watchdog-test",
            },
            "entry_sequence_max": 1,
            "protection_sequence_start": 11,
            "protection_roles": {},
            "tp_consumed": {},
            "pending_cancel_ids": (),
        }

        strategy._check_protection_watchdog(str(intent_id))
        strategy._check_protection_watchdog(str(intent_id))

        self.assertEqual(strategy.scheduled, [0.0, 0.0])
        self.assertIn("SOLUSDT", strategy.symbol_open_freezes)
        self.assertEqual(
            strategy.reported_events[0]["event_type"],
            "ProtectionWatchdogSymbolStopped",
        )

    def test_trading_state_getter_accepts_enum_values(self) -> None:
        class TradingState(Enum):
            ACTIVE = "ACTIVE"

        strategy = IntentExecutionStrategy(
            IntentExecutionStrategyConfig(account_id="account-a")
        )
        strategy.set_trading_state_getter(lambda: TradingState.ACTIVE)

        self.assertEqual(strategy._trading_state(), "ACTIVE")

    def test_live_account_a_strategy_rechecks_canary_permit(self) -> None:
        strategy = IntentExecutionStrategy(
            IntentExecutionStrategyConfig(
                account_id="account-a",
                node_id="node-a",
                trading_state="ACTIVE",
                environment="live",
                release_id="release-a",
            )
        )
        strategy.set_live_canary_portfolio_baseline_getter(
            lambda symbol: "4" * 64
        )
        intent_id = uuid4()
        valid = SimpleNamespace(
            intent_id=intent_id,
            instrument_id="BTCUSDT-PERP.BINANCE",
            risk_budget=SimpleNamespace(max_notional="12"),
        )
        now = datetime.now(timezone.utc)
        order_plan = {
            "type": "limit",
            "quantity": "0.1",
            "price": "100",
            "time_in_force": "IOC",
            "canary_permit": {
                "permit_id": str(uuid4()),
                "account_id": "account-a",
                "node_id": "node-a",
                "release_id": "release-a",
                "symbol": "BTCUSDT",
                "max_notional_usdt": "12",
                "max_cumulative_loss_usdt": "1.49",
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "portfolio_baseline_sha256": "4" * 64,
            },
        }

        self.assertIsNone(
            strategy._live_canary_intent_denial(
                valid,
                action="open_position",
                order_plan=order_plan,
            )
        )

        over_cap = SimpleNamespace(
            intent_id=intent_id,
            instrument_id="BTCUSDT-PERP.BINANCE",
            risk_budget=SimpleNamespace(max_notional="12.01"),
        )
        denial = strategy._live_canary_intent_denial(
            over_cap,
            action="open_position",
            order_plan=order_plan,
        )

        self.assertEqual(denial.reason, "canary_notional_exceeded")

        expired_plan = json.loads(json.dumps(order_plan))
        expired_plan["canary_permit"]["expires_at"] = (
            now - timedelta(seconds=1)
        )
        expired_plan["canary_permit"]["expires_at"] = (
            expired_plan["canary_permit"]["expires_at"].isoformat()
        )
        expired = strategy._live_canary_intent_denial(
            valid,
            action="open_position",
            order_plan=expired_plan,
        )
        self.assertEqual(expired.reason, "canary_permit_expired")

        strategy.set_live_canary_portfolio_baseline_getter(
            lambda symbol: "5" * 64
        )
        drifted = strategy._live_canary_intent_denial(
            valid,
            action="open_position",
            order_plan=order_plan,
        )
        self.assertEqual(
            drifted.reason,
            "canary_portfolio_baseline_drift",
        )

    def test_live_secondary_accounts_ignore_canary_only_permit_requirement(
        self,
    ) -> None:
        for account_id, node_id in (
            ("account-b", "node-b"),
            ("account-c", "node-c"),
            ("account-d", "node-d"),
        ):
            with self.subTest(account_id=account_id):
                strategy = IntentExecutionStrategy(
                    IntentExecutionStrategyConfig(
                        account_id=account_id,
                        node_id=node_id,
                        trading_state="ACTIVE",
                        environment="live",
                        release_id="release-a",
                    )
                )
                strategy.set_live_canary_portfolio_baseline_getter(
                    lambda _symbol: "4" * 64
                )
                strategy.set_live_open_gate_getter(
                    lambda: _canary_live_open_gate()
                )
                regular = SimpleNamespace(
                    intent_id=uuid4(),
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    risk_budget=SimpleNamespace(max_notional="100"),
                    order_plan={
                        "type": "market",
                        "side": "buy",
                        "quantity": "0.001",
                    },
                )

                denial = strategy._live_canary_intent_denial(
                    regular,
                    action="open_position",
                    order_plan=regular.order_plan,
                )
                self.assertIsNone(denial)
                regular_plan = _live_entry_order_plan(
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    quantity="0.001",
                    price="100",
                )
                identity_denial = (
                    strategy._live_canary_execution_identity(
                        regular,
                        regular_plan,
                    )
                )
                self.assertFalse(identity_denial)

    def test_fleet_complete_regular_open_skips_canary_runtime(self) -> None:
        for account_id, node_id in (
            ("account-a", "node-a"),
            ("account-b", "node-b"),
            ("account-c", "node-c"),
            ("account-d", "node-d"),
        ):
            with self.subTest(account_id=account_id):
                strategy = IntentExecutionStrategy(
                    IntentExecutionStrategyConfig(
                        account_id=account_id,
                        node_id=node_id,
                        trading_state="ACTIVE",
                        environment="live",
                        release_id="release-a",
                    )
                )
                order_plan = {
                    "type": "market",
                    "side": "buy",
                    "quantity": "0.001",
                    "rollout_phase": "fleet_complete",
                    "live_open_gate": _normal_live_open_gate(),
                }
                strategy.set_live_open_gate_getter(
                    lambda: _normal_live_open_gate()
                )
                intent = SimpleNamespace(
                    intent_id=uuid4(),
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    risk_budget=SimpleNamespace(max_notional="100"),
                    order_plan=order_plan,
                )

                self.assertIsNone(
                    strategy._live_canary_intent_denial(
                        intent,
                        action="open_position",
                        order_plan=order_plan,
                    )
                )
                plan = _live_entry_order_plan(
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    quantity="0.001",
                    price="100",
                )
                self.assertIs(
                    strategy._live_canary_execution_identity(
                        intent,
                        plan,
                    ),
                    False,
                )

    def test_fleet_complete_regular_open_rejects_stale_gate(self) -> None:
        strategy = IntentExecutionStrategy(
            IntentExecutionStrategyConfig(
                account_id="account-a",
                node_id="node-a",
                trading_state="ACTIVE",
                environment="live",
                release_id="release-a",
            )
        )
        strategy.set_live_open_gate_getter(
            lambda: _normal_live_open_gate()
        )
        stale_gate = _normal_live_open_gate()
        stale_gate["phase_version"] = 4
        order_plan = {
            "type": "market",
            "side": "buy",
            "quantity": "0.001",
            "live_open_gate": stale_gate,
        }
        intent = SimpleNamespace(
            intent_id=uuid4(),
            instrument_id="BTCUSDT-PERP.BINANCE",
            risk_budget=SimpleNamespace(max_notional="100"),
            order_plan=order_plan,
        )

        denial = strategy._live_canary_intent_denial(
            intent,
            action="open_position",
            order_plan=order_plan,
        )

        self.assertEqual(denial.reason, "live_open_gate_mismatch")

    def test_live_secondary_canary_durable_store_rejects_cross_account_identity(
        self,
    ) -> None:
        for account_id, node_id in (
            ("account-b", "node-b"),
            ("account-c", "node-c"),
            ("account-d", "node-d"),
        ):
            with self.subTest(account_id=account_id):
                with tempfile.TemporaryDirectory() as state_dir:
                    strategy = _CanarySubmitStrategy(
                        Path(state_dir),
                        account_id=account_id,
                        node_id=node_id,
                    )
                    identity = _canary_execution_identity(
                        account_id=account_id,
                        node_id=node_id,
                    )
                    plan = _canary_order_plan(identity)

                    submitted = strategy._submit_order_plan(
                        plan,
                        live_canary_execution=identity,
                    )

                    self.assertTrue(submitted)
                    record = strategy.live_canary_store.get(identity)
                    self.assertTrue(record)
                    self.assertEqual(record.account_id, account_id)

                with tempfile.TemporaryDirectory() as state_dir:
                    strategy = _CanarySubmitStrategy(
                        Path(state_dir),
                        account_id=account_id,
                        node_id=node_id,
                    )
                    wrong_account = _canary_execution_identity(
                        account_id="account-a",
                        node_id=node_id,
                    )
                    plan = _canary_order_plan(wrong_account)

                    submitted = strategy._submit_order_plan(
                        plan,
                        live_canary_execution=wrong_account,
                    )

                    self.assertFalse(submitted)
                    self.assertEqual(
                        strategy.denials[-1].reason,
                        "canary_execution_identity_mismatch",
                    )

    def test_live_canary_final_notional_rejects_1000_times_100(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _CanarySubmitStrategy(Path(state_dir))
            identity = _canary_execution_identity()
            plan = _canary_order_plan(
                identity,
                quantity="1000",
                price="100",
            )

            submitted = strategy._submit_order_plan(
                plan,
                live_canary_execution=identity,
            )

            self.assertFalse(submitted)
            self.assertEqual(strategy.submitted_orders, [])
            self.assertEqual(
                strategy.denials[-1].reason,
                "canary_notional_exceeded",
            )

    def test_live_canary_limit_ioc_at_or_below_12_submits_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _CanarySubmitStrategy(Path(state_dir))
            identity = _canary_execution_identity()
            plan = _canary_order_plan(
                identity,
                quantity="0.12",
                price="100",
            )

            submitted = strategy._submit_order_plan(
                plan,
                live_canary_execution=identity,
            )

            self.assertTrue(submitted)
            self.assertEqual(strategy.submitted_orders, [plan.client_order_id])
            record = strategy.live_canary_store.get(identity)
            self.assertTrue(record)
            self.assertEqual(record.state.value, "dispatched")

    def test_live_canary_limit_ioc_requires_available_mark_price(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _CanarySubmitStrategy(Path(state_dir))
            identity = _canary_execution_identity()
            plan = _canary_order_plan(identity)
            strategy._cache_mark_price = (  # type: ignore[method-assign]
                lambda instrument_id: False
            )

            submitted = strategy._submit_order_plan(
                plan,
                live_canary_execution=identity,
            )

            self.assertFalse(submitted)
            self.assertEqual(strategy.submitted_orders, [])
            self.assertEqual(
                strategy.denials[-1].reason,
                "live_entry_mark_price_unavailable",
            )

    def test_live_account_b_rejects_instrument_missing_from_risk_inventory(
        self,
    ) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "100"),),
        )
        plan = _live_entry_order_plan(
            instrument_id="XAUUSDT-PERP.BINANCE",
            quantity="0.01",
            price="10",
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertFalse(submitted)
        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(
            strategy.denials[-1].reason,
            "live_entry_instrument_not_allowed",
        )

    def test_live_default_cap_allows_instrument_missing_from_inventory(
        self,
    ) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "100000"), ("*", "10000")),
            final_quantity="99",
            final_price="100",
        )
        plan = _live_entry_order_plan(
            instrument_id="JTOUSDT-PERP.BINANCE",
            quantity="0.5",
            price="90",
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertTrue(submitted)
        self.assertEqual(
            strategy.submitted_orders,
            [plan.client_order_id],
        )
        self.assertEqual(strategy.denials, [])

    def test_live_default_cap_enforces_notional_for_unlisted_instrument(
        self,
    ) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("*", "10000"),),
            final_quantity="100.01",
            final_price="100",
        )
        plan = _live_entry_order_plan(
            instrument_id="JTOUSDT-PERP.BINANCE",
            quantity="0.5",
            price="90",
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertFalse(submitted)
        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(
            strategy.denials[-1].reason,
            "live_entry_notional_exceeded",
        )
        self.assertEqual(
            strategy.denials[-1].detail,
            "instrument=JTOUSDT-PERP.BINANCE:actual=10001.00:cap=10000",
        )

    def test_live_explicit_cap_overrides_default_cap(self) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("*", "10000"), ("BTCUSDT-PERP.BINANCE", "100000")),
            final_quantity="500",
            final_price="100",
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity="0.5",
            price="90",
            approved_max_notional="100000",
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertTrue(submitted)
        self.assertEqual(
            strategy.submitted_orders,
            [plan.client_order_id],
        )
        self.assertEqual(strategy.denials, [])

    def test_live_account_b_rejects_final_limit_notional_over_cap(
        self,
    ) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "100"),),
            final_quantity="1.01",
            final_price="100",
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity="0.5",
            price="90",
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertFalse(submitted)
        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(
            strategy.denials[-1].reason,
            "live_entry_notional_exceeded",
        )
        self.assertEqual(
            strategy.denials[-1].detail,
            "instrument=BTCUSDT-PERP.BINANCE:actual=101.00:cap=100",
        )

    def test_live_account_b_rejects_final_limit_notional_over_intent_budget(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "12000"),),
                release_id="release-a",
                final_quantity="1.01",
                final_price="100",
                state_dir=Path(state_dir),
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_entry_intent(max_notional="100")
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = _normal_live_open_gate()

            strategy._handle_intent_ready(
                intent,
                exchange_state_ready=False,
            )

            self.assertEqual(strategy.submitted_orders, [])
            self.assertEqual(
                strategy.denials[-1].reason,
                "approved_max_notional_exceeded",
            )
            self.assertEqual(
                strategy.denials[-1].detail,
                "instrument=BTCUSDT-PERP.BINANCE:actual=101.00:approved=100",
            )

    def test_live_entry_requires_approved_max_notional_on_final_gate(
        self,
    ) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "12000"),),
            final_quantity="1",
            final_price="100",
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity="1",
            price="100",
            approved_max_notional="",
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertFalse(submitted)
        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(
            strategy.denials[-1].reason,
            "approved_max_notional_invalid",
        )
        self.assertEqual(
            strategy.denials[-1].detail,
            "order_plan.approved_max_notional",
        )

    def test_live_account_b_allows_final_limit_notional_at_cap(
        self,
    ) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "100"),),
            final_quantity="1",
            final_price="100",
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity="0.5",
            price="90",
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertTrue(submitted)
        self.assertEqual(
            strategy.submitted_orders,
            [plan.client_order_id],
        )
        self.assertEqual(strategy.denials, [])

    def test_live_zone_ladder_rejects_three_rung_total_before_first_submit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "12000"),),
                release_id="release-a",
                state_dir=Path(state_dir),
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(max_notional="100")
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = _normal_live_open_gate()
            identity = _durable_identity(intent)
            strategy._intent_execution_inbox.register_received(
                identity,
                _durable_payload(intent),
            )

            strategy._handle_intent_ready(
                intent,
                exchange_state_ready=False,
            )

            self.assertEqual(strategy.submitted_orders, [])
            self.assertEqual(
                strategy.denials[-1].reason,
                "approved_max_notional_exceeded",
            )
            self.assertIn("actual=120", strategy.denials[-1].detail)
            self.assertIn("approved=100", strategy.denials[-1].detail)

    def test_live_zone_ladder_rejects_final_adjusted_total_before_submit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                final_quantity="0.34",
                state_dir=Path(state_dir),
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(
                max_notional="100",
                tranche_quantity="0.33",
            )
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = _normal_live_open_gate()

            strategy._handle_intent_ready(
                intent,
                exchange_state_ready=False,
            )

            self.assertEqual(strategy.submitted_orders, [])
            self.assertEqual(
                strategy.denials[-1].reason,
                "approved_max_notional_exceeded",
            )
            self.assertIn("actual=102.00", strategy.denials[-1].detail)
            self.assertIn("approved=100", strategy.denials[-1].detail)

    def test_durable_zone_ladder_rejects_before_dispatch_persist(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                final_quantity="0.34",
                state_dir=Path(state_dir),
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(
                max_notional="100",
                tranche_quantity="0.33",
            )
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = _normal_live_open_gate()
            identity = _durable_identity(intent)
            strategy._intent_execution_inbox.register_received(
                identity,
                _durable_payload(intent),
            )

            strategy._handle_intent_ready(
                intent,
                exchange_state_ready=False,
                durable_async=True,
            )

            self.assertEqual(strategy.submitted_orders, [])
            self.assertEqual(
                strategy.denials[-1].reason,
                "approved_max_notional_exceeded",
            )
            self.assertIn(
                "actual=102.00",
                strategy.denials[-1].detail,
            )
            self.assertIn(
                "approved=100",
                strategy.denials[-1].detail,
            )
            record = strategy._intent_execution_inbox.get(identity)
            self.assertTrue(record)
            self.assertEqual(record.state, IntentExecutionState.RECEIVED)

            restarted = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                final_quantity="0.34",
                state_dir=Path(state_dir),
            )
            restarted.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            restarted._handle_intent(intent)

            self.assertEqual(restarted.submitted_orders, [])
            self.assertEqual(
                restarted.denials[-1].reason,
                "approved_max_notional_exceeded",
            )
            replay_record = (
                restarted._intent_execution_inbox.get(identity)
            )
            self.assertTrue(replay_record)
            self.assertEqual(
                replay_record.state,
                IntentExecutionState.RECEIVED,
            )

    def test_durable_zone_ladder_submits_prebuilt_orders_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=Path(state_dir),
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(max_notional="150")
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = (
                _normal_live_open_gate()
            )
            identity = _durable_identity(intent)
            strategy._intent_execution_inbox.register_received(
                identity,
                _durable_payload(intent),
            )

            try:
                strategy._handle_intent_ready(
                    intent,
                    exchange_state_ready=False,
                    durable_async=True,
                )

                self.assertEqual(len(strategy.built_orders), 3)
                self.assertEqual(
                    len(strategy._prepared_zone_ladder_orders),
                    1,
                )
                self.assertTrue(
                    _pump_durable_until(
                        strategy,
                        lambda: len(strategy.submitted_orders) == 3,
                        timeout=1.0,
                    )
                )
                self.assertEqual(len(strategy.built_orders), 3)
                self.assertEqual(
                    strategy.submitted_order_objects,
                    strategy.built_orders,
                )
                for submitted, built in zip(
                    strategy.submitted_order_objects,
                    strategy.built_orders,
                ):
                    self.assertIs(submitted, built)
                self.assertEqual(
                    strategy._prepared_zone_ladder_orders,
                    {},
                )
                self.assertFalse(
                    strategy._durable_entry_prepare_active
                )
                self.assertFalse(
                    strategy._durable_entry_prepare_preimage
                )
            finally:
                strategy.on_stop()

    def test_durable_zone_ladder_queue_rejection_releases_prebuilt_orders(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=Path(state_dir),
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(max_notional="150")
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = (
                _normal_live_open_gate()
            )

            try:
                with patch.object(
                    strategy,
                    "_submit_durable_io_task",
                    return_value=False,
                ):
                    strategy._handle_intent_ready(
                        intent,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                self.assertEqual(strategy.submitted_orders, [])
                self.assertEqual(
                    strategy._prepared_zone_ladder_orders,
                    {},
                )
                self.assertFalse(
                    strategy._durable_entry_prepare_active
                )
                self.assertFalse(
                    strategy._durable_entry_prepare_preimage
                )
            finally:
                strategy.on_stop()

    def test_durable_zone_ladder_worker_failure_releases_prebuilt_orders(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=Path(state_dir),
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(max_notional="150")
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = (
                _normal_live_open_gate()
            )
            identity = _durable_identity(intent)
            strategy._intent_execution_inbox.register_received(
                identity,
                _durable_payload(intent),
            )

            try:
                with patch.object(
                    strategy._intent_execution_inbox,
                    "begin_dispatch",
                    side_effect=OSError("dispatch fsync failed"),
                ):
                    strategy._handle_intent_ready(
                        intent,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                    self.assertTrue(
                        strategy.wait_for_durable_io(
                            timeout_seconds=1.0
                        )
                    )
                    strategy.drain_durable_io_mailbox()

                self.assertEqual(strategy.submitted_orders, [])
                self.assertEqual(
                    strategy._prepared_zone_ladder_orders,
                    {},
                )
                self.assertEqual(
                    strategy._entry_protection_stash,
                    {},
                )
                self.assertFalse(
                    strategy._durable_entry_prepare_active
                )
                self.assertEqual(
                    strategy._pending_durable_entry_intents,
                    [],
                )
                self.assertIn(
                    "dispatch fsync failed",
                    strategy.durable_io_halted_reason,
                )
            finally:
                strategy.on_stop()

    def test_durable_zone_ladder_fifo_restores_rejected_a_then_submits_b(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=Path(state_dir),
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent_a = _live_zone_ladder_intent(
                max_notional="150"
            )
            intent_b = _live_zone_ladder_intent(
                max_notional="150"
            )
            for intent in (intent_a, intent_b):
                intent.order_plan["rollout_phase"] = "fleet_complete"
                intent.order_plan["live_open_gate"] = (
                    _normal_live_open_gate()
                )
                intent.order_plan["stop_loss"] = "90"

            queued_tasks: list[_DurableIoTask] = []

            def capture_task(task: _DurableIoTask) -> bool:
                queued_tasks.append(task)
                return True

            try:
                with patch.object(
                    strategy,
                    "_submit_durable_io_task",
                    side_effect=capture_task,
                ):
                    strategy._handle_intent_ready(
                        intent_a,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                    strategy._handle_intent_ready(
                        intent_b,
                        exchange_state_ready=False,
                        durable_async=True,
                    )

                    self.assertEqual(len(queued_tasks), 1)
                    self.assertEqual(len(strategy.built_orders), 3)
                    self.assertEqual(
                        strategy._pending_durable_entry_intents,
                        [(intent_b, False)],
                    )
                    self.assertIn(
                        str(intent_a.intent_id),
                        strategy._entry_protection_stash,
                    )
                    self.assertNotIn(
                        str(intent_b.intent_id),
                        strategy._entry_protection_stash,
                    )

                    strategy._on_prepare_submit_result(
                        _DurableIoResult(
                            task=queued_tasks[0],
                            outcome={
                                "dispatch_result": (
                                    IntentDispatchResult.REJECTED
                                ),
                                "claim_result": False,
                            },
                        )
                    )

                    self.assertEqual(len(queued_tasks), 2)
                    self.assertEqual(len(strategy.built_orders), 6)
                    self.assertNotIn(
                        str(intent_a.intent_id),
                        strategy._entry_protection_stash,
                    )
                    self.assertIn(
                        str(intent_b.intent_id),
                        strategy._entry_protection_stash,
                    )
                    prepared_b = tuple(
                        strategy._prepared_zone_ladder_orders[
                            queued_tasks[1].operation_id
                        ]
                    )

                    strategy._on_prepare_submit_result(
                        _DurableIoResult(
                            task=queued_tasks[1],
                            outcome={
                                "dispatch_result": (
                                    IntentDispatchResult.READY
                                ),
                                "claim_result": False,
                            },
                        )
                    )

                self.assertEqual(
                    tuple(strategy.submitted_order_objects),
                    prepared_b,
                )
                for submitted, prepared in zip(
                    strategy.submitted_order_objects,
                    prepared_b,
                ):
                    self.assertIs(submitted, prepared)
                self.assertEqual(
                    set(strategy._entry_protection_stash),
                    {str(intent_b.intent_id)},
                )
                self.assertEqual(
                    strategy._prepared_zone_ladder_orders,
                    {},
                )
                self.assertEqual(
                    strategy._pending_durable_entry_intents,
                    [],
                )
                self.assertFalse(
                    strategy._durable_entry_prepare_active
                )
            finally:
                strategy.on_stop()

    def test_durable_single_and_ladder_share_entry_prepare_fifo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=Path(state_dir),
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            single = _live_entry_intent(max_notional="150")
            ladder = _live_zone_ladder_intent(
                max_notional="150"
            )
            for intent in (single, ladder):
                intent.order_plan["rollout_phase"] = "fleet_complete"
                intent.order_plan["live_open_gate"] = (
                    _normal_live_open_gate()
                )
                intent.order_plan["stop_loss"] = "90"

            queued_tasks: list[_DurableIoTask] = []

            def capture_task(task: _DurableIoTask) -> bool:
                queued_tasks.append(task)
                return True

            try:
                with patch.object(
                    strategy,
                    "_submit_durable_io_task",
                    side_effect=capture_task,
                ):
                    strategy._handle_intent_ready(
                        single,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                    strategy._handle_intent_ready(
                        ladder,
                        exchange_state_ready=False,
                        durable_async=True,
                    )

                    self.assertEqual(len(queued_tasks), 1)
                    self.assertEqual(
                        queued_tasks[0].continuation["mode"],
                        "single",
                    )
                    self.assertEqual(strategy.built_orders, [])

                    strategy._on_prepare_submit_result(
                        _DurableIoResult(
                            task=queued_tasks[0],
                            outcome={
                                "dispatch_result": (
                                    IntentDispatchResult.READY
                                ),
                                "claim_result": False,
                            },
                        )
                    )

                    self.assertEqual(len(queued_tasks), 2)
                    self.assertEqual(
                        queued_tasks[1].continuation["mode"],
                        "zone_ladder",
                    )
                    self.assertEqual(len(strategy.built_orders), 4)
                    strategy._on_prepare_submit_result(
                        _DurableIoResult(
                            task=queued_tasks[1],
                            outcome={
                                "dispatch_result": (
                                    IntentDispatchResult.READY
                                ),
                                "claim_result": False,
                            },
                        )
                    )

                self.assertEqual(len(strategy.submitted_orders), 4)
                self.assertEqual(
                    set(strategy._entry_protection_stash),
                    {str(ladder.intent_id)},
                )
                self.assertFalse(
                    strategy._durable_entry_prepare_active
                )
            finally:
                strategy.on_stop()

    def test_durable_entry_operator_halt_rolls_back_staged_disk_stash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=state_path,
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(
                max_notional="150"
            )
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = (
                _normal_live_open_gate()
            )
            intent.order_plan["stop_loss"] = "90"
            identity = _durable_identity(intent)
            strategy._intent_execution_inbox.register_received(
                identity,
                _durable_payload(intent),
            )
            queued_tasks: list[_DurableIoTask] = []

            def capture_task(task: _DurableIoTask) -> bool:
                queued_tasks.append(task)
                return True

            try:
                with patch.object(
                    strategy,
                    "_submit_durable_io_task",
                    side_effect=capture_task,
                ):
                    strategy._handle_intent_ready(
                        intent,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                prepare_task = queued_tasks[0]
                outcome = strategy._process_prepare_submit_task(
                    prepare_task
                )
                self.assertTrue(outcome["protection_persisted"])
                self.assertIn(
                    str(intent.intent_id),
                    strategy._load_entry_protection_stash(),
                )

                strategy.set_trading_state_getter(lambda: "HALTED")
                strategy._on_prepare_submit_result(
                    _DurableIoResult(
                        task=prepare_task,
                        outcome=outcome,
                    )
                )
                self.assertTrue(
                    _pump_durable_until(
                        strategy,
                        lambda: (
                            not strategy._durable_entry_prepare_active
                        ),
                        timeout=1.0,
                    )
                )

                restarted = _LiveEntrySubmitStrategy(
                    inventory=(
                        ("BTCUSDT-PERP.BINANCE", "100000"),
                    ),
                    release_id="release-a",
                    state_dir=state_path,
                )
                self.assertEqual(
                    restarted._load_entry_protection_stash(),
                    {},
                )
            finally:
                strategy.on_stop()

    def test_durable_entry_fifo_waits_for_rollback_fsync(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=Path(state_dir),
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent_a = _live_zone_ladder_intent(
                max_notional="150"
            )
            intent_b = _live_zone_ladder_intent(
                max_notional="150"
            )
            for intent in (intent_a, intent_b):
                intent.order_plan["rollout_phase"] = "fleet_complete"
                intent.order_plan["live_open_gate"] = (
                    _normal_live_open_gate()
                )
                intent.order_plan["stop_loss"] = "90"
            queued_tasks: list[_DurableIoTask] = []

            def capture_task(task: _DurableIoTask) -> bool:
                queued_tasks.append(task)
                return True

            try:
                with patch.object(
                    strategy,
                    "_submit_durable_io_task",
                    side_effect=capture_task,
                ):
                    strategy._handle_intent_ready(
                        intent_a,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                    strategy._handle_intent_ready(
                        intent_b,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                    prepare_a = queued_tasks[0]
                    strategy._prepared_zone_ladder_orders.pop(
                        prepare_a.operation_id
                    )
                    strategy._on_prepare_submit_result(
                        _DurableIoResult(
                            task=prepare_a,
                            outcome={
                                "dispatch_result": (
                                    IntentDispatchResult.READY
                                ),
                                "claim_result": False,
                                "protection_persisted": True,
                            },
                        )
                    )

                    self.assertEqual(len(queued_tasks), 2)
                    rollback_a = queued_tasks[1]
                    self.assertEqual(
                        rollback_a.kind,
                        _DurableIoTaskKind.PREPARE_ROLLBACK,
                    )
                    self.assertEqual(len(strategy.built_orders), 3)
                    self.assertEqual(
                        strategy._pending_durable_entry_intents,
                        [(intent_b, False)],
                    )

                    strategy._on_prepare_rollback_result(
                        _DurableIoResult(
                            task=rollback_a,
                            outcome={"rolled_back": True},
                        )
                    )

                    self.assertEqual(len(queued_tasks), 3)
                    self.assertEqual(
                        queued_tasks[2].kind,
                        _DurableIoTaskKind.PREPARE_SUBMIT,
                    )
                    self.assertEqual(len(strategy.built_orders), 6)
            finally:
                strategy.on_stop()

    def test_durable_entry_rejection_preserves_concurrent_memory_update(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=Path(state_dir),
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            concurrent_key = str(uuid4())
            strategy._entry_protection_stash[concurrent_key] = {
                "instrument_id": "ETHUSDT-PERP.BINANCE",
                "state": "ready",
            }
            intent = _live_zone_ladder_intent(
                max_notional="150"
            )
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = (
                _normal_live_open_gate()
            )
            intent.order_plan["stop_loss"] = "90"
            queued_tasks: list[_DurableIoTask] = []

            def capture_task(task: _DurableIoTask) -> bool:
                queued_tasks.append(task)
                return True

            try:
                with patch.object(
                    strategy,
                    "_submit_durable_io_task",
                    side_effect=capture_task,
                ):
                    strategy._handle_intent_ready(
                        intent,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                    strategy._entry_protection_stash[
                        concurrent_key
                    ]["state"] = "managed"
                    strategy._entry_protection_stash[
                        concurrent_key
                    ]["management_revision"] = 3
                    strategy._on_prepare_submit_result(
                        _DurableIoResult(
                            task=queued_tasks[0],
                            outcome={
                                "dispatch_result": (
                                    IntentDispatchResult.REJECTED
                                ),
                                "claim_result": False,
                                "protection_persisted": False,
                            },
                        )
                    )

                self.assertNotIn(
                    str(intent.intent_id),
                    strategy._entry_protection_stash,
                )
                self.assertEqual(
                    strategy._entry_protection_stash[
                        concurrent_key
                    ]["state"],
                    "managed",
                )
                self.assertEqual(
                    strategy._entry_protection_stash[
                        concurrent_key
                    ]["management_revision"],
                    3,
                )
            finally:
                strategy.on_stop()

    def test_durable_entry_disk_rollback_preserves_concurrent_update(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=state_path,
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(
                max_notional="150"
            )
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = (
                _normal_live_open_gate()
            )
            intent.order_plan["stop_loss"] = "90"
            identity = _durable_identity(intent)
            strategy._intent_execution_inbox.register_received(
                identity,
                _durable_payload(intent),
            )
            queued_tasks: list[_DurableIoTask] = []

            def capture_task(task: _DurableIoTask) -> bool:
                queued_tasks.append(task)
                return True

            try:
                with patch.object(
                    strategy,
                    "_submit_durable_io_task",
                    side_effect=capture_task,
                ):
                    strategy._handle_intent_ready(
                        intent,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                prepare_task = queued_tasks[0]
                outcome = strategy._process_prepare_submit_task(
                    prepare_task
                )
                self.assertTrue(outcome["protection_persisted"])
                concurrent_key = str(uuid4())
                concurrent_payload = (
                    strategy._read_entry_protection_stash_payload()
                )
                concurrent_payload[concurrent_key] = {
                    "instrument_id": "ETHUSDT-PERP.BINANCE",
                    "state": "protection_callback_persisted",
                }
                strategy._write_entry_protection_stash(
                    concurrent_payload
                )

                rollback_task = _DurableIoTask(
                    kind=_DurableIoTaskKind.PREPARE_ROLLBACK,
                    operation_id=uuid4().hex,
                    protection_payload=(
                        prepare_task.protection_payload
                    ),
                    protection_rollback_payload=(
                        prepare_task.protection_rollback_payload
                    ),
                )
                rollback_outcome = (
                    strategy._process_prepare_rollback_task(
                        rollback_task
                    )
                )

                self.assertTrue(rollback_outcome["rolled_back"])
                persisted = (
                    strategy._read_entry_protection_stash_payload()
                )
                self.assertNotIn(str(intent.intent_id), persisted)
                self.assertEqual(
                    persisted[concurrent_key]["state"],
                    "protection_callback_persisted",
                )
            finally:
                strategy.on_stop()

    def test_durable_entry_disk_rollback_preserves_updated_old_owner(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=state_path,
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            old_owner_key = str(uuid4())
            old_owner = {
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "entry_side": "BUY",
                "state": "ready",
                "protection_revision": 2,
            }
            strategy._entry_protection_stash[old_owner_key] = (
                dict(old_owner)
            )
            strategy._persist_entry_protection_stash()
            intent = _live_zone_ladder_intent(
                max_notional="150"
            )
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = (
                _normal_live_open_gate()
            )
            intent.order_plan["stop_loss"] = "90"
            identity = _durable_identity(intent)
            strategy._intent_execution_inbox.register_received(
                identity,
                _durable_payload(intent),
            )
            queued_tasks: list[_DurableIoTask] = []

            def capture_task(task: _DurableIoTask) -> bool:
                queued_tasks.append(task)
                return True

            try:
                with patch.object(
                    strategy,
                    "_submit_durable_io_task",
                    side_effect=capture_task,
                ):
                    strategy._handle_intent_ready(
                        intent,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                prepare_task = queued_tasks[0]
                outcome = strategy._process_prepare_submit_task(
                    prepare_task
                )
                self.assertTrue(outcome["protection_persisted"])
                concurrent_payload = (
                    strategy._read_entry_protection_stash_payload()
                )
                concurrent_payload[old_owner_key] = {
                    **old_owner,
                    "state": "terminal_callback_persisted",
                    "protection_revision": 3,
                }
                strategy._write_entry_protection_stash(
                    concurrent_payload
                )
                rollback_task = _DurableIoTask(
                    kind=_DurableIoTaskKind.PREPARE_ROLLBACK,
                    operation_id=uuid4().hex,
                    protection_payload=(
                        prepare_task.protection_payload
                    ),
                    protection_rollback_payload=(
                        prepare_task.protection_rollback_payload
                    ),
                )

                outcome = strategy._process_prepare_rollback_task(
                    rollback_task
                )

                self.assertTrue(outcome["rolled_back"])
                restarted = _LiveEntrySubmitStrategy(
                    inventory=(
                        ("BTCUSDT-PERP.BINANCE", "100000"),
                    ),
                    release_id="release-a",
                    state_dir=state_path,
                )
                persisted = restarted._load_entry_protection_stash()
                self.assertNotIn(str(intent.intent_id), persisted)
                self.assertEqual(
                    persisted[old_owner_key]["state"],
                    "terminal_callback_persisted",
                )
                self.assertEqual(
                    persisted[old_owner_key]["protection_revision"],
                    3,
                )
            finally:
                strategy.on_stop()

    def test_halt_before_first_durable_submit_sends_no_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=Path(state_dir),
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(
                max_notional="150"
            )
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = (
                _normal_live_open_gate()
            )
            intent.order_plan["stop_loss"] = "90"
            queued_tasks: list[_DurableIoTask] = []

            def capture_task(task: _DurableIoTask) -> bool:
                queued_tasks.append(task)
                return True

            original_submission_order = strategy._submission_order

            def halt_before_submit(plan, *, prepared_order=False):
                order = original_submission_order(
                    plan,
                    prepared_order=prepared_order,
                )
                strategy._halt_durable_io(
                    "halt before first exchange submit"
                )
                return order

            try:
                with patch.object(
                    strategy,
                    "_submit_durable_io_task",
                    side_effect=capture_task,
                ):
                    strategy._handle_intent_ready(
                        intent,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                with patch.object(
                    strategy,
                    "_submission_order",
                    side_effect=halt_before_submit,
                ):
                    strategy._on_prepare_submit_result(
                        _DurableIoResult(
                            task=queued_tasks[0],
                            outcome={
                                "dispatch_result": (
                                    IntentDispatchResult.READY
                                ),
                                "claim_result": False,
                                "protection_persisted": False,
                            },
                        )
                    )

                self.assertEqual(strategy.submitted_orders, [])
                self.assertNotIn(
                    str(intent.intent_id),
                    strategy._entry_protection_stash,
                )
                self.assertIn(
                    "halt before first exchange submit",
                    strategy.durable_io_halted_reason,
                )
            finally:
                strategy.on_stop()

    def test_halt_after_first_ladder_submit_blocks_later_rungs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=Path(state_dir),
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(
                max_notional="150"
            )
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = (
                _normal_live_open_gate()
            )
            intent.order_plan["stop_loss"] = "90"
            queued_tasks: list[_DurableIoTask] = []
            cancelled: list[str] = []

            def capture_task(task: _DurableIoTask) -> bool:
                queued_tasks.append(task)
                return True

            original_submit_order = strategy.submit_order

            def submit_then_halt(order, position_id=None) -> None:
                original_submit_order(
                    order,
                    position_id=position_id,
                )
                if len(strategy.submitted_orders) == 1:
                    strategy._halt_durable_io(
                        "halt after first ladder submit"
                    )

            try:
                with patch.object(
                    strategy,
                    "_submit_durable_io_task",
                    side_effect=capture_task,
                ):
                    strategy._handle_intent_ready(
                        intent,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                with (
                    patch.object(
                        strategy,
                        "submit_order",
                        side_effect=submit_then_halt,
                    ),
                    patch.object(
                        strategy,
                        "_cancel_order_by_client_order_id",
                        side_effect=lambda _instrument_id, order_id: (
                            cancelled.append(order_id)
                        ),
                    ),
                ):
                    strategy._on_prepare_submit_result(
                        _DurableIoResult(
                            task=queued_tasks[0],
                            outcome={
                                "dispatch_result": (
                                    IntentDispatchResult.READY
                                ),
                                "claim_result": False,
                                "protection_persisted": True,
                            },
                        )
                    )

                self.assertEqual(
                    len(strategy.submitted_orders),
                    1,
                )
                self.assertEqual(
                    cancelled,
                    strategy.submitted_orders,
                )
                self.assertIn(
                    str(intent.intent_id),
                    strategy._entry_protection_stash,
                )
                self.assertIn(
                    "halt after first ladder submit",
                    strategy.durable_io_halted_reason,
                )
            finally:
                strategy.on_stop()

    def test_halt_wins_threaded_final_submit_lock_race(
        self,
    ) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity="0.1",
            price="100",
        )
        reached_final_gate = Event()
        release_final_gate = Event()
        submit_result: list[bool] = []

        def block_before_final_gate(_order, _plan):
            reached_final_gate.set()
            release_final_gate.wait(timeout=1.0)
            return None

        def submit() -> None:
            submit_result.append(
                strategy._submit_order_plan_after_durable_prepare(
                    plan,
                    live_canary_execution=False,
                )
            )

        try:
            with patch.object(
                strategy,
                "_hedge_position_id",
                side_effect=block_before_final_gate,
            ):
                submit_thread = Thread(target=submit)
                submit_thread.start()
                self.assertTrue(
                    reached_final_gate.wait(timeout=1.0)
                )
                halt_thread = Thread(
                    target=strategy._halt_durable_io,
                    args=("threaded halt before submit",),
                )
                halt_thread.start()
                halt_thread.join(timeout=1.0)
                self.assertFalse(halt_thread.is_alive())
                release_final_gate.set()
                submit_thread.join(timeout=1.0)
                self.assertFalse(submit_thread.is_alive())

            self.assertEqual(submit_result, [False])
            self.assertEqual(strategy.submitted_orders, [])
            self.assertIn(
                "threaded halt before submit",
                strategy.durable_io_halted_reason,
            )
        finally:
            release_final_gate.set()
            strategy.on_stop()

    def test_entry_rollback_skips_key_changed_after_stage(
        self,
    ) -> None:
        current = {"entry": {"state": "managed"}}
        staged = {"entry": {"state": "staged"}}
        preimage = {"entry": {"state": "previous"}}

        merged, status = (
            IntentExecutionStrategy._conditional_protection_rollback(
                current,
                staged,
                preimage,
            )
        )

        self.assertEqual(status, "preserved")
        self.assertEqual(merged, current)

    def test_durable_entry_worker_failure_after_stage_rolls_back_disk(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=state_path,
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(
                max_notional="150"
            )
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = (
                _normal_live_open_gate()
            )
            intent.order_plan["stop_loss"] = "90"
            identity = _durable_identity(intent)
            strategy._intent_execution_inbox.register_received(
                identity,
                _durable_payload(intent),
            )
            original_write = strategy._write_entry_protection_stash

            def write_then_fail(payload) -> None:
                original_write(payload)
                if str(intent.intent_id) in payload:
                    raise OSError("failure after staged fsync")

            try:
                with patch.object(
                    strategy,
                    "_write_entry_protection_stash",
                    side_effect=write_then_fail,
                ):
                    strategy._handle_intent_ready(
                        intent,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                    self.assertTrue(
                        strategy.wait_for_durable_io(
                            timeout_seconds=1.0
                        )
                    )
                    strategy.drain_durable_io_mailbox()

                restarted = _LiveEntrySubmitStrategy(
                    inventory=(
                        ("BTCUSDT-PERP.BINANCE", "100000"),
                    ),
                    release_id="release-a",
                    state_dir=state_path,
                )
                self.assertEqual(
                    restarted._load_entry_protection_stash(),
                    {},
                )
                self.assertIn(
                    "failure after staged fsync",
                    strategy.durable_io_halted_reason,
                )
            finally:
                strategy.on_stop()

    def test_durable_entry_stop_rolls_back_unpublished_disk_stash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=state_path,
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(
                max_notional="150"
            )
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = (
                _normal_live_open_gate()
            )
            intent.order_plan["stop_loss"] = "90"
            identity = _durable_identity(intent)
            strategy._intent_execution_inbox.register_received(
                identity,
                _durable_payload(intent),
            )
            queued_tasks: list[_DurableIoTask] = []

            def capture_task(task: _DurableIoTask) -> bool:
                queued_tasks.append(task)
                return True

            with patch.object(
                strategy,
                "_submit_durable_io_task",
                side_effect=capture_task,
            ):
                strategy._handle_intent_ready(
                    intent,
                    exchange_state_ready=False,
                    durable_async=True,
                )
            prepare_task = queued_tasks[0]
            outcome = strategy._process_prepare_submit_task(
                prepare_task
            )
            self.assertTrue(outcome["protection_persisted"])

            strategy.on_stop()

            restarted = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=state_path,
            )
            self.assertEqual(
                restarted._load_entry_protection_stash(),
                {},
            )

    def test_durable_entry_halt_compensates_disk_before_mailbox_drain(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=state_path,
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(
                max_notional="150"
            )
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = (
                _normal_live_open_gate()
            )
            intent.order_plan["stop_loss"] = "90"
            identity = _durable_identity(intent)
            strategy._intent_execution_inbox.register_received(
                identity,
                _durable_payload(intent),
            )
            queued_tasks: list[_DurableIoTask] = []

            def capture_task(task: _DurableIoTask) -> bool:
                queued_tasks.append(task)
                return True

            try:
                with patch.object(
                    strategy,
                    "_submit_durable_io_task",
                    side_effect=capture_task,
                ):
                    strategy._handle_intent_ready(
                        intent,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                prepare_task = queued_tasks[0]
                outcome = strategy._process_prepare_submit_task(
                    prepare_task
                )
                self.assertTrue(outcome["protection_persisted"])

                strategy._halt_durable_io("worker timeout")

                restarted = _LiveEntrySubmitStrategy(
                    inventory=(
                        ("BTCUSDT-PERP.BINANCE", "100000"),
                    ),
                    release_id="release-a",
                    state_dir=state_path,
                )
                self.assertEqual(
                    restarted._load_entry_protection_stash(),
                    {},
                )
            finally:
                strategy.on_stop()

    def test_durable_entry_mailbox_overflow_compensates_disk(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            strategy = _LiveEntrySubmitStrategy(
                inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
                release_id="release-a",
                state_dir=state_path,
            )
            strategy.set_live_open_gate_getter(
                lambda: _normal_live_open_gate()
            )
            intent = _live_zone_ladder_intent(
                max_notional="150"
            )
            intent.order_plan["rollout_phase"] = "fleet_complete"
            intent.order_plan["live_open_gate"] = (
                _normal_live_open_gate()
            )
            intent.order_plan["stop_loss"] = "90"
            identity = _durable_identity(intent)
            strategy._intent_execution_inbox.register_received(
                identity,
                _durable_payload(intent),
            )
            queued_tasks: list[_DurableIoTask] = []

            def capture_task(task: _DurableIoTask) -> bool:
                queued_tasks.append(task)
                return True

            try:
                with patch.object(
                    strategy,
                    "_submit_durable_io_task",
                    side_effect=capture_task,
                ):
                    strategy._handle_intent_ready(
                        intent,
                        exchange_state_ready=False,
                        durable_async=True,
                    )
                prepare_task = queued_tasks[0]
                outcome = strategy._process_prepare_submit_task(
                    prepare_task
                )
                self.assertTrue(outcome["protection_persisted"])
                for index in range(
                    strategy._DURABLE_IO_QUEUE_CAPACITY
                ):
                    strategy._durable_io_mailbox.put_nowait(
                        _DurableIoResult(
                            task=_DurableIoTask(
                                kind=(
                                    _DurableIoTaskKind.INTENT_RECEIVE
                                ),
                                operation_id=str(index),
                            ),
                            outcome=False,
                        )
                    )

                strategy._publish_durable_io_result(
                    _DurableIoResult(
                        task=prepare_task,
                        outcome=outcome,
                    )
                )

                restarted = _LiveEntrySubmitStrategy(
                    inventory=(
                        ("BTCUSDT-PERP.BINANCE", "100000"),
                    ),
                    release_id="release-a",
                    state_dir=state_path,
                )
                self.assertEqual(
                    restarted._load_entry_protection_stash(),
                    {},
                )
                self.assertIn(
                    "result mailbox capacity exceeded",
                    strategy.durable_io_halted_reason,
                )
            finally:
                strategy.on_stop()

    def test_durable_entry_fifo_handles_many_sync_rejections_iteratively(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _DurableIntentStrategy(Path(state_dir))
            intents = [
                _durable_entry_intent()
                for _index in range(1500)
            ]
            strategy._pending_durable_entry_intents.extend(
                (intent, False)
                for intent in intents
            )

            with patch.object(
                strategy,
                "_handle_intent_ready_now",
            ) as handle:
                strategy._advance_durable_entry_prepare()

            self.assertEqual(handle.call_count, len(intents))
            self.assertEqual(
                strategy._pending_durable_entry_intents,
                [],
            )
            self.assertFalse(
                strategy._durable_entry_prepare_active
            )

    def test_live_secondary_regular_submit_stays_compatible_with_release_id(
        self,
    ) -> None:
        for account_id, node_id in (
            ("account-b", "node-b"),
            ("account-c", "node-c"),
            ("account-d", "node-d"),
        ):
            with self.subTest(account_id=account_id):
                strategy = _LiveEntrySubmitStrategy(
                    inventory=(("BTCUSDT-PERP.BINANCE", "100"),),
                    account_id=account_id,
                    node_id=node_id,
                    release_id="release-a",
                )
                plan = _live_entry_order_plan(
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    quantity="1",
                    price="100",
                )

                submitted = strategy._submit_order_plan(plan)

                self.assertTrue(submitted)
                self.assertEqual(
                    strategy.submitted_orders,
                    [plan.client_order_id],
                )
                self.assertEqual(strategy.denials, [])

    def test_live_account_b_rejects_missing_explicit_inventory(
        self,
    ) -> None:
        build_live_risk_engine_kwargs(
            RiskLimitConfig(
                max_notional_per_order={
                    "BTCUSDT-PERP.BINANCE": "100",
                },
                max_order_submit_rate="50/00:00:01",
                max_order_modify_rate="1/00:00:01",
            )
        )
        strategy = _LiveEntrySubmitStrategy(
            inventory=(),
            final_quantity="1",
            final_price="100",
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity="0.5",
            price="90",
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertFalse(submitted)
        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(
            strategy.denials[-1].reason,
            "live_entry_instrument_not_allowed",
        )

    def test_live_market_entry_requires_available_mark_price(self) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "100"),),
            mark_price=False,
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            order_type="MARKET",
            quantity="1",
            price=None,
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertFalse(submitted)
        self.assertEqual(
            strategy.denials[-1].reason,
            "live_entry_mark_price_unavailable",
        )

    def test_live_market_entry_uses_fallback_mark_snapshot(self) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "100"),),
            mark_price=False,
            fallback_mark_price="99",
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            order_type="MARKET",
            quantity="1",
            price=None,
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertTrue(submitted)
        self.assertEqual(
            strategy.submitted_orders,
            [plan.client_order_id],
        )
        self.assertEqual(strategy.denials, [])

    def test_live_market_entry_rejects_stale_fallback_mark_snapshot(
        self,
    ) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "100"),),
            mark_price=False,
            fallback_mark_price="99",
            fallback_mark_price_at=datetime(
                2026,
                8,
                8,
                11,
                59,
                49,
                tzinfo=timezone.utc,
            ),
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            order_type="MARKET",
            quantity="1",
            price=None,
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertFalse(submitted)
        self.assertEqual(
            strategy.denials[-1].reason,
            "live_entry_mark_price_stale",
        )

    def test_live_limit_entry_rejects_stale_mark_price(self) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "100"),),
            mark_price="100",
            mark_price_at=datetime(
                2026,
                8,
                8,
                11,
                59,
                49,
                tzinfo=timezone.utc,
            ),
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            order_type="LIMIT",
            quantity="1",
            price="100",
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertFalse(submitted)
        self.assertEqual(
            strategy.denials[-1].reason,
            "live_entry_mark_price_stale",
        )

    def test_prepared_live_limit_rechecks_mark_before_submit(self) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "100"),),
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            order_type="LIMIT",
            quantity="1",
            price="100",
        )
        prepared_order = strategy._submission_order(plan)
        strategy._mark_price = False

        submitted = strategy._submit_order_plan(
            plan,
            prepared_order=prepared_order,
        )

        self.assertFalse(submitted)
        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(
            strategy.denials[-1].reason,
            "live_entry_mark_price_unavailable",
        )

    def test_live_market_entry_rejects_stale_mark_price(self) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "100"),),
            mark_price="100",
            mark_price_at=datetime(
                2026,
                8,
                8,
                11,
                59,
                49,
                tzinfo=timezone.utc,
            ),
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            order_type="MARKET",
            quantity="1",
            price=None,
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertFalse(submitted)
        self.assertEqual(
            strategy.denials[-1].reason,
            "live_entry_mark_price_stale",
        )

    def test_live_market_entry_allows_fresh_mark_price_at_cap(self) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "100"),),
            mark_price="100",
            mark_price_at=datetime(
                2026,
                8,
                8,
                11,
                59,
                55,
                tzinfo=timezone.utc,
            ),
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            order_type="MARKET",
            quantity="1",
            price=None,
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertTrue(submitted)
        self.assertEqual(
            strategy.submitted_orders,
            [plan.client_order_id],
        )

    def test_live_market_entry_rejects_fresh_mark_over_intent_budget(
        self,
    ) -> None:
        strategy = _LiveEntrySubmitStrategy(
            inventory=(("BTCUSDT-PERP.BINANCE", "100000"),),
            mark_price="100",
            mark_price_at=datetime(
                2026,
                8,
                8,
                11,
                59,
                55,
                tzinfo=timezone.utc,
            ),
        )
        plan = _live_entry_order_plan(
            instrument_id="BTCUSDT-PERP.BINANCE",
            order_type="MARKET",
            quantity="1.01",
            price=None,
            approved_max_notional="100",
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertFalse(submitted)
        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(
            strategy.denials[-1].reason,
            "approved_max_notional_exceeded",
        )
        self.assertEqual(
            strategy.denials[-1].detail,
            "instrument=BTCUSDT-PERP.BINANCE:actual=101.00:approved=100",
        )

    def test_live_reduce_only_market_bypasses_entry_inventory_and_mark_price(
        self,
    ) -> None:
        strategy = _LiveEntrySubmitStrategy(inventory=())
        plan = _live_entry_order_plan(
            instrument_id="UNKNOWN-PERP.BINANCE",
            order_type="MARKET",
            quantity="1000",
            price=None,
            reduce_only=True,
            approved_max_notional="",
        )

        submitted = strategy._submit_order_plan(plan)

        self.assertTrue(submitted)
        self.assertEqual(
            strategy.submitted_orders,
            [plan.client_order_id],
        )
        self.assertEqual(strategy.denials, [])

    def test_same_permit_direct_delivery_of_two_intents_submits_one_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _CanaryMsgbusStrategy(Path(state_dir))
            permit_id = str(uuid4())
            first = _live_canary_intent(permit_id=permit_id)
            second = _live_canary_intent(permit_id=permit_id)

            try:
                strategy._on_intent_msg(first)
                strategy._on_intent_msg(second)

                self.assertTrue(
                    _pump_durable_until(
                        strategy,
                        lambda: (
                            len(strategy.submitted_orders) == 1
                            and bool(strategy.denials)
                        ),
                        timeout=1.0,
                    )
                )
                self.assertEqual(
                    strategy.submitted_orders,
                    [encode_client_order_id(first.intent_id)],
                )
                self.assertEqual(
                    strategy.denials[-1].reason,
                    "canary_permit_already_claimed",
                )
            finally:
                strategy.on_stop()

    def test_data_client_received_canary_with_equivalent_price_submits_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _CanaryMsgbusStrategy(Path(state_dir))
            intent = _live_canary_intent(permit_id=str(uuid4()))
            canary_identity = _canary_execution_identity(
                permit_id=intent.order_plan["canary_permit"]["permit_id"],
                intent_id=str(intent.intent_id),
            )
            execution_identity = _durable_identity(intent)

            self.assertIs(
                strategy.live_canary_store.register_received(
                    canary_identity
                ),
                LiveCanaryRegisterResult.REGISTERED,
            )
            strategy._intent_execution_inbox.register_received(
                execution_identity,
                _durable_payload(intent),
            )

            try:
                strategy._on_intent_msg(intent)

                self.assertTrue(
                    _pump_durable_until(
                        strategy,
                        lambda: len(strategy.submitted_orders) == 1,
                        timeout=1.0,
                    )
                )
                self.assertEqual(
                    strategy.submitted_orders,
                    [encode_client_order_id(intent.intent_id)],
                )
                self.assertEqual(strategy.denials, [])
            finally:
                strategy.on_stop()

    def test_submit_crash_replay_recovers_without_second_submit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            identity = _canary_execution_identity()
            plan = _canary_order_plan(identity)
            first = _CanarySubmitStrategy(state_path)
            first.crash_before_dispatch_persist = True

            with self.assertRaisesRegex(
                SystemExit,
                "crash before dispatch persist",
            ):
                first._submit_order_plan(
                    plan,
                    live_canary_execution=identity,
                )

            restarted = _CanarySubmitStrategy(
                state_path,
                existing_order_ids={identity.client_order_id},
            )
            replayed = restarted._submit_order_plan(
                plan,
                live_canary_execution=identity,
            )

            self.assertTrue(replayed)
            self.assertEqual(first.submitted_orders, [identity.client_order_id])
            self.assertEqual(restarted.submitted_orders, [])
            self.assertEqual(restarted.denials, [])
            record = restarted.live_canary_store.get(identity)
            self.assertTrue(record)
            self.assertEqual(record.state.value, "exchange_confirmed")

    def test_generic_submit_crash_recovers_from_exchange_truth_without_second_open(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            intent = _durable_entry_intent()
            identity = _durable_identity(intent)
            inbox = JsonIntentExecutionInbox(
                state_path / "intent-execution-inbox.json"
            )
            inbox.register_received(identity, _durable_payload(intent))
            exchange_order_ids: set[str] = set()
            first = _DurableIntentStrategy(
                state_path,
                exchange_order_ids=exchange_order_ids,
                crash_after_submit=True,
            )

            with self.assertRaisesRegex(
                SystemExit,
                "crash after exchange submit",
            ):
                first._handle_intent(intent)

            restarted = _DurableIntentStrategy(
                state_path,
                exchange_order_ids=exchange_order_ids,
            )
            restarted._handle_intent(intent)

            expected_id = encode_client_order_id(intent.intent_id)
            self.assertEqual(first.submitted_orders, [expected_id])
            self.assertEqual(restarted.submitted_orders, [])
            record = inbox.get(identity)
            self.assertTrue(record)
            self.assertEqual(
                record.state,
                IntentExecutionState.EXCHANGE_CONFIRMED,
            )

    def test_generic_replay_after_closed_position_uses_durable_terminal_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            intent = _durable_entry_intent()
            identity = _durable_identity(intent)
            inbox = JsonIntentExecutionInbox(
                state_path / "intent-execution-inbox.json"
            )
            inbox.register_received(identity, _durable_payload(intent))
            first = _DurableIntentStrategy(state_path)
            first._handle_intent(intent)
            client_order_id = encode_client_order_id(intent.intent_id)
            first.on_order_accepted(
                SimpleNamespace(client_order_id=client_order_id)
            )
            self.assertTrue(
                first.wait_for_durable_io(timeout_seconds=1.0)
            )

            restarted = _DurableIntentStrategy(state_path)
            try:
                restarted._handle_intent(intent)

                self.assertEqual(
                    first.submitted_orders,
                    [client_order_id],
                )
                self.assertEqual(restarted.submitted_orders, [])
                record = inbox.get(identity)
                self.assertTrue(record)
                self.assertEqual(
                    record.state,
                    IntentExecutionState.EXCHANGE_CONFIRMED,
                )
            finally:
                first.on_stop()
                restarted.on_stop()

    def test_exchange_confirmation_fsync_does_not_block_order_callback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            intent = _durable_entry_intent()
            identity = _durable_identity(intent)
            inbox = JsonIntentExecutionInbox(
                state_path / "intent-execution-inbox.json"
            )
            inbox.register_received(identity, _durable_payload(intent))
            strategy = _DurableIntentStrategy(state_path)
            strategy._handle_intent(intent)
            client_order_id = encode_client_order_id(intent.intent_id)
            fsync_started = Event()
            release_fsync = Event()
            inbox_module = __import__(
                "runtime.intent_execution_inbox",
                fromlist=["os"],
            )
            original_fsync = inbox_module.os.fsync

            def blocking_fsync(fd: int) -> None:
                fsync_started.set()
                release_fsync.wait(timeout=1.0)
                original_fsync(fd)

            try:
                with patch.object(
                    inbox_module.os,
                    "fsync",
                    blocking_fsync,
                ):
                    started_at = time.monotonic()
                    strategy.on_order_accepted(
                        SimpleNamespace(
                            client_order_id=client_order_id
                        )
                    )
                    elapsed = time.monotonic() - started_at

                    self.assertLess(elapsed, 0.01)
                    self.assertTrue(fsync_started.wait(timeout=1.0))
                    release_fsync.set()
                    self.assertTrue(
                        _wait_until(
                            lambda: (
                                inbox.get(identity).state
                                is IntentExecutionState.EXCHANGE_CONFIRMED
                            ),
                            timeout=1.0,
                        )
                    )
            finally:
                release_fsync.set()
                strategy.on_stop()

    def test_intent_receipt_flock_does_not_block_actor_callback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _DurableIntentStrategy(Path(state_dir))
            intent = _durable_entry_intent()
            flock_started = Event()
            release_flock = Event()
            inbox_module = __import__(
                "runtime.intent_execution_inbox",
                fromlist=["fcntl"],
            )
            original_flock = inbox_module.fcntl.flock

            def blocking_flock(fd: int, operation: int) -> None:
                flock_started.set()
                release_flock.wait(timeout=1.0)
                original_flock(fd, operation)

            try:
                with patch.object(
                    inbox_module.fcntl,
                    "flock",
                    blocking_flock,
                ):
                    started_at = time.monotonic()
                    strategy._on_intent_msg(intent)
                    elapsed = time.monotonic() - started_at

                    self.assertLess(elapsed, 0.01)
                    self.assertTrue(flock_started.wait(timeout=1.0))
                    self.assertEqual(strategy.submitted_orders, [])
                    release_flock.set()
                    self.assertTrue(
                        _pump_durable_until(
                            strategy,
                            lambda: len(strategy.submitted_orders) == 1,
                            timeout=1.0,
                        )
                    )
            finally:
                release_flock.set()
                strategy.on_stop()

    def test_dispatch_fsync_completes_before_actor_submits_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            intent = _durable_entry_intent()
            identity = _durable_identity(intent)
            inbox = JsonIntentExecutionInbox(
                state_path / "intent-execution-inbox.json"
            )
            inbox.register_received(identity, _durable_payload(intent))
            strategy = _DurableIntentStrategy(state_path)
            fsync_started = Event()
            release_fsync = Event()
            inbox_module = __import__(
                "runtime.intent_execution_inbox",
                fromlist=["os"],
            )
            original_fsync = inbox_module.os.fsync

            def blocking_fsync(fd: int) -> None:
                fsync_started.set()
                release_fsync.wait(timeout=1.0)
                original_fsync(fd)

            try:
                with patch.object(
                    inbox_module.os,
                    "fsync",
                    blocking_fsync,
                ):
                    strategy._on_intent_msg(intent)
                    self.assertTrue(
                        strategy.wait_for_durable_io(
                            timeout_seconds=1.0
                        )
                    )
                    strategy.drain_durable_io_mailbox()

                    self.assertTrue(fsync_started.wait(timeout=1.0))
                    self.assertEqual(strategy.submitted_orders, [])
                    release_fsync.set()
                    self.assertTrue(
                        _pump_durable_until(
                            strategy,
                            lambda: len(strategy.submitted_orders) == 1,
                            timeout=1.0,
                        )
                    )
            finally:
                release_fsync.set()
                strategy.on_stop()

    def test_live_canary_claim_and_dispatch_persist_before_state_progress(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _CanaryMsgbusStrategy(Path(state_dir))
            intent = _live_canary_intent(permit_id=str(uuid4()))
            claim_started = Event()
            release_claim = Event()
            original_claim = strategy._live_canary_execution_store.claim

            def blocking_claim(identity):
                claim_started.set()
                release_claim.wait(timeout=1.0)
                return original_claim(identity)

            def canary_dispatched() -> bool:
                record = (
                    strategy.live_canary_store
                    .find_by_client_order_id(
                        encode_client_order_id(intent.intent_id)
                    )
                )
                return bool(
                    record
                    and record.state.value == "dispatched"
                )

            strategy._live_canary_execution_store.claim = blocking_claim
            try:
                started_at = time.monotonic()
                strategy._on_intent_msg(intent)
                elapsed = time.monotonic() - started_at

                self.assertLess(elapsed, 0.01)
                self.assertTrue(
                    _pump_durable_until(
                        strategy,
                        claim_started.is_set,
                        timeout=1.0,
                    )
                )
                self.assertEqual(strategy.submitted_orders, [])
                release_claim.set()
                self.assertTrue(
                    _pump_durable_until(
                        strategy,
                        lambda: len(strategy.submitted_orders) == 1,
                        timeout=1.0,
                    )
                )
                self.assertTrue(
                    _pump_durable_until(
                        strategy,
                        canary_dispatched,
                        timeout=1.0,
                    )
                )
            finally:
                release_claim.set()
                strategy.on_stop()

    def test_order_submitted_keeps_live_canary_dispatched(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _CanarySubmitStrategy(Path(state_dir))
            identity = _canary_execution_identity()
            plan = _canary_order_plan(identity)
            try:
                self.assertTrue(
                    strategy._submit_order_plan(
                        plan,
                        live_canary_execution=identity,
                    )
                )

                strategy.on_order_submitted(
                    SimpleNamespace(
                        client_order_id=identity.client_order_id
                    )
                )

                record = strategy.live_canary_store.get(identity)
                self.assertTrue(record)
                self.assertEqual(record.state.value, "dispatched")
            finally:
                strategy.on_stop()

    def test_order_denied_replay_never_creates_exchange_confirmation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            intent = _durable_entry_intent()
            identity = _durable_identity(intent)
            inbox = JsonIntentExecutionInbox(
                state_path / "intent-execution-inbox.json"
            )
            inbox.register_received(identity, _durable_payload(intent))
            exchange_order_ids: set[str] = set()
            first = _DurableIntentStrategy(
                state_path,
                exchange_order_ids=exchange_order_ids,
            )
            first._handle_intent(intent)
            client_order_id = encode_client_order_id(intent.intent_id)

            try:
                first.on_order_denied(
                    SimpleNamespace(client_order_id=client_order_id)
                )
            finally:
                first.on_stop()

            exchange_order_ids.discard(client_order_id)
            record = inbox.get(identity)
            self.assertTrue(record)
            self.assertEqual(
                record.state,
                IntentExecutionState.DISPATCHED,
            )

            restarted = _DurableIntentStrategy(
                state_path,
                exchange_order_ids=exchange_order_ids,
            )
            try:
                restarted._handle_intent(intent)
            finally:
                restarted.on_stop()

            self.assertEqual(restarted.submitted_orders, [])
            self.assertEqual(
                restarted.denials[-1].reason,
                "intent_exchange_confirmation_required",
            )
            replayed = inbox.get(identity)
            self.assertTrue(replayed)
            self.assertEqual(
                replayed.state,
                IntentExecutionState.DISPATCHED,
            )

    def test_durable_confirmation_failure_sticky_halts_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            intent = _durable_entry_intent()
            identity = _durable_identity(intent)
            inbox = JsonIntentExecutionInbox(
                state_path / "intent-execution-inbox.json"
            )
            inbox.register_received(identity, _durable_payload(intent))
            strategy = _DurableIntentStrategy(state_path)
            strategy._handle_intent(intent)
            client_order_id = encode_client_order_id(intent.intent_id)
            halt_reasons: list[str] = []
            strategy.set_terminal_exchange_worker(
                False,
                halt_reasons.append,
            )

            def fail_confirmation(_client_order_id: str) -> bool:
                raise OSError("fsync failed")

            strategy._intent_execution_inbox.mark_exchange_confirmed_by_client_order_id = (
                fail_confirmation
            )
            try:
                strategy.on_order_accepted(
                    SimpleNamespace(client_order_id=client_order_id)
                )
                self.assertTrue(
                    _wait_until(
                        lambda: bool(halt_reasons),
                        timeout=1.0,
                    )
                )
                strategy.on_order_accepted(
                    SimpleNamespace(client_order_id=client_order_id)
                )
                time.sleep(0.02)
            finally:
                strategy.on_stop()

            self.assertEqual(len(halt_reasons), 1)
            self.assertIn("fsync failed", halt_reasons[0])
            self.assertEqual(strategy._trading_state(), "HALTED")

    def test_durable_confirmation_queue_full_sticky_halts_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            intent = _durable_entry_intent()
            identity = _durable_identity(intent)
            inbox = JsonIntentExecutionInbox(
                state_path / "intent-execution-inbox.json"
            )
            inbox.register_received(identity, _durable_payload(intent))
            strategy = _TinyDurableIntentStrategy(state_path)
            strategy._handle_intent(intent)
            client_order_id = encode_client_order_id(intent.intent_id)
            halt_reasons: list[str] = []
            started = Event()
            release = Event()
            strategy.set_terminal_exchange_worker(
                False,
                halt_reasons.append,
            )
            original_confirmation = (
                strategy._intent_execution_inbox
                .mark_exchange_confirmed_by_client_order_id
            )

            def block_confirmation(target: str) -> bool:
                started.set()
                release.wait(timeout=1.0)
                return original_confirmation(target)

            strategy._intent_execution_inbox.mark_exchange_confirmed_by_client_order_id = (
                block_confirmation
            )
            try:
                strategy.on_order_accepted(
                    SimpleNamespace(client_order_id=client_order_id)
                )
                self.assertTrue(started.wait(timeout=1.0))
                strategy.on_order_accepted(
                    SimpleNamespace(client_order_id=client_order_id)
                )
                strategy.on_order_accepted(
                    SimpleNamespace(client_order_id=client_order_id)
                )
                strategy.on_order_accepted(
                    SimpleNamespace(client_order_id=client_order_id)
                )

                self.assertEqual(len(halt_reasons), 1)
                self.assertIn(
                    "queue capacity exceeded",
                    halt_reasons[0],
                )
                self.assertEqual(strategy._trading_state(), "HALTED")
            finally:
                release.set()
                strategy.on_stop()

    def test_durable_confirmation_timeout_sticky_halts_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir)
            intent = _durable_entry_intent()
            identity = _durable_identity(intent)
            inbox = JsonIntentExecutionInbox(
                state_path / "intent-execution-inbox.json"
            )
            inbox.register_received(identity, _durable_payload(intent))
            strategy = _TimeoutDurableIntentStrategy(state_path)
            strategy._handle_intent(intent)
            client_order_id = encode_client_order_id(intent.intent_id)
            halt_reasons: list[str] = []
            started = Event()
            release = Event()
            strategy.set_terminal_exchange_worker(
                False,
                halt_reasons.append,
            )

            def block_confirmation(_target: str) -> bool:
                started.set()
                release.wait(timeout=1.0)
                return True

            strategy._intent_execution_inbox.mark_exchange_confirmed_by_client_order_id = (
                block_confirmation
            )
            try:
                strategy.on_order_accepted(
                    SimpleNamespace(client_order_id=client_order_id)
                )
                self.assertTrue(started.wait(timeout=1.0))
                self.assertTrue(
                    _wait_until(
                        lambda: bool(halt_reasons),
                        timeout=1.0,
                    )
                )
                time.sleep(0.03)

                self.assertEqual(len(halt_reasons), 1)
                self.assertIn("task timeout", halt_reasons[0])
                self.assertEqual(strategy._trading_state(), "HALTED")
            finally:
                release.set()
                strategy.on_stop()

    def test_sticky_halt_discards_late_prepare_submit_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _DurableIntentStrategy(Path(state_dir))
            intent = _durable_entry_intent()
            identity = _durable_identity(intent)
            plan = OrderPlan(
                intent_id=str(intent.intent_id),
                client_order_id=encode_client_order_id(
                    intent.intent_id
                ),
                tags=(f"intent_id={intent.intent_id}",),
                instrument_id=intent.instrument_id,
                side="BUY",
                order_type="LIMIT",
                quantity="0.1",
                price="100",
                time_in_force="IOC",
                reduce_only=False,
            )
            strategy._entry_protection_stash = {
                str(intent.intent_id): {"state": "staged"}
            }
            strategy._durable_entry_prepare_active = True
            strategy._durable_entry_prepare_task_queued = True
            strategy._durable_entry_prepare_preimage = {}
            result = _DurableIoResult(
                task=_DurableIoTask(
                    kind=_DurableIoTaskKind.PREPARE_SUBMIT,
                    operation_id=uuid4().hex,
                    intent=intent,
                    intent_execution=identity,
                    client_order_ids=(plan.client_order_id,),
                    plans=(plan,),
                    continuation={
                        "kind": "prepare_submit",
                        "mode": "single",
                    },
                ),
                outcome={},
            )

            try:
                strategy._halt_durable_io("later task fsync failed")
                strategy._on_durable_io_result(result)
            finally:
                strategy.on_stop()

            self.assertEqual(strategy.submitted_orders, [])
            self.assertEqual(strategy._entry_protection_stash, {})

    def test_discard_late_zone_ladder_result_releases_prebuilt_orders(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _DurableIntentStrategy(Path(state_dir))
            result = _prepared_zone_ladder_result(
                _durable_entry_intent()
            )
            operation_id = result.task.operation_id
            strategy._prepared_zone_ladder_orders[operation_id] = (
                SimpleNamespace(client_order_id="order-a"),
                SimpleNamespace(client_order_id="order-b"),
                SimpleNamespace(client_order_id="order-c"),
            )
            strategy._entry_protection_stash = {
                "pending": {"state": "staged"}
            }
            strategy._durable_entry_prepare_active = True
            strategy._durable_entry_prepare_task_queued = True
            strategy._durable_entry_prepare_preimage = {}
            strategy._pending_durable_entry_intents.append(
                (_durable_entry_intent(), False)
            )

            try:
                strategy._discard_durable_io_result(result)
                self.assertEqual(
                    strategy._prepared_zone_ladder_orders,
                    {},
                )
                self.assertEqual(
                    strategy._pending_durable_entry_intents,
                    [],
                )
                self.assertEqual(
                    strategy._entry_protection_stash,
                    {},
                )
                self.assertFalse(
                    strategy._durable_entry_prepare_active
                )
            finally:
                strategy.on_stop()

    def test_actor_mailbox_drain_releases_orders_after_worker_halt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _DurableIntentStrategy(Path(state_dir))
            strategy._prepared_zone_ladder_orders["operation-a"] = (
                SimpleNamespace(client_order_id="order-a"),
            )
            strategy._prepared_zone_ladder_orders["operation-b"] = (
                SimpleNamespace(client_order_id="order-b"),
            )
            preimage = {"existing": {"state": "ready"}}
            strategy._entry_protection_stash = {
                **preimage,
                "pending": {"state": "staged"},
            }
            strategy._durable_entry_prepare_active = True
            strategy._durable_entry_prepare_task_queued = True
            strategy._durable_entry_prepare_preimage = preimage
            strategy._pending_durable_entry_intents.append(
                (_durable_entry_intent(), False)
            )

            try:
                strategy._halt_durable_io("worker timeout")
                strategy.drain_durable_io_mailbox()
                self.assertEqual(
                    strategy._prepared_zone_ladder_orders,
                    {},
                )
                self.assertEqual(
                    strategy._pending_durable_entry_intents,
                    [],
                )
                self.assertEqual(
                    strategy._entry_protection_stash,
                    preimage,
                )
                self.assertFalse(
                    strategy._durable_entry_prepare_active
                )
            finally:
                strategy.on_stop()

    def test_worker_halt_skips_queued_prepare_persistence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _DurableIntentStrategy(Path(state_dir))
            result = _prepared_zone_ladder_result(
                _durable_entry_intent()
            )
            task = result.task
            operation_id = task.operation_id
            strategy._prepared_zone_ladder_orders[operation_id] = (
                SimpleNamespace(client_order_id="order-a"),
            )
            strategy._entry_protection_stash = {
                "pending": {"state": "staged"}
            }
            strategy._durable_entry_prepare_active = True
            strategy._durable_entry_prepare_task_queued = True
            strategy._durable_entry_prepare_preimage = {}
            strategy._pending_durable_entry_intents.append(
                (_durable_entry_intent(), False)
            )

            try:
                strategy._halt_durable_io("earlier worker failure")
                with patch.object(
                    strategy,
                    "_process_prepare_submit_task",
                ) as process:
                    strategy._process_durable_io_task(task)
                process.assert_not_called()
                strategy.drain_durable_io_mailbox()
                self.assertEqual(
                    strategy._prepared_zone_ladder_orders,
                    {},
                )
                self.assertEqual(
                    strategy._pending_durable_entry_intents,
                    [],
                )
                self.assertEqual(
                    strategy._entry_protection_stash,
                    {},
                )
            finally:
                strategy.on_stop()

    def test_operator_halt_blocks_late_risk_increasing_prepare_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _DurableIntentStrategy(Path(state_dir))
            intent = _durable_entry_intent()
            result = _prepared_submit_result(intent)
            strategy.set_trading_state_getter(lambda: "HALTED")

            try:
                strategy._on_durable_io_result(result)
            finally:
                strategy.on_stop()

            self.assertEqual(strategy.submitted_orders, [])
            self.assertEqual(
                strategy.denials[-1].reason,
                "trading_not_active",
            )

    def test_strategy_stop_discards_late_prepare_timer_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _DurableIntentStrategy(Path(state_dir))
            intent = _durable_entry_intent()
            strategy._durable_io_mailbox.put_nowait(
                _prepared_submit_result(intent)
            )

            strategy.on_stop()
            strategy.on_event(
                SimpleNamespace(name="strategy.durable-io.mailbox")
            )

            self.assertEqual(strategy.submitted_orders, [])
            self.assertTrue(strategy._durable_io_mailbox.empty())
            self.assertFalse(
                strategy._submit_durable_io_task(
                    _DurableIoTask(
                        kind=_DurableIoTaskKind.INTENT_RECEIVE,
                    )
                )
            )
            self.assertFalse(
                strategy._durable_io_worker.snapshot().running
            )

    def test_strategy_stop_releases_unpublished_zone_ladder_orders(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _DurableIntentStrategy(Path(state_dir))
            strategy._prepared_zone_ladder_orders["operation-a"] = (
                SimpleNamespace(client_order_id="order-a"),
            )
            preimage = {"existing": {"state": "ready"}}
            strategy._entry_protection_stash = {
                **preimage,
                "pending": {"state": "staged"},
            }
            strategy._durable_entry_prepare_active = True
            strategy._durable_entry_prepare_task_queued = True
            strategy._durable_entry_prepare_preimage = preimage
            strategy._pending_durable_entry_intents.append(
                (_durable_entry_intent(), False)
            )

            strategy.on_stop()

            self.assertEqual(
                strategy._prepared_zone_ladder_orders,
                {},
            )
            self.assertEqual(
                strategy._pending_durable_entry_intents,
                [],
            )
            self.assertEqual(
                strategy._entry_protection_stash,
                preimage,
            )
            self.assertFalse(
                strategy._durable_entry_prepare_active
            )

    def test_venue_backed_terminal_events_remain_exchange_confirmations(
        self,
    ) -> None:
        callbacks = (
            "on_order_rejected",
            "on_order_canceled",
            "on_order_expired",
            "on_order_filled",
        )
        for callback_name in callbacks:
            with self.subTest(callback=callback_name):
                with tempfile.TemporaryDirectory() as state_dir:
                    state_path = Path(state_dir)
                    intent = _durable_entry_intent()
                    identity = _durable_identity(intent)
                    inbox = JsonIntentExecutionInbox(
                        state_path / "intent-execution-inbox.json"
                    )
                    inbox.register_received(
                        identity,
                        _durable_payload(intent),
                    )
                    strategy = _DurableIntentStrategy(state_path)
                    strategy._handle_intent(intent)
                    client_order_id = encode_client_order_id(
                        intent.intent_id
                    )

                    try:
                        callback = getattr(strategy, callback_name)
                        callback(
                            SimpleNamespace(
                                client_order_id=client_order_id
                            )
                        )
                        self.assertTrue(
                            _wait_until(
                                lambda: (
                                    inbox.get(identity).state
                                    is IntentExecutionState.EXCHANGE_CONFIRMED
                                ),
                                timeout=1.0,
                            )
                        )
                    finally:
                        strategy.on_stop()

    def test_live_canary_loss_breach_halts_and_closes_exact_fill_quantity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _CanarySubmitStrategy(Path(state_dir))
            identity = _canary_execution_identity()
            plan = _canary_order_plan(identity)
            tasks: list[dict[str, object]] = []
            halt_reasons: list[str] = []
            strategy.set_live_canary_risk_reporter(
                lambda task: tasks.append(task) is None
            )
            strategy.set_live_canary_halt_handler(halt_reasons.append)
            strategy._cache_mark_price = lambda instrument_id: (  # type: ignore[method-assign]
                SimpleNamespace(
                    value="99",
                    ts_event=1786190400000000000,
                )
            )

            self.assertTrue(
                strategy._submit_order_plan(
                    plan,
                    live_canary_execution=identity,
                )
            )
            strategy.on_order_filled(
                SimpleNamespace(
                    trade_id="entry-trade",
                    client_order_id=identity.client_order_id,
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    side="BUY",
                    last_qty="0.1",
                    last_px="100",
                    commission=SimpleNamespace(
                        amount="0.01",
                        currency=SimpleNamespace(code="USDT"),
                    ),
                    reduce_only=False,
                    ts_event=1,
                )
            )

            task_index = 0
            while task_index < len(tasks):
                task = tasks[task_index]
                task_index += 1
                decisions = strategy.process_live_canary_risk_task(task)
                for decision in decisions:
                    strategy._on_live_canary_loss_decision(decision)

            self.assertEqual(
                strategy.submitted_orders,
                [identity.client_order_id],
            )
            strategy._cache_mark_price = lambda instrument_id: (  # type: ignore[method-assign]
                SimpleNamespace(
                    value="80",
                    ts_event=1786190401000000000,
                )
            )
            strategy._queue_live_canary_mark_checks()
            while task_index < len(tasks):
                task = tasks[task_index]
                task_index += 1
                decisions = strategy.process_live_canary_risk_task(task)
                for decision in decisions:
                    strategy._on_live_canary_loss_decision(decision)

            close_id = encode_client_order_id(
                UUID(identity.intent_id),
                sequence=99,
            )
            self.assertEqual(
                strategy.submitted_orders,
                [identity.client_order_id, close_id],
            )
            self.assertTrue(halt_reasons)
            self.assertIn(
                "cumulative loss limit reached",
                halt_reasons[-1],
            )
            record = strategy.live_canary_store.get(identity)
            self.assertTrue(record)
            self.assertEqual(record.state.value, "close_dispatched")
            self.assertEqual(record.close_required_quantity, "0.1")

    def test_live_canary_fill_uses_order_mark_after_18ms_cache_gap(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _CanarySubmitStrategy(Path(state_dir))
            identity = _canary_execution_identity()
            plan = _canary_order_plan(identity)
            tasks: list[dict[str, object]] = []
            halt_reasons: list[str] = []
            mark_calls = 0
            submitted_at = datetime(
                2026,
                8,
                8,
                12,
                tzinfo=timezone.utc,
            )

            def transient_mark(_instrument_id: str):
                nonlocal mark_calls
                mark_calls += 1
                if mark_calls == 1:
                    return SimpleNamespace(
                        value="99",
                        ts_event=int(
                            (
                                submitted_at
                                - timedelta(seconds=9, milliseconds=995)
                            ).timestamp()
                            * 1_000_000_000
                        ),
                    )
                return False

            strategy.set_live_canary_risk_reporter(
                lambda task: tasks.append(task) is None
            )
            strategy.set_live_canary_halt_handler(halt_reasons.append)
            strategy._cache_mark_price = transient_mark  # type: ignore[method-assign]

            self.assertTrue(
                strategy._submit_order_plan(
                    plan,
                    live_canary_execution=identity,
                )
            )
            strategy._now = lambda: (  # type: ignore[method-assign]
                submitted_at + timedelta(milliseconds=18)
            )
            strategy.on_order_filled(
                SimpleNamespace(
                    trade_id="entry-trade",
                    client_order_id=identity.client_order_id,
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    side="BUY",
                    last_qty="0.1",
                    last_px="100",
                    commission=SimpleNamespace(
                        amount="0.01",
                        currency=SimpleNamespace(code="USDT"),
                    ),
                    reduce_only=False,
                    ts_event=int(
                        (
                            submitted_at
                            + timedelta(milliseconds=18)
                        ).timestamp()
                        * 1_000_000_000
                    ),
                )
            )

            for task in tasks:
                decisions = strategy.process_live_canary_risk_task(task)
                self.assertEqual(decisions, ())

            fill_tasks = [
                task
                for task in tasks
                if task.get("kind") == "fill"
            ]
            self.assertEqual(len(fill_tasks), 1)
            fill = fill_tasks[0]["fill"]
            self.assertEqual(fill["mark_price_usdt"], "99")
            self.assertEqual(fill["accounting_error"], "")
            self.assertEqual(mark_calls, 2)
            self.assertEqual(halt_reasons, [])
            self.assertEqual(
                strategy.submitted_orders,
                [identity.client_order_id],
            )
            record = strategy.live_canary_store.get(identity)
            self.assertTrue(record)
            self.assertEqual(record.state.value, "exchange_confirmed")
            self.assertEqual(record.last_mark_price_usdt, "99")

    def test_consumed_canary_baseline_drift_halts_and_closes_position(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _CanarySubmitStrategy(Path(state_dir))
            identity = _canary_execution_identity()
            plan = _canary_order_plan(identity)
            tasks: list[dict[str, object]] = []
            halt_reasons: list[str] = []
            strategy.set_live_canary_risk_reporter(
                lambda task: tasks.append(task) is None
            )
            strategy.set_live_canary_halt_handler(halt_reasons.append)
            strategy._cache_mark_price = lambda instrument_id: (  # type: ignore[method-assign]
                SimpleNamespace(
                    value="99",
                    ts_event=1786190400000000000,
                )
            )
            self.assertTrue(
                strategy._submit_order_plan(
                    plan,
                    live_canary_execution=identity,
                )
            )
            strategy.on_order_filled(
                SimpleNamespace(
                    trade_id="entry-trade",
                    client_order_id=identity.client_order_id,
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    side="BUY",
                    last_qty="0.1",
                    last_px="100",
                    commission=SimpleNamespace(
                        amount="0.01",
                        currency=SimpleNamespace(code="USDT"),
                    ),
                    reduce_only=False,
                    ts_event=1,
                )
            )
            task_index = 0
            while task_index < len(tasks):
                decisions = strategy.process_live_canary_risk_task(
                    tasks[task_index]
                )
                task_index += 1
                for decision in decisions:
                    strategy._on_live_canary_loss_decision(decision)

            strategy.set_live_canary_portfolio_baseline_getter(
                lambda symbol: "5" * 64
            )
            strategy._queue_live_canary_mark_checks()
            while task_index < len(tasks):
                decisions = strategy.process_live_canary_risk_task(
                    tasks[task_index]
                )
                task_index += 1
                for decision in decisions:
                    strategy._on_live_canary_loss_decision(decision)

            self.assertTrue(halt_reasons)
            self.assertIn(
                "portfolio baseline drifted after permit consumption",
                halt_reasons[-1],
            )
            record = strategy.live_canary_store.get(identity)
            self.assertTrue(record)
            self.assertEqual(record.state.value, "close_dispatched")
            self.assertEqual(record.close_required_quantity, "0.1")

    def test_live_account_a_strategy_rejects_add_position(self) -> None:
        strategy = IntentExecutionStrategy(
            IntentExecutionStrategyConfig(
                account_id="account-a",
                node_id="node-a",
                trading_state="ACTIVE",
                environment="live",
                release_id="release-a",
            )
        )
        intent = SimpleNamespace(
            intent_id=uuid4(),
            instrument_id="BTCUSDT-PERP.BINANCE",
            risk_budget=SimpleNamespace(max_notional="1"),
        )

        denial = strategy._live_canary_intent_denial(
            intent,
            action="add_position",
            order_plan={},
        )

        self.assertEqual(denial.reason, "live_open_gate_unavailable")

    def test_protection_rejection_is_persisted_before_retry_without_advancing_revision(self) -> None:
        intent_id = uuid4()
        client_order_id = encode_client_order_id(intent_id, sequence=22)
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _ProtectionTerminalStrategy(Path(state_dir))
            strategy._entry_protection_stash[str(intent_id)] = {
                "instrument_id": "ATOMUSDT-PERP.BINANCE",
                "protection_sequence_start": 11,
                "protection_revision": 2,
                "protection_ids": (client_order_id,),
                "protection_roles": {
                    client_order_id: {
                        "role": "take_profit",
                        "tp_price": "6.75",
                        "quantity": "144.17",
                        "submitted_at": "",
                    }
                },
                "pending_cancel_ids": (),
            }
            event = SimpleNamespace(
                event_type="OrderRejected",
                client_order_id=client_order_id,
                instrument_id="ATOMUSDT-PERP.BINANCE",
                reason="Filter failure: PERCENT_PRICE (-4131)",
                ts_event=1785312000000000000,
            )

            try:
                strategy.on_order_rejected(event)
                self.assertTrue(
                    _pump_durable_until(
                        strategy,
                        lambda: bool(strategy.scheduled_delays),
                        timeout=1.0,
                    )
                )

                terminal = strategy._entry_protection_stash[str(intent_id)][
                    "last_protection_terminal_event"
                ]
                self.assertEqual(terminal["event_type"], "OrderRejected")
                self.assertEqual(terminal["reason"], "Filter failure: PERCENT_PRICE (-4131)")
                self.assertEqual(terminal["error_code"], "-4131")
                self.assertEqual(terminal["role"], "take_profit")
                self.assertEqual(terminal["tp_price"], "6.75")
                self.assertEqual(terminal["protection_revision"], 2)
                self.assertEqual(terminal["ts_event"], "1785312000000000000")
                self.assertEqual(
                    strategy._entry_protection_stash[str(intent_id)]["protection_revision"],
                    2,
                )
                self.assertEqual(
                    strategy._entry_protection_stash[str(intent_id)]["sync_retries"],
                    1,
                )
                self.assertEqual(
                    strategy.scheduled_delays,
                    [strategy._PROTECTION_SYNC_DELAY_S],
                )
                self.assertEqual(strategy.submitted_plans, [])
                persisted = strategy.persisted_before_schedule
                persisted_terminal = persisted[str(intent_id)][
                    "last_protection_terminal_event"
                ]
                self.assertEqual(persisted_terminal, terminal)
            finally:
                strategy.on_stop()

    def test_protection_fsync_does_not_block_order_callback_or_retry_early(
        self,
    ) -> None:
        intent_id = uuid4()
        client_order_id = encode_client_order_id(intent_id, sequence=22)
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _ProtectionTerminalStrategy(Path(state_dir))
            strategy._entry_protection_stash[str(intent_id)] = {
                "instrument_id": "ATOMUSDT-PERP.BINANCE",
                "protection_sequence_start": 11,
                "protection_revision": 2,
                "protection_ids": (client_order_id,),
                "protection_roles": {
                    client_order_id: {
                        "role": "take_profit",
                        "tp_price": "6.75",
                        "quantity": "144.17",
                        "submitted_at": "",
                    }
                },
                "pending_cancel_ids": (),
            }
            event = SimpleNamespace(
                event_type="OrderRejected",
                client_order_id=client_order_id,
                instrument_id="ATOMUSDT-PERP.BINANCE",
                reason="Filter failure: PERCENT_PRICE (-4131)",
            )
            fsync_started = Event()
            release_fsync = Event()
            strategy_module = __import__(
                "strategy.intent_execution_strategy",
                fromlist=["os"],
            )
            original_fsync = strategy_module.os.fsync

            def blocking_fsync(fd: int) -> None:
                fsync_started.set()
                release_fsync.wait(timeout=1.0)
                original_fsync(fd)

            try:
                with patch.object(
                    strategy_module.os,
                    "fsync",
                    blocking_fsync,
                ):
                    started_at = time.monotonic()
                    strategy.on_order_rejected(event)
                    elapsed = time.monotonic() - started_at

                    self.assertLess(elapsed, 0.01)
                    self.assertTrue(fsync_started.wait(timeout=1.0))
                    self.assertEqual(strategy.scheduled_delays, [])
                    release_fsync.set()
                    self.assertTrue(
                        _pump_durable_until(
                            strategy,
                            lambda: bool(strategy.scheduled_delays),
                            timeout=1.0,
                        )
                    )
            finally:
                release_fsync.set()
                strategy.on_stop()

    def test_mit_immediate_trigger_rejection_submits_reduce_only_market_once(self) -> None:
        intent_id = uuid4()
        source_client_order_id = encode_client_order_id(intent_id, sequence=22)
        tags = (
            f"intent_id={intent_id}",
            "lifecycle_role=take_profit",
            "position_id=ATOMUSDT-PERP.BINANCE-LONG",
        )
        with tempfile.TemporaryDirectory() as state_dir:
            strategy = _ProtectionTerminalStrategy(Path(state_dir))
            reported_events: list[dict] = []
            strategy.set_protection_event_reporter(
                lambda event: reported_events.append(event) is None or True
            )
            strategy._entry_protection_stash[str(intent_id)] = {
                "instrument_id": "ATOMUSDT-PERP.BINANCE",
                "entry_side": "BUY",
                "entry_tags": tags,
                "take_profits": ({"price": "6.75"},),
                "take_profit_quantities": ("144.17",),
                "tp_consumed": {},
                "protection_sequence_start": 11,
                "protection_revision": 2,
                "protection_ids": (source_client_order_id,),
                "protection_roles": {
                    source_client_order_id: {
                        "role": "take_profit",
                        "tp_price": "6.75",
                        "quantity": "144.17",
                        "submitted_at": "",
                        "order_type": "MARKET_IF_TOUCHED",
                        "side": "SELL",
                        "tags": tags,
                    }
                },
                "pending_cancel_ids": (),
            }
            event = SimpleNamespace(
                event_type="OrderRejected",
                client_order_id=source_client_order_id,
                instrument_id="ATOMUSDT-PERP.BINANCE",
                order_type="MARKET_IF_TOUCHED",
                side="SELL",
                tags=tags,
                reason="Order would immediately trigger. (-2021)",
            )

            try:
                strategy.on_order_rejected(event)
                strategy.on_order_rejected(event)
                self.assertTrue(
                    _pump_durable_until(
                        strategy,
                        lambda: len(strategy.submitted_plans) == 1,
                        timeout=1.0,
                    )
                )

                self.assertEqual(len(strategy.submitted_plans), 1)
                fallback_plan = strategy.submitted_plans[0]
                self.assertEqual(fallback_plan.order_type, "MARKET")
                self.assertEqual(fallback_plan.quantity, "144.17")
                self.assertEqual(fallback_plan.side, "SELL")
                self.assertTrue(fallback_plan.reduce_only)
                self.assertEqual(fallback_plan.tags, tags)
                self.assertTrue(fallback_plan.client_order_id.startswith("M"))
                self.assertEqual(
                    fallback_plan.client_order_id[1:],
                    source_client_order_id[1:],
                )
                self.assertEqual(strategy.scheduled_delays, [])
                self.assertEqual(len(reported_events), 1)
                self.assertEqual(
                    reported_events[0]["event_type"],
                    "TakeProfitImmediateMarketFallback",
                )
                remaining = strategy._take_profit_remaining_quantities(
                    strategy._entry_protection_stash[str(intent_id)],
                    ({"price": "6.75"},),
                    "144.17",
                    "0.01",
                )
                self.assertEqual(remaining, (None,))

                strategy.on_order_filled(
                    SimpleNamespace(
                        client_order_id=fallback_plan.client_order_id,
                        instrument_id="ATOMUSDT-PERP.BINANCE",
                        last_qty="144.17",
                    )
                )
                self.assertTrue(
                    strategy.wait_for_durable_io(
                        timeout_seconds=1.0
                    )
                )

                stash = strategy._entry_protection_stash[str(intent_id)]
                fallback_state = stash["tp_market_fallbacks"][
                    fallback_plan.client_order_id
                ]
                self.assertEqual(fallback_state["status"], "filled")
                self.assertEqual(fallback_state["remaining_quantity"], "0")
                self.assertEqual(stash["tp_consumed"]["6.75"], "144.17")
            finally:
                strategy.on_stop()

    def test_revisions_exhausted_emits_one_protection_frozen_event(self) -> None:
        intent_id = uuid4()
        strategy = IntentExecutionStrategy(
            IntentExecutionStrategyConfig(
                account_id="account-a",
                trading_state="ACTIVE",
            )
        )
        reported_events: list[dict] = []
        strategy.set_protection_event_reporter(
            lambda event: reported_events.append(event) is None or True
        )
        stash = {
            "instrument_id": "ATOMUSDT-PERP.BINANCE",
            "protection_revision": strategy._PROTECTION_MAX_REVISION,
        }

        strategy._normalize_protection_stash(str(intent_id), stash)
        strategy._normalize_protection_stash(str(intent_id), stash)

        self.assertEqual(stash["protection_frozen"], "revisions_exhausted")
        self.assertEqual(
            stash["protection_freeze_denial_reason"],
            "protection_revisions_exhausted",
        )
        self.assertEqual(len(reported_events), 1)
        self.assertEqual(reported_events[0]["event_type"], "ProtectionFrozen")
        self.assertEqual(
            reported_events[0]["payload"]["reason"],
            "revisions_exhausted",
        )


class _ProtectionTerminalStrategy(IntentExecutionStrategy):
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self.scheduled_delays: list[float | None] = []
        self.persisted_before_schedule: dict = {}
        self.submitted_plans: list = []
        super().__init__(
            IntentExecutionStrategyConfig(account_id="account-a", trading_state="ACTIVE")
        )

    def _protection_stash_path(self) -> str:
        return str(self._state_dir / self._PROTECTION_STASH_FILENAME)

    def _now(self) -> datetime:
        return datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    def _schedule_protection_sync(
        self,
        _intent_key: str,
        delay_seconds: float | None = None,
    ) -> None:
        with open(self._protection_stash_path(), "r") as fh:
            self.persisted_before_schedule = json.load(fh)
        self.scheduled_delays.append(delay_seconds)

    def _submit_order_plan(self, plan) -> bool:
        self.submitted_plans.append(plan)
        return True


class _LiveEntryMarkSubscriptionStrategy(IntentExecutionStrategy):
    def __init__(
        self,
        *,
        environment: str,
        inventory: tuple[tuple[str, str], ...],
        fail_instrument_id: str = "",
    ) -> None:
        self.mark_subscriptions: list[str] = []
        self.fail_instrument_id = fail_instrument_id
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id="account-a",
                node_id="node-a",
                trading_state="HALTED",
                environment=environment,
                release_id="release-a",
                live_entry_notional_inventory=inventory,
            )
        )

    def subscribe_mark_prices(
        self,
        instrument_id,
        client_id=None,
        params=None,
    ) -> None:
        del client_id, params
        instrument_text = str(instrument_id)
        if instrument_text == self.fail_instrument_id:
            raise RuntimeError("subscription rejected")
        self.mark_subscriptions.append(instrument_text)


class _TerminalExchangeStrategy(IntentExecutionStrategy):
    def __init__(self) -> None:
        self.command_results: list[dict[str, object]] = []
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id="account-a",
                node_id="node-a",
                trading_state="HALTED",
                environment="live",
                release_id="release-a",
            )
        )
        self.set_live_canary_portfolio_baseline_getter(
            lambda symbol: "4" * 64
        )

    def _publish_terminal_command_result(
        self,
        payload: dict[str, object],
    ) -> None:
        self.command_results.append(payload)

    def _all_open_positions(self):
        return ()


class _ProtectionWatchdogStrategy(IntentExecutionStrategy):
    def __init__(self) -> None:
        self.scheduled: list[float | None] = []
        self.reported_events: list[dict] = []
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id="account-a",
                node_id="node-a",
                trading_state="ACTIVE",
            )
        )
        self.set_protection_event_reporter(
            lambda event: self.reported_events.append(event) is None or True
        )

    def _cache_positions(self, instrument_id):
        del instrument_id
        return (
            SimpleNamespace(
                instrument_id="SOLUSDT-PERP.BINANCE",
                side="LONG",
                quantity="1",
                position_id="SOLUSDT-PERP.BINANCE-LONG",
                entry_price="100",
            ),
        )

    def _cache_orders_all(self, instrument_id):
        del instrument_id
        return ()

    def _schedule_protection_sync(
        self,
        _intent_key: str,
        delay_seconds: float | None = None,
    ) -> None:
        self.scheduled.append(delay_seconds)

    def _queue_entry_protection_stash_persist(
        self,
        *,
        continuation=False,
    ) -> bool:
        del continuation
        return True


class _SlowTerminalMirror:
    def __init__(
        self,
        *,
        client_order_id: str | None = None,
    ) -> None:
        self.refresh_count = 0
        if client_order_id is None:
            client_order_id = "B" + ("1" * 32) + "01"
        self._orders = (
            ExchangeOrderRef(
                account_id="account-a",
                symbol="SOLUSDT",
                position_side="LONG",
                order_kind="regular",
                venue_order_id="42",
                client_order_id=client_order_id,
                order_type="LIMIT",
                side="SELL",
                quantity="0.1",
                price="100",
                trigger_price=None,
                tags=(),
            ),
        )

    def refresh(self, **_kwargs):
        self.refresh_count += 1
        time.sleep(0.05)
        return self._orders


class _SlowTerminalAdapter:
    def __init__(self) -> None:
        self.cancel_count = 0

    def cancel(self, _action, request, **_kwargs):
        self.cancel_count += 1
        time.sleep(0.05)
        return CancelResult(
            account_id=request.account_id,
            symbol=request.symbol,
            position_side=request.position_side,
            order_kind=request.order_kind,
            outcome="canceled",
            terminal_status="CANCELED",
        )


class _CanarySubmitStrategy(IntentExecutionStrategy):
    def __init__(
        self,
        state_dir: Path,
        *,
        existing_order_ids: set[str] | None = None,
        account_id: str = "account-a",
        node_id: str = "node-a",
        release_id: str = "release-a",
    ) -> None:
        self._state_dir = state_dir
        self.submitted_orders: list[str] = []
        self.existing_order_ids = existing_order_ids or set()
        self.crash_before_dispatch_persist = False
        execution_path = state_dir / "live-canary-execution.json"
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id=account_id,
                node_id=node_id,
                trading_state="ACTIVE",
                environment="live",
                release_id=release_id,
                live_canary_execution_path=str(execution_path),
                live_entry_notional_inventory=(
                    ("BTCUSDT-PERP.BINANCE", "100"),
                ),
            )
        )
        self.set_live_canary_portfolio_baseline_getter(
            lambda symbol: "4" * 64
        )

    @property
    def live_canary_store(self) -> JsonLiveCanaryExecutionStore:
        return self._live_canary_execution_store

    def _protection_stash_path(self) -> str:
        return str(self._state_dir / self._PROTECTION_STASH_FILENAME)

    def _cache_instrument(self, instrument_id: str):
        return SimpleNamespace(id=instrument_id)

    def _cache_orders(self, instrument_id):
        del instrument_id
        return tuple(
            SimpleNamespace(client_order_id=client_order_id)
            for client_order_id in self.existing_order_ids
        )

    def _cache_mark_price(self, instrument_id: str):
        del instrument_id
        return SimpleNamespace(
            value="100",
            ts_event=int(self._now().timestamp() * 1_000_000_000),
        )

    def _build_nautilus_order(self, plan, instrument):
        del instrument
        return SimpleNamespace(
            client_order_id=plan.client_order_id,
            instrument_id=plan.instrument_id,
            quantity=plan.quantity,
            price=plan.price,
        )

    def _hedge_position_id(self, order, plan):
        del order, plan
        return None

    def submit_order(self, order, position_id=None) -> None:
        del position_id
        client_order_id = str(order.client_order_id)
        self.submitted_orders.append(client_order_id)
        self.existing_order_ids.add(client_order_id)

    def _mark_live_canary_dispatched(
        self,
        identity: LiveCanaryExecutionIdentity,
    ) -> None:
        if self.crash_before_dispatch_persist:
            raise SystemExit("crash before dispatch persist")
        super()._mark_live_canary_dispatched(identity)

    def _now(self) -> datetime:
        return datetime(2026, 8, 8, 12, tzinfo=timezone.utc)


class _CanaryMsgbusStrategy(_CanarySubmitStrategy):
    def _instrument_spec(self, instrument_id: str) -> InstrumentSpec:
        return InstrumentSpec(
            instrument_id=instrument_id,
            price_increment="0.01",
            quantity_increment="0.001",
        )

    def _stash_entry_protection(self, intent, plan) -> bool:
        del intent, plan
        return True

    def _now(self) -> datetime:
        return datetime(2026, 8, 8, 12, tzinfo=timezone.utc)


class _DurableIntentStrategy(IntentExecutionStrategy):
    def __init__(
        self,
        state_dir: Path,
        *,
        exchange_order_ids: set[str] | None = None,
        crash_after_submit: bool = False,
    ) -> None:
        self._state_dir = state_dir
        self.submitted_orders: list[str] = []
        self.exchange_order_ids = exchange_order_ids
        if self.exchange_order_ids is None:
            self.exchange_order_ids = set()
        self.crash_after_submit = crash_after_submit
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id="account-b",
                node_id="node-b",
                trading_state="ACTIVE",
                environment="testnet",
                intent_execution_inbox_path=str(
                    state_dir / "intent-execution-inbox.json"
                ),
            )
        )

    def _instrument_spec(self, instrument_id: str) -> InstrumentSpec:
        return InstrumentSpec(
            instrument_id=instrument_id,
            price_increment="0.01",
            quantity_increment="0.001",
        )

    def _cache_instrument(self, instrument_id: str):
        return SimpleNamespace(id=instrument_id)

    def _cache_orders(self, instrument_id):
        del instrument_id
        return tuple(
            SimpleNamespace(
                client_order_id=client_order_id,
                instrument_id="SOLUSDT-PERP.BINANCE",
                status="ACCEPTED",
            )
            for client_order_id in self.exchange_order_ids
        )

    def _cache_positions(self, instrument_id):
        del instrument_id
        return ()

    def _build_nautilus_order(self, plan, instrument):
        del instrument
        return SimpleNamespace(
            client_order_id=plan.client_order_id,
            instrument_id=plan.instrument_id,
            quantity=plan.quantity,
            price=plan.price,
        )

    def _hedge_position_id(self, order, plan):
        del order, plan
        return None

    def _stash_entry_protection(self, intent, plan) -> bool:
        del intent, plan
        return True

    def submit_order(self, order, position_id=None) -> None:
        del position_id
        client_order_id = str(order.client_order_id)
        self.submitted_orders.append(client_order_id)
        self.exchange_order_ids.add(client_order_id)
        if self.crash_after_submit:
            raise SystemExit("crash after exchange submit")

    def _now(self) -> datetime:
        return datetime(2026, 8, 8, 12, tzinfo=timezone.utc)


class _TinyDurableIntentStrategy(_DurableIntentStrategy):
    _DURABLE_IO_QUEUE_CAPACITY = 1


class _TimeoutDurableIntentStrategy(_DurableIntentStrategy):
    _DURABLE_IO_TASK_TIMEOUT_SECONDS = 0.02


class _LiveEntrySubmitStrategy(IntentExecutionStrategy):
    def __init__(
        self,
        *,
        inventory: tuple[tuple[str, str], ...],
        account_id: str = "account-b",
        node_id: str = "node-b",
        release_id: str = "",
        final_quantity: str | None = None,
        final_price: str | None = None,
        mark_price: str | bool = "100",
        mark_price_at: datetime | None = None,
        fallback_mark_price: str | bool = False,
        fallback_mark_price_at: datetime | None = None,
        state_dir: Path | None = None,
    ) -> None:
        self.submitted_orders: list[str] = []
        self.submitted_order_objects: list[object] = []
        self.built_orders: list[object] = []
        self._final_quantity = final_quantity
        self._final_price = final_price
        self._mark_price = mark_price
        self._mark_price_at = mark_price_at
        self._fallback_mark_price = fallback_mark_price
        self._fallback_mark_price_at = fallback_mark_price_at
        if self._mark_price_at is None:
            self._mark_price_at = datetime(
                2026,
                8,
                8,
                11,
                59,
                55,
                tzinfo=timezone.utc,
            )
        if (
            self._fallback_mark_price is not False
            and self._fallback_mark_price_at is None
        ):
            self._fallback_mark_price_at = datetime(
                2026,
                8,
                8,
                11,
                59,
                55,
                tzinfo=timezone.utc,
            )
        self._state_dir = state_dir
        live_canary_execution_path = ""
        intent_execution_inbox_path = ""
        if state_dir is not None:
            live_canary_execution_path = str(
                state_dir / "live-canary-execution.json"
            )
            intent_execution_inbox_path = str(
                state_dir / "intent-execution-inbox.json"
            )
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id=account_id,
                node_id=node_id,
                trading_state="ACTIVE",
                environment="live",
                release_id=release_id,
                live_canary_execution_path=live_canary_execution_path,
                intent_execution_inbox_path=intent_execution_inbox_path,
                live_entry_notional_inventory=inventory,
            )
        )
        self.set_live_entry_mark_snapshot_getter(
            self._fallback_mark_snapshot
        )

    def _cache_instrument(self, instrument_id: str):
        return SimpleNamespace(id=instrument_id)

    def _instrument_spec(self, instrument_id: str) -> InstrumentSpec:
        return InstrumentSpec(
            instrument_id=instrument_id,
            price_increment="0.01",
            quantity_increment="0.01",
        )

    def _protection_stash_path(self) -> str:
        if self._state_dir is None:
            return super()._protection_stash_path()
        return str(self._state_dir / self._PROTECTION_STASH_FILENAME)

    def _cache_mark_price(self, instrument_id: str):
        del instrument_id
        if self._mark_price is False or self._mark_price_at is None:
            return False
        return SimpleNamespace(
            value=self._mark_price,
            ts_event=int(self._mark_price_at.timestamp() * 1_000_000_000),
        )

    def _fallback_mark_snapshot(self, instrument_id: str):
        del instrument_id
        if (
            self._fallback_mark_price is False
            or self._fallback_mark_price_at is None
        ):
            return False
        return SimpleNamespace(
            value=self._fallback_mark_price,
            ts_event=int(
                self._fallback_mark_price_at.timestamp()
                * 1_000_000_000
            ),
        )

    def _cache_orders(self, instrument_id):
        del instrument_id
        return ()

    def _cache_positions(self, instrument_id):
        del instrument_id
        return ()

    def _build_nautilus_order(self, plan, instrument):
        del instrument
        quantity = plan.quantity
        if self._final_quantity is not None:
            quantity = self._final_quantity
        price = plan.price
        if self._final_price is not None:
            price = self._final_price
        order = SimpleNamespace(
            client_order_id=plan.client_order_id,
            instrument_id=plan.instrument_id,
            quantity=quantity,
            price=price,
        )
        self.built_orders.append(order)
        return order

    def _hedge_position_id(self, order, plan):
        del order, plan
        return None

    def submit_order(self, order, position_id=None) -> None:
        del position_id
        self.submitted_order_objects.append(order)
        self.submitted_orders.append(str(order.client_order_id))

    def _now(self) -> datetime:
        return datetime(2026, 8, 8, 12, tzinfo=timezone.utc)


def _canary_execution_identity(
    *,
    permit_id: str | None = None,
    intent_id: str | None = None,
    account_id: str = "account-a",
    node_id: str = "node-a",
    release_id: str = "release-a",
) -> LiveCanaryExecutionIdentity:
    normalized_intent_id = intent_id or str(uuid4())
    return LiveCanaryExecutionIdentity(
        permit_id=permit_id or str(uuid4()),
        release_id=release_id,
        intent_id=normalized_intent_id,
        client_order_id=encode_client_order_id(
            UUID(normalized_intent_id)
        ),
        account_id=account_id,
        node_id=node_id,
        symbol="BTCUSDT",
        max_notional_usdt="12",
        max_cumulative_loss_usdt="1.49",
        authorized_limit_price_usdt="100",
        expires_at="2026-08-12T00:00:00+00:00",
        portfolio_baseline_sha256="4" * 64,
    )


def _live_canary_intent(
    *,
    permit_id: str,
    account_id: str = "account-a",
    node_id: str = "node-a",
    release_id: str = "release-a",
    intent_id: UUID | None = None,
) -> SimpleNamespace:
    normalized_intent_id = intent_id or uuid4()
    return SimpleNamespace(
        intent_id=normalized_intent_id,
        decision_id=uuid4(),
        risk_decision_id=uuid4(),
        idempotency_key=str(uuid4()),
        account_id=account_id,
        instrument_id="BTCUSDT-PERP.BINANCE",
        action="open_position",
        valid_until=(
            datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
            + timedelta(minutes=5)
        ),
        risk_budget=SimpleNamespace(max_notional="12"),
        order_plan={
            "type": "limit",
            "side": "buy",
            "quantity": "0.1",
            "price": "100",
            "time_in_force": "IOC",
            "authorization": {
                "authorized_by_type": "user",
                "authorized_by_id": "canary-review",
                "source_message_id": str(uuid4()),
            },
            "canary_permit": {
                "permit_id": permit_id,
                "account_id": account_id,
                "node_id": node_id,
                "release_id": release_id,
                "symbol": "BTCUSDT",
                "max_notional_usdt": "12",
                "max_cumulative_loss_usdt": "1.49",
                "expires_at": "2026-08-12T00:00:00+00:00",
                "portfolio_baseline_sha256": "4" * 64,
            },
        },
    )


def _durable_entry_intent() -> SimpleNamespace:
    intent_id = uuid4()
    return SimpleNamespace(
        intent_id=intent_id,
        decision_id=uuid4(),
        risk_decision_id=uuid4(),
        idempotency_key=f"idempotency-{intent_id}",
        account_id="account-b",
        instrument_id="SOLUSDT-PERP.BINANCE",
        action="open_position",
        valid_until=(
            datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
            + timedelta(minutes=5)
        ),
        risk_budget=SimpleNamespace(max_notional="12"),
        order_plan={
            "type": "limit",
            "side": "buy",
            "quantity": "0.1",
            "price": "100",
            "time_in_force": "IOC",
            "authorization": {
                "authorized_by_type": "user",
                "authorized_by_id": "durable-test",
                "source_message_id": str(uuid4()),
            },
        },
    )


def _live_entry_intent(
    *,
    max_notional: str = "100",
) -> SimpleNamespace:
    intent_id = uuid4()
    return SimpleNamespace(
        intent_id=intent_id,
        decision_id=uuid4(),
        risk_decision_id=uuid4(),
        idempotency_key=f"idempotency-{intent_id}",
        account_id="account-b",
        instrument_id="BTCUSDT-PERP.BINANCE",
        action="open_position",
        valid_until=(
            datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
            + timedelta(minutes=5)
        ),
        risk_budget=SimpleNamespace(max_notional=max_notional),
        order_plan={
            "type": "limit",
            "side": "buy",
            "quantity": "1",
            "price": "100",
            "time_in_force": "IOC",
            "authorization": {
                "authorized_by_type": "user",
                "authorized_by_id": "dynamic-budget-test",
                "source_message_id": str(uuid4()),
            },
        },
    )


def _live_zone_ladder_intent(
    *,
    max_notional: str,
    tranche_quantity: str = "0.4",
) -> SimpleNamespace:
    intent = _live_entry_intent(max_notional=max_notional)
    intent.order_plan = {
        "type": "zone_ladder",
        "side": "buy",
        "authorization": {
            "authorized_by_type": "user",
            "authorized_by_id": "dynamic-budget-test",
            "source_message_id": str(uuid4()),
        },
        "tranches": [
            {"seq": 1, "quantity": tranche_quantity, "price": "100"},
            {"seq": 2, "quantity": tranche_quantity, "price": "100"},
            {"seq": 3, "quantity": tranche_quantity, "price": "100"},
        ],
    }
    return intent


def _durable_identity(intent: SimpleNamespace) -> IntentExecutionIdentity:
    return IntentExecutionIdentity(
        account_id=intent.account_id,
        intent_id=str(intent.intent_id),
        idempotency_key=intent.idempotency_key,
        instrument_id=intent.instrument_id,
        action=intent.action,
    )


def _durable_payload(intent: SimpleNamespace) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "intent_id": str(intent.intent_id),
        "decision_id": str(intent.decision_id),
        "risk_decision_id": str(intent.risk_decision_id),
        "idempotency_key": intent.idempotency_key,
        "account_id": intent.account_id,
        "instrument_id": intent.instrument_id,
        "action": intent.action,
        "valid_until": intent.valid_until.isoformat(),
        "risk_budget": {"max_notional": "12"},
        "order_plan": dict(intent.order_plan),
    }


def _prepared_submit_result(
    intent: SimpleNamespace,
) -> _DurableIoResult:
    plan = OrderPlan(
        intent_id=str(intent.intent_id),
        client_order_id=encode_client_order_id(intent.intent_id),
        tags=(f"intent_id={intent.intent_id}",),
        instrument_id=intent.instrument_id,
        side="BUY",
        order_type="LIMIT",
        quantity="0.1",
        price="100",
        time_in_force="IOC",
        reduce_only=False,
    )
    return _DurableIoResult(
        task=_DurableIoTask(
            kind=_DurableIoTaskKind.PREPARE_SUBMIT,
            intent=intent,
            intent_execution=_durable_identity(intent),
            client_order_ids=(plan.client_order_id,),
            plans=(plan,),
            continuation={
                "kind": "prepare_submit",
                "protection_preimage": {},
            },
        ),
        outcome={},
    )


def _prepared_zone_ladder_result(
    intent: SimpleNamespace,
) -> _DurableIoResult:
    plans = tuple(
        OrderPlan(
            intent_id=str(intent.intent_id),
            client_order_id=encode_client_order_id(
                intent.intent_id,
                sequence=sequence,
            ),
            tags=(f"intent_id={intent.intent_id}",),
            instrument_id=intent.instrument_id,
            side="BUY",
            order_type="LIMIT",
            quantity="0.1",
            price="100",
            time_in_force="IOC",
            reduce_only=False,
        )
        for sequence in (1, 2, 3)
    )
    return _DurableIoResult(
        task=_DurableIoTask(
            kind=_DurableIoTaskKind.PREPARE_SUBMIT,
            operation_id=uuid4().hex,
            intent=intent,
            intent_execution=_durable_identity(intent),
            client_order_ids=tuple(
                plan.client_order_id for plan in plans
            ),
            plans=plans,
            continuation={
                "kind": "prepare_submit",
                "mode": "zone_ladder",
                "protection_preimage": {},
            },
        ),
        outcome={},
    )


def _wait_until(predicate, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


def _pump_durable_until(strategy, predicate, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        strategy.drain_durable_io_mailbox()
        if predicate():
            return True
        time.sleep(0.001)
    strategy.drain_durable_io_mailbox()
    return bool(predicate())


def _canary_order_plan(
    identity: LiveCanaryExecutionIdentity,
    *,
    quantity: str = "0.1",
    price: str = "100",
    approved_max_notional: str = "12",
) -> OrderPlan:
    return OrderPlan(
        intent_id=UUID(identity.intent_id),
        client_order_id=identity.client_order_id,
        tags=(),
        instrument_id="BTCUSDT-PERP.BINANCE",
        side="BUY",
        order_type="LIMIT",
        quantity=quantity,
        price=price,
        time_in_force="IOC",
        approved_max_notional=approved_max_notional,
    )


def _live_entry_order_plan(
    *,
    instrument_id: str,
    quantity: str,
    price: str | None,
    order_type: str = "LIMIT",
    reduce_only: bool = False,
    approved_max_notional: str = "100000",
) -> OrderPlan:
    intent_id = uuid4()
    return OrderPlan(
        intent_id=intent_id,
        client_order_id=encode_client_order_id(intent_id),
        tags=(),
        instrument_id=instrument_id,
        side="BUY",
        order_type=order_type,
        quantity=quantity,
        price=price,
        time_in_force="IOC",
        reduce_only=reduce_only,
        approved_max_notional=approved_max_notional,
    )


def _canary_live_open_gate() -> dict[str, object]:
    return {
        "mode": "canary_only",
        "release_id": "release-a",
        "rollout_phase": "account_a_canary",
        "phase_version": 1,
    }


def _normal_live_open_gate() -> dict[str, object]:
    return {
        "mode": "normal",
        "release_id": "release-a",
        "rollout_phase": "fleet_complete",
        "phase_version": 5,
    }


if __name__ == "__main__":
    unittest.main()
