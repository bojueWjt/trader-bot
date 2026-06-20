# 窗口 C 合并与集成执行手册（C-00 / C-01 就绪版）

> 状态：**待 A/B handoff**。本手册在 A、B 完成并停止操作共享工作树后逐步执行。
> 分支：`work/integration-acceptance-v3`，base = `plan/nautilus-hermes-v3`。

## 0. 共享工作树风险（必须先解除）

拓扑（已确认，同仓多 worktree，共享 .git/refs）：A 在 `/Users/pudu/projects/trader-bot`（work/hermes-data-v3，**已停手**），B 在 `/Users/pudu/projects/trader-bot-nautilus-dashboard`（work/nautilus-dashboard-v3，**进行中**）。
**B 停手前，C 绝不在 A/B 的 worktree 里做 git 写操作。** C 合并时**另开独立 worktree**，互不干扰：
```bash
git worktree add /Users/pudu/projects/trader-bot-integration -b work/integration-acceptance-v3 plan/nautilus-hermes-v3
```

## 1. 进入合并的前置条件（全部满足才动手）

只读校验（不依赖当前 checkout 的分支）：

```bash
# A、B 都有超出 base 的提交
git rev-list --count 237cabc..work/hermes-data-v3
git rev-list --count 237cabc..work/nautilus-dashboard-v3
# A、B handoff 文档存在
git cat-file -e work/hermes-data-v3:WINDOW_A_HANDOFF.md
git cat-file -e work/nautilus-dashboard-v3:WINDOW_B_HANDOFF.md
# 工作树已静止（无并发写）：连续两次间隔采样，HEAD 与 dirty 集合不再变化
git rev-parse --abbrev-ref HEAD; git status --short
```

外加 operator 明确确认「A、B 已完成，可以合并」。

## 2. 合并顺序（PLAN C.2）

1. 从 `plan/nautilus-hermes-v3` 建 `work/integration-acceptance-v3`。
2. 落入 C 暂存物（本 staging 目录内容：release-gate.json、docs/acceptance、tests/replay schema、runbooks），首个 C 提交。
3. **合并窗口 A**（`work/hermes-data-v3`）。
4. 跑 contracts suite、db migration、API tests（A 的交付）。
5. **合并窗口 B**（`work/nautilus-dashboard-v3`）。
6. 解决 imports / schema / compose / API client 冲突。
7. **锁定 `contracts-v1`**，之后只允许兼容性修复。
8. 跑全量测试 + import/build smoke。
9. 进入 C-01..C-12。

## 3. 冲突解决协议

- `packages/contracts/**` 由窗口 A 拥有、窗口 B 只读。冲突时 **A 的契约为准**，把 B 对契约的引用对齐到 A；任何对契约的改动只能是 change request，不得静默偏离 `contracts-v1`。
- compose / infra：B 拥有 `infra/compose`、`docker-compose*.yml`；A 改的 `bridge/apps/api` 与 B 的 dashboard/adapter 在 wiring 层对接，按 control-plane API 契约对齐。
- 每个冲突在 `resolved-conflicts.md` 记录：文件、双方意图、最终取舍、理由、影响的契约/测试。
- 合并后立即跑 §2 的测试门，任一红灯则停止并记录，不带病推进。

## 4. 安全红线（合并与集成期间始终成立）

- 所有 Nautilus node 配置保持 `HALTED` / testnet，**绝不开启 live**。
- 不把旧 Freqtrade SQLite/状态灌进 Nautilus cache。
- 不引入 fixture/fake adapter 到生产构建。
- 合并不得绕过或弱化任何「全局不可妥协约束」；如发现 A/B 交付违反，记为 P0 缺陷并保持 gate=blocked。

## 5. C-00 交付物

- `MERGE_REPORT.md`（合并顺序、每步测试输出、commit 链）。
- `resolved-conflicts.md`。
- 合并后 contracts/import/build smoke 输出。

## 6. 委派边界（角色分工）

- 判断/验收/架构裁决（契约取舍、gate 评定、风险结论）由 Claude Code（窗口 C）自己做。
- 实现/批量改文件/跑测试/写 harness/migration/chaos/testnet 脚本，交给 Codex（`/codex:rescue --background`），C 验收 diff 与输出。
