#!/usr/bin/env python3
"""Generate fail-closed recreate scripts for trader-v3 node containers.

Usage (root):
  python3 hk-gen-recreate-patched.py \
    trader-v3-node-a BINANCE_EXEC_DST BINANCE_FUTURES_EXEC_DST

Discover BINANCE_EXEC_DST from the running image before generation:
  docker exec trader-v3-node-a python -c \
    "import nautilus_trader.adapters.binance.execution as m; print(m.__file__)"
"""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


class DeploymentConfigError(ValueError):
    pass


PATCH_MOUNT_TARGETS = (
    ("intent_execution_planner.py", "/app/strategy/intent_execution_planner.py"),
    ("contracts.py", "/app/execution_domain/contracts.py"),
    ("control_plane.py", "/app/execution_domain/control_plane.py"),
    ("http_client.py", "/app/execution_domain/http_client.py"),
    ("projection_actor.py", "/app/projection/actor.py"),
    ("event_mapper.py", "/app/projection/event_mapper.py"),
    ("intent_execution_strategy.py", "/app/strategy/intent_execution_strategy.py"),
    ("exchange_cancel_adapter.py", "/app/runtime/exchange_cancel_adapter.py"),
    ("lifecycle.py", "/app/runtime/lifecycle.py"),
    ("binance_adapter_config.py", "/app/runtime/binance_adapter_config.py"),
    ("node.py", "/app/app/node.py"),
    ("nautilus_actors.py", "/app/app/nautilus_actors.py"),
)


def parse_args(argv):
    if len(argv) != 4:
        raise DeploymentConfigError(
            "BINANCE_EXEC_DST and BINANCE_FUTURES_EXEC_DST are required: "
            "hk-gen-recreate-patched.py CONTAINER "
            "BINANCE_EXEC_DST BINANCE_FUTURES_EXEC_DST"
        )

    name = argv[1].strip()
    binance_dst = argv[2].strip()
    binance_futures_dst = argv[3].strip()
    if not name:
        raise DeploymentConfigError("container name is empty")
    if not binance_dst:
        raise DeploymentConfigError("BINANCE_EXEC_DST is empty")
    if not binance_dst.startswith("/"):
        raise DeploymentConfigError("BINANCE_EXEC_DST must be an absolute path")
    if ":" in binance_dst or "\n" in binance_dst:
        raise DeploymentConfigError("BINANCE_EXEC_DST contains invalid characters")
    if not binance_futures_dst:
        raise DeploymentConfigError("BINANCE_FUTURES_EXEC_DST is empty")
    if not binance_futures_dst.startswith("/"):
        raise DeploymentConfigError(
            "BINANCE_FUTURES_EXEC_DST must be an absolute path"
        )
    if ":" in binance_futures_dst or "\n" in binance_futures_dst:
        raise DeploymentConfigError(
            "BINANCE_FUTURES_EXEC_DST contains invalid characters"
        )

    return name, binance_dst, binance_futures_dst


def explicit_mounts(trader_root, suffix, binance_dst, binance_futures_dst):
    patch_dir = trader_root / "container-patches"
    mounts = [
        (str(trader_root / "node-state" / suffix), "/state", "rw"),
    ]
    for filename, destination in PATCH_MOUNT_TARGETS:
        mounts.append((str(patch_dir / filename), destination, "ro"))
    mounts.extend(
        [
            (str(patch_dir / "binance_execution.py"), binance_dst, "ro"),
            (
                str(patch_dir / "binance_futures_execution.py"),
                binance_futures_dst,
                "ro",
            ),
        ]
    )
    return mounts


def validate_mount_plan(mounts):
    sources = {}
    destinations = {}
    for source, destination, mode in mounts:
        if not source.startswith("/") or not destination.startswith("/"):
            raise DeploymentConfigError(
                f"mount paths must be absolute: {source} -> {destination}"
            )
        if ":" in source or ":" in destination:
            raise DeploymentConfigError(
                f"mount paths cannot contain colons: {source} -> {destination}"
            )
        if mode not in {"ro", "rw"}:
            raise DeploymentConfigError(f"invalid mount mode: {mode}")
        if source in sources:
            previous = sources[source]
            raise DeploymentConfigError(
                f"duplicate mount source: {source} -> {previous}, {destination}"
            )
        if destination in destinations:
            previous = destinations[destination]
            raise DeploymentConfigError(
                "duplicate mount destination: "
                f"{destination} <- {previous}, {source}"
            )
        sources[source] = destination
        destinations[destination] = source


def validate_patch_sources(mounts, trader_root):
    patch_dir = trader_root / "container-patches"
    prefix = f"{patch_dir}{os.sep}"
    for source, _, _ in mounts:
        if not source.startswith(prefix):
            continue
        if not Path(source).is_file():
            raise DeploymentConfigError(f"mount source is missing: {source}")


def append_inherited_mounts(run, inherited_mounts, explicit):
    explicit_sources = {source for source, _, _ in explicit}
    explicit_destinations = {destination for _, destination, _ in explicit}

    for mount in inherited_mounts:
        source = mount["Source"]
        destination = mount["Destination"]
        if source in explicit_sources or destination in explicit_destinations:
            continue

        mode = "rw"
        if not mount.get("RW", True):
            mode = "ro"
        run.extend(["-v", f"{source}:{destination}:{mode}"])


def generate(name, binance_dst, binance_futures_dst, trader_root):
    suffix = name.rsplit("-", 1)[-1]
    mounts = explicit_mounts(
        trader_root,
        suffix,
        binance_dst,
        binance_futures_dst,
    )
    validate_mount_plan(mounts)
    validate_patch_sources(mounts, trader_root)

    inspect_output = subprocess.check_output(["docker", "inspect", name])
    inspected = json.loads(inspect_output)[0]
    image = inspected["Config"]["Image"]
    env = inspected["Config"]["Env"]
    cmd = inspected["Config"]["Cmd"] or []
    entrypoint = inspected["Config"]["Entrypoint"] or []
    networks = inspected["NetworkSettings"]["Networks"]
    network = list(networks.keys())[0]
    restart = inspected["HostConfig"]["RestartPolicy"]["Name"]
    if not restart:
        restart = "unless-stopped"

    state_dir = trader_root / "node-state" / suffix
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        f"docker rm -f {shlex.quote(name)} 2>/dev/null || true",
        f"mkdir -p {shlex.quote(str(state_dir))}",
    ]
    run = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        f"--network={network}",
        f"--restart={restart}",
    ]
    for env_value in env:
        if env_value.startswith(("PATH=", "PYTHON", "LANG=", "GPG_KEY", "HOME=")):
            continue
        env_name = env_value.split("=", 1)[0]
        if env_name == "NAUTILUS_INITIAL_TRADING_STATE":
            continue
        run.extend(["-e", env_value])

    append_inherited_mounts(run, inspected["Mounts"], mounts)
    for source, destination, mode in mounts:
        run.extend(["-v", f"{source}:{destination}:{mode}"])

    run.extend(["-e", "NODE_STATE_DIR=/state"])
    run.extend(["-e", "NAUTILUS_INITIAL_TRADING_STATE=HALTED"])
    if entrypoint:
        run.extend(["--entrypoint", entrypoint[0]])
    run.append(image)
    if len(entrypoint) > 1:
        run.extend(entrypoint[1:])
    run.extend(cmd)
    lines.append(" ".join(shlex.quote(value) for value in run))

    output_path = trader_root / f"recreate-{name}.sh"
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output_path.chmod(0o700)

    print(f"WROTE {output_path}")
    print(f"image: {image} | net: {network} | restart: {restart}")
    print("explicit mount pairs:")
    for source, destination, mode in mounts:
        print(f"  {source} -> {destination} ({mode})")


def main(argv=None):
    if argv is None:
        argv = sys.argv

    try:
        name, binance_dst, binance_futures_dst = parse_args(argv)
        trader_root = Path(os.environ.get("TRADER_ROOT", "/srv/trader-v3"))
        generate(name, binance_dst, binance_futures_dst, trader_root)
    except DeploymentConfigError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
