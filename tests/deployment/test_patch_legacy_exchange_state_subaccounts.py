from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "patch_legacy_exchange_state_subaccounts.py"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "patch_legacy_exchange_state_subaccounts",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _synthetic_source(module) -> str:
    return (
        '"""Header.\n\n'
        + module._DOC_OLD
        + '"""\n\n'
        + "import argparse\n"
        + "import hashlib\n"
        + "import hmac\n"
        + "import json\n"
        + "import os\n"
        + module._IMPORT_OLD
        + "import time\n"
        + "import urllib.parse\n"
        + "import urllib.request\n"
        + module._PATH_IMPORT_OLD
        + "\nimport psycopg2\n\n"
        + module._ACCOUNTS_OLD
        + "\n"
        + "def log(message):\n"
        + "    return message\n\n"
        + module._CONTAINER_KEYS_OLD
    )


def _runtime_namespace(module) -> dict:
    source = module.patch_source(_synthetic_source(module))
    source = source.replace("import psycopg2\n", "")
    namespace = {}
    exec(compile(source, "synthetic-recorder.py", "exec"), namespace)
    return namespace


def test_patch_source_adds_four_accounts_and_secret_mount_loader() -> None:
    module = _load_script()
    patched = module.patch_source(_synthetic_source(module))

    assert module.PATCH_MARKER in patched
    assert '"account-c": ("trader-v3-node-c", "BINANCE_ACCOUNT_C")' in patched
    assert '"account-d": ("trader-v3-node-d", "BINANCE_ACCOUNT_D")' in patched
    assert "/run/secrets/{secret_stem}_api_key" in patched
    assert "source_mode != 0o440" in patched
    compile(patched, "patched-recorder.py", "exec")


def test_container_keys_prefers_complete_environment(monkeypatch) -> None:
    module = _load_script()
    namespace = _runtime_namespace(module)
    inspected = [
        {
            "Config": {
                "Env": [
                    "BINANCE_ACCOUNT_C_API_KEY=key-value",
                    "BINANCE_ACCOUNT_C_API_SECRET=secret-value",
                ]
            },
            "Mounts": [],
        }
    ]
    monkeypatch.setattr(
        namespace["subprocess"],
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps(inspected)
        ),
    )

    assert namespace["container_keys"](
        "trader-v3-node-c",
        "BINANCE_ACCOUNT_C",
    ) == ("key-value", "secret-value")


def test_container_keys_reads_reviewed_read_only_secret_mounts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_script()
    namespace = _runtime_namespace(module)
    key_path = tmp_path / "key"
    secret_path = tmp_path / "secret"
    key_path.write_text("key-value\n", encoding="utf-8")
    secret_path.write_text("secret-value\n", encoding="utf-8")
    key_path.chmod(0o440)
    secret_path.chmod(0o440)
    inspected = [
        {
            "Config": {
                "Env": [
                    "BINANCE_ACCOUNT_C_API_KEY=",
                    "BINANCE_ACCOUNT_C_API_SECRET=",
                ]
            },
            "Mounts": [
                {
                    "Source": str(key_path),
                    "Destination": (
                        "/run/secrets/binance_account_c_api_key"
                    ),
                    "RW": False,
                },
                {
                    "Source": str(secret_path),
                    "Destination": (
                        "/run/secrets/binance_account_c_api_secret"
                    ),
                    "RW": False,
                },
            ],
        }
    ]
    monkeypatch.setattr(
        namespace["subprocess"],
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps(inspected)
        ),
    )
    real_fstat = namespace["os"].fstat

    def root_owned_fstat(descriptor):
        value = real_fstat(descriptor)
        return SimpleNamespace(
            st_mode=value.st_mode,
            st_uid=0,
            st_gid=999,
        )

    monkeypatch.setattr(namespace["os"], "fstat", root_owned_fstat)

    assert namespace["container_keys"](
        "trader-v3-node-c",
        "BINANCE_ACCOUNT_C",
    ) == ("key-value", "secret-value")


def test_container_keys_fails_closed_on_writable_secret_mount(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_script()
    namespace = _runtime_namespace(module)
    key_path = tmp_path / "key"
    key_path.write_text("key-value", encoding="utf-8")
    key_path.chmod(0o440)
    inspected = [
        {
            "Config": {"Env": []},
            "Mounts": [
                {
                    "Source": str(key_path),
                    "Destination": (
                        "/run/secrets/binance_account_c_api_key"
                    ),
                    "RW": True,
                }
            ],
        }
    ]
    monkeypatch.setattr(
        namespace["subprocess"],
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps(inspected)
        ),
    )

    assert namespace["container_keys"](
        "trader-v3-node-c",
        "BINANCE_ACCOUNT_C",
    ) is None


def test_patch_file_is_atomic_and_idempotent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_script()
    source = _synthetic_source(module).encode()
    patched = module.patch_source(source.decode()).encode()
    monkeypatch.setattr(module, "EXPECTED_SOURCE_SHA256", module._sha256(source))
    monkeypatch.setattr(
        module,
        "EXPECTED_PATCHED_SHA256",
        module._sha256(patched),
    )
    target = tmp_path / "exchange_state_recorder.py"
    backup_dir = tmp_path / "backup"
    target.write_bytes(source)
    target.chmod(0o640)

    first = module.patch_file(target, backup_dir=backup_dir)
    second = module.patch_file(target, backup_dir=backup_dir)

    assert first["status"] == "patched"
    assert second["status"] == "already_patched"
    assert target.read_bytes() == patched
    assert Path(first["backup"]).read_bytes() == source
    assert os.stat(target).st_mode & 0o777 == 0o640


def test_patch_rejects_anchor_drift() -> None:
    module = _load_script()
    source = _synthetic_source(module).replace(module._ACCOUNTS_OLD, "")

    with pytest.raises(module.PatchError, match="account registry"):
        module.patch_source(source)
