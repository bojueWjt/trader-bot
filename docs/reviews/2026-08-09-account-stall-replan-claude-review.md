# Claude Review: Account Stall Recovery Replan

日期：2026-08-09
Reviewer：Claude Opus，Claude Code 2.1.226
模式：只读，未修改项目工作树
历史结论：NO-GO；二次复核曾给出 `PASS`
当前状态：Superseded by meta-review

2026-08-09 后续元复核确认三项阻断问题：历史根因与当前残余机制混写、测试证据缺少
可复现命令与 inventory、Node/manifest 的 runtime resource 可选性漂移。当前有效结论见
`docs/reviews/2026-08-09-account-stall-replan-claude-meta-review.md`，当前只授权 Phase A。

## Findings

### P0-1：live runtime context fail-open

`services/nautilus-node/config/node_config.py:891` 的
`_requires_explicit_runtime_resources` 只有在 `environment=live` 且两个环境变量精确匹配
时才要求完整 `runtime_resources`。任一环境变量缺失，live 配置会回落到默认资源。

建议：移除环境变量猜测。live 配置始终要求完整 release-bound runtime resources。

### P0-2：Redis rebaseline 默认资源路径错误

`scripts/hk-redis-rebaseline.sh:18` 指向
`$SCRIPT_DIR/infra/systemd/account-stall-redis.conf`，实际仓库文件位于
`infra/systemd/account-stall-redis.conf`。使用默认值执行会在资源校验前失败。

建议：从经过验证的 release root 解析路径，并保留路径存在性和 hash 校验。

### P1-1：runtime resource contract 有重复 owner

`scripts/release_manifest.py` 和
`services/nautilus-node/config/node_config.py` 分别维护 resource defaults、字段和验证逻辑。
增加字段需要同步修改两个实现和多组 fixture。

建议：建立一个纯 runtime resource 验证模块作为唯一 owner，Node 与 release manifest
共同消费。

元复核补充：`scripts/make_account_stall_release.py` 还维护 host systemd/Docker
schema/constraints。三个代码级 owner 分属应用 runtime resources 与 host resources 两个
契约。Node 将部分 resource group 视为可选，manifest 将全部 group 视为必填，这个分歧是
GAP-1 与 GAP-5 的共同机制。

### P1-2：operation guard 路径不统一

Redis rebaseline 只有本地 `flock`；deploy 和 rollout 同时检查 `flock` 与 maintenance
fence；control-plane isolation 有独立的 Shell 实现。mutation 路径尚未形成一条统一、
可证明的事务链。

建议：先提取共享 Shell lock/fence helper，避免首轮增加新的 Python guard CLI。

### P2-1：rollout 测试 fixture 未同步

rollout mutation 新增 operation lock 和 maintenance fence 后，直接调用测试没有提供新
契约，形成 8 个失败。这组问题属于调用 fixture 漂移。

建议：使用统一 operation guard fixture，并保留 lock/fence 缺失、过期和 owner 漂移的
负向测试。

## 对原计划的修正

1. 首轮新建 Python guard CLI 会扩大待测接口面，应延后。
2. 一次性建立覆盖整个发布链路的 ReleaseContractV3 模块范围过大。
3. runtime resource contract 必须明确唯一 owner。
4. runtime 套件 gate 应使用零失败和无新增回归，既有 skip 需要逐项说明。
5. 当时记录为 167 个脏文件；元复核重新计数为 61 个 tracked 修改、107 个 untracked
   文件、168 个 status entries。干净 worktree 必须先于任何代码修改。

## 历史推荐顺序

以下顺序已由元复核收紧，当前只执行 Phase A：

1. 创建干净 worktree，隔离 account-stall patch。
2. 修复 live runtime fail-open，归零当前 4 个 runtime 失败。
3. 收敛 runtime resource contract owner 和 strict v3 fixture。
4. 修复 Redis 默认路径，统一 Shell lock/fence helper，升级 rollout fixture。
5. 执行 Linux flock、真实 PostgreSQL/Redis、Docker immutable build 验证。
6. 完成生产只读预检后再判定 account-a HALTED canary。
7. 所有 gate PASS 后才允许 12 USDT SOLUSDT round trip。

## 二次复核

Claude Opus 对吸收首轮意见后的计划执行了第二次只读复核，当时结论为 `PASS`。该结论
已由后续元复核 supersede。二次复核当时提出五个文档问题：

1. 删除固定 pass 数量 gate，统一使用零失败和无新增回归。
2. 根因聚类改用 `GAP-1` 到 `GAP-7`，避免与 Phase C 工作项编号冲突。
3. 明确 MessageBus host integration 和 simulated harness 的测试来源；元复核随后纠正
   harness 库与实际 importer 测试入口的计数。
4. 精确说明 Redis 默认路径从 `$SCRIPT_DIR/infra/...` 解析到不存在目录。
5. 明确 systemd projection 只消费归一化 host resource model，不重复维护字段 defaults。

元复核进一步要求 systemd projection 去重 schema/constraints，并将当前 stall 诊断、
测试 inventory 冻结和 Node/manifest parity 提升为开工前置。

## 历史 Gate

以下 gate 保留为首轮 review 记录，当前授权以 meta-review 为准：

| 阶段 | GO | NO-GO |
|---|---|---|
| Worktree | 干净、零无关改动、原子提交可回滚 | 混入 Attention/Hermes/channel 改动 |
| Runtime | 当前 4 个失败归零、无新增回归 | live 仍可使用缺字段默认值 |
| Release | 契约单一、release 套件零失败、漂移测试 fail-closed | 存在第二套 schema/defaults |
| Deployment | 默认路径可用、四类 mutation 互斥、fence 过期即停 | 任一路径绕过 lock/fence |
| Linux/Dependencies | flock、PG、Redis、Docker、rollback 全部实测 PASS | 只具备 macOS fixture 证据 |
| Production | 新鲜只读证据、digest 一致、无 deleted inode、无 P0/P1 incident | 任一证据陈旧或冲突 |
| Live trade | HALTED soak PASS、permit 有效、可精确平仓归零 | 任一安全 gate 缺失 |
