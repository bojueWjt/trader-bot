from __future__ import annotations

import importlib.util
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
sys.path.insert(0, str(SERVICE_ROOT))


class _CapturedConfig:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


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
    config.BinanceDataClientConfig = _CapturedConfig
    config.BinanceExecClientConfig = _CapturedConfig
    config.BinanceInstrumentProviderConfig = _CapturedConfig
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
