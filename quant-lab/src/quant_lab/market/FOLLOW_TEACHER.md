# Follow teacher backtest (kernel A v0.7)

`base-v1-timeexit-follow`, `base-v1-timeexit-live-follow`,
`base-v1-timeexit-w60-follow`, `base-v1-timeexit-w60-live-follow` opt into teacher
management. Live variants retain the existing `live_profile.py` plan concessions.
Kernel B rejects follow policies before market resolution or Nautilus import.

## Contract and input

`ExecutionRequest.management` defaults to `[]`. Each `ManagementAction` contains
`at`, `kind`, `fraction`, `stop_price`, `to_entry`, `source_message_id`.
`at` is UTC execution time, strictly after decision availability plus policy
latency, ordered and inside the observation window. Fractions are Decimal:
reduce accepts [0, 1], including a legal zero; add accepts nonnegative values.
Missing reduce fraction uses 0.5 only during execution.

Empty management is omitted from both Python and JSON request serialization.
`ExecutionPolicy.follow_teacher=False` is omitted from policy content, preserving
all 15 previous policy hashes. True adds the flag to content_hash. The execution
contract version stays `g2-exec-v0`; the optional compatible extension does not
rewrite old normalized requests.

L0 defaults to `Layout.from_root(None).silver_dir / "followup_action.parquet"`,
currently `$QUANT_LAB_DATA_ROOT/lake/telegram/silver/followup_action.parquet`.
`--followup-actions PATH` overrides it. A missing or malformed file fails a follow
batch, even for an empty channel. Non-follow policies do not read the file.

Rows must match channel, replayed episode and, when present, graph version.
Uncertain/null certainty, episode ambiguity and `none` are discarded. Visibility
must satisfy `t_dec < available_at < horizon_end`; execution uses
`available_at + policy.latency_s`. A delay placing execution at/after horizon is
discarded separately. Equal execution times sort by message_id, instruction_id
and canonical action payload; parquet row order has no effect.

## Execution and events

Existing rule/coverage, funding, TTL, holding-limit and horizon priorities remain.
After ingesting current prices, teacher actions run before same-time automatic
stop/TP matching. Market exits use only the current or latest already known last
point, with fresh mark/last checks, existing adverse slippage, taker fees and
multiplier. Like existing market entry/time exit, they bypass resting-limit bar
capacity. No future high/low/close point supplies the execution price.

- `close_all`: market-close residual position, cancel entry/TP siblings, finish.
  With no position, cancel entry and finish `no_fill`.
- `reduce`: floor current absolute position × fraction to lot step. Ignore and
  count no-position/zero-lot cases. Actual exits retain the existing exit latch,
  cancelling pending entry and resizing SL. Existing cumulative-entry TP targets
  remain; reduce-only fills cap at residual position.
- `move_stop`: use actual cumulative fill average for `to_entry`, otherwise the
  supplied price. Keep original stop trigger/timeframe; an already-crossed move
  triggers immediately via the existing mark-stop path, filling at the next legal
  last point. Before entry, retain the new price; pending `to_entry` resolves to
  fill average as entries fill. It never changes initial sizing or the R denominator.
- `cancel_pending`: cancel entry only; terminate as unfilled if flat.
- `add`: ignored and counted.

The only new event kind is `management`. It has no order lifecycle effect.
Its existing `reason` field carries canonical JSON with kind, status, source
message ID, fraction, to_entry and fraction_defaulted. `price`/`qty` record the
effective destination/reduction when applicable. All fills, amendments, cancels,
triggers and closes retain their existing kinds and fields. Source evidence text
is never copied to reports. Result `management_stats` is derived from this stream,
preserving old result serialization bytes.

L0 `summary.follow_teacher` records rows read/adopted/discarded, exclusive discard
reasons, read/adopted kind counts, defaulted reduce counts, plus execution counts,
ignored reasons and actions never processed because execution had already ended
or become censored. Trades add `n_teacher_actions_executed` and
`last_teacher_action_kind` (last executed action, null when none). Non-follow rows
carry 0/null. After bracket termination later commands are not processed.

## Version and hashes

Old build: `kernel-a-v0.5+873e23649e6a`.
New build: `kernel-a-v0.6+e1366ea99c91` (version + hash of kernel_a.py/contract.py).
Real build identity and trace hashes change for all policies; fixed-identity
compatibility is verified separately, never implemented by freezing old builds.

| New policy | content_hash |
| --- | --- |
| base-v1-timeexit-follow | c3d67b1574f29ae338c7f4736b09e7d87651c9c00d85fddbd1992b265924eeac |
| base-v1-timeexit-live-follow | caf17563082ad3bbdf8ba8aed680ce0da0e8d71f3e27a873b3d100b5e3a33ce2 |
| base-v1-timeexit-w60-follow | 51bb2d2e479eb0141fe91ce241a9b0f4daf4e3a736c894c2273860293124c376 |
| base-v1-timeexit-w60-live-follow | 1fecbd45e6ad5e75a0c6d1e26fc9c2adb393288bb0cbca18ad36dadc064a3cfd |

## v8: stopless plans, live v4 and the second pass (kernel A v0.7)

`OrderPlan.stop` may be null. Such a plan only runs under a policy with
`nostop_leg_notional_k` (each leg k×B) or `nostop_plan_notional_k` (k×B split
equally); both are excluded from content unless set, so all 19 earlier hashes are
unchanged. The request must then be `sizing.mode=fixed_qty`. L0 shapes a zone to
one limit at its near end (long: upper edge, short: lower edge), applies the live
profile, then sizes `q_i = N_i / (p_i × multiplier)` from the final prices
(market legs: t_dec as-of mark) and records `sizing_basis=nostop`. Missing,
non-TRADING or tick/multiplier-less rules at t_dec give `NOSTOP_RULES_UNRESOLVED`
(never `PLAN_CONTRACT_INVALID`). Old policies keep `PLAN_NO_STOP`.

Kernel A v0.7 builds no `sl-0` for a stopless plan until the teacher supplies a
stop: a priced `move_stop` on an open position creates `sl-0` at once (mark
trigger; an already-crossed price triggers immediately); a move or `to_entry`
before entry places `sl-0` at the fill. No automatic breakeven without `sl-0`.
Plans with a stop produce byte-identical events (112 frozen cases). Kernel B
rejects every nostop policy before the market resolver.

Live v4 (`trader-v3-live-v4`) applies only to rows carrying a `stop_rule` key:
`r9_fuzzy_break` gets exactly one 0.1% (silver already widened 0.3%),
`close_from_clause` none, every other rule 0.1% only when a fuzzy word sits on
`stop_base` in the stop message (`stop_source_version_id`, else the root text),
breakout wording off. A `relative` stop, and any base equal to an entry level
rather than the stop, is anchored on the stop price itself, so the entry's
「附近」 never widens it. Rows without the key keep v3. The audit's
`stop_v4.stop_message` is none / resolved / unresolved, the summary counts
`n_stop_text_unresolved`, and a stop message not visible strictly before t_dec
fails the run (look-ahead in the graph).

Episodes with `repost_of`/`amend_of` run in a causal second pass ordered by
(t_dec, episode_id): a repost is skipped (`REPOST_OF_LIVE_PLAN`) while any member
of the target's family (target plus executed reposts and amends) is alive
strictly before its t_dec, or was decided at the same t_dec; if no member is
known live but a censored member's evidence ends while it was still live, the
state is unknown and the repost gets `REPOST_TARGET_CENSORED`. An amend needs a
follow policy (`AMEND_REQUIRES_FOLLOW`), an unfilled target
(`AMEND_TARGET_FILLED`) and a target whose state is known
(`AMEND_TARGET_CENSORED`). A skipped target is replaced by its nearest executed
ancestor; a target without a result makes the plan independent.
Stopless market legs have no 0.25R stale-quote gate (the gate is measured in
stop distance); `market_ref_entries.nostop_quoted` counts their quote-to-mark gaps.

Summary `overall/by/cumulative_R/censor_counts` cover `sizing_basis=risk` rows
only (old policies: unchanged); `blocks.nostop` reports U metrics, MAE and
censor-time mark-to-market; `blocks.total` is a money sum only.

| v8 policy | content_hash |
| --- | --- |
| base-v1-timeexit-w14-live | c0fefe0a2d74e3199091a521b5360e7be86482185dc1dc160c92d00f5652db49 |
| base-v1-timeexit-w14-live-follow | 740d1bd40b24aaf8376c03e26d1db08610b751bd1a16db334f0736915503c0b5 |
| base-v1-timeexit-w60-live-follow-ns300 | 1269a7a730a04eaafefe2287d49a8adf0db32c80eb57dc6fb7a47b324d87a198 |
| base-v1-timeexit-w1-live-follow-ns300 | 42ada5b2b3fe4735c14b93d2c1bda80e3c975630d5747273f6a28698f0ab0148 |
| base-v1-timeexit-live-follow-ns300 | 0c924d1faa5dadef99050420c1217143bff46ed0868bc05f7efd38622de631a3 |
| base-v1-timeexit-w14-live-follow-ns300 | 16b41a23d7a170776d85b8f80dba9201c614c9c944fbfb89e2dbabd854b8b293 |
| base-v1-timeexit-w60-live-follow-ns100 | 327c603e265fda9905436d5db2cbea5599bdf608db0347e6ce60fee135b05147 |
| base-v1-timeexit-w60-live-follow-ns3rd | 6d654abfe74431f13781933df485b5c0cfb6ae16504ba592883cf5911136dea0 |
| base-v1-timeexit-w60-live-ns300 | 1a74a32fa8ac7174462a9d1ce61050699253e5b11290cdc8afa243be2a6b6e3c |

Tests: `tests/market/test_nostop.py`, the v4 cases in `test_live_profile.py`,
`test_live_replay.py::test_v8_rows_use_v4_and_v7_rows_keep_v3`; both mutation
scripts carry v8 mutants.

## Validation and explicit choices

`tests/market/test_follow_teacher.py`: long/short close before TP, half reduce then
remaining TP, current-position reduction and lot rounding, BE stop, crossed stop
between price points, pending cancel/move, actual fill average, original risk,
unsupported add, zero fraction, partial entry, fresh as-of quotes, costs,
funding/expiry priority, request validation, B rejection and build identity.

`test_all_15_old_hashes_and_112_synthetic_cases_are_byte_identical` consumes
`tests/market/follow-teacher-legacy-baseline.json`, captured before implementation.
It pins old request bytes, normalized requests, events, net_R, trace hashes and
full result bytes under the fixed old build, covering 22 gold fixtures plus
15 policies × 2 sides × 3 synthetic endings.

`tests/market/test_follow_replay.py` checks filtering/reasons, delay boundaries,
stable ties, missing/invalid/empty input. `tests/integration/test_follow_teacher_l0.py`
checks all four policies, both input paths, synthetic graph/lake consumption,
summary/trades, empty channels and non-follow isolation.

`tests/market/follow_teacher_mutations.py` runs only these relevant selectors
against temporary source copies. Final evidence:
`tests/market/follow-teacher-mutation-evidence.json`: **58/58 killed**, including
three initially surviving mutations closed by stronger reduction/crossed-stop
assertions. No working source is mutated.

Authorized assertion updates: old v0.5 assertions → v0.6; registry set gains four
follow entries. Two existing real-partition smokes are converted to temporary
synthetic silver using `synthetic_archive_lake`, preserving their coverage/tick/
as-of assertions and removing their skip gates to satisfy synthetic-only tests.
The legacy unknown `management_commands` field remains rejected. The S07
unsupported marker continues to describe live E13/C01/reservation lifecycle,
which this offline extension does not implement.

The static single-source gate required the minimal additional edit to
`single_source.py`: register `l0_replay.attach_management` as a caller of the
existing latency helper, and register the existing G1
`followup.visible_conversation` minutes conversion as a foreign hit. Request
visibility compares directly with the already resolved t_start. No duplicate
latency calculation or broader gate exclusion remains.

Full-suite runs, each run once without skip switches:

```text
.venv-g2/bin/python -m pytest -q tests/market tests/integration --tb=short
2 failed, 763 passed in 95.04s (0:01:35)

.venv-g1/bin/python -m pytest -q tests/data --tb=short
684 passed, 1 warning in 61.86s (0:01:01)
```

The two G2 failures were
`test_no_inline_reexpression_of_single_sources_anywhere_in_src` and
`test_foreign_hits_registry_is_exact`. Both were repaired as described above.
Relevant files were then rerun, including all follow acceptance and integration
tests and the 112-case compatibility check:

```text
.venv-g2/bin/python -m pytest -q tests/market/test_single_source.py tests/market/test_follow_teacher.py tests/market/test_follow_replay.py tests/integration/test_follow_teacher_l0.py --tb=short
119 passed in 28.83s
```

The full G2 suite was not repeated, respecting the once-only instruction. Raw
logs are `tests/market/follow-teacher-full-g2.log`,
`follow-teacher-full-g1.log`, `follow-teacher-repair-g2.log`.
G1's warning is Telethon's existing experimental async-session warning.
Final mutation evidence is regenerated against the repaired source.
No network, delegation or git commit; no skip switch was set.

## Remaining limits

1m bars use the existing synthetic intrabar path, not tick-accurate market data.
BE exits still incur existing adverse tick/fees; net_R need not be exactly zero.
Stop execution retains the existing next-last/slippage/gap model. Add and kernel B
remain unsupported. This change does not validate real followup extraction,
production execution or reservation management; src/quant_lab/data is untouched.

## Changed files and implementation locations

| Area | Files | Main entry points |
| --- | --- | --- |
| Contract/identity | `contract.py`, `policy_hashes.json` | `ManagementAction`, `ExecutionRequest._serialize/_chk`, `ExecutionResult.management_stats`, `POLICIES` |
| Execution | `kernel_a.py`, `execution.py`, `nautilus_adapter.py` | `KernelA.timeline/manage/teacher_market_exit/protect/protective_stop`, public `simulate`, `_simulate_b` rejection |
| L0 | `l0_replay.py` | `load_followup_actions`, `attach_management`, `replay`, `main` |
| Static gate | `single_source.py` | `ALLOWED_CALLERS`, `ALLOWED_CALL_COUNTS`, `FOREIGN_HITS` |
| Documentation | `src/quant_lab/market/FOLLOW_TEACHER.md` | This receipt |
| Existing tests | `tests/market/conftest.py`, `test_asof.py`, `test_execution_api.py`, `test_breakeven.py`, `test_live_replay.py` | Synthetic archive smokes; version/registry assertions |
| New tests | `tests/market/test_follow_teacher.py`, `test_follow_replay.py`, `tests/integration/test_follow_teacher_l0.py` | Test names below |
| Evidence | `tests/market/follow_teacher_mutations.py`, `follow-teacher-legacy-baseline.json`, `follow-teacher-mutation-evidence.json`, `follow-teacher-mutations.log`, `follow-teacher-full-g2.log`, `follow-teacher-full-g1.log`, `follow-teacher-repair-g2.log` | Offline reproducible commands and raw output |

## New test names

`tests/market/test_follow_teacher.py`:

- `test_close_before_tp_is_market_taker_and_cancels_protection`
- `test_reduce_half_then_remaining_tp`
- `test_reduce_uses_current_position_each_time_and_rounds_lots`
- `test_to_entry_stop_break_even_preserves_original_risk`
- `test_move_stop_past_current_mark_triggers_at_instruction_time`
- `test_cancel_before_entry_is_unfilled`
- `test_move_stop_before_entry_updates_protection_without_changing_sizing`
- `test_no_position_reduce_and_add_are_counted_and_zero_fraction_is_not_defaulted`
- `test_partial_entry_cancel_keeps_position_and_reduce_cancels_unfilled_entries`
- `test_intrabar_management_uses_only_known_point_and_market_capacity_is_unbounded`
- `test_management_uses_market_cost_model_and_fee_multiplier`
- `test_management_at_non_price_time_requires_fresh_quotes`
- `test_follow_policy_off_does_not_execute_management`
- `test_move_stop_preserves_close_timeframe_until_crossed_at_instruction`
- `test_pending_stop_changes_do_not_resize_original_risk_budget`
- `test_move_to_entry_uses_actual_average_of_multiple_fills`
- `test_funding_and_expiry_keep_priority_over_same_time_management`
- `test_follow_registrations_only_change_name_and_follow_flag`
- `test_kernel_b_rejects_follow_before_market_resolver_or_nautilus`
- `test_management_contract_rejects_bad_actions`
- `test_management_request_time_boundaries_and_provenance_change_hash`
- `test_all_15_old_hashes_and_112_synthetic_cases_are_byte_identical`
- `test_new_build_identity_covers_changed_source`

`tests/market/test_follow_replay.py`:

- `test_filter_counts_strict_visibility_uncertainty_ambiguity_episode_channel_graph_and_none`
- `test_equal_times_order_by_message_and_instruction_id_independent_of_parquet_order`
- `test_processing_latency_is_same_as_decision_and_late_execution_is_counted`
- `test_follow_file_empty_schema_valid_missing_and_bad_schema_are_batch_errors`

`tests/integration/test_follow_teacher_l0.py`:

- `test_follow_cli_default_and_explicit_path_reports_filters_and_executed_commands`
- `test_missing_follow_file_fails_batch_before_market_loading_or_writing`
- `test_empty_follow_input_keeps_economics_and_empty_channel_schema`
- `test_non_follow_does_not_read_followup_file`
