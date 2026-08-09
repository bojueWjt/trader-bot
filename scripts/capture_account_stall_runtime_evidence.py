#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import sysconfig
import time
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
OWNED_PYTEST_INI_OPTIONS = frozenset(
    {
        "addopts",
        "filterwarnings",
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


@dataclass(frozen=True)
class DistributionContext:
    distribution: object
    canonical_name: str
    version: str
    package_files: tuple[object, ...]
    metadata_dir: Path
    record_file: object
    allowed_roots: tuple[Path, ...]


Runner = Callable[..., subprocess.CompletedProcess]


def _json_text(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _write_json(path: Path, value: object) -> None:
    _write_bytes(path, _json_text(value).encode("utf-8"))


def _write_text(path: Path, value: str) -> None:
    _write_bytes(path, value.encode("utf-8"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _regular_open_flags(flags: int) -> int:
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    return flags


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _raise_unsafe_path(path: Path, exc: OSError) -> None:
    raise CaptureError(
        f"evidence path contains a symlink or invalid component: {path}"
    ) from exc


def _open_directory_path_nofollow(
    path: Path,
    *,
    create: bool = False,
    create_mode: int = 0o755,
) -> int:
    absolute = _absolute_path(path)
    directory_fd = os.open("/", _directory_open_flags())
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(
                        component,
                        mode=create_mode,
                        dir_fd=directory_fd,
                    )
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(
                        component,
                        _directory_open_flags(),
                        dir_fd=directory_fd,
                    )
                except OSError as exc:
                    _raise_unsafe_path(absolute, exc)
            except OSError as exc:
                _raise_unsafe_path(absolute, exc)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _open_regular_path_nofollow(path: Path, flags: int) -> int:
    absolute = _absolute_path(path)
    parent_fd = _open_directory_path_nofollow(absolute.parent)
    try:
        try:
            return os.open(
                absolute.name,
                _regular_open_flags(flags),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            _raise_unsafe_path(absolute, exc)
    finally:
        os.close(parent_fd)


def _write_bytes_at(
    directory_fd: int,
    relative_path: str,
    value: bytes,
    *,
    display_path: Path,
) -> None:
    if not relative_path or "/" in relative_path or relative_path in {".", ".."}:
        raise CaptureError(f"invalid evidence file name: {relative_path}")
    flags = _regular_open_flags(os.O_WRONLY)
    try:
        fd = os.open(
            relative_path,
            flags,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        try:
            fd = os.open(
                relative_path,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            _raise_unsafe_path(display_path, exc)
    except OSError as exc:
        _raise_unsafe_path(display_path, exc)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise CaptureError(
                f"evidence path is not a regular file: {display_path}"
            )
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(value)
            handle.flush()
    finally:
        os.close(fd)


def _write_bytes(path: Path, value: bytes) -> None:
    absolute = _absolute_path(path)
    parent_fd = _open_directory_path_nofollow(absolute.parent)
    try:
        _write_bytes_at(
            parent_fd,
            absolute.name,
            value,
            display_path=absolute,
        )
    finally:
        os.close(parent_fd)


def _write_text_at(
    directory_fd: int,
    relative_path: str,
    value: str,
    *,
    display_path: Path,
) -> None:
    _write_bytes_at(
        directory_fd,
        relative_path,
        value.encode("utf-8"),
        display_path=display_path,
    )


def _write_json_at(
    directory_fd: int,
    relative_path: str,
    value: object,
    *,
    display_path: Path,
) -> None:
    _write_bytes_at(
        directory_fd,
        relative_path,
        _json_text(value).encode("utf-8"),
        display_path=display_path,
    )


def _same_file_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _regular_file_digest(path: Path) -> tuple[str, int]:
    fd = _open_regular_path_nofollow(path, os.O_RDONLY)
    return _regular_file_digest_from_fd(fd, path)


def _regular_file_digest_from_fd(
    fd: int,
    display_path: Path,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    try:
        before_stat = os.fstat(fd)
        if not stat.S_ISREG(before_stat.st_mode):
            raise CaptureError(
                f"evidence path is not a regular file: {display_path}"
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            handle.seek(0)
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after_stat = os.fstat(fd)
        if not _same_file_snapshot(before_stat, after_stat):
            raise CaptureError(
                f"evidence file changed while being read: {display_path}"
            )
        return digest.hexdigest(), int(after_stat.st_size)
    finally:
        os.close(fd)


def _sha256_path(path: Path) -> str:
    digest, _size = _regular_file_digest(path)
    return digest


def _regular_file_bytes_at(
    directory_fd: int,
    relative_path: str,
    *,
    display_path: Path,
) -> bytes:
    try:
        fd = os.open(
            relative_path,
            _regular_open_flags(os.O_RDONLY),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        _raise_unsafe_path(display_path, exc)
    try:
        before_stat = os.fstat(fd)
        if not stat.S_ISREG(before_stat.st_mode):
            raise CaptureError(
                f"evidence path is not a regular file: {display_path}"
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            value = handle.read()
        after_stat = os.fstat(fd)
        if not _same_file_snapshot(before_stat, after_stat):
            raise CaptureError(
                f"evidence file changed while being read: {display_path}"
            )
        return value
    finally:
        os.close(fd)


def _regular_file_bytes(path: Path) -> bytes:
    absolute = _absolute_path(path)
    parent_fd = _open_directory_path_nofollow(absolute.parent)
    try:
        return _regular_file_bytes_at(
            parent_fd,
            absolute.name,
            display_path=absolute,
        )
    finally:
        os.close(parent_fd)


def _directory_identity_from_fd(
    directory_fd: int,
    *,
    display_path: Path,
) -> tuple[int, int]:
    directory_stat = os.fstat(directory_fd)
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise CaptureError(f"evidence path is not a directory: {display_path}")
    return int(directory_stat.st_dev), int(directory_stat.st_ino)


def _directory_identity(path: Path) -> tuple[int, int]:
    directory_fd = _open_directory_path_nofollow(path)
    try:
        return _directory_identity_from_fd(
            directory_fd,
            display_path=path,
        )
    finally:
        os.close(directory_fd)


def _path_is_within(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _distribution_record_file(
    distribution: object,
    package_files: Sequence[object],
    *,
    metadata_dir: Path,
    canonical_name: str,
    version: str,
) -> object:
    candidates = []
    exact_candidates = []
    for package_file in package_files:
        relative = Path(str(package_file))
        if relative.is_absolute():
            continue
        if relative.name != "RECORD":
            continue
        if not relative.parent.name.endswith(".dist-info"):
            continue
        candidates.append(package_file)
        located = _absolute_path(Path(distribution.locate_file(package_file)))
        try:
            located_parent = located.parent.resolve(strict=True)
        except OSError:
            continue
        if located_parent == metadata_dir:
            exact_candidates.append(package_file)
    if len(exact_candidates) == 1:
        return exact_candidates[0]
    if candidates:
        raise CaptureError(
            "distribution lacks exact RECORD binding: "
            f"{canonical_name}=={version}"
        )
    raise CaptureError(
        f"distribution lacks RECORD inventory: {canonical_name}=={version}"
    )


def _distribution_metadata_directory(
    distribution: object,
    *,
    canonical_name: str,
    version: str,
) -> Path:
    metadata_path = getattr(distribution, "_path", False)
    if metadata_path is False:
        raise CaptureError(
            "distribution metadata directory is unavailable: "
            f"{canonical_name}=={version}"
        )
    try:
        metadata_dir = Path(metadata_path).resolve(strict=True)
    except OSError as exc:
        raise CaptureError(
            "distribution metadata directory is unavailable: "
            f"{canonical_name}=={version}"
        ) from exc
    if not metadata_dir.name.endswith(".dist-info"):
        raise CaptureError(
            "distribution metadata directory is not dist-info: "
            f"{canonical_name}=={version}: {metadata_dir}"
        )
    if not metadata_dir.is_dir():
        raise CaptureError(
            "distribution metadata directory is unavailable: "
            f"{canonical_name}=={version}"
        )
    return metadata_dir


def _distribution_allowed_roots(metadata_dir: Path) -> tuple[Path, ...]:
    install_library_root = metadata_dir.parent.resolve(strict=True)
    roots = {install_library_root}
    for scheme in sysconfig.get_scheme_names():
        try:
            configured_paths = sysconfig.get_paths(scheme=scheme)
        except (KeyError, TypeError, ValueError):
            continue
        library_roots = set()
        for key in ("platlib", "purelib"):
            configured = configured_paths.get(key)
            if not configured:
                continue
            try:
                library_roots.add(Path(configured).resolve(strict=True))
            except OSError:
                continue
        if install_library_root not in library_roots:
            continue
        for key in (
            "data",
            "include",
            "platinclude",
            "platlib",
            "purelib",
            "scripts",
        ):
            configured = configured_paths.get(key)
            if not configured:
                continue
            try:
                roots.add(Path(configured).resolve(strict=True))
            except OSError:
                continue
    return tuple(sorted(roots, key=lambda item: os.fsencode(item)))


def _safe_distribution_file(
    distribution: object,
    package_file: object,
    *,
    allowed_roots: Sequence[Path],
    metadata_dir: Path,
    canonical_name: str,
    version: str,
) -> Path:
    relative = Path(str(package_file))
    if relative.is_absolute():
        raise CaptureError(
            "distribution contains unsafe installed file path: "
            f"{canonical_name}=={version}: {relative.as_posix()}"
        )
    located = Path(distribution.locate_file(package_file))
    try:
        resolved = located.resolve(strict=True)
    except OSError as exc:
        raise CaptureError(
            "distribution installed file is unavailable: "
            f"{canonical_name}=={version}: {relative.as_posix()}"
        ) from exc
    if not _path_is_within(resolved, allowed_roots):
        raise CaptureError(
            "distribution contains unsafe installed file path: "
            f"{canonical_name}=={version}: {relative.as_posix()}"
        )
    for parent in resolved.parents:
        if not parent.name.endswith(".dist-info"):
            continue
        if parent != metadata_dir:
            raise CaptureError(
                "distribution contains cross-distribution metadata path: "
                f"{canonical_name}=={version}: {relative.as_posix()}"
            )
    return resolved


def _distribution_file_record(path: Path, relative_path: str) -> dict[str, object]:
    try:
        digest, size = _regular_file_digest(path)
    except OSError as exc:
        raise CaptureError(
            f"distribution installed file is unavailable: {relative_path}"
        ) from exc
    return {
        "path": relative_path,
        "sha256": digest,
        "size": size,
    }


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


def _distribution_contexts(
    distributions: Sequence[object],
) -> tuple[DistributionContext, ...]:
    contexts = []
    for distribution in distributions:
        name = distribution.metadata.get("Name")
        if not name:
            name = "unknown-distribution"
        canonical_name = _normalize_distribution_name(str(name))
        version = str(distribution.version).strip()
        package_files = tuple(distribution.files or ())
        if not package_files:
            raise CaptureError(
                f"distribution lacks installed file inventory: "
                f"{canonical_name}=={version}"
            )
        metadata_dir = _distribution_metadata_directory(
            distribution,
            canonical_name=canonical_name,
            version=version,
        )
        record_file = _distribution_record_file(
            distribution,
            package_files,
            metadata_dir=metadata_dir,
            canonical_name=canonical_name,
            version=version,
        )
        contexts.append(
            DistributionContext(
                distribution=distribution,
                canonical_name=canonical_name,
                version=version,
                package_files=package_files,
                metadata_dir=metadata_dir,
                record_file=record_file,
                allowed_roots=_distribution_allowed_roots(metadata_dir),
            )
        )
    return tuple(contexts)


def _distribution_resolved_files(
    contexts: Sequence[DistributionContext],
) -> tuple[tuple[tuple[object, Path, Path], ...], ...]:
    resolved_by_distribution = []
    owners: dict[Path, list[tuple[int, str]]] = {}
    for context_index, context in enumerate(contexts):
        resolved_files = []
        seen_relative_paths = set()
        seen_resolved_paths = set()
        for package_file in context.package_files:
            package_relative_path = Path(str(package_file))
            relative_path = package_relative_path.as_posix()
            if relative_path in seen_relative_paths:
                raise CaptureError(
                    "distribution contains duplicate installed file path: "
                    f"{context.canonical_name}=={context.version}: {relative_path}"
                )
            seen_relative_paths.add(relative_path)
            path = _safe_distribution_file(
                context.distribution,
                package_file,
                allowed_roots=context.allowed_roots,
                metadata_dir=context.metadata_dir,
                canonical_name=context.canonical_name,
                version=context.version,
            )
            if path in seen_resolved_paths:
                raise CaptureError(
                    "distribution contains duplicate installed file identity: "
                    f"{context.canonical_name}=={context.version}: {relative_path}"
                )
            seen_resolved_paths.add(path)
            resolved_files.append(
                (
                    package_file,
                    package_relative_path,
                    path,
                )
            )
            owners.setdefault(path, []).append((context_index, relative_path))
        resolved_by_distribution.append(tuple(resolved_files))

    for path, path_owners in owners.items():
        distribution_indexes = {item[0] for item in path_owners}
        if len(distribution_indexes) < 2:
            continue
        owner_names = sorted(
            {
                (
                    f"{contexts[index].canonical_name}=="
                    f"{contexts[index].version}"
                )
                for index in distribution_indexes
            }
        )
        raise CaptureError(
            "cross-distribution installed file identity: "
            f"{path}: {', '.join(owner_names)}"
        )
    return tuple(resolved_by_distribution)


def _distribution_artifact_records() -> list[dict[str, object]]:
    contexts = _distribution_contexts(tuple(metadata.distributions()))
    resolved_by_distribution = _distribution_resolved_files(contexts)
    records = []
    for context, resolved_files in zip(
        contexts,
        resolved_by_distribution,
        strict=True,
    ):
        record_relative_path = Path(str(context.record_file))
        metadata_relative_dir = record_relative_path.parent
        evidence_files = []
        installed_files = []
        editable = False
        for _package_file, package_relative_path, path in resolved_files:
            relative_path = package_relative_path.as_posix()
            installed_record = _distribution_file_record(path, relative_path)
            installed_files.append(installed_record)
            evidence_name = package_relative_path.name
            if package_relative_path.parent != metadata_relative_dir:
                continue
            if evidence_name not in DIST_INFO_EVIDENCE_FILES:
                continue
            if evidence_name == "direct_url.json":
                direct_url_bytes = _regular_file_bytes(path)
                if (
                    _sha256_bytes(direct_url_bytes)
                    != installed_record["sha256"]
                ):
                    raise CaptureError(
                        "distribution installed file changed while "
                        f"being recorded: {relative_path}"
                    )
                try:
                    direct_url = json.loads(
                        direct_url_bytes.decode("utf-8")
                    )
                except (TypeError, UnicodeError, ValueError):
                    direct_url = {}
                dir_info = direct_url.get("dir_info")
                if isinstance(dir_info, dict):
                    editable = bool(dir_info.get("editable"))
            evidence_files.append(
                {
                    "name": evidence_name,
                    "sha256": installed_record["sha256"],
                    "size": installed_record["size"],
                }
            )
        evidence_files.sort(key=lambda item: str(item["name"]).encode("utf-8"))
        installed_files.sort(key=lambda item: str(item["path"]).encode("utf-8"))
        records.append(
            {
                "name": context.canonical_name,
                "version": context.version,
                "editable": editable,
                "metadata_files": evidence_files,
                "installed_files": installed_files,
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
    return _junit_report_text(
        path.read_text(encoding="utf-8", errors="replace"),
        repo_root,
    )


def _junit_report_text(content: str, repo_root: Path) -> dict[str, object]:
    root = ET.fromstring(content)
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
        override = _pytest_ini_override(previous, argument)
        if override in OWNED_PYTEST_INI_OPTIONS:
            raise CaptureError(
                f"pytest ini option is managed by the evidence collector: {override}"
            )
        previous = argument


def _pytest_ini_override(previous: str, argument: str) -> str:
    value = ""
    if previous in {"-o", "--override-ini"}:
        value = argument
    elif argument.startswith("--override-ini="):
        value = argument.split("=", 1)[1]
    elif argument.startswith("-o") and argument != "-o":
        value = argument[2:]
    if not value:
        return ""
    return value.split("=", 1)[0].strip()


def _managed_pytest_ini_args() -> tuple[str, ...]:
    return (
        "-o",
        "addopts=",
        "-o",
        "filterwarnings=default",
    )


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
    output_dir = Path(os.path.abspath(config.output_dir.expanduser()))
    repo_root = config.repo_root.expanduser().resolve()
    suite_name = config.suite_name.strip()
    pytest_args = tuple(str(value) for value in config.pytest_args)

    if not suite_name:
        raise CaptureError("suite name is empty")
    if re.search(r"[\x00-\x1f\x7f]", suite_name) is not None:
        raise CaptureError("suite name contains control characters")
    if not repo_root.is_dir():
        raise CaptureError(f"repository root is not a directory: {repo_root}")
    if os.path.lexists(output_dir):
        raise CaptureError(f"output directory already exists: {output_dir}")
    _validate_pytest_args(pytest_args)

    return CaptureConfig(
        output_dir=output_dir,
        suite_name=suite_name,
        pytest_args=pytest_args,
        repo_root=repo_root,
    )


def _placeholder_junit_bytes(suite_name: str) -> bytes:
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
    return (
        ET.tostring(
            testsuites,
            encoding="utf-8",
            xml_declaration=True,
        )
        + b"\n"
    )


def _write_placeholder_junit(path: Path, suite_name: str) -> None:
    _write_bytes(path, _placeholder_junit_bytes(suite_name))


def _entry_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return int(file_stat.st_dev), int(file_stat.st_ino)


def _entry_stat_at(
    directory_fd: int,
    name: str,
    *,
    display_path: Path,
) -> os.stat_result:
    try:
        return os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        _raise_unsafe_path(display_path, exc)


def _directory_entry_identity(
    directory_fd: int,
    name: str,
    *,
    display_path: Path,
) -> tuple[int, int]:
    entry_stat = _entry_stat_at(
        directory_fd,
        name,
        display_path=display_path,
    )
    if stat.S_ISLNK(entry_stat.st_mode):
        raise CaptureError(
            f"evidence path contains a symlink or invalid component: {display_path}"
        )
    if not stat.S_ISDIR(entry_stat.st_mode):
        raise CaptureError(f"evidence path is not a directory: {display_path}")
    return _entry_identity(entry_stat)


def _verify_directory_path_identity(
    path: Path,
    directory_fd: int,
    *,
    description: str,
) -> None:
    try:
        path_fd = _open_directory_path_nofollow(path)
    except OSError as exc:
        raise CaptureError(f"{description} is unavailable: {path}") from exc
    try:
        expected_identity = _directory_identity_from_fd(
            directory_fd,
            display_path=path,
        )
        path_identity = _directory_identity_from_fd(
            path_fd,
            display_path=path,
        )
    finally:
        os.close(path_fd)
    if path_identity != expected_identity:
        raise CaptureError(f"{description} identity changed: {path}")


def _open_directory_entry(
    directory_fd: int,
    name: str,
    *,
    expected_stat: os.stat_result,
    display_path: Path,
) -> int:
    try:
        child_fd = os.open(
            name,
            _directory_open_flags(),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        _raise_unsafe_path(display_path, exc)
    child_stat = os.fstat(child_fd)
    if _entry_identity(child_stat) != _entry_identity(expected_stat):
        os.close(child_fd)
        raise CaptureError(
            f"evidence directory identity changed: {display_path}"
        )
    return child_fd


def _open_regular_entry(
    directory_fd: int,
    name: str,
    *,
    flags: int,
    expected_stat: os.stat_result,
    display_path: Path,
) -> int:
    try:
        file_fd = os.open(
            name,
            _regular_open_flags(flags),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        _raise_unsafe_path(display_path, exc)
    file_stat = os.fstat(file_fd)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(file_fd)
        raise CaptureError(
            f"evidence path is not a regular file: {display_path}"
        )
    if _entry_identity(file_stat) != _entry_identity(expected_stat):
        os.close(file_fd)
        raise CaptureError(
            f"evidence file identity changed: {display_path}"
        )
    return file_fd


def _payload_entries(directory_fd: int) -> list[os.DirEntry]:
    with os.scandir(directory_fd) as iterator:
        return sorted(
            iterator,
            key=lambda item: os.fsencode(item.name),
        )


def _regular_file_exists_at(
    directory_fd: int,
    relative_path: str,
    *,
    display_path: Path,
) -> bool:
    try:
        file_stat = os.stat(
            relative_path,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(file_stat.st_mode):
        raise CaptureError(
            f"evidence payload contains a symlink: {relative_path}"
        )
    if not stat.S_ISREG(file_stat.st_mode):
        raise CaptureError(
            f"evidence path is not a regular file: {display_path}"
        )
    return True


def _sanitize_regular_file_at(
    directory_fd: int,
    relative_path: str,
    *,
    display_path: Path,
    secret_values: Sequence[str],
) -> str:
    entry_stat = _entry_stat_at(
        directory_fd,
        relative_path,
        display_path=display_path,
    )
    if stat.S_ISLNK(entry_stat.st_mode):
        raise CaptureError(
            f"evidence payload contains a symlink: {relative_path}"
        )
    if not stat.S_ISREG(entry_stat.st_mode):
        raise CaptureError(
            f"evidence path is not a regular file: {display_path}"
        )
    file_fd = _open_regular_entry(
        directory_fd,
        relative_path,
        flags=os.O_RDWR,
        expected_stat=entry_stat,
        display_path=display_path,
    )
    return _sanitize_file_from_fd(
        file_fd,
        display_path,
        secret_values,
    )


def _payload_artifact_records_from_fd(
    directory_fd: int,
    *,
    display_dir: Path,
    relative_dir: str = "",
) -> list[dict[str, object]]:
    artifacts = []
    for entry in _payload_entries(directory_fd):
        relative_path = entry.name
        if relative_dir:
            relative_path = f"{relative_dir}/{entry.name}"
        display_path = display_dir / relative_path
        entry_stat = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise CaptureError(
                f"evidence payload contains a symlink: {relative_path}"
            )
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = _open_directory_entry(
                directory_fd,
                entry.name,
                expected_stat=entry_stat,
                display_path=display_path,
            )
            try:
                child_artifacts = _payload_artifact_records_from_fd(
                    child_fd,
                    display_dir=display_dir,
                    relative_dir=relative_path,
                )
            finally:
                os.close(child_fd)
            if not child_artifacts:
                raise CaptureError(
                    "evidence payload contains an empty directory: "
                    f"{relative_path}"
                )
            artifacts.extend(child_artifacts)
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            raise CaptureError(
                "evidence payload contains an unsupported file type: "
                f"{relative_path}"
            )
        if relative_path == "artifact-manifest.json":
            continue
        file_fd = _open_regular_entry(
            directory_fd,
            entry.name,
            flags=os.O_RDONLY,
            expected_stat=entry_stat,
            display_path=display_path,
        )
        digest, size = _regular_file_digest_from_fd(file_fd, display_path)
        artifacts.append(
            {
                "path": relative_path,
                "sha256": digest,
                "size": size,
            }
        )
    return artifacts


def _artifact_manifest_from_fd(
    directory_fd: int,
    *,
    display_dir: Path,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm": "sha256",
        "manifest_excludes": ["artifact-manifest.json"],
        "artifacts": _payload_artifact_records_from_fd(
            directory_fd,
            display_dir=display_dir,
        ),
    }


def _sanitize_file_from_fd(
    fd: int,
    display_path: Path,
    secret_values: Sequence[str],
) -> str:
    try:
        before_stat = os.fstat(fd)
        if not stat.S_ISREG(before_stat.st_mode):
            raise CaptureError(
                f"evidence path is not a regular file: {display_path}"
            )
        with os.fdopen(
            fd,
            "r+",
            encoding="utf-8",
            errors="replace",
            closefd=False,
        ) as handle:
            content = handle.read()
            before_write_stat = os.fstat(fd)
            if not _same_file_snapshot(before_stat, before_write_stat):
                raise CaptureError(
                    f"evidence file changed while being read: {display_path}"
                )
            sanitized = _sanitize_text(content, secret_values)
            handle.seek(0)
            handle.write(sanitized)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        return content
    finally:
        os.close(fd)


def _sanitize_file(path: Path, secret_values: Sequence[str]) -> None:
    fd = _open_regular_path_nofollow(path, os.O_RDWR)
    _sanitize_file_from_fd(fd, path, secret_values)


def _sanitize_staging_files_from_fd(
    directory_fd: int,
    *,
    display_dir: Path,
    secret_values: Sequence[str],
    relative_dir: str = "",
) -> None:
    for entry in _payload_entries(directory_fd):
        relative_path = entry.name
        if relative_dir:
            relative_path = f"{relative_dir}/{entry.name}"
        display_path = display_dir / relative_path
        entry_stat = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise CaptureError(
                f"evidence payload contains a symlink: {relative_path}"
            )
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = _open_directory_entry(
                directory_fd,
                entry.name,
                expected_stat=entry_stat,
                display_path=display_path,
            )
            try:
                _sanitize_staging_files_from_fd(
                    child_fd,
                    display_dir=display_dir,
                    secret_values=secret_values,
                    relative_dir=relative_path,
                )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            raise CaptureError(
                "evidence payload contains an unsupported file type: "
                f"{relative_path}"
            )
        file_fd = _open_regular_entry(
            directory_fd,
            entry.name,
            flags=os.O_RDWR,
            expected_stat=entry_stat,
            display_path=display_path,
        )
        _sanitize_file_from_fd(file_fd, display_path, secret_values)


def _sanitize_staging_files(
    staging_dir: Path,
    secret_values: Sequence[str],
) -> None:
    directory_fd = _open_directory_path_nofollow(staging_dir)
    try:
        _sanitize_staging_files_from_fd(
            directory_fd,
            display_dir=staging_dir,
            secret_values=secret_values,
        )
    finally:
        os.close(directory_fd)


def _artifact_manifest(staging_dir: Path) -> dict:
    directory_fd = _open_directory_path_nofollow(staging_dir)
    try:
        return _artifact_manifest_from_fd(
            directory_fd,
            display_dir=staging_dir,
        )
    finally:
        os.close(directory_fd)


def _write_artifact_manifest(
    staging_dir: Path,
    *,
    directory_fd: int | None = None,
) -> None:
    owned_fd = False
    active_fd = directory_fd
    if active_fd is None:
        active_fd = _open_directory_path_nofollow(staging_dir)
        owned_fd = True
    try:
        manifest = _artifact_manifest_from_fd(
            active_fd,
            display_dir=staging_dir,
        )
        _write_json_at(
            active_fd,
            "artifact-manifest.json",
            manifest,
            display_path=staging_dir / "artifact-manifest.json",
        )
    finally:
        if owned_fd is True:
            os.close(active_fd)


def _validated_manifest_bytes_from_fd(
    directory_fd: int,
    *,
    display_dir: Path,
) -> bytes:
    manifest_path = display_dir / "artifact-manifest.json"
    manifest_bytes = _regular_file_bytes_at(
        directory_fd,
        "artifact-manifest.json",
        display_path=manifest_path,
    )
    try:
        stored_manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (TypeError, UnicodeError, ValueError) as exc:
        raise CaptureError(
            f"evidence artifact manifest is malformed: {manifest_path}"
        ) from exc
    expected_manifest = _artifact_manifest_from_fd(
        directory_fd,
        display_dir=display_dir,
    )
    expected_bytes = _json_text(expected_manifest).encode("utf-8")
    if stored_manifest != expected_manifest or manifest_bytes != expected_bytes:
        raise CaptureError(
            "evidence artifact manifest does not match payload: "
            f"{manifest_path}"
        )
    return manifest_bytes


def _fsync_payload_tree_from_fd(
    directory_fd: int,
    *,
    display_dir: Path,
    relative_dir: str = "",
) -> None:
    for entry in _payload_entries(directory_fd):
        relative_path = entry.name
        if relative_dir:
            relative_path = f"{relative_dir}/{entry.name}"
        display_path = display_dir / relative_path
        entry_stat = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise CaptureError(
                f"evidence payload contains a symlink: {relative_path}"
            )
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = _open_directory_entry(
                directory_fd,
                entry.name,
                expected_stat=entry_stat,
                display_path=display_path,
            )
            try:
                _fsync_payload_tree_from_fd(
                    child_fd,
                    display_dir=display_dir,
                    relative_dir=relative_path,
                )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            raise CaptureError(
                "evidence payload contains an unsupported file type: "
                f"{relative_path}"
            )
        file_fd = _open_regular_entry(
            directory_fd,
            entry.name,
            flags=os.O_RDONLY,
            expected_stat=entry_stat,
            display_path=display_path,
        )
        try:
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
    os.fsync(directory_fd)


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
    staging_fd: int,
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
        *_managed_pytest_ini_args(),
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
    nodeids_hash = _sha256_bytes(sanitized_nodeids_content)
    junit_path = staging_dir / "junit.xml"
    pytest_command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        *_managed_pytest_ini_args(),
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

    junit_generated_by_pytest = _regular_file_exists_at(
        staging_fd,
        "junit.xml",
        display_path=junit_path,
    )
    if not junit_generated_by_pytest:
        _write_bytes_at(
            staging_fd,
            "junit.xml",
            _placeholder_junit_bytes(config.suite_name),
            display_path=junit_path,
        )
    junit_content = _sanitize_regular_file_at(
        staging_fd,
        "junit.xml",
        display_path=junit_path,
        secret_values=secret_values,
    )
    junit_report = _junit_report_text(junit_content, config.repo_root)
    executed_nodeids = [str(nodeid) for nodeid in junit_report["nodeids"]]
    executed_nodeids_content = _nodeids_text_bytes(executed_nodeids)
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
    sanitized_executed_nodeids = _sanitize_text(
        executed_nodeids_content.decode("utf-8"),
        secret_values,
    ).encode("utf-8")
    executed_nodeids_hash = _sha256_bytes(sanitized_executed_nodeids)
    collected_nodeid_count = len(collected_nodeids)
    junit_test_count = int(junit_report["test_count"])

    _write_text_at(
        staging_fd,
        "git-head.txt",
        _sanitize_text(git_head.stdout, secret_values),
        display_path=staging_dir / "git-head.txt",
    )
    _write_text_at(
        staging_fd,
        "git-head.stderr.txt",
        _sanitize_text(git_head.stderr, secret_values),
        display_path=staging_dir / "git-head.stderr.txt",
    )
    _write_text_at(
        staging_fd,
        "git-head.exit-code.txt",
        f"{git_head.exit_code}\n",
        display_path=staging_dir / "git-head.exit-code.txt",
    )
    _write_text_at(
        staging_fd,
        "git-status.txt",
        _sanitize_text(git_status.stdout, secret_values),
        display_path=staging_dir / "git-status.txt",
    )
    _write_text_at(
        staging_fd,
        "git-status.stderr.txt",
        _sanitize_text(git_status.stderr, secret_values),
        display_path=staging_dir / "git-status.stderr.txt",
    )
    _write_text_at(
        staging_fd,
        "git-status.exit-code.txt",
        f"{git_status.exit_code}\n",
        display_path=staging_dir / "git-status.exit-code.txt",
    )
    _write_json_at(
        staging_fd,
        "python-platform.json",
        python_platform,
        display_path=staging_dir / "python-platform.json",
    )
    _write_json_at(
        staging_fd,
        "environment.json",
        environment_manifest,
        display_path=staging_dir / "environment.json",
    )
    _write_bytes_at(
        staging_fd,
        "distributions.txt",
        distributions_content,
        display_path=staging_dir / "distributions.txt",
    )
    _write_text_at(
        staging_fd,
        "distributions.sha256.txt",
        f"{distributions_hash}\n",
        display_path=staging_dir / "distributions.sha256.txt",
    )
    _write_bytes_at(
        staging_fd,
        "distribution-artifacts.json",
        distribution_artifact_content,
        display_path=staging_dir / "distribution-artifacts.json",
    )
    _write_text_at(
        staging_fd,
        "distribution-artifacts.sha256.txt",
        f"{distribution_artifact_hash}\n",
        display_path=staging_dir / "distribution-artifacts.sha256.txt",
    )
    _write_text_at(
        staging_fd,
        "collect.stdout.txt",
        sanitized_collect_stdout,
        display_path=staging_dir / "collect.stdout.txt",
    )
    _write_text_at(
        staging_fd,
        "collect.stderr.txt",
        _sanitize_text(collect.stderr, secret_values),
        display_path=staging_dir / "collect.stderr.txt",
    )
    _write_text_at(
        staging_fd,
        "collect.exit-code.txt",
        f"{collect.exit_code}\n",
        display_path=staging_dir / "collect.exit-code.txt",
    )
    _write_bytes_at(
        staging_fd,
        "nodeids.txt",
        sanitized_nodeids_content,
        display_path=staging_dir / "nodeids.txt",
    )
    _write_text_at(
        staging_fd,
        "nodeids.sha256.txt",
        f"{nodeids_hash}\n",
        display_path=staging_dir / "nodeids.sha256.txt",
    )
    _write_bytes_at(
        staging_fd,
        "executed-nodeids.txt",
        sanitized_executed_nodeids,
        display_path=staging_dir / "executed-nodeids.txt",
    )
    _write_text_at(
        staging_fd,
        "executed-nodeids.sha256.txt",
        f"{executed_nodeids_hash}\n",
        display_path=staging_dir / "executed-nodeids.sha256.txt",
    )
    _write_text_at(
        staging_fd,
        "pytest.stdout.txt",
        _sanitize_text(pytest_run.stdout, secret_values),
        display_path=staging_dir / "pytest.stdout.txt",
    )
    _write_text_at(
        staging_fd,
        "pytest.stderr.txt",
        _sanitize_text(pytest_run.stderr, secret_values),
        display_path=staging_dir / "pytest.stderr.txt",
    )
    _write_text_at(
        staging_fd,
        "pytest.exit-code.txt",
        f"{pytest_run.exit_code}\n",
        display_path=staging_dir / "pytest.exit-code.txt",
    )
    _write_json_at(
        staging_fd,
        "test-report.json",
        _sanitized_test_report(
            junit_report=junit_report,
            warning_report=warning_report,
            secret_values=secret_values,
        ),
        display_path=staging_dir / "test-report.json",
    )

    path_replacements = {
        str(staging_dir): str(config.output_dir),
    }
    _write_json_at(
        staging_fd,
        "commands.json",
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
        display_path=staging_dir / "commands.json",
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
    _write_json_at(
        staging_fd,
        "result.json",
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
        display_path=staging_dir / "result.json",
    )
    _write_artifact_manifest(
        staging_dir,
        directory_fd=staging_fd,
    )
    return result_exit_code


def _write_unexpected_failure(
    *,
    staging_dir: Path,
    staging_fd: int,
    config: CaptureConfig,
    exc: Exception,
    secret_values: Sequence[str],
) -> None:
    _sanitize_staging_files_from_fd(
        staging_fd,
        display_dir=staging_dir,
        secret_values=secret_values,
    )
    error_message = _sanitize_text(
        f"{type(exc).__name__}: {exc}",
        secret_values,
    )
    _write_json_at(
        staging_fd,
        "capture-error.json",
        {
            "schema_version": SCHEMA_VERSION,
            "suite_name": config.suite_name,
            "error": error_message,
        },
        display_path=staging_dir / "capture-error.json",
    )
    _write_json_at(
        staging_fd,
        "result.json",
        {
            "schema_version": SCHEMA_VERSION,
            "suite_name": config.suite_name,
            "status": "capture-error",
            "exit_code": 2,
            "capture_errors": [error_message],
            "known_secret_values_are_sanitized": True,
        },
        display_path=staging_dir / "result.json",
    )
    _write_artifact_manifest(
        staging_dir,
        directory_fd=staging_fd,
    )


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

    output_parent = normalized.output_dir.parent
    parent_fd = _open_directory_path_nofollow(
        output_parent,
        create=True,
    )
    staging_dir = normalized.output_dir.with_name(
        f".{normalized.output_dir.name}.payload"
    )
    staging_fd: int | None = None
    try:
        _verify_directory_path_identity(
            output_parent,
            parent_fd,
            description="evidence output parent",
        )
        try:
            os.stat(
                normalized.output_dir.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            _raise_unsafe_path(normalized.output_dir, exc)
        else:
            raise CaptureError(
                f"output directory already exists: {normalized.output_dir}"
            )

        try:
            os.mkdir(
                staging_dir.name,
                mode=0o700,
                dir_fd=parent_fd,
            )
        except FileExistsError as exc:
            raise CaptureError(
                f"evidence payload already exists: {staging_dir}"
            ) from exc
        except OSError as exc:
            _raise_unsafe_path(staging_dir, exc)

        staging_stat = _entry_stat_at(
            parent_fd,
            staging_dir.name,
            display_path=staging_dir,
        )
        if not stat.S_ISDIR(staging_stat.st_mode):
            raise CaptureError(
                f"evidence staging path is not a directory: {staging_dir}"
            )
        staging_fd = _open_directory_entry(
            parent_fd,
            staging_dir.name,
            expected_stat=staging_stat,
            display_path=staging_dir,
        )

        result_exit_code = 2
        try:
            result_exit_code = _capture_into_staging(
                staging_dir=staging_dir,
                config=normalized,
                staging_fd=staging_fd,
                runner=runner,
                environ=environment,
                distributions=distributions,
            )
        except Exception as exc:  # noqa: BLE001 - preserve capture evidence.
            try:
                _write_unexpected_failure(
                    staging_dir=staging_dir,
                    staging_fd=staging_fd,
                    config=normalized,
                    exc=exc,
                    secret_values=secret_values,
                )
            except Exception as write_exc:
                safe_write_error = _sanitize_text(
                    f"{type(write_exc).__name__}: {write_exc}",
                    secret_values,
                )
                raise CaptureError(
                    "evidence capture failed and the staging result could not be "
                    f"completed: {staging_dir}: {safe_write_error}"
                ) from write_exc

        _publish_staging_directory(
            staging_dir=staging_dir,
            output_dir=normalized.output_dir,
            parent_fd=parent_fd,
            staging_fd=staging_fd,
        )
        return result_exit_code
    finally:
        if staging_fd is not None:
            os.close(staging_fd)
        os.close(parent_fd)


def _publish_staging_directory(
    *,
    staging_dir: Path,
    output_dir: Path,
    parent_fd: int | None = None,
    staging_fd: int | None = None,
) -> None:
    absolute_staging = _absolute_path(staging_dir)
    absolute_output = _absolute_path(output_dir)
    if absolute_staging.parent != absolute_output.parent:
        raise CaptureError(
            "evidence staging and output directories require the same parent"
        )

    active_parent_fd = parent_fd
    owned_parent_fd = False
    if active_parent_fd is None:
        active_parent_fd = _open_directory_path_nofollow(absolute_output.parent)
        owned_parent_fd = True

    active_staging_fd = staging_fd
    owned_staging_fd = False
    lock_fd: int | None = None
    lock_identity: tuple[int, int] | None = None
    lock_name = f".{absolute_output.name}.publish.lock"
    lock_path = absolute_output.with_name(lock_name)
    try:
        _verify_directory_path_identity(
            absolute_output.parent,
            active_parent_fd,
            description="evidence output parent",
        )
        try:
            lock_fd = os.open(
                lock_name,
                _regular_open_flags(os.O_CREAT | os.O_EXCL | os.O_WRONLY),
                0o600,
                dir_fd=active_parent_fd,
            )
        except FileExistsError as exc:
            raise CaptureError(
                "evidence publication lock already exists; staging evidence "
                f"retained at {absolute_staging}"
            ) from exc
        except OSError as exc:
            _raise_unsafe_path(lock_path, exc)

        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode):
            raise CaptureError(
                f"evidence publication lock is not a regular file: {lock_path}"
            )
        lock_identity = _entry_identity(lock_stat)
        with os.fdopen(lock_fd, "wb", closefd=False) as lock_handle:
            lock_handle.write(f"{absolute_staging}\n".encode())
            lock_handle.flush()
        os.fsync(lock_fd)
        os.fsync(active_parent_fd)

        if active_staging_fd is None:
            staging_stat = _entry_stat_at(
                active_parent_fd,
                absolute_staging.name,
                display_path=absolute_staging,
            )
            if not stat.S_ISDIR(staging_stat.st_mode):
                raise CaptureError(
                    "evidence staging directory is unavailable: "
                    f"{absolute_staging}"
                )
            active_staging_fd = _open_directory_entry(
                active_parent_fd,
                absolute_staging.name,
                expected_stat=staging_stat,
                display_path=absolute_staging,
            )
            owned_staging_fd = True

        expected_identity = _directory_identity_from_fd(
            active_staging_fd,
            display_path=absolute_staging,
        )
        expected_manifest = _validated_manifest_bytes_from_fd(
            active_staging_fd,
            display_dir=absolute_staging,
        )
        _fsync_payload_tree_from_fd(
            active_staging_fd,
            display_dir=absolute_staging,
        )
        durable_manifest = _validated_manifest_bytes_from_fd(
            active_staging_fd,
            display_dir=absolute_staging,
        )
        if durable_manifest != expected_manifest:
            raise CaptureError(
                "evidence manifest or payload changed before publication"
            )

        _verify_directory_path_identity(
            absolute_output.parent,
            active_parent_fd,
            description="evidence output parent",
        )
        source_identity = _directory_entry_identity(
            active_parent_fd,
            absolute_staging.name,
            display_path=absolute_staging,
        )
        if source_identity != expected_identity:
            raise CaptureError(
                "evidence staging directory identity changed before publication"
            )

        try:
            _rename_directory_noreplace_at(
                active_parent_fd,
                absolute_staging.name,
                active_parent_fd,
                absolute_output.name,
                absolute_output,
            )
        except FileExistsError as exc:
            raise CaptureError(
                "output path appeared during capture; staging evidence retained at "
                f"{absolute_staging}"
            ) from exc
        except OSError as exc:
            raise CaptureError(
                "evidence publication failed; staging evidence retained at "
                f"{absolute_staging}: {type(exc).__name__}: {exc}"
            ) from exc

        os.fsync(active_parent_fd)
        publication_error = _published_identity_error(
            parent_fd=active_parent_fd,
            output_dir=absolute_output,
            expected_identity=expected_identity,
            expected_manifest=expected_manifest,
        )
        if publication_error:
            quarantine_dir = _quarantine_published_directory_at(
                parent_fd=active_parent_fd,
                output_dir=absolute_output,
            )
            raise CaptureError(
                f"{publication_error}; rejected evidence retained at "
                f"{quarantine_dir}"
            )
    finally:
        if owned_staging_fd is True and active_staging_fd is not None:
            os.close(active_staging_fd)
        if lock_fd is not None:
            os.close(lock_fd)
            if lock_identity is not None:
                _unlink_owned_entry_at(
                    active_parent_fd,
                    lock_name,
                    expected_identity=lock_identity,
                )
            os.fsync(active_parent_fd)
        if owned_parent_fd is True:
            os.close(active_parent_fd)


def _published_identity_error(
    *,
    parent_fd: int,
    output_dir: Path,
    expected_identity: tuple[int, int],
    expected_manifest: bytes,
) -> str | bool:
    try:
        output_stat = _entry_stat_at(
            parent_fd,
            output_dir.name,
            display_path=output_dir,
        )
        if not stat.S_ISDIR(output_stat.st_mode):
            return "published evidence directory is unavailable"
        published_fd = _open_directory_entry(
            parent_fd,
            output_dir.name,
            expected_stat=output_stat,
            display_path=output_dir,
        )
    except (CaptureError, OSError):
        return "published evidence directory is unavailable"
    try:
        published_identity = _directory_identity_from_fd(
            published_fd,
            display_path=output_dir,
        )
        if published_identity != expected_identity:
            return "published evidence directory identity changed"
        _verify_directory_path_identity(
            output_dir,
            published_fd,
            description="published evidence path",
        )
        published_manifest = _validated_manifest_bytes_from_fd(
            published_fd,
            display_dir=output_dir,
        )
        if published_manifest != expected_manifest:
            return "published evidence manifest changed"
        repeated_manifest = _validated_manifest_bytes_from_fd(
            published_fd,
            display_dir=output_dir,
        )
        if repeated_manifest != expected_manifest:
            return "published evidence manifest changed"
        path_identity = _directory_entry_identity(
            parent_fd,
            output_dir.name,
            display_path=output_dir,
        )
        if path_identity != expected_identity:
            return "published evidence path identity changed"
    except CaptureError:
        return "published evidence manifest changed or does not match payload"
    except OSError:
        return "published evidence verification failed"
    finally:
        os.close(published_fd)
    return False


def _unlink_owned_entry_at(
    directory_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int],
) -> None:
    try:
        entry_stat = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if _entry_identity(entry_stat) != expected_identity:
        return
    os.unlink(name, dir_fd=directory_fd)


def _quarantine_published_directory_at(
    *,
    parent_fd: int,
    output_dir: Path,
) -> Path:
    quarantine_dir = output_dir.with_name(
        f".{output_dir.name}.rejected-{os.getpid()}-{time.time_ns()}"
    )
    try:
        _rename_directory_noreplace_at(
            parent_fd,
            output_dir.name,
            parent_fd,
            quarantine_dir.name,
            quarantine_dir,
        )
        os.fsync(parent_fd)
    except OSError as exc:
        raise CaptureError(
            "published evidence failed identity verification and could not be "
            f"quarantined: {output_dir}: {type(exc).__name__}: {exc}"
        ) from exc
    return quarantine_dir


def _rename_directory_noreplace(
    source: Path,
    destination: Path,
) -> None:
    absolute_source = _absolute_path(source)
    absolute_destination = _absolute_path(destination)
    source_parent_fd = _open_directory_path_nofollow(absolute_source.parent)
    try:
        destination_parent_fd = _open_directory_path_nofollow(
            absolute_destination.parent
        )
        try:
            _rename_directory_noreplace_at(
                source_parent_fd,
                absolute_source.name,
                destination_parent_fd,
                absolute_destination.name,
                absolute_destination,
            )
        finally:
            os.close(destination_parent_fd)
    finally:
        os.close(source_parent_fd)


def _rename_directory_noreplace_at(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
    display_destination: Path,
) -> None:
    for name in (source_name, destination_name):
        if not name or "/" in name or name in {".", ".."}:
            raise CaptureError(f"invalid evidence directory name: {name}")
    if sys.platform == "darwin":
        _darwin_rename_noreplace_at(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
            display_destination,
        )
        return
    if sys.platform.startswith("linux"):
        _linux_rename_noreplace_at(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
            display_destination,
        )
        return
    raise OSError(
        errno.ENOTSUP,
        "atomic no-replace directory publication is unsupported",
        str(display_destination),
    )


def _darwin_rename_noreplace_at(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
    display_destination: Path,
) -> None:
    rename_excl = 0x00000004
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = libc.renameatx_np
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        rename_excl,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            str(display_destination),
        )
    raise OSError(
        error_number,
        os.strerror(error_number),
        str(display_destination),
    )


def _linux_rename_noreplace_at(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
    display_destination: Path,
) -> None:
    rename_noreplace = 1
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise OSError(
            errno.ENOTSUP,
            "libc renameat2 is unavailable",
            str(display_destination),
        ) from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        rename_noreplace,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            str(display_destination),
        )
    raise OSError(
        error_number,
        os.strerror(error_number),
        str(display_destination),
    )


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
        f"WROTE {config.output_dir.expanduser().absolute()} "
        f"suite={config.suite_name} exit_code={exit_code}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
