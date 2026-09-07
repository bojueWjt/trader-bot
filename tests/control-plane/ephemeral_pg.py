"""Ephemeral Postgres for control-plane pytest.

Binds a random free TCP port (no hardcoded 55440) and always stops the
postmaster: fixture finally + atexit + SIGTERM/SIGKILL on leftover pid.
"""

from __future__ import annotations

import atexit
import getpass
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


def find_pg_tool(name: str) -> str:
    for candidate in (
        shutil.which(name),
        f"/opt/homebrew/opt/postgresql@16/bin/{name}",
        f"/usr/local/opt/postgresql@16/bin/{name}",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError(f"{name} not found; install Homebrew postgresql@16")


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _kill_pid(pid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        except OSError:
            return
        time.sleep(0.15)


@dataclass
class EphemeralPostgres:
    url: str
    pg_ctl: str
    data_dir: Path
    socket_dir: Path
    port: int
    _stopped: bool = False

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        subprocess.run(
            [self.pg_ctl, "-D", str(self.data_dir), "-m", "fast", "-w", "stop"],
            text=True,
            capture_output=True,
            check=False,
        )
        pid_file = self.data_dir / "postmaster.pid"
        if pid_file.is_file():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").splitlines()[0].strip())
            except (ValueError, OSError, IndexError):
                pid = 0
            if pid > 1:
                _kill_pid(pid)
        shutil.rmtree(self.data_dir, ignore_errors=True)
        shutil.rmtree(self.socket_dir, ignore_errors=True)


def start_ephemeral_postgres(*, prefix: str = "pg-cp") -> EphemeralPostgres:
    initdb = find_pg_tool("initdb")
    pg_ctl = find_pg_tool("pg_ctl")
    data_dir = Path(tempfile.mkdtemp(prefix=f"{prefix}-data-"))
    socket_dir = Path(tempfile.mkdtemp(prefix=f"{prefix}-sock-"))
    port = free_tcp_port()
    subprocess.run(
        [initdb, "-D", str(data_dir), "-A", "trust", "-U", getpass.getuser()],
        check=True,
        text=True,
        capture_output=True,
    )
    started = subprocess.run(
        [
            pg_ctl,
            "-D",
            str(data_dir),
            "-l",
            str(data_dir / "postgres.log"),
            "-o",
            f"-p {port} -k {socket_dir} -c timezone=UTC -c listen_addresses=127.0.0.1",
            "-w",
            "start",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if started.returncode != 0:
        log = (data_dir / "postgres.log").read_text(encoding="utf-8", errors="replace")
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(socket_dir, ignore_errors=True)
        raise RuntimeError(
            f"pg_ctl start failed rc={started.returncode} port={port}\n"
            f"{started.stderr}\n{log[-2000:]}"
        )
    url = f"postgresql://localhost/postgres?host={socket_dir}&port={port}"
    cluster = EphemeralPostgres(
        url=url,
        pg_ctl=pg_ctl,
        data_dir=data_dir,
        socket_dir=socket_dir,
        port=port,
    )
    atexit.register(cluster.stop)
    return cluster
