from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "hk-deploy-20260803.sh"


def _function_source(name: str) -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _definitions(*names: str) -> str:
    return "\n".join(_function_source(name) for name in names)


def _write_fake_systemctl(bin_dir: Path) -> None:
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
command_name="$1"
shift
case "$command_name" in
  show)
    unit="${@: -1}"
    if grep -Fxq "$unit" "$FAKE_SYSTEMD_UNITS"; then
      printf 'loaded\\n'
    else
      printf 'not-found\\n'
    fi
    ;;
  is-active)
    unit="${@: -1}"
    grep -Fxq "$unit" "$FAKE_SYSTEMD_ACTIVE"
    ;;
  restart)
    printf 'restart %s\\n' "$*" >>"$FAKE_SYSTEMD_LOG"
    ;;
  *)
    printf 'unexpected systemctl command: %s %s\\n' \
      "$command_name" "$*" >&2
    exit 64
    ;;
esac
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)


def _base_environment(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_systemctl(bin_dir)
    units = tmp_path / "units"
    active = tmp_path / "active"
    log = tmp_path / "systemctl.log"
    units.write_text("", encoding="utf-8")
    active.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_SYSTEMD_UNITS": str(units),
        "FAKE_SYSTEMD_ACTIVE": str(active),
        "FAKE_SYSTEMD_LOG": str(log),
    }


def _run_bash(
    source: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"set -Eeuo pipefail\n{source}"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_role_topology_restarts_and_checks_all_three_units(
    tmp_path: Path,
) -> None:
    env = _base_environment(tmp_path)
    role_units = (
        "trader-v3-controlplane-node-control.service",
        "trader-v3-controlplane-event-ingest.service",
        "trader-v3-controlplane-operator-query.service",
    )
    Path(env["FAKE_SYSTEMD_UNITS"]).write_text(
        "\n".join(role_units) + "\n",
        encoding="utf-8",
    )
    Path(env["FAKE_SYSTEMD_ACTIVE"]).write_text(
        "\n".join(role_units) + "\n",
        encoding="utf-8",
    )
    source = (
        _definitions(
            "die",
            "unit_exists",
            "discover_control_plane_units",
            "restart_control_plane_units",
        )
        + """
ROLE_CONTROL_PLANE_UNITS=(
  trader-v3-controlplane-node-control.service
  trader-v3-controlplane-event-ingest.service
  trader-v3-controlplane-operator-query.service
)
CONTROL_PLANE_UNITS=()
CONTROL_PLANE_ISOLATION_REQUIRED=0
CONTROL_PLANE_ISOLATION_MODE=require
CONTROL_PLANE_ISOLATION_SCRIPT=/unused
CONTROL_PLANE_RESTARTED=0
CONTROL_PLANE_TOPOLOGY=
LEGACY_CONTROL_PLANE_UNIT=trader-v3-controlplane.service
verify_maintenance_fence() { return 0; }
discover_control_plane_units
restart_control_plane_units
printf 'topology=%s\\n' "$CONTROL_PLANE_TOPOLOGY"
printf 'units=%s\\n' "${CONTROL_PLANE_UNITS[*]}"
"""
    )

    result = _run_bash(source, env)

    assert result.returncode == 0, result.stderr
    assert "topology=roles" in result.stdout
    assert f"units={' '.join(role_units)}" in result.stdout
    assert Path(env["FAKE_SYSTEMD_LOG"]).read_text(encoding="utf-8") == (
        f"restart {' '.join(role_units)}\n"
    )


def test_legacy_mode_requires_explicit_emergency_rollback(
    tmp_path: Path,
) -> None:
    env = _base_environment(tmp_path)
    legacy_unit = "trader-v3-controlplane.service"
    Path(env["FAKE_SYSTEMD_UNITS"]).write_text(
        f"{legacy_unit}\n",
        encoding="utf-8",
    )
    Path(env["FAKE_SYSTEMD_ACTIVE"]).write_text(
        f"{legacy_unit}\n",
        encoding="utf-8",
    )
    source = (
        _definitions("die", "unit_exists", "discover_control_plane_units")
        + f"""
ROLE_CONTROL_PLANE_UNITS=(
  trader-v3-controlplane-node-control.service
  trader-v3-controlplane-event-ingest.service
  trader-v3-controlplane-operator-query.service
)
CONTROL_PLANE_UNITS=()
CONTROL_PLANE_ISOLATION_REQUIRED=0
CONTROL_PLANE_ISOLATION_MODE=disable
CONTROL_PLANE_ISOLATION_SCRIPT=/unused
CONTROL_PLANE_TOPOLOGY=
LEGACY_CONTROL_PLANE_UNIT={legacy_unit}
EMERGENCY_ROLLBACK=1
discover_control_plane_units
printf 'topology=%s\\n' "$CONTROL_PLANE_TOPOLOGY"
printf 'isolation=%s\\n' "$CONTROL_PLANE_ISOLATION_REQUIRED"
printf 'units=%s\\n' "${{CONTROL_PLANE_UNITS[*]}}"
"""
    )

    result = _run_bash(source, env)

    assert result.returncode == 0, result.stderr
    assert "topology=legacy" in result.stdout
    assert "isolation=0" in result.stdout
    assert f"units={legacy_unit}" in result.stdout


def test_partial_role_topology_fails_closed(tmp_path: Path) -> None:
    env = _base_environment(tmp_path)
    Path(env["FAKE_SYSTEMD_UNITS"]).write_text(
        "trader-v3-controlplane-node-control.service\n"
        "trader-v3-controlplane-event-ingest.service\n",
        encoding="utf-8",
    )
    source = (
        _definitions("die", "unit_exists", "discover_control_plane_units")
        + """
ROLE_CONTROL_PLANE_UNITS=(
  trader-v3-controlplane-node-control.service
  trader-v3-controlplane-event-ingest.service
  trader-v3-controlplane-operator-query.service
)
CONTROL_PLANE_UNITS=()
CONTROL_PLANE_ISOLATION_REQUIRED=0
CONTROL_PLANE_ISOLATION_MODE=require
CONTROL_PLANE_ISOLATION_SCRIPT=/unused
CONTROL_PLANE_TOPOLOGY=
LEGACY_CONTROL_PLANE_UNIT=trader-v3-controlplane.service
discover_control_plane_units
"""
    )

    result = _run_bash(source, env)

    assert result.returncode != 0
    assert "partial control-plane role topology detected: 2/3 units" in (
        result.stderr
    )


def test_required_mode_runs_isolation_and_requires_complete_role_topology(
    tmp_path: Path,
) -> None:
    env = _base_environment(tmp_path)
    legacy_unit = "trader-v3-controlplane.service"
    role_units = (
        "trader-v3-controlplane-node-control.service",
        "trader-v3-controlplane-event-ingest.service",
        "trader-v3-controlplane-operator-query.service",
    )
    units_path = Path(env["FAKE_SYSTEMD_UNITS"])
    active_path = Path(env["FAKE_SYSTEMD_ACTIVE"])
    units_path.write_text(f"{legacy_unit}\n", encoding="utf-8")
    active_path.write_text(f"{legacy_unit}\n", encoding="utf-8")
    isolation_log = tmp_path / "isolation.log"
    isolation = tmp_path / "isolation.sh"
    isolation.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'root=%s backup=%s legacy=%s ownership=%s fd=%s token=%s\\n' \
  "$TRADER_ROOT" \
  "$CONTROL_PLANE_ISOLATION_BACKUP_ROOT" \
  "$LEGACY_CONTROL_PLANE_UNIT" \
  "$ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP" \
  "$ACCOUNT_STALL_OPERATION_LOCK_FD" \
  "$ACCOUNT_STALL_OPERATION_LOCK_TOKEN" >"$FAKE_ISOLATION_LOG"
cat >"$FAKE_SYSTEMD_UNITS" <<'EOF'
trader-v3-controlplane-node-control.service
trader-v3-controlplane-event-ingest.service
trader-v3-controlplane-operator-query.service
EOF
cp "$FAKE_SYSTEMD_UNITS" "$FAKE_SYSTEMD_ACTIVE"
""",
        encoding="utf-8",
    )
    isolation.chmod(0o755)
    env["FAKE_ISOLATION_LOG"] = str(isolation_log)
    env["ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP"] = "inherited"
    env["ACCOUNT_STALL_OPERATION_LOCK_FD"] = "9"
    env["ACCOUNT_STALL_OPERATION_LOCK_TOKEN"] = "fixture-owner-token"
    source = (
        _definitions(
            "die",
            "unit_exists",
            "discover_control_plane_units",
            "activate_control_plane_topology",
        )
        + f"""
ROLE_CONTROL_PLANE_UNITS=(
  {' '.join(role_units)}
)
CONTROL_PLANE_UNITS=()
CONTROL_PLANE_ISOLATION_REQUIRED=0
CONTROL_PLANE_ISOLATION_MODE=require
CONTROL_PLANE_ISOLATION_SCRIPT={isolation}
CONTROL_PLANE_RESTARTED=0
CONTROL_PLANE_TOPOLOGY=
LEGACY_CONTROL_PLANE_UNIT={legacy_unit}
T={tmp_path / "trader-v3"}
BACKUP_ROOT={tmp_path / "backup"}
verify_maintenance_fence() {{ return 0; }}
discover_control_plane_units
activate_control_plane_topology
printf 'topology=%s\\n' "$CONTROL_PLANE_TOPOLOGY"
printf 'restarted=%s\\n' "$CONTROL_PLANE_RESTARTED"
"""
    )

    result = _run_bash(source, env)

    assert result.returncode == 0, result.stderr
    assert "topology=roles" in result.stdout
    assert "restarted=1" in result.stdout
    isolation_text = isolation_log.read_text(encoding="utf-8")
    assert f"root={tmp_path / 'trader-v3'}" in isolation_text
    assert f"backup={tmp_path / 'backup' / 'control-plane-isolation'}" in (
        isolation_text
    )
    assert f"legacy={legacy_unit}" in isolation_text
    assert "ownership=inherited" in isolation_text
    assert "fd=9" in isolation_text
    assert "token=fixture-owner-token" in isolation_text


def test_required_mode_propagates_isolation_failure(tmp_path: Path) -> None:
    env = _base_environment(tmp_path)
    legacy_unit = "trader-v3-controlplane.service"
    Path(env["FAKE_SYSTEMD_UNITS"]).write_text(
        f"{legacy_unit}\n",
        encoding="utf-8",
    )
    Path(env["FAKE_SYSTEMD_ACTIVE"]).write_text(
        f"{legacy_unit}\n",
        encoding="utf-8",
    )
    isolation = tmp_path / "isolation.sh"
    isolation.write_text(
        "#!/usr/bin/env bash\nexit 23\n",
        encoding="utf-8",
    )
    isolation.chmod(0o755)
    source = (
        _definitions(
            "die",
            "unit_exists",
            "discover_control_plane_units",
            "activate_control_plane_topology",
        )
        + f"""
ROLE_CONTROL_PLANE_UNITS=(
  trader-v3-controlplane-node-control.service
  trader-v3-controlplane-event-ingest.service
  trader-v3-controlplane-operator-query.service
)
CONTROL_PLANE_UNITS=()
CONTROL_PLANE_ISOLATION_REQUIRED=0
CONTROL_PLANE_ISOLATION_MODE=require
CONTROL_PLANE_ISOLATION_SCRIPT={isolation}
CONTROL_PLANE_RESTARTED=0
CONTROL_PLANE_TOPOLOGY=
LEGACY_CONTROL_PLANE_UNIT={legacy_unit}
T={tmp_path / "trader-v3"}
BACKUP_ROOT={tmp_path / "backup"}
verify_maintenance_fence() {{ return 0; }}
discover_control_plane_units
activate_control_plane_topology
"""
    )

    result = _run_bash(source, env)

    assert result.returncode == 23


def test_required_isolation_must_be_covered_by_staging_checksums(
    tmp_path: Path,
) -> None:
    env = _base_environment(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    checksums = staging / "SHA256SUMS"
    checksums.write_text("0" * 64 + "  unrelated-file\n", encoding="utf-8")
    source = (
        _definitions("die", "verify_control_plane_isolation_artifact")
        + f"""
STAGING={staging}
CONTROL_PLANE_ISOLATION_REQUIRED=1
verify_control_plane_isolation_artifact
"""
    )

    missing = _run_bash(source, env)

    assert missing.returncode != 0
    assert "SHA256SUMS does not cover hk-control-plane-isolation.sh" in (
        missing.stderr
    )

    checksums.write_text(
        "0" * 64 + "  hk-control-plane-isolation.sh\n",
        encoding="utf-8",
    )
    covered = _run_bash(source, env)

    assert covered.returncode == 0, covered.stderr


def test_journal_scan_covers_every_effective_unit_and_fails_on_errors(
    tmp_path: Path,
) -> None:
    env = _base_environment(tmp_path)
    journalctl = tmp_path / "bin" / "journalctl"
    journalctl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_JOURNAL_LOG"
if [ "${FAKE_JOURNAL_ERROR_UNIT:-}" = "$2" ]; then
  printf 'Traceback: injected failure\\n'
fi
""",
        encoding="utf-8",
    )
    journalctl.chmod(0o755)
    journal_log = tmp_path / "journal.log"
    env["FAKE_JOURNAL_LOG"] = str(journal_log)
    role_units = (
        "trader-v3-controlplane-node-control.service",
        "trader-v3-controlplane-event-ingest.service",
        "trader-v3-controlplane-operator-query.service",
    )
    source = (
        _definitions("die", "scan_control_plane_journals")
        + f"""
CONTROL_PLANE_UNITS=({' '.join(role_units)})
scan_control_plane_journals
"""
    )

    result = _run_bash(source, env)

    assert result.returncode == 0, result.stderr
    journal_text = journal_log.read_text(encoding="utf-8")
    for unit in role_units:
        assert f"-u {unit} --since -2 min --no-pager" in journal_text

    env["FAKE_JOURNAL_ERROR_UNIT"] = role_units[1]
    failed = _run_bash(source, env)

    assert failed.returncode != 0
    assert f"control-plane logging errors after restart: {role_units[1]}" in (
        failed.stderr
    )


def _write_quiescence_fakes(bin_dir: Path) -> None:
    curl = bin_dir / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
url="${@: -1}"
case "$url" in
  *:8081/ready)
    state="${FAKE_ACCOUNT_A_STATE:-HALTED}"
    account_id="${FAKE_ACCOUNT_A_ACCOUNT_ID:-account-a}"
    ;;
  *:8082/ready)
    if [ "${FAKE_ACCOUNT_B_UNAVAILABLE:-0}" = "1" ]; then
      exit 22
    fi
    state="${FAKE_ACCOUNT_B_STATE:-HALTED}"
    account_id="${FAKE_ACCOUNT_B_ACCOUNT_ID:-account-b}"
    ;;
  *:8083/ready)
    state="${FAKE_ACCOUNT_C_STATE:-HALTED}"
    account_id="${FAKE_ACCOUNT_C_ACCOUNT_ID:-account-c}"
    ;;
  *:8084/ready)
    state="${FAKE_ACCOUNT_D_STATE:-HALTED}"
    account_id="${FAKE_ACCOUNT_D_ACCOUNT_ID:-account-d}"
    ;;
  *)
    exit 64
    ;;
esac
printf '{"account_id":"%s","trading_state":"%s"}\\n' \
  "$account_id" \
  "$state"
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
container="${@: -1}"
case "$container" in
  trader-v3-node-a)
    printf '%s\\n' "${FAKE_ACCOUNT_A_RUNNING:-true}"
    ;;
  trader-v3-node-b)
    printf '%s\\n' "${FAKE_ACCOUNT_B_RUNNING:-true}"
    ;;
  trader-v3-node-c)
    printf '%s\\n' "${FAKE_ACCOUNT_C_RUNNING:-true}"
    ;;
  trader-v3-node-d)
    printf '%s\\n' "${FAKE_ACCOUNT_D_RUNNING:-true}"
    ;;
  *)
    exit 64
    ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)


def test_shared_mutation_gate_rejects_active_peer(tmp_path: Path) -> None:
    env = _base_environment(tmp_path)
    _write_quiescence_fakes(tmp_path / "bin")
    env["FAKE_ACCOUNT_A_STATE"] = "HALTED"
    env["FAKE_ACCOUNT_B_STATE"] = "ACTIVE"
    source = (
        _definitions(
            "die",
            "verify_execution_account_quiesced",
            "verify_all_execution_accounts_quiesced",
        )
        + """
TEMP_FILES=()
verify_all_execution_accounts_quiesced
"""
    )

    result = _run_bash(source, env)

    assert result.returncode != 0
    assert "account-b trading state is ACTIVE" in result.stderr


def test_shared_mutation_gate_accepts_both_halted(tmp_path: Path) -> None:
    env = _base_environment(tmp_path)
    _write_quiescence_fakes(tmp_path / "bin")
    env["FAKE_ACCOUNT_A_STATE"] = "HALTED"
    env["FAKE_ACCOUNT_B_STATE"] = "HALTED"
    source = (
        _definitions(
            "die",
            "verify_execution_account_quiesced",
            "verify_all_execution_accounts_quiesced",
        )
        + """
TEMP_FILES=()
verify_all_execution_accounts_quiesced
"""
    )

    result = _run_bash(source, env)

    assert result.returncode == 0, result.stderr


def test_shared_mutation_gate_accepts_unavailable_stopped_peer(
    tmp_path: Path,
) -> None:
    env = _base_environment(tmp_path)
    _write_quiescence_fakes(tmp_path / "bin")
    env["FAKE_ACCOUNT_A_STATE"] = "HALTED"
    env["FAKE_ACCOUNT_B_UNAVAILABLE"] = "1"
    env["FAKE_ACCOUNT_B_RUNNING"] = "false"
    source = (
        _definitions(
            "die",
            "verify_execution_account_quiesced",
            "verify_all_execution_accounts_quiesced",
        )
        + """
TEMP_FILES=()
verify_all_execution_accounts_quiesced
"""
    )

    result = _run_bash(source, env)

    assert result.returncode == 0, result.stderr


def test_shared_mutation_gate_rejects_wrong_peer_identity(
    tmp_path: Path,
) -> None:
    env = _base_environment(tmp_path)
    _write_quiescence_fakes(tmp_path / "bin")
    env["FAKE_ACCOUNT_A_STATE"] = "HALTED"
    env["FAKE_ACCOUNT_B_STATE"] = "HALTED"
    env["FAKE_ACCOUNT_B_ACCOUNT_ID"] = "account-a"
    source = (
        _definitions(
            "die",
            "verify_execution_account_quiesced",
            "verify_all_execution_accounts_quiesced",
        )
        + """
TEMP_FILES=()
verify_all_execution_accounts_quiesced
"""
    )

    result = _run_bash(source, env)

    assert result.returncode != 0
    assert "account-b readiness payload is invalid" in result.stderr


def test_shared_mutation_gate_precedes_migration_and_topology_replacement() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    migration_section = text.index("# ---------- database schema ----------")
    migration_gate = text.index(
        "verify_all_execution_accounts_quiesced",
        migration_section,
    )
    recovery_armed = text.index(
        "POST_MIGRATION_RECOVERY_REQUIRED=1",
        migration_gate,
    )
    migration = text.index(
        "apply_and_verify_database_migration",
        recovery_armed,
    )
    install_section = text.index("# ---------- install ----------")
    install_gate = text.index(
        "verify_all_execution_accounts_quiesced",
        install_section,
    )
    files_installed = text.index("FILES_INSTALLED=1", install_gate)
    topology_section = text.index("# ---------- recreate & verify ----------")
    topology_gate = text.index(
        "verify_all_execution_accounts_quiesced",
        topology_section,
    )
    topology = text.index("activate_control_plane_topology", topology_gate)

    assert migration_section < migration_gate < recovery_armed < migration
    assert install_section < install_gate < files_installed
    assert topology_section < topology_gate < topology
