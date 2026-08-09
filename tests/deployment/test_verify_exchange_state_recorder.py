from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_exchange_state_recorder.py"
SPEC = importlib.util.spec_from_file_location(
    "verify_exchange_state_recorder",
    SCRIPT,
)
assert SPEC is not None
assert SPEC.loader is not None
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def test_verify_process_binds_cwd_python_and_recorder(tmp_path: Path) -> None:
    process_root = tmp_path / "proc" / "42"
    process_root.mkdir(parents=True)
    working_directory = tmp_path / "trader"
    working_directory.mkdir()
    python_path = working_directory / ".venv-cp" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text("", encoding="utf-8")
    recorder_path = (
        working_directory
        / "services"
        / "control-plane"
        / "tools"
        / "exchange_state_recorder.py"
    )
    recorder_path.parent.mkdir(parents=True)
    recorder_path.write_text("", encoding="utf-8")
    os.symlink(working_directory, process_root / "cwd")
    (process_root / "cmdline").write_bytes(
        (
            str(python_path)
            + "\0services/control-plane/tools/"
            "exchange_state_recorder.py\0--interval\0"
            "45\0"
        ).encode()
    )

    verify.verify_process(
        42,
        working_directory,
        python_path,
        recorder_path,
        proc_root=tmp_path / "proc",
    )


def test_verify_process_rejects_other_recorder_path(
    tmp_path: Path,
) -> None:
    process_root = tmp_path / "proc" / "42"
    process_root.mkdir(parents=True)
    working_directory = tmp_path / "trader"
    working_directory.mkdir()
    python_path = working_directory / "python"
    python_path.write_text("", encoding="utf-8")
    recorder_path = working_directory / "exchange_state_recorder.py"
    recorder_path.write_text("", encoding="utf-8")
    other_recorder = working_directory / "other.py"
    other_recorder.write_text("", encoding="utf-8")
    os.symlink(working_directory, process_root / "cwd")
    (process_root / "cmdline").write_bytes(
        f"{python_path}\0{other_recorder}\0".encode()
    )

    with pytest.raises(
        verify.VerificationError,
        match="recorder path mismatch",
    ):
        verify.verify_process(
            42,
            working_directory,
            python_path,
            recorder_path,
            proc_root=tmp_path / "proc",
        )


def test_fresh_snapshot_accounts_requires_watermark_and_fields() -> None:
    watermark = datetime(2026, 8, 9, 21, 0, tzinfo=timezone.utc)
    fresh_at = watermark + timedelta(seconds=1)
    required_fields = {
        "leverage",
        "margin_type",
        "isolated_margin",
        "is_auto_add_margin",
    }
    rows = [
        (
            "account-a",
            fresh_at,
            {
                "fetched_at": fresh_at.isoformat(),
                "positions": [
                    {
                        "symbol": "SOLUSDT",
                        "leverage": "5",
                        "margin_type": "cross",
                        "isolated_margin": "0",
                        "is_auto_add_margin": "false",
                    }
                ],
            },
        ),
        (
            "account-b",
            fresh_at,
            {
                "fetched_at": fresh_at.isoformat(),
                "positions": [],
            },
        ),
    ]

    fresh = verify.fresh_snapshot_accounts(
        rows,
        watermark=watermark,
        expected_accounts={"account-a", "account-b"},
        required_position_fields=required_fields,
    )

    assert fresh == {"account-a", "account-b"}


def test_fresh_snapshot_accounts_rejects_stale_or_old_shape() -> None:
    watermark = datetime(2026, 8, 9, 21, 0, tzinfo=timezone.utc)
    fresh_at = watermark + timedelta(seconds=1)
    rows = [
        (
            "account-a",
            watermark,
            {
                "fetched_at": fresh_at.isoformat(),
                "positions": [],
            },
        ),
        (
            "account-b",
            fresh_at,
            {
                "fetched_at": fresh_at.isoformat(),
                "positions": [{"symbol": "BTCUSDT"}],
            },
        ),
    ]

    fresh = verify.fresh_snapshot_accounts(
        rows,
        watermark=watermark,
        expected_accounts={"account-a", "account-b"},
        required_position_fields={
            "leverage",
            "margin_type",
            "isolated_margin",
            "is_auto_add_margin",
        },
    )

    assert fresh == set()
