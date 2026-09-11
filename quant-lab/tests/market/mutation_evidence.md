# G2 十审修复方突变证据

本文件由修复方生成，不是独立审查报告，不改变十审 fail 结论。

45 次独立生产源码突变。每次实际写源码、运行 pytest 得 RED、finally 按原字节还原、全 market 源码 SHA256 比对、运行同测试得 GREEN。
原始源码替换片段、逐次完整 pytest 输出、退出码及 SHA256 向量见 mutation_evidence.json；命令输出见 verification_round10.json。

| 测试名 | 生产源码注入缺陷 | 注入后 | 还原后 | SHA256 一致 |
|---|---|---|---|---|
| `tests.market.test_review_p1_round10::test_s29_subsecond_calendar_start_has_no_false_first_gap` | `S29-first-gap`（`partition_check.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s29_off_grid_never_covers_calendar[mixed-off-grid]` | `S29-range-only`（`partition_check.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s29_off_grid_never_covers_calendar[all-off-grid]` | `S29-range-only`（`partition_check.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s29_subsecond_end_includes_last_grid_point` | `S29-truncated-count`（`partition_check.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_outcome_kind::test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes[first-version]` | `S31-remove-output-gate`（`execution.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_outcome_kind::test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes[later-version]` | `S31-remove-output-gate`（`execution.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_outcome_kind::test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes[first-version]` | `S31-sanitized-copy`（`execution.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_outcome_kind::test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes[later-version]` | `S31-sanitized-copy`（`execution.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_outcome_kind::test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes[first-version]` | `S31-redacted-version`（`execution.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_outcome_kind::test_b16_b17_conflict_gate_is_wired_and_reports_conflicting_hashes[later-version]` | `S31-redacted-version`（`execution.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s38_policy_rejects_negative_latency` | `S38-remove-domain`（`contract.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s38_resolved_start_checked_for_both_spellings` | `S38-explicit-only`（`contract.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s39_loader_grid_return_controls_coverage` | `S39-discard-return`（`execution.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s39_loader_grid_return_controls_coverage` | `S39-inline-count`（`execution.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s39_loader_grid_return_controls_coverage` | `S39-wrong-helper-value`（`contract.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s33_aligned_calendar_equivalence[2024-01-01-1-1m]` | `S33-wrong-expected`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s33_aligned_calendar_equivalence[2024-01-01-1-15m]` | `S33-wrong-expected`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s33_aligned_calendar_equivalence[2024-02-29-1-1m]` | `S33-wrong-expected`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s33_aligned_calendar_equivalence[2024-02-29-1-15m]` | `S33-wrong-expected`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s33_aligned_calendar_equivalence[2024-01-31-1m]` | `S33-wrong-expected`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s33_aligned_calendar_equivalence[2024-01-31-15m]` | `S33-wrong-expected`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s33_aligned_calendar_equivalence[2024-02-29-1m]` | `S33-wrong-expected`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s33_aligned_calendar_equivalence[2024-02-29-15m]` | `S33-wrong-expected`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s33_aligned_calendar_equivalence[2023-02-28-1m]` | `S33-wrong-expected`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s33_aligned_calendar_equivalence[2023-02-28-15m]` | `S33-wrong-expected`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s33_aligned_calendar_equivalence[2024-12-31-1m]` | `S33-wrong-expected`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_review_p1_round10::test_s33_aligned_calendar_equivalence[2024-12-31-15m]` | `S33-wrong-expected`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_constants_effective::test_grid_math_single_source_and_exact_to_microsecond` | `modified-existing-first-point-sentinel`（`kernel_a.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_single_source_call_sites_match_registry` | `A24-real-caller-missing`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_single_source_call_sites_match_registry` | `A24-real-caller-extra`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_registry_gate_fails_when_mutated[missing]` | `A24-gate-missing-disabled`（`single_source.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_registry_gate_fails_when_mutated[extra]` | `A24-gate-extra-disabled`（`single_source.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `A24-real-inline-latency`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_lint_gate_fails_on_injected_violation[latency]` | `A24-disable-pattern-latency`（`single_source.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `A24-real-inline-int-seconds`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_lint_gate_fails_on_injected_violation[int-seconds]` | `A24-disable-pattern-int-seconds`（`single_source.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `A24-real-inline-duration-div`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_lint_gate_fails_on_injected_violation[duration-div]` | `A24-disable-pattern-duration-div`（`single_source.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `A24-real-inline-start-or`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_lint_gate_fails_on_injected_violation[start-or]` | `A24-disable-pattern-start-or`（`single_source.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `A24-real-inline-expiry-add`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_lint_gate_fails_on_injected_violation[expiry-add]` | `A24-disable-pattern-expiry-add`（`single_source.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `A24-real-inline-middle-grid`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_lint_gate_fails_on_injected_violation[middle-grid]` | `A24-disable-pattern-middle-grid`（`single_source.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `A24-real-inline-tail-grid`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_lint_gate_fails_on_injected_violation[tail-grid]` | `A24-disable-pattern-tail-grid`（`single_source.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `A24-real-inline-ttl-expression`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_lint_gate_fails_on_injected_violation[ttl-expression]` | `A24-disable-pattern-ttl-expression`（`single_source.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `A24-real-inline-ttl-branches`（`vision.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_lint_gate_fails_on_injected_violation[ttl-branches]` | `A24-disable-pattern-ttl-branches`（`single_source.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_foreign_hits_registry_is_exact` | `A24-foreign-freeze`（`single_source.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_single_source_call_sites_match_registry` | `S32-build-start-bypass`（`contract.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `S32-build-start-bypass`（`contract.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_single_source_call_sites_match_registry` | `S35-ttl-before`（`contract.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_single_source_call_sites_match_registry` | `S35-ttl-after`（`contract.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_single_source_call_sites_match_registry` | `S35-ttl-builder`（`contract.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_single_source_call_sites_match_registry` | `S35-expiry-A-timeline`（`kernel_a.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `S35-expiry-A-timeline`（`kernel_a.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_single_source_call_sites_match_registry` | `S35-expiry-A-entries`（`kernel_a.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `S35-expiry-A-entries`（`kernel_a.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_single_source_call_sites_match_registry` | `S35-expiry-B`（`nautilus_adapter.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `S35-expiry-B`（`nautilus_adapter.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `S37-middle`（`kernel_a.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |
| `tests.market.test_single_source::test_no_inline_reexpression_of_single_sources_anywhere_in_src` | `S37-tail`（`kernel_a.py`） | RED / failed / exit 1 | GREEN / passed / exit 0 | 是（全生产源码） |

## 真实修改范围

- contract.py：latency 非负校验、解析后的启动时刻检查、TTL/expiry helper、build_request 归一。
- partition_check.py：离网隔离、合法网格计数、缺口不变量；funding 间隔比较改用 timedelta 以满足树级时长除法禁令，未修改 execution 的 S34 8h 网格。
- kernel_a.py：TTL 到期委派；中尾网格委派。
- nautilus_adapter.py：TTL 到期委派。
- vision.py：expected_rows 委派。
- single_source.py：采用草稿结构的真实 AST 门、七项 helper 调用登记、七族禁令、冻结 G3 两处命中。
- test_outcome_kind.py：真实 result_row 注入冲突；删除旧 B16 的测试内 group_by 副本，由新行为测试覆盖。
- test_constants_effective.py：删除无效 loader 哨兵；保留既有网格与 A 首点断言，并重新证明该修改后的测试会红。
- test_review_p1_round10.py：阻断项行为回归和 S33 日历等价枚举。
- test_single_source.py：真实 AST 门验收及合成输入探针。
- mutation_round10.py / mutation_evidence.json / verification_round10.json / mutation_evidence.md：可复跑的突变工具与修复方证据。

## 范围外

S34、S36 保留 partial-P2；G3 的两处命中保持冻结。未改审查报告、contracts、research、taskList 或夹具。
AST 门防守登记的已知语法族，不声称能够证明任意改名、别名或混淆形式的语义等价。
