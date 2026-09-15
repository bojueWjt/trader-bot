import pytest

from connection import transaction
from test_m1e_trace import AUTH, client, _seed_account, _seed_intent_chain


@pytest.mark.parametrize("resource", ["channels", "messages", "orders", "intents", "fills", "outcomes", "report"])
def test_portable_queries_use_real_selects_and_keep_freshness(client, db_conn, resource):
    with transaction(db_conn):
        _seed_account(db_conn)
        _seed_intent_chain(db_conn)
    response = client.get(f"/v1/query/{resource}?channel=-1001234567890", headers=AUTH)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["data_source"] == "postgres_projection"
    assert "stale" in result
    if resource in {"channels", "messages", "intents", "fills"}:
        assert len(result[resource]) == 1


def test_query_intent_prefix_and_parameters_are_not_sql(client, db_conn):
    with transaction(db_conn):
        _seed_account(db_conn)
        ids = _seed_intent_chain(db_conn)
    response = client.get(f"/v1/query/intent?prefix={ids['intent_id'][:8]}", headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["intent"]["intent_id"] == ids["intent_id"]
    response = client.get("/v1/query/messages", params={"channel": "' OR TRUE --"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["messages"] == []
    assert client.get("/v1/query/intent?prefix=%25", headers=AUTH).status_code == 400
    assert client.get("/v1/query/sql", headers=AUTH).status_code == 422


def test_query_requires_global_reader_and_has_no_writer_route(client, monkeypatch):
    monkeypatch.setenv("SIGNAL_TOKEN_ACCOUNT_A", "signal-a")
    assert client.get("/v1/query/channels").status_code == 401
    assert client.get("/v1/query/channels", headers={"Authorization": "Bearer signal-a"}).status_code == 403
    assert client.post("/v1/query/channels", headers=AUTH).status_code == 405
