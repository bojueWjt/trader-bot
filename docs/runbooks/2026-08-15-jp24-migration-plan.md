# jp-24 迁移计划（trader-bot 四账户栈 HK → jp-24）

状态：**计划稿，未执行**。执行方：Codex（按阶段派发）。验收方：Claude Code review。
日期：2026-08-15。

## 背景与目标

2026-08-13 部署事故的三层根因（币安单 IP 权重配额超 53%、node-control 连接池每节点 1 条、Redis 512MiB 与 4×256MiB stream 合同冲突）中，有两层受限于 HK 机器只有 8GB 内存和单一出口 IP。新购 jp-24（24GB，3 个 IPv4 + IPv6 /64，日本机房，离币安撮合最近）同时解决内存墙和出口配额两个结构性问题。

- 目标 1：jp-24 收编进机群（SSH 加固、Tailscale、balen-deloy inventory 同步、三台 Mac 的 codex/claude code 更新）。
- 目标 2：四账户交易栈整体迁移到 jp-24，携带四项代码缺陷修复，全程 HALTED，HK 保留为回滚冷备。
- 目标 3：四账户按出口 IP 分流（2+1+1），单 IP 权重回到 2400 限额内。

## 硬性禁止事项（每个阶段都适用）

1. **全程 HALTED。不发 RESUME，不下任何单，不平任何仓。** 放行交易永远是用户的决定。
2. **明文密码不落盘。** jp-24 初始 root 密码已由用户在会话中提供，仅用于首次登录装公钥；不得写入 inventory.md、任何 repo、任何日志。公钥认证确认后提醒用户在服务商面板轮换该密码。
3. **币安 API key 与 IP 白名单由用户在币安后台操作。** Codex 不接触任何密钥明文。
4. **HK 数据只复制不删除。** 不动 HK Redis 的四个全局安全键（`trader-bot:redis-fencing-epoch`、`redis-namespaces:active`、`:active:fencing`、`:active:leases`），不跑 `/srv/trader-v3/redis_purge_dead_instances.sh`。
5. Redis 数据**不迁移**：jp-24 用全新空卷 + 新 fencing epoch 做 exchange-first rebaseline；HK 旧卷冷备保留。
6. 每阶段完成后先给验收读数，不自动进入下一阶段。

## jp-24 机器信息（用户提供，待 P0 实测核对）

- 主 IPv4：170.205.39.79（Primary）；附加 IPv4：170.205.39.82、170.205.39.88
- IPv6：2406:ef80:4:eccf::/64
- 预期规格：24GB RAM（hostname `jp-24` 暗示，**必须 `free -g` 实测**，若非 ~24G 立即停止并上报）

### 出口 IP 分配方案

| 账户 | 出口 IP | 该 IP 权重预算（3s 证据刷新 ≈ 920/min/节点） |
|---|---|---|
| account-a | 170.205.39.79 | 920 / 2400 |
| account-b | 170.205.39.82 | 920 / 2400 |
| account-c | 170.205.39.88 | 1840 / 2400（与 d 共享） |
| account-d | 170.205.39.88 | 同上 |

实现方式：每账户独立 docker network + iptables SNAT 按网段绑定源 IP（P1 落地，P4 从容器内 `curl api.ipify.org` 实证核对）。IPv6 作为未来第 4 出口的备选实验项，不作为本次依赖。

---

## P0 — jp-24 收编 + 机群同步（可立即派发，与交易栈无耦合）

1. 首次登录（密码）→ 安装用户公钥 → 验证 key 登录 → `PasswordAuthentication no`、`PermitRootLogin prohibit-password`；SSH 端口按机群惯例（jp-max 用 53222，jp-24 建议同规格非标端口）。改 sshd 前先开好第二个会话防锁死。
2. 装 Tailscale 并加入 tailnet，记录 Tailscale IP；此后管理一律走 Tailscale（机群规则）。
3. 硬件核对：`free -g`（≈24G）、`df -h`、`nproc`、`ip -4 addr`（三个 IPv4 都在）、`ip -6 addr`。
4. 基线：时区 UTC、chrony/ntp 同步（币安签名对时钟敏感，偏移须 <1s）、docker + compose 安装、基础防火墙（默认拒入，放行 Tailscale + SSH 端口 + 后续 Caddy 8080 仅限 Tailscale）。
5. 三 IP 出口实测：`curl --interface <每个IP> https://api.ipify.org` 逐一确认回显一致；`curl -w '%{time_total}' https://fapi.binance.com/fapi/v1/ping` 逐 IP 测延迟。
6. balen-deloy inventory 同步：在 `references/inventory.md` 的 Topology 表、VPS Services 表、Network Model 增加 jp-24 条目（角色：trader v3 四账户主机 + 多出口 IP；含三个 IPv4、Tailscale IP、SSH 端口）。**不写密码**。
7. 三台 Mac（pudu-main、home-mini、pudu-mini，走 Tailscale/ssh 别名）同步：
   - `~/.claude/skills/balen-deloy` 与 `~/.codex/skills/balen-deloy` 两份更新为同一内容（先 diff 确认无本地散改，有散改先上报）；
   - 升级 CLI：`npm i -g @anthropic-ai/claude-code@latest @openai/codex@latest`（本机实测为 npm 全局安装；其他两台先探测安装方式，brew 装的用 brew 升）。
   - pudu-mini 注意有 `balen` 和 `pudu` 两个账户，逐账户检查哪个在用 skill/CLI。

**P0 验收**：key 登录可用且密码登录被拒的 `sshd -T` 证据；`free -g` 读数；三 IP 各自的 ipify 回显与 fapi 延迟；inventory diff；三台 Mac 上 `claude --version`/`codex --version` 新版本号 + 两份 skill 的 sha256 一致。

**P0 之后用户动作**：在服务商面板轮换 root 密码；在币安后台为四个账户的 API key 追加三个新 IP 白名单（保留 HK/JP 旧 IP 直到 P5 收尾）。

## P1 — jp-24 环境准备

1. PostgreSQL 16（本机部署，`max_connections≥100`）、Redis（**maxmemory 1536MiB**，匹配 4×256MiB stream 合同 + 头寸；noeviction 保留）、Caddy 控制面路由（8080，仅 Tailscale/loopback）。
2. 三个控制面角色（node-control / event-ingest / operator-query）systemd 单元与 DB 角色，沿用列级锁授权方案。
3. 环境变量修正（事故修复项）：`CONTROL_PLANE_NODE_CONTROL_DB_POOL_SIZE=24`、`CONTROL_PLANE_NODE_CONTROL_DB_CHECKOUT_TIMEOUT_SECONDS=2`。
4. 部署 staging 目录用真实磁盘 `/srv/trader-staging`，**禁用 /tmp（若为 tmpfs）作 staging**——HK 的 tmpfs 教训。
5. 四账户出口分流落地（docker network + SNAT），按上表映射。
6. systemd 资源合同沿用 `MemoryMax=640M`/节点。

**P1 验收**：三角色启动自检（`_ROLLBACK_ONLY_PERMISSION_PROBES`）全过；Redis `maxmemory` 读数；每个账户网络的容器内出口 IP 实测与映射表一致；staging 目录挂载点非 tmpfs（`df -T` 证据）。

## P2 — 新 release 构建（本地仓库，含四缺陷修复）

1. 在 `codex/four-account-immutable-release-20260812` 分支确认/补齐四项缺陷修复：writer bootstrap 竞态、证据采集 singleflight + 连接复用、durable I/O 1s 死线、projection 关停预算；外加容量准入。
2. 跑既有门禁：`uv run --no-project --with pytest --with 'pydantic>=2.7,<3' --with jsonschema --with fastapi --with httpx --with psycopg2-binary python -m pytest -q tests/deployment`（基线 631 passed / 2 skipped）+ 涉改模块测试。
3. `make_account_stall_release.py` 打包 → 上传 jp-24 → 两跑 reviewer trust proof 仪式（沿用 2026-08-14 轮换后的信任根 `2b149fe2...`，签名人 balen，按既有授权自动签）。

**P2 验收**：测试全绿输出；bundle sha256；runA 证据链 + `TRADER_RELEASE_REVIEWER_TRUST_SHA256` 固定值。

## P3 — 数据迁移（HK → jp-24，只复制）

1. PG：HK `pg_dump` → jp-24 restore → **读回校验冻结系数**（a=1.26492097、b=1.52017779、c=2.96902319、d=3.0）与关键表行数比对。
2. rsync 证据与信任材料：`/srv/trader-v3/account-a-canary/{reviewer,evidence,releases}`、`/srv/trader-v3/backups/`（含 pre-multiplier 备份与 hotpatch 台账）。
3. Redis：不迁移数据（见禁止事项 5）。HK 旧 Redis 卷做冷备快照存档。
4. bridge/watcher 栈（telegram-watcher 等 `/srv/trader` compose）随迁：先迁数据库与配置，服务在 P4 部署完成后再起，避免双活。

**P3 验收**：dump/restore 行数与系数读回证据；rsync `--checksum` 无差异输出；HK 侧无任何删除操作的记录。

## P4 — 部署与验收（jp-24，SKIP_RESUME=1）

1. 用 P2 attested bundle 走 `hk-deploy-20260803.sh` 全流程（fail-closed 门禁不减一条），`SKIP_RESUME=1` 保持 HALTED。
2. 六项门禁读数汇报（四节点 ready+HALTED、startup 内存峰值 vs 640MiB、rollout phase 注册、心跳围栏等）。
3. 逐容器实测出口 IP 与分配表一致。
4. 重装 per-account recorder（补 c/d 录制盲区）+ exchange-state recorder 重启。
5. **24 小时 HALTED 浸泡**：全程无 -1003、无 503、无重启、Redis 内存平稳、`X-MBX-USED-WEIGHT-1M` 每 IP 峰值 <50%。这是对三层根因修复的直接回归验证。

**P4 验收**：六项读数 + 浸泡期监控数据（权重、503 计数、restart 计数、Redis used_memory 曲线）。

## P5 — 切换与收尾

1. HK 四节点维持 stopped/`restart=no`（现状），HK trader 栈标记 deprecated，**保留 7 天回滚窗口**后由用户决定是否退役。
2. inventory 更新：jp-24 服务表补全，HK trader 条目标注状态。
3. 用户决策点（不在本计划内执行）：币安白名单收敛（移除旧 IP）、HK 缩配或退租、canary 放行（RESUME → A 账户 SOLUSDT ≤12U 往返）。

## 遗留债务（不阻塞迁移，迁移后处理）

F1–F3 回滚守卫；旧 immutable 镜像 GC；容量证据 rebaseline 的 24h 窗口机制改良；三套测试的 CI；本地 brew postgresql@16 修复；canary 适配器 `lease_fencing_token` 固定化设计缺陷（需要契约层决策，不许在部署中顺手改围栏语义）。

---

## Codex 派发模板（P0 首发）

```
目标：完成 jp-24 收编与机群同步（本文档 P0 全部 7 步）。
范围：jp-24 远端系统配置；本机及 home-mini、pudu-mini 的 ~/.claude/skills/balen-deloy、~/.codex/skills/balen-deloy、npm 全局 claude/codex 包；本 repo 不改代码。
约束：见本文档"硬性禁止事项"6 条，特别是密码不落盘、改 sshd 前保持第二会话、inventory 不写任何凭据；只做 P0，不进入 P1。
验收：见"P0 验收"清单，每项给命令输出证据。
验证命令：sshd -T 双向核验 / free -g / curl --interface 三 IP / diff + sha256 skill 双份 / 三台 Mac 的版本号。
输出要求：changed files、每项验收证据、remaining risks（含未完成项）。
```

后续 P1–P5 逐阶段派发，每阶段完成经 Claude Code review 后再放行下一阶段。
