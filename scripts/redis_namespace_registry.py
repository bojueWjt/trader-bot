#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NAUTILUS_NODE_ROOT = REPO_ROOT / "services" / "nautilus-node"
if str(NAUTILUS_NODE_ROOT) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_NODE_ROOT))

from persistence.redis_namespace_lease import (
    DEFAULT_REDIS_NAMESPACE_LEASE_MAX_AGE_SECONDS,
    DEFAULT_REDIS_NAMESPACE_REGISTRY_KEY,
    RedisNamespaceLeaseError,
    RedisNamespaceLeaseRecord,
    acquire_namespace_lease,
    force_remove_stale_namespace_lease,
    get_namespace_lease,
    list_active_namespace_leases,
    redis_server_time_epoch,
    refresh_namespace_lease,
    remove_namespace_lease,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintain fenced Redis namespace leases.",
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL"),
        help="Redis URL; defaults to REDIS_URL and is never included in output.",
    )
    parser.add_argument(
        "--registry-key",
        default=DEFAULT_REDIS_NAMESPACE_REGISTRY_KEY,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register")
    register.add_argument("namespace")
    register.add_argument("--owner", default=_default_owner())
    register.add_argument("--release-id", default=_default_release_id())
    register.add_argument(
        "--max-age-seconds",
        type=int,
        default=DEFAULT_REDIS_NAMESPACE_LEASE_MAX_AGE_SECONDS,
    )

    refresh = subparsers.add_parser("refresh")
    refresh.add_argument("namespace")
    refresh.add_argument("--owner", required=True)
    refresh.add_argument("--release-id", required=True)
    refresh.add_argument("--fencing-token", type=int, required=True)

    remove = subparsers.add_parser("remove")
    remove.add_argument("namespace")
    remove.add_argument("--owner", required=True)
    remove.add_argument("--release-id", required=True)
    remove.add_argument("--fencing-token", type=int, required=True)

    force_remove_stale = subparsers.add_parser("force-remove-stale")
    force_remove_stale.add_argument("namespace")
    force_remove_stale.add_argument(
        "--max-age-seconds",
        type=int,
        default=DEFAULT_REDIS_NAMESPACE_LEASE_MAX_AGE_SECONDS,
    )

    list_active = subparsers.add_parser("list")
    list_active.add_argument(
        "--max-age-seconds",
        type=int,
        default=DEFAULT_REDIS_NAMESPACE_LEASE_MAX_AGE_SECONDS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.redis_url:
        print("redis URL missing; set REDIS_URL or pass --redis-url", file=sys.stderr)
        return 2

    try:
        import redis
    except ImportError:
        print("redis-py is required", file=sys.stderr)
        return 2

    try:
        client = redis.Redis.from_url(args.redis_url, decode_responses=True)
        if args.command == "register":
            record = acquire_namespace_lease(
                client,
                namespace=args.namespace,
                owner=args.owner,
                release_id=args.release_id,
                registry_key=args.registry_key,
                max_age_seconds=args.max_age_seconds,
            )
            print(json.dumps({"lease": asdict(record)}, sort_keys=True))
            return 0
        if args.command == "refresh":
            record = _current_record(client, args)
            refreshed = refresh_namespace_lease(
                client,
                record=record,
                registry_key=args.registry_key,
            )
            print(json.dumps({"lease": asdict(refreshed)}, sort_keys=True))
            return 0
        if args.command == "remove":
            removed = _remove(client, args)
            print(
                json.dumps(
                    {
                        "namespace": _namespace(args.namespace),
                        "removed": removed,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "force-remove-stale":
            removed = force_remove_stale_namespace_lease(
                client,
                args.namespace,
                registry_key=args.registry_key,
                max_age_seconds=args.max_age_seconds,
            )
            print(
                json.dumps(
                    {
                        "namespace": _namespace(args.namespace),
                        "removed": removed,
                    },
                    sort_keys=True,
                )
            )
            return 0

        if args.max_age_seconds <= 0:
            raise RedisNamespaceLeaseError(
                "max-age-seconds must be positive"
            )
        now = redis_server_time_epoch(client)
        records = list_active_namespace_leases(
            client,
            registry_key=args.registry_key,
            fresh_after_epoch=now - args.max_age_seconds,
        )
        print(
            json.dumps(
                {
                    "active_namespaces": [
                        record.namespace for record in records
                    ],
                    "leases": [asdict(record) for record in records],
                },
                sort_keys=True,
            )
        )
        return 0
    except (RedisNamespaceLeaseError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001
        print("Redis namespace registry operation failed", file=sys.stderr)
        return 1


def _remove(client, args: argparse.Namespace) -> bool:
    record = _current_record(client, args)
    return remove_namespace_lease(
        client,
        record=record,
        registry_key=args.registry_key,
    )


def _current_record(
    client,
    args: argparse.Namespace,
) -> RedisNamespaceLeaseRecord:
    namespace = _namespace(args.namespace)
    record = get_namespace_lease(
        client,
        namespace,
        registry_key=args.registry_key,
    )
    if record is False:
        raise RedisNamespaceLeaseError(
            "namespace lease identity is missing"
        )
    expected = (
        str(args.owner),
        str(args.release_id),
        int(args.fencing_token),
    )
    actual = (
        record.owner,
        record.release_id,
        record.fencing_token,
    )
    if actual != expected:
        raise RedisNamespaceLeaseError(
            "namespace lease identity does not match current owner"
        )
    return record


def _namespace(value: str) -> str:
    namespace = value.strip().rstrip(":")
    if not namespace:
        raise ValueError("namespace must be non-empty")
    return namespace


def _default_owner() -> str:
    configured = os.environ.get("REDIS_NAMESPACE_OWNER")
    if configured:
        return configured
    return f"{socket.gethostname()}:{os.getpid()}"


def _default_release_id() -> str:
    configured = os.environ.get("TRADER_RELEASE_ID")
    if configured:
        return configured
    return "manual-registration"


if __name__ == "__main__":
    raise SystemExit(main())
