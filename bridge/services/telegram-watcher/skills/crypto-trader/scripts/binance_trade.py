#!/usr/bin/env python3
"""
Binance Futures Trading CLI Tool

CLI tool and importable module for interacting with Binance Futures.
Claude calls this script via Bash to place orders, query positions, etc.

Usage (CLI):
    python3 binance_trade.py get-price BTCUSDT
    python3 binance_trade.py --db ~/projects/trading-data/trading.db --account main place-order BTCUSDT BUY 0.01
    python3 binance_trade.py calc-position --equity 10000 --capital-multiplier 1 --risk-ratio 0.02 --entry 60000 --sl 59000
    python3 binance_trade.py calc-capital-multiplier --initial-equity 5000 --target-equity 10000

Usage (import):
    from binance_trade import BinanceTrader
    trader = BinanceTrader(api_key="...", api_secret="...", testnet=True)
    price = trader.get_price("BTCUSDT")
"""

import argparse
import json
import math
import os
import sys
from urllib.parse import urlsplit


# ---------------------------------------------------------------------------
# BinanceTrader class (importable API)
# ---------------------------------------------------------------------------
class BinanceTrader:
    """High-level interface to Binance Futures trading."""

    TESTNET_URL = "https://testnet.binancefuture.com"
    PROXY_ENV = "BINANCE_PROXY"

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True, proxy: str = None):
        from binance.client import Client

        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet

        proxy_url = proxy
        if proxy_url is None:
            proxy_url = os.environ.get(self.PROXY_ENV, "")
        proxy_url = proxy_url.strip()
        requests_params = None
        if proxy_url:
            parsed_proxy = urlsplit(proxy_url)
            if (
                parsed_proxy.scheme not in {"http", "https"}
                or not parsed_proxy.hostname
            ):
                raise ValueError(
                    "--proxy or BINANCE_PROXY must be an http(s) URL"
                )
            if (
                parsed_proxy.username is not None
                or parsed_proxy.password is not None
            ):
                raise ValueError(
                    "--proxy or BINANCE_PROXY must not contain credentials"
                )
            requests_params = {
                "proxies": {
                    "https": proxy_url,
                    "http": proxy_url,
                }
            }

        if testnet:
            self.client = Client(api_key, api_secret, testnet=True, requests_params=requests_params)
            self.client.FUTURES_URL = self.TESTNET_URL
        else:
            self.client = Client(api_key, api_secret, requests_params=requests_params)

    # -- market data ---------------------------------------------------------
    def get_price(self, symbol: str) -> float:
        """Get the current futures price for a symbol."""
        ticker = self.client.futures_symbol_ticker(symbol=symbol)
        return float(ticker["price"])

    def get_bookticker(self, symbol: str) -> dict:
        """Get best bid/ask (bid1/ask1) for a symbol."""
        book = self.client.futures_orderbook_ticker(symbol=symbol)
        return {
            "symbol": symbol,
            "bidPrice": float(book["bidPrice"]),
            "bidQty": float(book["bidQty"]),
            "askPrice": float(book["askPrice"]),
            "askQty": float(book["askQty"]),
        }

    # -- account -------------------------------------------------------------
    def get_balance(self, asset: str = "USDT") -> float:
        """Get futures wallet balance for a given asset."""
        balances = self.client.futures_account_balance()
        for b in balances:
            if b["asset"] == asset:
                return float(b["balance"])
        return 0.0

    def get_equity(self) -> float:
        """Get current USDT-M account equity, including unrealized PnL."""
        account = self.client.futures_account()
        raw_equity = account.get("totalMarginBalance")
        if raw_equity is None or raw_equity == "":
            raise ValueError("totalMarginBalance missing from futures account")

        equity = float(raw_equity)
        if not math.isfinite(equity) or equity < 0:
            raise ValueError("totalMarginBalance must be a non-negative number")
        return equity

    # -- position sizing (pure math) ----------------------------------------
    @staticmethod
    def calculate_risk_capital_multiplier(
        initial_actual_equity: float,
        target_effective_equity: float,
    ) -> float:
        """Calculate the one-time configured multiplier for an account."""
        if (
            not math.isfinite(initial_actual_equity)
            or initial_actual_equity <= 0
        ):
            raise ValueError("initial_actual_equity must be greater than 0")
        if (
            not math.isfinite(target_effective_equity)
            or target_effective_equity <= 0
        ):
            raise ValueError("target_effective_equity must be greater than 0")
        return target_effective_equity / initial_actual_equity

    @staticmethod
    def calculate_position_size(
        balance: float,
        risk_ratio: float,
        entry_price: float,
        stop_loss: float,
        risk_capital_multiplier: float,
    ) -> dict:
        """Calculate position size based on risk parameters.

        Formula:
        actual_equity = current live account equity
        effective_equity = actual_equity * risk_capital_multiplier
        qty = (effective_equity * risk_ratio) / abs(entry_price - stop_loss)

        The balance argument is retained for compatibility and represents
        current actual equity. The configured multiplier stays fixed while
        actual equity changes with profit and loss.
        If entry_price == stop_loss, returns quantity=0 to prevent division by zero.
        """
        if not math.isfinite(balance) or balance < 0:
            raise ValueError("actual equity must be a non-negative number")
        if not math.isfinite(risk_ratio) or risk_ratio < 0:
            raise ValueError("risk_ratio must be a non-negative number")
        if (
            not math.isfinite(risk_capital_multiplier)
            or risk_capital_multiplier <= 0
        ):
            raise ValueError("risk_capital_multiplier must be greater than 0")

        distance = abs(entry_price - stop_loss)
        effective_equity = balance * risk_capital_multiplier
        risk_amount = effective_equity * risk_ratio

        if distance == 0:
            return {
                "quantity": 0.0,
                "risk_amount": risk_amount,
                "distance": 0.0,
                "actual_equity": balance,
                "effective_equity": effective_equity,
                "effective_balance": effective_equity,
                "risk_capital_multiplier": risk_capital_multiplier,
            }

        quantity = risk_amount / distance
        return {
            "quantity": round(quantity, 8),
            "risk_amount": round(risk_amount, 8),
            "distance": round(distance, 8),
            "actual_equity": round(balance, 8),
            "effective_equity": round(effective_equity, 8),
            "effective_balance": round(effective_equity, 8),
            "risk_capital_multiplier": risk_capital_multiplier,
        }

    # -- order placement -----------------------------------------------------
    def place_market_order(self, symbol: str, side: str, quantity: float,
                           position_side: str = None) -> dict:
        """Place a MARKET order on futures."""
        params = dict(
            symbol=symbol,
            side=side.upper(),
            type="MARKET",
            quantity=quantity,
        )
        if position_side:
            params["positionSide"] = position_side.upper()
        order = self.client.futures_create_order(**params)
        return order

    def place_limit_order(
        self, symbol: str, side: str, quantity: float, price: float,
        position_side: str = None,
    ) -> dict:
        """Place a LIMIT order on futures with GTC time-in-force."""
        params = dict(
            symbol=symbol,
            side=side.upper(),
            type="LIMIT",
            quantity=quantity,
            price=price,
            timeInForce="GTC",
        )
        if position_side:
            params["positionSide"] = position_side.upper()
        order = self.client.futures_create_order(**params)
        return order

    def place_stop_loss(
        self, symbol: str, side: str, quantity: float, stop_price: float,
        position_side: str = None,
    ) -> dict:
        """Place a STOP_MARKET order (stop loss) with reduceOnly=true."""
        params = dict(
            symbol=symbol,
            side=side.upper(),
            type="STOP_MARKET",
            quantity=quantity,
            stopPrice=stop_price,
        )
        if position_side:
            params["positionSide"] = position_side.upper()
        else:
            params["reduceOnly"] = "true"
        order = self.client.futures_create_order(**params)
        return order

    def place_take_profit(
        self, symbol: str, side: str, quantity: float, stop_price: float,
        position_side: str = None,
    ) -> dict:
        """Place a TAKE_PROFIT_MARKET order with reduceOnly=true."""
        params = dict(
            symbol=symbol,
            side=side.upper(),
            type="TAKE_PROFIT_MARKET",
            quantity=quantity,
            stopPrice=stop_price,
        )
        if position_side:
            params["positionSide"] = position_side.upper()
        else:
            params["reduceOnly"] = "true"
        order = self.client.futures_create_order(**params)
        return order

    # -- order management ----------------------------------------------------
    def cancel_order(self, symbol: str, order_id: int) -> dict:
        """Cancel a specific order by orderId."""
        result = self.client.futures_cancel_order(
            symbol=symbol, orderId=order_id
        )
        return result

    def cancel_all_orders(self, symbol: str) -> dict:
        """Cancel all open orders for a symbol."""
        self.client.futures_cancel_all_open_orders(symbol=symbol)
        return {"status": "ok", "symbol": symbol}

    # -- position / orders query ---------------------------------------------
    def get_position(self, symbol: str) -> dict | None:
        """Get current position for a symbol. Returns None if no position."""
        positions = self.client.futures_position_information(symbol=symbol)
        for pos in positions:
            if float(pos.get("positionAmt", 0)) != 0:
                return pos
        return None

    def get_open_orders(self, symbol: str) -> list:
        """Get all open orders for a symbol (including algo/conditional orders like SL/TP)."""
        regular = self.client.futures_get_open_orders(symbol=symbol)
        try:
            algo = self.client.futures_get_open_algo_orders(symbol=symbol)
        except Exception:
            algo = []
        return regular + algo

    # -- leverage ------------------------------------------------------------
    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Set leverage for a symbol."""
        result = self.client.futures_change_leverage(
            symbol=symbol, leverage=leverage
        )
        return result


# ---------------------------------------------------------------------------
# Credential loading helpers
# ---------------------------------------------------------------------------
def _load_credentials_from_db(db_path: str, account_id: str) -> tuple[str, str, bool]:
    """Load API credentials from the trading database.

    Returns (api_key, api_secret, is_testnet).
    Raises SystemExit with JSON error on failure.
    """
    # Add scripts directory to path so we can import db_manager
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    try:
        from db_manager import DatabaseManager
    except ImportError:
        _error_exit("Cannot import db_manager. Ensure db_manager.py is in the same directory.")

    db = DatabaseManager(db_path=db_path)
    accounts = db.list_accounts()
    for acct in accounts:
        if acct["account_id"] == account_id:
            return acct["api_key"], acct["api_secret"], bool(acct.get("is_testnet", 1))

    _error_exit(f"Account '{account_id}' not found in database '{db_path}'")


def _resolve_credentials(args) -> tuple[str, str, bool]:
    """Resolve API credentials following priority: CLI > DB > env.

    Returns (api_key, api_secret, testnet).
    """
    # Priority 1: CLI arguments
    if args.api_key and args.api_secret:
        return args.api_key, args.api_secret, args.testnet

    # Priority 2: Database + account
    if args.db and args.account:
        api_key, api_secret, is_testnet = _load_credentials_from_db(args.db, args.account)
        # Use DB setting by default; only override if user explicitly passed --testnet or --no-testnet
        testnet = is_testnet
        return api_key, api_secret, testnet

    # Priority 3: Environment variables
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if api_key and api_secret:
        return api_key, api_secret, args.testnet

    _error_exit(
        "No API credentials provided. Use --api-key/--api-secret, "
        "--db/--account, or set BINANCE_API_KEY/BINANCE_API_SECRET env vars."
    )


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def _json_out(data) -> None:
    """Print data as compact JSON to stdout."""
    print(json.dumps(data, ensure_ascii=False))


def _error_exit(message: str) -> None:
    """Print a JSON error message and exit with code 1."""
    _json_out({"status": "error", "message": message})
    sys.exit(1)


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Binance Futures trading CLI tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Global arguments (before subcommand)
    parser.add_argument("--api-key", default=None, help="Binance API Key (or env BINANCE_API_KEY)")
    parser.add_argument("--api-secret", default=None, help="Binance API Secret (or env BINANCE_API_SECRET)")
    parser.add_argument("--testnet", action=argparse.BooleanOptionalAction, default=True, help="Use testnet (default: True). Use --no-testnet for production.")
    parser.add_argument(
        "--proxy",
        default=None,
        help=(
            "Explicit unauthenticated http(s) proxy URL. When omitted, reads "
            "BINANCE_PROXY; when both are unset, connects directly."
        ),
    )
    parser.add_argument("--db", default=None, help="Path to SQLite database for credential lookup")
    parser.add_argument("--account", default=None, help="Account ID to load from database")

    sub = parser.add_subparsers(dest="command")

    # 1. get-price
    p = sub.add_parser("get-price", help="Get current futures price for a symbol")
    p.add_argument("symbol", help="Trading pair symbol (e.g. BTCUSDT)")

    # 1b. get-bookticker
    p = sub.add_parser("get-bookticker", help="Get best bid/ask (bid1/ask1) for a symbol")
    p.add_argument("symbol", help="Trading pair symbol (e.g. BTCUSDT)")

    # 2. get-balance
    p = sub.add_parser("get-balance", help="Get futures wallet balance")
    p.add_argument("--asset", default="USDT", help="Asset to query (default: USDT)")

    # 2b. get-equity
    sub.add_parser(
        "get-equity",
        help="Get current account equity including unrealized PnL",
    )

    # 3. calc-capital-multiplier
    p = sub.add_parser(
        "calc-capital-multiplier",
        help="Calculate an initial risk capital multiplier",
    )
    p.add_argument(
        "--initial-equity",
        type=float,
        required=True,
        help="Actual account equity at initialization",
    )
    p.add_argument(
        "--target-equity",
        type=float,
        required=True,
        help=(
            "Initialization-only target effective equity; runtime saves and "
            "uses the resulting multiplier"
        ),
    )

    # 4. calc-position
    p = sub.add_parser("calc-position", help="Calculate position size (pure math, no API)")
    p.add_argument(
        "--equity",
        "--balance",
        dest="actual_equity",
        type=float,
        required=True,
        help=(
            "Current live actual equity; --balance remains a compatible alias"
        ),
    )
    p.add_argument("--risk-ratio", type=float, required=True, help="Risk ratio (e.g. 0.02 for 2%%)")
    p.add_argument(
        "--capital-multiplier",
        type=float,
        required=True,
        help=(
            "Saved risk capital multiplier applied to current actual equity"
        ),
    )
    p.add_argument("--entry", type=float, required=True, help="Entry price")
    p.add_argument("--sl", type=float, required=True, help="Stop loss price")

    # 5. place-order
    p = sub.add_parser("place-order", help="Place an order (MARKET or LIMIT)")
    p.add_argument("symbol", help="Trading pair symbol")
    p.add_argument("side", help="BUY or SELL")
    p.add_argument("quantity", type=float, help="Order quantity")
    p.add_argument("--type", dest="order_type", default="MARKET", choices=["MARKET", "LIMIT"], help="Order type (default: MARKET)")
    p.add_argument("--price", type=float, default=None, help="Limit price (required for LIMIT orders)")
    p.add_argument("--position-side", default=None, choices=["LONG", "SHORT", "BOTH"], help="Position side for hedge mode (LONG/SHORT/BOTH)")

    # 6. place-sl
    p = sub.add_parser("place-sl", help="Place a stop loss order (STOP_MARKET, reduceOnly)")
    p.add_argument("symbol", help="Trading pair symbol")
    p.add_argument("side", help="BUY or SELL (opposite of position direction)")
    p.add_argument("quantity", type=float, help="Order quantity")
    p.add_argument("stop_price", type=float, help="Stop trigger price")
    p.add_argument("--position-side", default=None, choices=["LONG", "SHORT", "BOTH"], help="Position side for hedge mode")

    # 7. place-tp
    p = sub.add_parser("place-tp", help="Place a take profit order (TAKE_PROFIT_MARKET, reduceOnly)")
    p.add_argument("symbol", help="Trading pair symbol")
    p.add_argument("side", help="BUY or SELL (opposite of position direction)")
    p.add_argument("quantity", type=float, help="Order quantity")
    p.add_argument("stop_price", type=float, help="Take profit trigger price")
    p.add_argument("--position-side", default=None, choices=["LONG", "SHORT", "BOTH"], help="Position side for hedge mode")

    # 8. cancel-order
    p = sub.add_parser("cancel-order", help="Cancel a specific order")
    p.add_argument("symbol", help="Trading pair symbol")
    p.add_argument("order_id", type=int, help="Binance order ID to cancel")

    # 9. cancel-all
    p = sub.add_parser("cancel-all", help="Cancel all open orders for a symbol")
    p.add_argument("symbol", help="Trading pair symbol")

    # 10. get-position
    p = sub.add_parser("get-position", help="Query current position for a symbol")
    p.add_argument("symbol", help="Trading pair symbol")

    # 11. get-orders
    p = sub.add_parser("get-orders", help="Query all open orders for a symbol")
    p.add_argument("symbol", help="Trading pair symbol")

    # 12. set-leverage
    p = sub.add_parser("set-leverage", help="Set leverage for a symbol")
    p.add_argument("symbol", help="Trading pair symbol")
    p.add_argument("leverage", type=int, help="Leverage multiplier (e.g. 10)")

    return parser


# ---------------------------------------------------------------------------
# CLI main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        _error_exit("No command specified. Use --help for usage.")

    # Pure calculations need no API credentials.
    if args.command == "calc-capital-multiplier":
        multiplier = BinanceTrader.calculate_risk_capital_multiplier(
            initial_actual_equity=args.initial_equity,
            target_effective_equity=args.target_equity,
        )
        _json_out(
            {
                "initial_actual_equity": args.initial_equity,
                "target_effective_equity": args.target_equity,
                "risk_capital_multiplier": multiplier,
            }
        )
        return

    if args.command == "calc-position":
        result = BinanceTrader.calculate_position_size(
            balance=args.actual_equity,
            risk_ratio=args.risk_ratio,
            entry_price=args.entry,
            stop_loss=args.sl,
            risk_capital_multiplier=args.capital_multiplier,
        )
        _json_out(result)
        return

    # All other commands require API credentials
    try:
        api_key, api_secret, testnet = _resolve_credentials(args)
    except SystemExit:
        raise
    except Exception as e:
        _error_exit(f"Failed to resolve credentials: {e}")
        return  # unreachable, but satisfies type checker

    try:
        trader = BinanceTrader(api_key, api_secret, testnet=testnet, proxy=getattr(args, 'proxy', None))
    except Exception as e:
        _error_exit(f"Failed to initialize Binance client: {e}")
        return

    try:
        match args.command:
            case "get-price":
                price = trader.get_price(args.symbol)
                _json_out({"symbol": args.symbol, "price": price})

            case "get-bookticker":
                book = trader.get_bookticker(args.symbol)
                _json_out(book)

            case "get-balance":
                balance = trader.get_balance(args.asset)
                _json_out({"asset": args.asset, "balance": balance})

            case "get-equity":
                equity = trader.get_equity()
                _json_out({"asset": "USDT", "equity": equity})

            case "place-order":
                ps = getattr(args, 'position_side', None)
                if args.order_type == "LIMIT":
                    if args.price is None:
                        _error_exit("--price is required for LIMIT orders")
                    result = trader.place_limit_order(
                        args.symbol, args.side, args.quantity, args.price,
                        position_side=ps,
                    )
                else:
                    result = trader.place_market_order(
                        args.symbol, args.side, args.quantity,
                        position_side=ps,
                    )
                _json_out(result)

            case "place-sl":
                ps = getattr(args, 'position_side', None)
                result = trader.place_stop_loss(
                    args.symbol, args.side, args.quantity, args.stop_price,
                    position_side=ps,
                )
                _json_out(result)

            case "place-tp":
                ps = getattr(args, 'position_side', None)
                result = trader.place_take_profit(
                    args.symbol, args.side, args.quantity, args.stop_price,
                    position_side=ps,
                )
                _json_out(result)

            case "cancel-order":
                result = trader.cancel_order(args.symbol, args.order_id)
                _json_out({"status": "ok", "orderId": result.get("orderId", args.order_id)})

            case "cancel-all":
                result = trader.cancel_all_orders(args.symbol)
                _json_out(result)

            case "get-position":
                pos = trader.get_position(args.symbol)
                if pos is None:
                    _json_out({"position": None})
                else:
                    _json_out(pos)

            case "get-orders":
                orders = trader.get_open_orders(args.symbol)
                _json_out({"orders": orders})

            case "set-leverage":
                trader.set_leverage(args.symbol, args.leverage)
                _json_out({
                    "status": "ok",
                    "symbol": args.symbol,
                    "leverage": args.leverage,
                })

            case _:
                parser.print_help()
                sys.exit(1)

    except SystemExit:
        raise
    except Exception as e:
        _error_exit(str(e))


if __name__ == "__main__":
    main()
