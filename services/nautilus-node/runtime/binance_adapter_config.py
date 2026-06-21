from __future__ import annotations

from typing import Any

from config.node_config import NodeConfig


def build_binance_client_configs(config: NodeConfig) -> tuple[Any, Any]:
    """Build Nautilus Binance data/exec client configs.

    Host-only: pudu-mini cannot import ``nautilus_trader==1.227.0``. Verified in the
    hk Python 3.12 container (2026-06-19): the example account config resolves to
    ``BinanceAccountType.USDT_FUTURES`` and ``BinanceEnvironment.TESTNET`` (default-safe),
    producing real ``BinanceDataClientConfig`` / ``BinanceExecClientConfig`` objects.
    """

    from nautilus_trader.adapters.binance.common.enums import (  # type: ignore[import-not-found]
        BinanceAccountType,
        BinanceEnvironment,
    )
    from nautilus_trader.adapters.binance.config import (  # type: ignore[import-not-found]
        BinanceDataClientConfig,
        BinanceExecClientConfig,
        BinanceInstrumentProviderConfig,
    )

    account_type = _enum_value(BinanceAccountType, ("USDT_FUTURES", "USDT_M_FUTURES"))
    # Honor the validated config.binance.environment. node_config restricts it to
    # {sandbox, testnet, live}; "live" -> real money (BinanceEnvironment.LIVE),
    # testnet/sandbox -> TESTNET. Anything unexpected is default-safe to TESTNET so a
    # missing/garbled value can never silently route to mainnet.
    env_name = (getattr(config.binance, "environment", "") or "").strip().lower()
    env_candidates = {
        "live": ("LIVE",),
        "testnet": ("TESTNET", "SANDBOX", "DEMO"),
        "sandbox": ("TESTNET", "SANDBOX", "DEMO"),
    }.get(env_name, ("TESTNET", "SANDBOX", "DEMO"))
    environment = _enum_value(BinanceEnvironment, env_candidates)
    instrument_provider = BinanceInstrumentProviderConfig(load_all=True)

    data_config = BinanceDataClientConfig(
        api_key=config.binance.credentials.api_key,
        api_secret=config.binance.credentials.api_secret,
        account_type=account_type,
        environment=environment,
        instrument_provider=instrument_provider,
    )
    exec_config = BinanceExecClientConfig(
        api_key=config.binance.credentials.api_key,
        api_secret=config.binance.credentials.api_secret,
        account_type=account_type,
        environment=environment,
        instrument_provider=instrument_provider,
    )
    return data_config, exec_config


def _enum_value(enum_type: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        if hasattr(enum_type, name):
            return getattr(enum_type, name)
    raise RuntimeError(
        f"unsupported Nautilus enum {enum_type!r}; tried {', '.join(names)}"
    )
