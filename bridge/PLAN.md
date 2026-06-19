# Hermes Trader 系统改造执行方案

> 版本：v2.1（2026-06-10）
> 前序账本：`tasklist.json`（v1，36/36 已完成，作为历史存档保留，勿改）
> 本期账本：`tasklist.v2.json`（唯一进度真相源）
> 工作分支：`bridge-import`

---

## 1. 背景与已核实的故障

v1 交付了 bridge 工作区（信号解析、SignalStore、risk governor、审计、release gates、dashboard 前端、SignalStrategy），本地 124 个测试通过。但系统在真实运行中暴露出体验割裂与订单管理缺失，并发生了四起已核实根因的故障：

| 故障 | 根因 | 核实方式 |
|---|---|---|
| HYPE 清晰信号未下单 | `signal_id = f"{channel_id}:{message_id}"`，Telegram 频道 ID 为负数，ID 以 `-` 开头导致下游 CLI argparse 解析失败；随后 Hermes 用错 DB 路径；cron 停在中间态无人通知 | `parser.py:137`、`importer.py:51` 代码确认 |
| INJ 更新消息被当新信号开仓 | 解析器为纯正则提取，无消息类型分类层；持仓截图中的价格框几何自洽，能通过 review 校验 | `parser.py` 全方法清单确认无分类逻辑 |
| 交易 DB 分裂 | 订单/幂等记录与 price alert 分别写在 Mac 上两个不同的 trading.db，持仓判断与幂等检查拿到不一致上下文 | 现象与故障链吻合（Mac 暂不可达，待上机核实） |
| cron 无失败兜底 | 调度任务遇错停在中间态，无失败标记、无重试、无"未下单"通知 | 同上 |

另有遗留债务：dashboard 单测当前 4 failed / 5 passed（涉及 kill switch、手动平仓等安全关键路径）；服务器 `/root/freqtrade` 此前不在版本控制下（已抢救提交至 `bridge-import`，commit `78adad62f`）；分支 `.gitignore` 曾忽略 `freqtrade/` 与 `tasklist.json`，是账本与运行时包失踪的根因；bridge API 的 SignalStore 为内存实现；报告管线为 main.py 内硬编码桩。

---

## 2. 目标架构

```
Telegram watcher ──> Hermes（调度中枢：分析、归类、富化、决策）
                        │ 读：全系统快照（口子全开）
                        │ 写：交易意图 + 提案（收窄、有审计）
                        ▼
              SignalStore + risk governor（机器规则硬门，确定性代码）
                        ▼
              freqtrade SignalStrategy（唯一执行引擎）
                        │ 下单 / 保本 / 时效 / 对账 / 通知
                        ▼
                     交易所
                        ▲
        dashboard 融合中心（统一鉴权入口）
        读 SignalStore（信号/审核流）+ freqtrade API（订单真相）
```

### 部署形态：全栈同址部署（香港服务器）

历史上 watcher/Hermes 在 balen 的 Mac、freqtrade/dashboard 在香港服务器分开部署，跨机请求间隔与网络抖动是体验割裂和故障链（HYPE 的 SSH 远程 importer、DB 分裂）的温床。本期目标：**全部服务部署到香港服务器（149.104.30.223）同一台机器**，服务间走 loopback/容器内网，对外只暴露统一鉴权入口。

```
香港服务器 docker compose（单一编排文件，对外仅 443/SSH）
├── telegram-watcher     （从 Mac 迁移，PM2 → 容器）
├── hermes-harness       （调度中枢 agent 运行时，从 Mac 迁移）
├── api                  （bridge：SignalStore + risk governor + 聚合端点）
├── freqtrade            （SignalStrategy 执行引擎，API 仅 loopback）
├── dashboard            （融合中心前端）
├── postgres / redis     （唯一数据层）
└── caddy                （统一入口：TLS + 鉴权反代，唯一对外暴露面）
```

同址化的直接收益：importer 不再走 SSH 远程调用（HYPE 故障链第二环消失）；交易数据落同一数据层（DB 分裂从根上消失）；Hermes 拉 snapshot 为本机内网调用，决策上下文零延迟。

### 职责边界（三条铁律）

1. **Hermes 决定"要不要交易"，freqtrade 负责"这笔交易怎么活着"。** Hermes 永远不直接碰交易所；Hermes 侧 place-order cron 路径整体下线。
2. **Hermes 读全开、写收窄。** `system_observer` 角色可读一切（信号全量及生命周期事件、风控实时状态与限额占用、freqtrade 订单/持仓代理、审计流水），写只能走交易意图与提案通道；直接改限额、关 kill switch、绕过几何校验均不允许。最后一道闸必须是确定性代码——INJ 事故是依据。
3. **订单真相只有一份**：freqtrade 的 trade DB。Hermes 本地 DB 仅存内部调度记账，不再保存订单与幂等记录。

### 你点名的四个订单管理缺失 → freqtrade 原生机制

| 缺失 | 机制 |
|---|---|
| 下单后无价格监控、失败重下 | `unfilledtimeout` 自动撤未成交单 + `adjust_entry_price` 调整挂单；重复消息由 SignalStore 幂等预留拦截 |
| 缺保本 | `custom_stoploss` 回调：盈利 ≥ 阈值（BTC 2%、山寨 4%，按 pair 配置）将 SL 移至入场价 |
| 订单时效 | `unfilledtimeout`（挂单层）+ `signal_max_age_minutes`（信号层，现行 240）+ reserved 超 24h 自动 expired |
| 整体查询失败 | freqtrade REST API + tradesv3 DB 为唯一订单源，启动与运行中均与交易所对账；dashboard 与 Hermes 都读它 |

---

## 3. 里程碑（按复杂度递进）

> 复杂度：S（小时级）/ M（天级）/ L（多天）。优先级：P0 止血 / P1 核心 / P2 体验。
> 执行方：`codex`（可立即派单）/ `hermes_mac`（需恢复 balen Mac 访问）/ `server_ops`（需服务器操作）/ `operator`（需人工确认）。

### M0 止血（复杂度 S–M，P0，本仓库内立即可做）

消除已知事故的直接诱因，修复安全关键测试回归。

- M0-01 signal_id 改为 CLI 安全格式 `sig-c{abs(channel_id)}-m{message_id}`，下游一律 `--signal-id=` 或 stdin 传参，兼容存量 ID
- M0-02 importer 以 stdin JSON 为主通道；交易意图增加 `message_type` 字段；SignalStore 硬规则：非 `new_signal` 不得进入 approved
- M0-03 修复 `.gitignore`（`freqtrade/`、`tasklist*.json` 不再忽略）
- M0-04 修复 dashboard 4 个失败单测（kill-switch POST、持仓手动操作 POST、断流 resync、daily report 路由）

**验收**：全套 pytest + dashboard vitest 全绿；负数频道 ID 端到端用例通过；`message_type=update` 的意图无法被 approve。

### M1 Hermes 全局视野（复杂度 M，P1，bridge 侧）

给调度中枢开读全开的口子。

- M1-01 `system_observer` 角色 + Bearer token（扩展 `permissions.py`）
- M1-02 `GET /api/system/snapshot`：一次调用返回账户、持仓、近期信号、风控状态与限额占用、最近事件的紧凑快照
- M1-03 `GET /api/risk/explain?signal_id=`：信号被拦原因与放行差距
- M1-04 bridge 只读代理 freqtrade REST（Hermes 不直连 freqtrade 端口）

**验收**：observer token 可读全部数据、无任何写权限（403 用例覆盖）；snapshot 字段契约入 `shared_contracts`。

### M2 freqtrade 执行引擎升级（复杂度 M–L，P1）

把四个订单管理机制落进执行层。

- M2-01 `custom_stoploss` 保本：per-pair 盈利阈值配置（默认 BTC 2%、山寨 4%），触发后 SL 移至入场价
- M2-02 `unfilledtimeout` 配置 + `adjust_entry_price` 策略
- M2-03 信号 TTL：approved/reserved 超 24h 自动转 expired + 清理任务
- M2-04 SignalStore 持久化：统一 sqlite schema + migrations，替换 bridge API 内存实现，与 strategy 共用同一存储
- M2-05 freqtrade Telegram 通知打通（成交/撤单/止损推送）

**验收**：保本/超时/TTL 各有单测与 dry-run 集成用例；bridge API 重启不丢信号。

### M3 Hermes/Harness 迁移上服务器并改造（复杂度 M–L，含 P0 项）

把 telegram-watcher 与 Hermes 调度面（harness）从 balen 的 Mac 迁移到香港服务器容器化运行，并在迁移版本上完成调度中枢改造。

- M3-00 迁移：取得 telegram-watcher 与 Hermes harness 源码（Mac 可达时直取，或走其 git 远端），容器化进 compose（PM2 → 容器），环境变量与凭据经 `.env`（0600）注入
- M3-01（P0）消息分类门：new_signal / update / position_screenshot / noise，仅 new_signal 可产交易意图
- M3-02（P0）调度状态机：dispatched / failed / timeout 全捕获，失败必发"未下单"通知，禁止静默中间态
- M3-03（P0）交易 DB 统一：订单与幂等记录全部落服务器数据层，Hermes 仅留内部调度记账
- M3-04 决策前拉 `/api/system/snapshot`（归类前先查该币当前持仓），同机内网调用
- M3-05 importer 改本机直连 SignalStore（loopback，废弃 SSH 远程 importer 整条路径）

**验收**：watcher 与 harness 在服务器容器内稳定运行并接收真实消息；INJ 原文回放被分类为 update 且不产意图；模拟调度失败收到通知；幂等查询命中统一存储。
**前置**：唯一外部依赖是一次性取码（Mac 恢复可达，或提供 telegram-watcher / harness 的 git 远端地址与凭据）。

### M4 整机部署、切流与旧路径下线（复杂度 M–L，P1，高风险操作）

- M4-01 服务器部署全面改为从 git `bridge-import` 分支拉取 + 单一 compose 编排（含 caddy 统一入口），消除漂移；冻结手工改动
- M4-02 testnet 端到端：watcher → Hermes → SignalStore → freqtrade（Binance testnet）下单 → 通知 → dashboard 全链路在同一台服务器上验证
- M4-03 下线旧路径：停掉 Mac 上的 watcher/Hermes/place-order cron 全部服务（在 M4-02 通过后执行），Mac 退役为纯开发机
- M4-04 对账巡检：freqtrade DB vs 交易所定期比对，差异告警

**验收**：testnet 真实信号完整生命周期在单机跑通；Mac 侧无任何运行中的交易相关服务；对外暴露面仅 caddy 443 与 SSH。

### M5 融合中心（复杂度 L，P2）

- M5-01 统一鉴权入口（登录 + token 发放，dashboard 与 API 共用）
- M5-02 dashboard 订单中心（持仓/挂单/历史/盈亏，读 freqtrade API）
- M5-03 dashboard 信号审核流（needs_review 队列、approve/reject、Hermes 归类结论与提案展示）
- M5-04 真实报告管线（snapshot/renderer/fallback 替换 main.py 桩）
- M5-05 Playwright e2e 跑通并留存证据
- M5-06 Telegram 机器人退化为纯通知通道

**验收**：用户登录一个入口可完成查单、审核、风控操作全流程；报告含账户/交易/信号/风险/持仓真实快照。

### M6 门禁与放量（operator 人工项，沿用 v1 release gates）

- M6-01 testnet gate：kill switch 实操演练、交易所 key 限 testnet 核查
- M6-02 live readonly gate：live 无自动开仓、只读看板、基于 live 快照的报告、审计无密钥泄漏、人工恢复演练
- M6-03 live 小额自动化 gate：连续 7 天 dry-run 报告、30 个信号完整生命周期、被拦信号全部可解释、kill switch 与日亏熔断演练、单笔风险 ≤ 0.25%

---

## 4. 执行顺序与依赖

```
M0 ──> M1 ──> M2 ──┐
        │           ├──> M4 ──> M6
        └──> M3 ────┘
                    M5（M1 后即可并行启动，M4 后收尾）
```

- M0/M1/M2 可立即派 Codex 在 `bridge-import` 分支执行（M0 先行，M1/M2 可并行）。
- M3 的唯一外部依赖是 **telegram-watcher / Hermes harness 源码的一次性获取**（Mac 恢复可达，或提供 git 远端）；拿到码后 M3 全部在服务器上进行，不再依赖 Mac。
- M4-03（下线 Mac 旧服务）必须在 M4-02 testnet 全链路通过后执行，不可提前。

## 5. 验证策略

- 每个任务必须附 evidence（命令 + 输出或产物路径）才能置 done，沿用 v1 账本纪律。
- 回归基线：bridge pytest 124 个 + dashboard vitest 9 个全绿为合入门槛。
- 故障回放用例：HYPE 原始消息（负数频道 ID）、INJ 原文（更新语义）作为固定 fixture 进入测试集，永久防回归。
- 高危变更（切流、下线 cron、gate 放行）需 operator 在 dashboard/audit 留痕确认。

## 6. 风险与未决

| 风险 | 缓解 |
|---|---|
| watcher/harness 源码取不到（Mac 不可达且无 git 远端） | M0–M2 先行不受影响；优先确认 telegram-watcher 的 git remote；最坏情况在服务器上按既有接口契约重写 watcher（其核心是 Telegram 收消息 + 调 importer，规模可控） |
| Mac 上 watcher 工作树有未提交改动（bridge notes 已记录 dirty） | 取码时必须连未提交改动一起带走，不能只 clone 远端 |
| 服务器部署与 git 漂移再次发生 | M4-01 后冻结服务器手工改动，一切变更走 git |
| 单机部署单点故障 | 接受为当前阶段权衡（体验优先）；compose 全栈可一键重建，数据卷独立备份；live 放量前（M6）再评估高可用 |
| 迁移期间双路径并存导致重复下单 | 切流窗口内 Mac 侧先置只读（SIGNAL_IMPORTER_ENABLED=0 / dry），SignalStore 幂等预留为最后防线 |
| 存量 signal_id（旧格式）与新格式共存 | M0-01 提供兼容读取，仅新增信号用新格式 |
| Hermes 归类误判（LLM 不确定性） | message_type 硬门 + risk governor 几何/限额校验双层兜底，宁可漏单不可错单 |
| Telegram 会话/凭据迁移（watcher 登录态、bot token） | 凭据经服务器 `.env`（0600）注入；迁移窗口安排在无信号时段，迁移后用测试频道消息验证收发 |
