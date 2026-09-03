#!/usr/bin/env python3
"""Synchronize non-sensitive watcher risk configuration to operator-query."""

from __future__ import annotations

import argparse
import errno
import grp
import json
import os
import pwd
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

DEFAULT_SOURCE = Path(
    "/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db"
)
DEFAULT_TARGET = Path(
    "/srv/trader-v3/state/operator-query-risk/trading-risk.db"
)
DEFAULT_OWNER = "root:trader-v3-cp-operator-query"
DEFAULT_MODE = 0o640
CHECK_DIFFERENT_EXIT_CODE = 3

ALLOWED_TABLES = {
    "account_configs": (
        "account_id",
        "account_type",
        "parent_account_id",
        "execution_account_id",
        "risk_capital_multiplier",
        "risk_capital_addon",
        "is_enabled",
    ),
    "channel_routing": (
        "channel_id",
        "target_account_id",
    ),
    "symbol_risk_configs": (
        "symbol",
        "risk_ratio",
    ),
}

FORBIDDEN_COLUMN_PATTERNS = (
    "api_key",
    "api_secret",
    "secret",
    "token",
    "passphrase",
    "password",
)

_SAFE_DECLARED_TYPE_RE = re.compile(r"^[A-Za-z0-9_(), +.\-]*$")


class SyncError(RuntimeError):
    """Raised when synchronization cannot proceed safely."""


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    declared_type: str
    not_null: bool
    primary_key_position: int


@dataclass(frozen=True)
class TableSnapshot:
    name: str
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]

    def row_key(self, row: tuple[object, ...]) -> tuple[object, ...]:
        column_names = tuple(column.name for column in self.columns)
        indexes = tuple(column_names.index(name) for name in self.primary_key)
        return tuple(row[index] for index in indexes)


@dataclass(frozen=True)
class DatabaseSnapshot:
    tables: dict[str, TableSnapshot]


@dataclass(frozen=True)
class Difference:
    table_name: str
    target_row_count: int | None
    source_row_count: int
    changed_keys: tuple[tuple[object, ...], ...]
    reason: str = ""


@dataclass(frozen=True)
class Comparison:
    differences: tuple[Difference, ...]

    @property
    def changed_tables(self) -> tuple[str, ...]:
        return tuple(item.table_name for item in self.differences)


def _forbidden_pattern(column_name: str) -> str | bool:
    normalized = column_name.casefold()
    for pattern in FORBIDDEN_COLUMN_PATTERNS:
        if pattern.casefold() in normalized:
            return pattern
    return False


def _assert_allowed_tables_safe() -> None:
    for table_name, columns in ALLOWED_TABLES.items():
        for column_name in columns:
            pattern = _forbidden_pattern(column_name)
            if pattern is False:
                continue
            raise SyncError(
                "allowed column matches forbidden pattern: "
                f"{table_name}.{column_name} pattern={pattern}"
            )


_assert_allowed_tables_safe()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _readonly_uri(path: Path) -> str:
    absolute_path = os.path.abspath(os.fspath(path))
    encoded_path = quote(absolute_path, safe="/")
    return f"file:{encoded_path}?mode=ro"


def _open_readonly(path: Path, label: str) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            _readonly_uri(path),
            uri=True,
        )
        connection.execute("PRAGMA query_only=1")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or int(query_only[0]) != 1:
            connection.close()
            raise SyncError(f"{label} database query_only could not be enabled")
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise SyncError(
            f"{label} database cannot be opened read-only: {path}: {exc}"
        ) from exc


def _table_info(
    connection: sqlite3.Connection,
    table_name: str,
) -> tuple[sqlite3.Row, ...]:
    sql = f"PRAGMA table_info({_quote_identifier(table_name)})"
    return tuple(connection.execute(sql).fetchall())


def _validated_declared_type(
    table_name: str,
    column_name: str,
    raw_type: object,
) -> str:
    declared_type = str(raw_type or "").strip()
    if _SAFE_DECLARED_TYPE_RE.fullmatch(declared_type) is None:
        raise SyncError(
            "source column has unsupported declared type: "
            f"{table_name}.{column_name} type={declared_type!r}"
        )
    return declared_type


def _build_table_schema(
    table_name: str,
    allowed_columns: tuple[str, ...],
    raw_info: tuple[sqlite3.Row, ...],
) -> tuple[tuple[ColumnSpec, ...], tuple[str, ...]]:
    rows_by_name = {str(row["name"]): row for row in raw_info}
    source_primary_key = sorted(
        (
            int(row["pk"]),
            str(row["name"]),
        )
        for row in raw_info
        if int(row["pk"]) > 0
    )
    if not source_primary_key:
        raise SyncError(f"source table has no primary key: {table_name}")

    disallowed_primary_key = [
        name
        for _, name in source_primary_key
        if name not in allowed_columns
    ]
    if disallowed_primary_key:
        joined = ",".join(disallowed_primary_key)
        raise SyncError(
            f"source table primary key uses disallowed columns: "
            f"{table_name}.{joined}"
        )

    columns = []
    for column_name in allowed_columns:
        row = rows_by_name[column_name]
        columns.append(
            ColumnSpec(
                name=column_name,
                declared_type=_validated_declared_type(
                    table_name,
                    column_name,
                    row["type"],
                ),
                not_null=bool(row["notnull"]),
                primary_key_position=int(row["pk"]),
            )
        )
    primary_key = tuple(name for _, name in source_primary_key)
    return tuple(columns), primary_key


def _assert_unique_primary_keys(table: TableSnapshot) -> None:
    seen = set()
    for row in table.rows:
        key = table.row_key(row)
        if any(value is None for value in key):
            raise SyncError(
                f"source table has NULL primary key: {table.name} key={key!r}"
            )
        if key in seen:
            raise SyncError(
                f"source table has duplicate primary key: "
                f"{table.name} key={key!r}"
            )
        seen.add(key)


def read_source_snapshot(source: Path) -> DatabaseSnapshot:
    if not source.exists():
        raise SyncError(f"source database does not exist: {source}")
    if source.is_dir():
        raise SyncError(f"source database is a directory: {source}")

    connection = _open_readonly(source, "source")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        raw_schemas: dict[str, tuple[sqlite3.Row, ...]] = {}
        missing_tables = []
        missing_columns = []

        for table_name, allowed_columns in ALLOWED_TABLES.items():
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            if exists is None:
                missing_tables.append(table_name)
                continue

            raw_info = _table_info(connection, table_name)
            raw_schemas[table_name] = raw_info
            available_columns = {
                str(row["name"])
                for row in raw_info
            }
            for column_name in allowed_columns:
                if column_name in available_columns:
                    continue
                missing_columns.append(f"{table_name}.{column_name}")

        schema_errors = []
        if missing_tables:
            schema_errors.append(
                "missing tables: " + ",".join(missing_tables)
            )
        if missing_columns:
            schema_errors.append(
                "missing columns: " + ",".join(missing_columns)
            )
        if schema_errors:
            raise SyncError(
                "source schema validation failed: " + "; ".join(schema_errors)
            )

        tables: dict[str, TableSnapshot] = {}
        for table_name, allowed_columns in ALLOWED_TABLES.items():
            columns, primary_key = _build_table_schema(
                table_name,
                allowed_columns,
                raw_schemas[table_name],
            )
            selected = ", ".join(
                _quote_identifier(column_name)
                for column_name in allowed_columns
            )
            ordered = ", ".join(
                _quote_identifier(column_name)
                for column_name in primary_key
            )
            sql = (
                f"SELECT {selected} FROM {_quote_identifier(table_name)} "
                f"ORDER BY {ordered}"
            )
            rows = tuple(
                tuple(row)
                for row in connection.execute(sql).fetchall()
            )
            table = TableSnapshot(
                name=table_name,
                columns=columns,
                primary_key=primary_key,
                rows=rows,
            )
            _assert_unique_primary_keys(table)
            tables[table_name] = table
        return DatabaseSnapshot(tables=tables)
    except sqlite3.Error as exc:
        raise SyncError(f"source database read failed: {exc}") from exc
    finally:
        connection.close()


def _schema_signature(
    columns: tuple[ColumnSpec, ...],
) -> tuple[tuple[str, str, bool, int], ...]:
    return tuple(
        (
            column.name,
            column.declared_type,
            column.not_null,
            column.primary_key_position,
        )
        for column in columns
    )


def _columns_from_table_info(
    table_name: str,
    raw_info: tuple[sqlite3.Row, ...],
) -> tuple[ColumnSpec, ...]:
    columns = []
    for row in raw_info:
        column_name = str(row["name"])
        columns.append(
            ColumnSpec(
                name=column_name,
                declared_type=_validated_declared_type(
                    table_name,
                    column_name,
                    row["type"],
                ),
                not_null=bool(row["notnull"]),
                primary_key_position=int(row["pk"]),
            )
        )
    return tuple(columns)


def _database_forbidden_columns(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    table_rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    forbidden = []
    for table_row in table_rows:
        table_name = str(table_row["name"])
        for column_row in _table_info(connection, table_name):
            column_name = str(column_row["name"])
            pattern = _forbidden_pattern(column_name)
            if pattern is False:
                continue
            forbidden.append(f"{table_name}.{column_name}")
    return tuple(forbidden)


def _all_source_keys(
    snapshot: DatabaseSnapshot,
    table_name: str,
) -> tuple[tuple[object, ...], ...]:
    table = snapshot.tables[table_name]
    return tuple(table.row_key(row) for row in table.rows)


def _unusable_target_comparison(
    source: DatabaseSnapshot,
    reason: str,
    target_row_count: int | None = None,
) -> Comparison:
    differences = []
    for table_name, table in source.tables.items():
        differences.append(
            Difference(
                table_name=table_name,
                target_row_count=target_row_count,
                source_row_count=len(table.rows),
                changed_keys=_all_source_keys(source, table_name),
                reason=reason,
            )
        )
    return Comparison(differences=tuple(differences))


def _rows_by_key(
    table: TableSnapshot,
) -> dict[tuple[object, ...], tuple[object, ...]]:
    return {
        table.row_key(row): row
        for row in table.rows
    }


def _changed_keys(
    source_table: TableSnapshot,
    target_table: TableSnapshot,
) -> tuple[tuple[object, ...], ...]:
    source_rows = _rows_by_key(source_table)
    target_rows = _rows_by_key(target_table)
    keys = set(source_rows) | set(target_rows)
    changed = [
        key
        for key in keys
        if source_rows.get(key) != target_rows.get(key)
    ]
    return tuple(sorted(changed, key=_format_key))


def compare_target(
    source: DatabaseSnapshot,
    target: Path,
) -> Comparison:
    if not target.exists():
        return _unusable_target_comparison(
            source,
            reason="target missing",
            target_row_count=0,
        )
    if target.is_dir():
        return _unusable_target_comparison(
            source,
            reason="target is a directory",
        )

    try:
        connection = _open_readonly(target, "target")
    except SyncError as exc:
        return _unusable_target_comparison(source, reason=str(exc))

    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        forbidden_columns = _database_forbidden_columns(connection)
        if forbidden_columns:
            return _unusable_target_comparison(
                source,
                reason=(
                    "target contains forbidden columns: "
                    + ",".join(forbidden_columns)
                ),
            )

        table_rows = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        target_table_names = {
            str(row["name"])
            for row in table_rows
        }
        expected_table_names = set(source.tables)
        if target_table_names != expected_table_names:
            return _unusable_target_comparison(
                source,
                reason="target table set differs",
            )

        target_tables: dict[str, TableSnapshot] = {}
        for table_name, source_table in source.tables.items():
            raw_info = _table_info(connection, table_name)
            target_columns = _columns_from_table_info(
                table_name,
                raw_info,
            )
            if _schema_signature(target_columns) != _schema_signature(
                source_table.columns
            ):
                return _unusable_target_comparison(
                    source,
                    reason=f"target schema differs: {table_name}",
                )

            selected = ", ".join(
                _quote_identifier(column.name)
                for column in source_table.columns
            )
            ordered = ", ".join(
                _quote_identifier(column_name)
                for column_name in source_table.primary_key
            )
            sql = (
                f"SELECT {selected} FROM {_quote_identifier(table_name)} "
                f"ORDER BY {ordered}"
            )
            rows = tuple(
                tuple(row)
                for row in connection.execute(sql).fetchall()
            )
            target_tables[table_name] = TableSnapshot(
                name=table_name,
                columns=target_columns,
                primary_key=source_table.primary_key,
                rows=rows,
            )

        differences = []
        for table_name, source_table in source.tables.items():
            target_table = target_tables[table_name]
            if source_table.rows == target_table.rows:
                continue
            differences.append(
                Difference(
                    table_name=table_name,
                    target_row_count=len(target_table.rows),
                    source_row_count=len(source_table.rows),
                    changed_keys=_changed_keys(
                        source_table,
                        target_table,
                    ),
                )
            )
        return Comparison(differences=tuple(differences))
    except sqlite3.Error as exc:
        return _unusable_target_comparison(
            source,
            reason=f"target database read failed: {exc}",
        )
    finally:
        connection.close()


def _create_table_sql(table: TableSnapshot) -> str:
    definitions = []
    for column in table.columns:
        parts = [_quote_identifier(column.name)]
        if column.declared_type:
            parts.append(column.declared_type)
        if column.not_null:
            parts.append("NOT NULL")
        definitions.append(" ".join(parts))

    primary_key = ", ".join(
        _quote_identifier(column_name)
        for column_name in table.primary_key
    )
    definitions.append(f"PRIMARY KEY ({primary_key})")
    body = ", ".join(definitions)
    return f"CREATE TABLE {_quote_identifier(table.name)} ({body})"


def _assert_target_columns_safe(
    connection: sqlite3.Connection,
) -> None:
    forbidden_columns = _database_forbidden_columns(connection)
    if forbidden_columns:
        raise SyncError(
            "target schema contains forbidden columns: "
            + ",".join(forbidden_columns)
        )


def _resolve_owner(owner: str) -> tuple[int, int] | bool:
    parts = owner.split(":")
    parse_error = ""
    if len(parts) != 2 or not parts[0] or not parts[1]:
        parse_error = "expected user:group"
    else:
        user_name, group_name = parts
        try:
            uid = pwd.getpwnam(user_name).pw_uid
            gid = grp.getgrnam(group_name).gr_gid
            return uid, gid
        except KeyError as exc:
            parse_error = str(exc)

    if os.geteuid() != 0:
        print(
            f"warning: owner {owner!r} could not be resolved; "
            f"leaving temporary file ownership unchanged: {parse_error}",
            file=sys.stderr,
        )
        return False
    raise SyncError(f"owner {owner!r} could not be resolved: {parse_error}")


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
            raise
    finally:
        os.close(descriptor)


def _cleanup_target_sidecars(target: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(os.fspath(target) + suffix)
        try:
            sidecar.unlink()
        except FileNotFoundError:
            continue


def write_target_atomic(
    source: DatabaseSnapshot,
    target: Path,
    *,
    owner: str,
    mode: int,
) -> None:
    parent = target.parent
    if not parent.is_dir():
        raise SyncError(f"target directory does not exist: {parent}")

    owner_ids = _resolve_owner(owner)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary)
        try:
            journal_mode = connection.execute(
                "PRAGMA journal_mode=DELETE"
            ).fetchone()
            if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
                raise SyncError("temporary target could not enable DELETE journal mode")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            for table in source.tables.values():
                connection.execute(_create_table_sql(table))

            connection.row_factory = sqlite3.Row
            _assert_target_columns_safe(connection)
            connection.row_factory = None

            for table in source.tables.values():
                columns = ", ".join(
                    _quote_identifier(column.name)
                    for column in table.columns
                )
                placeholders = ", ".join("?" for _ in table.columns)
                sql = (
                    f"INSERT INTO {_quote_identifier(table.name)} "
                    f"({columns}) VALUES ({placeholders})"
                )
                connection.executemany(sql, table.rows)
            connection.commit()

            integrity = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()
            if integrity is None or str(integrity[0]).casefold() != "ok":
                raise SyncError("temporary target failed integrity_check")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        _fsync_file(temporary)
        os.chmod(temporary, mode)
        if owner_ids is not False:
            uid, gid = owner_ids
            os.chown(temporary, uid, gid)
        os.replace(temporary, target)
        _cleanup_target_sidecars(target)
        _fsync_directory(parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _format_key(key: tuple[object, ...]) -> str:
    value: object
    if len(key) == 1:
        value = key[0]
    else:
        value = list(key)
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        default=str,
    )


def _format_difference_summary(comparison: Comparison) -> str:
    parts = []
    for difference in comparison.differences:
        target_count = "?"
        if difference.target_row_count is not None:
            target_count = str(difference.target_row_count)
        keys = ",".join(
            _format_key(key)
            for key in difference.changed_keys
        )
        if not keys:
            keys = "schema"
        reason = ""
        if difference.reason:
            reason = f" reason={json.dumps(difference.reason)}"
        parts.append(
            f"{difference.table_name} "
            f"rows={target_count}->{difference.source_row_count} "
            f"keys={keys}{reason}"
        )
    return "differences " + "; ".join(parts)


def _format_sync_summary(
    source: DatabaseSnapshot,
    comparison: Comparison,
) -> str:
    rows = ",".join(
        f"{table_name}:{len(table.rows)}"
        for table_name, table in source.tables.items()
    )
    changed = ",".join(comparison.changed_tables)
    return (
        f"synced tables={len(source.tables)} rows={rows} "
        f"changed={changed}"
    )


def _parse_mode(raw_mode: str) -> int:
    normalized = raw_mode.strip().lower()
    if normalized.startswith("0o"):
        normalized = normalized[2:]
    try:
        mode = int(normalized, 8)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid octal mode: {raw_mode}"
        ) from exc
    if mode < 0 or mode > 0o7777:
        raise argparse.ArgumentTypeError(
            f"mode is outside 0000..7777: {raw_mode}"
        )
    return mode


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize the non-sensitive watcher risk configuration "
            "to the operator-query replica."
        )
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"watcher source database (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help=f"operator-query replica database (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--owner",
        default=DEFAULT_OWNER,
        help=f"target owner as user:group (default: {DEFAULT_OWNER})",
    )
    parser.add_argument(
        "--mode",
        type=_parse_mode,
        default=DEFAULT_MODE,
        help="target file mode in octal (default: 0640)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare only; return 3 when differences exist",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print source and target paths to stderr",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    _assert_allowed_tables_safe()
    source_path = args.source
    target_path = args.target
    if source_path.resolve() == target_path.resolve():
        raise SyncError("source and target must be different paths")

    if args.verbose:
        print(f"source={source_path} target={target_path}", file=sys.stderr)

    source = read_source_snapshot(source_path)
    comparison = compare_target(source, target_path)
    if not comparison.differences:
        print("unchanged")
        return 0

    if args.check:
        print(_format_difference_summary(comparison))
        return CHECK_DIFFERENT_EXIT_CODE

    write_target_atomic(
        source,
        target_path,
        owner=args.owner,
        mode=args.mode,
    )
    print(_format_sync_summary(source, comparison))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except (OSError, sqlite3.Error, SyncError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
