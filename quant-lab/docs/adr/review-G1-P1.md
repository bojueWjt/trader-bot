# G1 P1 独立审查

终裁：**fail**。必修共 **16 条（S01–S16）**；首轮 100 个测试通过，收尾并发改动后补跑 101 个通过，仍不足以证明冻结契约、因果闭包和不可变发布成立。并发变更说明见 §10，覆盖正文中相关旧状态。
审查日期：2026-09-11（Asia/Manila）；审查者：当前 GPT-6 Codex 主会话，未委派 Grok 或再次调用模型。
范围：工作区现状的 D-03–D-09；只写本报告。测试、verify 的临时输出按用户授权运行；未改实现、测试、契约、看板或研究湖。
未联网、未读生产配置/凭据、未采集 Telegram、未启动真实 LLM、未执行真实批次放行。
优先级：research-schema v1 §9（含 §9.6）优先于旧表；execution-interface §5.1 引入的 G2 修订报告 §3.1 为 order_plan 规范附件。
GOAL-1 旧文的边界 `≤` 不覆盖严格 as-of 要求；闭合 bar 的 `close_time <= at` 则是已明确批准的独立条件，不能混为同一种等号。

## 1. 审查基线和证据范围

- HEAD：`5d9c89e22cb1359661bbc382a158f70ee33a12c8`；工作区本来存在未提交/未跟踪文件，本审查不是仅看 HEAD diff。
- 已读 AGENTS.md、GROK.md、GOAL-1、两份指定契约及 G2 规范附件、511 行 ADR-G1、taskList.json 的 modules.data 全部任务/notes/review，以及指定 data 模块与 tests/data、合成和录制夹具及其生成器。
- code-review skill 已读取；它的提交差异工作流不适用于本次指定目录全量验收，采用用户给定七项检查和三档输出。
- `src/quant_lab/data` 16 文件的树摘要：`8b5cf1f553dcf22122e231a360ea7b264d3f2c55a4fe6f4a1ee2e622ab30ee6e`。
- `tests/data` 35 文件的树摘要：`989485c2585e99e1ede86fb4865c245d73bf74e2e1879bed78e392104c9de340`。
- 树摘要算法：排除 `__pycache__`，路径排序，对逐行 `<仓库相对路径>:<文件 sha256>\n` 再做 sha256。
- research-schema 摘要：`99c1c274144f8d7ea90c787a7ee76a5ad7b0774d85978660287adb8cc2040371`。
- execution-interface 摘要：`bef79b45ec065429ea21be27bc64454fd430b1d4ffbcacc5cdeb082bd7f370a1`。
- ADR-G1 摘要：`916615d0b13e3471c4f0dd5525ea897f6d6ca2377f3469ec24ce61d7997d3015`。
- 本次直接读取默认湖得到描述图 34 episodes / 50 events，默认决策视图 27 行；不沿用 notes 中旧的 35/51 自报数。
- 以下运行时反例均在内存构造/重放或只读现有 Parquet；没有通过修改业务文件来制造失败。

## 2. 逐任务 verify 实跑

cwd 均为 `quant-lab`。环境加 `PYTHONDONTWRITEBYTECODE=1`、`PYTEST_ADDOPTS='-p no:cacheprovider'`，避免写仓库字节码与 pytest 缓存。verify 正文按看板原样执行，每条单独捕获退出码，没有 tail 管道吞掉 pytest 失败。

全套命令：`.venv-g1/bin/python -m pytest tests/data -q`。
实际输出：`100 passed in 1.16s`；退出码 **0**。

### D-03

```sh
.venv-g1/bin/python -m pytest tests/data/test_normalize.py -q && .venv-g1/bin/python -m quant_lab.data.normalize --fixture tests/data/fixtures/tdesktop_sample --out /tmp/ql-norm && .venv-g1/bin/python -c "import polars as pl;d=pl.read_parquet('/tmp/ql-norm/message_version.parquet');assert d.height>0;print(d.height)"
```
实际输出：`14 passed in 0.39s`；CLI `batch_id=tg-3146396a4422, n_messages=191, n_versions=191, n_versions_added=0, n_quarantine_rows=5, quarantine_total=41`；末行 `191`；退出码 **0**。
时间级输出：`H0=181, H1=2, U=1, V=7`；输出路径 `/tmp/ql-norm/message_version.parquet`。
有显式非空断言；但固定 /tmp 路径已存在，added=0，不能拿该末行独立证明本轮新产物。pytest 在新临时目录另验证了 >180 行和三时钟。

### D-04

```sh
.venv-g1/bin/python -m pytest tests/data/test_dedup.py -q
```
实际输出：`10 passed in 0.53s`；退出码 **0**。
命令本身无行数断言；测试有 `dg.height>0`、重复引用/候选组计数非零和原始行保留断言。未验证相同文字的独立新单必须保留，见 S05。

### D-05

```sh
.venv-g1/bin/python -m pytest tests/data/test_extract.py -q && .venv-g1/bin/python -m quant_lab.data.extract --bench ../eval/hermes/v3-trader-signal-bench --report /tmp/ql-extract.json && .venv-g1/bin/python -c "import json;r=json.load(open('/tmp/ql-extract.json'));assert r['n_items']>=30 and r['parser_recall']>0;print(r)"
```
实际输出：`13 passed in 0.45s`；退出码 **0**。报告的主要输出如下（逐项 items 列表和重复打印的同一字典省略）：
```text
bench=/Users/balen/projects/trader-bot/eval/v3_trader_signal_bench/dataset.json
bench_warning=bench 路径 ../eval/hermes/v3-trader-signal-bench 不存在，回退到 /Users/balen/projects/trader-bot/eval/v3_trader_signal_bench/dataset.json（看板 verify 的 ../eval/hermes/v3-trader-signal-bench 路径漂移）
n_items=30; n_actionable=16; parser_version=parser-v0.1
parser_recall=1.0; parser_strict_recall=0.75; skip_precision=1.0; action_acc=1.0
field_acc: symbol_ok=1.0, side_ok=0.9375, entry_ok=0.8125, sl_ok=1.0, tps_ok=1.0
co_error source=eval/v3_trader_signal_bench/results.json
gpt-5.6-sol/spark-medium: side both_wrong=1/16, entry=1/12
spark-high/spark-xhigh: side both_wrong=0/16, entry=1/12
四组 action=0/16、symbol=0/16、sl=0/14
```
确有 `n_items>=30` 与 `parser_recall>0` 断言。回退不是不存在的测试证据，但看板路径必须修正；指标只证明这 30 条上的粗动作命中，不是全窗口召回。

### D-06

```sh
.venv-g1/bin/python -m pytest tests/data/test_normalize_validate.py -q
```
实际输出：`7 passed in 0.42s`；退出码 **0**。
测试含 cp>=20、scale ok>=10、L5 output>=10，非空成立；缺未来 available_at、阈值版本时钟、未知价格与媒体依赖检查，见 S03/S04/S07/S13。

### D-07

```sh
.venv-g1/bin/python -m pytest tests/data/test_linker.py tests/data/test_lifecycle.py -q && .venv-g1/bin/python -c "from quant_lab.data.api import load_episodes;d=load_episodes('fixture-v1');assert d.height>=5;print(d.select('episode_id','author_plan_state','author_claim_state'))"
```
实际输出：`16 passed in 0.77s`；`shape: (27, 3)`，打印列为 episode_id / author_plan_state / author_claim_state，状态 active / unknown；退出码 **0**。
有显式 >=5；后半段读取预先存在的默认湖，并不证明它来自前半段的新临时构建。测试的可见性断言主要对自行过滤的 DataFrame 验证，未验证公共投影所有列的未来不变性。

### D-08

```sh
.venv-g1/bin/python -m quant_lab.data.harvest --report /tmp/ql-1a.json && .venv-g1/bin/python -c "import json;r=json.load(open('/tmp/ql-1a.json'));assert r['n_messages']>0 and 'edit_ratio' in r;print(r)"
```
实际输出：`NotImplementedError: harvest 待实现（PoC 1a/1b）；args={'report': '/tmp/ql-1a.json', 'export_dir': None, 'allow_network': False}`；位置 `harvest.py:21`；退出码 **1**。
第二段非零断言未执行；未获得本轮 1a 报告。已检查实现，调用在任何采集之前抛错，未绕过账号/导出授权。gated/todo 的理由成立，不能记成 verify pass。
若 P1 门继续字面要求 D-03..D-09 全过，门不成立；G0 应明确合成 P1 的 D-08 豁免/离线报告契约，不能让 reviewer 宣称真实 PoC 已完成。

### D-09

```sh
.venv-g1/bin/python -m pytest tests/data/test_audit.py -q && .venv-g1/bin/python -m quant_lab.data.audit --oc 200 3 --oc 400 0 | grep -E '0\.5%.*98\.1|0\.5%.*13\.4'
```
实际输出：`7 passed in 0.55s`；`p=0.5%  200/3 accept=98.1319%  400/0 accept=13.4658%`；退出码 **0**。
pytest 独立数值断言两方案均存在且数值正确；grep 只是 OR，一支命中即可，且 audit 管道未设 pipefail。该命令本身没有独立断言两项都输出，见应改 A01。

## 3. 字段和接口对照

下表逐字段组核对列名/类型/语义；“在”只表示存在，不代表有效性已经验收。未冻结的具体小数精度不擅自裁定。

| 对象 / 契约字段 | 实现位置和实际情况 | 裁定 |
|---|---|---|
| MV source_id/source_version_id/version_no/content_hash/text | normalize.py:44/210/337：struct 身份、哈希和列齐；text 被 NFC/换行归一；版本证据 hash 仅含类别，追加会重编号 | 不可变/版本身份问题 S10；原文与规范文须区分 |
| MV media_hashes/media_uris/reply_to_message_id/forward_from/grouped_id | 列在，媒体按 hash 拷贝，URI 相对 media 目录；相册只有 grouped_id，无必要媒体到达时钟 | S03/S13 |
| MV message_date/last_edit_at/first_seen_at/snapshot_at/time_grade/survival_scope/deleted_after_observation/raw_hash/raw_uri/三时钟 | 列在，UTC us；H1 用 edit+60s；deleted 恒 null，V 直接推出 watched_cohort | S02/S09；生存范围不能由时间级单独推导 |
| MV §9.1 channel_id/message_type/sequence/version_evidence/temporal_assumptions/text_entities/media_kinds/asserted_event_time | 前七列在；asserted_event_time 缺失；sequence=message_id；实际 evidence 含 `export_snapshot+album_infer`，超冻结 enum | S03/S08 |
| EX extract_id/source_version_id/kind/symbol_raw/instrument_id/side | 列在；规范化写到内部 canonical_plan，公共 extracted_event.instrument_id 恒空；一消息只产一条 parser 结果 | 分层落表语义须仲裁；多腿缺口 S05/S13 |
| EX entry/stop/tps/size_hint | entry lo/hi、stop、tps level/fraction、size_hint qty/fraction/notional 都 Float64；leverage Int64；tps.kind 在 | 明确 decimal 字段漂移 S08；未知分配问题 S06 |
| EX spans/bboxes/extractor/confidence_bucket/reason_codes/三时钟 | 列在；bboxes 永远 []；parser 不运行全字段证据复验；LLM usage/response hash 未持久化 | S13 |
| EX §9.1 channel_id/version_no/size_hint 四子字段/tps.kind | 列名齐；非规范新增 message_id/entries/expires_after_s/plan_ref/text_hash/rule_version/batch_id | 新增接缝字段应登记，不能只靠本地 schema |
| EP episode_id/graph_version/predecessor_ids/successor_ids/channel_id/trader_id/instrument_id/side/entry_branch_id/duplicate_group_id/cluster_id | 全在；trader_id=channel_id 字符串；predecessor/successor=[]、cluster=null | 身份归因与迁移能力未完成 S05/S11 |
| EP author_plan_state/author_claim_state/entry_observed/exit_observed/left_truncated/right_censored/censor_at/censor_reason | 列在；P×C 表面值齐；终点/守卫问题 S09；默认视图部分屏蔽 | S01/S09 |
| EP time_grade_min/decision_eligible_at/claimed_outcome/reconstructed_outcome/eligibility_by_estimand | 列在；两个 outcome 是 String 而非 struct；eligibility 是 JSON String 而非 map；time_grade_min 用全部未来成员 | S01/S08 |
| EP audit_stratum/sampling_probability/label_status/signoffs/derivation_hash | 列在；前两恒空、label_status=unresolved、signoffs=[]；签字子结构尚待明确，不能宣称审核态已发布 | S10/S14 |
| EP §9.1 t_dec/processing_delay_s/order_plan/decision_snapshot_hash/temporal_assumptions/is_tombstone/tombstoned_at/migration_reason | 全在，包括本轮实读的 migration_reason；processing_delay_s=1；无 execution_state | 旧“缺 migration_reason”已闭合；order_plan 类型和语义见 S06/S08 |
| EE event_id/episode_id/graph_version/kind/event_seq/extract_id/source_version_id/supersedes_event_id/link_method/link_confidence/edge_available_at/payload/三时钟/channel_id/plan_id | 全在；available_at 写成 edge_available_at，违反 §9.1 源版本时钟；版本链 payload 不含 §9.6 要求的 basis | S08；event_seq Int32 的位宽待统一 |
| Q 身份六列、来源时钟、检测九列、处置、血缘 | lake.py:28/201：字段组齐、32 列、复合键幂等，旧处置不会被重跑覆盖；空 dependency/consent/retention 常见 | 严重度 S07；处置历史和撤权传播 S11 |
| LOSS batch_id/flow/layer/layer_name/stratum/input_unit/input_n/output_unit/output_n/n_ok/n_review/n_quarantine/n_dup_ref/primary_reason_dist/cum_excluded_ids/cum_excluded_weight/rule_version/schema_hash/mapping_hash | 19 列在；layer 1..6 合 §9.6；缺三时钟；cum 为本层数/零；mapping_hash 是常量式 tag hash，未对应实际 MAP | S12 |
| §7 load_episodes(graph_version, *, decision_graph=True) | 参数名、keyword-only/default 对齐；但不校验 manifest 绑定 | S01/S11 |
| §7 load_episode_events(graph_version) | 实现新增 keyword-only decision_graph=True，兼容旧调用但签名未经 §9 批准；测试只断言第一个参数 | S08，按 manifest 绑定图型或先仲裁 |
| §7 loss_table(batch_id) | 对齐；额外接受 latest | latest 是便利入口，应记录实际 batch |
| §7 quarantine(flow, *, status=None) | 实现给 flow 增默认 telegram；status keyword-only 对齐 | 默认值漂移，S08 |
| §4+§9 ReasonCode | 首轮 codes.ReasonCode is reasons.Reason，29 项；收尾 codes.py 被其他会话删除 | 首轮闭合，最新再次违背 §9.5 的公共路径；并入 S08，详见 §10 |

## 4. 必修清单

### S01 必修：决策投影泄漏未来生命周期

- 位置：`src/quant_lab/data/api.py:31`；`lifecycle.py:242–281`。
- 问题：只替换 P/C 和部分 outcome/censor 列，仍公开完整图的 n_events、n_invalid_transitions、reason_codes、time_grade_min、derivation_hash、eligibility.outcome 及最终复制组等信息；并未从决策闭包重建所有列。
- 实证：A#103 根单独重放与附加后续事件，decision_snapshot_hash 相等，n_events 从 1→3、eligibility.outcome 从 false→true；默认 API 已返回该 outcome=true，同时把 exit_observed 改为 false。G3 可直接据此获取未来标签。
- 改法：建立明确的决策列投影和按根截止的字段级重放；完整生命周期诊断留描述视图；所有决策列纳入同一快照 hash。管理事件应有独立快照或在接缝裁定前明确拒绝发布该用途。
- 验收：对公共 API 全列做未来编辑/结果/补父/复制扰动对比；旧根、旧截止下语义列及 hash 不变，不能只比较 stop 或事件过滤结果。
- 收尾更新：其他会话补 dec_n_events/dec_reason_codes/dec_derivation_hash 并移除 eligibility.outcome，已缓解上述具体暴露；仍沿用全生命周期 time_grade_min/复制组，且将 n_invalid_transitions 无条件填 0，未达到全字段闭包验收。新增测试还显式排除了 decision_snapshot_hash 比较，S01 保持未闭合。

### S02 必修：H1 编辑时钟提前且被复活为可执行入场

- 位置：`normalize.py:190–197`；`validate.py:128–139`；`lifecycle.py:258–267`；`test_normalize.py:76`、`test_lifecycle.py:80`。
- 问题：仅最终编辑版的 available_at=last_edit_at+60s，忽略首次可证明完整快照；原始入场只标 original_entry=false，execution 仍 true，lifecycle 又赋 entry_decision=true。缺证据证明编辑后机会仍有效。
- 实证：默认 API 的 A#120 为 H1、t_dec=2024-04-02 10:21:01Z、original_entry=false，但 execution/entry_decision=true；测试把这项提前可知当正确期望。ADR §7/§9 要求首次完整可证时刻，非 edit_date 的假设回填。
- 改法：版本可见证据与编辑发生时刻分开；无完整可用证据时只描述/隔离原始入场，明确新提议才建新决策；历史假设不得自动晋级用途。
- 验收：编辑 10:20、首次证实 10:25 的例子在 10:21 不可用；补最终版不能复活已结束的机会；V 配对子例继续保留真实旧 SL。

### S03 必修：闭包缺依赖，等号及顺序证据不成立

- 位置：`graph.py:32–53`；`lifecycle.py:206/266`；`linker.py:113–140/171`；`normalize.py:175/230`。
- 问题：closure 仅 max 传入时间，调用只传 root.available_at；无 DFS/环检测/字段媒体或市场规则 DEP。reply 递归只保留终端根和事件两端，不计中间父帖；缺端点时间也通过过滤 None 取 max。窗口允许 dt=0；message_id 被写成 sequence，未知序列被转成 0。
- 实证：根在 T、中间父帖在 T+1d、子事件在 T+1h，子边仍 strong 且 edge_available_at=T+1h；无证明顺序的同刻窗口根/管理照样 link。
- 改法：显式依赖图、完整证据闭包与循环拒收；未知时钟传播 unknown；sequence 只来自实证；同刻未知严格 <。裁决实际用过的候选和上下文也入 hash。
- 验收：分别推迟节点、边、字段、媒体、市场校准时间，相关旧快照拒收；循环、缺父、null 时间和同秒无序不能通过；不能用固定 +1s 掩盖缺依赖。

### S04 必修：行情只检查闭合，未来可用/修订值仍可进入

- 位置：`market_stub.py:34–43`；`validate.py:145–203`；被调用的 `market/asof.py:126–166`（只读定位，上游修复由 G2 负责）。
- 问题：FrameMarks 直接调用仅按 close_time+latency 过滤的 last_closed_bar，未限制记录 available_at。T_plaus 只是数值字典，没有版本、冻结时刻、样本量和依赖证据。SyntheticMarks 插值用未来锚点，且 validate.run 默认选择合成 provider。
- 实证：close_time=T−1min、available_at=T+1d、close=123 的单行被 FrameMarks.mark_at(T) 返回 price=123、reason=None。bar 已闭合并不代表该版本当时已知。
- 改法：在冻结 as-of 接缝增加可用版本约束或先由 G1 提供严格可见视图；阈值/品种登记增加可用时刻和版本；合成 provider 明确限定 fixture 模式，外部导出不得默认用它校验。
- 验收：未来 available_at、最终修订晚到不能影响旧 mark；120 秒边界、未闭合 bar、同刻有/无顺序分开测试；未来阈值拒用。H0 闭合整点等号本身是合同允许项，不需一刀切改成 <。

### S05 必修：归属强守卫和独立计划保留不足

- 位置：`linker.py:82–196`；`dedup.py:163–188`；`extract.py:260–462`。
- 问题：reply 找到一条根即强，无品种/方向/多腿守卫；first_of_source 选一条掩盖同消息多分支；plan_ref 全批索引无决策截止/作者命名空间；quote 未实现。去重对不同 source_id 的同文 72h 内直接折叠，缺“确为重发”的身份依据。
- 实证：ETH short 管理 reply BTC long 根仍 strong link；把 A#103 复制为同频道新 message_id=999、晚一天，只有文字相同便 is_canonical=false、DUPLICATE_EXACT，后续根不再产生。
- 改法：按证据+分支+语义守卫判强弱；候选索引按当时可见集合生成；一消息多提议分别建分支；不同 source 的同文先作复制候选，确认重发才折叠，保留明确新单。
- 验收：reply 多腿、方向/品种冲突、plan_ref 后续复用、跨频道指针、超窗强引用、同文独立新单与确切重发均有成对断言；追加未来候选不改变旧选择。

### S06 必修：单位传播和 order_plan 猜测无来源数字/动作

- 位置：`extract.py:235–257`；`lifecycle.py:113–135`。
- 问题：取整条消息第一个 万/k 锚点按数量级传播，不检查价格语义或已有单位；缺 entry 自动变 market_ref；多 entry 平分、缺 TP fraction 均分，属于 ADR §2.3 禁止的止盈分配/仓位猜值。
- 实证：`BTC 做多 入场 6 止损 5 止盈 7\n历史成交量 6.5万` 产出 entry=60000、stop=50000、TP=70000；`BTC 做多 止损 90` 无 entry 也产 IOC market_ref；三个未给比例 TP 自动各 0.333333。
- 改法：单位绑定字段/同一价格表达的来源，冲突隔离；缺入场方式不等于市价；未给比例留缺失并交经 G0 冻结的 G2 policy 展开，不能在 gold 冒充作者事实。
- 验收：非价格单位、多币种、多单位消息不得改价；市价必须有原文证据；原文未给 fraction 不产生默认分配；每个派生数字能追到原文及明确变换规则。
- 收尾更新：其他会话增加价格语境筛选，换行版成交量反例已修；同句 `BTC 做多 入场 6 止损 5 止盈 7；历史成交量 6.5万` 仍将入场放大为 60000、止损 50000，并把 65000 混成 TP。默认 market/fraction 问题也仍在。

### S07 必修：隔离标记在产线中丢失，致命错可降为一般

- 位置：`extract.py:528–559`；`validate.py:128/218–223`；`lifecycle.py:258–265`；`lake.py:228–242`；`reasons.py:44–77`。
- 问题：extract 不消费 MV quality_status/reasons；validate 重建 reasons4+reasons5，丢 EX 歧义原因；方向错误只写 INTENT_AMBIGUOUS，未禁 execution；API 不限制 entry_decision/execution=false 行。Q severity 只看主原因，主优先级先一般后致命。
- 实证：Q 输入 EDIT_ORIGINAL_UNAVAILABLE+UNIT_SCALE_CONFLICT 得 severity=general；默认决策 API 含 SYMBOL_TIME_INVALID 的 instrument=null 行，也含 UNIT_SCALE_CONFLICT 行。保留隔离数据是对的，但用途准入没有闭合。
- 改法：保留跨层拒因/血缘，按用途传播限制；主原因先严重度后规则优先级，severity=max(all reasons)；方向/品种/归属错不可降级；公开决策消费需执行明确准入或返回不可消费状态并强制校验。
- 验收：混合一般/致命码必为 fatal；缺媒体必要依赖、方向歧义、数量级错不能被后层洗成可执行；隔离对象仍可在描述/审核框追溯。

### S08 必修：冻结 schema 和公共签名仍有漂移

- 位置：`normalize.py:44/209`；`extract.py:36`；`lifecycle.py:75–109/245–254`；`api.py:54/77`；上方字段表。
- 问题：MV 缺 asserted_event_time；version_evidence 拼串超 enum；decimal→Float64；EP outcome struct/map→String；EE available_at 不等源版本，版本链 plan_ref 缺 payload.basis；公共 API 擅增参数/默认值。§9.6 批准 plan_ref 是附 basis 的批准，并非可省。
- 实证：version_evidence 实含 export_snapshot+album_infer；amend payload 有 stop/tps/from/to/action，却无 basis；inspect.signature 得 load_episode_events(graph_version, *, decision_graph=True)。
- 改法：按 v1 对齐已冻结字段；尚未冻结的精度/嵌套类型提交 G0 仲裁，不私自发布另一 schema。补 API 精确签名和嵌套类型验证；重新发布对应版本产物。
- 验收：对实际 Parquet 而非只对常量查全列、嵌套类型、可空性、enum、FK、三时钟；EE source available 与 edge available 可不同且分别正确；版本链 basis 可消费。
- 收尾更新：codes.py 被其他会话删除，`import quant_lab.data.codes` 实跑 ModuleNotFoundError，而未变化的契约 §9.1/§9.5 仍指定 codes.ReasonCode。恢复兼容别名或先走 G0 接缝修订，不能通过删掉对应 smoke 断言消除合同要求。

### S09 必修：状态表之外的守卫、期限和删帖生命周期未实现

- 位置：`lifecycle.py:52–62/188–264`；`validate.py:24`；`extract.py:419`。
- 问题：108 格文本对齐，但 J 没有真正新建带证据分支，R 不触发纠错重放/失效；L 却能应用 stop/tps 补丁。expire 无来源守卫，合成到期不检查 observation_end；close/cancel/后续事件未按观察截止筛选。delete_notice 在 canonicalize 前被过滤，deleted_after_observation 不更新。
- 实证：observation_end=2024-05-20 10:00Z 仍生成 2024-05-21 09:00Z 的 expire，并回放成 expired；观察尚未到期却不记相应右删失。当前 tests 把 transition action 返回正确视作完整 reducer 验收。
- 改法：将 J/R/L/expire 守卫与动作真正接到 reducer；按观察边界处理已到期与未来期限；censor 只截观察，不伪造作者状态。删帖证据保留并限制受影响用途，不能统一删除完整实收 cohort。
- 验收：12×全部 kind 独立期望，守卫真/假各测；提前管理合法但不补 entry；到期前/后、取消后持仓、H 边界外平仓、纠错、只证明删除与明确撤回分别验收。

### S10 必修：不可变版本身份和内容哈希保护可被绕过

- 位置：`normalize.py:210–215/337–356`；`lifecycle.py:264–268/324–337`；`extract.py:493/615`。
- 问题：MV hash 只含 version_evidence 类别，不含编辑/观察证据内容；相同内容的不同编辑状态折为一版。追加重算旧 version_no。图 input_hash 只含 plan/candidate ID，不含选边、JD、规则参数、观测终点/处理延迟；force 可覆盖同名版本；重跑写 ingested_at。
- 实证：同 source/text、不同 edit_date 和 first_seen_at 的两条内存输入合成 1 行；processing_delay_s 从 1→2 会改变快照，但 input_hash 表达式完全不含它。删除一行 cp 的测试不能覆盖字段值/选边变化。
- 改法：冻结版本证据规范化内容；版本顺序独立索引或未裁定时拒绝旧行重编号；派生 hash 覆盖全部语义输入/规则/假设；同名版本冲突必须拒绝，force 不得改已发布图。
- 验收：内容回退编辑、仅改时钟/阈值/选边/处理延迟/终点均被检测；同输入重跑原对象首次 ingested_at 和字节不改；新空湖语义重放一致。

### S11 必修：manifest 发布、迁移和撤权没有形成消费屏障

- 位置：`lifecycle.py:315–340`；`graph.py:75–123`；`api.py:31–65`。
- 问题：EP、EE 分别 replace 后普通写 manifest；API 不读取 manifest/hash/状态，缺 manifest 也能读。record_migration 不 tombstone 旧图、不连新 EP predecessor；只有整图手工 tombstone，没有 source 级依赖撤权、下游回执、stale 屏障、恢复协议。
- 证据：test_api_decision_and_description_views 只复制 EP/EE 到新湖、没有 manifest，仍预期 API 成功；tombstone API 文案让审计直接读旧 manifest，却未区分 CONSENT_REVOKED 的原文读取限制。
- 改法：实现 G1 内 staging、文件 hash、单一发布指针和拒读未提交/失效版本；谱系+屏障+消费回执按 ADR §8；协议未裁定前禁止将迁移产物作为完整研究版本发布。
- 验收：二合一/一拆二/改归属、提交每阶段崩溃、并发代际、source 撤回传递闭包与备份恢复先应用撤权；任何 EP/EE 混版本、缺 manifest、缺回执不可消费。

### S12 必修：损耗表不守恒、无实际映射账且累计量填假值

- 位置：`lake.py:63/276`；`normalize.py:307–327`；`extract.py:567–587`；`validate.py:247–267`；`lifecycle.py:292–304`。
- 问题：三时钟缺失；主状态混用 message_id/版本/输出 episode；LLM 一入多出没 MAP；cum_excluded_ids 逐层重新计数而非对象并集，权重未经依据直接 float(count) 或 0；空层/分层可能不落行。
- 实证：现湖 L1 input=191，但 ok186+quarantine4+dup0=190；L4 input54、ok49+review0+quarantine2=51；累计排除 L3=133→L4=6→L5=2→L6=0。L3 输出49与 L4 输入54 未给 LLM 扩张映射。
- 改法：以冻结输入单位逐对象互斥记账；多出/合并/引用/排除有 MAP，mapping_hash 对实际映射内容；累计按用途计算集合和已知权重，未知留缺失；LOSS 补三时钟。
- 验收：逐层逐 stratum 输入状态守恒、跨层单位转换有映射、累计排除不凭空下降、空层可诊断；纯图/负类/隔离/LLM 拒答都能追回分母。

### S13 必修：媒体证据和离线裁决工具未达到 ADR 合成范围

- 位置：`extract.py:260/466–489/493–523/601–612`；`llm.py:106–166`；`linker.py:176–196`；`tests/data/fixtures/gen_tdesktop_sample.py:39`。
- 问题：没有 OCR provider/录制 OCR、图文独立冲突检测、字段 bbox 依赖；图片为 8×8 单色，bboxes 恒 []。LLM 只实现本条文本抽取，无 adjudicate 协议/候选 id 校验/上下文截止/一次扩展和重试上限。字段验证不含 size_hint/期限/TP fraction 等，格式坏值可直接抛异常；两边 stop 都空会算 agree_stop=true。
- 理由判断：真实 LLM gated 合理；但离线协议、录制裁决、伪造证据/上下文测试不需要联网，不能以该闸门免除。纯图拒答保留样本是正确降级，不等于完成图文产线。
- 改法：先补离线 provider、结构化 schema、完整数值/身份/span/bbox 校验和有界拒答；图文冲突隔离；裁决只可选输入候选。未运行的抽取/校验报 not_run/insufficient，不算一致。
- 验收：可读纯图成功和模糊图拒答配对；图文冲突、晚图、越界 bbox、伪造 source/candidate id、非法数值类型、两空、超预算/重试上限各有独立录制夹具。

### S14 必修：审核器可在缺标注/缺独立复核下输出 pass

- 位置：`audit.py:91–108/158–193/197–230/255–266`。
- 问题：disagreement_report 跳过缺失配对、两空相等、无有效字段默认 key_rate=0；acceptance 仅靠调用者传 attempt/n，没有固定样本身份、修复证据或独立复核签字。窗口 bootstrap 只从有真实机会的 per_win 重采，丢掉抽中负窗口；标注头展示模型 instrument_id 和预测归属，非完整盲标。
- 实证：两份仅有 episode_id 的空标签返回 status=ok/verdict=pass；3 个抽中窗口仅一窗有机会时 n_windows 返回 1。现测试允许模型 instrument_id 出现在任务中。
- 改法：缺标签、字段、复核人明确 insufficient/not_run；审核身份/样本/首次与重验记录可验证，实际 n 与冻结 n 一致；bootstrap 按全部抽中窗口；盲标输入去掉模型品种/归属提示并保留全窗口漏机会入口。
- 验收：空标签、部分缺配对、同一复核人、缺签字不能 pass；固定 200/3 与一次重验约束可追溯；零机会窗口参与重采；小批全审和最终集全审单独闭合。

### S15 必修：测试矩阵有自证断言，五反例与夹具规格未完整兑现

- 位置：`tests/data/test_lifecycle.py:30/80–178`；`test_smoke.py:66/102`；`test_normalize_validate.py:91`；`fixtures/gen_tdesktop_sample.py`、`fixtures/gen_llm_recorded.py`。
- 问题：转移穷举只证明 action 非 move 时状态不变，没独立 108 格期望和守卫；schema 测试只查自家常量的三时钟且漏 LOSS；旧快照只查 SL 或自行过滤边。夹具 190 个 source/191 MV，只有一条 source 多版本，无 H2/必要媒体晚到/撤权恢复矩阵；不同于 ADR 的 216 source/246 MV、seed=20260911 和更丰富跨年证据。
- 五反例差异：CE1 固化错误 H1 时钟；CE2 把“TP1 到了”换成“到达 3000，止盈一半”并期待 reduce，是合理另一输入但未覆盖原反例；CE4 集成夹具有明确 reply，未覆盖无指针歧义→补 reply 配对；CE5 基础缺入口合法性有测试。五例均缺逐例闭包 hash/Q/LOSS 独立对账。
- 改法：补 ADR §12 的独立字段级 golden 和性质测试，不从被测实现生成期望；先加能抓住 S01–S14/S16 的回归，再报告覆盖状态。规格降级须由 G0 明确裁定。
- 验收：矩阵下表各组有可定位独立断言；复现本报告反例在修复前失败、修复后通过；P1 不能仅以 100 passed 和 >=5 行结案。

### S16 必修：导出媒体路径可越过授权导出目录读取本机文件

- 位置：`sources.py:132–145/221–226`；`normalize.py:116–131`。
- 问题：`base / rel`、`path.parent / md['path']` 未检查绝对路径、`..` 或符号链接逃逸；一旦路径指向可读本机文件，会先 sha256 再复制进湖。无需 import services 即可能跨越零生产凭据边界。审查未读取任何敏感文件来测试该路径。
- 改法：对路径 resolve 后验证处于明确授权导出根内；拒绝绝对路径/目录逃逸/指向根外的 symlink，并隔离来源；不要把原始不可信路径当成额外本机读取授权。
- 验收：用临时无敏感 sentinel 文件测试绝对路径、../、symlink 均不读不复制；正常根内媒体和缺媒体隔离路径仍可用。

## 5. ADR 偏离与自报理由裁定

| 项目 | 对照结论 |
|---|---|
| 六级 B0–B5 与清洗层 C1–C5 | 清洗+链接 1..6 已获 §9.6 批准；不等于离线 B3 裁决、B5 审核态及 MAP/DEP 全部实现。S12/S13/S14 |
| P×C 108 格 | 表格逐字对齐，U-01 终态普通管理按保守 I 可接受；J/R/L 及其余 kind 动作未闭合，S09 |
| 决策图闭包/严格 < | API 选边确实用 <；全字段投影、必要依赖与候选截止仍有漏洞，S01/S03 |
| 链接器强弱 | 删除 STRONG_LINK_METHODS 正确；运行时 reply 仍缺守卫，同文独立计划被折叠，S05 |
| tombstone/迁移 | 整图标记后普通 API 拒读、旧文件保留这一点通过；不是 ADR 的原子迁移/级联撤权，S11 |
| 增 layer=6、EE plan_id | §9.6 明确准许，非问题；1..5 未被重编号 |
| same_source → 公共 plan_ref | 粗枚举批准成立；payload.basis 缺失，未满足附加条件，S08 |
| processing_delay_s=1 | 作为合成固定正值合理，已落 temporal_assumptions；不能当实测延迟，亦不能代替 DEP |
| 无 T_plaus 记 insufficient | 合理；输入阈值无冻结时钟/样本量校验、波动率诊断未实现，不能宣称整项完成 |
| simhash 阈值未校准但仅弱组 | 未并生命周期降低风险；仍违 ADR “未校准仅精确”，须禁用默认近似或先冻结/仲裁，见 A03 |
| D-08/D-11 未运行、真实 LLM 未调用 | 授权闸门理由成立；不触发账号/预算动作。D-08 verify 失败及 P1 门文字矛盾须真实记录 |
| 原因码收敛、migration_reason | 首轮均闭合；收尾 codes.py 被删除导致公共路径回归 S08，migration_reason 仍在；严重度问题 S07 |

## 6. ADR §12 测试矩阵缺口

| 测试组 | 当前证据 | 尚缺 |
|---|---|---|
| schema/normalize | >180 行、UTC、hash 命名媒体、H0/H1/V/U | 冻结物理 schema、H2、edit 时钟损坏、raw hash 校验失败、相册晚到、未知 sequence |
| dedup/loss | 跨频道保留、近似数字差、不删 bronze | 同文新单、精确 source 版本身份、多腿 MAP、所有单位守恒/累计权重 |
| extract/llm | parser/LLM 独立成行、8 条录制中故意伪造 span 拒答 | OCR/bbox、两空不一致、全部数字字段、候选/证据 id、预算/重试/扩展 |
| validate/asof | 数量级大错、无阈值 insufficient、未闭合/stale | 未来可用版本、120s 恰界、零/负/NaN、未来阈值、30 日/10000 bar 波动率诊断 |
| linker | 唯一弱边、双候选拒答、reply 链 | 多腿/语义冲突/quote、plan_ref 复用、超窗强指针、未来扰动、同秒未知 |
| transition_product | 12×部分 kind 调用及少数具体格 | 全表独立真值、J/R/L 动作、守卫失败审计、delete_notice、无效事件字段不变 |
| five_counterexamples | 基础状态/部分字段 | ADR 原输入配对、逐例 hash/Q/LOSS、G2 未 fill 独立桩断言 |
| decision_closure | < 过滤、旧 SL 留存 | 五类依赖推迟/未知/循环、公共视图全列未来不变、缺 manifest 拒读 |
| migration/recovery | 手工整图 tombstone 拒读 | merge/split/改归属、缓存失效、下游回执、跨文件发布故障恢复 |
| audit_sampling | 抽窗非零 π、加权点估计、普通分歧 | 负窗重采、全窗口漏机会、完整盲标、原始缺标签计入不足 |
| acceptance | OC/Wilson、致命停批、小批需全审、attempt=3 拒绝 | 固定抽样身份、复核人/签字、新样本重验、全审后闭合出口 |
| replay/property | 同输入 ID/hash 列重复一致 | 输入乱序/晚到/同值编辑/字段改动/跨湖、旧文件字节不可变 |
| revocation | 只有整图 CONSENT_REVOKED 标记 | source 传递撤权、备份复活保护、处置历史、缺回执 fail-closed |

## 7. 应改

- **A01**：G0 修看板 verify：D-03 用本轮唯一临时输出并断言新增/输入 hash；D-05 改为 `../eval/v3_trader_signal_bench`，不要让测试永久要求 warning 非空；D-07 绑定本轮构建 manifest；D-09 两方案分别数值断言并 pipefail。D-01 虽非本次执行范围，其坏 grep 仍在看板。
- **A02**：修 bench 指标名称和严格度。extract.py:725 的 skip_precision 实际分母是真 skip，属于 skip recall；tps_ok 只要求预测的子集命中，漏 TP 也可能通过 strict。全窗口 recall 需人工机会框和 π，不能用该 bench 替代。
- **A03**：禁止未校准 near 组默认参与研究 cluster/fold；冻结阈值前输出候选审计信息即可。当前 group id 会受全批复制关系变化影响，纳入未来扰动测试。
- **A04**：明确远端区间数量级检查规则。当前 `[100,10000]` 对 mark=100 得 delta_hi=4.605、scale_gate=ok，因为仅最近端触门；ADR 用“最近端作告警指标”有歧义，先由 G0 区分合理性指标与逐数字数量级完整性。
- **A05**：增强输入边界：非法 edited_unixtime 目前解析错误被丢弃，可落成 H0；过大 unix/未知时区、负数/NaN/Infinity/空区间应隔离而非异常中断或归正常。
- **A06**：语义 provenance 不要捏造 trader_id=channel_id、V→watched_cohort；分别保存作者证据与采样框/观察连续性证据。原文和规范化文本分列以保留可验证 span 坐标。
- **A07**：测试中 `assert price is not None and ... or ...` 加括号/拆断言，避免后半分支使缺价假绿；删除只计调用数的伪穷举表达，具体期望见 S15。

## 8. 可选

- **O01**：dedup 近似比较 O(n²)，将来扩量前用索引预筛并记录候选召回率；当前小合成集不因此阻断。
- **O02**：清理 unused import、DECISION_MASKED、ex_extract 等未使用变量；拆开密集一行字典和条件赋值，降低规则漏字段概率。
- **O03**：给 review/verify 产物附统一机器可读摘要，记录实际 bench 路径、输入摘要、图版本及测试退出码，降低 notes 与实际产物不一致的风险。

## 9. 凭据、幸存偏差与终裁范围

未发现 data 模块 import services/* 或直接读取 /srv、生产 session、生产数据库配置；实际 I/O 为指定导出、录制 fixture、研究湖及已有 bench/results。NoNetworkClient 默认 Abstention，设 allow 环境变量后也只是 NotImplementedError；仓库无真实 provider，默认路径确实 gated。自定义 client 的调用入口本身无统一闸门，未来接入真实 provider 时须将审批判定放在统一入口。
凭据边界仍有不可信媒体路径逃逸（S16），所以“无 services import”不能作为完整安全证明。
bronze 和 quarantine 实物保留、纯图拒答保留、无 G1 净 R 填零是已成立的部分；同文独立机会折叠、删帖无传播、权重假值、按未来 outcome 暴露/筛选和负窗口 bootstrap 缺失仍会损害机会分母与偏差控制。
合成 fixture 与录制响应没有运行期读取金标给 parser 的路径；但它们与 parser 模板共同设计，合成行情按信号价配套，不能证明真实质量，图片也没有可读计划内容。没有证据可把这 30 条 bench 或 8 条合成录制当独立验收集。
终裁 **fail**，原因是存在已复现的正确性和隔离漏洞，而非仅缺真实数据证据。真实 PoC/真实 LLM/真实批次放行保持 gated；若仅评价这些真实能力，应标 insufficient/not_run，不与本次合成链路 fail 混淆。
关闭条件：S01–S16 逐项补实现/回归及必要 G0 接缝裁定，再独立复审；不得以自报 done、旧湖行数或再次单独运行 100 个旧测试替代闭合证据。

## 10. 收尾并发变更补充（优先于正文的旧实现描述）

报告写入后的源/测试树摘要复核不相等。观察到 api.py、extract.py、lifecycle.py、test_extract.py、test_lifecycle.py 更新，codes.py 删除，test_smoke.py 移除其 import/断言。这些不是本审查修改；本会话唯一仓库写入仍为本报告。
因此 §1 摘要和 §2 各任务退出码属于首轮实际审查/执行时的版本，不得用作新工作树的不可变认证。本文保留已复现旧证据，并逐项补记最新状态，不抹掉并发改变的事实。
补跑同一全套命令，环境同 §2：`101 passed in 1.38s`，退出码 0。此次没有再次声称所有独立任务 verify 已在新版本重跑。
已阅读新增未来事件测试和单位测试：未来测试只扰动同时间级平仓，比较时主动 drop decision_snapshot_hash 等列；单位测试只覆盖一种文本布局。最新代码仍有 S01 的 time_grade_min 全生命周期依赖与 S06 的同句单位泄漏。
最新只读复现输出：
```text
换行版成交量：entry=6, stop=5，notes=[]（该子例已修）
同句分号版成交量：entry=60000, stop=50000, tps=[70000,65000]（仍失败）
import quant_lab.data.codes → No module named 'quant_lab.data.codes'
```
收尾读取的文件摘要（不是持续变动目录的最终冻结承诺）：
```text
api.py       4b8472de46e6ca8af07cbedd87f8f280ac4de8e4953c120babbac25096d1074d
extract.py   1e54a9138dc27c9323c3e9f4ff8bf2c5b4fa9909645b6cdd96c3550b1326e130
lifecycle.py cea66306abfa8a9ddd432d9f0cf2f14c5be47b3a3c6067dde47c97b731bb1db1
```
以上局部修补未关闭任何完整 S 项，编号和待闭合条数仍为 16，终裁保持 fail。复审应先固定交付快照，避免边写盘边拿旧 verify 证明新版本。
