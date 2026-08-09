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
    # Nautilus lifecycle: build the data/exec clients from the registered adapter
    # factories before starting. Host-verify gap: the live path skipped node.build(),
    # so node.run() raised "clients have not been built".
    node.build()
    node.run()
    return 0


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
