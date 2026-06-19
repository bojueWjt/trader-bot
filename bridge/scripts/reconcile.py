from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


WATCHED_SIGNAL_STATUSES = ["sent_to_freqtrade", "entered", "partially_exited"]
TRADE_REQUIRED_STATUSES = {"entered", "partially_exited"}
DEFAULT_FREQTRADE_API_URL = "http://freqtrade-dryrun:8080"
DEFAULT_REPORT_PATH = Path(os.environ.get("RECONCILE_REPORT_PATH", "/data/reconcile-latest.json"))

logger = logging.getLogger("reconcile")


class FreqtradeClient:
    def __init__(
        self,
        base_url: str = DEFAULT_FREQTRADE_API_URL,
        username: str = "",
        password: str = "",
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout

    def list_open_trades(self) -> list[dict[str, Any]]:
        payload = self._get_json("/api/v1/trades")
        return _extract_items(payload, "trades")

    def get_trade(self, trade_id: int | str) -> dict[str, Any] | None:
        payload = self._get_json(f"/api/v1/trade/{trade_id}")
        if isinstance(payload, dict):
            for key in ("trade", "data", "result"):
                value = payload.get(key)
                if isinstance(value, dict):
                    return value
            return payload
        return None

    def _get_json(self, path: str) -> Any:
        response = httpx.get(
            f"{self.base_url}{path}",
            auth=self._auth(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def _auth(self) -> tuple[str, str] | None:
        if self.username or self.password:
            return (self.username, self.password)
        return None


class SignalStoreClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def list_signals(self, statuses: list[str]) -> list[dict[str, Any]]:
        if self.base_url.startswith("sqlite:"):
            return self._list_signals_sqlite(statuses)
        response = httpx.get(
            self._signals_url(),
            params=[("status", status) for status in statuses],
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        signals = _extract_items(payload, "signals")
        return [signal for signal in signals if signal.get("status") in set(statuses)]

    def _list_signals_sqlite(self, statuses: list[str]) -> list[dict[str, Any]]:
        import sqlite3

        db_path = self.base_url
        for prefix in ("sqlite:////", "sqlite:///", "sqlite://"):
            if db_path.startswith(prefix):
                db_path = "/" + db_path[len(prefix):] if prefix == "sqlite:////" else db_path[len(prefix):]
                break
        placeholders = ",".join("?" for _ in statuses)
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=self.timeout)
        try:
            rows = connection.execute(
                f"SELECT signal_id, status, pair FROM signals WHERE status IN ({placeholders})",
                statuses,
            ).fetchall()
        finally:
            connection.close()
        return [
            {"signal_id": row[0], "status": row[1], "pair": row[2]}
            for row in rows
        ]

    def _signals_url(self) -> str:
        parsed = urlparse(self.base_url)
        if parsed.path and parsed.path != "/":
            return self.base_url
        return f"{self.base_url}/api/signals"


def main(
    env: dict[str, str] | None = None,
    output_path: str | Path = DEFAULT_REPORT_PATH,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    env = os.environ if env is None else env

    freqtrade = FreqtradeClient(
        base_url=env.get("FREQTRADE_API_URL", DEFAULT_FREQTRADE_API_URL),
        username=env.get("FREQTRADE_API_USERNAME", ""),
        password=env.get("FREQTRADE_API_PASSWORD", ""),
    )
    signal_store_url = env.get("SIGNAL_STORE_URL", "")
    signal_store = SignalStoreClient(signal_store_url) if signal_store_url else None

    discrepancies: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    open_trades: list[dict[str, Any]] = []

    if signal_store is None:
        logger.error("SIGNAL_STORE_URL is required for reconciliation")
    else:
        signals = signal_store.list_signals(WATCHED_SIGNAL_STATUSES)

    try:
        open_trades = freqtrade.list_open_trades()
    except Exception as exc:  # noqa: BLE001 - network clients can raise several exception types
        discrepancies.append(
            {
                "type": "api_unreachable",
                "severity": "alert",
                "message": str(exc),
            }
        )

    if not any(discrepancy["type"] == "api_unreachable" for discrepancy in discrepancies):
        discrepancies.extend(_reconcile_signals(signals, open_trades, freqtrade))
        discrepancies.extend(_find_orphan_trades(signals, open_trades))

    report = _build_report(signals, open_trades, discrepancies)
    _write_report(report, Path(output_path))
    print(json.dumps(report, sort_keys=True))

    if discrepancies:
        send_telegram_alert(env, report)
        return 2
    return 0


def _reconcile_signals(
    signals: list[dict[str, Any]],
    open_trades: list[dict[str, Any]],
    freqtrade: FreqtradeClient,
) -> list[dict[str, Any]]:
    open_signal_ids = {
        signal_id
        for signal_id in (_signal_id_from_enter_tag(trade.get("enter_tag")) for trade in open_trades)
        if signal_id
    }
    discrepancies: list[dict[str, Any]] = []

    for signal in signals:
        status = str(signal.get("status", ""))
        if status not in TRADE_REQUIRED_STATUSES:
            continue
        signal_id = str(signal.get("signal_id", ""))
        if not signal_id or signal_id in open_signal_ids:
            continue
        trade_id = signal.get("trade_id")
        if trade_id not in (None, "") and freqtrade.get_trade(trade_id):
            continue
        discrepancies.append(
            {
                "type": "missing_trade",
                "severity": "warning",
                "signal_id": signal_id,
                "status": status,
            }
        )

    return discrepancies


def _find_orphan_trades(
    signals: list[dict[str, Any]],
    open_trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    signal_ids = {str(signal.get("signal_id", "")) for signal in signals}
    discrepancies: list[dict[str, Any]] = []

    for trade in open_trades:
        enter_tag = str(trade.get("enter_tag", "") or "")
        signal_id = _signal_id_from_enter_tag(enter_tag)
        if not signal_id or signal_id in signal_ids:
            continue
        discrepancies.append(
            {
                "type": "orphan_trade",
                "severity": "warning",
                "trade_id": _trade_id(trade),
                "signal_id": signal_id,
                "enter_tag": enter_tag,
            }
        )

    return discrepancies


def _build_report(
    signals: list[dict[str, Any]],
    open_trades: list[dict[str, Any]],
    discrepancies: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "signals_checked": len(signals),
        "open_trades_checked": len(open_trades),
        "discrepancies": discrepancies,
    }


def _write_report(report: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def send_telegram_alert(env: dict[str, str], report: dict[str, Any]) -> None:
    token = env.get("RECONCILE_ALERT_BOT_TOKEN", "")
    chat_id = env.get("RECONCILE_ALERT_CHAT_ID", "")
    if not token or not chat_id:
        logger.error("Reconciliation discrepancies found but Telegram alert env vars are absent")
        return

    text = _alert_text(report)
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10.0,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - alerting must not mask reconcile result
        logger.error("Telegram reconciliation alert failed: %s", exc)


def _alert_text(report: dict[str, Any]) -> str:
    discrepancies = report["discrepancies"]
    lines = [
        "Trader Bridge reconciliation discrepancies",
        f"Count: {len(discrepancies)}",
    ]
    for discrepancy in discrepancies[:10]:
        lines.append(json.dumps(discrepancy, sort_keys=True))
    return "\n".join(lines)


def _signal_id_from_enter_tag(enter_tag: object) -> str:
    if not isinstance(enter_tag, str):
        return ""
    if not enter_tag.startswith("sig:"):
        return ""
    return enter_tag[4:]


def _trade_id(trade: dict[str, Any]) -> int | str:
    return trade.get("trade_id") or trade.get("id") or trade.get("tradeid") or ""


def _extract_items(payload: Any, preferred_key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (preferred_key, "data", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_items(value, preferred_key)
            if nested:
                return nested
    return []


if __name__ == "__main__":
    sys.exit(main())
