# Titan 双入场方案严格评审

日期：2026-09-11。对象：本目录 `2026-09-11-titan-two-entry-plan.md` 的首版方案及其拟复用代码；不是对当前工作区其他变更的评审。

**首版结论：FAIL，不能按首版直接实现/上线。** “一个来源、一份预算、两笔入场”方向可保留，但首版把多档提交基础误当成完整的按计划执行能力。发现 4 个 P1 和 2 个 P2。以下都是本地代码和离线探针结论，不代表本轮确认了对应生产事故。

后续状态：用户确认两腿同等名义金额后，已完成修正实现和本地测试，闭环及剩余边界见 [修复记录](2026-09-11-titan-two-entry-implementation.md)。保留本报告原始发现，不将设计评审结果改写为生产验收。

## P1-1：复用多档入口会改变已有仓位校验

首版要求“新计划准入沿用现有仓位校验”，同时建议提取 `_zone_ladder_order_plans`。

实际存在两套校验：

- `intent_execution_planner.py:927` 的 `_validate_position` 在已有仓位时拒绝 open，并检查对账状态。
- `intent_execution_strategy.py:9855` 的 `_entry_position_denial` 对 open 直接返回 None；多档 planner 在 3506 附近调用这一套。

对同一个非零 LONG PositionSnapshot 的离线调用输出：单笔 `OrderDenied(position_exists)`，多档 `None`。因此“复用”可能同时放开已有仓位上的新计划，并且没有自动继承单笔对账校验。

修正：先明确并测试新批次的准入条件和续执行条件；它们不是同一个判断。新批次不能借多档路径绕过既有约束；已批准批次恢复不能被本批次首腿成交误判成重复开仓。不要全局删除 position_exists。修正旧 zone 校验差异需单列影响验证，不能悄悄夹带。

## P1-2：现有保护同步不具备首版承诺的计划隔离

首版称“依据本计划累计净成交量重整 SL/TP”“复用现有保护单同步机制”。

实际：`_protection_position`（strategy:6791）按品种方向取仓位；`_sync_protection`（6090）把该仓位完整数量传给保护 planner。`_stage_entry_protection`（3181）还会移除同品种同方向、不同来源的旧 stash。

离线探针：输入合计 15 的仓位（假设机器人 10 + 手动 5），选择器返回 15，接口没有接收计划归属数量；新增来源的 stage 调用后旧来源 stash 已不存在。结合实际同步调用，不能承诺只保护机器人 10。

修正：保护单撤换/重试部分可复用；保护数量的来源不可直接复用。必须先验证现有成交和管理归属能否得到去重后的本计划净数量；缺失时明确判定不具备上线条件，不能用整仓数量代替。此处属于项目已要求的手动交易隔离，不是可推迟的附加功能。

## P1-3：首腿平仓后，第二腿与保护记录可能脱节

首版写“计划终结撤销剩余入场腿”，但未指定现有代码哪处承担这项责任。

`_sync_protection`（strategy:6071）在 position 为 None 时只清理保护订单并删除 stash；没有检查入场腿是否未成交。随后 `on_order_filled`（3646）对已无 stash 的入场成交只尝试同步同品种其他 stash，不会为这个计划重建保护记录。

离线调用确认 position=None 会删除 stash。由代码可推导：若首腿已止盈/止损平仓，而第二腿仍挂单，后续成交可能缺少本计划保护上下文。这一完整交易所时序尚未端到端复现，必须成为实现前的失败用例。

修正：现有执行记录必须保留“仍有在场/结果未知入场腿”的事实；不能把“现在空仓”等同“计划结束”。明确取消或终结后，待所有入场腿结果收敛、迟到成交完成处理，才清理保护上下文。按现有 HALT 政策处理保护暂停，不能在方案里同时承诺 HALT 下立即重挂和节点订单静默。

## P1-4：恢复路径没有证明能补齐部分提交

首版承诺首腿成交后崩溃、第二腿结果未知都能恢复，但已有通过测试主要证明完整提交不重复。

`_on_prepare_submit_result`（strategy:4431 附近）的 RECOVERY_REQUIRED 只有全部订单可查时确认；否则冻结等待。`_durable_intent_orders_exist`（8104）使用 all 检查。进程崩溃在两腿之间时，“第二腿从未发送”和“已经发送但未确认”不能仅凭查询不到区分。

修正：先对现有 inbox 做部分提交断电测试，记录其真实收敛行为。复用 `client_order_ids`、`exchange_confirmed_client_order_ids`；只有测试证明现有信息不足时才补字段。不能为了自动补齐而把短暂查不到订单当作未下单，也不能承诺所有部分提交都自动恢复。冻结后需用户行动的情况必须有明确回执。

## P2-1：提案扩展了状态与风控概念，未说明唯一真相

首版同时引入“计划状态”“预留风险”“分配策略版本”和新类型，却未说明它们与 trade_intents、inbox、execution_events、protection stash 的关系，容易形成第二套事实源。

控制面当前在读取已批准意向时调用 `_execution_order_plan`（read_api:898）；该函数可能根据行情展开订单。首版虽要求固定数量，却未指定持久化位置，也没覆盖预览/轮询/重放。

另有独立 `decision_gateway/zone_ladder.py`，其输出使用 `mode/plan_version`；当前 operator 路径使用 `type=zone_ladder`。仓库内引用搜索未发现 operator 路径调用该独立展开函数，不能把它当成已接通的复用实现。

修正：本轮不增加计划表、预留账本、调度服务或第二套状态机。首先尝试在现有 trade_intents.order_plan 保存不可变执行快照，inbox 管提交事实，成交事件管成交事实；状态展示由这些事实派生。两腿预算在审批时固定且只减不增，不引入动态预算回收。若仍需字段/迁移，逐项给出现有事实无法表达的失败用例。

## P2-2：改了订单语义，且把估算损失写成保证

首版提议把 market 变成带上限 IOC，却没有现有用户滑点政策作为依据。IOC 可能部分成交或零成交；这会改变 Titan 的“首入 CMP”。50/50 风险权重同样只是候选。止损触发价格也不是保证成交价，止损滑点和费用可能使实际亏损超过公式估计。

修正：把“计划止损损失估算”和“实际损失”分开。已有市场单语义先保留，沿用已验证的估值/滑点规则；需要新增价格边界或改 IOC 时单独确认。只留下必须的产品决策：两腿分配，以及原信号没有说明的未成交腿退出条件；不要创建更多可调旋钮。

## 最小落地顺序

1. **验证共同基础**：先复现上述准入、归属数量、空仓仍有第二腿、部分提交恢复四个场景。能修在现有模块的就就地修；不要先加新计划管理系统。
2. **增加一次提交两腿的表达**：一个 ref/intent、固定两腿数量、一个总预算。复用现有 OrderPlan 和 inbox 提交路径。类型/schema 的兼容分支集中在解析/展开入口，后续提交尽量不重复判断 zone/batch。
3. **接入 Hermes**：上面验证通过，再让 CLI 一次提交完整两腿并替换错误提示词。单腿/原 zone 回归必须通过。历史 e1 不自动补单。

复杂度预算：没有新的常驻服务、独立计划表、自动预算回收、通用 N 腿策略引擎或全局加仓开关。若“本计划数量”需要重大所有权改造，先报告这个真实前置条件，不能以小修名义启动重构或上线整仓替代实现。

## 本轮验证

- 直接调用现有函数的 4 项离线探针确认：准入不一致、旧来源 stash 被替换、选择合计仓位、空仓删除 stash。探针使用内存 fixture，无网络或下单。
- 回归命令：`.venv-arch/bin/python -m pytest tests/execution/open/test_intent_execution_planner_reconciled_open.py tests/execution/open/test_intent_execution_strategy_shell.py tests/nautilus/runtime/test_intent_execution_inbox.py tests/execution/manage/test_take_profit_tombstone.py -q -k 'ladder or submit_crash or same_source_message_entry or new_entry_submit_failure or duplicate_open_denied_when_venue_reports_same_side_position or partial_fill_does_not_restore_symbol_freeze'`
- 结果：**17 passed, 202 deselected**。这是当前行为基线，不是双腿安全证明；P1-3/P1-4 的完整故障时序尚需新用例。
- 本轮仅调整方案文档，未修改执行代码、提示词或生产配置。
