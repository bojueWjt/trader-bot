from __future__ import annotations

import re

import psycopg2


LEGACY_TABLES = (
    "legacy_signals",
    "legacy_signal_events",
    "legacy_signal_operations",
)


def _table_counts(pg_dsn: str) -> dict[str, int]:
    with psycopg2.connect(pg_dsn) as conn, conn.cursor() as cur:
        counts = {}
        for table_name in LEGACY_TABLES:
            cur.execute(f"SELECT count(*) FROM {table_name}")
            counts[table_name] = cur.fetchone()[0]
        return counts


def _parse_reconciliation(stdout: str) -> dict[str, tuple[int, str]]:
    rows = {}
    for line in stdout.splitlines():
        match = re.fullmatch(
            r"(legacy_[a-z_]+) row_count=(\d+) sha256=([0-9a-f]{64})",
            line.strip(),
        )
        if match:
            rows[match.group(1)] = (int(match.group(2)), match.group(3))
    return rows


def test_fixture_sqlite_imports_expected_legacy_row_counts(run_import, pg_dsn):
    stdout = run_import()
    expected_counts = {
        "legacy_signals": 2,
        "legacy_signal_events": 2,
        "legacy_signal_operations": 2,
    }

    assert _table_counts(pg_dsn) == expected_counts
    assert {
        table_name: row_count
        for table_name, (row_count, _checksum) in _parse_reconciliation(stdout).items()
    } == expected_counts


def test_import_is_idempotent_and_reports_stable_checksums(run_import, pg_dsn):
    first_stdout = run_import()
    first_counts = _table_counts(pg_dsn)

    second_stdout = run_import()

    assert _table_counts(pg_dsn) == first_counts
    assert _parse_reconciliation(second_stdout) == _parse_reconciliation(first_stdout)


def test_sensitive_payload_fields_are_excluded_from_legacy_tables(run_import, pg_dsn):
    run_import()

    with psycopg2.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT string_agg(payload::text, ' ')
            FROM (
                SELECT payload FROM legacy_signals
                UNION ALL
                SELECT payload FROM legacy_signal_events
            ) payloads
            """
        )
        payload_text = (cur.fetchone()[0] or "").lower()

    for forbidden in ("api_key", "secret", "token", "password", "credential"):
        assert forbidden not in payload_text
