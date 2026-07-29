from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_NAUTILUS_NODE = _REPO / "services" / "nautilus-node"
if str(_NAUTILUS_NODE) not in sys.path:
    sys.path.insert(0, str(_NAUTILUS_NODE))

from observability import (  # noqa: E402
    MetricsRegistry,
    TraceContext,
    default_registry,
    derive_trace_id,
    emit_structured_log,
    inject_trace_context,
    structured_log_line,
    trace_context,
)

__all__ = [
    "MetricsRegistry",
    "TraceContext",
    "default_registry",
    "derive_trace_id",
    "emit_structured_log",
    "inject_trace_context",
    "structured_log_line",
    "trace_context",
]
