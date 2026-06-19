from freqtrade.signal_strategy import freqtrade_positions
from freqtrade.signal_strategy.freqtrade_positions import get_open_position


def set_freqtrade_env(monkeypatch):
    monkeypatch.setenv("FREQTRADE_BASE_URL", "http://freqtrade.local")
    monkeypatch.setenv("FREQTRADE_API_USER", "api-user")
    monkeypatch.setenv("FREQTRADE_API_PASSWORD", "api-password")


def test_get_open_position_returns_matching_trade(monkeypatch):
    set_freqtrade_env(monkeypatch)
    calls = []

    def fake_fetch(base_url, api_user, api_password):
        calls.append((base_url, api_user, api_password))
        return {
            "trades": [
                {
                    "trade_id": 42,
                    "pair": "BTC/USDT:USDT",
                    "is_short": False,
                    "amount": 0.25,
                    "open_rate": 65000.0,
                    "profit_pct": 1.7,
                }
            ]
        }

    monkeypatch.setattr(freqtrade_positions, "_fetch_open_trades", fake_fetch)

    position = get_open_position("BTC/USDT:USDT")

    assert position == {
        "pair": "BTC/USDT:USDT",
        "side": "long",
        "amount": 0.25,
        "open_rate": 65000.0,
        "current_profit_pct": 1.7,
        "trade_id": 42,
    }
    assert calls == [("http://freqtrade.local", "api-user", "api-password")]
    assert freqtrade_positions.position_query_succeeded() is True


def test_get_open_position_returns_none_when_pair_has_no_open_trade(monkeypatch):
    set_freqtrade_env(monkeypatch)

    def fake_fetch(base_url, api_user, api_password):
        return {"trades": [{"trade_id": 7, "pair": "ETH/USDT:USDT"}]}

    monkeypatch.setattr(freqtrade_positions, "_fetch_open_trades", fake_fetch)

    assert get_open_position("BTC/USDT:USDT") is None
    assert freqtrade_positions.position_query_succeeded() is True


def test_get_open_position_safely_degrades_when_freqtrade_unreachable(monkeypatch):
    set_freqtrade_env(monkeypatch)

    def fake_fetch(base_url, api_user, api_password):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(freqtrade_positions, "_fetch_open_trades", fake_fetch)

    assert get_open_position("BTC/USDT:USDT") is None
    assert freqtrade_positions.position_query_succeeded() is False


def test_get_open_position_returns_none_when_env_missing(monkeypatch):
    monkeypatch.delenv("FREQTRADE_BASE_URL", raising=False)
    monkeypatch.delenv("FREQTRADE_API_USER", raising=False)
    monkeypatch.delenv("FREQTRADE_API_PASSWORD", raising=False)

    assert get_open_position("BTC/USDT:USDT") is None
    assert freqtrade_positions.position_query_succeeded() is False
