from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "hk-root-window-20260729.sh"
ROLLBACK = REPO_ROOT / "scripts" / "hk-rollback-20260729.sh"


class RootWindow20260729Test(unittest.TestCase):
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
