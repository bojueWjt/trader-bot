from __future__ import annotations

import json
from pathlib import Path

from settings.schema import default_settings, iter_field_descriptors, load_descriptor, validate_descriptor


ROOT = Path(__file__).resolve().parents[3]
DESCRIPTOR_PATH = ROOT / "packages" / "contracts" / "v1" / "order_management_settings.v1.json"
SNAPSHOT_PATH = ROOT / "packages" / "contracts" / "v1" / ".snapshot.json"

EXPECTED_CATEGORIES = {
    "general",
    "entry",
    "protection",
    "money",
    "price_monitor",
    "reconciliation",
    "emergency",
    "notifications",
    "advanced",
}

FROZEN_PRICE_MONITOR_DEFAULTS = {
    "market_data_stale_seconds": 10,
    "account_data_stale_seconds": 15,
    "execution_event_stale_seconds": 30,
    "projection_lag_threshold_ms": 5000,
    "reconciliation_stale_seconds": 300,
    "price_deviation_bps": 200,
    "evaluation_interval_seconds": 5,
    "protection_watchdog_interval_seconds": 10,
    "disconnect_action": "halt",
}


def test_order_management_settings_descriptor_is_reference_data_not_snapshot_schema() -> None:
    descriptor = load_descriptor()

    assert DESCRIPTOR_PATH.exists()
    assert descriptor["schema_version"] == "1.0"
    assert descriptor["kind"] == "settings-descriptor"
    assert set(descriptor["categories"]) == EXPECTED_CATEGORIES

    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert "order_management_settings" not in snapshot


def test_every_setting_field_has_required_metadata_and_valid_defaults() -> None:
    descriptor = load_descriptor()
    validate_descriptor(descriptor)

    fields = list(iter_field_descriptors(descriptor))
    assert fields
    for category, field_name, field in fields:
        assert field["type"] in {"number", "integer", "boolean", "string", "enum"}, (category, field_name)
        assert "unit" in field, (category, field_name)
        assert "default" in field, (category, field_name)
        assert field["apply_mode"] in {"hot_reload", "restart_required"}, (category, field_name)
        assert isinstance(field["scope_overridable"], bool), (category, field_name)
        assert field.get("secret", False) is False, (category, field_name)


def test_safe_defaults_are_fail_closed() -> None:
    defaults = default_settings()

    assert defaults["general"]["order_manager_enabled"] is False
    assert defaults["general"]["execution_mode"] == "shadow"
    assert defaults["general"]["risk_posture"] == "HALTED"
    assert defaults["protection"]["require_stop"] is True
    assert defaults["price_monitor"]["disconnect_action"] == "halt"


def test_price_monitor_freshness_fields_are_frozen() -> None:
    descriptor = load_descriptor()
    price_monitor = descriptor["categories"]["price_monitor"]

    assert set(price_monitor) == set(FROZEN_PRICE_MONITOR_DEFAULTS)
    for field_name, default in FROZEN_PRICE_MONITOR_DEFAULTS.items():
        field = price_monitor[field_name]
        assert field["default"] == default
        assert field["apply_mode"] == "hot_reload"
    assert price_monitor["disconnect_action"]["enum"] == ["halt", "reducing", "hold"]


def test_descriptor_contains_no_secret_setting_names_or_secret_fields() -> None:
    descriptor = load_descriptor()
    forbidden_fragments = ("secret", "token", "api_key", "apikey", "password", "credential")

    for category, field_name, field in iter_field_descriptors(descriptor):
        assert field.get("secret", False) is False
        lowered = f"{category}.{field_name}".lower()
        assert not any(fragment in lowered for fragment in forbidden_fragments)
