# review-G2-P1 必修闭合记录（**一审期文件，已被后续十轮取代**）

> ⚠ **时效声明（2026-09-11 补）**：本文写于**一审之后、二审之前**，只记录 S01–S13 在当时的闭合情况（那时 `pytest tests/market` 是 164 passed）。
> 此后审查进行到**十审**，S01–S39 的**当前**状态一律以下列两处为准，不以本文为准：
> - 各项当前状态：`docs/adr/review-G2-P1.md` 最新一轮判定表（当前为十审，文件前 487 行）
> - 各轮终裁与记录来源：`docs/adr/review-G2-P1-verdicts.md`（只追加台账）
>
> 保留本文是因为它记录了一审十三条的**具体改法落点**，这些落点后续各轮仍在引用；按契约 §9 A21「只追加不覆盖」不重写、不删除。
> 按 §13 A25 的同一道理——**文件名与抬头不得使读者误认证据的时效**——故加本声明而非默默更新内容。

> 对应 `docs/adr/review-G2-P1.md`（Codex 一审，2026-09-11，终裁 fail，必修 S01–S13）。
> 本文按条给出：改法落点 / 验收证据（测试名 + 实跑命令）/ 未闭合残余。全部证据可用
> `.venv-g2/bin/python -m pytest tests/market -q`（164 passed）与 `tests/market/test_review_p1.py`（13 条反例回归）复现。
> 内核版本：A `kernel-a-v0.2+<源码摘要>`，B `kernel-b-nautilus-1.227.0-spike-v0.2+nautilus1.227.0+<源码摘要>`。

| ID | 状态 | 改法落点 | 验收证据 | 残余 / 说明 |
|---|---|---|---|---|
| S01 TP 历史触及≠永久可成交 | **闭合** | `kernel_a.match_point`：TP 只在**当前 last 满足限价**时成交（`tp_executable`），`triggered` 只用于 tp_triggered 事件 | `test_review_p1.test_s01_*`：审查反例（qty2、T+30 105 容量1、T+60 100）只平 1、余仓 LABEL_RIGHT_CENSORED；short 镜像与首次触及零容量不设 latch | — |
| S02 funding 闭合 mark / 周期证据 / 重复冲突 | **闭合** | `closed_mark_at`：结算 mark = calc_time 前最后**已闭合** mark（显式点或 bar 的 C 点，不取新 bar 的 O）；同 calc_time 冲突费率 → `ContractError` 拒收，相同重复只入账一次，成功入账后记键；`funding_schedule_complete=False` 的持仓在入场后即 `FUNDING_SCHEDULE_GAP`（不再按固定 8h 网格猜）；B 同步（闭合 mark、冲突拒收、入场即删失、`flush()` 让无后续 tick 的结算行也入账） | `test_funding.py`（12 例：正负费率×多空、闭合 bar 100 vs 新 bar 110、冲突拒收、重复一次、陈旧、t_start 前忽略、8h→4h 链、schedule 不完整）；`test_review_p1.test_s02_b_*` | 真实 calc_time 与交易所入账时刻偏差仍是 U03（P2 真实数据敏感性）；`load_market_from_lake` 的 `funding_schedule_complete` 现要求 fundingRate 分区 manifest 存在且体检通过，仍不是区间完整性证明（体检只查相邻周期）→ 记为 P2 |
| S03 分钟内部启动 | **闭合** | `_expanded`：只展开 `open_time >= t_start` 的 bar；启动前最新已闭合 mark 作初始游标；B 同步 | `test_review_p1.test_s03_*`：t_start=T+10s 时 A/B 首次开仓均在 T+60 的 O；改动启动 bar 的 H/L/C 不影响首个入场 | — |
| S04 max_holding / TP 余量 | **闭合** | 首次开仓设 `hold_end`，到点持仓 → LABEL_RIGHT_CENSORED（v0 删失边界，ADR §12）；`tp_targets`：fractions 和为 1 时舍入余量给末档；`plan_qty` = 分配总量 | `test_review_p1.test_s04_*`：max_holding=10s 删失；qty1 两档 0.5/0.5 → 末档 1；sum<1 保留余仓 | — |
| S05 覆盖/规则隔离到执行入口 | **部分闭合** | 每时刻查 `effective_to`（过期即 SYMBOL_TIME_INVALID，保留此前事件）；入场前也要求 mark 新鲜；tick/step 缺 → RULE_HISTORY_MISSING；`load_market_from_lake`：manifest 必须存在且 `check_status∈{ok,gap}`、ohlc_valid=false/gap_flag/尾缺（按期望 bar 数）→ `bars_complete=False`、规则缺 tick/step 或窗口内版本切换 → `rules_known=False`，`quality_notes` 记录原因 | `test_review_p1.test_s05_*`（规则中途过期、首笔前无 mark、tick=0、未体检分区、尾缺、manifest_refs） | 规则**中途切换**当前一律 unsupported（拒绝而非切换）；quarantine severity=error 的行只通过 ohlc_valid/gap_flag 间接体现，未直接读 quarantine 表 |
| S06 silver 去重 / bronze 不可变 / 隔离 id | **部分闭合** | `vision.ingest_bytes`：完全相同副本折叠（`exact_duplicate_keys` 映射账），同键异值 → 两候选都剔出 silver 并进 quarantine（`quarantine.conflict_records`，object_version=行内容摘要，id 含 source_sha256）；旧 bronze 按 sha 改名保留（`retained_previous`）；`partition_check` 改用共享 `quarantine.py`，id 含 raw_hash | `test_review_p1.test_s06_*`：冲突键隔离、副本折叠、同源重跑不增、新源版本新记录且旧字节可取 | `check_partition` 仍读 silver（不重放 bronze），docstring 已改口；源端重复现在在入湖时处理并写 manifest |
| S07 队列 / post-only / 账户 | **部分闭合** | entry 按价格优先（买高先）+ accepted_seq；post-only 首个撮合机会穿价即 `rejected(POST_ONLY_CROSS)`；预留含 taker 费缓冲；每笔 fill 前按实际价重算可承担量，余量 `cancelled(MARGIN)`；`cash` 账本 + I10 守恒断言（wallet + realized − fees + funding） | `test_review_p1.test_s07_*`（穿价拒绝/非穿价成交/价格优先/跳空 200 只成交 5）；`kernel_a.result` 的 I10 断言 | 无管理命令流（C01 待 G0），改单失去优先级、同价改单等 E13 情景无法在 v0 请求模型里表达 → 标 unsupported |
| S08 multiplier | **闭合** | realized / unrealized / slippage / fee / funding / 预留全部乘 multiplier；`check_invariants(multiplier=)` 复算 gross | `test_review_p1.test_s08_*`：multiplier=2 时 gross 10、mfe 2、market 入场 fee 0.101、slippage 2；B 同步 | — |
| S09 as-of 等号候选 null | **闭合** | `asof_join`：等号候选行存在则整行采用（`__eq_hit`），不按单元格回退；非 UTC / naive → `TimeUnitInvalid`（不再自动转换） | `test_review_p1.test_s09_*`：审查反例 v=null、matched_at=T；naive 与 Asia/Tokyo 拒绝 | — |
| S10 解释器洗码 | **闭合** | `classify`：EXC/not_run 优先；人工码绑定 (episode, 首个 diff 谓词, 是否要求全部标量一致)，不命中 → UNEXPLAINED；`diff_result(all_diffs=True, ignore_reason=False)` 全枚举；`ab_report` 对每个 B 结果强制 `check_invariants`（失败→EXC→UNEXPLAINED）；B 暴露按 mark 点+last 点重放（E02 的 −1.2 已覆盖）；零容量/参与率未采用在报告 §5 明示 | `test_review_p1.test_s10_*`：已知 ID 注入未知异常/事件差/标量差 → UNEXPLAINED；E02 B mae_R=−1.2 | 引擎账户余额未直接读取（报告 §3 标 not_run） |
| S11 门禁与矩阵 | **部分闭合** | 新增 `test_funding.py`（12）、`test_replay.py`（跨进程/批次重排）、`test_review_p1.py`（13）；`test_kernel_a` 随机 40 例改为随机计划×随机行情（方向/梯档/IOC/多档/路径/政策随机）；M-08 verify 之外由 `ab_report` 返回码（UNEXPLAINED>0 → 非零）把关；G3 对接改为直接调用 `pair_arms`+`evaluate`（G3 侧两处缺陷已在看板告知） | `pytest tests/market -q` → 164 passed；`python -m quant_lab.market.nautilus_adapter report` → UNEXPLAINED=0 | E13（改单队列）随 C01 gated；M-08 的看板 verify 字符串本身仍只 grep 表头（看板由 G0 改） |
| S12 哈希绑定不可变输入 | **部分闭合** | `MarketView.manifest_refs`（partition_id/source_sha256/schema_hash/available_at_basis/check_rule_version）进 `manifest_hash`，`quality_notes` 不进；`kernel_version` = 版本串 + 内核源码 sha（B 另含 nautilus 版本），进 trace_hash；`KernelA.__init__` 重验 `policy_hash` 与当前注册表内容 | `test_review_p1.test_s12_*`；`test_replay.py` 跨进程一致 | policy 注册表仍是进程内 dict（跨修订版本名复用只能靠 (version,hash) 一一对应测试拦截）；`market_manifest` 字符串到版本化清单的解析（不可变数据集快照）留 P2 |
| S13 事件层不变量 | **部分闭合** | `_fill_event`：终态订单再成交 → `ExecutionInvariantError`；`finish_if_flat`：closed 前存活订单即错；`check_invariants`：终态后 fill/amended、closed 时兄弟腿未终态、I11 结算键重复、multiplier 复算；输入约束：价格/数量有限且 Decimal(38,12) 内、`max_holding_s>0`、`reduce_only_exit: Literal[True]`、risk_budget 有限 | `test_review_p1.test_s13_*`（终态后 fill、兄弟腿未终态、结算键重复、非法输入拒绝） | 共享容量 / 当前可执行价 / 预留不为负 等仍在内核内部路径保证，结果层不重放价点（需要行情才能核） |

## 残余风险（供 G0 裁定 P1）

1. S05/S06/S07/S11/S12/S13 为"部分闭合"：核心反例已修并有回归，但审查提出的完整验收集合（规则中途切换、bronze 重放体检、管理命令流、版本化清单解析、结果层价点重放）超出 v0 请求模型或依赖 C01/G0 裁定，已在表中逐项列出。
2. 候选 B 仍 12/22 MATCH，差异全部有解释码但 **B_COMMAND_LATENCY 是合同不通过**（ADR §9.1），B 不能作为正式内核；A 为参考实现出主数字。
3. 22 个夹具的独立期望未因本轮改动而修改（除重生成加入 policy_hash）；新增语义（max_holding、post-only、multiplier、schedule 缺证据）以 test_review_p1 的手算期望覆盖，未新增 JSON 夹具。

## 二审（fail，closed 1 / partial 12 / S14 open）后的处置 — 2026-09-11 第二轮

回归：`tests/market/test_review_p1_round2.py`（9 例，复现二审 C1–C3 与 S14）；全套 `tests/market` 175 passed；`nautilus_adapter report` UNEXPLAINED=0、A_GOLD_FAIL=0。

| ID | 二审缺口 | 本轮改法 | 证据 |
|---|---|---|---|
| S02 | loader 缺 funding 文件仍 complete=True；B flush 无 adjust_account | `load_market_from_lake` 按 8h 网格核对结算行，缺行 → `funding_schedule_complete=False` → FUNDING_SCHEDULE_GAP | `test_c2_loader_missing_funding_and_unknown_rules` |
| S04 | hold_end 未进时间线，先记 funding 再截断；B 未落实 | A：首次开仓插入 hold_end 时刻，hold/horizon 检查先于同刻 funding 与撮合；B：事后按 open_at+hold 截断事件并右删失，TP 余量末档 | `test_c1_s04_hold_end_precedes_funding_and_b_truncates` |
| S05 | 规则 status/min_notional None 伪装已知；全窗 bars_complete=False 起点清空 | loader：status≠TRADING / min_notional 未知 / 区间重叠 / 版本切换 → rules_known=False；kernel：bars 首个缺口作为时刻 → 逐时 BAR_GAP 保留此前事件 | `test_c2_loader_missing_funding_and_unknown_rules`（BREAK / None） |
| S06/S14 | 源包修订后旧 silver 日分区复活；docstring 失实 | `ingest_bytes` 把本 period 内新版不含的日分区改名 `part.<sha>.superseded.parquet`；loader 逐行 `source_sha256` 与 manifest 核对；partition_check docstring 改为"只读 silver，bronze 重放留 P2" | `test_s14_revised_source_supersedes_old_silver_days` |
| S07 | 额度只用 cash−成本保证金 | `affordable_qty` 用 equity（cash + 最新 mark 未实现）− 已用保证金 | `test_c3_s07_equity_based_affordability` |
| S08 | 公共 `execution.simulate` 二次不变量未传 multiplier；B 费用/暴露未乘 | 已传；B 原生费 ×multiplier，暴露重放 realized ×multiplier | `test_c1_s08_public_simulate_multiplier` |
| S09 | 同名 sequence 列退回旧值 | 右 seq 先改名 `__asof_rseq`；保留列名冲突拒绝 | `test_c1_s09_same_name_sequence_columns` |
| S10 | 只校首差；need_equal=False 项不核标量；b=None → MATCH | classify 改为：EXC/缺结果 → UNEXPLAINED/NOT_RUN；人工码 = 冻结的**完整差异键集合**必须完全相等 + 集合外标量全部相等（`ab_explanations.json`，绑定 KERNEL_VERSION）；`diff_result(all_diffs=True)` 长度差也报；报告用 ignore_reason=False；A 金标失败 → A_GOLD_FAIL；报告文字改为"引擎余额 not_run" | `test_s10_*`（篡改 closed.reason / 标量 → UNEXPLAINED） |
| S11 | 报告只凭 UNEXPLAINED 返回码；空集 | replay/report：空集或 A 失败均非零退出；新增 round2 回归 | CLI 返回码 |
| S12 | B 不重验 policy_hash；无跨修订登记；build id 不含共享 reducer | B 重验；`policy_hashes.json` 登记 version→hash，`resolve_policy` 校验（改内容不改名即拒绝）；A/B build id 含 `contract.py`，B 另含 nautilus dist-info RECORD 摘要 | `test_c3_s12_b_rejects_stale_policy_and_registry_pins_hash` |
| S13 | closed 用最终终态集；数量精度不全；take 未按 step | closed 当刻（事件序到此）兄弟腿必须终态，closed 后无订单事件；`check_decimal` 对价格/数量/分数/预算限定 Decimal(38,12)（整数位 ≤ 26，小数 ≤ 12）；`take()` 按 step 向下量化 | `test_c1_s13_closed_causality_precision_and_step` |
| S03(B) | B mark bars 未按 t_start 过滤 | B 的 bars_mark 只展开 open_time ≥ t_start，启动前只保留已闭合 C 点 | `test_c3_s03_b_mark_bars_filtered_by_t_start` |

仍列 P2 / gated：规则中途切换（当前 unsupported 拒绝）；bronze 深度重放体检；管理命令流与 E13（C01）；B 引擎账户余额直读；版本化湖快照解析；M-03 网络 verify 由 G0 实跑（本地 manifest/bronze sha 已一致）。

## 三审（fail：closed 4 / partial-P2 8 / open S05、S14；新增 S15、S16）后的处置 — 2026-09-11 第三轮

回归：`tests/market/test_review_p1_round3.py`（6 例）；全套 `tests/market` 180 passed。

| ID | 三审缺口 | 本轮改法 | 证据 |
|---|---|---|---|
| S15（归 S05） | 两流缺口用 `or` 取第一个非空而非最早；质量失败被可定位时间洞覆盖 | `timeline`：last/mark 两流的首个缺口取 **min**；`_first_bar_gap` 覆盖首缺（首根 bar 晚于 t_start 所在网格）/内部洞/尾缺（last+iv < horizon）；窗口内无 bars 的流不按网格判缺（显式点驱动）；`MarketView.bars_quality_ok`（来源/体检/OHLC/manifest 失败）为 False → 起点即 BAR_GAP，不再降为时间洞 | `test_s15_earliest_gap_across_streams`（mark T+60 缺、last T+240 缺 → T+60 删失，T+120 TP 不成交；反序同）、`test_s15_head_and_tail_gaps`、`test_s15_quality_failure_censors_at_start_even_with_locatable_gap` |
| S14 余项 | loader 遇 `source_sha256` 缺列/null 放行；旧 sha 行 + 后续 gap 仍成交 | loader：缺列/null/不符/manifest 缺 → `bars_quality_ok=False`（fail closed）；`ohlc_valid` 缺列同样质量失败；质量失败起点删失不受后续可定位缺口影响 | `test_s14_loader_unverifiable_source_rows_fail_closed`（null 行 / 缺列 / 旧 sha+gap 三变体均起点删失） |
| S16（P2/B，归 S04） | B 截断事件后暴露仍遍历到 horizon | B 暴露评估点截止到 min(最后事件 ts, open_at+max_holding−1µs)；截止后 mark 改动不影响 A/B 暴露与事件 | `test_s16_b_exposure_respects_hold_cutoff` |

三审标 partial-P2 的 8 条（S02/S04/S06/S07/S10/S11/S12/S13）按三审判定留 P2，本轮未再扩展。

## 四审（fail：closed 6 / partial-P2 9 / open S05、S17）后的处置 — 2026-09-11 第四轮

回归：`tests/market/test_review_p1_round4.py`（2 例）；全套 `tests/market` **183 passed**；A 22/22；report UNEXPLAINED=0。

| ID | 四审缺口（实跑反例） | 本轮改法 | 证据 |
|---|---|---|---|
| S05（open→已修） | `ohlc_valid` 改 null 后 `bars_complete=True`、net=5、censor=None：polars `.any()` 跳过 null，"未知质量"被当合格 | loader 显式 `is_null().any()` + `fill_null(False)`：缺列 / null / false 三者一律 `bars_quality_ok=False`（起点删失） | `test_s05_null_ohlc_valid_fails_closed`（null 与 false 两变体均 BAR_GAP、fill_status=none；基线质量已知时仍通过） |
| S17（新，open→已修） | 显式 `entry_fractions/tp_fractions` 成为绕过 policy 的第三种分配来源（同 policy_hash 下 TP 分配 1 与 .5 都被接受，A 净利 10→删失）；显式值未过 `check_decimal`，`.5000000000001` 被接受而 batch 截成 12 位 | `ExecutionRequest` 校验新增：逐值 `check_decimal`（Decimal(38,12)）＋ 与 `resolve_fractions(plan, resolve_policy(policy_version))` **逐值相等**（作者给值与 policy 兜底统一走这一条）；B8 验收 (b) 改用**已登记**政策 `fixture-halftp-v1`（只差 `tp_total_fraction`），不再 `model_copy` 绕登记 | `test_s17_explicit_fractions_cannot_bypass_policy_or_precision`（三组伪造分配、两组超精度均 ContractError；作者显式给值仍须逐值相等；batch 往返逐值等于参与 trace_hash 的请求值） |
| S16（partial-P2，按四审维持） | B 按"最后事件"截断暴露，丢失窗内合法 mark（X16 的 T+5=102，.4R） | 按四审裁定维持 P2/B：B 是失败 spike，不作合格标签内核；改为"按真实平仓/删失/hold/horizon 截止重放"排入 P2 | 四审 §S16 |

另按契约 §9.9 A7 与 G0 两次点名：`taskList.json` 的 M-10 verify 由负向 `! grep -q '必修.*未闭合'`（报告写 insufficient/fail 也 rc=0 的假绿）改为**取最后一条终裁行正向判读**（轮次名不写死）；改动经新增的 `scripts/task.py set-verify`（同目录锁 + 原子替换 + `verifyHistory` 留痕），未手改 JSON。自检：当前 fail → rc=1；`pass` → rc=0；`insufficient` → rc=1。
