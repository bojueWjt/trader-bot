from __future__ import annotations

import json
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
