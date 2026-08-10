from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping

SCHEMA_VERSION = "trader-v3-runtime-resources/v2"
ABSENT = object()


class RuntimeResourcePolicy(str, Enum):
    LIVE_STRICT = "live_strict"
    COMPAT = "compat"


class RuntimeResourceContractError(ValueError):
    def __init__(self, path: str, code: str, detail: str) -> None:
        self.path = path
        self.code = code
        self.detail = detail
        super().__init__(f"{path} {detail}")


@dataclass(frozen=True)
class CommandJournalResources:
    max_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ControlPlaneSessionResources:
    command_delivery_capacity: int
    command_ack_capacity: int
    intent_delivery_capacity: int
    execution_event_capacity: int
    queue_degraded_ratio: float
    retry_budget: int
    retry_base_delay_seconds: float
    retry_max_delay_seconds: float
    retry_jitter_ratio: float
    circuit_reset_seconds: float
    operation_timeout_seconds: float
    shutdown_timeout_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyDurableIoResources:
    queue_capacity: int
    task_timeout_seconds: float
    shutdown_timeout_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeResources:
    schema_version: str
    command_journal: CommandJournalResources
    control_plane_session: ControlPlaneSessionResources
    strategy_durable_io: StrategyDurableIoResources

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "command_journal": self.command_journal.to_dict(),
            "control_plane_session": self.control_plane_session.to_dict(),
            "strategy_durable_io": self.strategy_durable_io.to_dict(),
        }


COMMAND_JOURNAL_DEFAULTS = {
    "max_bytes": 16 * 1024 * 1024,
}
CONTROL_PLANE_SESSION_DEFAULTS = {
    "command_delivery_capacity": 128,
    "command_ack_capacity": 256,
    "intent_delivery_capacity": 256,
    "execution_event_capacity": 1024,
    "queue_degraded_ratio": 0.8,
    "retry_budget": 3,
    "retry_base_delay_seconds": 0.05,
    "retry_max_delay_seconds": 1.0,
    "retry_jitter_ratio": 0.2,
    "circuit_reset_seconds": 5.0,
    "operation_timeout_seconds": 15.0,
    "shutdown_timeout_seconds": 1.0,
}
STRATEGY_DURABLE_IO_DEFAULTS = {
    "queue_capacity": 128,
    "task_timeout_seconds": 1.0,
    "shutdown_timeout_seconds": 2.0,
}

_ROOT_FIELDS = {
    "schema_version",
    "command_journal",
    "control_plane_session",
    "strategy_durable_io",
}
_COMPAT_REQUIRED_ROOT_FIELDS = {
    "schema_version",
    "command_journal",
}


def parse_runtime_resources(
    raw: Any,
    *,
    policy: RuntimeResourcePolicy,
    legacy_session: Any = ABSENT,
) -> RuntimeResources:
    if not isinstance(policy, RuntimeResourcePolicy):
        raise TypeError("policy must be a RuntimeResourcePolicy")
    source = _root_source(raw, policy)
    strict = policy is RuntimeResourcePolicy.LIVE_STRICT
    required_root = _COMPAT_REQUIRED_ROOT_FIELDS
    if strict:
        required_root = _ROOT_FIELDS
    _validate_fields(
        source,
        path="runtime_resources",
        allowed=_ROOT_FIELDS,
        required=required_root,
    )
    schema_version = source.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        _fail(
            "runtime_resources.schema_version",
            "schema_version",
            f"must equal {SCHEMA_VERSION}",
        )

    command_journal = _parse_command_journal(
        source.get("command_journal"),
    )
    release_session = source.get("control_plane_session", ABSENT)
    session_source = release_session
    session_path = "runtime_resources.control_plane_session"
    if session_source is ABSENT:
        session_source = legacy_session
        if legacy_session is not ABSENT:
            session_path = "control_plane.session"
    control_plane_session = _parse_control_plane_session(
        session_source,
        path=session_path,
        strict=strict and release_session is not ABSENT,
    )
    if release_session is not ABSENT and legacy_session is not ABSENT:
        normalized_legacy = _parse_control_plane_session(
            legacy_session,
            path="control_plane.session",
            strict=False,
        )
        if normalized_legacy != control_plane_session:
            _fail(
                "runtime_resources.control_plane_session",
                "conflict",
                "differs from control_plane.session",
            )
    strategy_durable_io = _parse_strategy_durable_io(
        source.get("strategy_durable_io", ABSENT),
        strict=strict,
    )
    return RuntimeResources(
        schema_version=SCHEMA_VERSION,
        command_journal=command_journal,
        control_plane_session=control_plane_session,
        strategy_durable_io=strategy_durable_io,
    )


def _root_source(
    raw: Any,
    policy: RuntimeResourcePolicy,
) -> Mapping[str, Any]:
    if raw is ABSENT:
        if policy is RuntimeResourcePolicy.LIVE_STRICT:
            _fail(
                "runtime_resources",
                "required",
                "must be release-bound for live nodes",
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "command_journal": dict(COMMAND_JOURNAL_DEFAULTS),
        }
    return _mapping(raw, "runtime_resources")


def _parse_command_journal(
    raw: Any,
) -> CommandJournalResources:
    path = "runtime_resources.command_journal"
    source = _mapping(raw, path)
    required = set(COMMAND_JOURNAL_DEFAULTS)
    _validate_fields(
        source,
        path=path,
        allowed=required,
        required=required,
    )
    return CommandJournalResources(
        max_bytes=_positive_int(
            source["max_bytes"],
            f"{path}.max_bytes",
        )
    )


def _parse_control_plane_session(
    raw: Any,
    *,
    path: str,
    strict: bool,
) -> ControlPlaneSessionResources:
    source = _optional_group_source(
        raw,
        path=path,
        defaults=CONTROL_PLANE_SESSION_DEFAULTS,
        strict=strict,
    )
    parsed: dict[str, int | float] = {}
    for field in (
        "command_delivery_capacity",
        "command_ack_capacity",
        "intent_delivery_capacity",
        "execution_event_capacity",
        "retry_budget",
    ):
        parsed[field] = _positive_int(source[field], f"{path}.{field}")
    for field in (
        "retry_base_delay_seconds",
        "retry_max_delay_seconds",
        "circuit_reset_seconds",
        "operation_timeout_seconds",
        "shutdown_timeout_seconds",
    ):
        parsed[field] = _positive_number(source[field], f"{path}.{field}")
    parsed["queue_degraded_ratio"] = _ratio(
        source["queue_degraded_ratio"],
        f"{path}.queue_degraded_ratio",
        allow_zero=False,
    )
    parsed["retry_jitter_ratio"] = _ratio(
        source["retry_jitter_ratio"],
        f"{path}.retry_jitter_ratio",
        allow_zero=True,
    )
    if (
        parsed["retry_max_delay_seconds"]
        < parsed["retry_base_delay_seconds"]
    ):
        _fail(
            f"{path}.retry_max_delay_seconds",
            "cross_constraint",
            "must be at least retry_base_delay_seconds",
        )
    return ControlPlaneSessionResources(**parsed)


def _parse_strategy_durable_io(
    raw: Any,
    *,
    strict: bool,
) -> StrategyDurableIoResources:
    path = "runtime_resources.strategy_durable_io"
    source = _optional_group_source(
        raw,
        path=path,
        defaults=STRATEGY_DURABLE_IO_DEFAULTS,
        strict=strict,
    )
    return StrategyDurableIoResources(
        queue_capacity=_positive_int(
            source["queue_capacity"],
            f"{path}.queue_capacity",
        ),
        task_timeout_seconds=_positive_number(
            source["task_timeout_seconds"],
            f"{path}.task_timeout_seconds",
        ),
        shutdown_timeout_seconds=_positive_number(
            source["shutdown_timeout_seconds"],
            f"{path}.shutdown_timeout_seconds",
        ),
    )


def _optional_group_source(
    raw: Any,
    *,
    path: str,
    defaults: Mapping[str, int | float],
    strict: bool,
) -> dict[str, Any]:
    if raw is ABSENT:
        if strict:
            _fail(path, "required", "must be explicit")
        return dict(defaults)
    source = _mapping(raw, path)
    required: set[str] = set()
    if strict:
        required = set(defaults)
    _validate_fields(
        source,
        path=path,
        allowed=set(defaults),
        required=required,
    )
    return _with_defaults(source, defaults)


def _mapping(raw: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        _fail(path, "type", "must be an object")
    return raw


def _validate_fields(
    raw: Mapping[str, Any],
    *,
    path: str,
    allowed: set[str],
    required: set[str],
) -> None:
    missing = sorted(required - set(raw))
    if missing:
        _fail(f"{path}.{missing[0]}", "required", "field is required")
    extra = sorted(set(raw) - allowed)
    if extra:
        _fail(f"{path}.{extra[0]}", "unsupported", "field is unsupported")


def _with_defaults(
    source: Mapping[str, Any],
    defaults: Mapping[str, int | float],
) -> dict[str, Any]:
    values = dict(defaults)
    values.update(source)
    return values


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(path, "type", "must be a positive integer")
    return value


def _positive_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "type", "must be a positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        _fail(path, "range", "must be a positive number")
    return number


def _ratio(value: Any, path: str, *, allow_zero: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "type", "must be a ratio")
    ratio = float(value)
    lower_valid = ratio > 0
    if allow_zero:
        lower_valid = ratio >= 0
    if not math.isfinite(ratio) or not lower_valid or ratio >= 1:
        _fail(path, "range", "must be between zero and one")
    return ratio


def _fail(path: str, code: str, detail: str) -> None:
    raise RuntimeResourceContractError(path, code, detail)
