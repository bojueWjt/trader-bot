from app.settings import canonicalize_database_url, get_settings


def test_empty_url_returns_empty_string():
    assert canonicalize_database_url("") == ""


def test_non_sqlite_url_passes_through():
    assert canonicalize_database_url("postgresql://x") == "postgresql://x"
    assert canonicalize_database_url("https://x") == "https://x"


def test_memory_sqlite_url_is_preserved():
    assert canonicalize_database_url("sqlite:///:memory:") == "sqlite:///:memory:"


def test_absolute_sqlite_url_is_normalized():
    raw_url = "sqlite:////tmp/workspace/../trading.db"

    assert canonicalize_database_url(raw_url) == "sqlite:////tmp/trading.db"


def test_relative_sqlite_url_resolves_against_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    assert canonicalize_database_url("sqlite:///relative/path.db") == (
        f"sqlite:///{tmp_path}/relative/path.db"
    )


def test_parent_segments_are_normalized(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    assert canonicalize_database_url("sqlite:///../trading.db") == (
        f"sqlite:///{tmp_path}/trading.db"
    )


def test_tilde_is_expanded(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    assert canonicalize_database_url("sqlite:///~/trading.db") == (
        f"sqlite:///{home}/trading.db"
    )
    assert canonicalize_database_url("sqlite:////~/trading.db") == (
        f"sqlite:///{home}/trading.db"
    )


def test_get_settings_canonicalizes_hermes_signal_store_url(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_SIGNAL_STORE_URL", "sqlite:///signals/store.db")
    monkeypatch.delenv("RISK_STATE_PATH", raising=False)

    settings = get_settings()

    assert settings.hermes_signal_store_url == f"sqlite:///{tmp_path}/signals/store.db"
