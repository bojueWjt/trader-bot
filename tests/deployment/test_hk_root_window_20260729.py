import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "hk-root-window-20260729.sh"
ROLLBACK = REPO_ROOT / "scripts" / "hk-rollback-20260729.sh"


class RootWindow20260729Test(unittest.TestCase):
    def run_controlplane_readiness(self, curl_failures):
        text = DEPLOY.read_text(encoding="utf-8")
        function_start = text.index("wait_for_controlplane_http() {")
        function_end = text.index("\n}\n\n[ \"$(id -u)\"", function_start) + 3
        function_text = text[function_start:function_end]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            counter_path = temp_path / "curl-count"
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "count=0\n"
                "if [ -r \"$CURL_COUNTER\" ]; then\n"
                "  count=$(cat \"$CURL_COUNTER\")\n"
                "fi\n"
                "count=$((count + 1))\n"
                "printf '%s\\n' \"$count\" >\"$CURL_COUNTER\"\n"
                "if [ \"$count\" -le \"$CURL_FAILURES\" ]; then\n"
                "  echo 'connection refused' >&2\n"
                "  exit 7\n"
                "fi\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            harness = temp_path / "readiness-harness.sh"
            harness.write_text(
                "#!/usr/bin/env bash\n"
                "sleep() {\n"
                "  :\n"
                "}\n"
                "die() {\n"
                "  echo \"FATAL: $*\" >&2\n"
                "  return 91\n"
                "}\n"
                f"{function_text}\n"
                "wait_for_controlplane_http\n"
                "exit $?\n",
                encoding="utf-8",
            )
            harness.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["CURL_COUNTER"] = str(counter_path)
            env["CURL_FAILURES"] = str(curl_failures)
            result = subprocess.run(
                ["bash", str(harness)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            attempts = 0
            if counter_path.exists():
                attempts = int(counter_path.read_text(encoding="utf-8"))
            return result, attempts

    def test_deploy_applies_only_migration_0009(self):
        text = DEPLOY.read_text(encoding="utf-8")

        self.assertIn("apply_migration_0009", text)
        self.assertIn("0009_trade_outcome_job_runs.up.sql", text)
        self.assertNotIn('migrate.py" up', text)

    def test_deploy_restarts_existing_containers_without_recreating_them(self):
        text = DEPLOY.read_text(encoding="utf-8")

        self.assertIn('docker stop -t 30 "$NODE_A" "$NODE_B"', text)
        self.assertIn('docker start "$NODE_A"', text)
        self.assertIn('docker start "$NODE_B"', text)
        self.assertNotIn('bash "$T/recreate-$NODE_A.sh"', text)
        self.assertNotIn('bash "$T/recreate-$NODE_B.sh"', text)
        self.assertIn("halt_nodes_for_deploy", text)
        self.assertIn("HALT acknowledged by", text)

    def test_mount_verification_uses_mountinfo_root_and_mountpoint(self):
        text = DEPLOY.read_text(encoding="utf-8")

        self.assertIn(
            "pairs.append((left_fields[3], left_fields[4]))",
            text,
        )
        self.assertNotIn(
            "pairs.append((right_fields[1], left_fields[4]))",
            text,
        )

    def test_timer_starts_after_manual_watermark_acceptance(self):
        text = DEPLOY.read_text(encoding="utf-8")

        cron_removal = text.index(
            "rm -f /etc/cron.d/trader-v3-trade-outcomes"
        )
        old_timer_stop = text.index(
            "systemctl stop trader-v3-trade-outcomes.timer"
        )
        manual_start = text.index(
            "systemctl start trader-v3-trade-outcomes.service"
        )
        watermark = text.index("database_marker verify-watermark")
        stamp = text.index(
            "touch /var/lib/systemd/timers/"
            "stamp-trader-v3-trade-outcomes.timer"
        )
        timer_start = text.index(
            "systemctl start trader-v3-trade-outcomes.timer"
        )
        self.assertLess(old_timer_stop, manual_start)
        self.assertLess(cron_removal, manual_start)
        self.assertLess(manual_start, watermark)
        self.assertLess(watermark, stamp)
        self.assertLess(stamp, timer_start)
        self.assertNotIn(
            "systemctl enable --now trader-v3-trade-outcomes.timer",
            text,
        )
        self.assertIn(
            'database_marker verify-watermark "$OUTCOME_RUN_STARTED_AT"',
            text,
        )
        backup_start = text.index(
            "exec pg_dump -Fc"
        )
        self.assertLess(old_timer_stop, backup_start)

    def test_outcome_service_uses_dedicated_environment_file(self):
        text = DEPLOY.read_text(encoding="utf-8")

        self.assertIn("/etc/trader-v3/trade-outcomes.env", text)
        self.assertIn("DATABASE_URL contains a newline", text)
        self.assertNotIn(
            'docker exec \\\n  -e DATABASE_URL="$DATABASE_URL"',
            text,
        )

    def test_backup_path_cannot_be_reused(self):
        text = DEPLOY.read_text(encoding="utf-8")

        self.assertIn(
            '[ ! -e "$BACKUP_ROOT" ] '
            '|| die "backup path already exists: $BACKUP_ROOT"',
            text,
        )
        self.assertIn("BACKUP_SHA256SUMS", text)

    def test_deploy_signals_fail_closed_with_fixed_statuses(self):
        text = DEPLOY.read_text(encoding="utf-8")

        self.assertIn("trap 'on_signal 129 HUP' HUP", text)
        self.assertIn("trap 'on_signal 130 INT' INT", text)
        self.assertIn("trap 'on_signal 143 TERM' TERM", text)

    def test_controlplane_readiness_is_bounded_and_precedes_node_start(self):
        text = DEPLOY.read_text(encoding="utf-8")

        self.assertIn("wait_for_controlplane_http() {", text)
        self.assertIn("local max_attempts=30", text)
        self.assertIn("--connect-timeout 1", text)
        self.assertIn("--max-time 5", text)
        readiness = text.rindex("wait_for_controlplane_http")
        node_a_start = text.index('docker start "$NODE_A"')
        self.assertLess(readiness, node_a_start)

    def test_controlplane_readiness_retries_until_http_succeeds(self):
        result, attempts = self.run_controlplane_readiness(curl_failures=2)
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(attempts, 3)
        self.assertIn("attempt 3/30", output)

    def test_controlplane_readiness_fails_after_attempt_limit(self):
        result, attempts = self.run_controlplane_readiness(curl_failures=100)
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 91, output)
        self.assertEqual(attempts, 30)
        self.assertIn(
            "control plane HTTP did not become ready after 30 attempts",
            output,
        )

    def test_rollback_restarts_existing_containers(self):
        text = ROLLBACK.read_text(encoding="utf-8")

        self.assertIn('docker stop -t 30 "$NODE_A" "$NODE_B"', text)
        self.assertIn('docker start "$NODE_A"', text)
        self.assertIn('docker start "$NODE_B"', text)
        self.assertNotIn("hk-gen-recreate-patched.py", text)
        self.assertIn("trap on_error ERR", text)
        self.assertIn("trap 'on_signal 129 HUP' HUP", text)
        self.assertIn("trap 'on_signal 130 INT' INT", text)
        self.assertIn("trap 'on_signal 143 TERM' TERM", text)
        self.assertIn("TRUNCATE TABLE public.trade_outcomes", text)
        self.assertIn("pg_restore --list", DEPLOY.read_text(encoding="utf-8"))
        checksum_check = text.index(
            "(cd \"$BACKUP_ROOT\" && sha256sum -c BACKUP_SHA256SUMS)"
        )
        mutation_start = text.index("MUTATION_STARTED=1")
        self.assertLess(checksum_check, mutation_start)


if __name__ == "__main__":
    unittest.main()
