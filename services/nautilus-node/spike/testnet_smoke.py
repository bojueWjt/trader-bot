#!/usr/bin/env python3
from __future__ import annotations

import importlib
import inspect
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Any


TESTNET_PRICE_URL = "https://testnet.binancefuture.com/fapi/v1/ticker/price?symbol={symbol}"


@dataclass
class Step:
    name: str
    status: str
    detail: str


@dataclass
class SmokeResult:
    steps: list[Step] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.steps.append(Step(name=name, status=status, detail=detail))
        print(f"{name}: {status}: {detail}")

    def ok(self) -> bool:
        required = {"credentials", "version", "testnet_guard", "adapter_import", "config_introspection"}
        passed = {step.name for step in self.steps if step.status == "OK"}
        return required.issubset(passed) and any(
            step.name in {"order_cancel", "node_order_attempt"} and step.status == "OK"
            for step in self.steps
        )


def import_symbol(module_name: str, symbol_name: str) -> Any:
    module = importlib.import_module(module_name)
    return getattr(module, symbol_name)


def constructor_fields(cls: type[Any]) -> set[str]:
    fields: set[str] = set()
    for attr_name in ("model_fields", "__fields__", "__annotations__"):
        value = getattr(cls, attr_name, None)
        if isinstance(value, dict):
            fields.update(str(name) for name in value)
    try:
        signature = inspect.signature(cls)
    except Exception:
        return fields
    for name, parameter in signature.parameters.items():
        if name not in {"self", "args", "kwargs"} and parameter.kind.name != "VAR_POSITIONAL":
            fields.add(name)
    return fields


def first_enum_value(module_name: str, enum_name: str, contains: tuple[str, ...]) -> Any | None:
    try:
        enum_cls = import_symbol(module_name, enum_name)
    except Exception:
        return None
    for member in enum_cls:
        name = getattr(member, "name", str(member)).upper()
        if all(part in name for part in contains):
            return member
    return None


def instantiate_config(cls: type[Any], preferred: dict[str, Any]) -> Any:
    fields = constructor_fields(cls)
    kwargs = {name: value for name, value in preferred.items() if name in fields}
    return cls(**kwargs)


def public_testnet_price(symbol: str) -> Decimal:
    with urllib.request.urlopen(TESTNET_PRICE_URL.format(symbol=symbol), timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return Decimal(str(payload["price"]))


def quantize_price(price: Decimal, decimals: str = "0.1") -> Decimal:
    return price.quantize(Decimal(decimals), rounding=ROUND_DOWN)


def guarded_env(result: SmokeResult) -> tuple[str, str] | None:
    key = os.environ.get("BINANCE_TESTNET_API_KEY")
    secret = os.environ.get("BINANCE_TESTNET_API_SECRET")
    if not key or not secret:
        print("SKIPPED: no testnet credentials")
        return None
    result.add("credentials", "OK", "BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET are present")
    result.add("testnet_guard", "OK", "script is hard-wired to Binance USDT-M Futures testnet endpoints only")
    return key, secret


def try_nautilus_config(result: SmokeResult, key: str, secret: str) -> dict[str, Any]:
    import nautilus_trader

    result.add("version", "OK", f"nautilus_trader={getattr(nautilus_trader, '__version__', 'unknown')}")
    if getattr(nautilus_trader, "__version__", None) != "1.227.0":
        result.add("version_pin", "FAIL", "expected nautilus_trader==1.227.0")
        return {}

    config_module = "nautilus_trader.adapters.binance.config"
    data_config_cls = import_symbol(config_module, "BinanceDataClientConfig")
    exec_config_cls = import_symbol(config_module, "BinanceExecClientConfig")
    result.add("adapter_import", "OK", f"{config_module} config classes imported")

    account_type = first_enum_value(
        "nautilus_trader.adapters.binance.common.enums",
        "BinanceAccountType",
        ("USDT", "FUTURE"),
    ) or first_enum_value(
        "nautilus_trader.adapters.binance.common.enums",
        "BinanceAccountType",
        ("FUTURE",),
    )
    environment = first_enum_value(
        "nautilus_trader.adapters.binance.common.enums",
        "BinanceEnvironment",
        ("TESTNET",),
    )

    preferred = {
        "api_key": key,
        "api_secret": secret,
        "account_type": account_type,
        "environment": environment,
        "testnet": True,
        "sandbox": True,
        "use_testnet": True,
        "base_url_http": "https://testnet.binancefuture.com",
        "base_url_ws": "wss://stream.binancefuture.com",
    }
    data_config = instantiate_config(data_config_cls, preferred)
    exec_config = instantiate_config(exec_config_cls, preferred)
    result.add(
        "config_introspection",
        "OK",
        "instantiated Binance data/exec config using fields exposed by constructors",
    )
    return {
        "data_config": data_config,
        "exec_config": exec_config,
        "account_type": account_type,
        "environment": environment,
    }


def try_native_smoke(result: SmokeResult, configs: dict[str, Any]) -> None:
    symbol = os.environ.get("BINANCE_TESTNET_SYMBOL", "BTCUSDT")
    qty = Decimal(os.environ.get("BINANCE_TESTNET_QTY", "0.001"))
    try:
        mark = public_testnet_price(symbol)
        limit_price = quantize_price(mark * Decimal("0.80"))
        result.add("public_testnet_price", "OK", f"{symbol} mark={mark}, buy_limit={limit_price}, qty={qty}")
    except Exception as exc:
        result.add("public_testnet_price", "FAIL", f"{type(exc).__name__}: {exc}")
        return

    result.add(
        "node_order_attempt",
        "UNKNOWN",
        (
            "Nautilus TradingNode order placement is intentionally not guessed in this script. "
            "Use the config objects printed here plus capability_matrix.py output to fill the "
            "exact 1.227.0 Strategy/TradingNode order path on the target host, then rerun."
        ),
    )
    print(f"data_config_type={type(configs.get('data_config')).__module__}.{type(configs.get('data_config')).__name__}")
    print(f"exec_config_type={type(configs.get('exec_config')).__module__}.{type(configs.get('exec_config')).__name__}")


def main() -> int:
    result = SmokeResult()
    credentials = guarded_env(result)
    if credentials is None:
        return 0

    key, secret = credentials
    try:
        configs = try_nautilus_config(result, key, secret)
    except Exception as exc:
        result.add("config_introspection", "FAIL", f"{type(exc).__name__}: {exc}")
        return 1

    try_native_smoke(result, configs)
    print("testnet_smoke_summary:")
    for step in result.steps:
        print(f"- {step.name}: {step.status}: {step.detail}")

    if result.ok():
        return 0

    result.add(
        "order_cancel",
        "PENDING",
        "Limit order -> cancel and conditional order smoke still require target-host Nautilus API completion.",
    )
    time.sleep(0.1)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
