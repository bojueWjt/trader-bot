from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import textwrap

import pytest

import watcher_config_snapshot as snapshot
from test_watcher_config_snapshot import Clock, payload


_TOKEN = "fake-token-must-not-appear"
_PRIVATE_VALUES = ("private-account-sentinel", "private-channel-sentinel", "0.073918")


def _private_payload(revision=1):
    raw = payload(revision)
    raw["accounts"][0]["account_id"] = _PRIVATE_VALUES[0]
    raw["channels"][0].update(
        channel_id=_PRIVATE_VALUES[1], target_account_id=_PRIVATE_VALUES[0],
    )
    raw["risks"][0]["risk_ratio"] = _PRIVATE_VALUES[2]
    body = {key: raw[key] for key in ("schema_version", "accounts", "channels", "risks")}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    raw["content_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    return raw


def _assert_private(lines):
    output = "\n".join(lines)
    for value in (_TOKEN, *_PRIVATE_VALUES, "accounts", "channels", "risk_ratio"):
        assert value not in output


def _messages(caplog):
    return [record.getMessage() for record in caplog.records
            if record.name == snapshot.__name__]


def test_warmup_success_logs_verified_metadata_once(caplog):
    clock = Clock()
    raw = _private_payload(7)
    subject = snapshot.SnapshotCache(fetch=lambda *_: (200, raw), clock=clock, token=_TOKEN)
    with caplog.at_level(logging.INFO, logger=snapshot.__name__):
        subject.start()
        subject.start()
        subject.stop()
    warmups = [line for line in _messages(caplog) if "snapshot_warmup" in line]
    assert len(warmups) == 1
    assert "result=success" in warmups[0]
    assert "revision=7" in warmups[0]
    assert f"content_sha256={raw['content_sha256'][:12]} " in warmups[0]
    assert "pid=" in warmups[0]
    assert "role=operator-query" in warmups[0]
    assert "duration_ms=" in warmups[0]
    _assert_private(_messages(caplog))


def test_warmup_failure_is_cold_and_never_logs_token(caplog):
    secret = _TOKEN
    subject = snapshot.SnapshotCache(
        fetch=lambda *_: (401, {"token": secret}), clock=Clock(), token=secret,
    )
    with caplog.at_level(logging.INFO, logger=snapshot.__name__):
        subject.start()
        subject.stop()
    lines = _messages(caplog)
    assert len([line for line in lines if "snapshot_warmup" in line]) == 1
    assert any("snapshot_warmup result=cold" in line for line in lines)
    assert secret not in "\n".join(lines)
    assert "'token'" not in "\n".join(lines)
    _assert_private(lines)


def test_state_transition_expiry_unauthorized_recovery_revision_and_dedup(caplog):
    clock = Clock()
    responses = [(200, _private_payload(1)), (200, _private_payload(1)), (401, {}),
                 (200, _private_payload(2)), (200, _private_payload(2)), (200, _private_payload(3))]
    subject = snapshot.SnapshotCache(
        fetch=lambda *_: responses.pop(0), clock=clock, token=_TOKEN,
    )
    with caplog.at_level(logging.INFO, logger=snapshot.__name__):
        assert subject.state == "cold"
        assert subject.refresh()
        assert subject.refresh()
        clock.now = 61
        assert subject.state == "expired"
        assert subject.state == "expired"
        assert not subject.refresh()
        assert subject.state == "unauthorized"
        assert subject.refresh()
        assert subject.refresh()
        assert subject.refresh()
    changes = [line for line in _messages(caplog) if "snapshot_state_transition" in line]
    assert len(changes) == 5
    assert "from=cold to=fresh" in changes[0]
    assert "from=fresh to=expired" in changes[1]
    assert "reason=max_age_exceeded" in changes[1]
    assert "age_ms=61000" in changes[1]
    assert "from=expired to=unauthorized" in changes[2]
    assert "reason=unauthorized" in changes[2]
    assert "from=unauthorized to=fresh" in changes[3]
    assert "revision=2" in changes[3]
    assert "from=fresh to=fresh" in changes[4]
    assert "revision=3" in changes[4]
    _assert_private(_messages(caplog))


@pytest.mark.parametrize("error", ["timeout", "unavailable"])
def test_refresh_failure_logs_expiry_without_state_read(caplog, error):
    clock = Clock()
    raw = _private_payload()

    def fetch(*_):
        if clock.now == 61:
            if error == "timeout":
                raise TimeoutError(_TOKEN)
            raise OSError(_TOKEN)
        return 200, raw

    subject = snapshot.SnapshotCache(fetch=fetch, clock=clock, token=_TOKEN)
    with caplog.at_level(logging.INFO, logger=snapshot.__name__):
        assert subject.refresh()
        clock.now = 61
        assert not subject.refresh()
        assert not subject.refresh()
        # Never read .state: only the background refresh path may emit expiry.
        changes = _messages(caplog)
        assert len(changes) == 2
        assert "from=fresh to=expired reason=max_age_exceeded" in changes[1]
        assert "age_ms=61000" in changes[1]
        assert f"last_error={error}" in changes[1]
        clock.now = 62
        assert subject.refresh()
    assert "from=expired to=fresh" in _messages(caplog)[-1]
    assert "last_error=None" in _messages(caplog)[-1]
    _assert_private(_messages(caplog))


@pytest.mark.parametrize("handler_location", ["none", "module", "root", "blocked_root", "null"])
def test_start_emits_stderr_under_uvicorn_defaults(capfd, handler_location):
    # Isolate dictConfig (which closes existing handlers) from pytest logging.
    # The child's inherited file descriptors are captured by capfd.
    script = f"sys.path[:0] = {sys.path!r}\n" + textwrap.dedent('''
        import logging
        import logging.config
        import os
        from copy import deepcopy
        import watcher_config_snapshot as snapshot
        from test_watcher_config_snapshot import Clock
        from test_watcher_config_snapshot_logging import _private_payload, _TOKEN

        try:
            from uvicorn.config import LOGGING_CONFIG
        except ImportError:
            # Uvicorn default handler/logger topology with stdlib formatters.
            LOGGING_CONFIG = {
                "version": 1, "disable_existing_loggers": False,
                "handlers": {
                    "default": {"class": "logging.StreamHandler", "stream": "ext://sys.stderr"},
                    "access": {"class": "logging.StreamHandler", "stream": "ext://sys.stdout"},
                },
                "loggers": {
                    "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
                    "uvicorn.error": {"level": "INFO"},
                    "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
                },
            }
        logging.config.dictConfig(deepcopy(LOGGING_CONFIG))
        root = logging.getLogger()
        logger = logging.getLogger(snapshot.__name__)
        assert root.level == logging.WARNING and not root.handlers
        assert logger.getEffectiveLevel() == logging.WARNING and not logger.handlers
        location = sys.argv[1]
        existing = None
        if location in {"module", "root", "blocked_root"}:
            existing = logging.StreamHandler()
            target = logger if location == "module" else root
            target.addHandler(existing)
        elif location == "null":
            logger.addHandler(logging.NullHandler())
        if location == "blocked_root":
            logger.propagate = False

        clock = Clock()
        raw = _private_payload(7)
        def fetch(*_):
            if clock.now > 60:
                raise TimeoutError(_TOKEN)
            return 200, raw
        subject = snapshot.SnapshotCache(fetch=fetch, clock=clock, token=_TOKEN)
        snapshot.get_process_cache = lambda: subject
        os.environ["WATCHER_CONFIG_SNAPSHOT_ENABLED"] = "1"
        try:
            snapshot.start_if_enabled()
            handlers = list(logger.handlers)
            snapshot.start_if_enabled()
            assert logger.handlers == handlers
            if location == "module":
                assert handlers == [existing]
            elif location == "root":
                assert handlers == []
            else:
                assert logger.propagate is False
                assert len([h for h in handlers if isinstance(h, logging.StreamHandler)]) == 1
            clock.now = 61
            assert not subject.refresh()
        finally:
            subject.stop()
    ''')
    subprocess.run([sys.executable, "-c", "import sys\n" + script, handler_location], check=True)
    captured = capfd.readouterr()
    lines = captured.err.splitlines()
    assert captured.out == ""
    assert len(lines) == 3
    assert "snapshot_state_transition from=cold to=fresh" in lines[0]
    assert "snapshot_warmup result=success revision=7" in lines[1]
    assert f"content_sha256={_private_payload(7)['content_sha256'][:12]} " in lines[1]
    assert "pid=" in lines[1]
    assert "from=fresh to=expired reason=max_age_exceeded" in lines[2]
    assert "last_error=timeout" in lines[2]
    _assert_private(lines)
    # pytest -rP exposes the real stderr evidence after assertions.
    print(captured.err, end="")
