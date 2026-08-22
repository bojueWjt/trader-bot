from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, cast

from execution_domain.order_ownership import is_robot_client_order_id


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
    # Account position deltas cannot prove ownership. Feeding them to Nautilus
    # reconciliation can synthesize external orders and inferred fills from
    # manual trading, so ownership mode deliberately excludes this evidence.
    return {}, set()


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

    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.execution.messages import GenerateOrderStatusReport

    tasks: list[Awaitable[Any]] = []
    task_clients: list[Any] = []
    for order in _owned_cached_orders(engine._cache, instrument_ids):
        client = _client_for_instrument(engine, order.instrument_id)
        if client is None:
            continue
        command = GenerateOrderStatusReport(
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=getattr(order, "venue_order_id", None),
            command_id=UUID4(),
            ts_init=engine._clock.timestamp_ns(),
        )
        tasks.append(client.generate_order_status_report(command))
        task_clients.append(client)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_order_reports: list[Any] = []
    failed_venues: set[Any] = set()
    for client, report_or_exception in zip(
        task_clients,
        results,
        strict=True,
    ):
        if isinstance(report_or_exception, Exception):
            failed_venues.add(client.venue)
            engine._log.error(
                "Failed to generate owned order status report for "
                f"venue {client.venue}: {report_or_exception}"
            )
            continue
        if report_or_exception is None:
            continue
        report = cast(Any, report_or_exception)
        if not is_robot_client_order_id(
            str(getattr(report, "client_order_id", "") or "")
        ):
            continue
        all_order_reports.append(report)

    venue_reported_ids = {
        report.client_order_id
        for report in all_order_reports
        if report.client_order_id is not None
    }
    if failed_venues:
        engine._log.error(
            "Owned order status polling failed for venue(s): "
            f"{sorted(str(venue) for venue in failed_venues)}"
        )
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

    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.execution.messages import GenerateOrderStatusReport
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
    del lookback_mins

    try:
        cache = getattr(client, "_cache", None)
        for order in _owned_cached_orders(cache, instrument_ids):
            order_command = GenerateOrderStatusReport(
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=getattr(order, "venue_order_id", None),
                command_id=UUID4(),
                ts_init=client._clock.timestamp_ns(),
            )
            report = await client.generate_order_status_report(
                order_command
            )
            if report is None:
                continue
            if not is_robot_client_order_id(
                str(getattr(report, "client_order_id", "") or "")
            ):
                continue
            mass_status.add_order_reports(reports=[report])
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


def _client_for_instrument(engine: Any, instrument_id: Any) -> Any | None:
    venue = getattr(instrument_id, "venue", None)
    for client in engine._clients.values():
        if venue is None or getattr(client, "venue", None) == venue:
            return client
    return None


def _owned_cached_orders(
    cache: Any,
    instrument_ids: tuple[Any, ...],
) -> tuple[Any, ...]:
    if cache is None:
        return ()
    orders_method = getattr(cache, "orders", None)
    if not callable(orders_method):
        return ()
    owned: list[Any] = []
    seen: set[str] = set()
    for instrument_id in instrument_ids:
        try:
            orders = orders_method(instrument_id=instrument_id)
        except TypeError:
            orders = orders_method()
        for order in orders or ():
            order_instrument_id = getattr(order, "instrument_id", None)
            if order_instrument_id != instrument_id:
                continue
            client_order_id = str(
                getattr(order, "client_order_id", "") or ""
            )
            if not is_robot_client_order_id(client_order_id):
                continue
            if client_order_id in seen:
                continue
            seen.add(client_order_id)
            owned.append(order)
    return tuple(owned)


def _require_original(
    original: Callable[..., Awaitable[Any]] | bool,
    method_name: str,
) -> Callable[..., Awaitable[Any]]:
    if original is False:
        raise RuntimeError(
            f"scoped reconciliation patch was not installed for {method_name}"
        )
    return original
