from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "hk-deploy-20260803.sh"
APP_SCHEMA_EPOCH = "account-stall-hardening-runtime/v1"
DATABASE_SCHEMA_EPOCH = "0015_refresh_evidence_command"
REDIS_SCHEMA_EPOCH = "fenced-generation-namespace/v2"
REVIEWER_KEY_SHA256 = (
    "2b149fe2d7357dfea74441a1f6d6f1dd"
    "9ff6ea6a1a800fd5f2f7c2778339f928"
)


def _validator_source() -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    start_marker = "# ACCOUNT_B_EVIDENCE_VALIDATOR_BEGIN\n"
    end_marker = "\n# ACCOUNT_B_EVIDENCE_VALIDATOR_END"
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def _migration_validator_source() -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    function_start = text.index("apply_and_verify_database_migration()")
    heredoc_start = text.index("<<'PY'\n", function_start) + len("<<'PY'\n")
    heredoc_end = text.index("\nPY\n", heredoc_start)
    return text[heredoc_start:heredoc_end]


def _deploy_gate_mode_source() -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    function_start = text.index("configure_deploy_gate_mode()")
    heredoc_start = text.index("<<'PY'\n", function_start) + len("<<'PY'\n")
    heredoc_end = text.index("\nPY\n", heredoc_start)
    return text[heredoc_start:heredoc_end]


class AccountBEvidenceGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.manifest = {
            "schema_version": "trader-v3-release/v3",
            "release_id": "release-reviewed-a",
            "image_digest": "sha256:" + ("1" * 64),
            "config_sha256": "2" * 64,
            "dependency_lock_sha256": "3" * 64,
        }
        self.manifest_path = self.root / "release-manifest.json"
        self.manifest_path.write_text(
            json.dumps(self.manifest),
            encoding="utf-8",
        )
        self.report_names = (
            "exchange_snapshot",
            "postgres_snapshot",
            "fault_report",
            "live_trade_report",
        )
        self.evidence = {
            "schema_version": "trader-v3-account-a-canary-evidence/v2",
            "account_a_container": "trader-v3-node-a",
            "release_id": self.manifest["release_id"],
            "image_digest": self.manifest["image_digest"],
            "config_sha256": self.manifest["config_sha256"],
            "dependency_lock_sha256": self.manifest[
                "dependency_lock_sha256"
            ],
            "manifest_schema_version": self.manifest["schema_version"],
            "app_schema_epoch": APP_SCHEMA_EPOCH,
            "database_schema_epoch": DATABASE_SCHEMA_EPOCH,
            "redis_schema_epoch": REDIS_SCHEMA_EPOCH,
            "rollout_phase": "account_a_canary",
            "reviewer_public_key_sha256": REVIEWER_KEY_SHA256,
            "verify_live_passed": True,
            "canary_halted": True,
            "target_symbol": "SOLUSDT",
            "target_symbol_flat": True,
            "target_symbol_regular_orders_zero": True,
            "target_symbol_algo_orders_zero": True,
            "non_target_portfolio_baseline_sha256": "b" * 64,
            "fault_gate_passed": True,
            "live_gate_passed": True,
            "no_open_p0_p1_incidents": True,
            "soak_seconds": 1800,
            "account_a_safety_state_sha256": "a" * 64,
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }
        now = datetime.now(timezone.utc).isoformat()
        reports = {
            "exchange_snapshot": {
                "schema_version": "trader-v3-exchange-snapshot/v1",
                "account_id": "account-a",
                "symbol": "SOLUSDT",
                "release_id": self.manifest["release_id"],
                "image_digest": self.manifest["image_digest"],
                "config_sha256": self.manifest["config_sha256"],
                "target_symbol_flat": True,
                "target_symbol_regular_orders_zero": True,
                "target_symbol_algo_orders_zero": True,
                "captured_at": now,
            },
            "postgres_snapshot": {
                "schema_version": "trader-v3-postgres-snapshot/v1",
                "account_id": "account-a",
                "release_id": self.manifest["release_id"],
                "image_digest": self.manifest["image_digest"],
                "config_sha256": self.manifest["config_sha256"],
                "database_schema_epoch": DATABASE_SCHEMA_EPOCH,
                "rollout_phase": "account_a_canary",
                "heartbeat_status": "HALTED",
                "reconciliation_status": "healthy",
                "no_open_p0_p1_incidents": True,
                "runtime_generation": "runtime-account-a",
                "lease_fencing_token": 41,
                "heartbeat_sequence": 7,
                "captured_at": now,
            },
            "fault_report": {
                "schema_version": "trader-v3-fault-report/v1",
                "account_id": "account-a",
                "release_id": self.manifest["release_id"],
                "image_digest": self.manifest["image_digest"],
                "config_sha256": self.manifest["config_sha256"],
                "rollout_phase": "account_a_canary",
                "passed": True,
                "canary_halted": True,
                "scenarios": [
                    {
                        "name": "control-plane-timeout",
                        "passed": True,
                    }
                ],
                "completed_at": now,
            },
            "live_trade_report": {
                "schema_version": "trader-v3-live-trade-report/v1",
                "account_id": "account-a",
                "symbol": "SOLUSDT",
                "release_id": self.manifest["release_id"],
                "image_digest": self.manifest["image_digest"],
                "config_sha256": self.manifest["config_sha256"],
                "dependency_lock_sha256": self.manifest[
                    "dependency_lock_sha256"
                ],
                "passed": True,
                "failure_reason": "",
                "authorization_signatures_verified": True,
                "rollout_phase": "account_a_canary",
                "round_trip_count": 1,
                "finished_halted": True,
                "non_target_portfolio_before_sha256": "b" * 64,
                "non_target_portfolio_after_sha256": "b" * 64,
                "testnet_emergency_close": {
                    "verified": True,
                    "verified_at": now,
                    "environment": "testnet",
                    "release_id": self.manifest["release_id"],
                    "image_digest": self.manifest["image_digest"],
                    "symbol": "SOLUSDT",
                    "open_order_type": "LIMIT",
                    "open_time_in_force": "IOC",
                    "open_filled_quantity": "0.1",
                    "close_order_type": "MARKET",
                    "close_reduce_only": True,
                    "close_quantity": "0.1",
                    "close_filled_quantity": "0.1",
                    "target_symbol_flat": True,
                    "target_symbol_regular_orders_zero": True,
                    "target_symbol_algo_orders_zero": True,
                },
                "mainnet_round_trip": {
                    "open_order_type": "LIMIT",
                    "open_time_in_force": "IOC",
                    "open_filled_quantity": "0.1",
                    "actual_open_notional_usdt": "10",
                    "close_order_type": "MARKET",
                    "close_reduce_only": True,
                    "close_quantity": "0.1",
                    "close_filled_quantity": "0.1",
                    "target_symbol_flat": True,
                    "target_symbol_regular_orders_zero": True,
                    "target_symbol_algo_orders_zero": True,
                    "emergency_close_available": True,
                    "max_cumulative_loss_usdt": "1.49",
                    "gross_pnl_usdt": "-0.10",
                    "fees_usdt": "0.02",
                    "net_pnl_usdt": "-0.12",
                    "cumulative_net_loss_usdt": "0.12",
                },
            },
        }
        self.reports = reports
        for report_name in self.report_names:
            report_path = self.root / f"{report_name}.json"
            report_path.write_text(
                json.dumps(reports[report_name], sort_keys=True),
                encoding="utf-8",
            )
            report_path.chmod(0o400)
            digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
            self.evidence[f"{report_name}_path"] = report_path.name
            self.evidence[f"{report_name}_sha256"] = digest
        self.evidence_source = self.root / "account-a-evidence.json"
        self.evidence_copy = self.root / "account-a-evidence.copy.json"
        self._write_evidence()

    def _write_evidence(self) -> None:
        payload = json.dumps(self.evidence, sort_keys=True)
        for path in (self.evidence_source, self.evidence_copy):
            if path.exists():
                path.chmod(0o600)
            path.write_text(payload, encoding="utf-8")
            path.chmod(0o400)

    def _write_report(self, report_name: str, report: dict) -> None:
        report_path = self.root / f"{report_name}.json"
        report_path.chmod(0o600)
        report_path.write_text(
            json.dumps(report, sort_keys=True),
            encoding="utf-8",
        )
        report_path.chmod(0o400)
        digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
        self.evidence[f"{report_name}_sha256"] = digest
        self._write_evidence()

    def _run_validator(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3",
                "-c",
                _validator_source(),
                str(self.evidence_copy),
                str(self.evidence_source),
                str(self.manifest_path),
                APP_SCHEMA_EPOCH,
                DATABASE_SCHEMA_EPOCH,
                REDIS_SCHEMA_EPOCH,
                REVIEWER_KEY_SHA256,
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def _run_deploy_gate_mode_detector(
        self,
        *,
        manifest_payload: dict | None = None,
        capacity_payload: dict | None = None,
        state_manifest_payload: dict | None = None,
        state_capacity_payload: dict | None = None,
        resume_manifest: bool = True,
        state_registration_key: str | None = None,
        write_release_manifest: bool = True,
        live_manifest_payload: dict | None = None,
    ) -> subprocess.CompletedProcess[str]:
        base_manifest = {
            "release_id": "release-reviewed-a",
            "image_digest": "sha256:" + ("1" * 64),
            "config_sha256": "2" * 64,
            "dependency_lock_sha256": "3" * 64,
            "schema_epochs": {"db": DATABASE_SCHEMA_EPOCH},
        }
        base_capacity = {
            "redis_fencing_epoch": "redis-epoch-a",
            "account_ids": ["account-a", "account-b", "account-c", "account-d"],
        }
        bundle_payload = {
            "schema_version": "trader-v3-bundle-manifest/v1",
            "release_id": base_manifest["release_id"],
        }
        if manifest_payload is None:
            manifest_payload = dict(base_manifest)
        if capacity_payload is None:
            capacity_payload = dict(base_capacity)
        if state_manifest_payload is None:
            state_manifest_payload = dict(base_manifest)
        if state_capacity_payload is None:
            state_capacity_payload = dict(base_capacity)

        def encode(payload: dict) -> bytes:
            return json.dumps(payload, sort_keys=True).encode("utf-8")

        env_path = self.root / "deploy-gate.env"
        manifest_path = self.root / "account-a-release-manifest.json"
        live_manifest_path = self.root / "live-release-manifest.json"
        release_manifest_path = self.root / "staging-release-manifest.json"
        capacity_path = self.root / "redis-capacity-evidence.json"
        bundle_path = self.root / "bundle-manifest.json"
        env_path.write_text(
            "DATABASE_URL=postgresql://fixture.invalid/test\n",
            encoding="utf-8",
        )
        manifest_path.write_bytes(encode(manifest_payload))
        if write_release_manifest:
            release_manifest_path.write_bytes(encode(manifest_payload))
        if live_manifest_payload is not None:
            live_manifest_path.write_bytes(encode(live_manifest_payload))
        capacity_path.write_bytes(encode(capacity_payload))
        bundle_path.write_bytes(encode(bundle_payload))
        state_manifest_bytes = encode(state_manifest_payload)
        state_capacity_bytes = encode(state_capacity_payload)
        bundle_sha256 = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
        if state_registration_key is None:
            state_registration_key = (
                "bootstrap-register:"
                + state_manifest_payload["release_id"]
            )
        state = {
            "redis_count": 1,
            "rollout_count": 1,
            "active_rollouts": [
                [
                    state_manifest_payload["release_id"],
                    state_capacity_payload["redis_fencing_epoch"],
                    state_manifest_payload["image_digest"],
                    state_manifest_payload["config_sha256"],
                    state_manifest_payload["dependency_lock_sha256"],
                    state_manifest_payload["schema_epochs"]["db"],
                    hashlib.sha256(state_manifest_bytes).hexdigest(),
                    bundle_sha256,
                    state_registration_key,
                    "account_a_canary",
                ]
            ],
            "active_epochs": [
                [
                    state_capacity_payload["redis_fencing_epoch"],
                    hashlib.sha256(state_capacity_bytes).hexdigest(),
                ]
            ],
            "target_release_count": 0,
            "target_epoch_count": 0,
        }
        fake_module = self.root / "psycopg2.py"
        fake_module.write_text(
            """
import json
import os


STATE = json.loads(os.environ["FAKE_PG_STATE"])


class Cursor:
    def __init__(self):
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, *_args):
        self.query = query

    def fetchone(self):
        if "to_regclass" in self.query:
            return (True, True)
        if "count(*) FROM redis_fencing_epochs" in self.query:
            return (STATE["redis_count"],)
        if "WHERE redis_fencing_epoch=%s" in self.query:
            return (STATE["target_epoch_count"],)
        if (
            "count(*)" in self.query
            and "WHERE release_id=%s" in self.query
        ):
            return (STATE["target_release_count"],)
        if "count(*) FROM reviewed_release_rollouts" in self.query:
            return (STATE["rollout_count"],)
        return False

    def fetchall(self):
        if "FROM reviewed_release_rollouts" in self.query:
            return [tuple(row) for row in STATE["active_rollouts"]]
        if "FROM redis_fencing_epochs" in self.query:
            return [tuple(row) for row in STATE["active_epochs"]]
        return []


class Connection:
    def cursor(self):
        return Cursor()

    def close(self):
        return None


def connect(_url):
    return Connection()
""",
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(self.root)
        environment["FAKE_PG_STATE"] = json.dumps(state, sort_keys=True)

        return subprocess.run(
            [
                "python3",
                "-c",
                _deploy_gate_mode_source(),
                str(env_path),
                "trader-v3-node-a",
                "0",
                str(manifest_path) if resume_manifest else "",
                str(capacity_path),
                str(bundle_path),
                str(live_manifest_path),
                str(release_manifest_path),
                "1",
            ],
            cwd=REPO_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_embedded_reviewer_public_key_matches_pinned_hash(self) -> None:
        text = DEPLOY.read_text(encoding="utf-8")
        match = re.search(
            r"cat > \"\$output\" <<'PEM'\n(?P<pem>.*?)\nPEM",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        pem = (match.group("pem") + "\n").encode("ascii")
        self.assertEqual(hashlib.sha256(pem).hexdigest(), REVIEWER_KEY_SHA256)
        self.assertNotIn("ACCOUNT_B_EVIDENCE_PUBLIC_KEY", text)

    def test_required_migrations_are_atomic_and_precede_control_plane_install(
        self,
    ) -> None:
        text = DEPLOY.read_text(encoding="utf-8")
        function_start = text.index("apply_and_verify_database_migration()")
        function_end = text.index(
            "\ncapture_account_a_safety_snapshot()",
            function_start,
        )
        migration_function = text[function_start:function_end]

        self.assertIn("with conn:", migration_function)
        self.assertIn("pg_advisory_xact_lock", migration_function)
        self.assertIn('"redis_fencing_epoch"', migration_function)
        self.assertIn('"redis_fencing_epochs"', migration_function)
        self.assertIn(
            '"uq_redis_fencing_epochs_active"',
            migration_function,
        )
        self.assertIn(
            "0011 reviewed_release_rollouts columns missing",
            migration_function,
        )
        order_management_spec = migration_function.index(
            '("0005", "order_management", order_management_path)',
        )
        evidence_indexes_spec = migration_function.index(
            '"0010",\n        "evidence_and_poll_indexes",',
        )
        live_safety_spec = migration_function.index(
            '("0011", "live_safety", migration_path)',
        )
        cancel_order_spec = migration_function.index(
            '"0014",\n        "cancel_order_contract",',
        )
        refresh_evidence_spec = migration_function.index(
            '"0015",\n        "refresh_evidence_command",',
        )
        migration_sql = migration_function.index("cur.execute(sql)")
        migration_record = migration_function.index(
            "INSERT INTO schema_migrations",
        )
        durable_check = migration_function.index(
            "migration was not durably committed",
        )
        self.assertLess(order_management_spec, evidence_indexes_spec)
        self.assertLess(evidence_indexes_spec, live_safety_spec)
        self.assertLess(live_safety_spec, cancel_order_spec)
        self.assertLess(cancel_order_spec, refresh_evidence_spec)
        self.assertLess(refresh_evidence_spec, migration_sql)
        self.assertLess(migration_sql, migration_record)
        self.assertLess(migration_record, durable_check)

        apply_call = text.index(
            "\napply_and_verify_database_migration\n",
            function_end,
        )
        install_section = text.index("\n# ---------- install ----------", apply_call)
        control_plane_restart = text.index(
            "\nrestart_control_plane_units\n",
            install_section,
        )
        self.assertLess(apply_call, install_section)
        self.assertLess(install_section, control_plane_restart)
        bootstrap_register = text.index(
            "\n    ensure_bootstrap_rollout_registration\n",
            apply_call,
        )
        self.assertLess(apply_call, bootstrap_register)
        self.assertLess(bootstrap_register, install_section)
        bootstrap_function_start = text.index(
            "ensure_bootstrap_rollout_registration()",
        )
        bootstrap_function_end = text.index(
            "\nwrite_bootstrap_recovery_blocked_evidence()",
            bootstrap_function_start,
        )
        bootstrap_function = text[
            bootstrap_function_start:bootstrap_function_end
        ]
        self.assertIn(
            "run_reviewed_rollout bootstrap-register",
            bootstrap_function,
        )
        self.assertIn(
            '--idempotency-key "bootstrap-register:$RELEASE_ID"',
            bootstrap_function,
        )
        self.assertIn(
            '--capacity-evidence "$REDIS_CAPACITY_EVIDENCE"',
            bootstrap_function,
        )

    def test_bootstrap_recreate_nodes_uses_ad_fleet_loops(self) -> None:
        text = DEPLOY.read_text(encoding="utf-8")
        configure_start = text.index("configure_deploy_gate_mode()")
        configure_end = text.index("\nverify_bootstrap_stopped_gate()", configure_start)
        configure_function = text[configure_start:configure_end]
        bootstrap_branch = configure_function.index("bootstrap_stopped)")
        resume_branch = configure_function.index("bootstrap_resume_stopped)")
        migration_branch = configure_function.index(
            "migration_rebaseline_stopped)"
        )
        maintenance_branch = configure_function.index("maintenance_fence)")

        self.assertIn(
            'RECREATE_NODES=("${ALL_NODES[@]}")',
            configure_function[bootstrap_branch:resume_branch],
        )
        self.assertIn(
            'RECREATE_NODES=("${ALL_NODES[@]}")',
            configure_function[resume_branch:migration_branch],
        )
        self.assertIn(
            'RECREATE_NODES=("${ALL_NODES[@]}")',
            configure_function[migration_branch:maintenance_branch],
        )

        recreate_start = text.index("# ---------- recreate & verify ----------")
        recreate_end = text.index(
            'if [ "$ROLLOUT_NODE" = "trader-v3-node-b" ]',
            recreate_start,
        )
        recreate_section = text[recreate_start:recreate_end]
        generate_loop = recreate_section.index(
            'for node in "${RECREATE_NODES[@]}"; do\n'
            '  generate_release_recreate "$RELEASE_MANIFEST" "$node"',
        )
        bootstrap_bash_loop = recreate_section.index(
            'for node in "${RECREATE_NODES[@]}"; do\n'
            '  recreate_release_node "$node"',
            generate_loop,
        )
        verify_loop = recreate_section.index(
            'verify_release_nodes "${RECREATE_NODES[@]}"',
            bootstrap_bash_loop,
        )
        self.assertLess(generate_loop, bootstrap_bash_loop)
        self.assertLess(bootstrap_bash_loop, verify_loop)
        self.assertNotIn(
            'bash "$T/recreate-$ROLLOUT_NODE.sh"',
            recreate_section,
        )

    def test_migration_rebaseline_requires_stopped_restart_no_fleet(
        self,
    ) -> None:
        text = DEPLOY.read_text(encoding="utf-8")
        stopped_start = text.index(
            "verify_all_execution_accounts_stopped()"
        )
        stopped_end = text.index("\nstop_recreate_nodes()", stopped_start)
        stopped_gate = text[stopped_start:stopped_end]
        configure_start = text.index("configure_deploy_gate_mode()")
        configure_end = text.index(
            "\nBOOTSTRAP_GATE_QUIESCED_BY=",
            configure_start,
        )
        configure_gate = text[configure_start:configure_end]
        quiescence_start = text.index("verify_bootstrap_gate_quiescence()")
        quiescence_end = text.index(
            "\nverify_bootstrap_stopped_gate()",
            quiescence_start,
        )
        quiescence_gate = text[quiescence_start:quiescence_end]

        self.assertIn('for node in "${ALL_NODES[@]}"; do', stopped_gate)
        self.assertIn("{{.State.Running}}", stopped_gate)
        self.assertIn("{{.HostConfig.RestartPolicy.Name}}", stopped_gate)
        self.assertIn('[ "$running" = "false" ]', stopped_gate)
        self.assertIn('[ "$restart_policy" = "no" ]', stopped_gate)
        self.assertIn(
            "migration_rebaseline_stopped)",
            configure_gate,
        )
        self.assertIn(
            "verify_bootstrap_gate_quiescence",
            configure_gate,
        )
        self.assertIn(
            "migration_rebaseline_live_manifest_exists",
            quiescence_gate,
        )
        self.assertIn(
            "verify_all_execution_accounts_quiesced",
            quiescence_gate,
        )
        self.assertIn(
            "verify_all_execution_accounts_stopped",
            quiescence_gate,
        )
        self.assertIn(
            'BOOTSTRAP_GATE_QUIESCED_BY="stopped-container"',
            quiescence_gate,
        )
        self.assertIn(
            "verify_migration_rebaseline_live_manifest",
            quiescence_gate,
        )
        helper_start = text.index(
            "verify_migration_rebaseline_live_manifest()"
        )
        helper_end = text.index(
            "\nverify_bootstrap_stopped_gate()",
            helper_start,
        )
        helper = text[helper_start:helper_end]
        self.assertIn(
            'cmp -s "$live_manifest" "$RELEASE_MANIFEST"',
            helper,
        )
        self.assertIn(
            "return 1",
            helper,
        )

    def test_new_control_plane_role_modules_support_first_install(self) -> None:
        text = DEPLOY.read_text(encoding="utf-8")

        self.assertNotIn(
            '[ -f "$APP_ROLES_TGT" ] '
            '|| die "live app_roles not found at $APP_ROLES_TGT"',
            text,
        )
        self.assertNotIn(
            '[ -f "$DB_POOLS_TGT" ] '
            '|| die "live pools not found at $DB_POOLS_TGT"',
            text,
        )
        self.assertIn(
            'if [ -f "$APP_ROLES_TGT" ]; then\n'
            '        bk "$APP_ROLES_TGT" "host__app_roles.py"\n'
            '      else\n'
            "        printf '%s\\n' \"$APP_ROLES_TGT\" \\\n"
            '          >> "$BACKUP_ROOT/new-files.txt"\n'
            "      fi",
            text,
        )
        self.assertIn(
            'if [ -f "$DB_POOLS_TGT" ]; then\n'
            '        bk "$DB_POOLS_TGT" "host__pools.py"\n'
            '      else\n'
            "        printf '%s\\n' \"$DB_POOLS_TGT\" \\\n"
            '          >> "$BACKUP_ROOT/new-files.txt"\n'
            "      fi",
            text,
        )
        self.assertIn(
            'install_host_python_module host/app_roles.py "$APP_ROLES_TGT"',
            text,
        )
        self.assertIn(
            'install_host_python_module host/pools.py "$DB_POOLS_TGT"',
            text,
        )

    def test_valid_evidence_recomputes_all_report_hashes(self) -> None:
        result = self._run_validator()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            f"{'a' * 64}\tSOLUSDT\t{'b' * 64}",
        )

    def test_legacy_redis_schema_epoch_is_rejected(self) -> None:
        self.evidence["redis_schema_epoch"] = (
            "stable-account-namespace/v1"
        )
        self._write_evidence()

        result = self._run_validator()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "account-b evidence mismatch: redis_schema_epoch",
            result.stderr,
        )

    def test_mutated_report_is_rejected(self) -> None:
        report_path = self.root / "live_trade_report.json"
        report_path.chmod(0o600)
        report_path.write_text('{"report":"mutated"}', encoding="utf-8")
        report_path.chmod(0o400)

        result = self._run_validator()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "account-b report hash mismatch: live_trade_report",
            result.stderr,
        )

    def test_live_trade_report_rejects_missing_required_fields(self) -> None:
        required_fields = (
            "passed",
            "failure_reason",
            "authorization_signatures_verified",
            "rollout_phase",
            "round_trip_count",
            "finished_halted",
        )
        original = copy.deepcopy(self.reports["live_trade_report"])
        for field in required_fields:
            with self.subTest(field=field):
                report = copy.deepcopy(original)
                report.pop(field)
                self._write_report("live_trade_report", report)

                result = self._run_validator()

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    f"live trade report mismatch: {field}",
                    result.stderr,
                )

    def test_live_trade_report_rejects_incomplete_safety_evidence(self) -> None:
        mutations = {
            "report did not pass": lambda report: report.update(
                {"passed": False}
            ),
            "report has failure reason": lambda report: report.update(
                {"failure_reason": "authorization failed"}
            ),
            "authorization signatures unverified": lambda report: (
                report.update(
                    {"authorization_signatures_verified": False}
                )
            ),
            "testnet evidence": lambda report: report.pop(
                "testnet_emergency_close"
            ),
            "testnet close quantity": lambda report: report[
                "testnet_emergency_close"
            ].update({"close_quantity": "0.2"}),
            "mainnet close quantity": lambda report: report[
                "mainnet_round_trip"
            ].update({"close_filled_quantity": "0.09"}),
            "mainnet notional": lambda report: report[
                "mainnet_round_trip"
            ].update({"actual_open_notional_usdt": "12.01"}),
            "mainnet loss limit": lambda report: report[
                "mainnet_round_trip"
            ].update({"max_cumulative_loss_usdt": "1.51"}),
            "mainnet loss limit exact boundary": lambda report: report[
                "mainnet_round_trip"
            ].update({"max_cumulative_loss_usdt": "1.5"}),
            "mainnet loss": lambda report: report[
                "mainnet_round_trip"
            ].update(
                {
                    "gross_pnl_usdt": "-1.49",
                    "fees_usdt": "0.01",
                    "net_pnl_usdt": "-1.50",
                    "cumulative_net_loss_usdt": "1.50",
                }
            ),
            "mainnet flat": lambda report: report[
                "mainnet_round_trip"
            ].update({"target_symbol_flat": False}),
            "halted final state": lambda report: report.update(
                {"finished_halted": False}
            ),
            "portfolio baseline": lambda report: report.update(
                {"non_target_portfolio_after_sha256": "c" * 64}
            ),
        }
        original = copy.deepcopy(self.reports["live_trade_report"])
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                report = copy.deepcopy(original)
                mutate(report)
                self._write_report("live_trade_report", report)
                result = self._run_validator()
                self.assertNotEqual(result.returncode, 0)

    def test_embedded_migration_validator_compiles(self) -> None:
        compile(
            _migration_validator_source(),
            "hk-deploy-embedded-migration.py",
            "exec",
        )

    def test_embedded_migration_validator_requires_deploy_gate_mode(self) -> None:
        text = DEPLOY.read_text(encoding="utf-8")
        function_start = text.index("apply_and_verify_database_migration()")
        heredoc_start = text.index("<<'PY'\n", function_start)
        invocation = text[function_start:heredoc_start]
        source = _migration_validator_source()

        self.assertIn('"$DEPLOY_GATE_MODE"', invocation)
        self.assertIn("deploy_gate_mode = sys.argv[17]", source)
        self.assertIn('"bootstrap_stopped"', source)
        self.assertIn('"bootstrap_resume_stopped"', source)
        self.assertIn('"maintenance_fence"', source)
        self.assertIn("bootstrap migration must not carry a fence id", source)

    def test_existing_0010_without_sql_hash_is_rejected(self) -> None:
        fake_module = self.root / "psycopg2.py"
        migration_payload = "SELECT 1;\n"
        migration_digest = hashlib.sha256(
            migration_payload.encode("utf-8")
        ).hexdigest()
        fake_module.write_text(
            f"""
class Cursor:
    def __init__(self):
        self.fetchone_count = 0
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False
    def execute(self, *_args):
        return None
    def fetchone(self):
        self.fetchone_count += 1
        if self.fetchone_count == 1:
            return ("order_management", "{migration_digest}")
        return ("live_safety", None)

class Connection:
    def __init__(self):
        self._cursor = Cursor()
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False
    def cursor(self):
        return self._cursor
    def close(self):
        return None

def connect(_url):
    return Connection()
""",
            encoding="utf-8",
        )
        env_path = self.root / "deploy.env"
        env_path.write_text(
            "DATABASE_URL=postgresql://fixture.invalid/test\n",
            encoding="utf-8",
        )
        order_management_path = (
            self.root / "0005_order_management.up.sql"
        )
        order_management_path.write_text(
            migration_payload,
            encoding="utf-8",
        )
        evidence_indexes_path = (
            self.root / "0010_evidence_and_poll_indexes.up.sql"
        )
        evidence_indexes_path.write_text(
            migration_payload,
            encoding="utf-8",
        )
        live_safety_path = self.root / "0011_live_safety.up.sql"
        live_safety_path.write_text(
            migration_payload,
            encoding="utf-8",
        )
        maintenance_fence_path = (
            self.root / "0012_control_plane_maintenance_fence.up.sql"
        )
        maintenance_fence_path.write_text(
            migration_payload,
            encoding="utf-8",
        )
        four_account_path = (
            self.root / "0013_four_account_rollout.up.sql"
        )
        four_account_path.write_text(
            migration_payload,
            encoding="utf-8",
        )
        cancel_order_path = (
            self.root / "0014_cancel_order_contract.up.sql"
        )
        cancel_order_path.write_text(
            migration_payload,
            encoding="utf-8",
        )
        refresh_evidence_path = (
            self.root / "0015_refresh_evidence_command.up.sql"
        )
        refresh_evidence_path.write_text(
            migration_payload,
            encoding="utf-8",
        )
        marker_path = self.root / "migration-marker.json"
        fence_state_path = self.root / "maintenance-fence.json"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(self.root)

        result = subprocess.run(
            [
                "python3",
                "-c",
                _migration_validator_source(),
                str(env_path),
                str(order_management_path),
                str(evidence_indexes_path),
                str(live_safety_path),
                str(maintenance_fence_path),
                str(four_account_path),
                str(cancel_order_path),
                str(refresh_evidence_path),
                DATABASE_SCHEMA_EPOCH,
                str(marker_path),
                "58deee06-3a70-47d3-b056-92854fc6c322",
                "owner-token",
                "test-actor",
                str(fence_state_path),
                "120",
                "15",
                "maintenance_fence",
            ],
            cwd=REPO_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SQL hash mismatch", result.stderr)

    def test_bootstrap_fence_waits_for_all_nodes_halted_and_versioned(
        self,
    ) -> None:
        text = DEPLOY.read_text(encoding="utf-8")
        verify_function_start = text.index("verify_release_nodes()")
        verify_function_end = text.index("\ncleanup()", verify_function_start)
        verify_function = text[verify_function_start:verify_function_end]
        halted = verify_function.index('verify_node_ready_halted "$node"')
        version = verify_function.index('verify_version_endpoint "$port"')
        live = verify_function.index('python3 "$RELEASE_TOOL" verify-live')

        self.assertLess(halted, version)
        self.assertLess(version, live)

        phase_start = text.index('if [ "$PHASE_ONLY_ROLLOUT" = "1" ]; then')
        phase_end = text.index("\n# ---------- backup ----------", phase_start)
        phase_section = text[phase_start:phase_end]
        phase_verify = phase_section.index('verify_release_nodes "${ALL_NODES[@]}"')
        phase_fence = phase_section.index("acquire_maintenance_fence_after_bootstrap")
        self.assertLess(phase_verify, phase_fence)

        recreate_start = text.index("# ---------- recreate & verify ----------")
        recreate_end = text.index(
            'if [ "$ROLLOUT_NODE" = "trader-v3-node-b" ]',
            recreate_start,
        )
        recreate_section = text[recreate_start:recreate_end]
        fleet_verify = recreate_section.index(
            'verify_release_nodes "${RECREATE_NODES[@]}"',
        )
        bootstrap_fence = recreate_section.index(
            "acquire_maintenance_fence_after_bootstrap",
            fleet_verify,
        )
        self.assertLess(fleet_verify, bootstrap_fence)

    def test_bootstrap_register_failure_stops_ad_and_blocks_restore(
        self,
    ) -> None:
        text = DEPLOY.read_text(encoding="utf-8")
        on_err_start = text.index("on_err()")
        on_err_end = text.index("\ntrap 'on_err $?'", on_err_start)
        on_err = text[on_err_start:on_err_end]

        self.assertIn('failure_nodes=("${RECREATE_NODES[@]}")', on_err)
        self.assertIn('failure_nodes=("${ALL_NODES[@]}")', on_err)
        initial_stop = on_err.index(
            'for node in "${failure_nodes[@]}"; do\n'
            '    docker stop --time 30 "$node"',
        )
        migration_recovery = on_err.index(
            'if [ "$post_migration_recovery_required" = "1" ]; then',
        )
        bootstrap_register = on_err.index(
            "&& ! ensure_bootstrap_rollout_registration",
            migration_recovery,
        )
        blocked_evidence = on_err.index(
            '"bootstrap-registration-failed-after-migration"',
            bootstrap_register,
        )
        recovery_failed = on_err.index(
            'echo "!! post-migration recovery FAILED; nodes remain stopped"',
            blocked_evidence,
        )
        final_stop = on_err.index(
            'for node in "${failure_nodes[@]}"; do\n'
            '        docker stop --time 30 "$node"',
            recovery_failed,
        )
        restore = on_err.index("restore_pre_migration_state", final_stop)

        self.assertLess(initial_stop, migration_recovery)
        self.assertLess(bootstrap_register, blocked_evidence)
        self.assertLess(blocked_evidence, final_stop)
        self.assertLess(final_stop, restore)
        self.assertNotIn(
            "restore_pre_migration_state",
            on_err[bootstrap_register:blocked_evidence],
        )

    def test_bootstrap_resume_accepts_matching_manifest_and_epoch(self) -> None:
        result = self._run_deploy_gate_mode_detector()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "bootstrap_resume_stopped")

    def test_bootstrap_resume_rejects_manifest_mismatch(self) -> None:
        manifest = {
            "release_id": "release-reviewed-a",
            "image_digest": "sha256:" + ("4" * 64),
            "config_sha256": "2" * 64,
            "dependency_lock_sha256": "3" * 64,
            "schema_epochs": {"db": DATABASE_SCHEMA_EPOCH},
        }

        result = self._run_deploy_gate_mode_detector(
            manifest_payload=manifest,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "bootstrap resume manifest differs from active rollout",
            result.stderr,
        )

    def test_bootstrap_resume_rejects_redis_epoch_evidence_mismatch(
        self,
    ) -> None:
        capacity = {
            "redis_fencing_epoch": "redis-epoch-a",
            "account_ids": ["account-a", "account-b", "account-c", "account-d"],
            "nonce": "changed",
        }

        result = self._run_deploy_gate_mode_detector(
            capacity_payload=capacity,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "bootstrap resume Redis epoch evidence differs",
            result.stderr,
        )

    def test_migration_rebaseline_accepts_migrated_history_and_fresh_epoch(
        self,
    ) -> None:
        old_manifest = {
            "release_id": "release-hk-old",
            "image_digest": "sha256:" + ("4" * 64),
            "config_sha256": "5" * 64,
            "dependency_lock_sha256": "6" * 64,
            "schema_epochs": {"db": DATABASE_SCHEMA_EPOCH},
        }
        old_capacity = {
            "redis_fencing_epoch": "redis-epoch-hk-old",
            "account_ids": ["account-a", "account-b", "account-c", "account-d"],
        }
        result = self._run_deploy_gate_mode_detector(
            state_manifest_payload=old_manifest,
            state_capacity_payload=old_capacity,
            resume_manifest=False,
            write_release_manifest=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "migration_rebaseline_stopped",
        )

    def test_migration_rebaseline_rejects_reused_redis_epoch(self) -> None:
        old_manifest = {
            "release_id": "release-hk-old",
            "image_digest": "sha256:" + ("4" * 64),
            "config_sha256": "5" * 64,
            "dependency_lock_sha256": "6" * 64,
            "schema_epochs": {"db": DATABASE_SCHEMA_EPOCH},
        }
        shared_capacity = {
            "redis_fencing_epoch": "redis-epoch-shared",
            "account_ids": ["account-a", "account-b", "account-c", "account-d"],
        }
        result = self._run_deploy_gate_mode_detector(
            capacity_payload=shared_capacity,
            state_manifest_payload=old_manifest,
            state_capacity_payload=shared_capacity,
            resume_manifest=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "migration rebaseline active release differs from the fresh Redis epoch",
            result.stderr,
        )

    def test_same_epoch_live_hotfix_uses_maintenance_fence(self) -> None:
        old_manifest = {
            "release_id": "release-hk-old",
            "image_digest": "sha256:" + ("4" * 64),
            "config_sha256": "5" * 64,
            "dependency_lock_sha256": "6" * 64,
            "schema_epochs": {"db": DATABASE_SCHEMA_EPOCH},
        }
        hotfix_manifest = {
            "release_id": "release-hotfix",
            "image_digest": "sha256:" + ("7" * 64),
            "config_sha256": "8" * 64,
            "dependency_lock_sha256": "6" * 64,
            "schema_epochs": {"db": DATABASE_SCHEMA_EPOCH},
        }
        shared_capacity = {
            "redis_fencing_epoch": "redis-epoch-shared",
            "account_ids": ["account-a", "account-b", "account-c", "account-d"],
        }
        result = self._run_deploy_gate_mode_detector(
            manifest_payload=hotfix_manifest,
            capacity_payload=shared_capacity,
            state_manifest_payload=old_manifest,
            state_capacity_payload=shared_capacity,
            resume_manifest=False,
            live_manifest_payload=old_manifest,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "maintenance_fence")

    def test_migration_rebaseline_replay_requires_exact_release(self) -> None:
        manifest = {
            "release_id": "release-reviewed-a",
            "image_digest": "sha256:" + ("1" * 64),
            "config_sha256": "2" * 64,
            "dependency_lock_sha256": "3" * 64,
            "schema_epochs": {"db": DATABASE_SCHEMA_EPOCH},
        }
        result = self._run_deploy_gate_mode_detector(
            manifest_payload=manifest,
            state_manifest_payload=manifest,
            resume_manifest=False,
            state_registration_key=(
                "migration-rebaseline-register:release-reviewed-a"
            ),
            live_manifest_payload=manifest,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "migration_rebaseline_stopped",
        )

    def test_migration_rebaseline_accepts_prior_live_manifest_mismatch(
        self,
    ) -> None:
        manifest = {
            "release_id": "release-reviewed-a",
            "image_digest": "sha256:" + ("1" * 64),
            "config_sha256": "2" * 64,
            "dependency_lock_sha256": "3" * 64,
            "schema_epochs": {"db": DATABASE_SCHEMA_EPOCH},
        }
        live_manifest = dict(manifest)
        live_manifest["config_sha256"] = "9" * 64

        result = self._run_deploy_gate_mode_detector(
            manifest_payload=manifest,
            state_manifest_payload=manifest,
            resume_manifest=False,
            state_registration_key=(
                "migration-rebaseline-register:release-reviewed-a"
            ),
            live_manifest_payload=live_manifest,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "migration_rebaseline_stopped",
        )

    def test_live_bootstrap_rollout_remains_maintenance_fence(self) -> None:
        manifest = {
            "release_id": "release-reviewed-a",
            "image_digest": "sha256:" + ("1" * 64),
            "config_sha256": "2" * 64,
            "dependency_lock_sha256": "3" * 64,
            "schema_epochs": {"db": DATABASE_SCHEMA_EPOCH},
        }

        result = self._run_deploy_gate_mode_detector(
            manifest_payload=manifest,
            state_manifest_payload=manifest,
            resume_manifest=False,
            live_manifest_payload=manifest,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "maintenance_fence")

    def test_report_symlink_is_rejected(self) -> None:
        report_path = self.root / "fault_report.json"
        target = self.root / "fault-report-target.json"
        target.write_bytes(report_path.read_bytes())
        target.chmod(0o400)
        report_path.unlink()
        report_path.symlink_to(target)

        result = self._run_validator()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("report cannot be a symlink", result.stderr)


if __name__ == "__main__":
    unittest.main()
