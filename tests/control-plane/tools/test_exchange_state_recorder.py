from __future__ import annotations

import importlib.util
import hashlib
import hmac
import io
import sys
import types
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = (
    REPO_ROOT / "services" / "control-plane" / "tools" / "exchange_state_recorder.py"
)


class ExchangeStateRecorderTest(unittest.TestCase):
    def test_live_mirror_matches_deployment_source(self) -> None:
        mirror_path = (
            REPO_ROOT / ".live-mirror" / "tools" / "exchange_state_recorder.py"
        )

        self.assertEqual(
            mirror_path.read_bytes(),
            MODULE_PATH.read_bytes(),
        )

    def test_slim_order_preserves_hedge_position_side(self) -> None:
        module = _load_module()

        row = module.slim_order(
            {
                "symbol": "MUUSDT",
                "positionSide": "LONG",
                "side": "SELL",
                "type": "STOP_MARKET",
                "algoId": 770,
            }
        )

        self.assertEqual(row["position_side"], "LONG")

    def test_slim_order_preserves_terminal_execution_evidence(self) -> None:
        module = _load_module()

        row = module.slim_order(
            {
                "symbol": "BTCUSDT",
                "positionSide": "LONG",
                "side": "BUY",
                "type": "MARKET",
                "orderId": 42,
                "clientOrderId": "B1111111111111111111111111111111101",
                "status": "FILLED",
                "executedQty": "0.01",
                "avgPrice": "118000",
                "time": 1_754_700_000_000,
                "updateTime": 1_754_700_001_000,
            }
        )

        self.assertEqual(row["status"], "FILLED")
        self.assertEqual(row["executed_quantity"], "0.01")
        self.assertEqual(row["average_price"], "118000")
        self.assertEqual(row["created_at_ms"], 1_754_700_000_000)
        self.assertEqual(row["updated_at_ms"], 1_754_700_001_000)

    def test_snapshot_records_recent_regular_and_algo_history(self) -> None:
        module = _load_module()
        responses = {
            "/fapi/v3/account": {
                "totalMarginBalance": "100",
                "totalInitialMargin": "10",
                "availableBalance": "90",
            },
            "/fapi/v2/positionRisk": [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.01",
                    "entryPrice": "118000",
                    "markPrice": "118100",
                    "unRealizedProfit": "1",
                    "positionSide": "LONG",
                }
            ],
            "/fapi/v1/openOrders": [],
            "/fapi/v1/openAlgoOrders": {"orders": []},
            "/fapi/v1/allOrders": [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "LONG",
                    "side": "BUY",
                    "type": "MARKET",
                    "orderId": 42,
                    "clientOrderId": "B1111111111111111111111111111111101",
                    "status": "FILLED",
                    "executedQty": "0.01",
                }
            ],
            "/fapi/v1/allAlgoOrders": {
                "orders": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                        "side": "BUY",
                        "orderType": "STOP_MARKET",
                        "algoId": 43,
                        "clientAlgoId": "B2222222222222222222222222222222201",
                        "algoStatus": "CANCELED",
                        "actualQty": "0",
                    }
                ]
            },
        }

        def fake_signed_get(_base, path, _key, _sec, params=None):
            if path in {"/fapi/v1/allOrders", "/fapi/v1/allAlgoOrders"}:
                self.assertEqual(params["symbol"], "BTCUSDT")
                self.assertEqual(params["limit"], 1000)
            return responses[path]

        with patch.object(module, "signed_get", side_effect=fake_signed_get):
            payload = module.snapshot_account(
                "https://fapi.binance.com",
                "api-key",
                "api-secret",
            )

        self.assertEqual(payload["recent_order_history_symbols"], ["BTCUSDT"])
        self.assertEqual(
            payload["recent_order_history"][0]["status"],
            "FILLED",
        )
        self.assertEqual(
            payload["recent_algo_order_history"][0]["status"],
            "CANCELED",
        )

    def test_targeted_history_symbols_are_prioritized_and_bounded(
        self,
    ) -> None:
        module = _load_module()

        symbols = module._recent_history_symbols(
            [{"symbol": "SOLUSDT"}],
            [{"symbol": "XRPUSDT"}],
            [],
            (
                "ETHUSDT-PERP.BINANCE",
                "BTCUSDT",
                "ETHUSDT",
            ),
            max_symbols=3,
        )

        self.assertEqual(
            symbols,
            ("ETHUSDT", "BTCUSDT", "SOLUSDT"),
        )

    def test_history_symbol_limit_is_configurable_and_capped(
        self,
    ) -> None:
        module = _load_module()

        with patch.dict(
            module.os.environ,
            {"EXCHANGE_STATE_HISTORY_MAX_SYMBOLS": "999"},
        ):
            self.assertEqual(
                module._history_symbol_limit(),
                module.ABSOLUTE_RECENT_HISTORY_MAX_SYMBOLS,
            )

    def test_signed_get_includes_extended_recv_window(self) -> None:
        module = _load_module()
        response = io.BytesIO(b"{}")

        with patch.object(module.time, "time", return_value=1_700_000_000), patch.object(
            module.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            module.signed_get(
                "https://fapi.binance.com",
                "/fapi/v1/openOrders",
                "api-key",
                "api-secret",
            )

        request = urlopen.call_args.args[0]
        query = module.urllib.parse.parse_qs(
            module.urllib.parse.urlsplit(request.full_url).query
        )
        unsigned_query, signature = (
            module.urllib.parse.urlsplit(request.full_url)
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

    def test_signed_get_reports_binance_error_code_and_message(self) -> None:
        module = _load_module()
        error = HTTPError(
            "https://fapi.binance.com/fapi/v1/openOrders",
            400,
            "Bad Request",
            {},
            io.BytesIO(
                b'{"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}'
            ),
        )

        with patch.object(module.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(
                RuntimeError,
                r"Binance API -1021: Timestamp .* recvWindow",
            ):
                module.signed_get(
                    "https://fapi.binance.com",
                    "/fapi/v1/openOrders",
                    "api-key",
                    "api-secret",
                )

    def test_signed_get_preserves_non_object_json_error_body(self) -> None:
        module = _load_module()
        error = HTTPError(
            "https://fapi.binance.com/fapi/v1/openOrders",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'["unexpected", "shape"]'),
        )

        with patch.object(module.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(
                RuntimeError,
                r'Binance API 400: \["unexpected", "shape"\]',
            ):
                module.signed_get(
                    "https://fapi.binance.com",
                    "/fapi/v1/openOrders",
                    "api-key",
                    "api-secret",
                )


def _load_module() -> types.ModuleType:
    modules: dict[str, types.ModuleType] = {}
    if "psycopg2" not in sys.modules:
        try:
            __import__("psycopg2")
        except ModuleNotFoundError:
            modules["psycopg2"] = types.ModuleType("psycopg2")
    spec = importlib.util.spec_from_file_location(
        "_exchange_state_recorder_under_test",
        MODULE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load exchange state recorder: {MODULE_PATH}")
    with patch.dict(sys.modules, modules):
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
