import pytest

from security.permissions import PermissionDenied
from security.principal import (
    PrincipalKind, TokenCatalogError, assert_account_authorized,
    can_read_trace, can_write_operator_orders, resolve_principal,
)


def test_server_credentials_determine_kind_and_account_despite_forged_body():
    env = {"RISK_ADMIN_TOKEN": "operator", "SIGNAL_TOKEN_ACCOUNT_A": "signal"}
    principal = resolve_principal(
        "Bearer signal", env=env,
        body={"actor": "risk_admin", "account_id": "account-b", "kind": "operator"},
    )
    assert principal.kind is PrincipalKind.SIGNAL
    assert principal.account_id == "account-a"
    assert not can_read_trace(principal)
    assert not can_write_operator_orders(principal)
    assert_account_authorized(principal, "account-a")
    with pytest.raises(PermissionDenied):
        assert_account_authorized(principal, "account-b")


@pytest.mark.parametrize("env", [
    {"SIGNAL_TOKEN_ACCOUNT_A": "same", "SIGNAL_TOKEN_ACCOUNT_B": "same"},
    {"SIGNAL_TOKEN_ACCOUNT_A": "same", "RISK_ADMIN_TOKEN": "same"},
    {"SIGNAL_TOKEN_ACCOUNT_A": "same", "NAUTILUS_NODE_TOKEN": "same"},
    {"SIGNAL_TOKEN_ACCOUNT_A": "same", "NAUTILUS_NODE_AUTH_JSON": '{"node-a":{"token":"same"}}'},
])
def test_colliding_credentials_fail_closed(env):
    with pytest.raises(TokenCatalogError):
        resolve_principal("Bearer same", env=env)


def test_global_operator_and_reader_have_distinct_write_authority():
    env = {"RISK_ADMIN_TOKEN": "operator", "VIEWER_TOKEN": "reader"}
    operator = resolve_principal("Bearer operator", env=env)
    reader = resolve_principal("Bearer reader", env=env)
    assert operator.kind is PrincipalKind.OPERATOR
    assert reader.kind is PrincipalKind.READER
    assert can_write_operator_orders(operator)
    assert not can_write_operator_orders(reader)
    for account in ("account-a", "account-b", "account-c", "account-d"):
        assert_account_authorized(operator, account)
    with pytest.raises(PermissionDenied):
        assert_account_authorized(operator, "unassigned")
