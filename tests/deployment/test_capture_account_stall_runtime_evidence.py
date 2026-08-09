from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "capture_account_stall_runtime_evidence.py"
SPEC = importlib.util.spec_from_file_location(
    "capture_account_stall_runtime_evidence",
    SCRIPT,
)
assert SPEC is not None
assert SPEC.loader is not None
capture = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capture
SPEC.loader.exec_module(capture)


class FakePytestRunner:
    def __init__(
        self,
        *,
        collect_stdout: str,
        collect_stderr: str = "",
        collect_exit_code: int = 0,
        run_stdout: str = "",
        run_stderr: str = "",
        run_exit_code: int = 0,
    ) -> None:
        self.collect_stdout = collect_stdout
        self.collect_stderr = collect_stderr
        self.collect_exit_code = collect_exit_code
        self.run_stdout = run_stdout
        self.run_stderr = run_stderr
        self.run_exit_code = run_exit_code
        self.commands: list[list[str]] = []
        self.pytest_commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        command = [str(value) for value in command]
        self.commands.append(command)
        if command[0] == "git":
            return subprocess.run(command, **kwargs)

        self.pytest_commands.append(command)
        if "--collect-only" in command:
            return subprocess.CompletedProcess(
                command,
                self.collect_exit_code,
                self.collect_stdout,
                self.collect_stderr,
            )

        junit_path = self._junit_path(command)
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        junit_path.write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<testsuites tests="1"><testsuite tests="1">'
            '<testcase classname="fake" name="test_case"/>'
            "</testsuite></testsuites>\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            command,
            self.run_exit_code,
            self.run_stdout,
            self.run_stderr,
        )

    @staticmethod
    def _junit_path(command: list[str]) -> Path:
        for index, argument in enumerate(command):
            if argument == "--junitxml":
                return Path(command[index + 1])
            if argument.startswith("--junitxml="):
                return Path(argument.split("=", 1)[1])
        raise AssertionError("pytest run command did not request JUnit XML")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "evidence@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Evidence Test"],
        cwd=repo,
        check=True,
    )
    (repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"],
        cwd=repo,
        check=True,
    )
    return repo


def _config(
    git_repo: Path,
    output_dir: Path,
    *pytest_args: str,
    suite_name: str = "runtime",
):
    return capture.CaptureConfig(
        output_dir=output_dir,
        suite_name=suite_name,
        pytest_args=pytest_args,
        repo_root=git_repo,
    )


def _read_json(output_dir: Path, name: str):
    return json.loads((output_dir / name).read_text(encoding="utf-8"))


def _assert_artifact_manifest(output_dir: Path) -> None:
    manifest = _read_json(output_dir, "artifact-manifest.json")
    recorded = {
        item["path"]: item
        for item in manifest["artifacts"]
    }
    expected_paths = {
        path.relative_to(output_dir).as_posix()
        for path in output_dir.rglob("*")
        if path.is_file() and path.name != "artifact-manifest.json"
    }
    assert set(recorded) == expected_paths
    for relative_path, item in recorded.items():
        artifact = output_dir / relative_path
        assert item["size"] == artifact.stat().st_size
        assert item["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()


def test_success_publishes_complete_atomic_evidence(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence-success"
    expected_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (git_repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    runner = FakePytestRunner(
        collect_stdout=(
            "tests/test_b.py::test_b\n"
            "tests/test_a.py::test_a\n"
            "\n2 tests collected in 0.01s\n"
        ),
        run_stdout=".. 2 passed in 0.02s\n",
    )

    exit_code = capture.capture_runtime_evidence(
        _config(git_repo, output_dir, "-q", "tests"),
        runner=runner,
        environ={"LANG": "C.UTF-8", "IGNORED_FLAG": "value"},
        distributions=[("Beta_Pkg", "2.0"), ("alpha.pkg", "1.0")],
    )

    assert exit_code == 0
    assert output_dir.is_dir()
    assert list(tmp_path.glob(".evidence-success.tmp-*")) == []
    assert (output_dir / "git-head.txt").read_text(encoding="utf-8") == expected_head
    git_status = (output_dir / "git-status.txt").read_text(encoding="utf-8")
    assert "## " in git_status
    assert " M tracked.txt" in git_status
    assert (output_dir / "nodeids.txt").read_text(encoding="utf-8") == (
        "tests/test_a.py::test_a\n"
        "tests/test_b.py::test_b\n"
    )
    assert (output_dir / "distributions.txt").read_text(encoding="utf-8") == (
        "alpha-pkg==1.0\n"
        "beta-pkg==2.0\n"
    )
    result = _read_json(output_dir, "result.json")
    assert result["status"] == "passed"
    assert result["collect_exit_code"] == 0
    assert result["pytest_exit_code"] == 0
    assert result["nodeid_count"] == 2
    assert result["junit_generated_by_pytest"] is True
    assert len(runner.pytest_commands) == 2
    assert runner.pytest_commands[0].count("-q") == 1
    assert runner.commands[0][:3] == ["git", "rev-parse", "--verify"]
    assert runner.commands[1][:2] == ["git", "status"]
    assert "--collect-only" in runner.commands[2]
    assert "--junitxml" in runner.commands[3]
    _assert_artifact_manifest(output_dir)


def test_test_failure_returns_pytest_code_and_keeps_evidence(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence-test-failure"
    runner = FakePytestRunner(
        collect_stdout="tests/test_sample.py::test_failure\n",
        run_stdout="F\n",
        run_stderr="assert 1 == 2\n",
        run_exit_code=1,
    )

    exit_code = capture.capture_runtime_evidence(
        _config(git_repo, output_dir, "tests/test_sample.py"),
        runner=runner,
        environ={"LC_ALL": "C"},
        distributions=[],
    )

    assert exit_code == 1
    result = _read_json(output_dir, "result.json")
    assert result["status"] == "failed"
    assert result["collect_exit_code"] == 0
    assert result["pytest_exit_code"] == 1
    assert (output_dir / "pytest.stderr.txt").read_text(encoding="utf-8") == (
        "assert 1 == 2\n"
    )
    assert (output_dir / "junit.xml").is_file()
    _assert_artifact_manifest(output_dir)


def test_collection_failure_still_runs_pytest_and_publishes_evidence(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence-collection-failure"
    runner = FakePytestRunner(
        collect_stdout="",
        collect_stderr="collection error\n",
        collect_exit_code=2,
        run_stderr="collection error\n",
        run_exit_code=2,
    )

    exit_code = capture.capture_runtime_evidence(
        _config(git_repo, output_dir, "tests/broken.py"),
        runner=runner,
        environ={},
        distributions=[],
    )

    assert exit_code == 2
    result = _read_json(output_dir, "result.json")
    assert result["status"] == "failed"
    assert result["collect_exit_code"] == 2
    assert result["pytest_exit_code"] == 2
    assert result["nodeid_count"] == 0
    assert result["nodeids_sha256"] == hashlib.sha256(b"").hexdigest()
    assert len(runner.pytest_commands) == 2
    _assert_artifact_manifest(output_dir)


def test_sensitive_environment_and_command_values_are_redacted(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence-redaction"
    runner = FakePytestRunner(
        collect_stdout="tests/test_auth.py::test_auth[cli-secret]\n",
        run_stdout=(
            "env-secret cli-secret inline-secret "
            "postgresql://user:password@example.invalid/db\n"
        ),
    )

    exit_code = capture.capture_runtime_evidence(
        _config(
            git_repo,
            output_dir,
            "--api-token",
            "cli-secret",
            "--password=inline-secret",
            "tests/test_auth.py",
        ),
        runner=runner,
        environ={
            "LANG": "C.UTF-8",
            "API_TOKEN": "env-secret",
            "DATABASE_URL": "postgresql://user:password@example.invalid/db",
            "CUSTOM_FLAG": "visible-name-only",
        },
        distributions=[],
    )

    assert exit_code == 0
    environment = _read_json(output_dir, "environment.json")
    assert environment["inherited"] == {"LANG": "C.UTF-8"}
    assert environment["omitted_names"] == [
        "API_TOKEN",
        "CUSTOM_FLAG",
        "DATABASE_URL",
    ]
    command_text = (output_dir / "commands.json").read_text(encoding="utf-8")
    assert "--api-token" in command_text
    assert "--password=[REDACTED]" in command_text
    all_artifacts = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in output_dir.rglob("*")
        if path.is_file()
    )
    for secret in (
        "env-secret",
        "cli-secret",
        "inline-secret",
        "postgresql://user:password@example.invalid/db",
    ):
        assert secret not in all_artifacts


def test_inventory_and_distribution_hashes_are_deterministic(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    first_output = tmp_path / "evidence-first"
    second_output = tmp_path / "evidence-second"
    first_runner = FakePytestRunner(
        collect_stdout=(
            "tests/test_z.py::test_z\n"
            "tests/test_a.py::test_a\n"
        ),
        run_stdout="2 passed\n",
    )
    second_runner = FakePytestRunner(
        collect_stdout=(
            "tests/test_a.py::test_a\n"
            "tests/test_z.py::test_z\n"
        ),
        run_stdout="2 passed\n",
    )

    first_exit = capture.capture_runtime_evidence(
        _config(git_repo, first_output, "tests"),
        runner=first_runner,
        environ={"LC_ALL": "C"},
        distributions=[("Zeta_Pkg", "3"), ("alpha.pkg", "1")],
    )
    second_exit = capture.capture_runtime_evidence(
        _config(git_repo, second_output, "tests"),
        runner=second_runner,
        environ={"LC_ALL": "C"},
        distributions=[("ALPHA-PKG", "1"), ("zeta.pkg", "3")],
    )

    assert first_exit == 0
    assert second_exit == 0
    first_result = _read_json(first_output, "result.json")
    second_result = _read_json(second_output, "result.json")
    assert first_result["nodeids_sha256"] == second_result["nodeids_sha256"]
    assert (
        first_result["distributions_sha256"]
        == second_result["distributions_sha256"]
    )
    assert (first_output / "nodeids.txt").read_bytes() == (
        second_output / "nodeids.txt"
    ).read_bytes()
    assert (first_output / "distributions.txt").read_bytes() == (
        second_output / "distributions.txt"
    ).read_bytes()
