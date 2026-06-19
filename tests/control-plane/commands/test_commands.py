from __future__ import annotations

import commands
import risk_state


def _mode(conn, account, instrument):
    with conn.cursor() as cur:
        return risk_state.get_mode(cur, account, instrument)


def issue_halt(conn, *, nodes=("node-a", "node-b"), key="k1", timeout=30):
    return commands.issue_command(
        conn,
        command_type="HALT",
        requested_by="op-1",
        reason="manual kill switch",
        idempotency_key=key,
        target_nodes=list(nodes),
        scope={"account_id": "acct-1", "instruments": ["BTCUSDT"]},
        ack_timeout_seconds=timeout,
    )


def test_http_return_is_not_completion_and_sets_halted_mode(db_conn):
    issued = issue_halt(db_conn)
    # not completed merely because the call returned
    assert issued["status"] == "pending"
    status = commands.get_status(db_conn, issued["command_id"])
    assert status["completed"] is False
    assert [a["status"] for a in status["acks"]] == ["pending", "pending"]
    # risk mode is applied immediately so the governor blocks new risk
    assert _mode(db_conn, "acct-1", "BTCUSDT") == "HALTED"


def test_all_nodes_ack_completes_command(db_conn):
    issued = issue_halt(db_conn)
    commands.record_ack(db_conn, issued["command_id"], "node-a", status="acked")
    final = commands.record_ack(db_conn, issued["command_id"], "node-b", status="acked")
    assert final["status"] == "completed" and final["completed"] is True


def test_single_node_failure_is_partial(db_conn):
    issued = issue_halt(db_conn)
    commands.record_ack(db_conn, issued["command_id"], "node-a", status="acked")
    final = commands.record_ack(db_conn, issued["command_id"], "node-b", status="failed", detail="node down")
    assert final["status"] == "partial"


def test_timeout_marks_unacked_failed(db_conn):
    issued = issue_halt(db_conn, timeout=-5)  # expires_at already in the past
    commands.record_ack(db_conn, issued["command_id"], "node-a", status="acked")
    expired = commands.expire_timeouts(db_conn)
    assert issued["command_id"] in expired
    final = commands.get_status(db_conn, issued["command_id"])
    assert final["status"] == "partial"
    assert {a["node_id"]: a["status"] for a in final["acks"]}["node-b"] == "failed"


def test_all_timeout_fails_command(db_conn):
    issued = issue_halt(db_conn, timeout=-5)
    commands.expire_timeouts(db_conn)
    final = commands.get_status(db_conn, issued["command_id"])
    assert final["status"] == "failed"


def test_duplicate_idempotency_key_returns_same_command(db_conn):
    first = issue_halt(db_conn, key="dup")
    second = issue_halt(db_conn, key="dup")
    assert second["idempotent"] is True
    assert second["command_id"] == first["command_id"]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM operator_commands")
        assert cur.fetchone()[0] == 1


def test_resume_sets_active_mode(db_conn):
    issue_halt(db_conn, key="halt1")
    assert _mode(db_conn, "acct-1", "BTCUSDT") == "HALTED"
    commands.issue_command(
        db_conn,
        command_type="RESUME",
        requested_by="op-1",
        reason="resume",
        idempotency_key="resume1",
        target_nodes=["node-a"],
        scope={"account_id": "acct-1", "instruments": ["BTCUSDT"]},
    )
    assert _mode(db_conn, "acct-1", "BTCUSDT") == "ACTIVE"


def test_default_mode_is_active(db_conn):
    assert _mode(db_conn, "acct-x", "ETHUSDT") == "ACTIVE"
