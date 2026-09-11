from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FEEDER_PATH = REPO_ROOT / "scripts" / "hermes_signal_feeder.py"
TRADE_PATH = (
    REPO_ROOT
    / "hermes-profile"
    / "skills"
    / "trading"
    / "v3-trader"
    / "scripts"
    / "v3_trade.py"
)
FOUR_CHANNEL_ROUTES = (
    (
        "-1002136478186",
        "jiataotx@gmail.com",
        "account-a",
        "main",
        "",
        0.0,
    ),
    (
        "-1002198013097",
        "balenwong3@gmail.com",
        "account-b",
        "main",
        "",
        0.0,
    ),
    (
        "-1002189417451",
        "泰山",
        "account-c",
        "subaccount",
        "jiataotx@gmail.com",
        6000.0,
    ),
    (
        "-1002193304023",
        "黄山",
        "account-d",
        "subaccount",
        "balenwong3@gmail.com",
        6000.0,
    ),
)

MANAGEMENT_CASES = (
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
)


def test_two_entries_are_one_cli_request(monkeypatch):
    trade = _load_module('test_two_entry_trade', TRADE_PATH)
    calls = []
    monkeypatch.setattr(trade, '_call', _successful_call(calls))
    monkeypatch.setattr(sys, 'argv', [
        'v3_trade.py', 'open', 'ICPUSDT', 'long', '--sl', '2.58',
        '--second-price', '2.662', '--channel', 'operator',
        '--account', 'account-b', '--ref', 'operator-two-entry-test',
        '--reason', 'two entries', '--no-wait',
        '--authorized-by-type', 'user', '--authorized-by-id', 'test-user',
        '--source-message-id', 'operator-two-entry-test',
    ])
    trade.main()
    posts = [payload for method, path, payload in calls if method == 'POST']
    assert len(posts) == 1
    assert posts[0]['entry'] == {'type': 'market', 'second_price': 2.662}
    assert posts[0]['client_ref'] == 'operator-two-entry-test'


@pytest.fixture(autouse=True)
def _clear_trading_db_path_env(monkeypatch):
    for name in (
        "TRADER_TRADING_DB_PATH",
        "WATCHER_TRADING_DB",
        "TRADING_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_v3_trade_trading_db_path_resolver_accepts_canonical_and_legacy_aliases() -> None:
    trade = _load_module("test_v3_trade_db_path", TRADE_PATH)

    assert trade.resolve_trading_db_path({}) == trade.DEFAULT_TRADING_DB_PATH
    assert trade.resolve_trading_db_path(
        {"WATCHER_TRADING_DB": "/data/watcher-trading.db"}
    ) == "/data/watcher-trading.db"
    assert trade.resolve_trading_db_path(
        {
            "TRADER_TRADING_DB_PATH": "/data/watcher-trading.db",
            "WATCHER_TRADING_DB": "/data/watcher-trading.db",
            "TRADING_DB_PATH": "/data/watcher-trading.db",
        }
    ) == "/data/watcher-trading.db"


def test_v3_trade_trading_db_path_conflict_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("TRADER_TRADING_DB_PATH", "/data/a.db")
    monkeypatch.setenv("WATCHER_TRADING_DB", "/data/b.db")

    with pytest.raises(RuntimeError, match="conflicting trading DB path"):
        _load_module("test_v3_trade_db_path_conflict", TRADE_PATH)


def _create_routing_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE account_configs (
                account_id TEXT PRIMARY KEY,
                account_type TEXT NOT NULL,
                parent_account_id TEXT NOT NULL,
                risk_capital_addon REAL NOT NULL,
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
            addon,
        ) in FOUR_CHANNEL_ROUTES:
            conn.execute(
                """
                INSERT INTO account_configs (
                    account_id,
                    account_type,
                    parent_account_id,
                    risk_capital_addon,
                    execution_account_id
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    credential_account,
                    account_type,
                    parent_account,
                    addon,
                    execution_account,
                ),
            )
            conn.execute(
                """
                INSERT INTO channel_routing (
                    channel_id,
                    target_account_id
                )
                VALUES (?, ?)
                """,
                (channel_id, credential_account),
            )
        conn.commit()
    finally:
        conn.close()


def test_v3_trade_rejects_orphan_subaccount_route(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    routing_db = tmp_path / "orphan-subaccount.db"
    _create_routing_db(routing_db)
    conn = sqlite3.connect(routing_db)
    try:
        conn.execute(
            "UPDATE account_configs "
            "SET parent_account_id='missing-main' "
            "WHERE execution_account_id='account-c'"
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("TRADER_TRADING_DB_PATH", str(routing_db))
    trade = _load_module("test_v3_trade_orphan_subaccount", TRADE_PATH)

    with pytest.raises(SystemExit) as exc:
        trade._channel_execution_account("-1002189417451")

    assert exc.value.code == 1
    assert "parent must resolve to one main account" in capsys.readouterr().out


def _signal(channel_id: str, message_id: int) -> dict[str, str]:
    return {
        "signal_id": f"{channel_id}:{message_id}",
        "received_at": "2026-08-12T00:00:00+00:00",
        "payload": json.dumps(
            {
                "source_channel_id": channel_id,
                "source_channel_name": f"channel-{message_id}",
                "source_message_id": str(message_id),
                "raw_text": "BTC long, stop 60000",
            }
        ),
    }


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
    (
        "channel_id",
        "credential_account",
        "execution_account",
        "_account_type",
        "_parent_account",
        "addon",
    ),
    FOUR_CHANNEL_ROUTES,
)
def test_watcher_route_reaches_matching_hermes_open_payload(
    monkeypatch,
    tmp_path: Path,
    channel_id: str,
    credential_account: str,
    execution_account: str,
    _account_type: str,
    _parent_account: str,
    addon: float,
) -> None:
    routing_db = tmp_path / "watcher-trading.db"
    _create_routing_db(routing_db)
    monkeypatch.setenv("TRADER_TRADING_DB_PATH", str(routing_db))

    feeder = _load_module(
        f"test_four_account_feeder_{execution_account}",
        FEEDER_PATH,
    )
    monkeypatch.setattr(feeder, "V3_MEDIA", str(tmp_path / "media"))
    message_id = 7000 + ord(execution_account[-1])
    signal = _signal(channel_id, message_id)
    prompt = feeder.build_prompt(signal)
    client_ref = (
        f"tg-sig-c{channel_id.lstrip('-')}-m{message_id}"
    )

    assert f"路由凭据账号(审计): {credential_account}" in prompt
    assert f"固定执行账号: {execution_account}" in prompt
    assert f"风险资金加权额(审计): +{addon:g} USDT" in prompt
    assert f"必须使用 --account {execution_account}" in prompt
    assert f"交易ref: {client_ref}" in prompt

    trade = _load_module(
        f"test_four_account_trade_{execution_account}",
        TRADE_PATH,
    )
    calls = []
    monkeypatch.setattr(trade, "_call", _successful_call(calls))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            "open",
            "BTCUSDT",
            "long",
            "--sl",
            "60000",
            "--reason",
            "four-account end-to-end route test",
            "--ref",
            client_ref,
            "--channel",
            channel_id,
            "--account",
            execution_account,
            "--authorized-by-type",
            "channel",
            "--authorized-by-id",
            channel_id,
            "--source-message-id",
            client_ref,
            "--no-wait",
        ],
    )

    trade.main()

    payload = next(
        payload
        for method, path, payload in calls
        if method == "POST" and path == "/v1/operator/orders"
    )
    assert payload["account_id"] == execution_account
    assert payload["source_channel"] == channel_id
    assert payload["authorized_by_type"] == "channel"
    assert payload["authorized_by_id"] == channel_id
    assert payload["source_message_id"] == client_ref
    assert payload["client_ref"] == client_ref
    assert "notional_usdt" not in payload
    assert "quantity" not in payload
    assert "canary_permit_id" not in payload


@pytest.mark.parametrize(
    (
        "channel_id",
        "_credential_account",
        "execution_account",
        "_account_type",
        "_parent_account",
        "_addon",
    ),
    FOUR_CHANNEL_ROUTES,
)
@pytest.mark.parametrize(("command", "expected_action"), MANAGEMENT_CASES)
def test_four_channel_management_payload_keeps_execution_account(
    monkeypatch,
    tmp_path: Path,
    channel_id: str,
    _credential_account: str,
    execution_account: str,
    _account_type: str,
    _parent_account: str,
    _addon: float,
    command: list[str],
    expected_action: str,
) -> None:
    routing_db = tmp_path / "watcher-trading.db"
    _create_routing_db(routing_db)
    monkeypatch.setenv("WATCHER_TRADING_DB", str(routing_db))
    trade = _load_module(
        f"test_four_account_management_{execution_account}_{expected_action}",
        TRADE_PATH,
    )
    calls = []
    entry_ref = f"tg-sig-c{channel_id.lstrip('-')}-m7001"
    source_ref = f"tg-sig-c{channel_id.lstrip('-')}-m7002"
    monkeypatch.setattr(trade, "_call", _successful_call(calls))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            *command,
            "--reason",
            "four-account management payload test",
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

    trade.main()

    payload = next(
        payload
        for method, path, payload in calls
        if method == "POST" and path == "/v1/operator/orders"
    )
    assert payload["action"] == expected_action
    assert payload["account_id"] == execution_account
    assert payload["channel"] == channel_id
    assert payload["entry_ref"] == entry_ref
    assert payload["authorized_by_type"] == "channel"
    assert payload["authorized_by_id"] == channel_id
    assert payload["source_message_id"] == source_ref
    assert payload["client_ref"] == f"manage-{expected_action}-{source_ref}"
    if expected_action != "cancel_order":
        assert payload["position_side"] == "long"


@pytest.mark.parametrize(
    "account_id",
    ["account-a", "account-b", "account-c", "account-d"],
)
def test_reviewed_canary_cli_contract_is_available_for_every_account(
    monkeypatch,
    tmp_path: Path,
    account_id: str,
) -> None:
    routing_db = tmp_path / "watcher-trading.db"
    _create_routing_db(routing_db)
    monkeypatch.setenv("WATCHER_TRADING_DB", str(routing_db))
    trade = _load_module(
        f"test_four_account_canary_{account_id}",
        TRADE_PATH,
    )
    calls = []
    monkeypatch.setattr(trade, "_call", _successful_call(calls))
    permit_id = f"permit-{account_id}-7001"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            "open",
            "SOLUSDT",
            "long",
            "--entry-type",
            "limit",
            "--price",
            "145",
            "--time_in_force",
            "IOC",
            "--quantity",
            "0.08",
            "--notional",
            "11.6",
            "--canary_permit_id",
            permit_id,
            "--reason",
            f"reviewed {account_id} canary",
            "--ref",
            f"operator-{account_id}-canary-7001",
            "--channel",
            "operator",
            "--account",
            account_id,
            "--authorized-by-type",
            "user",
            "--authorized-by-id",
            "balen",
            "--source-message-id",
            f"operator-request-{account_id}-canary-7001",
            "--no-wait",
        ],
    )

    trade.main()

    payload = next(
        payload
        for method, path, payload in calls
        if method == "POST" and path == "/v1/operator/orders"
    )
    assert payload["account_id"] == account_id
    assert payload["entry"] == {
        "type": "limit",
        "price": 145.0,
        "time_in_force": "IOC",
    }
    assert payload["quantity"] == 0.08
    assert payload["notional_usdt"] == 11.6
    assert payload["canary_permit_id"] == permit_id


def test_normal_operator_open_keeps_explicit_notional_compatibility(
    monkeypatch,
    tmp_path: Path,
) -> None:
    routing_db = tmp_path / "watcher-trading.db"
    _create_routing_db(routing_db)
    monkeypatch.setenv("WATCHER_TRADING_DB", str(routing_db))
    trade = _load_module("test_normal_operator_notional", TRADE_PATH)
    calls = []
    monkeypatch.setattr(trade, "_call", _successful_call(calls))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py",
            "open",
            "BTCUSDT",
            "short",
            "--notional",
            "300",
            "--reason",
            "normal explicit notional compatibility",
            "--ref",
            "operator-normal-notional-7001",
            "--channel",
            "operator",
            "--account",
            "account-a",
            "--authorized-by-type",
            "user",
            "--authorized-by-id",
            "balen",
            "--source-message-id",
            "operator-request-normal-notional-7001",
            "--no-wait",
        ],
    )

    trade.main()

    payload = next(
        payload
        for method, path, payload in calls
        if method == "POST" and path == "/v1/operator/orders"
    )
    assert payload["account_id"] == "account-a"
    assert payload["entry"] == {"type": "market"}
    assert payload["notional_usdt"] == 300.0
    assert "quantity" not in payload
    assert "canary_permit_id" not in payload
