from __future__ import annotations

import importlib.util
import hashlib
import hmac
import sys
import unittest
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch


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
OPENING_CONFIRMED_EXECUTED = (
    EXCHANGE_CANCEL_ADAPTER.OPENING_CONFIRMED_EXECUTED
)
OPENING_DEFINITIVELY_ABSENT = (
    EXCHANGE_CANCEL_ADAPTER.OPENING_DEFINITIVELY_ABSENT
)
OPENING_UNKNOWN = EXCHANGE_CANCEL_ADAPTER.OPENING_UNKNOWN
OrderAlreadyFilledError = EXCHANGE_CANCEL_ADAPTER.OrderAlreadyFilledError
SignedBinanceTransport = EXCHANGE_CANCEL_ADAPTER.SignedBinanceTransport
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


class ExchangeStateMirrorTest(unittest.TestCase):
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

    def test_historical_filled_order_is_confirmed_after_refresh(self) -> None:
        client_order_id = "B1111111111111111111111111111111101"
        response = _opening_mirror_response(
            client_order_id=client_order_id,
            state=OPENING_CONFIRMED_EXECUTED,
            order_status="filled",
            sources=["orders_projection", "execution_events"],
        )
        mirror = _mirror()

        with patch.object(
            EXCHANGE_CANCEL_ADAPTER.urllib.request,
            "urlopen",
            return_value=response,
        ):
            mirror.refresh()

        evidence = mirror.opening_execution_state(client_order_id)
        self.assertEqual(evidence.state, OPENING_CONFIRMED_EXECUTED)
        self.assertEqual(evidence.order_status, "filled")
        self.assertEqual(evidence.venue_order_id, "venue-42")
        self.assertEqual(
            evidence.sources,
            ("orders_projection", "execution_events"),
        )
        self.assertEqual(
            mirror.find_order("BTCUSDT-PERP.BINANCE", client_order_id),
            False,
        )

    def test_explicit_rejection_is_definitively_absent(self) -> None:
        client_order_id = "B2222222222222222222222222222222201"
        mirror = _mirror()

        with patch.object(
            EXCHANGE_CANCEL_ADAPTER.urllib.request,
            "urlopen",
            return_value=_opening_mirror_response(
                client_order_id=client_order_id,
                state=OPENING_DEFINITIVELY_ABSENT,
                order_status="rejected",
                sources=["orders_projection"],
            ),
        ):
            mirror.refresh()

        evidence = mirror.opening_execution_state(client_order_id)
        self.assertEqual(evidence.state, OPENING_DEFINITIVELY_ABSENT)
        self.assertEqual(evidence.reason, "projection_status_rejected")

    def test_missing_stale_or_failed_evidence_is_unknown(self) -> None:
        client_order_id = "B3333333333333333333333333333333301"
        mirror = _mirror()

        before_refresh = mirror.opening_execution_state(client_order_id)
        self.assertEqual(before_refresh.state, OPENING_UNKNOWN)
        self.assertEqual(before_refresh.reason, "mirror_not_fresh")

        with patch.object(
            EXCHANGE_CANCEL_ADAPTER.urllib.request,
            "urlopen",
            return_value=_opening_mirror_response(
                client_order_id=client_order_id,
                state=OPENING_CONFIRMED_EXECUTED,
                order_status="filled",
                sources=["orders_projection"],
                authoritative=False,
            ),
        ):
            mirror.refresh()

        unavailable = mirror.opening_execution_state(client_order_id)
        self.assertEqual(unavailable.state, OPENING_UNKNOWN)
        self.assertEqual(unavailable.reason, "evidence_not_authoritative")

        with patch.object(
            EXCHANGE_CANCEL_ADAPTER.urllib.request,
            "urlopen",
            side_effect=EXCHANGE_CANCEL_ADAPTER.URLError("timeout"),
        ):
            with self.assertRaises(ExchangeCancelError):
                mirror.refresh()

        failed = mirror.opening_execution_state(client_order_id)
        self.assertEqual(failed.state, OPENING_UNKNOWN)
        self.assertEqual(failed.reason, "mirror_not_fresh")

    def test_evidence_from_another_account_is_ignored(self) -> None:
        client_order_id = "B4444444444444444444444444444444401"
        response = _opening_mirror_response(
            client_order_id=client_order_id,
            state=OPENING_CONFIRMED_EXECUTED,
            order_status="filled",
            sources=["orders_projection"],
            evidence_account_id="account-b",
        )
        mirror = _mirror()

        with patch.object(
            EXCHANGE_CANCEL_ADAPTER.urllib.request,
            "urlopen",
            return_value=response,
        ):
            mirror.refresh()

        evidence = mirror.opening_execution_state(client_order_id)
        self.assertEqual(evidence.state, OPENING_UNKNOWN)
        self.assertEqual(evidence.reason, "no_authoritative_evidence")


class SignedBinanceTransportTest(unittest.TestCase):
    def test_signed_request_includes_extended_recv_window_in_signature(self) -> None:
        response = _JsonResponse({"status": "CANCELED"})
        transport = SignedBinanceTransport(
            base_url="https://fapi.binance.com",
            api_key="api-key",
            api_secret="api-secret",
            timestamp_ms=lambda: 1_700_000_000_000,
        )

        with patch.object(
            EXCHANGE_CANCEL_ADAPTER.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            transport.request(
                "DELETE",
                "/fapi/v1/order",
                {"symbol": "BTCUSDT", "orderId": "42"},
            )

        request = urlopen.call_args.args[0]
        query = EXCHANGE_CANCEL_ADAPTER.urllib.parse.parse_qs(
            EXCHANGE_CANCEL_ADAPTER.urllib.parse.urlsplit(request.full_url).query
        )
        unsigned_query, signature = (
            EXCHANGE_CANCEL_ADAPTER.urllib.parse.urlsplit(request.full_url)
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


class _ScriptedTransport:
    def __init__(self, scripts: dict[tuple[str, str], list[Any]]) -> None:
        self.scripts = {key: list(values) for key, values in scripts.items()}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, path: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, path, dict(params)))
        value = self.scripts[(method, path)].pop(0)
        if isinstance(value, Exception):
            raise value
        return value


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


def _opening_mirror_response(
    *,
    client_order_id: str,
    state: str,
    order_status: str,
    sources: list[str],
    authoritative: bool = True,
    evidence_account_id: str = ACCOUNT_ID,
) -> _JsonResponse:
    return _JsonResponse(
        {
            "account_id": ACCOUNT_ID,
            "stale": False,
            "updated_at": "2026-08-09T12:00:00+00:00",
            "payload": {
                "open_orders": [],
                "algo_orders": [],
            },
            "opening_execution_evidence": {
                "authoritative": authoritative,
                "reason": "fresh_account_scoped_evidence",
                "items": [
                    {
                        "account_id": evidence_account_id,
                        "client_order_id": client_order_id,
                        "state": state,
                        "order_status": order_status,
                        "instrument_id": "BTCUSDT-PERP.BINANCE",
                        "venue_order_id": "venue-42",
                        "filled_quantity": "0.01",
                        "sources": sources,
                        "observed_at": "2026-08-09T11:59:00+00:00",
                        "reason": f"projection_status_{order_status}",
                    }
                ],
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


if __name__ == "__main__":
    unittest.main()
