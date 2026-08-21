from __future__ import annotations

from typing import Any

from config.node_config import NodeConfig
from risk.config import DEFAULT_LIVE_ENTRY_NOTIONAL_KEY


BINANCE_RECV_WINDOW_MS = 30_000


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
    from nautilus_trader.model.identifiers import (  # type: ignore[import-not-found]
        InstrumentId,
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
    owned_instrument_ids = _owned_instrument_ids(config)
    if _has_default_notional_cap(config):
        # The "*" default cap admits instruments beyond the explicit
        # inventory, so their definitions must be loadable too.
        instrument_provider = BinanceInstrumentProviderConfig(load_all=True)
    elif owned_instrument_ids:
        instrument_provider = BinanceInstrumentProviderConfig(
            load_all=False,
            load_ids=frozenset(
                InstrumentId.from_str(value)
                for value in owned_instrument_ids
            ),
        )
    else:
        if env_name == "live":
            raise RuntimeError(
                "live Binance instrument provider requires owned instruments"
            )
        instrument_provider = BinanceInstrumentProviderConfig(load_all=True)
    proxy_url = config.binance.proxy_url
    if proxy_url is False:
        proxy_url = None

    data_config = BinanceDataClientConfig(
        api_key=config.binance.credentials.api_key,
        api_secret=config.binance.credentials.api_secret,
        account_type=account_type,
        environment=environment,
        instrument_provider=instrument_provider,
        proxy_url=proxy_url,
    )
    exec_config = BinanceExecClientConfig(
        api_key=config.binance.credentials.api_key,
        api_secret=config.binance.credentials.api_secret,
        account_type=account_type,
        environment=environment,
        instrument_provider=instrument_provider,
        proxy_url=proxy_url,
        # The live Binance accounts run in Hedge Mode, where Binance/Nautilus reject
        # reduce_only (positionSide is used instead). Testnet runs one-way — the tested
        # path — where reduce_only is valid, so keep it there unchanged.
        use_reduce_only=(env_name != "live"),
        recv_window_ms=BINANCE_RECV_WINDOW_MS,
    )
    return data_config, exec_config


def _enum_value(enum_type: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        if hasattr(enum_type, name):
            return getattr(enum_type, name)
    raise RuntimeError(
        f"unsupported Nautilus enum {enum_type!r}; tried {', '.join(names)}"
    )


def _owned_instrument_ids(config: NodeConfig) -> tuple[str, ...]:
    risk = getattr(config, "risk", None)
    if risk is None:
        return ()
    raw_limits = getattr(risk, "max_notional_per_order", None)
    if not raw_limits:
        return ()
    return tuple(
        sorted(
            {
                str(instrument_id).strip()
                for instrument_id in raw_limits
                if str(instrument_id).strip()
                and str(instrument_id).strip()
                != DEFAULT_LIVE_ENTRY_NOTIONAL_KEY
            }
        )
    )


def _has_default_notional_cap(config: NodeConfig) -> bool:
    risk = getattr(config, "risk", None)
    if risk is None:
        return False
    raw_limits = getattr(risk, "max_notional_per_order", None)
    if not raw_limits:
        return False
    return any(
        str(instrument_id).strip() == DEFAULT_LIVE_ENTRY_NOTIONAL_KEY
        for instrument_id in raw_limits
    )
