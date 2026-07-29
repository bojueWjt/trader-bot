from __future__ import annotations

from pathlib import Path

from order_management.recovery import RecoveryJob, decide_recovery_action
from persistence.execution_state import ExecutionStateStore


def test_recovery_does_not_resubmit_when_order_already_accepted() -> None:
    decision = decide_recovery_action(
        RecoveryJob(
            execution_job_id="job-1",
            status="claimed",
            client_order_id="client-1",
            payload={"submit": {"state": "unknown"}},
        ),
        local_orders=[{"client_order_id": "client-1", "status": "accepted"}],
        reconciliation_clean=True,
    )

    assert decision.action == "do_not_resubmit"
    assert decision.next_status == "executing"


def test_unknown_job_requires_reconciliation_before_deciding() -> None:
    decision = decide_recovery_action(
        RecoveryJob(
            execution_job_id="job-1",
            status="claimed",
            client_order_id="client-1",
            payload={"submit": {"state": "unknown"}},
        ),
        local_orders=[],
        reconciliation_clean=None,
    )

    assert decision.action == "reconcile_first"
    assert decision.next_status == "needs_review"


def test_persistence_store_recovers_cursor_spool_and_accepted_jobs_idempotently(tmp_path: Path) -> None:
    store = ExecutionStateStore(tmp_path / "execution-state.json")

    store.save_cursor("orders", "cursor-1")
    store.remember_accepted_order("job-1", "client-1")
    store.spool_job({"execution_job_id": "job-1", "payload": {"n": 1}})
    store.spool_job({"execution_job_id": "job-1", "payload": {"n": 1}})

    restored = ExecutionStateStore(tmp_path / "execution-state.json")

    assert restored.load_cursor("orders") == "cursor-1"
    assert restored.was_order_accepted("job-1", "client-1") is True
    assert restored.load_spool() == [{"execution_job_id": "job-1", "payload": {"n": 1}}]

