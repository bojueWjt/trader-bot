from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("nautilus_trader")
pd = pytest.importorskip("pandas")

from nautilus_trader.core.uuid import UUID4  # noqa: E402
from nautilus_trader.execution.messages import GenerateFillReports  # noqa: E402
from nautilus_trader.model.identifiers import AccountId  # noqa: E402
from nautilus_trader.model.identifiers import ClientId  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId  # noqa: E402
from nautilus_trader.model.identifiers import Venue  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
MODULE_PATH = (
    SERVICE_ROOT / "runtime" / "nautilus_reconciliation_scope.py"
)
SPEC = importlib.util.spec_from_file_location(
    "nautilus_reconciliation_scope",
    MODULE_PATH,
)
assert SPEC is not None
assert SPEC.loader is not None
scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope)
BINANCE_PATCH_PATH = REPO_ROOT / "container-patches" / "binance_execution.py"
BINANCE_PATCH_SPEC = importlib.util.spec_from_file_location(
    "scoped_binance_execution",
    BINANCE_PATCH_PATH,
)
assert BINANCE_PATCH_SPEC is not None
assert BINANCE_PATCH_SPEC.loader is not None
binance_patch = importlib.util.module_from_spec(BINANCE_PATCH_SPEC)
BINANCE_PATCH_SPEC.loader.exec_module(binance_patch)


OWNED_INSTRUMENTS = (
    InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
    InstrumentId.from_str("ETHUSDT-PERP.BINANCE"),
)


class _Clock:
    def timestamp_ns(self) -> int:
        return 1_777_777_777_000_000_000

    def utc_now(self) -> pd.Timestamp:
        return pd.Timestamp("2026-08-20T00:00:00Z")


class _Log:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return

    def error(self, *_args: object, **_kwargs: object) -> None:
        return

    def exception(self, *_args: object, **_kwargs: object) -> None:
        return

    def info(self, *_args: object, **_kwargs: object) -> None:
        return


class _Client:
    def __init__(self) -> None:
        self.id = ClientId("BINANCE")
        self.account_id = AccountId("BINANCE-001")
        self.venue = Venue("BINANCE")
        self._clock = _Clock()
        self._log = _Log()
        self.reconciliation_active = False
        self.commands: list[tuple[str, object]] = []

    async def generate_order_status_reports(self, command: object) -> list[object]:
        self.commands.append(("order", command))
        return []

    async def generate_fill_reports(self, command: object) -> list[object]:
        self.commands.append(("fill", command))
        return []

    async def generate_position_status_reports(self, command: object) -> list[object]:
        self.commands.append(("position", command))
        return []


class _Engine:
    def __init__(self, client: _Client) -> None:
        self._clients = {client.id: client}
        self._clock = _Clock()
        self._log = _Log()
        self.reconciliation_instrument_ids = list(OWNED_INSTRUMENTS)
        self.open_check_lookback_mins = 60
        self.open_check_open_only = False


class _HttpAccount:
    def __init__(self) -> None:
        self.open_order_symbols: list[str | None] = []
        self.trade_symbols: list[str] = []

    async def query_open_orders(
        self,
        symbol: str | None,
        *,
        recv_window: str,
    ) -> list[object]:
        assert recv_window == "30000"
        self.open_order_symbols.append(symbol)
        return []

    async def query_user_trades(
        self,
        *,
        symbol: str,
        start_time: int | None,
        end_time: int | None,
        recv_window: str,
    ) -> list[object]:
        assert start_time is None
        assert end_time is None
        assert recv_window == "30000"
        self.trade_symbols.append(symbol)
        return []


class _TargetedBinanceClient:
    def __init__(self) -> None:
        self._active_symbols_cache = None
        self._recv_window = 30_000
        self._http_account = _HttpAccount()
        self._log = _Log()
        self._clock = _Clock()

    def _get_cache_active_symbols(self) -> set[str]:
        raise AssertionError("targeted query expanded to cached symbols")

    async def _get_binance_active_position_symbols(
        self,
        _symbol: str | None = None,
    ) -> set[str]:
        raise AssertionError("targeted query expanded to active positions")

    def _log_report_receipt(
        self,
        _count: int,
        _report_name: str,
        _level: object,
    ) -> None:
        return


def test_continuous_reconciliation_commands_are_scoped_per_owned_instrument() -> None:
    client = _Client()
    engine = _Engine(client)

    asyncio.run(scope._query_position_status_reports_scoped(engine))
    asyncio.run(scope._query_order_status_reports_scoped(engine))

    assert _command_instrument_ids(client, "position") == set(
        OWNED_INSTRUMENTS
    )
    assert _command_instrument_ids(client, "order") == set(
        OWNED_INSTRUMENTS
    )
    assert all(
        getattr(command, "instrument_id", None) is not None
        for _kind, command in client.commands
    )


def test_startup_mass_status_queries_each_owned_instrument_only() -> None:
    client = _Client()
    engine = _Engine(client)
    scope._bind_client_scopes(engine)

    mass_status = asyncio.run(
        scope._generate_mass_status_scoped(client, lookback_mins=60)
    )

    assert mass_status is not None
    assert client.reconciliation_active is False
    assert _command_instrument_ids(client, "order") == set(
        OWNED_INSTRUMENTS
    )
    assert _command_instrument_ids(client, "fill") == set(
        OWNED_INSTRUMENTS
    )
    assert _command_instrument_ids(client, "position") == set(
        OWNED_INSTRUMENTS
    )
    assert all(
        getattr(command, "instrument_id", None) is not None
        for _kind, command in client.commands
    )
    assert len(client.commands) == len(OWNED_INSTRUMENTS) * 3


def test_targeted_order_scope_does_not_read_other_active_symbols() -> None:
    client = _TargetedBinanceClient()

    active_symbols, open_orders = asyncio.run(
        binance_patch.BinanceCommonExecutionClient._build_active_symbols(
            client,
            "BTCUSDT",
        )
    )

    assert active_symbols == {"BTCUSDT"}
    assert open_orders == []
    assert client._http_account.open_order_symbols == ["BTCUSDT"]


def test_targeted_fill_scope_queries_only_the_requested_symbol() -> None:
    client = _TargetedBinanceClient()
    instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
    command = GenerateFillReports(
        instrument_id=instrument_id,
        venue_order_id=None,
        start=None,
        end=None,
        command_id=UUID4(),
        ts_init=client._clock.timestamp_ns(),
    )

    reports = asyncio.run(
        binance_patch.BinanceCommonExecutionClient.generate_fill_reports(
            client,
            command,
        )
    )

    assert reports == []
    assert client._http_account.trade_symbols == ["BTCUSDT"]


def _command_instrument_ids(
    client: _Client,
    kind: str,
) -> set[InstrumentId]:
    return {
        command.instrument_id
        for command_kind, command in client.commands
        if command_kind == kind
    }
