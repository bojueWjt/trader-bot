# quant-lab G1 P1 第二轮独立复审（r2）

审查日期：2026-09-11（Asia/Manila）。审查者：当前 GPT-6 Codex 主会话；未委派 Grok、未调用其他模型。
范围：合成数据链路，逐条复核首轮 S01–S16，并核对 A01–A07。
裁定汇总：**closed 2/16（S02、S16）；partially 14/16；open 0/16；新增必修 4 条（T01–T04）。**
五项 G0 待裁决不作为未闭合理由；真实 PoC、真实 LLM、真实批次放行保持 gated，不计入本次合成终裁。
本会话唯一持久化仓库写入为本报告。pytest/旧版看板 verify 的临时产物按授权运行，未修改实现、测试、契约、看板或默认研究湖。

## 1. 基线、并发变化与证据口径

已读根 AGENTS.md、/Users/balen/.codex/GROK.md、首轮报告（含 §10）、taskList.json modules.data（含最后闭合记录、五项裁决与后续 04:44–04:45 更正）、research-schema v1 §9、ADR-G1、data 全模块、tests/data、TDesktop/LLM/OCR 夹具。
检查过 code-review skill 的适用范围；其 merge-base 差异双代理流程不适用于本次指定目录全量复核，未采用该 skill、未启动子代理。
树摘要算法与首轮一致：排除 __pycache__，按仓库相对路径排序，将每行「路径:文件 sha256\n」拼接后再 SHA256。路径前缀含 quant-lab/。

| 基线 | 源码树（17 文件） | 测试树（40 文件） |
|---|---|---|
| B0：首次固定 | fcaedb998f3f295072ed4fd462e01908f84e3b65cc1ce8c37b497020b8614135 | 6851a945e19ea14e26b9ebe3784403a6d21e53f547b9c61a2dc8f55264d017da |
| B1：并发修补后复核 | 06013114c75df8e3c90ee8ebbdf67c42c9f47382d69d7c5d9d898f8b9f55288b | 3a0250d5a2c609ce9ed01036ed4e0f2278b0c1586f92ccec39aead6e9bede706 |

首次 HEAD：59f9a27ba0df1b1968c36583731d7a0b6a9a3691；本报告审查工作树，不将 HEAD 当作未提交文件的证据。
research-schema sha256：82a04219cf139a59346d050d21644c82a0b0dbdc7611df3a40a0cd783a5cf67e。
ADR-G1 sha256：916615d0b13e3471c4f0dd5525ea897f6d6ca2377f3469ec24ce61d7997d3015。
首轮报告 sha256：d1dd9f92671140448dcd4cee63a85ff927e50ec9f699790fd254967e571da60b。
审查期间 api/lifecycle/test_lifecycle 等被其他会话更新；B0 的 cluster_id=null 观察已失效，本报告不把它列成当前缺陷。
B1 独立实跑：默认决策视图 23 行，instrument_id/t_dec/cluster_id null 均 0；内存重建描述图 36 行，cluster_id null 0。
B1 下再次全套 pytest 119 passed；本报告所有最终反例均在 B1 重新运行，脚本退出 0。退出 0 表示探针完成，**不表示业务断言全部通过**。
不采信 notes 的「已闭合」作为证据；例如 notes 的 delete_notice 保留、完整依赖闭包、全列不变性，均与下文实际返回冲突。

## 2. 验证命令与执行结果

cwd 均为 /Users/balen/projects/trader-bot/quant-lab。
统一环境：PYTHONDONTWRITEBYTECODE=1，PYTEST_ADDOPTS='-p no:cacheprovider'；shell 设置 set -o pipefail。
C1 为授权全套命令：

```sh
set -o pipefail
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' .venv-g1/bin/python -m pytest tests/data -q
```

B0：119 passed in 2.10s，rc=0。B1：119 passed in 2.11s，rc=0。
C2 为本报告附录中的独立只读探针，直接从报告加载，在内存构造反例，不生成脚本文件：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-g1/bin/python -c 'from pathlib import Path; s=Path("docs/adr/review-G1-P1-r2.md").read_text(); c=s.split("\n# R2_PROBE_BEGIN\n",1)[1].split("\n# R2_PROBE_END",1)[0]; exec(compile(c,"r2-readonly-probe","exec"))'
```

C2 读现有合成 Parquet；只替换内存数据/依赖注入。API 投影测试 mock 读文件与发布前置，专门测投影；manifest 测试单独走真实校验器和公共 API。
dry_run 拦截全部生命周期写入口，只获取真实 input_hash 计算结果；没有调用落盘、删除、迁移或撤权操作。
S16 除 C1 中实文件 sentinel 测试外，C2 另用解析路径替身和禁止 hash 钩子复现三种拒绝；不读取敏感文件。
C3 为看板原文只读核对：

```sh
python3 -c 'import json; d=json.load(open("taskList.json")); print("\n".join(t["id"]+" "+t["verify"] for t in d["modules"]["data"]["tasks"]))'
```

| verify | 实跑结果 | 解释 |
|---|---|---|
| D-01 | rc=1 | 旧 grep 'collected [1-9]' 与 pytest -q --co 输出不匹配；不是测试 collect 失败 |
| D-02 | rc=0 | ADR 行数及关键词门通过 |
| D-03 | rc=0；18 passed；193 MV；added=0；Q=3 | 固定 /tmp/ql-norm 已存在，不能把 added=0 当本次新建证据；pytest 独立临时目录另有非空断言 |
| D-04 | rc=0；11 passed | 含同文不同 source 不折叠 |
| D-05 | rc=0；17 passed；30 items | bench 实际 ../eval/v3_trader_signal_bench/dataset.json；recall=1.0、strict=0.75、skip_recall=1.0、tps_ok=0.9375 |
| D-06 | rc=0；8 passed | 显式 synthetic 夹具调用恢复；未来可用价已挡住 |
| D-07 进入时旧版 | rc=0；23 passed；公共 API 23 行 | 只运行 pytest 与读湖，没有重建默认湖 |
| D-07 当前新版 | 原命令 not_run；只读替代 rc=0，23 passed、23 行、三列非空 | 新版含 rm -rf，违背本次只读范围；没有执行，详见 T04 |
| D-08 | rc=1；NotImplementedError，harvest.py:21 | gated/todo，不计合成失败；没有报告或真实采集 |
| D-09 | rc=0；7 passed | p=0.5%：200/3=98.1319%，400/0=13.4658%；本会话 pipefail 已启用 |
| D-10 | 旧版 rc=0 假绿；新版 rc=1 | 并发改为正向检查最后一份报告终裁，本次最终应为非通过 |
| D-11 | rc=0；7 行 layer=5 非空 | 仅读合成损耗，不能证明真实 1b 完成 |

D-07 只读替代明确执行了 test_linker.py + test_lifecycle.py，并对 load_episodes('fixture-v1') 断言 height>=5 与三列 null_count=0。
未拿替代命令的 rc=0 冒称当前包含删除/重建的完整 verify 已运行。

## 3. S01–S16 逐条闭合裁定

证据命令 C2 的 Rxx/Xxx 标签在附录有完整输入及原始输出；C1 中点名测试均实际执行。
partially 表示已有子修复通过，但完整首轮验收仍有反例；不是五项待裁决的替代标签。

| S | 裁定 | 命令及实际输出 | 残余 / 闭合依据 |
|---|---|---|---|
| S01 | partially | C2 X01-original：A#103 根单独/完整图 n_events=[1,1]、diff=[]；R01：diff=[temporal_assumptions]；X01-correction：diff=[decision_snapshot_hash,eligibility_by_estimand]，execution true→false | 原反例核心已修；未来 correction 被完整 elig 带回 dec_elig，观察终点也留在公开列。全列与哈希不变性不成立 |
| S02 | closed | C2 R02：编辑 10:20、首次快照 10:25，available_at=10:25，10:21 visible=false；两 H1 EP t_dec/order_plan=null，entry_decision/execution/original_entry=false；C1 test_ce1_edited_sl_not_backfilled_and_not_revived 通过 | H1 不再由 edit+60s 提前可知，不复活旧入场；V 配对子例旧 SL=61000、新 amend 不回填 |
| S03 | partially | C2 R03a：T+1d 中间父帖将子边推迟到 T+1d；R03b：相册必要成员晚一天，t_dec 仍 2024-03-04 08:31:01，deps 无晚媒体 | 只补了 reply 中间父；根闭包仍仅 max(root.available_at,MV.available_at)，没有字段/相册/市场/校准 DFS，sequence 仍由 message_id 填充 |
| S04 | partially | C2 R04：未来 available_at 的 123 返回 MARK_STALE；缺 available_at 返回 123；X04-null：null available_at 也返回 123；无 n_samples 校准返回 [0.1,ok] | 明确未来值被挡住；未知时钟依旧放行，校准缺样本数被当有效，registry 只有有效期而无可知证据 |
| S05 | partially | C2 R05a：ETH short reply BTC long → weak/unresolved；R05b：A#103 与 #999 均 canonical；R05c：同消息两腿 r2→r1 被当 same_source，管理 s→r1 为 reply | 原冲突与同文子例已修；first_of_source 仍吞分支，未验证同源是否真版本链，quote/作者命名空间未闭合。同文政策等待 G0 不计残余 |
| S06 | partially | C2 R06a：分号成交量例 entry=6/stop=5/TP=7；R06b/d：无入场 entry_mode=unknown/order_plan=null；R06c：FIL 6/5/7 + BTC 阻力6.5万 → 60000/50000/70000 | 非价格单位子例修好，但其他品种的价格单位仍全消息传播；无依据均分已移除；显式 fraction 丢失另列 T02 |
| S07 | partially | C2 R07：混合码主因 UNIT_SCALE_CONFLICT，fatal；X07：RAW_HASH_MISMATCH/SCHEMA_DRIFT/KEY_DUPLICATE_OR_ORDER/MEDIA_MISSING 继承后 execution=true；方向冲突 Q 为 INTENT_AMBIGUOUS/general | 原 max severity 修好；跨层虽保留码，未按完整用途限制消费；方向错仍被一般严重度低估 |
| S08 | partially | C2 R08：asserted_event_time 在、enum 合规、codes 别名相同且29、公共签名正确、普通 EE 源时钟差0；X08-basis：版本链 basis 正确；X08-all-clock：expire.available_at=05-21 09:00，所引源为05-20 09:01 | decimal/固定 struct 仅待 G0，不算失败；但冻结 §9.1 的 EE 源时钟仍被合成 expire 覆盖。event_time/edge_available_at 可取到期时刻，available_at 应保留源时钟，或另获明确例外 |
| S09 | partially | C2 R09：原提前观察截止例 active/censor_at=05-20 10:00，未来 expire=0；delete_parse=delete_notice、delete_allowed=false；观察终点后根仍18个 | 到期子例与 L 补丁限制已修；delete_notice 在 validate.SIGNAL_KINDS 前被丢，deleted_after_observation 恒空；J/R 仅标记，缺真实分支守卫与重放/失效闭合 |
| S10 | partially | C2 R10a：同文 edit+seen 都变为2行，仅 edit 变仍1行；R10b：改 expires_after_s，input_hash_equal=true，而 order_plan 不同；C1 延迟/选边/首次 ingested 测试通过 | 部分输入纳入 hash，但期限、MV/DG、完整用途与证据仍漏；同名发布可在相同 input_hash 下改语义，无法称不可变 |
| S11 | partially | C2 X11-public-missing：LookupError；X11-public-empty-files：仍返回23行；X11-stale：episode_rows=0，但 event_rows=1 | 普通缺 manifest/hash/tombstone 子例已修；不完整清单绕过见 T01，G1 自有事件 API 缺 stale 屏障。跨模块回执/备份恢复待 G0 不计未闭合 |
| S12 | partially | C2 R12：各行计数恒等式=true；L1=193/193、L3=188/196、L4=57/57；L4 缺139个上层 EX 映射、L5 缺20个 CP、L6 缺5个 CP | 数字形式守恒、三时钟列、MAP 与未知权重为空已修；输入框由过滤后的对象反推，负类/无价/LLM 输出消失无显式退出账，不能据局部恒等式证明全链守恒 |
| S13 | partially | C2 R13：纯图录制能产 entry；负 bbox 被接受，短 bbox IndexError；decision cutoff 后证据被裁成 link；X13：十倍图文冲突 reasons=[]；21候选裁成20，选未发送第21仍 link | OCR/裁决协议已搭好且 forged-id 常规测试通过；完整 bbox/字段/实际上下文/预算校验不足，不因真实 LLM gated 豁免这些离线错误 |
| S14 | partially | C2 R14：仅 episode_id 空标签 insufficient；三抽中窗/一正窗返回 n_windows=3；双方 kind=null 的部分空标签却 verdict=pass；n=201、attempt=0、空样本hash/空复核人仍 pass | 原两个子例已修；缺完整标签/固定样本/有效独立签字和正式历史仍可通过，尚未形成审核器 |
| S15 | partially | C1 119 passed，独立手写转移 golden/五例均跑；C2 R15：192 sources/193 MV，只有2024/2025、V/H0/H1/U；R01/R03/R12/R13/R14 仍击穿 | 比首轮提升，但非 ADR 的216 sources/246 MV/seed=20260911；H2、可读真实合成图、逐例完整 hash/Q/LOSS、守卫与恢复矩阵不足；不能以119绿代替闭合 |
| S16 | closed | C2 R16：绝对/../symlink 分别 absolute_path/parent_escape/symlink_escape，sha256=null/exists=false，禁止hash钩子未触发；C1 test_media_path_escape_rejected 通过 | 两种源读取器均先 safe_media_path，根内媒体正向/缺媒体隔离已测；实文件 sentinel 不读入、不复制到湖 |

## 4. 六项关键性质的独立裁决

1. **决策视图全列不变性：未通过。** 固定 ingested_at、根、图版本，C2 对公共投影所有列逐一比较，没有 drop hash/batch 等列。首轮 A#103 普通后续事件例现在稳定，但未来 correction 改变 execution 与 hash，观察终点改变 temporal_assumptions；lifecycle.py:301 从完整 elig 导出 dec_elig 是直接原因。
2. **H1 不复活：通过。** 独立构造首次快照10:25的 H1，10:21不可用，公共合成 EP 原始入场用途不再准入；实收版本旧 SL 测试继续通过。
3. **manifest 屏障：未通过。** 缺文件本身的旧反例现拒绝；published+files={} 却能完整读取23行。此问题位于 G1 自有校验器，不属于 G0 的跨模块回执延期。
4. **路径逃逸：通过。** C2 使用不敏感虚构路径及解析替身，hash调用一旦发生立即失败；三种均在读取前拒绝；C1 实文件测试补齐 symlink/正常路径/复制行为。
5. **损耗守恒：未通过。** 直接从上层实物提取 ID 集与 MAP 输入集合做差，139/20/5 个对象缺去向账。非信号合法退出也必须记账，不能先筛掉再用剩余行自证 input_n。
6. **OCR/裁决离线协议：未通过。** 三条录制 OCR 指向8×8单色图，bbox 却可到120×72；provider录制存在不等于可读图验证成立。裁决只检查全 req 的 ID，不核对真正发送的截断集合与截止。

## 5. 首轮后本轮新发现的必修

以下是新发现或新补入机制中的具体错误；若缺可靠旧快照，不推断其具体引入提交。
这些条目与对应 S 残余有关联，不把同一问题重复增加到 S 分母；新增必修条数为4。

### T01：manifest 空清单让发布屏障失效

- 位置：src/quant_lab/data/graph.py:82，尤其91行；publish_manifest:58。
- 问题：仅遍历 doc.get('files',{})，不要求 EP/EE 两项齐全，也不核验必备元数据或 doc.graph_version 与请求一致；发布端也只收 exists 的文件。
- 实证命令：C2 R11-empty_files/R11-no_files、X11-public-empty-files；返回 ACCEPTED/23行。
- 改法：在校验前固定必需文件集合及 schema，核对 graph_version/输入hash/规则/假设/计数等，严格要求两份文件完整；发布端缺任一文件不得提交指针。
- 验收：缺files、空files、只EP、只EE、错graph_version、缺必要元数据均 LookupError；两个正确文件才可读。验证前后代际一致，不能校验旧文件后消费新文件。

### T02：作者明确 TP fraction 在快照中被清空

- 位置：src/quant_lab/data/lifecycle.py:291；dec.tps 在重放时仅保留 level。
- 问题：把所有 TP 重构成 fraction=None，连有来源的0.25也丢失；修「不猜均分」变成「删除已知分配」。
- 实证命令：C2 R06e，将合法 CP TP fraction=0.25，gold order_plan.tps 返回 fraction=null。
- 改法：决策重放保留完整 TP struct 及来源，未知才留空；amend 的分配也按字段证据更新。
- 验收：已知0.25原样保留、未知仍null、未来修改不回填；每个字段都进入快照hash，不能让G2补政策掩盖作者事实丢失。

### T03：新增 OCR 路径接受越界坐标，并可被短 bbox 中断整批

- 位置：src/quant_lab/data/extract.py:502、536；tests/data/fixtures/llm_recorded/ocr_v1.json；gen_ocr_recorded.py。
- 问题：未验证长度/有限性/坐标顺序/图片尺寸；短数组直接索引抛 IndexError。当前三张录制对应图片均8×8，录制坐标本身已越界，仍作为成功金样本。
- 实证命令：C2 R13-ocr-[-1,0,2,1] 返回正常 entry_proposal，R13-ocr-[0] 返回 IndexError；独立解析 PNG IHDR 得三张均(8,8)，录制最大x1=120/y1=72。
- 改法：在 OCR provider 边界按媒体尺寸核验全部结构与每字段引用；格式坏隔离并有界拒答；用真实离线文字绘制的合成图替换单色占位，重录 bbox。
- 验收：负数/短数组/NaN/越界/错图hash/无bbox数字都拒收且不崩批；可读图与模糊图成对，OCR数字能回指实际图中文字。

### T04：D-07 verify 先删除研究湖，绕过不可变与保留屏障

- 位置：taskList.json modules.data.tasks[id=D-07].verify（并发更新后的当前值，见C3）。
- 问题：当前以 rm -rf data/lake/telegram data/quarantine/telegram.parquet 开头；即使已从删除整个data根收缩范围，仍清除同名版本冲突证据、撤权/迁移/隔离历史。后续构建失败则先破坏可消费原版本。
- 实证：C3 实读上述前缀；本会话没有执行。04:45 notes 自报曾两次删除共享data根，仅作为外部自报记录，不声称本轮独立证实历史事故。
- 改法：verify 在独立临时 QUANT_LAB_DATA_ROOT 构建后断言；正式发布使用新graph_version与staging/单一指针，绝不先删既有湖/Q来使同名重跑通过。
- 验收：既有湖与Q/tombstone摘要不变；故意使新构建失败仍能消费原版本；临时构建验证和发布验证分开，verify不含共享持久数据删除。

## 6. 五项 G0 待裁决：现状真实性

| 项目 | 实核现状 | 本轮处理 |
|---|---|---|
| decimal/嵌套类型 | EX.stop=Float64；EP outcome固定struct；eligibility固定六键struct，与notes一致 | 待G0定稿，不计S08未闭合理由；不能宣称物理schema已最终获批 |
| CR-07全量回执 | 有manifest hash、图tombstone、迁移索引；未见G2/G3回执与备份先撤权协议 | notes对“仅G1部分”的描述属实，不计延期；G1自身空清单与EE stale漏洞仍需修 |
| A04规则 | market_check_row对两端逐一≥ln3判断；C1宽区间用例通过 | notes属实；是否改为仅告警由G0裁定，不作为失败理由 |
| codes别名 | codes.ReasonCode is reasons.Reason=True，29项；纯别名恢复 | 属实，当前公共路径已满足；删文件指令冲突等G0确认，不算阻断 |
| 同文不折叠 | R05b两 source 都canonical，第二行为repost_same_channel | 属实；保留候选和种子数偏多是待裁决政策，不算S05残余 |

## 7. A01–A07 处理状态与应改

| A | 状态 | 实核证据 / 剩余 |
|---|---|---|
| A01 | partially | D-05路径已修，D-10并发改正向判读；D-01 grep仍坏，D-03固定/tmp仍复用，D-09仍单个OR grep；D-07改为删除重建产生T04 |
| A02 | closed（所点指标修正） | 字段已名为skip_recall；score_item TP双向集合包含；bench tps_ok由1降为0.9375。不得把30条bench当全窗口召回 |
| A03 | partially | near行有usable_for_cluster=false，但DSU先把near合入组，早成员仍可标true；lifecycle dec_group不检查该标记，未校准近似关系仍可污染簇 |
| A04 | deferred | 两端门已实现，G0待裁决，不计未闭合 |
| A05 | partially | edited_unixtime损坏标VERSION_TIME_UNKNOWN；仍缺完整零/负/NaN/Infinity输入拒收，FrameMarks未知可知时钟放行见S04 |
| A06 | partially | author_id传递及无cohort的V→unknown、text_raw已加；content_hash仍hash规范化text，sequence仍message_id，原始实体偏移与规范文本可能不一致 |
| A07 | closed（所点断言修正） | mark断言已拆分为price非空/close_time≤at；转移表改独立手写golden；但S15完整矩阵仍未完成 |

应改：
- 将A01剩余命令问题与T04一起修复，明确“真实gated”与合成verify的不同含义。
- A03按复制边而非单成员标志建立可消费簇，禁止近似边经连通分量间接进入决策簇。
- A05用完整边界校验拒收异常数值；不能把log_deviation返回None后的路径当scale_gate=ok。
- A06冻结原文/规范文坐标与hash语义、作者证据和顺序证据，不能依赖字段名存在宣称已完成。
- S15优先补当前能击穿实现的独立反例，再补规格数量；不要只增加与实现同构的恒等式断言。
- S12需要逐对象、逐用途、明确单位转换的退出账；review/隔离是否进入分母必须显式，累计ID不能混用跨层命名空间后声称对象并集。

## 8. 可选

- 保留首轮O01：小合成集可容忍dedup O(n²)，规模增长前再做索引预筛及召回测量。
- 保留首轮O02：清理未用变量、过长dict表达式与密集逻辑；不以风格问题替代正确性修复。
- 保留首轮O03：为交付生成机器可读证据摘要（树hash、图manifest、pytest/verify退出码及gated项目）。
- r3前应停止同一交付文件的并发覆写，或交付可定位快照；本轮已将并发状态分别取证，不能把B0测试证明挪到B1。

## 9. 可重复运行的独立探针

以下代码是C2正文，包含首轮反例或等价只读反例和本轮新反例。测试辅助模块只借输入构造器，不借其期望值。
路径逃逸测试不访问真实秘密；全部mock仅在进程内生效。默认湖必须仍是上述fixture-v1合成制品。
脚本部分标签顺序只用于定位，不是结论优先级。

```python
# R2_PROBE_BEGIN
import copy, dataclasses, hashlib, inspect, json, pathlib, runpy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
import polars as pl
from quant_lab.data import api, audit, codes, dedup, extract, graph, lake, lifecycle, linker, llm, normalize, reasons, sources, validate
from quant_lab.data.market_stub import FrameMarks, SyntheticMarks, PlausibilityCalibration, fixture_registry
T = datetime(2024,6,10,9,1,tzinfo=UTC)
BUILD = datetime(2026,9,11,tzinfo=UTC)
lay = lake.Layout.from_root()
mv = pl.read_parquet(lay.message_version)
cp = pl.read_parquet(lay.canonical_plan)
cb = pl.read_parquet(lay.silver_dir/'candidate_edges.parquet')
jd = pl.read_parquet(lay.silver_dir/'adjudications.parquet')
dg = pl.read_parquet(lay.duplicate_group)
ex = pl.read_parquet(lay.extracted_event)
def emit(k, v):
    print(k, json.dumps(v, ensure_ascii=False, default=str))
def build(c=cp, m=mv, b=cb, j=jd, d=dg, **kw):
    return lifecycle.build_graph(c,m,b,j,d,graph_version='r2-memory',ingested_at=BUILD,**kw)
def project(ep):
    with patch.object(api,'verify_manifest'), patch.object(api,'stale_episodes',return_value=set()), patch.object(api.pl,'read_parquet',return_value=ep):
        return api.load_episodes('r2-memory')
ep, ev, _, _, _ = build()
root = cp.filter((pl.col('message_id')==103)&(pl.col('channel_id')==-1002000000001)&(pl.col('extractor_name')=='parser')).row(0,named=True)
rid = root['plan_id']
key = pl.col('root_plan_id')==rid
base_view = project(ep).filter(key)
# S01: fixed root and observation clock perturbation, all columns compared.
ep2,*_ = build(observation_end=datetime(2026,1,1,tzinfo=UTC))
other = project(ep2).filter(key)
emit('R01', {'rows':[base_view.height,other.height], 'diff':[c for c in base_view.columns if base_view[c].to_list()!=other[c].to_list()]})
# H1 actual original example and independent 10:20/10:25 normalization without media I/O.
raw = next(m for m in sources.read_all(pathlib.Path('tests/data/fixtures/tdesktop_sample')) if m.channel_id==-1002000000001 and m.message_id==120)
raw=dataclasses.replace(raw,media=[],export_snapshot_at=datetime(2024,4,2,10,25,tzinfo=UTC))
nm,*_=normalize.normalize_messages([raw],lay,ingested_at=BUILD)
emit('R02', {'available_at':nm['available_at'][0], 'at_1021_visible':nm['available_at'][0] < datetime(2024,4,2,10,21,tzinfo=UTC),'h1':ep.filter(pl.col('time_grade_min')=='H1').select('t_dec','order_plan','eligibility_by_estimand').to_dicts()})
# Linker fixtures supply only constructors; no expected values reused.
h=runpy.run_path('tests/data/test_linker.py')
cr,mr,frames=h['_cp_row'],h['_mv_row'],h['_frames']
c,m=frames([cr('r',10,'entry_proposal',T),cr('p',20,'stop_move',T+timedelta(days=1)),cr('s',30,'close_claimed',T+timedelta(hours=1))],[mr('r',10),mr('p',20,reply=10),mr('s',30,reply=20)])
b,j,_=linker.build_candidates(c,m)
emit('R03a', b.filter((pl.col('from_plan_id')=='s')&pl.col('selected')).select('edge_available_at','dependency_refs').to_dicts())
# album necessary member late; current builder does not see grouped_id or its clock.
rm=mv.filter(pl.col('source_version_id')==root['source_version_id']).row(0,named=True)
rm['grouped_id']=777
late=copy.deepcopy(rm);late.update(source_version_id='r2-late-media',source_id={'peer_id':rm['channel_id'],'message_id':99999},available_at=root['available_at']+timedelta(days=1),media_hashes=['late-image'])
m2=pl.concat([mv.filter(pl.col('source_version_id')!=root['source_version_id']),pl.DataFrame([rm,late],schema=mv.schema)])
e2,*_=build(m=m2)
emit('R03b',{'before':ep.filter(key)['t_dec'][0],'late_media':late['available_at'],'after':e2.filter(key)['t_dec'][0],'deps':e2.filter(key)['dependency_refs'][0].to_list()})
# S04 original late available bar; boundary and missing availability.
inst=root['instrument_id']
bars=pl.DataFrame({'instrument_id':[inst],'interval':['1m'],'open_time':[T-timedelta(minutes=2)],'close_time':[T-timedelta(minutes=1)],'open':[123.],'high':[123.],'low':[123.],'close':[123.],'volume':[0.],'event_time':[T-timedelta(minutes=1)],'available_at':[T+timedelta(days=1)],'ingested_at':[T]})
emit('R04',{'future':FrameMarks(bars).mark_at(inst,T),'missing_available':FrameMarks(bars.drop('available_at')).mark_at(inst,T),'cal_no_samples':PlausibilityCalibration({(1,'limit'):0.1},version='v',frozen_at=T-timedelta(days=1)).lookup(1,'limit',T)})
# S05 original reply conflict and same-text new source.
c,m=frames([cr('r',10,'entry_proposal',T),cr('s',20,'stop_move',T+timedelta(hours=1),side='short',inst='ETHUSDT-PERP.BINANCE-UM')],[mr('r',10),mr('s',20,reply=10)])
b,j,_=linker.build_candidates(c,m)
emit('R05a',{'edges':b.select('strength','selected','reason_codes').to_dicts(),'actions':j['action'].to_list()})
dup=copy.deepcopy(rm);dup.update(source_version_id='r2-copy',source_id={'peer_id':rm['channel_id'],'message_id':999},available_at=rm['available_at']+timedelta(days=1))
d,*_=dedup.dedup_frame(pl.DataFrame([rm,dup],schema=mv.schema),ingested_at=BUILD)
emit('R05b',d.select('message_id','is_canonical','dup_kind').to_dicts())
# same-source two legs, no version change: should not be version amendment.
c,m=frames([cr('r1',10,'entry_proposal',T),cr('r2',10,'entry_proposal',T,side='short',inst='ETHUSDT-PERP.BINANCE-UM'),cr('s',20,'stop_move',T+timedelta(hours=1))],[mr('r1',10),mr('r2',10),mr('s',20,reply=10)])
b,j,_=linker.build_candidates(c,m)
emit('R05c',b.filter(pl.col('selected')).select('from_plan_id','to_plan_id','method').to_dicts())
# S06 old unit counterexample and cross-instrument unit.
for tag,text in [('R06a','BTC 做多 入场 6 止损 5 止盈 7；历史成交量 6.5万'),('R06b','BTC 做多 止损 90'),('R06c','FIL 做多 入场 6 止损 5 止盈 7；BTC 阻力 6.5万')]:
    p=extract.parse_message(text)
    emit(tag,{'entry':p.entry,'stop':p.stop,'tps':p.tps,'entry_mode':p.entry_mode,'notes':p.notes})
r=copy.deepcopy(root);r['entry']=None;r['entry_mode']='unknown'
emit('R06d',lifecycle._order_plan(r,90,[],None))
# explicit author TP fraction must survive.
r=copy.deepcopy(root);r['tps']=[{'level':70000.,'fraction':0.25,'kind':'price'}]
c2=cp.with_columns(pl.when(pl.col('plan_id')==rid).then(pl.lit(r['tps'],dtype=cp.schema['tps'])).otherwise(pl.col('tps')).alias('tps'))
e2,*_=build(c=c2)
emit('R06e',e2.filter(key)['order_plan'][0]['tps'])
# S07 mixed severity + inherited missing media and direction severity.
q=lake.quarantine_row(batch_id='r2',object_kind='x',object_id='x',object_version='v',partition_id='p',reason_codes=['EDIT_ORIGINAL_UNAVAILABLE','UNIT_SCALE_CONFLICT'],rule_version='r',schema_hash_='s')
xr=ex.filter(pl.col('extract_id')==root['extract_id']).with_columns(pl.lit(['MEDIA_MISSING'],dtype=pl.List(pl.String)).alias('reason_codes'))
v,qrows,_,_=validate.validate_frame(xr,mv,registry=fixture_registry(),marks=SyntheticMarks({inst:[(datetime(2020,1,1,tzinfo=UTC),float(root['entry_ref']))]}),ingested_at=BUILD)
emit('R07',{'mixed':[q['reason_code'],q['severity']],'missing_media':v.select('reason_codes','eligibility_by_estimand').to_dicts()})
# S08 actual schemas/API/source clock
ee=api.load_episode_events_description('fixture-v1')
joined=ee.filter(~pl.col('payload').str.contains('"synthesized": true')).join(mv.select('source_version_id',pl.col('available_at').alias('mv_available')),on='source_version_id')
emit('R08',{'asserted_event_time':'asserted_event_time' in mv.columns,'evidence':mv['version_evidence'].unique().to_list(),'alias':codes.ReasonCode is reasons.Reason,'codes':len(codes.ReasonCode),'events_signature':str(inspect.signature(api.load_episode_events)),'quarantine_signature':str(inspect.signature(api.quarantine)),'source_clock_mismatches':joined.filter(pl.col('available_at')!=pl.col('mv_available')).height,'stop_type':str(ex.schema['stop']),'elig_type':str(ep.schema['eligibility_by_estimand'])})
# S09 original observation-end example plus actual delete propagation
end=datetime(2024,5,20,10,tzinfo=UTC)
e2,v2,*_=build(observation_end=end)
emit('R09',{'deadline_root':e2.filter((pl.col('channel_id')==-1002000000001)&(pl.col('root_message_id')==140)).select('author_plan_state','censor_at').to_dicts(),'future_expire':v2.filter((pl.col('kind')=='expire')&(pl.col('edge_available_at')>end)).height,'delete_parse':extract.parse_message('BTC 已删除上一条').kind,'delete_allowed': 'delete_notice' in validate.SIGNAL_KINDS,'future_roots':e2.filter(pl.col('event_time')>end).height})
# S10 same-content changed edit+seen versions original, then unchanged first seen / edit changes.
a=dataclasses.replace(raw,first_seen_at=datetime(2024,4,2,10,25,tzinfo=UTC),media=[])
b=dataclasses.replace(a,first_seen_at=a.first_seen_at+timedelta(minutes=1),edited_unixtime=a.edited_unixtime+1)
n,*_=normalize.normalize_messages([a,b],lay,ingested_at=BUILD)
c=dataclasses.replace(a,edited_unixtime=a.edited_unixtime+1)
n2,*_=normalize.normalize_messages([a,c],lay,ingested_at=BUILD)
emit('R10a',{'both_clocks_changed_rows':n.height,'edit_only_changed_rows':n2.height})
# Capture lifecycle.run publication in memory; prohibit all writer side effects.
orig_read=pl.read_parquet
def dry_run(frame):
    docs=[]
    def rd(path,*args,**kwargs):
        if pathlib.Path(path)==lay.canonical_plan:
            return frame
        return orig_read(path,*args,**kwargs)
    with patch.object(lifecycle.pl,'read_parquet',side_effect=rd),patch.object(lifecycle,'is_tombstoned',return_value=False),patch.object(lifecycle,'read_manifest',return_value=None),patch.object(lake.Layout,'ensure'),patch.object(lifecycle,'write_parquet_atomic'),patch.object(lifecycle,'append_quarantine'),patch.object(lifecycle,'write_loss'),patch.object(lifecycle,'write_mapping'),patch.object(lifecycle,'publish_manifest',side_effect=lambda *a,**k:docs.append(k)):
        return lifecycle.run(lay,graph_version='r2-memory',ingested_at=BUILD)['input_hash']
c2=cp.with_columns(pl.when(pl.col('plan_id')==rid).then(pl.lit(3600)).otherwise(pl.col('expires_after_s')).alias('expires_after_s'))
e2,*_=build(c=c2)
emit('R10b',{'input_hash_equal':dry_run(cp)==dry_run(c2),'plan_equal':ep.filter(key)['order_plan'].to_list()==e2.filter(key)['order_plan'].to_list()})
# S11 missing manifest and vacuous files. No disk mutation.
for tag,doc in [('missing',None),('empty_files',{'status':'published','files':{}}),('no_files',{'status':'published'})]:
    with patch.object(graph,'read_manifest',return_value=doc),patch.object(graph,'is_tombstoned',return_value=False):
        try:
            graph.verify_manifest(lay,'fixture-v1')
            out='ACCEPTED'
        except LookupError:
            out='LookupError'
    emit('R11-'+tag,out)
# S12 independently derive denominators from physical outputs vs mapping inputs.
loss=api.loss_table('latest')
emit('R12a',{'conserved_rows':(loss['input_n']==loss['n_ok']+loss['n_review']+loss['n_quarantine']+loss['n_dup_ref']).all(),'totals':loss.group_by('layer').agg(pl.col('input_n').sum(),pl.col('output_n').sum(),pl.col('cum_excluded_ids').sum()).sort('layer').to_dicts()})
batch=loss['batch_id'][0]
for layer,prev in [(4,set(ex['extract_id'])),(5,set(cp['plan_id'])),(6,set(cp['plan_id']))]:
    mp=pl.read_parquet(lay.mapping(batch,layer))
    emit('R12-L'+str(layer),{'previous_objects':len(prev),'mapped_inputs':mp['input_ref'].n_unique(),'unaccounted':len(prev-set(mp['input_ref']))})
# S13 original missing offline protocol now present; independent malformed bbox and adjudication cutoff.
for bbox in [[0,0,1,1],[-1,0,2,1],[0]]:
    o=llm.RecordedOcr({'h':{'text':'BTC 做多 入场 100 止损 90 止盈 110','numbers':[{'value':100,'bbox':bbox},{'value':90,'bbox':[0,0,1,1]},{'value':110,'bbox':[0,0,1,1]}]}})
    try:
        p=extract.apply_ocr(extract.parse_message(''),'',['h'],o)
        out={'kind':p.kind,'reasons':p.reason_codes,'bboxes':p.bboxes}
    except Exception as err:
        out={'error':type(err).__name__}
    emit('R13-ocr-'+str(bbox),out)
req=llm.AdjudicationRequest('r','e',[{'candidate_id':'c'}],[{'peer_id':1,'message_id':1,'source_version_id':'v','text':'future','available_at':(T+timedelta(days=1)).isoformat()}],T.isoformat(),purpose='decision')
payload={'action':'link','selected_candidate_id':'c','evidence_message_ids':[{'peer_id':1,'message_id':1,'source_version_id':'v'}],'rejected_candidate_ids':[]}
s,u=llm.build_adjudicate_prompt(req)
client=llm.RecordedClient({llm.record_key(s,u,llm.SCHEMA_NAME_ADJ):{'response':payload}})
emit('R13-adj',dataclasses.asdict(llm.adjudicate(req,client=client)))
emit('R13-numeric',llm.validate_evidence({'size_hint':{'fraction':2.0},'expires_after_s':42,'tps':[]},'BTC'))
# S14 original empty labels and negative windows; partial-empty evidence still passes.
empty=[{'episode_id':'e'}]
partial=[{'episode_id':'e','kind':None}]
emit('R14',{'empty':audit.disagreement_report(empty,empty),'partial':audit.disagreement_report(partial,partial),'windows':audit.weighted_recall({(1,'a'):1.,(1,'b'):1.,(1,'c'):1.},[((1,'a'),'e')],{'e'},n_boot=50),'unaudited':dataclasses.asdict(audit.acceptance_decision(n_sampled=201,errors_general=0,errors_fatal=0,sample_ids_hash='',reviewer_ids=('',),attempt=0))})
# S15 actual fixture specification without writing/generation.
raws=list(sources.read_all(pathlib.Path('tests/data/fixtures/tdesktop_sample')))
emit('R15',{'unique_sources':len({(r.channel_id,r.message_id) for r in raws}),'mv':mv.height,'grades':mv['time_grade'].unique().to_list(),'years':mv['event_time'].dt.year().unique().to_list()})
# S16 pure path check: existing non-sensitive repository path, simulated symlink resolve, hash hook forbidden.
base=pathlib.Path('tests/data/fixtures/tdesktop_sample/AlphaSignals').resolve()
for rel in ['/r2-sentinel','../r2-sentinel','photos/r2-link']:
    if rel.endswith('r2-link'):
        resolve=pathlib.Path.resolve
        def resolver(p,*a,**k):
            if p.name=='r2-link':
                return base.parent/'r2-sentinel'
            return resolve(p,*a,**k)
        ctx=patch.object(pathlib.Path,'resolve',resolver)
    else:
        from contextlib import nullcontext
        ctx=nullcontext()
    with ctx,patch.object(sources,'sha256_file',side_effect=AssertionError('must not hash')):
        refs=sources._media_refs({'photo':rel},base)
    emit('R16-'+rel,[{'reason':r.reject_reason,'sha256':r.sha256,'exists':r.exists} for r in refs])
# New/final delivery seam.
live=api.load_episodes('fixture-v1')
emit('C01-cluster',{'decision_rows':live.height,'nulls':live.select('instrument_id','t_dec','cluster_id').null_count().to_dicts(),'rebuilt_null_clusters':ep['cluster_id'].null_count()})
# future correction must not alter historical execution eligibility / hash.
chosen=cb.filter((pl.col('to_plan_id')==rid)&pl.col('selected'))
if chosen.height:
    pid=chosen['from_plan_id'][0]
    c2=cp.with_columns(pl.when(pl.col('plan_id')==pid).then(pl.lit('correction')).otherwise(pl.col('kind')).alias('kind'))
    e2,*_=build(c=c2)
    r2=project(e2).filter(key)
    emit('X01-correction',{'diff':[c for c in base_view.columns if base_view[c].to_list()!=r2[c].to_list()],'before':base_view['eligibility_by_estimand'].to_list(),'after':r2['eligibility_by_estimand'].to_list()})

# Additional independent checks; run after the R01-R16 script in the same process.
# No missing/invalid evidence may be laundered into execution.
for reason in ['RAW_HASH_MISMATCH','SCHEMA_DRIFT','KEY_DUPLICATE_OR_ORDER','MEDIA_MISSING']:
    xr=ex.filter(pl.col('extract_id')==root['extract_id']).with_columns(pl.lit([reason],dtype=pl.List(pl.String)).alias('reason_codes'))
    v,qs,_,_=validate.validate_frame(xr,mv,registry=fixture_registry(),marks=SyntheticMarks({inst:[(datetime(2020,1,1,tzinfo=UTC),float(root['entry_ref']))]}),ingested_at=BUILD)
    emit('X07-'+reason, json.loads(v['eligibility_by_estimand'][0]))
xr=ex.filter(pl.col('extract_id')==root['extract_id']).with_columns((pl.col('entry').struct.field('hi')*1.01).alias('stop'),pl.lit([],dtype=pl.List(pl.String)).alias('reason_codes'))
v,qs,_,_=validate.validate_frame(xr,mv,registry=fixture_registry(),marks=SyntheticMarks({inst:[(datetime(2020,1,1,tzinfo=UTC),float(root['entry_ref']))]}),ingested_at=BUILD)
emit('X07-direction',[(q['reason_code'],q['severity']) for q in qs])
emit('X04-null',FrameMarks(bars.with_columns(pl.lit(None,dtype=pl.Datetime('us','UTC')).alias('available_at'))).mark_at(inst,T))
# Default API includes all columns; full original prefix replay counterexample.
pids={rid}
c0=cp.filter(pl.col('plan_id').is_in(list(pids)))
e0,*_=build(c=c0,b=cb.head(0),j=jd.head(0),observation_end=datetime(2026,1,1,tzinfo=UTC))
ef,*_=build(observation_end=datetime(2026,1,1,tzinfo=UTC))
d0,df=project(e0).filter(key),project(ef).filter(key)
emit('X01-original',{'n_events':[d0['n_events'][0],df['n_events'][0]],'diff':[c for c in d0.columns if d0[c].to_list()!=df[c].to_list()]})
# Original absent manifest through public API with real EP/EE intact.
with patch.object(graph,'read_manifest',return_value=None):
    try:
        api.load_episodes('fixture-v1')
        val='ACCEPTED'
    except LookupError:
        val='LookupError'
emit('X11-public-missing',val)
with patch.object(graph,'read_manifest',return_value={'status':'published','files':{}}):
    emit('X11-public-empty-files',api.load_episodes('fixture-v1').height)
# stale predecessor index is checked only in episode API, event API still returns it.
migrated=api.load_episodes('fixture-v1')['episode_id'][0]
migration=pl.DataFrame([{'old_graph_version':'fixture-v1','predecessor_ids':[migrated]}])
with patch.object(graph,'read_migrations',return_value=migration):
    emit('X11-stale',{'episode_rows':api.load_episodes('fixture-v1').filter(pl.col('episode_id')==migrated).height,'event_rows':api.load_episode_events('fixture-v1').filter(pl.col('episode_id')==migrated).height})
# OCR large scale conflict and budget actually delivered to provider.
text='BTC 做多 入场 100 止损 90 止盈 110'
ocr=llm.RecordedOcr({'h':{'text':'BTC 做多 入场 1000 止损 900 止盈 1100','numbers':[{'value':v,'bbox':[0,0,1,1]} for v in [1000,900,1100]]}})
emit('X13-conflict',extract.apply_ocr(extract.parse_message(text),text,['h'],ocr).reason_codes)
req=llm.AdjudicationRequest('r','e',[{'candidate_id':str(i)} for i in range(21)],[{'peer_id':1,'message_id':1,'source_version_id':'v','text':'x'*100000}],T.isoformat())
s,u=llm.build_adjudicate_prompt(req)
payload={'action':'link','selected_candidate_id':'20','evidence_message_ids':[{'peer_id':1,'message_id':1,'source_version_id':'v'}],'rejected_candidate_ids':[str(i) for i in range(20)]}
client=llm.RecordedClient({llm.record_key(s,u,llm.SCHEMA_NAME_ADJ):{'response':payload}})
emit('X13-budget',{'sent_candidates':len(json.loads(u)['candidates']),'sent_chars':len(u),'selected_in_sent':any(c['candidate_id']=='20' for c in json.loads(u)['candidates']),'result':llm.adjudicate(req,client=client).action})
# Explicit physical version-chain basis.
emit('X08-basis',[json.loads(r['payload']).get('basis') for r in ee.filter(pl.col('link_method')=='plan_ref').to_dicts()])

allj=ee.join(mv.select('source_version_id',pl.col('available_at').alias('mv_available')),on='source_version_id')
emit('X08-all-clock',allj.filter(pl.col('available_at')!=pl.col('mv_available')).select('kind','available_at','mv_available').to_dicts())

# R2_PROBE_END
```

## 10. C2 原始输出

退出码：0。下列为实际 stdout；对象次序可能受 Polars 分组顺序影响，不影响字段比较结论。

```text
R01 {"rows": [1, 1], "diff": ["temporal_assumptions"]}
R02 {"available_at": "2024-04-02 10:25:00+00:00", "at_1021_visible": false, "h1": [{"t_dec": null, "order_plan": null, "eligibility_by_estimand": {"description": true, "entry_decision": false, "execution": false, "original_entry": false, "price_check": true, "outcome": false}}, {"t_dec": null, "order_plan": null, "eligibility_by_estimand": {"description": true, "entry_decision": false, "execution": false, "original_entry": false, "price_check": true, "outcome": false}}]}
R03a [{"edge_available_at": "2024-06-11 09:01:00+00:00", "dependency_refs": ["sv-r", "sv-p", "sv-r"]}]
R03b {"before": "2024-03-04 08:31:01+00:00", "late_media": "2024-03-05 08:31:00+00:00", "after": "2024-03-04 08:31:01+00:00", "deps": ["51b03b15bf2e7b4abb8b5242d7db24915dd1f2866b01cf16efb901b0717da61d", "c68d3c5f940588dc2f26e9d4fda7d3ba232e3e1cb1875b492b07e8efd1dba6ee", "38296d6ab0418c607a4f922cc68047aa7578eb7d19852d9a7e0c1f1ff335ccbb", "registry:"]}
R04 {"future": [null, "MARK_STALE", null, null], "missing_available": ["123.0", null, "2024-06-10 09:00:00+00:00", 60.0], "cal_no_samples": [0.1, "ok"]}
R05a {"edges": [{"strength": "weak", "selected": false, "reason_codes": ["ENTRY_LINK_AMBIGUOUS"]}], "actions": ["unresolved"]}
R05b [{"message_id": 103, "is_canonical": true, "dup_kind": "none"}, {"message_id": 999, "is_canonical": true, "dup_kind": "repost_same_channel"}]
R05c [{"from_plan_id": "r2", "to_plan_id": "r1", "method": "same_source"}, {"from_plan_id": "s", "to_plan_id": "r1", "method": "reply"}]
R06a {"entry": {"lo": 6.0, "hi": 6.0, "kind": "limit"}, "stop": 5.0, "tps": [{"level": 7.0, "fraction": null, "kind": "price"}], "entry_mode": "price", "notes": []}
R06b {"entry": null, "stop": 90.0, "tps": [], "entry_mode": "unknown", "notes": []}
R06c {"entry": {"lo": 60000.0, "hi": 60000.0, "kind": "limit"}, "stop": 50000.0, "tps": [{"level": 70000.0, "fraction": null, "kind": "price"}], "entry_mode": "price", "notes": ["unit_propagated:6.0->60000.0", "unit_propagated:6.0->60000.0", "unit_propagated:5.0->50000.0", "unit_propagated:7.0->70000.0"]}
R06d null
R06e [{"level": 70000.0, "fraction": null}]
R07 {"mixed": ["UNIT_SCALE_CONFLICT", "fatal"], "missing_media": [{"reason_codes": ["MEDIA_MISSING"], "eligibility_by_estimand": "{\"description\": true, \"execution\": true, \"original_entry\": true, \"price_check\": true}"}]}
R08 {"asserted_event_time": true, "evidence": ["live_receive", "edit_date", "export_snapshot"], "alias": true, "codes": 29, "events_signature": "(graph_version: 'str') -> 'pl.DataFrame'", "quarantine_signature": "(flow: 'str', *, status: 'str | None' = None) -> 'pl.DataFrame'", "source_clock_mismatches": 0, "stop_type": "Float64", "elig_type": "Struct({'description': Boolean, 'entry_decision': Boolean, 'execution': Boolean, 'original_entry': Boolean, 'price_check': Boolean, 'outcome': Boolean})"}
R09 {"deadline_root": [{"author_plan_state": "active", "censor_at": "2024-05-20 10:00:00+00:00"}], "future_expire": 0, "delete_parse": "delete_notice", "delete_allowed": false, "future_roots": 18}
R10a {"both_clocks_changed_rows": 2, "edit_only_changed_rows": 1}
R10b {"input_hash_equal": true, "plan_equal": false}
R11-missing "LookupError"
R11-empty_files "ACCEPTED"
R11-no_files "ACCEPTED"
R12a {"conserved_rows": true, "totals": [{"layer": 1, "input_n": 193, "output_n": 193, "cum_excluded_ids": 3}, {"layer": 2, "input_n": 188, "output_n": 188, "cum_excluded_ids": 3}, {"layer": 3, "input_n": 188, "output_n": 196, "cum_excluded_ids": 3}, {"layer": 4, "input_n": 57, "output_n": 57, "cum_excluded_ids": 5}, {"layer": 5, "input_n": 37, "output_n": 35, "cum_excluded_ids": 7}, {"layer": 6, "input_n": 52, "output_n": 36, "cum_excluded_ids": 7}]}
R12-L4 {"previous_objects": 196, "mapped_inputs": 57, "unaccounted": 139}
R12-L5 {"previous_objects": 57, "mapped_inputs": 37, "unaccounted": 20}
R12-L6 {"previous_objects": 57, "mapped_inputs": 52, "unaccounted": 5}
R13-ocr-[0, 0, 1, 1] {"kind": "entry_proposal", "reasons": [], "bboxes": [{"field": "stop", "media_hash": "h", "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}, {"field": "tps", "media_hash": "h", "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}, {"field": "entry.lo", "media_hash": "h", "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}]}
R13-ocr-[-1, 0, 2, 1] {"kind": "entry_proposal", "reasons": [], "bboxes": [{"field": "stop", "media_hash": "h", "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}, {"field": "tps", "media_hash": "h", "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}, {"field": "entry.lo", "media_hash": "h", "x0": -1.0, "y0": 0.0, "x1": 2.0, "y1": 1.0}]}
R13-ocr-[0] {"error": "IndexError"}
R13-adj {"action": "link", "selected_candidate_id": "c", "evidence_message_ids": [{"peer_id": 1, "message_id": 1, "source_version_id": "v"}], "rejected_candidate_ids": [], "reason_codes": [], "provider": "recorded", "attempts": 1, "extended": false, "note": "", "response_hash": "02e1c997ed69c7556491e8bed852ca639e173b33bf4d4f965e8aec2e29128e02"}
R13-numeric []
R14 {"empty": {"status": "insufficient", "n": 1, "missing_pairs": 0, "scored_fields": [], "note": "缺标注/缺配对/无可评字段：不出 pass"}, "partial": {"status": "ok", "n": 1, "missing_pairs": 0, "scored_fields": ["kind"], "field_disagreement": {"instrument_id": null, "side": null, "entry": null, "stop": null, "kind": 0.0}, "key_field_rate_max": 0.0, "whole_episode_rate": 0.0, "unresolved_rate": 0.0, "gates": {"key_field": 0.02, "whole_episode": 0.05, "unresolved": 0.05}, "verdict": "pass", "note": "原始意见分歧率；仲裁后的零分歧不得回填此表"}, "windows": {"status": "ok", "recall": 1.0, "interval": [1.0, 1.0], "interval_method": "cluster_bootstrap_all_sampled_windows_within_channel", "n_true": 1, "n_windows": 3, "n_windows_with_opportunity": 1}, "unaudited": {"result": "pass", "n": 201, "errors_general": 0, "errors_fatal": 0, "p_hat": 0.0, "interval": [0.0, 0.018754003093121707], "interval_method": "wilson_95_two_sided", "reason": "一般错 0 ≤ c=3；接受后仍记录非零错误估计，不宣称总体≤1%", "attempt": 0}}
R15 {"unique_sources": 192, "mv": 193, "grades": ["U", "H1", "V", "H0"], "years": [null, 2024, 2025]}
R16-/r2-sentinel [{"reason": "absolute_path", "sha256": null, "exists": false}]
R16-../r2-sentinel [{"reason": "parent_escape", "sha256": null, "exists": false}]
R16-photos/r2-link [{"reason": "symlink_escape", "sha256": null, "exists": false}]
C01-cluster {"decision_rows": 23, "nulls": [{"instrument_id": 0, "t_dec": 0, "cluster_id": 0}], "rebuilt_null_clusters": 0}
X01-correction {"diff": ["decision_snapshot_hash", "eligibility_by_estimand"], "before": [{"description": true, "entry_decision": true, "execution": true, "original_entry": true, "price_check": true, "outcome": null}], "after": [{"description": true, "entry_decision": true, "execution": false, "original_entry": true, "price_check": true, "outcome": null}]}
X07-RAW_HASH_MISMATCH {"description": true, "execution": true, "original_entry": true, "price_check": true}
X07-SCHEMA_DRIFT {"description": true, "execution": true, "original_entry": true, "price_check": true}
X07-KEY_DUPLICATE_OR_ORDER {"description": true, "execution": true, "original_entry": true, "price_check": true}
X07-MEDIA_MISSING {"description": true, "execution": true, "original_entry": true, "price_check": true}
X07-direction [["INTENT_AMBIGUOUS", "general"]]
X04-null ["123.0", null, "2024-06-10 09:00:00+00:00", 60.0]
X01-original {"n_events": [1, 1], "diff": []}
X11-public-missing "LookupError"
X11-public-empty-files 23
X11-stale {"episode_rows": 0, "event_rows": 1}
X13-conflict []
X13-budget {"sent_candidates": 20, "sent_chars": 100736, "selected_in_sent": false, "result": "link"}
X08-basis ["same_source_version_chain", "frozen_deadline_in_proposal"]
X08-all-clock [{"kind": "expire", "available_at": "2024-05-21 09:00:00+00:00", "mv_available": "2024-05-20 09:01:00+00:00"}]
```

## 11. 最终裁决范围

S闭合：2/16；其余14项部分闭合。新增必修：4条。待G0裁决的五项不计未闭合。
119个模块测试通过，证明已有正常路径与部分反例修复成立；不能覆盖本轮实跑仍存在的前视、闭包、未知证据放行、损耗漏账与离线协议错误。
实际存在已复现错误，因此合成链路不是insufficient，而是fail。真实PoC/真实LLM/真实放行仍gated，不据此推断真实能力通过或失败。
二审终裁：**fail**

