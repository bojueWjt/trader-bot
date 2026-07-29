from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTINUE = REPO_ROOT / "scripts" / "hk-root-continue-20260729.sh"


class RootContinue20260729Test(unittest.TestCase):
    def script_text(self):
        self.assertTrue(
            CONTINUE.is_file(),
            f"continuation script is missing: {CONTINUE}",
        )
        return CONTINUE.read_text(encoding="utf-8")

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

        marker_match = re.search(
            r"(?m)^(OUTCOME_RUN_STARTED_AT|CONTINUE_STARTED_AT)="
            r'"?\$\(date -u \+%Y-%m-%dT%H:%M:%SZ\)"?$',
            text,
        )
        self.assertIsNotNone(marker_match)
        marker_name = marker_match.group(1)
        run_started = marker_match.start()
        manual_start = text.index(
            "systemctl start trader-v3-trade-outcomes.service"
        )
        watermark = text.index(
            f'database_marker verify-watermark "${marker_name}"'
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


if __name__ == "__main__":
    unittest.main()
