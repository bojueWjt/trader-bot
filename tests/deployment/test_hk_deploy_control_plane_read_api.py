import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "hk-deploy-control-plane-read-api.sh"
READ_API_RELATIVE = Path("services/control-plane/api/read_api.py")
UNIT_RELATIVE = Path("infra/systemd/trader-v3-controlplane.service")
UNIT_NAME = "trader-v3-controlplane.service"


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _old_read_api() -> bytes:
    result = subprocess.run(
        [
            "git",
            "show",
            "9ef98fb:services/control-plane/api/read_api.py",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    return result.stdout


def _old_unit() -> bytes:
    payload = (
        "[Unit]\n"
        "Description=Trader v3 control-plane read API "
        "(isolated, 127.0.0.1:8080)\n"
        "After=docker.service network.target\n"
        "[Service]\n"
        "WorkingDirectory=/srv/trader-v3/services/control-plane/api\n"
        "EnvironmentFile=/srv/trader-v3/.env.v3\n"
        "ExecStart=/srv/trader-v3/.venv-cp/bin/uvicorn read_api:app "
        "--host 127.0.0.1 --port 8080\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    ).encode("utf-8")
    assert hashlib.sha256(payload).hexdigest() == (
        "cdce9a88f429b760df255ee09f17ae80b30a338d9cb3fe8169bc508718b90d0f"
    )
    return payload


def _fake_tools(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / "id",
        """#!/usr/bin/env bash
set -eu
if [ "${1:-}" = "-u" ]; then
  printf '0\n'
  exit 0
fi
exit 2
""",
    )
    _write_executable(
        fake_bin / "flock",
        """#!/usr/bin/env bash
set -eu
printf 'flock %s\n' "$*" >>"$COMMAND_LOG"
exit 0
""",
    )
    _write_executable(
        fake_bin / "install",
        """#!/usr/bin/env bash
set -eu
mode=""
source_path=""
target_path=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -D)
      shift
      ;;
    -m)
      mode="$2"
      shift 2
      ;;
    *)
      if [ -z "$source_path" ]; then
        source_path="$1"
      else
        target_path="$1"
      fi
      shift
      ;;
  esac
done
if [ -n "${FAKE_INSTALL_FAIL_TARGET:-}" ] \
  && [ "$target_path" = "$FAKE_INSTALL_FAIL_TARGET" ]; then
  exit 73
fi
mkdir -p "$(dirname "$target_path")"
cp "$source_path" "$target_path"
chmod "$mode" "$target_path"
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
set -eu
output=""
headers=""
write_out=""
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output)
      output="$2"
      shift 2
      ;;
    --dump-header)
      headers="$2"
      shift 2
      ;;
    --write-out)
      write_out="$2"
      shift 2
      ;;
    --connect-timeout|--max-time)
      shift 2
      ;;
    --silent|--show-error|--no-buffer)
      shift
      ;;
    *)
      url="$1"
      shift
      ;;
  esac
done
printf 'curl %s\n' "$url" >>"$COMMAND_LOG"
if [ "$url" = "$ACCOUNT_A_READY_URL" ]; then
  payload="${FAKE_READY_PAYLOAD:-{\\"ready\\":true,\\"trading_state\\":\\"HALTED\\"}}"
  if [ -n "$output" ] && [ "$output" != "/dev/null" ]; then
    printf '%s' "$payload" >"$output"
  else
    printf '%s' "$payload"
  fi
  exit 0
fi
if [ "$url" = "$CONTROL_PLANE_DIRECT_SSE_URL" ]; then
  status="${FAKE_DIRECT_STATUS:-401}"
  if [ -n "$write_out" ]; then
    printf '%s' "$status"
  fi
  exit 0
fi
status="${FAKE_PROXY_STATUS:-200}"
if [ -n "$headers" ]; then
  printf 'HTTP/1.1 %s OK\r\nContent-Type: text/event-stream\r\n\r\n' \
    "$status" >"$headers"
  printf 'event: dashboard_snapshot\ndata: {}\n\n' >"$output"
  touch "$FAKE_SSE_ACTIVE_FILE"
  trap 'rm -f "$FAKE_SSE_ACTIVE_FILE"; exit 0' TERM INT
  trap 'rm -f "$FAKE_SSE_ACTIVE_FILE"' EXIT
  while true; do
    sleep 0.1
  done
fi
if [ -n "$write_out" ]; then
  printf '%s' "$status"
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "journalctl",
        """#!/usr/bin/env bash
set -eu
printf 'journalctl %s\n' "$*" >>"$COMMAND_LOG"
printf '%s\n' "${FAKE_JOURNAL_TEXT:-clean shutdown}"
""",
    )
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
set -eu
load_state() {
  if [ -e "$FAKE_UNIT_TARGET" ] || [ -L "$FAKE_UNIT_TARGET" ]; then
    printf 'loaded\n'
    return
  fi
  if [ -n "$FAKE_VENDOR_FRAGMENT" ]; then
    printf 'loaded\n'
    return
  fi
  printf 'not-found\n'
}
fragment_path() {
  if [ -e "$FAKE_UNIT_TARGET" ] || [ -L "$FAKE_UNIT_TARGET" ]; then
    printf '%s\n' "$FAKE_UNIT_TARGET"
    return
  fi
  printf '%s\n' "$FAKE_VENDOR_FRAGMENT"
}
enablement_state() {
  if [ -L "$FAKE_WANTS_LINK" ]; then
    printf 'enabled\n'
    return
  fi
  if [ "$(load_state)" = "not-found" ]; then
    printf 'not-found\n'
    return
  fi
  printf 'disabled\n'
}
active_state() {
  if [ -e "$FAKE_ACTIVE_FILE" ]; then
    printf 'active\n'
    return
  fi
  printf 'inactive\n'
}
command="$1"
shift
printf 'systemctl %s' "$command" >>"$COMMAND_LOG"
if [ "$#" -gt 0 ]; then
  printf ' %s' "$*" >>"$COMMAND_LOG"
fi
printf '\n' >>"$COMMAND_LOG"
case "$command" in
  daemon-reload)
    ;;
  disable)
    rm -f "$FAKE_WANTS_LINK"
    ;;
  enable)
    [ "$(load_state)" = "loaded" ]
    mkdir -p "$(dirname "$FAKE_WANTS_LINK")"
    rm -f "$FAKE_WANTS_LINK"
    ln -s "$(fragment_path)" "$FAKE_WANTS_LINK"
    ;;
  is-enabled)
    state="$(enablement_state)"
    quiet=0
    for argument in "$@"; do
      if [ "$argument" = "--quiet" ]; then
        quiet=1
      fi
    done
    if [ "$quiet" -eq 0 ]; then
      printf '%s\n' "$state"
    fi
    [ "$state" = "enabled" ]
    ;;
  is-active)
    state="$(active_state)"
    quiet=0
    for argument in "$@"; do
      if [ "$argument" = "--quiet" ]; then
        quiet=1
      fi
    done
    if [ "$quiet" -eq 0 ]; then
      printf '%s\n' "$state"
    fi
    [ "$state" = "active" ]
    ;;
  restart)
    [ -e "$FAKE_SSE_ACTIVE_FILE" ]
    touch "$FAKE_RESTART_SAW_SSE"
    touch "$FAKE_ACTIVE_FILE"
    ;;
  start)
    [ "$(load_state)" = "loaded" ]
    touch "$FAKE_ACTIVE_FILE"
    ;;
  stop)
    if [ "$(load_state)" = "not-found" ]; then
      exit 5
    fi
    rm -f "$FAKE_ACTIVE_FILE"
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
      FragmentPath)
        fragment_path
        ;;
      LoadState)
        load_state
        ;;
      *)
        exit 3
        ;;
    esac
    ;;
  *)
    exit 4
    ;;
esac
""",
    )


def _prepare_harness(
    tmp_path: Path,
    *,
    unit_origin: str = "local",
    enablement: str = "enabled",
    active: bool = True,
) -> dict[str, object]:
    staging = tmp_path / "staging"
    trader_root = tmp_path / "trader"
    systemd_dir = tmp_path / "etc" / "systemd" / "system"
    read_source = staging / READ_API_RELATIVE
    unit_source = staging / UNIT_RELATIVE
    read_target = trader_root / READ_API_RELATIVE
    unit_target = systemd_dir / UNIT_NAME
    for source, target in (
        (REPO_ROOT / READ_API_RELATIVE, read_source),
        (REPO_ROOT / UNIT_RELATIVE, unit_source),
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    read_target.parent.mkdir(parents=True, exist_ok=True)
    read_target.write_bytes(_old_read_api())
    systemd_dir.mkdir(parents=True, exist_ok=True)

    vendor_fragment = ""
    if unit_origin == "local":
        unit_target.write_bytes(_old_unit())
    elif unit_origin == "vendor":
        vendor_path = (
            tmp_path
            / "usr"
            / "lib"
            / "systemd"
            / "system"
            / UNIT_NAME
        )
        vendor_path.parent.mkdir(parents=True, exist_ok=True)
        vendor_path.write_bytes(_old_unit())
        vendor_fragment = str(vendor_path)
    elif unit_origin != "absent":
        raise AssertionError(f"unsupported unit origin: {unit_origin}")

    wants_link = systemd_dir / "multi-user.target.wants" / UNIT_NAME
    if enablement == "enabled":
        wants_link.parent.mkdir(parents=True, exist_ok=True)
        fragment = unit_target
        if vendor_fragment:
            fragment = Path(vendor_fragment)
        wants_link.symlink_to(fragment)

    fake_state = tmp_path / "fake-state"
    fake_state.mkdir()
    active_file = fake_state / "active"
    if active:
        active_file.touch()

    fake_bin = tmp_path / "fake-bin"
    _fake_tools(fake_bin)
    command_log = tmp_path / "commands.log"
    command_log.touch()
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "T": str(trader_root),
            "STAGING": str(staging),
            "SYSTEMD_UNIT_DIR": str(systemd_dir),
            "OPERATION_LOCK": str(tmp_path / "operation.lock"),
            "ACCOUNT_A_READY_URL": "http://127.0.0.1:18081/ready",
            "CONTROL_PLANE_SSE_PROXY_URL": "http://proxy.test/v1/stream",
            "CONTROL_PLANE_DIRECT_SSE_URL": (
                "http://127.0.0.1:18080/v1/stream"
            ),
            "COMMAND_LOG": str(command_log),
            "FAKE_UNIT_TARGET": str(unit_target),
            "FAKE_VENDOR_FRAGMENT": vendor_fragment,
            "FAKE_WANTS_LINK": str(wants_link),
            "FAKE_ACTIVE_FILE": str(active_file),
            "FAKE_SSE_ACTIVE_FILE": str(fake_state / "sse-active"),
            "FAKE_RESTART_SAW_SSE": str(fake_state / "restart-saw-sse"),
        }
    )
    return {
        "env": environment,
        "trader_root": trader_root,
        "read_target": read_target,
        "unit_target": unit_target,
        "wants_link": wants_link,
        "vendor_fragment": vendor_fragment,
        "active_file": active_file,
        "restart_saw_sse": fake_state / "restart-saw-sse",
        "command_log": command_log,
    }


def _run_script(
    harness: dict[str, object],
) -> subprocess.CompletedProcess[str]:
    environment = harness["env"]
    assert isinstance(environment, dict)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _backup_root(harness: dict[str, object]) -> Path:
    trader_root = harness["trader_root"]
    assert isinstance(trader_root, Path)
    backups = list(
        (trader_root / "backups").glob("control-plane-read-api-*")
    )
    assert len(backups) == 1
    return backups[0]


def test_source_is_a_strict_two_file_deployment_allowlist() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert (
        'READ_API_RELATIVE="services/control-plane/api/read_api.py"'
        in text
    )
    assert (
        'UNIT_RELATIVE="infra/systemd/'
        'trader-v3-controlplane.service"'
    ) in text
    assert 'DEPLOYMENT_FILE_COUNT=2' in text
    assert "bundle-manifest" not in text
    assert "container-patches" not in text
    assert "account_a_live_trade" not in text
    assert "/v1/nodes/" not in text
    assert "/v1/orders" not in text
    assert "/v1/commands" not in text
    for forbidden in ("docker", "migration", "migrate", "recreate"):
        assert re.search(rf"\b{forbidden}\b", text, re.IGNORECASE) is None

    installs = re.findall(
        r'install -D -m 0[0-9]+ "([^"]+)" "([^"]+)"',
        text,
    )
    assert set(installs) == {
        ("$READ_API_SOURCE", "$READ_API_TARGET"),
        ("$UNIT_SOURCE", "$UNIT_TARGET"),
        ("$BACKUP_ROOT/read_api.py", "$READ_API_TARGET"),
    }


def test_script_has_versioned_sha_lock_and_restart_acceptance() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert (
        'OPERATION_LOCK="${OPERATION_LOCK:-/var/lock/'
        'trader-v3-account-stall-operation.lock}"'
    ) in text
    assert "953a429f3290e64409784e1fc9ce5e69f" in text
    assert "9a010de0d2486b37669ec8b293102aff7" in text
    assert "4226533812c96b267c929246752f4a70" in text
    assert "cdce9a88f429b760df255ee09f17ae8" in text
    assert "systemctl daemon-reload" in text
    assert 'systemctl enable "$UNIT_NAME"' in text
    assert 'systemctl restart "$UNIT_NAME"' in text
    assert "restart must complete in less than 15 seconds" in text
    assert "stop.*timed out" in text
    assert "SIGKILL" in text
    assert "status=9/KILL" in text


def test_success_holds_caddy_sse_during_restart_and_deploys_two_files(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path)
    old_read = _old_read_api()
    old_unit = _old_unit()

    result = _run_script(harness)

    assert result.returncode == 0, result.stdout + result.stderr
    read_target = harness["read_target"]
    unit_target = harness["unit_target"]
    assert isinstance(read_target, Path)
    assert isinstance(unit_target, Path)
    assert read_target.read_bytes() == (REPO_ROOT / READ_API_RELATIVE).read_bytes()
    assert unit_target.read_bytes() == (REPO_ROOT / UNIT_RELATIVE).read_bytes()
    assert read_target.read_bytes() != old_read
    assert unit_target.read_bytes() != old_unit
    restart_saw_sse = harness["restart_saw_sse"]
    assert isinstance(restart_saw_sse, Path)
    assert restart_saw_sse.exists()
    command_log = harness["command_log"]
    assert isinstance(command_log, Path)
    commands = command_log.read_text(encoding="utf-8")
    assert "curl http://proxy.test/v1/stream" in commands
    assert "curl http://127.0.0.1:18080/v1/stream" in commands
    assert "systemctl restart trader-v3-controlplane.service" in commands
    assert "journalctl -u trader-v3-controlplane.service" in commands
    assert "rollback.sh" in result.stdout


@pytest.mark.parametrize(
    ("unit_origin", "enablement", "active"),
    [
        ("local", "enabled", True),
        ("vendor", "disabled", False),
        ("absent", "disabled", False),
    ],
)
def test_rollback_restores_fragment_enablement_and_active_state(
    tmp_path: Path,
    unit_origin: str,
    enablement: str,
    active: bool,
) -> None:
    harness = _prepare_harness(
        tmp_path,
        unit_origin=unit_origin,
        enablement=enablement,
        active=active,
    )
    old_read = _old_read_api()
    old_unit = _old_unit()
    result = _run_script(harness)
    assert result.returncode == 0, result.stdout + result.stderr

    backup_root = _backup_root(harness)
    rollback = backup_root / "rollback.sh"
    environment = harness["env"]
    assert isinstance(environment, dict)
    rollback_result = subprocess.run(
        ["bash", str(rollback)],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert rollback_result.returncode == 0, (
        rollback_result.stdout + rollback_result.stderr
    )
    read_target = harness["read_target"]
    unit_target = harness["unit_target"]
    wants_link = harness["wants_link"]
    active_file = harness["active_file"]
    assert isinstance(read_target, Path)
    assert isinstance(unit_target, Path)
    assert isinstance(wants_link, Path)
    assert isinstance(active_file, Path)
    assert read_target.read_bytes() == old_read
    if unit_origin == "local":
        assert unit_target.read_bytes() == old_unit
    elif unit_origin == "vendor":
        vendor_fragment = harness["vendor_fragment"]
        assert isinstance(vendor_fragment, str)
        assert Path(vendor_fragment).read_bytes() == old_unit
        assert not unit_target.exists()
    else:
        assert not unit_target.exists()
    assert wants_link.is_symlink() is (enablement == "enabled")
    assert active_file.exists() is active

    command_log = harness["command_log"]
    assert isinstance(command_log, Path)
    commands = command_log.read_text(encoding="utf-8").splitlines()
    rollback_stop = len(commands) - 1 - commands[::-1].index(
        f"systemctl stop {UNIT_NAME}"
    )
    rollback_disable = commands.index(
        f"systemctl disable {UNIT_NAME}",
        rollback_stop,
    )
    rollback_reload = commands.index(
        "systemctl daemon-reload",
        rollback_disable,
    )
    assert rollback_disable < rollback_reload
    if unit_origin == "absent":
        assert not any(
            command == f"systemctl start {UNIT_NAME}"
            for command in commands[rollback_reload:]
        )


def test_rejects_unapproved_old_read_api_sha_before_mutation(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path)
    read_target = harness["read_target"]
    assert isinstance(read_target, Path)
    read_target.write_text("unapproved baseline\n", encoding="utf-8")

    result = _run_script(harness)

    assert result.returncode != 0
    assert "read API baseline SHA256 is not approved" in result.stderr
    trader_root = harness["trader_root"]
    assert isinstance(trader_root, Path)
    assert not (trader_root / "backups").exists()
    command_log = harness["command_log"]
    assert isinstance(command_log, Path)
    assert "systemctl restart" not in command_log.read_text(encoding="utf-8")


def test_rejects_modified_staging_sha_before_mutation(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path)
    environment = harness["env"]
    assert isinstance(environment, dict)
    staging = Path(environment["STAGING"])
    (staging / READ_API_RELATIVE).write_text(
        "modified staging\n",
        encoding="utf-8",
    )

    result = _run_script(harness)

    assert result.returncode != 0
    assert "staging read API SHA256 mismatch" in result.stderr
    command_log = harness["command_log"]
    assert isinstance(command_log, Path)
    assert "systemctl restart" not in command_log.read_text(encoding="utf-8")


def test_proxy_contract_failure_precedes_backup_and_mutation(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(tmp_path)
    environment = harness["env"]
    assert isinstance(environment, dict)
    environment["FAKE_PROXY_STATUS"] = "502"

    result = _run_script(harness)

    assert result.returncode != 0
    assert "SSE proxy expected HTTP 200, got 502" in result.stderr
    trader_root = harness["trader_root"]
    assert isinstance(trader_root, Path)
    assert not (trader_root / "backups").exists()


def test_partial_install_failure_rolls_back_when_original_unit_was_absent(
    tmp_path: Path,
) -> None:
    harness = _prepare_harness(
        tmp_path,
        unit_origin="absent",
        enablement="disabled",
        active=False,
    )
    environment = harness["env"]
    unit_target = harness["unit_target"]
    read_target = harness["read_target"]
    assert isinstance(environment, dict)
    assert isinstance(unit_target, Path)
    assert isinstance(read_target, Path)
    environment["FAKE_INSTALL_FAIL_TARGET"] = str(unit_target)
    old_read = _old_read_api()

    result = _run_script(harness)

    assert result.returncode != 0
    assert "automatic rollback completed" in result.stderr
    assert read_target.read_bytes() == old_read
    assert not unit_target.exists()
    command_log = harness["command_log"]
    assert isinstance(command_log, Path)
    commands = command_log.read_text(encoding="utf-8")
    assert f"systemctl stop {UNIT_NAME}" in commands
    assert f"systemctl disable {UNIT_NAME}" in commands


@pytest.mark.parametrize(
    "journal_text",
    [
        "State 'stop-sigterm' timed out. Killing.",
        "Main process exited, code=killed, status=9/KILL",
        "Killing process 42 with signal SIGKILL",
    ],
)
def test_bad_restart_journal_fails_and_automatically_rolls_back(
    tmp_path: Path,
    journal_text: str,
) -> None:
    harness = _prepare_harness(tmp_path)
    environment = harness["env"]
    assert isinstance(environment, dict)
    environment["FAKE_JOURNAL_TEXT"] = journal_text
    old_read = _old_read_api()

    result = _run_script(harness)

    assert result.returncode != 0
    assert "unsafe control-plane stop found in journal" in result.stderr
    assert "automatic rollback completed" in result.stderr
    read_target = harness["read_target"]
    assert isinstance(read_target, Path)
    assert read_target.read_bytes() == old_read
    wants_link = harness["wants_link"]
    assert isinstance(wants_link, Path)
    assert wants_link.is_symlink()


def test_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
