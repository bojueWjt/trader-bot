from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

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

    def test_recorder_covers_all_four_execution_accounts(self) -> None:
        module = _load_module()

        self.assertEqual(
            module.ACCOUNTS,
            {
                "account-a": (
                    "trader-v3-node-a",
                    "BINANCE_ACCOUNT_A",
                ),
                "account-b": (
                    "trader-v3-node-b",
                    "BINANCE_ACCOUNT_B",
                ),
                "account-c": (
                    "trader-v3-node-c",
                    "BINANCE_ACCOUNT_C",
                ),
                "account-d": (
                    "trader-v3-node-d",
                    "BINANCE_ACCOUNT_D",
                ),
            },
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

    def test_build_binance_opener_reads_http_proxy_from_environment(self) -> None:
        module = _load_module()

        with patch.dict(
            module.os.environ,
            {"BINANCE_PROXY_URL": "http://100.107.72.78:13128"},
        ):
            opener = module.build_binance_opener()

        proxy_handlers = [
            handler
            for handler in opener.handlers
            if isinstance(handler, module.urllib.request.ProxyHandler)
        ]
        self.assertEqual(len(proxy_handlers), 1)
        self.assertEqual(
            proxy_handlers[0].proxies,
            {
                "http": "http://100.107.72.78:13128",
                "https": "http://100.107.72.78:13128",
            },
        )

    def test_build_binance_opener_accepts_https_proxy(self) -> None:
        module = _load_module()

        opener = module.build_binance_opener(
            "https://proxy.example.test:8443"
        )

        proxy_handlers = [
            handler
            for handler in opener.handlers
            if isinstance(handler, module.urllib.request.ProxyHandler)
        ]
        self.assertEqual(
            proxy_handlers[0].proxies,
            {
                "http": "https://proxy.example.test:8443",
                "https": "https://proxy.example.test:8443",
            },
        )

    def test_build_binance_opener_disables_implicit_proxy_without_config(
        self,
    ) -> None:
        module = _load_module()

        with patch.dict(module.os.environ, {}, clear=True), patch.object(
            module.urllib.request,
            "build_opener",
        ) as build_opener:
            module.build_binance_opener()

        proxy_handler = build_opener.call_args.args[0]
        self.assertIsInstance(
            proxy_handler,
            module.urllib.request.ProxyHandler,
        )
        self.assertEqual(proxy_handler.proxies, {})

    def test_build_binance_opener_rejects_invalid_proxy_scheme(self) -> None:
        module = _load_module()

        with self.assertRaisesRegex(
            ValueError,
            "BINANCE_PROXY_URL must be an http\\(s\\) URL",
        ):
            module.build_binance_opener("socks5://127.0.0.1:1080")

    def test_build_binance_opener_rejects_embedded_credentials(self) -> None:
        module = _load_module()

        with self.assertRaisesRegex(
            ValueError,
            "BINANCE_PROXY_URL must not contain credentials",
        ):
            module.build_binance_opener(
                "http://proxy-user:proxy-secret@127.0.0.1:13128"
            )

    def test_signed_get_includes_extended_recv_window(self) -> None:
        module = _load_module()
        response = io.BytesIO(b"{}")
        opener = Mock()
        opener.open.return_value = response

        with patch.object(module.time, "time", return_value=1_700_000_000):
            module.signed_get(
                "https://fapi.binance.com",
                "/fapi/v1/openOrders",
                "api-key",
                "api-secret",
                opener=opener,
            )

        request = opener.open.call_args.args[0]
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
        opener.open.assert_called_once_with(request, timeout=15)

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
        opener = Mock()
        opener.open.side_effect = error

        with self.assertRaisesRegex(
            RuntimeError,
            r"Binance API -1021: Timestamp .* recvWindow",
        ):
            module.signed_get(
                "https://fapi.binance.com",
                "/fapi/v1/openOrders",
                "api-key",
                "api-secret",
                opener=opener,
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
        opener = Mock()
        opener.open.side_effect = error

        with self.assertRaisesRegex(
            RuntimeError,
            r'Binance API 400: \["unexpected", "shape"\]',
        ):
            module.signed_get(
                "https://fapi.binance.com",
                "/fapi/v1/openOrders",
                "api-key",
                "api-secret",
                opener=opener,
            )

    def test_snapshot_account_routes_all_signed_gets_through_one_opener(
        self,
    ) -> None:
        module = _load_module()
        opener = object()
        responses = {
            "/fapi/v3/account": {
                "totalMarginBalance": "1000",
                "totalInitialMargin": "0",
                "availableBalance": "1000",
            },
            "/fapi/v2/positionRisk": [],
            "/fapi/v1/openOrders": [],
            "/fapi/v1/openAlgoOrders": {"orders": []},
        }

        def fake_signed_get(
            base,
            path,
            key,
            secret,
            params=None,
            opener=None,
        ):
            self.assertIs(opener, expected_opener)
            return responses[path]

        expected_opener = opener
        with patch.object(module, "signed_get", side_effect=fake_signed_get):
            module.snapshot_account(
                "https://fapi.binance.com",
                "api-key",
                "api-secret",
                opener=opener,
            )

    def test_canonical_account_summary_accepts_empty_account(self) -> None:
        module = _load_module()

        summary = module.canonical_account_summary(
            {
                "totalMarginBalance": "0",
                "totalInitialMargin": "0.00000000",
                "availableBalance": "0E-8",
            }
        )

        self.assertEqual(
            summary,
            {
                "currency": "USDT",
                "equity": "0",
                "margin": "0E-8",
                "free": "0E-8",
            },
        )

    def test_canonical_account_summary_rejects_negative_equity(self) -> None:
        module = _load_module()

        with self.assertRaisesRegex(
            ValueError,
            "totalMarginBalance must be non-negative",
        ):
            module.canonical_account_summary(
                {
                    "totalMarginBalance": "-0.01",
                    "totalInitialMargin": "0",
                    "availableBalance": "0",
                }
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
