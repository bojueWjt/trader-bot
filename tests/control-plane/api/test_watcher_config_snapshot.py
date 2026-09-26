from __future__ import annotations

import hashlib
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import read_api
import watcher_config_snapshot as snapshot


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def payload(revision=1):
    body = {
        "schema_version": "watcher-config-snapshot.v1",
        "accounts": [{
            "account_id": "source-a", "kind": "main", "parent_account_id": None,
            "execution_account_id": "account-a", "enabled": True,
            "risk_capital_addon": "25", "default_risk": "0.01",
        }],
        "channels": [{"channel_id": "123", "target_account_id": "source-a"}],
        "risks": [{"symbol": "BTCUSDT", "risk_ratio": "0.02"}],
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        **body, "revision": revision,
        "content_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "generated_at": "2026-09-26T08:00:00.000Z",
    }


def cache(clock, fetch):
    return snapshot.SnapshotCache(fetch=fetch, clock=clock, token="fake-token")


def test_t0_3_cold_fresh_boundary_expired_and_recovery():
    clock = Clock()
    responses = [(200, payload())]
    subject = cache(clock, lambda *_: responses.pop(0))
    assert subject.state == "cold"
    assert subject.refresh() is True
    assert subject.state == "fresh"
    lease = subject.require_fresh()
    assert lease.channel_addon("123", "account-a") == 25.0
    assert lease.risk_ratio("BTCUSDT", "account-a") == 0.02
    assert lease.check["revision"] == 1
    clock.now = 59
    assert subject.state == "fresh"
    clock.now = 60
    assert subject.state == "fresh"
    clock.now = 61
    assert subject.state == "expired"
    with pytest.raises(snapshot.SnapshotUnavailable):
        subject.require_fresh()
    responses.append((200, payload(2)))
    assert subject.refresh() is True
    assert subject.state == "fresh"
    assert subject.require_fresh().check["revision"] == 2
    assert subject.next_refresh_at == clock.now + 30


@pytest.mark.parametrize("bad_response,error", [
    ((401, {}), "unauthorized"),
    ((200, {**payload(), "schema_version": "wrong"}), "schema_mismatch"),
    ((200, {**payload(), "content_sha256": "0" * 64}), "invalid"),
    ((200, {**payload(), "api_secret": "leak"}), "invalid"),
])
def test_t0_3_invalid_and_401_immediately_close_gate(bad_response, error):
    clock = Clock()
    responses = [(200, payload()), bad_response, (200, payload(2))]
    subject = cache(clock, lambda *_: responses.pop(0))
    subject.refresh()
    clock.now = 1
    subject.refresh()
    assert subject.last_refresh_error == error
    assert subject.state in {"invalid", "unauthorized"}
    with pytest.raises(snapshot.SnapshotUnavailable):
        subject.require_fresh()
    assert subject.refresh() is True
    assert subject.state == "fresh"


def test_t0_3_timeout_backoff_and_old_snapshot_age():
    clock = Clock()
    calls = 0

    def fetch(*_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 200, payload()
        raise TimeoutError()

    subject = cache(clock, fetch)
    subject.refresh()
    for expected in (1, 2, 4, 8, 15, 15):
        subject.refresh()
        assert subject.next_refresh_at - clock.now == expected
    assert subject.state == "fresh"
    clock.now = 61
    assert subject.state == "expired"


def test_t0_3_reader_uses_two_second_timeout_and_bearer_token(monkeypatch):
    seen = []
    monkeypatch.setenv("WATCHER_SNAPSHOT_URL", "http://127.0.0.1:9090/api/trading/config-snapshot")
    monkeypatch.setenv("WATCHER_SNAPSHOT_TOKEN", "  fake-token  ")

    def fetch(url, token, timeout):
        seen.append((url, token, timeout))
        return 200, payload()

    subject = snapshot.SnapshotCache(fetch=fetch, clock=Clock())
    assert subject.refresh() is True
    assert seen == [(
        "http://127.0.0.1:9090/api/trading/config-snapshot", "fake-token", 2.0,
    )]


def test_t0_3_single_flight_and_worker_independence():
    clock = Clock()
    entered = threading.Event()
    release = threading.Event()

    def fetch(*_):
        entered.set()
        assert release.wait(2)
        return 200, payload()

    first = cache(clock, fetch)
    second = cache(clock, lambda *_: (200, payload(2)))
    worker = threading.Thread(target=first.refresh)
    worker.start()
    assert entered.wait(2)
    assert first.refresh() is False
    assert second.state == "cold"
    assert second.refresh() is True
    release.set()
    worker.join(2)
    assert first.state == "fresh"
    assert second.require_fresh().check["revision"] == 2


def test_t0_3_invalid_gate_stays_closed_through_network_failure():
    clock = Clock()
    responses = [(200, payload()), (401, {})]

    def fetch(*_):
        if responses:
            return responses.pop(0)
        raise TimeoutError()

    subject = cache(clock, fetch)
    subject.refresh()
    subject.refresh()
    assert subject.state == "unauthorized"
    subject.refresh()
    assert subject.last_refresh_error == "timeout"
    assert subject.state == "unauthorized"


def test_t0_3_same_revision_changed_content_is_invalid():
    clock = Clock()
    changed = payload()
    changed["accounts"][0]["risk_capital_addon"] = "26"
    canonical = {field: changed[field] for field in ("schema_version", "accounts", "channels", "risks")}
    changed["content_sha256"] = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    responses = [(200, payload()), (200, changed)]
    subject = cache(clock, lambda *_: responses.pop(0))
    subject.refresh()
    subject.refresh()
    assert subject.state == "invalid"
    with pytest.raises(snapshot.SnapshotUnavailable):
        subject.require_fresh()


def test_t0_3_lease_pins_one_immutable_revision():
    clock = Clock()
    next_payload = payload(2)
    next_payload["accounts"][0]["risk_capital_addon"] = "50"
    next_payload["risks"][0]["risk_ratio"] = "0.03"
    canonical = {field: next_payload[field] for field in ("schema_version", "accounts", "channels", "risks")}
    next_payload["content_sha256"] = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    responses = [(200, payload()), (200, next_payload)]
    subject = cache(clock, lambda *_: responses.pop(0))
    subject.refresh()
    lease = subject.require_fresh()
    subject.refresh()
    assert lease.check["revision"] == 1
    assert lease.channel_addon("123", "account-a") == 25
    assert lease.risk_ratio("BTCUSDT", "account-a") == 0.02
    current = subject.require_fresh()
    assert current.check["revision"] == 2
    assert current.channel_addon("123", "account-a") == 50
    assert current.risk_ratio("BTCUSDT", "account-a") == 0.03
    with pytest.raises(TypeError):
        lease.snapshot.channels["123"] = "changed"


def test_t0_3_missing_risk_does_not_read_mutable_environment(monkeypatch):
    raw = payload()
    raw["accounts"][0]["default_risk"] = None
    raw["risks"] = []
    canonical = {field: raw[field] for field in ("schema_version", "accounts", "channels", "risks")}
    raw["content_sha256"] = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    subject = cache(Clock(), lambda *_: (200, raw))
    subject.refresh()
    monkeypatch.setenv("OPERATOR_DEFAULT_RISK_RATIO", "0.05")
    with pytest.raises(snapshot.SnapshotUnavailable):
        subject.require_fresh().risk_ratio("BTCUSDT", "account-a")


def test_t0_3_background_retries_without_request():
    recovered = threading.Event()
    calls = 0

    def fetch(*_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError()
        recovered.set()
        return 200, payload()

    subject = snapshot.SnapshotCache(fetch=fetch, token="fake-token")
    try:
        subject.start()
        assert subject.state == "cold"
        assert recovered.wait(2.5)
        assert subject.state == "fresh"
    finally:
        subject.stop()


@pytest.mark.parametrize("action", [
    "close_position", "partial_close", "move_stop_loss", "replace_take_profits",
])
def test_t0_3_non_entry_actions_never_read_snapshot(monkeypatch, action):
    class StopAtCaps(Exception):
        pass

    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    monkeypatch.setattr(read_api, "_require_operator_principal", lambda *_args, **_kwargs: SimpleNamespace(
        kind=read_api.PrincipalKind.OPERATOR, actor_id="risk_admin",
    ))
    monkeypatch.setattr(read_api, "_operator_accounts", lambda: ("account-a",))
    monkeypatch.setattr(read_api, "_operator_caps", lambda: (_ for _ in ()).throw(StopAtCaps()))
    monkeypatch.setattr(snapshot, "get_process_cache", lambda: (_ for _ in ()).throw(
        AssertionError("non-entry action must not read snapshot")
    ))
    with pytest.raises(StopAtCaps):
        read_api.operator_order({
            "action": action, "account_id": "account-a", "symbol": "BTCUSDT",
            "reason": "snapshot independence", "dry_run": True,
        })


def test_t0_3_system_query_has_no_snapshot_dependency(monkeypatch):
    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(read_api, "require_reader", lambda *_: "viewer")
    monkeypatch.setattr(snapshot, "get_process_cache", lambda: (_ for _ in ()).throw(
        AssertionError("system query must not read watcher snapshot")
    ))
    with pytest.raises(HTTPException) as exc:
        read_api.system_snapshot("Bearer fake-reader")
    assert exc.value.detail == "projection store unavailable"


def test_t0_3_entry_rejects_cold_snapshot_without_sqlite(monkeypatch):
    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    monkeypatch.setattr(read_api, "_require_operator_principal", lambda *_args, **_kwargs: SimpleNamespace(
        kind=read_api.PrincipalKind.OPERATOR, actor_id="risk_admin",
    ))
    monkeypatch.setattr(read_api, "_operator_accounts", lambda: ("account-a",))
    subject = cache(Clock(), lambda *_: (200, payload()))
    monkeypatch.setattr(snapshot, "get_process_cache", lambda: subject)
    monkeypatch.setattr(read_api, "_account_risk_capital_addon", lambda *_: (_ for _ in ()).throw(
        AssertionError("snapshot-enabled order must not read sqlite")
    ))
    with pytest.raises(HTTPException) as exc:
        read_api.operator_order({
            "action": "open_position", "account_id": "account-a", "symbol": "BTCUSDT",
            "reason": "snapshot independence", "dry_run": True,
        })
    assert exc.value.status_code == 503
    assert exc.value.detail == "snapshot_unavailable"


def test_t0_3_switch_off_keeps_legacy_addon_call(monkeypatch):
    class LegacyCall(Exception):
        pass

    monkeypatch.delenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", raising=False)
    monkeypatch.setattr(read_api, "_require_operator_principal", lambda *_args, **_kwargs: SimpleNamespace(
        kind=read_api.PrincipalKind.OPERATOR, actor_id="risk_admin",
    ))
    monkeypatch.setattr(read_api, "_operator_accounts", lambda: ("account-a",))
    monkeypatch.setattr(read_api, "_account_risk_capital_addon", lambda *_: (_ for _ in ()).throw(LegacyCall()))
    monkeypatch.setattr(snapshot, "get_process_cache", lambda: (_ for _ in ()).throw(
        AssertionError("disabled snapshot switch must not access cache")
    ))
    with pytest.raises(LegacyCall):
        read_api.operator_order({
            "action": "open_position", "account_id": "account-a", "symbol": "BTCUSDT",
            "reason": "legacy path", "dry_run": True,
        })


def test_t0_3_enabled_readers_use_one_lease_without_sqlite(monkeypatch, tmp_path):
    clock = Clock()
    subject = cache(clock, lambda *_: (200, payload()))
    subject.refresh()
    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    monkeypatch.setattr(snapshot, "get_process_cache", lambda: subject)
    monkeypatch.setattr(read_api, "_WATCHER_TRADING_DB", str(tmp_path / "missing.db"))
    assert read_api._load_channel_risk_route("123", "account-a") == {
        "execution_account_id": "account-a", "risk_capital_addon": 25.0,
    }
    assert read_api._account_risk_capital_addon("account-a") == 25.0
    assert read_api._symbol_risk_ratio("BTCUSDT", "account-a") == 0.02


def test_t0_3_order_sizing_uses_pinned_snapshot_risk(monkeypatch):
    clock = Clock()
    subject = cache(clock, lambda *_: (200, payload()))
    subject.refresh()
    lease = subject.require_fresh()
    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    monkeypatch.setattr(snapshot, "get_process_cache", lambda: (_ for _ in ()).throw(
        AssertionError("sizing must use the order's lease")
    ))
    monkeypatch.setattr(read_api, "_account_financial_state", lambda *_: {
        "real_equity": 1000.0, "available_balance": 1000.0,
    })
    checks = [lease.check]
    notional = read_api._size_open_order(
        None, "BTCUSDT", "account-a", "long", "limit", 100.0, None, None,
        95.0, 10.0,
        {"max_notional": None, "max_leverage": 10.0, "max_risk_fraction": 0.06,
         "no_sl_equity_fraction": 0.2},
        checks, lease.account_addon("account-a"), snapshot_lease=lease,
    )
    assert notional > 0
    assert next(item for item in checks if item["name"] == "risk_sizing")["risk_ratio"] == 0.02
    assert checks[0]["revision"] == 1
