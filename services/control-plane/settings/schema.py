"""Order-management settings descriptor loading and validation."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[3]
DESCRIPTOR_PATH = REPO_ROOT / "packages" / "contracts" / "v1" / "order_management_settings.v1.json"

EXPECTED_CATEGORIES = (
    "general",
    "entry",
    "protection",
    "money",
    "price_monitor",
    "reconciliation",
    "emergency",
    "notifications",
    "advanced",
)
ALLOWED_TYPES = {"number", "integer", "boolean", "string", "enum"}
ALLOWED_APPLY_MODES = {"hot_reload", "restart_required"}


class SettingsValidationError(ValueError):
    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__("settings validation failed")
        self.errors = errors


def load_descriptor(path: str | Path | None = None) -> dict[str, Any]:
    descriptor_path = Path(path) if path is not None else DESCRIPTOR_PATH
    with descriptor_path.open(encoding="utf-8") as handle:
        descriptor = json.load(handle)
    validate_descriptor(descriptor)
    return descriptor


def validate_descriptor(descriptor: dict[str, Any]) -> None:
    errors: list[dict[str, Any]] = []
    if descriptor.get("schema_version") != "1.0":
        errors.append(_error("$", "schema_version must be 1.0"))
    if descriptor.get("kind") != "settings-descriptor":
        errors.append(_error("$", "kind must be settings-descriptor"))

    categories = descriptor.get("categories")
    if not isinstance(categories, dict):
        errors.append(_error("categories", "categories must be an object"))
        raise SettingsValidationError(errors)
    if set(categories) != set(EXPECTED_CATEGORIES):
        errors.append(_error("categories", f"categories must be exactly {sorted(EXPECTED_CATEGORIES)}"))

    for category, field_name, field in iter_field_descriptors(descriptor, validate_first=False):
        path = f"{category}.{field_name}"
        field_type = field.get("type")
        if field_type not in ALLOWED_TYPES:
            errors.append(_error(path, f"type must be one of {sorted(ALLOWED_TYPES)}"))
        if "unit" not in field:
            errors.append(_error(path, "unit required"))
        if "default" not in field:
            errors.append(_error(path, "default required"))
            continue
        if field.get("apply_mode") not in ALLOWED_APPLY_MODES:
            errors.append(_error(path, f"apply_mode must be one of {sorted(ALLOWED_APPLY_MODES)}"))
        if not isinstance(field.get("scope_overridable"), bool):
            errors.append(_error(path, "scope_overridable must be boolean"))
        if field.get("secret", False) is not False:
            errors.append(_error(path, "business settings must not be secret fields"))

        default_errors = _validate_value(category, field_name, field.get("default"), field)
        errors.extend(default_errors)
        if field_type == "enum":
            enum_values = field.get("enum")
            if not isinstance(enum_values, list) or not enum_values:
                errors.append(_error(path, "enum fields require a non-empty enum array"))
            elif field.get("default") not in enum_values:
                errors.append(_error(path, "enum default must be listed in enum"))

    if errors:
        raise SettingsValidationError(errors)


def iter_field_descriptors(
    descriptor: dict[str, Any] | None = None,
    *,
    validate_first: bool = True,
) -> Iterable[tuple[str, str, dict[str, Any]]]:
    if descriptor is None:
        descriptor = load_descriptor()
        validate_first = False
    if validate_first:
        validate_descriptor(descriptor)
    categories = descriptor.get("categories") or {}
    for category_name in EXPECTED_CATEGORIES:
        fields = categories.get(category_name, {})
        if not isinstance(fields, dict):
            continue
        for field_name, field in fields.items():
            if isinstance(field, dict):
                yield category_name, field_name, field


def default_settings(descriptor: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    descriptor = descriptor or load_descriptor()
    defaults: dict[str, dict[str, Any]] = {category: {} for category in EXPECTED_CATEGORIES}
    for category, field_name, field in iter_field_descriptors(descriptor, validate_first=False):
        defaults[category][field_name] = copy.deepcopy(field["default"])
    return defaults


def field_descriptor(category: str, field_name: str, descriptor: dict[str, Any] | None = None) -> dict[str, Any]:
    descriptor = descriptor or load_descriptor()
    try:
        field = descriptor["categories"][category][field_name]
    except KeyError as exc:
        raise KeyError(f"unknown setting field {category}.{field_name}") from exc
    return field


def validate_settings(
    settings: dict[str, Any],
    descriptor: dict[str, Any] | None = None,
    *,
    partial: bool = True,
) -> dict[str, Any]:
    descriptor = descriptor or load_descriptor()
    errors: list[dict[str, Any]] = []
    normalized: dict[str, dict[str, Any]] = {}

    if not isinstance(settings, dict):
        return {"valid": False, "errors": [_error("$", "settings must be an object")], "normalized": {}}

    known_categories = descriptor["categories"]
    categories_to_visit = settings.keys() if partial else known_categories.keys()
    for category in categories_to_visit:
        if category not in known_categories:
            errors.append(_error(str(category), "unknown settings category"))
            continue
        raw_category_settings = settings.get(category, {})
        if raw_category_settings is None and partial:
            continue
        if not isinstance(raw_category_settings, dict):
            errors.append(_error(str(category), "category settings must be an object"))
            continue
        normalized_category: dict[str, Any] = {}
        known_fields = known_categories[category]
        fields_to_visit = raw_category_settings.keys() if partial else known_fields.keys()
        for field_name in fields_to_visit:
            if field_name not in known_fields:
                errors.append(_error(f"{category}.{field_name}", "unknown settings field"))
                continue
            value = raw_category_settings.get(field_name)
            if value is None and partial:
                normalized_category[field_name] = None
                continue
            field = known_fields[field_name]
            field_errors = _validate_value(category, field_name, value, field)
            errors.extend(field_errors)
            if not field_errors:
                normalized_category[field_name] = value
        if normalized_category:
            normalized[category] = normalized_category

    return {"valid": not errors, "errors": errors, "normalized": normalized}


def validate_settings_or_raise(
    settings: dict[str, Any],
    descriptor: dict[str, Any] | None = None,
    *,
    partial: bool = True,
) -> dict[str, dict[str, Any]]:
    result = validate_settings(settings, descriptor, partial=partial)
    if not result["valid"]:
        raise SettingsValidationError(result["errors"])
    return result["normalized"]


def flatten_settings(settings: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for category, fields in (settings or {}).items():
        if not isinstance(fields, dict):
            continue
        for field_name, value in fields.items():
            flattened[f"{category}.{field_name}"] = value
    return flattened


def without_secret_fields(settings: dict[str, Any], descriptor: dict[str, Any] | None = None) -> dict[str, Any]:
    descriptor = descriptor or load_descriptor()
    cleaned: dict[str, dict[str, Any]] = {}
    for category, fields in (settings or {}).items():
        if not isinstance(fields, dict):
            continue
        for field_name, value in fields.items():
            field = descriptor["categories"].get(category, {}).get(field_name)
            if field is None or field.get("secret", False):
                continue
            cleaned.setdefault(category, {})[field_name] = copy.deepcopy(value)
    return cleaned


def _validate_value(category: str, field_name: str, value: Any, field: dict[str, Any]) -> list[dict[str, Any]]:
    path = f"{category}.{field_name}"
    field_type = field.get("type")
    errors: list[dict[str, Any]] = []

    if field_type == "boolean":
        if not isinstance(value, bool):
            errors.append(_error(path, "value must be boolean"))
    elif field_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(_error(path, "value must be integer"))
    elif field_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(_error(path, "value must be number"))
    elif field_type == "string":
        if not isinstance(value, str):
            errors.append(_error(path, "value must be string"))
    elif field_type == "enum":
        enum_values = field.get("enum") or []
        if not isinstance(value, str) or value not in enum_values:
            errors.append(_error(path, f"value must be one of {enum_values}"))

    if errors:
        return errors
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = field.get("min")
        maximum = field.get("max")
        if minimum is not None and value < minimum:
            errors.append(_error(path, f"value must be >= {minimum}"))
        if maximum is not None and value > maximum:
            errors.append(_error(path, f"value must be <= {maximum}"))
    return errors


def _error(path: str, message: str) -> dict[str, str]:
    return {"path": path, "message": message}
