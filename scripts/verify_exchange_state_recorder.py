#!/usr/bin/env python3
"""Verify the deployed exchange-state recorder process and fresh snapshots."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


class VerificationError(RuntimeError):
    pass


def read_environment(path: Path) -> dict[str, str]:
    environment: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        environment[key] = value
    return environment


def verify_process(
    pid: int,
    working_directory: Path,
    python_path: Path,
    recorder_path: Path,
    *,
    proc_root: Path = Path("/proc"),
) -> None:
    if pid <= 0:
        raise VerificationError("exchange-state MainPID is invalid")
    process_root = proc_root / str(pid)
    try:
        actual_cwd = (process_root / "cwd").resolve(strict=True)
        raw_command = (process_root / "cmdline").read_bytes()
    except OSError as exc:
        raise VerificationError(
            "exchange-state process metadata is unavailable"
        ) from exc
    expected_cwd = working_directory.resolve(strict=True)
    if actual_cwd != expected_cwd:
        raise VerificationError("exchange-state process cwd mismatch")
    command = [
        value.decode("utf-8", errors="strict")
        for value in raw_command.split(b"\0")
        if value
    ]
    if len(command) < 2:
        raise VerificationError("exchange-state process command is invalid")
    actual_python = Path(command[0]).resolve(strict=True)
    expected_python = python_path.resolve(strict=True)
    if actual_python != expected_python:
        raise VerificationError("exchange-state Python executable mismatch")
    recorder_argument = Path(command[1])
    if not recorder_argument.is_absolute():
        recorder_argument = actual_cwd / recorder_argument
    actual_recorder = recorder_argument.resolve(strict=True)
    expected_recorder = recorder_path.resolve(strict=True)
    if actual_recorder != expected_recorder:
        raise VerificationError("exchange-state recorder path mismatch")


def capture_watermark(database_url: str) -> datetime:
    import psycopg2

    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT clock_timestamp()")
            row = cursor.fetchone()
    if not row or not isinstance(row[0], datetime):
        raise VerificationError("exchange-state watermark is invalid")
    return row[0]


def fresh_snapshot_accounts(
    rows: Iterable[tuple[Any, Any, Any]],
    *,
    watermark: datetime,
    expected_accounts: set[str],
    required_position_fields: set[str],
) -> set[str]:
    fresh: set[str] = set()
    for account_id_raw, updated_at, payload in rows:
        account_id = str(account_id_raw)
        if account_id not in expected_accounts:
            continue
        if not isinstance(updated_at, datetime) or updated_at <= watermark:
            continue
        if not isinstance(payload, dict):
            continue
        fetched_at_raw = payload.get("fetched_at")
        if not isinstance(fetched_at_raw, str) or not fetched_at_raw:
            continue
        try:
            fetched_at = datetime.fromisoformat(
                fetched_at_raw.replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if fetched_at <= watermark:
            continue
        positions = payload.get("positions")
        if not isinstance(positions, list):
            continue
        valid_positions = True
        for position in positions:
            if not isinstance(position, dict):
                valid_positions = False
                break
            if not required_position_fields.issubset(position):
                valid_positions = False
                break
        if valid_positions:
            fresh.add(account_id)
    return fresh


def wait_for_fresh_snapshots(
    database_url: str,
    *,
    watermark: datetime,
    accounts: tuple[str, ...],
    required_position_fields: tuple[str, ...],
    timeout_seconds: float,
    poll_seconds: float,
) -> set[str]:
    import psycopg2

    expected_accounts = set(accounts)
    required_fields = set(required_position_fields)
    deadline = time.monotonic() + timeout_seconds
    fresh: set[str] = set()
    while time.monotonic() < deadline:
        try:
            with psycopg2.connect(database_url) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT account_id, updated_at, payload
                        FROM exchange_state_mirror
                        WHERE account_id = ANY(%s)
                        """,
                        (list(accounts),),
                    )
                    rows = cursor.fetchall()
        except psycopg2.Error:
            rows = []
        fresh = fresh_snapshot_accounts(
            rows,
            watermark=watermark,
            expected_accounts=expected_accounts,
            required_position_fields=required_fields,
        )
        if fresh == expected_accounts:
            return fresh
        time.sleep(poll_seconds)
    missing = sorted(expected_accounts - fresh)
    raise VerificationError(
        f"fresh exchange-state snapshots missing after timeout: {missing}"
    )


def _database_url(environment_path: Path) -> str:
    database_url = read_environment(environment_path).get(
        "DATABASE_URL",
        "",
    )
    if not database_url:
        raise VerificationError("DATABASE_URL missing from environment")
    return database_url


def _watermark(path: Path) -> datetime:
    raw = path.read_text(encoding="utf-8").strip()
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise VerificationError(
            "exchange-state watermark file is invalid"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    process = commands.add_parser("process")
    process.add_argument("--pid", type=int, required=True)
    process.add_argument("--working-directory", type=Path, required=True)
    process.add_argument("--python", type=Path, required=True)
    process.add_argument("--recorder", type=Path, required=True)

    capture = commands.add_parser("capture")
    capture.add_argument("--env-file", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)

    wait = commands.add_parser("wait")
    wait.add_argument("--env-file", type=Path, required=True)
    wait.add_argument("--watermark-file", type=Path, required=True)
    wait.add_argument("--account", action="append", required=True)
    wait.add_argument(
        "--require-position-field",
        action="append",
        default=[],
    )
    wait.add_argument("--timeout-seconds", type=float, default=150)
    wait.add_argument("--poll-seconds", type=float, default=5)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "process":
        verify_process(
            args.pid,
            args.working_directory,
            args.python,
            args.recorder,
        )
        print("exchange-state process identity verified")
        return 0
    if args.command == "capture":
        watermark = capture_watermark(_database_url(args.env_file))
        args.output.write_text(
            watermark.isoformat(),
            encoding="utf-8",
        )
        print("exchange-state watermark captured")
        return 0
    if args.command == "wait":
        fresh = wait_for_fresh_snapshots(
            _database_url(args.env_file),
            watermark=_watermark(args.watermark_file),
            accounts=tuple(args.account),
            required_position_fields=tuple(
                args.require_position_field
            ),
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
        print(
            json.dumps(
                {"fresh_accounts": sorted(fresh)},
                separators=(",", ":"),
            )
        )
        return 0
    raise VerificationError("unsupported verification command")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as exc:
        raise SystemExit(str(exc)) from exc
