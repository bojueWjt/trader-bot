from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import time
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from email.message import Message
from io import BytesIO
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

MODULE_PATH = SERVICE_ROOT / "runtime" / "exchange_cancel_adapter.py"
SPEC = importlib.util.spec_from_file_location(
    "_exchange_cancel_adapter_under_test",
    MODULE_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load exchange cancel adapter: {MODULE_PATH}")
EXCHANGE_CANCEL_ADAPTER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = EXCHANGE_CANCEL_ADAPTER
SPEC.loader.exec_module(EXCHANGE_CANCEL_ADAPTER)

BinanceApiError = EXCHANGE_CANCEL_ADAPTER.BinanceApiError
BinanceExchangeCancelAdapter = EXCHANGE_CANCEL_ADAPTER.BinanceExchangeCancelAdapter
BinanceExchangeEvidenceProvider = (
    EXCHANGE_CANCEL_ADAPTER.BinanceExchangeEvidenceProvider
)
CancelConfirmationTimeoutError = (
    EXCHANGE_CANCEL_ADAPTER.CancelConfirmationTimeoutError
)
CancelStateError = EXCHANGE_CANCEL_ADAPTER.CancelStateError
CancelIntentRequiredError = EXCHANGE_CANCEL_ADAPTER.CancelIntentRequiredError
CancelOrderRequest = EXCHANGE_CANCEL_ADAPTER.CancelOrderRequest
ControlPlaneExchangeStateMirror = (
    EXCHANGE_CANCEL_ADAPTER.ControlPlaneExchangeStateMirror
)
ExchangeCancelError = EXCHANGE_CANCEL_ADAPTER.ExchangeCancelError
OrderAlreadyFilledError = EXCHANGE_CANCEL_ADAPTER.OrderAlreadyFilledError
SignedBinanceTransport = EXCHANGE_CANCEL_ADAPTER.SignedBinanceTransport
TerminalExchangeRequest = EXCHANGE_CANCEL_ADAPTER.TerminalExchangeRequest
TerminalExchangeWorker = EXCHANGE_CANCEL_ADAPTER.TerminalExchangeWorker
WrongAccountError = EXCHANGE_CANCEL_ADAPTER.WrongAccountError


ACCOUNT_ID = "account-a"


class ExchangeCancelAdapterTest(unittest.TestCase):
    def test_regular_order_cancel_uses_regular_endpoint_and_confirms_absence(self) -> None:
        transport = _ScriptedTransport(
            {
                ("DELETE", "/fapi/v1/order"): [{"orderId": 42, "status": "CANCELED"}],
                ("GET", "/fapi/v1/openOrders"): [[]],
                ("GET", "/fapi/v1/order"): [{"orderId": 42, "status": "CANCELED"}],
            }
        )
        adapter = _adapter(transport)

        result = adapter.cancel("cancel_order", _request(order_kind="regular", venue_order_id="42"))

        self.assertEqual(result.outcome, "canceled")
        delete = transport.calls[0]
        self.assertEqual(delete[0:2], ("DELETE", "/fapi/v1/order"))
        self.assertEqual(delete[2]["symbol"], "BTCUSDT")
        self.assertEqual(delete[2]["orderId"], "42")
        self.assertNotIn("algoId", delete[2])
        self.assertEqual(
            [
                call
                for call in transport.calls
                if call[0:2] == ("GET", "/fapi/v1/openOrders")
            ],
            [("GET", "/fapi/v1/openOrders", {"symbol": "BTCUSDT"})],
        )

    def test_regular_order_can_route_by_orig_client_order_id(self) -> None:
        transport = _ScriptedTransport(
            {
                ("DELETE", "/fapi/v1/order"): [{"status": "CANCELED"}],
                ("GET", "/fapi/v1/openOrders"): [[]],
                ("GET", "/fapi/v1/order"): [{"status": "CANCELED"}],
            }
        )
        adapter = _adapter(transport)

        result = adapter.cancel(
            "cancel_order",
            _request(order_kind="regular", venue_order_id=None),
        )

        self.assertEqual(result.outcome, "canceled")
        delete = transport.calls[0]
        self.assertEqual(
            delete[2]["origClientOrderId"],
            "B0123456789abcdef0123456789abcdef01",
        )
        self.assertNotIn("orderId", delete[2])

    def test_algo_order_cancel_uses_algo_id_and_confirms_absence(self) -> None:
        transport = _ScriptedTransport(
            {
                ("DELETE", "/fapi/v1/algoOrder"): [{"algoId": 9001, "algoStatus": "CANCELED"}],
                ("GET", "/fapi/v1/openAlgoOrders"): [{"orders": []}],
                ("GET", "/fapi/v1/algoOrder"): [{"algoId": 9001, "algoStatus": "CANCELED"}],
            }
        )
        adapter = _adapter(transport)

        result = adapter.cancel("cancel_order", _request(order_kind="algo", venue_order_id="9001"))

        self.assertEqual(result.outcome, "canceled")
        delete = transport.calls[0]
        self.assertEqual(delete[0:2], ("DELETE", "/fapi/v1/algoOrder"))
        self.assertEqual(delete[2]["algoId"], "9001")
        self.assertNotIn("orderId", delete[2])

    def test_algo_order_rejects_client_id_only_route(self) -> None:
        with self.assertRaisesRegex(ValueError, "algo cancellation requires venue_order_id"):
            _request(order_kind="algo", venue_order_id=None)

    def test_order_filled_before_cancel_is_reported_as_filled(self) -> None:
        transport = _ScriptedTransport(
            {
                ("DELETE", "/fapi/v1/order"): [BinanceApiError(-2011, "Unknown order sent")],
                ("GET", "/fapi/v1/openOrders"): [[]],
                ("GET", "/fapi/v1/order"): [{"orderId": 42, "status": "FILLED"}],
            }
        )
        adapter = _adapter(transport)

        with self.assertRaisesRegex(OrderAlreadyFilledError, "FILLED"):
            adapter.cancel("cancel_order", _request(order_kind="regular", venue_order_id="42"))

    def test_already_canceled_is_idempotent(self) -> None:
        transport = _ScriptedTransport(
            {
                ("DELETE", "/fapi/v1/algoOrder"): [BinanceApiError(-2011, "Unknown order sent")],
                ("GET", "/fapi/v1/openAlgoOrders"): [{"orders": []}],
                ("GET", "/fapi/v1/algoOrder"): [{"algoId": 9001, "algoStatus": "CANCELED"}],
            }
        )
        adapter = _adapter(transport)

        result = adapter.cancel("cancel_order", _request(order_kind="algo", venue_order_id="9001"))

        self.assertEqual(result.outcome, "already_canceled")

    def test_delete_success_without_terminal_status_fails_closed(self) -> None:
        transport = _ScriptedTransport(
            {
                ("DELETE", "/fapi/v1/order"): [{"status": "CANCELED"}],
                ("GET", "/fapi/v1/openOrders"): [[]],
                ("GET", "/fapi/v1/order"): [BinanceApiError(-2013, "Order does not exist")],
            }
        )
        adapter = _adapter(transport)

        with self.assertRaisesRegex(CancelStateError, "terminal status UNKNOWN"):
            adapter.cancel(
                "cancel_order",
                _request(order_kind="regular", venue_order_id="42"),
            )

    def test_wrong_account_is_rejected_before_exchange_request(self) -> None:
        transport = _ScriptedTransport({})
        adapter = _adapter(transport)

        with self.assertRaises(WrongAccountError):
            adapter.cancel(
                "cancel_order",
                _request(account_id="account-b", order_kind="regular", venue_order_id="42"),
            )

        self.assertEqual(transport.calls, [])

    def test_adapter_rejects_non_cancel_intent(self) -> None:
        transport = _ScriptedTransport({})
        adapter = _adapter(transport)

        with self.assertRaises(CancelIntentRequiredError):
            adapter.cancel("move_stop_loss", _request(order_kind="algo", venue_order_id="9001"))

        self.assertEqual(transport.calls, [])

    def test_cancel_times_out_when_order_remains_open(self) -> None:
        transport = _ScriptedTransport(
            {
                ("DELETE", "/fapi/v1/order"): [{"orderId": 42, "status": "CANCELED"}],
                ("GET", "/fapi/v1/openOrders"): [[{"orderId": 42, "status": "NEW"}]],
            }
        )
        monotonic_values = iter((0.0, 0.02))
        adapter = BinanceExchangeCancelAdapter(
            account_id=ACCOUNT_ID,
            transport=transport,
            confirmation_timeout_seconds=0.01,
            poll_interval_seconds=0,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(monotonic_values),
        )

        with self.assertRaisesRegex(CancelConfirmationTimeoutError, "remained open"):
            adapter.cancel("cancel_order", _request(order_kind="regular", venue_order_id="42"))


class TerminalExchangeWorkerTest(unittest.TestCase):
    def test_duplicate_id_reuses_immutable_result_without_reexecution(
        self,
    ) -> None:
        mirror = _CountingMirror()
        results: list[Any] = []
        worker = TerminalExchangeWorker(
            account_id=ACCOUNT_ID,
            mirror=mirror,
            adapter=_NoopCancelAdapter(),
            result_publisher=results.append,
            capacity=4,
            total_deadline_seconds=1,
        )
        request = TerminalExchangeRequest(
            request_id="refresh-1",
            account_id=ACCOUNT_ID,
            operation="refresh",
            purpose="test",
            deadline_monotonic=worker.new_deadline(),
        )

        with self.assertRaises(FrozenInstanceError):
            request.purpose = "mutated"

        worker.start()
        try:
            self.assertTrue(worker.submit(request))
            self.assertTrue(worker.wait_empty(timeout_seconds=1))
            self.assertTrue(worker.submit(request))
        finally:
            worker.stop()

        self.assertEqual(mirror.refresh_count, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].request_id, "refresh-1")
        self.assertEqual(results[1], results[0])

    def test_queue_reaches_degraded_at_80_percent_and_full_sticky_halts(
        self,
    ) -> None:
        mirror = _BlockingMirror()
        degraded: list[str] = []
        halts: list[str] = []
        worker = TerminalExchangeWorker(
            account_id=ACCOUNT_ID,
            mirror=mirror,
            adapter=_NoopCancelAdapter(),
            result_publisher=lambda _result: None,
            capacity=5,
            total_deadline_seconds=1,
            on_degraded=degraded.append,
            on_halt=halts.append,
        )
        worker.start()
        try:
            self.assertTrue(worker.submit(_refresh_request(worker, "active")))
            self.assertTrue(mirror.started.wait(timeout=1))
            for index in range(5):
                self.assertTrue(
                    worker.submit(
                        _refresh_request(worker, f"queued-{index}")
                    )
                )

            self.assertTrue(degraded)
            self.assertTrue(worker.snapshot().degraded)
            self.assertFalse(
                worker.submit(_refresh_request(worker, "overflow"))
            )
            self.assertTrue(halts)
            self.assertTrue(worker.snapshot().halted)
            self.assertFalse(
                worker.submit(_refresh_request(worker, "after-halt"))
            )
        finally:
            mirror.release.set()
            worker.stop()

    def test_total_deadline_halts_and_reports_timeout(self) -> None:
        results: list[Any] = []
        halts: list[str] = []
        worker = TerminalExchangeWorker(
            account_id=ACCOUNT_ID,
            mirror=_SlowMirror(0.03),
            adapter=_NoopCancelAdapter(),
            result_publisher=results.append,
            capacity=2,
            total_deadline_seconds=0.01,
            on_halt=halts.append,
        )
        worker.start()
        try:
            self.assertTrue(
                worker.submit(_refresh_request(worker, "deadline"))
            )
            self.assertTrue(worker.wait_empty(timeout_seconds=1))
        finally:
            worker.stop()

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].timed_out)
        self.assertTrue(halts)
        snapshot = worker.snapshot()
        self.assertTrue(snapshot.halted)
        self.assertEqual(snapshot.timed_out, 1)

    def test_stop_drains_accepted_work_before_returning(self) -> None:
        results: list[Any] = []
        worker = TerminalExchangeWorker(
            account_id=ACCOUNT_ID,
            mirror=_SlowMirror(0.03),
            adapter=_NoopCancelAdapter(),
            result_publisher=results.append,
            capacity=2,
            total_deadline_seconds=1,
        )
        worker.start()
        self.assertTrue(worker.submit(_refresh_request(worker, "drain")))

        worker.stop(timeout_seconds=1)

        self.assertEqual([result.request_id for result in results], ["drain"])
        self.assertFalse(worker.snapshot().running)


class ExchangeStateMirrorTest(unittest.TestCase):
    def test_refresh_carries_complete_writer_identity_headers(self) -> None:
        mirror = _mirror()
        mirror.bind_writer_identity(
            redis_fencing_epoch=(
                "11111111-1111-4111-8111-111111111111"
            ),
            runtime_generation="runtime-generation-a",
            lease_fencing_token=42,
        )

        with patch.object(
            EXCHANGE_CANCEL_ADAPTER.urllib.request,
            "urlopen",
            return_value=_mirror_response(
                stale=False,
                client_order_id="fresh-order",
            ),
        ) as urlopen:
            mirror.refresh()

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.get_header("X-redis-fencing-epoch"),
            "11111111-1111-4111-8111-111111111111",
        )
        self.assertEqual(
            request.get_header("X-runtime-generation"),
            "runtime-generation-a",
        )
        self.assertEqual(
            request.get_header("X-lease-fencing-token"),
            "42",
        )

    def test_writer_rejection_triggers_sticky_fatal_fence(self) -> None:
        mirror = _mirror()
        fatal_reasons: list[str] = []
        mirror.bind_writer_identity(
            redis_fencing_epoch=(
                "11111111-1111-4111-8111-111111111111"
            ),
            runtime_generation="runtime-generation-a",
            lease_fencing_token=42,
        )
        mirror.bind_fatal_fence_hook(fatal_reasons.append)
        headers = Message()
        headers["X-Writer-Fence-Rejected"] = "1"
        rejection = HTTPError(
            url=(
                "http://control-plane:8080/v1/nodes/"
                "node-a/exchange-state"
            ),
            code=409,
            msg="Conflict",
            hdrs=headers,
            fp=BytesIO(b'{"detail":"stale mirror writer"}'),
        )

        with patch.object(
            EXCHANGE_CANCEL_ADAPTER.urllib.request,
            "urlopen",
            side_effect=rejection,
        ) as urlopen:
            with self.assertRaisesRegex(
                ExchangeCancelError,
                "stale mirror writer",
            ):
                mirror.refresh()
            with self.assertRaisesRegex(
                ExchangeCancelError,
                "stale mirror writer",
            ):
                mirror.refresh()

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(
            fatal_reasons,
            [
                "exchange state mirror rejected stale writer: "
                "stale mirror writer"
            ],
        )

    def test_regular_and_algo_ids_remain_separate_in_planner_view(self) -> None:
        response = _JsonResponse(
            {
                "account_id": ACCOUNT_ID,
                "stale": False,
                "payload": {
                    "open_orders": [
                        {
                            "symbol": "BTCUSDT",
                            "position_side": "LONG",
                            "client_order_id": "regular-client",
                            "venue_order_id": "42",
                            "order_id": "42",
                            "type": "LIMIT",
                            "side": "SELL",
                            "quantity": "0.5",
                        }
                    ],
                    "algo_orders": [
                        {
                            "symbol": "BTCUSDT",
                            "position_side": "LONG",
                            "client_order_id": "algo-client",
                            "venue_order_id": "9001",
                            "order_id": "9001",
                            "type": "STOP_MARKET",
                            "side": "SELL",
                            "quantity": "0.5",
                        }
                    ],
                },
            }
        )
        mirror = ControlPlaneExchangeStateMirror(
            account_id=ACCOUNT_ID,
            node_id="node-a",
            base_url="http://control-plane:8080",
            token="node-token",
        )

        with patch.object(
            EXCHANGE_CANCEL_ADAPTER.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            mirror.refresh()

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-node-id"), "node-a")
        self.assertEqual(request.get_header("X-account-id"), ACCOUNT_ID)
        regular = mirror.find_order("BTCUSDT-PERP.BINANCE", "regular-client")
        algo = mirror.find_order("BTCUSDT-PERP.BINANCE", "algo-client")
        self.assertNotEqual(regular, False)
        self.assertNotEqual(algo, False)
        self.assertEqual(regular.order_kind, "regular")
        self.assertEqual(regular.venue_order_id, "42")
        self.assertEqual(algo.order_kind, "algo")
        self.assertEqual(algo.venue_order_id, "9001")

    def test_stale_response_invalidates_previous_orders(self) -> None:
        mirror = _mirror()

        with patch.object(
            EXCHANGE_CANCEL_ADAPTER.urllib.request,
            "urlopen",
            side_effect=[
                _mirror_response(stale=False, client_order_id="fresh-order"),
                _mirror_response(stale=True, client_order_id="stale-order"),
            ],
        ):
            mirror.refresh()
            self.assertNotEqual(
                mirror.find_order(
                    "BTCUSDT-PERP.BINANCE",
                    "fresh-order",
                ),
                False,
            )
            with self.assertRaisesRegex(ExchangeCancelError, "stale"):
                mirror.refresh()

        with self.assertRaisesRegex(ExchangeCancelError, "not fresh"):
            mirror.find_order("BTCUSDT-PERP.BINANCE", "fresh-order")

    def test_refresh_failure_invalidates_previous_orders(self) -> None:
        mirror = _mirror()

        with patch.object(
            EXCHANGE_CANCEL_ADAPTER.urllib.request,
            "urlopen",
            return_value=_mirror_response(
                stale=False,
                client_order_id="fresh-order",
            ),
        ):
            mirror.refresh()

        with patch.object(
            EXCHANGE_CANCEL_ADAPTER.urllib.request,
            "urlopen",
            side_effect=EXCHANGE_CANCEL_ADAPTER.URLError("timeout"),
        ):
            with self.assertRaisesRegex(ExchangeCancelError, "refresh failed"):
                mirror.refresh()

        with self.assertRaisesRegex(ExchangeCancelError, "not fresh"):
            mirror.orders_for_instrument("BTCUSDT-PERP.BINANCE")


class ExchangeEvidenceProviderTest(unittest.TestCase):
    def test_margin_snapshot_reads_and_caches_account_margin_ratio(
        self,
    ) -> None:
        transport = _ScriptedTransport(
            {
                ("GET", "/fapi/v2/account"): [
                    {
                        "availableBalance": "25",
                        "totalMarginBalance": "100",
                    }
                ],
            }
        )
        provider = BinanceExchangeEvidenceProvider(
            transport=transport,
            monotonic=lambda: 10.0,
        )

        first = provider.margin_snapshot()
        second = provider.margin_snapshot()

        self.assertEqual(first["available_balance"], "25")
        self.assertEqual(first["total_margin_balance"], "100")
        self.assertEqual(first["margin_ratio"], "0.25")
        self.assertEqual(second, first)
        self.assertEqual(len(transport.calls), 1)

    def test_default_refresh_interval_reuses_evidence_for_five_seconds(
        self,
    ) -> None:
        clock = _VirtualClock(
            datetime(2026, 8, 8, 8, 30, tzinfo=timezone.utc),
            monotonic_value=10.0,
        )
        transport = _ScriptedTransport(
            {
                ("GET", "/fapi/v2/positionRisk"): [[], []],
                ("GET", "/fapi/v1/openOrders"): [[], []],
                ("GET", "/fapi/v1/openAlgoOrders"): [
                    {"orders": []},
                    {"orders": []},
                ],
            }
        )
        provider = BinanceExchangeEvidenceProvider(
            transport=transport,
            monotonic=clock.monotonic,
            now=clock.now,
        )

        first = provider.snapshot()
        clock.advance(4.99)
        cached = provider.snapshot()

        self.assertEqual(cached, first)
        self.assertEqual(len(transport.calls), 3)

        clock.advance(0.02)
        provider.snapshot()

        self.assertEqual(len(transport.calls), 6)

    def test_snapshot_reuses_fresh_cached_evidence(self) -> None:
        transport = _ScriptedTransport(
            {
                ("GET", "/fapi/v2/positionRisk"): [[]],
                ("GET", "/fapi/v1/openOrders"): [[]],
                ("GET", "/fapi/v1/openAlgoOrders"): [{"orders": []}],
            }
        )
        provider = BinanceExchangeEvidenceProvider(
            transport=transport,
            monotonic=lambda: 10.0,
        )

        first = provider.snapshot()
        second = provider.snapshot()

        self.assertEqual(second, first)
        self.assertIsNot(second, first)
        self.assertEqual(len(transport.calls), 3)

    def test_cached_snapshot_never_performs_network_io_and_expires(self) -> None:
        clock = _VirtualClock(
            datetime(2026, 8, 8, 8, 30, tzinfo=timezone.utc),
            monotonic_value=10.0,
        )
        transport = _ScriptedTransport(
            {
                ("GET", "/fapi/v2/positionRisk"): [[]],
                ("GET", "/fapi/v1/openOrders"): [[]],
                ("GET", "/fapi/v1/openAlgoOrders"): [{"orders": []}],
            }
        )
        provider = BinanceExchangeEvidenceProvider(
            transport=transport,
            monotonic=clock.monotonic,
            now=clock.now,
        )

        self.assertIs(provider.cached_snapshot(max_age_seconds=5.0), False)
        refreshed = provider.snapshot()
        network_call_count = len(transport.calls)

        cached = provider.cached_snapshot(max_age_seconds=5.0)

        self.assertEqual(cached, refreshed)
        self.assertIsNot(cached, refreshed)
        self.assertEqual(len(transport.calls), network_call_count)

        clock.advance(5.01)

        self.assertIs(provider.cached_snapshot(max_age_seconds=5.0), False)
        self.assertEqual(len(transport.calls), network_call_count)

    def test_force_refresh_reloads_all_exchange_endpoints(self) -> None:
        transport = _ScriptedTransport(
            {
                ("GET", "/fapi/v2/positionRisk"): [
                    [],
                    [
                        {
                            "symbol": "SOLUSDT",
                            "positionAmt": "0.13",
                            "positionSide": "LONG",
                        }
                    ],
                ],
                ("GET", "/fapi/v1/openOrders"): [[], []],
                ("GET", "/fapi/v1/openAlgoOrders"): [
                    {"orders": []},
                    {"orders": []},
                ],
            }
        )
        provider = BinanceExchangeEvidenceProvider(
            transport=transport,
            monotonic=lambda: 10.0,
        )

        cached = provider.snapshot()
        refreshed = provider.snapshot(force_refresh=True)

        self.assertEqual(cached["positions"], [])
        self.assertEqual(refreshed["positions"][0]["symbol"], "SOLUSDT")
        self.assertEqual(
            [path for _, path, _ in transport.calls],
            [
                "/fapi/v2/positionRisk",
                "/fapi/v1/openOrders",
                "/fapi/v1/openAlgoOrders",
                "/fapi/v2/positionRisk",
                "/fapi/v1/openOrders",
                "/fapi/v1/openAlgoOrders",
            ],
        )

    def test_concurrent_force_refresh_calls_share_one_in_flight_round(
        self,
    ) -> None:
        transport = _BlockingEvidenceTransport()
        provider = BinanceExchangeEvidenceProvider(
            transport=transport,
        )
        snapshots: list[dict[str, Any]] = []
        failures: list[BaseException] = []

        def snapshot() -> None:
            try:
                snapshots.append(provider.snapshot(force_refresh=True))
            except BaseException as exc:
                failures.append(exc)

        first = Thread(target=snapshot)
        second = Thread(target=snapshot)
        first.start()
        self.assertTrue(transport.first_request_started.wait(timeout=1.0))
        second.start()
        time.sleep(0.05)
        transport.release_first_request.set()
        first.join(timeout=1.0)
        second.join(timeout=1.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(
            [path for _, path, _ in transport.calls],
            [
                "/fapi/v2/positionRisk",
                "/fapi/v1/openOrders",
                "/fapi/v1/openAlgoOrders",
            ],
        )

    def test_force_refresh_fails_closed_during_backoff_and_circuit(self) -> None:
        for threshold, expected_error in (
            (2, "backoff active"),
            (1, "circuit open"),
        ):
            with self.subTest(expected_error=expected_error):
                clock = _VirtualClock(
                    datetime(2026, 8, 8, 8, 30, tzinfo=timezone.utc),
                    monotonic_value=100.0,
                )
                transport = _ScriptedTransport(
                    {
                        ("GET", "/fapi/v2/positionRisk"): [
                            [],
                            RuntimeError("forced refresh failed"),
                        ],
                        ("GET", "/fapi/v1/openOrders"): [[]],
                        ("GET", "/fapi/v1/openAlgoOrders"): [
                            {"orders": []},
                        ],
                    }
                )
                provider = BinanceExchangeEvidenceProvider(
                    transport=transport,
                    circuit_failure_threshold=threshold,
                    now=clock.now,
                    monotonic=clock.monotonic,
                    jitter=lambda _delay: 0.0,
                )

                provider.snapshot()
                with self.assertRaisesRegex(
                    ExchangeCancelError,
                    "forced refresh failed",
                ):
                    provider.snapshot(force_refresh=True)
                call_count = len(transport.calls)

                with self.assertRaisesRegex(
                    ExchangeCancelError,
                    expected_error,
                ):
                    provider.snapshot(force_refresh=True)

                self.assertEqual(len(transport.calls), call_count)

    def test_snapshot_reads_positions_regular_and_algo_orders(self) -> None:
        transport = _ScriptedTransport(
            {
                ("GET", "/fapi/v2/positionRisk"): [
                    [
                        {
                            "symbol": "BTCUSDT",
                            "positionAmt": "0",
                            "positionSide": "LONG",
                        },
                        {
                            "symbol": "ETHUSDT",
                            "positionAmt": "-0.02",
                            "positionSide": "SHORT",
                            "entryPrice": "3000",
                            "markPrice": "2999",
                        },
                    ]
                ],
                ("GET", "/fapi/v1/openOrders"): [
                    [
                        {
                            "symbol": "BTCUSDT",
                            "orderId": 42,
                            "clientOrderId": "regular-1",
                            "positionSide": "LONG",
                            "side": "BUY",
                            "type": "LIMIT",
                            "origQty": "0.001",
                            "executedQty": "0",
                            "price": "65000",
                            "timeInForce": "GTC",
                        }
                    ]
                ],
                ("GET", "/fapi/v1/openAlgoOrders"): [
                    {
                        "orders": [
                            {
                                "symbol": "ETHUSDT",
                                "algoId": 9001,
                                "clientAlgoId": "algo-1",
                                "positionSide": "SHORT",
                                "side": "BUY",
                                "orderType": "STOP_MARKET",
                                "quantity": "0.02",
                                "executedQuantity": "0",
                                "triggerPrice": "3050",
                                "workingType": "MARK_PRICE",
                                "priceProtect": True,
                            }
                        ]
                    }
                ],
            }
        )
        now = datetime(2026, 8, 8, 8, 30, tzinfo=timezone.utc)
        provider = BinanceExchangeEvidenceProvider(
            transport=transport,
            refresh_interval_seconds=3,
            now=lambda: now,
            monotonic=lambda: 10.0,
        )

        snapshot = provider.snapshot()

        self.assertEqual(snapshot["fetched_at"], now)
        self.assertEqual(
            snapshot["positions"],
            [
                {
                    "symbol": "ETHUSDT",
                    "quantity": "-0.02",
                    "position_side": "SHORT",
                    "entry_price": "3000",
                    "mark_price": "2999",
                }
            ],
        )
        self.assertEqual(snapshot["regular_orders"][0]["order_kind"], "regular")
        self.assertEqual(snapshot["regular_orders"][0]["price"], "65000")
        self.assertEqual(
            snapshot["regular_orders"][0]["executed_quantity"],
            "0",
        )
        self.assertEqual(
            snapshot["regular_orders"][0]["time_in_force"],
            "GTC",
        )
        self.assertEqual(snapshot["algo_orders"][0]["order_kind"], "algo")
        self.assertEqual(snapshot["algo_orders"][0]["stop_price"], "3050")
        self.assertEqual(
            snapshot["algo_orders"][0]["working_type"],
            "MARK_PRICE",
        )
        self.assertTrue(snapshot["algo_orders"][0]["price_protect"])
        self.assertEqual(len(transport.calls), 3)

    def test_snapshot_uses_earliest_endpoint_completion_time(self) -> None:
        clock = _VirtualClock(
            datetime(2026, 8, 8, 8, 30, tzinfo=timezone.utc),
        )
        transport = _TimedEvidenceTransport(
            clock,
            delays={
                "/fapi/v2/positionRisk": 0.25,
                "/fapi/v1/openOrders": 0.5,
                "/fapi/v1/openAlgoOrders": 0.75,
            },
        )
        provider = BinanceExchangeEvidenceProvider(
            transport=transport,
            now=clock.now,
            monotonic=clock.monotonic,
        )

        snapshot = provider.snapshot()

        expected_position_time = datetime(
            2026,
            8,
            8,
            8,
            30,
            0,
            250000,
            tzinfo=timezone.utc,
        )
        self.assertEqual(snapshot["positions_fetched_at"], expected_position_time)
        self.assertEqual(
            snapshot["regular_orders_fetched_at"],
            expected_position_time + timedelta(seconds=0.5),
        )
        self.assertEqual(
            snapshot["algo_orders_fetched_at"],
            expected_position_time + timedelta(seconds=1.25),
        )
        self.assertEqual(snapshot["fetched_at"], expected_position_time)
        self.assertEqual(
            transport.timeouts,
            [4.0, 3.75, 3.25],
        )

    def test_round_deadline_prevents_slow_orders_from_refreshing_old_position(
        self,
    ) -> None:
        clock = _VirtualClock(
            datetime(2026, 8, 8, 8, 30, tzinfo=timezone.utc),
        )
        transport = _TimedEvidenceTransport(
            clock,
            delays={
                "/fapi/v2/positionRisk": 0.0,
                "/fapi/v1/openOrders": 10.0,
                "/fapi/v1/openAlgoOrders": 10.0,
            },
        )
        provider = BinanceExchangeEvidenceProvider(
            transport=transport,
            now=clock.now,
            monotonic=clock.monotonic,
        )

        with self.assertRaisesRegex(
            ExchangeCancelError,
            "deadline",
        ):
            provider.snapshot()

        self.assertEqual(
            [path for _, path, _ in transport.calls],
            [
                "/fapi/v2/positionRisk",
                "/fapi/v1/openOrders",
            ],
        )
        self.assertLessEqual(clock.monotonic(), 4.0)

    def test_rate_limit_retry_after_blocks_high_frequency_requests(self) -> None:
        clock = _VirtualClock(
            datetime(2026, 8, 8, 8, 30, tzinfo=timezone.utc),
            monotonic_value=100.0,
        )
        transport = _ScriptedTransport(
            {
                ("GET", "/fapi/v2/positionRisk"): [
                    BinanceApiError(
                        -1003,
                        "Too many requests",
                        http_status=429,
                        headers={"Retry-After": "3"},
                    ),
                    [],
                ],
                ("GET", "/fapi/v1/openOrders"): [[]],
                ("GET", "/fapi/v1/openAlgoOrders"): [{"orders": []}],
            }
        )
        provider = BinanceExchangeEvidenceProvider(
            transport=transport,
            now=clock.now,
            monotonic=clock.monotonic,
            jitter=lambda _delay: 0.0,
        )

        with self.assertRaisesRegex(ExchangeCancelError, "rate limited"):
            provider.snapshot()
        self.assertEqual(len(transport.calls), 1)

        clock.advance(2.9)
        with self.assertRaisesRegex(ExchangeCancelError, "backoff"):
            provider.snapshot()
        self.assertEqual(len(transport.calls), 1)

        clock.advance(0.1)
        snapshot = provider.snapshot()
        self.assertEqual(snapshot["positions"], [])
        self.assertEqual(len(transport.calls), 4)

    def test_ip_ban_retry_after_opens_circuit_without_sleeping(self) -> None:
        clock = _VirtualClock(
            datetime(2026, 8, 8, 8, 30, tzinfo=timezone.utc),
            monotonic_value=200.0,
        )
        transport = _ScriptedTransport(
            {
                ("GET", "/fapi/v2/positionRisk"): [
                    BinanceApiError(
                        -1003,
                        "IP banned",
                        http_status=418,
                        headers={"Retry-After": "5"},
                    ),
                ],
            }
        )
        provider = BinanceExchangeEvidenceProvider(
            transport=transport,
            now=clock.now,
            monotonic=clock.monotonic,
            jitter=lambda _delay: 0.0,
        )

        with self.assertRaisesRegex(ExchangeCancelError, "rate limited"):
            provider.snapshot()

        clock.advance(4.99)
        with self.assertRaisesRegex(ExchangeCancelError, "circuit open"):
            provider.snapshot()
        self.assertEqual(len(transport.calls), 1)

    def test_generic_failures_use_bounded_exponential_backoff_and_circuit(
        self,
    ) -> None:
        clock = _VirtualClock(
            datetime(2026, 8, 8, 8, 30, tzinfo=timezone.utc),
            monotonic_value=300.0,
        )
        transport = _ScriptedTransport(
            {
                ("GET", "/fapi/v2/positionRisk"): [
                    RuntimeError(f"failure-{index}")
                    for index in range(5)
                ],
            }
        )
        provider = BinanceExchangeEvidenceProvider(
            transport=transport,
            now=clock.now,
            monotonic=clock.monotonic,
            jitter=lambda delay: delay,
        )
        expected_delays = (0.625, 1.25, 2.5, 4.0, 4.0)

        for attempt, expected_delay in enumerate(expected_delays, start=1):
            with self.assertRaisesRegex(
                ExchangeCancelError,
                f"failure-{attempt - 1}",
            ):
                provider.snapshot()
            delay = provider._next_attempt_at - clock.monotonic()
            self.assertEqual(delay, expected_delay)
            self.assertLessEqual(delay, 4.0)
            self.assertEqual(len(transport.calls), attempt)

            with self.assertRaisesRegex(
                ExchangeCancelError,
                "backoff|circuit",
            ):
                provider.snapshot()
            self.assertEqual(len(transport.calls), attempt)
            clock.advance(delay)

        self.assertEqual(provider._circuit_open_until, provider._next_attempt_at)

    def test_malformed_exchange_collection_fails_closed(self) -> None:
        transport = _ScriptedTransport(
            {
                ("GET", "/fapi/v2/positionRisk"): [{"positions": []}],
            }
        )
        provider = BinanceExchangeEvidenceProvider(
            transport=transport,
        )

        with self.assertRaisesRegex(
            ExchangeCancelError,
            "invalid collection",
        ):
            provider.snapshot()


class SignedBinanceTransportTest(unittest.TestCase):
    def test_reuses_one_connection_across_signed_requests(self) -> None:
        connection = _RecordingHttpConnection(
            [
                _HttpResponse({"status": "CANCELED"}),
                _HttpResponse([]),
            ]
        )
        factory_timeouts: list[float] = []

        def connection_factory(timeout: float) -> Any:
            factory_timeouts.append(timeout)
            return connection

        transport = SignedBinanceTransport(
            base_url="https://fapi.binance.com",
            api_key="api-key",
            api_secret="api-secret",
            timestamp_ms=lambda: 1_700_000_000_000,
            connection_factory=connection_factory,
        )

        transport.request(
            "DELETE",
            "/fapi/v1/order",
            {"symbol": "BTCUSDT", "orderId": "42"},
        )
        transport.request(
            "GET",
            "/fapi/v2/positionRisk",
            {},
        )

        self.assertEqual(factory_timeouts, [10.0])
        self.assertEqual(
            [request[0] for request in connection.requests],
            ["DELETE", "GET"],
        )

    def test_connection_failure_rebuilds_on_next_request(self) -> None:
        failed = _RecordingHttpConnection(
            [OSError("connection reset")]
        )
        recovered = _RecordingHttpConnection(
            [_HttpResponse([])]
        )
        connections = [failed, recovered]
        factory_calls = 0

        def connection_factory(timeout: float) -> Any:
            nonlocal factory_calls
            del timeout
            factory_calls += 1
            return connections.pop(0)

        transport = SignedBinanceTransport(
            base_url="https://fapi.binance.com",
            api_key="api-key",
            api_secret="api-secret",
            connection_factory=connection_factory,
        )

        with self.assertRaisesRegex(ExchangeCancelError, "connection reset"):
            transport.request(
                "GET",
                "/fapi/v2/positionRisk",
                {},
            )
        result = transport.request(
            "GET",
            "/fapi/v2/positionRisk",
            {},
        )

        self.assertEqual(result, [])
        self.assertEqual(factory_calls, 2)
        self.assertTrue(failed.closed)

    def test_proxy_opener_handles_signed_cancel_and_evidence_requests(
        self,
    ) -> None:
        proxy_url = "http://100.107.72.78:13128"
        connection = _RecordingHttpConnection(
            [
                _HttpResponse({"status": "CANCELED"}),
                _HttpResponse([]),
            ]
        )
        with patch.object(
            EXCHANGE_CANCEL_ADAPTER.http.client,
            "HTTPSConnection",
            return_value=connection,
        ) as connection_type:
            transport = SignedBinanceTransport(
                base_url="https://fapi.binance.com",
                api_key="api-key",
                api_secret="api-secret",
                proxy_url=proxy_url,
                timestamp_ms=lambda: 1_700_000_000_000,
            )
            transport.request(
                "DELETE",
                "/fapi/v1/order",
                {"symbol": "BTCUSDT", "orderId": "42"},
            )
            transport.request(
                "GET",
                "/fapi/v2/positionRisk",
                {},
            )

        connection_type.assert_called_once_with(
            "100.107.72.78",
            13128,
            timeout=10.0,
        )
        self.assertEqual(
            connection.tunnels,
            [("fapi.binance.com", 443)],
        )
        self.assertEqual(
            [
                method
                for method, _target, _body, _headers
                in connection.requests
            ],
            ["DELETE", "GET"],
        )
        self.assertEqual(
            [
                EXCHANGE_CANCEL_ADAPTER.urllib.parse.urlsplit(
                    target
                ).path
                for _method, target, _body, _headers
                in connection.requests
            ],
            ["/fapi/v1/order", "/fapi/v2/positionRisk"],
        )

    def test_proxy_url_rejects_non_http_and_embedded_user_info(
        self,
    ) -> None:
        invalid_proxy_urls = (
            "socks5://100.107.72.78:1080",
            "http://proxy-user@100.107.72.78:13128",
            "https://proxy-user:proxy-password@proxy.example:443",
            "http://100.107.72.78:not-a-port",
            "https://",
        )

        for proxy_url in invalid_proxy_urls:
            with self.subTest(proxy_url=proxy_url):
                with self.assertRaises(ValueError):
                    SignedBinanceTransport(
                        base_url="https://fapi.binance.com",
                        api_key="api-key",
                        api_secret="api-secret",
                        proxy_url=proxy_url,
                    )

    def test_signed_request_includes_extended_recv_window_in_signature(self) -> None:
        connection = _RecordingHttpConnection(
            [_HttpResponse({"status": "CANCELED"})]
        )
        transport = SignedBinanceTransport(
            base_url="https://fapi.binance.com",
            api_key="api-key",
            api_secret="api-secret",
            timestamp_ms=lambda: 1_700_000_000_000,
            connection_factory=lambda _timeout: connection,
        )
        transport.request(
            "DELETE",
            "/fapi/v1/order",
            {"symbol": "BTCUSDT", "orderId": "42"},
        )

        _method, target, _body, _headers = connection.requests[0]
        query = EXCHANGE_CANCEL_ADAPTER.urllib.parse.parse_qs(
            EXCHANGE_CANCEL_ADAPTER.urllib.parse.urlsplit(target).query
        )
        unsigned_query, signature = (
            EXCHANGE_CANCEL_ADAPTER.urllib.parse.urlsplit(target)
            .query.rsplit("&signature=", 1)
        )
        expected_signature = hmac.new(
            b"api-secret",
            unsigned_query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(query["recvWindow"], ["30000"])
        self.assertEqual(query["timestamp"], ["1700000000000"])
        self.assertEqual(signature, expected_signature)

    def test_http_error_preserves_status_headers_and_retry_after(self) -> None:
        response = _HttpResponse(
            {"code": -1003, "msg": "Too many requests"},
            status=429,
            headers={"Retry-After": "2"},
        )
        connection = _RecordingHttpConnection([response])
        transport = SignedBinanceTransport(
            base_url="https://fapi.binance.com",
            api_key="api-key",
            api_secret="api-secret",
            timeout_seconds=10.0,
            timestamp_ms=lambda: 1_700_000_000_000,
            connection_factory=lambda _timeout: connection,
        )
        with self.assertRaises(BinanceApiError) as raised:
            transport.request(
                "GET",
                "/fapi/v2/positionRisk",
                {},
                timeout_seconds=1.5,
            )

        self.assertEqual(connection.timeout, 1.5)
        self.assertEqual(raised.exception.http_status, 429)
        self.assertEqual(raised.exception.headers["retry-after"], "2")


class _ScriptedTransport:
    def __init__(self, scripts: dict[tuple[str, str], list[Any]]) -> None:
        self.scripts = {key: list(values) for key, values in scripts.items()}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        del timeout_seconds
        self.calls.append((method, path, dict(params)))
        value = self.scripts[(method, path)].pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class _BlockingEvidenceTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.first_request_started = Event()
        self.release_first_request = Event()
        self._lock = Lock()

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        del timeout_seconds
        with self._lock:
            self.calls.append((method, path, dict(params)))
            call_number = len(self.calls)
        if call_number == 1:
            self.first_request_started.set()
            self.release_first_request.wait(timeout=1.0)
        if path == "/fapi/v1/openAlgoOrders":
            return {"orders": []}
        return []


class _HttpResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        will_close: bool = False,
    ) -> None:
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.will_close = will_close
        self.closed = False

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        self.closed = True


class _RecordingHttpConnection:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requests: list[
            tuple[str, str, bytes | None, dict[str, str]]
        ] = []
        self.timeout = 0.0
        self.sock = None
        self.closed = False
        self.tunnels: list[tuple[str, int]] = []

    def set_tunnel(self, host: str, port: int) -> None:
        self.tunnels.append((host, port))

    def request(
        self,
        method: str,
        target: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        response = self._responses[0]
        if isinstance(response, Exception):
            self._responses.pop(0)
            raise response
        self.requests.append(
            (method, target, body, dict(headers or {}))
        )

    def getresponse(self) -> Any:
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


class _RecordingUrlOpener:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[Any, float]] = []

    def open(self, request: Any, *, timeout: float) -> Any:
        self.calls.append((request, timeout))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _VirtualClock:
    def __init__(
        self,
        wall_time: datetime,
        *,
        monotonic_value: float = 0.0,
    ) -> None:
        self._wall_time = wall_time
        self._monotonic_value = monotonic_value

    def advance(self, seconds: float) -> None:
        self._wall_time += timedelta(seconds=seconds)
        self._monotonic_value += seconds

    def monotonic(self) -> float:
        return self._monotonic_value

    def now(self) -> datetime:
        return self._wall_time


class _TimedEvidenceTransport:
    def __init__(
        self,
        clock: _VirtualClock,
        *,
        delays: dict[str, float],
    ) -> None:
        self._clock = clock
        self._delays = delays
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.timeouts: list[float] = []

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        if timeout_seconds is None:
            raise AssertionError("evidence request must carry remaining deadline")
        self.calls.append((method, path, dict(params)))
        self.timeouts.append(timeout_seconds)
        delay = self._delays[path]
        elapsed = min(delay, timeout_seconds)
        self._clock.advance(elapsed)
        if delay > timeout_seconds:
            raise ExchangeCancelError(f"{path} deadline exceeded")
        if path == "/fapi/v1/openAlgoOrders":
            return {"orders": []}
        return []


class _JsonResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        import json

        self._body = BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body.read()


def _mirror() -> ControlPlaneExchangeStateMirror:
    return ControlPlaneExchangeStateMirror(
        account_id=ACCOUNT_ID,
        node_id="node-a",
        base_url="http://control-plane:8080",
        token="node-token",
    )


def _mirror_response(*, stale: bool, client_order_id: str) -> _JsonResponse:
    return _JsonResponse(
        {
            "account_id": ACCOUNT_ID,
            "stale": stale,
            "updated_at": "2026-07-29T12:00:00+00:00",
            "payload": {
                "open_orders": [
                    {
                        "symbol": "BTCUSDT",
                        "position_side": "LONG",
                        "client_order_id": client_order_id,
                        "venue_order_id": "42",
                        "order_id": "42",
                        "type": "LIMIT",
                        "side": "SELL",
                        "quantity": "0.5",
                    }
                ],
                "algo_orders": [],
            },
        }
    )


def _adapter(transport: _ScriptedTransport) -> BinanceExchangeCancelAdapter:
    return BinanceExchangeCancelAdapter(
        account_id=ACCOUNT_ID,
        transport=transport,
        confirmation_timeout_seconds=0.01,
        poll_interval_seconds=0,
        sleep=lambda _seconds: None,
    )


def _request(**overrides: Any) -> CancelOrderRequest:
    values = {
        "account_id": ACCOUNT_ID,
        "symbol": "BTCUSDT",
        "position_side": "LONG",
        "order_kind": "regular",
        "venue_order_id": "42",
        "client_order_id": "B0123456789abcdef0123456789abcdef01",
    }
    values.update(overrides)
    return CancelOrderRequest(**values)


def _refresh_request(
    worker: TerminalExchangeWorker,
    request_id: str,
) -> TerminalExchangeRequest:
    return TerminalExchangeRequest(
        request_id=request_id,
        account_id=ACCOUNT_ID,
        operation="refresh",
        purpose="test",
        deadline_monotonic=worker.new_deadline(),
    )


class _CountingMirror:
    def __init__(self) -> None:
        self.refresh_count = 0

    def refresh(self, **_kwargs: Any) -> tuple[Any, ...]:
        self.refresh_count += 1
        return ()


class _BlockingMirror:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def refresh(self, **_kwargs: Any) -> tuple[Any, ...]:
        self.started.set()
        self.release.wait(timeout=1)
        return ()


class _SlowMirror:
    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds

    def refresh(self, **_kwargs: Any) -> tuple[Any, ...]:
        time.sleep(self._delay_seconds)
        return ()


class _NoopCancelAdapter:
    def cancel(self, *_args: Any, **_kwargs: Any) -> Any:
        return None


if __name__ == "__main__":
    unittest.main()
