from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from commands import durable_command_journal as journal_module  # noqa: E402
from commands.durable_command_journal import (  # noqa: E402
    AMBIGUOUS_APPLY_ERROR,
    CommandJournalPhase,
    DurableCommandJournal,
)


def test_final_ack_survives_process_restart(tmp_path: Path) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
    )

    journal.begin("command-1", "halt")
    journal.complete(
        "command-1",
        status="completed",
        error=False,
    )

    recovered = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
    ).recover()

    assert len(recovered) == 1
    record = recovered[0]
    assert record.command_id == "command-1"
    assert record.phase is CommandJournalPhase.ACK_QUEUED
    assert record.status == "completed"
    assert record.error is False


def test_applying_record_recovers_as_ambiguous_failed_ack(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
    )
    journal.begin("command-1", "close_all")

    recovered = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
    ).recover()

    assert len(recovered) == 1
    record = recovered[0]
    assert record.phase is CommandJournalPhase.ACK_QUEUED
    assert record.status == "failed"
    assert record.error == (
        f"{AMBIGUOUS_APPLY_ERROR}:close_all"
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    stored = payload["commands"]["command-1"]
    assert stored["phase"] == "ACK_QUEUED"
    assert stored["status"] == "failed"


def test_unapplied_mailbox_rejection_can_be_retried(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
    )
    journal.begin("command-1", "resume")

    assert journal.discard_unapplied("command-1") is True
    assert journal.get("command-1") is False

    record = journal.begin("command-1", "resume")

    assert record.phase is CommandJournalPhase.APPLYING


def test_command_identity_and_final_result_are_immutable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
    )
    journal.begin("command-1", "halt")

    with pytest.raises(
        ValueError,
        match="different command_type",
    ):
        journal.begin("command-1", "resume")

    journal.complete(
        "command-1",
        status="completed",
        error=False,
    )
    with pytest.raises(
        ValueError,
        match="result changed",
    ):
        journal.complete(
            "command-1",
            status="failed",
            error="different",
        )


def test_atomic_replace_failure_keeps_previous_durable_and_memory_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
    )
    journal.begin("command-1", "halt")
    before = path.read_bytes()

    def fail_replace(source: str, target: Path) -> None:
        del source, target
        raise OSError("disk full")

    monkeypatch.setattr(
        journal_module.os,
        "replace",
        fail_replace,
    )

    with pytest.raises(OSError, match="disk full"):
        journal.complete(
            "command-1",
            status="completed",
            error=False,
        )

    assert path.read_bytes() == before
    record = journal.get("command-1")
    assert record.phase is CommandJournalPhase.APPLYING
    assert list(tmp_path.glob("*.tmp")) == []
