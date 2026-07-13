#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import shutil
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
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


class ReportValidationError(ValueError):
    pass


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


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


def fetch_all(cursor: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cursor.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def safe_query(cursor: Any, sql: str, params: tuple[Any, ...], missing: list[str], label: str) -> list[dict[str, Any]]:
    try:
        return fetch_all(cursor, sql, params)
    except Exception as exc:
        missing.append(f"{label}: {exc}")
        return []


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


def fetch_report_data(report_type: str, report_date: str, database_url: str | None = None) -> dict[str, Any]:
    data = empty_db_data()
    start, end = window_bounds(report_type, report_date)
    data["window"] = {"start": start.isoformat(), "end": end.isoformat()}
    dsn = database_url or os.getenv("DATABASE_URL")
    if not dsn:
        data["missing_data"].append("DATABASE_URL is not set")
        return data
    if psycopg2 is None:
        data["missing_data"].append("psycopg2 is not installed")
        return data

    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:
        data["missing_data"].append(f"database connection: {exc}")
        return data
    conn.autocommit = True

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            outcome_rows = safe_query(
                cur,
                """
                SELECT closed_at, instrument_id, side, realized_pnl, r_multiple
                FROM trade_outcomes
                WHERE closed_at >= %s AND closed_at < %s
                ORDER BY closed_at ASC
                """,
                (start, end),
                data["missing_data"],
                "trade_outcomes",
            )
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

            mirror_rows = safe_query(
                cur,
                "SELECT account_id, payload, updated_at FROM exchange_state_mirror ORDER BY account_id",
                (),
                [],
                "exchange_state_mirror",
            )
            positions = []
            if mirror_rows:
                for row in mirror_rows:
                    positions.extend(normalize_position_payload(row.get("payload"), row.get("account_id"), row.get("updated_at")))
            else:
                positions = safe_query(
                    cur,
                    """
                    SELECT account_id, instrument_id AS symbol, side::text AS side, quantity,
                           avg_entry_price AS entry_price, mark_price, unrealized_pnl, updated_at,
                           'positions_projection' AS source
                    FROM positions_projection
                    WHERE quantity > 0 AND status NOT IN ('closed', 'flat')
                    ORDER BY account_id, instrument_id
                    """,
                    (),
                    data["missing_data"],
                    "positions_projection",
                )
            data["positions"] = positions
    finally:
        conn.close()
    return data


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

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        token = os.getenv("REPORT_TOKEN")
        if not token:
            return
        expected = f"Bearer {token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid report token")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/reports", dependencies=[Depends(require_auth)])
    def create_report(body: dict[str, Any]) -> dict[str, str]:
        try:
            payload = normalize_payload(body)
            db_data = fetch_report_data(payload["type"], payload["date"])
            return write_report(payload, db_data)
        except ReportValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
