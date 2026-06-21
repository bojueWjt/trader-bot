"""OM0-01 — consistency tests for the frozen order-management state machines.

The descriptor ``packages/contracts/v1/order_state.v1.json`` is the single source
of truth for the intent / order / position / command state machines and the
HALTED/REDUCING permission matrix (PLAN §5). These tests prove two things:

1. The descriptor is internally well-formed (states, transitions, terminals,
   reachability, event maps are mutually consistent).
2. The descriptor reconciles with the REAL code/DB it must drive — the
   ``trade_intent_status`` SQL enum, the Nautilus order/position event catalogs,
   the operator-command terminal statuses, and the risk_state mode gate — so the
   contract cannot silently drift away from the implementation.

Source files are parsed (ast / regex), never imported, to avoid coupling this
test to service-package import side effects.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESCRIPTOR_PATH = ROOT / "packages" / "contracts" / "v1" / "order_state.v1.json"
DOC_PATH = ROOT / "docs" / "order-management" / "STATE_MACHINES.md"

CANONICAL_SCHEMA_SQL = ROOT / "db" / "migrations" / "0001_canonical_schema.up.sql"
EVENT_MAPPER_PY = ROOT / "services" / "nautilus-node" / "projection" / "event_mapper.py"
COMMANDS_PY = ROOT / "services" / "control-plane" / "commands" / "commands.py"
RISK_STATE_PY = ROOT / "services" / "control-plane" / "risk_state" / "risk_state.py"

NON_STATE_TARGETS = {"self", "derive"}


def load_descriptor() -> dict:
    with DESCRIPTOR_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------- #
# Source extraction helpers (parse, do not import)
# --------------------------------------------------------------------------- #
def _string_members(node: ast.AST) -> set[str]:
    """Collect string constants from a set/list/tuple literal or frozenset(...) call."""
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "frozenset":
        if node.args:
            return _string_members(node.args[0])
        return set()
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return {
            elt.value
            for elt in node.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        }
    return set()


def assigned_collection(py_path: Path, name: str) -> set[str]:
    tree = ast.parse(py_path.read_text(encoding="utf-8"))
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return _string_members(stmt.value)
    raise AssertionError(f"{name} not found in {py_path}")


def sql_enum_values(sql_path: Path, type_name: str) -> list[str]:
    text = sql_path.read_text(encoding="utf-8")
    match = re.search(
        rf"CREATE TYPE {re.escape(type_name)} AS ENUM\s*\(([^)]*)\)",
        text,
    )
    assert match, f"enum {type_name} not found in {sql_path}"
    return re.findall(r"'([^']+)'", match.group(1))


# --------------------------------------------------------------------------- #
# 1. Structural well-formedness
# --------------------------------------------------------------------------- #
def test_descriptor_is_valid_json_and_versioned():
    d = load_descriptor()
    assert d["schema_version"] == "1.0"
    assert d["kind"] == "state-machine-descriptor"
    assert set(d["domains"]) == {"intent", "order", "position", "command"}


def test_every_transition_references_defined_states():
    d = load_descriptor()
    for name, dom in d["domains"].items():
        states = set(dom["states"])
        assert dom["initial"] in states, f"{name}: initial not a defined state"
        for tr in dom["transitions"]:
            assert tr["from"] in states, f"{name}: transition from {tr['from']!r} undefined"
            assert tr["to"] in states, f"{name}: transition to {tr['to']!r} undefined"
            assert tr.get("trigger"), f"{name}: transition {tr} missing trigger"
        for target in dom.get("any_state_to", {}).get("targets", []):
            assert target in states, f"{name}: any_state_to target {target!r} undefined"


def test_terminal_states_have_no_outgoing_transitions():
    d = load_descriptor()
    for name, dom in d["domains"].items():
        terminal = {s for s, meta in dom["states"].items() if meta.get("terminal")}
        for tr in dom["transitions"]:
            assert tr["from"] not in terminal, (
                f"{name}: terminal state {tr['from']!r} has outgoing transition"
            )
        # any_state_to interrupts must only fire from non-terminal states; they
        # never originate at a terminal, so terminals can never be re-animated.


def test_all_states_reachable_from_entry_states():
    d = load_descriptor()
    for name, dom in d["domains"].items():
        states = dom["states"]
        entries = {dom["initial"]} | {
            s for s, meta in states.items() if meta.get("entry")
        }
        reachable = set(entries)
        edges = [(t["from"], t["to"]) for t in dom["transitions"]]
        interrupts = dom.get("any_state_to", {}).get("targets", [])
        changed = True
        while changed:
            changed = False
            for src, dst in edges:
                if src in reachable and dst not in reachable:
                    reachable.add(dst)
                    changed = True
            # interrupts fire from any reachable non-terminal state
            if any(not states[s].get("terminal") for s in reachable):
                for tgt in interrupts:
                    if tgt not in reachable:
                        reachable.add(tgt)
                        changed = True
        missing = set(states) - reachable
        assert not missing, f"{name}: unreachable states {sorted(missing)}"


def test_event_state_map_targets_are_defined():
    d = load_descriptor()
    for name in ("order", "position"):
        dom = d["domains"][name]
        states = set(dom["states"])
        for event, target in dom["event_state_map"].items():
            assert target in states or target in NON_STATE_TARGETS, (
                f"{name}: event {event!r} maps to undefined target {target!r}"
            )


# --------------------------------------------------------------------------- #
# 2. Reconciliation with the real implementation
# --------------------------------------------------------------------------- #
def test_intent_reconciles_with_trade_intent_status_enum():
    d = load_descriptor()
    intent = d["domains"]["intent"]
    sql_values = sql_enum_values(CANONICAL_SCHEMA_SQL, "trade_intent_status")
    # descriptor's declared reconcile snapshot must match the live DB enum
    assert intent["reconcile_with"]["existing_values"] == sql_values, (
        "descriptor existing_values drifted from trade_intent_status SQL enum"
    )
    # every existing DB value except the pre-approval 'draft' must be a real state
    states = set(intent["states"])
    for value in sql_values:
        if value == "draft":
            assert value not in states, "'draft' must stay out of the post-approval lifecycle"
        else:
            assert value in states, f"trade_intent_status {value!r} missing from intent states"


def test_order_event_map_covers_nautilus_order_events():
    d = load_descriptor()
    order_events = assigned_collection(EVENT_MAPPER_PY, "ORDER_EVENT_TYPES")
    assert order_events, "could not parse ORDER_EVENT_TYPES"
    mapped = set(d["domains"]["order"]["event_state_map"])
    assert order_events == mapped, (
        f"order event map drift: only-in-code={sorted(order_events - mapped)}, "
        f"only-in-descriptor={sorted(mapped - order_events)}"
    )


def test_position_event_map_covers_nautilus_position_events():
    d = load_descriptor()
    position_events = assigned_collection(EVENT_MAPPER_PY, "POSITION_EVENT_TYPES")
    assert position_events, "could not parse POSITION_EVENT_TYPES"
    mapped = set(d["domains"]["position"]["event_state_map"])
    assert position_events == mapped, (
        f"position event map drift: only-in-code={sorted(position_events - mapped)}, "
        f"only-in-descriptor={sorted(mapped - position_events)}"
    )


def test_command_terminals_superset_existing_operator_command_terminals():
    d = load_descriptor()
    command = d["domains"]["command"]
    descriptor_terminals = {s for s, m in command["states"].items() if m.get("terminal")}
    existing_terminals = assigned_collection(COMMANDS_PY, "TERMINAL_STATUSES")
    assert existing_terminals, "could not parse TERMINAL_STATUSES"
    assert existing_terminals <= descriptor_terminals, (
        f"operator_command terminals {sorted(existing_terminals)} not all terminal in descriptor"
    )
    # the declared reconcile mapping must map every existing status into a defined state
    states = set(command["states"])
    mapping = command["reconcile_with"]["mapping"]
    assert set(mapping) == set(command["reconcile_with"]["existing_statuses"])
    for canonical in mapping.values():
        assert canonical in states, f"reconcile mapping target {canonical!r} undefined"


def test_halted_semantics_match_risk_state_modes_and_reduce_only_rule():
    d = load_descriptor()
    halted = d["halted_semantics"]
    valid_modes = assigned_collection(RISK_STATE_PY, "VALID_MODES")
    assert valid_modes == set(halted["modes"]), "halted modes drifted from risk_state.VALID_MODES"

    matrix = halted["permission_matrix"]
    # reduce-only channel ALWAYS open in every mode (PLAN §3 rule #4)
    for mode in halted["modes"]:
        for action in halted["reduce_only_actions"]:
            assert matrix[mode][action] is True, f"{action} must be allowed under {mode}"
    # opening actions blocked under REDUCING and HALTED, allowed under ACTIVE
    for action in halted["opening_actions"]:
        assert matrix["ACTIVE"][action] is True
        assert matrix["REDUCING"][action] is False
        assert matrix["HALTED"][action] is False


# --------------------------------------------------------------------------- #
# 3. Doc-drift guard
# --------------------------------------------------------------------------- #
def test_doc_mentions_every_state():
    d = load_descriptor()
    doc = DOC_PATH.read_text(encoding="utf-8")
    for name, dom in d["domains"].items():
        for state in dom["states"]:
            assert state in doc, f"STATE_MACHINES.md missing {name} state {state!r}"
