from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from threading import Event
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
)
from runtime.intent_execution_inbox import (  # noqa: E402
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

    def test_live_secondary_accounts_enter_canary_only_with_explicit_permit(
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
                self.assertEqual(
                    denial.reason,
                    "canary_permit_missing",
                )
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
                self.assertEqual(
                    identity_denial.reason,
                    "canary_permit_missing",
                )

                canary = _live_canary_intent(
                    permit_id=str(uuid4()),
                    account_id=account_id,
                    node_id=node_id,
                )
                canary.order_plan["canary_permit"]["expires_at"] = (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat()
                identity = _canary_execution_identity(
                    intent_id=str(canary.intent_id),
                    account_id=account_id,
                    node_id=node_id,
                )
                canary_plan = _canary_order_plan(identity)
                self.assertIsNone(
                    strategy._live_canary_intent_denial(
                        canary,
                        action="open_position",
                        order_plan=canary.order_plan,
                    )
                )
                parsed = strategy._live_canary_execution_identity(
                    canary,
                    canary_plan,
                )
                self.assertIsInstance(
                    parsed,
                    LiveCanaryExecutionIdentity,
                )
                self.assertEqual(parsed.account_id, account_id)
                self.assertEqual(parsed.node_id, node_id)

                cross_account = _live_canary_intent(
                    permit_id=str(uuid4()),
                    account_id="account-a",
                    node_id=node_id,
                )
                cross_account.order_plan["canary_permit"]["expires_at"] = (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat()
                denial = strategy._live_canary_intent_denial(
                    cross_account,
                    action="open_position",
                    order_plan=cross_account.order_plan,
                )
                self.assertEqual(
                    denial.reason,
                    "canary_identity_mismatch",
                )

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
            result = _DurableIoResult(
                task=_DurableIoTask(
                    kind=_DurableIoTaskKind.PREPARE_SUBMIT,
                    intent=intent,
                    intent_execution=identity,
                    client_order_ids=(plan.client_order_id,),
                    plans=(plan,),
                    continuation={
                        "kind": "prepare_submit",
                        "protection_preimage": {},
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

        self.assertEqual(denial.reason, "canary_open_position_only")

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


class _SlowTerminalMirror:
    def __init__(self) -> None:
        self.refresh_count = 0
        self._orders = (
            ExchangeOrderRef(
                account_id="account-a",
                symbol="SOLUSDT",
                position_side="LONG",
                order_kind="regular",
                venue_order_id="42",
                client_order_id="terminal-order",
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
        mark_price: str | None = None,
        mark_price_at: datetime | None = None,
    ) -> None:
        self.submitted_orders: list[str] = []
        self._final_quantity = final_quantity
        self._final_price = final_price
        self._mark_price = mark_price
        self._mark_price_at = mark_price_at
        super().__init__(
            IntentExecutionStrategyConfig(
                account_id=account_id,
                node_id=node_id,
                trading_state="ACTIVE",
                environment="live",
                release_id=release_id,
                live_entry_notional_inventory=inventory,
            )
        )

    def _cache_instrument(self, instrument_id: str):
        return SimpleNamespace(id=instrument_id)

    def _cache_mark_price(self, instrument_id: str):
        del instrument_id
        if self._mark_price is None or self._mark_price_at is None:
            return False
        return SimpleNamespace(
            value=self._mark_price,
            ts_event=int(self._mark_price_at.timestamp() * 1_000_000_000),
        )

    def _build_nautilus_order(self, plan, instrument):
        del instrument
        quantity = plan.quantity
        if self._final_quantity is not None:
            quantity = self._final_quantity
        price = plan.price
        if self._final_price is not None:
            price = self._final_price
        return SimpleNamespace(
            client_order_id=plan.client_order_id,
            instrument_id=plan.instrument_id,
            quantity=quantity,
            price=price,
        )

    def _hedge_position_id(self, order, plan):
        del order, plan
        return None

    def submit_order(self, order, position_id=None) -> None:
        del position_id
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
    )


def _live_entry_order_plan(
    *,
    instrument_id: str,
    quantity: str,
    price: str | None,
    order_type: str = "LIMIT",
    reduce_only: bool = False,
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
