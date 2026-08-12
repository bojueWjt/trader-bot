from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "hk-deploy-20260803.sh"
ROUTES = (
    (
        "-1002136478186",
        "jiataotx@gmail.com",
        "account-a",
        1.30521817,
    ),
    (
        "-1002198013097",
        "balenwong3@gmail.com",
        "account-b",
        1.0,
    ),
    (
        "-1002189417451",
        "泰山",
        "account-c",
        2.96902319,
    ),
    (
        "-1002193304023",
        "黄山",
        "account-d",
        3.0,
    ),
)


def _gate_source() -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    start = text.index("verify_four_channel_account_mapping() {")
    end = text.index("\n# FOUR_CHANNEL_MAPPING_GATE_END", start)
    return text[start:end]


def _create_database(
    path: Path,
    *,
    routes: tuple[tuple[str, str, str, float], ...] = ROUTES,
    include_enabled: bool = True,
) -> None:
    connection = sqlite3.connect(path)
    try:
        account_schema = """
            CREATE TABLE account_configs (
                account_id TEXT PRIMARY KEY,
                api_key TEXT NOT NULL,
                api_secret TEXT NOT NULL,
                is_testnet INTEGER NOT NULL,
                execution_account_id TEXT NOT NULL,
                risk_capital_multiplier REAL NOT NULL
        """
        if include_enabled:
            account_schema += ",\n                is_enabled INTEGER NOT NULL"
        account_schema += """
            );
            CREATE TABLE channel_routing (
                channel_id TEXT PRIMARY KEY,
                target_account_id TEXT NOT NULL
            );
        """
        connection.executescript(account_schema)
        for channel_id, credential_id, execution_id, multiplier in routes:
            columns = (
                "account_id, api_key, api_secret, is_testnet, "
                "execution_account_id, risk_capital_multiplier"
            )
            values = "?, ?, ?, 0, ?, ?"
            parameters: tuple[object, ...] = (
                credential_id,
                f"private-key-{execution_id}",
                f"private-secret-{execution_id}",
                execution_id,
                multiplier,
            )
            if include_enabled:
                columns += ", is_enabled"
                values += ", 1"
            connection.execute(
                f"INSERT OR IGNORE INTO account_configs ({columns}) "
                f"VALUES ({values})",
                parameters,
            )
            connection.execute(
                "INSERT INTO channel_routing (channel_id, target_account_id) "
                "VALUES (?, ?)",
                (channel_id, credential_id),
            )
        connection.commit()
    finally:
        connection.close()


def _run_gate(
    database_path: Path,
    *,
    evidence_path: Path | None = None,
    schema_mode: str = "strict",
) -> subprocess.CompletedProcess[str]:
    evidence_value = ""
    if evidence_path is not None:
        evidence_value = str(evidence_path)
    source = (
        "set -Eeuo pipefail\n"
        + _gate_source()
        + "\n"
        + f"WATCHER_TRADING_DB={shlex.quote(str(database_path))}\n"
        + "verify_four_channel_account_mapping "
        + shlex.quote(evidence_value)
        + " "
        + shlex.quote(schema_mode)
        + "\n"
    )
    return subprocess.run(
        ["bash", "-c", source],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )


def _update_account(
    database_path: Path,
    credential_account_id: str,
    column: str,
    value: object,
) -> None:
    allowed_columns = {
        "execution_account_id",
        "risk_capital_multiplier",
        "is_enabled",
        "is_testnet",
    }
    assert column in allowed_columns
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            f"UPDATE account_configs SET {column} = ? WHERE account_id = ?",
            (value, credential_account_id),
        )
        connection.commit()
    finally:
        connection.close()


def _insert_route(
    database_path: Path,
    *,
    channel_id: str,
    credential_account_id: str,
    execution_account_id: str,
    is_enabled: int,
    is_testnet: int,
) -> None:
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO account_configs ("
            "account_id, api_key, api_secret, is_testnet, "
            "execution_account_id, risk_capital_multiplier, is_enabled"
            ") VALUES (?, 'extra-key', 'extra-secret', ?, ?, 1.0, ?)",
            (
                credential_account_id,
                is_testnet,
                execution_account_id,
                is_enabled,
            ),
        )
        connection.execute(
            "INSERT INTO channel_routing (channel_id, target_account_id) "
            "VALUES (?, ?)",
            (channel_id, credential_account_id),
        )
        connection.commit()
    finally:
        connection.close()


def test_valid_mapping_writes_durable_redacted_evidence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "watcher-trading.db"
    evidence_path = tmp_path / "evidence" / "mapping.json"
    evidence_path.parent.mkdir(mode=0o700)
    _create_database(database_path)

    result = _run_gate(database_path, evidence_path=evidence_path)

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o400
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == (
        "trader-v3-four-channel-account-mapping/v2"
    )
    assert payload["schema_mode"] == "strict"
    assert payload["enabled_column_mode"] == "explicit"
    assert payload["database"]["quick_check"] == "ok"
    assert payload["database"]["journal_mode"] == "delete"
    database_before = payload["database"]["files_before_read"]
    database_after = payload["database"]["files_after_read"]
    assert database_before["database"]["inode"] == (
        database_after["database"]["inode"]
    )
    assert database_before["wal"]["present"] is False
    assert database_after["shm"]["present"] is False
    mapping = payload["mapping"]
    assert {
        item["channel_id"]: item["execution_account_id"]
        for item in mapping
    } == {
        channel_id: execution_id
        for channel_id, _, execution_id, _ in ROUTES
    }
    evidence_text = evidence_path.read_text(encoding="utf-8")
    assert "private-key-" not in evidence_text
    assert "private-secret-" not in evidence_text
    assert "api_key" not in evidence_text
    assert "api_secret" not in evidence_text


def test_wal_and_shm_metadata_are_recorded(tmp_path: Path) -> None:
    database_path = tmp_path / "watcher-trading.db"
    evidence_path = tmp_path / "evidence" / "mapping.json"
    evidence_path.parent.mkdir(mode=0o700)
    _create_database(database_path)
    writer = sqlite3.connect(database_path)
    try:
        journal_mode = writer.execute("PRAGMA journal_mode = WAL").fetchone()
        assert journal_mode[0].lower() == "wal"
        writer.execute(
            "UPDATE account_configs "
            "SET risk_capital_multiplier = 1.125 "
            "WHERE execution_account_id = 'account-b'"
        )
        writer.commit()

        result = _run_gate(database_path, evidence_path=evidence_path)
    finally:
        writer.close()

    assert result.returncode == 0, result.stderr
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["database"]["journal_mode"] == "wal"
    files_before = payload["database"]["files_before_read"]
    files_after = payload["database"]["files_after_read"]
    assert files_before["wal"]["present"] is True
    assert files_before["shm"]["present"] is True
    assert files_after["database"]["inode"] == (
        files_before["database"]["inode"]
    )


def test_positive_runtime_multiplier_is_preserved_without_fixed_value(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "watcher-trading.db"
    evidence_path = tmp_path / "evidence" / "mapping.json"
    evidence_path.parent.mkdir(mode=0o700)
    _create_database(database_path)
    _update_account(
        database_path,
        "泰山",
        "risk_capital_multiplier",
        7.125,
    )

    result = _run_gate(database_path, evidence_path=evidence_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    by_channel = {
        item["channel_id"]: item for item in payload["mapping"]
    }
    assert (
        by_channel["-1002189417451"]["risk_capital_multiplier"]
        == 7.125
    )


def test_pre_restart_schema_uses_verified_implicit_enabled_mode(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "watcher-trading.db"
    evidence_path = tmp_path / "evidence" / "mapping.json"
    evidence_path.parent.mkdir(mode=0o700)
    _create_database(database_path, include_enabled=False)

    result = _run_gate(
        database_path,
        evidence_path=evidence_path,
        schema_mode="pre_restart",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["schema_mode"] == "pre_restart"
    assert payload["pre_restart_contract"] == (
        "dynamic-routing-columns-present/enabled-column-optional-v1"
    )
    assert payload["enabled_column_mode"] == "legacy_implicit_enabled"
    assert all(item["is_enabled"] == 1 for item in payload["mapping"])


def test_strict_gate_rejects_pre_restart_schema_without_enabled_column(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "watcher-trading.db"
    _create_database(database_path, include_enabled=False)

    result = _run_gate(database_path, schema_mode="strict")

    assert result.returncode != 0
    assert "account_configs lacks required columns: is_enabled" in (
        result.stderr
    )


@pytest.mark.parametrize(
    "missing_column",
    (
        "execution_account_id",
        "risk_capital_multiplier",
    ),
)
def test_pre_restart_gate_requires_dynamic_routing_columns(
    tmp_path: Path,
    missing_column: str,
) -> None:
    database_path = tmp_path / "watcher-trading.db"
    account_columns = {
        "account_id": "TEXT PRIMARY KEY",
        "api_key": "TEXT NOT NULL",
        "api_secret": "TEXT NOT NULL",
        "is_testnet": "INTEGER NOT NULL",
        "execution_account_id": "TEXT NOT NULL",
        "risk_capital_multiplier": "REAL NOT NULL",
    }
    account_columns.pop(missing_column)
    schema = ",\n".join(
        f"{name} {definition}"
        for name, definition in account_columns.items()
    )
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            f"""
            CREATE TABLE account_configs (
                {schema}
            );
            CREATE TABLE channel_routing (
                channel_id TEXT PRIMARY KEY,
                target_account_id TEXT NOT NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    result = _run_gate(database_path, schema_mode="pre_restart")

    assert result.returncode != 0
    assert (
        f"account_configs lacks required columns: {missing_column}"
        in result.stderr
    )


def test_extra_enabled_live_route_remains_compatible(tmp_path: Path) -> None:
    database_path = tmp_path / "watcher-trading.db"
    _create_database(database_path)
    _insert_route(
        database_path,
        channel_id="-1002999999999",
        credential_account_id="extra-live",
        execution_account_id="account-extra",
        is_enabled=1,
        is_testnet=0,
    )

    result = _run_gate(database_path)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("is_enabled", "is_testnet"),
    (
        (0, 0),
        (1, 1),
    ),
)
def test_historical_non_live_route_remains_compatible(
    tmp_path: Path,
    is_enabled: int,
    is_testnet: int,
) -> None:
    database_path = tmp_path / "watcher-trading.db"
    _create_database(database_path)
    _insert_route(
        database_path,
        channel_id="-1002999999999",
        credential_account_id="historical-route",
        execution_account_id="account-history",
        is_enabled=is_enabled,
        is_testnet=is_testnet,
    )

    result = _run_gate(database_path)

    assert result.returncode == 0, result.stderr


def test_duplicate_execution_account_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "watcher-trading.db"
    _create_database(database_path)
    _update_account(
        database_path,
        "balenwong3@gmail.com",
        "execution_account_id",
        "account-a",
    )

    result = _run_gate(database_path)

    assert result.returncode != 0
    assert (
        "execution accounts must cover account-a through account-d "
        "exactly once"
    ) in result.stderr


def test_wrong_channel_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "watcher-trading.db"
    routes = list(ROUTES)
    channel_id, credential_id, execution_id, multiplier = routes[3]
    routes[3] = (
        "-1000000000000",
        credential_id,
        execution_id,
        multiplier,
    )
    assert channel_id == "-1002193304023"
    _create_database(database_path, routes=tuple(routes))

    result = _run_gate(database_path)

    assert result.returncode != 0
    assert "required channel routes are missing: -1002193304023" in (
        result.stderr
    )


@pytest.mark.parametrize("multiplier", (0, float("inf")))
def test_invalid_multiplier_fails_closed(
    tmp_path: Path,
    multiplier: float,
) -> None:
    database_path = tmp_path / "watcher-trading.db"
    _create_database(database_path)
    _update_account(
        database_path,
        "泰山",
        "risk_capital_multiplier",
        multiplier,
    )

    result = _run_gate(database_path)

    assert result.returncode != 0
    assert (
        "risk capital multiplier is invalid for -1002189417451"
        in result.stderr
    )


@pytest.mark.parametrize(
    ("credential_account_id", "column", "value", "message"),
    (
        (
            "jiataotx@gmail.com",
            "is_enabled",
            0,
            "mapped account is disabled for -1002136478186",
        ),
        (
            "黄山",
            "is_testnet",
            1,
            "mapped account is not live for -1002193304023",
        ),
    ),
)
def test_account_safety_flags_fail_closed(
    tmp_path: Path,
    credential_account_id: str,
    column: str,
    value: int,
    message: str,
) -> None:
    database_path = tmp_path / "watcher-trading.db"
    _create_database(database_path)
    _update_account(
        database_path,
        credential_account_id,
        column,
        value,
    )

    result = _run_gate(database_path)

    assert result.returncode != 0
    assert message in result.stderr


def test_missing_schema_column_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "watcher-trading.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE account_configs (
                account_id TEXT PRIMARY KEY
            );
            CREATE TABLE channel_routing (
                channel_id TEXT PRIMARY KEY,
                target_account_id TEXT NOT NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    result = _run_gate(database_path)

    assert result.returncode != 0
    assert "account_configs lacks required columns:" in result.stderr


def test_corrupt_sqlite_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "watcher-trading.db"
    database_path.write_bytes(b"not-a-sqlite-database")

    result = _run_gate(database_path)

    assert result.returncode != 0
    assert "file is not a database" in result.stderr


def test_deploy_orders_preflight_evidence_before_schema_and_install() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    preflight = text.index("# ---------- preflight ----------")
    preflight_gate = text.index(
        'verify_four_channel_account_mapping "" pre_restart',
        preflight,
    )
    halt = text.index("# ---------- HALT ----------", preflight_gate)
    backup_creation = text.index(
        '[ ! -e "$BACKUP_ROOT" ]',
        halt,
    )
    evidence_gate = text.index(
        'verify_four_channel_account_mapping '
        '\\\n  "$FOUR_CHANNEL_MAPPING_EVIDENCE" '
        '\\\n  pre_restart',
        backup_creation,
    )
    database_schema = text.index(
        "# ---------- database schema ----------",
        evidence_gate,
    )
    install = text.index("# ---------- install ----------", database_schema)
    watcher_restart = text.index(
        "restart_watcher_runtime",
        install,
    )
    forced_schema_restart = text.index(
        'if [ "$WATCHER_SCHEMA_RESTART_REQUIRED" = "1" ]; then',
        install,
    )
    transition_change_gate = text.index(
        '|| [ "$WATCHER_SCHEMA_RESTART_REQUIRED" = "1" ]',
        preflight_gate,
    )
    strict_gate = text.index(
        "verify_post_restart_four_channel_account_mapping",
        watcher_restart,
    )
    recreate = text.index(
        "# ---------- recreate & verify ----------",
        strict_gate,
    )

    assert preflight_gate < halt
    assert halt < backup_creation < evidence_gate
    assert evidence_gate < database_schema < install
    assert preflight_gate < transition_change_gate < halt
    assert install < forced_schema_restart < watcher_restart
    assert watcher_restart < strict_gate < recreate
