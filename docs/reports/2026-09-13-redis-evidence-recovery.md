# JP-24 Redis 部署证据恢复

2026-09-13 12:46 UTC（20:46 UTC+8），恢复原始证据并重新采集在线容量报告。未停止、重启 Redis 或账户节点，未执行 RESUME；四账户采样均 ACTIVE。

## 恢复与验证

- 原目录：`/srv/trader-v3/redis-rebaseline/20260816T1104Z`；`current` 已重新指向该目录。
- `capacity-evidence.json` 从 home-main 留存的部署工具输出中恢复，SHA256 为 `0a0ea4f032640171106137ec9956a677e3201cd7c932152ed35a7abc07ba9e6c`，与生产数据库当前 fencing epoch 登记完全一致。
- `cold-backup-manifest.json` SHA256 为 `87738f9a8a4b4f1dd827e65a8aabd2d3009bd5b33b72678bef1ac79d8fc4e900`，与容量原件引用完全一致。
- RDB 从保留的旧卷 `trader-v3-redis-hardening-20260816T103229Z` 恢复；11,048,472 字节，SHA256 为 `d540c2bbf58640efc6e91c8c77ac8550b22567a66e707ba6bb88cda6026a3b59`，与清单一致。
- 使用原 Redis 镜像、禁网只读容器重新运行 `redis-check-rdb`，退出码 0；报告 SHA256 为 `b25070afefdaab62c68d89e7d76a8f96e9b5f87bab24346d0813ce7290c97a71`，与历史报告一致。
- 新生成 `capacity-refresh.json`，身份、epoch、卷、内存、持久化、主机和磁盘余量检查全部通过；有效期至 **2026-09-14 12:46:01 UTC**。
- 单独执行部署脚本原有 `REDIS_EVIDENCE_V3_VALIDATOR`，退出码 0。未执行完整部署脚本。

来源：home-main 会话 `01a0015f-1ad1-7550-a6c0-f3c89770c56c`，工具调用 `call_rUS3meGmTsIK0Bb5rH0E24gj`。只将哈希与生产数据库及原始清单匹配的内容发布；未修改历史时间戳、数据库哈希或历史检查结论。

生产目录保留 `recovery-attestation.json` 和 `recovery-gate-check.json`；异机副本保存在工作机 `/Users/balen/.local/share/trader-bot/ops-evidence/20260913-redis-recovery/20260816T1104Z`。

## 尚未通过的部署条件

`trader-v3-redis-legacy-20260816T1104Z` 旧容器已被删除。部署脚本还要求此容器存在、处于停止状态且容器 ID 与历史清单相同；因此**证据恢复成功不等于完整部署门禁通过**。原卷及冷备份数据仍完整。

未创建伪装成原容器的替代对象，未跳过此门禁，未部署本地冻结修复。后续需为旧容器已删除、原卷及原始校验链完整的场景实现可审计的恢复验收；不能改写原历史容器 ID。
