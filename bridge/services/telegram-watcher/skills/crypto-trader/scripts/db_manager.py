#!/usr/bin/env python3
"""
Database Manager for Crypto Trading System

CLI tool and importable module for managing the SQLite database
that backs the crypto-trader Claude Code Skill.

Usage (CLI):
    python3 db_manager.py init-db
    python3 db_manager.py add-account main KEY SECRET --risk 0.01 --testnet
    python3 db_manager.py list-accounts

Usage (import):
    from db_manager import DatabaseManager
    db = DatabaseManager()
    db.init_db()
"""

import argparse
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB_PATH = os.path.expanduser("~/projects/trading-data/trading.db")

# ---------------------------------------------------------------------------
# Thread-safe connection management
# ---------------------------------------------------------------------------
_local = threading.local()


def _get_connection(db_path: str) -> sqlite3.Connection:
    """Return a thread-local SQLite connection with WAL mode and Row factory."""
    conn = getattr(_local, "conn", None)
    stored_path = getattr(_local, "db_path", None)

    if conn is None or stored_path != db_path:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
        _local.db_path = db_path

    return conn


# ---------------------------------------------------------------------------
# SQL Schema
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS account_configs (
    account_id          TEXT PRIMARY KEY,
    api_key             TEXT NOT NULL,
    api_secret          TEXT NOT NULL,
    default_risk_ratio  REAL DEFAULT 0.01,
    is_testnet          INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS channel_routing (
    channel_id          TEXT PRIMARY KEY,
    target_account_id   TEXT NOT NULL,
    channel_name        TEXT DEFAULT '',
    FOREIGN KEY (target_account_id) REFERENCES account_configs(account_id)
);

CREATE TABLE IF NOT EXISTS symbol_risk_configs (
    symbol              TEXT PRIMARY KEY,
    risk_ratio          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS active_orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          TEXT,
    account_id          TEXT,
    symbol              TEXT,
    side                TEXT,
    entry_price         REAL,
    stop_loss           REAL,
    take_profit         REAL,
    quantity            REAL,
    binance_order_id    TEXT,
    binance_sl_order_id TEXT,
    binance_tp_order_id TEXT,
    status              TEXT DEFAULT 'PENDING',
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS briefings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          TEXT,
    content             TEXT NOT NULL,
    category            TEXT DEFAULT 'analysis',
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS signal_operations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id           TEXT NOT NULL,
    operation_type      TEXT NOT NULL,
    symbol              TEXT,
    side                TEXT,
    detail              TEXT DEFAULT '{}',
    created_at          TEXT DEFAULT (datetime('now')),
    UNIQUE(signal_id, operation_type)
);
"""


# ---------------------------------------------------------------------------
# DatabaseManager class (importable API)
# ---------------------------------------------------------------------------
class DatabaseManager:
    """High-level interface to the trading database."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH

    @property
    def conn(self) -> sqlite3.Connection:
        return _get_connection(self.db_path)

    # -- helpers -------------------------------------------------------------
    def _ensure_dir(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return dict(row)

    @staticmethod
    def _rows_to_list(rows: list[sqlite3.Row]) -> list[dict]:
        return [dict(r) for r in rows]

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # -- init ----------------------------------------------------------------
    def init_db(self) -> dict:
        """Create all tables. Returns status dict."""
        self._ensure_dir()
        self.conn.executescript(SCHEMA_SQL)
        return {"status": "ok", "message": "Database initialized", "path": self.db_path}

    # -- account_configs -----------------------------------------------------
    def add_account(
        self,
        account_id: str,
        api_key: str,
        api_secret: str,
        risk_ratio: float = 0.01,
        is_testnet: bool = True,
    ) -> dict:
        try:
            self.conn.execute(
                "INSERT INTO account_configs (account_id, api_key, api_secret, default_risk_ratio, is_testnet) "
                "VALUES (?, ?, ?, ?, ?)",
                (account_id, api_key, api_secret, risk_ratio, int(is_testnet)),
            )
            self.conn.commit()
            return {"status": "ok", "account_id": account_id}
        except sqlite3.IntegrityError:
            return {"status": "error", "message": f"Account '{account_id}' already exists"}

    def list_accounts(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM account_configs").fetchall()
        return self._rows_to_list(rows)

    # -- channel_routing -----------------------------------------------------
    def set_channel(self, channel_id: str, target_account_id: str, channel_name: str = "") -> dict:
        self.conn.execute(
            "INSERT OR REPLACE INTO channel_routing (channel_id, target_account_id, channel_name) "
            "VALUES (?, ?, ?)",
            (channel_id, target_account_id, channel_name),
        )
        self.conn.commit()
        return {"status": "ok", "channel_id": channel_id, "target_account_id": target_account_id}

    def list_channels(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM channel_routing").fetchall()
        return self._rows_to_list(rows)

    # -- symbol_risk_configs -------------------------------------------------
    def set_risk(self, symbol: str, risk_ratio: float) -> dict:
        self.conn.execute(
            "INSERT OR REPLACE INTO symbol_risk_configs (symbol, risk_ratio) VALUES (?, ?)",
            (symbol, risk_ratio),
        )
        self.conn.commit()
        return {"status": "ok", "symbol": symbol, "risk_ratio": risk_ratio}

    def list_risks(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM symbol_risk_configs").fetchall()
        return self._rows_to_list(rows)

    def get_risk(self, symbol: str, account_id: str | None = None) -> dict:
        row = self.conn.execute(
            "SELECT * FROM symbol_risk_configs WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row:
            return {"symbol": symbol, "risk_ratio": row["risk_ratio"], "source": "symbol_config"}

        # Fallback: try account default
        if account_id:
            acct = self.conn.execute(
                "SELECT default_risk_ratio FROM account_configs WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if acct:
                return {
                    "symbol": symbol,
                    "risk_ratio": acct["default_risk_ratio"],
                    "source": "account_default",
                    "account_id": account_id,
                }

        # Fallback: global default
        return {"symbol": symbol, "risk_ratio": 0.01, "source": "global_default"}

    # -- active_orders -------------------------------------------------------
    def create_order(
        self,
        channel_id: str,
        account_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        quantity: float | None = None,
    ) -> dict:
        now = self._now()
        cur = self.conn.execute(
            "INSERT INTO active_orders "
            "(channel_id, account_id, symbol, side, entry_price, stop_loss, take_profit, quantity, "
            "status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)",
            (channel_id, account_id, symbol, side, entry_price, stop_loss, take_profit, quantity, now, now),
        )
        self.conn.commit()
        return {"status": "ok", "order_id": cur.lastrowid}

    def update_order(self, order_id: int, status: str, binance_order_id: str | None = None) -> dict:
        now = self._now()
        valid = ("PENDING", "OPEN", "PARTIAL_CLOSED", "CLOSED", "CANCELLED")
        if status not in valid:
            return {"status": "error", "message": f"Invalid status. Must be one of {valid}"}

        parts = ["status = ?", "updated_at = ?"]
        params: list = [status, now]
        if binance_order_id is not None:
            parts.append("binance_order_id = ?")
            params.append(binance_order_id)
        params.append(order_id)

        cur = self.conn.execute(
            f"UPDATE active_orders SET {', '.join(parts)} WHERE id = ?", params
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return {"status": "error", "message": f"Order {order_id} not found"}
        return {"status": "ok", "order_id": order_id, "new_status": status}

    def update_sl(self, order_id: int, new_sl: float, sl_order_id: str | None = None) -> dict:
        now = self._now()
        parts = ["stop_loss = ?", "updated_at = ?"]
        params: list = [new_sl, now]
        if sl_order_id is not None:
            parts.append("binance_sl_order_id = ?")
            params.append(sl_order_id)
        params.append(order_id)

        cur = self.conn.execute(
            f"UPDATE active_orders SET {', '.join(parts)} WHERE id = ?", params
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return {"status": "error", "message": f"Order {order_id} not found"}
        return {"status": "ok", "order_id": order_id, "stop_loss": new_sl}

    def list_orders(self, status: str | None = None, channel_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM active_orders WHERE 1=1"
        params: list = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if channel_id:
            query += " AND channel_id = ?"
            params.append(channel_id)
        query += " ORDER BY created_at DESC"
        rows = self.conn.execute(query, params).fetchall()
        return self._rows_to_list(rows)

    # -- signal_operations ---------------------------------------------------
    def check_signal(self, signal_id: str, operation_type: str) -> dict:
        """Check if a signal+operation combination already exists."""
        row = self.conn.execute(
            "SELECT * FROM signal_operations WHERE signal_id = ? AND operation_type = ?",
            (signal_id, operation_type),
        ).fetchone()
        if row:
            return {"exists": True, "signal_id": signal_id, "operation_type": operation_type, **self._row_to_dict(row)}
        return {"exists": False, "signal_id": signal_id, "operation_type": operation_type}

    def record_signal(
        self,
        signal_id: str,
        operation_type: str,
        symbol: str = "",
        side: str = "",
        detail: str = "{}",
    ) -> dict:
        """Record a signal operation. Returns error if duplicate."""
        now = self._now()
        try:
            cur = self.conn.execute(
                "INSERT INTO signal_operations (signal_id, operation_type, symbol, side, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (signal_id, operation_type, symbol, side, detail, now),
            )
            self.conn.commit()
            return {"status": "ok", "id": cur.lastrowid, "signal_id": signal_id, "operation_type": operation_type}
        except sqlite3.IntegrityError:
            return {"status": "duplicate", "signal_id": signal_id, "operation_type": operation_type,
                    "message": f"Operation '{operation_type}' already recorded for signal '{signal_id}'"}

    def list_signal_ops(self, signal_id: str | None = None) -> list[dict]:
        """List signal operations, optionally filtered by signal_id."""
        if signal_id:
            rows = self.conn.execute(
                "SELECT * FROM signal_operations WHERE signal_id = ? ORDER BY created_at DESC",
                (signal_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM signal_operations ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        return self._rows_to_list(rows)

    # -- briefings -----------------------------------------------------------
    def add_briefing(self, channel_id: str, content: str, category: str = "analysis") -> dict:
        now = self._now()
        cur = self.conn.execute(
            "INSERT INTO briefings (channel_id, content, category, created_at) VALUES (?, ?, ?, ?)",
            (channel_id, content, category, now),
        )
        self.conn.commit()
        return {"status": "ok", "briefing_id": cur.lastrowid}

    def list_briefings(self, hours: int = 24) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        rows = self.conn.execute(
            "SELECT * FROM briefings WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff,),
        ).fetchall()
        return self._rows_to_list(rows)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------
def _json_out(data) -> None:
    """Print data as compact JSON to stdout."""
    print(json.dumps(data, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the crypto-trader SQLite database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")
    sub = parser.add_subparsers(dest="command")

    # init-db
    sub.add_parser("init-db", help="Initialize database tables")

    # add-account
    p = sub.add_parser("add-account", help="Add a Binance account")
    p.add_argument("account_id")
    p.add_argument("api_key")
    p.add_argument("api_secret")
    p.add_argument("--risk", type=float, default=0.01, help="Default risk ratio (default: 0.01)")
    p.add_argument("--testnet", action="store_true", default=False, help="Use testnet")

    # list-accounts
    sub.add_parser("list-accounts", help="List all accounts")

    # set-channel
    p = sub.add_parser("set-channel", help="Set channel routing")
    p.add_argument("channel_id")
    p.add_argument("account_id")
    p.add_argument("--name", default="", help="Channel display name")

    # list-channels
    sub.add_parser("list-channels", help="List all channel routings")

    # set-risk
    p = sub.add_parser("set-risk", help="Set symbol risk ratio")
    p.add_argument("symbol")
    p.add_argument("ratio", type=float)

    # list-risks
    sub.add_parser("list-risks", help="List all symbol risk configs")

    # get-risk
    p = sub.add_parser("get-risk", help="Get risk ratio for a symbol")
    p.add_argument("symbol")
    p.add_argument("--account", default=None, help="Fallback account for default ratio")

    # create-order
    p = sub.add_parser("create-order", help="Create an active order")
    p.add_argument("channel_id")
    p.add_argument("account_id")
    p.add_argument("symbol")
    p.add_argument("side")
    p.add_argument("entry_price", type=float)
    p.add_argument("--sl", type=float, default=None, help="Stop loss price")
    p.add_argument("--tp", type=float, default=None, help="Take profit price")
    p.add_argument("--qty", type=float, default=None, help="Quantity")

    # update-order
    p = sub.add_parser("update-order", help="Update order status")
    p.add_argument("order_id", type=int)
    p.add_argument("status")
    p.add_argument("--binance-id", default=None, help="Binance order ID")

    # update-sl
    p = sub.add_parser("update-sl", help="Update order stop loss")
    p.add_argument("order_id", type=int)
    p.add_argument("new_sl", type=float)
    p.add_argument("--sl-order-id", default=None, help="Binance SL order ID")

    # list-orders
    p = sub.add_parser("list-orders", help="List active orders")
    p.add_argument("--status", default=None, help="Filter by status")
    p.add_argument("--channel", default=None, help="Filter by channel_id")

    # add-briefing
    p = sub.add_parser("add-briefing", help="Add a briefing entry")
    p.add_argument("channel_id")
    p.add_argument("content")
    p.add_argument("--category", default="analysis", help="Briefing category")

    # list-briefings
    p = sub.add_parser("list-briefings", help="List recent briefings")
    p.add_argument("--hours", type=int, default=24, help="Look back N hours (default: 24)")

    # check-signal
    p = sub.add_parser("check-signal", help="Check if signal operation already exists")
    p.add_argument("signal_id", help="Signal ID (e.g. -1002198013097:3927)")
    p.add_argument("operation_type", help="Operation type: entry, move_sl, partial_close, full_close, add_tp, briefing")

    # record-signal
    p = sub.add_parser("record-signal", help="Record a signal operation (idempotent)")
    p.add_argument("signal_id", help="Signal ID (e.g. -1002198013097:3927)")
    p.add_argument("operation_type", help="Operation type: entry, move_sl, partial_close, full_close, add_tp, briefing")
    p.add_argument("--symbol", default="", help="Trading symbol")
    p.add_argument("--side", default="", help="Trade side (BUY/SELL)")
    p.add_argument("--detail", default="{}", help="JSON detail string")

    # list-signal-ops
    p = sub.add_parser("list-signal-ops", help="List signal operations")
    p.add_argument("--signal-id", default=None, help="Filter by signal ID")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    db = DatabaseManager(db_path=args.db)

    match args.command:
        case "init-db":
            _json_out(db.init_db())

        case "add-account":
            _json_out(db.add_account(args.account_id, args.api_key, args.api_secret, args.risk, args.testnet))

        case "list-accounts":
            _json_out(db.list_accounts())

        case "set-channel":
            _json_out(db.set_channel(args.channel_id, args.account_id, args.name))

        case "list-channels":
            _json_out(db.list_channels())

        case "set-risk":
            _json_out(db.set_risk(args.symbol, args.ratio))

        case "list-risks":
            _json_out(db.list_risks())

        case "get-risk":
            _json_out(db.get_risk(args.symbol, args.account))

        case "create-order":
            _json_out(db.create_order(args.channel_id, args.account_id, args.symbol, args.side, args.entry_price, args.sl, args.tp, args.qty))

        case "update-order":
            _json_out(db.update_order(args.order_id, args.status, args.binance_id))

        case "update-sl":
            _json_out(db.update_sl(args.order_id, args.new_sl, args.sl_order_id))

        case "list-orders":
            _json_out(db.list_orders(args.status, args.channel))

        case "add-briefing":
            _json_out(db.add_briefing(args.channel_id, args.content, args.category))

        case "list-briefings":
            _json_out(db.list_briefings(args.hours))

        case "check-signal":
            _json_out(db.check_signal(args.signal_id, args.operation_type))

        case "record-signal":
            _json_out(db.record_signal(args.signal_id, args.operation_type, args.symbol, args.side, args.detail))

        case "list-signal-ops":
            _json_out(db.list_signal_ops(args.signal_id))

        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
