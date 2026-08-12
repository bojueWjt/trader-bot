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


def test_optional_host_module_rejects_same_content_symlink_before_migration(
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "control-plane"
    api_root = target_root / "api"
    db_root = target_root / "db"
    api_root.mkdir(parents=True)
    db_root.mkdir(parents=True)
    payload = tmp_path / "app_roles.py"
    payload.write_text("ROLE = 'operator-query'\n", encoding="utf-8")
    app_roles_target = api_root / "app_roles.py"
    app_roles_target.symlink_to(payload)
    pools_target = db_root / "pools.py"

    source = (
        _definitions("die", "verify_optional_host_python_module_target")
        + f"""
verify_optional_host_python_module_target {app_roles_target}
verify_optional_host_python_module_target {pools_target}
"""
    )
    result = _run_bash(source, os.environ.copy())

    assert result.returncode != 0
    assert "optional host module target cannot be a symlink" in result.stderr

    text = DEPLOY.read_text(encoding="utf-8")
    app_roles_gate = text.index(
        'verify_optional_host_python_module_target "$APP_ROLES_TGT"',
    )
    pools_gate = text.index(
        'verify_optional_host_python_module_target "$DB_POOLS_TGT"',
    )
    migration_section = text.index("# ---------- database schema ----------")
    assert app_roles_gate < migration_section
    assert pools_gate < migration_section


def test_partial_host_module_install_is_restored_before_forward_recovery(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    api_root = tmp_path / "live" / "api"
    db_root = tmp_path / "live" / "db"
    backup_root = tmp_path / "backup"
    staging.mkdir()
    api_root.mkdir(parents=True)
    db_root.mkdir(parents=True)
    (backup_root / "files").mkdir(parents=True)
    app_roles_source = staging / "app_roles.py"
    pools_source = staging / "pools.py"
    app_roles_source.write_text("ROLE = 'operator-query'\n", encoding="utf-8")
    pools_source.write_text("POOL = 'writer'\n", encoding="utf-8")
    app_roles_target = api_root / "app_roles.py"
    pools_target = db_root / "pools.py"
    (backup_root / "index.tsv").write_text("", encoding="utf-8")
    (backup_root / "new-files.txt").write_text(
        f"{app_roles_target}\n{pools_target}\n",
        encoding="utf-8",
    )
    recovery_log = tmp_path / "recovery.log"

    source = (
        _definitions(
            "restore_installed_runtime_files",
            "rollback_restart_changed_runtimes",
            "on_err",
        )
        + f"""
BACKUP_ROOT={backup_root}
APP_ROLES_TGT={app_roles_target}
DB_POOLS_TGT={pools_target}
FILES_INSTALLED=1
HOST_RUNTIME_INSTALL_COMPLETE=0
BACKUP_CAPTURED=1
WATCHER_RESTARTED=0
EXCHANGE_STATE_RESTARTED=0
HERMES_RESTARTED=0
POST_MIGRATION_RECOVERY_REQUIRED=1
POST_MIGRATION_RECOVERY_VERIFIED=0
MIGRATION_COMMIT_MARKER={tmp_path / "migration-marker.json"}
DEPLOY_GATE_MODE=maintenance_fence
ROLLOUT_NODE=trader-v3-node-a
ROLLOUT_TRACKED=0
ROLLOUT_FINALIZED=0
PRESERVE_ROLLOUT_FOR_RETRY=0
BOOTSTRAP_REGISTRATION_COMPLETED=0
RELEASE_ID=
ROLLBACK_IN_PROGRESS=0
RECREATE_NODES=(trader-v3-node-a)

rollback_restart_watcher_runtime() {{ return 0; }}
rollback_restart_exchange_state_recorder() {{ return 0; }}
rollback_restart_hermes_units() {{ return 0; }}
ensure_bootstrap_rollout_registration() {{ return 0; }}
acquire_maintenance_fence_after_bootstrap() {{ return 0; }}
write_bootstrap_recovery_blocked_evidence() {{ return 0; }}
write_partial_install_recovery_evidence() {{ return 0; }}
finalize_node_startup_resource_evidence() {{ return 0; }}
run_reviewed_rollout() {{ return 0; }}
restore_pre_migration_state() {{ return 90; }}
docker() {{ return 0; }}
recover_post_migration_node() {{
  [ ! -e "$APP_ROLES_TGT" ] || return 71
  [ ! -e "$DB_POOLS_TGT" ] || return 72
  printf 'recovered\\n' >{recovery_log}
  return 0
}}

printf "ROLE = 'operator-query'\\n" >"$APP_ROLES_TGT"
ln -s {pools_source} "$DB_POOLS_TGT"
on_err 47
"""
    )
    result = _run_bash(source, os.environ.copy())

    assert result.returncode == 47, result.stderr
    assert not app_roles_target.exists()
    assert not pools_target.exists()
    assert recovery_log.is_file()
    assert recovery_log.read_text(encoding="utf-8") == "recovered\n"


def test_partial_install_cleanup_failure_blocks_forward_recovery(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    blocked_target = tmp_path / "blocked-target"
    recovery_log = tmp_path / "recovery.log"
    evidence_log = tmp_path / "evidence.log"
    (backup_root / "files").mkdir(parents=True)
    blocked_target.mkdir()
    (backup_root / "index.tsv").write_text("", encoding="utf-8")
    (backup_root / "new-files.txt").write_text(
        f"{blocked_target}\n",
        encoding="utf-8",
    )

    source = (
        _definitions(
            "restore_installed_runtime_files",
            "rollback_restart_changed_runtimes",
            "on_err",
        )
        + f"""
BACKUP_ROOT={backup_root}
FILES_INSTALLED=1
HOST_RUNTIME_INSTALL_COMPLETE=0
BACKUP_CAPTURED=1
WATCHER_RESTARTED=0
EXCHANGE_STATE_RESTARTED=0
HERMES_RESTARTED=0
POST_MIGRATION_RECOVERY_REQUIRED=1
POST_MIGRATION_RECOVERY_VERIFIED=0
MIGRATION_COMMIT_MARKER={tmp_path / "migration-marker.json"}
DEPLOY_GATE_MODE=maintenance_fence
ROLLOUT_NODE=trader-v3-node-a
ROLLOUT_TRACKED=0
ROLLOUT_FINALIZED=0
PRESERVE_ROLLOUT_FOR_RETRY=0
BOOTSTRAP_REGISTRATION_COMPLETED=0
RELEASE_ID=
ROLLBACK_IN_PROGRESS=0
RECREATE_NODES=(trader-v3-node-a)

rollback_restart_watcher_runtime() {{ return 0; }}
rollback_restart_exchange_state_recorder() {{ return 0; }}
rollback_restart_hermes_units() {{ return 0; }}
ensure_bootstrap_rollout_registration() {{ return 0; }}
acquire_maintenance_fence_after_bootstrap() {{ return 0; }}
write_bootstrap_recovery_blocked_evidence() {{ return 0; }}
write_partial_install_recovery_evidence() {{
  printf '%s:%s\\n' "$1" "$2" >{evidence_log}
  return 0
}}
finalize_node_startup_resource_evidence() {{ return 0; }}
run_reviewed_rollout() {{ return 0; }}
restore_pre_migration_state() {{ return 90; }}
docker() {{ return 0; }}
recover_post_migration_node() {{
  printf 'unexpected-recovery\\n' >{recovery_log}
  return 0
}}

on_err 53
"""
    )
    result = _run_bash(source, os.environ.copy())

    assert result.returncode == 53, result.stderr
    assert blocked_target.is_dir()
    assert not recovery_log.exists()
    assert evidence_log.read_text(encoding="utf-8") == (
        "partial-host-install-restore-failed:0\n"
    )
