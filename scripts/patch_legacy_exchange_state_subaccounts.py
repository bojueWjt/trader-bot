#!/usr/bin/env python3
"""Patch the pinned HK exchange-state recorder for C/D secret mounts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

EXPECTED_SOURCE_SHA256 = (
    "32ef605c86966229b42f8e41559511cf943fde9d8cdb08857ad3d00d21daeb52"
)
EXPECTED_PATCHED_SHA256 = (
    "8992edb597b8399815364eeaeeef1211d1774cf070ed08548fe3ecddb23cafaf"
)
PATCH_MARKER = "# legacy-exchange-state-four-accounts:v1"
DEFAULT_TARGET = Path(
    "/srv/trader-v3/services/control-plane/tools/exchange_state_recorder.py"
)


class PatchError(RuntimeError):
    """Raised when the reviewed transform cannot be applied."""


_DOC_OLD = """\
API keys are read from the running node containers' env (single source of
truth; survives key rotation via the recreate scripts).
"""

_DOC_NEW = """\
API keys are read from the running node containers' env or reviewed read-only
secret mounts (single source of truth; survives key rotation via recreate).
"""

_IMPORT_OLD = """\
import re
import subprocess
import sys
"""

_IMPORT_NEW = """\
import re
import stat
import subprocess
import sys
"""

_PATH_IMPORT_OLD = """\
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError
"""

_PATH_IMPORT_NEW = """\
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError
"""

_ACCOUNTS_OLD = """\
ACCOUNTS = {
    "account-a": ("trader-v3-node-a", "BINANCE_ACCOUNT_A"),
    "account-b": ("trader-v3-node-b", "BINANCE_ACCOUNT_B"),
}
"""

_ACCOUNTS_NEW = """\
# legacy-exchange-state-four-accounts:v1
ACCOUNTS = {
    "account-a": ("trader-v3-node-a", "BINANCE_ACCOUNT_A"),
    "account-b": ("trader-v3-node-b", "BINANCE_ACCOUNT_B"),
    "account-c": ("trader-v3-node-c", "BINANCE_ACCOUNT_C"),
    "account-d": ("trader-v3-node-d", "BINANCE_ACCOUNT_D"),
}
"""

_CONTAINER_KEYS_OLD = """\
def container_keys(container: str, prefix: str) -> tuple[str, str] | None:
    try:
        out = subprocess.run(
            ["docker", "inspect", container, "--format", "{{json .Config.Env}}"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
        env = dict(item.split("=", 1) for item in json.loads(out) if "=" in item)
        key, sec = env.get(f"{prefix}_API_KEY"), env.get(f"{prefix}_API_SECRET")
        if key and sec:
            return key, sec
    except Exception as exc:  # noqa: BLE001 - missing keys must not kill the loop
        log(f"key fetch failed for {container}: {exc}")
    return None
"""

_CONTAINER_KEYS_NEW = """\
def _secret_mount_value(inspected: dict, destination: str) -> str | None:
    mounts = inspected.get("Mounts")
    if not isinstance(mounts, list):
        return None
    matches = [
        mount
        for mount in mounts
        if isinstance(mount, dict)
        and str(mount.get("Destination") or "") == destination
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError(f"secret mount is not unique: {destination}")
    mount = matches[0]
    if mount.get("RW") is not False:
        raise RuntimeError(f"secret mount must be read-only: {destination}")
    source = Path(str(mount.get("Source") or ""))
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(source, flags)
    try:
        source_stat = os.fstat(descriptor)
        source_mode = stat.S_IMODE(source_stat.st_mode)
        if not stat.S_ISREG(source_stat.st_mode):
            raise RuntimeError(
                f"secret mount source must be a regular file: {destination}"
            )
        if (
            source_stat.st_uid != 0
            or source_stat.st_gid != 999
            or source_mode != 0o440
        ):
            raise RuntimeError(
                f"secret mount source ownership or mode is invalid: {destination}"
            )
        raw = os.read(descriptor, 8193)
    finally:
        os.close(descriptor)
    if len(raw) > 8192:
        raise RuntimeError(f"secret mount value is too large: {destination}")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"secret mount value is not UTF-8: {destination}"
        ) from exc
    if not value:
        raise RuntimeError(f"secret mount value is empty: {destination}")
    return value


def container_keys(container: str, prefix: str) -> tuple[str, str] | None:
    try:
        out = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
        inspected_rows = json.loads(out)
        if not isinstance(inspected_rows, list) or len(inspected_rows) != 1:
            raise RuntimeError("docker inspect must return one container")
        inspected = inspected_rows[0]
        env_rows = inspected.get("Config", {}).get("Env", [])
        env = dict(
            item.split("=", 1)
            for item in env_rows
            if isinstance(item, str) and "=" in item
        )
        key = env.get(f"{prefix}_API_KEY")
        secret = env.get(f"{prefix}_API_SECRET")
        if key and secret:
            return key, secret

        secret_stem = prefix.lower()
        key = _secret_mount_value(
            inspected,
            f"/run/secrets/{secret_stem}_api_key",
        )
        secret = _secret_mount_value(
            inspected,
            f"/run/secrets/{secret_stem}_api_secret",
        )
        if key and secret:
            return key, secret
    except Exception as exc:  # noqa: BLE001 - missing keys must not kill the loop
        log(f"key fetch failed for {container}: {exc}")
    return None
"""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise PatchError(f"{label} anchor count must be 1, got {count}")
    return source.replace(old, new, 1)


def patch_source(source: str) -> str:
    if PATCH_MARKER in source:
        raise PatchError("source already contains the patch marker")
    patched = _replace_once(source, _DOC_OLD, _DOC_NEW, "docstring")
    patched = _replace_once(patched, _IMPORT_OLD, _IMPORT_NEW, "stat import")
    patched = _replace_once(
        patched,
        _PATH_IMPORT_OLD,
        _PATH_IMPORT_NEW,
        "path import",
    )
    patched = _replace_once(
        patched,
        _ACCOUNTS_OLD,
        _ACCOUNTS_NEW,
        "account registry",
    )
    patched = _replace_once(
        patched,
        _CONTAINER_KEYS_OLD,
        _CONTAINER_KEYS_NEW,
        "credential loader",
    )
    compile(patched, "patched-exchange-state-recorder.py", "exec")
    return patched


def _backup_path(target: Path, backup_dir: Path | None) -> Path:
    root = backup_dir if backup_dir is not None else target.parent
    return root / (
        target.name
        + ".pre-four-accounts."
        + EXPECTED_SOURCE_SHA256[:16]
        + ".bak"
    )


def _write_file(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.chown(temp_path, uid, gid)
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def patch_file(
    target: Path,
    *,
    backup_dir: Path | None = None,
    check_only: bool = False,
) -> dict[str, str]:
    if target.is_symlink() or not target.is_file():
        raise PatchError(f"target must be a regular file: {target}")
    source = target.read_bytes()
    source_sha256 = _sha256(source)
    backup = _backup_path(target, backup_dir)

    if source_sha256 == EXPECTED_PATCHED_SHA256:
        if not backup.is_file():
            raise PatchError(f"patched target has no reviewed backup: {backup}")
        if _sha256(backup.read_bytes()) != EXPECTED_SOURCE_SHA256:
            raise PatchError(f"patched target backup is invalid: {backup}")
        return {
            "status": "already_patched",
            "target": str(target),
            "source_sha256": source_sha256,
            "patched_sha256": source_sha256,
            "backup": str(backup),
        }
    if PATCH_MARKER.encode("utf-8") in source:
        raise PatchError("target contains a partial patch marker")
    if source_sha256 != EXPECTED_SOURCE_SHA256:
        raise PatchError(
            "target SHA256 mismatch: "
            f"expected {EXPECTED_SOURCE_SHA256}, got {source_sha256}"
        )
    try:
        patched = patch_source(source.decode("utf-8")).encode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError("target is not UTF-8") from exc
    patched_sha256 = _sha256(patched)
    if patched_sha256 != EXPECTED_PATCHED_SHA256:
        raise PatchError(
            "generated patch SHA256 mismatch: "
            f"expected {EXPECTED_PATCHED_SHA256}, got {patched_sha256}"
        )
    if check_only:
        return {
            "status": "ready",
            "target": str(target),
            "source_sha256": source_sha256,
            "patched_sha256": patched_sha256,
            "backup": str(backup),
        }

    target_stat = target.stat()
    mode = stat.S_IMODE(target_stat.st_mode)
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        if _sha256(backup.read_bytes()) != EXPECTED_SOURCE_SHA256:
            raise PatchError(f"existing backup is invalid: {backup}")
    else:
        _write_file(
            backup,
            source,
            mode=mode,
            uid=target_stat.st_uid,
            gid=target_stat.st_gid,
        )
    if _sha256(target.read_bytes()) != EXPECTED_SOURCE_SHA256:
        raise PatchError("target changed while backup was created")
    _write_file(
        target,
        patched,
        mode=mode,
        uid=target_stat.st_uid,
        gid=target_stat.st_gid,
    )
    if _sha256(target.read_bytes()) != EXPECTED_PATCHED_SHA256:
        raise PatchError("final patched SHA256 verification failed")
    return {
        "status": "patched",
        "target": str(target),
        "source_sha256": source_sha256,
        "patched_sha256": patched_sha256,
        "backup": str(backup),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = patch_file(
            args.target,
            backup_dir=args.backup_dir,
            check_only=args.check,
        )
    except PatchError as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
