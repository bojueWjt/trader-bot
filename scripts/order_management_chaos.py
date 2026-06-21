#!/usr/bin/env python3
"""Operator-gated chaos/restart/disconnect harness for OM8-06.

The harness does not touch infrastructure in --list or --dry-run. Live runs read
operator evidence JSON files and fail closed unless every required assertion is
present and true.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = REPO_ROOT / "docs" / "acceptance" / "ORDER_MANAGEMENT_CHAOS_REPORT.md"
REQUIRED_LIVE_ENV = ("DATABASE_URL",)
REQUIRED_ASSERTIONS = (
    "no_duplicate_order",
    "stale_zero_new_risk",
    "reconciled_before_active",
)


@dataclass(frozen=True)
class ChaosCase:
    number: int
    name: str
    title: str
    fault: str
    fn: Callable[["ChaosContext", "ChaosCase"], "ChaosResult"]


@dataclass(frozen=True)
class ChaosResult:
    number: int
    name: str
    title: str
    status: str
    passed: bool
    assertions: dict[str, bool] = field(default_factory=dict)
    event_chain: list[str] = field(default_factory=list)
    recovery_state: str = ""
    notes: str = ""


class ChaosContext:
    def __init__(self, *, evidence_dir: Path | None) -> None:
        self.evidence_dir = evidence_dir

    def from_evidence(self, case: ChaosCase) -> ChaosResult:
        if self.evidence_dir is None:
            return _failed(case, "FAILED - missing --evidence-dir for live run")
        path = self.evidence_dir / f"{case.name}.json"
        if not path.exists():
            return _failed(case, f"FAILED - missing evidence file {path}")
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return _failed(case, f"FAILED - invalid evidence JSON: {exc}")

        raw_assertions = evidence.get("assertions") or {}
        assertions = {key: bool(raw_assertions.get(key)) for key in REQUIRED_ASSERTIONS}
        event_chain = evidence.get("event_chain") or evidence.get("events") or []
        if not isinstance(event_chain, list):
            return _failed(case, "FAILED - evidence event_chain must be a list")
        event_chain = [str(item) for item in event_chain if str(item).strip()]
        recovery_state = str(evidence.get("recovery_state") or "").strip()
        notes = str(evidence.get("notes") or "").strip()
        passed = bool(evidence.get("passed")) and all(assertions.values()) and bool(recovery_state)
        if not passed:
            missing = []
            if evidence.get("passed") is not True:
                missing.append("passed=true")
            missing.extend(key for key, value in assertions.items() if not value)
            if not recovery_state:
                missing.append("recovery_state")
            return ChaosResult(
                case.number,
                case.name,
                case.title,
                "FAILED - chaos evidence did not pass",
                False,
                assertions=assertions,
                event_chain=event_chain,
                recovery_state=recovery_state,
                notes=", ".join(missing) or notes,
            )
        return ChaosResult(
            case.number,
            case.name,
            case.title,
            "PASS",
            True,
            assertions=assertions,
            event_chain=event_chain,
            recovery_state=recovery_state,
            notes=notes,
        )


def node_restart(ctx: ChaosContext, case: ChaosCase) -> ChaosResult:
    return ctx.from_evidence(case)


def control_plane_restart(ctx: ChaosContext, case: ChaosCase) -> ChaosResult:
    return ctx.from_evidence(case)


def postgres_restart(ctx: ChaosContext, case: ChaosCase) -> ChaosResult:
    return ctx.from_evidence(case)


def redis_restart(ctx: ChaosContext, case: ChaosCase) -> ChaosResult:
    return ctx.from_evidence(case)


def network_disconnect(ctx: ChaosContext, case: ChaosCase) -> ChaosResult:
    return ctx.from_evidence(case)


def ws_reconnect(ctx: ChaosContext, case: ChaosCase) -> ChaosResult:
    return ctx.from_evidence(case)


def db_fault(ctx: ChaosContext, case: ChaosCase) -> ChaosResult:
    return ctx.from_evidence(case)


CHAOS_CASES: tuple[ChaosCase, ...] = (
    ChaosCase(1, "node_restart", "Nautilus node restart", "restart node container/process", node_restart),
    ChaosCase(2, "control_plane_restart", "control-plane restart", "restart control-plane API", control_plane_restart),
    ChaosCase(3, "postgres_restart", "PostgreSQL restart", "restart PostgreSQL", postgres_restart),
    ChaosCase(4, "redis_restart", "Redis restart", "restart Redis", redis_restart),
    ChaosCase(5, "network_disconnect", "control-plane to node network disconnect", "disconnect node/control-plane network", network_disconnect),
    ChaosCase(6, "ws_reconnect", "Binance websocket reconnect", "disconnect/reconnect Binance WS", ws_reconnect),
    ChaosCase(7, "db_fault", "database fault", "inject transient DB fault", db_fault),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run or prepare OM8 chaos evidence.")
    parser.add_argument("--list", action="store_true", help="List chaos cases and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Validate harness wiring offline and write a pending report.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="Report path to write.")
    parser.add_argument("--evidence-dir", type=Path, help="Directory containing one <case>.json evidence file per live case.")
    parser.add_argument("--case", action="append", choices=[case.name for case in CHAOS_CASES], help="Run only this case; repeatable.")
    args = parser.parse_args(argv)

    if args.list:
        _print_cases(CHAOS_CASES)
        return 0

    selected = _select_cases(args.case)
    if args.dry_run:
        _validate_registry()
        _write_report(args.report, _pending_results(selected), mode="dry-run")
        missing = [name for name in REQUIRED_LIVE_ENV if not os.environ.get(name)]
        print(f"dry-run OK: {len(selected)} chaos cases registered; no infra calls made")
        if missing:
            print(f"live env not set: {', '.join(missing)}")
        return 0

    missing = [name for name in REQUIRED_LIVE_ENV if not os.environ.get(name)]
    if missing:
        print(f"missing required live environment: {', '.join(missing)}", file=sys.stderr)
        return 2

    ctx = ChaosContext(evidence_dir=args.evidence_dir)
    results = [case.fn(ctx, case) for case in selected]
    _write_report(args.report, results, mode="live")
    failed = [result for result in results if not result.passed]
    if failed:
        for result in failed:
            print(f"{result.name}: {result.status} {result.notes}".strip(), file=sys.stderr)
        return 1
    print(f"chaos PASS: {len(results)} cases")
    return 0


def _select_cases(names: list[str] | None) -> tuple[ChaosCase, ...]:
    if not names:
        return CHAOS_CASES
    wanted = set(names)
    return tuple(case for case in CHAOS_CASES if case.name in wanted)


def _print_cases(cases: Iterable[ChaosCase]) -> None:
    for case in cases:
        print(f"{case.number:02d}. {case.name} - {case.title}")


def _validate_registry() -> None:
    names = [case.name for case in CHAOS_CASES]
    if len(set(names)) != len(names):
        raise SystemExit("chaos case names must be unique")
    if len(names) < 7:
        raise SystemExit("expected at least 7 chaos cases")


def _pending_results(cases: Iterable[ChaosCase]) -> list[ChaosResult]:
    return [
        ChaosResult(
            case.number,
            case.name,
            case.title,
            "PENDING - run against chaos infra",
            False,
            assertions={key: False for key in REQUIRED_ASSERTIONS},
        )
        for case in cases
    ]


def _failed(case: ChaosCase, notes: str) -> ChaosResult:
    return ChaosResult(
        case.number,
        case.name,
        case.title,
        "FAILED",
        False,
        assertions={key: False for key in REQUIRED_ASSERTIONS},
        notes=notes,
    )


def _write_report(path: Path, results: Iterable[ChaosResult], *, mode: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    rows = list(results)
    passed = sum(1 for row in rows if row.passed)
    lines = [
        "# Order Management Chaos Report",
        "",
        f"Generated: {generated_at}",
        f"Mode: {mode}",
        f"Summary: {passed}/{len(rows)} passed",
        "",
        "| # | Case | Status | Assertions | Event Chain | Recovery State | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for result in rows:
        assertions = ", ".join(f"{key}={value}" for key, value in result.assertions.items())
        lines.append(
            "| {number} | {name} | {status} | {assertions} | {event_chain} | {recovery_state} | {notes} |".format(
                number=result.number,
                name=_escape(result.name),
                status=_escape(result.status),
                assertions=_escape(assertions or "PENDING"),
                event_chain=_escape(" -> ".join(result.event_chain) if result.event_chain else "PENDING"),
                recovery_state=_escape(result.recovery_state or "PENDING"),
                notes=_escape(result.notes or ""),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _escape(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
