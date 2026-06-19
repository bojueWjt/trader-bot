from __future__ import annotations

import sys

from app.db.schema import configured_signal_store_path, migrate_signal_store_schema


def migrate(db_path: str) -> None:
    migrate_signal_store_schema(db_path)


def main(argv: list[str] | None = None) -> int:
    args = argv
    if args is None:
        args = sys.argv[1:]

    db_path = args[0] if args else configured_signal_store_path()
    if not db_path:
        raise SystemExit("signal store db path required")

    migrate_signal_store_schema(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
