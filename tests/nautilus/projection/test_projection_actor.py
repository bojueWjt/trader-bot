from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID, uuid4


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from projection import (  # noqa: E402
    EVENT_MAPPING_CATALOG,
    ExecutionEventEnvelopeV1,
    JsonExecutionSpool,
    ProjectionActor,
    ProjectionConfig,
    ProjectionEventMapper,
)


ACCOUNT_ID = "acct-1"
NODE_ID = "node-1"
NOW = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
INTENT_ID = uuid4()
CLIENT_ORDER_ID = f"B{INTENT_ID.hex}01"


class ProjectionActorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_order_fill_maps_to_envelope_with_stable_business_event_id(self) -> None:
        mapper = _mapper(now=lambda: NOW)
        event = _Event(
            "OrderFilled",
            ts_event=1_718_000_000_000_000_000,
            client_order_id=CLIENT_ORDER_ID,
            venue_order_id="venue-42",
            trade_id="trade-7",
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity="0.25",
            price="65000",
            id="nautilus-emission-a",
        )
        replay = _Event(
            "OrderFilled",
            ts_event=1_718_000_000_000_000_000,
            client_order_id=CLIENT_ORDER_ID,
            venue_order_id="venue-42",
            trade_id="trade-7",
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity="0.25",
            price="65000",
            id="nautilus-emission-b",
        )

        envelope = mapper.to_envelope(event)
        replay_envelope = mapper.to_envelope(replay)

        self.assertIsInstance(envelope, ExecutionEventEnvelopeV1)
        self.assertEqual(envelope.event_id, replay_envelope.event_id)
        self.assertEqual(envelope.node_id, NODE_ID)
        self.assertEqual(envelope.account_id, ACCOUNT_ID)
        self.assertEqual(envelope.intent_id, INTENT_ID)
        self.assertEqual(envelope.client_order_id, CLIENT_ORDER_ID)
        self.assertEqual(envelope.venue_order_id, "venue-42")
        self.assertEqual(envelope.trade_id, "trade-7")
        self.assertEqual(envelope.event_type, "OrderFilled")
        self.assertEqual(envelope.ts_ingest, NOW)
        self.assertEqual(envelope.payload["instrument_id"], "BTCUSDT-PERP.BINANCE")
        self.assertNotIn("nautilus-emission", envelope.event_id)

    def test_duplicate_business_event_is_spooled_and_posted_once(self) -> None:
        sink = _RecordingSink()
        actor = _actor(self._tmp_spool(), sink=sink)
        event = _Event(
            "OrderFilled",
            ts_event=1_718_000_000_000_000_000,
            client_order_id=CLIENT_ORDER_ID,
            venue_order_id="venue-42",
            trade_id="trade-7",
        )
        duplicate = _Event(
            "OrderFilled",
            ts_event=1_718_000_000_000_000_000,
            client_order_id=CLIENT_ORDER_ID,
            venue_order_id="venue-42",
            trade_id="trade-7",
            id="different-nautilus-event-id",
        )

        first = actor.on_event(event)
        second = actor.on_event(duplicate)

        self.assertEqual(first, second)
        self.assertEqual(len(sink.posted_batches), 1)
        self.assertEqual(len(sink.posted_batches[0]), 1)
        self.assertEqual(actor.spool.pending_count, 0)

    def test_offline_spool_replays_in_event_time_order_and_clears_only_acked(self) -> None:
        sink = _RecordingSink(fail=True)
        spool = self._tmp_spool()
        actor = _actor(spool, sink=sink)

        later = _Event("OrderAccepted", ts_event=30, client_order_id="coid-2")
        earlier = _Event("OrderSubmitted", ts_event=10, client_order_id="coid-1")
        actor.on_event(later)
        actor.on_event(earlier)
        self.assertEqual(actor.spool.pending_count, 2)

        sink.fail = False
        sink.ack_limit = 1
        actor.flush()
        self.assertEqual(actor.spool.pending_count, 1)
        self.assertEqual(
            [event.event_type for event in sink.posted_batches[-1]],
            ["OrderSubmitted", "OrderAccepted"],
        )

        sink.ack_limit = None
        actor.flush()
        self.assertEqual(actor.spool.pending_count, 0)

    def test_offline_sink_failure_marks_projection_degraded(self) -> None:
        sink = _RecordingSink(fail=True)
        health = _RecordingHealth()
        actor = _actor(
            self._tmp_spool(),
            sink=sink,
            health=health,
        )

        actor.on_event(
            _Event(
                "OrderAccepted",
                ts_event=30,
                client_order_id="coid-2",
            )
        )

        self.assertEqual(
            health.degraded,
            ["control-plane execution-event sink unavailable"],
        )
        self.assertEqual(health.failed, [])

    def test_out_of_order_duplicate_delivery_keeps_single_pending_update(self) -> None:
        sink = _RecordingSink(fail=True)
        actor = _actor(self._tmp_spool(), sink=sink)
        fill = _Event(
            "OrderFilled",
            ts_event=20,
            client_order_id=CLIENT_ORDER_ID,
            venue_order_id="venue-42",
            trade_id="trade-7",
        )
        cancel = _Event("OrderCanceled", ts_event=30, client_order_id=CLIENT_ORDER_ID)

        actor.on_event(cancel)
        actor.on_event(fill)
        actor.on_event(fill)

        self.assertEqual(actor.spool.pending_count, 2)
        self.assertEqual(
            [event.event_type for event in actor.spool.pending_events()],
            ["OrderFilled", "OrderCanceled"],
        )

    def test_projection_lag_updates_health_and_degrades_when_above_threshold(self) -> None:
        sink = _RecordingSink(fail=True)
        health = _RecordingHealth()
        actor = _actor(
            self._tmp_spool(),
            sink=sink,
            config=ProjectionConfig(
                node_id=NODE_ID,
                account_id=ACCOUNT_ID,
                lag_degrade_threshold_ms=5,
            ),
            health=health,
            now=lambda: NOW,
        )

        event_id = actor.on_event(
            _Event("AccountState", ts_event=_ns(NOW - timedelta(milliseconds=6)))
        )

        self.assertEqual(health.progress[-1], (6, event_id))
        self.assertIn(
            "projection lag 6ms exceeds 5ms",
            health.degraded,
        )
        self.assertEqual(health.failed, [])

    def test_event_mapping_catalog_lists_required_families(self) -> None:
        self.assertIn("OrderFilled", EVENT_MAPPING_CATALOG)
        self.assertIn("PositionOpened", EVENT_MAPPING_CATALOG)
        self.assertIn("AccountState", EVENT_MAPPING_CATALOG)

    def _tmp_spool(self) -> JsonExecutionSpool:
        path = Path(self._tmpdir.name) / f"{self._testMethodName}.json"
        return JsonExecutionSpool(path)


def _mapper(now=lambda: NOW) -> ProjectionEventMapper:
    return ProjectionEventMapper(
        ProjectionConfig(node_id=NODE_ID, account_id=ACCOUNT_ID),
        now=now,
    )


def _ns(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


def _actor(
    spool: JsonExecutionSpool,
    sink: _RecordingSink,
    config: ProjectionConfig | None = None,
    health: _RecordingHealth | None = None,
    now=lambda: NOW,
) -> ProjectionActor:
    return ProjectionActor(
        config=config or ProjectionConfig(node_id=NODE_ID, account_id=ACCOUNT_ID),
        sink=sink,
        spool=spool,
        now=now,
        health=health,
    )


@dataclass
class _Event:
    class_name: str
    ts_event: int
    client_order_id: str | None = None
    venue_order_id: str | None = None
    trade_id: str | None = None
    instrument_id: str | None = None
    quantity: str | None = None
    price: str | None = None
    id: str = "ignored-nautilus-id"

    @property
    def __class__(self) -> type[Any]:  # type: ignore[override]
        return type(self.class_name, (), {})


class _RecordingSink:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.ack_limit: int | None = None
        self.posted_batches: list[list[ExecutionEventEnvelopeV1]] = []

    def post_events(
        self, node_id: str, events: Sequence[ExecutionEventEnvelopeV1]
    ) -> list[str]:
        del node_id
        if self.fail:
            raise RuntimeError("control-plane offline")
        batch = list(events)
        self.posted_batches.append(batch)
        accepted = batch[: self.ack_limit] if self.ack_limit is not None else batch
        return [event.event_id for event in accepted]


class _RecordingHealth:
    def __init__(self) -> None:
        self.progress: list[tuple[int, str | None]] = []
        self.ready = 0
        self.degraded: list[str] = []
        self.failed: list[str] = []

    def record_projection_progress(
        self, projection_lag_ms: int, last_event_id: str | None = None
    ) -> None:
        self.progress.append((projection_lag_ms, last_event_id))

    def mark_projection_ready(self) -> None:
        self.ready += 1

    def mark_projection_degraded(self, reason: str) -> None:
        self.degraded.append(reason)

    def mark_projection_failed(self, reason: str) -> None:
        self.failed.append(reason)


if __name__ == "__main__":
    unittest.main()
