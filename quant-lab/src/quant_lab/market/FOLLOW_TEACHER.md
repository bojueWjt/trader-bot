# Follow teacher backtest (kernel A v0.6)

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
