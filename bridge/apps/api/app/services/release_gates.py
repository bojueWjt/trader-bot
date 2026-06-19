from __future__ import annotations


GATE_REQUIREMENTS = {
    "dry_run": [
        "unit_tests_passed",
        "security_tests_passed",
        "strategy_loads",
        "dashboard_fake_data",
        "fallback_report_generated",
    ],
    "testnet": [
        "integration_tests_passed",
        "e2e_tests_passed",
        "kill_switch_manual_acceptance",
        "freqtrade_api_local_only",
        "exchange_key_testnet_only",
    ],
    "live_readonly": [
        "live_auto_entries_disabled",
        "dashboard_live_readonly",
        "live_snapshot_report",
        "audit_logs_secret_free",
        "recovery_drill_done",
    ],
    "live_small_size": [
        "dry_run_report_days",
        "dry_run_signal_count",
        "blocked_signals_have_reasons",
        "kill_switch_drill_passed",
        "daily_loss_guard_drill_passed",
        "default_single_trade_risk_pct",
    ],
}


class ReleaseGateEvaluator:
    def evaluate(self, evidence: dict) -> dict:
        return {
            "dry_run": self._evaluate_named_gate("dry_run", evidence),
            "testnet": self._evaluate_named_gate("testnet", evidence),
            "live_readonly": self._evaluate_named_gate("live_readonly", evidence),
            "live_small_size": self._evaluate_live_small_size(evidence),
        }

    def _evaluate_named_gate(self, gate: str, evidence: dict) -> dict:
        missing = []
        for key in GATE_REQUIREMENTS[gate]:
            if not evidence.get(key):
                missing.append(key)
        return {
            "passed": len(missing) == 0,
            "missing": missing,
        }

    def _evaluate_live_small_size(self, evidence: dict) -> dict:
        missing = []
        dry_run_report_days = evidence.get("dry_run_report_days", 0)
        if dry_run_report_days < 7:
            missing.append("dry_run_report_days")
        dry_run_signal_count = evidence.get("dry_run_signal_count", 0)
        if dry_run_signal_count < 30:
            missing.append("dry_run_signal_count")
        for key in [
            "blocked_signals_have_reasons",
            "kill_switch_drill_passed",
            "daily_loss_guard_drill_passed",
        ]:
            if not evidence.get(key):
                missing.append(key)
        risk_pct = evidence.get("default_single_trade_risk_pct")
        if risk_pct is None:
            missing.append("default_single_trade_risk_pct")
        else:
            if risk_pct > 0.25:
                missing.append("default_single_trade_risk_pct")
        return {
            "passed": len(missing) == 0,
            "missing": missing,
        }
