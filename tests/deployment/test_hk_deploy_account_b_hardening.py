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


def _materialize_recreate_validator(tmp_path: Path) -> Path:
    validator = tmp_path / "validate-recreate.py"
    shell = (
        "set -Eeuo pipefail\n"
        f'RECREATE_VALIDATOR="{validator}"\n'
        f"{_function_text('write_recreate_validator')}\n"
        "write_recreate_validator\n"
    )
    result = _run_shell(tmp_path, shell)
    assert result.returncode == 0, result.stdout + result.stderr
    return validator


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
    assert "if set(mounts) != expected_mount_targets:" in text
    assert "unexpected account-a runtime hash" in text
    assert 'EXPECTED_GENERATOR_SHA256="0a0026cd307080c57037b94726f835339' in text
    assert "account-b recreate generator hash is invalid" in text
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


def test_script_isolates_account_b_patches_and_installs_atomically() -> None:
    text = _text()

    assert (
        'cp "$TEMP_DIR/container-inspect.json" \\\n    '
        '"$BACKUP_ROOT/container-inspect.json"'
    ) in text
    assert 'backup_target "$RECREATE_TARGET"' in text
    assert 'backup_target "$GENERATOR_TARGET"' in text
    assert 'backup_target "$PEER_PATCH_DIR/$bundle_path"' not in text
    assert 'B_RELEASE_ROOT="$T/account-b-releases/$RELEASE_COMMIT"' in text
    assert 'mkdir -p "$B_PATCH_DIR"' in text
    assert '"$B_PATCH_DIR/$bundle_path"' in text
    assert '"$B_RELEASE_ROOT" \\\n    "$OPERATION_LOCK"' in text
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
    assert "account-b runtime mount set differs from manifest" in text
    assert "account-b config mount source changed" in text
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


def test_peer_runtime_rejects_any_extra_account_a_mount(
    tmp_path: Path,
) -> None:
    trader_root = tmp_path / "trader"
    patch_dir = trader_root / "container-patches"
    state_dir = trader_root / "node-state" / "a"
    config = trader_root / "node-a.hk.json"
    runtime = patch_dir / "runtime.py"
    digest = hashlib.sha256(b"runtime").hexdigest()
    manifest = tmp_path / "peer.tsv"
    manifest.write_text(
        f"runtime.py\t/app/runtime/runtime.py\t{digest}\n",
        encoding="utf-8",
    )
    inspect_path = tmp_path / "peer-inspect.json"
    inspect_path.write_text(
        json.dumps(
            [
                {
                    "Mounts": [
                        {
                            "Source": str(runtime),
                            "Destination": "/app/runtime/runtime.py",
                            "RW": False,
                        },
                        {
                            "Source": str(state_dir),
                            "Destination": "/state",
                            "RW": True,
                        },
                        {
                            "Source": str(config),
                            "Destination": "/cfg.json",
                            "RW": False,
                        },
                        {
                            "Source": "/tmp/python",
                            "Destination": "/usr/local/bin/python",
                            "RW": False,
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -eu
if [ "$1" = "inspect" ]; then
  cat "$PEER_INSPECT"
  exit 0
fi
exit 99
""",
    )
    shell = (
        "set -Eeuo pipefail\n"
        f'PATH="{fake_bin}:$PATH"\n'
        f'PEER_TSV="{manifest}"\n'
        'PEER_CONTAINER="trader-v3-node-a"\n'
        f'T="{trader_root}"\n'
        f"{_function_text('verify_peer_runtime')}\n"
        "verify_peer_runtime\n"
    )

    result = _run_shell(
        tmp_path,
        shell,
        environment={"PEER_INSPECT": str(inspect_path)},
    )

    assert result.returncode != 0
    assert (
        "account-a runtime mount set differs from peer manifest"
        in result.stderr
    )


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


def test_recreate_validator_rejects_shell_control_suffix(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "node-state" / "b"
    recreate = tmp_path / "recreate.sh"
    mount_plan = tmp_path / "mounts.tsv"
    mount_plan.write_text("", encoding="utf-8")
    validator = _materialize_recreate_validator(tmp_path)
    _write_executable(
        recreate,
        (
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            "docker rm -f trader-v3-node-b 2>/dev/null || true\n"
            f"mkdir -p {state_dir}\n"
            "docker run -d --name trader-v3-node-b "
            "--publish 8082:8082/tcp "
            "-e NODE_STATE_DIR=/state "
            "-e NAUTILUS_INITIAL_TRADING_STATE=HALTED "
            "trader-bot/nautilus-node:b10-verify "
            f"; touch {tmp_path / 'unexpected'}\n"
        ),
    )
    shell = (
        "set -Eeuo pipefail\n"
        'NODE_CONTAINER="trader-v3-node-b"\n'
        'TARGET_IMAGE="trader-bot/nautilus-node:b10-verify"\n'
        'NODE_PORT="8082"\n'
        f'STATE_DIR="{state_dir}"\n'
        f'RECREATE_VALIDATOR="{validator}"\n'
        f"{_function_text('validate_recreate_script')}\n"
        f'validate_recreate_script "{recreate}" "{mount_plan}" '
        '"$TARGET_IMAGE"\n'
    )

    result = _run_shell(tmp_path, shell)

    assert result.returncode != 0
    assert "recreate command is not canonical" in result.stderr


@pytest.mark.parametrize(
    ("image_and_command", "extra_mount", "expected"),
    (
        (
            (
                "old/image:latest "
                "trader-bot/nautilus-node:b10-verify"
            ),
            "",
            "recreate Docker image is invalid",
        ),
        (
            "trader-bot/nautilus-node:b10-verify",
            " -v /tmp/extra:/usr/local/bin/python:ro",
            "recreate mount plan is invalid",
        ),
    ),
)
def test_recreate_validator_binds_image_and_exact_mount_plan(
    tmp_path: Path,
    image_and_command: str,
    extra_mount: str,
    expected: str,
) -> None:
    state_dir = tmp_path / "node-state" / "b"
    config = tmp_path / "node-b.json"
    recreate = tmp_path / "recreate.sh"
    mount_plan = tmp_path / "mounts.tsv"
    mount_plan.write_text(
        (
            f"{config}\t/cfg.json\tro\n"
            f"{state_dir}\t/state\trw\n"
        ),
        encoding="utf-8",
    )
    validator = _materialize_recreate_validator(tmp_path)
    _write_executable(
        recreate,
        (
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            "docker rm -f trader-v3-node-b 2>/dev/null || true\n"
            f"mkdir -p {state_dir}\n"
            "docker run -d --name trader-v3-node-b "
            "--publish 8082:8082/tcp "
            "-e NODE_STATE_DIR=/state "
            "-e NAUTILUS_INITIAL_TRADING_STATE=HALTED "
            f"-v {config}:/cfg.json:ro "
            f"-v {state_dir}:/state:rw"
            f"{extra_mount} {image_and_command}\n"
        ),
    )
    result = subprocess.run(
        [
            "python3",
            str(validator),
            "validate",
            str(recreate),
            str(mount_plan),
            "trader-v3-node-b",
            "trader-bot/nautilus-node:b10-verify",
            "8082",
            str(state_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert expected in result.stderr


def test_recreate_rewrite_binds_release_mounts_and_immutable_image(
    tmp_path: Path,
) -> None:
    trader_root = tmp_path / "trader"
    peer_patch_dir = trader_root / "container-patches"
    release_patch_dir = (
        trader_root / "account-b-releases" / "commit" / "container-patches"
    )
    state_dir = trader_root / "node-state" / "b"
    config = trader_root / "node-b.hk.json"
    recreate = trader_root / "recreate-trader-v3-node-b.sh"
    manifest = tmp_path / "container.tsv"
    mount_plan = tmp_path / "release-mounts.tsv"
    validator = tmp_path / "validate-recreate.py"
    target_image = "trader-bot/nautilus-node:b10-verify"
    image_id = "sha256:immutable-image"
    manifest.write_text(
        "runtime.py\t/app/runtime/runtime.py\tdeadbeef\n",
        encoding="utf-8",
    )
    mount_plan.write_text(
        (
            f"{config}\t/cfg.json\tro\n"
            f"{release_patch_dir / 'runtime.py'}"
            "\t/app/runtime/runtime.py\tro\n"
            f"{state_dir}\t/state\trw\n"
        ),
        encoding="utf-8",
    )
    _write_executable(
        recreate,
        (
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            "docker rm -f trader-v3-node-b 2>/dev/null || true\n"
            f"mkdir -p {state_dir}\n"
            "docker run -d --name trader-v3-node-b "
            "--network=host "
            "-e NAUTILUS_HEALTH_PORT=8082 "
            "-e NODE_STATE_DIR=/state "
            "-e NAUTILUS_INITIAL_TRADING_STATE=HALTED "
            f"-v {config}:/cfg.json:ro "
            f"-v {state_dir}:/state:rw "
            f"-v {peer_patch_dir / 'runtime.py'}"
            ":/app/runtime/runtime.py:ro "
            f"{target_image}\n"
        ),
    )
    shell = (
        "set -Eeuo pipefail\n"
        f'RECREATE_TARGET="{recreate}"\n'
        f'CONTAINER_TSV="{manifest}"\n'
        f'PEER_PATCH_DIR="{peer_patch_dir}"\n'
        f'B_PATCH_DIR="{release_patch_dir}"\n'
        f'TARGET_IMAGE="{target_image}"\n'
        f'TARGET_IMAGE_ID="{image_id}"\n'
        f'RECREATE_VALIDATOR="{validator}"\n'
        'NODE_CONTAINER="trader-v3-node-b"\n'
        'NODE_PORT="8082"\n'
        f'STATE_DIR="{state_dir}"\n'
        f"{_function_text('write_recreate_validator')}\n"
        f"{_function_text('validate_recreate_script')}\n"
        f"{_function_text('rewrite_recreate_mounts')}\n"
        "write_recreate_validator\n"
        "rewrite_recreate_mounts\n"
        "python3 \"$RECREATE_VALIDATOR\" rewrite-image "
        "\"$RECREATE_TARGET\" \"$TARGET_IMAGE\" \"$TARGET_IMAGE_ID\"\n"
        f'validate_recreate_script "$RECREATE_TARGET" "{mount_plan}" '
        '"$TARGET_IMAGE_ID"\n'
    )

    result = _run_shell(tmp_path, shell)

    assert result.returncode == 0, result.stdout + result.stderr
    rewritten = recreate.read_text(encoding="utf-8")
    assert image_id in rewritten
    assert target_image not in rewritten
    assert str(release_patch_dir / "runtime.py") in rewritten
    assert str(peer_patch_dir / "runtime.py") not in rewritten


def test_rollback_recreates_halted_and_never_starts_the_legacy_container() -> None:
    text = _text()
    marker = "cat >\"$ROLLBACK_PATH\" <<'ROLLBACK'\n"
    start = text.index(marker) + len(marker)
    end = text.index("\nROLLBACK\n", start)
    rollback = text[start:end]

    assert "docker start" not in rollback
    assert 'docker rm -f "$NODE_CONTAINER"' in rollback
    assert '"$RECREATE_VALIDATOR" \\\n  rewrite-image' in rollback
    assert '"$RECREATE_VALIDATOR" \\\n  validate' in rollback
    assert '"$LEGACY_MOUNTS_TSV"' in rollback
    assert 'bash "$RECREATE_TARGET"' in rollback
    assert 'mv "$STATE_DIR" "$FAILED_STATE"' in rollback
    assert 'cp -a "$BACKUP_ROOT/node-state" "$STATE_DIR"' in rollback
    assert 'rm -rf -- "$B_RELEASE_ROOT"' in rollback


def test_rollback_restores_recreate_and_state_then_removes_failed_release(
    tmp_path: Path,
) -> None:
    trader_root = tmp_path / "trader"
    backup_root = trader_root / "backups" / "account-b-hardening-test"
    state_dir = trader_root / "node-state" / "b"
    config = trader_root / "node-b.hk.json"
    recreate = trader_root / "recreate-trader-v3-node-b.sh"
    backup_recreate = (
        backup_root
        / "files"
        / str(recreate).lstrip("/")
    )
    valid_recreate = (
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "docker rm -f trader-v3-node-b 2>/dev/null || true\n"
        f"mkdir -p {state_dir}\n"
        "docker run -d --name trader-v3-node-b "
        "--publish 8082:8082/tcp "
        "-e NODE_STATE_DIR=/state "
        "-e NAUTILUS_INITIAL_TRADING_STATE=HALTED "
        f"-v {config}:/cfg.json:ro "
        f"-v {state_dir}:/state:rw "
        "trader-bot/nautilus-node:b10-verify\n"
    )
    backup_recreate.parent.mkdir(parents=True)
    _write_executable(backup_recreate, valid_recreate)
    _write_executable(
        recreate,
        "#!/usr/bin/env bash\nexit 99\n",
    )
    index = (
        f"present\t{recreate}\t"
        f"{backup_recreate.relative_to(backup_root).as_posix()}\n"
    )
    (backup_root / "index.tsv").write_text(index, encoding="utf-8")

    state_dir.mkdir(parents=True)
    (state_dir / "journal.json").write_text("new state\n", encoding="utf-8")
    backup_state = backup_root / "node-state"
    backup_state.mkdir()
    (backup_state / "journal.json").write_text(
        "old state\n",
        encoding="utf-8",
    )
    release_root = trader_root / "account-b-releases" / "failed-release"
    release_root.mkdir(parents=True)
    (release_root / "runtime.py").write_text(
        "failed release\n",
        encoding="utf-8",
    )
    operation_lock = tmp_path / "default-operation.lock"
    image_id = "sha256:test-image"
    (backup_root / "rollback-config.tsv").write_text(
        (
            f"{trader_root}\t8082\t{release_root}\t"
            f"{operation_lock}\t{image_id}\n"
        ),
        encoding="utf-8",
    )
    validator = _materialize_recreate_validator(tmp_path)
    validator.replace(backup_root / "validate-recreate.py")
    (backup_root / "legacy-mounts.tsv").write_text(
        (
            f"{config}\t/cfg.json\tro\n"
            f"{state_dir}\t/state\trw\n"
        ),
        encoding="utf-8",
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
    restored_recreate = recreate.read_text(encoding="utf-8")
    assert image_id in restored_recreate
    assert "trader-bot/nautilus-node:b10-verify" not in restored_recreate
    assert (state_dir / "journal.json").read_text(
        encoding="utf-8"
    ) == "old state\n"
    assert not release_root.exists()
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
