from __future__ import annotations

import hashlib
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


MUTATION_SEAMS = (
    "begin",
    "complete",
    "mark_acked",
    "recover",
    "discard",
)


def _prepare_mutation(
    journal: DurableCommandJournal,
    mutation: str,
) -> None:
    if mutation == "begin":
        return
    journal.begin("command-1", "halt")
    if mutation == "mark_acked":
        journal.complete(
            "command-1",
            status="completed",
            error=False,
        )


def _invoke_mutation(
    journal: DurableCommandJournal,
    mutation: str,
) -> None:
    if mutation == "begin":
        journal.begin("command-1", "halt")
        return
    if mutation == "complete":
        journal.complete(
            "command-1",
            status="completed",
            error=False,
        )
        return
    if mutation == "mark_acked":
        journal.mark_acked("command-1")
        return
    if mutation == "recover":
        journal.recover()
        return
    if mutation == "discard":
        journal.discard_unapplied("command-1")
        return
    raise AssertionError(f"unknown mutation: {mutation}")


def _stored_record(path: Path) -> dict[str, object] | bool:
    payload = json.loads(path.read_text(encoding="utf-8"))
    commands = payload["commands"]
    assert isinstance(commands, dict)
    return commands.get("command-1", False)


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


def test_acked_result_survives_process_restart(tmp_path: Path) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
    )
    journal.begin("command-1", "halt")
    journal.complete(
        "command-1",
        status="failed",
        error="operator denied",
    )
    journal.mark_acked("command-1")

    recovered = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
    ).recover()

    assert len(recovered) == 1
    record = recovered[0]
    assert record.phase is CommandJournalPhase.ACKED
    assert record.status == "failed"
    assert record.error == "operator denied"
    assert record.acked_sequence == 1


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


@pytest.mark.parametrize("mutation", MUTATION_SEAMS)
def test_pre_replace_failure_rolls_back_every_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
    )
    _prepare_mutation(journal, mutation)
    before_record = journal.get("command-1")
    before_payload: bytes | bool = False
    if path.exists():
        before_payload = path.read_bytes()

    def fail_replace(source: str, target: Path) -> None:
        del source, target
        raise OSError("replace failed")

    monkeypatch.setattr(journal_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed") as caught:
        _invoke_mutation(journal, mutation)

    assert getattr(caught.value, "committed", False) is False
    assert journal.get("command-1") == before_record
    if before_payload is False:
        assert not path.exists()
    else:
        assert path.read_bytes() == before_payload


@pytest.mark.parametrize(
    ("mutation", "expected_phase"),
    [
        ("begin", CommandJournalPhase.APPLYING),
        ("complete", CommandJournalPhase.ACK_QUEUED),
        ("mark_acked", CommandJournalPhase.ACKED),
        ("recover", CommandJournalPhase.ACK_QUEUED),
        ("discard", False),
    ],
)
def test_post_replace_fsync_failure_keeps_committed_disk_and_memory_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_phase: CommandJournalPhase | bool,
) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
    )
    _prepare_mutation(journal, mutation)

    def fail_directory_fsync(directory: Path) -> None:
        del directory
        raise OSError("directory fsync failed")

    monkeypatch.setattr(
        journal_module,
        "_fsync_directory",
        fail_directory_fsync,
    )

    with pytest.raises(OSError, match="durability uncertain") as caught:
        _invoke_mutation(journal, mutation)

    assert getattr(caught.value, "committed", False) is True
    assert getattr(caught.value, "durability_uncertain", False) is True
    memory_record = journal.get("command-1")
    disk_record = _stored_record(path)
    if expected_phase is False:
        assert memory_record is False
        assert disk_record is False
        return
    assert memory_record.phase is expected_phase
    assert isinstance(disk_record, dict)
    assert disk_record["phase"] == expected_phase.value
    if mutation == "complete":
        assert memory_record.status == "completed"
        assert disk_record["status"] == "completed"
    if mutation == "recover":
        assert memory_record.status == "failed"
        assert disk_record["status"] == "failed"


def test_post_replace_fsync_failure_keeps_compacted_begin_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
        max_bytes=470,
    )
    for command_id in ("z-oldest", "a-middle"):
        journal.begin(command_id, "halt")
        journal.complete(
            command_id,
            status="completed",
            error=False,
        )
        journal.mark_acked(command_id)

    def fail_directory_fsync(directory: Path) -> None:
        del directory
        raise OSError("directory fsync failed")

    monkeypatch.setattr(
        journal_module,
        "_fsync_directory",
        fail_directory_fsync,
    )

    with pytest.raises(OSError, match="durability uncertain"):
        journal.begin("m-newest", "halt")

    assert journal.get("z-oldest") is False
    assert journal.get("a-middle").phase is CommandJournalPhase.ACKED
    assert journal.get("m-newest").phase is CommandJournalPhase.APPLYING
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload["commands"]) == {"a-middle", "m-newest"}


def test_acked_history_is_compacted_under_small_capacity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
        max_bytes=360,
    )

    for sequence in range(12):
        command_id = f"command-{sequence:02d}"
        journal.begin(command_id, "halt")
        journal.complete(
            command_id,
            status="completed",
            error=False,
        )
        journal.mark_acked(command_id)

    assert path.stat().st_size <= 360
    assert journal.get("command-11").phase is CommandJournalPhase.ACKED
    assert journal.begin("command-11", "halt").phase is (
        CommandJournalPhase.ACKED
    )


def test_compacted_journal_can_restart_and_continue(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
        max_bytes=360,
    )
    for sequence in range(6):
        command_id = f"command-{sequence:02d}"
        journal.begin(command_id, "resume")
        journal.complete(
            command_id,
            status="completed",
            error=False,
        )
        journal.mark_acked(command_id)

    recovered = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
        max_bytes=360,
    )
    recovered.begin("command-next", "resume")
    recovered.complete(
        "command-next",
        status="completed",
        error=False,
    )

    assert recovered.get("command-next").phase is (
        CommandJournalPhase.ACK_QUEUED
    )
    assert path.stat().st_size <= 360


def test_compaction_never_discards_applying_or_ack_queued(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
        max_bytes=700,
    )
    journal.begin("command-applying", "halt")
    journal.begin("command-queued", "resume")
    journal.complete(
        "command-queued",
        status="completed",
        error=False,
    )

    with pytest.raises(ValueError, match="max_bytes"):
        journal.begin("command-overflow", "close_all")

    recovered = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
        max_bytes=700,
    ).recover()
    by_id = {record.command_id: record for record in recovered}

    assert by_id["command-applying"].phase is CommandJournalPhase.ACK_QUEUED
    assert by_id["command-queued"].phase is CommandJournalPhase.ACK_QUEUED


def test_begin_reserves_terminal_capacity_before_side_effect(
    tmp_path: Path,
) -> None:
    journal = DurableCommandJournal(
        tmp_path / "command-journal.json",
        account_id="account-a",
        node_id="node-a",
        max_bytes=230,
    )

    with pytest.raises(ValueError, match="max_bytes"):
        journal.begin("command-1", "halt")

    assert journal.recover() == ()
    assert not journal.path.exists()


def test_terminal_transitions_fit_the_reserved_capacity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
        max_bytes=360,
    )

    journal.begin("command-1", "halt")
    original_error = "exchange response " + ("x" * 512)
    completed = journal.complete(
        "command-1",
        status="completed",
        error=original_error,
    )

    assert completed.phase is CommandJournalPhase.ACK_QUEUED
    assert isinstance(completed.error, str)
    assert "[truncated:sha256=" in completed.error
    assert hashlib.sha256(original_error.encode("utf-8")).hexdigest() in (
        completed.error
    )
    assert completed.error is not False
    persisted_error = completed.error

    recovered_journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
        max_bytes=360,
    )
    recovered = recovered_journal.recover()
    assert recovered[0].error == persisted_error

    acked = recovered_journal.mark_acked("command-1")
    assert acked.phase is CommandJournalPhase.ACKED
    assert path.stat().st_size <= 360


def test_acked_sequence_capacity_is_reserved_before_begin(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
        max_bytes=304,
    )

    journal.begin("command-1", "halt")
    journal.complete(
        "command-1",
        status="completed",
        error="exchange-" + ("e" * 512),
    )
    acked = journal.mark_acked("command-1")

    assert acked.phase is CommandJournalPhase.ACKED
    assert path.stat().st_size <= 304

    recovered = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
        max_bytes=304,
    ).recover()
    assert recovered[0].phase is CommandJournalPhase.ACKED


def test_begin_rejects_capacity_without_acked_sequence_reservation(
    tmp_path: Path,
) -> None:
    journal = DurableCommandJournal(
        tmp_path / "command-journal.json",
        account_id="account-a",
        node_id="node-a",
        max_bytes=303,
    )

    with pytest.raises(ValueError, match="max_bytes"):
        journal.begin("command-1", "halt")

    assert journal.recover() == ()
    assert not journal.path.exists()


def test_invalid_command_ack_status_is_rejected(
    tmp_path: Path,
) -> None:
    journal = DurableCommandJournal(
        tmp_path / "command-journal.json",
        account_id="account-a",
        node_id="node-a",
    )
    journal.begin("command-1", "halt")

    with pytest.raises(
        ValueError,
        match="accepted, completed, or failed",
    ):
        journal.complete(
            "command-1",
            status="completed-" + ("s" * 128),
            error=False,
        )

    assert journal.get("command-1").phase is CommandJournalPhase.APPLYING


def test_legacy_acked_records_migrate_with_complete_results(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command-journal.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "account_id": "account-a",
                "node_id": "node-a",
                "commands": {
                    "z-command": {
                        "command_id": "z-command",
                        "command_type": "halt",
                        "phase": "ACKED",
                        "status": "completed",
                        "error": False,
                    },
                    "a-command": {
                        "command_id": "a-command",
                        "command_type": "resume",
                        "phase": "ACKED",
                        "status": "failed",
                        "error": "operator denied",
                    },
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    first = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
    ).recover()
    second = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
    ).recover()

    first_by_id = {record.command_id: record for record in first}
    second_by_id = {record.command_id: record for record in second}
    assert first_by_id["z-command"].status == "completed"
    assert first_by_id["z-command"].error is False
    assert first_by_id["a-command"].status == "failed"
    assert first_by_id["a-command"].error == "operator denied"
    assert first_by_id["z-command"].acked_sequence == (
        second_by_id["z-command"].acked_sequence
    )
    assert first_by_id["a-command"].acked_sequence == (
        second_by_id["a-command"].acked_sequence
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.1"


@pytest.mark.parametrize(
    "phase",
    [
        CommandJournalPhase.ACKED,
        CommandJournalPhase.ACK_QUEUED,
    ],
)
def test_legacy_long_error_is_bounded_before_capacity_migration(
    tmp_path: Path,
    phase: CommandJournalPhase,
) -> None:
    path = tmp_path / "command-journal.json"
    original_error = "legacy exchange error " + ("x" * 2048)
    record = {
        "command_id": "command-1",
        "command_type": "halt",
        "phase": phase.value,
        "status": "failed",
        "error": original_error,
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "account_id": "account-a",
                "node_id": "node-a",
                "commands": {"command-1": record},
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    recovered = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
        max_bytes=500,
    ).recover()

    assert len(recovered) == 1
    migrated = recovered[0]
    assert migrated.command_id == "command-1"
    assert migrated.phase is phase
    assert migrated.status == "failed"
    assert isinstance(migrated.error, str)
    assert "[truncated:sha256=" in migrated.error
    assert hashlib.sha256(original_error.encode("utf-8")).hexdigest() in (
        migrated.error
    )
    assert path.stat().st_size <= 500


def test_legacy_ack_queued_fails_closed_when_bounded_record_cannot_fit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command-journal.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "account_id": "account-a",
                "node_id": "node-a",
                "commands": {
                    "command-1": {
                        "command_id": "command-1",
                        "command_type": "halt",
                        "phase": "ACK_QUEUED",
                        "status": "failed",
                        "error": "x" * 2048,
                    }
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="before terminal state can be reserved",
    ):
        DurableCommandJournal(
            path,
            account_id="account-a",
            node_id="node-a",
            max_bytes=250,
        )


def test_acked_eviction_uses_persisted_sequence_across_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command-journal.json"
    journal = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
        max_bytes=470,
    )
    for command_id in ("z-oldest", "a-middle"):
        journal.begin(command_id, "halt")
        journal.complete(
            command_id,
            status="completed",
            error=False,
        )
        journal.mark_acked(command_id)

    restarted = DurableCommandJournal(
        path,
        account_id="account-a",
        node_id="node-a",
        max_bytes=470,
    )
    restarted.begin("m-newest", "halt")
    restarted.complete(
        "m-newest",
        status="completed",
        error=False,
    )
    restarted.mark_acked("m-newest")

    assert restarted.get("z-oldest") is False
    assert restarted.get("a-middle").phase is CommandJournalPhase.ACKED
    assert restarted.get("m-newest").phase is CommandJournalPhase.ACKED

    payload = json.loads(path.read_text(encoding="utf-8"))
    middle = payload["commands"]["a-middle"]
    newest = payload["commands"]["m-newest"]
    assert middle["acked_sequence"] < newest["acked_sequence"]
