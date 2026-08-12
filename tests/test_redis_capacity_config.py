from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "redis_capacity_config.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("redis_capacity_config", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _safe_budget(module) -> dict[str, int]:
    return {
        "redis_cgroup_limit_bytes": module.parse_memory_size("8gb"),
        "host_total_memory_bytes": module.parse_memory_size("16gb"),
        "host_available_memory_bytes": module.parse_memory_size("10gb"),
        "other_services_reserve_bytes": module.parse_memory_size("1gb"),
        "system_reserve_bytes": module.parse_memory_size("3gb"),
        "container_headroom_percent": 20,
    }


def test_capacity_plan_generates_persistent_command_and_config() -> None:
    module = _load_script()

    plan = module.build_capacity_plan(
        maxmemory_bytes=module.parse_memory_size("6gb"),
        used_memory_bytes=module.parse_memory_size("4gb"),
        dataset_bytes=module.parse_memory_size("3.5gb"),
        headroom_percent=20,
        **_safe_budget(module),
    )

    assert plan.policy == "noeviction"
    assert plan.required_container_bytes == module.parse_memory_size("7.2gb")
    assert plan.required_host_total_bytes == module.parse_memory_size("11.2gb")
    assert plan.required_host_available_bytes == module.parse_memory_size("7.2gb")
    assert module.render_docker_args(plan) == (
        "--maxmemory",
        str(6 * 1024**3),
        "--maxmemory-policy",
        "noeviction",
    )
    assert module.render_docker_memory_args(plan) == (
        "--memory",
        str(8 * 1024**3),
        "--memory-swap",
        str(8 * 1024**3),
    )
    assert module.render_redis_config(plan) == (
        f"maxmemory {6 * 1024**3}\n"
        "maxmemory-policy noeviction\n"
    )


def test_capacity_redis_argument_fragment_preserves_persistence_args() -> None:
    module = _load_script()
    plan = module.build_capacity_plan(
        maxmemory_bytes=module.parse_memory_size("6gb"),
        used_memory_bytes=module.parse_memory_size("4gb"),
        dataset_bytes=module.parse_memory_size("3.5gb"),
        headroom_percent=20,
        **_safe_budget(module),
    )
    existing = (
        "redis-server",
        "--appendonly",
        "yes",
        "--save",
        "60",
        "1",
        "--dir",
        "/data",
    )

    combined = existing + module.render_docker_args(plan)

    assert combined[: len(existing)] == existing
    assert combined[len(existing) :] == (
        "--maxmemory",
        str(6 * 1024**3),
        "--maxmemory-policy",
        "noeviction",
    )


def test_capacity_plan_rejects_limit_below_current_memory_or_headroom() -> None:
    module = _load_script()

    for maxmemory in ("3gb", "4.5gb"):
        try:
            module.build_capacity_plan(
                maxmemory_bytes=module.parse_memory_size(maxmemory),
                used_memory_bytes=module.parse_memory_size("4gb"),
                dataset_bytes=module.parse_memory_size("3.5gb"),
                headroom_percent=20,
                **_safe_budget(module),
            )
        except module.CapacitySafetyError as exc:
            assert "noeviction" in str(exc)
        else:
            raise AssertionError("unsafe maxmemory must fail validation")


def test_capacity_plan_accepts_empty_dataset_with_runtime_overhead() -> None:
    module = _load_script()

    plan = module.build_capacity_plan(
        maxmemory_bytes=module.parse_memory_size("2gb"),
        used_memory_bytes=module.parse_memory_size("1mb"),
        dataset_bytes=0,
        redis_cgroup_limit_bytes=module.parse_memory_size("2.5gb"),
        host_total_memory_bytes=module.parse_memory_size("8gb"),
        host_available_memory_bytes=module.parse_memory_size("7gb"),
        other_services_reserve_bytes=module.parse_memory_size("1gb"),
        system_reserve_bytes=module.parse_memory_size("3gb"),
        headroom_percent=20,
        container_headroom_percent=20,
    )

    assert plan.dataset_bytes == 0
    assert plan.required_host_available_bytes < plan.host_available_memory_bytes


def test_cold_backup_manifest_rejects_fake_rdb_artifact(tmp_path: Path) -> None:
    module = _load_script()
    backup_root = tmp_path / "cold-backup"
    data_root = backup_root / "data"
    data_root.mkdir(parents=True)
    rdb_path = data_root / "dump.rdb"
    rdb_path.write_text("ordinary text pretending to be Redis", encoding="utf-8")
    digest = hashlib.sha256(rdb_path.read_bytes()).hexdigest()
    validator_root = tmp_path / "validators"
    validator_root.mkdir()
    validator_output = validator_root / "redis-check-rdb-data_dump.rdb.txt"
    validator_output.write_text("RDB looks OK\n", encoding="utf-8")
    validator_digest = hashlib.sha256(validator_output.read_bytes()).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "trader-v3-redis-cold-backup/v2",
                "mode": "cold",
                "backup_root": str(backup_root),
                "source_container_id": "source-id",
                "source_redis_run_id": "source-run",
                "source_run_id": "source-run",
                "source_mount_type": "volume",
                "source_volume_name": "source-volume",
                "source_redis_dir": "/data",
                "source_rdb_filename": "dump.rdb",
                "source_save_policy": "3600 1",
                "source_aof_enabled": 0,
                "source_appendonly": "no",
                "nodes_stopped": True,
                "rdb_changes_since_last_save": 0,
                "files": [
                    {
                        "path": "data/dump.rdb",
                        "size_bytes": rdb_path.stat().st_size,
                        "sha256": digest,
                    }
                ],
                "artifacts": [
                    {
                        "kind": "rdb",
                        "path": "data/dump.rdb",
                        "sha256": digest,
                        "validator": "redis-check-rdb",
                        "validation_passed": True,
                        "validator_output_path": (
                            "validators/redis-check-rdb-data_dump.rdb.txt"
                        ),
                        "validator_output_sha256": validator_digest,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.CapacitySafetyError, match="RDB header"):
        module.verify_cold_backup_manifest(
            manifest_path,
            expected_backup_root=backup_root,
            expected_source_run_id="source-run",
            expected_source_volume="source-volume",
            expected_source_container_id="source-id",
        )


def test_eight_gib_host_rejects_six_gib_used_and_7_3_gib_limit() -> None:
    module = _load_script()

    with pytest.raises(module.CapacitySafetyError, match="host total"):
        module.build_capacity_plan(
            maxmemory_bytes=module.parse_memory_size("7.3gb"),
            used_memory_bytes=module.parse_memory_size("6gb"),
            dataset_bytes=module.parse_memory_size("5.8gb"),
            redis_cgroup_limit_bytes=module.parse_memory_size("8gb"),
            host_total_memory_bytes=module.parse_memory_size("8gb"),
            host_available_memory_bytes=module.parse_memory_size("700mb"),
            other_services_reserve_bytes=module.parse_memory_size("512mb"),
            system_reserve_bytes=module.parse_memory_size("3gb"),
            headroom_percent=20,
            container_headroom_percent=1,
        )


def test_capacity_plan_rejects_insufficient_host_available_memory() -> None:
    module = _load_script()
    budget = _safe_budget(module)
    budget["host_available_memory_bytes"] = module.parse_memory_size("7gb")

    with pytest.raises(module.CapacitySafetyError, match="host available"):
        module.build_capacity_plan(
            maxmemory_bytes=module.parse_memory_size("6gb"),
            used_memory_bytes=module.parse_memory_size("4gb"),
            dataset_bytes=module.parse_memory_size("3.5gb"),
            headroom_percent=20,
            **budget,
        )


def test_capacity_plan_rejects_system_reserve_below_safety_floor() -> None:
    module = _load_script()
    budget = _safe_budget(module)
    budget["system_reserve_bytes"] = module.parse_memory_size("512mb")

    with pytest.raises(module.CapacitySafetyError, match="at least"):
        module.build_capacity_plan(
            maxmemory_bytes=module.parse_memory_size("6gb"),
            used_memory_bytes=module.parse_memory_size("4gb"),
            dataset_bytes=module.parse_memory_size("3.5gb"),
            headroom_percent=20,
            **budget,
        )


def test_capacity_plan_rejects_unbounded_cgroup_budget() -> None:
    module = _load_script()

    with pytest.raises(module.CapacitySafetyError, match="exceeds host total"):
        module.build_capacity_plan(
            maxmemory_bytes=module.parse_memory_size("6gb"),
            used_memory_bytes=module.parse_memory_size("4gb"),
            dataset_bytes=module.parse_memory_size("3.5gb"),
            redis_cgroup_limit_bytes=module.parse_memory_size("64gb"),
            host_total_memory_bytes=module.parse_memory_size("16gb"),
            host_available_memory_bytes=module.parse_memory_size("10gb"),
            other_services_reserve_bytes=module.parse_memory_size("1gb"),
            system_reserve_bytes=module.parse_memory_size("3gb"),
            headroom_percent=20,
            container_headroom_percent=20,
        )


def test_host_and_cgroup_read_only_probes_reject_missing_or_unlimited_data(
    tmp_path: Path,
) -> None:
    module = _load_script()
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       16777216 kB\nMemAvailable:   10485760 kB\n",
        encoding="utf-8",
    )
    cgroup = tmp_path / "memory.max"
    cgroup.write_text("8589934592\n", encoding="utf-8")

    assert module.read_host_memory(meminfo) == (16 * 1024**3, 10 * 1024**3)
    assert module.read_cgroup_memory_limit((cgroup,)) == 8 * 1024**3

    cgroup.write_text("max\n", encoding="utf-8")
    with pytest.raises(module.CapacitySafetyError, match="unlimited"):
        module.read_cgroup_memory_limit((cgroup,))


def test_inspect_reads_redis_info_and_config_with_external_budgets() -> None:
    module = _load_script()

    class FakeRedis:
        def info(self, section: str):
            assert section == "memory"
            return {
                "used_memory": module.parse_memory_size("4gb"),
                "used_memory_dataset": module.parse_memory_size("3.5gb"),
            }

        def config_get(self, key: str):
            if key == "maxmemory":
                return {"maxmemory": str(module.parse_memory_size("6gb"))}
            return {"maxmemory-policy": "noeviction"}

    plan = module.inspect_running_redis(
        FakeRedis(),
        expected_maxmemory_bytes=module.parse_memory_size("6gb"),
        headroom_percent=20,
        **_safe_budget(module),
    )

    assert plan.maxmemory_bytes == module.parse_memory_size("6gb")
    assert plan.host_total_memory_bytes == module.parse_memory_size("16gb")


def test_inspect_rejects_unbounded_redis_before_capacity_math() -> None:
    module = _load_script()

    class FakeRedis:
        def info(self, section: str):
            assert section == "memory"
            return {
                "used_memory": module.parse_memory_size("4gb"),
                "used_memory_dataset": module.parse_memory_size("3.5gb"),
            }

        def config_get(self, key: str):
            if key == "maxmemory":
                return {"maxmemory": "0"}
            return {"maxmemory-policy": "noeviction"}

    with pytest.raises(module.CapacitySafetyError, match="unbounded"):
        module.inspect_running_redis(
            FakeRedis(),
            expected_maxmemory_bytes=0,
            headroom_percent=20,
            **_safe_budget(module),
        )


def test_capacity_cli_fails_closed_when_host_or_cgroup_budget_is_missing() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "generate",
            "--maxmemory",
            "6gb",
            "--current-used-memory",
            "4gb",
            "--current-dataset-size",
            "3.5gb",
            "--other-services-reserve",
            "1gb",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "cgroup budget missing" in result.stderr


def test_capacity_cli_json_contains_no_redis_url_or_credentials() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "generate",
            "--maxmemory",
            "6gb",
            "--current-used-memory",
            "4gb",
            "--current-dataset-size",
            "3.5gb",
            "--redis-cgroup-limit",
            "8gb",
            "--host-total-memory",
            "16gb",
            "--host-available-memory",
            "10gb",
            "--other-services-reserve",
            "1gb",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["maxmemory_policy"] == "noeviction"
    assert payload["docker_args"][0] == "--maxmemory"
    assert payload["redis_argument_fragment"] == payload["docker_args"]
    assert payload["docker_memory_args"] == [
        "--memory",
        str(8 * 1024**3),
        "--memory-swap",
        str(8 * 1024**3),
    ]
    assert payload["system_reserve_bytes"] == 3 * 1024**3
    assert "redis_url" not in payload
    assert "password" not in result.stdout.lower()
