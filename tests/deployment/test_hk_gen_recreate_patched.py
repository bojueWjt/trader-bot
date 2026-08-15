import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "hk-gen-recreate-patched.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release_manifest

PATCH_FILES = (
    "intent_execution_planner.py",
    "contracts.py",
    "control_plane.py",
    "http_client.py",
    "projection_actor.py",
    "projection_spool.py",
    "event_mapper.py",
    "intent_execution_strategy.py",
    "exchange_cancel_adapter.py",
    "lifecycle.py",
    "binance_adapter_config.py",
    "node.py",
    "run_node.py",
    "health_server.py",
    "nautilus_actors.py",
    "approved_intent_client.py",
    "health.py",
    "bounded_task_worker.py",
    "control_plane_session.py",
    "intent_execution_inbox.py",
    "redis_safety.py",
    "reconciliation.py",
    "live_canary_execution.py",
    "node_config.py",
    "risk_config.py",
    "risk_init.py",
    "nautilus_config.py",
    "persistence_init.py",
    "redis_namespace_lease.py",
    "redis_resp_client.py",
    "routing_init.py",
    "routing_multi_account.py",
    "binance_execution.py",
    "binance_futures_execution.py",
)
BINANCE_DST = (
    "/usr/local/lib/python3.12/site-packages/"
    "nautilus_trader/adapters/binance/execution.py"
)
BINANCE_FUTURES_DST = (
    "/usr/local/lib/python3.12/site-packages/"
    "nautilus_trader/adapters/binance/futures/execution.py"
)
PATCH_TARGETS = {
    "intent_execution_planner.py": "/app/strategy/intent_execution_planner.py",
    "contracts.py": "/app/execution_domain/contracts.py",
    "control_plane.py": "/app/execution_domain/control_plane.py",
    "http_client.py": "/app/execution_domain/http_client.py",
    "projection_actor.py": "/app/projection/actor.py",
    "projection_spool.py": "/app/projection/spool.py",
    "event_mapper.py": "/app/projection/event_mapper.py",
    "intent_execution_strategy.py": "/app/strategy/intent_execution_strategy.py",
    "exchange_cancel_adapter.py": "/app/runtime/exchange_cancel_adapter.py",
    "lifecycle.py": "/app/runtime/lifecycle.py",
    "health.py": "/app/runtime/health.py",
    "bounded_task_worker.py": "/app/runtime/bounded_task_worker.py",
    "control_plane_session.py": "/app/runtime/control_plane_session.py",
    "intent_execution_inbox.py": "/app/runtime/intent_execution_inbox.py",
    "redis_safety.py": "/app/runtime/redis_safety.py",
    "reconciliation.py": "/app/runtime/reconciliation.py",
    "live_canary_execution.py": "/app/runtime/live_canary_execution.py",
    "node_config.py": "/app/config/node_config.py",
    "risk_config.py": "/app/risk/config.py",
    "risk_init.py": "/app/risk/__init__.py",
    "binance_adapter_config.py": "/app/runtime/binance_adapter_config.py",
    "node.py": "/app/app/node.py",
    "run_node.py": "/app/app/run_node.py",
    "health_server.py": "/app/app/health_server.py",
    "nautilus_actors.py": "/app/app/nautilus_actors.py",
    "approved_intent_client.py": "/app/data_client/approved_intent_client.py",
    "nautilus_config.py": "/app/persistence/nautilus_config.py",
    "persistence_init.py": "/app/persistence/__init__.py",
    "redis_namespace_lease.py": "/app/persistence/redis_namespace_lease.py",
    "redis_resp_client.py": "/app/persistence/redis_resp_client.py",
    "routing_init.py": "/app/routing/__init__.py",
    "routing_multi_account.py": "/app/routing/multi_account.py",
    "binance_execution.py": BINANCE_DST,
    "binance_futures_execution.py": BINANCE_FUTURES_DST,
}
DATABASE_SCHEMA_EPOCH = "0014_cancel_order_contract"


class GenRecreatePatchedTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp_path = Path(self.temp_dir.name)
        self.trader_root = self.temp_path / "trader-v3"
        self.patch_dir = self.trader_root / "container-patches"
        self.patch_dir.mkdir(parents=True)
        for filename in PATCH_FILES:
            (self.patch_dir / filename).write_text(filename, encoding="utf-8")

        self.fake_bin = self.temp_path / "bin"
        self.fake_bin.mkdir()
        self.docker_called = self.temp_path / "docker-called"
        self.image_labels_path = self.temp_path / "image-labels.json"
        self.image_labels_path.write_text("{}\n", encoding="utf-8")
        fake_docker = self.fake_bin / "docker"
        fake_docker.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "inspect" ]; then\n'
            '  touch "$FAKE_DOCKER_CALLED"\n'
            '  cat "$FAKE_DOCKER_INSPECT"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then\n'
            '  cat "$FAKE_DOCKER_IMAGE_LABELS"\n'
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)

        self.inspect_path = self.temp_path / "inspect.json"
        inspect_payload = [
            {
                "Id": "f" * 64,
                "Image": "sha256:" + ("1" * 64),
                "State": {
                    "Running": False,
                },
                "Config": {
                    "Image": "trader-node:test",
                    "Hostname": "node-a-runtime",
                    "User": "10001:10001",
                    "WorkingDir": "/app",
                    "Labels": {
                        "com.example.owner": "trading",
                        release_manifest.LABEL_RELEASE_ID: "stale-release",
                    },
                    "Env": [
                        "ACCOUNT_ID=account_a",
                        "BINANCE_API_SECRET=do-not-write-this-secret",
                        "NAUTILUS_INITIAL_TRADING_STATE=ACTIVE",
                        "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON={\"BTCUSDT-PERP.BINANCE\":\"999\"}",
                        "NAUTILUS_MAX_ORDER_SUBMIT_RATE=999/00:00:01",
                        "NAUTILUS_MAX_ORDER_MODIFY_RATE=999/00:00:01",
                        "PATH=/usr/bin",
                    ],
                    "Cmd": ["python", "-m", "app.run_node"],
                    "Entrypoint": None,
                },
                "NetworkSettings": {
                    "Networks": {
                        "trader-v3": {
                            "Aliases": [
                                "trader-v3-node-a",
                                "account-a-writer",
                            ]
                        },
                        "metrics": {
                            "Aliases": [
                                "node-a-metrics",
                            ]
                        },
                    }
                },
                "HostConfig": {
                    "RestartPolicy": {
                        "Name": "on-failure",
                        "MaximumRetryCount": 7,
                    },
                    "PortBindings": {
                        "8080/tcp": [
                            {
                                "HostIp": "127.0.0.1",
                                "HostPort": "8081",
                            }
                        ]
                    },
                    "Ulimits": [
                        {
                            "Name": "nofile",
                            "Soft": 65536,
                            "Hard": 65536,
                        }
                    ],
                    "SecurityOpt": ["no-new-privileges"],
                    "CapAdd": ["NET_ADMIN"],
                    "CapDrop": ["MKNOD"],
                    "Devices": [],
                    "ReadonlyRootfs": True,
                    "PidMode": "",
                    "IpcMode": "private",
                    "Memory": 536870912,
                    "MemorySwap": 536870912,
                    "NanoCpus": 500000000,
                    "CpuShares": 512,
                    "LogConfig": {
                        "Type": "json-file",
                        "Config": {
                            "max-file": "3",
                            "max-size": "10m",
                        },
                    },
                },
                "Mounts": [
                    {
                        "Source": str(self.patch_dir / "projection_actor.py"),
                        "Destination": "/app/wrong/actor.py",
                        "RW": False,
                    },
                    {
                        "Source": "/legacy/projection_actor.py",
                        "Destination": "/app/projection/actor.py",
                        "RW": False,
                    },
                    {
                        "Source": "/legacy/lifecycle.py.fixed",
                        "Destination": "/app/runtime/lifecycle.py",
                        "RW": False,
                    },
                    {
                        "Source": "/legacy/nautilus_actors.py.fixed",
                        "Destination": "/app/app/nautilus_actors.py",
                        "RW": False,
                    },
                    {
                        "Source": "/legacy/binance_adapter_config.py.fixed",
                        "Destination": "/app/runtime/binance_adapter_config.py",
                        "RW": False,
                    },
                    {
                        "Source": "/legacy/app",
                        "Destination": "/app",
                        "RW": False,
                    },
                    {
                        "Source": "/legacy/helpers",
                        "Destination": "/app/common",
                        "RW": False,
                    },
                    {
                        "Source": str(self.trader_root / "config" / "node-a.json"),
                        "Destination": "/cfg.json",
                        "RW": False,
                    },
                ],
            }
        ]
        self.inspect_path.write_text(json.dumps(inspect_payload), encoding="utf-8")

        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.fake_bin}:{self.env['PATH']}"
        self.env["TRADER_ROOT"] = str(self.trader_root)
        self.env["FAKE_DOCKER_CALLED"] = str(self.docker_called)
        self.env["FAKE_DOCKER_INSPECT"] = str(self.inspect_path)
        self.env["FAKE_DOCKER_IMAGE_LABELS"] = str(
            self.image_labels_path
        )

    def run_script(self, *args):
        return subprocess.run(
            ["python3", str(SCRIPT), *args],
            cwd=REPO_ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def generated_recreate_path(self, container_name="trader-v3-node-a"):
        return self.trader_root / f"recreate-{container_name}.sh"

    def run_tokens_from_path(self, recreate):
        lines = recreate.read_text(encoding="utf-8").splitlines()
        start = lines.index("run=(") + 1
        end = lines.index(")", start)
        return shlex.split(" ".join(lines[start:end]))

    def generated_run_tokens(self, container_name="trader-v3-node-a"):
        recreate = self.generated_recreate_path(container_name)
        return self.run_tokens_from_path(recreate)

    def generated_mounts(self, container_name="trader-v3-node-a"):
        tokens = self.generated_run_tokens(container_name)
        mounts = []
        for index, token in enumerate(tokens):
            if token == "-v":
                mounts.append(tokens[index + 1])
        return mounts

    def generated_environment(self, container_name="trader-v3-node-a"):
        recreate = self.generated_recreate_path(container_name)
        return recreate.read_text(encoding="utf-8")

    def release_identity_args(self, manifest_path):
        return (
            "--release-manifest",
            str(manifest_path),
            "--database-schema-epoch",
            DATABASE_SCHEMA_EPOCH,
        )

    def write_release_manifest(
        self,
        image_digest=None,
        delivery_mode=release_manifest.DELIVERY_TRANSITION,
        node_configs=None,
    ):
        if image_digest is None:
            image_digest = "sha256:" + ("1" * 64)
        bundle = {
            "repo_commit": "2" * 40,
            "repo_dirty": False,
            "files": [
                {
                    "bundle_path": filename,
                    "mount_target": PATCH_TARGETS[filename],
                    "sha256": release_manifest.sha256_file(
                        self.patch_dir / filename
                    ),
                }
                for filename in PATCH_FILES
            ],
        }
        manifest_options = {
            "image_digest": image_digest,
            "config_sha256": (
                "3" * 64 if node_configs is None else None
            ),
            "dependency_lock_sha256": "4" * 64,
            "patch_root": self.patch_dir,
            "delivery_mode": delivery_mode,
            "node_configs": node_configs,
            "emergency_rollback": (
                delivery_mode == release_manifest.DELIVERY_TRANSITION
            ),
        }
        if node_configs is None:
            manifest = release_manifest.build_release_manifest(
                bundle,
                **manifest_options,
            )
        else:
            envelope = self.write_strict_release_envelope(
                bundle,
                image_digest=image_digest,
            )
            manifest_options["dependency_lock_sha256"] = envelope[
                "dependency_lock_sha256"
            ]
            strict_options = {
                "dependency_inventory_sha256": envelope[
                    "dependency_inventory_sha256"
                ],
                "migration_manifest_sha256": envelope[
                    "migration_manifest_sha256"
                ],
                "release_payload": envelope["release_payload"],
                "sha256sums_sha256": envelope["sha256sums_sha256"],
                "release_source_manifest_sha256": envelope[
                    "release_source_manifest_sha256"
                ],
                "systemd_resource_contract_sha256": envelope[
                    "systemd_resource_contract_sha256"
                ],
                "build_attestation_sha256": envelope[
                    "build_attestation_sha256"
                ],
            }
            provisional = release_manifest.build_release_manifest(
                bundle,
                _allow_unreviewed_subject=True,
                **manifest_options,
                **strict_options,
            )
            reviewer_path = (
                self.temp_path
                / release_manifest.REVIEWER_TRUST_PROOF_NAME
            )
            reviewer_document = {
                "schema_version": (
                    release_manifest.REVIEWER_TRUST_PROOF_SCHEMA_VERSION
                ),
                "reviewer": "fixture-reviewer",
                "decision": "approved",
                "source_commit": bundle["repo_commit"],
                "review_subject_sha256": provisional[
                    "review_subject_sha256"
                ],
                "build_attestation_sha256": strict_options[
                    "build_attestation_sha256"
                ],
            }
            reviewer_path.write_text(
                json.dumps(
                    reviewer_document,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            reviewer_sha256 = release_manifest.sha256_file(reviewer_path)
            self.env["TRADER_RELEASE_REVIEWER_TRUST_SHA256"] = (
                reviewer_sha256
            )
            reviewer_trust_proof = {
                "path": release_manifest.REVIEWER_TRUST_PROOF_NAME,
                "sha256": reviewer_sha256,
                "reviewer": "fixture-reviewer",
                "decision": "approved",
                "review_subject_sha256": provisional[
                    "review_subject_sha256"
                ],
            }
            manifest = release_manifest.build_release_manifest(
                bundle,
                reviewer_trust_proof=reviewer_trust_proof,
                **manifest_options,
                **strict_options,
            )
        path = self.temp_path / "release-manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path, manifest

    def write_strict_release_envelope(self, bundle, *, image_digest):
        bundle_path = self.temp_path / "bundle-manifest.json"
        bundle_path.write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for filename in PATCH_FILES:
            source = self.patch_dir / filename
            (self.temp_path / filename).write_bytes(source.read_bytes())

        lock_path = (
            self.temp_path / release_manifest.RELEASE_DEPENDENCY_LOCK_NAME
        )
        lock_path.write_text(
            """
version = 1
revision = 1
requires-python = "==3.12.*"

[[package]]
name = "nautilus-node-runtime"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "runtime-demo" },
]

[[package]]
name = "runtime-demo"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://example.invalid/runtime-demo.tar.gz", hash = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", size = 1 }
""".strip()
            + "\n",
            encoding="utf-8",
        )
        inventory_path = (
            self.temp_path / release_manifest.DEPENDENCY_INVENTORY_NAME
        )
        release_manifest.write_dependency_inventory(
            lock_path,
            inventory_path,
        )

        release_files = [
            lock_path.relative_to(self.temp_path).as_posix(),
            inventory_path.relative_to(self.temp_path).as_posix(),
        ]
        runner_relative = release_manifest.MIGRATION_RUNNER_PATH
        runner_path = self.temp_path / runner_relative
        runner_path.parent.mkdir(parents=True, exist_ok=True)
        runner_path.write_text("# fixture migration runner\n", encoding="utf-8")
        release_files.append(runner_relative)

        migration_entries = []
        for relative in release_manifest.CANONICAL_MIGRATION_PATHS:
            migration_path = self.temp_path / relative
            migration_path.parent.mkdir(parents=True, exist_ok=True)
            migration_path.write_text(f"-- fixture {relative}\n", encoding="utf-8")
            migration_entries.append(
                {
                    "path": relative,
                    "sha256": release_manifest.sha256_file(migration_path),
                }
            )
            release_files.append(relative)
        migration_manifest_path = (
            self.temp_path / release_manifest.MIGRATION_MANIFEST_NAME
        )
        migration_manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": (
                        release_manifest.MIGRATION_MANIFEST_SCHEMA_VERSION
                    ),
                    "schema_epoch": release_manifest.SCHEMA_EPOCHS["db"],
                    "runner": {
                        "path": runner_relative,
                        "sha256": release_manifest.sha256_file(runner_path),
                    },
                    "up": release_manifest.MIGRATION_UP_PATH,
                    "down": release_manifest.MIGRATION_DOWN_PATH,
                    "prerequisites": list(
                        release_manifest.MIGRATION_PREREQUISITE_PATHS
                    ),
                    "steps": [
                        dict(item)
                        for item in release_manifest.CANONICAL_MIGRATION_STEPS
                    ],
                    "migrations": migration_entries,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        resource_specs = (
            (
                release_manifest.NODE_DOCKER_RESOURCE_ARTIFACT,
                "docker_host_config",
            ),
            (
                release_manifest.REDIS_DOCKER_RESOURCE_ARTIFACT,
                "docker_host_config",
            ),
            (
                "infra/systemd/account-stall-control-plane-reader.conf",
                "systemd_service_drop_in",
            ),
            (
                "infra/systemd/account-stall-control-plane-writer.conf",
                "systemd_service_drop_in",
            ),
        )
        docker_resources = {
            "memory_bytes": 536_870_912,
            "memory_swap_bytes": 536_870_912,
            "nano_cpus": 500_000_000,
            "pids_limit": 256,
            "nofile_soft": 65_536,
            "nofile_hard": 65_536,
            "restart_policy": "on-failure",
        }
        resources = []
        for relative, application in resource_specs:
            resource_path = self.temp_path / relative
            resource_path.parent.mkdir(parents=True, exist_ok=True)
            resource_path.write_text(
                f"# fixture resource {relative}\n",
                encoding="utf-8",
            )
            item = {
                "artifact": relative,
                "sha256": release_manifest.sha256_file(resource_path),
                "application": application,
            }
            binding = release_manifest.SYSTEMD_RESOURCE_BINDINGS[relative]
            item["consumer_entrypoint"] = binding["consumer_entrypoint"]
            item["owner_units"] = list(binding["owner_units"])
            item["destinations"] = list(binding["destinations"])
            if application == "docker_host_config":
                item["effective_docker_host_config"] = dict(
                    docker_resources
                )
            resources.append(item)
            release_files.append(relative)
        systemd_path = (
            self.temp_path
            / release_manifest.SYSTEMD_RESOURCE_CONTRACT_NAME
        )
        systemd_path.write_text(
            json.dumps(
                {
                    "schema_version": (
                        release_manifest
                        .SYSTEMD_RESOURCE_CONTRACT_SCHEMA_VERSION
                    ),
                    "resources": resources,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        watcher_manifest_path = (
            self.temp_path
            / release_manifest.WATCHER_RUNTIME_MANIFEST_NAME
        )
        watcher_manifest_path.write_text("{}\n", encoding="utf-8")
        source_path = (
            self.temp_path / release_manifest.RELEASE_SOURCE_MANIFEST_NAME
        )
        source_path.write_text(
            json.dumps(
                {
                    "schema_version": "trader-v3-release-source/v1",
                    "source_mode": "git_object",
                    "source_commit": bundle["repo_commit"],
                    "source_tree": "d" * 40,
                    "schema_epochs": dict(release_manifest.SCHEMA_EPOCHS),
                    "bundle_manifest_sha256": (
                        release_manifest.sha256_file(bundle_path)
                    ),
                    "migration": {
                        "runner": runner_relative,
                        "up": release_manifest.MIGRATION_UP_PATH,
                        "down": release_manifest.MIGRATION_DOWN_PATH,
                        "prerequisites": list(
                            release_manifest.MIGRATION_PREREQUISITE_PATHS
                        ),
                        "steps": [
                            dict(item)
                            for item in (
                                release_manifest.CANONICAL_MIGRATION_STEPS
                            )
                        ],
                        "migration_files": list(
                            release_manifest.CANONICAL_MIGRATION_PATHS
                        ),
                        "db_schema_epoch": (
                            release_manifest.SCHEMA_EPOCHS["db"]
                        ),
                        "python_dependencies": ["psycopg2"],
                        "manifest_sha256": (
                            release_manifest.sha256_file(
                                migration_manifest_path
                            )
                        ),
                    },
                    "systemd_resource_contract": {
                        "path": (
                            release_manifest
                            .SYSTEMD_RESOURCE_CONTRACT_NAME
                        ),
                        "sha256": release_manifest.sha256_file(
                            systemd_path
                        ),
                    },
                    "watcher_runtime": {
                        "manifest": (
                            release_manifest
                            .WATCHER_RUNTIME_MANIFEST_NAME
                        ),
                        "manifest_sha256": (
                            release_manifest.sha256_file(
                                watcher_manifest_path
                            )
                        ),
                    },
                    "files": [
                        {
                            "release_path": relative,
                            "sha256": release_manifest.sha256_file(
                                self.temp_path / relative
                            ),
                        }
                        for relative in release_files
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        source_manifest = release_manifest.validate_release_source_manifest(
            source_path,
            payload_root=self.temp_path,
        )
        release_payload = release_manifest._expected_release_payload(
            payload_root=self.temp_path,
            source_manifest=source_manifest,
        )
        checksum_path = self.temp_path / release_manifest.SHA256SUMS_NAME
        checksum_path.write_text(
            "".join(
                f"{item['sha256']}  {item['path']}\n"
                for item in release_payload
            ),
            encoding="utf-8",
        )
        envelope = release_manifest.validate_release_payload_envelope(
            payload_root=self.temp_path
        )

        attestation_inputs = {
            "base_image_digest": "sha256:" + ("e" * 64),
            "dockerfile_sha256": "f" * 64,
            "bundle_manifest_sha256": source_manifest[
                "bundle_manifest_sha256"
            ],
            "release_source_manifest_sha256": envelope[
                "release_source_manifest_sha256"
            ],
            "dependency_lock_sha256": envelope[
                "dependency_lock_sha256"
            ],
            "dependency_inventory_sha256": envelope[
                "dependency_inventory_sha256"
            ],
            "migration_manifest_sha256": envelope[
                "migration_manifest_sha256"
            ],
        }
        image_labels = release_manifest.build_attestation_labels(
            attestation_inputs
        )
        attestation_path = (
            self.temp_path / release_manifest.BUILD_ATTESTATION_NAME
        )
        attestation_path.write_text(
            json.dumps(
                {
                    "schema_version": (
                        release_manifest.BUILD_ATTESTATION_SCHEMA_VERSION
                    ),
                    "generated_at": "2026-08-12T00:00:00+00:00",
                    "base_image_digest": attestation_inputs[
                        "base_image_digest"
                    ],
                    "dockerfile_sha256": attestation_inputs[
                        "dockerfile_sha256"
                    ],
                    "bundle_manifest": {
                        "path": "bundle-manifest.json",
                        "sha256": attestation_inputs[
                            "bundle_manifest_sha256"
                        ],
                    },
                    "release_source_manifest": {
                        "path": (
                            release_manifest.RELEASE_SOURCE_MANIFEST_NAME
                        ),
                        "sha256": attestation_inputs[
                            "release_source_manifest_sha256"
                        ],
                    },
                    "dependency_lock": {
                        "path": release_manifest.RELEASE_DEPENDENCY_LOCK_NAME,
                        "sha256": attestation_inputs[
                            "dependency_lock_sha256"
                        ],
                    },
                    "dependency_inventory": {
                        "path": release_manifest.DEPENDENCY_INVENTORY_NAME,
                        "sha256": attestation_inputs[
                            "dependency_inventory_sha256"
                        ],
                    },
                    "migration_manifest": {
                        "path": release_manifest.MIGRATION_MANIFEST_NAME,
                        "sha256": attestation_inputs[
                            "migration_manifest_sha256"
                        ],
                    },
                    "build_subject_sha256": (
                        release_manifest.build_attestation_subject_sha256(
                            attestation_inputs
                        )
                    ),
                    "image_digest": image_digest,
                    "image_labels": image_labels,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.image_labels_path.write_text(
            json.dumps(image_labels) + "\n",
            encoding="utf-8",
        )
        envelope["build_attestation_sha256"] = (
            release_manifest.sha256_file(attestation_path)
        )
        return envelope

    def write_node_config_artifacts(self):
        specs = {}
        paths = {}
        artifact_root = self.temp_path / "config-artifacts"
        for suffix in ("a", "b", "c", "d"):
            account_id = f"account-{suffix}"
            container = f"trader-v3-node-{suffix}"
            config = {
                "account_id": account_id,
                "node_id": f"nautilus-node-{account_id}",
                "trader_id": f"trader-{account_id}",
                "instance_id": f"instance-{account_id}",
                "redis": {
                    "key_prefix": f"nautilus:{account_id}:node-{suffix}"
                },
                "runtime_resources": {
                    "schema_version": (
                        release_manifest.RUNTIME_RESOURCES_SCHEMA_VERSION
                    ),
                    "redis": {
                        "stream_max_entries": 1000,
                        "stream_max_bytes": 1_048_576,
                        "total_stream_max_bytes": 8_388_608,
                        "scan_count": 100,
                        "sample_interval_seconds": 1.0,
                        "critical_window_seconds": 30.0,
                        "thread_join_timeout_seconds": 5.0,
                        "memory_warning_ratio": 0.6,
                        "memory_degraded_ratio": 0.75,
                        "memory_critical_ratio": 0.85,
                    },
                    "command_journal": {
                        "max_bytes": 1_048_576,
                    },
                    "control_plane_session": dict(
                        release_manifest.CONTROL_PLANE_SESSION_RESOURCE_DEFAULTS
                    ),
                    "strategy_durable_io": dict(
                        release_manifest.STRATEGY_DURABLE_IO_RESOURCE_DEFAULTS
                    ),
                    "terminal_exchange": dict(
                        release_manifest.TERMINAL_EXCHANGE_RESOURCE_DEFAULTS
                    ),
                    "reporter_workers": dict(
                        release_manifest.REPORTER_WORKER_RESOURCE_DEFAULTS
                    ),
                },
            }
            payload = (
                json.dumps(config, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            digest = release_manifest.hashlib.sha256(payload).hexdigest()
            account_root = artifact_root / account_id
            account_root.mkdir(parents=True)
            path = account_root / f"{digest}.json"
            path.write_bytes(payload)
            path.chmod(0o440)
            specs[account_id] = (container, path)
            paths[account_id] = path
        return release_manifest.build_node_config_artifacts(specs), paths

    def write_v3_release_manifest(
        self,
        delivery_mode=release_manifest.DELIVERY_IMMUTABLE,
    ):
        node_configs, paths = self.write_node_config_artifacts()
        manifest_path, manifest = self.write_release_manifest(
            "sha256:" + ("9" * 64),
            delivery_mode=delivery_mode,
            node_configs=node_configs,
        )
        return manifest_path, manifest, paths

    def test_missing_binance_destination_fails_before_docker_inspect(self):
        result = self.run_script("trader-v3-node-a")

        self.assertEqual(result.returncode, 2)
        self.assertIn("transition mode requires", result.stderr)
        self.assertFalse(self.docker_called.exists())
        self.assertFalse(
            (self.trader_root / "recreate-trader-v3-node-a.sh").exists()
        )

    def test_generated_mounts_use_each_canonical_source_destination_pair_once(self):
        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = self.generated_mounts()
        expected = {
            f"{self.patch_dir}/{filename}:{destination}:ro"
            for filename, destination in PATCH_TARGETS.items()
        }
        actual_patch_mounts = {
            mount
            for mount in mounts
            if mount.startswith(f"{self.patch_dir}/")
        }
        self.assertEqual(actual_patch_mounts, expected)
        for mount in expected:
            self.assertEqual(mounts.count(mount), 1)
        self.assertNotIn(
            f"{self.patch_dir}/projection_actor.py:/app/wrong/actor.py:ro",
            mounts,
        )
        self.assertNotIn(
            "/legacy/projection_actor.py:/app/projection/actor.py:ro",
            mounts,
        )
        self.assertNotIn(
            "/legacy/lifecycle.py.fixed:/app/runtime/lifecycle.py:ro",
            mounts,
        )
        self.assertNotIn(
            "/legacy/nautilus_actors.py.fixed:/app/app/nautilus_actors.py:ro",
            mounts,
        )
        self.assertNotIn(
            "/legacy/binance_adapter_config.py.fixed:"
            "/app/runtime/binance_adapter_config.py:ro",
            mounts,
        )

    def test_missing_lifecycle_patch_fails_before_docker_inspect(self):
        (self.patch_dir / "lifecycle.py").unlink()

        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("mount source is missing", result.stderr)
        self.assertIn("lifecycle.py", result.stderr)
        self.assertFalse(self.docker_called.exists())

    def test_generated_container_always_starts_halted(self):
        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        environment = self.generated_environment()
        self.assertEqual(environment.count("NAUTILUS_INITIAL_TRADING_STATE=HALTED"), 1)
        self.assertNotIn("NAUTILUS_INITIAL_TRADING_STATE=ACTIVE", environment)
        self.assertIn(
            "NAUTILUS_MAX_NOTIONAL_PER_ORDER_JSON|",
            environment,
        )
        self.assertIn("NAUTILUS_MAX_ORDER_SUBMIT_RATE|", environment)
        self.assertIn("NAUTILUS_MAX_ORDER_MODIFY_RATE|", environment)
        self.assertNotIn("999/00:00:01", environment)
        self.assertNotIn('BTCUSDT-PERP.BINANCE\\":\\"999', environment)

    def test_generated_container_preserves_allowlisted_runtime_spec(self):
        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        tokens = self.generated_run_tokens()
        expected_pairs = {
            "--user": "10001:10001",
            "--workdir": "/app",
            "--hostname": "node-a-runtime",
            "--publish": "127.0.0.1:8081:8080/tcp",
            "--ulimit": "nofile=65536:65536",
            "--security-opt": "no-new-privileges",
            "--cap-add": "NET_ADMIN",
            "--cap-drop": "MKNOD",
            "--ipc": "private",
            "--memory": "536870912",
            "--memory-swap": "536870912",
            "--cpus": "0.5",
            "--cpu-shares": "512",
            "--log-driver": "json-file",
        }
        for option, value in expected_pairs.items():
            index = tokens.index(option)
            self.assertEqual(tokens[index + 1], value)
        self.assertIn("--read-only", tokens)
        self.assertIn("--restart=on-failure:7", tokens)
        self.assertIn("--network=trader-v3", tokens)
        alias_index = tokens.index("--network-alias")
        self.assertEqual(tokens[alias_index + 1], "account-a-writer")
        self.assertIn("com.example.owner=trading", tokens)
        self.assertNotIn(
            f"{release_manifest.LABEL_RELEASE_ID}=stale-release",
            tokens,
        )
        text = self.generated_environment()
        self.assertIn(
            "docker network connect --alias node-a-metrics metrics "
            "trader-v3-node-a",
            text,
        )
        self.assertIn('container_id="$("${run[@]}")"', text)

    def test_snapshot_runtime_preserves_live_contract_without_secret_text(self):
        inspect_payload = json.loads(
            self.inspect_path.read_text(encoding="utf-8")
        )
        inspected = inspect_payload[0]
        inspected["Config"]["Env"] = [
            "ACCOUNT_ID=account-c",
            "BINANCE_ACCOUNT_C_API_SECRET=snapshot-secret",
            "NAUTILUS_INITIAL_TRADING_STATE=HALTED",
            "PATH=/usr/bin",
        ]
        inspected["Mounts"] = [
            {
                "Type": "bind",
                "Source": "/srv/trader-v3/legacy-release/node.py",
                "Destination": "/app/app/node.py",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
            {
                "Type": "bind",
                "Source": "/srv/trader-v3/secrets/subaccounts/api-key",
                "Destination": "/run/secrets/binance_account_c_api_key",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
            {
                "Type": "bind",
                "Source": (
                    "/srv/trader-v3/legacy-release/"
                    "binance_execution.py"
                ),
                "Destination": BINANCE_DST,
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
            {
                "Type": "bind",
                "Source": (
                    "/srv/trader-v3/legacy-release/"
                    "binance_futures_execution.py"
                ),
                "Destination": BINANCE_FUTURES_DST,
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
        ]
        self.inspect_path.write_text(
            json.dumps(inspect_payload),
            encoding="utf-8",
        )
        snapshot_root = self.temp_path / "snapshot"
        snapshot_root.mkdir()
        recreate = snapshot_root / "recreate.sh"
        environment = snapshot_root / "container-env.json"
        evidence = snapshot_root / "evidence.json"

        result = self.run_script(
            "trader-v3-node-c",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            "--snapshot-runtime",
            "--output",
            str(recreate),
            "--environment-output",
            str(environment),
            "--evidence-output",
            str(evidence),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(recreate.stat().st_mode & 0o777, 0o700)
        self.assertEqual(environment.stat().st_mode & 0o777, 0o400)
        self.assertEqual(evidence.stat().st_mode & 0o777, 0o400)
        text = recreate.read_text(encoding="utf-8")
        self.assertNotIn("snapshot-secret", text)
        self.assertNotIn("snapshot-secret", result.stdout)
        self.assertIn(str(environment), text)
        self.assertIn(
            "BINANCE_ACCOUNT_C_API_SECRET=snapshot-secret",
            environment.read_text(encoding="utf-8").splitlines(),
        )
        self.assertIn("--env-file", self.run_tokens_from_path(recreate))
        self.assertNotIn('run+=("-e" "$env_value")', text)
        tokens = self.run_tokens_from_path(recreate)
        self.assertIn(f"run+=({inspected['Image']})", text)
        mounts = [
            tokens[index + 1]
            for index, token in enumerate(tokens)
            if token == "-v"
        ]
        self.assertEqual(
            set(mounts),
            {
                (
                    f"{mount['Source']}:{mount['Destination']}:"
                    f"{mount['Mode']}"
                )
                for mount in inspected["Mounts"]
            },
        )
        self.assertNotIn(str(self.patch_dir), text)
        evidence_payload = json.loads(evidence.read_text(encoding="utf-8"))
        self.assertEqual(
            evidence_payload["runtime_contract"]["image_digest"],
            inspected["Image"],
        )
        self.assertEqual(
            evidence_payload["runtime_contract"]["restart_policy"],
            inspected["HostConfig"]["RestartPolicy"],
        )

        verify = self.run_script(
            "trader-v3-node-c",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            "--verify-snapshot-evidence",
            str(evidence),
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)

    def test_snapshot_runtime_verification_rejects_live_mount_drift(self):
        inspect_payload = json.loads(
            self.inspect_path.read_text(encoding="utf-8")
        )
        inspected = inspect_payload[0]
        inspected["Config"]["Env"] = [
            "NAUTILUS_INITIAL_TRADING_STATE=HALTED",
        ]
        inspected["Mounts"] = [
            {
                "Type": "bind",
                "Source": "/legacy/binance_execution.py",
                "Destination": BINANCE_DST,
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
            {
                "Type": "bind",
                "Source": "/legacy/binance_futures_execution.py",
                "Destination": BINANCE_FUTURES_DST,
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
            {
                "Type": "bind",
                "Source": "/legacy/node.py",
                "Destination": "/app/app/node.py",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
        ]
        self.inspect_path.write_text(
            json.dumps(inspect_payload),
            encoding="utf-8",
        )
        snapshot_root = self.temp_path / "snapshot-drift"
        snapshot_root.mkdir()
        recreate = snapshot_root / "recreate.sh"
        environment = snapshot_root / "container-env.json"
        evidence = snapshot_root / "evidence.json"
        generated = self.run_script(
            "trader-v3-node-c",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            "--snapshot-runtime",
            "--output",
            str(recreate),
            "--environment-output",
            str(environment),
            "--evidence-output",
            str(evidence),
        )
        self.assertEqual(generated.returncode, 0, generated.stderr)

        inspect_payload[0]["Mounts"][2]["Source"] = "/drifted/node.py"
        self.inspect_path.write_text(
            json.dumps(inspect_payload),
            encoding="utf-8",
        )
        verify = self.run_script(
            "trader-v3-node-c",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            "--verify-snapshot-evidence",
            str(evidence),
        )

        self.assertEqual(verify.returncode, 2)
        self.assertIn("runtime contract differs", verify.stderr)

    def test_snapshot_runtime_rejects_non_private_mount_propagation(self):
        inspect_payload = json.loads(
            self.inspect_path.read_text(encoding="utf-8")
        )
        inspected = inspect_payload[0]
        inspected["Config"]["Env"] = [
            "NAUTILUS_INITIAL_TRADING_STATE=HALTED",
        ]
        inspected["Mounts"] = [
            {
                "Type": "bind",
                "Source": "/legacy/binance_execution.py",
                "Destination": BINANCE_DST,
                "Mode": "ro",
                "RW": False,
                "Propagation": "rshared",
            },
            {
                "Type": "bind",
                "Source": "/legacy/binance_futures_execution.py",
                "Destination": BINANCE_FUTURES_DST,
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
        ]
        self.inspect_path.write_text(
            json.dumps(inspect_payload),
            encoding="utf-8",
        )
        snapshot_root = self.temp_path / "snapshot-propagation"
        snapshot_root.mkdir()

        result = self.run_script(
            "trader-v3-node-c",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            "--snapshot-runtime",
            "--output",
            str(snapshot_root / "recreate.sh"),
            "--environment-output",
            str(snapshot_root / "container.env"),
            "--evidence-output",
            str(snapshot_root / "evidence.json"),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("mount propagation is unsupported", result.stderr)

    def test_snapshot_runtime_rejects_static_network_endpoint(self):
        inspect_payload = json.loads(
            self.inspect_path.read_text(encoding="utf-8")
        )
        inspect_payload[0]["Config"]["Env"] = [
            "NAUTILUS_INITIAL_TRADING_STATE=HALTED",
        ]
        inspect_payload[0]["NetworkSettings"]["Networks"]["trader-v3"][
            "IPAMConfig"
        ] = {
            "IPv4Address": "172.20.0.9",
        }
        self.inspect_path.write_text(
            json.dumps(inspect_payload),
            encoding="utf-8",
        )
        snapshot_root = self.temp_path / "snapshot-static-ip"
        snapshot_root.mkdir()

        result = self.run_script(
            "trader-v3-node-c",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            "--snapshot-runtime",
            "--output",
            str(snapshot_root / "recreate.sh"),
            "--environment-output",
            str(snapshot_root / "container.env"),
            "--evidence-output",
            str(snapshot_root / "evidence.json"),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("network endpoint option is unsupported", result.stderr)

    def test_snapshot_runtime_rejects_endpoint_mac_address(self):
        inspect_payload = json.loads(
            self.inspect_path.read_text(encoding="utf-8")
        )
        inspect_payload[0]["Config"]["Env"] = [
            "NAUTILUS_INITIAL_TRADING_STATE=HALTED",
        ]
        inspect_payload[0]["NetworkSettings"]["Networks"]["trader-v3"][
            "MacAddress"
        ] = "02:42:ac:14:00:09"
        self.inspect_path.write_text(
            json.dumps(inspect_payload),
            encoding="utf-8",
        )
        snapshot_root = self.temp_path / "snapshot-endpoint-mac"
        snapshot_root.mkdir()

        result = self.run_script(
            "trader-v3-node-c",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            "--snapshot-runtime",
            "--output",
            str(snapshot_root / "recreate.sh"),
            "--environment-output",
            str(snapshot_root / "container.env"),
            "--evidence-output",
            str(snapshot_root / "evidence.json"),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "trader-v3.MacAddress",
            result.stderr,
        )

    def test_snapshot_runtime_rejects_unreplayed_host_config(self):
        inspect_payload = json.loads(
            self.inspect_path.read_text(encoding="utf-8")
        )
        inspect_payload[0]["Config"]["Env"] = [
            "NAUTILUS_INITIAL_TRADING_STATE=HALTED",
        ]
        inspect_payload[0]["HostConfig"]["MemoryReservation"] = 268435456
        self.inspect_path.write_text(
            json.dumps(inspect_payload),
            encoding="utf-8",
        )
        snapshot_root = self.temp_path / "snapshot-host-config"
        snapshot_root.mkdir()

        result = self.run_script(
            "trader-v3-node-c",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            "--snapshot-runtime",
            "--output",
            str(snapshot_root / "recreate.sh"),
            "--environment-output",
            str(snapshot_root / "container.env"),
            "--evidence-output",
            str(snapshot_root / "evidence.json"),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "HostConfig.MemoryReservation is unsupported",
            result.stderr,
        )

    def test_snapshot_runtime_rejects_explicit_mac_address(self):
        inspect_payload = json.loads(
            self.inspect_path.read_text(encoding="utf-8")
        )
        inspect_payload[0]["Config"]["Env"] = [
            "NAUTILUS_INITIAL_TRADING_STATE=HALTED",
        ]
        inspect_payload[0]["Config"]["MacAddress"] = "02:42:ac:14:00:09"
        self.inspect_path.write_text(
            json.dumps(inspect_payload),
            encoding="utf-8",
        )
        snapshot_root = self.temp_path / "snapshot-mac-address"
        snapshot_root.mkdir()

        result = self.run_script(
            "trader-v3-node-c",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            "--snapshot-runtime",
            "--output",
            str(snapshot_root / "recreate.sh"),
            "--environment-output",
            str(snapshot_root / "container.env"),
            "--evidence-output",
            str(snapshot_root / "evidence.json"),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "snapshot Config.MacAddress is unsupported",
            result.stderr,
        )

    def test_snapshot_runtime_replays_unlimited_memory_swap(self):
        inspect_payload = json.loads(
            self.inspect_path.read_text(encoding="utf-8")
        )
        inspected = inspect_payload[0]
        inspected["Config"]["Env"] = [
            "NAUTILUS_INITIAL_TRADING_STATE=HALTED",
        ]
        inspected["HostConfig"]["MemorySwap"] = -1
        inspected["Mounts"] = [
            {
                "Type": "bind",
                "Source": "/legacy/binance_execution.py",
                "Destination": BINANCE_DST,
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
            {
                "Type": "bind",
                "Source": "/legacy/binance_futures_execution.py",
                "Destination": BINANCE_FUTURES_DST,
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
        ]
        self.inspect_path.write_text(
            json.dumps(inspect_payload),
            encoding="utf-8",
        )
        snapshot_root = self.temp_path / "snapshot-unlimited-swap"
        snapshot_root.mkdir()
        recreate = snapshot_root / "recreate.sh"

        result = self.run_script(
            "trader-v3-node-c",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            "--snapshot-runtime",
            "--output",
            str(recreate),
            "--environment-output",
            str(snapshot_root / "container.env"),
            "--evidence-output",
            str(snapshot_root / "evidence.json"),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        tokens = self.run_tokens_from_path(recreate)
        index = tokens.index("--memory-swap")
        self.assertEqual(tokens[index + 1], "-1")

    def test_snapshot_runtime_requires_halted_live_container(self):
        snapshot_root = self.temp_path / "snapshot-active"
        snapshot_root.mkdir()

        result = self.run_script(
            "trader-v3-node-c",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            "--snapshot-runtime",
            "--output",
            str(snapshot_root / "recreate.sh"),
            "--environment-output",
            str(snapshot_root / "container-env.json"),
            "--evidence-output",
            str(snapshot_root / "evidence.json"),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("must already be HALTED", result.stderr)

    def test_dynamic_published_port_is_rejected(self):
        inspect_payload = json.loads(
            self.inspect_path.read_text(encoding="utf-8")
        )
        bindings = inspect_payload[0]["HostConfig"]["PortBindings"]
        bindings["8080/tcp"][0]["HostPort"] = ""
        self.inspect_path.write_text(
            json.dumps(inspect_payload),
            encoding="utf-8",
        )

        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("fixed HostPort", result.stderr)

    def test_reviewed_release_restores_missing_account_health_binding(self):
        inspect_payload = json.loads(
            self.inspect_path.read_text(encoding="utf-8")
        )
        inspected = inspect_payload[0]
        inspected["HostConfig"]["PortBindings"] = {}
        inspected["Config"]["Env"].append("NAUTILUS_HEALTH_PORT=8081")
        inspected["Config"]["Env"].append(
            "NAUTILUS_HEALTH_HOST=127.0.0.1"
        )
        self.inspect_path.write_text(
            json.dumps(inspect_payload),
            encoding="utf-8",
        )
        manifest_path, _, _ = self.write_v3_release_manifest()

        result = self.run_script(
            "trader-v3-node-a",
            *self.release_identity_args(manifest_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        tokens = self.generated_run_tokens()
        publish_index = tokens.index("--publish")
        self.assertEqual(
            tokens[publish_index + 1],
            "127.0.0.1:8081:8081/tcp",
        )
        text = self.generated_environment()
        self.assertIn(
            "TRADER_RELEASE_PURPOSE|NAUTILUS_HEALTH_HOST)",
            text,
        )
        self.assertEqual(
            text.count('run+=("-e" "NAUTILUS_HEALTH_HOST=0.0.0.0")'),
            1,
        )
        self.assertNotIn(
            "NAUTILUS_HEALTH_HOST=127.0.0.1",
            text,
        )

    def test_reviewed_release_rejects_missing_health_port_environment(self):
        inspect_payload = json.loads(
            self.inspect_path.read_text(encoding="utf-8")
        )
        inspect_payload[0]["HostConfig"]["PortBindings"] = {}
        self.inspect_path.write_text(
            json.dumps(inspect_payload),
            encoding="utf-8",
        )
        manifest_path, _, _ = self.write_v3_release_manifest()

        result = self.run_script(
            "trader-v3-node-a",
            *self.release_identity_args(manifest_path),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "must define one NAUTILUS_HEALTH_PORT",
            result.stderr,
        )

    def test_binance_destination_cannot_replace_another_patch(self):
        result = self.run_script(
            "trader-v3-node-a",
            "/app/projection/actor.py",
            BINANCE_FUTURES_DST,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("FATAL: duplicate mount destination", result.stderr)
        self.assertFalse(self.docker_called.exists())

    def test_release_manifest_pins_digest_labels_and_avoids_secret_output(self):
        manifest_path, manifest = self.write_release_manifest()

        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            *self.release_identity_args(manifest_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        recreate = self.trader_root / "recreate-trader-v3-node-a.sh"
        text = recreate.read_text(encoding="utf-8")
        self.assertIn(manifest["image_digest"], text)
        self.assertIn(manifest["release_id"], text)
        self.assertIn(release_manifest.LABEL_RELEASE_COMMIT, text)
        self.assertIn(release_manifest.LABEL_RELEASE_CONFIG, text)
        self.assertIn(release_manifest.LABEL_RELEASE_DELIVERY, text)
        self.assertIn("old node process is still alive", text)
        self.assertIn('kill -0 "$old_pid"', text)
        self.assertIn("TRADER_RELEASE_IMAGE_DIGEST", text)
        self.assertIn("TRADER_RELEASE_CONFIG_SHA256", text)
        self.assertIn("TRADER_RELEASE_DEPENDENCY_LOCK_SHA256", text)
        self.assertIn("TRADER_RELEASE_SCHEMA_EPOCH", text)
        self.assertIn(
            f"TRADER_RELEASE_SCHEMA_EPOCH={DATABASE_SCHEMA_EPOCH}",
            text,
        )
        self.assertIn(
            "io.trader.release.database-schema-epoch="
            f"{DATABASE_SCHEMA_EPOCH}",
            text,
        )
        self.assertNotIn(
            f"TRADER_RELEASE_SCHEMA_EPOCH={manifest['schema_version']}",
            text,
        )
        self.assertNotIn("do-not-write-this-secret", text)
        self.assertNotIn("do-not-write-this-secret", result.stdout)
        syntax = subprocess.run(
            ["bash", "-n", str(recreate)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_release_manifest_requires_database_schema_epoch(self):
        manifest_path, _ = self.write_release_manifest()

        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            "--release-manifest",
            str(manifest_path),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "release recreation requires --database-schema-epoch",
            result.stderr,
        )
        self.assertFalse(self.docker_called.exists())

    def test_release_manifest_can_select_new_immutable_image_digest(self):
        manifest_path, manifest = self.write_release_manifest(
            "sha256:" + ("9" * 64)
        )

        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
            *self.release_identity_args(manifest_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        recreate = self.trader_root / "recreate-trader-v3-node-a.sh"
        text = recreate.read_text(encoding="utf-8")
        self.assertIn(f"run+=({manifest['image_digest']})", text)

    def test_immutable_mode_removes_all_inherited_python_code_mounts(self):
        manifest_path, manifest, _paths = self.write_v3_release_manifest(
            delivery_mode=release_manifest.DELIVERY_IMMUTABLE,
        )

        result = self.run_script(
            "trader-v3-node-a",
            *self.release_identity_args(manifest_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = self.generated_mounts()
        selected = next(
            item
            for item in manifest["node_configs"]
            if item["account_id"] == "account-a"
        )
        self.assertIn(
            f"{selected['host_path']}:/cfg.json:ro",
            mounts,
        )
        self.assertIn(
            f"{self.trader_root}/node-state/a:/state:rw",
            mounts,
        )
        code_targets = set(PATCH_TARGETS.values())
        for mount in mounts:
            destination = mount.rsplit(":", 1)[0].split(":", 1)[1]
            normalized_destination = destination.rstrip("/")
            covered_targets = {
                target
                for target in code_targets
                if target == normalized_destination
                or target.startswith(f"{normalized_destination}/")
            }
            self.assertFalse(
                covered_targets,
                (
                    "immutable recreate retained a bind mount covering "
                    f"business code: {mount} -> {sorted(covered_targets)}"
                ),
            )
        recreate = self.trader_root / "recreate-trader-v3-node-a.sh"
        text = recreate.read_text(encoding="utf-8")
        self.assertIn(f"run+=({manifest['image_digest']})", text)
        self.assertNotIn(str(self.patch_dir), text)
        self.assertNotIn("/legacy/helpers:/app/common:ro", mounts)

    def test_v3_account_a_replaces_legacy_config_with_target_artifact(self):
        manifest_path, manifest, paths = self.write_v3_release_manifest()

        result = self.run_script(
            "trader-v3-node-a",
            *self.release_identity_args(manifest_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = self.generated_mounts()
        selected = next(
            item
            for item in manifest["node_configs"]
            if item["account_id"] == "account-a"
        )
        self.assertIn(f"{selected['host_path']}:/cfg.json:ro", mounts)
        self.assertNotIn(
            f"{self.trader_root}/config/node-a.json:/cfg.json:ro",
            mounts,
        )
        self.assertNotIn(f"{paths['account-b']}:/cfg.json:ro", mounts)
        self.assertEqual(selected["container"], "trader-v3-node-a")

    def test_v3_account_b_replaces_legacy_config_with_target_artifact(self):
        manifest_path, manifest, paths = self.write_v3_release_manifest()

        result = self.run_script(
            "trader-v3-node-b",
            *self.release_identity_args(manifest_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = self.generated_mounts("trader-v3-node-b")
        selected = next(
            item
            for item in manifest["node_configs"]
            if item["account_id"] == "account-b"
        )
        self.assertIn(f"{selected['host_path']}:/cfg.json:ro", mounts)
        self.assertNotIn(
            f"{self.trader_root}/config/node-a.json:/cfg.json:ro",
            mounts,
        )
        self.assertNotIn(f"{paths['account-a']}:/cfg.json:ro", mounts)
        self.assertEqual(selected["container"], "trader-v3-node-b")

    def test_v3_target_generation_does_not_read_peer_artifact(self):
        manifest_path, manifest, paths = self.write_v3_release_manifest()
        peer_path = paths["account-b"]
        peer_path.unlink()

        result = self.run_script(
            "trader-v3-node-a",
            *self.release_identity_args(manifest_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(peer_path.exists())
        mounts = self.generated_mounts()
        selected = next(
            item
            for item in manifest["node_configs"]
            if item["account_id"] == "account-a"
        )
        self.assertIn(f"{selected['host_path']}:/cfg.json:ro", mounts)

    def test_v3_target_generation_rejects_missing_config_artifact(self):
        manifest_path, _, paths = self.write_v3_release_manifest()
        paths["account-a"].unlink()

        result = self.run_script(
            "trader-v3-node-a",
            *self.release_identity_args(manifest_path),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("release config artifact is missing", result.stderr)
        self.assertFalse(self.docker_called.exists())

    def test_v3_target_generation_rejects_writable_config_artifact(self):
        manifest_path, _, paths = self.write_v3_release_manifest()
        paths["account-a"].chmod(0o640)

        result = self.run_script(
            "trader-v3-node-a",
            *self.release_identity_args(manifest_path),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("release config artifact must be read-only", result.stderr)
        self.assertFalse(self.docker_called.exists())

    def test_v3_target_generation_rejects_config_hash_drift(self):
        manifest_path, _, paths = self.write_v3_release_manifest()
        target_path = paths["account-a"]
        target_path.chmod(0o640)
        target_path.write_text('{"account_id":"account-a"}\n', encoding="utf-8")
        target_path.chmod(0o440)

        result = self.run_script(
            "trader-v3-node-a",
            *self.release_identity_args(manifest_path),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("release config artifact hash mismatch", result.stderr)
        self.assertFalse(self.docker_called.exists())

    def test_manifest_missing_hardening_module_fails_exact_set_gate(self):
        for missing in (
            "reconciliation.py",
            "risk_init.py",
            "redis_namespace_lease.py",
            "redis_resp_client.py",
        ):
            with self.subTest(missing=missing):
                manifest_path, manifest = self.write_release_manifest()
                manifest["files"] = [
                    item
                    for item in manifest["files"]
                    if item["bundle_path"] != missing
                ]
                manifest["release_id"] = release_manifest.calculate_release_id(
                    manifest
                )
                manifest_path.write_text(
                    json.dumps(manifest),
                    encoding="utf-8",
                )

                result = self.run_script(
                    "trader-v3-node-a",
                    BINANCE_DST,
                    BINANCE_FUTURES_DST,
                    *self.release_identity_args(manifest_path),
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn(
                    "release manifest and recreate delivery plan differ",
                    result.stderr,
                )
                self.assertIn(missing, result.stderr)
                self.assertFalse(self.docker_called.exists())


if __name__ == "__main__":
    unittest.main()
