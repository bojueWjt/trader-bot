from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest


def test_intent_prefix_multiple_matches_lists_candidates_and_exits(
    load_query_module, monkeypatch, capsys
):
    module = load_query_module()
    monkeypatch.setattr(
        module,
        "q_json",
        lambda sql: [
            {
                "intent_id": "abcd1111-1111-1111-1111-111111111111",
                "created_at": "2026-07-14T01:00:00+00:00",
                "instrument_id": "BTCUSDT",
            },
            {
                "intent_id": "abcd2222-2222-2222-2222-222222222222",
                "created_at": "2026-07-14T02:00:00+00:00",
                "instrument_id": "ETHUSDT",
            },
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.cmd_intent(SimpleNamespace(intent_id="abcd"))

    output = json.loads(capsys.readouterr().out)
    assert exc.value.code == 1
    assert "multiple intents match" in output["error"]
    assert output["candidates"] == [
        {
            "intent": "abcd1111",
            "created_at": "2026-07-14T01:00:00+00:00",
            "symbol": "BTCUSDT",
        },
        {
            "intent": "abcd2222",
            "created_at": "2026-07-14T02:00:00+00:00",
            "symbol": "ETHUSDT",
        },
    ]


def test_nodes_parses_http_503_health_body(load_query_module, monkeypatch, capsys):
    module = load_query_module()

    def fake_query(sql):
        if "FROM node_heartbeats" in sql:
            return [{
                "node_id": "node-a",
                "status": "HALTED",
                "last_seen_at": "2026-07-30T02:42:11+00:00",
                "age_seconds": 1.2,
                "reconciliation": "healthy",
            }]
        return [{
            "command_type": "HALT",
            "status": "completed",
            "reason": "maintenance",
            "created_at": "2026-07-30T02:41:27+00:00",
            "completed_at": "2026-07-30T02:41:29+00:00",
        }]

    body = io.BytesIO(json.dumps({
        "ready": False,
        "trading_state": "HALTED",
        "halt_reason": "control-plane heartbeat stale",
    }).encode())
    error = urllib.error.HTTPError(
        "http://node-a/ready",
        503,
        "Service Unavailable",
        {},
        body,
    )
    monkeypatch.setattr(module, "q_json", fake_query)
    monkeypatch.setattr(module, "NODE_HEALTH", {"node-a": "http://node-a/ready"})
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    module.cmd_nodes(SimpleNamespace())

    output = json.loads(capsys.readouterr().out)
    health = output["nodes"][0]["local_health"]
    assert health["http_status"] == 503
    assert health["halt_reason"] == "control-plane heartbeat stale"
    assert output["warnings"] == [
        "node-a HALTED 原因: control-plane heartbeat stale"
    ]


def test_nodes_treats_commands_as_history_and_ignores_stale_reason_when_active(
    load_query_module, monkeypatch, capsys
):
    module = load_query_module()

    def fake_query(sql):
        if "FROM node_heartbeats" in sql:
            return [{
                "node_id": "node-a",
                "status": "ACTIVE",
                "last_seen_at": "2026-07-30T03:03:00+00:00",
                "age_seconds": 1.0,
                "reconciliation": "healthy",
            }]
        return [{
            "command_type": "HALT",
            "status": "completed",
            "reason": "historical maintenance",
            "created_at": "2026-07-30T02:41:27+00:00",
            "completed_at": "2026-07-30T02:41:29+00:00",
        }]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "ready": True,
                "trading_state": "ACTIVE",
                "halt_reason": "old reason",
            }).encode()

    monkeypatch.setattr(module, "q_json", fake_query)
    monkeypatch.setattr(module, "NODE_HEALTH", {"node-a": "http://node-a/ready"})
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: Response())

    module.cmd_nodes(SimpleNamespace())

    output = json.loads(capsys.readouterr().out)
    assert "warnings" not in output
    assert output["operator_command_history"][0]["command_type"] == "HALT"
    assert "不代表当前节点状态" in output["operator_command_history_note"]
