# ADR-G1：不可变版本、双状态与因果闭包的 episode 引擎

状态：proposed；日期：2026-09-11；用途：G1 D-02 与后续实现依据。
本次仅编写本 ADR；不代表契约冻结、实现通过、真实数据放行或任何生产授权。
采用版本事件重放生成作者轨，以依赖闭包生成决策图；描述图保留迟到、缺口、冲突和未选边。
契约缺口阻止相应持久化接口发布，不以 ADR 自动扩充公共 schema。

## 1. 依据、可信度与范围

- R：[`research-schema.md`](../../contracts/research-schema.md)，当前 v0 草案。
- I：[`contracts/README.md`](../../contracts/README.md)，接缝、授权和目录边界。
- M：[`合并稿`](../../../../docs/plans/2026-09-11-quant-scale-up-merged-plan.md)，本设计使用 A、B、C。
- G：[`GOAL-1-data.md`](../../goals/GOAL-1-data.md)，D-01 至 D-11 与交付物。
- A：[`AGENTS.md`](../../../../AGENTS.md)，生产隔离；另已读本机 GROK 分工规则。
- K：[`codes.py`](../../src/quant_lab/data/codes.py)，现有枚举与骨架，仅视为现状证据。

可信度标注规则：高＝上述输入直接规定；中＝由不变量推导的工程选择，未经过实现验证；低＝需冻结或实测的阈值/策略。
下文每节、每表标注覆盖该节或表的全部设计行；更局部标注优先。高可信引用表示规定明确，不表示产线有效性已有实证。
不联网、不调用其他模型、不安装依赖、不修改看板、不运行测试或产生夹具；本次验收只检查文档。
模型使用当前 GPT-6 会话；不再次启动 dispatch 或 Grok，避免越过用户零收费调用与单文件范围。

### 1.1 术语与边界（高；R §2–3、M A.1/B.1/B.2）

| 术语 | 唯一含义 |
|---|---|
| source_id | `(peer_id,message_id)`，不能只用频道内 message_id 跨频道关联 |
| source_version_id | 某一内容、媒体和版本证据的身份；编辑不覆盖旧版本 |
| entry_branch_id | 有独立提议依据的计划分支；同方向、同币不等价于同分支 |
| episode_id | 某图版本内的派生生命周期身份；merge/split 或新图重分配 |
| P / C | 作者计划状态 / 作者声称持仓状态；都不是交易所实际持仓 |
| entry_observed | 本图视图有明确入场提议来源；仅有“已进场”不补提议 |
| exit_observed | 有明确作者全平来源；不是触价、到期或模拟 fill |
| duplicate_group_id | 复制关系分组，保留每个来源，不等同生命周期合并 |
| cluster_id | 研究相关性分组，与复制组、episode 身份分离 |
| left_truncated | 入口缺失的操作标记，不隐含生存分析独立延迟入组假设 |
| decision graph | 对固定决策根与可知时点构建的不可变投影，不是完整图删掉晚边后的剩余物 |

模拟轨、撮合、净 R 属 G2；G1 不从市场价格生成 `entry_claimed` 或 `close_claimed`。
未知数字使用 Arrow 缺失值并附原因；`false` 仅用于确实为假的布尔字段，不能替代未知价格或时间。

## 2. 六级产线与 Parquet 边界

### 2.1 编号与 schema 记号（高；M B.1/C.2、R §1–6；实现映射中）

六级固定为 B0 归一、B1 独立抽取、B2 规则链接、B3 歧义裁决、B4 生命周期、B5 抽审。
C.2 Telegram 1–5 是清洗子步骤：C1/C2 在 B0，C3 在 B1，C4/C5 在 B1 后至 B2 前；不另算第二条抽取产线。
K 的 `Layer` 当前为 normalize/dedup/extract/canonicalize/market_validate/link_lifecycle，与 B.1 不同；本 ADR 不改 K，见 CR-10。
以下 schema 缩写是完整引用，禁止实现时任意投影掉溯源列。
`T` 指 `event_time,available_at,ingested_at: timestamp[us,UTC]`；所有表，包括损耗和内部中间表，均带 T。
契约未给出的类型不在正文假定已经确定，逐项列在末尾；缺这些类型的表只能做内部原型，不能作为冻结公共 Parquet 发布。

| 缩写 / 主键 | Parquet 列集（字段名遵守 R） |
|---|---|
| MV / source_version_id | `source_id:struct<peer_id:int64,message_id:int64>, source_version_id:string, version_no:int32, content_hash:string, text:string, media_hashes:list<string>, media_uris:list<string>, reply_to_message_id:int64?, forward_from:struct?, grouped_id:int64?, message_date,last_edit_at,first_seen_at,snapshot_at:timestamp?, time_grade:enum<V,H0,H1,H2,U>, survival_scope:enum<historical_survivor,watched_cohort,unknown>, deleted_after_observation:bool?, raw_hash,raw_uri:string, T` |
| EX / extract_id | `extract_id,source_version_id:string, kind:R枚举, symbol_raw:string, instrument_id:string?, side:enum<long,short>?, entry:struct<lo,hi,kind>?, stop:decimal?, tps:list<struct<level,fraction?>>, size_hint:struct?, spans:list<struct<field,start,end,source>>, bboxes:list<struct<field,media_hash,x0,y0,x1,y1>>, extractor:struct<name,version,model?>, confidence_bucket:enum<high,mid,low>, reason_codes:list<string>, T` |
| EE / (graph_version,event_id) | `event_id,episode_id,graph_version,kind,event_seq,extract_id,source_version_id,supersedes_event_id,link_method,link_confidence,edge_available_at,payload(json),T`；link_method 仅 `reply/quote/plan_ref/window/llm` |
| EP / (graph_version,episode_id) | `episode_id,graph_version,predecessor_ids,successor_ids,channel_id,trader_id,instrument_id,side,entry_branch_id,duplicate_group_id,cluster_id,author_plan_state,author_claim_state,entry_observed,exit_observed,left_truncated,right_censored,censor_at,censor_reason,time_grade_min,decision_eligible_at,claimed_outcome,reconstructed_outcome,eligibility_by_estimand,audit_stratum,sampling_probability,label_status,signoffs,derivation_hash,T` |
| Q / R §5 唯一键 | 身份、来源时钟、检测、处置、血缘五组全量字段，具体见 §2.4；不得仅写 reason 字符串 |
| LOSS / (batch_id,flow,layer,stratum) | `batch_id,flow,layer,stratum,input_unit,input_n,output_unit,output_n,n_ok,n_review,n_quarantine,n_dup_ref,primary_reason_dist,cum_excluded_ids,cum_excluded_weight,rule_version,schema_hash,mapping_hash,T` |

MV/EX 的已定义类型照 R；EP/EE 未明确的物理类型见 CR-01，不以表格缩写掩盖不足。
内部候选边表 CB、裁决表 JD、映射表 MAP、依赖表 DEP、审计表 AUD 的**拟议** schema 只列在末尾 CR-04/05/06/08；它们不是当前已存在的契约。
下面用这些名字说明实现依赖；G0 定稿前可在内存用同构对象测试，禁止向 G3 暴露未冻结表。

### 2.2 每级输入、输出与重放（中；M B.1/C.1/C.2、R §2/5/6）

| 级 | 输入 Parquet / 原始例外 | 输出 Parquet schema 与职责 | 幂等与失败出口 |
|---|---|---|---|
| B0 归一 | TDesktop 原包是 JSON/媒体，不伪称 Parquet；重放输入为已封存 MV 与原包 manifest | `bronze/message_version:MV`；内部 MAP；Q、LOSS。UTF-8/UTC/单位校验，相册成员独立版本、媒体按 hash 引用；C2 精确去重仅折叠规范引用 | source_version_id 相同只引用一次；同 source 不同版本保留；raw hash 不符停批。版本序号晚到冲突见 CR-02，禁止重写 bronze |
| B1 独立抽取 | MV + 媒体/OCR 证据；规范化和价格检查额外读取冻结 G2 市场 Parquet manifest | `silver/extracted_event:EX`，每条消息至少分类结果、parser/LLM 各自成行；规范化 EX 为新 extract_id；内部 DEP/MAP；Q、LOSS | hash(source_version_id,extractor版本,配置,分支序号)；缓存模型响应作为输入证据；抽取失败保留 undecidable，非信号保留负类框 |
| B2 规则链接 | 已规范化 EX + MV 的回复/转发/相册结构 + 历史索引 | 内部 `candidate_edges:CB`、MAP、Q、LOSS；不提前伪造 gold episode_id | 候选按端点、证据版本、方法、规则版本去重；候选集合排序仅用于稳定字节，不作为时间证据；冲突保留全部候选 |
| B3 歧义裁决 | CB + EX + 裁剪后源证据引用 | 内部 `adjudications:JD`、DEP、Q、LOSS；记录 link/new_episode/unresolved 与未选边 | request hash 命中录制结果；一个初始请求最多两次失败重试，最多一次上下文扩展；仍歧义拒判，不无限回流 |
| B4 生命周期 | EX + CB + JD + MAP + 冻结时间证据 | `gold/episode:EP` 与 `gold/episode_event:EE` 的描述/决策视图；内部 DEP、迁移记录；Q、LOSS | graph_version 固定输入/代码/规则/裁决快照；拓扑重放，晚到/改归属出新版本；生命周期回流 B2/B3 最多一次，仍冲突隔离 |
| B5 抽审 | 冻结 EP/EE + 概率抽样框 + 独立人工标签 | 新发布版本的 EP/EE（schema 不变，质量字段更新）、内部 AUD、Q、LOSS | 审核输入/计划/seed/签字固定；重跑复用同一抽样结果，不重新抽签；审核变更创建新 manifest，原版本留审计 |

EP 的重建结果在 G1 初次发布为缺失；G2 “回填”只能产生带 execution_contract_version 的新派生版本，不原地写回已冻结 EP。（中；M B.2、R §2）
未裁决的 CB 在内部表完整保留；缺 episode_id 的边不得塞进非空 EE 列。描述图必须能联查 CB，不能把 EE 冒称全部候选图。（中；R §2、CR-04）
新建无前驱事件的 `link_method`、`supersedes_event_id` 可空性当前未定，见 CR-01；不把 new_episode 编成新增 link_method。（高；R §2）

### 2.3 规范化、去重与市场校验（高：规则来源 M C.2；低：阈值沿用其 P05）

精确重复要求 source_id、内容、媒体、版本证据相同；不同频道即使逐字相同也保留各自 MV。
相册按 `(peer_id,grouped_id)` 建成员关系；每个成员保留 source_id，晚到图片只影响依赖它的字段，不提前完成整个相册。
原文与 OCR 独立解析数字；任一路空不算一致。图片没有可读数字时保留 `OCR_UNREADABLE`，不由币名推价格。
规范化只转换有证据的单位、方向与档位排序；不推默认 SL、仓位比例、杠杆或止盈分配。
symbol 映射以字段可用时刻的历史市场身份查表，映射版本加入 DEP；不能用今天的改名表回填过去。
取字段闭包可用时刻 t_a，调用 G2 as-of，只用已闭合且可用的 1m mark；缺价或超过 120 秒为 MARK_STALE。
计算区间两端 `abs(log(p/m))`，最近端作告警指标；`δ≥ln(3)` 为 UNIT_SCALE_CONFLICT，绝不自动乘除 1000。
合理性带由开发窗人工确认价格的第 99 百分位冻结；频道×订单类型少于 50 时用合并开发分布并标不足，不能借验收收益校准。
`δ>T_plaus` 为 ENTRY_MARK_DEVIATION，原文支持的远挂单可经签字保留；无冻结阈值时该项 insufficient。
30 自然日、至少 10,000 个有效连续 1m 收益的稳健波动率单独作诊断，缺 bar 不跨洞算收益；不足 VOL_HISTORY_SHORT。
M 的 `5×tick_size/m` 下限未明确修补后用于何处，见 U-04；不得重新引入被撤掉的波动率入场门。
市场检验依赖与抽取依赖分开：市场晚到不改 EX 继承的消息时钟，但会使依赖该检验的用途无法通过闭包。

### 2.4 隔离与损耗（高；R §4–6、M C.3；记账算法中）

Q 身份：`quarantine_id,batch_id,object_kind,object_id,object_version,partition_id`。
Q 来源：`raw_uri,raw_hash,source_refs,event_time,available_at,ingested_at`；未知时刻对应 unknown_reason 的位置待 CR-03。
Q 检测：`reason_code,all_reason_codes,severity,field_path,observed_value_ref,expected_contract,rule_version,schema_hash`。
Q 处置：`status,eligibility_by_estimand,reviewer_ids,decision_reason,decision_at,successor_ref`。
Q 血缘：`dependency_refs,affected_manifest_ids,invalidation_job_id,consent_id,retention_deadline`。
Q 唯一键 `(object_id,object_version,rule_version,reason_code)`；quarantine_id 可由该键确定性 UUID 化，处置变更留版本历史。
`invalid_transition` 是状态机诊断，不是新增 ReasonCode；Q 使用 `LIFECYCLE_INVALID`，EE payload 记录该诊断。
主原因先按严重度，再按冻结规则优先级确定；一输入状态互斥归入 ok/review/quarantine/dup_ref，all_reason_codes 保留重叠。
损耗以实际输入单位对账；一消息多提议、相册多消息一事件、重复引用都经 MAP，不用 output_n-input_n 推排除量。
累计损耗按 estimand 的不可评估对象并集与冻结权重计算；权重未知报告缺失，不填零。负类留抽样框，隔离也不删除源。
B0–B5 各有 LOSS，同时记录 C1–C5 清洗子层；共享步骤引用同一 task/mapping hash，汇总不重复加总抽取次数。

## 3. 作者轨 P × C 完整转移表

可信度：高，状态值与允许事件来自 R §3/M B.1；中，以下确定性守卫顺序与歧义出口；终态管理边界见 U-01。
`N/A/X/E` 分别为 none/active/cancelled/expired；`U/O/C` 分别为 unknown/claimed_open/claimed_closed。
表内 `=` 保持原态并追加事件，`I` 为 invalid_transition；`R` 为新图纠错重放，不在旧图倒退。
`J` 仅在明确“新单/重开”及独立分支证据下新建 episode，旧态不变；否则 I。
`L` 为缺提议的左截断改单，保持原态、保留证据，不对缺失计划应用字段补丁。
先处理精确重复（无新增事件），再处理纠错，再判新提议/重开，再按表执行；不能用重复到达触发“重开”。
“提议”列仅用于作者明确入场计划；分析句即使包含全部价格也不能进入该列。

| 前态 P×C | 提议 entry_proposal | 改单 amend | 取消 cancel | 到期 expire | 入场声称 entry_claimed | 管理 add/stop_move/tp_ladder | 部分退出 reduce | 全平 close_claimed | correction / 明确撤回 |
|---|---|---|---|---|---|---|---|---|---|
| N,U | A,U | L | I | I | N,O | = | = | N,C | R |
| N,O | J | L | I | I | I | = | = | N,C | R |
| N,C | J | I | I | I | J | I | I | I | R |
| A,U | J | = | X,U | E,U | A,O | = | = | A,C | R |
| A,O | J | = | X,O | E,O | I | = | = | A,C | R |
| A,C | J | I | I | I | J | I | I | I | R |
| X,U | J | I | I | I | X,O | I | I | X,C | R |
| X,O | J | I | I | I | I | I | I | X,C | R |
| X,C | J | I | I | I | J | I | I | I | R |
| E,U | J | I | I | I | E,O | I | I | E,C | R |
| E,O | J | I | I | I | I | I | I | E,C | R |
| E,C | J | I | I | I | J | I | I | I | R |

所有 I 保留原 P/C；`event_seq` 仍保留审计位置；该事件不更新可用于决策的字段、不隐式丢弃整个早先有效 episode。
若归属错导致 I，同时记录 ENTRY_LINK_AMBIGUOUS 并按致命处理；不能因主码 LIFECYCLE_INVALID 属一般而降级。
终态后明确全平/入场声称依表保留作者陈述；普通管理暂按 M 的“终态后的其他管理属冲突”处理。
这会阻断“取消未成交余量但已持有仓位继续管理”的作者轨解释；G2 模拟轨照其合同继续管理实仓，两轨不互相覆盖，U-01 待仲裁。
N,O 后首次看到一条提议不能倒补入口：描述图可通过新版本与明确历史证据纠错，当前决策时间线保持缺入口。
A,U/A,O 的第二条独立提议创建新 episode；如果只有“更新”语气则必须先分类成 amend，不能靠状态机猜。
expire 守卫要求冻结的明确计划期限；研究 H=7 日只触发 censor，不自动制造作者计划期限。
entry_claimed 必须明确“已进场/持有”；close_claimed 必须明确“全平”；“TP1 到了”默认 result_post。
reduce 无论百分比缺失或达到猜测总量都不变 C；只有原文明确全平才另提取 close_claimed。
add 只是作者管理主张，不能当作模拟加仓 fill；重复 entry_claimed 若只是语义复述可分类为非状态备注，否则 I 待审。
amend 仅替换带 span/bbox 的字段，产生 supersedes_event_id；SL 新版本不能覆盖先前决策所用旧 SL。
所有 N,* 缺计划记录均 `entry_observed=false,left_truncated=true`；明确初始提议的正常分支为 true/false。
close_claimed 可令 `exit_observed=true`，但缺入场记录仍不可计算收益；C.closed 不自动把 P 改 cancelled。

### 3.1 其余 kind 的穷尽处理（高；R kind 枚举、M B.1）

| kind / 输入 | 任意 P×C 下的行为 |
|---|---|
| analysis / result_post / chatter | 状态不变，描述性节点；不得由结果词推 fill 或全平 |
| undecidable | 状态不变、unresolved、保留拒因，不进入决策状态更新 |
| delete_notice：只证明删帖 | 状态不变，更新可观测删帖证据；不代表取消计划，不统一剔除已完整实收 cohort |
| delete_notice：明确撤回既有断言 | 进入 R，来源与撤回范围必须确定；不删除旧图 |
| correction | 任意态进入 R；找不到被纠正对象则 unresolved，禁止自由重写整段历史 |
| 外部市场触价 / 观察结束 | 不是作者 kind；不改变 C，观察结束只标 right_censored/censor_at/censor_reason |
| 未知 kind 或任意守卫不满足 | invalid_transition + LIFECYCLE_INVALID；schema 未知枚举另记 SCHEMA_DRIFT |

## 4. 规则链接器

### 4.1 候选边生成（中；M B.1/C.2；72 小时为低可信冻结提案）

建立分索引：source_id→版本集、reply 反向索引、带频道/作者命名空间的计划号索引、引用片段指纹索引、instrument/side/时间索引、媒体及文本复制指纹索引。
对每个 EX 取以下候选集合并集，不按最近候选提前截断；去重不丢不同理由。

| 来源 | 生成算法 | 强/弱与冲突规则 |
|---|---|---|
| reply | 同 peer 的 reply_to_message_id 定位父消息及其版本，必要时递归追父链 | 只有父版本已知、分支唯一、无语义矛盾才为强；父有两腿则仍歧义；缺父 PARENT_MISSING |
| quote | 先识别显式被引用 source_id，否则在引用 span/规范文本索引找匹配 | 可验证唯一来源、匹配分支且证据当时可用才强；短句或多个来源弱；引用不能提前暴露被引原文 |
| plan_ref | 从证据 span 提取计划号，用 `(channel,trader,plan_ref)` 查全部分支 | 唯一明确号且无复用才强；号码复用、作者不明、多腿同号均弱；不能无条件按枚举认强 |
| window | 同 instrument/side 的过去 72 小时候选；描述性补查可含后出现节点但标晚边 | 永远弱；无 side 可扩大召回但不推方向；多单并存不就近归属，超窗强指针仍保留 |
| 复制指纹 | 原文精确 hash、媒体 hash；规范文本 token 相似度和感知图指纹作近似召回 | 跨频道一律先建复制候选组；不直接合并 episode；近似阈值冻结于开发夹具，未校准只启用精确指纹 |

跨频道转发有可证明原帖坐标时可产 quote 候选；只有转发显示名或指纹时不得虚构 quote。
复制指纹不在 R 的 link_method 枚举内：仅存在内部 CB；最终经裁决建立归属时使用 llm 或有真实证据的 reply/quote/plan_ref，保留复制依据。
强度是证据属性，不是方法名常量；K 的 STRONG_LINK_METHODS 当前不足以表达守卫，见 CR-10。
两条相互冲突的强指针不按优先级吞掉一条：记录所有候选、ENTRY_LINK_AMBIGUOUS，进入 B3/独立复核。
图片管理与文字计划必须有共同价格/计划号/引用锚点；单币名或同向不能把弱边升级为强边。
每条候选保留证据 source_id、source_version_id、span/bbox、端点、拒因与 edge_available_at；内部 CB schema 待 CR-04。
窗口索引的“当时有哪些候选”也是依赖：后出现的提议不得通过全量候选排序改变旧决策的 winner。

## 5. LLM 裁决接口

可信度：高，M B.1/B.5 与 I 闸门；中，结构化接口和证据校验；低，上下文数值预算。
接口归属 `quant_lab.data.llm`，分 `extract` 与 `adjudicate`；本 ADR 仅规定协议，测试提供录制响应，默认无网络 provider。
抽取模型与确定性 parser 各自读源/OCR，不将 parser 预测当模型答案；二者冲突不得投票猜数字。
裁决输入为单一任务：request_id、schema/rule/prompt/model hash、用途（描述/决策）、决策截止、候选边、中心事件、证据版本清单、裁剪说明。
每条上下文以 `(peer_id,message_id,source_version_id)` 标识，正文视为引用数据；模型无工具、无代码执行、无外部检索权限。
模型输出只允许一个 JSON 对象；不接受自由文本当判决，不接受模型自报 confidence 充当正确率。

| 输出项 | 语义与校验 |
|---|---|
| action | 严格 `link/new_episode/unresolved` |
| selected_candidate_id | link 时必须来自输入候选；其余为空，不能创造目标 episode |
| evidence_message_ids | 必填，link/new_episode 非空；每项包含 peer_id/message_id/source_version_id，必须是本次输入证据 |
| evidence_spans / evidence_bboxes | 与裁决所依赖价格、计划号、文字主张逐项匹配；越界、错媒体 hash 或不存在字段则拒收 |
| rejected_candidate_ids | 所有未选候选及其理由，不能遗漏互斥竞争者 |
| reason_codes | 仅 R §4 现有码；空响应、无法判断使用对应 INTENT_AMBIGUOUS/ENTRY_LINK_AMBIGUOUS 等 |
| needs_more_context | 布尔；仅首次未决允许请求一次扩展，不由模型直接读取更多数据 |

unresolved 允许 evidence_message_ids 为空，但必须给出缺证原因；格式坏、伪造 id、证据不支持断言一律改写为本地拒收记录。
初始上下文提案：中心版本、直接父/引用/计划号证据、最多 20 个候选、32 条消息、8 幅必要图片，文本预算 8,000 tokens。
扩展一次提案：64 条、16 图、16,000 tokens；数字均需 U-03 冻结，超预算 unresolved，不隐藏竞争候选后假称唯一。
裁剪优先保留直接证据与全部冲突锚点，其次窗口邻居；原文只按 span 周边裁剪，记录被截断部分与 hash。
对决策用途先 as-of 过滤再裁剪，不喂未来结果/后编辑；描述性裁决即使选择了旧边，其整个输入上下文也进入该判决依赖。
不得事后宣称“模型只用了一条早消息”以丢掉其看到的晚消息依赖；需要早期决策时另作只看早证据的任务。
重试仅限传输/格式失败，每请求最多两次；语义拒答先唯一一次扩展，再人工或 unresolved，不靠重试挑答案。
独立计量初筛、抽取、链接、扩展、重试的输入/输出/图片 tokens、usage、缓存、拒答；录制夹具 usage 标 synthetic，不伪报真实成本。
当前云与预算均不由本任务授权；真实 provider 必须等待 I 对应闸门，且不能用生产 session 取上下文。

## 6. 描述图构建与决策图闭包

### 6.1 描述图算法（中；M B.1/B.2、R §2）

1. 校验固定 manifest 的源 hash 与 schema；构建 MV 版本节点、EX 节点、媒体节点及其出处边。
2. 加入 B2 的全部候选边与 B3 的选择/拒答记录；区分候选、选中、拒绝、缺父占位，不用连通分量直接定义 episode。
3. 从明确提议逐分支建根；无提议管理建缺入口根；按唯一归属选择把事件挂入，竞争归属 unresolved。
4. 添加字段派生节点：入场、SL、TP 各自依赖 EX 和 span/bbox；改单仅 supersedes 对应字段，不抹掉旧节点。
5. 将缺父表示为结构缺口，不生成伪 MV/伪入场；引用片段自身是引用版本的证据，不能充原帖全内容。
6. 按实际可知顺序重放 §3；并列时钟只有因果无关、可交换事件可按稳定 ID 存储，不得拿 ID 当真实顺序。
7. 关系图可有互引环，派生 DEP 必须无环；有强连通依赖的候选先隔离，禁止拓扑失败后强行排序。
8. 输出完整图版本及描述 EP/EE；所有未选/无归属候选仍由 CB 联查；描述态可含后来补证，不能直接交 G3。

### 6.2 决策图闭包（高：无前视规则；中：实现算法；R §1–2、M C.1/B.2）

定义 `deps(x)` 为 x 的**全部必要输入**，包括选择 x 的规则/模型实际看到的上下文；不是仅最终输出引用的几条边。
叶节点的 available_at 由时间证据或已冻结假设给出；未知记未知，不用最小时间或 ingested_at 倒推历史。
`A*(x)=max(available_at(x), A*(d) for d in deps(x))`；任一必要依赖未知则 A* 未定义，拒绝相关决策用途。
`decision_eligible_at` 是闭包完成时刻 A*；`t_dec=A*+冻结处理延迟`，两者不混为同一列。

| 依赖对象 | 必须进入闭包的内容 |
|---|---|
| 节点 | MV 内容版本、EX 解析证据、父版本、提议分支；不能只验当前消息 |
| 边 | 两端版本、reply/quote/plan_ref 证据、规则输入候选集、裁决全部上下文及裁决可用证据 |
| 字段 | 每个价格/方向/数量/期限的源版本和 span/bbox，旧值到新值的 supersedes 关系 |
| 媒体 | 实际用到的图片 hash、成员来源、接收证据、OCR 输入及处理延迟；未用到图片不强制拖慢纯文本子用途 |
| 派生特征 | 所有上游字段、映射规则、市场 bar、校准阈值、历史窗口、版本与假设；只见最终 feature timestamp 不够 |
| 市场校验 | 已闭合且 as-of 可用的 mark、上市精度历史、波动率窗口和冻结开发校准产物；禁止取未来修订值 |

算法针对一个已确定决策根 r，而非“整个 episode 最终结局”计算：
1. 固定 graph_version、目标用途、触发事件和处理延迟版本；初始依赖只含该触发所需输入。
2. 递归 DFS（白/灰/黑标记）收集闭包；灰节点再次出现即拒绝循环；记录最弱 time_grade 与全部假设。
3. 计算 A* 和 t_dec；决策版本选择按 `(available_at,sequence)`，仅有实证先后才容许等号，未知先后用严格 `<`。
4. 对市场数据同时要求 bar_close≤t_dec；用于价格合理性检查仍按字段 t_a 选 mark，不通过延迟扩大成事后校验。
5. 对每条选边再验证 `edge_available_at≤t_dec` 及其依赖闭包；数字、媒体、特征逐个复验，失败 DEPENDENCY_NOT_AVAILABLE。
6. 只重放这一时点已可知、守卫合法的事件；输出该时点的 P/C 和字段，不复制描述 EP 的终态、结果或最终 censor。
7. 冻结快照 hash；未来编辑、补父、结果帖或复制分组变化不得改变该快照；需要新决策则生成新根与新快照。

严禁不断增大 t_dec 直到所有未来边都可见：A* 的输入集合由决策根固定；后来的管理产生另一决策，不回填旧根。
若冻结处理延迟为零且顺序未知，最大依赖恰等于 t_dec 不能使用；拒该同刻快照或等下一可证明决策，不加虚构微秒。
H1 事后补证不能复活已经结束的机会；若现有证据无法判定机会在新时点仍有效，则仅描述，等待明确新提议。
R 的 API 缺决策根/t_dec/快照标识，多时点 EP 的公共表达存在缺口：CR-06 解决前拒绝发布会混入完整生命周期的 G3 视图。
`load_episodes(graph_version,decision_graph=True)` 保持现签名；仅允许加载 manifest 已明确绑定单个快照语义的版本，未绑定则报错。
`load_episode_events(graph_version)` 暂与该 manifest 的图类型一致，不能默认返回描述事件给决策消费者；最终协议待 CR-06。

## 7. 三时钟、H0/H1 与顺序证据

可信度：高，R §1、M A.1/C.1；中，派生对象 event_time 的实现约定；假设数值均低。
event_time 表示源主张发生时刻，追述时间不能取代可知时刻；asserted_event_time 当前缺字段位置，见 CR-03。
ingested_at 为该物化对象首次入湖时间；缓存命中不更新它，新版本记录新的实际入湖时间。
确定性 derivation_hash 排除本次墙钟、文件行组布局及运行耗时，包含全部语义输入；已有对象重跑直接引用，字节不变。
全新空湖重建可有不同 ingested_at，但语义 hash 与逻辑结果必须相同；不能为字节一致伪造首次写入时间。

| 级 / 对象 | event_time | available_at / 决策依赖 | ingested_at |
|---|---|---|---|
| B0 MV | 发布版本用 message_date，编辑版本用 last_edit_at；无证据不猜 | V=max(内容实收,必要媒体实收)；H0=发布+冻结分项延迟；H1=首次可证明该最终版本完整可见的时刻 | 首次落湖；原包接收时间另作证据 |
| B1 EX | 继承所属 MV；作者追述只作附属断言 | 按 R 继承 MV，抽取/OCR/市场验证的额外依赖在 DEP 和 t_dec 显式计入，不暗改 EX 时钟 | 该 extract_id 首次落湖 |
| B2 CB | 引入链接证据的源事件时刻；多源用明确触发消息 | max(两端版本、锚点字段、必要媒体、规则上下文 A*)；无父版本则未知 | 首次保存候选 |
| B3 JD | 中心源事件时刻，裁决执行时刻另存 | 描述性事后裁决不得倒填；历史模拟裁决只在冻结处理模型假设下可用，依赖全部实际上下文 | 录制或真实响应首次入湖 |
| B4 EE / EP | EE 取源事件；EP 取该投影的触发事件，完整描述聚合约定待 CR-01 | EE=max(源及选边闭包)；EP=max(该用途全部依赖)，decision_eligible_at=A*，再加处理延迟得 t_dec | 新图/快照首次落湖 |
| B5 审核投影 / AUD | 审核发生时刻用于 AUD；EP 保持其源语义 | AUD=签字实际可用时刻；审核不回填历史交易信息，仅改变研究放行。若签字裁决改变归属，按新 JD 依赖重建 | 首次写审核版本 |
| Q / LOSS / MAP | Q 源对象时刻；聚合 LOSS 取本批源 event_time 最大值，MAP 取被映射输入时刻 | max(必要输入可用时刻)，未知不猜；处置/发布时刻另记，不能代替源可用时刻 | 该记录实际物化时刻 |

H0 必须有导出字段保真证据，不能把缺 last_edit_at 自动视作“确定从未编辑”；输出用途标签 H0_conditional，不晋升严格 V。
H1 只见最终编辑版：原始入场用途隔离；有完整版本/媒体/上下文同时可用证据才研究编辑后新决策，否则从首次可证快照开始。
R 说历史导出 snapshot_at 为空而 M 允许首次可证快照；需 CR-03 仲裁，当前不能随意把导出时间写成已证明内容的最早时刻。
H2 仅保留可见引用及缺口，引用片段自引用版本出现才可用；U 只保留不依赖未知项的描述。
时间最弱级沿 K 的 V/H0/H1/H2/U 保守归并，但用途还要逐依赖判定；time_grade_min 不是充分准入条件。
survival_scope 与时间级正交；删除发生后只限制缺失用途，不统一删除完整实收 episode 或假装观察未结束者未删除。
temporal_assumptions 需记录假设 ID、适用对象、证据引用、延迟、顺序政策、冻结时间和版本；具体持久化建议见 CR-03。
合成默认延迟可由夹具固定，例如内容 2 秒、媒体 5 秒、处理 1 秒；仅验证算法，不宣称真实频道延迟。（低；测试参数）

## 8. tombstone、merge/split 与级联失效 DAG

可信度：高，R §2 与 M B.2 N04；中，事务发布及恢复方案；持久化协议未冻结见 CR-07。
merge/split 不修改旧 episode，不把旧金标自动复制给新身份；管理改归属与纠错使用同一迁移流程。

| 操作 | 新身份与谱系 | 必须保持的约束 |
|---|---|---|
| merge A+B→C | C.predecessor_ids=[A,B]；迁移索引给 A/B 的 successor_ids=[C] | 原来源、事件、旧图全留；重算去重与机会数，不能直接累加旧权重 |
| split A→B+C | B/C.predecessor_ids=[A]；迁移索引给 A 的 successor_ids=[B,C] | 每个事件明确归属或 unresolved；共享上下文不重复算计划；旧金标失效 |
| 管理从 A 移到 B | 产生 A'/B' 并分别关联旧身份，记录移动边 | A 与 B 两条生命周期及其所有下游都重新计算 |
| correction / 撤回 | 新图重放、旧图 tombstone；授权撤回还禁用原始及派生消费 | 审计保留不含原文的允许证据；不能通过旧缓存或备份恢复消费 |

旧 EP 的 successor_ids 在创建时无法预知未来；禁止为补它而原位更新旧 Parquet。
采用追加迁移索引表达后继，最新谱系视图解析后继；旧 manifest 原始行维持原值。索引与视图语义需 G0 冻结 CR-07。
tombstone 是消费撤销标记，包含旧对象版本、替代关系、原因、批准者与生效时刻；不等同物理删源。
当前 R 没有 tombstone 列/表，不能借 label_status=rejected 或伪造 delete_notice 代替它；协议未定前禁止发布迁移版本。

依赖方向统一为“输入→派生”，反向索引用于查询所有消费该输入的后继；按这个方向逐层失效：

```text
source/media/time evidence → extract/field → candidate/adjudication → episode/decision snapshot
episode → gold labels / sampling frame / inclusion probability
episode + copy evidence → duplicate group → economic cluster
cluster + sampling weights → fold / purge / K / tier
decision snapshot + market/rules → feature snapshot / execution result / return cache
fold + feature + returns + labels → fitted model / selection / test result / published manifest
```

1. 固定 migration_id=hash(旧图集,新输入,规则,批准记录)，检查前驱版本仍是预期；并发迁移通过 manifest 代际比较阻止覆盖。
2. 计算受影响子图的传递闭包，先发消费屏障，使所有相关派生键拒命中；不能等重算完成才标 stale。
3. 在 staging 构建新 EP/EE、谱系与 tombstone，执行无环、外键、分支守恒、时钟、五反例验证。
4. 发下游失效任务，按 DAG 拓扑重算抽样概率、复制组/簇、fold/K/档位、特征、执行结果与统计结果。
5. 下游未确认完成则新完整研究 manifest 不发布；允许发布明确 incomplete 的 G1 审计产物，但 G3 拒消费。
6. 校验所有文件 hash 后，通过单一原子 manifest 指针切换一次发布；文件逐个 rename 不等于跨文件原子提交。
7. 崩溃前若仅 staging 落盘，不可见；若已设屏障，恢复后继续同 migration_id，不撤销屏障放出旧缓存。
8. 旧 manifest 可用于明确审计重放，但普通研究 API 拒绝 tombstone/stale 版本；撤权对象连审计原文也不得绕过权限读取。

下游 G2/G3 由其所有者实现重算，G1 只发依赖失效契约与等待回执，不越权写 market/research 文件。
跨锁箱边界先冻结评估，独立审核重新分配并记录暴露历史；看过收益的数据转开发，新最终时间窗另留。
任何修改 source 内容/媒体、时间证据、归属、规则/模型响应、schema、授权、市场规则或抽样方案均触发对应 DAG 重算。
冻结逻辑代码无变化、同输入重跑不触发新迁移；本次 ingested_at 的墙钟差异也不触发统计重算。

## 9. 五反例的独立期望输出

可信度：高，M B.1 明列反例、R §3；中，以下精确夹具实例。价格全为虚构，期望由人工手写，不调用引擎自产 golden。

| 反例 / 输入 | 描述图与作者轨期望 | 决策图 / 失效与下游期望 |
|---|---|---|
| 编辑 SL 不回填：m1 10:00 提议 SL=90，m1-v2 10:20 改 SL=95，10:25 才首次收到最终版 | 仅有最终版则 H1；P=active,C=unknown 可作描述；旧 SL 无独立证据不造 v1 | 10:00/10:20 快照无可用 SL=95；最早 10:25 加处理延迟后才可讨论新决策；原始入场隔离 VERSION_TIME_UNKNOWN。配对 V 子例若原版 10:00:02 已收，则旧快照保留 90，新版到达后才用 95 |
| 未成交触 TP：m2 提议 long limit=100，TP=110，行情始终高于 100，m3 说“TP1 到了” | m3=result_post；P=active,C=unknown，exit_observed=false；不生成 close_claimed | G1 不生成 fill；G2 桩期望 filled_qty=0，无 closed 事件；完整可评估未成交结果与删失分开，G1 不自行填净 R=0 |
| 超时只过期：m4 提议明确期限 11:00，无入场声称，11:00 到期 | P=expired,C=unknown，exit_observed=false；有来源的 expire 可存在 | 不生成平仓；若只是 H 观察终点且无期限，则 P 仍 active，right_censored=true，censor_reason=LABEL_RIGHT_CENSORED，结果缺失 |
| 同向双单不误并：m5 BTC long 计划 A=100，m6 BTC long 计划 B=95；m7 无指针“移保本” | 至少两个 episode/entry_branch_id；m7 两候选均保留，unresolved/ENTRY_LINK_AMBIGUOUS | 两个计划字段均不因 m7 改写；补充明确 reply B 后仅 B 新管理快照生效；复制组也不把 A/B 合为一笔 |
| 提前管理留缺入口：m8 “BTC long SL 移到 98”且父帖不可见；m9 “全部平仓”明确指向 m8 | m8 为 N,U、entry_observed=false,left_truncated=true；m9 后 N,C、exit_observed=true；不捏造 entry | m8 不是 invalid_transition；PARENT_MISSING 限制收益/入场用途，允许管理描述；m9 不补 entry_price 或收益零 |

每例断言 EP 状态、EE kind/归属、字段来源版本、闭包 hash、Q 原因、LOSS 分母；不能只断言函数不抛异常。
配对测试在加入未来编辑/结果/复制消息后重新构建旧截止，旧决策投影 hash 必须相同；完整描述图允许变化。

## 10. 合成夹具生成规格

可信度：中；满足 G §2 与 M A/B/C 反例覆盖；数量分配为低可信测试设计，不代表真实分布。
目标目录 `tests/data/fixtures/tdesktop_sample/`，生成器与 golden 都属于项目 tests 根，不放 src；本次仅设计不生成。
固定 seed=20260911，3 个虚构 peer_id，各 72 个唯一 source_id，共 216 条基本消息，满足每频道至少 60 条。
三频道重复使用 message_id=1..72，刻意验证复合键；不得靠全局递增 message_id 掩盖跨频道碰撞。
各频道消息分配如下，总计严格 72；表中类别作为主类别计数，回复/转发/编辑作为交叉属性另计。

| 每频道主类别 | 数量 | 必含内容 |
|---|---:|---|
| 提议 | 14 | 同币同向双单、多腿、区间/单价、纯观察像信号的对照 |
| 管理 | 16 | 改 SL、加仓主张、部分退出、提前管理、终态管理冲突 |
| 明确入场/全平声称 | 8 | 各 4；无提议全平、追述时间 |
| 取消/到期/纠错 | 6 | 各 2；冻结期限与仅观测结束分别出现 |
| 分析/结果/闲聊 | 12 | 各 4；“TP 到了”、指令注入字符串、非信号负类 |
| 相册成员 | 8 | 3+3+2 三组；成员文本/图片拆分、末图晚到 |
| 纯图 | 4 | 可读计划、模糊数字、图文矛盾配对、仅装饰图 |
| 转发/复制 | 4 | 明确原帖转发与无法证明来源的复制各 2 |

每频道另外选 8 个 source_id 增加编辑版本，其中 2 个再追加第二次编辑：每频道至少 82 个 MV、总至少 246 个 MV。
另输出原包精确重复记录用于 B0 去重，重复记录不计入 216 个源消息或 246 个真实版本的门槛。
每频道至少 12 条 reply（含缺父、多腿父、72 小时外父）、4 条 quote、4 条 plan_ref；至少 3 个跨频道复制组。
跨频道复制包括精确文本、相同图片不同文案、近似数字变化；最后一种必须验证不自动当同一计划。
基础时间覆盖 2024/2025/2026 与 UTC 跨日 6 小时窗口边界；补同秒有序/无序、迟到、秒毫秒混用、不合法时间。
V/H0/H1/H2/U 各有明确证据标签；历史最终版与真实收录版本放不同输入场景，不能伪装 TDesktop 可导出全部编辑史。
输出 TDesktop 风格 JSON、最小本地图片、不可变合成 observation sidecar；V 的实收/顺序证据来自 sidecar，不来自猜测导出字段。
图片使用确定性离线文字/几何绘制方案，OCR 响应录制并带 hash/bbox；不需要图像生成 API，不拿真实截图作夹具。
额外负向文件包括缺媒体、raw hash 损坏、OCR 数量级冲突、相册迟到、循环引用、伪造 LLM evidence id、回复错误频道。
G2 桩提供闭合/未闭合 bar、缺 bar、mark 过旧、历史改名、最终修订晚到、30 日不足；固定价格避免收益选择。
五反例分布到三个频道，每例有 expected JSON，明确输入版本、根、截止、P/C、所选/拒绝边、字段来源与原因码。
期望文件和生成器分离审阅；对输出逐字段比较，并检验 count/source/version/branch/episode/cluster 六种分母不混用。
已有 30 条 bench 只复用在授权和来源明确的解析器基准中；本任务不读取或扩散真实消息，合成集不冒称 bench 实测通过。

## 11. 模块与文件划分

可信度：中；以 G §2 交付目录、I 接缝与当前骨架为依据；均为未来实现路径，本次不创建。

| 文件（相对 quant-lab） | 单一职责 |
|---|---|
| src/quant_lab/data/normalize.py | 导出适配、UTC、MV、相册成员及媒体证据 |
| src/quant_lab/data/dedup.py | 精确规范引用、复制候选与映射账；不合生命周期 |
| src/quant_lab/data/extract.py | 全类型分类、parser、span/bbox、独立抽取结果 |
| src/quant_lab/data/llm.py | 纯接口、上下文裁剪、响应校验、录制 provider、usage；真实 provider gated |
| src/quant_lab/data/validate.py | 单位/品种/档位与 G2 as-of 校验，原因及用途状态 |
| src/quant_lab/data/linker.py | 候选索引、强弱守卫、裁决编排、一次回流 |
| src/quant_lab/data/lifecycle.py | 双状态 reducer、supersedes、缺入口和删失；不模拟撮合 |
| src/quant_lab/data/graph.py | 描述图、闭包 DFS、决策快照与确定性图 hash |
| src/quant_lab/data/lineage.py | DEP、migration、tombstone、失效任务与回执状态 |
| src/quant_lab/data/storage.py | schema 校验、不可变 Parquet、manifest 发布/恢复、幂等键 |
| src/quant_lab/data/audit.py | 全窗口框、盲复标、固定抽样、签字、损耗与 Q 处置历史 |
| src/quant_lab/data/harvest.py | 仅独立研究导出适配；真实接入等授权，不 import services |
| src/quant_lab/data/api.py | 保持 R §7 签名、默认决策视图、拒绝 stale/缺闭包 manifest |
| src/quant_lab/data/codes.py | 现有枚举；仅经 G0 仲裁后对齐层编号/强边概念 |
| tests/data/test_*.py / fixtures/ | 独立期望、性质、重放、故障夹具；不放 src 内 |

reducer、候选生成与闭包计算为无 I/O 纯函数；存储、时间证据与模型 provider 通过显式参数注入，便于离线反例验证。
不增加数据库服务或生产依赖；Parquet+manifest 足够支持单写者批次，跨进程发布比较代际，竞争失败退出重读。

## 12. 测试矩阵与发布条件

可信度：高，验收方向来自 G D-03–D-10、M B.3–B.5；中，具体断言与测试拆分。

| 测试组 | 必须断言 | 对应层 |
|---|---|---|
| schema/normalize | MV/EX/EP/EE 物理 schema、UTC/单位/可空性、相册晚到、raw hash；未知不得零填 | B0–B4 |
| dedup/loss | 重复导出不增版本、跨频道同内容保留、多腿映射、各单位损耗守恒 | B0/B1 |
| extract/llm | parser 与模型独立、两空不一致、span/bbox 越界拒收、证据 id 伪造拒收、一次扩展/两次失败重试封顶 | B1/B3 |
| validate/asof | 未闭合 bar、同秒严格比较、120 秒边界、三倍数量级门、远挂单复核、未来校准不可用 | B1 |
| linker | reply 多腿歧义、plan_ref 复用、强指针超窗、复制不并单、窗口候选未来扰动不影响旧选择 | B2/B3 |
| transition_product | 12 个 P×C ×全部 kind 穷举；每个 I 均 invalid_transition+保留事件+状态不变；守卫真/假各一例 | B4 |
| five_counterexamples | §9 的独立字段级期望全部通过；提前管理合法，未 fill 不闭仓 | B4 |
| decision_closure | 分别把节点/边/字段/媒体/特征 available_at 推后，相关旧快照必须拒收；环、缺父、同秒未知、未来输入不变性 | B4 |
| migration/recovery | 二合一、一拆二、管理改归属；旧缓存全拒命中、旧图可审计；staging/屏障/提交前后崩溃恢复幂等 | B4/B5 |
| audit_sampling | 正负全窗口非零 π、同窗漏机会、多腿、跨窗管理；盲复标原始分歧/未决不被仲裁清零 | B5 |
| acceptance | 200/3 固定一次、致命停批、少于 200 全审、重验最多一次、独立复核缺人 not_run | B5 |
| replay/property | 打乱输入文件顺序、重复运行逻辑 hash 不变；晚版本追加旧快照不变；ingested_at 新湖差异不改变语义 hash | 全层 |
| revocation | 一 source 撤回后全部派生键拒读，备份恢复先应用 tombstone，无法收到回执不放行 | 全层 |

真实主验收需 I 闸门；工具先在合成上验证 M 的 OC 数值 200/3、p=0.5% 约 98.13%，0/400 同 p 约 13.47%。
开发 200 与验收 200 分离；开发随机 50 普通盲复标、验收 40，危险例独立复核；普通字段/整例/未决门按 M B.3 预冻结。
全窗口 recall 使用 `ΣTP/π ÷ Σ真实机会/π`，在频道内按窗口簇重采；无真实机会 insufficient，不把预测框精度称召回。
主样本、定向补审、最终集全审分账；最终集全部复核、不得用抽样通过替代；每个版本一次正式验收、修复后最多一次新样本重验。
未来实现验证命令为 `.venv-g1/bin/python -m pytest tests/data -q`；本 ADR 没有执行它，也不声称 D-03–D-09 已完成。
合成通过只证明指定不变量，不证明真实召回、历史捕获率、LLM 质量或 H0/H1 假设成立。

## 13. 未决

以下恰 5 条；默认出口不等于已批准。契约结构缺口另列 CR，不重复计入本表。

| ID | 未决事项、可信度与依据 | 当前出口 / 闭合责任 |
|---|---|---|
| U-01 | P 终态而 C 仍 open 的管理：M B.1 同时说管理 C未知/持有可保留与终态后管理冲突；中，文字解释有歧义 | 按 §3 保守 invalid_transition、保留作者证据；G0 仲裁作者轨规则，禁止借 G2 规则自行改作者轨 |
| U-02 | H0/H1 接受条件、分项延迟与历史模型处理可迁移性；低，M A.1/C.1 无实测参数 | 仅合成固定参数；真实历史用途不足，不以 edit_date 或接受假设升级 V |
| U-03 | 近似复制阈值、上下文 20/32/64 与图片/token 限额的召回/成本权衡；低，M B.1/B.5 | 初版仅精确复制召回、超额拒答；开发夹具及获授权 PoC 冻结后启用近似 |
| U-04 | T_plaus 开发样本、波动率诊断下限在修补后语义；低，M C.2 P05 | 无有效冻结校准则价格合理性 insufficient；不恢复旧 k×s 门，不猜阈值 |
| U-05 | 真实导出、保留/云、预算人力、200/3 风险与独立复核人尚未由本任务确认；高，I §6/M B.3–B.5 | 仅合成与录制响应；不采集、不付费、不安排真实批次，不声称真实放行 |

## 14. 契约修订建议

以下恰 10 条，仅提交设计建议；当前契约仍未改变。本任务不运行 block/note 命令，后续由 G0 纳入仲裁。
此节是缺失物理类型、内部表与新字段的唯一提案区；正文引用它们均表示发布依赖，不表示已可消费。
可信度：高，缺口可从 R 与 M B.2/C.3 对照；中，具体补齐方案；数值精度等候冻结。

### CR-01：补齐公共 Arrow schema、空值与 EE 约束

建议冻结 decimal 精度（候选 decimal128(38,18)，越界隔离而非截断）、entry.kind、size_hint、forward_from、claimed/reconstructed_outcome 子结构。
建议 channel_id:int64、trader_id:string?、instrument_id:string?、side:已有枚举?；身份字符串、predecessor/successor:list<string>；censor_at:timestamp?、censor_reason:string?。
建议 sampling_probability:float64? 且有效范围 (0,1]、signoffs:list<struct<reviewer_id:string,decision_at:timestamp,scope:string,record_hash:string>>、audit_stratum:string?。
建议 EE event_seq:int64?、link_confidence:float64?（非概率保证）、edge_available_at:timestamp?、supersedes_event_id:string?、link_method:现有枚举?；根事件无归属边允许为空。
冻结 EE.kind 是否完全复用 EX.kind；冻结三时钟/无源占位的可空性、EP event_time 聚合规则、span 字符偏移与 bbox 坐标类型/范围；不得填假 extract_id 表示期限事件。
明确由源提议派生的 expire 如何引用 extract/source，研究 censor 不伪造 EX；根/候选/期限事件的外键规则需要一致。

### CR-02：解决不可变 bronze 与 version_no 晚到排序冲突

source_version_id 继续唯一稳定；建议 version_no 作为特定 manifest 的排序视图，不作为跨版本外键。
增加版本排序索引 `source_id,source_version_id,manifest_id,version_no,sequence:int64?,sequence_evidence_ref:string?,T`；未知同刻不以排序结果宣称顺序已知。
冻结 version_evidence 的规范序列化与 hash 输入；晚到重建新索引不修改旧 MV。未定前检测到需重编号即隔离待审，不静默改旧文件。

### CR-03：时间证据与 temporal_assumptions

建议内部 Parquet `assumption_id:string,object_ref:string,grade:enum,evidence_refs:list<string>,assumption_kind:string,delay_us:int64?,sequence_policy:string,frozen_at:timestamp,assumption_version:string,T`。
补 asserted_event_time、unknown_reason、version_evidence、媒体实收证据的具体存储位置；建议独立证据表，避免每表无约束 JSON。
仲裁“历史导出 snapshot_at 为空”与 H1 首次可证快照的关系，明确观察源类型和首次证实时间，未知时钟可空并限制用途。
区分内容可知时间、事后审核时间、历史处理模型假设与实收处理时间；全部假设参与 derivation_hash，不能把现代 LLM 执行冒称历史 V 处理。

### CR-04：规则候选与裁决内部 Parquet

CB 提案：`candidate_id:string,from_extract_id:string,to_extract_id:string?,target_source_ref:string?,method:string,strength:enum<weak,strong>,evidence_refs:list<string>,reason_codes:list<string>,selected:bool,edge_available_at:timestamp?,rule_version:string,T`。
内部 method 可表达 copy_fingerprint，但不得传播为公共 EE.link_method 新枚举；强度守卫及缺父端点独立表达。
JD 提案：`request_id:string,input_hash:string,action:enum<link,new_episode,unresolved>,selected_candidate_id:string?,evidence_message_ids:list<struct<peer_id:int64,message_id:int64,source_version_id:string>>,rejected_candidate_ids:list<string>,reason_codes:list<string>,context_manifest_hash:string,response_hash:string,attempt:int32,provider_version:string,T`。
JD 的 span/bbox、扩展父 task、usage 和校验错误另用固定 struct；缓存键包含输入裁剪、截止、schema/prompt/model/规则，禁止换模型沿用旧响应。

### CR-05：字段血缘、派生依赖与映射账

DEP 提案：`dependency_id:string,consumer_ref:string,producer_ref:string,producer_version:string,field_path:string?,evidence_ref:string?,available_at:timestamp?,required:bool,rule_version:string,event_time:timestamp?,ingested_at:timestamp`。
producer 可指媒体、候选集合、规则/映射、校准产物、市场分区、模型上下文与单字段；明确依赖方向、反向索引、循环拒收规则。
MAP 提案：`mapping_id:string,batch_id:string,stage:string,input_ref:string,output_ref:string?,relation:enum<one_to_one,split,merge,duplicate_ref,excluded>,estimand:string?,weight:decimal?,reason_codes:list<string>,rule_version:string,T`。
冻结 MAP/LOSS 的单位、互斥状态、权重空值、stratum 类型与累计集合序列化；补 LOSS 三时钟及 C 清洗层与 B 产线层的独立命名空间。

### CR-06：决策快照、多时点公共读取与 payload

建议显式快照表 `decision_snapshot_hash:string,graph_version:string,episode_id:string,root_event_id:string,t_dec:timestamp,decision_eligible_at:timestamp,dependency_hash:string,graph_kind:enum<description,decision>,temporal_assumption_ids:list<string>,T`。
冻结每快照 EP/EE 投影主键，允许同一 episode 多个决策而不把最终作者状态/结果暴露为入场特征；与 G2 ExecutionRequest 接缝一致。
仲裁 load_episodes/load_episode_events 的 as-of 与图类型绑定，可新增 API 或由 manifest 绑定，不能悄悄改现签名。
冻结 EE payload 中诊断、字段补丁、证据引用与 invalid_transition 的结构；不得把它当任意扩 schema 逃生口。
冻结 eligibility_by_estimand 键、严格 V 与 H0_conditional 用途口径、审核后准入和决策当时信息的边界。

### CR-07：tombstone、谱系与原子失效协议

建议迁移表 `migration_id:string,old_graph_version:string,new_graph_version:string,predecessor_ids:list<string>,successor_ids:list<string>,migration_reason:string,approved_by:list<string>,effective_at:timestamp,T`。
建议 tombstone 表 `object_ref:string,object_version:string,migration_id:string,reason_code:string,successor_refs:list<string>,revoked_at:timestamp,T`，并定义永久撤权与普通 stale 的不同读取权限。
定义旧 EP successor_ids 的解析规则、旧 manifest 审计读取、最新索引、DAG 任务状态与 G2/G3 回执；不能原位补历史 Parquet。
冻结 manifest 代际、文件 hash 清单、consumer 屏障、staging/提交恢复、锁箱冻结与暴露历史，确保批次不混图版本。

### CR-08：审核、质量处置与抽样协议表

AUD 提案：`audit_plan_version:string,batch_id:string,frame_hash:string,n:int64,c:int64,seed:int64,error_count_by_severity:map<string,int64>,p_hat:float64?,interval_method:string,interval:struct<lo:float64,hi:float64>?,acceptance_result:enum<pass,fail,insufficient>,attempt_history:list<string>,review_hours:float64,excluded_layers:list<string>,low_confidence_rule:string,T`。
另冻结全窗口框、包含概率、原始盲标签/独立复标/仲裁签字的版本引用，以及小批全审与最终集全审状态。
Q 唯一键与“处置历史只追加”目前有冲突风险；建议另设处置事件表、当前 Q 为视图，原诊断不重复，释放需签字和新版本。

### CR-09：市场检查证据接口与规范化版本

现 mark_price_at 只返价格/原因，不足以构造闭包；建议增加伴随证据接口返回 bar/source ref、close_time、available_at、sequence、market_manifest、time_grade/assumption refs。
补 symbol 映射、单位映射与 T_plaus 校准产物的版本及可用时刻；验证 EX 继承 MV 时钟、检验结果独立依赖的物理表示。
建议 validation 表 `validation_id:string,extract_id:string,check:string,reason_codes:list<string>,dependency_refs:list<string>,threshold_ref:string?,eligibility_by_estimand:map<string,bool>,rule_version:string,T`；G1 不直接改 G2 schema。

### CR-10：对齐六级编号、强边语义与代码枚举

由 G0 明确 B0–B5 为 episode 六级，C1–C5 为清洗子层；K.Layer 当前六级和 G 的“损耗五层”需给兼容映射，不能直接重命名既有数据。
STRONG_LINK_METHODS 不应充当无需守卫的强边判定；reply/quote 仍可能缺父/多腿，plan_ref 唯一时可强，复制方法暂只在内部候选表。
同时冻结作者 P 终态管理裁决后的回归表版本；本次不增删 ReasonCode，不将 invalid_transition 注册为原因枚举。
