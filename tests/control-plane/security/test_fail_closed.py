from __future__ import annotations

import pytest

from security import REQUIRED_SECRET_ENV_VARS, validate_required_secrets


def test_missing_required_secret_or_token_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED_SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError) as exc_info:
        validate_required_secrets()

    message = str(exc_info.value)
    assert "missing required security environment variables" in message
    assert "CONTROL_PLANE_AUTH_SECRET" in message
    assert "RISK_ADMIN_TOKEN" in message


def test_configured_required_secrets_allow_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED_SECRET_ENV_VARS:
        monkeypatch.setenv(name, f"unit-{name.lower().replace('_', '-')}")

    validate_required_secrets()
