import os
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "scripts"
    / "hk-deploy-account-stall-availability.sh"
)


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _rollback_text() -> str:
    text = _text()
    marker = "cat >\"$ROLLBACK_PATH\" <<'ROLLBACK'\n"
    start = text.index(marker) + len(marker)
    end = text.index("\nROLLBACK\n", start)
    return text[start:end] + "\n"


def _sse_probe_function_text() -> str:
    text = _text()
    marker = "probe_control_plane_sse_contract() {"
    start = text.index(marker)
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_rollback(
    tmp_path: Path,
    *,
    original_state: str,
    vendor_fragment: str = "",
) -> tuple[
    subprocess.CompletedProcess[str],
    list[str],
    Path,
    str,
    Path,
]:
    trader_root = tmp_path / "trader"
    backup_root = trader_root / "backups" / f"rollback-{original_state}"
    backup_root.mkdir(parents=True)
    rollback_path = backup_root / "rollback.sh"
    rollback = _rollback_text().replace(
        'LOCK="/var/lock/trader-v3-account-stall-operation.lock"',
        f'LOCK="{tmp_path / "rollback.lock"}"',
    )
    _write_executable(rollback_path, rollback)

    unit_target = (
        tmp_path
        / "etc"
        / "systemd"
        / "system"
        / "trader-v3-controlplane.service"
    )
    unit_target.parent.mkdir(parents=True)
    unit_target.write_text("deployed unit\n", encoding="utf-8")
    backup_relative = Path("files") / str(unit_target).lstrip("/")
    snapshot_fragment = str(unit_target)
    if original_state == "absent" or vendor_fragment:
        index_line = f"absent\t{unit_target}\t-\n"
    else:
        backup_unit = backup_root / backup_relative
        backup_unit.parent.mkdir(parents=True)
        backup_unit.write_text("original unit\n", encoding="utf-8")
        index_line = (
            f"present\t{unit_target}\t{backup_relative.as_posix()}\n"
        )
    (backup_root / "index.tsv").write_text(index_line, encoding="utf-8")
    (backup_root / "SHA256SUMS").write_text("", encoding="utf-8")
    load_state = "loaded"
    if original_state == "absent":
        load_state = "not-found"
        snapshot_fragment = "-"
    elif vendor_fragment:
        snapshot_fragment = vendor_fragment
    (backup_root / "control-plane-unit-state.txt").write_text(
        (
            f"{original_state}\t{load_state}\t"
            f"{snapshot_fragment}\n"
        ),
        encoding="utf-8",
    )

    fake_bin = tmp_path / "fake-bin"
    systemctl_log = tmp_path / f"systemctl-{original_state}.log"
    wants_link = (
        unit_target.parent
        / "multi-user.target.wants"
        / "trader-v3-controlplane.service"
    )
    wants_link.parent.mkdir(parents=True)
    wants_link.symlink_to(unit_target)
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
set -eu
load_state() {
  if [ -e "$FAKE_DEPLOY_UNIT_FILE" ]; then
    printf 'loaded\\n'
    return
  fi
  if [ -n "$FAKE_VENDOR_FRAGMENT" ]; then
    printf 'loaded\\n'
    return
  fi
  printf 'not-found\\n'
}
fragment_path() {
  if [ -e "$FAKE_DEPLOY_UNIT_FILE" ]; then
    printf '%s\\n' "$FAKE_DEPLOY_UNIT_FILE"
    return
  fi
  printf '%s\\n' "$FAKE_VENDOR_FRAGMENT"
}
enablement_state() {
  if [ -L "$FAKE_WANTS_LINK" ]; then
    printf 'enabled\\n'
    return
  fi
  if [ "$(load_state)" = "not-found" ]; then
    printf 'not-found\\n'
    return
  fi
  printf 'disabled\\n'
}
command="$1"
shift
printf '%s' "$command" >>"$SYSTEMCTL_LOG"
if [ "$#" -gt 0 ]; then
  printf ' %s' "$*" >>"$SYSTEMCTL_LOG"
fi
printf '\\n' >>"$SYSTEMCTL_LOG"
case "$command" in
  enable)
    [ "$(load_state)" = "loaded" ]
    mkdir -p "$(dirname "$FAKE_WANTS_LINK")"
    rm -f "$FAKE_WANTS_LINK"
    ln -s "$(fragment_path)" "$FAKE_WANTS_LINK"
    ;;
  disable)
    [ "$(load_state)" = "loaded" ]
    rm -f "$FAKE_WANTS_LINK"
    ;;
  is-enabled)
    state="$(enablement_state)"
    printf '%s\\n' "$state"
    [ "$state" = "enabled" ]
    ;;
  start|is-active)
    for argument in "$@"; do
      if [ "$argument" = "trader-v3-controlplane.service" ] \
        && [ "$(load_state)" = "not-found" ]; then
        exit 9
      fi
    done
    ;;
  show)
    property=""
    for argument in "$@"; do
      case "$argument" in
        --property=*)
          property="${argument#--property=}"
          ;;
      esac
    done
    case "$property" in
      LoadState)
        load_state
        ;;
      FragmentPath)
        fragment_path
        ;;
      MainPID)
        printf '4242\\n'
        ;;
    esac
    ;;
esac
""",
    )
    _write_executable(
        fake_bin / "flock",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        fake_bin / "sha256sum",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        trader_root / ".venv-cp" / "bin" / "python",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        trader_root / "recreate-trader-v3-node-a.sh",
        (
            "#!/usr/bin/env bash\n"
            "# NAUTILUS_INITIAL_TRADING_STATE=HALTED\n"
            "exit 0\n"
        ),
    )

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["SYSTEMCTL_LOG"] = str(systemctl_log)
    environment["FAKE_DEPLOY_UNIT_FILE"] = str(unit_target)
    environment["FAKE_VENDOR_FRAGMENT"] = vendor_fragment
    environment["FAKE_WANTS_LINK"] = str(wants_link)
    result = subprocess.run(
        ["bash", str(rollback_path)],
        cwd=trader_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    commands = systemctl_log.read_text(encoding="utf-8").splitlines()
    if wants_link.is_symlink():
        restored_state = "enabled"
    elif unit_target.exists() or vendor_fragment:
        restored_state = "disabled"
    else:
        restored_state = "not-found"
    return result, commands, unit_target, restored_state, wants_link


def _run_sse_probe(
    tmp_path: Path,
    *,
    proxy_status: str,
    direct_status: str,
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "fake-bin"
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
set -eu
url=""
for argument in "$@"; do
  url="$argument"
done
if [ "$url" = "http://127.0.0.1:8080/v1/stream" ]; then
  printf '%s' "$DIRECT_STATUS"
else
  printf '%s' "$PROXY_STATUS"
fi
exit 28
""",
    )
    shell = (
        "set -Eeuo pipefail\n"
        "die() {\n"
        "  echo \"FATAL: $*\" >&2\n"
        "  return 1\n"
        "}\n"
        "CONTROL_PLANE_SSE_PROXY_URL="
        '"http://100.104.27.123:8088/v1/stream"\n'
        f"{_sse_probe_function_text()}\n"
        'probe_control_plane_sse_contract "test"\n'
    )
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["PROXY_STATUS"] = proxy_status
    environment["DIRECT_STATUS"] = direct_status
    return subprocess.run(
        ["bash", "-c", shell],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_deploy_script_is_account_a_only_and_never_auto_resumes() -> None:
    text = _text()

    assert 'NODE_CONTAINER="trader-v3-node-a"' in text
    assert "trader-v3-node-b" not in text
    assert "account-b" not in text
    assert "NAUTILUS_INITIAL_TRADING_STATE=HALTED" in text
    assert "RESUME" not in text
    assert '"type": "resume"' not in text.lower()


def test_deploy_script_is_control_plane_first() -> None:
    text = _text()

    host_install = text.index('target_path="$T/$target_relative"')
    unit_install = text.index(
        'install -D -m 0644 "$CONTROL_PLANE_UNIT_SOURCE" '
        '"$CONTROL_PLANE_UNIT_TARGET"',
        host_install,
    )
    daemon_reload = text.index("systemctl daemon-reload", unit_install)
    control_plane_enable = text.index(
        'systemctl enable "$CONTROL_PLANE_UNIT"',
        daemon_reload,
    )
    control_plane_enabled = text.index(
        'systemctl is-enabled --quiet "$CONTROL_PLANE_UNIT"',
        control_plane_enable,
    )
    control_plane_restart = text.index(
        'systemctl restart "$CONTROL_PLANE_UNIT"',
        control_plane_enabled,
    )
    recorder_restart = text.index(
        'systemctl start "$EXCHANGE_STATE_UNIT"',
        control_plane_restart,
    )
    port_8080_check = text.index(
        '"http://127.0.0.1:8080/openapi.json"',
        recorder_restart,
    )
    patch_install = text.index(
        'target_path="$PATCH_DIR/$bundle_path"',
        port_8080_check,
    )
    recreate = text.index('bash "$RECREATE_TARGET"', patch_install)

    assert host_install < unit_install
    assert unit_install < daemon_reload
    assert daemon_reload < control_plane_enable
    assert control_plane_enable < control_plane_enabled
    assert control_plane_enabled < control_plane_restart
    assert control_plane_restart < recorder_restart
    assert recorder_restart < port_8080_check
    assert port_8080_check < patch_install
    assert patch_install < recreate


def test_deploy_script_installs_and_probes_live_trade_contract() -> None:
    text = _text()

    assert "scripts/account_a_live_trade_executor.py" in text
    assert "scripts/account_a_live_trade_http_adapter.py" in text
    assert 'expected_mode = "0755"' in text
    assert 'install -D -m "$install_mode"' in text
    assert '"source_evidence"' in text
    assert 'environment.get("NAUTILUS_NODE_AUTH_JSON"' in text
    assert "/v1/nodes/{node_id}/exchange-state?{query}" in text
    assert "opening execution evidence authority is invalid" in text
    assert "source-specific opening evidence is missing" in text


def test_deploy_script_installs_and_restarts_exchange_state_recorder() -> None:
    text = _text()

    recorder_relative = (
        "services/control-plane/tools/exchange_state_recorder.py"
    )
    assert f'RECORDER_RELATIVE="{recorder_relative}"' in text
    assert 'RECORDER_TARGET="$T/$RECORDER_RELATIVE"' in text
    assert recorder_relative in text
    assert '"host/exchange_state_recorder.py"' not in text
    assert 'EXCHANGE_STATE_UNIT="trader-v3-exchange-state.service"' in text
    assert 'RECORDER_VERIFIER="$STAGING/tools/' in text
    assert "verify-exchange-state-recorder.py" in text
    assert "--property=MainPID" in text
    assert "--working-directory" in text
    assert "--recorder" in text
    assert "--watermark-file" in text
    assert "--require-position-field leverage" in text
    assert "--require-position-field margin_type" in text
    assert "--require-position-field isolated_margin" in text
    assert "--require-position-field is_auto_add_margin" in text
    assert "--account account-a" in text
    assert '[ -f "$RECORDER_TARGET" ]' in text
    assert text.count('systemctl stop "$EXCHANGE_STATE_UNIT"') == 1
    assert text.count('systemctl start "$EXCHANGE_STATE_UNIT"') == 2
    assert text.count(
        'systemctl is-active --quiet "$EXCHANGE_STATE_UNIT"'
    ) == 2


def test_deploy_script_keeps_lock_backup_and_precise_rollback() -> None:
    text = _text()

    assert (
        'OPERATION_LOCK="/var/lock/'
        'trader-v3-account-stall-operation.lock"'
    ) in text
    assert 'flock -n 9 || die "another operation holds $OPERATION_LOCK"' in text
    assert 'backup_target "$PATCH_DIR/$bundle_path"' in text
    assert 'backup_target "$T/$target_relative"' in text
    assert (
        '"services/control-plane/tools/exchange_state_recorder.py",'
        in text
    )
    assert '"$BACKUP_ROOT/verify-exchange-state-recorder.py"' in text
    assert "rollback-exchange-state-watermark.txt" in text
    assert 'backup_target "$RECREATE_TARGET"' in text
    assert 'backup_target "$DEPLOYED_COMMIT_TARGET"' in text
    assert 'backup_target "$CONTROL_PLANE_UNIT_TARGET"' in text
    assert 'CONTROL_PLANE_UNIT_STATE_FILE="$BACKUP_ROOT/' in text
    assert "control-plane-unit-state.txt" in text
    assert 'docker inspect "$NODE_CONTAINER" >' in text
    assert 'printf \'absent\\t%s\\t-\\n\'' in text
    assert 'echo "ROLLBACK: bash $ROLLBACK_PATH"' in text
    assert 'cat >"$ROLLBACK_PATH"' in text
    assert 'rm -rf -- "$target"' in text
    assert text.count("systemctl daemon-reload") == 2
    rollback_start = text.index("cat >\"$ROLLBACK_PATH\" <<'ROLLBACK'")
    rollback_end = text.index("\nROLLBACK", rollback_start)
    rollback = text[rollback_start:rollback_end]
    daemon_reload = rollback.index("systemctl daemon-reload")
    exchange_state_start = rollback.index(
        'systemctl start "$EXCHANGE_STATE_UNIT"',
        daemon_reload,
    )
    restore_state_case = rollback.index(
        'case "$CONTROL_PLANE_UNIT_STATE" in',
        exchange_state_start,
    )

    assert daemon_reload < exchange_state_start < restore_state_case


def test_deploy_snapshots_control_plane_unit_state_before_install() -> None:
    text = _text()

    snapshot_start = text.index('CONTROL_PLANE_UNIT_LOAD_STATE="$(')
    state_snapshot = text.index(
        '>"$CONTROL_PLANE_UNIT_STATE_FILE"',
        snapshot_start,
    )
    backup_unit = text.index(
        'backup_target "$CONTROL_PLANE_UNIT_TARGET"',
        state_snapshot,
    )
    unit_install = text.index(
        'install -D -m 0644 "$CONTROL_PLANE_UNIT_SOURCE" '
        '"$CONTROL_PLANE_UNIT_TARGET"',
        backup_unit,
    )

    assert state_snapshot < backup_unit < unit_install


def test_deploy_uses_systemd_load_state_and_fragment_path() -> None:
    text = _text()
    start = text.index('CONTROL_PLANE_UNIT_LOAD_STATE="$(')
    end = text.index("\n\nbackup_target()", start)
    snapshot = text[start:end]

    assert "systemctl show" in snapshot
    assert "--property=LoadState" in snapshot
    assert "--property=FragmentPath" in snapshot
    assert '[ -e "$CONTROL_PLANE_UNIT_TARGET" ]' not in snapshot
    assert '[ -L "$CONTROL_PLANE_UNIT_TARGET" ]' not in snapshot


def test_rollback_restores_present_enabled_control_plane(
    tmp_path: Path,
) -> None:
    result, commands, unit_target, restored_state, wants_link = (
        _run_rollback(
            tmp_path,
            original_state="enabled",
        )
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert unit_target.read_text(encoding="utf-8") == "original unit\n"
    assert restored_state == "enabled"
    assert wants_link.resolve() == unit_target
    assert "enable trader-v3-controlplane.service" in commands
    assert "start trader-v3-controlplane.service" in commands
    assert "start trader-v3-exchange-state.service" in commands


def test_rollback_restores_present_disabled_control_plane(
    tmp_path: Path,
) -> None:
    result, commands, unit_target, restored_state, wants_link = (
        _run_rollback(
            tmp_path,
            original_state="disabled",
        )
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert unit_target.read_text(encoding="utf-8") == "original unit\n"
    assert restored_state == "disabled"
    assert not wants_link.exists()
    assert "disable trader-v3-controlplane.service" in commands
    assert "enable trader-v3-controlplane.service" not in commands
    assert "start trader-v3-controlplane.service" in commands
    assert "start trader-v3-exchange-state.service" in commands


def test_rollback_restores_vendor_enabled_control_plane(
    tmp_path: Path,
) -> None:
    fragment_path = (
        "/usr/lib/systemd/system/trader-v3-controlplane.service"
    )
    result, commands, unit_target, restored_state, wants_link = (
        _run_rollback(
            tmp_path,
            original_state="enabled",
            vendor_fragment=fragment_path,
        )
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not unit_target.exists()
    assert restored_state == "enabled"
    assert os.readlink(wants_link) == fragment_path
    assert (
        "show --property=FragmentPath --value "
        "trader-v3-controlplane.service"
    ) in commands
    assert "enable trader-v3-controlplane.service" in commands
    assert "start trader-v3-controlplane.service" in commands
    assert "start trader-v3-exchange-state.service" in commands


def test_rollback_restores_vendor_disabled_control_plane(
    tmp_path: Path,
) -> None:
    fragment_path = (
        "/lib/systemd/system/trader-v3-controlplane.service"
    )
    result, commands, unit_target, restored_state, wants_link = (
        _run_rollback(
            tmp_path,
            original_state="disabled",
            vendor_fragment=fragment_path,
        )
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not unit_target.exists()
    assert restored_state == "disabled"
    assert not wants_link.exists()
    assert (
        "show --property=FragmentPath --value "
        "trader-v3-controlplane.service"
    ) in commands
    assert "disable trader-v3-controlplane.service" in commands
    assert "enable trader-v3-controlplane.service" not in commands
    assert "start trader-v3-controlplane.service" in commands
    assert "start trader-v3-exchange-state.service" in commands


def test_rollback_restores_absent_control_plane_without_starting_it(
    tmp_path: Path,
) -> None:
    result, commands, unit_target, restored_state, wants_link = (
        _run_rollback(
            tmp_path,
            original_state="absent",
        )
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not unit_target.exists()
    assert restored_state == "not-found"
    assert not wants_link.exists()
    assert "enable trader-v3-controlplane.service" not in commands
    disable_index = commands.index(
        "disable trader-v3-controlplane.service"
    )
    disabled_index = commands.index(
        "is-enabled trader-v3-controlplane.service",
        disable_index,
    )
    daemon_reload_index = commands.index("daemon-reload")
    absent_index = commands.index(
        "is-enabled trader-v3-controlplane.service",
        daemon_reload_index,
    )
    exchange_start_index = commands.index(
        "start trader-v3-exchange-state.service"
    )
    assert disable_index < disabled_index < daemon_reload_index
    assert daemon_reload_index < exchange_start_index < absent_index
    assert "start trader-v3-controlplane.service" not in commands
    assert "start trader-v3-exchange-state.service" in commands


def test_deploy_probes_versioned_sse_proxy_before_and_after_restart() -> None:
    text = _text()

    assert (
        'CONTROL_PLANE_SSE_PROXY_URL="${CONTROL_PLANE_SSE_PROXY_URL:-'
        'http://100.104.27.123:8088/v1/stream}"'
    ) in text
    before_probe = text.index(
        'probe_control_plane_sse_contract "before"'
    )
    state_snapshot = text.index(
        'CONTROL_PLANE_UNIT_LOAD_STATE="$(',
        before_probe,
    )
    control_plane_restart = text.index(
        'systemctl restart "$CONTROL_PLANE_UNIT"',
        state_snapshot,
    )
    after_probe = text.index(
        'probe_control_plane_sse_contract "after"',
        control_plane_restart,
    )
    patch_install = text.index(
        'target_path="$PATCH_DIR/$bundle_path"',
        after_probe,
    )

    assert before_probe < state_snapshot
    assert state_snapshot < control_plane_restart < after_probe
    assert after_probe < patch_install


def test_sse_proxy_probe_fails_closed_on_non_200(
    tmp_path: Path,
) -> None:
    result = _run_sse_probe(
        tmp_path,
        proxy_status="502",
        direct_status="401",
    )

    assert result.returncode != 0
    assert "proxy expected HTTP 200, got 502" in result.stderr


def test_sse_probe_fails_closed_when_direct_stream_is_authenticated(
    tmp_path: Path,
) -> None:
    result = _run_sse_probe(
        tmp_path,
        proxy_status="200",
        direct_status="200",
    )

    assert result.returncode != 0
    assert "direct control-plane SSE expected HTTP 401, got 200" in (
        result.stderr
    )


def test_sse_proxy_probe_accepts_proxy_200_and_direct_401(
    tmp_path: Path,
) -> None:
    result = _run_sse_probe(
        tmp_path,
        proxy_status="200",
        direct_status="401",
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_deploy_script_installs_versioned_controlplane_unit() -> None:
    text = _text()

    assert (
        'CONTROL_PLANE_UNIT_SOURCE_RELATIVE="infra/systemd/'
        'trader-v3-controlplane.service"'
    ) in text
    assert (
        'CONTROL_PLANE_UNIT_TARGET="/etc/systemd/system/'
        'trader-v3-controlplane.service"'
    ) in text
    assert '"--timeout-graceful-shutdown 10"' in text
    assert 'TimeoutStopSec=20s' in text
    assert 'KillMode=control-group' in text
    assert (
        'install -D -m 0644 "$CONTROL_PLANE_UNIT_SOURCE" '
        '"$CONTROL_PLANE_UNIT_TARGET"'
    ) in text
    assert (
        '"$CONTROL_PLANE_UNIT_SOURCE" "$CONTROL_PLANE_UNIT_TARGET"'
    ) in text
    assert 'systemctl enable "$CONTROL_PLANE_UNIT"' in text
    assert 'systemctl is-enabled --quiet "$CONTROL_PLANE_UNIT"' in text


def test_deploy_script_repairs_non_file_patch_sources() -> None:
    text = _text()

    assert "prepare_file_target()" in text
    assert '[ -e "$target" ] && [ ! -f "$target" ]' in text
    assert text.count('prepare_file_target "$target_path"') == 3


def test_deploy_script_verifies_sha_mounts_and_deleted_inodes() -> None:
    text = _text()

    assert 'sha256sum -c "$(basename "$CHECKSUMS")"' in text
    staging_verify = text.index(
        'sha256sum -c "$(basename "$CHECKSUMS")"'
    )
    recorder_process_verify = text.index(
        'python3 "$RECORDER_VERIFIER" process'
    )
    assert staging_verify < recorder_process_verify
    assert "container patch SHA256 mismatch" in text
    assert "container target SHA256 mismatch" in text
    assert "container mount mismatch" in text
    assert "bundle and recreate mount plans differ" in text
    assert "/proc/{int(sys.argv[2])}/mountinfo" in text
    assert "deleted inode mount detected" in text
    assert "account-a /ready did not report HALTED" in text
    assert "account-a Docker memory limit mismatch" in text
    assert "account-a Docker memory-swap limit mismatch" in text


def test_deploy_script_backs_up_and_applies_idempotent_schema_transition() -> None:
    text = _text()

    backup = text.index('DB_DUMP="$BACKUP_ROOT/postgres-pre-migration.dump"')
    migrate = text.index(
        '"$T/services/control-plane/db/migrate.py" up'
    )
    control_plane_restart = text.index(
        'systemctl restart "$CONTROL_PLANE_UNIT"',
        migrate,
    )
    patch_install = text.index(
        'target_path="$PATCH_DIR/$bundle_path"',
        control_plane_restart,
    )

    assert backup < migrate < control_plane_restart < patch_install
    assert "postgres-pre-migration.dump.sha256" in text
    assert "postgres-pre-migration.dump.list" in text
    assert "schema-migrations-before.json" in text
    assert "schema-migrations-after.json" in text
    assert "versions not in (legacy, target)" in text
    assert "expected_delta = set(expected_after) - before_versions" in text
    assert "delta != expected_delta" in text
    assert "unexpected pre-migration exact set" in text
    assert "unexpected post-migration exact set" in text
    assert "0005 trigger/function verification failed" in text
    assert "idx_execution_events_targeted_opening_evidence" in text
    assert "INSERT INTO schema_migrations" not in text


def test_deploy_script_supports_staging_and_trader_root_overrides() -> None:
    text = _text()

    assert 'T="${T:-/srv/trader-v3}"' in text
    assert 'STAGING="${STAGING:-$SCRIPT_DIR}"' in text
    assert 'MEMORY_LIMIT="${ACCOUNT_A_MEMORY_LIMIT:-768m}"' in text
    assert 'MEMORY_SWAP_LIMIT="${ACCOUNT_A_MEMORY_SWAP_LIMIT:-768m}"' in text
    assert 'RELEASE_COMMIT="$(' in text
    assert '"release_id": repo_commit' not in text


def test_deploy_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
