from __future__ import annotations

from contextlib import contextmanager

import read_api


class _RecordingCursor:
    def __init__(self) -> None:
        self.query = ""
        self.params: tuple[object, ...] = ()

    def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> None:
        self.query = query
        self.params = params or ()

    def fetchall(self) -> list[tuple[object, ...]]:
        return []

    def fetchone(self) -> None:
        return None


class _RecordingConnection:
    def __init__(self, cursor: _RecordingCursor) -> None:
        self._cursor = cursor
        self.closed = False

    @contextmanager
    def cursor(self):
        yield self._cursor

    def close(self) -> None:
        self.closed = True


def test_node_command_poll_has_a_hard_database_limit(monkeypatch) -> None:
    cursor = _RecordingCursor()
    connection = _RecordingConnection(cursor)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(
        read_api,
        "require_node",
        lambda *args, **kwargs: "account-a",
    )
    monkeypatch.setattr(
        read_api.psycopg2,
        "connect",
        lambda _database_url: connection,
    )

    response = read_api.node_commands(
        "node-a",
        "account-a",
        authorization="Bearer token",
        x_node_id="node-a",
        x_account_id="account-a",
        x_redis_fencing_epoch=None,
        x_runtime_generation=None,
        x_lease_fencing_token=None,
    )

    assert response == {"commands": []}
    assert "ORDER BY oc.created_at LIMIT %s" in cursor.query
    assert cursor.params == (
        "node-a",
        "account-a",
        read_api.NODE_COMMAND_POLL_LIMIT,
    )
    assert read_api.NODE_COMMAND_POLL_LIMIT == 64
    assert connection.closed is True
