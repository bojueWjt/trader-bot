import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release_manifest

IMAGE_DIGEST = "sha256:" + ("1" * 64)
CONFIG_SHA256 = "2" * 64
LOCK_SHA256 = "3" * 64
SOURCE_SHA256 = "4" * 64
RELEASE_COMMIT = "5" * 40
DEPENDENCY_INVENTORY_SHA256 = "6" * 64
MIGRATION_MANIFEST_SHA256 = "7" * 64
RELEASE_PAYLOAD_SHA256 = "8" * 64
SHA256SUMS_SHA256 = "9" * 64
RELEASE_SOURCE_MANIFEST_SHA256 = "a" * 64
SYSTEMD_RESOURCE_CONTRACT_SHA256 = "b" * 64
BUILD_ATTESTATION_SHA256 = "c" * 64
REVIEWER_TRUST_PROOF_SHA256 = "d" * 64
EXPECTED_SCHEMA_EPOCHS = {
    "app": "account-stall-hardening-runtime/v1",
    "db": "0014_cancel_order_contract",
    "redis": "fenced-generation-namespace/v2",
}
EXPECTED_TRANSITION_RUNTIME_FILES = {
    "health_server.py": "/app/app/health_server.py",
    "run_node.py": "/app/app/run_node.py",
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
    "projection_spool.py": "/app/projection/spool.py",
    "approved_intent_client.py": "/app/data_client/approved_intent_client.py",
    "nautilus_config.py": "/app/persistence/nautilus_config.py",
    "persistence_init.py": "/app/persistence/__init__.py",
    "redis_namespace_lease.py": "/app/persistence/redis_namespace_lease.py",
    "redis_resp_client.py": "/app/persistence/redis_resp_client.py",
}
LIVE_DELETED_MOUNTINFO_LINE = (
    "401 300 8:1 "
    "/srv/trader-v3/container-patches/node.py//deleted "
    "/app/app/node.py ro,relatime - ext4 /dev/vda1 rw"
)


class ReleaseManifestTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp_path = Path(self.temp_dir.name)
        self.patch_root = self.temp_path / "container-patches"
        self.patch_root.mkdir()
        self.host_source = self.patch_root / "node.py"
        self.host_source.write_bytes(b"release-source")
        self.source_sha256 = release_manifest.sha256_file(self.host_source)
        bundle = {
            "repo_commit": RELEASE_COMMIT,
            "repo_dirty": False,
            "schema_epochs": dict(EXPECTED_SCHEMA_EPOCHS),
            "files": [
                {
                    "bundle_path": "node.py",
                    "mount_target": "/app/app/node.py",
                    "sha256": self.source_sha256,
                }
            ],
        }
        self.manifest = release_manifest.build_release_manifest(
            bundle,
            image_digest=IMAGE_DIGEST,
            config_sha256=CONFIG_SHA256,
            dependency_lock_sha256=LOCK_SHA256,
            patch_root=self.patch_root,
            delivery_mode=release_manifest.DELIVERY_TRANSITION,
            emergency_rollback=True,
        )
        self.bundle = bundle
        self.node_config_specs = self.write_node_config_artifacts()
        node_configs = release_manifest.build_node_config_artifacts(
            self.node_config_specs
        )
        self.artifact_manifest = self.build_strict_manifest(
            bundle,
            node_configs,
        )

    def build_strict_manifest(self, bundle, node_configs):
        strict_fields = {
            "dependency_inventory_sha256": (
                DEPENDENCY_INVENTORY_SHA256
            ),
            "migration_manifest_sha256": MIGRATION_MANIFEST_SHA256,
            "release_payload": [
                {
                    "path": "bundle-manifest.json",
                    "sha256": RELEASE_PAYLOAD_SHA256,
                }
            ],
            "sha256sums_sha256": SHA256SUMS_SHA256,
            "release_source_manifest_sha256": (
                RELEASE_SOURCE_MANIFEST_SHA256
            ),
            "systemd_resource_contract_sha256": (
                SYSTEMD_RESOURCE_CONTRACT_SHA256
            ),
            "build_attestation_sha256": BUILD_ATTESTATION_SHA256,
        }
        provisional = release_manifest.build_release_manifest(
            bundle,
            image_digest=IMAGE_DIGEST,
            config_sha256=None,
            dependency_lock_sha256=LOCK_SHA256,
            patch_root=self.patch_root,
            node_configs=node_configs,
            _allow_unreviewed_subject=True,
            **strict_fields,
        )
        reviewer_trust_proof = {
            "path": release_manifest.REVIEWER_TRUST_PROOF_NAME,
            "sha256": REVIEWER_TRUST_PROOF_SHA256,
            "reviewer": "release-reviewer",
            "decision": "approved",
            "review_subject_sha256": provisional[
                "review_subject_sha256"
            ],
        }
        return release_manifest.build_release_manifest(
            bundle,
            image_digest=IMAGE_DIGEST,
            config_sha256=None,
            dependency_lock_sha256=LOCK_SHA256,
            patch_root=self.patch_root,
            node_configs=node_configs,
            reviewer_trust_proof=reviewer_trust_proof,
            **strict_fields,
        )

    def strict_envelope_evidence(self):
        return {
            "build_attestation": {
                "image_labels": {},
            },
            "systemd_resource_contract": {},
        }

    def write_systemd_resource_contract(self):
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
        for artifact, binding in (
            release_manifest.SYSTEMD_RESOURCE_BINDINGS.items()
        ):
            path = self.temp_path / artifact
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"# fixture resource {artifact}\n",
                encoding="utf-8",
            )
            application = "systemd_service_drop_in"
            if artifact in {
                release_manifest.NODE_DOCKER_RESOURCE_ARTIFACT,
                release_manifest.REDIS_DOCKER_RESOURCE_ARTIFACT,
            }:
                application = "docker_host_config"
            item = {
                "artifact": artifact,
                "sha256": release_manifest.sha256_file(path),
                "application": application,
                "consumer_entrypoint": binding["consumer_entrypoint"],
                "owner_units": list(binding["owner_units"]),
                "destinations": list(binding["destinations"]),
            }
            if application == "docker_host_config":
                item["effective_docker_host_config"] = dict(
                    docker_resources
                )
            resources.append(item)
        contract = (
            self.temp_path
            / release_manifest.SYSTEMD_RESOURCE_CONTRACT_NAME
        )
        contract.write_text(
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
        return contract

    def test_systemd_resource_contract_requires_four_account_node_owners(
        self,
    ):
        path = self.write_systemd_resource_contract()
        document = json.loads(path.read_text(encoding="utf-8"))
        node_resource = next(
            item
            for item in document["resources"]
            if item["artifact"] == (
                release_manifest.NODE_DOCKER_RESOURCE_ARTIFACT
            )
        )
        node_resource["owner_units"] = node_resource["owner_units"][:2]
        node_resource["destinations"] = node_resource["destinations"][:2]
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "owner units mismatch",
        ):
            release_manifest.validate_systemd_resource_contract(
                path,
                payload_root=self.temp_path,
            )

    def test_systemd_resource_contract_preserves_four_account_bindings(
        self,
    ):
        path = self.write_systemd_resource_contract()

        contract = release_manifest.validate_systemd_resource_contract(
            path,
            payload_root=self.temp_path,
        )

        node_resource = next(
            item
            for item in contract["resources"]
            if item["artifact"] == (
                release_manifest.NODE_DOCKER_RESOURCE_ARTIFACT
            )
        )
        expected = release_manifest.SYSTEMD_RESOURCE_BINDINGS[
            release_manifest.NODE_DOCKER_RESOURCE_ARTIFACT
        ]
        self.assertEqual(
            node_resource["owner_units"],
            expected["owner_units"],
        )
        self.assertEqual(
            node_resource["destinations"],
            expected["destinations"],
        )

    def write_node_config_artifacts(self):
        specs = {}
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
        return specs

    def write_dependency_lock(self):
        path = (
            self.temp_path
            / release_manifest.RELEASE_DEPENDENCY_LOCK_NAME
        )
        path.write_text(
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
        return path

    def write_migration_manifest(self):
        runner_relative = release_manifest.MIGRATION_RUNNER_PATH
        up_relative = release_manifest.MIGRATION_UP_PATH
        down_relative = release_manifest.MIGRATION_DOWN_PATH
        runner = self.temp_path / runner_relative
        runner.parent.mkdir(parents=True, exist_ok=True)
        runner.write_text("# migration runner\n", encoding="utf-8")
        migrations = []
        for relative in release_manifest.CANONICAL_MIGRATION_PATHS:
            path = self.temp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"-- {relative}\n", encoding="utf-8")
            migrations.append(
                {
                    "path": relative,
                    "sha256": release_manifest.sha256_file(path),
                }
            )
        manifest = self.temp_path / release_manifest.MIGRATION_MANIFEST_NAME
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": (
                        release_manifest.MIGRATION_MANIFEST_SCHEMA_VERSION
                    ),
                    "schema_epoch": EXPECTED_SCHEMA_EPOCHS["db"],
                    "runner": {
                        "path": runner_relative,
                        "sha256": release_manifest.sha256_file(runner),
                    },
                    "up": up_relative,
                    "down": down_relative,
                    "prerequisites": list(
                        release_manifest.MIGRATION_PREREQUISITE_PATHS
                    ),
                    "steps": [
                        dict(item)
                        for item in release_manifest.CANONICAL_MIGRATION_STEPS
                    ],
                    "migrations": migrations,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return manifest

    def inspected_node(self, name):
        labels = {
            release_manifest.LABEL_RELEASE_COMMIT: RELEASE_COMMIT,
            release_manifest.LABEL_RELEASE_CONFIG: CONFIG_SHA256,
            release_manifest.LABEL_RELEASE_DELIVERY: (
                self.manifest["delivery_mode"]
            ),
            release_manifest.LABEL_RELEASE_ID: self.manifest["release_id"],
            release_manifest.LABEL_RELEASE_IMAGE: IMAGE_DIGEST,
        }
        return {
            "Name": f"/{name}",
            "Image": IMAGE_DIGEST,
            "Config": {"Labels": labels},
            "State": {"Pid": 101},
            "Mounts": [
                {
                    "Source": str(self.host_source),
                    "Destination": "/app/app/node.py",
                }
            ],
        }

    def inspected_artifact_node(self, name):
        config_entry = next(
            item
            for item in self.artifact_manifest["node_configs"]
            if item["container"] == name
        )
        labels = {
            release_manifest.LABEL_RELEASE_COMMIT: RELEASE_COMMIT,
            release_manifest.LABEL_RELEASE_CONFIG: (
                self.artifact_manifest["config_sha256"]
            ),
            release_manifest.LABEL_RELEASE_DELIVERY: (
                self.artifact_manifest["delivery_mode"]
            ),
            release_manifest.LABEL_RELEASE_ID: (
                self.artifact_manifest["release_id"]
            ),
            release_manifest.LABEL_RELEASE_IMAGE: IMAGE_DIGEST,
        }
        return {
            "Name": f"/{name}",
            "Image": IMAGE_DIGEST,
            "Config": {"Labels": labels},
            "State": {"Pid": 101},
            "Mounts": [
                {
                    "Source": config_entry["host_path"],
                    "Destination": "/cfg.json",
                    "RW": False,
                },
            ],
        }

    def test_live_double_slash_deleted_mount_is_detected(self):
        self.assertTrue(
            release_manifest.mountinfo_has_deleted_source(
                LIVE_DELETED_MOUNTINFO_LINE
            )
        )

    def test_standard_deleted_mount_forms_are_detected(self):
        self.assertTrue(
            release_manifest.mountinfo_has_deleted_source(
                "/source/file.py (deleted) /app/file.py"
            )
        )
        self.assertTrue(
            release_manifest.mountinfo_has_deleted_source(
                "/source/file.py\\040(deleted) /app/file.py"
            )
        )

    def test_account_specific_configs_share_one_normalized_hash(self):
        config_a = (
            REPO_ROOT
            / "services/nautilus-node/config/examples/account-a.sandbox.json"
        )
        hashes = {
            release_manifest.node_config_sha256(
                REPO_ROOT
                / "services"
                / "nautilus-node"
                / "config"
                / "examples"
                / f"account-{suffix}.sandbox.json"
            )
            for suffix in ("a", "b", "c", "d")
        }

        self.assertEqual(len(hashes), 1)

    def test_v3_manifest_binds_each_immutable_node_config_artifact(self):
        self.assertEqual(
            self.artifact_manifest["schema_version"],
            release_manifest.SCHEMA_VERSION,
        )
        self.assertEqual(
            {
                item["account_id"]
                for item in self.artifact_manifest["node_configs"]
            },
            {"account-a", "account-b", "account-c", "account-d"},
        )
        raw_hashes = {
            item["sha256"]
            for item in self.artifact_manifest["node_configs"]
        }
        normalized_hashes = {
            item["normalized_sha256"]
            for item in self.artifact_manifest["node_configs"]
        }
        self.assertEqual(len(raw_hashes), 4)
        self.assertEqual(
            normalized_hashes,
            {self.artifact_manifest["config_sha256"]},
        )
        self.assertTrue(
            release_manifest.STRICT_V3_REQUIRED_FIELDS.issubset(
                self.artifact_manifest
            )
        )
        self.assertEqual(
            self.artifact_manifest["reviewer_trust_proof"][
                "review_subject_sha256"
            ],
            self.artifact_manifest["review_subject_sha256"],
        )
        for item in self.artifact_manifest["node_configs"]:
            self.assertEqual(
                Path(item["host_path"]).name,
                f"{item['sha256']}.json",
            )
            resources = item["runtime_resources"]
            self.assertEqual(
                resources["control_plane_session"][
                    "command_delivery_capacity"
                ],
                128,
            )
            self.assertEqual(
                resources["strategy_durable_io"],
                release_manifest.STRATEGY_DURABLE_IO_RESOURCE_DEFAULTS,
            )
            self.assertEqual(
                resources["terminal_exchange"],
                release_manifest.TERMINAL_EXCHANGE_RESOURCE_DEFAULTS,
            )
            self.assertEqual(
                resources["reporter_workers"],
                release_manifest.REPORTER_WORKER_RESOURCE_DEFAULTS,
            )

    def test_strict_v3_manifest_fails_closed_when_any_field_is_missing(self):
        for field in sorted(release_manifest.STRICT_V3_REQUIRED_FIELDS):
            manifest = json.loads(json.dumps(self.artifact_manifest))
            manifest.pop(field)
            manifest["release_id"] = release_manifest.calculate_release_id(
                manifest
            )

            with self.subTest(field=field), self.assertRaisesRegex(
                release_manifest.ReleaseManifestError,
                "strict release v3 fields are required",
            ):
                release_manifest.validate_release_manifest(manifest)

    def test_capture_builds_v3_manifest_from_reviewed_config_artifacts(self):
        bundle_path = self.temp_path / "bundle-manifest.json"
        bundle_path.write_text(
            json.dumps(self.bundle),
            encoding="utf-8",
        )
        lock_path = self.write_dependency_lock()
        migration_manifest = self.write_migration_manifest()
        output_path = self.temp_path / "release-manifest.json"
        attestation_path = (
            self.temp_path / release_manifest.BUILD_ATTESTATION_NAME
        )
        attestation_path.write_text("attested\n", encoding="utf-8")
        reviewer_path = (
            self.temp_path / release_manifest.REVIEWER_TRUST_PROOF_NAME
        )
        reviewer_path.write_text("approved\n", encoding="utf-8")
        containers = list(release_manifest.DEFAULT_CONTAINERS)
        inventory_sha256 = release_manifest.dependency_inventory_sha256(
            release_manifest.build_dependency_inventory(lock_path)
        )
        migration_sha256 = release_manifest.sha256_file(
            migration_manifest
        )
        envelope = {
            "release_payload": [
                {
                    "path": "bundle-manifest.json",
                    "sha256": RELEASE_PAYLOAD_SHA256,
                }
            ],
            "sha256sums_sha256": SHA256SUMS_SHA256,
            "release_source_manifest_sha256": (
                RELEASE_SOURCE_MANIFEST_SHA256
            ),
            "systemd_resource_contract_sha256": (
                SYSTEMD_RESOURCE_CONTRACT_SHA256
            ),
            "dependency_lock_sha256": (
                release_manifest.sha256_file(lock_path)
            ),
            "dependency_inventory_sha256": inventory_sha256,
            "migration_manifest_sha256": migration_sha256,
        }

        def reviewer_proof(_path, **kwargs):
            return {
                "path": release_manifest.REVIEWER_TRUST_PROOF_NAME,
                "sha256": REVIEWER_TRUST_PROOF_SHA256,
                "reviewer": "release-reviewer",
                "decision": "approved",
                "review_subject_sha256": kwargs[
                    "expected_subject_sha256"
                ],
            }

        with (
            mock.patch.object(
                release_manifest,
                "_runtime_image_identity",
                return_value=([], IMAGE_DIGEST),
            ),
            mock.patch.object(
                release_manifest,
                "validate_build_attestation",
                return_value={"image_digest": IMAGE_DIGEST},
            ),
            mock.patch.object(
                release_manifest,
                "validate_release_payload_envelope",
                return_value=envelope,
            ),
            mock.patch.object(
                release_manifest,
                "validate_reviewer_trust_proof",
                side_effect=reviewer_proof,
            ),
        ):
            manifest = release_manifest.capture_release_manifest(
                bundle_path=bundle_path,
                dependency_lock_path=lock_path,
                output_path=output_path,
                patch_root=self.patch_root,
                containers=containers,
                image_digest=IMAGE_DIGEST,
                node_config_specs=self.node_config_specs,
                migration_manifest_path=migration_manifest,
                build_attestation_path=attestation_path,
                reviewer_trust_proof_path=reviewer_path,
                reviewer_trust_sha256=REVIEWER_TRUST_PROOF_SHA256,
            )

        self.assertEqual(
            manifest["schema_version"],
            release_manifest.SCHEMA_VERSION,
        )
        self.assertEqual(len(manifest["node_configs"]), 4)
        self.assertTrue(
            release_manifest.STRICT_V3_REQUIRED_FIELDS.issubset(manifest)
        )
        self.assertEqual(
            manifest["dependency_inventory_sha256"],
            inventory_sha256,
        )
        self.assertEqual(
            manifest["migration_manifest_sha256"],
            migration_sha256,
        )
        self.assertEqual(
            manifest["reviewer_trust_proof"]["sha256"],
            REVIEWER_TRUST_PROOF_SHA256,
        )
        self.assertEqual(
            json.loads(output_path.read_text(encoding="utf-8")),
            manifest,
        )

    def test_capture_fails_closed_without_config_artifacts(self):
        bundle_path = self.temp_path / "bundle-manifest.json"
        bundle_path.write_text(
            json.dumps(self.bundle),
            encoding="utf-8",
        )
        lock_path = self.write_dependency_lock()

        with self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "requires immutable",
        ):
            release_manifest.capture_release_manifest(
                bundle_path=bundle_path,
                dependency_lock_path=lock_path,
                output_path=self.temp_path / "release-manifest.json",
                patch_root=self.patch_root,
                containers=[
                    "trader-v3-node-a",
                    "trader-v3-node-b",
                ],
            )

    def test_dependency_lock_rejects_pseudo_lock(self):
        pseudo_root = self.temp_path / "pseudo"
        pseudo_root.mkdir()
        lock_path = (
            pseudo_root / release_manifest.RELEASE_DEPENDENCY_LOCK_NAME
        )
        lock_path.write_text("version = 1\n", encoding="utf-8")

        with self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "revision",
        ):
            release_manifest.build_dependency_inventory(lock_path)

    def test_repository_lock_resolves_linux_inventory(self):
        lock_path = (
            REPO_ROOT / "infra/docker/nautilus/uv.node.lock"
        )

        inventory = release_manifest.build_dependency_inventory(
            lock_path
        )
        packages = {
            item["name"]: item["version"]
            for item in inventory["packages"]
        }

        self.assertEqual(packages["nautilus-trader"], "1.227.0")
        self.assertEqual(packages["uvloop"], "0.22.1")
        self.assertNotIn("colorama", packages)
        self.assertNotIn("nautilus-node-runtime", packages)

    def test_v3_release_id_covers_each_node_config_artifact(self):
        tampered = json.loads(json.dumps(self.artifact_manifest))
        replacement_hash = "6" * 64
        entry = tampered["node_configs"][0]
        entry["sha256"] = replacement_hash
        entry["host_path"] = str(
            Path(entry["host_path"]).parent
            / f"{replacement_hash}.json"
        )
        review_subject = release_manifest.release_review_subject_sha256(
            tampered
        )
        tampered["review_subject_sha256"] = review_subject
        tampered["reviewer_trust_proof"][
            "review_subject_sha256"
        ] = review_subject

        with self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "release_id does not match manifest contents",
        ):
            release_manifest.validate_release_manifest(tampered)

    def test_release_id_covers_dependency_and_migration_inventory(self):
        manifest = release_manifest.build_release_manifest(
            self.bundle,
            image_digest=IMAGE_DIGEST,
            config_sha256=CONFIG_SHA256,
            dependency_lock_sha256=LOCK_SHA256,
            patch_root=self.patch_root,
            delivery_mode=release_manifest.DELIVERY_TRANSITION,
            emergency_rollback=True,
            dependency_inventory_sha256="6" * 64,
            migration_manifest_sha256="7" * 64,
        )
        tampered = json.loads(json.dumps(manifest))
        tampered["migration_manifest_sha256"] = "8" * 64

        with self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "release_id does not match manifest contents",
        ):
            release_manifest.validate_release_manifest(tampered)

    def test_v3_target_only_verification_proves_config_inode_and_bytes(self):
        container = "trader-v3-node-a"
        inspected = self.inspected_artifact_node(container)
        config_entry = self.artifact_manifest["node_configs"][0]
        config_stat = Path(config_entry["host_path"]).stat()
        mount_pairs = [
            (config_entry["host_path"], "/cfg.json"),
        ]

        with (
            mock.patch.object(
                release_manifest,
                "_runtime_image_identity",
                return_value=([inspected], IMAGE_DIGEST),
            ),
            mock.patch.object(
                release_manifest,
                "validate_strict_release_envelope",
                return_value=self.strict_envelope_evidence(),
            ),
            mock.patch.object(
                release_manifest,
                "docker_resource_contract",
                return_value=None,
            ),
            mock.patch.object(
                release_manifest,
                "_read_mountinfo",
                return_value=mount_pairs,
            ),
            mock.patch.object(
                release_manifest,
                "_stat_path",
                return_value=config_stat,
            ),
            mock.patch.object(
                release_manifest,
                "_container_sha256",
                side_effect=[
                    config_entry["sha256"],
                    LOCK_SHA256,
                    DEPENDENCY_INVENTORY_SHA256,
                    MIGRATION_MANIFEST_SHA256,
                    self.source_sha256,
                ],
            ),
            mock.patch.object(
                release_manifest,
                "_container_dependency_inventory_sha256",
                return_value=DEPENDENCY_INVENTORY_SHA256,
            ),
        ):
            result = release_manifest.verify_live_release(
                json.loads(json.dumps(self.artifact_manifest)),
                [container],
                payload_root=self.temp_path,
            )

        self.assertEqual(len(result), 1)
        evidence = result[0]["config_artifact"]
        self.assertEqual(evidence["sha256"], config_entry["sha256"])
        self.assertEqual(evidence["inode"], config_stat.st_ino)

    def test_v3_target_only_verification_does_not_read_running_peer_path(self):
        container = "trader-v3-node-a"
        inspected = self.inspected_artifact_node(container)
        config_entry = self.artifact_manifest["node_configs"][0]
        peer_entry = self.artifact_manifest["node_configs"][1]
        Path(peer_entry["host_path"]).unlink()
        config_stat = Path(config_entry["host_path"]).stat()

        with (
            mock.patch.object(
                release_manifest,
                "_runtime_image_identity",
                return_value=([inspected], IMAGE_DIGEST),
            ),
            mock.patch.object(
                release_manifest,
                "validate_strict_release_envelope",
                return_value=self.strict_envelope_evidence(),
            ),
            mock.patch.object(
                release_manifest,
                "docker_resource_contract",
                return_value=None,
            ),
            mock.patch.object(
                release_manifest,
                "_read_mountinfo",
                return_value=[
                    (config_entry["host_path"], "/cfg.json"),
                ],
            ),
            mock.patch.object(
                release_manifest,
                "_stat_path",
                return_value=config_stat,
            ),
            mock.patch.object(
                release_manifest,
                "_container_sha256",
                side_effect=[
                    config_entry["sha256"],
                    LOCK_SHA256,
                    DEPENDENCY_INVENTORY_SHA256,
                    MIGRATION_MANIFEST_SHA256,
                    self.source_sha256,
                ],
            ),
            mock.patch.object(
                release_manifest,
                "_container_dependency_inventory_sha256",
                return_value=DEPENDENCY_INVENTORY_SHA256,
            ),
        ):
            result = release_manifest.verify_live_release(
                json.loads(json.dumps(self.artifact_manifest)),
                [container],
                payload_root=self.temp_path,
            )

        self.assertEqual(result[0]["container"], container)

    def test_v3_verification_rejects_replaced_host_inode(self):
        container = "trader-v3-node-a"
        inspected = self.inspected_artifact_node(container)
        config_entry = self.artifact_manifest["node_configs"][0]
        host_stat = Path(config_entry["host_path"]).stat()
        stale_stat = mock.Mock()
        stale_stat.st_dev = host_stat.st_dev
        stale_stat.st_ino = host_stat.st_ino + 1

        with (
            mock.patch.object(
                release_manifest,
                "_runtime_image_identity",
                return_value=([inspected], IMAGE_DIGEST),
            ),
            mock.patch.object(
                release_manifest,
                "validate_strict_release_envelope",
                return_value=self.strict_envelope_evidence(),
            ),
            mock.patch.object(
                release_manifest,
                "docker_resource_contract",
                return_value=None,
            ),
            mock.patch.object(
                release_manifest,
                "_read_mountinfo",
                return_value=[
                    (config_entry["host_path"], "/cfg.json"),
                    (str(self.host_source), "/app/app/node.py"),
                ],
            ),
            mock.patch.object(
                release_manifest,
                "_stat_path",
                return_value=stale_stat,
            ),
            self.assertRaisesRegex(
                release_manifest.ReleaseManifestError,
                "config artifact inode differs",
            ),
        ):
            release_manifest.verify_live_release(
                json.loads(json.dumps(self.artifact_manifest)),
                [container],
                payload_root=self.temp_path,
            )

    def test_v3_verification_rejects_writable_config_mount(self):
        container = "trader-v3-node-a"
        inspected = self.inspected_artifact_node(container)
        inspected["Mounts"][0]["RW"] = True

        with (
            mock.patch.object(
                release_manifest,
                "_runtime_image_identity",
                return_value=([inspected], IMAGE_DIGEST),
            ),
            mock.patch.object(
                release_manifest,
                "validate_strict_release_envelope",
                return_value=self.strict_envelope_evidence(),
            ),
            mock.patch.object(
                release_manifest,
                "docker_resource_contract",
                return_value=None,
            ),
            mock.patch.object(
                release_manifest,
                "_read_mountinfo",
                return_value=[],
            ),
            self.assertRaisesRegex(
                release_manifest.ReleaseManifestError,
                "mount must be read-only",
            ),
        ):
            release_manifest.verify_live_release(
                json.loads(json.dumps(self.artifact_manifest)),
                [container],
                payload_root=self.temp_path,
            )

    def test_release_gate_accepts_matching_four_node_identity(self):
        containers = [
            "trader-v3-node-a",
            "trader-v3-node-b",
            "trader-v3-node-c",
            "trader-v3-node-d",
        ]
        inspected = [self.inspected_node(name) for name in containers]
        inspected[1]["State"]["Pid"] = 202

        with (
            mock.patch.object(
                release_manifest,
                "_runtime_identity",
                return_value=(inspected, IMAGE_DIGEST, CONFIG_SHA256),
            ),
            mock.patch.object(
                release_manifest,
                "_read_mountinfo",
                return_value=[
                    (str(self.host_source), "/app/app/node.py"),
                ],
            ),
            mock.patch.object(
                release_manifest,
                "_container_sha256",
                return_value=self.source_sha256,
            ),
        ):
            result = release_manifest.verify_live_release(
                json.loads(json.dumps(self.manifest)),
                containers,
            )

        self.assertEqual(len(result), 4)
        self.assertEqual(result[0]["image_digest"], IMAGE_DIGEST)
        self.assertEqual(result[1]["config_sha256"], CONFIG_SHA256)
        self.assertEqual(result[0]["release_id"], self.manifest["release_id"])
        self.assertEqual(
            result[0]["delivery_mode"],
            release_manifest.DELIVERY_TRANSITION,
        )

    def test_release_gate_rejects_release_label_drift(self):
        containers = ["trader-v3-node-a", "trader-v3-node-b"]
        inspected = [self.inspected_node(name) for name in containers]
        inspected[1]["Config"]["Labels"][
            release_manifest.LABEL_RELEASE_ID
        ] = "6" * 64

        with mock.patch.object(
            release_manifest,
            "_runtime_identity",
            return_value=(inspected, IMAGE_DIGEST, CONFIG_SHA256),
        ), self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "release label mismatch",
        ):
            release_manifest.verify_live_release(
                json.loads(json.dumps(self.manifest)),
                containers,
            )

        with mock.patch.object(
            release_manifest,
            "_runtime_identity",
            return_value=(inspected, IMAGE_DIGEST, "8" * 64),
        ), self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "config hash",
        ):
            release_manifest.verify_live_release(
                json.loads(json.dumps(self.manifest)),
                containers,
            )

    def test_release_gate_rejects_image_or_config_drift(self):
        containers = ["trader-v3-node-a", "trader-v3-node-b"]
        inspected = [self.inspected_node(name) for name in containers]
        with mock.patch.object(
            release_manifest,
            "_runtime_identity",
            return_value=(inspected, "sha256:" + ("7" * 64), CONFIG_SHA256),
        ), self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "image digest",
        ):
            release_manifest.verify_live_release(
                json.loads(json.dumps(self.manifest)),
                containers,
            )

    def test_release_id_covers_delivery_mode(self):
        immutable = json.loads(json.dumps(self.manifest))
        immutable["delivery_mode"] = release_manifest.DELIVERY_IMMUTABLE
        immutable["release_purpose"] = (
            release_manifest.RELEASE_PURPOSE_HARDENING
        )
        immutable["release_id"] = release_manifest.calculate_release_id(
            immutable
        )

        self.assertNotEqual(
            immutable["release_id"],
            self.manifest["release_id"],
        )

    def test_release_manifest_binds_expected_schema_epochs(self):
        self.assertEqual(
            self.manifest["schema_epochs"],
            EXPECTED_SCHEMA_EPOCHS,
        )

    def test_release_manifest_rejects_legacy_redis_schema_epoch(self):
        legacy = json.loads(json.dumps(self.manifest))
        legacy["schema_epochs"]["redis"] = "stable-account-namespace/v1"
        legacy["release_id"] = release_manifest.calculate_release_id(legacy)

        with self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "schema_epochs",
        ):
            release_manifest.validate_release_manifest(legacy)

    def test_release_id_covers_each_file_hash(self):
        tampered = json.loads(json.dumps(self.manifest))
        tampered["files"][0]["sha256"] = "6" * 64

        with self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "release_id does not match manifest contents",
        ):
            release_manifest.validate_release_manifest(tampered)

    def test_release_manifest_requires_exact_bundle_file_set(self):
        bundle_path = self.temp_path / "bundle-manifest.json"
        bundle = {
            "repo_commit": RELEASE_COMMIT,
            "repo_dirty": False,
            "schema_epochs": dict(EXPECTED_SCHEMA_EPOCHS),
            "files": [
                {
                    "bundle_path": "node.py",
                    "mount_target": "/app/app/node.py",
                    "sha256": self.source_sha256,
                }
            ],
        }
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        release_manifest.require_release_matches_bundle(
            self.manifest,
            bundle_path,
        )

        extra = self.temp_path / "health.py"
        extra.write_text("health\n", encoding="utf-8")
        bundle["files"].append(
            {
                "bundle_path": "health.py",
                "mount_target": "/app/runtime/health.py",
                "sha256": release_manifest.sha256_file(extra),
            }
        )
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        with self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "file contracts differ",
        ):
            release_manifest.require_release_matches_bundle(
                self.manifest,
                bundle_path,
            )

        bundle["files"] = bundle["files"][:1]
        bundle["files"][0]["sha256"] = "7" * 64
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        with self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "file contracts differ",
        ):
            release_manifest.require_release_matches_bundle(
                self.manifest,
                bundle_path,
            )

    def test_release_match_gate_requires_explicit_bundle_schema_epochs(self):
        bundle_path = self.temp_path / "bundle-without-epochs.json"
        bundle = {
            "repo_commit": RELEASE_COMMIT,
            "repo_dirty": False,
            "files": [
                {
                    "bundle_path": "node.py",
                    "mount_target": "/app/app/node.py",
                    "sha256": self.source_sha256,
                }
            ],
        }
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        with self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "schema_epochs",
        ):
            release_manifest.require_release_matches_bundle(
                self.manifest,
                bundle_path,
            )

    def test_release_manifest_requires_bundle_commit_and_clean_state(self):
        bundle_path = self.temp_path / "bundle-manifest.json"
        bundle = {
            "repo_commit": "a" * 40,
            "repo_dirty": False,
            "schema_epochs": dict(EXPECTED_SCHEMA_EPOCHS),
            "files": [
                {
                    "bundle_path": "node.py",
                    "mount_target": "/app/app/node.py",
                    "sha256": self.source_sha256,
                }
            ],
        }
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        with self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "commit differs",
        ):
            release_manifest.require_release_matches_bundle(
                self.manifest,
                bundle_path,
            )

        bundle["repo_commit"] = RELEASE_COMMIT
        bundle["repo_dirty"] = True
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        with self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "repo_dirty=false",
        ):
            release_manifest.require_release_matches_bundle(
                self.manifest,
                bundle_path,
            )

    def test_runtime_identity_rejects_a_b_digest_difference(self):
        config_a = self.temp_path / "account-a.json"
        config_b = self.temp_path / "account-b.json"
        config_a.write_text('{"account":"account-a"}', encoding="utf-8")
        config_b.write_text('{"account":"account-b"}', encoding="utf-8")
        inspected = []
        for name, image, config in (
            ("trader-v3-node-a", IMAGE_DIGEST, config_a),
            (
                "trader-v3-node-b",
                "sha256:" + ("9" * 64),
                config_b,
            ),
        ):
            inspected.append(
                {
                    "Name": f"/{name}",
                    "Image": image,
                    "Mounts": [
                        {
                            "Source": str(config),
                            "Destination": "/cfg.json",
                        }
                    ],
                }
            )
        with (
            mock.patch.object(
                release_manifest,
                "_docker_inspect",
                return_value=inspected,
            ),
            self.assertRaisesRegex(
                release_manifest.ReleaseManifestError,
                "node image content digests differ",
            ),
        ):
            release_manifest._runtime_identity(
                ["trader-v3-node-a", "trader-v3-node-b"]
            )

    def test_immutable_gate_accepts_unmounted_matching_image_targets(self):
        containers = ["trader-v3-node-a", "trader-v3-node-b"]
        manifest = json.loads(json.dumps(self.artifact_manifest))
        inspected = [
            self.inspected_artifact_node(name)
            for name in containers
        ]
        inspected[1]["State"]["Pid"] = 202

        with (
            mock.patch.object(
                release_manifest,
                "_runtime_image_identity",
                return_value=(inspected, IMAGE_DIGEST),
            ),
            mock.patch.object(
                release_manifest,
                "validate_strict_release_envelope",
                return_value=self.strict_envelope_evidence(),
            ),
            mock.patch.object(
                release_manifest,
                "docker_resource_contract",
                return_value=None,
            ),
            mock.patch.object(
                release_manifest,
                "_verify_live_node_config",
                return_value={},
            ),
            mock.patch.object(
                release_manifest,
                "_read_mountinfo",
                side_effect=[[], []],
            ),
            mock.patch.object(
                release_manifest,
                "_container_sha256",
                side_effect=[
                    LOCK_SHA256,
                    DEPENDENCY_INVENTORY_SHA256,
                    MIGRATION_MANIFEST_SHA256,
                    self.source_sha256,
                    LOCK_SHA256,
                    DEPENDENCY_INVENTORY_SHA256,
                    MIGRATION_MANIFEST_SHA256,
                    self.source_sha256,
                ],
            ),
            mock.patch.object(
                release_manifest,
                "_container_dependency_inventory_sha256",
                return_value=DEPENDENCY_INVENTORY_SHA256,
            ),
        ):
            result = release_manifest.verify_live_release(
                manifest,
                containers,
                payload_root=self.temp_path,
            )

        self.assertEqual(len(result), 2)

    def test_immutable_gate_rejects_target_mount_override(self):
        containers = ["trader-v3-node-a", "trader-v3-node-b"]
        manifest = json.loads(json.dumps(self.artifact_manifest))
        inspected = [
            self.inspected_artifact_node(name)
            for name in containers
        ]
        for item in inspected:
            item["Mounts"].append(
                {
                    "Source": str(self.host_source),
                    "Destination": "/app",
                }
            )

        with (
            mock.patch.object(
                release_manifest,
                "_runtime_image_identity",
                return_value=(inspected, IMAGE_DIGEST),
            ),
            mock.patch.object(
                release_manifest,
                "validate_strict_release_envelope",
                return_value=self.strict_envelope_evidence(),
            ),
            mock.patch.object(
                release_manifest,
                "docker_resource_contract",
                return_value=None,
            ),
            mock.patch.object(
                release_manifest,
                "_verify_live_node_config",
                return_value={},
            ),
            mock.patch.object(
                release_manifest,
                "_read_mountinfo",
                return_value=[],
            ),
            self.assertRaisesRegex(
                release_manifest.ReleaseManifestError,
                "immutable code root is bind-mounted",
            ),
        ):
            release_manifest.verify_live_release(
                manifest,
                containers,
                payload_root=self.temp_path,
            )

    def test_immutable_gate_rejects_node_b_old_target_bytes(self):
        containers = ["trader-v3-node-a", "trader-v3-node-b"]
        manifest = json.loads(json.dumps(self.artifact_manifest))
        inspected = [
            self.inspected_artifact_node(name)
            for name in containers
        ]
        inspected[1]["State"]["Pid"] = 202

        with (
            mock.patch.object(
                release_manifest,
                "_runtime_image_identity",
                return_value=(inspected, IMAGE_DIGEST),
            ),
            mock.patch.object(
                release_manifest,
                "validate_strict_release_envelope",
                return_value=self.strict_envelope_evidence(),
            ),
            mock.patch.object(
                release_manifest,
                "docker_resource_contract",
                return_value=None,
            ),
            mock.patch.object(
                release_manifest,
                "_verify_live_node_config",
                return_value={},
            ),
            mock.patch.object(
                release_manifest,
                "_read_mountinfo",
                side_effect=[[], []],
            ),
            mock.patch.object(
                release_manifest,
                "_container_sha256",
                side_effect=[
                    LOCK_SHA256,
                    DEPENDENCY_INVENTORY_SHA256,
                    MIGRATION_MANIFEST_SHA256,
                    self.source_sha256,
                    LOCK_SHA256,
                    DEPENDENCY_INVENTORY_SHA256,
                    MIGRATION_MANIFEST_SHA256,
                    "9" * 64,
                ],
            ),
            mock.patch.object(
                release_manifest,
                "_container_dependency_inventory_sha256",
                return_value=DEPENDENCY_INVENTORY_SHA256,
            ),
            self.assertRaisesRegex(
                release_manifest.ReleaseManifestError,
                "immutable target hash mismatch",
            ),
        ):
            release_manifest.verify_live_release(
                manifest,
                containers,
                payload_root=self.temp_path,
            )

    def test_immutable_gate_rejects_untracked_code_root_mount(self):
        containers = ["trader-v3-node-a"]
        manifest = json.loads(json.dumps(self.artifact_manifest))
        inspected = [self.inspected_artifact_node(containers[0])]
        inspected[0]["Mounts"].append(
            {
                "Source": str(self.host_source),
                "Destination": "/app/common",
            }
        )

        with (
            mock.patch.object(
                release_manifest,
                "_runtime_image_identity",
                return_value=(inspected, IMAGE_DIGEST),
            ),
            mock.patch.object(
                release_manifest,
                "validate_strict_release_envelope",
                return_value=self.strict_envelope_evidence(),
            ),
            mock.patch.object(
                release_manifest,
                "docker_resource_contract",
                return_value=None,
            ),
            mock.patch.object(
                release_manifest,
                "_verify_live_node_config",
                return_value={},
            ),
            mock.patch.object(
                release_manifest,
                "_read_mountinfo",
                return_value=[],
            ),
            self.assertRaisesRegex(
                release_manifest.ReleaseManifestError,
                "immutable code root is bind-mounted",
            ),
        ):
            release_manifest.verify_live_release(
                manifest,
                containers,
                payload_root=self.temp_path,
            )

    def test_immutable_gate_allows_normal_root_mountinfo(self):
        containers = ["trader-v3-node-a"]
        manifest = json.loads(json.dumps(self.artifact_manifest))
        inspected = [self.inspected_artifact_node(containers[0])]

        with (
            mock.patch.object(
                release_manifest,
                "_runtime_image_identity",
                return_value=(inspected, IMAGE_DIGEST),
            ),
            mock.patch.object(
                release_manifest,
                "validate_strict_release_envelope",
                return_value=self.strict_envelope_evidence(),
            ),
            mock.patch.object(
                release_manifest,
                "docker_resource_contract",
                return_value=None,
            ),
            mock.patch.object(
                release_manifest,
                "_verify_live_node_config",
                return_value={},
            ),
            mock.patch.object(
                release_manifest,
                "_read_mountinfo",
                return_value=[("/", "/"), ("/proc", "/proc")],
            ),
            mock.patch.object(
                release_manifest,
                "_container_sha256",
                side_effect=[
                    LOCK_SHA256,
                    DEPENDENCY_INVENTORY_SHA256,
                    MIGRATION_MANIFEST_SHA256,
                    self.source_sha256,
                ],
            ),
            mock.patch.object(
                release_manifest,
                "_container_dependency_inventory_sha256",
                return_value=DEPENDENCY_INVENTORY_SHA256,
            ),
        ):
            result = release_manifest.verify_live_release(
                manifest,
                containers,
                payload_root=self.temp_path,
            )

        self.assertEqual(len(result), 1)

    def test_immutable_gate_rejects_dependency_lock_mismatch(self):
        containers = ["trader-v3-node-a"]
        manifest = json.loads(json.dumps(self.artifact_manifest))
        inspected = [self.inspected_artifact_node(containers[0])]

        with (
            mock.patch.object(
                release_manifest,
                "_runtime_image_identity",
                return_value=(inspected, IMAGE_DIGEST),
            ),
            mock.patch.object(
                release_manifest,
                "validate_strict_release_envelope",
                return_value=self.strict_envelope_evidence(),
            ),
            mock.patch.object(
                release_manifest,
                "docker_resource_contract",
                return_value=None,
            ),
            mock.patch.object(
                release_manifest,
                "_verify_live_node_config",
                return_value={},
            ),
            mock.patch.object(
                release_manifest,
                "_read_mountinfo",
                return_value=[],
            ),
            mock.patch.object(
                release_manifest,
                "_container_sha256",
                return_value="9" * 64,
            ),
            self.assertRaisesRegex(
                release_manifest.ReleaseManifestError,
                "dependency lock hash differs",
            ),
        ):
            release_manifest.verify_live_release(
                manifest,
                containers,
                payload_root=self.temp_path,
            )

    def test_transition_runtime_contract_contains_all_hardening_modules(self):
        actual = {
            bundle_name: mount_target
            for bundle_name, _source_relative, mount_target in (
                release_manifest.TRANSITION_RUNTIME_FILES
            )
        }

        self.assertEqual(actual, EXPECTED_TRANSITION_RUNTIME_FILES)

    def test_transition_bundle_requires_each_hardening_runtime_file(self):
        complete = [
            {
                "bundle_path": bundle_name,
                "mount_target": mount_target,
            }
            for bundle_name, mount_target in (
                EXPECTED_TRANSITION_RUNTIME_FILES.items()
            )
        ]
        release_manifest.require_transition_runtime_files(complete)

        for missing in EXPECTED_TRANSITION_RUNTIME_FILES:
            incomplete = [
                item
                for item in complete
                if item["bundle_path"] != missing
            ]
            with self.subTest(missing=missing), self.assertRaisesRegex(
                release_manifest.ReleaseManifestError,
                "transition runtime mounts",
            ):
                release_manifest.require_transition_runtime_files(incomplete)

    def test_transition_bundle_rejects_node_only_manifest(self):
        with self.assertRaisesRegex(
            release_manifest.ReleaseManifestError,
            "transition runtime mounts",
        ):
            release_manifest.require_transition_runtime_files(
                self.manifest["files"]
            )

    def test_augment_transition_bundle_copies_and_hashes_required_files(self):
        repo_root = self.temp_path / "repo"
        bundle_dir = self.temp_path / "bundle"
        bundle_dir.mkdir()
        for bundle_name, source_relative, _ in (
            release_manifest.TRANSITION_RUNTIME_FILES
        ):
            source = repo_root / source_relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(f"source:{bundle_name}\n", encoding="utf-8")
        bundle_path = bundle_dir / "bundle-manifest.json"
        bundle_path.write_text(
            json.dumps(
                {
                    "repo_commit": RELEASE_COMMIT,
                    "repo_dirty": False,
                    "schema_epochs": dict(EXPECTED_SCHEMA_EPOCHS),
                    "files": [],
                }
            ),
            encoding="utf-8",
        )

        bundle = release_manifest.augment_transition_bundle(
            bundle_path,
            repo_root,
        )

        files = release_manifest._validated_bundle_files(
            bundle,
            self.patch_root,
        )
        release_manifest.require_transition_runtime_files(files)
        for bundle_name, _, mount_target in (
            release_manifest.TRANSITION_RUNTIME_FILES
        ):
            payload = bundle_dir / bundle_name
            self.assertTrue(payload.is_file())
            entry = next(
                item
                for item in bundle["files"]
                if item["mount_target"] == mount_target
            )
            self.assertEqual(
                entry["sha256"],
                release_manifest.sha256_file(payload),
            )


if __name__ == "__main__":
    unittest.main()
