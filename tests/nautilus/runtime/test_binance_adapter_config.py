from __future__ import annotations

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
FUTURES_PATCH_PATH = REPO_ROOT / "container-patches" / "binance_futures_execution.py"
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
    ) -> None:
        self.kwargs = {
            "api_key": api_key,
            "api_secret": api_secret,
            "account_type": account_type,
            "environment": environment,
            "instrument_provider": instrument_provider,
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
        use_reduce_only: bool,
        recv_window_ms: int,
    ) -> None:
        super().__init__(
            api_key=api_key,
            api_secret=api_secret,
            account_type=account_type,
            environment=environment,
            instrument_provider=instrument_provider,
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
                    credentials=SimpleNamespace(
                        api_key="api-key",
                        api_secret="api-secret",
                    ),
                )
            )

            _data_config, exec_config = module.build_binance_client_configs(config)

        self.assertEqual(exec_config.kwargs["recv_window_ms"], 30_000)

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
