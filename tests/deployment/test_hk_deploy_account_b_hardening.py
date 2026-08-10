from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "hk-deploy-account-b-hardening.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _function_text(name: str) -> str:
    text = _text()
    matches = list(
        re.finditer(r"^[a-z_][a-z0-9_]*\(\) \{$", text, re.MULTILINE)
    )
    for index, match in enumerate(matches):
        if match.group(0) != f"{name}() {{":
            continue
        end = len(text)
        if index + 1 < len(matches):
            end = matches[index + 1].start()
        return text[match.start():end].rstrip() + "\n"
    raise AssertionError(f"function is missing: {name}")


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_shell(
    tmp_path: Path,
    shell: str,
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if environment:
        merged.update(environment)
    return subprocess.run(
        ["bash", "-c", shell],
        cwd=tmp_path,
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_targets_only_the_stopped_account_b_runtime() -> None:
    text = _text()

    assert 'NODE_CONTAINER="trader-v3-node-b"' in text
    assert 'PEER_CONTAINER="trader-v3-node-a"' in text
    assert 'NODE_PORT="${NODE_PORT:-8082}"' in text
    assert 'TARGET_IMAGE="trader-bot/nautilus-node:b10-verify"' in text
    assert (
        'OPERATION_LOCK="${OPERATION_LOCK:-/var/lock/'
        'trader-v3-account-stall-operation.lock}"'
    ) in text
    assert 'flock -n 9 || die "another operation holds $OPERATION_LOCK"' in text
    assert "docker start" not in text
    assert 'bash "$RECREATE_TARGET"' in text
    assert "NAUTILUS_INITIAL_TRADING_STATE=HALTED" in text


def test_script_is_manifest_driven_and_rejects_current_head_mount_drift() -> None:
    text = _text()

    assert "account_b_peer_contract" in text
    assert "peer_files" in text
    assert "observability_delta" in text
    assert 'EXPECTED_OBSERVABILITY_BUNDLE="control_plane_session.py"' in text
    assert (
        'EXPECTED_OBSERVABILITY_TARGET="/app/runtime/'
        'control_plane_session.py"'
    ) in text
    assert "PATCH_MOUNT_TARGETS" in text
    assert "bundle and recreate mount plans differ" in text
    assert "account-a runtime mount set differs from peer manifest" in text
    assert "unexpected account-a runtime hash" in text
    assert "runtime_resource_contract.py" not in text


def test_script_gates_exchange_risk_and_stopped_baseline_before_mutation() -> None:
    text = _text()
    main = text.index("main() {")

    stopped = text.index("verify_stopped_baseline", main)
    exchange = text.index('verify_exchange_zero "before"', stopped)
    risk = text.index("verify_risk_halted", exchange)
    backup = text.index("create_backup", risk)
    install = text.index("install_container_patches", backup)

    assert stopped < exchange < risk < backup < install
    assert "account-b container must be stopped" in text
    assert "account-b exchange mirror is stale" in text
    assert "account-b exchange state is not zero" in text
    assert "account-b BTC/ETH/SOL risk modes are not HALTED" in text
    assert "account-b node state is not empty" in text
    assert "account-b Redis namespace is not empty" in text
    assert "account-b pending runtime work is not empty" in text


def test_script_backs_up_runtime_state_and_installs_patches_atomically() -> None:
    text = _text()

    assert (
        'cp "$TEMP_DIR/container-inspect.json" \\\n    '
        '"$BACKUP_ROOT/container-inspect.json"'
    ) in text
    assert 'backup_target "$RECREATE_TARGET"' in text
    assert 'backup_target "$GENERATOR_TARGET"' in text
    assert 'backup_target "$PATCH_DIR/$bundle_path"' in text
    assert 'snapshot_state_metadata "$STATE_DIR"' in text
    assert 'cp -a "$STATE_DIR" "$BACKUP_ROOT/node-state"' in text
    assert "atomic_install()" in text
    assert 'mv -fT -- "$temporary" "$target"' in text


def test_script_verifies_halted_health_identity_mounts_and_memory() -> None:
    text = _text()

    assert "account-b /ready did not report HALTED" in text
    assert "heartbeat_stale must be false" in text
    assert "operational_state must be ONLINE" in text
    assert "admission_eligible must be false" in text
    assert "reconciliation_state must be healthy" in text
    assert "process_liveness must be true" in text
    assert "container target SHA256 mismatch" in text
    assert "deleted inode mount detected" in text
    assert "account-b Docker memory limit mismatch" in text
    assert "account-b Docker memory-swap limit mismatch" in text
    assert "account-b Docker restart count is not zero" in text
    assert "account-b structured control-plane logs are missing" in text
    assert 'verify_exchange_zero "after"' in text


def test_failure_handler_prints_an_executable_rollback_command() -> None:
    text = _text()

    assert 'chmod 0700 "$ROLLBACK_PATH"' in text
    assert 'echo "ROLLBACK: bash $ROLLBACK_PATH" >&2' in text
    assert 'echo "== rollback: bash $ROLLBACK_PATH"' in text


def test_stopped_preflight_rejects_a_running_account_b(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    node_inspect = tmp_path / "node.json"
    peer_inspect = tmp_path / "peer.json"
    node_inspect.write_text(
        json.dumps(
            [
                {
                    "State": {
                        "Running": True,
                        "Status": "running",
                        "ExitCode": 0,
                        "OOMKilled": False,
                        "FinishedAt": "",
                    },
                    "Config": {
                        "Image": "trader-bot/nautilus-node:b10-verify",
                    },
                    "HostConfig": {
                        "PortBindings": {
                            "8082/tcp": [{"HostPort": "8082"}],
                        },
                    },
                    "Mounts": [{} for _ in range(17)],
                }
            ]
        ),
        encoding="utf-8",
    )
    peer_inspect.write_text(
        json.dumps(
            [
                {
                    "State": {"Running": True},
                    "Config": {
                        "Image": "trader-bot/nautilus-node:b10-verify",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -eu
if [ "$2" = "trader-v3-node-b" ]; then
  cat "$NODE_INSPECT"
else
  cat "$PEER_INSPECT"
fi
""",
    )
    shell = (
        "set -Eeuo pipefail\n"
        f'PATH="{fake_bin}:$PATH"\n'
        f'TEMP_DIR="{tmp_path}"\n'
        'NODE_CONTAINER="trader-v3-node-b"\n'
        'PEER_CONTAINER="trader-v3-node-a"\n'
        'TARGET_IMAGE="trader-bot/nautilus-node:b10-verify"\n'
        'TARGET_IMAGE_ID="sha256:test-image"\n'
        'NODE_PORT="8082"\n'
        f'CONTAINER_TSV="{tmp_path / "container.tsv"}"\n'
        f'T="{tmp_path / "trader"}"\n'
        f"{_function_text('verify_stopped_baseline')}\n"
        "verify_stopped_baseline\n"
    )
    result = _run_shell(
        tmp_path,
        shell,
        environment={
            "NODE_INSPECT": str(node_inspect),
            "PEER_INSPECT": str(peer_inspect),
        },
    )

    assert result.returncode != 0
    assert "account-b container must be stopped" in result.stderr


@pytest.mark.parametrize(
    ("row", "expected"),
    (
        (
            (
                "2026-08-10T12:00:00.000000Z\ttrue\t"
                '{"positions":[],"open_orders":[],"algo_orders":[]}'
            ),
            "account-b exchange mirror is stale",
        ),
        (
            (
                "2026-08-10T12:00:00.000000Z\tfalse\t"
                '{"positions":[{"symbol":"BTCUSDT"}],'
                '"open_orders":[],"algo_orders":[]}'
            ),
            "account-b exchange state is not zero",
        ),
    ),
)
def test_exchange_preflight_fails_closed(
    tmp_path: Path,
    row: str,
    expected: str,
) -> None:
    fake_bin = tmp_path / "bin"
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$EXCHANGE_ROW"
""",
    )
    shell = (
        "set -Eeuo pipefail\n"
        f'PATH="{fake_bin}:$PATH"\n'
        f'TEMP_DIR="{tmp_path}"\n'
        'POSTGRES_CONTAINER="trader-v3-postgres"\n'
        'POSTGRES_USER="postgres"\n'
        'POSTGRES_DB="trader"\n'
        'ACCOUNT_ID="account-b"\n'
        f"{_function_text('exchange_snapshot')}\n"
        f"{_function_text('verify_exchange_zero')}\n"
        'verify_exchange_zero "before"\n'
    )
    result = _run_shell(
        tmp_path,
        shell,
        environment={"EXCHANGE_ROW": row},
    )

    assert result.returncode != 0
    assert expected in result.stderr


def test_risk_preflight_requires_all_three_halted_modes(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -eu
printf 'BTCUSDT\tHALTED\nETHUSDT\tACTIVE\nSOLUSDT\tHALTED\n'
""",
    )
    shell = (
        "set -Eeuo pipefail\n"
        f'PATH="{fake_bin}:$PATH"\n'
        f'TEMP_DIR="{tmp_path}"\n'
        'POSTGRES_CONTAINER="trader-v3-postgres"\n'
        'POSTGRES_USER="postgres"\n'
        'POSTGRES_DB="trader"\n'
        'ACCOUNT_ID="account-b"\n'
        f"{_function_text('verify_risk_halted')}\n"
        "verify_risk_halted\n"
    )
    result = _run_shell(tmp_path, shell)

    assert result.returncode != 0
    assert "account-b BTC/ETH/SOL risk modes are not HALTED" in result.stderr


def test_rollback_recreates_halted_and_never_starts_the_legacy_container() -> None:
    text = _text()
    marker = "cat >\"$ROLLBACK_PATH\" <<'ROLLBACK'\n"
    start = text.index(marker) + len(marker)
    end = text.index("\nROLLBACK\n", start)
    rollback = text[start:end]

    assert "docker start" not in rollback
    assert 'docker rm -f "$NODE_CONTAINER"' in rollback
    assert 'grep -Fq "NAUTILUS_INITIAL_TRADING_STATE=HALTED"' in rollback
    assert 'bash "$RECREATE_TARGET"' in rollback
    assert 'mv "$STATE_DIR" "$FAILED_STATE"' in rollback
    assert 'cp -a "$BACKUP_ROOT/node-state" "$STATE_DIR"' in rollback


def test_rollback_restores_patch_and_state_then_recreates_halted(
    tmp_path: Path,
) -> None:
    trader_root = tmp_path / "trader"
    backup_root = trader_root / "backups" / "account-b-hardening-test"
    backup_file = (
        backup_root
        / "files"
        / str(trader_root / "container-patches" / "runtime.py").lstrip("/")
    )
    backup_file.parent.mkdir(parents=True)
    backup_file.write_text("old runtime\n", encoding="utf-8")
    patch_target = trader_root / "container-patches" / "runtime.py"
    patch_target.parent.mkdir(parents=True)
    patch_target.write_text("new runtime\n", encoding="utf-8")
    index = (
        f"present\t{patch_target}\t"
        f"{backup_file.relative_to(backup_root).as_posix()}\n"
    )
    (backup_root / "index.tsv").write_text(index, encoding="utf-8")

    state_dir = trader_root / "node-state" / "b"
    state_dir.mkdir(parents=True)
    (state_dir / "journal.json").write_text("new state\n", encoding="utf-8")
    backup_state = backup_root / "node-state"
    backup_state.mkdir()
    (backup_state / "journal.json").write_text(
        "old state\n",
        encoding="utf-8",
    )
    recreate = trader_root / "recreate-trader-v3-node-b.sh"
    _write_executable(
        recreate,
        """#!/usr/bin/env bash
set -Eeuo pipefail
# NAUTILUS_INITIAL_TRADING_STATE=HALTED
docker rm -f trader-v3-node-b 2>/dev/null || true
docker run -d --name trader-v3-node-b \
  -e NAUTILUS_INITIAL_TRADING_STATE=HALTED \
  trader-bot/nautilus-node:b10-verify
""",
    )

    text = _text()
    marker = "cat >\"$ROLLBACK_PATH\" <<'ROLLBACK'\n"
    start = text.index(marker) + len(marker)
    end = text.index("\nROLLBACK\n", start)
    rollback = text[start:end] + "\n"
    rollback_path = backup_root / "rollback.sh"
    _write_executable(rollback_path, rollback)

    checksum_lines = []
    for path in sorted(backup_root.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        relative = path.relative_to(backup_root).as_posix()
        checksum_lines.append(f"{digest}  {relative}")
    (backup_root / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    command_log = tmp_path / "commands.log"
    _write_executable(
        fake_bin / "flock",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -eu
printf 'docker %s\n' "$*" >>"$COMMAND_LOG"
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
set -eu
printf '{"ready":true,"trading_state":"HALTED"}'
""",
    )
    _write_executable(
        fake_bin / "mv",
        """#!/usr/bin/env bash
set -eu
if [ "${1:-}" = "-fT" ]; then
  shift
fi
if [ "${1:-}" = "--" ]; then
  shift
fi
/bin/mv "$@"
""",
    )
    result = subprocess.run(
        ["bash", str(rollback_path)],
        cwd=trader_root,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "COMMAND_LOG": str(command_log),
            "OPERATION_LOCK": str(tmp_path / "operation.lock"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert patch_target.read_text(encoding="utf-8") == "old runtime\n"
    assert (state_dir / "journal.json").read_text(
        encoding="utf-8"
    ) == "old state\n"
    failed_states = list(backup_root.glob("failed-node-state-*"))
    assert len(failed_states) == 1
    assert (failed_states[0] / "journal.json").read_text(
        encoding="utf-8"
    ) == "new state\n"
    commands = command_log.read_text(encoding="utf-8")
    assert "docker start" not in commands
    assert "docker run" in commands


def test_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
