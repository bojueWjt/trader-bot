from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import bootstrap_control_plane_roles as bootstrap  # noqa: E402


ROLE_BY_LABEL = {
    spec.label: spec.database_role
    for spec in bootstrap.ROLE_SPECS
}
PASSWORDS = {
    "trader_v3_node_control": "node-pass-fixture",
    "trader_v3_event_ingest": "event-pass-fixture",
    "trader_v3_operator_query": "operator-pass-fixture",
}


def _runtime_env_text() -> str:
    return "\n".join(
        [
            "DATABASE_URL=postgresql://admin:super-secret@db.local:5432/trader?sslmode=disable",
            "NAUTILUS_NODE_AUTH_JSON='{\"node-a\":{\"account_id\":\"account-a\",\"token\":\"node-a-secret\"}}'",
            "NAUTILUS_NODE_TOKEN=legacy-node-secret",
            "RISK_ADMIN_TOKEN=risk-admin-secret",
            "VIEWER_TOKEN=viewer-secret",
            "REVIEWER_TOKEN=reviewer-secret",
            "SYSTEM_OBSERVER_TOKEN=observer-secret",
            "TRADER_TRADING_DB_PATH=/data/watcher-trading.db",
            "WATCHER_TRADING_DB=/data/watcher-trading.db",
            "OPERATOR_ACCOUNT_REGISTRY_JSON='{\"account-a\":{},\"account-b\":{},\"account-c\":{},\"account-d\":{}}'",
            "OPERATOR_MAX_LEVERAGE=9",
            "OPERATOR_MAX_RISK_FRACTION=0.06",
            "OPERATOR_NO_SL_EQUITY_FRACTION=0.2",
            "OPERATOR_EQUITY_MAX_AGE_SECONDS=45",
            "OPERATOR_DEFAULT_RISK_RATIO=0.01",
            "OPERATOR_DEFAULT_ACCOUNT=account-a",
            "MEDIA_ROOT=/srv/trader-v3/media",
            "ATTRIBUTION_SHADOW_LOG=/srv/trader-v3/logs/attribution-shadow.jsonl",
            "CONTROL_PLANE_NODE_CONTROL_DB_POOL_SIZE=3",
            "CONTROL_PLANE_EVENT_INGEST_DB_POOL_SIZE=5",
            "CONTROL_PLANE_OPERATOR_QUERY_DB_STATEMENT_TIMEOUT_MS=2500",
            "AUTH_SECRET_KEY=must-not-copy",
            "POSTGRES_PASSWORD=must-not-copy",
            "TRADER_API_TOKEN=must-not-copy",
            "BINANCE_API_SECRET=must-not-copy",
            "FREQTRADE_EXCHANGE_SECRET=must-not-copy",
        ]
    ) + "\n"


def _write_runtime_env(tmp_path: Path, text: str | bool = False) -> Path:
    env_file = tmp_path / ".env.v3"
    if text is False:
        text = _runtime_env_text()
    env_file.write_text(text, encoding="utf-8")
    return env_file


def _role_urls(role_envs: dict[str, dict[str, str]]) -> dict[str, str]:
    return {
        label: values["DATABASE_URL"]
        for label, values in role_envs.items()
    }


def _role_url_user(database_url: str) -> str:
    parsed = urlsplit(database_url)
    return unquote(parsed.username or "")


def _role_url_password(database_url: str) -> str:
    parsed = urlsplit(database_url)
    return unquote(parsed.password or "")


def _parse_rendered_env(path: Path) -> dict[str, str]:
    return bootstrap.parse_env_file(path)


class _FakeCursor:
    def __init__(self, connection: "_FakeConnection") -> None:
        self.connection = connection

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[str, ...] | None = None) -> None:
        self.connection.queries.append((sql, params))

    def fetchone(self) -> tuple[str, str]:
        return (
            self.connection.session_user,
            self.connection.current_user,
        )


class _FakeConnection:
    def __init__(
        self,
        *,
        session_user: str,
        current_user: str | bool = False,
    ) -> None:
        self.session_user = session_user
        if current_user is False:
            current_user = session_user
        self.current_user = str(current_user)
        self.queries: list[tuple[str, tuple[str, ...] | None]] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def _identity_connect(database_url: str) -> _FakeConnection:
    return _FakeConnection(session_user=_role_url_user(database_url))


def test_check_mode_builds_role_specific_allowlisted_envs_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = _write_runtime_env(tmp_path)
    output_dir = tmp_path / "control-plane"

    result = bootstrap.run_check(env_file, output_dir)
    assert bootstrap.main(
        [
            "check",
            "--env-file",
            str(env_file),
            "--output-dir",
            str(output_dir),
        ]
    ) == 0

    captured = capsys.readouterr()
    for secret in (
        "super-secret",
        "node-a-secret",
        "legacy-node-secret",
        "risk-admin-secret",
        "viewer-secret",
        "must-not-copy",
    ):
        assert secret not in captured.out
        assert secret not in captured.err

    role_envs = result["role_envs"]
    assert not output_dir.exists()

    node = role_envs["node-control"]
    event = role_envs["event-ingest"]
    operator = role_envs["operator-query"]

    assert set(node) == {
        "CONTROL_PLANE_APP_ROLE",
        "CONTROL_PLANE_EXPECT_DATABASE_ROLE",
        "DATABASE_URL",
        "NAUTILUS_NODE_AUTH_JSON",
        "NAUTILUS_NODE_TOKEN",
        "CONTROL_PLANE_NODE_CONTROL_DB_POOL_SIZE",
    }
    assert set(event) == {
        "CONTROL_PLANE_APP_ROLE",
        "CONTROL_PLANE_EXPECT_DATABASE_ROLE",
        "DATABASE_URL",
        "NAUTILUS_NODE_AUTH_JSON",
        "NAUTILUS_NODE_TOKEN",
        "CONTROL_PLANE_EVENT_INGEST_DB_POOL_SIZE",
    }
    assert {
        "CONTROL_PLANE_APP_ROLE",
        "CONTROL_PLANE_EXPECT_DATABASE_ROLE",
        "DATABASE_URL",
        "RISK_ADMIN_TOKEN",
        "VIEWER_TOKEN",
        "REVIEWER_TOKEN",
        "SYSTEM_OBSERVER_TOKEN",
        "TRADER_TRADING_DB_PATH",
        "WATCHER_TRADING_DB",
        "OPERATOR_ACCOUNT_REGISTRY_JSON",
        "OPERATOR_MAX_LEVERAGE",
        "OPERATOR_MAX_RISK_FRACTION",
        "OPERATOR_NO_SL_EQUITY_FRACTION",
        "OPERATOR_EQUITY_MAX_AGE_SECONDS",
        "OPERATOR_DEFAULT_RISK_RATIO",
        "OPERATOR_DEFAULT_ACCOUNT",
        "MEDIA_ROOT",
        "ATTRIBUTION_SHADOW_LOG",
        "CONTROL_PLANE_OPERATOR_QUERY_DB_STATEMENT_TIMEOUT_MS",
    } == set(operator)

    for values in role_envs.values():
        assert "AUTH_SECRET_KEY" not in values
        assert "POSTGRES_PASSWORD" not in values
        assert "TRADER_API_TOKEN" not in values
        assert "BINANCE_API_SECRET" not in values
        assert "FREQTRADE_EXCHANGE_SECRET" not in values


def test_generated_database_urls_are_independent_and_role_specific(
    tmp_path: Path,
) -> None:
    env = bootstrap.parse_env_file(_write_runtime_env(tmp_path))

    role_envs = bootstrap.build_role_envs(env, PASSWORDS)
    urls = _role_urls(role_envs)

    assert len(set(urls.values())) == 3
    for label, database_url in urls.items():
        assert _role_url_user(database_url) == ROLE_BY_LABEL[label]
        assert _role_url_password(database_url) == PASSWORDS[ROLE_BY_LABEL[label]]
        parsed = urlsplit(database_url)
        assert parsed.hostname == "db.local"
        assert parsed.port == 5432
        assert parsed.path == "/trader"
        assert parsed.query == "sslmode=disable"


@pytest.mark.parametrize(
    ("env_text", "match"),
    (
        (
            "DATABASE_URL=postgresql://admin:secret@db/trader\n"
            "RISK_ADMIN_TOKEN=token\n"
            "TRADER_TRADING_DB_PATH=/data/watcher-trading.db\n",
            "node auth token configuration",
        ),
        (
            "DATABASE_URL=postgresql://admin:secret@db/trader\n"
            "NAUTILUS_NODE_TOKEN=node\n"
            "TRADER_TRADING_DB_PATH=/data/watcher-trading.db\n",
            "risk admin token",
        ),
        (
            "DATABASE_URL=postgresql://admin:secret@db/trader\n"
            "NAUTILUS_NODE_TOKEN=node\n"
            "RISK_ADMIN_TOKEN=token\n",
            "watcher SQLite path",
        ),
        (
            "DATABASE_URL=postgresql://admin:secret@db/trader\n"
            "NAUTILUS_NODE_TOKEN=node\n"
            "RISK_ADMIN_TOKEN=token\n"
            "TRADER_TRADING_DB_PATH=/data/a.db\n"
            "WATCHER_TRADING_DB=/data/b.db\n",
            "conflicting watcher SQLite path",
        ),
        (
            "DATABASE_URL=sqlite:///tmp/nope.db\n"
            "NAUTILUS_NODE_TOKEN=node\n"
            "RISK_ADMIN_TOKEN=token\n"
            "TRADER_TRADING_DB_PATH=/data/watcher-trading.db\n",
            "DATABASE_URL scheme",
        ),
    ),
)
def test_role_env_generation_fails_closed_on_missing_or_conflicting_runtime_env(
    tmp_path: Path,
    env_text: str,
    match: str,
) -> None:
    env_file = _write_runtime_env(tmp_path, env_text)
    env = bootstrap.parse_env_file(env_file)

    with pytest.raises(bootstrap.BootstrapControlPlaneRolesError, match=match):
        bootstrap.build_role_envs(env, PASSWORDS)


def test_apply_requires_operation_lock_before_database_or_file_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = _write_runtime_env(tmp_path)
    output_dir = tmp_path / "control-plane"
    called = {"alter": 0, "verify": 0}
    monkeypatch.setenv(
        bootstrap.OPERATION_LOCK_OWNERSHIP_ENV,
        "invalid",
    )
    monkeypatch.setattr(
        bootstrap,
        "alter_role_passwords",
        lambda *_args, **_kwargs: called.__setitem__("alter", 1),
    )
    monkeypatch.setattr(
        bootstrap,
        "verify_role_database_identities",
        lambda *_args, **_kwargs: called.__setitem__("verify", 1),
    )

    with pytest.raises(
        bootstrap.BootstrapControlPlaneRolesError,
        match="invalid account-stall operation lock ownership",
    ):
        bootstrap.run_apply(
            env_file,
            output_dir,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert called == {"alter": 0, "verify": 0}
    assert not output_dir.exists()


def test_apply_alters_roles_verifies_identities_and_writes_0600_env_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = _write_runtime_env(tmp_path)
    output_dir = tmp_path / "control-plane"
    lock_path = tmp_path / "account-stall-operation.lock"
    calls: list[tuple[str, object]] = []
    original_verify = bootstrap.verify_role_database_identities

    monkeypatch.setenv(bootstrap.TEST_MODE_ENV, "1")
    monkeypatch.setenv(
        bootstrap.TEST_OPERATION_LOCK_PATH_ENV,
        str(lock_path),
    )
    monkeypatch.setattr(
        bootstrap,
        "generate_passwords",
        lambda: dict(PASSWORDS),
    )

    def fake_alter(admin_database_url: str, passwords: dict[str, str]) -> None:
        calls.append(("alter", admin_database_url, dict(passwords)))

    def fake_verify(
        role_envs: dict[str, dict[str, str]],
    ) -> dict[str, dict[str, str]]:
        calls.append(("verify", _role_urls(role_envs)))
        return original_verify(
            role_envs,
            connect=_identity_connect,
        )

    monkeypatch.setattr(bootstrap, "alter_role_passwords", fake_alter)
    monkeypatch.setattr(
        bootstrap,
        "verify_role_database_identities",
        fake_verify,
    )

    result = bootstrap.run_apply(
        env_file,
        output_dir,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert calls[0] == (
        "alter",
        "postgresql://admin:super-secret@db.local:5432/trader?sslmode=disable",
        PASSWORDS,
    )
    assert calls[1][0] == "verify"
    assert sorted(result["identities"]) == sorted(ROLE_BY_LABEL)
    assert lock_path.exists()

    for label, path in result["paths"].items():
        metadata = path.stat()
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        assert metadata.st_uid == os.getuid()
        assert metadata.st_gid == os.getgid()
        values = _parse_rendered_env(path)
        assert values["CONTROL_PLANE_EXPECT_DATABASE_ROLE"] == ROLE_BY_LABEL[label]
        assert _role_url_user(values["DATABASE_URL"]) == ROLE_BY_LABEL[label]
        assert _role_url_password(values["DATABASE_URL"]) == PASSWORDS[
            ROLE_BY_LABEL[label]
        ]


def test_alter_role_passwords_uses_three_expected_role_statements() -> None:
    connection = _FakeConnection(session_user="admin")

    bootstrap.alter_role_passwords(
        "postgresql://admin:secret@db/trader",
        PASSWORDS,
        connect=lambda _url: connection,
    )

    assert connection.commit_calls == 1
    assert connection.rollback_calls == 0
    assert connection.close_calls == 1
    assert connection.queries == [
        (
            "ALTER ROLE trader_v3_node_control WITH PASSWORD %s",
            ("node-pass-fixture",),
        ),
        (
            "ALTER ROLE trader_v3_event_ingest WITH PASSWORD %s",
            ("event-pass-fixture",),
        ),
        (
            "ALTER ROLE trader_v3_operator_query WITH PASSWORD %s",
            ("operator-pass-fixture",),
        ),
    ]


def test_verify_role_database_identities_requires_session_and_current_user(
    tmp_path: Path,
) -> None:
    env = bootstrap.parse_env_file(_write_runtime_env(tmp_path))
    role_envs = bootstrap.build_role_envs(env, PASSWORDS)

    identities = bootstrap.verify_role_database_identities(
        role_envs,
        connect=_identity_connect,
    )

    assert identities == {
        label: {
            "session_user": database_role,
            "current_user": database_role,
        }
        for label, database_role in ROLE_BY_LABEL.items()
    }

    def wrong_current_user(database_url: str) -> _FakeConnection:
        return _FakeConnection(
            session_user=_role_url_user(database_url),
            current_user="postgres",
        )

    with pytest.raises(
        bootstrap.BootstrapControlPlaneRolesError,
        match="current_user mismatch",
    ):
        bootstrap.verify_role_database_identities(
            role_envs,
            connect=wrong_current_user,
        )


def test_parse_args_defaults_to_production_root_owned_targets() -> None:
    args = bootstrap.parse_args(["apply"])

    assert args.env_file == Path("/srv/trader-v3/.env.v3")
    assert args.output_dir == Path("/srv/trader-v3/secrets/control-plane")
    assert args.owner_uid == 0
    assert args.owner_gid == 0
