import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTINUE = REPO_ROOT / "scripts" / "hk-root-continue-20260729.sh"
ROLLBACK = REPO_ROOT / "scripts" / "hk-rollback-20260729.sh"


class RootContinue20260729Test(unittest.TestCase):
    def script_text(self):
        self.assertTrue(
            CONTINUE.is_file(),
            f"continuation script is missing: {CONTINUE}",
        )
        return CONTINUE.read_text(encoding="utf-8")

    def run_fail_closed_cleanup(
        self,
        flock_ok,
        docker_running,
        systemctl_ok=True,
    ):
        text = self.script_text()
        function_start = text.index("fail_closed_cleanup() {")
        function_end = text.index(
            "\n}\n\nreport_fail_closed_state()",
            function_start,
        ) + 3
        function_text = text[function_start:function_end]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            command_log = temp_path / "commands.log"
            scripts = {
                "systemctl": (
                    "#!/bin/sh\n"
                    "printf 'systemctl %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
                    "[ \"$SYSTEMCTL_OK\" = 1 ]\n"
                ),
                "flock": (
                    "#!/bin/sh\n"
                    "printf 'flock %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
                    "[ \"$FLOCK_OK\" = 1 ]\n"
                ),
                "docker": (
                    "#!/bin/sh\n"
                    "printf 'docker %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
                    "if [ \"$1\" = inspect ]; then\n"
                    "  printf '%s\\n' \"$DOCKER_RUNNING\"\n"
                    "fi\n"
                    "exit 0\n"
                ),
                "sleep": "#!/bin/sh\nexit 0\n",
            }
            for name, content in scripts.items():
                path = fake_bin / name
                path.write_text(content, encoding="utf-8")
                path.chmod(0o755)
            harness = temp_path / "cleanup-harness.sh"
            harness.write_text(
                "#!/usr/bin/env bash\n"
                "set -u\n"
                'NODE_A="node-a"\n'
                'NODE_B="node-b"\n'
                "CLEANUP_CONFIRMED=0\n"
                f"{function_text}\n"
                "set +e\n"
                "fail_closed_cleanup\n"
                "status=$?\n"
                'printf "status=%s confirmed=%s\\n" '
                '"$status" "$CLEANUP_CONFIRMED"\n',
                encoding="utf-8",
            )
            harness.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["COMMAND_LOG"] = str(command_log)
            env["FLOCK_OK"] = "1" if flock_ok else "0"
            env["DOCKER_RUNNING"] = docker_running
            env["SYSTEMCTL_OK"] = "1" if systemctl_ok else "0"
            result = subprocess.run(
                ["bash", str(harness)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            log = ""
            if command_log.exists():
                log = command_log.read_text(encoding="utf-8")
            return result, log

    def test_reuses_existing_containers_without_executable_recreate_commands(self):
        text = self.script_text()

        self.assertNotRegex(
            text,
            re.compile(
                r"^[ \t]*(?:sudo[ \t]+)?docker[ \t]+"
                r"(?:container[ \t]+)?(?:run|create)\b",
                re.MULTILINE,
            ),
        )
        self.assertNotRegex(
            text,
            re.compile(
                r"^[ \t]*(?:sudo[ \t]+)?docker[ \t]+"
                r"compose[^\n]*\b(?:up|create)\b",
                re.MULTILINE,
            ),
        )
        self.assertNotIn("--force-recreate", text)
        self.assertNotRegex(
            text,
            re.compile(
                r"^[ \t]*(?:sudo[ \t]+)?(?:bash|sh|python3?)[ \t]+"
                r"[^\n#]*(?:recreate-[^\n#]*\.sh|"
                r"hk-gen-recreate-patched\.py)",
                re.MULTILINE,
            ),
        )

    def test_staging_self_locates_and_reviewed_sha_is_explicit(self):
        text = self.script_text()
        header = "\n".join(text.splitlines()[:30])

        script_dir_match = re.search(
            r"(?ms)^([A-Z_]*DIR)=\$\("
            r".*?BASH_SOURCE\[0\].*?\bpwd\b.*?^\)",
            header,
        )
        d_assignment = re.search(r"(?m)^D=(.+)$", header)
        sha_assignment = re.search(
            r"(?m)^EXPECTED_STAGING_MANIFEST_SHA256=(.+)$",
            header,
        )
        self.assertIsNotNone(script_dir_match)
        self.assertIsNotNone(d_assignment)
        self.assertIsNotNone(sha_assignment)

        script_dir_name = script_dir_match.group(1)
        self.assertRegex(
            d_assignment.group(1),
            re.compile(rf"\$(?:\{{)?{script_dir_name}(?:\}})?"),
        )
        self.assertNotIn("/home/balen/deploy-", d_assignment.group(1))
        self.assertNotRegex(
            sha_assignment.group(1),
            re.compile(r"[0-9a-f]{64}"),
        )
        self.assertIn(
            'BACKUP_ROOT="/srv/trader-v3/backups/'
            'deploy-20260729T084509Z"',
            header,
        )
        self.assertNotIn("${BACKUP_ROOT:-", header)
        rollback_header = "\n".join(
            ROLLBACK.read_text(encoding="utf-8").splitlines()[:12]
        )
        self.assertIn("BASH_SOURCE[0]", rollback_header)
        self.assertIn('D="${DEPLOY_DIR:-$SCRIPT_DIR}"', rollback_header)

        sha_guard_start = re.search(
            r'(?m)^\s*\[{1,2}[^\n]*'
            r'\$EXPECTED_STAGING_MANIFEST_SHA256[^\n]*',
            text,
        )
        sha_use = text.index("ACTUAL_STAGING_MANIFEST_SHA256")
        explicit_parameter = ":?" in sha_assignment.group(1)
        explicit_guard = False
        if sha_guard_start is not None:
            guard_block = text[
                sha_guard_start.start():sha_guard_start.start() + 300
            ]
            explicit_guard = "die" in guard_block or "exit" in guard_block
        self.assertTrue(
            explicit_parameter or explicit_guard,
            "reviewed staging SHA must be supplied by argument or environment",
        )
        if sha_guard_start is not None:
            self.assertLess(sha_guard_start.start(), sha_use)

    def test_rejects_non_stopped_nodes_before_starting_node_a(self):
        text = self.script_text()
        node_a_start = text.index('docker start "$NODE_A"')
        stopped_preflight = text.rindex("verify_existing_node_runtime")

        self.assertIn('item.get("State")', text)
        self.assertIn('state.get("Running")', text)
        self.assertIn('state.get("Status") != "exited"', text)
        self.assertLess(stopped_preflight, node_a_start)

    def test_starts_nodes_sequentially_and_accepts_only_ready_halted(self):
        text = self.script_text()

        self.assertIn('payload.get("ready")', text)
        self.assertIn('payload.get("trading_state") == "HALTED"', text)
        node_a_start = text.index('docker start "$NODE_A"')
        node_a_ready = text.index("verify_node_halted 8081", node_a_start)
        node_b_start = text.index('docker start "$NODE_B"')
        node_b_ready = text.index("verify_node_halted 8082", node_b_start)
        self.assertLess(node_a_start, node_a_ready)
        self.assertLess(node_a_ready, node_b_start)
        self.assertLess(node_b_start, node_b_ready)

    def test_verifies_migration_0009_without_generic_migrations(self):
        text = self.script_text()

        self.assertIn("database_marker verify-0009", text)
        self.assertIn("trade_outcome_job_runs", text)
        self.assertNotRegex(
            text,
            re.compile(
                r"^[ \t]*[^#\n]*migrate(?:\.py)?[^\n]*"
                r"(?:\bup\b|\bdown\b)",
                re.IGNORECASE | re.MULTILINE,
            ),
        )

    def test_verifies_staging_backup_hashes_and_exact_mount_pairs(self):
        text = self.script_text()

        self.assertIn(
            '(cd "$D" && sha256sum -c SHA256SUMS)',
            text,
        )
        self.assertIn(
            '(cd "$BACKUP_ROOT" && sha256sum -c BACKUP_SHA256SUMS)',
            text,
        )
        self.assertIn("manifest.txt", text)
        self.assertIn(
            "required artifacts missing from staging SHA256SUMS",
            text,
        )
        self.assertIn('hk-root-continue-20260729.sh', text)
        self.assertIn('hk-rollback-20260729.sh', text)
        inline_verification = (
            "hashlib.sha256" in text
            and "/proc/{pid}/mountinfo" in text
            and "pairs.count((source, destination)) != 1" in text
        )
        verifier_command = (
            "verify_hk_deployment.sh" in text
            and '"$D/manifest.txt"' in text
        )
        self.assertTrue(
            inline_verification or verifier_command,
            "continuation must verify manifest hashes and exact mount pairs",
        )

    def test_requires_fresh_outcome_watermark_before_timer_start(self):
        text = self.script_text()

        run_started = text.index(
            "OUTCOME_RUN_STARTED_AT=$(database_marker capture-time)"
        )
        manual_start = text.index(
            "systemctl start trader-v3-trade-outcomes.service"
        )
        watermark = text.index(
            'database_marker verify-watermark "$OUTCOME_RUN_STARTED_AT"'
        )
        outcome_files = text.index("verify_outcome_files", watermark)
        timer_start = text.index(
            "systemctl start trader-v3-trade-outcomes.timer"
        )
        self.assertRegex(
            text,
            re.compile(r"started_at\s*<=?\s*required_timestamp"),
        )
        self.assertRegex(
            text,
            re.compile(r"completed_at\s*<=?\s*required_timestamp"),
        )
        self.assertIn(
            "/var/log/trader-v3/trade-outcomes-latest.json",
            text,
        )
        self.assertIn("/var/log/trader-v3/trade-outcomes.log", text)
        self.assertIn('if mode == "capture-time"', text)
        self.assertIn('cur.execute("SELECT clock_timestamp()")', text)
        self.assertIn('payload["upserted_count"] <= 0', text)
        self.assertLess(run_started, manual_start)
        self.assertLess(manual_start, watermark)
        self.assertLess(watermark, outcome_files)
        self.assertLess(outcome_files, timer_start)
        self.assertNotIn(
            "systemctl enable --now trader-v3-trade-outcomes.timer",
            text,
        )

    def test_checks_report_health_daily_weekly_then_finishes_halted(self):
        text = self.script_text()

        report_check = text.rindex("verify_report")
        final_node_a = text.rindex("verify_node_halted 8081")
        final_node_b = text.rindex("verify_node_halted 8082")
        success_match = re.search(
            r'echo "(?:DEPLOY )?CONTINUATION SUCCEEDED"',
            text,
        )
        self.assertIsNotNone(success_match)
        success = success_match.start()
        self.assertIn("http://127.0.0.1:8090/healthz", text)
        self.assertIn('("daily", "weekly")', text)
        self.assertIn("fetch_report_data", text)
        self.assertIn("source_trade_count", text)
        self.assertIn("source_position_count", text)
        self.assertIn("weekly report has no trade outcomes", text)
        self.assertIn('("symbol", "quantity", "mark_price", "updated_at")', text)
        self.assertLess(report_check, final_node_a)
        self.assertLess(final_node_a, final_node_b)
        self.assertLess(final_node_b, success)
        self.assertIn("Trading remains HALTED", text)

    def test_errors_and_signals_stop_both_nodes(self):
        text = self.script_text()

        self.assertIn("trap on_error ERR", text)
        self.assertIn("trap 'on_signal 129 HUP' HUP", text)
        self.assertIn("trap 'on_signal 130 INT' INT", text)
        self.assertIn("trap 'on_signal 143 TERM' TERM", text)
        on_error_start = text.index("on_error() {")
        on_error_end = text.index("\n}", on_error_start)
        on_error = text[on_error_start:on_error_end]
        stop_command = 'docker stop -t 20 "$NODE_A" "$NODE_B"'
        if stop_command not in on_error:
            self.assertIn("fail_closed_cleanup", on_error)
            cleanup_start = text.index("fail_closed_cleanup() {")
            cleanup_end = text.index("\n}", cleanup_start)
            cleanup = text[cleanup_start:cleanup_end]
            self.assertIn(stop_command, cleanup)
        self.assertIn("trap - ERR", cleanup)
        self.assertIn(
            "systemctl stop trader-v3-trade-outcomes.service",
            cleanup,
        )
        self.assertIn(
            "flock -n /var/lock/trader-v3-trade-outcomes.lock",
            cleanup,
        )
        self.assertIn("docker inspect --format '{{.State.Running}}'", cleanup)
        self.assertIn("CLEANUP_CONFIRMED=1", cleanup)

    def test_fail_closed_cleanup_confirms_stopped_state(self):
        result, log = self.run_fail_closed_cleanup(
            flock_ok=True,
            docker_running="false",
        )
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("status=0 confirmed=1", output)
        self.assertIn(
            "systemctl stop trader-v3-trade-outcomes.service",
            log,
        )
        self.assertIn("flock -n /var/lock/trader-v3-trade-outcomes.lock", log)
        self.assertIn("docker stop -t 20 node-a node-b", log)

    def test_fail_closed_cleanup_rejects_unconfirmed_state(self):
        result, _ = self.run_fail_closed_cleanup(
            flock_ok=False,
            docker_running="true",
            systemctl_ok=False,
        )
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("status=1 confirmed=0", output)
        self.assertIn("trade outcomes lock remains held", output)
        self.assertIn("node-a is not confirmed stopped", output)
        self.assertIn("node-b is not confirmed stopped", output)
        self.assertIn("FAIL-CLOSED CLEANUP INCOMPLETE", output)


if __name__ == "__main__":
    unittest.main()
