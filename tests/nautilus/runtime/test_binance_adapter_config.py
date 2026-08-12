from __future__ import annotations

import ast
from collections import Counter
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
MODULE_PATH = (
    SERVICE_ROOT / "runtime" / "binance_adapter_config.py"
)
COMMON_PATCH_PATH = REPO_ROOT / "container-patches" / "binance_execution.py"
FUTURES_PATCH_PATH = REPO_ROOT / "container-patches" / "binance_futures_execution.py"
EXPECTED_SIGNED_ACCOUNT_CALLS = Counter(
    {
        "binance_execution.py:_http_account.cancel_algo_order": 1,
        "binance_execution.py:_http_account.cancel_all_open_algo_orders": 1,
        "binance_execution.py:_http_account.cancel_all_open_orders": 1,
        "binance_execution.py:_http_account.cancel_order": 1,
        "binance_execution.py:_http_account.modify_order": 1,
        "binance_execution.py:_http_account.new_algo_order": 4,
        "binance_execution.py:_http_account.new_order": 4,
        "binance_execution.py:_http_account.query_all_orders": 1,
        "binance_execution.py:_http_account.query_open_orders": 1,
        "binance_execution.py:_http_account.query_order": 2,
        "binance_execution.py:_http_account.query_user_trades": 1,
        "binance_futures_execution.py:_futures_http_account.cancel_multiple_orders": 1,
        "binance_futures_execution.py:_futures_http_account.query_algo_order": 1,
        "binance_futures_execution.py:_futures_http_account.query_all_algo_orders": 1,
        "binance_futures_execution.py:_futures_http_account.query_futures_account_info": 1,
        "binance_futures_execution.py:_futures_http_account.query_futures_hedge_mode": 1,
        "binance_futures_execution.py:_futures_http_account.query_futures_position_risk": 2,
        "binance_futures_execution.py:_futures_http_account.query_futures_symbol_config": 1,
        "binance_futures_execution.py:_futures_http_account.query_open_algo_orders": 1,
        "binance_futures_execution.py:_futures_http_account.set_leverage": 1,
        "binance_futures_execution.py:_futures_http_account.set_margin_type": 1,
    }
)
sys.path.insert(0, str(SERVICE_ROOT))


class _CapturedDataConfig:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        account_type: object,
        environment: object,
        instrument_provider: object,
        proxy_url: str | None,
    ) -> None:
        self.kwargs = {
            "api_key": api_key,
            "api_secret": api_secret,
            "account_type": account_type,
            "environment": environment,
            "instrument_provider": instrument_provider,
            "proxy_url": proxy_url,
        }


class _CapturedExecConfig(_CapturedDataConfig):
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        account_type: object,
        environment: object,
        instrument_provider: object,
        proxy_url: str | None,
        use_reduce_only: bool,
        recv_window_ms: int,
    ) -> None:
        super().__init__(
            api_key=api_key,
            api_secret=api_secret,
            account_type=account_type,
            environment=environment,
            instrument_provider=instrument_provider,
            proxy_url=proxy_url,
        )
        self.kwargs["use_reduce_only"] = use_reduce_only
        self.kwargs["recv_window_ms"] = recv_window_ms


class _CapturedProviderConfig:
    def __init__(self, *, load_all: bool) -> None:
        self.kwargs = {"load_all": load_all}


class _BinanceAccountType:
    USDT_FUTURES = "USDT_FUTURES"


class _BinanceEnvironment:
    LIVE = "LIVE"
    TESTNET = "TESTNET"


class BinanceAdapterConfigTest(unittest.TestCase):
    def test_exec_client_uses_extended_recv_window(self) -> None:
        modules = _fake_nautilus_modules()
        with patch.dict(sys.modules, modules):
            module = _load_module()
            config = SimpleNamespace(
                binance=SimpleNamespace(
                    environment="live",
                    proxy_url="http://100.107.72.78:13128",
                    credentials=SimpleNamespace(
                        api_key="api-key",
                        api_secret="api-secret",
                    ),
                )
            )

            data_config, exec_config = module.build_binance_client_configs(config)

        self.assertEqual(exec_config.kwargs["recv_window_ms"], 30_000)
        self.assertEqual(
            data_config.kwargs["proxy_url"],
            "http://100.107.72.78:13128",
        )
        self.assertEqual(
            exec_config.kwargs["proxy_url"],
            "http://100.107.72.78:13128",
        )

    def test_futures_account_initialization_uses_configured_recv_window(self) -> None:
        source = FUTURES_PATCH_PATH.read_text(encoding="utf-8")

        self.assertRegex(
            source,
            re.compile(
                r"query_futures_account_info\(\s*"
                r"recv_window=str\(self\._recv_window\)\s*\)"
            ),
        )
        self.assertNotIn(
            "query_futures_account_info(recv_window=str(5000))",
            source,
        )

    def test_all_signed_account_calls_use_configured_recv_window(self) -> None:
        calls: Counter[str] = Counter()
        missing: list[str] = []
        for patch_path in (COMMON_PATCH_PATH, FUTURES_PATCH_PATH):
            patch_calls, patch_missing = _signed_account_calls(patch_path)
            calls.update(patch_calls)
            missing.extend(patch_missing)

        self.assertEqual(calls, EXPECTED_SIGNED_ACCOUNT_CALLS)
        self.assertEqual(missing, [])


def _fake_nautilus_modules() -> dict[str, types.ModuleType]:
    modules: dict[str, types.ModuleType] = {}
    for name in (
        "nautilus_trader",
        "nautilus_trader.adapters",
        "nautilus_trader.adapters.binance",
        "nautilus_trader.adapters.binance.common",
        "nautilus_trader.adapters.binance.common.enums",
        "nautilus_trader.adapters.binance.config",
    ):
        modules[name] = types.ModuleType(name)
    enums = modules["nautilus_trader.adapters.binance.common.enums"]
    enums.BinanceAccountType = _BinanceAccountType
    enums.BinanceEnvironment = _BinanceEnvironment
    config = modules["nautilus_trader.adapters.binance.config"]
    config.BinanceDataClientConfig = _CapturedDataConfig
    config.BinanceExecClientConfig = _CapturedExecConfig
    config.BinanceInstrumentProviderConfig = _CapturedProviderConfig
    return modules


def _signed_account_calls(path: Path) -> tuple[Counter[str], list[str]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    calls: Counter[str] = Counter()
    missing: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        direct_method = _account_method_name(node.func)
        if direct_method:
            label = f"{path.name}:{direct_method}"
            calls[label] += 1
            if not _has_configured_recv_window(node):
                missing.append(f"{label}:{node.lineno}")
            continue

        for argument in node.args:
            indirect_method = _account_method_name(argument)
            if not indirect_method:
                continue
            label = f"{path.name}:{indirect_method}"
            calls[label] += 1
            if not _has_configured_recv_window(node):
                missing.append(f"{label}:{node.lineno}")

    return calls, missing


def _account_method_name(node: ast.AST) -> str | bool:
    if not isinstance(node, ast.Attribute):
        return False
    owner = node.value
    if not isinstance(owner, ast.Attribute):
        return False
    root = owner.value
    if not isinstance(root, ast.Name):
        return False
    if root.id != "self":
        return False
    if owner.attr not in {"_http_account", "_futures_http_account"}:
        return False
    return f"{owner.attr}.{node.attr}"


def _has_configured_recv_window(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if keyword.arg != "recv_window":
            continue
        return _is_configured_recv_window(keyword.value)
    return False


def _is_configured_recv_window(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if not isinstance(node.func, ast.Name):
        return False
    if node.func.id != "str":
        return False
    if len(node.args) != 1:
        return False
    argument = node.args[0]
    if not isinstance(argument, ast.Attribute):
        return False
    if not isinstance(argument.value, ast.Name):
        return False
    return argument.value.id == "self" and argument.attr == "_recv_window"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_binance_adapter_config_under_test",
        MODULE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Binance adapter config: {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
