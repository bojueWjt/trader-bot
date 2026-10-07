# Titan 三腿明确点位入场

状态：**本地实现与测试已完成，尚未部署，未补单，生产未修复。** 2026-09-23。本文不是生产配置，也不是恢复交易或补历史单的指令。

## 事故与根因

2026-09-22 11:12 UTC，Titan 频道 `-1002198013097` 消息 `m4543`：ETH 空单，三个限价 `2877` / `2967` / `3067`，止损 `3167`。消息已收到，没有生成交易意向。

根因是决策层拒绝多于两腿，不是本次核验里看到的节点故障。`cron/output/5e8d377b8020/2026-09-22_11-15-24.md` 明确回复：m4543 因为三档暂不支持，所以没有下单。前序 `m4537` 同样被拒绝。Codex 在 2026-09-22 17:12–17:16 UTC 做了只读 SSH 核验，当时四个生产节点是 ACTIVE。这是查询当时的状态，不是 11:12 UTC 信号发生时的历史心跳，不能外推成信号当时的节点状态。当时有两处硬编码把能力缺口写成了策略限制：

- `hermes-profile/skills/trading/v3-trader/SKILL.md` 铁律 15 只接受两个明确入场位，多于两腿要求报告暂不支持，禁止截断。
- `scripts/hermes_signal_feeder.py` 的处理模板写死“两腿 / 多于两腿不支持”。

所以决策层在提交前就停了，控制面没有 entry_batch 意向，节点也没有订单。这不是漏挂之后的交易所拒单。

## 本地规则

沿用现有 `entry_batch` v1，从固定两腿扩为 2 或 3 腿。不新增计划类型，不用 zone 的 55/30/15 代替明确点位，不拆成三次 `open_position`，不把一份风险乘成三份。

- 各腿同等名义金额。参考价是 `N / sum(1/pi)`，总名义金额只调用一次现有开仓定量，再均分。
- 首腿仍是 market 或 limit。两腿时第二腿必须是 limit；三腿时后两腿必须是 limit。
- `entry.third_price` 必须同时带 `second_price`，且只允许带有效止损的 market/limit `open_position`。
- 止损必须位于每一腿的正确一侧。任一腿方向错误、价格非有限或非正、缺第二腿、zone、add、canary，都拒绝。
- 同一 ref 只持久化一次。改第三腿价格按原幂等冲突拒绝，不新开一笔。
- 明确点位不偏移。只有调用方显式带 `--entry-offset` 时才移动价格，第三腿一并移动，审计保留原值。
- 第三腿使用同一 intent 的序号 `03`。成交归属、保护数量、到期或止损后撤销未成交余腿、拒单后的已提交腿回滚、恢复与幂等都走原批次集合，不另做一套账。
- 多于三腿仍拒绝，不截断。已有仓或历史缺腿不自动补。

## 发布与回滚

滚动发布必须按这个顺序，不能倒过来：

1. 先让执行节点能接受并追踪 2 或 3 腿的 `entry_batch`。
2. 再发布会发出三腿计划的控制面。
3. 最后才切换 feeder 模板和 v3-trader skill。在这之前生产 Hermes 仍应拒绝第三腿，而不是先放开提示词去打旧节点。

回滚前先处理已经在途的三腿批次。旧的两腿节点不能接管一份尚未执行完的三腿计划：它只认识两条入场，第三腿会漏掉或被当成非法计划。两腿批次仍按原来的回滚方式处理。

## 明确没有做的事

生产事实来自 Codex 的只读 SSH 核验，没有改生产文件、没有部署、没有下单、没有 RESUME。m4543 和 m4537 仍未补单。这次本地改动不会自己生效。

## 测试

2026-09-22 17:40:26Z 到 17:40:34Z，本地 `.venv-arch`，控制面用例使用现有临时 Postgres，没有读生产：

```text
.venv-arch/bin/python -m pytest tests/execution/test_entry_batch.py tests/execution/open/test_entry_batch_strategy.py tests/control-plane/api/test_operator_entry_batch.py tests/test_v3_trade_routing.py tests/test_hermes_signal_feeder.py tests/contracts/test_contracts_v1.py tests/execution/open/test_intent_execution_strategy_shell.py -q --tb=line
```

结果：**248 passed**，54 subtests passed，1 条既有 httpx/Starlette 弃用警告，退出码 0。完整输出在 `/tmp/titan-three-entry-acceptance.log`。此前不含 shell 文件、三腿止损重载用例还未参数化时的同一组其余测试是 117 passed。本次没有无关失败。
