"""The existing login issuer can authorize the real operator transaction."""
from pathlib import Path
import subprocess
import sys

import psycopg2

from test_operator_add_position import client, _entry_body, _same_side_books


ROOT = Path(__file__).resolve().parents[3]
SECRET = "dashboard-session-integration-fixture"


def _session(role="risk_admin", subject="alice", *, age=0):
    # Keep the bridge's app package isolated from the control-plane test process.
    code = """
import os, sys, time
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from app.security.auth import issue_auth_token
os.environ['AUTH_SECRET_KEY'] = sys.argv[2]
issued_at = int(time.time()) - int(sys.argv[5])
with patch('app.security.auth.time.time', return_value=issued_at):
    print(issue_auth_token({'actor_id': sys.argv[3], 'role': sys.argv[4]}, 3600))
"""
    return subprocess.check_output(
        [sys.executable, "-c", code, str(ROOT / "bridge/apps/api"), SECRET,
         subject, role, str(age)], text=True,
    ).strip()


def test_login_session_accepts_and_queries_with_signed_audit_identity(client, migrated_db, monkeypatch):
    monkeypatch.setenv("AUTH_SECRET_KEY", SECRET)
    _same_side_books(migrated_db)
    token = _session(subject="signal:account-a")
    headers = {"Authorization": f"Bearer {token}", "X-Request-Id": "session-accepted"}
    body = _entry_body("add_position", "session-accepted")
    body.update(authorized_by_type="user", authorized_by_id="forged-admin")
    response = client.post("/v1/operator/orders", headers=headers, json=body)
    assert response.status_code == 200, response.text
    intent_id = response.json()["intent_id"]
    replay = client.post("/v1/operator/orders", headers=headers, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["intent_id"] == intent_id
    query = client.get(f"/v1/operator/orders/{intent_id}", headers=headers)
    assert query.status_code == 200, query.text
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT order_plan FROM trade_intents WHERE intent_id=%s", (intent_id,))
        plan = cur.fetchone()[0]
        assert plan["principal"] == {
            "kind": "operator", "actor_id": "user:signal:account-a", "scope": "global",
            "account_id": None, "session_subject": "signal:account-a",
        }
        assert plan["authorization"]["authorized_by_id"] == "user:signal:account-a"
        cur.execute("SELECT actor,payload FROM audit_events WHERE intent_id=%s AND event_type='operator_order'", (intent_id,))
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "user:signal:account-a"
        assert rows[0][1]["principal"] == plan["principal"]


def test_invalid_or_readonly_sessions_cannot_create_operator_intent(client, migrated_db, monkeypatch):
    monkeypatch.setenv("AUTH_SECRET_KEY", SECRET)
    valid = _session()
    head, claims, signature = valid.split(".")
    forged = f"{head}.{claims}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    cases = [(_session(role), 403) for role in ("viewer", "reviewer", "system_observer")]
    cases += [(_session(age=7200), 401), (forged, 401), (_session("signal_agent"), 401),
              (_session("nautilus_node"), 401), (_session("unknown"), 401)]
    body = _entry_body("add_position", "session-rejected")
    body.update(role="risk_admin", actor_id="risk_admin", authorized_by_type="user", authorized_by_id="risk_admin")
    for token, expected in cases:
        response = client.post("/v1/operator/orders", headers={"Authorization": f"Bearer {token}"}, json=body)
        assert response.status_code == expected, response.text
    monkeypatch.delenv("AUTH_SECRET_KEY")
    response = client.post("/v1/operator/orders", headers={"Authorization": f"Bearer {valid}"}, json=body)
    assert response.status_code == 401, response.text
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trade_intents")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM audit_events WHERE event_type='operator_order'")
        assert cur.fetchone()[0] == 0
