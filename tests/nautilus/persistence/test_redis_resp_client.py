from __future__ import annotations

import io
import socket
import ssl
import sys
import threading
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import BinaryIO, Self
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from persistence.redis_resp_client import RedisRespClient, RedisRespError

RespCommand = tuple[bytes, ...]
RespResponder = Callable[[int, int, RespCommand], bytes | bool]


class FakeRespServer(AbstractContextManager["FakeRespServer"]):
    def __init__(self, responder: RespResponder) -> None:
        self._responder = responder
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.1)
        host, port = self._listener.getsockname()
        self.host = str(host)
        self.port = int(port)
        self.commands: list[tuple[int, RespCommand]] = []
        self.errors: list[Exception] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise AssertionError("fake Redis server did not stop")
        if self.errors:
            raise self.errors[0]

    def _serve(self) -> None:
        connection_index = 0
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                raise
            try:
                self._serve_connection(connection, connection_index)
            except (AssertionError, OSError, TypeError, ValueError) as exc:
                self.errors.append(exc)
                return
            finally:
                connection.close()
            connection_index += 1

    def _serve_connection(
        self,
        connection: socket.socket,
        connection_index: int,
    ) -> None:
        command_index = 0
        reader = connection.makefile("rb")
        try:
            while not self._stop.is_set():
                command = _read_command(reader)
                if command is False:
                    return
                self.commands.append((connection_index, command))
                response = self._responder(
                    connection_index,
                    command_index,
                    command,
                )
                command_index += 1
                if response is False:
                    return
                if not isinstance(response, bytes):
                    raise TypeError("fake Redis response must be bytes or False")
                connection.sendall(response)
        finally:
            reader.close()


class RedisRespClientTest(unittest.TestCase):
    def test_runtime_safety_commands_use_typed_resp2(self) -> None:
        def respond(
            _connection_index: int,
            _command_index: int,
            command: RespCommand,
        ) -> bytes:
            operation = command[0]
            if operation == b"SCAN":
                return _array_response(
                    (
                        b"17",
                        (b"stream-root:orders", b"stream-root:positions"),
                    )
                )
            if operation == b"XTRIM":
                return b":23\r\n"
            if operation == b"MEMORY":
                if command[2] == b"missing":
                    return b"$-1\r\n"
                return b":4096\r\n"
            if operation == b"INFO":
                return _bulk_response(
                    b"# Memory\r\nused_memory:1258291200\r\n"
                    b"maxmemory:2147483648\r\n"
                )
            raise AssertionError(f"unexpected command: {command!r}")

        with FakeRespServer(respond) as server:
            client = RedisRespClient(
                f"redis://{server.host}:{server.port}/0",
                timeout_seconds=1,
            )

            cursor, streams = client.scan_streams(
                0,
                match="stream-root:*",
                count=500,
            )
            trimmed = client.xtrim_maxlen(
                "stream-root:orders",
                100_000,
            )
            stream_bytes = client.memory_usage("stream-root:orders")
            missing_bytes = client.memory_usage("missing")
            used_memory, maxmemory = client.memory_info()
            client.close()

        self.assertEqual(cursor, 17)
        self.assertEqual(
            streams,
            [b"stream-root:orders", b"stream-root:positions"],
        )
        self.assertEqual(trimmed, 23)
        self.assertEqual(stream_bytes, 4096)
        self.assertIsNone(missing_bytes)
        self.assertEqual(used_memory, 1_258_291_200)
        self.assertEqual(maxmemory, 2_147_483_648)
        self.assertEqual(
            [command for _connection, command in server.commands],
            [
                (
                    b"SCAN",
                    b"0",
                    b"MATCH",
                    b"stream-root:*",
                    b"COUNT",
                    b"500",
                    b"TYPE",
                    b"stream",
                ),
                (
                    b"XTRIM",
                    b"stream-root:orders",
                    b"MAXLEN",
                    b"=",
                    b"100000",
                ),
                (b"MEMORY", b"USAGE", b"stream-root:orders"),
                (b"MEMORY", b"USAGE", b"missing"),
                (b"INFO", b"memory"),
            ],
        )

    def test_registry_commands_use_resp2(self) -> None:
        def respond(
            _connection_index: int,
            _command_index: int,
            command: RespCommand,
        ) -> bytes:
            operation = command[0]
            if operation in {b"AUTH", b"SELECT"}:
                return b"+OK\r\n"
            if operation == b"EVAL":
                return _array_response((1, 42, b"ACQUIRED"))
            if operation == b"HGET":
                if command[2] == b"missing":
                    return b"$-1\r\n"
                return _bulk_response(b'{"owner":"node-a"}')
            if operation == b"ZRANGEBYSCORE":
                if command[-1] == b"WITHSCORES":
                    return _array_response((b"namespace-a", b"1723100000"))
                return _array_response((b"namespace-a", b"namespace-b"))
            if operation == b"ZSCORE":
                if command[2] == b"missing":
                    return b"$-1\r\n"
                return _bulk_response(b"1723100000")
            if operation == b"TIME":
                return _array_response((b"1723100000", b"123456"))
            raise AssertionError(f"unexpected command: {command!r}")

        with FakeRespServer(respond) as server:
            client = RedisRespClient(
                "redis://lease%2Duser:p%40ss@"
                f"{server.host}:{server.port}/3",
                timeout_seconds=1,
            )

            evaluated = client.eval(
                "return {1, 42, ARGV[1]}",
                1,
                "lease:key",
                "ACQUIRED",
            )
            current = client.hget("lease:metadata", "namespace-a")
            missing = client.hget("lease:metadata", "missing")
            members = client.zrangebyscore(
                "lease:registry",
                0,
                "+inf",
                withscores=False,
            )
            scored_members = client.zrangebyscore(
                "lease:registry",
                0,
                "+inf",
                withscores=True,
            )
            score = client.zscore("lease:registry", "namespace-a")
            missing_score = client.zscore("lease:registry", "missing")
            server_time = client.time()
            client.close()

        self.assertEqual(evaluated, [1, 42, b"ACQUIRED"])
        self.assertEqual(current, b'{"owner":"node-a"}')
        self.assertIsNone(missing)
        self.assertEqual(list(members), [b"namespace-a", b"namespace-b"])
        self.assertEqual(list(scored_members), [(b"namespace-a", 1723100000.0)])
        self.assertEqual(score, 1723100000.0)
        self.assertIsNone(missing_score)
        self.assertEqual(server_time, (1723100000, 123456))
        self.assertEqual(
            [command for _connection, command in server.commands],
            [
                (b"AUTH", b"lease-user", b"p@ss"),
                (b"SELECT", b"3"),
                (
                    b"EVAL",
                    b"return {1, 42, ARGV[1]}",
                    b"1",
                    b"lease:key",
                    b"ACQUIRED",
                ),
                (b"HGET", b"lease:metadata", b"namespace-a"),
                (b"HGET", b"lease:metadata", b"missing"),
                (b"ZRANGEBYSCORE", b"lease:registry", b"0", b"+inf"),
                (
                    b"ZRANGEBYSCORE",
                    b"lease:registry",
                    b"0",
                    b"+inf",
                    b"WITHSCORES",
                ),
                (b"ZSCORE", b"lease:registry", b"namespace-a"),
                (b"ZSCORE", b"lease:registry", b"missing"),
                (b"TIME",),
            ],
        )

    def test_registry_commands_reject_malformed_responses(self) -> None:
        responses = iter(
            (
                _bulk_response(b"invalid"),
                _array_response((b"1723100000",)),
                _array_response((b"1723100000", b"1000000")),
            )
        )

        def respond(
            _connection_index: int,
            _command_index: int,
            _command: RespCommand,
        ) -> bytes:
            return next(responses)

        with FakeRespServer(respond) as server:
            client = RedisRespClient(
                f"redis://{server.host}:{server.port}/0",
                timeout_seconds=1,
            )
            with self.assertRaisesRegex(RedisRespError, "invalid score"):
                client.zscore("lease:registry", "namespace-a")
            with self.assertRaisesRegex(RedisRespError, "invalid response"):
                client.time()
            with self.assertRaisesRegex(RedisRespError, "invalid values"):
                client.time()
            client.close()

    def test_runtime_safety_commands_reject_malformed_responses(self) -> None:
        responses = iter(
            (
                _array_response((b"not-a-cursor", ())),
                _array_response((b"0", (1,))),
                _bulk_response(b"1"),
                _bulk_response(b"4096"),
                _bulk_response(b"used_memory:1000\r\n"),
                _bulk_response(b"\xff"),
            )
        )

        def respond(
            _connection_index: int,
            _command_index: int,
            _command: RespCommand,
        ) -> bytes:
            return next(responses)

        with FakeRespServer(respond) as server:
            client = RedisRespClient(
                f"redis://{server.host}:{server.port}/0",
                timeout_seconds=1,
            )
            with self.assertRaisesRegex(RedisRespError, "invalid cursor"):
                client.scan_streams(
                    0,
                    match="stream-root:*",
                    count=500,
                )
            with self.assertRaisesRegex(RedisRespError, "invalid stream key"):
                client.scan_streams(
                    0,
                    match="stream-root:*",
                    count=500,
                )
            with self.assertRaisesRegex(RedisRespError, "XTRIM"):
                client.xtrim_maxlen("stream-root:orders", 100_000)
            with self.assertRaisesRegex(RedisRespError, "MEMORY USAGE"):
                client.memory_usage("stream-root:orders")
            with self.assertRaisesRegex(RedisRespError, "memory fields"):
                client.memory_info()
            with self.assertRaisesRegex(RedisRespError, "invalid UTF-8"):
                client.memory_info()
            client.close()

    def test_redis_error_reply_is_raised_without_transport_retry(self) -> None:
        def respond(
            _connection_index: int,
            _command_index: int,
            _command: RespCommand,
        ) -> bytes:
            return b"-ERR namespace lease is held\r\n"

        with FakeRespServer(respond) as server:
            client = RedisRespClient(
                f"redis://{server.host}:{server.port}/0",
                timeout_seconds=1,
            )
            with self.assertRaisesRegex(
                RedisRespError,
                "namespace lease is held",
            ):
                client.execute("PING")
            client.close()

        self.assertEqual(
            server.commands,
            [(0, (b"PING",))],
        )

    def test_connection_drop_reconnects_and_retries_command_once(self) -> None:
        def respond(
            connection_index: int,
            _command_index: int,
            command: RespCommand,
        ) -> bytes | bool:
            self.assertEqual(command, (b"PING",))
            if connection_index == 0:
                return False
            return b"+PONG\r\n"

        with FakeRespServer(respond) as server:
            client = RedisRespClient(
                f"redis://{server.host}:{server.port}/0",
                timeout_seconds=1,
            )
            response = client.execute("PING")
            client.close()

        self.assertEqual(response, "PONG")
        self.assertEqual(
            server.commands,
            [
                (0, (b"PING",)),
                (1, (b"PING",)),
            ],
        )

    def test_auth_error_closes_connection_before_next_command(self) -> None:
        def respond(
            connection_index: int,
            command_index: int,
            command: RespCommand,
        ) -> bytes:
            if command_index == 0:
                self.assertEqual(command, (b"AUTH", b"secret"))
                if connection_index == 0:
                    return b"-WRONGPASS invalid credentials\r\n"
                return b"+OK\r\n"
            self.assertEqual(command, (b"PING",))
            return b"+PONG\r\n"

        with FakeRespServer(respond) as server:
            client = RedisRespClient(
                f"redis://:secret@{server.host}:{server.port}/0",
                timeout_seconds=1,
            )
            with self.assertRaisesRegex(RedisRespError, "WRONGPASS"):
                client.execute("PING")
            response = client.execute("PING")
            client.close()

        self.assertEqual(response, "PONG")
        self.assertEqual(
            server.commands,
            [
                (0, (b"AUTH", b"secret")),
                (1, (b"AUTH", b"secret")),
                (1, (b"PING",)),
            ],
        )

    def test_rediss_wraps_socket_with_default_tls_context_and_hostname(self) -> None:
        raw_socket = _FakeSocket()
        tls_socket = _FakeSocket(b"+PONG\r\n")
        context = _FakeTlsContext(tls_socket)

        with (
            patch(
                "persistence.redis_resp_client.socket.create_connection",
                return_value=raw_socket,
            ) as create_connection,
            patch(
                "persistence.redis_resp_client.ssl.create_default_context",
                return_value=context,
            ) as create_default_context,
        ):
            client = RedisRespClient(
                "rediss://redis.internal:6380/0",
                timeout_seconds=1.5,
            )
            response = client.execute("PING")
            client.close()

        self.assertEqual(response, "PONG")
        create_connection.assert_called_once_with(
            ("redis.internal", 6380),
            timeout=1.5,
        )
        create_default_context.assert_called_once_with()
        self.assertEqual(raw_socket.timeouts, [1.5])
        self.assertEqual(
            context.wrap_calls,
            [(raw_socket, "redis.internal")],
        )
        self.assertEqual(
            tls_socket.sent,
            [b"*1\r\n$4\r\nPING\r\n"],
        )
        self.assertTrue(tls_socket.closed)

    def test_rediss_handshake_failure_closes_each_raw_socket(self) -> None:
        raw_sockets = [_FakeSocket(), _FakeSocket()]
        context = _FailingTlsContext()

        with (
            patch(
                "persistence.redis_resp_client.socket.create_connection",
                side_effect=raw_sockets,
            ),
            patch(
                "persistence.redis_resp_client.ssl.create_default_context",
                return_value=context,
            ),
        ):
            client = RedisRespClient(
                "rediss://redis.internal:6380/0",
                timeout_seconds=1,
            )
            with self.assertRaises(ssl.SSLError):
                client.execute("PING")

        self.assertEqual(context.wrap_count, 2)
        self.assertTrue(all(raw_socket.closed for raw_socket in raw_sockets))


class _FakeSocket:
    def __init__(self, response: bytes = b"") -> None:
        self._response = response
        self.timeouts: list[float] = []
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def makefile(self, mode: str) -> BinaryIO:
        if mode != "rb":
            raise AssertionError(f"unexpected makefile mode: {mode}")
        return io.BytesIO(self._response)

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        self.closed = True


class _FakeTlsContext:
    def __init__(self, wrapped_socket: _FakeSocket) -> None:
        self._wrapped_socket = wrapped_socket
        self.wrap_calls: list[tuple[_FakeSocket, str]] = []

    def wrap_socket(
        self,
        connection: _FakeSocket,
        *,
        server_hostname: str,
    ) -> _FakeSocket:
        self.wrap_calls.append((connection, server_hostname))
        return self._wrapped_socket


class _FailingTlsContext:
    def __init__(self) -> None:
        self.wrap_count = 0

    def wrap_socket(
        self,
        connection: _FakeSocket,
        *,
        server_hostname: str,
    ) -> _FakeSocket:
        del connection, server_hostname
        self.wrap_count += 1
        raise ssl.SSLError("handshake failed")


def _read_command(reader: BinaryIO) -> RespCommand | bool:
    header = reader.readline()
    if not header:
        return False
    if not header.startswith(b"*") or not header.endswith(b"\r\n"):
        raise AssertionError(f"invalid RESP array header: {header!r}")
    count = int(header[1:-2])
    parts: list[bytes] = []
    for _index in range(count):
        length_line = reader.readline()
        if not length_line.startswith(b"$") or not length_line.endswith(b"\r\n"):
            raise AssertionError(f"invalid RESP bulk header: {length_line!r}")
        length = int(length_line[1:-2])
        payload = reader.read(length)
        terminator = reader.read(2)
        if len(payload) != length or terminator != b"\r\n":
            raise AssertionError("truncated RESP command")
        parts.append(payload)
    return tuple(parts)


def _bulk_response(value: bytes) -> bytes:
    return b"$" + str(len(value)).encode("ascii") + b"\r\n" + value + b"\r\n"


def _array_response(
    values: tuple[int | bytes | tuple[int | bytes, ...], ...],
) -> bytes:
    response = [b"*" + str(len(values)).encode("ascii") + b"\r\n"]
    for value in values:
        if isinstance(value, int):
            response.append(b":" + str(value).encode("ascii") + b"\r\n")
            continue
        if isinstance(value, tuple):
            response.append(_array_response(value))
            continue
        response.append(_bulk_response(value))
    return b"".join(response)


if __name__ == "__main__":
    unittest.main()
