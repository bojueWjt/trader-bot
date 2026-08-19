from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FEEDER_PATH = REPO_ROOT / "scripts" / "hermes_signal_feeder.py"


def _load_feeder():
    spec = importlib.util.spec_from_file_location(
        "test_signal_feeder_delivery",
        FEEDER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _signal(signal_id: str = "sig-1") -> dict[str, str]:
    return {
        "signal_id": signal_id,
        "received_at": "2026-08-06T00:00:00+00:00",
        "payload": json.dumps(
            {
                "source_channel_id": "channel-1",
                "source_channel_name": "channel",
                "source_message_id": "1",
                "raw_text": "BTC long",
            }
        ),
    }


def test_pending_job_survives_restart_and_blocks_redispatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    pending_path = tmp_path / "pending.json"
    monkeypatch.setattr(module, "PENDING_STATE", str(pending_path))
    monkeypatch.setattr(module, "build_prompt", lambda batch: "prompt")
    monkeypatch.setattr(module, "_latest_markdown_response", lambda job_id: None)
    dispatched: list[str] = []

    def run_hermes(prompt, name, dry_run, on_job_created=None):
        del prompt, dry_run
        dispatched.append(name)
        if on_job_created is not None:
            on_job_created("job-1")
        return "job-1"

    monkeypatch.setattr(module, "run_hermes", run_hermes)

    assert module.attempt_batch_delivery([_signal()], dry_run=False, now_ts=1000) == "pending"
    assert module.load_pending_delivery()["job_id"] == "job-1"

    restarted = _load_feeder()
    monkeypatch.setattr(restarted, "PENDING_STATE", str(pending_path))
    monkeypatch.setattr(restarted, "build_prompt", lambda batch: "prompt")
    monkeypatch.setattr(restarted, "_latest_markdown_response", lambda job_id: None)
    monkeypatch.setattr(
        restarted,
        "run_hermes",
        lambda *args, **kwargs: dispatched.append("duplicate") or "job-2",
    )

    assert restarted.attempt_batch_delivery([_signal()], dry_run=False, now_ts=1010) == "pending"
    assert dispatched == ["signal-sig-1"]


def test_dispatching_state_recovers_existing_job_without_redispatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    pending_path = tmp_path / "pending.json"
    monkeypatch.setattr(module, "PENDING_STATE", str(pending_path))
    pending = module._new_pending_delivery([_signal()])
    pending["status"] = "dispatching"
    module.save_pending_delivery(pending)
    monkeypatch.setattr(module, "find_existing_cron_job", lambda name: "job-existing")
    monkeypatch.setattr(module, "_latest_markdown_response", lambda job_id: None)
    dispatched: list[str] = []
    monkeypatch.setattr(
        module,
        "run_hermes",
        lambda *args, **kwargs: dispatched.append("duplicate") or "job-new",
    )

    result = module.attempt_batch_delivery([_signal()], dry_run=False, now_ts=1000)

    assert result == "pending"
    assert module.load_pending_delivery()["job_id"] == "job-existing"
    assert dispatched == []


def test_dispatch_lookup_failure_keeps_uncertain_job_pending(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    pending_path = tmp_path / "pending.json"
    monkeypatch.setattr(module, "PENDING_STATE", str(pending_path))
    pending = module._new_pending_delivery([_signal()])
    pending["status"] = "dispatching"
    module.save_pending_delivery(pending)
    monkeypatch.setattr(module, "find_existing_cron_job", lambda name: False)

    result = module.attempt_batch_delivery([_signal()], dry_run=False, now_ts=1000)

    assert result == "pending"
    assert module.load_pending_delivery()["attempts"] == 0


def test_create_failure_retries_after_confirmed_job_absence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    pending_path = tmp_path / "pending.json"
    monkeypatch.setattr(module, "PENDING_STATE", str(pending_path))
    monkeypatch.setattr(module, "build_prompt", lambda batch: "prompt")
    monkeypatch.setattr(module, "run_hermes", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "find_existing_cron_job", lambda name: None)

    result = module.attempt_batch_delivery([_signal()], dry_run=False, now_ts=1000)
    pending = module.load_pending_delivery()

    assert result == "retry"
    assert pending["attempts"] == 1
    assert pending["status"] == "retry"
    assert pending["retry_after"] == 1000 + module.BRAIN_RETRY_DELAY_SECONDS


def test_explicit_brain_failures_persist_attempts_and_eventually_skip(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    pending_path = tmp_path / "pending.json"
    monkeypatch.setattr(module, "PENDING_STATE", str(pending_path))
    monkeypatch.setattr(module, "build_prompt", lambda batch: "prompt")
    notices: list[str | None] = []
    job_ids = iter(("job-1", "job-2", "job-3"))

    def run_hermes(prompt, name, dry_run, on_job_created=None):
        del prompt, name, dry_run
        job_id = next(job_ids)
        if on_job_created is not None:
            on_job_created(job_id)
        return job_id

    monkeypatch.setattr(module, "run_hermes", run_hermes)
    monkeypatch.setattr(
        module,
        "_latest_markdown_response",
        lambda job_id: "API call failed after 3 retries: Connection error.",
    )
    monkeypatch.setattr(
        module,
        "notify_blocked",
        lambda sig, dry_run, reason=None: notices.append(reason),
    )

    results = [
        module.attempt_batch_delivery(
            [_signal()],
            dry_run=False,
            now_ts=1000 + index * 1000,
        )
        for index in range(module.MAX_ATTEMPTS)
    ]

    assert results == ["retry", "retry", "skip"]
    pending = module.load_pending_delivery()
    assert pending["attempts"] == module.MAX_ATTEMPTS
    assert pending["status"] == "skipped"
    assert len(notices) == 1


def test_successful_pending_response_is_committed_before_state_clears(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    pending_path = tmp_path / "pending.json"
    monkeypatch.setattr(module, "PENDING_STATE", str(pending_path))
    module.save_pending_delivery(
        {
            "batch_key": "2026-08-06T00:00:00+00:00|sig-1",
            "last_cursor": "2026-08-06T00:00:00+00:00|sig-1",
            "job_name": "signal-sig-1",
            "job_id": "job-1",
            "attempts": 0,
            "retry_after": 0,
        }
    )
    monkeypatch.setattr(module, "_latest_markdown_response", lambda job_id: "processed")
    monkeypatch.setattr(
        module,
        "load_cron_job_status",
        lambda _job_id: {
            "last_run_at": "2026-08-19T00:00:00+00:00",
            "last_status": "ok",
            "last_delivery_error": None,
        },
    )
    monkeypatch.setattr(module, "remove_cron_job", lambda _job_id: True)
    contexts: list[str] = []
    monkeypatch.setattr(
        module,
        "append_channel_context",
        lambda batch, response, now=None: contexts.append(response),
    )

    assert module.attempt_batch_delivery([_signal()], dry_run=False, now_ts=1000) == "success"
    pending = module.load_pending_delivery()
    assert pending["status"] == "succeeded"
    monkeypatch.setattr(module, "STATE", str(tmp_path / "cursor"))
    module.commit_batch_cursor(pending["last_cursor"])
    assert module.load_pending_delivery() is None
    assert contexts == ["processed"]


def test_find_existing_cron_job_uses_exact_signal_name(monkeypatch) -> None:
    module = _load_feeder()
    output = {
        "jobs": [
            {"id": "job-other", "name": "signal-sig-10", "status": "running"},
            {"id": "job-target", "name": "signal-sig-1", "status": "running"},
        ],
    }

    class Result:
        returncode = 0
        stdout = json.dumps(output)
        stderr = ""

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())

    assert module.find_existing_cron_job("signal-sig-1") == "job-target"
