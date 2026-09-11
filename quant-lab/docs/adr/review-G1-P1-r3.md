# quant-lab G1 P1 第三轮独立复审（r3）

审查日期：2026-09-11（Asia/Manila）。审查者：GPT-6 Codex 主会话；独立执行，未委派 Grok 或其他模型。
范围：用户指定的 P1 合成链路；真实 PoC、真实 LLM、真实批次放行继续 gated，均不计入终裁。
唯一持久化仓库写入：本报告。未修改实现、测试、契约、看板；未删除 quant-lab/data 下任何内容。
测试与看板 verify 的临时产物按用户授权执行；字节码和 pytest 缓存写入均禁用。

**裁定汇总：T01–T04 closed 2/4（T02、T04）；P1 残余 S closed 3/9（S01、S07、S08）；新增必修 1 条（U01）。**
S02、S16 为二审已经 closed 的项目，本轮复核仍通过，不重复计入“残余 S”分母。
残余 S 分母为 S01/S04/S06/S07/S08/S10/S11/S13/S14；其余 S03/S05/S09/S12/S15 的残余整体延期。
S13、S14 同时具有 P1 具体错误和 P2 设计子项；只对其 P1 部分计数，绝不把设计延期视为未通过。
本表 closed/open 是二值验收；open 项中已经修好的子例仍逐一列明。
T/S/U 表存在引用关系，数字不相加充当独立缺陷总数；U01 是 T03 修补引入的新尺寸边界错误。

## 1. 基线、必读材料与并发变化

已读根 AGENTS.md、/Users/balen/.codex/GROK.md、首轮报告（含收尾补充）、二审报告（含 §4/§9 全部探针）、modules.data 最后闭合记录与 P2 声明。
已检查 data 全部 17 模块、tests/data 测试与合成/录制夹具、生成器；读取 research-schema §9 全部裁定，包含审查中新增的 §9.10.10 与 §9.10.11（A15）。
用户称 research-schema v1.1；实际文件头仍写“v1 已冻结”。按内容和 hash 定位，不把版本标题当作额外阻断。
本任务按用户指定目录与探针全量复审，不采用提交差异双代理工作流。

树摘要算法与前两轮一致：排除 __pycache__，按仓库相对路径排序，对每行“路径:文件 SHA256\n”拼接后再次 SHA256；路径前缀包含 quant-lab/。
HEAD 首次读取：fceb16634f2a9f01f36d0a72ff638fe95126f4ed；审查对象是工作树，不以 HEAD 替代未提交文件证据。

| 基线 | 源码（17 文件） | 测试（40 文件） |
|---|---|---|
| B0 首次 | a5432322c1dac8cc64dda17b0e1c08eef1c8adb6f7ad7233fd49ee5d13a0b7e8 | c3100af3f6770369b046d3de640cf443afea556512de12ff918a85e4808e7de9 |
| B1 中间复核 | f5642789c0cc714a2e28ee203483aee2997f83ac1af30869334f409aa57e5574 | 699dc4cc7adb7fba206d8ec85d5f0ba31cce7cc3f85c18952711eefa3992c7aa |
| B2 稳定复核 | 274f996a06b65c5d1f0788eebf9b0e86eacc4be1e38f7f8a5b3fa3bbbdf37027 | 634433abe95f1df8bdd4e886a03d3671b2160c331b1d8b3910667f0787bd6719 |

| B3 收尾最终验收 | 6e467b938ac95b2995650078883c4da1252da1fff1e8b5d80206a5a8796254b2 | 9646faa1ecabe8076c55a9f8fc534b4c3f463ceaddfcedfdfd69aa136a937346 |

B2 的全套测试/全部 verify 前后摘要相同；其后原探针与补充探针前后摘要也相同。
报告初稿完成后他方继续修改 A15 outcome schema 并升 lifecycle v0.3，故再固定 B3：全套 pytest、原探针、补充探针和 D-07 均已重新执行，前后摘要相同；本报告最终结论取 B3。
**审查截止为 B3 上述前后摘要一致的执行区间。** 本报告落盘后再次检测到 audit/extract/graph/lifecycle/linker/llm/market_stub 被他方修改，属于后续修补，未纳入本轮验收。下文 closed/open 与终裁均限定 B3，不能据此宣称后续工作树仍有完全相同的缺陷或已经通过；后续修补须以新摘要与实跑证据另行验收。本轮不追随报告发布后的无限并发改写。
首轮报告 SHA256：d1dd9f92671140448dcd4cee63a85ff927e50ec9f699790fd254967e571da60b。
二审报告 SHA256：2d092efc2387dce4b90512d74f9b28c7123fcbe44be798ac37dcd461a5785cc0。
research-schema：B0=51425a60dc315f2adedc9e95752af1c09950d30fcb86e65c574c80d33a317a41；B2=2d01a10741ffd065626ab04d95e0acb96f7f0d8981da18900ffdd56865e210d9。
taskList.json：B0=946ac41b9ad9892c6e12313a42a7dbf21cb3e1e61ed87f9bd70a3a6e389b796d；B2=7b7cbf46fd587a75c9884bb376e8eb53ac2b84c64bb0856e36906d6893194f8a。
期间他方增加 Decimal、版本索引、六键非空、A10 分支归因等实现及测试，并移动 fixture-v1 别名；本会话没有执行共享湖发布。
B0 默认别名为 fixture-v1@f5191444；B2 为 fixture-v1@c469d020；B3 为 fixture-v1@2e130b30。附录对当前解析后的版本检查 stale，避免把旧字面别名当作版本身份。

B0 verify 前后共享 data 变化仅落在 lockbox（账本/锁/事件文件），不据此宣称整个 data 无变化。
B2 verify 前后，排除 lockbox 的 147 个 data 文件路径/内容完全相同，包含 Telegram 湖、quarantine 和 market。
该集合摘要（路径从 data/ 起）为 26ade3d7b0b4006fbe7ff5e995b4bf1e2fea844631d82c132ffa0950c81bb4ba。
B3 research-schema SHA256=1f32935a3c1a795d897914a6b3a78107f20a78334db22e318f46c37c111c7a0f；taskList SHA256=21ddc1210cc3a26dda8358d914e8f1188e2e36698c6f13ad86124408521e3a83。
B3 notes 11:18 自报曾同名改写 c469d020，随后 tombstone 并发布2e130b30；本轮只确认当前别名/API状态，没有独立重现其历史写入过程，不拿自报当新增事故实证。
没有将并发 lockbox 写入归因于 G1，也没有读改其业务内容来修复本次验收。

## 2. 命令与实际验证

统一 cwd=/Users/balen/projects/trader-bot/quant-lab。
环境：PYTHONDONTWRITEBYTECODE=1，PYTEST_ADDOPTS='-p no:cacheprovider'；每条 shell verify 设置 set -o pipefail。
C1：.venv-g1/bin/python -m pytest tests/data -q。
B0：124 passed in 2.14s，rc=0。
B1：127 passed / 1 failed，rc=1；test_a9_index_and_a12_cluster_sharing 以 idx[0] 假设旧版位置，命中 published 新版。
B2：128 passed in 2.63s，rc=0；他方已将该断言改为按 graph_version 查询。中间失败不冒充当前缺陷，不计新增必修。
B3：129 passed in 5.27s，rc=0；D-07 再跑30 passed in 2.31s、shape=(23,4)、rc=0。其余verify保留B2原次证据，不冒称在B3全部再跑。

| 看板 verify | B2 退出码 | 输出/限定 |
|---|---:|---|
| D-01 | 1 | grep 'collected [1-9]' 无匹配；全套收集/执行成功，不是 import 失败证据 |
| D-02 | 0 | ADR 行数和 invalid_transition/tombstone 关键词门通过 |
| D-03 | 0 | 18 passed；193 MV；added=0；Q=3。固定 /tmp/ql-norm 复用，不冒称全新构建 |
| D-04 | 0 | 11 passed |
| D-05 | 0 | 18 passed；bench 使用 ../eval/v3_trader_signal_bench |
| D-06 | 0 | 9 passed |
| D-07 | 0 | 临时根构建；29 passed；公共视图 shape=(23,4)，三列非空断言通过 |
| D-08 | 1 | NotImplementedError: harvest 待实现；真实 PoC gated，不计失败 |
| D-09 | 0 | 8 passed；0.5% 的 200/3=98.1319%、400/0=13.4658% |
| D-10 | 1 | 写本报告前读取r2；写报告后实跑输出 reading docs/adr/review-G1-P1-r3.md、rc=1，正确反映终裁 |
| D-11 | 0 | layer=5 非空；仅合成损耗，不代表真实 1b |

C2：直接从二审 §9 的 R2_PROBE_BEGIN/END 提取代码，在进程内按顶层 AST 语句执行。
唯一执行包装是捕获单条异常并继续，避免修复后的 LookupError 使后续探针未运行；不修改探针业务输入和预期。
原 X11-public-empty-files 现在抛 LookupError，记录为 PROBE_CAUGHT 行 200，即该子例 closed。
原 X11-stale 使用 old_graph_version='fixture-v1'，别名引入后不再命中；C3 以 resolve_alias 得到实际版本后复跑，两 API 均 0 行。
C2 对 API 投影使用内存替身；manifest 正反例单独走真实 verify_manifest；lifecycle.run 的全部写入口在 dry_run 中被拦截。
C3：附录补充代码；所有修改仅在内存，包含真实发送 prompt 的录制、schema 反例和 MV 输入变更。
C2/C3 最终退出码均 0，表示探针完成，绝不等于业务断言全部通过。
完整最终输出见 §9；补充可复现代码见 §10。

## 3. T01–T04 闭合裁定

| 项目 | 裁定 | 已闭合证据 | 仍开原因 |
|---|---|---|---|
| T01 manifest | open | R11 缺/空/无 files 均 LookupError；C3 只 EP、只 EE、错版本、逐个缺九个必要键均 LookupError | 只检查键存在，input_hash/rule_versions/assumptions/counts/graph_kind/built_at 分别置 null 后仍返回 23 行；二审要求的必要元数据 schema 校验未闭合 |
| T02 TP fraction | closed | R06e=0.250000000000；同一 graph_version 下未知 null 与已知 .25 的 snapshot 不同；未来 .75 改动不改变历史 plan/hash | 没有以不同 graph_version 的 hash 天生不同充当 fraction 入 hash 证据 |
| T03 OCR bbox | open | 原负 bbox、短 bbox 已丢弃且不崩；补测 NaN/Infinity/逆序/8×8 外坐标均丢弃；三录制实际/声明尺寸均 8×8，九条数值 bbox 有效 | 新增 size 校验未验证 size 自身：短数组 IndexError、非数字 ValueError、NaN 尺寸绕过边界；见新增 U01 |
| T04 verify 破坏共享湖 | closed | 命令前缀 tmp=$(mktemp -d)；构建和公共 API 均 QUANT_LAB_DATA_ROOT=$tmp；末尾只 rm -rf "$tmp"；B2 rc=0，非 lockbox 共享文件全量摘要不变 | 这里关闭的是 verify 删除共享湖的问题，不宣称 S10 的同名发布不可变性也已通过 |

T01 位置：src/quant_lab/data/graph.py:164，特别是 169 行只判断 k not in doc。
必要字段为 null 不等于有效输入哈希、规则集合或计数。此项对应二审 T01 的“固定必需文件集合及 schema，核对输入 hash/规则/假设/计数”原要求，不是增加 G0 全量回执。
发布端缺文件拒绝与普通 hash/tombstone 门已有测试；缺元数据值/类型时也应统一 LookupError。
T03 的真实绘字、缺 bbox 数字仍能从 OCR text 成计划、字段级图文冲突，按用户声明延期；没有用这些 P2 内容判 T03 open。
T04 未额外运行共享湖故障注入或真实发布；失败构建的所有写路径仍指向临时根，这一点由命令与 Layout 实现核实。

## 4. 残余 S 与指定探针逐条裁定

| S | P1 裁定 | 实跑输出与结论 |
|---|---|---|
| S01 | closed | R01 diff=[]；X01-correction diff=[]，execution 保持 true；X01-original n_events=[1,1]、diff=[]。所有公开列比较，未 drop hash |
| S02 | closed（维持，不计残余） | R02 首次快照10:25，10:21 visible=false；H1 的 t_dec/order_plan=null，entry_decision/execution/original_entry=false |
| S03 | P2，不计 | R03a 中间父帖推迟子边；R03b 晚相册成员仍不推迟 t_dec，DFS/sequence 实证确未做 |
| S04 | open | R04.future 与 X04-null 均 MARK_STALE；无样本量校准不可用；但 R04.missing_available 仍返回 123.0、reason=null |
| S05 | P2，不计 | R05a 冲突回复 weak/unresolved；R05c 同消息两腿仍 r2→r1 same_source，quote 未实现。延期，不要求本轮修多腿 |
| S06 | open | R06a 6/5/7、R06b/d 无入场不猜市价已修；R06e fraction 已修；R06c FIL 6/5/7 + BTC阻力6.5万仍变60000/50000/70000 |
| S07 | closed | X07 四种继承拒因 execution 全 false；RAW_HASH_MISMATCH/SCHEMA_DRIFT/KEY_DUPLICATE_OR_ORDER price_check=false；方向冲突 severity=fatal |
| S08 | closed（本轮计入部分） | R08 enum/API/basis 正确；X08-all-clock=[]，包括 expire 的源时钟；五项除外事项另列 |
| S09 | P2，不计 | R09 提前截止 active、未来 expire=0；delete_parse=delete_notice，delete_allowed=false；future_roots=18，不能泛称全部终点后对象均过滤 |
| S10 | open | R10b 改期限 input_hash_equal=false 已修；C3 只改 MV 根 available_at 一天，input_hash_equal=true，t_dec 与 snapshot 都改变 |
| S11 | open | X11-public-empty-files 拒读；X11-stale-resolved episode_rows=0/event_rows=0；但关联 T01 必要元数据值可绕过 |
| S12 | P2，不计 | R12 局部恒等式=true；上层实物到 MAP 未记账数 L4=139、L5=20、L6=5，未假称全链守恒 |
| S13 | open（离线裁决部分） | 原 R13-adj=unresolved/no_recording 不足以证明闭合；C3 真实 sent_context=[] 时，引用被过滤的未来证据仍 link/attempts=1；21候选选未发第21已正确 unresolved |
| S14 | open（标签完整性部分） | kind=null→insufficient；混合已标+null→pause；n=201/attempt=0 分别 insufficient；空hash/复核人 not_run；但仅 kind 有值、其余四键全缺时仍 verdict=pass |
| S15 | P2，不计 | R15=192 sources/193 MV，H0/H1/V/U，无H2；生成器seed=42，非ADR216/246/20260911 |
| S16 | closed（维持，不计残余） | absolute_path/parent_escape/symlink_escape；sha256=null、exists=false；禁止hash钩子未触发，实文件 sentinel 测试通过 |

S04 定位 market_stub.py:45：仅在存在 available_at 列时过滤；缺整列的输入绕过，不能把 null 子例修复推广成未知可知时钟全部拒用。
S06 定位 extract.py:253：价格语境锚仍全消息传播，尚未绑定同一品种/价格表达。这是原有改价错误，不是 S05 多腿分支扩容。
S10 定位 lifecycle.py:425：input_hash_of 读取 CP/CB/JD/DG，却不读 MV；build_graph 又使用 MV 的 available_at/sequence/media/author 等。
C3 的 S10-MV：固定 CP、候选、裁决、图版本和 BUILD，仅 MV.available_at 从03-04推到03-05，t_dec 同步推一天、snapshot 变，input_hash 不变。
这不要求实现 P2 DFS；只要求已经真实参与当前运算的 MV 字段进入版本身份。
S13 定位 llm.py:412 与420：发送使用过滤后的 cur，校验却重建自原 req；截断候选已修，截止上下文没有同步。
原录制按未过滤 prompt 生成，所以 no_recording 是夹具未命中；改为按实际过滤 prompt 录制后，旧漏洞仍可达。
X13-budget 的 100736 字符仍只受条数截断，不执行 tokens=8000 限制；保留二审预算残余。字符数不直接冒充实测 token 数。
S14 定位 audit.py:205/234：只处理“两边有键”或“一边有键”，两边都缺某必需字段时直接跳过。
仅 kind='entry_proposal' 的两份标签对五个默认关键字段，只评 kind 就给整份 verdict=pass；这是二审“缺完整标签”残余。
它可由现有函数的输入检查修正，不涉及本轮延期的签字历史表或正式验收记录持久化。

## 5. 二审 §4 六项关键性质重新裁决

| 性质 | 本轮裁决 | 证据及边界 |
|---|---|---|
| 决策视图全列不变性 | 通过（本轮指定扰动） | 固定图版本/根/BUILD，观察终点、未来correction、根单独与完整图三例 diff=[]；不推断 P2 全依赖闭包已完成 |
| H1 不复活 | 通过 | R02 与 CE1/V 旧 SL 测试通过；最终编辑版本不能倒填原入场 |
| manifest 屏障 | 未通过 | 缺文件/空清单已修；必要元数据 null 仍可消费，且 S10 MV 漏 hash 破坏不可变前提；未声称完成跨文件并发代际验收 |
| 路径逃逸 | 通过 | 三路径在 hash 前拒绝；正常根内媒体与实际临时 sentinel 回归通过 |
| 损耗守恒 | 全对象性质未成立，P2 豁免，不计本轮失败 | 局部行恒等式成立；全对象差集仍139/20/5，数字局部守恒不能代替完整退出账 |
| OCR/裁决离线协议 | 未通过（P1 部分） | 21候选已修；未来证据仍可被未发送上下文校验接受；新增 size 可崩批。真实绘字/字段级 OCR 单列 P2 |

## 6. 新回归与新增必修

### U01（必修）：T03 新增 size 分支未校验维度结构，重新引入崩批与越界接受

位置：src/quant_lab/data/llm.py:294–320；RecordedOcr.read → valid_ocr_number。
输入：value=100、bbox=[0,0,1,999]，分别令 size=[8]、['bad',8]、[8,NaN]。
实际：IndexError / ValueError / status=ok 且保留 y1=999，见 §9 原始输出。
影响：一个损坏的离线 OCR 尺寸记录可中断 provider/抽取调用；非有限尺寸使越界比较失效。
该分支属于 T03 新增的尺寸约束；原短 bbox 修复正确，但“非法数据不中断”的边界仍未闭合。
改法：先验证 size 为两项有限正数，再验证 bbox；失败返回有界拒答/隔离信息，不裸索引或信任 NaN。异常不得传出中断整批。
验收：三例都明确拒收且不抛异常；合法8×8框仍可用；同时覆盖空数组、错误类型、零/负/Infinity尺寸。
这是纯离线结构校验，不要求真实 OCR 服务、图片文字绘制或字段级真值系统。
新增必修合计1条；T01元数据、S10漏MV、S13截止、S14缺标签均归原条目的具体残余，不重复新编号。
B1 的 idx[0] 测试失败已在 B2 消失，只保留历史验证记录，不列 U02。

## 7. 七项 P2 声明与五项除外事项：仅核实现状

| P2 项 | 现状核实 | 对 notes 描述的修正 |
|---|---|---|
| S03 全依赖 DFS/sequence | 属实，晚相册探针未进闭包，sequence仍message_id | “现有夹具没覆盖”属实；不能推广为合成因果性质已经成立，r2/r3合成反例本身就能击穿 |
| S05 多腿/quote/作者命名空间 | 属实，first_of_source 吞分支，quote缺失 | 多档入场与多个独立提议不是同一概念，Titan两档不自动等于两条episode；此处只确认能力未实现 |
| S09 delete_notice/J/R | 属实，delete_notice未进CP，J/R只有标记，未真实新分支/重放 | 普通后续事件和到期过滤已有；R09仍有18个终点后根，不接受“所有对象均过滤”的宽泛表述 |
| S12 过滤前全对象退出账 | 属实，139/20/5差集仍在 | “仅影响可读性”过轻；它使排除量/分母无法完整核算，但按用户范围不阻断P1 |
| S13 真实绘字与字段级OCR | 属实，三图均8×8单色，录制坐标现在落图内，仍不能回指真实文字 | 缺bbox与十倍图文冲突 reasons=[]按P2记录；“裁决只按发送集合”不全属实，截止部分仍为P1错误 |
| S14 签字历史/正式验收记录 | 属实，无完整持久化审核器，真实放行gated | 固定n/attempt/空身份守卫确已补；不要把这些局部守卫宣传为完整标签检查通过 |
| S15 ADR规模夹具 | 属实，192/193、seed42、无H2，与目标216/246不符 | 规格扩量延期；已有五反例测试不能自动证明未覆盖的截止/元数据边界 |

七项全部按延期处理；上述真实性修正不新增 P1 设计任务，也不据它们单独判 fail。
五项除外事项虽在当前 §9.10 已有 G0 裁定，仍按用户本轮范围不计失败：
- decimal/嵌套类型：B2 新建 gold 的 order_plan 等已用 Decimal(38,12)，六键非空，决策 outcome=false；旧湖 EX.stop 仍 Float64。没有以常量/单个测试冒称“全链每一步无浮点”。
- CR-07：B2 新增 index/consumable_versions，发布/tombstone 更新；全量回执与备份恢复不作本轮要求。G1 自有 manifest 的 T01 不属于延期。
- A04：B2 已加端点 basis、field_path 及损耗分支，新增测试通过；政策及完整跨层归因由 G0 后续验收。
- codes：纯别名，29项；ReasonCode/FATAL_REASONS 身份测试通过。
- 同文不折叠：R05b 双方 canonical；B2 增同簇测试。是否覆盖全部 as-of 场景不作为本轮重新设门理由。

## 8. 应改与可选

应改（不替代前述必修）：
- A01：D-01 的收集 grep 仍坏；D-03 固定/tmp复用仍无法证明本次新增；D-09 应分别断言两个 OC 值，单个 OR grep 只命中一个也能绿。
- D-10 现已按数值选轮次并正向检查，旧字典序问题不再保留为当前缺陷；报告完成后只读复核它确实读取 r3。
- A03：lifecycle 已检查 usable_for_cluster，但 dedup 的 DSU 仍先合并 near 边，成员级标记不等于复制边级隔离。保留应改，不升级为新增必修。
- A05/A06：保留完整异常数值校验、原文与规范文坐标/hash语义、作者/顺序证据检查；不把字段存在当作证据完备。
- T02 回归测试原有不同 graph_version 比hash的混淆，本报告已用同版本反例补证；测试本身宜改为固定版本。
- 更新闭合记录时应写“具体子例通过/仍有哪些反例”，避免“全链闭合”与后续P2说明相互矛盾。
- P2工单保留当前139/20/5差集、相册推迟、真实图与审核历史的验收输入，便于后续接续。

可选：
- 保留 O01：扩量前优化近似去重 O(n²)，当前小集不据性能设门。
- 保留 O02：拆密集表达式、未用变量与重复字典拼装，降低漏字段风险。
- 保留 O03：产出机器可读的树摘要/不可变版本/命令退出码/排除范围；锁定交付快照后再复审，减少并发重跑。

## 9. B3 最终探针原始输出

以下输出原样记录；JSON 的 Decimal 由 default=str 表达，不表示真实存储为 String。
原 X11-stale 的字面别名结果保留用于说明适配必要性，最终依据 X11-stale-resolved。
所有 open 的业务反例都在 B3 重跑成立；表中其他 B2 特定历史证据已显式标注。

```text
BEFORE {'src/quant_lab/data': '6e467b938ac95b2995650078883c4da1252da1fff1e8b5d80206a5a8796254b2', 'tests/data': '9646faa1ecabe8076c55a9f8fc534b4c3f463ceaddfcedfdfd69aa136a937346'}
PYTEST 0 ........................................................................ [ 55%]
.........................................................                [100%]
129 passed in 5.27s

R2_RC 0
R01 {"rows": [1, 1], "diff": []}
R02 {"available_at": "2024-04-02 10:25:00+00:00", "at_1021_visible": false, "h1": [{"t_dec": null, "order_plan": null, "eligibility_by_estimand": {"description": true, "entry_decision": false, "execution": false, "original_entry": false, "price_check": true, "outcome": false}}, {"t_dec": null, "order_plan": null, "eligibility_by_estimand": {"description": true, "entry_decision": false, "execution": false, "original_entry": false, "price_check": true, "outcome": false}}]}
R03a [{"edge_available_at": "2024-06-11 09:01:00+00:00", "dependency_refs": ["sv-r", "sv-p", "sv-r"]}]
R03b {"before": "2024-03-04 08:31:01+00:00", "late_media": "2024-03-05 08:31:00+00:00", "after": "2024-03-04 08:31:01+00:00", "deps": ["51b03b15bf2e7b4abb8b5242d7db24915dd1f2866b01cf16efb901b0717da61d", "c68d3c5f940588dc2f26e9d4fda7d3ba232e3e1cb1875b492b07e8efd1dba6ee", "38296d6ab0418c607a4f922cc68047aa7578eb7d19852d9a7e0c1f1ff335ccbb", "registry:"]}
R04 {"future": [null, "MARK_STALE", null, null], "missing_available": ["123.0", null, "2024-06-10 09:00:00+00:00", 60.0], "cal_no_samples": [null, "no_frozen_T_plaus_with_sample_count"]}
R05a {"edges": [{"strength": "weak", "selected": false, "reason_codes": ["ENTRY_LINK_AMBIGUOUS"]}], "actions": ["unresolved"]}
R05b [{"message_id": 103, "is_canonical": true, "dup_kind": "none"}, {"message_id": 999, "is_canonical": true, "dup_kind": "repost_same_channel"}]
R05c [{"from_plan_id": "r2", "to_plan_id": "r1", "method": "same_source"}, {"from_plan_id": "s", "to_plan_id": "r1", "method": "reply"}]
R06a {"entry": {"lo": 6.0, "hi": 6.0, "kind": "limit"}, "stop": 5.0, "tps": [{"level": 7.0, "fraction": null, "kind": "price"}], "entry_mode": "price", "notes": []}
R06b {"entry": null, "stop": 90.0, "tps": [], "entry_mode": "unknown", "notes": []}
R06c {"entry": {"lo": 60000.0, "hi": 60000.0, "kind": "limit"}, "stop": 50000.0, "tps": [{"level": 70000.0, "fraction": null, "kind": "price"}], "entry_mode": "price", "notes": ["unit_propagated:6.0->60000.0", "unit_propagated:6.0->60000.0", "unit_propagated:5.0->50000.0", "unit_propagated:7.0->70000.0"]}
R06d null
R06e [{"level": "70000.000000000000", "fraction": "0.250000000000"}]
R07 {"mixed": ["UNIT_SCALE_CONFLICT", "fatal"], "missing_media": [{"reason_codes": ["MEDIA_MISSING"], "eligibility_by_estimand": "{\"description\": true, \"execution\": false, \"original_entry\": true, \"price_check\": true}"}]}
R08 {"asserted_event_time": true, "evidence": ["live_receive", "edit_date", "export_snapshot"], "alias": true, "codes": 29, "events_signature": "(graph_version: 'str') -> 'pl.DataFrame'", "quarantine_signature": "(flow: 'str', *, status: 'str | None' = None) -> 'pl.DataFrame'", "source_clock_mismatches": 0, "stop_type": "Float64", "elig_type": "Struct({'description': Boolean, 'entry_decision': Boolean, 'execution': Boolean, 'original_entry': Boolean, 'price_check': Boolean, 'outcome': Boolean})"}
R09 {"deadline_root": [{"author_plan_state": "active", "censor_at": "2024-05-20 10:00:00+00:00"}], "future_expire": 0, "delete_parse": "delete_notice", "delete_allowed": false, "future_roots": 18}
R10a {"both_clocks_changed_rows": 2, "edit_only_changed_rows": 1}
R10b {"input_hash_equal": false, "plan_equal": false}
R11-missing "LookupError"
R11-empty_files "LookupError"
R11-no_files "LookupError"
R12a {"conserved_rows": true, "totals": [{"layer": 1, "input_n": 193, "output_n": 193, "cum_excluded_ids": 3}, {"layer": 2, "input_n": 188, "output_n": 188, "cum_excluded_ids": 3}, {"layer": 3, "input_n": 188, "output_n": 196, "cum_excluded_ids": 3}, {"layer": 4, "input_n": 57, "output_n": 57, "cum_excluded_ids": 5}, {"layer": 5, "input_n": 37, "output_n": 35, "cum_excluded_ids": 7}, {"layer": 6, "input_n": 52, "output_n": 36, "cum_excluded_ids": 7}]}
R12-L4 {"previous_objects": 196, "mapped_inputs": 57, "unaccounted": 139}
R12-L5 {"previous_objects": 57, "mapped_inputs": 37, "unaccounted": 20}
R12-L6 {"previous_objects": 57, "mapped_inputs": 52, "unaccounted": 5}
R13-ocr-[0, 0, 1, 1] {"kind": "entry_proposal", "reasons": [], "bboxes": [{"field": "stop", "media_hash": "h", "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}, {"field": "tps", "media_hash": "h", "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}, {"field": "entry.lo", "media_hash": "h", "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}]}
R13-ocr-[-1, 0, 2, 1] {"kind": "entry_proposal", "reasons": [], "bboxes": [{"field": "stop", "media_hash": "h", "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}, {"field": "tps", "media_hash": "h", "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}]}
R13-ocr-[0] {"kind": "entry_proposal", "reasons": [], "bboxes": [{"field": "stop", "media_hash": "h", "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}, {"field": "tps", "media_hash": "h", "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}]}
R13-adj {"action": "unresolved", "selected_candidate_id": null, "evidence_message_ids": [], "rejected_candidate_ids": ["c"], "reason_codes": ["ENTRY_LINK_AMBIGUOUS"], "provider": "recorded", "attempts": 0, "extended": false, "note": "no_recording", "response_hash": ""}
R13-numeric []
R14 {"empty": {"status": "insufficient", "n": 1, "missing_pairs": 0, "scored_fields": [], "note": "缺标注/缺配对/无可评字段：不出 pass"}, "partial": {"status": "insufficient", "n": 1, "missing_pairs": 0, "scored_fields": [], "note": "缺标注/缺配对/无可评字段：不出 pass"}, "windows": {"status": "ok", "recall": 1.0, "interval": [1.0, 1.0], "interval_method": "cluster_bootstrap_all_sampled_windows_within_channel", "n_true": 1, "n_windows": 3, "n_windows_with_opportunity": 1}, "unaudited": {"result": "not_run", "n": 201, "errors_general": 0, "errors_fatal": 0, "p_hat": null, "interval": null, "interval_method": "none", "reason": "缺固定抽样身份（sample_ids_hash）：不能出 pass", "attempt": 0}}
R15 {"unique_sources": 192, "mv": 193, "grades": ["H0", "U", "H1", "V"], "years": [null, 2024, 2025]}
R16-/r2-sentinel [{"reason": "absolute_path", "sha256": null, "exists": false}]
R16-../r2-sentinel [{"reason": "parent_escape", "sha256": null, "exists": false}]
R16-photos/r2-link [{"reason": "symlink_escape", "sha256": null, "exists": false}]
C01-cluster {"decision_rows": 23, "nulls": [{"instrument_id": 0, "t_dec": 0, "cluster_id": 0}], "rebuilt_null_clusters": 0}
X01-correction {"diff": [], "before": [{"description": true, "entry_decision": true, "execution": true, "original_entry": true, "price_check": true, "outcome": false}], "after": [{"description": true, "entry_decision": true, "execution": true, "original_entry": true, "price_check": true, "outcome": false}]}
X07-RAW_HASH_MISMATCH {"description": true, "execution": false, "original_entry": true, "price_check": false}
X07-SCHEMA_DRIFT {"description": true, "execution": false, "original_entry": true, "price_check": false}
X07-KEY_DUPLICATE_OR_ORDER {"description": true, "execution": false, "original_entry": true, "price_check": false}
X07-MEDIA_MISSING {"description": true, "execution": false, "original_entry": true, "price_check": true}
X07-direction [["INTENT_AMBIGUOUS", "fatal"]]
X04-null [null, "MARK_STALE", null, null]
X01-original {"n_events": [1, 1], "diff": []}
X11-public-missing "LookupError"
PROBE_CAUGHT 200 LookupError graph_version=fixture-v1@2e130b30 manifest 缺必备字段：视为未发布
X11-stale {"episode_rows": 1, "event_rows": 1}
X13-conflict []
X13-budget {"sent_candidates": 20, "sent_chars": 100736, "selected_in_sent": false, "result": "unresolved"}
X08-basis ["same_source_version_chain", "frozen_deadline_in_proposal"]
X08-all-clock []

SUP_RC 0
T01-empty_files {"error": "LookupError"}
T01-wrong_version {"error": "LookupError"}
T01-missing_graph_version {"error": "LookupError"}
T01-missing_input_hash {"error": "LookupError"}
T01-missing_rule_versions {"error": "LookupError"}
T01-missing_assumptions {"error": "LookupError"}
T01-missing_files {"error": "LookupError"}
T01-missing_counts {"error": "LookupError"}
T01-missing_graph_kind {"error": "LookupError"}
T01-missing_status {"error": "LookupError"}
T01-missing_built_at {"error": "LookupError"}
T01-only_episode__fixture-v1@2e130b30.parquet {"error": "LookupError"}
T01-only_episode_event__fixture-v1@2e130b30.parquet {"error": "LookupError"}
T01-null_input_hash {"accepted_rows": 23}
T01-null_rule_versions {"accepted_rows": 23}
T01-null_assumptions {"accepted_rows": 23}
T01-null_counts {"accepted_rows": 23}
T01-null_graph_kind {"accepted_rows": 23}
T01-null_built_at {"accepted_rows": 23}
S13-filtered-prompt {"sent_context": [], "result": {"action": "link", "selected_candidate_id": "c", "evidence_message_ids": [{"peer_id": 1, "message_id": 1, "source_version_id": "future"}], "rejected_candidate_ids": [], "reason_codes": [], "provider": "recorded", "attempts": 1, "extended": false, "note": "", "response_hash": "d77775786c5494f17c4f1a374c01b8402d6441021b7d7b99614949b9a7794d74"}}
R13-adj-21-exact {"sent_candidates": 20, "result": {"action": "unresolved", "selected_candidate_id": null, "evidence_message_ids": [], "rejected_candidate_ids": ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16", "17", "18", "19", "20"], "reason_codes": ["ENTRY_LINK_AMBIGUOUS"], "provider": "recorded", "attempts": 1, "extended": false, "note": "evidence_rejected:selected_not_in_candidates:'20'", "response_hash": "23972c048a8e37d67895262467b108c86aac82eef9ba135d2f8b6e075ea128fe"}}
R14-partial_null {"status": "insufficient", "n": 1, "missing_pairs": 0, "scored_fields": [], "note": "缺标注/缺配对/无可评字段：不出 pass"}
R14-partial_present {"status": "ok", "n": 1, "missing_pairs": 0, "scored_fields": ["kind"], "field_disagreement": {"instrument_id": null, "side": null, "entry": null, "stop": null, "kind": 0.0}, "key_field_rate_max": 0.0, "whole_episode_rate": 0.0, "unresolved_rate": 0.0, "gates": {"key_field": 0.02, "whole_episode": 0.05, "unresolved": 0.05}, "verdict": "pass", "note": "原始意见分歧率；仲裁后的零分歧不得回填此表"}
R14-mixed_null {"status": "ok", "n": 1, "missing_pairs": 0, "scored_fields": ["instrument_id"], "field_disagreement": {"instrument_id": 0.0, "side": null, "entry": null, "stop": null, "kind": null}, "key_field_rate_max": 0.0, "whole_episode_rate": 0.0, "unresolved_rate": 1.0, "gates": {"key_field": 0.02, "whole_episode": 0.05, "unresolved": 0.05}, "verdict": "pause", "note": "原始意见分歧率；仲裁后的零分歧不得回填此表"}
R14-good {"result": "pass", "n": 200, "errors_general": 0, "errors_fatal": 0, "p_hat": 0.0, "interval": [0.0, 0.018846005918320894], "interval_method": "wilson_95_two_sided", "reason": "一般错 0 ≤ c=3；接受后仍记录非零错误估计，不宣称总体≤1%", "attempt": 1}
R14-n201 {"result": "insufficient", "n": 201, "errors_general": 0, "errors_fatal": 0, "p_hat": null, "interval": null, "interval_method": "none", "reason": "抽样 201 ≠ 冻结 n=200（一次随机抽满后只判一次）", "attempt": 1}
R14-attempt0 {"result": "insufficient", "n": 200, "errors_general": 0, "errors_fatal": 0, "p_hat": null, "interval": null, "interval_method": "none", "reason": "attempt 必须 ≥1（正式验收记录）", "attempt": 0}
R14-hash_empty {"result": "not_run", "n": 200, "errors_general": 0, "errors_fatal": 0, "p_hat": null, "interval": null, "interval_method": "none", "reason": "缺固定抽样身份（sample_ids_hash）：不能出 pass", "attempt": 1}
R14-reviewer_empty {"result": "not_run", "n": 200, "errors_general": 0, "errors_fatal": 0, "p_hat": null, "interval": null, "interval_method": "none", "reason": "缺独立复核人（或复核人=产线作者）：真值门 not_run", "attempt": 1}
T03-valid {"numbers": [{"value": 100, "bbox": [0, 0, 1, 1]}], "kind": "entry_proposal", "reasons": [], "bbox_count": 1}
T03-negative {"numbers": [], "kind": "entry_proposal", "reasons": [], "bbox_count": 0}
T03-short {"numbers": [], "kind": "entry_proposal", "reasons": [], "bbox_count": 0}
T03-nan {"numbers": [], "kind": "entry_proposal", "reasons": [], "bbox_count": 0}
T03-inf {"numbers": [], "kind": "entry_proposal", "reasons": [], "bbox_count": 0}
T03-reverse {"numbers": [], "kind": "entry_proposal", "reasons": [], "bbox_count": 0}
T03-outside {"numbers": [], "kind": "entry_proposal", "reasons": [], "bbox_count": 0}
U01-size-[8] {"error": "IndexError", "message": "list index out of range"}
U01-size-['bad', 8] {"error": "ValueError", "message": "could not convert string to float: 'bad'"}
U01-size-[8, nan] {"status": "ok", "text": "BTC 做多 入场 100", "numbers": [{"value": 100, "bbox": [0, 0, 1, 999]}], "provider": "recorded_ocr", "version": "ocr-fixture-v1"}
T03-fixture [{"hash": "f047ddfca417e2301e12527ae2d6ed2c9ae9529bf1dba3289e46133d765f7360", "actual": [8, 8], "recorded": [8, 8], "numbers": 3, "all_valid": true}, {"hash": "a0bd3e24cc7031f01fce47147738603c2048995a3064bbc843549f095217f599", "actual": [8, 8], "recorded": [8, 8], "numbers": 3, "all_valid": true}, {"hash": "8d1f6b5c4e4057ddf764cf02170261eb8e2171de123a41040f62515ab457353d", "actual": [8, 8], "recorded": [8, 8], "numbers": 3, "all_valid": true}]
S10-MV {"input_hash_equal": true, "t_dec_before": "2024-03-04 08:31:01+00:00", "t_dec_after": "2024-03-05 08:31:01+00:00", "snapshot_equal": false}
T02-same-version {"unknown": [{"level": "70000.000000000000", "fraction": null}], "known": [{"level": "70000.000000000000", "fraction": "0.250000000000"}], "snapshot_equal": false}
T02-future {"snapshot_equal": true, "plan_equal": true}
X11-stale-resolved {"version": "fixture-v1@2e130b30", "episode_rows": 0, "event_rows": 0}
PUBLIC-eligibility [{"description": 0, "entry_decision": 0, "execution": 0, "original_entry": 0, "price_check": 0, "outcome": 0}]

D07 0 ['30 passed in 2.31s', 'shape: (23, 4)']
AFTER {'src/quant_lab/data': '6e467b938ac95b2995650078883c4da1252da1fff1e8b5d80206a5a8796254b2', 'tests/data': '9646faa1ecabe8076c55a9f8fc534b4c3f463ceaddfcedfdfd69aa136a937346'}
contracts/research-schema.md 1f32935a3c1a795d897914a6b3a78107f20a78334db22e318f46c37c111c7a0f
taskList.json 21ddc1210cc3a26dda8358d914e8f1188e2e36698c6f13ad86124408521e3a83

```

## 10. 补充探针源码与复现方式

原探针无需复制：读取 r2 的标记区，在 AST 顶层捕获异常并继续。
不得直接以原脚本中 X11-public-empty-files 抛异常为由跳过后面的 X07/X08/X13。
在 quant-lab 下设置 PYTHONDONTWRITEBYTECODE=1 后，使用 .venv-g1/bin/python 从本报告提取下列标记区执行即可；不生成脚本文件。
示例 Python 入口：s=Path('docs/adr/review-G1-P1-r3.md').read_text(); exec(s.split('\n# R3_PROBE_BEGIN\n',1)[1].split('\n# R3_PROBE_END',1)[0])。
写入边界：仅内存 DataFrame/dict 与 mock；read_parquet/manifest/image均只读；不触发共享湖构建、迁移、tombstone或清理。

```python
# R3_PROBE_BEGIN
import copy,dataclasses,json,pathlib,hashlib,struct
from datetime import UTC,datetime,timedelta
from unittest.mock import patch
import polars as pl
from quant_lab.data import api,audit,graph,lake,lifecycle,llm,extract
T=datetime(2024,6,10,9,1,tzinfo=UTC)
BUILD=datetime(2026,9,11,tzinfo=UTC)
lay=lake.Layout.from_root()
def emit(k,v):
    print(k,json.dumps(v,ensure_ascii=False,default=str))
gv=graph.resolve_alias(lay,'fixture-v1')
good=graph.read_manifest(lay,gv)
# Public API metadata schema, not just presence.
cases={'empty_files':{**good,'files':{}},'wrong_version':{**good,'graph_version':'other'}}
for k in graph.REQUIRED_MANIFEST_KEYS:
    cases['missing_'+k]={a:b for a,b in good.items() if a!=k}
for name in good['files']:
    cases['only_'+name]={**good,'files':{name:good['files'][name]}}
for k in ['input_hash','rule_versions','assumptions','counts','graph_kind','built_at']:
    cases['null_'+k]={**good,k:None}
for tag,doc in cases.items():
    with patch.object(graph,'read_manifest',return_value=doc):
        try:
            result={'accepted_rows':api.load_episodes('fixture-v1').height}
        except Exception as err:
            result={'error':type(err).__name__}
    emit('T01-'+tag,result)
# Current send-time prompt versus validation-time context: record exact filtered prompt.
future={'peer_id':1,'message_id':1,'source_version_id':'future','text':'future','available_at':(T+timedelta(days=1)).isoformat()}
req=llm.AdjudicationRequest('r3','e',[{'candidate_id':'c'}],[future],T.isoformat(),purpose='decision')
cur=dataclasses.replace(req,context=[])
s,u=llm.build_adjudicate_prompt(cur)
payload={'action':'link','selected_candidate_id':'c','evidence_message_ids':[{'peer_id':1,'message_id':1,'source_version_id':'future'}],'rejected_candidate_ids':[]}
client=llm.RecordedClient({llm.record_key(s,u,llm.SCHEMA_NAME_ADJ):{'response':payload}})
emit('S13-filtered-prompt',{'sent_context':json.loads(u)['context'],'result':dataclasses.asdict(llm.adjudicate(req,client=client))})
# 21-candidate actual provider call.
req=dataclasses.replace(req,candidates=[{'candidate_id':str(i)} for i in range(21)],context=[{**future,'available_at':(T-timedelta(seconds=1)).isoformat()}])
s,u=llm.build_adjudicate_prompt(req)
p={**payload,'selected_candidate_id':'20','rejected_candidate_ids':[str(i) for i in range(20)]}
client=llm.RecordedClient({llm.record_key(s,u,llm.SCHEMA_NAME_ADJ):{'response':p}})
emit('R13-adj-21-exact',{'sent_candidates':len(json.loads(u)['candidates']),'result':dataclasses.asdict(llm.adjudicate(req,client=client))})
# Missing versus partial null labels and individually isolated acceptance guards.
for name,labels in [('partial_null',[{'episode_id':'e','kind':None}]),('partial_present',[{'episode_id':'e','kind':'entry_proposal'}]),('mixed_null',[{'episode_id':'e','instrument_id':'BTC','kind':None}])]:
    emit('R14-'+name,audit.disagreement_report(labels,copy.deepcopy(labels)))
args=dict(n_sampled=200,errors_general=0,errors_fatal=0,sample_ids_hash='a'*64,reviewer_ids=('reviewer-a',),attempt=1)
for tag,changes in [('good',{}),('n201',{'n_sampled':201}),('attempt0',{'attempt':0}),('hash_empty',{'sample_ids_hash':''}),('reviewer_empty',{'reviewer_ids':('',)})]:
    emit('R14-'+tag,dataclasses.asdict(audit.acceptance_decision(**(args|changes))))
# T03 coordinate matrix and malformed newly introduced dimensions.
for tag,bbox in [('valid',[0,0,1,1]),('negative',[-1,0,2,1]),('short',[0]),('nan',[0,0,float('nan'),1]),('inf',[0,0,float('inf'),1]),('reverse',[2,0,1,1]),('outside',[0,0,9,1])]:
    rec={'size':[8,8],'text':'BTC 做多 入场 100 止损 90 止盈 110','numbers':[{'value':100,'bbox':bbox}]}
    o=llm.RecordedOcr({'h':rec})
    r=extract.apply_ocr(extract.parse_message(''),'',['h'],o)
    emit('T03-'+tag,{'numbers':o.read('h').numbers,'kind':r.kind,'reasons':r.reason_codes,'bbox_count':len(r.bboxes)})
for size in [[8],['bad',8],[8,float('nan')]]:
    o=llm.RecordedOcr({'h':{'size':size,'text':'BTC 做多 入场 100','numbers':[{'value':100,'bbox':[0,0,1,999]}]}})
    try:
        out=dataclasses.asdict(o.read('h'))
    except Exception as err:
        out={'error':type(err).__name__,'message':str(err)}
    emit('U01-size-'+str(size),out)
# Current recorded images: PNG actual dimensions, only read local fixture media.
ocr=json.loads(pathlib.Path('tests/data/fixtures/llm_recorded/ocr_v1.json').read_text())
pngs={}
for p in pathlib.Path('tests/data/fixtures/tdesktop_sample').rglob('*.png'):
    b=p.read_bytes()
    if b[:8]==b'\x89PNG\r\n\x1a\n':
        pngs[hashlib.sha256(b).hexdigest()]=list(struct.unpack('>II',b[16:24]))
emit('T03-fixture', [{'hash':h,'actual':pngs.get(h),'recorded':r.get('size'),'numbers':len(r.get('numbers',[])),'all_valid':all(llm.valid_ocr_number(n,r.get('size')) for n in r.get('numbers',[]))} for h,r in ocr['items'].items()])
# S10 omitted MV input; no file writes.
mv=pl.read_parquet(lay.message_version); cp=pl.read_parquet(lay.canonical_plan)
cb=pl.read_parquet(lay.silver_dir/'candidate_edges.parquet');jd=pl.read_parquet(lay.silver_dir/'adjudications.parquet');dg=pl.read_parquet(lay.duplicate_group)
root=cp.filter((pl.col('message_id')==103)&(pl.col('channel_id')==-1002000000001)&(pl.col('extractor_name')=='parser')).row(0,named=True)
key=pl.col('root_plan_id')==root['plan_id']
mv2=mv.with_columns(pl.when(pl.col('source_version_id')==root['source_version_id']).then(pl.lit(root['available_at']+timedelta(days=1))).otherwise(pl.col('available_at')).alias('available_at'))
orig=pl.read_parquet
def ih(frame):
    def rd(p,*a,**k):
        if pathlib.Path(p)==lay.message_version:
            return frame
        return orig(p,*a,**k)
    with patch.object(lifecycle.pl,'read_parquet',side_effect=rd):
        return lifecycle.input_hash_of(lay,ingested_at=BUILD)
def build(c,m):
    return lifecycle.build_graph(c,m,cb,jd,dg,graph_version='r3-memory',ingested_at=BUILD)[0].filter(key).row(0,named=True)
b=build(cp,mv); changed=build(cp,mv2)
emit('S10-MV',{'input_hash_equal':ih(mv)==ih(mv2),'t_dec_before':b['t_dec'],'t_dec_after':changed['t_dec'],'snapshot_equal':b['decision_snapshot_hash']==changed['decision_snapshot_hash']})
# Same graph_version TP fraction change, avoiding test's different graph versions.
def fraction(v,root_id):
    return cp.with_columns(pl.when(pl.col('plan_id')==root_id).then(pl.lit([{'level':70000.,'fraction':v,'kind':'price'}],dtype=cp.schema['tps'])).otherwise(pl.col('tps')).alias('tps'))
f0=build(fraction(None,root['plan_id']),mv);f1=build(fraction(.25,root['plan_id']),mv)
emit('T02-same-version',{'unknown':f0['order_plan']['tps'],'known':f1['order_plan']['tps'],'snapshot_equal':f0['decision_snapshot_hash']==f1['decision_snapshot_hash']})
chosen=cb.filter((pl.col('to_plan_id')==root['plan_id'])&pl.col('selected'))
if chosen.height:
    changed=build(fraction(.75,chosen['from_plan_id'][0]),mv)
    emit('T02-future',{'snapshot_equal':b['decision_snapshot_hash']==changed['decision_snapshot_hash'],'plan_equal':b['order_plan']==changed['order_plan']})

migrated=api.load_episodes('fixture-v1')['episode_id'][0]
migration=pl.DataFrame([{'old_graph_version':gv,'predecessor_ids':[migrated]}])
with patch.object(graph,'read_migrations',return_value=migration):
    emit('X11-stale-resolved',{'version':gv,'episode_rows':api.load_episodes('fixture-v1').filter(pl.col('episode_id')==migrated).height,'event_rows':api.load_episode_events('fixture-v1').filter(pl.col('episode_id')==migrated).height})
emit('PUBLIC-eligibility',api.load_episodes('fixture-v1')['eligibility_by_estimand'].struct.unnest().null_count().to_dicts())

# R3_PROBE_END
```

## 11. 最终计数与终裁范围

T闭合2/4；P1残余S闭合3/9；原已闭合S02/S16维持；新增必修1条U01。
即使全部忽略七项P2设计工作与五项G0除外事项，缺列行情放行、跨品种改价、MV漏hash、manifest空元数据、未发送未来证据接受、缺标签pass及OCR尺寸异常仍在。
129个模块测试通过与D-07通过不覆盖上述实跑反例；本次终裁只针对P1合成链路，不推断真实PoC/LLM/放行能力。
三审终裁：fail
