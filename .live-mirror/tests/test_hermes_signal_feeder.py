from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
FEEDER_PATH = REPO_ROOT / ".live-mirror" / "scripts" / "hermes_signal_feeder.py"


@pytest.fixture(autouse=True)
def _clear_trading_db_path_env(monkeypatch):
    for name in (
        "TRADER_TRADING_DB_PATH",
        "WATCHER_TRADING_DB",
        "TRADING_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def _load_feeder(name: str = "test_hermes_signal_feeder"):
    spec = importlib.util.spec_from_file_location(
        name,
        FEEDER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_trading_db_path_resolver_accepts_canonical_and_legacy_aliases():
    module = _load_feeder("test_live_mirror_feeder_db_path")

    assert (
        module.resolve_trading_db_path({})
        == module.DEFAULT_TRADING_DB_PATH
    )
    assert module.resolve_trading_db_path(
        {"TRADING_DB_PATH": "/data/watcher-trading.db"}
    ) == "/data/watcher-trading.db"
    assert module.resolve_trading_db_path(
        {
            "TRADER_TRADING_DB_PATH": "/data/watcher-trading.db",
            "WATCHER_TRADING_DB": "/data/watcher-trading.db",
            "TRADING_DB_PATH": "/data/watcher-trading.db",
        }
    ) == "/data/watcher-trading.db"


def test_trading_db_path_conflict_fails_closed(monkeypatch):
    monkeypatch.setenv("TRADER_TRADING_DB_PATH", "/data/a.db")
    monkeypatch.setenv("TRADING_DB_PATH", "/data/b.db")

    try:
        _load_feeder("test_live_mirror_feeder_db_conflict")
    except RuntimeError as exc:
        assert "conflicting trading DB path" in str(exc)
        return
    raise AssertionError("conflicting trading DB path was accepted")


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


def test_prompt_uses_canonical_route_and_requires_multiplier_schema(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "watcher-trading.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE account_configs (
            account_id TEXT PRIMARY KEY,
            account_type TEXT NOT NULL DEFAULT 'main',
            parent_account_id TEXT NOT NULL DEFAULT '',
            execution_account_id TEXT NOT NULL,
            risk_capital_multiplier REAL NOT NULL
        );
        CREATE TABLE channel_routing (
            channel_id TEXT PRIMARY KEY,
            target_account_id TEXT NOT NULL
        );
        INSERT INTO account_configs (
            account_id,
            execution_account_id,
            risk_capital_multiplier
        ) VALUES ('credential-a', 'account-a', 1.8);
        INSERT INTO channel_routing (channel_id, target_account_id)
        VALUES ('-1002136478186', 'credential-a');
        """
    )
    conn.close()
    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(db_path))
    monkeypatch.setattr(module, "V3_MEDIA", str(tmp_path / "media"))
    signal = _signal("sig-route")
    signal["payload"] = json.dumps(
        {
            "source_channel_id": "-1002136478186",
            "source_channel_name": "route-channel",
            "source_message_id": "7001",
            "raw_text": "BTC long",
        }
    )

    prompt = module.build_prompt(signal)

    assert "固定执行账号: account-a" in prompt
    assert "风险资金系数(审计): 1.8" in prompt
    assert "交易ref: tg-sig-c1002136478186-m7001" in prompt
    assert "必须使用 --account account-a" in prompt
    assert "Hermes 无权选择或改写" in prompt


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
