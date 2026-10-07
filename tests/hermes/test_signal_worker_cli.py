"""CLI and deployment-template dry checks: no database, HTTP or systemd writes."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import worker
from hermes_client import HermesRequest, RealHermesClient
from prompt import PROMPT_VERSION, SIGNAL_PROMPT_VERSION


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cli_env(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("SIGNAL_TOKEN_", "TELEGRAM_", "TG_")):
            monkeypatch.delenv(key)
    for key, value in {
        "DATABASE_URL": "postgresql://unused-for-dry-check",
        "HERMES_API_URL": "http://invalid.example/v1",
        "HERMES_API_KEY": "test-model-key",
        "HERMES_MODEL": "pinned-test-model",
        "SIGNAL_OPERATOR_URL": "http://invalid.example/v1/operator/orders",
        "SIGNAL_TOKEN_ACCOUNT_A": "test-account-a",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("args", [
    ["--mode", "signal", "--once", "--projection-adapters"],
    ["--mode", "signal", "--account-id", "account-a", "--once", "--fixture-snapshot-json", "unused"],
])
def test_signal_cli_requires_account_and_real_projection(args):
    with pytest.raises(SystemExit) as error:
        worker.main(args)
    assert error.value.code == 2


@pytest.mark.parametrize("forbidden", ["SIGNAL_TOKEN_ACCOUNT_B", "TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN"])
def test_signal_cli_rejects_other_account_or_telegram_credentials(cli_env, monkeypatch, forbidden):
    monkeypatch.setenv(forbidden, "must-not-enter-worker")
    with pytest.raises(SystemExit) as error:
        worker.main(["--mode", "signal", "--account-id", "account-a", "--once", "--projection-adapters"])
    assert error.value.code == 2


def test_signal_cli_missing_own_token_never_connects_database(cli_env, monkeypatch):
    monkeypatch.delenv("SIGNAL_TOKEN_ACCOUNT_A")
    monkeypatch.setattr("psycopg2.connect", lambda *_a, **_k: pytest.fail("must fail before DB"))
    with pytest.raises(ValueError, match="SIGNAL_TOKEN_ACCOUNT_A"):
        worker.main(["--mode", "signal", "--account-id", "account-a", "--once", "--projection-adapters"])


@pytest.mark.parametrize("loop,statuses", [
    (False, ["dispatched"]),
    (True, ["dispatched", "skipped"]),
    (True, ["reconciling"]),
])
def test_signal_cli_account_scoped_once_and_bounded_loop(cli_env, monkeypatch, loop, statuses):
    calls = []
    closed = []

    class Connection:
        def close(self):
            closed.append(True)

    monkeypatch.setattr("psycopg2.connect", lambda *_a, **_k: Connection())

    def process(conn, **kwargs):
        calls.append(kwargs)
        assert len(calls) <= len(statuses), "signal loop must stop instead of spinning"
        return worker.WorkerResult(status=statuses[len(calls) - 1])

    monkeypatch.setattr(worker, "process_signal_one", process)
    result = worker.main(["--mode", "signal", "--account-id", "account-a",
                          "--loop" if loop else "--once", "--projection-adapters",
                          "--media-root", "/example/media", "--worker-id", "hermes-signal"])
    assert result == 0
    assert len(calls) == len(statuses)
    assert calls[0]["account_id"] == "account-a"
    assert calls[0]["worker_id"] == "hermes-signal:account-a"
    assert calls[0]["model_version"] == "pinned-test-model"
    assert closed == [True]


def test_cli_default_remains_shadow(cli_env, monkeypatch):
    class Connection:
        def close(self):
            pass

    calls = []
    monkeypatch.setattr("psycopg2.connect", lambda *_a, **_k: Connection())
    monkeypatch.setattr(worker, "run_shadow_consumer", lambda conn, **kw: calls.append(kw) or worker.WorkerResult(status="skipped"))
    monkeypatch.setattr(worker, "process_signal_one", lambda *_a, **_k: pytest.fail("default must remain shadow"))
    assert worker.main(["--once", "--projection-adapters"]) == 0
    assert calls[0]["worker_id"] == "shadow-worker"


@pytest.mark.parametrize("signal", [False, True])
def test_real_client_metadata_uses_actual_prompt_version(monkeypatch, signal):
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

    def urlopen(request, **kwargs):
        captured.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    request = HermesRequest(raw_message_id="test", text="test",
                            system_snapshot={"execution_account_id": "account-a"} if signal else None)
    RealHermesClient(api_url="http://invalid.example/v1", api_key="test", model="pinned").analyze(request, timeout=1)
    assert captured[0]["metadata"]["prompt_version"] == (SIGNAL_PROMPT_VERSION if signal else PROMPT_VERSION)


def test_systemd_template_execstart_flags_match_real_help_without_starting_service():
    path = ROOT / "infra/systemd/trader-v3-signal-worker@.service"
    text = path.read_text()
    command = next(line.removeprefix("ExecStart=") for line in text.splitlines() if line.startswith("ExecStart="))
    argv = shlex.split(command.replace("%i", "account-a").replace("${HERMES_MEDIA_ROOT}", "/example/media"))
    assert argv[:2] == ["/srv/trader-v3/.venv-cp/bin/python", "/srv/trader-v3/services/hermes-worker/worker.py"]
    dry = subprocess.run([sys.executable, str(ROOT / "services/hermes-worker/worker.py"), *argv[2:], "--help"],
                         text=True, capture_output=True, check=False)
    assert dry.returncode == 0, dry.stderr
    assert "--mode {shadow,signal}" in dry.stdout
    for expected in (
        "EnvironmentFile=/srv/trader-v3/secrets/signal-workers/%i.env",
        "Environment=HERMES_HOME=/srv/hermes/profiles/trader-%i",
        "Environment=HERMES_PROFILE=trader-%i", "User=trader-signal-%i",
        "Restart=always", "RestartSec=5", "NoNewPrivileges=true", "PrivateTmp=true",
        "UnsetEnvironment=RISK_ADMIN_TOKEN",
    ):
        assert expected in text
    assert ".env.v3" not in text
    assert "docker.sock" not in text
