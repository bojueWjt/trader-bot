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

# Configuration writes and migrations are owned by watcher; corresponding
# coverage lives in bridge/services/telegram-watcher/__tests__/trading-api.test.js.

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


def test_trade_loader_resolves_main_and_subaccount_credentials(tmp_path: Path) -> None:
    db_path = tmp_path / "trade-loader.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(DB_MANAGER_MODULE.SCHEMA_SQL)
        conn.executemany(
            """
            INSERT INTO account_configs (
                account_id, api_key, api_secret, default_risk_ratio, is_testnet,
                account_type, parent_account_id, risk_capital_multiplier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("main-live", "fake-main-key", "fake-main-secret", 0.01, 0,
                 "main", "", 1.0),
                ("channel-sub", "fake-sub-key", "fake-sub-secret", 0.02, 0,
                 "subaccount", "main-live", 1.0),
            ],
        )

    main_credentials = BINANCE_TRADE_MODULE._load_credentials_from_db(
        str(db_path),
        "main-live",
    )
    subaccount_credentials = BINANCE_TRADE_MODULE._load_credentials_from_db(
        str(db_path),
        "channel-sub",
    )

    assert main_credentials == ("fake-main-key", "fake-main-secret", False)
    assert subaccount_credentials == ("fake-sub-key", "fake-sub-secret", False)


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


def test_add_account_cli_rejects_configuration_write(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.db"
    result = subprocess.run(
        [
            sys.executable,
            str(DB_MANAGER_PATH),
            "--db",
            str(db_path),
            "add-account",
            "sub",
            "fake-sub-key",
            "fake-sub-secret",
            "--type",
            "subaccount",
            "--parent-account",
            "main",
            "--capital-multiplier",
            "2",
            "--execution-account",
            "account-sub",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "watcher site or app" in result.stderr
    assert "fake-sub-key" not in result.stderr
    assert "fake-sub-secret" not in result.stderr
    assert not db_path.exists()
