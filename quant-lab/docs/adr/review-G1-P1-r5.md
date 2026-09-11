# quant-lab G1 P1 第五轮独立复审（r5）

审查日期：2026-09-11（Asia/Manila）；当前 Codex 主会话独立执行，未委派其他模型。
证据完整性：完成
范围：S01–S16、T01–T04、U01、V01–V04；七项设计规模残余全部纳入，没有以 P2 身份豁免任何一项。
真实 PoC、真实 LLM、真实批次放行继续 gated，不作为失败理由。
唯一仓库写入是本文件；源码、测试、契约、看板和共享湖均未由本会话改动。

**闭合结果：S 13/16、T 4/4、U 1/1、V 3/4；新增必修 W01–W03，共 3 条。**
S08、S13、S14 仍 open；V01 仍 open。其余列明原项 closed。
closed 指本项指定反例及已生效关闭条件通过，不是整个模块无缺陷的承诺。
W 与原项有关联，不把 S/V/W 数量相加冒充互不重叠的缺陷数。
223 项测试通过，但离线录制精度、数量证据校验、正式标签完整性仍有实跑反例，故不能通过。

## 1. 固定树、材料与审查边界

开审 HEAD：`ff167baedbef974980e2ed66e744ef510bef740d`；本轮审查工作树，不用 HEAD 代替未跟踪文件内容。
摘要算法：排除 `__pycache__`，按路径排序，拼接每行 `仓库相对路径:文件SHA256\n`，然后 SHA256；路径包含 `quant-lab/`。
下表全部是该算法的聚合摘要，包括单文件行；不是单文件裸内容 SHA256。

| 开审对象 | 文件数 | 聚合 SHA256 |
|---|---:|---|
| src/quant_lab/data | 17 | 6baa25f72e3e2b18c0fa41ae630959c830650b3471747203b19d9adcc7bd911e |
| tests/data | 84 | bbdb216aed21778d22e1150886b52f9089a9c8d166dba04195551f7af55ad222 |
| research-schema.md | 1 | 608b34ebcd6fa838e4cbf37520987fd6c9889c4a5eb2d45e1eb4cb5b7ff7e703 |
| ADR-G1-episode-engine.md | 1 | 0848d98f0c477c01d9c554540084d586604d79479d69350cb8b163c91aff4c95 |
| taskList.json | 1 | 307a0418a0d7dbc99c255300ad7113a186b1993ef469cc2050e94d3c72a4bcec |
| review-G1-P1.md | 1 | 10bda6b285579361f927b8067e71f986e7e7a3d07bda22334c3f43771aa0d963 |
| review-G1-P1-r2.md | 1 | 3dda46df6321afd29781681965a41dce8c2e040cf71c89a39e5d1bde5e5e2776 |
| review-G1-P1-r3.md | 1 | bc807e851c7f8bf423fbcdd8f381bac4d53412943ba78c79d78d9ab72fb00954 |
| review-G1-P1-r4.md | 1 | d45c16d4de46927ebe0f737d2e1c160df2cff10eb4d073f2eef82c700bf6ed08 |
| quant-lab/data（全共享湖） | 2649 | 74f60edd9b052255ee6f7c825f900f529225bffebba997949d478735fe1e363a |

全套、看板及补充探针后，源码、测试、ADR、四份旧报告、共享湖的逐文件路径与内容摘要均相同。
契约与看板在审查期间被外部会话更新；不声称它们全程静止，也未回滚其更新。
收尾读取 research-schema 裸文件 SHA256=`6b95e590e59edcf67ee3837e003b54e574d07f761d33da0d04cf653ee6ea3f4a`。
收尾读取 taskList 裸文件 SHA256=`9d880900368a7dabbb6d06ba6cf64de21206d2cd7ed623250bf763be2ffb8965`。
指定 §9.10.1–§9.10.14 已逐项核对；收尾契约另有 A19/A20 报告完整性声明条款，本报告提供明确完成声明。
复核材料包括 AGENTS.md、GROK.md、前三轮问题与探针、r4 §6/§7/§8/§9、ADR、contracts/research-schema、data 源码及对应测试。
`R5_CLOSURE_EVIDENCE.md`、`REVIEW_EVIDENCE.md` 与 modules.data notes 仅用于定位线索，不采信其中自报 pass、旧湖行数或完成状态。
检查过 code-review skill；其 merge-base 差异双代理流程不适用于本次固定目录逐项裁决，未启用该工作流。

## 2. 实跑命令与输出

所有 Python 执行均设置 `PYTHONDONTWRITEBYTECODE=1`；pytest 设置 `PYTEST_ADDOPTS=-p no:cacheprovider`，主套另加 `-s` 以记录原探针 stdout。
所有构建及测试外层 `QUANT_LAB_DATA_ROOT` 指向 mktemp 目录；shell 使用 `set -o pipefail`。
C1：`cd quant-lab && set -o pipefail && .venv-g1/bin/python -m pytest tests/data -q`。
C1 输出：**223 passed in 30.37s，rc=0**；包含 test_review_probes.py 的前四轮固化反例及本轮实现者新增断言。
C1 固化执行器直接提取 r2/r3/r4 标记区，按 AST 顶层语句执行；r1 反例由 R/X 和 CE 对应输入复现。
仅将旧夹具 ID 103/120/140 经 ANCHORS 映射；未改反例文本、扰动、时钟或业务预期。
预期的 LookupError 被记录为负向结果；其余异常不会被当成成功，固化 fixture 末尾断言 failures 为空。
C2：逐条读取 modules.data 当前 verify，经 `bash -o pipefail -c` 执行；D-08 保持 gated。
C3：本报告新增的内存/临时湖反例；不是安装真实 provider，不涉及联网或生产账号。

| 看板项 | rc / 状态 | 实跑输出及限度 |
|---|---|---|
| D-01 | 1 | collection 文本不匹配 `collected [1-9]`；不是模块导入或223项测试失败 |
| D-02 | 0 | ADR 行数、invalid_transition、tombstone 关键词门通过 |
| D-03 | 0 | 19 passed in 0.63s；248 MV，added=0；固定 `/tmp/ql-norm` 是复用输出，不能证明新批次 |
| D-04 | 0 | 11 passed in 0.29s |
| D-05 | 0 | 19 passed in 0.49s；bench 30条的粗动作召回不等价于全窗口字段质量 |
| D-06 | 0 | 9 passed in 0.40s |
| D-07 | 0 | 33 passed in 14.89s；公共默认视图 `(56,4)`；三列非空；构建与读取均在自身 mktemp 根 |
| D-08 | gated | 未调用 harvest；真实 PoC 不纳入本轮失败计数 |
| D-09 | 0 | 9 passed in 1.45s；200/3=98.1319%，400/0=13.4658%（p=.5%） |
| D-10 | 1 | 写报告前读取 r4，正确未放行；当前正则仅识别二/三/四审，见应改 |
| D-11 | 0 | 在本轮新临时湖执行原 verify，8条 layer=5 非零输出 |

另一次独立新湖构建：`fixture-v1@63866609`；input_hash=`638666093dbebba2fa1ddcdfe83dd4179497a2ae0822a8548e092bef5de4e6d1`。
批次 `tg-de7710d283f0`；manifest counts：episodes=75、events=124、decision_roots=61；默认决策 API 56行。
decision_roots 统计有 t_dec 的根，默认 API 再按用途/致命原因筛选，所以61与56不冲突。
index 为1条 published 实际版本，别名解析所得版本与公共行中的版本一致。
D-11 另有 `unconfirmed_repost_candidates=12,n_reviewed=0,status=not_run,confirmation_rate=null`；未审核不伪造确认率。
主日志 `/private/tmp/g1-r5-full.log`；补充日志 `/private/tmp/g1-r5-extra.log`。
看板日志目录 `/var/folders/js/d8w4bf351gvdk84fn820t3nw0000gn/T/g1-r5-board-nd_iqxry`；摘要目录同级 `g1-r5-review-eik59s3d`。
日志是便利复查入口，下文已保留裁定必需的输入、输出和源码，不依赖临时文件长期存在。

## 3. S01–S16 闭合裁定

| 项 | closed/open | 本轮输出与裁定 |
|---|---|---|
| S01 | closed | R01/X01-correction/X01-original 全列 diff=[]；原始根 n_events=[1,1]；未来复制一根→两根→三根的旧行逐列相等，含 hash/stratum |
| S02 | closed | R02 available_at=2024-04-02 10:25Z，10:21 visible=false；H1 t_dec/order_plan=null、原始入场/执行=false；CE1 V旧SL61000保持 |
| S03 | closed | R03父链边推迟T+1d；根相册03-04→03-05；r4父相册反例 A*=2024-06-11 09:01Z，refs含media:image:image-hash；集成子边和根分别T+1d/T+1d+1s；missing/clock/cycle、purpose、sequence正反例通过 |
| S04 | closed | future/missing_available/null mark全为null/MARK_STALE；无样本校准=no_frozen_T_plaus_with_sample_count；120s、未闭合、阶梯锚点及显式synthetic门测试通过 |
| S05 | closed | R05a冲突reply=weak/unresolved；R05b两源均canonical；R05c无同源两腿误并；多分支、quote唯一/歧义、作者命名空间配对通过；V02默认API同簇且旧快照不变 |
| S06 | closed | R06a/c为6/5/7，其他币单位不传播；R06b/d未知入场不猜市价；未知fraction=null，显式.25保存；高精度录制问题归S08/W02 |
| S07 | closed | 混合原因UNIT_SCALE_CONFLICT/fatal；RAW_HASH_MISMATCH/SCHEMA_DRIFT/KEY_DUPLICATE_OR_ORDER/MEDIA_MISSING均execution=false；方向冲突INTENT_AMBIGUOUS/fatal |
| S08 | open | R08原因码29、身份别名/签名正确、时钟mismatch=0、basis非空，物理经济列Decimal；parser高精度gold逐字测试通过，但录制JSON经float后EX/CP静默改值，W02/V01 |
| S09 | closed | R09 active/future_expire=0/future_roots=0/delete_allowed=true；明确撤回两种文本×有无reply触发supersede/replay_required，无法定位unresolved；合法reopen持久化，跨币/跨作者无前驱 |
| S10 | closed | R10两时钟变/仅edit变均2行；r4 same_id=false、different_event_time=true、rows=2、Q=0；改期限/MV时钟/同版TP分配均改hash；同输入幂等及首入湖时刻测试通过 |
| S11 | closed | 缺/空/部分manifest、错版本、hash/status/tombstone及stale两API反例拒读；T01虚假counts也LookupError；A9最小G1发布屏障正反例通过 |
| S12 | closed | r4 mv=248、L2 mapped=248、missing=0；5个service显式退出；L1输入251及输出MV全集，L2–L6实物输入集−MAP输入集全空；review/excluded并集逐层逐stratum等于LOSS，计数恒等式通过 |
| S13 | open | r4无/非法bbox均undecidable、entry/stop=null、OCR_UNREADABLE；跨品种/方向/数量/期限比较通过，未来证据和第21候选拒收；但合法LLM仓位/数量/名义金额证据会TypeError，且精度录制错值通过校验，W01/W02 |
| S14 | open | 原空/部分标签、负窗口、n201/attempt0/身份门通过；正式历史fail1→带修复新样本pass2→第三次insufficient；但正式记录只查关键键存在，可收全空关键值并pass，W03 |
| S15 | closed | r4具体测试缺口已修：edit-only期望2；T03断最终undecidable；A12默认公共API要求checked>0；216源/248MV/五级/跨年/seed、独立转移golden及CE1–CE5实跑通过。新增W路径的测试缺口单列，不以“出现新bug”自动重开整个矩阵项 |
| S16 | closed | R16绝对/父逃逸/符号链接在hash前拒绝，sha256=null/exists=false；Windows分隔符与真实无敏感sentinel正反例通过 |

S closed=13、open=3。S03/S05/S09/S12/S13/S14/S15 七项均纳入；没有沿用三审的设计规模豁免。

## 4. T/U/V 闭合裁定

| 项 | closed/open | 实跑输出与结论 |
|---|---|---|
| T01 | closed | r3十九个缺键/空值/缺文件/错版本全LookupError；r4全零counts拒读；3个计数分别改值×两API均拒读 |
| T02 | closed | 同graph_version unknown fraction=null→known=.250000000000，snapshot_equal=false；未来fraction扰动snapshot_equal/plan_equal均true |
| T03 | closed | valid单框仍缺stop证据也undecidable；负/短/NaN/Inf/倒序/越界框全部拒收数字且最终undecidable；不再只断bbox空 |
| T04 | closed | D-07原verify在mktemp构建读回成功；rm目标仅其自身tmp；共享data全树2649文件前后相同 |
| U01 | closed | size=[8]、['bad',8]、[8,NaN]均unreadable/numbers=[]；正常尺寸正例通过 |
| V01 | open | 原60000.123456789123 canonicalize反例现返回相同q12与Decimal；parser EX/CP/EP往返和源串gold逐字通过。但全链禁止有损转换仍被RecordedClient JSON数值路径击穿，W02 |
| V02 | closed | r4默认API两根cluster均dg-96b3e0d1274dfce4；一/二/三根前缀全列不变；A12真实默认API非空组核查通过 |
| V03 | closed | r4无physical sample IDs三次均非pass；新正式历史按制品身份派生attempt，改计划名/无修复/重复样本/第三次均拒；空样本、错hash、外来样本、缺键标签均拒。W03属于标签值校验残余，不是计划改名绕过复现 |
| V04 | closed | r4 ETH/b根predecessor_ids=[]、migration_reason=null；BTC/a合法reopen正例有唯一前驱，跨币/跨作者/两者皆跨负例全部无前驱 |

T=4/4、U=1/1、V=3/4。V01原局部反例修复不能代替A18对全部精度字段路径的约束。

## 5. 二审 §4 六项关键性质重裁

| 性质 | 裁决 | 证据与边界 |
|---|---|---|
| 决策视图全列不变性 | 指定扰动通过 | R/X全列空差集、未来correction和复制前缀；没有丢弃hash列降低比较标准 |
| H1不复活 | 通过 | 10:25首次证实、10:21不可用、H1旧机会无计划；V旧SL与后来修订分离 |
| manifest屏障 | 指定反例通过 | 缺键/缺文件/hash/tombstone/stale/counts都拒读；新湖EP/EE与manifest实读核对 |
| 路径逃逸 | 通过 | 两读取器在hash前拒根外路径；真实临时sentinel及Windows路径测试通过 |
| 损耗守恒 | 指定六层性质通过 | 输入实物集合、全部映射退出、逐stratum累计集合与局部计数等式同时验证；未知权重留空 |
| OCR/裁决离线协议 | 未通过完整性质 | OCR图像/bbox及裁决截止/截断已修；W01合法字段导致异常、W02错误数字被当作有证据的录制结果 |

## 6. 契约核对

| 契约 | 裁决 | 本轮证据 |
|---|---|---|
| A8经济量物理类型 | 通过 | 新湖gold order_plan entries.price_lo/hi/fraction、stop.price、tps.level/fraction、sizing.qty及dec_stop/dec_tps均decimal(38,12)；EX/CP经济列往返同型 |
| A8哈希q12 | 指定性质通过 | Decimal('6')与Decimal('6.000000000000')同hash；delta_lo诊断变更不改_sig；q12不去尾零、不用科学计数法 |
| A18字段路径保真 | 未通过 | parser原文64094.879166666667/63000.123456789123/65500.000000000001→gold str逐字相等测试通过；RecordedClient.from_file→LLM EX/CP却分别变为…666/…120，reason_codes=[]，W02 |
| A9 index.json | 指定发布/失效性质通过 | 新湖1项published、实际版本与input_hash；测试发布另一版、旧版tombstone及successor、built_at升序、不可变换版均通过 |
| A10 basis | 通过指定端点例 | [100,10000]对mark100为far_end_only，指向entries[0].price_hi；Q字段路径/observed basis与L5主原因归因测试通过 |
| A11原因码别名 | 通过 | ReasonCode is Reason、FATAL_REASONS is FATAL；29项逐字集合测试通过 |
| A12不折叠且同簇 | 指定同文重发通过 | 两源保留、默认公共API同簇；候选进入repost_same_channel stratum；D-11报告12未确认、n_reviewed=0、rate=null |
| A13六键非空 | 通过 | 新湖56行，description/entry_decision/execution/original_entry/price_check/outcome逐键null_count全0 |
| A14 outcome/别名 | 通过指定语义 | 默认决策outcome_true=0；作者三种终态在描述视图；fixture-v1解析到@63866609，公共行用实际版本；显式旧名保留 |
| A15七值+censor_reason | G1列与校验通过 | 七值集合精确，五键含censor_reason；unevaluable缺原因返回unevaluable_without_censor_reason；初始G1不伪造G2重建结果 |
| A16/A17验收门 | 有应改项 | 本轮没有改verify；D-01文本门失配，D-10未识别五审；不能拿执行器rc代替实际业务断言 |

A9明文不要求的跨模块ack、备份恢复、保留期清理不扩充为新验收；这不是把七项设计残余重新降为P2。
关于A18的静态抽验：只grep `stop/price/qty` 同一行上的 `float(` 不充分；`json.loads` 默认先造float，随后 `Decimal(str(...))` 无法恢复源串。
`llm._num` 仍返回float；虽多数调用只用于检查，数量字段混算会直接崩溃。统计诊断可用float不等于经济字段校验可以有损。

## 7. 固化期望抽验

| 位置 | 是否遵循报告口径 |
|---|---|
| test_review_probes.py:73、141 | R/X全列diff=[]正确；edit-only已从1改为2，符合r4 §6，不再固化丢版 |
| test_review_probes.py:211、510 | T03不只断numbers/bbox为空，还断undecidable/reasons；r4无bbox最终entry/stop清空，正确 |
| test_review_probes.py:540、734 | parser非整数往返和源串gold逐字是独立常量预期，不能被实现输出自动重算；但两者都没有覆盖JSON数值录制解码 |
| test_review_probes.py:563 | 复制前缀直接比较完整旧行；没有排除cluster/hash/stratum，符合r4 V02 |
| test_lifecycle.py:432 | 从默认API读取；checked>0且ep.height>0，取消空集成功分支，符合r4 §7 |
| test_review_probes.py:619 | 六层从上层实物取输入集合，不从MAP反推输入；累计规则另算，覆盖r4 L2服务消息缺口 |
| test_review_probes.py:363、685 | 正式历史、修复、换名与无样本测试有效；partial_annotation只删除键，未测关键键齐但值全空，W03暴露该缺口 |
| test_extract.py:104及R13-numeric | 有非法fraction=2和缺期限证据拒答；没有合法fraction/qty/notional span正例，W01不会被223绿发现 |

目视检查了3张更新的合成图：FIL LONG ENTRY1/ENTRY2/STOP，BTC LONG ENTRY LO/HI/STOP，BTC LONG ENTRY/STOP/TP。
数字分别与录制对应；原r4“图只画数字、录制凭空补方向和标签”的具体缺陷已修。
测试和样例输出不是对任意未来录制质量的保证；W02特意使用新的高精度数值录制，而不是复用整数golden。

## 8. 新增必修 W01–W03

### W01：合法LLM数量证据触发Decimal/float混算，离线抽取中断

定位：`src/quant_lab/data/llm.py::_num`及`validate_evidence`的`abs(parsed - number)`；调用端`extract.py::llm_extract/extract_frame`。
输入：`BTC 做多 入场 60000 止损 59000 仓位 10%`；录制payload fraction='0.1'，span准确覆盖10%。
执行真实RecordedClient→llm_extract，calls=1，抛`TypeError: unsupported operand type(s) for -: 'decimal.Decimal' and 'float'`。
另外合法qty='2'/span='2'、notional='10000'/span='10000'同样抛该异常。
原因：parse_number_token改为Decimal，但_num仍float；函数承诺非法/证据错误有界拒答，实际连合法输入都崩溃。
影响：extract_frame未拦截该错误，录制批次无法完成；不依赖真实LLM开闸。
必须改：数字校验全程Decimal，独立覆盖fraction/qty/notional合法与非法正反例，异常应走显式拒收出口。
关闭条件：三条合法记录不抛且完成字段证据检查；非法记录有界拒答，不吞错为正确数字。

### W02：JSON录制解码有损，错误高精度经济量被静默写入EX/CP

定位：`llm.py::RecordedClient.from_file`默认`json.loads`、`validate_evidence`的相对误差比较、`extract.py::llm_extract`的Decimal(str(...))。
临时录制文件的JSON数字是64094.879166666667和63000.123456789123；原消息span逐字覆盖同一数字串。
实际validation=[]，abstention=null；Parquet往返后的两种抽取器结果如下。

| 路径 | entry.lo | stop | reason_codes |
|---|---|---|---|
| parser EX/CP | 64094.879166666667 | 63000.123456789123 | [] |
| llm EX/CP | 64094.879166666666 | 63000.123456789120 | [] |

物理列仍decimal(38,12)，不能证明保真。相对1e-9容差把精度损失当作有证据的正确数值接受。
本例确认错误写至silver EX及CP；当前linker只选parser，**没有声称这次LLM错值已进入gold或触发真实交易**。
A8明确覆盖silver，A18覆盖声明精度的全部字段路径，所以parser→gold的正确性不能豁免此项。
必须改：录制JSON经济量以无损Decimal/字符串读取，响应hash序列化同步规范化；有损数字输入拒收或从已核实源span精确重建。
关闭条件：1e4–1e5非整数JSON录制从读入到EX/CP往返与源串逐字相等，非法/冲突数字必须拒收；现有parser→gold逐字测试保留。
归属：S08/V01和离线证据协议S13，新增编号用于标识本轮新找到的遗漏路径。

### W03：正式审核仅校验键齐全，空关键标签仍可签字pass

定位：`audit.py::acceptance_decision`的required_annotation检查；对instrument_id/side/entry/stop只查键存在，对值只检查kind。
先用固定制品身份、真实样本ID、可重算sample hash、独立审核者，提交200份“kind=entry_proposal、其余四关键值均None、severity=ok”标签。
结果：`result=pass,attempt=1,acceptance_records=1`；不是只调用无batch的计算器。
再用本轮api.build真实产生的75行描述gold，取完整实物episode_id集做小批全审，同样空标签得到`n=75,result=pass,records=1`。
同一空标注交disagreement_report则`status=insufficient`；新正式入口绕开了原有未决语义。
必须改：按标注字段的适用性和未决状态校验值，缺真值不能由调用方severity='ok'覆盖；原文无字段应显式记录适用性/依据，而非任意空值通行。
关闭条件：正式200抽审及小批全审两条路径都拒绝上述空标注，不产生pass/signoff；完整合法标签与一次有修复新样本重验仍可完成。
归属S14。V03的计划改名绕次数已关闭，这条是其新增正式记录机制中不同的漏验。

## 9. 新反例最小复现源码

以下以quant-lab为cwd，设置PYTHONDONTWRITEBYTECODE=1和QUANT_LAB_DATA_ROOT=$(mktemp -d)，用`.venv-g1/bin/python -`执行。
源码不改仓库；W03使用测试辅助器构造临时制品，独立修改输入并打印结果；正文另有真实75行gold复核。

```python
# R5_PROBE_BEGIN
import json, os, pathlib, runpy
from quant_lab.data import llm, extract, lake, audit
ns = runpy.run_path('tests/data/test_review_probes.py')
root = pathlib.Path(os.environ['QUANT_LAB_DATA_ROOT']).resolve()
assert root != pathlib.Path('data').resolve()
def emit(tag, value):
    print(tag, json.dumps(value, default=str))
for key, token, value in [('fraction', '10%', '0.1'), ('qty', '2', '2'),
                          ('notional', '10000', '10000')]:
    payload = {'size_hint': {key: value},
               'spans': [{'field': 'size_hint.' + key, 'start': 0, 'end': len(token)}]}
    try:
        emit('W01-' + key, llm.validate_evidence(payload, token))
    except Exception as error:
        emit('W01-' + key, [type(error).__name__, str(error)])
source = '64094.879166666667'
text = 'BTC 做多 入场 ' + source + ' 止损 63000.123456789123'
system, user = llm.build_extract_prompt(text, channel_name='synthetic', message_date=None)
spans = []
for field, token in [('entry.lo', source), ('entry.hi', source),
                     ('stop', '63000.123456789123')]:
    start = text.index(token)
    spans.append({'field': field, 'start': start, 'end': start + len(token)})
response = ('{"kind":"entry_proposal","symbol_raw":"BTC","side":"long",'
            '"entry":{"lo":64094.879166666667,"hi":64094.879166666667,"kind":"limit"},'
            '"stop":63000.123456789123,"spans":' + json.dumps(spans) + '}')
key = llm.record_key(system, user, llm.SCHEMA_NAME_EXTRACT)
recording = root / 'precise.json'
recording.write_text('{"items":{' + json.dumps(key) + ':{"response":' + response + '}}}')
client = llm.RecordedClient.from_file(recording)
result, abstention, _ = extract.llm_extract(text, client=client,
                                          channel_name='synthetic', message_date=None)
emit('W02', {'source': source, 'entry': str(result.entry['lo']),
             'stop': lake.q12(result.stop), 'abstention': abstention})
layout = ns['_audit_artifact'](root / 'audit')
sample = ns['_sample']('sample')
sample['labels'] = tuple({**row, 'annotation': {
    'kind': 'entry_proposal', 'instrument_id': None, 'side': None,
    'entry': None, 'stop': None}} for row in sample['labels'])
result = audit.acceptance_decision(
    n_sampled=200, errors_fatal=0, reviewer_ids=('independent',),
    producer_ids=('producer',), batch_id='b', layout=layout,
    graph_version='reviewed', input_hash='a' * 64, **sample)
emit('W03', {'result': result.result, 'attempt': result.attempt,
             'records': audit.read_acceptance_records(layout).height})
# R5_PROBE_END
```

## 10. 应改、可选与边界

应改：为W01补合法数量证据正例；为W02补JSON数值录制入口及银层逐字往返；为W03补“键齐而值空”的正式抽审/全审负例。
应改：D-01按collection退出码和明确条目数判读；D-03使用唯一临时目录，避免added=0被误当新鲜构建。
应改：D-10正则支持五审及后续轮次，精确读取完整性声明与末尾终裁；本轮fail不受该正则缺陷影响。
应改：D-05当前阈值仅parser_recall>0；不得把粗动作门通过描述为字段全正确。D-09保留两种OC独立数值断言。
可选：为每根重复计算复制图、API重复读取Parquet计数建立规模基准；此处不把未测性能升级为必修。
可选：对正式审核的样本抽取与修复制品关系补充更明确的使用文档，减少调用方把计算器与正式记录混用。

凭据边界：未读取生产凭据/session、未SSH、未连生产库、未调用真实LLM/OCR；默认NoNetworkClient/NoOcr及gate拒绝测试通过。
本轮录制payload、审核者名称与签字记录都是临时合成证据，不是生产授权或真实用户认可。
共享湖只做路径/内容哈希的读取；没有删除、重建、改别名、清理tombstone或触碰G2/G3分区。
D-07删除仅限自身mktemp；其他新增湖、录制文件和审核记录均在临时QUANT_LAB_DATA_ROOT。
源码/测试与共享湖逐文件前后相同，具体摘要见§1；契约/看板外部并发变更另行标明，没有冒称全仓库静止。

S/T/U/V闭合数：13/16、4/4、1/1、3/4；新增必修3条（W01–W03）。
五审终裁：fail
