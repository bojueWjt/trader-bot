from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = REPO_ROOT / "services" / "control-plane" / "api"
UNIT = REPO_ROOT / "infra" / "systemd" / "trader-v3-controlplane.service"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _graceful_timeout_seconds() -> int:
    text = UNIT.read_text(encoding="utf-8")
    marker = "--timeout-graceful-shutdown "
    start = text.index(marker) + len(marker)
    value = text[start:].split()[0]
    return int(value)


def _wait_until_ready(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 10
    url = f"http://127.0.0.1:{port}/openapi.json"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"uvicorn exited before readiness: {process.returncode}"
            )
        try:
            with urllib.request.urlopen(url, timeout=0.2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("uvicorn did not become ready")


def _open_long_lived_stream(port: int) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    request = (
        "GET /v1/stream HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Authorization: Bearer observer-token\r\n"
        "Accept: text/event-stream\r\n"
        "Connection: keep-alive\r\n"
        "\r\n"
    )
    client.sendall(request.encode("ascii"))
    client.settimeout(2)
    headers = bytearray()
    while b"\r\n\r\n" not in headers:
        chunk = client.recv(1)
        if not chunk:
            break
        headers.extend(chunk)
    assert b"HTTP/1.1 200 OK" in headers
    return client


def test_long_lived_sse_exits_within_uvicorn_shutdown_bound(
    tmp_path: Path,
) -> None:
    graceful_timeout = _graceful_timeout_seconds()
    assert graceful_timeout == 10
    port = _free_port()
    log_path = tmp_path / "uvicorn.log"
    environment = os.environ.copy()
    environment["SYSTEM_OBSERVER_TOKEN"] = "observer-token"
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "read_api:app",
        "--app-dir",
        str(API_DIR),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--timeout-graceful-shutdown",
        str(graceful_timeout),
    ]

    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        client = False
        try:
            _wait_until_ready(process, port)
            client = _open_long_lived_stream(port)
            started_at = time.monotonic()
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=12)
            elapsed = time.monotonic() - started_at

            assert process.returncode in (0, -signal.SIGTERM)
            assert elapsed < 12
            log.flush()
            output = log_path.read_text(encoding="utf-8")
            assert "Finished server process" in output
        finally:
            if client:
                client.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
