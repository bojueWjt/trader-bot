from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

from strategy.intent_execution_planner import encode_client_order_id


COMMAND_TYPES = frozenset(
    {"halt", "resume", "set_reducing", "cancel_all", "close_all"}
)


class RoutingError(ValueError):
    """Base class for fail-closed multi-account routing errors."""


class UnknownAccountError(RoutingError):
    """The intent or command references an account outside this route table."""


class WrongAccountError(RoutingError):
    """The current node is not the target node for the routed account."""


class RouteConflictError(RoutingError):
    """Two accounts attempted to share a mutable routing namespace."""


@dataclass(frozen=True)
class ClientOrderRef:
    """B-03 venue id plus the account namespace used by routing/projection."""

    account_id: str
    node_id: str
    client_order_id: str
    namespace_key: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class AccountRoute:
    """All mutable namespaces owned by one account/node process."""

    account_id: str
    node_id: str
    redis_key_prefix: str
    control_plane_base_url: str
    spool_root: str | Path
    environment: str = "testnet"
    initial_trading_state: str = "HALTED"
    spool_file_name: str = "execution-events.json"
    _spool_root_path: Path = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in (
            "account_id",
            "node_id",
            "redis_key_prefix",
            "control_plane_base_url",
            "environment",
            "initial_trading_state",
            "spool_file_name",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise RoutingError(f"{name} must be a non-empty string")
        expected_prefix = f"nautilus:{self.account_id}:"
        if not self.redis_key_prefix.startswith(expected_prefix):
            raise RoutingError(
                "redis_key_prefix must start with "
                f"{expected_prefix!r} for account isolation"
            )
        object.__setattr__(self, "_spool_root_path", Path(self.spool_root))

    @property
    def spool_path(self) -> Path:
        return (
            self._spool_root_path
            / _namespace_part(self.account_id)
            / _namespace_part(self.node_id)
            / self.spool_file_name
        )

    def redis_key(self, *parts: object) -> str:
        if not parts:
            raise RoutingError("redis key requires at least one suffix part")
        suffix = ":".join(_namespace_part(str(part)) for part in parts)
        return f"{self.redis_key_prefix.rstrip(':')}:{suffix}"

    def client_order_ref(self, intent_id: UUID | str, sequence: int = 1) -> ClientOrderRef:
        parsed_intent_id = intent_id if isinstance(intent_id, UUID) else UUID(str(intent_id))
        client_order_id = encode_client_order_id(parsed_intent_id, sequence=sequence)
        namespace_key = ":".join(
            (
                "client-order-id",
                _namespace_part(self.account_id),
                client_order_id,
            )
        )
        return ClientOrderRef(
            account_id=self.account_id,
            node_id=self.node_id,
            client_order_id=client_order_id,
            namespace_key=namespace_key,
            tags=(
                f"account_id={self.account_id}",
                f"node_id={self.node_id}",
                f"client_order_id={client_order_id}",
            ),
        )

    def event_stream(self, event_type: str) -> str:
        return ":".join(
            (
                "execution-events",
                _namespace_part(self.account_id),
                _namespace_part(self.node_id),
                _namespace_part(event_type),
            )
        )

    def event_route_key(self, event_id: str) -> str:
        return ":".join(
            (
                "execution-event",
                _namespace_part(self.account_id),
                _namespace_part(self.node_id),
                _namespace_part(event_id),
            )
        )


@dataclass(frozen=True)
class CommandRoute:
    command_type: str
    node_ids: tuple[str, ...]
    account_ids: tuple[str, ...]


class MultiAccountRouteTable:
    """Fail-closed account -> node routing table for multi-account deployments."""

    def __init__(self, routes: Iterable[AccountRoute]) -> None:
        self._routes_by_account: dict[str, AccountRoute] = {}
        self._account_by_node: dict[str, str] = {}
        self._node_status: dict[str, str] = {}
        for route in routes:
            self.upsert_route(route)

    def route_for_account(self, account_id: str) -> AccountRoute:
        try:
            return self._routes_by_account[account_id]
        except KeyError as exc:
            raise UnknownAccountError(f"unknown account_id {account_id!r}") from exc

    def route_for_node(self, node_id: str) -> AccountRoute:
        try:
            account_id = self._account_by_node[node_id]
        except KeyError as exc:
            raise UnknownAccountError(f"unknown node_id {node_id!r}") from exc
        return self._routes_by_account[account_id]

    def route_intent(self, intent: Any) -> AccountRoute:
        account_id = _account_id_from_intent(intent)
        if account_id is None:
            raise UnknownAccountError("intent missing account_id")
        return self.route_for_account(account_id)

    def assert_intent_for_node(self, intent: Any, node_id: str) -> AccountRoute:
        route = self.route_intent(intent)
        if route.node_id != node_id:
            raise WrongAccountError(
                f"intent account_id {route.account_id!r} targets {route.node_id!r}, "
                f"not {node_id!r}"
            )
        return route

    def upsert_route(self, route: AccountRoute) -> None:
        old_route = self._routes_by_account.get(route.account_id)
        candidates = [
            candidate
            for account_id, candidate in self._routes_by_account.items()
            if account_id != route.account_id
        ]
        _assert_no_conflicts([*candidates, route])

        if old_route is not None and old_route.node_id != route.node_id:
            self._account_by_node.pop(old_route.node_id, None)
            self._node_status.pop(old_route.node_id, None)
        self._routes_by_account[route.account_id] = route
        self._account_by_node[route.node_id] = route.account_id
        self._node_status.setdefault(route.node_id, "running")

    def mark_node_unavailable(self, node_id: str, reason: str = "") -> None:
        del reason
        self.route_for_node(node_id)
        self._node_status[node_id] = "unavailable"

    def node_status(self, node_id: str) -> str:
        self.route_for_node(node_id)
        return self._node_status.get(node_id, "running")

    def route_command(
        self,
        command_type: str,
        *,
        account_id: str | None = None,
        node_id: str | None = None,
        all_nodes: bool = False,
    ) -> CommandRoute:
        if command_type not in COMMAND_TYPES:
            raise RoutingError(f"unsupported command type {command_type!r}")
        target_count = sum(
            (
                account_id is not None,
                node_id is not None,
                all_nodes,
            )
        )
        if target_count != 1:
            raise RoutingError("command target must be exactly one account, node, or all")
        if all_nodes:
            routes = tuple(self._routes_by_account.values())
        elif account_id is not None:
            routes = (self.route_for_account(account_id),)
        else:
            assert node_id is not None
            routes = (self.route_for_node(node_id),)
        return CommandRoute(
            command_type=command_type,
            node_ids=tuple(route.node_id for route in routes),
            account_ids=tuple(route.account_id for route in routes),
        )


def _assert_no_conflicts(routes: list[AccountRoute]) -> None:
    seen_accounts: set[str] = set()
    seen_nodes: set[str] = set()
    seen_redis_prefixes: set[str] = set()
    seen_spools: set[Path] = set()
    for route in routes:
        if route.account_id in seen_accounts:
            raise RouteConflictError(f"duplicate account_id {route.account_id!r}")
        if route.node_id in seen_nodes:
            raise RouteConflictError(f"duplicate node_id {route.node_id!r}")
        redis_prefix = route.redis_key_prefix.rstrip(":")
        if redis_prefix in seen_redis_prefixes:
            raise RouteConflictError(f"shared redis_key_prefix {redis_prefix!r}")
        spool_path = route.spool_path
        if spool_path in seen_spools:
            raise RouteConflictError(f"shared spool_path {str(spool_path)!r}")
        seen_accounts.add(route.account_id)
        seen_nodes.add(route.node_id)
        seen_redis_prefixes.add(redis_prefix)
        seen_spools.add(spool_path)


def _account_id_from_intent(intent: Any) -> str | None:
    if isinstance(intent, dict):
        value = intent.get("account_id")
    else:
        value = getattr(intent, "account_id", None)
    if value is None:
        return None
    text = str(value)
    return text or None


def _namespace_part(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise RoutingError("namespace parts must be non-empty")
    return stripped.replace(":", "_").replace("/", "_")
