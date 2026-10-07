"""Reporter → actor → spool chain for OrderDenied; preserve envelope identity."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"


def _load_isolated_nautilus():
    saved_path = list(sys.path)
    saved_app = {
        name: module
        for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    sys.path.insert(0, str(DOMAIN_ROOT))
    sys.path.insert(0, str(SERVICE_ROOT))
    try:
        from projection.actor import (  # noqa: E402
            ProjectionActor,
            ProjectionIngestOutcome,
        )
        from projection.contracts import ExecutionEventEnvelopeV1  # noqa: E402
        from projection.event_mapper import (  # noqa: E402
            ProjectionConfig,
            ProjectionEventMapper,
        )
        from projection.spool import JsonExecutionSpool  # noqa: E402

        spec = importlib.util.spec_from_file_location(
            "_g1_nautilus_node",
            SERVICE_ROOT / "app" / "node.py",
        )
        if spec is None or spec.loader is None:
            raise ImportError("nautilus node.py is unavailable")
        node_mod = importlib.util.module_from_spec(spec)
        sys.modules["_g1_nautilus_node"] = node_mod
        spec.loader.exec_module(node_mod)
        return SimpleNamespace(
            ProjectionActor=ProjectionActor,
            ProjectionIngestOutcome=ProjectionIngestOutcome,
            ExecutionEventEnvelopeV1=ExecutionEventEnvelopeV1,
            ProjectionConfig=ProjectionConfig,
            ProjectionEventMapper=ProjectionEventMapper,
            JsonExecutionSpool=JsonExecutionSpool,
            build_protection_event_reporter=node_mod._build_protection_event_reporter,
            stop_background_workers=node_mod._stop_background_workers,
        )
    finally:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                if name not in saved_app:
                    sys.modules.pop(name, None)
        sys.modules.update(saved_app)
        sys.path[:] = saved_path


_nautilus = _load_isolated_nautilus()
ProjectionActor = _nautilus.ProjectionActor
ProjectionIngestOutcome = _nautilus.ProjectionIngestOutcome
ExecutionEventEnvelopeV1 = _nautilus.ExecutionEventEnvelopeV1
ProjectionConfig = _nautilus.ProjectionConfig
ProjectionEventMapper = _nautilus.ProjectionEventMapper
JsonExecutionSpool = _nautilus.JsonExecutionSpool
_build_protection_event_reporter = _nautilus.build_protection_event_reporter
_stop_background_workers = _nautilus.stop_background_workers


ACCOUNT_ID = "account-b"
NODE_ID = "node-b"
INSTRUMENT_ID = "SOLUSDT-PERP.BINANCE"
NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)


class _RecordingSink:
    def __init__(self) -> None:
        self.posted: list[ExecutionEventEnvelopeV1] = []

    def post_events(self, node_id: str, events):
        del node_id
        batch = list(events)
        self.posted.extend(batch)
        return [event.event_id for event in batch]


class _Lifecycle:
    def __init__(self) -> None:
        self.failures: list[tuple[str, str]] = []

    def mark_dependency_failed(self, dependency, reason: str) -> None:
        self.failures.append((str(getattr(dependency, "value", dependency)), reason))

    def force_halt(self, reason: str) -> None:
        del reason


class _Health:
    def register_provider(self, name: str, provider) -> None:
        del name, provider


def _live_config() -> ProjectionConfig:
    return ProjectionConfig(
        node_id=NODE_ID,
        account_id=ACCOUNT_ID,
        allowed_instrument_ids=frozenset({INSTRUMENT_ID}),
        require_robot_order_ownership=True,
    )


def _runtime(actor: ProjectionActor):
    return SimpleNamespace(
        config=SimpleNamespace(account_id=ACCOUNT_ID, node_id=NODE_ID),
        control_plane=SimpleNamespace(),
        lifecycle=_Lifecycle(),
        health=_Health(),
        projection_actor=actor,
        background_workers=[],
        incident_reporter=None,
        exchange_state_mirror=None,
        exchange_cancel_adapter=None,
    )


def _robot_id(intent_id: UUID, sequence: int = 1) -> str:
    return f"B{intent_id.hex}{sequence:02d}"


def test_reporter_actor_spool_keeps_nested_reason_and_instrument(tmp_path: Path) -> None:
    spool = JsonExecutionSpool(tmp_path / "spool.json")
    sink = _RecordingSink()
    actor = ProjectionActor(
        config=_live_config(),
        sink=sink,
        spool=spool,
        now=lambda: NOW,
    )
    runtime = _runtime(actor)
    reporter = _build_protection_event_reporter(runtime)
    intent_id = uuid4()
    client_order_id = _robot_id(intent_id)
    event = {
        "event_type": "OrderDenied",
        "event_key": f"{intent_id}:{client_order_id}:lot-size",
        "intent_id": str(intent_id),
        "client_order_id": client_order_id,
        "instrument_id": INSTRUMENT_ID,
        "reason": "lot-size",
        "ts_event": NOW,
        "payload": {"reason": "lot-size", "instrument_id": INSTRUMENT_ID},
    }
    ingested: list[object] = []
    original_ingest = actor.ingest_event

    def _capture_ingest(source):
        ingested.append(source)
        return original_ingest(source)

    actor.ingest_event = _capture_ingest  # type: ignore[method-assign]
    try:
        assert reporter(event) is True
        worker = runtime.background_workers[0]
        assert worker.wait_empty(timeout_seconds=1.0)
    finally:
        _stop_background_workers(runtime)

    assert ingested
    assert isinstance(ingested[0], dict)
    assert not isinstance(ingested[0], ExecutionEventEnvelopeV1)
    assert len(sink.posted) == 1
    durable = sink.posted[0]
    assert durable.event_type == "OrderDenied"
    assert durable.account_id == ACCOUNT_ID
    assert durable.node_id == NODE_ID
    assert durable.client_order_id == client_order_id
    assert durable.payload.get("reason") == "lot-size"
    assert durable.payload.get("instrument_id") == INSTRUMENT_ID
    pending = list(spool.pending_events())
    assert (not pending) or pending[0].event_id == durable.event_id


def test_no_client_order_denied_is_filtered_not_invented(tmp_path: Path) -> None:
    spool = JsonExecutionSpool(tmp_path / "spool.json")
    sink = _RecordingSink()
    actor = ProjectionActor(
        config=_live_config(),
        sink=sink,
        spool=spool,
        now=lambda: NOW,
    )
    mapper = ProjectionEventMapper(_live_config(), now=lambda: NOW)
    assert mapper.to_envelope(
        {
            "event_type": "OrderDenied",
            "intent_id": str(uuid4()),
            "instrument_id": INSTRUMENT_ID,
            "reason": "position_exists",
            "payload": {"reason": "position_exists"},
            "ts_event": NOW,
        }
    ) is None
    result = actor.ingest_event(
        {
            "event_type": "OrderDenied",
            "intent_id": str(uuid4()),
            "instrument_id": INSTRUMENT_ID,
            "reason": "position_exists",
            "payload": {"reason": "position_exists", "instrument_id": INSTRUMENT_ID},
            "ts_event": NOW,
        }
    )
    assert result.outcome in {
        ProjectionIngestOutcome.FILTERED,
        ProjectionIngestOutcome.IGNORED,
    }
    manual = actor.ingest_event(
        {
            "event_type": "OrderDenied",
            "client_order_id": "stToAg_manual_stop_1",
            "instrument_id": INSTRUMENT_ID,
            "reason": "manual",
            "payload": {"reason": "manual", "instrument_id": INSTRUMENT_ID},
            "ts_event": NOW,
        }
    )
    assert manual.outcome in {
        ProjectionIngestOutcome.FILTERED,
        ProjectionIngestOutcome.IGNORED,
    }
    assert sink.posted == []
    actor.flush()
    assert sink.posted == []


def test_canonical_envelope_keeps_event_id_and_rejects_foreign_account(
    tmp_path: Path,
) -> None:
    spool = JsonExecutionSpool(tmp_path / "spool.json")
    sink = _RecordingSink()
    actor = ProjectionActor(
        config=_live_config(),
        sink=sink,
        spool=spool,
        now=lambda: NOW,
    )
    assert not hasattr(actor, "_mapping_source")
    mapper_calls: list[object] = []
    original_to_envelope = actor._mapper.to_envelope

    def _tracked_to_envelope(event):
        mapper_calls.append(event)
        return original_to_envelope(event)

    actor._mapper.to_envelope = _tracked_to_envelope  # type: ignore[method-assign]
    intent_id = uuid4()
    client_order_id = _robot_id(intent_id)
    canonical_id = "canonical-event-id-keep-me"
    local = ExecutionEventEnvelopeV1(
        event_id=canonical_id,
        node_id=NODE_ID,
        account_id=ACCOUNT_ID,
        intent_id=intent_id,
        client_order_id=client_order_id,
        event_type="OrderDenied",
        ts_event=NOW,
        ts_ingest=NOW,
        payload={"reason": "lot-size", "instrument_id": INSTRUMENT_ID},
    )
    remapped = original_to_envelope(local)
    assert remapped is not None
    assert remapped.event_id != canonical_id
    accepted = actor.ingest_event(local)
    assert mapper_calls == []
    assert accepted.outcome is ProjectionIngestOutcome.DURABLE
    assert accepted.event_id == canonical_id
    actor.flush()
    assert sink.posted[0].event_id == canonical_id
    assert sink.posted[0].account_id == ACCOUNT_ID
    assert sink.posted[0].node_id == NODE_ID
    assert sink.posted[0].payload.get("reason") == "lot-size"
    posted_after_local = len(sink.posted)

    foreign = ExecutionEventEnvelopeV1(
        event_id="foreign-event-id",
        node_id=NODE_ID,
        account_id="account-z",
        intent_id=intent_id,
        client_order_id=client_order_id,
        event_type="OrderDenied",
        ts_event=NOW,
        ts_ingest=NOW,
        payload={"reason": "lot-size", "instrument_id": INSTRUMENT_ID},
    )
    relabeled = original_to_envelope(foreign)
    assert relabeled is not None
    assert relabeled.account_id == ACCOUNT_ID
    assert relabeled.event_id != "foreign-event-id"
    dropped = actor.ingest_event(foreign)
    assert mapper_calls == []
    assert dropped.outcome is ProjectionIngestOutcome.FILTERED
    assert dropped.event_id is None
    actor.flush()
    assert len(sink.posted) == posted_after_local
    assert all(item.event_id != "foreign-event-id" for item in sink.posted)
    assert all(item.event_id != relabeled.event_id for item in sink.posted)
    assert all(item.account_id != "account-z" for item in sink.posted)

    other_node = ExecutionEventEnvelopeV1(
        event_id="other-node-event-id",
        node_id="node-z",
        account_id=ACCOUNT_ID,
        intent_id=intent_id,
        client_order_id=client_order_id,
        event_type="OrderDenied",
        ts_event=NOW,
        ts_ingest=NOW,
        payload={"reason": "lot-size", "instrument_id": INSTRUMENT_ID},
    )
    node_dropped = actor.ingest_event(other_node)
    assert mapper_calls == []
    assert node_dropped.outcome is ProjectionIngestOutcome.FILTERED
    actor.flush()
    assert len(sink.posted) == posted_after_local

    manual = ExecutionEventEnvelopeV1(
        event_id="manual-event-id",
        node_id=NODE_ID,
        account_id=ACCOUNT_ID,
        intent_id=intent_id,
        client_order_id="aos_half_sl_1789061188",
        event_type="OrderDenied",
        ts_event=NOW,
        ts_ingest=NOW,
        payload={"reason": "manual", "instrument_id": INSTRUMENT_ID},
    )
    ignored = actor.ingest_event(manual)
    assert mapper_calls == []
    assert ignored.outcome is ProjectionIngestOutcome.IGNORED
    actor.flush()
    assert len(sink.posted) == posted_after_local
