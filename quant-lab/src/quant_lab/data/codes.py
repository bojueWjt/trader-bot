"""契约 §9.5 要求的公共路径 `quant_lab.data.codes.ReasonCode`：纯别名，唯一真身在 `reasons.py`（G0 2026-09-11 指令：只保留一套）。"""
from __future__ import annotations

from .reasons import FATAL as FATAL_REASONS
from .reasons import PRIMARY_PRIORITY
from .reasons import Reason as ReasonCode
from .reasons import primary_reason, severity, severity_of_all

__all__ = ["ReasonCode", "FATAL_REASONS", "PRIMARY_PRIORITY", "primary_reason", "severity", "severity_of_all"]
