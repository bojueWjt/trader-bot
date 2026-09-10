# GOAL-1 · 数据系统 (G1 data)

> 一句话目标：把 Telegram 五频道的原始消息整理成**可数损耗、可追溯来源、无前视时钟**的 episode 表；质量底线：每条丢弃有原因码、每个数字能定位到原文或图片、删帖样本隔离不删除。

## 0. 你是谁 / 边界
- 你负责：`src/quant_lab/data/`、`tests/data/`、`data/lake/telegram/`、`data/quarantine/telegram.parquet`、`docs/adr/ADR-G1-*.md`、`docs/adr/review-G1-*.md`。
- **不碰** `market/`、`research/`、`contracts/`。你只管"消息 → episode"。
- 上下游：上游无；下游 G3 消费 `gold/episode` 决策图，G2 的 2b 消费你的历史种子品种清单。你消费 G2 的 `quant_lab.market.asof`（D-06 前用桩）。
- **先做 D-01 时把 `contracts/research-schema.md` 的字段修订通过看板 note 提交给 G0**，让 G3 按签名写桩。
- 路线依据：合并稿 A / B / C 节；分级放行 B.4；清洗 C.2 Telegram 流。

## 1. 技术栈
Python 3.12、uv、`.venv-g1`（`requirements/g1.txt`）：polars 1.44.2、pyarrow、duckdb 1.5.5、pydantic 2.13、telethon 1.45、label-studio-sdk、rapidfuzz。LLM 抽取通过 `quant_lab.data.llm` 抽象接口，测试用录制夹具；真实调用等闸门。

## 2. 交付物
- `src/quant_lab/data/{normalize,dedup,extract,validate,linker,lifecycle,audit,harvest,api}.py`
- `tests/data/fixtures/tdesktop_sample/`（合成 TDesktop 导出：≥3 频道 × ≥60 条，含相册、编辑、回复、转发、纯图、跨频道复制、五反例）
- `docs/adr/ADR-G1-episode-engine.md`（Codex）、`docs/adr/review-G1-P1.md`（Codex）
- 复用现有金标：`eval/hermes/v3-trader-signal-bench`（30 条）作解析器 bench。

## 3. 接口契约（provides）
见 `contracts/research-schema.md` §7：`load_episodes(graph_version, decision_graph=True)`、`load_episode_events`、`loss_table`、`quarantine`。表结构 §2，原因码 §4。

## 4. 任务清单（对应 taskList.json → modules.data）
| id | 交付 | 验收要点 |
|---|---|---|
| D-01 | 环境 + 骨架 + 字段修订提交 G0 | tests 可 collect，`import quant_lab.data` |
| D-02 | **[Codex ADR]** episode 引擎设计 | `docs/adr/ADR-G1-episode-engine.md` ≥150 行，含转移表与 tombstone |
| D-03 | 层 1 归一 | 夹具 → message_version 非空，三时钟齐 |
| D-04 | 层 2 去重 | pytest |
| D-05 | 层 3 抽取（parser + LLM 接口）| bench 30 条报告 recall > 0；真实 LLM gated |
| D-06 | 层 4/5 规范化 + 行情校验 | 数量级门 / 合理性带 / MARK_STALE 单测 |
| D-07 | 链接器 + 双轨状态机 + 决策图 | 五反例夹具全过，`load_episodes('fixture-v1')` ≥5 行 |
| D-08 | PoC 1a 导出接入 | gated；1a 报告字段齐 |
| D-09 | 标注与分级放行工具 | OC 表数值：200/3 在 0.5% ≈ 98.1%，0/400 ≈ 13.5% |
| D-10 | **[Codex review]** P1 | 必修闭合 |
| D-11 | PoC 1b 扩量 | gated |

## 5. Codex 参与（你自己派）
- D-02 开工前：`bash ~/.claude/skills/codex-dispatch/scripts/dispatch.sh --dir /Users/balen/projects/trader-bot --model gpt-6-astra --effort medium -- "$(cat docs/agent-team/quant-lab-codex-briefs.md 中 §1 ADR-G1 任务书)"`，用输出的 WATCH_CMD 放进 Monitor 工具监控，result.sh 取结果，核 ADR 行数与关键词后再实现。
- D-10：同样方式派 P1 review（§3 模板），必修项闭合后 `done`。
- 禁止 `--resume-last`；Codex 只读仓库 + 只写指定文件。

## 6. 协作协议
```bash
python scripts/task.py claim data D-03
python scripts/task.py report data --status in_progress --progress 0.3 --note "<进展>"
python scripts/task.py done data D-03
python scripts/task.py block data --on "契约: research-schema §2 需要加 X 列"
```
契约冻结后要改接缝：先 block，等 G0 改契约，禁止私改。

## 7. DoD
- P1：D-03..D-09 在合成夹具上 verify 全部实跑通过；`load_episodes` 决策图视图只含 `edge_available_at ≤ t_dec` 的边（有测试）；损耗表 5 层非空；Codex P1 review 必修闭合。
- P2：闸门批准后 D-08 1a 实测报告、D-11 扩量与 200/3 批验收。

## 8. 验证命令
```bash
.venv-g1/bin/python -m pytest tests/data -q
```

## 9. 铁律
零生产凭据：不读 `services/telegram-watcher` 的 session、不连生产库；只用独立研究账号与用户授权的导出。不 import `services/*`。原始层只读带哈希；隔离不删除；不填零不猜值。
