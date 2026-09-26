from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from types import MappingProxyType, SimpleNamespace

import psycopg2
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import operator_queries
import read_api
import watcher_config_snapshot as snapshot
from test_operator_add_position import (
    ACCOUNT_B, NODE_B, RISK_TOKEN, SYMBOL, _activate_redis_epoch,
    _entry_body, _seed_account, _seed_reviewed_rollout,
)
from test_watcher_config_snapshot import Clock, cache, payload


RISK_UNAVAILABLE = "account risk ratio configuration is unavailable or invalid"

BASE_ACCOUNTS = [{
    "account_id": "source-a", "kind": "main", "parent_account_id": None,
    "execution_account_id": "account-a", "enabled": True,
    "risk_capital_addon": "25", "default_risk": "0.01",
}]
BASE_CHANNELS = [{"channel_id": "123", "target_account_id": "source-a"}]
BASE_RISKS = [{"symbol": "BTCUSDT", "risk_ratio": "0.02"}]


def resign(raw, revision=None):
    body = dict(raw)
    if revision is not None:
        body["revision"] = revision
    canonical = {field: body[field] for field in ("schema_version", "accounts", "channels", "risks")}
    body["content_sha256"] = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
    ).hexdigest()
    body.setdefault("generated_at", "2026-09-26T08:00:00.000Z")
    body.setdefault("schema_version", snapshot.SCHEMA_VERSION)
    return body


def signed(accounts=None, channels=None, risks=None, revision=1, **extra):
    body = {
        "schema_version": snapshot.SCHEMA_VERSION,
        "accounts": accounts if accounts is not None else BASE_ACCOUNTS,
        "channels": channels if channels is not None else BASE_CHANNELS,
        "risks": risks if risks is not None else BASE_RISKS,
        **extra,
    }
    return resign(body, revision=revision)


def write_legacy_db(path, *, accounts=None, channels=None, risks=None):
    accounts = accounts if accounts is not None else BASE_ACCOUNTS
    channels = channels if channels is not None else BASE_CHANNELS
    risks = risks if risks is not None else BASE_RISKS
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE account_configs (
                account_id TEXT PRIMARY KEY,
                execution_account_id TEXT NOT NULL,
                risk_capital_addon REAL NOT NULL,
                account_type TEXT NOT NULL,
                parent_account_id TEXT NOT NULL,
                is_enabled INTEGER NOT NULL,
                default_risk_ratio REAL
            );
            CREATE TABLE channel_routing (
                channel_id TEXT PRIMARY KEY,
                target_account_id TEXT NOT NULL
            );
            CREATE TABLE symbol_risk_configs (
                symbol TEXT PRIMARY KEY,
                risk_ratio REAL NOT NULL
            );
            """
        )
        for account in accounts:
            parent = account["parent_account_id"]
            conn.execute(
                "INSERT INTO account_configs VALUES (?,?,?,?,?,?,?)",
                (
                    account["account_id"],
                    account["execution_account_id"],
                    float(account["risk_capital_addon"]),
                    "main" if account["kind"] == "main" else "subaccount",
                    "" if parent is None else parent,
                    1 if account["enabled"] else 0,
                    None if account["default_risk"] is None else float(account["default_risk"]),
                ),
            )
        for channel in channels:
            conn.execute(
                "INSERT INTO channel_routing VALUES (?,?)",
                (channel["channel_id"], channel["target_account_id"]),
            )
        for risk in risks:
            conn.execute(
                "INSERT INTO symbol_risk_configs VALUES (?,?)",
                (risk["symbol"], float(risk["risk_ratio"])),
            )
        conn.commit()
    finally:
        conn.close()


def outcome(fn):
    try:
        return ("ok", fn())
    except HTTPException as exc:
        return (exc.status_code, exc.detail)


def assert_switch_equivalent(monkeypatch, tmp_path, fn, *, accounts=None, channels=None, risks=None):
    db_path = tmp_path / f"watcher-{uuid.uuid4().hex}.db"
    write_legacy_db(db_path, accounts=accounts, channels=channels, risks=risks)
    monkeypatch.setattr(read_api, "_WATCHER_TRADING_DB", str(db_path))
    monkeypatch.delenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", raising=False)
    monkeypatch.setattr(snapshot, "get_process_cache", lambda: (_ for _ in ()).throw(
        AssertionError("disabled snapshot switch must not access cache"),
    ))
    legacy = outcome(fn)
    subject = cache(Clock(), lambda *_: (200, signed(accounts, channels, risks)))
    assert subject.refresh() is True
    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    monkeypatch.setattr(snapshot, "get_process_cache", lambda: subject)
    live = outcome(fn)
    assert live == legacy
    return live


def operator_principal():
    return SimpleNamespace(
        kind=read_api.PrincipalKind.OPERATOR,
        actor_id="risk_admin",
        role="risk_admin",
        scope="all",
        account_id=None,
        session_subject=None,
    )


def stub_operator(monkeypatch, subject=None):
    monkeypatch.setattr(read_api, "_require_operator_principal", lambda *_args, **_kwargs: operator_principal())
    monkeypatch.setattr(read_api, "_operator_accounts", lambda: ("account-a",))
    if subject is not None:
        monkeypatch.setattr(snapshot, "get_process_cache", lambda: subject)


def patch_http(monkeypatch, status, body: bytes):
    class Response:
        def __init__(self):
            self.status = status
            self._buf = body
            self._off = 0
            self.reads = []

        def read(self, n):
            self.reads.append(n)
            chunk = self._buf[self._off:self._off + n]
            self._off += len(chunk)
            return chunk

    response = Response()

    class Connection:
        sock = None

        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return response

        def close(self):
            pass

    monkeypatch.setattr(snapshot.http.client, "HTTPConnection", Connection)
    return response


def test_403_latches_unauthorized_and_blocks_open_within_fresh_window(monkeypatch):
    clock = Clock()
    responses = [(200, payload()), (403, {}), (200, payload(2))]
    subject = cache(clock, lambda *_: responses.pop(0))
    subject.refresh()
    clock.now = 10
    assert subject.refresh() is False
    assert subject.last_refresh_error == "unauthorized"
    assert subject.state == "unauthorized"
    with pytest.raises(snapshot.SnapshotUnavailable):
        subject.require_fresh()
    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    stub_operator(monkeypatch, subject)
    with pytest.raises(HTTPException) as exc:
        read_api.operator_order({
            "action": "open_position", "account_id": "account-a", "symbol": "BTCUSDT",
            "reason": "403 latch", "dry_run": True,
        })
    assert exc.value.status_code == 503
    assert exc.value.detail == "snapshot_unavailable"
    assert subject.refresh() is True
    assert subject.state == "fresh"


def test_non_json_refresh_does_not_latch_and_age_grows():
    clock = Clock()
    calls = {"n": 0}

    def fetch(*_):
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, payload()
        raise json.JSONDecodeError("expecting value", "<html>", 0)

    subject = cache(clock, fetch)
    subject.refresh()
    clock.now = 10
    assert subject.refresh() is False
    assert subject.last_refresh_error == "unavailable"
    assert subject.state == "fresh"
    assert subject.require_fresh().check["revision"] == 1
    clock.now = 61
    assert subject.state == "expired"
    with pytest.raises(snapshot.SnapshotUnavailable):
        subject.require_fresh()


def test_invalid_utf8_refresh_does_not_latch_and_age_grows():
    clock = Clock()
    calls = {"n": 0}

    def fetch(*_):
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, payload()
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")

    subject = cache(clock, fetch)
    subject.refresh()
    clock.now = 10
    assert subject.refresh() is False
    assert subject.last_refresh_error == "unavailable"
    assert subject.state == "fresh"
    clock.now = 61
    assert subject.state == "expired"


def test_oversize_refresh_does_not_latch_and_age_grows():
    clock = Clock()
    calls = {"n": 0}

    def fetch(*_):
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, payload()
        raise OSError("snapshot exceeds 4 MiB")

    subject = cache(clock, fetch)
    subject.refresh()
    clock.now = 10
    assert subject.refresh() is False
    assert subject.last_refresh_error == "unavailable"
    assert subject.state == "fresh"
    clock.now = 61
    assert subject.state == "expired"


def test_http_fetch_allows_exactly_4_mib(monkeypatch):
    body = b"{" + b" " * (4194304 - 2) + b"}"
    assert len(body) == 4194304
    patch_http(monkeypatch, 200, body)
    status, raw = snapshot._http_fetch(
        "http://127.0.0.1:9090/api/trading/config-snapshot", "fake-token", 2.0,
    )
    assert status == 200
    assert raw == {}


def test_http_fetch_rejects_over_4_mib_without_unbounded_read(monkeypatch):
    class Response:
        status = 200

        def __init__(self):
            self.sent = 0
            self.reads = []

        def read(self, n):
            self.reads.append(n)
            if n > 65536:
                raise AssertionError(f"unbounded read chunk {n}")
            if self.sent >= 4194305:
                raise AssertionError("read continued after the 4 MiB cap")
            remain = 4194305 - self.sent
            chunk = b"x" * min(n, remain)
            self.sent += len(chunk)
            return chunk

    response = Response()

    class Connection:
        sock = None

        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return response

        def close(self):
            pass

    monkeypatch.setattr(snapshot.http.client, "HTTPConnection", Connection)
    with pytest.raises(OSError, match="4 MiB"):
        snapshot._http_fetch(
            "http://127.0.0.1:9090/api/trading/config-snapshot", "fake-token", 2.0,
        )
    assert response.sent == 4194305
    assert response.reads
    assert max(response.reads) <= 65536
    assert sum(response.reads) <= 4194304 + 65536


def test_over_4_mib_http_response_is_ordinary_refresh_failure(monkeypatch):
    clock = Clock()
    subject = cache(clock, lambda *_: (200, payload()))
    assert subject.refresh() is True
    patch_http(monkeypatch, 200, b"{" + b" " * (4194305 - 2) + b"}")
    subject._fetch = snapshot._http_fetch
    clock.now = 10
    assert subject.refresh() is False
    assert subject.last_refresh_error == "unavailable"
    assert subject.state == "fresh"
    clock.now = 61
    assert subject.state == "expired"


def test_http_fetch_html_is_json_decode_error(monkeypatch):
    patch_http(monkeypatch, 200, b"<html>watcher error</html>")
    with pytest.raises(json.JSONDecodeError):
        snapshot._http_fetch(
            "http://127.0.0.1:9090/api/trading/config-snapshot", "fake-token", 2.0,
        )


def test_channel_route_query_requires_fresh_when_switch_on(monkeypatch):
    clock = Clock()
    subject = cache(clock, lambda *_: (200, payload()))
    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    monkeypatch.setattr(read_api, "require_reader", lambda *_args, **_kwargs: "viewer")
    monkeypatch.setattr(snapshot, "get_process_cache", lambda: subject)
    with pytest.raises(HTTPException) as cold:
        operator_queries.operator_query("channel-route", channel="123")
    assert cold.value.status_code == 503
    assert cold.value.detail == "snapshot_unavailable"
    assert subject.refresh() is True
    assert operator_queries.operator_query("channel-route", channel="123") == {
        "execution_account_id": "account-a", "risk_capital_addon": 25.0,
    }
    clock.now = 61
    with pytest.raises(HTTPException) as expired:
        operator_queries.operator_query("channel-route", channel="123")
    assert expired.value.status_code == 503
    assert expired.value.detail == "snapshot_unavailable"


def test_channel_route_query_keeps_legacy_when_switch_off(monkeypatch, tmp_path):
    write_legacy_db(tmp_path / "watcher-trading.db")
    monkeypatch.setattr(read_api, "_WATCHER_TRADING_DB", str(tmp_path / "watcher-trading.db"))
    monkeypatch.delenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", raising=False)
    monkeypatch.setattr(read_api, "require_reader", lambda *_args, **_kwargs: "viewer")
    monkeypatch.setattr(snapshot, "get_process_cache", lambda: (_ for _ in ()).throw(
        AssertionError("disabled snapshot switch must not access cache"),
    ))
    assert operator_queries.operator_query("channel-route", channel="123") == {
        "execution_account_id": "account-a", "risk_capital_addon": 25.0,
    }


def test_channel_route_expired_does_not_fall_back_to_sqlite(monkeypatch, tmp_path):
    write_legacy_db(tmp_path / "watcher-trading.db")
    monkeypatch.setattr(read_api, "_WATCHER_TRADING_DB", str(tmp_path / "watcher-trading.db"))
    clock = Clock()
    subject = cache(clock, lambda *_: (200, payload()))
    subject.refresh()
    clock.now = 61
    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    monkeypatch.setattr(read_api, "require_reader", lambda *_args, **_kwargs: "viewer")
    monkeypatch.setattr(snapshot, "get_process_cache", lambda: subject)
    with pytest.raises(HTTPException) as exc:
        operator_queries.operator_query("channel-route", channel="123")
    assert exc.value.detail == "snapshot_unavailable"


def test_happy_channel_and_account_match_legacy(monkeypatch, tmp_path):
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._load_channel_risk_route("123"),
    ) == ("ok", {"execution_account_id": "account-a", "risk_capital_addon": 25.0})
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._account_risk_capital_addon("account-a"),
    ) == ("ok", 25.0)


def test_disabled_account_matches_legacy_details(monkeypatch, tmp_path):
    accounts = [{**BASE_ACCOUNTS[0], "enabled": False}]
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._load_channel_risk_route("123", "account-a"),
        accounts=accounts,
    ) == (503, "watcher channel route target account is disabled")
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._account_risk_capital_addon("account-a"),
        accounts=accounts,
    ) == (503, "watcher account risk configuration is disabled")


def test_missing_route_matches_legacy_detail(monkeypatch, tmp_path):
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._load_channel_risk_route("missing", "account-a"),
    ) == (503, "channel missing must resolve to exactly one execution account")


def test_missing_account_matches_legacy_detail(monkeypatch, tmp_path):
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._account_risk_capital_addon("account-z"),
    ) == (503, "execution account account-z must resolve to exactly one risk configuration")


def test_empty_channel_matches_legacy_detail(monkeypatch, tmp_path):
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._load_channel_risk_route("  ", "account-a"),
    ) == (503, "channel risk route has no channel_id")


def test_route_conflict_matches_legacy_detail(monkeypatch, tmp_path):
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._load_channel_risk_route("123", "account-b"),
    ) == (409, "watcher channel route conflicts with requested account_id")


def test_disabled_route_target_with_conflicting_account_checks_conflict_first(monkeypatch, tmp_path):
    accounts = [{**BASE_ACCOUNTS[0], "enabled": False}]
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._load_channel_risk_route("123", "account-b"),
        accounts=accounts,
    ) == (409, "watcher channel route conflicts with requested account_id")


def test_risk_priority_symbol_account_env_match_legacy(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_DEFAULT_RISK_RATIO", "0.05")
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._symbol_risk_ratio("BTCUSDT", "account-a"),
    ) == ("ok", 0.02)
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._symbol_risk_ratio("ETHUSDT", "account-a"),
    ) == ("ok", 0.01)
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._symbol_risk_ratio("ETHUSDT", "account-z"),
    ) == ("ok", 0.05)


def test_null_default_risk_matches_legacy_detail(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_DEFAULT_RISK_RATIO", "0.05")
    accounts = [{**BASE_ACCOUNTS[0], "default_risk": None}]
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._symbol_risk_ratio("ETHUSDT", "account-a"),
        accounts=accounts,
    ) == (503, RISK_UNAVAILABLE)


def test_out_of_range_env_default_matches_legacy_detail(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_DEFAULT_RISK_RATIO", "0.5")
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._symbol_risk_ratio("ETHUSDT", "account-z"),
    ) == (503, RISK_UNAVAILABLE)


def test_out_of_range_account_default_risk_uses_legacy_detail(monkeypatch, tmp_path):
    accounts = [{**BASE_ACCOUNTS[0], "default_risk": "0.2"}]
    db_path = tmp_path / "oob.db"
    write_legacy_db(db_path, accounts=accounts)
    monkeypatch.setattr(read_api, "_WATCHER_TRADING_DB", str(db_path))
    monkeypatch.delenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", raising=False)
    legacy = outcome(lambda: read_api._symbol_risk_ratio("ETHUSDT", "account-a"))
    account = snapshot.Account(
        "source-a", "main", None, "account-a", True, 25.0, 0.2,
    )
    lease = snapshot.SnapshotLease(
        snapshot.ConfigSnapshot(
            1, "0" * 64,
            MappingProxyType({"source-a": account}),
            MappingProxyType({"account-a": account}),
            MappingProxyType({}),
            MappingProxyType({}),
        ),
        0,
    )
    live = outcome(lambda: read_api._symbol_risk_ratio("ETHUSDT", "account-a", lease))
    assert live == legacy == (503, RISK_UNAVAILABLE)


def test_disabled_account_still_supplies_default_risk(monkeypatch, tmp_path):
    accounts = [{**BASE_ACCOUNTS[0], "enabled": False}]
    assert assert_switch_equivalent(
        monkeypatch, tmp_path, lambda: read_api._symbol_risk_ratio("ETHUSDT", "account-a"),
        accounts=accounts,
    ) == ("ok", 0.01)


def test_open_disabled_account_uses_legacy_detail_not_snapshot_unavailable(monkeypatch):
    raw = signed(accounts=[{**BASE_ACCOUNTS[0], "enabled": False}])
    subject = cache(Clock(), lambda *_: (200, raw))
    subject.refresh()
    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    stub_operator(monkeypatch, subject)
    with pytest.raises(HTTPException) as exc:
        read_api.operator_order({
            "action": "open_position", "account_id": "account-a", "symbol": "BTCUSDT",
            "reason": "disabled account", "dry_run": True,
        })
    assert exc.value.status_code == 503
    assert exc.value.detail == "watcher account risk configuration is disabled"


@pytest.mark.parametrize("action,extra", [
    ("close_position", {"position_side": "long"}),
    ("partial_close", {"position_side": "long", "quantity": "0.1"}),
    ("move_stop_loss", {"position_side": "long", "stop_loss": 90.0}),
    ("replace_take_profits", {
        "position_side": "long",
        "take_profits": [{"price": 120.0, "quantity": 0.1}],
    }),
])
def test_m4b_non_entry_dry_run_ignores_expired_snapshot(monkeypatch, action, extra):
    clock = Clock()
    subject = cache(clock, lambda *_: (200, payload()))
    subject.refresh()
    clock.now = 61
    assert subject.state == "expired"
    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    stub_operator(monkeypatch, subject)
    result = read_api.operator_order({
        "action": action, "account_id": "account-a", "symbol": "BTCUSDT",
        "reason": "non-entry independence", "dry_run": True,
        "client_ref": f"m4b-{action}", "authorized_by_type": "user",
        **extra,
    })
    assert result["dry_run"] is True
    assert result["action"] == action
    assert all(item.get("name") != "config_snapshot" for item in result["checks"])


def _open_with_pinned_lease(monkeypatch, *, batch):
    clock = Clock()
    rev2 = resign({**payload(2), "risks": [{"symbol": "BTCUSDT", "risk_ratio": "0.03"}]})
    responses = [(200, payload()), (200, rev2)]
    subject = cache(clock, lambda *_: responses.pop(0))
    subject.refresh()
    calls = []
    original = subject.require_fresh

    def counted():
        lease = original()
        calls.append(lease.check["revision"])
        if len(calls) == 1:
            assert subject.refresh() is True
        return lease

    subject.require_fresh = counted
    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    stub_operator(monkeypatch, subject)
    monkeypatch.setattr(read_api, "_account_financial_state", lambda *_: {
        "real_equity": 1000.0, "available_balance": 1000.0,
    })
    entry = {"type": "limit", "price": 100.0}
    if batch:
        entry["second_price"] = 98.0
    result = read_api.operator_order({
        "action": "open_position", "account_id": "account-a", "symbol": "BTCUSDT",
        "side": "long", "entry": entry, "stop_loss": 90.0,
        "reason": "pin lease", "dry_run": True, "client_ref": "pin-lease",
    })
    subject.require_fresh = original
    return result, calls, subject


def test_m5a_m6_single_leg_uses_one_lease_and_records_evidence(monkeypatch):
    result, calls, subject = _open_with_pinned_lease(monkeypatch, batch=False)
    assert calls == [1]
    assert result["dry_run"] is True
    snapshot_check = next(item for item in result["checks"] if item["name"] == "config_snapshot")
    assert snapshot_check["passed"] is True
    assert snapshot_check["revision"] == 1
    assert snapshot_check["content_sha256"] == payload()["content_sha256"]
    assert snapshot_check["snapshot_state"] == "fresh"
    assert isinstance(snapshot_check["age_ms"], int) and snapshot_check["age_ms"] >= 0
    sizing = next(item for item in result["checks"] if item["name"] == "risk_sizing")
    assert sizing["risk_ratio"] == 0.02
    assert subject.require_fresh().check["revision"] == 2


def test_m5b_batch_open_uses_one_lease(monkeypatch):
    result, calls, _subject = _open_with_pinned_lease(monkeypatch, batch=True)
    assert calls == [1]
    sizing = next(item for item in result["checks"] if item["name"] == "risk_sizing")
    assert sizing["risk_ratio"] == 0.02
    assert next(item for item in result["checks"] if item["name"] == "config_snapshot")["revision"] == 1


def test_committed_open_persists_one_pinned_snapshot_check(monkeypatch, migrated_db):
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("RISK_ADMIN_TOKEN", RISK_TOKEN)
    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    monkeypatch.setenv("OPERATOR_MAX_LEVERAGE", "10")
    monkeypatch.setenv("OPERATOR_ACCOUNT_REGISTRY_JSON", '{"account-b": {}}')
    _activate_redis_epoch(migrated_db)
    _seed_reviewed_rollout(migrated_db)
    _seed_account(migrated_db, account_id=ACCOUNT_B, node_id=NODE_B)

    accounts = [{**BASE_ACCOUNTS[0], "execution_account_id": ACCOUNT_B}]
    risks = [{"symbol": SYMBOL, "risk_ratio": "0.02"}]
    revisions = [(200, signed(accounts=accounts, risks=risks, revision=7)),
                 (200, signed(accounts=accounts, risks=risks, revision=8))]
    subject = cache(Clock(), lambda *_: revisions.pop(0))
    assert subject.refresh() is True
    calls = []
    original = subject.require_fresh

    def counted():
        lease = original()
        calls.append(lease.check["revision"])
        if len(calls) == 1:
            assert subject.refresh() is True
        return lease

    monkeypatch.setattr(subject, "require_fresh", counted)
    monkeypatch.setattr(snapshot, "get_process_cache", lambda: subject)
    body = _entry_body("open_position", "wac-033-pinned-commit")
    client = TestClient(read_api.app)
    try:
        response = client.post(
            "/v1/operator/orders",
            headers={"Authorization": f"Bearer {RISK_TOKEN}", "X-Request-Id": body["client_ref"]},
            json=body,
        )
    finally:
        client.close()
    assert response.status_code == 200, response.text
    assert calls == [7]
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT rd.checks FROM risk_decisions rd "
            "JOIN trade_intents ti ON ti.risk_decision_id = rd.risk_decision_id "
            "WHERE ti.intent_id = %s",
            (response.json()["intent_id"],),
        )
        row = cur.fetchone()
    assert row is not None
    checks = row[0]
    evidence = [check for check in checks if check.get("name") == "config_snapshot"]
    assert len(evidence) == 1
    assert evidence[0]["revision"] == 7
    assert evidence[0]["snapshot_state"] == "fresh"
    assert original().check["revision"] == 8


def test_m5d_size_entry_batch_forwards_lease(monkeypatch):
    subject = cache(Clock(), lambda *_: (200, payload()))
    subject.refresh()
    lease = subject.require_fresh()
    monkeypatch.setenv("WATCHER_CONFIG_SNAPSHOT_ENABLED", "1")
    monkeypatch.setattr(snapshot, "get_process_cache", lambda: (_ for _ in ()).throw(
        AssertionError("batch sizing must use the order's lease"),
    ))
    monkeypatch.setattr(read_api, "_account_financial_state", lambda *_: {
        "real_equity": 1000.0, "available_balance": 1000.0,
    })
    checks = [lease.check]
    _total, _batch = read_api._size_entry_batch(
        None, "BTCUSDT", "account-a", "long", "limit", 100.0, 98.0,
        90.0, 10.0,
        {"max_notional": None, "max_leverage": 10.0, "max_risk_fraction": 0.06,
         "no_sl_equity_fraction": 0.2},
        checks, lease.account_addon("account-a"), snapshot_lease=lease,
    )
    assert next(item for item in checks if item["name"] == "risk_sizing")["risk_ratio"] == 0.02


@pytest.mark.parametrize("table,secret", [
    ("accounts", "api_key"),
    ("channels", "session"),
    ("risks", "api_key"),
])
def test_m7b_row_secret_key_is_invalid_even_with_matching_digest(table, secret):
    raw = payload()
    raw[table][0][secret] = "leak"
    raw = resign(raw)
    subject = cache(Clock(), lambda *_: (200, raw))
    subject.refresh()
    assert subject.last_refresh_error == "invalid"
    assert subject.state == "invalid"
    with pytest.raises(snapshot.SnapshotUnavailable):
        subject.require_fresh()


def test_m10_failed_refresh_does_not_reset_age():
    clock = Clock()
    calls = {"n": 0}

    def fetch(*_):
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, payload()
        raise TimeoutError()

    subject = cache(clock, fetch)
    subject.refresh()
    for step in (10, 20, 30, 40, 50, 60):
        clock.now = step
        assert subject.refresh() is False
        assert subject.last_refresh_error == "timeout"
        assert subject.state == "fresh"
    clock.now = 61
    assert subject.state == "expired"
    with pytest.raises(snapshot.SnapshotUnavailable):
        subject.require_fresh()
