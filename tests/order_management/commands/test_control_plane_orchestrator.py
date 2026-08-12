from __future__ import annotations

import pytest

from order_management import commands as om_commands
from api import read_api


def test_request_persists_accepted_not_terminal(db_conn):
    run = om_commands.request_command_run(
        db_conn,
        node_id="om6-node-a",
        command_type="cancel_all",
        request_id="om6-01-accepted",
        payload={"account_id": "acct-1"},
    )

    assert run["status"] == "accepted"
    assert run["completed"] is False
    assert run["idempotent"] is False

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, request_id, payload FROM node_command_runs WHERE node_command_run_id=%s",
            (run["node_command_run_id"],),
        )
        row = cur.fetchone()
    assert row[0] == "accepted"
    assert row[1] == "om6-01-accepted"
    assert row[2]["account_id"] == "acct-1"


def test_request_replay_by_request_id_returns_same_terminal_result(db_conn):
    first = om_commands.request_command_run(
        db_conn,
        node_id="om6-node-a",
        command_type="close_all",
        request_id="om6-01-replay",
        payload={"account_id": "acct-1"},
    )
    om_commands.mark_running(db_conn, request_id="om6-01-replay", result={"dispatched": True})
    om_commands.mark_verifying(db_conn, request_id="om6-01-replay", result={"node_acked": True})
    terminal = om_commands.mark_terminal(
        db_conn,
        request_id="om6-01-replay",
        status="completed",
        result={"verified_flat": True},
    )

    replay = om_commands.request_command_run(
        db_conn,
        node_id="om6-node-a",
        command_type="close_all",
        request_id="om6-01-replay",
        payload={"account_id": "acct-1", "ignored_on_replay": True},
    )

    assert terminal["status"] == "completed"
    assert replay["idempotent"] is True
    assert replay["node_command_run_id"] == first["node_command_run_id"]
    assert replay["status"] == "completed"
    assert replay["payload"]["result"] == {"verified_flat": True}


def test_command_run_cannot_skip_venue_verification(db_conn):
    om_commands.request_command_run(
        db_conn,
        node_id="om6-node-a",
        command_type="cancel_all",
        request_id="om6-01-illegal",
    )

    with pytest.raises(om_commands.CommandRunTransitionError):
        om_commands.mark_terminal(
            db_conn,
            request_id="om6-01-illegal",
            status="completed",
            result={"verified": False},
        )


def test_node_ack_state_machine_keeps_running_non_terminal():
    assert read_api._command_ack_transition_allowed("pending", "accepted")
    assert read_api._command_ack_transition_allowed("accepted", "running")
    assert read_api._command_ack_transition_allowed("running", "completed")
    assert read_api._command_ack_transition_allowed("running", "failed")
    assert not read_api._command_ack_transition_allowed(
        "completed",
        "running",
    )
    assert not read_api._command_ack_transition_allowed(
        "failed",
        "running",
    )
