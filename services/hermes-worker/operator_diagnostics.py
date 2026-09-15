"""Bounded transport diagnostics, separate from signal interpretation."""

import re
from typing import Any


def safe_operator_detail(value: Any, token: str) -> str:
    detail = value if isinstance(value, str) else "operator request rejected"
    if token:
        detail = detail.replace(token, "[redacted]")
    detail = re.sub(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+", "[redacted-authorization]", detail)
    detail = re.sub(r"(?i)\b(?:token|password|secret|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+", "[redacted-credential]", detail)
    detail = re.sub(r"(?i)https?://\S+", "[redacted-url]", detail)
    detail = re.sub(r"\?\S+", "[redacted-query]", detail)
    return " ".join(detail.split())[:500]
