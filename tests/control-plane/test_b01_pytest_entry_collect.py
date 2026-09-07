"""B-01: lock GOAL-1 pytest entries to a real .venv-arch collect."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENV_PY = ROOT / ".venv-arch" / "bin" / "python"
REPORT_ENTRY = "services/report/tests"
CONTROL_PLANE_ENTRY = "tests/control-plane"


def _collect_only(target: str) -> subprocess.CompletedProcess[str]:
    assert VENV_PY.is_file(), f"missing frozen interpreter {VENV_PY}"
    return subprocess.run(
        [str(VENV_PY), "-m", "pytest", target, "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _assert_clean_collect(target: str, proc: subprocess.CompletedProcess[str]) -> None:
    output = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0, (
        f"{target} collect failed rc={proc.returncode}\n{output}"
    )
    lowered = output.lower()
    assert "error collecting" not in lowered, output
    assert "error during collection" not in lowered, output
    nodeids = [
        line.strip()
        for line in proc.stdout.splitlines()
        if "::" in line and not line.lstrip().lower().startswith("error")
    ]
    summary = re.search(r"(\d+)\s+tests?\s+collected", output)
    collected = int(summary.group(1)) if summary else len(nodeids)
    assert collected > 0, f"{target} collected nothing\n{output}"


def test_report_pytest_entry_collects_without_errors():
    proc = _collect_only(REPORT_ENTRY)
    _assert_clean_collect(REPORT_ENTRY, proc)


def test_control_plane_pytest_entry_collects_without_errors():
    proc = _collect_only(CONTROL_PLANE_ENTRY)
    _assert_clean_collect(CONTROL_PLANE_ENTRY, proc)
