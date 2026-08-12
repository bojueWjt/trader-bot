from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "hk-deploy-20260803.sh"
RELEASE_TOOL = REPO_ROOT / "scripts" / "release_manifest.py"
REDIS_SCHEMA_EPOCH = "fenced-generation-namespace/v2"
REDIS_FENCING_EPOCH = "123e4567-e89b-42d3-a456-426614174000"
REDIS_FENCING_EPOCH_KEY = "trader-bot:redis-fencing-epoch"
RUNTIME_CHECKS = {
    "active_volume_matches": True,
    "aof_disabled": True,
    "active_marker_only": True,
    "epoch_marker_persisted": True,
    "memory_limit_matches": True,
    "memory_swap_is_finite": True,
    "maxmemory_is_explicit": True,
    "maxmemory_policy_noeviction": True,
    "namespace_schema_epoch_bound": True,
    "nodes_stopped": True,
    "rdb_status_ok": True,
    "save_policy_configured": True,
    "source_container_preserved": True,
    "source_run_id_changed": True,
    "stable_namespaces_bound": True,
    "stream_retention_configured": True,
    "swap_disabled": True,
}
RUNTIME_RESOURCE_POLICY = {
    "schema_version": "trader-v3-runtime-resources/v1",
    "namespace_schema_epoch": REDIS_SCHEMA_EPOCH,
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


def _validator_source() -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    start_marker = "# REDIS_EVIDENCE_V3_VALIDATOR_BEGIN\n"
    end_marker = "\n# REDIS_EVIDENCE_V3_VALIDATOR_END"
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def _live_validator_source() -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    start_marker = "# REDIS_LIVE_STATE_VALIDATOR_BEGIN\n"
    end_marker = "\n# REDIS_LIVE_STATE_VALIDATOR_END"
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def _live_validator_command() -> list[str]:
    text = DEPLOY.read_text(encoding="utf-8")
    marker = text.index("# REDIS_LIVE_STATE_VALIDATOR_BEGIN")
    start = text.rfind("python3 - \\\n", 0, marker)
    end = text.index(" <<'PY'", start)
    command = text[start:end].replace("\\\n", " ")
    return shlex.split(command)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ceiling_percent(value: int, percent: int) -> int:
    return (value * (100 + percent) + 99) // 100


class RedisEvidenceV3GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.trusted_root = Path(self.temp_dir.name) / "redis-rebaseline"
        self.evidence_root = (
            self.trusted_root / "20260808T000000Z"
        )
        self.backup_root = self.evidence_root / "cold-backup"
        self.data_root = self.backup_root / "data"
        self.validator_root = self.evidence_root / "validators"
        self.source_data_root = (
            Path(self.temp_dir.name) / "volumes" / "source" / "_data"
        )
        self.active_volume_source = (
            Path(self.temp_dir.name) / "volumes" / "active" / "_data"
        )
        for path in (
            self.data_root,
            self.validator_root,
            self.source_data_root,
            self.active_volume_source,
        ):
            path.mkdir(parents=True, exist_ok=True)

        self.rdb = self.data_root / "dump.rdb"
        self.rdb.write_bytes(b"REDIS0011deployment-gate-fixture\n")
        self.checker_report = (
            self.validator_root / "redis-check-rdb-data_dump.rdb.txt"
        )
        self.checker_report.write_text(
            "RDB looks OK!\n",
            encoding="utf-8",
        )

        now = datetime.now(timezone.utc)
        self.source_container_id = "1" * 64
        self.active_container_id = "2" * 64
        self.source_run_id = "a" * 40
        self.active_run_id = "b" * 40
        self.backup = {
            "schema_version": "trader-v3-redis-cold-backup/v2",
            "created_at": now.isoformat(),
            "completed_at_epoch": int(now.timestamp()),
            "mode": "cold",
            "source_container": "trader-v3-redis",
            "source_container_id": self.source_container_id,
            "source_image_digest": "sha256:" + ("3" * 64),
            "source_config_image": "redis:7.2",
            "source_redis_run_id": self.source_run_id,
            "source_run_id": self.source_run_id,
            "source_key_count": 191847,
            "source_used_memory_bytes": 6 * 1024**3,
            "source_dataset_memory_bytes": 5 * 1024**3,
            "source_aof_enabled": 0,
            "source_appendonly": "no",
            "source_appendfilename": "appendonly.aof",
            "source_appenddirname": "appendonlydir",
            "source_save_policy": "3600 1",
            "source_redis_dir": "/data",
            "source_rdb_filename": "dump.rdb",
            "source_mount_type": "volume",
            "source_volume_name": "trader-v3-redis-source",
            "source_data_root": str(self.source_data_root),
            "nodes_stopped": True,
            "rdb_changes_since_last_save": 0,
            "backup_root": str(self.backup_root),
            "artifacts": [
                {
                    "kind": "rdb",
                    "path": "data/dump.rdb",
                    "sha256": _sha256(self.rdb),
                    "validator": "redis-check-rdb",
                    "validation_passed": True,
                    "validator_output_path": (
                        "validators/redis-check-rdb-data_dump.rdb.txt"
                    ),
                    "validator_output_sha256": _sha256(
                        self.checker_report
                    ),
                }
            ],
            "files": [
                {
                    "path": "data/dump.rdb",
                    "size_bytes": self.rdb.stat().st_size,
                    "sha256": _sha256(self.rdb),
                }
            ],
        }
        self.backup_path = self.evidence_root / "cold-backup-manifest.json"
        self.capacity_path = self.evidence_root / "capacity-evidence.json"
        self.risk_policy_path = self.evidence_root / "live-risk-policy.json"
        self.resource_contract_path = (
            self.evidence_root / "systemd-resource-contract.json"
        )
        self.risk_policy_path.write_text(
            json.dumps(
                {
                    "schema_version": "trader-v3-live-risk-policy/v2",
                    "entry_contract": {},
                    "legacy_migration_contract": {},
                    "runtime_resource_contract": {
                        "schema_version": (
                            "trader-v3-runtime-resources/v1"
                        ),
                        "command_journal": {
                            "max_bytes": 16 * 1024**2,
                        },
                        "redis": {
                            "critical_window_seconds": 30,
                            "memory_critical_ratio": 0.85,
                            "memory_degraded_ratio": 0.75,
                            "memory_warning_ratio": 0.6,
                            "sample_interval_seconds": 5,
                            "scan_count": 500,
                            "stream_max_bytes": 64 * 1024**2,
                            "stream_max_entries": 100_000,
                            "thread_join_timeout_seconds": 5,
                            "total_stream_max_bytes": 256 * 1024**2,
                        },
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self._write_backup()

        maxmemory = 512 * 1024**2
        used_memory = 256 * 1024**2
        dataset_memory = 128 * 1024**2
        headroom_percent = 20
        cgroup_limit = 640 * 1024**2
        container_headroom_percent = 20
        required_maxmemory = _ceiling_percent(
            used_memory,
            headroom_percent,
        )
        required_container = _ceiling_percent(
            maxmemory,
            container_headroom_percent,
        )
        host_total = 8 * 1024**3
        host_available = 7 * 1024**3
        other_services_reserve = 2304 * 1024**2
        system_reserve = 3 * 1024**3
        growth_to_maxmemory = maxmemory - used_memory
        container_overhead = required_container - maxmemory
        self.capacity = {
            "maxmemory_bytes": maxmemory,
            "used_memory_bytes": used_memory,
            "dataset_bytes": dataset_memory,
            "headroom_percent": headroom_percent,
            "required_maxmemory_bytes": required_maxmemory,
            "remaining_headroom_bytes": growth_to_maxmemory,
            "redis_cgroup_limit_bytes": cgroup_limit,
            "container_headroom_percent": container_headroom_percent,
            "required_container_bytes": required_container,
            "host_total_memory_bytes": host_total,
            "host_available_memory_bytes": host_available,
            "other_services_reserve_bytes": other_services_reserve,
            "system_reserve_bytes": system_reserve,
            "required_host_total_bytes": (
                required_container
                + other_services_reserve
                + system_reserve
            ),
            "growth_to_maxmemory_bytes": growth_to_maxmemory,
            "required_host_available_bytes": (
                growth_to_maxmemory
                + container_overhead
                + other_services_reserve
                + system_reserve
            ),
            "maxmemory_policy": "noeviction",
            "docker_args": [
                "--maxmemory",
                str(maxmemory),
                "--maxmemory-policy",
                "noeviction",
            ],
            "redis_argument_fragment": [
                "--maxmemory",
                str(maxmemory),
                "--maxmemory-policy",
                "noeviction",
            ],
            "docker_memory_args": [
                "--memory",
                str(cgroup_limit),
                "--memory-swap",
                str(cgroup_limit),
            ],
            "redis_config": (
                f"maxmemory {maxmemory}\n"
                "maxmemory-policy noeviction\n"
            ),
            "schema_version": "trader-v3-redis-capacity-evidence/v3",
            "created_at": now.isoformat(),
            "passed": True,
            "dataset_mode": "empty-volume-exchange-first-rebaseline",
            "active_container": "trader-v3-redis",
            "active_container_id": self.active_container_id,
            "active_redis_run_id": self.active_run_id,
            "initial_redis_run_id": self.active_run_id,
            "active_volume": "trader-v3-redis-hardening-20260808T000000Z",
            "active_volume_source": str(self.active_volume_source),
            "redis_fencing_epoch": REDIS_FENCING_EPOCH,
            "redis_fencing_epoch_key": REDIS_FENCING_EPOCH_KEY,
            "redis_fencing_epoch_sha256": hashlib.sha256(
                REDIS_FENCING_EPOCH.encode("ascii")
            ).hexdigest(),
            "control_keys_reinitialized": True,
            "memory_limit_bytes": cgroup_limit,
            "memory_swap_limit_bytes": cgroup_limit,
            "active_key_count": 1,
            "active_used_memory_bytes": used_memory,
            "active_dataset_memory_bytes": dataset_memory,
            "active_appendonly": "no",
            "active_aof_enabled": 0,
            "active_save_policy": "3600 1 300 100 60 10000",
            "active_rdb_last_bgsave_status": "ok",
            "source_backup_manifest": str(self.backup_path),
            "source_backup_manifest_sha256": _sha256(self.backup_path),
            "source_container_id": self.source_container_id,
            "source_redis_run_id": self.source_run_id,
            "legacy_container": (
                "trader-v3-redis-legacy-20260808T000000Z"
            ),
            "nodes_stopped": True,
            "runtime_resource_policy": json.loads(
                json.dumps(RUNTIME_RESOURCE_POLICY)
            ),
            "runtime_checks": dict(RUNTIME_CHECKS),
        }
        self._write_capacity()
        node_resource = {
            "memory_bytes": 448 * 1024**2,
            "memory_swap_bytes": 448 * 1024**2,
            "nano_cpus": 1_000_000_000,
            "nofile_hard": 65_536,
            "nofile_soft": 65_536,
            "pids_limit": 512,
            "restart_policy": "always",
        }
        redis_resource = {
            "memory_bytes": cgroup_limit,
            "memory_swap_bytes": cgroup_limit,
            "nano_cpus": 1_500_000_000,
            "nofile_hard": 65_536,
            "nofile_soft": 65_536,
            "pids_limit": 256,
            "restart_policy": "always",
        }
        bindings = [
            (
                "infra/systemd/account-stall-account-node.conf",
                "hk-gen-recreate-patched.py",
                "docker_host_config",
                [
                    "trader-v3-node-a",
                    "trader-v3-node-b",
                    "trader-v3-node-c",
                    "trader-v3-node-d",
                ],
                [
                    "docker://trader-v3-node-a/HostConfig",
                    "docker://trader-v3-node-b/HostConfig",
                    "docker://trader-v3-node-c/HostConfig",
                    "docker://trader-v3-node-d/HostConfig",
                ],
                node_resource,
            ),
            (
                "infra/systemd/account-stall-control-plane-reader.conf",
                "hk-control-plane-isolation.sh",
                "inline_service_unit",
                ["trader-v3-controlplane-operator-query.service"],
                [
                    (
                        "/etc/systemd/system/"
                        "trader-v3-controlplane-operator-query.service"
                    )
                ],
                False,
            ),
            (
                "infra/systemd/account-stall-control-plane-writer.conf",
                "hk-control-plane-isolation.sh",
                "inline_service_unit",
                [
                    "trader-v3-controlplane-node-control.service",
                    "trader-v3-controlplane-event-ingest.service",
                ],
                [
                    (
                        "/etc/systemd/system/"
                        "trader-v3-controlplane-node-control.service"
                    ),
                    (
                        "/etc/systemd/system/"
                        "trader-v3-controlplane-event-ingest.service"
                    ),
                ],
                False,
            ),
            (
                "infra/systemd/account-stall-redis.conf",
                "hk-redis-rebaseline.sh",
                "docker_host_config",
                ["trader-v3-redis"],
                ["docker://trader-v3-redis/HostConfig"],
                redis_resource,
            ),
        ]
        resources = []
        for (
            artifact,
            consumer,
            application,
            owners,
            destinations,
            host_config,
        ) in bindings:
            artifact_path = self.evidence_root / artifact
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text(
                f"# fixture {artifact}\n",
                encoding="utf-8",
            )
            resource = {
                "application": application,
                "artifact": artifact,
                "consumer_entrypoint": consumer,
                "destinations": destinations,
                "owner_units": owners,
                "sha256": _sha256(artifact_path),
            }
            if host_config is not False:
                resource["effective_docker_host_config"] = host_config
            resources.append(resource)
        self.resource_contract = {
            "schema_version": "trader-v3-systemd-resource-contract/v1",
            "resources": resources,
        }
        self._write_resource_contract()

    def _write_backup(self) -> None:
        self.backup_path.write_text(
            json.dumps(self.backup, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _write_capacity(self, *, refresh_backup_hash: bool = False) -> None:
        if refresh_backup_hash:
            self.capacity["source_backup_manifest_sha256"] = _sha256(
                self.backup_path
            )
        self.capacity_path.write_text(
            json.dumps(self.capacity, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _write_resource_contract(self) -> None:
        self.resource_contract_path.write_text(
            json.dumps(
                self.resource_contract,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _run_validator(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3",
                "-c",
                _validator_source(),
                str(self.backup_path),
                str(self.capacity_path),
                str(self.trusted_root),
                REDIS_SCHEMA_EPOCH,
                str(self.risk_policy_path),
                str(RELEASE_TOOL),
                str(self.resource_contract_path),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def _live_validator_arguments(self) -> list[str]:
        inspected = [
            {
                "Id": self.active_container_id,
                "Mounts": [
                    {
                        "Destination": "/data",
                        "Name": self.capacity["active_volume"],
                        "RW": True,
                        "Source": str(self.active_volume_source),
                        "Type": "volume",
                    }
                ],
            }
        ]
        return [
            str(self.backup_path),
            str(self.capacity_path),
            self.active_run_id,
            "1",
            REDIS_FENCING_EPOCH,
            str(self.capacity["maxmemory_bytes"]),
            "noeviction",
            "no",
            str(self.capacity["active_save_policy"]),
            "0",
            "ok",
            str(self.capacity["active_used_memory_bytes"]),
            str(self.capacity["active_dataset_memory_bytes"]),
            str(self.capacity["memory_limit_bytes"]),
            str(self.capacity["memory_swap_limit_bytes"]),
            str(self.capacity["host_total_memory_bytes"]),
            str(self.capacity["host_available_memory_bytes"]),
            json.dumps(inspected),
            str(RELEASE_TOOL),
            str(self.resource_contract_path),
        ]

    def _run_live_validator(
        self,
        arguments: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        validator_arguments = arguments
        if validator_arguments is None:
            validator_arguments = self._live_validator_arguments()
        return subprocess.run(
            ["python3", "-c", _live_validator_source(), *validator_arguments],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_generated_v3_evidence_passes(self) -> None:
        result = self._run_validator()

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_capacity_evidence_must_match_release_redis_host_config(
        self,
    ) -> None:
        mutations = {
            "cgroup": (
                "redis_cgroup_limit_bytes",
                "Redis capacity cgroup differs from release resource contract",
            ),
            "memory": (
                "memory_limit_bytes",
                (
                    "Redis capacity memory limit differs from release "
                    "resource contract"
                ),
            ),
            "memory swap": (
                "memory_swap_limit_bytes",
                (
                    "Redis capacity memory swap differs from release "
                    "resource contract"
                ),
            ),
        }
        original = json.loads(json.dumps(self.capacity))
        for name, (field, message) in mutations.items():
            with self.subTest(name=name):
                self.capacity = json.loads(json.dumps(original))
                self.capacity[field] += 1024
                self._write_capacity()

                result = self._run_validator()

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        self.capacity = original
        self._write_capacity()

    def test_legacy_application_schema_epoch_is_rejected(self) -> None:
        result = subprocess.run(
            [
                "python3",
                "-c",
                _validator_source(),
                str(self.backup_path),
                str(self.capacity_path),
                str(self.trusted_root),
                "stable-account-namespace/v1",
                str(self.risk_policy_path),
                str(RELEASE_TOOL),
                str(self.resource_contract_path),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Redis application schema epoch mismatch",
            result.stderr,
        )

    def test_legacy_capacity_evidence_is_rejected(self) -> None:
        for document in ("backup", "capacity"):
            with self.subTest(document=document):
                original_backup = self.backup["schema_version"]
                original_capacity = self.capacity["schema_version"]
                if document == "backup":
                    self.backup["schema_version"] = (
                        "trader-v3-redis-cold-backup/v1"
                    )
                    self._write_backup()
                    self._write_capacity(refresh_backup_hash=True)
                else:
                    self.capacity["schema_version"] = (
                        "trader-v3-redis-capacity-evidence/v2"
                    )
                    self._write_capacity()

                result = self._run_validator()

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("schema mismatch", result.stderr)
                self.backup["schema_version"] = original_backup
                self.capacity["schema_version"] = original_capacity
                self._write_backup()
                self._write_capacity(refresh_backup_hash=True)

    def test_critical_field_tampering_is_rejected(self) -> None:
        mutations = {
            "runtime check": lambda: self.capacity["runtime_checks"].update(
                {"source_container_preserved": False}
            ),
            "source identity": lambda: self.capacity.update(
                {"source_container_id": "4" * 64}
            ),
            "active volume source": lambda: self.capacity.update(
                {"active_volume_source": str(self.source_data_root)}
            ),
            "memory cgroup": lambda: self.capacity.update(
                {
                    "memory_limit_bytes": (
                        self.capacity["memory_limit_bytes"] - 1
                    )
                }
            ),
            "epoch hash": lambda: self.capacity.update(
                {"redis_fencing_epoch_sha256": "f" * 64}
            ),
            "epoch marker key": lambda: self.capacity.update(
                {"redis_fencing_epoch_key": "unexpected:epoch"}
            ),
            "control key reset": lambda: self.capacity.update(
                {"control_keys_reinitialized": False}
            ),
            "runtime resource policy": lambda: self.capacity[
                "runtime_resource_policy"
            ]["stream_retention"].update(
                {"stream_max_entries": 99_999}
            ),
        }
        original = json.loads(json.dumps(self.capacity))
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.capacity = json.loads(json.dumps(original))
                mutate()
                self._write_capacity()

                result = self._run_validator()

                self.assertNotEqual(result.returncode, 0)
        self.capacity = original
        self._write_capacity()

    def test_non_uuid4_epoch_is_rejected(self) -> None:
        self.capacity["redis_fencing_epoch"] = (
            "123e4567-e89b-12d3-a456-426614174000"
        )
        self.capacity["redis_fencing_epoch_sha256"] = hashlib.sha256(
            self.capacity["redis_fencing_epoch"].encode("ascii")
        ).hexdigest()
        self._write_capacity()

        result = self._run_validator()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Redis fencing epoch is invalid", result.stderr)

    def test_deploy_binds_live_marker_and_rollout_registration(self) -> None:
        text = DEPLOY.read_text(encoding="utf-8")

        self.assertIn(
            f'REDIS_FENCING_EPOCH_KEY="{REDIS_FENCING_EPOCH_KEY}"',
            text,
        )
        self.assertIn("CURRENT_REDIS_FENCING_EPOCH", text)
        self.assertIn("running Redis fencing epoch differs from evidence", text)
        registration_start = text.index(
            "ensure_bootstrap_rollout_registration()"
        )
        registration_end = text.index(
            "\nwrite_bootstrap_recovery_blocked_evidence()",
            registration_start,
        )
        registration_command = text[registration_start:registration_end]
        self.assertIn(
            '--capacity-evidence "$REDIS_CAPACITY_EVIDENCE"',
            registration_command,
        )
        self.assertIn(
            '--idempotency-key "bootstrap-register:$RELEASE_ID"',
            registration_command,
        )
        self.assertIn(
            "BOOTSTRAP_REGISTRATION_COMPLETED=1",
            registration_command,
        )
        dispatch_start = text.index(
            'if [ "$EMERGENCY_ROLLBACK" = "1" ]; then',
            text.index("# ---------- database schema ----------"),
        )
        dispatch_end = text.index("\n# ---------- install ----------", dispatch_start)
        registration_dispatch = text[dispatch_start:dispatch_end]
        self.assertIn(
            'if [[ "$DEPLOY_GATE_MODE" =~ ^bootstrap(_resume)?_stopped$ ]]; then',
            registration_dispatch,
        )
        self.assertIn(
            "ensure_bootstrap_rollout_registration",
            registration_dispatch,
        )
        self.assertIn(
            "run_reviewed_rollout register",
            registration_dispatch,
        )
        self.assertIn(
            '--idempotency-key "register:$RELEASE_ID"',
            registration_dispatch,
        )

    def test_bootstrap_resume_rejects_redis_epoch_or_evidence_hash_drift(
        self,
    ) -> None:
        text = DEPLOY.read_text(encoding="utf-8")
        gate_start = text.index("configure_deploy_gate_mode()")
        gate_end = text.index("\nverify_bootstrap_stopped_gate()", gate_start)
        gate_source = text[gate_start:gate_end]

        self.assertIn(
            "SELECT redis_fencing_epoch::text,\n"
            "                           capacity_evidence_sha256",
            gate_source,
        )
        self.assertIn(
            "hashlib.sha256(\n"
            "                        capacity_evidence_path.read_bytes()\n"
            "                    ).hexdigest()",
            gate_source,
        )
        self.assertIn(
            "if active_epochs != [expected_epoch]:",
            gate_source,
        )
        self.assertIn(
            "bootstrap resume Redis epoch evidence differs",
            gate_source,
        )
        self.assertIn(
            'print("bootstrap_resume_stopped")',
            gate_source,
        )

    def test_bootstrap_resume_rejects_rollout_manifest_hash_drift(
        self,
    ) -> None:
        text = DEPLOY.read_text(encoding="utf-8")
        gate_start = text.index("configure_deploy_gate_mode()")
        gate_end = text.index("\nverify_bootstrap_stopped_gate()", gate_start)
        gate_source = text[gate_start:gate_end]

        self.assertIn(
            "hashlib.sha256(manifest_bytes).hexdigest()",
            gate_source,
        )
        self.assertIn(
            "hashlib.sha256(bundle_bytes).hexdigest()",
            gate_source,
        )
        self.assertIn(
            'f"bootstrap-register:{manifest.get(\'release_id\')}"',
            gate_source,
        )
        self.assertIn(
            "if tuple(rollout) != expected:",
            gate_source,
        )
        self.assertIn(
            "bootstrap resume manifest differs from active rollout",
            gate_source,
        )

    def test_live_validator_argument_contract_is_ordered_and_executable(
        self,
    ) -> None:
        expected_command = [
            "python3",
            "-",
            "$REDIS_COLD_BACKUP_MANIFEST",
            "$REDIS_CAPACITY_EVIDENCE",
            "$CURRENT_REDIS_RUN_ID",
            "$CURRENT_REDIS_DBSIZE",
            "$CURRENT_REDIS_FENCING_EPOCH",
            "$CURRENT_REDIS_MAXMEMORY",
            "$CURRENT_REDIS_POLICY",
            "$CURRENT_REDIS_APPENDONLY",
            "$CURRENT_REDIS_SAVE_POLICY",
            "$CURRENT_REDIS_AOF_ENABLED",
            "$CURRENT_REDIS_RDB_STATUS",
            "$CURRENT_REDIS_USED_MEMORY",
            "$CURRENT_REDIS_DATASET_MEMORY",
            "$CURRENT_REDIS_MEMORY",
            "$CURRENT_REDIS_MEMORY_SWAP",
            "$HOST_TOTAL_BYTES",
            "$HOST_AVAILABLE_BYTES",
            "$CURRENT_REDIS_INSPECT",
            "$RELEASE_TOOL",
            "$SYSTEMD_RESOURCE_CONTRACT",
        ]
        self.assertEqual(_live_validator_command(), expected_command)

        result = self._run_live_validator()

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_live_validator_rejects_epoch_marker_argument_drift(self) -> None:
        arguments = self._live_validator_arguments()
        arguments[4] = "1"

        result = self._run_live_validator(arguments)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "running Redis fencing epoch differs from evidence",
            result.stderr,
        )

    def test_live_validator_rejects_release_resource_drift(self) -> None:
        redis_resource = next(
            item
            for item in self.resource_contract["resources"]
            if item["artifact"]
            == "infra/systemd/account-stall-redis.conf"
        )
        host_config = redis_resource["effective_docker_host_config"]
        host_config["memory_bytes"] += 1024
        host_config["memory_swap_bytes"] += 1024
        self._write_resource_contract()

        result = self._run_live_validator()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "running Redis memory differs from release resource contract",
            result.stderr,
        )

    def test_backup_artifact_and_checker_report_tampering_is_rejected(
        self,
    ) -> None:
        original_rdb = self.rdb.read_bytes()
        original_report = self.checker_report.read_bytes()
        for name, path, payload in (
            ("artifact", self.rdb, b"tampered-rdb\n"),
            ("checker report", self.checker_report, b"tampered-report\n"),
        ):
            with self.subTest(name=name):
                path.write_bytes(payload)

                result = self._run_validator()

                self.assertNotEqual(result.returncode, 0)
                self.rdb.write_bytes(original_rdb)
                self.checker_report.write_bytes(original_report)


if __name__ == "__main__":
    unittest.main()
