from __future__ import annotations

import grp
import os
import pwd
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "sync_operator_risk_db.py"


def _owner_spec() -> str:
    user_name = pwd.getpwuid(os.geteuid()).pw_name
    group_name = grp.getgrgid(os.getegid()).gr_name
    return f"{user_name}:{group_name}"


def _create_source(
    path: Path,
    *,
    include_risk_capital_addon: bool = True,
) -> None:
    addon_definition = ""
    addon_column = ""
    addon_value = ""
    if include_risk_capital_addon:
        addon_definition = (
            "risk_capital_addon REAL NOT NULL DEFAULT 0.0,"
        )
        addon_column = ", risk_capital_addon"
        addon_value = ", ?"

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            f"""
            CREATE TABLE account_configs (
                account_id TEXT PRIMARY KEY,
                api_key TEXT NOT NULL,
                api_secret TEXT NOT NULL,
                account_type TEXT NOT NULL,
                parent_account_id TEXT NOT NULL,
                execution_account_id TEXT NOT NULL,
                risk_capital_multiplier REAL NOT NULL,
                {addon_definition}
                is_enabled INTEGER NOT NULL,
                default_risk_ratio REAL DEFAULT 0.01
            );
            CREATE TABLE channel_routing (
                channel_id TEXT PRIMARY KEY,
                target_account_id TEXT NOT NULL,
                channel_name TEXT DEFAULT ''
            );
            CREATE TABLE symbol_risk_configs (
                symbol TEXT PRIMARY KEY,
                risk_ratio REAL NOT NULL
            );
            """
        )
        columns = (
            "account_id, api_key, api_secret, account_type, "
            "parent_account_id, execution_account_id, "
            f"risk_capital_multiplier{addon_column}, is_enabled"
        )
        placeholders = f"?, ?, ?, ?, ?, ?, ?{addon_value}, ?"
        first_row = [
            "account-a",
            "EXAMPLE_API_KEY_A_DO_NOT_COPY",
            "EXAMPLE_API_SECRET_A_DO_NOT_COPY",
            "main",
            "",
            "account-a",
            1.0,
        ]
        second_row = [
            "account-b",
            "EXAMPLE_API_KEY_B_DO_NOT_COPY",
            "EXAMPLE_API_SECRET_B_DO_NOT_COPY",
            "subaccount",
            "account-a",
            "account-b",
            0.75,
        ]
        if include_risk_capital_addon:
            first_row.append(100.0)
            second_row.append(25.0)
        first_row.append(1)
        second_row.append(1)
        connection.executemany(
            f"INSERT INTO account_configs ({columns}) "
            f"VALUES ({placeholders})",
            (first_row, second_row),
        )
        connection.executemany(
            "INSERT INTO channel_routing "
            "(channel_id, target_account_id, channel_name) "
            "VALUES (?, ?, ?)",
            (
                ("100", "account-a", "alpha"),
                ("200", "account-b", "beta"),
            ),
        )
        connection.executemany(
            "INSERT INTO symbol_risk_configs (symbol, risk_ratio) "
            "VALUES (?, ?)",
            (
                ("BTCUSDT", 0.02),
                ("ETHUSDT", 0.015),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _run(
    source: Path,
    target: Path,
    *extra_args: str,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        os.fspath(SCRIPT),
        "--source",
        os.fspath(source),
        "--target",
        os.fspath(target),
        "--owner",
        _owner_spec(),
        "--mode",
        "0640",
        *extra_args,
    ]
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
    )


def test_normal_sync_generates_minimal_risk_replica(tmp_path: Path) -> None:
    source = tmp_path / "watcher.db"
    target = tmp_path / "risk.db"
    _create_source(source)

    result = _run(source, target)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "synced tables=3 "
        "rows=account_configs:2,channel_routing:2,symbol_risk_configs:2 "
        "changed=account_configs,channel_routing,symbol_risk_configs"
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    with _connect_readonly(target) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == {
            "account_configs",
            "channel_routing",
            "symbol_risk_configs",
        }
        account = connection.execute(
            "SELECT account_type, risk_capital_multiplier, "
            "risk_capital_addon, is_enabled "
            "FROM account_configs WHERE account_id='account-b'"
        ).fetchone()
        assert account == ("subaccount", 0.75, 25.0, 1)


def test_source_secret_columns_and_values_never_reach_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "watcher.db"
    target = tmp_path / "risk.db"
    _create_source(source)

    result = _run(source, target)

    assert result.returncode == 0, result.stderr
    with _connect_readonly(target) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(account_configs)"
            )
        }
    assert "api_key" not in columns
    assert "api_secret" not in columns
    raw_target = target.read_bytes()
    assert b"EXAMPLE_API_KEY_A_DO_NOT_COPY" not in raw_target
    assert b"EXAMPLE_API_SECRET_A_DO_NOT_COPY" not in raw_target
    assert b"EXAMPLE_API_KEY_B_DO_NOT_COPY" not in raw_target
    assert b"EXAMPLE_API_SECRET_B_DO_NOT_COPY" not in raw_target


def test_unchanged_skips_target_rewrite(tmp_path: Path) -> None:
    source = tmp_path / "watcher.db"
    target = tmp_path / "risk.db"
    _create_source(source)
    first = _run(source, target)
    assert first.returncode == 0, first.stderr
    before = target.stat()
    before_bytes = target.read_bytes()

    second = _run(source, target)

    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "unchanged"
    after = target.stat()
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert target.read_bytes() == before_bytes


def test_check_reports_changed_primary_key_and_returns_three(
    tmp_path: Path,
) -> None:
    source = tmp_path / "watcher.db"
    target = tmp_path / "risk.db"
    _create_source(source)
    first = _run(source, target)
    assert first.returncode == 0, first.stderr
    before_bytes = target.read_bytes()
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE account_configs "
            "SET risk_capital_addon=125.0 WHERE account_id='account-a'"
        )

    result = _run(source, target, "--check")

    assert result.returncode == 3
    assert result.stdout.startswith("differences account_configs ")
    assert "rows=2->2" in result.stdout
    assert 'keys="account-a"' in result.stdout
    assert target.read_bytes() == before_bytes


def test_missing_source_column_fails_closed_without_touching_target(
    tmp_path: Path,
) -> None:
    good_source = tmp_path / "good-watcher.db"
    bad_source = tmp_path / "bad-watcher.db"
    target = tmp_path / "risk.db"
    _create_source(good_source)
    _create_source(
        bad_source,
        include_risk_capital_addon=False,
    )
    first = _run(good_source, target)
    assert first.returncode == 0, first.stderr
    before = target.stat()
    before_bytes = target.read_bytes()

    result = _run(bad_source, target)

    assert result.returncode != 0
    assert "missing columns: account_configs.risk_capital_addon" in (
        result.stderr
    )
    after = target.stat()
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert target.read_bytes() == before_bytes


def test_atomic_replacement_is_readable_in_readonly_mode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "watcher.db"
    target = tmp_path / "risk.db"
    _create_source(source)
    first = _run(source, target)
    assert first.returncode == 0, first.stderr
    first_inode = target.stat().st_ino
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE symbol_risk_configs "
            "SET risk_ratio=0.025 WHERE symbol='BTCUSDT'"
        )

    result = _run(source, target)

    assert result.returncode == 0, result.stderr
    assert "changed=symbol_risk_configs" in result.stdout
    assert target.stat().st_ino != first_inode
    with _connect_readonly(target) as connection:
        value = connection.execute(
            "SELECT risk_ratio FROM symbol_risk_configs "
            "WHERE symbol='BTCUSDT'"
        ).fetchone()
    assert value == (0.025,)


def test_missing_target_is_created_on_first_sync(tmp_path: Path) -> None:
    source = tmp_path / "watcher.db"
    target_dir = tmp_path / "operator-query-risk"
    target_dir.mkdir()
    target = target_dir / "trading-risk.db"
    _create_source(source)
    assert not target.exists()

    result = _run(source, target)

    assert result.returncode == 0, result.stderr
    assert target.is_file()
    with _connect_readonly(target) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM channel_routing"
        ).fetchone()
    assert count == (2,)
