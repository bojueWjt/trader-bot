from __future__ import annotations

import re
from urllib.parse import urlparse


SECRET_PATTERNS = [
    ("api_key", re.compile(r"(?i)(api[_-]?key|exchange[_-]?key|binance[_-]?api[_-]?key)\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{20,})")),
    ("secret", re.compile(r"(?i)(secret|api[_-]?secret|jwt[_-]?secret)\s*[:=]\s*['\"]?([^'\"\s,]{6,})")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("password", re.compile(r"(?i)(password|passwd)\s*[:=]\s*['\"]?([^'\"\s,]{4,})")),
]

REDACTION_PATTERNS = [
    re.compile(r"(?i)(password|passwd|secret|jwt|token|api[_-]?key)\s*[:=]\s*([^,\s]+)"),
]


def scan_text_for_secrets(text: str, source: str) -> dict:
    findings = []
    for secret_type, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                {
                    "source": source,
                    "type": secret_type,
                    "start": match.start(),
                    "end": match.end(),
                }
            )
    return {
        "passed": len(findings) == 0,
        "findings": findings,
    }


def redact_text(text: str) -> str:
    redacted = text
    for pattern in REDACTION_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    return redacted


def check_freqtrade_api_binding(binding: str) -> dict:
    target = binding
    if "://" not in target:
        target = f"//{target}"
    parsed = urlparse(target)
    host = parsed.hostname
    if not host:
        host = binding.split(":")[0].strip("[]")
    public_hosts = {"0.0.0.0", "::", "[::]"}
    passed = host not in public_hosts
    return {
        "passed": passed,
        "binding": binding,
        "reason": "freqtrade_api_publicly_exposed" if not passed else "local_binding",
    }
