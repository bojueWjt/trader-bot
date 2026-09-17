"""Replay ZEC m4403: an exit fill must finish the protection lifecycle."""
from __future__ import annotations

import copy
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "services/nautilus-node"), str(ROOT / "packages/execution-domain")]

from strategy.intent_execution_strategy import IntentExecutionStrategy, IntentExecutionStrategyConfig
from strategy.intent_execution_planner import encode_client_order_id


NOW = datetime(2026, 9, 17, 14, 29, 40, tzinfo=timezone.utc)
KEY = "06157fcc-b976-4a2f-8135-eb423a160a83"
INSTRUMENT = "ZECUSDT-PERP.BINANCE"
IDS = [encode_client_order_id(UUID(KEY), seq) for seq in (1, 31, 32, 33)]


class ClosedProtectionHarness(IntentExecutionStrategy):
    def __init__(self, directory):
        super().__init__(IntentExecutionStrategyConfig(
            account_id="account-d", trading_state="ACTIVE",
            intent_execution_inbox_path=str(Path(directory) / "inbox.json"),
        ))
        self.directory = directory
        self.requests = []
        self.repairs = []
        self.scheduled = []
        self.persisted = {}
        self.snapshot = {"fetched_at": NOW, "positions": [], "regular_orders": [], "algo_orders": []}
        terminal_ns = int((NOW - timedelta(seconds=6)).timestamp() * 1e9)
        self.orders = [SimpleNamespace(
            client_order_id=cid, instrument_id=INSTRUMENT,
            status="FILLED" if i < 2 else "ACCEPTED", ts_last=terminal_ns,
            filled_qty="1.394" if i < 2 else "0", quantity="1.394" if i < 2 else "0.697",
            side="SELL" if i == 0 else "BUY", position_side="SHORT",
            tags=(),
        ) for i, cid in enumerate(IDS)]
        self.snapshot["algo_orders"] = [
            {"client_order_id": cid, "symbol": "ZECUSDT", "position_side": "SHORT"}
            for cid in IDS[2:]
        ]
        auth = {"authorized_by_type": "channel", "authorized_by_id": "channel-d",
                "source_message_id": "m4403", "parent_intent_id": KEY}
        self._entry_protection_stash[KEY] = {
            "instrument_id": INSTRUMENT, "entry_side": "SELL", "entry_intent_id": KEY,
            "entry_sequence_max": 1, "protection_sequence_start": 11,
            "protection_revision": 2, "stop_loss": "1440", "take_profits": ["1231.23", "1201.2"],
            "entry_tags": (f"intent_id={KEY}",) + tuple(f"{k}={v}" for k, v in auth.items()),
            "stop_loss_authorization": auth, "take_profit_authorization": auth,
            "protection_ids": tuple(IDS[1:]), "protected_quantity": "1.394",
            "pending_cancel_ids": (), "protection_roles": {},
        }
        self.set_exchange_evidence_provider(SimpleNamespace(cached_snapshot=lambda **kw: self.snapshot,
                                                            snapshot=lambda **kw: self.snapshot))
        self._exchange_state_mirror = SimpleNamespace(find_order=self.find_order)
        self._terminal_exchange_worker = SimpleNamespace(new_deadline=lambda: 9999999999,
                                                         submit=self.submit_cancel)

    def find_order(self, instrument, cid):
        if instrument != INSTRUMENT:
            return False
        return SimpleNamespace(account_id="account-d", symbol="ZECUSDT", position_side="SHORT",
                               order_kind="algo", venue_order_id="123", client_order_id=cid)

    def submit_cancel(self, request):
        self.requests.append(request)
        return True

    def _now(self):
        return NOW

    def _cache_orders_all(self, instrument):
        return tuple(self.orders) if instrument == INSTRUMENT else ()

    def _cache_positions(self, instrument):
        return ()

    def _protection_stash_path(self):
        return str(Path(self.directory) / "protection_stash.json")

    def _queue_entry_protection_stash_persist(self, *, continuation=False):
        self.persisted = copy.deepcopy(self._entry_protection_stash_payload())
        self._write_entry_protection_stash(self.persisted)
        if continuation:
            self._run_protection_continuation(continuation)
        return True

    def _schedule_protection_sync(self, key, delay_seconds=None):
        self.scheduled.append(key)

    def _repair_missing_protection_orders(self, *args):
        self.repairs.append(args)
        return True


def test_flat_venue_cancels_remaining_tps_without_dropping_unconfirmed_state():
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        strategy._check_protection_watchdog(KEY)
        assert len(strategy.requests) == 1
        assert {r.client_order_id for r in strategy.requests[0].cancel_requests} == set(IDS[2:])
        assert KEY in strategy._entry_protection_stash
        assert KEY in strategy.persisted
        assert strategy.repairs == []


def test_position_closed_fences_old_open_snapshot_before_watchdog_repair():
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        strategy.snapshot["fetched_at"] = NOW - timedelta(seconds=5)
        strategy.snapshot["positions"] = [{"symbol": "ZECUSDT", "position_side": "SHORT", "quantity": "1.394"}]
        close_event(strategy)
        strategy._check_protection_watchdog(KEY)
        assert strategy.repairs == []
        assert strategy.requests == []


def close_event(strategy, book="SHORT"):
    strategy.on_position_closed(SimpleNamespace(
        instrument_id=INSTRUMENT, position_id=f"{INSTRUMENT}-{book}",
        ts_closed=int((NOW - timedelta(seconds=2)).timestamp() * 1e9),
    ))


def complete(strategy, statuses):
    request = strategy.requests[-1]
    outcomes = tuple(SimpleNamespace(request=r, status=status, terminal_status=terminal,
                                     error="" if status == "confirmed" else "unknown cancel")
                     for r, (status, terminal) in zip(request.cancel_requests, statuses))
    strategy._on_terminal_exchange_result(SimpleNamespace(
        request_id=request.request_id, account_id="account-d", cancel_outcomes=outcomes,
        error="", timed_out=False,
    ))


def test_cancel_confirmation_and_new_flat_snapshot_retire_stale_cache():
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        strategy._check_protection_watchdog(KEY)
        strategy._check_protection_watchdog(KEY)
        assert len(strategy.requests) == 1  # no duplicate batch while in flight
        complete(strategy, [("confirmed", "CANCELED"), ("confirmed", "CANCELED")])
        assert KEY in strategy._entry_protection_stash  # cancel receipt alone is not flat evidence
        strategy.snapshot["algo_orders"] = []
        strategy._check_protection_watchdog(KEY)
        assert KEY not in strategy._entry_protection_stash
        assert strategy.repairs == []


def test_unknown_cancel_stays_pending_across_restart_and_is_retried():
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        close_event(strategy)
        strategy._check_protection_watchdog(KEY)
        complete(strategy, [("confirmed", "CANCELED"), ("reconciling", "TRIGGERED")])
        remaining = strategy.requests[-1].cancel_requests[-1].client_order_id
        assert strategy._entry_protection_stash[KEY]["pending_cancel_ids"] == (remaining,)
        persisted = strategy._load_entry_protection_stash()
        restarted = ClosedProtectionHarness(directory)
        restarted._entry_protection_stash = persisted
        restarted.snapshot["algo_orders"] = [r for r in restarted.snapshot["algo_orders"] if r["client_order_id"] == remaining]
        restarted._check_protection_watchdog(KEY)
        assert [r.client_order_id for r in restarted.requests[0].cancel_requests] == [remaining]
        assert KEY in restarted._entry_protection_stash


@pytest.mark.parametrize("case", ["stale", "missing", "halted", "reopened", "wrong_mirror"])
def test_cleanup_requires_fresh_flat_authorized_scope(case):
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        if case == "stale":
            strategy.snapshot["fetched_at"] = NOW - timedelta(minutes=1)
        elif case == "missing":
            strategy.snapshot.pop("positions")
        elif case == "halted":
            strategy.set_trading_state_getter(lambda: "HALTED")
        elif case == "reopened":
            strategy.snapshot["positions"] = [{"symbol": "ZECUSDT", "position_side": "SHORT", "quantity": "1"}]
        else:
            original = strategy.find_order
            def foreign(instrument, cid):
                order = original(instrument, cid)
                order.account_id = "account-a"
                return order
            strategy._exchange_state_mirror.find_order = foreign
        strategy._check_protection_watchdog(KEY)
        assert strategy.requests == []


def test_manual_orders_other_intents_and_opposite_book_are_never_cancelled():
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        for cid, book in [("aos_123", "SHORT"), ("stToAg_456", "SHORT"),
                          (encode_client_order_id(UUID(int=2), 32), "SHORT"), (IDS[1], "LONG")]:
            strategy.snapshot["algo_orders"].append({"symbol": "ZECUSDT", "position_side": book, "client_order_id": cid})
        strategy._check_protection_watchdog(KEY)
        assert {r.client_order_id for r in strategy.requests[0].cancel_requests} == set(IDS[2:])


def test_close_fence_survives_restart_and_does_not_fence_opposite_book():
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        close_event(strategy, "LONG")
        assert "position_closed_at" not in strategy._entry_protection_stash[KEY]
        close_event(strategy)
        persisted = strategy._load_entry_protection_stash()
        restarted = ClosedProtectionHarness(directory)
        restarted._entry_protection_stash = persisted
        restarted.snapshot["fetched_at"] = NOW - timedelta(seconds=5)
        restarted.snapshot["positions"] = [{"symbol": "ZECUSDT", "position_side": "SHORT", "quantity": "1.394"}]
        restarted._check_protection_watchdog(KEY)
        assert restarted.repairs == []
        assert restarted.requests == []


def test_only_new_attributed_entry_fill_releases_close_fence():
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        close_event(strategy)
        for cid, age in [(IDS[0], 10), ("aos_late", 1), (IDS[1], 1)]:
            strategy._release_closed_protection_fence(SimpleNamespace(
                client_order_id=cid, last_qty="0.1", ts_event=int((NOW - timedelta(seconds=age)).timestamp() * 1e9)))
            assert "position_closed_at" in strategy._entry_protection_stash[KEY]
        strategy._release_closed_protection_fence(SimpleNamespace(
            client_order_id=IDS[0], last_qty="0.1", ts_event=int((NOW - timedelta(seconds=1)).timestamp() * 1e9)))
        assert "position_closed_at" not in strategy._entry_protection_stash[KEY]


def test_frozen_repair_does_not_prevent_flat_cleanup():
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        strategy._entry_protection_stash[KEY]["protection_frozen"] = "revisions_exhausted"
        strategy._check_protection_watchdog(KEY)
        assert len(strategy.requests) == 1


def test_restart_after_venue_cancel_before_receipt_reconciles_absent_orders():
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        strategy._check_protection_watchdog(KEY)
        persisted = strategy._load_entry_protection_stash()
        restarted = ClosedProtectionHarness(directory)
        restarted._entry_protection_stash = persisted
        restarted.snapshot["algo_orders"] = []
        restarted._exchange_state_mirror.find_order = lambda *args: False
        restarted._check_protection_watchdog(KEY)
        assert len(restarted.requests) == 1
        assert restarted.requests[0].cancel_requests == strategy.requests[0].cancel_requests
        complete(restarted, [("confirmed", "CANCELED"), ("confirmed", "CANCELED")])
        restarted._check_protection_watchdog(KEY)
        assert KEY not in restarted._entry_protection_stash


def test_close_fence_precedes_entry_newer_than_snapshot_and_stale_cache():
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        strategy.snapshot["fetched_at"] = NOW - timedelta(seconds=5)
        strategy._entry_protection_stash[KEY]["last_entry_fill_at"] = (NOW - timedelta(seconds=4)).isoformat()
        strategy._protection_position = lambda *args: SimpleNamespace(quantity="1.394")
        close_event(strategy)
        assert strategy._defer_flat_or_unknown_protection(KEY, strategy._entry_protection_stash[KEY])
        assert strategy.requests == []


def test_cancel_rejection_does_not_erase_unconfirmed_cleanup():
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        strategy._check_protection_watchdog(KEY)
        strategy._live_protection_orders = lambda *args: ()
        strategy.on_order_cancel_rejected(SimpleNamespace(client_order_id=IDS[2], instrument_id=INSTRUMENT))
        assert set(strategy._entry_protection_stash[KEY]["pending_cancel_ids"]) == set(IDS[2:])


@pytest.mark.parametrize("terminal_status", ["CANCELED", "FILLED", "TRIGGERED"])
def test_real_cancel_lane_terminal_evidence_controls_retirement(terminal_status):
    from runtime.exchange_cancel_adapter import BinanceExchangeCancelAdapter, TerminalExchangeWorker

    class Transport:
        def request(self, method, path, params, **kwargs):
            if method == "DELETE":
                return {"code": 200}
            if path == "/fapi/v1/openAlgoOrders":
                return {"orders": []}
            assert path == "/fapi/v1/algoOrder"
            return {"algoStatus": terminal_status}

    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        strategy._check_protection_watchdog(KEY)
        request = strategy.requests[-1]
        worker = TerminalExchangeWorker(
            account_id="account-d", mirror=None,
            adapter=BinanceExchangeCancelAdapter(account_id="account-d", transport=Transport()),
            result_publisher=lambda result: None,
        )
        outcomes = worker._cancel_batch(request.cancel_requests, worker.new_deadline())
        strategy._on_terminal_exchange_result(SimpleNamespace(
            request_id=request.request_id, account_id="account-d", cancel_outcomes=outcomes,
            error="", timed_out=False,
        ))
        strategy.snapshot["algo_orders"] = []
        strategy._check_protection_watchdog(KEY)
        assert (KEY not in strategy._entry_protection_stash) == (terminal_status != "TRIGGERED")


def test_cleanup_waits_for_durable_write_and_rechecks_position_before_submit():
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        continuations = []
        strategy._queue_entry_protection_stash_persist = lambda *, continuation=False: continuations.append(continuation)
        strategy._check_protection_watchdog(KEY)
        assert strategy.requests == []
        strategy.snapshot["positions"] = [{"symbol": "ZECUSDT", "position_side": "SHORT", "quantity": "1"}]
        strategy._run_protection_continuation(continuations[0])
        assert strategy.requests == []


def test_prepared_protection_cannot_submit_after_close_event():
    with tempfile.TemporaryDirectory() as directory:
        strategy = ClosedProtectionHarness(directory)
        close_event(strategy)
        for cid, role in [(IDS[1], "stop_loss"), (IDS[2], "take_profit")]:
            plan = SimpleNamespace(
                intent_id=UUID(KEY), client_order_id=cid, instrument_id=INSTRUMENT,
                tags=(f"lifecycle_role={role}",),
            )
            denial = strategy._plan_for_submission(plan)
            assert denial.reason == "protection_position_closed"
