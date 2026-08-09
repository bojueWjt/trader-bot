from __future__ import annotations

import sys
import time
from pathlib import Path
from threading import Event, get_ident


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
