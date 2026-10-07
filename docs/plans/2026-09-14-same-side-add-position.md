# 同向加仓最小实现方案

日期：2026-09-14。对照 case：account-c BTCUSDT SHORT 0.055，信号 m6901「大饼788加仓个空。防守卡8w。」，intent `748dbcd9-…b66fa61f`。产品澄清：**同向持仓不影响加仓**。

本文只做有边界的代码/运行配置分析与修改方案。未改代码、未改生产、未 RESUME、未下单撤单。上一调查：`docs/reports/2026-09-14-account-c-btc-b66fa61f-contradictory-notify.md`。

本地工作区已有无关改动（Titan 减仓 `--percent`、planner `quantity`/`fraction`、`.live-mirror` 删除等），实现时不得覆盖。

**当前仅完成方案，未实现、未部署、未重放历史信号。**

## Codex审阅后的推荐（优先于下方候选）

2026-09-14 架构验收。本节优先于后文原「推荐」、P1-A / P3-A / P4-A。那些是原候选，含不安全假设，**不得当已通过方案**；实现须按本节校正。

1. **动作**：保留真实 `open_position` 防重守卫，接通明确 `add_position`。上层工具/CLI 可确定性选择动作，但必须在**审批之前**完成；审计同时保留信号意图与实际 action。不只依赖模型遵守技能。禁止控制面/节点把 open 静默改写成 add。
2. **金额与门禁**：加仓是增量名义，并复用全部资金/交易门禁。限额必须核查 **现仓敞口 + 已批准未完成请求 + 新请求**；不得假设一次 `available_balance` 查询能防并发超配。不规定新锁技术，但不能没有预留检查。
3. **保护**：禁止用交易所净仓当机器人保护数量。只管理可追溯机器人数量。本 case 机器人开后手减剩 0.055 可对账确认；额外手动或跨来源数量不得隐式接管。本次新 SL 先覆盖**新增成交量**；旧机器人 SL/TP 保持原数量，直到明确的整笔保护意图。若同来源新 SL 明确用于整笔机器人仓：先验证归属与数量，**先建立足量保护再替换**机器人单。`aos_*` 等手动单绝不动。
4. **仓位状态**：未知 / 过期 / 冲突一律拒绝风险增加。cache 有仓也不能盖过新鲜 venue 冲突。
5. **幂等**：基于来源消息稳定身份。动作从 open 改为 add **不**自动准许新下单。旧 open 已拒且核对零 execution 命令/成交后，才可显式重提交 add。历史 m6901 **不**自动补执行。
6. **来源**：用户已明确同向持仓允许增加。跨来源已有持仓不能作为一概拒绝新增仓的理由；既有来源保护不可被接管。账本不足时标明归属支持缺口。通知改为「已受理/待执行」或合并终态是配套建议，非本切片实现门。

## 结论

1. **不能只放开 `open_position` 的 `position_exists`。** 那是 WP-C 为 8-26 空缓存重复开仓加的 venue 守卫；放开会直接回归。
2. **现有 `add_position` 枚举可复用，现有 operator API 不能直接用。** 规划器有同向 add 快路径，但控制面白名单没有它；cache 空时 add 会 `position_required`，venue 回退只给了管理动作。
3. **接通明确 `add_position`，保留真实 open 防重。** 工具/CLI 在审批前选定动作；保护只覆盖可追溯机器人数量；限额含现仓敞口与已批准未完成占用。详见文首 Codex 节。
4. 本仓是 **机器人开、用户手动减半后的剩余空仓**，0.055 可对账确认，不能当纯手动仓拒绝加仓；额外手动量与 `aos_*` 不得接管或撤销。

## 本 case 状态流与阻断点

```
m6901「加仓空」
  → Hermes skill 铁律 5：不同消息必须独立提交
  → v3_trade.py cmd_open 固定 action=open_position
  → POST /v1/operator/orders
  → 风控 _size_open_order：7000U 过（不看 venue 仓）
  → intent approved + inbox accepted
  → plan_intent_execution → _validate_position
       cache 空 + reconciled KNOWN_OPEN SHORT 0.055
       → OrderDenied("position_exists")     ← 本次唯一执行阻断
  → 零 execution_events / 零交易所 B748dbcd9*
```

仓位归属（调查已核）：2026-09-03 机器人市价开 0.111 SHORT @ 80593.6；2026-09-10 用户 `aos_half_*` 减到 0.055。入场 intent 仍在，不是「查不到入场 ref 的手动仓」。该 0.055 可作机器人可追溯剩余；不得把交易所净仓上多出来的手动/跨来源数量算进机器人保护。

运行配置（调查已核，非本仓库字段）：节点 04:08 重启后 Nautilus `snapshot_positions=False`、`use_instance_id=True`，cache 空；心跳 venue 仍有 0.055。仓库里 `CacheConfig` 不传 `snapshot_positions`（`services/nautilus-node/persistence/nautilus_config.py:329-335`），`use_instance_id` 在 `:166`。**不要靠打开 snapshot_positions 修加仓**。venue 可用于仓位**存在性/冲突**判定；**不得**用 venue 净仓当作机器人保护数量。

## 仅放开 open guard 不可行（证据）

| 证据 | 位置 | 含义 |
|---|---|---|
| 空 cache + venue 同向 → 拒 open | `intent_execution_planner.py:986-999` | 专门恢复 8-26 守卫 |
| 回归测试 | `tests/execution/open/test_intent_execution_planner_reconciled_open.py:55-68` | 放开则红 |
| 反向仓独立 | 同文件 `:107-120`；WP-C `docs/plans/2026-08-28-execution-state-arch-migration.md:90-91` | hedge LONG 不影响开 SHORT，**保持** |
| Titan 新计划仍走 open 准入 | `docs/plans/2026-09-11-titan-two-entry-implementation.md:19` | 独立 open 仍要 `position_exists`；双腿是同一 intent，不走第二次 open |
| 铁律 5 仍要独立提交 | `hermes-profile/skills/trading/v3-trader/SKILL.md:18` | 不同消息不该跳过，但也不该再 `open` |

放开后：本单会当成新开仓打出 7000U，与已有 0.055 叠在同一 SHORT 书上，效果像加仓，但保护接管、8-26 重复开、Titan 独立第二 open 全部失守。

静默把 open 改写成 add 同样不行：open 的幂等键是 `operator|{account}|{client_ref}`（`read_api.py:8308-8316`），add 是 `operator-v2|{account}|{action}|{symbol}|{side}|{client_ref}`；live open gate 只挂在 `action == "open_position"`（`:8378-8408`）；Titan `second_price` 只允许 open（`:7639-7640`）。改写会拆幂等、绕过/漏挂闸门。动作变化也不等于自动准许新下单（Codex §5）。

## 现有 add_position 差在哪

**能复用的**

- 枚举：contracts / DB / planner `ENTRY_ACTIONS` / 节点 inbox / durable 串行提交都认 `add_position`。
- 规划快路径：cache 有同向仓时 add 出与 open 相同的入场 `OrderPlan`（`tests/execution/open/test_intent_execution_planner.py:71-91`）。
- 模拟引擎：cache 有仓时可挂加仓限价（`tests/execution/open/test_intent_execution_strategy_hk.py:70-105`）。
- 下发定量：`_execution_order_plan`（`read_api.py:562-705`）按 `max_notional/price` 算增量数量，管理动作集合不含 add，形状已通。
- 管理动作 venue 回退：`_reconciled_target_position`（`intent_execution_planner.py:806-873`）已能从 KNOWN_OPEN 合成 `PositionSnapshot`。ledger `assess`（`account_execution_ledger.py:136-167`）在 cache 空 + venue 有仓时就是 KNOWN_OPEN。存在性可复用；保护数量不可把该 quantity 当机器人整仓。

**不能直接用的**

| 缺口 | 位置 | 本 case 后果 |
|---|---|---|
| operator 白名单无 add | `read_api.py:5784-5791`；`:7580` 不在列表则 400 | 工具即使改 action 也进不了 |
| 定量/来源/幂等/闸门只认 open | 定量 `:7930-7979`；`client_ref` `:7652`；channel 溯源 `:7608-7726`；live open gate `:8378` | 只加工名单会得到 `max_notional=0` 然后被节点拒 |
| add 无 venue 回退 | `_validate_position` `:1005-1012` 只看 `context.position`（来自 cache，`intent_execution_strategy.py:2759-2764,7352-7356`） | 本 case cache 空 → `position_required` |
| zone 非 batch 走旧函数 | `_entry_position_denial` `:10086-10101`；batch 才调 `_validate_position` `:3535-3539` | 区间加仓同样盲 |
| canary 禁止 add | `_live_canary_intent_denial` `:2868-2869` `canary_open_position_only` | 以后 account-a canary 加仓必拒 |
| 保护数量用整本 cache/venue 仓 | `_protection_quantity` `:3755-3758`（非 batch） | 会把手动/跨来源量算进机器人保护（Codex 已否） |
| 保护仓位只读 cache | `_protection_position` `:6999-7004` | cache 空则不同步保护；有仓也不能盖过 venue 冲突 |
| 新入场挤掉其它来源 stash | `_stage_entry_protection` `:3179-3206` | 9-03 保护所有权被新 intent 抢走（禁止接管） |
| `stop_only` 会撤旧机器人 TP | `_protection_replacement_actions` `:6627-6633`：desired TP 为空 → 其它触发价的 live TP 进 `replace_ids` | 会撤 9-03/9-11 机器人 TP（禁止，直到明确整笔保护） |
| `protection_policy` 节点不读 | 控制面写入 order_plan（`read_api.py:8015-8016`）；strategy 零引用 | `stop_only` 只是记录，不约束撤单 |
| 手动单 | `_live_protection_orders` `:6927-6930` 只收 `^B[0-9a-f]{32}[0-9]{2}$` | `aos_half_sl_*` **本来就不会被撤**，必须保持 |
| 管理归属只认 open 父单 | `read_api.py:6707-6710` | 以后用加仓 ref 做 `--entry-ref` 会对不上；账本不足须标明缺口 |
| Hermes 无 `add` 子命令 | `v3_trade.py:507-527` 只有 `cmd_open` | 上层无法在审批前确定性选 add |
| 风控不看现仓+在途占用 | `governor.py:151-164`；operator `_size_open_order` 只过单次 `available_balance*leverage` | 并发两笔 add 可同时过同一余额（Codex 已否「单次查询即防超配」） |

因此：**复用 add 语义，补齐 operator + 仓位状态机 + 机器人可追溯保护 + 预留检查；不是新开一条平行 API，也不是把 venue 净仓当保护数量。**

## 推荐改法（须按文首 Codex 校正）

实现顺序：节点仓位状态机（含冲突 fail-closed）→ 控制面真正接纳 add（含预留检查）→ 上层 CLI 在审批前选动作。先改 skill 后改服务会 400。

### A. 执行层：接通 add，open 防重不动

文件：`services/nautilus-node/strategy/intent_execution_planner.py` `_validate_position`（约 978-1012）。

- `open_position`：保留真实防重（cache 非零同向或 venue KNOWN_OPEN 同向 → `position_exists`）。**校正**：UNKNOWN / 过期 / CONFLICTED 对风险增加一律拒绝；cache 有仓不得盖过新鲜 venue 冲突。原 WP-C「失明放行」不适用于 add，也不再作为 open 加仓旁路。
- `add_position`：按 **intent 方向对应的 book**（BUY→LONG，SELL→SHORT）判断，不要用 `_first_nonzero_position`（`:10732`，hedge 下会抓错边）：
  - 新鲜 venue 同向 KNOWN_OPEN，或 cache 同向非零且与新鲜 venue 无冲突 → 允许加仓（存在性，不是把 venue 净量当保护量）
  - KNOWN_FLAT → `position_required`
  - UNKNOWN / 过期 / CONFLICTED → 拒风险增加
  - 同品种反向仓：不拦（已有 hedge 测试）
- `_zone_ladder_order_plans` 非 batch 分支改调 `_validate_position`，不要再走 `_entry_position_denial`。
- **不要**在执行层把 open 改写成 add。
- **不要**为加仓去改 Nautilus `snapshot_positions`。
- **不要**把 venue/cache 净仓写入保护数量。

节点已有：`_build_reconciled_state` `:1069-1125`（心跳 30s 内 mirror，无新 I/O）；入场 durable 串行 `:2681-2696`；同 symbol 在途确认冻结 `_symbol_open_freeze_denial` `:7187-7201`。这些不能替代控制面的「现仓 + 已批准未完成 + 新请求」预留检查。

### B. 控制面：增量 add + 全部门禁 + 预留检查

文件：`services/control-plane/api/read_api.py` operator 入口。

- `_OPERATOR_ACTIONS` 加入 `add_position`。
- 对 add 复用 open 的全部资金/交易门禁：`client_ref`、channel 溯源、live open gate、`protection_policy` 校验、语义去重（已含 add，`:8425`）。
- **名义 = 增量**，不是目标总额。本单 7000U 是再加 7000U。
- **校正**：不得假设 `_size_open_order` 的单次 `available_balance*leverage`（`:7174-7181`）能防并发超配。审批前必须把现仓敞口、已批准未完成入场、本请求一并计入限额。不规定新锁实现，但不能没有预留检查。
- 审批前完成动作选择：上层已提交 `add_position` 才按 add 批；若提交仍是 `open_position` 则走真实 open 防重，**不改写**。审计同时记下信号意图（加仓）与实际 action。
- 幂等按来源消息稳定身份，不因 action 从 open 变成 add 就新开一张单。旧 open 已拒且零命令/成交后，才允许**显式**重提交 add。历史 m6901 不自动补。
- `entry_batch` / `second_price` 仍只允许 `open_position`。
- 管理 `--entry-ref` 解析把父 action 扩成 `open_position|add_position`（`:6707`）。账本不足以证明归属时标明缺口，不隐式接管。

### C. 上层工具/CLI：审批前确定性选动作

文件：`hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py`、`SKILL.md`。

- 新增 `add` 子命令，payload `action=add_position`。
- CLI/工具根据 mirror **在调用审批接口之前**选择 `open` 或 `add`；技能文案是辅助，**验收不得只靠模型守铁律**。
- 同向已有仓发 add 不得发 open；无同向仓才 open。反向仓按现 hedge 规则。跨来源已有仓 **不能**当成一概拒绝新增的理由（用户已允许同向增加）；也不得因此去动既有来源保护。
- 同一来源消息身份：被拒 open 不得靠改 action 自动变成新准许。核对零命令/成交后才能显式 add。本次 m6901 不重放。
- 铁律 13「无入场 ref 当手动仓不动」不把本 case 0.055 当成纯手动；额外无法对账的手动量仍不纳入机器人保护。

### D. 保护：只管理可追溯机器人数量

本单 `stop_only` + SL 80000。硬约束：

- 任何路径不得撤销非 `^B[0-9a-f]{32}[0-9]{2}$` 订单（`aos_half_sl_1789061188` 等）。
- 禁止用交易所净仓（0.055+其它）当机器人保护数量。
- 本次新 SL **先覆盖本加仓新增成交量**；9-03/9-11 旧机器人 SL/TP **保持原数量**，直到出现明确的整笔机器人仓保护意图。
- 若同来源新 SL 明确用于整笔可追溯机器人仓：先验证归属与数量，先挂足量新保护，再替换旧机器人单。不得先撤后挂。
- 额外手动或跨来源数量不得隐式接管。`stop_only` 不得把旧机器人 TP 放进 `replace_ids`。
- 禁止 add 的 `_stage_entry_protection` 丢掉其它来源 stash 再按空 TP 收敛（`:3179-3206` + `:6627-6633`）。

## 原候选（待实现校正，非通过方案）

以下原表保留作对照。标「原候选」的不得直接实现。

### P1 新 SL / `stop_only` 如何作用剩余仓

Binance hedge 同账户同 symbol 同向只有一本净仓。本仓 0.055 混有机器人开仓与手动减仓。

| 方案 | 内容 | 状态 |
|---|---|---|
| P1-A | 新 SL 按 **venue 净仓**挂一条机器人 SL；不撤手动 SL；`stop_only` 保留旧 TP | **原候选，已否。** 用交易所净仓当机器人保护数量不安全 |
| P1-B | 新 SL 只罩 **本加仓成交量**；旧机器人 SL/TP 保持原数量 | 方向接近 Codex §3；须加上「可追溯机器人数量 / 先建后换 / 手动绝不碰」 |
| P1-C | 按来源拆子仓保护 | 超范围；账本不足时标明归属缺口即可 |

实现校正：保护数量 = 可对账机器人剩余（本 case 手减后 0.055 可确认）± 本加仓成交，**不是** venue 净仓。cache 空时用 ledger 判断存在与冲突，仍不得把 `assessment.quantity` 整段写进 SL。

### P2 谁做 open→add 规范化

| 方案 | 内容 | 状态 |
|---|---|---|
| P2-A | 只在 Hermes 显式 `add`；控制面/节点不改写 | **原候选不完整。** 工具/CLI 须在审批前确定性选动作；不只依赖模型守技能 |
| P2-B | 控制面若发现同向仓，把 open 改成 add 再审批 | 否。拆幂等与闸门 |
| P2-C | 节点规划时把 open 当 add | 否。8-26 重复开变成「成功加仓」 |

校正：上层确定性选 action → 控制面按所提交 action 审批并审计「信号意图 + 实际 action」→ 节点执行该 action。

### P3 在途同向入场单

| 方案 | 内容 | 状态 |
|---|---|---|
| P3-A | 不同入场价允许并行；靠 `available_balance` 与 durable 串行限幅；不做预留 | **原候选，已否。** 单次余额查询不能防并发超配 |
| P3-B | 有任一未成交同向入场就拒新 add | 过严，误伤分批加仓 |

校正：并行加仓可以，但审批必须计入已批准未完成请求。不规定新锁，不能无预留检查。

### P4 风险是否计入现仓

| 方案 | 内容 | 状态 |
|---|---|---|
| P4-A | 只算增量名义；现仓占用只通过一次 `available_balance*leverage` | **原候选，已否。** 漏现仓敞口与在途占用 |
| P4-B | 把现仓在新 SL 上的损失并入同一 `max_risk_fraction` 新公式 | 超出本 case，不单独立项 |

校正：增量金额 + 复用全部门禁 + 限额 = 现仓敞口 + 已批准未完成 + 新请求。不要顺手改 hermes-worker `governor.py` 投影路径，除非同一预留语义必须共用。

### P5 canary 与 add

`canary_open_position_only` 明确拒绝 add。把 live open gate 挂到 add 上，避免白名单放开后绕过 canary。canary 期间禁止 add（保持现状），fleet_complete/`normal` 才允许。

### P6 多来源同书

| 方案 | 内容 | 状态 |
|---|---|---|
| P6-A | 同频道才允许 add；保护按 P1-A 作用在净仓上 | **原候选部分作废。** 跨来源已有仓不能一概拒绝新增；保护更不能按净仓接管 |
| P6-B | 同书多来源各挂各的 SL/TP 且数量严格等于该来源剩余 | 账本不足则标明缺口，不在本切片做完整子仓 |

校正：同向允许增加；既有来源保护不可接管；手动单不动。

## 最小测试矩阵

本 case 回归优先；反向仓、Titan 双腿、8-26 不得坏。期望以文首 Codex 为准。

| # | 场景 | 期望 |
|---|---|---|
| T1 | cache 空 + venue SHORT 0.055 + `open_position` SELL | 仍 `position_exists`（8-26） |
| T2 | cache 空 + venue SHORT 0.055 + `add_position` SELL 限价 78800 | 产出入场 OrderPlan，非 reduce-only |
| T3 | cache 空 + venue flat + add | `position_required` |
| T4 | venue 过期 / UNKNOWN / CONFLICTED + add 或其它风险增加 | 拒绝；cache 有仓也不得盖过新鲜 venue 冲突 |
| T5 | venue LONG only + add SELL / open SELL | add 拒；open SHORT 允许（hedge 不扩大） |
| T6 | cache 已有同向且与新鲜 venue 一致 + add | 与现测试一致，出限价单 |
| T7 | operator POST add 无 client_ref / 无 channel 溯源 | 400 |
| T8 | operator add 增量名义，复用全部门禁；限额含现仓敞口 + 已批准未完成 + 本请求 | 并发第二笔不得只靠同一 `available_balance` 快照过关 |
| T9 | operator 白名单：add 200；`second_price`+add 400 |
| T10 | 同一来源消息身份重放 | 不因改 action 自动新开；replay 或拒，不第二张单 |
| T11 | 被拒 open、已核零命令/成交后 **显式** 再提交 add | 允许；历史 m6901 **不**自动补执行 |
| T12 | `stop_only` add 保护 | 不撤 `aos_*`；不撤/不改旧机器人 SL/TP 数量；新 SL 只罩新增成交量 |
| T13 | 保护数量 | 可追溯机器人数量（本 case 可对账 0.055 不强制写入新 SL）；**不是** venue 净仓 |
| T14 | Titan `entry_batch` 仍要求 `open_position`，第一腿成交不把第二腿当独立 open |
| T15 | zone add 走 `_validate_position`，空 cache+新鲜 venue 同向可通过存在性检查 |
| T16 | CLI 在审批前发 `add_position`；误发 open 不得被服务端改写 |
| T17 | HALTED/REDUCING 拒 add（已有 planner 测试 `:210-215`） |
| T18 | canary_only 账号 add 无 permit | 拒（P5） |
| T19 | 审计 | 同时有信号意图（加仓）与实际 `action=add_position` |

通知文案改为「已受理/待执行」或合并终态是配套建议，不挡本切片代码验收。旧 TP 数量卫生（0.111 与 0.055 叠）不在本切片。

## 验证命令

实现后在本机跑（分包，禁止全树一把）：

```bash
.venv-arch/bin/python -m pytest \
  tests/execution/open/test_intent_execution_planner.py \
  tests/execution/open/test_intent_execution_planner_reconciled_open.py \
  tests/execution/manage/test_intent_execution_planner_reconciled_manage.py \
  tests/execution/open/test_intent_execution_strategy_hk.py \
  tests/execution/open/test_entry_batch_strategy.py \
  -q

.venv-arch/bin/python -m pytest \
  tests/control-plane/api/test_operator_protection_and_dedup.py \
  tests/control-plane/api/test_live_safety_gates.py \
  tests/test_v3_trade_routing.py \
  -q
```

保护/canary 相关新测试放在现有 `tests/execution/manage` 或 `tests/execution/open`，保持 unittest 风格与 WP-C 一致。

上线前只读复核（不 RESUME、不下单、不重放 m6901）：

```text
jp-24: node-c 心跳 BTCUSDT SHORT 仍为 0.055；intent 748dbcd9 仍 rejected；
execution_events / B748dbcd9* 仍空；aos_half_sl_* 仍在。
```

## 部署门禁与回滚

- 按 `docs/runbooks/2026-08-18-deployment-gate-matrix.md`：preflight 不碰 A-D；硬闸失败 **不得进入停机窗**，保持原版本。
- AGENTS：门禁必须全部完成于停节点之前；**不得**因本方案 RESUME。
- 发布顺序：节点（A+D）→ 控制面（B，含预留检查）→ CLI/技能（C）。只发 skill 会 400；只发控制面而无节点状态机，本 case cache 空仍 `position_required`。
- 回滚：先回 CLI（恢复只发 open，节点继续拒 `position_exists`，安全）；再回控制面；最后回节点。有在途 add intent 时，旧节点会拒，**不要**为回滚去补单、改 ref 或重放 m6901。
- `/tmp` 是 tmpfs；staging 用 `/srv/trader-staging`。
- 控制面改动需重启 operator-query 等 systemd；文件落盘 ≠ 生效。

## 明确不做

- 不改代码/生产/Redis、不 RESUME、不补 m6901、不撤 `aos_*`、不收敛旧 TP 数量。
- 不打开 `snapshot_positions` 当主修复。
- 不静默 open→add。
- 不用交易所净仓当机器人保护数量。
- 不把反向仓、one-way、Titan 双腿、8-26 守卫纳入「放开」。通知文案是配套建议。
- 不新建完整子仓账本、不改 `contracts/` 枚举语义（add 已存在）。
- 不覆盖工作区已有 Titan 减仓 / planner fraction 改动。

## 实现入口（按文首 Codex，非原 P1-A/P3-A/P4-A）

| 层 | 文件 |
|---|---|
| 规划 | `services/nautilus-node/strategy/intent_execution_planner.py` |
| 保护/仓位视图 | `services/nautilus-node/strategy/intent_execution_strategy.py` |
| ledger（只读复用，原则上不改） | `packages/execution-domain/execution_domain/account_execution_ledger.py` |
| operator | `services/control-plane/api/read_api.py` |
| CLI/技能 | `hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py`、`SKILL.md` |
| 测试 | `tests/execution/open/*`、`tests/execution/manage/*`、`tests/control-plane/api/*`、`tests/test_v3_trade_routing.py` |
