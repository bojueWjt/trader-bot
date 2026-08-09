from __future__ import annotations

import hashlib
import importlib.metadata as importlib_metadata
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
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
        junit_xml: str | None = None,
    ) -> None:
        self.collect_stdout = collect_stdout
        self.collect_stderr = collect_stderr
        self.collect_exit_code = collect_exit_code
        self.run_stdout = run_stdout
        self.run_stderr = run_stderr
        self.run_exit_code = run_exit_code
        self.junit_test_count = junit_test_count
        self.junit_xml = junit_xml
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
        if self.junit_xml is not None:
            junit_path.write_text(self.junit_xml, encoding="utf-8")
            return subprocess.CompletedProcess(
                command,
                self.run_exit_code,
                self.run_stdout,
                self.run_stderr,
            )
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
        if (
            path.is_file()
            and path.relative_to(output_dir).as_posix()
            != "artifact-manifest.json"
        )
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
    assert output_dir.is_symlink() is False
    assert (tmp_path / ".evidence-success.payload").exists() is False
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
    for command in runner.pytest_commands:
        assert command.count("-o") == 2
        assert "addopts=" in command
        assert "filterwarnings=default" in command
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
    package_path = tmp_path / "sample.py"
    metadata_path.write_text("Name: sample-pkg\nVersion: 1.0\n", encoding="utf-8")
    record_path.write_text("sample.py,sha256=abc,3\n", encoding="utf-8")
    wheel_path.write_text("Wheel-Version: 1.0\n", encoding="utf-8")
    package_path.write_text("one\n", encoding="utf-8")

    class FakeDistribution:
        version = "1.0"
        files = (
            Path("sample_pkg-1.0.dist-info/METADATA"),
            Path("sample_pkg-1.0.dist-info/RECORD"),
            Path("sample_pkg-1.0.dist-info/WHEEL"),
            Path("sample.py"),
        )

        def __init__(self) -> None:
            self.metadata = {"Name": "Sample_Pkg"}
            self._path = dist_info

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
            "installed_files": [
                {
                    "path": "sample.py",
                    "sha256": hashlib.sha256(package_path.read_bytes()).hexdigest(),
                    "size": package_path.stat().st_size,
                },
                {
                    "path": "sample_pkg-1.0.dist-info/METADATA",
                    "sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
                    "size": metadata_path.stat().st_size,
                },
                {
                    "path": "sample_pkg-1.0.dist-info/RECORD",
                    "sha256": hashlib.sha256(record_path.read_bytes()).hexdigest(),
                    "size": record_path.stat().st_size,
                },
                {
                    "path": "sample_pkg-1.0.dist-info/WHEEL",
                    "sha256": hashlib.sha256(wheel_path.read_bytes()).hexdigest(),
                    "size": wheel_path.stat().st_size,
                },
            ],
        }
    ]

    first_hash = hashlib.sha256(
        capture._distribution_artifact_bytes(records)
    ).hexdigest()
    package_path.write_text("two\n", encoding="utf-8")
    second_hash = hashlib.sha256(
        capture._distribution_artifact_bytes(capture._distribution_artifact_records())
    ).hexdigest()
    assert second_hash != first_hash


def test_distribution_without_record_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dist_info = tmp_path / "sample_pkg-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Name: sample-pkg\nVersion: 1.0\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "sample.py"
    package_path.write_text("sample\n", encoding="utf-8")

    class FakeDistribution:
        version = "1.0"
        files = (Path("sample.py"),)

        def __init__(self) -> None:
            self.metadata = {"Name": "sample-pkg"}
            self._path = dist_info

        def locate_file(self, package_file: Path) -> Path:
            return tmp_path / package_file

    monkeypatch.setattr(
        capture.metadata,
        "distributions",
        lambda: (FakeDistribution(),),
    )

    with pytest.raises(capture.CaptureError, match="lacks RECORD"):
        capture._distribution_artifact_records()


def test_distribution_direct_url_must_match_installed_file_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dist_info = tmp_path / "sample_pkg-1.0.dist-info"
    dist_info.mkdir()
    metadata_path = dist_info / "METADATA"
    record_path = dist_info / "RECORD"
    direct_url_path = dist_info / "direct_url.json"
    metadata_path.write_text(
        "Name: sample-pkg\nVersion: 1.0\n",
        encoding="utf-8",
    )
    record_path.write_text(
        "sample_pkg-1.0.dist-info/METADATA,,\n"
        "sample_pkg-1.0.dist-info/RECORD,,\n"
        "sample_pkg-1.0.dist-info/direct_url.json,,\n",
        encoding="utf-8",
    )
    direct_url_path.write_text(
        '{"dir_info":{"editable":false}}\n',
        encoding="utf-8",
    )

    class FakeDistribution:
        version = "1.0"
        files = (
            Path("sample_pkg-1.0.dist-info/METADATA"),
            Path("sample_pkg-1.0.dist-info/RECORD"),
            Path("sample_pkg-1.0.dist-info/direct_url.json"),
        )

        def __init__(self) -> None:
            self.metadata = {"Name": "sample-pkg"}
            self._path = dist_info

        def locate_file(self, package_file: Path) -> Path:
            return tmp_path / package_file

    real_regular_file_bytes = capture._regular_file_bytes

    def changed_direct_url(path: Path) -> bytes:
        if path == direct_url_path:
            return b'{"dir_info":{"editable":true}}\n'
        return real_regular_file_bytes(path)

    monkeypatch.setattr(
        capture.metadata,
        "distributions",
        lambda: (FakeDistribution(),),
    )
    monkeypatch.setattr(
        capture,
        "_regular_file_bytes",
        changed_direct_url,
    )

    with pytest.raises(capture.CaptureError, match="changed while being recorded"):
        capture._distribution_artifact_records()


def test_real_distribution_record_identity_and_install_roots_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ambiguous_site = tmp_path / "ambiguous-site"
    ambiguous_site.mkdir()
    ambiguous_dist_info = ambiguous_site / "sample-1.0.dist-info"
    ambiguous_dist_info.mkdir()
    (ambiguous_dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: sample\nVersion: 1.0\n",
        encoding="utf-8",
    )
    package = ambiguous_site / "package"
    package.mkdir()
    (package / "RECORD").write_text("unrelated\n", encoding="utf-8")
    (ambiguous_site / "sample.py").write_text("sample\n", encoding="utf-8")
    (ambiguous_dist_info / "RECORD").write_text(
        "package/RECORD,,\nsample.py,,\n",
        encoding="utf-8",
    )
    ambiguous_distributions = tuple(
        importlib_metadata.distributions(path=[str(ambiguous_site)])
    )

    with monkeypatch.context() as isolated:
        isolated.setattr(
            capture.metadata,
            "distributions",
            lambda: ambiguous_distributions,
        )
        with pytest.raises(capture.CaptureError, match="lacks RECORD"):
            capture._distribution_artifact_records()

    wrong_record_site = tmp_path / "wrong-record-site"
    wrong_record_site.mkdir()
    wrong_record_dist_info = wrong_record_site / "sample-1.0.dist-info"
    wrong_record_dist_info.mkdir()
    (wrong_record_dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: sample\nVersion: 1.0\n",
        encoding="utf-8",
    )
    unrelated_dist_info = wrong_record_site / "other-9.9.dist-info"
    unrelated_dist_info.mkdir()
    (unrelated_dist_info / "RECORD").write_text(
        "attacker-controlled-record\n",
        encoding="utf-8",
    )
    (wrong_record_site / "sample.py").write_text(
        "sample\n",
        encoding="utf-8",
    )
    (wrong_record_dist_info / "RECORD").write_text(
        "other-9.9.dist-info/RECORD,,\nsample.py,,\n",
        encoding="utf-8",
    )
    wrong_record_distributions = tuple(
        distribution
        for distribution in importlib_metadata.distributions(
            path=[str(wrong_record_site)]
        )
        if distribution.metadata.get("Name") == "sample"
    )

    with monkeypatch.context() as isolated:
        isolated.setattr(
            capture.metadata,
            "distributions",
            lambda: wrong_record_distributions,
        )
        with pytest.raises(capture.CaptureError, match="exact RECORD"):
            capture._distribution_artifact_records()

    traversal_site = tmp_path / "traversal-site"
    traversal_site.mkdir()
    traversal_dist_info = traversal_site / "sample-1.0.dist-info"
    traversal_dist_info.mkdir()
    (traversal_dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: sample\nVersion: 1.0\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside-host.bin"
    outside.write_bytes(b"outside-host-bytes")
    (traversal_dist_info / "RECORD").write_text(
        "sample-1.0.dist-info/RECORD,,\n../outside-host.bin,,\n",
        encoding="utf-8",
    )
    traversal_distributions = tuple(
        importlib_metadata.distributions(path=[str(traversal_site)])
    )

    with monkeypatch.context() as isolated:
        isolated.setattr(
            capture.metadata,
            "distributions",
            lambda: traversal_distributions,
        )
        with pytest.raises(capture.CaptureError, match="unsafe"):
            capture._distribution_artifact_records()

    global_root_site = tmp_path / "global-root-site"
    global_root_site.mkdir()
    global_root_dist_info = global_root_site / "sample-1.0.dist-info"
    global_root_dist_info.mkdir()
    (global_root_dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: sample\nVersion: 1.0\n",
        encoding="utf-8",
    )
    interpreter = Path(sys.executable).resolve(strict=True)
    interpreter_relative = Path(
        os.path.relpath(
            interpreter,
            global_root_site.resolve(strict=True),
        )
    )
    (global_root_dist_info / "RECORD").write_text(
        "sample-1.0.dist-info/RECORD,,\n"
        f"{interpreter_relative.as_posix()},,\n",
        encoding="utf-8",
    )
    global_root_distributions = tuple(
        distribution
        for distribution in importlib_metadata.distributions(
            path=[str(global_root_site)]
        )
        if distribution.metadata.get("Name") == "sample"
    )

    with monkeypatch.context() as isolated:
        isolated.setattr(
            capture.metadata,
            "distributions",
            lambda: global_root_distributions,
        )
        with pytest.raises(capture.CaptureError, match="unsafe"):
            capture._distribution_artifact_records()


def test_distribution_rejects_cross_distribution_file_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    shared_path = site / "shared.py"
    shared_path.write_text("shared\n", encoding="utf-8")

    for name in ("alpha", "beta"):
        dist_info = site / f"{name}-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
            encoding="utf-8",
        )
        (dist_info / "RECORD").write_text(
            f"{name}-1.0.dist-info/RECORD,,\nshared.py,,\n",
            encoding="utf-8",
        )

    distributions = tuple(importlib_metadata.distributions(path=[str(site)]))
    monkeypatch.setattr(
        capture.metadata,
        "distributions",
        lambda: distributions,
    )

    with pytest.raises(capture.CaptureError, match="cross-distribution"):
        capture._distribution_artifact_records()


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
    stored_nodeids = (output_dir / "nodeids.txt").read_bytes()
    result = _read_json(output_dir, "result.json")
    assert result["nodeids_sha256"] == hashlib.sha256(stored_nodeids).hexdigest()
    assert (
        result["nodeids_sha256"]
        != hashlib.sha256(b"tests/test_auth.py::test_auth[pg-secret]\n").hexdigest()
    )


def test_malformed_junit_is_sanitized_before_capture_error_publication(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence-malformed-junit"
    runner = FakePytestRunner(
        collect_stdout="tests/test_auth.py::test_auth\n",
        junit_xml='<testsuites secret="pg-secret">',
    )

    exit_code = capture.capture_runtime_evidence(
        _config(git_repo, output_dir, "tests"),
        runner=runner,
        environ={"PGPASSWORD": "pg-secret"},
        distributions=[],
    )

    assert exit_code == 2
    result = _read_json(output_dir, "result.json")
    assert result["status"] == "capture-error"
    for path in output_dir.rglob("*"):
        if path.is_file():
            assert "pg-secret" not in path.read_text(
                encoding="utf-8",
                errors="replace",
            )


def test_managed_ini_overrides_restore_warning_evidence(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence-warning-config"
    (git_repo / "pytest.ini").write_text(
        "[pytest]\naddopts = --disable-warnings\nfilterwarnings = ignore\n",
        encoding="utf-8",
    )
    (git_repo / "test_warning.py").write_text(
        "import warnings\n\n"
        "def test_warning():\n"
        "    warnings.warn('visible warning', RuntimeWarning)\n",
        encoding="utf-8",
    )

    exit_code = capture.capture_runtime_evidence(
        _config(git_repo, output_dir, "-q", "test_warning.py"),
        environ={"PATH": os.environ["PATH"]},
        distributions=[],
    )

    assert exit_code == 0
    result = _read_json(output_dir, "result.json")
    assert result["warning_count"] == 1
    report = _read_json(output_dir, "test-report.json")
    assert report["warnings"]["count"] == 1
    assert any(
        "RuntimeWarning: visible warning" in line
        for line in report["warnings"]["summary"]
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


def test_manifest_audits_nested_manifest_name_and_rejects_symlinks(
    tmp_path: Path,
) -> None:
    staging_dir = tmp_path / "evidence"
    staging_dir.mkdir()
    nested = staging_dir / "nested"
    nested.mkdir()
    (nested / "artifact-manifest.json").write_text(
        "nested evidence\n",
        encoding="utf-8",
    )

    manifest = capture._artifact_manifest(staging_dir)

    assert {
        item["path"] for item in manifest["artifacts"]
    } == {"nested/artifact-manifest.json"}

    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (staging_dir / "linked.txt").symlink_to(outside)
    with pytest.raises(capture.CaptureError, match="symlink"):
        capture._sanitize_staging_files(staging_dir, ("outside",))
    assert outside.read_text(encoding="utf-8") == "outside\n"

    with pytest.raises(capture.CaptureError, match="contains a symlink"):
        capture._artifact_manifest(staging_dir)


def test_parent_directory_symlink_is_rejected_before_sanitization(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    outside = real_parent / "outside.txt"
    outside.write_text("outside secret\n", encoding="utf-8")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(capture.CaptureError, match="symlink"):
        capture._sanitize_file(linked_parent / "outside.txt", ("secret",))

    assert outside.read_text(encoding="utf-8") == "outside secret\n"


def test_manifest_rejects_non_regular_payload_entries(tmp_path: Path) -> None:
    staging_dir = tmp_path / "evidence"
    staging_dir.mkdir()
    (staging_dir / "result.json").write_text("{}\n", encoding="utf-8")
    os.mkfifo(staging_dir / "unversionable.pipe")

    with pytest.raises(capture.CaptureError, match="unsupported file type"):
        capture._artifact_manifest(staging_dir)


def test_manifest_rejects_socket_payload_entry() -> None:
    temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
    with tempfile.TemporaryDirectory(
        prefix="a3-socket-",
        dir=temporary_root,
    ) as temp_dir:
        staging_dir = Path(temp_dir) / "evidence"
        staging_dir.mkdir()
        socket_path = staging_dir / "payload.socket"
        unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            unix_socket.bind(str(socket_path))
            with pytest.raises(
                capture.CaptureError,
                match="unsupported file type",
            ):
                capture._artifact_manifest(staging_dir)
        finally:
            unix_socket.close()


@pytest.mark.parametrize("device_mode", (stat.S_IFCHR, stat.S_IFBLK))
def test_manifest_rejects_device_payload_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    device_mode: int,
) -> None:
    staging_dir = tmp_path / "evidence"
    staging_dir.mkdir()
    directory_fd = capture._open_directory_path_nofollow(staging_dir)

    class DeviceEntry:
        name = "unversionable.device"

        @staticmethod
        def stat(*, follow_symlinks: bool):
            assert follow_symlinks is False
            return os.stat_result((device_mode | 0o600, *([0] * 9)))

    monkeypatch.setattr(
        capture,
        "_payload_entries",
        lambda _directory_fd: [DeviceEntry()],
    )
    try:
        with pytest.raises(capture.CaptureError, match="unsupported file type"):
            capture._artifact_manifest_from_fd(
                directory_fd,
                display_dir=staging_dir,
            )
    finally:
        os.close(directory_fd)


def test_capture_rejects_payload_symlink_without_writing_external_target(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-original\n", encoding="utf-8")
    delegate = FakePytestRunner(
        collect_stdout="tests/test_sample.py::test_ok\n",
        run_stdout="1 passed\n",
    )

    def runner(command, **kwargs):
        result = delegate(command, **kwargs)
        command_values = [str(value) for value in command]
        if command_values[0] == "git":
            return result
        if "--collect-only" in command_values:
            return result
        junit_path = delegate._junit_path(command_values)
        (junit_path.parent / "result.json").symlink_to(outside)
        return result

    with pytest.raises(capture.CaptureError, match="symlink"):
        capture.capture_runtime_evidence(
            _config(git_repo, output_dir, "tests/test_sample.py"),
            runner=runner,
            environ={"LC_ALL": "C"},
            distributions=[],
        )

    assert outside.read_text(encoding="utf-8") == "outside-original\n"
    assert output_dir.exists() is False


def test_capture_rejects_symlinked_output_parent(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    runner = FakePytestRunner(
        collect_stdout="tests/test_sample.py::test_ok\n",
        run_stdout="1 passed\n",
    )

    with pytest.raises(capture.CaptureError, match="symlink"):
        capture.capture_runtime_evidence(
            _config(
                git_repo,
                linked_parent / "evidence",
                "tests/test_sample.py",
            ),
            runner=runner,
            environ={"LC_ALL": "C"},
            distributions=[],
        )

    assert list(real_parent.iterdir()) == []


def test_capture_rejects_output_parent_replaced_after_pytest(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    output_parent = tmp_path / "publication"
    output_parent.mkdir()
    output_dir = output_parent / "evidence"
    preserved_parent = tmp_path / "preserved-publication"
    delegate = FakePytestRunner(
        collect_stdout="tests/test_sample.py::test_ok\n",
        run_stdout="1 passed\n",
    )
    parent_replaced = False

    def runner(command, **kwargs):
        nonlocal parent_replaced
        result = delegate(command, **kwargs)
        command_values = [str(value) for value in command]
        if command_values[0] == "git":
            return result
        if "--collect-only" in command_values:
            return result
        if parent_replaced:
            return result
        parent_replaced = True
        output_parent.rename(preserved_parent)
        output_parent.mkdir()
        return result

    with pytest.raises(capture.CaptureError, match="output parent.*identity changed"):
        capture.capture_runtime_evidence(
            _config(git_repo, output_dir, "tests/test_sample.py"),
            runner=runner,
            environ={"LC_ALL": "C"},
            distributions=[],
        )

    assert list(output_parent.iterdir()) == []
    assert (preserved_parent / ".evidence.payload").is_dir()


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


def test_publication_never_replaces_external_output_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence"
    staging_dir = tmp_path / ".evidence.payload-test"
    staging_dir.mkdir()
    (staging_dir / "result.json").write_text("{}\n", encoding="utf-8")
    capture._write_artifact_manifest(staging_dir)
    real_rename = capture._rename_directory_noreplace_at

    def create_competing_output(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
        display_destination: Path,
    ) -> None:
        output_dir.mkdir()
        (output_dir / "owner.txt").write_text("external\n", encoding="utf-8")
        real_rename(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
            display_destination,
        )

    monkeypatch.setattr(
        capture,
        "_rename_directory_noreplace_at",
        create_competing_output,
    )

    with pytest.raises(capture.CaptureError, match="output path appeared"):
        capture._publish_staging_directory(
            staging_dir=staging_dir,
            output_dir=output_dir,
        )

    assert staging_dir.is_dir()
    assert output_dir.is_dir()
    assert (output_dir / "owner.txt").read_text(encoding="utf-8") == "external\n"


def test_publication_quarantines_replaced_staging_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence"
    staging_dir = tmp_path / ".evidence.payload"
    staging_dir.mkdir()
    (staging_dir / "result.json").write_text("trusted\n", encoding="utf-8")
    capture._write_artifact_manifest(staging_dir)

    replacement_dir = tmp_path / ".replacement"
    replacement_dir.mkdir()
    (replacement_dir / "result.json").write_text(
        "replacement\n",
        encoding="utf-8",
    )
    capture._write_artifact_manifest(replacement_dir)
    preserved_dir = tmp_path / ".preserved"
    real_rename = capture._rename_directory_noreplace_at

    def replace_source_before_rename(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
        display_destination: Path,
    ) -> None:
        if source_name != staging_dir.name:
            real_rename(
                source_fd,
                source_name,
                destination_fd,
                destination_name,
                display_destination,
            )
            return
        staging_dir.rename(preserved_dir)
        replacement_dir.rename(staging_dir)
        real_rename(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
            display_destination,
        )

    monkeypatch.setattr(
        capture,
        "_rename_directory_noreplace_at",
        replace_source_before_rename,
    )

    with pytest.raises(
        capture.CaptureError,
        match="directory identity changed",
    ):
        capture._publish_staging_directory(
            staging_dir=staging_dir,
            output_dir=output_dir,
        )

    assert output_dir.exists() is False
    assert (preserved_dir / "result.json").read_text(
        encoding="utf-8"
    ) == "trusted\n"
    rejected = list(tmp_path.glob(".evidence.rejected-*"))
    assert len(rejected) == 1
    assert (rejected[0] / "result.json").read_text(
        encoding="utf-8"
    ) == "replacement\n"


def test_publication_quarantines_manifest_changed_after_rename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence"
    staging_dir = tmp_path / ".evidence.payload"
    staging_dir.mkdir()
    (staging_dir / "result.json").write_text("trusted\n", encoding="utf-8")
    capture._write_artifact_manifest(staging_dir)
    real_rename = capture._rename_directory_noreplace_at
    publication_mutated = False

    def mutate_after_rename(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
        display_destination: Path,
    ) -> None:
        nonlocal publication_mutated
        real_rename(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
            display_destination,
        )
        if publication_mutated:
            return
        publication_mutated = True
        (display_destination / "artifact-manifest.json").write_text(
            "{}\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        capture,
        "_rename_directory_noreplace_at",
        mutate_after_rename,
    )

    with pytest.raises(capture.CaptureError, match="manifest changed"):
        capture._publish_staging_directory(
            staging_dir=staging_dir,
            output_dir=output_dir,
        )

    assert output_dir.exists() is False
    rejected = list(tmp_path.glob(".evidence.rejected-*"))
    assert len(rejected) == 1


@pytest.mark.parametrize(
    ("manifest_content", "payload_content"),
    (
        (False, "tampered\n"),
        ("{}\n", "trusted\n"),
    ),
)
def test_publication_rejects_manifest_not_bound_to_payload(
    tmp_path: Path,
    manifest_content: str | bool,
    payload_content: str,
) -> None:
    output_dir = tmp_path / "evidence"
    staging_dir = tmp_path / ".evidence.payload"
    staging_dir.mkdir()
    result_path = staging_dir / "result.json"
    result_path.write_text("trusted\n", encoding="utf-8")
    capture._write_artifact_manifest(staging_dir)
    result_path.write_text(payload_content, encoding="utf-8")
    if manifest_content is not False:
        (staging_dir / "artifact-manifest.json").write_text(
            manifest_content,
            encoding="utf-8",
        )

    with pytest.raises(capture.CaptureError, match="manifest"):
        capture._publish_staging_directory(
            staging_dir=staging_dir,
            output_dir=output_dir,
        )

    assert staging_dir.is_dir()
    assert output_dir.exists() is False


def test_publication_quarantines_payload_changed_after_rename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence"
    staging_dir = tmp_path / ".evidence.payload"
    staging_dir.mkdir()
    (staging_dir / "result.json").write_text("trusted\n", encoding="utf-8")
    capture._write_artifact_manifest(staging_dir)
    real_rename = capture._rename_directory_noreplace_at
    publication_mutated = False

    def mutate_after_rename(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
        display_destination: Path,
    ) -> None:
        nonlocal publication_mutated
        real_rename(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
            display_destination,
        )
        if publication_mutated:
            return
        publication_mutated = True
        (display_destination / "result.json").write_text(
            "tampered\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        capture,
        "_rename_directory_noreplace_at",
        mutate_after_rename,
    )

    with pytest.raises(capture.CaptureError, match="manifest"):
        capture._publish_staging_directory(
            staging_dir=staging_dir,
            output_dir=output_dir,
        )

    assert output_dir.exists() is False
    rejected = list(tmp_path.glob(".evidence.rejected-*"))
    assert len(rejected) == 1


def test_publication_fsyncs_payload_tree_before_rename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evidence"
    staging_dir = tmp_path / ".evidence.payload"
    nested_dir = staging_dir / "nested"
    nested_dir.mkdir(parents=True)
    result_path = staging_dir / "result.json"
    nested_path = nested_dir / "detail.txt"
    result_path.write_text("trusted\n", encoding="utf-8")
    nested_path.write_text("detail\n", encoding="utf-8")
    capture._write_artifact_manifest(staging_dir)

    payload_files = (
        result_path,
        nested_path,
        staging_dir / "artifact-manifest.json",
    )
    payload_directories = (staging_dir, nested_dir)
    expected_file_identities = {
        capture._entry_identity(path.stat()) for path in payload_files
    }
    expected_directory_identities = {
        capture._entry_identity(path.stat()) for path in payload_directories
    }
    synced_file_identities: set[tuple[int, int]] = set()
    synced_directory_identities: set[tuple[int, int]] = set()
    real_fsync = os.fsync
    real_rename = capture._rename_directory_noreplace_at

    def record_fsync(file_descriptor: int) -> None:
        file_stat = os.fstat(file_descriptor)
        identity = capture._entry_identity(file_stat)
        if stat.S_ISREG(file_stat.st_mode):
            synced_file_identities.add(identity)
        if stat.S_ISDIR(file_stat.st_mode):
            synced_directory_identities.add(identity)
        real_fsync(file_descriptor)

    def assert_fsync_before_rename(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
        display_destination: Path,
    ) -> None:
        assert expected_file_identities <= synced_file_identities
        assert expected_directory_identities <= synced_directory_identities
        real_rename(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
            display_destination,
        )

    monkeypatch.setattr(capture.os, "fsync", record_fsync)
    monkeypatch.setattr(
        capture,
        "_rename_directory_noreplace_at",
        assert_fsync_before_rename,
    )

    capture._publish_staging_directory(
        staging_dir=staging_dir,
        output_dir=output_dir,
    )

    assert output_dir.is_dir()


def test_published_directory_is_recursively_versionable(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    output_dir = git_repo / "evidence"
    runner = FakePytestRunner(
        collect_stdout="tests/test_sample.py::test_ok\n",
        run_stdout="1 passed\n",
    )
    exit_code = capture.capture_runtime_evidence(
        _config(git_repo, output_dir, "tests/test_sample.py"),
        runner=runner,
        environ={"LC_ALL": "C"},
        distributions=[],
    )
    assert exit_code == 0
    assert output_dir.is_dir()
    assert output_dir.is_symlink() is False

    subprocess.run(
        ["git", "add", "evidence"],
        cwd=git_repo,
        check=True,
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--stage", "evidence"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "\tevidence/result.json\n" in tracked
    assert "\tevidence/artifact-manifest.json\n" in tracked
