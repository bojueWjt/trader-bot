# G2 P1 独立审查：M-03～M-09

- 日期：2026-09-11；审查者：当前 GPT-6 主控会话，未调用其他模型。
- 终裁：**fail**。下列 S01～S13 共 13 条必修未闭合；测试全绿也不能替代经济语义验收。
- 范围：工作区实际产物，而非仅 HEAD diff；HEAD 为 `a383d8597bbc0d338d5443407b4136f4fb02c140`，多项产物尚未提交。
- 本会话只新增本文件。未运行下载器、夹具写出 main、A/B 报告生成器；未联网、未访问生产、未调用收费模型。
- pytest 使用 `PYTHONDONTWRITEBYTECODE=1`；仓库 pytest 配置已禁用 cacheprovider。测试自身的合成湖写入 pytest 临时目录，不改仓库行情湖。
- 已读：根 AGENTS.md、GOAL-2、execution-interface、M-01 R1～R11、ADR-G2、A/B 报告、market 全部模块、tests/market、22 个 JSON、build_episodes.py、taskList.modules.market。
- 采用 code-review 的规范/实现两轴，但用户要求的工作区范围、单文件输出、不调用外部模型优先于技能的 diff/委派流程。
- **并发边界**：审查期间其他会话修改了 contract、execution、kernel A/B、夹具、测试和 G3。以下明确区分初读、复跑和静态证据；本报告不是对整棵移动工作区的原子快照签名。
- 初读缺失的 policy_hash / instrument_id 校验 / build_request 已在后续读取中补入，故不再列“字段缺失”；重复 funding 也已补同时间键去重，旧反例只作为历史证据。

## 1. 逐任务 verify 实跑记录

工作目录为 quant-lab；命令来自 taskList.json。表内 pytest 的 Python 均为 `.venv-g2/bin/python`。
除明确跳过的 fetch 外，逐任务执行了获授权部分；退出码按实际进程记录。
pytest 空收集通常退出 5，但全 skip 仍可退出 0，不能把退出 0 等同于业务矩阵非空。

| 任务 | 实际执行命令 / 获授权部分 | 输出与退出码 | 非空/非零门禁评价 |
|---|---|---|---|
| M-03 | `python -m pytest tests/market/test_vision.py -q` | `11 passed in 5.23s`，0 | test_full_month_ok_manifest 明确 actual_rows=44640、>40000、31 日分区；不是只测文件存在 |
| M-03 | 原 verify 中 `python -m quant_lab.market.vision fetch --symbol BTCUSDT --type markPriceKlines --interval 1m --month 2024-01` | **未运行**：会联网并写湖，超出本次授权；完整原命令验收为 insufficient | 不能拿历史下载或 MockTransport 冒充本次真实下载 |
| M-03 | 原尾部 glob/load/assert `m['actual_rows']>40000`，只读实跑 | 44640，0；manifest 摘要见下 | 确实断言非零，但只信 manifest，自身不核 silver/bronze 内容一致 |
| M-04 | `python -m pytest tests/market/test_partition_check.py -q` | `13 passed in 0.73s`，0 | 注入缺口、重复、OHLC、尖刺并断言原因码/保留行；端到端用例没有重复键输入 |
| M-05 | `python -m pytest tests/market/test_asof.py -q` | `11 passed in 0.08s`，0 | 非空合成输入，断言值/等号/晚到/121s；缺 null 等号候选和未来修改性质 |
| M-06 | `python -m pytest tests/market/test_contract.py -q && test $(ls tests/market/fixtures/episodes/*.json \| wc -l) -ge 10` | 初次 `15 passed in 0.06s`；字段更新后 `17 passed in 0.06s`，均 0；JSON 数=22 | 明确 ≥10，且测试加载全部 JSON、查唯一 id、非空 derivation、gold 自洽；不证明 ADR 每个 E 编号覆盖 |
| M-07 | `python -m pytest tests/market/test_kernel_a.py -q && python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A \| grep -E 'passed=(1[0-9]\|[2-9][0-9]) failed=0'` | `71 passed in 0.20s`；`kernel=A passed=22 failed=0`，0 | grep 要求 10～99 通过，非零；未锚定整行且管道未启 pipefail；CLI 自身空集会返回 passed=0 failed=0/0 |
| M-08 | `test -f docs/adr/report-G2-kernel-AB.md && grep -qE 'episode.*diff' docs/adr/report-G2-kernel-AB.md` | 无 stdout，0 | **不验证任何 episode 结果**，只有表头也能过，不验证报告新鲜度、非零数量、不变量 |
| M-09 | `python -m pytest tests/market/test_execution_api.py -q` | 初次 `5 passed, 1 skipped in 1.61s`，0；后续见并发复跑记录 | df.height 等断言存在，但初次真正 G3 evaluate 被 skip，不能报完整对接通过 |

M-03 本地 manifest：actual_rows=expected_rows=distinct_keys=44640，missing=duplicates=0，status/check_status=ok。
source_uri=`https://data.binance.vision/data/futures/um/monthly/markPriceKlines/BTCUSDT/1m/BTCUSDT-1m-2024-01.zip`。
source_sha256=`1607b1522928d2a698592019fef7c3fcc9bee2cd01674c72a27e7fc028fd66ca`；实际只读计算 bronze ZIP sha256 与之相同。
quarantine_n=87，原因全部 PRICE_SPIKE_FLAG，符合“尖刺标记不删除”；下载时间是历史记录，不是本次网络证据。

### 全量与 replay 输出

首轮 `.venv-g2/bin/python -m pytest tests/market -q`：`137 passed, 1 skipped in 7.91s`，退出 0。
字段与 funding 测试并发更新后的复跑：`1 failed, 148 passed in 7.51s`，退出 1。
该失败为 `test_g3_evaluate_consumes_frozen_contract_output`，`ASTRejected: UNKNOWN_FIELD at $: 'f_x'`。
M-09 单独复跑先得到 `1 failed, 5 passed in 2.36s`（同错误），再得到 `1 failed, 5 passed in 2.40s`。
后一轮异常为 `ColumnNotFoundError: unable to find column "t_dec"; valid columns: ["episode_id", "cluster_id"]`，栈落 G3 freeze_opportunity_set。
运行时文件继续变化，pytest traceback 展示的源码行与已加载字节码出现不一致；不能把这些轮次当作同一固定版本的结果。
最后完成的一轮全量：`1 failed, 148 passed in 8.26s`，退出1；同一G3冒烟抛 `EvalProtocolError: execution 含多个 policy_version ['fixture-tick-v1', 'fixture-wallet99-v1', 'fixture-zero-v1']，须显式指定 baseline/candidate`。
收尾再读测试发现其他会话已改成筛选 fixture-zero-v1，并对 evaluate 的 TypeError 使用 pytest.xfail；这份新改动没有本次完整验证证据。xfail不能算真正evaluator通过，也不能据此关闭S11。

独立执行两条授权 replay（默认 repeat=2），首轮及字段更新后的复跑结论相同：

```text
A: E01 E02 E03 E04a E04b E04c E05 E06 E07 E08 E09 E10 E11 E12 E14a E14b E14c E15a E15b E16 E17 E18 -> ok, replay=True
kernel=A passed=22 failed=0                 exit=0
B: E01 E03 E04b E04c E05 E07 E14a E14b E14c E15a E15b E16 -> ok, replay=True
B: E02 E04a E06 E08 E09 E10 E11 E12 E17 E18 -> FAIL, replay=True
kernel=B passed=12 failed=10                exit=1
```

B 首差：E02 event[11].ts 20s→21s；E04a event[10].price 110→105；E06 accepted 时间后移；E08 cancelled→submitted；E09 expired→filled；E10 cancelled→tp_triggered；E11 accepted→submitted；E12 accepted→submitted；E17 entry price101→100；E18 accepted→stop_triggered。
报告中的 MATCH 集与实跑一致；“全部差异已解释”不等于两内核都满足合同。

## 2. 必修问题（位置 / 问题 / 改法 / 验收）

### S01 必修：TP 的历史触及状态被当成永久可成交条件

- 位置：`src/quant_lab/market/kernel_a.py:381` 的 TP 循环、run 的 P5；`contract.py:422` check_invariants。
- 问题：TP 一次 triggered 后，后续每个 last 点只查 triggered，不再查 last 是否满足限价；`max(limit,last)` 为多仓虚构了可执行价格。
- 实跑反例：E03 改固定量2，last=(T,100,容量2)、(T+30,105,容量1)、(T+60,100,容量1)，mark 保持100；输出两笔 TP 都105，net=10、未删失。第二笔当前 last=100，根本不能卖105。
- 改法：每次限价 fill 重查当前价点可成交性；触及事件与工作限价单状态分离；不变量加入真实价点/限价/容量核对。
- 验收：上述反例只平1，余仓删失；long/short、首次触及零容量、部分成交后回撤均有独立期望。

### S02 必修：funding 的闭合 mark、周期缺证据与重复冲突仍不符合 ADR

- 位置：`kernel_a.py:168` timeline、`:444` settle_funding；`nautilus_adapter.py:385`；`execution.py:157`。
- 问题：funding 使用合成 mark 点 `ts<=calc_time`，包含新分钟 O，而 ADR §6 指定最后已闭合 bar close；不完整 schedule 仍固定补8h网格；仅目录存在就把 funding_schedule_complete 设 true。
- 实跑：E05 结算前一分钟 bar 全100，结算时新 bar 全110，实际 funding price110/cash=-0.11；规范应取前一分钟 close100/cash=-0.1。
- 实跑：E07 删除12:00行、schedule_complete=False，仍输出 funding=+0.1、net=1.1、censor=None；没有周期证据时不能判 funding_ok=true。
- 初轮同一 E05 funding 行重复会扣两次（-0.2）；后续代码增加 ts 集合去重，重复扣款已修。但同 calc_time 的冲突费率会静默 first-wins，仍违背同快照重复键拒收；applied 标记也早于校验/实际入账成功。
- B 的 funding 回调仅随 last tick 推进，process 为 pass；无下一 tick 时到 horizon 的资金费可能漏结，不是独立定时器。
- 改法：独立闭合 mark 视图、历史 schedule 与区间完整性证据；重复输入拒收、重复已应用回调幂等；成功入账后记键。
- 验收：新旧 bar 结算边界、8h→4h 缺首/中/尾行、冲突重复、无成交 tick 到期结算、q(t−) 开/平仓同刻、正负费率与钱包舍入，A/B 分别断言。

### S03 必修：分钟内部启动仍消费该分钟的合成极值

- 位置：`kernel_a.py:145` timeline；`nautilus_adapter.py:127` 行情展开。
- 问题：先展开整根 bar，再按价点 ts 过滤。ADR §3 要求 t_start 在 bar 内部时从下一完整 bar O 开始，避免使用此前可能已发生的极值。
- 实跑：E04a t_start 改为 T+10s，A 的 position_open_at=T+20s，仍在 T 所属 bar；不是下一根 T+60s。
- 改法：bar 输入先过滤 `bar.open_time >= t_start` 再展开；真实有序逐笔可按点过滤，两种输入明确区分。A 启动前最新有效 mark 可作初始游标，不能丢掉或回填未来值。
- 验收：分别修改包含启动时刻 bar 的 H/L/C，A/B 均不影响启动后的首个合法入场；last 与 mark 各自异步测试。

### S04 必修：max_holding_s 和 TP 舍入余量被忽略

- 位置：`contract.py:130` Expiry、`:275` build_request；`kernel_a.py:253` protect、timeline；B `_protect` 与 end。
- 问题：max_holding_s 仅在 builder 默认 horizon 的 TTL+hold 中出现，内核没有以实际开仓时间设置持仓终点；显式 horizon 可完全覆盖掉该限制。
- 实跑：E03 max_holding_s=10，仍 T+60 TP 平仓、net=5、censor=None；应 T+10 删失。
- 另一反例：固定量1，两个 TP fraction=.5/.5，step=1，protect 把两腿都舍成0，最终 LABEL_RIGHT_CENSORED；ADR §4.2 要把 sum=1 时余量分给末档。
- 改法：按首次实际开仓+hold 建终点；TP 分配末档补差；entry 分数舍入后的有效计划量也需与分配总量一致。
- 验收：提前/延迟/部分入场、horizon 早于/晚于持仓上限、1单位多档止盈、sum<1 保留余仓均逐事件手算。

### S05 必修：coverage/规则隔离没有落实到执行入口

- 位置：`execution.py:109` load_market_from_lake；`kernel_a.py:471` run；`partition_check.py:216,287`。
- 问题：loader 不检查 manifest check_status/quarantine、ohlc_valid、重复冲突、实际网格首尾完整性；只看 gap_flag 和文件存在。尾部缺 bar 无下一行承载 gap_flag，可能被当完整；已有文件但过滤后0行也 complete=True。
- rules 只取起点一行，缺 tick/step/min_notional 用0替代并仍 rules_known；体检整月只用 rule_start 或 rule_end 的 tick；不处理规则中途切换/区间重叠/状态。
- 实跑：E03 rules.effective_to=T+20s，仍在 T+60s 成交并输出完整 net=5；起点 P0 校验不等于每时刻 P0。
- mark 新鲜度只在已有仓位且有 last 时查；无 mark 的首笔 entry 仍成交，后一个 last 才删失。
- 改法：显式解析质量与规则版本证据；缺证据保留机会行并删失，不能填0放行；当前时刻验证规则/mark，保留阻断前合法事件，不因未来分区缺口在起点清空历史。
- 验收：隔离 OHLC、schema drift、尾缺、空文件、未知精度、规则过期/变更、mark 首笔缺失；不得有未获证据支持的正常 fill/净R。

### S06 必修：silver 去重与 bronze 覆盖破坏隔离和来源可追溯

- 位置：`vision.py:370–373` ingest_bytes；`partition_check.py:202,147,379`。
- 问题：silver 按时间键 first-wins，未区分完全相同副本与不同 OHLC 的冲突。check_partition 实际只读 silver，虽 docstring 声称 bronze 重放，代码并未重放；所以端到端重复原因在去重前丢失。
- bronze 路径按月份固定，源包更新/force 时原子替换旧 ZIP 与 sha；“原子”不等于“不可变”，旧 manifest 引用的原包可丢失。
- quarantine UUID 使用 obj_ver=rule_version（funding 常为空），不含 source_sha256；新源版本同对象同原因会被旧记录吞掉。单次 new 内部重复也没有统一 unique。
- 改法：保留版本化 bronze/源行引用；冲突键隔离全部候选，完全副本有映射账；实际体检源端重复；隔离对象版本绑定原始内容摘要。
- 验收：端到端两条同键异价、源包修订后旧字节仍可取、新源新隔离记录、同源重跑不增加记录；无尖刺删除或缺口补值。

### S07 必修：订单队列、post-only 和账户可承担性只是部分实现

- 位置：`kernel_a.py:202` submit_entries、`:289` live_orders、`:361` match_point；ADR §4.1/4.3。
- 问题：entry 按 dict 插入序而非价格/accepted_seq；post_only 存入 Order 后未用于拒单；只按参考价检查一次保证金，没有手续费预留、实际 fill 重检或 funding 后余额约束。
- 实跑：E03 改 fixed_qty1、buy limit101、post_only=True，当前 last100，A 仍填100并最终 net=5；应 post-only crossing 拒绝。
- wallet 检查参考价100不保证跳空 market200时仍可承担；无 AccountLedger 无法证明 I10/I12。
- 改法：落实价格时间队列、穿价拒绝、现金/预留账本和 fill 前校验；未实现能力显式 unsupported，不能默默按普通 maker 成交。
- 验收：反序价阶争抢容量、同价改单失去优先级、post-only 边界、fee buffer、跳空超钱包、funding 后可用余额变化；终态与账户守恒断言。

### S08 必修：multiplier 非1时 PnL/暴露/滑点算错

- 位置：`kernel_a.py:313` apply_exit_fill、run P8；B `_on_fill`/`_replay_ledger`；`contract.py:532` 附近 gross 复算。
- 问题：sizing、fee、funding 乘 multiplier，realized、unrealized 与 slippage 未乘；结果不变量用同一错误公式，因此仍通过。
- 实跑：E03 固定量1、multiplier=2，100入105出，net=5；应为10。现有 test_risk_budget_sizing_and_multiplier 只检查 filled_qty。
- 改法：所有经济金额按单位统一乘 multiplier；C02 未裁定前也可直接拒绝非1，但不能接受并给出正常标签。
- 验收：multiplier 1/2、long/short、部分平仓、funding、费用、MAE/MFE 和净R独立 T 账一致。

### S09 必修：as-of 等号候选的 null 被旧值偷偷填回

- 位置：`asof.py:112` 对每个右列分别 coalesce。
- 问题：合法等号候选存在而某列值 null 时，coalesce 取 strict_lt 老行的非空值，却把 matched_at 标成新时间；产出不存在的拼接记录并掩盖缺值。
- 实跑：右行 (T−1s,seq0,v9)、(T,seq1,vnull)，左 (T,seq2)；结果 v=9、matched_at=T、reason=None，正确应 v=null。
- 改法：按“等号候选行存在”选择整行，保留候选 null；不要按每个单元格是否为空回退。
- 验收：等号行含部分/全部 null，strict fallback，tolerance；左/右 sequence 同名和 suffix 冲突也需测试。

### S10 必修：A/B 的解释器可把未知错误自动洗成已解释

- 位置：`nautilus_adapter.py:541–564` MANUAL_CODES/classify、ab_report；`contract.py:676` diff_result。
- 问题：按 fid 查人工码早于 EXC 检查；已知 E02 的任何新异常都标 B_COMMAND_LATENCY。兜底只比较少数标量，就把任何事件差标 SAME_TS_PRIORITY。diff_result 默认忽略 reason，且只留第一个事件差。
- 实跑：`classify('E02',['EXC RuntimeError: unrelated'],None,None)` 返回 B_COMMAND_LATENCY，不是 UNEXPLAINED。
- 报告 E02 的 B 暴露也不同：B 只在 last 点归约，遗漏 mark-only 的 -1.2 MAE；不能把首个时间差当全部差异解释。B qty=0 容量替换成最小步、bar 容量未采用 policy.participation，也必须明确标记。
- ab_report 直接 simulate_b，没有强制 check_invariants，却生成“B 不变量全过”文字；报告中余额刷新“已核”缺可复查账户余额断言，测试只验 adapter 自己累计的 funding。
- 改法：EXC/not_run 优先；人工解释绑定版本和具体差异谓词；枚举全差异与 reason；报告生成强制金标/不变量/余额证据，失败保持失败。
- 验收：对已知 ID 注入未知异常、金额/MAE/reason 变化均能变成 UNEXPLAINED；MATCH 必须全字段一致。B 已解释合同差异不能放行成正式内核。

### S11 必修：M-08/M-09 门禁和 ADR 测试矩阵不足

- 位置：taskList.modules.market 的 verify；tests/market；ADR §13；report-G2-kernel-AB。
- 问题：M-08 只 grep 表头；M-09 初次 skip 隐藏真实 G3 未验收，后续实际对接报错。E13 缺失；没有 test_order_invariants 的随机命令状态机；随机40例只是固定计划随机行情。
- 并发新增 test_funding.py 已读，覆盖正负费率、多空、重复同值行、变量周期和旧/未来显式 mark；**不能再记作该文件不存在**。但不覆盖 bars 的闭合结算 mark、冲突重复、4h 缺行和钱包舍入。
- 改法：冻结一组可重跑 source/test 摘要；验证非零 episode 数、完整矩阵、未解释差异、真实 G3 结果；补缺口而非增加自我一致断言。
- 验收：M-03～M-09 各门禁具有非空业务断言；全量 pytest 与真实 G3 合成对接在同一冻结版本通过；B 差异报告仍明确失败状态；新增反例全部通过。

### S12 必修：manifest/policy 哈希仍未完整约束不可变输入

- 位置：`execution.py:43,109`；`contract.py:203,259,404,617`；A/B kernel_version 常量。
- 问题：load_market_from_lake 从当前目录读取，直接把 req.market_manifest 字符串赋为 MarketView.manifest_id，并未解析该版本的不可变清单。实际 hash 绑定的是裁剪后的 MarketView，不含原始 partition/source hash 与 available_at_basis。
- policy_hash 字段已补且模型构造时验证，属于已完成进展；但可变 registry 和浅 frozen 的 costs/list 不构成历史 version→hash 锁，simulate 不重验已有 request 对应当前政策；CI 当前不同 policy 互异断言不能禁止跨修订复用版本名。
- 改法：manifest 引用解析到版本化分区摘要，拒绝任意同名最新目录；版本-内容登记可跨进程/修订核验；内核 build 身份包含代码摘要，B 包摘要纳入身份。
- 验收：源值相同但源版本/可见性假设不同会改 hash；旧 request 在同名政策内容改变后拒绝执行；下载墙钟/ingested_at 不改经济 hash；跨进程可定位准确制品。

### S13 必修：结果级不变量不足以证明 I01～I20

- 位置：`contract.py:422` check_invariants；kernel A 仅 result() 调用；B 模块账务/游标。
- 问题：没有逐事件 leaves/cancelled 数量、账户预留、共享容量、当前可执行价、规则版本、不变量 I11 结算键、I20 未来修改的完整验证；“不变量全过”实际只是结果子集自洽。
- 即使同一订单已 cancelled，随后 partial_fill 也未通过终态状态检查明确禁止；closed 时兄弟腿终态未全检。trigger_basis 校验不证明资金账务/撮合顺序正确。
- I01 对 finite/正值/Decimal(38,12) 容量、非负 latency/max_holding、stop/TP、reduce_only_exit=true 的约束不完整；这些模型可接受超出承诺的输入。
- 改法：以订单/账户/价点状态在转换时检查，结果层只负责归约一致性；对输入拒绝、未支持能力、市场删失三类明确区分。
- 验收：对终态后fill、超容量、错误钱包、错误规则、错误结算价/冲突键、超精度分别注入故障，必须独立失败；不能只篡改净R来证明检查器有效。

## 3. 契约逐字段与签名对照

以 execution-interface §5 + M-01 R1～R10 优先；R11 是 verify 修订，不是字段。下表按相关字段分组逐一列出。

| 对象/字段 | 实现对照 | 漂移/判定 |
|---|---|---|
| manifest partition_id/data_type/interval/instrument_id/period | Manifest 含这些字段 | 存在；interval 接受额外值，需明确扩展边界 |
| source_uri/source_sha256/checksum_source/downloaded_at/parser_version/zip_member | 全部存在，checksum 可 computed | 本地真实源哈希核对通过；旧源可覆盖见 S06 |
| expected_rows/actual_rows/missing/duplicates/schema_hash/quarantine_n/rule_version/available_at_basis/status | 全部存在 | status 额外 missing_source；404 返回简化 dict 不进入常规 manifest，coverage 会漏计缺源机会 |
| silver instrument_id/interval/open_time/close_time/close_time_raw | 标准 ID、UTC us、右端点、原始值保留 | 正常 bar 一致 |
| open/high/low/close/volume/quote_volume/taker_buy_volume/taker_buy_quote_volume/trades | Float64/Int64 | 不把 OHLC 转 Decimal，符合 R4；mark 归档结构性0不是缺行情补0 |
| event_time/available_at/ingested_at/source_sha256/rule_version | 入湖写入 | H0=close+0 固定；P2 latency1s 尚需敏感性，不阻断合成 P1 |
| ohlc_valid/spike_flag/gap_flag/spike_score | 实算/回填 | 尖刺仅标；使用端未 enforce 隔离见 S05 |
| funding calc_time/funding_interval_hours/funding_rate/三时钟 | 存在，时间UTC us | 体检相邻周期不等于区间完整证明 |
| metrics create_time及六个数值列 | 存在 | parser 仅接受秒级字符串，M-01 写毫秒向下取整；当前样例等价，毫秒输入未经验证 |
| rules effective_from/effective_to/tick_size/step_size/min_notional/multiplier/funding_interval_hours/status | 表列存在 | 执行只取起点基础子集，见 S05 |
| asof_join left_on/right_on/by/strategy/tolerance/sequence_cols/suffix | 签名存在；strict_lt 与有序等号分支 | null候选错误 S09；UTC非UTC aware会自动转换，规范原文要求非UTC抛错 |
| asof_reason / AsOfKeyDuplicate / _r | 已实现；额外 asof_matched_at | 同秒键检查存在；未覆盖 reserved/suffix 同名冲突 |
| last_closed_bar at/instrument_id/interval/latency | close+latency<=at | 满足 H0；不读已有 available_at，因此仅适用 H0，不能泛称真实晚到也安全 |
| mark_bar_at/mark_price_at price/reason/close_time/staleness | 取已闭合1m close、>120 stale | 缺 tick_size 时仍 reason=None，M-01 说需 RULE_HISTORY_MISSING 第二原因；应仲裁双原因输出形式 |
| request episode_id/graph_version/decision_snapshot_hash/t_dec | 模型与 batch 皆有 | build_request 仅消费冻结行，没有读取未来管理事件 |
| order_plan instrument_id/side/entries.kind/price_lo/price_hi/fraction/tif/post_only | 存在，后续新增 ID regex | post_only执行漂移 S07 |
| stop.price/trigger；tps.level/fraction；sizing.mode/qty；reduce_only_exit | 存在，trigger=mark | reduce_only_exit 是 bool 未限定 true；TP余量 S04；非1 multiplier S08 |
| expiry.entry_ttl_s/max_holding_s | schema 有；TTL实际执行 | max_holding S04 |
| request policy_version/policy_hash/risk_budget/cost_scenario/path_scenario | 后续字段补齐、hash进入canonical/batch | 正预算、base/stress、三路径存在；不可变版本登记 S12 |
| request market_manifest/execution_contract_version/seed | 存在且合同版本校验 | manifest不是快照解析；版本字符串常量不等于制品身份 |
| request t_start/horizon_end/position_mode | 存在，one_way限制 | effective latency 后边界需校验；分钟内启动 S03 |
| fee_rates | 经 policy.cost(scenario) 展开，非独立 request 字段 | B4允许政策文档定数值，接受此布局；执行资金约束不足 |
| canonical seq/ts/kind/order_id/leg/trigger_basis | 全部存在，kind含amended，leg五值含funding | 按产生次序而非 R7 同ts全局 kind/order_id 排序；ADR C03理由成立但仍需 G0 正式裁定 |
| canonical price/qty/fee/reason/bar_open_time/path_step | 全部存在 | reason比较被默认忽略；B mark来源 bar_step 取last点可能错归因 |
| canonical cash_delta | 额外typed列 | ADR C05合理增补，尚缺 rate/settlement_id 与 amended old/new 自包含信息 |
| result fill_status/filled_qty/fees/funding/slippage/gross_pnl/net_pnl/net_R | 全部存在 | 删失null/完整未成交0落实；错误撮合仍可给自洽金额，见 S01/S08 |
| result kernel/kernel_version/trace_hash | 全部存在 | 哈希封装五项、Decimal字符串/时间微秒符合；S12制品来源不足 |
| entry_avg_price/exit_avg_price/position_open_at/position_close_at/mae_R/mfe_R | 全部存在 | A mark微步、B last采样，B不符合相同暴露定义 |
| censor_reason/coverage_mask 五位 | 对象层存在；batch五位平铺 | R9说全部标量，平铺合理但应明确输出schema；coverage证明不足 S05 |
| simulate(req, kernel) | 增加显式 market/resolver | 无二者直接报错，不能仅靠 req.market_manifest 调用；消除全局状态合理，签名行为仍需 G0接受 |
| simulate_batch Iterable / 一行一request / PAIR_KEY / Decimal(38,12) / List(Struct) | 正常成功分支落实 | strict=False错误行仅保留episode_id，丢graph/policy等配对键并移到尾部；见应改 |
| build_request | 后续已按 research-schema §9.4 加入 | 不再列缺失；max_holding默认计算不替代运行时约束 |
| R11 | M-01 verify仍保留旧grep | 不属于M-03～M-09执行结果，但看板修订未闭环 |

## 4. ADR 判定顺序与不变量覆盖

| Phase | A 实现 | 偏离与理由 |
|---|---|---|
| P0 | run前全局rules/bars检查 | 未每时刻验证，未来缺口会提前删失，S05 |
| P1 | funding先于同刻fill | q(t−)基本正确；结算mark/schedule错误，S02；t_start先submit再funding与严格phase不同 |
| P2 | TTL/到期在last撮合前 | GTD等号不成交正确；max_holding缺失；历史命令流C01待定可接受，内部队列仍需补 |
| P3 | mark在当前last前摄入 | 启动前有效mark未初始化、首次入场缺mark未阻断；同ts多mark无source_seq |
| P4 | mark触发SL、取消TP/entry | A异步E02不回用旧last，符合；B只在下一last时发现/释放 |
| P5 | last触TP | 历史trigger latch导致回落仍填，S01 |
| P6 | SL→TP→entry共享capacity | 局部共享正确；订单价格时间优先/资金重检缺失，S07 |
| P7 | 入场后SL一次重检 | E18存在；B命令屏障差异已披露，不能作合同通过 |
| P8 | 暴露与最终结果断言 | 未逐转换执行全套不变量；B只last采样；S13 |

| 不变量 | 实际覆盖结论 |
|---|---|
| I01 | UTC/正预算/部分计划价格校验；边界与精度不完整 |
| I02 | market字符串相等；没有规则版本区间唯一/快照解析证明 |
| I03 | 累计fill检查存在；leaves+cancelled/expired全量守恒未断言 |
| I04 | 终态计数、累计fill≤qty存在；终态后partial/amend状态机不足 |
| I05 | 从事件复算仓位/成本；multiplier错误被同公式掩盖 |
| I06 | 出场不增绝对仓位/不翻转有检查 |
| I07 | 未入场不得触发/出场有检查；只看had_fill不足以覆盖所有零仓状态 |
| I08 | GTD/IOC若干夹具；无tick IOC等边界缺，B GTD明确失败 |
| I09 | capacity局部消耗实现；无独立断言，TP历史触及错误可漏 |
| I10 | net=gross-fee+funding有；真实cash账本守恒无 |
| I11 | q等于事件前仓位有；新加ts去重，冲突输入/结算时点仍缺 |
| I12 | 仅submit参考保证金检查，无预留生命周期 |
| I13 | mark只stop_triggered、TP只last落实 |
| I14 | seq连续/ts非降落实；无完整因果状态机/source_seq |
| I15 | exit latch与reduce-only基本有；TP剩余量与舍入不完整 |
| I16 | censor非空时net/R为null、完整未成交为0落实 |
| I17 | closed时pos=0、最多一次；未核全部兄弟腿终态 |
| I18 | 分母固定、slippage不重复扣落实；multiplier仍错 |
| I19 | A跨进程、单条/批次重排有测试；B同进程有，跨进程缺 |
| I20 | asof晚到例有；未来修改不影响历史特征的性质测试缺 |

路径 primary 等距H先、adverse/favorable按long/short分开，A/B各自展开，定义与ADR一致。
合成路径在执行标签阶段读取未来整根bar，是明确模型假设；不能把这一点一概定为决策前视。真正违规是包含t_start的旧bar极值仍被使用，以及funding混用未闭合bar。
A/B撮合、触发和funding helper 未共享；contract里的单位/量化/序列化共享在ADR允许范围。B模块底部报告器import A作比较，不是B借A算fill。
B已披露GTD、跳空改善、滑点、命令延迟的理由作为spike解释基本成立；“不改vendor”可接受，不能据此放行合同不合格的B。
C03因果排序和C05 cash_delta的设计理由成立，但公共冻结契约尚需批准；不能用ADR提案自动覆盖G0规范。

## 5. 夹具独立性、前视与幸存偏差

22 个JSON全部加载并检查完整 expected/canonical_events；使用 `runpy.run_path(..., run_name='review_readonly')` 只执行builder声明，未调用main写文件。
builder的 F 字典与22个磁盘JSON逐对象比较：`builder_vs_json 22 []`，没有差异。
builder只import datetime/json/Path及后续contract政策哈希，不调用A/B/execution/ResultReducer；期望事件和金额由显式Events表与手算字符串构成。
由源码能确认当前没有“运行内核后录制gold”的路径；不能据此证明作者历史上从未参考输出，未提供纸面期望冻结的提交历史。
E01、E05、E07、E11、E12、E17等算式与输入可手算核对；gold自洽不是撮合覆盖完整的证明。
缺E13；E10没有ADR规定的超额TP qty2竞合；E14 LOT_SIZE仅额外测试，不是独立JSON；E17用SL实现出场以符合当前无主动close命令的接口，理由可接受但需记变体。
JSON路径分布：20 primary、2 adverse、0 favorable；全部cost=base；三情景×两成本笛卡尔积未覆盖。
asof strict_lt排除等号、le_with_sequence严格right_seq<left_seq、晚到用available_at有实测；last_closed_bar H0等号与latency1s有实测。
last_closed_bar忽略已有available_at仅适合H0；负latency应拒绝；mark_price_at不检查ohlc_valid，输入有效性需在接缝证明。
spike_scores用全分区median/MAD，是离线质量诊断；若用该标记做历史特征/样本筛除，会泄漏未来分布。当前执行未按尖刺删样本，没有发现这条直接前视筛选路径。
尖刺只标、缺bar不插值在解析/体检层成立；silver冲突去重、loader忽略隔离、404不进常规coverage仍会污染样本完整性，见S05/S06。
rules_from_manifests不从最后归档月份推断下线，正确；但把首个已下载key_min当上市起点、后续无限TRADING不是完整生命周期证据，不足以排除幸存偏差。
G3 helper的亏损skip不缩共同分母/删失不补0有合成检查；真正evaluator对接必须独立过关，不能以helper替代。

## 6. ADR §13 测试矩阵盘点

| 规划文件/矩阵 | 当前实际文件与覆盖 | 缺口 |
|---|---|---|
| test_contract | 已存在，后续17项，字段/hash/builder/金标/部分负例 | Decimal溢出、完整枚举约束、冻结版本跨修订登记 |
| test_kernel_episodes | 由 test_kernel_a 参数化22例承担，不要求同名文件 | E13、各适用方向×3路径×2成本、独立钱包账 |
| test_order_invariants | **文件缺失**；kernel_a有40随机行情种子 | 无随机submit/amend/cancel状态机、队列/容量/预留故障注入 |
| test_funding | **后续已新增并阅读**，6个函数，其中多空/正负参数化4项，共9项 | closed-bar vs point、冲突重复、4h缺行、无tick B、币种细额舍入 |
| test_path_scenarios | kernel_a和adapter中的展开/方向测试承担 | 双流极值不同、分钟内启动、重复时间有序证据、完整cost组合 |
| test_asof | 11项，等号/晚到/未闭合/119与121秒 | null候选、120s恰边界、未来修改、重复closed-bar选择 |
| test_nautilus_adapter | 6项，MATCH集合/不变量/funding/路径/已知差异 | 真实余额/命令屏障原始日志、完整合法/非法filter对 |
| test_kernel_ab | adapter中的对拍测试承担 | 解释码过宽、忽略reason、not_run/EXC绕过、首差后经济差 |
| test_replay | **存在**：跨进程A、批次重排、单条对批量 | B跨进程；ingested_at修改测试名不符实质，原用例改的是原值graph_version |
| test_execution_g3 | test_execution_api承担，一request一行/Decimal/null与0 | 移动版本对接曾skip并报错；需固定证据闭环 |

## 7. 凭据与网络边界

market源码静态import扫描及test_no_services_import未发现实际import services/*；没发现生产账户配置、私有交易所API或凭据读取。
默认网络源为data.binance.vision及同桶S3镜像，属于公开归档；并非字面上“唯一域名”。http_get可传hosts且允许redirect，未实现硬域名白名单，应明确这属于配置约定而非强隔离。
rules_from_exchange_info仅处理调用方提供的公开离线JSON，不在线拉取；不能称全部规则也来源于Vision。
本次test_vision/partition_check网络调用使用httpx.MockTransport，真实分区测试只读本地归档。未实际请求上述网络。
G3真实冒烟import研究层，不是生产服务；本会话未修改该层。

## 8. 应改

1. strict=False错误行保留完整配对键、输入序号与coverage/error，不能只有episode_id；重复配对检查应覆盖错误行。
2. 统一QUANT_LAB_DATA_ROOT：research-schema §9.1要求全仓统一，当前LakePaths.default使用QUANT_LAB_MARKET_LAKE，loader默认相对data路径；补路径隔离验收。
3. M-03完整网络冒烟只在另获授权后运行；本次保留insufficient，避免修改verify来绕过真实源要求。
4. 空fixture/replay与全skip应失败；M-07 grep改结构化JSON计数+退出码；M-08检查完整非零记录和报告绑定的源摘要。
5. mark_price_at缺rules的第二原因、simulate默认resolver、coverage_mask batch形态、C03/C05扩展统一提交G0，避免各方自行猜测。
6. 代码规范：contract部分多层条件表达式、B账本多语句分号降低可读性；按AGENTS拆为卫语句和中间变量。JSON null按契约保留，不机械改false。
7. 报告代码量/性能绑定完整源码摘要、硬件与测量原始记录；现报告commit未包含未提交实现，不能复算同一产物。

## 9. 可选

1. 将反例按缺证据、撮合、账务、归约分组，保留独立手写T账，便于维护且不共享A/B撮合helper。
2. 增加source_seq、phase等诊断字段作为经批准的扩展，提升异步路径与首差定位能力。
3. P2前补100/1000/10000规模、RSS与冷热基准；P1不以吞吐抵消正确性失败。

## 10. 收尾

终裁 **fail**：S01～S13 必修未闭合。M-03完整真实下载因授权边界为insufficient，但终裁不止于证据不足，已有独立复现的确定性错误。
本报告没有修复业务代码，也不依据其他会话“已完成”转述判定闭环；后续应对固定源码/测试摘要重跑并逐项销号。

## 二审（闭合核对）

审查日期：2026-09-11。审查者：当前 GPT-6 主控会话；未委派、未调用其他模型。依据为工作区实际代码、`review-G2-P1-closure.md`、冻结的 execution-interface（含 §5.9）、ADR-G2 和 GOAL-2 §7；不以闭合记录的自报状态替代执行证据。

**结论：不满足 P1。S01–S13：closed 1 条、partial 12 条、open 0 条。** partial 表示有已验证的实质修复，但整条验收仍有缺口，不等于通过。新增 S14 为入湖修复引入的旧 silver 残留问题，状态 open，另计。失败依据包括当前可复现的静态计划执行错误、as-of 错选和审查门禁漏检，不以候选 B 尚未定型为失败理由。

### 1. 本次证据与并发边界

- 只追加本章节；一审原文的 36,021 字节保持不变，原文 SHA256：`7cbe50099dd991f8d8e47c1d85bcac567e956ff4538969ddf83944fa7fc43a6c`。另按用户授权运行报告生成器，更新 `report-G2-kernel-AB.md`。
- 必读文件已逐一阅读，包括根 AGENTS.md、8 个指定 market 模块、5 个指定测试文件、接口契约、ADR、闭合记录；另读 GOAL-2、taskList、fixture builder、22 个 JSON 和接缝测试。未联网、未操作生产、未读生产凭据；没有运行下载器或夹具生成器 main。
- pytest 使用 `PYTHONDONTWRITEBYTECODE=1`，项目配置禁用 cacheprovider；测试合成湖只落 pytest 临时目录。下述补充反例只在进程内构造数据；入湖反例将所有写入函数替换为内存收集器，未创建临时审查文件。
- **发现并发修改，已复验**：初轮为 164 passed；随后其他会话加入 nullable TTL / policy 兜底及输出列，并更新政策哈希、夹具和测试，HEAD 也发生变化。最后读取 HEAD=`539854ff6026021f6dfb3bcba7d4d4a17570e383`，但多数实现尚未提交，不能仅用 HEAD 定位本次产物。
- TTL 初读反例曾报 `ValidationError`；复读后 `build_request(entry_ttl_s=None)` 返回 `entry_ttl_s=86400`，公开 `simulate` 返回 `entry_ttl_source=policy`。最后全量测试包含新增 TTL 用例且全过，**不把这次已修错误继续列为新增问题**。
- 最后证据集摘要：`ed9a7784118067723de5d8821f8b388ad512072a295a5f57d77dc60d2a9ce5e8`。算法为：将 market 顶层 `*.py`、tests/market 递归 `*.py`、22 个 episode JSON、execution-interface、ADR-G2、closure、GOAL-2、taskList 共 49 文件按路径排序，连接 `路径:sha256(文件字节)\n` 再 SHA256。不含本审查和可重生成的 A/B 报告。此摘要是工作区证据绑定，不声称整个会话期间持有原子快照。
- 收尾时其他会话又更新 taskList 回执，49文件摘要变为 `ed08f928f6db0a54146fefe5b93c28839b7ac275af2223ca45c17c74e22222b5`；M-03/M-08/M-09 verify 字符串复读未变，核心源码摘要未变。排除可变看板后的48文件收尾摘要为 `da017d300dd0a71add341797e239b1977f87ea691b5f7303f291efb9ea33cc58`。新回执仍自报“闭合”，不覆盖本节的独立反例与判定。

最终核心源码 SHA256（便于定位反例对应制品）：

| 文件 | SHA256 |
|---|---|
| kernel_a.py | `86eeed58e7eda6e136ed1113e53d777201855c41101f1ec50b796f47879d2446` |
| nautilus_adapter.py | `6e72d286670f16247b3c504392eca7ea62e2a485e455bf9103eeed147742959c` |
| contract.py | `41cc971b27beee350a1553b62181ebd3cbed1f5f46cb9a9558d9b46fa9280857` |
| execution.py | `40ca4263512716b0dccdbc8c07b3397b67f622edc7d273f53d8588a1ee6bc349` |

### 2. 实跑命令与 P1 DoD

以下 cwd 均为 `/Users/balen/projects/trader-bot/quant-lab`，命令前均加 `PYTHONDONTWRITEBYTECODE=1`。专项测试只有 13 个收集项，其中 S05 两项、没有独立名为 S11 的项；不能把“13 测试”直接换算成“13 必修闭合”。

| 代号 | 实跑命令 | 输出摘要 / 退出码 |
|---|---|---|
| T | `.venv-g2/bin/python -m pytest tests/market -q` | 初轮 `164 passed in 8.23s`；TTL 更新后 `166 passed in 8.23s`，均 0；最终无 skip/xfail |
| R | `.venv-g2/bin/python -m pytest tests/market/test_review_p1.py -q -v` | `13 passed`，0；TTL 更新后再次运行通过 |
| A | `.venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A` | `passed=22 failed=0`，22 个 replay=True，0；TTL 更新后复跑相同 |
| B | 同上，`--kernel B` | `passed=12 failed=10`，22 个 replay=True，1；TTL 更新后复跑相同 |
| AB | `.venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1` | 0；MATCH=12、B_COMMAND_LATENCY=3、GAP_PRICE=1、SAME_TS_PRIORITY=4、GTD_BOUNDARY=1、B_LIQUIDITY_MODEL=1；最后中位 A=7.85ms、B=39.61ms（单次，仅记录） |

T 实际执行了闭合文档引用的 `test_funding`（12 项）、`test_replay`、`test_kernel_a`、`test_nautilus_adapter`，以及 M-03..M-09 的 vision/partition/asof/contract/execution_api 测试，包括真正的 G3 `pair_arms`/`evaluate` 调用；此轮没有 skip/xfail，收尾源码确认原 TypeError→xfail 分支已移除，ImportError→skip 分支尚保留但本轮未触发。它仍是手建 OpportunitySet 的合成接缝冒烟，不证明完整 G1→G3 实际数据流水线。

按 GOAL-2 §7 分别判断：

| DoD 项 | 二审核对 |
|---|---|
| M-03..M-09 verify | 获授权的测试部分经 T 实跑通过；夹具数=22；A 满足 M-07 的非零数量门槛；M-08 表头门实际存在但强度仍不足。M-03 原 verify 含真实网络 fetch，本次依用户禁网要求未执行；只读本地 manifest actual_rows=44640、check_status=ok，bronze 实际 SHA256 与 manifest 相符。完整 M-03 网络 verify 的本次证据仍为 insufficient，不能把看板 done 或历史下载说明当本次下载实跑。 |
| A ≥10 最小 episode 与不变量全过 | 当前 22 金标与已实现断言全过；补充反例仍发现 S04/S07/S13 的经济/状态错误，故不等于 ADR I01–I20 验收全过。 |
| A/B 报告出具，定型留 P2 | 已重生成。B 十例不匹配本身不阻断“出具失败差异报告”，但 S10 使 UNEXPLAINED=0 不可靠；报告仍有“余额已核”等与 not_run 相冲突的文字。 |
| 必修闭合 | 未满足，见下表。即使补齐 M-03 网络证据，也不能改变本轮 fail。 |

### 3. S01–S13 逐条判定

表中 `R:test_sXX_*` 指 R 命令实际执行的对应测试，`T:文件` 指全量命令实际执行该文件；补充只读反例 C1/C2/C3 的完整命令在下一节。一审原反例已经进入回归者，不要求另写重复测试文件。

| ID | 判定 | 复现命令与输出摘要 | 剩余缺口 | 是否阻断 P1 |
|---|---|---|---|---|
| S01 | **closed** | `R:test_s01_*`：qty2、105 容量1后回到100，只 TP 1，余仓删失；short 镜像首次95容量0、回到100不填、再次95才平2。源码 `tp_executable` 每次 fill 重验当前 last；零容量触及不设置 exit latch。 | 本条历史触及导致虚构成交的反例已闭合；通用容量断言不足归 S13，不重复挂账。 | 否 |
| S02 | **partial** | `T:test_funding.py`、`R:test_s02_b_*`：闭合 bar100/新 bar110 结算取100；多空正负、同值重复一次、冲突费率拒绝、schedule=false 入场删失、B 无后续 tick 的 flush 均有实跑。C2：funding manifest 为 ok、实际 funding 文件不存在，loader 仍给 complete=True、0行，跨08:00平仓得到 net=5、censor=None（应缺证据，不能把资金费当0）。 | U03 的真实入账时刻敏感性可留 P2；**现有 loader 的缺文件/首尾证据放行不是敏感性研究**，必须先 fail closed。B flush 仅补 adapter 事件/总额，没有 adjust_account；pre_process 仍在入账前记 applied，真实余额验证留 P2，不接受“B 同步全闭合”。 | **是**：现有加载入口仍输出缺资金费证据的完整标签；把完整 schedule 研究记 P2 不能豁免缺文件拒绝。 |
| S03 | **partial** | `R:test_s03_*`：一审 t_start=T+10，A/B 均从下一根 last bar O 入场，A 改旧 bar H/L/C 首填不变。C3：t_start=T+10、显式 last 在T+45，包含启动时刻的 mark bar 在T+40合成L=90；A 无 stop、net5，B 在T+45按90止损、net0。 | A 的原反例闭合；B 只过滤 bars_last，marks 仍展开全部 bars，混合异步输入消费了应排除的启动 bar 极值。此处明确登记 **P2/B 定型前必修**，B 不得作为合格标签内核。 | 否，限 GOAL-2 的 A 参考实现 + B 失败 spike 报告口径；不能宣称 B 的分钟内启动语义闭合。 |
| S04 | **partial** | `R:test_s04_*`：hold10 后无TP；qty1/.5+.5 的余量给末档、sum<1保留余仓均过。C1：hold10、T+30 funding 仍入账−0.1，随后才 LABEL_RIGHT_CENSORED；B 同输入还在T+60平仓，net4.9。 | A 没将实际 open_at+hold 插入 timeline；先处理当前 mo.funding 再检查 hold_end，会越观察终点记账。须在T+10截断，保留到该点的事件、费用和暴露。B 完全未落实 max_holding/末档补差，列 P2/B。 | **是**：A 静态合成请求的观察终点和资金账务错误，不能留到定型。 |
| S05 | **partial** | `R:test_s05_*` 两项过：规则过期保留先前入场、无mark首填阻断、tick0拒绝、未体检/尾缺阻断、refs存在。C2：规则 status=BREAK、min_notional=None 仍 rules_known=True、min_notional=0，正常成交 net5。 | loader 忽略 status、以0代替未知 min_notional、未拒绝重叠区间；仅比较起止 effective_from，不能证明全窗规则唯一。全窗 bars_complete=False 仍起点清空事件，尚未实现逐时 coverage。中途规则切换拒绝可以 gated，未知规则伪装已知不可以。 | **是**：S05 要求的有效规则/缺证据边界仍能放行，完整 filter 扩展 C04 可留 P2，基础未知项不能。 |
| S06 | **partial** | `R:test_s06_*` 过：同键冲突两候选隔离、完全副本折叠、同源重跑幂等、新源新增隔离、旧 bronze 字节摘要可取。C2 的源包修订反例：新版全日都是冲突，manifest.days=[]，内存 silver 仍保留旧源1行，见 S14。 | 旧 ZIP 保留有效；但没有完整版本化 manifest/源行映射，旧路径已移动，不能称旧引用自动稳定。check_partition 仍读 silver，文件顶部 docstring 还声称 bronze 重放；闭合记录“docstring 已改口”不准确。新修订不清理消失日期的 silver 是当前入湖正确性问题。 | **是**：M-03/M-04 的隔离/来源一致性仍有实际反例；bronze 深度重放可留 P2，旧有效行复活不可。 |
| S07 | **partial** | `R:test_s07_*`：post-only crossing拒绝、非穿价正常成交、跳空200钱包1000只成交5并取消余量均过。源码 entry 已按价格/seq 排序（现有 E11 本来高价在前，测试不能独立证明反序输入）。C3：钱包1000，先5@100，mark20、SL1时 equity600/已用margin500；仍新买5@90，最终net100。 | affordable_qty 只用cash−成本保证金，未用mark更新后的equity；新买450超过当时剩余100额度。I10恒等式不证明I12可承担性。C01管理命令/E13可 gated，但此反例无管理命令，属于现有静态计划。 | **是**：静态请求能超额开仓；不能用 C01 挡住账户缺口。 |
| S08 | **partial** | `R:test_s08_*`：直接 A/B multiplier2 gross10；A fee=.101、slippage2、gross8、mfe2均过。C1：经公开 `execution.simulate`，A/B 都抛 `ExecutionInvariantError: gross_pnl 10 != 由事件复算 5`。 | execution.py:62 的第二次 check_invariants 未传 multiplier；B 原生fee未缩放multiplier，暴露重放 realized 分量也没乘。仅测直接 simulate_a/b 不能证明公共接缝闭合。 | 非1 multiplier 仍受 ADR C02 的正式标签 gate，**本残余明确列 P2/C02 解闸前必修，不单独据此否决只用 multiplier1 的 P1**；不得再写“multiplier 全链路闭合”。 |
| S09 | **partial** | `R:test_s09_*`：不同列名 lseq/seq 的原 null 反例正确保留null，naive/Tokyo列拒绝。C1：同一输入把左序号列也叫seq、sequence_cols=(seq,seq)，输出 v9/matched_at=T−1s，而合法等号行是null/T。 | 等号join后右seq被后缀重命名，filter仍比较左seq<左seq，整行退回旧值；一审验收明确要求同名sequence。suffix/reserved列冲突尚缺完整覆盖。 | **是**：公共 M-05 接口静默选错候选，不是 P2 扩展。 |
| S10 | **partial** | `R:test_s10_*` 的 EXC、新首差、E02 MAE异常均正确拒绝；AB强制 B 不变量。C1：保留已知首差并把 E02 最后closed.reason 改成 UNRELATED_CORRUPTION，不变量通过，仍归 B_COMMAND_LATENCY；E17 mae_R=−999 仍归 B_LIQUIDITY_MODEL；classify(E02,[],None,None)=MATCH。 | 仍只校验首差子串，后三个 need_equal=False 项连意外标量也不核；未绑定全部差异/预期金额/可运行状态。diff_result(all_diffs=True) 在已有差异时不再报长度差。报告只列首差+数量，不给完整差异证据；余额not_run与“余额已核”矛盾。 | **是**：审查门禁仍可把未知错误洗成已解释；UNEXPLAINED=0 不是可靠验收。 |
| S11 | **partial** | T 最后166 passed、R13 passed、A22/22；G3 evaluate 本轮实际通过，无 skip/xfail；40例已改随机计划×行情，funding/replay确实存在。 | M-08仍只grep表头；报告CLI仅凭UNEXPLAINED决定返回码，不以A金标失败/空集失败兜底。E13可随C01 gated；B真实余额、B跨进程和完整路径×成本矩阵登记P2。现有测试没覆盖本次C1–C3的核心缺口，不能宣布必修闭合。M-03禁网部分另记 insufficient。 | **是**：非空/正确性门禁与S10仍缺；不是因 E13 尚未实现或 B 尚未定型。 |
| S12 | **partial** | `R:test_s12_*`：源refs改变hash、quality_notes不改hash、A拒绝旧policy_hash；`T:test_replay.py` 跨进程与批次重排通过。C3：同名政策wallet1000→999，旧request在A抛ContractError，B仍接受net5。 | B未重验policy_hash；注册表没有跨修订version→hash登记。本轮新增entry_ttl_s同时保留fixture/base的v1名字，也直观证明“字段有hash”不等于“版本不可复用”。build id只哈希各自内核文件，不含共享contract/reducer；B只有包版本、无wheel摘要。版本化湖快照解析已记P2。 | **是（冻结契约 §5.5 B4 的版本登记要求）**；版本化真实湖与B制品审计可留P2，但不能声称本轮已落实跨修订一一对应。 |
| S13 | **partial** | `R:test_s13_*`：终态后fill、缺兄弟腿终态、重复funding、reduce_only=false与hold0拒绝通过。C1：把E03的SL cancelled移到closed之后，check_invariants仍通过；qty=1e−13/1e27也能构造。C3：step1/capacity.5，A入场与TP都成交.5，net2.5且不变量通过。 | closed检查使用第一遍扫描得到的最终terminal集合，不能证明closed当刻已终态；输入Decimal(38,12)数量约束不全，撮合take未向step量化。容量/钱包/当前规则不能仅声称由内部路径保证，当前路径已有反例。 | **是**：I17因果与合法数量实际漏检，直接影响M-06/M-07不变量门。 |

### 4. 补充反例的只读复现命令

以下为本轮实跑构造的可重放版本。输出摘要见逐条表；异常被捕获以便一条命令完成所有反例，**这些探针退出0不表示被测逻辑通过**。均不写文件、不访问网络。`model_copy` 用于构造受控场景，核心价格/数量/时序均在正常请求域；超精度项另外用 `model_validate` 验证真实模型边界。

**C1：公共接口、持仓截止、解释器、as-of 和事件因果。**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python - <<'PY'
import datetime as dt
from decimal import Decimal as D
import polars as pl
from tests.market.test_review_p1 import FIX, e03, pt, T0
from quant_lab.market import contract as c, execution as x, asof, nautilus_adapter as nb
from quant_lab.market.kernel_a import simulate_a

req, mk = e03(plan_upd={'sizing': c.Sizing(mode='fixed_qty', qty=D(1))},
              market_upd={'rules': c.Rules(multiplier=D(2))})
print('S08 direct', simulate_a(req, mk).gross_pnl)
for k in ('A', 'B'):
    try:
        print('S08 public', k, x.simulate(req, kernel=k, market=mk).gross_pnl)
    except Exception as e:
        print('S08 public', k, type(e).__name__, str(e))

req, mk = e03(plan_upd={'expiry': c.Expiry(entry_ttl_s=3600, max_holding_s=10)},
    market_upd={'funding': [c.FundingRow(calc_time=T0+dt.timedelta(seconds=30),
                                       rate=D('.001'), interval_hours=8)]})
r = simulate_a(req, mk)
print('S04', r.funding, r.censor_reason,
      [(e.ts, e.cash_delta) for e in r.canonical_events if e.kind == 'funding'])
print('S04 B', nb.simulate_b(req, mk).net_pnl)

f = FIX['E02']; a = simulate_a(f.request, f.market); b = nb.simulate_b(f.request, f.market)
ev = list(b.canonical_events)
ev[-1] = ev[-1].model_copy(update={'reason': 'UNRELATED_CORRUPTION'})
bad = b.model_copy(update={'canonical_events': ev})
c.check_invariants(f.request, bad)
d = c.diff_result(f.expected, bad, ignore_reason=False, all_diffs=True)
print('S10 tail', d[-1], nb.classify('E02', d, a, bad))
f = FIX['E17']; a = simulate_a(f.request, f.market); b = nb.simulate_b(f.request, f.market)
bad = b.model_copy(update={'mae_R': D(-999)})
c.check_invariants(f.request, bad)
print('S10 scalar', nb.classify('E17', c.diff_result(f.expected, bad,
      ignore_reason=False, all_diffs=True), a, bad))
print('S10 not_run', nb.classify('E02', [], None, None))

r = pl.DataFrame({'available_at': [T0-dt.timedelta(seconds=1), T0],
    'seq': [0, 1], 'v': [9, None]}, schema_overrides={
    'available_at': pl.Datetime('us', 'UTC'), 'v': pl.Int64})
l = pl.DataFrame({'t_dec': [T0], 'seq': [2]},
                 schema_overrides={'t_dec': pl.Datetime('us', 'UTC')})
print('S09', asof.asof_join(l, r, strategy='le_with_sequence',
      sequence_cols=('seq', 'seq')).select('v', 'asof_matched_at').to_dicts())

f = FIX['E03']; r = simulate_a(f.request, f.market)
ev = list(r.canonical_events)
i = next(i for i, e in enumerate(ev) if e.kind == 'cancelled')
ev.append(ev.pop(i))
ev = [e.model_copy(update={'seq': i}) for i, e in enumerate(ev)]
c.check_invariants(f.request, r.model_copy(update={'canonical_events': ev}))
print('S13 closed_before_cancel ACCEPTED', [(e.kind, e.order_id) for e in ev[-2:]])
for q in ('1e-13', '1e27'):
    p = c.OrderPlan.model_validate({**f.request.order_plan.model_dump(),
                                  'sizing': {'mode': 'fixed_qty', 'qty': q}})
    print('S13 qty ACCEPTED', p.sizing.qty)
PY
```

**C2：缺 funding 文件/非交易规则，以及修订源包后旧 silver 复活。**所有路径都是内存替身；`retain_previous_bronze` 被替换仅为隔离本反例的 silver 行为，bronze 保留本身由 R 的真实临时目录测试验证。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python - <<'PY'
import datetime as dt
import hashlib, json
from pathlib import Path
from unittest.mock import patch
import polars as pl
from tests.market.test_review_p1 import FIX
from tests.market.test_vision import JAN, bar_rows, make_zip
from quant_lab.market import execution as x, partition_check as pc, vision as v, quarantine as q
from quant_lab.market.kernel_a import simulate_a

f = FIX['E05']; start = f.request.t_dec
req = f.request.model_copy(update={'horizon_end': start+dt.timedelta(seconds=120)})
root = Path('/readonly-review-lake')
bars = pl.DataFrame({'open_time': [start, start+dt.timedelta(seconds=60)],
    'open': [100., 105.], 'high': [100., 105.], 'low': [100., 105.],
    'close': [100., 105.], 'volume': [100., 100.],
    'ohlc_valid': [True, True], 'gap_flag': [False, False]})
marks = bars.with_columns([pl.lit(100.).alias(k) for k in ('open', 'high', 'low', 'close')])
def exists(p):
    return '_manifest' in str(p) or '/klines/' in str(p) or '/markPriceKlines/' in str(p)
def readtext(p, *args, **kw):
    return json.dumps({'partition_id': p.stem, 'source_sha256': 'mock-source',
                       'schema_hash': 'mock-schema', 'check_status': 'ok'})
for status in ('TRADING', 'BREAK'):
    rules = pl.DataFrame([{'instrument_id': req.order_plan.instrument_id,
        'effective_from': JAN, 'effective_to': None, 'tick_size': '1', 'step_size': '1',
        'min_notional': None, 'multiplier': '1', 'funding_interval_hours': 8,
        'status': status, 'source': 'read-only-mock'}], schema=pc.RULES_SCHEMA)
    with patch.object(Path, 'exists', exists), patch.object(Path, 'read_text', readtext), \
         patch.object(pl, 'read_parquet', side_effect=lambda p:
                      marks if '/markPriceKlines/' in str(p) else bars), \
         patch.object(pc, 'load_rules', return_value=rules):
        mk = x.load_market_from_lake(req, lake_root=root)
    r = simulate_a(req, mk)
    print('S02/S05', status, mk.bars_complete, mk.rules_known, mk.rules.min_notional,
          mk.funding_schedule_complete, len(mk.funding), r.net_pnl, r.censor_reason)

mem = {}; rows = bar_rows(JAN, 1)
def ingest(z):
    return v.ingest_bytes(z, data_type='markPriceKlines', interval='1m', symbol='BTCUSDT',
        period='2024-01', source_uri='memory-only', source_sha256=hashlib.sha256(z).hexdigest(),
        checksum_source='computed', head_meta={}, lake=v.LakePaths(root))
with patch.object(v, 'retain_previous_bronze', return_value=None), \
     patch.object(v, 'atomic_write_bytes'), patch.object(v, 'atomic_write_json'), \
     patch.object(v, 'atomic_write_parquet', side_effect=lambda p, d: mem.__setitem__(str(p), d.clone())), \
     patch.object(q, 'append_quarantine', return_value=2):
    m1 = ingest(make_zip(rows, 'x.csv'))
    bad = list(rows[0]); bad[4] = 999.
    m2 = ingest(make_zip(rows+[bad], 'x.csv'))
    print('S14', len(m2.conflict_keys), m2.days, sum(d.height for d in mem.values()),
          all(d['source_sha256'][0] == m1.source_sha256 for d in mem.values()))
PY
```

输出：两种 status 均为 `True True 0 True 0 5.00000000 None`；S14 为 `1 [] 1 True`。

**C3：B 启动 mark、政策重验、A 账户额度及 step 边界。**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python - <<'PY'
import datetime as dt
from decimal import Decimal as D
from tests.market.test_review_p1 import FIX, e03, pt, T0
from quant_lab.market import contract as c, nautilus_adapter as nb
from quant_lab.market.kernel_a import simulate_a

f = FIX['E04a']; req = f.request.model_copy(update={'t_start': T0+dt.timedelta(seconds=10)})
mk = c.MarketView(manifest_id=f.market.manifest_id,
    last=[pt(45, 100), pt(60, 105)], mark=[pt(-1, 100), pt(60, 100)],
    bars_mark=[c.Bar(open_time=T0, o=100, h=101, l=90, c=100)])
for k, sim in [('A', simulate_a), ('B', nb.simulate_b)]:
    r = sim(req, mk)
    print('S03', k, [(e.ts, e.price) for e in r.canonical_events
                    if e.kind == 'stop_triggered'], r.net_pnl)

f = FIX['E03']; old = c.POLICIES[f.request.policy_version]
try:
    c.POLICIES[f.request.policy_version] = old.model_copy(update={'wallet': D(999)})
    for k, sim in [('A', simulate_a), ('B', nb.simulate_b)]:
        try:
            print('S12', k, 'ACCEPTED', sim(f.request, f.market).net_pnl)
        except Exception as e:
            print('S12', k, type(e).__name__)
finally:
    c.POLICIES[f.request.policy_version] = old

req, mk = e03(plan_upd={'sizing': c.Sizing(mode='fixed_qty', qty=D(10)),
    'stop': c.Stop(price=D(1)), 'entries': [
        c.Entry(kind='limit', price_lo=100, price_hi=100, fraction=D('.5')),
        c.Entry(kind='limit', price_lo=90, price_hi=90, fraction=D('.5'))]},
    market_upd={'last': [pt(0, 100), pt(30, 90), pt(60, 105)],
                'mark': [pt(0, 100), pt(30, 20), pt(60, 100)]})
r = simulate_a(req, mk)
print('S07', [(e.order_id, e.qty, e.price) for e in r.canonical_events
             if e.kind in c.FILL_KINDS and e.leg == 'entry'], r.net_pnl)

req, mk = e03(plan_upd={'sizing': c.Sizing(mode='fixed_qty', qty=D(1))},
    market_upd={'last': [pt(0, 100, '.5'), pt(60, 105)], 'mark': [pt(0, 100), pt(60, 100)]})
r = simulate_a(req, mk)
c.check_invariants(req, r)
print('S13 step1', [(e.leg, e.qty) for e in r.canonical_events if e.kind in c.FILL_KINDS], r.net_pnl)
PY
```

### 5. 新增问题与偏差/凭据复核

**S14（open，阻断 P1）：冲突隔离修复在源包修订时保留消失日期的旧 silver。**

- 位置：`vision.py:399–403`，`ingest_bytes` 只遍历新版 sdf 中仍存在的 day 并写 part.parquet。原先正常的某天在新版全部变成冲突（或被源修订删除），该天不再进入循环，旧文件未失效。
- C2 的内存文件系统反例先写一条正常行，再写同键两候选冲突的新包；新版manifest的days为空、冲突隔离记录有两条，但旧source_sha256的silver仍有一行。不是“旧bronze为追溯保留”，而是可执行silver仍暴露旧有效数据。
- `partition_check.load_partition` 枚举日期路径、loader同样按路径加载，均不按新manifest.days/source_sha256筛选，因此后续体检可能把旧有效行当成新版数据。未在本会话写真实湖验证该后续链，后续影响为源码推断；旧文件不失效本身已由实际 ingest 逻辑+内存写入记录复现。
- 修复/验收：按版本发布完整silver清单或使本源拥有且新版消失的日分区失效；loader核对逐行源版本与选定manifest。必须加入“旧完整日→新版全冲突/全删除→再次体检/加载”的端到端反例，仍保留旧bronze作为历史证据。不要简单删除其他来源拥有的分区。

偏差与边界核对：

- **前视/因果**：A 启动bar过滤与闭合funding mark原反例已修；B 仍有 C3 所示包含启动时刻mark bar的极值污染。S04 在观察终点之后摄入资金费，属于标签窗口泄漏。未发现新增“把未来funding费率送入决策特征”的直接路径。
- **幸存/来源偏差**：S14 使新版已隔离的数据以旧有效行复活；S05仍接受BREAK状态/未知最小名义额。rules_from_manifests 仍以已下载首行推断上市、向后无限TRADING，不能用来证明完整历史品种集合。P2生命周期/退市证据与44品种扩展仍 gated；不能把当前单品种合成验证升格为无幸存偏差证明。
- **特征库**：S09同名sequence会静默退回旧值。spike_scores仍用全分区median/MAD，但当前只标记、执行未按spike删样本；没有看到本轮新增按未来尖刺分布筛选机会的直接路径。last_closed_bar仍按H0闭合时间处理，不代表任意晚到数据安全。
- **凭据边界**：静态扫描与test_no_services_import未发现market读取生产账户配置、私有API或import services；新增quarantine/helper也无凭据入口。Vision仍只有既有公开源默认值，hosts参数与redirect不是硬白名单，未把它描述成强网络隔离。本次仅MockTransport与本地文件读取，没有发起外部请求。

### 6. 22 个夹具的独立性复核

只读执行 `runpy.run_path('tests/market/fixtures/build_episodes.py', run_name='readonly_review')`，未调用main；将F字典与磁盘22个JSON逐对象比较，初读和TTL更新后均输出 `builder_vs_json 22 []`。builder import只有datetime/json/Path和contract.resolve_policy；期望仍由显式Events表、手写字符串与derivation构成，无A/B/execution/ResultReducer调用、无读取报告/内核输出再回填的路径。

逐例核对事件输入与算式，并以独立Decimal表校对22个gross/fee/funding/net/R，输出 `MANUAL_SCALARS 22 PASS`。本次手算核对重点如下（零费用/资金费者省略）：

| episode | 独立核算与事件依据 |
|---|---|
| E01 / E02 / E03 | 100→90为−10/R−2；异步mark94、last98为−2/R−.4且MAE−6/5=−1.2；仅last94不触SL，105退出为5/R1。 |
| E04a / E04b / E04c | 同bar long primary先H110为+10/R2；long adverse先L90为−10/R−2；short adverse先H110为−10/R−2。不是把A的获利路径选作默认。 |
| E05 / E06 / E07 | 平仓同刻先扣1×100×.001=.1，net4.9/R.98；开仓同刻q(t−)=0，net5/R1；short收.1再付.2、100→99毛利1，net.9/R.18。 |
| E08 / E09 / E10 | IOC容量1只进1、撤2，毛利5/预算10=.5；GTD等号到期无成交R0；markSL优先、last105平1，净5且不反向。 |
| E11 / E12 | 100+98入2、105出2：毛利12/R1.2；2@105、1@110、1@94：10+10−6=14，均价103.5、R.7。 |
| E14a / E14b / E14c | 100.5不合tick1、100<min_notional101、100>钱包99：各自指定拒因、无成交、net/R0。 |
| E15a / E15b / E16 | 持仓到窗尾、mark121秒、funding缺证据：各保留入场及相应coverage，net/R均null，未被0回填。E16说明仍写8h网格，当前实现实际为schedule=false入场即删失，说明需同步但gold未改配内核。 |
| E17 / E18 | 101买、109卖毛利8，fee=.0505+.0545=.105，net7.895/R1.579；mark94下入场后重检，容量2共享入1/出1，毛利0。 |

最终22个expected对象的独立摘要（`json.dumps({id: expected}, sort_keys=True)`后SHA256）：`7a5fee6e7bcebe71104721ffa23c6fd862f498ca6fce162bea3b2a61cabb1bf3`。当前独立构造方式和手算结果通过；**未提交的历史夹具没有可审计的期望冻结提交链，不能证明作者过去从未参考过输出，也不能凭闭合记录断言本轮历史上只改policy_hash**。本审查确认的是当前未发现内核输出回填路径和当前22例经济期望一致。

现有22例仍为20 primary、2 adverse、0 favorable，全部base；E13缺失随C01 gated，完整场景×成本矩阵登记P2。≥10门槛确已满足，但这些夹具未覆盖C1–C3，不能用于豁免本轮反例。

### 7. 最终判定

P1所需“必修闭合”仍未达到：S04/S07/S09/S10/S13在当前合成接口内存在确定性反例，S02/S05/S06及新增S14的证据/来源边界仍能误放行，S11/S12的门禁与冻结要求未完整落实。不能以166项全绿、A22/22或报告返回0替代这些验收。

S03的B专属残余、S08的非1 multiplier残余明确受P2/B定型和C02门禁约束，不要求本轮选定内核；C01管理命令、C04完整历史filter、真实延迟/生命周期敏感性亦保留P2或既有gate。上述边界不覆盖已经实现且用于P1的静态执行、as-of、入湖隔离和报告解释门禁。M-03网络实跑证据单独为insufficient，但已有确定性错误，所以整体取fail而非insufficient。

二审终裁：fail
