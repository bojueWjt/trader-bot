# HK 切换与回滚 Runbook（C-11，草稿待 A/B 落地后定稿）

> 目标环境：`hk`（149.104.30.223 / tailscale 100.104.27.123，Debian 13）。
> 原则：PLAN C.3。**真实 live 默认关闭**；未过 release gate + operator 签名前，所有 Nautilus node 保持 `HALTED`/testnet。本 runbook 只描述流程，不在验收通过前执行真实切换。
> ⚠ 一切「真实下单」动作受 `release-gate.json` 门控；本文件不授权放开 live。

## 0. HK 现状（来自 DEPLOYMENT.md，旧系统）

旧 trader 栈在 `/srv/trader`（docker compose）：
- `trader-api-1` 127.0.0.1:8000 · `trader-frontend-1` :3000 · `trader-watcher-1` :9090
- `trader-freqtrade-dryrun-1` :18081 · `trader-postgres-1` :15432 · `trader-redis-1` :16379
- Hermes gateway：`/srv/hermes/hermes-agent`，systemd `hermes-gateway-trader`，profile `trader`，`active(running)`。
- 公网入口 host Caddy：`hk-bot.balen.wang`→dashboard/api/auth/watcher；`hk.balen.wang`→FreqUI(:18081)。
- 凭据在 `/srv/trader/.env`（不在任何包里）。

旧系统的信号路径用 `freqtrade.signal_strategy.importer --approve-parsed --refresh-window`（正则旁路自动批准）—— **v3 切换的核心就是废掉它**。

## 1. 切换前（冻结旧自动开仓）

1. **停旧自动开仓**：停掉 watcher→importer→approved 自动路径与 freqtrade-dryrun 新开仓；保留只读以便核对。
2. **全量备份并记 hash**：所有 SQLite（signal store / freqtrade db）、`/srv/trader/.env`（仅安全备份，不入库不入包）、配置、日志、媒体 → `backup-manifest.json`（每项 sha256）。
3. 确认账户层面**无挂单、无持仓**为首选切换窗口；若有，走 §5 external position adoption。

## 2. 数据迁移（C-02，幂等）

1. 旧 SQLite 历史只作 `legacy_*` 表导入 PostgreSQL；row counts + hash 对账。
2. **不**把旧 Freqtrade 内部状态转换后写入 Nautilus cache/runtime。
3. 交易所密钥 / session **不进** PostgreSQL；secret scan 必须 0 命中。
4. 迁移脚本可双跑幂等（dry-run + double-run）。

## 3. 起 v3 栈（全 HALTED/testnet）

1. control-plane（PostgreSQL 唯一查询源）、Redis（仅 cache/bus）、ingress watcher（只采集不批准）、Hermes worker（真多模态）、Decision Gateway。
2. 每账户一个 Nautilus TradingNode，独立 trader_id/Redis 前缀/凭据，**初始 `HALTED`**，testnet。
3. Dashboard 只连 control-plane API（无 FakeAdapter）。
4. readiness 必须真实校验 Redis/control-plane/adapter/reconciliation/projection。
5. Caddy 路由切到 v3 服务（保留旧路由可快速回切）。

## 4. 逐账户对账后才 ACTIVE

1. startup + continuous reconciliation 开启。
2. 逐账户用**交易所现实**对账 orders/positions/account，与 PostgreSQL 投影一致。
3. 未完成逐账户 reconciliation **不得** ACTIVE。
4. 升 ACTIVE / 放开 live 必须 `release-gate.json` 满足 + operator 签名（见 ACCEPTANCE_CRITERIA §4）；首阶段仅小额、低杠杆、限交易对，初始 HALTED 人工激活。

## 5. 无法清仓时（external position adoption）

走专门 runbook：Nautilus reconciliation 拉交易所现实 → 人工逐仓核对 → 登记为受管持仓；全程 HALTED，只允许 cancel/减仓。

## 6. 回滚（一键，fail-safe）

1. **所有节点 → HALTED**，停止一切新增风险。
2. **不恢复**旧 `importer --approve-parsed` 自动正则开仓路径（回滚不等于复活旧旁路）。
3. 需要时切 Caddy 回旧只读面板观察；交易动作只允许 cancel/close。
4. 记录 `ROLLBACK_REPORT.md`：触发原因、时间线、各节点最终状态、与交易所对账结果。

## 7. 验证（演练，测试环境）

- cutover rehearsal：§1–§4 全流程可复跑。
- rollback rehearsal：§6 可把全节点带回 HALTED 且不复活旧路径。
- 证据：命令、输出、各节点状态、对账结果、commit SHA。

## 8. 待定（A/B 落地后补）

- v3 的 compose/服务名与端口（B 的 `infra/compose`、docker-compose）。
- 账户↔chatId 绑定：Titan `-1002198013097` / Gauls `-1002228497993` → 各自 trader_id（A/B 账户配置）。
- 迁移源 schema（旧 signal store 表结构）与 A 的 `legacy_*` 目标表映射。
