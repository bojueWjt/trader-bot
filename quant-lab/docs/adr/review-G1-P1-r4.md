# quant-lab G1 P1 第四轮独立复审（r4）

审查日期：2026-09-11（Asia/Manila）。审查者：当前 GPT-6 Codex 主会话，未委派其他模型。
审查对象：当前工作树的 G1 合成链路，不以实现者自验、看板 done 或前三轮结论代替本轮执行。
范围：S01–S16、T01–T04、U01；七项原 P2 残余全部重新纳入验收。
research-schema §9.10.1–§9.10.12 已生效的裁定全部纳入；A9 明文延期的跨模块 ack、备份恢复和保留期清理仍不扩入 P1。
真实 PoC、真实 LLM、真实批次放行保持 gated，不计入本轮失败理由。
唯一仓库写入是本报告；未修改实现、测试、契约、看板或共享 quant-lab/data。

**闭合结果：S 6/16，T 2/4，U 1/1；新增必修 V01–V04 共 4 条。**
S closed：S01、S02、S04、S06、S07、S16。T closed：T02、T04。U01 closed。
open 表示仍有具体反例或原验收缺口；不否认其中已经通过的子修复。
V 与 S/T 有归属关系，不将两种编号相加冒充独立缺陷总数。

## 1. 固定工作树与证据边界

树摘要算法与前三轮相同：排除 `__pycache__`，按路径排序，将每行 `仓库相对路径:文件SHA256\n` 拼接，再取 SHA256；路径包含 `quant-lab/` 前缀。
源码、测试两棵树在原探针、看板 verify、补充反例和最终 pytest 前后相同。
审查使用工作树内容，HEAD 仅作定位；收尾 HEAD 为 `b9ce3c235d8d50a0de480c506fcd2f616c828613`。

| 对象 | 文件数 | SHA256 |
|---|---:|---|
| src/quant_lab/data | 17 | 2d7f05b2e4faa356f16e3fcab70494be6b684634f7a3c354fe24eb57ffc0bc5b |
| tests/data | 83 | 1f605a6253d191a5be3cc54ee7fd13c1ab08ec2e798c30494a4086325dc23978 |
| review-G1-P1.md | 1 | d1dd9f92671140448dcd4cee63a85ff927e50ec9f699790fd254967e571da60b |
| review-G1-P1-r2.md | 1 | 2d092efc2387dce4b90512d74f9b28c7123fcbe44be798ac37dcd461a5785cc0 |
| review-G1-P1-r3.md | 1 | 4660b5948e9159ddc78453a86e4fe978c572e5803dddeed862c8709c34f28689 |
| ADR-G1-episode-engine.md | 1 | 916615d0b13e3471c4f0dd5525ea897f6d6ca2377f3469ec24ce61d7997d3015 |
| research-schema.md | 1 | cb76219535d3f4142e8f33d8628350ab012e95148892afed76cc2dae311b78bb |
| execution-interface.md | 1 | 373084a199b786d7207e598d16cea91b2013b5f892588c83e970cfd8e59da4b4 |
| taskList.json | 1 | 70b2340915ecadcd2f3d94c7e40c519980ab9c6f9ca90857ff9778be2eb25441 |

契约和看板表内为收尾读取摘要，不将其冒称为整段审查期间均未变化的证明。
必读材料包括根 AGENTS.md、GROK.md、前三轮报告及 r2 §9/r3 §10 源码、ADR、两份契约、modules.data 逐轮 notes、自验记录和固化测试；核查了 data 源码、对应测试、夹具生成器、JSON/JSONL 与录制图像证据。
REVIEW_EVIDENCE.md 的 176 tests、看板的 177 tests、旧湖行数都仅作为线索；下列数字来自本轮执行。

## 2. 本轮实跑命令

cwd 为 `/Users/balen/projects/trader-bot/quant-lab`。
统一 `PYTHONDONTWRITEBYTECODE=1`、`PYTEST_ADDOPTS=-p no:cacheprovider`，构建及测试外层 `QUANT_LAB_DATA_ROOT=$(mktemp -d)`，shell 使用 `set -o pipefail`。
C1：`.venv-g1/bin/python -m pytest tests/data -q`，最终输出 **177 passed in 8.11s，rc=0**。
C2：同套测试启用输出捕获关闭（环境附 `-s`），固化模块实际逐语句提取 r2/r3 标记区执行；R/X/T/U 原始输出已逐项检查。
C2 只将旧消息 ID 103/120/140 转为 ANCHORS 的对应身份；未换反例的文本、时钟、候选、扰动或业务预期。
C3：本报告 §9 的补充反例及正文所述只读检查；纯函数、内存替身和临时湖，未对共享湖故障注入。
原 probe 的打印成功不等于性质通过；例如 `edit_only_changed_rows=1` 在 C2 中运行成功，在本报告仍判未闭合。

| 看板项目 | rc | 本轮输出 / 限定 |
|---|---:|---|
| D-01 | 1 | `--co -q` 后 grep `collected [1-9]` 未命中；不代表模块不能导入，C1 已完整执行 |
| D-02 | 0 | ADR 行数、invalid_transition、tombstone 文本门通过 |
| D-03 | 0 | 19 passed；251 observations、248 MV、added=0、Q=3；固定 `/tmp/ql-norm` 复用，不能据此声称新批次 |
| D-04 | 0 | 11 passed |
| D-05 | 0 | 19 passed；实际 bench 为 `../eval/v3_trader_signal_bench`，30 items；不是全窗口质量验收 |
| D-06 | 0 | 9 passed |
| D-07 | 0 | 原命令在自身 mktemp 根构建，33 passed；公共视图 `(56,4)`，instrument_id/t_dec/cluster_id 无 null；只清理该临时目录 |
| D-08 | gated | 未调用真实 harvest；实现仍是默认抛 NotImplementedError 的骨架，不冒充 PoC 通过 |
| D-09 | 0 | 9 passed；p=.5% 时 200/3=98.1319%，400/0=13.4658% |
| D-10 | 1 | 写报告前读取 r3；收尾原命令输出 `reading docs/adr/review-G1-P1-r4.md`、rc=1，正确反映本轮 fail |
| D-11 | 0 | 在 C3 新临时湖运行原 `--loss latest` verify，8 条 layer=5 非零输出；不是实际 PoC 1b |

C3 全新湖的批次是 `tg-656ccc240244`；别名构建得到 `fixture-v1@e5426214`，input_hash=`e54262140d41485fc578faf3cfe6f271bed1decc4cd8068cf732c5aa86b6c58d`。
该临时制品描述视图 75 行、决策视图 56 行，index 1 项，manifest published。
另对共享湖只读复核：`fixture-v1 → fixture-v1@0c51102b`，公共视图 79 行、index 6 项、manifest 校验通过。
79 是既有发布湖、56 是本轮全新输入构建，二者未混用；本轮没有重建、清空或“修复”共享湖。

## 3. S01–S16 闭合裁定

| 项 | closed/open | 实跑输出及裁定 |
|---|---|---|
| S01 | closed | R01、X01-correction、X01-original 全列 `diff=[]`；原始/完整图 `n_events=[1,1]`，未来 correction 不改 execution。未 drop hash；只关闭这些指定历史扰动，不外推全依赖正确性 |
| S02 | closed | R02 available_at=2024-04-02 10:25Z，10:21 visible=false；H1 t_dec/order_plan=null，entry_decision/execution/original_entry=false；V 旧 SL 配对子例通过 |
| S03 | open | R03a 子边推迟至 T+1d；R03b 根相册 t_dec 从03-04 08:31:01推至03-05 08:31:01，包含 late-media，修复成立。但 C3 父帖相册晚一天时 A*=T、refs仅 child/e/p/parent，媒体未进入递归闭包，见 §6 |
| S04 | closed | R04 future、missing_available、X04-null 均 price=null/MARK_STALE；无样本量校准返回 no_frozen_T_plaus_with_sample_count；原价格时钟、120s和阶梯锚点测试通过。此裁定不包含真实历史规则治理 |
| S05 | open | R05a 冲突 reply=weak/unresolved；R05b 两条均 canonical；R05c 不再以 same_source 合并两腿。独立双根、唯一/歧义 quote、作者命名空间正反例通过。但 A12 默认决策视图同文不同簇，V02；不能以描述视图同簇关闭完整接口义务 |
| S06 | closed | R06a/c 均 entry=6、stop=5、TP=7；R06b/d 未知入场不猜市价；未知 fraction 保留 null、显式 .25 保留。数值精度另归 S08/V01 |
| S07 | closed | R07 混合主因 UNIT_SCALE_CONFLICT/fatal；四类继承拒因 execution=false，前三类 price_check=false；X07-direction=INTENT_AMBIGUOUS/fatal。关闭指定严重度与继承反例 |
| S08 | open | R08 codes=29、别名身份正确、公共签名正确、EX.stop 为 Decimal；X08-all-clock=[]，版本链/期限/计划引用 basis 非空。新生效 A8 的全链 Decimal 未完成，C3 合法12位输入被 float 改值，V01 |
| S09 | open | R09 active、future_expire=0、future_roots=0、delete_allowed=true。delete/correction supersede、同币 reopen 和持久化正例通过；跨作者跨币裸 reply 却被记 reopen 前驱，V04。明确“撤回上一条断言”被 analysis 过滤、replay_required=false，R 的语义出口仍不完整 |
| S10 | open | R10b 改期限、r3:S10-MV 改 available_at 均 input_hash_equal=false；同输入重跑 EP/EE/manifest 字节不变。可是 R10a edit_only=1 仍在：C3 分别归一得到同 svid、不同 event_time，合并仅1行、Q=0，版本证据仍丢失 |
| S11 | open | 缺/空/缺一份 manifest 文件及 stale 两 API 均拒读/过滤，原反例大部分关闭；T01 虚假 counts 仍可消费，不能关闭整个 manifest 元数据核对要求。A9 不要求的跨模块 ack 不列失败 |
| S12 | open | R12 局部恒等式=true，L4/L5/L6 原漏账差集全部0；扩大同口径至 L2 后，248 MV 只有243个 MAP 输入，缺5个 service；review+excluded 未进入累计排除集合，见 §6 |
| S13 | open | 未来实际 sent_context=[] 时 evidence_rejected/unresolved，21候选未发送第21拒收，sent_chars=7153；十倍价格冲突能检出。缺/非法 bbox 后仍 entry_proposal/reasons=[]；同数值的 ETH short 图文录制与 BTC long 文本被判全 match，见 §6 |
| S14 | open | 空/部分标签 insufficient，3抽窗中负窗仍计数；n201/attempt0/空身份拒绝；有正式记录与小批全审出口。但同一批改 audit_plan_version 可从两次 fail 重置为 pass/attempt1，V03；正式抽审还能提交空 sample_ids，仅信任64hex自报 |
| S15 | open | R15=216 sources/248 MV，三频道各1..72，seed=20260911，V/H0/H1/H2/U及2024/25/26成立；规模子项 closed。完整独立验收矩阵仍 open：固化期望接受 edit-only 丢版，T03漏断言拒绝无字段证据计划，A12测试可空集通过且只读描述图；详 §7 |
| S16 | closed | R16 absolute_path/parent_escape/symlink_escape 均 sha256=null、exists=false，禁止hash钩子未触发；实际 sentinel 正反例与 Windows 路径反例通过 |

S 共 closed 6、open 10。七项重纳项目 S03/S05/S09/S12/S13/S14/S15 均已实跑，其规模或正常路径修复不能替代上表反例闭合。

## 4. T01–T04、U01 闭合裁定

| 项 | closed/open | 实跑输出 |
|---|---|---|
| T01 | open | r3 的19个缺键/null/缺文件/错版本案例全部 LookupError；C3 仅将有效 manifest.counts 改成 episodes/events/decision_roots 全0，真实文件与 hash 不动，公共 API 仍 accepted_rows=56。类型检查尚不等于核对计数 |
| T02 | closed | 同 graph_version：unknown fraction=null，known=.250000000000，snapshot_equal=false；未来 fraction 修改 snapshot_equal=true、plan_equal=true |
| T03 | open | 原负/短/NaN/Inf/倒序/越界 bbox 都丢弃且不崩；实图256×80，录制尺寸匹配。r2 §5 的验收还明确要求“无bbox数字都拒收”，本轮重新纳入后仍失败：entry_proposal、bboxes=[]、reasons=[] |
| T04 | closed | D-07 rc=0，mktemp根贯穿构建和读取，删除对象仅为该tmp；无共享湖删除或 tombstone 历史清理 |
| U01 | closed | size=[8]、['bad',8]、[8,NaN] 全部 unreadable/numbers=[]；原 IndexError/ValueError/NaN绕界消失，正常尺寸正例通过 |

T 共 closed 2、open 2；U 共 closed 1、open 0。
T03 不把“丢弃 bbox 后继续接受没有证据的数字”误计为完整拒收成功；U01 只核尺寸结构故仍可 closed。

## 5. 二审 §4 六项关键性质重新裁决

| 性质 | 本轮裁决 | 依据 |
|---|---|---|
| 决策视图全列不变性 | 指定扰动通过 | C2 三组 diff=[]，包括 hash、eligibility、temporal_assumptions；不意味着父相册闭包或经济簇定义已通过 |
| H1 不复活 | 通过 | 10:25首次可证时钟、旧入场无 t_dec，实收旧 SL 配对通过 |
| manifest 屏障 | 未通过完整性质 | 文件集合/hash/status/tombstone/stale门成立；虚假 counts=0 仍消费56行，T01残余 |
| 路径逃逸 | 通过 | 三类根外路径在 hash 前拒绝；Windows分隔符和实际sentinel测试通过 |
| 损耗守恒 | 未通过 | L4–L6原差集归零，L2仍缺5个service；累计排除未覆盖review+excluded对象，不能拿局部加法等式替代全对象账 |
| OCR/裁决离线协议 | 未通过 | 截止/候选/预算已修；无bbox数字仍成计划，图像未证明的品种/方向被录制文本补入 |

## 6. 原项残余的具体证据与关闭条件

**S03：DFS 工具存在，但实际构造的依赖图不完整。**
`graph.py:108` 只为根的 grouped_id 附相册成员；遍历父帖时只附 reply 和其自身 media_hashes。
C3 将 r2 根相册反例向下多接一级：child@T → parent@T-1min；parent 同相册的 image@T+1d。
实际 `plan_dependencies` 返回 `T, [child,e,p,parent]`，没有 image 或 media:image 引用。
期望是依赖相册媒体的父证据将该用途闭包推至 T+1d，或缺必要字段血缘时拒绝；不能截断在父帖时间。
另核 price_check 专用未知依赖：price_check=false、entry_decision/execution=true；用途分离本身不在此另立失败，失败依据仍是必要父媒体丢失。

**S10：版本身份仍漏编辑证据。**
`normalize.py:228–261` 在 live_receive 分支仅将 first_seen_at 作为 evidence_time，svid 不含 last_edit_at。
固定 source/text/media/first_seen_at，只将 edited_unixtime 加1秒，分别归一：`same_id=true, different_event_time=true`。
一起归一：`rows=1, quarantine=0`。两个不同源事件时钟被同一身份合并，且没有冲突出口。
若该证据组合应被视为互相矛盾，应隔离并保留两份观察；不能静默择一后把“一行”写成成功金标。
图层改时钟 hash 已修，与 bronze 阶段在图输入前丢版是两件事。
同输入图层字节复核输出 `changed=[]`，覆盖 EP、EE、manifest，首次 ingested_at 保留；本报告没有否认该正例。

**S12：上层实物到 MAP 的全对象差集与累计出口仍有缺口。**
本轮新湖：L1 input/output=251/248；L2=243/243；L3=248/251；L4=251/153；L5=153/102；L6=153/77。
`dedup.py:114` 在建立 L2 账本前先筛 message_type=message；差集5个对象全部是service。
service 可以合法不参与去重，但必须有显式退出或旁路映射，正是前三轮“过滤前全对象退出账”的同一要求。
L5 有49个、L6有30个 `relation=excluded,status=review` 映射；`lake.py:LayerLedger.excluded/read_cum_excluded` 只数 quarantine/dup_ref。
例如一个无价提议被review且无输出，它并非可评估对象，却不进入累计排除。应按 estimand 与实际映射出口累计，而非仅按两种状态名。
未知权重保持 null、三时钟列存在、L4–L6原差集为0这几项已修，不再重复列旧139/20/5为现状。

**S13/T03：证据删除后，数字仍被 OCR text 重新引入。**
在 `extract.py:apply_ocr` 中，纯图直接 parse_message(recording.text)，只在找得到 bbox 时附框，从不要求已生成字段均有有效图像来源。
合法size、numbers无bbox或全部bbox非法两例，都得到 `entry=60000,stop=59000,kind=entry_proposal,bboxes=[],reasons=[]`。
字段比较仅覆盖 entry上下端、stop、TP level；相同价格但币种/方向相反的录制得到三项match=true，未报 TEXT_IMAGE_CONFLICT。
目视读取 B_pure_image 实图，仅有三行 `64000 / 64500 / 63000`，没有 BTC、做多、入场区间、止损标签。
对应录制却为“BTC 做多 入场 64000-64500 止损 63000”；数字像素确实存在，但其余决定计划身份/字段含义的信息来自录制作者。
这证明“已绘数字”子项成立，不证明纯图完整计划可从图中抽出；不是要求真实 OCR 服务，而是要求合成图与录制自身相符。
关闭条件：缺数字bbox有界拒收；图文品种/方向/数量/期限/比例按字段验证；图中绘制完整计划标签或明确其不可判定，不能凭录制补事实。

**S14：记录表不等于完整审核证据链。**
正式记录确实有锁、record_hash、签字先写/决定后提交、由历史推attempt，以及小批sample_ids对账。
本轮实物正式200样本记录的 `sample_ids=[]`，仅提供任意64hex即可形成pass记录；未绑定可重算的200个冻结样本、完整标签及修复记录。
V03又证明同批两次失败可以更换计划名绕过次数限制。修复后应冻结制品身份及审核计划，并从已保存样本/标签/签字推导结果。
不要求执行任何真实放行；上述反例均为临时合成审核流。

## 7. 固化测试期望值抽验

| 测试位置 | 对前三轮口径的检查 |
|---|---|
| test_review_probes.py:70–121 | R01全列空差集、H1时钟、晚相册、未知行情、6/5/7与显式fraction是独立预期；这些断言没有照搬旧错误输出 |
| test_review_probes.py:140–145 | **不合格**：把 `edit_only_changed_rows=1` 固化为成功期望；本轮另证同ID对应不同event_time且Q=0，不能以测试绿消除S10 |
| test_review_probes.py:147–155 | 缺/null/单文件19例期望LookupError正确；但只数这些例子，不覆盖值合法而counts错误的manifest |
| test_review_probes.py:158–166 | L4–L6差集=0正确，但没有独立检验L2完整MV输入；L3断言先交canonical集合，不能证明六层全链守恒 |
| test_review_probes.py:209–217 | 非法bbox只断言numbers=[]/bbox_count=0，**漏掉同一输出kind仍为entry_proposal且数字还在**；r2 T03要求无bbox数字拒收，现断言偏弱 |
| test_review_probes.py:342–358 | 正式审核第一次pass后又允许第二次pass，仅第三次拒；未测同批改名或修复证据，不足以证明一次正式+修复后一次重验 |
| test_lifecycle.py:432–451 | A9发布/索引/tombstone正例有意义；A12却只读描述EP且允许`eps.height==0`直接通过，漏测真正下游默认视图 |
| fixture像素测试 | 墨迹行列占用断言能排除单色块，但不能证明64000具体字符、币种/方向和字段标签；本轮目视图像发现完整计划标签不存在 |

S15 的216/248、年份、五时间级、编辑链、72复用ID均认可；open针对原“独立golden与性质矩阵”的剩余质量缺口，不要求无理由增加规模。
CE1–CE5现有测试全部执行通过；不可将这些正例覆盖推广为所有字段血缘、纠错、复制簇、损耗出口都已验证。

## 8. 契约核对与新增必修

| 契约项 | 裁定 | 本轮证据 |
|---|---|---|
| A8 Decimal物理列 | 通过列类型检查 | 新湖EX/CP entry.lo/hi、entries、stop、TP level/fraction、size_hint.qty/fraction/notional均Decimal(38,12)；EP计划的价格/比例/qty、dec_stop/dec_tps同样正确 |
| A8 全链精度与q12 | 未通过完整要求 | Decimal('6')与Decimal('6.000000000000')同hash；但canonicalize中转float使合法12位值被改写，V01；全列_sig还把统计float纳入输入hash，不满足纯统计值不得入hash的约束 |
| eligibility六键 | 通过 | 新湖56行、共享只读79行，description/entry_decision/execution/original_entry/price_check/outcome逐键null_count全0；决策outcome恒false |
| cluster_id非空 | 通过可空性检查 | 两个湖instrument_id/t_dec/cluster_id均0 null；非空不代表A12同簇语义正确 |
| A9 index.json | 指定正例通过 | 新湖索引含实际不可变版本与input_hash；已有测试验证发布/tombstone/排序；共享只读6项。不扩入延期ack/备份 |
| 别名语义 | 通过指定解析/换版测试 | 共享fixture-v1解析@0c51102b；新湖别名解析@e5426214；原测试换指针后旧版字节保留，公共行携带实际版本名 |
| A10 basis | 通过指定端点反例 | [100,10000]对mark100：conflict/far_end_only/entries[0].price_hi；Q与L5归因代码及现有测试实跑。此项不抵消S12累计账缺口 |
| A11 codes | 通过 | ReasonCode与Reason、FATAL_REASONS与FATAL为同一对象；冻结29项集合比较通过 |
| A12不折叠且同簇 | 未通过 | 两条不同source保留；描述图同簇、默认决策图分簇，V02。audit_stratum仍空，未确认重发审计分层/报告义务也不能宣称完成 |
| A13/A14作者结果 | 指定状态正例通过 | claimed_outcome三值、close/cancel/expire映射；决策outcome=false，描述状态按已有终态赋值 |
| A15 reconstructed_outcome | G1列及校验通过 | 五键含censor_reason；七值集合精确；unevaluable无原因返回unevaluable_without_censor_reason。G1初次发布全null，不伪造G2结果 |
| execution §3.1/§5 | 结构/缺省边界通过，精度未通过 | order_plan八键和子键对齐；未知TTL/持有上限/fraction留null，显式TP .25保留；V01违反跨层Decimal要求 |
| A16 verifyHistory | 本轮可见新增0条 | modules.data各task无verifyHistory条目；当前verify未见本轮缩小覆盖面的历史证据。不反推没有留痕的旧手改均获授权 |

### V01 必修：Decimal落盘前绕回float，合法12位数字被改写

定位：`src/quant_lab/data/validate.py:97–100`、size_hint转换及末尾decimal_value；关联S08/A8。
输入entry=`Decimal('60000.123456789123')`、stop=`Decimal('59000.123456789123')`。
实际返回类型float，q12分别变成`60000.123456789120`、`59000.123456789120`。
这是已复现的数值改变，不只是静态风格问题；最终Parquet是Decimal不能恢复已丢位数。
修复：价格/比例/数量计算全程Decimal，统计诊断与哈希输入分离；用非整数字段往返测试验证末位不丢。

### V02 必修：默认决策视图把同文重发拆成不同经济簇

定位：`lifecycle.py:363–380`、`api.py:49`；关联S05/A12。
输入同频道同作者两次“BTC 做多 入场 60000 止损 59000”，相隔1分钟，不折叠。
描述cluster均`dg-96b3e0d1274dfce4`；默认API第一行cluster=`d5a6fd3fbc43aaf9939cfd2c42a4b20f`，第二行=`dg-425275063b9e5975`。
原因：每个根按自己的t_dec重算可见成员集合，早根只有自身，晚根两成员，得到不同cluster。
影响：G3读默认API时把复制机会算成两个经济簇，违反A12压低有效n的目的。
修复：用当时可知的稳定复制家族锚点等方案实现同簇，并同时保持旧快照未来不变；不要通过回填未来全成员hash解决。

### V03 必修：新审核历史按可改计划名分桶，重验次数可重置

定位：`audit.py:164–173`；关联S14。
同batch、同n=200/c=3、同producer/reviewer，顺序调用：v1/4个一般错→fail attempt1；v1/4错→fail attempt2；只换v2/0错→pass attempt1。
实际三份正式记录均落盘；更换字符串就绕过同一批最多两次限制。
修复：以不可变被审制品身份约束总尝试史，审核计划冻结、变更走显式授权；重验必须绑定修复证据与新样本，不能由调用方新起名称消除失败历史。

### V04 必修：新reopen谱系绕过品种和作者守卫

定位：`lifecycle.py:418–443`；关联S09及ADR §4强指针守卫。
输入：作者a的BTC long提议→明确全平；之后作者b的ETH short新提议，仅reply第一条，没有“重开原单”或迁移声明。
实际ETH新根仍被赋BTC旧根predecessor_ids及migration_reason=reopen；BTC/ETH、a/b都可从现有行直接区分。
原因：后处理只检查频道、reply到根、旧终态；没有调用链接器语义/作者/分支守卫，给无充分依据的关系定案。
修复：保持新提议独立根；谱系另需可验证的重开/改归属证据，矛盾或不足保留未决。补同币同作者正例及跨币/跨作者裸reply负例。

新增必修共4条。S03父媒体、S10编辑证据、T01计数、S12漏账、S13无bbox均归原验收残余，不重复编V。

## 9. 补充反例复现入口

下列代码只读取仓库，写操作限mktemp研究根；不生成脚本文件，不读取或删除共享data。
在quant-lab中设置PYTHONDONTWRITEBYTECODE=1及QUANT_LAB_DATA_ROOT为新mktemp后，以`.venv-g1/bin/python -`执行。
借用`_pipeline`仅构造合成输入，不借其期望；断言口径和上文实际输出独立给出。

```python
# R4_PROBE_BEGIN
import dataclasses, json, os, pathlib, runpy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
import polars as pl
from quant_lab.data import api, audit, extract, graph, lake, lifecycle, llm, normalize, sources, validate
from quant_lab.data.market_stub import fixture_registry
ns = runpy.run_path('tests/data/test_review_probes.py')
T, B, fixture = ns['T'], ns['BUILD'], ns['FIX']
root = pathlib.Path(os.environ['QUANT_LAB_DATA_ROOT']).resolve()
assert root != pathlib.Path('data').resolve()
pipe = ns['_pipeline']
def emit(tag, value):
    print(tag, json.dumps(value, default=str, ensure_ascii=False))
text = 'BTC 做多 入场 60000 止损 59000'
mv, ex, cp, cb, jd, ep, ev, _ = pipe(root/'base', [(text, None, T, 'a')])
r = ex.row(0, named=True)
r.update(time_grade='V', entry={'lo': Decimal('60000.123456789123'),
         'hi': Decimal('60000.123456789123'), 'kind': 'limit'}, entries=[],
         stop=Decimal('59000.123456789123'), tps=[])
c, _, _ = validate.canonicalize_row(r, registry=fixture_registry())
emit('V01', [str(r['entry']['lo']), lake.q12(c['entry']['lo']), type(c['entry']['lo']).__name__])
rows = [(text, None, T, 'a'), (text, None, T+timedelta(minutes=1), 'a')]
ep = pipe(root/'copies', rows)[5]
with patch.object(api, 'verify_manifest'), patch.object(api, 'resolve_alias', return_value='independent'), patch.object(api, 'stale_episodes', return_value=set()), patch.object(api.pl, 'read_parquet', return_value=ep):
    emit('V02', api.load_episodes('independent').select('root_message_id', 'cluster_id').to_dicts())
args = dict(n_sampled=200, errors_fatal=0, reviewer_ids=('r',), producer_ids=('p',),
            layout=lake.Layout.flat(root/'audit'), batch_id='same-batch')
for i, (version, errors) in enumerate([('v1', 4), ('v1', 4), ('v2', 0)]):
    result = audit.acceptance_decision(**args, errors_general=errors,
             audit_plan_version=version, sample_ids_hash=str(i+1)*64)
    emit('V03', [version, result.result, result.attempt])
rows = [(text, None, T, 'a'), ('BTC 全部平仓', 1, T+timedelta(hours=1), 'a'),
        ('ETH 做空 入场 3400 止损 3500', 1, T+timedelta(hours=2), 'b')]
ep = pipe(root/'reopen', rows)[5]
emit('V04', ep.select('root_message_id', 'instrument_id', 'trader_id', 'predecessor_ids', 'migration_reason').to_dicts())
for numbers in [[{'value':60000}, {'value':59000}],
                [{'value':60000,'bbox':[-1,0,2,2]}, {'value':59000,'bbox':[0]}]]:
    provider = llm.RecordedOcr({'h':{'size':[256,80], 'text':text, 'numbers':numbers}})
    result = extract.apply_ocr(extract.parse_message(''), '', ['h'], provider)
    emit('T03/S13', {'kind':result.kind,'entry':result.entry,'stop':result.stop,
                     'bbox':result.bboxes,'reasons':result.reason_codes})
parent_root = {'plan_id':'p','extract_id':'e','source_version_id':'child','available_at':T,'checks':'{}'}
versions = {
    'child':{'source_id':{'message_id':3},'channel_id':1,'available_at':T,'reply_to_message_id':1},
    'parent':{'source_id':{'message_id':1},'channel_id':1,'available_at':T-timedelta(minutes=1),'grouped_id':10},
    'image':{'source_id':{'message_id':2},'channel_id':1,'available_at':T+timedelta(days=1),'grouped_id':10,'media_hashes':['image-hash']}}
emit('S03', graph.plan_dependencies(parent_root, versions, {}))
m = next(sources.read_all(fixture))
m = dataclasses.replace(m, text=text, message_type='message', media=[], unknown_keys=[],
    time_unit_problem=None, edit_time_problem=None,
    date_unixtime=int((T-timedelta(hours=1)).timestamp()),
    edited_unixtime=int((T-timedelta(minutes=1)).timestamp()), first_seen_at=T)
n = dataclasses.replace(m, edited_unixtime=m.edited_unixtime+1)
layout = lake.Layout.flat(root/'versions')
a = normalize.normalize_messages([m], layout, ingested_at=B)[0]
b = normalize.normalize_messages([n], layout, ingested_at=B)[0]
both, q, _, _ = normalize.normalize_messages([m,n], layout, ingested_at=B)
emit('S10', {'same_id':a['source_version_id'][0]==b['source_version_id'][0],
    'different_event_time':a['event_time'][0]!=b['event_time'][0],'rows':both.height,'quarantine':len(q)})
layout = lake.Layout.from_root(root/'built')
built = api.build(fixture, layout, graph_version='fixture-v1', alias=True, ingested_at=B,
    llm_fixture=fixture.parent/'llm_recorded/extract_v1.json',
    ocr_fixture=fixture.parent/'llm_recorded/ocr_v1.json')
batch = built['normalize']['batch_id']
mv = pl.read_parquet(layout.message_version)
mapping = pl.read_parquet(layout.mapping(batch,2))
emit('S12', {'mv':mv.height,'mapped':mapping['input_ref'].n_unique(),
    'missing':len(set(mv['source_version_id'])-set(mapping['input_ref']))})
good = graph.read_manifest(layout, graph.resolve_alias(layout,'fixture-v1'))
fake = {**good,'counts':{'episodes':0,'events':0,'decision_roots':0}}
with patch.object(api,'_layout',return_value=layout), patch.object(graph,'read_manifest',return_value=fake):
    emit('T01', {'accepted_rows':api.load_episodes('fixture-v1').height})
# R4_PROBE_END
```

收尾已从本报告标记区提取源码，在另一全新mktemp根原样执行，rc=0；V01–V04及S03/S10/S12/T01/T03输出与上文一致，源码/测试树摘要仍与开审相同。

期望修复后：V01末位保持123；V02两行同簇且旧快照不变；V03第三次不得pass；V04新ETH根不得因裸reply自动继承BTC前驱。
T03/S13缺字段图像证据不可成可用计划；S03父相册完整闭包或明确拒绝；S10不同编辑证据不得静默折叠；S12全对象差集为0；T01虚假计数拒读。

## 10. 应改、可选与凭据边界

应改：D-01改为可靠collection判读；D-03用唯一临时目录和新增输入摘要；D-09对两个OC值分别数值断言，单个OR grep不能证明两项都命中。
应改：A12未确认重发的audit_stratum、抽审折叠率和D-11独立诊断需要按契约落实；未审核时明确not_run，不填确认率。
应改：去掉无效的“空集或通过”验收，把Decimal测试从整数字面量扩展至12位有效末位，把所有非法OCR数字的最终用途一并断言。
可选：近似复制DSU的边级隔离与性能优化另做，保持未校准近似不得影响研究簇的约束；可加机器可读review摘要，但不能替代本报告反例。

凭据边界：源码import检查与实跑未发现services导入、生产session/数据库调用；本轮没有联网、没有安装依赖、没有调用真实provider。
默认LLM为NoNetworkClient，非录制LLM经gate拒绝；默认OCR为NoOcr/not_run，本轮仅RecordedOcr。该默认边界通过，不把任意未来provider集成视为已获准。
`lake.write_parquet_atomic` 的 unlink仅清理该次写入临时文件；本轮所有构建、审核记录、sentinel均在临时根。
D-07唯一rm目标是自己mktemp所得目录；没有任何针对quant-lab/data、G2/G3分区、旧图、Q或撤权历史的删除。
共享湖仅进行manifest、index、API的只读取证。源码/测试树摘要稳定；没有改动他方文件来使探针通过。

177项测试通过与上述反例同时成立；本轮失败来自合成链路的已复现错误和原验收残余，不来自真实能力gated。
闭合数：S 6/16，T 2/4，U 1/1；新增必修4条（V01–V04）。
四审终裁：fail
