"""The unchanged login issuer and the control plane share one session contract."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import sys

import pytest

from security.permissions import AuthRequired
from security.principal import PrincipalKind, can_write_operator_orders, resolve_principal
from security.session_auth import SessionTokenError, verify_session_token

ROOT = Path(__file__).resolve().parents[3]
NOW = 2_000_000_000
SECRET = "session-contract-fixture-only"


def issued_session(role="risk_admin", subject="alice"):
    # Import the actual, unchanged bridge issuer without contaminating app modules
    # loaded by the control-plane tests in this process.
    code = """
import os, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from app.security.auth import issue_auth_token
os.environ['AUTH_SECRET_KEY'] = sys.argv[2]
with patch('app.security.auth.time.time', return_value=int(sys.argv[3])):
    print(issue_auth_token({'actor_id': sys.argv[4], 'role': sys.argv[5]}, 3600))
"""
    return subprocess.check_output(
        [sys.executable, "-c", code, str(ROOT / "bridge/apps/api"), SECRET, str(NOW), subject, role],
        text=True,
    ).strip()


def signed_fixture(claims, header=None):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    head = encode(header if header is not None else {"alg": "HS256", "typ": "JWT"})
    body = encode(claims)
    content = f"{head}.{body}"
    signature = hmac.new(SECRET.encode(), content.encode(), hashlib.sha256).digest()
    return content + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


@pytest.mark.parametrize("role", ["risk_admin", "viewer", "reviewer", "system_observer"])
def test_existing_issuer_identity_and_role_are_verified(role, monkeypatch):
    token = issued_session(role)
    assert verify_session_token(token, SECRET, now=NOW) == {"sub": "alice", "role": role}
    monkeypatch.setattr("security.session_auth.time.time", lambda: NOW)
    principal = resolve_principal("Bearer " + token, env={"AUTH_SECRET_KEY": SECRET},
                                  body={"role": "risk_admin", "actor_id": "forged"})
    assert principal.actor_id == "user:alice"
    assert principal.session_subject == "alice"
    assert principal.role == role
    assert principal.kind is (PrincipalKind.OPERATOR if role == "risk_admin" else PrincipalKind.READER)
    assert can_write_operator_orders(principal) is (role == "risk_admin")


def test_session_subject_cannot_impersonate_signal_actor(monkeypatch):
    token = issued_session(subject="signal:account-a")
    monkeypatch.setattr("security.session_auth.time.time", lambda: NOW)
    principal = resolve_principal("Bearer " + token, env={"AUTH_SECRET_KEY": SECRET})
    assert principal.actor_id == "user:signal:account-a"
    assert principal.kind is PrincipalKind.OPERATOR
    assert principal.session_subject == "signal:account-a"


@pytest.mark.parametrize("change", [
    {"exp": NOW}, {"exp": True}, {"iat": True}, {"iat": NOW + 1},
    {"iat": -1}, {"exp": "future"}, {"role": "nautilus_node"},
    {"role": "signal_agent"}, {"role": "operator"}, {"sub": ""},
    {"sub": "a\nb"}, {"sub": ["alice"]},
])
def test_signed_but_invalid_session_never_grants_authority(change):
    claims = {"sub": "alice", "role": "risk_admin", "iat": NOW, "exp": NOW + 3600}
    claims.update(change)
    with pytest.raises(SessionTokenError):
        verify_session_token(signed_fixture(claims), SECRET, now=NOW)


@pytest.mark.parametrize("header", [{"alg": "none", "typ": "JWT"},
                                    {"alg": "HS512", "typ": "JWT"},
                                    {"alg": "HS256"}, []])
def test_algorithm_and_header_are_fixed(header):
    claims = {"sub": "alice", "role": "risk_admin", "iat": NOW, "exp": NOW + 3600}
    with pytest.raises(SessionTokenError):
        verify_session_token(signed_fixture(claims, header), SECRET, now=NOW)


def test_tampering_missing_configuration_and_expiry_require_login(monkeypatch):
    token = issued_session()
    monkeypatch.setattr("security.session_auth.time.time", lambda: NOW)
    for value, secret in [(token, "different-key"), (token, ""),
                          ("!" + token, SECRET), ("a.b.c", SECRET)]:
        with pytest.raises(AuthRequired):
            resolve_principal("Bearer " + value, env={"AUTH_SECRET_KEY": secret})
    monkeypatch.setattr("security.session_auth.time.time", lambda: NOW + 3600)
    with pytest.raises(AuthRequired):
        resolve_principal("Bearer " + token, env={"AUTH_SECRET_KEY": SECRET})
