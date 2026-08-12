from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import uuid
from hashlib import sha256
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "hk-redis-rebaseline.sh"
CAPACITY_PLANNER = REPO_ROOT / "scripts" / "redis_capacity_config.py"
DEPLOY = REPO_ROOT / "scripts" / "hk-deploy-20260803.sh"
STAMP = "20260808T120000Z"
REDIS_CONTAINER = "trader-v3-redis"
LEGACY_CONTAINER = f"{REDIS_CONTAINER}-legacy-{STAMP}"
NEW_VOLUME = f"{REDIS_CONTAINER}-hardening-{STAMP}"
REDIS_FENCING_EPOCH_KEY = "trader-bot:redis-fencing-epoch"
NODE_CONTAINERS = (
    "trader-v3-node-a",
    "trader-v3-node-b",
    "trader-v3-node-c",
    "trader-v3-node-d",
)
NODE_IDS = {
    "trader-v3-node-a": "node-a-id",
    "trader-v3-node-b": "node-b-id",
    "trader-v3-node-c": "node-c-id",
    "trader-v3-node-d": "node-d-id",
}


FAKE_DOCKER = r"""#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


state_path = Path(os.environ["FAKE_DOCKER_STATE"])
log_path = Path(os.environ["FAKE_DOCKER_LOG"])
volume_root = Path(os.environ["FAKE_DOCKER_VOLUME_ROOT"])
fail_at = os.environ.get("FAKE_DOCKER_FAIL_AT", "")


def load_state():
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state):
    state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def log(message):
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def fail(stage):
    if fail_at == stage:
        print(f"injected failure: {stage}", file=sys.stderr)
        raise SystemExit(42)


def require_container(state, name):
    container = state["containers"].get(name)
    if container is None:
        raise SystemExit(1)
    return container


def inspect_value(container, format_string):
    if format_string == "{{.Id}}":
        return container["id"]
    if format_string == "{{.Image}}":
        return container["image"]
    if format_string == "{{.Config.Image}}":
        return container["config_image"]
    if format_string == "{{.State.Running}}":
        if container["running"]:
            return "true"
        return "false"
    if format_string == "{{.State.Pid}}":
        return str(container["pid"])
    if format_string == "{{.HostConfig.Memory}}":
        return str(container.get("memory", 0))
    if format_string == "{{.HostConfig.MemorySwap}}":
        return str(container.get("memory_swap", 0))
    if format_string == "{{.HostConfig.NanoCpus}}":
        return str(container.get("nano_cpus", 0))
    if format_string == "{{.HostConfig.PidsLimit}}":
        return str(container.get("pids_limit", 0))
    if "HostConfig.Ulimits" in format_string:
        return str(container.get("nofile", ""))
    if format_string == "{{.HostConfig.RestartPolicy.Name}}":
        return str(container.get("restart_policy", ""))
    if "Config.Labels" in format_string:
        labels = container.get("labels", {})
        return labels.get("trader-v3.redis-rebaseline", "")
    if ".Mounts" in format_string:
        for mount in container.get("mounts", []):
            if mount["Destination"] != "/data":
                continue
            rw = "false"
            if mount["RW"]:
                rw = "true"
            return "|".join(
                [
                    mount["Type"],
                    mount["Name"],
                    mount["Source"],
                    rw,
                ]
            )
        return ""
    print(f"unsupported inspect format: {format_string}", file=sys.stderr)
    raise SystemExit(2)


args = sys.argv[1:]
if not args:
    raise SystemExit(2)
command = args.pop(0)
state = load_state()

if command == "inspect":
    format_string = ""
    if args and args[0] == "--format":
        format_string = args[1]
        args = args[2:]
    if format_string:
        container = require_container(state, args[0])
        print(inspect_value(container, format_string))
        raise SystemExit(0)
    for name in args:
        require_container(state, name)
    print("[]")
    raise SystemExit(0)

if command == "top":
    container = require_container(state, args[0])
    if container["running"]:
        print("PID CMD")
        print(f'{container["pid"]} fake-writer')
        raise SystemExit(0)
    raise SystemExit(1)

if command == "stop":
    name = args[-1]
    container = require_container(state, name)
    if name == "trader-v3-redis" and container["id"] == "source-id":
        fail("source_stop")
    container["running"] = False
    container["pid"] = 0
    save_state(state)
    log(f'stop:{name}:{container["id"]}')
    print(name)
    raise SystemExit(0)

if command == "start":
    name = args[-1]
    container = require_container(state, name)
    container["running"] = True
    container["pid"] = 9000 + len(state["containers"])
    if container["id"] == "source-id":
        container["redis"]["run_id"] = "restored-source-run"
    save_state(state)
    log(f'start:{name}:{container["id"]}')
    print(name)
    raise SystemExit(0)

if command == "rename":
    fail("rename")
    source_name, target_name = args
    if target_name in state["containers"]:
        raise SystemExit(1)
    container = require_container(state, source_name)
    del state["containers"][source_name]
    state["containers"][target_name] = container
    save_state(state)
    log(f'rename:{source_name}:{target_name}:{container["id"]}')
    raise SystemExit(0)

if command == "rm":
    name = args[-1]
    container = require_container(state, name)
    del state["containers"][name]
    save_state(state)
    log(f'rm:{name}:{container["id"]}')
    print(name)
    raise SystemExit(0)

if command == "volume":
    subcommand = args.pop(0)
    if subcommand == "inspect":
        format_string = ""
        if args and args[0] == "--format":
            format_string = args[1]
            args = args[2:]
        volume = state["volumes"].get(args[0])
        if volume is None:
            raise SystemExit(1)
        if "Mountpoint" in format_string:
            print(volume["mountpoint"])
        elif ".Labels" in format_string:
            print(volume["labels"].get("trader-v3.redis-rebaseline", ""))
        else:
            print(json.dumps([volume]))
        raise SystemExit(0)
    if subcommand == "create":
        fail("volume_create")
        label = ""
        index = 0
        while index < len(args):
            if args[index] == "--label":
                label = args[index + 1].split("=", 1)[1]
                index += 2
                continue
            index += 1
        name = args[-1]
        mountpoint = volume_root / name
        mountpoint.mkdir(parents=True, exist_ok=True)
        state["volumes"][name] = {
            "mountpoint": str(mountpoint),
            "labels": {"trader-v3.redis-rebaseline": label},
        }
        save_state(state)
        log(f"volume-create:{name}")
        print(name)
        raise SystemExit(0)
    if subcommand == "rm":
        name = args[-1]
        if name not in state["volumes"]:
            raise SystemExit(1)
        del state["volumes"][name]
        save_state(state)
        log(f"volume-rm:{name}")
        print(name)
        raise SystemExit(0)
    raise SystemExit(2)

if command == "exec":
    name = args.pop(0)
    container = require_container(state, name)
    if not container["running"]:
        raise SystemExit(1)
    if not args or args.pop(0) != "redis-cli":
        raise SystemExit(2)
    if args and args[0] == "--raw":
        args.pop(0)
    redis_state = container["redis"]
    redis_command = args.pop(0).upper()
    if redis_command == "PING":
        if container["id"] == "new-id":
            fail("new_ping")
        print("PONG")
        raise SystemExit(0)
    if redis_command == "SAVE":
        if container["id"] == "new-id":
            fail("epoch_save")
        redis_state["rdb_changes_since_last_save"] = 0
        mountpoint = Path(container["mounts"][0]["Source"])
        rdb_path = mountpoint / redis_state["config"]["dbfilename"]
        rdb_path.write_bytes(b"REDIS0011fake-rdb-payload")
        save_state(state)
        log(f'redis-save:{name}:{container["id"]}')
        print("OK")
        raise SystemExit(0)
    if redis_command == "SET":
        if container["id"] == "new-id":
            fail("epoch_set")
        key, value = args
        strings = redis_state.setdefault("strings", {})
        if key not in strings:
            redis_state["dbsize"] += 1
        strings[key] = value
        redis_state["rdb_changes_since_last_save"] += 1
        save_state(state)
        log(f"redis-set:{name}:{key}:{value}")
        print("OK")
        raise SystemExit(0)
    if redis_command == "GET":
        if container["id"] == "new-id":
            fail("epoch_get")
        key = args.pop(0)
        value = redis_state.setdefault("strings", {}).get(key, "")
        log(f"redis-get:{name}:{key}:{value}")
        print(value)
        raise SystemExit(0)
    if redis_command == "DBSIZE":
        print(redis_state["dbsize"])
        raise SystemExit(0)
    if redis_command == "CONFIG":
        if args.pop(0).upper() != "GET":
            raise SystemExit(2)
        key = args.pop(0)
        value = redis_state["config"].get(key, "")
        print(key)
        print(value)
        raise SystemExit(0)
    if redis_command == "INFO":
        section = args.pop(0)
        print(f"# {section.title()}")
        if section == "server":
            print(f'run_id:{redis_state["run_id"]}')
        elif section == "memory":
            print(f'used_memory:{redis_state["used_memory"]}')
            print(f'used_memory_dataset:{redis_state["dataset_memory"]}')
        elif section == "persistence":
            print(f'aof_enabled:{redis_state["aof_enabled"]}')
            print(
                "rdb_changes_since_last_save:"
                f'{redis_state["rdb_changes_since_last_save"]}'
            )
            print(
                "rdb_last_bgsave_status:"
                f'{redis_state["rdb_last_bgsave_status"]}'
            )
        elif section == "keyspace" and redis_state["dbsize"] > 0:
            print(f'db0:keys={redis_state["dbsize"]},expires=0,avg_ttl=0')
        raise SystemExit(0)
    raise SystemExit(2)

if command == "run":
    if "--rm" in args:
        fail("checker")
        checker = args[args.index("--entrypoint") + 1]
        mount = args[args.index("-v") + 1]
        host_root = Path(mount.split(":", 1)[0])
        target = args[-1]
        relative = target.removeprefix("/backup/")
        artifact = host_root / relative
        data = artifact.read_bytes()
        if checker == "redis-check-rdb" and not data.startswith(b"REDIS"):
            raise SystemExit(1)
        if checker == "redis-check-aof":
            if not data.startswith(b"REDIS") and not data.startswith(b"*"):
                raise SystemExit(1)
        print(f"{checker}: {relative}: OK")
        raise SystemExit(0)

    fail("new_run")
    name = args[args.index("--name") + 1]
    label_raw = args[args.index("--label") + 1]
    label = label_raw.split("=", 1)[1]
    memory = int(args[args.index("--memory") + 1])
    memory_swap = int(args[args.index("--memory-swap") + 1])
    cpu_limit = args[args.index("--cpus") + 1]
    nano_cpus = int(float(cpu_limit) * 1_000_000_000)
    pids_limit = int(args[args.index("--pids-limit") + 1])
    nofile = args[args.index("--ulimit") + 1].removeprefix("nofile=")
    restart_policy = args[args.index("--restart") + 1]
    volume_spec = args[args.index("-v") + 1]
    volume_name = volume_spec.split(":", 1)[0]
    volume = state["volumes"][volume_name]
    image_index = args.index(volume_spec) + 1
    image = args[image_index]
    save_policy = args[args.index("--save") + 1]
    maxmemory = args[args.index("--maxmemory") + 1]
    container = {
        "id": "new-id",
        "image": image,
        "config_image": image,
        "running": True,
        "pid": 7777,
        "memory": memory,
        "memory_swap": memory_swap,
        "nano_cpus": nano_cpus,
        "pids_limit": pids_limit,
        "nofile": nofile,
        "restart_policy": restart_policy,
        "labels": {"trader-v3.redis-rebaseline": label},
        "mounts": [
            {
                "Type": "volume",
                "Name": volume_name,
                "Source": volume["mountpoint"],
                "Destination": "/data",
                "RW": True,
            }
        ],
            "redis": {
                "run_id": "new-run",
                "dbsize": 0,
                "strings": {},
                "used_memory": 1048576,
            "dataset_memory": 0,
            "aof_enabled": 0,
            "rdb_changes_since_last_save": 0,
            "rdb_last_bgsave_status": "ok",
            "config": {
                "dir": "/data",
                "dbfilename": "dump.rdb",
                "save": save_policy,
                "appendonly": "no",
                "appendfilename": "appendonly.aof",
                "appenddirname": "appendonlydir",
                "maxmemory": maxmemory,
                "maxmemory-policy": "noeviction",
            },
        },
    }
    state["containers"][name] = container
    save_state(state)
    log(f'run:{name}:{container["id"]}')
    print(container["id"])
    raise SystemExit(0)

print(f"unsupported docker command: {command} {args}", file=sys.stderr)
raise SystemExit(2)
"""


class RebaselineHarness:
    def __init__(self, tmp_path: Path, *, host_available_kb: int = 7 * 1024**2):
        self.root = tmp_path
        self.trader_root = tmp_path / "trader-v3"
        self.trader_root.mkdir()
        self.volume_root = tmp_path / "volumes"
        self.volume_root.mkdir()
        self.old_volume = self.volume_root / "old-volume"
        self.old_volume.mkdir()
        (self.old_volume / "dump.rdb").write_bytes(
            b"REDIS0011initial-fake-rdb-payload"
        )
        self.state_path = tmp_path / "docker-state.json"
        self.log_path = tmp_path / "docker.log"
        self.log_path.write_text("", encoding="utf-8")
        self.meminfo_path = tmp_path / "meminfo"
        self.meminfo_path.write_text(
            "MemTotal:       8388608 kB\n"
            f"MemAvailable:   {host_available_kb} kB\n",
            encoding="utf-8",
        )
        self.cgroup_root = tmp_path / "cgroup"
        for container_id in NODE_IDS.values():
            cgroup = self.cgroup_root / "docker" / container_id
            cgroup.mkdir(parents=True)
            (cgroup / "cgroup.procs").write_text("", encoding="utf-8")

        self.fake_bin = tmp_path / "bin"
        self.fake_bin.mkdir()
        docker = self.fake_bin / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        docker.chmod(0o755)
        flock = self.fake_bin / "flock"
        flock.write_text(
            "#!/bin/sh\n"
            'if [ "${FAKE_FLOCK_CONFLICT:-0}" = "1" ]; then\n'
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        flock.chmod(0o755)
        sleep = self.fake_bin / "sleep"
        sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        sleep.chmod(0o755)
        real_cp = shutil.which("cp")
        assert real_cp is not None
        cp = self.fake_bin / "cp"
        cp.write_text(
            "#!/bin/sh\n"
            'if [ "${FAKE_DOCKER_FAIL_AT:-}" = "copy" ]; then\n'
            '  echo "injected failure: copy" >&2\n'
            "  exit 42\n"
            "fi\n"
            f"exec {shlex.quote(real_cp)} \"$@\"\n",
            encoding="utf-8",
        )
        cp.chmod(0o755)
        planner = self.fake_bin / "redis-capacity-planner"
        planner.write_text(
            "#!/usr/bin/env python3\n"
            "from __future__ import annotations\n"
            "import os\n"
            "import sys\n"
            "fail_at = os.environ.get('FAKE_DOCKER_FAIL_AT', '')\n"
            "if fail_at == 'manifest_verify' "
            "and sys.argv[1] == 'verify-backup':\n"
            "    print('injected failure: manifest_verify', file=sys.stderr)\n"
            "    raise SystemExit(42)\n"
            "real_planner = os.environ['REAL_CAPACITY_PLANNER']\n"
            "os.execv(sys.executable, [sys.executable, real_planner, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        planner.chmod(0o755)

        self.state_path.write_text(
            json.dumps(self._initial_state(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.fake_bin}:{self.env['PATH']}",
                "TRADER_ROOT": str(self.trader_root),
                "REDIS_CAPACITY_PLANNER": str(planner),
                "REAL_CAPACITY_PLANNER": str(CAPACITY_PLANNER),
                "ACCOUNT_STALL_OPERATION_LOCK": str(
                    tmp_path / "account-stall-operation.lock"
                ),
                "REDIS_REBASELINE_STAMP": STAMP,
                "REDIS_MEMINFO_PATH": str(self.meminfo_path),
                "REDIS_CGROUP_ROOT": str(self.cgroup_root),
                "FAKE_DOCKER_STATE": str(self.state_path),
                "FAKE_DOCKER_LOG": str(self.log_path),
                "FAKE_DOCKER_VOLUME_ROOT": str(self.volume_root),
            }
        )

    def _initial_state(self) -> dict[str, object]:
        redis_config = {
            "dir": "/data",
            "dbfilename": "dump.rdb",
            "save": "3600 1",
            "appendonly": "no",
            "appendfilename": "appendonly.aof",
            "appenddirname": "appendonlydir",
            "maxmemory": "0",
            "maxmemory-policy": "noeviction",
        }
        containers = {
            REDIS_CONTAINER: {
                "id": "source-id",
                "image": "sha256:source-image",
                "config_image": "redis:7.2",
                "running": True,
                "pid": 6001,
                "memory": 0,
                "memory_swap": 0,
                "labels": {},
                "mounts": [
                    {
                        "Type": "volume",
                        "Name": "old-volume",
                        "Source": str(self.old_volume),
                        "Destination": "/data",
                        "RW": True,
                    }
                ],
                "redis": {
                    "run_id": "source-run",
                    "dbsize": 191847,
                    "strings": {},
                    "used_memory": 6 * 1024**3,
                    "dataset_memory": 5 * 1024**3,
                    "aof_enabled": 0,
                    "rdb_changes_since_last_save": 1408,
                    "rdb_last_bgsave_status": "ok",
                    "config": redis_config,
                },
            },
            "trader-v3-node-a": self._node("node-a-id", 6101),
            "trader-v3-node-b": self._node("node-b-id", 6102),
            "trader-v3-node-c": self._node("node-c-id", 6103),
            "trader-v3-node-d": self._node("node-d-id", 6104),
        }
        return {
            "containers": containers,
            "volumes": {
                "old-volume": {
                    "mountpoint": str(self.old_volume),
                    "labels": {},
                }
            },
        }

    @staticmethod
    def _node(container_id: str, pid: int) -> dict[str, object]:
        return {
            "id": container_id,
            "image": "sha256:node-image",
            "config_image": "trader-node:test",
            "running": True,
            "pid": pid,
            "memory": 0,
            "memory_swap": 0,
            "labels": {},
            "mounts": [],
            "redis": {},
        }

    @property
    def evidence_root(self) -> Path:
        return self.trader_root / "redis-rebaseline" / STAMP

    def run_rebaseline(
        self,
        *,
        fail_at: str = "",
        flock_conflict: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        env["FAKE_DOCKER_FAIL_AT"] = fail_at
        env["FAKE_FLOCK_CONFLICT"] = "1" if flock_conflict else "0"
        source = shlex.quote(str(SCRIPT))
        command = f"source {source}; require_root() {{ return 0; }}; main"
        return subprocess.run(
            ["bash", "-c", command],
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

    def run_rollback(self) -> subprocess.CompletedProcess[str]:
        rollback = self.evidence_root / "rollback-redis.sh"
        source = shlex.quote(str(rollback))
        command = f"source {source}; require_root() {{ return 0; }}; main"
        env = self.env.copy()
        env["FAKE_DOCKER_FAIL_AT"] = ""
        env["FAKE_FLOCK_CONFLICT"] = "0"
        return subprocess.run(
            ["bash", "-c", command],
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

    def state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def write_state(self, state: dict[str, object]) -> None:
        self.state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def log_lines(self) -> list[str]:
        return self.log_path.read_text(encoding="utf-8").splitlines()


def test_redis_rebaseline_contract_is_fail_closed() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    normalized_text = " ".join(text.replace("\\\n", " ").split())

    assert "NODE_CONTAINERS=(" in text
    for node in NODE_CONTAINERS:
        assert node in text
    assert 'set_phase "source_renamed"' in text
    assert 'container_label_or_empty "$REDIS_CONTAINER"' in text
    assert 'run_redis_checker redis-check-rdb "$RDB_RELATIVE"' in text
    assert 'python3 "$CAPACITY_PLANNER" verify-backup' in text
    assert 'python3 "$CAPACITY_PLANNER" generate' in text
    assert f'REDIS_FENCING_EPOCH_KEY="{REDIS_FENCING_EPOCH_KEY}"' in text
    assert '"$REDIS_FENCING_EPOCH_KEY" "$epoch"' in text
    assert 'redis-cli SAVE' in text
    assert (
        'redis-cli --raw GET "$REDIS_FENCING_EPOCH_KEY"'
        in normalized_text
    )
    assert '[ "$NEW_KEY_COUNT" = "1" ]' in text
    assert '[ "$ACTUAL_POLICY" = "noeviction" ]' in text
    assert '[ "$ACTUAL_MEMORY_SWAP" = "$CGROUP_LIMIT_BYTES" ]' in text
    assert '[ "$new_volume_mountpoint" = "$ACTIVE_VOLUME_SOURCE" ]' in text
    assert (
        'DEFAULT_REDIS_RESOURCE_ARTIFACT="$SCRIPT_DIR/'
        'infra/systemd/account-stall-redis.conf"'
        in text
    )
    assert (
        'DEFAULT_REDIS_RESOURCE_ARTIFACT="$SCRIPT_DIR/../'
        'infra/systemd/account-stall-redis.conf"'
        in text
    )
    assert (
        'DEFAULT_OPERATION_LOCK="/var/lock/'
        'trader-v3-account-stall-operation.lock"'
        in text
    )
    assert 'flock -n 9 || die "another account-stall operation holds' in text
    assert 'STREAM_MAX_ENTRIES="${REDIS_STREAM_MAX_ENTRIES:-100000}"' in text
    assert (
        'ACCOUNT_A_STABLE_NAMESPACE="${REDIS_ACCOUNT_A_STABLE_NAMESPACE:'
        '-trader-TRADER-ACCOUNT-A}"'
        in text
    )
    assert (
        'ACCOUNT_B_STABLE_NAMESPACE="${REDIS_ACCOUNT_B_STABLE_NAMESPACE:'
        '-trader-TRADER-ACCOUNT-B}"'
        in text
    )
    assert (
        'ACCOUNT_C_STABLE_NAMESPACE="${REDIS_ACCOUNT_C_STABLE_NAMESPACE:'
        '-trader-TRADER-ACCOUNT-C}"'
        in text
    )
    assert (
        'ACCOUNT_D_STABLE_NAMESPACE="${REDIS_ACCOUNT_D_STABLE_NAMESPACE:'
        '-trader-TRADER-ACCOUNT-D}"'
        in text
    )
    assert "A-D stable Redis namespaces must be distinct" in text
    assert "fenced-generation-namespace/v2" in text
    assert 'write_rollback_script' in text
    assert 'assert_node_stopped "$node"' in text
    assert "source Redis identity is not recoverable" in text


@pytest.mark.parametrize(
    "fail_at",
    [
        "source_stop",
        "copy",
        "checker",
        "manifest_verify",
        "rename",
        "volume_create",
        "new_run",
        "new_ping",
        "epoch_set",
        "epoch_save",
        "epoch_get",
    ],
)
def test_each_failure_stage_restores_source_without_deleting_it(
    tmp_path: Path,
    fail_at: str,
) -> None:
    harness = RebaselineHarness(tmp_path)

    result = harness.run_rebaseline(fail_at=fail_at)

    assert result.returncode != 0
    state = harness.state()
    source = state["containers"][REDIS_CONTAINER]
    assert source["id"] == "source-id"
    assert source["running"] is True
    assert LEGACY_CONTAINER not in state["containers"]
    for node in NODE_CONTAINERS:
        assert state["containers"][node]["running"] is False
    assert f"rm:{REDIS_CONTAINER}:source-id" not in harness.log_lines()


def test_capacity_headroom_failure_restores_source(tmp_path: Path) -> None:
    harness = RebaselineHarness(tmp_path, host_available_kb=512 * 1024)

    result = harness.run_rebaseline()

    assert result.returncode != 0
    assert "host available memory" in result.stderr
    state = harness.state()
    assert state["containers"][REDIS_CONTAINER]["id"] == "source-id"
    assert state["containers"][REDIS_CONTAINER]["running"] is True
    assert LEGACY_CONTAINER not in state["containers"]
    assert NEW_VOLUME not in state["volumes"]


def test_global_operation_lock_conflict_prevents_redis_mutation(
    tmp_path: Path,
) -> None:
    harness = RebaselineHarness(tmp_path)

    result = harness.run_rebaseline(flock_conflict=True)

    assert result.returncode != 0
    assert "another account-stall operation holds" in result.stderr
    state = harness.state()
    assert state["containers"][REDIS_CONTAINER]["id"] == "source-id"
    assert state["containers"][REDIS_CONTAINER]["running"] is True
    for node in NODE_CONTAINERS:
        assert state["containers"][node]["running"] is True
    assert harness.log_lines() == []
    assert not harness.evidence_root.exists()


def test_success_generates_verified_evidence_and_executable_rollback(
    tmp_path: Path,
) -> None:
    harness = RebaselineHarness(tmp_path)

    result = harness.run_rebaseline()

    assert result.returncode == 0, result.stderr
    state = harness.state()
    assert state["containers"][REDIS_CONTAINER]["id"] == "new-id"
    assert state["containers"][LEGACY_CONTAINER]["id"] == "source-id"
    assert state["containers"][LEGACY_CONTAINER]["running"] is False
    backup_manifest = json.loads(
        (harness.evidence_root / "cold-backup-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert backup_manifest["mode"] == "cold"
    assert backup_manifest["source_container_id"] == "source-id"
    assert backup_manifest["source_volume_name"] == "old-volume"
    assert backup_manifest["artifacts"][0]["kind"] == "rdb"
    assert backup_manifest["artifacts"][0]["validation_passed"] is True
    capacity = json.loads(
        (harness.evidence_root / "capacity-evidence.json").read_text(
            encoding="utf-8"
        )
    )
    assert capacity["passed"] is True
    assert capacity["schema_version"] == "trader-v3-redis-capacity-evidence/v3"
    assert capacity["active_key_count"] == 1
    assert capacity["redis_fencing_epoch_key"] == REDIS_FENCING_EPOCH_KEY
    epoch = uuid.UUID(capacity["redis_fencing_epoch"])
    assert epoch.version == 4
    assert str(epoch) == capacity["redis_fencing_epoch"]
    assert capacity["redis_fencing_epoch_sha256"] == sha256(
        capacity["redis_fencing_epoch"].encode("ascii")
    ).hexdigest()
    assert capacity["initial_redis_run_id"] == capacity["active_redis_run_id"]
    assert capacity["control_keys_reinitialized"] is True
    assert capacity["maxmemory_policy"] == "noeviction"
    assert capacity["maxmemory_bytes"] == 512 * 1024**2
    assert capacity["other_services_reserve_bytes"] == 2304 * 1024**2
    assert capacity["memory_limit_bytes"] == 640 * 1024**2
    assert capacity["memory_swap_limit_bytes"] == 640 * 1024**2
    assert capacity["runtime_resource_policy"] == {
        "schema_version": "trader-v3-runtime-resources/v1",
        "namespace_schema_epoch": "fenced-generation-namespace/v2",
        "stable_namespaces": {
            "account-a": "trader-TRADER-ACCOUNT-A",
            "account-b": "trader-TRADER-ACCOUNT-B",
            "account-c": "trader-TRADER-ACCOUNT-C",
            "account-d": "trader-TRADER-ACCOUNT-D",
        },
        "stream_retention": {
            "stream_max_entries": 100_000,
            "stream_max_bytes": 64 * 1024**2,
            "total_stream_max_bytes": 256 * 1024**2,
        },
    }
    assert all(capacity["runtime_checks"].values())
    active_redis = state["containers"][REDIS_CONTAINER]["redis"]
    assert active_redis["strings"] == {
        REDIS_FENCING_EPOCH_KEY: capacity["redis_fencing_epoch"]
    }
    epoch_set = next(
        index
        for index, line in enumerate(harness.log_lines())
        if line.startswith(
            f"redis-set:{REDIS_CONTAINER}:{REDIS_FENCING_EPOCH_KEY}:"
        )
    )
    epoch_save = harness.log_lines().index(
        f"redis-save:{REDIS_CONTAINER}:new-id"
    )
    epoch_read = next(
        index
        for index, line in enumerate(harness.log_lines())
        if line.startswith(
            f"redis-get:{REDIS_CONTAINER}:{REDIS_FENCING_EPOCH_KEY}:"
        )
    )
    assert epoch_set < epoch_save < epoch_read
    rollback = harness.evidence_root / "rollback-redis.sh"
    assert rollback.stat().st_mode & 0o100

    for index, node in enumerate(NODE_CONTAINERS, start=1):
        state["containers"][node]["running"] = True
        state["containers"][node]["pid"] = 7100 + index
    harness.write_state(state)
    log_offset = len(harness.log_lines())

    rollback_result = harness.run_rollback()

    assert rollback_result.returncode == 0, rollback_result.stderr
    restored = harness.state()
    assert restored["containers"][REDIS_CONTAINER]["id"] == "source-id"
    assert restored["containers"][REDIS_CONTAINER]["running"] is True
    assert LEGACY_CONTAINER not in restored["containers"]
    for node in NODE_CONTAINERS:
        assert restored["containers"][node]["running"] is False
    restored_epoch_raw = restored["containers"][REDIS_CONTAINER]["redis"][
        "strings"
    ][REDIS_FENCING_EPOCH_KEY]
    restored_epoch = uuid.UUID(restored_epoch_raw)
    assert restored_epoch.version == 4
    assert str(restored_epoch) == restored_epoch_raw
    assert restored_epoch_raw != capacity["redis_fencing_epoch"]
    rollback_log = harness.log_lines()[log_offset:]
    redis_stop_index = rollback_log.index(f"stop:{REDIS_CONTAINER}:new-id")
    for node in NODE_CONTAINERS:
        assert (
            rollback_log.index(f"stop:{node}:{NODE_IDS[node]}")
            < redis_stop_index
        )
    rollback_set_index = next(
        index
        for index, line in enumerate(rollback_log)
        if line.startswith(
            f"redis-set:{REDIS_CONTAINER}:{REDIS_FENCING_EPOCH_KEY}:"
        )
    )
    rollback_save_index = rollback_log.index(
        f"redis-save:{REDIS_CONTAINER}:source-id"
    )
    assert rollback_set_index < rollback_save_index


def test_ordinary_redis_restart_preserves_fencing_epoch(
    tmp_path: Path,
) -> None:
    harness = RebaselineHarness(tmp_path)
    result = harness.run_rebaseline()
    assert result.returncode == 0, result.stderr
    state = harness.state()
    epoch_before = state["containers"][REDIS_CONTAINER]["redis"]["strings"][
        REDIS_FENCING_EPOCH_KEY
    ]

    subprocess.run(
        ["docker", "stop", REDIS_CONTAINER],
        cwd=REPO_ROOT,
        env=harness.env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["docker", "start", REDIS_CONTAINER],
        cwd=REPO_ROOT,
        env=harness.env,
        check=True,
        capture_output=True,
        text=True,
    )
    marker = subprocess.run(
        [
            "docker",
            "exec",
            REDIS_CONTAINER,
            "redis-cli",
            "--raw",
            "GET",
            REDIS_FENCING_EPOCH_KEY,
        ],
        cwd=REPO_ROOT,
        env=harness.env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert marker.stdout.strip() == epoch_before


def test_rollback_refuses_switch_when_node_cgroup_retains_writer(
    tmp_path: Path,
) -> None:
    harness = RebaselineHarness(tmp_path)
    result = harness.run_rebaseline()
    assert result.returncode == 0, result.stderr
    process_file = (
        harness.cgroup_root / "docker" / "node-a-id" / "cgroup.procs"
    )
    process_file.write_text("8123\n", encoding="utf-8")

    rollback_result = harness.run_rollback()

    assert rollback_result.returncode != 0
    state = harness.state()
    assert state["containers"][REDIS_CONTAINER]["id"] == "new-id"
    assert state["containers"][LEGACY_CONTAINER]["id"] == "source-id"


def test_deploy_requires_live_capacity_and_cold_backup_evidence() -> None:
    text = DEPLOY.read_text(encoding="utf-8")

    assert "REDIS_COLD_BACKUP_MANIFEST" in text
    assert "REDIS_CAPACITY_EVIDENCE" in text
    assert "Redis cold backup hash mismatch" in text
    assert "running Redis differs from capacity evidence" in text
    assert "running Redis maxmemory differs from capacity evidence" in text
    assert "running Redis cgroup differs from capacity evidence" in text
