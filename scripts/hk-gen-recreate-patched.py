#!/usr/bin/env python3
"""Generate fail-closed recreate scripts for trader-v3 node containers.

Usage (root):
  python3 hk-gen-recreate-patched.py \
    trader-v3-node-a BINANCE_EXEC_DST BINANCE_FUTURES_EXEC_DST

Discover BINANCE_EXEC_DST from the running image before generation:
  docker exec trader-v3-node-a python -c \
    "import nautilus_trader.adapters.binance.execution as m; print(m.__file__)"
"""

import hashlib
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
    NODE_CONFIG_SCHEMA_VERSIONS,
    ReleaseManifestError,
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
    ("execution_domain_init.py", "/app/execution_domain/__init__.py"),
    (
        "account_execution_ledger.py",
        "/app/execution_domain/account_execution_ledger.py",
    ),
    ("idempotency.py", "/app/execution_domain/idempotency.py"),
    ("identifiers.py", "/app/execution_domain/identifiers.py"),
    ("contracts.py", "/app/execution_domain/contracts.py"),
    ("control_plane.py", "/app/execution_domain/control_plane.py"),
    (
        "portfolio_baseline.py",
        "/app/execution_domain/portfolio_baseline.py",
    ),
    ("order_ownership.py", "/app/execution_domain/order_ownership.py"),
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
        "owned_order_recovery.py",
        "/app/runtime/owned_order_recovery.py",
    ),
    (
        "intent_execution_inbox.py",
        "/app/runtime/intent_execution_inbox.py",
    ),
    ("redis_safety.py", "/app/runtime/redis_safety.py"),
    ("reconciliation.py", "/app/runtime/reconciliation.py"),
    ("binance_adapter_config.py", "/app/runtime/binance_adapter_config.py"),
    (
        "nautilus_reconciliation_scope.py",
        "/app/runtime/nautilus_reconciliation_scope.py",
    ),
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
    ("routing_init.py", "/app/routing/__init__.py"),
    (
        "routing_multi_account.py",
        "/app/routing/multi_account.py",
    ),
)
BINANCE_EXECUTION_FILE = "binance_execution.py"
BINANCE_FUTURES_EXECUTION_FILE = "binance_futures_execution.py"
SNAPSHOT_EVIDENCE_SCHEMA = "trader-v3-runtime-recreate-snapshot/v1"
DEFAULT_DOCKER_SHM_SIZE = 64 * 1024 * 1024
REVIEWED_NODE_HEALTH_HOST_PORTS = {
    "trader-v3-node-a": 8081,
    "trader-v3-node-b": 8082,
    "trader-v3-node-c": 8083,
    "trader-v3-node-d": 8084,
}


def _pop_flag(values, flag):
    if flag not in values:
        return False
    values.remove(flag)
    if flag in values:
        raise DeploymentConfigError(f"{flag} cannot be repeated")
    return True


def _pop_option(values, option):
    if option not in values:
        return False
    index = values.index(option)
    if index + 1 >= len(values):
        raise DeploymentConfigError(f"{option} requires a value")
    value = values[index + 1]
    del values[index : index + 2]
    if option in values:
        raise DeploymentConfigError(f"{option} cannot be repeated")
    return value


def parse_args(argv):
    values = list(argv[1:])
    release_manifest_path = False
    database_schema_epoch = False
    snapshot_runtime = _pop_flag(values, "--snapshot-runtime")
    output_raw = _pop_option(values, "--output")
    environment_output_raw = _pop_option(
        values,
        "--environment-output",
    )
    evidence_output_raw = _pop_option(values, "--evidence-output")
    verify_snapshot_raw = _pop_option(
        values,
        "--verify-snapshot-evidence",
    )
    release_manifest_raw = _pop_option(values, "--release-manifest")
    database_schema_raw = _pop_option(
        values,
        "--database-schema-epoch",
    )
    if release_manifest_raw is not False:
        release_manifest_path = Path(release_manifest_raw)
    if database_schema_raw is not False:
        database_schema_epoch = database_schema_raw.strip()
    if len(values) not in {1, 3}:
        raise DeploymentConfigError(
            "expected CONTAINER for immutable mode or CONTAINER plus "
            "BINANCE_EXEC_DST and BINANCE_FUTURES_EXEC_DST for transition: "
            "hk-gen-recreate-patched.py CONTAINER "
            "[BINANCE_EXEC_DST BINANCE_FUTURES_EXEC_DST] "
            "[--release-manifest PATH] "
            "[--database-schema-epoch EPOCH] or snapshot runtime options"
        )

    name = values[0].strip()
    binance_dst = False
    binance_futures_dst = False
    if len(values) == 3:
        binance_dst = values[1].strip()
        binance_futures_dst = values[2].strip()
    if not name:
        raise DeploymentConfigError("container name is empty")
    snapshot_mode = snapshot_runtime or verify_snapshot_raw is not False
    if snapshot_mode:
        if len(values) != 3:
            raise DeploymentConfigError(
                "snapshot runtime mode requires both Binance "
                "execution destinations"
            )
        if release_manifest_path is not False or database_schema_epoch is not False:
            raise DeploymentConfigError(
                "snapshot runtime mode cannot use a release manifest"
            )
        if snapshot_runtime and verify_snapshot_raw is not False:
            raise DeploymentConfigError(
                "snapshot generation and verification are separate operations"
            )
        output_values = (
            output_raw,
            environment_output_raw,
            evidence_output_raw,
        )
        if snapshot_runtime and any(
            value is False for value in output_values
        ):
            raise DeploymentConfigError(
                "snapshot runtime requires --output, "
                "--environment-output, and --evidence-output"
            )
        if verify_snapshot_raw is not False and any(
            value is not False for value in output_values
        ):
            raise DeploymentConfigError(
                "snapshot verification cannot use generation outputs"
            )
    elif any(
        value is not False
        for value in (
            output_raw,
            environment_output_raw,
            evidence_output_raw,
        )
    ):
        raise DeploymentConfigError(
            "snapshot output options require --snapshot-runtime"
        )
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

    def output_path(raw, option):
        if raw is False:
            return False
        path = Path(raw)
        if not path.is_absolute():
            raise DeploymentConfigError(
                f"{option} must be an absolute path"
            )
        return path

    return (
        name,
        binance_dst,
        binance_futures_dst,
        release_manifest_path,
        database_schema_epoch,
        snapshot_runtime,
        output_path(output_raw, "--output"),
        output_path(
            environment_output_raw,
            "--environment-output",
        ),
        output_path(evidence_output_raw, "--evidence-output"),
        output_path(
            verify_snapshot_raw,
            "--verify-snapshot-evidence",
        ),
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
    build_labels = {}
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
            raw_build_labels = strict_envelope["build_attestation"][
                "image_labels"
            ]
            build_labels = {
                str(key): str(value)
                for key, value in raw_build_labels.items()
            }
        except ReleaseManifestError as exc:
            raise DeploymentConfigError(
                f"strict release envelope is invalid: {path}"
            ) from exc
    return {
        "build_labels": build_labels,
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
    if (
        release_identity["manifest_schema_version"]
        not in NODE_CONFIG_SCHEMA_VERSIONS
    ):
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


def _reviewed_health_container_port(config, name):
    expected_host_port = REVIEWED_NODE_HEALTH_HOST_PORTS.get(name)
    if expected_host_port is None:
        return False
    environment = _require_list(
        config.get("Env") or [],
        "Config.Env",
    )
    values = []
    for item in environment:
        if not isinstance(item, str) or "=" not in item:
            continue
        env_name, value = item.split("=", 1)
        if env_name == "NAUTILUS_HEALTH_PORT":
            values.append(value.strip())
    if len(values) != 1:
        raise DeploymentConfigError(
            f"{name} must define one NAUTILUS_HEALTH_PORT"
        )
    raw_port = values[0]
    if re.fullmatch(r"[1-9][0-9]{0,4}", raw_port) is None:
        raise DeploymentConfigError(
            f"{name} NAUTILUS_HEALTH_PORT is invalid"
        )
    container_port = int(raw_port)
    if container_port > 65535:
        raise DeploymentConfigError(
            f"{name} NAUTILUS_HEALTH_PORT is invalid"
        )
    return expected_host_port, container_port


def append_allowlisted_runtime_spec(
    run,
    inspected,
    name,
    *,
    preserve_build_labels=True,
    reviewed_resources=False,
    preserve_release_labels=False,
    preserve_runtime_defaults=False,
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
    if reviewed_resources is not False and not port_bindings:
        health_ports = _reviewed_health_container_port(config, name)
        if health_ports is not False:
            host_port, container_port = health_ports
            port_bindings = {
                f"{container_port}/tcp": [
                    {
                        "HostIp": "127.0.0.1",
                        "HostPort": str(host_port),
                    }
                ]
            }
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
    if preserve_runtime_defaults:
        _append_nonempty_option(
            run,
            "--cgroupns",
            host_config.get("CgroupnsMode"),
        )
        _append_nonempty_option(
            run,
            "--runtime",
            host_config.get("Runtime"),
        )
        shm_size = host_config.get("ShmSize")
        if isinstance(shm_size, int) and shm_size > 0:
            run.extend(["--shm-size", str(shm_size)])

    if reviewed_resources is False:
        memory = host_config.get("Memory")
        memory_swap = host_config.get("MemorySwap")
        nano_cpus = host_config.get("NanoCpus")
        cpu_shares = host_config.get("CpuShares")
        if isinstance(memory, int) and memory > 0:
            run.extend(["--memory", str(memory)])
        if (
            isinstance(memory_swap, int)
            and (memory_swap > 0 or memory_swap == -1)
        ):
            run.extend(["--memory-swap", str(memory_swap)])
        if isinstance(nano_cpus, int) and nano_cpus > 0:
            run.extend(
                ["--cpus", f"{nano_cpus / 1_000_000_000:g}"]
            )
        if isinstance(cpu_shares, int) and cpu_shares > 0:
            run.extend(["--cpu-shares", str(cpu_shares)])
        pids_limit = host_config.get("PidsLimit")
        if isinstance(pids_limit, int) and pids_limit > 0:
            run.extend(["--pids-limit", str(pids_limit)])
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
            preserve_release_labels is False
            and (
                normalized_key.startswith("io.trader.release.")
                or normalized_key.startswith("com.trader.release.")
            )
        ):
            continue
        if (
            preserve_build_labels is False
            and normalized_key.startswith("com.trader.build.")
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


def _canonical_json_sha256(value):
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_file(path, payload, mode):
    if not path.parent.is_dir():
        raise DeploymentConfigError(
            f"snapshot output directory is missing: {path.parent}"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, mode)
    except OSError as exc:
        raise DeploymentConfigError(
            f"snapshot output cannot be created: {path}"
        ) from exc
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _snapshot_environment(inspected):
    config = _require_dict(inspected.get("Config"), "Config")
    environment = _require_list(config.get("Env") or [], "Config.Env")
    normalized = []
    names = set()
    for raw in environment:
        value = str(raw)
        if re.search(r"[\x00\r\n]", value):
            raise DeploymentConfigError(
                "container environment contains an unsupported control byte"
            )
        name, separator, _ = value.partition("=")
        if (
            not separator
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
        ):
            raise DeploymentConfigError(
                "container environment entry is invalid"
            )
        if name in names:
            raise DeploymentConfigError(
                f"container environment name is duplicated: {name}"
            )
        names.add(name)
        normalized.append(value)
    halted_values = [
        value
        for value in normalized
        if value.startswith("NAUTILUS_INITIAL_TRADING_STATE=")
    ]
    if halted_values != ["NAUTILUS_INITIAL_TRADING_STATE=HALTED"]:
        raise DeploymentConfigError(
            "snapshot source container must already be HALTED"
        )
    return normalized


def _snapshot_mounts(inspected):
    raw_mounts = _require_list(inspected.get("Mounts") or [], "Mounts")
    mounts = []
    destinations = set()
    for raw in raw_mounts:
        mount = _require_dict(raw, "Mounts[]")
        mount_type = str(mount.get("Type") or "").strip()
        destination = str(mount.get("Destination") or "").strip()
        if not destination.startswith("/") or ":" in destination:
            raise DeploymentConfigError(
                f"snapshot mount destination is invalid: {destination}"
            )
        if destination in destinations:
            raise DeploymentConfigError(
                f"snapshot mount destination is duplicated: {destination}"
            )
        destinations.add(destination)
        source = str(mount.get("Source") or "").strip()
        volume_name = str(mount.get("Name") or "").strip()
        if mount_type == "bind":
            if not source.startswith("/") or ":" in source:
                raise DeploymentConfigError(
                    f"snapshot bind source is invalid: {source}"
                )
            run_source = source
        elif mount_type == "volume":
            if not volume_name or re.search(r"[:\n]", volume_name):
                raise DeploymentConfigError(
                    f"snapshot volume name is invalid: {volume_name}"
                )
            run_source = volume_name
        else:
            raise DeploymentConfigError(
                f"unsupported snapshot mount type: {mount_type}"
            )
        writable = mount.get("RW") is True
        propagation = str(mount.get("Propagation") or "").strip()
        if mount_type == "bind" and propagation not in {"", "rprivate"}:
            raise DeploymentConfigError(
                "snapshot mount propagation is unsupported: "
                f"{source} -> {destination} ({propagation})"
            )
        mode = str(mount.get("Mode") or "").strip()
        if not mode:
            mode = "rw"
            if not writable:
                mode = "ro"
        if re.fullmatch(r"[a-zA-Z0-9,._-]+", mode) is None:
            raise DeploymentConfigError(
                f"snapshot mount mode is invalid: {mode}"
            )
        mounts.append(
            {
                "destination": destination,
                "mode": mode,
                "name": volume_name,
                "propagation": propagation,
                "run_source": run_source,
                "rw": writable,
                "source": source,
                "type": mount_type,
            }
        )
    return sorted(
        mounts,
        key=lambda item: (
            item["destination"],
            item["run_source"],
        ),
    )


def _is_inactive(value):
    if value is None or value is False:
        return True
    if value == "" or value == 0:
        return True
    if value == [] or value == {}:
        return True
    return False


def _validate_snapshot_config(inspected):
    config = _require_dict(inspected.get("Config"), "Config")
    inactive_fields = (
        "AttachStderr",
        "AttachStdin",
        "AttachStdout",
        "Domainname",
        "ExposedPorts",
        "Healthcheck",
        "MacAddress",
        "OnBuild",
        "OpenStdin",
        "StdinOnce",
        "StopSignal",
        "StopTimeout",
        "Tty",
        "Volumes",
    )
    for field_name in inactive_fields:
        value = config.get(field_name)
        if not _is_inactive(value):
            raise DeploymentConfigError(
                f"snapshot Config.{field_name} is unsupported"
            )


def _validate_snapshot_host_config(inspected):
    host_config = _require_dict(
        inspected.get("HostConfig"),
        "HostConfig",
    )
    inactive_fields = (
        "AutoRemove",
        "BlkioDeviceReadBps",
        "BlkioDeviceReadIOps",
        "BlkioDeviceWriteBps",
        "BlkioDeviceWriteIOps",
        "BlkioWeight",
        "BlkioWeightDevice",
        "Cgroup",
        "CgroupParent",
        "ConsoleSize",
        "CpuCount",
        "CpuPercent",
        "CpuPeriod",
        "CpuQuota",
        "CpuRealtimePeriod",
        "CpuRealtimeRuntime",
        "CpusetCpus",
        "CpusetMems",
        "DeviceCgroupRules",
        "DeviceRequests",
        "Dns",
        "DnsOptions",
        "DnsSearch",
        "ExtraHosts",
        "GroupAdd",
        "Init",
        "IOMaximumBandwidth",
        "IOMaximumIOps",
        "Isolation",
        "Links",
        "MemoryReservation",
        "MemorySwappiness",
        "OomKillDisable",
        "OomScoreAdj",
        "Privileged",
        "PublishAllPorts",
        "StorageOpt",
        "Sysctls",
        "Tmpfs",
        "UTSMode",
        "UsernsMode",
        "VolumeDriver",
        "VolumesFrom",
    )
    for field_name in inactive_fields:
        value = host_config.get(field_name)
        if field_name == "ConsoleSize" and value == [0, 0]:
            continue
        if not _is_inactive(value):
            raise DeploymentConfigError(
                f"snapshot HostConfig.{field_name} is unsupported"
            )
    shm_size = host_config.get("ShmSize")
    if shm_size not in {None, 0, DEFAULT_DOCKER_SHM_SIZE}:
        raise DeploymentConfigError(
            "snapshot HostConfig.ShmSize is unsupported"
        )


def _validate_snapshot_networks(inspected):
    host_config = _require_dict(
        inspected.get("HostConfig"),
        "HostConfig",
    )
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
    primary_network = next(iter(networks))
    network_mode = str(host_config.get("NetworkMode") or "").strip()
    compatible_modes = {"", primary_network}
    if primary_network == "bridge":
        compatible_modes.add("default")
    if network_mode not in compatible_modes:
        raise DeploymentConfigError(
            "snapshot HostConfig.NetworkMode differs from primary network"
        )
    unsupported_fields = (
        "DriverOpts",
        "IPAMConfig",
        "Links",
        "MacAddress",
    )
    for network_name, raw in sorted(networks.items()):
        network = _require_dict(
            raw,
            f"NetworkSettings.Networks.{network_name}",
        )
        for field_name in unsupported_fields:
            value = network.get(field_name)
            if not _is_inactive(value):
                raise DeploymentConfigError(
                    "snapshot network endpoint option is unsupported: "
                    f"{network_name}.{field_name}"
                )


def _validate_snapshot_replayability(inspected):
    _validate_snapshot_config(inspected)
    _validate_snapshot_host_config(inspected)
    _validate_snapshot_networks(inspected)


def _validate_snapshot_binance_targets(
    mounts,
    binance_dst,
    binance_futures_dst,
):
    requirements = (
        (BINANCE_EXECUTION_FILE, binance_dst),
        (BINANCE_FUTURES_EXECUTION_FILE, binance_futures_dst),
    )
    for filename, destination in requirements:
        matches = [
            mount
            for mount in mounts
            if mount["destination"] == destination
            and Path(mount["source"]).name == filename
        ]
        if len(matches) != 1:
            raise DeploymentConfigError(
                "snapshot source lacks one canonical Binance mount: "
                f"{filename} -> {destination}"
            )


def _snapshot_runtime_contract(inspected, environment, mounts):
    config = _require_dict(inspected.get("Config"), "Config")
    host_config = _require_dict(
        inspected.get("HostConfig"),
        "HostConfig",
    )
    network_settings = _require_dict(
        inspected.get("NetworkSettings"),
        "NetworkSettings",
    )
    networks = _require_dict(
        network_settings.get("Networks"),
        "NetworkSettings.Networks",
    )
    normalized_networks = {}
    for name, raw in sorted(networks.items()):
        network = _require_dict(
            raw,
            f"NetworkSettings.Networks.{name}",
        )
        aliases = _require_list(
            network.get("Aliases") or [],
            f"NetworkSettings.Networks.{name}.Aliases",
        )
        normalized_networks[str(name)] = {
            "aliases": [str(alias) for alias in aliases],
        }
    restart_policy = _require_dict(
        host_config.get("RestartPolicy") or {},
        "HostConfig.RestartPolicy",
    )
    replayed_host_fields = (
        "CapAdd",
        "CapDrop",
        "CgroupnsMode",
        "CpuShares",
        "Devices",
        "IpcMode",
        "LogConfig",
        "Memory",
        "MemorySwap",
        "NanoCpus",
        "NetworkMode",
        "PidMode",
        "PidsLimit",
        "PortBindings",
        "ReadonlyRootfs",
        "Runtime",
        "SecurityOpt",
        "ShmSize",
        "Ulimits",
    )
    return {
        "image_digest": str(inspected.get("Image") or ""),
        "config_image": str(config.get("Image") or ""),
        "environment_count": len(environment),
        "environment_names": sorted(
            value.partition("=")[0] for value in environment
        ),
        "environment_sha256": _canonical_json_sha256(environment),
        "container_config": {
            "Cmd": config.get("Cmd"),
            "Entrypoint": config.get("Entrypoint"),
            "Hostname": config.get("Hostname"),
            "Labels": config.get("Labels") or {},
            "User": config.get("User"),
            "WorkingDir": config.get("WorkingDir"),
        },
        "host_config": {
            field_name: host_config.get(field_name)
            for field_name in replayed_host_fields
        },
        "mounts": mounts,
        "networks": normalized_networks,
        "restart_policy": {
            "MaximumRetryCount": restart_policy.get(
                "MaximumRetryCount"
            ),
            "Name": restart_policy.get("Name"),
        },
    }


def _snapshot_restart_policy(inspected):
    host_config = _require_dict(
        inspected.get("HostConfig"),
        "HostConfig",
    )
    policy = _require_dict(
        host_config.get("RestartPolicy") or {},
        "HostConfig.RestartPolicy",
    )
    name = str(policy.get("Name") or "").strip()
    if not name:
        name = "no"
    retry_count = policy.get("MaximumRetryCount")
    if name == "on-failure":
        if isinstance(retry_count, int) and retry_count > 0:
            return f"{name}:{retry_count}"
    return name


def _snapshot_script_lines(
    name,
    inspected,
    environment_path,
    environment_sha256,
    mounts,
):
    config = _require_dict(inspected.get("Config"), "Config")
    image = str(inspected.get("Image") or "").strip()
    if not image:
        raise DeploymentConfigError(
            "snapshot source image digest is missing"
        )
    cmd = _require_list(config.get("Cmd") or [], "Config.Cmd")
    entrypoint = _require_list(
        config.get("Entrypoint") or [],
        "Config.Entrypoint",
    )
    restart = _snapshot_restart_policy(inspected)
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        f"snapshot_env={shlex.quote(str(environment_path))}",
        f"snapshot_env_sha256={shlex.quote(environment_sha256)}",
        'actual_env_sha256="$(sha256sum "$snapshot_env" | awk \'{print $1}\')"',
        'if [ "$actual_env_sha256" != "$snapshot_env_sha256" ]; then',
        '  echo "FATAL: rollback environment snapshot hash mismatch" >&2',
        "  exit 1",
        "fi",
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
    ]
    run = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        f"--restart={restart}",
        "--env-file",
        str(environment_path),
    ]
    primary_network, network_connects = append_allowlisted_runtime_spec(
        run,
        inspected,
        name,
        preserve_release_labels=True,
        preserve_runtime_defaults=True,
    )
    for mount in mounts:
        run.extend(
            [
                "-v",
                (
                    f"{mount['run_source']}:"
                    f"{mount['destination']}:{mount['mode']}"
                ),
            ]
        )
    if entrypoint:
        run.extend(["--entrypoint", str(entrypoint[0])])
    lines.append("run=(")
    for value in run:
        lines.append(f"  {shlex.quote(str(value))}")
    lines.append(")")
    suffix = [image]
    if len(entrypoint) > 1:
        suffix.extend(entrypoint[1:])
    suffix.extend(cmd)
    for value in suffix:
        lines.append(f"run+=({shlex.quote(str(value))})")
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
    return lines, primary_network, restart


def generate_snapshot_runtime(
    name,
    binance_dst,
    binance_futures_dst,
    output_path,
    environment_output_path,
    evidence_output_path,
):
    inspect_output = subprocess.check_output(["docker", "inspect", name])
    inspected = json.loads(inspect_output)[0]
    state = _require_dict(inspected.get("State"), "State")
    if state.get("Running") is not False:
        raise DeploymentConfigError(
            "snapshot source container must be stopped"
        )
    _validate_snapshot_replayability(inspected)
    environment = _snapshot_environment(inspected)
    mounts = _snapshot_mounts(inspected)
    _validate_snapshot_binance_targets(
        mounts,
        binance_dst,
        binance_futures_dst,
    )
    runtime_contract = _snapshot_runtime_contract(
        inspected,
        environment,
        mounts,
    )
    environment_bytes = ("\n".join(environment) + "\n").encode("utf-8")
    _write_private_file(
        environment_output_path,
        environment_bytes,
        0o400,
    )
    environment_file_sha256 = _file_sha256(environment_output_path)
    lines, primary_network, restart = _snapshot_script_lines(
        name,
        inspected,
        environment_output_path,
        environment_file_sha256,
        mounts,
    )
    script_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    _write_private_file(output_path, script_bytes, 0o700)
    evidence = {
        "schema_version": SNAPSHOT_EVIDENCE_SCHEMA,
        "container": name,
        "container_id": str(inspected.get("Id") or ""),
        "environment_path": str(environment_output_path),
        "environment_sha256": environment_file_sha256,
        "recreate_path": str(output_path),
        "recreate_sha256": _file_sha256(output_path),
        "runtime_contract": runtime_contract,
    }
    evidence_bytes = (
        json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_private_file(
        evidence_output_path,
        evidence_bytes,
        0o400,
    )
    print(
        f"WROTE snapshot recreate for {name}"
        f" | primary_net: {primary_network} | restart: {restart}"
    )


def _read_private_snapshot_file(path, expected_mode, label):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DeploymentConfigError(
            f"{label} is missing: {path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise DeploymentConfigError(
                f"{label} must be regular: {path}"
            )
        if stat.S_IMODE(before.st_mode) != expected_mode:
            raise DeploymentConfigError(
                f"{label} mode mismatch: {path}"
            )
        if before.st_nlink != 1:
            raise DeploymentConfigError(
                f"{label} link count is invalid: {path}"
            )
        if before.st_uid != os.geteuid():
            raise DeploymentConfigError(
                f"{label} owner mismatch: {path}"
            )
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        path_metadata = os.lstat(path)
        stable = (
            before.st_dev == after.st_dev == path_metadata.st_dev,
            before.st_ino == after.st_ino == path_metadata.st_ino,
            before.st_size == after.st_size == path_metadata.st_size,
            before.st_mtime_ns
            == after.st_mtime_ns
            == path_metadata.st_mtime_ns,
            before.st_ctime_ns
            == after.st_ctime_ns
            == path_metadata.st_ctime_ns,
        )
        if not all(stable):
            raise DeploymentConfigError(
                f"{label} changed during read: {path}"
            )
        payload = b"".join(chunks)
        digest = hashlib.sha256(payload).hexdigest()
        return payload, digest
    except OSError as exc:
        raise DeploymentConfigError(
            f"{label} is unreadable: {path}"
        ) from exc
    finally:
        os.close(descriptor)


def verify_snapshot_evidence(
    name,
    binance_dst,
    binance_futures_dst,
    evidence_path,
):
    evidence_bytes, _ = _read_private_snapshot_file(
        evidence_path,
        0o400,
        "snapshot evidence",
    )
    try:
        evidence = json.loads(evidence_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentConfigError(
            f"snapshot evidence is invalid: {evidence_path}"
        ) from exc
    if evidence.get("schema_version") != SNAPSHOT_EVIDENCE_SCHEMA:
        raise DeploymentConfigError(
            "snapshot evidence schema is invalid"
        )
    if evidence.get("container") != name:
        raise DeploymentConfigError(
            "snapshot evidence container differs"
        )
    environment_path = Path(str(evidence.get("environment_path") or ""))
    recreate_path = Path(str(evidence.get("recreate_path") or ""))
    environment_bytes, environment_sha256 = _read_private_snapshot_file(
        environment_path,
        0o400,
        "snapshot environment",
    )
    recreate_bytes, recreate_sha256 = _read_private_snapshot_file(
        recreate_path,
        0o700,
        "snapshot recreate",
    )
    if environment_sha256 != evidence.get("environment_sha256"):
        raise DeploymentConfigError(
            "snapshot environment hash mismatch"
        )
    if recreate_sha256 != evidence.get("recreate_sha256"):
        raise DeploymentConfigError(
            "snapshot recreate hash mismatch"
        )
    try:
        environment = environment_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise DeploymentConfigError(
            "snapshot environment is invalid"
        ) from exc
    inspect_output = subprocess.check_output(["docker", "inspect", name])
    inspected = json.loads(inspect_output)[0]
    state = _require_dict(inspected.get("State"), "State")
    if state.get("Running") is not False:
        raise DeploymentConfigError(
            "snapshot verification requires a stopped container"
        )
    _validate_snapshot_replayability(inspected)
    live_environment = _snapshot_environment(inspected)
    if live_environment != environment:
        raise DeploymentConfigError(
            "snapshot environment differs from live inspect"
        )
    mounts = _snapshot_mounts(inspected)
    _validate_snapshot_binance_targets(
        mounts,
        binance_dst,
        binance_futures_dst,
    )
    contract = _snapshot_runtime_contract(
        inspected,
        live_environment,
        mounts,
    )
    if contract != evidence.get("runtime_contract"):
        raise DeploymentConfigError(
            "snapshot runtime contract differs from live inspect"
        )
    if str(inspected.get("Id") or "") != evidence.get("container_id"):
        raise DeploymentConfigError(
            "snapshot source container identity differs"
        )
    expected_lines, _, _ = _snapshot_script_lines(
        name,
        inspected,
        environment_path,
        str(evidence.get("environment_sha256") or ""),
        mounts,
    )
    expected_script = "\n".join(expected_lines) + "\n"
    try:
        actual_script = recreate_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeploymentConfigError(
            "snapshot recreate is invalid"
        ) from exc
    if actual_script != expected_script:
        raise DeploymentConfigError(
            "snapshot recreate differs from live runtime contract"
        )
    print(f"VERIFIED snapshot recreate for {name}")


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


def append_active_release_image_guard(
    lines,
    *,
    trader_root,
    expected_image,
):
    database_environment = trader_root / ".env.v3"
    database_python = trader_root / ".venv-cp" / "bin" / "python"
    lines.extend(
        [
            'active_release_image="$(',
            (
                f"  {shlex.quote(str(database_python))} - "
                f"{shlex.quote(str(database_environment))} <<'PY'"
            ),
            "import sys",
            "from pathlib import Path",
            "",
            "import psycopg2",
            "",
            "",
            "def read_database_url(path):",
            (
                "    for raw in Path(path).read_text("
                "encoding=\"utf-8\").splitlines():"
            ),
            "        line = raw.strip()",
            (
                "        if not line or line.startswith(\"#\") "
                "or \"=\" not in line:"
            ),
            "            continue",
            "        key, value = line.split(\"=\", 1)",
            "        if key.strip() != \"DATABASE_URL\":",
            "            continue",
            "        value = value.strip()",
            "        if (",
            "            len(value) >= 2",
            "            and value[0] == value[-1]",
            "            and value[0] in {\"'\", '\"'}",
            "        ):",
            "            value = value[1:-1]",
            "        return value",
            "    raise SystemExit(\"DATABASE_URL is missing\")",
            "",
            "",
            "database_url = read_database_url(sys.argv[1])",
            "with psycopg2.connect(database_url) as conn:",
            "    with conn.cursor() as cur:",
            "        cur.execute(",
            "            \"\"\"",
            "            SELECT image_digest",
            "            FROM reviewed_release_rollouts",
            "            WHERE phase IN (",
            "                'account_a_canary',",
            "                'account_b_rollout',",
            "                'account_c_rollout',",
            "                'account_d_rollout'",
            "            )",
            "            \"\"\"",
            "        )",
            "        active_rows = cur.fetchall()",
            "        if len(active_rows) == 1:",
            "            selected = active_rows[0]",
            "        elif active_rows:",
            "            raise SystemExit(",
            "                \"multiple DB active releases; use bootstrap_stopped\"",
            "            )",
            "        else:",
            "            cur.execute(",
            "                \"\"\"",
            "                SELECT image_digest",
            "                FROM reviewed_release_rollouts",
            "                WHERE phase='fleet_complete'",
            "                ORDER BY reviewed_at DESC, release_id DESC",
            "                LIMIT 1",
            "                \"\"\"",
            "            )",
            "            selected = cur.fetchone()",
            "if selected is None:",
            "    raise SystemExit(",
            "        \"DB active release is missing; use bootstrap_stopped\"",
            "    )",
            "print(str(selected[0]))",
            "PY",
            ')"',
            (
                "if [ \"$active_release_image\" != "
                f"{shlex.quote(expected_image)} ]; then"
            ),
            (
                f'  echo "FATAL: recreate image {expected_image} differs '
                'from DB active release image $active_release_image; '
                'use bootstrap_stopped" >&2'
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

    excluded_environment_pattern = (
        "PATH|PYTHON*|LANG|GPG_KEY|HOME|"
        "NAUTILUS_INITIAL_TRADING_STATE|"
        "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON|"
        "NAUTILUS_MAX_ORDER_SUBMIT_RATE|"
        "NAUTILUS_MAX_ORDER_MODIFY_RATE|"
        "TRADER_RELEASE_COMMIT|TRADER_RELEASE_ID|"
        "TRADER_RELEASE_IMAGE_DIGEST|TRADER_RELEASE_CONFIG_SHA256|"
        "TRADER_RELEASE_DEPENDENCY_LOCK_SHA256|"
        "TRADER_RELEASE_SCHEMA_EPOCH|"
        "TRADER_RELEASE_MANIFEST_SCHEMA_VERSION|"
        "TRADER_RELEASE_PURPOSE"
    )
    if release_identity is not False:
        excluded_environment_pattern += "|NAUTILUS_HEALTH_HOST"

    state_dir = trader_root / "node-state" / suffix
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
    ]
    if release_identity is not False:
        append_active_release_image_guard(
            lines,
            trader_root=trader_root,
            expected_image=release_identity["image_digest"],
        )
    lines.extend(
        [
        "inherited_env=()",
        "while IFS= read -r env_value; do",
        # An empty inherited entry would become `docker run -e ""`,
        # which docker rejects after the old container is already gone.
        '  [ -n "$env_value" ] || continue',
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
    )
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
        preserve_build_labels=release_identity is False,
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
        labels.update(release_identity["build_labels"])
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
            f"    {excluded_environment_pattern})",
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
            'run+=("-e" "NAUTILUS_HEALTH_HOST=0.0.0.0")'
        )
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
            snapshot_runtime,
            output_path,
            environment_output_path,
            evidence_output_path,
            verify_snapshot_path,
        ) = parse_args(argv)
        trader_root = Path(os.environ.get("TRADER_ROOT", "/srv/trader-v3"))
        if snapshot_runtime:
            generate_snapshot_runtime(
                name,
                binance_dst,
                binance_futures_dst,
                output_path,
                environment_output_path,
                evidence_output_path,
            )
        elif verify_snapshot_path is not False:
            verify_snapshot_evidence(
                name,
                binance_dst,
                binance_futures_dst,
                verify_snapshot_path,
            )
        else:
            generate(
                name,
                binance_dst,
                binance_futures_dst,
                trader_root,
                release_manifest_path,
                database_schema_epoch,
            )
    except (
        DeploymentConfigError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
