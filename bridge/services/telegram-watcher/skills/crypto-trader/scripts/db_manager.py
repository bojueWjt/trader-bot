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
import math
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB_PATH = os.path.expanduser("~/projects/trading-data/trading.db")
CREDENTIAL_ACCOUNT_ID_PATTERN = re.compile(
    r"^[^\x00-\x1f\x7f]{1,128}$"
)
EXECUTION_ACCOUNT_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
)
ACCOUNT_TYPES = {"main", "subaccount"}

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
    is_testnet          INTEGER DEFAULT 1,
    account_type        TEXT NOT NULL DEFAULT 'main',
    parent_account_id   TEXT NOT NULL DEFAULT '',
    execution_account_id TEXT NOT NULL DEFAULT '',
    risk_capital_multiplier REAL NOT NULL,
    is_enabled          INTEGER NOT NULL DEFAULT 1,
    CHECK (account_type IN ('main', 'subaccount')),
    CHECK (
      risk_capital_multiplier IS NULL
      OR risk_capital_multiplier > 0
    ),
    CHECK (is_enabled IN (0, 1))
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
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def _ensure_schema(self) -> None:
        self._ensure_dir()
        self.conn.executescript(SCHEMA_SQL)
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(account_configs)").fetchall()
        }
        multiplier_column_was_missing = (
            "risk_capital_multiplier" not in columns
        )
        if "account_type" not in columns:
            self.conn.execute(
                "ALTER TABLE account_configs "
                "ADD COLUMN account_type TEXT NOT NULL DEFAULT 'main'"
            )
        if "parent_account_id" not in columns:
            self.conn.execute(
                "ALTER TABLE account_configs "
                "ADD COLUMN parent_account_id TEXT NOT NULL DEFAULT ''"
            )
        if multiplier_column_was_missing:
            self.conn.execute(
                "ALTER TABLE account_configs "
                "ADD COLUMN risk_capital_multiplier REAL"
            )
        if "execution_account_id" not in columns:
            self.conn.execute(
                "ALTER TABLE account_configs "
                "ADD COLUMN execution_account_id TEXT NOT NULL DEFAULT ''"
            )
        if "is_enabled" not in columns:
            self.conn.execute(
                "ALTER TABLE account_configs "
                "ADD COLUMN is_enabled INTEGER NOT NULL DEFAULT 1"
            )
        self.conn.executescript(
            """
            UPDATE account_configs
            SET account_type = 'main'
            WHERE account_type IS NULL
               OR account_type = ''
               OR account_type NOT IN ('main', 'subaccount');

            UPDATE account_configs
            SET parent_account_id = ''
            WHERE account_type = 'main'
               OR parent_account_id IS NULL;

            UPDATE account_configs
            SET execution_account_id = account_id
            WHERE execution_account_id IS NULL
               OR execution_account_id = '';

            CREATE INDEX IF NOT EXISTS idx_account_configs_parent
            ON account_configs (parent_account_id, account_type);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_account_configs_execution_account
            ON account_configs (execution_account_id);
            """
        )
        if multiplier_column_was_missing:
            self.conn.execute(
                "UPDATE account_configs SET is_enabled = 0"
            )
        self._disable_accounts_with_invalid_multiplier()
        channel_columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(channel_routing)"
            ).fetchall()
        }
        if "channel_name" not in channel_columns:
            self.conn.execute(
                "ALTER TABLE channel_routing "
                "ADD COLUMN channel_name TEXT DEFAULT ''"
            )
        self.conn.commit()

    def _disable_accounts_with_invalid_multiplier(self) -> None:
        rows = self.conn.execute(
            "SELECT account_id, risk_capital_multiplier "
            "FROM account_configs"
        ).fetchall()
        for row in rows:
            if self._valid_risk_capital_multiplier(
                row["risk_capital_multiplier"]
            ):
                continue
            self.conn.execute(
                "UPDATE account_configs SET is_enabled = 0 "
                "WHERE account_id = ?",
                (row["account_id"],),
            )

    @staticmethod
    def _valid_risk_capital_multiplier(value: object) -> bool:
        if value is None or isinstance(value, bool):
            return False
        try:
            multiplier = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(multiplier) and multiplier > 0

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
        self._ensure_schema()
        return {"status": "ok", "message": "Database initialized", "path": self.db_path}

    # -- account_configs -----------------------------------------------------
    def add_account(
        self,
        account_id: str,
        api_key: str,
        api_secret: str,
        risk_ratio: float = 0.01,
        is_testnet: bool = True,
        account_type: str = "main",
        parent_account_id: str = "",
        risk_capital_multiplier: float | None = None,
        execution_account_id: str = "",
    ) -> dict:
        self._ensure_schema()
        normalized_account_id = str(account_id).strip()
        normalized_account_type = str(account_type or "main").strip().lower()
        normalized_parent_account_id = str(parent_account_id or "").strip()
        normalized_execution_account_id = str(
            execution_account_id or normalized_account_id
        ).strip()

        if not CREDENTIAL_ACCOUNT_ID_PATTERN.fullmatch(
            normalized_account_id
        ):
            return {
                "status": "error",
                "message": (
                    "account_id must be 1-128 printable characters"
                ),
            }
        if normalized_account_type not in ACCOUNT_TYPES:
            return {
                "status": "error",
                "message": "account_type must be main or subaccount",
            }
        if not EXECUTION_ACCOUNT_ID_PATTERN.fullmatch(
            normalized_execution_account_id
        ):
            return {
                "status": "error",
                "message": (
                    "execution_account_id must use letters, numbers, dots, "
                    "underscores, or hyphens"
                ),
            }
        if not math.isfinite(risk_ratio) or risk_ratio < 0:
            return {
                "status": "error",
                "message": "risk_ratio must be a non-negative number",
            }
        if not self._valid_risk_capital_multiplier(
            risk_capital_multiplier
        ):
            return {
                "status": "error",
                "message": (
                    "risk_capital_multiplier is required and must be "
                    "greater than 0"
                ),
            }

        hierarchy_error = self._validate_account_hierarchy(
            account_id=normalized_account_id,
            account_type=normalized_account_type,
            parent_account_id=normalized_parent_account_id,
            is_testnet=is_testnet,
        )
        if hierarchy_error:
            return {"status": "error", "message": hierarchy_error}

        existing_execution_account = self.conn.execute(
            "SELECT account_id FROM account_configs "
            "WHERE execution_account_id = ?",
            (normalized_execution_account_id,),
        ).fetchone()
        if existing_execution_account:
            return {
                "status": "error",
                "message": "V3 execution account already exists",
            }

        try:
            self.conn.execute(
                "INSERT INTO account_configs "
                "(account_id, api_key, api_secret, default_risk_ratio, is_testnet, "
                "account_type, parent_account_id, risk_capital_multiplier, "
                "execution_account_id, is_enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    normalized_account_id,
                    api_key,
                    api_secret,
                    risk_ratio,
                    int(is_testnet),
                    normalized_account_type,
                    normalized_parent_account_id,
                    risk_capital_multiplier,
                    normalized_execution_account_id,
                ),
            )
            self.conn.commit()
            return {"status": "ok", "account_id": normalized_account_id}
        except sqlite3.IntegrityError:
            return {
                "status": "error",
                "message": f"Account '{normalized_account_id}' already exists",
            }

    def list_accounts(self) -> list[dict]:
        self._ensure_schema()
        rows = self.conn.execute(
            """
            SELECT *
            FROM account_configs
            ORDER BY
              CASE
                WHEN account_type = 'main' THEN account_id
                ELSE parent_account_id
              END,
              CASE account_type WHEN 'main' THEN 0 ELSE 1 END,
              account_id
            """
        ).fetchall()
        return self._rows_to_list(rows)

    def _validate_account_hierarchy(
        self,
        account_id: str,
        account_type: str,
        parent_account_id: str,
        is_testnet: bool,
    ) -> str | bool:
        if account_type == "main":
            if parent_account_id:
                return "Main account cannot have parent_account_id"
            return False

        if not parent_account_id:
            return "Subaccount requires parent_account_id"
        if parent_account_id == account_id:
            return "Subaccount cannot reference itself"

        parent = self.conn.execute(
            "SELECT account_type, is_testnet, is_enabled "
            "FROM account_configs WHERE account_id = ?",
            (parent_account_id,),
        ).fetchone()
        if not parent:
            return "Parent account not found"
        if parent["account_type"] != "main":
            return "Parent account must be a main account"
        if int(parent["is_enabled"]) != 1:
            return "Parent account is disabled"
        if int(parent["is_testnet"]) != int(is_testnet):
            return "Subaccount environment must match its main account"
        return False

    # -- channel_routing -----------------------------------------------------
    def set_channel(self, channel_id: str, target_account_id: str, channel_name: str = "") -> dict:
        self._ensure_schema()
        target = self.conn.execute(
            "SELECT account_id, is_enabled FROM account_configs "
            "WHERE account_id = ?",
            (target_account_id,),
        ).fetchone()
        if not target:
            return {"status": "error", "message": "Target account not found"}
        if int(target["is_enabled"]) != 1:
            return {"status": "error", "message": "Target account is disabled"}

        self.conn.execute(
            "INSERT INTO channel_routing (channel_id, target_account_id, channel_name) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET "
            "target_account_id = excluded.target_account_id, "
            "channel_name = excluded.channel_name",
            (channel_id, target_account_id, channel_name),
        )
        self.conn.commit()
        return {"status": "ok", "channel_id": channel_id, "target_account_id": target_account_id}

    def list_channels(self) -> list[dict]:
        self._ensure_schema()
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
    p.add_argument(
        "--type",
        choices=("main", "subaccount"),
        default="main",
        help="Execution account type",
    )
    p.add_argument(
        "--parent-account",
        default="",
        help="Required main account ID when --type subaccount",
    )
    p.add_argument(
        "--capital-multiplier",
        type=float,
        required=True,
        help="Risk capital multiplier applied to the account balance",
    )
    p.add_argument(
        "--execution-account",
        default="",
        help="Canonical V3 execution account ID (defaults to account_id)",
    )

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
            _json_out(
                db.add_account(
                    args.account_id,
                    args.api_key,
                    args.api_secret,
                    args.risk,
                    args.testnet,
                    args.type,
                    args.parent_account,
                    args.capital_multiplier,
                    args.execution_account,
                )
            )

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
