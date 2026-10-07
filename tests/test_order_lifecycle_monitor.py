"""Read-only OLM notification boundary; all senders, SQL and network are local fakes."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "order_lifecycle_monitor.py"
ROBOT = "B" + "a" * 32 + "01"
ROBOT_TWO = "B" + "b" * 32 + "02"
STOP = "B" + "a" * 32 + "11"
NOW = 200_000.0


@pytest.fixture()
def monitor(monkeypatch):
    spec = importlib.util.spec_from_file_location("olm_readonly_tests", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.sent = []
    module.queries = []
    monkeypatch.setattr(module, "tg_send_direct", lambda text: module.sent.append(text) or True)
    monkeypatch.setattr(module, "q", lambda sql: module.queries.append(sql) or [])
    monkeypatch.setattr(module, "probe_llm", lambda *args: True)
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("unexpected network call"))
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected process execution"))
    return module


def _snapshot(*ids, fresh=True, algo=False):
    orders = [{"clientOrderId": cid} for cid in ids]
    return {"fresh": fresh, "open_orders": [] if algo else orders, "algo_orders": orders if algo else []}


@pytest.mark.parametrize("cid,owned", [
    (ROBOT, True), (STOP, True), ("B" + "f" * 32 + "99", True),
    ("aos_manual", False), ("stToAg_manual", False), ("B" + "z" * 32 + "01", False),
    ("B" + "A" * 32 + "01", False), (ROBOT + "\n", False), (None, False),
])
def test_robot_ownership_is_exact(monitor, cid, owned):
    assert monitor.is_system_id(cid) is owned


def test_ttl_only_notifies_live_robot_entries_and_never_renews(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "q", lambda sql: [
        [ROBOT, "account-a", "BTCUSDT-PERP.BINANCE", "49", "open_position"],
        [ROBOT_TWO, "account-a", "BTCUSDT-PERP.BINANCE", "100", "add_position"],
        [STOP, "account-a", "BTCUSDT-PERP.BINANCE", "99", "open_position"],
        ["aos_manual", "account-a", "BTCUSDT", "99", "open_position"],
        ["stToAg_manual", "account-a", "BTCUSDT", "99", "open_position"],
        [ROBOT, "account-b", "BTCUSDT", "99", "move_stop_loss"],
        [ROBOT, "account-c", "BTCUSDT", "99", "open_position"],
    ])
    monkeypatch.setattr(monitor, "_exchange_state_snapshots", lambda: {
        "account-a": _snapshot(ROBOT, ROBOT_TWO, STOP, "aos_manual"),
        "account-b": _snapshot(ROBOT), "account-c": _snapshot(ROBOT, fresh=False),
    })
    state = {}
    monitor.sweep_ttl(state, False, NOW)
    monitor.sweep_ttl(state, False, NOW + 10)
    assert len(monitor.sent) == 2
    assert all("未撤单、未续期" in text and "自动到期撤单尚未接管" in text for text in monitor.sent)
    assert set(state) == {f"ttlalert:account-a:{ROBOT}", f"ttlalert:account-a:{ROBOT_TWO}"}


def test_ttl_absence_never_terminalizes_projection_or_alerts_to_cancel(monitor, monkeypatch):
    def query(sql):
        monitor.queries.append(sql)
        return [[ROBOT, "account-a", "BTCUSDT", "100", "open_position"]]
    monkeypatch.setattr(monitor, "q", query)
    monkeypatch.setattr(monitor, "_exchange_state_snapshots", lambda: {"account-a": _snapshot()})
    monitor.sweep_ttl({}, False, NOW)
    assert monitor.sent == []
    assert all(sql.lstrip().startswith("SELECT") for sql in monitor.queries)


def test_failed_ttl_delivery_does_not_consume_dedupe(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "q", lambda sql: [[ROBOT, "account-a", "BTCUSDT", "49", "open_position"]])
    monkeypatch.setattr(monitor, "_exchange_state_snapshots", lambda: {"account-a": _snapshot(ROBOT)})
    monkeypatch.setattr(monitor, "tg_send_direct", lambda text: False)
    state = {}
    monitor.sweep_ttl(state, False, NOW)
    assert state == {}
    monkeypatch.setattr(monitor, "tg_send_direct", lambda text: monitor.sent.append(text) or True)
    monitor.sweep_ttl(state, False, NOW + 1)
    assert len(monitor.sent) == 1


@pytest.mark.parametrize("payload,age,fresh", [
    ({"open_orders": [], "algo_orders": []}, "0", True),
    ({"open_orders": [], "algo_orders": [{"clientAlgoId": STOP}]}, "10", True),
    ({"open_orders": []}, "10", False),
    ({"open_orders": None, "algo_orders": []}, "10", False),
    ({"open_orders": "broken", "algo_orders": []}, "10", False),
    ({"open_orders": [None], "algo_orders": []}, "10", False),
    ({"open_orders": [{}], "algo_orders": []}, "10", False),
    ({"open_orders": [], "algo_orders": []}, "301", False),
    ({"open_orders": [], "algo_orders": []}, "NaN", False),
    ({"open_orders": [], "algo_orders": []}, "-1", False),
    ([], "10", False),
])
def test_mirror_distinguishes_complete_empty_from_missing_or_stale(monitor, monkeypatch, payload, age, fresh):
    monkeypatch.setattr(monitor, "q", lambda sql: [["account-a", age, json.dumps(payload)]])
    snapshot = monitor._exchange_state_snapshots()["account-a"]
    assert snapshot["fresh"] is fresh
    if fresh and snapshot["algo_orders"]:
        assert monitor._snapshot_open_order_ids(snapshot) == {STOP}


def test_reconcile_is_account_scoped_and_accepts_fresh_empty_mirror(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "q", lambda sql: [
        ["account-a", ROBOT], ["account-b", ROBOT], ["account-c", ROBOT_TWO],
        ["account-a", "aos_manual"], ["account-a", "stToAg_manual"],
    ])
    monkeypatch.setattr(monitor, "_exchange_state_snapshots", lambda: {
        "account-a": _snapshot(), "account-b": _snapshot(ROBOT, STOP, "aos_manual", algo=True),
        "account-c": _snapshot(fresh=False),
    })
    state = {}
    monitor.sweep_reconcile(state, False, NOW)
    assert len(monitor.sent) == 2
    assert "account-a" in monitor.sent[0] and "交易所镜像未见" in monitor.sent[0]
    assert "account-b" in monitor.sent[1] and STOP in monitor.sent[1]
    assert ROBOT not in monitor.sent[1]
    assert "account-c" not in "".join(monitor.sent)
    assert "aos_" not in "".join(monitor.sent) and "stToAg_" not in "".join(monitor.sent)
    monitor.sweep_reconcile(state, False, NOW + 1)
    assert len(monitor.sent) == 2


def test_manual_only_projection_and_mirror_are_quiet(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "q", lambda sql: [["account-a", "aos_manual"]])
    monkeypatch.setattr(monitor, "_exchange_state_snapshots", lambda: {"account-a": _snapshot("stToAg_manual")})
    monitor.sweep_reconcile({}, False, NOW)
    assert monitor.sent == []


def test_reconcile_failure_and_dry_run_preserve_notification_eligibility(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "q", lambda sql: [["account-a", ROBOT]])
    monkeypatch.setattr(monitor, "_exchange_state_snapshots", lambda: {"account-a": _snapshot()})
    state = {}
    monitor.sweep_reconcile(state, True, NOW)
    assert state == {} and monitor.sent == []
    monkeypatch.setattr(monitor, "tg_send_direct", lambda text: False)
    monitor.sweep_reconcile(state, False, NOW)
    assert state == {}


def test_pending_cancel_discards_manual_records_even_after_restart(monitor, monkeypatch, tmp_path):
    pending = NOW - 700
    state = {"pendingcancel:account-a:aos_manual": {
        "account_id": "account-a", "client_order_id": "aos_manual", "pending_at": pending,
    }}
    path = str(tmp_path / "state.json")
    monitor.save_state(state, path)
    restored = monitor.load_state(path)
    monkeypatch.setattr(monitor, "_pending_cancel_rows", lambda: [
        ["account-a", "stToAg_manual", "manual-event", str(pending), ""],
    ])
    monitor.sweep_pending_cancels(restored, False, NOW)
    assert restored == {} and monitor.sent == []


def test_pending_robot_risk_survives_restart_and_stale_mirror(monitor, monkeypatch, tmp_path):
    pending = NOW - 700
    monkeypatch.setattr(monitor, "_pending_cancel_rows", lambda: [["account-a", ROBOT, "event-1", str(pending), ""]])
    monkeypatch.setattr(monitor, "_exchange_state_snapshots", lambda: {"account-a": _snapshot(ROBOT, fresh=False)})
    state = {}
    monitor.sweep_pending_cancels(state, False, NOW)
    assert monitor.sent == []
    path = str(tmp_path / "state.json")
    monitor.save_state(state, path)
    restored = monitor.load_state(path)
    monkeypatch.setattr(monitor, "_pending_cancel_rows", lambda: [])
    monkeypatch.setattr(monitor, "_exchange_state_snapshots", lambda: {"account-a": _snapshot(ROBOT)})
    monitor.sweep_pending_cancels(restored, False, NOW + 200_000)
    assert len(monitor.sent) == 1 and "仍挂着" in monitor.sent[0]
    monitor.sweep_pending_cancels(restored, False, NOW + 200_001)
    assert len(monitor.sent) == 1
    monitor.sweep_pending_cancels(restored, False, NOW + 200_000 + monitor.ALERT_DEDUP_SECONDS)
    assert len(monitor.sent) == 2


@pytest.mark.parametrize("terminal,word", [("", "已消失"), ("OrderFilled", "已成交")])
def test_pending_cancel_disappearance_does_not_fabricate_cancel_truth(monitor, monkeypatch, terminal, word):
    monkeypatch.setattr(monitor, "_pending_cancel_rows", lambda: [["account-a", ROBOT, "event-1", str(NOW - 700), terminal]])
    monkeypatch.setattr(monitor, "_exchange_state_snapshots", lambda: {"account-a": _snapshot()})
    state = {}
    monitor.sweep_pending_cancels(state, False, NOW)
    assert len(monitor.sent) == 1 and word in monitor.sent[0]
    assert state[f"pendingcancel:account-a:{ROBOT}"]["resolved"] is True
    assert monitor.queries == []


def test_pending_cancel_failure_does_not_record_delivery_or_resolution(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "_pending_cancel_rows", lambda: [["account-a", ROBOT, "event-1", str(NOW - 700), ""]])
    monkeypatch.setattr(monitor, "_exchange_state_snapshots", lambda: {"account-a": _snapshot()})
    monkeypatch.setattr(monitor, "tg_send_direct", lambda text: False)
    state = {}
    monitor.sweep_pending_cancels(state, False, NOW)
    entry = state[f"pendingcancel:account-a:{ROBOT}"]
    assert "resolved" not in entry and "last_alert_at" not in entry


def test_resolved_pending_cancel_tombstone_survives_prune_until_query_window_ends(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "_pending_cancel_rows", lambda: [["account-a", ROBOT, "event-1", str(NOW - 700), ""]])
    monkeypatch.setattr(monitor, "_exchange_state_snapshots", lambda: {"account-a": _snapshot()})
    state = {}
    monitor.sweep_pending_cancels(state, False, NOW)
    monitor.prune_state(state, NOW + 60)
    monitor.sweep_pending_cancels(state, False, NOW + 60)
    assert len(monitor.sent) == 1
    monkeypatch.setattr(monitor, "_pending_cancel_rows", lambda: [])
    monitor.sweep_pending_cancels(state, False, NOW + 200_000)
    assert state == {}


def test_intent_stall_and_daily_report_still_notify_directly(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "q", lambda sql: [["intent-1", "account-a", "BTCUSDT", "add_position", "600"]])
    state = {}
    monitor.sweep_intent_stalls(state, False, NOW)
    monitor.sweep_intent_stalls(state, False, NOW + 1)
    assert len(monitor.sent) == 1 and "Intent 消费停滞" in monitor.sent[0]
    monkeypatch.setattr(monitor, "_daily_report_file_exists", lambda date: False)
    monkeypatch.setattr(monitor, "_report_health", lambda: {})
    noon = monitor.datetime(2026, 9, 15, 16, tzinfo=monitor.timezone.utc).timestamp()
    monitor.sweep_daily_report(state, False, noon)
    monitor.sweep_daily_report(state, False, noon + 1)
    assert len(monitor.sent) == 2 and "日报缺失" in monitor.sent[1]


def test_failed_protection_notification_retries_without_dedupe(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "q", lambda sql: [["freeze-1", "account-a", "intent-1", STOP, "{}"]])
    monkeypatch.setattr(monitor, "tg_send_direct", lambda text: False)
    state = {}
    monitor.sweep_protection_events(state, False, NOW)
    assert state == {}
    monkeypatch.setattr(monitor, "tg_send_direct", lambda text: monitor.sent.append(text) or True)
    monitor.sweep_protection_events(state, False, NOW + 1)
    assert len(monitor.sent) == 1


@pytest.mark.parametrize("status,readiness,age,command,command_age,alerts", [
    ("ACTIVE", "true", "0", "", "-1", 0),
    ("HALTED", "true", "0", "HALT", "10", 0),
    ("HALTED", "true", "0", "HALT", "90000", 1),
    ("ACTIVE", "false", "0", "", "-1", 1),
    ("HALTED", "false", "0", "HALT", "10", 1),
    ("ACTIVE", "true", "600", "", "-1", 1),
])
def test_node_faults_use_direct_notifications_and_keep_operator_halt_semantics(
    monitor, monkeypatch, status, readiness, age, command, command_age, alerts,
):
    monkeypatch.setattr(monitor, "q", lambda sql: [["node-a", status, readiness, "", age, command, command_age]])
    state = {}
    monitor.sweep_node_health(state, False, NOW)
    monitor.sweep_node_health(state, False, NOW + 1)
    assert len(monitor.sent) == alerts
    if alerts:
        assert "禁止自动 RESUME" in monitor.sent[0]


def test_stale_node_dry_run_never_sends_and_failed_delivery_retries(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "q", lambda sql: [["node-a", "ACTIVE", "true", "", "600", "", "-1"]])
    state = {}
    monitor.sweep_node_health(state, True, NOW)
    assert monitor.sent == []
    assert "nodehalt:node-a:heartbeat_stale" not in state
    monkeypatch.setattr(monitor, "tg_send_direct", lambda text: False)
    monitor.sweep_node_health(state, False, NOW)
    assert "nodehalt:node-a:heartbeat_stale" not in state
    monkeypatch.setattr(monitor, "tg_send_direct", lambda text: monitor.sent.append(text) or True)
    monitor.sweep_node_health(state, False, NOW + 1)
    assert len(monitor.sent) == 1


def test_protection_exception_events_replace_unattributed_net_position_wakes(monitor, monkeypatch):
    def query(sql):
        monitor.queries.append(sql)
        assert "ProtectionFrozen" in sql and "ProtectionWatchdogSymbolStopped" in sql
        return [
            ["frozen-1", "account-a", "intent-1", STOP, json.dumps({"reason": "revisions_exhausted"})],
            ["watchdog-1", "account-b", "intent-2", "", json.dumps({"reason": "protection_order_missing"})],
            ["manual-1", "account-a", "", "aos_manual", "{}"],
        ]
    monkeypatch.setattr(monitor, "q", query)
    state = {}
    monitor.sweep_protection_events(state, False, NOW)
    monitor.sweep_protection_events(state, False, NOW + 1)
    assert len(monitor.sent) == 2
    assert "revisions_exhausted" in monitor.sent[0]
    assert "protection_order_missing" in monitor.sent[1]


def test_brain_failed_delivery_does_not_consume_cooldown(monitor, monkeypatch):
    state = {}
    monkeypatch.setattr(monitor, "tg_send_direct", lambda text: False)
    monitor.sweep_brain(state, False, NOW, prober=lambda *args: False)
    monitor.sweep_brain(state, False, NOW + 1, prober=lambda *args: False)
    assert "brainalert:critical" not in state
    monkeypatch.setattr(monitor, "tg_send_direct", lambda text: monitor.sent.append(text) or True)
    monitor.sweep_brain(state, False, NOW + 2, prober=lambda *args: False)
    assert len(monitor.sent) == 1
    monitor.sweep_brain(state, False, NOW + 3, prober=lambda *args: False)
    assert len(monitor.sent) == 1


def test_state_prune_removes_old_agent_bookkeeping_but_retains_unresolved_robot_risk(monitor):
    pending_key = f"pendingcancel:account-a:{ROBOT}"
    state = {"ttl:old": {"renewals": 1}, "ttlwake:old:1": NOW,
             "authalert:naked:account-a:BTCUSDT:long": NOW, "recon:ghost:old": NOW,
             pending_key: {"client_order_id": ROBOT, "pending_at": 1},
             "pendingcancel:account-a:aos_manual": {"client_order_id": "aos_manual"}}
    monitor.prune_state(state, NOW)
    assert set(state) == {pending_key}


def test_main_has_no_model_management_price_fill_or_naked_work(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "load_state", lambda: {})
    monkeypatch.setattr(monitor, "save_state", lambda state: None)
    monkeypatch.setattr(monitor, "_daily_report_file_exists", lambda *args: True)
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--once", "--dry-run"])
    monitor.main()
    assert monitor.sent == []
    assert all(sql.lstrip().startswith(("SELECT", "WITH")) for sql in monitor.queries)
    text = SCRIPT.read_text()
    assert "wake_hermes" not in text and "run_hermes" not in text
    assert "UPDATE orders_projection" not in text and "v3_trade.py" not in text
    assert "_position_parent_authorizations" not in text


def test_database_client_enforces_readonly_session(monitor, monkeypatch):
    spec = importlib.util.spec_from_file_location("olm_query_test", SCRIPT)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="account-a|0\n", stderr="")
    monkeypatch.setattr(fresh.subprocess, "run", run)
    assert fresh.q("SELECT account_id, 0 FROM exchange_state_mirror") == [["account-a", "0"]]
    assert "PGOPTIONS=-c default_transaction_read_only=on" in calls[0]
