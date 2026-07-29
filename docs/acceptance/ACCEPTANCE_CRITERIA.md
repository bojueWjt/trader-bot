# 窗口 C 验收判据（机器可核对）

> 来源：PLAN v3.0 第 1 节「全局不可妥协约束」、C.6「核心安全指标」、「全局放行条件」、C.10「Release gate」。
> 本文件是审计清单：每条判据 = 含义 + 由哪个 C 任务产出证据 + 通过阈值。`release-gate.json` 是其机器可读镜像。

## 0. 不可妥协约束（任何一条违反 → gate=blocked，不可协商）

1. Hermes 是唯一消息语义处理器。
2. 生产链路禁止语义正则（正则仅做 UUID/数字/枚举/精度/JSON Schema/脱敏）。
3. 禁止任何旁路自动批准（无 watcher→parser→approved、`--approve-parsed`、`--refresh-window` 等价路径）。
4. PostgreSQL 是应用层唯一查询源；Dashboard/Hermes/API 不直连 SQLite/Redis/Nautilus Cache/交易所。
5. Redis 不是业务真相，丢失后可由 PG + Nautilus 持久事件 + 交易所对账恢复。
6. 交易所是外部现实，NautilusTrader 负责对账后投影到 PG。
7. 生产禁止虚拟数据回退（无真实数据→空状态 + `stale/unavailable`）。
8. 失败关闭：任何依赖故障下禁止增加风险，只允许 cancel / 明确减仓。
9. 一个 TradingNode 一个进程，独立 trader_id/Redis 前缀/凭据。
10. 真实 live 默认关闭（C 完成前全部 sandbox/testnet 或 HALTED）。
11. 全链路可追溯：`raw_message_id → hermes_run_id → decision_id → risk_decision_id → intent_id → client_order_id → venue_order_id/trade_id`。
12. 不可伪造完成：done 必须附可复跑命令 + 输出 + 报告路径 + commit SHA。

## 1. 全局放行条件（G1–G10）

| ID | 判据 | 验证任务 | 证据形式 | 阈值 |
|----|------|----------|----------|------|
| G1 | 生产不存在 `raw message → regex parser → approved` 路径 | C-10 | `scripts/check_no_semantic_regex.py` 报告 + 全仓代码搜索 | 命中 = 0 |
| G2 | Hermes 不可用时新增风险命令 = 0 | C-07 | chaos 场景日志（Hermes timeout/5xx/invalid JSON/schema mismatch） | 新增风险订单 = 0 |
| G3 | Dashboard 生产构建无 FakeAdapter/fixture fallback/固定账户值 | C-06 | 生产 bundle 扫描报告 | 命中 = 0 |
| G4 | Dashboard/Hermes 只调 control-plane API；API 只读 PG 投影 | C-06 | 调用图 + 网络暴露报告 | 旁路调用 = 0 |
| G5 | 每节点可独立 HALTED/REDUCING 并回写 ack | C-09 | kill-switch 演练 + ack 审计导出 | 全节点 ack |
| G6 | 两账户 intent/order/position/Redis key/client order ID 不串线 | C-08 | 两账户隔离测试证据 | 错路由 = 0 |
| G7 | 重复/编辑消息、WS 重连、reconciliation 后无重复订单/成交投影 | C-05 | 幂等回放报告 | 重复 = 0 |
| G8 | 真实图文回放验收全部通过 | C-05 | REPLAY_ACCEPTANCE_REPORT + metrics.json | 全通过 |
| G9 | Binance testnet 全场景覆盖 | C-08 | TESTNET_ACCEPTANCE_REPORT + 四层证据 | 14 场景全通过 |
| G10 | 无 test token fallback / 匿名 watcher 写 / 明文交易所密钥 / 默认 live 凭据 | C-10 | SECURITY_REPORT + secret scan | 命中 = 0 |

## 2. 核心安全指标（S1–S11，C.6）

全部为零容忍 / 100%，任一不达标 → gate=blocked。

| ID | 指标 | 目标 | 验证任务 |
|----|------|------|----------|
| S1 | 非 Hermes 来源的 executable intent | 0 | C-05 |
| S2 | update/noise/analysis/ambiguous 误产生新增风险订单 | 0 | C-05 |
| S3 | 缺图/图像解析失败/上下文 stale 时新增风险订单 | 0 | C-07 |
| S4 | duplicate message/decision/intent 导致重复订单 | 0 | C-05 |
| S5 | 两账户错路由 | 0 | C-08 |
| S6 | 可执行 gold label 与 Hermes+Gateway 最终 action/instrument/side 不一致（不确定须 needs_review） | 0 | C-05 |
| S7 | 执行事件 trace 完整率 | 100% | C-05 |
| S8 | Dashboard fixture/fake 数据命中 | 0 | C-06 |
| S9 | Dashboard 对 projection 字段一致率 | 100% | C-06 |
| S10 | kill switch 对所有节点 ack 完整率 | 100% | C-09 |
| S11 | 节点失联或 projection stale 时自动新增风险 | 0 | C-07 |

## 3. 回放稳定性裁决

三次干净回放（C-05）中，Hermes 文本表达可以变化，但 `message_type / action / instrument / side / 关键交易字段` 必须稳定。
出现关键差异的样本 → 强制 `needs_review`，不计入“可执行通过”，且记入残余风险。

## 4. gate 升级规则（C.10）

- 窗口 C 默认输出上限 = `testnet_only`。
- 升级到 `live_readonly` / `live_small` 必须 operator 留痕，且 `release-gate.json.operator_signoff.preconditions_for_live_small` 全满足。
- 初始节点状态 = HALTED，人工激活。
