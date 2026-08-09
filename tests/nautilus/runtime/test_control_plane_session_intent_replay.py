from __future__ import annotations

import sys
import time
from pathlib import Path
from threading import Event, get_ident
from typing import Callable

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from runtime.control_plane_session import NodeControlPlaneSession


def test_startup_replay_is_delivered_by_intent_delivery_lane() -> None:
    replay_item = object()
    delivered = Event()
    replay_threads: list[int] = []
    delivery_threads: list[int] = []

    def replay_pending() -> tuple[object, ...]:
        replay_threads.append(get_ident())
        return (replay_item,)

    def deliver(item: object) -> None:
        assert item is replay_item
        delivery_threads.append(get_ident())
        delivered.set()

    session = NodeControlPlaneSession(
        intent_replay=replay_pending,
        intent_fetch=lambda _capacity: (),
        intent_deliver=deliver,
        intent_delivery_capacity=4,
        retry_budget=1,
        retry_base_delay_seconds=0.001,
        retry_max_delay_seconds=0.001,
        retry_jitter_ratio=0.0,
        circuit_reset_seconds=0.01,
        operation_timeout_seconds=0.5,
    )
    session.start()
    try:
        assert delivered.wait(timeout=1.0)
        assert len(replay_threads) == 1
        assert len(delivery_threads) == 1
        assert replay_threads[0] != delivery_threads[0]
    finally:
        assert session.stop(time.monotonic() + 1.0)


@pytest.mark.parametrize("retry_budget", (1, 3))
def test_startup_replay_over_capacity_backpressures_then_delivers_once(
    retry_budget: int,
) -> None:
    receipts = ("receipt-1", "receipt-2", "receipt-3")
    first_delivery_started = Event()
    release_delivery = Event()
    replay_calls = 0
    delivered: list[str] = []
    fatal_reasons: list[str] = []

    def replay_pending() -> tuple[str, ...]:
        nonlocal replay_calls
        replay_calls += 1
        return receipts

    def deliver(receipt: str) -> None:
        delivered.append(receipt)
        if receipt == receipts[0]:
            first_delivery_started.set()
            release_delivery.wait(timeout=1.0)

    session = NodeControlPlaneSession(
        intent_replay=replay_pending,
        intent_fetch=lambda _capacity: (),
        intent_deliver=deliver,
        intent_fetch_interval_seconds=0.005,
        intent_delivery_capacity=2,
        retry_budget=retry_budget,
        retry_base_delay_seconds=0.001,
        retry_max_delay_seconds=0.001,
        retry_jitter_ratio=0.0,
        circuit_reset_seconds=0.01,
        operation_timeout_seconds=0.5,
        fatal_termination_hook=fatal_reasons.append,
    )
    session.start()
    try:
        assert first_delivery_started.wait(timeout=1.0)
        backpressured = session.snapshot()

        assert backpressured.process_liveness is True
        assert backpressured.degraded is True
        assert (
            backpressured.lanes["intent_delivery"].fatal_failure
            is False
        )
        assert fatal_reasons == []
        assert session.wait_for_termination(timeout=0.01) is False

        release_delivery.set()
        assert _wait_until(lambda: len(delivered) == len(receipts))
        assert delivered == list(receipts)
        assert replay_calls == 1
        assert _wait_until(lambda: session.snapshot().ready)
    finally:
        release_delivery.set()
        assert session.stop(time.monotonic() + 1.0)


def test_durable_replay_read_failure_remains_fatal() -> None:
    fatal_reasons: list[str] = []

    def replay_pending(_capacity: int) -> tuple[object, ...]:
        raise OSError("durable inbox read failed")

    session = NodeControlPlaneSession(
        intent_replay=replay_pending,
        intent_deliver=lambda _item: None,
        retry_budget=3,
        retry_base_delay_seconds=0.001,
        retry_max_delay_seconds=0.001,
        retry_jitter_ratio=0.0,
        fatal_termination_hook=fatal_reasons.append,
    )
    session.start()
    try:
        assert session.wait_for_termination(timeout=1.0)
        failed = session.snapshot()

        assert failed.process_liveness is False
        assert (
            failed.lanes["intent_fetch"].fatal_failure
            == "durable inbox read failed"
        )
        assert fatal_reasons == ["durable inbox read failed"]
    finally:
        assert session.stop(time.monotonic() + 1.0)


def _wait_until(
    predicate: Callable[[], bool],
    timeout: float = 1.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())
