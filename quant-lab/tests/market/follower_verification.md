# Follower execution verification

完成范围：I01、I04、G5a、I03、I02/I12、I42、I46、I43、I45、G1c。仅改 market 与对应测试；没有修改 src/quant_lab/data/，没有提交 Git，没有联网。

Grok 降级：当前无 grok_rescue MCP；companion CLI 启动失败（EPERM，不能写 /Users/balen/.grok/codex-plugin/state/.../state.json），按约定由 Codex 完成本地实现与执行。

## 实现位置与验收测试

| 规则 | 实现位置 | 合成回归 |
|---|---|---|
| I01 | kernel_a.py `_exit_sl`、`trigger_stop` | `test_stop_intrabar_uses_stop_plus_adverse_tick`、`test_stop_gap_uses_last_open_without_slippage`、`test_stop_cost_scenario_slippage_and_common_path`、`test_close_stop_recovery_still_fills_next_open` |
| I04 | kernel_a.py `match_point`、`match_tps`、`Order.submitted_at`、`run` 的已知 last 即时限价撮合 | `test_resting_entry_cross_fills_at_limit`、`test_resting_entry_gap_fills_at_open`、`test_resting_tp_cross_and_gap`、`test_marketable_limit_is_taker_at_current_price`、`test_marketable_tp_is_immediate_taker`、`test_limit_submitted_before_first_price_is_resting_maker` |
| G5a | kernel_a.py `path_orders`、`_expanded`、`all_marks` | `test_stop_cost_scenario_slippage_and_common_path` |
| I03 | kernel_a.py `match_point` 市价数量直接按 step/钱包计算，不经过 `take` 的分钟容量 | `test_ioc_market_ignores_zero_minute_capacity` |
| I02/I12 | l0_replay.py `resolve_market_refs`、`replay_exclusions` | `test_plan_stale_includes_equal_stop`、`test_quote_quarter_r_boundary_and_mark_sizing`、`test_unpriced_market_and_limit_share_stale_gate`、`test_follower_invalid_plan_counts_in_replay_exclusions_not_r` |
| I42 | kernel_a.py `ladder_prices`、`submit_entries` 各腿过滤、`protect` 的 TP 向内取整/独立拒绝 | `test_entry_and_tp_tick_rounding_is_passive_and_inward`、`test_illegal_entry_leg_does_not_reject_market_sibling`、`test_invalid_tp_does_not_reject_other_protection_legs` |
| I46 | execution.py `load_market_from_lake(window_before_s=60)`；kernel_a.py `settle_funding` 零仓不因缺 mark 删失 | `test_loader_preminute_and_quarantined_funding_month_are_evaluable`、`test_minute_before_funding_has_closed_mark_and_no_position_is_not_stale`、`test_funding_before_first_fill_does_not_need_a_mark` |
| I43 | execution.py 局部质量缺口 `bar_gap_times`；kernel_a.py `timeline/run`，不再预设整窗 bars_ok=False；没有按 now() 缩窗口 | `test_bar_gap_only_excludes_still_open_trade`、`test_unfilled_waiting_period_complete_is_zero_despite_later_gap`、`test_gap_at_unfilled_expiry_is_outside_waiting_period`、`test_lake_quality_gap_and_missing_tail_only_affect_open_position` |
| I45 | contract.py `missing_funding_times`；execution.py 读取前一日周期上下文并保留缺失时刻；kernel_a.py 仅有仓遇缺行才删失；partition_check.py `check_funding` 允许前/后周期切换 | `test_funding_cycle_switch_and_missing_actual_settlement`、`test_funding_missing_row_checked_only_while_holding`、`test_lake_holding_period_missing_settlement_row_censors`、`test_loader_preminute_and_quarantined_funding_month_are_evaluable` |
| G1c | execution.py `BATCH_SCHEMA/result_row` 添加 Boolean evaluable；未覆盖且无原因的行补 COVERAGE_* | `test_evaluable_and_coverage_reason_match_summary`、`test_follower_invalid_plan_counts_in_replay_exclusions_not_r` |
| B 支持边界 | nautilus_adapter.py `_simulate_b` 显式拒绝 base-* 研究策略，保留旧合成 spike | `test_kernel_b_rejects_follower_research_policy` |

contract.py 模块文档字符串同步止损、限价、市价容量和持仓期资金费覆盖描述。策略字段/策略登记表未改；合约版本仍 g2-exec-v0（兼容新增结果列）。

## 验证命令与完整结果行

两套完整测试最后串行各运行一次。QUANT_LAB_SYNTHETIC_ONLY=1 仅跳过本地真实行情冒烟；HTTP 用例使用 MockTransport。

```text
QUANT_LAB_SYNTHETIC_ONLY=1 .venv-g2/bin/python -m pytest tests/market tests/integration
================== 601 passed, 2 skipped in 183.16s (0:03:03) ==================
QUANT_LAB_SYNTHETIC_ONLY=1 .venv-g1/bin/python -m pytest tests/data
================== 608 passed, 1 warning in 182.36s (0:03:02) ==================
git diff --check: exit 0
```

G2 两个跳过项：`test_real_partition_mark_price_at`、`test_load_market_from_lake_and_simulate_real_bars`。普通运行仍保留原真实数据冒烟；仅本轮合成开关跳过。初轮选测意外触发一次现有本地真实行情冒烟，随即加开关；最终验收仅用合成数据，无网络请求。

## 变异核验

29/29 killed；复制源码到临时目录变异，项目源码不做临时替换。每个变异仅跑相关文件及 -k 规则用例。完整证据见 follower_mutation_results.json / follower_mutations.log。

| 变异 | 结果 | pytest 结果行 |
|---|---|---|
| I01-extreme | KILLED | `2 failed, 39 deselected in 0.28s` |
| I01-zero-tick | KILLED | `2 failed, 39 deselected in 0.29s` |
| I01-close-next-open | KILLED | `1 failed, 40 deselected in 0.31s` |
| I01-gap | KILLED | `2 failed, 39 deselected in 0.28s` |
| I04-entry-cross | KILLED | `2 failed, 39 deselected in 0.28s` |
| I04-tp-cross | KILLED | `2 failed, 39 deselected in 0.28s` |
| I04-entry-fee | KILLED | `2 failed, 39 deselected in 0.28s` |
| I04-tp-fee | KILLED | `1 failed, 40 deselected in 0.28s` |
| I04-gap-limit | KILLED | `4 failed, 37 deselected in 0.28s` |
| G5a-path | KILLED | `1 failed, 40 deselected in 0.28s` |
| I03-cap | KILLED | `1 failed, 40 deselected in 0.29s` |
| I12-stop-boundary | KILLED | `2 failed, 4 passed, 35 deselected in 0.26s` |
| I02-quote-boundary | KILLED | `2 failed, 2 passed, 37 deselected in 0.25s` |
| I02-quote-sizing | KILLED | `2 failed, 2 passed, 37 deselected in 0.26s` |
| I42-entry-tick | KILLED | `2 failed, 39 deselected in 0.27s` |
| I42-tp-tick | KILLED | `2 failed, 39 deselected in 0.31s` |
| I42-cascade | KILLED | `1 failed, 40 deselected in 0.28s` |
| I46-prewindow | KILLED | `1 failed, 3 deselected in 0.33s` |
| I43-window-mask | KILLED | `1 failed, 1 passed, 39 deselected in 0.29s` |
| I43-gap-skip | KILLED | `1 failed, 1 passed, 39 deselected in 0.29s` |
| I43-unfilled-boundary | KILLED | `1 failed, 40 deselected in 0.23s` |
| I45-fill-censor | KILLED | `1 failed, 1 passed, 39 deselected in 0.25s` |
| I45-missing-row | KILLED | `1 failed, 3 deselected in 0.30s` |
| I45-month-manifest | KILLED | `1 failed, 3 deselected in 0.42s` |
| I45-actual-interval | KILLED | `1 failed, 40 deselected in 0.36s` |
| I46-zero-position | KILLED | `1 failed, 40 deselected in 0.26s` |
| I45-switch | KILLED | `1 failed, 40 deselected in 0.25s` |
| G1c-evaluable | KILLED | `1 failed, 40 deselected in 0.29s` |
| G1c-reason | KILLED | `1 failed, 40 deselected in 0.31s` |

## 旧预期更新（逐条）

- E01：无 bar 跳空开盘证据，止损由 last 90 改为 stop 95 − 1 tick = 94，R −2 → −1.2；E02 异步 last 98 同样改 94，R −0.4 → −1.2。
- E04a：静止 TP 的 bar H 110 改限价 105，R 2 → 1；E04b 多止损 90 改 94，E04c 空止损 110 改 106，R 均 −2 → −1.2。路径情景测试同步这些数值。
- E10：mark/last 同刻竞合的 SL 105 改 94，R 1 → −1.2；E18 入场后保护重检 SL 100 改 94，R 0 → −1.2（L0 会先 PLAN_STALE 排除，内核直测仍验保护）。
- E12：价格仍 94，但 slippage 诊断由 0 改 1；多档 TP 与剩余 SL 的账务不变。
- E14a：off-tick 入场 100.5 不再整单 PRICE_FILTER 拒绝，向下取整 100；TP 105，R 0 → 1。PRICE_FILTER 边界和 outcome_kind 用例同步；真正非法腿仍独立拒绝。
- E17：市价入场 101 保留；SL 原先错误 109 改 stop95−tick=94，fees .105 → .0975，net_R 1.579 → −1.4195。
- 保本系列：有效 stop100/80 等同样加不利 tick，不再无滑点成交于 100/80；wick stop90 改 94。四分之一 TP/保本比较：gross 10 → 7，未保本 gross −50 → −53；entry taker 费用同步。
- 到期平仓系列：marketable 入场由 maker .04 改 taker .1；长单 net_R .3858 → .3798、短单 .3862 → .3802；TP1+到期净值 6.888 → 6.828。到期平仓与 stress 滑点机制保留。
- L0 合成端到端：marketable entry 收 taker、静止 TP 收 maker，sum_R .9958 → .9928，mean .4979 → .4964。
- funding_schedule_complete=False 不再在成交时或 time_exit 无条件删失；没有持仓期缺失结算行时保留结算和原右删失/到期结果。明确缺行仍 FUNDING_SCHEDULE_GAP。
- 局部来源/OHLC/null/off-grid 质量失败不再把整窗标坏：删除无效行并保存发生时刻，已成交后遇缺口保留此前成交并删失；在规则先失败时，不宣称已走到未来 bar 缺口。窗口精确装载差分测试显式用 window_before_s=0，默认 60 秒由新集成覆盖。
- B 的旧冻结 MATCH/解释集合随 A 金标变化失效：保持 UNEXPLAINED，不重写旧解释登记；变化夹具 kernels 改为 [A]。研究 base-* 明确不支持 B。
- 构建/trace 旧值冻结测试改为校验新独立金标、未受影响夹具事件与 R 保持不变，所有旧 trace 失效；fixture 完整性/Decimal 差分测试同步新值。
- 原限价 IOC 部分成交 E08 不变：保留限价容量及 ioc_remainder；只有市价容量断言改为全量。

## 版本与哈希

```text
kernel-a-v0.4+eab1491f053f -> kernel-a-v0.5+7a2dbde9e403
policy content_hash: 11 registered policies unchanged = True
synthetic fixture trace_hash changed: 22/22
```

每个策略的完整 hash、22 例前后 trace_hash/events_hash/net_R 见 follower_hash_audit.json；没有新策略版本、没有修改旧策略名/登记哈希。

## 保持的语义与剩余风险

- 限价保留 participation/价点容量：离散 bar 无盘口深度，保留被动单容量约束避免另行扩大撮合假设；市价腿仍受合法数量、钱包/保证金约束，取消的只有分钟成交量上限。止损退出的既有容量规则也保留。
- 原 post-only 穿价拒绝、价格/时间优先、reduce-only、退出 latch、数量 step、风险分母、TTL 与到期/资金费同刻优先级、mark 最大陈旧时间保持。限价取整不另改已有定仓参考价算法。
- instrument_rules 整窗版本一致性/未知规则、全局 bars_quality_ok=False 的 fail-closed 和未体检/源版本不可验证的 bar 拒收保持；本轮没有实现历史规则动态切换或修复湖数据。
- funding 仅行内周期证据：周期切换边界允许前/后任一间隔；若一处缺行恰好与周期切换同样解释，仅凭行无法辨别，仍需更细的周期生效记录。稳定周期缺行及持仓期尾部缺行均强制删失；无周期上下文时沿用 8h。
- 1m 路径仍为 O-L-H-C / O-H-L-C 合成近似，不代表秒级盘口；固定止损滑点未用真实数据校准。未重放任何实际频道报告。
- B 仅保留旧合成审计 spike，研究策略显式拒绝；不要将 B 夹具结果当成此次跟单口径结果。

## Changed files

- `src/quant_lab/market/contract.py`
- `src/quant_lab/market/execution.py`
- `src/quant_lab/market/kernel_a.py`
- `src/quant_lab/market/l0_replay.py`
- `src/quant_lab/market/nautilus_adapter.py`
- `src/quant_lab/market/partition_check.py`
- `tests/integration/test_follower_lake.py`
- `tests/market/fixtures/build_episodes.py`
- `tests/market/fixtures/episodes/E01.json`
- `tests/market/fixtures/episodes/E02.json`
- `tests/market/fixtures/episodes/E04a.json`
- `tests/market/fixtures/episodes/E04b.json`
- `tests/market/fixtures/episodes/E04c.json`
- `tests/market/fixtures/episodes/E10.json`
- `tests/market/fixtures/episodes/E12.json`
- `tests/market/fixtures/episodes/E14a.json`
- `tests/market/fixtures/episodes/E17.json`
- `tests/market/fixtures/episodes/E18.json`
- `tests/market/follower_full_g1.log`
- `tests/market/follower_full_g2.log`
- `tests/market/follower_hash_audit.json`
- `tests/market/follower_mutation_results.json`
- `tests/market/follower_mutations.log`
- `tests/market/follower_mutations.py`
- `tests/market/follower_verification.md`
- `tests/market/test_asof.py`
- `tests/market/test_breakeven.py`
- `tests/market/test_close_stop.py`
- `tests/market/test_contract.py`
- `tests/market/test_execution_api.py`
- `tests/market/test_follower_execution.py`
- `tests/market/test_funding.py`
- `tests/market/test_kernel_a.py`
- `tests/market/test_l0_replay.py`
- `tests/market/test_nautilus_adapter.py`
- `tests/market/test_outcome_kind.py`
- `tests/market/test_review_fixes.py`
- `tests/market/test_review_p1.py`
- `tests/market/test_review_p1_round3.py`
- `tests/market/test_review_p1_round4.py`
- `tests/market/test_single_source.py`
- `tests/market/test_time_exit.py`

## 新增测试名

### tests/market/test_follower_execution.py

- `test_stop_intrabar_uses_stop_plus_adverse_tick`
- `test_stop_gap_uses_last_open_without_slippage`
- `test_stop_cost_scenario_slippage_and_common_path`
- `test_resting_entry_cross_fills_at_limit`
- `test_resting_entry_gap_fills_at_open`
- `test_resting_tp_cross_and_gap`
- `test_marketable_limit_is_taker_at_current_price`
- `test_marketable_tp_is_immediate_taker`
- `test_ioc_market_ignores_zero_minute_capacity`
- `test_entry_and_tp_tick_rounding_is_passive_and_inward`
- `test_illegal_entry_leg_does_not_reject_market_sibling`
- `test_plan_stale_includes_equal_stop`
- `test_quote_quarter_r_boundary_and_mark_sizing`
- `test_unpriced_market_and_limit_share_stale_gate`
- `test_bar_gap_only_excludes_still_open_trade`
- `test_unfilled_waiting_period_complete_is_zero_despite_later_gap`
- `test_evaluable_and_coverage_reason_match_summary`
- `test_funding_cycle_switch_and_missing_actual_settlement`
- `test_funding_missing_row_checked_only_while_holding`
- `test_minute_before_funding_has_closed_mark_and_no_position_is_not_stale`
- `test_gap_at_unfilled_expiry_is_outside_waiting_period`
- `test_invalid_tp_does_not_reject_other_protection_legs`
- `test_kernel_b_rejects_follower_research_policy`
- `test_funding_before_first_fill_does_not_need_a_mark`
- `test_close_stop_recovery_still_fills_next_open`
- `test_limit_submitted_before_first_price_is_resting_maker`

### tests/integration/test_follower_lake.py

- `test_loader_preminute_and_quarantined_funding_month_are_evaluable`
- `test_lake_quality_gap_and_missing_tail_only_affect_open_position`
- `test_lake_holding_period_missing_settlement_row_censors`

修改既有回归的文件及期望见 Changed files 与“旧预期更新”；原测试名除更准确的 funding/trace 标题外保留。

## 修改的既有测试名

### tests/market/test_asof.py

- `test_real_partition_mark_price_at`

### tests/market/test_breakeven.py

- `test_fee_adjusted_quarter_tp_then_breakeven_versus_original_stop`
- `test_quarter_tp_r_uses_initial_stop_risk`
- `test_partial_tp_fill_arms_breakeven_once`
- `test_tp_touch_without_fill_keeps_original_stop`
- `test_same_bar_stop_precedes_tp_and_p7_keeps_initial_stop`
- `test_follower_fixtures_match_new_gold_and_invalidate_old_traces`
- 原名 `test_old_fixtures_keep_events_net_r_and_fixed_build_trace`（由更准确的标题替换，见上）

### tests/market/test_close_stop.py

- `test_mark_stop_keeps_existing_wick_trigger_and_sequence`

### tests/market/test_contract.py

- `test_fixture_count_and_integrity`
- `test_diff_result_reports_first_event_diff_and_scalars`

### tests/market/test_execution_api.py

- `test_simulate_requires_market_and_checks_manifest`
- `test_load_market_from_lake_and_simulate_real_bars`

### tests/market/test_funding.py

- `test_incomplete_window_schedule_does_not_censor_without_missing_held_settlement`
- 原名 `test_incomplete_schedule_censors_even_when_some_rows_present`（由更准确的标题替换，见上）

### tests/market/test_kernel_a.py

- `test_scenario_interval_favorable_vs_adverse_same_bars`
- `test_filters_accept_at_boundary_and_reject_beyond`

### tests/market/test_l0_replay.py

- `test_follower_invalid_plan_counts_in_replay_exclusions_not_r`

### tests/market/test_nautilus_adapter.py

- `test_b_match_set_and_all_diffs_explained`

### tests/market/test_outcome_kind.py

- `test_rejected_not_merged_into_unfilled_expired`
- `test_stopped_takes_precedence_and_exit_legs_keeps_mixed_information`

### tests/market/test_review_fixes.py

- `test_s05_loader_unknown_quality`

### tests/market/test_review_p1.py

- `test_s08_multiplier_scales_pnl_fees_exposure`
- `test_s10_classifier_exc_first_and_predicate_bound`

### tests/market/test_review_p1_round3.py

- `test_s14_loader_unverifiable_source_rows_fail_closed`

### tests/market/test_review_p1_round4.py

- `test_s05_null_ohlc_valid_fails_closed`

### tests/market/test_single_source.py

- `test_differential_lake_grid`
- `test_differential_start`
- `test_differential_lake_public_interior_bar`

### tests/market/test_time_exit.py

- `test_no_tp_exits_at_horizon_with_realized_net_r`
- `test_short_direction_and_stress_slippage_use_market_exit_model`
- `test_tp1_then_remaining_position_exits_at_horizon`
- `test_missing_mark_or_funding_evidence_remains_unevaluable`
- `test_max_holding_precedes_same_timestamp_price_and_funding`

