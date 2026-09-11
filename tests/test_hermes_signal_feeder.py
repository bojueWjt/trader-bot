from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime
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
            risk_capital_addon REAL NOT NULL DEFAULT 0,
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
    addon: float = 0.0,
    execution_account_id: str | None = None,
    enabled: int | None = None,
) -> None:
    if execution_account_id is None:
        execution_account_id = account_id
    columns = [
        "account_id",
        "account_type",
        "parent_account_id",
        "risk_capital_addon",
        "execution_account_id",
    ]
    values: list[object] = [
        account_id,
        account_type,
        parent_account_id,
        addon,
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


def _ensure_telegram_messages_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id INTEGER,
            channel_id TEXT,
            chat_title TEXT DEFAULT '',
            sender TEXT DEFAULT '',
            text TEXT DEFAULT '',
            has_media INTEGER DEFAULT 0,
            media_type TEXT DEFAULT '',
            media_filename TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )


def _insert_telegram_message(
    conn: sqlite3.Connection,
    *,
    msg_id: int,
    channel_id: str = "-1002136478186",
    chat_title: str = "C01",
    text: str = "BTC long",
    media_type: str = "",
    media_filename: str = "",
    created_at: str = "2026-08-16 00:00:00",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO telegram_messages (
            msg_id, channel_id, chat_title, sender, text,
            has_media, media_type, media_filename, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            msg_id,
            channel_id,
            chat_title,
            "sender",
            text,
            1 if media_filename else 0,
            media_type,
            media_filename,
            created_at,
        ),
    )
    return int(cursor.lastrowid)


def _prepare_watcher_db(path: Path) -> sqlite3.Connection:
    conn = _create_trading_db(path)
    conn.row_factory = sqlite3.Row
    _ensure_telegram_messages_table(conn)
    _insert_account(
        conn,
        "credential-a",
        execution_account_id="account-a",
    )
    conn.execute(
        "INSERT INTO channel_routing (channel_id, target_account_id) "
        "VALUES (?, ?)",
        ("-1002136478186", "credential-a"),
    )
    return conn


def test_watcher_outbox_cursor_reads_only_new_unique_messages(
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "watcher-trading.db"
    conn = _prepare_watcher_db(db_path)
    first_id = _insert_telegram_message(conn, msg_id=7001)
    duplicate_id = _insert_telegram_message(conn, msg_id=7001)
    second_id = _insert_telegram_message(conn, msg_id=7002)
    conn.commit()

    assert module.latest_cursor(conn) == f"telegram_messages:{second_id}"
    rows = module.fetch_new(
        conn,
        f"telegram_messages:{first_id}",
    )
    conn.close()

    assert duplicate_id > first_id
    assert [row["watcher_message_id"] for row in rows] == [second_id]
    assert module._signal_cursor(rows[0]) == f"telegram_messages:{second_id}"
    payload = module._payload(rows[0])
    assert payload["source_channel_id"] == "-1002136478186"
    assert payload["source_message_id"] == "7002"
    assert payload["raw_text"] == "BTC long"


def test_canonical_ingress_succeeds_before_hermes_dispatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "watcher-trading.db"
    conn = _prepare_watcher_db(db_path)
    _insert_telegram_message(conn, msg_id=7003)
    conn.commit()
    row = module.fetch_new(conn, "telegram_messages:0")[0]
    conn.close()

    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(db_path))
    monkeypatch.setattr(module, "PENDING_STATE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(module, "V3_MEDIA", str(tmp_path / "v3-media"))
    monkeypatch.setattr(module, "CHANNEL_CONTEXT_DIR", str(tmp_path / "context"))
    calls: list[str] = []

    def submit_canonical_ingress(signal):
        calls.append(f"ingress:{signal['watcher_message_id']}")
        return {
            "inserted": True,
            "raw_message_id": "00000000-0000-0000-0000-000000000001",
        }

    def run_hermes(prompt, name, dry_run, on_job_created=None):
        del prompt, name, dry_run
        calls.append("hermes")
        if on_job_created is not None:
            on_job_created("job-1")
        return "job-1"

    monkeypatch.setattr(
        module,
        "submit_canonical_ingress",
        submit_canonical_ingress,
    )
    monkeypatch.setattr(module, "run_hermes", run_hermes)
    monkeypatch.setattr(
        module,
        "_latest_markdown_response",
        lambda _job_id: "processed",
    )
    monkeypatch.setattr(
        module,
        "load_cron_job_status",
        lambda _job_id: {
            "last_run_at": "2026-08-19T00:00:00+00:00",
            "last_status": "ok",
            "last_delivery_error": None,
        },
    )
    monkeypatch.setattr(module, "remove_cron_job", lambda _job_id: True)

    result = module.attempt_batch_delivery(
        [row],
        dry_run=False,
        now_ts=1000,
    )

    assert result == "success"
    assert calls == [f"ingress:{row['watcher_message_id']}", "hermes"]
    pending = module.load_pending_delivery()
    assert pending["canonical_ingress"][0]["raw_message_id"].endswith("0001")


def test_failed_ingress_blocks_hermes_and_cursor_advancement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "watcher-trading.db"
    conn = _prepare_watcher_db(db_path)
    first_id = _insert_telegram_message(conn, msg_id=7004)
    second_id = _insert_telegram_message(conn, msg_id=7005)
    conn.commit()
    conn.close()

    state_path = tmp_path / "cursor"
    state_path.write_text(
        f"telegram_messages:{first_id}",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(db_path))
    monkeypatch.setattr(module, "STATE", str(state_path))
    monkeypatch.setattr(module, "PENDING_STATE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(
        module,
        "QUARANTINE_STATE",
        str(tmp_path / "quarantine.json"),
    )
    monkeypatch.setattr(module, "LOCK", str(tmp_path / "feeder.lock"))
    monkeypatch.setattr(module, "V3_MEDIA", str(tmp_path / "v3-media"))
    monkeypatch.setattr(module, "CHANNEL_CONTEXT_DIR", str(tmp_path / "context"))
    monkeypatch.setattr(module, "compress_channel_contexts", lambda: None)
    monkeypatch.setattr(
        module,
        "submit_canonical_ingress",
        lambda _signal: (_ for _ in ()).throw(
            module.CanonicalIngressError("ingress unavailable")
        ),
    )
    dispatched: list[str] = []
    monkeypatch.setattr(
        module,
        "run_hermes",
        lambda *args, **kwargs: dispatched.append("hermes") or "job-1",
    )
    monkeypatch.setattr(sys, "argv", ["hermes_signal_feeder.py", "--once"])

    module.main()

    assert second_id > first_id
    assert state_path.read_text(encoding="utf-8") == (
        f"telegram_messages:{first_id}"
    )
    assert dispatched == []
    pending = module.load_pending_delivery()
    assert pending["status"] == "ingress_retry"
    assert pending["ingress_attempts"] == 1


def test_startup_without_cursor_selects_latest_and_emits_no_history(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "watcher-trading.db"
    conn = _prepare_watcher_db(db_path)
    _insert_telegram_message(conn, msg_id=7006)
    latest_id = _insert_telegram_message(conn, msg_id=7007)
    conn.commit()
    conn.close()

    state_path = tmp_path / "cursor"
    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(db_path))
    monkeypatch.setattr(module, "STATE", str(state_path))
    monkeypatch.setattr(module, "PENDING_STATE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(module, "LOCK", str(tmp_path / "feeder.lock"))
    monkeypatch.setattr(module, "compress_channel_contexts", lambda: None)
    ingressed: list[str] = []
    dispatched: list[str] = []
    monkeypatch.setattr(
        module,
        "submit_canonical_ingress",
        lambda signal: ingressed.append(str(signal["watcher_message_id"])),
    )
    monkeypatch.setattr(
        module,
        "run_hermes",
        lambda *args, **kwargs: dispatched.append("hermes") or "job-1",
    )
    monkeypatch.setattr(sys, "argv", ["hermes_signal_feeder.py", "--once"])

    module.main()

    assert state_path.read_text(encoding="utf-8") == (
        f"telegram_messages:{latest_id}"
    )
    assert ingressed == []
    assert dispatched == []


def test_media_filename_maps_to_canonical_media_asset(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "watcher-trading.db"
    conn = _prepare_watcher_db(db_path)
    row_id = _insert_telegram_message(
        conn,
        msg_id=7008,
        media_type="photo",
        media_filename="photo-7008.jpg",
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM telegram_messages WHERE id = ?",
        (row_id,),
    ).fetchone()
    conn.close()

    watcher_root = tmp_path / "watcher"
    media_path = watcher_root / "media" / "photo-7008.jpg"
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"photo-bytes")
    monkeypatch.setattr(module, "WATCHER_ROOT", str(watcher_root))

    signal = module.normalize_watcher_row(row)
    payload = module.canonical_ingress_payload(signal)

    assert payload["message_kind"] == "photo"
    assert payload["media_assets"][0]["object_key"] == (
        "watcher/media/photo-7008.jpg"
    )
    assert payload["media_assets"][0]["mime"] == "image/jpeg"
    assert len(payload["media_assets"][0]["sha256"]) == 64
    signal_payload = module._payload(signal)
    assert signal_payload["media"][0]["path"] == (
        "/data/media/photo-7008.jpg"
    )


def test_duplicate_ingress_response_is_accepted_as_idempotent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    signal = {
        "watcher_message_id": 9,
        "signal_id": "sig-c1002136478186-m7009",
        "received_at": "2026-08-16T00:00:00+00:00",
        "payload": json.dumps(
            {
                "source_channel_id": "-1002136478186",
                "source_channel_name": "C01",
                "source_message_id": "7009",
                "raw_text": "BTC long",
            }
        ),
    }
    monkeypatch.setattr(module, "INGRESS_API_TOKEN", "secret")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "inserted": False,
                    "raw_message_id": (
                        "00000000-0000-0000-0000-000000000009"
                    ),
                    "outbox_event_id": None,
                }
            ).encode("utf-8")

    monkeypatch.setattr(module, "urlopen", lambda *_args, **_kwargs: Response())

    result = module.submit_canonical_ingress(signal)

    assert result["inserted"] is False
    assert result["raw_message_id"].endswith("0009")


def test_historical_review_export_is_explicitly_non_trading(
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "watcher-trading.db"
    conn = _prepare_watcher_db(db_path)
    first_id = _insert_telegram_message(conn, msg_id=7010)
    second_id = _insert_telegram_message(conn, msg_id=7011)
    conn.commit()
    output_path = tmp_path / "historical-review.jsonl"

    count = module.export_historical_review(
        conn,
        output_path,
        after_id=first_id - 1,
        through_id=second_id,
    )
    conn.close()

    records = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert count == 2
    assert [record["source_message_id"] for record in records] == [
        "7010",
        "7011",
    ]
    assert all(record["trade_capable"] is False for record in records)
    assert all(record["dispatch_allowed"] is False for record in records)


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
        addon=6000.0,
        execution_account_id="account-c",
    )
    _insert_account(
        conn,
        "黄山",
        account_type="subaccount",
        parent_account_id="jiataotx@gmail.com",
        addon=6000.0,
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

        assert "当前时间(UTC):" in prompt
        timestamp = prompt.split("当前时间(UTC): ", 1)[1].splitlines()[0]
        datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S UTC")
        assert "开仓时效:" in prompt
        assert "超过 30 分钟" in prompt
        assert "无法确认发布时间时，开仓类一律只汇报" in prompt
        assert "两腿同等名义金额，共享总风险预算" in prompt
        assert "第二腿传 --second-price" in prompt
        assert f"路由凭据账号(审计): {target_account_id}" in prompt
        assert f"固定执行账号: {execution_account_id}" in prompt
        assert f"必须使用 --account {execution_account_id}" in prompt
        assert f"--channel {channel_id}" in prompt
        assert f"--authorized-by-id {channel_id}" in prompt
        expected_ref = f"tg-sig-c{channel_id.lstrip('-')}-m{index}"
        assert f"交易ref: {expected_ref}" in prompt
        assert "对应正文块的“交易ref”原样传给 --source-message-id" in prompt
        assert "Hermes 无权选择或改写" in prompt
        expected_addon = "6000" if target_account_id in {"泰山", "黄山"} else "0"
        assert f"风险资金加权额(审计): +{expected_addon} USDT" in prompt


def test_unmapped_channel_is_quarantined_once_and_does_not_starve_queue(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    module = _load_feeder()
    trading_db = tmp_path / "watcher-trading.db"
    conn = _create_trading_db(trading_db)
    conn.row_factory = sqlite3.Row
    _ensure_telegram_messages_table(conn)
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
    unknown_id = _insert_telegram_message(
        conn,
        msg_id=9001,
        channel_id="-1002328068747",
        created_at="2026-08-11 00:00:00",
    )
    valid_id = _insert_telegram_message(
        conn,
        msg_id=9002,
        channel_id="-1002136478186",
        created_at="2026-08-11 00:01:00",
    )
    conn.commit()
    unknown, _valid = module.fetch_new(
        conn,
        "telegram_messages:0",
    )
    conn.close()

    state_path = tmp_path / "cursor"
    initial_cursor = "telegram_messages:0"
    state_path.write_text(initial_cursor, encoding="utf-8")
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
    ingressed: list[int] = []

    def submit_canonical_ingress(signal):
        ingressed.append(int(signal["watcher_message_id"]))
        return {
            "inserted": True,
            "raw_message_id": (
                "00000000-0000-0000-0000-000000000001"
            ),
        }

    monkeypatch.setattr(
        module,
        "submit_canonical_ingress",
        submit_canonical_ingress,
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
    monkeypatch.setattr(
        module,
        "load_cron_job_status",
        lambda _job_id: {
            "last_run_at": "2026-08-19T00:00:00+00:00",
            "last_status": "ok",
            "last_delivery_error": None,
        },
    )
    monkeypatch.setattr(module, "remove_cron_job", lambda _job_id: True)
    monkeypatch.setattr(sys, "argv", ["hermes_signal_feeder.py", "--once"])

    module.main()
    module.main()

    quarantine = module.load_quarantine()
    output = capsys.readouterr().out
    assert state_path.read_text(encoding="utf-8") == (
        f"telegram_messages:{valid_id}"
    )
    assert len(quarantine["entries"]) == 1
    entry = next(iter(quarantine["entries"].values()))
    assert entry["channel_id"] == "-1002328068747"
    assert entry["reason_code"] == "route_not_found"
    assert entry["last_cursor"] == (
        f"telegram_messages:{unknown_id}"
    )
    assert notices == []
    assert dispatched == ["signal-sig-c1002136478186-m9002"]
    assert ingressed == [unknown_id, valid_id]
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


def test_response_waits_until_telegram_delivery_status_is_recorded(
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
    monkeypatch.setattr(module, "V3_MEDIA", str(tmp_path / "media"))
    monkeypatch.setattr(
        module,
        "submit_canonical_ingress",
        lambda _signal: {
            "inserted": True,
            "raw_message_id": "00000000-0000-0000-0000-000000000001",
        },
    )

    def run_hermes(prompt, name, dry_run, on_job_created=None):
        del prompt, name, dry_run
        if on_job_created is not None:
            on_job_created("job-waiting")
        return "job-waiting"

    monkeypatch.setattr(module, "run_hermes", run_hermes)
    monkeypatch.setattr(
        module,
        "_latest_markdown_response",
        lambda _job_id: "processed",
    )
    monkeypatch.setattr(
        module,
        "load_cron_job_status",
        lambda _job_id: {
            "last_run_at": None,
            "last_status": None,
            "last_delivery_error": None,
        },
    )

    result = module.attempt_batch_delivery(
        [_signal("-1002136478186", "sig-waiting")],
        dry_run=False,
        now_ts=1000,
    )

    assert result == "pending"
    pending = module.load_pending_delivery()
    assert pending["status"] == "observing"
    assert pending["job_id"] == "job-waiting"


def test_definitive_delivery_error_resends_without_rerunning_agent(
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
    monkeypatch.setattr(module, "V3_MEDIA", str(tmp_path / "media"))
    monkeypatch.setattr(module, "CHANNEL_CONTEXT_DIR", str(tmp_path / "context"))
    monkeypatch.setattr(
        module,
        "submit_canonical_ingress",
        lambda _signal: {
            "inserted": True,
            "raw_message_id": "00000000-0000-0000-0000-000000000001",
        },
    )
    agent_runs: list[str] = []

    def run_hermes(prompt, name, dry_run, on_job_created=None):
        del prompt, dry_run
        agent_runs.append(name)
        if on_job_created is not None:
            on_job_created("job-delivery-error")
        return "job-delivery-error"

    monkeypatch.setattr(module, "run_hermes", run_hermes)
    monkeypatch.setattr(
        module,
        "_latest_markdown_response",
        lambda _job_id: "Hermes already processed this signal",
    )
    monkeypatch.setattr(
        module,
        "load_cron_job_status",
        lambda _job_id: {
            "last_run_at": "2026-08-19T00:00:00+00:00",
            "last_status": "ok",
            "last_delivery_error": "Telegram send failed: ConnectTimeout",
        },
    )
    redelivered: list[str] = []
    monkeypatch.setattr(
        module,
        "redeliver_hermes_response",
        lambda response: (
            redelivered.append(response)
            or {"success": True, "message_id": "2160"}
        ),
    )
    cleaned: list[str] = []
    monkeypatch.setattr(
        module,
        "remove_cron_job",
        lambda job_id: cleaned.append(job_id) or True,
    )

    result = module.attempt_batch_delivery(
        [_signal("-1002136478186", "sig-delivery-error")],
        dry_run=False,
        now_ts=1000,
    )

    assert result == "success"
    assert len(agent_runs) == 1
    assert redelivered == ["Hermes already processed this signal"]
    assert cleaned == ["job-delivery-error"]
    pending = module.load_pending_delivery()
    assert pending["delivery_confirmed"] is True
    assert pending["delivery_recovered"] is True
    assert pending["telegram_message_id"] == "2160"


def test_uncertain_delivery_waits_then_redelivers_at_most_once(
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
    monkeypatch.setattr(module, "V3_MEDIA", str(tmp_path / "media"))
    monkeypatch.setattr(module, "CHANNEL_CONTEXT_DIR", str(tmp_path / "context"))
    monkeypatch.setattr(
        module,
        "submit_canonical_ingress",
        lambda _signal: {
            "inserted": True,
            "raw_message_id": "00000000-0000-0000-0000-000000000001",
        },
    )
    agent_runs: list[str] = []

    def run_hermes(prompt, name, dry_run, on_job_created=None):
        del prompt, dry_run
        agent_runs.append(name)
        if on_job_created is not None:
            on_job_created("job-uncertain")
        return "job-uncertain"

    monkeypatch.setattr(module, "run_hermes", run_hermes)
    monkeypatch.setattr(
        module,
        "_latest_markdown_response",
        lambda _job_id: "Hermes already processed this signal",
    )
    monkeypatch.setattr(
        module,
        "load_cron_job_status",
        lambda _job_id: {
            "last_run_at": "2026-08-19T00:00:00+00:00",
            "last_status": "ok",
            "last_delivery_error": "Telegram send failed: Timed out",
        },
    )
    redelivered: list[str] = []
    monkeypatch.setattr(
        module,
        "redeliver_hermes_response",
        lambda response: (
            redelivered.append(response)
            or {"success": True, "message_id": "2161"}
        ),
    )
    monkeypatch.setattr(module, "remove_cron_job", lambda _job_id: True)
    signal = _signal("-1002136478186", "sig-delivery-uncertain")

    first = module.attempt_batch_delivery(
        [signal],
        dry_run=False,
        now_ts=1000,
    )
    early = module.attempt_batch_delivery(
        [signal],
        dry_run=False,
        now_ts=1001,
    )
    recovered = module.attempt_batch_delivery(
        [signal],
        dry_run=False,
        now_ts=1000 + module.DELIVERY_UNCERTAIN_GRACE_SECONDS,
    )

    assert [first, early, recovered] == ["retry", "retry", "success"]
    assert len(agent_runs) == 1
    assert redelivered == ["Hermes already processed this signal"]
    pending = module.load_pending_delivery()
    assert pending["redelivery_attempts"] == 1
    assert pending["telegram_message_id"] == "2161"


def test_uncertain_redelivery_timeout_stops_automatic_retries(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    pending_path = tmp_path / "pending.json"
    monkeypatch.setattr(module, "PENDING_STATE", str(pending_path))
    module.save_pending_delivery(
        {
            "batch_key": "2026-08-11T00:00:00+00:00|sig-uncertain",
            "last_cursor": "2026-08-11T00:00:00+00:00|sig-uncertain",
            "job_name": "signal-sig-uncertain",
            "job_id": "job-uncertain",
            "status": "delivery_uncertain",
            "attempts": 0,
            "redelivery_attempts": 0,
            "delivery_origin_uncertain": True,
            "delivery_uncertain_at": 1000,
            "retry_after": 1000,
        }
    )
    monkeypatch.setattr(
        module,
        "_latest_markdown_response",
        lambda _job_id: "processed",
    )
    monkeypatch.setattr(
        module,
        "load_cron_job_status",
        lambda _job_id: {
            "last_run_at": "2026-08-19T00:00:00+00:00",
            "last_status": "ok",
            "last_delivery_error": "Telegram send failed: Timed out",
        },
    )
    redeliveries: list[str] = []
    monkeypatch.setattr(
        module,
        "redeliver_hermes_response",
        lambda response: (
            redeliveries.append(response)
            or {
                "success": False,
                "error": "Telegram redelivery timed out",
            }
        ),
    )
    signal = _signal("-1002136478186", "sig-uncertain")

    first = module.attempt_batch_delivery(
        [signal],
        dry_run=False,
        now_ts=1300,
    )
    second = module.attempt_batch_delivery(
        [signal],
        dry_run=False,
        now_ts=1600,
    )

    assert [first, second] == ["pending", "pending"]
    assert redeliveries == ["processed"]
    pending = module.load_pending_delivery()
    assert pending["status"] == "delivery_manual_review"
    assert pending["redelivery_attempts"] == 1


def test_run_hermes_retains_one_shot_job_for_delivery_confirmation(
    monkeypatch,
) -> None:
    module = _load_feeder()
    commands: list[list[str]] = []

    class Result:
        def __init__(self, stdout: str = "") -> None:
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def run(command, **_kwargs):
        commands.append(command)
        if command[1:3] == ["cron", "create"]:
            return Result("Created job: job-retained\n")
        return Result("completed\n")

    monkeypatch.setattr(module.subprocess, "run", run)

    job_id = module.run_hermes(
        "review only",
        name="signal-retained",
        dry_run=False,
    )

    assert job_id == "job-retained"
    create_command = commands[0]
    repeat_index = create_command.index("--repeat")
    assert create_command[repeat_index + 1] == "2"
    assert create_command[-2:] == ["--skill", "v3-trader"]


def test_load_cron_job_status_reads_retained_job(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job-retained",
                        "last_status": "ok",
                        "last_delivery_error": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "HERMES_JOBS_FILE", str(jobs_path))

    job = module.load_cron_job_status("job-retained")

    assert isinstance(job, dict)
    assert job["last_status"] == "ok"
    assert module.load_cron_job_status("missing") is None


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


def test_route_addon_accepts_zero_and_rejects_negative(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "addon-validation.db"
    conn = _create_trading_db(db_path)
    _insert_account(
        conn,
        "credential-a",
        addon=0.0,
        execution_account_id="account-a",
    )
    _insert_account(
        conn,
        "credential-b",
        addon=-1.0,
        execution_account_id="account-b",
    )
    conn.execute(
        "INSERT INTO channel_routing (channel_id, target_account_id) VALUES (?, ?)",
        ("-10006", "credential-a"),
    )
    conn.execute(
        "INSERT INTO channel_routing (channel_id, target_account_id) VALUES (?, ?)",
        ("-10007", "credential-b"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(module, "WATCHER_TRADING_DB", str(db_path))

    route = module.resolve_channel_account("-10006")
    assert route.risk_capital_addon == 0.0
    with pytest.raises(module.ChannelRouteError, match="risk_capital_addon"):
        module.resolve_channel_account("-10007")


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


def test_route_schema_requires_dynamic_risk_capital_addon(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_feeder()
    db_path = tmp_path / "missing-addon.db"
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
        match="risk_capital_addon",
    ):
        module.resolve_channel_account("-10005")
