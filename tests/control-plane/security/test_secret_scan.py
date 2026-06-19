from __future__ import annotations

import re
from pathlib import Path

from security.audit import redact_payload


REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_PATHS = [
    REPO_ROOT / "services" / "control-plane" / "security",
    REPO_ROOT / "bridge" / "apps" / "api" / "app" / "security",
    REPO_ROOT / "bridge" / "apps" / "api" / "app" / "dependencies.py",
]


def _source_text() -> str:
    chunks: list[str] = []
    for path in SOURCE_PATHS:
        if path.is_file():
            chunks.append(path.read_text())
            continue
        if path.is_dir():
            for source_file in sorted(path.rglob("*.py")):
                chunks.append(source_file.read_text())
    return "\n".join(chunks)


def test_no_test_token_fallbacks_in_a07_sources() -> None:
    assert re.search(r"test-[a-z0-9-]*-token", _source_text()) is None


def test_audit_payload_redacts_exchange_keys_and_secret_values() -> None:
    redacted = redact_payload(
        {
            "exchange_key": "unit-sensitive-value",
            "nested": {"api_secret": "unit-sensitive-value"},
            "message": "token=unit-sensitive-value",
        }
    )

    assert redacted["exchange_key"] == "[REDACTED]"
    assert redacted["nested"]["api_secret"] == "[REDACTED]"
    assert "unit-sensitive-value" not in redacted["message"]
