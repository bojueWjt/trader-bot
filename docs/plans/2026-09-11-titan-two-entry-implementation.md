# Titan 双入场修复记录

2026-09-11。本地代码修复，未部署、未 RESUME、未补历史订单。

后续已按用户授权完成提交并执行正式 preflight；因缺失生产 Redis 基线证据被门禁阻断，节点保持原版运行。提交号、候选包与现场状态见 [部署记录](2026-09-11-titan-two-entry-deployment.md)。交易计划已明确标记待部署生效。

## 最终规则

用户最后确认“两腿同等金额”。以同等 USDT 名义金额分配，而非同等币数或各一半止损风险。一次 `open --second-price <第二入场价>` 使用一个来源 ref、一份总预算、同一 intent 下的 01/02 两个订单 ID。第一腿保持信号的 market/limit 类型，第二腿为指定价格 limit。

设两腿参考价格为 p1、p2，共同止损为 s，总名义金额 N。每腿目标金额 N/2，数量为 N/(2pi) 并向下取精度；计划止损损失为 `N/2 × (abs(p1-s)/p1 + abs(p2-s)/p2)`。通过调和均价 `2/(1/p1+1/p2)` 复用原定量函数，一次应用权益、风险比例、资金和名义金额上限。实际金额会有数量步长误差；市价腿保留原市价成交语义，参考行情定量与提交前校验并不保证成交价或实际止损亏损。没有擅自新增 IOC 限价或滑点参数。

本版限定两个明确入场位、共同有效 SL。超过两腿、未知版本、无效 SL 或第二腿类型均拒绝，不回退成单腿。未注明独立条件的两腿在审批后一起提交，任一腿可先成交；部分止盈不重新分配第二腿预算。指定 expire-hours 时固化绝对到期时间；未指定时沿用 GTC，不发明默认有效期。

## 实现与评审问题闭环

- CLI 和 feeder/skill 一次传完整计划；删除首腿成交后跳过 e2 的指令。老 e1 不补 e2，不给历史信号换 ref。
- 控制面存储两腿定量快照。重复请求返回原 intent；改第二腿价格的同 ref 请求冲突。轮询时不重新取行情或扩大数量。
- 新批次复用单笔 open 的仓位准入。两腿在同一持久化提交操作中预先构造；第二腿不再作为独立 open 重新接受 position_exists 判断。原 zone 三档准入行为未修改。
- 复用持久化 inbox 和提交围栏。部分提交、结果未知时保留原记录和保护上下文，等待交易所证据；不自动重发未知腿。重启后的在途记录先走原恢复分支，不按新计划定量。
- 保护记录增加本计划订单 ID 与去重成交证据。SL/TP 和 parent-scoped 管理动作的数量取本计划净成交与同向可见仓位的较小值；只选择本计划保护单。手动订单 ID 不计入成交归属、不被撤销。
- 首腿已通过本计划退出单平仓、明确 close 或计划到期时，撤本计划剩余入场单。保留终结记录以处理撤单期间的迟到成交。成交先于仓位缓存更新时不误判平仓。HALT 在两腿之间到达时阻止后续新增风险，保留已发送腿的保护。
- 新增纯计算模块；没有新服务或数据库表。保护数量归属保存在现有 stash 中。发布清单、容器包、控制面模块与 feeder 固定哈希同步更新。

## 验证与边界

测试覆盖同等金额/总风险、多空与不同价格量级、首次即时成交、HALT、重复回报、部分提交重启、迟到成交、无效 SL/到期时间、管理单缩量、不同计划保护冲突、API 幂等及旧单腿/zone 回归。测试使用固定行情、模拟交易所和本机临时 PostgreSQL，没有连接下单端点。

验证命令与结果：

- `.venv-arch/bin/python -m pytest tests/execution/open tests/execution/manage tests/execution/test_entry_batch.py tests/contracts tests/test_v3_trade_routing.py tests/test_hermes_signal_feeder.py tests/nautilus/runtime/test_intent_execution_inbox.py -q`：430 passed，2 skipped，67 subtests passed。
- `.venv-arch/bin/python -m pytest tests/control-plane/api/test_operator_entry_batch.py tests/control-plane/api/test_operator_protection_and_dedup.py -q`：12 passed。本机临时数据库，出现一条现有 httpx 弃用提示。
- `.venv-arch/bin/python -m pytest tests/deployment/test_release_manifest.py tests/deployment/test_make_account_stall_release.py tests/deployment/test_make_container_bundle.py -q`：74 passed，33 subtests passed。
- `python3 /Users/balen/.codex/skills/.system/skill-creator/scripts/quick_validate.py hermes-profile/skills/trading/v3-trader`：Skill is valid；`git diff --check` 通过。

数量归属依赖本计划成交回报与缓存累计成交证据。与手动交易混用同一净仓时，这不是交易所提供的独立子仓；本次不引入所有手动平仓、反手与机器人仓位的完整逐笔归因系统。缺失的第二腿提交结果也不会仅凭“查不到订单”被推断为可安全重发。

发布相关测试原有夹具仍指向 0017，且遗漏已在代码中的执行归属/投影模块；本轮将其明确期望同步到当前 0018 和模块清单，并加入本次 entry_batch，保留精确集合校验。

上线必须按项目部署门禁执行：先部署能处理新类型及其 stash 的节点和控制面，再启用新版 feeder/skill。尚有在途双入场计划时，禁止直接降级到不理解批次归属的旧执行器。生产版本尚未改变，本记录不代表生产验收。
