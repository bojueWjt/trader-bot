# 执行状态架构迁移 v1 —— 契约规格（agent team 唯一真源）

背景：2026-08-25~27 ATOM 事故合并 review（Claude × Codex）结论落地第一批。本文档是所有工作包的接口契约与验收标准；实现与测试并行开发，以本文档为准，不得私自变更接口。分歧一律按本文档裁定，文档缺陷记录在输出报告里而不是自行发挥。

## 全局约束（每个 agent 必须遵守）

- 测试运行器：`.venv-arch/bin/python -m pytest <包目录> -q`。**必须分包跑**（`tests/control-plane`、`tests/order_management tests/execution`、`tests/contracts`、`tests/hermes` 各自独立跑），禁止全树一把跑（存在跨包 sys.path 污染，属已知存量问题，本次不修）。
- 存量失败基线（4 个，不许修也不许恶化；你的改动后这些之外不得新增失败）：
  - tests/order_management/execution/test_intent_execution_planner_om4.py 的 3 个用例
  - tests/order_management/risk/test_position_sizing.py::test_fixed_risk_sizing_uses_smallest_notional_cap
- 禁止触碰：`hermes-profile/`、`services/report/`、`eval/`、`.deploy-scratch/`、`engine/`、`.live-mirror/`、`.codex/`、`container-patches/`、`.worktrees/`、任何远程主机/线上环境。禁止 git commit/push（由协调者统一提交）。
- 代码风格随现有文件；不做顺带重构；`intent_execution_strategy.py`（10k 行）内改动保持最小 diff。
- 新增 SQL 迁移放 `db/migrations/`，命名 `0018_*.up.sql` / `.down.sql` 起（多个迁移按包分配的编号，见各 WP）。down 必须真正可逆。

## WP-A：P0 投影通道修复（编号 0018）

背景 bug（已线上证实）：`services/control-plane/api/read_api.py:2790` 调用 `OrderProjectionReducer().apply_event(conn, event, manage_transaction=False)`，但 reducer 签名是 `apply_event(self, conn, event)`（`services/control-plane/order_management/order_reducer.py:33`）——每次调用抛 TypeError，被 `read_api.py:2866-2871` 的 savepoint 裸 except 吞掉且事件照常 ACK。后果：orders_projection 自 8-10（account-b）/8-21（account-a）停更。

交付：
1. `OrderProjectionReducer.apply_event(self, conn, event, *, manage_transaction: bool = True)`：True 保持现行为（内部 `with transaction(conn)`）；False 时不开启/提交事务，直接在调用方事务内执行。幂等、返回值语义不变。
2. `post_node_events` 的 savepoint 异常路径：保留"原始事件先落库、坏 payload 不拖垮批次"语义，但派生失败必须（a）rollback 到 savepoint 后向新表 `projection_failures` 写一行（在外层事务内、savepoint 之外），（b）结构化日志输出 event_id + 异常。不允许再有无记录的静默吞噬。
3. 新表（迁移 0018_projection_reliability）：
   - `projection_failures(id uuid pk default gen_random_uuid(), event_id text not null, account_id text not null, projector text not null, error text not null, created_at timestamptz not null default now(), resolved_at timestamptz)`
   - `projection_watermarks(account_id text not null, projector text not null, last_event_id text not null, last_event_ts timestamptz not null, updated_at timestamptz not null default now(), primary key(account_id, projector))`
4. 每次成功派生后 upsert watermark（projector='orders'；position hint 路径 projector='positions'）。
5. 重放工具 `scripts/rebuild_orders_projection.py`：从 execution_events 按 account 重建 orders_projection，默认 dry-run 输出 diff 概要，`--apply` 才写；参照 `scripts/rebuild_positions_projection.py` 的结构。
6. 权限：迁移里为相关角色补 GRANT（参照 0017 迁移的做法）。

测试（tests/control-plane/api/ 与 tests/order_management/projections/）：
- 回归主测试：经 `post_node_events` 端点投递一个 OrderAccepted/OrderFilled 事件 → orders_projection 出现/更新行（这是对 live bug 的直接回归）。
- 坏 payload：原始事件落库且 ACK，projection_failures 写入一行，批内其他事件正常派生。
- watermark 随成功派生前进；失败不前进。
- 重放工具：construct events → dry-run 报差异 → --apply 后投影与期望一致。

## WP-B：AccountExecutionLedger 领域核心（无迁移）

位置：`packages/execution-domain/execution_domain/account_execution_ledger.py`（纯领域，无 I/O、无 DB、无网络）。已有 `ownership_ledger.py` 保持不动（粒度不足，后续版本再合并）。

契约：
```python
class PositionState(str, Enum):
    KNOWN_FLAT = "known_flat"
    KNOWN_OPEN = "known_open"
    UNKNOWN = "unknown"
    CONFLICTED = "conflicted"

@dataclass(frozen=True)
class BookKey:
    account_id: str
    instrument_id: str          # 例 "ATOMUSDT-PERP.BINANCE"
    position_side: str          # "LONG" | "SHORT"（hedge mode 一等公民）

@dataclass(frozen=True)
class PositionAssessment:
    state: PositionState
    quantity: Decimal           # 交易所侧净量（KNOWN_* 时有效，否则 0）
    venue_fresh: bool
    detail: str                 # 人可读判定依据

class ReconciledExecutionState:
    @classmethod
    def build(cls, *, account_id: str,
              venue_snapshot: dict | None,      # exchange_state_mirror.payload 同构：positions/open_orders/algo_orders，币安原生 symbol（如 "ATOMUSDT"）
              venue_fetched_at: datetime | None,
              cache_positions: Iterable[Any],   # PositionSnapshot 同构（instrument_id, side, quantity, position_id）
              now: datetime,
              freshness_window: timedelta = timedelta(seconds=30)) -> "ReconciledExecutionState": ...
    def assess(self, book: BookKey) -> PositionAssessment: ...
    def bot_open_order_client_ids(self, book: BookKey) -> tuple[str, ...]: ...

def semantic_operation_id(account_id: str, source_message_id: str,
                          action: str, book: BookKey, revision: int = 0) -> str:
    """sha256 十六进制。source_message_id 先剥离末尾 -e\\d+ 派生后缀取 base。"""
```

判定规则（assess）：
- venue_snapshot 为 None 或 venue_fetched_at 超出 freshness_window → UNKNOWN。
- venue 新鲜：该 book（symbol+position_side 匹配，instrument_id "X-PERP.BINANCE" ↔ venue symbol "X" 的映射自己实现并测试）仓位非零 → KNOWN_OPEN（quantity=venue 量）。cache 同 book 非零但数量差异 >1% 时仍 KNOWN_OPEN 但 detail 记录漂移。
- venue 新鲜且该 book 为零：cache 也为零/缺失 → KNOWN_FLAT；cache 非零（幻影仓征兆）→ CONFLICTED。
- 机器人订单识别：clientOrderId 匹配 `^B[0-9a-f]{32}[0-9]{2}$`。

测试（tests/execution/ 下新建 test_account_execution_ledger.py，纯 unittest 风格，参照 tests/execution/open 的 sys.path 处理但指向 packages/execution-domain）：hedge 双向同 symbol 独立判定；四态各自路径；新鲜窗边界；幻影仓 CONFLICTED；ATOM 事故形态（cache 空 + venue 有仓 → KNOWN_OPEN）；semantic_operation_id 对 `-e1`/`-e2` 后缀归一、revision 区分。

## WP-C：planner/strategy 消费 ReconciledExecutionState

改动点 1 —— `services/nautilus-node/strategy/intent_execution_planner.py`：
- `PlannerContext` 增加可选字段 `reconciled_state: Any | None = None`（保持向后兼容默认 None）。
- `_select_target_position`（约 line 642）与 `_validate_position`（约 line 793）新逻辑，仅当**缓存视图为空**时启用回退：
  - 管理动作（replace_take_profits/move_stop_loss/close 等）：assess=KNOWN_OPEN → 用 venue 数据合成 PositionSnapshot（position_id 取 `f"{instrument_id}-{side}"` 与现网命名一致）继续规划；UNKNOWN → `OrderDenied("position_state_unknown", ...)`；CONFLICTED → `OrderDenied("position_state_conflicted", ...)`。原 `position_required` 语义只在 KNOWN_FLAT 时给出。（与旧行为同等阻断度：旧行为在缓存空时一律拒 position_required，此处只是把拒因说清楚并在 KNOWN_OPEN 时放行——净效果是更宽松。）
  - 开仓：assess=KNOWN_OPEN → `OrderDenied("position_exists", ...)`（用交易所证据恢复空缓存丢失的既有 position_exists 守卫，堵 8-26 重复加仓的 fail-open）；UNKNOWN/CONFLICTED → **回落旧行为放行**（2026-08-28 操作者指令：自有账户、只提醒不阻断——证据失明不得成为新的拦截理由）；KNOWN_FLAT → 放行（现行为）。
  - 缓存有非零仓位时行为与现在完全一致（快路径不变），reconciled_state=None 时行为与现在完全一致。
- target_position_id 匹配失败但 assess=KNOWN_OPEN 且 requested side 一致时，视为同一 book 匹配成功（重启后 position_id 变体问题）。

改动点 2 —— `services/nautilus-node/strategy/intent_execution_strategy.py`（最小 diff）：
- 在构建 PlannerContext 处（约 line 2686）注入 `reconciled_state`，用**已有**的 fresh exchange evidence 快照（管理动作 gate 约 line 2557-2592 已在获取）+ `ReconciledExecutionState.build(...)`。找不到新鲜快照时传 None（等价现行为）。sys.path 上 packages/execution-domain 已在节点镜像可用（与 ownership_ledger 同包）。

测试（tests/execution/manage/、tests/execution/open/ 新增文件，不改现有用例）：
- ATOM 回归：cache 空、venue 快照含 596.48 LONG、intent 带 target_position_id=ATOMUSDT-PERP.BINANCE-LONG 的 replace_take_profits → 产出 4 张 MIT 止盈 OrderPlan（不再 position_required）。
- 重复加仓回归：cache 空、venue 有同向仓 → open_position 拒 position_exists。
- venue 过期 → 管理动作拒 position_state_unknown；开仓拒 position_state_unknown。
- CONFLICTED 路径；reconciled_state=None 完全等价旧行为（回归保护）。

## WP-D：启动对账范围解除白名单锁死（与 WP-C 同一 agent 组）

改动 —— `services/nautilus-node/persistence/nautilus_config.py::build_live_exec_engine_kwargs`：
- 新增可选参数 `venue_instrument_ids: Iterable[str] | None = None`；最终 `reconciliation_instrument_ids = sorted(set(risk keys) | set(venue_instrument_ids or ()))`（仍排除 DEFAULT_LIVE_ENTRY_NOTIONAL_KEY）。
- `services/nautilus-node/app/node.py` 装配处：若启动时可获得交易所快照（mirror/evidence 任一来源，包含非零仓位 + 挂单 + algo 单的 instrument），把这些 instrument id（转成 "SYMBOL-PERP.BINANCE" 形态）传入；取不到则传 None 并打 WARNING 日志（不得让启动失败）。
- 不变量测试：构造 venue 有 ATOM 仓而 risk 键无 ATOM → 对账集合包含 ATOM。

测试放 tests/nautilus/ 或 tests/execution/（跟现有 nautilus_config 测试同处，先找到它们再落位）。

## WP-E：合同保护完备性闸门 + 语义去重（编号 0019，若需要迁移才建）

改动点 1 —— `packages/contracts/v1/approved_trade_intent.v1.json`：order_plan 增加可选字段 `protection_policy`，enum `["complete","stop_only","deferred","waived"]`。不设 required（向后兼容）。同步一份 valid example。

【2026-08-28 操作者指令修订：自有账户，WP-E 全部闸门降级为"提醒不阻断"——除格式/枚举类输入校验外，不允许任何 422/409 拦截交易指令。】

改动点 2 —— 控制面 operator 开仓路径（`services/control-plane/api/read_api.py` 约 8060-8239 的 operator_order 创建）验证规则：
- open_position/add_position 且 `take_profits` 为空数组时：无 `stop_loss` 且无 `protection_policy` → **放行**，自动记录 `protection_policy="deferred"` 并在响应 `warnings` 数组中提示；有 `stop_loss` 无 `protection_policy` → 自动填充 `protection_policy="stop_only"` 并在响应中回显；`protection_policy` 存在则校验枚举（非法枚举值仍 422——这是输入格式校验不是交易限制）。
- take_profits 非空 → 不受影响。
改动点 3 —— hermes-worker 确定性 backstop（`services/hermes-worker/worker.py` 现有 backstop 附近）：open 决策 take_profits 为空且无 stop_loss 时，将保护缺口注记进 ambiguity_reasons（**不置 ambiguous、不拦截 intent**，纯审计注记）。prompt.py 只允许加一行中性说明（可选，不改变既有输出 schema）。

改动点 4 —— 语义去重（控制面 operator 开仓路径）：创建 open/add intent 前查询同 account+instrument 下 status='approved' 且 valid_until 未过期的同 action intent；若存在一条 entry 价格相同（Decimal 相等比较）的活跃 intent → **照常创建**，响应 `warnings` 数组含 `duplicate_open_intent` 警告与已存在 intent_id；请求体显式 `"allow_duplicate": true` 时不出警告。8-25 的 1.55 与 1.459 两笔属不同价格，无警告——测试要覆盖。

测试（tests/contracts/ + tests/control-plane/api/ + tests/hermes/）：schema 校验通过/拒绝矩阵；operator 端点 422/409/自动填充路径；不同价格两笔放行；allow_duplicate 旁路；hermes backstop 标记 ambiguous。

## 完成定义（每个 WP）

1. 实现与测试都落盘；对应包测试全绿（存量 4 失败除外）。
2. 输出：changed files 清单、跑过的命令与结果、偏离契约之处及理由、遗留风险。

## 批次 1.1 修复清单（2026-08-28 Codex 交付对抗 review 后，逐条已由协调者核实）

裁定记录：P0-4（证据失明时开仓放行）为 2026-08-28 操作者指令的既定产品决策，维持现状不修。以下按代码域分三组：

### 组 CP（控制面）
1. **P0-1 生产角色授权缺口**：`trader_v3_event_ingest` 对 `order_events` 零授权（0012 只给了 operator_query），reducer 却要 `SELECT 1 FROM order_events`（order_reducer.py:156）和 `INSERT INTO order_events`（:264），`record_reconciliation_finding` 还要写 `reconciliation_findings`。修 0018 迁移（up 补 `GRANT SELECT, INSERT ON order_events TO trader_v3_event_ingest` 与 `GRANT SELECT, INSERT ON reconciliation_findings TO trader_v3_event_ingest`，down 对应 REVOKE；先核对 reducer 全部表访问面再定授权清单）。**必交测试**：以生产角色 `trader_v3_event_ingest` 连接（参照 tests/control-plane/api/test_control_plane_role_isolation.py 的角色连接方式）走 post_node_events 投递订单事件 → orders_projection 出行、projection_failures 零记录。
2. **P0-2 并发首事件 UUID 竞态**：`_upsert_order_projection` 的 ON CONFLICT DO UPDATE 不回传已存在行的 order_projection_id，冲突方随后用自己生成的 UUID 写 order_events → FK 失败。修法：upsert 加 `RETURNING order_projection_id`，reducer 后续 `_insert_order_event` 与返回值一律用回传的 id。**测试**：预插一行不同 UUID 的投影行，构造 current=None 路径调用，断言 order_events 用的是已存在行的 UUID 且无 FK 异常。
3. **P1-2 watermark 可回退**：repository.py 的 watermark upsert 无单调守卫。修：ON CONFLICT DO UPDATE 加 `WHERE (EXCLUDED.last_event_ts, EXCLUDED.last_event_id) >= (projection_watermarks.last_event_ts, projection_watermarks.last_event_id)`。**测试**：先进后退两次 upsert，断言不回退。
4. **P2-2 protection_policy 节点下行被剥离**：read_api.py 节点拉取转换器的元数据白名单（约 :527-552）缺 `protection_policy`，补入。**测试**：含该字段的 intent 经节点拉取路径后字段保留。

### 组 RT（重放工具 scripts/rebuild_orders_projection.py）
5. **P0-3 高水位检查对回填事件失明**：shadow 构建后 commit、apply 只比对全局 max(ts_event,event_id)——窗口期插入 ts_event 更旧的事件不改变 max，检查通过后该事件的投影效果被覆盖擦除。修：shadow 构建时记录 Order% 事件的 `(COUNT(*), max(created_at, event_id))`，apply 在 SHARE 锁下重查并比对全部三项，不一致即 abort。**测试**：build 与 apply 之间插入 ts_event 回填事件 → RuntimeError。
6. **P1-1 apply 在真实历史库上被 FK 阻断**：DELETE+INSERT 换新 UUID，而 order_events/order_links/protective_orders_projection/reconciliation_findings 都引用旧 projection UUID。修：shadow 行按 (account_id, client_order_id) 与现存 orders_projection JOIN 复用已有 UUID；apply 改为逐行 upsert + 只删除 shadow 中不存在且无引用的残留行（有引用的残留行保留并 WARNING 列出）。**测试**：被 order_events 引用的行经重建后 UUID 不变、FK 不断。

### 组 NP（节点/planner/prompt）
7. **P0-5 target_position_id 跨 instrument 命中**：`_target_position_id_side` 只看 -LONG/-SHORT 后缀，`_reconciled_target_position` 用 intent 自身 instrument 评估——ATOM intent 带 `BTCUSDT-PERP.BINANCE-LONG` 会操作 ATOM 仓。修：剥后缀后的前缀非空且 != instrument_id → 返回 `OrderDenied("position_required", str(target_position_id))`（与 legacy 无匹配语义一致）。**测试**：跨 instrument target id → position_required，绝不合成仓位。
8. **P1-4 hedge 单侧缓存跳过回退**：`_select_target_position` 只在 instrument 级 positions 为空时启用回退；cache 只有 LONG、管理 SHORT 时直接 position_required。修：请求 book（requested_side 或 target id side）在 cache 无匹配且 reconciled_state 存在时，对该 book 走回退评估。**测试**：cache LONG + venue SHORT + 管理 SHORT intent → 按 venue 合成继续。
9. **P1-7 启动路径可致命**：node.py `_startup_venue_instrument_ids` 的 mirror 回退会调 `mirror.refresh()`，其 409 处理直接 `_trigger_fatal_fence`（进程退出，外层 try/except 无效），且该回退只取订单漏仓位。修：整段删除 mirror 回退，仅保留 evidence-provider 主路径 + WARNING 降级。**测试**：mirror 存在但 provider 不可用时返回 None 且不触发 refresh。
10. **P1-9 prompt 把提醒升级为阻断**：prompt.py 新增行指示无 SL/TP 的 open 置 `ambiguous=true`（→ governor needs_review 阻断），违反操作者指令。改写为：记录缺口进 ambiguity_reasons，明示"仅注记，不因此单独置 ambiguous/needs_review"。同步相关 prompt 内容测试。

### 批次 2 债务（本轮不修，记录在案）
- P1-3 批内后续异常回滚先前 raw event（节点 spool 整批重试可自愈，at-least-once 兜底）。
- P1-5 move_stop_to_entry 在回退路径缺 entry_price（现状：明确拒因 position_entry_price_required，无静默危害）。
- P1-6 管理 gate（mirror）与 ledger（provider cache）双状态源不一致（最坏回到旧 position_required，不劣化；统一状态源属批次 2 架构项）。
- P1-8 venue 并集未进 instrument provider / scoped reconciliation 仍以 cache 为种子（WP-D 只扩了对账配置面；真正的恢复重做属批次 2 第三大项）。
- P1-10 replay 响应与 hermes CLI/canary 适配器丢 warnings（hermes-profile/ 属禁区未动；控制面主响应已带）。
- P1-11 `_OPERATOR_ACTIONS` 无 add_position（端点历史上就不支持，非本次回归）；并发同价去重竞态（现为纯提醒，漏警告无资损面）。
- P2-1 SHORT quantity 取绝对值与契约"净量"措辞不一致（对齐文档措辞即可）。
- P2-3 测试未覆盖生产装配/并发模型（P0-1 修复自带角色级测试，其余记债）。

## 部署后新增债务（2026-08-28/29 部署与恢复实战产出，按危害排序）

1. **引擎层缓存失明（批次 2 置顶）**：Nautilus RiskEngine 的 reduce-only 校验只看本地缓存——缓存失明时止损"先撤后挂"替换 = 撤旧成功+挂新被引擎拒 → 保护空窗（SNDK 空单 1.12 张裸奔实案 2026-08-28T17:02，planner 层证据回退无法覆盖）。修复方向：引擎层校验数据源统一，或保护替换改先挂后撤（7 月 BTC 裸奔同族课题）。
2. **intent 状态覆写循环 bug（必修）**：节点消费循环把 approved 的 durable 埋伏单 intent 反复打回 rejected（account-b/c RESUME 双双复现）；57a984d 只修了 replay ack 路径，消费侧路径未修。当前过闸依赖竞速（UPDATE→亚秒 RESUME）。
3. **部署流水线结构债**：maintenance fence 心跳窗 DB 硬限 1-60s 与"节点已停"恢复态自相矛盾（bootstrap_stopped 分支 fence acquire 必死且不 die，读状态文件才炸）；同 release 重跑走 replay 模式要求从未产出的签名 manifest（cp 空变量崩）；rollout abort 记录失败留下三代混杂状态需手工归位 RELEASE_MANIFEST.json。
4. **宿主配置漂移无同步机制**：/etc/caddy/Caddyfile 与仓库源漂移（orders 路由缺失=owned_order_recovery 404 根因）；/srv/trader-v3/packages/contracts/v1/ 缺 order_state.v1.json（event-ingest 派生 100% 失败,新可观测层首案抓获）；合约 JSON 在部署体系中无载体。
5. **恢复物料缺陷**：post-migration-recovery 的 recreate.sh 从现容器继承 env（首代缺陷自续）且漏 NAUTILUS_HEALTH_PORT；payload 登记表两次漏 import 闭包成员（idempotency、owned_order_recovery），需要把"镜像深度 import 验证"（import app.node）固化为 execute 硬门。
6. **caddy reload 缺陷**：reload 触发主进程退出（admin EOF），运维一律用 restart。

已完成的修复性事实（同窗口）：orders_projection 全量重放修复已 apply（951→1492 行,542 缺失/746 状态/205 成交量矫正,watermark 五路就位）；16 条 projection_failures（order_state.v1.json 缺失所致）已 resolved；ATOM 四档止盈经证据回退在生产成功挂出（原始事故闭环）。
