# Titan 双入场部署记录

2026-09-11，用户授权“提交部署并更新交易计划”。

## 已完成

- `b401f73`：双入场执行、等名义金额定量、保护和恢复修复。
- `24f9307`：补齐正式部署脚本的 entry_batch 宿主安装、备份、校验和节点挂载声明。原发布包已有模块，但安装脚本未消费，预检发现后补齐；5 项挂载/部署契约测试通过。
- `codex/titan-two-entry-release` 分支的 `f5d89bf`：归并已核验的线上停机告警、权益历史、outcomes 与 trace API，并将其依赖加入发布清单及安装流程。避免当前主工作分支覆盖已有线上功能。
- 发布分支相关回归：457 passed、2 skipped、67 subtests passed；API 临时 PostgreSQL 集成测试 12 passed。生产 API 合并前后的模块均通过编译检查。
- 干净、固定提交的完整发布包已生成，上传至 jp-24 `/srv/trader-staging/titan-f5d89bf`，SHA256SUMS 校验通过。
- 交易计划保存于 `hermes-profile/plans/titan-two-entry.md`，状态为待部署生效。

## 正式 preflight 结果：未通过

运行 `LC_ALL=C bash hk-deploy-20260803.sh preflight`，退出码 1。生产日志：`/srv/trader-staging/titan-f5d89bf/preflight.log`。

关键输出：

```text
capacity refresh receipt is invalid: /srv/trader-v3/redis-rebaseline/current/capacity-refresh.json
Redis capacity evidence is invalid
FATAL: deploy gate mode detection failed
!! preflight gate FAILED — A-D runtime remains untouched.
```

jp-24 `/srv`、`/root`、`/var/lib` 搜索未发现可用的 `capacity-evidence.json` 或 `cold-backup-manifest.json`；仅有旧 staging 的 acceptance-capacity.json。脚本的正式容量证据还要求与冷备份哈希、Redis 身份及历史 stopped-node 证明一致，不能把实时采样伪装成丢失的迁移证据。

另已核实：宿主 `RELEASE_MANIFEST.json` 为 `42db5f151823...`，A–D 实际心跳/镜像标签为 `8ea2e67e8bbc...`，两者不一致。新候选不可直接使用旧总清单作回滚身份凭据。A–D 的执行器、inbox 和 recovery 文件哈希彼此一致；旧 recovery 挂载对应当前仓库代码，没有丢弃。

## 生产状态与后续

未进入 execute；本任务未发送 HALT、未停止/重建节点、未替换生产执行代码、未切换生产 feeder/skill、未 RESUME、未新增或补交易订单。preflight 刚结束时 A–D 均 ACTIVE，心跳新鲜。

最终复核出现额外运行事件：D 在旧镜像上出现交易所查询超时、控制面心跳超时及自动重启，随后短暂因旧 Redis 租约 held 循环重启。租约老化后进程自动启动成功，fencing token 为 1319；ready=true、reconciliation=healthy、心跳恢复，但 trading_state=HALTED，halt_reason=resume_command_rejected。A–C 保持 ACTIVE。未发现本任务新增 operator_command；最后一条仍为前一天。已单独请求用户明确授权核验后恢复 D，未发送 RESUME。不能把本次恢复的进程健康写成 D 已恢复交易，也未断言超时与预检负载无关。

生产计划文档已同步至 `/srv/hermes/profiles/trader/plans/titan-two-entry.md`，本地与线上 SHA256 一致：`37814b8c6469dd5024ad5a28be834305fba24042074305250a517fd7c08439d0`；状态明确为待部署生效。发布分支已推送到 origin。

后续需先恢复可信的 Redis 冷备份/容量基线及实际发布身份，或单独设计并审查适用于“只更新运行时代码、不迁移 Redis”的正式部署路径。不能跳过当前门禁，也不能为凑证据直接运行 empty-volume Redis rebaseline。完整恢复材料和门禁通过后再执行此次候选版本。

## 12:39 UTC 续查：D 已恢复，历史第二腿仍未补挂

用户明确回复 `resume` 后，针对 D 发出 REFRESH_EVIDENCE 和 RESUME。审计 request_id 为 `user-chat-20260911T123450Z-31fe46f5-resume-d`，command_id 为 `cb369da5-ad79-465d-903f-437ede4a7e3d`；operator_command 审计时间为 2026-09-11 12:34:54 UTC。随后复查 A–D 均 ACTIVE，D 心跳年龄 0.5 秒。此操作恢复原运行版本，没有完成双入场部署。

用户另要求检查并挂上 Titan 漏掉的第二腿，属于本次历史补挂授权。12:38:41 UTC 交易所镜像中 B 普通挂单为空：

| 原信号 | 首腿原成交 | 当前持仓 | 原第二入场 | 原止损 |
| --- | ---: | ---: | ---: | ---: |
| JTO m4488 | 2814 | 1407 | 0.3957 | 0.3834 |
| BTC m4487 | 0.103 | 0.052 | 75578 | 74557 |
| ICP m4486 | 429 | 429 | 2.662 | 2.58 |
| ETH m4476 | 1.357 | 1.357 | 2425 | 2356.5 |

首腿来源已通过机器人 clientOrderId 与 intent 对应。BTC/JTO 当前数量约减半，但本次 execution_events 查询没有对应减仓事件，不能据此断言减仓操作者。ICP/ETH 仍占用原完整首腿额度，直接增加等金额第二腿会增加原计划风险。TAO 已止盈并移损到 217.9，不能当作尚未执行的新双入场计划补挂；无仓位且已更新或了结的旧信号同样未重开。

线上 read_api.py 的 `_OPERATOR_ACTIONS` 不包含 `add_position`；旧 `open_position` 持仓准入仍存在，而新双入场候选只处理新完整计划，不提供历史首腿转换。因此本次没有提交任何补单，也没有绕过执行器直接调用交易所下单。后续需要先解决正式部署门禁，并提供含剩余预算、历史首腿归属和合并保护的补挂路径；ICP/ETH 若需先减首腿，应另外明确该调整，不擅自平仓。
