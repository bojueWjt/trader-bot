from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[3]
PATCH_PATH = REPO_ROOT / "container-patches" / "binance_futures_execution.py"
COMMON_PATH = REPO_ROOT / "container-patches" / "binance_execution.py"

try:
    import nautilus_trader
except ModuleNotFoundError as e:
    if e.name != "nautilus_trader":
        raise
    nautilus_trader = None


def _class_methods(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    client = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    return {
        node.name: node
        for node in client.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


class FuturesMinLeverageSourceTest(unittest.TestCase):
    def test_both_entry_points_share_inner_hook_before_common_submission(self) -> None:
        common = _class_methods(COMMON_PATH)
        for name in ("_submit_order", "_submit_order_list"):
            self.assertIn("self._submit_order_inner(", ast.unparse(common[name]))
        methods = _class_methods(PATCH_PATH)
        hook = ast.unparse(methods["_submit_order_inner"])
        self.assertLess(
            hook.index("await self._ensure_min_open_leverage("),
            hook.index("await super()._submit_order_inner("),
        )
        self.assertNotIn("generate_order_submitted", hook)
        self.assertIn("BINANCE_FUTURES_ALGO_ORDER_TYPES", hook)

    def test_environment_is_read_only_in_constructor(self) -> None:
        methods = _class_methods(PATCH_PATH)
        reads = [
            name for name, method in methods.items()
            if "os.environ.get(" in ast.unparse(method)
        ]
        self.assertEqual(reads, ["__init__"])
        constructor = ast.unparse(methods["__init__"])
        self.assertIn("BINANCE_MIN_OPEN_LEVERAGE", constructor)
        self.assertIn("except ValueError", constructor)
        self.assertIn("self._log.error(", constructor)

    def test_bracket_uses_signed_get_with_timestamp_and_configured_window(self) -> None:
        method = _class_methods(PATCH_PATH)["_ensure_min_open_leverage"]
        call = next(
            node for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "sign_request"
        )
        kwargs = {keyword.arg: keyword.value for keyword in call.keywords}
        self.assertEqual(ast.unparse(call.func), "self._http_client.sign_request")
        self.assertEqual(ast.unparse(kwargs["http_method"]), "HttpMethod.GET")
        self.assertEqual(ast.literal_eval(kwargs["url_path"]), "/fapi/v1/leverageBracket")
        payload = kwargs["payload"]
        values = {ast.literal_eval(key): ast.unparse(value)
                  for key, value in zip(payload.keys, payload.values)}
        self.assertEqual(values["symbol"], "symbol")
        self.assertEqual(values["recvWindow"], "str(self._recv_window)")
        self.assertEqual(values["timestamp"], "str(self._clock.timestamp_ms())")
        self.assertIn("binance:global", ast.literal_eval(kwargs["ratelimiter_keys"]))


class _FakeAccount:
    def __init__(self, events: list) -> None:
        self.values = {}
        self.events = events

    def leverage(self, instrument_id):
        return self.values.get(instrument_id)

    def set_leverage(self, instrument_id, leverage) -> None:
        self.values[instrument_id] = leverage
        self.events.append(("cache", instrument_id, leverage))


class _FakeClock:
    def timestamp_ms(self) -> int:
        return 1_800_000_000_123


class _FakeLog:
    def __init__(self) -> None:
        self.infos = []
        self.warnings = []
        self.errors = []

    def info(self, message, color=None) -> None:
        self.infos.append(message)

    def warning(self, message, color=None) -> None:
        self.warnings.append(message)

    def error(self, message, color=None) -> None:
        self.errors.append(message)


class _FakeHttp:
    def __init__(self, events: list) -> None:
        self.events = events
        self.bracket_calls = []
        self.set_calls = []
        self.caps = {}
        self.bracket_error = None
        self.bracket_response = None
        self.set_error = None
        self.before_bracket = None

    async def sign_request(self, **kwargs):
        self.bracket_calls.append(kwargs)
        symbol = kwargs["payload"]["symbol"]
        if self.before_bracket is not None:
            await self.before_bracket(symbol)
        await asyncio.sleep(0)
        if self.bracket_error is not None:
            raise self.bracket_error
        if self.bracket_response is not None:
            return self.bracket_response
        return json.dumps([
            {"symbol": symbol, "brackets": [
                {"initialLeverage": self.caps.get(symbol, 150)},
                {"initialLeverage": 1},
            ]},
        ]).encode()

    async def set_leverage(self, symbol, leverage, recv_window=None):
        self.set_calls.append((symbol, leverage, recv_window))
        await asyncio.sleep(0)
        if self.set_error is not None:
            raise self.set_error
        self.events.append(("exchange", symbol, leverage))
        return SimpleNamespace(symbol=symbol, leverage=leverage)


@unittest.skipIf(nautilus_trader is None, "nautilus_trader is not installed")
class FuturesMinLeverageBehaviorTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Load the real patch; do not hide import/API failures in an installed image.
        spec = importlib.util.spec_from_file_location("_futures_min_leverage_patch", PATCH_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load {PATCH_PATH}")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

        class Harness(cls.module.BinanceFuturesExecutionClient):
            # The Cython base exposes readonly fields. Python overrides allow a
            # minimal harness without constructing a live execution engine.
            @property
            def _log(self):
                return self.fake_log

            @_log.setter
            def _log(self, value):
                self.fake_log = value

            @property
            def _clock(self):
                return self.fake_clock

            @_clock.setter
            def _clock(self, value):
                self.fake_clock = value

            @property
            def _cache(self):
                return self.fake_cache

            def get_account(self):
                return self.fake_account

            def _get_cached_instrument_id(self, symbol):
                return cls.module.InstrumentId.from_str(f"{symbol}-PERP.BINANCE")

            def _get_position_side_from_position_id(self, **kwargs):
                return self.position_side

        cls.Harness = Harness

    def setUp(self) -> None:
        self.events = []
        self.http = _FakeHttp(self.events)
        self.account = _FakeAccount(self.events)
        self.log = _FakeLog()
        self.clock = _FakeClock()
        self.now = 1000.0
        timer = patch.object(self.module, "time", SimpleNamespace(monotonic=lambda: self.now))
        timer.start()
        self.addCleanup(timer.stop)

        async def submitted(client, order, position_side, params=None):
            self.events.append(("submitted", order, position_side, params))

        submit = patch.object(
            self.module.BinanceCommonExecutionClient, "_submit_order_inner", submitted,
        )
        submit.start()
        self.addCleanup(submit.stop)

    def make_client(self, minimum="50", current=5, cap=150, account_type=None):
        module = self.module
        if account_type is None:
            account_type = module.BinanceAccountType.USDT_FUTURES

        def common_init(instance, **kwargs):
            instance._log = self.log
            instance._clock = self.clock
            instance._http_client = self.http
            instance._recv_window = 30_000
            instance._binance_account_type = kwargs["account_type"]

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(module.BinanceCommonExecutionClient, "__init__", common_init),
            patch.object(module, "BinanceFuturesAccountHttpAPI", return_value=self.http),
            patch.object(module, "BinanceFuturesMarketHttpAPI"),
        ):
            if minimum is not None:
                os.environ["BINANCE_MIN_OPEN_LEVERAGE"] = minimum
            client = self.Harness(
                loop=None, client=self.http, msgbus=None, cache=None, clock=self.clock,
                instrument_provider=None, base_url_ws="", account_type=account_type,
                config=SimpleNamespace(
                    use_trade_lite=False, futures_leverages=None, futures_margin_types=None,
                ),
                environment=module.BinanceEnvironment.LIVE, api_key="", api_secret="",
            )
        client.fake_account = self.account
        client.instruments = {}
        client.fake_cache = SimpleNamespace(
            instrument=lambda instrument_id: (
                instrument_id if instrument_id in client.instruments.values() else None
            ),
        )
        client.position_side = module.BinanceFuturesPositionSide.LONG
        self.add_instrument(client, "BTCUSDT", current, cap)
        return client

    def add_instrument(self, client, symbol, current=5, cap=150):
        instrument_id = self.module.InstrumentId.from_str(f"{symbol}-PERP.BINANCE")
        client.instruments[symbol] = instrument_id
        self.account.values[instrument_id] = Decimal(current) if current is not None else None
        self.http.caps[symbol] = cap
        return instrument_id

    def order(self, client, symbol="BTCUSDT", side=None, order_type=None, reduce_only=False):
        module = self.module
        return SimpleNamespace(
            instrument_id=client.instruments[symbol],
            side=module.OrderSide.BUY if side is None else side,
            order_type=module.OrderType.MARKET if order_type is None else order_type,
            is_reduce_only=reduce_only, is_closed=False, exec_spawn_id=None,
            linked_order_ids=[],
        )

    async def submit(self, client, order=None, position_side="default"):
        if order is None:
            order = self.order(client)
        if position_side == "default":
            position_side = self.module.BinanceFuturesPositionSide.LONG
        await client._submit_order_inner(order, position_side, {"sentinel": True})

    def assert_no_http(self) -> None:
        self.assertEqual(self.http.bracket_calls, [])
        self.assertEqual(self.http.set_calls, [])

    async def test_missing_empty_and_invalid_environment_disable_all_order_checks(self) -> None:
        for value in (None, "", " ", "no", "1.5", "0", "-2", "1_0"):
            with self.subTest(value=value):
                client = self.make_client(minimum=value)
                for side in self.module.OrderSide.BUY, self.module.OrderSide.SELL:
                    for kind in self.module.OrderType:
                        for position in [None, *self.module.BinanceFuturesPositionSide]:
                            await self.submit(client, self.order(client, side=side, order_type=kind), position)
                self.assertIsNone(client._min_open_leverage)
                self.assert_no_http()
        self.assertEqual(len(self.log.errors), 5)

    async def test_configuration_is_read_once(self) -> None:
        client = self.make_client(minimum="50")
        with patch.dict(os.environ, {"BINANCE_MIN_OPEN_LEVERAGE": "80"}):
            await self.submit(client)
        self.assertEqual(self.http.set_calls, [("BTCUSDT", 50, "30000")])

    async def test_increase_to_minimum_and_cache_success_before_submission(self) -> None:
        client = self.make_client()
        await self.submit(client)
        await self.submit(client)
        self.assertEqual(self.http.set_calls, [("BTCUSDT", 50, "30000")])
        self.assertEqual(len(self.http.bracket_calls), 1)
        self.assertEqual(self.account.leverage(client.instruments["BTCUSDT"]), Decimal(50))
        self.assertEqual([event[0] for event in self.events], ["exchange", "cache", "submitted", "submitted"])
        request = self.http.bracket_calls[0]
        self.assertEqual(request["http_method"], self.module.HttpMethod.GET)
        self.assertEqual(request["url_path"], "/fapi/v1/leverageBracket")
        self.assertEqual(request["payload"], {
            "symbol": "BTCUSDT", "recvWindow": "30000", "timestamp": str(self.clock.timestamp_ms()),
        })
        self.assertTrue(any("BTCUSDT" in message for message in self.log.infos))

    async def test_cap_below_minimum_is_cached_and_not_requested_again(self) -> None:
        client = self.make_client(cap=20)
        await self.submit(client)
        await self.submit(client)
        self.assertEqual(self.http.set_calls, [("BTCUSDT", 20, "30000")])
        self.assertEqual(len(self.http.bracket_calls), 1)

    async def test_already_above_minimum_does_not_query_bracket(self) -> None:
        client = self.make_client(current=75)
        await self.submit(client)
        self.assert_no_http()

    async def test_current_above_cap_is_never_lowered(self) -> None:
        client = self.make_client(current=25, cap=20)
        await self.submit(client)
        await self.submit(client)
        self.assertEqual(len(self.http.bracket_calls), 1)
        self.assertEqual(self.http.set_calls, [])
        self.assertEqual(self.account.leverage(client.instruments["BTCUSDT"]), Decimal(25))

    async def test_all_closing_sides_reduce_only_and_algo_types_skip_http(self) -> None:
        client = self.make_client()
        module = self.module
        for side, position in (
            (module.OrderSide.SELL, module.BinanceFuturesPositionSide.LONG),
            (module.OrderSide.BUY, module.BinanceFuturesPositionSide.SHORT),
        ):
            await self.submit(client, self.order(client, side=side), position)
        for position in (None, module.BinanceFuturesPositionSide.BOTH):
            for side in (module.OrderSide.BUY, module.OrderSide.SELL):
                await self.submit(client, self.order(client, side=side, reduce_only=True), position)
        for kind in module.BINANCE_FUTURES_ALGO_ORDER_TYPES:
            for side, position in (
                (module.OrderSide.BUY, module.BinanceFuturesPositionSide.LONG),
                (module.OrderSide.SELL, module.BinanceFuturesPositionSide.SHORT),
                (module.OrderSide.BUY, None),
            ):
                await self.submit(client, self.order(client, side=side, order_type=kind), position)
        self.assert_no_http()
        self.assertTrue(self.events)

    async def test_short_and_one_way_opening_orders_increase_leverage(self) -> None:
        module = self.module
        for side, position in (
            (module.OrderSide.SELL, module.BinanceFuturesPositionSide.SHORT),
            (module.OrderSide.BUY, None),
            (module.OrderSide.SELL, module.BinanceFuturesPositionSide.BOTH),
        ):
            client = self.make_client()
            self.http.set_calls.clear()
            await self.submit(client, self.order(client, side=side), position)
            self.assertEqual(self.http.set_calls, [("BTCUSDT", 50, "30000")])

    async def test_coin_m_and_closed_orders_do_not_enable_feature(self) -> None:
        client = self.make_client(account_type=self.module.BinanceAccountType.COIN_FUTURES)
        await self.submit(client)
        client = self.make_client()
        order = self.order(client)
        order.is_closed = True
        await self.submit(client, order)
        self.assert_no_http()

    async def test_set_failure_submits_and_cools_down_for_five_minutes(self) -> None:
        client = self.make_client()
        self.http.set_error = RuntimeError("set unavailable")
        await self.submit(client)
        self.now += 299
        await self.submit(client)
        self.assertEqual(len(self.http.set_calls), 1)
        self.assertEqual(len([e for e in self.events if e[0] == "submitted"]), 2)
        self.assertEqual(self.account.leverage(client.instruments["BTCUSDT"]), Decimal(5))
        self.assertEqual(len(self.log.warnings), 1)
        for text in ("BTCUSDT", "current=5X", "target=50X", "set unavailable"):
            self.assertIn(text, self.log.warnings[0])
        self.now += 1
        self.http.set_error = None
        await self.submit(client)
        self.assertEqual(len(self.http.set_calls), 2)
        self.assertEqual(len(self.http.bracket_calls), 1)

    async def test_bracket_failure_submits_and_retries_after_cooldown(self) -> None:
        client = self.make_client()
        self.http.bracket_error = RuntimeError("bracket unavailable")
        await self.submit(client)
        self.now += 299
        await self.submit(client)
        self.assertEqual(len(self.http.bracket_calls), 1)
        self.assertEqual(self.http.set_calls, [])
        self.assertEqual(len([e for e in self.events if e[0] == "submitted"]), 2)
        for text in ("BTCUSDT", "current=5X", "target=50X", "bracket unavailable"):
            self.assertIn(text, self.log.warnings[0])
        self.now += 1
        self.http.bracket_error = None
        await self.submit(client)
        self.assertEqual(len(self.http.bracket_calls), 2)
        self.assertEqual(len(self.http.set_calls), 1)

    async def test_malformed_or_invalid_bracket_fails_open_and_cools_down(self) -> None:
        for response in (
            b'not json', b'[]', b'[{"symbol":"BTCUSDT","brackets":[]}]',
            b'[{"symbol":"BTCUSDT","brackets":[{"initialLeverage":0}]}]',
        ):
            with self.subTest(response=response):
                client = self.make_client()
                self.http.bracket_calls.clear()
                self.http.bracket_response = response
                before = len([e for e in self.events if e[0] == "submitted"])
                await self.submit(client)
                await self.submit(client)
                self.assertEqual(len(self.http.bracket_calls), 1)
                self.assertEqual(self.http.set_calls, [])
                self.assertEqual(len([e for e in self.events if e[0] == "submitted"]), before + 2)

    async def test_selects_requested_symbol_in_bracket_response(self) -> None:
        client = self.make_client()
        self.http.bracket_response = (
            b'[{"symbol":"ETHUSDT","brackets":[{"initialLeverage":150}]},'
            b'{"symbol":"BTCUSDT","brackets":[{"initialLeverage":20}]}]'
        )
        await self.submit(client)
        self.assertEqual(self.http.set_calls, [("BTCUSDT", 20, "30000")])

    async def test_cooldown_starts_when_failed_request_finishes(self) -> None:
        client = self.make_client()

        async def delayed_failure(symbol):
            self.now += 60

        self.http.before_bracket = delayed_failure
        self.http.bracket_error = RuntimeError("failed after waiting")
        await self.submit(client)
        self.now += 299
        await self.submit(client)
        self.assertEqual(len(self.http.bracket_calls), 1)
        self.assertEqual(client._leverage_retry_after["BTCUSDT"], 1360)

    async def test_missing_cached_leverage_fails_open(self) -> None:
        client = self.make_client(current=None)
        await self.submit(client)
        self.assert_no_http()
        self.assertEqual(len(self.log.warnings), 1)
        self.assertEqual(self.events[0][0], "submitted")

    async def test_concurrent_same_symbol_only_changes_once(self) -> None:
        client = self.make_client()
        await asyncio.gather(self.submit(client), self.submit(client))
        self.assertEqual(len(self.http.bracket_calls), 1)
        self.assertEqual(len(self.http.set_calls), 1)
        self.assertEqual(len([e for e in self.events if e[0] == "submitted"]), 2)

    async def test_concurrent_failure_only_requests_once(self) -> None:
        client = self.make_client()
        self.http.set_error = RuntimeError("failed")
        await asyncio.gather(self.submit(client), self.submit(client))
        self.assertEqual(len(self.http.set_calls), 1)
        self.assertEqual(len(self.log.warnings), 1)
        self.assertEqual(len([e for e in self.events if e[0] == "submitted"]), 2)

    async def test_different_symbols_do_not_share_lock(self) -> None:
        client = self.make_client()
        self.add_instrument(client, "ETHUSDT")
        started = asyncio.Event()
        release = asyncio.Event()

        async def block_btc(symbol):
            if symbol == "BTCUSDT":
                started.set()
                await release.wait()

        self.http.before_bracket = block_btc
        btc = asyncio.create_task(self.submit(client))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.wait_for(self.submit(client, self.order(client, "ETHUSDT")), timeout=1)
            self.assertFalse(btc.done())
            self.assertEqual(self.http.set_calls, [("ETHUSDT", 50, "30000")])
        finally:
            release.set()
            await btc

    async def test_single_and_list_entry_points_invoke_hook(self) -> None:
        client = self.make_client()
        await client._submit_order(SimpleNamespace(
            order=self.order(client), position_id=None, params={"single": True},
        ))
        self.account.values[client.instruments["BTCUSDT"]] = Decimal(5)
        await client._submit_order_list(SimpleNamespace(
            order_list=SimpleNamespace(orders=[self.order(client), self.order(client)]),
            position_id=None, params={"list": True},
        ))
        self.assertEqual(len(self.http.set_calls), 2)
        submissions = [e for e in self.events if e[0] == "submitted"]
        self.assertEqual([e[3] for e in submissions], [{"single": True}, {"list": True}, {"list": True}])

    async def test_account_config_update_refreshes_external_leverage_and_ignores_unknown(self) -> None:
        client = self.make_client()
        client._handle_account_config_update(
            b'{"e":"ACCOUNT_CONFIG_UPDATE","E":1,"T":1,"ac":{"s":"BTCUSDT","l":75}}',
        )
        self.assertEqual(self.account.leverage(client.instruments["BTCUSDT"]), Decimal(75))
        await self.submit(client)
        self.assert_no_http()
        client._handle_account_config_update(b'{"ac":{"s":"UNKNOWN","l":25}}')
        client._handle_account_config_update(b'{"ai":{"j":true}}')
        self.assertEqual(len([e for e in self.events if e[0] == "cache"]), 1)

    async def test_external_increase_during_bracket_query_is_not_lowered(self) -> None:
        client = self.make_client()

        async def external_increase(symbol):
            client._handle_account_config_update(b'{"ac":{"s":"BTCUSDT","l":75}}')

        self.http.before_bracket = external_increase
        await self.submit(client)
        self.assertEqual(self.http.set_calls, [])
        self.assertEqual(self.account.leverage(client.instruments["BTCUSDT"]), Decimal(75))

    async def test_external_reduction_after_success_raises_again_using_cached_cap(self) -> None:
        client = self.make_client(cap=20)
        await self.submit(client)
        client._handle_account_config_update(b'{"ac":{"s":"BTCUSDT","l":5}}')
        await self.submit(client)
        self.assertEqual(self.http.set_calls, [("BTCUSDT", 20, "30000"), ("BTCUSDT", 20, "30000")])
        self.assertEqual(len(self.http.bracket_calls), 1)


if __name__ == "__main__":
    unittest.main()
