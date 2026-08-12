from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "hk-control-plane-isolation.sh"
DEPLOY = REPO_ROOT / "scripts" / "hk-deploy-20260803.sh"
RESOURCE_ROOT = REPO_ROOT / "infra" / "systemd"
LEGACY_UNIT = "trader-v3-controlplane.service"
ROLE_UNITS = (
    "trader-v3-controlplane-node-control.service",
    "trader-v3-controlplane-event-ingest.service",
    "trader-v3-controlplane-operator-query.service",
)
ROLE_IDENTITIES = (
    (
        "trader-v3-cp-node-control",
        "trader-v3-cp-node-control",
        "node-control.env",
        "trader_v3_node_control",
    ),
    (
        "trader-v3-cp-event-ingest",
        "trader-v3-cp-event-ingest",
        "event-ingest.env",
        "trader_v3_event_ingest",
    ),
    (
        "trader-v3-cp-operator-query",
        "trader-v3-cp-operator-query",
        "operator-query.env",
        "trader_v3_operator_query",
    ),
)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _write_fake_systemctl(bin_dir: Path) -> None:
    _write_executable(
        bin_dir / "systemctl",
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

state_root = Path(os.environ["FAKE_SYSTEMD_STATE"])
state_root.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
command = args.pop(0)
quiet = False
now = False
filtered = []
for arg in args:
    if arg == "--quiet":
        quiet = True
    elif arg == "--now":
        now = True
    else:
        filtered.append(arg)
args = filtered

def state_path(kind, unit):
    return state_root / f"{kind}.{unit}"

def read_state(kind, unit, default):
    path = state_path(kind, unit)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return default

def write_state(kind, unit, value):
    state_path(kind, unit).write_text(value + "\\n", encoding="utf-8")

if command == "daemon-reload":
    count_path = state_root / "daemon-reload-count"
    count = 0
    if count_path.exists():
        count = int(count_path.read_text(encoding="utf-8").strip())
    count += 1
    count_path.write_text(f"{count}\\n", encoding="utf-8")
    fail_at = int(os.environ.get("FAKE_FAIL_DAEMON_RELOAD_AT", "0"))
    if fail_at == count:
        sys.exit(1)
    sys.exit(0)

unit = args[0]
if command == "is-enabled":
    value = read_state("enabled", unit, "disabled")
    if not quiet:
        print(value)
    sys.exit(0 if value == "enabled" else 1)
if command == "is-active":
    value = read_state("active", unit, "inactive")
    if not quiet:
        print(value)
    sys.exit(0 if value == "active" else 3)
if command == "enable":
    write_state("enabled", unit, "enabled")
    if now:
        write_state("active", unit, "active")
    sys.exit(0)
if command == "disable":
    write_state("enabled", unit, "disabled")
    if now:
        write_state("active", unit, "inactive")
    sys.exit(0)
if command == "start":
    write_state("active", unit, "active")
    sys.exit(0)
if command == "stop":
    write_state("active", unit, "inactive")
    sys.exit(0)
if command == "reload":
    marker = state_root / "caddy-reload-failed"
    if (
        unit == "caddy.service"
        and os.environ.get("FAKE_FAIL_CADDY_RELOAD_ONCE") == "1"
        and not marker.exists()
    ):
        marker.write_text("failed\\n", encoding="utf-8")
        sys.exit(1)
    sys.exit(0)
raise SystemExit(f"unexpected systemctl invocation: {command} {args}")
""",
    )


def _deploy_function_source(name: str) -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _isolation_function_source(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _write_fixture(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    trader_root = tmp_path / "trader"
    systemd_root = tmp_path / "systemd"
    fake_bin = tmp_path / "bin"
    state_root = tmp_path / "systemd-state"
    backup_root = tmp_path / "rollback-artifact"
    caddy_file = tmp_path / "Caddyfile"
    resource_root = tmp_path / "resources"
    operation_lock = tmp_path / "account-stall-operation.lock"
    secret_root = trader_root / "secrets" / "control-plane"

    systemd_root.mkdir()
    fake_bin.mkdir()
    state_root.mkdir()
    resource_root.mkdir()
    secret_root.mkdir(parents=True)
    (trader_root / "services/control-plane/api").mkdir(parents=True)
    (trader_root / "services/control-plane/db").mkdir(parents=True)
    (trader_root / ".venv-cp/bin").mkdir(parents=True)
    (trader_root / ".env.v3").write_text("DATABASE_URL=test\n", encoding="utf-8")
    (trader_root / "services/control-plane/api/read_api.py").write_text(
        "# fixture\n",
        encoding="utf-8",
    )
    (trader_root / "services/control-plane/api/app_roles.py").write_text(
        "# fixture\n",
        encoding="utf-8",
    )
    (trader_root / "services/control-plane/db/pools.py").write_text(
        "# fixture\n",
        encoding="utf-8",
    )
    (trader_root / "services/control-plane/db/migrate.py").write_text(
        "# fixture\n",
        encoding="utf-8",
    )
    _write_executable(
        trader_root / ".venv-cp/bin/uvicorn",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        trader_root / ".venv-cp/bin/python",
        """#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *"maintenance-fence acquire"*)
    printf '{"fence_id":"00000000-0000-4000-8000-000000000001"}\n'
    ;;
  *"maintenance-fence verify"*|*"maintenance-fence release"*)
    ;;
  *)
    cat >/dev/null
    ;;
esac
""",
    )
    _write_fake_systemctl(fake_bin)
    _write_executable(
        fake_bin / "getent",
        """#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  passwd)
    printf '%s:x:2000:2000::/nonexistent:/usr/sbin/nologin\n' "$2"
    ;;
  group)
    printf '%s:x:2000:\n' "$2"
    ;;
  *)
    exit 2
    ;;
esac
""",
    )
    _write_executable(
        fake_bin / "flock",
        """#!/usr/bin/env bash
if [ "${FAKE_FLOCK_CONFLICT:-0}" = "1" ]; then
  exit 1
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "timeout",
        """#!/usr/bin/env bash
while [[ "${1:-}" == --kill-after=* ]]; do
  shift
done
shift
exec "$@"
""",
    )
    _write_executable(
        fake_bin / "caddy",
        "#!/usr/bin/env bash\n[ \"$1\" = validate ]\nexit $?\n",
    )
    _write_executable(
        fake_bin / "ss",
        """#!/usr/bin/env bash
printf 'LISTEN 0 4096 127.0.0.1:8080 0.0.0.0:*\\n'
""",
    )

    caddy_file.write_text(
        "{\n\tadmin off\n}\n\nhttp://127.0.0.1:8080 {\n\trespond 200\n}\n",
        encoding="utf-8",
    )
    (systemd_root / LEGACY_UNIT).write_text(
        "[Service]\nExecStart=/srv/legacy\n",
        encoding="utf-8",
    )
    (systemd_root / ROLE_UNITS[0]).write_text(
        "[Service]\nExecStart=/srv/preexisting-node-control\n",
        encoding="utf-8",
    )
    (state_root / f"enabled.{LEGACY_UNIT}").write_text(
        "enabled\n",
        encoding="utf-8",
    )
    (state_root / f"active.{LEGACY_UNIT}").write_text(
        "active\n",
        encoding="utf-8",
    )
    (state_root / f"enabled.{ROLE_UNITS[0]}").write_text(
        "disabled\n",
        encoding="utf-8",
    )
    (state_root / f"active.{ROLE_UNITS[0]}").write_text(
        "inactive\n",
        encoding="utf-8",
    )
    (state_root / "active.caddy.service").write_text(
        "active\n",
        encoding="utf-8",
    )
    for name in (
        "account-stall-control-plane-writer.conf",
        "account-stall-control-plane-reader.conf",
    ):
        (resource_root / name).write_text(
            (RESOURCE_ROOT / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    for _user, _group, filename, database_role in ROLE_IDENTITIES:
        env_file = secret_root / filename
        env_file.write_text(
            "DATABASE_URL="
            f"postgresql://{database_role}:fixture@127.0.0.1/trader\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "TRADER_ROOT": str(trader_root),
            "SYSTEMD_ROOT": str(systemd_root),
            "CADDY_FILE": str(caddy_file),
            "CONTROL_PLANE_ISOLATION_BACKUP_ROOT": str(backup_root),
            "ACCOUNT_STALL_SYSTEMD_RESOURCE_ROOT": str(resource_root),
            "ACCOUNT_STALL_OPERATION_LOCK": str(operation_lock),
            "FAKE_SYSTEMD_STATE": str(state_root),
            "FAKE_FAIL_CADDY_RELOAD_ONCE": "1",
            "FAKE_FAIL_DAEMON_RELOAD_AT": "0",
            "FAKE_FLOCK_CONFLICT": "0",
            "CONTROL_PLANE_SECRET_OWNER_UID": str(os.getuid()),
            "CONTROL_PLANE_SECRET_OWNER_GID": str(os.getgid()),
        }
    )
    return env, systemd_root, state_root, caddy_file


def _read_state(state_root: Path, kind: str, unit: str) -> str:
    return (state_root / f"{kind}.{unit}").read_text(
        encoding="utf-8"
    ).strip()


def _assert_original_topology(
    systemd_root: Path,
    state_root: Path,
    caddy_file: Path,
    original_caddy: bytes,
    original_legacy: bytes,
    original_role: bytes,
) -> None:
    assert (systemd_root / LEGACY_UNIT).read_bytes() == original_legacy
    assert (systemd_root / ROLE_UNITS[0]).read_bytes() == original_role
    assert not (systemd_root / ROLE_UNITS[1]).exists()
    assert not (systemd_root / ROLE_UNITS[2]).exists()
    assert caddy_file.read_bytes() == original_caddy
    assert _read_state(state_root, "enabled", LEGACY_UNIT) == "enabled"
    assert _read_state(state_root, "active", LEGACY_UNIT) == "active"
    assert _read_state(state_root, "enabled", ROLE_UNITS[0]) == "disabled"
    assert _read_state(state_root, "active", ROLE_UNITS[0]) == "inactive"
    for unit in ROLE_UNITS[1:]:
        assert _read_state(state_root, "enabled", unit) == "disabled"
        assert _read_state(state_root, "active", unit) == "inactive"


def test_failure_restores_exact_topology_and_rollback_is_idempotent(
    tmp_path: Path,
) -> None:
    env, systemd_root, state_root, caddy_file = _write_fixture(tmp_path)
    backup_root = Path(env["CONTROL_PLANE_ISOLATION_BACKUP_ROOT"])
    original_caddy = caddy_file.read_bytes()
    original_legacy = (systemd_root / LEGACY_UNIT).read_bytes()
    original_role = (systemd_root / ROLE_UNITS[0]).read_bytes()

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    _assert_original_topology(
        systemd_root,
        state_root,
        caddy_file,
        original_caddy,
        original_legacy,
        original_role,
    )
    assert (backup_root / "topology.tsv").is_file()
    assert (backup_root / "Caddyfile.before").is_file()
    assert (backup_root / "rollback-command.txt").is_file()
    assert not list(backup_root.parent.glob(".control-plane-isolation.tmp.*"))

    for _ in range(2):
        rollback = subprocess.run(
            ["bash", str(SCRIPT), "--rollback", str(backup_root)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert rollback.returncode == 0, rollback.stderr
        assert "CONTROL_PLANE_ISOLATION_ROLLBACK_OK" in rollback.stdout

    _assert_original_topology(
        systemd_root,
        state_root,
        caddy_file,
        original_caddy,
        original_legacy,
        original_role,
    )


def test_success_artifact_restores_topology_for_outer_deploy_rollback(
    tmp_path: Path,
) -> None:
    env, systemd_root, state_root, caddy_file = _write_fixture(tmp_path)
    env["FAKE_FAIL_CADDY_RELOAD_ONCE"] = "0"
    backup_root = Path(env["CONTROL_PLANE_ISOLATION_BACKUP_ROOT"])
    original_caddy = caddy_file.read_bytes()
    original_legacy = (systemd_root / LEGACY_UNIT).read_bytes()
    original_role = (systemd_root / ROLE_UNITS[0]).read_bytes()

    migration = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert migration.returncode == 0, migration.stderr
    assert "CONTROL_PLANE_ISOLATION_OK" in migration.stdout
    assert (
        f"rollback_artifact={backup_root}/rollback-command.txt"
        in migration.stdout
    )
    assert caddy_file.read_bytes() != original_caddy
    assert _read_state(state_root, "enabled", LEGACY_UNIT) == "disabled"
    assert _read_state(state_root, "active", LEGACY_UNIT) == "inactive"
    for unit in ROLE_UNITS:
        assert (systemd_root / unit).is_file()
        assert _read_state(state_root, "enabled", unit) == "enabled"
        assert _read_state(state_root, "active", unit) == "active"
        unit_text = (systemd_root / unit).read_text(encoding="utf-8")
        assert "MemoryMax=" in unit_text
        assert "MemorySwapMax=0" in unit_text
        assert "CPUQuota=" in unit_text
        assert "TasksMax=" in unit_text
        assert "LimitNOFILE=" in unit_text
        assert "Restart=on-failure" in unit_text
    secret_root = Path(env["TRADER_ROOT"]) / "secrets" / "control-plane"
    unit_texts = [
        (systemd_root / unit).read_text(encoding="utf-8")
        for unit in ROLE_UNITS
    ]
    for unit_text, identity in zip(
        unit_texts,
        ROLE_IDENTITIES,
        strict=True,
    ):
        user, group, env_filename, _database_role = identity
        assert f"User={user}" in unit_text
        assert f"Group={group}" in unit_text
        assert f"EnvironmentFile={secret_root / env_filename}" in unit_text
    assert len(
        {
            line
            for unit_text in unit_texts
            for line in unit_text.splitlines()
            if line.startswith("EnvironmentFile=")
        }
    ) == 3
    assert not list(backup_root.parent.glob(".control-plane-isolation.tmp.*"))

    rollback = subprocess.run(
        ["bash", str(SCRIPT), "--rollback", str(backup_root)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert rollback.returncode == 0, rollback.stderr
    assert "CONTROL_PLANE_ISOLATION_ROLLBACK_OK" in rollback.stdout
    _assert_original_topology(
        systemd_root,
        state_root,
        caddy_file,
        original_caddy,
        original_legacy,
        original_role,
    )


def test_global_operation_lock_conflict_prevents_snapshot_and_mutation(
    tmp_path: Path,
) -> None:
    env, systemd_root, state_root, caddy_file = _write_fixture(tmp_path)
    env["FAKE_FLOCK_CONFLICT"] = "1"
    backup_root = Path(env["CONTROL_PLANE_ISOLATION_BACKUP_ROOT"])
    original_caddy = caddy_file.read_bytes()
    original_legacy = (systemd_root / LEGACY_UNIT).read_bytes()
    original_role = (systemd_root / ROLE_UNITS[0]).read_bytes()

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "another account-stall operation holds" in result.stderr
    assert not backup_root.exists()
    assert caddy_file.read_bytes() == original_caddy
    assert (systemd_root / LEGACY_UNIT).read_bytes() == original_legacy
    assert (systemd_root / ROLE_UNITS[0]).read_bytes() == original_role
    assert _read_state(state_root, "enabled", LEGACY_UNIT) == "enabled"
    assert _read_state(state_root, "active", LEGACY_UNIT) == "active"


def test_missing_resource_budget_fails_before_snapshot(tmp_path: Path) -> None:
    env, systemd_root, state_root, caddy_file = _write_fixture(tmp_path)
    resource_root = Path(env["ACCOUNT_STALL_SYSTEMD_RESOURCE_ROOT"])
    writer_conf = resource_root / "account-stall-control-plane-writer.conf"
    writer_conf.write_text(
        writer_conf.read_text(encoding="utf-8").replace(
            "MemoryMax=768M\n",
            "",
        ),
        encoding="utf-8",
    )
    backup_root = Path(env["CONTROL_PLANE_ISOLATION_BACKUP_ROOT"])

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "resource drop-in lacks MemoryMax" in result.stderr
    assert not backup_root.exists()
    assert _read_state(state_root, "enabled", LEGACY_UNIT) == "enabled"
    assert _read_state(state_root, "active", LEGACY_UNIT) == "active"
    assert (systemd_root / LEGACY_UNIT).is_file()
    assert caddy_file.is_file()


def test_reused_database_role_url_fails_before_snapshot(tmp_path: Path) -> None:
    env, _systemd_root, state_root, _caddy_file = _write_fixture(tmp_path)
    secret_root = Path(env["TRADER_ROOT"]) / "secrets" / "control-plane"
    node_url = (secret_root / "node-control.env").read_text(encoding="utf-8")
    (secret_root / "event-ingest.env").write_text(
        node_url,
        encoding="utf-8",
    )
    (secret_root / "event-ingest.env").chmod(0o600)
    backup_root = Path(env["CONTROL_PLANE_ISOLATION_BACKUP_ROOT"])

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "DATABASE_URL role mismatch" in result.stderr
    assert not backup_root.exists()
    assert _read_state(state_root, "active", LEGACY_UNIT) == "active"


def test_secret_mode_contract_fails_before_snapshot(tmp_path: Path) -> None:
    env, _systemd_root, state_root, _caddy_file = _write_fixture(tmp_path)
    secret = (
        Path(env["TRADER_ROOT"])
        / "secrets"
        / "control-plane"
        / "operator-query.env"
    )
    secret.chmod(0o644)
    backup_root = Path(env["CONTROL_PLANE_ISOLATION_BACKUP_ROOT"])

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "secret mode mismatch" in result.stderr
    assert not backup_root.exists()
    assert _read_state(state_root, "active", LEGACY_UNIT) == "active"


def test_secret_owner_contract_fails_before_snapshot(tmp_path: Path) -> None:
    env, _systemd_root, state_root, _caddy_file = _write_fixture(tmp_path)
    env["CONTROL_PLANE_SECRET_OWNER_UID"] = str(os.getuid() + 1)
    backup_root = Path(env["CONTROL_PLANE_ISOLATION_BACKUP_ROOT"])

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "secret owner mismatch" in result.stderr
    assert not backup_root.exists()
    assert _read_state(state_root, "active", LEGACY_UNIT) == "active"


def test_missing_role_secret_fails_before_snapshot(tmp_path: Path) -> None:
    env, _systemd_root, state_root, _caddy_file = _write_fixture(tmp_path)
    secret = (
        Path(env["TRADER_ROOT"])
        / "secrets"
        / "control-plane"
        / "event-ingest.env"
    )
    secret.unlink()
    backup_root = Path(env["CONTROL_PLANE_ISOLATION_BACKUP_ROOT"])

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "event-ingest secret is unavailable" in result.stderr
    assert not backup_root.exists()
    assert _read_state(state_root, "active", LEGACY_UNIT) == "active"


def test_duplicate_service_identity_fails_before_snapshot(
    tmp_path: Path,
) -> None:
    env, _systemd_root, state_root, _caddy_file = _write_fixture(tmp_path)
    env["CONTROL_PLANE_EVENT_INGEST_USER"] = (
        "trader-v3-cp-node-control"
    )
    backup_root = Path(env["CONTROL_PLANE_ISOLATION_BACKUP_ROOT"])

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "service users must be independent" in result.stderr
    assert not backup_root.exists()
    assert _read_state(state_root, "active", LEGACY_UNIT) == "active"


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("flock") is None,
    reason="requires Linux flock and /proc fd identity",
)
def test_deploy_lock_is_inherited_by_isolation_and_rollback(
    tmp_path: Path,
) -> None:
    env, _systemd_root, _state_root, _caddy_file = _write_fixture(tmp_path)
    env["FAKE_FAIL_CADDY_RELOAD_ONCE"] = "0"
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    fake_flock = fake_bin / "flock"
    fake_flock.unlink()
    fake_flock.symlink_to(shutil.which("flock"))
    env["ISOLATION_SCRIPT"] = str(SCRIPT)
    env["ROLLBACK_ROOT"] = env["CONTROL_PLANE_ISOLATION_BACKUP_ROOT"]
    source = (
        "set -Eeuo pipefail\n"
        + _deploy_function_source("acquire_account_stall_operation_lock")
        + """
acquire_account_stall_operation_lock
bash "$ISOLATION_SCRIPT"
bash "$ISOLATION_SCRIPT" --rollback "$ROLLBACK_ROOT"
"""
    )

    result = subprocess.run(
        ["bash", "-c", source],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "CONTROL_PLANE_ISOLATION_OK" in result.stdout
    assert "CONTROL_PLANE_ISOLATION_ROLLBACK_OK" in result.stdout


def test_rollback_failure_stops_all_control_plane_roles(tmp_path: Path) -> None:
    env, _systemd_root, state_root, _caddy_file = _write_fixture(tmp_path)
    env["FAKE_FAIL_DAEMON_RELOAD_AT"] = "2"

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 3
    assert "services remain stopped" in result.stderr
    for unit in (LEGACY_UNIT, *ROLE_UNITS):
        assert _read_state(state_root, "enabled", unit) == "disabled"
        assert _read_state(state_root, "active", unit) == "inactive"


@pytest.mark.parametrize(
    ("running_node", "live_epoch", "expected_error"),
    (
        ("", "epoch-fixture", ""),
        (
            "trader-v3-node-c",
            "epoch-fixture",
            "bootstrap control-plane mutation requires stopped node",
        ),
        (
            "",
            "different-epoch",
            "bootstrap Redis fencing epoch marker changed",
        ),
    ),
)
def test_bootstrap_stopped_gate_binds_all_nodes_and_redis_epoch(
    tmp_path: Path,
    running_node: str,
    live_epoch: str,
    expected_error: str,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"
if [ "$1" = "inspect" ]; then
  node="${@: -1}"
  if [ -n "${FAKE_RUNNING_NODE:-}" ] \
    && [ "$node" = "$FAKE_RUNNING_NODE" ]; then
    printf 'true\n'
  else
    printf 'false\n'
  fi
  exit 0
fi
if [ "$1" = "exec" ] && [ "$2" = "trader-v3-redis" ]; then
  printf '%s\n' "$FAKE_REDIS_EPOCH"
  exit 0
fi
exit 64
""",
    )
    source = "\n".join(
        (
            _isolation_function_source("die"),
            _isolation_function_source("verify_bootstrap_stopped_gate"),
            """
verify_account_stall_operation_lock() { return 0; }
BOOTSTRAP_STOPPED_GATE=1
BOOTSTRAP_REDIS_FENCING_EPOCH=epoch-fixture
REDIS_FENCING_EPOCH_KEY=trader-v3:fencing-epoch
verify_bootstrap_stopped_gate fixture-stage
""",
        )
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_RUNNING_NODE": running_node,
            "FAKE_REDIS_EPOCH": live_epoch,
        }
    )

    result = subprocess.run(
        ["bash", "-c", f"set -Eeuo pipefail\n{source}"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    if expected_error:
        assert result.returncode != 0
        assert expected_error in result.stderr
        return

    assert result.returncode == 0, result.stderr
    log = docker_log.read_text(encoding="utf-8")
    for node in (
        "trader-v3-node-a",
        "trader-v3-node-b",
        "trader-v3-node-c",
        "trader-v3-node-d",
    ):
        assert f"inspect --format {{{{.State.Running}}}} {node}" in log
    assert (
        "exec trader-v3-redis redis-cli --raw GET "
        "trader-v3:fencing-epoch"
    ) in log


def test_control_plane_isolation_script_is_fail_closed() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    text = SCRIPT.read_text(encoding="utf-8")

    assert '"${ROLE_UNITS[0]}"' in text
    assert '"${ROLE_UNITS[1]}"' in text
    assert '"${ROLE_UNITS[2]}"' in text
    assert "Environment=CONTROL_PLANE_APP_ROLE=$role" in text
    assert "127.0.0.1:$NODE_PORT" in text
    assert "127.0.0.1:$EVENT_PORT" in text
    assert "127.0.0.1:$OPERATOR_PORT" in text
    assert 'run_timed systemctl stop "$LEGACY_UNIT"' in text
    assert "run_timed systemctl reload caddy.service" in text
    assert "event_ingest ^/v1/nodes/" in text
    assert "node_control ^/v1/nodes/" in text
    assert "account_generated ^/v1/accounts/" in text
    assert "(?:" not in text
    assert "trap rollback EXIT" in text
    assert "--rollback" in text
    assert (
        'DEFAULT_OPERATION_LOCK="/var/lock/'
        'trader-v3-account-stall-operation.lock"'
        in text
    )
    assert "ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP" in text
    assert "ACCOUNT_STALL_OPERATION_LOCK_TOKEN" in text
    assert "validate_role_identity_contract" in text
    assert "User=$service_user" in text
    assert "Group=$service_group" in text
    assert "EnvironmentFile=$env_file" in text
    assert "restore_topology_fail_closed" in text
    assert "fail_closed_control_plane" in text
    assert '--kill-after="${TIMEOUT_KILL_AFTER_SECONDS}s"' in text
    assert "run_timed caddy validate" in text
    assert "account-stall-control-plane-writer.conf" in text
    assert "account-stall-control-plane-reader.conf" in text
    assert "CONTROL_PLANE_ISOLATION_OK" in text
