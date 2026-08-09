# Claude Meta-Review: Account Stall Recovery Replan

日期：2026-08-09
Reviewer：Claude Opus
模式：只读，多 agent 分域核验
结论：NO-GO；仅授权 Phase A

## 1. 决定

元复核接受原计划的分阶段 gate、stop condition 和 GAP-1/2/3/5/6、D4 技术方向，同时
确认三个开工阻断项。原 review 的二次 `PASS` 已被本结论 supersede。

## 2. 阻断项

### M1：历史根因与当前残余机制混写

历史生产 traceback 证明旧运行字节在 Nautilus actor callback 内执行同步 HTTP。当前
工作树已将 heartbeat、command、ACK、terminal 和 exchange adapter I/O 放入专用
executor/worker。当前 residual stall 机制缺少可重复证据。

吸收动作：

- 新增 GAP-0。
- Phase A 建立当前代码 fault-injection loop。
- Phase B 只处理 runtime resource 契约安全。
- 新增条件式 Phase B-S，仅在 GAP-0 复现后授权。
- 删除“stall 主路径无回归”这类缺少验证目标的 Phase B 结论。

### M2：测试证据缺少复现边界

原计划只记录 pass/fail 数字，缺少命令、依赖版本、path list、collection inventory 和
hash。2026-08-08 incident 的 `491 passed / 21 skipped` 与 2026-08-09 的
`523 passed / 4 failed / 11 skipped` 使用不同日期和测试 inventory。

吸收动作：

- 新增 `docs/evidence/2026-08-09-account-stall-test-baseline.md`。
- 固化四组精确命令、依赖版本、失败测试和 suite path list。
- 保存 538/77/72/16 项 sorted node-id inventory hash。
- Phase A 将版本化 collect-only inventory 设为硬 gate。

### M3：Runtime resource 可选性漂移

`node_config.py` 允许部分 resource group 使用默认值，`release_manifest.py` 要求所有
top-level group 显式存在。该分歧连接 GAP-1 live fail-open 与 GAP-5 strict v3 fixture
级联失败。

吸收动作：

- Phase B 行为修改前先提取共享 required/allowed field sets、defaults 和类型约束。
- 建立 Node/manifest parity matrix。
- parity 建立后移除 live 环境变量双门。
- Phase C 将归一化和 hash 收入共享纯模块。

## 3. Owner 结论

当前三个代码级 owner 分属两个契约：

| 契约 | 当前 owner/重复实现 |
|---|---|
| Application runtime resources | `node_config.py`、`release_manifest.py` |
| Host systemd/Docker resources | `make_account_stall_release.py`、`release_manifest.py`、测试 parser |

`build_immutable_node_image.py` 和 `hk-deploy-20260803.sh` 是 consumer。
`infra/systemd/*.conf` 提供数值。目标架构为两个深模块、两个清晰 interface，各自单一
owner。

## 4. 其他修正

- GAP-4 改为 dependency lock/source-manifest fixture 路径漂移。
- GAP-7 改为 guard fixture 结构漂移，要求真实 red run。
- systemd projection 的重复项改为 schema/constraints。
- `81f356a` 改述为引入持久 Redis cache/message bus 与 per-account prefix 派生的提交。
- 工作树计数更新为 61 tracked、107 untracked、168 total。
- `services/` 定义为 canonical source。
- `.live-mirror/` 定义为生产证据与 drift comparator。
- 测试使用显式 test mode 和临时 `TRADER_ROOT`。
- simulated harness 的七项测试改用三个 importer 文件计数。

## 5. 授权 Gate

当前允许：

1. 创建干净 worktree。
2. 冻结 source authority、范围、命令、依赖和 test inventory。
3. 构建当前代码 stall fault-injection loop。
4. 形成 GAP-0 诊断结论和 Phase B/Phase B-S 授权建议。

当前生产状态保持 `HALTED / NO-GO`。SSH、生产采集、Redis/PostgreSQL mutation、部署、
restart 和真实交易均保持冻结。
