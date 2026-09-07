# 面板与移动端升级设计 v1（panel-and-app-upgrade-v1）

- 日期：2026-08-30
- 状态：设计冻结，待实现（本文档只含设计/验收/测试标准，不含实现）
- 决策人：balen；设计整理：Claude Code
- 差距盘点依据：2026-08-30 全仓扫描（bridge/apps/dashboard、bridge/apps/api、services/control-plane、services/report、hermes-profile v3-trader、db/migrations 0005–0018）

---

## 0. 决策记录（已拍板，不再复议）

| # | 决策 | 内容 |
|---|---|---|
| D1 | 升级范围 | web 管理面板（bridge/apps/dashboard）与战报页（services/report）都升级 |
| D2 | Attention app 定位 | 告警链路保持现状不动；app 新增"Trading"区：多账号状态（持仓/余额）+ 手动止盈止损 + 百分比平仓 |
| D3 | app 源码位置 | `~/projects/working/alert-personal/apps/attention-android`（RN 0.82 monorepo）；trader-bot 内 `apps/attention-android` 仅为 APK 产物落点 |
| D4 | app 连接方式 | **直连控制面** `/v1/*`（不走 attention-service 网关）。兜底：仅 Tailscale 可达、危险操作客户端二次确认 + 幂等 ref（原"独立受限 token"一条被 D12 撤销） |
| D5 | 面板优先主题 | 全部四块：多账号+真相层、保护单真实状态、绩效与战报增强、审计链+舰队健康；三处真断裂无条件先修 |
| D6 | 分期 | Phase 0 修断裂 → Phase 1 控制面读模型（app/面板共用地基）→ Phase 2 面板四主题 → Phase 3 RN app |
| D7 | 面板 UI 基座 | 前端交互组件换 **Headless UI**（`@headlessui/react` + Tailwind CSS）；Phase 2 重构页面时逐页迁移，Phase 0 不动样式 |
| D8 | 交付方式 | **分模块交付**（见 §10 模块划分），每个模块独立派发、独立验收，可并行的并行 |
| D9 | 记账红线 | 本次升级**不得触碰任何记账写路径**：不改 schema、不新增对 trade_outcomes / orders_projection / execution_events 等表的写入；所有写操作只经既有 operator API（见 §7A 记账安全） |
| D10 | 功能基准 | 部署基准是 **jp-bot.balen.wang**（jp-24）上的这套面板；app Trading 区功能与升级后的 jp-bot 面板保持一致（同数据源、同口径、同操作语义），app 不做面板没有的能力 |
| D11 | app 组件库 | **Ant Design RN 版 `@ant-design/react-native`**（5.4.3，peer `react-native>=0.67.5`，需引入 gesture-handler + reanimated）。注：用户指的 mobile.ant.design（antd-mobile v5）是 web/H5 库，RN 端对应物即此包。M3a 必须先打样验证 RN 0.82 新架构下 List/Modal/Slider/Stepper/Toast 可用；打样失败则降级为"仅采用 antd 设计 token/样式规范，组件自绘"，由 G0 裁决 |
| D12 | 不加安全类约束 | 用户拍板（2026-08-30）：本轮**不新增安全类约束**——撤销 `mobile_operator` 新角色（原 M1d 整体出栈，app 复用既有 operator token）、不做 viewer 门禁扩展、不做逐路由权限判断。二次确认（dry_run 流）与幂等 ref 保留——它们属于记账正确性（D9），不属安全加固 |
| D13 | v1.1 修订 | 2026-08-30 codex 落地前 review 报 19 阻断 + 10 缺口，本文档已按其逐条修订（各节标注 review #N）；review 全文见会话存档 |

---

## 1. 现状基线与断裂点（事实，附证据）

### 1.1 三处真断裂（Phase 0 目标）

| # | 断裂 | 证据 |
|---|---|---|
| B1 | 面板发出的 `cancel_order` / `move_stop_loss` / `set_reducing` 不在控制面命令白名单（`HALT/REDUCE/RESUME/CANCEL_ALL/CLOSE_ALL/REFRESH_EVIDENCE`），必然 400。单笔操作的真实端点 `POST /v1/operator/orders`（动作集 open_position/close_position/partial_close/move_stop_loss/replace_take_profits/cancel_order）面板从不调用 | `bridge/apps/dashboard/src/utils/api.ts:1939-2006` vs `services/control-plane/api/read_api.py:919-927`、`:5722`、`:7469` |
| B2 | 面板 Reports 页调用 `/v1/reports/daily/*`，该路由在控制面不存在（只存在于 e2e mock）；真实实现在 bridge `/api/reports/*` | `read_api.py` 全文无 reports 路由；`bridge/apps/api/app/reports/router.py` |
| B3 | HALT/RESUME 属 account-scoped 命令，控制面要求 `scope.account_id` + `target_nodes`，面板只发 `{args, type}` | `read_api.py:945-955` vs `api.ts:1779-1785` |

### 1.2 关键数据事实（设计约束）

- 真相层级：`exchange_state_mirror`（直连币安）> 任何投影/快照；镜像新鲜度告警阈值 300s（`v3_query.py`）
- 机器人订单识别：`clientOrderId ~ ^B[0-9a-f]{32}[0-9]{2}$`
- hedge 模式：SL/TP 保护单必须按 exit side 拆分归属（2026-07-12 ETH 双向空单误判"裸奔"事故的教训）
- `/v1` 响应统一携带 `_envelope()` 新鲜度信封（snapshot_id / generated_at / projection_lag_ms / stale / missing_nodes / reconciliation_state）
- `/v1/positions`、`/v1/orders`、`/v1/trades` 现不返回 `account_id`（面板 Account 列恒 `--`）
- 面板 Protection 状态为前端猜测（`stopLoss > 0 ? "protected" : "missing"`）；真值在 `protective_orders_projection` + protection watchdog
- `trade_outcomes`（R 倍数/持仓时长/费用）面板完全不读
- `protection_policy` 契约四态：`complete | stop_only | deferred | waived`
- operator 开仓/动仓响应含 `warnings`（含 `duplicate_open_intent`）与 `attribution.would_reject`

---

## 2. 总体架构

```
┌─────────────────────────┐        ┌──────────────────────────┐
│ web 面板 (dashboard)     │        │ RN app (alert-personal)   │
│ Phase 0 + Phase 2       │        │ Phase 3 Trading 区        │
└───────────┬─────────────┘        └───────────┬──────────────┘
            │ HTTPS (现有链路)                   │ Tailscale only
            ▼                                  ▼
┌──────────────────────────────────────────────────────────────┐
│ 控制面 read_api（Phase 1 扩展）                                │
│  既有: /v1/accounts /v1/positions /v1/orders /v1/operator/*   │
│  新增: /v1/mirror/positions  /v1/reconcile  /v1/outcomes      │
│  修改: positions/orders/trades 补 account_id；保护单真值        │
└──────────────────────────────────────────────────────────────┘
            │
            ▼
  Postgres（accounts_projection / exchange_state_mirror /
  protective_orders_projection / trade_outcomes / audit_events…）
```

### 2.1 面板 UI 技术栈（D7）

- 引入 `@headlessui/react`（Dialog / Menu / Tab / Listbox / Switch / Combobox / Popover）+ Tailwind CSS；现有手写 `styles.css` 逐页退役
- 迁移策略：**逐页替换，不做一次性大爆炸**。Phase 0 只修逻辑不动 UI；Phase 2 各模块重构到哪页就迁移哪页；未迁移页与已迁移页允许共存（Tailwind 以 scoped/preflight 控制方式引入，不得破坏未迁移页样式）
- 替换映射：SaveReviewDialog / DangerConfirmDialog / 详情抽屉 → `Dialog`；settings 8-tab → `Tab`；scope/账户选择器 → `Listbox`；过滤条 → `Combobox`/`Listbox`；开关类设置 → `Switch`
- 自绘展示组件（StatusPill、数据龄徽章、图表容器）保留自绘，只换交互组件
- 收益即验收项：Headless UI 自带键盘导航与 ARIA，迁移后的每页需通过基本 a11y 断言（见 T2-11）

原则：

1. **控制面是唯一权威读写面**。app 与面板不新建第二数据通道；bridge FastAPI 不再承接新功能（其内存态 audit/release-gates 不扩展）。
2. **读走信封，写走 operator**。所有新增读端点必须复用 `_envelope()`；所有单笔仓位/订单操作只走 `POST /v1/operator/orders`。
3. **镜像优先展示**。凡展示持仓/挂单的地方标注数据源（mirror vs projection）与数据龄，两边打架以 mirror 为准并显式提示。

---

## 3. Phase 0 — 修面板真断裂

### 3.1 设计（v1.1 按 codex review 修订）

1. `src/utils/api.ts` 命令层重构，按**代码事实契约**（review 阻断 #1/#2/#3/#7）：
   - 全局命令白名单：`HALT / REDUCE / RESUME / CANCEL_ALL / CLOSE_ALL / REFRESH_EVIDENCE`
   - **信封必填**：每条命令顶层携带 `reason`、`confirm: true`、`request_id`（前缀 `dashboard-`）
   - **account-scoped 集合是 `HALT / REDUCE / RESUME / REFRESH_EVIDENCE`**（不是 CANCEL_ALL/CLOSE_ALL）：这四种必须带 `scope.account_id` + `target_nodes`，且一账户一节点一命令（四账户需循环发四条）
   - RESUME 额外需要 `scope.symbol`（锚点）与 `scope.release_id`（64 位完整值）；`release_id` 由 M1a 补进 `/v1/nodes` 响应；Pause/Resume UI 必须加账户选择器
   - **命令完成判定**（review 缺口 #1）：POST 2xx 仅表示命令已创建；前端轮询 `GET /v1/commands/{id}` 至终态，定义 pending/partial/failed 展示与 30s 超时；REFRESH_EVIDENCE 成功后需等证据新鲜窗（30s 内 updated_at 跳变）再放 RESUME
   - Kill Switch 语义闭合（#7）：输入 `CLOSE ALL` 确认后按序发 各账户 HALT → `CLOSE_ALL`，全部命令轮询到终态才算完成，UI 分步展示
2. 单笔操作（撤单/平仓/部分平仓/移损/改 TP）改走 `POST /v1/operator/orders`，按其**真实契约**（#5/#6/#11/#12）：
   - 请求通用字段：`action / symbol / account_id / reason / client_ref`；管理动作另需目标解析字段（`parent_intent_id` 或 `entry_ref` 或 `position_side`）；cancel 用机器人格式 `client_order_id`；move SL 字段名 `stop_loss`；partial_close 传**绝对 `quantity`**（百分比→数量换算在前端完成，见 §6）
   - 响应是 `intent_id / order_plan / warnings / attribution`（**不是** CommandResult 的 `command_id/acks`）：前端新增 operator 响应解析层，`warnings` 与 `attribution.would_reject` 全文展示
   - **二次确认协议 = `dry_run → 用户确认 → 正式提交`**（#11）：先带 `dry_run: true` 提交拿 would_reject/warnings 预览（不落 intent），用户确认后正式提交；正式提交的重试才复用同一 `client_ref`
   - 因 M0 的 operator 动作强制 `account_id` 而现有行数据无此字段（#4），**M0 依赖 M1a 先合并**（见 §9 依赖表，原"无依赖"作废）
3. pair lock 改调 bridge `/api/risk/pair-locks`（#8）：请求带 `pair / expires_at / reason / confirm: true`，`postJson` 支持自定义 `X-Request-Id` 头（非空必填）；UI 增加过期时间输入，并标注其审计为进程内存态（重启即失）
4. Reports 页接入 `/api/reports/*`（#9/#10）——不是只换前缀：
   - 按真实响应形状适配：markdown 端点返回 JSON 对象（取字段再渲染）、versions 返回裸数组（不得被 `getJson()` 归一化吞掉）、daily 的 `open_positions` 是汇总对象非明细数组
   - **验收降级**：bridge 报表收集器生产返回空值是既知现状，M0 验收 = 五个端点真实调通、按 schema 正确渲染、空数据显式标注"数据源未接线"；真实数据源接线是 M2g（新模块，见 §5）
5. Freqtrade 遗留清理：持仓表 `Freqtrade Trade ID` 列删除；`/api/freqtrade` 反代路由标记 deprecated（本轮不删端点，防外部依赖）

（D12：viewer 角色门禁扩展等安全类新约束不做，维持现状。）

### 3.2 验收标准

- [ ] 面板上每一个可点的操作按钮（Pause/Resume/Close/Partial/Move SL/Cancel/Lock pair/Kill Switch）在真实控制面上执行均返回 2xx 或**业务语义拒绝**（如 would_reject），不再出现命令类型 400
- [ ] HALT → RESUME 全流程在面板可走通（含账户选择、REFRESH_EVIDENCE 前置与证据新鲜窗等待），命令落入 `operator_commands` 且 `request_id` 前缀可识别来源；命令终态经 `GET /v1/commands/{id}` 轮询确认
- [ ] Kill Switch 走 各账户 HALT → CLOSE_ALL 顺序流并轮询至终态；单笔操作走 dry_run→确认→提交，warnings/would_reject 全文展示
- [ ] Reports 页对真实 bridge 后端五端点调通且按真实响应形状渲染（数据可为零值，页面标注"数据源未接线，待 M2g"）；下载 Markdown / Telegram 预览可用
- [ ] 全仓 `grep -ri freqtrade bridge/apps/dashboard/src` 仅剩注释/迁移说明，UI 无 Freqtrade 字样
- [ ] viewer 角色下所有操作按钮不可见或禁用（回归既有行为）

### 3.3 测试标准

前端（vitest，`bridge/apps/dashboard`）：
- T0-1 `issueCommand` 单测（函数导出供测试；文件命名符合 vitest 收集规则——review 缺口 #4）：六种合法命令各一例，断言顶层 `reason`/`confirm:true`/`request_id` 必含；account-scoped 四种（HALT/REDUCE/RESUME/REFRESH_EVIDENCE）断言 `scope.account_id`+`target_nodes`；RESUME 另断言 `scope.symbol`+`scope.release_id`；断言不再产生 `cancel_order`/`move_stop_loss`/`set_reducing` 命令类型
- T0-2 operator 操作单测：close/partial/move_sl/cancel 各一例，断言打到 `/v1/operator/orders` 且 body 含 action/symbol/account_id/reason/client_ref 与目标解析字段（parent_intent_id/entry_ref/position_side）；partial 为绝对 quantity；dry_run 预检与正式提交两段式；响应解析层取 intent_id/warnings/attribution（不是 command_id）
- T0-3 Reports 页：按 bridge 真实响应形状 mock（markdown 为 JSON 对象、versions 为裸数组、daily.open_positions 为汇总对象），断言渲染与下载链接；全零数据 → "数据源未接线"标注
- T0-7 pair-lock：请求含 pair/expires_at/reason/confirm 与非空 X-Request-Id 头；缺项 422 场景断言
- T0-8 命令轮询：pending→终态状态机、30s 超时展示、REFRESH_EVIDENCE 成功后的证据新鲜窗等待
- T0-4 持仓表快照测试：无 Freqtrade 列

e2e（Playwright，现有 `e2e/dashboard.spec.ts` 扩展）：
- T0-5 mock server 按控制面真实契约校验请求（收到白名单外命令类型直接 500 使测试失败）
- T0-6 HALT→REFRESH_EVIDENCE→RESUME 剧本

命令：`npm --prefix bridge/apps/dashboard test && npm --prefix bridge/apps/dashboard run test:e2e`

---

## 4. Phase 1 — 控制面读模型补齐（地基）

### 4.1 端点契约

**(a) 既有端点补字段**（不破坏现有消费者：只增不改不删）

- `GET /v1/positions`、`GET /v1/orders`、`GET /v1/trades` 每行新增 `account_id`
- `GET /v1/positions` 每行新增 `protection` 对象（真值，见 (d)）
- `GET /v1/nodes` 每节点新增 `release_id`（RESUME 组装需要，review #3）与 `halt_reason` / `trading_state`（从节点 `/ready` 或心跳取，review #15）

**(b) `GET /v1/mirror/positions`（新增）**

以 `exchange_state_mirror` 为唯一来源。响应（信封内 `data`）：

```json
{
  "accounts": [{
    "account_id": "account-a",
    "mirror_age_seconds": 12.4,
    "stale": false,
    "positions": [{
      "symbol": "ETHUSDT",
      "position_side": "SHORT",
      "quantity": "1.25",
      "entry_price": "...",
      "mark_price": "...",
      "unrealized_pnl": "...",
      "leverage": "...",
      "quantity_step": "0.001",
      "min_quantity": "0.001",
      "protection": {
        "stop_loss": [{"order_id": "...", "trigger_price": "...", "quantity": "...", "is_bot_order": true}],
        "take_profits": [{"order_id": "...", "trigger_price": "...", "quantity": "..."}],
        "status": "protected | partial | unprotected"
      }
    }],
    "open_orders_count": 3,
    "algo_orders_count": 2
  }]
}
```

约束（v1.1 修订）：
- SL/TP 归属按 **exit side** 拆分（hedge LONG 仓只认 side=SELL 的保护单，SHORT 仓只认 side=BUY），逻辑从 `v3_query.py:146-185` 移植，服务端单测覆盖；**保护单必须同时读 mirror 的 `open_orders` 与 `algo_orders`**（币安 SL/TP 条件单在 algo 侧，review #17）
- `quantity_step` / `min_quantity` 来自控制面既有 exchange filters（review #12：供前端做百分比→数量预览取整；最终有效性仍由服务端/交易所判定）
- `mirror_age_seconds > 300` 时该账户 `stale: true`；端点信封 `data_source` 标 `exchange_state_mirror`，信封级 `stale` = 任一账户 stale（review 缺口 #8）
- `is_bot_order` 按 `^B[0-9a-f]{32}[0-9]{2}$` 判定，外部手动单要能被识别出来
- **不含 `protection_policy`**（review #18：该值只存在于 open/add intent 的 order_plan，没有稳定的 position 归属规则；policy 展示推迟到有归属规则后，本轮三态只由 mirror 保护单在场性计算）

**(c) `GET /v1/reconcile`（新增，v1.1 修订）**

`orders_projection` vs `exchange_state_mirror` 差异（以 `v3_query.py:361-395` 为起点但修其两处缺陷，review #17）：
- 交易所侧订单集合 = mirror `open_orders` **∪ `algo_orders`**（否则 SL/TP 条件单全部误报为幽灵单）
- 账户 mirror stale 时 ghost 与 missing **都跳过**并标注该账户 skipped（旧脚本只跳 ghost）
- 返回 `ghost_orders`（投影有/交易所无）与 `missing_orders`（交易所有/投影无），每条带 account_id、symbol、order_id、检出时间；附最近一次 `reconciliation_runs` 摘要与未解决 `reconciliation_findings` 计数

**(d) 保护单真值**

`/v1/positions` 与 `/v1/mirror/positions` 的 `protection.status` 计算规则统一（数据源含 algo_orders，见 (b)）：
- `protected`：存在覆盖全量仓位的 SL（数量 ≥ 仓位量）
- `partial`：有 SL 但覆盖不足，或只有 TP 无 SL
- `unprotected`：无任何 SL
- `protection_policy` 不进本契约（review #18，见 (b) 约束）；无保护告警仅按三态判定

**(e) `GET /v1/outcomes`（新增）**

`trade_outcomes` 查询：参数 `days`（默认 7）、`account_id`（可选）。每行：symbol、direction、realized_pnl、r_multiple、holding_seconds、fees、entry_avg、exit_avg、closed_at、account_id。附聚合与水位（`trade_outcome_job_runs` 最新物化时间，36h 阈值判 stale）。

**KPI 公式冻结**（review 缺口 #6，实现与测试都以此为准）：
- `win_rate` = 盈利笔数 / 总笔数（0-1 小数，非百分制）；空窗口 → null
- `profit_factor` = 总盈利 / |总亏损|；零亏损 → null（前端显示 "∞"）；零盈利 → 0；空窗口 → null
- `avg_r` = r_multiple 非 NULL 行的均值；`avg_holding_seconds` 分母 = holding_seconds 非 NULL 行数
- `r_distribution` 6 桶沿用 `report_service.py:661` 边界；边界值归属按该实现现状锁定并在单测断言 -2/0/1/2 四个边界

**(f) app 鉴权（D12 修订：撤销 mobile_operator 方案）**

app 直连控制面时**复用既有 operator token**（与 `v3_trade.py` 同一凭据体系），不新增角色、不做逐路由权限收窄。原 M1d 模块出栈；T1-9 越权矩阵测试作废。

**(g) M1e 审计链与节点读模型（新增模块，M2e 的地基，review #15）**

- `GET /v1/operator/orders/{intent_id}` 扩展（或新端点 `/v1/intents/{id}/trace`）：聚合六层——原始消息（raw_messages+media 引用）、hermes 决策、风控决策、intent、execution events、节点回执——对齐 `v3_query.py intent` 的信息量
- `production_incidents` 只读列表端点（operator 可读）；resolve 仍走既有节点身份端点，**面板本轮只读不做 resolve 按钮**
- 命令状态查询 `GET /v1/commands/{id}`（如已存在则确认响应含终态字段；M0 的轮询协议依赖它）

### 4.2 验收标准

- [ ] 上述各项端点契约全部实现且带 `_envelope()` 信封（mirror 端点 data_source 标 exchange_state_mirror）
- [ ] 既有消费者零回归：现有面板在未升级前端的情况下所有页面仍正常（字段只增）
- [ ] `/v1/mirror/positions` 与 `v3_query.py positions` 在同一时刻对同一库的输出一致（人工抽查 hedge 账户一次，记录在 PR 描述）
- [ ] 契约文件落盘并**完成登记**（review 缺口 #7）：schema 进 `packages/contracts/v1/` 且同步更新 `tests/contracts/test_contracts_v1.py` 的 SCHEMAS 表、version manifest、snapshot、examples 索引，否则不进验证——**吸取 e2a37f6 教训：契约文件必须进节点/服务镜像的发布登记表**

### 4.3 测试标准

服务端（pytest，`services/control-plane` 现有测试套扩展）：
- T1-1 mirror positions：单向持仓 + SL/TP 完整 → `protected`
- T1-2 mirror positions：**hedge 双向同 symbol**（LONG+SHORT），SL 各归各的 exit side；断言不出现交叉归属（回归 2026-07-12 事故形态）
- T1-3 mirror positions：只有 TP 无 SL → `partial`；无任何保护单 → `unprotected`；**SL 在 algo_orders 而非 open_orders 时必须被识别**（review #17）
- T1-4 mirror 新鲜度：镜像时间戳 > 300s → 账户 `stale: true`，信封级 stale 联动
- T1-5 bot 单识别：合法 `B<32hex><2digit>` → `is_bot_order: true`；外部手动单 → false
- T1-6 reconcile：构造幽灵单/漏记单各一 → 分别出现在对应数组；algo 保护单在场 → 不误报幽灵；stale 账户 → ghost/missing 都跳过并标 skipped；无差异 → 两数组为空且 runs 摘要存在
- T1-7 outcomes：KPI 公式冻结表逐条断言（含 profit_factor 零亏损/零盈利/空窗口、win_rate 小数口径、R 分桶 -2/0/1/2 边界）、水位过期（>36h）→ stale 标记
- T1-8 account_id 回填：positions/orders/trades 三端点每行非空且属于 account-a..d；nodes 端点含 release_id/halt_reason
- T1-9 operator 动作契约用例（review 缺口 #5）：缺/错 `position_side` 的 hedge 用例、错误归属引用被拒、外部手动单（非 B 前缀）撤单被服务端拒绝、同 `client_ref` 重试幂等
- T1-10 operator 动作回归：partial_close 以绝对 `quantity` 提交；`dry_run: true` 不落 intent；响应含 warnings/attribution
- T1-11 M1e 溯源端点：完整六层 fixture 聚合正确；缺层（无 hermes 决策的手动 intent）优雅降级；incidents 只读列表可查

命令：按 `services/control-plane` 现有测试入口（Makefile/pytest 约定）跑全量，新增用例并入同一套。

---

## 5. Phase 2 — web 面板四主题

### 5.1 设计

1. **多账号 + 真相层**
   - Dashboard 顶部改为四账户卡（equity / available / unrealized PnL / 在场仓数 / 挂单数），聚合行保留总计；数据 `/v1/accounts` + `/v1/mirror/positions`
   - 持仓视图切换器：`镜像（真相）` / `投影` 两档，默认镜像；每个表头显示数据龄徽章（绿 <60s / 黄 <300s / 红 ≥300s 或 stale）
   - 新增 `/reconcile` 页：幽灵单/漏记单表 + 最近对账 run 摘要；有未解决差异时侧边栏该项亮红点
2. **保护单真实状态**
   - 持仓表 Protection 列改用后端真值三态 + `protection_policy` 角标；删除前端猜测逻辑
   - 顶部告警条：存在 `unprotected` 且 policy 非 waived/deferred 的仓位时全局红条，点击跳转到该仓
3. **绩效**
   - Dashboard 新增 Performance 区（数据 `/v1/outcomes`）：R 分布柱状、累计 PnL 折线、分币种横向柱状、win rate / profit factor / avg R / 平均持仓时长 KPI —— 图表规格对齐战报 `template.html` 现有实现
   - History 表替换数据源为 `/v1/outcomes` 明细（修 `pnlPct` 恒 0 问题）
   - 战报增强（services/report，review #16 修订）：KPI 行追加 profit factor 与平均持仓时长（公式对齐 §4.1e 冻结表）；依赖三态渲染**仅覆盖"仍可出报"的降级**（mirror 过期、部分数据缺失 → 页首黄条 + `missing_data`），database/outcomes 水位硬失败**保持现状 503 fail-closed 不出报**；新鲜度阈值双轨并存并显式记录：战报发布闸门 mirror 阈值维持既有 180s，面板/app 展示阈值 300s，两者语义不同不强行统一
4. **审计链 + 舰队健康**（依赖 M1e，review #15 修订）
   - Orders 详情抽屉接 M1e 溯源端点：完整六层链路 原始消息 → hermes 决策 → 风控 → intent → 执行事件 → 节点回执，拒因（含 `position_state_unknown / position_state_conflicted / position_exists`）原样展示
   - Bot 列表升级为节点健康卡：心跳 age（>120s 红）、`readiness` / `health_degraded_reasons` / `version`（既有字段）+ `halt_reason` / `trading_state` / `release_id`（M1a 补入）
   - 新增未关闭 `production_incidents` 表（**只读**，走 M1e 列表端点；resolve 需节点身份+围栏头，本轮不做按钮）
5. **M2g 报表数据源接线**（新增模块，review #10）：bridge `report_snapshot` 的 account/trades/risk/positions 收集器从生产空值实现改接控制面读端点（`/v1/accounts`、`/v1/outcomes`、`/v1/risk/state`、`/v1/mirror/positions`），Reports 页从"结构正确但全零"变为真实日报；依赖 M1b/M1c

### 5.2 验收标准

- [ ] 四账户卡与战报"四账户收盘"同时刻数字一致（同库人工抽查一次，记录在 PR）
- [ ] 镜像/投影切换下持仓数据源和数据龄徽章正确；拔掉 mirror 数据（模拟 stale）时 UI 降级为黄/红而非报错白屏
- [ ] 构造一笔无保护仓位（测试环境）→ 全局红条出现（三态仅由 mirror 保护单在场性判定，D12/review #18 后无 waived 抑制逻辑）
- [ ] intent 溯源抽屉对一笔真实 intent 展示全部 6 层信息（走 M1e 端点），拒因文本可见
- [ ] 节点 HALT 时面板 5 秒内（下一次拉取周期）显示 halt_reason；心跳冻结 >120s 显示红
- [ ] 全部操作按钮在 viewer 角色下只读（回归）
- [ ] 战报页在依赖 degraded 时页首出现三态标识

### 5.3 测试标准

前端（vitest）：
- T2-1 四账户卡渲染与聚合计算（含某账户缺失时的占位）
- T2-2 数据龄徽章三档阈值边界（59/60/299/300s）
- T2-3 Protection 列三态渲染；全局红条出现逻辑（无 policy 角标，review #18）
- T2-4 溯源抽屉：完整链路 fixture 渲染 6 层（M1e 契约 fixture）；缺层（如无 hermes 决策的手动单）优雅降级
- T2-5 节点卡：halt_reason 展示、心跳 >120s 红色态
- T2-6 Performance 区图表数据变换（R 分桶映射、累计 PnL 累加）

e2e（Playwright）：
- T2-7 reconcile 页剧本：mock 返回幽灵单 → 红点 + 表格行
- T2-8 无保护告警剧本：红条 → 点击跳转定位到仓位行
- T2-11 a11y 断言（Headless UI 迁移页）：Dialog 聚焦陷阱与 Esc 关闭、Tab 键盘切换、Listbox 方向键选择；每个迁移页跑 axe 基本检查无 critical 违规（axe 相关依赖需先加入 devDependencies，review 缺口 #4）

战报（pytest，`services/report/tests` 扩展）：
- T2-9 新 KPI 计算（profit factor 除零、无平仓窗口）
- T2-10 可出报降级的三态渲染进 HTML（mirror 过期 fixture → 页首黄条）；db/outcomes 硬失败 → 维持 503 不出报（断言现状不回归）
- T2-12 M2g 收集器：接线后 daily 响应各段非零（对测试库 fixture）；控制面不可达时收集器降级为显式 unavailable 标记而非静默全零

---

## 6. Phase 3 — RN app Trading 区（alert-personal 仓库）

### 6.1 设计

**功能基准（D10）**：app Trading 区是 jp-bot 面板（升级后）对应能力的移动端子集——账户视图对齐面板四账户卡、持仓视图对齐面板镜像持仓视图（同 `/v1/mirror/positions`、同保护单三态口径、同数据龄阈值）、操作对齐面板的 Close/Partial/Move SL/TP 语义（同 `/v1/operator/orders` 动作与参数）。禁止 app 单独发明面板没有的口径或操作；两端展示同一数字必须同源同算法。

导航：底部或抽屉新增 `Trading` 入口，与现有告警区并列，互不影响（D2：告警链路零改动）。

配置：Setup/Settings 扩展一组独立配置 `tradingApi = { baseUrl, token }`，与 attention 配置分开存储（`secureStorage` 新命名空间）；baseUrl 预期为 Tailscale 内网地址，UI 提示"仅内网可达"。

三个屏：

1. **AccountsScreen**：四账户卡（equity / available / unrealized PnL），下拉刷新；顶部全局新鲜度条（取信封 `stale` / `projection_lag_ms`）
2. **PositionsScreen**：数据 `/v1/mirror/positions`，按账户分组；每仓一行：symbol、方向、数量、开仓价、标记价、未实现盈亏、Protection 三态色点；mirror stale 时整屏黄条警示
3. **PositionDetailScreen**：
   - 信息区：仓位全量字段 + 现有 SL/TP 列表（触发价/数量/是否机器人单）
   - **改止损**：输入新触发价 → 预览（距离现价百分比、预计风险额）→ 确认 → `POST /v1/operator/orders {action: move_stop_loss}`
   - **改止盈**：编辑 TP 列表（价格+数量）→ 确认 → `{action: replace_take_profits}`
   - **百分比平仓**：快捷档 25/50/75/100 + 自定义滑条（步进 5%）→ 前端按 mirror 行的 `quantity_step`/`min_quantity` 把百分比换算为**绝对 `quantity`** 并预取整（服务端只收绝对数量，review #12；取整后为 0 则禁用提交）→ 确认 → `{action: partial_close}`（100% 走 `close_position`）

安全与可靠性（D4 兜底的客户端实现）：
- 每个写操作生成幂等 ref（复用 `src/utils/id.ts`），失败重试沿用同 ref
- 确认框必须回显：账户、symbol、方向、动作、数量/价格，且**需要用户手动输入数量或滑到底部确认**（不可单击直发）
- 确认流 = `dry_run` 预检（不落 intent）→ 回显 warnings/`would_reject` 全文 → 用户确认 → 正式提交；正式提交的重试才复用同一 `client_ref`（review #11）
- token 仅存 secureStorage；网络错误/401 时禁用写操作按钮并提示重新配置
- 写操作回执 = 响应的 `intent_id`（无 execution job id 可拿，review #12）；提交后轮询 operator status 至终态（intent 过期/节点拒绝/部分成交/执行失败各有 UI 态，review 缺口 #2），终态后再刷新 `/v1/mirror/positions`（镜像滞后于节点执行，不作即时判据）

### 6.2 验收标准

- [ ] 告警功能零回归：现有 5 屏与推送/配对/签名确认行为不变（现有 jest 套全绿）
- [ ] 未配置 tradingApi 时 Trading 区显示引导页，不影响告警区
- [ ] 四账户余额/持仓与 web 面板同时刻一致（人工抽查一次）
- [ ] hedge 双向仓在 PositionsScreen 显示为两行，保护单归属各自正确
- [ ] 真实测试环境完成一次全链路演练并留存记录：改 SL → 改 TP → 50% 平仓 → 100% 平仓，四个动作在 `trade_intents`/`audit_events` 可查（operator 路径不写 operator_commands，review #13）、交易所侧生效
- [ ] would_reject 场景（错误归属引用）演练一次：app 展示警告且默认不执行
- [ ] `typecheck`、`lint`、`test` 全绿；Android release 构建产出 APK

### 6.3 测试标准

单元/组件（jest，`apps/attention-android`）：
- T3-1 百分比→数量换算：25/50/75/100/自定义档，step size 取整、极小仓位取整后为 0 时禁用提交
- T3-2 幂等 ref：同一操作重试沿用同 ref；新操作生成新 ref
- T3-3 确认框：未完成手动确认时提交按钮禁用；回显字段齐全（快照测试）
- T3-4 API client：401 → 写操作禁用态；超时 → 可重试且不重复计数；`warnings`/`would_reject` 解析与展示
- T3-5 PositionsScreen：hedge fixture 渲染两行、保护单归属正确；mirror stale fixture → 黄条
- T3-6 状态隔离：tradingApi 配置读写不触碰 attention 配置命名空间（secureStorage mock 断言 key 前缀）
- T3-7 导航：未配置 → 引导页；配置后 → AccountsScreen；告警深链（现有 linking）不受影响

回归：现有告警相关测试套全量通过（`npm --prefix apps/attention-android test`）。

命令：`npm run typecheck:android && npm run lint:android && npm run test:android && npm run build:android`（monorepo 根）。

---

## 7A. 记账安全与对账保障（D9，回答"会不会影响记账/对不齐账单怎么办"）

### 7A.1 为什么本次升级不会动账

1. **零新写路径**：Phase 1 全部是只读端点；不改任何表 schema、不新增迁移、不新增任何对 `trade_outcomes` / `orders_projection` / `positions_projection` / `execution_events` / `exchange_state_mirror` 的写入代码。记账链路（成交事件 → 投影 → outcomes 物化）一行不碰。
2. **写操作走老路**：app 和面板的平仓/改损/改 TP 全部经由**既有** `POST /v1/operator/orders` → 既有 intent/execution job → 既有事件流入账。与现在用 `v3_trade.py` 手工操作走的是同一条记账路径，口径零变化。
3. **幂等防重复入账**：对账最大的威胁是重复执行（一次点击两次成交）。所有写操作强制幂等 ref（app 复用 `id.ts`，面板同规则），重试沿用同 ref；服务端幂等体系（`intent_idempotency_key` / `semantic_operation_id`）已存在，测试 T1-10 / T3-2 覆盖。
4. **数量以服务端为准**：百分比平仓的数量换算由服务端按交易所过滤器（step size / min notional）落定，app/面板的换算只是预览——避免"app 显示平 0.50、实际成交 0.49"这类账面差。
5. **来源可归因**：app 侧写操作 `client_ref` 前缀 `mobile-`、面板命令 `request_id` 前缀 `dashboard-`。注意落点（review #13）：operator 单笔操作写 `trade_intents` + `audit_events`；`operator_commands` 只记录 `/v1/commands` 全局命令。

### 7A.2 对不齐账单时的处置路径（runbook，按顺序）

1. **先定真相**：`exchange_state_mirror`（=币安直读）是唯一真相层；任何投影/面板/app 数字与它打架，以 mirror 为准。快速核对：`v3_query.py positions` / `orders`，或升级后的 `/v1/mirror/positions`。
2. **看差异清单**：`/v1/reconcile`（升级后有面板页）列出幽灵单（投影有/交易所无）与漏记单（交易所有/投影无）。
3. **查投影是否断更**：`projection_failures` 表有无未解决行、`projection_watermarks` 水位是否停走（0018 迁移引入，历史上 orders_projection 曾静默断更 11 天）。
4. **修复工具**：`scripts/rebuild_orders_projection.py --dry-run` 看差异，确认后 `--apply` 重建（SHARE 锁 + 三项高水位比对，安全）。幽灵投影行（accepted 但交易所无单）参照 2026-08-29 处置：点名 `client_order_id` 且限定 `status='accepted'` UPDATE 为 cancelled——幽灵行不清会连带 owned_order_recovery 崩溃循环与 RESUME 409。
5. **outcomes 对不上**：先查 `trade_outcome_job_runs` 物化水位（>36h 过期），战报/绩效区在水位过期时必须显示 stale 而不是给旧数（验收项）。

### 7A.3 记账相关验收补充

- [ ] 全部 Phase 合并后，跑一次端到端账实核对：测试环境执行 改SL→改TP→50%平仓→100%平仓 后，`v3_query.py positions/orders/outcomes` 与面板、app 三端数字一致，交易所侧成交与 `trade_outcomes` 增量一致
- [ ] 幂等演练：同一平仓请求带同 ref 重发 3 次 → 交易所只成交一次，账面只入一笔
- [ ] Phase 1/2/3 的 diff 审查清单包含一项硬检查：**无任何对记账表的 INSERT/UPDATE/DELETE 新增代码、无新增迁移**（reviewer 逐条确认）

## 7. 横切要求

1. **新鲜度诚实**：任何页面/屏展示交易数据必须同时展示数据龄或 stale 标识；宁可显示"数据过期"也不显示看似新鲜的旧数。
2. **审计完整**：所有写操作 reason 必填（面板既有约束保持），app 侧操作的 `request_id` 前缀统一为 `mobile-`，便于 `audit_events` 归因（对齐 `deploy-*`/`manual-*`/`user-chat-*` 既有约定）。
3. **契约先行**：Phase 1 新端点 schema 落 `packages/contracts/v1/`，并登记进发布产物清单（e2a37f6 债务教训：契约文件缺失曾导致派生 100% 失败）。
4. **只增不改**：既有 `/v1` 响应字段一律只增；面板与 app 各自兼容缺字段的旧后端（灰度期两端版本可能错开）。
5. **不做的事**（本轮明确出界）：告警链路改动；settings import/export UI；release gates / canary / maintenance fence 视图；bridge 内存态 audit/release-gates 改造；`/api/freqtrade` 端点删除；iOS 构建。

---

## 8. 风险与未决问题

| # | 风险 | 处置 |
|---|---|---|
| R1 | app 直连控制面：token 在手机上，等级低于签名确认轨 | 已被 D4/D12 接受；靠 Tailscale + 客户端强确认兜底。若未来要开仓能力，届时必须重新评估改走 attention 签名网关 |
| R2 | mirror 为快照，写操作与展示间存在竞态（看到的仓位可能已变） | 写操作响应后强制刷新；partial_close 以服务端换算为准，前端换算仅作预览 |
| R3 | D12 后 app 持有全权 operator token（可开仓/发全局命令的凭据在手机上） | 用户知情接受（D12）；靠 Tailscale 内网可达 + app 端不提供越界操作入口兜底 |
| R4 | 控制面 read_api.py 已 8600 行，Phase 1 继续增长 | 新端点允许拆子模块（router 挂载），不强制单文件 |
| R5 | 面板与战报数字口径差异（投影 vs mirror vs outcomes） | 每处数字标注来源；验收含同时刻一致性抽查 |
| R6 | alert-personal 与 trader-bot 是两个仓库，契约漂移 | app 端 API 类型定义注明对应 contracts/v1 版本号；Phase 1 合并前 app 不开工 |
| R7 | Headless UI/Tailwind 与存量 styles.css 共存期样式互相污染 | Tailwind preflight 控制引入范围；M2a 先打样一个 Dialog 验证共存，e2e 全量回归作闸门 |
| R8 | `@ant-design/react-native` 对 RN 0.82 新架构兼容未经实证（peer 范围宽但滞后于 RN 版本节奏），且引入 gesture-handler/reanimated 两个原生依赖需重编译 | M3a 第一步打样验证核心组件 + Android release 构建通过；失败则降级"antd token + 自绘"（D11），不阻塞 M3 主线 |

---

## 9. 模块划分与交付顺序（D8）

每个模块 = 一个独立 Codex 派发单元 = 一次独立验收。派发时从本文档摘取对应"设计 + 验收 + 测试标准"，附文件范围与禁止事项（含 §7A.3 记账硬检查）。

| 模块 | 内容 | 仓库/范围 | 依赖 |
|---|---|---|---|
| M1a | 控制面：既有端点补 account_id/保护单真值/nodes 补 release_id+halt_reason | services/control-plane | 无 |
| M1b | 控制面：`/v1/mirror/positions`（含 algo 保护单、filters 字段） + `/v1/reconcile` | services/control-plane | 无 |
| M1c | 控制面：`/v1/outcomes`（KPI 公式冻结表） | services/control-plane | 无 |
| M1e | 控制面：六层溯源端点 + incidents 只读列表 + 命令状态查询确认 | services/control-plane | 无 |
| M0 | Phase 0 断裂修复（命令契约/operator 两段式/pair-lock/Reports 形状适配/Freqtrade 清理） | bridge/apps/dashboard | **M1a**（review #4） |
| M2a | 面板：UI 基座引入（Headless UI + Tailwind 脚手架，先迁 1 个 Dialog 打样） | bridge/apps/dashboard | M0 |
| M2b | 面板：多账号 + 真相层 + reconcile 页 | bridge/apps/dashboard | M1a、M1b、M2a |
| M2c | 面板：保护单真实状态 + 无保护告警 | bridge/apps/dashboard | M1a、M2a |
| M2d | 面板：绩效区 + History 换源 | bridge/apps/dashboard | M1c、M2a |
| M2e | 面板：审计链抽屉 + 舰队健康卡 + incidents 只读表 | bridge/apps/dashboard | M1e、M2a |
| M2f | 战报：新 KPI + 可出报降级三态渲染 | services/report | M1c |
| M2g | bridge 报表收集器接线（真实日报数据源） | bridge/apps/api/app/services/report_snapshot.py | M1b、M1c |
| M3a | app：Trading 配置/导航/AccountsScreen（未实现盈亏由 mirror 聚合） | alert-personal | M1a、**M1b**（review #19） |
| M3b | app：PositionsScreen + PositionDetail 只读部分 | alert-personal | M1b、M3a |
| M3c | app：写操作（改SL/改TP/百分比平仓 + dry_run 确认流 + 幂等 + 终态轮询） | alert-personal | M3b |

- 并行度：M1a/M1b/M1c/M1e 四个后端模块可立即并行；M0 等 M1a；M2b–M2g 在依赖就绪后并行；M3a–M3c 串行
- app 与 jp-bot 面板一致性（D10）在 M3 各模块验收时逐项核对面板对应页
- 每模块完成后 `/codex:review --background`；M3c 必须 adversarial review（关注：幂等、hedge 归属、竞态、记账红线）

### 9.1 部署注意（jp-24 / jp-bot.balen.wang）

- 面板部署在 jp-24，dashboard 登录走 trader-api-1（JWT）；watcher 路径另有 basicauth，两套认证不要混
- **任何触碰 Caddy 的部署动作会瞬断节点→控制面通道，触发全舰队 fail-closed HALT**（2026-08-30 实证）；面板上线若需改 Caddy 路由，操作后必须立即跑 `/srv/trader-staging/resume_race.py` 全舰队恢复
- Phase 1 新契约文件必须登记进发布产物清单（e2a37f6 教训），节点/控制面镜像同步发布
