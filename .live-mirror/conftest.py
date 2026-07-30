from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parent.parent
API_PATH = REPO_ROOT / ".live-mirror" / "api" / "read_api.py"
TRADE_PATH = (
    REPO_ROOT
    / "hermes-profile"
    / "skills"
    / "trading"
    / "v3-trader"
    / "scripts"
    / "v3_trade.py"
)
QUERY_PATH = (
    REPO_ROOT
    / "hermes-profile"
    / "skills"
    / "trading"
    / "v3-trader"
    / "scripts"
    / "v3_query.py"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


snapshot = types.ModuleType("snapshot")
snapshot.DEFAULT_STALENESS_MS = 60_000
snapshot._missing_nodes = lambda *args, **kwargs: []
snapshot._worst_reconciliation_state = lambda *args, **kwargs: "ok"
snapshot.build_system_snapshot = lambda conn: {"data": {}}
sys.modules.setdefault("snapshot", snapshot)

try:
    import psycopg2  # noqa: F401
except ImportError:
    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.connect = lambda database_url: False
    extras = types.ModuleType("psycopg2.extras")

    class Json:
        def __init__(self, adapted):
            self.adapted = adapted

    extras.Json = Json
    extras.RealDictCursor = object
    psycopg2.extras = extras
    sys.modules["psycopg2"] = psycopg2
    sys.modules["psycopg2.extras"] = extras

os.environ.setdefault("RISK_ADMIN_TOKEN", "test-risk-token")
os.environ.setdefault("DATABASE_URL", "postgresql://fake")

read_api = _load_module("live_mirror_read_api", API_PATH)


class FakeDB:
    def __init__(self):
        self.attribution_by_idem = {}
        self.intents_by_idem = {}
        self.executions = []
        self.connections = []

    def connect(self, database_url):
        conn = FakeConnection(self)
        self.connections.append(conn)
        return conn


class FakeConnection:
    def __init__(self, db):
        self.db = db
        self.closed = False
        self.committed = False

    def cursor(self, *args, **kwargs):
        return FakeCursor(self.db)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.result = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        params = params or ()
        compact = " ".join(sql.split())
        self.db.executions.append((compact, params))
        self.result = False

        if "JOIN hermes_decisions hd" in compact and "JOIN raw_messages rm" in compact:
            idem = params[0]
            self.result = self.db.attribution_by_idem.get(idem, False)
            return

        if compact.startswith(
            "SELECT intent_id::text, status::text, valid_until "
            "FROM trade_intents WHERE idempotency_key=%s"
        ) or compact.startswith(
            "SELECT intent_id::text, status::text, valid_until, order_plan "
            "FROM trade_intents WHERE idempotency_key=%s"
        ):
            self.result = self.db.intents_by_idem.get(params[0], False)
            return

        if compact.startswith("SELECT 1 FROM trade_intents WHERE intent_id::text = %s"):
            self.result = (1,)
            return

        if compact.startswith("SELECT side FROM positions_projection"):
            self.result = False
            return

        if compact.startswith("INSERT INTO trade_intents"):
            intent_id = params[0]
            valid_until = params[-2]
            idem = params[-1]
            order_plan = getattr(params[6], "adapted", params[6])
            self.db.intents_by_idem[idem] = (
                intent_id,
                "approved",
                valid_until,
                order_plan,
            )

    def fetchone(self):
        return self.result

    def fetchall(self):
        if self.result is False:
            return []
        if isinstance(self.result, list):
            return self.result
        return [self.result]


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(read_api.psycopg2, "connect", db.connect)
    monkeypatch.setattr(read_api, "_size_open_order", lambda *args, **kwargs: 100.0)
    monkeypatch.setattr(read_api, "_validate_stop_direction", lambda *args, **kwargs: None)
    monkeypatch.setattr(read_api, "_safe_execution_preview", lambda *args, **kwargs: {})
    return db


@pytest.fixture
def api_client(fake_db, monkeypatch, tmp_path):
    monkeypatch.setenv("RISK_ADMIN_TOKEN", "test-risk-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("ATTRIBUTION_SHADOW_LOG", str(tmp_path / "attribution.jsonl"))
    return TestClient(read_api.app)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-risk-token"}


@pytest.fixture
def load_trade_module():
    return lambda: _load_module("test_v3_trade", TRADE_PATH)


@pytest.fixture
def load_query_module():
    return lambda: _load_module("test_v3_query", QUERY_PATH)
