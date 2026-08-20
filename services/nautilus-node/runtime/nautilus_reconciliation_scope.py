from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Awaitable, Callable, cast


EXPECTED_NAUTILUS_VERSION = "1.227.0"
CLIENT_SCOPE_ATTRIBUTE = "_trader_reconciliation_instrument_ids"
PATCH_MARKER_ATTRIBUTE = "_trader_reconciliation_scope_installed"

_original_query_position_status_reports: Callable[..., Awaitable[Any]] | bool = (
    False
)
_original_query_order_status_reports: Callable[..., Awaitable[Any]] | bool = (
    False
)
_original_reconcile_execution_state: Callable[..., Awaitable[bool]] | bool = (
    False
)
_original_generate_mass_status: Callable[..., Awaitable[Any]] | bool = False


def install_scoped_reconciliation() -> None:
    """Install the request-level ownership scope for pinned Nautilus 1.227.0."""

    import nautilus_trader
    from nautilus_trader.live.execution_client import LiveExecutionClient
    from nautilus_trader.live.execution_engine import LiveExecutionEngine

    version = str(getattr(nautilus_trader, "__version__", "")).strip()
    if version != EXPECTED_NAUTILUS_VERSION:
        raise RuntimeError(
            "scoped reconciliation patch requires "
            f"nautilus_trader=={EXPECTED_NAUTILUS_VERSION}, found {version!r}"
        )
    if getattr(LiveExecutionEngine, PATCH_MARKER_ATTRIBUTE, False):
        return

    global _original_generate_mass_status
    global _original_query_order_status_reports
    global _original_query_position_status_reports
    global _original_reconcile_execution_state

    _original_query_position_status_reports = (
        LiveExecutionEngine._query_position_status_reports
    )
    _original_query_order_status_reports = (
        LiveExecutionEngine._query_order_status_reports
    )
    _original_reconcile_execution_state = (
        LiveExecutionEngine.reconcile_execution_state
    )
    _original_generate_mass_status = LiveExecutionClient.generate_mass_status

    LiveExecutionEngine._query_position_status_reports = (  # type: ignore[method-assign]
        _query_position_status_reports_scoped
    )
    LiveExecutionEngine._query_order_status_reports = (  # type: ignore[method-assign]
        _query_order_status_reports_scoped
    )
    LiveExecutionEngine.reconcile_execution_state = (  # type: ignore[method-assign]
        _reconcile_execution_state_scoped
    )
    LiveExecutionClient.generate_mass_status = (  # type: ignore[method-assign]
        _generate_mass_status_scoped
    )
    setattr(LiveExecutionEngine, PATCH_MARKER_ATTRIBUTE, True)


async def _reconcile_execution_state_scoped(
    engine: Any,
    timeout_secs: float = 10.0,
) -> bool:
    _bind_client_scopes(engine)
    original = _require_original(
        _original_reconcile_execution_state,
        "LiveExecutionEngine.reconcile_execution_state",
    )
    return await original(engine, timeout_secs)


async def _query_position_status_reports_scoped(
    engine: Any,
) -> tuple[dict[tuple[Any, Any], Any], set[Any]]:
    instrument_ids = _owned_instrument_ids(engine)
    if not instrument_ids:
        original = _require_original(
            _original_query_position_status_reports,
            "LiveExecutionEngine._query_position_status_reports",
        )
        return await original(engine)

    from nautilus_trader.common.enums import LogLevel
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.execution.messages import (
        GeneratePositionStatusReports,
    )

    clients = list(engine._clients.values())
    tasks: list[Awaitable[Any]] = []
    task_clients: list[Any] = []
    for client in clients:
        for instrument_id in _instrument_ids_for_client(
            instrument_ids,
            client,
        ):
            command = GeneratePositionStatusReports(
                instrument_id=instrument_id,
                start=None,
                end=None,
                command_id=UUID4(),
                ts_init=engine._clock.timestamp_ns(),
                log_receipt_level=LogLevel.DEBUG,
            )
            tasks.append(client.generate_position_status_reports(command))
            task_clients.append(client)

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:
        engine._log.error(
            f"Failed to gather scoped position status reports: {exc}"
        )
        return {}, {client.venue for client in clients}

    venue_positions: dict[tuple[Any, Any], Any] = {}
    failed_venues: set[Any] = set()
    for client, reports_or_exception in zip(
        task_clients,
        results,
        strict=True,
    ):
        if isinstance(reports_or_exception, Exception):
            failed_venues.add(client.venue)
            engine._log.error(
                "Failed to generate scoped position status reports for "
                f"venue {client.venue}: {reports_or_exception}"
            )
            continue

        reports = cast(list[Any], reports_or_exception)
        for report in reports:
            venue_positions[(report.instrument_id, report.account_id)] = (
                report
            )

    return venue_positions, failed_venues


async def _query_order_status_reports_scoped(
    engine: Any,
) -> tuple[list[Any], set[Any]]:
    instrument_ids = _owned_instrument_ids(engine)
    if not instrument_ids:
        original = _require_original(
            _original_query_order_status_reports,
            "LiveExecutionEngine._query_order_status_reports",
        )
        return await original(engine)

    from nautilus_trader.common.enums import LogLevel
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.execution.messages import GenerateOrderStatusReports

    order_status_start = engine._clock.utc_now() - timedelta(
        minutes=engine.open_check_lookback_mins,
    )
    tasks: list[Awaitable[Any]] = []
    for client in engine._clients.values():
        for instrument_id in _instrument_ids_for_client(
            instrument_ids,
            client,
        ):
            command = GenerateOrderStatusReports(
                instrument_id=instrument_id,
                start=order_status_start,
                end=None,
                open_only=engine.open_check_open_only,
                command_id=UUID4(),
                ts_init=engine._clock.timestamp_ns(),
                log_receipt_level=LogLevel.DEBUG,
            )
            tasks.append(client.generate_order_status_reports(command))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_order_reports: list[Any] = []
    for reports_or_exception in results:
        if isinstance(reports_or_exception, Exception):
            engine._log.error(
                "Failed to generate scoped order status reports: "
                f"{reports_or_exception}"
            )
            continue
        all_order_reports.extend(cast(list[Any], reports_or_exception))

    venue_reported_ids = {
        report.client_order_id
        for report in all_order_reports
        if report.client_order_id is not None
    }
    return all_order_reports, venue_reported_ids


async def _generate_mass_status_scoped(
    client: Any,
    lookback_mins: int | None = None,
) -> Any:
    raw_instrument_ids = getattr(client, CLIENT_SCOPE_ATTRIBUTE, False)
    if raw_instrument_ids is False:
        original = _require_original(
            _original_generate_mass_status,
            "LiveExecutionClient.generate_mass_status",
        )
        return await original(client, lookback_mins)

    from nautilus_trader.common.enums import LogLevel
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.execution.messages import GenerateFillReports
    from nautilus_trader.execution.messages import GenerateOrderStatusReports
    from nautilus_trader.execution.messages import (
        GeneratePositionStatusReports,
    )
    from nautilus_trader.execution.reports import ExecutionMassStatus

    instrument_ids = tuple(raw_instrument_ids)
    client._log.info(
        "Generating scoped ExecutionMassStatus for "
        f"{len(instrument_ids)} instrument(s)..."
    )
    client.reconciliation_active = True
    mass_status = ExecutionMassStatus(
        client_id=client.id,
        account_id=client.account_id,
        venue=client.venue,
        report_id=UUID4(),
        ts_init=client._clock.timestamp_ns(),
    )
    since = None
    if lookback_mins is not None:
        since = client._clock.utc_now() - timedelta(minutes=lookback_mins)

    try:
        for instrument_id in instrument_ids:
            order_command = GenerateOrderStatusReports(
                instrument_id=instrument_id,
                start=since,
                end=None,
                open_only=False,
                command_id=UUID4(),
                ts_init=client._clock.timestamp_ns(),
                log_receipt_level=LogLevel.DEBUG,
            )
            fill_command = GenerateFillReports(
                instrument_id=instrument_id,
                venue_order_id=None,
                start=since,
                end=None,
                command_id=UUID4(),
                ts_init=client._clock.timestamp_ns(),
            )
            position_command = GeneratePositionStatusReports(
                instrument_id=instrument_id,
                start=since,
                end=None,
                command_id=UUID4(),
                ts_init=client._clock.timestamp_ns(),
                log_receipt_level=LogLevel.DEBUG,
            )
            reports = await asyncio.gather(
                client.generate_order_status_reports(order_command),
                client.generate_fill_reports(fill_command),
                client.generate_position_status_reports(position_command),
            )
            mass_status.add_order_reports(reports=reports[0])
            mass_status.add_fill_reports(reports=reports[1])
            mass_status.add_position_reports(reports=reports[2])
        return mass_status
    except Exception as exc:
        client._log.exception(
            "Cannot reconcile scoped execution state",
            exc,
        )
        return None
    finally:
        client.reconciliation_active = False


def _bind_client_scopes(engine: Any) -> None:
    instrument_ids = _owned_instrument_ids(engine)
    for client in engine._clients.values():
        client_instrument_ids = _instrument_ids_for_client(
            instrument_ids,
            client,
        )
        setattr(
            client,
            CLIENT_SCOPE_ATTRIBUTE,
            client_instrument_ids,
        )


def _owned_instrument_ids(engine: Any) -> tuple[Any, ...]:
    raw_instrument_ids = getattr(
        engine,
        "reconciliation_instrument_ids",
        (),
    )
    if not raw_instrument_ids:
        return ()
    return tuple(dict.fromkeys(raw_instrument_ids))


def _instrument_ids_for_client(
    instrument_ids: tuple[Any, ...],
    client: Any,
) -> tuple[Any, ...]:
    client_venue = getattr(client, "venue", None)
    if client_venue is None:
        return instrument_ids
    return tuple(
        instrument_id
        for instrument_id in instrument_ids
        if instrument_id.venue == client_venue
    )


def _require_original(
    original: Callable[..., Awaitable[Any]] | bool,
    method_name: str,
) -> Callable[..., Awaitable[Any]]:
    if original is False:
        raise RuntimeError(
            f"scoped reconciliation patch was not installed for {method_name}"
        )
    return original
