# 2026-09-30 六条规则本地修复验收

基线 `7677b619a47fcde74a745dde1602ecf47e150788`，分支 `codex/node-release-20260930`。strategy **+292 / -507，净删 215 行**（`git diff --numstat 7677b61`）。执行测试 **593 passed、192 subtests passed**；控制面相关测试 **17 passed**。修复、测试、报告在同一个本地提交，完整提交哈希见交付消息或 `git log -1`。

没有 SSH、访问生产、push、RESUME、修改 stash 或添加运行依赖；没有参考其他工作区。下文首次 ACTIVE 行为是按用户提供的生产状态推演，不是生产验证。

## 六条规则的实现

路径均相对仓库根，行号对应本次最终文件。

| 规则 | 实现位置 | 最终行为 |
| --- | --- | --- |
| R1 平仓 | `services/nautilus-node/strategy/intent_execution_strategy.py:9814` `_cancel_close_position_entries`；`:10009` 异步 continuation；`:10170` `_submit_management_plan` | 只选择交易所镜像里同方向、机器人、非 reduce-only、入场方向的挂单；撤单与现有 planner 的 reduce-only 平仓提交并行推进。不读取历史入场终态，不等待撤单回报或撤后仓位比对。重启前成交、镜像里已不存在的单不再撤，也不阻塞平仓。 |
| R2 入场到期 | strategy `:769` `_check_entry_expiry`、`:909` `_plan_entry_lifetime`、`:7140` `_sync_protection` 中移除过期写标记 | 从未成交按显式期限或 approved_at + expire_hours（默认 48h）撤入场腿。已成交含 add_position，派生 48h 不再撤；自己退出把持有量清零、关闭标记，或新鲜交易所方向归零才结束。显式 entry_expires_at 仍生效。未知/陈旧生命周期证据不动作，不写永久关闭标记。 |
| R3 保护看门狗 | strategy `:891` `_snapshot_book_has_robot_stop`、`:1036` `_check_protection_watchdog`、`:1441` `_release_symbol_watchdog_freezes`；`services/nautilus-node/projection/event_mapper.py:43` 起 | 真实交易所同品种同方向任意机器人 reduce-only STOP 即有保护，忽略 stash 价格、数量和期望 ID；不补 TP、不补 STOP、不冻结，并清除看门狗冻结。确实没有 STOP 且连续两次修复失败才冻结。STOP 出现或方向归零立即解冻。冻结/解冻均记录日志，独立 Stopped/Recovered 事件经既有投影和磁盘队列投递。其他原因冻结仍保留。 |
| R4 比例减仓 | `services/control-plane/api/read_api.py:656`，仅 `_execution_order_plan` 函数体新增 2 行 | 管理动作 `fraction` 与 quantity 一样透传为字符串。字符串和数字 0.7 都进入现有节点 planner；无 quantity 时不再丢 fraction。AST 验证函数体外完全相同。 |
| R5 陈旧镜像 | `services/nautilus-node/runtime/exchange_cancel_adapter.py:1998`、`:2003`；strategy `:739`、`:748`、`:3407`、`:10009`、`:11206` | 镜像不新鲜时懒刷新一次；刷新失败保持失败语义，不伪装空订单。管理动作在回调边界接住类型化异常，通过既有 exchange-state 定时器/worker 重试，valid_until 到期才终结。不重复规划已经 DISPATCHED 的不确定动作。 |
| R6 拒绝回报 | strategy `:11206` `_report_denial`、`:11233` `_reject_durable_intent`；既有 management 拒绝路径 | 最终不可执行仍走已有 denial reporter → intent ack rejected 通道。临时镜像失败在有效期内暂缓 rejected；到期原因 `expired:exchange_state_refresh_failed until valid_until`，并写入既有 durable inbox 的 REJECTED 状态。 |

R5 无法仅改 adapter：懒刷新可以集中在 adapter，但“有效期内暂缓拒绝、下一轮重试、到期 ack”必须接入 strategy 已有的回调和定时器。没有独立扫描器或新的持久化机制。

R3 多改一份已有节点文件 event_mapper：原投影 catalog 不接受自定义看门狗事件，且默认 payload 提取会丢动作字段。将两个看门狗事件归入账户风险事件，保留其 payload，并用事件键保证不同转换的去重身份；不伪造订单或账户余额事件，也不修改控制面。

## 删除的函数和检查

下表行数为基线 AST 函数首尾跨度（含函数内注释/空行，不含函数间空行）。12 个函数共 427 行；与 git 的 507 删除行统计口径不同，git 还包含其他分支、调用、字段和改写行。

| 删除函数 | 基线行号 | 行数 | 删除理由 |
| --- | ---: | ---: | --- |
| `_plan_in_progress` | 815 | 24 | 计划净成交精确等于账户方向仓位会错误处理其他计划/add/manual 的仓位。 |
| `_watchdog_recovery_denied` | 1303 | 10 | 删除双快照恢复后，不再需要其拒绝告警限频状态。 |
| `_watchdog_recovery_orders` | 1314 | 28 | 不用缓存历史订单和成交时间推迟新鲜交易所空仓解冻。 |
| `_recover_flat_watchdog_freezes` | 1343 | 72 | 删除两次独立快照 ≥60s、期间成交重置、stash/缓存检查链。 |
| `_recover_protection_freeze` | 1416 | 15 | 恢复统一按真实 STOP/空仓判断，不再按各 owner 期望保护键证明。 |
| `_exchange_protection_keys` | 1474 | 61 | 不再将实际保护映射回 stash 期望价格/ID。 |
| `_protection_key_for_client_order_id` | 1536 | 39 | 取消期望 ID 归类闸门。 |
| `_protection_key_for_order` | 1576 | 24 | 取消按期望价格判断缺失的辅助映射。 |
| `_close_entry_ids` | 9907 | 44 | 删除缓存终态、durable 入场 ID、parent 集合证明闸门；只读当前交易所挂单。 |
| `_close_entry_submit_allowed` | 9952 | 23 | 删除撤后 5s、唯一仓位、精确数量与缓存比对。 |
| `_begin_close_entry_barrier` | 9976 | 42 | 不排入证明 barrier，不等撤单完整终态才平仓。 |
| `_complete_close_entry_cancels` | 10019 | 45 | 不验证证明集合、不强制撤后重新规划数量。 |

一并删除 `_close_entry_proofs`、close_entry_cancel mailbox 分支及上述函数调用；删除 expiry 路径和 `_sync_protection` 的过期永久 batch_closing 写入。真实 close/reject/自有退出的 batch_closing 用途保留。终端操作命令的独立审计/幂等/仓位证据机制没有扩大修改。

## 旧测试逐条删改清单

没有修改新测试输入去绕开 `quantity_or_fraction_required` 或陈旧镜像异常；fraction 测试没有添加 quantity；镜像测试从真实不新鲜状态经过实际 adapter。旧测试仅有三个测试函数完全删除（参数化后共 8 个用例），其他是保留或改写。

### `tests/execution/manage/test_exchange_cancel_adapter.py`

| 原测试 | 原保护 | 本次调整及原因 |
| --- | --- | --- |
| `test_stale_response_invalidates_previous_orders` | 陈旧响应使旧订单缓存失效，读时立即报 not fresh。 | 缓存失效断言保留；读会触发懒刷新，网络失败 mock 覆盖整个读，断言刷新失败；R5 新真实 callback 测试覆盖恢复成功。 |
| `test_refresh_failure_invalidates_previous_orders` | 刷新失败不沿用旧订单。 | 同上，保留失效和报错，不将失败变成空列表。 |

### `tests/execution/manage/test_intent_execution_strategy_manage_shell.py`

| 原测试 | 原保护 | 新测试/理由 |
| --- | --- | --- |
| `test_close_waits_for_real_worker_cancel_and_replans_after_racing_fill` | 等真实 worker 撤单并按撤后 .600 重规划。 | 改为 `test_close_submits_without_waiting_for_real_worker_cancel`；真实 worker 撤单阻塞时已发 .500 reduce-only。R1 明确允许竞态余仓由看门狗/下一次 close 处理。原测试内附带重启/终态代码块移除；原有 `test_async_add_prepare_cannot_cross_close_reopen_with_same_position`、durable/terminal 测试继续保护 receipt 代际与终态，新增 account-d 测试保护重启后的实际平仓。 |
| `test_close_cancel_unknown_or_stale_evidence_remains_pending` | 撤单 unknown、旧证据、缓存滞后、证据缺失都不平。 | 改为 `test_close_cancel_unknown_or_stale_evidence_does_not_block_reduce_only`；保留四个子场景和真实 worker，已发单且不撤人工单。撤后证据不再是 R1 的必要条件；当前镜像不新鲜仍由 R5 验证。 |

### `tests/execution/open/test_intent_execution_strategy_shell.py`

| 原测试 | 原保护 | 调整及理由 |
| --- | --- | --- |
| `test_async_add_prepare_cannot_cross_close_reopen_with_same_position` | 异步 add 不跨越 close/reopen 的 receipt 代际。 | 用实际空镜像替代已删除 `_close_entry_ids` mock；代际断言保留。 |
| `test_watchdog_recovery_preserves_other_freezes_and_unhealthy_owners` | 恢复不得清除其他冻结。 | 去掉期望键 mock，用实际无 STOP；其他冻结断言保留。 |
| `test_watchdog_recovery_keeps_pending_receipt_fence` | 保护恢复不得清除 pending receipt 围栏。 | 用真实 STOP 行替代期望键 mock，仍保留非看门狗冻结。 |
| `test_watchdog_confirmed_recovery_clears_only_protection_freeze` | 保护确认后清看门狗冻结。 | 用真实 STOP 行替代旧 mock，不要求 stash 价格/ID 匹配。 |
| `test_protection_watchdog_repairs_missing_take_profit_without_freezing_symbol` | 有 STOP 时仍补 TP。 | 改为 `test_protection_watchdog_existing_stop_does_not_repair_missing_take_profit`；R3 的有保护定义只看 STOP，要求不补单。 |
| `test_protection_watchdog_submits_only_missing_take_profit` | 有 STOP 时提交一个 TP。 | 改为 `test_protection_watchdog_existing_stop_submits_nothing_for_missing_take_profit`；保留实际提交列表断言，要求 0 单。 |

该文件 `_MissingProtectionEvidence` fixture 补充真实规则需要的 fresh snapshot、positions、position_side、reduce_only、order_type，绑定 strategy 以反映 recovered PENGU 的实际品种。原来只有 ID 的 snapshot 无法证明“同品种同方向机器人 reduce-only STOP”，补齐字段而非绕过不新鲜错误。使用该 fixture 的两次失败冻结、成功 STOP 修复、PENGU 恢复修复、仅提交修复不解冻等旧断言仍在。

### `tests/execution/open/test_plan_isolation_regressions.py`

| 原测试 | 原保护 | 新测试/理由 |
| --- | --- | --- |
| `test_parent_management_has_private_position_orders_and_close_barrier` | parent 私有数量、只撤 parent 的入场 barrier。 | 改为 `test_parent_management_keeps_owned_quantity_but_cancels_all_same_book_entries`；保留 parent 数量隔离，撤单改为同方向所有机器人当前入场单（parent+sibling），符合 R1；非机器人/异方向/保护单过滤由新增实际 close 测试覆盖。 |

### `tests/execution/open/test_watchdog_freeze_recovery.py`

| 原测试 | 原保护 | 调整/覆盖 |
| --- | --- | --- |
| `test_watchdog_flat_recovery` | 首次空仓不解冻；缓存/时间/stash 条件通过且 60s 后才恢复。 | 保留 24 个场景；新鲜空仓立即解冻，缺失/陈旧证据、裸仓、其他冻结仍阻止恢复。 |
| `test_invalid_robot_fill_timestamp_blocks_thaw` | 非法历史成交时间阻塞恢复。 | 改为 `test_invalid_robot_fill_timestamp_does_not_block_fresh_flat_thaw`，保留 18 个参数；历史缓存不阻塞新鲜交易所空仓证据。 |
| `test_two_independent_snapshots_and_intervening_fill_reset` | 双快照 60s 和期间成交重置。 | 删除；该机制由 R3 明确取消，flat recovery 和新增真实 STOP 自动恢复覆盖新规则。 |
| `test_invalid_intermediate_evidence_clears_first_observation` | 中途六种错误清除第一次观察并重新等待。 | 删除六个参数用例；不存在第一次观察状态。陈旧/缺失 snapshot 保留冻结的场景仍由 flat recovery 与黄金 stale/missing 测试覆盖。 |
| `test_denial_warning_is_per_symbol_rate_limited` | 旧恢复检查拒绝告警每品种限频。 | 删除；旧检查器及其限频函数删除。新的冻结/解冻转换日志和事件由 production_watchdog_events 覆盖。 |
| `test_manual_flat_then_robot_position_refreezes_after_two_failed_repairs` | 人工平仓后恢复、新机器人仓位两次失败再冻结。 | 空仓立即恢复；人工单保留和随后两次失败冻结断言继续保留。 |
| `test_entry_expiry_anchor_and_cancel_scope` | 日期优先、entry_batch fallback、撤单范围。 | approved_at 优先、默认 48h 同样适用 entry_batch；账号/动作/保护/人工单过滤、非法时间、HALTED 等原场景保留。 |
| `test_started_plan_remaining_entries_do_not_expire` | node-c 精确数量例外，陈旧/数量不同则过期。 | 陈旧/缺失、venue larger/smaller 改为保留；方向归零、自己的退出、显式期限、拒绝/关闭仍撤；R2 取消精确相等要求。 |
| `test_explicit_stash_batch_expiry_still_closes_started_plan` | 显式期限写 batch_closing 后撤腿。 | 改断言为直接 scoped 撤入场腿，不留下 batch_closing；显式期限作用保留。 |

## 六个生产序列与基线证据

最终新增执行用例共 35 个。相同最终测试文件复制到 `git archive 7677b61` 的隔离临时目录 `/private/tmp/six-rules-7677b61-FLj2`，运行结果 **34 failed、1 passed**，退出码 1，没有 collection error。其中基线通过的是已派发不确定管理动作不重复提交（保留不变式）。控制面新增两例基线 **2 failed**，fraction 均丢失为 None。

| 验收序列 | 新测试位置 | 7677b61 失败表现 | 新代码 |
| --- | --- | --- | --- |
| 1 account-d 两个历史入场成交、重启缓存为空 | `tests/execution/manage/test_production_six_rules.py:133` | `order_cancel_not_found` / `close_entries_reconciling: entry terminal evidence unavailable`，0 单。 | 直接 .210 reduce-only；不撤历史已成交 ID。 |
| 2 partial_close，先过控制面 fraction 转换 | 同文件 `:359`、`:362`、`:365`、`:368`；`tests/control-plane/api/test_execution_order_plan_fraction.py:67` | `quantity_or_fraction_required`，fraction None。 | 字符串/数字 .7，.210 → .147；机器人 scope 的 venue10/robot1 → .700。 |
| 3 move_stop_loss 陈旧镜像、刷新成功/失败到期 | `tests/execution/manage/test_production_mirror_retry.py:52`、`:66`、`:84`、`:119`、`:133` | typed mirror 异常逃逸，或临时失败立即 rejected、缺少后续推进。 | callback 不崩；成功移到 82954.50；失败有效期内重试，到期单次 rejected；同步及真实异步 worker/durable lane 均覆盖。 |
| 4 黄金 .212 + .167 + .103 | `tests/execution/manage/test_production_lifecycle_rules.py:36`（12 参数） | 满 48h 撤 t2/t3，或写永久关闭标记。 | .482 方向仍有仓位时保留；fresh flat/自己的 STOP 成交撤；never-filled/default48/explicit 仍撤；stale/missing 不动作；保护和人工单不撤。 |
| 5 account-b STOP @73650，stash @75793 | 同文件 `:121` | 按期望 ID/价格判缺失，修复/冻结不能恢复。 | 不补单，不冻结，已有冻结清除，stash 不改。 |
| 6 无机器人 STOP，两次补单失败，然后 STOP 出现 | 同文件 `:143`（6 参数）；`tests/execution/manage/test_production_watchdog_events.py:18`（2 参数） | 冻结后不按新规则解冻，且投影过滤事件。 | 第二次才冻结；真实 STOP 出现恢复；人工、异方向、非 reduce-only、TP 不算 STOP；磁盘队列重读验证两个转换事件及 payload。 |

fraction 测试保留现有 planner 授权口径：用户 operator 指令沿用 whole-book 权限，机器人 channel/parent 权限用 robot-owned 数量。本次不改变 planner，也不改人工授权协议。生产验收 .210 gross=robot；另有 venue 10 / robot 1 不等量的机器人 scope 测试。

## 完整验证结果

| 验证 | 结果 | 输出 |
| --- | --- | --- |
| 原始基线 tests/execution（不含新增测试） | 566 passed、192 subtests passed，退出 0 | `/private/tmp/six-rules-baseline-original-execution.log` |
| 最终 tests/execution 全量 | 593 passed、192 subtests passed，12 个既有 NumPy/Pandas warning，退出 0，11.70s | `/private/tmp/six-rules-final-execution.log` |
| 基线运行最终新增执行用例 | 34 failed、1 passed，退出 1，2.49s | `/private/tmp/six-rules-final-baseline.log` |
| 控制面相关三文件 | 17 passed，1 个既有 Starlette warning，退出 0，1.36s | `/private/tmp/six-rules-control-plane-final.log` |
| 基线控制面新增用例 | 2 failed，退出 1，0.20s | `/private/tmp/six-rules-7677b61-control-plane.log` |
| diff 空白检查及控制面范围 | `git diff --check` 通过；AST 除 `_execution_order_plan` body 外完全一致 | 本地检查 |

全量执行命令：

```sh
T=$(mktemp -d)
NODE_STATE_DIR=$T PYTHONPATH=/Users/balen/projects/trader-bot/quant-lab/.venv-g2/lib/python3.12/site-packages /Users/balen/projects/trader-bot/.venv-arch/bin/python -m pytest tests/execution -q -p no:cacheprovider
```

控制面命令：

```sh
LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 PYTHONUTF8=1 /Users/balen/projects/trader-bot/.venv-arch/bin/python -m pytest tests/control-plane/api/test_execution_order_plan_fraction.py tests/control-plane/api/test_intent_authorization_downlink.py tests/control-plane/api/test_operator_entry_batch.py -q
```

新增 35 执行用例、删去 8 个旧机制用例，因此 566 + 35 - 8 = 593；192 subtests 不变。

## 部署文件与 SHA256

四个节点应统一替换下面三份已有节点文件的 bind-mount 内容。没有新模块/依赖。部署需要同时带 event_mapper，否则冻结/解冻事件仍会被过滤。

| 文件 | SHA256 |
| --- | --- |
| `services/nautilus-node/strategy/intent_execution_strategy.py` | `6e7b0229a27b3ec46cbcf4bf6cd34d7e33a19f98fc5d6fffc8c7d8bad99c0b3d` |
| `services/nautilus-node/runtime/exchange_cancel_adapter.py` | `429077f9424a7b445e3d7610dca6f23511ec2e1c573081d3e7aff6dfdfcdf39f` |
| `services/nautilus-node/projection/event_mapper.py` | `1002e35ae26beaa5ec37a1fff0e6fa1042af8dd37b4160dc5f32d7c439159e18` |
| 本地 `services/control-plane/api/read_api.py`（仅校验源文件，不用于整体覆盖生产漂移） | `a5574a74750099555080344f82806e8eb30b2c57bd6f8539e2c8944ab9fae162` |
| `docs/reports/2026-09-30-execution-order-plan.function.py.txt`（精确函数块，生产仅移植这一块） | `3469198817a76d22e117669e494230ba3824ab0499cf623d14753dba57b22def` |

read_api 只移植 `_execution_order_plan`，不能整体覆盖生产的热补丁漂移。控制面文件落盘后需对应 operator-query/node-control/event-ingest 服务实际加载新函数；本次没有执行部署。现有发布门禁仍需停节点之前全部完成，RESUME 仍需要用户明确指令。

## 按提供状态推演首次 ACTIVE

| 账户 | 行为 |
| --- | --- |
| a | 用相同策略；镜像不新鲜先刷新，暂时失败的管理指令保留到下一轮，过有效期通过 ack rejected。未提供 a 的额外订单/仓位状态，不能声称具体成交或撤单。 |
| b / BTC 3bb0dab5 | fresh snapshot 上 B3bb…31 @73650、qty .062 的真实 LONG reduce-only STOP 即有保护；不会按 stash @75793 补单或冻结；已有 watchdog reason 冻结自动清除并写日志/Recovered 事件。不改 stash，不动人工单。 |
| c / 黄金 9a6c4b89 | .212 的计划加上其他 add 形成 LONG .482 时，48h 不撤仍挂着的 t2/t3；随后自己 STOP/TP/close 退出归零或 fresh LONG=0 时撤剩余入场腿。已在旧版本撤掉的 t2/t3不会自动复活；显式期限、尚未成交且过期的计划照撤。 |
| d / BTC close 9a4b8ff9 | 如果到达一条仍有效、授权且 planner 确认 .210 的 close，镜像不包含旧两个成交入场 ID 就无需撤它们，直接 reduce-only 市价 .210。真实挂着的同方向机器人入场腿另行撤；不等撤单终态。旧 9月29日指令若已过 valid_until 不会自动重放。move_stop_loss 在镜像懒刷新恢复后推进，持续失败则到期 rejected。 |

部署代码本身不打开账户总闸门；“首次 ACTIVE”仅指用户授权恢复后的策略行为。

## 剩余风险和边界

- 规则明确接受任意价格/数量 STOP。它可能覆盖不足或价格不理想；看门狗不会因此补单或冻结。
- 撤入场单与市价平仓并行，可能出现竞态剩余小仓位；按 R1 交给保护或下一次 close，不增加终态闸门。
- 回调里的懒刷新受现有请求 timeout 限制，可能短暂阻塞。没有额外线程池或依赖；已 DISPATCHED 的不确定动作不重规划，以避免重复交易。
- 新鲜证据仍必须是现有完整快照。证据持续不可用时不解冻、不判断 filled 计划归零；管理指令在有效期结束后 rejected，定时器周期会影响回报的实际到达时刻。
- 旧版本已写下的 batch_closing 无法可靠区分“旧过期副作用”和真实关闭，不擅自清洗。此补丁防止新的 expiry 永久标记；没有 stash 整治脚本。
- 暂缓管理指令集合在内存中；重启沿用现有 durable inbox 恢复。本次没有独立按 intent 保留终态证据或新的到期扫描器。
- 日志/事件链路已经本地验证磁盘持久化；外部 API、生产实际快照、四节点发布后的心跳/审计尚未验证。
- Grok MCP 不可用，companion CLI 本次反复执行失败/无可用终态产出，达到已用 session 上限后按用户委派降级约定由 Codex 完成剩余实现与验收；未继续开新 session。
