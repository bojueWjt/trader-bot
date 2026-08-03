# 2026-08-03 五日工作深度审计（2026-07-29 ~ 08-03）

- 审计方式：4 个只读 subagent 并行——代码演进（133 个提交实为本窗口收敛量）、线上交易行为（hk 生产 DB 直查）、线上服务健康（systemd/journal/docker/资源）、遗留项落实核对。线上全程只读。
- 数据截止：2026-08-03 08:47 UTC。审计进行时另一会话正在线上执行"BTC 风险 3% 重挂"与 cancel_order 热修，其后状态会继续变化。
- **总评：代码工程质量与线上基础设施这五天表现合格（测试全绿、调度 5/5 准点、零缺报、无计划外 HALT）；但出现三件必须立即面对的事：MIT 红线在文档声称冻结的情况下实际已上生产并直接制造了一次 30.5 小时裸仓；07-30 部署埋下的 cancel_order 枚举回归今晨引爆、靠未入库 hotpatch 救回；07-30 排期的修补项五天执行率为 0。另外交易结构性事实：bot 已基本不在交易，97.9% 名义额是治理盲区的手工单。**

---

## 一、重大发现（按严重度）

### 1. P0：MIT 改造实际已上生产（与验收文档矛盾），并直接导致 BZ 裸仓 30.5 小时

三个 agent 的证据拼合成完整链条：

- **MIT 在主树与生产**：`cba1dab`（07-29 21:44）把 TP 从 `LIMIT` 改为 `MARKET_IF_TOUCHED`（planner:598 `order_type="MARKET_IF_TOUCHED"`、strategy:1861），`0702863` 是其后裔；线上 `/srv/trader-v3/container-patches/intent_execution_strategy.py` md5 与主树已提交版**完全一致**——MIT 07-30 起就在生产运行。而 `docs/plans/2026-07-29-fix-round-acceptance.md` 的 07-30 更新称"LIMIT→MARKET_IF_TOUCHED 改造未进入发布物料/未进入 0702863"，**与仓库和线上事实直接矛盾，该文档需勘误**。
- **因果链到事故**：07-31 20:57，BZ short（intent `8fbc2847…`，信号入场估 99.9、TP 95.095、**无 SL**）实际成交在 89.91——成交时价格已越过 TP 位。旧 LIMIT 语义下这张 TP（short 平仓=BUY LIMIT 95.095，高于市价）会**立即吃单成交、当场止盈离场**；MIT 语义下触发条件已成立 → Binance 连拒 9 次，reason 原文一字不差：`{'code': -2021, 'msg': 'Order would immediately trigger.'}`。重试循环 9 次原价重发（注定全拒）→ `protection_frozen: "revisions_exhausted"` 静默放弃 → 该仓位无 SL 无 TP **裸奔 30.5 小时**，08-02 03:25 用户手工平仓（侥幸 +18.62）。
- **三层教训**：① 元复核红线"先取拒绝日志再动策略代码"被跳过，投机修复上线后第一个真实场景就出事；② 拒绝取证能力倒是生效了（-2021 reason 已完整落库），但"取到 reason 后无对策"——重试循环不识别 -2021 这类"重试无意义"的错误码，应改为立即市价平仓或升级告警；③ revisions_exhausted 冻结仍是静默的，无告警升级路径（07-28 审计就指出过 ATOM 同模式）。
- 注意区分：BZ 的 -2021 是 **MIT 新代码**的证据，不能直接回答老 P0-1（ATOM/RUNE 是 LIMIT 时代被拒，reason 至今未取到，-2021 不适用于 LIMIT 单）。老案根因仍开放。

### 2. P0：cancel_order 枚举回归毒丸（08-03 08:26–08:44，account-a 执行链中断 18 分钟）

- `0702863` 部署的 `contracts.py` 的 action 枚举**丢了 `cancel_order`**。潜伏 4 天，今晨 lifecycle-monitor 生成 BTCUSDT cancel_order intent 触发：整批 intent 校验失败、node-a 消费游标卡死 07:08、错误刷屏 512+ 条，account-a 全部已批准 intent 无法下发。
- 08:34/08:37 由另一会话线上热修（contracts.py 加回枚举、planner 同步改、两次手动重启 node-a），08:44 恢复 ACTIVE。**热修的 2 个文件是未入库 hotpatch 分叉**——P0-3"线上真身漂移"模式再次上演，需立即回灌仓库并查清 0702863 为何丢了该枚举值。
- 监控盲区实锤：整个事故期间**没有任何一条告警指向它**，靠人撞见。"intent 消费游标停滞/校验失败率"缺监控项。
- 审计留痕缺口：这次停机重启没有对应 HALT operator_command 记录。

### 3. P0（结构性）：bot 已基本退出交易，手工单占名义额 97.9%

窗口内成交 106 笔 / $61,669：**101 笔、$60,377（97.9%）是 aos_ 无 intent 手工单**（07-24 审计时为 48%，恶化一倍）。净实现约 **-201.9U**，最大两笔亏损（MU -196.1、BTC -95.3）都在手工域——归属、风控、outcomes、TTL 治理对其全部失明；交易所还挂着 CL/MU/ETH/BZ 十几张无主老单无人管。归属校验仍 shadow-only（33 条 shadow 中 42% would_reject）。**治理体系修得越来越好，但它治理的那部分交易正在消失**——这是产品层面需要用户决策的问题：要么把手工交易迁回 intent 通道，要么给 aos_ 域建独立的风控与记账。

### 4. P1：孤儿 reduce-only algo 止损在增殖

RUNE LONG 侧同价位 STOP 817@0.406 已堆到 **4 份**（节点每次重启对账收养失败就 +1 份），RUNE 多仓 08-03 已平，4 份全成孤儿；ETH 1.309@1802.76（07-24 老孤儿）仍在；BTC 0.004 无仓孤儿。未来同向重开仓即被连环误平——07-27 JTO 自触发是同族风险的既遂案例。F1 algo 撤单路径至今零真实验证。

### 5. P1：投影幻影仓全数存活 + smartness-p0 修复堆积未并入

positions_projection 12 open vs 交易所真实 3 个；ETH 0.193 / JTO 850 / SOL / TAO / BTC / BZ / HYPE / RUNE 幻影全在，SNDK/ZEC 数值漂移。而**修这个的工具和加固恰好都堆在 work/smartness-p0-v3 的 16 个未并入提交里**（rebuild_positions_projection.py、projection 单调性 reducer 加固 72524cf、market snapshot、halted 管理保留 3ec33d0）——"修复只在悬置分支"的唯一成规模堆积点，越拖 rebase 成本越高。好消息：orders_projection 的 `updated+filled_quantity=0` 缺陷窗口内零新增（07-24 守卫有效），事件流连续。

### 6. P1：redis 内存无上限逼近悬崖 + 排期执行率 0

- redis 5.48G（07-26 审计时 4.07G）、maxmemory=0、88 个僵尸命名空间；主机 free 仅 155Mi，swap 已用 5.7G/8G。清理脚本 `/srv/trader-v3/redis_purge_dead_instances.sh` 07-27 就绪至今**未获批执行**。再拖会进入 OOM/swap 抖动区。
- 07-30 排期的 4 大项 + 6 小项：五天内 **0 提交、0 上线动作**（全仓库 07-30 16:14 后零提交）。唯一进展（拒绝 reason 取证）是既有管道被动送上门的，躺库 3 天无人消费。

### 7. P2 清单

- a621ca8 operator-HALT 告警抑制条件是 OR 且无时效窗：长停机窗口内新故障的 halt_reason 会被静默。
- 0702863 幂等重放对授权证据全等比较：带新 X-Request-Id 的合法重试会 409，打击 client_ref 幂等初衷。
- d72afac systemd unit 以 balen 跑 root 属主路径（ExecStartPre/flock），fresh host 不可复现。
- 71ca656 的 MIT 数量容差精修未回主树：TP 部分成交场景会 cancel/replace churn。
- 日报"已实现盈亏"常态为 0（outcomes 归集滞后于手工平仓），报表诚实但不完整；INJ move_stop_loss 拒绝 reason 仍是裸 "REJECTED"（内部拒绝路径未接 evidence）。
- 大脑主通道 gpt-5.5 07-30~08-01 七次探活失败切备胎（08-01 后未复发）。
- tp-tombstone worktree 仍脏（.rej 残留，内容已被主树吸收，纯卫生问题）；4 个已等价并入的 codex 分支未删。

---

## 二、各域详情摘要

**代码域**（133 个提交收敛入主树，实跑测试全绿：.live-mirror 120、report 25、deployment+analysis 58、execution 77）：ffa2c92 并发安全修复干净；32124b0 完成了 recorder 双实现收敛（三对镜像文件字节级一致，07-29 报告的分叉项已解决）并带字节级一致性测试；0702863 授权闭环质量高（五层证据落库、两阶段 tombstone、14 挂载契约测试），合并完整性核验：order-auth/report/tp-live-combined 三分支完整并入可删，tp-execution 有 2 处容差精修遗漏。

**运维域**：调度线可靠（timer 5/5 准点、cron 遗迹清净、lock 修复经受验证）；报表线可靠（零缺报、KPI 与 DB 对账一致、mark_price 已非空——07-28 审计的报表失真问题实质解决）；监控线基本可靠但今晨证明存在关键盲区（毒丸事故零告警）。

**交易域**：intent 链路、授权硬拒绝（position_exists ×6、order_ownership_unverified ×8 全部正确拒绝）、事件流连续性、reducer 守卫都正常工作；无计划外 HALT；BTC tombstone 生效且无系统 TP 重挂。异常集中在第一节所列。

## 三、建议优先级（供决策，未经批准不动手）

1. **勘误 07-29 验收文档的 MIT 表述**，并决策 MIT 去留：保留则必须先修 -2021 对策（识别"重试无意义"错误码→市价平仓或升级告警）+ revisions_exhausted 告警升级 + 带回 71ca656 容差精修；回退则走 LIT+limit_price 备选路线（cdc4fc8 分支已有）。
2. **cancel_order 热修回灌仓库** + 查清 0702863 枚举丢失原因 + 补"intent 消费停滞/校验失败率"告警。
3. **批准执行 redis 清理**（脚本已就绪、双闸门、dry-run 已验）并决策 janitor 长效方案。
4. **并入 smartness-p0 的 16 个提交**（尤其 projection 加固 + rebuild 脚本），然后跑 F2b 清幻影仓。
5. 清 RUNE ×4 / ETH / BTC 孤儿 algo 单（F1 撤单路径顺便完成首次真单验证）。
6. 手工单治理产品决策（迁回 intent 通道 vs 独立 aos_ 风控域）。
7. 遗留小项批量清一轮（prune_state authalert、全史扫描窗口、operator-HALT 时效窗、幂等重放、unit 移植性、缺日报告警）。
