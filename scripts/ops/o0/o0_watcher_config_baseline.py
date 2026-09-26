#!/usr/bin/env python3
"""O-0 data baseline for the watcher configuration tables (read-only on a COPY).

DRAFT for docs/agent-team/release/o0-runbook-snapshot-switch.md step 2 and the
rollback "verified consistent snapshot of the real DB".

What it does, on a SQLite file that is already a consistent copy (never on the
live volume file):

* reads ONLY non-secret columns (explicit column lists; never ``SELECT *``);
* re-implements the watcher ``config-snapshot`` validation of contract
  WGW-1.0.1 §9.10 (mirrors auto/wac-011 lib/config-store.js makeSnapshot) and
  lists every violation ``{rule, table, key}``;
* lists the extra O-0 baseline items required before the snapshot switch
  (§9.11 / E-14, reviews wac-001 🟡-9 💭-2 💭-3, wac-011-r2 #6, wac-013 💭-3):
  legacy-lenient values, legacy ``enabled``/``status`` columns, NULL or
  out-of-range ``default_risk_ratio``, ids the gateway pattern cannot address,
  ids with surrounding whitespace (C-0 ``_safe_id``), CHECK violations;
* when there are no violations, computes the §9.10 ``content_sha256`` exactly
  as the watcher does (JS ``String(Number(x))`` number formatting), so the copy
  can be compared with the live snapshot. With violations the digest is
  reported as UNCOMPARABLE, never as a value;
* prints per-table row counts and per-table digests of the non-secret export,
  which is what A/B/C comparisons and the replica check use.

Exit status: 0 = snapshot would be valid and no blocking baseline item;
1 = violations or blocking items (listed); 2 = unreadable input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

SCHEMA_VERSION = "watcher-config-snapshot.v1"
ACCOUNT_COLUMNS = (
    "account_id", "account_type", "parent_account_id", "execution_account_id",
    "is_enabled", "risk_capital_addon", "default_risk_ratio",
)
CHANNEL_COLUMNS = ("channel_id", "target_account_id")
RISK_COLUMNS = ("symbol", "risk_ratio")
LEGACY_OPTIONAL = ("enabled", "status", "risk_capital_multiplier")
GATEWAY_PATTERNS = {  # contracts/watcher-gateway-routes.yaml path_params.*.gateway_pattern
    "account_id": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$"),
    "channel_id": re.compile(r"^-?[0-9]{1,24}$"),
    "symbol": re.compile(r"^[A-Z0-9]{2,32}$"),
}
LEGACY_TRUE = {"1", "active", "enabled", "true"}
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class BaselineError(Exception):
    pass


def js_number_string(value: float | int) -> str:
    """ECMAScript Number::toString(10) for finite doubles."""
    x = float(value)
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    dec = Decimal(repr(abs(x)))  # repr is the shortest round-trip form, same digits as JS
    digits_tuple, exp = dec.as_tuple().digits, dec.as_tuple().exponent
    digits = "".join(map(str, digits_tuple)).lstrip("0")
    trailing = len(digits) - len(digits.rstrip("0"))
    digits = digits.rstrip("0")
    exp += trailing
    k = len(digits)
    n = k + exp
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    exp_text = ("+" if e >= 0 else "-") + str(abs(e))
    if k == 1:
        return sign + digits + "e" + exp_text
    return sign + digits[0] + "." + digits[1:] + "e" + exp_text


def valid_string(value: object) -> bool:
    return isinstance(value, str) and len(value) > 0 and CONTROL.search(value) is None


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def open_copy(path: Path) -> sqlite3.Connection:
    """Open a consistent COPY read-only.

    Never ``immutable=1``: that flag makes SQLite ignore the ``-wal`` file, so a
    database whose last writer did not checkpoint (e.g. a container killed with
    SIGKILL) would be read as it was BEFORE those writes (review wac-032 🔴-4).
    A non-empty ``-wal`` next to the file means it is not a finished copy: refuse
    and ask for ``o0_tool.py sqlite-backup`` (the backup API reads the WAL frames
    and the copy is switched to a rollback journal).
    """
    if not path.is_file():
        raise BaselineError(f"not a file: {path}")
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise BaselineError(
            f"wal_not_checkpointed: {wal.name} holds {wal.stat().st_size} bytes of un-checkpointed writes; "
            "make a copy first (o0_tool.py sqlite-backup --source <db> --dest <copy>) and analyse the copy"
        )
    uri = "file:" + str(path.resolve()) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return (row["sql"] or "") if row else ""


def canonical_digest(obj: object) -> str:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def analyse(conn: sqlite3.Connection) -> dict:
    present = {}
    for table, required in (("account_configs", ACCOUNT_COLUMNS), ("channel_routing", CHANNEL_COLUMNS), ("symbol_risk_configs", RISK_COLUMNS)):
        cols = table_columns(conn, table)
        if not cols:
            raise BaselineError(f"table missing or unreadable: {table} (snapshot_unreadable)")
        present[table] = cols
    violations: list[dict] = []
    baseline: list[dict] = []

    def add(rule: str, table: str, key: object) -> None:
        violations.append({"rule": rule, "table": table, "key": key if valid_string(key) else ""})

    def note(item: str, table: str, key: object, blocking: bool, detail: str = "") -> None:
        baseline.append({"item": item, "table": table, "key": key if valid_string(key) else repr(key)[:40], "blocking": blocking, "detail": detail})

    for table, required in (("account_configs", ACCOUNT_COLUMNS), ("channel_routing", CHANNEL_COLUMNS), ("symbol_risk_configs", RISK_COLUMNS)):
        for name in required:
            if name not in present[table]:
                add("missing_field", table, name)
    if violations:
        return {"violations": violations, "baseline": baseline, "content_sha256": None, "counts": {}, "table_digests": {}, "revision": None}

    extra_cols = [c for c in LEGACY_OPTIONAL if c in present["account_configs"]]
    select_cols = list(ACCOUNT_COLUMNS) + extra_cols
    conn.execute("BEGIN")
    accounts = [dict(r) for r in conn.execute(
        "SELECT " + ", ".join(select_cols) + " FROM account_configs ORDER BY account_id COLLATE BINARY")]
    channels = [dict(r) for r in conn.execute(
        "SELECT channel_id, target_account_id FROM channel_routing ORDER BY channel_id COLLATE BINARY")]
    risks = [dict(r) for r in conn.execute(
        "SELECT symbol, risk_ratio FROM symbol_risk_configs ORDER BY symbol COLLATE BINARY")]
    revision = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='config_revision'").fetchone():
        row = conn.execute("SELECT revision FROM config_revision WHERE id = 1").fetchone()
        revision = row["revision"] if row else None
    conn.execute("COMMIT")

    execution_ids: set = set()
    ids: set = set()
    for row in accounts:
        key = row["account_id"]
        parent = row["parent_account_id"]
        if not valid_string(key) or not valid_string(row["execution_account_id"]) or (parent not in ("", None) and not valid_string(parent)):
            add("invalid_string", "account_configs", key)
        if row["account_type"] not in ("main", "subaccount"):
            add("invalid_enum", "account_configs", key)
        if not (is_number(row["is_enabled"]) and row["is_enabled"] in (0, 1)):
            add("invalid_enum", "account_configs", key)
        if any(row[c] is None for c in ("account_id", "account_type", "parent_account_id", "execution_account_id", "is_enabled", "risk_capital_addon")):
            add("missing_field", "account_configs", key)
        if row["execution_account_id"] in execution_ids:
            add("duplicate_execution_account", "account_configs", key)
        ids.add(key)
        execution_ids.add(row["execution_account_id"])
        addon = row["risk_capital_addon"]
        if not is_number(addon) or not math.isfinite(addon) or addon < 0:
            add("invalid_number", "account_configs", key)
        drr = row["default_risk_ratio"]
        if drr is not None and (not is_number(drr) or not math.isfinite(drr) or drr <= 0 or drr > 0.1):
            add("invalid_number", "account_configs", key)
    by_id = {r["account_id"]: r for r in accounts}
    for row in accounts:
        parent = by_id.get(row["parent_account_id"])
        if row["account_type"] == "main" and row["parent_account_id"]:
            add("invalid_parent", "account_configs", row["account_id"])
        if row["account_type"] == "subaccount" and (parent is None or parent["account_type"] != "main" or row["parent_account_id"] == row["account_id"]):
            add("invalid_parent", "account_configs", row["account_id"])
    for row in channels:
        if not valid_string(row["channel_id"]) or (row["target_account_id"] is not None and not valid_string(row["target_account_id"])):
            add("invalid_string", "channel_routing", row["channel_id"])
        if row["channel_id"] is None or row["target_account_id"] is None:
            add("missing_field", "channel_routing", row["channel_id"])
        elif row["target_account_id"] not in ids:
            add("dangling_route", "channel_routing", row["channel_id"])
    for row in risks:
        if not valid_string(row["symbol"]):
            add("invalid_string", "symbol_risk_configs", row["symbol"])
        rr = row["risk_ratio"]
        if row["symbol"] is None or rr is None:
            add("missing_field", "symbol_risk_configs", row["symbol"])
        elif not is_number(rr) or not math.isfinite(rr) or rr <= 0 or rr > 0.1:
            add("invalid_number", "symbol_risk_configs", row["symbol"])

    # ---- O-0 baseline items beyond the watcher's own validation
    for row in accounts:
        key = row["account_id"]
        at = row["account_type"]
        if at not in ("main", "subaccount") and isinstance(at, str) and at.strip().lower() in ("main", "subaccount"):
            note("legacy_lenient_account_type", "account_configs", key, True, "legacy reader accepts lower(trim()); snapshot is invalid")
        en = row["is_enabled"]
        if not (is_number(en) and en in (0, 1)) and str(en or "").strip().lower() in LEGACY_TRUE:
            note("legacy_lenient_is_enabled", "account_configs", key, True, "legacy reader accepts 1/active/enabled/true")
        for legacy in ("enabled", "status"):
            if legacy in extra_cols:
                value = str(row.get(legacy) or "").strip().lower()
                if legacy == "status" and not value:
                    continue
                if value not in LEGACY_TRUE:
                    note(f"legacy_{legacy}_column_disables_row", "account_configs", key, True,
                         f"legacy reader treats row as disabled via '{legacy}' column; snapshot ignores it")
        drr = row["default_risk_ratio"]
        if drr is None:
            note("default_risk_ratio_null", "account_configs", key, False, "E-14: executing account with NULL default_risk -> 503 on entries that fall back to it (both readers)")
        if isinstance(key, str) and not GATEWAY_PATTERNS["account_id"].match(key):
            note("account_id_not_gateway_addressable", "account_configs", key, False, "app cannot edit this account (wac-001 💭-3)")
        for col in ("account_id", "parent_account_id", "execution_account_id"):
            val = row[col]
            if isinstance(val, str) and val and val != val.strip():
                note("surrounding_whitespace_id", "account_configs", key, True, f"{col}: C-0 _safe_id rejects the whole snapshot (wac-013 💭-3)")
        mult = row.get("risk_capital_multiplier") if "risk_capital_multiplier" in extra_cols else None
        check_bad = (
            at not in ("main", "subaccount")
            or (mult is not None and is_number(mult) and mult <= 0)
            or (is_number(row["risk_capital_addon"]) and row["risk_capital_addon"] < 0)
            or not (is_number(en) and en in (0, 1))
        )
        if check_bad:
            note("check_constraint_violation", "account_configs", key, True, "row violates the current CHECKs; unrelated edits get 400 (wac-011-r2 #6)")
    for row in channels:
        cid = row["channel_id"]
        if isinstance(cid, str) and cid != cid.strip():
            note("surrounding_whitespace_id", "channel_routing", cid, True, "C-0 _safe_id rejects the whole snapshot (wac-013 💭-3)")
        if isinstance(cid, str) and not GATEWAY_PATTERNS["channel_id"].match(cid):
            note("channel_id_not_gateway_addressable", "channel_routing", cid, False, "app cannot delete this route")
    for row in risks:
        sym = row["symbol"]
        if isinstance(sym, str) and not GATEWAY_PATTERNS["symbol"].match(sym):
            note("symbol_not_gateway_addressable", "symbol_risk_configs", sym, False, "app cannot delete this risk row")
    if "CHECK" not in table_sql(conn, "account_configs").upper():
        note("account_configs_without_check_constraints", "account_configs", "<table>", False, "table predates CHECKs; baseline rows above are authoritative")

    export_accounts = []
    for row in accounts:
        export_accounts.append({
            "account_id": row["account_id"],
            "kind": "main" if row["account_type"] == "main" else "sub",
            "parent_account_id": None if row["account_type"] == "main" else row["parent_account_id"],
            "execution_account_id": row["execution_account_id"],
            "enabled": row["is_enabled"] == 1,
            "risk_capital_addon": js_number_string(row["risk_capital_addon"]) if is_number(row["risk_capital_addon"]) else None,
            "default_risk": None if row["default_risk_ratio"] is None else (js_number_string(row["default_risk_ratio"]) if is_number(row["default_risk_ratio"]) else None),
        })
    export_channels = [{"channel_id": r["channel_id"], "target_account_id": r["target_account_id"]} for r in channels]
    export_risks = [{"symbol": r["symbol"], "risk_ratio": js_number_string(r["risk_ratio"]) if is_number(r["risk_ratio"]) else None} for r in risks]
    content = {"accounts": export_accounts, "channels": export_channels, "risks": export_risks, "schema_version": SCHEMA_VERSION}
    return {
        "violations": violations,
        "baseline": baseline,
        "content_sha256": None if violations else canonical_digest(content),
        "counts": {"accounts": len(accounts), "channels": len(channels), "risks": len(risks)},
        "table_digests": {
            "accounts": canonical_digest(export_accounts),
            "channels": canonical_digest(export_channels),
            "risks": canonical_digest(export_risks),
        },
        "revision": revision,
        "export": content,
    }


def cmd_baseline(args: argparse.Namespace) -> int:
    try:
        conn = open_copy(args.db)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            print("FAIL integrity_check != ok")
            return 2
        result = analyse(conn)
    except (BaselineError, sqlite3.Error) as exc:
        print(f"ERROR {exc}")
        return 2
    print(f"INFO db={args.db.name} integrity=ok revision={result['revision']}")
    for name, count in result["counts"].items():
        print(f"INFO rows.{name}={count} digest.{name}={result['table_digests'][name]}")
    for v in result["violations"]:
        print(f"VIOLATION rule={v['rule']} table={v['table']} key={v['key']!r}")
    for b in result["baseline"]:
        print(f"BASELINE {'BLOCKING' if b['blocking'] else 'note'} item={b['item']} table={b['table']} key={b['key']!r} {b['detail']}")
    if args.export_json:
        fd = os.open(args.export_json, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({k: v for k, v in result.items()}, handle, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"INFO export written (non-secret fields only): {args.export_json}")
    if result["content_sha256"] is None:
        print(f"SNAPSHOT_WOULD_BE_INVALID violations={len(result['violations'])} content_sha256=UNCOMPARABLE")
        return 1
    blocking = [b for b in result["baseline"] if b["blocking"]]
    if args.expect_content_sha256:
        if args.expect_content_sha256 != result["content_sha256"]:
            print(f"FAIL content_sha256 {result['content_sha256']} != expected {args.expect_content_sha256}")
            return 1
        print("PASS content_sha256 equals the expected (live snapshot) value")
    print(f"content_sha256={result['content_sha256']}")
    if blocking:
        print(f"BASELINE_BLOCKING items={len(blocking)}")
        return 1
    print("BASELINE_OK")
    return 0


def _wal_regression(base: Path, repro_db: Path | None) -> str:
    """Review wac-032 🔴-4: writes left in an un-checkpointed WAL must be seen or refused."""
    import subprocess
    db = base / "wal.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE account_configs (account_id TEXT PRIMARY KEY, default_risk_ratio REAL, account_type TEXT NOT NULL,
          parent_account_id TEXT NOT NULL DEFAULT '', execution_account_id TEXT NOT NULL, risk_capital_addon REAL NOT NULL DEFAULT 0,
          is_enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE channel_routing (channel_id TEXT PRIMARY KEY, target_account_id TEXT NOT NULL);
        CREATE TABLE symbol_risk_configs (symbol TEXT PRIMARY KEY, risk_ratio REAL NOT NULL);
        INSERT INTO account_configs VALUES ('main-1', 0.01, 'main', '', 'account-a', 0, 1);
        """
    )
    conn.commit()
    conn.close()
    # a "migration" that dies without checkpointing, like `docker rm -f` on the dry-run container
    code = (
        "import os,sqlite3,sys\n"
        "c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0')\n"
        "c.execute('CREATE TABLE config_revision (id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL)')\n"
        "c.execute('INSERT INTO config_revision VALUES (1, 0)'); c.commit(); os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", code, str(db)], check=True)
    wal = Path(str(db) + "-wal")
    assert wal.exists() and wal.stat().st_size > 0, "fixture did not leave a WAL"
    immutable = sqlite3.connect("file:" + str(db) + "?mode=ro&immutable=1", uri=True)
    hidden = [r[0] for r in immutable.execute("SELECT name FROM sqlite_master WHERE name='config_revision'")]
    immutable.close()
    assert hidden == [], "fixture no longer reproduces the immutable=1 blind spot"
    try:
        open_copy(db)
    except BaselineError as exc:
        assert "wal_not_checkpointed" in str(exc)
    else:
        raise AssertionError("open_copy accepted a database with an un-checkpointed WAL")
    tool = Path(__file__).resolve().parent / "o0_tool.py"
    copy = base / "wal.copy.db"
    subprocess.run([sys.executable, str(tool), "sqlite-backup", "--source", str(db), "--dest", str(copy)],
                   check=True, stdout=subprocess.DEVNULL)
    result = analyse(open_copy(copy))
    assert result["revision"] == 0, ("the copy must carry the migration's config_revision", result["revision"])
    note = "wal_case=ok"
    if repro_db is not None:
        import shutil
        local = base / "repro.db"
        for suffix in ("", "-wal", "-shm"):  # work on a private copy: the repro stays untouched
            if Path(str(repro_db) + suffix).exists():
                shutil.copyfile(str(repro_db) + suffix, str(local) + suffix)
        repro_db = local
        try:
            open_copy(repro_db)
        except BaselineError as exc:
            assert "wal_not_checkpointed" in str(exc)
            note += " repro_refused=ok"
        else:
            raise AssertionError(f"review repro {repro_db} was not refused")
        repro_copy = base / "repro.copy.db"
        subprocess.run([sys.executable, str(tool), "sqlite-backup", "--source", str(repro_db), "--dest", str(repro_copy)],
                       check=True, stdout=subprocess.DEVNULL)
        names = [r[0] for r in open_copy(repro_copy).execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        assert "config_revision" in names, names
        note += " repro_copy_sees_config_revision=ok"
    return note


def cmd_selftest(args: argparse.Namespace) -> int:
    cases = {0.01: "0.01", 0.1: "0.1", 100.0: "100", 1e-7: "1e-7", 1.5e-7: "1.5e-7", 1e21: "1e+21",
             123456789012345680000.0: "123456789012345680000", 0.000001: "0.000001", 2.5: "2.5", 0: "0", 1e22: "1e+22", 5e-324: "5e-324"}
    for value, expected in cases.items():
        got = js_number_string(value)
        assert got == expected, (value, got, expected)
    base = Path(tempfile.mkdtemp(prefix="o0-baseline-selftest-"))
    try:
        good = base / "good.db"
        conn = sqlite3.connect(good)
        conn.executescript(
            """
            CREATE TABLE account_configs (account_id TEXT PRIMARY KEY, api_key TEXT, api_secret TEXT,
              default_risk_ratio REAL DEFAULT 0.01, account_type TEXT NOT NULL DEFAULT 'main',
              parent_account_id TEXT NOT NULL DEFAULT '', execution_account_id TEXT NOT NULL DEFAULT '',
              risk_capital_multiplier REAL NOT NULL DEFAULT 1, risk_capital_addon REAL NOT NULL DEFAULT 0,
              is_enabled INTEGER NOT NULL DEFAULT 1, CHECK (account_type IN ('main','subaccount')));
            CREATE TABLE channel_routing (channel_id TEXT PRIMARY KEY, target_account_id TEXT NOT NULL, channel_name TEXT DEFAULT '');
            CREATE TABLE symbol_risk_configs (symbol TEXT PRIMARY KEY, risk_ratio REAL NOT NULL);
            CREATE TABLE config_revision (id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL);
            INSERT INTO config_revision VALUES (1, 7);
            INSERT INTO account_configs (account_id, api_key, api_secret, default_risk_ratio, account_type, parent_account_id, execution_account_id, risk_capital_addon, is_enabled)
              VALUES ('main-1', 'SENTINEL_KEY_VALUE', 'SENTINEL_SECRET_VALUE', 0.01, 'main', '', 'account-a', 100, 1),
                     ('sub-1', 'SENTINEL_KEY_2', 'SENTINEL_SECRET_2', NULL, 'subaccount', 'main-1', 'account-b', 0, 0);
            INSERT INTO channel_routing VALUES ('-1001', 'main-1', 'name');
            INSERT INTO symbol_risk_configs VALUES ('BTCUSDT', 0.02);
            """
        )
        conn.commit()
        conn.close()
        result = analyse(open_copy(good))
        assert not result["violations"], result["violations"]
        assert result["revision"] == 7
        text = json.dumps(result, ensure_ascii=False)
        assert "SENTINEL" not in text, "secret column leaked into the analysis"
        expected_content = {
            "accounts": [
                {"account_id": "main-1", "kind": "main", "parent_account_id": None, "execution_account_id": "account-a", "enabled": True, "risk_capital_addon": "100", "default_risk": "0.01"},
                {"account_id": "sub-1", "kind": "sub", "parent_account_id": "main-1", "execution_account_id": "account-b", "enabled": False, "risk_capital_addon": "0", "default_risk": None},
            ],
            "channels": [{"channel_id": "-1001", "target_account_id": "main-1"}],
            "risks": [{"symbol": "BTCUSDT", "risk_ratio": "0.02"}],
            "schema_version": SCHEMA_VERSION,
        }
        assert result["content_sha256"] == canonical_digest(expected_content)
        assert [b["item"] for b in result["baseline"]] == ["default_risk_ratio_null"], result["baseline"]
        bad = base / "bad.db"
        conn = sqlite3.connect(bad)
        conn.executescript(
            """
            CREATE TABLE account_configs (account_id TEXT PRIMARY KEY, default_risk_ratio REAL, account_type TEXT,
              parent_account_id TEXT, execution_account_id TEXT, risk_capital_addon REAL, is_enabled INTEGER, status TEXT);
            CREATE TABLE channel_routing (channel_id TEXT PRIMARY KEY, target_account_id TEXT);
            CREATE TABLE symbol_risk_configs (symbol TEXT PRIMARY KEY, risk_ratio REAL);
            INSERT INTO account_configs VALUES ('m1', 0.2, ' Main ', '', 'acc-x', 0, 1, 'active'),
                                               ('m2', 0.01, 'main', '', 'acc-x', -1, 1, 'disabled'),
                                               ('s1 ', 0.01, 'subaccount', 'nope', 'acc-y', 0, 2, NULL);
            INSERT INTO channel_routing VALUES (' 100', 'ghost');
            INSERT INTO symbol_risk_configs VALUES ('btc', 0.5);
            """
        )
        conn.commit()
        conn.close()
        result = analyse(open_copy(bad))
        rules = sorted({v["rule"] for v in result["violations"]})
        assert rules == ["dangling_route", "duplicate_execution_account", "invalid_enum", "invalid_number", "invalid_parent"], rules
        items = sorted({b["item"] for b in result["baseline"]})
        for needed in ("legacy_lenient_account_type", "legacy_status_column_disables_row", "surrounding_whitespace_id",
                       "check_constraint_violation", "symbol_not_gateway_addressable", "account_configs_without_check_constraints",
                       "account_id_not_gateway_addressable"):
            assert needed in items, (needed, items)
        assert result["content_sha256"] is None
        wal_note = _wal_regression(base, args.repro_db)
        print(f"SELFTEST_OK number_cases={len(cases)} good_digest={result and 'checked'} bad_rules={len(rules)} bad_items={len(items)} {wal_note}")
        return 0
    finally:
        for p in sorted(base.iterdir()):
            p.unlink()
        base.rmdir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("baseline", help="analyse a consistent COPY of watcher-trading.db")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--export-json", type=Path, help="write the non-secret export (0600, must not exist)")
    p.add_argument("--expect-content-sha256", help="compare with the live snapshot content_sha256")
    p.set_defaults(func=cmd_baseline)
    p = sub.add_parser("selftest")
    p.add_argument("--repro-db", type=Path, help="optional: review repro DB with an un-checkpointed WAL (scratchpad/walt/w.db); it is copied, never modified")
    p.set_defaults(func=cmd_selftest)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
