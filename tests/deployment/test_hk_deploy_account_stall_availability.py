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
    control_plane_restart = text.index(
        'systemctl restart "$CONTROL_PLANE_UNIT"',
        daemon_reload,
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
    assert daemon_reload < control_plane_restart
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
    assert 'docker inspect "$NODE_CONTAINER" >' in text
    assert 'printf \'absent\\t%s\\t-\\n\'' in text
    assert 'echo "ROLLBACK: bash $ROLLBACK_PATH"' in text
    assert 'cat >"$ROLLBACK_PATH"' in text
    assert 'rm -rf -- "$target"' in text
    assert text.count("systemctl daemon-reload") == 2


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
