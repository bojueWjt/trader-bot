"""Validated, per-process watcher configuration cache for order entry.

The cache never refreshes on an order request. A lease pins one immutable
snapshot, so routing, capital addon, and risk sizing use the same revision.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import math
import os
import re
import socket
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Callable, Mapping


SCHEMA_VERSION = "watcher-config-snapshot.v1"
DEFAULT_URL = "http://127.0.0.1:9090/api/trading/config-snapshot"
REFRESH_INTERVAL = 30.0
MAX_AGE = 60.0
REQUEST_TIMEOUT = 2.0
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
_READ_CHUNK = 65536
_BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0)
_RISK_UNAVAILABLE = "account risk ratio configuration is unavailable or invalid"
_TOP_FIELDS = frozenset({
    "schema_version", "revision", "content_sha256", "generated_at",
    "accounts", "channels", "risks",
})
_ACCOUNT_FIELDS = frozenset({
    "account_id", "kind", "parent_account_id", "execution_account_id",
    "enabled", "risk_capital_addon", "default_risk",
})
_CHANNEL_FIELDS = frozenset({"channel_id", "target_account_id"})
_RISK_FIELDS = frozenset({"symbol", "risk_ratio"})
_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?\Z")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_GENERATED_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z\Z")
_LOG = logging.getLogger(__name__)


class SnapshotUnavailable(Exception):
    """No verified and currently authorized snapshot is usable for entry."""


class SnapshotRouteConflict(Exception):
    """The requested execution account conflicts with a channel route."""


class SnapshotDataRejected(Exception):
    """A fresh snapshot produced the same business rejection as the SQLite reader."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _InvalidSnapshot(Exception):
    pass


class _SchemaMismatch(_InvalidSnapshot):
    pass


@dataclass(frozen=True)
class Account:
    account_id: str
    kind: str
    parent_account_id: str | None
    execution_account_id: str
    enabled: bool
    risk_capital_addon: float
    default_risk: float | None


@dataclass(frozen=True)
class ConfigSnapshot:
    revision: int
    content_sha256: str
    accounts: Mapping[str, Account]
    execution_accounts: Mapping[str, Account]
    channels: Mapping[str, str]
    risks: Mapping[str, float]


@dataclass(frozen=True)
class SnapshotLease:
    snapshot: ConfigSnapshot
    age_ms: int

    @property
    def check(self) -> dict:
        return {
            "name": "config_snapshot",
            "passed": True,
            "revision": self.snapshot.revision,
            "content_sha256": self.snapshot.content_sha256,
            "age_ms": self.age_ms,
            "snapshot_state": "fresh",
        }

    def account_addon(self, execution_account_id: str) -> float:
        account = self.snapshot.execution_accounts.get(execution_account_id)
        if account is None:
            raise SnapshotDataRejected(
                503,
                f"execution account {execution_account_id} must resolve to exactly "
                "one risk configuration",
            )
        if not account.enabled:
            raise SnapshotDataRejected(
                503,
                "watcher account risk configuration is disabled",
            )
        return account.risk_capital_addon

    def channel_route(self, channel_id: str | None, account_id: str | None = None) -> dict:
        normalized = str(channel_id or "").strip()
        if not normalized:
            raise SnapshotDataRejected(503, "channel risk route has no channel_id")
        target_id = self.snapshot.channels.get(normalized)
        if target_id is None:
            raise SnapshotDataRejected(
                503,
                f"channel {normalized} must resolve to exactly one execution account",
            )
        account = self.snapshot.accounts.get(target_id)
        if account is None:
            raise SnapshotDataRejected(
                503,
                "watcher channel route target account is invalid",
            )
        if account_id is not None and account.execution_account_id != account_id:
            raise SnapshotRouteConflict()
        if not account.enabled:
            raise SnapshotDataRejected(
                503,
                "watcher channel route target account is disabled",
            )
        return {
            "execution_account_id": account.execution_account_id,
            "risk_capital_addon": account.risk_capital_addon,
        }

    def channel_addon(self, channel_id: str, execution_account_id: str) -> float:
        return self.channel_route(channel_id, execution_account_id)["risk_capital_addon"]

    def risk_ratio(self, symbol: str, execution_account_id: str) -> float:
        ratio = self.snapshot.risks.get(symbol)
        if ratio is None:
            account = self.snapshot.execution_accounts.get(execution_account_id)
            if account is None:
                raw = os.environ.get("OPERATOR_DEFAULT_RISK_RATIO", "0.01")
                try:
                    ratio = float(raw)
                except (TypeError, ValueError) as exc:
                    raise SnapshotDataRejected(503, _RISK_UNAVAILABLE) from exc
            else:
                ratio = account.default_risk
        try:
            if ratio is None:
                raise TypeError("default risk is null")
            ratio = float(ratio)
            if not math.isfinite(ratio) or not 0 < ratio <= 0.1:
                raise ValueError("risk ratio must be in (0, 0.1]")
        except (TypeError, ValueError) as exc:
            raise SnapshotDataRejected(503, _RISK_UNAVAILABLE) from exc
        return ratio


def enabled() -> bool:
    return os.environ.get("WATCHER_CONFIG_SNAPSHOT_ENABLED", "0").strip() == "1"


def _safe_id(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _InvalidSnapshot()
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise _InvalidSnapshot()
    return value


def _number(value: object, *, allow_zero: bool) -> float:
    if not isinstance(value, str) or _DECIMAL_RE.fullmatch(value) is None:
        raise _InvalidSnapshot()
    try:
        number = Decimal(value)
        result = float(number)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise _InvalidSnapshot() from exc
    if not math.isfinite(result) or result < 0 or (not allow_zero and result == 0):
        raise _InvalidSnapshot()
    return result


def _check_fields(value: object, expected: frozenset[str]) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise _InvalidSnapshot()
    return value


def validate(raw: object, previous: ConfigSnapshot | None = None) -> ConfigSnapshot:
    data = _check_fields(raw, _TOP_FIELDS)
    if data["schema_version"] != SCHEMA_VERSION:
        raise _SchemaMismatch()
    revision = data["revision"]
    if type(revision) is not int or revision < 0:
        raise _InvalidSnapshot()
    digest = data["content_sha256"]
    if not isinstance(digest, str) or _SHA_RE.fullmatch(digest) is None:
        raise _InvalidSnapshot()
    generated_at = data["generated_at"]
    if not isinstance(generated_at, str) or _GENERATED_RE.fullmatch(generated_at) is None:
        raise _InvalidSnapshot()
    try:
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _InvalidSnapshot() from exc
    for field in ("accounts", "channels", "risks"):
        if not isinstance(data[field], list):
            raise _InvalidSnapshot()
    canonical = {field: data[field] for field in ("schema_version", "accounts", "channels", "risks")}
    try:
        encoded = json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _InvalidSnapshot() from exc
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise _InvalidSnapshot()
    if previous is not None and previous.revision == revision and previous.content_sha256 != digest:
        raise _InvalidSnapshot()

    accounts: dict[str, Account] = {}
    execution_accounts: dict[str, Account] = {}
    for raw_account in data["accounts"]:
        row = _check_fields(raw_account, _ACCOUNT_FIELDS)
        account_id = _safe_id(row["account_id"])
        execution_id = _safe_id(row["execution_account_id"])
        kind = row["kind"]
        parent = row["parent_account_id"]
        if kind not in ("main", "sub") or type(row["enabled"]) is not bool:
            raise _InvalidSnapshot()
        if parent is not None:
            parent = _safe_id(parent)
        if (kind == "main" and parent is not None) or (kind == "sub" and parent is None):
            raise _InvalidSnapshot()
        addon = _number(row["risk_capital_addon"], allow_zero=True)
        default = row["default_risk"]
        if default is not None:
            default = _number(default, allow_zero=False)
            if default > 0.1:
                raise _InvalidSnapshot()
        if account_id in accounts or execution_id in execution_accounts:
            raise _InvalidSnapshot()
        account = Account(account_id, kind, parent, execution_id, row["enabled"], addon, default)
        accounts[account_id] = account
        execution_accounts[execution_id] = account
    for account in accounts.values():
        if account.kind == "sub":
            parent = accounts.get(account.parent_account_id)
            if parent is None or parent.kind != "main":
                raise _InvalidSnapshot()

    channels: dict[str, str] = {}
    for raw_channel in data["channels"]:
        row = _check_fields(raw_channel, _CHANNEL_FIELDS)
        channel_id = _safe_id(row["channel_id"])
        target = _safe_id(row["target_account_id"])
        if channel_id in channels or target not in accounts:
            raise _InvalidSnapshot()
        channels[channel_id] = target

    risks: dict[str, float] = {}
    for raw_risk in data["risks"]:
        row = _check_fields(raw_risk, _RISK_FIELDS)
        symbol = _safe_id(row["symbol"])
        ratio = _number(row["risk_ratio"], allow_zero=False)
        if ratio > 0.1 or symbol in risks:
            raise _InvalidSnapshot()
        risks[symbol] = ratio
    if list(accounts) != sorted(accounts) or list(channels) != sorted(channels) or list(risks) != sorted(risks):
        raise _InvalidSnapshot()
    if previous is not None and revision < previous.revision:
        _LOG.warning("snapshot_revision_regressed old=%d new=%d", previous.revision, revision)
    return ConfigSnapshot(
        revision, digest, MappingProxyType(accounts), MappingProxyType(execution_accounts),
        MappingProxyType(channels), MappingProxyType(risks),
    )


def _http_fetch(url: str, token: str, timeout: float) -> tuple[int, object]:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise OSError("snapshot URL is invalid")
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    deadline = time.monotonic() + timeout
    try:
        connection.request("GET", path, headers={"Authorization": f"Bearer {token}"})
        if time.monotonic() >= deadline:
            raise TimeoutError()
        if connection.sock is not None:
            connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
        response = connection.getresponse()
        if response.status != 200:
            return response.status, None
        chunks = []
        size = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            if connection.sock is not None:
                connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
            chunk = response.read(min(_READ_CHUNK, MAX_SNAPSHOT_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_SNAPSHOT_BYTES:
                raise OSError("snapshot exceeds 4 MiB")
            chunks.append(chunk)
        if time.monotonic() >= deadline:
            raise TimeoutError()
        return 200, json.loads(b"".join(chunks))
    finally:
        connection.close()


class SnapshotCache:
    def __init__(
        self,
        *,
        fetch: Callable[[str, str, float], tuple[int, object]] = _http_fetch,
        clock: Callable[[], float] = time.monotonic,
        url: str | None = None,
        token: str | None = None,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._url = url if url is not None else os.environ.get("WATCHER_SNAPSHOT_URL", DEFAULT_URL)
        configured_token = token if token is not None else os.environ.get("WATCHER_SNAPSHOT_TOKEN", "")
        self._token = configured_token.strip()
        self._lock = threading.RLock()
        self._flight = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: ConfigSnapshot | None = None
        self._verified_at: float | None = None
        self._failures = 0
        self.last_refresh_error: str | None = None
        self._blocked_reason: str | None = None
        self.next_refresh_at = self._clock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._blocked_reason == "unauthorized":
                return "unauthorized"
            if self._blocked_reason in {"invalid", "schema_mismatch"}:
                return "invalid"
            if self._snapshot is None or self._verified_at is None:
                return "cold"
            if self._clock() - self._verified_at > MAX_AGE:
                return "expired"
            return "fresh"

    def require_fresh(self) -> SnapshotLease:
        with self._lock:
            if self.state != "fresh" or self._snapshot is None or self._verified_at is None:
                raise SnapshotUnavailable()
            age_ms = max(0, int((self._clock() - self._verified_at) * 1000))
            return SnapshotLease(self._snapshot, age_ms)

    def refresh(self) -> bool:
        if not self._flight.acquire(blocking=False):
            return False
        try:
            if not self._token.strip():
                raise OSError("snapshot token missing")
            try:
                started_at = time.monotonic()
                status, raw = self._fetch(self._url, self._token, REQUEST_TIMEOUT)
                if time.monotonic() - started_at > REQUEST_TIMEOUT:
                    self._failure("timeout")
                    return False
            except (TimeoutError, socket.timeout):
                self._failure("timeout")
                return False
            except (json.JSONDecodeError, UnicodeError):
                self._failure("unavailable")
                return False
            except (OSError, http.client.HTTPException, ValueError):
                self._failure("unavailable")
                return False
            except Exception:
                self._failure("unavailable")
                return False
            if status in (401, 403):
                self._failure("unauthorized")
                return False
            if status != 200:
                self._failure("unavailable")
                return False
            with self._lock:
                previous = self._snapshot
            try:
                validated = validate(raw, previous)
            except _SchemaMismatch:
                self._failure("schema_mismatch")
                return False
            except (_InvalidSnapshot, TypeError, ValueError):
                self._failure("invalid")
                return False
            with self._lock:
                self._snapshot = validated
                self._verified_at = self._clock()
                self.last_refresh_error = None
                self._blocked_reason = None
                self._failures = 0
                self.next_refresh_at = self._verified_at + REFRESH_INTERVAL
            return True
        except OSError:
            self._failure("unavailable")
            return False
        finally:
            self._flight.release()

    def _failure(self, error: str) -> None:
        with self._lock:
            self.last_refresh_error = error
            if error in {"unauthorized", "invalid", "schema_mismatch"}:
                self._blocked_reason = error
            self._failures += 1
            delay = _BACKOFF[min(self._failures - 1, len(_BACKOFF) - 1)]
            self.next_refresh_at = self._clock() + delay

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
        self.refresh()
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="watcher-config-snapshot", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                delay = max(0.0, self.next_refresh_at - self._clock())
            if self._stop.wait(delay):
                break
            if not self.refresh():
                self._stop.wait(0.05)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=REQUEST_TIMEOUT + 1)


_process_cache: SnapshotCache | None = None
_process_pid: int | None = None
_process_lock = threading.Lock()


def get_process_cache() -> SnapshotCache:
    global _process_cache, _process_pid
    with _process_lock:
        if _process_cache is None or _process_pid != os.getpid():
            _process_cache = SnapshotCache()
            _process_pid = os.getpid()
        return _process_cache


def start_if_enabled() -> None:
    if enabled():
        get_process_cache().start()


def stop_if_started() -> None:
    if _process_cache is not None:
        _process_cache.stop()
