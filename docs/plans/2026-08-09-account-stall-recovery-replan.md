# Account Stall 修复重新复盘与收敛计划

日期：2026-08-09
状态：Claude 元复核与 reviewer 发现已吸收；生产运行 `3f3cbe8` 并保持 HALTED；
本地放宽补丁已完成 executor、adapter 和 deployment 聚焦验证，reviewer
`P0=0 P1=0`，等待生成新 commit-bound 部署
生产结论：account-a 保持 HALTED；真实交易 permit 在 `C-GATE=done`、`C-DEPLOY=done`
且 deployed commit/hash 复核完成后生效，目标为一次
`0.07 SOLUSDT LIMIT + IOC` 小额往返

## 1. 决策

当前工作保持收敛模式。Claude 元复核提出的三个必修项已经进入现行决策：

1. 历史同步 I/O stall 与当前投影恢复回归使用两条独立因果链。
2. 测试结论绑定可复跑命令、选择范围和明确的 pass/skip 数字。
3. runtime resource 契约按三个 owner、两个 consumer 和可选性漂移建模，后续提取共享
   required/allowed/default 定义。

当前生产可用性回归的直接根因已经定位为：`34d41e7` 引入 async durable ingress 后，
wrapper 将 mapper 不支持的订阅事件统一解释为 fatal `IGNORED`。节点启动恢复既有保护单时
收到 `OrderInitialized`，该事件按 mapper 契约应被过滤，却触发 account-wide HALT 与
`/ready` 503。修复把 durable ingress 结果拆分为：

- `FILTERED`：记录降级并继续，后续成功 durable flush 清除降级。
- `HALTED`：durable core 已停止，保持 sticky fatal。
- durable write、queue capacity、deadline 和 fsync 失败：保持 hard fail-closed。

该回归与 2026-07 历史 account stall、Redis namespace 变更分别记录，避免互相替代因果。

后续交付面保持四类：

1. Node stall 核心运行时修复。
2. Redis fencing、容量和恢复安全。
3. 不可变 release、部署互斥和 maintenance fence。
4. 可复跑验证证据与生产 gate。

Attention、Hermes 通知、channel strategy、普通业务功能和非 account-stall
重构退出本轮交付范围。现有相关改动保持原状，不回滚、不覆盖。

## 2. 当前证据

基线 HEAD 为 `7368641e98c410b3c6edbe4ab3ea8f72cf5efe1d`，分支为
`codex/account-stall-hardening`。测试结果摘要采样时有 168 个 directory-collapsed status
entries；后续 source freeze 有 169 个，其中 61 个 tracked 修改、108 个 untracked
directory entries。`--untracked-files=all` 展开后是 373 个 status entries。该工作树已混入
其他任务。

2026-08-09 当前收敛分支的可复跑测试选择范围和结果如下；初始冻结清单见
`docs/evidence/2026-08-09-account-stall-test-baseline.md`：

| 验证面 | 结果 | 判定 |
|---|---:|---|
| `tests/execution tests/nautilus` | 422 passed, 11 skipped, 2 subtests passed | PASS |
| runtime/projection/http reviewer 聚焦 | 73 passed | PASS |
| filtered-event 调度竞态独立进程循环 | 50/50 passed | PASS |
| `tests/deployment` | 387 passed | PASS |
| `tests/control-plane/api` | 86 passed | PASS |
| 当前 live executor + adapter | 320 passed | PASS |
| recorder / deployment bundle 聚焦 | 45 passed | PASS |
| heartbeat persistence 聚焦 | 14 passed | PASS |
| reviewer red-suite + projection mapper 聚焦 | 284 passed | PASS |
| Python compile + `git diff --check` | PASS | PASS |
| 双 Codex reviewer | `P0=0 P1=0 P2=0` / `P0=0 P1=0 P2=0` | PASS |

`recorder / deployment bundle 聚焦` 的 `45 passed` 对应以下六文件选择范围：
`tests/control-plane/tools/test_exchange_state_recorder.py`、
`tests/deployment/test_verify_exchange_state_recorder.py`、
`tests/deployment/test_hk_deploy_account_stall_availability.py`、
`tests/deployment/test_make_container_bundle.py`、
`tests/deployment/test_node_patch_mount_contract.py` 和
`tests/deployment/test_hk_gen_recreate_patched.py`。

2026-08-08 incident 中的 `491 passed, 21 skipped` 与上述数字对应不同日期、不同工作树和
不同测试 inventory。2026-08-08 没有保存 collect-only 清单，两个结果只能分别证明各自
时点的选择范围，不能计算回归差值。

根因事实按历史生产字节与当前工作树分开：

| 事实 | 结论 |
|---|---|
| 最早同类 stall | 2026-07-02 14:18:15 UTC |
| 首次明确 poller freeze | 2026-07-07 |
| Redis 变更 lineage | `81f356a` 于 2026-06-19 21:08:21 UTC 引入持久 Redis cache/message bus 与 per-account prefix 派生 |
| 历史生产直接根因 | 旧生产运行字节在 Nautilus actor callback 中执行同步 HTTP/外部 I/O，生产 traceback 已证实 |
| 当前工作树状态 | heartbeat、command、ACK、terminal 等外部 I/O 已进入专用 executor/worker |
| 当前 cleanup/fencing 缺陷 | shared-session terminal executor 泄漏和 durable worker `stop(False)` 传播缺口已有确定性 red/green 验证；它们与生产 account stall 的直接因果仍待 A7 审查 |
| 历史放大因素 | 随机 Redis namespace、无界 streams、内存/swap/AOF/I/O 压力 |
| 历史发布因素 | bind-mounted hotpatch、deleted inode、A/B 运行字节漂移 |
| 当前生产可用性回归 | `OrderInitialized` 被错误提升为 durable fatal；与 Redis lineage 无直接因果 |
| 当前生产状态 | 2026-08-09 23:04:11 UTC 部署 `3f3cbe8`；account-a 保持 HALTED，live permit ledger 不存在 |
| 新鲜 exchange filter | 2026-08-09 20:39 UTC 活跃 Redis generation 的 `SOLUSDT` instrument 为 `min_notional=5`、tick/step=`0.01`、min quantity=`0.01`；`0.07 SOL` 在当前价格下满足 |
| 冲突 filter 处置 | 公网 `exchangeInfo` 同时返回 `minNotional=50`、`minPrice=556.8` 和约 `77.24` 的市场价格，证据内部冲突；gate 采用节点刚启动加载的活跃 instrument cache |

上述历史基线继续保留为 2026-08-09 复盘证据。当前分支的 pass 数字只证明本次
projection/canary 变更选择面。

### 2.1 真实交易门禁分层

为了避免保护逻辑再次阻塞恢复验证，canary 使用以下分层：

| 类型 | 条件 | 行为 |
|---|---|---|
| Soft | readiness、heartbeat、projection、reconciliation stale/false | 签名告警，继续 |
| Soft | HTTP timeout、5xx、circuit open、普通 queue/resource pressure | bounded retry 或降级继续 |
| Soft | 新鲜 exchange preflight 已证明目标归零后，`/v1/nodes` timeout、普通 5xx 或 snapshot 缺失 | 保留 exchange authority，记录 warning，继续 OPEN |
| Soft | before-open exchange snapshot 仍在 freshness 窗口内，但时间早于 RESUME ACK | 记录 `BEFORE_OPEN_CAUSAL_FRESHNESS_RELAXED`，继续校验目标仓位、两类目标订单和已知余额充足性后 OPEN |
| Soft | heartbeat、execution-event、loss-monitor 纯遥测发布普通永久 4xx；本地 durable spool 完整 | 有界降级，不终止进程 |
| Soft | emergency-close 只有确定性 `contract_replay`，缺少新鲜 testnet execution | 明确记录 `EMERGENCY_CLOSE_CONTRACT_REPLAY_ONLY`，继续 |
| Soft | `risk_healthy` 缺失/false；actor/loss progress 陈旧；ownership/fencing/durability、writer/lease、余额、filter 遥测缺失 | 记录精确 warning，继续 |
| Soft | `exchange_authoritative` 标记缺失/false，同时 exchange source/freshness 有效 | 记录 `PREFLIGHT_EXCHANGE_AUTHORITY_UNCONFIRMED`，继续 |
| Hard | account/symbol/release/writer/lease/fencing identity | 阻断 |
| Hard | 节点或 loss-monitor 发布 401/403/409、identity/fencing conflict | 阻断 |
| Hard | `process_liveness=false`、`loss_monitor_healthy=false` | 阻断 |
| Hard | quantity 固定 `0.07`、notional、已知 exchange filter、已知余额不足、single-use permit、loss cap | 阻断 |
| Hard | `OPEN` 前 freshness 窗口内的 exchange preflight | 缺失时停止新增风险；快照早于 RESUME ACK 时降级继续 |
| Hard | durable journal/订单副作用身份/执行结果唯一性 | 阻断或进入恢复 |
| Hard | 目标最终平仓、目标订单归零、最终 HALT | 阻断最终 PASS |
| Hard completion | open/close 唯一成交集合数量守恒、逐 fill commission、成交价、方向和 signed PnL 完整 | 缺失时交易仍完成平仓与 HALT，最终结果为 BLOCKED |

非目标组合基线保留为签名审计快照。持仓只记录 `symbol`、`position_side`、
`position_amt`、`entry_price`、`leverage`、`margin_type`、`isolated_margin` 和
`is_auto_add_margin`；普通挂单和 algo 挂单完整记录。该快照发生变化时执行器输出
`NON_TARGET_PORTFOLIO_DRIFT`、签名哈希、实时哈希和发生阶段，交易继续执行。硬门聚焦
account-a 的 `SOLUSDT` 目标仓位、目标订单、唯一 permit、名义金额、累计损失、精确
reduce-only close 和最终 HALT。

交易执行顺序固定为：

1. OPEN 终态、异常或结果不明确。
2. 首次 HALT，关闭新增风险窗口。
3. 撤单、查询目标仓位、按本轮实际成交量和授权上限执行 capped exact close。
4. 证明目标仓位和目标订单归零。
5. 再次幂等 HALT，将 durable ledger 收口到 HALTED。
6. 获取 post-HALT 新鲜交易所快照。
7. 使用最终成交集合认证实际名义金额、手续费和净 PnL。

close ACK 丢失时只重放首次 close identity 和 quantity，累计实际 close effect 保持在
本轮授权量内。post-HALT 目标归零证明失败时，evidence 保持未提交，permit ledger 保持
recoverable。后续恢复成功时，只允许同 permit、同 authorization、旧结果为 BLOCKED 的
evidence 通过原子替换完成终结。

## 3. 为什么持续返工

### 3.1 范围失控

原始目标是修复 account-a stall。实现过程同时引入控制面角色隔离、release
attestation、SBOM、maintenance fence、A/B rollout、live permit 和真实交易执行器。
这些能力分别合理，组合后形成了一个跨 Node、Redis、PostgreSQL、systemd、Docker 和
交易所的发布平台改造。

### 3.2 缺少集成边界

团队协议要求 Executor 使用独立 worktree 和原子提交。实际实现集中在一个已有大量改动的
工作树中，运行时修复、发布工具、测试 fixture 和其他业务改动缺少可独立回滚的 commit
边界。

### 3.3 契约存在多个真相源

当前存在三个代码级 contract owner，分属两个契约：

- 应用 runtime resources：`services/nautilus-node/config/node_config.py` 与
  `scripts/release_manifest.py` 分别定义字段、默认值和校验。
- host systemd/Docker resources：`scripts/make_account_stall_release.py` 与
  `scripts/release_manifest.py` 分别定义 schema/constraints。
- `tests/deployment/test_account_stall_systemd_resources.py` 继续重复字段集合和 parser。

`scripts/build_immutable_node_image.py` 与 `scripts/hk-deploy-20260803.sh` 属于 consumer；
`infra/systemd/*.conf` 只提供具体数值。当前最危险的漂移是
`node_config.py` 允许部分 resource group 使用默认值，`release_manifest.py` 要求全部
group 显式存在。字段增加或可选性变化后，遗漏会生成数十个级联失败。

### 3.4 安全上下文依赖环境变量

live runtime 是否强制完整资源配置，当前由 manifest schema 和 release purpose
环境变量共同决定。调用方、测试和节点启动缺少一个显式、类型化的 release context。
相同 live 配置在不同进程环境中可能得到不同验证结论。

### 3.5 Shell 函数依赖隐式全局状态

operation lock、maintenance fence、路径和 systemd topology 通过大量 Shell 全局变量传递。
函数级测试抽取单个函数后出现未绑定 UID/GID、缺 helper 和路径漂移，说明接口过浅且依赖
集合不可见。

### 3.6 测试基线没有随协议冻结

strict v3、完整 runtime resources、reviewed payload lock、migration exact-set 和
maintenance fence 进入实现后，旧 fixture 继续构造 v2/部分资源对象。当前大量失败属于
协议升级后的 fixture 漂移；少量失败属于真实生产路径缺陷。

## 4. 剩余失败的真实聚类

当前各聚焦套件中的失败按根因收敛为以下八组：

| ID | 类型 | 根因 | 影响 |
|---|---|---|---|
| GAP-0 | 诊断与因果缺口 | 当前代码已复现两个 shutdown/fencing 缺陷；生产 stall 因果、actor tick/progress freeze 与完整 cleanup order 仍待版本化证据和 A7 审查 | 允许修复已证实的 cleanup/fencing 缺陷；禁止宣称生产 stall 主路径已闭环 |
| GAP-1 | 产品/契约缺陷 | Node 使用环境变量推导 strict live context，并与 manifest 存在 resource group 必填/可选分歧 | 4 个 runtime 失败；可能绕过 release-bound 容量限制或形成反向契约漂移 |
| GAP-2 | 产品缺陷 | Redis rebaseline 的 `$SCRIPT_DIR/infra/...` 默认值解析到不存在的 `scripts/infra/...` | Redis rebaseline 大量级联失败；真实脚本无法读取仓库根目录的资源契约 |
| GAP-3 | 契约缺陷 | systemd parser、consumer type 和实际 drop-in 应用方式未统一 | release builder 在生成 source manifest 前失败 |
| GAP-4 | Fixture 路径漂移 | dependency lock 与 release-source-manifest fixture 路径未对齐 immutable build 读取契约 | 非生产放置缺陷；immutable build 测试提前失败 |
| GAP-5 | Fixture 漂移 | release/rollout node config 仍只提供 Redis 和 journal 两组资源 | strict v3 manifest 与 rollout 测试级联失败 |
| GAP-6 | 接口断点 | rollout mutation 新增 operation lock 和 maintenance fence，直接调用测试未提供 | 8 个 control-plane rollout 失败 |
| GAP-7 | Harness 结构漂移 | deployment Shell fixture 缺真实 migration runner、真实 `flock` 与 fence env，假二进制可能掩盖失败 | 需要一次真实 red run 后才能确定级联范围 |

先由 A7 对 GAP-0 的机制证据和因果边界给出结论，再按 GAP-1 到 GAP-7 逐组归零。禁止按
单个失败逐条打补丁。

## 5. 目标架构调整

### 5.0 可用性优先的故障分级

本轮 runtime 修复以“软故障继续交易，硬一致性故障停止新增风险”为统一契约：

| 分类 | 故障 | ACTIVE 行为 |
|---|---|---|
| Soft | HTTP poll/heartbeat/ACK timeout、5xx、普通 operation timeout、circuit open | 记录 degraded，继续开仓 admission、订单管理和保护动作 |
| Soft | 控制面内存 queue pressure/full | 暂停或合并新输入，按容量分批 drain，继续交易 |
| Soft | exchange refresh/cancel 可恢复错误、external result mailbox 压力 | bounded retry，继续保护与交易 |
| Hard | 显式 HALT；带结构化 fence code 的 lease/owner/fencing HTTP 409 | sticky HALTED 或 fatal termination |
| Hard | durable write/fsync/deadline、durable task/result/inbox/outbox 满载 | sticky HALTED 或 fatal termination |
| Hard | session stopped、actor tick/continuation progress freeze、writer/release identity conflict | sticky HALTED 或 fatal termination |

生产部署、Redis/PostgreSQL mutation 与真实交易继续受 Phase E-G gate 约束。故障分级放宽
运行时 availability，发布门禁继续验证 release identity、持久化与交易后归零证据。

### 5.1 明确契约所有权

本轮不新建覆盖整个发布链路的 `ReleaseContractV3` 大模块。先消除已经造成失败的重复
定义和可选性分歧：

| 契约 | 当前定义位置 | 目标 owner 与消费方 |
|---|---|---|
| Application runtime resources 字段、必填集合、允许集合、默认值和类型 | `node_config.py`、`release_manifest.py` | 在 `packages/` 下建立可独立导入的纯模块；Node 与 manifest 共同消费 |
| Release envelope、source inventory、attestation、SBOM、migration exact-set | `scripts/release_manifest.py` | builder、rollout、deploy verifier |
| Host systemd-to-Docker schema、constraints 和 projection | `make_account_stall_release.py`、`release_manifest.py`、测试 parser | builder 暴露归一化 host resource model；manifest 与测试共同消费 |

Phase B 行为修改前先提取共享 required/allowed field sets，并建立 Node/manifest
parity matrix。`release_manifest.py` 已有 manifest-local normalization/hash；Node 当前
没有该能力。拟新增的是双方共同消费的 normalization/hash owner。测试通过统一 fixture
factory 生成合法 strict v3 物料，再对单一字段做负向变异。禁止在测试文件内继续手写一套
runtime resource defaults。

systemd projection 只消费 systemd parser 生成的归一化 host resource model，不重复声明
字段 schema 或 constraints。应用 runtime resources 和 host systemd resources 保持两个
清晰契约，各自只有一个 owner。

### 5.2 Explicit Runtime Context

共享字段集合与 Node/manifest parity 建立后，移除当前基于环境变量猜测 strict 模式的
逻辑。`binance.environment=live` 始终要求完整 runtime resources，testnet/sandbox 保留
兼容默认值。emergency rollback 必须携带已经物化的完整 live 配置并保持 HALTED，不能
依赖缺字段默认值。

只有后续出现两个真实调用模式且无法由该 invariant 表达时，才引入类型化
`release_context` 参数。本轮禁止提前扩大接口。

### 5.3 Operation Guard

先将本地 `flock` 和 PostgreSQL maintenance fence 的 Shell 实现提取为一个共享 helper：

```text
acquire -> verify(stage) -> mutate -> renew/verify -> release
```

deploy、Redis rebaseline、control-plane isolation 和 janitor source 同一个 helper；
rollout 继续使用现有 Python lock/fence 校验并验证继承的 FD/token。共享 helper 稳定后，
再根据重复度决定是否需要 Python guard CLI，本轮不预先增加该接口。

### 5.4 Deterministic Payload Root

全部发布工具接收一个显式 `release_root`。资源文件、migration runner、lock、SBOM 和
manifest 路径从 validated source manifest 解析。禁止使用 `SCRIPT_DIR` 或当前目录猜测
仓库布局。

### 5.5 Source Authority 与测试根目录

`services/` 是应用源码的 canonical source。`.live-mirror/` 是历史生产运行字节的证据副本
和 drift comparator；它只用于取证与差异验证，任何迁移都需要独立范围和 review。

测试使用显式 test mode 与临时 `TRADER_ROOT`。`hk-deploy-20260803.sh` 的
`T=/srv/trader-v3` 当前是硬编码生产根。Phase A 必须明确让完整脚本支持受控
`TRADER_ROOT` override，或将完整脚本排除出本地执行范围；测试 fixture 不读取或写入共享
生产根目录。

## 6. 执行计划

### Phase A：隔离和冻结

目标：建立可审查、可回滚、可复现的工作面，并用当前代码重新诊断 stall。

| 项目 | 动作 | 验收 |
|---|---|---|
| A1 | 从 `7368641` 创建干净 worktree/分支 | 新 worktree `git status` 为空 |
| A2 | 建立 source authority 与 in-scope 文件清单 | `services/` 标记为 canonical；`.live-mirror/` 标记为生产证据/drift comparator；每个文件标记所属域 |
| A3 | 固定测试环境、命令、inventory、hash、replay package 与现有契约差异矩阵 | 保存依赖制品及 hash、继承环境清单、四组 path list、collect/run 原始输出、exit code、JUnit、warning/skip 报告、node-id SHA-256、Node/manifest 必填差异 |
| A4 | 隔离测试根目录 | 显式 test mode；完整部署脚本支持受控 `TRADER_ROOT` override 或退出本地执行范围；测试不接触共享 `/srv/trader-v3` |
| A5 | 构建当前代码 stall fault-injection loop | 阻塞 HTTP、饱和 executor/queue、并发 shutdown 与 terminal worker；独立观测 actor tick 和各 lane progress |
| A6 | 对同时包含其他任务的文件做 hunk 审计 | Attention/Hermes/channel 变更不进入 account-stall patch |
| A7 | 按模块导入现有实现 | 每个模块形成独立 commit，禁止一次导入全部工作树 |

当前 stall repro 的 red 判据是：故障注入期间 actor tick age 超过 2 秒，或 heartbeat、
command poll、ACK、intent 任一 progress clock 在 worker 可恢复后持续冻结。测试必须直接
驱动当前 executor/worker 接线。只验证函数返回或线程存活不满足该判据。

退出条件：

1. 干净分支包含可解释的原子提交，当前混合工作树保持不变。
2. 四组测试 inventory、hash、命令、依赖制品、继承环境、原始输出、exit code、JUnit、
   warning/skip 报告和失败映射已保存。
3. 当前 stall 机制得到可重复 red 证据，或明确记录为“当前残余机制未证实”。
4. Reviewer 对 GAP-0 给出结论后，才授权 Phase B 或条件式 Phase B-S。

### Phase B：Runtime Resource 契约安全

目标：完成 GAP-1。该阶段只处理 runtime resource fail-closed 和契约 parity。

| 项目 | 动作 | 验收 |
|---|---|---|
| B1 | 提取共享 runtime resource required/allowed sets、defaults 和类型约束 | Node 与 manifest import 同一份定义 |
| B2 | 建立 Node/manifest parity matrix | 完整、缺字段、多字段、类型错误、testnet default 对同一输入结论一致 |
| B3 | 移除 live 环境变量双门 | 任意 live 配置缺任一资源字段都 fail-closed |
| B4 | 保留 testnet defaults | testnet/sandbox 兼容测试 PASS |
| B5 | emergency rollback 使用完整物化配置 | HALTED rollback 也要求完整字段 |
| B6 | 复跑 execution/Nautilus | 当前 4 个 runtime 失败归零，无新增失败，skip 逐项说明来源 |
| B7 | 复跑真实 Nautilus 入口 | projection host integration 1 项和三个 importer 文件中的 simulated engine 7 项 PASS |

真实入口为：

- `tests/nautilus/projection/test_projection_hk.py::ProjectionHostNautilusTests::test_projection_actor_receives_real_nautilus_on_event_callbacks`
- `tests/execution/open/test_intent_execution_strategy_hk.py` 中 2 项
- `tests/execution/manage/test_intent_execution_strategy_manage_hk.py` 中 3 项
- `tests/nautilus/risk/test_nautilus_integration_hk.py` 中 2 项

`tests/nautilus_simulated_harness.py` 是共享 harness 库，不作为独立测试入口计数。

退出条件：Node/manifest parity matrix PASS，当前 4 个 runtime 失败归零，HALT 和恢复语义
保持通过。本阶段不声明当前 stall 主路径已经修复。

### Phase B-S：条件式 Stall 修复

授权条件：Phase A 通过当前代码复现 GAP-0，并给出最小 red-capable loop。

| 项目 | 动作 | 验收 |
|---|---|---|
| B-S1 | 对已证实机制实施最小修复 | 修改范围与 Phase A repro 的 load-bearing 路径一致 |
| B-S2 | 将最小 repro 固化为回归测试 | 修复前 RED，修复后 GREEN |
| B-S3 | 执行队列饱和、blocked I/O、shutdown 和恢复压力循环 | actor tick 与各 lane progress 满足阈值 |
| B-S4 | 复跑 execution/Nautilus 与真实入口 | 无新增失败，HALT、恢复和终态语义保持通过 |

退出条件：原始 fault-injection loop 与最小回归测试均 PASS。Phase A 未复现当前机制时，
Phase B-S 保持未授权。

### Phase C：契约所有权和 release fixture 收敛

目标：完成 GAP-3、GAP-4、GAP-5，深化 Phase B 的共享 runtime resource 模块并消除
重复 fixture。

| 项目 | 动作 | 验收 |
|---|---|---|
| C1 | 定义 canonical fixture factory | 所有 release/rollout 测试共享合法 strict v3 base fixture |
| C2 | 统一 systemd resource parser、schema/constraints 和 consumer enum | 四个资源文件均可生成确定性 Docker/systemd contract |
| C3 | 将归一化与 hash 收入共享 runtime resource 模块 | Node 与 manifest 对同一输入产生同一归一化结果和 hash |
| C4 | 绑定 reviewed payload root | lock、migration、SBOM、source manifest 和 checksums 必须来自同一 release root；source inventory 显式拒绝 `.live-mirror/**` |
| C5 | 纳入 migration `0012_control_plane_maintenance_fence` | migration exact-set 和 up/down hash PASS |
| C6 | 完成 build attestation 和 reviewer trust proof | 任一 source/image/lock/migration/SBOM 漂移或 `.live-mirror/**` 注入均 fail-closed |
| C7 | 复跑 release 聚焦套件 | 0 failed，测试清单无永久 skip |

退出条件：release builder 可生成完整 payload；strict v3 verifier 对合法物料 PASS，对每类
漂移 FAIL。

### Phase D：Operation Guard 和部署闭环

目标：完成 GAP-2、GAP-6、GAP-7。

| 项目 | 动作 | 验收 |
|---|---|---|
| D1 | 修复 Redis resource path，改为 release-root 解析 | rebaseline 正常和故障注入测试 PASS |
| D2 | rollout API 统一 operation guard fixture | register/advance/status 语义 PASS |
| D3 | 先用真实 `flock`、migration runner 和 fence env 建立 GAP-7 red run，再修 fixture | topology rollback、secret/role/resource 校验 PASS |
| D4 | 四个 Shell mutation 路径 source 同一 guard helper | deploy/rebaseline/isolation/janitor/rollout 不可并发 |
| D5 | 每个不可逆阶段前重新 verify fence | lease 过期、owner/token 漂移立即停止 |
| D6 | 复跑 deployment/control-plane | 聚焦套件全部 PASS；shell fail-closed PASS |

退出条件：本地锁、DB fence、rollout phase 和 Redis epoch 形成一条可审计事务链。

### Phase E：真实依赖与 Linux 发布验证

目标：证明 macOS fixture 之外的部署行为。

| 项目 | 动作 | 验收 |
|---|---|---|
| E1 | Linux 执行真实 `flock` 继承测试 | deploy、rollout、janitor、rebaseline 互斥 PASS |
| E2 | PostgreSQL 16.14 全迁移和恢复 | migration、backup hash、restore/list validation PASS |
| E3 | Redis 8.10.0 replacement/restart/janitor | fencing、容量、retention、重启恢复 PASS |
| E4 | Docker `--network=none` immutable build | image digest、labels、SBOM、attestation PASS |
| E5 | 容器内 import smoke test | release exact-set 全部模块可导入 |

退出条件：产生一个可部署、按 digest 固定、通过独立 reviewer 的 release artifact。

### Phase F：生产只读预检

目标：重新获得 2026-08-09 之后的新鲜生产事实。

| 项目 | 动作 | 验收 |
|---|---|---|
| F1 | 只读采集 A/B 容器、heartbeat、tick、mountinfo 和 `/version` | 证据带绝对时间和哈希 |
| F2 | 只读采集 Redis、内存、swap、AOF 和 keyspace | 容量计划使用新鲜数据 |
| F3 | 只读采集 PostgreSQL migration、writer 和 incidents | 两账户状态可证明 |
| F4 | 生成 GO/NO-GO 报告 | 任一证据陈旧或冲突即 NO-GO |

退出条件：Reviewer 和 Evidence Auditor 对同一 release digest 签署 PASS。

### Phase G：HALTED canary 和真实小额交易

目标：在所有前置门通过后完成 account-a 单次 `0.07 SOLUSDT LIMIT + IOC` round trip，
实际名义金额不超过 12 USDT。

| 项目 | 动作 | 验收 |
|---|---|---|
| G1 | 部署 account-a immutable digest，保持 HALTED | 无 deleted inode，release identity 完整 |
| G2 | HALTED soak 和故障注入 | 显式 process/loss unhealthy、identity conflict、durable failure 可硬停；遥测缺失可降级 |
| G3 | emergency close contract | 新鲜 testnet execution 或签名 `contract_replay` 覆盖 `LIMIT + IOC` 与 reduce-only close |
| G4 | 单次 permit、restricted RESUME 和 round trip | quantity=`0.07`，名义金额不超过 12 USDT，净亏损低于 1.5 USDT |
| G5 | 精确平仓、HALT、目标归零、组合活动审计 | SOLUSDT position/orders 为零，非目标组合差异完整记录 |
| G6 | account-b rollout | account-a 证据签名后才允许进入 fleet complete |

退出条件：四层证据齐全，A/B 运行同一 digest，目标账户恢复期望 HALTED/ACTIVE 状态。

## 7. Agent 分工

| 角色 | 写入范围 | 禁止事项 |
|---|---|---|
| Runtime Executor | Node config、runtime、projection、execution 对应测试 | release/deployment 文件 |
| Release Executor | canonical release contract、builder、manifest、release tests | Node 交易策略 |
| Deployment Executor | operation guard、deploy/isolation/rebaseline、deployment tests | release schema自行扩展 |
| Reviewer | 只读 diff、接口、状态机、回滚和测试审查 | 直接修改或合并 |
| Evidence Auditor | 只读核对命令、版本、digest、测试和生产证据 | 生产 mutation |
| Planner/Integrator | 拆分任务、集成已 review commit、维护 gate | 同时展开多个相互依赖 Phase |

同一文件只有一个写入 owner。每个 Executor 完成一个 Phase 后停止，等待 Reviewer
结论。禁止再次在共享脏工作树内并行写同一文件。

## 8. 每阶段强制证据

每个 Phase 必须提供：

1. 目标 commit 和允许修改的文件列表。
2. 精确依赖版本、测试 path list、collect-only inventory 和 SHA-256。
3. 一个已经执行过的 red-capable 反馈命令。
4. PASS 数量、skip 数量和 skip 原因。
5. 负向故障注入结果。
6. 回滚面和残余风险。
7. Reviewer 的 P0/P1 findings 为零。

测试数量只能证明对应测试面。macOS fixture 结果不能替代 Linux、Docker、Redis、
PostgreSQL 或生产证据。

## 9. 停止条件

以下条件阻断 release-wide rollout：

- 工作树重新混入无关功能。
- GAP-0 缺少当前代码的可重复诊断结论。
- 测试结果缺命令、依赖制品 hash、继承环境、inventory、原始输出、exit code、JUnit 或
  warning/skip 报告。
- canonical contract 出现第二套字段定义。
- Node 与 manifest 对 resource group 的必填/可选结论不同。
- live hardening 仍依赖环境变量猜测 release context。
- operation guard 任一 mutation 路径可绕过。
- release 或 deployment 聚焦测试存在失败。
- Linux immutable build、真实 Redis/PostgreSQL 或 rollback 验证缺失。
- 生产事实超过 gate 规定的新鲜度。
- account-a 或 account-b 存在与 ownership、fencing、durability、目标仓位或 loss monitor
  直接相关的开放 P0/P1 incident。

受限 account-a canary 使用独立 gate：无关历史 incident、软遥测缺失和普通 transport
degradation 进入签名告警；明确 identity 冲突、durable 失败、已知资金/filter 违规、
process/loss unhealthy、交易结果歧义和最终安全证明失败继续阻断。

## 10. Phase A 历史执行批次

第一批只执行 Phase A：

1. 创建干净 worktree。
2. 冻结 source authority、文件范围、临时测试根和四组测试 inventory。
3. 固化环境、依赖制品 hash、命令、collection hash、原始输出、exit code、JUnit、
   warning/skip 报告、失败清单与 `491 -> 523` 的证据边界。
4. 对当前 executor/worker 接线建立 stall fault-injection loop。
5. 对 GAP-0 给出“已复现机制”或“当前残余机制未证实”的 Reviewer 结论。
6. 形成独立 commit 和 Phase B/Phase B-S 授权建议。

该段记录 2026-08-09 Phase A 启动时的授权边界。当前执行边界由顶部状态、2.1 节和
Phase G 更新。

## 11. Claude Review 状态

Claude Opus 于 2026-08-09 使用只读方式核验计划、事故记录、关键实现和测试。首轮 review
及其二次 `PASS` 记录见
`docs/reviews/2026-08-09-account-stall-replan-claude-review.md`。

后续元复核发现 M1 根因时态混淆、M2 测试证据不可复现、M3 runtime resource
可选性漂移三项阻断问题，因此二次 `PASS` 已被 supersede。吸收后的结论与授权范围见
`docs/reviews/2026-08-09-account-stall-replan-claude-meta-review.md`。

当前采纳结论：

1. 历史生产同步 callback I/O 与当前 residual stall 机制分开记录。
2. GAP-0、测试 inventory 和契约差异矩阵属于 Phase A 硬 gate。
3. Phase B 只处理 runtime resource fail-closed 与 Node/manifest parity。
4. Phase B-S 只在当前代码复现 stall 后授权。
5. Runtime resources 与 host resources 保持两个单一 owner 契约。
6. rollout 的 8 个失败按调用 fixture 漂移处理。
7. 首轮使用共享 Shell guard helper，Python guard CLI 延后。
8. GO gate 使用零失败、无新增回归、skip 有解释和 inventory 可比性。

当前 canary 收敛对三项元复核的处理边界：

- M1：历史同步 callback I/O、当前 cleanup/fencing 缺陷和 `OrderInitialized` FILTERED
  回归分别保留独立证据链。
- M2：本轮 gate 已绑定可复跑命令和明确 pass/skip 数字；release-wide 原始输出、JUnit、
  继承环境和依赖制品 hash 继续作为 fleet rollout 前置项。
- M3：runtime resource 单一 owner 与 Node/manifest parity 继续作为 release-wide Phase B；
  受限 canary 只消费已部署 release identity，不把该架构债转化为动态遥测硬门。

## 12. Runtime Validation 状态

验证分支 `codex/account-stall-runtime-validation` 已提交
`ffc14e559afdf7b56bf245d8cadbea1d3013e609`，完成当前已证实的 bounded
cleanup/fencing 修复：

- 104 项 runtime 广域选择通过
- 62 项冻结 focused 选择通过
- b10 node assembly 42 项通过
- fault injection 四项连续 50 个独立进程通过
- 两位 Codex reviewer 均给出 P0/P1/P2 为零的 `PASS`

该结果吸收 Claude M1：它只证明当前 cleanup/fencing 缺陷闭环，不证明历史生产 stall
因果，也不满足 Phase B-S 的 actor tick/progress freeze 授权条件。

该结果部分满足 Claude M2：runtime 子集已有精确 commit、依赖版本、命令、collection
hash、文件 hash 和 review。全四组套件的原始输出、JUnit、继承环境和依赖制品 hash
仍需补齐，A3 保持未完成。

Claude M3 对应的 runtime resource 单一 owner、Node/manifest parity 和 live
fail-closed 仍属 Phase B，当前验证提交未修改相关 owner。

A7 直接导入被依赖闭包阻断。Phase A 与验证分支在七个目标文件上相差 15,543 行新增和
2,274 行删除，Strategy 单文件相差 8,383 行。下一步必须先形成 hunk-level
dependency-closed import manifest，再决定可导入的原子批次。验证提交不作为 release
source。

该历史 A7 结论继续约束验证分支的直接导入。现行
`codex/account-stall-phase-a` 分支已独立完成 runtime、deployment、control-plane 和
canary 契约验证；本文顶部的签名单次 `0.07 SOLUSDT` 授权为当前 superseding gate。
常规 production/fleet RESUME 继续保持关闭。
