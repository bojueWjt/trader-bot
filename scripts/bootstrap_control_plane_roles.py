#!/usr/bin/env python3
"""Generate isolated control-plane role env files from the live runtime env."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import secrets
import shlex
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlsplit, urlunsplit


DEFAULT_ENV_FILE = Path("/srv/trader-v3/.env.v3")
DEFAULT_OUTPUT_DIR = Path("/srv/trader-v3/secrets/control-plane")
DEFAULT_OPERATION_LOCK_PATH = Path(
    "/var/lock/trader-v3-account-stall-operation.lock"
)
OPERATION_LOCK_OWNERSHIP_ENV = "ACCOUNT_STALL_OPERATION_LOCK_OWNERSHIP"
OPERATION_LOCK_FD_ENV = "ACCOUNT_STALL_OPERATION_LOCK_FD"
OPERATION_LOCK_TOKEN_ENV = "ACCOUNT_STALL_OPERATION_LOCK_TOKEN"
TEST_OPERATION_LOCK_PATH_ENV = "TRADER_TEST_ACCOUNT_STALL_OPERATION_LOCK"
TEST_MODE_ENV = "TRADER_RELEASE_TEST_MODE"
DATABASE_URL_ENV = "DATABASE_URL"
SQLITE_ENV_NAMES = (
    "TRADER_TRADING_DB_PATH",
    "WATCHER_TRADING_DB",
    "TRADING_DB_PATH",
)
NODE_AUTH_ENVS = (
    "NAUTILUS_NODE_AUTH_JSON",
    "NAUTILUS_NODE_TOKEN",
)
READER_TOKEN_ENVS = (
    "SYSTEM_OBSERVER_TOKEN",
    "VIEWER_TOKEN",
    "RISK_ADMIN_TOKEN",
    "REVIEWER_TOKEN",
)
OPERATOR_CONFIG_ENVS = (
    "OPERATOR_ACCOUNT_REGISTRY_JSON",
    "OPERATOR_MAX_NOTIONAL_USDT",
    "OPERATOR_MAX_LEVERAGE",
    "OPERATOR_MAX_RISK_FRACTION",
    "OPERATOR_NO_SL_EQUITY_FRACTION",
    "OPERATOR_EQUITY_MAX_AGE_SECONDS",
    "OPERATOR_DEFAULT_RISK_RATIO",
    "OPERATOR_DEFAULT_ACCOUNT",
    "MEDIA_ROOT",
    "ATTRIBUTION_SHADOW_LOG",
)
POOL_ENV_SUFFIXES = (
    "POOL_SIZE",
    "CHECKOUT_TIMEOUT_SECONDS",
    "CONNECT_TIMEOUT_SECONDS",
    "STATEMENT_TIMEOUT_MS",
)
SENSITIVE_KEY_RE = re.compile(
    r"(SECRET|PASSWORD|TOKEN|AUTH|KEY|DATABASE_URL)",
    re.IGNORECASE,
)
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DATABASE_ROLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")


class BootstrapControlPlaneRolesError(RuntimeError):
    pass


@dataclass(frozen=True)
class RoleSpec:
    label: str
    app_role: str
    database_role: str
    filename: str

    @property
    def pool_prefix(self) -> str:
        return "CONTROL_PLANE_" + self.app_role.upper().replace("-", "_") + "_DB_"


ROLE_SPECS = (
    RoleSpec(
        label="node-control",
        app_role="node-control",
        database_role="trader_v3_node_control",
        filename="node-control.env",
    ),
    RoleSpec(
        label="event-ingest",
        app_role="event-ingest",
        database_role="trader_v3_event_ingest",
        filename="event-ingest.env",
    ),
    RoleSpec(
        label="operator-query",
        app_role="operator-query",
        database_role="trader_v3_operator_query",
        filename="operator-query.env",
    ),
)


class AccountStallOperationLock:
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled
        self._fd: int | bool = False
        self._inherited = False
        self._token = ""

    def __enter__(self) -> "AccountStallOperationLock":
        if not self._enabled:
            return self
        path = operation_lock_path()
        ownership = os.environ.get(
            OPERATION_LOCK_OWNERSHIP_ENV,
            "standalone",
        ).strip()
        if ownership == "inherited":
            self._acquire_inherited(path)
            return self
        if ownership != "standalone":
            raise BootstrapControlPlaneRolesError(
                "invalid account-stall operation lock ownership"
            )
        self._acquire_standalone(path)
        return self

    def __exit__(self, *_args: object) -> None:
        fd = self._fd
        self._fd = False
        self._token = ""
        if fd is False or self._inherited:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _acquire_inherited(self, path: Path) -> None:
        fd_raw = os.environ.get(OPERATION_LOCK_FD_ENV, "").strip()
        token = os.environ.get(OPERATION_LOCK_TOKEN_ENV, "").strip()
        if fd_raw != "9":
            raise BootstrapControlPlaneRolesError(
                "account-stall inherited lock fd must be 9"
            )
        if re.fullmatch(r"[0-9a-f]{64}", token) is None:
            raise BootstrapControlPlaneRolesError(
                "account-stall inherited lock token is invalid"
            )
        fd = int(fd_raw)
        try:
            fd_metadata = os.fstat(fd)
            path_metadata = path.lstat()
        except OSError as exc:
            raise BootstrapControlPlaneRolesError(
                "account-stall inherited lock is unavailable"
            ) from exc
        if not stat.S_ISREG(fd_metadata.st_mode):
            raise BootstrapControlPlaneRolesError(
                "account-stall inherited lock fd is not a regular file"
            )
        if not stat.S_ISREG(path_metadata.st_mode):
            raise BootstrapControlPlaneRolesError(
                "account-stall inherited lock path is unsafe"
            )
        if fd_metadata.st_nlink != 1 or path_metadata.st_nlink != 1:
            raise BootstrapControlPlaneRolesError(
                "account-stall inherited lock path is unsafe"
            )
        if fd_metadata.st_dev != path_metadata.st_dev:
            raise BootstrapControlPlaneRolesError(
                "account-stall inherited lock fd does not match lock path"
            )
        if fd_metadata.st_ino != path_metadata.st_ino:
            raise BootstrapControlPlaneRolesError(
                "account-stall inherited lock fd does not match lock path"
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            recorded_token = os.pread(fd, 4096, 0).decode("ascii").strip()
        except (BlockingIOError, OSError, UnicodeDecodeError) as exc:
            raise BootstrapControlPlaneRolesError(
                "account-stall inherited lock ownership was lost"
            ) from exc
        if recorded_token != token:
            raise BootstrapControlPlaneRolesError(
                "account-stall inherited lock token mismatch"
            )
        self._fd = fd
        self._inherited = True
        self._token = token

    def _acquire_standalone(self, path: Path) -> None:
        flags = os.O_RDWR | os.O_CREAT
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags | no_follow, 0o600)
        except OSError as exc:
            raise BootstrapControlPlaneRolesError(
                f"cannot open account-stall operation lock: {path}"
            ) from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise BootstrapControlPlaneRolesError(
                    "account-stall operation lock is not a regular file"
                )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(fd, 0o600)
            token = secrets.token_hex(32)
            os.ftruncate(fd, 0)
            os.pwrite(fd, (token + "\n").encode("ascii"), 0)
            os.fsync(fd)
        except BlockingIOError as exc:
            os.close(fd)
            raise BootstrapControlPlaneRolesError(
                f"another account-stall operation holds {path}"
            ) from exc
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        self._token = token

    def require_held(self) -> None:
        if not self._enabled:
            raise BootstrapControlPlaneRolesError(
                "mutation requires the account-stall operation lock"
            )
        fd = self._fd
        if fd is False:
            raise BootstrapControlPlaneRolesError(
                "account-stall operation lock is not held"
            )
        path = operation_lock_path()
        try:
            fd_metadata = os.fstat(fd)
            path_metadata = path.lstat()
        except OSError as exc:
            raise BootstrapControlPlaneRolesError(
                "account-stall operation lock identity is unavailable"
            ) from exc
        if not stat.S_ISREG(fd_metadata.st_mode):
            raise BootstrapControlPlaneRolesError(
                "account-stall operation lock must remain a regular file"
            )
        if not stat.S_ISREG(path_metadata.st_mode):
            raise BootstrapControlPlaneRolesError(
                "account-stall operation lock must remain a regular file"
            )
        if fd_metadata.st_nlink != 1 or path_metadata.st_nlink != 1:
            raise BootstrapControlPlaneRolesError(
                "account-stall operation lock must remain a regular file"
            )
        if fd_metadata.st_dev != path_metadata.st_dev:
            raise BootstrapControlPlaneRolesError(
                "account-stall operation lock inode changed"
            )
        if fd_metadata.st_ino != path_metadata.st_ino:
            raise BootstrapControlPlaneRolesError(
                "account-stall operation lock inode changed"
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            recorded_token = os.pread(fd, 4096, 0).decode("ascii").strip()
        except (BlockingIOError, OSError, UnicodeDecodeError) as exc:
            raise BootstrapControlPlaneRolesError(
                "account-stall operation lock ownership was lost"
            ) from exc
        if recorded_token != self._token:
            raise BootstrapControlPlaneRolesError(
                "account-stall operation lock owner token mismatch"
            )


def operation_lock_path() -> Path:
    raw = str(DEFAULT_OPERATION_LOCK_PATH)
    test_path = os.environ.get(TEST_OPERATION_LOCK_PATH_ENV, "").strip()
    if test_path:
        if os.environ.get(TEST_MODE_ENV) != "1":
            raise BootstrapControlPlaneRolesError(
                "operation lock test override requires test mode"
            )
        raw = test_path
    configured = os.environ.get("ACCOUNT_STALL_OPERATION_LOCK", "").strip()
    if configured and configured != raw:
        raise BootstrapControlPlaneRolesError(
            "account-stall operation lock path is fixed"
        )
    path = Path(raw)
    if not path.is_absolute():
        raise BootstrapControlPlaneRolesError(
            "account-stall operation lock path must be absolute"
        )
    if path.is_symlink():
        raise BootstrapControlPlaneRolesError(
            "account-stall operation lock cannot be a symlink"
        )
    return path


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BootstrapControlPlaneRolesError(
            f"runtime env file is unavailable: {path}"
        ) from exc
    for index, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        name, separator, raw_value = line.partition("=")
        if not separator:
            raise BootstrapControlPlaneRolesError(
                f"invalid env assignment at line {index}"
            )
        name = name.strip()
        if ENV_NAME_RE.fullmatch(name) is None:
            raise BootstrapControlPlaneRolesError(
                f"invalid env name at line {index}"
            )
        if name in values:
            raise BootstrapControlPlaneRolesError(
                f"duplicate env assignment: {name}"
            )
        values[name] = _parse_env_value(raw_value.strip(), index)
    return values


def _parse_env_value(raw_value: str, index: int) -> str:
    if not raw_value:
        return ""
    quote_char = raw_value[0]
    if quote_char not in {"'", '"'}:
        return raw_value
    if len(raw_value) < 2 or raw_value[-1] != quote_char:
        raise BootstrapControlPlaneRolesError(
            f"unterminated quoted value at line {index}"
        )
    if quote_char == "'":
        return raw_value[1:-1]
    try:
        parsed = shlex.split("VALUE=" + raw_value, posix=True)
    except ValueError as exc:
        raise BootstrapControlPlaneRolesError(
            f"invalid quoted value at line {index}"
        ) from exc
    if len(parsed) != 1 or not parsed[0].startswith("VALUE="):
        raise BootstrapControlPlaneRolesError(
            f"invalid quoted value at line {index}"
        )
    return parsed[0][len("VALUE="):]


def shell_env_line(name: str, value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise BootstrapControlPlaneRolesError(
            f"{name} contains an unsafe control character"
        )
    if ENV_NAME_RE.fullmatch(name) is None:
        raise BootstrapControlPlaneRolesError(f"invalid env name: {name}")
    return f"{name}={shlex.quote(value)}"


def source_database_url(env: dict[str, str]) -> str:
    database_url = env.get(DATABASE_URL_ENV, "").strip()
    if not database_url:
        raise BootstrapControlPlaneRolesError("DATABASE_URL is required")
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise BootstrapControlPlaneRolesError("DATABASE_URL scheme is invalid")
    if not parsed.hostname:
        raise BootstrapControlPlaneRolesError("DATABASE_URL host is required")
    path = parsed.path.strip()
    if not path or path == "/":
        raise BootstrapControlPlaneRolesError(
            "DATABASE_URL database name is required"
        )
    return database_url


def role_database_url(source_url: str, role_name: str, password: str) -> str:
    if DATABASE_ROLE_RE.fullmatch(role_name) is None:
        raise BootstrapControlPlaneRolesError("database role is invalid")
    if not password:
        raise BootstrapControlPlaneRolesError("database password is required")
    parsed = urlsplit(source_url)
    host = parsed.hostname
    if not host:
        raise BootstrapControlPlaneRolesError("DATABASE_URL host is required")
    username = quote(role_name, safe="")
    encoded_password = quote(password, safe="")
    netloc = f"{username}:{encoded_password}@{host}"
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def generate_passwords(
    password_factory: Callable[[], str] | bool = False,
) -> dict[str, str]:
    passwords: dict[str, str] = {}
    for spec in ROLE_SPECS:
        if password_factory is not False:
            password = password_factory()
        else:
            password = secrets.token_urlsafe(48)
        if not password:
            raise BootstrapControlPlaneRolesError("generated password is empty")
        passwords[spec.database_role] = password
    if len(set(passwords.values())) != len(passwords):
        raise BootstrapControlPlaneRolesError(
            "generated database passwords must be independent"
        )
    return passwords


def validate_sqlite_env(env: dict[str, str]) -> dict[str, str]:
    configured: list[tuple[str, str]] = []
    for name in SQLITE_ENV_NAMES:
        value = env.get(name, "").strip()
        if value:
            configured.append((name, value))
    if not configured:
        raise BootstrapControlPlaneRolesError(
            "operator-query requires a configured watcher SQLite path"
        )
    canonical_name, canonical_value = configured[0]
    for name, value in configured[1:]:
        if value != canonical_value:
            raise BootstrapControlPlaneRolesError(
                "conflicting watcher SQLite path environment: "
                f"{canonical_name} and {name}"
            )
    result = {name: value for name, value in configured}
    if "WATCHER_TRADING_DB" not in result:
        result["WATCHER_TRADING_DB"] = canonical_value
    return result


def require_any(env: dict[str, str], names: tuple[str, ...], label: str) -> None:
    for name in names:
        if env.get(name, "").strip():
            return
    raise BootstrapControlPlaneRolesError(f"{label} is required")


def copy_present(
    source: dict[str, str],
    names: tuple[str, ...],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in names:
        value = source.get(name)
        if value is not None and value != "":
            result[name] = value
    return result


def pool_env_names(spec: RoleSpec) -> tuple[str, ...]:
    return tuple(spec.pool_prefix + suffix for suffix in POOL_ENV_SUFFIXES)


def build_role_envs(
    source_env: dict[str, str],
    passwords: dict[str, str],
) -> dict[str, dict[str, str]]:
    base_database_url = source_database_url(source_env)
    role_envs: dict[str, dict[str, str]] = {}
    require_any(source_env, NODE_AUTH_ENVS, "node auth token configuration")
    require_any(source_env, ("RISK_ADMIN_TOKEN",), "risk admin token")
    sqlite_env = validate_sqlite_env(source_env)
    for spec in ROLE_SPECS:
        password = passwords.get(spec.database_role, "")
        role_env = {
            "CONTROL_PLANE_APP_ROLE": spec.app_role,
            "CONTROL_PLANE_EXPECT_DATABASE_ROLE": spec.database_role,
            "DATABASE_URL": role_database_url(
                base_database_url,
                spec.database_role,
                password,
            ),
        }
        role_env.update(copy_present(source_env, pool_env_names(spec)))
        if spec.app_role in {"node-control", "event-ingest"}:
            role_env.update(copy_present(source_env, NODE_AUTH_ENVS))
        if spec.app_role == "operator-query":
            role_env.update(copy_present(source_env, READER_TOKEN_ENVS))
            role_env.update(copy_present(source_env, OPERATOR_CONFIG_ENVS))
            role_env.update(sqlite_env)
        role_envs[spec.label] = role_env
    validate_role_envs(role_envs)
    return role_envs


def validate_role_envs(role_envs: dict[str, dict[str, str]]) -> None:
    expected_labels = {spec.label for spec in ROLE_SPECS}
    if set(role_envs) != expected_labels:
        raise BootstrapControlPlaneRolesError("role env exact-set mismatch")
    urls: list[str] = []
    for spec in ROLE_SPECS:
        env = role_envs[spec.label]
        if env.get("CONTROL_PLANE_APP_ROLE") != spec.app_role:
            raise BootstrapControlPlaneRolesError(
                f"{spec.label} app role mismatch"
            )
        if env.get("CONTROL_PLANE_EXPECT_DATABASE_ROLE") != spec.database_role:
            raise BootstrapControlPlaneRolesError(
                f"{spec.label} database role mismatch"
            )
        url = env.get("DATABASE_URL", "")
        parsed = urlsplit(url)
        username = unquote(parsed.username or "")
        if username != spec.database_role:
            raise BootstrapControlPlaneRolesError(
                f"{spec.label} DATABASE_URL user mismatch"
            )
        urls.append(url)
        if spec.app_role in {"node-control", "event-ingest"}:
            require_any(env, NODE_AUTH_ENVS, f"{spec.label} node auth")
        if spec.app_role == "operator-query":
            require_any(env, ("RISK_ADMIN_TOKEN",), "operator-query risk admin token")
            validate_sqlite_env(env)
    if len(set(urls)) != len(urls):
        raise BootstrapControlPlaneRolesError(
            "control-plane DATABASE_URL values must not be reused"
        )


def render_env(role_env: dict[str, str]) -> str:
    lines = [
        "# generated by bootstrap_control_plane_roles.py",
        "# contains secrets; keep mode 0600 root:root",
    ]
    for name in sorted(role_env):
        lines.append(shell_env_line(name, role_env[name]))
    return "\n".join(lines) + "\n"


def target_paths(output_dir: Path) -> dict[str, Path]:
    if not output_dir.is_absolute():
        raise BootstrapControlPlaneRolesError("output directory must be absolute")
    result: dict[str, Path] = {}
    for spec in ROLE_SPECS:
        result[spec.label] = output_dir / spec.filename
    return result


def assert_no_sensitive_output(role_envs: dict[str, dict[str, str]]) -> list[str]:
    safe: list[str] = []
    for spec in ROLE_SPECS:
        env = role_envs[spec.label]
        keys = ",".join(sorted(env))
        safe.append(f"{spec.label}: keys={keys}")
    return safe


def alter_role_passwords(
    admin_database_url: str,
    passwords: dict[str, str],
    *,
    connect: Callable[..., Any] | bool = False,
) -> None:
    if connect is False:
        import psycopg2

        connect = psycopg2.connect
    conn = connect(admin_database_url)
    try:
        with conn.cursor() as cur:
            for spec in ROLE_SPECS:
                password = passwords[spec.database_role]
                cur.execute(
                    f"ALTER ROLE {spec.database_role} WITH PASSWORD %s",
                    (password,),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_role_database_identities(
    role_envs: dict[str, dict[str, str]],
    *,
    connect: Callable[..., Any] | bool = False,
) -> dict[str, dict[str, str]]:
    if connect is False:
        import psycopg2

        connect = psycopg2.connect
    identities: dict[str, dict[str, str]] = {}
    for spec in ROLE_SPECS:
        database_url = role_envs[spec.label]["DATABASE_URL"]
        conn = connect(database_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT session_user, current_user")
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None or len(row) != 2:
            raise BootstrapControlPlaneRolesError(
                f"{spec.label} role identity query returned no row"
            )
        session_user = str(row[0] or "")
        current_user = str(row[1] or "")
        if session_user != spec.database_role:
            raise BootstrapControlPlaneRolesError(
                f"{spec.label} session_user mismatch"
            )
        if current_user != spec.database_role:
            raise BootstrapControlPlaneRolesError(
                f"{spec.label} current_user mismatch"
            )
        identities[spec.label] = {
            "session_user": session_user,
            "current_user": current_user,
        }
    return identities


def atomic_write_env_file(
    path: Path,
    content: str,
    *,
    owner_uid: int,
    owner_gid: int,
    mode: int = 0o600,
) -> None:
    if not path.is_absolute():
        raise BootstrapControlPlaneRolesError("env path must be absolute")
    if path.is_symlink():
        raise BootstrapControlPlaneRolesError(
            f"env path cannot be a symlink: {path}"
        )
    parent = path.parent
    if parent.is_symlink():
        raise BootstrapControlPlaneRolesError(
            f"env parent cannot be a symlink: {parent}"
        )
    parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(parent),
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, owner_uid, owner_gid)
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        raise
    metadata = path.stat()
    actual_mode = stat.S_IMODE(metadata.st_mode)
    if actual_mode != mode:
        raise BootstrapControlPlaneRolesError(
            f"env file mode mismatch: {path}"
        )
    if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
        raise BootstrapControlPlaneRolesError(
            f"env file owner mismatch: {path}"
        )


def write_role_env_files(
    role_envs: dict[str, dict[str, str]],
    output_dir: Path,
    *,
    owner_uid: int,
    owner_gid: int,
) -> dict[str, Path]:
    paths = target_paths(output_dir)
    for spec in ROLE_SPECS:
        atomic_write_env_file(
            paths[spec.label],
            render_env(role_envs[spec.label]),
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
    return paths


def run_check(env_file: Path, output_dir: Path) -> dict[str, Any]:
    source_env = parse_env_file(env_file)
    passwords = {
        spec.database_role: f"check-mode-{index}"
        for index, spec in enumerate(ROLE_SPECS, start=1)
    }
    role_envs = build_role_envs(source_env, passwords)
    paths = target_paths(output_dir)
    return {
        "role_envs": role_envs,
        "paths": paths,
        "safe_lines": assert_no_sensitive_output(role_envs),
    }


def run_apply(
    env_file: Path,
    output_dir: Path,
    *,
    owner_uid: int,
    owner_gid: int,
) -> dict[str, Any]:
    with AccountStallOperationLock(enabled=True) as operation_lock:
        operation_lock.require_held()
        source_env = parse_env_file(env_file)
        admin_database_url = source_database_url(source_env)
        passwords = generate_passwords()
        role_envs = build_role_envs(source_env, passwords)
        alter_role_passwords(admin_database_url, passwords)
        identities = verify_role_database_identities(role_envs)
        paths = write_role_env_files(
            role_envs,
            output_dir,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
    return {
        "paths": paths,
        "identities": identities,
        "safe_lines": assert_no_sensitive_output(role_envs),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap isolated control-plane DB role env files.",
    )
    parser.add_argument(
        "mode",
        choices=("check", "apply"),
        help="check is read-only; apply mutates DB roles and env files",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="source runtime env file",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="target directory for role env files",
    )
    parser.add_argument(
        "--owner-uid",
        type=int,
        default=0,
        help="target file owner uid; production uses root",
    )
    parser.add_argument(
        "--owner-gid",
        type=int,
        default=0,
        help="target file owner gid; production uses root",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    try:
        if args.mode == "check":
            result = run_check(args.env_file, args.output_dir)
            print("CONTROL_PLANE_ROLE_ENV_CHECK_OK")
        else:
            result = run_apply(
                args.env_file,
                args.output_dir,
                owner_uid=args.owner_uid,
                owner_gid=args.owner_gid,
            )
            print("CONTROL_PLANE_ROLE_ENV_APPLY_OK")
        for spec in ROLE_SPECS:
            path = result["paths"][spec.label]
            print(f"{spec.label}: path={path}")
        for safe_line in result["safe_lines"]:
            print(safe_line)
    except BootstrapControlPlaneRolesError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
