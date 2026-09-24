# 2026-09-24 执行节点计划隔离修复（本地，未部署）

## 状态与边界

基于本 worktree 的暂存生产基线增量修改；未 git add、commit、stash，未连接或操作生产。未改 control-plane、scripts、hermes-profile、db。全部改动未暂存；本说明和新增回归文件是 untracked。

**尚未满足全量通过的验收条件，不能据此宣布可部署。** 二次修复后，原命令仍因指定虚拟环境缺少 Nautilus 而收集失败；使用已有完整依赖解释器、不加外部状态目录环境变量的补充全量结果为 1036 passed、3 failed、11 skipped、226 subtests passed。两者不能混为原命令的通过结果。

Grok MCP 委派被当前 never 审批策略拒绝；companion CLI 因不能创建 `~/.grok/codex-plugin/state` 而 EPERM。按照项目 GROK.md 的双入口失败降级规则，由 Codex 完成本地修改和验证。

## 实现与根因

### 1. 计划保护独立计量

`_stage_entry_protection` 为新的 open_position market/limit/zone_ladder/entry_batch 建立 `batch_entry_ids / batch_fills / batch_exit_ids`，没有止损止盈也保留账本。原 `_protection_quantity` 的分账分支由此覆盖所有新开仓：`min(owned_quantity(batch_fills), 同向仓位)`；零成交不进入旧的账户合计回退分支。保留 add_position 原有计算/入场约束。

删除同向 `entry_batch_active`、`entry_batch_conflicting_owner` 拒绝；已有分账计划保持独立。无法证明历史退出的旧非批次账本标记 `legacy_fill_evidence_unresolved` 并冻结；保留原在场保护单。原有确证已平且订单终态的旧账退役逻辑仍保留。

`_batch_management_context` 将父级管理的仓位与订单缩小到本计划，禁用 reconciled_state 合计回退；旧未分账冻结账本不能被父级管理自动接管。`_close_entry_ids` 的父级限定只筛选本计划入场单，同时保留原有新鲜证据和撤单确认门禁。

`_queue_management_plan_after_persist` 和 `_submit_management_plan` 在提交管理退出单前登记 exit IDs；落盘排队失败恢复 preimage。`_live_protection_orders` 支持管理 intent 的显式 exit IDs。`_continue_protection_revision_submit` 在持久化回调后重新核对净额，避免过期的保护方案在净额归零后提交。

### 2. 多腿拒单回滚

`_rollback_rejected_entry` 由 `on_order_rejected / on_order_denied / on_order_accepted` 调用；拒绝时设持久化 `batch_closing`，取消本计划尚未终态的入场腿。接受事件处理迟到的接受回报。同步与异步多腿提交循环遇到 closing 停止后续提交；同步失败和 `_cancel_partial_prepared_submit` 都保留 closing 账本，避免失败后遗留的腿成为无人管理的挂单。已成交部分及迟到成交仍计入原计划私有账本。

### 3. 到期撤单与重启

代码根因：旧逻辑只在 `_sync_protection` 中检查 `batch_expires_at`，且只有 entry_batch 建立该字段；stash 丢失、被冻结、保护授权/仓位证据提前返回时，可能不再到达到期分支。重启并不会恢复一个独立的入场到期检查器。

新增 `_check_entry_expiry`，通过 `ENTRY_LIFECYCLE_SCAN` 在现有 durable worker 读取持久化 inbox 的 `intent_payload.order_plan.entry_expires_at` 和 `client_order_ids`，主线程 mailbox 回调后执行处理。此读取不在 actor 定时器上阻塞文件锁。保护 stash 缺失仍可恢复范围；周期 watchdog 与 reconcile 均触发。拒绝但留有已派发腿的持久化 intent 也会继续回滚。

`_cancel_scoped_venue_orders` 从有效 mirror 挑选本计划机器人 ID、regular 非 reduce-only 入场单，通过既有 terminal exchange worker 撤单。live 环境没有该 worker 时明确拒绝，不在 actor 上退化为同步 HTTP。没有 entry_expires_at 的正常旧入场不会仅因年龄被撤。

因此 account-a ETH `B6328feae26a64a8b9747ab872b70576603`、account-d BTC `Bb59e9bd8bd6546ec9aae639b3eac5a7101` 不属于自动过期清理。未做生产 SELECT，以上为代码路径定位，不声称已经验证过生产持久化文件的具体内容。

### 4. 净额归零和孤儿保护

`_sync_protection` 保留净额零的 tombstone，撤本计划剩余 cache/venue 保护单，其他同向计划不受影响；`_check_protection_watchdog` 对零净额走同步清理，不将缺少保护当成应补挂保护。

新增 `_cleanup_orphan_protections`：仅机器人 ID、reduce_only、STOP/TAKE_PROFIT 类型，在已校验新鲜的全账户快照中，以该 symbol+方向没有非零行判平；同 symbol 的非零 BOTH 行同样阻止清理。`positionRisk` 查询不传 symbol，且 `_position_evidence_rows` 会过滤零行，故不能要求快照存在零行。过期/未知证据、非零仓位、手动 ID 均不清理；保留快照之后本地成交的 stale_flat 守卫。无 stash 的重启节点亦可清理。

### 5. Algo 最终一致撤单确认

`BinanceExchangeCancelAdapter.cancel`：只对 algo、HTTP DELETE 无异常且响应无错误码、openAlgoOrders 已验证缺席的组合，NEW/UNKNOWN 最多额外回读两次；仍未更新则再次验证缺席后返回 CANCELED。受 confirmation/外部 deadline 限制，响应不是有效集合/订单项时不能当成缺席。

TRIGGERED/EXECUTED/FINISHED 保持 CancelConfirmationUnknownError；FILLED 保持独立终态。DELETE -2011/-2013 不获得成功标记，不适用缺席确认；仍在场不确认。regular 的确认规则不变。

## 回归覆盖

新增 `tests/execution/open/test_plan_isolation_regressions.py`：四种新开仓账本；旧 .117@80880.8 与新三腿第2腿 -2019 拒绝；零成交不产生保护计划；两腿迟到成交 .118 时新止损仅 .118@83600；旧止损不变；重启后 ledger/closing 保留；迟到接受撤单；同步拒绝停止第三腿；有/无到期字段的持久化恢复；净额归零后的本计划保护清理；机器人/方向/未知仓位限制；父级管理净额、close barrier；异步退出登记与持久化拒绝回滚；延迟保护提交前重新核对净额。

Algo 新增覆盖 NEW/UNKNOWN 有界重读与缺席确认、回读延迟后变 CANCELED、DELETE 失败不能据缺席确认、无效列表不能当缺席、在场单不能确认。

## 既有测试逐项调整

| 文件 / 原测试名 | 原断言或夹具 | 修改与理由 |
|---|---|---|
| open/test_entry_batch_strategy.py — test_batch_maps_exact_two_legs_and_rejects_existing_position | 同向仓返回 position_exists | 返回两腿计划；用户目标1允许独立同向开仓 |
| 同文件 — test_later_unrelated_plan_cannot_replace_batch_context | 新计划 stage 为 False | stage 为 True，仍保留原计划；允许共存，不是覆盖 |
| open/test_intent_execution_planner.py — test_validates_instrument_account_expiry_and_position_before_ordering | 同向 open 为 OrderDenied(position_exists) | 为 OrderPlan；其余校验保持 |
| open/test_intent_execution_planner_add_position.py — test_open_still_denied_when_cache_empty_and_venue_same_side | OrderDenied 类型、position_exists | OrderPlan；所内同向开仓允许，add_position 必须有仓断言未改 |
| open/test_intent_execution_planner_reconciled_open.py — test_duplicate_open_denied_when_venue_reports_same_side_position | OrderDenied 类型、position_exists | OrderPlan；同向独立计划允许 |
| 同文件 — test_open_simulation_without_reconciled_state_uses_cache | 缓存同向仓时等于 OrderDenied(position_exists) | OrderPlan；同向规则在模拟环境一致 |
| 同文件 — test_open_cache_and_matching_venue_still_position_exists | OrderDenied 类型、position_exists | OrderPlan；缓存和所内同向均允许 |
| open/test_intent_execution_strategy_shell.py — test_batch_accepts_only_proven_retired_legacy_owner | stage/new stash 存在性等于旧账是否已终态 | stage 和新 stash 均成功；旧非终态保留冻结，已证实终态退役。补入合法两腿 tranches，原夹具缺少必填字段 |
| 同文件 — test_durable_confirmation_timeout_sticky_halts_once | 等待 task timeout、一次 HALT | 阻塞期间无 HALT，释放后 worker 完成；生产已删除该落盘时限，仍等待持久化完成 |
| manage/test_take_profit_tombstone.py — test_mu_legacy_stale_owner_is_inert_and_new_message_terminates_it | 新消息删除旧未分账账本 | 保留旧账；目标1要求保留旧保护并冻结，不能静默接管 |
| 同文件 — test_new_entry_submit_failure_restores_previous_owner | 假提交函数只接受一个位置参数，与现有调用签名不兼容 | 仅令测试替身接受额外关键字；仍返回 False，所有失败后恢复原账本断言不变；没有忽略真实系统异常 |
| manage/test_exchange_cancel_adapter.py — test_algo_disappearance_is_not_terminal_or_filled_evidence | NEW/UNKNOWN/TRIGGERED/EXECUTED/FINISHED 均要求 unknown | 按用户后续裁定保留后三个子用例；NEW/UNKNOWN 拆成新测试，验证成功 DELETE、两次重读、再次缺席确认 |
| 同文件 — TerminalExchangeWorkerTest.test_unknown_cancel_uses_existing_reconciling_outcome | 以 NEW 回读测试 reconciling/error 内容 | 改以 TRIGGERED 回读及对应错误文本；reconciling 覆盖保留 |

`test_delete_success_without_terminal_status_fails_closed` 与 `test_regular_new_after_disappearance_remains_unknown` 未修改。剩余三个 Nautilus 失败未改断言、未 skip、未 xfail。

## 二次修复与验证更正

前次报告外部设置 `NODE_STATE_DIR` 后的“3 failed”，掩盖了两个补丁引入的测试环境失败。用户无该变量实测为5 failed，这两个错误不能算基线问题。现已在 `test_intent_execution_strategy_shell.py` 添加逐用例 autouse fixture，将 NODE_STATE_DIR 指向该用例的 tmp_path，退出恢复环境。整个模块统一隔离，包括只给 inbox 设置路径、而保护 stash 仍默认 /state 的 `_DurableIntentStrategy` 及其子类。**本轮没有修改任何既有用例断言。**

孤儿清理回归也已替换：不再手工构造包含零行的 snapshot；模拟传输返回交易所原始 positionRisk（含 positionAmt=0）和原始 openAlgoOrders，经真实 `BinanceExchangeEvidenceProvider.snapshot -> _refresh_evidence -> _position_evidence_rows` 解析和真实 freshness 校验后调用清理。测试断言查询不带 symbol、解析后不存在零数量行，并覆盖 LONG/SHORT 缺席清理、对应方向非零和 BOTH 非零不清理、其他品种/反向仓不阻止本方向清理、手动/非 reduce-only/非保护单不撤、过期/不可用证据与迟到成交守卫。

### 原命令（未设置额外环境变量）

```sh
cd /Users/balen/projects/trader-bot/.worktrees/prodfix-node-20260924
LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 PYTHONUTF8=1 /Users/balen/projects/trader-bot/.venv-arch/bin/python -m pytest tests/execution tests/nautilus -q --tb=line -p no:cacheprovider
```

`/tmp/prodfix-node-recheck.log`，退出码2：

```text
ERROR tests/execution/manage/test_hedge_reduce_only_submission.py
ModuleNotFoundError: No module named 'nautilus_trader'
7 skipped, 1 warning, 1 error in 0.35s
```

这是收集错误，未运行全量，不能报告为0 failed或3 failed。本机指定的 `.venv-arch` 确实缺少 Nautilus；没有修改测试去跳过导入，也没有向工作区外的虚拟环境安装文件。

### 已有完整依赖解释器的补充全量

仅替换 Python 路径为本机已有 Nautilus 1.227.0 的解释器；**没有额外设置 NODE_STATE_DIR 或 PYTHONPATH**：

```sh
LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 PYTHONUTF8=1 /Users/balen/.cache/uv/archive-v0/iOIrUuVWDw-_AeTw/bin/python -m pytest tests/execution tests/nautilus -q --tb=line -p no:cacheprovider
```

`/tmp/prodfix-node-recheck-cached-python.log`，退出码1：

```text
FAILED tests/nautilus/risk/test_nautilus_integration_hk.py::test_margin_risk_engine_cap_is_bypassed_and_live_strategy_fails_closed
FAILED tests/nautilus/risk/test_nautilus_integration_hk.py::test_emergency_cancel_all_and_reduce_only_close_all_confirm_events
FAILED tests/nautilus/runtime/test_command_journal.py::test_expired_restored_resume_rewrites_completed_ack_as_failed
3 failed, 1036 passed, 11 skipped, 19 warnings, 226 subtests passed in 24.54s
```

两个被点名的 fsync/flock 用例在此全量均通过。剩余三项分别为 position_state_unknown 先于名义上限拒绝、模拟 close_all 缺少入场撤单确认、过期 RESUME 重启后的 HALTED 应用不符；没有放宽生产语义、skip或xfail。

此前集中运行整个 shell 模块和事故回归时，另观察到一次 `test_terminal_exchange_faults_keep_actor_callback_under_10ms` 的13.6246ms耗时失败：1 failed、162 passed、107 subtests passed（`/tmp/prodfix-node-focused-recheck.log`）。这次失败保留记录；后续补充全量该用例通过，没有改其10ms断言。

最终针对事故回归和两个 fsync/flock 用例：**26 passed、2 warnings，退出码0**（`/tmp/prodfix-node-targeted-recheck.log`）。其中孤儿清理为9个真实解析路径场景，且没有命令外状态目录变量。`git diff --check` 通过。

## 生产热挂载替换清单（仅清单，未执行）

| 仓库文件 | 容器内目标（按当前 Dockerfile） |
|---|---|
| services/nautilus-node/strategy/intent_execution_strategy.py | /app/strategy/intent_execution_strategy.py |
| services/nautilus-node/runtime/exchange_cancel_adapter.py | /app/runtime/exchange_cancel_adapter.py |

本次不需要替换 execution-domain 文件、修改数据库或控制面；保持现有生产基线的其它热挂载。上述 Python 模块需要经独立授权的节点进程重新加载后才生效，替换文件本身不等于已生效。当前不满足全量验收，不提供上线完成结论。

## 剩余风险

- 补充全量的三项失败及指定原命令的依赖缺失尚未解决；另有一次集中测试10ms时延门失败，不能据补充解释器结果宣称原命令通过。
- 未连接生产验证旧 inbox/stash 是否完整；到期恢复依赖原始持久化 `entry_expires_at` 和派发 ID 尚在。缺字段不能按年龄猜测撤单。
- 旧非分账账本不自动推算历史净额。共存后冻结自动管理，保留原保护，需要权威完整历史才能迁移。
- 孤儿清理依赖 positionRisk 为完整全账户查询这一契约；若未来改为过滤/分页查询，必须同时调整缺席判平。非零 BOTH 仓位阻止清理，ACTIVE 之外保持已有 HALT 语义。
- Binance 仓位按账户方向合并，计划账本不能提供交易所原生的逐计划仓位隔离；并发成交与取消确认仍有竞争窗口。晚到成交继续入本计划账、保护缩量/清理按回报与新鲜证据收敛。
- Algo 缺席确认只限用户裁定组合；触发后子订单及不可确认状态继续拒绝替换。
