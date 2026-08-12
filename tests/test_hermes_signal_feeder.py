from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FEEDER_PATH = REPO_ROOT / "scripts" / "hermes_signal_feeder.py"


@pytest.fixture(autouse=True)
def _clear_trading_db_path_env(monkeypatch):
    for name in (
        "TRADER_TRADING_DB_PATH",
        "WATCHER_TRADING_DB",
        "TRADING_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def _load_feeder(name: str = "test_hermes_signal_feeder"):
    spec = importlib.util.spec_from_file_location(
        name,
        FEEDER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load feeder: {FEEDER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_trading_db_path_resolver_accepts_canonical_and_legacy_aliases() -> None:
    module = _load_feeder("test_hermes_signal_feeder_db_path")

    assert (
        module.resolve_trading_db_path({})
        == module.DEFAULT_TRADING_DB_PATH
    )
    assert module.resolve_trading_db_path(
        {"WATCHER_TRADING_DB": "/data/watcher-trading.db"}
    ) == "/data/watcher-trading.db"
    assert module.resolve_trading_db_path(
        {
            "TRADER_TRADING_DB_PATH": "/data/watcher-trading.db",
            "WATCHER_TRADING_DB": "/data/watcher-trading.db",
            "TRADING_DB_PATH": "/data/watcher-trading.db",
        }
    ) == "/data/watcher-trading.db"


def test_trading_db_path_conflict_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("TRADER_TRADING_DB_PATH", "/data/a.db")
    monkeypatch.setenv("WATCHER_TRADING_DB", "/data/b.db")

    with pytest.raises(RuntimeError, match="conflicting trading DB path"):
        _load_feeder("test_hermes_signal_feeder_db_conflict")


def _create_trading_db(
    path: Path,
    *,
    include_enabled: bool = False,
    duplicate_routes: bool = False,
) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    enabled_column = ""
    if include_enabled:
        enabled_column = ", is_enabled INTEGER NOT NULL DEFAULT 1"
    route_constraint = "PRIMARY KEY"
    if duplicate_routes:
        route_constraint = ""
    conn.executescript(
        f"""
        CREATE TABLE account_configs (
            account_id TEXT PRIMARY KEY,
            account_type TEXT NOT NULL DEFAULT 'main',
            parent_account_id TEXT NOT NULL DEFAULT '',
            risk_capital_multiplier REAL NOT NULL DEFAULT 1.0,
            execution_account_id TEXT NOT NULL
            {enabled_column}
        );

        CREATE TABLE channel_routing (
            channel_id TEXT {route_constraint},
            target_account_id TEXT NOT NULL
        );
        """
    )
    return conn


def _insert_account(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    account_type: str = "main",
    parent_account_id: str = "",
    multiplier: float = 1.0,
    execution_account_id: str | None = None,
    enabled: int | None = None,
) -> None:
    if execution_account_id is None:
        execution_account_id = account_id
    columns = [
        "account_id",
        "account_type",
        "parent_account_id",
        "risk_capital_multiplier",
        "execution_account_id",
    ]
    values: list[object] = [
        account_id,
        account_type,
        parent_account_id,
        multiplier,
        execution_account_id,
    ]
    if enabled is not None:
        columns.append("is_enabled")
        values.append(enabled)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO account_configs ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )


def _signal(
    channel_id: str,
    signal_id: str,
    message_id: str = "1",
) -> dict[str, str]:
    return {
        "signal_id": signal_id,
        "received_at": "2026-08-11T00:00:00+00:00",
        "payload": json.dumps(
            {
                "source_channel_id": channel_id,
                "source_channel_name": f"channel-{signal_id}",
                "source_message_id": message_id,
                "raw_text": "BTC long",
            }
        ),
    }


def _create_signal_db(path: Path, signals: list[dict[str, str]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE signals (
                signal_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO signals (signal_id, received_at, payload)
            VALUES (:signal_id, :received_at, :payload)
            """,
            signals,
        )
        conn.commit()
    finally:
        conn.close()


def test_four_channel_routes_bind_distinct_accounts_in_hermes_prompt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "watcher-trading.db"
    conn = _create_trading_db(db_path)
    routes = {
        "-1002136478186": ("jiataotx@gmail.com", "account-a"),
        "-1002198013097": ("balenwong3@gmail.com", "account-b"),
        "-1002189417451": ("泰山", "account-c"),
        "-1002193304023": ("黄山", "account-d"),
    }
    _insert_account(
        conn,
        "jiataotx@gmail.com",
        execution_account_id="account-a",
    )
    _insert_account(
        conn,
        "balenwong3@gmail.com",
        execution_account_id="account-b",
    )
    _insert_account(
        conn,
        "泰山",
        account_type="subaccount",
        parent_account_id="jiataotx@gmail.com",
        multiplier=2.0,
        execution_account_id="account-c",
    )
    _insert_account(
        conn,
        "黄山",
        account_type="subaccount",
        parent_account_id="jiataotx@gmail.com",
        multiplier=2.0,
        execution_account_id="account-d",
    )
    for channel_id, (target_account_id, _) in routes.items():
        conn.execute(
            "INSERT INTO channel_routing (channel_id, target_account_id) VALUES (?, ?)",
            (channel_id, target_account_id),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(db_path))
    monkeypatch.setattr(module, "V3_MEDIA", str(tmp_path / "media"))

    for index, (channel_id, route) in enumerate(routes.items(), start=1):
        target_account_id, execution_account_id = route
        prompt = module.build_prompt(
            _signal(channel_id, f"{channel_id}:{index}", str(index))
        )

        assert f"路由凭据账号(审计): {target_account_id}" in prompt
        assert f"固定执行账号: {execution_account_id}" in prompt
        assert f"必须使用 --account {execution_account_id}" in prompt
        assert f"--channel {channel_id}" in prompt
        assert f"--authorized-by-id {channel_id}" in prompt
        expected_ref = f"tg-sig-c{channel_id.lstrip('-')}-m{index}"
        assert f"交易ref: {expected_ref}" in prompt
        assert "对应正文块的“交易ref”原样传给 --source-message-id" in prompt
        assert "Hermes 无权选择或改写" in prompt
        expected_multiplier = "2" if target_account_id in {"泰山", "黄山"} else "1"
        assert f"风险资金系数(审计): {expected_multiplier}" in prompt


def test_unmapped_channel_is_quarantined_once_and_does_not_starve_queue(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    module = _load_feeder()
    signal_db = tmp_path / "signal-store.db"
    unknown = _signal(
        "-1002328068747",
        "sig-unknown",
        "9001",
    )
    unknown["received_at"] = "2026-08-11T00:00:00+00:00"
    valid = _signal(
        "-1002136478186",
        "sig-valid",
        "9002",
    )
    valid["received_at"] = "2026-08-11T00:01:00+00:00"
    _create_signal_db(signal_db, [unknown, valid])

    trading_db = tmp_path / "watcher-trading.db"
    conn = _create_trading_db(trading_db)
    _insert_account(
        conn,
        "credential-secret-label",
        execution_account_id="account-a",
    )
    conn.execute(
        "INSERT INTO channel_routing (channel_id, target_account_id) "
        "VALUES (?, ?)",
        ("-1002136478186", "credential-secret-label"),
    )
    conn.commit()
    conn.close()

    state_path = tmp_path / "cursor"
    initial_cursor = "2026-08-10T23:59:00+00:00|seed"
    state_path.write_text(initial_cursor, encoding="utf-8")
    monkeypatch.setattr(module, "WATCHER_DB", str(signal_db))
    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(trading_db))
    monkeypatch.setattr(module, "STATE", str(state_path))
    monkeypatch.setattr(module, "PENDING_STATE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(
        module,
        "QUARANTINE_STATE",
        str(tmp_path / "quarantine.json"),
        raising=False,
    )
    monkeypatch.setattr(module, "LOCK", str(tmp_path / "feeder.lock"))
    monkeypatch.setattr(module, "V3_MEDIA", str(tmp_path / "media"))
    legacy_pending = module._new_pending_delivery([unknown])
    legacy_pending["status"] = "route_blocked"
    legacy_pending["route_error"] = (
        "channel_id '-1002328068747' matched 0 routes"
    )
    module.save_pending_delivery(legacy_pending)
    monkeypatch.setattr(
        module,
        "compress_channel_contexts",
        lambda: None,
    )
    monkeypatch.setattr(
        module,
        "append_channel_context",
        lambda *_args, **_kwargs: None,
    )
    notices: list[str] = []
    dispatched: list[str] = []
    monkeypatch.setattr(
        module,
        "notify_blocked",
        lambda sig, dry_run, reason=None: notices.append(
            str(_payload_for_test(sig).get("source_channel_id"))
        ),
    )

    def run_hermes(prompt, name, dry_run, on_job_created=None):
        del prompt, dry_run
        dispatched.append(name)
        if on_job_created is not None:
            on_job_created("job-valid")
        return "job-valid"

    monkeypatch.setattr(module, "run_hermes", run_hermes)
    monkeypatch.setattr(
        module,
        "_latest_markdown_response",
        lambda _job_id: "processed",
    )
    monkeypatch.setattr(sys, "argv", ["hermes_signal_feeder.py", "--once"])

    module.main()
    module.main()

    quarantine = module.load_quarantine()
    output = capsys.readouterr().out
    assert state_path.read_text(encoding="utf-8") == (
        "2026-08-11T00:01:00+00:00|sig-valid"
    )
    assert len(quarantine["entries"]) == 1
    entry = next(iter(quarantine["entries"].values()))
    assert entry["channel_id"] == "-1002328068747"
    assert entry["reason_code"] == "route_not_found"
    assert entry["last_cursor"] == (
        "2026-08-11T00:00:00+00:00|sig-unknown"
    )
    assert notices == []
    assert dispatched == ["signal-sig-valid"]
    assert "credential-secret-label" not in output
    assert "matched 0 routes" not in output
    assert "credential-secret-label" not in json.dumps(
        quarantine,
        sort_keys=True,
    )


def _payload_for_test(signal: dict[str, str]) -> dict:
    return json.loads(signal["payload"])


def test_subaccount_route_requires_an_existing_main_parent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "invalid-parent.db"
    conn = _create_trading_db(db_path)
    _insert_account(
        conn,
        "泰山",
        account_type="subaccount",
        parent_account_id="不存在的主账号",
        execution_account_id="account-c",
    )
    conn.execute(
        "INSERT INTO channel_routing (channel_id, target_account_id) "
        "VALUES (?, ?)",
        ("-1002189417451", "泰山"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(db_path))

    with pytest.raises(
        module.ChannelRouteError,
        match="parent must resolve to one main account",
    ):
        module.resolve_channel_account("-1002189417451")


def test_malformed_signal_provenance_is_rejected_before_hermes_dispatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "watcher-trading.db"
    conn = _create_trading_db(db_path)
    _insert_account(conn, "credential-a", execution_account_id="account-a")
    conn.execute(
        "INSERT INTO channel_routing (channel_id, target_account_id) "
        "VALUES (?, ?)",
        ("-1002136478186", "credential-a"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(db_path))
    monkeypatch.setattr(module, "PENDING_STATE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(
        module,
        "QUARANTINE_STATE",
        str(tmp_path / "quarantine.json"),
    )
    monkeypatch.setattr(module, "V3_MEDIA", str(tmp_path / "media"))
    dispatched: list[str] = []
    monkeypatch.setattr(
        module,
        "notify_blocked",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        module,
        "run_hermes",
        lambda *args, **kwargs: dispatched.append("dispatched") or "job-1",
    )

    result = module.attempt_batch_delivery(
        [
            _signal(
                "-1002136478186",
                "-1002136478186:bad",
                "bad",
            )
        ],
        dry_run=False,
        now_ts=1000,
    )

    assert result == "quarantined"
    assert dispatched == []
    pending = module.load_pending_delivery()
    assert pending["status"] == "quarantined"
    assert pending["route_error"] == "signal_provenance_invalid"
    quarantine = module.load_quarantine()
    entry = next(iter(quarantine["entries"].values()))
    assert entry["reason_code"] == "signal_provenance_invalid"


def test_missing_channel_mapping_blocks_new_risk_dispatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "watcher-trading.db"
    conn = _create_trading_db(db_path)
    _insert_account(conn, "account-a")
    conn.commit()
    conn.close()
    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(db_path))
    monkeypatch.setattr(module, "PENDING_STATE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(
        module,
        "QUARANTINE_STATE",
        str(tmp_path / "quarantine.json"),
    )
    monkeypatch.setattr(module, "V3_MEDIA", str(tmp_path / "media"))
    notices: list[str] = []
    dispatched: list[str] = []
    monkeypatch.setattr(
        module,
        "notify_blocked",
        lambda sig, dry_run, reason=None: notices.append(str(reason)),
    )
    monkeypatch.setattr(
        module,
        "run_hermes",
        lambda *args, **kwargs: dispatched.append("dispatched") or "job-1",
    )

    result = module.attempt_batch_delivery(
        [_signal("-10099", "sig-missing")],
        dry_run=False,
        now_ts=1000,
    )

    assert result == "quarantined"
    assert dispatched == []
    assert notices == []
    pending = module.load_pending_delivery()
    assert pending["status"] == "quarantined"
    assert pending["route_error"] == "route_not_found"
    quarantine_before = module.load_quarantine()

    restarted = module.attempt_batch_delivery(
        [_signal("-10099", "sig-missing")],
        dry_run=False,
        now_ts=1010,
    )

    assert restarted == "quarantined"
    assert dispatched == []
    assert notices == []
    assert module.load_quarantine() == quarantine_before


def test_disabled_or_duplicate_channel_route_is_rejected(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    disabled_db = tmp_path / "disabled.db"
    conn = _create_trading_db(disabled_db, include_enabled=True)
    _insert_account(conn, "account-a", enabled=0)
    conn.execute(
        "INSERT INTO channel_routing (channel_id, target_account_id) VALUES (?, ?)",
        ("-10001", "account-a"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(disabled_db))

    with pytest.raises(module.ChannelRouteError, match="disabled"):
        module.resolve_channel_account("-10001")

    duplicate_db = tmp_path / "duplicate.db"
    conn = _create_trading_db(duplicate_db, duplicate_routes=True)
    _insert_account(conn, "account-a")
    conn.execute(
        "INSERT INTO channel_routing (channel_id, target_account_id) VALUES (?, ?)",
        ("-10002", "account-a"),
    )
    conn.execute(
        "INSERT INTO channel_routing (channel_id, target_account_id) VALUES (?, ?)",
        ("-10002", "account-a"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(duplicate_db))

    with pytest.raises(module.ChannelRouteError, match="matched 2 routes"):
        module.resolve_channel_account("-10002")

    monkeypatch.setattr(
        module,
        "PENDING_STATE",
        str(tmp_path / "duplicate-pending.json"),
    )
    monkeypatch.setattr(
        module,
        "QUARANTINE_STATE",
        str(tmp_path / "duplicate-quarantine.json"),
    )
    dispatched: list[str] = []
    monkeypatch.setattr(
        module,
        "run_hermes",
        lambda *args, **kwargs: dispatched.append("dispatched") or "job-1",
    )

    result = module.attempt_batch_delivery(
        [_signal("-10002", "sig-duplicate")],
        dry_run=False,
        now_ts=1000,
    )

    assert result == "quarantined"
    assert dispatched == []
    quarantine = module.load_quarantine()
    entry = next(iter(quarantine["entries"].values()))
    assert entry["reason_code"] == "route_not_unique"


def test_legacy_schema_or_duplicate_execution_identity_is_rejected(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    legacy_db = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_db)
    conn.executescript(
        """
        CREATE TABLE account_configs (
            account_id TEXT PRIMARY KEY
        );
        CREATE TABLE channel_routing (
            channel_id TEXT PRIMARY KEY,
            target_account_id TEXT NOT NULL
        );
        INSERT INTO account_configs (account_id) VALUES ('credential-a');
        INSERT INTO channel_routing (channel_id, target_account_id)
        VALUES ('-10003', 'credential-a');
        """
    )
    conn.close()
    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(legacy_db))

    with pytest.raises(module.ChannelRouteError, match="execution_account_id"):
        module.resolve_channel_account("-10003")

    duplicate_execution_db = tmp_path / "duplicate-execution.db"
    conn = _create_trading_db(duplicate_execution_db)
    _insert_account(
        conn,
        "credential-a",
        execution_account_id="account-a",
    )
    _insert_account(
        conn,
        "credential-b",
        execution_account_id="account-a",
    )
    conn.execute(
        "INSERT INTO channel_routing (channel_id, target_account_id) VALUES (?, ?)",
        ("-10004", "credential-a"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        module,
        "WATCHER_TRADING_DB",
        str(duplicate_execution_db),
    )

    with pytest.raises(module.ChannelRouteError, match="must be unique"):
        module.resolve_channel_account("-10004")


def test_route_schema_requires_dynamic_risk_capital_multiplier(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "missing-multiplier.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE account_configs (
            account_id TEXT PRIMARY KEY,
            execution_account_id TEXT NOT NULL
        );
        CREATE TABLE channel_routing (
            channel_id TEXT PRIMARY KEY,
            target_account_id TEXT NOT NULL
        );
        INSERT INTO account_configs (account_id, execution_account_id)
        VALUES ('credential-a', 'account-a');
        INSERT INTO channel_routing (channel_id, target_account_id)
        VALUES ('-10005', 'credential-a');
        """
    )
    conn.close()
    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(db_path))

    with pytest.raises(
        module.ChannelRouteError,
        match="risk_capital_multiplier",
    ):
        module.resolve_channel_account("-10005")
