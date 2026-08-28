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
