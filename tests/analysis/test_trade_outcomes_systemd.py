from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "infra" / "systemd" / "trader-v3-trade-outcomes.service"
TIMER = ROOT / "infra" / "systemd" / "trader-v3-trade-outcomes.timer"


def test_trade_outcomes_service_uses_environment_secret_and_file_logs():
    text = SERVICE.read_text(encoding="utf-8")

    assert "User=balen" in text
    assert "Group=balen" in text
    assert "EnvironmentFile=/etc/trader-v3/trade-outcomes.env" in text
    assert "scripts/analysis/trade_outcomes.py" in text
    assert "Environment=DATABASE_URL=" not in text
    assert "--db-url" not in text
    assert "--cache-dir /var/tmp/hermes-zone-klines" in text
    assert "--output /var/log/trader-v3/trade-outcomes-latest.json" in text
    assert "StandardOutput=append:/var/log/trader-v3/trade-outcomes.log" in text
    assert "StandardError=append:/var/log/trader-v3/trade-outcomes.log" in text
    assert "/usr/bin/flock -n /var/lock/trader-v3-trade-outcomes.lock" in text
    assert (
        "ExecStartPre=+/usr/bin/install -d -m 0750 /var/log/trader-v3"
        in text
    )
    assert (
        "ExecStartPre=+/usr/bin/install -d -m 0750 "
        "/var/tmp/hermes-zone-klines"
        in text
    )


def test_trade_outcomes_timer_runs_hourly_and_catches_missed_runs():
    text = TIMER.read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* *:30:00 UTC" in text
    assert "00:30:00 UTC" not in text
    assert "Persistent=true" in text
    assert "Unit=trader-v3-trade-outcomes.service" in text
    assert "WantedBy=timers.target" in text
