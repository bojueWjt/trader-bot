# quant-lab 接口契约总纲 (Contracts)

> 本文件是 quant-lab 的「接缝真相」。所有窗口以它对齐，**由 GOAL-0 维护**，其余窗口只读，发现不一致就在 `taskList.json` 里提 blocker。路线依据是合并稿 `docs/plans/2026-09-11-quant-scale-up-merged-plan.md`（下称"合并稿"），契约与合并稿冲突时以合并稿为准并由 G0 改契约。

## 0. 项目一句话

`quant-lab`：把五个 Telegram 信号频道 2024 年起的全历史整理成可评估的交易 episode，在自有表达式引擎与事件级评估器上做可复算的研究；产出只进 `research_only`，任何上线、影子、RESUME 都要用户另行明示。质量底线：**无前视（三时钟 as-of）、无幸存者偏差掩盖（隔离不删除、损耗可数）、零生产凭据。**

## 1. 仓库布局（所有窗口共用）

```
quant-lab/
├─ taskList.json            # 唯一看板（scripts/task.py 读写）
├─ scripts/task.py          # 看板 CLI（stdlib）
├─ contracts/               # 本目录，G0 维护
│   ├─ README.md
│   ├─ research-schema.md   # G1 provides：消息版本 / episode / 原因码 / quarantine / 损耗表
│   ├─ execution-interface.md # G2 provides：行情湖分区、三时钟 as-of、执行请求/结果
│   └─ feature-snapshot.md  # G3 provides：AST JSON、算子登记、feature_snapshot、EventEvaluator、账本
├─ goals/GOAL-0..3          # 各窗口目标文件
├─ docs/adr/                # Codex 出的引擎设计 ADR（ADR-G1/G2/G3）
├─ requirements/{base,g1,g2,g3}.txt  # 钉死版本
├─ src/quant_lab/{data,market,research}/
├─ tests/{data,market,research}/
└─ data/                    # 研究湖（gitignore；bronze 只读带哈希）
    ├─ lake/telegram/{bronze,silver,gold}/
    ├─ lake/market/{bronze,silver}/
    ├─ quarantine/
    └─ lockbox/
```

语言 Python 3.12（`.python-version`），包管理 uv，包名 `quant_lab`，测试 pytest。

## 2. 看板协议

**铁律：**
1. 只用 `python3 scripts/task.py` 改看板，不手改 JSON。
2. 只动自己模块的 `modules.<module>` 子树；`contracts` / `integration` 只有 G0 能写。
3. 完成一个任务或状态变化立即上报。被卡 → `block`；对方就绪 → 自己 `unblock`。
4. **自报 done 不算完成**：G0 以实跑 `verify` 命令且产出非空为准。
5. **verify 命令的 collect 检查（2026-09-11 实测修订）**：pytest 9.1.1 输出是 `N tests collected`，**不是** `collected N items`。看板里 D-01 / M-01 / R-01 写的 `--co 2>&1 | grep -E 'collected [1-9]'` 永远匹配不上（实测 grep 退出码 1），整条 `&&` 链必失败。各窗口自行把本模块该行改成规范写法：
```bash
.venv-gN/bin/python -m pytest tests/<mod> --co -q 2>&1 | grep -Eq '[1-9][0-9]* tests? collected'
```
6. 顺带：`grep -q` 比 `grep -E` 更适合放进 `&&` 链（不污染输出）；任何 verify 都必须在前台实跑核对过 grep 模式，禁止照抄未验证的模式。

```bash
python3 scripts/task.py show
python3 scripts/task.py claim data D-03
python3 scripts/task.py report data --status in_progress --progress 0.4 --note "..."
python3 scripts/task.py done data D-03
python3 scripts/task.py block research --on "契约: feature-snapshot §3 缺 weight 列"
```

## 3. 环境仲裁（防多窗口互相拆台）

1. `.python-version` 钉 3.12；`requirements/*.txt` 钉版本；改版本走 G0 契约变更。
2. **每窗口隔离 venv**：`.venv-g1` / `.venv-g2` / `.venv-g3` / `.venv-g0`，绝不共用 `.venv`，绝不重建别人的 venv。
3. 镜像与重试在 `uv.toml`；安装失败换镜像重试，不换环境。
3b. **本机没有 `python` 命令，只有 `python3`**：看板一律 `python3 scripts/task.py ...`；跑模块代码一律用本窗口 venv 的解释器（`.venv-gN/bin/python`）。目标文件里写成 `python scripts/task.py` 的地方按此替换。
4. 长驻服务（Label Studio、DuckDB 查询服务）用 `nohup` 起，端口固定：Label Studio 8081、任何 dev server 8090+，不抢生产端口。
5. 研究湖 `data/` 是共享目录：**bronze 只读带 sha256，只有 G1（telegram）和 G2（market）各写自己的分区**，G3 只读。

## 4. 模块接缝表

| 模块 | 窗口 | provides | consumes | 契约文件 |
|---|---|---|---|---|
| data | G1 | `data/lake/telegram/*`（MessageVersion / ExtractedEvent / Episode Parquet）、quarantine、损耗表、金标与放行元数据 | market 的 as-of 库与 1m 标记价（行情校验层） | research-schema.md |
| market | G2 | `data/lake/market/*` 分区与 manifest、`quant_lab.market.asof`、执行合同（ExecutionRequest→ExecutionResult）、候选 A 参考实现与 A/B 报告 | data 的历史种子品种清单（2b 扩展时） | execution-interface.md |
| research | G3 | AST/算子注册表、`feature_snapshot`、`EventEvaluator` 与 θ、walk-forward/账本/max-t/空模型、分档报告 | data 的 Episode 表；market 的 as-of 与 ExecutionResult | feature-snapshot.md |
| orchestrator | G0 | contracts/、端到端冒烟（合成数据）、INTEGRATION_REPORT.md、Codex 集成 adversarial review | 全部 | — |

**并行开工顺序**：G2 先在 `execution-interface.md` 定死 as-of 签名与 ExecutionRequest/Result 字段（M-01 之内），G1 在 `research-schema.md` 定死 Episode 字段（D-01 之内），G3 按两份签名写桩开工；不等真实数据。

## 5. Codex 参与规则

- **引擎设计**：每个窗口开工核心实现前，先用 codex-dispatch 派 ADR（任务书模板见 `docs/agent-team/quant-lab-codex-briefs.md`）：ADR-G1 episode 引擎、ADR-G2 执行内核、ADR-G3 表达式与统计引擎。ADR 落到 `docs/adr/`，窗口按 ADR 实现；ADR 与契约冲突走 block 让 G0 仲裁。
- **里程碑 review**：每个窗口在 P0/P1/P2 收口时派 Codex review（同一模板），review 文件落 `docs/adr/review-G<N>-P<k>.md`，必修项闭合后才能置里程碑 done。
- **集成 review**：G0 在 OR-05 派一次 adversarial review，关注前视、幸存偏差、契约漂移、凭据边界。
- Codex 只读仓库 + 只写指定输出文件；禁止 `--resume-last`；模型 `gpt-6-astra`，effort 默认 `medium`（`high` 在网关路径下曾被上游杀死，直连 ChatGPT 登录可试）。

## 6. 用户决策闸门（合并稿 H.2 六项；未拍板前对应任务保持 `todo` 并标 gated）

闸门 ID 与 `taskList.json.integration.blockers[*].gate` 一一对应，状态 `pending|approved|declined` 以看板为准，**只有用户明示才能改状态，任何窗口不得自行推定"默认批准"**。

| # | 闸门 ID | 合并稿 H.2 决策 | 未拍板时的出口 | 影响的任务 |
|---|---|---|---|---|
| 1 | `G-ACCOUNT-SCOPE` | 独立研究账号与频道范围（TDesktop 导出授权；单频道先行→五频道） | 仅合成夹具；**禁止用生产 watcher 会话/凭据或其落盘会话材料代替独立研究账号** | D-08、D-11 |
| 2 | `G-RETENTION-CLOUD` | 云上传范围与数据保留/撤回合同（合并稿 H.1，逐对象批准，默认不上传） | 真实聊天记录一律不得进入 `data/`；只跑本地合成 | D-05（真实 LLM 调用）、D-08、D-11 |
| 3 | `G-OC-200-3` | 一般错误 200/3 放行门与约 14.72% RQL 误收风险接受（或另预注册 (n,c)）；致命零容忍不变 | 放行**工具**可先做，真实批次放行不得执行 | D-09（真实批次放行）、D-11 |
| 4 | `G-BUDGET-LABOR` | 真值人力与预算：主审核/独立复核、≤80 人时、API ≤300 美元、45 工程日为规划量 | 不发起付费 API 批量调用与人工标注排产；用录制夹具 | D-05、D-09、D-11 |
| 5 | `G-STAT-CLAIM` | 统计声明与风险容忍：冻结 V 的最终前瞻窗口、尾损/回撤上限、成本压力档（κ/τ 只作场景，不得当既成事实） | 只描述不声明；报告里 κ/τ 标为"未批准场景" | R-08、R-09、OR-06 |
| 6 | `G-LICENSE-SEARCH` | 许可与延后搜索：AlphaGen 代码复用需许可证据、DEAP 交付方式另核；GP/E 档位再议 | 只做封顶枚举，不引入受限代码；**研究许可 ≠ 上线授权** | R-05、R-09 |

合成数据（fixtures）上的全部工作不受闸门限制，P0 与 P1 全部在合成数据上完成。P2 的真实数据任务须先在看板见到对应闸门 `approved`。

## 7. 铁律（AGENTS.md，任何窗口不得违反）

RESUME/开闸只凭用户明示；不碰、不撤销、不针对用户手工单告警；研究层零生产凭据；不改生产；不 import `services/*`。

## 8. OR-01 接缝消歧记录（G0，2026-09-11，已闭合）

**【2026-09-11 03:20 更新：4 项全部闭合，`contracts.frozen = true`】** G1 在 D-01 note、G2 在 `docs/adr/report-G2-contract-revision-M01.md` 分别提交修订，G0 已裁定并并入子契约：

| # | 结论 | 落在哪 |
|---|---|---|
| 1 `t_dec` | `gold/episode` 的**实列**，`= max(闭包依赖 available_at) + processing_delay_s`；`decision_eligible_at` 降为诊断列 | research-schema §9.3、feature-snapshot §7.1 |
| 2 构造属主 | 三段式：G1 落 `order_plan`/`t_dec`/`decision_snapshot_hash` → G2 `build_request(...)` → 调用方给 `risk_budget`/policy/场景/seed | research-schema §9.4 |
| 3 `net_R` | `decimal?`；删失 null（排除计损耗）、未成交 0、skip 由 G3 记 0 | execution-interface §5.4、feature-snapshot §7.4 |
| 4 `mark_price_at` | 加 `marks: pl.DataFrame` 首参，隐式全局状态消除 | execution-interface §5.1（R1） |

另有 4 项 G0 主动裁定：`instrument_id` 改 `BTCUSDT-PERP.BINANCE-UM`（Nautilus 实测，旧格式 `from_str` 直接 ValueError）、`policy_hash` 必须进 `ExecutionRequest` 与 `trace_hash`（否则改费率不改版本串会静默复用 `trace_hash`）、`leg` 枚举含 `funding`、`simulate_batch` 不含 baseline/candidate 概念（G3 自行配对）。详见各子契约的定稿修订节。

**冻结后改接缝的唯一路径**：窗口 `block` → G0 仲裁 → 改 `contracts/` + 写 `changeLog` → 看板广播。下表保留作历史记录。

| # | 接缝问题 | 归属 | 现状 | 需要的答案 |
|---|---|---|---|---|
| 1 | `t_dec` 到底落在哪张表 | G1 (D-01) | `research-schema.md` §2 的 `gold/episode` 只有 `decision_eligible_at`，没有 `t_dec` 列；但 `feature-snapshot.md` §3 的 `anchors` 要求 `episode_id, instrument_id, t_dec`，`execution-interface.md` §3 的 `ExecutionRequest` 也要 `t_dec` | 明确 `t_dec` 是 `gold/episode` 的实列还是由 `decision_eligible_at` 派生；若派生，给出确定性公式与"冻结处理延迟"的取值来源 |
| 2 | Episode → `order_plan` / `risk_budget` 由谁构造 | G1+G2 (D-01/M-01) | `gold/episode` 无 `order_plan`、无 `size`、无 `risk_budget`（`size_hint` 只在 `silver/extracted_event`）；`ExecutionRequest` 却要求完整冻结计划。三份契约都没写这个映射的属主 | 指定属主模块与函数签名（建议 `quant_lab.market.contract.build_request(episode_row, events, *, policy_version, risk_budget) -> ExecutionRequest`），并写清 `risk_budget` 的确定规则（入场前固定，移动 SL 不重置） |
| 3 | `net_R` 的可空性自相矛盾 | G2 (M-01) | `execution-interface.md` §3 表里 `net_R` 是 `decimal`，同节不变量却要求"`censor_reason` 非空时 `net_R` 为 null"；`feature-snapshot.md` §4 又说候选 NaN → `skip(0)` | 定死 `net_R: decimal?`，并给出 null / 0 / NaN 三者的判定表（skip=0、未成交=0、删失=null、证据缺失=排除并计损耗） |
| 4 | `mark_price_at` 没有数据来源参数 | G2 (M-01) | 签名 `mark_price_at(at, instrument_id, *, max_staleness_s=120)` 不带 bars/manifest/lake 路径，等于隐式全局状态，破坏可复算与 `market_manifest` 版本钉死 | 补显式来源参数（如 `*, bars: pl.DataFrame` 或 `market_manifest: str`），或明确它是绑定 manifest 的类方法 |

补充事实（G0 于 2026-09-11 实测）：`quant_lab.data.codes` 的 `ReasonCode` 与 `research-schema.md` §4 的 28 个原因码**逐字一致**（missing 0 / extra 0），`FATAL_REASONS` 与契约的三个致命码一致，`TimeGrade / PlanState / ClaimState / LinkMethod / QuarantineStatus / LabelStatus` 均与契约一致 —— G1 侧枚举接缝已对齐，无需再谈。
