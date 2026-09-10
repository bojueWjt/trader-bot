# 任务：从零独立审两份"规模化修订方案"，出终裁

你是独立审稿人，用 gpt-6。两份方案由同一 prompt 独立写成，作者互未读对方。你的产出是终裁，不是折中：每一节指出哪一稿对、哪一稿错、两稿都错的地方是什么。

## 必读（只读）

- 题目：docs/plans/2026-09-10-quant-scale-up-codex-prompt.md
- 方案一：docs/plans/2026-09-10-quant-scale-up-claude-plan.md
- 方案二：docs/plans/2026-09-10-quant-scale-up-codex-plan.md
- 背景：docs/research/2026-09-07-quant-toolchain-selection/2026-09-07-quant-toolchain-selection-report.md
- 背景：docs/plans/2026-09-06-channel-seed-to-quant-merged-review.md
- AGENTS.md（铁律：RESUME/开闸只凭用户明示；不碰用户手工单；研究层零生产凭据；不改生产）

禁止读取：docs/reviews/ 下除本文件外的任何文件（尤其 `2026-09-10-quant-scale-up-side-by-side.md`）。不要参考任何既有 review 的结论。

## 审稿要求

按 A–G 逐节，每节给一张表：`条目 | 方案一 | 方案二 | 裁定（一对 / 二对 / 都对 / 都错） | 理由与证据`。然后：

1. **事实核验**：两稿引用的版本号、日期、许可证、源码语义（如 Qlib `Ref(0)`、DEAP `eval`、AlphaGen 许可证、Polars 版本、Telegram `edit_date` 语义、Wilson 数值、费用算术）逐条复核，写"已核 / 未核 / 错误"。你有联网就核，没有就标未核，不得凭记忆判"已核"。
2. **统计方法**：幸存偏差定界（方案二 A.3 的 q_min/κ/τ 与最坏界）、分档规则（方案二按折内最小簇数与 DEFF，方案一按总簇数）、多重比较（方案二 max-t bootstrap 为主、方案一 DSR 或等价）、Wilson 准入阈值——哪个正确、哪个有漏洞、哪个是不必要的复杂。
3. **产线设计**：episode 组装五级、状态机、左截断/右删失、描述图 vs 决策图、漏检审计——指出会在真实数据上失效的地方。
4. **引擎选型**：两稿评分差异（方案二 Polars 83.25 / Qlib 76 / DEAP 77.25 / AlphaGen 58.5；方案一另有 polars_ta 78.25、穷举 86、gplearn/PySR 67.5）是否合理；polars_ta 该不该进候选；DSL 只收 JSON 的约束是否必要。
5. **两稿都没覆盖的问题**：至少找出三个，并说明为什么必须在 PoC 前解决。
6. **必修清单**：编号 S01…，每条写"位置、问题、改法、验收方式"。区分必修 / 应改 / 可选。
7. **终裁**：合并后的方案应以哪一稿为主干、并入对方哪些条目、弃用哪些条目；这份合并稿能否作为选型报告新增 Q6 与修订 Q5/PoC 清单的依据；哪些项必须等 PoC 1a 实测后才能定。

## 写作约束

- 只读仓库，只写一个输出文件，不修改任何现有文档，不运行改动状态的命令。
- 每个非平凡论断标可信度（高/中/低）与来源；"已核实"与"未核实"分列。
- 中文，不超过 350 行，表格优先。不复述两稿内容，只写判定。

## 输出

写到：/Users/balen/projects/trader-bot/docs/reviews/2026-09-11-gpt6-review-of-scale-up-plans.md

完成后回复：文件路径、行数、必修条数、你判为"都错"的条目数。
