# GOAL-2 · 行情与执行内核 (G2 market)

> 一句话目标：建一个**分区可体检、三时钟可证**的行情湖，和一个 **order_plan → 规范执行事件**的永续执行内核（候选 A 自研参考实现 + 候选 B Nautilus SimulationModule 对拍）；质量底线：最小 episode 期望全过、不变量全过、同输入重放哈希一致。

## 0. 你是谁 / 边界
- 你负责：`src/quant_lab/market/`、`tests/market/`、`data/lake/market/`、`data/quarantine/market.parquet`、`docs/adr/ADR-G2-*.md`、`docs/adr/report-G2-*.md`、`docs/adr/review-G2-*.md`。
- **不碰** `data/`（G1 的 Telegram 层）、`research/`、`contracts/`。
- 上下游：你是最上游之一。**M-01 之内把 `contracts/execution-interface.md` 的 as-of 签名与 ExecutionRequest/Result 字段修订提交 G0**，G1 的行情校验层与 G3 的评估器都等你的签名写桩。2b 消费 G1 的种子品种清单。
- 路线依据：合并稿 C.1 三时钟、C.2 行情流、D.4 执行接口；选型报告 Q3 / Q4（Nautilus 1.227.0 源码事实：无 `process_mark_price`、`engine.rs:1018-1023` 路由绕过 MarkPriceUpdate、`is_stop_matched` 为普通止损、无默认 funding 结算、`modules.pyx:42 SimulationModule` 是候选 B 入口）。

## 1. 技术栈
Python 3.12、`.venv-g2`（`requirements/g2.txt`）：polars、duckdb、pydantic、nautilus_trader==1.227.0、httpx。数据源 data.binance.vision 免费归档（klines / markPriceKlines / indexPriceKlines / premiumIndexKlines / fundingRate / metrics）。

## 2. 交付物
- `src/quant_lab/market/{vision,partition_check,asof,contract,kernel_a,nautilus_adapter,execution}.py`
- `tests/market/fixtures/episodes/*.json`（≥10 手工最小 episode 期望，契约 §4 清单）
- `docs/adr/ADR-G2-execution-kernel.md`（Codex）、`docs/adr/report-G2-kernel-AB.md`、`docs/adr/review-G2-P1.md`（Codex）

## 3. 接口契约（provides）
见 `contracts/execution-interface.md`：§1 分区与 manifest、§2 `asof_join / last_closed_bar / mark_price_at`、§3 `simulate / simulate_batch` 与 ExecutionRequest/Result、不变量。

## 4. 任务清单（对应 taskList.json → modules.market）
| id | 交付 | 验收要点 |
|---|---|---|
| M-01 | 环境 + 骨架 + 签名定稿提交 G0 | tests 可 collect，nautilus 可 import |
| M-02 | **[Codex ADR]** 执行内核设计 | ≥200 行，含 SimulationModule 方案与 `is_stop_matched` 语义 |
| M-03 | PoC 2a 下载器 + manifest（真实网络冒烟 BTCUSDT 2024-01 1m markPrice ≥40000 行）| manifest 行数断言 |
| M-04 | 分区体检 + quarantine | 注入缺口/重复/OHLC 违规/尖刺 → 对应原因码 |
| M-05 | 三时钟 as-of 库 | 等号/晚到/同秒/未收盘/MARK_STALE 性质测试 |
| M-06 | 执行合同 schema + 不变量 + ≥10 夹具 | 夹具数断言 |
| M-07 | 候选 A 参考实现 v0 | 夹具全过、不变量全过、trace_hash 重放一致 |
| M-08 | 候选 B spike + A/B 报告 | 报告含逐 episode 差异与解释码 |
| M-09 | 执行接口输出 + 与 G3 冒烟 | pytest |
| M-10 | **[Codex review]** P1 | 必修闭合 |
| M-11 | PoC 2b 扩品种 | gated 于 D-11 |

## 5. Codex 参与（你自己派）
- M-02 开工前派 ADR-G2（任务书 `docs/agent-team/quant-lab-codex-briefs.md` §1）；ADR 必须回答：候选 A 的挂单队列/部分成交/撤改单/组合订单/账户约束怎么实现、mark 触发与 last 撮合的判定顺序、funding 账务时点、同 bar 路径情景的三档定义、不变量清单、最小 episode 集规格、Nautilus 适配器审计范围与候选 B 的 SimulationModule 钩子。
- M-10 派 P1 review（§3）。禁止 `--resume-last`。

## 6. 协作协议
```bash
python scripts/task.py claim market M-03
python scripts/task.py report market --status in_progress --progress 0.5 --note "<进展>"
python scripts/task.py done market M-03
python scripts/task.py block market --on "契约: execution-interface §3 需要加 X"
```

## 7. DoD
- P1：M-03..M-09 verify 实跑通过；候选 A 在 ≥10 最小 episode 与不变量上全过；A/B 报告出具（定型留到 P2）；Codex P1 review 必修闭合。
- P2：真实 44 品种 2024-01 起分区体检通过（每分区缺口有归因或隔离）；内核定型写入 `execution_contract_version`。

## 8. 验证命令
```bash
.venv-g2/bin/python -m pytest tests/market -q
```

## 9. 铁律
零生产凭据：不连交易所私有 API、不读 `services/nautilus-node` 的账户配置；只用公开归档。不 import `services/*`。尖刺只标不删；不填零；bronze 只读带哈希。
