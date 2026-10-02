# trader-v3 可选执行口径验收记录

已完成四个 live 策略的纯函数、L0 贯通、登记、审计记录与合成测试。未联网、未提交 git；生产文件只读取 `git show HEAD:<path>`。规则来源 commit：`15d2323733e0213b8c3655faa55f8b367cf6233e`。

## Changed files / 实现位置

路径均相对 quant-lab。

| 文件 | 改动 |
| --- | --- |
| `src/quant_lab/market/live_profile.py:72` | 按物理行、角色句、点位、品种/方向定位措辞；缺失或歧义按精确点位执行 |
| `src/quant_lab/market/live_profile.py:130` | 独立纯函数：入场/止损/止盈让点、zone 三档、等名义双点、方向取整；不修改输入 |
| `src/quant_lab/market/l0_replay.py:121` | 构造 ExecutionRequest 前变换计划副本，历史 tick 缺失时保持规则缺失删失 |
| `src/quant_lab/market/l0_replay.py:227` | live 请求新增 `trades.live_execution_json`；原始 entries/stop/targets 保持原值 |
| `src/quant_lab/market/l0_replay.py:253` | summary 记录全部规则触发计数、无法解析原文/规则的计数与生产引用；不输出原文 |
| `src/quant_lab/market/contract.py:344` | 沿用续做前已有的 13 行：默认关闭且不进入旧策略内容，登记四个新策略 |
| `src/quant_lab/market/policy_hashes.json` | 登记四个新哈希；旧 11 个哈希不变 |
| `src/quant_lab/data/api.py:92` | 必要的最小越界改动：只读 bronze 精确 source_version_id 的 text；不修改解析规则 |
| `tests/market/test_live_profile.py` | 50 个纯函数参数化合成用例 |
| `tests/market/test_live_replay.py` | 40 个登记、字节一致性、根版本、L0、内核 A 与缺规则合成用例 |
| `tests/market/live_profile_mutations.py` | 临时源码副本、全新进程、仅相关测试选择器的 23 个变异 |
| `tests/market/live-profile-mutation-evidence.json` | 23/23 检出；包含测试名、变换、原源码哈希与失败输出；哈希已核对最终文件 |
| `tests/market/live-profile-off-evidence.json` | 五个旧策略 × 三个 L0 输出文件的逐字节对照与 SHA-256 |
| `tests/market/live-profile-validation.md` | 本验收记录 |

四个新策略：`base-v1-timeexit-live`、`base-v1-timeexit-be1-live`、`base-v1-timeexit-w60-live`、`base-v1-timeexit-w60-be1-live`。分别与对应旧策略只差版本名和 live 开关；已有 be1 行为保留。

## 实盘规则引用（均为已提交版本的 file:line）

路径相对 trader-bot；行号属于上述 commit，不属于生产文件的未提交工作区版本。

| 引用 | 核对结果 |
| --- | --- |
| `hermes-profile/skills/trading/v3-trader/SKILL.md:19` | 模糊 zone/点位加 entry-offset；精确点位不让利；zone 风险份额 55/30/15 |
| `hermes-profile/skills/trading/v3-trader/SKILL.md:27` | 模糊突破 0.3%；入场 0.1%；止损再次外扩 0.1%；TP 向成交方向 0.1%；按有利成交/避免早触发方向取 tick。v2（10-02 用户确认）：止损、止盈的 0.1% 与入场一样只在该点位带「附近/左右/大约/约」或突破措辞时生效，精确点位原值执行。v3：模糊词挂在价格数字上即算，不要求「入场/止损/目标」标签（「现价:58800附近」「等106660附近多」「约$71,250」「875附近」=87500、「6.2和6.3万支撑附近」）；有标签句时只看该句里挂在这个价格上的词；入场按档判断 |
| `hermes-profile/skills/trading/v3-trader/SKILL.md:34` | 双明确点位共享总风险预算、等名义金额 |
| `hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py:475` | Decimal 入场偏移，多上移/空下移；market 无可平移的挂单价格 |
| `services/control-plane/api/read_api.py:547` | 近端 `t1_near`: 深度 0%，风险 55% |
| `services/control-plane/api/read_api.py:548` | 中档 `t2_mid`: 深度 50%，风险 30% |
| `services/control-plane/api/read_api.py:549` | 深档 `t3_deep`: 深度 85%，风险 15% |
| `services/control-plane/api/read_api.py:746` | 空单近端为下沿、远端为上沿；多单近端为上沿、远端为下沿 |
| `services/control-plane/api/read_api.py:777` | 档位价格 `near + depth * (far - near)` |
| `services/control-plane/api/read_api.py:770` | 单档基准量 `max_notional / near`，基准总风险为量乘近端止损距离 |
| `services/control-plane/api/read_api.py:781` | 每档 `q_i = R * w_i / abs(p_i - SL)`；各档名义 `N_i = q_i * p_i` |
| `services/control-plane/api/read_api.py:790` | 总名义超 max_notional 时各档共同缩放；之后数量向下取整 |
| `packages/execution-domain/execution_domain/entry_batch.py:28` | 双明确点位的参考价格为调和平均数 |
| `packages/execution-domain/execution_domain/entry_batch.py:35` | 每腿名义 `N/2`，数量 `(N/2)/p_i`（第 41 行向下取整），风险为 `q_i * abs(p_i - SL)`（第 53 行） |
| `services/nautilus-node/strategy/intent_execution_planner.py:1004` | 通用节点价格取整实际为 ROUND_HALF_UP，见剩余差异 |

回测 entry.fraction 是数量份额，不能直接填入 55/30/15。zone 使用归一化 `w_i / abs(p_i - SL)`，双点使用归一化 `1/p_i`；份额量化至 12 位、末腿吸收余量。内核 A 再按现有共同风险预算和数量步长定量。变换记录包含实际执行计划、档位顺序、风险与数量份额；summary 说明数量换算及计数单位。

## 测试名与逐条变异

| 规则 | 主测试名 | 变异结果 |
| --- | --- | --- |
| 入场仅模糊让利、多空方向 | `test_entry_concession_only_for_fuzzy_wording`、`test_fuzzy_pair_moves_both_entry_legs` | 去掉让利、反转方向、精确点位也平移均检出 |
| 句子/点位定位 | `test_unresolved_or_unrelated_wording_is_exact`、`test_one_line_roles_do_not_leak_and_line_numbers_are_audited`、`test_opposite_opening_does_not_enable_entry_concession` | 去掉价格锚、忽略歧义、忽略品种均检出 |
| 止损 0.3% 后再 0.1%；「附近」只放宽 0.1%；精确不动 | `test_stop_breakout_then_widening` | 去掉突破、突破反向、去掉放宽、放宽反向、精确也放宽均检出 |
| 止盈 0.1%（仅模糊措辞，每档看自己的句子） | `test_take_profit_concession`、`test_each_target_uses_its_own_wording` | 去掉让利、反转方向、精确也让利均检出 |
| 无标签写法、按档让点、长句不串价 | `test_wording_attached_to_the_price_needs_no_label`、`test_level_wording_cases`、`test_only_the_fuzzy_leg_gets_the_entry_concession`、`test_run_on_clause_wording_stays_with_its_own_price` | 忽略挂价措辞、各档共用一个判断、整句措辞串到别的价格均检出 |
| tick 定向取整 | `test_tick_rounding_toward_fill_and_later_stop` | 反转取整方向检出 |
| zone 深度/顺序/风险换算 | `test_zone_depth_order_and_risk_shares` | 深度 85% 改为 100%、近深风险交换、风险份额直接当数量份额均检出 |
| 双点等名义 | `test_explicit_pair_equal_notional` | 改为等数量检出 |
| 市价与关闭开关 | `test_market_reference_and_disabled_are_unchanged` | 市价参考价平移、关闭仍变换均检出 |
| 根 source_version_id | `test_bronze_accessor_uses_exact_root_versions_and_rejects_ambiguity` | 取消精确版本过滤检出 |
| L0 贯通/summary | `test_four_live_policies_construct_and_run_kernel_a`、`test_l0_live_audit_and_summary_use_root_version_without_changing_source` | 构造请求时使用原计划、summary 计数归零均检出 |
| 策略内容哈希 | `test_old_hashes_and_disabled_policy_bytes_are_pinned` | 新策略漏掉 live 哈希字段检出 |

其他验收：`test_disabled_request_and_kernel_result_bytes_are_identical` 对旧 11 个策略比较请求和完整内核结果字节；`test_four_live_policies_construct_and_run_kernel_a` 覆盖四策略 × 多空 × 单点/zone/双点，包含真实 be1 保本事件和定量风险/名义核对；`test_l0_off_has_no_new_columns_or_source_reads` 禁止关闭口径读取原文或额外规则；`test_missing_tick_keeps_kernel_rule_censor_and_empty_live_output` 覆盖缺规则删失和空结果；`test_stop_trigger_and_explicit_tp_sizing_are_preserved` 保留 close 止损、TP 比例、定量与到期语义。

## 完整测试结果行与失败复验

所有输入均为合成夹具。未设置 QUANT_LAB_SYNTHETIC_ONLY。全套两组各运行一次；变异只运行相关测试选择器。

```text
相关初次回归（live_profile/live_replay/l0_replay/contract）：126 passed in 41.32s
最终 live_profile/live_replay：90 passed in 10.41s
最终变异：23/23 mutants killed
G2 全套 tests/market tests/integration：24 failed, 667 passed, 2 skipped in 46.00s
G1 全套 tests/data：620 passed, 1 warning in 44.65s
G2 失败文件及新增测试定向复验：237 passed in 14.67s
```

G2 全套没有被记为通过。首轮 22 个失败由临时 cwd 缺少相对路径 `tests/market/fixtures/episodes` 引起，已通过只映射合成 tests 目录修正；2 个失败来自新增 derived_t_start 调用未登记，已删去不必要调用，按 t_dec as-of 查 tick（四个 live 策略 latency_s 都为 0）。保持原单一来源门与原断言。随后复验了全部失败所在文件 `test_force_close_net_r.py`、`test_outcome_kind.py`、`test_review_fixes.py`、`test_single_source.py`、`test_time_exit.py`，以及两个新增测试文件，全部通过。按全套各一次约束，未重复运行整个 G2。

两个自然跳过是既有真实分区测试 `test_real_partition_mark_price_at`、`test_load_market_from_lake_and_simulate_real_bars`；隔离 cwd 中没有真实行情湖，符合仅合成数据约束。G1 唯一警告是既有 async sessions 实验性支持警告。

原始日志：`/tmp/quant-live-full-g2/pytest.log`、`/tmp/quant-live-full-g2/targeted-recheck.log`、`/tmp/quant-live-full-g1/pytest.log`。测试从隔离 cwd 执行，使用项目绝对路径 pyproject/tests 与对应 .venv；只映射 tests，未映射真实 data。

关闭口径的五个 L0 旧策略分别为 `base-v1`、`base-v1-timeexit`、`base-v1-timeexit-be1`、`base-v1-timeexit-w60`、`base-v1-timeexit-w60-be1`。与 git show HEAD 的旧 L0、使用同一当前 contract/kernel 构建，对照 15 个文件，均逐字节一致。原始 plan 不变已由纯函数输入副本断言、L0 原始列与发布图不变断言验证。

## 保留的语义、必要改动与剩余风险

- 必要最小改动只有上述 market 功能、测试及 data/api 的只读原文入口；旧断言没有更新。续做前 contract 的 13 行沿用。工作区其他会话的 cx_batch/followup 和生产代码改动均保留，未纳入本次交付。
- 市价没有可平移的挂单价格，保持 market_ref 的 as-of 定仓与内核市价成交；过时报价转限价规则保持。close 止损周期、已有 be1、TTL、TP 比例、固定数量与总体风险预算语义保持。
- tick 方向按用户本次明确要求及 SKILL 第 9 条：多单入场 ceil，空单入场 floor；多单 SL/TP floor，空单 SL/TP ceil。已提交节点的通用函数却是 HALF_UP，因此不能宣称与该通用函数逐 tick 完全相同。
- 分档位置与风险/名义相对分配与提交版本一致；数量保留内核 A 现有“总量先 floor_step、各腿再 floor_step”的规则，可能与生产逐档取整有一个或数个 lot 的差异。生产的 max_notional 共同缩放、窄区间/已穿透门禁与追价行为未增加到本次 plan 变换；现有 wallet/杠杆/成交模型保留。
- 措辞必须挂在计划里的那个价格上（或在含该价格的唯一标签句里挂在它上面）；点名其他币种的行、反方向开仓的行不算。真实原文复核（10-02，六频道回放开仓）：峰哥入场 368/676、舒琴入场 377/429、坚果 9/31（有价格的）判为模糊；Titan、高卢人、Cash 多为翻译体「区域/之间」，极少带模糊词。跨行无角色、多个同价候选、品种或方向冲突、缺根原文保持精确；可能漏掉需要人工语义理解的模糊表达。混合多 zone 计划拒绝并按既有计划契约错误计数，不默猜分配。超过双明确点位的计划保留既有比例语义。
- 价格外移后保护点位冲突或 tick 取到非正数时按契约排除，不自动更改目标或吞掉错误。历史 tick 缺失保留 RULE_HISTORY_MISSING。
- 字节对照的边界是同一构建下旧/新 L0。跨完整 HEAD 的 contract 源码改变会按既有 kernel_build_id 改变构建身份/trace_hash；没有冻结或绕过这项来源身份规则。
