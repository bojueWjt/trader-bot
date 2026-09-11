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

   **5.1 分区归属（2026-09-11 OR-02 R5 加严，规范性）**——每个窗口**只准**删改自己名下的路径：

   | 窗口 | 可写/可删路径 | 其余路径 |
   |---|---|---|
   | G1 data | `data/lake/telegram/**`、`data/quarantine/telegram.parquet` | 只读 |
   | G2 market | `data/lake/market/**`、`data/quarantine/market.parquet` | 只读 |
   | G3 research | `data/lockbox/**`（尝试账本）、`data/cache/research/**`（自建）；`data/lake/**` 一律**只读** | 只读 |
   | G0 orchestrator | 不写 `data/`（集成冒烟用 `tmp_path` 或 `data/tmp/e2e-*`） | 只读 |

   **5.2 禁止对研究湖根做删除。** 任何窗口的源码、脚本、任务 `verify`、临时命令中**不得**出现对 `data/`、`data/lake/`、`data/quarantine/`（目录本身）的 `rm -rf` / 递归删除 / 重建。清理只能精确到本窗口分区（例：`rm -rf data/lake/telegram data/quarantine/telegram.parquet`），且必须是**幂等重建**流程的一部分。

   **5.3 违规判定**：删改他人分区 = **契约漂移，G0 一律判 `fail`**，该窗口当轮里程碑不得推进，并须(a) 看板 note 立案，(b) 协助被害窗口重建，(c) 把致害命令从源码与 `verify` 中移除后由 G0 复核命令全文。

   **5.4 判例**：2026-09-11 04:38 / 04:44 G1 两次 `rm -rf data` 清空 G2 真实行情湖（`data/lake/market`）。已由 G1 立案、致歉、整改（D-07 `verify` 收敛为 telegram 子树），G2 重新入湖。本条即由该事故生成。

   **5.5 G0 复核义务**：任何含删除动作的 `verify`，G0 在实跑前必须先打印其全文并确认删除范围不触及 §5.2 的根路径；不满足即**不跑并判 fail**。

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


## 9. 裁定 A21：审计记录必须入库，报告不得先删旧终裁再写新终裁（G0 OR-02 R38，changeLog #43）

### 9.1 `docs/adr/` 必须纳入版本控制

**事实**：截至 R38，`quant-lab/docs/adr/` 的 **21 份文件、772K** 从未进入 git——三份引擎 ADR、G1/G2/G3 的全部审查轮次、闭合表、观察窗与 estimand 提案，全部只存在于工作区磁盘上。它们**未被 `.gitignore` 排除**，只是从未 `add`。

**这是 G0 的疏漏**：每轮都在提交 `contracts/` 与 `taskList.json`，却从未提交它们所**援引的证据**——裁定被版本化了，裁定所依据的材料没有。已于 R38 一次性入库（commit `c14eee7`）。

**裁定**：`docs/adr/**` 与 `contracts/**`、`taskList.json` 同级，属**必须入库**的审计记录。任何窗口产出新的 ADR、审查报告或提案后，由 G0 在当轮评审中提交。

### 9.2 报告不得"先删旧终裁、再写新终裁"

**风险在本轮变成具体的**：九审正在原地重写 `review-G2-P1.md`，**已删除八审终裁行而新终裁尚未写入**，文件当时处于"零终裁"状态。若该轮悬死（前若干轮确实多次悬死），八审结论将**永久丢失**，且当时无 git 历史可恢复。

**裁定**：
1. 审查报告**只追加、不覆盖历史轮次的判定文字**（G2 此前"历史轮次原文保留"的做法是对的，应保持）；
2. 新一轮的终裁行**追加在文末**，旧轮终裁行**原样保留**并以轮次名区分；review 类 verify 按 §9.9 A7 取**最后一条**，行为不受影响；
3. 确需整体重写（如 G2 曾把正文外移为 `rounds-1-2.md` 快照）时，**必须先提交当前版本入库**，再重写。

**一般原则**：**在覆盖一份记录之前，先让它可恢复。** 这与 §9.10.2 的"published 不可原地变更"、§9.8 A6 的"改门须同时发布制品"是同一条纪律在文档上的体现——G1 曾因同名原地改写 parquet 触发判例 2，本条防的是同一件事发生在审计记录上。


## 10. 裁定 A22：无法区分成功与失败的检查 + 入库到正确状态（G0 OR-02 R39，changeLog #44）

### 10.1 更正 G0 在 R38 的说法

R38 称 `docs/adr/` "已一次性入库（`c14eee7`）"，语气上像是问题已解决。**G2 更正属实，G0 实测确认**：`git show c14eee7:quant-lab/docs/adr/review-G2-P1.md` 为 **318 行、零终裁行**——入库的正是"删旧未写新"那个窗口，**该动作把损坏状态固化了**。

**教训**：**"入库"与"入库到正确状态"是两件事。** 提交一份正在被改写的文件，得到的是一个损坏快照的永久记录，不是一份保险。

### 10.2 但八审终裁并未丢失 —— 看板是有效的第二记录

G0 实测：`git show 323c9cb:quant-lab/taskList.json` 的 `modules.market.review.note` 逐字记载 **"八审**实际终裁为 fail**（319 行，未完成标记 0，G0 已复核）"**。该记录与损坏的报告文件相互独立，且已入 git。

**由此确立一条规范（此前只是 G0 的习惯）**：G0 的评审 note **必须逐字记录关键结论本身**（终裁文字、行数、计数、关键数值），**不得只写"见某文件"**。理由本轮已被验证：**引用会随被引用物一起损坏，抄录不会。** 看板与报告是两条独立链路，任一条损坏时另一条仍可复原事实。

### 10.3 一个无法区分成功与失败的检查，比没有检查更坏（采纳 G2 的自陈，列为规范）

G2 自陈：其突变自证脚本用"还原后 `rc` 是否为 1"作为还原检查，而 **"文件正确还原（终裁 fail）"与"文件被毁（找不到终裁行）"都得 `rc=1`**——该检查**在原理上无法区分成功与失败**，于是脚本在文件已被破坏时报告"已还原 ✓"。八审终裁行即在此丢失。已改为内容 SHA256 比对。

**裁定：凡"还原/回滚/恢复/同步完成"类检查，必须使用能区分目标状态与损坏状态的判据**（内容哈希、逐字比对、行数+关键行联合断言），**禁止使用被检查对象的退出码或存在性**作为还原成功的证据。

**归入同族并给出其位置**：本族此前各例是"用看似合理的值悄悄替换真实值"（兜底贴标签、`or` 吞假值、有损转换、未知当 1）；本例是它在**验证层**的对应物——**一个看起来在把关、实际无法失败的检查**。危害更隐蔽：前者骗的是数据，后者骗的是"我们已经检查过了"这一信念。与 §5.16 B14 的定级一致，属最重档。

### 10.4 落实

1. **每轮审查落盘后立即提交**，不攒（采纳 G2 建议）。攒着提交会让"提交时刻恰好落在重写窗口内"的概率随攒的轮数上升。
2. 提交审查报告前，G0 **先检查该文件是否处于自洽状态**（终裁行存在、无未完成标记、行数与上轮比较合理）；处于重写窗口内的文件**等待其自洽后再提交**，并在看板记录等待原因。
3. G2 新建的只追加台账 `docs/adr/review-G2-P1-verdicts.md`（一至八审终裁 + 计数 + 来源）认可，纳入必须入库范围；**它不参与 verify 判读**（仍按 §9.9 A7 读正文最后一条），只作可恢复性保险。
4. G2 已修正其任务书中"全文件只允许一条终裁行"的要求——该要求与 §9 A21「只追加」直接冲突，且 M-10 的 `tail -1` 判读**本就兼容多条终裁行**。**A7 当初留的余量是对的，是任务书把它用掉了。**


## 11. 裁定 A23：源码文本断言的两种用法必须分开（G0 OR-02 R40，changeLog #45）

G2 按 §10 A22 自查验证层，报出 6 处 `inspect.getsource` 文本断言。G0 逐条核实（`tests/market/test_constants_effective.py:113/114/123`、`test_outcome_kind.py:252/253/264`），**结论比"全部替换"更细**：这 6 处分属两类，只有一类无效。

### 11.1 正向委派断言 —— 无效，必须改为行为证明

形如 `assert "first_grid_point" in getsource(KernelA._first_bar_gap)`、`assert "derived_t_start" in src`、`assert "resolved_t_start" in src`。

**文本出现不等于该路径真的被走。** 调用被条件短路、调用了却丢弃结果、调用在死分支里——断言照样绿。**它在原理上无法因"委派失效"而失败**，正是 A22 定义的形态。

**裁定**：证明"确实委派给单一来源"必须用**行为证据**——把单一来源函数 monkeypatch 成哨兵值，断言调用方的**可观察输出随之改变**。改变则委派成立；不变则委派是假的。

### 11.2 负向模式禁令 —— 合法，但须正名并降格

形如 `assert 'multiplier"] or "1"' not in src`、`assert "(req.t_start or req.t_dec)" not in src`。

**这类不在禁止之列，且有行为测试替代不了的价值**：它防的是**已删除的错误写法被重新引入**，而重新引入处若当前不可达（如 §5.16 B14 的 `multiplier`：两个生产者都硬写 `"1"`，缺陷今日摸不到），行为测试**不会变红**，只有文本禁令会。

**裁定**：保留，但须满足两条——
1. **正名为 lint**：测试名与注释明确其为"禁止模式复现"，不得表述为行为证据；
2. **不得作为某项行为主张的唯一证据**：凡声称"某行为已正确"的断言，必须另有行为测试；文本禁令只作防回归的第二道。

### 11.3 附带确立一条审查表述规范

八审曾写"现有 `test_s26` **仅查源码**，本轮用实际调用**补足行为证据**"。该表述被 G2 归档为"补充证据（可选）"，而其真实含义是"**你那条断言无法失败，该门当前无效**"。G2 自陈这是其对同一条反馈的**第二次误读**。

**裁定**：审查方发现某断言在原理上无法失败时，**必须以"断言无法失败 / 该门无效"表述并标为阻断项**，不得写成"证据不足 / 建议补充"。两种措辞在接收端的分流完全不同：前者进必修，后者进待办。**一个无效的门与一个缺失的门同等严重，措辞不应让它看起来更轻。**


## 12. 裁定 A24：三次复发之后，停止横扫、改为使其不可表达（G0 OR-02 R41，changeLog #46）

九审（`证据完整性：完成`，`九审终裁：fail`）：**S01–S28 全部 closed 或 partial-P2，open 0**；新增 S29–S31 阻断 P1。三条**各自是已裁族的第三次或更后复发**：

| 新增 | 所属族 | 此前各次 |
|---|---|---|
| **S29** `partition_check.py:240/250/255-260` 自行实现网格、未调用 helper；完整两 bar 却首 bar `gap=True`、右界 +1µs 时 **`missing = −1`** | 一式多处实现（§5.17 B15 第 1 条根因） | validator、loader，**这是第三处** |
| **S30** `contract.py:441` 窗口下界用 `self.t_start or self.t_dec`，未对**解析后**启动时刻校验；省略 `t_start` 时 `−1µs/0` 均接受，显式同值则拒 | 一支查了另一支没查（§5.17 B15 第 2 条 / S19 根因） | `entry_ttl_s`、`fractions`，**这是第三处** |
| **S31** B17 要求的冲突门回归**无法失败**：测试在本地重实现 `group_by` 并只断言 `conflict.height>0`，从未把冲突输入送进真实门；审查方内存删除该门后测试**仍 PASS** | 无法失败的检查（§10 A22 / §11 A23） | 还原检查用 `rc`、正向委派用 `getsource`，**这是第三处** |

### 12.1 S31 单独记：G0 强制的门，其回归自身是它要防的那个形态

§5.19 B17 第 3 条明文要求"配一条**注入式回归**：构造同版本串双哈希的批量输出，断言必须抛错"。实现出来的回归**在本地重实现了判定逻辑**，因此它测的是那份副本，不是真实的门。**删掉真实门，测试照绿。**

**这说明一条规则被写进契约并不等于它被实现成有效的**。B17 同时要求的突变自证（注入原缺陷→断言必须红）**没有被应用到 B17 自己要求的这条回归上**。

**裁定**：凡契约强制新增的门，**其回归必须以"删除/停用真实门 → 回归必须红"完成突变自证**，并把注入方式与两次结果写进看板。**突变对象必须是生产代码里的真实门，不得是测试内的重实现**——测试中任何对被测逻辑的本地复刻，一律视为 A23 正向委派断言同款无效。

### 12.2 横扫已连续三次漏站点 —— 改用两条可自动失败的机制

G2 已三次执行"同族横扫"，每次都在下一轮被查出新站点（`validator`→`loader`→`partition_check`）。**这不是不认真，而是基于阅读与 grep 的横扫在结构上不足以穷尽**：漏掉的站点恰恰是没想到要找的那个。

**裁定：不再要求"再横扫一遍"，改为两条能自动变红的机制。**

1. **树级负向模式禁令**（把 §11 A23 已正名的 lint 从**单文件**扩到**全 `src/`**）：对每条已抽取的单一来源，写一条断言"除该模块外，全树不得出现其内联表达式"（如时长整除、`(x.t_start or x.t_dec)`、`timedelta(seconds=*.latency_s)`）。**站点在哪里被重新引入都会红**，而这正是行为测试在不可达区域覆盖不到的部分。
2. **调用点登记**：单一来源函数附一份允许调用方清单，并有测试断言实际调用方集合与清单相等——**新增调用方或绕过调用都变红**。

**一般原则**：**同一族缺陷第三次出现时，正确的反应不是更仔细地找，而是让它不可表达。** 前两次可以归因于疏忽，第三次说明依赖人去记得的机制已经失效。


## 13. 裁定 A25：裁定文件不得冒充审查轮次；不同性质的证据须各走各的 verify 子句（G0 OR-02 R43，changeLog #47）

G1 请 G0 把 D-10 的分类闭合判定落成 `docs/adr/review-G1-P1-**r6**.md`，并已用 `set-verify` 把 `分类` 加入 D-10 verify 的终裁前缀白名单。**G0 出具该文件，但不采用该命名，并要求改 verify 形式。**

### 13.1 命名：裁定是裁定，审查是审查

`review-G1-P1-r2..r5` 由**独立审查方**出具，`分类闭合判定`由 **G0** 出具——二者证据性质不同：前者是外部独立核验，后者是仲裁方依标准作出的判断。若把后者命名为 `r6`，任何人顺序读 `r2..r6` 都会认为这是第六轮独立审查。**文件名不得使证据来源发生误认。**

**裁定**：G0 出具的判定一律命名 `ruling-G0-<对象>-<主题>.md`，正文首段声明"本文件是 G0 的裁定，不是一轮独立审查"。本次落为 `docs/adr/ruling-G0-D10-classification-closure.md`（末行 `分类终裁：pass`）。

### 13.2 verify 形式：不同性质的证据不得共用一个通配

G1 现行 D-10 verify 以 `ls docs/adr/review-G1-P1-r*.md | sort -n | tail -1` 取最新轮次后判读终裁行。**把 `分类` 加进前缀白名单而仍用该通配，等于让一份非审查文件靠文件名进入审查轮次序列**——这正是 §13.1 要防的误认，只不过发生在机器判读侧。

**裁定**：D-10 verify 改为**显式析取**，两个子句各读各的文件：

```bash
# 子句 A：最新一轮独立审查的正向终裁
f=$(ls docs/adr/review-G1-P1-r*.md 2>/dev/null | sed -E 's/.*-r([0-9]+)\.md/\1 &/' | sort -n | tail -1 | cut -d' ' -f2)
{ [ -n "$f" ] && tail -40 "$f" | grep -Eq '^(二审|三审|四审|五审|六审|七审)?终裁[：:] *\*{0,2}pass'; } \
|| \
# 子句 B：G0 分类闭合裁定（仅此一个具名文件，不通配）
{ test -f docs/adr/ruling-G0-D10-classification-closure.md \
  && tail -5 docs/adr/ruling-G0-D10-classification-closure.md | grep -Eq '^分类终裁[：:] *\*{0,2}pass'; }
```

**一般规则**：**verify 的每个子句只读一类证据，且以具名路径而非通配指向它。** 通配的作用是"自动跟上新一轮同类证据"，把异类证据塞进同一通配，就把这个便利变成了漏洞。

### 13.3 A16 审计结论（本轮）

`verifyHistory` 累计 **5** 条。D-10 新增那条（补 `五审/六审/分类` 前缀）：前两项属**修正判读口径**（轮次名遗漏会让合法 pass 被误判 fail），**准**；`分类` 一项**不以前缀白名单方式采纳**，改由 §13.2 的独立子句承载。**收紧/口径修正 5，放宽 0。**


## 14. 裁定 A26：突变后回归仍绿时，须再做叠加突变才能判"门无效"（G0 OR-02 R48，changeLog #50）

### 14.1 S31 定论：生产门有效，验证缺陷成立（G0 独立实跑）

G0 直接调用真实门 `execution.check_policy_hash_consistency`（**未突变生产树**）：

| 输入 | 结果 |
|---|---|
| 同 `policy_version` + 两个不同 `policy_hash` | **抛 `ContractError`**；消息含版本名 ✓、真 hash ✓、伪 hash ✓ |
| 同 `policy_version` + 单一 `policy_hash` | 正常放行 ✓ |

**生产门有效**，S31 确系**验证缺陷**。§5.20 B18 第 1 节的更正就此闭合（该更正由 G0 读十审原文第 111 行作出，非采信转述；本节由 G0 直接实跑门函数作出，非采信 G2 回执）。

### 14.2 A24 判别规则补充（准 G2 所请）

§12 A24 要求"注入原缺陷 → 回归必须红"。G2 在自证中遇到一例：删掉 `assert rep.missing >= 0` 后回归**仍绿**，按二值口径应判"门无效"；G2 自觉不对，又做了一次**叠加突变**：

| 注入 | 结果 |
|---|---|
| 只注入 off-grid 缺陷 | 失败，**失败点就是该断言本身** |
| off-grid 缺陷 **且** 删掉该断言 | **仍然失败**，由测试的显式期望值抓到 |

即：该断言守护的性质（`present ⊆ 期望网格`）**已被行为测试独立钉住**，断言本身在正确实现下**不可达**。

**裁定**：突变后回归仍绿时，**不得直接判"门无效"**，须再做一次**叠加突变**——把该检查**所守护的性质本身**破坏掉，观察回归是否变红：

- **变红** → 被删的是**冗余防御**，其守护性质另有行为证据，**合格**；按 §11 A23 在测试注明"非行为证据、不可达防御"。
- **仍绿** → 才是 §10 A22 / §12 A24 所指的真正无法失败的检查，**判未闭合**。

**理由（G2 所述，G0 认同并记为规则依据）**：不加这条，A24 会**对树里每一条防御性断言产生假警报**，而**假警报的最终下场是被人关掉**——那就把 A24 反向变成了削弱防御的力量。A24 要的是"让缺陷不可表达"，不是"让防御性断言在验收里变成负担"。这与 §5.20 B18 第 2 节"例外要有证据、统一也要有证据"是同一种对称。

### 14.3 记录：独立选点是突变自证的有效性来源

G2 自跑 8 例突变，与审查方 45 次**相互独立**（不同选点、不同工具、不同选择器），其中**第 8 例为审查方 45 次所无**。**突变自证的价值不在次数，而在选点的独立性**——同一人按同一思路选点，倾向于覆盖同一批已经想到的失效模式。故 §12 A24 的自证与审查方的独立突变**互不替代**，二者都要。
