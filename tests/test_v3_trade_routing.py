from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
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


def test_v3_trade_does_not_read_host_database_or_env_files(monkeypatch) -> None:
    monkeypatch.setenv("TRADER_TRADING_DB_PATH", "/unavailable/a.db")
    monkeypatch.setenv("WATCHER_TRADING_DB", "/unavailable/b.db")
    monkeypatch.setenv("RISK_ADMIN_TOKEN", "injected-token")
    trade = _load_module("portable_trade", TRADE_PATH)
    def forbidden(*args, **kwargs):
        raise AssertionError("portable CLI tried to open a host file")
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    assert trade._token() == "injected-token"
    calls = []
    monkeypatch.setattr(trade, "_call", _successful_call(calls))
    assert trade._channel_execution_account("-1002189417451") == "account-c"
    assert len(calls) == 1
    assert calls[0][1].startswith("/v1/query/channel-route?")


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


def test_v3_trade_rejects_unregistered_control_plane_route(monkeypatch, capsys) -> None:
    trade = _load_module("portable_trade_invalid_route", TRADE_PATH)
    monkeypatch.setattr(trade, "_call", lambda *args: {"execution_account_id": "credential-email"})
    with pytest.raises(SystemExit):
        trade._channel_execution_account("-1002189417451")
    assert "no registered execution account" in capsys.readouterr().out


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


def _snapshot_response(
    *,
    positions_by_account: dict[str, list] | None = None,
    stale: bool = False,
) -> dict:
    positions_by_account = positions_by_account or {}
    return {
        "stale": stale,
        "data": {
            "positions": [],
            "exchange_state": [
                {
                    "account_id": account_id,
                    "stale": stale,
                    "updated_at": "2026-09-14T00:00:00+00:00",
                    "payload": {
                        "positions": list(positions_by_account.get(account_id) or []),
                    },
                }
                for account_id in (
                    "account-a",
                    "account-b",
                    "account-c",
                    "account-d",
                )
            ],
        },
    }


def _successful_call(calls, *, positions_by_account=None, snapshot_stale=False):
    def fake_call(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET" and path.startswith("/v1/query/channel-route?"):
            from urllib.parse import parse_qs, urlsplit
            channel = parse_qs(urlsplit(path).query)["channel"][0]
            route = next(row for row in FOUR_CHANNEL_ROUTES if row[0] == channel)
            return {"execution_account_id": route[2], "risk_capital_addon": route[5]}
        if method == "GET" and path == "/api/system/snapshot":
            return _snapshot_response(
                positions_by_account=positions_by_account,
                stale=snapshot_stale,
            )
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


_MANAGE_AUTH = [
    "--account", "account-b",
    "--side", "long",
    "--reason", "titan lock 20 percent",
    "--ref", "partial-jto-tg-sig-c1002198013097-m4502",
    "--channel", "-1002198013097",
    "--entry-ref", "tg-sig-c1002198013097-m4493-e1",
    "--authorized-by-type", "channel",
    "--authorized-by-id", "-1002198013097",
    "--source-message-id", "tg-sig-c1002198013097-m4502",
    "--no-wait",
]


def test_resolve_partial_close_quantity_20_percent_of_snapshot() -> None:
    trade = _load_module("test_partial_qty_helper", TRADE_PATH)
    assert trade.resolve_partial_close_quantity(
        quantity=None, percent=20, position_quantity=1407,
    ) == 281.4
    assert trade.resolve_partial_close_quantity(
        quantity=None, percent=20, position_quantity=11871.2,
    ) == pytest.approx(2374.24)
    assert trade.resolve_partial_close_quantity(
        quantity=281.0, percent=None, position_quantity=1407,
    ) == 281.0


def test_resolve_partial_close_rejects_missing_and_illegal_ratios() -> None:
    trade = _load_module("test_partial_qty_illegal", TRADE_PATH)
    with pytest.raises(ValueError, match="exactly one"):
        trade.resolve_partial_close_quantity(
            quantity=None, percent=None, position_quantity=1407,
        )
    with pytest.raises(ValueError, match="exactly one"):
        trade.resolve_partial_close_quantity(
            quantity=281, percent=20, position_quantity=1407,
        )
    with pytest.raises(ValueError, match="percent"):
        trade.resolve_partial_close_quantity(
            quantity=None, percent=0, position_quantity=1407,
        )
    with pytest.raises(ValueError, match="percent"):
        trade.resolve_partial_close_quantity(
            quantity=None, percent=120, position_quantity=1407,
        )
    with pytest.raises(ValueError, match="no open position"):
        trade.resolve_partial_close_quantity(
            quantity=None, percent=20, position_quantity=None,
        )


def test_resolve_partial_close_rejects_non_finite_percent_quantity_and_position() -> None:
    trade = _load_module("test_partial_qty_nonfinite", TRADE_PATH)
    nan = float("nan")
    inf = float("inf")
    ninf = float("-inf")
    with pytest.raises(ValueError, match="illegal percent"):
        trade.resolve_partial_close_quantity(
            quantity=None, percent=nan, position_quantity=1407,
        )
    with pytest.raises(ValueError, match="illegal percent"):
        trade.resolve_partial_close_quantity(
            quantity=None, percent=inf, position_quantity=1407,
        )
    with pytest.raises(ValueError, match="illegal percent"):
        trade.resolve_partial_close_quantity(
            quantity=None, percent=ninf, position_quantity=1407,
        )
    with pytest.raises(ValueError, match="illegal quantity"):
        trade.resolve_partial_close_quantity(
            quantity=nan, percent=None, position_quantity=1407,
        )
    with pytest.raises(ValueError, match="illegal quantity"):
        trade.resolve_partial_close_quantity(
            quantity=inf, percent=None, position_quantity=1407,
        )
    with pytest.raises(ValueError, match="illegal quantity"):
        trade.resolve_partial_close_quantity(
            quantity=ninf, percent=None, position_quantity=1407,
        )
    with pytest.raises(ValueError, match="illegal position_quantity"):
        trade.resolve_partial_close_quantity(
            quantity=None, percent=20, position_quantity=nan,
        )
    with pytest.raises(ValueError, match="illegal position_quantity"):
        trade.resolve_partial_close_quantity(
            quantity=None, percent=20, position_quantity=inf,
        )


def test_partial_percent_20_posts_partial_close_not_full(
    monkeypatch,
) -> None:
    trade = _load_module("test_partial_percent_20", TRADE_PATH)
    calls = []
    monkeypatch.setattr(trade, "_call", _successful_call(calls))
    monkeypatch.setattr(
        trade,
        "_position_for",
        lambda symbol, account_id, side=None: {
            "quantity": 1407,
            "account_id": account_id,
            "instrument_id": symbol,
            "side": side,
            "status": "open",
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["v3_trade.py", "partial", "JTOUSDT", "--percent", "20", *_MANAGE_AUTH],
    )
    trade.main()
    payload = next(
        payload
        for method, path, payload in calls
        if method == "POST" and path == "/v1/operator/orders"
    )
    assert payload["action"] == "partial_close"
    assert payload["quantity"] == pytest.approx(281.4)
    first = dict(payload)
    calls.clear()
    trade.main()
    retry = next(
        payload
        for method, path, payload in calls
        if method == "POST" and path == "/v1/operator/orders"
    )
    assert retry["quantity"] == first["quantity"]
    assert retry["client_ref"] == first["client_ref"]


def test_close_with_percent_is_rejected_not_silent_full_close(monkeypatch) -> None:
    trade = _load_module("test_close_percent_rejected", TRADE_PATH)
    calls = []
    monkeypatch.setattr(trade, "_call", _successful_call(calls))
    monkeypatch.setattr(
        sys,
        "argv",
        ["v3_trade.py", "close", "JTOUSDT", "--percent", "20", *_MANAGE_AUTH],
    )
    with pytest.raises(SystemExit) as exited:
        trade.main()
    assert exited.value.code == 1
    assert calls == []


def test_partial_without_quantity_or_percent_is_rejected(monkeypatch) -> None:
    trade = _load_module("test_partial_missing_ratio", TRADE_PATH)
    calls = []
    monkeypatch.setattr(trade, "_call", _successful_call(calls))
    monkeypatch.setattr(
        sys,
        "argv",
        ["v3_trade.py", "partial", "JTOUSDT", *_MANAGE_AUTH],
    )
    with pytest.raises(SystemExit) as exited:
        trade.main()
    assert exited.value.code == 1
    assert calls == []


@pytest.mark.parametrize(
    ("extra_argv", "position_quantity"),
    [
        (["--percent", "nan"], 1407),
        (["--percent", "inf"], 1407),
        (["--percent=-inf"], 1407),
        (["--quantity", "nan"], None),
        (["--quantity", "inf"], None),
        (["--quantity=-inf"], None),
        (["--percent", "20"], float("nan")),
        (["--percent", "20"], float("inf")),
    ],
)
def test_partial_non_finite_cli_exits_without_post(
    monkeypatch,
    extra_argv: list[str],
    position_quantity: float | None,
) -> None:
    label = "_".join(extra_argv).replace("-", "")
    pos = "none" if position_quantity is None else str(position_quantity)
    trade = _load_module(f"test_partial_nonfinite_{label}_{pos}", TRADE_PATH)
    calls = []
    monkeypatch.setattr(trade, "_call", _successful_call(calls))
    if position_quantity is not None:
        monkeypatch.setattr(
            trade,
            "_position_for",
            lambda symbol, account_id, side=None, qty=position_quantity: {
                "quantity": qty,
                "account_id": account_id,
                "instrument_id": symbol,
                "side": side,
                "status": "open",
            },
        )
    monkeypatch.setattr(
        sys,
        "argv",
        ["v3_trade.py", "partial", "JTOUSDT", *extra_argv, *_MANAGE_AUTH],
    )
    with pytest.raises(SystemExit) as exited:
        trade.main()
    assert exited.value.code == 1
    assert calls == []


_ENTRY_AUTH = [
    "--reason", "same-side add routing",
    "--account", "account-c",
    "--authorized-by-type", "channel",
    "--authorized-by-id", "-1002189417451",
    "--source-message-id", "tg-sig-c1002189417451-m6901",
    "--channel", "-1002189417451",
    "--ref", "tg-sig-c1002189417451-m6901",
    "--no-wait",
]


def _short_btc_mirror() -> dict[str, list]:
    return {
        "account-c": [
            {
                "symbol": "BTCUSDT",
                "position_side": "SHORT",
                "position_amt": "0.055",
                "entry_price": "80593.6",
                "mark_price": "77676",
            }
        ]
    }


def test_cli_add_posts_add_position_before_approval(monkeypatch, tmp_path: Path) -> None:
    routing_db = tmp_path / "watcher-trading.db"
    _create_routing_db(routing_db)
    monkeypatch.setenv("WATCHER_TRADING_DB", str(routing_db))
    trade = _load_module("test_cli_add_posts_add", TRADE_PATH)
    calls = []
    monkeypatch.setattr(
        trade,
        "_call",
        _successful_call(calls, positions_by_account=_short_btc_mirror()),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py", "add", "BTCUSDT", "short",
            "--entry-type", "limit", "--price", "78800",
            "--sl", "80000", "--notional", "700",
            *_ENTRY_AUTH,
        ],
    )
    trade.main()
    payload = next(
        payload
        for method, path, payload in calls
        if method == "POST" and path == "/v1/operator/orders"
    )
    assert payload["action"] == "add_position"
    assert payload["intended_action"] == "add_position"
    assert payload["signal_intent"] == "加仓"
    assert payload["client_ref"] == "tg-sig-c1002189417451-m6901"


def test_cli_open_preserves_explicit_open_even_with_same_side_position(
    monkeypatch,
    tmp_path: Path,
) -> None:
    routing_db = tmp_path / "watcher-trading.db"
    _create_routing_db(routing_db)
    monkeypatch.setenv("WATCHER_TRADING_DB", str(routing_db))
    trade = _load_module("test_cli_open_selects_add", TRADE_PATH)
    calls = []
    monkeypatch.setattr(
        trade,
        "_call",
        _successful_call(calls, positions_by_account=_short_btc_mirror()),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py", "open", "BTCUSDT", "short",
            "--entry-type", "limit", "--price", "78800",
            "--sl", "80000", "--notional", "700",
            *_ENTRY_AUTH,
        ],
    )
    trade.main()
    payload = next(
        payload
        for method, path, payload in calls
        if method == "POST" and path == "/v1/operator/orders"
    )
    assert payload["action"] == "open_position"
    assert payload["intended_action"] == "open_position"


def test_cli_open_without_same_side_posts_open_position(
    monkeypatch,
    tmp_path: Path,
) -> None:
    routing_db = tmp_path / "watcher-trading.db"
    _create_routing_db(routing_db)
    monkeypatch.setenv("WATCHER_TRADING_DB", str(routing_db))
    trade = _load_module("test_cli_open_flat_stays_open", TRADE_PATH)
    calls = []
    monkeypatch.setattr(trade, "_call", _successful_call(calls))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v3_trade.py", "open", "BTCUSDT", "short",
            "--notional", "300",
            *_ENTRY_AUTH,
        ],
    )
    trade.main()
    payload = next(
        payload
        for method, path, payload in calls
        if method == "POST" and path == "/v1/operator/orders"
    )
    assert payload["action"] == "open_position"
    assert payload["intended_action"] == "open_position"


def test_cli_submits_explicit_action_without_separate_snapshot_read(monkeypatch, tmp_path: Path) -> None:
    routing_db = tmp_path / "watcher-trading.db"
    _create_routing_db(routing_db)
    monkeypatch.setenv("WATCHER_TRADING_DB", str(routing_db))
    trade = _load_module("test_cli_stale_mirror", TRADE_PATH)
    calls = []
    monkeypatch.setattr(
        trade,
        "_call",
        _successful_call(calls, snapshot_stale=True),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["v3_trade.py", "open", "BTCUSDT", "short", "--notional", "300", *_ENTRY_AUTH],
    )
    trade.main()
    assert not any(method == "GET" and path == "/api/system/snapshot" for method, path, _ in calls)
    payload = next(payload for method, path, payload in calls if method == "POST")
    assert payload["action"] == "open_position"


@pytest.mark.parametrize("command", ["open", "add", "close", "partial", "set-sl", "set-tps", "disable-tps", "cancel", "status", "positions"])
def test_all_trade_subcommands_can_render_help(command):
    result = subprocess.run(
        [sys.executable, str(TRADE_PATH), command, "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
