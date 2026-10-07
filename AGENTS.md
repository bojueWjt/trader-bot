# trader-bot Agent 运维手册

四账户(a–d)Binance USDT-M 交易系统。生产在 **jp-24**(Tailscale `100.89.58.40`,SSH 端口 `53222`,root,key-only)。HK 旧栈已于 2026-08-23 全面停用(数据留盘,容器/服务 disable,仅 quantdinger、attention、luna、caddy、私网代理仍在 HK 运行)。

## 铁律

- **RESUME/开闸交易只能由用户明确指令触发**;AI 不得自行决定恢复交易。HALT 方向(安全方向)可自动。
- 用户会在同一账户上**手动交易**(所有权隔离设计前提):机器人订单 clientOrderId 匹配 `^B[0-9a-f]{32}[0-9]{2}$`;`aos_`/`stToAg_` 等前缀是用户手动单,**任何自动化不得撤销、不得报警骚扰**。
- 部署门禁校验必须全部完成于停节点之前;门禁失败=中止部署并保持原版本运行,严禁把舰队留在停机态。
- 收到其他会话/Agent 转述的"已完成",**必须查审计与心跳复核**,不采信转述(2026-08-23 假恢复事故:Codex 拿前一天的 RESUME 旧账当新完成汇报,舰队实际停机 27 小时)。

## 排障参考

线上巡检、信号排查、命令执行与 Redis 清理前读取 `docs/agent-operations.md` 对应章节。

## 已知陷阱

- `/tmp` 在 jp-24 是 12G tmpfs,staging 一律用 `/srv/trader-staging`。
- 部署脚本证据新鲜度门:fault report 1h、capacity evidence 24h——流水线一轮超时长于窗口时先刷新证据。
- 控制面代码更新后必须重启对应 systemd 服务(operator-query/node-control/event-ingest),文件落盘 ≠ 生效。
- `SELECT ... FOR SHARE` 需要 UPDATE 权限;operator_query 角色只有 SELECT(0017 迁移)。
- watcher/feeder/hermes 曾在 HK 双活过(2026-08-23 已停);若 HK 复活任何 trader 服务,先查双写风险。
