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
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

from release_manifest import (
    DELIVERY_IMMUTABLE,
    DELIVERY_TRANSITION,
    IMMUTABLE_CODE_ROOTS,
    LABEL_RELEASE_COMMIT,
    LABEL_RELEASE_CONFIG,
    LABEL_RELEASE_DELIVERY,
    LABEL_RELEASE_ID,
    LABEL_RELEASE_IMAGE,
    NODE_DOCKER_RESOURCE_ARTIFACT,
    ReleaseManifestError,
    SCHEMA_VERSION,
    docker_resource_contract,
    sha256_file,
    validate_strict_release_envelope,
    validate_release_manifest,
)


class DeploymentConfigError(ValueError):
    pass


LABEL_RELEASE_DATABASE_SCHEMA = "io.trader.release.database-schema-epoch"


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
    (
        "bounded_task_worker.py",
        "/app/runtime/bounded_task_worker.py",
    ),
    (
        "control_plane_session.py",
        "/app/runtime/control_plane_session.py",
    ),
    (
        "intent_execution_inbox.py",
        "/app/runtime/intent_execution_inbox.py",
    ),
    ("redis_safety.py", "/app/runtime/redis_safety.py"),
    ("reconciliation.py", "/app/runtime/reconciliation.py"),
    ("binance_adapter_config.py", "/app/runtime/binance_adapter_config.py"),
    (
        "live_canary_execution.py",
        "/app/runtime/live_canary_execution.py",
    ),
    ("node_config.py", "/app/config/node_config.py"),
    ("risk_config.py", "/app/risk/config.py"),
    ("risk_init.py", "/app/risk/__init__.py"),
    ("node.py", "/app/app/node.py"),
    ("run_node.py", "/app/app/run_node.py"),
    ("health_server.py", "/app/app/health_server.py"),
    ("nautilus_actors.py", "/app/app/nautilus_actors.py"),
    (
        "approved_intent_client.py",
        "/app/data_client/approved_intent_client.py",
    ),
    ("nautilus_config.py", "/app/persistence/nautilus_config.py"),
    ("persistence_init.py", "/app/persistence/__init__.py"),
    (
        "redis_namespace_lease.py",
        "/app/persistence/redis_namespace_lease.py",
    ),
    ("redis_resp_client.py", "/app/persistence/redis_resp_client.py"),
)
BINANCE_EXECUTION_FILE = "binance_execution.py"
BINANCE_FUTURES_EXECUTION_FILE = "binance_futures_execution.py"


def parse_args(argv):
    values = list(argv[1:])
    release_manifest_path = False
    database_schema_epoch = False
    if "--release-manifest" in values:
        index = values.index("--release-manifest")
        if index + 1 >= len(values):
            raise DeploymentConfigError("--release-manifest requires a path")
        release_manifest_path = Path(values[index + 1])
        del values[index : index + 2]
    if "--database-schema-epoch" in values:
        index = values.index("--database-schema-epoch")
        if index + 1 >= len(values):
            raise DeploymentConfigError(
                "--database-schema-epoch requires a value"
            )
        database_schema_epoch = values[index + 1].strip()
        del values[index : index + 2]
    if len(values) not in {1, 3}:
        raise DeploymentConfigError(
            "expected CONTAINER for immutable mode or CONTAINER plus "
            "BINANCE_EXEC_DST and BINANCE_FUTURES_EXEC_DST for transition: "
            "hk-gen-recreate-patched.py CONTAINER "
            "[BINANCE_EXEC_DST BINANCE_FUTURES_EXEC_DST] "
            "[--release-manifest PATH] "
            "[--database-schema-epoch EPOCH]"
        )

    name = values[0].strip()
    binance_dst = False
    binance_futures_dst = False
    if len(values) == 3:
        binance_dst = values[1].strip()
        binance_futures_dst = values[2].strip()
    if not name:
        raise DeploymentConfigError("container name is empty")
    if release_manifest_path is not False:
        if not database_schema_epoch:
            raise DeploymentConfigError(
                "release recreation requires --database-schema-epoch"
            )
        if re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9._/-]{0,127}",
            database_schema_epoch,
        ) is None:
            raise DeploymentConfigError(
                "database schema epoch contains invalid characters"
            )
    elif database_schema_epoch is not False:
        raise DeploymentConfigError(
            "--database-schema-epoch requires --release-manifest"
        )
    if binance_dst is not False:
        if not binance_dst:
            raise DeploymentConfigError("BINANCE_EXEC_DST is empty")
        if not binance_dst.startswith("/"):
            raise DeploymentConfigError(
                "BINANCE_EXEC_DST must be an absolute path"
            )
        if re.search(r"[:\n]", binance_dst):
            raise DeploymentConfigError(
                "BINANCE_EXEC_DST contains invalid characters"
            )
    if binance_futures_dst is not False:
        if not binance_futures_dst:
            raise DeploymentConfigError("BINANCE_FUTURES_EXEC_DST is empty")
        if not binance_futures_dst.startswith("/"):
            raise DeploymentConfigError(
                "BINANCE_FUTURES_EXEC_DST must be an absolute path"
            )
        if re.search(r"[:\n]", binance_futures_dst):
            raise DeploymentConfigError(
                "BINANCE_FUTURES_EXEC_DST contains invalid characters"
            )

    return (
        name,
        binance_dst,
        binance_futures_dst,
        release_manifest_path,
        database_schema_epoch,
    )


def load_release_identity(path):
    if path is False:
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        manifest = validate_release_manifest(raw)
    except (OSError, json.JSONDecodeError, ReleaseManifestError) as exc:
        raise DeploymentConfigError(
            f"release manifest is invalid: {path}"
        ) from exc
    strict_envelope = False
    node_resources = False
    if manifest["release_purpose"] == "account_stall_hardening":
        pinned_reviewer = os.environ.get(
            "TRADER_RELEASE_REVIEWER_TRUST_SHA256",
            "",
        ).strip()
        try:
            strict_envelope = validate_strict_release_envelope(
                manifest,
                payload_root=path.parent,
                reviewer_trust_sha256=pinned_reviewer,
                verify_image_labels=True,
            )
            node_resources = docker_resource_contract(
                strict_envelope["systemd_resource_contract"],
                NODE_DOCKER_RESOURCE_ARTIFACT,
            )
        except ReleaseManifestError as exc:
            raise DeploymentConfigError(
                f"strict release envelope is invalid: {path}"
            ) from exc
    return {
        "commit": manifest["release_commit"],
        "config_sha256": manifest["config_sha256"],
        "dependency_lock_sha256": manifest["dependency_lock_sha256"],
        "delivery_mode": manifest["delivery_mode"],
        "files": manifest["files"],
        "image_digest": manifest["image_digest"],
        "release_id": manifest["release_id"],
        "manifest_schema_version": manifest["schema_version"],
        "node_configs": list(manifest.get("node_configs") or ()),
        "node_resources": node_resources,
        "release_purpose": manifest["release_purpose"],
    }


def validate_release_delivery_plan(
    release_identity,
    binance_dst,
    binance_futures_dst,
):
    if release_identity is False:
        return
    actual = {
        (item["bundle_path"], item["mount_target"])
        for item in release_identity["files"]
    }
    expected = set(PATCH_MOUNT_TARGETS)
    if release_identity["delivery_mode"] == DELIVERY_TRANSITION:
        if binance_dst is False or binance_futures_dst is False:
            raise DeploymentConfigError(
                "transition mode requires both Binance execution destinations"
            )
        expected.add((BINANCE_EXECUTION_FILE, binance_dst))
        expected.add(
            (BINANCE_FUTURES_EXECUTION_FILE, binance_futures_dst)
        )
    else:
        by_name = {
            item["bundle_path"]: item["mount_target"]
            for item in release_identity["files"]
        }
        for bundle_path in (
            BINANCE_EXECUTION_FILE,
            BINANCE_FUTURES_EXECUTION_FILE,
        ):
            target = by_name.get(bundle_path)
            if not target:
                raise DeploymentConfigError(
                    f"immutable manifest lacks {bundle_path}"
                )
            expected.add((bundle_path, target))
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise DeploymentConfigError(
            "release manifest and recreate delivery plan differ: "
            f"missing={missing} extra={extra}"
        )


def explicit_mounts(
    trader_root,
    suffix,
    binance_dst,
    binance_futures_dst,
    delivery_mode=DELIVERY_TRANSITION,
    *,
    release_identity=False,
    container_name="",
):
    patch_dir = trader_root / "container-patches"
    mounts = [
        (str(trader_root / "node-state" / suffix), "/state", "rw"),
    ]
    config_entry = release_node_config(
        release_identity,
        container_name,
    )
    if config_entry is not False:
        validate_release_node_config_artifact(config_entry)
        mounts.append(
            (
                config_entry["host_path"],
                config_entry["mount_target"],
                "ro",
            )
        )
    if delivery_mode == DELIVERY_IMMUTABLE:
        return mounts
    if binance_dst is False or binance_futures_dst is False:
        raise DeploymentConfigError(
            "transition mode requires both Binance execution destinations"
        )
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


def release_node_config(release_identity, container_name):
    if release_identity is False:
        return False
    if release_identity["manifest_schema_version"] != SCHEMA_VERSION:
        return False
    matches = [
        item
        for item in release_identity["node_configs"]
        if item["container"] == container_name
    ]
    if len(matches) != 1:
        raise DeploymentConfigError(
            "release manifest must bind one config artifact to "
            f"{container_name}"
        )
    return matches[0]


def validate_release_node_config_artifact(config_entry):
    path = Path(config_entry["host_path"])
    if path.is_symlink():
        raise DeploymentConfigError(
            f"release config artifact cannot be a symlink: {path}"
        )
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise DeploymentConfigError(
            f"release config artifact is missing: {path}"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise DeploymentConfigError(
            f"release config artifact must be regular: {path}"
        )
    if stat.S_IMODE(file_stat.st_mode) & 0o222:
        raise DeploymentConfigError(
            f"release config artifact must be read-only: {path}"
        )
    actual_hash = sha256_file(path)
    if actual_hash != config_entry["sha256"]:
        raise DeploymentConfigError(
            f"release config artifact hash mismatch: {path}"
        )


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


def _is_python_code_destination(destination):
    return re.search(r"\.py(?:c)?$", destination) is not None


def _mount_covers_destination(mount_destination, target):
    normalized_mount = mount_destination.rstrip("/") or "/"
    normalized_target = target.rstrip("/") or "/"
    if normalized_mount == "/":
        return True
    return (
        normalized_target == normalized_mount
        or normalized_target.startswith(f"{normalized_mount}/")
    )


def append_inherited_mounts(
    run,
    inherited_mounts,
    explicit,
    *,
    excluded_destinations=(),
    remove_python_code=False,
):
    explicit_sources = {source for source, _, _ in explicit}
    explicit_destinations = {destination for _, destination, _ in explicit}

    for mount in inherited_mounts:
        source = mount["Source"]
        destination = mount["Destination"]
        if source in explicit_sources or destination in explicit_destinations:
            continue
        if destination in excluded_destinations:
            continue
        if remove_python_code:
            overlaps_code_root = any(
                _mount_covers_destination(destination, code_root)
                or _mount_covers_destination(code_root, destination)
                for code_root in IMMUTABLE_CODE_ROOTS
            )
            covers_release_code = any(
                _mount_covers_destination(destination, target)
                for target in excluded_destinations
            )
            if (
                overlaps_code_root
                or covers_release_code
                or _is_python_code_destination(destination)
            ):
                continue

        mode = "rw"
        if not mount.get("RW", True):
            mode = "ro"
        run.extend(["-v", f"{source}:{destination}:{mode}"])


def _require_dict(value, field_name):
    if not isinstance(value, dict):
        raise DeploymentConfigError(
            f"docker inspect {field_name} must be an object"
        )
    return value


def _require_list(value, field_name):
    if not isinstance(value, list):
        raise DeploymentConfigError(
            f"docker inspect {field_name} must be an array"
        )
    return value


def _append_nonempty_option(run, option, value):
    normalized = str(value or "").strip()
    if normalized:
        run.extend([option, normalized])


def append_allowlisted_runtime_spec(
    run,
    inspected,
    name,
    *,
    reviewed_resources=False,
):
    config = _require_dict(inspected.get("Config"), "Config")
    host_config = _require_dict(
        inspected.get("HostConfig"),
        "HostConfig",
    )

    _append_nonempty_option(run, "--user", config.get("User"))
    _append_nonempty_option(run, "--workdir", config.get("WorkingDir"))
    _append_nonempty_option(run, "--hostname", config.get("Hostname"))

    port_bindings = host_config.get("PortBindings") or {}
    port_bindings = _require_dict(
        port_bindings,
        "HostConfig.PortBindings",
    )
    for container_port, bindings in sorted(port_bindings.items()):
        bindings = _require_list(
            bindings or [],
            f"HostConfig.PortBindings.{container_port}",
        )
        for binding in bindings:
            binding = _require_dict(
                binding,
                f"HostConfig.PortBindings.{container_port}[]",
            )
            host_ip = str(binding.get("HostIp") or "").strip()
            host_port = str(binding.get("HostPort") or "").strip()
            if not host_port:
                raise DeploymentConfigError(
                    "published ports must have a fixed HostPort"
                )
            published = f"{host_port}:{container_port}"
            if host_ip:
                published = f"{host_ip}:{published}"
            run.extend(["--publish", published])

    ulimits = _require_list(
        host_config.get("Ulimits") or [],
        "HostConfig.Ulimits",
    )
    for item in ulimits:
        item = _require_dict(item, "HostConfig.Ulimits[]")
        limit_name = str(item.get("Name") or "").strip()
        soft = item.get("Soft")
        hard = item.get("Hard")
        if (
            not limit_name
            or not isinstance(soft, int)
            or not isinstance(hard, int)
        ):
            raise DeploymentConfigError("docker ulimit is incomplete")
        if reviewed_resources is not False and limit_name == "nofile":
            continue
        run.extend(["--ulimit", f"{limit_name}={soft}:{hard}"])

    for option, field_name in (
        ("--security-opt", "SecurityOpt"),
        ("--cap-add", "CapAdd"),
        ("--cap-drop", "CapDrop"),
    ):
        values = _require_list(
            host_config.get(field_name) or [],
            f"HostConfig.{field_name}",
        )
        for value in values:
            normalized = str(value or "").strip()
            if normalized:
                run.extend([option, normalized])

    devices = _require_list(
        host_config.get("Devices") or [],
        "HostConfig.Devices",
    )
    for device in devices:
        device = _require_dict(device, "HostConfig.Devices[]")
        host_path = str(device.get("PathOnHost") or "").strip()
        container_path = str(
            device.get("PathInContainer") or ""
        ).strip()
        permissions = str(
            device.get("CgroupPermissions") or ""
        ).strip()
        if not host_path or not container_path or not permissions:
            raise DeploymentConfigError("docker device is incomplete")
        run.extend(
            [
                "--device",
                f"{host_path}:{container_path}:{permissions}",
            ]
        )

    if host_config.get("ReadonlyRootfs") is True:
        run.append("--read-only")
    _append_nonempty_option(run, "--pid", host_config.get("PidMode"))
    _append_nonempty_option(run, "--ipc", host_config.get("IpcMode"))

    if reviewed_resources is False:
        memory = host_config.get("Memory")
        memory_swap = host_config.get("MemorySwap")
        nano_cpus = host_config.get("NanoCpus")
        cpu_shares = host_config.get("CpuShares")
        if isinstance(memory, int) and memory > 0:
            run.extend(["--memory", str(memory)])
        if isinstance(memory_swap, int) and memory_swap > 0:
            run.extend(["--memory-swap", str(memory_swap)])
        if isinstance(nano_cpus, int) and nano_cpus > 0:
            run.extend(
                ["--cpus", f"{nano_cpus / 1_000_000_000:g}"]
            )
        if isinstance(cpu_shares, int) and cpu_shares > 0:
            run.extend(["--cpu-shares", str(cpu_shares)])
    else:
        run.extend(
            [
                "--memory",
                str(reviewed_resources["memory_bytes"]),
                "--memory-swap",
                str(reviewed_resources["memory_swap_bytes"]),
                "--cpus",
                f"{reviewed_resources['nano_cpus'] / 1_000_000_000:g}",
                "--pids-limit",
                str(reviewed_resources["pids_limit"]),
                "--ulimit",
                (
                    "nofile="
                    f"{reviewed_resources['nofile_soft']}:"
                    f"{reviewed_resources['nofile_hard']}"
                ),
            ]
        )

    log_config = _require_dict(
        host_config.get("LogConfig") or {},
        "HostConfig.LogConfig",
    )
    log_driver = str(log_config.get("Type") or "").strip()
    if log_driver:
        run.extend(["--log-driver", log_driver])
    log_options = _require_dict(
        log_config.get("Config") or {},
        "HostConfig.LogConfig.Config",
    )
    for key, value in sorted(log_options.items()):
        run.extend(["--log-opt", f"{key}={value}"])

    labels = _require_dict(
        config.get("Labels") or {},
        "Config.Labels",
    )
    for key, value in sorted(labels.items()):
        normalized_key = str(key)
        if (
            normalized_key.startswith("io.trader.release.")
            or normalized_key.startswith("com.trader.release.")
        ):
            continue
        run.extend(["--label", f"{key}={value}"])

    network_settings = _require_dict(
        inspected.get("NetworkSettings"),
        "NetworkSettings",
    )
    networks = _require_dict(
        network_settings.get("Networks"),
        "NetworkSettings.Networks",
    )
    if not networks:
        raise DeploymentConfigError(
            "container must be attached to at least one network"
        )
    network_names = list(networks)
    primary_network = network_names[0]
    run.append(f"--network={primary_network}")
    container_id = str(inspected.get("Id") or "")
    short_id = container_id[:12]
    network_connects = []
    for index, network_name in enumerate(network_names):
        network = _require_dict(
            networks[network_name],
            f"NetworkSettings.Networks.{network_name}",
        )
        aliases = []
        raw_aliases = network.get("Aliases") or []
        raw_aliases = _require_list(
            raw_aliases,
            f"NetworkSettings.Networks.{network_name}.Aliases",
        )
        for alias in raw_aliases:
            normalized = str(alias or "").strip()
            if not normalized:
                continue
            if normalized in {name, container_id, short_id}:
                continue
            aliases.append(normalized)
        if index == 0:
            for alias in aliases:
                run.extend(["--network-alias", alias])
            continue
        network_connects.append((network_name, tuple(aliases)))
    return primary_network, network_connects


def append_reviewed_resource_verification(
    lines,
    *,
    name,
    resources,
):
    quoted_name = shlex.quote(name)
    checks = (
        (
            "memory",
            "{{.HostConfig.Memory}}",
            resources["memory_bytes"],
        ),
        (
            "memory-swap",
            "{{.HostConfig.MemorySwap}}",
            resources["memory_swap_bytes"],
        ),
        (
            "CPU",
            "{{.HostConfig.NanoCpus}}",
            resources["nano_cpus"],
        ),
        (
            "tasks",
            "{{.HostConfig.PidsLimit}}",
            resources["pids_limit"],
        ),
        (
            "nofile",
            (
                '{{range .HostConfig.Ulimits}}'
                '{{if eq .Name "nofile"}}'
                "{{.Soft}}:{{.Hard}}"
                "{{end}}{{end}}"
            ),
            (
                f"{resources['nofile_soft']}:"
                f"{resources['nofile_hard']}"
            ),
        ),
        (
            "restart",
            "{{.HostConfig.RestartPolicy.Name}}",
            resources["restart_policy"],
        ),
    )
    for label, format_string, expected in checks:
        variable = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        lines.append(
            f"actual_{variable}=$(docker inspect --format "
            f"{shlex.quote(format_string)} {quoted_name})"
        )
        lines.extend(
            [
                f'if [ "$actual_{variable}" != {shlex.quote(str(expected))} ]; then',
                (
                    f'  echo "FATAL: reviewed Docker {label} resource '
                    f'mismatch for {name}" >&2'
                ),
                "  exit 1",
                "fi",
            ]
        )


def generate(
    name,
    binance_dst,
    binance_futures_dst,
    trader_root,
    release_manifest_path=False,
    database_schema_epoch=False,
):
    suffix = name.rsplit("-", 1)[-1]
    release_identity = load_release_identity(release_manifest_path)
    delivery_mode = DELIVERY_TRANSITION
    if release_identity is not False:
        delivery_mode = release_identity["delivery_mode"]
    validate_release_delivery_plan(
        release_identity,
        binance_dst,
        binance_futures_dst,
    )
    mounts = explicit_mounts(
        trader_root,
        suffix,
        binance_dst,
        binance_futures_dst,
        delivery_mode,
        release_identity=release_identity,
        container_name=name,
    )
    validate_mount_plan(mounts)
    if delivery_mode == DELIVERY_TRANSITION:
        validate_patch_sources(mounts, trader_root)

    inspect_output = subprocess.check_output(["docker", "inspect", name])
    inspected = json.loads(inspect_output)[0]
    current_image = inspected["Image"]
    image_ref = inspected["Config"]["Image"]
    image = current_image
    if release_identity is not False:
        image = release_identity["image_digest"]
    cmd = inspected["Config"]["Cmd"] or []
    entrypoint = inspected["Config"]["Entrypoint"] or []
    restart_policy = _require_dict(
        inspected["HostConfig"].get("RestartPolicy") or {},
        "HostConfig.RestartPolicy",
    )
    restart = str(restart_policy.get("Name") or "").strip()
    if not restart:
        restart = "unless-stopped"
    maximum_retry_count = restart_policy.get("MaximumRetryCount")
    if restart == "on-failure":
        if isinstance(maximum_retry_count, int) and maximum_retry_count > 0:
            restart = f"{restart}:{maximum_retry_count}"
    reviewed_resources = False
    if release_identity is not False:
        reviewed_resources = release_identity["node_resources"]
    if reviewed_resources is not False:
        restart = reviewed_resources["restart_policy"]

    state_dir = trader_root / "node-state" / suffix
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        "inherited_env=()",
        "while IFS= read -r env_value; do",
        '  inherited_env+=("$env_value")',
        (
            "done < <(docker inspect --format "
            "'{{range .Config.Env}}{{println .}}{{end}}' "
            f"{shlex.quote(name)})"
        ),
        (
            "old_pid=$(docker inspect --format "
            "'{{.State.Pid}}' "
            f"{shlex.quote(name)} 2>/dev/null || true)"
        ),
        f"docker rm -f {shlex.quote(name)} 2>/dev/null || true",
        'if [[ "$old_pid" =~ ^[1-9][0-9]*$ ]]; then',
        "  for _ in $(seq 1 100); do",
        '    if ! kill -0 "$old_pid" 2>/dev/null; then',
        "      break",
        "    fi",
        "    sleep 0.1",
        "  done",
        '  if kill -0 "$old_pid" 2>/dev/null; then',
        '    echo "FATAL: old node process is still alive: pid=$old_pid" >&2',
        "    exit 1",
        "  fi",
        "fi",
        f"mkdir -p {shlex.quote(str(state_dir))}",
    ]
    run = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        f"--restart={restart}",
    ]
    primary_network, network_connects = append_allowlisted_runtime_spec(
        run,
        inspected,
        name,
        reviewed_resources=reviewed_resources,
    )

    excluded_destinations = set()
    remove_python_code = False
    if release_identity is not False:
        excluded_destinations = {
            item["mount_target"]
            for item in release_identity["files"]
        }
        remove_python_code = (
            release_identity["delivery_mode"] == DELIVERY_IMMUTABLE
        )
    append_inherited_mounts(
        run,
        inspected["Mounts"],
        mounts,
        excluded_destinations=excluded_destinations,
        remove_python_code=remove_python_code,
    )
    for source, destination, mode in mounts:
        run.extend(["-v", f"{source}:{destination}:{mode}"])

    if release_identity is not False:
        labels = {
            LABEL_RELEASE_COMMIT: release_identity["commit"],
            LABEL_RELEASE_CONFIG: release_identity["config_sha256"],
            LABEL_RELEASE_DELIVERY: release_identity["delivery_mode"],
            LABEL_RELEASE_ID: release_identity["release_id"],
            LABEL_RELEASE_IMAGE: release_identity["image_digest"],
            LABEL_RELEASE_DATABASE_SCHEMA: database_schema_epoch,
        }
        for key, value in sorted(labels.items()):
            run.extend(["--label", f"{key}={value}"])
    if entrypoint:
        run.extend(["--entrypoint", entrypoint[0]])
    lines.append("run=(")
    for value in run:
        lines.append(f"  {shlex.quote(value)}")
    lines.extend(
        [
            ")",
            'for env_value in "${inherited_env[@]}"; do',
            '  env_name="${env_value%%=*}"',
            '  case "$env_name" in',
            (
                "    PATH|PYTHON*|LANG|GPG_KEY|HOME|"
                "NAUTILUS_INITIAL_TRADING_STATE|"
                "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON|"
                "NAUTILUS_MAX_ORDER_SUBMIT_RATE|"
                "NAUTILUS_MAX_ORDER_MODIFY_RATE|"
                "TRADER_RELEASE_COMMIT|TRADER_RELEASE_ID|"
                "TRADER_RELEASE_IMAGE_DIGEST|TRADER_RELEASE_CONFIG_SHA256|"
                "TRADER_RELEASE_DEPENDENCY_LOCK_SHA256|"
                "TRADER_RELEASE_SCHEMA_EPOCH|"
                "TRADER_RELEASE_MANIFEST_SCHEMA_VERSION|"
                "TRADER_RELEASE_PURPOSE)"
            ),
            "      continue",
            "      ;;",
            "  esac",
            '  run+=("-e" "$env_value")',
            "done",
            'run+=("-e" "NODE_STATE_DIR=/state")',
            'run+=("-e" "NAUTILUS_INITIAL_TRADING_STATE=HALTED")',
        ]
    )
    if release_identity is not False:
        lines.append(
            'run+=("-e" '
            f"{shlex.quote('TRADER_RELEASE_COMMIT=' + release_identity['commit'])}"
            ")"
        )
        lines.append(
            'run+=("-e" '
            f"{shlex.quote('TRADER_RELEASE_ID=' + release_identity['release_id'])}"
            ")"
        )
        lines.append(
            'run+=("-e" '
            f"{shlex.quote('TRADER_RELEASE_IMAGE_DIGEST=' + release_identity['image_digest'])}"
            ")"
        )
        lines.append(
            'run+=("-e" '
            f"{shlex.quote('TRADER_RELEASE_CONFIG_SHA256=' + release_identity['config_sha256'])}"
            ")"
        )
        lines.append(
            'run+=("-e" '
            f"{shlex.quote('TRADER_RELEASE_DEPENDENCY_LOCK_SHA256=' + release_identity['dependency_lock_sha256'])}"
            ")"
        )
        lines.append(
            'run+=("-e" '
            f"{shlex.quote('TRADER_RELEASE_SCHEMA_EPOCH=' + database_schema_epoch)}"
            ")"
        )
        lines.append(
            'run+=("-e" '
            f"{shlex.quote('TRADER_RELEASE_MANIFEST_SCHEMA_VERSION=' + release_identity['manifest_schema_version'])}"
            ")"
        )
        lines.append(
            'run+=("-e" '
            f"{shlex.quote('TRADER_RELEASE_PURPOSE=' + release_identity['release_purpose'])}"
            ")"
        )
    suffix = [image]
    if len(entrypoint) > 1:
        suffix.extend(entrypoint[1:])
    suffix.extend(cmd)
    for value in suffix:
        lines.append(f"run+=({shlex.quote(value)})")
    lines.extend(
        [
            'container_id="$("${run[@]}")"',
            '[ -n "$container_id" ] || {',
            '  echo "FATAL: docker run returned an empty container id" >&2',
            "  exit 1",
            "}",
        ]
    )
    for network_name, aliases in network_connects:
        connect = ["docker", "network", "connect"]
        for alias in aliases:
            connect.extend(["--alias", alias])
        connect.extend([network_name, name])
        lines.append(" ".join(shlex.quote(value) for value in connect))
    if reviewed_resources is not False:
        append_reviewed_resource_verification(
            lines,
            name=name,
            resources=reviewed_resources,
        )

    output_path = trader_root / f"recreate-{name}.sh"
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output_path.chmod(0o700)

    print(f"WROTE {output_path}")
    print(
        f"image: selected={image} current={image_ref} ({current_image})"
        f" | primary_net: {primary_network} | restart: {restart}"
    )
    if release_identity is not False:
        print(
            "release identity:"
            f" commit={release_identity['commit']}"
            f" release_id={release_identity['release_id']}"
            f" delivery={release_identity['delivery_mode']}"
            f" config={release_identity['config_sha256']}"
            f" db_schema={database_schema_epoch}"
        )
    print("explicit mount pairs:")
    for source, destination, mode in mounts:
        print(f"  {source} -> {destination} ({mode})")


def main(argv=None):
    if argv is None:
        argv = sys.argv

    try:
        (
            name,
            binance_dst,
            binance_futures_dst,
            release_manifest_path,
            database_schema_epoch,
        ) = parse_args(argv)
        trader_root = Path(os.environ.get("TRADER_ROOT", "/srv/trader-v3"))
        generate(
            name,
            binance_dst,
            binance_futures_dst,
            trader_root,
            release_manifest_path,
            database_schema_epoch,
        )
    except (DeploymentConfigError, subprocess.CalledProcessError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
