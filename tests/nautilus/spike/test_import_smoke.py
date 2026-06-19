from __future__ import annotations

import importlib

import pytest


EXPECTED_NAUTILUS_VERSION = "1.227.0"


nautilus_trader = pytest.importorskip("nautilus_trader")


def test_nautilus_trader_version_is_pinned() -> None:
    assert getattr(nautilus_trader, "__version__", None) == EXPECTED_NAUTILUS_VERSION


@pytest.mark.parametrize(
    ("module_name", "symbol_name"),
    [
        ("nautilus_trader", "__version__"),
        ("nautilus_trader.adapters.binance", "BINANCE"),
        ("nautilus_trader.adapters.binance", "BINANCE_USDM"),
        ("nautilus_trader.adapters.binance.config", "BinanceDataClientConfig"),
        ("nautilus_trader.adapters.binance.config", "BinanceExecClientConfig"),
        ("nautilus_trader.live.node", "TradingNode"),
        ("nautilus_trader.model.enums", "OrderType"),
        ("nautilus_trader.model.enums", "TimeInForce"),
    ],
)
def test_key_nautilus_symbols_import(module_name: str, symbol_name: str) -> None:
    module = importlib.import_module(module_name)
    assert hasattr(module, symbol_name)
