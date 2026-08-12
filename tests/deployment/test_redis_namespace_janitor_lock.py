from __future__ import annotations

import fcntl
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import redis_namespace_janitor


def test_apply_lock_conflict_precedes_manifest_and_redis_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lock_path = tmp_path / "account-stall-operation.lock"
    monkeypatch.setenv("ACCOUNT_STALL_OPERATION_LOCK", str(lock_path))

    with lock_path.open("a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = redis_namespace_janitor.main(
            [
                "--redis-url",
                "redis://127.0.0.1:6379/0",
                "--legacy-prefix",
                "trader-legacy:",
                "--apply",
                "--safety-manifest",
                str(tmp_path / "must-not-be-read.json"),
            ]
        )

    assert result == 2
    assert "another account-stall operation holds" in capsys.readouterr().err
