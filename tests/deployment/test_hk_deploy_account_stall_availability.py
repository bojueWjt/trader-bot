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
    control_plane_restart = text.index(
        'systemctl restart "$CONTROL_PLANE_UNIT"',
        host_install,
    )
    port_8080_check = text.index(
        '"http://127.0.0.1:8080/openapi.json"',
        control_plane_restart,
    )
    patch_install = text.index(
        'target_path="$PATCH_DIR/$bundle_path"',
        port_8080_check,
    )
    recreate = text.index('bash "$RECREATE_TARGET"', patch_install)

    assert host_install < control_plane_restart
    assert control_plane_restart < port_8080_check
    assert port_8080_check < patch_install
    assert patch_install < recreate


def test_deploy_script_keeps_lock_backup_and_precise_rollback() -> None:
    text = _text()

    assert (
        'OPERATION_LOCK="/var/lock/'
        'trader-v3-account-stall-operation.lock"'
    ) in text
    assert 'flock -n 9 || die "another operation holds $OPERATION_LOCK"' in text
    assert 'backup_target "$PATCH_DIR/$bundle_path"' in text
    assert 'backup_target "$T/$target_relative"' in text
    assert 'backup_target "$RECREATE_TARGET"' in text
    assert 'backup_target "$DEPLOYED_COMMIT_TARGET"' in text
    assert 'docker inspect "$NODE_CONTAINER" >' in text
    assert 'printf \'absent\\t%s\\t-\\n\'' in text
    assert 'echo "ROLLBACK: bash $ROLLBACK_PATH"' in text
    assert 'cat >"$ROLLBACK_PATH"' in text
    assert 'rm -rf -- "$target"' in text


def test_deploy_script_repairs_non_file_patch_sources() -> None:
    text = _text()

    assert "prepare_file_target()" in text
    assert '[ -e "$target" ] && [ ! -f "$target" ]' in text
    assert text.count('prepare_file_target "$target_path"') == 3


def test_deploy_script_verifies_sha_mounts_and_deleted_inodes() -> None:
    text = _text()

    assert 'sha256sum -c "$(basename "$CHECKSUMS")"' in text
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
