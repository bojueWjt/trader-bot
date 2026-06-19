from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile
from typing import Any

from freqtrade.signal_strategy.domain import MessageType, RiskPolicyResult, SignalStatus, parse_utc_datetime, utc_now
from freqtrade.signal_strategy.freqtrade_positions import get_open_position, position_query_succeeded
from freqtrade.signal_strategy.parser import (
    classify_message_type,
    extract_directives,
    format_signal_id,
    map_pair_to_freqtrade,
    parse_signal,
)
from freqtrade.signal_strategy.risk import RiskPolicy
from freqtrade.signal_strategy.store import make_signal_store_from_url


MAX_WATCHER_ZIP_ENTRIES = 256
MAX_SIGNALS_JSON_BYTES = 1_000_000
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")


@dataclass
class ImportResult:
    total: int = 0
    approved: int = 0
    needs_review: int = 0
    rejected: int = 0


def normalize_watcher_signal(item: dict[str, Any]) -> dict[str, Any]:
    source_channel_id = _first_text(
        item,
        "source_channel_id",
        "chat_id",
        "channel_id",
        "chatId",
        default="manual",
    )
    source_message_id = _first_text(
        item,
        "source_message_id",
        "message_id",
        "msg_id",
        "id",
        default="0",
    )
    signal_id = _first_text(item, "signal_id")
    if not signal_id:
        signal_id = format_signal_id(source_channel_id, source_message_id)

    media = normalize_watcher_media(item.get("media"))
    raw_text = item.get("raw_text")
    if raw_text is None:
        raw_text = item.get("text", "")
    received_at = item.get("date")
    if received_at is None:
        received_at = item.get("received_at")

    message_type = item.get("message_type")
    if message_type is None or message_type == "":
        message_type = classify_message_type(str(raw_text), media)
    else:
        message_type = _message_type_text(message_type)

    return {
        "signal_id": signal_id,
        "source": _first_text(item, "source", default="telegram"),
        "source_channel_id": source_channel_id,
        "source_channel_name": _first_text(
            item,
            "source_channel_name",
            "chat_title",
            "chatTitle",
            "channel_name",
        ),
        "source_message_id": source_message_id,
        "message_type": _message_type_text(message_type),
        "received_at": received_at,
        "raw_text": str(raw_text),
        "media": media,
    }


def normalize_watcher_media(value: Any) -> list[dict[str, str]]:
    if isinstance(value, list):
        media_items = []
        for item in value:
            media_items.extend(normalize_watcher_media(item))
        return media_items

    if not isinstance(value, dict):
        return []

    path = value.get("packaged_path")
    if not path:
        path = value.get("path")
    if not path:
        path = value.get("media_path")
    if not path:
        path = value.get("filename", "")

    return [
        {
            "type": _first_text(value, "type", "media_type"),
            "path": str(path),
            "mime_type": _first_text(value, "mimeType", "mime_type"),
        }
    ]


def import_signal_items(
    items: list[dict[str, Any]],
    store_url: str,
    pair_whitelist: set[str] | None = None,
    approve_parsed: bool = False,
    refresh_window: bool = False,
    risk_policy: RiskPolicy | None = None,
) -> ImportResult:
    store = make_signal_store_from_url(store_url, risk_policy=risk_policy)
    result = ImportResult(total=len(items))
    now = utc_now()

    for item in items:
        raw_message = normalize_watcher_signal(item)
        signal = parse_signal(raw_message, pair_whitelist=pair_whitelist, risk_policy=risk_policy)
        signal.message_type = MessageType(raw_message["message_type"])
        if approve_parsed:
            _apply_auto_approval(signal, now, risk_policy)

        if refresh_window:
            signal.received_at = now
            signal.entry.valid_from = now
            signal.entry.expires_at = now + timedelta(minutes=240)

        position_event_type, position_context = _position_context_for_signal(signal)
        _attach_position_context(signal, position_context)
        store.upsert_signal(signal)
        _record_position_context_event(
            store,
            position_event_type,
            signal.signal_id,
            position_context,
        )
        if signal.message_type == MessageType.UPDATE:
            message_id = _message_id_int(raw_message["source_message_id"])
            directives = extract_directives(raw_message["raw_text"], signal.pair_freqtrade, message_id)
            for directive in directives:
                store.save_directive(directive)

        if signal.status == SignalStatus.APPROVED:
            result.approved += 1
        elif signal.status == SignalStatus.NEEDS_REVIEW:
            result.needs_review += 1
        elif signal.status == SignalStatus.REJECTED:
            result.rejected += 1

    return result


def _position_context_for_signal(signal: Any) -> tuple[str, dict[str, Any]]:
    pair = str(getattr(signal, "pair_freqtrade", "") or "")
    position = None
    query_success = False
    try:
        position = get_open_position(pair)
        query_success = position is not None or position_query_succeeded()
    except Exception:
        position = None
        query_success = False

    context = {
        "pair": pair,
        "has_open_position": position is not None,
        "position": position,
        "query_success": query_success,
    }
    event_type = "position_context_checked" if query_success else "position_context_unknown"
    return event_type, context


def _attach_position_context(signal: Any, context: dict[str, Any]) -> None:
    risk_policy_result = getattr(signal, "risk_policy_result", None)
    if not risk_policy_result:
        return

    details = getattr(risk_policy_result, "details", None)
    if isinstance(details, dict):
        updated_details = dict(details)
    else:
        updated_details = {}
    updated_details["position_context"] = context
    risk_policy_result.details = updated_details


def _record_position_context_event(
    store: Any,
    event_type: str,
    signal_id: str,
    context: dict[str, Any],
) -> None:
    try:
        record_audit_event = getattr(store, "record_audit_event", None)
        if callable(record_audit_event):
            record_audit_event(event_type, signal_id, **context)
            return

        audit_events = getattr(store, "audit_events", None)
        if isinstance(audit_events, list):
            audit_events.append({"type": event_type, "signal_id": signal_id, **context})
    except Exception:
        return


def import_signals_from_path(
    source_path: str | Path,
    store_url: str,
    pair_whitelist: set[str] | None = None,
    approve_parsed: bool = False,
    refresh_window: bool = False,
    risk_policy: RiskPolicy | None = None,
) -> ImportResult:
    items = load_watcher_signals(source_path)
    return import_signal_items(
        items,
        store_url,
        pair_whitelist=pair_whitelist,
        approve_parsed=approve_parsed,
        refresh_window=refresh_window,
        risk_policy=risk_policy,
    )


def _apply_auto_approval(signal: Any, now, risk_policy: RiskPolicy | None = None) -> None:
    if signal.message_type != MessageType.NEW_SIGNAL:
        _mark_rejected(signal, "message_type_not_new_signal")
        return
    if signal.review_reason_codes:
        return
    if signal.stop_loss is None:
        _mark_needs_review(signal, "stop_loss_missing")
        return
    if _is_cmp_market_entry(signal) and not _cmp_signal_is_fresh(signal, now, risk_policy):
        _mark_expired(signal, "cmp_signal_expired")
        return

    signal.status = SignalStatus.APPROVED
    signal.approved_at = now


def _mark_rejected(signal: Any, reason_code: str) -> None:
    signal.status = SignalStatus.REJECTED
    signal.review_reason_codes.append(reason_code)
    signal.review_reason_codes = sorted(set(signal.review_reason_codes))
    signal.risk_policy_result = RiskPolicyResult(
        decision="blocked",
        reason_codes=[reason_code],
    )


def _mark_needs_review(signal: Any, reason_code: str) -> None:
    signal.status = SignalStatus.NEEDS_REVIEW
    signal.review_reason_codes.append(reason_code)
    signal.review_reason_codes = sorted(set(signal.review_reason_codes))
    signal.risk_policy_result = RiskPolicyResult(
        decision="blocked",
        reason_codes=["manual_review_required"],
    )


def _mark_expired(signal: Any, reason_code: str) -> None:
    signal.status = SignalStatus.EXPIRED
    signal.review_reason_codes.append(reason_code)
    signal.review_reason_codes = sorted(set(signal.review_reason_codes))
    signal.risk_policy_result = RiskPolicyResult(
        decision="blocked",
        reason_codes=[reason_code],
    )


def _is_cmp_market_entry(signal: Any) -> bool:
    mode = str(getattr(signal.entry, "mode", ""))
    return mode.startswith("cmp") and getattr(signal.entry, "primary_price", None) is None


def _cmp_signal_is_fresh(signal: Any, now, risk_policy: RiskPolicy | None = None) -> bool:
    received_at = parse_utc_datetime(signal.received_at)
    active_now = parse_utc_datetime(now)
    active_policy = risk_policy or RiskPolicy()
    return active_now - received_at <= timedelta(minutes=active_policy.cmp_max_age_minutes)


def load_watcher_signals(source_path: str | Path) -> list[dict[str, Any]]:
    path = Path(source_path)
    if path.suffix == ".zip":
        return load_watcher_signals_zip(path)

    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return ensure_signal_list(data)


def load_watcher_signals_zip(path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        signal_info = find_signals_json(archive)
        if signal_info.file_size > MAX_SIGNALS_JSON_BYTES:
            raise ValueError("signals.json is too large")
        data = json.loads(archive.read(signal_info))
    return ensure_signal_list(data)


def find_signals_json(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    entries = archive.infolist()
    if len(entries) > MAX_WATCHER_ZIP_ENTRIES:
        raise ValueError("too many zip entries")

    signal_entries = []
    for entry in entries:
        validate_zip_entry_path(entry.filename)
        posix_path = PurePosixPath(entry.filename)
        if posix_path.name == "signals.json":
            signal_entries.append(entry)

    if len(signal_entries) > 1:
        raise ValueError("multiple signals.json files found in archive")
    if signal_entries:
        return signal_entries[0]
    raise FileNotFoundError("signals.json not found in archive")


def validate_zip_entry_path(name: str) -> None:
    if not name:
        raise ValueError("unsafe zip entry path")
    if "\\" in name:
        raise ValueError("unsafe zip entry path")
    if _WINDOWS_DRIVE_PATH_RE.match(name):
        raise ValueError("unsafe zip entry path")

    posix_path = PurePosixPath(name)
    if posix_path.is_absolute():
        raise ValueError("unsafe zip entry path")
    if ".." in posix_path.parts:
        raise ValueError("unsafe zip entry path")


def ensure_signal_list(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        if isinstance(data, dict):
            return [data]
        raise ValueError("signals payload must be a list or object")
    signals = []
    for item in data:
        if isinstance(item, dict):
            signals.append(item)
    return signals


def load_watcher_signals_stdin(payload: str | None = None) -> list[dict[str, Any]]:
    if payload is None:
        payload = sys.stdin.read()
    data = json.loads(payload)
    return ensure_signal_list(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import Telegram watcher signals into Signal Store")
    parser.add_argument("source_path", nargs="?")
    parser.add_argument("--store-url", required=True)
    parser.add_argument("--pair-whitelist", default="")
    parser.add_argument("--approve-parsed", action="store_true")
    parser.add_argument("--refresh-window", action="store_true")
    parser.add_argument("--stdin", action="store_true")
    args = parser.parse_args(argv)

    whitelist = parse_pair_whitelist(args.pair_whitelist)
    stdin_payload = ""
    try:
        if args.stdin or not sys.stdin.isatty():
            stdin_payload = sys.stdin.read()
    except OSError:
        if args.stdin:
            raise

    if stdin_payload.strip():
        items = load_watcher_signals_stdin(stdin_payload)
        result = import_signal_items(
            items,
            args.store_url,
            pair_whitelist=whitelist,
            approve_parsed=args.approve_parsed,
            refresh_window=args.refresh_window,
        )
    else:
        if args.stdin:
            parser.error("stdin payload is required")
        if not args.source_path:
            parser.error("source_path is required unless --stdin is used")
        # Legacy CLI file path for pre-M0-02 callers.
        result = import_signals_from_path(
            args.source_path,
            args.store_url,
            pair_whitelist=whitelist,
            approve_parsed=args.approve_parsed,
            refresh_window=args.refresh_window,
        )
    print(
        json.dumps(
            {
                "approved": result.approved,
                "needs_review": result.needs_review,
                "rejected": result.rejected,
                "total": result.total,
            },
            sort_keys=True,
        )
    )
    return 0


def parse_pair_whitelist(raw_value: str) -> set[str]:
    if not raw_value:
        return set()
    pairs = set()
    for item in raw_value.split(","):
        pair = item.strip()
        if not pair:
            continue
        pairs.add(map_pair_to_freqtrade(pair))
    return pairs


def _first_text(item: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        return str(value)
    return default


def _message_type_text(value: Any) -> str:
    if value is None or value == "":
        return MessageType.NEW_SIGNAL.value
    enum_value = getattr(value, "value", False)
    if enum_value is not False:
        value = enum_value
    return MessageType(str(value)).value


def _message_id_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
