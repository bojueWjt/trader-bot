from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import psycopg2


class PoolConfigurationError(ValueError):
    pass


class PoolCheckoutTimeout(TimeoutError):
    pass


@dataclass(frozen=True)
class PostgresPoolConfig:
    role: str
    max_size: int
    checkout_timeout_seconds: float
    connect_timeout_seconds: float
    statement_timeout_ms: int

    def __post_init__(self) -> None:
        if not _is_supported_role(self.role):
            raise PoolConfigurationError("pool role is invalid")
        if self.max_size < 1 or self.max_size > 64:
            raise PoolConfigurationError("pool max_size must be between 1 and 64")
        if (
            not math.isfinite(self.checkout_timeout_seconds)
            or self.checkout_timeout_seconds < 0.01
            or self.checkout_timeout_seconds > 30.0
        ):
            raise PoolConfigurationError(
                "pool checkout timeout must be between 0.01 and 30 seconds"
            )
        if (
            not math.isfinite(self.connect_timeout_seconds)
            or self.connect_timeout_seconds <= 0
            or self.connect_timeout_seconds > self.checkout_timeout_seconds
        ):
            raise PoolConfigurationError(
                "pool connect timeout must be positive and no greater than "
                "the checkout timeout"
            )
        if self.statement_timeout_ms < 1 or self.statement_timeout_ms > 120_000:
            raise PoolConfigurationError(
                "statement timeout must be between 1 and 120000 milliseconds"
            )


_ROLE_DEFAULTS = {
    "all": (12, 1.0, 1.0, 10_000),
    "node-control": (4, 0.25, 0.25, 2_000),
    "event-ingest": (6, 0.5, 0.5, 5_000),
    "operator-query": (4, 0.5, 0.5, 3_000),
}


def _is_supported_role(value: str) -> bool:
    return value in _ROLE_DEFAULTS


def _role_name(role: Any) -> str:
    value = getattr(role, "value", role)
    normalized = str(value or "").strip().lower().replace("_", "-")
    if not _is_supported_role(normalized):
        raise PoolConfigurationError(f"unsupported pool role: {normalized}")
    return normalized


def _env_prefix(role: str) -> str:
    return "CONTROL_PLANE_" + role.upper().replace("-", "_") + "_DB_"


def _bounded_int(
    env_name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise PoolConfigurationError(f"{env_name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise PoolConfigurationError(
            f"{env_name} must be between {minimum} and {maximum}"
        )
    return value


def _bounded_float(
    env_name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise PoolConfigurationError(f"{env_name} must be numeric") from exc
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise PoolConfigurationError(
            f"{env_name} must be between {minimum} and {maximum}"
        )
    return value


def pool_config_for_role(role: Any) -> PostgresPoolConfig:
    role_name = _role_name(role)
    (
        default_size,
        default_checkout,
        default_connect,
        default_statement,
    ) = _ROLE_DEFAULTS[role_name]
    prefix = _env_prefix(role_name)
    checkout_timeout_seconds = _bounded_float(
        prefix + "CHECKOUT_TIMEOUT_SECONDS",
        default_checkout,
        minimum=0.01,
        maximum=30.0,
    )
    return PostgresPoolConfig(
        role=role_name,
        max_size=_bounded_int(
            prefix + "POOL_SIZE",
            default_size,
            minimum=1,
            maximum=64,
        ),
        checkout_timeout_seconds=checkout_timeout_seconds,
        connect_timeout_seconds=_bounded_float(
            prefix + "CONNECT_TIMEOUT_SECONDS",
            min(default_connect, checkout_timeout_seconds),
            minimum=0.0,
            maximum=checkout_timeout_seconds,
        ),
        statement_timeout_ms=_bounded_int(
            prefix + "STATEMENT_TIMEOUT_MS",
            default_statement,
            minimum=1,
            maximum=120_000,
        ),
    )


class PooledConnectionLease:
    def __init__(self, pool: "BoundedPostgresPool", connection: Any) -> None:
        self._pool = pool
        self._connection = connection
        self._returned = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def __enter__(self) -> "PooledConnectionLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self.close()
        return False

    def close(self) -> None:
        if self._returned:
            return
        self._returned = True
        self._pool.release(self._connection)


class BoundedPostgresPool:
    def __init__(
        self,
        database_url: str,
        config: PostgresPoolConfig,
        *,
        connect: Callable[..., Any] = psycopg2.connect,
    ) -> None:
        normalized_url = str(database_url or "").strip()
        if not normalized_url:
            raise PoolConfigurationError("database_url is required")
        self._database_url = normalized_url
        self.config = config
        self._connect = connect
        self._condition = threading.Condition()
        self._idle: list[Any] = []
        self._created = 0
        self._closed = False

    @property
    def checked_out(self) -> int:
        with self._condition:
            return self._created - len(self._idle)

    def checkout(self) -> PooledConnectionLease:
        deadline = time.monotonic() + self.config.checkout_timeout_seconds
        connection = self._reserve_connection(deadline)
        if connection is None:
            connection = self._create_connection(deadline)
        return PooledConnectionLease(self, connection)

    def _reserve_connection(self, deadline: float) -> Any | None:
        with self._condition:
            while True:
                if self._closed:
                    raise RuntimeError(
                        f"{self.config.role} PostgreSQL pool is closed"
                    )
                while self._idle:
                    connection = self._idle.pop()
                    if not _connection_closed(connection):
                        return connection
                    self._created -= 1
                if self._created < self.config.max_size:
                    self._created += 1
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PoolCheckoutTimeout(
                        f"{self.config.role} PostgreSQL pool checkout timed out"
                    )
                self._condition.wait(timeout=remaining)

    def _create_connection(self, deadline: float) -> Any:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            self._release_creation_slot()
            raise PoolCheckoutTimeout(
                f"{self.config.role} PostgreSQL pool checkout timed out"
            )
        connect_budget_seconds = min(
            self.config.connect_timeout_seconds,
            remaining_seconds,
        )
        connect_timeout_seconds = max(1, math.ceil(connect_budget_seconds))
        attempt_lock = threading.Lock()
        attempt_done = threading.Event()
        attempt: dict[str, Any] = {
            "state": "pending",
            "connection": False,
            "error": False,
        }

        def connect_in_background() -> None:
            connection: Any = False
            error: BaseException | bool = False
            try:
                connection = self._connect(
                    self._database_url,
                    application_name=(
                        f"trader-control-plane:{self.config.role}"
                    ),
                    connect_timeout=connect_timeout_seconds,
                    options=(
                        "-c statement_timeout="
                        f"{self.config.statement_timeout_ms}"
                    ),
                )
            except BaseException as exc:
                error = exc

            with attempt_lock:
                if attempt["state"] == "pending":
                    attempt["state"] = "completed"
                    attempt["connection"] = connection
                    attempt["error"] = error
                    attempt_done.set()
                    return

            if connection is not False:
                _close_connection(connection)
            self._release_creation_slot()

        worker = threading.Thread(
            target=connect_in_background,
            name=f"postgres-connect:{self.config.role}",
            daemon=True,
        )
        try:
            worker.start()
        except BaseException:
            self._release_creation_slot()
            raise

        wait_budget_seconds = deadline - time.monotonic()
        if wait_budget_seconds <= 0:
            caller_owns_result = self._abandon_connection_attempt(
                attempt,
                attempt_lock,
            )
            if caller_owns_result:
                connection = attempt["connection"]
                if connection is not False:
                    _close_connection(connection)
                self._release_creation_slot()
            raise PoolCheckoutTimeout(
                f"{self.config.role} PostgreSQL pool checkout timed out "
                "while establishing a connection"
            )

        try:
            completed_in_time = attempt_done.wait(timeout=wait_budget_seconds)
        except BaseException:
            caller_owns_result = self._abandon_connection_attempt(
                attempt,
                attempt_lock,
            )
            if caller_owns_result:
                connection = attempt["connection"]
                if connection is not False:
                    _close_connection(connection)
                self._release_creation_slot()
            raise

        if not completed_in_time:
            caller_owns_result = self._abandon_connection_attempt(
                attempt,
                attempt_lock,
            )
            if caller_owns_result:
                connection = attempt["connection"]
                if connection is not False:
                    _close_connection(connection)
                self._release_creation_slot()
            raise PoolCheckoutTimeout(
                f"{self.config.role} PostgreSQL pool checkout timed out "
                "while establishing a connection"
            )

        connection = attempt["connection"]
        error = attempt["error"]
        if error is not False:
            self._release_creation_slot()
            raise error
        if time.monotonic() > deadline:
            if connection is not False:
                _close_connection(connection)
            self._release_creation_slot()
            raise PoolCheckoutTimeout(
                f"{self.config.role} PostgreSQL pool checkout timed out "
                "while establishing a connection"
            )
        return connection

    @staticmethod
    def _abandon_connection_attempt(
        attempt: dict[str, Any],
        attempt_lock: threading.Lock,
    ) -> bool:
        with attempt_lock:
            if attempt["state"] == "pending":
                attempt["state"] = "abandoned"
                return False
            return True

    def _release_creation_slot(self) -> None:
        with self._condition:
            self._created -= 1
            self._condition.notify()

    def release(self, connection: Any) -> None:
        healthy = not _connection_closed(connection)
        if healthy:
            try:
                connection.rollback()
            except Exception:
                healthy = False
        with self._condition:
            if healthy and not self._closed:
                self._idle.append(connection)
            else:
                _close_connection(connection)
                self._created -= 1
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            idle = list(self._idle)
            self._idle.clear()
            self._created -= len(idle)
            self._condition.notify_all()
        for connection in idle:
            _close_connection(connection)


def _connection_closed(connection: Any) -> bool:
    return bool(getattr(connection, "closed", False))


def _close_connection(connection: Any) -> None:
    try:
        connection.close()
    except Exception:
        return


_POOL_REGISTRY: dict[str, BoundedPostgresPool] = {}
_POOL_REGISTRY_LOCK = threading.Lock()


def checkout_role_connection(
    database_url: str,
    role: Any,
) -> PooledConnectionLease:
    role_name = _role_name(role)
    config = pool_config_for_role(role_name)
    registry_key = "|".join(
        (
            role_name,
            database_url,
            str(config.max_size),
            str(config.checkout_timeout_seconds),
            str(config.connect_timeout_seconds),
            str(config.statement_timeout_ms),
        )
    )
    with _POOL_REGISTRY_LOCK:
        pool = _POOL_REGISTRY.get(registry_key)
        if pool is None:
            pool = BoundedPostgresPool(database_url, config)
            _POOL_REGISTRY[registry_key] = pool
    return pool.checkout()


def close_role_pools(role: Any) -> None:
    role_name = _role_name(role)
    prefix = role_name + "|"
    with _POOL_REGISTRY_LOCK:
        keys = [
            key
            for key in _POOL_REGISTRY
            if key.startswith(prefix)
        ]
        pools = [_POOL_REGISTRY.pop(key) for key in keys]
    for pool in pools:
        pool.close()
