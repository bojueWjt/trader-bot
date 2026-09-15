import importlib.util
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit, parse_qs

import pytest


PATH = Path(__file__).resolve().parents[1] / "hermes-profile/skills/trading/v3-trader/scripts/v3_query.py"


def _load():
    spec = importlib.util.spec_from_file_location("portable_v3_query", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("argv,path", [
    (["channels"], "/v1/query/channels"),
    (["messages", "titan", "--limit", "3"], "/v1/query/messages"),
    (["positions"], "/v1/mirror/positions"),
    (["orders"], "/v1/query/orders"),
    (["intents", "--status", "rejected"], "/v1/query/intents"),
    (["intent", "abcd"], "/v1/query/intent"),
    (["fills", "--hours", "48"], "/v1/query/fills"),
    (["outcomes"], "/v1/query/outcomes"),
    (["nodes"], "/v1/nodes"),
    (["reconcile"], "/v1/reconcile"),
    (["report"], "/v1/query/report"),
    (["signals", "--days", "2"], "/v1/signals"),
])
def test_queries_run_without_host_files_and_preserve_unknown_state(monkeypatch, capsys, argv, path):
    module = _load()
    monkeypatch.setenv("V3_READ_TOKEN", "test-read-token")
    result = {"stale": True, "data_source": "postgres_projection", "source_ts": None}
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return json.dumps(result).encode()

    def urlopen(request, timeout):
        calls.append(request)
        assert request.get_header("Authorization") == "Bearer test-read-token"
        assert request.get_method() == "GET"
        return Response()

    def forbidden(*args, **kwargs):
        raise AssertionError("read CLI attempted host file access")

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(sys, "argv", ["v3_query.py", *argv])
    module.main()
    assert json.loads(capsys.readouterr().out) == result
    assert len(calls) == 1
    assert urlsplit(calls[0].full_url).path == path
    if argv[0] == "messages":
        assert parse_qs(urlsplit(calls[0].full_url).query)["channel"] == ["-1002198013097"]
