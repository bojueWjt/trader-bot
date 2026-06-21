"""Runtime consumer for order-management settings.changed events."""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_REPO = Path(__file__).resolve().parents[3]
_CONTROL_PLANE = _REPO / "services" / "control-plane"
if str(_CONTROL_PLANE) not in sys.path:
    sys.path.insert(0, str(_CONTROL_PLANE))

from settings.schema import validate_settings  # noqa: E402


class SettingsConsumer:
    def __init__(self) -> None:
        self.desired_version: int | None = None
        self.effective_version: int | None = None
        self.effective_settings: dict[str, Any] = {}
        self.findings: list[dict[str, Any]] = []

    def apply_event(self, event: Mapping[str, Any]) -> bool:
        if event.get("event_type") != "settings.changed":
            return False
        payload = event.get("payload") or {}
        version = payload.get("version")
        settings = payload.get("settings") or {}
        self.desired_version = int(version) if version is not None else None

        validation = validate_settings(settings)
        if not validation["valid"]:
            self.findings.insert(
                0,
                {
                    "type": "settings_apply_failed",
                    "severity": "critical",
                    "desired_version": self.desired_version,
                    "effective_version": self.effective_version,
                    "errors": validation["errors"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return False
        self.effective_settings = copy.deepcopy(settings)
        self.effective_version = self.desired_version
        return True

    def status(self) -> dict[str, Any]:
        return {
            "desired_version": self.desired_version,
            "effective_version": self.effective_version,
            "settings": copy.deepcopy(self.effective_settings),
            "findings": copy.deepcopy(self.findings),
        }
