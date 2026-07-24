from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FEEDER_PATH = REPO_ROOT / ".live-mirror" / "scripts" / "hermes_signal_feeder.py"


def _load_feeder():
    spec = importlib.util.spec_from_file_location(
        "test_hermes_signal_feeder",
        FEEDER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _signal(signal_id="sig-1"):
    return {
        "signal_id": signal_id,
        "received_at": "2026-07-23T13:27:00+00:00",
        "payload": "{}",
    }


def test_timeout_counts_as_attempt_and_eventually_skips(monkeypatch):
    module = _load_feeder()
    attempts = {}
    retry_after = {}
    timeout_keys = set()
    notices = []

    monkeypatch.setattr(module, "find_existing_cron_job", lambda name: None)

    def timeout_delivery(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="hermes cron run", timeout=240)

    monkeypatch.setattr(module, "deliver_batch", timeout_delivery)
    monkeypatch.setattr(
        module,
        "notify_blocked",
        lambda sig, dry_run, reason=None: notices.append(reason),
    )

    results = [
        module.attempt_batch_delivery(
            [_signal()],
            dry_run=False,
            attempts=attempts,
            retry_after=retry_after,
            timeout_keys=timeout_keys,
            now_ts=1_000 + index * 1_000,
        )
        for index in range(module.MAX_ATTEMPTS)
    ]

    key = "2026-07-23T13:27:00+00:00|sig-1"
    assert results == ["retry", "retry", "skip"]
    assert attempts.get(key) is None
    assert key not in timeout_keys
    assert len(notices) == 1
    assert "双 job 风险" in notices[0]


def test_retry_after_timeout_reuses_existing_signal_job(monkeypatch):
    module = _load_feeder()
    attempts = {}
    retry_after = {}
    timeout_keys = set()
    calls = []

    def first_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="hermes cron run", timeout=240)

    monkeypatch.setattr(module, "deliver_batch", first_timeout)
    first = module.attempt_batch_delivery(
        [_signal()],
        dry_run=False,
        attempts=attempts,
        retry_after=retry_after,
        timeout_keys=timeout_keys,
        now_ts=1_000,
    )

    monkeypatch.setattr(module, "find_existing_cron_job", lambda name: "job-existing")

    def resumed_delivery(batch, dry_run, existing_job_id=None, **kwargs):
        calls.append(existing_job_id)
        return True

    monkeypatch.setattr(module, "deliver_batch", resumed_delivery)
    second = module.attempt_batch_delivery(
        [_signal()],
        dry_run=False,
        attempts=attempts,
        retry_after=retry_after,
        timeout_keys=timeout_keys,
        now_ts=2_000,
    )

    assert first == "retry"
    assert second == "success"
    assert calls == ["job-existing"]


def test_retry_dispatches_again_only_when_signal_job_is_absent(monkeypatch):
    module = _load_feeder()
    key = "2026-07-23T13:27:00+00:00|sig-1"
    attempts = {key: 1}
    retry_after = {}
    timeout_keys = {key}
    calls = []
    logs = []

    monkeypatch.setattr(module, "find_existing_cron_job", lambda name: None)
    monkeypatch.setattr(
        module,
        "deliver_batch",
        lambda batch, dry_run, existing_job_id=None, **kwargs:
        calls.append(existing_job_id) or True,
    )
    monkeypatch.setattr(module, "log", logs.append)

    result = module.attempt_batch_delivery(
        [_signal()],
        dry_run=False,
        attempts=attempts,
        retry_after=retry_after,
        timeout_keys=timeout_keys,
        now_ts=2_000,
    )

    assert result == "success"
    assert calls == [None]
    assert any("双 job 风险" in line for line in logs)


def test_find_existing_cron_job_uses_exact_signal_name(monkeypatch):
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
