# ADR-G3：表达式引擎与研究统计引擎

日期：2026-09-11。状态：设计提案，供 G3 R-03–R-09 实现；不是实验通过证明。
作者：Codex（GPT-6 主控）；本次仅阅读仓库并新增本 ADR，未调用其他模型。
范围：JSON AST、因果特征、配对评估、切分、尝试账本、统计校正、合成验收与分档。
所有下述模块、测试与落盘行为均为后续实现设计，本次不执行。

## 0. 依据、可信度与优先级

依据代号只指本次读到的本地文件，不声称外部包当前版本或算法实测已核验。

| 代号 | 本地依据 |
|---|---|
| F | [feature-snapshot 全文](../../contracts/feature-snapshot.md)，§7 优先于 §1–§6 |
| R | [research-schema](../../contracts/research-schema.md)，§9 优先 |
| X | [execution-interface](../../contracts/execution-interface.md)，§5 优先 |
| M | [G2 M-01 修订](report-G2-contract-revision-M01.md)，§0、§3.1–§3.3；受 X 的最终裁定约束 |
| P | [合并计划](../../../docs/plans/2026-09-11-quant-scale-up-merged-plan.md)，D.1–D.5、E |
| C | [Claude 验收](../../../docs/reviews/2026-09-11-claude-review-of-merged-plan.md)，R03 |
| G | [GOAL-3](../../goals/GOAL-3-research-engine.md)，R-01–R-10 |
| L | [data_root()](../../src/quant_lab/data/paths.py)、data/api.py、data/lifecycle.py、data/schemas.py、research/__init__.py 的本次只读状态 |
| A | 仓库 AGENTS.md、用户提供的编码规则及 /Users/balen/.codex/GROK.md；本任务属 Codex 架构工作 |

可信度标记约定：**高**＝冻结约束或可直接核对的定义；**中**＝本文可实现但尚待测试的工程/数值选择；**低**＝依赖数据、算力或统计假设、必须实测的提案。
每节开头的标记覆盖该节设计选择；表中有独立标记者以该行优先。高可信的契约要求不等于其统计适用性已证实。
用户约束与 OR-01 定稿优先；C R03 的分档模拟次数按本次用户明确要求采用。
未批 G-LICENSE-SEARCH：不接入、不导入 DEAP，不执行 G 中旧的 deap import 验收；只保留枚举接口。
未批 G-STAT-CLAIM：全部真实结果只能描述；不得以数值显著、研究档位或合成通过替代声明授权。（高；用户、P E/G）

## 1. 边界与数据流

**决定［高；F §7、R §9、X §5］**：G3 只消费 G1 决策图及 G2 冻结执行结果，不推导消息闭包、不撮合订单。
`anchors.t_dec` 直接取 gold/episode 实列；缺失即拒绝该机会并记损耗，禁止用 decision_eligible_at 或重算延迟替代。
`instrument_id` 使用 `BTCUSDT-PERP.BINANCE-UM`；不在 G3 增加旧格式转换层。
数据流为：冻结配置 → 决策图/机会集/折 → 账本 reservation → AST lint → 后端 → snapshot → 两臂执行适配 → 配对差 → selection/max-t → 外折聚合 → 描述报告。
`rule` 虽在 F 的 Python 签名中为 Callable，也只能是代码内注册 RuleSpec 的实现；配置只引用 rule_id、版本与已校验参数。
JSON 中不得包含 Python、模块路径、动态 import、pickle 或表达式字符串；后端的“编译”只构造受控表达式对象。
`null` 用于契约可空列，不能按一般编码偏好改成 false；布尔 false 只表示合法假值。
**决定［中；F §7.5、L］**：所有湖路径调用 `quant_lab.data.paths.data_root()` 后拼接子路径。
例如账本为 `data_root() / "lockbox" / "ledger.parquet"`，缓存为 `data_root() / "research" / "cache"`。
不得使用 cwd 相对 `data/`；环境变量在一次 run 开始解析并冻结，运行中改变根目录即拒绝续跑。
最终集只经独立数据访问入口解锁；训练进程的 manifest 白名单不含最终标签，文件路径配置不是授权凭证。

## 2. canonical_hash：AST 规范化及身份

**决定［中；F §1、P D.2］**：采用版本化 `g3-json-c14n-v1`，不直接依赖任一 JSON 库默认浮点输出。
只收 JSON 对象树；文本传输层可解码 JSON，但拒绝重复对象键、NaN/Infinity 扩展、尾随内容与非 JSON 对象。
节点为 field、const、op 三选一；不允许混用或附加 metadata。metadata 放账本，不能混入运行 AST。
根深度定义为 1，叶节点也计数；重复子树按每次出现计节点，不能靠 DAG 引用绕过 15 节点上限。
规范化流水线在校验后运行，禁止先“修好”非法输入再让它过门：

1. 检查节点形状、类型、资源、白名单、单位与因果依赖，拒绝 bool 被当作数值常量。
2. 将注册默认参数显式填全；仅允许 OpSpec 的默认值，不能补未知参数。
3. 数值按十进制精确值解码；1、1.0、1e0 规范为 JSON 数字 1；-0 规范为 0。
4. 有限小数去无效前导/尾随零，展开指数，用最短无指数十进制表示；不以 binary64 round-trip 合并不同十进制常量。
5. 对象键按 Unicode 码点递增；本版所有语法键、字段/算子/枚举标识符限制 ASCII。
6. 数组保持原顺序；输出 UTF-8、无 BOM、无空白、无末尾换行；字符串用标准 JSON 转义。
7. `canonical_hash = sha256(canonical_json_bytes).hexdigest()`，64 位小写十六进制；规范化版本单独持久化。

**决定［中；数值可复现性、P D.2］**：不排序 Add/Mul/And 参数，不做结合律、代数约分、常量折叠或自动 Div→SafeDiv。
浮点舍入、NaN、短路规则会使代数等价不同于执行等价；本版只消除表示差异。
`canonical_hash` 标识规范语法，不独自代表语义版本；语义身份还包含 canonicalization_version、registry_digest 与 op_versions。
固定向量应证明：对象键顺序/1.0 与 1e0 同 hash；参数顺序、窗口、Ref lag、字段变化不同 hash。
规范化须幂等；解析规范串再规范化的字节必须完全相同；版本升级不能覆盖旧缓存。
常量精确规范化后转换 Float64 若溢出、非零下溢为零或超出字段允许域，则 lint 拒绝，不默默改变语义。

### 2.1 lint 硬门清单

**决定［高：冻结限制；中：资源默认值；F §1–§2、P D.2］**：任何失败返回结构化 ASTRejected(reason, json_path)，留原输入摘要。

| 硬门 | 检查与失败出口 |
|---|---|
| 形状 | 未知键、未知 op/field、错误 arity、混合节点、空 args、非数值 const、重复 JSON 键拒收 |
| 树预算 | 深度 >4、节点 >15；传输体 >64KiB、数值 token >128 字符或指数绝对值 >308 拒收（后三项为版本化提案） |
| 类型与单位 | Add/Sub/比较要求同单位；And 只收 bool；LogPositive 只收无量纲正值；单位由字段表和算子推导 |
| 因果性 | lookahead 必须 0；Ref lag 必须非负整数；center、未来终值、跨资产 rank/join、隐式未来填充拒收 |
| 窗口 | 只能选训练前冻结的集合；正整数；wallclock.minutes 与 rows.count 互斥；未知 unit/min_samples 拒收 |
| 字段血缘 | 禁标签、收益、成交后状态、描述图列；field registry 固定 source、unit、availability 与时间周期 |
| 算子状态 | 未通过 reference、被隔离、未支持后端或缺版本的算子拒收 |
| 计算预算 | 静态估计行数×窗口×AST 成本超过 run 的 CPU/内存/候选预算即拒绝；运行超限记 timeout |
| 协议预算 | p、唯一配置、调用总数和可见 cutoff 超限均不得启动计算；不是仅在报告时提醒 |

非法输入可能无 canonical_hash，账本该列可空，另存 raw_input_hash；不能为了有 hash 执行不可信解析扩展。

## 3. 算子注册表与 reference 契约框架

**决定［中；F §2、P D.2］**：`OpSpec` 为不可变记录，注册表由显式代码构建，不扫描插件。
字段：name、arity、arg_types、input_units、output_unit、parameter_schema、defaults、window_semantics、lookback、lookahead=0、min_samples、nan_policy、ddof、tie_rule、init_rule、version、backends、cost_model、reference_id、contract_status。
lookback 为可组合描述：none、rows(n)、duration(dt)、recursive(origin)；组合按真实依赖计算，不将墙钟窗口换算成“最近 n 个非空行”。
首批以 F 的显式清单为准，共 **19** 个名称；G 的“18”须纠正，见 C02。
以下语义是待契约确认的 v1 细化；所有参数进入规范 AST 或 registry_digest，不借后端默认值决定。

| 算子族 | v1 reference 定义与边界 |
|---|---|
| Add/Sub/Mul/Abs | 逐点数学运算；任一必需输入 invalid 则 invalid；非有限输出 invalid |
| SafeDiv | 分母精确为 0 则 invalid，不加隐形 epsilon；近零有限结果保留，溢出 invalid |
| LogPositive | x>0 才输出自然对数，其余 invalid；有单位输入需显式合法归一化 |
| Gt/Lt/And | 严格比较；相等 false；And 不短路掩盖 invalid，两输入都须有效 |
| Ref/Delta | params.lag=k，非负整数行滞后；Ref(x,0)=当前闭合值；Delta=x−Ref；行周期写入上下文 |
| Mean/Min/Max | 后向窗口；墙钟成员 close_time∈(t−W,t]；行窗取当前及前 n−1 个计划槽位 |
| Std | 样本标准差 ddof=1，n≤ddof invalid；不采用包默认 ddof |
| TSRank | 当前值在窗内的平均并列秩除 n；秩从 1 开始，单点有效时结果 1 |
| Corr | 成对有效的相同时间槽；样本协方差/样本标准差乘积；任一零方差 invalid |
| EMA | span=n，alpha=2/(n+1)，前 n 个连续有效值的均值初始化，之后递推；缺值/缺 bar 重置 warm-up |

**决定［中；F 的缺 bar 硬门］**：先用 manifest 的 interval 建计划槽位，区分“缺 bar”和“数值缺失”。
所有窗口默认为 strict_required：所需槽位缺失或任一必需值无效即 invalid，不删除缺行后压缩时间。
Ref(k) 不能跨缺失槽位找到更早第 k 个有效值；墙钟窗口不向左扩张填足 min_samples。
上市前不足完整窗口为 warmup；EMA 需要分区起点/上市起点或可验证 checkpoint 的完整递推前缀，不能在分块边界重新初始化。
窗口结束于闭合 bar 时间；不能按 episode 最近成交时刻改变窗口。
输出统一为 Float64 或 nullable bool 与独立 validity/reason；数值 NaN/null/Inf 在边界统一 invalid，合法 false 保持有效。

### 3.1 reference 的生成方法

**决定［中；F §2、P D.2］**：oracle 写在 `quant-lab/tests/research/reference/`，只用 Python 标准库循环、Decimal/Fraction 与 math。
不得调用被测后端、共享 rolling helper 或从其输出录制期望；测试目录位于项目根，不进 src。
代数/均值/方差分子使用 Fraction 精确计算；sqrt/log 用高精度 Decimal 或独立标量公式，最终转为比较域。
每算子至少 20 个具名边界夹具，提交输入、成员索引、逐步手算式、最终值和 mask；EMA 列出初始化及前数步递推。
固定种子为每算子生成至少 100 个随机序列，长度覆盖 0、1、窗口前后及多个周期，值含正负、重复、近零与常量。
另生成 ≥1,000 个合法组合 AST，逐节点用 oracle 解释，验证类型、lookback、组合 mask 与两后端结果。
对数值使用 `abs(actual−expected) <= 1e-10 + 1e-8*abs(expected)`；数值与 mask 分开比较，bool 完全一致。
价格/量纲夹具同时核原始值与预注册尺度下值，禁止临时放宽容差掩盖高量级误差。

### 3.2 四类必测协议

**决定［高：测试类别；中：具体构造；F §2、P D.2］**：每算子、每后端均运行相同测试工厂。

1. 截断重算：对每个可见时点 t，只给 available_at≤t 且已闭合的数据重算；全前缀对应输出和 validity 必须相同。
2. NaN 注入：分别在过去、当前、未来注入 NaN/null/Inf/极值；过去按 nan_policy，未来不得改变过去值或 mask。
3. 墙钟缺 bar：删除窗口内/左边界/窗口外槽位，插入晚到 bar，交换未来行序；验证不跨洞、不取未闭合 bar。
4. 手算 reference：常量窗、秩并列、零方差、0/0、log≤0、Ref(0)、EMA 重置与初始化均有独立期望。

乱序的有效输入先按唯一时间键排序；重复键直接拒绝，不能用“取第一条”消歧。
任何前视或数值门失败：隔离算子版本、禁用全部依赖 AST，报告最小反例；修复版本全套重验后才解禁。

## 4. polars / polars_ta 后端抽象及对拍

**决定［中；P D.1–D.3、F §2］**：AST 解释器和 OpSpec 为唯一语义属主，后端只实现已注册运算。
内部接口：`Backend.evaluate(typed_ast, bars, context) -> FeatureColumn(values, validity, reasons, dependency_span)`。
context 冻结 interval、availability_basis、registry_digest、backend_version、cutoff、资源限额、递推 checkpoint；不传任意执行代码。
polars 为首个参考实现目标；polars_ta 为显式可选变体，不把它当候选生成器，不预认任何覆盖率。
polars_ta 的行窗、ddof、EMA seed、秩规则必须适配；无法实现同合同则该 op 标 unsupported，不以近似替代。
backend 参数只能从 polars/polars_ta 二选一；运行中不静默 fallback 或混搭，切换开启新 run/执行身份并记账。
先用共同 10 算子 Add/Sub/Mul/SafeDiv/Abs/Ref/Mean/Std/Min/Max 做 spike，再扩到显式 19 算子矩阵。
两侧同 AST、bars manifest、硬件、线程、容差与 seed，比较值、mask、as-of 选择和空/热/分块结果。
报告契约通过率、未支持项、适配代码量、依赖锁及许可记录、冷/热耗时、峰值 RSS、退出维护成本。
128 AST×15m 夹具冷跑≤20分钟、RSS≤8GiB、spike≤8工程日只作资源提案，尚未实测。（低；P D.3）
不得用回测收益挑后端；若结果不等价先停统计评估，定位差异属于合同还是实现。

## 5. feature_snapshot：as-of、缓存和 tombstone

**决定［高；F §3/§7、M R2/R3、X §5.3］**：保留 F 的公共签名和输出列 `episode_id,t_dec,f_<canonical_hash>,validity_<hash>`。
anchors 读取 G1 `t_dec`，同 run 固定 graph_version；episode_id 唯一，UTC 微秒，空时钟拒绝并记损耗。
bars 按 instrument_id、interval 隔离；一个 snapshot 上下文只选冻结周期，不能把 1m 和 15m 混在同一 rolling 序列。
默认 H0 latency=0：取 `close_time<=t_dec` 最后一根；close_time 是区间右端点，禁止使用 close_time_raw 的 −1ms。
latency>0 时取 `close_time+latency<=t_dec`；若有真实可知时刻还必须满足 available_at≤t_dec。
例：15m 的 10:00–10:15 bar 在 10:15:00 可见；10:14:59.999999 不可见；latency=1s 时到 10:15:01 才可见。
消息/图边 as-of 不能套用 bar 等号：无顺序证据 strict_lt；le_with_sequence 的同刻要求 right_seq<left_seq。（高；R §1、M §2.1）
cutoff 是数据可见上限；t_dec>cutoff 的 anchor 拒评，不把 t_dec 偷换成 cutoff。历史 ingested_at 不冒充 available_at。
缺最近计划 bar/过旧时不无限向前寻找有效值；按计划槽位及冻结 staleness 容限给 invalid/reason。
真实 θ 描述附 latency=1s 重跑及 Δθ；若绝对 Δθ 超过主 SE，未来即使声明门已批也须降为描述。

**决定［中；F 缓存合同、P N04/D.2、L］**：采用两层内容寻址缓存，manifest 为显式内部 RunContext，不藏进可变全局变量。
纯行情层 key 包含 canonical_hash、canonicalization_version、op_versions、backend 名/版本、market_manifest、instrument/interval、availability_basis/latency、cutoff、preprocess_state_hash、递推起点/checkpoint_hash。
snapshot 层额外包含 graph_version、decision_snapshot_hash 集合摘要、anchor_hash（含 t_dec）、fold_id、OpportunitySet 摘要、schema/protocol_version、撤销代数 revocation_epoch。
preprocess_state 明确 fit 只见到的训练 IDs/cutoff/参数；不得只存一个自由名称“standardized”。
同上下文输出排序稳定；冷热缓存、整批/分块必须同值同 mask；cache metadata 不匹配即 miss，损坏摘要拒读。
G1 merge/split 后旧 episode_id 与 graph_version 不得重用；订阅或轮询撤销清单不是充分条件，每次 cache read/publish 都校验当前 tombstone/manifest 有效性。
失效 DAG：源版本/consent → 决策快照 → anchors/机会集/cluster → folds/K/权重 → 特征快照/收益配对 → selection/模型/报告。
行情纯特征若不依赖被撤图可保留，但任何旧图 snapshot 与报告必须拒读；旧账本保留并追加 invalidated 事件，不能改写历史尝试。
并发发布采用读取 epoch→计算→发布前复查 epoch；变化则丢弃待发布项。恢复备份必须先重放撤销，不能复活旧键。
目前 L 的 data/api.py、lifecycle.py 为 skeleton，schemas.py 仍有 `tombstone`，契约规定 `is_tombstone`；不宣称失效通道已存在，接入列名未达标应拒收（U01）。

## 6. OpportunitySet、簇均权 θ 与 G2 配对

**决定［高：主口径；中：适配结构；F §4/§7.3–§7.4、P D.4］**：收益可见前冻结 OpportunitySet。
内容包括排序 episode_ids、graph_version、eligibility/reason、cluster_id、固定风险预算、初始权重、证据层、日历与 label_maturity 规则、manifest 摘要。
冻结 dataclass 不能防内部 DataFrame 被改；保存不可变快照摘要，evaluate 入口重算校验。
must-link 同源转发、同计划多版本/多腿；同品种风险或标签区间重叠建经济边，取连通分量；不为提高 K 拆巨型簇。
簇 c 的合格机会数 n_c 在收益前冻结，`w_i=1/n_c`；每簇初始总权重为 1。
无损耗时 `θ=(1/K)Σ_c mean_i∈c(R_cand,i−R_base,i)`。
存在共同删失排除时 `θ=Σ_i m_i w_i d_i / Σ_i m_i w_i`，m_i 为全 family 共用的可评 mask；不按候选或删失结果重分配簇内权重。
报告初始簇均权与排除后各簇剩余权重；不会声称部分排除后仍每簇恰好权重 1。该细化列 C05 待确认。

### 6.1 请求与列对齐

**决定［高；R §9.4、X §5、M §3.1–§3.3］**：调用 G2 build_request，G3 提供 policy_version/policy_hash、risk_budget、场景、seed。
G1 order_plan 原样通过 G2 校验：entries 的 fraction/tif/post_only，stop.price/trigger=mark，tps，sizing，expiry，reduce_only_exit。
不得回退到 X 旧表的 size 字段；risk_budget 入场前固定，移 SL 不重置。
为同一 OpportunitySet 发 baseline 与 candidate 两组 ExecutionRequest，共享 episode_id、graph_version、decision_snapshot_hash、t_dec、风险预算与场景。
baseline 全 take；candidate 规则在 G3 应用，policy_version/policy_hash 与规则身份可不同，纯过滤候选的执行经济政策须等价。
为共同证据诊断，本版两组均对完整机会集回放；G2 不生成 skip，G3 在证据判定后覆盖有效候选 skip 的 R 为 0。
`simulate_batch` 原表不加 arm 语义；G3 适配器持有 request_id→arm/policy_hash 映射并给自己的配对表加 arm 列。
每组验证 M R9 键 `(episode_id,graph_version,policy_version,cost_scenario,path_scenario,kernel)` 唯一。
再固定 graph_version、场景、kernel、manifest、contract_version、seed 和 decision_snapshot_hash，于两臂按 episode_id 做一对一全连接。
重复、缺臂、意外 episode 或上下文不等为协议错误；不能 inner join 静默丢行。缺失的 policy_hash 回填只能来自已验证请求映射。
M §3.3 批输出的身份列为 episode_id、graph_version、decision_snapshot_hash、t_dec、policy_version、cost_scenario、path_scenario、market_manifest、execution_contract_version、seed。
消费标量为 fill_status、filled_qty、fees、funding、slippage、net_pnl、net_R、censor_reason、coverage_mask、trace_hash、kernel、kernel_version。
辅助标量为 entry_avg_price、exit_avg_price、position_open_at、position_close_at、gross_pnl、mae_R、mfe_R；保留 canonical_events 列用于审计。
coverage_mask 包含 mark_ok/funding_ok/rules_ok/bars_ok/liquidation_unmodeled；最后一项是模型边界，不等同证据缺失。
Decimal 列是 `Decimal(38,12)`，先校验空值和执行不变量再转 Float64 给统计引擎，禁止 cast 后 fill_null(0)。

### 6.2 缺值优先级与分母

**决定［高；F §7.4、X §5.4］**：按下表从上到下执行，NaN fallback 不得覆盖删失。

| 条件 | G3 处理 |
|---|---|
| 任一臂 censor_reason 非空 | 必须 net_R/net_pnl 为 null；共同排除并计损耗，不写 0 |
| 无删失却 net_R=null/非有限，或 coverage 与删失不一致 | 契约错误，拒评并记录；不自动补 0 |
| 无删失且 fill_status=none | 验证 G2 net_R=0；仍在分母 |
| 有效候选规则 skip | G3 R_candidate=0，baseline R 保留，仍在分母 |
| 候选特征 invalid/NaN | 同 skip；记录 nan，不让该候选单独缩分母 |
| 正常 take | 使用对应 G2 net_R，差值配对 |

family 的共同 mask 由冻结全候选池的删失并集生成，且在看 θ 前确定；任一候选导致池级损耗都报告来源。
政策变体若造成差异删失，另报各臂损耗和原机会总体不可识别限制；不能选择删失多的政策后宣称同总体改善。（中；P D.4，C05）
`nan_rate` 主门定义为候选所需特征任一 invalid 的初始合格机会数/初始合格机会数，避免先排除亏损行降低 NaN 率。
另报共同可评集合中的 NaN 率与簇权重 NaN 率；任何初始机会不能因 NaN 从 nan_rate 分母移除。
主 nan_rate>0.05 拒收，恰为 0.05 不拒；空分母 insufficient。（中：分母细化；高：阈值规则；F §4）
EvalResult 附原机会数/共同可评数/簇数、初始及损耗权重、coverage、nan_rate、per_fill_R、tail_loss、unclosed_rate。
per_fill_R 只在实际 take 且 partial/filled 的有效结果求均值；同时报告成交数，不替代主 θ。
未闭合率分母为已开仓结果，分子 position_open_at 非空且 position_close_at 空；未成交不算未闭合。
尾损同时报告 net_R 下尾分位/ES 与 mae_R 分布，符号和 α 在配置冻结；无风险上限授权不判经济 pass。（中；M R8、P G.1）

## 7. walk-forward、内层拆分及 PurgedKFold(t1)

**决定［中；F §5、P D.5/E.1］**：先冻结 UTC 日历边界、expanding/rolling、最小训练跨度、测试长度、最终窗口、embargo 与 maturity。
每折只训练当时 available 且标签已成熟的机会；标签成熟时刻是执行标签信息终点 t1 与标签可知时刻的较晚者。
t1 来自执行/随访证据，禁止用 t_dec 代替；未知 t1 不允许进入监督训练，右删失进入损耗。
每个机会有闭信息区间 I_i=[t_dec_i,t1_i]；簇区间取成员区间包络并保存真实成员区间，purge 以交集保守拒收。
训练窗内按时间拆 search→selection，分别 purge/embargo；search 子折拟合预处理、特征筛选、参数和阈值。
selection 前冻结候选池及规则；selection 只读一次、max-t 校正后按预注册准则选候选，平局以 canonical_hash 排序。
无合格候选或 selection insufficient 时采用预注册 baseline fallback，不临时换目标再选。
选定流程可在整个合法外训练窗重拟合已冻结的参数结构，但不能利用外测试标签改超参数/候选池。
外测试每个 episode 只产生一次预测；同簇跨外测试窗则整簇分配给最早触及窗，其余成员记边界排除，禁止重复累计或拆簇。
后折可在历史标签成熟后按冻结在线训练规则使用较早数据，但不能把外折得分反馈给人工新增语法。
每个实际 search 训练子折、最终 refit 的合法训练集都输出簇数/类别数/损耗；空折不删除，整体降档或 insufficient。

### 7.1 稳定性参考实现

**决定［中；P D.5 要求闭区间枚举参考］**：PurgedKFold(t1) 为独立 O(n²) oracle，生产优化版必须对拍，不把其分数作上线表现。

```text
输入：固定 test 索引；train_candidates 为其余索引；t0=t_dec；t1；cluster_id；embargo=e。
对每个训练候选 i：
  若 t0/t1 缺失或 t1<t0：拒绝 invalid_interval。
  若任一测试 j 与 i 同 cluster 或 duplicate_group：purge 同组。
  若存在 j 满足 t0_i<=t1_j 且 t0_j<=t1_i：purge 闭区间交集。
  若 t0_i 落入任一测试块之后的 (max_test_t1,max_test_t1+e]：purge embargo。
  若在拟合时点标签尚不可知：purge immature。
  否则保留 i，并保留逐条拒因证据。
```

walk-forward 只有过去训练时，测试后的 embargo 常为空操作；search/selection 前仍要求标签成熟、区间不交叠与冻结的时间隔离。
若额外设 selection 前 gap，明确记为 pre_gap，不把它与传统测试后 embargo 混称。
性质测试：端点相等必 purge；相差 1us 不重叠；增大 embargo 不增加 train；扩大测试区间不增加 train；行重排不改变集合。
另测同簇不交叠也隔离、未知 t1、巨型簇、全 purge、单类、跨折重复、修改未来标签不改变早折训练集合。
优化区间索引与枚举 oracle 在固定边界集和随机区间集完全一致，报告 purge 原因和 embargo 区间供人工核对。

## 8. 尝试账本 schema、写入时机与恢复

**决定［中；F §5、P D.5/E.1］**：账本为单写者追加事件日志加 Parquet 投影；不对 Parquet 做不安全并发 append。
路径均由 data_root 解析：lockbox/ledger-events/ 为提交事件，lockbox/ledger.parquet 为可恢复的当前投影。
每次提交先写同目录临时文件并 fsync、原子改名、同步目录；崩溃重放已提交事件，投影通过临时文件原子替换。
初版协调器单写者，锁定 run 和预算计数；并行计算 worker 不直接更新账本。投影坏了只重建投影，不删事件。

| 字段组 | schema 与用途 |
|---|---|
| 身份 | attempt_id UUID、event_seq int64、run_id、protocol_hash、parent_id?、retry_of?、origin enum human/llm/enumeration/gp |
| AST | raw_input_hash、canonical_hash?、canonicalization_version、registry_digest、params canonical JSON、rule_hash、candidate_pool_hash |
| 阶段 | fold_id、stage enum search/selection/outer/final/null_validation、visible_cutoff UTC、objective、evidence_layer |
| 血缘 | code_version、model_version?、data_manifest、market_manifest、graph_version、op_versions、backend_version、preprocess_state_hash |
| 执行 | policy_version/hash、cost_scenario、path_scenario、execution_contract_version、seed、OpportunitySet_hash、pair_table_hash? |
| 预算 | cost struct(cpu_seconds,wall_seconds,peak_bytes,api_cost)、reserved_budget、config_id、compute_call_count、statistical_exposure_count |
| 状态 | status、reason_code?、created_at、finished_at?、result_hash?、duplicate_of?、label_access_started_at?、invalidated_by? |

F 的全部必需字段保留；model_version 无模型时为空，cost 记录实测，不能把免费缓存命中误报为新的统计机会。
入口在 lint/收益访问前持久化 reserved，随后合法性校验；开始访问标签前再提交 running/label_access_started。
终态包括 completed、rejected、duplicate、failed、timeout、budget_exhausted、interrupted；失效另追加 invalidated 事件，保留原终态。
预算先原子预留后派发，写失败则不得计算；重启将未闭合 attempt 标 interrupted，重试使用新 attempt_id 并关联原记录。
同 config 在其他折重算计计算调用；看新的窗口/目标/成本/频道结果增加统计暴露；新规则/参数形成新 config，不能重新命名绕过全协议上限。
非法/重复不消失，但不重复耗唯一合法配置额度；解析/生成/调用成本仍受独立硬预算约束，超时不退还已消耗预算。
不落原始敏感正文或外来代码；无法规范化的输入只记受限摘要、错误路径与长度。
final 标签首次访问即写 opened；崩溃只允许同配置恢复，不能产生新的最终窗口或新的挑选机会。

## 9. max-t：共同日历块、权重、SE 与下界

**决定［中：算法实现；低：数据适用性；F §5、P D.5］**：统计输入必须含逐机会配对差，单独 thetas/ses 无法执行合法块重采样（C03）。
内部 `PairedPanel` 包含 episode/cluster、t_dec、fold、固定 n_c、w_i、共同 mask、各候选差 d_ij、family_hash；候选池已冻结。
公共旧 `max_t_bootstrap(thetas,ses,...)` 在 C03 未裁定前只能保留拒绝缺 panel 的桩，不以隐式全局缓存补数据。
共同日历按 UTC 固定起点划分长度 L 天的不重叠块，包含空块；L 默认候选 1/3/7 日，主 L 仅由训练诊断预注册。
为保持簇完整，统计重采样把整个簇归到最早 t_dec 所在的日历块，保留成员原时间与整个候选向量。
跨块长簇比例、最大跨度及移位质量必须报告；大量跨块依赖不能靠归属规则“消除”，改用事前更长 L 或返回 insufficient（U03）。
所有频道/资产/候选用同一日历及同一索引矩阵 `I[b,q]`，不得候选单独抽块；共同非空块少于 20 即 insufficient。
每次 b 有放回抽取与原日历块数相同的块数；空块被抽到仍占位置，不为了凑样本重抽。
每个被抽中的块实例创建新的 block_copy_id/cluster_copy_id，同一源块重复抽中产生独立副本身份。
这样每份簇副本总初始权重仍为 1，不会因用旧 cluster_id 分组而把重复抽样抵消。

### 9.1 重估器和独立 SE reference

**决定［中；P D.5 每次重估要求］**：每次重采样从机会行重建副本簇的初始 n_c*，再赋 w_i*=1/n_c*，最后应用共同 mask。
即使整簇抽样下数值恰与原权重相同，仍按副本重建，不复用原 θ、原分母或原 SE。
重估 `A_j*=Σ m_i*w_i* d_ij`、`D*=Σ m_i*w_i*`、`θ_j*=A_j*/D*`；空 D* 不重抽，整个调用 insufficient。
初版 SE 用共同块 delete-one jackknife，避免嵌套 B 次 bootstrap，且让独立 reference 可手算。
对当前样本的 Q 个日历块实例，逐一删除 q、重新算簇权重/共同分母，得到 θ_j,(-q)。
令 mean_leave_j 为 Q 个 leave-one θ 的均值，`se_j²=(Q−1)/Q * Σ_q(θ_j,(-q)−mean_leave_j)²`。
原样本和每个 bootstrap 样本都调用同一重估器与 jackknife；重复抽中的源块按实例分别删除。
空块保留在 Q 中；任何 leave-one 零分母、非有限或接近零 SE 触发 insufficient，不删失败副本后继续计算。
SE 下限与杠杆诊断阈值写入 protocol；建议 `se <= 1e-12*max(1,abs(theta))` 拒评，适用性需合成校准。（低；数值稳定性提案）
无差异的 baseline 不放入待检候选 family；候选零方差不是“无限显著”，返回 insufficient。

### 9.2 max-t 统计量

**决定［高：公式来自 P D.5；中：分位实现］**：B=2000，单侧 α=0.05。
观测 `t_j=θ_j/se_j`；每个复制 `T_b=max_j((θ_bj*−θ_j)/se_bj*)`，不取绝对值，不改变为双侧检验。
`p_adj,j=(1+Σ_b 1[T_b>=t_j])/(B+1)`，加一修正与 ≥ 的等号都必须测试。
令 k=ceil((1−α)(B+1))，q 为升序 T 的第 k 项；k>B 时无有限分位并返回 insufficient。
同时单侧下界 `LB_j=θ_j−q*se_j`；报告为依赖假设下 bootstrap 近似置信下界，不称有限样本保证。
1/3/7 日敏感性全部报告，不选最小 p；主块长、family 成员与缺失规则在 selection 前冻结。
独立验收包含手工两候选/少数复制矩阵验证 max、p、分位与 LB，另用满足 ≥20 块的输入测完整 API。
候选完全复制不增加 max 分布；交换候选列不改变结果；共享冲击时与错误的独立抽块实现应出现可诊断差异。
其他 insufficient 条件：不稳定 SE、制度突变无法由预注册机制表达、依赖跨度不覆盖、候选公共支持消失或协议血缘不一致。

### 9.3 外折与最终 family

**决定［高；P D.5］**：每机会只收一次折外预测，将所有外折的配对差拼成全时间 panel，按共同日历块估一个 θ/CI。
不平均折 t 值、p 值或显著票数；这个 CI 条件于已产生的折外预测，不声称包含全部选模不确定性。
估计完整选择流程的不确定性时必须在复制中重跑 search/selection/refit，合成空模型验收必须覆盖此路径。
未来确认性 family 仅冻结后的 V 前瞻 cohort、五频道经济簇均权、一个基线、一个主 θ、一个最终窗口。
H0/频道分层/其他目标仅探索；若各自允许宣告成功，开箱前加入声明 family 并以 Holm 校正，不能事后挑显著项。
selection 的 max-t 与最终 family 分层记录；不把每个内部尝试机械重复惩罚成 DSR，也不漏掉最终多声明控制。
G-STAT-CLAIM 未批时输出 `claim_status=descriptive_only`，包括 LB>0；最终标签不得为提前形成声明而开箱。

## 10. 空模型：整块残差、FPR/功效与资源

**决定［低：统计生成机制；高：禁止逐 episode 洗牌；P D.5、C R03］**：每种机制仅用训练窗拟合，并冻结生成参数、seed 清单、漂移/相关诊断。
采用训练日历格点上的联合残差向量，维度覆盖全部资产/频道及收益共同因子；拟合条件均值后对残差中心化。
以共同 L 日块抽取整段向量，块内时间顺序、横截面相关、波动聚集、事件强度与风险暴露结构一起保留。
在合成的固定 episode 时间/簇布局上，通过预注册暴露映射将格点冲击积累为机会收益；不把长短不同 episode 列表强行一一洗牌。
因果特征由独立随机流生成，或采用独立整块位移的特征过程；固定种子映射，禁止取决于最终收益的位移选择。
过滤主空模型将净基线收益的条件均值设为 0，使任何独立 take/skip 的期望增益为 0；不能只把 candidate−baseline 事后减均值冒充完整空流程。
政策变体必须有相应无增益结构生成器；无法构造其合法空机制就限制验收范围，不能拿纯过滤空模型背书所有政策。
合成 G2 输出要满足 Decimal/null/coverage 不变量，可经 synthetic adapter 生成；这是 G3 统计验证，不替代 G2 撮合验收。
预注册机制至少含共同市场冲击、簇内复制/重尾波动、非均匀事件密度/缺失；额外整块循环位移为压力夹具。
逐 episode 独立洗牌明确禁止；整块 resample 也必须诊断块间残余相关、跨资产相关、尾部、簇大小与缺失结构是否保持。
诊断失败标 invalid_null_model，而非“FPR 很低所以通过”。合成未来标签与生成器状态不能流入训练拟合。

### 10.1 完整模拟与功效注入

**决定［中；P D.5、G R-08］**：每个 MC replicate 独立运行生成→候选搜索→selection→外折→最终模拟声明，使用当档真实预算与停止条件。
不能先用真实数据挑最优候选，再只模拟该候选；不能把廉价单检验当全流程 FPR。
FPR 事件为“任一预注册允许主声明为阳性”；运行失败/insufficient 单列，严禁静默从 MC 分母删掉。
合成声明函数允许检验公式，但输出明确 `synthetic_validation`，不绕过真实 G-STAT-CLAIM。
功效机制保持同一相关结构，在可由合法小语法表达的因果特征上植入预定有效规则，使该规则总体每种子配对净增益 δ=0.2R。
例如 oracle skip 集合比例 π>0：令其基线均值为 −δ/π，skip 后 0；其余 take，不直接给所有候选结果加 δ。
注入值使用总体 π 与预注册模型参数，不用当前样本正负结果反推；报告实际实现效应、规则找回率及噪声/样本规模。

### 10.2 分级验收

**决定［中；用户明确采纳 C R03］**：为消除 T3 对应 E 多档的歧义，本 ADR 将高搜索档 T3a/T3b 合称 T3；见 C06。

| 验收级 | E 的 K 档 / 配置顶 | 每种空机制次数 | 每种功效机制次数 | 区间与标准 |
|---|---|---:|---:|---|
| T0 | <80 / 固定政策 | 工程夹具 | 不作收益选择功效声明 | 不伪造统计通过 |
| T1 | 80–199 / 12 | 1000 | 1000 | 双侧95%精确二项 CI，FPR 上界≤7%，功效下界≥80% |
| T2 | 200–499 / 64 | 1000 | 1000 | 同 T1 |
| T3a/T3b | 500–1499 / 512；≥1500 / 2048 | 200 | 200 | 同样阈值，预注册更宽精确区间，不能降低验收标准 |

精确区间使用 Clopper–Pearson：x>0 时下界 BetaInv(.025,x,n−x+1)，否则 0；x<n 时上界 BetaInv(.975,x+1,n−x)，否则 1。
例如 0/200 双侧上界约1.83%；该数只是区间算术，不能推断通常 5% FPR 的实现很容易通过。
T3 可在看真实收益前选择参数化 bootstrap 替代方案：冻结联合生成模型/参数不确定性、完整搜索复现次数、区间算法与小规模精确对照。
默认选择 200 次精确二项方案；参数化近似需另行统计审查，不能在精确方案不通过后改用近似“救结果”。
所有预注册机制均需过门；报告每机制和最坏机制，不将简单机制与失败机制混池稀释 FPR。
MC 若有失败，给最坏界：FPR 上界将失败视为阳性，功效下界将失败视为未检出；并报失败率；不允许未完成却用计划 n 充数。
资源不足为 not_run，区间不足为 insufficient，点估计超标为 fail；阈值通过仅对已验收规模/噪声/协议有效。

### 10.3 资源预计与停止条件

**估计［低；P E.1、C R03；未测速］**：按 3 折、每配置每折1 CPU秒作容量算例，不是工期承诺。
一套空机制加一套功效机制的最低评估成本近似 `2*M*C*F*t`；搜索重拟合、最终评估、B=2000 与 I/O 另加。

| 档 | M/C/F | 上述最低 CPU 量（t=1s） |
|---|---|---:|
| T1 | 1000/12/3 | 20 小时 |
| T2 | 1000/64/3 | 106.7 小时 |
| T3a | 200/512/3 | 170.7 小时 |
| T3b | 200/2048/3 | 682.7 小时 |

多机制线性增加，t=10s 则乘10；不能把并行线程数当作保证等比例加速。
朴素 jackknife-bootstrap 约 O(B*C*Q²) 聚合工作；块充分统计量优化可降到 O(B*C*Q)，但必须与逐行重估 reference 对拍。
不持有 B×N×C 张量；流式聚合，索引矩阵/seed 和输出摘要落盘；允许跨 MC 复用因果纯特征，不得复用已看标签的选择结果。
先用小批合成测速记录 P50/P90、RSS、不同 C/F/Q 与 15m/1m 数据规模，再冻结验收资源上限。
真实搜索 CPU≤24小时/轮与合成验收是两份预算；无授权预算/算力或预测超限则 not_run，不启动更贵档位。

## 11. 分档：K、DEFF、p 与搜索硬顶

**决定［高：执行 P E 的门；低：阈值经验充分性；P E.1］**：`K=min_fold N_cluster_fold`，fold 指全部实际内层训练折 purge 后可用簇。
不能以全样本簇数、外测试簇数或均值替代；V/H0 分开计数；空折/单类折不删除以晋档。
为每个主目标及基线估 `DEFF=max(1,Var_common_calendar_blocks(theta)/Var_independent_clusters(theta))`。
共同块方差与第9节同口径；独立簇方差用同机会权重、同 mask 的簇重采样，不能对两者使用不同分母。
`K_eff=min_fold floor(K_fold/DEFF_fold)` 只作降档诊断；不是独立样本数，也不能用基线 DEFF 保证候选尾损。
分母方差为0/不稳定则 DEFF undefined、insufficient，不能以1替代；训练标签内估计并报误差/块长敏感性。
候选新产生的高依赖可在 selection 前使门收紧；不能看外折收益再调 DEFF 晋档。

| 档 | K | 额外条件 | p 硬顶 | 全协议唯一配置上限 |
|---|---:|---|---:|---:|
| T0 | <80 | 分母与证据可数 | 0 | 固定政策，不收益选优 |
| T1 | 80–199 | ≥6个月，每评估块≥20簇 | 4 | 12，预列规则 |
| T2 | 200–499 | ≥12个月，≥3外折，每折≥30簇 | 8 | 64，有界枚举 |
| T3a | 500–1499 | ≥18个月，≥3外折，每折≥50簇 | 16 | 512，本轮仍枚举 |
| T3b | ≥1500 | ≥24个月，≥3外折，每折≥100簇，最终另≥200簇 | 32 | 2048，本轮仍枚举 |

按 K 初档，再由 K_eff、跨度、有效秩、类数、功效和损耗只降不升；额外条件不满足保留原始失败原因。
`p <= min(降档后硬顶, floor(K/20))`；若 K_eff 更低，另保守收紧到 floor(K_eff/20)，不得借原 K 留高维预算。（中；降档实施细化）
分类再限 floor(E_min/10)，E_min 为每内折正负类簇权重较小值的最小值；连续 R 不套 EPV。
p 计拟合系数、交互、哑变量、样条、可调阈值自由度；节点/窗口搜索另计配置数，不能把158选10只记10次尝试。
配置跨来源、频道、外折共享上限，计算调用≤配置上限×冻结折数×重复数，先耗尽配置/CPU/内存任一即停。
每折报告原始/去重/证据/成熟/行情/簇/purge 损耗、类数、最大簇占比、跨度、缺失率、有效秩及 K/DEFF/K_eff。
T3a 只可申请前瞻研究；T3b 仍需所有声明/工程/经济/授权门。未批声明门时所有档统一 descriptive_only。

## 12. 封顶小语法枚举生成器

**决定［中；F §6、P D.1/E.2、用户许可约束］**：实现 `enumerate_grammar(depth=2,*,ops,windows,fields,cap)`，只输出合法 JSON AST。
ops/windows/fields 先排序去重并校验；默认深度≤2，仍须受全局深度≤4、节点≤15、p 与资源门约束。
按深度、算子名、参数、子树字节序确定遍历顺序，使用流式笛卡尔积而非先物化全候选空间。
先类型/单位剪枝，再 lint 与 canonical_hash 去重；收集 cap 个唯一合法 AST 即停，不承诺深度2自然少于2000。
另设 visited-node/生成时间上限，避免大量重复或无效组合耗尽资源；达到上限输出完整终态与实际池大小。
T1 用预列配置，T2/T3 可枚举；实际 cap 不超过剩余全协议配置额度，配置含 AST、阈值、政策、目标与场景。
生成顺序不读取收益；池冻结后 selection 不补候选。每次提议/拒收/重复都有账本事件，生成不等于已访问标签。
冻结 pool_hash、截断顺序、语法版本与 seed；同配置输出一致，输入列表重排不改变集合或排序。
不调用 DEAP compile/from_string、Qlib eval，不接 GP/PySR，不调用人/LLM 付费提案；未来许可批准仍须新的等总预算实验。

## 13. 模块划分与测试矩阵

**决定［中；G §2、A、F］**：实现只依赖 data.api、market.contract/execution/asof 的公开接缝，research 不 import services。

| 模块 | 单一责任 | 必测证据 |
|---|---|---|
| research/ast.py | JSON schema、类型、规范化、canonical_hash | 重复键/资源炸弹/单位/因果拒收，规范化幂等与固定 hash 向量 |
| research/ops.py | OpSpec 与隔离注册表 | 19名清单、版本默认值、每算子20边界+100随机 |
| research/backends/polars.py | 受控后端求值 | oracle、截断、NaN、缺 bar、1000组合 |
| research/backends/polars_ta.py | 同合同适配 | 全支持矩阵、同值同 mask、无静默 fallback、spike |
| research/features.py | as-of 与两层 cache | 闭合等号、latency、cutoff、冷热/分块、tombstone 并发发布 |
| research/evaluator.py | OpportunitySet、请求映射、共同配对差 | 重复/缺臂、Decimal空值、skip/none/NaN/删失优先级、簇均权 |
| research/protocol.py | 日历折、内层拆分、purge/embargo | 枚举oracle、边界/单类/空折、未来标签扰动、OOF唯一性 |
| research/ledger.py | 单写者预留、事件与恢复 | lint前落账、写失败不计算、崩溃重试、并发预算、开箱一次 |
| research/maxt.py | panel重估、共同块、SE、p/LB | 手算比值/SE/复制索引、共同冲击、零SE/块不足、优化对拍 |
| research/nullmodel.py | 联合残差生成与完整流程MC | 块内相关保持、禁止逐episode洗牌、注入δ、区间与失败分母 |
| research/tiers.py | K/DEFF/p及共享预算 | 阈值两侧、巨型簇、缺外折、DEFF未定义、只降档 |
| research/grammar.py | 确定性有界枚举 | cap=0/1/耗尽、去重、排序、visited预算、不得读标签 |
| research/api.py | run_protocol编排与报告 | 合成端到端、声明门、许可门、相同输入可恢复 |
| research/synthetic.py | fake episodes/bars/execution | t_dec实列、新instrument_id、M列名、Decimal(38,12)、独立预期 |

测试与夹具统一置于 `quant-lab/tests/research/`，包括 fixtures/protocol_synthetic.yaml；不新增 src 内测试目录。
合成 smoke 将 QUANT_LAB_DATA_ROOT 指向临时目录，验证账本/缓存/报告不写到 cwd 的 data，且不访问生产网络或凭据。
端到端最小反例：一个簇两个机会与另一簇一个机会，验证均权；亏损机会注 NaN 保持分母；删失排除计损耗；图 split 使旧报告拒读。
后续验收产物：report-G3-backend-spike.md、report-G3-null-model.md、review-G3-P1.md；本 ADR 不代替这些实跑报告。
ProtocolReport 必含 schema/protocol/hash、tier、K/DEFF/p、n_attempts/调用数/暴露数、theta/se/p_adj/LB、损耗、资源、门状态与限制。
验收顺序：语法/算子→后端→snapshot/配对→折/账本→max-t reference→空模型→分档枚举端到端；每步失败阻断依赖步骤。

## 14. 契约修订建议

以下 **7 条**是需 G0 仲裁的提案，本次不修改契约、不调用 task 状态命令；公共接口的冲突部分在裁定前不伪实现。

| ID | 修订建议 | 未裁定时的实现出口 | 可信度与依据 |
|---|---|---|---|
| C01 | F §1 示例 Div 改为注册 SafeDiv，或另定义 Div 合同；本 ADR 建议前者 | Div 一律 ASTRejected，不自动别名 | 高；F §1示例与§2白名单不一致 |
| C02 | G R-04/DoD 的18算子改为显式19个；R-01 deap import 门在许可前标不适用 | 以 F 明列19名实现，禁接DEAP | 高；F §2、G、用户许可门 |
| C03 | F §5 max_t_bootstrap 增加显式 PairedPanel/共同mask/cluster/calendar 输入；thetas/ses 可作校验值但不充分 | 旧签名无 panel 返回 contract_blocked；先实现内部纯函数和合成测试 | 高；仅均值与SE不能恢复相关与重采样权重 |
| C04 | F feature_snapshot/walk_forward 的 manifest、fold、graph、preprocess、cluster、t1 等上下文补成显式版本化对象 | 公共签名不私改；内部 runner 显式传上下文，无上下文禁缓存或拒绝统计切分 | 中；F公共参数不足以表达完整缓存/切分合同 |
| C05 | 明定初始簇均权、family共同删失mask、排除后不重分权、nan_rate分母与政策差异删失限制 | 保留空值/损耗，公共比较不按候选删行；未确认总体口径只描述 | 中；F §7.4、P D.4未穷尽部分簇删失 |
| C06 | 固化 T0/T1/T2/T3a/T3b 与 E 表映射，T3=高两档；写入1000/1000/200及精确区间方案 | 按本次用户约束做合成设计，真实统计放行仍未批 | 中；C R03未独立列出五档名称 |
| C07 | G1提供有效manifest/撤销epoch与tombstone查询接缝，明确is_tombstone列及备份重放次序 | 无可靠撤销状态时旧图缓存拒读，G3不读取描述图自行修图 | 中；R §9、P N04与L骨架现状 |

## 15. 未决

以下 **7 条**需后续证据或裁定；均不通过本次写文档自动闭合。

| ID | 未决事项 | 关闭依据与当前出口 | 可信度与来源 |
|---|---|---|---|
| U01 | G1实列、decision snapshot、t1/标签可知证据及撤销接缝是否已落地 | G1/G2契约测试和merge/split演练；目前仅合成桩，不把L的skeleton算完成 | 高：当前代码状态；R/X/L |
| U02 | OpSpec参数名、窗口完整性、EMA初始化、墙钟/行支持集与容差是否获统一签字 | 19算子reference、两后端边界对拍及契约细化；未通过项隔离 | 中；F §2、本文§3 |
| U03 | 共同块长、跨块长簇阈值、jackknife SE在目标相关结构中的适用性 | 训练诊断与各档完整空模型；不足返回insufficient，不按p选块 | 低；P D.5、本文§9 |
| U04 | T3精确200次还是预注册参数化替代，以及多机制所需算力预算 | 真实收益前冻结方法/预算和pilot容量；默认200精确，资源不足not_run | 低；C R03、本文§10 |
| U05 | G-STAT-CLAIM、最终V窗口、尾损/回撤上限、成本压力是否批准 | 明确门状态与预注册文件hash；此前全部descriptive_only，经济门不判pass | 高：门约束；用户、P H/G |
| U06 | polars_ta版本锁/许可/合同覆盖与切换成本；G-LICENSE-SEARCH及后续GP范围 | spike证据及明确许可/范围裁定；当前只枚举，未支持算子拒收 | 低：实测覆盖；高：许可约束；P D/E、用户 |
| U07 | 真实逐折K、DEFF、共同损耗/NaN率和功效是否足够 | G1/G2输入验收后仅训练窗计量，逐折报告并降档；禁止预判研究优势 | 低；P E、F §4、本文§11 |

## 16. 本次文档验收边界

**记录［高；用户任务范围］**：只新增本文件；不改现有实现、契约、目标、账本或任何生产状态。
未安装包、未调用收费模型、未运行研究/收益测试；文中全部性能、FPR/功效与门状态要求均是待验收设计。
本次机械验证为 `wc -l`、`grep -c 'canonical_hash'`、`grep -c 'max-t'`；设计含 canonical_hash、max-t、空模型、分档独立章节。
