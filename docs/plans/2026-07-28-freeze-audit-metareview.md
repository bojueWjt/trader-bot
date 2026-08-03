# 2026-07-28 发布冻结审计 元复核报告

- 输入：用户提供的 2026-07-20~07-28 审计结论（P0 ×3、P1/P2 ×8、测试结果、修复顺序四步）
- 核验方式：5 个只读 subagent 按代码域并行核验（策略 / 报表 / 部署基线 / live-mirror / smartness-p0）+ 2 个对抗审查（P0-1 机理、修复顺序），全部要求 file:line 级证据，只读不改
- 代码 ground truth：trader-bot 单仓库多 worktree——主树 `work/hermes-data-v3`、`trader-bot-integration`=`work/integration-acceptance-v3`、`.worktrees/smartness-p0`=`work/smartness-p0-v3`；**main 分支是空壳**（仅 engine/bridge，停在初始提交）；线上为服务器侧 hotpatch 分叉，不等于任何 git 分支
- **总体结论：现象层大体可信，"冻结发布"结论成立；但 3 条 P0 中 1 条根因机理被代码证据推翻、1 条失效机制推断错误，8 条 P1/P2 中 1 条整体不成立、2 条归因/定性需修正，修复顺序判 AMEND（含前提未证实与重复劳动）。不能按原文直接开工，尤其步骤 2 在拿到线上拒绝日志前不应动代码。**

---

## ⚠️ 必修项（照方案做会白做 / 做错 / 引入风险）

### 1. P0-1 根因机理被推翻：Nautilus RiskEngine 不存在"累计减仓额度"校验

两路独立核验（域核验 agent + 对抗审查 agent，分别从 GitHub tag 与 PyPI wheel 取 nautilus_trader **1.227.0**——与 `trader-bot-integration/infra/docker/nautilus/requirements.spike.lock.txt:33` pin 一致）得到相同结论：

- RiskEngine 对 reduce_only 的唯一校验是**逐单**的 `would_reduce_only`（`risk/engine.pyx:425-435` → `model/orders/base.pyx:962-995`，只比较单笔 `leaves_qty > position_qty`），**没有任何跨挂单的累计额度追踪**。SL 全量通过后，各档 TP（各 ≤ 仓位）逐单照样通过。
- 保证金账户整段跳过累计类风控：`risk/engine.pyx:678-679` `if account.is_margin_account: return True`。币安 U 本位合约即 margin account。
- 线上强制 HEDGING（`services/nautilus-node/app/node.py:329-332`，integration worktree），而 hedge 模式下币安 adapter **根本不发 reduceOnly 参数**（nautilus `adapters/binance/execution.py:753-756`），交易所侧 -2022 累计拒单路径也不适用。
- 逐单校验对 SL/TP 完全对称，无法解释"SL 存活、TP 死亡"的不对称。"接受后异步拒绝"的行为签名属于**交易所侧 OrderRejected**，不是 RiskEngine（其 deny 在命令处理内同步发生）。

**属实的部分**：SL 占全仓 + ΣTP 恰等于全仓的 2x 算术事实（`intent_execution_strategy.py:671-674, :978, :987-992, :1485-1543`）；`_PROTECTION_MAX_REVISION = 8` 与 `revisions_exhausted` 冻结（`:484, :716-721, :953-957`）；ATOM/RUNE 保护角色缺失的生产现象本身。

**替代假说（对抗审查排序）**：首选 **TP 限价越出币安 PERCENT_PRICE 价格带（-4131/-1013）**——完美解释不对称（SL 是 STOP_MARKET 无 price 字段不受价带约束）、解释逐 revision 重下逐次被拒打满 8 revision、解释 CL 成功（其 TP 价恰在带内）；次选**分档后单档名义低于 venue 下限（-4164）**——审计称"最小名义已排除"，但若只按全仓名义排除，分档后仍可能踩线。"CL 只配 TP 成功"是被混杂的对照（不同符号/价格/档位），对区分假说几乎无力；有区分力的实验（同符号 ATOM 只配 TP，或 CL 配 SL+TP）审计都没做。

**修复方向风险**：审计步骤 2"修复 hedge-mode 保护单内部 reduce_only 处理"**大概率零收益**（hedge 下 venue 侧本就无该参数，RiskEngine 校验本就放行），且有**主动风险**：`_hedge_position_id` 用 plan 的 reduce_only 语义推导 positionSide book（`intent_execution_strategy.py:1256-1261`，靠 `:1196-1204` 传原始 plan 保住语义）——修复若碰了这条传参路径，SELL 保护单会从"平 LONG"翻成"开 SHORT"。**先取 ATOM 任一 revision 的拒绝日志定因，再动代码**（RiskEngine deny 打 `SubmitOrder ... DENIED: <reason>`，venue reject 带币安错误码，一条日志即可终局）。

另：审计引用的行号 `:1263` 是 `_submit_management_plan`（运营管理指令的 make-before-break 路径），**不是**入场保护栈提交点；实际提交顺序在 `_sync_protection` 的 `:734-752`。

### 2. P0-3 的失效机制推断错误："事件继续 ACK + 投影停更"不成立

- 占位属实：主树 `services/control-plane/db/repository.py` 的 ProjectionWriter 四个方法抛 `NotImplementedError`（实际在 `:89-98`，`:82` 是类定义行）。
- 但当前分支（`work/hermes-data-v3`）的 `read_api.py` 仅 66 行、只有 snapshot 路由，**没有 events ingest 端点，ProjectionWriter 零调用者**。从当前 HEAD 部署的后果是节点推送 **404、投影链路整体缺失**，不是"静默 ACK + 投影停更"。即使按 smartness-p0 版调用方式（其 `read_api.py:312-326`），writer 抛异常传播为 HTTP 500 且不 commit——异常不被吞，事件也不会被 ACK。
- "当前主分支"表述错误：`git show main:...` 证实 main 上根本没有 control-plane 代码；占位文件属于当前 checkout 的 `work/hermes-data-v3`。已实现版在 `.worktrees/smartness-p0/services/control-plane/db/repository.py:80` 起（提交 `b24f675`、`72524cf`）。
- 基线缺失的核心结论仍然成立（见可开工项），但按审计的机制描述去设计 fail-safe（防"ACK 后丢投影"）会修错方向——真实风险是**部署错分支导致整个 ingest 面消失**。

### 3. P1"ref/channel 格式冲突"整体不成立（WRONG）

feeder 指示的 ref 格式 `tg-<signal_id>`（`.live-mirror/scripts/hermes_signal_feeder.py:100`），signal_id 实为 `sig-c<abs(channel)>-m<msgid>`（`bridge/freqtrade/signal_strategy/parser.py:77-82`），拼出 `tg-sig-c…-m…(-eN)`——与 CLI 校验正则 `_TG_OPEN_REF_RE`（`hermes-profile/skills/trading/v3-trader/scripts/v3_trade.py:64-66`）及 `_require_open_provenance`（`:116-156`）静态走查**完全兼容**，含 `-eN` 后缀与负号 channel 的 lstrip 处理。不存在"有效信号进控制面前退出"的正常路径。此条建议从修复清单删除。

### 4. P1"游标推进致信号永久跳过"定性错误

`hermes_signal_feeder.py:496-506` 捕获超时返回 `"(结果未捕获)"` 后推进游标属实，**但走到捕获阶段时 `run_hermes` 已成功返回 job_id（`:549-551`），信号已投递**。丢的是响应观察与 channel context，不是信号本身；此时重投反而制造双 job/双下单（代码多处防双 job：`:630, :646, :658`）。所有未投递路径（brain failure、TimeoutExpired）都计 attempts、耗尽后 `notify_blocked`（`:569-603, :654-668`）。真实缺口是"job 派发成功但 Hermes 中途死亡"的**结果观察盲区**——修法是补观察/告警，不是不推游标。

### 5. P2 markPrice 归因错文件：报表 mark_price 为空的上游是 event_mapper，不是 recorder

- recorder 遗漏属实（`.worktrees/smartness-p0/services/control-plane/tools/exchange_state_recorder.py:132-137` 不取 positionRisk 的 markPrice），但它只写 `exchange_state_mirror`，**不回填 positions_projection**。
- 报表 `/v1/positions` 的 mark_price 读自 `positions_projection`（smartness-p0 `api/read_api.py:933`），该列由 `position_reducer.py:142,166` 从**执行事件 payload** 写入——而 `event_mapper.py` 的 `_payload` 白名单（`:145-170`）**同样没有 mark_price**，这才是投影列为空的直接血缘。连带 `/v1/trades` 的 close_price 同源为空（`read_api.py:976`）。
- 只修 recorder 修不好报表；需同时补 event_mapper 白名单（或改报表数据源）。

### 6. 修复顺序步骤 3 前提未证实且处方有倒退风险

- "主机缺少 cron 执行器"**无仓库证据**，且与 07-24 部署基线直接矛盾：`docs/runbooks/2026-07-24-hk-deployment-baseline.md:157` 记录 `/etc/cron.d/trader-v3-trade-outcomes` 存在（root 644、每日 00:30 UTC、flock、输出至 /var/log/trader-v3/），死因判定是 `MAILTO=""` 吞错。真实死因（cron 未触发 vs 每天跑但静默失败）**未诊断**——迁 systemd 是在死因不明时换调度器。
- 可观测性倒退：balen 无 journald 权限（基线 §6 明列盲区），迁 systemd 不显式 `StandardOutput=append:` 落文件日志，错误会从"MAILTO 吞掉"变成"journald 里没人读得到"。
- freshness gate 深坑：`computed_at` 仅 insert 时 DEFAULT now()、upsert 不刷新（`docs/plans/2026-07-24-five-day-audit-fix-plan.md:70` 已由 review 证实），基于它的 gate 永久误报；必须用独立 job-run watermark。
- 回填有现成幂等脚本 `.worktrees/smartness-p0/scripts/analysis/trade_outcomes.py:321-336`（`ON CONFLICT DO UPDATE`），无需新写；但需明示两处 KPI 局限：aos_ 手工单无 intent 键控（近半成交名义额失明）、hedge episode 算法正确性已被 v2 review 另立任务。CLI 强制 `--db-url`（`:366`）写进 systemd unit 会固化密码暴露，需与 H3 协同。

### 7. 修复顺序步骤 1/2/4 含重复劳动与靶点错位（判决 AMEND）

- 步骤 1 一半是重做已完成的 S4：验证门 `scripts/verify_hk_deployment.sh`（盲区已修 67b7866）、`hk-gen-recreate-patched.py`、全量指纹基线与部署手册（`docs/runbooks/2026-07-24-*`）均已存在并实战用过。真正缺的只有一件事：**线上 merged strategy + read_api hotpatch 真身收编回仓库**。
- 步骤 2 靶点错位：07-27 JTO 自触发事件证明 reduce_only 拒单是**安全阀**（对空仓拒单才没误平）；真正让 BTC 裸奔的是保护单从未提交（stash supersede + 撤单无确认），对应修复 **S1 保护单持久化至今未开工**——这才是唯一未开工的资金保护 P0。hedge 映射与保护单测试骨架在 smartness-p0 已有，步骤 2 的 e2e 验收应扩展它们而非从零。
- 步骤 4 两项疑似已修完：feeder TimeoutExpired 计 attempts（4eafc6f，07-24 已部署）、PendingCancel 三态追踪 + 回看窗（2273c0c，线上 sha 已核对）。不先 diff 线上版本就照单开工是纯重复劳动。
- 修订版顺序（对抗审查输出）：**① 止血与取证**（S1 开工、Account B 误路由取证、ATOM 拒绝日志取证、维持手工保护 SOP）→ **② 收编砍半**（只收编 strategy/read_api 真身，复用既有验证门）→ **③ 保护单 e2e + F1 algo 撤单真单验证**（后者至今零验证，才是开放发布的闸门）→ **④ outcomes**（先诊断 cron 死因，freshness 用 job-run watermark，回填用现成脚本）→ **⑤ 收尾**（线上 diff 后的增量修复 + fail-closed 流程化）。

---

## 清理项（不阻塞，建议在文档/执行中修正）

| 条目 | 问题 | 修正 |
|---|---|---|
| MERGED 文件引用行号 | 写 `hk-root-window-20260724.sh:14`，实际在 `:16`（14 是 for 循环起始）；同清单 `:17` 还有 `read_api_MERGED.py` 审计未提 | 行号改 16；P0-3 应同时覆盖 read_api 双源分叉 |
| repository.py 行号 | `:82` 是类定义行，NotImplementedError 在 `:89-98` | 改行号 |
| gen-recreate "警告并继续" | 混淆两种情况：补丁**文件缺失**是 FATAL exit 1（`hk-gen-recreate-patched.py:68-71`）；仅 **BINANCE_EXEC_DST 参数未传**（容器路径探测失败）才 WARN 跳过（`:56`）。但风险链成立：`hk-root-window-20260724.sh:51-53` 探测 15s 超时留空 → 整轮"成功"却漏挂 binance 补丁 | 定性改为"路径探测失败静默降级"，修复点是探测失败改 FATAL 或强制人工确认 |
| template.html:106 | "渲染 0.00" 只对 KPI 成立（`money()` 走 `value \|\| 0`）；持仓卡片浮盈（`:271`）与标记价（`:113-118`）有 null 守卫渲染 "—" | 缩小表述范围到 KPI |
| missing_data=[] 定性 | `safe_query` 失败**会** append missing（`report_service.py:245-249`）；观测到 `missing_data=[]` + `trade_count=0` 说明查询成功返回 0 行（上游陈旧），非"查询失败被降级"。真正不留痕的只有 mirror 查询传字面量 `[]`（`:406-411`） | 吞错点改指 `:410`；freshness gate 才是对症修复 |
| Account B 误路由后果 | 缺 `--account` 属实（monitor `:792-793`；CLI 默认 account-a `v3_trade.py:412`），但后果是 B 的撤单 intent 在 A 账户 order_not_found 被 deny——**TTL 撤单静默失败、超龄单继续挂**，而非误撤 A 的单；已部署 F1 adapter 另有跨账户硬闸 | 修正后果描述；同文件 naked/exhausted 唤醒 prompt（`:787, :1047`）同样不带 --account，一并修 |
| event_mapper position_side 影响面 | 缺字段属实（`event_mapper.py:145-170` 白名单无该 key），但只影响**成交后 180s 宽限期**（`_position_side` 对空串路径返回 None → 进不了 recent_fill_keys，monitor `:1026-1035, :298-313`）；裸仓 key 本身取自交易所镜像、position_side 齐全。后果是每次新成交后一次性抢跑 set-sl 补单（24h 去重不刷屏），非裸仓检测整体失效 | 缩小影响面表述 |
| 测试数字 | ".live-mirror 60"、"Report 9" 实跑复现一致；"集成策略 10 passed 2 failed" 的 2 failed 及成因（hedge 开仓不设限 `intent_execution_planner.py:624-627`、make-before-break `:1263-1275` vs 旧测试契约）完全吻合，但 "10 passed" 口径本机任何切法都不复现（实测 19/12 passed + 5 skipped） | 数字标注环境口径 |

---

## 可开工项（核验通过，可直接落地）

**主树 `work/hermes-data-v3`（.live-mirror / report / scripts）**
- PendingCancel 48h 淘汰 still_open：真缺陷非误读——`order_lifecycle_monitor.py:1167-1169` 无条件 pop 超窗条目，与 `:1227` "still_open 保持每日重播"的设计自述矛盾；连带镜像 stale 时只跳过不推迟淘汰时钟（`:1205-1207`）。修法：淘汰前区分 outcome，still_open 豁免或转长期台账。
- TTL 撤单补 `--account`（monitor `:792-793`，连带 `:787, :1047` 两处 prompt）。
- 报表三件套：freshness gate（用 job-run watermark，勿用 computed_at）、缺日报告警 + 成功水位（`write_report` `:457-463` 现在只写文件）、mirror 查询失败接入 missing_data（`:410`）。
- gen-recreate 的 BINANCE_EXEC_DST 探测失败改 fail-closed（`hk-gen-recreate-patched.py:56` + `hk-root-window-20260724.sh:51-53`）；hk-root-window 预检清单补 `contracts.py`、`projection_actor.py`、`binance_execution.py`（当前预检通过仍可能在阶段 5 FATAL）。

**smartness-p0 worktree**
- event_mapper `_payload` 白名单补 `position_side` **和 `mark_price`**（`event_mapper.py:145-170`）——后者是报表 mark_price/close_price 为空的真正上游。
- recorder 补 markPrice 采集（`exchange_state_recorder.py:132-137`），作为镜像侧补全，与上条并行。

**基线收敛（P0-3 核心，范围砍半后）**
- 收编线上 `intent_execution_strategy_MERGED.py` + `read_api_MERGED.py` 真身回仓库（两者均不在任何分支的 git 历史中，`git log --all --diff-filter=A -- '*MERGED*'` 为空）；复用既有验证门/基线/gen_recreate，不重做。
- "冻结发布"结论：**成立**。保护角色缺失现象真实 + 基线缺失 + main 空壳，未收敛前从任何本地分支直接部署都不安全。

---

## 阻塞项（依赖外部才能定案）

| 条目 | 缺什么 | 动作 |
|---|---|---|
| P0-1 根因 | ATOM 任一 revision 的拒绝日志（RiskEngine deny 签名 `SubmitOrder ... DENIED:` vs 币安错误码 -4131/-1013/-4164/-2022/-4061） | root 窗口取 node 日志 / execution_events reason 字段；一条即终局 |
| trade_outcomes 死因 | cron 是否触发过（hk `/var/log/trader-v3/`、cron daemon 状态、`grep CRON /var/log/syslog`） | 先诊断再决定 cron 修复 vs systemd 迁移 |
| 7/21、7/24 日报缺失 | hk `/srv/trader-v3/reports/` 目录清单 | root 窗口顺手核 |
| "10 passed" 口径 | 含 nautilus_trader 的容器环境 | 收敛分支后在 docker 里重跑 |

---

## 结论：能不能直接开工？

**不能按原文开工。** 可以立即开工的是可开工项清单（monitor 三处、报表三件套、event_mapper/recorder 字段、部署 fail-closed、真身收编）；**必须先修正再开工**的是 P0-1（先取拒绝日志定因，禁止直接改 reduce_only 内部处理）、P0-3 的 fail-safe 设计（按"部署错分支 = ingest 面消失"设防，非"ACK 后丢投影"）、修复顺序步骤 3（先诊断 cron 死因）；**应删除**的是 ref/channel 格式冲突项；**应改写**的是信号游标项（补投递后观察，不是不推游标）。修复顺序采用修订版五步，S1 保护单持久化与 F1 algo 撤单真单验证提级为发布闸门。
