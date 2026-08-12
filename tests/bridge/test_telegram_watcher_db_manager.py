import importlib.util
import inspect
import json
import re
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DB_MANAGER_PATH = (
    REPO_ROOT
    / "bridge"
    / "services"
    / "telegram-watcher"
    / "skills"
    / "crypto-trader"
    / "scripts"
    / "db_manager.py"
)

SPEC = importlib.util.spec_from_file_location("telegram_watcher_db_manager", DB_MANAGER_PATH)
assert SPEC
assert SPEC.loader
DB_MANAGER_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DB_MANAGER_MODULE)
DatabaseManager = DB_MANAGER_MODULE.DatabaseManager

BINANCE_TRADE_PATH = DB_MANAGER_PATH.with_name("binance_trade.py")
BINANCE_TRADE_SPEC = importlib.util.spec_from_file_location(
    "telegram_watcher_binance_trade",
    BINANCE_TRADE_PATH,
)
assert BINANCE_TRADE_SPEC
assert BINANCE_TRADE_SPEC.loader
BINANCE_TRADE_MODULE = importlib.util.module_from_spec(BINANCE_TRADE_SPEC)
BINANCE_TRADE_SPEC.loader.exec_module(BINANCE_TRADE_MODULE)


def install_fake_binance_client(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    constructor_calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            constructor_calls.append(
                {
                    "args": args,
                    "kwargs": kwargs,
                }
            )

    binance_module = types.ModuleType("binance")
    client_module = types.ModuleType("binance.client")
    client_module.Client = FakeClient
    binance_module.client = client_module
    monkeypatch.setitem(sys.modules, "binance", binance_module)
    monkeypatch.setitem(sys.modules, "binance.client", client_module)
    return constructor_calls


def create_legacy_database(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;

        CREATE TABLE account_configs (
            account_id          TEXT PRIMARY KEY,
            api_key             TEXT NOT NULL,
            api_secret          TEXT NOT NULL,
            default_risk_ratio  REAL DEFAULT 0.01,
            is_testnet          INTEGER DEFAULT 1
        );

        CREATE TABLE channel_routing (
            channel_id          TEXT PRIMARY KEY,
            target_account_id   TEXT NOT NULL,
            FOREIGN KEY (target_account_id) REFERENCES account_configs(account_id)
        );
        """
    )
    conn.execute(
        "INSERT INTO account_configs "
        "(account_id, api_key, api_secret, default_risk_ratio, is_testnet) "
        "VALUES (?, ?, ?, ?, ?)",
        ("legacy-main", "legacy-key", "legacy-secret", 0.02, 0),
    )
    conn.execute(
        "INSERT INTO channel_routing (channel_id, target_account_id) "
        "VALUES (?, ?)",
        ("legacy-channel", "legacy-main"),
    )
    conn.commit()
    conn.close()


def test_init_db_migrates_legacy_accounts_and_preserves_channels(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    create_legacy_database(db_path)
    manager = DatabaseManager(str(db_path))

    manager.init_db()
    manager.init_db()

    columns = {
        row["name"]
        for row in manager.conn.execute("PRAGMA table_info(account_configs)").fetchall()
    }
    assert {
        "account_type",
        "parent_account_id",
        "risk_capital_multiplier",
        "execution_account_id",
        "is_enabled",
    } <= columns
    assert manager.list_accounts() == [
        {
            "account_id": "legacy-main",
            "api_key": "legacy-key",
            "api_secret": "legacy-secret",
            "default_risk_ratio": 0.02,
            "is_testnet": 0,
            "account_type": "main",
            "parent_account_id": "",
            "risk_capital_multiplier": None,
            "execution_account_id": "legacy-main",
            "is_enabled": 0,
        }
    ]
    assert manager.list_channels() == [
        {
            "channel_id": "legacy-channel",
            "target_account_id": "legacy-main",
            "channel_name": "",
        }
    ]


def test_add_account_requires_explicit_multiplier_and_defaults_to_main(
    tmp_path: Path,
) -> None:
    manager = DatabaseManager(str(tmp_path / "legacy-call.db"))
    manager.init_db()

    missing = manager.add_account("missing", "key", "secret", 0.03, False)
    result = manager.add_account(
        "legacy",
        "key",
        "secret",
        0.03,
        False,
        risk_capital_multiplier=1.0,
    )

    assert missing["status"] == "error"
    assert "required" in missing["message"]
    assert result == {"status": "ok", "account_id": "legacy"}
    account = manager.list_accounts()[0]
    assert account["account_type"] == "main"
    assert account["parent_account_id"] == ""
    assert account["is_testnet"] == 0
    assert account["risk_capital_multiplier"] == 1.0
    assert account["execution_account_id"] == "legacy"
    assert account["is_enabled"] == 1


def test_migration_preserves_legal_multiplier_and_disables_invalid_values(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "multiplier-migration.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE account_configs (
            account_id TEXT PRIMARY KEY,
            api_key TEXT NOT NULL,
            api_secret TEXT NOT NULL,
            default_risk_ratio REAL DEFAULT 0.01,
            is_testnet INTEGER DEFAULT 1,
            account_type TEXT NOT NULL DEFAULT 'main',
            parent_account_id TEXT NOT NULL DEFAULT '',
            execution_account_id TEXT NOT NULL DEFAULT '',
            risk_capital_multiplier REAL,
            is_enabled INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO account_configs (
            account_id,
            api_key,
            api_secret,
            execution_account_id,
            risk_capital_multiplier,
            is_enabled
        )
        VALUES (?, 'key', 'secret', ?, ?, ?)
        """,
        (
            ("legal-one", "execution-legal", 1.0, 1),
            ("disabled-one", "execution-disabled", 1.0, 0),
            ("invalid-zero", "execution-zero", 0.0, 1),
            ("invalid-null", "execution-null", None, 1),
        ),
    )
    conn.commit()
    conn.close()

    manager = DatabaseManager(str(db_path))
    manager.init_db()

    rows = {
        row["account_id"]: row
        for row in manager.list_accounts()
    }
    assert rows["legal-one"]["risk_capital_multiplier"] == 1.0
    assert rows["legal-one"]["is_enabled"] == 1
    assert rows["disabled-one"]["risk_capital_multiplier"] == 1.0
    assert rows["disabled-one"]["is_enabled"] == 0
    assert rows["invalid-zero"]["risk_capital_multiplier"] == 0.0
    assert rows["invalid-zero"]["is_enabled"] == 0
    assert rows["invalid-null"]["risk_capital_multiplier"] is None
    assert rows["invalid-null"]["is_enabled"] == 0
    assert manager.set_channel(
        "disabled-channel",
        "invalid-zero",
    ) == {
        "status": "error",
        "message": "Target account is disabled",
    }


def test_adds_main_and_subaccount_with_independent_credentials(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / "accounts.db"))
    manager.init_db()

    assert manager.add_account(
        "main-live",
        "main-key",
        "main-secret",
        0.01,
        False,
        risk_capital_multiplier=1.0,
        execution_account_id="account-main",
    ) == {
        "status": "ok",
        "account_id": "main-live",
    }
    assert manager.add_account(
        "channel-sub",
        "sub-key",
        "sub-secret",
        0.02,
        False,
        "subaccount",
        "main-live",
        2.0,
        "account-sub",
    ) == {"status": "ok", "account_id": "channel-sub"}

    accounts = {row["account_id"]: row for row in manager.list_accounts()}
    assert accounts["main-live"]["account_type"] == "main"
    assert accounts["main-live"]["parent_account_id"] == ""
    assert accounts["channel-sub"]["account_type"] == "subaccount"
    assert accounts["channel-sub"]["parent_account_id"] == "main-live"
    assert accounts["channel-sub"]["api_key"] == "sub-key"
    assert accounts["channel-sub"]["api_secret"] == "sub-secret"
    assert accounts["main-live"]["risk_capital_multiplier"] == 1.0
    assert accounts["channel-sub"]["risk_capital_multiplier"] == 2.0
    assert accounts["main-live"]["execution_account_id"] == "account-main"
    assert accounts["channel-sub"]["execution_account_id"] == "account-sub"


def test_trade_loader_resolves_main_and_subaccount_credentials(tmp_path: Path) -> None:
    db_path = tmp_path / "trade-loader.db"
    manager = DatabaseManager(str(db_path))
    manager.init_db()
    manager.add_account(
        "main-live",
        "main-key",
        "main-secret",
        0.01,
        False,
        risk_capital_multiplier=1.0,
    )
    manager.add_account(
        "channel-sub",
        "sub-key",
        "sub-secret",
        0.02,
        False,
        "subaccount",
        "main-live",
        1.0,
    )

    main_credentials = BINANCE_TRADE_MODULE._load_credentials_from_db(
        str(db_path),
        "main-live",
    )
    subaccount_credentials = BINANCE_TRADE_MODULE._load_credentials_from_db(
        str(db_path),
        "channel-sub",
    )

    assert main_credentials == ("main-key", "main-secret", False)
    assert subaccount_credentials == ("sub-key", "sub-secret", False)


def test_position_size_applies_account_risk_capital_multiplier() -> None:
    result = BINANCE_TRADE_MODULE.BinanceTrader.calculate_position_size(
        balance=5000,
        risk_ratio=0.02,
        entry_price=100,
        stop_loss=90,
        risk_capital_multiplier=2,
    )

    assert result == {
        "quantity": 20.0,
        "risk_amount": 200.0,
        "distance": 10,
        "actual_equity": 5000,
        "effective_equity": 10000,
        "effective_balance": 10000,
        "risk_capital_multiplier": 2,
    }

    with pytest.raises(ValueError, match="greater than 0"):
        BINANCE_TRADE_MODULE.BinanceTrader.calculate_position_size(
            balance=5000,
            risk_ratio=0.02,
            entry_price=100,
            stop_loss=90,
            risk_capital_multiplier=0,
        )


def test_position_size_tracks_live_equity_with_a_fixed_configured_multiplier() -> None:
    multiplier = (
        BINANCE_TRADE_MODULE.BinanceTrader.calculate_risk_capital_multiplier(
            initial_actual_equity=4500,
            target_effective_equity=9000,
        )
    )

    initial = BINANCE_TRADE_MODULE.BinanceTrader.calculate_position_size(
        balance=4500,
        risk_ratio=0.02,
        entry_price=100,
        stop_loss=90,
        risk_capital_multiplier=multiplier,
    )
    after_loss = BINANCE_TRADE_MODULE.BinanceTrader.calculate_position_size(
        balance=4200,
        risk_ratio=0.02,
        entry_price=100,
        stop_loss=90,
        risk_capital_multiplier=multiplier,
    )
    after_profit = BINANCE_TRADE_MODULE.BinanceTrader.calculate_position_size(
        balance=4800,
        risk_ratio=0.02,
        entry_price=100,
        stop_loss=90,
        risk_capital_multiplier=multiplier,
    )

    assert multiplier == 2
    assert initial["effective_equity"] == 9000
    assert after_loss["effective_equity"] == 8400
    assert after_profit["effective_equity"] == 9600
    assert after_loss["quantity"] < initial["quantity"] < after_profit["quantity"]

    sizing_source = inspect.getsource(
        BINANCE_TRADE_MODULE.BinanceTrader.calculate_position_size
    )
    assert "9000" not in sizing_source


def test_calc_position_cli_applies_saved_multiplier_to_current_actual_equity() -> None:
    scenarios = (
        (4200, 8400, 16.8),
        (4800, 9600, 19.2),
    )

    for actual_equity, expected_effective_equity, expected_quantity in scenarios:
        completed = subprocess.run(
            [
                sys.executable,
                str(BINANCE_TRADE_PATH),
                "calc-position",
                "--equity",
                str(actual_equity),
                "--capital-multiplier",
                "2",
                "--risk-ratio",
                "0.02",
                "--entry",
                "100",
                "--sl",
                "90",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        assert result["actual_equity"] == actual_equity
        assert result["effective_equity"] == expected_effective_equity
        assert result["quantity"] == expected_quantity
        assert result["risk_capital_multiplier"] == 2


def test_calc_position_cli_requires_saved_multiplier() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(BINANCE_TRADE_PATH),
            "calc-position",
            "--equity",
            "5000",
            "--risk-ratio",
            "0.02",
            "--entry",
            "100",
            "--sl",
            "90",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "--capital-multiplier" in completed.stderr
    assert "required" in completed.stderr


def test_cli_help_defines_explicit_unauthenticated_proxy_sources() -> None:
    help_text = BINANCE_TRADE_MODULE.build_parser().format_help()

    assert "Explicit unauthenticated http(s) proxy URL" in help_text
    assert "BINANCE_PROXY" in help_text
    assert "connects directly" in help_text
    assert "built-in proxy" not in help_text
    assert "--proxy none" not in help_text


def test_calc_position_help_defines_dynamic_equity_inputs() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(BINANCE_TRADE_PATH),
            "calc-position",
            "--help",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    normalized_help = " ".join(completed.stdout.split())

    assert "Current live actual equity" in normalized_help
    assert (
        "Saved risk capital multiplier applied to current actual equity"
        in normalized_help
    )


def test_explicit_binance_proxy_takes_priority_over_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls = install_fake_binance_client(monkeypatch)
    monkeypatch.setenv("BINANCE_PROXY", "http://env-proxy.internal:13128")

    BINANCE_TRADE_MODULE.BinanceTrader(
        "key",
        "secret",
        testnet=False,
        proxy="https://explicit-proxy.internal:8443",
    )

    assert constructor_calls[-1]["kwargs"]["requests_params"] == {
        "proxies": {
            "http": "https://explicit-proxy.internal:8443",
            "https": "https://explicit-proxy.internal:8443",
        }
    }


def test_binance_proxy_reads_environment_when_argument_is_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls = install_fake_binance_client(monkeypatch)
    monkeypatch.setenv("BINANCE_PROXY", "http://env-proxy.internal:13128")

    BINANCE_TRADE_MODULE.BinanceTrader(
        "key",
        "secret",
        testnet=False,
    )

    assert constructor_calls[-1]["kwargs"]["requests_params"] == {
        "proxies": {
            "http": "http://env-proxy.internal:13128",
            "https": "http://env-proxy.internal:13128",
        }
    }


def test_binance_proxy_defaults_to_direct_connection_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls = install_fake_binance_client(monkeypatch)
    monkeypatch.delenv("BINANCE_PROXY", raising=False)

    BINANCE_TRADE_MODULE.BinanceTrader(
        "key",
        "secret",
        testnet=False,
    )

    assert constructor_calls[-1]["kwargs"]["requests_params"] is None


@pytest.mark.parametrize(
    ("proxy_url", "message"),
    (
        ("socks5://proxy.internal:1080", "http\\(s\\) URL"),
        ("none", "http\\(s\\) URL"),
        (
            "http://proxy-user:proxy-password@proxy.internal:13128",
            "must not contain credentials",
        ),
    ),
)
def test_binance_proxy_rejects_unsupported_or_authenticated_urls(
    monkeypatch: pytest.MonkeyPatch,
    proxy_url: str,
    message: str,
) -> None:
    install_fake_binance_client(monkeypatch)

    with pytest.raises(ValueError, match=message):
        BINANCE_TRADE_MODULE.BinanceTrader(
            "key",
            "secret",
            testnet=False,
            proxy=proxy_url,
        )


def test_binance_proxy_has_no_embedded_credentials() -> None:
    source = BINANCE_TRADE_PATH.read_text(encoding="utf-8")

    assert "BINANCE_PROXY" in source
    assert "DEFAULT_PROXY" not in source
    assert re.search(r"https?://[^\\s\"']+:[^\\s\"']+@", source) is None


def test_capital_multiplier_initialization_uses_configured_target() -> None:
    calculate = (
        BINANCE_TRADE_MODULE.BinanceTrader.calculate_risk_capital_multiplier
    )

    assert calculate(4500, 9000) == 2
    assert calculate(4500, 11250) == 2.5

    with pytest.raises(ValueError, match="initial_actual_equity"):
        calculate(0, 9000)
    with pytest.raises(ValueError, match="target_effective_equity"):
        calculate(4500, 0)


def test_get_equity_reads_current_total_margin_balance() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.total_margin_balance = "4500.25"

        def futures_account(self) -> dict:
            return {"totalMarginBalance": self.total_margin_balance}

    trader = object.__new__(BINANCE_TRADE_MODULE.BinanceTrader)
    trader.client = FakeClient()

    assert trader.get_equity() == 4500.25
    trader.client.total_margin_balance = "4388.75"
    assert trader.get_equity() == 4388.75


def test_rejects_invalid_subaccount_parent_relationships(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / "invalid-parents.db"))
    manager.init_db()
    manager.add_account(
        "main-live",
        "key",
        "secret",
        is_testnet=False,
        risk_capital_multiplier=1.0,
    )
    manager.add_account(
        "existing-sub",
        "key",
        "secret",
        is_testnet=False,
        account_type="subaccount",
        parent_account_id="main-live",
        risk_capital_multiplier=1.0,
    )

    invalid_type = manager.add_account(
        "invalid-type",
        "key",
        "secret",
        account_type="secondary",
        risk_capital_multiplier=1.0,
    )
    missing_parent = manager.add_account(
        "missing-parent",
        "key",
        "secret",
        account_type="subaccount",
        risk_capital_multiplier=1.0,
    )
    unknown_parent = manager.add_account(
        "unknown-parent",
        "key",
        "secret",
        account_type="subaccount",
        parent_account_id="unknown",
        risk_capital_multiplier=1.0,
    )
    self_parent = manager.add_account(
        "self-parent",
        "key",
        "secret",
        account_type="subaccount",
        parent_account_id="self-parent",
        risk_capital_multiplier=1.0,
    )
    nested_subaccount = manager.add_account(
        "nested-subaccount",
        "key",
        "secret",
        is_testnet=False,
        account_type="subaccount",
        parent_account_id="existing-sub",
        risk_capital_multiplier=1.0,
    )
    environment_mismatch = manager.add_account(
        "testnet-sub",
        "key",
        "secret",
        is_testnet=True,
        account_type="subaccount",
        parent_account_id="main-live",
        risk_capital_multiplier=1.0,
    )
    main_with_parent = manager.add_account(
        "main-with-parent",
        "key",
        "secret",
        account_type="main",
        parent_account_id="main-live",
        risk_capital_multiplier=1.0,
    )
    invalid_multiplier = manager.add_account(
        "invalid-multiplier",
        "key",
        "secret",
        risk_capital_multiplier=0,
    )

    for result in (
        invalid_type,
        missing_parent,
        unknown_parent,
        self_parent,
        nested_subaccount,
        environment_mismatch,
        main_with_parent,
        invalid_multiplier,
    ):
        assert result["status"] == "error"


def test_channels_continue_to_route_to_main_and_subaccounts(tmp_path: Path) -> None:
    manager = DatabaseManager(str(tmp_path / "channels.db"))
    manager.init_db()
    manager.add_account(
        "main-testnet",
        "key",
        "secret",
        risk_capital_multiplier=1.0,
    )
    manager.add_account(
        "sub-testnet",
        "sub-key",
        "sub-secret",
        account_type="subaccount",
        parent_account_id="main-testnet",
        risk_capital_multiplier=1.0,
    )

    manager.set_channel("main-channel", "main-testnet", "Main Channel")
    manager.set_channel("sub-channel", "sub-testnet", "Sub Channel")

    channels = {
        row["channel_id"]: row["target_account_id"]
        for row in manager.list_channels()
    }
    assert channels == {
        "main-channel": "main-testnet",
        "sub-channel": "sub-testnet",
    }


def test_existing_unicode_credential_aliases_can_bind_execution_accounts(
    tmp_path: Path,
) -> None:
    manager = DatabaseManager(str(tmp_path / "unicode-accounts.db"))
    manager.init_db()

    main = manager.add_account(
        "jiataotx@gmail.com",
        "main-key",
        "main-secret",
        is_testnet=False,
        execution_account_id="account-a",
        risk_capital_multiplier=1.0,
    )
    subaccount = manager.add_account(
        "泰山",
        "sub-key",
        "sub-secret",
        is_testnet=False,
        account_type="subaccount",
        parent_account_id="jiataotx@gmail.com",
        execution_account_id="account-c",
        risk_capital_multiplier=2,
    )

    assert main == {
        "status": "ok",
        "account_id": "jiataotx@gmail.com",
    }
    assert subaccount == {"status": "ok", "account_id": "泰山"}
    accounts = {
        row["account_id"]: row
        for row in manager.list_accounts()
    }
    assert accounts["泰山"]["parent_account_id"] == (
        "jiataotx@gmail.com"
    )
    assert accounts["泰山"]["execution_account_id"] == "account-c"


def test_add_account_cli_supports_subaccount_flags(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.db"

    subprocess.run(
        [sys.executable, str(DB_MANAGER_PATH), "--db", str(db_path), "init-db"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(DB_MANAGER_PATH),
            "--db",
            str(db_path),
            "add-account",
            "main",
            "main-key",
            "main-secret",
            "--capital-multiplier",
            "1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [
            sys.executable,
            str(DB_MANAGER_PATH),
            "--db",
            str(db_path),
            "add-account",
            "sub",
            "sub-key",
            "sub-secret",
            "--type",
            "subaccount",
            "--parent-account",
            "main",
            "--capital-multiplier",
            "2",
            "--execution-account",
            "account-sub",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    listed = subprocess.run(
        [
            sys.executable,
            str(DB_MANAGER_PATH),
            "--db",
            str(db_path),
            "list-accounts",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {"status": "ok", "account_id": "sub"}
    accounts = {row["account_id"]: row for row in json.loads(listed.stdout)}
    assert accounts["sub"]["account_type"] == "subaccount"
    assert accounts["sub"]["parent_account_id"] == "main"
    assert accounts["sub"]["risk_capital_multiplier"] == 2.0
    assert accounts["sub"]["execution_account_id"] == "account-sub"
