"""Hot-reload and restart-required settings apply planning."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from .schema import field_descriptor


class HotReloadTimeout(TimeoutError):
    pass


def build_apply_plan(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    plan = {"hot_reload": [], "restart_required": [], "auto_restart": False}
    for path in sorted(_flatten(before).keys() | _flatten(after).keys()):
        category, field_name = path.split(".", 1)
        before_value = _flatten(before).get(path)
        after_value = _flatten(after).get(path)
        if before_value == after_value:
            continue
        try:
            apply_mode = field_descriptor(category, field_name)["apply_mode"]
        except KeyError:
            continue
        item = {
            "path": path,
            "before": before_value,
            "after": after_value,
            "apply_mode": apply_mode,
        }
        plan[apply_mode].append(item)
    return plan


class HotReloadAckTracker:
    def __init__(self, *, timeout_seconds: float = 30, poll_interval_seconds: float = 0.25) -> None:
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def wait_for_ack(self, desired_version: int, effective_version_provider: Callable[[], int | None]) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            effective_version = effective_version_provider()
            if effective_version is not None and effective_version >= desired_version:
                return {"acked": True, "effective_version": effective_version}
            if time.monotonic() >= deadline:
                raise HotReloadTimeout(f"settings hot reload timed out for version {desired_version}")
            time.sleep(max(0, self.poll_interval_seconds))


def _flatten(settings: Mapping[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for category, fields in (settings or {}).items():
        if not isinstance(fields, Mapping):
            continue
        for field_name, value in fields.items():
            flattened[f"{category}.{field_name}"] = value
    return flattened
