from __future__ import annotations

import pytest

from settings.import_export import export_payload, sanitize_export, validate_import_payload
from settings.permissions import (
    SettingsPermissionDenied,
    assert_can_write_settings,
    has_settings_permission,
    requires_live_risk_confirmation,
)


def test_settings_rbac_matrix_covers_viewer_operator_and_risk_admin() -> None:
    matrix = {
        "viewer": {"read", "validate", "export"},
        "operator": {"read", "validate", "export", "patch"},
        "risk_admin": {"read", "validate", "export", "patch", "rollback", "import"},
    }

    for role, allowed in matrix.items():
        for operation in {"read", "validate", "export", "patch", "rollback", "import"}:
            assert has_settings_permission(role, operation) is (operation in allowed)

    with pytest.raises(SettingsPermissionDenied):
        assert_can_write_settings({"actor_id": "viewer-1", "role": "viewer"}, operation="patch")
    assert_can_write_settings({"actor_id": "operator-1", "role": "operator"}, operation="patch")


def test_relaxing_live_risk_requires_confirm_and_operator_signoff() -> None:
    before = {
        "general": {"execution_mode": "live"},
        "money": {"max_notional_per_order": 0, "risk_per_trade_pct": 0},
    }
    after = {
        "general": {"execution_mode": "live"},
        "money": {"max_notional_per_order": 1000, "risk_per_trade_pct": 1},
    }

    assert requires_live_risk_confirmation(before, after) is True
    with pytest.raises(SettingsPermissionDenied):
        assert_can_write_settings(
            {"actor_id": "risk-1", "role": "risk_admin"},
            operation="patch",
            before_settings=before,
            after_settings=after,
            confirm=False,
            operator_signoff=None,
        )
    assert_can_write_settings(
        {"actor_id": "risk-1", "role": "risk_admin"},
        operation="patch",
        before_settings=before,
        after_settings=after,
        confirm=True,
        operator_signoff="operator-1",
    )


def test_export_never_includes_secret_like_fields() -> None:
    exported = sanitize_export(
        {
            "general": {"order_manager_enabled": True},
            "advanced": {"api_key": "must-not-leak", "token": "must-not-leak"},
        }
    )

    assert exported == {"general": {"order_manager_enabled": True}}
    payload = export_payload(
        scope="account",
        scope_key="acct-1",
        version=3,
        settings=exported,
        environment="testnet",
    )
    assert "api_key" not in str(payload).lower()
    assert "token" not in str(payload).lower()


def test_import_validate_warns_on_environment_mismatch_and_risk_relaxation() -> None:
    result = validate_import_payload(
        {
            "schema_version": "1.0",
            "environment": "live",
            "settings": {
                "general": {"execution_mode": "live"},
                "money": {"max_notional_per_order": 1000},
            },
        },
        current_environment="testnet",
        before_settings={
            "general": {"execution_mode": "live"},
            "money": {"max_notional_per_order": 0},
        },
    )

    assert result["valid"] is True
    assert "environment_mismatch" in {warning["code"] for warning in result["warnings"]}
    assert "risk_relaxation" in {warning["code"] for warning in result["warnings"]}
