"""G2-T1: persist-first intake, per-account shadow queue, legacy live writer intact."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest

import signal_queue
import worker
from connection import transaction
from hermes_client import HermesTimeoutError
from repository import ingest_raw_message_with_outbox

REPO_ROOT = Path(__file__).resolve().parents[2]
FEEDER_PATH = REPO_ROOT / "scripts" / "hermes_signal_feeder.py"

JIAN_GUO_CHANNEL = "-1001111111111"
FENG_GE_CHANNEL = "-1002222222222"


def _load_feeder(name: str = "g2_hermes_signal_feeder"):
    spec = importlib.util.spec_from_file_location(name, FEEDER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load feeder: {FEEDER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MockHermesClient:
    def __init__(self, *, candidate=None, raises=None, block=None):
        self._candidate = candidate
        self._raises = raises
        self._block = block
        self.calls = 0

    def analyze(self, request, *, timeout):
        self.calls += 1
        if self._block is not None:
            self._block.wait(timeout=10)
        if self._raises is not None:
            raise self._raises
        return self._candidate


class MockMediaLoader:
    def load(self, object_key):
        return "image/png", "Zg=="


class StaticSnapshot:
    def current(self):
        return {
            "data_source": "postgres_projection",
            "snapshot_id": str(uuid4()),
            "generated_at": "2026-09-13T10:00:00Z",
            "last_execution_event_at": None,
            "projection_lag_ms": 0,
            "stale": False,
            "missing_nodes": [],
            "reconciliation_state": "healthy",
            "data": {"accounts": [], "orders": [], "positions": []},
        }


def valid_candidate(action="open_position"):
    return {
        "classification": {
            "message_type": "new_signal",
            "action": action,
            "ambiguous": False,
            "ambiguity_reasons": [],
        },
        "intent": {
            "account_scope": "unassigned",
            "target_account_id": None,
            "target_position_id": None,
            "instrument_symbol": "BTCUSDT",
            "side": "long",
            "entry": {
                "type": "market",
                "price": None,
                "price_min": None,
                "price_max": None,
            },
            "stop_loss": None,
            "take_profits": [],
            "leverage": None,
            "valid_until": None,
        },
        "evidence": [],
        "confidence": 0.8,
    }


def _create_trading_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE account_configs (
            account_id TEXT PRIMARY KEY,
            account_type TEXT NOT NULL DEFAULT 'main',
            parent_account_id TEXT NOT NULL DEFAULT '',
            risk_capital_addon REAL NOT NULL DEFAULT 0,
            execution_account_id TEXT NOT NULL,
            is_enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE channel_routing (
            channel_id TEXT PRIMARY KEY,
            target_account_id TEXT NOT NULL
        );
        CREATE TABLE telegram_messages (
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
        );
        """
    )
    conn.execute(
        "INSERT INTO account_configs (account_id, execution_account_id) "
        "VALUES ('credential-c', 'account-c')"
    )
    conn.execute(
        "INSERT INTO account_configs (account_id, execution_account_id) "
        "VALUES ('credential-d', 'account-d')"
    )
    conn.execute(
        "INSERT INTO channel_routing (channel_id, target_account_id) VALUES (?, ?)",
        (JIAN_GUO_CHANNEL, "credential-c"),
    )
    conn.execute(
        "INSERT INTO channel_routing (channel_id, target_account_id) VALUES (?, ?)",
        (FENG_GE_CHANNEL, "credential-d"),
    )
    conn.commit()
    return conn


def _insert_msg(
    conn: sqlite3.Connection,
    *,
    msg_id: int,
    channel_id: str,
    chat_title: str,
    text: str,
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO telegram_messages (
            msg_id, channel_id, chat_title, sender, text, created_at
        ) VALUES (?, ?, ?, 'sender', ?, ?)
        """,
        (msg_id, channel_id, chat_title, text, created_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _ingress_payload(**overrides):
    received = _now_iso()
    payload = {
        "source": "telegram",
        "channel_id": JIAN_GUO_CHANNEL,
        "source_message_id": "6896",
        "source_version": "v1",
        "source_received_at": received,
        "author_id": "1",
        "message_text": "BTC long",
        "message_kind": "text",
        "update_id": "upd-1",
        "account_id": "account-c",
        "action": "evaluate",
        "raw_payload": {"receive_ts": received, "source_ts": None},
        "media_assets": [],
    }
    payload.update(overrides)
    return payload


def _count(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def _labeled_fixture_snapshot():
    return {
        "fixture_provenance": (
            "tests/hermes/test_g2_signal_intake.py local rehearsal"
        ),
        "data_source": "local_rehearsal_fixture",
        "stale": False,
        "reconciliation_state": "fixture",
        "data": {
            "accounts": [
                {
                    "account_id": "account-d",
                    "note": "labeled local rehearsal fixture; not a live projection",
                }
            ],
            "orders": [],
            "positions": [],
        },
    }


def _write_fixture_media(root: Path) -> tuple[bytes, str]:
    media_bytes = b"g2-local-rehearsal-media"
    media_path = root / "signals" / "6896.png"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(media_bytes)
    return media_bytes, hashlib.sha256(media_bytes).hexdigest()


def _ingress_with_fixture_media(root: Path, **overrides):
    media_bytes, sha256 = _write_fixture_media(root)
    payload = _ingress_payload(
        message_kind="photo",
        media_assets=[
            {
                "sha256": sha256,
                "object_key": "signals/6896.png",
                "mime": "image/png",
                "download_status": "downloaded",
            }
        ],
        **overrides,
    )
    return payload, media_bytes, sha256


def _start_hermes_http_mock(candidate):
    captured: list[dict] = []

    class HermesHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            raw = self.rfile.read(length)
            captured.append(json.loads(raw.decode("utf-8")))
            body = json.dumps(
                {"choices": [{"message": {"content": json.dumps(candidate)}}]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), HermesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, captured


def _shadow_cli_env(database_url: str, server) -> dict[str, str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["HERMES_API_URL"] = f"http://127.0.0.1:{server.server_address[1]}"
    env["HERMES_API_KEY"] = "test-key"
    env["HERMES_MODEL"] = "test-model"
    env.pop("INGRESS_DATABASE_URL", None)
    return env


def _run_shadow_cli(args, *, env, timeout=30):
    return subprocess.run(
        [sys.executable, worker.__file__, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _captured_context(captured: list[dict]) -> dict:
    body = captured[-1]
    for part in body["messages"][1]["content"]:
        text = str(part.get("text") or "")
        if part.get("type") == "text" and text.startswith("context_json:"):
            return json.loads(text.split("\n", 1)[1])
    raise AssertionError("Hermes request missing context_json")


def _captured_has_image(captured: list[dict]) -> bool:
    body = captured[-1]
    return any(
        part.get("type") == "image_url"
        for part in body["messages"][1]["content"]
    )


def test_claim_contract_is_not_node_writer_fence():
    assert "claim_token" in signal_queue.CLAIM_CONTRACT["claim_fields"]
    assert "attempt" in signal_queue.CLAIM_CONTRACT["claim_fields"]
    assert signal_queue.CLAIM_CONTRACT["source_identity"] == (
        "source_platform",
        "channel_id",
        "source_message_id",
        "edit_version",
        "account_id",
    )
    assert "x_lease_fencing_token" in signal_queue.CLAIM_CONTRACT["node_writer_fence_headers"]
    src = Path(signal_queue.__file__).read_text(encoding="utf-8")
    assert "x_redis_fencing_epoch" in src
    assert "Do not reuse attempt as runtime_generation" in src or "not runtime_generation" in src


def test_operator_query_is_select_only_on_signal_tasks(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                has_table_privilege('trader_v3_operator_query', 'signal_dispatch_tasks', 'SELECT'),
                has_table_privilege('trader_v3_operator_query', 'signal_dispatch_tasks', 'INSERT'),
                has_table_privilege('trader_v3_operator_query', 'signal_dispatch_tasks', 'UPDATE')
            """
        )
        select_priv, insert_priv, update_priv = cur.fetchone()
    assert select_priv is True
    assert insert_priv is False
    assert update_priv is False


def test_ingress_enqueues_pending_without_consuming_outbox(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    result = ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    assert result["inserted"] is True
    assert result["task_id"]
    assert _count(db_conn, "SELECT count(*) FROM signal_dispatch_tasks") == 1
    assert _count(
        db_conn,
        "SELECT count(*) FROM outbox_events WHERE status = 'pending'",
    ) == 1
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT disposition, disposition_reason, status FROM signal_dispatch_tasks"
        )
        disposition, reason, status = cur.fetchone()
    assert status == "pending"
    assert disposition == "pending"
    assert reason == "shadow_queued_legacy_still_live"


def test_duplicate_identity_does_not_second_dispatch(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    first = ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    second = ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    assert second["inserted"] is False
    assert second["raw_message_id"] == first["raw_message_id"]
    assert _count(db_conn, "SELECT count(*) FROM signal_dispatch_tasks") == 1
    assert _count(db_conn, "SELECT count(*) FROM outbox_events") == 1


def test_edit_version_is_related_evaluation(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    first = ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    edited = ingest_raw_telegram_update(
        _ingress_payload(
            source_version="edit:2",
            message_text="BTC long edited SL",
            update_id="upd-2",
        ),
        migrated_db,
    )
    assert edited["inserted"] is True
    assert edited["raw_message_id"] != first["raw_message_id"]
    assert edited["related_task_id"] == first["task_id"]
    assert _count(db_conn, "SELECT count(*) FROM signal_dispatch_tasks") == 2


def test_source_ts_is_not_receive_or_ingested_at(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT source_received_at, ingested_at, raw_payload FROM raw_messages"
        )
        source_received_at, ingested_at, raw_payload = cur.fetchone()
    assert raw_payload["source_ts"] is None
    assert raw_payload["source_ts_unknown"] is True
    assert raw_payload["receive_ts"]
    assert raw_payload["source_received_at_is_not_source_ts"] is True
    assert ingested_at is not None
    assert raw_payload.get("persist_at")


def test_legacy_process_one_still_claims_g2_ingested_outbox(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    result = worker.process_one(
        db_conn,
        worker_id="legacy",
        client=MockHermesClient(candidate=valid_candidate()),
        media_loader=MockMediaLoader(),
        snapshot_provider=StaticSnapshot(),
        model_version="mock",
        timeout=5,
    )
    assert result.status == "succeeded"
    assert _count(
        db_conn,
        "SELECT count(*) FROM outbox_events WHERE status = 'published'",
    ) == 1
    assert _count(
        db_conn,
        "SELECT count(*) FROM signal_dispatch_tasks WHERE status = 'pending'",
    ) == 1


def test_shadow_mode_does_not_consume_legacy_outbox(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    shadow = worker.process_one(
        db_conn,
        worker_id="shadow",
        client=MockHermesClient(candidate=valid_candidate()),
        media_loader=MockMediaLoader(),
        snapshot_provider=StaticSnapshot(),
        model_version="mock",
        timeout=5,
        mode="shadow",
    )
    assert shadow.status == "shadow_dispatched"
    assert shadow.operator_submitted is False
    assert _count(
        db_conn,
        "SELECT count(*) FROM outbox_events WHERE status = 'pending'",
    ) == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT shadow_result, disposition FROM signal_dispatch_tasks")
        payload, disposition = cur.fetchone()
    assert disposition == "pending"
    assert payload["semantic"]["action"] == "open_position"
    assert payload["semantic"]["client_ref"]
    assert payload["semantic"]["stable_action_or_leg_id"]
    assert payload["semantic"]["source_identity"]["account_id"] == "account-c"
    decision = payload["semantic"]["decision"]
    assert decision["classification"]["action"] == "open_position"
    assert decision["intent"]["instrument_symbol"] == "BTCUSDT"
    assert decision["intent"]["side"] == "long"
    assert decision["intent"]["entry"]["type"] == "market"
    assert "stop_loss" in decision["intent"]
    assert "take_profits" in decision["intent"]
    assert "quantity" not in decision["intent"]
    assert "notional" not in decision["intent"]
    assert payload["semantic"]["stable_operation_identity"]["client_ref"]
    assert payload["stages"]["model_started_at"]
    assert payload["stages"]["model_finished_at"]
    assert payload["outbox_published"] is False
    assert "classified" not in payload
    legacy = worker.process_one(
        db_conn,
        worker_id="legacy",
        client=MockHermesClient(candidate=valid_candidate()),
        media_loader=MockMediaLoader(),
        snapshot_provider=StaticSnapshot(),
        model_version="mock",
        timeout=5,
    )
    assert legacy.status == "succeeded"


def test_shadow_active_run_does_not_block_legacy_claim(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    shadow = signal_queue.claim_signal_task(db_conn, "shadow-w", lease_seconds=60)
    assert shadow is not None
    import claims as hermes_claims

    legacy = hermes_claims.claim(db_conn, "legacy-w", lease_seconds=30)
    assert legacy is not None
    assert legacy.raw_message_id == shadow.raw_message_id
    assert legacy.processing_run_id != shadow.processing_run_id
    assert legacy.processing_purpose == "legacy"
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT processing_purpose, status
            FROM message_processing_runs
            WHERE status IN ('started', 'processing')
            ORDER BY processing_purpose
            """
        )
        rows = cur.fetchall()
    purposes = {row[0] for row in rows}
    assert purposes == {"legacy", "shadow"}


def test_legacy_active_run_does_not_block_shadow_claim(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update
    import claims as hermes_claims

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    legacy = hermes_claims.claim(db_conn, "legacy-w", lease_seconds=60)
    assert legacy is not None
    shadow = signal_queue.claim_signal_task(db_conn, "shadow-w", lease_seconds=30)
    assert shadow is not None
    assert shadow.raw_message_id == legacy.raw_message_id
    assert shadow.processing_run_id != legacy.processing_run_id


def test_same_account_concurrent_claims_serialize(migrated_db):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(source_message_id="1"), migrated_db)
    ingest_raw_telegram_update(
        _ingress_payload(source_message_id="2", update_id="upd-2"),
        migrated_db,
    )
    barrier = threading.Barrier(2)
    results: list[signal_queue.SignalTask | None] = [None, None]
    errors: list[BaseException | None] = [None, None]

    def worker_claim(index: int) -> None:
        conn = psycopg2.connect(migrated_db)
        try:
            barrier.wait(timeout=10)
            results[index] = signal_queue.claim_signal_task(
                conn, f"w{index}", lease_seconds=30
            )
        except BaseException as exc:  # noqa: BLE001
            errors[index] = exc
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker_claim, args=(0,)),
        threading.Thread(target=worker_claim, args=(1,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert errors == [None, None]
    claimed = [task for task in results if task is not None]
    assert len(claimed) == 1
    observer = psycopg2.connect(migrated_db)
    try:
        with observer.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM signal_dispatch_tasks
                WHERE status = 'leased' AND lease_expires_at > now()
                """
            )
            assert cur.fetchone()[0] == 1
    finally:
        observer.close()


def test_other_account_claimable_during_long_lease(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    ingest_raw_telegram_update(
        _ingress_payload(
            channel_id=FENG_GE_CHANNEL,
            source_message_id="4374",
            account_id="account-d",
            update_id="upd-feng",
        ),
        migrated_db,
    )
    held = signal_queue.claim_signal_task(
        db_conn, "pfill-c", lease_seconds=600
    )
    other = signal_queue.claim_signal_task(
        db_conn, "signal-d", lease_seconds=30
    )
    assert held is not None and held.account_id == "account-c"
    assert other is not None and other.account_id == "account-d"


def test_complete_rejects_expired_lease_without_reclaim(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    task = signal_queue.claim_signal_task(db_conn, "w1", lease_seconds=30)
    assert task is not None
    with db_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE signal_dispatch_tasks
            SET lease_expires_at = now() - interval '1 second'
            WHERE task_id = %s
            """,
            (task.task_id,),
        )
        cur.execute(
            """
            UPDATE message_processing_runs
            SET lease_expires_at = now() - interval '1 second'
            WHERE processing_run_id = %s
            """,
            (task.processing_run_id,),
        )
    db_conn.commit()
    with pytest.raises(signal_queue.StaleClaimError):
        signal_queue.complete_signal_task(
            db_conn,
            task.task_id,
            task.claim_token,
            status="shadow_dispatched",
            shadow_result={"semantic": {"action": "open_position"}},
        )
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM signal_dispatch_tasks WHERE task_id = %s",
            (task.task_id,),
        )
        assert cur.fetchone()[0] == "leased"


def test_stale_token_rejected_after_reclaim(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    first = signal_queue.claim_signal_task(db_conn, "w1", lease_seconds=1)
    assert first is not None
    time.sleep(1.2)
    second = signal_queue.claim_signal_task(db_conn, "w2", lease_seconds=30)
    assert second is not None
    assert second.claim_token != first.claim_token
    with pytest.raises(signal_queue.StaleClaimError):
        signal_queue.complete_signal_task(
            db_conn,
            first.task_id,
            first.claim_token,
            status="failed",
            disposition_reason="stale",
        )
    semantic = {"action": "open_position", "client_ref": "tg-sig"}
    done = signal_queue.shadow_dispatch(db_conn, second, semantic=semantic)
    assert done.status == "shadow_dispatched"


def test_expired_signal_records_disposition(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(
        _ingress_payload(source_received_at="2020-01-01T00:00:00Z"),
        migrated_db,
    )
    claimed = signal_queue.claim_signal_task(db_conn, "w-exp", lease_seconds=30)
    assert claimed is None
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, disposition, disposition_reason FROM signal_dispatch_tasks"
        )
        status, disposition, reason = cur.fetchone()
    assert status == "expired"
    assert disposition in {"expired", "rejected"}
    assert reason == "signal_ttl_exceeded"


def test_hol_later_channel_persists_while_earlier_model_blocked(
    tmp_path, migrated_db, db_conn, monkeypatch
):
    feeder = _load_feeder("g2_hol")
    db_path = tmp_path / "watcher-trading.db"
    sqlite = _create_trading_db(db_path)
    jian_id = _insert_msg(
        sqlite,
        msg_id=6896,
        channel_id=JIAN_GUO_CHANNEL,
        chat_title="坚果",
        text="坚果 BTC",
        created_at=_now_iso(),
    )
    monkeypatch.setattr(feeder, "WATCHER_TRADING_DB", str(db_path))
    monkeypatch.setattr(feeder, "PERSIST_STATE", str(tmp_path / "persist.cursor"))
    monkeypatch.setattr(feeder, "STATE", str(tmp_path / "live.cursor"))
    monkeypatch.setenv("INGRESS_DATABASE_URL", migrated_db)
    feeder.save_persist_cursor("telegram_messages:0")
    first_cursor, first_count = feeder.persist_new_watcher_messages(
        sqlite, "telegram_messages:0", dry_run=False
    )
    assert first_count == 1
    blocked = signal_queue.claim_signal_task(
        db_conn, "blocked-c", lease_seconds=600
    )
    assert blocked is not None
    assert blocked.account_id == "account-c"
    _insert_msg(
        sqlite,
        msg_id=4374,
        channel_id=FENG_GE_CHANNEL,
        chat_title="峰哥",
        text="峰哥 ETH",
        created_at=_now_iso(),
    )
    later_cursor, later_count = feeder.persist_new_watcher_messages(
        sqlite, f"telegram_messages:{jian_id}", dry_run=False
    )
    assert later_count == 1
    assert later_cursor is not None
    other = signal_queue.claim_signal_task(db_conn, "feng-d", lease_seconds=30)
    assert other is not None
    assert other.account_id == "account-d"
    channels = set()
    with db_conn.cursor() as cur:
        cur.execute("SELECT channel_id FROM raw_messages")
        channels = {row[0] for row in cur.fetchall()}
    assert JIAN_GUO_CHANNEL in channels
    assert FENG_GE_CHANNEL in channels


def test_feeder_once_persists_before_live_cron(
    tmp_path, migrated_db, monkeypatch
):
    feeder = _load_feeder("g2_once")
    db_path = tmp_path / "watcher-trading.db"
    sqlite = _create_trading_db(db_path)
    _insert_msg(
        sqlite,
        msg_id=4374,
        channel_id=FENG_GE_CHANNEL,
        chat_title="峰哥",
        text="峰哥 BTC",
        created_at=_now_iso(),
    )
    sqlite.close()
    monkeypatch.setattr(feeder, "WATCHER_TRADING_DB", str(db_path))
    monkeypatch.setattr(feeder, "STATE", str(tmp_path / "live.cursor"))
    monkeypatch.setattr(feeder, "PERSIST_STATE", str(tmp_path / "persist.cursor"))
    monkeypatch.setattr(feeder, "PENDING_STATE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(feeder, "QUARANTINE_STATE", str(tmp_path / "quarantine.json"))
    monkeypatch.setattr(feeder, "LOCK", str(tmp_path / "feeder.lock"))
    monkeypatch.setattr(feeder, "V3_MEDIA", str(tmp_path / "media"))
    monkeypatch.setattr(feeder, "CHANNEL_CONTEXT_DIR", str(tmp_path / "ctx"))
    monkeypatch.setattr(feeder, "compress_channel_contexts", lambda: None)
    monkeypatch.setattr(feeder, "HOLD_SECONDS", 0)
    Path(tmp_path / "live.cursor").write_text("telegram_messages:0", encoding="utf-8")
    Path(tmp_path / "persist.cursor").write_text("telegram_messages:0", encoding="utf-8")
    monkeypatch.setenv("INGRESS_DATABASE_URL", migrated_db)
    cron_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        cron_calls.append(list(cmd))

        class Result:
            returncode = 0
            stdout = "Created job: job-live\n"
            stderr = ""

        return Result()

    monkeypatch.setattr(feeder.subprocess, "run", fake_run)
    monkeypatch.setattr(feeder, "_latest_markdown_response", lambda _job: "ok")
    monkeypatch.setattr(
        feeder,
        "load_cron_job_status",
        lambda _job: {
            "last_run_at": "2026-09-13T10:09:00+00:00",
            "last_status": "ok",
            "last_delivery_error": None,
        },
    )
    monkeypatch.setattr(feeder, "remove_cron_job", lambda _job: True)
    monkeypatch.setattr(sys, "argv", ["hermes_signal_feeder.py", "--once"])
    feeder.main()
    persist_text = Path(tmp_path / "persist.cursor").read_text(encoding="utf-8")
    assert persist_text.startswith("telegram_messages:")
    conn = psycopg2.connect(migrated_db)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM raw_messages")
            assert cur.fetchone()[0] == 1
            cur.execute(
                """
                SELECT account_id, count(*)
                FROM signal_dispatch_tasks
                GROUP BY account_id
                """
            )
            rows = cur.fetchall()
            assert rows == [("account-d", 1)]
            assert rows[0][0] != "unassigned"
    finally:
        conn.close()
    assert any("cron" in part for cmd in cron_calls for part in cmd)


def test_signal_worker_sources_have_no_telegram_token():
    for path in (
        Path(signal_queue.__file__),
        Path(worker.__file__),
        Path(claims_path()),
    ):
        text = path.read_text(encoding="utf-8")
        assert "TELEGRAM_BOT_TOKEN" not in text
        assert "BOT_TOKEN" not in text


def claims_path() -> Path:
    return REPO_ROOT / "services" / "hermes-worker" / "queue" / "claims.py"


def test_open_ttl_override_is_honored(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    claimed = signal_queue.claim_signal_task(
        db_conn, "ttl", lease_seconds=30, open_ttl_seconds=0
    )
    assert claimed is None
    with db_conn.cursor() as cur:
        cur.execute("SELECT disposition_reason FROM signal_dispatch_tasks")
        assert cur.fetchone()[0] == "signal_ttl_exceeded"


def test_http_401_preserves_persist_backlog(tmp_path, monkeypatch):
    feeder = _load_feeder("g2_http401")
    db_path = tmp_path / "watcher-trading.db"
    sqlite = _create_trading_db(db_path)
    _insert_msg(
        sqlite,
        msg_id=1,
        channel_id=JIAN_GUO_CHANNEL,
        chat_title="坚果",
        text="a",
        created_at=_now_iso(),
    )
    _insert_msg(
        sqlite,
        msg_id=2,
        channel_id=FENG_GE_CHANNEL,
        chat_title="峰哥",
        text="b",
        created_at=_now_iso(),
    )
    monkeypatch.setattr(feeder, "WATCHER_TRADING_DB", str(db_path))
    monkeypatch.setattr(feeder, "PERSIST_STATE", str(tmp_path / "persist.cursor"))
    monkeypatch.setattr(feeder, "QUARANTINE_STATE", str(tmp_path / "quarantine.json"))
    monkeypatch.setattr(
        feeder,
        "submit_canonical_ingress",
        lambda *_a, **_k: (_ for _ in ()).throw(
            feeder.CanonicalIngressError("canonical ingress returned HTTP 401: unauthorized")
        ),
    )
    cursor, count = feeder.persist_new_watcher_messages(
        sqlite, "telegram_messages:0", dry_run=False
    )
    assert count == 0
    assert cursor == "telegram_messages:0"
    assert not Path(tmp_path / "quarantine.json").exists()


def test_malformed_row_quarantines_reason_and_later_account_persists(
    tmp_path, monkeypatch
):
    feeder = _load_feeder("g2_poison")
    db_path = tmp_path / "watcher-trading.db"
    sqlite = _create_trading_db(db_path)
    bad_id = _insert_msg(
        sqlite,
        msg_id=1,
        channel_id=JIAN_GUO_CHANNEL,
        chat_title="坚果",
        text="bad",
        created_at=_now_iso(),
    )
    good_id = _insert_msg(
        sqlite,
        msg_id=2,
        channel_id=FENG_GE_CHANNEL,
        chat_title="峰哥",
        text="good",
        created_at=_now_iso(),
    )
    monkeypatch.setattr(feeder, "WATCHER_TRADING_DB", str(db_path))
    monkeypatch.setattr(feeder, "PERSIST_STATE", str(tmp_path / "persist.cursor"))
    monkeypatch.setattr(feeder, "QUARANTINE_STATE", str(tmp_path / "quarantine.json"))
    poison_reason = "HTTP 400: invalid_payload missing channel_id"

    def submit(signal, extra_fields=None):
        channel = feeder._signal_channel(signal)
        if channel == JIAN_GUO_CHANNEL:
            raise feeder.CanonicalIngressError(poison_reason)
        return {
            "inserted": True,
            "raw_message_id": "00000000-0000-0000-0000-0000000000aa",
        }

    monkeypatch.setattr(feeder, "submit_canonical_ingress", submit)
    cursor, count = feeder.persist_new_watcher_messages(
        sqlite, "telegram_messages:0", dry_run=False
    )
    assert count == 1
    assert cursor == f"telegram_messages:{good_id}"
    quarantine = feeder.load_quarantine()
    entry = quarantine["entries"][f"telegram_messages:{bad_id}"]
    assert entry["reason_code"] == "ingress_poison"
    assert entry["reason"] == poison_reason


def test_supplied_source_ts_is_preserved():
    feeder = _load_feeder("g2_source_ts")
    signal = {
        "watcher_message_id": 1,
        "signal_id": "sig",
        "received_at": "2026-09-13T10:00:00+00:00",
        "source_version": "v1",
        "payload": json.dumps(
            {
                "source_channel_id": JIAN_GUO_CHANNEL,
                "source_message_id": "1",
                "raw_text": "BTC",
                "source_ts": "2026-09-13T09:59:00+00:00",
            }
        ),
    }
    payload = feeder.canonical_ingress_payload(signal)
    assert payload["raw_payload"]["source_ts"] == "2026-09-13T09:59:00+00:00"
    assert payload["raw_payload"]["source_ts_unknown"] is False


def test_shadow_never_marks_filled(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    task = signal_queue.claim_signal_task(db_conn, "w", lease_seconds=30)
    with pytest.raises(ValueError, match="must not mark filled"):
        signal_queue.complete_signal_task(
            db_conn,
            task.task_id,
            task.claim_token,
            status="shadow_dispatched",
            disposition="filled",
        )


def test_model_call_closes_read_transaction(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update
    from psycopg2.extensions import TRANSACTION_STATUS_IDLE

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    seen = {}

    class Client(MockHermesClient):
        def analyze(self, request, *, timeout):
            seen["status"] = db_conn.get_transaction_status()
            return super().analyze(request, timeout=timeout)

    result = worker.process_one(
        db_conn,
        worker_id="shadow",
        client=Client(candidate=valid_candidate()),
        media_loader=MockMediaLoader(),
        snapshot_provider=StaticSnapshot(),
        mode="shadow",
        timeout=5,
    )
    assert result.status == "shadow_dispatched"
    assert seen["status"] == TRANSACTION_STATUS_IDLE


def test_blocked_shadow_worker_does_not_block_other_account(migrated_db):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    ingest_raw_telegram_update(
        _ingress_payload(
            channel_id=FENG_GE_CHANNEL,
            source_message_id="4374",
            account_id="account-d",
            update_id="upd-feng",
        ),
        migrated_db,
    )
    c_blocked = threading.Event()
    c_release = threading.Event()
    d_done = threading.Event()
    results: dict[str, str] = {}

    class BlockingClient(MockHermesClient):
        def analyze(self, request, *, timeout):
            c_blocked.set()
            assert c_release.wait(timeout=30)
            return super().analyze(request, timeout=timeout)

    def run_c() -> None:
        conn = psycopg2.connect(migrated_db)
        try:
            result = worker.process_one(
                conn,
                worker_id="worker-c",
                client=BlockingClient(candidate=valid_candidate()),
                media_loader=MockMediaLoader(),
                snapshot_provider=StaticSnapshot(),
                mode="shadow",
                timeout=5,
                lease_seconds=60,
            )
            results["c"] = result.status
        finally:
            conn.close()

    def run_d() -> None:
        assert c_blocked.wait(timeout=5)
        conn = psycopg2.connect(migrated_db)
        try:
            result = worker.process_one(
                conn,
                worker_id="worker-d",
                client=MockHermesClient(candidate=valid_candidate()),
                media_loader=MockMediaLoader(),
                snapshot_provider=StaticSnapshot(),
                mode="shadow",
                timeout=5,
                lease_seconds=60,
            )
            results["d"] = result.status
            d_done.set()
        finally:
            conn.close()

    t_c = threading.Thread(target=run_c)
    t_d = threading.Thread(target=run_d)
    t_c.start()
    t_d.start()
    try:
        assert d_done.wait(timeout=5)
        assert results.get("d") == "shadow_dispatched"
        assert "c" not in results
        assert t_c.is_alive()
    finally:
        c_release.set()
        t_c.join(timeout=10)
        t_d.join(timeout=10)
    assert results.get("c") == "shadow_dispatched"


def test_run_shadow_consumer_entrypoint(migrated_db, db_conn):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    result = worker.run_shadow_consumer(
        db_conn,
        worker_id="rehearsal",
        client=MockHermesClient(candidate=valid_candidate()),
        snapshot_provider=StaticSnapshot(),
        media_loader=MockMediaLoader(),
        once=True,
    )
    assert result.status == "shadow_dispatched"
    assert result.operator_submitted is False
    assert callable(worker.main)


def test_fixture_snapshot_keeps_provenance_and_refuses_projection_impersonation(
    tmp_path,
):
    labeled = tmp_path / "labeled.json"
    labeled.write_text(json.dumps(_labeled_fixture_snapshot()), encoding="utf-8")
    provider = worker.FixtureSnapshotProvider(labeled)
    snapshot = provider.current()
    assert snapshot["fixture_provenance"].startswith("tests/hermes/")
    assert snapshot["data_source"] == "local_rehearsal_fixture"
    unlabeled = tmp_path / "unlabeled.json"
    unlabeled.write_text(
        json.dumps({"data_source": "postgres_projection", "stale": False}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="fixture_provenance"):
        worker.FixtureSnapshotProvider(unlabeled)
    forged = tmp_path / "forged.json"
    forged.write_text(
        json.dumps(
            {
                "fixture_provenance": "unit",
                "data_source": "postgres_projection",
                "stale": False,
                "reconciliation_state": "healthy",
                "data": {"accounts": [], "orders": [], "positions": []},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="postgres_projection"):
        worker.FixtureSnapshotProvider(forged)


def test_shadow_accepts_labeled_fixture_and_rejects_unlabeled_non_projection(
    migrated_db, db_conn
):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)

    class LabeledFixture:
        def current(self):
            return _labeled_fixture_snapshot()

    ok = worker.process_shadow_one(
        db_conn,
        worker_id="fixture-ok",
        client=MockHermesClient(candidate=valid_candidate()),
        media_loader=MockMediaLoader(),
        snapshot_provider=LabeledFixture(),
        timeout=5,
    )
    assert ok.status == "shadow_dispatched"

    ingest_raw_telegram_update(
        _ingress_payload(source_message_id="6897", update_id="upd-6897"),
        migrated_db,
    )

    class UnlabeledNonProjection:
        def current(self):
            return {"data_source": "made_up", "stale": False}

    bad = worker.process_shadow_one(
        db_conn,
        worker_id="fixture-bad",
        client=MockHermesClient(candidate=valid_candidate()),
        media_loader=MockMediaLoader(),
        snapshot_provider=UnlabeledNonProjection(),
        timeout=5,
    )
    assert bad.status == "context_unavailable"


def test_shadow_cli_once_uses_labeled_fixture_media_and_http_hermes(
    migrated_db, tmp_path
):
    from ingress.service import ingest_raw_telegram_update

    media_root = tmp_path / "media"
    payload, _media_bytes, _sha = _ingress_with_fixture_media(media_root)
    ingest_raw_telegram_update(payload, migrated_db)
    snap_path = tmp_path / "snapshot.json"
    snap_path.write_text(json.dumps(_labeled_fixture_snapshot()), encoding="utf-8")
    server, captured = _start_hermes_http_mock(valid_candidate())
    env = _shadow_cli_env(migrated_db, server)
    try:
        missing_mode = _run_shadow_cli(
            ["--fixture-snapshot-json", str(snap_path), "--fixture-media-root", str(media_root)],
            env=env,
            timeout=20,
        )
        assert missing_mode.returncode != 0
        both_modes = _run_shadow_cli(
            [
                "--once",
                "--loop",
                "--fixture-snapshot-json",
                str(snap_path),
                "--fixture-media-root",
                str(media_root),
            ],
            env=env,
            timeout=20,
        )
        assert both_modes.returncode != 0
        missing_media_root = _run_shadow_cli(
            ["--once", "--fixture-snapshot-json", str(snap_path)],
            env=env,
            timeout=20,
        )
        assert missing_media_root.returncode != 0
        assert "fixture-media-root" in (missing_media_root.stderr + missing_media_root.stdout)
        proc = _run_shadow_cli(
            [
                "--once",
                "--fixture-snapshot-json",
                str(snap_path),
                "--fixture-media-root",
                str(media_root),
                "--worker-id",
                "cli-shadow",
            ],
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert "shadow_dispatched" in proc.stdout
        assert captured
        context = _captured_context(captured)
        snapshot = context["system_snapshot"]
        assert snapshot["fixture_provenance"].startswith("tests/hermes/")
        assert snapshot["data_source"] == "local_rehearsal_fixture"
        assert snapshot["data_source"] != "postgres_projection"
        assert snapshot["data"]["accounts"][0]["account_id"] == "account-d"
        assert _captured_has_image(captured)
    finally:
        server.shutdown()
        server.server_close()


def test_shadow_cli_rejects_unlabeled_and_projection_impersonation_fixtures(
    migrated_db, tmp_path
):
    server, _captured = _start_hermes_http_mock(valid_candidate())
    env = _shadow_cli_env(migrated_db, server)
    media_root = tmp_path / "media"
    media_root.mkdir()
    try:
        unlabeled = tmp_path / "unlabeled.json"
        unlabeled.write_text(
            json.dumps({"data_source": "postgres_projection", "stale": False}),
            encoding="utf-8",
        )
        forged = _run_shadow_cli(
            [
                "--once",
                "--fixture-snapshot-json",
                str(unlabeled),
                "--fixture-media-root",
                str(media_root),
            ],
            env=env,
            timeout=20,
        )
        assert forged.returncode != 0
        assert "fixture_provenance" in (forged.stderr + forged.stdout)
        impersonation = tmp_path / "impersonation.json"
        impersonation.write_text(
            json.dumps(
                {
                    "fixture_provenance": "forged-live",
                    "data_source": "postgres_projection",
                    "stale": False,
                    "reconciliation_state": "healthy",
                    "data": {"accounts": [], "orders": [], "positions": []},
                }
            ),
            encoding="utf-8",
        )
        impersonated = _run_shadow_cli(
            [
                "--once",
                "--fixture-snapshot-json",
                str(impersonation),
                "--fixture-media-root",
                str(media_root),
            ],
            env=env,
            timeout=20,
        )
        assert impersonated.returncode != 0
        assert "postgres_projection" in (impersonated.stderr + impersonated.stdout)
    finally:
        server.shutdown()
        server.server_close()


def test_shadow_cli_loop_drains_queue_with_labeled_fixture(migrated_db, tmp_path):
    from ingress.service import ingest_raw_telegram_update

    media_root = tmp_path / "media"
    payload, _media_bytes, _sha = _ingress_with_fixture_media(media_root)
    ingest_raw_telegram_update(payload, migrated_db)
    ingest_raw_telegram_update(
        _ingress_payload(
            source_message_id="4374",
            update_id="upd-feng",
            channel_id=FENG_GE_CHANNEL,
            account_id="account-d",
        ),
        migrated_db,
    )
    snap_path = tmp_path / "snapshot.json"
    snap_path.write_text(json.dumps(_labeled_fixture_snapshot()), encoding="utf-8")
    server, captured = _start_hermes_http_mock(valid_candidate())
    env = _shadow_cli_env(migrated_db, server)
    try:
        proc = _run_shadow_cli(
            [
                "--loop",
                "--fixture-snapshot-json",
                str(snap_path),
                "--fixture-media-root",
                str(media_root),
                "--worker-id",
                "cli-loop",
            ],
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert "skipped" in proc.stdout
        assert len(captured) == 2
        conn = psycopg2.connect(migrated_db)
        try:
            dispatched = _count(
                conn,
                "SELECT count(*) FROM signal_dispatch_tasks WHERE status = 'shadow_dispatched'",
            )
        finally:
            conn.close()
        assert dispatched == 2
    finally:
        server.shutdown()
        server.server_close()


def test_shadow_cli_projection_adapters_use_real_snapshot(migrated_db):
    from ingress.service import ingest_raw_telegram_update

    ingest_raw_telegram_update(_ingress_payload(), migrated_db)
    server, captured = _start_hermes_http_mock(valid_candidate())
    env = _shadow_cli_env(migrated_db, server)
    try:
        proc = _run_shadow_cli(
            ["--once", "--projection-adapters", "--worker-id", "cli-projection"],
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert "shadow_dispatched" in proc.stdout
        assert captured
        snapshot = _captured_context(captured)["system_snapshot"]
        assert snapshot["data_source"] == "postgres_projection"
        assert "fixture_provenance" not in snapshot
        assert "balances" in snapshot["data"]
        assert "orders" in snapshot["data"]
        assert "positions" in snapshot["data"]
    finally:
        server.shutdown()
        server.server_close()


def test_http_telegram_raw_shadow_rehearsal(migrated_db, monkeypatch):
    from http.server import ThreadingHTTPServer
    from ingress.http import IngressHTTPHandler

    IngressHTTPHandler.database_url = migrated_db
    IngressHTTPHandler.expected_token = "secret"
    server = ThreadingHTTPServer(("127.0.0.1", 0), IngressHTTPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        feeder = _load_feeder("g2_http_raw")
        monkeypatch.delenv("INGRESS_DATABASE_URL", raising=False)
        monkeypatch.setattr(
            feeder, "INGRESS_URL", f"http://127.0.0.1:{server.server_address[1]}"
        )
        monkeypatch.setattr(feeder, "INGRESS_API_TOKEN", "secret")
        signal = {
            "watcher_message_id": 11,
            "signal_id": "sig",
            "received_at": _now_iso(),
            "source_version": "v1",
            "payload": json.dumps(
                {
                    "source_channel_id": JIAN_GUO_CHANNEL,
                    "source_message_id": "11",
                    "raw_text": "BTC long",
                }
            ),
        }
        result = feeder.submit_canonical_ingress(
            signal, extra_fields={"account_id": "account-c", "action": "evaluate"}
        )
        assert result["raw_message_id"]
        conn = psycopg2.connect(migrated_db)
        try:
            shadow = worker.process_one(
                conn,
                worker_id="http-shadow",
                client=MockHermesClient(candidate=valid_candidate()),
                media_loader=MockMediaLoader(),
                snapshot_provider=StaticSnapshot(),
                mode="shadow",
            )
            assert shadow.status == "shadow_dispatched"
            assert shadow.operator_submitted is False
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
