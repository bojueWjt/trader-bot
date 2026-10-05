"""Offline isolated mutations. Run only named follow-related test selectors."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
UNIT = "tests/market/test_follow_teacher.py::"
REPLAY = "tests/market/test_follow_replay.py::"
FLOW = "tests/integration/test_follow_teacher_l0.py::"
NS = "tests/market/test_nostop.py::"
CONTRACT = "contract.py"
KERNEL = "kernel_a.py"
L0 = "l0_replay.py"
CASES = [
    ("empty-management-serialized", CONTRACT, 'payload.pop("management", None)', 'pass', UNIT + "test_all_15_old_hashes_and_112_synthetic_cases_are_byte_identical"),
    ("false-follow-in-hash", CONTRACT, 'if self.follow_teacher:', 'if True:', UNIT + "test_all_15_old_hashes_and_112_synthetic_cases_are_byte_identical"),
    ("true-follow-omitted-from-hash", CONTRACT, 'if self.follow_teacher:', 'if False:', UNIT + "test_follow_registrations_only_change_name_and_follow_flag"),
    ("invalid-reduce-fraction-accepted", CONTRACT, 'self.kind == "reduce" and self.fraction > 1', 'self.kind == "reduce" and self.fraction > 2', UNIT + "test_management_contract_rejects_bad_actions"),
    ("request-visibility-equality-accepted", CONTRACT, 'action.at <= exp_t_start', 'action.at < exp_t_start', UNIT + "test_management_request_time_boundaries_and_provenance_change_hash"),
    ("management-not-scheduled", KERNEL, 'at(action.at).management.append((index, action))', 'pass', UNIT + "test_close_before_tp_is_market_taker_and_cancels_protection"),
    ("management-delayed", KERNEL, 'at(action.at).management.append((index, action))', 'at(action.at + US).management.append((index, action))', UNIT + "test_intrabar_management_uses_only_known_point_and_market_capacity_is_unbounded"),
    ("disabled-policy-executes", KERNEL, 'if self.policy.follow_teacher:\n            for index', 'if True:\n            for index', UNIT + "test_follow_policy_off_does_not_execute_management"),
    ("close-all-only-half", KERNEL, 'abs(self.pos) if kind == "close_all" else floor_step(abs(self.pos) * fraction, self.rules.step_size)', 'floor_step(abs(self.pos) * fraction, self.rules.step_size)', UNIT + "test_close_before_tp_is_market_taker_and_cancels_protection"),
    ("teacher-exit-maker-fee", KERNEL, 'self.apply_exit_fill(ts, order, px, qty, True, point.bar_open_time, point.path_step)\n        if self.pos', 'self.apply_exit_fill(ts, order, px, qty, False, point.bar_open_time, point.path_step)\n        if self.pos', UNIT + "test_close_before_tp_is_market_taker_and_cancels_protection"),
    ("teacher-exit-costs-omitted", KERNEL, 'px = self.market_px(point.price, side)\n        self.slippage += abs(px - point.price) * qty * self.mult\n        self.apply_exit_fill(ts, order, px, qty, True, point.bar_open_time, point.path_step)\n        if self.pos', 'px = point.price\n        self.slippage += abs(px - point.price) * qty * self.mult\n        self.apply_exit_fill(ts, order, px, qty, True, point.bar_open_time, point.path_step)\n        if self.pos', UNIT + "test_management_uses_market_cost_model_and_fee_multiplier"),
    ("teacher-price-looks-ahead", KERNEL, 'point = self.last_point', 'point = PricePoint(ts=ts, price=max(b.h for b in self.market.bars_last))', UNIT + "test_intrabar_management_uses_only_known_point_and_market_capacity_is_unbounded"),
    ("reduce-original-instead-of-current-position", KERNEL, 'floor_step(abs(self.pos) * fraction, self.rules.step_size)', 'floor_step(self.entry_qty * fraction, self.rules.step_size)', UNIT + "test_reduce_uses_current_position_each_time_and_rounds_lots"),
    ("reduce-default-quarter", KERNEL, 'Decimal("0.5") if action.fraction is None else action.fraction', 'Decimal("0.25") if action.fraction is None else action.fraction', UNIT + "test_reduce_half_then_remaining_tp"),
    ("reduce-zero-defaulted", KERNEL, 'Decimal("0.5") if action.fraction is None else action.fraction', 'Decimal("0.5") if not action.fraction else action.fraction', UNIT + "test_no_position_reduce_and_add_are_counted_and_zero_fraction_is_not_defaulted"),
    ("no-position-reduce-not-counted", KERNEL, 'if kind == "reduce" and self.pos == 0:', 'if False:', UNIT + "test_no_position_reduce_and_add_are_counted_and_zero_fraction_is_not_defaulted"),
    ("add-not-counted-as-ignored", KERNEL, 'self.management_audit(ts, index, action, "ignored_add")', 'self.management_audit(ts, index, action, "executed")', UNIT + "test_no_position_reduce_and_add_are_counted_and_zero_fraction_is_not_defaulted"),
    ("stop-to-plan-instead-of-actual-average", KERNEL, 'price = None if self.pos == 0 else quantize_ratio(self.entry_cost / self.entry_qty)', 'price = None if self.pos == 0 else self.plan.stop.price', UNIT + "test_move_to_entry_uses_actual_average_of_multiple_fills"),
    ("crossed-stop-not-triggered", KERNEL, 'if crossed:\n                self.trigger_stop(ts)', 'if False:\n                self.trigger_stop(ts)', UNIT + "test_move_stop_past_current_mark_triggers_at_instruction_time"),
    ("numeric-stop-not-amended", KERNEL, 'sl.price = price\n                self.emit(ts, "amended"', 'sl.price = self.plan.stop.price\n                self.emit(ts, "amended"', UNIT + "test_move_stop_past_current_mark_triggers_at_instruction_time"),
    ("pending-stop-price-ignored", KERNEL, 'price = self.plan.stop.price if self.stop_override is None else self.stop_override', 'price = self.plan.stop.price', UNIT + "test_move_stop_before_entry_updates_protection_without_changing_sizing"),
    ("pending-to-entry-ignored", KERNEL, 'self.stop_to_entry = action.to_entry and self.pos == 0', 'self.stop_to_entry = False', UNIT + "test_move_stop_before_entry_updates_protection_without_changing_sizing"),
    ("risk-denominator-changed", KERNEL, 'b = self.req.risk_budget', 'b = self.req.risk_budget / 2 if self.teacher_stop else self.req.risk_budget', UNIT + "test_to_entry_stop_break_even_preserves_original_risk"),
    ("close-when-flat-does-not-cancel", KERNEL, 'kind in ("close_all", "cancel_pending") and self.pos == 0', 'kind == "cancel_pending" and self.pos == 0', UNIT + "test_cancel_before_entry_is_unfilled"),
    ("cancel-pending-with-position-flattens", KERNEL, 'if kind == "cancel_pending":', 'if False:', UNIT + "test_partial_entry_cancel_keeps_position_and_reduce_cancels_unfilled_entries"),
    ("reduce-does-not-latch-unfilled-entry", KERNEL, 'self.set_exit_latch(ts)\n        side =', 'side =', UNIT + "test_partial_entry_cancel_keeps_position_and_reduce_cancels_unfilled_entries"),
    ("reduce-protection-quantity-not-updated", KERNEL, 'if self.pos != 0:\n            self.protect(ts)\n        self.finish_if_flat(ts, reason="teacher_close")', 'self.finish_if_flat(ts, reason="teacher_close")', UNIT + "test_reduce_half_then_remaining_tp"),
    ("source-message-id-lost", KERNEL, '"source_message_id": action.source_message_id', '"source_message_id": 0', UNIT + "test_close_before_tp_is_market_taker_and_cancels_protection"),
    ("control-event-not-in-stream", KERNEL, 'self.emit(ts, "management", f"management-{index}"', 'self.emit(ts, "amended", f"management-{index}"', UNIT + "test_close_before_tp_is_market_taker_and_cancels_protection"),
    ("default-fraction-result-count-missing", CONTRACT, 'if kind == "reduce" and audit["fraction_defaulted"]:', 'if False:', UNIT + "test_reduce_half_then_remaining_tp"),
    ("last-quote-staleness-ignored", KERNEL, 'if point is None or (ts - point.ts).total_seconds() > self.policy.mark_max_staleness_s:\n            self.censor_now("BAR_GAP", "bars_ok")\n        return None', 'if point is None:\n            self.censor_now("BAR_GAP", "bars_ok")\n        return None', UNIT + "test_management_at_non_price_time_requires_fresh_quotes"),
    ("close-stop-timeframe-changed", KERNEL, 'basis = "mark" if self.breakeven_done else self.plan.stop.trigger', 'basis = "mark"', UNIT + "test_move_stop_preserves_close_timeframe_until_crossed_at_instruction"),
    ("public-b-accepts-follow", "execution.py", 'if resolve_policy(req.policy_version).follow_teacher:', 'if False:', UNIT + "test_kernel_b_rejects_follow_before_market_resolver_or_nautilus"),
    ("adapter-b-accepts-follow", "nautilus_adapter.py", 'if policy.follow_teacher:', 'if False:', UNIT + "test_kernel_b_rejects_follow_before_market_resolver_or_nautilus"),
    ("new-version-not-bumped", KERNEL, 'KERNEL_VERSION = "kernel-a-v0.7"', 'KERNEL_VERSION = "kernel-a-v0.6"', UNIT + "test_new_build_identity_covers_changed_source"),
    ("uncertain-actions-adopted", L0, 'elif row["uncertain"] is not False:', 'elif False:', REPLAY + "test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none"),
    ("ambiguous-episode-adopted", L0, 'elif row["episode_ambiguity"] not in (None, ""):', 'elif False:', REPLAY + "test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none"),
    ("at-tdec-adopted", L0, 'if available <= req.t_dec:', 'if available < req.t_dec:', REPLAY + "test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none"),
    ("at-horizon-adopted", L0, 'if available >= req.horizon_end:', 'if available > req.horizon_end:', REPLAY + "test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none"),
    ("wrong-episode-adopted", L0, 'req = by_episode.get(row["episode_id"])', 'req = next(iter(by_episode.values()))', REPLAY + "test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none"),
    ("other-channel-adopted", L0, 'if source_channel != channel:', 'if False:', REPLAY + "test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none"),
    ("other-graph-adopted", L0, 'elif row.get("graph_version", graph_version) != graph_version:', 'elif False:', REPLAY + "test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none"),
    ("none-not-discarded-as-none", L0, 'elif kind == "none":', 'elif False:', REPLAY + "test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none"),
    ("management-latency-omitted", L0, 'at = derived_t_start(available, policy)', 'at = available', REPLAY + "test_processing_latency_is_same_as_decision_and_late_execution_is_counted"),
    ("delayed-horizon-equality-adopted", L0, 'if at >= req.horizon_end:', 'if at > req.horizon_end:', REPLAY + "test_processing_latency_is_same_as_decision_and_late_execution_is_counted"),
    ("equal-time-tie-order-ignored", L0, 'key=lambda pair: (pair[0].at, pair[0].source_message_id, pair[1])', 'key=lambda pair: pair[0].at', REPLAY + "test_equal_times_order_by_message_and_instruction_id_independent_of_parquet_order"),
    ("adopted-default-count-missing", L0, 'defaulted += 1', 'defaulted += 0', REPLAY + "test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none"),
    ("missing-follow-file-silently-empty", L0, 'raise ContractError(f"follow_teacher requires followup actions file: {path}")', 'return []', REPLAY + "test_follow_file_empty_schema_valid_missing_and_bad_schema_are_batch_errors"),
    ("bad-file-schema-silently-empty", L0, 'raise ContractError(f"followup actions schema invalid: {path}")', 'return []', REPLAY + "test_follow_file_empty_schema_valid_missing_and_bad_schema_are_batch_errors"),
    ("explicit-follow-path-ignored", L0, 'Path(followup_actions).expanduser().resolve()', 'layout.silver_dir / "followup_action.parquet"', FLOW + "test_follow_cli_default_and_explicit_path_reports_filters_and_executed_commands[base-v1-timeexit-follow-True]"),
    ("default-follow-path-wrong", L0, 'else layout.silver_dir / "followup_action.parquet"', 'else layout.gold_dir / "followup_action.parquet"', FLOW + "test_follow_cli_default_and_explicit_path_reports_filters_and_executed_commands[base-v1-timeexit-follow-False]"),
    ("management-not-attached-to-request", L0, '"management": [a for a, _ in ordered]', '"management": []', FLOW + "test_follow_cli_default_and_explicit_path_reports_filters_and_executed_commands[base-v1-timeexit-follow-False]"),
    ("trades-executed-count-zero", L0, '[teacher_stats[eid]["n_executed"] for eid in table["episode_id"]]', '[0 for eid in table["episode_id"]]', FLOW + "test_follow_cli_default_and_explicit_path_reports_filters_and_executed_commands[base-v1-timeexit-follow-False]"),
    ("trades-last-kind-missing", L0, '[teacher_stats[eid]["last_kind"] for eid in table["episode_id"]]', '[None for eid in table["episode_id"]]', FLOW + "test_follow_cli_default_and_explicit_path_reports_filters_and_executed_commands[base-v1-timeexit-follow-False]"),
    ("input-read-count-zero", L0, '"n_read": len(rows)', '"n_read": 0', REPLAY + "test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none"),
    ("discard-count-zero", L0, '"n_discarded": sum(discarded.values())', '"n_discarded": 0', REPLAY + "test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none"),
    ("kind-count-zero", L0, 'kind: adopted_kinds[kind]', 'kind: 0', REPLAY + "test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none"),
    ("non-follow-reads-follow-file", L0, 'load_followup_actions(followup_path) if policy.follow_teacher else []', 'load_followup_actions(followup_path)', FLOW + "test_non_follow_does_not_read_followup_file"),
    # v8 F1 stopless execution (kernel A v0.7) and the §4 second pass.
    ("nostop-kernel-b-accepted", "execution.py", 'if resolve_policy(req.policy_version).nostop_enabled:', 'if False:', NS + "test_kernel_b_rejects_nostop_policies_before_resolver"),
    ("nostop-sl-built-without-teacher-price", KERNEL, 'if sl is None and self.stop_override is None and self.plan.stop is None:', 'if False:', NS + "test_fixed_notional_btc_leg_has_no_stop_events"),
    ("nostop-teacher-stop-not-placed", KERNEL, 'elif sl is None and self.pos != 0 and price is not None:\n                self.protect(ts)', 'elif False:\n                self.protect(ts)', NS + "test_teacher_stop_on_stopless_position_creates_sl_and_triggers_on_mark"),
    ("nostop-teacher-stop-close-basis", KERNEL, 'if sl is not None and sl.price is not None:\n                    return sl.price, "mark"\n            return None, "none"', 'if sl is not None and sl.price is not None:\n                    return sl.price, "close"\n            return None, "none"', NS + "test_teacher_stop_below_market_waits_then_triggers"),
    ("nostop-contract-accepts-risk-sizing", CONTRACT, 'if self.order_plan.sizing.mode != "fixed_qty":', 'if False:', NS + "test_ns_policy_stopless_plan_requires_fixed_qty"),
    ("nostop-old-policy-accepts-stopless", CONTRACT, 'if not pol.nostop_enabled:\n                raise', 'if False:\n                raise', NS + "test_ns_policy_stopless_plan_requires_fixed_qty"),
    ("nostop-leg-notional-ignored", L0, 'notionals = [policy.nostop_leg_notional_k * risk_budget] * n', 'notionals = [risk_budget] * n', NS + "test_fixed_notional_btc_leg_has_no_stop_events"),
    ("nostop-plan-notional-not-split", L0, 'notionals = [policy.nostop_plan_notional_k * risk_budget / n] * n', 'notionals = [policy.nostop_plan_notional_k * risk_budget] * n', NS + "test_whole_plan_third_splits_equally_across_legs"),
    ("nostop-zone-far-end", L0, 'near = entry["price_hi"] if long else entry["price_lo"]', 'near = entry["price_lo"] if long else entry["price_hi"]', NS + "test_zone_becomes_near_end_single_limit"),
    ("nostop-rules-merged-with-contract", L0, 'return None, None, "NOSTOP_RULES_UNRESOLVED", resolved, stale', 'return None, None, "PLAN_CONTRACT_INVALID:ValueError", resolved, stale', NS + "test_missing_rules_are_their_own_exclusion"),
    ("nostop-old-policy-executes", L0, 'if nostop and not policy.nostop_enabled:', 'if False:', NS + "test_old_policy_keeps_plan_no_stop"),
    ("summary-mixes-blocks", L0, 'rows = _risk_rows(all_rows)', 'rows = all_rows', NS + "test_summary_overall_is_risk_only_and_blocks_split"),
    ("repost-family-ignored", L0, 'for m in members[family[target]]}', 'for m in [target]}', NS + "test_second_pass_repost_family"),
    ("repost-censored-counted-as-live", L0, 'return "unknown" if evidence_short else "live"', 'return "live"', NS + "test_second_pass_censored_target_is_not_counted_as_live"),
    ("repost-censored-executes", L0, 'elif "unknown" in states:', 'elif False:', NS + "test_second_pass_censored_target_is_not_counted_as_live"),
    ("censored-without-events-ended", L0, 'return "unknown" if censor is not None and not events else "ended"', 'return "ended"', NS + "test_state_at_separates_unknown_from_live_and_ended"),
    ("amend-censored-target-executed", L0, 'elif state_at(events[target], censors[target], req.t_dec) == "unknown":', 'elif False:', NS + "test_second_pass_amend_with_censored_unfilled_target"),
    ("amend-outside-family", L0, 'root = eid if decision == "independent_no_target_result" else family[target]', 'root = family[target] if kind == "repost" else eid', NS + "test_second_pass_executed_amend_joins_the_target_family"),
    ("same-t-dec-member-not-live", L0, '"live" if decided[m] == req.t_dec else', '"live" if False else', NS + "test_second_pass_reposts_at_the_same_t_dec_execute_once"),
    ("mae-pct-planned-notional", L0, 'mae_U * 100 / entry_notional)', 'mae_U * 100 / notional)', NS + "test_mae_in_U_is_mae_R_times_budget"),
    ("repost-skip-chain-ignored", L0, 'while target in skipped and target not in seen:', 'while False:', NS + "test_second_pass_repost_family"),
    ("alive-sees-equal-time", L0, 'prior = [e for e in events if e["ts"] < t]', 'prior = [e for e in events if e["ts"] <= t]', NS + "test_alive_at_only_sees_strictly_earlier_events"),
    ("amend-filled-target-executed", L0, 'elif entry_filled_before(events[target], req.t_dec):', 'elif False:', NS + "test_second_pass_amend"),
    ("amend-without-follow-executed", L0, 'if not follow:\n                reason = "AMEND_REQUIRES_FOLLOW"', 'if False:\n                reason = "AMEND_REQUIRES_FOLLOW"', NS + "test_second_pass_amend"),
    ("mtm-ignores-mark", L0, 'unrealized = (Decimal(str(mark_price)) - row["entry_avg_price"]) * residual * sign * mult', 'unrealized = Decimal(0)', NS + "test_censored_nostop_mark_to_market_uses_censor_time_mark"),
]


def scrub(text: str, tmp: str) -> str:
    """Evidence must not carry local absolute paths: worktree, temp dirs and the home directory become placeholders."""
    for path, label in ((str(ROOT), "<quant-lab>"), (str(Path(tmp).resolve()), "<tmp>"), (tmp, "<tmp>"), (str(Path.home()), "~")):
        text = text.replace(path, label)
    text = re.sub(r"/(?:private/)?var/folders/[^/\s]+/[^/\s]+/T/", "<tmp>/", text)
    return re.sub(r"pytest-of-[^/\s]+", "pytest-of-<user>", text)


def main():
    assert "QUANT_LAB_SYNTHETIC_ONLY" not in os.environ, "No skip switch is permitted"
    evidence = {"command": ".venv-g2/bin/python tests/market/follow_teacher_mutations.py", "mutations": []}
    original = {name: (ROOT / "src/quant_lab/market" / name).read_bytes() for name in {case[1] for case in CASES}}
    with tempfile.TemporaryDirectory(prefix="quant-follow-mutants-") as tmp:
        for name, relative, before, after, selector in CASES:
            isolated = Path(tmp) / name
            shutil.copytree(ROOT / "src", isolated / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            path = isolated / "src/quant_lab/market" / relative
            source = path.read_text()
            assert source.count(before) == 1, (name, source.count(before))
            path.write_text(source.replace(before, after))
            runner = isolated / "run.py"
            runner.write_text('import pathlib, quant_lab, pytest, sys\n'
                'assert pathlib.Path(quant_lab.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1]).resolve())\n'
                'raise SystemExit(pytest.main(sys.argv[2:]))\n')
            env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(isolated / "src"), str(ROOT))), PYTHONDONTWRITEBYTECODE="1")
            args = [str(ROOT / ".venv-g2/bin/python"), str(runner), str(isolated / "src"), "-c", str(ROOT / "pyproject.toml"),
                    "-o", "pythonpath=" + str(isolated / "src"), "-q", "--tb=short", str(ROOT / selector)]
            proc = subprocess.run(args, cwd=isolated, env=env, capture_output=True, text=True, timeout=90)
            killed = proc.returncode == 1 and " failed" in proc.stdout and "ERROR collecting" not in proc.stdout
            evidence["mutations"].append(dict(name=name, file=relative, before=before, after=after, test=selector,
                source_sha256=hashlib.sha256(source.encode()).hexdigest(), returncode=proc.returncode,
                killed=killed, output=scrub(proc.stdout + proc.stderr, tmp)))
            print(f"{name}: {'KILLED' if killed else 'FAILED_CHECK'}", flush=True)
    for name, data in original.items():
        assert (ROOT / "src/quant_lab/market" / name).read_bytes() == data, f"working source changed: {name}"
    evidence["killed"] = sum(m["killed"] for m in evidence["mutations"])
    evidence["total"] = len(CASES)
    target = Path(__file__).with_name("follow-teacher-mutation-evidence.json")
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(f"{evidence['killed']}/{evidence['total']} mutants killed; {target.name}")
    return 0 if evidence["killed"] == evidence["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
