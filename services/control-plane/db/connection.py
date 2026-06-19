from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg2
from psycopg2.extensions import connection as PsycopgConnection


def database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL is required")
    return value


def connect(url: str | None = None) -> PsycopgConnection:
    return psycopg2.connect(url or database_url())


@contextmanager
def transaction(conn: PsycopgConnection) -> Iterator[PsycopgConnection]:
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
