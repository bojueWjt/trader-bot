from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import Any

from .health_server import build_health_server
from .node import (
    _stop_background_workers,
    _stop_control_plane_session,
    _stop_redis_runtime_safety,
    build_account_runtime,
    run_startup_readiness_checks,
)

SESSION_TERMINATED_EXIT_CODE = 75


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=os.environ.get("NODE_CONFIG_PATH"),
        help="per-account node config JSON path",
    )
    parser.add_argument(
        "--spool-root",
        default=os.environ.get("NAUTILUS_SPOOL_ROOT", "/var/lib/nautilus-node/spool"),
    )
    parser.add_argument("--health-host", default=os.environ.get("NAUTILUS_HEALTH_HOST", "127.0.0.1"))
    parser.add_argument(
        "--health-port",
        type=int,
        default=int(os.environ.get("NAUTILUS_HEALTH_PORT", "8081")),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="assemble config/components and exit without TradingNode.run()",
    )
    parser.add_argument(
        "--build-trading-node",
        action="store_true",
        help="instantiate host-only Nautilus TradingNode during dry-run",
    )
    args = parser.parse_args(argv)
    if not args.config:
        print("NODE_CONFIG_PATH or --config is required", file=sys.stderr)
        return 2

    runtime: Any = False
    server: Any = False
    try:
        runtime = build_account_runtime(
            Path(args.config),
            spool_root=Path(args.spool_root),
            build_trading_node=args.build_trading_node or not args.dry_run,
        )
        if args.dry_run:
            print(
                "assembled "
                f"account_id={runtime.config.account_id} "
                f"node_id={runtime.config.node_id} "
                f"environment={runtime.config.binance.environment} "
                f"trading_state={runtime.lifecycle.trading_state}"
            )
            return 0

        server = build_health_server(runtime, args.health_host, args.health_port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        run_startup_readiness_checks(runtime)

        node = runtime.trading_node
        if node is None:
            raise RuntimeError("TradingNode was not assembled")
        node.build()
        session = runtime.control_plane_session
        if session is None:
            raise RuntimeError("control-plane session was not assembled")
        session.start()
        return _run_node_until_session_termination(node, session)
    finally:
        active_exception = sys.exc_info()[0] is not None
        try:
            _cleanup_runtime(runtime, server)
        except Exception as cleanup_exc:
            if not active_exception:
                raise
            print(
                f"runtime cleanup failed: {cleanup_exc!r}",
                file=sys.stderr,
                flush=True,
            )


def _cleanup_runtime(runtime: Any, server: Any) -> None:
    errors: list[Exception] = []
    if server is not False:
        _call_cleanup(server, "shutdown", errors)
        _call_cleanup(server, "server_close", errors)

    if runtime is not False:
        writer_cleanup_clean = True
        try:
            _stop_control_plane_session(runtime)
        except Exception as exc:
            errors.append(exc)
            writer_cleanup_clean = False
        node = getattr(runtime, "trading_node", None)
        if node is not None:
            error_count = len(errors)
            _call_cleanup(node, "stop", errors)
            _call_cleanup(node, "dispose", errors)
            if len(errors) != error_count:
                writer_cleanup_clean = False
        try:
            _stop_background_workers(runtime)
        except Exception as exc:
            errors.append(exc)
            writer_cleanup_clean = False
        try:
            _stop_redis_runtime_safety(runtime)
        except Exception as exc:
            errors.append(exc)
            writer_cleanup_clean = False
        guard = getattr(runtime, "namespace_lease_guard", None)
        if guard is not None and writer_cleanup_clean:
            _call_cleanup(guard, "close", errors)
        elif guard is not None:
            errors.append(
                RuntimeError(
                    "namespace lease retained because writer cleanup "
                    "did not complete"
                )
            )

    if errors:
        raise RuntimeError(
            f"runtime cleanup failed with {len(errors)} error(s)"
        ) from errors[0]


def _run_node_until_session_termination(
    node: Any,
    session: Any,
) -> int:
    wait_for_termination = getattr(
        session,
        "wait_for_termination",
        None,
    )
    if not callable(wait_for_termination):
        node.run()
        return 0

    node_finished = threading.Event()
    session_terminated = threading.Event()
    monitor_errors: list[Exception] = []

    def monitor_session() -> None:
        while not node_finished.is_set():
            if not wait_for_termination(timeout=0.05):
                continue
            session_terminated.set()
            if node_finished.is_set():
                return
            try:
                _request_node_stop(node)
            except Exception as exc:
                monitor_errors.append(exc)
            return

    monitor = threading.Thread(
        target=monitor_session,
        name="control-plane-session.termination-monitor",
        daemon=True,
    )
    monitor.start()
    try:
        node.run()
    finally:
        node_finished.set()
        monitor.join(timeout=0.2)
    if monitor_errors:
        raise RuntimeError(
            "failed to request node stop after control-plane "
            "session termination"
        ) from monitor_errors[0]
    terminated = session_terminated.is_set()
    if not terminated:
        terminated = bool(wait_for_termination(timeout=0))
    if terminated:
        return SESSION_TERMINATED_EXIT_CODE
    return 0


def _request_node_stop(node: Any) -> None:
    stop = getattr(node, "stop", None)
    kernel = getattr(node, "kernel", None)
    loop = getattr(kernel, "loop", None)
    is_running = getattr(loop, "is_running", None)
    call_soon_threadsafe = getattr(
        loop,
        "call_soon_threadsafe",
        None,
    )
    create_task = getattr(loop, "create_task", None)
    stop_async = getattr(node, "stop_async", None)
    loop_running = False
    if callable(is_running):
        try:
            loop_running = bool(is_running())
        except Exception:
            loop_running = False
    if (
        loop_running
        and callable(call_soon_threadsafe)
        and callable(create_task)
        and callable(stop_async)
    ):
        call_soon_threadsafe(
            lambda: create_task(stop_async())
        )
        return
    if callable(stop):
        stop()


def _call_cleanup(
    target: Any,
    method_name: str,
    errors: list[Exception],
) -> None:
    method = getattr(target, method_name, None)
    if not callable(method):
        return
    try:
        method()
    except Exception as exc:
        errors.append(exc)


if __name__ == "__main__":
    raise SystemExit(main())
