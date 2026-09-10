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
1. 只用 `python scripts/task.py` 改看板，不手改 JSON。
2. 只动自己模块的 `modules.<module>` 子树；`contracts` / `integration` 只有 G0 能写。
3. 完成一个任务或状态变化立即上报。被卡 → `block`；对方就绪 → 自己 `unblock`。
4. **自报 done 不算完成**：G0 以实跑 `verify` 命令且产出非空为准。

```bash
python scripts/task.py show
python scripts/task.py claim data D-03
python scripts/task.py report data --status in_progress --progress 0.4 --note "..."
python scripts/task.py done data D-03
python scripts/task.py block research --on "契约: feature-snapshot §3 缺 weight 列"
```

## 3. 环境仲裁（防多窗口互相拆台）

1. `.python-version` 钉 3.12；`requirements/*.txt` 钉版本；改版本走 G0 契约变更。
2. **每窗口隔离 venv**：`.venv-g1` / `.venv-g2` / `.venv-g3` / `.venv-g0`，绝不共用 `.venv`，绝不重建别人的 venv。
3. 镜像与重试在 `uv.toml`；安装失败换镜像重试，不换环境。
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

## 6. 用户决策闸门（合并稿 H.2，未拍板前对应任务保持 `todo` 并标 gated）

| 闸门 | 影响的任务 |
|---|---|
| Telegram 独立研究账号 / TDesktop 导出授权与频道范围 | D-08（1a 导出）、D-11（1b 扩量） |
| 模型 API 预算与云上传范围 | D-05 真实 LLM 调用、D-11 |
| 一般错误 200/3 放行门的误收风险接受 | D-09 放行工具可先做，真实批次放行等拍板 |
| 数据保留 / 撤回合同（合并稿 H.1） | 任何真实聊天记录进入 `data/` 之前 |

合成数据（fixtures）上的全部工作不受闸门限制，P0 与 P1 全部在合成数据上完成。

## 7. 铁律（AGENTS.md，任何窗口不得违反）

RESUME/开闸只凭用户明示；不碰、不撤销、不针对用户手工单告警；研究层零生产凭据；不改生产；不 import `services/*`。
