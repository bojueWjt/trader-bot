# quant-lab 集成报告（OR-06 草稿）

> **状态：草稿。** 定稿条件 = P1 三条腿全绿（见 §6）。当前 R-10 六审 fail，故本文件**不得**改名为 `INTEGRATION_REPORT.md`。
> 契约版本 **v1.55**，78 条 changeLog。本文件由 G0（session `427ed064`）出具，非独立审查。

## 1. 交付范围

三个窗口并行实现、G0 仲裁接缝的**研究层**系统。目标是把 Telegram 频道消息变成可统计评估的交易假设，**不触生产**：不 import `services/*`、不用生产凭据、不改生产。

| 窗口 | 模块 | 职责 | 源码 | 测试 |
|---|---|---|---|---|
| G1 | `data` | 消息 → episode 六级产线：归一、去重、抽取、规范化与行情校验、链接与双轨状态机、标注与分级放行 | 17 文件 / 5676 行 | 10 文件 / 2759 行 |
| G2 | `market` | Binance Vision 行情湖与分区体检、三时钟 as-of 库、`order_plan` → 规范执行事件的永续执行内核 | 10 文件 / 4598 行 | 29 文件 / 4758 行 |
| G3 | `research` | JSON AST 与算子前视契约、feature_snapshot、事件级配对评估器、walk-forward 与尝试账本、共同日历块 max-t 与空模型验收 | 17 文件 / 5490 行 | 16 文件 / 3172 行 |
| G0 | `contracts` + `tests/integration` | 接缝仲裁、看板、端到端冒烟 | 4 契约 2307 行 | 1 文件 238 行 |

## 2. 接缝清单（跨窗口调用点）

**接缝由 G0 独占**：窗口遇到接缝分歧必须 `block`，不得自行改动。

| # | 方向 | 入口 | 契约 | 关键约束 |
|---|---|---|---|---|
| S1 | G1 → G3 | `data.api.load_episodes(graph_version, decision_graph=True)` | research-schema §9 | 决策视图：`t_dec` 非空、`entry_observed`、`entry_decision` 为真；致命码整行剔除 |
| S2 | G1 → G3 | `data.api.load_episode_events(graph_version)` | research-schema §9.7 | 只含 `edge_available_at < t_dec` 的边；顺序未知取**严格小于** |
| S3 | G1 → * | `data.api.loss_table` / `quarantine` | research-schema §7 | 损耗表 1–6 层；隔离区不删除 |
| S4 | G3 → G2 | `market.contract.build_request(...)` | execution-interface §5 | `Entry.fraction` 可空且**无默认值**（B8）；未知不得猜 |
| S5 | G3 → G2 | `market.execution.simulate_batch(reqs, kernel="A"\|"B")` | execution-interface §4 | 同批 `policy_hash` 唯一；`strict` 下不得吞错为 null 行 |
| S6 | G2 → G3 | 规范执行事件 `outcome.kind` | research-schema §9.10.11 A15 | 七值封闭集；`unfilled_expired` 已并入 `cancelled`，**不得新增兜底标签**（B10） |
| S7 | G2 → G3 | `force_close_net_R` | execution-interface §5.21 B19 | **是估值不是事件**；当前**无消费方**，差分列"不适用" |
| S8 | G3 内 | `maxt.calendar_blocks` 的 `n_nonempty` | research-schema §9.10 | 最小有效样本的**唯一口径**；须与 `block_len` 成对声明 |
| S9 | 全局 | 经济量 `Decimal(38,12)` | research-schema §9.6 | 声明精度路径上**任何**有损中间转换均违约（见 §5 可复算性） |

## 3. 各模块状态与证据

**G0 亲跑，未采信窗口回执。** 测量时刻 2026-09-11 11:29:05Z，A40 判据（最近写入 <120s **或** 近 10 分钟 ≥2 次 **或** 该 venv 下有在跑进程 → 记不判）。

| 模块 | verify | 结果 | 判定 |
|---|---|---|---|
| data | `pytest tests/data` | **225 passed** | 可判，绿 |
| market | `pytest tests/market` | **358 passed** | 可判，绿 |
| integration (OR-04) | `pytest tests/integration/test_e2e_synthetic.py` | **8 passed** | 可判，绿 |
| research | `pytest tests/research` | — | **不判**：G3 正在跑 MC 重生成报告制品（PID 71877，已 29 分钟，5 worker） |

**OR-04 端到端冒烟的判别力（按 §25.16 A46 必须随绿灯一并给出）**：

| 门 | 实际 | 下限 | 余量 |
|---|---|---|---|
| 成交样本 | 78 / 80 | 40 | 1.95× |
| 走完结局（`tp_hit`/`stopped`/`filled_closed`） | 65 / 80 | 20 | 3.25× |

**这是非退化门，不是回归门**：走完结局掉到 21 仍全绿。

## 4. 里程碑判定

| 腿 | 任务 | 终裁来源 | 状态 |
|---|---|---|---|
| G1 | D-10 | **G0 裁决书**（`ruling-G0-D10-classification-closure.md`），经 verify 析取第二支 | done。**非审查方 pass**：`review-G1-P1.md` 及 r3/r4/r5 终裁均 fail。用户在 G1 会话经 `AskUserQuestion` 预授权该路径。残余 **7 条**（S08/S13/S14/V01/W01–W03） |
| G2 | M-10 | `review-G2-P1.md` **十四审终裁 pass**（审查方自行给出） | done。10 项 partial-P2，`P2_UNSUPPORTED` 四键如实声明 |
| G3 | R-10 | **G0 范围重定裁决**（`ruling-G0-R10-scope-and-acceptance.md`）+ 具名能力证据 | **done**，G0 亲跑新 verify rc=0 / 403 passed。**非审查方 pass**：`review-G3-P1.md` 十五轮均 fail，43016 行原样保留。第 9–15 轮 **15 条对抗类反例保持 open**（能力文档 §2.2），已由 G0 正式接受残余风险 |

**用户决策闸门六项全部 pending，但无一挡 P1**（`affects` 为 D-05/08/09/11、R-05/08/09、OR-06）。`integration.blockedByUserGate = false`。

## 5. 五类必闭合项（用户裁定的分类闭合标准，仅适用 D-10）

按 §25.14 **A44** 复核后的现状。详见裁决书 §6–§7。

| 类别 | 判据形式 | 证据 |
|---|---|---|
| 可复算性 | 类级 | **突变证明**：`RecordedClient.from_file` 真实路径，摘掉 `parse_float=Decimal` 即静默改量 `-3E-12` |
| 前视 | 类级 | **突变证明**：同一条边 `t_dec−1s` 可见 / `t_dec+1s` 排除 / 同刻排除（严格 `<`） |
| 致命降级 | 类级 | **突变证明**：注入两个致命码各致剔除（80→79），非致命码不受影响 |
| 安全越权 | **枚举** | 7 类逃逸向量全 REJECT，拒绝在取 hash 之前。**不宣称类级证明** |
| 幸存偏差 | **无门** | `loss_table` / `quarantine` 直读 parquet，读侧无恒等校验；恒等式仅由构建期测试 S12 保证 |

## 6. readyForStitch 判定标准

**当前 `readyForStitch = false`。** 置 true 需**全部**满足：

0. **R-11 闭合**（依赖内容哈希并入制品身份，裁定 §10 的 P1 阻断项）。
1. **P1 三条腿全绿**：`review-G<N>-P1.md` 终裁 pass，或经用户授权的 G0 分类闭合裁决（现状：G1 ✅ 裁决、G2 ✅ 审查、G3 ✅ 裁决 + 能力证据）。
2. **OR-05 对抗式集成 review 无未闭合必修项**（现状：todo，本会话禁止派发）。
3. **接缝清单 §2 九条全部有实跑证据**，且 `force_close_net_R`（S7）的"无消费方"结论仍成立。
4. **已知缺口表逐条有债主与触发事件**，无"待回填"占位。
5. **统计声明闸门 `G-STAT-CLAIM`**：未批时报告**只描述不声明**，`readyForStitch` 可为 true 但**任何 θ 声明须另行授权**。

## 7. 已知缺口

见 `docs/known-gaps-draft.md`（方法边界 4 条、范围边界 6 条、统计口径 4 条、各窗口 partial-P2 三组）。定稿时并入本文件。

## 8. 定稿前须补

- [ ] R-10 收口后补 G3 最终残余清单与终裁引用（**路径已裁定：走七审，不发 G0 裁决书——§25.12 A43**）
- [ ] OR-05 对抗式 review 结果
- [ ] 接缝 §2 九条的实跑证据索引
- [ ] `readyForStitch` 终判与依据
