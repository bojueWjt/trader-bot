from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request

from app.services.report_fallback import build_fallback_report
from app.services.report_renderer import render
from app.services.report_snapshot import DailySnapshot, build_snapshot, snapshot_is_complete


router = APIRouter()


@router.get("/status")
def status() -> dict[str, str]:
    return {"status": "mounted"}


@router.get("/daily/{report_date}")
async def daily_report(report_date: str, request: Request) -> dict:
    result = await _build_report_result(report_date, request)
    if "fallback" in result:
        return _fallback_daily_response(report_date, result["fallback"])
    snapshot = result["snapshot"]
    return _snapshot_daily_response(snapshot)


@router.get("/daily/{report_date}/markdown")
async def daily_markdown(report_date: str, request: Request) -> dict[str, str | bool]:
    result = await _build_report_result(report_date, request)
    if "fallback" in result:
        markdown = _fallback_markdown(result["fallback"])
    else:
        markdown = render(result["snapshot"])
    return {
        "report_id": f"daily-{report_date}",
        "generated_at": _generated_at(report_date),
        "markdown": markdown,
        "llm_used": False,
    }


@router.get("/daily/{report_date}/versions")
async def daily_versions(report_date: str, request: Request) -> list[dict[str, str | bool]]:
    result = await _build_report_result(report_date, request)
    return [
        {
            "report_id": f"daily-{report_date}",
            "generated_at": _generated_at(report_date),
            "llm_used": False,
            "fallback": "fallback" in result,
        }
    ]


@router.post("/daily/{report_date}/telegram-preview")
async def daily_telegram_preview(report_date: str, request: Request) -> dict[str, str | bool]:
    result = await _build_report_result(report_date, request)
    if "fallback" in result:
        text = f"Hermes Daily {report_date}\nReport data unavailable."
    else:
        snapshot = result["snapshot"]
        text = (
            f"Hermes Daily {snapshot.date}\n"
            f"Signals: {len(snapshot.signals)}\n"
            f"Positions: {len(snapshot.positions)}"
        )
    return {"text": text, "llm_used": False}


@router.post("/daily/snapshot")
async def daily_snapshot(body: dict, request: Request) -> dict:
    report_date = str(body.get("date", ""))
    result = await _build_report_result(report_date, request)
    if "fallback" in result:
        return {"snapshot_id": f"daily-{report_date}", "fallback": result["fallback"]}
    snapshot = result["snapshot"]
    return {"snapshot_id": f"daily-{snapshot.date}", "snapshot": asdict(snapshot)}


async def _build_report_result(report_date: str, request: Request) -> dict:
    try:
        snapshot = await build_snapshot(date=report_date, request=request)
        if not snapshot_is_complete(snapshot):
            return {"fallback": build_fallback_report(report_date, "snapshot incomplete")}
    except Exception as exc:
        return {"fallback": build_fallback_report(report_date, str(exc))}
    return {"snapshot": snapshot}


def _snapshot_daily_response(snapshot: DailySnapshot) -> dict:
    return {
        "snapshot_id": f"daily-{snapshot.date}",
        "date": snapshot.date,
        "account": snapshot.account,
        "trades": _trades_summary(snapshot.trades),
        "signals": _signals_summary(snapshot.signals),
        "risk_events": _risk_summary(snapshot.risk),
        "open_positions": _positions_summary(snapshot.positions),
        "llm_used": False,
        "collected_at": snapshot.collected_at,
    }


def _fallback_daily_response(report_date: str, fallback: dict) -> dict:
    return {
        "snapshot_id": f"daily-{report_date}",
        "date": report_date,
        "account": {},
        "trades": {},
        "signals": {},
        "risk_events": {},
        "open_positions": {},
        "llm_used": False,
        "fallback": fallback,
    }


def _trades_summary(trades: list[dict]) -> dict[str, int]:
    realized_pnls = [_realized_pnl(trade) for trade in trades]
    return {
        "closed_today_count": len(trades),
        "closed_count": len(trades),
        "win_count": sum(1 for pnl in realized_pnls if pnl is not None and pnl > 0),
        "loss_count": sum(1 for pnl in realized_pnls if pnl is not None and pnl < 0),
    }


def _realized_pnl(trade: dict) -> float | None:
    value = trade.get("realized_pnl", trade.get("pnl"))
    if value is None:
        return None
    return float(value)


def _signals_summary(signals: list[dict]) -> dict[str, int]:
    accepted_statuses = {"approved", "reserved", "sent_to_freqtrade", "entered", "partially_exited", "exited"}
    rejected_statuses = {"rejected", "blocked_by_risk", "expired", "failed", "ignored"}
    return {
        "received_count": len(signals),
        "accepted_count": sum(1 for signal in signals if signal.get("status") in accepted_statuses),
        "rejected_count": sum(1 for signal in signals if signal.get("status") in rejected_statuses),
        "executed_count": sum(
            1
            for signal in signals
            if signal.get("status") in {"entered", "partially_exited", "exited"}
        ),
    }


def _risk_summary(risk: dict) -> dict[str, int]:
    events = risk.get("events", []) if isinstance(risk, dict) else []
    if not isinstance(events, list):
        events = []
    return {"count": len(events), "blocking_count": 0}


def _positions_summary(positions: list[dict]) -> dict[str, int]:
    return {"count": len(positions)}


def _fallback_markdown(fallback: dict) -> str:
    lines = [f"# Daily Trading Report {fallback['date']}", ""]
    for section in fallback["sections"]:
        lines.extend([f"## {section['name']}", section["content"], ""])
    return "\n".join(lines).rstrip()


def _generated_at(report_date: str) -> str:
    return f"{report_date}T00:00:00+00:00"
