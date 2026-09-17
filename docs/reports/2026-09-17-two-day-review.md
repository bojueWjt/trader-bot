# 2026-09-17 近两日工作与 Hermes 对话复核

结论：发布已经真实生效，但交易可靠性尚未闭环。当前存在管理订单授权回归、应急直连后账本未收敛、平仓后保护单生命周期遗漏；账户独立 signal 队列仍未切换到线上执行。

## 范围与证据

- 时间窗口：约 2026-09-15 15:19 至 2026-09-17 15:19 UTC；实时核验延伸至 15:28 UTC。本文时间均为 UTC。
- 固定代码范围：`git diff cbb56e9...1e67334`，7 个提交、99 个文件。重点审查执行授权、保护单、信号移交、发布及查询契约，不声称逐行穷尽全部变更。
- 工作区已有 quant-lab、eval、计划文档和 `.live-mirror` 删除等未提交改动，全部保留；不把这些归入已部署版本。
- 原始对话：jp-24 `/srv/hermes/profiles/trader/state.db`，SQLite `mode=ro`；交互记录来自 9/17，9/15–16 的相关工作主要见 cron 会话。另核对 `sessions/session_20260917_141750_c4d5d3.json` 的技能固化记录。会话中的历史指令没有作为本次操作授权。
- 生产事实：Postgres 审计/执行事件、节点日志/持久 stash、Docker 镜像及标签、推进中的心跳，以及币安只读订单、成交、仓位、算法单查询。没有部署、重启、RESUME、下单或撤单。
- 本次启动的两个 Grok 任务已在用户要求自行操作后取消；以下关键结论由 Codex 独立读取证据并复现。

## 已完成工作

| 提交 | 主要交付 |
|---|---|
| d9e4771 | 执行证据统一、管理操作状态、signal 权限与队列接入、OLM 剪枝 |
| c493c0c | 发布物覆盖运行依赖及 dashboard，保留网页操作员身份 |
| dde5df9 | 0019 权益采样迁移与 Redis 回退修正 |
| a9be037 | 持仓查询改用交易所 hedge book |
| 98eacdb | 权益历史采样与移动端读取契约修复 |
| f125884 | 保留合法工作入场单，完善资金占用预留 |
| 1e67334 | 旧保护状态依据交易所确证收敛 |

四个节点的真实镜像均为 `sha256:199c89fe0e13b96dfb2af19a8764691d8bdd1b0101c8996077e57b94627b03b2`，标签 commit 均为 `1e67334ec1853a94fb60beb1ec7cf6cce3b4d6a8`，release 均为 `a61126ca9b58d7e90bf5ad7f2a6c4901e392ce296948c1ced399b44a2e8c5eb1`。容器于 9/17 01:11 UTC 先后启动；审计中有当次 01:16–01:21 的 RESUME 记录。15:22 四节点 ACTIVE，心跳年龄 0.2–1.9 秒，四个 `/ready` 均为 200。这证明部署与运行状态，不能证明所有业务路径正确；审计 reason 中的授权说明也不代替用户原始授权的逐条审查。

## Standards：规范与操作边界

### S1 / P1：新固化的直连 skill 错把 aos_ 归为节点订单，模板撤单范围也过宽

远端 `/srv/hermes/profiles/trader/skills/trading/binance-algo-direct/SKILL.md:53` 写“节点算法单是 B<hex> / aos_ 前缀”。这与项目 AGENTS 的明确规则冲突：机器人身份只有 `^B[0-9a-f]{32}[0-9]{2}$`，`aos_` 属用户手动单。

被该 skill 推荐的 `/root/place_protections_d.py` 在 `placing_eth` 后仅按 ETHUSDT + SELL 过滤旧算法单并撤销，没有限制到原操作明确授权的 clientAlgoId/algoId，也未校验 positionSide/订单归属。模板若复用，会波及同方向的其他手动保护单。本次确证被替换的是旧 ETH 保护，不声称已经误撤了其他人的单。

修正方向：纠正 skill 的归属规则，撤单固定到本次批准替换的精确 ID 集合；不能把本次应急授权泛化成之后的批量撤单权限。

### S2 / P1：模板宣称幂等，实际每次运行都会撤旧并生成新 ID

同一脚本声明“逐单幂等”，但 `existing_ids` 被采集后未用于判重；ETH 每次运行都会进入撤单分支，随后用当前毫秒时间生成 `opm4402...` 新 ID。挂单成功但响应丢失、或脚本重跑时，不能凭同一业务身份找回原操作。`all_ok` 仅取决于请求是否抛异常，最后打印的 `verify_open_algo` 没有逐项断言计划确实在场。

修正方向：稳定业务 ID、查询核对后再重试、精确比较在场价量方向，并在成功报告前验证最终保护。当前脚本不宜直接当通用应急模板。

## Spec：需求实现与线上缺口

### R1 / P1：partial_close / 移止损的真实子订单没有登记到持久授权记录

代码：`services/nautilus-node/strategy/intent_execution_strategy.py:11827` 的 `_management_operation_ids` 只为 `close_position` 返回真实子订单 ID，其他管理动作保存合成的 `99`。而新提交守卫在 `:9146` 按真实 child ID 查询 inbox，于是找不到对应记录，落到 stash 后报 `reduction_authorization_missing`。

违反根因方案 §5 的“授权主体与范围必须持久记录”及全局交互合法操作的验收要求。9/17 12:59:42、13:24:32、13:26:55、13:27:13、13:27:35，account-d 的 5 次 ETH 管理请求被该原因拒绝，其中包括频道、用户授权和手机操作。

独立复现：读取 Hermes `/root/partial-close-child-id-fix/test_child_ids.py`，在本机导入当前仓库真实 strategy/planner/inbox，临时 inbox、Nautilus enum shim、无交易请求。当前 HEAD 的 11 项测试有 7 处失败记录（含 subtest）；仅在测试内存中将条件改为 `if plan.orders`，11 项全部通过。既有 `test_hedge_reduce_only_submission.py` 手动调用 `begin_dispatch(identity, (order_id,))`，绕开了出错的管理 ID 生成环节，因此其通过不足以证明接线正确。

Hermes 的一行修复方向正确，但仍仅存在于 jp-24 `/root/partial-close-child-id-fix/`。节点 D 实际 `/app/strategy/intent_execution_strategy.py` SHA256 仍为 `3ac3ddcad741ebf4d443f341d103bdbd5aacdfb742b2d58c4f5ce090b6990622`，与当前未修版本一致。修复还要覆盖历史 99-only 记录的安全处理，不能盲目重放旧减仓。

### R2 / P1：应急成交已完成，自动管理所需的归属和保护账本仍未收敛

15:25 币安确认 ETH 余仓 1.329，人工应急保护 `opm4402sl...` / `opm4402tp...` 在场。但节点持久 stash 仍保留旧机器人 SL2355/TP2699×3.797，`protected_quantity=null`，15:27 watchdog 失败计数已达 142，持续报 ETH `symbol_new_open_frozen`。

14:37:49，峰哥 m4404 要求 ETH 全出，控制面已认定同频道归属，intent `31af958d-6b1a-4712-9c16-bc09225bc47c` 随后被节点以 `reduction_quantity_exceeds_authority:1.329` 拒绝。其原因不同于 R1，修 child ID 不能据此宣称该问题也已解决。

代码调查点：`:9147` 从 `_robot_owned_position_quantity` 取得自动减仓额度；该函数 `:2562` 依赖保护 stash 数量/本地缓存成交。账本与现役应急单断开已被实证；具体归属额度为什么降到不足，仍需沿缓存和保护变更事件做定向复现，不能直接放宽授权上限或把人工单冒认机器人单。

这违反方案“交易所事实为权威、cache/stash 为派生视图”的目标。应急操作的完成标准应包含受授权的账本核对/后续管理交接，而不止币安挂单成功。

### R3 / P1：ZEC 止损平仓后仍尝试补止损，并留下两张止盈

9/17 14:29:33.863，ZEC 空仓 1.394 由原 SL1440 触发，成交均价 1441，币安成交与节点 `PositionClosed` 一致。14:29:35 节点却又提交同量 SL1440（CID 尾号 41），币安以 `-2021 Order would immediately trigger` 拒绝。

15:25 仓位已为零，但两张旧机器人 TP（algoId `3000002199115743` / `3000002199115741`）仍 NEW，各 0.697。不能将其描述为“当前 ZEC 仓位保护正常”。后续定向回放已定位：`_defer_flat_or_unknown_protection` 在空仓时提前返回，绕过旧清理路径；同时缺少持久化的平仓标记，旧 known_open 快照仍可驱动补单。下文记录本地修复与验证，生产状态仍以上述核验时点为准。

### R4 / P2：新队列实现交付了，账户独立执行路径未上线

feeder 真实进程环境没有 `SIGNAL_EXECUTION_*` 设置，`trader-v3-signal-worker@*` 无 loaded 实例。近 48 小时共 31 条任务全部为 `processing_purpose=shadow,status=pending`：a=8、b=5、c=2、d=16。Hermes 原始会话显示交易信号仍由旧 cron 路径执行。

这是未完成的切换和 shadow 消费验收，不把 shadow pending 直接等同漏交易；旧路径仍在工作。但方案“账户隔离调度”“管理 job 不阻塞其他账户”的线上收益尚无实现证据。不能凭代码/迁移已部署就报该目标完成。

## Hermes 对话评价与真实结果

- 9/15 cron `1260e2d8414a_20260915_190310` 从 TAO 移损失败一路进入节点热补丁排查；其他 cron 又轮询同一 HALT 和裸仓。这说明当时仍由 LLM 承担交易生命周期与运维协调，存在重复等待和操作交接不清。当前版本是否仍会走同样热补路径，不能由旧会话推定。
- 9/16 开仓 cron 曾报 Python/CLI 不兼容，随后日报却已有 ETH 持仓：单条失败汇报不足以判断最终是否成交，必须按 intent/venue 追踪。此次没有逐笔重建所有历史信号的完整漏单率。
- 9/17 GPT 阶段明确区分受理、拒绝、成交；正确定位 child-ID 缺陷，也如实承认补丁未上线。但多次建议使用币安官方客户端，直到用户说明“子账号只能 API”才纠正，处理路径不符合用户实际约束。
- 9/17 GLM 阶段按用户明确直连指令完成 ETH 应急操作；初期仅查普通 openOrders 就误判 ETH/ZEC 裸仓，后在 openAlgoOrders 中发现原保护仍在，主动纠正并跳过 ZEC 重挂。这个纠错是实质性的，但后续固化 skill 引入了 S1/S2 的问题。
- SQLite message 时间存在整段批量写入，不能当作每条工具动作的精确发生时间；以下成交时间以交易所 time 为准。

| 事项 | 独立核验结果 |
|---|---|
| ETH 减仓 65% | 9/17 14:08:47.651，SELL 2.468 ETH，均价 **2457.87**，4 笔成交，同订单 `8389766278939390100`，FILLED |
| ETH 本次已实现 | **142.79848 USDT**；该减仓单手续费合计 **3.03301157 USDT**，不含此前开仓费用/资金费 |
| ETH 余仓 | 15:25 时 **1.329 ETH LONG，入场 2400.01** |
| ETH 保护 | SL **2400.01**，closePosition、MARK_PRICE；TP **2699 × 1.329**，均 NEW；止损价等于入场价不保证实际无滑点或手续费损失 |
| ETH 后续全平 | 14:37 m4404 被节点拒绝，未完成；不能把此前减仓完成写成已全部清仓 |
| ZEC | 14:29:33.863 已止损平仓 **1.394 @ 1441**，已实现 **-112.02184 USDT（未扣费）**；两张旧 TP 仍在 |

原始定位：交互会话 `20260917_130128_ddd64e42`、`20260917_134341_f54444`、`20260917_141246_954ef2`；关键消息 ID 30773（明确授权仍拒）、30994（离线补丁）、31052（裸仓误判纠正）、31071（应急完成汇报）；后续 cron `c8dc119e1e1d_20260917_143706` 对应全平失败。

## 验证边界与后续顺序

### 补充：手机 App 操作（用户指定最新源码位于另一台 Mac）

用户补充：今天在实际减仓之前，多次用手机 App 面板操作平仓失败；最新 App 代码位于 `balenmacbook-pro-2.tail08d532.ts.net` 的 `projects/trader-bot`。因此不能用当前本机 dashboard 源码代替实际手机 App 验收。

已从生产 `trade_intents` 和 `audit_events` 对齐如下请求；三次均有 `authorized_by_type=user` 和 `mobile-op_...` 来源，均已通过控制面、在执行节点被 `reduction_authorization_missing` 拒绝：

| UTC 时间 | intent | App 请求 | 结果 |
|---|---|---|---|
| 9/17 13:26:54.229 | efb026b2-2d4d-4f0f-8dc1-0a0c4d11f7da | ETH LONG partial_close，quantity=2.847 | 13:26:55 节点拒绝 |
| 9/17 13:27:12.664 | 359ebc02-0ce4-4296-a9e2-c06944e5cad2 | ETH LONG move_stop_loss，stop_loss=2355 | 13:27:13 节点拒绝 |
| 9/17 13:27:33.868 | a1a784fa-6794-4b55-8bbb-901deba96b3d | ETH LONG partial_close，quantity=2.847 | 13:27:35 节点拒绝 |

这三次失败不是 HTTP 路由不通或控制面未受理。2.847 约为当时 3.797 仓位的 75%，与之后 Hermes 按 65% 成交的 2.468 不同；尚未见 App 源码，不能断言用户当时选择比例或 UI 是否显示正确。移损请求发出的也是原止损 2355，不是开仓价 2400.01。

源码读取状态：目标 Tailscale IP `100.111.192.24` 可达，但本机以 `balen` SSH 登录收到 `Permission denied (publickey,password,keyboard-interactive)`；已向用户询问实际 SSH 账户/免密入口。最新 App 源码审查待此访问条件解决。

`git diff --check cbb56e9...1e67334` 退出码 0。11 项离线回归完成上述红/绿对照；本机原先缺 pytest/pydantic，使用隔离的 `uv run --no-project --with pydantic python` 运行 unittest。测试采用实际仓库模块、临时持久文件及模拟运行时，未运行完整 Nautilus/控制面/前端测试，因此不能把历史报告的上千项通过数当作本次复测。

建议先合入并验收 R1，同时独立处理 R2/R3 的现役账本与生命周期问题；修正 S1/S2 后才复用应急 skill。交易核心正确性核验完成后，再做 R4 的 shadow 消费和单账户切换验收。初次 review 未做修复；后续用户要求处理 R3，已完成以下本地变更，未改变生产状态。

轴内汇总：Standards 2 项（最高 P1，订单归属/撤单范围）；Spec 4 项（最高 P1，管理订单授权与生命周期）。

## R3 后续修复：平仓后的保护单收尾（本地，未部署）

用户指出问题持续发生后，以 ZEC m4403 的时间顺序构建回归：止损成交、PositionClosed、旧持仓快照、兄弟 TP 留在交易所。原实现回放失败：未发起 TP 撤销，平仓后仍进入修复路径。

本次修改 `services/nautilus-node/strategy/intent_execution_strategy.py` 与 `services/nautilus-node/runtime/exchange_cancel_adapter.py`：

- 持久化平仓时间；watchdog、同步任务、已准备好的保护提交均受该标记约束。只有归属于原 intent 且晚于平仓的入场成交可以解除标记，手动成交与旧事件不能解除。
- 新鲜交易所空仓证据触发兄弟保护单清理。范围限定同账户、同交易对、同持仓方向、原 intent 或明确保护归属的严格机器人 CID；同时覆盖普通单与 algo 单。
- 提交撤单前，落盘待撤 CID 与交易所订单定位信息。重启后即使挂单列表已无该单，也能按原 ID 核对终态。撤单提交前再次核对持仓，异步等待期间重新开仓则不发起该次撤单。
- 撤单回执不明、部分失败、cancel rejected 均不丢弃待处理记录；正常保护修复被冻结时，空仓收尾仍可执行。相同 owner 的撤单在处理期间去重。
- 交易所确认已成交使用独立的 `terminal/FILLED` 结果，不能被替换订单流程误当作撤单成功。只有终态证据和足够新的空仓快照齐备，才能删除保护记录；algo 仅 TRIGGERED/FINISHED/EXECUTED 仍需核对子单，不能算完成。

新增回归文件 `tests/execution/manage/test_closed_position_protections.py`，覆盖真实策略入口、持久文件重载、实际撤单 adapter/worker（网络响应模拟）、旧快照、并发续任务与归属隔离。验证命令：

```sh
uv run --no-project --with pydantic --with pytest python -m pytest tests/execution/manage/test_closed_position_protections.py tests/execution/manage/test_exchange_cancel_adapter.py tests/execution/open/test_intent_execution_strategy_shell.py tests/execution/manage/test_intent_execution_strategy_manage_shell.py tests/execution/open/test_add_position_protection.py -q
```

结果：232 passed，145 subtests passed（9.03 秒）；本次两个生产源码文件的 `git diff --check` 通过。整个工作区另有用户既有文件 `quant-lab/tests/research/test_review_p1_round3.py:731` 的末尾空行提示，本次未改动该文件。

本地验证不等于线上验收：未部署、未重启、未撤销生产订单，也未执行交易或 RESUME。R1 手机管理授权、R2 应急成交后的归属账本仍为独立问题。新实现对不能确认终态的订单保留记录；它没有把无法确认的状态伪装成已清理，也没有新增 algo 子单终态查询器。

## 后续：R1 手机管理授权与 30D / 365D

用户继续要求修复并先同步最新 App。现已用用户提供的登录方式连接源 Mac，发现实际 App 位于 `/Users/balen/projects/working/alert-personal/.worktrees/task-F-08`。其 11 个未提交 App 文件已保存为独立提交 `b07f16e`，推送 `codex/app-source-sync-20260917`，并拉入本机 `/Users/balen/projects/alert-personal`。源 Mac 主仓库的未提交服务端改动未混入本次修复。

R1 已在本地正式修复：`_management_operation_ids` 对所有含提交订单的管理任务持久化真实子单 CID；只有不产生交易所订单的操作仍使用尾号 99 标记。真实 Nautilus 模块下，partial_close、move_stop_loss、move_stop_to_entry、replace_take_profits 四类回放先出现 `reduction_authorization_missing`，修复后通过。授权检查及交易所/归属数量限制未放宽。

收益曲线增加 30D / 365D，tab 缩小；后端 `/v1/accounts?history_hours=` 上限扩大为 8760，按展示窗口抽取真实快照，缺账户的样本继续显式不完整。临时 PostgreSQL 的一年数据回放验证窗口头尾、最新点、点数上限与缺账户行为，契约已更新。

最终验证：App 类型检查、lint、27 suites / 228 tests 通过；服务端原回归 232 项 + 145 子项通过，真实 Nautilus 授权回归 7 项 + 30 子项通过，历史接口 13 项通过。本次源码 diff 检查通过。R2 归属账本仍未因此修复；未部署生产或更新手机安装包。App 交付记录见本机 `/Users/balen/projects/alert-personal/docs/2026-09-17-trading-app-fix.md`。

## 后续：用户管理指令优先，删除重复权限前提

用户指出 Hermes 仍把频道规则置于本人指令之上，要求按减枝原则修复。本次明确区分当前用户指令与自动频道操作：交互凭证认证后的用户管理指令，自身就是授权来源，不继承旧入场或旧频道的权限上限。

- CLI 的用户管理不再要求频道路由、原 entry ref、重复填写的用户身份与消息 ID；身份由认证确定，消息 ID 默认使用稳定 ref。自动信号的来源字段仍必填。
- 控制面对用户管理跳过频道归属查询，清除旧频道/入场上下文后再做幂等处理；旧 parent 不再要求存在、授权一致或决定目标账户。旧客户端不传方向时，仅从新鲜交易所视图中的唯一持仓方向解析；双向或过期证据明确要求 position_side，不报权限不足。
- 节点以持久化批准记录中的授权为准，删除对子单标签授权副本的重复比对。用户管理不调用机器人归属数量计算，以实际仓位和本次批准数量为界；频道批准不会因标签被改为 user 而升级。自主保护维护仍使用原归属范围。
- Hermes skill 同步删除“用户管理必须先查原入场归属”的冲突要求；用户当前明确管理指令优先于频道策略和原入场归属，无需再次索要已有授权。RESUME 仍需用户明确指令。

新增回归覆盖旧频道/旧 parent、无归属上下文、唯一持仓省略方向、双向/过期视图、同 ref 重试，以及已批准用户子单缺失标签、频道子单伪造 user 标签。信号凭证不能冒充交互用户的原有测试一并复测。

本轮验证：接口与 CLI 121 项通过；真实 Nautilus 7 项 + 34 子项通过；订单生命周期/管理/撤单 adapter 232 项 + 145 子项通过。没有增加 force/override 放行开关。R2 的自动频道归属账本对账仍为独立问题；不能把频道 m4404 的旧失败指令自动重解释为用户指令或重放交易。本次未部署生产、未更新手机安装包，也未操作现役订单。
