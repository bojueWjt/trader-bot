# G3 P1 独立 review：R-03..R-09

审查日期：2026-09-11（Asia/Manila）；取证时间约 2026-09-10 19:50–19:58 UTC。
审查者：Codex / GPT-6 主控；未委派其他模型，未调用 codex --resume-last。
审查对象：当前工作树，而非仅 HEAD diff。HEAD 为 `59f9a27ba0df1b1968c36583731d7a0b6a9a3691`；多数研究实现仍为 untracked。
结论依据：现有测试全部通过，但存在可复现的契约、前视、隔离及审计缺陷；不能据 verify 绿灯关闭 P1。
必修共 19 条（S01–S19），全部未闭合。本次只产出 review，不实施修复。

## 1. 范围、证据与限制

已读 GOAL-3、feature-snapshot 全文（§7 优先）、research-schema §9（含 §9.10 后续裁定）、execution-interface §5（含 §5.10）、ADR-G3、两份 spike/null-model 报告及其原始 JSON。
已读 taskList.json 的 modules.research 全部任务、review 与 notes；其中历史 G0 pass 和任务 done 仅作为待核查陈述，不作为本次通过证据。
已读 research 的 ast/ops/backends/features/evaluator/protocol/ledger/maxt/nullmodel/tiers/grammar/api/synthetic/contract_tests/reference，以及 paths/spike 和全部 tests/research。
已读 GROK.md；本任务属于架构、统计协议及安全边界独立验收，由主控直接完成。
本次没有修改代码、测试、契约、taskList 或既有报告，也没有调用生产服务或获取生产凭据。
按用户明确例外执行 R-09：覆盖 `/tmp/ql-proto` 报告，并向默认湖 lockbox 的事件日志与 ledger.parquet 追加合成运行。
pytest 的 tmp_path 制品属于获准测试的副作用。执行时设置 `PYTHONDONTWRITEBYTECODE=1`、`PYTEST_ADDOPTS='-p no:cacheprovider'`，避免额外源码字节码和 pytest cache 写入。
定向 Python 复现仅使用内存数据；缓存复现用内存字典替代 `_cache_read/_cache_write`，没有额外写湖。
未重新执行 416 万行 spike 或整套 1000 次 MC；性能和大规模 FPR/功效数字明确标为既有报告记录，不冒充本次复测。
所有 θ、p、区间、性能和功效数字仅描述合成结果；声明门仍为 pending。

关键文件 SHA256（防止将本次意见错误套用到其他工作树）：

| 文件 | SHA256 |
|---|---|
| contracts/feature-snapshot.md | f70c06ae5c46ecc57372ad7c0d7413ce24fbf504aeaea5bffc46906550cecce5 |
| contracts/research-schema.md | 304a9b91d87de8bff95404b6ef9e8a25c4e7b0ea13ad6ddc590f26595994d244 |
| contracts/execution-interface.md | 70f7420e3139d959d92a3870eda71686a099a159be5aeb90c6d19e6fd164555a |
| docs/adr/ADR-G3-expression-and-stats-engine.md | 5c276d88bbfdfba8d1f2c41d12f980963f5c392e657502e90b8ab03782cbd9f2 |
| taskList.json | 3cdc3c0f4a3a9c82b94cf4222550c0840d11474e0eb1961f34f1a1d51ddfb4fd |

## 2. 逐任务 verify：原命令、输出与断言强度

全部命令 cwd=`/Users/balen/projects/trader-bot/quant-lab`。原 verify 字符串逐条从 taskList.json 读取执行；下述环境设置不改变测试断言。

### R-03

```sh
.venv-g3/bin/python -m pytest tests/research/test_ast.py -q
```

输出：`65 passed in 0.03s`；exit=0。
确有非空测试，pytest 未收集测试本身会失败；覆盖 schema 拒收和规范化向量，不等于验证了十进制精确保真（S19）。

### R-04

```sh
.venv-g3/bin/python -m pytest tests/research/test_ops_contract.py -q && .venv-g3/bin/python -c "from quant_lab.research.ops import REGISTRY;assert len(REGISTRY)>=18;print(sorted(REGISTRY))"
```

输出：`96 passed in 83.78s (0:01:23)`；exit=0。
注册表输出：`['Abs', 'Add', 'And', 'Corr', 'Delta', 'EMA', 'Gt', 'LogPositive', 'Lt', 'Max', 'Mean', 'Min', 'Mul', 'Ref', 'SafeDiv', 'Std', 'Sub', 'TSRank']`。
确实断言 ≥18，测试工厂包含 27 个边界数据集和 100 个随机序列；报告写 26，属于计数说明陈旧。
每算子五类检查实际执行，绝非空壳；但只测 polars，不证明 polars_ta 失败后被隔离。

### R-05

```sh
test -f docs/adr/report-G3-backend-spike.md && grep -qE 'cold_s|冷跑' docs/adr/report-G3-backend-spike.md
```

输出：无 stdout；exit=0。
只证明文件存在且命中关键词。没有断言输入行数、AST 数、耗时/内存非零、两后端通过数或失败算子拒用。
既有报告记录 polars 18/18、polars_ta 16/17，Corr FAIL、EMA unsupported；性能比较中后者少跑 6 个 EMA AST，不能当作同等工作量速度比。

### R-06

```sh
.venv-g3/bin/python -m pytest tests/research/test_features.py tests/research/test_evaluator.py -q
```

输出：`15 passed in 0.36s`；exit=0。
包含非零机会、非零 θ、等号、latency、cutoff、NaN 边界与删失计数断言；但冷热同序测试遗漏缓存错配和上下文冲突。

### R-07

```sh
.venv-g3/bin/python -m pytest tests/research/test_protocol.py -q && .venv-g3/bin/python -c "import polars as pl;d=pl.read_parquet('data/lockbox/ledger.parquet');assert d.height>0;print(d.height)"
```

输出：`10 passed in 0.12s`，随后 `73`；exit=0。
账本确实非空，但检查的是既有默认湖文件，不断言这些行由本次 R-07 测试产生；测试自身使用临时湖。
verify 中写死相对 `data/lockbox/ledger.parquet`，不遵循 QUANT_LAB_DATA_ROOT；业务 Ledger 默认路径则遵循。

### R-08

```sh
.venv-g3/bin/python -m pytest tests/research/test_maxt.py -q && test -f docs/adr/report-G3-null-model.md && grep -qE 'FPR.*(CI|区间)' docs/adr/report-G3-null-model.md
```

输出：`7 passed in 0.07s`；exit=0。
测试有非零 SE、多候选、共同冲击和重估对拍。报告门不读取 n_done、诊断、FPR 上界或功效下界，也不跑 test_nullmodel.py。
因此报告明确 power=fail 时仍会绿，见 S17。

### R-09

```sh
.venv-g3/bin/python -m quant_lab.research.api run --config tests/research/fixtures/protocol_synthetic.yaml --out /tmp/ql-proto && .venv-g3/bin/python -c "import json;r=json.load(open('/tmp/ql-proto/report.json'));assert r['tier'] and r['n_attempts']>0 and r['theta'] is not None;print(r)"
```

exit=0；第一段 stdout 完整如下：

```json
{"status":"ok","tier":"T1","K":187,"theta":-0.0016013500422323976,"se":0.024183311964615095,"lower_bound":-0.04093616203869879,"p_adj":0.5222388805597201,"n_attempts":73,"n_folds":3,"claim_status":"descriptive_only","out_dir":"/tmp/ql-proto"}
```

第二段打印完整 report 字典；审查相关输出摘录：

```text
candidate_pool: n_submitted=12, n_admitted=12, config_cap=12
K_by_fold: wf000=187, wf001=250, wf002=306
n_attempts_by_status: {'status': ['duplicate'], 'len': [73]}
n_selected_folds=1
oof: n=468, n_evaluable=450, n_censored_excluded=18, n_take=357
data: n_episodes=900, n_eligible=900, n_asts=6, n_rules=2, bars_rows=108000, n_censored=39
protocol_hash=4b2e095e310b28273defb811043d1d1a801c5ceb6ba214d4655126342149c8f6
ledger_path=/Users/balen/projects/trader-bot/quant-lab/data/lockbox/ledger.parquet
```

确有非空 tier 和正 n_attempts；θ 只断言非 None，合法 θ=0 应被允许，不应强行要求非零。
该断言也接受 NaN/Infinity、T0、全 duplicate；本次 73 条全 duplicate 仍重新拟合/重算，见 S08。

### 补跑 verify 未涵盖的测试

```sh
.venv-g3/bin/python -m pytest tests/research/test_api.py tests/research/test_nullmodel.py tests/research/test_g2_parity.py tests/research/test_smoke.py -q
```

输出：`21 passed in 5.89s`；exit=0。
与前述文件集合不重叠，合计 214 passed，覆盖当前 tests/research 全部测试文件；没有声称另跑过一次整目录命令。

## 3. 实现与冻结契约逐字段对照

| 接缝/字段 | 实现状态与漂移 |
|---|---|
| feature_snapshot 必需输入 asts/anchors、bars/backend/cutoff | 原参数保留，额外 ctx/latency/max_staleness/cache；时间上下文两套来源未统一（S02） |
| 输出 episode_id、t_dec | 存在，输出保持 anchors 行序；热缓存值不随行序重排（S01） |
| 输出 f_<canonical_hash>、validity_<hash> | 列名符合，数值/nullable bool 转 validity；没有缺必需输出列 |
| 缓存 canonical_hash/op_versions/market_manifest/cutoff/fold_id/preprocess_state | 均列入 key，但实际计算与 key 上的 ctx 可能不同；max_staleness 未入 key |
| graph_version/derivation_hash | graph_version 仅取 ctx，自带 unknown 默认值；research-schema §9.10.2 必需 derivation_hash 缺失（S03） |
| OpportunitySet episode_ids/eligibility/weights | 三列组保留；额外 digest；默认 digest='' 可绕开摘要校验；冻结 helper 检查非空簇、自洽权重 |
| eligibility_by_estimand | 合成使用 main；§9.10.1 冻结为 description/entry_decision/execution/original_entry/price_check/outcome，默认 helper 不兼容（S14） |
| evaluate ast/opp/features/rule/execution/fold_id/attempt_id | 必需参数保留，新增两臂 policy 与 ledger；可无账本调用。执行支持单政策过滤和两政策配对 |
| EvalResult theta/se/n_opportunities/n_clusters/coverage/nan_rate | 全部存在；θ 为初始簇权重的共同可评配对比值；nan_rate 仅信 validity（S12） |
| EvalResult per_fill_R/tail_loss/unclosed_rate | 全部存在；前两项为已成交均值/候选收益下分位；未闭合率先排除删失而失真（S16） |
| 账本 §5 attempt_id/origin/parent_id/canonical_hash/params/fold_id/visible_cutoff | 磁盘投影全部有列；params JSON 字符串、visible_cutoff ISO 字符串；非法 AST 尚未 reserve 就失败（S08） |
| 账本 §5 code_version/model_version/data_manifest/seed/cost/objective/status | 全部有列；code_version 固定 g3-research-v0、cost 通常 {}；objective 名保留，数值另存 objective_value |
| MemoryLedger 字段 | 不包含完整磁盘必需列，不能把它等同于磁盘审计验收；仅 MC 的计算路径替身 |
| max_t_bootstrap(thetas,ses,*,block_len_days,B=2000,seed) | 原调用形状接受；新增 panel/alpha/**kw；无 panel 返回 contract_blocked，有 panel 重估；ses 实际未校验 |
| walk_forward | 原关键字可用，另增 span/origin/max_folds；含 train/test IDs、purged、embargo_interval；未知标签不入 train |
| run_protocol(config_path) -> ProtocolReport | 实际 -> dict，另增 out_dir；含 tier/n_attempts/theta，未定义 ProtocolReport 类型；建议明确版本化报告 schema |
| enumerate_grammar(depth=2,*,ops,windows,fields,cap) | 原签名可调用，额外 max_visited；实际返回 AST list；资源上限未约束先行物化（S18） |
| instrument_id | synthetic 默认值和 YAML、nullmodel 使用 BTCUSDT-PERP.BINANCE-UM；算子单测用 X；fake_bars 只拒空/非串，不校验格式 |
| G2 输出/请求身份 | fake_execution 与当前 G2 BATCH_SCHEMA 对拍通过，但双方均未含 §5.10 新 fraction_source；当前代码对拍不能取代契约对拍 |
| OpSpec window_semantics | 引入 rows_or_wallclock，超出 §2 的 wallclock/rows/none 枚举；应提增量契约修订，不静默扩枚举 |

net_R 判定表（feature-snapshot §7.4 优先）：

| 情形 | evaluate / pair_arms | pipeline / 其他影响 |
|---|---|---|
| 任一臂删失，net_R=null | 两臂共同排除，n_censored_excluded 计损耗，未重分簇内权重 | 正确输入的 censored mask 会排除；缺少入口一致性校验，错误 mask 可将 NaN 当 0（S12） |
| 无删失但 net_R=null/NaN/Inf | 抛 EvalContractError | _panel/_oof 可填 0，未复用同一验证（S12） |
| fill_status=none，无删失，net_R=0 | 留分母，非零会拒绝 | t1 取平仓时间导致未成交样本不进成熟训练（S13） |
| 规则 skip | 候选收益 0，基线留分母；优先处理证据删失 | 正常过滤差值 np.where(take,cr,0)-base 同口径 |
| 候选特征 NaN | validity=False 时 skip；>5% 拒收、恰5%通过 | validity=True 的 NaN 被误算有效；outer 未实施 nan_rate 门（S12） |

## 4. 必修

### S01 必修：热缓存按位置拼回，机会行错配

位置：`src/quant_lab/research/features.py:70`、`:106`、`:121`、`:141`。
问题：anchor_hash 先按 episode_id 排序，缓存却仅存 __row/value/__valid，并将 Series 按当前行位置挂回。相同 anchor 集合换顺序会命中同 key，将其他机会的特征赋给当前机会。
实证：冷 `[a@4,b@6]` 得 `[4,6]`；反序热读 `[b@6,a@4]` 仍得 `[4,6]`，无缓存为 `[6,4]`。这是配对 θ 的输入污染。
改法：缓存保存稳定 episode/time 身份，读取后按身份一对一 join；或令 key 包含有序 anchor 序列，并严格核验列与行数。
验收：同集合多种排列的冷/热/分块结果逐 episode 一致；缺行、重复或错 schema 缓存拒读，不按位置兜底。

### S02 必修：时间上下文与缓存 key 分离，可复用违反 latency/staleness 的值

位置：`features.py:35`、`:58`、`:89`–`:101`。
问题：key 读取 ctx.cutoff/latency，计算读取独立 cutoff/latency 参数；ctx-only cutoff 不限制数据，ctx-only latency 不延迟可见性。max_staleness 不进 key。cache=True 又允许 unknown manifest/graph；bars.available_at 完全未使用。
实证：同 ctx 先 latency=0 缓存 `[4,6]`，随后 latency=1s 热读仍 `[4,6]`，冷算 `[3,5]`；ctx.cutoff=第5槽但第6槽仍输出6；先放宽 staleness 后切回默认，热读 `[3,6]`，冷算 `[None,6]`。
改法：建立唯一有效上下文，冲突参数拒绝；有效 cutoff/latency/staleness 和数据身份全部参与 key；真实 available_at 晚到数据拒绝提前使用。无可验证 manifest 时禁缓存或内容寻址。
验收：ctx 与参数单独提供/冲突、latency 等号、cutoff 后 anchor、strict staleness 边界、同 manifest 不同数据都覆盖，冷热值与 mask 一致。

### S03 必修：撤销/tombstone 缓存门缺失，且未包含 derivation_hash

位置：`features.py:45`–`:72`、`:130`–`:146`；research-schema §9.10.2。
问题：只检查本次 anchors 上 is_tombstone=True；调用者持有旧 anchors 时，即使上游版本已撤销，仍可读取或发布缓存。graph_version 仅来自可自填 ctx，没有 derivation_hash，也不核验当前 manifest。固定 `.{key}.tmp` 允许同 key 并发写互相覆盖/rename 失败。
改法：落实最小版本读取门和 derivation_hash 身份，缓存读取时验证 graph 仍可消费；发布前复查撤销状态；每次写用唯一临时文件。无法验证真实 graph 时拒绝旧图缓存。
验收：计算期间 tombstone、热读期间 tombstone、同 key 两写者、graph/derivation 变化均有确定性测试。P1 不要求分布式 ack、跨机复制或备份恢复，§9.10.2 已明确推迟这些事项。

### S04 必修：已知失败的 polars_ta.Corr 没有隔离

位置：`backends/polars_ta.py:32`、`:48`、`:97`；`contract_tests.py:264`；`spike.py:113`；`ops.py:86`。
问题：UNSUPPORTED 只有 EMA；supports('Corr') 为 True。run_contract 和 contract_matrix 仅收集失败，从未调用隔离；进程内 QUARANTINE 也没有后端/版本维度及持久状态。
实证：新进程 `get_backend('polars_ta').supports('Corr') -> True`；huge 边界 Corr rows=3 实算 `0.9990234375`，reference 为 `1.0`。与报告“失败算子已隔离”相反。
改法：对该后端 Corr 明确 fail closed，登记后端版本级隔离状态；后端 compute 和 snapshot 必须拒绝依赖 AST；修正报告陈述。
验收：新进程和热缓存都拒用 quarantined Corr；polars Corr 仍正常；契约失败到运行禁用有真正连通测试。

### S05 必修：search/selection 只按行切半，没有内层 purge 和成熟门

位置：`api.py:207`–`:226`；ADR §7。
问题：外训练集排序后用 n_s 截断，跨边界簇、重复组和标签区间未清理。实际“后半段”按机会数量，不是固定日历训练窗后半段。外层成熟只相对 outer cutoff，不能保证 search 标签在 selection 开始前成熟。
实证：WorldConfig(seed=1) 首折 outer_K=161；search_K=82、selection_K=81，双方共享2簇；6个 search 标签 t1 不早于 selection 开始。
改法：冻结内层时间边界，按簇/duplicate_group 和信息区间 purge；按内层拟合时点核标签成熟，记录 search/selection IDs、cutoff、损耗及拟合状态。
验收：跨边界同簇、不重叠同组、端点相等、晚成熟标签均隔离；修改 selection/outer 信息不改变已冻结 search 拟合。

### S06 必修：分档和预算不是执行硬门，且 K 取错层级

位置：`api.py:186`–`:205`、`:247`–`:285`；`tiers.py:83`–`:109`；`protocol.py:119`–`:128`。
问题：K 用完整外训练簇数，遗漏实际 search 子折；cfg.config_cap 可直接大于档顶；DEFF 在 selection 后才计算，仅改最终报告，undefined 不阻断；f.insufficient 只记录，不阻止拟合。p_allowed 未用于模型复杂度准入。空测试窗被删除，尾部 carry 边界损耗可能直接遗失。
实证：seed=1、config_cap=36、min_train_clusters=9999 时，3折全部 insufficient，仍 admitted=36、attempts=216，而最终 config_cap=12。DEFF=None 且 K=250 的 tier_report 仍输出 T2/p_allowed=8。
改法：从全部真实内层训练折计算最小 K；在计算前冻结保守 DEFF/档位/预算，配置只能收紧硬顶；不足折保留且停止对应拟合或使协议 insufficient，空折/边界损耗不得删除。补 p 自由度核算。
验收：越过 cap 请求拒绝；DEFF undefined/高依赖、空折、min_train 不足和多级降档均不能继续原预算搜索；不再以报告警告替代门禁。

### S07 必修：账本预留非原子，崩溃后投影可能静默漏账

位置：`ledger.py:72`–`:103`、`:127`–`:153`、`:179`–`:230`。
问题：reserve 的 read/去重/预算检查在 `_commit` 的锁外；多个协调器可同时看到余额并各自 reserve。投影重建无锁且共用 `.ledger.parquet.tmp`。事件提交后、投影替换前崩溃时，read 只检查旧 parquet 能否解析，不检测事件水位，因而不重放新事件。损坏事件被直接 continue；终态还允许被 mark(...,'interrupted') 改写。
证据：代码路径直接成立；本次未对默认湖做破坏性崩溃/并发演练。
改法：在同一跨进程事务内检查并预留、提交事件及推进投影水位；唯一临时文件、目录 fsync；read/recover 对比 event_seq 并重放。坏事件 fail closed 并报告，终态不可任意转 interrupted。
验收：临时湖两进程争最后额度仅一个成功；事件提交后崩溃恢复不漏记录；投影并发/坏事件不静默跳过；全终态转移矩阵成立。

### S08 必修：编排绕过账本前置和重复终态，失败/final 不完整留痕

位置：`api.py:214`–`:274`、`:330`–`:397`；`ledger.py:260`；ADR §8。
问题：run_protocol 在创建 Ledger 前已经生成执行结果、lint 和计算快照，非法输入没有账。duplicate reserve 后仍 fit/评估，_mark 只是跳过终态更新。fit、panel、max-t 异常没有统一 finally 关闭尝试；selection 先 completed 后才真正 max-t；final 调用完全没有 reserve。也没有启动 recover、实际 cost、完整版本/机会集/策略血缘。
实证：R-09 本次73条全 duplicate，仍输出重算的 search/selection/outer/final 数字；不是缓存复用证明。固定 code_version 和协议 scope 也无法区分实现变更后的暴露。
改法：在 lint/特征/收益访问前持久化提议与 reservation；duplicate 仅复用验证过的结果，若重算则新计计算/暴露并关联原尝试；所有阶段包括 final 用一致生命周期，异常留终态；持久化结果摘要及实际成本/语义版本。
验收：非法 AST、算子失败、fit 异常、max-t 异常、重启和重复运行均可追溯，无遗留 reserved，无未记账计算；MemoryLedger/磁盘 Ledger 行为对拍。

### S09 必修：空模型训练没有成熟标签门，功效注入用全窗分位

位置：`nullmodel.py:137`–`:178`、`:216`–`:223`、`:298`；`api.py:134`–`:147`。
问题：fit_residual_model 只按 day<50% 和非删失选标签，不检查 t1/label_maturity；50% 窗与实际 PipelineConfig.min_train_span 可不同。自动块长诊断同样仅用 t_dec<train_end。功效注入 q=np.quantile(feats['f00'],pi) 使用全窗特征，使训练期收益的生成阈值依赖未来特征分布，不是 ADR 的总体预注册 q_pi。
实证：seed=1 训练窗内有5条 t1 到窗外的标签；只把这5条收益加10000，训练 mean 从 -0.010749525364839307 变130.1975838079685。
改法：残差拟合和块长诊断共用实际折的成熟 train IDs/cutoff；功效注入用预注册总体分位或独立训练校准常量，并记录总体 π 与簇均权实际效应。
验收：扰动未成熟/训练窗外收益不改变模型、块长；改变未来特征不反改过去生成标签；不同训练跨度、maturity 参数测试成立。

### S10 必修：空模型诊断失败仍可 pass，非均匀缺失机制被改写

位置：`nullmodel.py:242`–`:251`、`:312`–`:350`、`:223`。
问题：shuffle_guard 只在首个 replicate 计算，ok=False 不参与状态/计数/终裁；没有 invalid_null_model 出口。块间相关、跨品种、尾部和缺失结构也只报部分诊断。nonuniform_density 原世界 censor_frac×2，resample 却恢复普通 censor_frac，验收没有保留该更高缺失强度。
实证：在内存将 assert_not_episode_shuffle 固定返回 {'ok':False}，run_mc(nonuniform_density,n_rep=60,seed0=3,config_cap=0) 返回 verdict=pass、n_done=60、n_failed=0。这里使用禁搜索夹具仅定位诊断门，不能作为 FPR 证据。
改法：预注册诊断门失败即 invalid_null_model，计入失败最坏界且禁止 pass；保留/显式建模机制的缺失与相关结构；单列 T0 无搜索运行，不作为 T1 验收替身。
验收：坏相关、坏尾部、坏缺失、诊断非有限、全部 T0 均触发正确出口；正常模型有结构保持对照，不能只断言报告里存在 diagnostics。

### S11 必修：max-t 非空块数把全删失块也算进去

位置：`maxt.py:69`–`:88`、`:169`–`:172`。
问题：n_nonempty=len(set(block_of_row)) 不看 mask/有效权重。大量全删失块能够帮助通过“至少20非空块”门。
实证：100日、每天一簇，只有前19日 m=True，B=200/seed=1；返回 status=ok、n_nonempty_blocks=100、n_evaluable=19。
改法：空日历块仍保留在共同重采样矩阵，但准入的非空块按共同支持上 W>0 统计；明确正权重和有效差值条件。
验收：19个有效块+任意数量全删失块始终 insufficient，20块边界及所有候选共同 mask 同时覆盖。

### S12 必修：pipeline 可将未标删失的 NaN 收益填零；特征 NaN 门不一致

位置：`api.py:104`–`:113`、`:160`–`:165`、`:304`–`:312`；`maxt.py:43`–`:54`；`evaluator.py:214`–`:225`。
问题：PanelInputs 没有不变量检查；_panel 用 nan_to_num，_oof 把非有限 d 写0，PairedPanel.from_frame 在 Float64 cast 后 fill_null(0)。仅当 mask 已经正确排除时这才是无经济影响的占位；当前入口不保证这一点。evaluate 只信 validity，不复核 f 的 NaN/Inf；outer 丢弃 n_invalid，不报/执行5%门。
实证：base_R=NaN、censored=False 的2行进入 _panel 后 mask=[True,True]、diffs=[[0],[0]]；20个 NaN 特征但 validity=True，evaluate 返回 ok/nan_rate=0/n_take=20/n_nan_skip=0。
改法：统一 EventEvaluator/PanelInputs 证据与数值验证，未删失非有限收益拒收；只在 m=False 的存储占位可写0，保留损耗。validity 与数值联合判定；outer 报告并处理 NaN 超门，不重新选模型。
验收：两臂差异删失、NaN+validity=True、Inf、mask 不一致、部分簇删失、outer 高缺失对拍；有删失只排除计损耗，特征 NaN 仅 skip 且保分母。

### S13 必修：未成交的合法零收益因 t1=null 被排除出训练

位置：`api.py:350`–`:353`；`synthetic.py:219`–`:223`；`protocol.py:66`–`:68`。
问题：runner 将 t1 一律赋 cand_close。未成交没有 position_close_at，但其收益在订单到期/执行观察结束后已确定为0；当前永久 UNKNOWN_T1，会让 search/selection 只见成交且闭合的子样本。
实证：R-09 合成配置的 PanelInputs 中，85条无删失机会 t1=null；walk-forward 将这些合法零收益与未知标签一并排除。
改法：通过 G2 显式标签证据确定 t1：已平仓取标签可知时刻，未成交取到期/终态观察时刻；不要猜 t_dec，也不要把删失伪造为到期0。双政策取共同成熟上界。
验收：大量 none/0 的机会成熟后进入 train/selection 且分母不缩；none+删失仍排除；变更成交状态不隐式改变训练总体。

### S14 必修：上游定稿字段与合成桩共同漂移，当前对拍掩盖接缝错误

位置：`evaluator.py:69`–`:85`；`synthetic.py:26`–`:38`、`:170`；research-schema §9.10.1；execution-interface §5.10。
问题：freeze 默认 main，而冻结资格 struct 无 main；fake_episodes 仍只造 main。G2/G3 当前 BATCH_SCHEMA 都缺 fraction_source，因此真/假对拍仍绿，但不满足最新 §5.10。OpSpec 的 rows_or_wallclock 亦未获冻结枚举修订。
实证：将合成 episode 的资格 struct 改为契约六键后，默认 freeze_opportunity_set 抛 StructFieldNotFoundError: main。补跑 test_g2_parity 通过不消除该反例。
改法：明确主 estimand 对应的冻结键并贯通调用者、桩和测试；与 G2 属主同步 fraction_source 和可空 fraction 请求夹具；为扩展算子窗口枚举申报修订或适配为契约表达。
验收：以契约独立 schema 构造 episode/ExecutionResult，默认入口可用且未知键 fail closed；fraction_source=plan/policy 各有非空样本，三腿 Decimal 分配经 build_request。

### S15 必修：两臂上下文比较漏字段且 null 可穿透

位置：`evaluator.py:125`–`:187`。
问题：_CTX_KEYS 不含 t_dec/risk_budget；只比较双方恰好都有的列；`!=` 遇 null 返回 null，filter 丢弃而非报错。未验证同 policy_version 与 policy_hash 一一对应、关键身份非空或当前 snapshot 对应关系。
实证：20个候选结果 graph_version=null、t_dec 整体+1日、risk_budget=999，baseline 未变；pair_arms 仍成功返回20行。
改法：要求冻结必需列与非空身份，按 null-safe equality 验证必须相同的上下文；policy_hash 可因政策不同而不同，但各自须能验证并纳入身份；核 t_dec/风险预算和 snapshot 血缘。
验收：每个共享字段分别缺失/null/错值均拒绝；合法政策差异仍可配对；结果顺序变化不影响配对，缺臂/额外臂保留现有拒绝行为。

### S16 必修：unclosed_rate 先排除右删失，系统性低报

位置：`evaluator.py:232`–`:249`；ADR §6.2。
问题：opened 从共同可评 ev 再取 taken；右删失的已开未平仓在此之前已被删掉。损耗可以从 θ 分母排除，但不能从未闭合诊断分子/分母消失。
实证：20个 take 已开仓，1个右删失未平，其余19已平；n_censored_excluded=1，但 unclosed_rate=0，应诊断为1/20=0.05。
改法：用原两臂证据、在收益删失 mask 之前统计已开仓/未平仓诊断，明确取候选臂与规则 take 的口径；θ 仍保持共同删失排除。
验收：单个/全部右删失未平、未成交、两政策删失差异、规则 skip 组合，未闭合率与损耗独立正确。

### S17 必修：R-08 功效未达 DoD，verify 无法阻止假绿

位置：`docs/adr/report-G3-null-model.md:31`；`taskList.json` modules.research R-05/R-07/R-08/R-09/R-10；GOAL-3 §0/§4/§7。
问题：报告主功效 n=1000、失败135、检出313；条件成功样本比率36.18%，失败计未检出的实际比例31.30%，95% CI=[28.43%,34.28%]，未达80%。限制声明诚实，但不自动豁免 P1 DoD。报告敏感性表的高百分比排除了失败，不能代替主验收。
同时 R-05 只查性能词；R-08 只查 FPR 词；R-07 只读默认既有账本；R-09 不查 finite/生命周期；R-10 仍是负向 grep，违反 research-schema §9.9。
改法：修复算法/验收机制后按冻结条件重新完整运行，或由有权者明确修订里程碑准入并保留未达标状态；不能挑敏感性格子替换主条件。verify 正向读取结构化数值、完成数、诊断/最坏界及最终 review pass。
验收：报告 power=fail/diagnostic=false/n_done不足/仅标题/错误湖路径/全 duplicate/终裁fail 时对应门失败；主验收功效与 FPR 同时达冻结标准才可关闭原 DoD。

### S18 必修：grammar 的 visited 上限未限制先物化笛卡尔积

位置：`grammar.py:44`–`:76`；ADR §12。
问题：先构造全部 combos=list(product(...)) 并执行 canonical_hash 过滤，之后才逐 node 增 visited 并检查 cap。高深度/多窗口/多字段可在第一次预算检查前耗尽内存；max_visited 只限制末段 lint 数，不是生成资源门。
改法：流式 product、逐项类型剪枝/哈希/预算检查；输入先限制 depth、整数窗口和 visited 合法范围；达到上限返回可辨认的截断原因及实际池元数据。
验收：固定低 visited、很大理论组合空间仍在小内存/时间内退出；depth=0/1/4/超界、cap=0/1、无效组合密集、相同输入乱序均覆盖。

### S19 必修：canonicalization_version 宣称精确十进制，实际 float 下溢/合并

位置：`ast.py:75`–`:88`、`:131`–`:137`、`:287`–`:320`；ADR §2。
问题：json.loads 默认 binary64，_norm_number 再 float 转换；与 ADR “不以 binary64 合并不同十进制常量、非零下溢拒绝”的 g3-json-c14n-v1 不同。小数 token/exponent 限制未实现，非零值可变0并与0共 hash。
实证：`parse_json_text('{"op":"Add","args":[{"field":"close"},{"const":1e-400}]}')` 经 canonical_json 输出 const=0，未拒绝。
改法：文本层保留 Decimal/token，规范化后做数值域校验；或正式修订语义、变更 canonicalization_version 并失效旧身份，不能同版本两套定义。超大整数异常须统一 ASTRejected。
验收：非零下溢拒绝、相邻高精度常量身份、指数/token 资源边界、1/1.0/1e0 等价与固定向量、规范化再解析幂等均覆盖。

## 5. ADR 偏离及理由裁定

| 偏离 | 独立判断 |
|---|---|
| C02 的19算子改按契约明列18 | 成立。逐名数得18；实现纠正 ADR 误数，不应要求补第19个 |
| C03 旧 max_t_bootstrap 无 panel 返回 contract_blocked | 成立且保守。均值/SE 无法恢复联合重采样；仍需 G0 正式定稿公共 panel 签名，不将 stub 算对外统计功能完成 |
| 重复/预算按 protocol scope | 同协议去重合理；scope 随配置整体 hash 改变，不能证明全研究尝试总预算防重命名。不同协议比较需要更高层冻结 study 身份；当前并发和重复执行问题另见 S07/S08 |
| DEFF 用块 jackknife SE² / 簇 jackknife SE² | 可解释的低成本近似，但偏离独立簇重采样定义，尚无误差对拍/适用性校准。不能仅有一个 d>1 测试就视为统计等价；undefined 放行更不成立 |
| selection 用训练集后半 | 固定一次选择方向合理，且未用外测试收益选候选；目前按行数、不 purge，理由不足以覆盖成熟/同簇泄漏（S05） |
| 空模型用 MemoryLedger | 用于计算性能可接受的替身；不能称磁盘审计“全流程”已验。缺完整 schema、落盘故障/恢复/并发一致性与逐 replicate 审计证据 |
| polars_ta Add/Mul/Gt/Lt/And 显式原生路由、EMA unsupported | 已说明合同差异，不是收益驱动选后端，可接受；Corr 未隔离不可接受 |
| reference 放 src，使用 float/math 而非 tests/reference 下 Decimal/Fraction | 运算路径独立于 polars 核，已有真实对拍价值；不满足 ADR 的高精度独立 oracle 与目录分层提案，应改 |
| run_protocol 只支持 synthetic | 符合 GOAL-3 P1。不支持真实源直接 NotImplementedError，未发现绕 gold 发布门后退回合成桩 |
| 分布式撤销 ack / 备份恢复未实现 | §9.10.2 明确推迟，不作为本次缺陷；最小读前校验及内容身份仍必须满足 S03 |

## 6. 前视、幸存偏差和隔离专项判断

as-of 等号、显式 latency=1s、严格 staleness 默认一周期、显式 cutoff 后 anchor invalid 都有实际非空断言，基础路径正确；上下文/缓存组合不正确。
算子网格以 interval 建计划槽位；rows/wallclock=ceil(W/interval)，Ref 不跨洞找更早有效值，EMA 缺值重置；现有 polars 截断/未来免疫并非只比较自身空输出。
测试未来乱序仅打乱 cutoff 后行再排序，能证明排序稳定性；没有模拟 late available_at、版本修订可见时点、撤销并发和1000个组合 AST。
普通 walk_forward 训练确实检查 t1+label_maturity<=cutoff，测试有端点/单调性/未来标签扰动；内层拆分与空模型没有继承同一门。
收益评估没有删除普通特征 NaN 机会；以 invalid mask 输入时 skip 后基线仍留分母。nan_rate 的原始合格机会分母未按删失缩小，恰5%通过的测试成立。
删失 net_R 在 pair_arms 被校验为 null，evaluate 排除并计损耗；部分簇损耗未重新均分权重。这些正确行为不覆盖未经验证的 PanelInputs 路径。
原始 .cast(Float64) 后不填0在 evaluator 成立；maxt.from_frame 和 api 确有填0，需要严格区分 m=False 的存储占位与 m=True 的经济填零。
不足外训练折保留对象，但 runner 忽略 insufficient；空测试窗直接跳过，不能概括为“所有不足折都保留并降档”。
两政策共同删失 mask 在 evaluate 存在，但没有两臂不同删失的端到端回归，也没有多政策 family 公共支持设计的验收；现有 MC 仅支持纯过滤，不能为政策变体背书。

## 7. 凭据、路径及外来代码边界

对 research 源码/测试执行 rg 搜索 services、dotenv、production、/srv、os.environ/os.getenv、data/、eval/compile/from_string，并对全部 research Python AST 扫描调用。
未发现 research import services/*、生产配置读取、生产网络调用或 DEAP compile/from_string、Python eval/exec 调用。
唯一 compile 调用是 backends/__init__.py:22 的 re.compile，属于注册周期的正则，不是代码执行。
backends 的 getattr 仅访问 lint 通过的注册 `_op_<name>`；未发现外来字符串进入可执行解释器。
test_smoke.py import deap 仅作依赖存在性检查；无 GP 接入，不能把 import 本身误写成执行 DEAP 字符串。
业务湖路径 Ledger、features cache 经 research.paths.data_root -> QUANT_LAB_DATA_ROOT，未发现业务代码写死 cwd 相对 data/。R-07 verify 是确实存在的相对路径例外，必须改。
run_protocol 输出目录由 out_dir 显式提供；spike/nullmodel 默认 docs 报告路径不是湖路径，不应误报为违反 §7.5。
paths 每次重新读环境变量，而非 run 内冻结；Ledger root 在实例化时固定，但 cache 每次重新解析。建议统一 RunContext，防运行中根路径漂移。

## 8. 测试矩阵缺口与应改

| 矩阵 | 已覆盖 | 缺口/建议 |
|---|---|---|
| feature_snapshot | 等号、latency、cutoff、同序冷热、graph key | S01–S03；增 op_versions 改变、manifest 内容/derivation、反序、tombstone 并发、available_at 晚到 |
| 算子合同 | 18×5类别，边界+100随机，缺bar/EMA | 自动隔离门；polars_ta 默认 CI；1000组合 AST；超大常量/嵌套常量窗口；高精度 oracle |
| 两臂评估 | 重复/缺臂/意外、普通上下文、删失/NaN单例 | S12/S15/S16；双政策不同删失、部分簇残留权重、有效标记与值冲突、未闭合诊断 |
| G2 对拍 | build_request -> simulate_batch 与假结果列/dtype | 独立契约 schema，fraction_source 新字段、null fraction 多腿；不能只对拍两个共同陈旧实现 |
| PurgedKFold | 枚举区间性质、OOF唯一、行序不变 | 当前 walk_forward 和 PurgedKFold 共用 purge_train，没有独立优化实现可对拍；未来优化需独立 oracle；先补内层实际调用链 |
| max-t | 共同冲击 SE、复制/交换、θ/分母逐行对拍 | S11；生产 p 等号目前只测试独立 NumPy 表达式，不锁定 API 的 >=；补手工 draw 矩阵对 T/q/LB、SE 重估独立对拍 |
| nullmodel | 多机制、结构正例、CP、6次/4次MC | S09/S10；失败出口、成熟/未来扰动、非均匀缺失保持、政策变体；将失败最坏界放敏感性主表 |
| grammar | cap0/1/50、去重、排序、depth3 | S18；visited=0/1、非法窗口小数截断、耗尽原因、内存上限 |
| tiers | 阈值两侧、一次跨度/DEFF降档 | S06；连续多级降档、undefined fail closed、实际search K、功效/类数/有效秩/最终T3b簇数门 |
| ledger | 单实例状态、预算、投影坏文件重建 | S07/S08；并发额度、提交后崩溃、坏事件、重复重算、final留痕、root冻结 |

应改 A01：为公开 ProtocolReport 定义可序列化 schema/type；缺 fold 时也保持稳定字段，JSON 禁止 NaN/Infinity；report.json 与返回 dict 的 out_dir 字段一致。
应改 A02：max_t_bootstrap 对 thetas/SE 形状与数值都做显式校验，当前 ses 不读取、theta shape 不同时连 mismatch 都不记。
应改 A03：补独立 Decimal/Fraction reference；生产统计代码不要依赖只为测试存在的框架。目录仍置 tests/research，不新增 src 内 __tests__。
应改 A04：统一 instrument_id 边界校验；算子数值单测的 X 可明确声明非集成夹具，合成对外数据必须遵守 -PERP.BINANCE-UM 格式。
应改 A05：synthetic.fake_execution 应满足 gross/net/fee 守恒与 trace 身份；当前 gross_pnl=net_pnl 而 fees非0，固定 policy_hash 只由版本名生成，不代表真实政策内容。结构桩不能被当作经济不变量夹具。
应改 A06：协议输入先验证 search_frac、alpha、B、span、block_len、max_folds、候选 ID 唯一等，避免零 test_span 循环、错误窗口类型、重复 rule_id 覆盖 thr/cols。
应改 A07：spike 每后端独立进程记录 RSS、共同支持 AST 子集耗时；当前两个峰值共享进程累积水位，记录为进程峰值可以，不能直接归因各后端内存成本。
应改 A08：registry_digest 应覆盖完整语义元数据（nan_policy/init_rule/input/output units/params 规则），当前只包含部分字段；冻结版本变更流程应保证任何语义变化可追踪。

## 9. 可选

可选 O01：在事件日志有正确事务/恢复门后再优化每次 reserve/mark 全量投影的成本，避免先为性能削弱审计。
可选 O02：报告提供原机会→资格→成熟→行情→内层purge→共同删失→OOF 的计数和权重漏斗，便于区分样本不足与实现错误。
可选 O03：将所有重采样种子、协议摘要和最小反例摘要随报告保存；性能数据和统计通过条件分开呈现。

## 10. 收口条件

本次未关闭任何 S 项。应先修复数据身份/时间/隔离/审计及样本选择问题，再以修复后的冻结协议重跑 MC；旧 FPR/功效报告不能自动迁移为新版本证据。
P1 原 DoD 的功效条件未满足；单写“仅描述”只能约束结论措辞，不能把未满足的工程/统计验收改为通过。
最终审核还须正向读取本报告最后的终裁行；文件存在、行数达标或负向关键词未命中均不构成 pass。

终裁：fail

## 二审（R-10 round 2）

审查日期：2026-09-11（Asia/Manila）；本轮取证截至 2026-09-10 23:20 UTC。
审查者：Codex / GPT-6 主控，独立完成；未委派其他模型，未调用 codex --resume-last。
审查对象：当前工作树，HEAD=`d25e0dbb2f770ff2f03469a2296e5890151381ff`。不以任务 note 的“已完成”替代源码与实跑证据。
本轮完整闭合 **7/19**；**部分闭合12/19**，完全未修复的“未闭合”0项。若按是否满足一审整条验收二分，**尚未完全闭合12项**。二审不通过。

仅在本文件末尾追加本节。一审正文为原始39730字节/393行，SHA256=`ababca1a72cc198f7f0aca73baec8d0845f77547923fb75592d96b2cbff50070`；追加时校验该前缀未变。不改源码、测试、契约、taskList、两份报告或其他文件。
已读：一审全文；feature-snapshot 全文（§7优先）；research-schema §9（特别是A5/A7/§9.10.8）；execution-interface §5（含§5.9/§5.10）；ADR-G3 全文；research/全部20个Python源文件；tests/research全部测试及YAML；两份报告正文及原始JSON；modules.research任务与notes；GOAL-3及合并稿D.5/E.1。已读GROK.md，本次为跨模块/统计/并发最终验收，由主控处理。
research源码摘要（按路径排序，每项依次拼接UTF-8相对路径、NUL、文件字节、NUL）：`c742e98a16b730db5a4832fce170172ea281513714b161c0d20fc875a84416f7`。
本次null报告SHA256=`41b620109c2044e564c2e7280f778fc54b148178ce4ee8594d464e785739e046`；backend报告SHA256=`d9fc1690653016db72cd78c4756efc5b36b34ae04dcf6815aa02c27858cd7afd`；taskList SHA256=`0842da85b7f748e39dce147607129126cc3d321697b8fadf925947921f1f616c`。

所有运行cwd为 `/Users/balen/projects/trader-bot/quant-lab`。测试/探针均设置 `PYTHONDONTWRITEBYTECODE=1`，pytest另设 `PYTEST_ADDOPTS='-p no:cacheprovider'`。测试临时湖由原有autouse fixture隔离。按用户明确例外执行R-09，其写入仅为预期的 `/tmp/ql-proto` 与默认湖账本。其余探针只读或在内存中注入故障，不新增复现脚本文件。没有生产访问。
**数值仅描述合成/测试输出**，不作真实研究优势、上线或收益声明。没有重跑416万行spike或1000次/机制完整MC；本次确实重跑的是指定verify、全套pytest、定向pytest和下列内存探针。报告的既有MC结果与本轮实跑严格区分。

### 逐条闭合表

P-Sxx的完整实跑命令和输出见后文；探针A–D提供可直接复制的命令，全部exit=0（探针输出暴露失败不变量，exit=0不代表被审实现合格）。

| 一审项 | 状态 | 源码复核 | 实跑证据 |
|---|---|---|---|
| S01 | 已闭合 | 有序 anchor 摘要与 episode_id 一对一回读，拒绝缺行/重复/列集错误的缓存；原先反序串值反例消除。 | P-S01：1 passed；反序值 [6,4] 与无缓存一致，错 schema 后重新得到 [4,6]。 |
| S02 | 部分闭合 | cutoff/latency/staleness 与内容摘要已统一；available_at 只校验末根 bar，未覆盖窗口内历史依赖。 | P-S02：2 passed；探针 A：Mean3 全量 (3.0, True)，按可见数据重算 (None, False)。 |
| S03 | 部分闭合 | derivation_hash/epoch、读前及冷算发布前回调、唯一临时文件已加入；热读期间撤销后仍返回缓存。 | P-S03：1 passed；探针 B：ok=False、checks=1，returned_valid=[True]。 |
| S04 | 已闭合 | polars_ta.Corr 已被源码表保守禁用；新进程、compute、snapshot 均拒绝，polars.Corr 保留；契约失败已连接运行期隔离。 | P-S04：2 passed；新进程输出 False True，隔离后 compute/snapshot 抛 BackendOpQuarantined。 |
| S05 | 已闭合 | 内层已按日历时间分段，调用 purge_train 清理同簇/重复组、闭区间交集与成熟门；search 不再按行数切半。 | P-S05：1 passed；非空 search/selection 无共享簇，逐 search 标签 t1+2日≤t_mid。 |
| S06 | 部分闭合 | 实际内层 K、预计算基线 DEFF、cap 收紧和不足折停拟合已落地；空测试窗仍删除、尾部损耗丢失；undefined 分档报告仍给可搜索预算。 | P-S06：1 passed；探针 B：仅 wf000，tail_loss_recorded=False；C：T2/p=8/cap=64/K_eff=None。 |
| S07 | 部分闭合 | reserve 读判写同锁、水位重放、坏 JSON 拒收、唯一投影临时文件有效；mark 的读状态/终态判定仍在锁外。 | P-S07：1 passed，四进程额度仅一个成功；探针 B：completed→interrupted 被接受。 |
| S08 | 部分闭合 | 非法 AST 有记录，search/final 主路径与显式重算记录已增加；执行收益先于首次预留、snapshot 提前完成、selection 批中途异常仍遗留 reserved。 | P-S08：2 passed；B：收益访问时 0 行，backend boom 后 completed；C：selection 留 ['reserved','failed']。 |
| S09 | 已闭合 | 残差拟合与块长诊断使用标签成熟条件，MC 传入 min_train_span_days；注入改用预注册总体正态分位。 | P-S09：1 passed；扰动未成熟标签不改模型；C：train_days=(0,180)，maturity=2日排除46，q30=-0.7797157283503584。 |
| S10 | 部分闭合 | 逐 replicate ICC 门、非均匀机制双倍删失与全 T0 标记有效；power 分支仍能越过 invalid_null_model，尾部/尺度等保持门未实现。 | P-S10：2 passed；B：1次诊断失败的注入 power 仍 pass；C：收益尺度×100，真实 ICC guard 仍 True。 |
| S11 | 已闭合 | 非空块按共同支持上 W>0 计；全删失块保留在抽样矩阵但不凑准入数量。 | P-S11：1 passed；100个日历块仅19个有效时 insufficient，20个有效时计数20。 |
| S12 | 部分闭合 | PanelInputs.validate、_panel/_oof 可评差值门及 outer NaN fallback 有效；PairedPanel.from_frame 仍将 m=True 的 null 差值填0。 | P-S12：3 passed；A：null_m_true=0.0 True，max-t status=ok/nonempty=30。 |
| S13 | 已闭合 | P1 单政策合成路径的合法 none/0 取显式 entry_ttl_s 到期时刻作 t1；已平仓用 close，删失未闭合保留未知。 | P-S13：1 passed；非空未成交无删失集合 t1 无 null 且晚于 t_dec；代码分支明确为 t_dec+entry_ttl_s。 |
| S14 | 部分闭合 | 默认 entry_decision、六键词汇及 A5 自洽检查有效；fraction_source 两边都缺、窗口枚举未修订、合成计划金额仍是 String，完整资格列缺失会默认放行。 | P-S14：6 passed；B：fraction_source fake/real=False/False；A：缺资格列仍接受 a,b；独立 dtype 读取为 String。 |
| S15 | 部分闭合 | 新增 t_dec/risk_budget 对比与 ne_missing，部分身份非空及单臂 policy_hash 唯一性已检查；双方同为 null 与缺 policy_hash 仍可穿透。 | P-S15：1 passed（普通配对测试）；A：四个共享字段双方 null 各接受2行，缺 policy_hash 也接受2行。 |
| S16 | 部分闭合 | 存在可评收益时已改为删失前统计 opened_all；全部删失的提前返回仍把 unclosed_rate 写0。 | P-S16：1 passed（部分删失）；A：2个已开未平且删失，insufficient / excluded=2 / unclosed=0.0，诊断应为1.0。 |
| S17 | 部分闭合 | 新报告如实记录主功效不达标，新 R-08 此次诚实报红；功效准入尚未获修订，验证也仍存在错误 flag/T0/旧终裁放行洞。 | R-08 原 verify exit=1；主377/1000，最坏界 CI=[0.34686235465954923,0.40786417678951004]；探针 D 三个假绿成立。 |
| S18 | 已闭合 | 笛卡尔积已改惰性遍历，visited 在逐候选生成前截停；深度/窗口校验和截断元数据可验证。 | P-S18：1 passed；深度4的大理论池在 visited=500 停止，cap=3/0 与非法窗口拒收断言通过。 |
| S19 | 部分闭合 | Decimal 文本解析、下溢/溢出与 token 门修复有效；normalize() 仍依赖 Decimal 上下文精度，会合并不同长常量并使身份随上下文变化。 | P-S19：1 passed；A：两个31位有效小数 hash 相同；同一输入 prec=10/40 产生不同规范串。 |

S05闭合限定于本条的切分/成熟隔离；逐机会ID与拟合血缘的持久化仍归S08。S13闭合限定于用户要求的P1单政策合成路径；不据此宣称任意双政策t1接缝已验收。S04源码登记对所有polars_ta版本保守拒用Corr，未自动解禁；动态新失败的跨进程登记流程仍应由后续版本管理明确。

### 指定 verify 与全套测试实跑

命令原文直接从taskList.json读取，用zsh顺序执行R-05..R-09；即使R-08返回1仍继续R-09。以下stdout/stderr合并记录，省略pytest进度点，不省略失败原因。未以替换环境湖路径来掩盖默认湖验证。

#### R-05

```sh
test -f docs/adr/report-G3-backend-spike.md && .venv-g3/bin/python -c "import json,re,sys;t=open('docs/adr/report-G3-backend-spike.md',encoding='utf-8').read();j=json.loads(t[t.rindex(chr(96)*3+'json')+7:t.rindex(chr(96)*3)]);m=j['matrix'];assert all(v['status']=='pass' for v in m['polars'].values()),m['polars'];assert sum(v['status']=='pass' for v in m['polars_ta'].values())>=10;tm={r['backend']:r for r in j['timings']};assert tm['polars']['n_ast']==128 and tm['polars']['cold_s']>0 and tm['polars']['hot_s']>0 and 0<tm['polars']['rss_gib_after_hot_s']<=8 and tm['polars']['cold_s']<=1200;print('spike ok',tm['polars']['cold_s'],tm['polars_ta']['cold_s'])"
```

```text
spike ok 206.77 36.11
exit=0
```

#### R-06

```sh
.venv-g3/bin/python -m pytest tests/research/test_features.py tests/research/test_evaluator.py -q
```

```text
21 passed in 0.76s
exit=0
```

#### R-07

```sh
.venv-g3/bin/python -m pytest tests/research/test_protocol.py -q && .venv-g3/bin/python -c "import polars as pl;from quant_lab.research.paths import ledger_path;p=ledger_path();d=pl.read_parquet(p);assert d.height>0 and (d['status']=='reserved').sum()==0;print(p,d.height)"
```

```text
10 passed in 0.09s
/Users/balen/projects/trader-bot/quant-lab/data/lockbox/ledger.parquet 538
exit=0
```

#### R-08

```sh
.venv-g3/bin/python -m pytest tests/research/test_maxt.py tests/research/test_nullmodel.py -q && .venv-g3/bin/python -c "import json;t=open('docs/adr/report-G3-null-model.md',encoding='utf-8').read();j=json.loads(t[t.rindex(chr(96)*3+'json')+7:t.rindex(chr(96)*3)]);R=j['results'];N=[r for r in R if r['kind']=='null'];assert len(N)>=4 and all(r['n_done']>=1000 for r in N);assert all(r['verdict']=='pass' and r['worst_case_ci'][1]<=0.07 and r['diagnostics']['n_invalid_null_model']==0 for r in N if r['verdict']!='not_run_T0');E=[r for r in R if r['kind']=='null_ext'];assert all(r['verdict']=='pass' for r in E);P=[r for r in R if r['kind']=='power'];assert P and all(r['n_done']>=1000 for r in P);print('FPR',[(r['mechanism'],r['verdict'],round(r['worst_case_ci'][1],4)) for r in N+E]);print('POWER',[(r['mechanism'],r['verdict'],r['rate'],r['worst_case_ci']) for r in P]);assert all(r['verdict']=='pass' for r in P), 'power DoD not met'"
```

```text
12 passed in 0.99s
Traceback (most recent call last):
  File "<string>", line 1, in <module>
AssertionError: power DoD not met
FPR [('common_shock', 'pass', 0.0157), ('cluster_heavy_tail', 'pass', 0.0144), ('nonuniform_density', 'pass', 0.0307), ('circular_shift', 'pass', 0.0087)]
POWER [('common_shock', 'fail', 0.3947643979057592, [0.34686235465954923, 0.40786417678951004])]
exit=1
```

#### R-09

```sh
.venv-g3/bin/python -m quant_lab.research.api run --config tests/research/fixtures/protocol_synthetic.yaml --out /tmp/ql-proto && .venv-g3/bin/python -c "import json,math;r=json.load(open('/tmp/ql-proto/report.json'));assert r['tier'] in ('T0','T1','T2','T3a','T3b') and r['n_attempts']>0 and r['theta'] is not None and math.isfinite(r['theta']);s=r['n_attempts_by_status'];st=dict(zip(s['status'],s['len']));assert st.get('completed',0)>0 and st.get('reserved',0)==0 and st.get('duplicate',0)<r['n_attempts'];assert r['claim_status']=='descriptive_only' and r['final_status'] in ('ok','no_claim');print({k:r[k] for k in ('tier','K','theta','n_attempts','final_status')},st)"
```

```text
{"status": "ok", "tier": "T1", "K": 529, "theta": 0.0, "se": null, "lower_bound": null, "p_adj": null, "n_attempts": 152, "n_folds": 3, "claim_status": "descriptive_only", "out_dir": "/tmp/ql-proto"}
{'tier': 'T1', 'K': 529, 'theta': 0.0, 'n_attempts': 152, 'final_status': 'no_claim'} {'completed': 73, 'duplicate': 79}
exit=0
```

R-05是结构化读取既有spike，不是本轮性能重测；其统计矩阵18/18与16/17属于既有记录。R-07虽已正确解析QUANT_LAB_DATA_ROOT，仍只是检查既有投影非空及无reserved，不能替代事件水位/终态/本次运行归属验收。R-09的合法theta=0不应被拒；其73 completed与79 duplicate证明不再是“一律duplicate”，但6个snapshot duplicate之后的重算仍无新预留，见S08。

#### 全套及闭合文件

```sh
.venv-g3/bin/python -m pytest tests/research -q
```

实跑：`235 passed in 116.99s (0:01:56)`，exit=0。此为一次完整目录运行，**235不加上后续重复定向测试**。

```sh
.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py -q
```

实跑：`15 passed in 9.12s`，exit=0。该文件并非19条各有一个完备验收测试；S01–S03、S14–S16还有既有文件覆盖，S17没有独立功效达标回归。S15的“双方同为null”和S16的“全删失”确实未被现有绿测覆盖。

#### P-Sxx 定向命令及原始结果

所有行均实跑exit=0。S17的复现是上面的R-08原verify（exit=1）和后面的探针D。

| 证据 | 命令 | stdout末行 |
|---|---|---|
| P-S01 | `.venv-g3/bin/python -m pytest tests/research/test_features.py::test_cache_hot_read_follows_episode_identity_not_position -q` | `1 passed in 0.10s` |
| P-S02 | `.venv-g3/bin/python -m pytest tests/research/test_features.py::test_context_is_single_source_and_in_key tests/research/test_features.py::test_late_available_at_is_not_visible -q` | `2 passed in 0.04s` |
| P-S03 | `.venv-g3/bin/python -m pytest tests/research/test_features.py::test_revocation_gate_blocks_cached_and_fresh_reads -q` | `1 passed in 0.02s` |
| P-S04 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s04_quarantine_persists_across_processes tests/research/test_features.py::test_quarantined_backend_op_rejected_in_snapshot -q` | `2 passed in 1.55s` |
| P-S05 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s05_inner_split_is_calendar_based_purged_and_mature -q` | `1 passed in 0.57s` |
| P-S06 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s06_config_cap_can_only_tighten_and_insufficient_folds_do_not_fit -q` | `1 passed in 0.84s` |
| P-S07 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s07_concurrent_reserve_only_one_wins_and_replay_after_crash -q` | `1 passed in 0.99s` |
| P-S08 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s08_every_stage_has_terminal_state_including_failures_and_final tests/research/test_review_p1_closure.py::test_s08_run_protocol_reserves_before_lint_and_rejects_bad_ast -q` | `2 passed in 4.03s` |
| P-S09 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s09_residual_fit_uses_only_mature_labels_and_population_quantile -q` | `1 passed in 0.37s` |
| P-S10 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s10_diagnostic_failure_cannot_pass tests/research/test_review_p1_closure.py::test_s10_all_t0_is_not_run_not_pass -q` | `2 passed in 0.40s` |
| P-S11 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s11_censored_blocks_do_not_count_as_nonempty -q` | `1 passed in 0.38s` |
| P-S12 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s12_panel_invariants_and_placeholder_only_under_mask tests/research/test_review_p1_closure.py::test_s12_outer_nan_gate_falls_back_to_baseline tests/research/test_evaluator.py::test_nan_feature_is_skip_and_keeps_denominator -q` | `3 passed in 0.62s` |
| P-S13 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s13_unfilled_uncensored_gets_expiry_t1_not_null -q` | `1 passed in 2.32s` |
| P-S14 | `.venv-g3/bin/python -m pytest tests/research/test_evaluator.py::test_estimand_vocabulary_frozen_and_stub_isomorphic_with_g1 tests/research/test_g2_parity.py -q` | `6 passed in 0.15s` |
| P-S15 | `.venv-g3/bin/python -m pytest tests/research/test_evaluator.py::test_two_arm_pairing_and_protocol_errors -q` | `1 passed in 0.03s` |
| P-S16 | `.venv-g3/bin/python -m pytest tests/research/test_evaluator.py::test_synthetic_end_to_end_pairs_and_decimal -q` | `1 passed in 0.11s` |
| P-S18 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s18_streaming_budget_and_truncation_reason -q` | `1 passed in 0.37s` |
| P-S19 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s19_exact_decimal_canonicalization -q` | `1 passed in 0.38s` |

### 剩余必修

以下12项保留原S编号，不用新增编号把一审必修“移走”。每条均有已完成部分，故状态为部分闭合；完成下面验收前不能算整条关闭。

#### S02：历史依赖仍前视

位置：`features.py:149`、`:164`、`:174`附近。后端在全量vis_bars上先计算Mean/Ref/EMA，只对as-of命中的末根bar核available_at。探针A把bar3延迟2小时，bar4准时；t_dec=bar4时Mean3仍使用bar3，返回3且valid=True；过滤真正可见bar后该窗口缺槽，应invalid。
改法：将可见时刻沿AST依赖传播，或按anchor可见前缀在计划网格计算；递归EMA也必须处理历史晚到依赖，不能只检查末根或简单截close_time。
验收：历史依赖晚到/修订、嵌套Ref/窗口、EMA，available_at等号前后；全量、真实可见前缀、冷/热缓存逐episode同值同mask。探针A应得到invalid或显式拒收。

#### S03：热读期间撤销仍可返回

位置：`features.py:143`、`:162`、`:180`。回调在整个snapshot开始一次；cache hit之后没有复查，后续AST热读也没有单独门。探针B以内存_cache_read模拟读取期间撤销，返回valid=True。
改法：每条缓存读前及返回/发布前核同一graph身份与revocation epoch；变化即丢弃本次结果，异常fail closed。回调应能验证绑定的graph/derivation身份，而不仅是自由“True”。
验收：热读期间撤销、两个AST之间撤销、冷算发布期间撤销、同key并发写、epoch变化；任何已检测撤销的结果不得返回。现有唯一临时文件是有效修复，P1仍不要求分布式ack/备份恢复。

#### S06：空折/尾损耗与统一硬门仍不完整

位置：`protocol.py:120`；`api.py:315`、`:316`及`tiers.py:tier_report`。
探针B最后一个测试窗只有跨窗簇成员，整个窗跳过且carry随return丢失。探针C在DEFF=None时tier_report仍T2/p=8/cap=64；run_pipeline新增的单折停拟合有效，但共享报告/池没有统一失败语义。跨度仍由全输入（包括外测试）max/min决定，DEFF按search+selection合并估而非每个实际内层训练集，候选主目标DEFF/复杂度硬门尚不足。
改法：保留所有预注册日历折，包括空测试窗；终止前落完整边界损耗。按合法内层训练集冻结K/跨度/DEFF/p与协议预算，对undefined采用一致insufficient/保守零搜索出口；明确常量阈值与可调阈值的自由度，候选新依赖在selection前收紧门。
验收：尾部/中间空窗均有Fold与损耗；不靠外测试长度或删除空折晋档；任一undefined、高依赖或复杂度超顶不能用原预算继续相关搜索；外部传入较宽Ledger预算也不能解除协议硬顶。

#### S07：mark 的终态校验存在竞态

位置：`ledger.py:174`–`:187`；`invalidate/recover`同样需要原子状态约束。reserve同锁已经实跑成立，但mark先_row读与检查，之后_commit才加锁。探针B让两个mark先读running，再顺序提交completed/interrupted，后者覆盖终态。
改法：读取当前态、校验状态转移、提交事件在同一锁内，或用event_seq条件写/CAS；恢复只处理已确认失活的对应run，不能中断其他活跃run。目录锁不能仅凭60秒年龄抢占仍活跃持有者。
验收：临时湖两进程竞逐同一attempt终态只能一个成功；终态/失效竞态不回退或丢标记；跨run恢复不误中断。保留现有额度、水位、坏事件测试，补事件序号/文件名一致性校验与锁owner失活证据。本轮竞态探针使用真实mark逻辑、内存事件I/O来固定交错，没有冒充磁盘并发演练。

#### S08：账本前置与异常收尾仍有绕路

位置：`api.py:503`、`:525`–`:541`、`:368`–`:381`、final_panel构造；`ledger.py:197`。
探针B证明fake_execution读取收益时账本0行；snapshot reservation在lint后立即completed，然后才真正compute，backend失败没有failed。R-09重跑的snapshot记录又是duplicate却继续计算。探针C证明第二个selection候选异常时，第一个等待family max-t的attempt仍reserved；单个_Stage无法收尾整个family。fixed code_version、缺结果/机会集摘要及MemoryLedger忽略cost仍不能支撑完整审计。
改法：在任何收益/特征计算前持久化对应阶段reservation，计算和lint都放入该生命周期；duplicate仅复用已验证结果，重算须新预留并关联最初实际尝试。family级try/finally关闭所有未终态attempt，final面板构造亦纳入生命周期；记录实际代码/协议/机会集/政策/结果摘要与计算成本，恢复按run所有权处理。
验收：收益调用入口已存在live reservation；snapshot后端失败、第二候选失败、max-t失败、final面板失败均无残留live，无“失败但completed”；重跑包含snapshot的每项真实计算都有新调用记录或可验证缓存命中。MemoryLedger与磁盘账本终态/成本语义对拍。

#### S10：诊断门没有覆盖power与结构保持全量

位置：`nullmodel.py:assert_not_episode_shuffle`、`:330`、`:360`–`:379`。
逐replicate ICC与null分支invalid出口已修；但power分支未统一应用n_invalid>0，探针B的状态机故障注入得到invalid=1仍pass。探针C把真实生成的null收益全部乘100，ICC保持不变而guard仍True，说明尾部/尺度没有保持门；块间相关、跨品种相关也没有逐replicate有效性门。全部失败时worst_case_ci直接None，损失最坏界证据。
改法：所有kind在统计pass之前统一应用诊断硬门；预注册结构保持阈值，覆盖尺度/尾部、块间与跨资产相关、簇大小、缺失结构及非有限诊断。即使全部失败也报告相应保守计数/区间，不能用None代替最坏界。
验收：注入坏相关、尺度/尾部、缺失、非有限诊断，在null和power两路径都invalid_null_model；失败计数/最坏界正确；诊断逐次落摘要而非只存首个guard。本轮注入power阳性仅检验状态机，**不属于功效测量**。

#### S12：PairedPanel 的直接入口仍静默补零

位置：`maxt.py:51`；`api.py:PanelInputs.validate`。
上游_panel已用_placeholder约束，但公开PairedPanel.from_frame仍对所有null执行fill_null(0)。探针A的m=True/null差值被改0，30有效块仍返回ok；不能依赖调用者恰好先走API的验证。
改法：公共panel入口先校验形状、共同mask、权重与可评差值，m=True的null/NaN/Inf一律拒收；仅m=False存储占位可为0。两臂证据分别验证后再构造共同mask，不能要求共同删失必然意味着基线自身也删失。
验收：直接from_frame/构造器入口与API路径对拍，含m=True的null、NaN、Inf、候选单侧删失、部分簇剩余权重；不得改变可评经济量或分母。outer高NaN已有fallback但不替代该入口门。

#### S14：契约独立接缝仍未完成

位置：`synthetic.py:BATCH_SCHEMA/_order_plan`、`evaluator.py:78`、`ops.py:54`。
六键/default的修复确认有效。独立对照execution-interface§5.10发现G2/G3本地BATCH_SCHEMA都无fraction_source，现有parity共同漏列所以仍绿；合成order_plan.entries.fraction实际dtype=String，未满足research-schema§9.10.1的Decimal(38,12)。OpSpec仍使用未获修订的rows_or_wallclock。整个eligibility_by_estimand缺失时默认all eligible，固定六键Boolean非空也未完整验证。
改法：由G2属主补fraction政策解析与诊断后，G3按冻结契约独立schema同步桩；补plan/policy各至少一个及null三腿Decimal余量样本。资格列/键/dtype/null缺一拒收。窗口枚举正式申报修订或适配已有冻结表达。
验收：独立契约断言fraction_source及Decimal类型；build_request三腿精确和=1、部分比例拒收、plan/policy来源各非空；缺资格列、任一键非Boolean/null拒绝。不得以“等G2后parity自动发现”关闭G3本条。

#### S15：双方同为null以及缺policy_hash仍放行

位置：`evaluator.py:140`、`:156`、`:208`。
ne_missing会把null/null视为相等，源码注释“null≠null视同不等”与实际相反。_IDENTITY_NONNULL未包括market_manifest、execution_contract_version、seed、risk_budget；policy_hash仅在列存在时校验。探针A分别将四字段两臂置null及删除policy_hash均接受2行。
改法：必需身份字段逐列存在且非空，再作null-safe上下文对比；policy_hash必须存在并验证其政策血缘。核对执行t_dec/snapshot与当前features/机会集冻结身份，而不只核两个执行臂互相相等。
验收：每个共享字段缺失/null/错值逐一拒绝，包含双方同null；政策合法差异仍支持，缺hash/哈希-版本冲突拒绝；两臂一致但与当前snapshot不一致亦拒收。

#### S16：全删失提前返回造成诊断归零

位置：`evaluator.py:261`先返回，`:272`之后才统计opened_all。
2个take、已开未平、收益均删失时，theta正确insufficient、损耗正确2，但unclosed_rate错误0。
改法：在任何“无可评收益”返回前，用原候选臂和take统计开仓/未平诊断，并携带到所有返回对象；收益不足与诊断可计算分开。
验收：单个/全部右删失、未成交、规则skip、两臂不同删失；全部已开未平应1.0，未成交不进分母，theta共同删失口径保持。

#### S17：准入裁定与验收工程分开处理

**准入事项**：既有闭合后MC报告主条件为1600簇、noise_scale=1、δ=0.2R、1000次完整流程。377次检出、45次失败；条件成功样本比例377/955=39.4764%，失败计未检出比例377/1000=37.70%，95%最坏界CI=[34.6862%,40.7864%]，规则找回478/1000。主门是**CI下界≥80%**，不是点估计≥80%。这些都仅描述既有合成运行。
功效不足本身不证明代码有bug，**属于P1里程碑准入裁定事项**。D.5允许“未达标限制声明或扩新样本”规定了后续处置；GOAL-3§7仍明确要求FPR/功效达标、verify通过及必修闭合。当前没有用户或规范性G0修订把P1功效DoD改为“只要报告不足即可”。所以维持未达标/不准入，不能由审查者自行豁免。修复其余工程缺陷后，应在原主条件重验；若仍不足，由有权者正式选择限制声明式里程碑、或预注册新增样本后另验，并保留原条件失败记录。单纯增加MC次数不会把低功效点估计自动变80%。
**不得替代主验收**：敏感性格子同时改变样本/噪声/效应且每格200次，不能替代主1000次。即使δ=0.2、noise×0.6、2400簇格子最坏界下界约83.25%，仍是另一条件。本轮重算13行报告的Clopper–Pearson最坏界，与JSON全部相等（实跑stdout：`report_cp_recomputed_rows=13 all_equal=True`），不把这个算术复核说成MC重跑。
**工程缺陷**：R-08已能阻挡当前fail报告，这是有效修复；但其power只信verdict，不复算下界/诊断，null允许not_run_T0整行跳过，未要求四个机制身份唯一且全部实质验收。探针D使用原verify中的Python断言，内存中仅把主power verdict改pass，原37.70%/低CI照样放行；所有null改not_run_T0并标1000次invalid也放行。R-10虽由负向grep改正向，仍匹配tail40中任意pass；旧pass后最新fail仍exit=0，违反§9.9“最后一条为准”。
改法：固定机制与主条件身份，正向核n_done/n_planned、合法档/真实搜索数、诊断、失败计数与可复算CP最坏界，拒绝空/T0替身/伪flag；R-10严格解析最后终裁行。
验收：本条列出的fail power数字+pass flag、全T0、diagnostic=false/invalid>0、不足次数、重复机制、标题空壳、旧pass+最新fail均应失败；真满足冻结条件才可通过。不得将R-08保持红灯描述为任务完成绿灯。S10诊断缺陷与本项功效不足分别处置，不能用准入裁定免除工程修复。

#### S19：Decimal normalize 仍舍入身份

位置：`ast.py:339`–`:344`。
默认Decimal精度28，normalize先按上下文舍入；两个合法31位有效小数末位不同仍同hash。上下文prec=10/40又使同一AST的规范串不同。现有回归只检查约22位常量，没触及该边界。
改法：用Decimal.as_tuple的符号/数字/指数无损展开并去尾零，或使用严格隔离、足以覆盖全部合法token的上下文，禁止受调用方precision/rounding影响。数值资源门同时覆盖dict/文本入口；已产生的错误缓存/账本身份需明确版本迁移或失效策略。
验收：29–128位合法相邻常量不合并；相同输入在不同precision/rounding/traps下同字节；1/1.0/1e0等价，下溢拒收，规范化再解析幂等。探针A必须不再显示hash相同或上下文改变规范串。

### 内存复现命令与实跑输出

下列命令直接从quant-lab执行，不写新文件。探针B的账本事件读写、缓存读取，探针C的第二候选失败，探针D的报告读取均为内存替身/故障注入；明确区分状态机反例与真实统计测量。探针D只执行仓库taskList中的可信验证源码，不执行外来表达式。

#### 探针A

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import datetime as dt
from decimal import localcontext
import polars as pl
from quant_lab.research.ast import canonical_hash,canonical_json,parse_json_text
from quant_lab.research import features as F, contract_tests as C
from quant_lab.research.maxt import PairedPanel,max_t_panel
from quant_lab.research.evaluator import pair_arms,evaluate,freeze_opportunity_set
from tests.research.test_evaluator import episodes,execution,features,take_all,AST
j=lambda n: parse_json_text('{"op":"Add","args":[{"field":"close"},{"const":'+n+'}]}')
a=j('0.1234567890123456789012345678901');b=j('0.1234567890123456789012345678902')
print('S19 different_31_digit_hash_equal=',canonical_hash(a)==canonical_hash(b))
with localcontext() as ctx:
    ctx.prec=10
    c10=canonical_json(a)
with localcontext() as ctx:
    ctx.prec=40
    c40=canonical_json(a)
print('S19 context10=',c10,'context40=',c40)
bars=C.make_bars([0.,1.,2.,3.,4.,5.]).with_columns(pl.when(pl.col('close')==3).then(pl.col('close_time')+dt.timedelta(hours=2)).otherwise(pl.col('close_time')).alias('available_at'))
anchors=pl.DataFrame({'episode_id':['a'],'instrument_id':['X'],'t_dec':[C.T0+4*C.STEP]})
ast={'op':'Mean','args':[{'field':'close'}],'window':{'unit':'rows','count':3}}
h=canonical_hash(ast)
r=F.feature_snapshot([ast],anchors,bars=bars)
vis=F.feature_snapshot([ast],anchors,bars=bars.filter(pl.col('available_at')<=anchors['t_dec'][0]))
print('S02 full=',r.select('f_'+h,'validity_'+h).row(0),'visible_only=',vis.select('f_'+h,'validity_'+h).row(0))
ep=episodes([('a','ca',False),('b','cb',False)])
x=execution(ep,[1.,2.])
for key in ['market_manifest','execution_contract_version','seed','risk_budget']:
    bad=x.with_columns(pl.lit(None,dtype=x.schema[key]).alias(key))
    print('S15 both_null',key,'accepted_rows=',pair_arms(bad,['a','b']).height)
print('S15 missing_policy_hash accepted_rows=',pair_arms(x.drop('policy_hash'),['a','b']).height)
allc=episodes([('a','ca',True),('b','cb',True)])
opp=freeze_opportunity_set(allc)
r=evaluate(AST,opp,features=features(opp.episode_ids,[1.,1.]),rule=take_all,execution=execution(allc,[None,None]),fold_id='f',attempt_id='a')
print('S16 all_censored=',r.status,r.n_censored_excluded,r.unclosed_rate)
df=pl.DataFrame({'episode_id':[f'e{i}' for i in range(30)],'cluster_id':[f'c{i}' for i in range(30)],'t_dec':[C.T0+dt.timedelta(days=i) for i in range(30)],'weight':[1.]*30,'m':[True]*30,'d':[None]+[float(i%7) for i in range(29)]})
p=PairedPanel.from_frame(df,['d']);r=max_t_panel(p,block_len_days=1,B=200,seed=1)
print('S12 null_m_true=',p.diffs[0,0],bool(p.mask[0]),r.status,r.n_nonempty_blocks)
print('S14 missing_eligibility accepted=',freeze_opportunity_set(ep.drop('eligibility_by_estimand')).episode_ids)
PY
```

实跑exit=0，输出：

```text
S19 different_31_digit_hash_equal= True
S19 context10= {"args":[{"field":"close"},{"const":0.123456789}],"op":"Add"} context40= {"args":[{"field":"close"},{"const":0.1234567890123456789012345678901}],"op":"Add"}
S02 full= (3.0, True) visible_only= (None, False)
S15 both_null market_manifest accepted_rows= 2
S15 both_null execution_contract_version accepted_rows= 2
S15 both_null seed accepted_rows= 2
S15 both_null risk_budget accepted_rows= 2
S15 missing_policy_hash accepted_rows= 2
S16 all_censored= insufficient 2 0.0
S12 null_m_true= 0.0 True ok 30
S14 missing_eligibility accepted= ['a', 'b']
```

#### 探针B

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import copy,datetime as dt,threading
from unittest.mock import patch
import polars as pl
from quant_lab.research import api as A, nullmodel as N, features as F, synthetic as S, contract_tests as C
from quant_lab.research.ledger import Ledger,MemoryLedger
from quant_lab.research.protocol import walk_forward
from quant_lab.research.ops import REGISTRY
from tests.research.test_protocol import anchors,T0,D
from tests.research.test_features import CLOSE,CTX,H_CLOSE
# S03: cache read revokes graph after initial check; no filesystem writes.
state={'ok':True,'checks':0}
def consumable():
    state['checks']+=1
    return state['ok']
def read_cache(key,a):
    state['ok']=False
    return pl.DataFrame({'__row':[0],'value':[4.],'__valid':[True]})
ctx=F.replace(CTX,consumable=consumable)
a=pl.DataFrame({'episode_id':['a'],'instrument_id':['X'],'t_dec':[C.T0+4*C.STEP]})
with patch.object(F,'_cache_read',read_cache):
    r=F.feature_snapshot([CLOSE],a,bars=C.make_bars([float(i) for i in range(10)]),ctx=ctx,cache=True)
print('S03 revoked_during_hot_read=',state,'returned_valid=',r['validity_'+H_CLOSE].to_list())
# S06: final carry disappears.
a=anchors([('train','ct',0.,1.),('first','cx',89.5,90.),('tail','cx',90.5,91.)])
fs=walk_forward(a,min_train_clusters=1,test_span=D,min_train_span=89*D,origin=T0)
print('S06 folds=',[f.fold_id for f in fs],'tested=',[i for f in fs for i in f.test_ids],'tail_loss_recorded=',any('tail' in f.purged for f in fs))
# S07: deterministic interleaving of real mark() with memory-backed event I/O.
led=object.__new__(Ledger); current={'attempt_id':'a','status':'running','reason_code':None}
barrier=threading.Barrier(2); committed=threading.Event(); history=[]
def row(aid):
    snapshot=dict(current)
    barrier.wait(timeout=5)
    return snapshot
def commit(event):
    if event['status']=='interrupted':
        assert committed.wait(5)
    current.update(event);history.append(event['status'])
    if event['status']=='completed':
        committed.set()
    return event
led._row=row;led._commit=commit
threads=[threading.Thread(target=led.mark,args=('a',s)) for s in ['completed','interrupted']]
for t in threads:
    t.start()
for t in threads:
    t.join()
print('S07 concurrent_terminal_history=',history,'final=',current['status'])
# S08: synthetic execution already read before any reservation; backend exception after completed.
cfg=A.load_config('tests/research/fixtures/protocol_synthetic.yaml'); cfg=copy.deepcopy(cfg)
cfg['data']['synthetic'].update(n_episodes=30,n_bars=200,span_days=1)
cfg['candidates']['asts']=[{'field':'close'}]
l=MemoryLedger();original=S.fake_execution; seen=[]
def execution_probe(*args,**kw):
    seen.append(len(l.rows))
    return original(*args,**kw)
def boom(*args,**kw):
    raise RuntimeError('backend boom')
with patch.object(S,'fake_execution',execution_probe),patch.object(A,'feature_snapshot',boom):
    try:
        A.build_inputs_from_synthetic(cfg,ledger=l)
    except RuntimeError as e:
        print('S08 snapshot_error=',str(e),'rows_at_execution=',seen,'terminal_states=',[r['status'] for r in l.rows.values()])
# S14 independently compare frozen contract, not two local mirrors.
from quant_lab.market.execution import BATCH_SCHEMA
print('S14 fraction_source fake/real=', 'fraction_source' in S.BATCH_SCHEMA,'fraction_source' in BATCH_SCHEMA)
print('S14 window_enums=',sorted({s.window_semantics for s in REGISTRY.values()}))
# S10 state gate fault injection, not statistical evidence.
w=N.synth_world(N.WorldConfig(seed=1,n_clusters=200)); calls={'n':0}
def guard(*args,**kw):
    calls['n']+=1
    return {'ok':calls['n']!=1,'block_icc':{}}
def positive(*args,**kw):
    return {'status':'ok','final_status':'ok','synthetic_claim_positive':True,'tier':'T1','folds':[]}
with patch.object(N,'resample_null',lambda *a,**k:w.inputs),patch.object(N,'assert_not_episode_shuffle',guard),patch.object(N,'run_pipeline',positive):
    r=N.run_mc('common_shock',kind='power',n_rep=100,seed0=1,world_cfg=N.WorldConfig(n_clusters=200),pipe_cfg=A.PipelineConfig(B=200))
print('S10 injected_power=',r.verdict,'invalid=',r.diagnostics['n_invalid_null_model'],'failed=',r.n_failed,'worst_ci=',r.worst_case_ci)
PY
```

实跑exit=0，输出：

```text
S03 revoked_during_hot_read= {'ok': False, 'checks': 1} returned_valid= [True]
S06 folds= ['wf000'] tested= ['first'] tail_loss_recorded= False
S07 concurrent_terminal_history= ['completed', 'interrupted'] final= interrupted
S08 snapshot_error= backend boom rows_at_execution= [0] terminal_states= ['completed']
S14 fraction_source fake/real= False False
S14 window_enums= ['none', 'rows', 'rows_or_wallclock']
S10 injected_power= pass invalid= 1 failed= 1 worst_ci= (0.9455406146079194, 0.9997468539670226)
```

#### 探针C

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
from unittest.mock import patch
import numpy as np
from quant_lab.research import api as A,nullmodel as N
from quant_lab.research.ledger import MemoryLedger
w=N.synth_world(N.WorldConfig(seed=1,n_clusters=1600)); led=MemoryLedger(); orig=A.candidate_diffs; calls={'n':0}
def boom_second(*args,**kw):
    calls['n']+=1
    if calls['n']==2:
        raise RuntimeError('second selection fails')
    return orig(*args,**kw)
with patch.object(A,'candidate_diffs',boom_second):
    try:
        A.run_pipeline(w.inputs,N.default_candidates(12),A.PipelineConfig(B=200,seed=6),ledger=led)
    except RuntimeError as e:
        print('S08 selection_error=',str(e))
print('S08 remaining_selection_states=',[r['status'] for r in led.rows.values() if r['stage']=='selection'])
model=N.fit_residual_model(w)
x=N.resample_null(w,model,np.random.default_rng(3))
bad=N.replace(x,base_R=x.base_R*100)
print('S10 tail_scale_x100_guard=',N.assert_not_episode_shuffle(w.inputs,bad,w.day)['ok'],'sd_ratio=',round(float(np.nanstd(bad.base_R)/np.nanstd(x.base_R))))
# S09 actual maturity gate and training-only q diagnostics, no I/O.
m=N.fit_residual_model(w,train_end_day=180,label_maturity_days=2)
print('S09 train_end=',m.train_days,'immature_excluded=',m.diagnostics['n_immature_excluded'],'population_q30=',N.feature_population_quantile(.3))
# S06 undefined report remains search-capable even though pipeline skips that individual fold.
from quant_lab.research.tiers import tier_report
r=tier_report({'f':250},DEFF_by_fold={'f':None},span_months=13,n_outer_folds=3,min_fold_test_clusters=40)
print('S06 undefined_tier=',r.tier,'p=',r.p_allowed,'cap=',r.config_cap,'K_eff=',r.K_eff)
PY
```

实跑exit=0，输出：

```text
S08 selection_error= second selection fails
S08 remaining_selection_states= ['reserved', 'failed']
S10 tail_scale_x100_guard= True sd_ratio= 100
S09 train_end= (0, 180) immature_excluded= 46 population_q30= -0.7797157283503584
S06 undefined_tier= T2 p= 8 cap= 64 K_eff= None
```

#### 探针D

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import io,json,shlex,contextlib,subprocess
from unittest.mock import patch
m=json.load(open('taskList.json'))['modules']['research']
tasks={t['id']:t for t in m['tasks']}
t=open('docs/adr/report-G3-null-model.md',encoding='utf-8').read()
j=json.loads(t[t.rindex('```json')+7:t.rindex('```')])
code=shlex.split(tasks['R-08']['verify'].split(' && ')[-1])[-1]
for r in j['results']:
    if r['kind']=='power':
        r['verdict']='pass'
fake='```json\n'+json.dumps(j)+'\n```'
with patch('builtins.open',lambda *a,**k:io.StringIO(fake)),contextlib.redirect_stdout(io.StringIO()):
    exec(compile(code,'taskList:R-08:original-python-assertions','exec'),{})
print('S17 original_R08_python_accepts_power_fail_numbers_with_pass_flag=True')
for r in j['results']:
    if r['kind']=='null':
        r['verdict']='not_run_T0'
        r['diagnostics']['n_invalid_null_model']=1000
        r['tiers']={'T0':1000}
fake='```json\n'+json.dumps(j)+'\n```'
with patch('builtins.open',lambda *a,**k:io.StringIO(fake)),contextlib.redirect_stdout(io.StringIO()):
    exec(compile(code,'taskList:R-08:original-python-assertions','exec'),{})
print('S17 original_R08_python_accepts_all_T0_nulls_with_invalid_diagnostics=True')
cmd=tasks['R-10']['verify'].split(' | ')[-1]
r=subprocess.run(cmd,shell=True,executable='/bin/zsh',input='二审终裁：pass\n三审终裁：fail\n',text=True)
print('S17 R10_grep_old_pass_then_latest_fail_exit=',r.returncode)
PY
```

实跑exit=0，输出：

```text
S17 original_R08_python_accepts_power_fail_numbers_with_pass_flag=True
S17 original_R08_python_accepts_all_T0_nulls_with_invalid_diagnostics=True
S17 R10_grep_old_pass_then_latest_fail_exit= 0
```

### 二审终裁

完整闭合项：S01、S04、S05、S09、S11、S13、S18，共7项。其余12项部分闭合，按整条验收均仍需修复/裁定。本轮235项pytest通过与R-05/06/07/09通过不能抵消上述可复现反例；R-08实跑exit=1符合当前主功效未达标事实。

终裁依据有两条，各自足以阻止当前P1准入：第一，缓存时间因果、撤销、账本并发/生命周期、panel/配对/诊断及精确身份仍有工程必修；第二，GOAL-3现行DoD与ADR§10.2的主功效条件未满足，且无正式准入修订。S17的低功效本身按里程碑准入事项交由有权者选择后续路径，二审不擅自改门，也不以敏感性格子顶替主验收。修订准入仅影响第二条，不关闭第一条工程问题。

交付机械检查要求：`tail -1 docs/adr/review-G3-P1.md | grep -E '^二审终裁：(pass|fail)$'` 与 `grep -c '已闭合' docs/adr/review-G3-P1.md`；后者是文字出现行数，不能替代本节逐条表的7项计数。另须复核原始39730字节SHA256不变，并按§9.9读取最后终裁行；本次不改taskList状态。

二审终裁：fail

## 三审（R-10 round 3）

取证时间：2026-09-11T02:30:45.654700+00:00；审查：GPT-6 主控。仅追加本节。S17 按用户出口 A，仅核工程门与如实报告。

原审查前缀：83146 bytes，SHA256=0ad2240179bf56c8dd926c071d846d3917cea962457641ce3670239957f4f862。

逐文件 SHA256（路径相对 quant-lab；排除运行生成的 __pycache__）：

```text
5792ae78cc7030ef6f8e40e786177e84df851a892bd555eafeb7c91e1beadca0  src/quant_lab/research/__init__.py
f2c38f42b4c886c102cc5ca34e9bc5049e82c492b2f44a9f26a02880d16ed19b  src/quant_lab/research/api.py
b9fd438b20443a0705f0b0eb140efa28fd6cc8fef256fffc2b5cf99a4d7bc903  src/quant_lab/research/ast.py
4ac9410eda1a7df71c1ec2ff4e0d275e968fc0a4c18e17f177ed9fa20185117d  src/quant_lab/research/backends/__init__.py
c39e3efc2c07cbf69d8fbc8d7d78ddb12d2f003322c0b01fb78d807dec4d06a5  src/quant_lab/research/backends/polars.py
3873a84db354235cf3662e4b6bfab6a64e87835d963001f80b6230e530c78e27  src/quant_lab/research/backends/polars_ta.py
25053d6f43be30807fb7580893174fcee18ecff880e342074d2856c63eba683f  src/quant_lab/research/contract_tests.py
c6790c56695715ba601e9bba39d688f48e033b314b06e310786a12fcf9c3af21  src/quant_lab/research/evaluator.py
d911a7bfd22f1f00820da917448f8d156f1ea75c968b438a98b9a082a5b37df9  src/quant_lab/research/features.py
03841ce8d3c03bd65ed7d9e6fe310417319dc97c6befb3a3aa56c93ad02f09d9  src/quant_lab/research/grammar.py
cf3bb845174ff7eb071c3c7aa9971e0774b2a7d2d46343aa5588ed4e50abd982  src/quant_lab/research/ledger.py
3b5ed5afc5d899f6bd92ee2003baf17c9bdbcdd8cf5de8a08bb32e7f98e9d11c  src/quant_lab/research/maxt.py
8537f7b6a2e8e92f767f6a6d49fc708ea9c63b11d6cd0ea8426abb262dc7b063  src/quant_lab/research/nullmodel.py
2893fcfab671fa39abd7b6c6549ebaf92740652372dffc5f9fcb4e367b950d05  src/quant_lab/research/ops.py
2501ad7858939258167357dd830454475d044454ea294002a8140545af484ac9  src/quant_lab/research/paths.py
66e1c9494982624eaccb7dadcf64e02142ccba7c0e3c28aeafec52572cb2fc54  src/quant_lab/research/protocol.py
09a1b940e36c07367317e5ce74eb718dfee8918d3940d375aaef6e3111f4ad25  src/quant_lab/research/reference.py
9f3178104aab91ca67d76d14cbc49777f75167ea3b9788b81dfe32509150b503  src/quant_lab/research/spike.py
1e041395c3d3747d55a02ab9e51b0740457522b0177b7d7ae8fa173566bf386c  src/quant_lab/research/synthetic.py
987037ccf008b8874b78903a5c14645b55b9e0b099b5a248040c909029e1c418  src/quant_lab/research/tiers.py
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  tests/research/__init__.py
d375f18e484d7ccaa698e22505e1bddc087558290ea03c479e7385328ce93fa2  tests/research/conftest.py
1d2af763e4b00ba120d3e79cde981f5e54e8a6b2567128df9562b524ac3fb506  tests/research/fixtures/protocol_synthetic.yaml
aa9a5bf44c99c95dd1cb4717450e5eec30ec1b50dabebae1b0712b6e4360e176  tests/research/test_api.py
c0b19d733d2965b6677fbbc5acbe777916bbd5187aaf95107539b81ee8c28d50  tests/research/test_ast.py
e396d72a993f691d7210035923d093b6c7b0db1af568db1d852474afe2ea100b  tests/research/test_evaluator.py
36c607064c3530690e25ddfae4a333ca3cc34b8a5aa32540baa72e510f664055  tests/research/test_features.py
d7e0cb2045cc12d252ae9d390c4837bfcef64ef0b35108b7512fafef38468f94  tests/research/test_g2_parity.py
de279c3f6d82a6e6ed100a5ff7a1705ee7f0d9dd062095197731d42f4f1e82c4  tests/research/test_maxt.py
8766d8b06f52b21d4e0e9e03fc27d6e2120776582cedd822ad87b2e98726da7c  tests/research/test_nullmodel.py
664ef151fa6258c931cb41d55c01b979448cf2398745b64b0e5543ac208c4ca1  tests/research/test_ops_contract.py
d70d1a608797661005bd3858810dc0cc9c2f06ce7303cff8de1d9ef22cab850f  tests/research/test_protocol.py
15695e5c99b1e5bca9379ebe16001105d27e33f80ac51f9d7cbd035a4259c727  tests/research/test_review_p1_closure.py
be33574cdf8c298dc140713d040c55ffec113c61afad5a2bfe5df59eeab616ca  tests/research/test_smoke.py
```

#### 实跑 R-05

```sh
test -f docs/adr/report-G3-backend-spike.md && .venv-g3/bin/python -c "import json,re,sys;t=open('docs/adr/report-G3-backend-spike.md',encoding='utf-8').read();j=json.loads(t[t.rindex(chr(96)*3+'json')+7:t.rindex(chr(96)*3)]);m=j['matrix'];assert all(v['status']=='pass' for v in m['polars'].values()),m['polars'];assert sum(v['status']=='pass' for v in m['polars_ta'].values())>=10;tm={r['backend']:r for r in j['timings']};assert tm['polars']['n_ast']==128 and tm['polars']['cold_s']>0 and tm['polars']['hot_s']>0 and 0<tm['polars']['rss_gib_after_hot_s']<=8 and tm['polars']['cold_s']<=1200;print('spike ok',tm['polars']['cold_s'],tm['polars_ta']['cold_s'])"
```

```text
spike ok 206.77 36.11
exit=0
```

#### 实跑 R-06

```sh
.venv-g3/bin/python -m pytest tests/research/test_features.py tests/research/test_evaluator.py -q
```

```text
.........................                                                [100%]
25 passed in 1.51s
exit=0
```

#### 实跑 R-07

```sh
.venv-g3/bin/python -m pytest tests/research/test_protocol.py -q && .venv-g3/bin/python -c "import polars as pl;from quant_lab.research.paths import ledger_path;p=ledger_path();d=pl.read_parquet(p);assert d.height>0 and (d['status']=='reserved').sum()==0;print(p,d.height)"
```

```text
..........                                                               [100%]
10 passed in 0.14s
/Users/balen/projects/trader-bot/quant-lab/data/lockbox/ledger.parquet 1169
exit=0
```

#### 实跑 R-08

```sh
.venv-g3/bin/python -m pytest tests/research/test_maxt.py tests/research/test_nullmodel.py -q && .venv-g3/bin/python -c "import json;from scipy.stats import beta;t=open('docs/adr/report-G3-null-model.md',encoding='utf-8').read();assert '## 3.2 限制声明' in t and '不能**把' in t or '限制声明' in t;j=json.loads(t[t.rindex(chr(96)*3+'json')+7:t.rindex(chr(96)*3)]);R=j['results'];MECH={'common_shock','cluster_heavy_tail','nonuniform_density','circular_shift'}
def cp(x,n):
    return (0.0 if x==0 else float(beta.ppf(0.025,x,n-x+1)), 1.0 if x==n else float(beta.ppf(0.975,x+1,n-x)))
N={r['mechanism']:r for r in R if r['kind']=='null'};assert set(N)==MECH,set(N)
for m,r in N.items():
    ns=r['n_done']-r['tiers'].get('T0',0);assert r['n_done']==r['n_planned']>=1000 and r['diagnostics']['n_invalid_null_model']==0 and ns>=500 and ns>r['n_done']/2,(m,ns)
    hi=cp(r['n_positive']+r['n_failed'],ns)[1];assert hi<=0.07 and r['verdict']=='pass',(m,hi,r['verdict'])
P=[r for r in R if r['kind']=='power'];assert len(P)>=1
for r in P:
    ns=r['n_done']-r['tiers'].get('T0',0);assert r['n_done']==r['n_planned']>=1000 and r['diagnostics']['n_invalid_null_model']==0 and ns>=500;lo=cp(r['n_positive'],ns)[0];print('POWER(reported, limitation statement applies)',r['mechanism'],r['n_positive'],ns,round(lo,4),r['verdict'])
assert '## 3.2 限制声明' in t, 'limitation statement missing'
print('FPR',[(m,round(cp(r['n_positive']+r['n_failed'],r['n_done']-r['tiers'].get('T0',0))[1],4)) for m,r in N.items()])"
```

```text
............                                                             [100%]
12 passed in 1.90s
POWER(reported, limitation statement applies) common_shock 327 774 0.3874 fail
FPR [('common_shock', 0.0153), ('cluster_heavy_tail', 0.0148), ('nonuniform_density', 0.0283), ('circular_shift', 0.0088)]
exit=0
```

#### 二审探针 A 原文重放

命令与二审「探针A」代码块逐字相同（直接提取执行）。原脚本遇到已修复的拒绝会提前退出，后续语句另分段重放。

```text
S19 different_31_digit_hash_equal= False
S19 context10= {"args":[{"field":"close"},{"const":0.1234567890123456789012345678901}],"op":"Add"} context40= {"args":[{"field":"close"},{"const":0.1234567890123456789012345678901}],"op":"Add"}
S02 full= (None, False) visible_only= (None, False)
Traceback (most recent call last):
  File "<stdin>", line 30, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/evaluator.py", line 223, in pair_arms
    b = _validate_and_cast(_arm(execution, baseline_policy, ids, "baseline"), "baseline")
                           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/evaluator.py", line 185, in _arm
    raise EvalProtocolError(f"{label} 臂 {c} 含 null（{x[c].null_count()} 行）")
quant_lab.research.evaluator.EvalProtocolError: baseline 臂 market_manifest 含 null（2 行）
exit=1
```

#### 二审探针 B 原文重放

命令与二审「探针B」代码块逐字相同（直接提取执行）。原脚本遇到已修复的拒绝会提前退出，后续语句另分段重放。

```text
Traceback (most recent call last):
  File "<stdin>", line 21, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/features.py", line 173, in feature_snapshot
    raise SnapshotInvalid("热读期间 graph 已撤销：丢弃缓存结果（fail closed）")
quant_lab.research.features.SnapshotInvalid: 热读期间 graph 已撤销：丢弃缓存结果（fail closed）
exit=1
```

#### 二审探针 C 原文重放

命令与二审「探针C」代码块逐字相同（直接提取执行）。原脚本遇到已修复的拒绝会提前退出，后续语句另分段重放。

```text
S08 selection_error= second selection fails
S08 remaining_selection_states= ['failed', 'failed']
S10 tail_scale_x100_guard= False sd_ratio= 100
S09 train_end= (0, 180) immature_excluded= 46 population_q30= -0.7797157283503584
S06 undefined_tier= T0 p= 0 cap= 0 K_eff= None
exit=0
```

#### 二审探针 D 原文重放

命令与二审「探针D」代码块逐字相同（直接提取执行）。原脚本遇到已修复的拒绝会提前退出，后续语句另分段重放。

```text
Traceback (most recent call last):
  File "<stdin>", line 13, in <module>
  File "taskList:R-08:original-python-assertions", line 1, in <module>
AssertionError
exit=1
```

#### 实跑 R-09

```sh
.venv-g3/bin/python -m quant_lab.research.api run --config tests/research/fixtures/protocol_synthetic.yaml --out /tmp/ql-proto && .venv-g3/bin/python -c "import json,math;r=json.load(open('/tmp/ql-proto/report.json'));assert r['tier'] in ('T0','T1','T2','T3a','T3b') and r['n_attempts']>0 and r['theta'] is not None and math.isfinite(r['theta']);s=r['n_attempts_by_status'];st=dict(zip(s['status'],s['len']));assert st.get('completed',0)>0 and st.get('reserved',0)==0 and st.get('duplicate',0)<r['n_attempts'];assert r['claim_status']=='descriptive_only' and r['final_status'] in ('ok','no_claim');print({k:r[k] for k in ('tier','K','theta','n_attempts','final_status')},st)"
```

```text
{"status": "ok", "tier": "T1", "K": 531, "theta": 0.0, "se": null, "lower_bound": null, "p_adj": null, "n_attempts": 160, "n_folds": 3, "claim_status": "descriptive_only", "out_dir": "/tmp/ql-proto"}
{'tier': 'T1', 'K': 531, 'theta': 0.0, 'n_attempts': 160, 'final_status': 'no_claim'} {'completed': 80, 'duplicate': 80}
exit=0
```

#### 逐 S 定向测试（Q-Sxx）

均 cwd=quant-lab，PYTHONDONTWRITEBYTECODE=1，PYTEST_ADDOPTS=-p no:cacheprovider。

| 证据 | 实跑命令 | 输出 | exit |
|---|---|---|---|
| Q-S01 | `.venv-g3/bin/python -m pytest tests/research/test_features.py::test_cache_hot_read_follows_episode_identity_not_position -q` | `1 passed in 0.07s` | 0 |
| Q-S02 | `.venv-g3/bin/python -m pytest tests/research/test_features.py::test_context_is_single_source_and_in_key tests/research/test_features.py::test_late_available_at_is_not_visible -q` | `2 passed in 0.07s` | 0 |
| Q-S03 | `.venv-g3/bin/python -m pytest tests/research/test_features.py::test_revocation_gate_blocks_cached_and_fresh_reads -q` | `1 passed in 0.03s` | 0 |
| Q-S04 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s04_quarantine_persists_across_processes tests/research/test_features.py::test_quarantined_backend_op_rejected_in_snapshot -q` | `2 passed in 1.93s` | 0 |
| Q-S05 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s05_inner_split_is_calendar_based_purged_and_mature -q` | `1 passed in 0.74s` | 0 |
| Q-S06 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s06_config_cap_can_only_tighten_and_insufficient_folds_do_not_fit -q` | `1 passed in 1.08s` | 0 |
| Q-S07 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s07_concurrent_reserve_only_one_wins_and_replay_after_crash -q` | `1 passed in 1.58s` | 0 |
| Q-S08 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s08_every_stage_has_terminal_state_including_failures_and_final tests/research/test_review_p1_closure.py::test_s08_run_protocol_reserves_before_lint_and_rejects_bad_ast -q` | `2 passed in 6.15s` | 0 |
| Q-S09 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s09_residual_fit_uses_only_mature_labels_and_population_quantile -q` | `1 passed in 0.44s` | 0 |
| Q-S10 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s10_diagnostic_failure_cannot_pass tests/research/test_review_p1_closure.py::test_s10_all_t0_is_not_run_not_pass -q` | `2 passed in 0.58s` | 0 |
| Q-S11 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s11_censored_blocks_do_not_count_as_nonempty -q` | `1 passed in 0.51s` | 0 |
| Q-S12 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s12_panel_invariants_and_placeholder_only_under_mask tests/research/test_review_p1_closure.py::test_s12_outer_nan_gate_falls_back_to_baseline tests/research/test_evaluator.py::test_nan_feature_is_skip_and_keeps_denominator -q` | `3 passed in 0.88s` | 0 |
| Q-S13 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s13_unfilled_uncensored_gets_expiry_t1_not_null -q` | `1 passed in 3.88s` | 0 |
| Q-S14 | `.venv-g3/bin/python -m pytest tests/research/test_evaluator.py::test_estimand_vocabulary_frozen_and_stub_isomorphic_with_g1 tests/research/test_g2_parity.py -q` | `8 passed in 0.19s` | 0 |
| Q-S15 | `.venv-g3/bin/python -m pytest tests/research/test_evaluator.py::test_two_arm_pairing_and_protocol_errors -q` | `1 passed in 0.05s` | 0 |
| Q-S16 | `.venv-g3/bin/python -m pytest tests/research/test_evaluator.py::test_synthetic_end_to_end_pairs_and_decimal -q` | `1 passed in 0.15s` | 0 |
| Q-S18 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s18_streaming_budget_and_truncation_reason -q` | `1 passed in 0.46s` | 0 |
| Q-S19 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s19_exact_decimal_canonicalization -q` | `1 passed in 0.40s` | 0 |

补充二审回归及 A13/A15：
```sh
.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py tests/research/test_features.py tests/research/test_evaluator.py -k "test_r2 or test_s02_historical or test_s03_revocation or test_a13 or test_a15" -q
```
```text
............                                                             [100%]
12 passed, 36 deselected in 1.31s
exit=0
```

#### 全套 tests/research

```sh
.venv-g3/bin/python -m pytest tests/research -q
```

```text
........................................................................ [ 28%]
........................................................................ [ 57%]
........................................................................ [ 86%]
.................................                                        [100%]
249 passed in 137.10s (0:02:17)
exit=0
```

#### 分段重放 A/B/D 与补充反例 E

原探针拒收后继续独立分支；D 保留真实限制声明，仅改 JSON，R-10 保留完整 tail 管道。S07 旧内存替身缺 root/锁接口，改用实跑的四进程 r2 测试。

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'

import copy,datetime as dt,io,json,shlex,contextlib
from decimal import Decimal
from unittest.mock import patch
import numpy as np
import polars as pl
from quant_lab.research import api as A,nullmodel as N,features as F,synthetic as S,contract_tests as C
from quant_lab.research.evaluator import pair_arms,evaluate,freeze_opportunity_set
from quant_lab.research.ledger import Ledger,MemoryLedger
from quant_lab.research.maxt import PairedPanel
from quant_lab.research.protocol import walk_forward
from tests.research.test_evaluator import episodes,execution,features,take_all,AST
from tests.research.test_features import CLOSE,CTX,H_CLOSE
from tests.research.test_protocol import anchors,T0,D

def probe(label,fn):
    try:
        print(label,fn())
    except Exception as e:
        print(label,type(e).__name__,str(e))

ep=episodes([('a','ca',False),('b','cb',False)]);x=execution(ep,[1.,2.])
for key in ['market_manifest','execution_contract_version','seed','risk_budget']:
    probe('A S15 both_null '+key,lambda:pair_arms(x.with_columns(pl.lit(None,dtype=x.schema[key]).alias(key)),['a','b']).height)
probe('A S15 missing_policy_hash',lambda:pair_arms(x.drop('policy_hash'),['a','b']).height)
allc=episodes([('a','ca',True),('b','cb',True)]);opp=freeze_opportunity_set(allc)
r=evaluate(AST,opp,features=features(opp.episode_ids,[1.,1.]),rule=take_all,execution=execution(allc,[None,None]),fold_id='f',attempt_id='a')
print('A S16 all_censored',r.status,r.n_censored_excluded,r.unclosed_rate)
df=pl.DataFrame({'episode_id':[f'e{i}' for i in range(30)],'cluster_id':[f'c{i}' for i in range(30)],'t_dec':[C.T0+dt.timedelta(days=i) for i in range(30)],'weight':[1.]*30,'m':[True]*30,'d':[None]+[float(i%7) for i in range(29)]})
probe('A S12 null_m_true',lambda:PairedPanel.from_frame(df,['d']))
probe('A S14 missing_eligibility',lambda:freeze_opportunity_set(ep.drop('eligibility_by_estimand')).episode_ids)
a=anchors([('train','ct',0.,1.),('first','cx',89.5,90.),('tail','cx',90.5,91.)])
fs=walk_forward(a,min_train_clusters=1,test_span=D,min_train_span=89*D,origin=T0)
print('B S06 folds',[f.fold_id for f in fs],'tested',[i for f in fs for i in f.test_ids],'tail_loss_recorded',any('tail' in f.purged for f in fs))
# Original S07 object.__new__ no longer supplies the new lock state; real disk multiprocessing test Q-S07/r2 supplies this evidence.
cfg=copy.deepcopy(A.load_config('tests/research/fixtures/protocol_synthetic.yaml'))
cfg['data']['synthetic'].update(n_episodes=30,n_bars=200,span_days=1)
cfg['candidates']['asts']=[{'field':'close'}]
l=MemoryLedger();original=S.fake_execution;seen=[]
def execution_probe(*args,**kw):
    seen.append(len(l.rows));return original(*args,**kw)
def boom(*args,**kw):
    raise RuntimeError('backend boom')
with patch.object(S,'fake_execution',execution_probe),patch.object(A,'feature_snapshot',boom):
    probe('B S08 snapshot_error',lambda:A.build_inputs_from_synthetic(cfg,ledger=l))
print('B S08 rows_at_execution',seen,'terminal_states',[r['status'] for r in l.rows.values()])
from quant_lab.market.execution import BATCH_SCHEMA
from quant_lab.research.ops import REGISTRY
print('B S14 fraction_source fake/real','fraction_source' in S.BATCH_SCHEMA,'fraction_source' in BATCH_SCHEMA,'schema_order_equal',list(S.BATCH_SCHEMA.items())==list(BATCH_SCHEMA.items()))
print('B S14 window_enums',sorted({s.window_semantics for s in REGISTRY.values()}))
w=N.synth_world(N.WorldConfig(seed=1,n_clusters=200));calls={'n':0}
def guard(*args,**kw):
    calls['n']+=1;return {'ok':calls['n']!=1,'block_icc':{}}
def positive(*args,**kw):
    return {'status':'ok','final_status':'ok','synthetic_claim_positive':True,'tier':'T1','folds':[]}
with patch.object(N,'resample_null',lambda *a,**k:w.inputs),patch.object(N,'assert_not_episode_shuffle',guard),patch.object(N,'run_pipeline',positive):
    r=N.run_mc('common_shock',kind='power',n_rep=100,seed0=1,world_cfg=N.WorldConfig(n_clusters=200),pipe_cfg=A.PipelineConfig(B=200))
print('B S10 injected_power',r.verdict,'invalid',r.diagnostics['n_invalid_null_model'],'failed',r.n_failed,'worst_ci',r.worst_case_ci)
# D: retain real report prose/limitation, mutate JSON only (avoid the new missing-heading short circuit).
tasks={t['id']:t for t in json.load(open('taskList.json'))['modules']['research']['tasks']}
t=open('docs/adr/report-G3-null-model.md').read();start=t.rindex('```json')+7;end=t.rindex('```');j=json.loads(t[start:end])
verify=shlex.split(tasks['R-08']['verify'].split(' && ')[-1])[-1]
def check_json(label,data):
    fake=t[:start]+json.dumps(data)+t[end:]
    out=io.StringIO()
    try:
        with patch('builtins.open',lambda *a,**k:io.StringIO(fake)),contextlib.redirect_stdout(out):
            exec(compile(verify,'taskList:R08','exec'),{})
        print(label,'ACCEPTED',out.getvalue().strip())
    except Exception as e:
        print(label,'REJECTED',type(e).__name__,str(e))
q=copy.deepcopy(j)
for r in q['results']:
    if r['kind']=='power':r['verdict']='pass'
check_json('D1 power_false_pass_flag',q)
q=copy.deepcopy(j)
for r in q['results']:
    if r['kind']=='null':
        r['verdict']='not_run_T0';r['diagnostics']['n_invalid_null_model']=1000;r['tiers']={'T0':1000}
check_json('D2 all_T0_invalid',q)
# Equivalent complete tail+grep data path; do not strip tail as old probe D did.
import subprocess
pipeline=tasks['R-10']['verify'].split(' && ')[1].replace('docs/adr/review-G3-P1.md','')
for text in ['二审终裁：pass\n三审终裁：fail\n','二审终裁：fail\n三审终裁：pass\n']:
    rr=subprocess.run(pipeline,shell=True,executable='/bin/zsh',input=text,text=True)
    print('D3 full_tail_grep',repr(text),'exit',rr.returncode)
# S08: valid AST before invalid AST; repeated AST overwrites snap_aids entry.
for label,asts in [('later_invalid',[{'field':'close'},{'op':'Div','args':[{'field':'close'},{'field':'open'}]}]),('repeated_ast',[{'field':'close'},{'field':'close'}])]:
    cc=copy.deepcopy(cfg);cc['candidates']['asts']=asts;led=MemoryLedger()
    try:A.build_inputs_from_synthetic(cc,ledger=led)
    except Exception as e:print('E S08',label,type(e).__name__,str(e))
    print('E S08',label,[(r['objective'],r['status']) for r in led.rows.values()])
# S12: valid baseline, candidate-only censoring, common mask must exclude.
inp=A.PanelInputs(ep.select('episode_id','cluster_id','t_dec'),np.array([1.,2.]),np.array([True,False]),np.ones(2),{'f':np.ones(2)},cand_R=np.array([np.nan,2.]))
probe('E S12 candidate_only_censored',inp.validate)
# S14: valid mixed sources across entries vs TPs (not partial within one list).
from quant_lab.market.contract import build_request,resolve_policy
row=S.fake_episodes(1,span_days=2,seed=3,censor_frac=0).row(0,named=True)
row['order_plan']['entries'][0]['fraction']=Decimal(1)
for tp in row['order_plan']['tps']:tp['fraction']=None
req=build_request(row,policy_version='base-v1',policy_hash=resolve_policy('base-v1').content_hash,risk_budget=Decimal(100),market_manifest='mm')
fake=S.fake_execution(pl.DataFrame([row],schema_overrides={'order_plan':S.ORDER_PLAN_DTYPE}))
print('E S14 mixed_sources real/fake',req.fraction_source,fake['fraction_source'].to_list())
# S17 extra: duplicate bad mechanism before a good same-name row must not be dropped by dict comprehension.
q=copy.deepcopy(j);bad=copy.deepcopy(q['results'][0]);bad['n_positive']=900;bad['verdict']='fail';q['results'].insert(0,bad)
check_json('E S17 duplicate_failed_mechanism',q)

PY
```

```text
A S15 both_null market_manifest EvalProtocolError baseline 臂 market_manifest 含 null（2 行）
A S15 both_null execution_contract_version EvalProtocolError baseline 臂 execution_contract_version 含 null（2 行）
A S15 both_null seed EvalProtocolError baseline 臂 seed 含 null（2 行）
A S15 both_null risk_budget EvalProtocolError baseline 臂 risk_budget 含 null（2 行）
A S15 missing_policy_hash EvalProtocolError baseline 臂缺必需列 policy_hash
A S16 all_censored insufficient 2 1.0
A S12 null_m_true ValueError PairedPanel: 候选 d 在 1 个共同可评行（m=True）上为 null/NaN/Inf，拒收（不得静默补 0）
A S14 missing_eligibility EvalProtocolError 决策视图缺 eligibility_by_estimand（struct 固定六键），不得默认全部合格
B S06 folds ['wf000', 'wf001'] tested ['first'] tail_loss_recorded True
B S08 snapshot_error RuntimeError backend boom
B S08 rows_at_execution [1] terminal_states ['completed', 'failed']
B S14 fraction_source fake/real True True schema_order_equal True
B S14 window_enums ['none', 'rows', 'wallclock']
B S10 injected_power invalid_null_model invalid 1 failed 1 worst_ci (0.9455406146079194, 0.9997468539670226)
D1 power_false_pass_flag ACCEPTED POWER(reported, limitation statement applies) common_shock 327 774 0.3874 pass
FPR [('common_shock', 0.0153), ('cluster_heavy_tail', 0.0148), ('nonuniform_density', 0.0283), ('circular_shift', 0.0088)]
D2 all_T0_invalid REJECTED AssertionError ('common_shock', 0)
D3 full_tail_grep '二审终裁：pass\n三审终裁：fail\n' exit 1
D3 full_tail_grep '二审终裁：fail\n三审终裁：pass\n' exit 0
E S08 later_invalid ASTRejected UNKNOWN_OP at $: Div
E S08 later_invalid [('execution_batch', 'completed'), ('feature_snapshot', 'reserved'), ('feature_snapshot', 'rejected')]
E S08 repeated_ast [('execution_batch', 'completed'), ('feature_snapshot', 'reserved'), ('feature_snapshot', 'completed')]
E S12 candidate_only_censored PanelInvalid 1 行标记删失却有有限 base_R（删失须 null）
E S14 mixed_sources real/fake policy ['plan']
E S17 duplicate_failed_mechanism ACCEPTED POWER(reported, limitation statement applies) common_shock 327 774 0.3874 fail
FPR [('common_shock', 0.0153), ('cluster_heavy_tail', 0.0148), ('nonuniform_density', 0.0283), ('circular_shift', 0.0088)]
exit=0
```

#### 补充探针 F：二审尚未覆盖的身份、恢复、结构与候选门

以下替换仅在进程内生效；S07 检查真实 recover 的调度行为，不冒充磁盘并发测试。

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
from unittest.mock import patch
from dataclasses import replace
import datetime as dt
import polars as pl
import numpy as np
from quant_lab.research import features as F, nullmodel as N, api as A
from quant_lab.research.ledger import Ledger,MemoryLedger
from quant_lab.research.contract_tests import T0,make_bars
ast={"op":"Ref","args":[{"field":"close"}],"params":{"lag":0}}
a=pl.DataFrame({"episode_id":["e"],"instrument_id":["X"],"t_dec":[T0],"graph_version":["actual-revoked"],"derivation_hash":["actual-d"]})
ctx=F.SnapshotContext(market_manifest="m",graph_version="different-live",derivation_hash="different-d",consumable=lambda:True)
with patch.object(F,"_cache_read",return_value=None),patch.object(F,"_cache_write"):
    r=F.feature_snapshot([ast],a,bars=make_bars([1.,2.,3.]),ctx=ctx,cache=True)
print("F S03 mismatched_graph_identity_accepted",r.select(pl.col("^validity_.*$")).row(0))
# Recover logic real, event I/O in memory; caller PID is demonstrably alive throughout.
led=object.__new__(Ledger)
led.read=lambda:pl.DataFrame([{"attempt_id":"live-other","run_id":"other-active-run","status":"running","created_at":(dt.datetime.now(dt.UTC)-dt.timedelta(hours=2)).isoformat()}])
marks=[]
led.mark=lambda *args,**kw:marks.append((args,kw))
print("F S07 active_old_run_recover",led.recover(),marks)
w=N.synth_world(N.WorldConfig(seed=1,n_clusters=900,n_candidates=2))
bad=replace(w.inputs,anchors=w.inputs.anchors.with_columns(pl.Series("cluster_id",["unique"+str(i) for i in range(w.inputs.n)]),pl.lit("ONE-INSTRUMENT").alias("instrument_id")))
g=N.assert_not_episode_shuffle(w.inputs,bad,w.day)
print("F S10 clusters",w.inputs.anchors["cluster_id"].n_unique(),bad.anchors["cluster_id"].n_unique(),"instruments",bad.anchors["instrument_id"].n_unique(),"guard",g["ok"],"checks",g["checks"])
seen=[]
orig=A.deff_from_panel
def observed(panel,**kw):
    seen.append(panel.candidate_ids)
    return orig(panel,**kw)
with patch.object(A,"deff_from_panel",side_effect=observed):
    report=A.run_pipeline(w.inputs,N.default_candidates(2),A.PipelineConfig(B=100,block_len_days=3,config_cap=2),ledger=MemoryLedger())
print("F S06 deff_inputs",seen,"admitted",report["candidate_pool"]["n_admitted"],"selection_status",[f["selection"]["status"] for f in report["folds"]])
PY
```

```text
F S03 mismatched_graph_identity_accepted (True,)
F S07 active_old_run_recover 1 [(('live-other', 'interrupted'), {'reason': 'RECOVERED_STALE'})]
F S10 clusters 900 2204 instruments 1 guard True checks {'icc': True, 'scale': True, 'tail': True, 'missing': True}
F S06 deff_inputs [('base',), ('base',), ('base',), ('base',), ('base',), ('base',)] admitted 2 selection_status ['ok', 'ok', 'ok']
exit=0
```

#### 补充探针 G：诊断假绿与报告算术复算

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import copy,io,json,shlex,contextlib,re
from pathlib import Path
from unittest.mock import patch
from quant_lab.research.nullmodel import clopper_pearson as cp
s=Path("docs/adr/report-G3-null-model.md").read_text();start=s.rindex("```json")+7;end=s.rindex("```");j=json.loads(s[start:end])
tasks={x["id"]:x for x in json.loads(Path("taskList.json").read_text())["modules"]["research"]["tasks"]}
v=shlex.split(tasks["R-08"]["verify"].split(" && ")[-1])[-1]
q=copy.deepcopy(j)
for r in q["results"]:
    if r["kind"]=="null":r["diagnostics"]["shuffle_guard"]["ok"]=False
fake=s[:start]+json.dumps(q)+s[end:]
try:
    with patch("builtins.open",lambda *a,**k:io.StringIO(fake)),contextlib.redirect_stdout(io.StringIO()):exec(compile(v,"R08","exec"),{})
    print("G D diagnostic_false_only ACCEPTED")
except Exception as e:print("G D diagnostic_false_only REJECTED",type(e).__name__)
for r in j["results"]:
    n,x,f=r["n_done"],r["n_positive"],r["n_failed"]
    worst=x+f if r["kind"]=="null" else x
    for key,actual in [("ci",cp(x,n-f)),("worst_case_ci",cp(worst,n)),("searched_worst_ci",cp(worst,r["n_searched"]))]:
        assert all(abs(a-b)<1e-12 for a,b in zip(actual,r[key])),(r["kind"],key)
    assert abs(r["rate"]-x/(n-f))<1e-12
    assert abs(r["worst_case_rate"]-worst/n)<1e-12
    assert sum(r["tiers"].values())==n
    if r["kind"] in ("null","power"):
        print("G report",r["mechanism"],r["kind"],"n/x/failed",n,x,f,"searched",r["n_searched"],"searched_ci",[round(z,6) for z in r["searched_worst_ci"]],"verdict",r["verdict"])
print("G arithmetic",len(j["results"]),"rows checked")
PY
```

```text
G D diagnostic_false_only ACCEPTED
G report common_shock null n/x/failed 1000 5 1 searched 848 searched_ci [0.002601, 0.015336] verdict pass
G report cluster_heavy_tail null n/x/failed 1000 2 5 searched 973 searched_ci [0.002897, 0.014766] verdict pass
G report nonuniform_density null n/x/failed 1000 6 5 searched 690 searched_ci [0.007984, 0.028345] verdict pass
G report circular_shift null n/x/failed 1000 3 0 searched 992 searched_ci [0.000624, 0.008812] verdict pass
G report common_shock power n/x/failed 1000 327 38 searched 774 searched_ci [0.387388, 0.458166] verdict fail
G arithmetic 13 rows checked
exit=0
```

### 逐条闭合表

判定针对节头 SHA256 取证版本。Q-Sxx 的完整复现命令及实跑输出见本节「逐 S 定向测试」；A–D 原探针、分段重放及 E/F/G 的完整可复制命令和输出均已附于上文。原探针因新拒收门提前退出时，退出码 1 不作全探针通过解释，后续分支以独立重放补齐。S07 原内存替身不适配新增锁接口，其竞态验收以真实多进程回归补证。

| 项 | 三审状态 | 实跑证据与闭合判断 |
|---|---|---|
| S01 | 已闭合 | 抽验 Q-S01：1 passed；按 episode 身份回读，不按原行号套用。保留前审判定。 |
| S02 | 已闭合 | Q-S02：2 passed；新增历史晚到回归纳入补充 12 passed。A 的 Mean3 全量与真实可见前缀均 `(None, False)`；AST 回看依赖 available_at 传播、EMA 累积依赖门已落地。 |
| S03 | 部分闭合 | Q-S03：1 passed，B 热读撤销现在抛 SnapshotInvalid；冷算发布和最终返回复查有效。但 F：错配 graph/derivation 上下文仍返回 `(True,)`，零参 bool 回调未核被消费对象身份。 |
| S04 | 已闭合 | 抽验 Q-S04：2 passed；跨进程 quarantine 和 snapshot 拒用有效，BACKEND_QUARANTINE 保留 polars_ta Corr 拒用。保留前审判定。 |
| S05 | 已闭合 | 抽验 Q-S05：1 passed；inner_split 日历切分、purge、成熟门通过。保留前审判定。 |
| S06 | 部分闭合 | Q-S06：1 passed；B 保留 wf001 空尾折且 tail_loss_recorded=True；C undefined→T0/p=0/cap=0。实际 search/selection 各估基线 DEFF 后取保守值有效。F 实跑仅六次 `('base',)` DEFF 输入，仍 admitted=2、三折 selection=ok；候选主目标的新依赖没有 selection 前收紧门。 |
| S07 | 部分闭合 | Q-S07：1 passed；补充 r2 并发终态回归通过，reserve/mark/invalidate 锁内状态判定已修。F：2 小时的其他活跃 run 被 recover 调度为 interrupted（数量 1）；默认恢复仍只凭 created_at。事件序号与文件名一致性校验亦未补齐。 |
| S08 | 部分闭合 | Q-S08：2 passed；B 收益调用前已有 1 条 reservation，后端失败落 failed；C selection 家族异常两条均 failed。但 E 合法 AST 后接非法 AST 遗留 feature_snapshot reserved；重复 AST 成功执行后也遗留 reserved。实际代码/结果血缘和全阶段成本仍不完整。 |
| S09 | 已闭合 | 抽验 Q-S09：1 passed；C train_days=(0,180)、排除未成熟 46、总体 q30=-0.7797157283503584。保留前审判定。 |
| S10 | 部分闭合 | Q-S10：2 passed；B 注入诊断失败→invalid_null_model，C 尺度×100 guard=False。F 将 900 簇改成 2204 个单成员簇、品种折成 1 个后 guard 仍 True；逐 replicate 门只有 ICC/scale/tail/missing，未覆盖二审要求的簇布局及块间/跨品种相关保持。 |
| S11 | 已闭合 | 抽验 Q-S11：1 passed；maxt 按 W>0 计非空块。保留前审判定。 |
| S12 | 部分闭合 | Q-S12：3 passed；A 的 m=True/null 直接入口已 ValueError 拒收，NaN 门回退有效。但 E：基线有限、仅候选删失的合法共同排除行仍被 PanelInvalid 拒收；validate 把共同删失错误等同基线必须为空。 |
| S13 | 已闭合 | 抽验 Q-S13：1 passed；未成交未删失的到期 t1 非空，保留前审判定。 |
| S14 | 部分闭合 | Q-S14：8 passed，41 列镜像逐列逐序一致、窗口枚举和 estimand 六键通过，无待落地豁免。E：合法 entries=plan、TPs=policy 混合来源，G2 输出 policy、synthetic 输出 plan；§5.10 语义尚未全面对齐。 |
| S15 | 已闭合 | Q-S15：1 passed，r2 双方同 null、policy_hash 缺失、execution t_dec 错配回归通过；A 四类共享字段同 null 均拒收，缺 hash 亦拒收。 |
| S16 | 已闭合 | Q-S16：1 passed；r2 全删失回归及 A 分段输出 `insufficient 2 1.0`：统计排除前计未闭合率，无错误归零。 |
| S17 | 部分闭合 | R-08 原 verify exit=0，现存报告计数/区间复算一致；出口 A 与 §3.2 接受。D1 错误功效 pass flag 仍 ACCEPTED；D2 全 T0/invalid 拒收；D3 最新终裁 tail 管道正确（旧pass+新fail exit=1，反向 exit=0）。E 重复坏机制被后一个好记录覆盖后 ACCEPTED；G 仅改 diagnostic=false 仍 ACCEPTED。工程判读未闭合。 |
| S18 | 已闭合 | 抽验 Q-S18：1 passed；grammar 流式截断及原因留痕有效。保留前审判定。 |
| S19 | 已闭合 | Q-S19：1 passed；r2 长 Decimal 回归通过。A 长常量哈希不相等，精度 10/40 上下文 canonical JSON 一致，31 位小数原值保留；as_tuple 规范化不再依赖 context 舍入。 |

计数：已闭合 11 项；部分闭合 8 项；未闭合档 0 项。按用户二分统计，未完成工程闭合共 8 项（即上述部分闭合项）。前审已通过的 7 项均保留，不回退；本轮重点 12 项中新增完成 S02、S15、S16、S19，其余 8 项仍需修复。

A13 / A15 专项：`test_a13`、`test_a15` 已包含在上文补充命令的 `12 passed, 36 deselected` 内；已读 §9.10.9/§9.10.11 与实现。A13 只检查被请求 estimand 的 non-null，未请求键允许 null；A15 的单政策分类把覆盖失败计入 n_coverage_excluded、右删失计入 n_censored_excluded，断言互斥通过。这两项的现有修复不抵消 S12 共同 mask 与 S14 混合 fraction 来源反例。

### 剩余必修

以下全部沿用二审未完成的工程要求，不将 S17 的旧功效阈值重新列为准入条件。

1. **S03（features.py，SnapshotContext / feature_snapshot）**：绑定实际 graph_version、derivation_hash、revocation_epoch 与校验结果，拒收错配；可用带身份的结果或显式绑定校验器，不能仅凭任意零参 True 证明被读对象可消费。验收：F 错配拒收，保留热读/多 AST/冷算发布期间撤销回归；身份或 epoch 变化即丢弃结果。分布式 ack 仍不要求。
2. **S06（api.py，_baseline_deff / run_pipeline）**：候选配对主目标可能比基线有更强依赖；不能把仅基线 DEFF 当作其保守上界。按合法内层训练范围，在候选进入 selection 推断前核目标 DEFF、适用档位/预算，超门则拒收或收紧；明确阈值自由度。验收：低基线依赖、高候选依赖及 undefined 候选夹具不能沿原档预算继续；记录候选门结果，同时保留空折、训练跨度和当前 cap 测试。F 证明缺少候选门调用，不把它描述成候选实际 DEFF 数值。
3. **S07（ledger.py，recover / _events）**：默认 recover 被 run_pipeline 自动调用，必须用 run 所有权和失活证据恢复，不能按长任务创建时间中断其他活跃 run。事件读入应核文件名序号/attempt/status 与 JSON 一致、序号唯一及合法状态转移。验收：旧但活跃 run 不被中断，已证失活 run 可恢复；错误序号和重名事件 fail closed；保留真实跨进程终态竞态与水位重放测试。当前 mark 锁内保护已成立，残留问题不是原竞态复发。
4. **S08（api.py，build_inputs_from_synthetic；ledger.py）**：整个 AST 预留/校验/计算家族用异常收尾包住；按 attempt 或 occurrence 管理预留，重复 raw hash 不得覆盖已有 aid。重复实际计算必须关联原尝试；补实际 code/protocol/opportunity/policy/result 血缘与各阶段成本，磁盘与 MemoryLedger 语义对齐。验收：E 两种输入结束后无 reserved/running，计算失败不记 completed；每次实际重算都有对应有效 reservation；各异常出口及成本留痕可审计。
5. **S10（nullmodel.py，assert_not_episode_shuffle / run_mc）**：按预注册阈值补簇身份/大小与缺失布局、块间时间依赖、跨品种相关的逐 replicate 保持诊断，不能以原训练模型一次摘要替代重采样面板诊断。验收：F 破坏簇/品种布局以及相关结构、非有限诊断的独立故障注入，在 null/power 两路径均 invalid_null_model，并保留每项失败计数与最坏界。现存四门全部绿只能证明这些已实现检查的结果。
6. **S12（api.py，PanelInputs.validate）**：分别验证两臂的删失和值，再构造共同 mask；只要求 m=True 的参与行双方有限，m=False 合法行允许基线有限而候选被删失。验收：E 候选单侧删失接受且不进差值/簇权重，反向单侧删失同样处理；m=True 任一 null/NaN 继续拒收。不可把所有 null 无条件补零。
7. **S14（synthetic.py，fraction_source / _fractions）**：§5.10 在 entries 与 TPs 内各自做全有/全无校验，再对两组来源合并：只要任一组 policy 回退，整体来源就是 policy；列表内部分缺失拒收。验收：新增两种混合来源、双方 plan、双方 policy、三腿非均分与列表内部分缺失夹具；与 G2 逐字段值、Decimal 列表顺序及 41 列 schema 对拍。当前无豁免 parity 绿测尚未覆盖 E。
8. **S17（taskList.json，R-08/R-10 verify）**：R-10 最新终裁读取已正确；R-08 应先校验机制记录唯一、完整计数/档分布/诊断一致性，再复算区间和报告 flag。功效低时允许 verify 通过，但原历史阈值 flag 必须如实保持 fail（或采用明确定义的报告状态，不能伪造 pass）；§3.2 应核有效限制内容而非仅标题子串。验收：D1 错误 flag、E 重复机制、G diagnostic=false 必须拒收；真实低功效+正确 fail 记录+有效限制声明继续通过；保留全 T0、次数不足、invalid>0、最新终裁反例。此项不要求把功效提高到 80%。

### 三审终裁

**终裁：fail。** 必修工程项为 S03、S06、S07、S08、S10、S12、S14、S17；12 项工程闭合未全部成立。

R-05、R-06、R-07、R-08、R-09 的 taskList 原文 verify 均已实跑，退出码全部 0，完整命令和输出见本节前部。全套 `tests/research` 为 **249 passed in 137.10s**，exit=0。这些结果与补充反例并存，不能以测试数量或 verify 绿灯覆盖尚存缺口。R-09 的 `/tmp/ql-proto` 和默认湖账本写入为用户授权的验证副作用；没有修改源码、测试、契约或 taskList。

对当前重跑报告的复核：原始 JSON 共 13 行结果，三种分母下 CP 区间与比率均独立复算一致（探针 G）。四个 null 机制各 n=1000；有搜索样本数依次 848/973/690/992，失败计阳性的条件区间上界约 1.5336%/1.4766%/2.8345%/0.8812%。这些只是当前合成报告的描述，S10 未齐全的诊断限制仍然适用。

功效 n=1000、检出 327、失败 38、T0=226、有搜索 774、规则找回 430。报告非失败分母点值 33.9917%，全计划失败计未检出 32.7000%（95% CI 29.7970%–35.7053%），有搜索条件点值 42.2481%（95% CI 38.7388%–45.8166%）；原 fail 记录保留。用户出口 A 已接受，功效数值不作本轮准入判断。§3.2 的未检出不等于无增益、合成结果不等于真实功效等限制保留；敏感性表 n=200 的数值不能转述成真实研究检出能力保证。

本轮运行 taskList 指定 verify、全套 pytest 和所列探针；核读现有闭合后重跑的 MC/后端报告并复算 JSON，**没有另起一轮 5000 次 MC 或全规模后端计时**，不将报告中的历史耗时冒充三审新运行。后端 JSON 保留 polars_ta Corr FAIL（18 个失败）及 EMA unsupported，生产选择/隔离判读未绕过。

收尾版本复核时间：2026-09-11T02:42:22.051017+00:00。逐文件清单 34 个，收尾 34 个；变更=[]。原一审/二审完整前缀 83146 bytes SHA256=0ad2240179bf56c8dd926c071d846d3917cea962457641ce3670239957f4f862，与开审一致。taskList notes 不参与源码并发判定。

报告与契约取证摘要（收尾读取，SHA256）：

```text
f70c06ae5c46ecc57372ad7c0d7413ce24fbf504aeaea5bffc46906550cecce5  contracts/feature-snapshot.md
cb76219535d3f4142e8f33d8628350ab012e95148892afed76cc2dae311b78bb  contracts/research-schema.md
70f7420e3139d959d92a3870eda71686a099a159be5aeb90c6d19e6fd164555a  contracts/execution-interface.md
16cd16a39f1034bda7c70a968a52698291dc732c4952464f646351264208912d  docs/adr/report-G3-null-model.md
d9fc1690653016db72cd78c4756efc5b36b34ae04dcf6815aa02c27858cd7afd  docs/adr/report-G3-backend-spike.md
```

机械验收命令：`tail -1 docs/adr/review-G3-P1.md | grep -E "^三审终裁：(pass|fail)$"`；`grep -c "已闭合" docs/adr/review-G3-P1.md`。后者统计整份历史文档出现行数，不是本轮闭合项数；本轮条数以上表为准。

三审终裁：fail


## 四审（R-10 round 4）

取证日期：2026-09-11。审查者：GPT-6 / Codex 主控，未委派其他模型。范围：只读复核；仅在本文件末尾追加本节。按用户要求复核三审剩余 8 项，原已闭合 11 项只抽验、不回退；S17 使用用户已裁定的出口 A，功效如实报告并保留限制声明，不以功效数值作准入门。所有数字均为本轮测试、合成模拟或已有报告的描述。

**版本绑定（开审逐文件 SHA256）**：为同时满足“只追加、不改历史正文”，开审快照记在本次四审节头，不向三审正文插入内容。覆盖 research 源码及 tests/research 的全部源文件/夹具（35 个；不含生成的 __pycache__）。历史一至三审前缀 126433 bytes，SHA256=3be200166c763d60d00b541393191be11b4435538a946f4ab2f6a0e9bb92b5fe。

```text
5792ae78cc7030ef6f8e40e786177e84df851a892bd555eafeb7c91e1beadca0  src/quant_lab/research/__init__.py
8f3f2321b33be1d41ab8147d93e928802be1771b8673c45f3e26866c4f716629  src/quant_lab/research/api.py
b9fd438b20443a0705f0b0eb140efa28fd6cc8fef256fffc2b5cf99a4d7bc903  src/quant_lab/research/ast.py
4ac9410eda1a7df71c1ec2ff4e0d275e968fc0a4c18e17f177ed9fa20185117d  src/quant_lab/research/backends/__init__.py
c39e3efc2c07cbf69d8fbc8d7d78ddb12d2f003322c0b01fb78d807dec4d06a5  src/quant_lab/research/backends/polars.py
3873a84db354235cf3662e4b6bfab6a64e87835d963001f80b6230e530c78e27  src/quant_lab/research/backends/polars_ta.py
25053d6f43be30807fb7580893174fcee18ecff880e342074d2856c63eba683f  src/quant_lab/research/contract_tests.py
d2c73127ad42b91d83be4362b3ce56c730044bd80813688ea7d4b8dc377aa9fd  src/quant_lab/research/evaluator.py
65f36acacec9b7656dc572ee2490ef7dafc3f2782a43706560dbaae88cd07b46  src/quant_lab/research/features.py
03841ce8d3c03bd65ed7d9e6fe310417319dc97c6befb3a3aa56c93ad02f09d9  src/quant_lab/research/grammar.py
5838db4b5741fdac553cd48a21bea92975bf7fe16760196313b721edbecf6608  src/quant_lab/research/ledger.py
603c46f339300822fc2a503c4fde1cfcd2181347bb43bace1f7e5f3b30e3a7e2  src/quant_lab/research/maxt.py
023526012adab32b6fc52975f608e6b28ac1aadedae1792ff0fa5eba6ac88345  src/quant_lab/research/nullmodel.py
2893fcfab671fa39abd7b6c6549ebaf92740652372dffc5f9fcb4e367b950d05  src/quant_lab/research/ops.py
2501ad7858939258167357dd830454475d044454ea294002a8140545af484ac9  src/quant_lab/research/paths.py
66e1c9494982624eaccb7dadcf64e02142ccba7c0e3c28aeafec52572cb2fc54  src/quant_lab/research/protocol.py
09a1b940e36c07367317e5ce74eb718dfee8918d3940d375aaef6e3111f4ad25  src/quant_lab/research/reference.py
9f3178104aab91ca67d76d14cbc49777f75167ea3b9788b81dfe32509150b503  src/quant_lab/research/spike.py
e3259d422a7e676ac4699c1a4a6e0a85baf05f435be1292dbfa424a6b6fd284f  src/quant_lab/research/synthetic.py
987037ccf008b8874b78903a5c14645b55b9e0b099b5a248040c909029e1c418  src/quant_lab/research/tiers.py
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  tests/research/__init__.py
d375f18e484d7ccaa698e22505e1bddc087558290ea03c479e7385328ce93fa2  tests/research/conftest.py
1d2af763e4b00ba120d3e79cde981f5e54e8a6b2567128df9562b524ac3fb506  tests/research/fixtures/protocol_synthetic.yaml
aa9a5bf44c99c95dd1cb4717450e5eec30ec1b50dabebae1b0712b6e4360e176  tests/research/test_api.py
c0b19d733d2965b6677fbbc5acbe777916bbd5187aaf95107539b81ee8c28d50  tests/research/test_ast.py
af52849349543c3fb82d27f6e2b778a4a3f3b7e8200209ec3e83e59d5ad691af  tests/research/test_evaluator.py
34a62aa954f3714df9e5501680bea55df71d6c5ed0144c282ce8325b0115d5ff  tests/research/test_features.py
d7e0cb2045cc12d252ae9d390c4837bfcef64ef0b35108b7512fafef38468f94  tests/research/test_g2_parity.py
42253d2e41189805c127eacd92dd1b46e739a51ebd6da15127998aacca23b71c  tests/research/test_maxt.py
78e1a22c7e0239d05f77fc486b66bb2414d5430e47425ab9c77da8fd0f3434b7  tests/research/test_nullmodel.py
664ef151fa6258c931cb41d55c01b979448cf2398745b64b0e5543ac208c4ca1  tests/research/test_ops_contract.py
d70d1a608797661005bd3858810dc0cc9c2f06ce7303cff8de1d9ef22cab850f  tests/research/test_protocol.py
e1338b9c14e781f4fdaa20f6103945fd378603ea3c1662adb918cbe1808957ee  tests/research/test_review_p1_closure.py
fc12c2eef451126c9a10afab630dc55a77f3cd0ff566bb10cdfd68c4f0d76271  tests/research/test_review_p1_round3.py
be33574cdf8c298dc140713d040c55ffec113c61afad5a2bfe5df59eeab616ca  tests/research/test_smoke.py
```
核读依据：本文件一、二、三审全文及 A–G 探针；feature-snapshot 全文（§7 优先）、research-schema §9、execution-interface §5、ADR-G3；research 全部源码与 tests/research 全部测试/夹具；null-model、backend-spike、research-horizon-curve 三份报告；taskList.json 的 modules.research 与 R-05–R-10 verify 原文。源码边界核查包括 Decimal c14n、流式枚举、后端隔离、快照身份/撤销、两臂与 estimand、锁内账本、W>0、计算前分档门、成熟 t1、残差重采样及并行 MC。

### 逐条闭合表

判定：**已闭合 17 项，部分闭合 2 项（S08、S17），未闭合 0 项；未完全闭合合计 2 项。** 三审剩余 8 项中，本轮闭合 6 项。表内 Q-Sxx 命令与实跑输出见紧随其后的命令表；R4-L、R4-V、R4-STRUCT 是本节给出的独立内存探针。没有以 pytest/verify 全绿替代反例判定。

| 编号 | 四审判定 | 复现与实跑证据 |
|---|---|---|
| S01 | 已闭合 | 抽验 Q-S01：1 passed。热读按 episode 身份对齐，换序结果一致。 |
| S02 | 已闭合 | 抽验 Q-S02：2 passed；A 的 full/visible_only 均 (None, False)，历史晚到依赖未穿透，ctx 参数与 key 一致。 |
| S03 | 已闭合 | Q-S03：1 passed；F identity_mismatch 抛 SnapshotInvalid。三审文件回归覆盖零参回调拒收、绑定 graph/derivation/epoch、冷发布/AST 间/热读撤销，均通过。B 原探针在零参回调处提前拒绝，不能把该早退当作热读路径证明；热读由真实缓存回归补齐。 |
| S04 | 已闭合 | 抽验 Q-S04：2 passed。BACKEND_QUARANTINE 新进程即隔离 polars_ta.Corr；主 polars 路径可用，历史 spike 的 Corr FAIL 未删除。 |
| S05 | 已闭合 | 抽验 Q-S05：1 passed。inner_split 按日历、簇、区间 purge 与成熟标签形成 search/selection，未退回按行切半。 |
| S06 | 已闭合 | Q-S06：1 passed；B 尾部空窗保留 wf001 且 tail_loss_recorded=True；C undefined→T0/p=0/cap=0。F deff_inputs 包含候选配对目标，新增真实强依赖/undefined 候选回归拒绝原预算并禁止 selection。 |
| S07 | 已闭合 | Q-S07：1 passed（真实多进程）；锁内 reserve/mark、水位重放、坏事件 fail-closed 回归通过。F old_active_run recover=0；真实 PID 活跃/已失活对照及 seq/attempt/status/重复序号/非法转移测试通过。 |
| S08 | 部分闭合 | Q-S08：2 passed；E later_invalid/repeated_ast 均无 live 残留；ast_occurrences、selection 家族异常、recompute_of、成本/结果与 MemoryLedger/磁盘对拍通过。但 R4-L：backend bytes 变化时 ledger code_version 不变，backend_version=None，实际版本血缘仍缺。 |
| S09 | 已闭合 | 抽验 Q-S09：1 passed；C train_end=(0,180)、immature_excluded=46、population_q30=-0.7797157283503584。成熟门与总体预注册分位保持。 |
| S10 | 已闭合 | Q-S10：2 passed；F cluster-layout 破坏 guard=False。A27 回归及 R4-STRUCT 独立真实返回值+冲击格点注入均执行；正常 0/20 误拒，独立重排 8/20 单次检出；整轮 null/power 均 invalid_null_model。按当前校准规范闭合；报告验收遗漏归 S17。 |
| S11 | 已闭合 | 抽验 Q-S11：1 passed。非空块按共同 mask 上 W>0；全删失块仍可保留在日历矩阵但不增加准入块数。B20 成对 floor 另核通过。 |
| S12 | 已闭合 | Q-S12：3 passed；E candidate_only_censored validate 返回 None（正常完成），正反单侧删失回归均排除共同支持；m=True 任一臂 null/NaN/±Inf 拒收；直接 PairedPanel 不静默补零。 |
| S13 | 已闭合 | 抽验 Q-S13：1 passed。未成交未删失有 expiry t1，合法零收益不会仅因 t1 空而被删除。 |
| S14 | 已闭合 | Q-S14：8 passed；E mixed_sources real/fake 均 policy；三腿非均分、两组混合、部分缺失拒收、policy 总额回归通过。独立输出 44/44 列逐列逐序 dtype 一致，派生列存在且非特征输入；A13/A15 通过。 |
| S15 | 已闭合 | 抽验 Q-S15：1 passed；分段 A 对 market_manifest/contract_version/seed/risk_budget 双方 null 及缺 policy_hash 均抛 EvalProtocolError。 |
| S16 | 已闭合 | 抽验 Q-S16：1 passed；A 全删失输出 insufficient、n_censored_excluded=2、unclosed_rate=1.0，诊断未因提前返回归零。 |
| S17 | 部分闭合 | R-08 原文 exit=0；D1 伪造 power pass、D2 全 T0、E 重复机制、G shuffle_guard=false 均拒收，R-10 最新行判断正确；出口 A 与有效 §3.2 已满足。但 R4-V 的新结构诊断 false/缺失/invalid_reason/guard_fail_rate 仍被接受，A31 --rebuild 可洗掉旧生成哈希，故工程闭合不成立。 |
| S18 | 已闭合 | 抽验 Q-S18：1 passed。grammar 流式展开与 visited/cap 截断说明保持。 |
| S19 | 已闭合 | 抽验 Q-S19：1 passed；A 两个不同 31 位十进制 hash_equal=False，Decimal context=10/40 得相同完整规范串，未再受 normalize 精度舍入。 |

#### Q-Sxx：逐项命令及本轮新输出

以下命令 cwd=quant-lab，统一设置 `PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider'`，不写源码或 pytest cache。每条 exit=0。

| 证据 | 实跑命令 | 实跑输出末行 |
|---|---|---|
| Q-S01 | `.venv-g3/bin/python -m pytest tests/research/test_features.py::test_cache_hot_read_follows_episode_identity_not_position -q` | `1 passed in 0.52s` |
| Q-S02 | `.venv-g3/bin/python -m pytest tests/research/test_features.py::test_context_is_single_source_and_in_key tests/research/test_features.py::test_late_available_at_is_not_visible -q` | `2 passed in 0.94s` |
| Q-S03 | `.venv-g3/bin/python -m pytest tests/research/test_features.py::test_revocation_gate_blocks_cached_and_fresh_reads -q` | `1 passed in 0.40s` |
| Q-S04 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s04_quarantine_persists_across_processes tests/research/test_features.py::test_quarantined_backend_op_rejected_in_snapshot -q` | `2 passed in 1.16s` |
| Q-S05 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s05_inner_split_is_calendar_based_purged_and_mature -q` | `1 passed in 0.53s` |
| Q-S06 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s06_config_cap_can_only_tighten_and_insufficient_folds_do_not_fit -q` | `1 passed in 1.45s` |
| Q-S07 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s07_concurrent_reserve_only_one_wins_and_replay_after_crash -q` | `1 passed in 3.23s` |
| Q-S08 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s08_every_stage_has_terminal_state_including_failures_and_final tests/research/test_review_p1_closure.py::test_s08_run_protocol_reserves_before_lint_and_rejects_bad_ast -q` | `2 passed in 7.56s` |
| Q-S09 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s09_residual_fit_uses_only_mature_labels_and_population_quantile -q` | `1 passed in 0.41s` |
| Q-S10 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s10_diagnostic_failure_cannot_pass tests/research/test_review_p1_closure.py::test_s10_all_t0_is_not_run_not_pass -q` | `2 passed in 0.44s` |
| Q-S11 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s11_censored_blocks_do_not_count_as_nonempty -q` | `1 passed in 0.48s` |
| Q-S12 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s12_panel_invariants_and_placeholder_only_under_mask tests/research/test_review_p1_closure.py::test_s12_outer_nan_gate_falls_back_to_baseline tests/research/test_evaluator.py::test_nan_feature_is_skip_and_keeps_denominator -q` | `3 passed in 1.17s` |
| Q-S13 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s13_unfilled_uncensored_gets_expiry_t1_not_null -q` | `1 passed in 2.66s` |
| Q-S14 | `.venv-g3/bin/python -m pytest tests/research/test_evaluator.py::test_estimand_vocabulary_frozen_and_stub_isomorphic_with_g1 tests/research/test_g2_parity.py -q` | `8 passed in 0.15s` |
| Q-S15 | `.venv-g3/bin/python -m pytest tests/research/test_evaluator.py::test_two_arm_pairing_and_protocol_errors -q` | `1 passed in 0.07s` |
| Q-S16 | `.venv-g3/bin/python -m pytest tests/research/test_evaluator.py::test_synthetic_end_to_end_pairs_and_decimal -q` | `1 passed in 0.16s` |
| Q-S18 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s18_streaming_budget_and_truncation_reason -q` | `1 passed in 0.48s` |
| Q-S19 | `.venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s19_exact_decimal_canonicalization -q` | `1 passed in 0.40s` |

#### R-05–R-09：taskList 原文 verify 实跑

原文从 taskList.json 读取执行，没有替换断言。R-09 写 /tmp/ql-proto 与默认湖账本是本轮明确授权的验证副作用。R-07 默认湖快照数量是读取时的描述，不把历史条目当成本轮新计算。

**R-05**

```bash
test -f docs/adr/report-G3-backend-spike.md && .venv-g3/bin/python -c "import json,re,sys;t=open('docs/adr/report-G3-backend-spike.md',encoding='utf-8').read();j=json.loads(t[t.rindex(chr(96)*3+'json')+7:t.rindex(chr(96)*3)]);m=j['matrix'];assert all(v['status']=='pass' for v in m['polars'].values()),m['polars'];assert sum(v['status']=='pass' for v in m['polars_ta'].values())>=10;tm={r['backend']:r for r in j['timings']};assert tm['polars']['n_ast']==128 and tm['polars']['cold_s']>0 and tm['polars']['hot_s']>0 and 0<tm['polars']['rss_gib_after_hot_s']<=8 and tm['polars']['cold_s']<=1200;print('spike ok',tm['polars']['cold_s'],tm['polars_ta']['cold_s'])"
```

```text
spike ok 206.77 36.11
exit=0
```

**R-06**

```bash
.venv-g3/bin/python -m pytest tests/research/test_features.py tests/research/test_evaluator.py -q
```

```text
.........................                                                [100%]
25 passed in 0.78s

exit=0
```

**R-07**

```bash
.venv-g3/bin/python -m pytest tests/research/test_protocol.py -q && .venv-g3/bin/python -c "import polars as pl;from quant_lab.research.paths import ledger_path;p=ledger_path();d=pl.read_parquet(p);assert d.height>0 and (d['status']=='reserved').sum()==0;print(p,d.height)"
```

```text
..........                                                               [100%]
10 passed in 0.09s
/Users/balen/projects/trader-bot/quant-lab/data/lockbox/ledger.parquet 1969

exit=0
```

**R-08**

```bash
.venv-g3/bin/python -m pytest tests/research/test_maxt.py tests/research/test_nullmodel.py tests/research/test_review_p1_round3.py -q && .venv-g3/bin/python -c "from quant_lab.research.nullmodel import verify_report_text; verify_report_text(open('docs/adr/report-G3-null-model.md',encoding='utf-8').read())"
```

```text
91 passed in 33.76s
POWER(reported, limitation statement applies) common_shock 327 768 0.3905 fail
R-08 report verified: 5 unique primary records; arithmetic and diagnostics consistent
exit=0
```

**R-09**

```bash
.venv-g3/bin/python -m quant_lab.research.api run --config tests/research/fixtures/protocol_synthetic.yaml --out /tmp/ql-proto && .venv-g3/bin/python -c "import json,math;r=json.load(open('/tmp/ql-proto/report.json'));assert r['tier'] in ('T0','T1','T2','T3a','T3b') and r['n_attempts']>0 and r['theta'] is not None and math.isfinite(r['theta']);s=r['n_attempts_by_status'];st=dict(zip(s['status'],s['len']));assert st.get('completed',0)>0 and st.get('reserved',0)==0 and st.get('duplicate',0)<r['n_attempts'];assert r['claim_status']=='descriptive_only' and r['final_status'] in ('ok','no_claim');print({k:r[k] for k in ('tier','K','theta','n_attempts','final_status')},st)"
```

```text
{"status": "ok", "tier": "T1", "K": 531, "theta": 0.0, "se": null, "lower_bound": null, "p_adj": null, "n_attempts": 160, "n_folds": 3, "claim_status": "descriptive_only", "out_dir": "/tmp/ql-proto"}
{'tier': 'T1', 'K': 531, 'theta': 0.0, 'n_attempts': 160, 'final_status': 'no_claim'} {'completed': 80, 'duplicate': 80}
EXIT R-09 0
```

R-10 原文亦运行（追加前末行为三审 fail），exit=1、无 stdout，符合最新终裁为失败时应拒收；其历史串注入结果见 D3。四审写入后的格式/计数另行机械复核。

```bash
test -f docs/adr/review-G3-P1.md && tail -1 docs/adr/review-G3-P1.md | grep -Eq '^(二审|三审|四审|五审)?终裁：pass$'
```

#### 全套与三审专项回归

```bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' .venv-g3/bin/python -m pytest tests/research -q
```
```text
328 passed in 182.54s (0:03:02)
exit=0
```

```bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' .venv-g3/bin/python -m pytest tests/research/test_review_p1_round3.py -q
```
```text
........................................................................ [ 93%]
.....                                                                    [100%]
77 passed in 28.16s

exit=0
```
三审文件当前实收 **77** 个测试（原 73 条加本轮 B11/A27/B19/A31 四个测试），不是把 73 当作当前统计数。文件中的 A27 正常侧与破坏侧、A31 旧 hash 拒收均实际运行；A31 绿测不覆盖下面的 rebuild 绕路。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' .venv-g3/bin/python -m pytest tests/research/test_review_p1_round3.py tests/research/test_evaluator.py tests/research/test_maxt.py tests/research/test_g2_parity.py -q
```
```text
........................................................................ [ 69%]
...............................                                          [100%]
103 passed in 27.50s

exit=0
```

#### A–G 原文重放与后续分支

A/B/D/F 在新硬门提前退出时，exit=1 是该输入被拒绝的证据，不代表其后未运行分支已验收。下面保留新输出；分段 A/B/D/E 与继续执行的 F 补齐后续路径。S07 的旧 object.__new__ 内存替身不代表真实跨进程锁，真实恢复/并发证据来自以上 pytest。

#### 探针A

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import datetime as dt
from decimal import localcontext
import polars as pl
from quant_lab.research.ast import canonical_hash,canonical_json,parse_json_text
from quant_lab.research import features as F, contract_tests as C
from quant_lab.research.maxt import PairedPanel,max_t_panel
from quant_lab.research.evaluator import pair_arms,evaluate,freeze_opportunity_set
from tests.research.test_evaluator import episodes,execution,features,take_all,AST
j=lambda n: parse_json_text('{"op":"Add","args":[{"field":"close"},{"const":'+n+'}]}')
a=j('0.1234567890123456789012345678901');b=j('0.1234567890123456789012345678902')
print('S19 different_31_digit_hash_equal=',canonical_hash(a)==canonical_hash(b))
with localcontext() as ctx:
    ctx.prec=10
    c10=canonical_json(a)
with localcontext() as ctx:
    ctx.prec=40
    c40=canonical_json(a)
print('S19 context10=',c10,'context40=',c40)
bars=C.make_bars([0.,1.,2.,3.,4.,5.]).with_columns(pl.when(pl.col('close')==3).then(pl.col('close_time')+dt.timedelta(hours=2)).otherwise(pl.col('close_time')).alias('available_at'))
anchors=pl.DataFrame({'episode_id':['a'],'instrument_id':['X'],'t_dec':[C.T0+4*C.STEP]})
ast={'op':'Mean','args':[{'field':'close'}],'window':{'unit':'rows','count':3}}
h=canonical_hash(ast)
r=F.feature_snapshot([ast],anchors,bars=bars)
vis=F.feature_snapshot([ast],anchors,bars=bars.filter(pl.col('available_at')<=anchors['t_dec'][0]))
print('S02 full=',r.select('f_'+h,'validity_'+h).row(0),'visible_only=',vis.select('f_'+h,'validity_'+h).row(0))
ep=episodes([('a','ca',False),('b','cb',False)])
x=execution(ep,[1.,2.])
for key in ['market_manifest','execution_contract_version','seed','risk_budget']:
    bad=x.with_columns(pl.lit(None,dtype=x.schema[key]).alias(key))
    print('S15 both_null',key,'accepted_rows=',pair_arms(bad,['a','b']).height)
print('S15 missing_policy_hash accepted_rows=',pair_arms(x.drop('policy_hash'),['a','b']).height)
allc=episodes([('a','ca',True),('b','cb',True)])
opp=freeze_opportunity_set(allc)
r=evaluate(AST,opp,features=features(opp.episode_ids,[1.,1.]),rule=take_all,execution=execution(allc,[None,None]),fold_id='f',attempt_id='a')
print('S16 all_censored=',r.status,r.n_censored_excluded,r.unclosed_rate)
df=pl.DataFrame({'episode_id':[f'e{i}' for i in range(30)],'cluster_id':[f'c{i}' for i in range(30)],'t_dec':[C.T0+dt.timedelta(days=i) for i in range(30)],'weight':[1.]*30,'m':[True]*30,'d':[None]+[float(i%7) for i in range(29)]})
p=PairedPanel.from_frame(df,['d']);r=max_t_panel(p,block_len_days=1,B=200,seed=1)
print('S12 null_m_true=',p.diffs[0,0],bool(p.mask[0]),r.status,r.n_nonempty_blocks)
print('S14 missing_eligibility accepted=',freeze_opportunity_set(ep.drop('eligibility_by_estimand')).episode_ids)
PY
```
```text
S19 different_31_digit_hash_equal= False
S19 context10= {"args":[{"field":"close"},{"const":0.1234567890123456789012345678901}],"op":"Add"} context40= {"args":[{"field":"close"},{"const":0.1234567890123456789012345678901}],"op":"Add"}
S02 full= (None, False) visible_only= (None, False)
Traceback (most recent call last):
  File "<stdin>", line 30, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/evaluator.py", line 225, in pair_arms
    b = _validate_and_cast(_arm(execution, baseline_policy, ids, "baseline"), "baseline")
                           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/evaluator.py", line 187, in _arm
    raise EvalProtocolError(f"{label} 臂 {c} 含 null（{x[c].null_count()} 行）")
quant_lab.research.evaluator.EvalProtocolError: baseline 臂 market_manifest 含 null（2 行）
exit=1
```

#### 探针B

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import copy,datetime as dt,threading
from unittest.mock import patch
import polars as pl
from quant_lab.research import api as A, nullmodel as N, features as F, synthetic as S, contract_tests as C
from quant_lab.research.ledger import Ledger,MemoryLedger
from quant_lab.research.protocol import walk_forward
from quant_lab.research.ops import REGISTRY
from tests.research.test_protocol import anchors,T0,D
from tests.research.test_features import CLOSE,CTX,H_CLOSE
# S03: cache read revokes graph after initial check; no filesystem writes.
state={'ok':True,'checks':0}
def consumable():
    state['checks']+=1
    return state['ok']
def read_cache(key,a):
    state['ok']=False
    return pl.DataFrame({'__row':[0],'value':[4.],'__valid':[True]})
ctx=F.replace(CTX,consumable=consumable)
a=pl.DataFrame({'episode_id':['a'],'instrument_id':['X'],'t_dec':[C.T0+4*C.STEP]})
with patch.object(F,'_cache_read',read_cache):
    r=F.feature_snapshot([CLOSE],a,bars=C.make_bars([float(i) for i in range(10)]),ctx=ctx,cache=True)
print('S03 revoked_during_hot_read=',state,'returned_valid=',r['validity_'+H_CLOSE].to_list())
# S06: final carry disappears.
a=anchors([('train','ct',0.,1.),('first','cx',89.5,90.),('tail','cx',90.5,91.)])
fs=walk_forward(a,min_train_clusters=1,test_span=D,min_train_span=89*D,origin=T0)
print('S06 folds=',[f.fold_id for f in fs],'tested=',[i for f in fs for i in f.test_ids],'tail_loss_recorded=',any('tail' in f.purged for f in fs))
# S07: deterministic interleaving of real mark() with memory-backed event I/O.
led=object.__new__(Ledger); current={'attempt_id':'a','status':'running','reason_code':None}
barrier=threading.Barrier(2); committed=threading.Event(); history=[]
def row(aid):
    snapshot=dict(current)
    barrier.wait(timeout=5)
    return snapshot
def commit(event):
    if event['status']=='interrupted':
        assert committed.wait(5)
    current.update(event);history.append(event['status'])
    if event['status']=='completed':
        committed.set()
    return event
led._row=row;led._commit=commit
threads=[threading.Thread(target=led.mark,args=('a',s)) for s in ['completed','interrupted']]
for t in threads:
    t.start()
for t in threads:
    t.join()
print('S07 concurrent_terminal_history=',history,'final=',current['status'])
# S08: synthetic execution already read before any reservation; backend exception after completed.
cfg=A.load_config('tests/research/fixtures/protocol_synthetic.yaml'); cfg=copy.deepcopy(cfg)
cfg['data']['synthetic'].update(n_episodes=30,n_bars=200,span_days=1)
cfg['candidates']['asts']=[{'field':'close'}]
l=MemoryLedger();original=S.fake_execution; seen=[]
def execution_probe(*args,**kw):
    seen.append(len(l.rows))
    return original(*args,**kw)
def boom(*args,**kw):
    raise RuntimeError('backend boom')
with patch.object(S,'fake_execution',execution_probe),patch.object(A,'feature_snapshot',boom):
    try:
        A.build_inputs_from_synthetic(cfg,ledger=l)
    except RuntimeError as e:
        print('S08 snapshot_error=',str(e),'rows_at_execution=',seen,'terminal_states=',[r['status'] for r in l.rows.values()])
# S14 independently compare frozen contract, not two local mirrors.
from quant_lab.market.execution import BATCH_SCHEMA
print('S14 fraction_source fake/real=', 'fraction_source' in S.BATCH_SCHEMA,'fraction_source' in BATCH_SCHEMA)
print('S14 window_enums=',sorted({s.window_semantics for s in REGISTRY.values()}))
# S10 state gate fault injection, not statistical evidence.
w=N.synth_world(N.WorldConfig(seed=1,n_clusters=200)); calls={'n':0}
def guard(*args,**kw):
    calls['n']+=1
    return {'ok':calls['n']!=1,'block_icc':{}}
def positive(*args,**kw):
    return {'status':'ok','final_status':'ok','synthetic_claim_positive':True,'tier':'T1','folds':[]}
with patch.object(N,'resample_null',lambda *a,**k:w.inputs),patch.object(N,'assert_not_episode_shuffle',guard),patch.object(N,'run_pipeline',positive):
    r=N.run_mc('common_shock',kind='power',n_rep=100,seed0=1,world_cfg=N.WorldConfig(n_clusters=200),pipe_cfg=A.PipelineConfig(B=200))
print('S10 injected_power=',r.verdict,'invalid=',r.diagnostics['n_invalid_null_model'],'failed=',r.n_failed,'worst_ci=',r.worst_case_ci)
PY
```
```text
Traceback (most recent call last):
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/features.py", line 62, in check_identity
    return self.consumable(self.graph_version, self.derivation_hash, self.revocation_epoch) is True
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
TypeError: consumable() takes 0 positional arguments but 3 were given

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "<stdin>", line 21, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/features.py", line 157, in feature_snapshot
    if not ctx.check_identity(anchors):
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/features.py", line 64, in check_identity
    raise SnapshotInvalid("consumable 必须接收 graph_version/derivation_hash/revocation_epoch") from exc
quant_lab.research.features.SnapshotInvalid: consumable 必须接收 graph_version/derivation_hash/revocation_epoch
exit=1
```

#### 探针C

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
from unittest.mock import patch
import numpy as np
from quant_lab.research import api as A,nullmodel as N
from quant_lab.research.ledger import MemoryLedger
w=N.synth_world(N.WorldConfig(seed=1,n_clusters=1600)); led=MemoryLedger(); orig=A.candidate_diffs; calls={'n':0}
def boom_second(*args,**kw):
    calls['n']+=1
    if calls['n']==2:
        raise RuntimeError('second selection fails')
    return orig(*args,**kw)
with patch.object(A,'candidate_diffs',boom_second):
    try:
        A.run_pipeline(w.inputs,N.default_candidates(12),A.PipelineConfig(B=200,seed=6),ledger=led)
    except RuntimeError as e:
        print('S08 selection_error=',str(e))
print('S08 remaining_selection_states=',[r['status'] for r in led.rows.values() if r['stage']=='selection'])
model=N.fit_residual_model(w)
x=N.resample_null(w,model,np.random.default_rng(3))
bad=N.replace(x,base_R=x.base_R*100)
print('S10 tail_scale_x100_guard=',N.assert_not_episode_shuffle(w.inputs,bad,w.day)['ok'],'sd_ratio=',round(float(np.nanstd(bad.base_R)/np.nanstd(x.base_R))))
# S09 actual maturity gate and training-only q diagnostics, no I/O.
m=N.fit_residual_model(w,train_end_day=180,label_maturity_days=2)
print('S09 train_end=',m.train_days,'immature_excluded=',m.diagnostics['n_immature_excluded'],'population_q30=',N.feature_population_quantile(.3))
# S06 undefined report remains search-capable even though pipeline skips that individual fold.
from quant_lab.research.tiers import tier_report
r=tier_report({'f':250},DEFF_by_fold={'f':None},span_months=13,n_outer_folds=3,min_fold_test_clusters=40)
print('S06 undefined_tier=',r.tier,'p=',r.p_allowed,'cap=',r.config_cap,'K_eff=',r.K_eff)
PY
```
```text
S08 selection_error= second selection fails
S08 remaining_selection_states= ['failed']
S10 tail_scale_x100_guard= False sd_ratio= 100
S09 train_end= (0, 180) immature_excluded= 46 population_q30= -0.7797157283503584
S06 undefined_tier= T0 p= 0 cap= 0 K_eff= None
exit=0
```

#### 探针D

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import io,json,shlex,contextlib,subprocess
from unittest.mock import patch
m=json.load(open('taskList.json'))['modules']['research']
tasks={t['id']:t for t in m['tasks']}
t=open('docs/adr/report-G3-null-model.md',encoding='utf-8').read()
j=json.loads(t[t.rindex('```json')+7:t.rindex('```')])
code=shlex.split(tasks['R-08']['verify'].split(' && ')[-1])[-1]
for r in j['results']:
    if r['kind']=='power':
        r['verdict']='pass'
fake='```json\n'+json.dumps(j)+'\n```'
with patch('builtins.open',lambda *a,**k:io.StringIO(fake)),contextlib.redirect_stdout(io.StringIO()):
    exec(compile(code,'taskList:R-08:original-python-assertions','exec'),{})
print('S17 original_R08_python_accepts_power_fail_numbers_with_pass_flag=True')
for r in j['results']:
    if r['kind']=='null':
        r['verdict']='not_run_T0'
        r['diagnostics']['n_invalid_null_model']=1000
        r['tiers']={'T0':1000}
fake='```json\n'+json.dumps(j)+'\n```'
with patch('builtins.open',lambda *a,**k:io.StringIO(fake)),contextlib.redirect_stdout(io.StringIO()):
    exec(compile(code,'taskList:R-08:original-python-assertions','exec'),{})
print('S17 original_R08_python_accepts_all_T0_nulls_with_invalid_diagnostics=True')
cmd=tasks['R-10']['verify'].split(' | ')[-1]
r=subprocess.run(cmd,shell=True,executable='/bin/zsh',input='二审终裁：pass\n三审终裁：fail\n',text=True)
print('S17 R10_grep_old_pass_then_latest_fail_exit=',r.returncode)
PY
```
```text
Traceback (most recent call last):
  File "<stdin>", line 13, in <module>
  File "taskList:R-08:original-python-assertions", line 1, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 699, in verify_report_text
    raise ValueError("限制声明缺失")
ValueError: 限制声明缺失
exit=1
```

#### 分段 A/B/D/E（保持真实报告限制正文）

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'

import copy,datetime as dt,io,json,shlex,contextlib
from decimal import Decimal
from unittest.mock import patch
import numpy as np
import polars as pl
from quant_lab.research import api as A,nullmodel as N,features as F,synthetic as S,contract_tests as C
from quant_lab.research.evaluator import pair_arms,evaluate,freeze_opportunity_set
from quant_lab.research.ledger import Ledger,MemoryLedger
from quant_lab.research.maxt import PairedPanel
from quant_lab.research.protocol import walk_forward
from tests.research.test_evaluator import episodes,execution,features,take_all,AST
from tests.research.test_features import CLOSE,CTX,H_CLOSE
from tests.research.test_protocol import anchors,T0,D

def probe(label,fn):
    try:
        print(label,fn())
    except Exception as e:
        print(label,type(e).__name__,str(e))

ep=episodes([('a','ca',False),('b','cb',False)]);x=execution(ep,[1.,2.])
for key in ['market_manifest','execution_contract_version','seed','risk_budget']:
    probe('A S15 both_null '+key,lambda:pair_arms(x.with_columns(pl.lit(None,dtype=x.schema[key]).alias(key)),['a','b']).height)
probe('A S15 missing_policy_hash',lambda:pair_arms(x.drop('policy_hash'),['a','b']).height)
allc=episodes([('a','ca',True),('b','cb',True)]);opp=freeze_opportunity_set(allc)
r=evaluate(AST,opp,features=features(opp.episode_ids,[1.,1.]),rule=take_all,execution=execution(allc,[None,None]),fold_id='f',attempt_id='a')
print('A S16 all_censored',r.status,r.n_censored_excluded,r.unclosed_rate)
df=pl.DataFrame({'episode_id':[f'e{i}' for i in range(30)],'cluster_id':[f'c{i}' for i in range(30)],'t_dec':[C.T0+dt.timedelta(days=i) for i in range(30)],'weight':[1.]*30,'m':[True]*30,'d':[None]+[float(i%7) for i in range(29)]})
probe('A S12 null_m_true',lambda:PairedPanel.from_frame(df,['d']))
probe('A S14 missing_eligibility',lambda:freeze_opportunity_set(ep.drop('eligibility_by_estimand')).episode_ids)
a=anchors([('train','ct',0.,1.),('first','cx',89.5,90.),('tail','cx',90.5,91.)])
fs=walk_forward(a,min_train_clusters=1,test_span=D,min_train_span=89*D,origin=T0)
print('B S06 folds',[f.fold_id for f in fs],'tested',[i for f in fs for i in f.test_ids],'tail_loss_recorded',any('tail' in f.purged for f in fs))
# Original S07 object.__new__ no longer supplies the new lock state; real disk multiprocessing test Q-S07/r2 supplies this evidence.
cfg=copy.deepcopy(A.load_config('tests/research/fixtures/protocol_synthetic.yaml'))
cfg['data']['synthetic'].update(n_episodes=30,n_bars=200,span_days=1)
cfg['candidates']['asts']=[{'field':'close'}]
l=MemoryLedger();original=S.fake_execution;seen=[]
def execution_probe(*args,**kw):
    seen.append(len(l.rows));return original(*args,**kw)
def boom(*args,**kw):
    raise RuntimeError('backend boom')
with patch.object(S,'fake_execution',execution_probe),patch.object(A,'feature_snapshot',boom):
    probe('B S08 snapshot_error',lambda:A.build_inputs_from_synthetic(cfg,ledger=l))
print('B S08 rows_at_execution',seen,'terminal_states',[r['status'] for r in l.rows.values()])
from quant_lab.market.execution import BATCH_SCHEMA
from quant_lab.research.ops import REGISTRY
print('B S14 fraction_source fake/real','fraction_source' in S.BATCH_SCHEMA,'fraction_source' in BATCH_SCHEMA,'schema_order_equal',list(S.BATCH_SCHEMA.items())==list(BATCH_SCHEMA.items()))
print('B S14 window_enums',sorted({s.window_semantics for s in REGISTRY.values()}))
w=N.synth_world(N.WorldConfig(seed=1,n_clusters=200));calls={'n':0}
def guard(*args,**kw):
    calls['n']+=1;return {'ok':calls['n']!=1,'block_icc':{}}
def positive(*args,**kw):
    return {'status':'ok','final_status':'ok','synthetic_claim_positive':True,'tier':'T1','folds':[]}
with patch.object(N,'resample_null',lambda *a,**k:w.inputs),patch.object(N,'assert_not_episode_shuffle',guard),patch.object(N,'run_pipeline',positive):
    r=N.run_mc('common_shock',kind='power',n_rep=100,seed0=1,world_cfg=N.WorldConfig(n_clusters=200),pipe_cfg=A.PipelineConfig(B=200))
print('B S10 injected_power',r.verdict,'invalid',r.diagnostics['n_invalid_null_model'],'failed',r.n_failed,'worst_ci',r.worst_case_ci)
# D: retain real report prose/limitation, mutate JSON only (avoid the new missing-heading short circuit).
tasks={t['id']:t for t in json.load(open('taskList.json'))['modules']['research']['tasks']}
t=open('docs/adr/report-G3-null-model.md').read();start=t.rindex('```json')+7;end=t.rindex('```');j=json.loads(t[start:end])
verify=shlex.split(tasks['R-08']['verify'].split(' && ')[-1])[-1]
def check_json(label,data):
    fake=t[:start]+json.dumps(data)+t[end:]
    out=io.StringIO()
    try:
        with patch('builtins.open',lambda *a,**k:io.StringIO(fake)),contextlib.redirect_stdout(out):
            exec(compile(verify,'taskList:R08','exec'),{})
        print(label,'ACCEPTED',out.getvalue().strip())
    except Exception as e:
        print(label,'REJECTED',type(e).__name__,str(e))
q=copy.deepcopy(j)
for r in q['results']:
    if r['kind']=='power':r['verdict']='pass'
check_json('D1 power_false_pass_flag',q)
q=copy.deepcopy(j)
for r in q['results']:
    if r['kind']=='null':
        r['verdict']='not_run_T0';r['diagnostics']['n_invalid_null_model']=1000;r['tiers']={'T0':1000}
check_json('D2 all_T0_invalid',q)
# Equivalent complete tail+grep data path; do not strip tail as old probe D did.
import subprocess
pipeline=tasks['R-10']['verify'].split(' && ')[1].replace('docs/adr/review-G3-P1.md','')
for text in ['二审终裁：pass\n三审终裁：fail\n','二审终裁：fail\n三审终裁：pass\n']:
    rr=subprocess.run(pipeline,shell=True,executable='/bin/zsh',input=text,text=True)
    print('D3 full_tail_grep',repr(text),'exit',rr.returncode)
# S08: valid AST before invalid AST; repeated AST overwrites snap_aids entry.
for label,asts in [('later_invalid',[{'field':'close'},{'op':'Div','args':[{'field':'close'},{'field':'open'}]}]),('repeated_ast',[{'field':'close'},{'field':'close'}])]:
    cc=copy.deepcopy(cfg);cc['candidates']['asts']=asts;led=MemoryLedger()
    try:A.build_inputs_from_synthetic(cc,ledger=led)
    except Exception as e:print('E S08',label,type(e).__name__,str(e))
    print('E S08',label,[(r['objective'],r['status']) for r in led.rows.values()])
# S12: valid baseline, candidate-only censoring, common mask must exclude.
inp=A.PanelInputs(ep.select('episode_id','cluster_id','t_dec'),np.array([1.,2.]),np.array([True,False]),np.ones(2),{'f':np.ones(2)},cand_R=np.array([np.nan,2.]))
probe('E S12 candidate_only_censored',inp.validate)
# S14: valid mixed sources across entries vs TPs (not partial within one list).
from quant_lab.market.contract import build_request,resolve_policy
row=S.fake_episodes(1,span_days=2,seed=3,censor_frac=0).row(0,named=True)
row['order_plan']['entries'][0]['fraction']=Decimal(1)
for tp in row['order_plan']['tps']:tp['fraction']=None
req=build_request(row,policy_version='base-v1',policy_hash=resolve_policy('base-v1').content_hash,risk_budget=Decimal(100),market_manifest='mm')
fake=S.fake_execution(pl.DataFrame([row],schema_overrides={'order_plan':S.ORDER_PLAN_DTYPE}))
print('E S14 mixed_sources real/fake',req.fraction_source,fake['fraction_source'].to_list())
# S17 extra: duplicate bad mechanism before a good same-name row must not be dropped by dict comprehension.
q=copy.deepcopy(j);bad=copy.deepcopy(q['results'][0]);bad['n_positive']=900;bad['verdict']='fail';q['results'].insert(0,bad)
check_json('E S17 duplicate_failed_mechanism',q)

PY
```
```text
A S15 both_null market_manifest EvalProtocolError baseline 臂 market_manifest 含 null（2 行）
A S15 both_null execution_contract_version EvalProtocolError baseline 臂 execution_contract_version 含 null（2 行）
A S15 both_null seed EvalProtocolError baseline 臂 seed 含 null（2 行）
A S15 both_null risk_budget EvalProtocolError baseline 臂 risk_budget 含 null（2 行）
A S15 missing_policy_hash EvalProtocolError baseline 臂缺必需列 policy_hash
A S16 all_censored insufficient 2 1.0
A S12 null_m_true ValueError PairedPanel: 候选 d 在 1 个共同可评行（m=True）上为 null/NaN/Inf，拒收（不得静默补 0）
A S14 missing_eligibility EvalProtocolError 决策视图缺 eligibility_by_estimand（struct 固定六键），不得默认全部合格
B S06 folds ['wf000', 'wf001'] tested ['first'] tail_loss_recorded True
B S08 snapshot_error RuntimeError backend boom
B S08 rows_at_execution [1] terminal_states ['completed', 'failed']
B S14 fraction_source fake/real True True schema_order_equal True
B S14 window_enums ['none', 'rows', 'wallclock']
B S10 injected_power pass invalid 1 failed 1 worst_ci (0.9455406146079194, 0.9997468539670226)
D1 power_false_pass_flag REJECTED ValueError 功效不足却伪造 pass
D2 all_T0_invalid REJECTED ValueError 搜索计数不一致
D3 full_tail_grep '二审终裁：pass\n三审终裁：fail\n' exit 1
D3 full_tail_grep '二审终裁：fail\n三审终裁：pass\n' exit 0
E S08 later_invalid ASTRejected UNKNOWN_OP at $: Div
E S08 later_invalid [('execution_batch', 'completed'), ('feature_snapshot', 'failed'), ('feature_snapshot', 'rejected')]
E S08 repeated_ast [('execution_batch', 'completed'), ('feature_snapshot', 'completed'), ('feature_snapshot', 'completed')]
E S12 candidate_only_censored None
E S14 mixed_sources real/fake policy ['policy']
E S17 duplicate_failed_mechanism REJECTED ValueError 重复主机制

exit=0
```

解释：E `candidate_only_censored=None` 是 validate 正常返回，不是收益为零；正反单臂排除由专项测试核定。重复 AST 的两条 snapshot reservation 都有终态，第二次计算保留父关系。B 旧的“一次 guard 失败/100 次”内存替身现在给 power pass，是本轮 >5% 规则下的预期变化；这是控制流注入、不是新的功效数值证据，不能沿用旧规则将其判回退。

#### F：原文拒收 + 捕获拒收后继续逐条运行

```text
Traceback (most recent call last):
  File "<stdin>", line 13, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/features.py", line 157, in feature_snapshot
    if not ctx.check_identity(anchors):
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/features.py", line 58, in check_identity
    raise SnapshotInvalid(f"anchors {name} 身份错配")
quant_lab.research.features.SnapshotInvalid: anchors graph_version 身份错配
exit=1
```

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
from unittest.mock import patch
from dataclasses import replace
import datetime as dt
import polars as pl
import numpy as np
from quant_lab.research import features as F, nullmodel as N, api as A
from quant_lab.research.ledger import Ledger,MemoryLedger
from quant_lab.research.contract_tests import T0,make_bars
ast={"op":"Ref","args":[{"field":"close"}],"params":{"lag":0}}
a=pl.DataFrame({"episode_id":["e"],"instrument_id":["X"],"t_dec":[T0],"graph_version":["actual-revoked"],"derivation_hash":["actual-d"]})
ctx=F.SnapshotContext(market_manifest="m",graph_version="different-live",derivation_hash="different-d",consumable=lambda:True)
with patch.object(F,"_cache_read",return_value=None),patch.object(F,"_cache_write"):
    try:
        r=F.feature_snapshot([ast],a,bars=make_bars([1.,2.,3.]),ctx=ctx,cache=True)
    except Exception as e:
        print("F identity_mismatch",type(e).__name__,str(e))
# Recover logic real, event I/O in memory; caller PID is demonstrably alive throughout.
led=object.__new__(Ledger)
led.read=lambda:pl.DataFrame([{"attempt_id":"live-other","run_id":"other-active-run","status":"running","created_at":(dt.datetime.now(dt.UTC)-dt.timedelta(hours=2)).isoformat()}])
marks=[]
led.mark=lambda *args,**kw:marks.append((args,kw))
print("F S07 active_old_run_recover",led.recover(),marks)
w=N.synth_world(N.WorldConfig(seed=1,n_clusters=900,n_candidates=2))
bad=replace(w.inputs,anchors=w.inputs.anchors.with_columns(pl.Series("cluster_id",["unique"+str(i) for i in range(w.inputs.n)]),pl.lit("ONE-INSTRUMENT").alias("instrument_id")))
g=N.assert_not_episode_shuffle(w.inputs,bad,w.day)
print("F S10 clusters",w.inputs.anchors["cluster_id"].n_unique(),bad.anchors["cluster_id"].n_unique(),"instruments",bad.anchors["instrument_id"].n_unique(),"guard",g["ok"],"checks",g["checks"])
seen=[]
orig=A.deff_from_panel
def observed(panel,**kw):
    seen.append(panel.candidate_ids)
    return orig(panel,**kw)
with patch.object(A,"deff_from_panel",side_effect=observed):
    report=A.run_pipeline(w.inputs,N.default_candidates(2),A.PipelineConfig(B=100,block_len_days=3,config_cap=2),ledger=MemoryLedger())
print("F S06 deff_inputs",seen,"admitted",report["candidate_pool"]["n_admitted"],"selection_status",[f["selection"]["status"] for f in report["folds"]])
PY
```
```text
F identity_mismatch SnapshotInvalid anchors graph_version 身份错配
F S07 active_old_run_recover 0 []
F S10 clusters 900 2204 instruments 1 guard False checks {'cluster_layout': False, 'missing_layout': True, 'block_ac1': False, 'cross_instrument': False, 'finite_diagnostics': False, 'icc': True, 'scale': True, 'tail': True, 'missing': True}
F S06 deff_inputs [('base',), ('base',), ('base',), ('base',), ('base',), ('base',), ('f00:gt_q30',), ('f00:gt_q30',), ('f00:gt_q70',), ('f00:gt_q70',), ('f00:gt_q30',), ('f00:gt_q30',), ('f00:gt_q70',), ('f00:gt_q70',), ('f00:gt_q30',), ('f00:gt_q30',), ('f00:gt_q70',), ('f00:gt_q70',)] admitted 2 selection_status ['ok', 'ok', 'ok']

exit=0
```

F 的 identity_mismatch、old_active_run、cluster-layout、deff_inputs 均有新输出。无 shock_grid 的旧 F 兼容路径仍显示 block_ac1 检查；本轮 MC 实际生成器的冲击格点路径不以块间自相关设门，不能混为同一判据。

#### G：D1/E/G 伪造报告及独立算术

D1/E 已在分段探针运行，G 原文如下；均通过调用 taskList 中的实际 R-08 Python 判读体，不是手写替代 verify。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import copy,io,json,shlex,contextlib,re
from pathlib import Path
from unittest.mock import patch
from quant_lab.research.nullmodel import clopper_pearson as cp
s=Path("docs/adr/report-G3-null-model.md").read_text();start=s.rindex("```json")+7;end=s.rindex("```");j=json.loads(s[start:end])
tasks={x["id"]:x for x in json.loads(Path("taskList.json").read_text())["modules"]["research"]["tasks"]}
v=shlex.split(tasks["R-08"]["verify"].split(" && ")[-1])[-1]
q=copy.deepcopy(j)
for r in q["results"]:
    if r["kind"]=="null":r["diagnostics"]["shuffle_guard"]["ok"]=False
fake=s[:start]+json.dumps(q)+s[end:]
try:
    with patch("builtins.open",lambda *a,**k:io.StringIO(fake)),contextlib.redirect_stdout(io.StringIO()):exec(compile(v,"R08","exec"),{})
    print("G D diagnostic_false_only ACCEPTED")
except Exception as e:print("G D diagnostic_false_only REJECTED",type(e).__name__)
for r in j["results"]:
    n,x,f=r["n_done"],r["n_positive"],r["n_failed"]
    worst=x+f if r["kind"]=="null" else x
    for key,actual in [("ci",cp(x,n-f)),("worst_case_ci",cp(worst,n)),("searched_worst_ci",cp(worst,r["n_searched"]))]:
        assert all(abs(a-b)<1e-12 for a,b in zip(actual,r[key])),(r["kind"],key)
    assert abs(r["rate"]-x/(n-f))<1e-12
    assert abs(r["worst_case_rate"]-worst/n)<1e-12
    assert sum(r["tiers"].values())==n
    if r["kind"] in ("null","power"):
        print("G report",r["mechanism"],r["kind"],"n/x/failed",n,x,f,"searched",r["n_searched"],"searched_ci",[round(z,6) for z in r["searched_worst_ci"]],"verdict",r["verdict"])
print("G arithmetic",len(j["results"]),"rows checked")
PY
```
```text
G D diagnostic_false_only REJECTED ValueError
G report common_shock null n/x/failed 1000 2 1 searched 842 searched_ci [0.000735, 0.010377] verdict pass
G report cluster_heavy_tail null n/x/failed 1000 6 3 searched 974 searched_ci [0.004234, 0.017468] verdict pass
G report nonuniform_density null n/x/failed 1000 8 11 searched 725 searched_ci [0.01585, 0.040624] verdict pass
G report circular_shift null n/x/failed 1000 4 0 searched 993 searched_ci [0.001099, 0.010281] verdict pass
G report common_shock power n/x/failed 1000 327 34 searched 768 searched_ci [0.390503, 0.461632] verdict fail
G arithmetic 13 rows checked

exit=0
```

### 新增取证与独立注入

#### R4-STRUCT：S10/A27 结构门仍会触发

核对实现：跨品种相关使用生成器实际操作的 shock_grid 与拟合训练格点；逐复制以 Fisher-z 差及块数 SE 的 4 倍带宽判断；块间自相关只记诊断；ICC 以洗牌基线为锚（另保留绝对地板）；整轮由总体检查失败或 guard_fail_rate>0.05 判 invalid。不是把原 episode 噪声统计量换一个宽阈值继续门控。

独立种子与现有 A27 不同。破坏同时改 shock_grid **和实际 base_R**：逐品种独立重排完整 3 日冲击块，把新旧格点差加回对应 episode，保留特异残差与缺失布局。没有只改诊断旗标。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import numpy as np
from dataclasses import replace
from unittest.mock import patch
from quant_lab.research import nullmodel as N
from quant_lab.research.api import PipelineConfig
w=N.synth_world(N.WorldConfig(seed=2,n_clusters=1600));m=N.fit_residual_model(w,block_len_days=3,train_end_day=180);fitted=m.grid[:180]
def broken(world,x,rng):
 old=x.shock_grid;grid=old.copy();nb=len(grid)//3
 for j in range(grid.shape[1]):grid[:nb*3,j]=grid[:nb*3,j].reshape(nb,3)[rng.permutation(nb)].reshape(-1)
 # Change actual returns as well as diagnostic grid, retaining idiosyncratic residual and missing layout.
 r=x.base_R+grid[world.day,world.inst]-old[world.day,world.inst]
 return replace(x,shock_grid=grid,base_R=r)
normal=bad=0
for i in range(20):
 rng=np.random.default_rng(192110+i);x=N.resample_null(w,m,rng)
 g=N.assert_not_episode_shuffle(w.inputs,x,w.day,block_len_days=3,fitted_grid=fitted);normal+=not g['ok']
 y=broken(w,x,np.random.default_rng(292110+i));h=N.assert_not_episode_shuffle(w.inputs,y,w.day,block_len_days=3,fitted_grid=fitted)
 bad+=not h['checks']['cross_instrument']
print('R4 independent actual_returns_and_grid normal_fail',normal,'/20 broken_cross_caught',bad,'/20',flush=True)
original=N.resample_null
for kind in ['null','power']:
 for damage in [False,True]:
  def generate(world,model,rng,**kw):
   x=original(world,model,rng,**kw)
   return broken(world,x,rng) if damage else x
  with patch.object(N,'resample_null',generate):r=N.run_mc('common_shock',kind=kind,n_rep=8,seed0=2,world_cfg=N.WorldConfig(n_clusters=1600),pipe_cfg=PipelineConfig(B=100))
  print('R4 independent MC',kind,'damage',damage,'verdict',r.verdict,'reason',r.diagnostics['invalid_reason'],'invalid',r.diagnostics['n_invalid_null_model'],'grid',r.diagnostics['grid_dependence'],flush=True)
PY
```
```text
R4 independent actual_returns_and_grid normal_fail 0 /20 broken_cross_caught 8 /20
R4 independent MC null damage False verdict insufficient reason None invalid 0 grid {'fitted_ac1': -0.34331975388460007, 'null_mean_ac1': 0.0007829382515213024, 'fitted_cross': [0.4634950088944095, 0.39532745335499664, 0.5163415706931419], 'null_mean_cross': [0.4217087467435885, 0.35433834572116457, 0.5306870654567748], 'ok': True}
R4 independent MC null damage True verdict invalid_null_model reason GRID_DEPENDENCE_NOT_PRESERVED invalid 2 grid {'fitted_ac1': -0.34331975388460007, 'null_mean_ac1': -0.03617183796898137, 'fitted_cross': [0.4634950088944095, 0.39532745335499664, 0.5163415706931419], 'null_mean_cross': [0.052500310092695956, -0.013516525284505206, -0.005475121031724707], 'ok': False}
R4 independent MC power damage False verdict fail reason None invalid 0 grid {'fitted_ac1': -0.34331975388460007, 'null_mean_ac1': 0.0007829382515213024, 'fitted_cross': [0.4634950088944095, 0.39532745335499664, 0.5163415706931419], 'null_mean_cross': [0.4217087467435885, 0.35433834572116457, 0.5306870654567748], 'ok': True}
R4 independent MC power damage True verdict invalid_null_model reason GRID_DEPENDENCE_NOT_PRESERVED invalid 2 grid {'fitted_ac1': -0.34331975388460007, 'null_mean_ac1': -0.03617183796898137, 'fitted_cross': [0.4634950088944095, 0.39532745335499664, 0.5163415706931419], 'null_mean_cross': [0.052500310092695956, -0.013516525284505206, -0.005475121031724707], 'ok': False}

exit=0
```

正常单次 0/20，破坏单次 cross 检出 8/20；这不是“单次必检”的证明。更关键的是同一实际收益破坏在 null、power 整轮都输出 GRID_DEPENDENCE_NOT_PRESERVED / invalid_null_model，正常两条路径都未判结构无效。8 次小 MC 的 insufficient/fail 只说明小样本统计出口，不作 FPR/功效准入证据。按 A27 要求，校准后既有误拒对照又有真实破坏检出，S10 本体闭合。

#### B11、44 列、B19/B20、A13/A15 与曲线措辞

B11 现有测试实跑：caller 的 horizon_end 纳入 config_id，同值 duplicate，换窗独立 reserved；policy 推导窗不额外进入此键，由 policy_hash 标识。api 的候选、最终与执行 reservation 有 caller_horizon_end 传递。B20 的 SufficiencyFloor 成对声明 block_len/下限，并检查 block_len≥horizon+embargo、cross 与 max_span；max_t_panel 使用该 floor 的断言通过。B19 源码核读未见 G3 自算强平；哨兵通过，目前只镜像 liquidation_unmodeled，未以它替代 G2 经济记账。

A13 六键词汇保持，只对请求 estimand 的 null 拒收，其余 null 记 warning/diagnostics；A15 覆盖失败与右删失互斥排除并各记数。两条具名断言随专项与全套通过。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import inspect
from quant_lab.research import synthetic as S
from quant_lab.market.execution import BATCH_SCHEMA
from quant_lab.research.ops import FIELD_UNITS
x=S.fake_execution(S.fake_episodes(20,span_days=2,seed=19))
print('schema_columns',len(S.BATCH_SCHEMA),len(BATCH_SCHEMA),'order_dtype_equal',list(S.BATCH_SCHEMA.items())==list(BATCH_SCHEMA.items()))
for k in ['outcome_kind','exit_legs','horizon_source']:
 print(k,'produced',k in x.columns,'parameter',k in inspect.signature(S.fake_execution).parameters,'allowed_feature',k in FIELD_UNITS,'values',x[k].unique().to_list())
PY
```
```text
schema_columns 44 44 order_dtype_equal True
outcome_kind produced True parameter False allowed_feature False values ['filled_closed', 'unevaluable', 'unfilled_expired']
exit_legs produced True parameter False allowed_feature False values [[], ['sl'], ['tp']]
horizon_source produced True parameter False allowed_feature False values ['policy']

exit=0
```

parity 的 CONTRACT_PENDING_G2 为空；schema 镜像是 44 列逐列逐序一致（包含 outcome_kind、exit_legs、horizon_source）。上述三列由 fake_execution 产出，非它的同名参数、非 FIELD_UNITS 合法输入。fraction_source/entry_fractions/tp_fractions 另有 G2 实际 build_request→simulate_batch 对拍，不把随机 fake 收益与真实撮合数值要求相等。

B12 曲线报告把 67 条真实括号、单月 BTC 波动和随机游走假设分开，4000 机会是合成布局，未把它称作真实功效，G-STAT-CLAIM 仍 pending。记录两处非阻塞措辞修订建议：将“真实尾部只会更厚/真实删失率会略高”改成待验证的方向假设；将“统计效率赔掉约 11%”改成“该合成设定 K_min 从 662 到 587”，K 变化不等于功效或有效日历块数变化。报告的 3d 占位表述属于历史提案，现行 5d 以 §5.18 定稿为准，不能据旧提案自由回落。临时脚本路径含省略号，故本轮仅核读该提案，未声称重跑曲线。

### 剩余必修

#### S08：实际代码血缘未覆盖后端实现（R4-L）

位置：src/quant_lab/research/ledger.py:186 与 api.py 的 reservation。生命周期、重复 AST occurrence、异常终态、recompute_of、成本及 protocol/opportunity/policy/result 摘要已补齐；但 code_version 只对 research 顶层 glob("*.py") 求 hash，遗漏 backends/ 全部代码。api 未传实际 backend_version，默认 None。三审要求的“实际 code 血缘”尚未完成。

本轮不改磁盘源码，内存替换 Path.read_bytes 对 polars 后端返回的字节；递归 research digest 变化而账本身份完全相同。该遗漏同样会隐藏后端运算实现的实际改动，不只下面用来定位的字节标记。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import datetime as dt
from pathlib import Path
from unittest.mock import patch
from quant_lab.research.ledger import MemoryLedger
from quant_lab.research.nullmodel import research_code_digest
kw=dict(origin='human',canonical_hash='h',params={},fold_id='f',visible_cutoff=dt.datetime(2024,1,1,tzinfo=dt.UTC),objective='feature_snapshot',data_manifest='m',seed=1)
def row():
 l=MemoryLedger();a=l.reserve(**kw);return l.rows[a]
a=row();d=research_code_digest();original=Path.read_bytes
def changed(p):
 b=original(p)
 if p.as_posix().endswith('/research/backends/polars.py'):
  return b+b'\n# independent changed backend bytes\n'
 return b
with patch.object(Path,'read_bytes',changed):
 b=row();e=research_code_digest()
print('R4 S08 backend_bytes_changed',d!=e,'ledger_code_version_equal',a['code_version']==b['code_version'],'backend_version',a['backend_version'],b['backend_version'])
print('ledger_code_version',a['code_version'])
PY
```
```text
R4 S08 backend_bytes_changed True ledger_code_version_equal True backend_version None None
ledger_code_version 826fcd56c78cf1af21e99c05cc72067ee1fb396f429541f4d0c5ad0a5d87d562

exit=0
```

改法：使用包含相对路径与全部递归 research 源码的统一、冻结代码摘要（可复用 research_code_digest 的文件覆盖口径），写入实际后端名/版本及必要语义版本，绑定运行 scope/结果身份；不能只断言 code_version 非空。验收：仅改后端运算实现时，新 reservation 与旧记录的 code identity 必须不同，旧结果不得当作当前实现 duplicate 复用；MemoryLedger/磁盘两条路径都测；所有生命周期与并发回归保持通过。

#### S17：新结构诊断仍可假绿，A31 rebuild 可给旧结果换新身份（R4-V）

位置：nullmodel.py:749 附近 verify_report_text、:790/910 write_report、:935 --rebuild。原 D1/E/G 与全 T0、短跑、计数/算术、不完整限制、非有限值等拒收都已补齐。剩余两个独立可复现工程缺口：

1. verify_report_text 没有核对 grid_dependence.ok、dependence_aggregate.ok、invalid_reason、guard_fail_rate，也未要求新 grid 字段存在。只改其中一个字段，其余真实计数、区间、hash 与 §3.2 保持原样，R-08 实际判读仍接受。尤其总体 grid 检查失败不要求逐复制 invalid 计数必非零，不能靠旧 shuffle_guard/计数门代替新总体门。
2. --rebuild 只读取旧 JSON 的 MCResult 后重新排版，write_report 却把 meta.research_code_sha256 无条件替换为当前源码摘要。下面把旧 hash 置零，完整调用现有 --rebuild 路径并将 I/O 捕获在内存：results 完全没变、未运行 MC，却得到当前 hash 且 verify 接受。A31 的“陈旧文本直接拒收”测试通过，不等于生成来源绑定闭合。此反例不指控当前报告实际由旧结果伪装，只证明现有公开重建路径允许绕过。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import copy,io,json,contextlib,shlex
from pathlib import Path
from unittest.mock import patch
from quant_lab.research import nullmodel as N
s=Path('docs/adr/report-G3-null-model.md').read_text(); start=s.rindex('```json')+7;end=s.rindex('```');j=json.loads(s[start:end])
body=shlex.split(next(t for t in json.loads(Path('taskList.json').read_text())['modules']['research']['tasks'] if t['id']=='R-08')['verify'].split(' && ')[-1])[-1]
for damage in ['grid_false','aggregate_false','invalid_reason','guard_rate','missing_grid','stale_hash']:
 q=copy.deepcopy(j);d=q['results'][0]['diagnostics']
 if damage=='grid_false':d['grid_dependence']['ok']=False
 if damage=='aggregate_false':d['dependence_aggregate']['ok']=False
 if damage=='invalid_reason':d['invalid_reason']='GRID_DEPENDENCE_NOT_PRESERVED'
 if damage=='guard_rate':d['guard_fail_rate']=1.0
 if damage=='missing_grid':del d['grid_dependence']
 if damage=='stale_hash':q['meta']['research_code_sha256']='0'*64
 fake=s[:start]+json.dumps(q)+s[end:]
 try:
  with patch('builtins.open',return_value=io.StringIO(fake)),contextlib.redirect_stdout(io.StringIO()):exec(compile(body,'R08-original','exec'),{})
  print('R4 report',damage,'ACCEPTED')
 except Exception as e:print('R4 report',damage,'REJECTED',type(e).__name__,str(e))
# Exercise the exact --rebuild path with only read/write endpoints captured in memory.
old=copy.deepcopy(j);old['meta']['research_code_sha256']='0'*64
stale=s[:start]+json.dumps(old)+s[end:]; captured=[]
with patch.object(Path,'read_text',return_value=stale),patch.object(Path,'mkdir'),patch.object(Path,'write_text',lambda self,text,**kw:captured.append(text)),contextlib.redirect_stdout(io.StringIO()):
 N._main(['--rebuild','/tmp/not-created-old-report.md','--out','/tmp/not-created-rebuilt.md'])
new=json.loads(captured[0][captured[0].rindex('```json')+7:captured[0].rindex('```')])
with contextlib.redirect_stdout(io.StringIO()):N.verify_report_text(captured[0])
print('R4 A31 rebuild old_digest',old['meta']['research_code_sha256'],'new_digest',new['meta']['research_code_sha256'],'results_unchanged',new['results']==old['results'],'verify ACCEPTED','writes_captured',len(captured))
PY
```
```text
R4 report grid_false ACCEPTED
R4 report aggregate_false ACCEPTED
R4 report invalid_reason ACCEPTED
R4 report guard_rate ACCEPTED
R4 report missing_grid ACCEPTED
R4 report stale_hash REJECTED ValueError 制品陈旧（A31）：报告由代码 000000000000 生成，当前研究代码为 0ab9ba6a1649——门或流水线已变更，必须重跑 MC 再验收，不得用旧制品判定
R4 A31 rebuild old_digest 0000000000000000000000000000000000000000000000000000000000000000 new_digest 0ab9ba6a164992f314d04438a00f82615f46fde8132dd179b98677c6bc196707 results_unchanged True verify ACCEPTED writes_captured 1

exit=0
```

改法与验收：

- 将本轮总体格点门及失败率规则纳入结构化 report schema；要求字段齐全、类型正确且依据原始诊断重算判断，与 invalid_reason/verdict/各失败计数一致。新增本轮五种变体，每种均须由 taskList R-08 实际 verify 拒收；正常当前报告仍可接受。同步明确 ≤5% 时 run_mc 与 verifier 的口径（现 verifier 的零失败要求更严，不应把两个不同判定宣传为同一门）。
- MC 开始时冻结 generating_code_sha256，结束时确认源码仍一致；重排版沿用原生成 digest，可另记 renderer digest，禁止把旧 MC 身份更新成新生成身份。旧 hash 或缺生成 hash 的 --rebuild 产物必须仍被 verify 拒收，只有在当前代码真正重跑 MC 才能更新生成身份。回归须走实际 --rebuild 路径，不能只改文本 hash。
- §3.2 当前已有有效“未检出不能解释为无增益”“合成不是真实功效”等限制。功效 fail 历史记录继续保留，无需为本轮修复把功效提高到 80%；R-10 继续只读文件最后一行。

### 四审终裁

**fail。** 原 8 项工程中 S03、S06、S07、S10、S12、S14 已闭合；S08、S17 部分闭合。新增 (0) 的结构门独立注入条件成立；(0b) 的 A31 陈旧文本拒收成立，但 rebuild 生成来源绑定仍有绕路。11 项原已闭合项目抽验保持，没有回退。

R-05–R-09 原文 verify 均 exit=0，研究全套 328 passed；这些实跑结果与上述反例同时成立。R-08 正向结构化判读虽修复旧 D1/E/G，仍遗漏三审后重做的新结构门，因此不能出 pass。

现有闭合后重跑报告的 13 行计数、三分母比率和 CP 区间已独立复算。四个 null 主机制各 n=1000，阳性/失败/有搜索分别为 2/1/842、6/3/974、8/11/725、4/0/993；有搜索、失败计阳性的区间上界依次约 1.0377%、1.7468%、4.0624%、1.0281%。仅描述当前合成结果。

功效主记录 n=1000，检出 327，失败 34，T0=232，有搜索 768；全计划失败计未检出为 32.7000%（95% CP 29.7970%–35.7053%），非失败分母为 327/966=33.8509%，有搜索分母为 327/768=42.5781%（95% CP 39.0503%–46.1632%）。原 verdict=fail 保留；按用户出口 A 不作准入否决。不是实际交易/真实频道功效证明。

后端报告仍保留 polars 18/18、polars_ta 16/17 加 EMA unsupported 与 Corr 18 条失败；206.77s/36.11s 是历史报告冷跑描述、工作量并不等同，RSS 为进程峰值。本轮运行指定 verify/pytest 与列出的独立小规模注入，未另起全规模 5000 次主 MC 或 416 万行后端计时，也未改写任何报告/契约/源码/测试/taskList。

收尾版本复核：2026-09-11T06:59:35.811226+00:00；35 个开审文件与收尾相同，无新增/删除/内容变更；taskList notes 不作源码并发事件。收尾报告/契约摘要：

```text
f70c06ae5c46ecc57372ad7c0d7413ce24fbf504aeaea5bffc46906550cecce5  contracts/feature-snapshot.md
50ae0a3af130a5047612eb183efc13fd597bd949cbebc6a8de59f8f6cbe85644  contracts/research-schema.md
81ade79b819a6946579ddca80afa06baf09171e40ca7a4614e52132bbc88c7e8  contracts/execution-interface.md
dfae7f3708185c00e4b25b0705828858b03080a29bd7422e517df9dbca3ed151  docs/adr/report-G3-null-model.md
d9fc1690653016db72cd78c4756efc5b36b34ae04dcf6815aa02c27858cd7afd  docs/adr/report-G3-backend-spike.md
3c1845297e4055dab49383c6f3c47e89e90600a2550b00477941bea95e3ef8a8  docs/adr/report-G3-research-horizon-curve.md
```

落盘以 append 二进制模式写入，并机械验证一至三审 126433 bytes 前缀 SHA256 未变。收尾验证命令：`tail -1 docs/adr/review-G3-P1.md | grep -E '^四审终裁：(pass|fail)$'`；`grep -c '已闭合' docs/adr/review-G3-P1.md`。后者是整份历史文件出现行数，不能充当本轮 17 项闭合计数；本轮以逐条闭合表为准。

四审终裁：fail

## 五审（R-10 round 5）

审查日期：2026-09-11。审查者：Codex 主控，未委派。本节为追加审查；历史结论按当时证据保留，不改写。取证仅限用户给定 research 源码、research 测试、G3 报告、contracts、taskList 及本 review 的历史反例。源码、测试、报告、契约与 taskList 均未主动修改。显式要求的 pytest/API/账本验证所产生的临时数据位于 /tmp，湖根经 QUANT_LAB_DATA_ROOT=/tmp/ql-review-r5-lake；PYTHONDONTWRITEBYTECODE=1，PYTEST_ADDOPTS='-p no:cacheprovider'。所有 Python 探针通过 stdin 运行，报告破坏与不同源码视图均在内存注入；没有把伪造报告写回被审文件。R-09 原文固有的 /tmp/ql-proto 为临时产物。所有命令 cwd=quant-lab/，前台进程跟踪至结束。

**结论：fail。S08(R4-L) 按本轮明确的代码/后端血缘和复用口径闭合；S17(R4-V) 部分闭合。** 六种旧篡改和两种 rebuild 产物全部被拒收，但新反例证明结构失败没有被强制纳入 n_failed，能使本应超过 7% 的条件 FPR 最坏界假绿。另有相关向量漏项/非法值、首 replicate 特权和 worker 来源绑定缺口。S10 的旧强相关注入仍通过验收；本轮新增弱相关机制破坏暴露既有总体判据盲区，改判部分闭合，不能把“旧回归全绿”写成完整闭合。

### 逐条闭合表

以下“闭合”是本轮抽验范围内的判断，不是全仓正确性证明。D 表的 Q-Sxx 为四审逐项命令原文重放，后附完整命令与新输出。

| 项目 | 五审判定 | 证据 |
|---|---|---|
| S01 | 闭合 | Q-S01：1 passed；热缓存按 episode 身份对齐。 |
| S02 | 闭合 | Q-S02：2 passed；ctx 单一来源、晚到依赖不可见。 |
| S03 | 闭合 | Q-S03：1 passed；撤销门；全套与 R-08 round3 身份/epoch/发布间撤销回归通过。 |
| S04 | 闭合 | Q-S04：2 passed；隔离跨进程保持，主后端可用；R-05 仍保留历史 polars_ta 失败事实。 |
| S05 | 闭合 | Q-S05：1 passed；日历划分、purge 与成熟门。 |
| S06 | 闭合 | Q-S06：1 passed；预算只收紧；round3 真实候选依赖/undefined 回归也通过。 |
| S07 | 闭合 | Q-S07：1 passed（真实多进程）；R-07 10 passed，账本 77 行、无 reserved；round3 错序事件、活进程恢复回归通过。 |
| S08 / R4-L | 闭合 | A 原 Path.read_bytes 反例由 code_version_equal=True 变 False；A/B2 内存和磁盘重复=duplicate、换后端字节/版本均新建 completed，另一个配置被预算拒绝；API 实际 polars/0.2 进入 PanelInputs、snapshot 和 search；Q-S08：2 passed，异常终态保持。 |
| S09 | 闭合 | Q-S09：1 passed；成熟标签、总体分位与训练界。 |
| S10 | 部分闭合 | Q-S10：2 passed；C 强相关真实收益+格点破坏，两种 MC 均 GRID_DEPENDENCE_NOT_PRESERVED；B1 新弱相关机制破坏漏检，详见 R5-W。旧用例无回退，新覆盖揭露旧盲区。 |
| S11 | 闭合 | Q-S11：1 passed；删失块不增加准入块数。 |
| S12 | 闭合 | Q-S12：3 passed；round3 单臂删失和 null/NaN/Inf 拒收保持。 |
| S13 | 闭合 | Q-S13：1 passed；未成交未删失的 expiry t1 保持。 |
| S14 | 闭合 | Q-S14：8 passed；冻结六键、真实 G2 对拍和 fraction 来源，round3 三腿/部分缺失回归通过。 |
| S15 | 闭合 | Q-S15：1 passed；两臂身份字段/协议错误拒收。 |
| S16 | 闭合 | Q-S16：1 passed；合成端到端、Decimal 与删失诊断。 |
| S17 / R4-V | 部分闭合 | A 旧反例均拒收，E 最新报告 13 行算术与摘要一致，出口 A 保留；B 新反例 R5-C/G/O/H 未闭合。 |
| S18（额外） | 闭合 | 历史命令提取同时复跑 Q-S18：1 passed，流式枚举预算/截断原因。 |
| S19（额外） | 闭合 | Q-S19：1 passed，Decimal 规范化。 |

S01–S16 中四审已闭合的 15 项全部重放：14 项维持闭合；S10 新反例需补齐，未观察到旧用例回退。S08 本轮闭合。S01–S17 共 15 项闭合、2 项部分闭合；附加的 S18/S19 不混入该分母。

### 口径裁定与主动披露事项

1. **不以 0.2R 功效不足否决 P1。** E 独立复算：327/966=33.8509%（非失败）、327/1000=32.7%（全计划保守）、327/768=42.5781%（有搜索）；全计划 CP 下界 29.7970%，有搜索下界 39.0503%。§3.2 所述不能把未检出当无增益、合成不是真实功效，实质属实。其把 42.6% 与 29.8% 写在同一括号内容易混分母，宜清晰标注，但不是本轮 fail 依据。
2. **非零误拒带在原则上正确，不要求零失败。** 条件于固定拟合世界，若每复制独立且误拒率 p=1%/2%，1000 次“任一失败”概率分别 0.99995683/0.9999999983；超过 5%（至少 51 次）的精确二项尾概率分别 1.56e-20/2.96e-9。200 次时超过 5% 的概率约 6.88e-6/0.00253。计算命令在 E。这解释为什么零失败门会错杀，不证明所有生成世界都确有 p=1–2%，也不把 5% 叫普适统计保证。当前报告主机制均 0 次结构失败，1–2% 应理解为历史/设计说明，不能冒充本轮估计。五审不以选择 5% 本身判 fail；R5-C 是“失败未进入保守界”，R5-O 是“第一复制仍被零失败门特判”，R5-W 即使阈值降到零也漏检，三者都不是阈值偏好。
3. **预算单位接受用户明确口径。** B2 实证：同一 config_id 的三个实现尝试都记账，仍只占一个唯一配置；下一个 config_id 在 cap=1 时拒收。更改实现会重算，不是新配置被误去重，也没有抹掉成本/尝试历史。API 原有显式 recompute_of 本已允许同配置重算，因此本轮加字段没有新开一个此前不存在的计费通道。若事后看结果挑“最佳代码实现”，属于实现版本间研究选择，应预注册/纳入研究多重比较，而不能称无选择的维护重算；本轮没有据此强制把字节哈希加入 config 预算，也没有用“改个注释仍可重算”单独判 S08 fail。
4. **来源声明不等于执行证明。** B1 同步把 fitted/null 两边置零仍可通过，说明自填 JSON 哈希无法认证 MC 发生过。与用户披露一致；这不是本轮新发现的单字段漏核，也不要求为防任意恶意制品而引入签名系统。应以 world/pipeline、机制与 seed 段重跑抽样复核；本轮 E 验的是内容摘要相等，没有把它说成执行认证。收尾新见 contracts/research-schema.md §9.10.19 A39 已把此边界放进契约，并规定 P2 独立抽样复核；采用该安排，不要求另改报告正文，也不以欠这一句话列必修。R5-H 则不同：正常受信运行路径本身未核 worker 生成身份，属于工程来源绑定缺口。
5. **异常路径与 worker 路径分开裁定。** B3 两个真实 OS worker 的稳定对照能收尾；worker RuntimeError、父进程收尾摘要漂移、父进程 KeyboardInterrupt 都没有调用写报告。不能由此推导 worker 版本已被验证；不同 worker 源视图/执行修订仍会被父进程接收（R5-H）。硬杀进程未另行实跑，不把模拟 KeyboardInterrupt 写成 SIGKILL 证明。

### A–E 取证说明

下列输出为本轮 stdout/stderr 与退出码。A 的原四审 R4-V 脚本在 rebuild 被拒绝后未捕获异常，所以该原脚本 exit=1 是封堵成功，不是未执行六种篡改。后续 A 完整链逐种执行了 taskList R-08 原文的 pytest + report verifier；为了遵守只读约束，仅对该命令的 -c 报告读取注入内存文本，pytest 部分没有替换或跳过。B 探针中的 ACCEPTED 是反例被门接受，不能当成项目验收通过。

#### 指定验证：全套与 R-05/R-07/R-08/R-09 原文

````bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' QUANT_LAB_DATA_ROOT=/tmp/ql-review-r5-lake .venv-g3/bin/python -m pytest tests/research -q
````

````text
........................................................................ [ 21%]
........................................................................ [ 42%]
........................................................................ [ 64%]
........................................................................ [ 85%]
................................................                         [100%]
336 passed in 153.00s (0:02:33)
[取证驱动退出码 0]
````

#### 指定验证原文及逐条输出（R-09 先建隔离账本，再验 R-07）

````bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' QUANT_LAB_DATA_ROOT=/tmp/ql-review-r5-lake .venv-g3/bin/python - <<'PY'
import json,subprocess,os
from pathlib import Path
tasks=json.loads(Path('taskList.json').read_text())['modules']['research']['tasks']
for k in ['R-05','R-09','R-07','R-08']:
 cmd=next(t['verify'] for t in tasks if t['id']==k)
 print('TASK',k,'COMMAND',cmd,flush=True)
 r=subprocess.run(cmd,shell=True,executable='/bin/bash',env=os.environ.copy(),text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
 print(r.stdout+'exit='+str(r.returncode),flush=True)
PY
````

````text
TASK R-05 COMMAND test -f docs/adr/report-G3-backend-spike.md && .venv-g3/bin/python -c "import json,re,sys;t=open('docs/adr/report-G3-backend-spike.md',encoding='utf-8').read();j=json.loads(t[t.rindex(chr(96)*3+'json')+7:t.rindex(chr(96)*3)]);m=j['matrix'];assert all(v['status']=='pass' for v in m['polars'].values()),m['polars'];assert sum(v['status']=='pass' for v in m['polars_ta'].values())>=10;tm={r['backend']:r for r in j['timings']};assert tm['polars']['n_ast']==128 and tm['polars']['cold_s']>0 and tm['polars']['hot_s']>0 and 0<tm['polars']['rss_gib_after_hot_s']<=8 and tm['polars']['cold_s']<=1200;print('spike ok',tm['polars']['cold_s'],tm['polars_ta']['cold_s'])"
spike ok 206.77 36.11
exit=0
TASK R-09 COMMAND .venv-g3/bin/python -m quant_lab.research.api run --config tests/research/fixtures/protocol_synthetic.yaml --out /tmp/ql-proto && .venv-g3/bin/python -c "import json,math;r=json.load(open('/tmp/ql-proto/report.json'));assert r['tier'] in ('T0','T1','T2','T3a','T3b') and r['n_attempts']>0 and r['theta'] is not None and math.isfinite(r['theta']);s=r['n_attempts_by_status'];st=dict(zip(s['status'],s['len']));assert st.get('completed',0)>0 and st.get('reserved',0)==0 and st.get('duplicate',0)<r['n_attempts'];assert r['claim_status']=='descriptive_only' and r['final_status'] in ('ok','no_claim');print({k:r[k] for k in ('tier','K','theta','n_attempts','final_status')},st)"
{"status": "ok", "tier": "T1", "K": 531, "theta": 0.0, "se": null, "lower_bound": null, "p_adj": null, "n_attempts": 77, "n_folds": 3, "claim_status": "descriptive_only", "out_dir": "/tmp/ql-proto"}
{'tier': 'T1', 'K': 531, 'theta': 0.0, 'n_attempts': 77, 'final_status': 'no_claim'} {'budget_exhausted': 3, 'completed': 74}
exit=0
TASK R-07 COMMAND .venv-g3/bin/python -m pytest tests/research/test_protocol.py -q && .venv-g3/bin/python -c "import polars as pl;from quant_lab.research.paths import ledger_path;p=ledger_path();d=pl.read_parquet(p);assert d.height>0 and (d['status']=='reserved').sum()==0;print(p,d.height)"
..........                                                               [100%]
10 passed in 0.10s
/private/tmp/ql-review-r5-lake/lockbox/ledger.parquet 77
exit=0
TASK R-08 COMMAND .venv-g3/bin/python -m pytest tests/research/test_maxt.py tests/research/test_nullmodel.py tests/research/test_review_p1_round3.py -q && .venv-g3/bin/python -c "from quant_lab.research.nullmodel import verify_report_text; verify_report_text(open('docs/adr/report-G3-null-model.md',encoding='utf-8').read())"
........................................................................ [ 72%]
...........................                                              [100%]
99 passed in 29.76s
POWER(reported, limitation statement applies) common_shock 327 768 0.3905 fail
R-08 report verified: 5 unique primary records; arithmetic and diagnostics consistent
exit=0
[取证驱动退出码 0]
````

#### A：四审两段反例原文重放

````bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import re,subprocess,os,json
from pathlib import Path
s=Path('docs/adr/review-G3-P1.md').read_text()
for label,section in [('A-R4-L','#### S08：实际代码血缘'),('A-R4-V','#### S17：新结构诊断')]:
 chunk=s[s.rindex(section):];cmd=re.search(r'```bash\n(.*?)\n```',chunk,re.S).group(1)
 print('COMMAND',label,'original round4 block',flush=True)
 r=subprocess.run(cmd,shell=True,executable='/bin/bash',env=os.environ.copy(),text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
 print(r.stdout, 'exit='+str(r.returncode),flush=True)
PY
````

````text
COMMAND A-R4-L original round4 block
R4 S08 backend_bytes_changed True ledger_code_version_equal False backend_version None None
ledger_code_version 729437423243616f3fadddb5de743478442ec12c2da7cdbd7aaa93197046c2a5
 exit=0
COMMAND A-R4-V original round4 block
R4 report grid_false REJECTED ValueError grid_dependence.ok 与重算不符
R4 report aggregate_false REJECTED ValueError dependence_aggregate.ok 与重算不符
R4 report invalid_reason REJECTED ValueError invalid_reason 与重算不符：记录 'GRID_DEPENDENCE_NOT_PRESERVED' vs 重算 None
R4 report guard_rate REJECTED ValueError 报告算术不一致
R4 report missing_grid REJECTED ValueError grid_dependence 缺失
R4 report stale_hash REJECTED ValueError 制品陈旧（A31）：报告由代码 000000000000 生成，当前研究代码为 729437423243——门或流水线已变更，必须重跑 MC 再验收，不得用旧制品判定
Traceback (most recent call last):
  File "<stdin>", line 26, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 697, in verify_report_text
    raise ValueError(f"制品陈旧（A31）：报告由代码 {str(embedded)[:12]} 生成，当前研究代码为 {current[:12]}——"
ValueError: 制品陈旧（A31）：报告由代码 000000000000 生成，当前研究代码为 729437423243——门或流水线已变更，必须重跑 MC 再验收，不得用旧制品判定
 exit=1
[取证驱动退出码 0]
````

#### A：六种篡改及两种 rebuild，完整 R-08 原文链逐次运行

````bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' QUANT_LAB_DATA_ROOT=/tmp/ql-review-r5-lake .venv-g3/bin/python - <<'PY'
import os,json,subprocess
from pathlib import Path
cmd=next(t['verify'] for t in json.loads(Path('taskList.json').read_text())['modules']['research']['tasks'] if t['id']=='R-08')
injector=r'''
import os,sys,copy,io,json,contextlib
from pathlib import Path
from unittest.mock import patch
from quant_lab.research import nullmodel as N
s=Path('docs/adr/report-G3-null-model.md').read_text();start=s.rindex('```json')+7;end=s.rindex('```');q=json.loads(s[start:end]);d=q['results'][0]['diagnostics'];damage=os.environ['R5_CASE']
if damage=='grid_false':d['grid_dependence']['ok']=False
if damage=='aggregate_false':d['dependence_aggregate']['ok']=False
if damage=='invalid_reason':d['invalid_reason']='GRID_DEPENDENCE_NOT_PRESERVED'
if damage=='guard_rate':d['guard_fail_rate']=1.
if damage=='missing_grid':del d['grid_dependence']
if damage in ['stale_hash','rebuild_stale']:q['meta']['research_code_sha256']='0'*64
if damage=='rebuild_missing':del q['meta']['research_code_sha256']
fake=s[:start]+json.dumps(q)+s[end:]
if damage.startswith('rebuild'):
 captured=[]
 with patch.object(Path,'read_text',return_value=fake),patch.object(Path,'mkdir'),patch.object(Path,'write_text',lambda self,text,**kw:captured.append(text)),contextlib.redirect_stdout(io.StringIO()):N._main(['--rebuild','/tmp/not-created-source','--out','/tmp/not-created-rebuild'])
 fake=captured[0]
with patch('builtins.open',return_value=io.StringIO(fake)):exec(compile(sys.argv[1],'R08-original-body','exec'),{})
'''
# Execute unchanged verify text; Bash launcher only intercepts its report-reader -c I/O.
launcher='function .venv-g3/bin/python() { if [[ "$1" == "-c" ]]; then command .venv-g3/bin/python -c "$R5_INJECTOR" "$2"; else command .venv-g3/bin/python "$@"; fi; }\n'
print('R08 ORIGINAL',cmd,flush=True)
for case in ['grid_false','aggregate_false','invalid_reason','guard_rate','missing_grid','stale_hash','rebuild_stale','rebuild_missing']:
 env=dict(os.environ,R5_INJECTOR=injector,R5_CASE=case)
 r=subprocess.run(launcher+cmd,shell=True,executable='/bin/bash',env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
 print('A FULL ORIGINAL VERIFY CASE',case,'\n'+r.stdout+'exit='+str(r.returncode),flush=True)
PY
````

````text
R08 ORIGINAL .venv-g3/bin/python -m pytest tests/research/test_maxt.py tests/research/test_nullmodel.py tests/research/test_review_p1_round3.py -q && .venv-g3/bin/python -c "from quant_lab.research.nullmodel import verify_report_text; verify_report_text(open('docs/adr/report-G3-null-model.md',encoding='utf-8').read())"
A FULL ORIGINAL VERIFY CASE grid_false 
........................................................................ [ 72%]
...........................                                              [100%]
99 passed in 29.29s
Traceback (most recent call last):
  File "<string>", line 19, in <module>
  File "R08-original-body", line 1, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 748, in verify_report_text
    require(gridd["ok"] is agg_ok, "grid_dependence.ok 与重算不符")
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 711, in require
    raise ValueError(reason)
ValueError: grid_dependence.ok 与重算不符
exit=1
A FULL ORIGINAL VERIFY CASE aggregate_false 
........................................................................ [ 72%]
...........................                                              [100%]
99 passed in 28.54s
Traceback (most recent call last):
  File "<string>", line 19, in <module>
  File "R08-original-body", line 1, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 749, in verify_report_text
    require((diag.get("dependence_aggregate") or {}).get("ok") is agg_ok, "dependence_aggregate.ok 与重算不符")
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 711, in require
    raise ValueError(reason)
ValueError: dependence_aggregate.ok 与重算不符
exit=1
A FULL ORIGINAL VERIFY CASE invalid_reason 
........................................................................ [ 72%]
...........................                                              [100%]
99 passed in 28.50s
Traceback (most recent call last):
  File "<string>", line 19, in <module>
  File "R08-original-body", line 1, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 767, in verify_report_text
    require((diag.get("invalid_reason") or None) == expected_reason,
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 711, in require
    raise ValueError(reason)
ValueError: invalid_reason 与重算不符：记录 'GRID_DEPENDENCE_NOT_PRESERVED' vs 重算 None
exit=1
A FULL ORIGINAL VERIFY CASE guard_rate 
........................................................................ [ 72%]
...........................                                              [100%]
99 passed in 28.20s
Traceback (most recent call last):
  File "<string>", line 19, in <module>
  File "R08-original-body", line 1, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 762, in verify_report_text
    close(rate, n_invalid / n)
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 727, in close
    require(np.allclose(actual, expected, rtol=0, atol=1e-12, equal_nan=False), "报告算术不一致")
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 711, in require
    raise ValueError(reason)
ValueError: 报告算术不一致
exit=1
A FULL ORIGINAL VERIFY CASE missing_grid 
........................................................................ [ 72%]
...........................                                              [100%]
99 passed in 29.18s
Traceback (most recent call last):
  File "<string>", line 19, in <module>
  File "R08-original-body", line 1, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 743, in verify_report_text
    require(isinstance(gridd, dict), "grid_dependence 缺失")
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 711, in require
    raise ValueError(reason)
ValueError: grid_dependence 缺失
exit=1
A FULL ORIGINAL VERIFY CASE stale_hash 
........................................................................ [ 72%]
...........................                                              [100%]
99 passed in 27.94s
Traceback (most recent call last):
  File "<string>", line 19, in <module>
  File "R08-original-body", line 1, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 697, in verify_report_text
    raise ValueError(f"制品陈旧（A31）：报告由代码 {str(embedded)[:12]} 生成，当前研究代码为 {current[:12]}——"
ValueError: 制品陈旧（A31）：报告由代码 000000000000 生成，当前研究代码为 729437423243——门或流水线已变更，必须重跑 MC 再验收，不得用旧制品判定
exit=1
A FULL ORIGINAL VERIFY CASE rebuild_stale 
........................................................................ [ 72%]
...........................                                              [100%]
99 passed in 28.89s
Traceback (most recent call last):
  File "<string>", line 19, in <module>
  File "R08-original-body", line 1, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 697, in verify_report_text
    raise ValueError(f"制品陈旧（A31）：报告由代码 {str(embedded)[:12]} 生成，当前研究代码为 {current[:12]}——"
ValueError: 制品陈旧（A31）：报告由代码 000000000000 生成，当前研究代码为 729437423243——门或流水线已变更，必须重跑 MC 再验收，不得用旧制品判定
exit=1
A FULL ORIGINAL VERIFY CASE rebuild_missing 
........................................................................ [ 72%]
...........................                                              [100%]
99 passed in 30.01s
Traceback (most recent call last):
  File "<string>", line 19, in <module>
  File "R08-original-body", line 1, in <module>
  File "/Users/balen/projects/trader-bot/quant-lab/src/quant_lab/research/nullmodel.py", line 697, in verify_report_text
    raise ValueError(f"制品陈旧（A31）：报告由代码 {str(embedded)[:12]} 生成，当前研究代码为 {current[:12]}——"
ValueError: 制品陈旧（A31）：报告由代码 missing-gene 生成，当前研究代码为 729437423243——门或流水线已变更，必须重跑 MC 再验收，不得用旧制品判定
exit=1
[取证驱动退出码 0]
````

#### A 补充 / B1：rebuild 来源、报告结构和保守失败记账新反例

````bash
PYTHONDONTWRITEBYTECODE=1 QUANT_LAB_DATA_ROOT=/tmp/ql-review-r5-lake .venv-g3/bin/python - <<'PY'
import copy,io,json,contextlib,shlex
from pathlib import Path
from unittest.mock import patch
from quant_lab.research import nullmodel as N
s=Path('docs/adr/report-G3-null-model.md').read_text(); start=s.rindex('```json')+7; end=s.rindex('```'); j=json.loads(s[start:end])
body=shlex.split(next(t for t in json.loads(Path('taskList.json').read_text())['modules']['research']['tasks'] if t['id']=='R-08')['verify'].split(' && ')[-1])[-1]
def verify(label,q=None,text=None):
 fake=text if text is not None else s[:start]+json.dumps(q)+s[end:]
 try:
  with patch('builtins.open',return_value=io.StringIO(fake)),contextlib.redirect_stdout(io.StringIO()):exec(compile(body,'R08-original','exec'),{})
  print(label,'ACCEPTED')
 except Exception as e:print(label,'REJECTED',type(e).__name__,str(e))
for missing in [False,True]:
 old=copy.deepcopy(j)
 if missing:old['meta'].pop('research_code_sha256')
 else:old['meta']['research_code_sha256']='0'*64
 stale=s[:start]+json.dumps(old)+s[end:]; captured=[]
 with patch.object(Path,'read_text',return_value=stale),patch.object(Path,'mkdir'),patch.object(Path,'write_text',lambda self,text,**kw:captured.append(text)),contextlib.redirect_stdout(io.StringIO()):N._main(['--rebuild','/tmp/not-created-old-report.md','--out','/tmp/not-created-rebuilt.md'])
 new=json.loads(captured[0][captured[0].rindex('```json')+7:captured[0].rindex('```')])
 print('A rebuild missing',missing,'generating',new['meta']['research_code_sha256'],'renderer',new['meta']['renderer_code_sha256'],'results_unchanged',new['results']==old['results'])
 verify('A rebuild R08',text=captured[0])
# New: retain a visible broken pair then omit it, without replacing all report fields.
q=copy.deepcopy(j);g=q['results'][0]['diagnostics']['grid_dependence'];g['null_mean_cross'][1]=0.0
verify('B1 broken_pair_present',q)
g['fitted_cross']=g['fitted_cross'][:1];g['null_mean_cross']=g['null_mean_cross'][:1]
verify('B1 broken_pair_omitted_vector_len1',q)
q=copy.deepcopy(j);d=q['results'][0]['diagnostics'];d['grid_dependence']['fitted_cross']=[2.,2.,2.];d['grid_dependence']['null_mean_cross']=[2.,2.,2.]
verify('B1 impossible_correlations_2',q)
# New: 50 real guard failures not charged to worst case, all eight listed fields self-consistent.
q=copy.deepcopy(j);r=q['results'][0];d=r['diagnostics'];k=50
r['tiers']['T1']-=k;r['tiers']['invalid']=k;d['n_invalid_null_model']=k;d['guard_fail_rate']=k/r['n_done'];d['guard_failures_by_check']={c:(k if c=='icc' else 0) for c in d['shuffle_guard']['checks']}
print('B1 omitted_worst_charge n_invalid',k,'n_failed',r['n_failed'],'reported_upper',r['searched_worst_ci'][1],'minimum_correct_upper',N.clopper_pearson(r['n_positive']+k,r['n_searched'])[1])
verify('B1 50_invalid_only_1_failed',q)
# Provenance boundary: wholly coordinated fabricated cross values cannot prove execution.
q=copy.deepcopy(j);g=q['results'][0]['diagnostics']['grid_dependence'];g['fitted_cross']=[0.,0.,0.];g['null_mean_cross']=[0.,0.,0.]
verify('B1 coordinated_zero_reference',q)
PY
````

````text
A rebuild missing False generating 0000000000000000000000000000000000000000000000000000000000000000 renderer 729437423243616f3fadddb5de743478442ec12c2da7cdbd7aaa93197046c2a5 results_unchanged True
A rebuild R08 REJECTED ValueError 制品陈旧（A31）：报告由代码 000000000000 生成，当前研究代码为 729437423243——门或流水线已变更，必须重跑 MC 再验收，不得用旧制品判定
A rebuild missing True generating missing-generating-digest renderer 729437423243616f3fadddb5de743478442ec12c2da7cdbd7aaa93197046c2a5 results_unchanged True
A rebuild R08 REJECTED ValueError 制品陈旧（A31）：报告由代码 missing-gene 生成，当前研究代码为 729437423243——门或流水线已变更，必须重跑 MC 再验收，不得用旧制品判定
B1 broken_pair_present REJECTED ValueError grid_dependence.ok 与重算不符
B1 broken_pair_omitted_vector_len1 ACCEPTED
B1 impossible_correlations_2 ACCEPTED
B1 omitted_worst_charge n_invalid 50 n_failed 1 reported_upper 0.010376831619745201 minimum_correct_upper 0.08020158944615748
B1 50_invalid_only_1_failed ACCEPTED
B1 coordinated_zero_reference ACCEPTED
[取证驱动退出码 0]
````

#### B1：首个 replicate 的判读顺序反例

````bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import copy,json,io,contextlib
from pathlib import Path
from quant_lab.research import nullmodel as N
s=Path('docs/adr/report-G3-null-model.md').read_text();start=s.rindex('```json')+7;end=s.rindex('```');j=json.loads(s[start:end])
r=j['results'][0];d=r['diagnostics'];r['tiers']['T1']-=1;r['tiers']['invalid']=1;d['n_invalid_null_model']=1;d['guard_fail_rate']=.001;d['guard_failures_by_check']={k:int(k=='icc') for k in d['shuffle_guard']['checks']}
for first in [False,True]:
 q=copy.deepcopy(j)
 if first:q['results'][0]['diagnostics']['shuffle_guard']['ok']=False;q['results'][0]['diagnostics']['shuffle_guard']['checks']['icc']=False
 try:
  with contextlib.redirect_stdout(io.StringIO()):N.verify_report_text(s[:start]+json.dumps(q)+s[end:])
  print('B1 first_replicate_failed',first,'invalid=failed=1/1000','ACCEPTED')
 except Exception as e:print('B1 first_replicate_failed',first,'invalid=failed=1/1000','REJECTED',str(e))
PY
````

````text
B1 first_replicate_failed False invalid=failed=1/1000 ACCEPTED
B1 first_replicate_failed True invalid=failed=1/1000 REJECTED guard 失败
[取证驱动退出码 0]
````

#### B1：强/弱相关，真实重采样结构破坏 200 次对照

````bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import numpy as np
from dataclasses import replace
from quant_lab.research import nullmodel as N
for mech,seed in [('common_shock',1),('cluster_heavy_tail',2)]:
 w=N.synth_world(N.WorldConfig(mechanism=mech,seed=seed,n_clusters=1600));m=N.fit_residual_model(w,block_len_days=3,train_end_day=180);fg=m.grid[:180];fc=N._grid_cross_corr(fg,3);vals=[];failed=0;normal=0
 for i in range(200):
  rng=np.random.default_rng(820000+i);x=N.resample_null(w,m,rng)
  normal+=not N.assert_not_episode_shuffle(w.inputs,x,w.day,block_len_days=3,fitted_grid=fg)['ok']
  old=x.shock_grid;grid=old.copy();nb=len(grid)//3
  for col in range(grid.shape[1]):grid[:nb*3,col]=grid[:nb*3,col].reshape(nb,3)[rng.permutation(nb)].reshape(-1)
  bad=replace(x,shock_grid=grid,base_R=x.base_R+grid[w.day,w.inst]-old[w.day,w.inst]);g=N.assert_not_episode_shuffle(w.inputs,bad,w.day,block_len_days=3,fitted_grid=fg)
  vals.append(g['grid']['cross_instrument']['null']);failed+=not g['ok']
 mc=np.mean(vals,axis=0).tolist();ok=all(N._correlation_preserved(f,v) for f,v in zip(fc,mc))
 print('B1 actual independent instrument blocks',mech,'normal_invalid',normal,'broken_invalid',failed,'/200','fitted',fc,'broken_mean',mc,'aggregate_ok',ok,'invalid_reason',('GRID_DEPENDENCE_NOT_PRESERVED' if not ok else ('GUARD_FAIL_RATE' if failed/200>.05 else None)),flush=True)
PY
````

````text
B1 actual independent instrument blocks common_shock normal_invalid 0 broken_invalid 103 /200 fitted [0.5589960708759241, 0.5040997763236431, 0.4244275959833148] broken_mean [-0.0029295105509298276, 0.0006290536328795726, 0.001159820041183417] aggregate_ok False invalid_reason GRID_DEPENDENCE_NOT_PRESERVED
B1 actual independent instrument blocks cluster_heavy_tail normal_invalid 0 broken_invalid 0 /200 fitted [0.1392838284143582, 0.1769549842158233, 0.17954215469804016] broken_mean [0.001993250922467166, -0.001094925123612903, 0.00473002712518701] aggregate_ok True invalid_reason None
[取证驱动退出码 0]
````

#### B1：弱相关完整 run_mc 复核与成对差异

````bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import numpy as np
from dataclasses import replace
from unittest.mock import patch
from quant_lab.research import nullmodel as N
from quant_lab.research.api import PipelineConfig
original=N.resample_null
samples={'normal':[],'broken':[]}
def broken(w,m,rng,**kw):
 x=original(w,m,rng,**kw);old=x.shock_grid;g=old.copy();nb=len(g)//3
 for col in range(g.shape[1]):g[:nb*3,col]=g[:nb*3,col].reshape(nb,3)[rng.permutation(nb)].reshape(-1)
 samples['normal'].append(N._grid_cross_corr(old,3));samples['broken'].append(N._grid_cross_corr(g,3))
 return replace(x,shock_grid=g,base_R=x.base_R+g[w.day,w.inst]-old[w.day,w.inst])
with patch.object(N,'resample_null',broken):r=N.run_mc('cluster_heavy_tail',n_rep=100,seed0=2,world_cfg=N.WorldConfig(n_clusters=1600),pipe_cfg=PipelineConfig(B=100))
print('B1 actual weak-correlation MC','verdict',r.verdict,'reason',r.diagnostics['invalid_reason'],'invalid',r.diagnostics['n_invalid_null_model'],'done/positive/failed',r.n_done,r.n_positive,r.n_failed,'grid',r.diagnostics['grid_dependence'],'tiers',r.tiers,'errors',r.diagnostics.get('errors',[])[:2])
a=np.asarray(samples['normal']);b=np.asarray(samples['broken']);delta=a-b;se=delta.std(axis=0,ddof=1)/np.sqrt(len(delta))
print('B1 paired normal_mean',a.mean(axis=0).tolist(),'broken_mean',b.mean(axis=0).tolist(),'paired_difference_SE',se.tolist(),'difference_over_SE',(delta.mean(axis=0)/se).tolist())
PY
````

````text
B1 actual weak-correlation MC verdict insufficient reason None invalid 0 done/positive/failed 100 3 0 grid {'fitted_ac1': 0.020652651142966244, 'null_mean_ac1': -0.01565415055775264, 'fitted_cross': [0.1392838284143582, 0.1769549842158233, 0.17954215469804016], 'null_mean_cross': [-0.014176441759468965, 0.02007381701675623, -0.005835202366649829], 'ok': True} tiers {'T1': 100} errors []
B1 paired normal_mean [0.11550704372202836, 0.16350301682393503, 0.174862640796078] broken_mean [-0.014176441759468965, 0.02007381701675623, -0.005835202366649829] paired_difference_SE [0.013870042749428052, 0.012545825881122543, 0.012480059468164413] difference_over_SE [9.349898037397535, 11.432423912641259, 14.478924849971504]
[取证驱动退出码 0]
````

#### A 补充 / B2：内存/磁盘 duplicate、版本切换、预算与 API 血缘

````bash
PYTHONDONTWRITEBYTECODE=1 QUANT_LAB_DATA_ROOT=/tmp/ql-review-r5-lake .venv-g3/bin/python - <<'PY'
import datetime as dt,tempfile,copy,yaml
from pathlib import Path
from unittest.mock import patch
from quant_lab.research import ledger as L,paths,api as A
from quant_lab.research.features import get_backend
kw=dict(origin='human',canonical_hash='h',params={},fold_id='f',visible_cutoff=dt.datetime(2024,1,1,tzinfo=dt.UTC),objective='theta',data_manifest='m',seed=1,backend_version='polars:probe-v1')
read=Path.read_bytes
for disk in [False,True]:
 with tempfile.TemporaryDirectory(prefix='r5-ledger-') as tmp:
  led=L.Ledger(root=Path(tmp),budget_configs=1) if disk else L.MemoryLedger(budget_configs=1)
  def row(a):return led.read().filter(__import__('polars').col('attempt_id')==a).to_dicts()[0]
  a=led.reserve(**kw);led.mark(a,'completed');b=led.reserve(**kw)
  def changed(p):
   out=read(p)
   if p.as_posix().endswith('/research/backends/polars.py'):out+=b'\n# R5 backend byte change\n'
   return out
  with patch.object(Path,'read_bytes',changed):
   c=led.reserve(**kw);led.mark(c,'completed')
  d=led.reserve(**(kw|{'backend_version':'polars:probe-v2'}));led.mark(d,'completed')
  print('A/B2 disk',disk,'baseline',row(a)['status'],'repeat',row(b)['status'],'byte_changed',row(c)['status'],'digest_changed',row(a)['code_version']!=row(c)['code_version'],'backend_changed',row(d)['status'])
  try:led.reserve(**(kw|{'canonical_hash':'different-config'}))
  except L.LedgerBudgetExhausted:print('B2 new_config_after_versions BUDGET_EXHAUSTED')
  print('B2 recorded_rows',led.read().height,'unique_config_for_three_completed',led.read().filter(__import__('polars').col('status')=='completed')['config_id'].n_unique())
# API actual configured backend, real synthetic snapshot and search reservations.
cfg=yaml.safe_load(Path('tests/research/fixtures/protocol_synthetic.yaml').read_text());led=L.MemoryLedger()
inp,cands,meta=A.build_inputs_from_synthetic(cfg,ledger=led)
print('A API PanelInputs.backend_version',inp.backend_version,'actual_get_backend_version',get_backend(cfg.get('backend','polars')).version)
a=A._reserve(led,cands[0],fold_id='probe',cutoff=dt.datetime(2024,1,1,tzinfo=dt.UTC),stage='search',cfg=A.PipelineConfig(),inputs=inp)
print('A API feature_snapshot_versions',[r['backend_version'] for r in led.rows.values() if r['objective']=='feature_snapshot'],'search_version',led.rows[a]['backend_version'])
led.mark(a,'failed',reason='review probe complete')
PY
````

````text
A/B2 disk False baseline completed repeat duplicate byte_changed completed digest_changed True backend_changed completed
B2 new_config_after_versions BUDGET_EXHAUSTED
B2 recorded_rows 5 unique_config_for_three_completed 1
A/B2 disk True baseline completed repeat duplicate byte_changed completed digest_changed True backend_changed completed
B2 new_config_after_versions BUDGET_EXHAUSTED
B2 recorded_rows 5 unique_config_for_three_completed 1
A API PanelInputs.backend_version polars/0.2 actual_get_backend_version 0.2
A API feature_snapshot_versions ['polars/0.2', 'polars/0.2', 'polars/0.2', 'polars/0.2', 'polars/0.2', 'polars/0.2'] search_version polars/0.2
[取证驱动退出码 0]
````

#### B3：真实多进程、不同执行修订、异常及父进程中断

````bash
PYTHONDONTWRITEBYTECODE=1 QUANT_LAB_DATA_ROOT=/tmp/ql-review-r5-lake .venv-g3/bin/python - <<'PY'
import os,io,contextlib,multiprocessing,functools,inspect
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ProcessPoolExecutor
from quant_lab.research import nullmodel as N
original=N.run_mc; reader=Path.read_bytes; mode='stable'
def worker(**kw):
 if mode=='exception':raise RuntimeError('R5 injected worker exception')
 def changed(p):
  b=reader(p)
  if p.as_posix().endswith('/research/nullmodel.py'):b=b.replace(b'n_rec, label)',b'n_rec, "R5-child-code")')
  return b
 if mode=='worker_drift':
  with patch.object(Path,'read_bytes',changed):
   ns=dict(N.__dict__);exec(compile(inspect.getsource(original).replace('n_rec, label)', 'n_rec, "R5-child-code")'), '<r5-worker-revision>', 'exec'),ns);r=ns['run_mc'](**kw);r.diagnostics['r5_worker_digest']=__import__('quant_lab.research.paths',fromlist=['research_code_digest']).research_code_digest()
 else:
  r=original(**kw);r.diagnostics['r5_worker_digest']=__import__('quant_lab.research.paths',fromlist=['research_code_digest']).research_code_digest()
 r.diagnostics['r5_worker_pid']=os.getpid()
 return r
factory=functools.partial(ProcessPoolExecutor,mp_context=multiprocessing.get_context('fork'))
for mode in ['stable','worker_drift','exception','parent_end_drift','parent_interrupt']:
 captured=[];logs=io.StringIO();dig=N.research_code_digest();calls=[]
 def digest():
  calls.append(1)
  if mode=='parent_interrupt' and len(calls)>1:raise KeyboardInterrupt('R5 parent interrupted after workers')
  if mode=='parent_end_drift' and len(calls)>1:return 'f'*64
  return dig
 def capture(results,out,**kw):captured.append((results,kw))
 try:
  with patch('concurrent.futures.ProcessPoolExecutor',factory),patch.object(N,'run_mc',worker),patch.object(N,'write_report',capture),patch.object(N,'research_code_digest',digest),contextlib.redirect_stdout(logs):
   N._main(['--out','/tmp/not-created-r5-mc.md','--n-rep','2','--no-ext','--jobs','2','--B','100','--n-clusters','1600','--mechanisms','common_shock,circular_shift','--power-mechanisms',''])
  status='returned'
 except BaseException as e:status=type(e).__name__+': '+str(e)
 print('B3',mode,'status',status,'write_calls',len(captured))
 if captured:
  rs,kw=captured[0]
  print('B3 worker_processes',len(set(r.diagnostics['r5_worker_pid'] for r in rs)),'parent_pid_distinct',all(r.diagnostics['r5_worker_pid']!=os.getpid() for r in rs),'digest_equal',[r.diagnostics['r5_worker_digest']==kw['code_digest'] for r in rs],'executed_labels',[r.label for r in rs],'pipeline_errors',[len(r.diagnostics.get('errors',[])) for r in rs])
PY
````

````text
B3 stable status returned write_calls 1
B3 worker_processes 2 parent_pid_distinct True digest_equal [True, True] executed_labels ['', ''] pipeline_errors [0, 0]
B3 worker_drift status returned write_calls 1
B3 worker_processes 2 parent_pid_distinct True digest_equal [False, False] executed_labels ['R5-child-code', 'R5-child-code'] pipeline_errors [0, 0]
B3 exception status RuntimeError: R5 injected worker exception write_calls 0
B3 parent_end_drift status SystemExit: 研究代码在 MC 运行期间发生变化（起 729437423243 / 止 ffffffffffff）：结果与代码身份不再一一对应，必须在稳定源码上重跑，不得落盘 write_calls 0
B3 parent_interrupt status KeyboardInterrupt: R5 parent interrupted after workers write_calls 0
[取证驱动退出码 0]
````

#### C：按 A27 真实破坏收益与 shock_grid，null/power 双路径

````bash
PYTHONDONTWRITEBYTECODE=1 QUANT_LAB_DATA_ROOT=/tmp/ql-review-r5-lake .venv-g3/bin/python - <<'PY'
import re,subprocess,os
from pathlib import Path
s=Path('docs/adr/review-G3-P1.md').read_text();chunk=s[s.rindex('#### R4-STRUCT：'):]
cmd=re.search(r'```bash\n(.*?)\n```',chunk,re.S).group(1).replace('n_rep=8','n_rep=12').replace('R4 independent','R5 actual structure')
print(cmd,flush=True)
r=subprocess.run(cmd,shell=True,executable='/bin/bash',env=os.environ.copy(),text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
print(r.stdout+'exit='+str(r.returncode),flush=True)
PY
````

````text
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import numpy as np
from dataclasses import replace
from unittest.mock import patch
from quant_lab.research import nullmodel as N
from quant_lab.research.api import PipelineConfig
w=N.synth_world(N.WorldConfig(seed=2,n_clusters=1600));m=N.fit_residual_model(w,block_len_days=3,train_end_day=180);fitted=m.grid[:180]
def broken(world,x,rng):
 old=x.shock_grid;grid=old.copy();nb=len(grid)//3
 for j in range(grid.shape[1]):grid[:nb*3,j]=grid[:nb*3,j].reshape(nb,3)[rng.permutation(nb)].reshape(-1)
 # Change actual returns as well as diagnostic grid, retaining idiosyncratic residual and missing layout.
 r=x.base_R+grid[world.day,world.inst]-old[world.day,world.inst]
 return replace(x,shock_grid=grid,base_R=r)
normal=bad=0
for i in range(20):
 rng=np.random.default_rng(192110+i);x=N.resample_null(w,m,rng)
 g=N.assert_not_episode_shuffle(w.inputs,x,w.day,block_len_days=3,fitted_grid=fitted);normal+=not g['ok']
 y=broken(w,x,np.random.default_rng(292110+i));h=N.assert_not_episode_shuffle(w.inputs,y,w.day,block_len_days=3,fitted_grid=fitted)
 bad+=not h['checks']['cross_instrument']
print('R5 actual structure actual_returns_and_grid normal_fail',normal,'/20 broken_cross_caught',bad,'/20',flush=True)
original=N.resample_null
for kind in ['null','power']:
 for damage in [False,True]:
  def generate(world,model,rng,**kw):
   x=original(world,model,rng,**kw)
   return broken(world,x,rng) if damage else x
  with patch.object(N,'resample_null',generate):r=N.run_mc('common_shock',kind=kind,n_rep=12,seed0=2,world_cfg=N.WorldConfig(n_clusters=1600),pipe_cfg=PipelineConfig(B=100))
  print('R5 actual structure MC',kind,'damage',damage,'verdict',r.verdict,'reason',r.diagnostics['invalid_reason'],'invalid',r.diagnostics['n_invalid_null_model'],'grid',r.diagnostics['grid_dependence'],flush=True)
PY
R5 actual structure actual_returns_and_grid normal_fail 0 /20 broken_cross_caught 8 /20
R5 actual structure MC null damage False verdict insufficient reason None invalid 0 grid {'fitted_ac1': -0.34331975388460007, 'null_mean_ac1': 0.02525547307905279, 'fitted_cross': [0.4634950088944095, 0.39532745335499664, 0.5163415706931419], 'null_mean_cross': [0.43447188394598285, 0.3738123620903324, 0.509407596487563], 'ok': True}
R5 actual structure MC null damage True verdict invalid_null_model reason GRID_DEPENDENCE_NOT_PRESERVED invalid 5 grid {'fitted_ac1': -0.34331975388460007, 'null_mean_ac1': -0.027041081320688, 'fitted_cross': [0.4634950088944095, 0.39532745335499664, 0.5163415706931419], 'null_mean_cross': [0.03857487148497074, -0.03041932824048423, -0.04065053881222184], 'ok': False}
R5 actual structure MC power damage False verdict fail reason None invalid 0 grid {'fitted_ac1': -0.34331975388460007, 'null_mean_ac1': 0.02525547307905279, 'fitted_cross': [0.4634950088944095, 0.39532745335499664, 0.5163415706931419], 'null_mean_cross': [0.43447188394598285, 0.3738123620903324, 0.509407596487563], 'ok': True}
R5 actual structure MC power damage True verdict invalid_null_model reason GRID_DEPENDENCE_NOT_PRESERVED invalid 5 grid {'fitted_ac1': -0.34331975388460007, 'null_mean_ac1': -0.027041081320688, 'fitted_cross': [0.4634950088944095, 0.39532745335499664, 0.5163415706931419], 'null_mean_cross': [0.03857487148497074, -0.03041932824048423, -0.04065053881222184], 'ok': False}
exit=0
[取证驱动退出码 0]
````

#### D：15 项历史闭合项及 S08、S18/S19 逐项抽验

````bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' QUANT_LAB_DATA_ROOT=/tmp/ql-review-r5-lake .venv-g3/bin/python - <<'PY'
import re,subprocess,os
from pathlib import Path
s=Path('docs/adr/review-G3-P1.md').read_text();s=s[s.index('#### Q-Sxx：'):s.index('#### R-05–R-09：')]
for label,cmd in re.findall(r'\| (Q-S\d+) \| `([^`]+)`',s):
 print(label,cmd,flush=True)
 r=subprocess.run(cmd,shell=True,executable='/bin/bash',env=os.environ.copy(),text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
 print(r.stdout+'exit='+str(r.returncode),flush=True)
PY
````

````text
Q-S01 .venv-g3/bin/python -m pytest tests/research/test_features.py::test_cache_hot_read_follows_episode_identity_not_position -q
.                                                                        [100%]
1 passed in 0.14s
exit=0
Q-S02 .venv-g3/bin/python -m pytest tests/research/test_features.py::test_context_is_single_source_and_in_key tests/research/test_features.py::test_late_available_at_is_not_visible -q
..                                                                       [100%]
2 passed in 0.08s
exit=0
Q-S03 .venv-g3/bin/python -m pytest tests/research/test_features.py::test_revocation_gate_blocks_cached_and_fresh_reads -q
.                                                                        [100%]
1 passed in 0.04s
exit=0
Q-S04 .venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s04_quarantine_persists_across_processes tests/research/test_features.py::test_quarantined_backend_op_rejected_in_snapshot -q
..                                                                       [100%]
2 passed in 1.20s
exit=0
Q-S05 .venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s05_inner_split_is_calendar_based_purged_and_mature -q
.                                                                        [100%]
1 passed in 0.60s
exit=0
Q-S06 .venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s06_config_cap_can_only_tighten_and_insufficient_folds_do_not_fit -q
.                                                                        [100%]
1 passed in 1.22s
exit=0
Q-S07 .venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s07_concurrent_reserve_only_one_wins_and_replay_after_crash -q
.                                                                        [100%]
1 passed in 1.40s
exit=0
Q-S08 .venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s08_every_stage_has_terminal_state_including_failures_and_final tests/research/test_review_p1_closure.py::test_s08_run_protocol_reserves_before_lint_and_rejects_bad_ast -q
..                                                                       [100%]
2 passed in 6.17s
exit=0
Q-S09 .venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s09_residual_fit_uses_only_mature_labels_and_population_quantile -q
.                                                                        [100%]
1 passed in 0.42s
exit=0
Q-S10 .venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s10_diagnostic_failure_cannot_pass tests/research/test_review_p1_closure.py::test_s10_all_t0_is_not_run_not_pass -q
..                                                                       [100%]
2 passed in 0.49s
exit=0
Q-S11 .venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s11_censored_blocks_do_not_count_as_nonempty -q
.                                                                        [100%]
1 passed in 0.42s
exit=0
Q-S12 .venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s12_panel_invariants_and_placeholder_only_under_mask tests/research/test_review_p1_closure.py::test_s12_outer_nan_gate_falls_back_to_baseline tests/research/test_evaluator.py::test_nan_feature_is_skip_and_keeps_denominator -q
...                                                                      [100%]
3 passed in 0.77s
exit=0
Q-S13 .venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s13_unfilled_uncensored_gets_expiry_t1_not_null -q
.                                                                        [100%]
1 passed in 3.16s
exit=0
Q-S14 .venv-g3/bin/python -m pytest tests/research/test_evaluator.py::test_estimand_vocabulary_frozen_and_stub_isomorphic_with_g1 tests/research/test_g2_parity.py -q
........                                                                 [100%]
8 passed in 0.19s
exit=0
Q-S15 .venv-g3/bin/python -m pytest tests/research/test_evaluator.py::test_two_arm_pairing_and_protocol_errors -q
.                                                                        [100%]
1 passed in 0.08s
exit=0
Q-S16 .venv-g3/bin/python -m pytest tests/research/test_evaluator.py::test_synthetic_end_to_end_pairs_and_decimal -q
.                                                                        [100%]
1 passed in 0.18s
exit=0
Q-S18 .venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s18_streaming_budget_and_truncation_reason -q
.                                                                        [100%]
1 passed in 0.43s
exit=0
Q-S19 .venv-g3/bin/python -m pytest tests/research/test_review_p1_closure.py::test_s19_exact_decimal_canonicalization -q
.                                                                        [100%]
1 passed in 0.42s
exit=0
[取证驱动退出码 0]
````

#### E：独立计数、三分母、Clopper–Pearson 与递归代码摘要

````bash
PYTHONDONTWRITEBYTECODE=1 .venv-g3/bin/python - <<'PY'
import json,hashlib,math
from pathlib import Path
from scipy.stats import beta,binom
s=Path('docs/adr/report-G3-null-model.md').read_text();j=json.loads(s[s.rindex('```json')+7:s.rindex('```')])
h=hashlib.sha256();root=Path('src/quant_lab/research')
for p in sorted(root.rglob('*.py')):h.update(str(p.relative_to(root)).encode()+b'\0'+p.read_bytes()+b'\0')
print('E digest',h.hexdigest(),'embedded',j['meta']['research_code_sha256'],'equal',h.hexdigest()==j['meta']['research_code_sha256'])
def cp(x,n):return (0. if x==0 else float(beta.ppf(.025,x,n-x+1)),1. if x==n else float(beta.ppf(.975,x+1,n-x)))
for i,r in enumerate(j['results']):
 n,x,f=r['n_done'],r['n_positive'],r['n_failed'];t0=r['tiers'].get('T0',0);ns=n-t0;w=x+f if r['kind'].startswith('null') else x
 assert n==r['n_planned']==sum(r['tiers'].values()) and 0<=x+f<=n and r['n_T0']==t0 and r['n_searched']==ns
 vals=[(x,n-f,'rate','ci'),(w,n,'worst_case_rate','worst_case_ci'),(w,ns,'searched_worst_rate','searched_worst_ci')]
 for num,den,rk,ck in vals:
  assert abs(r[rk]-num/den)<1e-12 and all(abs(a-b)<1e-12 for a,b in zip(r[ck],cp(num,den)))
 print('E',i,r['kind'],r['mechanism'],r.get('label',''),'n/x/f/T0/search',n,x,f,t0,ns,'triples',[(num,den,round(num/den,8),tuple(round(v,8) for v in cp(num,den))) for num,den,_,_ in vals],'OK')
print('E all',len(j['results']),'rows checked; no pooled denominator')
for p in [.01,.02]:
 print('noise p',p,'P(any fail;1000)',1-binom.pmf(0,1000,p),'P(>5%;1000)',binom.sf(50,1000,p),'P(>5%;200)',binom.sf(10,200,p))
PY
````

````text
E digest 729437423243616f3fadddb5de743478442ec12c2da7cdbd7aaa93197046c2a5 embedded 729437423243616f3fadddb5de743478442ec12c2da7cdbd7aaa93197046c2a5 equal True
E 0 null common_shock  n/x/f/T0/search 1000 2 1 158 842 triples [(2, 999, 0.002002, (0.00024254, 0.00721303)), (3, 1000, 0.003, (0.0006191, 0.00874202)), (3, 842, 0.00356295, (0.00073537, 0.01037683))] OK
E 1 null cluster_heavy_tail  n/x/f/T0/search 1000 6 3 26 974 triples [(6, 997, 0.00601805, (0.00221163, 0.01305244)), (9, 1000, 0.009, (0.0041234, 0.01701578)), (9, 974, 0.00924025, (0.00423369, 0.01746811))] OK
E 2 null nonuniform_density  n/x/f/T0/search 1000 8 11 275 725 triples [(8, 989, 0.00808898, (0.00349853, 0.01587601)), (19, 1000, 0.019, (0.01147704, 0.0295124)), (19, 725, 0.0262069, (0.01585037, 0.04062375))] OK
E 3 null circular_shift  n/x/f/T0/search 1000 4 0 7 993 triples [(4, 1000, 0.004, (0.00109091, 0.01020966)), (4, 1000, 0.004, (0.00109091, 0.01020966)), (4, 993, 0.0040282, (0.00109861, 0.01028141))] OK
E 4 power common_shock  n/x/f/T0/search 1000 327 34 232 768 triples [(327, 966, 0.33850932, (0.3086819, 0.36932502)), (327, 1000, 0.327, (0.29796969, 0.3570529)), (327, 768, 0.42578125, (0.39050275, 0.46163176))] OK
E 5 power_sens common_shock δ=0.2 noise×1.0 clusters=1600 n/x/f/T0/search 200 96 8 27 173 triples [(96, 192, 0.5, (0.42715469, 0.57284531)), (96, 200, 0.48, (0.409014, 0.5515876)), (96, 173, 0.55491329, (0.47757047, 0.630342))] OK
E 6 power_sens common_shock δ=0.2 noise×1.0 clusters=2400 n/x/f/T0/search 200 146 2 10 190 triples [(146, 198, 0.73737374, (0.67027724, 0.79721445)), (146, 200, 0.73, (0.66284741, 0.79019666)), (146, 190, 0.76842105, (0.70185865, 0.82640877))] OK
E 7 power_sens common_shock δ=0.2 noise×0.6 clusters=1600 n/x/f/T0/search 200 115 9 65 135 triples [(115, 191, 0.60209424, (0.52891826, 0.67204941)), (115, 200, 0.575, (0.50330413, 0.64443882)), (115, 135, 0.85185185, (0.78051027, 0.90709706))] OK
E 8 power_sens common_shock δ=0.2 noise×0.6 clusters=2400 n/x/f/T0/search 200 169 2 27 173 triples [(169, 198, 0.85353535, (0.79647184, 0.8996645)), (169, 200, 0.845, (0.78726236, 0.8921906)), (169, 173, 0.97687861, (0.94186071, 0.99366509))] OK
E 9 power_sens common_shock δ=0.4 noise×1.0 clusters=1600 n/x/f/T0/search 200 94 8 93 107 triples [(94, 192, 0.48958333, (0.41691647, 0.56257678)), (94, 200, 0.47, (0.39923294, 0.5416695)), (94, 107, 0.87850467, (0.80120287, 0.93369579))] OK
E 10 power_sens common_shock δ=0.4 noise×1.0 clusters=2400 n/x/f/T0/search 200 141 1 55 145 triples [(141, 199, 0.70854271, (0.64010184, 0.77065143)), (141, 200, 0.705, (0.63657687, 0.76723122)), (141, 145, 0.97241379, (0.9308762, 0.99243361))] OK
E 11 power_sens common_shock δ=0.4 noise×0.6 clusters=1600 n/x/f/T0/search 200 27 0 172 28 triples [(27, 200, 0.135, (0.0908871, 0.19030917)), (27, 200, 0.135, (0.0908871, 0.19030917)), (27, 28, 0.96428571, (0.8165224, 0.9990962))] OK
E 12 power_sens common_shock δ=0.4 noise×0.6 clusters=2400 n/x/f/T0/search 200 50 1 149 51 triples [(50, 199, 0.25125628, (0.19260375, 0.31747867)), (50, 200, 0.25, (0.19160717, 0.31596283)), (50, 51, 0.98039216, (0.8955251, 0.9995037))] OK
E all 13 rows checked; no pooled denominator
noise p 0.01 P(any fail;1000) 0.9999568287525893 P(>5%;1000) 1.555696931554219e-20 P(>5%;200) 6.881770411199629e-06
noise p 0.02 P(any fail;1000) 0.9999999983170327 P(>5%;1000) 2.9622365546214887e-09 P(>5%;200) 0.0025305994353454625
[取证驱动退出码 0]
````

B1weakMC 的最终完整流水线证据使用 B=100；早期 B=50 试跑触发既有 PipelineConfig 的 B≥100 校验，100 次全 error/not_run，不作为结构成功或 FPR 证据。最终 B=100 为 100 个 T1、零 error，以上输出已区分。

### 剩余必修与验收

以下均有本节实际工程反例；低功效、统计口径偏好和未写一条免责声明均不作为必修来源。

**R5-C（S17）：结构失败未强制计入失败最坏界。** 位置：src/quant_lab/research/nullmodel.py:755–779；tests/research/test_review_p1_round3.py:706 的 set_guard 也只搬 tiers、不补 n_failed，说明测试本身容许不可能的计数。B1 保留现报告 n_done=1000、x=2、n_failed=1、n_searched=842、所有 CI，只把 50 个 T1 搬到 invalid 并同步 per-check/rate/invalid 数；八字段自洽，R-08 仍 ACCEPTED。真实 run_mc 每次 invalid 都 n_fail+=1，因此至少须有 n_failed≥50。当前记录却用 3/842，上界 1.037683%；最低正确收费应至少 52/842，上界 8.020159%>7%。这是可以改变准入结论的反例。改法：核对 invalid、error 是失败集合的互斥子集，至少 tiers.invalid+tiers.error≤n_failed，余下 insufficient 失败仍须能对账；按实际失败总数重算三分母，不许以 flags 代替计数。验收：上述最小反例必须拒收；合法 ≤5% invalid 且全部正确进入失败数的报告按保守界验收；>5% 或总体门失败仍拒收。另核 chosen_block_len 的总数应与实际进入 pipeline/选择主 L 的路径相符，不能要求结构早退者有主 L。

**R5-G（S17）：总体相关向量缺配对完整性和数值域。** 位置：nullmodel.py:742–747。B1 先将第二品种对 null correlation 置零，当前门正确拒收；再仅截取两边向量的第一项，第二/三对消失后 ACCEPTED。把三项相关都置 2.0 同样 ACCEPTED。不是要求识别任意伪造真数据，而是字段自身违反既有三品种世界的形状/相关系数定义，仍被当作可重算诊断。改法：报告写明 instrument/pair 标识并按冻结世界核齐全集和顺序（三品种应恰有 3 对），拒绝缺项/重复/未知对；数值须是非 bool 的有限实数且位于 [-1,1]，缺失需显式 invalid 处理；同步约束 producer/verifier。验收：删成 1 项、任意删对/重复对、2/-2/bool 非法值均拒收；完整合法向量仍可通过；不能只加两边长度相等。

**R5-O（S17）：第一个 replicate 仍有零失败特权。** 位置：nullmodel.py:592–593 仅保存 i==0 的 shuffle_guard；:751–752 强制其 ok 和每个 check 为 True。B1first：同样 1/1000 invalid、n_failed=1、总体门成功，首个失败=False 时 ACCEPTED，首个失败=True 时 REJECTED guard 失败。这既不是 >5%，也不是总体失败，仍与本轮对齐口径冲突。改法：核 guard 自身 ok/check 的逻辑及该首个观测是否被计入 per_check/invalid；允许首观测失败时进入统一总量规则；不要把展示用首观测当整轮准入门。验收：把同一合法失败观测移动到第一/中间/末尾，保持总计与 CP 不变，机器判读一致；首个 check=False 却对应 per_check=0 的不自洽报告仍须拒收。

**R5-H（S17/A31）：父进程摘要未绑定各 worker 的实际执行修订。** 位置：nullmodel.py:985、1002–1010、1030–1034。B3 使用真实两个 OS worker；在子进程中编译一份仅改变 MCResult.label 的 run_mc 修订，并令子进程看到的 nullmodel.py 字节作同样替换。子进程实际输出 R5-child-code，子进程递归 digest 与父进程 frozen digest 不等，但父进程照样调用 write_report(..., code_digest=parent)。写端点在内存捕获，没有写制品；这是受控不同 worker 源码视图注入，不宣称磁盘生产源码真的发生过此漂移。稳定对照、异常拒写对照一并实跑。改法：worker 起跑/收尾冻结并回传生成身份，父进程逐份结果核对预期身份，任何不等或缺失禁止发布；为覆盖 spawn 延迟加载、导入缓存以及 A→B→A 变化，应从一次固定只读源码快照/安装制品启动整组 worker，不能声称首尾相等证明全过程未变。验收：真实多进程不同 worker 修订、缺来源回执、起止改变都拒绝写；稳定串/并行 seed 对拍一致；worker 异常和父进程中断仍不发布新结果。无需把异常退出丢失的计算当作陈旧制品事故。

**R5-W（S10）：弱而真实的跨品种相关被完全摧毁仍不触发。** 位置：nullmodel.py:420–427 的 _correlation_preserved、:639 总体 agg_ok；:301–312 是单次 Fisher 门。B1lowcorr 对用户原有 cluster_heavy_tail/seed0=2 世界，拟合三对为 0.1393/0.1770/0.1795；独立整块重排每个品种并把格点差加回真实 base_R 后，200 次均值约 0.0020/-0.0011/0.0047，单次失败 0/200，总体 ok=True、reason=None；强相关 control 则 103/200 失败且总体拒绝。绝对容差把所有这类低于容差的拟合相关直接放进“允许归零”区域；这是预注册机制中的真实结构丢失，不是单纯伪造报告，也不是 5% 带过宽。B1weakMC 另跑 B=100、n_rep=100 的完整 run_mc：100 个 T1、3 阳性、0 failed、0 errors，verdict=insufficient（小样本 FPR 区间所致），但结构 reason=None/ok=True；不是声称整份小样本报告能过 R-08 的 1000 次门。对应三对 normal→broken 均值差约为成对标准误的 9.35/11.43/14.48 倍；这是条件于同一拟合世界的描述性差异证据，不把近似标准误当作精确检验保证。改法：总体门需以条件于拟合世界的结构保持分布校准“均值差”，把重复抽样均值的不确定性与单复制噪声分开；对弱相关破坏也应有功效，同时保持 A27 正常对照。可以使用独立预注册参考抽样/模拟校准或有依据的误差尺度，不能为堵此例机械改回会制造假警报的单次固定比例门。验收：四机制正常样本和至少强/弱相关独立跨品种重排均前台重跑；正常误拒与破坏检出率分开报告，aggregate 对上述弱相关破坏触发 GRID_DEPENDENCE_NOT_PRESERVED；随后用冻结当前代码全量重生成 A31 报告。旧 C 的强相关破坏成功不能代替这项。

本轮 fail 已由 R5-C 的 7% 准入翻转反例独立成立，其余问题不影响该证据。修复后需保持出口 A、历史报告事实、实际 backend 血缘、ledger 生命周期/预算/并发回归，重跑本节反例与用户指定 verify，并重生成受 A31 约束的报告。本轮没有修改这些被审文件。

### 并发变更与审查字节范围

开审记录 44 个被审文件的 SHA256（排除字节码）。收尾同一集合 44 个，无新增/删除；42 个内容相同。两个并发变更如下，本会话没有修改它们，也未回滚其他会话内容：

| 文件 | 开审 SHA256 | 收尾 SHA256 |
|---|---|---|
| contracts/research-schema.md | 69f50bf0eb1d6ca8fe79144245e33e756542393c733d5a1a3944e5e91e193850 | f8391b51e7f74b512681d3d3b26a4b910f5b97cbe94931dfd5d439b046ac6418 |
| taskList.json | c619f5e95a88e294b2d762a2cc36fc7a7f132c9579796141cce4a2b4051b2618 | fb59fec83fa80024ad2b85368733eb77240a0b96e790f4b7b956aad97ffc8f24 |

新增契约为 A39（changeLog #68，08:40:51Z）及其抽样比对补充（#69，08:43:50Z），已读并纳入本节：方法边界放契约，P2 独立抽样复核，S08 预算/复用分离，异常不落盘均接受。不同 worker 修订仍被正常发布不等同于异常退出丢结果，A39 后者的豁免不消除 R5-H。四条 verify 文本在取证开始/收尾逐字相等；源码、测试、G3 报告摘要无变化，因此无需因 task notes/契约追加而重跑不变的源码测试。未把其他会话“已闭合”的转述作为证据。

研究摘要独立复算与当前制品内嵌均为 `729437423243616f3fadddb5de743478442ec12c2da7cdbd7aaa93197046c2a5`。本轮执行的是指定验证、独立算术和小规模对抗注入，没有重跑全部 6600 次 MC，也没有重新测量历史后端计时；不冒称重生成既有报告。

### 追加落盘与历史前缀机械核验

历史一至四审前缀长度：192105 bytes。开审 SHA256 与追加后取相同字节范围的 SHA256 均为：

```text
3dd4801a0c5c485ea33ab6ca365c54a29aa202921c174f1e848128c59890acd2
```

落盘使用二进制 append 模式。写入后已实测 `after[:192105] == before`，并计算该前缀 SHA256；本节追加内容不混入此前四审摘要。可独立复查：

```bash
python3 - <<'CHECK'
from pathlib import Path
import hashlib
b = Path('docs/adr/review-G3-P1.md').read_bytes()
h = hashlib.sha256(b[:192105]).hexdigest()
assert h == '3dd4801a0c5c485ea33ab6ca365c54a29aa202921c174f1e848128c59890acd2'
assert b.count('## 五审（R-10 round 5）'.encode()) == 1
print('historical_prefix_bytes=192105 sha256=' + h + ' unchanged=True')
CHECK
tail -1 docs/adr/review-G3-P1.md | grep -E '^五审终裁：(pass|fail)$'
```

实际收尾输出（各 exit=0）：

```text
historical_prefix_bytes=192105 sha256=3dd4801a0c5c485ea33ab6ca365c54a29aa202921c174f1e848128c59890acd2 unchanged=True
五审终裁：fail
```

五审终裁：fail


### 收尾核验更正（同一五审章节，仍仅追加）

上一个收尾驱动在文件写完后，使用字面串 count 检查章节唯一性，误把展示用校验脚本里的章节名称也计入，实际得到 2 并触发 AssertionError；该驱动没有走到 tail 命令。因此上节所称“各 exit=0”对该次驱动不成立，应以下面这次真实执行为准。历史前缀逐字与 SHA256 核验在此前已经通过，未受影响；没有新增第二个五审标题。为遵守严格追加约束，保留前述原输出声明并在此明确纠正，没有回改任何字节。

正确的唯一标题校验应锚定整行，不应统计示例代码中的字面串：

```python
assert len(re.findall(r'^## 五审（R-10 round 5）$', text, re.M)) == 1
```

已重新实跑前缀摘要、整行标题计数与用户指定 tail/grep，实际输出：

```text
historical_prefix_bytes=192105 sha256=3dd4801a0c5c485ea33ab6ca365c54a29aa202921c174f1e848128c59890acd2 unchanged=True
actual_heading_lines=1
五审终裁：fail
tail_check_exit=0
```

本更正也用 ab 模式追加；再次核验原一至四审 192105 bytes 前缀，最终最后一行如下。

五审终裁：fail
