# G2 P1 八审（终审）
## 八审判定表
2026-09-11，GPT-6 独立八审；先落盘再探针。S01–S26：closed 18 / partial-P2 8 / open 0；新增 S27/S28 后：**closed 18 / partial-P2 8 / open 2**。历史轮次判定原文保留，当前以本表为准。

| ID | 状态 | 复现命令与输出摘要 | 是否阻断 P1 |
|---|---|---|---|
| S01 TP 当前可成交性 | closed | T:test_s01_* 通过；当前 last 不满足 TP 时不成交，A 22/22。 | 否 |
| S02 funding 证据与账务 | partial-P2 | T:test_funding.py 与 test_c2_loader_missing_funding_and_unknown_rules 通过；AB 保留 U03/引擎余额 unsupported。 | 否；U03、变周期完整性、引擎余额留 P2。 |
| S03 分钟内部启动 | closed | T:test_s03_* 与 test_c3_s03_b_mark_bars_filtered_by_t_start 通过，启动 bar 不参与。 | 否 |
| S04 持仓截止/TP 余量 | partial-P2 | T:test_c1_s04_hold_end_precedes_funding_and_b_truncates、T:test_s16_exposure_uses_observation_boundary 通过。 | 否；B 账户/资金事件独立证明仍 P2。 |
| S05 覆盖/规则入口 | closed | T:test_s05_loader_unknown_quality、test_s05_partition_unknown_ohlc_is_quarantined 通过，缺列/null/false 拒绝。 | 否；四审未知质量反例闭合。 |
| S06 入湖冲突/bronze | partial-P2 | T:test_s06_* 与 test_s14_revised_source_supersedes_old_silver_days 通过，未知重复隔离、旧日分区失效。 | 否；bronze 深度重放、多来源版本化仍 P2。 |
| S07 队列/post-only/账户 | partial-P2 | T:test_s07_*、T:test_s07_management_stream_rejected_at_request_boundary 通过，管理流显式拒绝。 | 否；C01/E13、完整预留生命周期仍 P2。 |
| S08 multiplier | closed | T:test_s08_multiplier_scales_pnl_fees_exposure 与 test_c1_s08_public_simulate_multiplier 通过。 | 否 |
| S09 as-of 等号/null/序号 | closed | T:test_s09_* 与 test_c1_s09_same_name_sequence_columns 通过，等号 null 不回退。 | 否 |
| S10 A/B 解释门禁 | partial-P2 | T:test_s10_* 通过；AB 无未知码，余额证据仍 not_run。 | 否；独立逐事件经济证明、B 定型仍 P2。 |
| S11 测试/CLI 门禁 | partial-P2 | T 253 passed；指定R 89 passed；A22/22；B与AB报告见八审证据。 | 否；原P2边界保留，全绿不豁免新反例。 |
| S12 不可变输入/构建身份 | partial-P2 | T:test_s12_*、test_replay.py通过；当前登记5/5匹配；历史迁移证据限度保持。 | 否；版本化湖/供应链及历史不可变登记仍P2。 |
| S13 事件因果/精度/step | partial-P2 | T:test_s13_*、R 两事件精度回归通过；静态 order_rank 死表归旧序列化残余。 | 否；共享容量/当前价/预留独立重放仍 P2。 |
| S14 源修订及逐行来源 | closed | T:test_s14_* 与 R 质量入口通过；来源缺列/null/旧 SHA 保持拒绝。 | 否 |
| S15 双流缺口/质量优先级 | closed | T:test_review_p1_round3.py 全5项通过，两流最早缺口、首尾缺及质量优先不退化。 | 否 |
| S16 B hold 暴露窗口 | closed | T:test_s16_exposure_uses_observation_boundary 与 horizon 等号共5项通过。 | 否；仅闭合该暴露反例，不扩大 B 验收。 |
| S17 显式 fraction 一致性/精度 | closed | T:test_s17_explicit_fractions_cannot_bypass_policy_or_precision 通过；独立162分配组合18接受/144拒绝。 | 否；合法推导值保持接受。 |
| S18 A15 无 expired 仍映到期 | closed | 原样 P18 exit1/ContractError，含完整实际事件集合；独立映射2688组合、160异常均回报集合。 | 否；三条未命中分支均闭合。 |
| S19 显式 TTL 绕过 policy | closed | T:test_s19_explicit_ttl_cannot_bypass_policy_fallback 通过；两来源16项独立探针6接受/10拒绝。 | 否；缺省/相等合法，偏离与非法域被拒。 |
| S20 t_start 推导对账 | closed | T:test_contract.py::test_request_validation 通过；直接 datetime 比较拒绝显式偏移。 | 否；观察窗的新截断问题另列 S25。 |
| S21 删失优先级落实 | closed | T:test_s21_primary_censor_follows_contract_priority_not_check_order 通过，A/B 均 RULE_HISTORY_MISSING、两 coverage 位 false。 | 否；常量推导主因断言同过。 |
| S22 显式空 entry 分配被当缺省 | closed | 原样 P22 exit1/ContractError（长度不符）；空entry/非空TP的空分配均拒绝，无TP时空元组合法。 | 否；不再静默修补显式空分配。 |
| S23 DF_DECIMAL 常量生效 | closed | T:test_constants_effective.py 6项通过；内存改(30,10)并重载，batch/event schema 同变，常量推导断言通过。 | 否；DF_DECIMAL 已真实接入。 |
| S24 B12 推导/必填未落实 | closed | 原样P24：259260.0 259260 True；小政策无hold110/有hold160；省略research字段实际拒绝。 | 否；构造与校验共用derived_window_s。 |
| S25 horizon 亚秒绕过 | closed | 原样P25：exit1/ContractError；补54次模型校验及999999个亚秒比较域验证。 | 否；逐值对账与封顶均精确比较timedelta。 |
| S26 未知 multiplier | closed | P26真实调用loader及simulate(A/B)：None→rules_known=False/RULE_HISTORY_MISSING/netNone；已知1→net5。 | 否；未知不再静默当1执行。 |
| S27 loader启动时间重复推导 | open | P27：同一resolved_t_start，省略t_start→unevaluable；显式写相同值→tp_hit/net5。 | **是**；loader漏latency，与contract/kernel分叉。 |
| S28 A首bar网格亚秒截断 | open | P28：完整行情，整分钟+1至999999µs误判BAR_GAP；0及+1s对照tp_hit/net5。 | **是**；同侧展开与缺口计算精度不一致。 |
## 八审证据与新增必修
命令目录 quant-lab；均设 PYTHONDONTWRITEBYTECODE=1，探针/CLI 另设 PYTHONPATH=src；pytest 加 -p no:cacheprovider。T=`.venv-g2/bin/python -m pytest tests/market -q`：253 passed/13.65s/exit0；R=`.venv-g2/bin/python -m pytest tests/market/{test_outcome_kind,test_constants_effective,test_contract,test_review_fixes}.py -q -v`：89 passed/4.92s/exit0，无 skip/xfail。A/B replay 原命令：A22/0/exit0、B12/10/exit1，双方22例 replay=True；nautilus_adapter report --reps 1：exit0，MATCH12/命令延迟3/跳空1/同刻优先4/GTD1/流动性1，无 UNEXPLAINED/NOT_RUN/A_GOLD_FAIL，未写 AB 报告。
P24 原样从七审正文抽取命令执行：exit0，259260.0 259260 True；无 hold 窗口确为60+259200。另以进程内登记政策 ttl60/research50/max200 独立核算，无hold=110、有hold100=160，build_request 与正常 model_validate 一致；缺 research_horizon_s 实际抛 ValidationError(missing)，没有默认值（不把模型层 ValidationError 误述成 ContractError）。构造与对账均调用 contract.derived_window_s。
P25 原样执行：exit1/ContractError，14d+1µs 被安全上限拒绝；原命令第一处即退出，caller 由补探针单独验证。hold=None/100 × 推导policy/封顶policy/封顶caller × 偏移{−1,0,1,2,123456,500000,999998,999999,1000000}µs=54次完整模型校验，全部符合独立期望；policy推导只收0，caller封顶只收−1/0。再穷举1..999999µs的 timedelta 比较域，逐项满足不等于推导值且大于封顶；此为比较运算穷举，不冒称999999次完整模型验证。
P26：在 P27 的内存 loader 输入中，固定显式合法 t_start，仅将规则 multiplier 改 None/1/2；None 时实际 rules_known=False，公共 simulate(A/B) 均 RULE_HISTORY_MISSING/netNone；1 时均 rules_known=True/net5，2保持已知（经济量/保证金随乘数变化）。未知值虽内部用占位 Rules() 承载，执行在 rules_known 门前删失，不能冒充乘数1正常标签。现有 test_s26 仅查源码，本轮用实际调用补足行为证据。
S27（open，P1）：同侧启动时刻分叉。contract.py:406/456 按 t_dec+latency 解析，A/B 调用 resolved_t_start；execution.py:133 却用 `(req.t_start or req.t_dec)` 装载并选规则，遗漏 latency。正常构造的等价请求（latency60，t_start=None 或显式T+60，horizon=T+120），同一份T+60起生效规则及完整窗内bar，前者 rules/bars=False、unevaluable/netNone，后者 True/True、tp_hit/net5。不是版本化湖或B定型问题；应统一使用已解析启动时间再减 window_before_s，并验证两种表达的输入窗/结果一致。
P27 完整只读复现（所有文件读取替身仅在进程内提供合成表，不是实际湖/联网验证；patch 在 simulate 前退出）：`.venv-g2/bin/python -c 'exec("import datetime as dt,json\nfrom pathlib import Path\nfrom unittest.mock import patch\nimport polars as pl\nfrom tests.market.test_review_p1 import e03\nfrom quant_lab.market import contract as c,execution as x,partition_check as pc\nq,_=e03();t=q.t_dec\np=c.ExecutionPolicy.model_validate({**c.resolve_policy(q.policy_version).model_dump(),\"version\":\"audit-latency\",\"latency_s\":60})\nreg=c.load_policy_registry();reg[p.version]=p.content_hash\nbase={**q.model_dump(),\"policy_version\":p.version,\"policy_hash\":p.content_hash,\"t_start\":None,\"horizon_source\":\"caller\",\"horizon_end\":t+dt.timedelta(seconds=120)}\nrules=pl.DataFrame([{\"instrument_id\":q.order_plan.instrument_id,\"effective_from\":t+dt.timedelta(seconds=60),\"effective_to\":None,\"tick_size\":\"1\",\"step_size\":\"1\",\"min_notional\":\"0\",\"multiplier\":\"1\",\"funding_interval_hours\":8,\"status\":\"TRADING\",\"source\":\"audit-synthetic\"}],schema=pc.RULES_SCHEMA)\ndef read(path,*args,**kwargs):\n    if \"fundingRate\" in str(path):\n        return pl.DataFrame({\"calc_time\":[t],\"funding_rate\":[0.0],\"funding_interval_hours\":[8]})\n    return pl.DataFrame({\"open_time\":[t+dt.timedelta(seconds=60)],\"open\":[100.0],\"high\":[105.0],\"low\":[100.0],\"close\":[105.0],\"volume\":[100.0],\"gap_flag\":[False],\"ohlc_valid\":[True],\"source_sha256\":[\"audit\"]})\nwith patch.dict(c.POLICIES,{p.version:p}),patch.object(c,\"load_policy_registry\",return_value=reg):\n    for start in (None,t+dt.timedelta(seconds=60)):\n        r=c.ExecutionRequest.model_validate({**base,\"t_start\":start})\n        with patch.object(Path,\"exists\",return_value=True),patch.object(Path,\"read_text\",return_value=json.dumps({\"source_sha256\":\"audit\",\"check_status\":\"ok\"})),patch.object(pl,\"read_parquet\",side_effect=read),patch.object(pc,\"load_rules\",return_value=rules):\n            m=x.load_market_from_lake(r,lake_root=\"/audit-memory-only\")\n        out=x.simulate(r,market=m)\n        print(start,r.resolved_t_start(p),m.rules_known,m.bars_complete,out.outcome_kind,out.net_pnl)\n")'`。
S28（open，P1）：A 内部对“首根可用完整bar”有两种实现：kernel_a.py:196–198 的展开精确比较 open_time>=t_start；:240–242 的缺口计算先 int(timestamp)，误把整分钟+亚秒当作整分钟，first_expected=t_start。完整双流bars在T/T+60/T+120，horizon=T+180，t_dec=T+{1,500000,999999}µs 时均 BAR_GAP/unevaluable/netNone/事件0；T及T+1s对照均tp_hit/net5/事件14。应以完整时间精度求网格ceil并与展开规则一致；这是S03原整秒反例之外的新亚秒反例，不是S25请求封顶修复失败。
P28 完整原生复现（正常 model_validate + 公共 simulate；无 policy/mock 绕验证）：`.venv-g2/bin/python -c 'exec("import datetime as dt\nfrom tests.market.test_review_p1 import e03\nfrom quant_lab.market import contract as c,execution as x\nq,m=e03();t=q.t_dec\nfor us in (0,1,500000,999999,1000000):\n    r=c.ExecutionRequest.model_validate({**q.model_dump(),\"t_dec\":t+dt.timedelta(microseconds=us),\"t_start\":None,\"horizon_source\":\"caller\",\"horizon_end\":t+dt.timedelta(seconds=180)})\n    bars=[c.Bar(open_time=t+dt.timedelta(seconds=s),o=100,h=105,l=100,c=105,volume=100) for s in (0,60,120)]\n    market=c.MarketView.model_validate({**m.model_dump(),\"last\":[],\"mark\":[],\"bars_last\":bars,\"bars_mark\":bars,\"bars_complete\":True})\n    out=x.simulate(r,market=market)\n    print(us,out.censor_reason,out.outcome_kind,out.net_pnl,len(out.canonical_events))\n")'`。
同族独立穷举：两来源×entry/tp各9输入（省略/None/[]/()/[1]/[.5]/False/0/空串）162项，18接受/144拒绝；TTL两来源×8输入16项，6接受/10拒绝。4种终止事件存在位×3种出场腿存在位×3成交状态×7删失值=2688项，独立首匹配期望差异0，160异常全部回报完整实际事件集合；七值在选择器合成域可达，真实22夹具仍仅六值，C06不升级。
同侧重复清单：TTL 在 before解析/after对账/build_request 三处，当前 None 分支一致；t_start 在校验/resolved_t_start/build_request/loader 四处，实质分叉见S27；A deadline 在 timeline/submit_entries 两处，当前一致；首完整bar在展开/缺口两处，见S28；OHLC 在 vision._parse_bars 与 partition_check.check_bars 两处，后者额外检查volume有限及null，loader必须经check_status，不据预体检mask宣称通过；DF边界check_decimal仍硬编码38/12，当前与DF_DECIMAL一致，后续改标度须同步。B pre_process/flush 资金费重复（flush不调引擎余额），增量账本/_replay_ledger/暴露重放三份，以及hold截断/暴露cutoff两处，均记录为既有S02/S04/S10/S13 P2风险，未证实本轮退化。A/B间sizing/TP/费用/funding/路径独立实现符合ADR，不计缺陷。
常量/截断审查：AST列出全部9模块103处or及int/round调用并结合全文审阅；新问题S28独立于G2自查。DF_DECIMAL进程内改(30,10)后reload execution，batch.net_R/event.price均Decimal(30,10)；六条constants_effective实跑通过，SETTLEMENT_QUANTUM/RATIO_QUANTUM/EXIT_LEG_ORDER/SPIKE_K/SPIKE_MIN_SAMPLES/INTERVAL_SECONDS/CENSOR_PRIORITY可追溯。BAR_COLUMNS/FUNDING_COLUMNS/_KEY为描述残余，OUTCOME_KINDS为目录，B order_rank沿S13；未把它们误报为已生效规则。settlement_quantum政策字段与固定金额常量同值，未声称支持另选量化。
偏差/边界：S27按是否显式写同一启动时刻改变样本可评估性，S28按亚秒相位错误排除有效样本，均会污染下游样本组成；未发现本轮修复新增未来决策特征读取、幸存品种筛选或凭据/私有API入口。八项partial-P2（S02/S04/S06/S07/S10/S11/S12/S13）原回归未退化。研究窗曲线v2已读（3d15.3%、5d6.0%）；按用户范围，数值待裁及后增§5.18配对/估值要求不作为本审阻断。
DoD（GOAL-2 §7）：M-03依G0亲跑已解除，不联网重验；M-04–M-09本地测试、A≥10金标/不变量及AB报告条件满足。M-10必修闭合因S27/S28不满足，P1 fail；S24/S25/S26闭合不抵消独立新反例，不因B既有差异或3d/5d数值选择判失败。
证据身份：HEAD 7353904；以quant-lab相对路径排序，market顶层*.*、tests/market递归*.py、episode/*.json共52文件，连接“路径:sha256\n”后SHA256=aec245a94e9d7b0ca3a99c4512fcc9ef3c63096fec5f775d083eeb135b7bdfd5；探针前后摘要相同，非原子快照，不证明历史未变。当前5个policy登记hash逐条匹配，历史迁移限制仍S12。
审查边界：必读文件与全部market/*.py已读；主控GPT-6独立完成，无其他模型调用。仅编辑本文件，未改代码或测试；新增探针全在进程内，授权pytest的临时产物由tmp_path管理；未联网、未做生产操作。七审至一审正文及历史判定逐字保留，顶部表为八审当前结论。
## 七审证据与新增必修
命令目录 quant-lab；全部设 `PYTHONDONTWRITEBYTECODE=1`，CLI/探针另设 `PYTHONPATH=src`，pytest 加 `-p no:cacheprovider`。T=`.venv-g2/bin/python -m pytest tests/market -q`：251 passed/14.49s/exit0；R=`.venv-g2/bin/python -m pytest tests/market/{test_outcome_kind,test_constants_effective,test_review_fixes,test_review_p1_round4,test_review_p1_round3}.py -q -v`：75 passed/6.66s/exit0，无 skip/xfail。表内 T:/R: 指对应命令中的测试选择器。
A/B=`.venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A`（B 换末参数）：A 22/0/exit0，B 12/10/exit1，双方22例 replay=True；AB=`.venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1`：exit0，MATCH12/命令延迟3/跳空1/同刻优先4/GTD1/流动性1，UNEXPLAINED/NOT_RUN/A_GOLD_FAIL=0；未重写报告。
原样六审 P18/P22（命令完整保留于下方历史段）均 exit1：P18 实际事件集合为 accepted/cancelled/filled/submitted/tp_triggered/working；P22 为 entry_fractions / tp_fractions 长度与计划不符。空事件、无closed、closed无出场腿三类异常均逐条核验，不把异常对象充作真实金标。
独立同族穷举：4种终止事件存在位×3出场腿存在位×3成交状态×7删失值=2688组合；按契约独立分支求期望，差异0，160异常全部比对完整sorted事件集合；真实22夹具六值可达，filled_closed只在合成close腿可达，维持C06。两来源×entry/tp各9输入（省略/None/[]/()/[1]/[.5]/False/0/空串）=162项，18接受/144拒绝；TTL两来源×8输入=16项，6接受/10拒绝；无TP的显式空分配正常保留。
静态逐项核对 ExecutionRequest 实际20字段（六审“22字段”计数不沿用），21个非法身份/枚举输入全拒；原始输入全进request_canonical，推导字段核对TTL/fractions/t_start/horizon。AST枚举market全部105处or表达式并审阅：datetime/对象缺省、首拒因、P2来源边界与新S25分开；空分区missing现保留None。无运行读取的BAR_COLUMNS/FUNDING_COLUMNS/_KEY为旧描述/残余，OUTCOME_KINDS为测试目录，不是阈值或分派规则；B order_rank仍归原S13。
S23：execution.py:26 的 DEC=pl.Decimal(*DF_DECIMAL) 被BATCH_SCHEMA及EVENT_STRUCT复用。仅进程内将c.DF_DECIMAL改(30,10)再importlib.reload(execution)，net_R与event.price均Decimal(30,10)，直接运行test_df_decimal_drives_batch_schema通过；原文件断言用pl.Decimal(*c.DF_DECIMAL)，非硬编码38/12。SETTLEMENT_QUANTUM/RATIO_QUANTUM/EXIT_LEG_ORDER/SPIKE_K/SPIKE_MIN_SAMPLES/INTERVAL_SECONDS/CENSOR_PRIORITY均有调用点及生效断言；未把原P2排序残余宣称完成。
B11正向：合法caller与policy能输出对应诊断，普通整秒篡改与超限拒绝；仅改horizon_end而事件不变时公共simulate(A/B)两条trace_hash均变化。caller经build_request的显式horizon_end参数自动标注，直接模型需声明；亚秒例外见S25。G3 config_id记账属G3，本审未代验。
S24（P1，contract.py:263、418、471）：B12规定无hold时仍为t_start+ttl+research_horizon_s，实际两处都只用research_horizon_s；不是数值待定问题。内存登记合成policy（ttl60/research50/max200），build_request得50s，契约110s在policy路径反遭ContractError；同一行情A的50s为right_censored/netNone，110s caller为tp_hit/net5。另research_horizon_s被宣告必填，模型却允许省略并补14d；应显式登记占位值。现有两条horizon测试把漏TTL的式子写成期望，不能证明合规。
P24最小原生复现：`.venv-g2/bin/python -c 'from tests.market.test_review_p1 import e03; from quant_lab.market import contract as c; q,_=e03(); p=c.resolve_policy(q.policy_version); plan=q.order_plan.model_dump(); plan["expiry"]={"entry_ttl_s":60,"max_holding_s":None}; row={k:getattr(q,k) for k in ("episode_id","graph_version","decision_snapshot_hash","t_dec")}; row["order_plan"]=plan; r=c.build_request(row,policy_version=p.version,policy_hash=p.content_hash,risk_budget=q.risk_budget,market_manifest=q.market_manifest); print((r.horizon_end-r.resolved_t_start(p)).total_seconds(),60+p.research_horizon_s,c.ExecutionPolicy.model_fields["research_horizon_s"].is_required())'` → 1209600.0 1209660 False；该默认本应触发上限拒绝，不能靠漏TTL逃过。
S25（P1，contract.py:414–420）：int(total_seconds())先丢微秒再对账/封顶，原datetime却进内核。hold=None/3600两推导路径各测−1µs/0/+1µs/+999999µs/+1s，均拒/收/收/收/拒；caller上限同五点为收/收/收/收/拒。修复须精确比较datetime/timedelta，不得接受偏移后继续标policy；上下界与对账需用同一时间精度。
P25最小复现：`.venv-g2/bin/python -c 'import datetime as dt; from tests.market.test_review_p1 import e03; from quant_lab.market import contract as c; q,_=e03(); p=c.resolve_policy(q.policy_version); d=q.model_dump(); d.pop("horizon_source"); d["horizon_end"]=q.resolved_t_start(p)+dt.timedelta(seconds=p.max_horizon_s,microseconds=1); r=c.ExecutionRequest.model_validate(d); print(r.horizon_source,(r.horizon_end-r.resolved_t_start(p)).total_seconds()); d["horizon_source"]="caller"; print(c.ExecutionRequest.model_validate(d).horizon_source)'` → policy 1209600.000001 / caller，exit0；两次均应拒绝。
policy迁移核验：5条当前content_hash与policy_hashes.json逐条相等且唯一；删除新增research_horizon_s后重算的旧schema哈希5条全不同，携这些旧哈希的请求5条全拒。版本键仍为原5个v1名；只能确认schema重登记与拒旧哈希生效，不能凭“全换哈希”宣称完整B4历史迁移合规。源码/注册表untracked且无旧登记快照，历史一一对应证明不足，保留S12既有partial-P2；本轮未证明旧已有数值被改且旧请求获准执行，不另升级阻断。
偏差/凭据：S24能改变标签成熟性，筛掉删失样本时可改变研究样本组成；S25允许观察边界外行情进入，是新增窗口口径漏洞。未发现本轮新增决策前视读取、幸存品种筛选或凭据/私有API入口；公开客户端可配置hosts并非硬网络隔离。S02/S04/S06/S07/S10/S11/S12/S13八条partial-P2回归未见退化，保持原归属，不借新问题重新升级。
收敛判断：S18/S22及S23已闭合，新增缺陷集中于B11/B12观察窗接缝，未再发现同族分配/映射扩散；但“接缝集中”不等于机制闭合。审查中另读到§5.15 B13数值裁定；依用户指定范围，不把后增取值/真实括号曲线要求作为本轮新门槛，S24/S25在§5.14内已成立。
DoD（GOAL-2 §7）：M-03按用户指令及§5.12记G0亲跑已解除，不计未决；本轮M-04–M-09本地测试、A≥10金标与不变量、AB报告均有实跑证据。M-10“必修闭合”因S24/S25未满足，故P1不通过；研究窗数值待定及B定型留P2均不构成本轮否决理由。
证据身份：HEAD 3a57094；market顶层文件+tests/market递归Python+episode JSON共52文件，按仓库相对路径排序连接“路径:sha256\n”后SHA256=8b99c0c8203f94a3d37698fe4a42a26146b3143433e7b9b43828f1d5c9533096；两次文件摘要比对无变化（非原子快照，不能证明历史未变）。
边界：必读文档、全部market/*.py与指定三测试文件已读；只写本文件，未改代码/测试，探针扰动仅在子进程内，pytest临时产物由其tmp_path管理；不联网、不调用收费模型、不做生产操作。历史六审至一审段逐字保留，原footer仅替换为本轮唯一裁决。
## 六审证据与未闭合项
命令目录 quant-lab；均设 PYTHONDONTWRITEBYTECODE=1，CLI/探针另设 PYTHONPATH=src；pytest 加 -p no:cacheprovider。T=`.venv-g2/bin/python -m pytest tests/market -q`：242 passed/14.83s/exit0；R=`.venv-g2/bin/python -m pytest tests/market/{test_outcome_kind,test_review_fixes,test_review_p1_round4,test_review_p1_round3}.py -q -v`：66 passed/6.10s/exit0，无 skip/xfail。
A/B=`.venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A`（B 换末参数）：A 22/0/exit0，B 12/10/exit1，双方22例 replay=True；AB=`.venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1`：exit0，MATCH12/命令延迟3/跳空1/同刻优先4/GTD1/流动性1，未知码/NOT_RUN/A_GOLD_FAIL=0；未重写 AB 文档。
原 N 从下方五审代码块抽取 Python 原样执行：IOC 事件均 submitted/accepted/cancelled/closed，按 B10 枚举④命中；86400 正好等于 fixture-zero-v1 默认 TTL，不能为了“后者被拒”误拒合法值；1≠86400 被拒。独立 TTL 的 plan/policy 两来源×None/相等/偏移/0/1.5 共10项，正常域与对账一致。
S18 残余位置 contract.py:569–570：无 closed 的有成交结果抛错文字仅“未删失且有成交却无 closed：违反不变量，无法映射 outcome_kind”，缺实际事件集合；空事件与 closed 无出场腿两条异常分支均附集合。已逐分支确认不再静默兜底，但 B10 要求所有未命中路径附集合，不能以异常对象为由豁免其诊断要求。
P18 复现：`.venv-g2/bin/python -c 'from tests.market.test_review_p1 import e03; from quant_lab.market import contract as c,execution as x; q,m=e03(); r=x.simulate(q,market=m); d=r.model_dump(); d["canonical_events"]=[e for e in r.canonical_events if e.kind!="closed"]; print(c.ExecutionResult.model_validate(d).outcome_kind)'` → ContractError/exit1，错误未含事件集合。
S22 位置 contract.py:358：before 验证器仅在某组为 None 时启动，随后 `data.get("entry_fractions") or ef` 把显式 []/() 吞掉；tp_fractions 已给则同一空 entry 会被长度检查拒绝。此为显式记录的输入校验漏洞，未证明可改变经济分配；与 S17 的非空伪造比例/超精度反例分开，不宣称哈希碰撞。
P22 复现：`.venv-g2/bin/python -c 'from tests.market.test_review_p1 import e03; from quant_lab.market import contract as c; q,_=e03(); d=q.model_dump(); d.pop("tp_fractions"); d["entry_fractions"]=[]; print(c.ExecutionRequest.model_validate(d).entry_fractions)'` → (Decimal('1'),)/exit0；应 ContractError。None/省略可解析，显式空列表不可替换。
P20/P21 独立内联探针使用 e03()+model_validate：t_start=t_dec+timedelta(seconds=s)，s∈{0,−1,1,45}；MarketView 的 rules_known 与 bars_complete/bars_quality_ok 同步布尔穷举后公共 simulate(A/B)。输出见表；正向均保留。相应可持久复现命令为 T:test_contract.py::test_request_validation、R:test_outcome_kind.py::test_s21_primary_censor_follows_contract_priority_not_check_order。
同族穷举独立于 G2 §7：逐项核对 ExecutionRequest 全22字段；TTL、两分配、t_start 为推导记录（发现 S22），policy_hash/合同版本受校验；cost/path/position 六种非法枚举或身份值均拒绝。episode/graph/snapshot/manifest、t_dec、plan、预算、seed 为原始输入，request_canonical 包含全 model_dump，未发现新增未入哈希输入。
映射穷举：6种事件存在位×3 fill_status×7删失值共1344组合，另 closed位×8出场腿集合×2有成交状态×7删失值共224组合，分支预期差异0；包含不变量非法对象，只用于异常/选择器边界，不充当真实金标。22夹具六值可达，filled_closed 仅合成 close 腿可达，C06 的 v0 不可达裁定保留；无兜底贴值，诊断遗漏见 S18。
定义/使用核验：CENSOR_PRIORITY 在 A 的 censor_now 与 B 入口 _censor 真正读取；CENSOR_REASONS/EVIDENCE_CENSORS/终态/成交集合与序列化列均核过。OUTCOME_KINDS 是导出/测试目录，非运行分派表，不算失效功能；B 的 order_rank 仍只定义不使用，动态删失也仍有直接赋值，不能宣称全 B 定型，归 S13/S02/S04/S10 原 P2 边界，未见本轮退化。
horizon_end 不新增缺陷编号：M01 R6 将其定义为观察截止，build_request 明确提供可选覆盖；请求缺少市场数据末端，不能强制等于 plan/policy 唯一推导值。独立1/3600/172800秒截止均接受且进入 request_canonical；max_holding 的运行时更早截止由现有回归验证。调用方选择观察窗可能改变删失样本，需固定研究口径，不等于新前视漏洞。
偏差/凭据与 DoD：已读全部指定文档、market/*.py、两测试文件及 AGENTS/GROK；未发现这四项修复新增前视、幸存筛选或凭据入口。M-03 依用户指令与 B10 §3 记 G0 实跑已解除（44640行/44640键/ok/vision_CHECKSUM/rc0），本轮不联网、不以 MockTransport 冒充；M-04–M-09 本地测试/A金标/AB报告通过，八项 partial-P2 不升级；M-10 因 S18/S22 必修未闭合，P1 fail。
证据身份：HEAD c209d8a；market 顶层文件+tests/market递归Python+episode JSON共51文件，按“仓库相对路径:sha256\n”排序连接的 SHA256=5d9e70557d54edeaab0a7cdc5c54781fb3773a1442e21c6b2d7357ff5c1b5445。源码/测试多为 untracked，不能据空 diff 证明历史断言未变；只对本轮实际读取与实跑负责。本轮仅写本文件，未改代码/测试、未调用其他模型或生产服务。
## 五审历史证据（原文保留）
命令均在 quant-lab，环境 `PYTHONDONTWRITEBYTECODE=1`，CLI/内联探针另设 `PYTHONPATH=src`；pytest 已禁 cacheprovider。T=`.venv-g2/bin/python -m pytest tests/market -q`：238 passed/15.19s/exit0；R=`.venv-g2/bin/python -m pytest tests/market/{test_review_fixes,test_review_p1_round4,test_review_p1_round3,test_outcome_kind}.py -q -v`：62 passed/11.09s/exit0，无 skip/xfail。
A/B=`.venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A`（B 替换末参数）：A 22/0/exit0，B 12/10/exit1，双方22例 replay=True；AB=`.venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1`：exit0，MATCH12/命令延迟3/跳空1/同刻优先4/GTD1/流动性1，未知码/NOT_RUN/A_GOLD_FAIL=0；默认仅输出摘要，本轮未重写 AB 文档。
D/Q/X 从下方四审 bash 代码块抽取 Python 原样执行；Q 1 passed/0.35s，另加原记录指定 ohlc-null 变体1 passed/0.17s；X 在首个非法 .5 如预期中断，随后逐值捕获异常确认超精度也拒绝。旧四审表是历史证据，勿用旧结果覆盖本轮结果。
A15：两类删失优先且不互串、rejected 优先、E12 exit_legs=(sl,tp) 保留混合；属性不读行情。22×2内核×2成本×3路径=264项扩展实跑，六值计数 stopped76/tp_hit104/unfilled_expired6/right_censored18/rejected36/unevaluable24，无 close 成交、无 filled_closed；不因 C06 已声明不可达判失败。但 S18 证明夹具之外的合法 IOC 结果不命中规格任何一条，contract.py:545–547 兜底伪装到期；需补齐规范与实现，不能仅放宽测试。已成交无 closed 的异常对象会抛 ContractError，不能声称对任意 schema 对象全函数。
哈希边界：exit_legs 由已入 trace 的事件派生，outcome_kind 代码随 contract.py 进入 A/B build_id；无新增可独立传入的派生字段后门。trace 本来不认证所有结果标量（含 censor_reason），不能据其单独认证外部篡改结果；此为既有 S12 边界。S19 不涉及碰撞：两请求 trace 不同，但同政策下能擅改入场期限，contract.py:391–393 只核对作者非空 TTL；需对 null 时 policy 兜底同样逐值核对。
历史反放宽核验：`git ls-files quant-lab/tests/market` 与 `git log --all --oneline -- quant-lab/tests/market` 均空；测试/22个 JSON 均 untracked，故历史未删改断言/期望的证明为 insufficient，不用空 diff 冒充未变。当前逐条读22个 derivation 与 expected，手算量价/费用/funding/R一致（E12=14/20=.7，E17=(8−.105)/5=1.579），未发现按内核输出生成金标；新增矩阵仅不变量/确定性，不冒充独立金标。四审整体摘要不能恢复逐文件旧内容。
偏差/凭据：读完 market/*.py；质量修复未知即隔离、尖刺仍只标、S16 按当时已成交均价及真实截止重放，未发现新增前视/幸存筛选或私有 API/凭据读取。S18/S19 会污染终态标签/可成交样本。只运行本地/MockTransport 测试，无网络、收费模型调用或生产操作；实际只写本文件，探针湖由 pytest tmp_path 承载。
DoD（GOAL-2 §7）：M-04–M-09 本地测试、A≥10独立期望/不变量、AB差异报告通过；M-03 网络 verify 因禁网记 insufficient，按指令不阻断。原八条 partial-P2 不升级；M-10 新增 S18/S19 必修未闭合，因此 P1 fail。已读指定文档及规则；原文件只有四审表与三审历史摘要，本轮不臆造不存在的三审表。
证据身份：HEAD dc0ddd3；market 顶层文件+tests/market递归Python+episode JSON，共51文件，按仓库相对路径排序连接“路径:sha256\n”的 SHA256=`1fda03a02f3baf1d36aea1c6cfbef190ffab24402fa9d6f5ad5c6a62c638d5cf`，排除本文件，不声称原子快照。
N（新增问题只读复现；正常 model_validate + 公共 simulate，无 model_copy 绕验证）：
```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-g2/bin/python - <<'PY'
from tests.market.test_review_p1 import e03,pt
from quant_lab.market import contract as c,execution as x
req,mk=e03()
p=req.order_plan.model_dump(); p['entries'][0]['tif']='IOC'
q=c.ExecutionRequest.model_validate({**req.model_dump(),'order_plan':p})
m=c.MarketView.model_validate({**mk.model_dump(),'last':[pt(0,101),pt(60,101)],'mark':[pt(0,100),pt(60,100)]})
for k in ('A','B'):
    r=x.simulate(q,kernel=k,market=m)
    print('S18',k,r.outcome_kind,[(e.kind,e.reason) for e in r.canonical_events])
p['entries'][0]['tif']='GTC'; p['expiry']['entry_ttl_s']=None
m=c.MarketView.model_validate({**mk.model_dump(),'last':[pt(0,101),pt(60,100),pt(90,105)],'mark':[pt(0,100),pt(60,100),pt(90,100)]})
for ttl in (86400,1):
    q=c.ExecutionRequest.model_validate({**req.model_dump(),'order_plan':p,'entry_ttl_s':ttl})
    r=x.simulate(q,market=m)
    print('S19',ttl,q.policy_hash[:12],r.fill_status,r.net_pnl,r.outcome_kind)
PY
```

## 四审终版判定表

2026-09-11，GPT-6 主控；不采信已取消任务 task-mtwa8727-xsmsdn 的中间结论。先写入待验证表，再完成本轮复核。
**S01–S16：closed 6 / partial-P2 9 / open 1；加新增 S17 后：closed 6 / partial-P2 9 / open 2。**
三审原有八项 partial-P2 均保留；第九项是 S16（原反例修复，但暴露窗口过度截短）。
S05 的 OHLC 未知值放行及 S17 的 B8 请求边界阻断 P1；S16 不阻断。

| ID | 状态 | 本轮复现命令与输出摘要 | 是否阻断 P1 |
|---|---|---|---|
| S01 TP 当前可成交性 | closed | T:test_s01_tp_fill_requires_current_last_to_satisfy_limit 通过，回撤后不虚构第二笔；A 22/22。 | 否 |
| S02 funding 证据与账务 | partial-P2 | T:test_funding.py、R:test_c2_loader_missing_funding_and_unknown_rules 与 B 闭合 mark/重复冲突回归通过。 | 否；变周期完整性、真实入账、B 引擎余额留 P2。 |
| S03 分钟内部启动 | closed | T:test_s03_intra_bar_start_begins_at_next_bar_open、R:test_c3_s03_b_mark_bars_filtered_by_t_start 通过。 | 否 |
| S04 持仓截止/TP 余量 | partial-P2 | R:test_c1_s04_hold_end_precedes_funding_and_b_truncates 通过；D 的 A/B 截止后 MFE 均 0，X 发现 B 截止前暴露漏算。 | 否；B 残余见 S16，原 P2 边界不升级。 |
| S05 覆盖/规则入口 | open | R 的 BREAK/未知规则拒绝通过；D/X 的缺口删失通过；Q+ 将第二行 ohlc_valid 改 null 后 bars_complete=True、net=5、censor=None。 | **是**；未知 OHLC 质量仍被放行，不能声称质量入口完整闭合。 |
| S06 入湖冲突/bronze | partial-P2 | T:test_s06_conflicting_keys_quarantined_and_bronze_retained、Q 真实修订均通过，active 消失、load_partition=0。 | 否；bronze 深度重放、多来源版本化清单留 P2。 |
| S07 队列/post-only/账户 | partial-P2 | T:test_s07_*、R:test_c3_s07_equity_based_affordability 通过，静态超额开仓反例未退化。 | 否；管理流/E13/C01、完整预留生命周期留 P2。 |
| S08 multiplier | closed | R:test_c1_s08_public_simulate_multiplier 与 T:test_s08_multiplier_scales_pnl_fees_exposure 通过。 | 否；不替代 C02 政策 gate。 |
| S09 as-of 等号/null/序号 | closed | R:test_c1_s09_same_name_sequence_columns 与 T:test_s09_equal_candidate_null_kept 通过，合法等号 null 不回退。 | 否 |
| S10 A/B 解释门禁 | partial-P2 | T:test_s10_* 通过；AB 为 MATCH12、命令延迟3、跳空1、同刻优先4、GTD1、流动性1，无未知码。 | 否；冻结差异键/标量不等于独立逐事件经济证明，B 不定型。 |
| S11 测试/CLI 门禁 | partial-P2 | T 181 passed、R 14 passed、A 22/22、AB exit0，G3 evaluate 接缝在 T 内通过，无 skip/xfail。 | 否；完整成本×路径矩阵、B 跨进程/账户等仍 P2；新增反例不被全绿豁免。 |
| S12 不可变输入/构建身份 | partial-P2 | T:test_s12_*、test_replay.py、R:test_c3_s12_b_rejects_stale_policy_and_registry_pins_hash 通过；B8 字段进哈希但解析一致性见 S17。 | 否；版本化湖和供应链身份仍 P2，新增请求缺陷单列 S17。 |
| S13 事件因果/精度/step | partial-P2 | R:test_c1_s13_closed_causality_precision_and_step 通过；原样 D 两超精度数量均 ContractError；新增 fraction 精度漏检见 S17。 | 否；原八项边界保留，B8 新增接缝单独阻断。 |
| S14 源修订及逐行来源 | closed | 原样 Q 与 Q+：修订通过；缺列/null/旧 SHA（含各自加 gap）均 BAR_GAP、net=None，组合场景事件数0。 | 否；本条 bar 来源反例闭合，不宣称 funding 来源或版本化湖全验收。 |
| S15 双流缺口/质量优先级 | closed | D 双流反例由 net5 改 BAR_GAP；X 反序、两种单流首/尾缺均删失，quality=False+内部洞事件数0。 | 否；内核 min/质量优先级闭合，loader 未知 OHLC 归 S05。 |
| S16 B hold 暴露窗口 | partial-P2 | D 中 B MFE180→0；X 截止后 mark 改动不影响 A/B 事件和暴露，但 T+5 mark102、hold10 得 A MFE=.4/B MFE=0。 | 否；最后订单事件不等于观察截止，B 定型前必修。 |

命令均在 quant-lab，设置 `PYTHONDONTWRITEBYTECODE=1`；pytest 配置禁用 cacheprovider，探针湖写 pytest tmp_path，未写测试/业务文件。
必读：根 AGENTS.md/GROK.md、三审全文、closure 末两节、round3、指定四模块、契约 §5.9–§5.10、GOAL-2 §7；本次按指定工作区审查，不委派模型。
除本文件，仅按授权重生成 report-G2-kernel-AB.md。M-03 网络 verify 本次 **insufficient，非阻断**，未用 MockTransport 冒充联网实跑。

| 代号 | 复现命令 | 本轮输出 |
|---|---|---|
| T | `.venv-g2/bin/python -m pytest tests/market -q` | 181 passed in 12.26s，exit0。 |
| R | `.venv-g2/bin/python -m pytest tests/market/test_review_p1_round3.py tests/market/test_review_p1_round2.py -q -v` | 14 passed in 4.62s，exit0；round3 实际5项、round2 9项，closure 的“6例/180”非本轮计数。 |
| A/B | `.venv-g2/bin/python -m quant_lab.market.execution replay --fixtures tests/market/fixtures/episodes --kernel A`（B 替换末参数） | A passed22/failed0/exit0；B passed12/failed10/exit1；全部22例 replay=True。 |
| AB | `.venv-g2/bin/python -m quant_lab.market.nautilus_adapter report --reps 1` | exit0，22个 episode；A_GOLD_FAIL/NOT_RUN/UNEXPLAINED=0；单次性能不作门槛。 |

按 GOAL-2 §7：A≥10 episode、不变量及本地 M-03–M-09 测试与 A/B 报告门槛满足；B 10例差异不单独否决 P1。但 S05/S17 未闭合，故不满足“必修闭合”。
报告仍有“adjust_account，余额已核”与“引擎余额 not_run”矛盾，本审采用后者，保留 S10 的既有 P2 限制。

## 四审新增问题

### S17（open，P1）：B8 显式解析字段可绕过 policy，并丢失 Decimal 精度

位置：contract.py:340–342、377–389；execution.py:68–72。请求仅检查长度/和/正值，以及作者显式给值的一致性；作者留空时不核对 `resolve_fractions(plan, policy)`，显式 fraction 也未调用 `check_decimal`。
X17 通过正常 `ExecutionRequest.model_validate`（非 model_copy 绕验证）接受同 policy_hash 下 TP 分配1与.5，两者均标 policy；A 净利从10变删失。显式字段应记录解析结果，不能成为隐藏的第三种分配来源。
同接口接受 `.5000000000001`；simulate_batch 将其截成 `.500000000000`，但 trace_hash 基于原始值。不同 trace 是预期的哈希敏感性，问题是请求违反12位精度，批量输出不能忠实复原参与哈希的输入。
修复验收：两组解析字段逐值等于 plan/policy 推导结果；校验有限值与 Decimal(38,12)，不一致/超精度一律拒绝；正常 build_request、A/B 和 batch 往返保持一致。此为本轮 B8 新增公共接缝，不受旧 P2 gate 豁免。
B8 正向证据：T 的等分末腿补余量、来源两值、混合来源、部分给出拒绝、哈希敏感性均过；真实 gold 三个 episode__fixture-v1*.parquet 各36行，其中25行有 plan 且 entry fraction 空，25/25 build_request 成功，无错误；未把合成单测注释当作真实25行验收。
§5.9 TTL nullable 回归通过。尚未发现本轮引入未来特征读取或凭据入口；S05 未知质量放行会污染标签/样本，S16 漏算窗内暴露，S17 能改变标签删失状态。
静态扫描 market/tests 的 services/API_KEY/私有路径，仅见既有公开 Vision URL、MockTransport 和禁止 services 的测试；T:test_no_services_import 通过。未联网、未访问生产配置；公开客户端可配置 hosts/redirect，不能声称硬网络隔离。
S05 补充复现（沿原编号）：在下述 Q 的 variant 列表加入 `("ohlc-null",original.with_columns(pl.Series("ohlc_valid",[True,None])))`；本轮输出 True/net5/None/notes=[]；对照 False 为 False/netNone/BAR_GAP。loader 的 `(~df["ohlc_valid"]).any()` 跳过 null，修复须将缺列/null/false 均视为质量失败。
S16 补充复现及修复：X16 中唯一合法窗内新 mark 是 T+5=102；B 以最后事件 T 截断，丢失 .4R 暴露；应按真实平仓/删失/hold/horizon 截止重放，不能按最后订单事件。保持 partial-P2，不把 B 独有缺陷升级为 P1。

### D / Q 原样复跑命令与三审对照

D：exit0；两非法 qty 仍 ContractError；双流由三审 None/net5/entry+TP 改 BAR_GAP/netNone/仅entry；A/B 均 LABEL_RIGHT_CENSORED、MAE/MFE=0、最大事件 ts=T；三审 B MFE=180。
```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python - <<'PY'
import datetime as dt
from decimal import Decimal as D
from tests.market.test_review_p1 import FIX,e03,pt,T0
from quant_lab.market import contract as c,nautilus_adapter as nb
from quant_lab.market.kernel_a import simulate_a
f=FIX['E03']
for q in ('1e-13','1e27'):
    try:
        c.OrderPlan.model_validate({**f.request.order_plan.model_dump(),'sizing':{'mode':'fixed_qty','qty':q}})
        print('S13 qty',q,'ACCEPTED')
    except c.ContractError as e:
        print('S13 qty',q,type(e).__name__)
def bar(s,p=100):
    return c.Bar(open_time=T0+dt.timedelta(seconds=s),o=D(p),h=D(p),l=D(p),c=D(p))
req,mk=e03()
req=req.model_copy(update={'horizon_end':T0+dt.timedelta(seconds=360)})
mk=mk.model_copy(update={'last':[],'mark':[],
    'bars_last':[bar(0),bar(60),bar(120,105),bar(180),bar(300)],
    'bars_mark':[bar(0),bar(120),bar(180),bar(240),bar(300)],'bars_complete':False})
r=simulate_a(req,mk)
print('S05 two_stream_gaps',r.censor_reason,r.net_pnl,
      [(e.ts,e.leg,e.price) for e in r.canonical_events if e.kind in c.FILL_KINDS],r.coverage_mask)
req,mk=e03(plan_upd={'expiry':c.Expiry(entry_ttl_s=3600,max_holding_s=10)},
    market_upd={'last':[pt(0,100),pt(60,105)],'mark':[pt(0,100),pt(30,1000),pt(60,100)]})
for k,sim in [('A',simulate_a),('B',nb.simulate_b)]:
    r=sim(req,mk)
    print('S04 exposure',k,r.censor_reason,r.mae_R,r.mfe_R,max(e.ts for e in r.canonical_events))
PY
```

Q：exit0、1 passed in 0.23s；真实修订 PASS；baseline True/net5/None；旧 SHA False/netNone/BAR_GAP；null/缺列由三审 True/net5/None 改 False/netNone/BAR_GAP；旧 SHA+gap 由 net5 改 netNone/BAR_GAP。
```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python - <<'PY'
import datetime as dt
import json
import pytest
import polars as pl
from tests.market.test_review_p1 import e03,T0
from tests.market.test_review_p1_round2 import test_s14_revised_source_supersedes_old_silver_days
from quant_lab.market import vision as v, execution as x, partition_check as pc
from quant_lab.market.kernel_a import simulate_a

def test_third_source_rows(tmp_path):
    revised = tmp_path / "revision" / "lake" / "market"
    revised.mkdir(parents=True)
    test_s14_revised_source_supersedes_old_silver_days(revised)
    print("S14 real revision PASS: superseded exists, active absent, load_partition rows=0")
    lake = v.LakePaths(tmp_path / "loader" / "lake" / "market")
    req,_ = e03()
    req = req.model_copy(update={"horizon_end":T0+dt.timedelta(seconds=120)})
    def frame(seconds):
        return pl.DataFrame({"open_time":[T0+dt.timedelta(seconds=s) for s in seconds],
          "open":[100. if s==0 else 105. for s in seconds],
          "high":[100. if s==0 else 105. for s in seconds],
          "low":[100. if s==0 else 105. for s in seconds],
          "close":[100. if s==0 else 105. for s in seconds],
          "volume":[100.]*len(seconds), "ohlc_valid":[True]*len(seconds),
          "gap_flag":[False]*len(seconds), "source_sha256":["current"]*len(seconds)})
    files = {}
    for typ,interval in (("klines","1m"),("markPriceKlines","1m"),("fundingRate","8h")):
        v.atomic_write_json(lake.manifest(v.partition_id(typ,interval,"BTCUSDT","2024-01")),
          {"partition_id":v.partition_id(typ,interval,"BTCUSDT","2024-01"),"source_sha256":"current","check_status":"ok"})
        if typ != "fundingRate":
            files[typ] = lake.silver_dir(typ,interval,"BTCUSDT")/"date=2024-01-01"/"part.parquet"
            df=frame([0,60])
            if typ=="markPriceKlines":
                df=df.with_columns([pl.lit(100.).alias(k) for k in ("open","high","low","close")])
            v.atomic_write_parquet(files[typ],df)
    rules=pl.DataFrame([{"instrument_id":req.order_plan.instrument_id,"effective_from":T0-dt.timedelta(days=1),
       "effective_to":None,"tick_size":"1","step_size":"1","min_notional":"0","multiplier":"1",
       "funding_interval_hours":8,"status":"TRADING","source":"synthetic"}],schema=pc.RULES_SCHEMA)
    pc.write_rules(lake,rules)
    original=pl.read_parquet(files["klines"])
    for label,df in [("baseline",original),
      ("second-row-stale",original.with_columns(pl.Series("source_sha256",["current","old"]))),
      ("second-row-null",original.with_columns(pl.Series("source_sha256",["current",None]))),
      ("source-column-missing",original.drop("source_sha256"))]:
        v.atomic_write_parquet(files["klines"],df)
        mk=x.load_market_from_lake(req,lake_root=lake.root)
        r=simulate_a(req,mk)
        print("S14 loader",label,"bars_complete",mk.bars_complete,"net",r.net_pnl,"censor",r.censor_reason,"notes",mk.quality_notes)
        if label=="second-row-stale":
            assert not mk.bars_complete and r.censor_reason=="BAR_GAP"
    # Same detected stale source + later time gap: bad first row must never fill.
    req=req.model_copy(update={"horizon_end":T0+dt.timedelta(seconds=240)})
    for typ in ("klines","markPriceKlines"):
        df=frame([0,60,180])
        if typ=="markPriceKlines":
            df=df.with_columns([pl.lit(100.).alias(k) for k in ("open","high","low","close")])
        else:
            df=df.with_columns(pl.Series("source_sha256",["old","current","current"]))
        v.atomic_write_parquet(files[typ],df)
    mk=x.load_market_from_lake(req,lake_root=lake.root)
    r=simulate_a(req,mk)
    print("S14 mixed-stale-gap",mk.bars_complete,r.censor_reason,r.net_pnl,mk.quality_notes)

class ReviewPlugin:
    def pytest_collection_modifyitems(self,session,config,items):
        parent=items[0].parent
        items[:]=[pytest.Function.from_parent(parent,name="test_third_source_rows",callobj=test_third_source_rows)]

raise SystemExit(pytest.main(["tests/market/test_review_p1_round2.py","-q","-s"],plugins=[ReviewPlugin()]))
PY
```

Q+：在 Q 的 mixed-stale-gap 场景，依次将 klines 来源设为 `["old","current","current"]`、`["current",None,"current"]`、删除来源列，保留 [0,60,180] 网格；三种实跑均 bars_quality_ok=False/BAR_GAP/netNone/事件0，1 passed in 0.33s。
X（补充缺口、S16/S17）：以下为本轮实跑场景的紧凑复现，X16 截止后 mark1000→5 时 A/B 的 mae/mfe/events 均不变；trace_hash 含市场摘要，不要求其对未来行情变更不变。
```bash
PYTHONDONTWRITEBYTECODE=1 .venv-g2/bin/python - <<'PY'
from decimal import Decimal as D
from tests.market.test_review_p1 import e03, pt
from tests.market.test_review_p1_round3 import bars_req
from quant_lab.market import contract as c, execution as x
from quant_lab.market.kernel_a import simulate_a
from quant_lab.market.nautilus_adapter import simulate_b
full=[0,60,120,180,240,300]
for label,l,m in [('mark-first',[0,60,120,180,300],[0,120,180,240,300]),('last-first',[0,120,180,240,300],[0,60,120,180,300]),('last-head',full[1:],full),('mark-head',full,full[1:]),('last-tail',[0,60],full),('mark-tail',full,[0,60])]:
    req,mk=bars_req(l,m,360)
    r=simulate_a(req,mk)
    print(label,r.censor_reason,r.net_pnl,r.fill_status)
req,mk=bars_req([0,60,180,240],full,360)
r=simulate_a(req,mk.model_copy(update={'bars_quality_ok':False}))
print('quality-gap',r.censor_reason,len(r.canonical_events))
req,mk=e03(plan_upd={'expiry':c.Expiry(entry_ttl_s=3600,max_holding_s=10)},market_upd={'last':[pt(0,100),pt(60,105)],'mark':[pt(0,100),pt(5,102),pt(30,1000),pt(60,100)]})
for k,sim in [('A',simulate_a),('B',simulate_b)]:
    r=sim(req,mk)
    r2=sim(req,mk.model_copy(update={'mark':[pt(0,100),pt(5,102),pt(30,5),pt(60,100)]}))
    print('X16',k,r.mae_R,r.mfe_R,r.mae_R==r2.mae_R and r.mfe_R==r2.mfe_R and r.canonical_events==r2.canonical_events)
req,mk=e03(plan_upd={'tps':[c.TakeProfit(level=D(105))],'sizing':c.Sizing(mode='fixed_qty',qty=D(2))})
for frac in [D(1),D('.5'),D('.5000000000001')]:
    q=c.ExecutionRequest.model_validate({**req.model_dump(),'tp_fractions':(frac,)})
    r=x.simulate(q,market=mk)
    df=x.simulate_batch([q],markets={mk.manifest_id:mk})
    print('X17',frac,q.policy_hash[:12],r.fraction_source,r.net_pnl,r.trace_hash[:12],df['tp_fractions'].to_list())
PY
```

证据身份：HEAD `4bc8ea3fb3fbb14c3b4c037bd5e59fbd6d57caac`；工作区51文件集合 SHA256 `417040ee529dc09e5162b016811a6261c38c1868aa4cf5f1489d5989070f1c68`。
集合算法：market 顶层文件、tests/market 递归 Python 与 episode JSON、接口/GOAL-2/closure，按路径排序连接 `路径:文件sha256\n` 后 SHA256；排除本文件及生成报告，不声称原子快照。
四模块 SHA256：A `0e900cd7c8b04ef131b77fdc4b3354ac1b8cf2d5926a317109c21e2368419d55`；execution `002343cb7504fb334ef4e2fb4e0fed9dcfae33d6c4baefad76ceaddc99307260`；contract `1dfa3f70124f55ab5dca8e54b8de2034ecbcc5e4290f2a4caf8e28eda0f0bdbd`；B `1ab356b40d144813cf3dc9e5a51dad21c96b22aeda3cd14e5cd91c997ee58f59`。

## 历史轮次

一审 fail（closed 0 / partial 0 / open 13）；二审 fail（closed 1 / partial 12 / open 1）；三审 fail（S01–S14：closed 4 / partial-P2 8 / open 2，另列 S15/P1、S16/P2）；正文以 docs/adr/review-G2-P1.rounds-1-2.md 快照与本文件旧版 git 历史为准，不再内嵌全文。

