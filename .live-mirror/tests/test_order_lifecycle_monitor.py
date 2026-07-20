from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MONITOR_PATH = REPO_ROOT / ".live-mirror" / "scripts" / "order_lifecycle_monitor.py"


def _load_monitor():
    feeder = types.ModuleType("hermes_signal_feeder")
    feeder.sanitize_prompt = lambda prompt: prompt
    feeder.run_hermes = lambda *args, **kwargs: True
    sys.modules["hermes_signal_feeder"] = feeder
    spec = importlib.util.spec_from_file_location(
        "test_order_lifecycle_monitor",
        MONITOR_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_halted_node_alert_is_deduplicated_for_24_hours(monkeypatch):
    module = _load_monitor()
    state = {}
    prompts = []
    rows = [["node-a", "HALTED", "false", "projection lag exceeded"]]
    monkeypatch.setattr(module, "q", lambda sql: rows)

    def fake_wake(prompt, name, dry_run):
        prompts.append((prompt, name, dry_run))
        return True

    monkeypatch.setattr(module, "wake_hermes", fake_wake)

    module.sweep_node_health(state, dry_run=False, now_ts=1_000)
    module.sweep_node_health(state, dry_run=False, now_ts=1_000 + 23 * 3600)
    module.sweep_node_health(state, dry_run=False, now_ts=1_000 + 24 * 3600)

    assert len(prompts) == 2
    assert "node-a" in prompts[0][0]
    assert "projection lag exceeded" in prompts[0][0]
    assert "持续" in prompts[0][0]


def test_exchange_open_order_ids_includes_algo_orders(monkeypatch):
    # 2026-07-18: reading only open_orders made resting SL/TP (algo_orders)
    # look like ghosts and heal_ghost_order terminalized 3 live stops.
    module = _load_monitor()
    captured = []

    def fake_q(sql):
        captured.append(sql)
        return [[
            "account-a",
            "10.0",
            '[{"client_order_id": "Blimit01"}, {"client_order_id": "Bstop01"}]',
        ]]

    monkeypatch.setattr(module, "q", fake_q)

    out = module._exchange_open_order_ids()

    assert out["account-a"] == {"Blimit01", "Bstop01"}
    assert "algo_orders" in captured[0]


def test_readiness_false_node_alerts_even_when_status_is_active(monkeypatch):
    module = _load_monitor()
    state = {}
    prompts = []
    rows = [["node-b", "ACTIVE", "false", ""]]
    monkeypatch.setattr(module, "q", lambda sql: rows)
    monkeypatch.setattr(
        module,
        "wake_hermes",
        lambda prompt, name, dry_run: prompts.append(prompt) or True,
    )

    module.sweep_node_health(state, dry_run=False, now_ts=2_000)

    assert len(prompts) == 1
    assert "node-b" in prompts[0]
    assert "readiness=false" in prompts[0]
