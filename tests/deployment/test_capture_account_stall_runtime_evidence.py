from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

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
        junit_test_count: int | None = None,
    ) -> None:
        self.collect_stdout = collect_stdout
        self.collect_stderr = collect_stderr
        self.collect_exit_code = collect_exit_code
        self.run_stdout = run_stdout
        self.run_stderr = run_stderr
        self.run_exit_code = run_exit_code
        self.junit_test_count = junit_test_count
        self.commands: list[list[str]] = []
        self.pytest_commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        command = [str(value) for value in command]
        self.commands.append(command)
        if command[0] == "git":
            return subprocess.run(command, **kwargs)  # noqa: PLW1510

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
        junit_test_count = self.junit_test_count
        nodeids = (
            capture._nodeids_bytes(self.collect_stdout).decode("utf-8").splitlines()
        )
        if junit_test_count is None:
            junit_test_count = len(nodeids)
        testcases = "".join(
            self._testcase_xml(nodeid) for nodeid in nodeids[:junit_test_count]
        )
        junit_path.write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<testsuites tests="{junit_test_count}">'
            f'<testsuite tests="{junit_test_count}">'
            f"{testcases}</testsuite></testsuites>\n",
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

    @staticmethod
    def _testcase_xml(nodeid: str) -> str:
        path, *identifiers = nodeid.split("::")
        module = path.removesuffix(".py").replace("/", ".")
        name = identifiers[-1]
        classname_parts = [module, *identifiers[:-1]]
        classname = ".".join(classname_parts)
        return f'<testcase classname="{classname}" name="{name}"/>'


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
    recorded = {item["path"]: item for item in manifest["artifacts"]}
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
        "tests/test_a.py::test_a\ntests/test_b.py::test_b\n"
    )
    assert (output_dir / "distributions.txt").read_text(encoding="utf-8") == (
        "alpha-pkg==1.0\nbeta-pkg==2.0\n"
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
        collect_stdout="tests/test_auth.py::test_auth\n",
        run_stdout=(
            "env-secret cli-secret inline-secret db-fragment-42 "
            "postgresql://user:db-fragment-42@example.invalid/db "
            "github-pat-secret pg-secret\n"
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
            "DATABASE_URL": ("postgresql://user:db-fragment-42@example.invalid/db"),
            "GITHUB_PAT": "github-pat-secret",
            "PGPASSWORD": "pg-secret",
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
        "GITHUB_PAT",
        "PGPASSWORD",
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
        "db-fragment-42",
        "github-pat-secret",
        "pg-secret",
        "postgresql://user:db-fragment-42@example.invalid/db",
    ):
        assert secret not in all_artifacts


def test_inventory_and_distribution_hashes_are_deterministic(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    first_output = tmp_path / "evidence-first"
    second_output = tmp_path / "evidence-second"
    first_runner = FakePytestRunner(
        collect_stdout=("tests/test_z.py::test_z\ntests/test_a.py::test_a\n"),
        run_stdout="2 passed\n",
    )
    second_runner = FakePytestRunner(
        collect_stdout=("tests/test_a.py::test_a\ntests/test_z.py::test_z\n"),
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
    assert first_result["distributions_sha256"] == second_result["distributions_sha256"]
    assert (first_output / "nodeids.txt").read_bytes() == (
        second_output / "nodeids.txt"
    ).read_bytes()
    assert (first_output / "distributions.txt").read_bytes() == (
        second_output / "distributions.txt"
    ).read_bytes()


def test_distribution_artifact_hashes_cover_installed_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dist_info = tmp_path / "sample_pkg-1.0.dist-info"
    dist_info.mkdir()
    metadata_path = dist_info / "METADATA"
    record_path = dist_info / "RECORD"
    wheel_path = dist_info / "WHEEL"
    metadata_path.write_text("Name: sample-pkg\nVersion: 1.0\n", encoding="utf-8")
    record_path.write_text("sample.py,sha256=abc,3\n", encoding="utf-8")
    wheel_path.write_text("Wheel-Version: 1.0\n", encoding="utf-8")

    class FakeDistribution:
        version = "1.0"
        files = (
            Path("sample_pkg-1.0.dist-info/METADATA"),
            Path("sample_pkg-1.0.dist-info/RECORD"),
            Path("sample_pkg-1.0.dist-info/WHEEL"),
        )

        def __init__(self) -> None:
            self.metadata = {"Name": "Sample_Pkg"}

        def locate_file(self, package_file: Path) -> Path:
            return tmp_path / package_file

    monkeypatch.setattr(
        capture.metadata,
        "distributions",
        lambda: (FakeDistribution(),),
    )

    records = capture._distribution_artifact_records()

    assert records == [
        {
            "editable": False,
            "name": "sample-pkg",
            "version": "1.0",
            "metadata_files": [
                {
                    "name": "METADATA",
                    "sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
                    "size": metadata_path.stat().st_size,
                },
                {
                    "name": "RECORD",
                    "sha256": hashlib.sha256(record_path.read_bytes()).hexdigest(),
                    "size": record_path.stat().st_size,
                },
                {
                    "name": "WHEEL",
                    "sha256": hashlib.sha256(wheel_path.read_bytes()).hexdigest(),
                    "size": wheel_path.stat().st_size,
                },
            ],
        }
    ]


def test_success_requires_collect_and_junit_counts_to_match(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence-nodeid-mismatch"
    runner = FakePytestRunner(
        collect_stdout=("tests/test_a.py::test_a\ntests/test_b.py::test_b\n"),
        run_stdout="1 passed\n",
        junit_test_count=1,
    )

    exit_code = capture.capture_runtime_evidence(
        _config(git_repo, output_dir, "tests"),
        runner=runner,
        environ={"LC_ALL": "C"},
        distributions=[],
    )

    assert exit_code == 2
    result = _read_json(output_dir, "result.json")
    assert result["status"] == "failed"
    assert result["nodeid_count"] == 2
    assert result["junit_test_count"] == 1
    assert result["capture_errors"] == [
        "collected node-id was not executed: tests/test_b.py::test_b"
    ]


def test_child_environment_removes_hidden_pytest_behavior() -> None:
    child = capture._child_environment(
        {
            "PATH": "/usr/bin",
            "PYTEST_ADDOPTS": "--collect-only",
            "PYTEST_PLUGINS": "hidden_plugin",
            "PYTHONPATH": "/tmp/injected",
        }
    )

    assert child["PATH"] == "/usr/bin"
    assert child["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert child["PYTHONHASHSEED"] == "0"
    assert child["PYTHONWARNINGS"] == "default"
    assert "PYTEST_ADDOPTS" not in child
    assert "PYTEST_PLUGINS" not in child
    assert "PYTHONPATH" not in child


def test_secret_nodeid_fails_closed(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence-secret-nodeid"
    runner = FakePytestRunner(
        collect_stdout="tests/test_auth.py::test_auth[pg-secret]\n",
        run_stdout="1 passed\n",
    )

    exit_code = capture.capture_runtime_evidence(
        _config(git_repo, output_dir, "tests"),
        runner=runner,
        environ={"PGPASSWORD": "pg-secret"},
        distributions=[],
    )

    assert exit_code == 2
    result = _read_json(output_dir, "result.json")
    assert result["capture_errors"] == [
        "collected node-id inventory contains a secret value",
        "executed node-id inventory contains a secret value",
    ]
    assert "pg-secret" not in (output_dir / "nodeids.txt").read_text(encoding="utf-8")
    assert "pg-secret" not in (output_dir / "executed-nodeids.txt").read_text(
        encoding="utf-8"
    )


def test_structured_warning_and_skip_report(tmp_path: Path) -> None:
    junit_path = tmp_path / "junit.xml"
    junit_path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite tests="1" skipped="1">'
        '<testcase classname="tests.test_sample" name="test_skip">'
        '<skipped message="requires redis">fixture unavailable</skipped>'
        "</testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    junit = capture._junit_report(junit_path, tmp_path)
    warnings = capture._warning_report(
        (
            "================ warnings summary ================\n"
            "tests/test_sample.py:10\n"
            "  RuntimeWarning: sample warning\n"
            "-- Docs: https://docs.pytest.org/\n"
            "1 skipped, 1 warning in 0.01s\n"
        ),
        "",
    )

    assert junit["skipped_count"] == 1
    assert junit["skips"] == [
        {
            "nodeid": "tests/test_sample.py::test_skip",
            "message": "requires redis",
            "detail": "fixture unavailable",
        }
    ]
    assert warnings["count"] == 1
    assert warnings["summary"] == [
        "tests/test_sample.py:10",
        "  RuntimeWarning: sample warning",
    ]


def test_publication_lock_preserves_staging(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence"
    staging_dir = tmp_path / ".evidence.tmp-test"
    lock_path = tmp_path / ".evidence.publish.lock"
    staging_dir.mkdir()
    lock_path.write_text("owner\n", encoding="utf-8")

    with pytest.raises(capture.CaptureError):
        capture._publish_staging_directory(
            staging_dir=staging_dir,
            output_dir=output_dir,
        )

    assert staging_dir.is_dir()
    assert not output_dir.exists()
