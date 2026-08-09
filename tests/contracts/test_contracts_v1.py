import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ModuleNotFoundError:
    Draft202012Validator = None
    FormatChecker = None


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_ROOT = ROOT / "packages" / "contracts"
V1_ROOT = CONTRACTS_ROOT / "v1"
SNAPSHOT_PATH = V1_ROOT / ".snapshot.json"

SCHEMAS = {
    "hermes_decision": V1_ROOT / "hermes_decision.v1.json",
    "approved_trade_intent": V1_ROOT / "approved_trade_intent.v1.json",
    "execution_event_envelope": V1_ROOT / "execution_event_envelope.v1.json",
    "system_snapshot": V1_ROOT / "system_snapshot.v1.json",
}


class ContractValidationError(AssertionError):
    pass


def load_json(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def canonical_dumps(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def schema_types(schema):
    raw = schema.get("type")
    if raw is None:
        if "properties" in schema:
            return {"object"}
        if "items" in schema:
            return {"array"}
        return set()
    if isinstance(raw, list):
        return set(raw)
    return {raw}


def assert_rfc3339_datetime(value, path):
    if not isinstance(value, str) or "T" not in value:
        raise ContractValidationError(f"{path}: format date-time expected")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContractValidationError(f"{path}: format date-time expected") from exc
    if parsed.tzinfo is None:
        raise ContractValidationError(f"{path}: format date-time expected")


def validate(instance, schema):
    if Draft202012Validator is not None:
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
        if errors:
            error = errors[0]
            location = "$" + "".join(f".{part}" for part in error.path)
            raise ContractValidationError(f"{location}: {error.validator} {error.message}")
        validate_minimal(instance, schema)
        return

    validate_minimal(instance, schema)


def validate_minimal(instance, schema, path="$"):
    if schema is True:
        return
    if schema is False:
        raise ContractValidationError(f"{path}: false schema rejected value")

    if "const" in schema and instance != schema["const"]:
        raise ContractValidationError(f"{path}: const {schema['const']!r} expected")

    if "enum" in schema and instance not in schema["enum"]:
        raise ContractValidationError(f"{path}: enum {schema['enum']!r} expected")

    allowed_types = schema_types(schema)
    if allowed_types:
        actual_type = json_type(instance)
        if actual_type == "integer" and "number" in allowed_types:
            actual_type = "number"
        if actual_type not in allowed_types:
            raise ContractValidationError(
                f"{path}: type {sorted(allowed_types)!r} expected, got {json_type(instance)}"
            )

    if instance is None:
        return

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for field in required:
            if field not in instance:
                raise ContractValidationError(f"{path}: required field {field!r} missing")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(instance) - set(properties))
            if extra:
                raise ContractValidationError(f"{path}: additionalProperties {extra!r} not allowed")
        for field, value in instance.items():
            if field in properties:
                validate_minimal(value, properties[field], f"{path}.{field}")

    if isinstance(instance, list) and "items" in schema:
        item_schema = schema["items"]
        for index, item in enumerate(instance):
            validate_minimal(item, item_schema, f"{path}[{index}]")

    if isinstance(instance, str):
        if schema.get("format") == "date-time":
            assert_rfc3339_datetime(instance, path)
        if schema.get("format") == "uuid":
            try:
                uuid.UUID(instance)
            except ValueError as exc:
                raise ContractValidationError(f"{path}: format uuid expected") from exc
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            raise ContractValidationError(f"{path}: pattern {schema['pattern']!r} expected")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise ContractValidationError(f"{path}: minLength {schema['minLength']} expected")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise ContractValidationError(f"{path}: maxLength {schema['maxLength']} expected")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise ContractValidationError(f"{path}: minimum {schema['minimum']} expected")


def iter_schema_nodes(schema):
    yield schema
    if isinstance(schema, dict):
        for child in schema.get("properties", {}).values():
            yield from iter_schema_nodes(child)
        if isinstance(schema.get("items"), dict):
            yield from iter_schema_nodes(schema["items"])


# order_plan is intentionally OPEN: it carries the action-specific execution plan
# into Nautilus and is validated by the execution strategy per action (window C
# contract lock per window-B CCR). Every other object must stay strict.
OPEN_OBJECTS = {"order_plan"}


def assert_all_objects_are_strict(schema, name=None):
    if not isinstance(schema, dict):
        return
    if "object" in schema_types(schema) and name not in OPEN_OBJECTS:
        assert schema.get("additionalProperties") is False
    for child_name, child in schema.get("properties", {}).items():
        assert_all_objects_are_strict(child, child_name)
    if isinstance(schema.get("items"), dict):
        assert_all_objects_are_strict(schema["items"], name)


def schema_for_example(example_path):
    for name, schema_path in SCHEMAS.items():
        if example_path.name.startswith(name):
            return load_json(schema_path)
    raise AssertionError(f"No schema mapping for {example_path}")


def valid_examples():
    return sorted(
        path
        for path in (V1_ROOT / "examples" / "valid").glob("*.json")
        if path.name != "index.json"
    )


def invalid_examples():
    return sorted((V1_ROOT / "examples" / "invalid").glob("*.json"))


def collect_shape(schema):
    shape = {}

    def visit(node, path):
        entry = {}
        types = sorted(schema_types(node))
        if types:
            entry["type"] = types
        if "required" in node:
            entry["required"] = sorted(node["required"])
        if "properties" in node:
            entry["properties"] = sorted(node["properties"])
        if "enum" in node:
            entry["enum"] = sorted(json.dumps(value, sort_keys=True) for value in node["enum"])
        if "const" in node:
            entry["const"] = json.dumps(node["const"], sort_keys=True)
        if entry:
            shape[path] = entry
        for field, child in node.get("properties", {}).items():
            visit(child, f"{path}.{field}" if path else field)
        if isinstance(node.get("items"), dict):
            visit(node["items"], f"{path}[]")

    visit(schema, "")
    return shape


def current_snapshot():
    return {name: collect_shape(load_json(path)) for name, path in sorted(SCHEMAS.items())}


def breaking_changes(previous, current):
    failures = []
    for schema_name, previous_shape in previous.items():
        current_shape = current.get(schema_name, {})
        for path, previous_entry in previous_shape.items():
            if path not in current_shape:
                failures.append(f"{schema_name}:{path}: field/schema node deleted")
                continue
            current_entry = current_shape[path]
            previous_properties = set(previous_entry.get("properties", []))
            current_properties = set(current_entry.get("properties", []))
            deleted_properties = sorted(previous_properties - current_properties)
            if deleted_properties:
                failures.append(f"{schema_name}:{path}: deleted fields {deleted_properties}")

            previous_required = set(previous_entry.get("required", []))
            current_required = set(current_entry.get("required", []))
            added_required = sorted(current_required - previous_required)
            if added_required:
                failures.append(f"{schema_name}:{path}: added required fields {added_required}")

            previous_enum = set(previous_entry.get("enum", []))
            current_enum = set(current_entry.get("enum", []))
            removed_enum = sorted(previous_enum - current_enum)
            if removed_enum:
                failures.append(f"{schema_name}:{path}: removed enum values {removed_enum}")

            previous_type = set(previous_entry.get("type", []))
            current_type = set(current_entry.get("type", []))
            removed_type = sorted(previous_type - current_type)
            if removed_type:
                failures.append(f"{schema_name}:{path}: narrowed types {removed_type}")
    return failures


def test_valid_examples_pass():
    assert len(valid_examples()) >= len(SCHEMAS) * 2
    for path in valid_examples():
        validate(load_json(path), schema_for_example(path))


def test_approved_trade_intent_wire_order_plan_variants_pass():
    schema = load_json(SCHEMAS["approved_trade_intent"])
    base = load_json(V1_ROOT / "examples" / "valid" / "approved_trade_intent.open_position.json")
    order_plans = [
        {
            "side": "sell",
            "type": "zone",
            "time_in_force": "GTC",
            "quantity": "0.0019261637239165329",
            "price": 62300.0,
            "price_min": 62300.0,
            "price_max": 62700.0,
            "stop_loss": 63100.0,
            "take_profits": [
                61500,
                60800,
                60000,
            ],
            "leverage": 10.0,
        },
        {
            "side": "sell",
            "type": "market",
            "time_in_force": "IOC",
            "quantity": "0.002",
            "stop_loss": 70000.0,
            "take_profits": [],
        },
        {
            "side": "sell",
            "type": "market",
            "time_in_force": "IOC",
            "take_profits": [],
        },
        {
            "side": "sell",
            "type": "market",
            "time_in_force": "IOC",
            "quantity": "0.002",
            "stop_price": 70100.0,
            "limit_price": 70050.0,
            "trigger_price": 69950.0,
            "take_profits": [],
        },
    ]
    for order_plan in order_plans:
        instance = dict(base)
        instance["order_plan"] = order_plan
        validate(instance, schema)


def test_invalid_examples_fail():
    reason_checks = {
        "additional_property": ("additionalProperties",),
        "bad_date_time": ("date-time", "format"),
        "bad_idempotency_key": ("pattern", "minLength", "maxLength"),
        "invalid_enum": ("enum",),
        "missing_required": ("required",),
    }
    assert invalid_examples()
    for path in invalid_examples():
        expected_reason = next(
            reason for marker, reason in reason_checks.items() if marker in path.stem
        )
        try:
            validate(load_json(path), schema_for_example(path))
        except ContractValidationError as exc:
            assert any(reason in str(exc) for reason in expected_reason)
        else:
            raise AssertionError(f"{path} unexpectedly passed validation")


def test_round_trip():
    for path in valid_examples():
        original = load_json(path)
        validate(original, schema_for_example(path))
        dumped = canonical_dumps(original)
        assert canonical_dumps(json.loads(dumped)) == dumped


def test_valid_examples_index_matches_files():
    index_path = V1_ROOT / "examples" / "valid" / "index.json"
    index = load_json(index_path)
    listed = []
    for schema_name, examples in index.items():
        assert schema_name in SCHEMAS
        assert isinstance(examples, list)
        listed.extend(examples)
    assert sorted(listed) == sorted(path.name for path in valid_examples())


def test_schema_invariants():
    for name, path in SCHEMAS.items():
        schema = load_json(path)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["properties"]["schema_version"]["const"] == "1.0"
        assert_all_objects_are_strict(schema)
        assert name in schema["$id"]


def test_backward_incompat_detector():
    snapshot = current_snapshot()
    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        SNAPSHOT_PATH.write_text(canonical_dumps(snapshot), encoding="utf-8")
        return

    previous = load_json(SNAPSHOT_PATH)
    failures = breaking_changes(previous, snapshot)
    assert not failures, "\n".join(failures)
