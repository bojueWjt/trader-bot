from __future__ import annotations

import importlib.util
import hashlib
import hmac
import io
import runpy
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
    def test_live_mirror_remains_historical_while_canonical_source_drifts(
        self,
    ) -> None:
        mirror_path = (
            REPO_ROOT / ".live-mirror" / "tools" / "exchange_state_recorder.py"
        )
        mirror_bytes = mirror_path.read_bytes()

        self.assertEqual(
            hashlib.sha256(mirror_bytes).hexdigest(),
            "7c6021ecd4b1e94bbd3be05dee820e2179f7d28198578e040ae5c0633cb7435d",
        )
        self.assertNotEqual(mirror_bytes, MODULE_PATH.read_bytes())

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

    def test_slim_position_preserves_stable_risk_fields(self) -> None:
        module = _load_module()

        row = module.slim_position(
            {
                "symbol": "SOLUSDT",
                "positionAmt": "0.07",
                "entryPrice": "77.10",
                "positionSide": "LONG",
                "leverage": "5",
                "marginType": "isolated",
                "isolatedMargin": "1.25",
                "isAutoAddMargin": "false",
            }
        )

        self.assertEqual(row["symbol"], "SOLUSDT")
        self.assertEqual(row["position_amt"], "0.07")
        self.assertEqual(row["entry_price"], "77.10")
        self.assertEqual(row["position_side"], "LONG")
        self.assertEqual(row["leverage"], "5")
        self.assertEqual(row["margin_type"], "isolated")
        self.assertEqual(row["isolated_margin"], "1.25")
        self.assertEqual(row["is_auto_add_margin"], "false")

    def test_slim_position_accepts_missing_optional_fields(self) -> None:
        module = _load_module()

        row = module.slim_position(
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.01",
            }
        )

        self.assertEqual(row["symbol"], "BTCUSDT")
        self.assertEqual(row["position_amt"], "0.01")
        self.assertIsNone(row["entry_price"])
        self.assertIsNone(row["position_side"])
        self.assertIsNone(row["leverage"])
        self.assertIsNone(row["margin_type"])
        self.assertIsNone(row["isolated_margin"])
        self.assertIsNone(row["is_auto_add_margin"])

    def test_slim_position_records_market_observation_fields(self) -> None:
        module = _load_module()

        row = module.slim_position(
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.01",
                "markPrice": "118100",
                "unRealizedProfit": "1",
                "notional": "1181",
                "liquidationPrice": "90000",
            }
        )

        self.assertEqual(row["mark_price"], "118100")
        self.assertEqual(row["unrealized_pnl"], "1")
        self.assertEqual(row["notional"], "1181")
        self.assertEqual(row["liquidation_price"], "90000")

    def test_slim_position_covers_canary_structural_baseline(self) -> None:
        module = _load_module()
        adapter_namespace = runpy.run_path(
            str(
                REPO_ROOT
                / "scripts"
                / "account_a_live_trade_http_adapter.py"
            )
        )
        baseline_fields = adapter_namespace[
            "NON_TARGET_POSITION_BASELINE_FIELDS"
        ]
        row = module.slim_position(
            {
                "symbol": "SOLUSDT",
                "positionAmt": "0.07",
                "entryPrice": "77.10",
                "positionSide": "LONG",
                "leverage": "5",
                "marginType": "isolated",
                "isolatedMargin": "1.25",
            }
        )

        self.assertEqual(
            baseline_fields,
            (
                "symbol",
                "position_side",
                "position_amt",
                "entry_price",
                "leverage",
                "margin_type",
                "isolated_margin",
                "is_auto_add_margin",
            ),
        )
        self.assertLessEqual(set(baseline_fields), set(row))

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
                    "leverage": "5",
                    "marginType": "isolated",
                    "isolatedMargin": "250",
                    "isAutoAddMargin": "false",
                    "notional": "1181",
                    "liquidationPrice": "90000",
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
            payload["positions"],
            [
                {
                    "symbol": "BTCUSDT",
                    "position_amt": "0.01",
                    "entry_price": "118000",
                    "position_side": "LONG",
                    "leverage": "5",
                    "margin_type": "isolated",
                    "isolated_margin": "250",
                    "is_auto_add_margin": "false",
                    "mark_price": "118100",
                    "unrealized_pnl": "1",
                    "notional": "1181",
                    "liquidation_price": "90000",
                }
            ],
        )
        self.assertEqual(
            payload["recent_order_history"][0]["status"],
            "FILLED",
        )
        self.assertEqual(
            payload["recent_algo_order_history"][0]["status"],
            "CANCELED",
        )
        self.assertEqual(
            payload["recent_order_history_coverage"][
                "searched_symbols"
            ],
            ["BTCUSDT"],
        )
        self.assertEqual(
            payload["recent_order_history_coverage"][
                "history_complete_symbols"
            ],
            ["BTCUSDT"],
        )

    def test_history_failure_marks_symbol_incomplete_and_keeps_core_snapshot(
        self,
    ) -> None:
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
            "/fapi/v1/allAlgoOrders": {
                "orders": [
                    {
                        "symbol": "BTCUSDT",
                        "clientAlgoId": (
                            "B2222222222222222222222222222222201"
                        ),
                        "algoStatus": "CANCELED",
                        "actualQty": "0",
                    }
                ]
            },
        }

        def fake_signed_get(_base, path, _key, _sec, params=None):
            if path == "/fapi/v1/allOrders":
                raise RuntimeError("regular history unavailable")
            return responses[path]

        with patch.object(module, "signed_get", side_effect=fake_signed_get):
            payload = module.snapshot_account(
                "https://fapi.binance.com",
                "api-key",
                "api-secret",
            )

        self.assertEqual(payload["account"]["equity"], "100")
        self.assertEqual(payload["positions"][0]["symbol"], "BTCUSDT")
        self.assertEqual(payload["recent_order_history"], [])
        self.assertEqual(
            payload["recent_algo_order_history"][0]["status"],
            "CANCELED",
        )
        coverage = payload["recent_order_history_coverage"]
        self.assertEqual(coverage["searched_symbols"], ["BTCUSDT"])
        self.assertEqual(
            coverage["regular_history_complete_symbols"],
            [],
        )
        self.assertEqual(
            coverage["algo_history_complete_symbols"],
            ["BTCUSDT"],
        )
        self.assertEqual(coverage["history_complete_symbols"], [])
        self.assertEqual(
            coverage["failed_requests"],
            [{"symbol": "BTCUSDT", "history": "regular"}],
        )
        self.assertTrue(coverage["degraded"])

    def test_history_request_budget_bounds_sequential_enrichment(
        self,
    ) -> None:
        module = _load_module()
        history_calls: list[tuple[str, str]] = []
        responses = {
            "/fapi/v3/account": {
                "totalMarginBalance": "100",
                "totalInitialMargin": "10",
                "availableBalance": "90",
            },
            "/fapi/v2/positionRisk": [],
            "/fapi/v1/openOrders": [],
            "/fapi/v1/openAlgoOrders": {"orders": []},
        }

        def fake_signed_get(_base, path, _key, _sec, params=None):
            if path in {
                "/fapi/v1/allOrders",
                "/fapi/v1/allAlgoOrders",
            }:
                history_calls.append((path, params["symbol"]))
                if path == "/fapi/v1/allAlgoOrders":
                    return {"orders": []}
                return []
            return responses[path]

        with patch.object(module, "signed_get", side_effect=fake_signed_get):
            payload = module.snapshot_account(
                "https://fapi.binance.com",
                "api-key",
                "api-secret",
                targeted_history_symbols=(
                    "BTCUSDT",
                    "ETHUSDT",
                    "SOLUSDT",
                    "XRPUSDT",
                ),
                history_symbol_limit=64,
                history_request_budget=4,
            )

        self.assertEqual(
            payload["recent_order_history_symbols"],
            ["BTCUSDT", "ETHUSDT"],
        )
        self.assertEqual(len(history_calls), 4)
        coverage = payload["recent_order_history_coverage"]
        self.assertEqual(coverage["request_budget"], 4)
        self.assertEqual(coverage["requests_attempted"], 4)
        self.assertTrue(coverage["truncated"])

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

    def test_pending_symbols_are_distinct_before_limit_and_ignore_expiry(
        self,
    ) -> None:
        module = _load_module()
        conn = _PendingSymbolConnection(
            [
                ("BTCUSDT-PERP.BINANCE",),
                ("ETHUSDT-PERP.BINANCE",),
            ]
        )

        symbols = module.pending_opening_symbols(
            conn,
            "account-a",
            limit=2,
            after_symbol="SOLUSDT",
        )

        self.assertEqual(symbols, ("BTCUSDT", "ETHUSDT"))
        normalized_sql = " ".join(conn.sql.split())
        group_position = normalized_sql.index(
            "GROUP BY ti.instrument_id"
        )
        limit_position = normalized_sql.rindex("LIMIT %s")
        self.assertLess(group_position, limit_position)
        self.assertNotIn("valid_until", normalized_sql)
        self.assertIn(
            "ti.status IN ('approved', 'expired')",
            normalized_sql,
        )
        self.assertIn("NOT EXISTS", normalized_sql)
        self.assertIn("FROM execution_events", normalized_sql)
        self.assertEqual(
            conn.params,
            (
                "account-a",
                "SOLUSDT-PERP.BINANCE",
                "SOLUSDT-PERP.BINANCE",
                2,
            ),
        )

    def test_run_once_rotates_pending_symbol_batches(
        self,
    ) -> None:
        module = _load_module()
        conn = _RecorderConnection()
        after_symbols: list[str] = []
        targeted_batches: list[tuple[str, ...]] = []

        def fake_pending_symbols(
            _conn,
            _account_id,
            *,
            limit,
            after_symbol="",
        ):
            self.assertEqual(limit, 2)
            after_symbols.append(after_symbol)
            if after_symbol == "":
                return ("BTCUSDT", "ETHUSDT")
            return ("SOLUSDT", "XRPUSDT")

        def fake_snapshot(
            _base,
            _key,
            _secret,
            *,
            targeted_history_symbols,
            history_symbol_limit,
            history_request_budget,
        ):
            self.assertEqual(history_symbol_limit, 2)
            self.assertEqual(history_request_budget, 4)
            targeted_batches.append(targeted_history_symbols)
            return _snapshot_payload()

        module._PENDING_SYMBOL_CURSOR_BY_ACCOUNT.clear()
        with patch.object(
            module,
            "ACCOUNTS",
            {"account-a": ("container-a", "PREFIX_A")},
        ), patch.object(
            module,
            "container_keys",
            return_value=("api-key", "api-secret"),
        ), patch.object(
            module,
            "_history_symbol_limit",
            return_value=64,
        ), patch.object(
            module,
            "_history_request_budget",
            return_value=4,
        ), patch.object(
            module,
            "pending_opening_symbols",
            side_effect=fake_pending_symbols,
        ), patch.object(
            module,
            "snapshot_account",
            side_effect=fake_snapshot,
        ):
            module.run_once(conn, "https://fapi.binance.com")
            module.run_once(conn, "https://fapi.binance.com")

        self.assertEqual(after_symbols, ["", "ETHUSDT"])
        self.assertEqual(
            targeted_batches,
            [
                ("BTCUSDT", "ETHUSDT"),
                ("SOLUSDT", "XRPUSDT"),
            ],
        )
        self.assertEqual(conn.commit_count, 2)

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


class _PendingSymbolConnection:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.sql = ""
        self.params = ()

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self):
        return list(self.rows)


class _RecorderConnection:
    def __init__(self) -> None:
        self.commit_count = 0
        self.rollback_count = 0
        self.executions: list[tuple[str, tuple]] = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params) -> None:
        self.executions.append((sql, params))

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


def _snapshot_payload() -> dict:
    return {
        "fetched_at": "2026-08-09T00:00:00Z",
        "account": {
            "currency": "USDT",
            "equity": "100",
            "margin": "10",
            "free": "90",
        },
    }


if __name__ == "__main__":
    unittest.main()
