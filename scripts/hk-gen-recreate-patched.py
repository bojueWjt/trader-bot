#!/usr/bin/env python3
"""Generate a HALTED bind-mount recreate script for a trader-v3 node."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any


class DeploymentConfigError(ValueError):
    pass


PATCH_MOUNT_TARGETS = (
    ("intent_execution_planner.py", "/app/strategy/intent_execution_planner.py"),
    ("contracts.py", "/app/execution_domain/contracts.py"),
    ("control_plane.py", "/app/execution_domain/control_plane.py"),
    ("http_client.py", "/app/execution_domain/http_client.py"),
    ("projection_actor.py", "/app/projection/actor.py"),
    ("projection_spool.py", "/app/projection/spool.py"),
    ("event_mapper.py", "/app/projection/event_mapper.py"),
    ("intent_execution_strategy.py", "/app/strategy/intent_execution_strategy.py"),
    ("exchange_cancel_adapter.py", "/app/runtime/exchange_cancel_adapter.py"),
    ("lifecycle.py", "/app/runtime/lifecycle.py"),
    ("health.py", "/app/runtime/health.py"),
    ("bounded_task_worker.py", "/app/runtime/bounded_task_worker.py"),
    ("control_plane_session.py", "/app/runtime/control_plane_session.py"),
    ("binance_adapter_config.py", "/app/runtime/binance_adapter_config.py"),
    ("node_config.py", "/app/config/node_config.py"),
    ("node.py", "/app/app/node.py"),
    ("run_node.py", "/app/app/run_node.py"),
    ("health_server.py", "/app/app/health_server.py"),
    ("nautilus_actors.py", "/app/app/nautilus_actors.py"),
    (
        "approved_intent_client.py",
        "/app/data_client/approved_intent_client.py",
    ),
    ("atomic_json.py", "/app/data_client/atomic_json.py"),
    (
        "durable_intent_inbox.py",
        "/app/data_client/durable_intent_inbox.py",
    ),
    ("data_client_init.py", "/app/data_client/__init__.py"),
    (
        "durable_command_journal.py",
        "/app/commands/durable_command_journal.py",
    ),
    ("commands_init.py", "/app/commands/__init__.py"),
)
_RESOURCE_RE = re.compile(r"^[1-9][0-9]*(?:[bkmg])?$", re.IGNORECASE)


def parse_args(argv: list[str]) -> tuple[str, str, str]:
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
    _validate_absolute_path("BINANCE_EXEC_DST", binance_dst)
    _validate_absolute_path(
        "BINANCE_FUTURES_EXEC_DST",
        binance_futures_dst,
    )
    return name, binance_dst, binance_futures_dst


def _validate_absolute_path(label: str, value: str) -> None:
    if not value:
        raise DeploymentConfigError(f"{label} is empty")
    if not value.startswith("/"):
        raise DeploymentConfigError(f"{label} must be an absolute path")
    if re.search(r"[:\r\n]", value):
        raise DeploymentConfigError(f"{label} contains invalid characters")


def _resource_limit(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if _RESOURCE_RE.fullmatch(value) is None:
        raise DeploymentConfigError(
            f"{name} must be a positive Docker memory value"
        )
    return value


def explicit_mounts(
    trader_root: Path,
    suffix: str,
    binance_dst: str,
    binance_futures_dst: str,
) -> list[tuple[str, str, str]]:
    patch_dir = trader_root / "container-patches"
    mounts = [
        (str(trader_root / "node-state" / suffix), "/state", "rw"),
    ]
    for filename, destination in PATCH_MOUNT_TARGETS:
        mounts.append((str(patch_dir / filename), destination, "ro"))
    mounts.extend(
        [
            (
                str(patch_dir / "binance_execution.py"),
                binance_dst,
                "ro",
            ),
            (
                str(patch_dir / "binance_futures_execution.py"),
                binance_futures_dst,
                "ro",
            ),
        ]
    )
    return mounts


def validate_mount_plan(mounts: list[tuple[str, str, str]]) -> None:
    sources: dict[str, str] = {}
    destinations: dict[str, str] = {}
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


def validate_patch_sources(
    mounts: list[tuple[str, str, str]],
    trader_root: Path,
) -> None:
    patch_dir = trader_root / "container-patches"
    prefix = f"{patch_dir}{os.sep}"
    for source, _, _ in mounts:
        if not source.startswith(prefix):
            continue
        if not Path(source).is_file():
            raise DeploymentConfigError(f"mount source is missing: {source}")


def append_inherited_mounts(
    run: list[str],
    inherited_mounts: list[dict[str, Any]],
    explicit: list[tuple[str, str, str]],
) -> None:
    explicit_sources = {source for source, _, _ in explicit}
    explicit_destinations = {destination for _, destination, _ in explicit}

    for mount in inherited_mounts:
        source = str(mount.get("Source") or "")
        destination = str(mount.get("Destination") or "")
        if not source or not destination:
            raise DeploymentConfigError("inherited mount is incomplete")
        if source in explicit_sources or destination in explicit_destinations:
            continue
        mode = "rw"
        if not mount.get("RW", True):
            mode = "ro"
        run.extend(["-v", f"{source}:{destination}:{mode}"])


def append_port_bindings(
    run: list[str],
    host_config: dict[str, Any],
) -> None:
    bindings = host_config.get("PortBindings") or {}
    if not isinstance(bindings, dict):
        raise DeploymentConfigError("docker PortBindings must be an object")
    for container_port in sorted(bindings):
        values = bindings[container_port] or []
        if not isinstance(values, list):
            raise DeploymentConfigError(
                f"docker PortBindings.{container_port} must be an array"
            )
        for binding in values:
            if not isinstance(binding, dict):
                raise DeploymentConfigError(
                    f"docker PortBindings.{container_port} is invalid"
                )
            host_port = str(binding.get("HostPort") or "").strip()
            host_ip = str(binding.get("HostIp") or "").strip()
            if not host_port:
                raise DeploymentConfigError(
                    f"docker PortBindings.{container_port} lacks HostPort"
                )
            published = f"{host_port}:{container_port}"
            if host_ip:
                published = f"{host_ip}:{published}"
            run.extend(["--publish", published])


def _network_plan(
    inspected: dict[str, Any],
    name: str,
) -> tuple[str, list[str], list[list[str]]]:
    network_settings = inspected.get("NetworkSettings") or {}
    networks = network_settings.get("Networks") or {}
    if not isinstance(networks, dict) or not networks:
        raise DeploymentConfigError(
            "container must be attached to at least one Docker network"
        )
    network_names = list(networks)
    primary = network_names[0]
    primary_args = [f"--network={primary}"]
    secondary_commands: list[list[str]] = []
    container_id = str(inspected.get("Id") or "").strip()
    short_id = container_id[:12]
    excluded_aliases = {name, container_id, short_id, ""}
    for index, network_name in enumerate(network_names):
        details = networks[network_name] or {}
        aliases = details.get("Aliases") or []
        normalized_aliases = []
        for alias in aliases:
            normalized = str(alias or "").strip()
            if normalized not in excluded_aliases:
                normalized_aliases.append(normalized)
        if index == 0:
            for alias in normalized_aliases:
                primary_args.extend(["--network-alias", alias])
            continue
        command = ["docker", "network", "connect"]
        for alias in normalized_aliases:
            command.extend(["--alias", alias])
        command.extend([network_name, name])
        secondary_commands.append(command)
    return primary, primary_args, secondary_commands


def _append_config_options(
    run: list[str],
    config: dict[str, Any],
) -> None:
    for option, field in (
        ("--user", "User"),
        ("--workdir", "WorkingDir"),
        ("--hostname", "Hostname"),
    ):
        value = str(config.get(field) or "").strip()
        if value:
            run.extend([option, value])


def generate(
    name: str,
    binance_dst: str,
    binance_futures_dst: str,
    trader_root: Path,
) -> None:
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
    inspected_payload = json.loads(inspect_output)
    if not isinstance(inspected_payload, list) or len(inspected_payload) != 1:
        raise DeploymentConfigError("docker inspect returned an invalid payload")
    inspected = inspected_payload[0]
    config = inspected.get("Config") or {}
    host_config = inspected.get("HostConfig") or {}
    if not isinstance(config, dict) or not isinstance(host_config, dict):
        raise DeploymentConfigError("docker inspect config is invalid")

    image = str(config.get("Image") or "").strip()
    if not image:
        raise DeploymentConfigError("docker inspect image is missing")
    env = config.get("Env") or []
    cmd = config.get("Cmd") or []
    entrypoint = config.get("Entrypoint") or []
    inherited_mounts = inspected.get("Mounts") or []
    if not isinstance(env, list):
        raise DeploymentConfigError("docker inspect environment is invalid")
    if not isinstance(cmd, list) or not isinstance(entrypoint, list):
        raise DeploymentConfigError("docker inspect command is invalid")
    if not isinstance(inherited_mounts, list):
        raise DeploymentConfigError("docker inspect mounts are invalid")

    restart_policy = host_config.get("RestartPolicy") or {}
    restart = str(restart_policy.get("Name") or "").strip()
    if not restart:
        restart = "unless-stopped"
    primary_network, network_args, network_commands = _network_plan(
        inspected,
        name,
    )
    memory_limit = _resource_limit("NODE_MEMORY_LIMIT", "768m")
    memory_swap_limit = _resource_limit(
        "NODE_MEMORY_SWAP_LIMIT",
        "768m",
    )

    state_dir = trader_root / "node-state" / suffix
    lines = [
        "#!/bin/bash",
        "set -Eeuo pipefail",
        f"docker rm -f {shlex.quote(name)} 2>/dev/null || true",
        f"mkdir -p {shlex.quote(str(state_dir))}",
    ]
    run = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        *network_args,
        f"--restart={restart}",
        "--memory",
        memory_limit,
        "--memory-swap",
        memory_swap_limit,
    ]
    _append_config_options(run, config)
    append_port_bindings(run, host_config)
    for env_value in env:
        normalized = str(env_value)
        env_name = normalized.split("=", 1)[0]
        if env_name in {
            "NODE_STATE_DIR",
            "NAUTILUS_INITIAL_TRADING_STATE",
        }:
            continue
        run.extend(["-e", normalized])

    append_inherited_mounts(run, inherited_mounts, mounts)
    for source, destination, mode in mounts:
        run.extend(["-v", f"{source}:{destination}:{mode}"])

    run.extend(["-e", "NODE_STATE_DIR=/state"])
    run.extend(["-e", "NAUTILUS_INITIAL_TRADING_STATE=HALTED"])
    if entrypoint:
        run.extend(["--entrypoint", str(entrypoint[0])])
    run.append(image)
    if len(entrypoint) > 1:
        run.extend(str(value) for value in entrypoint[1:])
    run.extend(str(value) for value in cmd)
    lines.append(" ".join(shlex.quote(value) for value in run))
    for command in network_commands:
        lines.append(
            " ".join(shlex.quote(value) for value in command)
        )

    output_path = trader_root / f"recreate-{name}.sh"
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output_path.chmod(0o700)

    print(f"WROTE {output_path}")
    print(
        f"image: {image} | primary_net: {primary_network} "
        f"| restart: {restart} | memory: {memory_limit}/{memory_swap_limit}"
    )
    print("explicit mount pairs:")
    for source, destination, mode in mounts:
        print(f"  {source} -> {destination} ({mode})")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv

    try:
        name, binance_dst, binance_futures_dst = parse_args(argv)
        trader_root = Path(
            os.environ.get("TRADER_ROOT", "/srv/trader-v3")
        )
        generate(name, binance_dst, binance_futures_dst, trader_root)
    except (
        DeploymentConfigError,
        json.JSONDecodeError,
        OSError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
