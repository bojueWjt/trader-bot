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
    rows = [["node-a", "HALTED", "false", "projection lag exceeded", "1.0"]]
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
    rows = [["node-b", "ACTIVE", "false", "", "1.0"]]
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


def test_operator_halt_with_healthy_readiness_does_not_raise_fault_alert(monkeypatch):
    module = _load_monitor()
    state = {
        "nodehalt:node-a:old fault": 100,
        "nodehalt-first:node-a:old fault": 50,
    }
    prompts = []
    rows = [["node-a", "HALTED", "true", "", "1.0", "HALT", "60"]]
    monkeypatch.setattr(module, "q", lambda sql: rows)
    monkeypatch.setattr(
        module,
        "wake_hermes",
        lambda prompt, name, dry_run: prompts.append(prompt) or True,
    )

    module.sweep_node_health(state, dry_run=False, now_ts=2_000)

    assert prompts == []
    assert not any(key.startswith("nodehalt:node-a:") for key in state)
    assert not any(key.startswith("nodehalt-first:node-a:") for key in state)


def test_stale_operator_halt_command_no_longer_suppresses_fault_alert(monkeypatch):
    module = _load_monitor()
    prompts = []
    rows = [[
        "node-a",
        "HALTED",
        "true",
        "operator_command",
        "1.0",
        "HALT",
        str(module.OPERATOR_HALT_SUPPRESS_SECONDS + 1),
    ]]
    monkeypatch.setattr(module, "q", lambda sql: rows)
    monkeypatch.setattr(
        module,
        "wake_hermes",
        lambda prompt, name, dry_run: prompts.append(prompt) or True,
    )

    module.sweep_node_health({}, dry_run=False, now_ts=2_000)

    assert len(prompts) == 1
    assert "operator_command" in prompts[0]


def test_operator_halt_still_alerts_when_readiness_is_false(monkeypatch):
    module = _load_monitor()
    state = {}
    prompts = []
    rows = [["node-a", "HALTED", "false", "", "1.0", "HALT"]]
    monkeypatch.setattr(module, "q", lambda sql: rows)
    monkeypatch.setattr(
        module,
        "wake_hermes",
        lambda prompt, name, dry_run: prompts.append(prompt) or True,
    )

    module.sweep_node_health(state, dry_run=False, now_ts=2_000)

    assert len(prompts) == 1
    assert "readiness=false" in prompts[0]


def test_stale_heartbeat_alerts_even_when_row_says_healthy(monkeypatch):
    # 2026-07-24: both nodes hung for 7h; heartbeats froze at 02:51 with
    # readiness=true left in the table, so value checks saw "healthy" forever.
    module = _load_monitor()
    state = {}
    direct, woken = [], []
    rows = [["node-a", "ACTIVE", "true", "", str(7 * 3600.0)]]
    monkeypatch.setattr(module, "q", lambda sql: rows)
    monkeypatch.setattr(module, "tg_send_direct", lambda text: direct.append(text) or True)
    monkeypatch.setattr(
        module, "wake_hermes", lambda prompt, name, dry_run: woken.append(name) or True
    )

    module.sweep_node_health(state, dry_run=False, now_ts=10_000)
    module.sweep_node_health(state, dry_run=False, now_ts=10_060)  # dedup window

    assert len(direct) == 1 and "心跳" in direct[0] and "node-a" in direct[0]
    assert woken == ["nodehalt-stale-node-a"]

    # heartbeat recovers → stale keys cleared, healthy row does not alert
    rows[0] = ["node-a", "ACTIVE", "true", "", "2.0"]
    module.sweep_node_health(state, dry_run=False, now_ts=20_000)
    assert "nodehalt:node-a:heartbeat_stale" not in state
    assert len(direct) == 1


def test_fresh_heartbeat_below_threshold_does_not_alert(monkeypatch):
    module = _load_monitor()
    state = {}
    direct = []
    rows = [["node-a", "ACTIVE", "true", "", "120.0"]]
    monkeypatch.setattr(module, "q", lambda sql: rows)
    monkeypatch.setattr(module, "tg_send_direct", lambda text: direct.append(text) or True)
    monkeypatch.setattr(module, "wake_hermes", lambda prompt, name, dry_run: True)

    module.sweep_node_health(state, dry_run=False, now_ts=1_000)

    assert direct == [] and not any(k.startswith("nodehalt:") for k in state)


def test_approved_intent_without_ack_or_execution_progress_alerts_once(monkeypatch):
    module = _load_monitor()
    alerts = []
    rows = [[
        "11111111-1111-1111-1111-111111111111",
        "account-a",
        "BTCUSDT",
        "cancel_order",
        "601",
    ]]
    monkeypatch.setattr(module, "q", lambda sql: rows)
    monkeypatch.setattr(
        module,
        "tg_send_direct",
        lambda text: alerts.append(text) or True,
    )
    state = {}

    module.sweep_intent_stalls(state, dry_run=False, now_ts=10_000)
    module.sweep_intent_stalls(state, dry_run=False, now_ts=10_060)

    assert len(alerts) == 1
    assert "Intent 消费停滞" in alerts[0]
    assert "cancel_order" in alerts[0]
    assert state["intentstall:11111111-1111-1111-1111-111111111111"] == 10_000


def test_intent_stall_sweep_is_quiet_when_query_has_no_backlog(monkeypatch):
    module = _load_monitor()
    alerts = []
    captured = []

    def fake_q(sql):
        captured.append(sql)
        return []

    monkeypatch.setattr(module, "q", fake_q)
    monkeypatch.setattr(
        module,
        "tg_send_direct",
        lambda text: alerts.append(text) or True,
    )

    module.sweep_intent_stalls({}, dry_run=False, now_ts=10_000)

    assert alerts == []
    assert "intent_ack.%" in captured[0]
    assert "execution_events" in captured[0]


def test_protection_frozen_execution_event_alerts_once(monkeypatch):
    module = _load_monitor()
    alerts = []
    payload = {
        "instrument_id": "ETHUSDT-PERP.BINANCE",
        "reason": "revisions_exhausted",
        "protection_revision": 8,
    }
    rows = [[
        "strategy-event-1",
        "account-a",
        "11111111-1111-1111-1111-111111111111",
        "",
        json.dumps(payload),
    ]]
    monkeypatch.setattr(module, "q", lambda sql: rows)
    monkeypatch.setattr(
        module,
        "tg_send_direct",
        lambda text: alerts.append(text) or True,
    )
    state = {}

    module.sweep_protection_events(state, dry_run=False, now_ts=10_000)
    module.sweep_protection_events(state, dry_run=False, now_ts=10_060)

    assert len(alerts) == 1
    assert "保护单冻结" in alerts[0]
    assert "revisions_exhausted" in alerts[0]
    assert state["protectionfreeze:strategy-event-1"] == 10_000


def test_protection_event_sweep_is_quiet_without_frozen_events(monkeypatch):
    module = _load_monitor()
    alerts = []
    monkeypatch.setattr(module, "q", lambda sql: [])
    monkeypatch.setattr(
        module,
        "tg_send_direct",
        lambda text: alerts.append(text) or True,
    )

    module.sweep_protection_events({}, dry_run=False, now_ts=10_000)

    assert alerts == []


def test_missing_daily_report_alerts_after_grace_window_once(monkeypatch):
    module = _load_monitor()
    alerts = []
    now_ts = module.datetime(
        2026,
        8,
        3,
        15,
        3,
        tzinfo=module.timezone.utc,
    ).timestamp()
    monkeypatch.setattr(
        module,
        "_daily_report_file_exists",
        lambda report_date: False,
    )
    monkeypatch.setattr(
        module,
        "_report_health",
        lambda: {
            "last_publication": {
                "status": "succeeded",
                "report_type": "daily",
                "report_date": "2026-08-02",
            }
        },
    )
    monkeypatch.setattr(
        module,
        "tg_send_direct",
        lambda text: alerts.append(text) or True,
    )
    state = {}

    module.sweep_daily_report(state, dry_run=False, now_ts=now_ts)
    module.sweep_daily_report(state, dry_run=False, now_ts=now_ts + 60)

    assert len(alerts) == 1
    assert "日报缺失" in alerts[0]
    assert "2026-08-03" in alerts[0]
    assert state["dailyreport:2026-08-03"] == now_ts


def test_current_daily_health_publication_suppresses_missing_report_alert(monkeypatch):
    module = _load_monitor()
    alerts = []
    now_ts = module.datetime(
        2026,
        8,
        3,
        15,
        3,
        tzinfo=module.timezone.utc,
    ).timestamp()
    monkeypatch.setattr(
        module,
        "_daily_report_file_exists",
        lambda report_date: False,
    )
    monkeypatch.setattr(
        module,
        "_report_health",
        lambda: {
            "last_publication": {
                "status": "succeeded",
                "report_type": "daily",
                "report_date": "2026-08-03",
            }
        },
    )
    monkeypatch.setattr(
        module,
        "tg_send_direct",
        lambda text: alerts.append(text) or True,
    )

    module.sweep_daily_report({}, dry_run=False, now_ts=now_ts)

    assert alerts == []


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


def _authorization(
    current_intent_id="11111111-1111-1111-1111-111111111111",
    parent_intent_id=None,
    account_id="account-a",
    source_message_id="signal-42",
    authorized_by_type="channel",
    authorized_by_id="-100123",
):
    if parent_intent_id is None:
        parent_intent_id = current_intent_id
    return {
        "parent_intent_id": parent_intent_id,
        "current_intent_id": current_intent_id,
        "account_id": account_id,
        "source_message_id": source_message_id,
        "authorized_by_type": authorized_by_type,
        "authorized_by_id": authorized_by_id,
    }


def _persisted_authorization(intent_id, **overrides):
    authorization = _authorization(current_intent_id=intent_id, **overrides)
    authorization.pop("current_intent_id")
    authorization.pop("account_id")
    return authorization


def _position_parent_row(
    symbol,
    side,
    account_id="account-a",
    intent_id="11111111-1111-1111-1111-111111111111",
    authorization=True,
    legacy_raw=None,
):
    plan = {"side": side}
    if authorization:
        plan["authorization"] = _persisted_authorization(intent_id)
    row = [
        intent_id,
        account_id,
        f"{symbol}-PERP",
        json.dumps(plan),
    ]
    if legacy_raw:
        row.extend(legacy_raw)
    return row


def _intent_context_row(
    intent_id,
    action,
    symbol,
    side="long",
    account_id="account-b",
    authorization=True,
    legacy_raw=None,
):
    plan = {"side": side}
    if authorization:
        plan["authorization"] = _persisted_authorization(intent_id)
    row = [
        intent_id,
        action,
        account_id,
        f"{symbol}-PERP",
        json.dumps(plan),
    ]
    if legacy_raw:
        row.extend(legacy_raw)
    return row


def test_direct_authorization_with_false_parent_uses_current_intent():
    module = _load_monitor()
    current_intent_id = "11111111-1111-1111-1111-111111111111"
    plan = {
        "authorization": _persisted_authorization(
            current_intent_id,
            parent_intent_id=False,
        ),
    }

    authorization = module._intent_authorization(
        plan,
        current_intent_id,
        "account-a",
    )

    assert module._has_complete_authorization(authorization)
    assert authorization["parent_intent_id"] == current_intent_id


def test_inherited_parent_authorization_is_accepted():
    module = _load_monitor()
    current_intent_id = "11111111-1111-1111-1111-111111111111"
    inherited_parent_intent_id = "22222222-2222-2222-2222-222222222222"
    plan = {
        "authorization": _persisted_authorization(
            current_intent_id,
            parent_intent_id=inherited_parent_intent_id,
        ),
    }

    authorization = module._intent_authorization(
        plan,
        current_intent_id,
        "account-a",
    )

    assert module._has_complete_authorization(authorization)
    assert authorization["parent_intent_id"] == inherited_parent_intent_id


def test_authorized_management_prompt_carries_required_cli_flags(monkeypatch):
    module = _load_monitor()
    prompts = []
    current_intent_id = "11111111-1111-1111-1111-111111111111"
    plan = {
        "authorization": _persisted_authorization(
            current_intent_id,
            parent_intent_id=False,
        ),
    }
    authorization = module._intent_authorization(
        plan,
        current_intent_id,
        "account-b",
    )
    monkeypatch.setattr(
        module,
        "wake_hermes",
        lambda prompt, name, dry_run: prompts.append(prompt) or True,
    )

    accepted = module.wake_authorized_management(
        "manage",
        "test-management",
        False,
        authorization,
    )

    assert accepted
    assert len(prompts) == 1
    prompt = prompts[0]
    assert "--account account-b" in prompt
    assert "--channel -100123" in prompt
    assert "--entry-ref signal-42" in prompt
    assert "--authorized-by-type channel" in prompt
    assert "--authorized-by-id -100123" in prompt
    assert "--source-message-id signal-42" in prompt
    assert "--created-by-service order-lifecycle-monitor" in prompt
    assert f"--parent-intent-id {current_intent_id}" in prompt


def test_position_parent_lookup_ignores_legacy_operator_raw_message(monkeypatch):
    module = _load_monitor()
    seen = []
    key = ("account-a", "MUUSDT", "long")
    legacy_row = _position_parent_row(
        "MUUSDT",
        "long",
        authorization=False,
        legacy_raw=["operator", "legacy-operator-ref", "operator"],
    )

    def fake_q(sql):
        seen.append(sql)
        return [legacy_row]

    monkeypatch.setattr(module, "q", fake_q)

    authorizations = module._position_parent_authorizations({key})

    assert authorizations == {}
    assert "raw_messages" not in seen[0]
    assert "interval '30 days'" in seen[0]


def test_load_intent_contexts_accepts_uuid_and_hex_and_drops_invalid_sql(monkeypatch):
    module = _load_monitor()
    captured = []

    def fake_q(sql):
        captured.append(sql)
        return []

    monkeypatch.setattr(module, "q", fake_q)

    module._load_intent_contexts({
        "11111111-1111-1111-1111-111111111111",
        "22222222222222222222222222222222",
        "bad'); DROP TABLE trade_intents; --",
    })

    assert len(captured) == 1
    assert "11111111-1111-1111-1111-111111111111" in captured[0]
    assert "22222222-2222-2222-2222-222222222222" in captured[0]
    assert "DROP TABLE" not in captured[0]


def test_load_intent_contexts_skips_query_when_all_ids_are_invalid(monkeypatch):
    module = _load_monitor()
    calls = []
    monkeypatch.setattr(
        module,
        "q",
        lambda sql: calls.append(sql) or [],
    )

    contexts = module._load_intent_contexts({"bad-id", "' OR true --"})

    assert contexts == {}
    assert calls == []


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
        if "FROM trade_intents ti" in sql:
            return [_position_parent_row("BTCUSDT", "long")]
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


def test_naked_account_b_prompt_requires_account_and_position_side(monkeypatch):
    module = _load_monitor()
    prompts = []
    mirror = _mirror_row(
        "account-b",
        [_position("ETHUSDT", "LONG", "2.5")],
    )

    def fake_q(sql):
        if "exchange_state_mirror" in sql:
            return [mirror]
        if "OrderFilled" in sql:
            return []
        if "FROM trade_intents ti" in sql:
            return [
                _position_parent_row(
                    "ETHUSDT",
                    "long",
                    account_id="account-b",
                )
            ]
        raise AssertionError(sql)

    monkeypatch.setattr(module, "q", fake_q)
    monkeypatch.setattr(
        module,
        "wake_hermes",
        lambda prompt, name, dry_run: prompts.append(prompt) or True,
    )

    module.sweep_naked({}, dry_run=False, now_ts=1_000)

    assert len(prompts) == 1
    assert "set-sl ETHUSDT" in prompts[0]
    assert "--account account-b" in prompts[0]
    assert "--side long" in prompts[0]
    assert "parent_intent_id=" in prompts[0]
    assert "source_message_id=signal-42" in prompts[0]
    assert "authorized_by_type=channel" in prompts[0]
    assert "authorized_by_id=-100123" in prompts[0]


def test_mu_naked_without_parent_authorization_only_alerts(monkeypatch):
    module = _load_monitor()
    prompts = []
    alerts = []
    mirror = _mirror_row(
        "account-a",
        [_position("MUUSDT", "LONG", "150")],
    )

    def fake_q(sql):
        if "exchange_state_mirror" in sql:
            return [mirror]
        if "OrderFilled" in sql:
            return []
        if "FROM trade_intents ti" in sql:
            return []
        raise AssertionError(sql)

    monkeypatch.setattr(module, "q", fake_q)
    monkeypatch.setattr(
        module,
        "wake_hermes",
        lambda prompt, name, dry_run: prompts.append(prompt) or True,
    )
    monkeypatch.setattr(
        module,
        "tg_send_direct",
        lambda text: alerts.append(text) or True,
    )

    state = {}
    module.sweep_naked(state, dry_run=False, now_ts=1_000)

    assert prompts == []
    assert len(alerts) == 1
    assert "MUUSDT" in alerts[0]
    assert "缺少完整用户或频道父授权" in alerts[0]
    assert "set-sl" not in alerts[0]
    assert "replace-tp" not in alerts[0]
    assert " cancel " not in alerts[0]
    assert "naked:account-a:MUUSDT:long" not in state
    assert state["authalert:naked:account-a:MUUSDT:long"] == 1_000


def test_naked_authorization_backoff_skips_parent_lookup(monkeypatch):
    module = _load_monitor()
    mirror = _mirror_row(
        "account-a",
        [_position("MUUSDT", "LONG", "150")],
    )
    queries = []

    def fake_q(sql):
        queries.append(sql)
        if "exchange_state_mirror" in sql:
            return [mirror]
        if "OrderFilled" in sql:
            return []
        if "FROM trade_intents ti" in sql:
            raise AssertionError("authorization lookup must honor authalert backoff")
        raise AssertionError(sql)

    monkeypatch.setattr(module, "q", fake_q)
    monkeypatch.setattr(
        module,
        "tg_send_direct",
        lambda text: (_ for _ in ()).throw(
            AssertionError("deduplicated auth alert must stay quiet")
        ),
    )
    state = {
        "authalert:naked:account-a:MUUSDT:long": 900,
    }

    module.sweep_naked(state, dry_run=False, now_ts=1_000)

    assert not any("FROM trade_intents ti" in sql for sql in queries)


def test_prune_state_removes_expired_authalert_prefix():
    module = _load_monitor()
    state = {
        "authalert:naked:account-a:MUUSDT:long": 100,
        "authalert:naked:account-a:BTCUSDT:long": 9_900,
    }

    module.prune_state(
        state,
        live_ids=set(),
        now_ts=10_000,
        max_age=1_000,
    )

    assert "authalert:naked:account-a:MUUSDT:long" not in state
    assert "authalert:naked:account-a:BTCUSDT:long" in state


def test_price_remediation_without_parent_authorization_only_alerts(monkeypatch):
    module = _load_monitor()
    prompts = []
    alerts = []
    cid = "B" + "c" * 32 + "12"
    order = {
        "client_order_id": cid,
        "account_id": "account-a",
        "symbol": "BTCUSDT",
        "price": 70_000,
        "is_stop_loss": False,
        "authorization": {
            "parent_intent_id": "",
            "source_message_id": "",
            "authorized_by_type": "",
        },
    }
    monkeypatch.setattr(module, "_live_protection_rows", lambda: [order])
    monkeypatch.setattr(
        module,
        "wake_hermes",
        lambda prompt, name, dry_run: prompts.append(prompt) or True,
    )
    monkeypatch.setattr(
        module,
        "tg_send_direct",
        lambda text: alerts.append(text) or True,
    )

    state = {}
    module.sweep_price_alerts(
        state,
        dry_run=False,
        fetch=lambda symbol: json.dumps({"markPrice": "70200"}).encode(),
        now_ts=100_000,
    )

    assert prompts == []
    assert len(alerts) == 1
    assert "BTCUSDT" in alerts[0]
    assert "缺少完整用户或频道父授权" in alerts[0]
    assert "set-sl" not in alerts[0]
    assert "replace-tp" not in alerts[0]
    assert " cancel " not in alerts[0]
    assert state[f"authalert:alert:{cid}:approach"] == 100_000


def test_fill_remediation_without_parent_authorization_only_alerts(monkeypatch):
    module = _load_monitor()
    prompts = []
    alerts = []
    cid = "B" + "e" * 32 + "12"

    def fake_q(sql):
        if "FROM execution_events" in sql:
            return [[cid, "account-a", "BTCUSDT-PERP", "0.01", "70000"]]
        if "FROM trade_intents ti" in sql:
            return [
                _intent_context_row(
                    module.intent_uuid_of(cid),
                    "open_position",
                    "BTCUSDT",
                    account_id="account-a",
                    authorization=False,
                    legacy_raw=["operator", "legacy-fill-ref", "operator"],
                )
            ]
        raise AssertionError(sql)

    monkeypatch.setattr(module, "q", fake_q)
    monkeypatch.setattr(
        module,
        "wake_hermes",
        lambda prompt, name, dry_run: prompts.append(prompt) or True,
    )
    monkeypatch.setattr(
        module,
        "tg_send_direct",
        lambda text: alerts.append(text) or True,
    )

    state = {}
    module.sweep_fill_alerts(state, dry_run=False, now_ts=1_000)

    assert prompts == []
    assert len(alerts) == 1
    assert cid in alerts[0]
    assert "缺少完整用户或频道父授权" in alerts[0]
    assert "set-sl" not in alerts[0]
    assert "replace-tp" not in alerts[0]
    assert " cancel " not in alerts[0]
    assert state[f"authalert:fill:{cid}"] == 1_000


def test_ttl_wake_and_exhausted_prompts_preserve_account_b(monkeypatch):
    module = _load_monitor()
    wake_cid = "B" + "a" * 32 + "01"
    exhausted_cid = "B" + "b" * 32 + "02"
    prompts = []
    rows = [
        [wake_cid, "account-b", "BTCUSDT-PERP", "49"],
        [exhausted_cid, "account-b", "ETHUSDT-PERP", "97"],
    ]

    def fake_q(sql):
        if "FROM orders_projection" in sql:
            return rows
        if "FROM trade_intents ti" in sql:
            return [
                _intent_context_row(
                    module.intent_uuid_of(wake_cid),
                    "open_position",
                    "BTCUSDT",
                ),
                _intent_context_row(
                    module.intent_uuid_of(exhausted_cid),
                    "open_position",
                    "ETHUSDT",
                ),
            ]
        raise AssertionError(sql)

    monkeypatch.setattr(module, "q", fake_q)
    monkeypatch.setattr(
        module,
        "_exchange_open_order_ids",
        lambda: {"account-b": {wake_cid, exhausted_cid}},
    )
    monkeypatch.setattr(
        module,
        "wake_hermes",
        lambda prompt, name, dry_run: prompts.append(prompt) or True,
    )
    state = {
        f"ttl:{exhausted_cid}": {"renewals": 1},
    }

    module.sweep_ttl(state, dry_run=False, now_ts=1_000)

    assert len(prompts) == 2
    assert (
        f"cancel BTCUSDT --account account-b --order {wake_cid}"
        in prompts[0]
    )
    assert (
        f"cancel ETHUSDT --account account-b --order {exhausted_cid}"
        in prompts[1]
    )
    assert "不可再续" in prompts[1]
    assert "parent_intent_id=" in prompts[0]
    assert "source_message_id=signal-42" in prompts[0]
    assert "authorized_by_type=channel" in prompts[0]
    assert "authorized_by_id=-100123" in prompts[0]


def test_ttl_legacy_operator_raw_message_without_plan_authorization_only_alerts(
    monkeypatch,
):
    module = _load_monitor()
    cid = "B" + "d" * 32 + "01"
    prompts = []
    alerts = []
    context_queries = []

    def fake_q(sql):
        if "FROM orders_projection" in sql:
            return [[cid, "account-a", "SOLUSDT-PERP", "49"]]
        if "FROM trade_intents ti" in sql:
            context_queries.append(sql)
            return [
                _intent_context_row(
                    module.intent_uuid_of(cid),
                    "open_position",
                    "SOLUSDT",
                    account_id="account-a",
                    authorization=False,
                    legacy_raw=["operator", "legacy-operator-ref", "operator"],
                )
            ]
        raise AssertionError(sql)

    monkeypatch.setattr(module, "q", fake_q)
    monkeypatch.setattr(
        module,
        "_exchange_open_order_ids",
        lambda: {"account-a": {cid}},
    )
    monkeypatch.setattr(
        module,
        "wake_hermes",
        lambda prompt, name, dry_run: prompts.append(prompt) or True,
    )
    monkeypatch.setattr(
        module,
        "tg_send_direct",
        lambda text: alerts.append(text) or True,
    )

    state = {}
    module.sweep_ttl(state, dry_run=False, now_ts=1_000)

    assert prompts == []
    assert len(alerts) == 1
    assert cid in alerts[0]
    assert "缺少完整用户或频道父授权" in alerts[0]
    assert "cancel SOLUSDT" not in alerts[0]
    assert f"ttl:{cid}" not in state
    assert not any(key.startswith(f"ttlwake:{cid}") for key in state)
    assert state[f"authalert:ttl:{cid}"] == 1_000
    assert len(context_queries) == 1
    assert "raw_messages" not in context_queries[0]


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


def test_pending_cancel_query_limits_lookback_window(monkeypatch):
    module = _load_monitor()
    seen = []
    monkeypatch.setattr(module, "q", lambda sql: seen.append(sql) or [])

    module._pending_cancel_rows()

    assert f"interval '{module.PENDING_CANCEL_LOOKBACK_HOURS} hours'" in seen[0]


def test_disappeared_order_alerts_once_and_stops(monkeypatch):
    """撤单丢失是尘埃落定的存量债,重播无人可行动 (2026-07-25 每晚 42 条刷屏)。"""
    module = _load_monitor()
    sent = []
    mirror = _mirror_row("account-a", [])
    monkeypatch.setattr(module, "q", _pending_cancel_q(mirror))
    monkeypatch.setattr(module, "tg_send_direct", lambda text: sent.append(text) or True)

    state = {}
    module.sweep_pending_cancels(state, dry_run=False, now_ts=701)
    assert len(sent) == 1
    assert "已消失" in sent[0]

    # 去重窗过期后仍不得重播
    module.sweep_pending_cancels(state, dry_run=False, now_ts=701 + module.ALERT_DEDUP_SECONDS + 1)

    assert len(sent) == 1


def test_still_open_order_keeps_realerting_after_dedup(monkeypatch):
    """仍挂在交易所的撤单未确认是活风险,不能被一次性抑制。"""
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
    module.sweep_pending_cancels(state, dry_run=False, now_ts=701 + module.ALERT_DEDUP_SECONDS + 1)

    assert len(sent) == 2
    assert all("仍挂着" in text for text in sent)


def test_still_open_beyond_lookback_is_retained_and_replayed_daily(monkeypatch):
    module = _load_monitor()
    sent = []
    mirror = _mirror_row(
        "account-a",
        [],
        open_orders=[{"client_order_id": "ancient", "symbol": "BTCUSDT"}],
    )

    def fake_q(sql):
        if "OrderPendingCancel" in sql:
            return []
        if "exchange_state_mirror" in sql:
            return [mirror]
        raise AssertionError(sql)

    monkeypatch.setattr(module, "q", fake_q)
    monkeypatch.setattr(module, "tg_send_direct", lambda text: sent.append(text) or True)

    pending_at = 1_000.0
    first_replay_at = (
        pending_at
        + module.PENDING_CANCEL_LOOKBACK_HOURS * 3600
        + module.ALERT_DEDUP_SECONDS
        + 1
    )
    state = {
        "pendingcancel:account-a:ancient": {
            "account_id": "account-a",
            "client_order_id": "ancient",
            "event_id": "old-event",
            "pending_at": pending_at,
            "last_outcome": "still_open",
            "last_alert_at": first_replay_at - module.ALERT_DEDUP_SECONDS - 1,
        },
    }

    module.sweep_pending_cancels(state, dry_run=False, now_ts=first_replay_at)
    module.sweep_pending_cancels(
        state,
        dry_run=False,
        now_ts=first_replay_at + module.ALERT_DEDUP_SECONDS - 1,
    )
    module.sweep_pending_cancels(
        state,
        dry_run=False,
        now_ts=first_replay_at + module.ALERT_DEDUP_SECONDS + 1,
    )

    assert len(sent) == 2
    assert all("仍挂着" in text for text in sent)
    assert "pendingcancel:account-a:ancient" in state
    assert not state["pendingcancel:account-a:ancient"].get("resolved")


def test_pending_cancel_beyond_lookback_survives_stale_mirror(monkeypatch):
    module = _load_monitor()
    sent = []
    stale_age = str(module.MIRROR_MAX_AGE_SECONDS + 1)
    mirror = _mirror_row("account-a", [], age=stale_age)

    def fake_q(sql):
        if "OrderPendingCancel" in sql:
            return []
        if "exchange_state_mirror" in sql:
            return [mirror]
        raise AssertionError(sql)

    monkeypatch.setattr(module, "q", fake_q)
    monkeypatch.setattr(module, "tg_send_direct", lambda text: sent.append(text) or True)
    key = "pendingcancel:account-a:ancient"
    entry = {
        "account_id": "account-a",
        "client_order_id": "ancient",
        "event_id": "old-event",
        "pending_at": 1_000.0,
        "last_outcome": "still_open",
        "last_alert_at": 2_000.0,
    }
    state = {key: dict(entry)}
    now_ts = 1_000.0 + module.PENDING_CANCEL_LOOKBACK_HOURS * 3600 + 1

    module.sweep_pending_cancels(state, dry_run=False, now_ts=now_ts)

    assert sent == []
    assert state[key] == entry


def test_pending_cancel_beyond_lookback_survives_mirror_query_failure(monkeypatch):
    module = _load_monitor()
    sent = []

    def fake_q(sql):
        if "OrderPendingCancel" in sql:
            return []
        if "exchange_state_mirror" in sql:
            raise RuntimeError("mirror unavailable")
        raise AssertionError(sql)

    monkeypatch.setattr(module, "q", fake_q)
    monkeypatch.setattr(module, "tg_send_direct", lambda text: sent.append(text) or True)
    key = "pendingcancel:account-b:ancient"
    entry = {
        "account_id": "account-b",
        "client_order_id": "ancient",
        "event_id": "old-event",
        "pending_at": 1_000.0,
        "last_outcome": "still_open",
        "last_alert_at": 2_000.0,
    }
    state = {key: dict(entry)}
    now_ts = 1_000.0 + module.PENDING_CANCEL_LOOKBACK_HOURS * 3600 + 1

    module.sweep_pending_cancels(state, dry_run=False, now_ts=now_ts)

    assert sent == []
    assert state[key] == entry
