from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATE_MODULE = (
    REPO_ROOT / "services" / "control-plane" / "db" / "migrate.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "control_plane_migrate",
        MIGRATE_MODULE,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Cursor:
    def __init__(self, registered_name: str | None) -> None:
        self.registered_name = registered_name
        self.executed: list[tuple[str, tuple[str, ...]]] = []

    def execute(self, sql: str, params: tuple[str, ...]) -> None:
        self.executed.append((sql, params))

    def fetchone(self):
        if self.registered_name is None:
            return None
        return (self.registered_name,)


def test_record_applied_migration_accepts_matching_self_registration() -> None:
    module = _load_module()
    migration = module.Migration(
        version="0007",
        name="exchange_state_mirror",
        up_path=Path("0007.up.sql"),
        down_path=Path("0007.down.sql"),
    )
    cursor = _Cursor("exchange_state_mirror")

    module._record_applied_migration(cursor, migration)

    assert len(cursor.executed) == 1
    assert "SELECT name" in cursor.executed[0][0]


def test_record_applied_migration_inserts_when_sql_did_not_register() -> None:
    module = _load_module()
    migration = module.Migration(
        version="0010",
        name="live_safety",
        up_path=Path("0010.up.sql"),
        down_path=Path("0010.down.sql"),
    )
    cursor = _Cursor(None)

    module._record_applied_migration(cursor, migration)

    assert len(cursor.executed) == 2
    assert "INSERT INTO schema_migrations" in cursor.executed[1][0]
    assert cursor.executed[1][1] == ("0010", "live_safety")


def test_record_applied_migration_rejects_conflicting_self_registration() -> None:
    module = _load_module()
    migration = module.Migration(
        version="0007",
        name="exchange_state_mirror",
        up_path=Path("0007.up.sql"),
        down_path=Path("0007.down.sql"),
    )
    cursor = _Cursor("wrong_name")

    with pytest.raises(RuntimeError, match="registered as wrong_name"):
        module._record_applied_migration(cursor, migration)
