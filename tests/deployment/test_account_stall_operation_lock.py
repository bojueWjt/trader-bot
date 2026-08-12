from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "hk-deploy-20260803.sh"
REDIS_REBASELINE = REPO_ROOT / "scripts" / "hk-redis-rebaseline.sh"
JANITOR = REPO_ROOT / "scripts" / "redis_namespace_janitor.py"
ROLLOUT = REPO_ROOT / "scripts" / "reviewed_release_rollout.py"


def _function_source(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def test_deploy_lock_fd_and_token_reach_python_mutation_tool(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "account-stall-operation.lock"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    flock = fake_bin / "flock"
    flock.write_text(
        """#!/usr/bin/env python3
import fcntl
import sys

arguments = list(sys.argv[1:])
if arguments and arguments[0] == "-n":
    arguments.pop(0)
if arguments != ["9"]:
    raise SystemExit(2)
fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
""",
        encoding="utf-8",
    )
    flock.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "ACCOUNT_STALL_OPERATION_LOCK": str(lock_path),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "SCRIPTS_ROOT": str(REPO_ROOT / "scripts"),
            "TEST_PYTHON": sys.executable,
            "TRADER_RELEASE_TEST_MODE": "1",
            "TRADER_TEST_ACCOUNT_STALL_OPERATION_LOCK": str(lock_path),
        }
    )
    source = (
        "set -Eeuo pipefail\n"
        + _function_source(
            DEPLOY,
            "acquire_account_stall_operation_lock",
        )
        + """
acquire_account_stall_operation_lock
"$TEST_PYTHON" - <<'PY'
import os
import sys

sys.path.insert(0, os.environ["SCRIPTS_ROOT"])
from reviewed_release_rollout import AccountStallOperationLock

with AccountStallOperationLock(enabled=True):
    print("PYTHON_MUTATION_LOCK_OK")
PY
"""
    )

    result = subprocess.run(
        ["bash", "-c", source],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "PYTHON_MUTATION_LOCK_OK" in result.stdout


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("flock") is None,
    reason="requires Linux flock interoperability",
)
def test_deploy_redis_janitor_and_rollout_share_one_operation_lock(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "account-stall-operation.lock"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >>"$FAKE_DOCKER_LOG"\n'
        "exit 99\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = os.environ.copy()
    env.pop("ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP", None)
    env.pop("ACCOUNT_STALL_OPERATION_LOCK_FD", None)
    env.pop("ACCOUNT_STALL_OPERATION_LOCK_TOKEN", None)
    env.update(
        {
            "ACCOUNT_STALL_OPERATION_LOCK": str(lock_path),
            "FAKE_DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "REDIS_CAPACITY_PLANNER": str(
                REPO_ROOT / "scripts" / "redis_capacity_config.py"
            ),
            "TRADER_ROOT": str(tmp_path / "trader"),
        }
    )
    deploy_source = (
        "set -Eeuo pipefail\n"
        + _function_source(
            DEPLOY,
            "acquire_account_stall_operation_lock",
        )
        + "\nacquire_account_stall_operation_lock\n"
    )
    contenders = {
        "deploy": ["bash", "-c", deploy_source],
        "redis": [
            "bash",
            "-c",
            (
                f"source {REDIS_REBASELINE!s}; "
                "require_root() { return 0; }; "
                "main"
            ),
        ],
        "janitor": [
            sys.executable,
            str(JANITOR),
            "--redis-url",
            "redis://127.0.0.1:6379/0",
            "--legacy-prefix",
            "trader-legacy:",
            "--apply",
            "--safety-manifest",
            str(tmp_path / "must-not-be-read.json"),
        ],
        "rollout": [
            sys.executable,
            str(ROLLOUT),
            "--database-url",
            "postgresql://fixture.invalid/trader",
            "advance",
            "--release-id",
            "release-fixture",
            "--to-phase",
            "aborted",
            "--actor",
            "test",
            "--reason",
            "test",
            "--idempotency-key",
            "abort:fixture",
        ],
    }

    with lock_path.open("a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        results = {
            name: subprocess.run(
                command,
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            for name, command in contenders.items()
        }

    for name, result in results.items():
        assert result.returncode != 0, name
        assert "another account-stall operation holds" in result.stderr, (
            name,
            result.stderr,
        )
    assert not docker_log.exists()
