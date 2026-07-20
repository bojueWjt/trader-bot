from __future__ import annotations

import json
import sys

import pytest


MANAGEMENT_CASES = [
    (
        ["close", "BTCUSDT"],
        "close_position",
    ),
    (
        ["partial", "BTCUSDT", "--quantity", "0.01"],
        "partial_close",
    ),
    (
        ["set-sl", "BTCUSDT", "--sl", "60000"],
        "move_stop_loss",
    ),
    (
        ["set-tps", "BTCUSDT", "--tp", "70000", "--qty", "0.01"],
        "replace_take_profits",
    ),
    (
        ["cancel", "BTCUSDT", "--order", "B" + "a" * 32 + "01"],
        "cancel_order",
    ),
]


def test_open_requires_channel_and_does_not_send_request(
    load_trade_module, monkeypatch, capsys
):
    module = load_trade_module()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            "open",
            "BTCUSDT",
            "short",
            "--notional",
            "100",
            "--reason",
            "test",
            "--ref",
            "tg-sig-c1002136478186-m5026",
        ],
    )
    monkeypatch.setattr(
        module,
        "_call",
        lambda *args, **kwargs: pytest.fail("request sent without channel"),
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    output = json.loads(capsys.readouterr().out)
    assert exc.value.code == 1
    assert "--channel is required" in output["error"]


def test_open_forwards_canonical_signal_provenance(load_trade_module, monkeypatch):
    module = load_trade_module()
    calls = []

    def fake_call(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "POST":
            return {"intent_id": "intent-1", "status": "approved", "replay": False}
        return {
            "intent": {"status": "approved"},
            "orders": [],
            "execution_events": [],
            "open_positions": [],
        }

    monkeypatch.setattr(module, "_call", fake_call)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            "open",
            "BTCUSDT",
            "short",
            "--notional",
            "100",
            "--reason",
            "test",
            "--ref",
            "tg-sig-c1002136478186-m5026",
            "--channel",
            "-1002136478186",
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["client_ref"] == "tg-sig-c1002136478186-m5026"
    assert payload["source_channel"] == "-1002136478186"


def test_open_rejects_noncanonical_client_ref(load_trade_module, monkeypatch, capsys):
    module = load_trade_module()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            "open",
            "BTCUSDT",
            "short",
            "--notional",
            "100",
            "--reason",
            "test",
            "--ref",
            "tg-5026",
            "--channel",
            "-1002136478186",
        ],
    )
    monkeypatch.setattr(
        module,
        "_call",
        lambda *args, **kwargs: pytest.fail("request sent with noncanonical ref"),
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    output = json.loads(capsys.readouterr().out)
    assert exc.value.code == 1
    assert "canonical" in output["error"]


def test_open_forwards_operator_provenance(load_trade_module, monkeypatch):
    module = load_trade_module()
    calls = []

    def fake_call(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "POST":
            return {"intent_id": "intent-1", "status": "approved", "replay": False}
        return {
            "intent": {"status": "approved"},
            "orders": [],
            "execution_events": [],
            "open_positions": [],
        }

    monkeypatch.setattr(module, "_call", fake_call)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            "open",
            "BTCUSDT",
            "short",
            "--notional",
            "100",
            "--reason",
            "test",
            "--ref",
            "operator-btc-short-0719",
            "--channel",
            "operator",
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["client_ref"] == "operator-btc-short-0719"
    assert payload["source_channel"] == "operator"


@pytest.mark.parametrize(("command", "expected_action"), MANAGEMENT_CASES)
def test_management_commands_forward_attribution_context(
    load_trade_module, monkeypatch, command, expected_action
):
    module = load_trade_module()
    calls = []

    def fake_call(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "POST":
            return {"intent_id": "intent-1", "status": "approved", "replay": False}
        return {
            "intent": {"status": "approved"},
            "orders": [],
            "execution_events": [],
            "open_positions": [],
        }

    monkeypatch.setattr(module, "_call", fake_call)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            *command,
            "--reason",
            "test",
            "--ref",
            "operation-5026",
            "--channel",
            "-1002136478186",
            "--entry-ref",
            "tg-sig-c1002136478186-m5026",
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["action"] == expected_action
    assert payload["client_ref"] == "operation-5026"
    assert payload["channel"] == "-1002136478186"
    assert payload["entry_ref"] == "tg-sig-c1002136478186-m5026"


@pytest.mark.parametrize(("command", "expected_slug"), MANAGEMENT_CASES)
def test_management_commands_require_operation_ref_with_suggestion(
    load_trade_module, monkeypatch, capsys, command, expected_slug
):
    module = load_trade_module()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            *command,
            "--reason",
            "test",
            "--entry-ref",
            "tg-sig-c1002136478186-m5026",
        ],
    )
    monkeypatch.setattr(
        module,
        "_call",
        lambda *args, **kwargs: pytest.fail("request sent without operation ref"),
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    output = json.loads(capsys.readouterr().out)
    assert exc.value.code == 1
    assert "--ref is required" in output["error"]
    assert output["suggested_ref"].endswith("tg-sig-c1002136478186-m5026")
