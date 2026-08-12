from __future__ import annotations

import socket
import ssl
from collections.abc import Iterable
from threading import Lock
from typing import Any
from urllib.parse import unquote, urlparse


class RedisRespError(RuntimeError):
    pass


class RedisRespClient:
    """Small RESP2 client for runtime lease commands.

    The production node image does not include redis-py. This client keeps the
    fenced namespace lease independent from optional Python packages.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"redis", "rediss"}:
            raise ValueError("Redis URL must use redis:// or rediss://")
        if parsed.hostname is None:
            raise ValueError("Redis URL must include a host")
        if timeout_seconds <= 0:
            raise ValueError("Redis timeout_seconds must be positive")

        self._host = parsed.hostname
        self._port = parsed.port or 6379
        self._username = unquote(parsed.username) if parsed.username else ""
        self._password = unquote(parsed.password) if parsed.password else ""
        self._database = _database_number(parsed.path)
        self._use_tls = parsed.scheme == "rediss"
        self._timeout_seconds = float(timeout_seconds)
        self._lock = Lock()
        self._socket: socket.socket | ssl.SSLSocket | bool = False
        self._reader: Any = False

    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        return self.execute("EVAL", script, numkeys, *keys_and_args)

    def zrangebyscore(
        self,
        key: str,
        minimum: int,
        maximum: str,
        *,
        withscores: bool,
    ) -> Iterable[tuple[bytes | str, float]] | Iterable[bytes | str]:
        parts: list[object] = ["ZRANGEBYSCORE", key, minimum, maximum]
        if withscores:
            parts.append("WITHSCORES")
        raw = self.execute(*parts)
        if not isinstance(raw, list):
            raise RedisRespError("ZRANGEBYSCORE returned a non-array response")
        if not withscores:
            return raw
        if len(raw) % 2 != 0:
            raise RedisRespError("ZRANGEBYSCORE WITHSCORES returned odd fields")
        records = []
        for index in range(0, len(raw), 2):
            records.append((raw[index], float(raw[index + 1])))
        return records

    def hget(self, key: str, field: str) -> bytes | str | None:
        raw = self.execute("HGET", key, field)
        if raw is None or isinstance(raw, (bytes, str)):
            return raw
        raise RedisRespError("HGET returned an invalid response")

    def zscore(self, key: str, member: str) -> float | None:
        raw = self.execute("ZSCORE", key, member)
        if raw is None:
            return None
        if not isinstance(raw, (bytes, str)):
            raise RedisRespError("ZSCORE returned an invalid response")
        try:
            return float(raw)
        except ValueError as exc:
            raise RedisRespError("ZSCORE returned an invalid score") from exc

    def time(self) -> tuple[int, int]:
        raw = self.execute("TIME")
        if not isinstance(raw, list) or len(raw) != 2:
            raise RedisRespError("TIME returned an invalid response")
        try:
            seconds = int(raw[0])
            microseconds = int(raw[1])
        except (TypeError, ValueError) as exc:
            raise RedisRespError("TIME returned invalid integers") from exc
        if seconds <= 0 or microseconds < 0 or microseconds >= 1_000_000:
            raise RedisRespError("TIME returned invalid values")
        return seconds, microseconds

    def scan_streams(
        self,
        cursor: int,
        *,
        match: str,
        count: int,
    ) -> tuple[int, list[bytes | str]]:
        if cursor < 0:
            raise ValueError("Redis SCAN cursor must be non-negative")
        if not match:
            raise ValueError("Redis SCAN match must be non-empty")
        if count < 1:
            raise ValueError("Redis SCAN count must be positive")
        raw = self.execute(
            "SCAN",
            cursor,
            "MATCH",
            match,
            "COUNT",
            count,
            "TYPE",
            "stream",
        )
        if not isinstance(raw, list) or len(raw) != 2:
            raise RedisRespError("SCAN returned an invalid response")
        raw_cursor, raw_keys = raw
        try:
            next_cursor = int(raw_cursor)
        except (TypeError, ValueError) as exc:
            raise RedisRespError("SCAN returned an invalid cursor") from exc
        if next_cursor < 0 or not isinstance(raw_keys, list):
            raise RedisRespError("SCAN returned an invalid response")
        keys: list[bytes | str] = []
        for key in raw_keys:
            if not isinstance(key, (bytes, str)):
                raise RedisRespError("SCAN returned an invalid stream key")
            keys.append(key)
        return next_cursor, keys

    def xtrim_maxlen(self, key: str | bytes, max_entries: int) -> int:
        if not key:
            raise ValueError("Redis stream key must be non-empty")
        if max_entries < 1:
            raise ValueError("Redis stream max_entries must be positive")
        raw = self.execute(
            "XTRIM",
            key,
            "MAXLEN",
            "=",
            max_entries,
        )
        if not isinstance(raw, int) or raw < 0:
            raise RedisRespError("XTRIM returned an invalid response")
        return raw

    def memory_usage(self, key: str | bytes) -> int | None:
        if not key:
            raise ValueError("Redis memory key must be non-empty")
        raw = self.execute("MEMORY", "USAGE", key)
        if raw is None:
            return None
        if not isinstance(raw, int) or raw < 0:
            raise RedisRespError("MEMORY USAGE returned an invalid response")
        return raw

    def memory_info(self) -> tuple[int, int]:
        raw = self.execute("INFO", "memory")
        if not isinstance(raw, (bytes, str)):
            raise RedisRespError("INFO memory returned an invalid response")
        text = raw
        if isinstance(text, bytes):
            try:
                text = text.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RedisRespError(
                    "INFO memory returned invalid UTF-8"
                ) from exc
        fields: dict[str, str] = {}
        for line in text.splitlines():
            if not line or line.startswith("#") or ":" not in line:
                continue
            name, value = line.split(":", 1)
            fields[name] = value
        try:
            used_memory = int(fields["used_memory"])
            maxmemory = int(fields["maxmemory"])
        except (KeyError, ValueError) as exc:
            raise RedisRespError(
                "INFO memory returned invalid memory fields"
            ) from exc
        if used_memory < 0 or maxmemory < 0:
            raise RedisRespError("INFO memory returned negative memory fields")
        return used_memory, maxmemory

    def execute(self, *parts: object) -> object:
        if not parts:
            raise ValueError("Redis command requires at least one part")
        with self._lock:
            for attempt in range(2):
                try:
                    self._ensure_connected()
                    self._write_command(parts)
                    return self._read_response()
                except (EOFError, OSError, ssl.SSLError):
                    self._close_unlocked()
                    if attempt == 1:
                        raise
            raise RedisRespError("Redis command retry exhausted")

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def _ensure_connected(self) -> None:
        if self._socket is not False and self._reader is not False:
            return
        connection = socket.create_connection(
            (self._host, self._port),
            timeout=self._timeout_seconds,
        )
        connection.settimeout(self._timeout_seconds)
        if self._use_tls:
            try:
                context = ssl.create_default_context()
                connection = context.wrap_socket(
                    connection,
                    server_hostname=self._host,
                )
            except Exception:
                connection.close()
                raise
        self._socket = connection
        self._reader = connection.makefile("rb")
        try:
            if self._password:
                if self._username:
                    self._write_command(("AUTH", self._username, self._password))
                else:
                    self._write_command(("AUTH", self._password))
                self._read_response()
            if self._database:
                self._write_command(("SELECT", self._database))
                self._read_response()
        except Exception:
            self._close_unlocked()
            raise

    def _write_command(self, parts: Iterable[object]) -> None:
        connection = self._socket
        if connection is False:
            raise RedisRespError("Redis socket is not connected")
        encoded = [_encode_part(part) for part in parts]
        payload = [f"*{len(encoded)}\r\n".encode("ascii")]
        for part in encoded:
            payload.append(f"${len(part)}\r\n".encode("ascii"))
            payload.append(part)
            payload.append(b"\r\n")
        connection.sendall(b"".join(payload))

    def _read_response(self) -> object:
        reader = self._reader
        if reader is False:
            raise RedisRespError("Redis response reader is not connected")
        prefix = reader.read(1)
        if not prefix:
            raise EOFError("Redis closed the connection")
        line = _read_line(reader)
        if prefix == b"+":
            return line.decode("utf-8")
        if prefix == b"-":
            raise RedisRespError(line.decode("utf-8", "replace"))
        if prefix == b":":
            return int(line)
        if prefix == b"$":
            length = int(line)
            if length == -1:
                return None
            payload = reader.read(length)
            terminator = reader.read(2)
            if len(payload) != length or terminator != b"\r\n":
                raise EOFError("Redis bulk response was truncated")
            return payload
        if prefix == b"*":
            length = int(line)
            if length == -1:
                return None
            return [self._read_response() for _index in range(length)]
        raise RedisRespError(f"unsupported Redis RESP prefix: {prefix!r}")

    def _close_unlocked(self) -> None:
        reader = self._reader
        connection = self._socket
        self._reader = False
        self._socket = False
        if reader is not False:
            try:
                reader.close()
            except OSError:
                pass
        if connection is not False:
            try:
                connection.close()
            except OSError:
                pass


def _database_number(path: str) -> int:
    value = path.strip("/")
    if not value:
        return 0
    if not value.isdigit():
        raise ValueError("Redis URL database path must be a non-negative integer")
    return int(value)


def _encode_part(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _read_line(reader: Any) -> bytes:
    line = reader.readline()
    if not line.endswith(b"\r\n"):
        raise EOFError("Redis line response was truncated")
    return line[:-2]
