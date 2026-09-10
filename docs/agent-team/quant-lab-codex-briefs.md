# quant-lab Codex 任务书模板

派发命令（所有任务书通用，cwd 任意）：
```bash
bash ~/.claude/skills/codex-dispatch/scripts/dispatch.sh --dir /Users/balen/projects/trader-bot --model gpt-6-astra --effort medium -- "<把下面对应任务书整段贴进来>"
# 输出的 WATCH_CMD 放进 Monitor 工具（超时 ≥ 3600000 ms，停滞阈值 40 分钟）；RESULT_CMD 取结果
```
禁止 `--resume-last`；STALLED 即 cancel 后重派全新任务。Codex 只读仓库、只写任务书指定的一个输出文件。

## §1 引擎设计 ADR（三份，各窗口第二个任务）

### ADR-G1 episode 引擎
```
目标：为 quant-lab 数据系统写 episode 引擎设计 ADR，作为 G1 实现依据。用 gpt-6。
范围：只读仓库；只写 /Users/balen/projects/trader-bot/quant-lab/docs/adr/ADR-G1-episode-engine.md。不改任何现有文件，不运行改动状态的命令，不调用收费模型，不做生产相关操作。
必读：quant-lab/contracts/research-schema.md、quant-lab/contracts/README.md、docs/plans/2026-09-11-quant-scale-up-merged-plan.md（A、B、C 节）、quant-lab/goals/GOAL-1-data.md、AGENTS.md。
约束：遵守契约字段；契约不足之处在 ADR 末尾"契约修订建议"单列，不在正文假设已改。设计必须覆盖：(1) 六级产线每级的输入/输出 Parquet schema 与幂等重放策略；(2) 作者轨双状态（计划 P × 声称 C）的完整转移表，含 invalid_transition 处理；(3) 描述图与决策图的构建算法与依赖闭包（节点、边、字段、媒体、派生特征的 available_at 闭包）；(4) 规则链接器的候选边生成（reply/quote/plan_ref/window/复制指纹）与弱边/强边规则；(5) LLM 裁决接口（输入上下文裁剪、输出必须带证据消息 id、拒答出口）；(6) merge/split 的 tombstone、predecessor/successor、级联失效 DAG 与重算触发；(7) 三时钟在每级的计算规则与 H0/H1 假设记录；(8) 五反例（编辑SL不回填 / 未成交触TP不闭仓 / 超时只过期 / 同向双单不误并 / 提前管理留缺入口）的期望输出；(9) 合成夹具的生成规格（≥3 频道 × ≥60 条，含相册、编辑、回复、转发、纯图、跨频道复制）；(10) 模块与文件划分、测试矩阵。
验收：文件 ≥150 行；含转移表、tombstone、决策图闭包三节；每个非平凡设计选择标可信度与依据；"未决"单列。
验证：wc -l；grep -c 'invalid_transition'；grep -c 'tombstone'。
输出要求：回复文件路径、行数、契约修订建议条数、未决条数。
```

### ADR-G2 执行内核
```
目标：为 quant-lab 写永续执行内核设计 ADR（候选 A 自研参考实现 + 候选 B Nautilus 1.227.0 SimulationModule 扩展 + A/B 比较方法），作为 G2 实现依据。用 gpt-6。
范围：只读仓库；只写 /Users/balen/projects/trader-bot/quant-lab/docs/adr/ADR-G2-execution-kernel.md。其余同 ADR-G1。
必读：quant-lab/contracts/execution-interface.md、quant-lab/contracts/README.md、docs/research/2026-09-07-quant-toolchain-selection/2026-09-07-quant-toolchain-selection-report.md（Q3、Q4 与 research/r2-nautilus-source-verify.md）、docs/plans/2026-09-11-quant-scale-up-merged-plan.md（C.1、C.2 行情流、D.4）、quant-lab/goals/GOAL-2-market-kernel.md、AGENTS.md。
约束：Nautilus 源码事实以选型报告已核内容为准（无 process_mark_price；engine.rs:1018-1023 路由绕过 MarkPriceUpdate；普通止损 is_stop_matched 买 ask≥触发、卖 bid≤触发；LAST/MID bar 合成 bid=ask=trade；无默认 funding 结算；modules.pyx SimulationModule + exchange.adjust_account 是候选 B 钩子），有异议标"待核"。设计必须覆盖：(1) 候选 A 的对象模型：挂单队列、部分成交、撤改单、组合订单（入场梯 + SL + 多档 TP）、reduce-only、GTD/IOC、账户约束、精度与 filter；(2) 触发与撮合判定顺序：mark 触发 SL / last 撮合 TP、同 bar 双触发、三档路径情景（primary/adverse/favorable）的精确定义；(3) funding 账务时点与 8 小时/可变周期；(4) 不变量清单与断言位置；(5) ≥10 最小 episode 的规格与独立期望的推导方法；(6) 候选 B：SimulationModule 里 mark 触发与 funding 入账怎么挂、适配器审计范围（订单状态、filter、OrderList/GTD、价格精度）、不能覆盖的差异；(7) A/B 比较表模板（代码量、可验证性、性能、维护面、逐 episode 差异解释码）；(8) trace_hash 的定义与重放一致性；(9) 与 G3 EventEvaluator 的接口对接（ExecutionResult 列）；(10) 模块划分与测试矩阵。
验收：文件 ≥200 行；含 SimulationModule、is_stop_matched、不变量、路径情景四节；未决单列。
验证：wc -l；grep -c 'SimulationModule'；grep -c 'is_stop_matched'。
输出要求：回复文件路径、行数、契约修订建议条数、待核条数。
```

### ADR-G3 表达式与统计引擎
```
目标：为 quant-lab 写表达式引擎与研究统计引擎设计 ADR，作为 G3 实现依据。用 gpt-6。
范围：只读仓库；只写 /Users/balen/projects/trader-bot/quant-lab/docs/adr/ADR-G3-expression-and-stats-engine.md。其余同 ADR-G1。
必读：quant-lab/contracts/feature-snapshot.md、quant-lab/contracts/research-schema.md、quant-lab/contracts/execution-interface.md、docs/plans/2026-09-11-quant-scale-up-merged-plan.md（D.1–D.5、E 节）、docs/reviews/2026-09-11-claude-review-of-merged-plan.md（R03 FPR 分级）、quant-lab/goals/GOAL-3-research-engine.md、AGENTS.md。
约束：AST 只收 JSON；不执行外来字符串（不用 DEAP compile/from_string、不用 Qlib eval）；本 DSL Ref(x,0)=当前闭合值。设计必须覆盖：(1) AST 规范化与 canonical_hash 算法、lint 硬门清单；(2) 算子注册表结构与契约测试框架：截断重算、NaN 注入、墙钟缺 bar、手算 reference 的生成方法；(3) polars 与 polars_ta 两后端抽象、切换与对拍协议；(4) feature_snapshot 的 as-of 对齐（等号语义）、缓存 key 与失效（对接 G1 graph_version tombstone）；(5) OpportunitySet 冻结、簇均权 θ、NaN→skip 与 nan_rate 门、与 G2 ExecutionResult 的配对；(6) walk-forward 折生成、search/selection 内层拆分、PurgedKFold(t1) 参考实现、purge/embargo 性质测试；(7) 尝试账本 schema 与写入时机；(8) 共同日历块 max-t：块索引共享、每次重采样重估权重与 SE、p_adj、下界、insufficient 条件；(9) 空模型：整块残差重采样保留块内相关、禁逐 episode 洗牌；FPR/功效验收按档分级（T1/T2 1000 次；T3 200 次 + 精确二项区间或参数化 bootstrap 替代）与资源预计；(10) 分档：K=min 折簇数、DEFF、p 硬顶；(11) 封顶小语法枚举生成器；(12) 模块划分与测试矩阵。
验收：文件 ≥200 行；含 canonical_hash、max-t、空模型、分档四节；未决单列。
验证：wc -l；grep -c 'canonical_hash'；grep -c 'max-t'。
输出要求：回复文件路径、行数、契约修订建议条数、未决条数。
```

## §3 窗口里程碑 review（P1 / P2）
```
目标：对 quant-lab G<N> 窗口 P<k> 里程碑产出做独立 review。用 gpt-6。
范围：只读仓库；只写 /Users/balen/projects/trader-bot/quant-lab/docs/adr/review-G<N>-P<k>.md。不改任何文件，可运行只读命令与该模块的 pytest（.venv-g<N>/bin/python -m pytest tests/<module> -q）。
必读：quant-lab/goals/GOAL-<N>-*.md、quant-lab/contracts/<对应契约>.md、quant-lab/docs/adr/ADR-G<N>-*.md、src/quant_lab/<module>/、tests/<module>/、quant-lab/taskList.json 中 modules.<module>。
审查要求：(1) 逐任务核 verify 命令是否真的断言了非空/非 0，实跑一次并记录输出；(2) 实现与契约逐字段对照，列漂移；(3) 实现与 ADR 对照，列偏离与理由是否成立；(4) 前视：任何用到未来信息的路径（as-of 等号、缓存穿越、夹具泄漏）；(5) 幸存偏差 / 隔离不删除 / 不填零是否被违反；(6) 凭据边界：是否 import services/*、是否读生产配置；(7) 测试矩阵缺口。
输出：必修（编号 S01…，位置/问题/改法/验收）、应改、可选；终裁 pass/fail/insufficient。
验证：wc -l ≥ 80；grep -c '必修'。
输出要求：回复文件路径、必修条数、终裁。
```

## §4 集成 adversarial review（G0 OR-05）
```
目标：对 quant-lab P1 合成端到端链路做 adversarial review。用 gpt-6。
范围：只读仓库；只写 /Users/balen/projects/trader-bot/quant-lab/docs/adr/review-G0-integration.md；可运行 .venv-g0/bin/python -m pytest quant-lab/tests -q 与只读命令。
必读：quant-lab/contracts/*.md、quant-lab/tests/integration/test_e2e_synthetic.py、三份 ADR、三份 review-G<N>-P1.md、docs/plans/2026-09-11-quant-scale-up-merged-plan.md、AGENTS.md。
关注点：前视（三时钟在跨模块边界是否被破坏）、幸存偏差（删帖样本路径）、契约漂移（三份契约 vs 实际列名/类型）、凭据边界与 AGENTS.md 铁律、回滚与重放（graph_version tombstone 后旧缓存是否被消费）、竞态（三窗口写 data/ 的分区隔离）、"静默为空"（任何断言只检查文件存在而不检查内容）。
输出：必修/应改/可选 + 终裁；每条附复现命令。
验证：wc -l ≥ 100。
输出要求：回复文件路径、必修条数、终裁。
```
