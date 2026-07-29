# 窗口 A 评审：待修清单（C → A）

> 评审对象：`work/hermes-data-v3`（A 自报全部 done、314 测试通过）。方法：5 路并行只读审计 + C 亲自复核关键项。
> 结论：**A 的数据建模与核心 fail-closed 写得很扎实，但「服务端实际跑的是 legacy 不安全栈」+「风控网关 fail-OPEN」两条线导致一批 P0**，且多个 A 自报为 ✅ 的放行条件在 served 系统里**不成立**。这些不修，C 的 release gate 保持 `blocked`。

## 头号根因（一句话）

A 重构 bridge 时**新建了安全的 `services/control-plane` 模块，却没把它接进 served app**；`bridge/apps/api/app/main.py` 仍挂着 legacy 路由（`system_router`/`risk_router`/`freqtrade_router`…）。所以「314 单测通过」测的是安全模块，**线上跑的是带 test-token 兜底、角色错、内存审计、无 §2.2 envelope 的旧栈**。下面半数 P0 源于此。

## A 自报但 **不成立** 的放行条件（「不可伪造完成」）

| A 自报 | 实际 | 证据 |
|---|---|---|
| G10 ✅ 无 test-token fallback | ❌ 至少 2 处 served 路由缺 env 即默认 `test-*-token`（含 **risk_admin**） | `system_router.py:42-52`、`risk/router.py:37` |
| 无匿名 watcher 写 ✅ | ❌ ingress 写端点零鉴权 | `services/ingress/ingress/http.py:14-29` |
| fail-closed（依赖故障不增风险） | ❌ **网关在风控上下文缺失/陈旧时 fail-OPEN 批准** | `decision_gateway/gateway.py` + `risk/governor.py` |
| G3/G4 ✅ 面板只读 PG 投影 + 真实快照 | ❌ served `/api/system/snapshot` **无 §2.2 envelope、不读投影**（合规 builder 未接） | `system_router.py:16-21`→`dashboard_provider.py:38-41` |
| G6 多账户 ⚠️ partial | ❌ 比 partial 更糟：`scope=all` **静默单路由=误路由** | `gateway.py:95` |
| 「real ingestion smoke」覆盖 media 回滚 | ⚠️ 测试在进事务前就被拒，**没真正验证原子回滚** | `tests/ingress/test_ingress_service.py:195-224` |

## P0（必须修，违反铁律/安全/fail-open）

1. **服务端挂 legacy 不安全栈，安全 control-plane 未接入。** `main.py:29-39` 挂 `system_router`/`risk_router` 等旧路由；`services/control-plane/api/read_api.py`、`build_system_snapshot`、canonical 权限**全程未被 bridge 引用**。→ 把安全 control-plane 接成 served API，退役/改造 legacy 路由。
2. **风控 fail-OPEN（最严重）。** `gateway._load_risk_state` 行缺失→`return {}`；`governor.evaluate` 把缺失当 `mode=ACTIVE`、`exposure=0` → **批准**。全仓 0 处 `risk_context_incomplete`。→ 缺/陈旧风控上下文必须显式拒绝（fail-closed），区分「无行」与「ACTIVE」。
3. **量化风控闸门是死的。** `risk_state.set_mode` 只写 `state.mode`；`exposure_notional`/`open_risk_fraction` **永不写入** → 单币种暴露/总风险闸门读恒为 0、永不触发；单笔风险用常数 `default_risk_fraction`，非由 |entry−stop|×size 算。→ 从 positions 投影填暴露、按几何算单笔风险。
4. **决策/快照新鲜度从不入闸。** `policy.freshness_seconds`、入站 `valid_until` 定义却从不被 governor 读；`snapshot.py` 算了 `stale`/`reconciliation_state`/`projection_lag_ms`，gateway **从不调用**。→ 批准前校验决策时效 + 账户 reconciliation/lag，超阈 → `risk_context_incomplete`。
5. **Ingress 写端点零鉴权。** `http.py:14-29` 任意 `POST /telegram/raw` 直接 `ingest_raw_telegram_update` 写 `raw_messages`+`outbox(queued_for_hermes)` 喂 Hermes→risk→intent。唯一「匿名写」测试只 grep bridge、根本没碰 ingress。→ 加 header 共享密钥（常量时间比较、缺即 fail-closed），client 带 token，补真实 401 测试。
6. **test-token 兜底（含最高权限）。** `system_router.py:43` `RISK_ADMIN_API_TOKEN` 缺省 `"test-risk-admin-token"`（+viewer/trader/observer）；`risk/router.py:37` `SYSTEM_OBSERVER_API_TOKEN` 缺省 `"test-system-observer-token"` 且它守 **kill-switch/close-all/pair-lock**。→ 删全部字符串默认，统一走 `security/tokens.py::static_token_users()`（缺/重复即 raise）；改 lifespan 启动校验**所有** mounted 路由用到的 token。
7. **角色模型错。** `app/services/permissions.py:43-52` = `{viewer,trader,risk_admin,system_observer}`：缺 `reviewer`、缺 `nautilus_node`、混入 `trader`。结果 `reviewer`（规范里的审批角色）被拒、`trader` 反而能审批且可用 `test-trader-token` 铸出；`nautilus_node` 最小权限（只能写 event/heartbeat/ack）无处强制。→ 换成 canonical 5 角色矩阵（已存在于未接入的 `services/control-plane/security/permissions.py`）。
8. **危险操作弱确认 + 内存审计。** `risk/router.py:148-203` `request_id` 默认 `"manual-request"`、仅布尔 `confirm`；审计写**进程内 `AuditLog()`** 非 Postgres `audit_events`（重启即丢，多 worker 分裂）。合规实现 `security/dangerous_ops.py`+`security/audit.py`（落库+脱敏）已存在但未用。→ 走合规实现，request_id 必填、落库审计。
9. **无代码层闸门挡非法动作组合。** `hermes_decision.v1.json` **0 个** if/then/allOf；worker 不挡 `position_update→open_position`、不挡 `close/move_*` 缺 `target_position_id`。实测此类候选 schema 校验 0 错误即落库为可执行决策（仅靠 prompt 自觉）。→ worker 持久化前加确定性 guard（非法组合→`needs_review`/fail），并在 schema 加 if/then。
10. **`hermes_decisions` 无 DB 幂等兜底。** 仅 `idx_hermes_decisions_raw_message_id`（非唯一）；防重复纯靠应用层 claim/lease。→ 加 `UNIQUE(raw_message_id)` 或 `message_processing_runs(raw_message_id) WHERE status='succeeded'` 部分唯一，重复插入硬失败。
11. **served 快照无 §2.2 data-quality envelope。** served `SystemSnapshotResponse` 只有 account/positions/signals/risk/events，缺 `data_source/snapshot_id/generated_at/last_execution_event_at/projection_lag_ms/stale/missing_nodes/reconciliation_state`；合规 builder（`control-plane/api/snapshot.py`，与 `system_snapshot.v1.json` 完全吻合）未接。→ 接合规 builder，删 Empty/Fake 适配器路径。

## P1

- **smoke_replay 可在零成功下 exit 0。** `smoke_replay.py:95` 注入的 `_LiveSnapshotProvider.current()` 直接 raise → 每条都 `hermes_failed`，`processed≥minimum` 仍 `return 0`；且 BLOCKED 也 `return 0`。→ 接 A-09 真实投影 provider，成功判据要求 `status=="succeeded"`，BLOCKED/失败返回非 0。
- **多账户 `scope=all` 静默单路由 = 误路由**（`gateway.py:95`）：广播决策被压成单账户单 order、其余账户静默不动。→ `scope=all`/`unassigned` → needs_review；`scope=single` 必须带 `target_account_id`，不许回退全局默认。
- **精度检查 float 脆弱**（`governor.py:169-173` 用 `format(float(value))`）→ 全程用 `Decimal`，`as_tuple().exponent`。
- **开仓缺 stop_loss 时跳过几何/TP 校验**（`governor.py:114-118`：`_geometry_error` 在 side/price/stop 任一为 None 时返回 OK）；`replace_take_profits` 无 TP 单调性校验。→ 开仓缺 stop_loss → reject/needs_review；校验 TP 顺序。
- **media 回滚测试没触发事务内 DB 失败**（`tests/ingress/...:195-224` 在进事务前即被规范化拒绝）→ 加事务内失败用例验证 raw/outbox/media 全 0。
- **`check_no_semantic_regex.py` 只是退役 importer token 黑名单**，不检测真正语义正则（`re.search` 判 type/coin/side…），名不副实（本切片实质无正则，但闸门没覆盖广义声明）。→ 扩成扫描生产目录 `re.*` + 白名单 + `# noqa: semantic-regex` 豁免。
- **`SystemSnapshotV1.account` 冻结空对象**（`system_snapshot.v1.json:79-83`）→ 快照无法表达账户身份，多账户隔离/路由受限。→ contract-change：把 `account_id`（含 venue/currency）放进 `data.account`，决定 balances/positions 是否需账户维度。
- **`ProjectionWriter.upsert_*`/`insert_execution_event` 全是 `NotImplementedError`**（`db/repository.py:82-99`）→ G7「event_id 幂等投影」在 A 无生产实现（属 B/C seam，但 A 不应声称 partial 已证）。→ 实现 `INSERT … ON CONFLICT(event_id) DO NOTHING` + 版本守护 upsert。

## P2

- watcher edit `source_version=f"edit:{edit_date}"`（秒级）同秒两次编辑碰撞、后者被当重复丢弃（`collector.py:70`）。
- 未鉴权 500 泄露内部错误串（`http.py:25-26`）。
- `node_heartbeats.account_id`、`context_snapshots.raw_message_id` 可空（路由/健壮性 nit，非链断；needs-confirmation 是否故意）。
- SSE `/api/dashboard/stream` 未鉴权（当前不带数据，低危）。
- `contracts/security.py:6` `Role = Literal["viewer","trader","risk_admin"]` 旧 3 角色类型，强化 `trader` 泄漏。

## B 需要、但 A 未暴露的端点（C 要补的集成 seam）

- ❌ `POST /v1/nodes/{id}/intents` + `.../ack`（节点 intent 投递/确认）—— A 有命令-ack 状态机但**无 HTTP 路由**。
- ❌ `POST /v1/commands`（operator 下发命令）—— 逻辑在 `commands.py`，无路由。
- ❌ `GET /v1/reports/daily/...` —— A 在 `/api/reports/daily/...`，前缀/版本不一致。
- ⚠️ `/api/system/snapshot` 存在但 body 形状不合规（见 P0-11）；**无任何节点 execution-event/heartbeat 摄入路由**（ProjectionWriter 是 stub）。

## 做对的（基础扎实，**不用重写**）

contracts 枚举与 PLAN §3 逐字吻合；migrations 0001-0003 up/down 可逆有序、无不当删数据；outbox 与业务写同一事务；projection 表 DB 角色隔离（NOLOGIN writer，无 SELECT）；**可追溯链 raw→processing_run→decision→risk→intent→client/venue/trade 完整**；`trade_intents` approved 双重约束（双 FK NOT NULL + `ck_..._approved_chain`）；`source_received_at` 不可改触发器；watcher 纯采集（无语义/审批/交易所 key）；hermes-worker 真多模态 + fail-closed 七态 + temperature0/prompt 版本 pinning + 审计 hash + 最小权限；gateway 结构性闸门（provenance 仅 hermes、schema、whitelist、更新≠开仓、target 唯一、kill-switch HALTED/REDUCING、idempotency、原子写）真实有效。
