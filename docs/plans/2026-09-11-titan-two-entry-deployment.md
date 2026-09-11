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

未进入 execute；未 HALT、未停止/重建节点、未替换生产执行代码、未切换生产 feeder/skill、未 RESUME、未新增或补交易订单。预检后 A–D 均仍 ACTIVE，心跳新鲜。

后续需先恢复可信的 Redis 冷备份/容量基线及实际发布身份，或单独设计并审查适用于“只更新运行时代码、不迁移 Redis”的正式部署路径。不能跳过当前门禁，也不能为凑证据直接运行 empty-volume Redis rebaseline。完整恢复材料和门禁通过后再执行此次候选版本。
