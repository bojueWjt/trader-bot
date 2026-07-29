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

    def test_reuses_existing_containers_without_recreate_commands(self):
        text = self.script_text()

        self.assertNotRegex(
            text,
            re.compile(r"^[ \t]*docker[ \t]+(?:run|create)\b", re.MULTILINE),
        )
        self.assertNotRegex(
            text,
            re.compile(
                r"^[ \t]*docker[ \t]+compose[^\n]*\bup\b",
                re.MULTILINE,
            ),
        )
        self.assertNotIn("--force-recreate", text)
        self.assertNotIn('bash "$T/recreate-', text)
        self.assertNotIn("hk-gen-recreate-patched.py", text)

    def test_rejects_non_stopped_nodes_before_starting_node_a(self):
        text = self.script_text()
        node_a_start = text.index('docker start "$NODE_A"')
        status_check = text.find("State.Status")
        exited_check = text.find("exited")

        self.assertGreaterEqual(status_check, 0)
        self.assertGreaterEqual(exited_check, 0)
        self.assertLess(status_check, node_a_start)
        self.assertLess(exited_check, node_a_start)

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

        run_started = text.index(
            "OUTCOME_RUN_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        )
        manual_start = text.index(
            "systemctl start trader-v3-trade-outcomes.service"
        )
        watermark = text.index(
            'database_marker verify-watermark "$OUTCOME_RUN_STARTED_AT"'
        )
        latest_json = text.index(
            "/var/log/trader-v3/trade-outcomes-latest.json"
        )
        timer_start = text.index(
            "systemctl start trader-v3-trade-outcomes.timer"
        )
        self.assertIn("started_at < required_timestamp", text)
        self.assertIn("completed_at < required_timestamp", text)
        self.assertLess(run_started, manual_start)
        self.assertLess(manual_start, watermark)
        self.assertLess(watermark, latest_json)
        self.assertLess(latest_json, timer_start)
        self.assertNotIn(
            "systemctl enable --now trader-v3-trade-outcomes.timer",
            text,
        )

    def test_checks_report_health_daily_weekly_then_finishes_halted(self):
        text = self.script_text()

        report_health = text.index("http://127.0.0.1:8090/healthz")
        daily = text.index('"daily"')
        weekly = text.index('"weekly"')
        final_node_a = text.rindex("verify_node_halted 8081")
        final_node_b = text.rindex("verify_node_halted 8082")
        success = text.index('echo "DEPLOY CONTINUATION SUCCEEDED"')
        self.assertIn("fetch_report_data", text)
        self.assertLess(report_health, daily)
        self.assertLess(daily, weekly)
        self.assertLess(weekly, final_node_a)
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
        self.assertIn('docker stop -t 20 "$NODE_A" "$NODE_B"', on_error)


if __name__ == "__main__":
    unittest.main()
