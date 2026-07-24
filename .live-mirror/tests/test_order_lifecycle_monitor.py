from __future__ import annotations

import importlib.util
import json
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


def _mirror_row(account_id, positions, open_orders=None, algo_orders=None, age="10"):
    payload = {
        "positions": positions,
        "open_orders": open_orders or [],
        "algo_orders": algo_orders or [],
    }
    return [account_id, age, json.dumps(payload)]


def _position(symbol, side, quantity):
    return {
        "symbol": symbol,
        "position_side": side,
        "position_amt": str(quantity),
    }


def _stop(symbol, side, quantity, trigger_price, position_side=None):
    order = {
        "symbol": symbol,
        "side": side,
        "type": "STOP_MARKET",
        "quantity": str(quantity),
        "trigger_price": str(trigger_price),
    }
    if position_side:
        order["position_side"] = position_side
    return order


def test_reduce_only_take_profit_is_not_stop_loss():
    module = _load_monitor()
    position_quantities = {("account-a", "BTCUSDT", "long"): 0.135}
    orders = [{
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "TAKE_PROFIT_MARKET",
        "quantity": "0.135",
        "trigger_price": "67432.5",
        "reduce_only": True,
    }]

    protected = module.snapshot_stop_loss_symbols(
        orders,
        position_quantities,
        account_id="account-a",
    )

    assert protected == set()


def test_partial_stop_coverage_leaves_position_naked():
    module = _load_monitor()
    position_quantities = {("account-a", "BTCUSDT", "long"): 0.135}
    orders = [_stop("BTCUSDT", "SELL", 0.1, 62947.3, position_side="LONG")]

    protected = module.snapshot_stop_loss_symbols(
        orders,
        position_quantities,
        account_id="account-a",
    )

    assert protected == set()


def test_stop_requires_closing_direction_and_positive_trigger():
    module = _load_monitor()
    position_quantities = {("account-a", "BTCUSDT", "long"): 0.135}
    orders = [
        _stop("BTCUSDT", "BUY", 0.135, 62947.3, position_side="LONG"),
        _stop("BTCUSDT", "SELL", 0.135, 0, position_side="LONG"),
    ]

    protected = module.snapshot_stop_loss_symbols(
        orders,
        position_quantities,
        account_id="account-a",
    )

    assert protected == set()


def test_valid_full_stop_coverage_protects_exact_position_leg():
    module = _load_monitor()
    position_quantities = {
        ("account-a", "BTCUSDT", "long"): 0.135,
        ("account-a", "BTCUSDT", "short"): 0.004,
    }
    orders = [
        _stop("BTCUSDT", "SELL", 0.135, 62947.3, position_side="LONG"),
        _stop("BTCUSDT", "BUY", 0.004, 66800, position_side="SHORT"),
    ]

    protected = module.snapshot_stop_loss_symbols(
        orders,
        position_quantities,
        account_id="account-a",
    )

    assert protected == set(position_quantities)


def test_btc_long_incident_alerts_despite_protected_short_residual(monkeypatch):
    # 2026-07-23 incident: BTC long 0.135 was naked while a protected 0.004
    # short residual and a reduce-only TP masked it at symbol granularity.
    module = _load_monitor()
    prompts = []
    mirror = _mirror_row(
        "account-a",
        [
            _position("BTCUSDT", "LONG", "0.135"),
            _position("BTCUSDT", "SHORT", "-0.004"),
        ],
        open_orders=[{
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "TAKE_PROFIT_MARKET",
            "quantity": "0.135",
            "trigger_price": "67432.5",
            "reduce_only": True,
            "position_side": "LONG",
        }],
        algo_orders=[
            _stop("BTCUSDT", "BUY", 0.004, 66800, position_side="SHORT"),
        ],
    )

    def fake_q(sql):
        if "exchange_state_mirror" in sql:
            return [mirror]
        if "OrderFilled" in sql:
            return []
        raise AssertionError(sql)

    monkeypatch.setattr(module, "q", fake_q)
    monkeypatch.setattr(
        module,
        "wake_hermes",
        lambda prompt, name, dry_run: prompts.append((prompt, name)) or True,
    )

    state = {}
    module.sweep_naked(state, dry_run=False, now_ts=1_000)

    assert len(prompts) == 1
    assert "account-a" in prompts[0][0]
    assert "BTCUSDT" in prompts[0][0]
    assert "long" in prompts[0][0]
    assert prompts[0][1] == "naked-account-a-BTCUSDT-long"
    assert state["naked:account-a:BTCUSDT:long"] == 1_000


def test_recent_fill_grace_is_scoped_to_account_symbol_and_position_side():
    module = _load_monitor()
    positions = {
        ("account-a", "BTCUSDT", "long"),
        ("account-a", "BTCUSDT", "short"),
        ("account-b", "BTCUSDT", "long"),
    }
    recent_fills = {("account-a", "BTCUSDT", "short")}

    naked = module.naked_positions(
        positions,
        set(),
        recent_fills,
        now_ts=1_000,
        state={},
    )

    assert naked == [
        ("account-a", "BTCUSDT", "long"),
        ("account-b", "BTCUSDT", "long"),
    ]


def _pending_cancel_q(mirror_row, terminal_type=""):
    def fake_q(sql):
        if "OrderPendingCancel" in sql:
            return [["account-a", "cancel-me", "pending-event", "100", terminal_type]]
        if "exchange_state_mirror" in sql:
            return [mirror_row]
        raise AssertionError(sql)

    return fake_q


def test_pending_cancel_timeout_alerts_when_order_is_still_open(monkeypatch):
    module = _load_monitor()
    sent = []
    mirror = _mirror_row(
        "account-a",
        [],
        open_orders=[{"client_order_id": "cancel-me", "symbol": "BTCUSDT"}],
    )
    monkeypatch.setattr(module, "q", _pending_cancel_q(mirror))
    monkeypatch.setattr(module, "tg_send_direct", lambda text: sent.append(text) or True)

    state = {}
    module.sweep_pending_cancels(state, dry_run=False, now_ts=701)

    assert len(sent) == 1
    assert "仍挂着" in sent[0]
    assert "cancel-me" in sent[0]
    assert state["pendingcancel:account-a:cancel-me"]["last_outcome"] == "still_open"


def test_pending_cancel_normal_cancel_before_timeout_does_not_alert(monkeypatch):
    module = _load_monitor()
    sent = []
    mirror = _mirror_row("account-a", [])
    monkeypatch.setattr(module, "q", _pending_cancel_q(mirror, "OrderCanceled"))
    monkeypatch.setattr(module, "tg_send_direct", lambda text: sent.append(text) or True)

    state = {}
    module.sweep_pending_cancels(state, dry_run=False, now_ts=500)

    assert sent == []
    assert "pendingcancel:account-a:cancel-me" not in state


def test_pending_cancel_racing_fill_is_classified_as_filled(monkeypatch):
    module = _load_monitor()
    sent = []
    mirror = _mirror_row("account-a", [])
    monkeypatch.setattr(module, "q", _pending_cancel_q(mirror, "OrderFilled"))
    monkeypatch.setattr(module, "tg_send_direct", lambda text: sent.append(text) or True)
    state = {
        "pendingcancel:account-a:cancel-me": {
            "event_id": "pending-event",
            "pending_at": 100,
        },
    }

    module.sweep_pending_cancels(state, dry_run=False, now_ts=701)

    assert len(sent) == 1
    assert "已成交" in sent[0]
    assert state["pendingcancel:account-a:cancel-me"]["last_outcome"] == "filled"


def test_pending_cancel_tracking_survives_monitor_restart(monkeypatch, tmp_path):
    module = _load_monitor()
    sent = []
    mirror = _mirror_row("account-a", [])
    monkeypatch.setattr(module, "q", _pending_cancel_q(mirror))
    monkeypatch.setattr(module, "tg_send_direct", lambda text: sent.append(text) or True)
    state_path = tmp_path / "monitor-state.json"

    state = {}
    module.sweep_pending_cancels(state, dry_run=False, now_ts=200)
    module.save_state(state, str(state_path))

    restarted_state = module.load_state(str(state_path))
    module.sweep_pending_cancels(restarted_state, dry_run=False, now_ts=701)

    assert len(sent) == 1
    assert "已消失" in sent[0]
    assert restarted_state["pendingcancel:account-a:cancel-me"]["event_id"] == "pending-event"
