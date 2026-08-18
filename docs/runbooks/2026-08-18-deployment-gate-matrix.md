# JP24 两相部署门禁总表

## 时间预算

- 部署全链最坏耗时 `W = 46,800s = 13h`。
- 自动刷新阈值 `2W = 93,600s = 26h`。
- 有效窗口短于 `26h` 的证据由 `preflight` 自动刷新或实时采样。
- `preflight` 成功只生成镜像、证据和恢复材料，A-D 容器保持运行。
- `execute` 重跑完整 `preflight`，完成在线 HALT、fence、migration 和 rollout
  一致性检查后，才将 `DOWNTIME_WINDOW_ENTERED` 设为 `1` 并调用
  `stop_recreate_nodes`。
- 停机前 fence 每次重新采集 A-D heartbeat，并原子刷新 receipt；停机后按
  receipt 内 `account_evidence_sha256` 复验 owner、lease 和 Redis fencing
  epoch，不再要求节点产生新 heartbeat。

## 门禁矩阵

| Gate | 证据来源 | 有效窗口 | 刷新方式 | 最坏流水线耗时 |
|---|---|---:|---|---:|
| Redis capacity | 不可变 `capacity-evidence.json`；实时 Docker/Redis/内存/磁盘采样写入 `capacity-refresh.json` | 24h | **preflight 第一步**运行 `refresh_redis_capacity_evidence.py refresh`，随后 `verify` | 13h |
| Staging 完整性 | `SHA256SUMS`、`bundle-manifest.json`、`release-source-manifest.json`、Git object tree | 与 release 内容同寿命 | 每次 preflight 全量 `sha256sum -c` 和 manifest 校验 | 13h |
| Release lineage | `release_commit`、bundle/source manifest、schema epochs、dependency lock | 与 release 内容同寿命 | 每次 preflight 从受审 Git object 重建并比对 | 13h |
| Redis 冷备可信度 | `cold-backup-manifest.json`、RDB/AOF hash、`redis-check-rdb`/`redis-check-aof` 输出 | 与不可变备份同寿命 | 每次 preflight 重新校验文件、hash、容器与卷身份 | 13h |
| Redis live identity | Redis `run_id`、fencing epoch、DBSIZE、CONFIG/INFO；Docker container/volume/cgroup inspect | 实时采样 | capacity 刷新和 Redis live validator 每次自动执行 | 13h |
| 磁盘储备 | `statvfs` 对 staging、`/srv/trader-v3`、Docker root 的可用字节 | 实时采样 | 每次 preflight 自动采样；三处均要求至少 8 GiB | 13h |
| Host memory reserve | `/proc/meminfo`、Redis cgroup/maxmemory、live risk resource contract | 实时采样 | capacity 刷新自动重算增长、容器、其他服务和系统 reserve | 13h |
| Binance egress | A-D 网络命名空间到 Binance API 的 HTTPS 响应和出口 IPv4 | 单次 preflight | 每次 preflight 自动探测，每个请求 `--max-time 15` | 13h |
| 四通道映射 | watcher SQLite、账号/频道映射、watcher runtime manifest | 单次 preflight | 每次 preflight 自动重读并验证，变更时准备 watcher 重启 | 13h |
| Control-plane topology | systemd units、role env、隔离状态、owner/mode、DB role check | 单次 preflight | 每次 preflight 自动发现和校验 | 13h |
| A-D heartbeat | `node_heartbeats.last_seen_at`、status、release/config/runtime generation、lease token、sequence | 15s | 节点持续自动上报；preflight/execute 每次查询最新行 | 13h |
| Rollout identity | `reviewed_release_rollouts`、`redis_fencing_epochs`、live release manifest | 当前事务快照 | 每次 preflight 自动查询；execute 在停机边界前完成注册和一致性复核 | 13h |
| Live config capture | A-D JSON、容器环境、risk policy、生成的 config artifact hash | 本次 release | 每次 preflight 自动捕获并绑定 release manifest | 13h |
| Immutable image build | content-addressed base、bundle、lock、生成的 image ID | 与镜像 digest 同寿命 | preflight 自动构建或复用已验证镜像 | 13h |
| Build attestation | `immutable-build-attestation.json`、bundle/source/lock hash、image labels/digest | 与 attested image 同寿命 | image build 自动生成；每次 preflight 重新验证 | 13h |
| Reviewer trust | `reviewer-trust-proof.json` 绑定 source commit、attestation hash、review subject | 与 attestation 同寿命 | reviewer 对新 attestation 签署一次；后续 preflight 自动验证 | 13h |
| Image readiness | `docker image inspect $TARGET_IMAGE`、attestation labels | 单次 preflight | 每次 preflight 自动 inspect；必须在停机前可用 | 13h |
| Account-B exchange snapshot | 签名 evidence 引用的 `exchange_snapshot` 内容和 hash | 1h | account-B preflight 自动调用受信 `refresh-account-b-evidence` hook | 13h |
| Account-B PostgreSQL snapshot | heartbeat writer identity、HALTED、reconciliation、incident 状态 | 1h | 同一 hook 自动执行 `REFRESH_EVIDENCE` 并重建签名证据 | 13h |
| Account-B fault report | fault scenarios、PASS、HALTED、release/image/config identity | 1h | 同一 hook 在 900s timeout 内自动重跑并重签 | 13h |
| Account-B top-level evidence | evidence `issued_at`、四类报告 hash、reviewer signature | 1h | 同一 hook 原子替换 evidence 和 signature | 13h |
| Testnet emergency close | signed live-trade report 内的 testnet open/close/flat proof | 24h | 同一 account-B hook 自动刷新；过期或刷新失败直接阻断 | 13h |
| Prior closure evidence | 前一账号 live closure report、signature、pinned reviewer public key | 与对应 release/phase 同寿命 | 每次 preflight 复制到临时只读文件并验签 | 13h |
| Backup readiness | PostgreSQL custom dump、restore list、SHA256、旧镜像/容器 inspect、旧文件索引 | 本次 deploy run | 每次 preflight 自动生成；`pg_dump`/`pg_restore --list` 各受 300s timeout 约束 | 13h |
| Recovery readiness | post-migration release payload、recreate scripts、container env、expectation hash | 本次 deploy run | 每次 preflight 自动生成并验证 | 13h |
| Online HALT | 控制面 ACK、A-D HALTED 状态、heartbeat sequence 持续推进 | 15s | execute 自动发送/验证；失败时容器继续运行 | 13h |
| Maintenance fence | operation lock、DB fence row、owner token、A-D HALTED heartbeat、receipt canonical `account_evidence_sha256`、active Redis fencing epoch | lease 120s 自动续租；停机前 heartbeat 15s；停机后冻结证据 | execute 在停机前在线 acquire/verify 并原子刷新 receipt；停机后主脚本、isolation、recovery、rollback 均以 `verify-frozen` 复验 owner/lease/hash/epoch | 13h |
| Migration/schema | SQL hash、`schema_migrations`、required epochs、事务提交 marker | 当前事务快照 | execute 在停机前自动 apply/verify；幂等迁移复用已提交结果 | 13h |
| Stop boundary | `DOWNTIME_WINDOW_ENTERED=0/1`、fence verification mode、Docker mutation trace | 本次 deploy process | 仅全部前置 gate PASS 后置 `1`，同时把 inherited fence 切换为 frozen；此前 ERR 只释放 fence 并退出 | 13h |
| Recreate/ready | recreate plan、resource evidence、`/ready`、version endpoint、`verify-live` | 启动后实时 | execute 在停机窗口内自动执行和验证 | 13h |
| Gate-failure acceptance | 六类注入 `capacity/disk/trust/image/fence/rollout`；A-D heartbeat sequence；Docker event/mutation 计数 | 每个 release | CI 自动跑 fail-closed harness；上线前对目标 release 再跑一次 | 13h |

## 执行入口

```bash
bash hk-deploy-20260803.sh preflight
bash hk-deploy-20260803.sh execute
```

失败注入仅用于验收：

```bash
DEPLOY_ALLOW_GATE_FAILURE_INJECTION=1 \
DEPLOY_INJECT_GATE_FAILURE=capacity \
bash hk-deploy-20260803.sh preflight
```

验收条件：注入任一道 gate 失败时，A-D heartbeat sequence 持续增加，Docker
`stop`、`rm`、`run`、`create` 计数为零。
