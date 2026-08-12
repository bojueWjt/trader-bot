from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from typing import Any

import pytest


pytest.importorskip("nautilus_trader")

ROOT = Path(__file__).resolve().parents[3]
NAUTILUS_NODE = ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN = ROOT / "packages" / "execution-domain"

for path in (NAUTILUS_NODE, EXECUTION_DOMAIN):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)

from app.nautilus_actors import ExecutionProjectionActor  # noqa: E402
from nautilus_trader import __version__ as nautilus_version  # noqa: E402
from nautilus_trader.test_kit.stubs.component import (  # noqa: E402
    TestComponentStubs,
)


EXPECTED_NAUTILUS_VERSION = "1.227.0"


class _RecordingProjection:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_event(self, event: Any) -> bool:
        self.events.append(event)
        return True


class ProjectionHostNautilusTests(unittest.TestCase):
    def test_projection_actor_receives_real_nautilus_on_event_callbacks(self) -> None:
        self.assertEqual(nautilus_version, EXPECTED_NAUTILUS_VERSION)
        topic = "events.execution.host"
        projection = _RecordingProjection()
        message_bus = TestComponentStubs.msgbus()
        clock = TestComponentStubs.clock()
        actor = ExecutionProjectionActor(
            projection,
            event_topics=(topic,),
            worker_shutdown_wait_seconds=1.0,
        )
        actor.register_base(
            TestComponentStubs.portfolio(),
            message_bus,
            TestComponentStubs.cache(),
            clock,
        )

        actor.on_start()
        try:
            self.assertEqual(message_bus.topics(), [topic])
            event = {"event_id": "host-event-1"}
            message_bus.publish(topic, event, external_pub=False)
            deadline = time.monotonic() + 1.0
            while not projection.events and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(projection.events, [event])
        finally:
            actor.on_stop()

        self.assertEqual(message_bus.topics(), [])


if __name__ == "__main__":
    unittest.main()
