from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class AppSettings:
    app_name: str = "trader-api"
    risk_state_path: str = ""
    hermes_signal_store_url: str = ""
    signal_store_url: str = ""
    freqtrade_base_url: str = "http://127.0.0.1:8080"
    freqtrade_api_user: str = ""
    freqtrade_api_password: str = ""
    request_id_header: str = "x-request-id"


def canonicalize_database_url(raw_url: str) -> str:
    if raw_url == "":
        return ""

    if raw_url == "sqlite:///:memory:":
        return raw_url

    sqlite_prefix = "sqlite:///"
    if not raw_url.startswith(sqlite_prefix):
        return raw_url

    database_path = raw_url[len(sqlite_prefix) :]
    if database_path.startswith("/~/"):
        database_path = "~/" + database_path[3:]
    if database_path == "/~":
        database_path = "~"

    database_path = os.path.expanduser(database_path)
    if not os.path.isabs(database_path):
        database_path = os.path.join(os.getcwd(), database_path)

    database_path = os.path.abspath(os.path.normpath(database_path))
    return f"sqlite:///{database_path}"


def get_settings() -> AppSettings:
    hermes_signal_store_url = os.environ.get("HERMES_SIGNAL_STORE_URL", "")
    signal_store_url = os.environ.get("SIGNAL_STORE_URL", hermes_signal_store_url)

    return AppSettings(
        risk_state_path=os.environ.get("RISK_STATE_PATH", ""),
        hermes_signal_store_url=canonicalize_database_url(hermes_signal_store_url),
        signal_store_url=canonicalize_database_url(signal_store_url),
        freqtrade_base_url=os.environ.get("FREQTRADE_BASE_URL", "http://127.0.0.1:8080"),
        freqtrade_api_user=os.environ.get("FREQTRADE_API_USER", ""),
        freqtrade_api_password=os.environ.get("FREQTRADE_API_PASSWORD", ""),
    )
