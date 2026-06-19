from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any


def normalize_signal(raw_signal: Any) -> dict[str, Any]:
    if isinstance(raw_signal, dict):
        return dict(raw_signal)
    if is_dataclass(raw_signal):
        return asdict(raw_signal)
    if hasattr(raw_signal, "__dict__"):
        return {
            key: value
            for key, value in vars(raw_signal).items()
            if not key.startswith("_")
        }
    return {}


def signal_id(signal: dict[str, Any]) -> str:
    for key in ["signal_id", "id"]:
        value = signal.get(key)
        if value:
            return str(value)
    return ""


def signal_pair(signal: dict[str, Any]) -> str:
    for key in ["pair_freqtrade", "pair"]:
        value = signal.get(key)
        if value:
            return str(value)
    return ""


def signal_status(signal: dict[str, Any]) -> str:
    value = signal.get("status")
    if isinstance(value, Enum):
        return str(value.value)
    if value:
        return str(value)
    return ""
