#!/usr/bin/env python3
"""Binance testnet acceptance harness for PLAN 15.3.

The harness is intentionally offline-safe for --list and --dry-run. Live runs
are operator gated: each scenario consumes an evidence JSON file produced while
running against the Binance testnet/control-plane stack. Missing or incomplete
evidence is a failure and exits non-zero.
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
DEFAULT_REPORT = REPO_ROOT / "docs" / "acceptance" / "ORDER_MANAGEMENT_TESTNET_REPORT.md"
REQUIRED_LIVE_ENV = (
    "DATABASE_URL",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
)


@dataclass(frozen=True)
class Scenario:
    number: int
    name: str
    title: str
    fn: Callable[["AcceptanceContext", "Scenario"], "ScenarioResult"]


@dataclass(frozen=True)
class ScenarioResult:
    number: int
    name: str
    title: str
    status: str
    passed: bool
    order_id: str = ""
    event_chain: list[str] = field(default_factory=list)
    final_exchange_status: str = ""
    notes: str = ""


class AcceptanceContext:
    def __init__(self, *, evidence_dir: Path | None) -> None:
        self.evidence_dir = evidence_dir

    def from_evidence(self, scenario: Scenario) -> ScenarioResult:
        if self.evidence_dir is None:
            return _failed(scenario, "FAILED - missing --evidence-dir for live run")
        path = self.evidence_dir / f"{scenario.name}.json"
        if not path.exists():
            return _failed(scenario, f"FAILED - missing evidence file {path}")
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return _failed(scenario, f"FAILED - invalid evidence JSON: {exc}")

        order_id = str(evidence.get("order_id") or "").strip()
        final_status = str(evidence.get("final_exchange_status") or "").strip()
        event_chain = evidence.get("event_chain") or evidence.get("events") or []
        if not isinstance(event_chain, list):
            return _failed(scenario, "FAILED - evidence event_chain must be a list")
        event_chain = [str(item) for item in event_chain if str(item).strip()]
        notes = str(evidence.get("notes") or "").strip()
        checks = evidence.get("checks") or {}
        passed = bool(evidence.get("passed")) and bool(order_id) and bool(final_status) and bool(event_chain)
        if isinstance(checks, dict):
            passed = passed and all(bool(value) for value in checks.values())
        if not passed:
            missing = []
            if not order_id:
                missing.append("order_id")
            if not event_chain:
                missing.append("event_chain")
            if not final_status:
                missing.append("final_exchange_status")
            if evidence.get("passed") is not True:
                missing.append("passed=true")
            if isinstance(checks, dict):
                missing.extend(f"check:{key}" for key, value in checks.items() if not value)
            return ScenarioResult(
                scenario.number,
                scenario.name,
                scenario.title,
                "FAILED - live evidence did not pass",
                False,
                order_id=order_id,
                event_chain=event_chain,
                final_exchange_status=final_status,
                notes=", ".join(missing) or notes,
            )
        return ScenarioResult(
            scenario.number,
            scenario.name,
            scenario.title,
            "PASS",
            True,
            order_id=order_id,
            event_chain=event_chain,
            final_exchange_status=final_status,
            notes=notes,
        )


def market_open_sl_tp(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def limit_timeout_cancel(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def reprice_fill(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def partial_fill_keep_remainder(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def partial_fill_cancel_remainder(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def stop_loss_fill(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def batched_take_profit(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def move_stop(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def breakeven(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def trailing_stop(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def partial_close(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def full_close(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def cancel_all(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def close_all_multi_position(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def node_restart(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def control_plane_restart(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def ws_reconnect(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def reconciliation_drift(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def stale_market_account_fail_closed(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def duplicate_intent_event_no_duplicate_orders(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def two_account_isolation(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


def settings_publish_ack_rollback(ctx: AcceptanceContext, scenario: Scenario) -> ScenarioResult:
    return ctx.from_evidence(scenario)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(1, "market_open_sl_tp", "market open -> SL + TP", market_open_sl_tp),
    Scenario(2, "limit_timeout_cancel", "limit timeout -> cancel", limit_timeout_cancel),
    Scenario(3, "reprice_fill", "reprice then fill", reprice_fill),
    Scenario(4, "partial_fill_keep_remainder", "partial fill keep remainder", partial_fill_keep_remainder),
    Scenario(5, "partial_fill_cancel_remainder", "partial fill cancel remainder", partial_fill_cancel_remainder),
    Scenario(6, "stop_loss_fill", "stop loss fill", stop_loss_fill),
    Scenario(7, "batched_take_profit", "batched take profit", batched_take_profit),
    Scenario(8, "move_stop", "move stop", move_stop),
    Scenario(9, "breakeven", "breakeven", breakeven),
    Scenario(10, "trailing_stop", "trailing stop", trailing_stop),
    Scenario(11, "partial_close", "partial close", partial_close),
    Scenario(12, "full_close", "full close", full_close),
    Scenario(13, "cancel_all", "cancel_all", cancel_all),
    Scenario(14, "close_all_multi_position", "close_all multi-position batch", close_all_multi_position),
    Scenario(15, "node_restart", "node restart", node_restart),
    Scenario(16, "control_plane_restart", "control-plane restart", control_plane_restart),
    Scenario(17, "ws_reconnect", "WS reconnect", ws_reconnect),
    Scenario(18, "reconciliation_drift", "reconciliation drift", reconciliation_drift),
    Scenario(19, "stale_market_account_fail_closed", "stale market/account fail-closed", stale_market_account_fail_closed),
    Scenario(20, "duplicate_intent_event_no_duplicate_orders", "duplicate intent/event no duplicate orders", duplicate_intent_event_no_duplicate_orders),
    Scenario(21, "two_account_isolation", "two-account isolation", two_account_isolation),
    Scenario(22, "settings_publish_ack_rollback", "settings publish, node ACK, rollback", settings_publish_ack_rollback),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run or prepare OM8 Binance testnet acceptance evidence.")
    parser.add_argument("--list", action="store_true", help="List the PLAN 15.3 scenarios and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Validate harness wiring offline and write a pending report.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="Report path to write.")
    parser.add_argument("--evidence-dir", type=Path, help="Directory containing one <scenario>.json evidence file per live scenario.")
    parser.add_argument("--scenario", action="append", choices=[scenario.name for scenario in SCENARIOS], help="Run only this scenario; repeatable.")
    args = parser.parse_args(argv)

    if args.list:
        _print_scenarios(SCENARIOS)
        return 0

    selected = _select_scenarios(args.scenario)
    if args.dry_run:
        _validate_registry()
        _write_report(args.report, _pending_results(selected), mode="dry-run")
        missing = [name for name in REQUIRED_LIVE_ENV if not os.environ.get(name)]
        print(f"dry-run OK: {len(selected)} scenarios registered; no network calls made")
        if missing:
            print(f"live env not set: {', '.join(missing)}")
        return 0

    missing = [name for name in REQUIRED_LIVE_ENV if not os.environ.get(name)]
    if missing:
        print(f"missing required live environment: {', '.join(missing)}", file=sys.stderr)
        return 2

    ctx = AcceptanceContext(evidence_dir=args.evidence_dir)
    results = [scenario.fn(ctx, scenario) for scenario in selected]
    _write_report(args.report, results, mode="live")
    failed = [result for result in results if not result.passed]
    if failed:
        for result in failed:
            print(f"{result.name}: {result.status} {result.notes}".strip(), file=sys.stderr)
        return 1
    print(f"live acceptance PASS: {len(results)} scenarios")
    return 0


def _select_scenarios(names: list[str] | None) -> tuple[Scenario, ...]:
    if not names:
        return SCENARIOS
    wanted = set(names)
    return tuple(scenario for scenario in SCENARIOS if scenario.name in wanted)


def _print_scenarios(scenarios: Iterable[Scenario]) -> None:
    for scenario in scenarios:
        print(f"{scenario.number:02d}. {scenario.name} - {scenario.title}")


def _validate_registry() -> None:
    names = [scenario.name for scenario in SCENARIOS]
    if len(names) != 22:
        raise SystemExit(f"expected 22 scenarios, found {len(names)}")
    if len(set(names)) != len(names):
        raise SystemExit("scenario names must be unique")
    fake_venue = REPO_ROOT / "tests" / "order_management" / "simulated_venue" / "fake_venue.py"
    if not fake_venue.exists():
        raise SystemExit(f"FakeVenue pattern source is missing: {fake_venue}")


def _pending_results(scenarios: Iterable[Scenario]) -> list[ScenarioResult]:
    return [
        ScenarioResult(
            scenario.number,
            scenario.name,
            scenario.title,
            "PENDING - run against testnet",
            False,
        )
        for scenario in scenarios
    ]


def _failed(scenario: Scenario, notes: str) -> ScenarioResult:
    return ScenarioResult(
        scenario.number,
        scenario.name,
        scenario.title,
        "FAILED",
        False,
        notes=notes,
    )


def _write_report(path: Path, results: Iterable[ScenarioResult], *, mode: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    rows = list(results)
    passed = sum(1 for row in rows if row.passed)
    lines = [
        "# Order Management Binance Testnet Report",
        "",
        f"Generated: {generated_at}",
        f"Mode: {mode}",
        f"Summary: {passed}/{len(rows)} passed",
        "",
        "| # | Scenario | Status | Order ID | Event Chain | Final Exchange Status | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for result in rows:
        lines.append(
            "| {number} | {name} | {status} | {order_id} | {event_chain} | {final_status} | {notes} |".format(
                number=result.number,
                name=_escape(result.name),
                status=_escape(result.status),
                order_id=_escape(result.order_id or "PENDING"),
                event_chain=_escape(" -> ".join(result.event_chain) if result.event_chain else "PENDING"),
                final_status=_escape(result.final_exchange_status or "PENDING"),
                notes=_escape(result.notes or ""),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _escape(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
