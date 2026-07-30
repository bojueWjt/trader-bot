#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import secrets
import shutil
import sys
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover - render-sample must work without DB deps.
    psycopg2 = None
    RealDictCursor = None

try:
    from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
    from fastapi.responses import JSONResponse
except Exception:  # pragma: no cover - allows CLI sample rendering without FastAPI.
    Depends = FastAPI = File = Header = HTTPException = UploadFile = None
    JSONResponse = None


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "template.html"
ECHARTS_PATH = BASE_DIR / "static" / "echarts.min.js"
MAX_ASSET_BYTES = 10 * 1024 * 1024
VALID_STANCES = {"bullish", "bearish", "neutral", "mixed"}
SECTION_KEYS = ("overview_md", "market_md", "risk_md", "actions_md")
SECTION_TITLES = {
    "overview_md": "概览",
    "market_md": "行情",
    "risk_md": "风险",
    "actions_md": "操作",
}
ASSET_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
OUTCOME_JOB_NAME = "trade_outcomes"
OUTCOME_JOB_STATUS = "succeeded"
OUTCOME_FRESHNESS_ENV = "REPORT_OUTCOME_FRESHNESS_HOURS"
DEFAULT_OUTCOME_FRESHNESS_HOURS = 36.0
EXCHANGE_MIRROR_FRESHNESS_ENV = "REPORT_EXCHANGE_MIRROR_FRESHNESS_SECONDS"
DEFAULT_EXCHANGE_MIRROR_FRESHNESS_SECONDS = 180.0


class ReportValidationError(ValueError):
    pass


class ReportDependencyError(RuntimeError):
    def __init__(self, dependency: str, reason: str):
        self.dependency = dependency
        self.reason = reason
        super().__init__(reason)


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_report_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ReportValidationError("date must be YYYY-MM-DD") from exc


def window_bounds(report_type: str, report_date: str) -> tuple[datetime, datetime]:
    end_date = parse_report_date(report_date) + timedelta(days=1)
    days = 1 if report_type == "daily" else 7
    end = datetime.combine(end_date, time.min, tzinfo=timezone.utc)
    # A report generated intraday must cover the trailing window ending now,
    # not the not-yet-finished calendar day: otherwise trades closing after
    # generation time never appear in any report (2026-07-13 weekly missed
    # 07-06 outcomes because "today+1" pushed the 7-day window forward).
    now = datetime.now(timezone.utc)
    if end > now:
        end = now
    return end - timedelta(days=days), end


def render_inline(text: str) -> str:
    escaped = html.escape(str(text), quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def render_markdown(markdown: str | None) -> str:
    if not markdown:
        return ""
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(f"<p>{render_inline(' '.join(paragraph))}</p>")
            paragraph = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            blocks.append("<ul>" + "".join(f"<li>{render_inline(item)}</li>" for item in list_items) + "</ul>")
            list_items = []

    for raw_line in str(markdown).splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_list()
            continue
        if line.startswith("- ") or line.startswith("* "):
            flush_paragraph()
            list_items.append(line[2:].strip())
        else:
            flush_list()
            paragraph.append(line)

    flush_paragraph()
    flush_list()
    return "\n".join(blocks)


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReportValidationError("request body must be a JSON object")
    report_type = payload.get("type")
    if report_type not in {"daily", "weekly"}:
        raise ReportValidationError("type must be daily or weekly")

    report_date = payload.get("date") or utc_today()
    parse_report_date(report_date)

    sections = payload.get("sections") or {}
    if not isinstance(sections, dict):
        raise ReportValidationError("sections must be an object")
    normalized_sections = {}
    sections_html = []
    for key in SECTION_KEYS:
        value = sections.get(key)
        if value is not None and not isinstance(value, str):
            raise ReportValidationError(f"sections.{key} must be a string")
        normalized_sections[key] = value or ""
        rendered = render_markdown(value)
        if rendered:
            sections_html.append({"key": key, "title": SECTION_TITLES[key], "html": rendered})

    channel_views = payload.get("channel_views") or []
    if not isinstance(channel_views, list):
        raise ReportValidationError("channel_views must be a list")
    normalized_views = []
    for idx, view in enumerate(channel_views):
        if not isinstance(view, dict):
            raise ReportValidationError(f"channel_views[{idx}] must be an object")
        channel = str(view.get("channel") or "").strip()
        stance = str(view.get("stance") or "").strip()
        summary = str(view.get("summary") or "").strip()
        if not channel or not summary or stance not in VALID_STANCES:
            raise ReportValidationError(
                f"channel_views[{idx}] requires channel, summary, and stance in {sorted(VALID_STANCES)}"
            )
        symbols = view.get("symbols") or []
        if not isinstance(symbols, list):
            raise ReportValidationError(f"channel_views[{idx}].symbols must be a list")
        normalized_views.append(
            {
                "channel": channel,
                "trader": str(view.get("trader") or "").strip(),
                "stance": stance,
                "summary": summary,
                "symbols": [str(s) for s in symbols if str(s).strip()],
            }
        )

    images = payload.get("images") or []
    if not isinstance(images, list):
        raise ReportValidationError("images must be a list")
    normalized_images = []
    for idx, image in enumerate(images):
        if not isinstance(image, dict) or not image.get("url"):
            raise ReportValidationError(f"images[{idx}] requires url")
        normalized_images.append({"url": str(image["url"]), "caption": str(image.get("caption") or "")})

    extra_metrics = payload.get("extra_metrics") or []
    if not isinstance(extra_metrics, list):
        raise ReportValidationError("extra_metrics must be a list")
    normalized_metrics = []
    for idx, metric in enumerate(extra_metrics):
        if not isinstance(metric, dict) or not metric.get("label"):
            raise ReportValidationError(f"extra_metrics[{idx}] requires label")
        normalized_metrics.append(
            {
                "label": str(metric["label"]),
                "value": str(metric.get("value", "")),
                "hint": str(metric.get("hint") or ""),
            }
        )

    title = payload.get("title") or f"Hermes {report_type.title()} Report"
    return {
        "type": report_type,
        "date": report_date,
        "title": str(title),
        "sections": normalized_sections,
        "sections_html": sections_html,
        "channel_views": normalized_views,
        "images": normalized_images,
        "extra_metrics": normalized_metrics,
    }


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def json_for_script(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, default=json_default, separators=(",", ":"))
    return text.replace("</", "<\\/")


def empty_db_data() -> dict[str, Any]:
    return {
        "window": {"start": None, "end": None},
        "kpis": {"period_pnl": 0, "win_rate": None, "trade_count": 0, "avg_r": None},
        "outcomes": {
            "trades": [],
            "cumulative_pnl": [],
            "daily_pnl": [],
            "r_distribution": [],
            "symbol_pnl": [],
        },
        "intents": {"activity": []},
        "positions": [],
        "missing_data": [],
    }


def empty_dependency_status() -> dict[str, dict[str, Any]]:
    return {
        "database": {
            "status": "unknown",
            "reason": "",
        },
        "trade_outcomes": {
            "status": "unknown",
            "reason": "",
            "job_name": OUTCOME_JOB_NAME,
            "required_status": OUTCOME_JOB_STATUS,
            "completed_at": "",
            "age_seconds": False,
            "freshness_threshold_seconds": False,
        },
        "exchange_state_mirror": {
            "status": "unknown",
            "reason": "",
            "updated_at": "",
            "age_seconds": False,
            "freshness_threshold_seconds": False,
        },
    }


def service_status(
    dependencies: dict[str, dict[str, Any]],
    last_publication: dict[str, Any] | None = None,
) -> str:
    database = dependencies.get("database") or {}
    trade_outcomes = dependencies.get("trade_outcomes") or {}
    mirror = dependencies.get("exchange_state_mirror") or {}
    if database.get("status") != "ok":
        return "unhealthy"
    if trade_outcomes.get("status") != "ok":
        return "unhealthy"
    if mirror.get("status") != "ok":
        return "degraded"
    publication = last_publication or {}
    failure_code = publication.get("error_code")
    if publication.get("status") == "failed" and failure_code != "invalid_report":
        return "degraded"
    return "ok"


def initial_health_state() -> dict[str, Any]:
    dependencies = empty_dependency_status()
    return {
        "status": "starting",
        "checked_at": "",
        "dependencies": dependencies,
        "last_publication": {
            "status": "never",
            "attempted_at": "",
            "report_type": "",
            "report_date": "",
            "url": "",
            "error_code": "",
            "reason": "",
        },
    }


def outcome_freshness_threshold() -> timedelta:
    raw_value = os.getenv(OUTCOME_FRESHNESS_ENV, str(DEFAULT_OUTCOME_FRESHNESS_HOURS))
    try:
        hours = float(raw_value)
    except (TypeError, ValueError) as exc:
        reason = f"{OUTCOME_FRESHNESS_ENV} must be a positive number"
        raise ReportDependencyError("trade_outcomes", reason) from exc
    if not math.isfinite(hours) or hours <= 0:
        reason = f"{OUTCOME_FRESHNESS_ENV} must be a positive number"
        raise ReportDependencyError("trade_outcomes", reason)
    return timedelta(hours=hours)


def exchange_mirror_freshness_threshold() -> timedelta:
    raw_value = os.getenv(
        EXCHANGE_MIRROR_FRESHNESS_ENV,
        str(DEFAULT_EXCHANGE_MIRROR_FRESHNESS_SECONDS),
    )
    try:
        seconds = float(raw_value)
    except (TypeError, ValueError) as exc:
        reason = f"{EXCHANGE_MIRROR_FRESHNESS_ENV} must be a positive number"
        raise ReportDependencyError("exchange_state_mirror", reason) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        reason = f"{EXCHANGE_MIRROR_FRESHNESS_ENV} must be a positive number"
        raise ReportDependencyError("exchange_state_mirror", reason)
    return timedelta(seconds=seconds)


def normalize_utc_datetime(value: Any) -> datetime:
    completed_at = value
    if isinstance(completed_at, str):
        normalized = completed_at.replace("Z", "+00:00")
        try:
            completed_at = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ReportDependencyError("trade_outcomes", "completed_at is not a valid timestamp") from exc
    if not isinstance(completed_at, datetime):
        raise ReportDependencyError("trade_outcomes", "completed_at is missing")
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    return completed_at.astimezone(timezone.utc)


def normalize_exchange_mirror_updated_at(value: Any) -> datetime:
    updated_at = value
    if isinstance(updated_at, str):
        normalized = updated_at.replace("Z", "+00:00")
        try:
            updated_at = datetime.fromisoformat(normalized)
        except ValueError as exc:
            reason = "updated_at is not a valid timestamp"
            raise ReportDependencyError("exchange_state_mirror", reason) from exc
    if not isinstance(updated_at, datetime):
        reason = "updated_at is missing"
        raise ReportDependencyError("exchange_state_mirror", reason)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return updated_at.astimezone(timezone.utc)


def exchange_mirror_rows_are_fresh(
    rows: list[dict[str, Any]],
    dependencies: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> bool:
    current_time = now or utc_now()
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_time = current_time.astimezone(timezone.utc)
    dependency = dependencies["exchange_state_mirror"]
    try:
        freshness = exchange_mirror_freshness_threshold()
    except ReportDependencyError as exc:
        dependency.update(
            {
                "status": "error",
                "reason": exc.reason,
            }
        )
        return False
    freshness_seconds = int(freshness.total_seconds())
    dependency["freshness_threshold_seconds"] = freshness_seconds
    if not rows:
        dependency.update(
            {
                "status": "missing",
                "reason": "exchange_state_mirror has no account snapshots",
                "updated_at": "",
                "age_seconds": False,
            }
        )
        return False

    oldest_account_id = ""
    oldest_updated_at = current_time
    for row in rows:
        account_id = str(row.get("account_id") or "unknown")
        try:
            updated_at = normalize_exchange_mirror_updated_at(row.get("updated_at"))
        except ReportDependencyError as exc:
            reason = f"exchange_state_mirror {exc.reason}: account_id={account_id}"
            dependency.update(
                {
                    "status": "missing",
                    "reason": reason,
                    "updated_at": "",
                    "age_seconds": False,
                }
            )
            return False
        if not oldest_account_id or updated_at < oldest_updated_at:
            oldest_account_id = account_id
            oldest_updated_at = updated_at

    age_seconds = max(0, int((current_time - oldest_updated_at).total_seconds()))
    if current_time - oldest_updated_at > freshness:
        reason = (
            f"exchange_state_mirror is stale: account_id={oldest_account_id}, "
            f"updated_at={oldest_updated_at.isoformat()}, age_seconds={age_seconds}, "
            f"threshold_seconds={freshness_seconds}"
        )
        dependency.update(
            {
                "status": "stale",
                "reason": reason,
                "updated_at": oldest_updated_at.isoformat(),
                "age_seconds": age_seconds,
            }
        )
        return False

    dependency.update(
        {
            "status": "ok",
            "reason": "",
            "updated_at": oldest_updated_at.isoformat(),
            "age_seconds": age_seconds,
        }
    )
    return True


def connect_database(
    database_url: str | None,
    dependencies: dict[str, dict[str, Any]],
) -> Any:
    dsn = database_url or os.getenv("DATABASE_URL")
    if not dsn:
        reason = "DATABASE_URL is not set"
        dependencies["database"] = {"status": "error", "reason": reason}
        raise ReportDependencyError("database", reason)
    if psycopg2 is None:
        reason = "psycopg2 is not installed"
        dependencies["database"] = {"status": "error", "reason": reason}
        raise ReportDependencyError("database", reason)
    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:
        reason = f"database connection failed: {exc}"
        dependencies["database"] = {"status": "error", "reason": reason}
        raise ReportDependencyError("database", reason) from exc
    conn.autocommit = True
    dependencies["database"] = {"status": "ok", "reason": ""}
    return conn


def fetch_all(cursor: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cursor.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def safe_query(cursor: Any, sql: str, params: tuple[Any, ...], missing: list[str], label: str) -> list[dict[str, Any]]:
    try:
        return fetch_all(cursor, sql, params)
    except Exception as exc:
        missing.append(f"{label}: {exc}")
        return []


def require_fresh_outcome_watermark(
    cursor: Any,
    dependencies: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> datetime:
    current_time = now or utc_now()
    try:
        freshness = outcome_freshness_threshold()
    except ReportDependencyError as exc:
        dependencies["trade_outcomes"].update(
            {
                "status": "error",
                "reason": exc.reason,
            }
        )
        raise
    freshness_seconds = int(freshness.total_seconds())
    dependencies["trade_outcomes"]["freshness_threshold_seconds"] = freshness_seconds
    try:
        rows = fetch_all(
            cursor,
            """
            SELECT completed_at
            FROM trade_outcome_job_runs
            WHERE job_name = %s AND status = %s
            ORDER BY completed_at DESC NULLS LAST
            LIMIT 1
            """,
            (OUTCOME_JOB_NAME, OUTCOME_JOB_STATUS),
        )
    except Exception as exc:
        reason = f"watermark query failed: {exc}"
        dependencies["trade_outcomes"].update(
            {
                "status": "error",
                "reason": reason,
            }
        )
        raise ReportDependencyError("trade_outcomes", reason) from exc
    if not rows:
        reason = "no succeeded trade_outcomes watermark"
        dependencies["trade_outcomes"].update(
            {
                "status": "missing",
                "reason": reason,
            }
        )
        raise ReportDependencyError("trade_outcomes", reason)
    try:
        completed_at = normalize_utc_datetime(rows[0].get("completed_at"))
    except ReportDependencyError as exc:
        dependencies["trade_outcomes"].update(
            {
                "status": "missing",
                "reason": exc.reason,
            }
        )
        raise
    age_seconds = max(0, int((current_time - completed_at).total_seconds()))
    if current_time - completed_at > freshness:
        reason = (
            f"trade_outcomes watermark is stale: completed_at={completed_at.isoformat()}, "
            f"age_seconds={age_seconds}, threshold_seconds={freshness_seconds}"
        )
        dependencies["trade_outcomes"].update(
            {
                "status": "stale",
                "reason": reason,
                "completed_at": completed_at.isoformat(),
                "age_seconds": age_seconds,
            }
        )
        raise ReportDependencyError("trade_outcomes", reason)
    dependencies["trade_outcomes"].update(
        {
            "status": "ok",
            "reason": "",
            "completed_at": completed_at.isoformat(),
            "age_seconds": age_seconds,
        }
    )
    return completed_at


def bucket_r_values(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = [
        ("≤-2R", None, -2),
        ("-2~-1R", -2, -1),
        ("-1~0R", -1, 0),
        ("0~1R", 0, 1),
        ("1~2R", 1, 2),
        (">2R", 2, None),
    ]
    counts = {label: 0 for label, _, _ in buckets}
    for row in rows:
        value = row.get("r_multiple")
        if value is None:
            continue
        r = float(value)
        for label, low, high in buckets:
            if (low is None or r > low) and (high is None or r <= high):
                counts[label] += 1
                break
    return [{"bucket": label, "count": count} for label, count in counts.items()]


def compute_outcomes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sorted_rows = sorted(rows, key=lambda r: str(r.get("closed_at") or ""))
    cumulative = []
    daily: dict[str, float] = {}
    symbol: dict[str, float] = {}
    total = 0.0
    wins = 0
    r_values = []
    trades = []
    for row in sorted_rows:
        pnl = float(row.get("realized_pnl") or 0)
        total += pnl
        if pnl > 0:
            wins += 1
        closed_at = row.get("closed_at")
        day = closed_at.date().isoformat() if isinstance(closed_at, datetime) else str(closed_at or "")[:10]
        instrument = row.get("instrument_id") or row.get("symbol") or "UNKNOWN"
        daily[day] = daily.get(day, 0.0) + pnl
        symbol[instrument] = symbol.get(instrument, 0.0) + pnl
        if row.get("r_multiple") is not None:
            r_values.append(float(row["r_multiple"]))
        cumulative.append({"time": json_default(closed_at), "pnl": round(total, 6)})
        trades.append(
            {
                "time": json_default(closed_at),
                "symbol": instrument,
                "side": row.get("side") or "",
                "pnl": pnl,
                "r": None if row.get("r_multiple") is None else float(row["r_multiple"]),
            }
        )
    trade_count = len(sorted_rows)
    return {
        "kpis": {
            "period_pnl": round(total, 6),
            "win_rate": None if trade_count == 0 else round(wins / trade_count * 100, 2),
            "trade_count": trade_count,
            "avg_r": None if not r_values else round(sum(r_values) / len(r_values), 4),
        },
        "outcomes": {
            "trades": trades,
            "cumulative_pnl": cumulative,
            "daily_pnl": [{"date": k, "pnl": round(v, 6)} for k, v in sorted(daily.items())],
            "r_distribution": bucket_r_values(sorted_rows),
            "symbol_pnl": [
                {"symbol": k, "pnl": round(v, 6)}
                for k, v in sorted(symbol.items(), key=lambda item: abs(item[1]), reverse=True)
            ],
        },
    }


def normalize_position_payload(payload: Any, account_id: str, updated_at: Any) -> list[dict[str, Any]]:
    positions = []
    if not isinstance(payload, dict):
        return positions
    for item in payload.get("positions") or []:
        qty = item.get("position_amt", item.get("quantity", item.get("qty")))
        try:
            if abs(float(qty or 0)) == 0:
                continue
        except (TypeError, ValueError):
            pass
        positions.append(
            {
                "account_id": account_id,
                "symbol": item.get("symbol") or item.get("instrument_id") or "",
                "side": (item.get("position_side") or item.get("side") or "").lower(),
                "quantity": qty,
                "entry_price": item.get("entry_price") or item.get("avg_entry_price"),
                "mark_price": item.get("mark_price"),
                "unrealized_pnl": item.get("unrealized_pnl"),
                "updated_at": updated_at,
                "source": "exchange_state_mirror",
            }
        )
    return positions


def fetch_report_data(
    report_type: str,
    report_date: str,
    database_url: str | None = None,
    dependency_status: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    data = empty_db_data()
    dependencies = dependency_status
    if dependencies is None:
        dependencies = empty_dependency_status()
    start, end = window_bounds(report_type, report_date)
    data["window"] = {"start": start.isoformat(), "end": end.isoformat()}
    conn = connect_database(database_url, dependencies)

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            require_fresh_outcome_watermark(cur, dependencies, now=now)
            try:
                outcome_rows = fetch_all(
                    cur,
                    """
                    SELECT closed_at, instrument_id, side, realized_pnl, r_multiple
                    FROM trade_outcomes
                    WHERE closed_at >= %s AND closed_at < %s
                    ORDER BY closed_at ASC
                    """,
                    (start, end),
                )
            except Exception as exc:
                reason = f"trade_outcomes query failed: {exc}"
                dependencies["trade_outcomes"].update(
                    {
                        "status": "error",
                        "reason": reason,
                    }
                )
                raise ReportDependencyError("trade_outcomes", reason) from exc
            computed = compute_outcomes(outcome_rows)
            data["kpis"].update(computed["kpis"])
            data["outcomes"].update(computed["outcomes"])

            intent_rows = safe_query(
                cur,
                """
                SELECT action::text AS action, status::text AS status, count(*)::int AS count
                FROM trade_intents
                WHERE created_at >= %s AND created_at < %s
                GROUP BY action::text, status::text
                ORDER BY count DESC, action ASC
                """,
                (start, end),
                data["missing_data"],
                "trade_intents activity",
            )
            data["intents"]["activity"] = intent_rows

            try:
                mirror_rows = fetch_all(
                    cur,
                    "SELECT account_id, payload, updated_at FROM exchange_state_mirror ORDER BY account_id",
                )
                mirror_fresh = exchange_mirror_rows_are_fresh(
                    mirror_rows,
                    dependencies,
                    now=now,
                )
                if not mirror_fresh:
                    data["missing_data"].append(dependencies["exchange_state_mirror"]["reason"])
            except Exception as exc:
                reason = f"exchange_state_mirror: {exc}"
                data["missing_data"].append(reason)
                dependencies["exchange_state_mirror"] = {
                    "status": "error",
                    "reason": reason,
                }
                mirror_rows = []
                mirror_fresh = False
            positions = []
            if mirror_rows and mirror_fresh:
                for row in mirror_rows:
                    positions.extend(normalize_position_payload(row.get("payload"), row.get("account_id"), row.get("updated_at")))
            data["positions"] = positions
    finally:
        conn.close()
    return data


def inspect_dependencies(
    database_url: str | None = None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    dependencies = empty_dependency_status()
    try:
        conn = connect_database(database_url, dependencies)
    except ReportDependencyError:
        return dependencies
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                require_fresh_outcome_watermark(cur, dependencies, now=now)
            except ReportDependencyError:
                pass
            try:
                mirror_rows = fetch_all(
                    cur,
                    "SELECT account_id, updated_at FROM exchange_state_mirror ORDER BY account_id",
                )
                exchange_mirror_rows_are_fresh(
                    mirror_rows,
                    dependencies,
                    now=now,
                )
            except Exception as exc:
                reason = f"exchange_state_mirror: {exc}"
                dependencies["exchange_state_mirror"] = {
                    "status": "error",
                    "reason": reason,
                }
    finally:
        conn.close()
    return dependencies


def render_report_html(payload: dict[str, Any], db_data: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    echarts = ECHARTS_PATH.read_text(encoding="utf-8")
    data = {
        "report": payload,
        "db": db_data,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return template.replace("/*__ECHARTS__*/", echarts).replace("/*__REPORT_DATA__*/", json_for_script(data))


def report_dir_from_env() -> Path:
    return Path(os.getenv("REPORT_DIR", str(Path.cwd() / "reports_out"))).resolve()


def public_base() -> str:
    return os.getenv("PUBLIC_BASE", "https://hk.balen.wang").rstrip("/")


def write_report(payload: dict[str, Any], db_data: dict[str, Any], report_dir: Path | None = None) -> dict[str, str]:
    out_dir = report_dir or report_dir_from_env()
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{payload['date']}-{payload['type']}-{secrets.token_hex(4)}.html"
    path = out_dir / filename
    path.write_text(render_report_html(payload, db_data), encoding="utf-8")
    return {"url": f"{public_base()}/reports/{filename}", "path": str(path)}


def validate_asset(content_type: str | None, size: int) -> str:
    content_type = (content_type or "").split(";")[0].strip().lower()
    if content_type not in ASSET_TYPES:
        raise ReportValidationError("asset content-type must be image/png, image/jpeg, or image/webp")
    if size > MAX_ASSET_BYTES:
        raise ReportValidationError("asset size must be <= 10MB")
    return ASSET_TYPES[content_type]


def sample_payload() -> dict[str, Any]:
    return normalize_payload(
        {
            "type": "daily",
            "date": utc_today(),
            "title": "Hermes Daily Report",
            "sections": {
                "overview_md": "Portfolio closed the period **net positive** with disciplined exposure.",
                "market_md": "- BTC held the upper range\n- ETH lagged BTC momentum\n- SOL remained event-driven",
                "risk_md": "Risk stayed below budget. No forced deleveraging was required.",
                "actions_md": "- Keep BTC stop discipline\n- Review SOL after next liquidity sweep",
            },
            "channel_views": [
                {
                    "channel": "C02-舒琴",
                    "trader": "舒琴",
                    "stance": "mixed",
                    "summary": "BTC bias remains range-bound until a clean break above resistance.",
                    "symbols": ["BTCUSDT", "ETHUSDT"],
                },
                {
                    "channel": "B01-02Titan",
                    "trader": "Titan",
                    "stance": "bullish",
                    "summary": "Prefers pullback longs while funding stays controlled.",
                    "symbols": ["SOLUSDT"],
                },
            ],
            "images": [],
            "extra_metrics": [{"label": "Max Exposure", "value": "42%", "hint": "of policy cap"}],
        }
    )


def sample_db_data() -> dict[str, Any]:
    data = empty_db_data()
    now = datetime.now(timezone.utc)
    trades = [
        {"closed_at": now - timedelta(hours=18), "instrument_id": "BTCUSDT", "side": "long", "realized_pnl": Decimal("26.4"), "r_multiple": Decimal("1.2")},
        {"closed_at": now - timedelta(hours=10), "instrument_id": "ETHUSDT", "side": "short", "realized_pnl": Decimal("-9.1"), "r_multiple": Decimal("-0.7")},
        {"closed_at": now - timedelta(hours=3), "instrument_id": "SOLUSDT", "side": "long", "realized_pnl": Decimal("14.8"), "r_multiple": Decimal("0.9")},
    ]
    computed = compute_outcomes(trades)
    data["window"] = {"start": (now - timedelta(days=1)).isoformat(), "end": now.isoformat()}
    data["kpis"].update(computed["kpis"])
    data["outcomes"].update(computed["outcomes"])
    data["intents"]["activity"] = [
        {"action": "open_position", "status": "approved", "count": 4},
        {"action": "close_position", "status": "approved", "count": 2},
        {"action": "move_stop_loss", "status": "draft", "count": 1},
    ]
    data["positions"] = [
        {
            "account_id": "account-a",
            "symbol": "BTCUSDT",
            "side": "long",
            "quantity": "0.015",
            "entry_price": "61320.5",
            "mark_price": "62110.2",
            "unrealized_pnl": "11.84",
            "updated_at": now.isoformat(),
            "source": "sample",
        }
    ]
    data["missing_data"] = ["sample mode: database was not queried"]
    return data


def create_app() -> Any:
    if FastAPI is None:
        raise RuntimeError("FastAPI is not installed; install services/report/requirements.txt")

    app = FastAPI(title="Hermes Report Service", version="1.0")
    app.state.report_health = initial_health_state()
    app.state.report_health_lock = Lock()

    def update_dependencies(dependencies: dict[str, dict[str, Any]]) -> None:
        with app.state.report_health_lock:
            health = app.state.report_health
            health["dependencies"] = deepcopy(dependencies)
            health["status"] = service_status(dependencies, health["last_publication"])
            health["checked_at"] = utc_now().isoformat()

    def record_publication(
        status: str,
        report_type: str,
        report_date: str,
        url: str = "",
        error_code: str = "",
        reason: str = "",
    ) -> None:
        with app.state.report_health_lock:
            health = app.state.report_health
            health["last_publication"] = {
                "status": status,
                "attempted_at": utc_now().isoformat(),
                "report_type": report_type,
                "report_date": report_date,
                "url": url,
                "error_code": error_code,
                "reason": reason,
            }
            if error_code != "invalid_report":
                health["status"] = service_status(
                    health["dependencies"],
                    health["last_publication"],
                )

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        token = os.getenv("REPORT_TOKEN")
        if not token:
            return
        expected = f"Bearer {token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid report token")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        dependencies = inspect_dependencies()
        update_dependencies(dependencies)
        with app.state.report_health_lock:
            return deepcopy(app.state.report_health)

    @app.post("/reports", dependencies=[Depends(require_auth)])
    def create_report(body: dict[str, Any]) -> dict[str, str]:
        report_type = ""
        report_date = utc_today()
        if isinstance(body, dict):
            report_type = str(body.get("type") or "")
            report_date = str(body.get("date") or report_date)
        try:
            payload = normalize_payload(body)
        except ReportValidationError as exc:
            record_publication(
                "failed",
                report_type,
                report_date,
                error_code="invalid_report",
                reason=str(exc),
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        dependencies = empty_dependency_status()
        try:
            db_data = fetch_report_data(
                payload["type"],
                payload["date"],
                dependency_status=dependencies,
            )
        except ReportDependencyError as exc:
            update_dependencies(dependencies)
            record_publication(
                "failed",
                payload["type"],
                payload["date"],
                error_code="report_dependency_unavailable",
                reason=exc.reason,
            )
            detail = {
                "code": "report_dependency_unavailable",
                "dependency": exc.dependency,
                "reason": exc.reason,
            }
            raise HTTPException(status_code=503, detail=detail) from exc
        update_dependencies(dependencies)
        try:
            result = write_report(payload, db_data)
            url = result.get("url")
            if not url:
                raise RuntimeError("report writer did not return a URL")
        except Exception as exc:
            reason = f"report publication failed: {exc}"
            record_publication(
                "failed",
                payload["type"],
                payload["date"],
                error_code="report_publication_failed",
                reason=reason,
            )
            detail = {
                "code": "report_publication_failed",
                "reason": reason,
            }
            raise HTTPException(status_code=500, detail=detail) from exc
        record_publication(
            "succeeded",
            payload["type"],
            payload["date"],
            url=url,
        )
        return result

    @app.post("/reports/assets", dependencies=[Depends(require_auth)])
    async def upload_asset(file: UploadFile = File(...)) -> dict[str, str]:
        assets_dir = report_dir_from_env() / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        ext = validate_asset(file.content_type, 0)
        filename = f"{secrets.token_hex(12)}{ext}"
        path = assets_dir / filename
        size = 0
        try:
            with path.open("wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_ASSET_BYTES:
                        raise ReportValidationError("asset size must be <= 10MB")
                    out.write(chunk)
            validate_asset(file.content_type, size)
        except ReportValidationError as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=413 if "size" in str(exc) else 400, detail=str(exc)) from exc
        finally:
            await file.close()
        return {"url": f"{public_base()}/reports/assets/{filename}", "path": str(path)}

    return app


app = create_app() if FastAPI is not None else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes HTML report service")
    parser.add_argument("--render-sample", metavar="OUT.html", help="render a complete sample report without DB access")
    args = parser.parse_args(argv)
    if args.render_sample:
        out = Path(args.render_sample)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_report_html(sample_payload(), sample_db_data()), encoding="utf-8")
        print(str(out))
        return 0

    if app is None:
        print("FastAPI is not installed; install services/report/requirements.txt", file=sys.stderr)
        return 2
    import uvicorn

    uvicorn.run(
        "report_service:app",
        host=os.getenv("REPORT_HOST", "127.0.0.1"),
        port=int(os.getenv("REPORT_PORT", "8090")),
        reload=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
