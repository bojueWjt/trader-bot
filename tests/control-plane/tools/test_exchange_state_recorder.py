from __future__ import annotations

import importlib.util
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
        self.assertEqual(query["recvWindow"], ["30000"])
        self.assertEqual(query["timestamp"], ["1700000000000"])
        self.assertIn("signature", query)

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
