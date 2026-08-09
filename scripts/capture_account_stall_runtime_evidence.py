#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlsplit

SCHEMA_VERSION = "1.0"
REDACTED = "[REDACTED]"
ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "CI",
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
        "GITHUB_ACTIONS",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "PYTHONUNBUFFERED",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "SYSTEMROOT",
        "TMPDIR",
        "TZ",
        "VIRTUAL_ENV",
    }
)
SECRET_NAME_PATTERN = re.compile(
    r"(?:^|[-_])(?:"
    r"api[-_]?key|auth(?:orization)?|connection[-_]?string|cookie|"
    r"credential(?:s)?|database[-_]?url|db[-_]?url|dsn|key|"
    r"pass(?:word|wd)?|private[-_]?key|redis[-_]?url|secret|session|token"
    r")(?:$|[-_])",
    re.IGNORECASE,
)
SECRET_NAME_EXACT = frozenset(
    {
        "MYSQL_PWD",
        "PGPASSFILE",
        "PGPASSWORD",
        "GITHUB_PAT",
    }
)
DIST_INFO_EVIDENCE_FILES = frozenset(
    {
        "METADATA",
        "RECORD",
        "WHEEL",
        "direct_url.json",
    }
)
OPTION_PATTERN = re.compile(r"^--([A-Za-z0-9][A-Za-z0-9_-]*)(?:=(.*))?$")
ASSIGNMENT_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
OWNED_PYTEST_OPTIONS = frozenset(
    {
        "--co",
        "--collect-only",
        "--junit-xml",
        "--junitxml",
    }
)
FORBIDDEN_PYTEST_OPTIONS = frozenset(
    {
        "--disable-pytest-warnings",
        "--disable-warnings",
        "--pythonwarnings",
        "-W",
    }
)
CHILD_ENVIRONMENT_OVERRIDES = {
    "LC_ALL": "C",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    "PYTHONWARNINGS": "default",
    "TZ": "UTC",
}


class CaptureError(ValueError):
    pass


@dataclass(frozen=True)
class CaptureConfig:
    output_dir: Path
    suite_name: str
    pytest_args: Sequence[str]
    repo_root: Path


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    invocation_error: str | bool = False


Runner = Callable[..., subprocess.CompletedProcess]


def _json_text(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _write_json(path: Path, value: object) -> None:
    path.write_text(_json_text(value), encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    runner: Runner,
) -> CommandResult:
    command_tuple = tuple(str(value) for value in command)
    try:
        completed = runner(
            list(command_tuple),
            cwd=str(cwd),
            env=dict(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - invocation failures are evidence.
        message = f"{type(exc).__name__}: {exc}\n"
        return CommandResult(
            command=command_tuple,
            exit_code=127,
            stdout="",
            stderr=message,
            invocation_error=message.strip(),
        )

    return CommandResult(
        command=command_tuple,
        exit_code=int(completed.returncode),
        stdout=_normalized_output(completed.stdout),
        stderr=_normalized_output(completed.stderr),
    )


def _is_secret_name(name: str) -> bool:
    if name.upper() in SECRET_NAME_EXACT:
        return True
    return SECRET_NAME_PATTERN.search(name) is not None


def _secret_argument_values(arguments: Sequence[str]) -> list[str]:
    values: list[str] = []
    redact_next = False
    for argument in arguments:
        if redact_next:
            if argument:
                values.append(argument)
            redact_next = False
            continue

        option_match = OPTION_PATTERN.fullmatch(argument)
        if option_match is not None:
            option_name, option_value = option_match.groups()
            if not _is_secret_name(option_name):
                continue
            if option_value is None:
                redact_next = True
                continue
            if option_value:
                values.append(option_value)
            continue

        assignment_match = ASSIGNMENT_PATTERN.fullmatch(argument)
        if assignment_match is None:
            continue
        variable_name, variable_value = assignment_match.groups()
        if not _is_secret_name(variable_name):
            continue
        if variable_value:
            values.append(variable_value)

    return values


def _known_secret_values(
    environ: Mapping[str, str],
    pytest_args: Sequence[str],
) -> tuple[str, ...]:
    values = []
    for name, value in environ.items():
        if not _is_secret_name(name):
            continue
        if not value:
            continue
        values.append(value)
    values.extend(_secret_argument_values(pytest_args))
    expanded_values = []
    for value in values:
        expanded_values.append(value)
        expanded_values.extend(_secret_fragments(value))
    unique_values = set(expanded_values)
    return tuple(sorted(unique_values, key=len, reverse=True))


def _secret_fragments(value: str) -> tuple[str, ...]:
    fragments = []
    try:
        parsed = urlsplit(value)
    except ValueError:
        parsed = False
    if parsed is not False and parsed.password:
        fragments.append(parsed.password)
        fragments.append(unquote(parsed.password))
    assignment_pattern = re.compile(
        r"(?:^|[;,\s])(?:pass(?:word|wd)?|pwd|secret|token)=([^;,\s]+)",
        re.IGNORECASE,
    )
    fragments.extend(match.group(1) for match in assignment_pattern.finditer(value))
    return tuple(fragment for fragment in fragments if len(fragment) >= 3)


def _sanitize_text(value: str, secret_values: Sequence[str]) -> str:
    sanitized = value
    for secret_value in secret_values:
        if not secret_value:
            continue
        sanitized = sanitized.replace(secret_value, REDACTED)
    return sanitized


def _redact_command(
    command: Sequence[str],
    *,
    secret_values: Sequence[str],
    path_replacements: Mapping[str, str] | None = None,
) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    replacements = path_replacements
    if replacements is None:
        replacements = {}

    for original_argument in command:
        argument = str(original_argument)
        for source, destination in replacements.items():
            argument = argument.replace(source, destination)

        if redact_next:
            redacted.append(REDACTED)
            redact_next = False
            continue

        option_match = OPTION_PATTERN.fullmatch(argument)
        if option_match is not None:
            option_name, option_value = option_match.groups()
            if _is_secret_name(option_name):
                if option_value is None:
                    redacted.append(argument)
                    redact_next = True
                    continue
                redacted.append(f"--{option_name}={REDACTED}")
                continue

        assignment_match = ASSIGNMENT_PATTERN.fullmatch(argument)
        if assignment_match is not None:
            variable_name, _variable_value = assignment_match.groups()
            if _is_secret_name(variable_name):
                redacted.append(f"{variable_name}={REDACTED}")
                continue

        redacted.append(_sanitize_text(argument, secret_values))

    return redacted


def _environment_manifest(
    environ: Mapping[str, str],
    child_environment: Mapping[str, str],
    secret_values: Sequence[str],
) -> dict:
    inherited = {}
    omitted_names = []
    for name in sorted(environ):
        if name in ENVIRONMENT_ALLOWLIST and not _is_secret_name(name):
            inherited[name] = _sanitize_text(environ[name], secret_values)
            continue
        omitted_names.append(name)

    return {
        "schema_version": SCHEMA_VERSION,
        "allowlist": sorted(ENVIRONMENT_ALLOWLIST),
        "inherited": inherited,
        "child": {
            name: _sanitize_text(value, secret_values)
            for name, value in sorted(child_environment.items())
        },
        "omitted_names": omitted_names,
    }


def _child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    child = {
        name: value
        for name, value in environ.items()
        if name in ENVIRONMENT_ALLOWLIST and not _is_secret_name(name)
    }
    if not child.get("PATH"):
        child["PATH"] = os.defpath
    child.update(CHILD_ENVIRONMENT_OVERRIDES)
    return child


def _python_platform_manifest() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "python": {
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "version_detail": sys.version,
        },
        "platform": {
            "machine": platform.machine(),
            "platform": platform.platform(),
            "release": platform.release(),
            "system": platform.system(),
        },
    }


def _installed_distributions() -> list[tuple[str, str]]:
    installed = []
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            name = "unknown-distribution"
        installed.append((name, distribution.version))
    return installed


def _distribution_artifact_records() -> list[dict[str, object]]:
    records = []
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            name = "unknown-distribution"
        canonical_name = _normalize_distribution_name(str(name))
        version = str(distribution.version).strip()
        evidence_files = []
        editable = False
        for package_file in distribution.files or ():
            evidence_name = Path(str(package_file)).name
            if evidence_name not in DIST_INFO_EVIDENCE_FILES:
                continue
            path = Path(distribution.locate_file(package_file))
            if not path.is_file():
                continue
            if evidence_name == "direct_url.json":
                try:
                    direct_url = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, TypeError, ValueError):
                    direct_url = {}
                dir_info = direct_url.get("dir_info")
                if isinstance(dir_info, dict):
                    editable = bool(dir_info.get("editable"))
            evidence_files.append(
                {
                    "name": evidence_name,
                    "sha256": _sha256_path(path),
                    "size": path.stat().st_size,
                }
            )
        evidence_files.sort(key=lambda item: str(item["name"]).encode("utf-8"))
        records.append(
            {
                "name": canonical_name,
                "version": version,
                "editable": editable,
                "metadata_files": evidence_files,
            }
        )
    records.sort(
        key=lambda item: (
            str(item["name"]).encode("utf-8"),
            str(item["version"]).encode("utf-8"),
        )
    )
    return records


def _distribution_artifact_bytes(records: Sequence[Mapping[str, object]]) -> bytes:
    return (json.dumps(records, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _normalize_distribution_name(name: str) -> str:
    stripped = name.strip()
    if not stripped:
        return "unknown-distribution"
    return re.sub(r"[-_.]+", "-", stripped).lower()


def _normalized_distributions_bytes(
    distributions: Iterable[tuple[str, str]],
) -> bytes:
    normalized = []
    for name, version in distributions:
        canonical_name = _normalize_distribution_name(str(name))
        canonical_version = str(version).strip()
        normalized.append(f"{canonical_name}=={canonical_version}")
    normalized.sort(key=lambda value: value.encode("utf-8"))
    if not normalized:
        return b""
    return ("\n".join(normalized) + "\n").encode("utf-8")


def _nodeids_bytes(collect_stdout: str) -> bytes:
    nodeids = []
    for raw_line in collect_stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("=", "<", "ERROR ", "FAILED ", "WARNING ")):
            continue
        if "::" not in line:
            continue
        path_part = line.split("::", 1)[0]
        if not path_part.endswith(".py"):
            continue
        nodeids.append(line)

    nodeids.sort(key=lambda value: value.encode("utf-8"))
    if not nodeids:
        return b""
    return ("\n".join(nodeids) + "\n").encode("utf-8")


def _nodeids_text_bytes(nodeids: Sequence[str]) -> bytes:
    normalized = sorted(
        (str(nodeid).strip() for nodeid in nodeids if str(nodeid).strip()),
        key=lambda value: value.encode("utf-8"),
    )
    if not normalized:
        return b""
    return ("\n".join(normalized) + "\n").encode("utf-8")


def _duplicate_lines(content: bytes) -> tuple[str, ...]:
    lines = content.decode("utf-8").splitlines()
    seen = set()
    duplicates = set()
    for line in lines:
        if line in seen:
            duplicates.add(line)
            continue
        seen.add(line)
    return tuple(sorted(duplicates, key=lambda value: value.encode("utf-8")))


def _contains_secret(value: str, secret_values: Sequence[str]) -> bool:
    return any(secret and secret in value for secret in secret_values)


def _junit_report(path: Path, repo_root: Path) -> dict[str, object]:
    root = ET.parse(path).getroot()
    nodeids = []
    skips = []
    failure_count = 0
    error_count = 0
    for testcase in root.iter("testcase"):
        nodeid = _junit_testcase_nodeid(testcase, repo_root)
        nodeids.append(nodeid)
        skipped = testcase.find("skipped")
        if skipped is not None:
            skips.append(
                {
                    "nodeid": nodeid,
                    "message": str(skipped.attrib.get("message") or ""),
                    "detail": str(skipped.text or ""),
                }
            )
        if testcase.find("failure") is not None:
            failure_count += 1
        if testcase.find("error") is not None:
            error_count += 1
    nodeids.sort(key=lambda value: value.encode("utf-8"))
    skips.sort(key=lambda item: str(item["nodeid"]).encode("utf-8"))
    return {
        "nodeids": nodeids,
        "test_count": len(nodeids),
        "skipped_count": len(skips),
        "failure_count": failure_count,
        "error_count": error_count,
        "skips": skips,
    }


def _junit_testcase_nodeid(testcase: ET.Element, repo_root: Path) -> str:
    classname = str(testcase.attrib.get("classname") or "").strip()
    name = str(testcase.attrib.get("name") or "").strip()
    parts = [part for part in classname.split(".") if part]
    module_parts = []
    class_parts = []
    for index in range(len(parts), 0, -1):
        candidate = repo_root.joinpath(*parts[:index]).with_suffix(".py")
        if candidate.is_file():
            module_parts = parts[:index]
            class_parts = parts[index:]
            break
    if not module_parts:
        split_at = len(parts)
        for index, part in enumerate(parts):
            if part[:1].isupper():
                split_at = index
                break
        module_parts = parts[:split_at]
        class_parts = parts[split_at:]
    path = "/".join(module_parts) + ".py"
    identifiers = [*class_parts, name]
    suffix = "::".join(identifier for identifier in identifiers if identifier)
    if path == ".py":
        return f"junit://{classname}::{name}"
    if not suffix:
        return path
    return f"{path}::{suffix}"


def _inventory_coverage_errors(
    collected: Sequence[str],
    executed: Sequence[str],
) -> tuple[str, ...]:
    errors = []
    for nodeid in collected:
        if any(_executed_matches_collected(item, nodeid) for item in executed):
            continue
        errors.append(f"collected node-id was not executed: {nodeid}")
    for nodeid in executed:
        if any(_executed_matches_collected(nodeid, item) for item in collected):
            continue
        errors.append(f"executed node-id was not collected: {nodeid}")
    return tuple(errors)


def _executed_matches_collected(executed: str, collected: str) -> bool:
    if executed == collected:
        return True
    return executed.startswith(
        (
            f"{collected}[",
            f"{collected} ",
            f"{collected}::",
        )
    )


def _warning_report(stdout: str, stderr: str) -> dict[str, object]:
    combined = f"{stdout}\n{stderr}"
    warning_counts = [
        int(match.group(1)) for match in re.finditer(r"\b(\d+) warnings?\b", combined)
    ]
    summary = []
    in_summary = False
    for raw_line in combined.splitlines():
        line = raw_line.rstrip()
        if " warnings summary " in line:
            in_summary = True
            continue
        if not in_summary:
            continue
        if line.startswith("-- Docs:"):
            break
        if re.fullmatch(r"=+[^=]+=+", line):
            break
        if line:
            summary.append(line)
    count = 0
    if warning_counts:
        count = warning_counts[-1]
    return {
        "count": count,
        "summary": summary,
    }


def _sanitized_test_report(
    *,
    junit_report: Mapping[str, object],
    warning_report: Mapping[str, object],
    secret_values: Sequence[str],
) -> dict[str, object]:
    skips = []
    for raw_skip in junit_report["skips"]:
        skip = dict(raw_skip)
        skips.append(
            {
                "nodeid": _sanitize_text(
                    str(skip["nodeid"]),
                    secret_values,
                ),
                "message": _sanitize_text(
                    str(skip["message"]),
                    secret_values,
                ),
                "detail": _sanitize_text(
                    str(skip["detail"]),
                    secret_values,
                ),
            }
        )
    warning_summary = [
        _sanitize_text(str(line), secret_values) for line in warning_report["summary"]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "junit": {
            "test_count": int(junit_report["test_count"]),
            "skipped_count": int(junit_report["skipped_count"]),
            "failure_count": int(junit_report["failure_count"]),
            "error_count": int(junit_report["error_count"]),
            "skips": skips,
        },
        "warnings": {
            "count": int(warning_report["count"]),
            "summary": warning_summary,
        },
    }


def _validate_pytest_args(pytest_args: Sequence[str]) -> None:
    previous = ""
    for argument in pytest_args:
        if "\x00" in argument:
            raise CaptureError("pytest argument contains a NUL byte")
        if argument == "--":
            raise CaptureError("pytest arguments cannot contain an internal --")
        option_name = argument.split("=", 1)[0]
        if option_name in OWNED_PYTEST_OPTIONS:
            raise CaptureError(
                f"pytest option is managed by the evidence collector: {option_name}"
            )
        if option_name in FORBIDDEN_PYTEST_OPTIONS:
            raise CaptureError(f"pytest option can hide evidence: {option_name}")
        if argument.startswith("-W"):
            raise CaptureError("pytest warning filters are managed by the collector")
        if previous == "-p" and argument == "no:warnings":
            raise CaptureError("pytest warning plugin cannot be disabled")
        if argument in {"-pno:warnings", "-p=no:warnings"}:
            raise CaptureError("pytest warning plugin cannot be disabled")
        previous = argument


def _collection_pytest_args(pytest_args: Sequence[str]) -> list[str]:
    collection_args = []
    skip_next = False
    for argument in pytest_args:
        if skip_next:
            skip_next = False
            continue
        if argument in {"--quiet", "--verbose"}:
            continue
        if argument == "--verbosity":
            skip_next = True
            continue
        if argument.startswith("--verbosity="):
            continue
        if re.fullmatch(r"-[qv]+", argument) is not None:
            continue
        collection_args.append(argument)
    return collection_args


def _normalized_config(config: CaptureConfig) -> CaptureConfig:
    output_dir = config.output_dir.expanduser().resolve()
    repo_root = config.repo_root.expanduser().resolve()
    suite_name = config.suite_name.strip()
    pytest_args = tuple(str(value) for value in config.pytest_args)

    if not suite_name:
        raise CaptureError("suite name is empty")
    if re.search(r"[\x00-\x1f\x7f]", suite_name) is not None:
        raise CaptureError("suite name contains control characters")
    if not repo_root.is_dir():
        raise CaptureError(f"repository root is not a directory: {repo_root}")
    if output_dir.exists():
        raise CaptureError(f"output directory already exists: {output_dir}")
    _validate_pytest_args(pytest_args)

    return CaptureConfig(
        output_dir=output_dir,
        suite_name=suite_name,
        pytest_args=pytest_args,
        repo_root=repo_root,
    )


def _write_placeholder_junit(path: Path, suite_name: str) -> None:
    testsuites = ET.Element(
        "testsuites",
        {
            "tests": "1",
            "errors": "1",
        },
    )
    testsuite = ET.SubElement(
        testsuites,
        "testsuite",
        {
            "name": suite_name,
            "tests": "1",
            "errors": "1",
        },
    )
    testcase = ET.SubElement(
        testsuite,
        "testcase",
        {
            "classname": "account_stall_evidence",
            "name": "pytest_junit_artifact",
        },
    )
    ET.SubElement(
        testcase,
        "error",
        {
            "message": "pytest did not create JUnit XML",
        },
    )
    tree = ET.ElementTree(testsuites)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    with path.open("ab") as handle:
        handle.write(b"\n")


def _sanitize_file(path: Path, secret_values: Sequence[str]) -> None:
    content = path.read_text(encoding="utf-8", errors="replace")
    sanitized = _sanitize_text(content, secret_values)
    path.write_text(sanitized, encoding="utf-8")


def _artifact_manifest(staging_dir: Path) -> dict:
    artifacts = []
    for path in sorted(
        staging_dir.rglob("*"),
        key=lambda item: item.relative_to(staging_dir).as_posix(),
    ):
        if not path.is_file():
            continue
        if path.name == "artifact-manifest.json":
            continue
        relative_path = path.relative_to(staging_dir).as_posix()
        artifacts.append(
            {
                "path": relative_path,
                "sha256": _sha256_path(path),
                "size": path.stat().st_size,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm": "sha256",
        "manifest_excludes": ["artifact-manifest.json"],
        "artifacts": artifacts,
    }


def _write_artifact_manifest(staging_dir: Path) -> None:
    _write_json(
        staging_dir / "artifact-manifest.json",
        _artifact_manifest(staging_dir),
    )


def _normalized_exit_code(exit_code: int) -> int:
    if exit_code < 0:
        return 2
    if exit_code > 255:
        return 2
    return exit_code


def _result_exit_code(
    *,
    git_head: CommandResult,
    git_status: CommandResult,
    collect: CommandResult,
    pytest_run: CommandResult,
    capture_errors: Sequence[str],
) -> int:
    if capture_errors:
        return 2
    if git_head.exit_code != 0:
        return 2
    if git_status.exit_code != 0:
        return 2
    if collect.exit_code != 0:
        return _normalized_exit_code(collect.exit_code)
    return _normalized_exit_code(pytest_run.exit_code)


def _capture_into_staging(
    *,
    config: CaptureConfig,
    staging_dir: Path,
    runner: Runner,
    environ: Mapping[str, str],
    distributions: Iterable[tuple[str, str]] | None,
) -> int:
    secret_values = _known_secret_values(environ, config.pytest_args)
    child_environment = _child_environment(environ)

    git_head = _run_command(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=config.repo_root,
        env=child_environment,
        runner=runner,
    )
    git_status = _run_command(
        [
            "git",
            "status",
            "--short",
            "--branch",
            "--untracked-files=all",
        ],
        cwd=config.repo_root,
        env=child_environment,
        runner=runner,
    )

    capture_errors = []
    if git_head.invocation_error:
        capture_errors.append(str(git_head.invocation_error))
    if git_status.invocation_error:
        capture_errors.append(str(git_status.invocation_error))

    python_platform = _python_platform_manifest()
    environment_manifest = _environment_manifest(
        environ,
        child_environment,
        secret_values,
    )

    distribution_values: Iterable[tuple[str, str]]
    distribution_values = []
    distribution_artifact_records: list[dict[str, object]] = []
    if distributions is not None:
        distribution_values = distributions
    else:
        try:
            distribution_values = _installed_distributions()
            distribution_artifact_records = _distribution_artifact_records()
            editable_names = [
                f"{item['name']}=={item['version']}"
                for item in distribution_artifact_records
                if item["editable"] is True
            ]
            if editable_names:
                capture_errors.append(
                    "editable dependencies cannot be content-addressed: "
                    + ", ".join(editable_names)
                )
        except Exception as exc:  # noqa: BLE001 - metadata failures are evidence.
            message = f"{type(exc).__name__}: {exc}"
            capture_errors.append(message)

    distributions_content = _normalized_distributions_bytes(distribution_values)
    distributions_hash = _sha256_bytes(distributions_content)
    distribution_artifact_content = _distribution_artifact_bytes(
        distribution_artifact_records
    )
    distribution_artifact_hash = _sha256_bytes(distribution_artifact_content)

    collection_args = _collection_pytest_args(config.pytest_args)
    collect_command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        *collection_args,
        "--collect-only",
        "-q",
    ]
    collect = _run_command(
        collect_command,
        cwd=config.repo_root,
        env=child_environment,
        runner=runner,
    )
    if collect.invocation_error:
        capture_errors.append(str(collect.invocation_error))

    nodeids_content = _nodeids_bytes(collect.stdout)
    nodeids_hash = _sha256_bytes(nodeids_content)
    collected_nodeids = nodeids_content.decode("utf-8").splitlines()
    duplicate_nodeids = _duplicate_lines(nodeids_content)
    if duplicate_nodeids:
        capture_errors.append("collected inventory contains duplicate node-id values")
    raw_nodeids_text = nodeids_content.decode("utf-8")
    if _contains_secret(raw_nodeids_text, secret_values):
        capture_errors.append("collected node-id inventory contains a secret value")
    sanitized_collect_stdout = _sanitize_text(collect.stdout, secret_values)
    sanitized_nodeids_content = _sanitize_text(
        raw_nodeids_text,
        secret_values,
    ).encode("utf-8")
    junit_path = staging_dir / "junit.xml"
    pytest_command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        *config.pytest_args,
        "-ra",
        "--junitxml",
        str(junit_path),
    ]
    pytest_run = _run_command(
        pytest_command,
        cwd=config.repo_root,
        env=child_environment,
        runner=runner,
    )
    if pytest_run.invocation_error:
        capture_errors.append(str(pytest_run.invocation_error))

    junit_generated_by_pytest = junit_path.is_file()
    if not junit_generated_by_pytest:
        _write_placeholder_junit(junit_path, config.suite_name)
    junit_report = _junit_report(junit_path, config.repo_root)
    executed_nodeids = [str(nodeid) for nodeid in junit_report["nodeids"]]
    executed_nodeids_content = _nodeids_text_bytes(executed_nodeids)
    executed_nodeids_hash = _sha256_bytes(executed_nodeids_content)
    if _contains_secret(
        executed_nodeids_content.decode("utf-8"),
        secret_values,
    ):
        capture_errors.append("executed node-id inventory contains a secret value")
    duplicate_executed_nodeids = _duplicate_lines(executed_nodeids_content)
    if duplicate_executed_nodeids:
        capture_errors.append("executed inventory contains duplicate node-id values")
    if collect.exit_code == 0 and pytest_run.exit_code == 0:
        if not collected_nodeids:
            capture_errors.append(
                "pytest reported success with an empty collected inventory"
            )
        if not executed_nodeids:
            capture_errors.append(
                "pytest reported success with an empty executed inventory"
            )
        capture_errors.extend(
            _inventory_coverage_errors(
                collected_nodeids,
                executed_nodeids,
            )
        )
    warning_report = _warning_report(
        pytest_run.stdout,
        pytest_run.stderr,
    )
    _sanitize_file(junit_path, secret_values)
    collected_nodeid_count = len(collected_nodeids)
    junit_test_count = int(junit_report["test_count"])

    _write_text(
        staging_dir / "git-head.txt",
        _sanitize_text(git_head.stdout, secret_values),
    )
    _write_text(
        staging_dir / "git-head.stderr.txt",
        _sanitize_text(git_head.stderr, secret_values),
    )
    _write_text(
        staging_dir / "git-head.exit-code.txt",
        f"{git_head.exit_code}\n",
    )
    _write_text(
        staging_dir / "git-status.txt",
        _sanitize_text(git_status.stdout, secret_values),
    )
    _write_text(
        staging_dir / "git-status.stderr.txt",
        _sanitize_text(git_status.stderr, secret_values),
    )
    _write_text(
        staging_dir / "git-status.exit-code.txt",
        f"{git_status.exit_code}\n",
    )
    _write_json(
        staging_dir / "python-platform.json",
        python_platform,
    )
    _write_json(
        staging_dir / "environment.json",
        environment_manifest,
    )
    (staging_dir / "distributions.txt").write_bytes(distributions_content)
    _write_text(
        staging_dir / "distributions.sha256.txt",
        f"{distributions_hash}\n",
    )
    (staging_dir / "distribution-artifacts.json").write_bytes(
        distribution_artifact_content
    )
    _write_text(
        staging_dir / "distribution-artifacts.sha256.txt",
        f"{distribution_artifact_hash}\n",
    )
    _write_text(
        staging_dir / "collect.stdout.txt",
        sanitized_collect_stdout,
    )
    _write_text(
        staging_dir / "collect.stderr.txt",
        _sanitize_text(collect.stderr, secret_values),
    )
    _write_text(
        staging_dir / "collect.exit-code.txt",
        f"{collect.exit_code}\n",
    )
    (staging_dir / "nodeids.txt").write_bytes(sanitized_nodeids_content)
    _write_text(
        staging_dir / "nodeids.sha256.txt",
        f"{nodeids_hash}\n",
    )
    sanitized_executed_nodeids = _sanitize_text(
        executed_nodeids_content.decode("utf-8"),
        secret_values,
    ).encode("utf-8")
    (staging_dir / "executed-nodeids.txt").write_bytes(sanitized_executed_nodeids)
    _write_text(
        staging_dir / "executed-nodeids.sha256.txt",
        f"{executed_nodeids_hash}\n",
    )
    _write_text(
        staging_dir / "pytest.stdout.txt",
        _sanitize_text(pytest_run.stdout, secret_values),
    )
    _write_text(
        staging_dir / "pytest.stderr.txt",
        _sanitize_text(pytest_run.stderr, secret_values),
    )
    _write_text(
        staging_dir / "pytest.exit-code.txt",
        f"{pytest_run.exit_code}\n",
    )
    _write_json(
        staging_dir / "test-report.json",
        _sanitized_test_report(
            junit_report=junit_report,
            warning_report=warning_report,
            secret_values=secret_values,
        ),
    )

    path_replacements = {
        str(staging_dir): str(config.output_dir),
    }
    _write_json(
        staging_dir / "commands.json",
        {
            "schema_version": SCHEMA_VERSION,
            "suite_name": config.suite_name,
            "working_directory": str(config.repo_root),
            "git_head": {
                "argv": _redact_command(
                    git_head.command,
                    secret_values=secret_values,
                ),
                "exit_code": git_head.exit_code,
            },
            "git_status": {
                "argv": _redact_command(
                    git_status.command,
                    secret_values=secret_values,
                ),
                "exit_code": git_status.exit_code,
            },
            "collect": {
                "argv": _redact_command(
                    collect.command,
                    secret_values=secret_values,
                    path_replacements=path_replacements,
                ),
                "exit_code": collect.exit_code,
            },
            "pytest": {
                "argv": _redact_command(
                    pytest_run.command,
                    secret_values=secret_values,
                    path_replacements=path_replacements,
                ),
                "exit_code": pytest_run.exit_code,
            },
        },
    )

    result_exit_code = _result_exit_code(
        git_head=git_head,
        git_status=git_status,
        collect=collect,
        pytest_run=pytest_run,
        capture_errors=capture_errors,
    )
    status = "failed"
    if result_exit_code == 0:
        status = "passed"
    sanitized_errors = [
        _sanitize_text(message, secret_values) for message in capture_errors
    ]
    _write_json(
        staging_dir / "result.json",
        {
            "schema_version": SCHEMA_VERSION,
            "suite_name": config.suite_name,
            "status": status,
            "exit_code": result_exit_code,
            "git_head_exit_code": git_head.exit_code,
            "git_status_exit_code": git_status.exit_code,
            "collect_exit_code": collect.exit_code,
            "pytest_exit_code": pytest_run.exit_code,
            "nodeid_count": collected_nodeid_count,
            "nodeids_sha256": nodeids_hash,
            "executed_nodeid_count": len(executed_nodeids),
            "executed_nodeids_sha256": executed_nodeids_hash,
            "distribution_count": len(distributions_content.splitlines()),
            "distributions_sha256": distributions_hash,
            "distribution_artifact_count": len(distribution_artifact_records),
            "distribution_artifacts_sha256": distribution_artifact_hash,
            "junit_generated_by_pytest": junit_generated_by_pytest,
            "junit_test_count": junit_test_count,
            "warning_count": int(warning_report["count"]),
            "skipped_count": int(junit_report["skipped_count"]),
            "failure_count": int(junit_report["failure_count"]),
            "error_count": int(junit_report["error_count"]),
            "capture_errors": sanitized_errors,
            "known_secret_values_are_sanitized": True,
            "child_environment_overrides": CHILD_ENVIRONMENT_OVERRIDES,
        },
    )
    _write_artifact_manifest(staging_dir)
    return result_exit_code


def _write_unexpected_failure(
    *,
    staging_dir: Path,
    config: CaptureConfig,
    exc: Exception,
    secret_values: Sequence[str],
) -> None:
    error_message = _sanitize_text(
        f"{type(exc).__name__}: {exc}",
        secret_values,
    )
    _write_json(
        staging_dir / "capture-error.json",
        {
            "schema_version": SCHEMA_VERSION,
            "suite_name": config.suite_name,
            "error": error_message,
        },
    )
    _write_json(
        staging_dir / "result.json",
        {
            "schema_version": SCHEMA_VERSION,
            "suite_name": config.suite_name,
            "status": "capture-error",
            "exit_code": 2,
            "capture_errors": [error_message],
            "known_secret_values_are_sanitized": True,
        },
    )
    _write_artifact_manifest(staging_dir)


def capture_runtime_evidence(
    config: CaptureConfig,
    *,
    runner: Runner = subprocess.run,
    environ: Mapping[str, str] | None = None,
    distributions: Iterable[tuple[str, str]] | None = None,
) -> int:
    normalized = _normalized_config(config)
    inherited_environment = environ
    if inherited_environment is None:
        inherited_environment = os.environ
    environment = {
        str(name): str(value) for name, value in inherited_environment.items()
    }
    secret_values = _known_secret_values(environment, normalized.pytest_args)

    normalized.output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_path = tempfile.mkdtemp(
        prefix=f".{normalized.output_dir.name}.tmp-",
        dir=normalized.output_dir.parent,
    )
    staging_dir = Path(staging_path)

    result_exit_code = 2
    try:
        result_exit_code = _capture_into_staging(
            config=normalized,
            staging_dir=staging_dir,
            runner=runner,
            environ=environment,
            distributions=distributions,
        )
    except Exception as exc:  # noqa: BLE001 - preserve unexpected capture evidence.
        try:
            _write_unexpected_failure(
                staging_dir=staging_dir,
                config=normalized,
                exc=exc,
                secret_values=secret_values,
            )
        except Exception as write_exc:
            raise CaptureError(
                "evidence capture failed and the staging result could not be "
                f"completed: {staging_dir}: {type(write_exc).__name__}"
            ) from write_exc

    _publish_staging_directory(
        staging_dir=staging_dir,
        output_dir=normalized.output_dir,
    )
    return result_exit_code


def _publish_staging_directory(
    *,
    staging_dir: Path,
    output_dir: Path,
) -> None:
    lock_path = output_dir.with_name(f".{output_dir.name}.publish.lock")
    try:
        lock_fd = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError as exc:
        raise CaptureError(
            "evidence publication lock already exists; staging evidence retained at "
            f"{staging_dir}"
        ) from exc
    try:
        os.write(lock_fd, f"{staging_dir}\n".encode())
        os.fsync(lock_fd)
        if output_dir.exists():
            raise CaptureError(
                "output directory appeared during capture; staging evidence retained at "
                f"{staging_dir}"
            )
        os.replace(staging_dir, output_dir)
    finally:
        os.close(lock_fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _parse_args(argv: Sequence[str] | None) -> CaptureConfig:
    parser = argparse.ArgumentParser(
        description=(
            "Capture deterministic, sanitized pytest evidence in the current "
            "Python environment."
        )
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--suite-name", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="pytest paths and arguments after --",
    )
    args = parser.parse_args(argv)
    pytest_args = list(args.pytest_args)
    if pytest_args and pytest_args[0] == "--":
        pytest_args = pytest_args[1:]
    return CaptureConfig(
        output_dir=args.output_dir,
        suite_name=args.suite_name,
        pytest_args=tuple(pytest_args),
        repo_root=args.repo_root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = _parse_args(argv)
        exit_code = capture_runtime_evidence(config)
    except (CaptureError, OSError) as exc:
        safe_message = _sanitize_text(
            str(exc),
            _secret_argument_values(tuple(argv or sys.argv[1:])),
        )
        print(f"FATAL: {safe_message}", file=sys.stderr)
        return 2

    print(
        f"WROTE {config.output_dir.expanduser().resolve()} "
        f"suite={config.suite_name} exit_code={exit_code}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
