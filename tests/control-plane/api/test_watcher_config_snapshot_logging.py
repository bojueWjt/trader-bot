from __future__ import annotations

import logging

import watcher_config_snapshot as snapshot
from test_watcher_config_snapshot import Clock, payload


def _messages(caplog):
    return [record.getMessage() for record in caplog.records
            if record.name == snapshot.__name__]


def test_warmup_success_logs_verified_metadata_once(caplog):
    clock = Clock()
    raw = payload(7)
    subject = snapshot.SnapshotCache(fetch=lambda *_: (200, raw), clock=clock, token="fake-token")
    with caplog.at_level(logging.INFO, logger=snapshot.__name__):
        subject.start()
        subject.start()
        subject.stop()
    warmups = [line for line in _messages(caplog) if "snapshot_warmup" in line]
    assert len(warmups) == 1
    assert "result=success" in warmups[0]
    assert "revision=7" in warmups[0]
    assert f"content_sha256={raw['content_sha256'][:12]}" in warmups[0]
    assert "pid=" in warmups[0]
    assert "role=operator-query" in warmups[0]
    assert "duration_ms=" in warmups[0]


def test_warmup_failure_is_cold_and_never_logs_token(caplog):
    secret = "fake-token-must-not-appear"
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


def test_state_transition_expiry_unauthorized_recovery_revision_and_dedup(caplog):
    clock = Clock()
    responses = [(200, payload(1)), (200, payload(1)), (401, {}),
                 (200, payload(2)), (200, payload(2)), (200, payload(3))]
    subject = snapshot.SnapshotCache(
        fetch=lambda *_: responses.pop(0), clock=clock, token="fake-token",
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
