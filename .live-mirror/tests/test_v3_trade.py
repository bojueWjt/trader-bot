from __future__ import annotations

import json
import sqlite3
import sys

import pytest


FOUR_CHANNEL_ROUTES = (
    ("-1002136478186", "credential-main-a", "account-a", "main", ""),
    ("-1002198013097", "credential-main-b", "account-b", "main", ""),
    (
        "-1002189417451",
        "credential-sub-c",
        "account-c",
        "subaccount",
        "credential-main-a",
    ),
    (
        "-1002193304023",
        "credential-sub-d",
        "account-d",
        "subaccount",
        "credential-main-b",
    ),
)


MANAGEMENT_CASES = [
    (
        ["close", "BTCUSDT", "--side", "long"],
        "close_position",
    ),
    (
        [
            "partial",
            "BTCUSDT",
            "--side",
            "long",
            "--quantity",
            "0.01",
        ],
        "partial_close",
    ),
    (
        ["set-sl", "BTCUSDT", "--side", "long", "--sl", "60000"],
        "move_stop_loss",
    ),
    (
        [
            "set-tps",
            "BTCUSDT",
            "--side",
            "long",
            "--tp",
            "70000",
            "--qty",
            "0.01",
        ],
        "replace_take_profits",
    ),
    (
        ["disable-tps", "BTCUSDT", "--side", "long"],
        "replace_take_profits",
    ),
    (
        ["cancel", "BTCUSDT", "--order", "B" + "a" * 32 + "01"],
        "cancel_order",
    ),
]


@pytest.fixture(autouse=True)
def channel_route_db(monkeypatch, tmp_path):
    db_path = tmp_path / "watcher-trading.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE account_configs (
            account_id TEXT PRIMARY KEY,
            account_type TEXT NOT NULL,
            parent_account_id TEXT NOT NULL,
            execution_account_id TEXT NOT NULL,
            is_enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE channel_routing (
            channel_id TEXT PRIMARY KEY,
            target_account_id TEXT NOT NULL
        );
        """
    )
    for (
        channel_id,
        credential_account,
        execution_account,
        account_type,
        parent_account,
    ) in FOUR_CHANNEL_ROUTES:
        conn.execute(
            "INSERT INTO account_configs ("
            "account_id, account_type, parent_account_id, "
            "execution_account_id, is_enabled"
            ") VALUES (?, ?, ?, ?, 1)",
            (
                credential_account,
                account_type,
                parent_account,
                execution_account,
            ),
        )
        conn.execute(
            "INSERT INTO channel_routing (channel_id, target_account_id) "
            "VALUES (?, ?)",
            (channel_id, credential_account),
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("WATCHER_TRADING_DB", str(db_path))


CHANNEL_AUTH_ARGS = [
    "--account",
    "account-a",
    "--authorized-by-type",
    "channel",
    "--authorized-by-id",
    "-1002136478186",
    "--source-message-id",
    "tg-sig-c1002136478186-m5026",
]


def _successful_call(calls):
    def fake_call(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "POST":
            return {
                "intent_id": "intent-1",
                "status": "approved",
                "replay": False,
            }
        return {
            "intent": {"status": "approved"},
            "orders": [],
            "execution_events": [],
            "open_positions": [],
        }

    return fake_call


@pytest.mark.parametrize(
    ("channel_id", "_credential_account", "execution_account", "_type", "_parent"),
    FOUR_CHANNEL_ROUTES,
)
def test_four_channel_opens_resolve_to_fixed_execution_accounts(
    load_trade_module,
    monkeypatch,
    channel_id,
    _credential_account,
    execution_account,
    _type,
    _parent,
):
    module = load_trade_module()
    calls = []
    message_ref = f"tg-sig-c{channel_id.lstrip('-')}-m7001"
    monkeypatch.setattr(module, "_call", _successful_call(calls))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            "open",
            "BTCUSDT",
            "long",
            "--notional",
            "10",
            "--reason",
            "four-channel route test",
            "--ref",
            message_ref,
            "--channel",
            channel_id,
            "--account",
            execution_account,
            "--authorized-by-type",
            "channel",
            "--authorized-by-id",
            channel_id,
            "--source-message-id",
            message_ref,
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["account_id"] == execution_account
    assert payload["source_channel"] == channel_id
    assert payload["authorized_by_id"] == channel_id


@pytest.mark.parametrize(
    ("channel_id", "_credential_account", "execution_account", "_type", "_parent"),
    FOUR_CHANNEL_ROUTES,
)
@pytest.mark.parametrize(("command", "expected_action"), MANAGEMENT_CASES)
def test_four_channel_management_actions_keep_entry_execution_account(
    load_trade_module,
    monkeypatch,
    channel_id,
    _credential_account,
    execution_account,
    _type,
    _parent,
    command,
    expected_action,
):
    module = load_trade_module()
    calls = []
    entry_ref = f"tg-sig-c{channel_id.lstrip('-')}-m7001"
    source_ref = f"tg-sig-c{channel_id.lstrip('-')}-m7002"
    monkeypatch.setattr(module, "_call", _successful_call(calls))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            *command,
            "--reason",
            "four-channel management route test",
            "--ref",
            f"manage-{expected_action}-{source_ref}",
            "--channel",
            channel_id,
            "--entry-ref",
            entry_ref,
            "--account",
            execution_account,
            "--authorized-by-type",
            "channel",
            "--authorized-by-id",
            channel_id,
            "--source-message-id",
            source_ref,
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["action"] == expected_action
    assert payload["account_id"] == execution_account
    assert payload["channel"] == channel_id
    assert payload["entry_ref"] == entry_ref
    assert payload["authorized_by_id"] == channel_id
    if expected_action != "cancel_order":
        assert payload["position_side"] == "long"


def test_channel_open_rejects_hermes_account_override_before_http(
    load_trade_module,
    monkeypatch,
    capsys,
):
    module = load_trade_module()
    channel_id = "-1002136478186"
    source_ref = "tg-sig-c1002136478186-m7002"
    argv = [
        "v3_trade.py",
        "open",
        "BTCUSDT",
        "long",
        "--notional",
        "10",
        "--reason",
        "reject account override",
        "--ref",
        source_ref,
        "--channel",
        channel_id,
        "--account",
        "account-b",
        "--authorized-by-type",
        "channel",
        "--authorized-by-id",
        channel_id,
        "--source-message-id",
        source_ref,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(
        module,
        "_call",
        lambda *args, **kwargs: pytest.fail(
            "request sent after channel account override"
        ),
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    output = json.loads(capsys.readouterr().out)
    assert exc.value.code == 1
    assert "requires account-a" in output["error"]


@pytest.mark.parametrize(("command", "expected_action"), MANAGEMENT_CASES)
def test_channel_management_allows_server_side_account_normalization(
    load_trade_module,
    monkeypatch,
    command,
    expected_action,
):
    module = load_trade_module()
    calls = []
    channel_id = "-1002136478186"
    entry_ref = "tg-sig-c1002136478186-m7001"
    source_ref = "tg-sig-c1002136478186-m7002"
    monkeypatch.setattr(module, "_call", _successful_call(calls))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            *command,
            "--reason",
            "manage entry after channel remap",
            "--ref",
            f"remap-{expected_action}-{source_ref}",
            "--channel",
            channel_id,
            "--entry-ref",
            entry_ref,
            "--account",
            "account-c",
            "--authorized-by-type",
            "channel",
            "--authorized-by-id",
            channel_id,
            "--source-message-id",
            source_ref,
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["action"] == expected_action
    assert payload["account_id"] == "account-c"
    assert payload["entry_ref"] == entry_ref
    assert payload["channel"] == channel_id


def test_channel_management_rejects_cross_channel_entry_ref_before_http(
    load_trade_module,
    monkeypatch,
    capsys,
):
    module = load_trade_module()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            "close",
            "BTCUSDT",
            "--side",
            "long",
            "--reason",
            "reject cross-channel entry",
            "--ref",
            "close-btc-tg-sig-c1002136478186-m7002",
            "--channel",
            "-1002136478186",
            "--entry-ref",
            "tg-sig-c1002198013097-m7001",
            "--account",
            "account-a",
            "--authorized-by-type",
            "channel",
            "--authorized-by-id",
            "-1002136478186",
            "--source-message-id",
            "tg-sig-c1002136478186-m7002",
        ],
    )
    monkeypatch.setattr(
        module,
        "_call",
        lambda *args, **kwargs: pytest.fail(
            "request sent with cross-channel entry ref"
        ),
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    output = json.loads(capsys.readouterr().out)
    assert exc.value.code == 1
    assert "--entry-ref must encode the same channel" in output["error"]


USER_AUTH_ARGS = [
    "--account",
    "account-a",
    "--authorized-by-type",
    "user",
    "--authorized-by-id",
    "balen",
    "--source-message-id",
    "operator-request-5026",
]


def test_open_requires_explicit_account_and_does_not_send_request(
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
            "operator-btc-short-5026",
            "--channel",
            "operator",
            "--authorized-by-type",
            "user",
            "--authorized-by-id",
            "balen",
            "--source-message-id",
            "operator-request-5026",
        ],
    )
    monkeypatch.setattr(
        module,
        "_call",
        lambda *args, **kwargs: pytest.fail("request sent without account"),
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 2
    assert "--account" in capsys.readouterr().err


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
            *CHANNEL_AUTH_ARGS,
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


@pytest.mark.parametrize("account_id", ["account-c", "account-d"])
def test_cli_accepts_configured_additional_accounts(
    load_trade_module,
    monkeypatch,
    account_id,
):
    module = load_trade_module()
    calls = []
    monkeypatch.setenv(
        "OPERATOR_ACCOUNT_REGISTRY_JSON",
        json.dumps(
            {
                candidate: {}
                for candidate in (
                    "account-a",
                    "account-b",
                    "account-c",
                    "account-d",
                )
            }
        ),
    )

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
            "four-account registry test",
            "--ref",
            f"operator-{account_id}-btc-short",
            "--channel",
            "operator",
            "--account",
            account_id,
            "--authorized-by-type",
            "user",
            "--authorized-by-id",
            "balen",
            "--source-message-id",
            f"operator-request-{account_id}",
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["account_id"] == account_id


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
            *CHANNEL_AUTH_ARGS,
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["client_ref"] == "tg-sig-c1002136478186-m5026"
    assert payload["source_channel"] == "-1002136478186"
    assert payload["authorized_by_type"] == "channel"
    assert payload["authorized_by_id"] == "-1002136478186"
    assert payload["source_message_id"] == "tg-sig-c1002136478186-m5026"
    assert payload["created_by_service"] == "hermes-agent"


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
            *CHANNEL_AUTH_ARGS,
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


def test_open_forwards_user_provenance(load_trade_module, monkeypatch):
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
            *USER_AUTH_ARGS,
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["client_ref"] == "operator-btc-short-0719"
    assert payload["source_channel"] == "operator"
    assert payload["authorized_by_type"] == "user"
    assert payload["authorized_by_id"] == "balen"
    assert payload["source_message_id"] == "operator-request-5026"
    assert payload["created_by_service"] == "hermes-agent"


def test_open_forwards_explicit_live_canary_contract(
    load_trade_module,
    monkeypatch,
):
    module = load_trade_module()
    calls = []

    monkeypatch.setattr(module, "_call", _successful_call(calls))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            "open",
            "BTCUSDT",
            "long",
            "--entry-type",
            "limit",
            "--price",
            "60000",
            "--time-in-force",
            "IOC",
            "--quantity",
            "0.0002",
            "--notional",
            "12",
            "--canary-permit-id",
            "permit-account-a-7001",
            "--reason",
            "reviewed account-a canary",
            "--ref",
            "operator-account-a-canary-7001",
            "--channel",
            "operator",
            *USER_AUTH_ARGS,
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["entry"] == {
        "type": "limit",
        "price": 60000.0,
        "time_in_force": "IOC",
    }
    assert payload["quantity"] == 0.0002
    assert payload["notional_usdt"] == 12.0
    assert payload["canary_permit_id"] == "permit-account-a-7001"


def test_normal_open_omits_live_canary_contract(
    load_trade_module,
    monkeypatch,
):
    module = load_trade_module()
    calls = []

    monkeypatch.setattr(module, "_call", _successful_call(calls))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            "open",
            "BTCUSDT",
            "short",
            "--sl",
            "65000",
            "--reason",
            "normal risk-sized order",
            "--ref",
            "operator-normal-risk-sized-7002",
            "--channel",
            "operator",
            *USER_AUTH_ARGS,
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["entry"] == {"type": "market"}
    assert "quantity" not in payload
    assert "canary_permit_id" not in payload


def test_management_forwards_internal_parent_intent(load_trade_module, monkeypatch):
    module = load_trade_module()
    calls = []
    parent_intent_id = "11111111-1111-1111-1111-111111111111"

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
            "close",
            "BTCUSDT",
            "--side",
            "long",
            "--reason",
            "authorized reconciliation",
            "--ref",
            "reconcile-close-5026",
            "--authorized-by-type",
            "user",
            "--authorized-by-id",
            "balen",
            "--source-message-id",
            "operator-request-5026",
            "--account",
            "account-a",
            "--created-by-service",
            "position-reconciler",
            "--parent-intent-id",
            parent_intent_id,
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["source"] == "position-reconciler"
    assert payload["created_by_service"] == "position-reconciler"
    assert payload["parent_intent_id"] == parent_intent_id


def test_disable_tps_sends_explicit_tombstone_payload(
    load_trade_module, monkeypatch
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
            "disable-tps",
            "BTCUSDT",
            "--side",
            "long",
            "--reason",
            "manual BTC profit close; keep TP automation disabled",
            "--ref",
            "disable-tps-btc-user-request-7001",
            *USER_AUTH_ARGS,
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["action"] == "replace_take_profits"
    assert payload["symbol"] == "BTCUSDT"
    assert payload["position_side"] == "long"
    assert payload["take_profits"] == []
    assert payload["disable_take_profits"] is True
    assert payload["authorized_by_type"] == "user"
    assert payload["account_id"] == "account-a"


def test_set_tps_rejects_empty_price_list(
    load_trade_module, monkeypatch, capsys
):
    module = load_trade_module()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            "set-tps",
            "BTCUSDT",
            "--side",
            "long",
            "--tp",
            "",
            "--qty",
            "",
            "--reason",
            "test",
            "--ref",
            "set-tps-empty-7001",
            *USER_AUTH_ARGS,
        ],
    )
    monkeypatch.setattr(
        module,
        "_call",
        lambda *args, **kwargs: pytest.fail("empty set-tps request was sent"),
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    output = json.loads(capsys.readouterr().out)
    assert exc.value.code == 1
    assert "--tp requires at least one price" in output["error"]


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
            *CHANNEL_AUTH_ARGS,
            "--no-wait",
        ],
    )

    module.main()

    payload = next(payload for method, _, payload in calls if method == "POST")
    assert payload["action"] == expected_action
    assert payload["client_ref"] == "operation-5026"
    assert payload["channel"] == "-1002136478186"
    assert payload["entry_ref"] == "tg-sig-c1002136478186-m5026"
    assert payload["authorized_by_type"] == "channel"
    assert payload["authorized_by_id"] == "-1002136478186"
    assert payload["source_message_id"] == "tg-sig-c1002136478186-m5026"
    assert payload["created_by_service"] == "hermes-agent"


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
            *CHANNEL_AUTH_ARGS,
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


def _fake_open_call(calls):
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
    return fake_call


def _open_argv(*extra):
    return [
        "v3_trade.py",
        "open",
        "ETHUSDT",
        *extra,
        "--reason",
        "test",
        "--ref",
        "tg-sig-c1002136478186-m5026",
        "--channel",
        "-1002136478186",
        *CHANNEL_AUTH_ARGS,
        "--no-wait",
    ]


def test_open_entry_offset_shifts_fuzzy_zone_long(load_trade_module, monkeypatch):
    module = load_trade_module()
    calls = []
    monkeypatch.setattr(module, "_call", _fake_open_call(calls))
    monkeypatch.setattr(sys, "argv", _open_argv(
        "long", "--entry-type", "zone",
        "--price-min", "1806", "--price-max", "1826",
        "--sl", "1787.82", "--entry-offset",
    ))

    module.main()

    payload = next(p for m, _, p in calls if m == "POST")
    entry = payload["entry"]
    assert entry["price_min"] == 1807.806
    assert entry["price_max"] == 1827.826
    assert entry["price_min_raw"] == 1806.0
    assert entry["price_max_raw"] == 1826.0
    assert entry["offset_pct"] == 0.001
    assert "入场模糊点位让利0.1%" in payload["reason"]
    assert "price_min=1806" in payload["reason"]


def test_open_entry_offset_shifts_fuzzy_limit_short(load_trade_module, monkeypatch):
    module = load_trade_module()
    calls = []
    monkeypatch.setattr(module, "_call", _fake_open_call(calls))
    monkeypatch.setattr(sys, "argv", _open_argv(
        "short", "--entry-type", "limit",
        "--price", "100", "--sl", "104", "--entry-offset",
    ))

    module.main()

    payload = next(p for m, _, p in calls if m == "POST")
    entry = payload["entry"]
    assert entry["price"] == 99.9
    assert entry["price_raw"] == 100.0
    assert entry["offset_pct"] == 0.001
    assert "入场模糊点位让利0.1%" in payload["reason"]


def test_open_without_entry_offset_keeps_exact_prices(load_trade_module, monkeypatch):
    module = load_trade_module()
    calls = []
    monkeypatch.setattr(module, "_call", _fake_open_call(calls))
    monkeypatch.setattr(sys, "argv", _open_argv(
        "long", "--entry-type", "zone",
        "--price-min", "1806", "--price-max", "1826", "--sl", "1787.82",
    ))

    module.main()

    payload = next(p for m, _, p in calls if m == "POST")
    entry = payload["entry"]
    assert entry["price_min"] == 1806.0
    assert entry["price_max"] == 1826.0
    assert "offset_pct" not in entry
    assert "price_min_raw" not in entry
    assert payload["reason"] == "test"


def test_open_entry_offset_requires_price(load_trade_module, monkeypatch, capsys):
    module = load_trade_module()
    calls = []
    monkeypatch.setattr(module, "_call", _fake_open_call(calls))
    monkeypatch.setattr(sys, "argv", _open_argv(
        "long", "--entry-type", "market", "--entry-offset",
    ))

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 1
    output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "--entry-offset requires" in output["error"]
    assert not [c for c in calls if c[0] == "POST"]
