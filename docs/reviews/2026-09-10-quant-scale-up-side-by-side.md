# 规模化修订方案：Claude 稿 vs Codex 稿 并排比对

日期：2026-09-11。比对人：Claude。对象：
- Claude 稿：`docs/plans/2026-09-10-quant-scale-up-claude-plan.md`（约 230 行）
- Codex 稿：`docs/plans/2026-09-10-quant-scale-up-codex-plan.md`（317 行，gpt-6）

两稿由同一 prompt 独立写成，互未读对方。本文只做比对与初步取舍，**不是终裁**；终裁等 gpt-6 从零审两份后再定（prompt 见 `2026-09-10-quant-scale-up-review-prompt.md`）。

## 0. 我对 Codex 稿做的事实抽查（2026-09-11）

| 断言 | 结果 |
|---|---|
| Qlib v0.9.7 `Ref(x, 0)` 返回首值广播，负 N 取未来 | 成立（源码 docstring 与 `series.iloc[0]` 实现，今日核） |
| DEAP `gp.compile` / `PrimitiveTree.from_string` 走 `eval` | 成立（master 源码，今日核） |
| AlphaGen 仓库 ICT-FinD-Lab，commit `259687e` 2026-06-04 存在 | 成立（commit 页存在，内容为 Update citation） |
| Wilson 零错双侧 95% 上界：n=80 4.58%、200 1.88%、400 0.951% | 复算一致 |
| 费用表算术、984 天、416 万行、d* = 0.15/2.15 = 6.98%、[54%, 64%] 例 | 复算一致 |
| 合并 review 仍残留"306 … 大概率只到 M3-R"类推断 | 成立：`merged-review.md` 第 52、91、117 行 |
| Polars 版本 1.44.1（2026-08-26） | 已过时：PyPI 今日 1.44.2（2026-09-09），不影响结论 |

## 1. 逐节比对

| 节 | 一致 | Codex 稿更强 | Claude 稿更强 | 初步取舍 |
|---|---|---|---|---|
| A 规模估算 | 都给情景区间不给点估；都要求 PoC 1a 实测同一批量 | 分层公式 `N_seed = Σ M×p×b×u` 把多腿、去重、版本显式分开；禁止把 2026 密度套到 2024；"信号数"拆七层计数 | 用 watcher 时代同频道同期做"导出计数 / watcher 计数"校准比 | 取 Codex 公式 + Claude 的 watcher 校准比 |
| A 证据等级 | 都以"版本可用时刻"为决策时刻、都禁止回填初始时间 | 五级 V/H0/H1/H2/U 更严：无编辑标记只叫"未见编辑"；H1 允许研究"编辑后新决策"；每条带 `survival_scope`；`N_cluster_V` 与 `N_cluster_H0` 分报不相加 | E0–E4 命名更直观；把 watcher 已删除样本（E3）单独作为"被删的是什么"分析对象 | 取 Codex 五级，并入 Claude 的 E3 用途 |
| A 幸存偏差 | 都用 watcher 删帖率外推历史 | 首次观察即入组的前瞻 cohort、7/30/90 天累计删除率、捕获率下界 q_min、κ 迁移敏感性、尾部 τ、最坏界闭式 `[(1-d)p, (1-d)p+d]`、`d* = μ/(μ-L)` 阈值 | 只有 d/2d/最坏三条线和 25% 门槛，粗 | **取 Codex**。Claude 稿 A4 的"watcher 删除记录 = 真值"是过度假设 |
| B 产线 | 都是五级；都要求 LLM 裁决必须引用证据 id；链接器保留未选边 | 描述图 vs 决策图（只含 `edge_available_at ≤ t_dec`）；左截断；`claimed_outcome` 与 `reconstructed_outcome` 分列；`ENTRY_REPORTED` 是作者声称，不自动创造成交；同频道同币同向 ≤72h 仅召回弱边 | 明确"链接器先在 watcher 时代调通再上历史"（PoC 9），那里有全版本与删除真值 | 取 Codex 状态机与 schema，加 Claude 的 PoC 9 |
| B 人工上限 | 都按 episode 计金标 | 开发金标 200 与冻结验收金标 400 分开；**漏检审计**（150 个无信号窗口找漏检）；主验收随机样本不与定向补审混算；硬上限 80 人时并说明不是 3–4 小时 | 47 人时估计偏乐观 | **取 Codex**。Claude 稿缺漏检审计，是实质缺口 |
| B 费用 | 都是低可信情景，都引官方 Batch 价 | 按阶段拆（初筛 / 抽取 / 裁决 / 重试），thinking 计费，促销到期翻倍 | 消息总量情景更大（15 万 vs 5 万） | 取 Codex 表结构，消息量用 PoC 1a 实测替换；建议预算上限 $300–1000 由用户定 |
| C 引擎候选 | 都推荐自写类型化 AST + Polars 后端；都排除 AlphaGen 作代码来源；都要求截断重算 + NaN 注入 | Qlib `Ref(0)` 语义陷阱；DSL 只收 JSON 不收 Python 字符串（DEAP/Qlib 的 `eval` 路径不得直接喂外来表达式）；深度 ≤4、节点 ≤15、窗口集合预注册；算子登记字段表；三来源共用 `hypothesis` 记录结构 | 多一个候选：polars_ta 0.5.17 作算子后端兜底（约七成 Alpha158 算子现成），可降 TCO | 取 Codex 契约与 DSL 约束；polars_ta 作 PoC 8 的第二后端参与对拍，过同一契约才用 |
| C 评估器 | 都否定横截面 IC，都用事件级决策锚点 | `θ = Σw(R_cand − R_base)/Σw` 配对定义；风险分母入场前固定；未成交记 0 但删失不记 0；at-risk 机会集不可复原时只能谈信号过滤不能谈择时 | 三层目标表（L2/L3a/L1）更直观 | 取 Codex 定义，保留 Claude 的三层目标表作索引 |
| C 多重比较 | 都要求逐次记账、最终时序测试锁箱 | **共同日历块 max-t bootstrap（Reality Check / SPA 同类）作主校正**，DSR 只在有真实资金曲线时辅助；每个外训练窗再拆 search/selection；指出 2026 模型可能记得 2024–2025 行情（U09），最终无前视主张需冻结后的前瞻 cohort | 只写"DSR 或等价" | **取 Codex**。DSR 针对 Sharpe 序列，事件级配对差用 max-t 更对 |
| D 分档 | 都按簇数不按信号数；都声明阈值是预注册选择；都引 EPV 局限 | **K = 各内层训练折簇数的最小值**而非全历史总数；DEFF 设计效应降档；`p ≤ floor(K/20)` 与分类 `p ≤ floor(E_min/10)` 双保护栏；五档配置上限 12/64/512/2048；每档要求时间跨度与每折簇数；`n ≈ (zσ/δ)²` 量级规划 | 四档更简单 | **取 Codex**。Claude 稿按总簇数定档是错的，应按折 |
| E PoC | 都保留回测 A/B（原 5）；都新增 DSL 契约 PoC | 7a 协议冻结前置到看收益之前；3 拆 3a/3b；8 拆 8a/8b；依赖图完整；45 工程日 + 80 人时 | PoC 9 链接器先跑 watcher 时代；DEAP 人造信号必须找回、打乱标签必须找不到 | 取 Codex 顺序，插入 Claude 的 PoC 9 与 GP 负对照 |
| F 冲突 | 都指出 PoC 6 的"≤10 维"、PoC 2 起点、M1 金标单位、M3 门槛按簇 | F03 合并 review 残留 306 推断；F04 "图片纠错率 >30% 全转人工"在几万条下不可控；F12 **新产线应用独立研究账号，不复制生产 watcher 会话材料** | F3 明确 AlphaGen 算子清单可作参考、代码不可复制 | F12 是新的用户决策项，见下 |
| G 决策 | 都列次号授权、删帖接受度、预算、阈值 | 云模型数据范围与脱敏；第二复核人；研究问题范围（信号过滤 vs 自主择时） | DEAP LGPL、行情回溯存储 | 合并 |

## 2. 两稿都没覆盖、需要 gpt-6 审时补的

1. **Telegram 导出的 `edited` 字段是否可靠区分"未编辑"与"字段缺失"**：两稿都依赖它定 H0/E1，报告 §6 已列为"未实跑核"。PoC 1a 必须先核。
2. **同一交易员跨频道复制**（Titan 与 Gauls 是否转发同源）：Codex 的 must-link 提到，但没有给检测规则；Claude 稿没提。
3. **图片计划帖的 episode 组装**：坚果TV 大量纯图，两稿都把 OCR 交给第 1 级，但没说图片入场帖与后续文字管理帖怎么链接。
4. **Nautilus A/B（原 PoC 5）与 Codex 的 `EventEvaluator` 的接口**：评估器需要"既定执行语义合同"输出净 R，这个合同在原 PoC 5 定，两稿都没写两者的数据契约。

## 3. 初步结论

以 Codex 稿为主干，并入 Claude 稿的五点：watcher 校准比、E3 删帖样本用途、PoC 9 链接器先跑 watcher 时代、polars_ta 作第二后端对拍、GP 人造信号/打乱标签负对照。Claude 稿的 A4 幸存偏差定界、B3 人工时、D 按总簇数定档三处判定为不足，弃用。

新增须由用户拍板的决策（Codex F12）：新历史产线是否改用**独立研究账号**，从而不再需要合并 review §10 里"复制生产 watcher 会话材料"这项授权。

以上取舍待 gpt-6 从零审两份后确认或推翻。
